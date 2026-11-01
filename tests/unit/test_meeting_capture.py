"""M2 step 1: real state/storage with fake PCM, server clocks and fsync."""

from __future__ import annotations

import json
import threading
import time
import wave
from pathlib import Path
from typing import Any

import pytest

from astra_voice.meetings.model import Meeting
from astra_voice.meetings.store import MeetingStore
from astra_voice.worker import ipc
from astra_voice.worker.meeting_capture import MeetingCaptureState
from astra_voice.worker.meeting_session import SessionAdmission

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("filesystem_euid")]
MID = "2026-10-31_120000-ab12"


class Clock:
    now = 100.0

    def __call__(self) -> float:
        return self.now


class Source:
    def __init__(self) -> None:
        self.opened = False
        self.chunks: list[bytes | None] = []
        self.samples = 0
        self.device_change: object | None = None
        self.ended = False
        self.clock_kind = "pulse"

    def open(self, device: str | None = None) -> None:
        self.opened = True

    def read_chunk(self) -> bytes | None:
        if self.chunks:
            chunk = self.chunks.pop(0)
            if chunk:
                self.samples += len(chunk) // 2
            return chunk
        return b""

    def sample_clock(self) -> tuple[str, int, int]:
        return self.clock_kind, self.samples // 16, 100000 + self.samples // 16

    def close(self) -> None:
        self.opened = False

    shutdown = close


class Backend:
    def __init__(self) -> None:
        self.sources: list[Source] = []
        self.detecting = False

    def prepare(self, far: dict[str, Any]) -> dict[str, Any]:
        return {"server_local": True, "server_kind": "pulseaudio", "far_available": True}

    def source(self, far: dict[str, Any]) -> Source:
        source = Source()
        self.sources.append(source)
        return source

    def detector_start(self) -> int:
        self.detecting = True
        return 42

    def detector_stop(self) -> None:
        self.detecting = False

    def close(self) -> None:
        self.detecting = False


def wait_until(predicate: Any) -> None:
    end = time.monotonic() + 3
    while not predicate():
        assert time.monotonic() < end, "capture-test-timeout"
        time.sleep(0.005)


class Rig:
    def __init__(self, tmp_path: Path, **kwargs: Any) -> None:
        self.clock = Clock()
        self.mics: list[Source] = []
        self.backend = Backend()
        self.store = MeetingStore(tmp_path / "meetings")
        self.store.create(Meeting(id=MID, started_at="2026-10-31T12:00:00+00:00"))
        self.directory = tmp_path / "meetings" / MID
        if "session" not in kwargs:
            session = SessionAdmission(kind="KDE", clock=self.clock, max_age=float("inf"))
            session.update(known=True, active=True, locked=False, sleeping=False)
            kwargs["session"] = session
        self.state = MeetingCaptureState(
            clock=self.clock,
            mic_factory=self.mic,
            backend_factory=lambda: self.backend,
            free_mb=lambda: 1000,
            **kwargs,
        )
        self.req = 0
        self.identity: dict[str, Any] = {"meeting_id": MID, "meeting_generation": 0}
        self.events: list[dict[str, Any]] = []
        self.inbox: list[dict[str, Any]] = []
        self.commands: list[dict[str, Any]] = []

    def mic(self) -> Source:
        source = Source()
        self.mics.append(source)
        return source

    def send(self, action: str, **fields: Any) -> list[dict[str, Any]]:
        self.req += 1
        kind = "meeting.capture." + action
        message = {"type": kind, "request_type": kind, "req": f"r{self.req}", **fields}
        if action != "shutdown":
            message = {**self.identity, **message}
        self.commands.append(message)
        result = self.state.handle(message)
        for event in result:
            ipc.encode(event, role="meeting-capture")
        self.events.extend(result)
        self.inbox.extend(result)
        self._identity(result)
        return result

    def prepare(self, kind: str = "app_stream") -> None:
        replies = self.send(
            "prepare",
            dir=str(self.directory),
            far={"kind": kind, "sink_input": 7, "object_serial": 11, "call_id": "call1"},
            segment_s=300,
            fsync_s=10,
            segment_index_from=0,
        )
        prepared = self.until("meeting.capture.prepared")
        assert prepared["type"] == "meeting.capture.prepared", replies
        self.identity.update({key: prepared[key] for key in ("capture_id", "state_rev")})

    def open(self, action: str = "open") -> None:
        replies = self.send(action, blocked_epoch=0, admission_mono_ms=int(self.clock() * 1000))
        assert replies[0]["type"] == "meeting.ack"
        self.until("meeting.capture.started")

    def poll(self) -> list[dict[str, Any]]:
        events = self.state.poll()
        for event in events:
            ipc.encode(event, role="meeting-capture")
        self.events.extend(events)
        self.inbox.extend(events)
        self._identity(events)
        return events

    def _identity(self, events: list[dict[str, Any]]) -> None:
        for event in events:
            if "capture_id" in event and event["capture_id"] == self.identity.get("capture_id"):
                self.identity["state_rev"] = max(self.identity["state_rev"], event["state_rev"])

    def frames(self, track: str, count: int) -> None:
        def written() -> bool:
            archive = self.state.archive
            return (
                archive is not None
                and track in archive.segments
                and archive.segments[track][2].frames >= count
            )

        wait_until(written)

    def until(self, kind: str) -> dict[str, Any]:
        found: list[dict[str, Any]] = []

        def ready() -> bool:
            self.poll()
            for index, event in enumerate(self.inbox):
                if event["type"] == kind:
                    found.append(self.inbox.pop(index))
                    return True
            return False

        wait_until(ready)
        return found[-1]

    def stop(self) -> None:
        self.send("stop")
        self.until("meeting.capture.finalized")

    def cleanup(self) -> None:
        self.state.cleanup()


@pytest.fixture
def rig(tmp_path: Path) -> Any:
    instance = Rig(tmp_path)
    yield instance
    instance.cleanup()


def test_prepare_has_zero_pcm_or_files_and_req_is_idempotent(rig: Rig) -> None:
    rig.prepare()
    assert not rig.mics and not rig.backend.sources
    assert list((rig.directory / "audio").iterdir()) == []
    message = {"type": "ping", "request_type": "ping", "req": "duplicate"}
    assert rig.state.handle(message) == rig.state.handle(message)
    message.update(type="meeting.call.query", request_type="meeting.call.query")
    assert rig.state.handle(message)[0]["code"] == "req-conflict"


@pytest.mark.parametrize("age", [-1, 1001])
def test_stale_or_future_open_has_zero_streams(rig: Rig, age: int) -> None:
    rig.prepare()
    result = rig.send("open", blocked_epoch=0, admission_mono_ms=100000 - age)
    assert result[-1]["code"] == "admission-stale"
    assert not rig.mics and not rig.backend.sources


def test_two_tracks_wav_sidecar_and_recoverable_pause_boundary(rig: Rig) -> None:
    rig.prepare()
    rig.open()
    for source in [rig.mics[0], rig.backend.sources[0]]:
        source.chunks.append(b"\x01\x00" * 16000)
    wait_until(lambda: all(s.samples == 16000 for s in [rig.mics[0], rig.backend.sources[0]]))
    rig.frames("mic", 16000)
    rig.frames("far", 16000)
    rig.clock.now += 1
    rig.send("pause", reason="user")
    rig.until("meeting.capture.finalized")
    for track in ("mic", "far"):
        with wave.open(str(rig.directory / "audio" / f"{track}-0000.wav")) as audio:
            assert audio.getnframes() == 16000 and audio.getframerate() == 16000
        sidecar = json.loads((rig.directory / "audio" / f"{track}-0000.json").read_text())
        assert sidecar["closed"] and sidecar["close_reason"] == "user"
        assert sidecar["sample_end"] - sidecar["sample_start"] == 16000
        assert sidecar["clock_anchor"]["t_mono_ms"] == 100000
    journal = json.loads((rig.directory / "capture.json").read_text())
    assert journal["boundaries"][-1]["reason"] == "user"
    assert journal["boundaries"][-1]["kind"] == "close"


def test_close_does_not_wait_for_fsync_or_keepalive(tmp_path: Path) -> None:
    entered, release = threading.Event(), threading.Event()

    def blocked_fsync(fd: int) -> None:
        entered.set()
        assert release.wait(5)

    rig = Rig(tmp_path, fsync=blocked_fsync)
    try:
        rig.prepare()
        rig.open()
        assert entered.wait(1)
        start = time.monotonic()
        rig.send("pause", reason="user")
        rig.until("meeting.capture.closed")
        assert time.monotonic() - start < 1
        assert not rig.mics[0].opened and not rig.backend.sources[0].opened
        assert not any(e["type"] == "meeting.capture.finalized" for e in rig.events)
        for seq in range(3):
            assert rig.send("keepalive", seq=seq)[0]["type"] == "meeting.ack"
        release.set()
        rig.until("meeting.capture.finalized")
    finally:
        release.set()
        rig.cleanup()


def test_resume_creates_new_epochs_and_no_pause_pcm(rig: Rig) -> None:
    rig.prepare("none")
    rig.open()
    rig.mics[-1].chunks.append(b"\0\0" * 1600)
    rig.frames("mic", 1600)
    rig.send("pause", reason="user")
    rig.until("meeting.capture.finalized")
    first = json.loads((rig.directory / "audio/mic-0000.json").read_text())
    rig.clock.now += 20
    rig.open("resume")
    rig.mics[-1].chunks.append(b"\0\0" * 1600)
    rig.frames("mic", 1600)
    rig.stop()
    second = json.loads((rig.directory / "audio/mic-0001.json").read_text())
    assert first["epoch_id"] != second["epoch_id"]
    assert second["sample_start"] == 0
    assert not rig.backend.sources


def test_disk_low_and_lease_self_pause_are_durable(rig: Rig) -> None:
    rig.prepare()
    rig.open()
    rig.state.free_mb = lambda: 199
    rig.clock.now += 10
    rig.send("keepalive", seq=1)
    rig.until("meeting.capture.autopaused")
    rig.until("meeting.capture.finalized")
    journal = json.loads((rig.directory / "capture.json").read_text())
    assert journal["boundaries"][-1]["reason"] == "disk"


def test_lease_without_gui_closes_and_persists(rig: Rig) -> None:
    rig.prepare()
    rig.open()
    rig.clock.now += 5
    rig.until("meeting.capture.autopaused")
    rig.until("meeting.capture.finalized")
    journal = json.loads((rig.directory / "capture.json").read_text())
    assert journal["boundaries"][-1]["reason"] == "lease"


def test_stop_replay_and_identity_mismatch_do_not_reopen(rig: Rig) -> None:
    rig.prepare()
    rig.open()
    bad = rig.send("stop", capture_id="wrong")
    assert bad[0]["code"] == "not-prepared"
    assert rig.mics[0].opened
    rig.stop()
    assert len(rig.mics) == 1 and not rig.mics[0].opened


def test_far_removed_ends_without_reopening_or_affecting_mic(rig: Rig) -> None:
    rig.prepare()
    rig.open()
    rig.backend.sources[0].ended = True
    rig.backend.sources[0].chunks.append(None)
    event = rig.until("meeting.capture.track")
    assert event["track"] == "far" and event["state"] == "ended"
    for seq in range(11):
        rig.clock.now += 1
        rig.send("keepalive", seq=seq)
        rig.poll()
    assert rig.mics[0].opened
    rig.mics[0].chunks.append(b"\0\0" * 1600)
    wait_until(lambda: rig.mics[0].samples == 1600)
    assert len(rig.backend.sources) == 1


def test_disk_preflight_refuses_below_600(rig: Rig) -> None:
    rig.state.free_mb = lambda: 599
    replies = rig.send(
        "prepare",
        dir=str(rig.directory),
        far={"kind": "none"},
        segment_s=300,
        fsync_s=10,
        segment_index_from=0,
    )
    assert replies[0]["type"] == "meeting.ack"
    assert rig.until("error")["code"] == "limit"
    assert not rig.mics


def test_close_timeout_not_extended_by_heartbeats(rig: Rig) -> None:
    rig.prepare("none")
    rig.open()
    release = threading.Event()
    source = rig.mics[0]

    def blocked_close() -> None:
        release.wait(4)
        source.opened = False

    source.shutdown = blocked_close  # type: ignore[method-assign]
    try:
        rig.send("pause", reason="user")
        for seq in range(3):
            rig.clock.now += 0.4
            rig.send("keepalive", seq=seq)
            rig.poll()
        assert rig.state.state == "closing-unknown"
        assert rig.state.exit_code != 0
        assert not any(e["type"] == "meeting.capture.closed" for e in rig.events)
        assert (
            rig.send("resume", blocked_epoch=0, admission_mono_ms=int(rig.clock() * 1000))[0][
                "type"
            ]
            == "error"
        )
    finally:
        release.set()


def test_clock_change_splits_epoch_in_durable_sidecars(rig: Rig) -> None:
    rig.prepare("none")
    rig.open()
    source = rig.mics[0]
    source.chunks.append(b"\0\0" * 1600)
    rig.frames("mic", 1600)
    source.clock_kind = "monotonic"
    source.chunks.append(b"\0\0" * 1600)
    wait_until(lambda: rig.state.archive is not None and rig.state.archive.counts["mic"] == 1)
    rig.frames("mic", 1600)
    rig.stop()
    first = json.loads((rig.directory / "audio/mic-0000.json").read_text())
    second = json.loads((rig.directory / "audio/mic-0001.json").read_text())
    assert first["close_reason"] == "clock_change"
    assert first["epoch_id"] != second["epoch_id"]
    assert second["clock"] == "monotonic"


def test_mic_gap_resume_leaves_far_running(rig: Rig) -> None:
    rig.prepare()
    rig.open()
    rig.mics[0].device_change = object()
    rig.mics[0].chunks.append(None)
    rig.until("meeting.capture.track")
    wait_until(lambda: not rig.mics[0].opened)
    far = rig.backend.sources[0]
    assert far.opened
    replies = rig.send(
        "track.resume",
        blocked_epoch=0,
        admission_mono_ms=100000,
        track="mic",
        mic_device="explicit",
    )
    assert replies[0]["type"] == "meeting.ack"
    event = rig.until("meeting.capture.track")
    assert event["track"] == "mic" and event["kind"] == "reopened"
    assert far.opened and len(rig.backend.sources) == 1
    rig.stop()


def test_shutdown_waits_for_physical_close_before_bye(rig: Rig) -> None:
    rig.prepare()
    rig.open()
    replies = rig.send("shutdown")
    assert replies[0]["type"] == "meeting.ack"
    rig.until("meeting.capture.bye")
    assert not rig.mics[0].opened and not rig.backend.sources[0].opened


def test_backlog_bounds_pcm_and_self_pauses(tmp_path: Path) -> None:
    entered, release = threading.Event(), threading.Event()

    def blocked_fsync(fd: int) -> None:
        entered.set()
        release.wait(5)

    rig = Rig(tmp_path, fsync=blocked_fsync)
    try:
        rig.prepare()
        rig.open()
        assert entered.wait(1)
        rig.mics[0].chunks.extend([b"\0\0" * 16000 for _ in range(8)])
        rig.until("meeting.capture.autopaused")
        rig.until("meeting.capture.closed")
        assert rig.state.archive is not None
        assert rig.state.archive.pending_bytes <= 128000
    finally:
        release.set()
        rig.cleanup()
