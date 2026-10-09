"""Small isolated download transactions and worker lifecycle, no real HTTP/GPG/Qt."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
import threading
from collections.abc import Callable, Iterator
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from typing import Any

import pytest

from astra_voice.core.policy import Policy
from astra_voice.core.settings import Settings
from astra_voice.net.gate import NetworkGate
from astra_voice.net.http import HttpClient, NetworkError
from astra_voice.security.verify import Verifier, VerifyResult
from astra_voice.updates.download import (
    SPACE_RESERVE_BYTES,
    DownloadController,
    DownloadError,
    DownloadStatus,
    FileIdentity,
    Phase,
    Progress,
    ReleaseDownloader,
    VerifiedDownload,
)
from astra_voice.updates.release import MAX_SUMS_BYTES, ReleaseMetadata, VerifiedRelease

pytestmark = pytest.mark.unit


class SignatureVerifier(Verifier):
    def __init__(self) -> None:
        super().__init__("release", Path("/unused-keyring"))
        self.allowed = True
        self.calls = 0

    def verify_detached(
        self, data: Path, sig: Path, *, cancel: Callable[[], bool] | None = None
    ) -> VerifyResult:
        self.calls += 1
        return VerifyResult(self.allowed)


class Response:
    def __init__(self, body: bytes) -> None:
        self.body = body
        self.status = 200
        self.headers: dict[str, str] = {}
        self.closed = False
        self.limit = 0
        self.chunk_size = 16
        self.before_chunk: Callable[[], None] = lambda: None
        self.error: NetworkError | None = None

    def __enter__(self) -> Response:
        return self

    def __exit__(self, *args: object) -> None:
        self.closed = True

    def iter_chunks(self, *, limit: int) -> Iterator[bytes]:
        self.limit = limit
        if self.error:
            raise self.error
        for start in range(0, min(len(self.body), limit), self.chunk_size):
            self.before_chunk()
            yield self.body[start : min(start + self.chunk_size, limit)]


class Client(HttpClient):
    def __init__(self, response: Response) -> None:
        super().__init__(NetworkGate(Settings(), Policy()), user_agent="test")
        self.response = response
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get_stream(self, url: str, **kwargs: Any) -> Any:
        self.calls.append((url, kwargs))
        return self.response


class Rig:
    def __init__(self, tmp_path: Path, track: str = "deb") -> None:
        self.body = b"harmless package content" * 4
        self.response = Response(self.body)
        self.client = Client(self.response)
        self.verifier = SignatureVerifier()
        self.metadata = ReleaseMetadata(
            self.client, self.verifier, staging_dir=tmp_path / "metadata"
        )
        local = tmp_path / "input"
        local.mkdir()
        digest = hashlib.sha256(self.body).hexdigest()
        artifacts = {
            "deb": {"name": "astra-voice_0.2.0_amd64.deb", "sha256": digest, "size": len(self.body)}
        }
        if track == "appimage":
            artifacts["appimage"] = {
                "name": "Astra_Voice-0.2.0-x86_64.AppImage",
                "sha256": digest,
                "size": len(self.body),
            }
        latest = json.dumps(
            {
                "schema": 2,
                "version": "0.2.0",
                "published_at": "2026-10-09T09:00:00Z",
                "min_astra": "1.8",
                "release_url": "https://github.com/ArrivaRUS/astra-voice/releases/tag/v0.2.0",
                "artifacts": artifacts,
            }
        ).encode()
        sums = f"{hashlib.sha256(latest).hexdigest()}  latest.json\n"
        for item in artifacts.values():
            sums += f"{digest}  {item['name']}\n"
        (local / "SHA256SUMS").write_text(sums)
        (local / "SHA256SUMS.asc").write_bytes(b"fake signature")
        (local / "latest.json").write_bytes(latest)
        self.release = self.metadata.validate_local(local, "v0.2.0", "0.1.1~dev9", track)  # type: ignore[arg-type]
        self.directory = tmp_path / "downloads"
        self.refusal = ""
        self.downloader = ReleaseDownloader(
            self.client,
            self.metadata,
            "0.1.1~dev9",
            directory=self.directory,
            refusal=lambda: self.refusal,
        )

    def no_partial(self) -> None:
        assert not self.directory.exists() or all(
            path.name == "download-backoff.json" for path in self.directory.iterdir()
        )


@pytest.mark.parametrize("track,mode", [("deb", 0o600), ("appimage", 0o755)])
def test_complete_bundle_is_verified_atomic_and_private(
    tmp_path: Path, track: str, mode: int
) -> None:
    rig = Rig(tmp_path, track)
    states: list[tuple[Phase, int, int]] = []

    def progress(phase: Phase, received: int, total: int) -> None:
        states.append((phase, received, total))
        assert not list(rig.directory.glob("*/complete"))

    result = rig.downloader.download(rig.release, progress=progress)
    assert result.path.read_bytes() == rig.body
    assert result.identity == FileIdentity.from_stat(result.path.stat())
    assert stat.S_IMODE(result.path.stat().st_mode) == mode
    assert stat.S_IMODE(result.path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(result.path.parent.parent.stat().st_mode) == 0o700
    assert result.path.parent.name == "complete"
    assert set(p.name for p in result.path.parent.iterdir()) == {
        rig.release.artifact.name,
        "SHA256SUMS",
        "SHA256SUMS.asc",
        "latest.json",
    }
    for name, body in (
        ("SHA256SUMS", rig.release.sums),
        ("SHA256SUMS.asc", rig.release.signature),
        ("latest.json", rig.release.latest),
    ):
        assert (result.path.parent / name).read_bytes() == body
        assert stat.S_IMODE((result.path.parent / name).stat().st_mode) == 0o600
    assert rig.verifier.calls == 2
    assert rig.client.calls[0][0] == rig.release.artifact.url
    assert rig.client.calls[0][1]["kind"] == "download"
    assert rig.client.calls[0][1]["raise_for_status"] is False
    assert rig.response.limit == len(rig.body) + 1
    assert rig.response.closed
    assert states[0] == ("checking", 0, 0)
    assert states[-1] == ("verifying", len(rig.body), len(rig.body))
    with pytest.raises(FrozenInstanceError):
        result.path = tmp_path  # type: ignore[misc]


def test_metadata_reverified_before_package_http(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.verifier.allowed = False
    with pytest.raises(DownloadError, match="signature-invalid"):
        rig.downloader.download(rig.release)
    assert rig.client.calls == []
    rig.no_partial()


@pytest.mark.parametrize(
    "field,value",
    [
        ("url", "https://evil.invalid/file"),
        ("size", 1),
        ("name", "../escape"),
        ("sha256", "a" * 64),
    ],
)
def test_constructed_verified_dataclass_cannot_override_metadata(
    tmp_path: Path, field: str, value: object
) -> None:
    rig = Rig(tmp_path)
    forged = replace(rig.release, artifact=replace(rig.release.artifact, **{field: value}))  # type: ignore[arg-type]
    with pytest.raises(DownloadError, match="release-mismatch"):
        rig.downloader.download(forged)
    assert rig.client.calls == []
    rig.no_partial()


def test_forged_oversize_metadata_not_written(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    with pytest.raises(DownloadError, match="metadata-too-large"):
        rig.downloader.download(replace(rig.release, sums=b"x" * (MAX_SUMS_BYTES + 1)))
    assert rig.client.calls == []
    rig.no_partial()


@pytest.mark.parametrize("delta", [-1, 0])
def test_space_reserve_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, delta: int
) -> None:
    rig = Rig(tmp_path)
    available = len(rig.body) + SPACE_RESERVE_BYTES + delta
    original = os.statvfs(rig.directory.parent)
    values = list(original)
    values[1] = 1  # fragment size
    values[4] = available  # available blocks
    monkeypatch.setattr(os, "statvfs", lambda path: os.statvfs_result(values))
    if delta < 0:
        with pytest.raises(DownloadError, match="no-space"):
            rig.downloader.download(rig.release)
        assert rig.client.calls == []
        rig.no_partial()
    else:
        assert rig.downloader.download(rig.release).path.exists()


@pytest.mark.parametrize(
    "body,code",
    [(b"short", "size-mismatch"), (b"x" * 97, "size-mismatch"), (b"x" * 96, "hash-mismatch")],
)
def test_wrong_package_never_published(tmp_path: Path, body: bytes, code: str) -> None:
    rig = Rig(tmp_path, "appimage")
    assert len(rig.body) == 96
    rig.response.body = body
    with pytest.raises(DownloadError, match=code):
        rig.downloader.download(rig.release)
    assert rig.response.closed
    rig.no_partial()


@pytest.mark.parametrize("phase", ["before", "downloading", "verifying"])
def test_cancel_cleans_only_own_partial(tmp_path: Path, phase: str) -> None:
    rig = Rig(tmp_path)
    rig.directory.mkdir(mode=0o700)
    foreign = rig.directory / "foreign"
    foreign.write_text("keep")
    cancel = threading.Event()
    if phase == "before":
        cancel.set()
    elif phase == "downloading":
        rig.response.before_chunk = cancel.set

    def progress(state: Phase, received: int, total: int) -> None:
        if phase == "verifying" and state == "verifying":
            cancel.set()

    with pytest.raises(DownloadError, match="cancelled"):
        rig.downloader.download(rig.release, cancel=cancel, progress=progress)
    assert list(rig.directory.iterdir()) == [foreign]
    assert foreign.read_text() == "keep"


@pytest.mark.parametrize("phase", ["checking", "downloading", "verifying"])
def test_policy_rechecked_during_transaction(tmp_path: Path, phase: str) -> None:
    rig = Rig(tmp_path)

    def progress(state: Phase, received: int, total: int) -> None:
        if state == phase:
            rig.refusal = "policy-denied"

    with pytest.raises(DownloadError, match="policy-denied"):
        rig.downloader.download(rig.release, progress=progress)
    rig.no_partial()


def test_midstream_policy_refusal(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.response.before_chunk = lambda: setattr(rig, "refusal", "policy-denied")
    with pytest.raises(DownloadError, match="policy-denied"):
        rig.downloader.download(rig.release)
    assert rig.response.closed
    rig.no_partial()


def test_network_error_closes_stream_and_removes_partial(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.response.error = NetworkError("short-read", "transport error")
    with pytest.raises(DownloadError, match="short-read"):
        rig.downloader.download(rig.release)
    assert rig.response.closed
    rig.no_partial()


def test_existing_bundle_not_overwritten_or_deleted(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    first = rig.downloader.download(rig.release)
    second = rig.downloader.download(rig.release)
    assert first.path != second.path
    assert first.path.read_bytes() == second.path.read_bytes() == rig.body
    rig.response.body = b"bad"
    with pytest.raises(DownloadError):
        rig.downloader.download(rig.release)
    assert first.path.exists() and second.path.exists()
    assert len(list(rig.directory.iterdir())) == 2


def test_symlink_ancestor_refused_without_mutating_target(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    target = tmp_path / "foreign"
    target.mkdir(mode=0o755)
    link = tmp_path / "alias"
    link.symlink_to(target, target_is_directory=True)
    rig.downloader = ReleaseDownloader(
        rig.client, rig.metadata, "0.1.0", directory=link / "updates"
    )
    with pytest.raises(DownloadError, match="path-unsafe"):
        rig.downloader.download(rig.release)
    assert list(target.iterdir()) == []
    assert stat.S_IMODE(target.stat().st_mode) == 0o755
    assert rig.client.calls == []


@pytest.mark.parametrize(
    "status,headers",
    [
        (429, {"Retry-After": "600"}),
        (429, {}),
        (403, {"X-RateLimit-Remaining": "0", "Retry-After": "600"}),
    ],
)
def test_server_backoff_survives_retry_click(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: int, headers: dict[str, str]
) -> None:
    rig = Rig(tmp_path)
    rig.response.status = status
    rig.response.headers = headers
    monkeypatch.setattr("astra_voice.updates.download.time.time", lambda: 1000.0)
    with pytest.raises(DownloadError, match="rate-limited") as first:
        rig.downloader.download(rig.release)
    assert first.value.retry_at == (1600.0 if headers else 1300.0)
    with pytest.raises(DownloadError, match="rate-limited") as second:
        rig.downloader.download(rig.release)
    assert second.value.retry_at == first.value.retry_at
    assert len(rig.client.calls) == 1
    assert rig.response.closed
    rig.no_partial()
    monkeypatch.setattr("astra_voice.updates.download.time.time", lambda: 2000.0)
    rig.response.status = 200
    assert rig.downloader.download(rig.release).path.exists()


def test_transfer_progress_is_rate_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rig = Rig(tmp_path)
    rig.response.chunk_size = 1
    monkeypatch.setattr("astra_voice.updates.download.time.monotonic", lambda: 50.0)
    states: list[Phase] = []
    rig.downloader.download(
        rig.release, progress=lambda phase, received, total: states.append(phase)
    )
    assert states == ["checking", "downloading", "verifying"]


class BlockingDownloader(ReleaseDownloader):
    def __init__(self) -> None:
        self.entered = threading.Event()
        self.proceed = threading.Event()

    def download(
        self,
        release: VerifiedRelease,
        *,
        cancel: threading.Event | None = None,
        progress: Progress | None = None,
    ) -> VerifiedDownload:
        if progress:
            progress("checking", 0, 0)
        self.entered.set()
        assert self.proceed.wait(2), "test must release worker"
        raise DownloadError(
            "cancelled" if cancel is not None and cancel.is_set() else "test-failure"
        )


def test_controller_busy_cancel_close_timeout_retains_thread(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    blocker = BlockingDownloader()
    states: list[DownloadStatus] = []
    controller = DownloadController(blocker, on_status=states.append)
    try:
        assert controller.start(rig.release) == 1
        assert blocker.entered.wait(1)
        (worker,) = controller.worker_threads
        with pytest.raises(DownloadError, match="busy"):
            controller.start(rig.release)
        controller.cancel()
        assert not controller.close(timeout=0)
        assert controller.worker_threads == (worker,)
        count = len(states)
        with pytest.raises(DownloadError, match="closed"):
            controller.start(rig.release)
        blocker.proceed.set()
        assert controller.close(timeout=1)
        assert len(controller.worker_threads) == 0
        assert len(states) == count
        assert controller.status.phase == "cancelled"
    finally:
        blocker.proceed.set()
        assert controller.close(timeout=2)


def test_controller_ready_survives_cancel_close_and_stale_snapshot(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    states: list[DownloadStatus] = []
    controller = DownloadController(rig.downloader, on_status=states.append)
    try:
        operation = controller.start(rig.release)
        controller.worker_threads[0].join(2)
        assert controller.status.phase == "ready"
        ready = controller.status
        assert ready.result is not None and ready.result.path.exists()
        controller._publish(DownloadStatus(operation - 1, "error", error="stale"))
        assert controller.status == ready
        controller.cancel()
        assert controller.close(timeout=1)
        assert ready.result.path.exists()
        assert states[-1] == ready
        with pytest.raises(FrozenInstanceError):
            ready.phase = "error"  # type: ignore[misc]
    finally:
        controller.close(timeout=2)


def test_controller_new_operation_id_and_callback_failure_do_not_leak(tmp_path: Path) -> None:
    rig = Rig(tmp_path)

    def callback(status: DownloadStatus) -> None:
        raise RuntimeError("test callback failure")

    controller = DownloadController(rig.downloader, on_status=callback)
    try:
        first = controller.start(rig.release)
        controller.worker_threads[0].join(2)
        assert controller.status.phase == "ready"
        second = controller.start(rig.release)
        assert second == first + 1
        controller.worker_threads[0].join(2)
        assert controller.status.phase == "ready"
    finally:
        assert controller.close(timeout=2)


def test_controller_rate_limit_status_exposes_retry_at(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.response.status = 429
    rig.response.headers = {"Retry-After": "600"}
    controller = DownloadController(rig.downloader)
    try:
        controller.start(rig.release)
        controller.worker_threads[0].join(2)
        assert controller.status.error == "rate-limited"
        assert controller.status.retry_at is not None
        controller.start(rig.release)
        controller.worker_threads[0].join(2)
        assert len(rig.client.calls) == 1
    finally:
        assert controller.close(timeout=2)


def test_thread_start_failure_has_no_worker_reference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = Rig(tmp_path)

    def fail_start(thread: threading.Thread) -> None:
        raise RuntimeError("test thread failure")

    monkeypatch.setattr(threading.Thread, "start", fail_start)
    controller = DownloadController(rig.downloader)
    with pytest.raises(DownloadError, match="thread-start"):
        controller.start(rig.release)
    assert len(controller.worker_threads) == 0
    assert controller.status.error == "thread-start"
    assert controller.close(timeout=0)


def test_persisted_embargo_blocks_new_instance_until_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = Rig(tmp_path)
    monkeypatch.setattr("astra_voice.updates.download.time.time", lambda: 1000.0)
    rig.response.status = 429
    rig.response.headers = {"Retry-After": "600"}
    with pytest.raises(DownloadError, match="rate-limited"):
        rig.downloader.download(rig.release)
    state_path = rig.directory / "download-backoff.json"
    state = json.loads(state_path.read_bytes())
    assert state["retry_at"] == 1600
    assert state["stored_at"] == 1000
    assert state["source"] == "https://github.com/ArrivaRUS/astra-voice/releases/download/"
    assert state_path.stat().st_size <= 4096
    assert stat.S_IMODE(state_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(rig.directory.stat().st_mode) == 0o700
    fresh = ReleaseDownloader(rig.client, rig.metadata, "0.1.1~dev9", directory=rig.directory)
    before_checks = rig.verifier.calls
    with pytest.raises(DownloadError, match="rate-limited") as error:
        fresh.download(rig.release)
    assert error.value.retry_at == 1600
    assert len(rig.client.calls) == 1 and rig.verifier.calls == before_checks
    monkeypatch.setattr("astra_voice.updates.download.time.time", lambda: 1600.0)
    rig.response.status = 200
    newest = ReleaseDownloader(rig.client, rig.metadata, "0.1.1~dev9", directory=rig.directory)
    assert newest.download(rig.release).path.exists()
    assert len(rig.client.calls) == 2
    assert not state_path.exists()


def test_embargo_survives_a_fresh_python_process(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.response.status = 429
    rig.response.headers = {"Retry-After": "600"}
    with pytest.raises(DownloadError, match="rate-limited"):
        rig.downloader.download(rig.release)
    # Sentinels ensure neither metadata nor HTTP can be reached in the new process.
    script = """
import sys
from pathlib import Path
from astra_voice.updates.download import ReleaseDownloader, DownloadError
instance = ReleaseDownloader(None, None, '0.1.0', directory=Path(sys.argv[1]))
try:
    instance.download(None)
except DownloadError as error:
    assert error.code == 'rate-limited' and error.retry_at is not None
    print('persistent-embargo')
else:
    raise AssertionError('missing embargo')
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(rig.directory)],
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src")},
        capture_output=True,
        text=True,
        check=True,
        timeout=5,
    )
    assert result.stdout.strip() == "persistent-embargo"


def state_bytes(**changes: object) -> bytes:
    state = {
        "schema": 1,
        "source": "https://github.com/ArrivaRUS/astra-voice/releases/download/",
        "stored_at": 1000,
        "retry_at": 1600,
        "failures": 1,
    }
    state.update(changes)
    return json.dumps(state).encode()


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b"x" * 4097,
        b"[" * 1500 + b"]" * 1500,
        state_bytes(source="https://evil.invalid/"),
        state_bytes(schema=True),
        state_bytes(retry_at=float("inf")),
        state_bytes(retry_at=float("nan")),
        state_bytes(retry_at=1000 + 86401),
        state_bytes(stored_at=2000, retry_at=2600),
        state_bytes(failures=True),
        state_bytes(failures=10**50),
        state_bytes(extra=1),
        state_bytes().replace(b'"schema": 1', b'"schema": 1, "schema": 1'),
    ],
)
def test_bad_state_is_ignored_and_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: bytes
) -> None:
    rig = Rig(tmp_path)
    monkeypatch.setattr("astra_voice.updates.download.time.time", lambda: 1000.0)
    rig.directory.mkdir(mode=0o700)
    path = rig.directory / "download-backoff.json"
    path.write_bytes(body)
    path.chmod(0o600)
    assert rig.downloader.download(rig.release).path.exists()
    assert path.read_bytes() == body
    # A server error must not overwrite or fix somebody else's damaged state either.
    rig.response.status = 429
    rig.response.headers = {"Retry-After": "600"}
    with pytest.raises(DownloadError, match="rate-limited"):
        rig.downloader.download(rig.release)
    assert path.read_bytes() == body


@pytest.mark.parametrize("kind", ["symlink", "fifo", "other-owner", "public", "hardlink"])
def test_unsafe_state_never_mutates_foreign_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    rig = Rig(tmp_path)
    monkeypatch.setattr("astra_voice.updates.download.time.time", lambda: 1000.0)
    rig.directory.mkdir(mode=0o700)
    path = rig.directory / "download-backoff.json"
    foreign = tmp_path / "foreign-state"
    body = state_bytes()
    foreign.write_bytes(body)
    foreign.chmod(0o600)
    if kind == "symlink":
        path.symlink_to(foreign)
    elif kind == "fifo":
        os.mkfifo(path)
    elif kind == "hardlink":
        os.link(foreign, path)
    else:
        path.write_bytes(body)
        path.chmod(0o644 if kind == "public" else 0o600)
        if kind == "other-owner":
            original = Path.lstat

            def foreign_owner(target: Path) -> os.stat_result:
                info = original(target)
                if target == path:
                    fields = list(info)
                    fields[4] = os.getuid() + 1
                    return os.stat_result(fields)
                return info

            monkeypatch.setattr(Path, "lstat", foreign_owner)
    original_info = path.lstat()
    rig.response.status = 429
    with pytest.raises(DownloadError, match="rate-limited"):
        rig.downloader.download(rig.release)
    assert len(rig.client.calls) == 1
    assert path.lstat() == original_info
    assert foreign.read_bytes() == body
    if kind != "fifo":
        assert path.read_bytes() == body


def test_old_state_does_not_create_an_indefinite_ban(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = Rig(tmp_path)
    monkeypatch.setattr("astra_voice.updates.download.time.time", lambda: 100000.0)
    rig.directory.mkdir(mode=0o700)
    path = rig.directory / "download-backoff.json"
    path.write_bytes(state_bytes())
    path.chmod(0o600)
    assert rig.downloader.download(rig.release).path.exists()
    assert not path.exists()


def test_state_from_another_source_cannot_block_this_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = Rig(tmp_path)
    monkeypatch.setattr("astra_voice.updates.download.time.time", lambda: 1000.0)
    rig.directory.mkdir(mode=0o700)
    path = rig.directory / "download-backoff.json"
    body = state_bytes(source="https://github.com/Someone/else/releases/download/")
    path.write_bytes(body)
    path.chmod(0o600)
    assert rig.downloader.download(rig.release).path.exists()
    assert path.read_bytes() == body


@pytest.mark.parametrize("boundary", ["verifying", "stream-closed"])
def test_offline_after_complete_download_keeps_local_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    rig = Rig(tmp_path, "appimage")
    reached_verifying = False
    original_exit = Response.__exit__

    def close(response: Response, *args: object) -> None:
        original_exit(response, *args)
        if boundary == "stream-closed":
            rig.refusal = "offline"

    monkeypatch.setattr(Response, "__exit__", close)

    def progress(phase: Phase, received: int, total: int) -> None:
        nonlocal reached_verifying
        if phase == "verifying":
            assert rig.response.closed
            assert received == total == len(rig.body)
            reached_verifying = True
            rig.refusal = "offline"

    result = rig.downloader.download(rig.release, progress=progress)
    assert reached_verifying
    assert result.path.read_bytes() == rig.body
    assert result.path.parent.name == "complete"
    assert stat.S_IMODE(result.path.stat().st_mode) == 0o755
    assert len(rig.client.calls) == 1


@pytest.mark.parametrize("reason", ["offline-cancel", "admin", "policy", "policy-denied"])
def test_local_verification_still_honours_cancel_and_policy(tmp_path: Path, reason: str) -> None:
    rig = Rig(tmp_path)
    cancel = threading.Event()

    def progress(phase: Phase, received: int, total: int) -> None:
        if phase == "verifying":
            assert rig.response.closed
            rig.refusal = "offline" if reason == "offline-cancel" else reason
            if reason == "offline-cancel":
                cancel.set()

    expected = "cancelled" if reason == "offline-cancel" else reason
    with pytest.raises(DownloadError, match=expected):
        rig.downloader.download(rig.release, cancel=cancel, progress=progress)
    rig.no_partial()
    assert len(rig.client.calls) == 1


@pytest.mark.parametrize("phase", ["checking", "downloading"])
def test_offline_before_local_verification_still_blocks_http(tmp_path: Path, phase: str) -> None:
    rig = Rig(tmp_path)

    def progress(current: Phase, received: int, total: int) -> None:
        if current == phase:
            rig.refusal = "offline"

    with pytest.raises(DownloadError, match="offline"):
        rig.downloader.download(rig.release, progress=progress)
    rig.no_partial()
    assert not rig.client.calls
