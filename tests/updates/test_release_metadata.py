"""Metadata trust, bounds and cancellation without sockets or real signing keys."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import threading
from collections.abc import Callable, Iterator
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any

import pytest

from astra_voice.core.policy import Policy
from astra_voice.core.settings import Settings
from astra_voice.net.gate import NetworkGate
from astra_voice.net.http import HttpClient, NetworkError
from astra_voice.security.verify import Verifier, VerifyResult
from astra_voice.updates.release import (
    MAX_ARTIFACT_BYTES,
    MAX_LATEST_BYTES,
    MAX_SIGNATURE_BYTES,
    MAX_SUMS_BYTES,
    REPOSITORY_URL,
    ReleaseMetadata,
    ReleaseMetadataError,
)

pytestmark = pytest.mark.unit


class SignatureVerifier(Verifier):
    """Only the GPG boundary is fake; checksum verification is the real helper."""

    def __init__(self) -> None:
        super().__init__("release", Path("/unused-test-keyring"))
        self.calls: list[str] = []
        self.result = VerifyResult(True, purpose="release")
        self.cancel: threading.Event | None = None
        self.cancel_callback: Callable[[], bool] | None = None

    def verify_detached(
        self, data: Path, sig: Path, *, cancel: Callable[[], bool] | None = None
    ) -> VerifyResult:
        self.cancel_callback = cancel
        self.calls.append("signature")
        assert data.name == "SHA256SUMS" and sig.name == "SHA256SUMS.asc"
        assert stat.S_IMODE(data.parent.stat().st_mode) == 0o700
        assert data.parent.stat().st_uid == os.getuid()
        for item in data.parent.iterdir():
            assert stat.S_IMODE(item.stat().st_mode) == 0o600
        if self.cancel is not None:
            self.cancel.set()
        return self.result

    def verify_sha256sums(self, sums: Path, file: Path) -> VerifyResult:
        self.calls.append("hash")
        return super().verify_sha256sums(sums, file)


class Response:
    def __init__(self, body: bytes, cancel: threading.Event | None = None) -> None:
        self.status = 200
        self.headers: dict[str, str] = {}
        self.body = body
        self.closed = False
        self.limit = 0
        self.cancel = cancel

    def __enter__(self) -> Response:
        return self

    def __exit__(self, *args: object) -> None:
        self.closed = True

    def iter_chunks(self, *, limit: int) -> Iterator[bytes]:
        self.limit = limit
        if self.cancel is not None:
            self.cancel.set()
        yield self.body[:limit]


class Client(HttpClient):
    def __init__(self, files: dict[str, bytes]) -> None:
        super().__init__(NetworkGate(Settings(check_app_updates=True), Policy()), user_agent="test")
        self.files = files
        self.calls: list[tuple[str, object]] = []
        self.responses: list[Response] = []
        self.cancel: threading.Event | None = None
        self.statuses: dict[str, int] = {}
        self.headers: dict[str, str] = {}
        self.options: list[dict[str, Any]] = []

    def get_stream(self, url: str, **kwargs: Any) -> Any:
        self.calls.append((url, kwargs["kind"]))
        self.options.append(kwargs)
        response = Response(self.files[url.rsplit("/", 1)[-1]], self.cancel)
        response.status = self.statuses.get(url.rsplit("/", 1)[-1], 200)
        response.headers = self.headers
        self.responses.append(response)
        return response


def metadata() -> dict[str, Any]:
    return {
        "schema": 2,
        "version": "0.2.0",
        "published_at": "2026-10-09T09:00:00Z",
        "min_astra": "1.8",
        "release_url": f"{REPOSITORY_URL}/releases/tag/v0.2.0",
        "artifacts": {
            "deb": {"name": "astra-voice_0.2.0_amd64.deb", "sha256": "a" * 64, "size": 123},
            "appimage": {
                "name": "Astra_Voice-0.2.0-x86_64.AppImage",
                "sha256": "b" * 64,
                "size": 456,
            },
        },
    }


def files(data: dict[str, Any] | None = None, *, raw: bytes | None = None) -> dict[str, bytes]:
    data = metadata() if data is None else data
    body = json.dumps(data).encode() if raw is None else raw
    sums = f"{hashlib.sha256(body).hexdigest()}  latest.json\n"
    sums += f"{'a' * 64}  astra-voice_0.2.0_amd64.deb\n"
    sums += f"{'b' * 64}  Astra_Voice-0.2.0-x86_64.AppImage\n"
    return {"latest.json": body, "SHA256SUMS": sums.encode(), "SHA256SUMS.asc": b"fake signature"}


def service(
    tmp_path: Path, body: dict[str, bytes] | None = None
) -> tuple[ReleaseMetadata, Client, SignatureVerifier]:
    client = Client(files() if body is None else body)
    verifier = SignatureVerifier()
    return ReleaseMetadata(client, verifier, staging_dir=tmp_path / "staging"), client, verifier


@pytest.mark.parametrize("tag", ["0.2.0", "v0.2.0"])
@pytest.mark.parametrize("track", ["deb", "appimage"])
@pytest.mark.parametrize("kind", ["check_app", "check_app_manual"])
def test_fetch_trusted_fixed_urls_and_cleanup(
    tmp_path: Path, tag: str, track: Any, kind: Any
) -> None:
    api, client, verifier = service(tmp_path)
    release = api.fetch(tag, "0.1.1~dev9", track, kind=kind)
    assert verifier.calls == ["signature", "hash"]
    assert release.raw_tag == tag
    assert release.artifact.track == track
    assert release.artifact.url.endswith(f"/{tag}/{release.artifact.name}")
    assert client.calls == [
        (f"{REPOSITORY_URL}/releases/download/{tag}/{name}", kind)
        for name in ("SHA256SUMS", "SHA256SUMS.asc", "latest.json")
    ]
    assert [r.limit for r in client.responses] == [
        MAX_SUMS_BYTES + 1,
        MAX_SIGNATURE_BYTES + 1,
        MAX_LATEST_BYTES + 1,
    ]
    assert all(r.closed for r in client.responses)
    assert list((tmp_path / "staging").iterdir()) == []
    with pytest.raises(FrozenInstanceError):
        release.version = "evil"  # type: ignore[misc]


@pytest.mark.parametrize(
    "tag", ["vv0.2.0", "../0.2.0", "v0.2.0/extra", "0.2.0+build", "0.2.0-rc1", "00.2.0"]
)
def test_bad_tags_never_reach_network(tmp_path: Path, tag: str) -> None:
    api, client, _ = service(tmp_path)
    with pytest.raises(ReleaseMetadataError, match="invalid-tag"):
        api.fetch(tag, "0.1.0", "deb")
    assert client.calls == []


@pytest.mark.parametrize("current", ["0.2.0", "0.3.0", "0.2.0+build"])
def test_no_downgrade(tmp_path: Path, current: str) -> None:
    api, client, _ = service(tmp_path)
    with pytest.raises(ReleaseMetadataError, match="version-not-newer"):
        api.fetch("v0.2.0", current, "deb")
    assert client.calls == []


@pytest.mark.parametrize(
    "name,limit",
    [
        ("SHA256SUMS", MAX_SUMS_BYTES),
        ("SHA256SUMS.asc", MAX_SIGNATURE_BYTES),
        ("latest.json", MAX_LATEST_BYTES),
    ],
)
def test_network_bounds_close_and_clean(tmp_path: Path, name: str, limit: int) -> None:
    body = files()
    body[name] = b"x" * (limit + 1)
    api, client, verifier = service(tmp_path, body)
    with pytest.raises(ReleaseMetadataError, match="metadata-too-large"):
        api.fetch("0.2.0", "0.1.0", "deb")
    assert not verifier.calls
    assert all(r.closed for r in client.responses)
    assert list((tmp_path / "staging").iterdir()) == []


def test_signature_before_hash_and_json(tmp_path: Path) -> None:
    api, _, verifier = service(tmp_path, files(raw=b"broken json"))
    verifier.result = VerifyResult(False, code="clock-behind")
    with pytest.raises(ReleaseMetadataError, match="clock-behind"):
        api.fetch("0.2.0", "0.1.0", "deb")
    assert verifier.calls == ["signature"]


def test_latest_must_match_signed_checksum_before_json(tmp_path: Path) -> None:
    body = files()
    body["latest.json"] = b"not json"
    api, _, verifier = service(tmp_path, body)
    with pytest.raises(ReleaseMetadataError, match="hash-mismatch"):
        api.fetch("0.2.0", "0.1.0", "deb")
    assert verifier.calls == ["signature", "hash"]


@pytest.mark.parametrize(
    "raw",
    [
        b'{"schema":2,"schema":2}',
        b'{"artifacts":{"deb":{},"deb":{}}}',
        b"[]",
        b"null",
        b"NaN",
        b"{}",
        b"\xff",
        b"[" * 2000,
    ],
)
def test_bad_json(tmp_path: Path, raw: bytes) -> None:
    api, _, _ = service(tmp_path, files(raw=raw))
    with pytest.raises(ReleaseMetadataError, match="metadata-invalid"):
        api.fetch("0.2.0", "0.1.0", "deb")


@pytest.mark.parametrize("size", [True, False, 0, -1, 1.5, "123", None, MAX_ARTIFACT_BYTES + 1])
def test_invalid_artifact_size(tmp_path: Path, size: object) -> None:
    data = metadata()
    data["artifacts"]["deb"]["size"] = size
    api, _, _ = service(tmp_path, files(data))
    with pytest.raises(ReleaseMetadataError, match="metadata-invalid"):
        api.fetch("0.2.0", "0.1.0", "deb")


@pytest.mark.parametrize(
    "field,value",
    [
        ("name", "../astra-voice_0.2.0_amd64.deb"),
        ("name", "wrong.deb"),
        ("sha256", "c" * 64),
        ("sha256", "A" * 64),
    ],
)
def test_artifact_names_hashes_must_match(tmp_path: Path, field: str, value: str) -> None:
    data = metadata()
    data["artifacts"]["deb"][field] = value
    api, _, _ = service(tmp_path, files(data))
    with pytest.raises(ReleaseMetadataError, match="metadata-invalid"):
        api.fetch("0.2.0", "0.1.0", "deb")


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema", True),
        ("schema", 1),
        ("version", "0.3.0"),
        ("release_url", "https://evil.invalid/"),
        ("published_at", "2026-02-30T00:00:00Z"),
        ("extra", 1),
        ("artifacts", []),
    ],
)
def test_strict_schema(tmp_path: Path, field: str, value: object) -> None:
    data = metadata()
    data[field] = value
    api, _, _ = service(tmp_path, files(data))
    with pytest.raises(ReleaseMetadataError, match="metadata-invalid"):
        api.fetch("0.2.0", "0.1.0", "deb")


@pytest.mark.parametrize(
    "suffix",
    [
        f"{'a' * 64}  astra-voice_0.2.0_amd64.deb\n",
        f"{'a' * 64}  ../bad\n",
        f"{'a' * 64}  bad\\name\n",
        f"{'a' * 64}  /absolute\n",
    ],
)
def test_signed_sums_reject_ambiguous_names(tmp_path: Path, suffix: str) -> None:
    body = files()
    body["SHA256SUMS"] += suffix.encode()
    api, _, _ = service(tmp_path, body)
    with pytest.raises(ReleaseMetadataError):
        api.fetch("0.2.0", "0.1.0", "deb")


@pytest.mark.parametrize("when", ["before", "network", "verifier"])
def test_cancel_never_returns_release(tmp_path: Path, when: str) -> None:
    api, client, verifier = service(tmp_path)
    cancel = threading.Event()
    if when == "before":
        cancel.set()
    elif when == "network":
        client.cancel = cancel
    else:
        verifier.cancel = cancel
    with pytest.raises(ReleaseMetadataError, match="cancelled"):
        api.fetch("0.2.0", "0.1.0", "deb", cancel=cancel)
    if when == "before":
        assert client.calls == []
    else:
        assert list((tmp_path / "staging").iterdir()) == []
    if when == "verifier":
        assert verifier.calls == ["signature"]


def test_local_always_reverifies_without_network_and_preserves_files(tmp_path: Path) -> None:
    api, client, verifier = service(tmp_path)
    local = tmp_path / "saved"
    local.mkdir()
    for name, body in files().items():
        (local / name).write_bytes(body)
    sentinel = local / "foreign"
    sentinel.write_text("keep")
    assert api.validate_local(local, "0.2.0", "0.1.0", "deb").version == "0.2.0"
    verifier.result = VerifyResult(False)
    with pytest.raises(ReleaseMetadataError, match="signature-invalid"):
        api.validate_local(local, "0.2.0", "0.1.0", "deb")
    assert verifier.calls == ["signature", "hash", "signature"]
    assert client.calls == []
    assert sentinel.read_text() == "keep"
    assert all((local / name).read_bytes() == body for name, body in files().items())


def test_symlink_staging_parent_refused(tmp_path: Path) -> None:
    api, client, _ = service(tmp_path)
    target = tmp_path / "target"
    target.mkdir(mode=0o755)
    (tmp_path / "staging").symlink_to(target, target_is_directory=True)
    with pytest.raises(ReleaseMetadataError, match="path-unsafe"):
        api.fetch("0.2.0", "0.1.0", "deb")
    assert stat.S_IMODE(target.stat().st_mode) == 0o755
    assert client.calls == []


@pytest.mark.parametrize("special", ["symlink", "fifo", "large"])
def test_local_unsafe_file_rejected(tmp_path: Path, special: str) -> None:
    api, client, _ = service(tmp_path)
    local = tmp_path / "local"
    local.mkdir()
    for name, body in files().items():
        (local / name).write_bytes(body)
    path = local / "SHA256SUMS"
    path.unlink()
    if special == "symlink":
        path.symlink_to(local / "latest.json")
    elif special == "fifo":
        os.mkfifo(path)
    else:
        path.write_bytes(b"x" * (MAX_SUMS_BYTES + 1))
    with pytest.raises(ReleaseMetadataError):
        api.validate_local(local, "0.2.0", "0.1.0", "deb")
    assert client.calls == []
    assert list((tmp_path / "staging").iterdir()) == []


def test_real_network_gate_blocks_metadata_without_transport(tmp_path: Path) -> None:
    gate = NetworkGate(Settings(offline=True), Policy())
    api = ReleaseMetadata(
        HttpClient(gate, user_agent="test"), SignatureVerifier(), staging_dir=tmp_path
    )
    with pytest.raises(NetworkError, match="Включена работа без сети"):
        api.fetch("v0.2.0", "0.1.0", "deb", kind="check_app_manual")
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("nested", [False, True])
def test_default_cache_symlink_has_no_side_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, existing: bool, nested: bool
) -> None:
    api, client, verifier = service(tmp_path)
    target = tmp_path / "unrelated"
    target.mkdir(mode=0o755)
    cache = target / "nested" if nested else target
    cache.mkdir(exist_ok=True)
    app_cache = cache / "astra-voice"
    if existing:
        app_cache.mkdir(mode=0o755)
        (app_cache / "sentinel").write_text("keep")
    link = tmp_path / "cache-link"
    link.symlink_to(target, target_is_directory=True)
    monkeypatch.setenv("XDG_CACHE_HOME", str(link / "nested" if nested else link))
    api = ReleaseMetadata(client, verifier)
    with pytest.raises(ReleaseMetadataError, match="path-unsafe"):
        api.fetch("0.2.0", "0.1.0", "deb")
    assert client.calls == []
    assert stat.S_IMODE(target.stat().st_mode) == 0o755
    assert app_cache.exists() is existing
    if existing:
        assert stat.S_IMODE(app_cache.stat().st_mode) == 0o755
        assert {p.name for p in app_cache.iterdir()} == {"sentinel"}
        assert (app_cache / "sentinel").read_text() == "keep"


@pytest.mark.parametrize(
    "status,headers,code,window",
    [
        (429, {"Retry-After": "900"}, "rate-limited", 900),
        (429, {}, "rate-limited", 300),
        (429, {"Retry-After": "-4"}, "rate-limited", 300),
        (429, {"Retry-After": "9" * 600}, "rate-limited", 86400),
        (403, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1900"}, "rate-limited", 900),
        (403, {"RateLimit": '"api";r=0;t=55'}, "rate-limited", 300),
        (403, {"Retry-After": "0"}, "rate-limited", 300),
        (403, {}, "backoff", 300),
        (403, {"Retry-After": "negative"}, "backoff", 300),
    ],
)
def test_metadata_rate_limit_retry_contract(
    tmp_path: Path,
    status: int,
    headers: dict[str, str],
    code: str,
    window: int,
) -> None:
    api, client, verifier = service(tmp_path)
    api._clock = lambda: 1000.0
    client.statuses["SHA256SUMS"] = status
    client.headers = headers
    with pytest.raises(ReleaseMetadataError) as error:
        api.fetch("v0.2.0", "0.1.0", "deb", kind="check_app_manual")
    assert error.value.code == code and error.value.retry_at == 1000 + window
    assert client.options[0]["raise_for_status"] is False
    assert len(client.calls) == 1 and verifier.calls == []
    assert all(response.closed for response in client.responses)
    assert list((tmp_path / "staging").iterdir()) == []


def test_cancellation_event_is_forwarded_as_live_verifier_predicate(tmp_path: Path) -> None:
    api, client, verifier = service(tmp_path)
    cancel = threading.Event()
    api.fetch("v0.2.0", "0.1.0", "deb", cancel=cancel)
    callback = verifier.cancel_callback
    assert callback is not None and not callback()
    cancel.set()
    assert callback()
