"""Independent filesystem/lifecycle acceptance of the signed-release contract.

Transport and detached signatures are test doubles. Metadata schema, metadata
hashes, package hashes, permissions and publication use the actual implementation.
Cryptographic trust itself is covered by test_release_trust_independent.py.
"""

from __future__ import annotations

import errno
import hashlib
import json
import stat
import threading
from collections.abc import Callable, Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from astra_voice.net.http import HttpClient
from astra_voice.security.verify import Verifier, VerifyResult
from astra_voice.updates.download import DownloadController, DownloadError, ReleaseDownloader
from astra_voice.updates.release import ReleaseMetadata, Track

pytestmark = pytest.mark.unit


class DetachedSignature(Verifier):
    def __init__(self) -> None:
        super().__init__("release", Path("/nonexistent/test-only-keyring"))
        self.calls = 0

    def verify_detached(
        self, data: Path, sig: Path, *, cancel: Callable[[], bool] | None = None
    ) -> VerifyResult:
        self.calls += 1
        return VerifyResult(True)


class Stream:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.status = 200
        self.headers: dict[str, str] = {}
        self.closed = False
        self.after_body: Callable[[], None] = lambda: None

    def __enter__(self) -> Stream:
        return self

    def __exit__(self, *unused: object) -> None:
        self.closed = True

    def iter_chunks(self, *, limit: int) -> Iterator[bytes]:
        yield self.payload[:limit]
        self.after_body()


class Transport(HttpClient):
    def __init__(self, stream: Stream) -> None:
        # No real HttpClient/session/gate: only the public stream boundary is used.
        self.stream = stream
        self.calls = 0

    def get_stream(self, url: str, **kwargs: Any) -> Any:
        self.calls += 1
        assert kwargs["kind"] == "download"
        assert url.startswith("https://github.com/ArrivaRUS/astra-voice/releases/download/v")
        return self.stream


class FilesystemCase:
    def __init__(self, root: Path, track: Track = "deb") -> None:
        self.body = b"independent harmless file\x00\xff" * 23
        self.stream = Stream(self.body)
        self.transport = Transport(self.stream)
        self.verifier = DetachedSignature()
        self.metadata = ReleaseMetadata(
            self.transport, self.verifier, staging_dir=root / "metadata-snapshots"
        )
        self.directory = root / "downloads"
        name = (
            "astra-voice_1.4.2_amd64.deb" if track == "deb" else "Astra_Voice-1.4.2-x86_64.AppImage"
        )
        digest = hashlib.sha256(self.body).hexdigest()
        artifacts = {
            "deb": {"name": "astra-voice_1.4.2_amd64.deb", "sha256": digest, "size": len(self.body)}
        }
        if track == "appimage":
            artifacts[track] = {"name": name, "sha256": digest, "size": len(self.body)}
        latest = json.dumps(
            {
                "schema": 2,
                "version": "1.4.2",
                "published_at": "2026-10-09T12:00:00Z",
                "min_astra": "1.8",
                "release_url": "https://github.com/ArrivaRUS/astra-voice/releases/tag/v1.4.2",
                "artifacts": artifacts,
            }
        ).encode()
        offered = root / "offered"
        offered.mkdir(mode=0o700)
        (offered / "latest.json").write_bytes(latest)
        (offered / "SHA256SUMS").write_text(
            f"{hashlib.sha256(latest).hexdigest()}  latest.json\n"
            + "".join(f"{digest}  {item['name']}\n" for item in artifacts.values())
        )
        (offered / "SHA256SUMS.asc").write_bytes(b"detached-signature-test-double")
        self.release = self.metadata.validate_local(offered, "v1.4.2", "1.0.0", track)

    def downloader(self) -> ReleaseDownloader:
        return ReleaseDownloader(self.transport, self.metadata, "1.0.0", directory=self.directory)

    def published(self) -> list[Path]:
        return sorted(self.directory.glob("download-*/complete"))


@pytest.mark.parametrize("track,mode", [("deb", 0o600), ("appimage", 0o755)])
def test_complete_is_one_publication_with_metadata_and_no_early_execute_bit(
    tmp_path: Path, track: Track, mode: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = FilesystemCase(tmp_path, track)
    original = Path.rename
    observations: list[set[str]] = []

    def observe(source: Path, target: Path) -> Path:
        if source.name == ".partial":
            assert not target.exists()
            assert not case.published()
            observations.append({entry.name for entry in source.iterdir()})
        return original(source, target)

    monkeypatch.setattr(Path, "rename", observe)

    def progress(phase: str, received: int, total: int) -> None:
        if phase == "verifying":
            partial = next(case.directory.glob("download-*/.partial/*.part"))
            assert stat.S_IMODE(partial.stat().st_mode) == 0o600
            assert not case.published()

    result = case.downloader().download(case.release, progress=progress)
    expected = {case.release.artifact.name, "SHA256SUMS", "SHA256SUMS.asc", "latest.json"}
    assert observations == [expected]
    assert {entry.name for entry in result.path.parent.iterdir()} == expected
    assert result.path.read_bytes() == case.body
    assert stat.S_IMODE(result.path.stat().st_mode) == mode
    assert stat.S_IMODE(result.path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(result.path.parent.parent.stat().st_mode) == 0o700
    assert all(
        stat.S_IMODE((result.path.parent / name).stat().st_mode) == 0o600
        for name in expected - {case.release.artifact.name}
    )
    assert case.stream.closed and case.transport.calls == 1


@pytest.mark.parametrize("when", ["last-byte", "verifying", "before-publication"])
def test_retry_cancellation_preserves_previous_verified_bundle_and_foreign_files(
    tmp_path: Path, when: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = FilesystemCase(tmp_path, "appimage")
    previous = case.downloader().download(case.release)
    saved = {path: path.read_bytes() for path in previous.path.parent.iterdir()}
    foreign = case.directory / "foreign.part"
    foreign.write_bytes(b"not owned by this operation")
    cancel = threading.Event()
    if when == "last-byte":
        case.stream.after_body = cancel.set
    elif when == "before-publication":
        rename = Path.rename

        def cancel_after_package_rename(source: Path, target: Path) -> Path:
            result = rename(source, target)
            if source.name.endswith(".part"):
                cancel.set()
            return result

        monkeypatch.setattr(Path, "rename", cancel_after_package_rename)

    def progress(phase: str, received: int, total: int) -> None:
        if when == "verifying" and phase == "verifying":
            cancel.set()

    with pytest.raises(DownloadError) as failed:
        case.downloader().download(case.release, cancel=cancel, progress=progress)
    assert failed.value.code == "cancelled"
    assert case.published() == [previous.path.parent]
    assert all(path.read_bytes() == body for path, body in saved.items())
    assert foreign.read_bytes() == b"not owned by this operation"
    assert not list(case.directory.glob("download-*/.partial"))


def test_publication_io_failure_preserves_existing_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = FilesystemCase(tmp_path)
    previous = case.downloader().download(case.release)
    rename = Path.rename

    def disk_full(source: Path, target: Path) -> Path:
        if source.name == ".partial":
            raise OSError(errno.ENOSPC, "test-only publication failure")
        return rename(source, target)

    monkeypatch.setattr(Path, "rename", disk_full)
    with pytest.raises(DownloadError) as failed:
        case.downloader().download(case.release)
    assert failed.value.code == "no-space"
    assert case.published() == [previous.path.parent]
    assert previous.path.read_bytes() == case.body
    assert not list(case.directory.glob("download-*/.partial"))


def test_persistent_rate_limit_survives_cancel_and_ends_only_after_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = FilesystemCase(tmp_path)
    now = [2000000000.0]
    monkeypatch.setattr("astra_voice.updates.download.time.time", lambda: now[0])
    previous = case.downloader().download(case.release)
    case.stream.status = 429
    case.stream.headers = {"Retry-After": "120"}
    with pytest.raises(DownloadError) as failed:
        case.downloader().download(case.release)
    deadline = failed.value.retry_at
    assert failed.value.code == "rate-limited" and deadline is not None
    state = case.directory / "download-backoff.json"
    persisted = state.read_bytes()
    signature_calls = case.verifier.calls
    http_calls = case.transport.calls
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(DownloadError, match="cancelled"):
        case.downloader().download(case.release, cancel=cancel)
    assert state.read_bytes() == persisted
    case.stream.status = 200
    now[0] = deadline - 0.001
    with pytest.raises(DownloadError, match="rate-limited"):
        case.downloader().download(case.release)
    assert case.transport.calls == http_calls
    assert case.verifier.calls == signature_calls
    assert previous.path.read_bytes() == case.body
    now[0] = deadline + 0.001
    next_result = case.downloader().download(case.release)
    assert next_result.path != previous.path
    assert next_result.path.read_bytes() == previous.path.read_bytes()
    assert not state.exists()


def test_saved_verified_flag_does_not_hide_metadata_change(tmp_path: Path) -> None:
    case = FilesystemCase(tmp_path)
    altered = replace(case.release, latest=case.release.latest.replace(b"1.8", b"1.9"))
    with pytest.raises(DownloadError) as failed:
        case.downloader().download(altered)
    assert failed.value.code == "hash-mismatch"
    assert case.transport.calls == 0
    assert not case.published()


def test_timed_out_close_owns_worker_and_successful_close_blocks_all_callbacks(
    tmp_path: Path,
) -> None:
    case = FilesystemCase(tmp_path)
    entered, resume = threading.Event(), threading.Event()
    events: list[str] = []

    def suspend_after_body() -> None:
        entered.set()
        assert resume.wait(3), "test must release its own stream"

    case.stream.after_body = suspend_after_body
    controller = DownloadController(
        case.downloader(), on_status=lambda value: events.append(value.phase)
    )
    controller.start(case.release)
    try:
        assert entered.wait(3)
        assert not controller.close(timeout=0)
        assert len(controller.worker_threads) == 1
        count = len(events)
    finally:
        resume.set()
        assert controller.close(timeout=3)
    assert events[count:] == []
    assert not controller.worker_threads
    assert controller.status.phase == "cancelled"
    assert not case.published()
