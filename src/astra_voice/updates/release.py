"""Bounded release metadata, verified during checking before publishing available.

Only metadata is fetched here; downloading a package requires a separate explicit
user action. Local metadata is always verified again, never a persisted trusted
flag. The cancellation event also reaches Verifier: subprocess cancellation is
polled every 50 ms, followed by TERM/KILL and reaping of its own child (200 ms
TERM grace, subject to OS scheduling). The caller still joins the worker.
Metadata reads and hashes are bounded; no total check-duration guarantee is made.
"""

from __future__ import annotations

import json
import os
import re
import stat
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from astra_voice.core.paths import PathError, cache_dir_path, ensure_private_dir
from astra_voice.net.github import installed_version, parse_semver
from astra_voice.net.http import (
    RATE_LIMIT_MIN_S,
    HttpClient,
    NetworkError,
    backoff_seconds,
    parse_rate_limit,
)
from astra_voice.security.verify import Verifier

REPOSITORY_URL = "https://github.com/ArrivaRUS/astra-voice"
MAX_SUMS_BYTES = 1 << 20
MAX_SIGNATURE_BYTES = 64 << 10
MAX_LATEST_BYTES = 64 << 10
MAX_ARTIFACT_BYTES = 2 << 30
_METADATA = (
    ("SHA256SUMS", MAX_SUMS_BYTES),
    ("SHA256SUMS.asc", MAX_SIGNATURE_BYTES),
    ("latest.json", MAX_LATEST_BYTES),
)
_STABLE = re.compile(r"(?:0|[1-9][0-9]{0,8})\.(?:0|[1-9][0-9]{0,8})\.(?:0|[1-9][0-9]{0,8})")
_SUM = re.compile(r"([0-9a-f]{64}) [ *]([A-Za-z0-9][A-Za-z0-9_.+~-]*)")
Track = Literal["deb", "appimage"]
CheckKind = Literal["check_app", "check_app_manual"]


class ReleaseMetadataError(Exception):
    """Stable code without attacker-controlled paths, JSON or signature output."""

    def __init__(self, code: str, *, retry_at: float | None = None) -> None:
        self.code = code
        self.retry_at = retry_at
        super().__init__(code)


@dataclass(frozen=True)
class VerifiedArtifact:
    track: Track
    name: str
    sha256: str
    size: int
    url: str


@dataclass(frozen=True)
class VerifiedRelease:
    version: str
    raw_tag: str
    published_at: str
    min_astra: str
    release_url: str
    artifact: VerifiedArtifact
    sums: bytes
    signature: bytes
    latest: bytes


def _cancelled(cancel: threading.Event) -> None:
    if cancel.is_set():
        raise ReleaseMetadataError("cancelled")


def _version(raw_tag: str, current_version: str, track: Track) -> str:
    if track not in ("deb", "appimage"):
        raise ReleaseMetadataError("unsupported-track")
    version = raw_tag.removeprefix("v")
    if _STABLE.fullmatch(version) is None:
        raise ReleaseMetadataError("invalid-tag")
    candidate = parse_semver(version)
    current = installed_version(current_version)
    if current is None:
        raise ReleaseMetadataError("invalid-installed-version")
    if candidate is None or not candidate > current:
        raise ReleaseMetadataError("version-not-newer")
    return version


def _no_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ReleaseMetadataError("metadata-invalid")
        result[key] = value
    return result


def _no_symlinks(path: Path) -> None:
    if not path.is_absolute() or ".." in path.parts:
        raise ReleaseMetadataError("path-unsafe")
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ReleaseMetadataError("path-unsafe")


def _read(path: Path, limit: int) -> bytes:
    _no_symlinks(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise ReleaseMetadataError("path-unsafe")
        if info.st_size > limit:
            raise ReleaseMetadataError("metadata-too-large")
        body = stream.read(limit + 1)
    if len(body) > limit:
        raise ReleaseMetadataError("metadata-too-large")
    return body


def _write(path: Path, body: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(body)


def _signed_sums(body: bytes) -> dict[str, str]:
    try:
        lines = body.decode("utf-8").splitlines()
    except UnicodeError:
        raise ReleaseMetadataError("metadata-invalid") from None
    result: dict[str, str] = {}
    for line in lines:
        if not line.strip():
            continue
        match = _SUM.fullmatch(line)
        if match is None or match[2] in result:
            raise ReleaseMetadataError("metadata-invalid")
        result[match[2]] = match[1]
    return result


def _schema(
    latest: bytes, sums: bytes, version: str, raw_tag: str, track: Track
) -> VerifiedArtifact:
    try:
        data = json.loads(latest.decode("utf-8"), object_pairs_hook=_no_duplicates)
    except (UnicodeError, ValueError, RecursionError):
        raise ReleaseMetadataError("metadata-invalid") from None
    required = {"schema", "version", "published_at", "min_astra", "release_url", "artifacts"}
    if not isinstance(data, dict) or set(data) != required:
        raise ReleaseMetadataError("metadata-invalid")
    if type(data["schema"]) is not int or data["schema"] != 2 or data["version"] != version:
        raise ReleaseMetadataError("metadata-invalid")
    published = data["published_at"]
    if (
        not isinstance(published, str)
        or re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z", published)
        is None
    ):
        raise ReleaseMetadataError("metadata-invalid")
    try:
        datetime.strptime(published, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        raise ReleaseMetadataError("metadata-invalid") from None
    # This build targets ALSE 1.8; do not advertise an incompatible future build.
    if data["min_astra"] != "1.8":
        raise ReleaseMetadataError("unsupported-platform")
    if data["release_url"] not in (
        f"{REPOSITORY_URL}/releases/tag/{version}",
        f"{REPOSITORY_URL}/releases/tag/v{version}",
    ):
        raise ReleaseMetadataError("metadata-invalid")
    artifacts = data["artifacts"]
    if not isinstance(artifacts, dict) or set(artifacts) not in ({"deb"}, {"deb", "appimage"}):
        raise ReleaseMetadataError("metadata-invalid")
    checksums = _signed_sums(sums)
    parsed: dict[str, VerifiedArtifact] = {}
    for kind, artifact in artifacts.items():
        if not isinstance(artifact, dict) or set(artifact) != {"name", "sha256", "size"}:
            raise ReleaseMetadataError("metadata-invalid")
        name = (
            f"astra-voice_{version}_amd64.deb"
            if kind == "deb"
            else f"Astra_Voice-{version}-x86_64.AppImage"
        )
        size = artifact["size"]
        digest = artifact["sha256"]
        if (
            artifact["name"] != name
            or type(size) is not int
            or not 0 < size <= MAX_ARTIFACT_BYTES
            or not isinstance(digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
            or checksums.get(name) != digest
        ):
            raise ReleaseMetadataError("metadata-invalid")
        parsed[kind] = VerifiedArtifact(
            "deb" if kind == "deb" else "appimage",
            name,
            digest,
            size,
            f"{REPOSITORY_URL}/releases/download/{raw_tag}/{name}",
        )
    if track not in parsed:
        raise ReleaseMetadataError("unsupported-track")
    return parsed[track]


class ReleaseMetadata:
    """Fetch/validate metadata with existing HTTP guard and release-key verifier.

    NetworkError retains existing transport/gate codes. Metadata, filesystem and
    cancellation failures raise ReleaseMetadataError. Successful results contain
    immutable bytes, not paths into temporary staging. No package is downloaded.
    """

    def __init__(
        self,
        client: HttpClient,
        verifier: Verifier,
        *,
        staging_dir: Path | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if verifier.purpose != "release":
            raise ValueError("Release metadata requires a release verifier")
        self._client = client
        self._verifier = verifier
        self._staging_dir = staging_dir
        self._clock = clock

    @contextmanager
    def _staging(self) -> Iterator[Path]:
        try:
            parent = self._staging_dir
            if parent is None:
                parent = cache_dir_path() / "updates"
            _no_symlinks(parent)
            if self._staging_dir is None:
                ensure_private_dir(parent.parent)
            ensure_private_dir(parent)
            with tempfile.TemporaryDirectory(prefix="metadata-", dir=parent) as temporary:
                yield Path(temporary)
        except (OSError, PathError):
            raise ReleaseMetadataError("metadata-io") from None

    def fetch(
        self,
        raw_tag: str,
        current_version: str,
        track: Track,
        *,
        kind: CheckKind = "check_app",
        cancel: threading.Event | None = None,
    ) -> VerifiedRelease:
        """Run while checking; only this trusted result may enable available."""
        if kind not in ("check_app", "check_app_manual"):
            raise ValueError("Metadata requires a check network kind")
        cancel = cancel if cancel is not None else threading.Event()
        _cancelled(cancel)
        version = _version(raw_tag, current_version, track)
        with self._staging() as directory:
            for name, limit in _METADATA:
                _cancelled(cancel)
                url = f"{REPOSITORY_URL}/releases/download/{raw_tag}/{name}"
                with self._client.get_stream(
                    url,
                    deadline_s=10.0,
                    connect_timeout_s=2.0,
                    cancel=cancel,
                    kind=kind,
                    _bounded_connect=True,
                    raise_for_status=False,
                ) as response:
                    if response.status in (403, 429):
                        now = self._clock()
                        window = parse_rate_limit(response.headers, now)
                        # Same rules as get_check: 403 without rate headers is
                        # ordinary backoff, while 429 always enforces an embargo.
                        limited = response.status == 429 or window is not None
                        delay = max(window or 0.0, backoff_seconds(1), RATE_LIMIT_MIN_S)
                        raise ReleaseMetadataError(
                            "rate-limited" if limited else "backoff", retry_at=now + delay
                        )
                    if response.status != 200:
                        raise NetworkError(
                            "http-status",
                            "Не удалось получить метаданные выпуска.",
                            status=response.status,
                        )
                    body = bytearray()
                    for block in response.iter_chunks(limit=limit + 1):
                        _cancelled(cancel)
                        if len(body) + len(block) > limit:
                            raise ReleaseMetadataError("metadata-too-large")
                        body.extend(block)
                    _cancelled(cancel)
                _write(directory / name, bytes(body))
            return self._validate(directory, version, raw_tag, track, cancel)

    def validate_local(
        self,
        directory: Path,
        raw_tag: str,
        current_version: str,
        track: Track,
        *,
        cancel: threading.Event | None = None,
    ) -> VerifiedRelease:
        """Reverify bounded local files using a private snapshot; no network."""
        cancel = cancel if cancel is not None else threading.Event()
        _cancelled(cancel)
        version = _version(raw_tag, current_version, track)
        with self._staging() as snapshot:
            for name, limit in _METADATA:
                _cancelled(cancel)
                _write(snapshot / name, _read(directory / name, limit))
            return self._validate(snapshot, version, raw_tag, track, cancel)

    def _validate(
        self, directory: Path, version: str, raw_tag: str, track: Track, cancel: threading.Event
    ) -> VerifiedRelease:
        _cancelled(cancel)
        signature = self._verifier.verify_detached(
            directory / "SHA256SUMS", directory / "SHA256SUMS.asc", cancel=cancel.is_set
        )
        _cancelled(cancel)
        if not signature:
            raise ReleaseMetadataError(signature.code or "signature-invalid")
        matched = self._verifier.verify_sha256sums(
            directory / "SHA256SUMS", directory / "latest.json"
        )
        _cancelled(cancel)
        if not matched:
            raise ReleaseMetadataError("hash-mismatch")
        sums = _read(directory / "SHA256SUMS", MAX_SUMS_BYTES)
        latest = _read(directory / "latest.json", MAX_LATEST_BYTES)
        artifact = _schema(latest, sums, version, raw_tag, track)
        # _schema validated these fields; never parse/return untrusted JSON early.
        data = json.loads(latest)
        _cancelled(cancel)
        return VerifiedRelease(
            version,
            raw_tag,
            data["published_at"],
            data["min_astra"],
            f"{REPOSITORY_URL}/releases/tag/{raw_tag}",
            artifact,
            sums,
            _read(directory / "SHA256SUMS.asc", MAX_SIGNATURE_BYTES),
            latest,
        )
