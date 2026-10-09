"""Explicit package download and a Qt-free, single-worker lifecycle.

Metadata is reverified before any package request. Only a complete verified
bundle is published; cancellation removes this operation's partial directory.
A ready bundle survives cancel/close. Same-UID hostile filesystem mutation is
outside this private-directory boundary; apply must reverify the saved result.
Signature verification supports cancellation; close still joins the owned worker.
"""

from __future__ import annotations

import errno
import hashlib
import json
import logging
import math
import os
import shutil
import stat
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from astra_voice.core.paths import PathError, cache_dir_path, ensure_private_dir
from astra_voice.net.http import (
    RATE_LIMIT_MAX_S,
    RATE_LIMIT_MIN_S,
    HttpClient,
    NetworkError,
    backoff_seconds,
    parse_rate_limit,
)
from astra_voice.net.update_cache import read_private, write_private
from astra_voice.updates.release import (
    MAX_LATEST_BYTES,
    MAX_SIGNATURE_BYTES,
    MAX_SUMS_BYTES,
    REPOSITORY_URL,
    ReleaseMetadata,
    ReleaseMetadataError,
    VerifiedRelease,
)

log = logging.getLogger(__name__)
SPACE_RESERVE_BYTES = 64 << 20
PROGRESS_INTERVAL_S = 0.1
_BACKOFF_NAME = "download-backoff.json"
_BACKOFF_MAX_BYTES = 4096
_BACKOFF_SOURCE = f"{REPOSITORY_URL}/releases/download/"
Phase = Literal["idle", "checking", "downloading", "verifying", "ready", "cancelled", "error"]
Progress = Callable[[Phase, int, int], None]


class DownloadError(Exception):
    """Stable error code, without URLs or paths from an underlying exception."""

    def __init__(self, code: str, *, retry_at: float | None = None) -> None:
        self.code = code
        self.retry_at = retry_at
        super().__init__(code)


@dataclass(frozen=True)
class FileIdentity:
    dev: int
    ino: int
    size: int
    mtime_ns: int
    ctime_ns: int
    mode: int
    uid: int

    @classmethod
    def from_stat(cls, info: os.stat_result) -> FileIdentity:
        return cls(
            info.st_dev,
            info.st_ino,
            info.st_size,
            info.st_mtime_ns,
            info.st_ctime_ns,
            info.st_mode,
            info.st_uid,
        )


@dataclass(frozen=True)
class VerifiedDownload:
    path: Path
    release: VerifiedRelease
    identity: FileIdentity


@dataclass(frozen=True)
class DownloadStatus:
    operation_id: int = 0
    phase: Phase = "idle"
    received: int = 0
    total: int = 0
    result: VerifiedDownload | None = None
    error: str = ""
    retry_at: float | None = None


def _no_symlinks(path: Path) -> None:
    if not path.is_absolute() or ".." in path.parts:
        raise DownloadError("path-unsafe")
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise DownloadError("path-unsafe")


def _write_metadata(directory: Path, release: VerifiedRelease) -> None:
    for name, body, limit in (
        ("SHA256SUMS", release.sums, MAX_SUMS_BYTES),
        ("SHA256SUMS.asc", release.signature, MAX_SIGNATURE_BYTES),
        ("latest.json", release.latest, MAX_LATEST_BYTES),
    ):
        if not isinstance(body, bytes):
            raise DownloadError("metadata-invalid")
        if len(body) > limit:
            raise DownloadError("metadata-too-large")
        fd = os.open(directory / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(body)


def _unique_state(pairs: list[tuple[str, object]]) -> dict[str, object]:
    state: dict[str, object] = {}
    for key, value in pairs:
        if key in state:
            raise ValueError("duplicate state key")
        state[key] = value
    return state


def _read_backoff(path: Path) -> tuple[float | None, int, bool]:
    """Return deadline/failures and whether this own state may be replaced.

    Unlike UpdateCache.set, never overwrite malformed or foreign state. Such a
    file cannot impose an indefinite network ban; ignore it without mutation.
    """
    try:
        info = path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1
        ):
            return None, 0, False
        data = json.loads(read_private(path, _BACKOFF_MAX_BYTES), object_pairs_hook=_unique_state)
        if not isinstance(data, dict) or set(data) != {
            "schema",
            "source",
            "stored_at",
            "retry_at",
            "failures",
        }:
            return None, 0, False
        if (
            type(data["schema"]) is not int
            or data["schema"] != 1
            or data["source"] != _BACKOFF_SOURCE
        ):
            return None, 0, False
        start, end, failures = data["stored_at"], data["retry_at"], data["failures"]
        if (
            any(
                type(number) not in (int, float) or not math.isfinite(number)
                for number in (start, end)
            )
            or type(failures) is not int
            or not 1 <= failures <= 32
            or start < 0
            or not 0 <= end - start <= RATE_LIMIT_MAX_S
        ):
            return None, 0, False
        now = time.time()
        if start > now:
            return None, 0, False  # clock rollback/future state must not extend a ban
        if now - start > RATE_LIMIT_MAX_S:
            return None, 0, True
        return (float(end) if end > now else None), failures, True
    except FileNotFoundError:
        return None, 0, True
    except (OSError, ValueError, TypeError, OverflowError, RecursionError):
        return None, 0, False


def _save_backoff(path: Path, retry_at: float, failures: int) -> None:
    if not _read_backoff(path)[2]:
        log.warning(
            "Состояние ограничения загрузки небезопасно или повреждено; сохранение пропущено"
        )
        return
    try:
        write_private(
            path,
            json.dumps(
                {
                    "schema": 1,
                    "source": _BACKOFF_SOURCE,
                    "stored_at": time.time(),
                    "retry_at": retry_at,
                    "failures": min(failures, 32),
                },
                separators=(",", ":"),
            ).encode("utf-8"),
        )
    except OSError:
        log.warning("Не удалось сохранить срок повторной загрузки")


def _clear_backoff(path: Path) -> None:
    if _read_backoff(path)[2]:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            log.warning("Не удалось очистить истёкшее ограничение загрузки")


class ReleaseDownloader:
    """Synchronous primitive; refusal returns a stable code, or '' if allowed.

    The caller supplies its current policy/capability check. It is repeated at
    each stage, every read/write chunk and immediately before publishing ready.
    The network gate and redirect guard remain the existing HttpClient's job.
    """

    def __init__(
        self,
        client: HttpClient,
        metadata: ReleaseMetadata,
        current_version: str,
        *,
        directory: Path | None = None,
        refusal: Callable[[], str] = lambda: "",
    ) -> None:
        self._client = client
        self._metadata = metadata
        self._current_version = current_version
        self._directory = directory
        self._refusal = refusal
        self._retry_at: float | None = None
        self._rate_failures = 0

    def _guard(self, cancel: threading.Event, *, local: bool = False) -> None:
        if cancel.is_set():
            raise DownloadError("cancelled")
        if self._retry_at is not None:
            remaining = self._retry_at - time.time()
            if 0 < remaining <= RATE_LIMIT_MAX_S:
                raise DownloadError("rate-limited", retry_at=self._retry_at)
            self._retry_at = None
        reason = self._refusal()
        # Only user offline is irrelevant once HTTP is closed and bytes are complete.
        # Explicit cancellation and every administrative/policy refusal still apply.
        if reason and not (local and reason == "offline"):
            raise DownloadError(reason)

    def download(
        self,
        release: VerifiedRelease,
        *,
        cancel: threading.Event | None = None,
        progress: Progress | None = None,
    ) -> VerifiedDownload:
        cancel = cancel if cancel is not None else threading.Event()
        transaction: Path | None = None
        committed = False
        last_progress = 0.0

        def report(phase: Phase, received: int, total: int, *, force: bool = True) -> None:
            nonlocal last_progress
            now = time.monotonic()
            if progress is not None and (force or now - last_progress >= PROGRESS_INTERVAL_S):
                last_progress = now
                progress(phase, received, total)

        try:
            self._guard(cancel)
            report("checking", 0, 0)
            parent = (
                self._directory if self._directory is not None else cache_dir_path() / "updates"
            )
            _no_symlinks(parent)
            if self._directory is None:
                ensure_private_dir(parent.parent)
            ensure_private_dir(parent)
            self._retry_at, self._rate_failures, _ = _read_backoff(parent / _BACKOFF_NAME)
            self._guard(cancel)
            transaction = Path(tempfile.mkdtemp(prefix="download-", dir=parent))
            stage = transaction / ".partial"
            stage.mkdir(mode=0o700)
            _write_metadata(stage, release)
            self._guard(cancel)
            trusted = self._metadata.validate_local(
                stage, release.raw_tag, self._current_version, release.artifact.track, cancel=cancel
            )
            self._guard(cancel)
            if trusted != release:
                raise DownloadError("release-mismatch")
            artifact = trusted.artifact
            space = os.statvfs(stage)
            if space.f_bavail * space.f_frsize < artifact.size + SPACE_RESERVE_BYTES:
                raise DownloadError("no-space")
            self._guard(cancel)
            part = stage / (artifact.name + ".part")
            received = 0
            report("downloading", received, artifact.size)
            descriptor = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(descriptor, "wb") as output:
                self._guard(cancel)
                with self._client.get_stream(
                    artifact.url,
                    deadline_s=3600.0,
                    connect_timeout_s=10.0,
                    cancel=cancel,
                    kind="download",
                    _bounded_connect=True,
                    raise_for_status=False,
                ) as response:
                    if response.status in (403, 429):
                        window = parse_rate_limit(response.headers, time.time())
                        if window is not None or response.status == 429:
                            self._rate_failures += 1
                            self._retry_at = time.time() + max(
                                window or 0.0,
                                RATE_LIMIT_MIN_S,
                                backoff_seconds(self._rate_failures),
                            )
                            _save_backoff(
                                parent / _BACKOFF_NAME, self._retry_at, self._rate_failures
                            )
                            raise DownloadError("rate-limited", retry_at=self._retry_at)
                    if response.status != 200:
                        raise DownloadError("http-status")
                    for block in response.iter_chunks(limit=artifact.size + 1):
                        self._guard(cancel)
                        if received + len(block) > artifact.size:
                            raise DownloadError("size-mismatch")
                        output.write(block)
                        received += len(block)
                        report("downloading", received, artifact.size, force=False)
                    self._guard(cancel)
                if received != artifact.size:
                    raise DownloadError("size-mismatch")
                output.flush()
                os.fsync(output.fileno())
            self._guard(cancel, local=True)
            report("verifying", received, artifact.size)
            descriptor = os.open(part, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(descriptor, "rb") as downloaded:
                info = os.fstat(downloaded.fileno())
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_uid != os.getuid()
                    or info.st_size != artifact.size
                ):
                    raise DownloadError("size-mismatch")
                digest = hashlib.sha256()
                remaining = artifact.size
                while remaining:
                    self._guard(cancel, local=True)
                    block = downloaded.read(min(1 << 20, remaining))
                    if not block:
                        raise DownloadError("size-mismatch")
                    digest.update(block)
                    remaining -= len(block)
                if downloaded.read(1):
                    raise DownloadError("size-mismatch")
                if digest.hexdigest() != artifact.sha256:
                    raise DownloadError("hash-mismatch")
                self._guard(cancel, local=True)
                if artifact.track == "appimage":
                    os.fchmod(downloaded.fileno(), 0o755)
                verified_identity = FileIdentity.from_stat(os.fstat(downloaded.fileno()))
            part.rename(stage / artifact.name)
            self._guard(cancel, local=True)
            complete = transaction / "complete"
            # Both names are inside our fresh private transaction, not a shared
            # version path; existing/foreign bundles are never overwritten.
            stage.rename(complete)
            identity = FileIdentity.from_stat((complete / artifact.name).lstat())
            if (
                identity.dev,
                identity.ino,
                identity.size,
                identity.mtime_ns,
                identity.mode,
                identity.uid,
            ) != (
                verified_identity.dev,
                verified_identity.ino,
                verified_identity.size,
                verified_identity.mtime_ns,
                verified_identity.mode,
                verified_identity.uid,
            ):
                raise DownloadError("file-changed")
            committed = True
            self._rate_failures = 0
            _clear_backoff(parent / _BACKOFF_NAME)
            return VerifiedDownload(complete / artifact.name, trusted, identity)
        except (ReleaseMetadataError, NetworkError) as error:
            raise DownloadError(error.code, retry_at=getattr(error, "retry_at", None)) from None
        except PathError:
            raise DownloadError("path-unsafe") from None
        except OSError as error:
            raise DownloadError(
                "no-space" if error.errno == errno.ENOSPC else "download-io"
            ) from None
        finally:
            if transaction is not None and not committed:
                shutil.rmtree(transaction)


class DownloadController:
    """One worker at a time; callbacks run on that worker, never on a Qt object.

    start rejects busy/closed; cancel is nonblocking. close stops publications,
    cancels and joins. False means the worker is still owned and must be joined
    later. An already running callback may finish
    after a timed-out close; a successful close is the publication barrier.
    """

    def __init__(
        self,
        downloader: ReleaseDownloader,
        *,
        on_status: Callable[[DownloadStatus], None] | None = None,
    ) -> None:
        self._downloader = downloader
        self.on_status = on_status
        self._lock = threading.RLock()
        self._closed = False
        self._cancel = threading.Event()
        self._worker: threading.Thread | None = None
        self._status = DownloadStatus()
        self._operation_id = 0

    @property
    def status(self) -> DownloadStatus:
        with self._lock:
            return self._status

    @property
    def worker_threads(self) -> tuple[threading.Thread, ...]:
        with self._lock:
            return (self._worker,) if self._worker is not None else ()

    def start(self, release: VerifiedRelease) -> int:
        with self._lock:
            if self._closed:
                raise DownloadError("closed")
            if self._worker is not None and self._worker.is_alive():
                raise DownloadError("busy")
            self._operation_id += 1
            operation_id = self._operation_id
            cancel = threading.Event()
            self._cancel = cancel
            self._status = DownloadStatus(operation_id, "checking")
            worker = threading.Thread(
                target=self._run, args=(operation_id, release, cancel), name="release-download"
            )
            self._worker = worker
            try:
                worker.start()
            except RuntimeError:
                self._worker = None
                self._status = DownloadStatus(operation_id, "error", error="thread-start")
                raise DownloadError("thread-start") from None
            return operation_id

    def cancel(self) -> None:
        with self._lock:
            self._cancel.set()

    def close(self, timeout: float | None = None) -> bool:
        with self._lock:
            self._closed = True
            self._cancel.set()
            worker = self._worker
        if worker is threading.current_thread():
            return False
        if worker is not None:
            worker.join(timeout)
            if worker.is_alive():
                return False
        with self._lock:
            self._worker = None
        return True

    def _publish(self, status: DownloadStatus) -> None:
        with self._lock:
            if status.operation_id != self._operation_id:
                return
            self._status = status
            callback = None if self._closed else self.on_status
        if callback is not None:
            try:
                callback(status)
            except Exception:  # noqa: BLE001 — subscriber failure must not orphan the worker
                log.warning("Не удалось передать состояние загрузки обновления", exc_info=True)

    def _run(self, operation_id: int, release: VerifiedRelease, cancel: threading.Event) -> None:
        def progress(phase: Phase, received: int, total: int) -> None:
            self._publish(DownloadStatus(operation_id, phase, received, total))

        try:
            result = self._downloader.download(release, cancel=cancel, progress=progress)
        except DownloadError as error:
            previous = self.status
            self._publish(
                DownloadStatus(
                    operation_id,
                    "cancelled" if error.code == "cancelled" else "error",
                    previous.received,
                    previous.total,
                    error=error.code,
                    retry_at=error.retry_at,
                )
            )
        except Exception:  # noqa: BLE001 — terminate a failed worker with a stable state
            log.warning("Не удалось загрузить обновление", exc_info=True)
            self._publish(DownloadStatus(operation_id, "error", error="download-failed"))
        else:
            size = result.release.artifact.size
            self._publish(DownloadStatus(operation_id, "ready", size, size, result=result))
