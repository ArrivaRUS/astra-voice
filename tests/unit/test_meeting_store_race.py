"""Contend on the real store lock from a separate descriptor."""

from __future__ import annotations

import errno
import fcntl
import os
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock, call

import pytest
from test_meeting_notice import _GracefulRole
from test_meeting_overlay import finish
from test_meeting_recording_bridge import MID, Rig

from astra_voice.core import meeting_session
from astra_voice.core.meeting_jobs import MeetingJobs
from astra_voice.meetings.events import MeetingState
from astra_voice.meetings.model import Meeting
from astra_voice.meetings.segments import CaptureJournal, SegmentSidecar
from astra_voice.meetings.store import (
    MeetingStore,
    MeetingStoreBusy,
    MeetingStoreError,
    MeetingWriter,
    Writer,
)
from astra_voice.worker import meeting_capture
from helpers.qt_app import get_qapplication

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("filesystem_euid")]
WATCHDOG = 3.0


@contextmanager
def store_lock(directory: Path) -> Iterator[int]:
    fd = os.open(directory / ".store.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield fd
    finally:
        os.close(fd)


class RetryTime:
    """Advance only when a retry asks to wait."""

    def __init__(self) -> None:
        self.now = 0.0
        self.delays: list[float] = []

    def clock(self) -> float:
        return self.now

    def wait(self, seconds: float) -> None:
        self.delays.append(seconds)
        if len(self.delays) > 20:
            raise OSError("test retry watchdog")
        self.now += seconds

    def wait_for_gui(self, stop: threading.Event, seconds: float) -> bool:
        self.wait(seconds)
        return stop.is_set()


@pytest.fixture
def room(tmp_path: Path) -> Iterator[Rig]:
    get_qapplication()
    rig = Rig(tmp_path, role_type=_GracefulRole, submit=None)
    role = rig.supervisor
    assert isinstance(role, _GracefulRole)
    role.rig = rig
    try:
        yield rig
    finally:
        rig.bridge.shutdown()
        assert rig.recording.controller.close(timeout=WATCHDOG)


def await_gui_writer(rig: Rig) -> None:
    drained = threading.Event()
    rig.recording.controller._writer.submit(drained.set)
    assert drained.wait(WATCHDOG)
    rig.recording.tick()


def prepare_room(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    previewed = threading.Event()
    submit = rig.recording._submit

    def observe_preview(preview: Callable[[], None]) -> None:
        def run() -> None:
            try:
                preview()
            finally:
                previewed.set()

        submit(run)

    with monkeypatch.context() as patch:
        patch.setattr(rig.recording, "_submit", observe_preview)
        rig.bridge.requestRecording()
        assert previewed.wait(WATCHDOG)
        rig.recording.tick()

    assert rig.bridge.selectedSource == "room"
    assert rig.bridge.startCanStart
    rig.bridge.on_start_clicked()
    await_gui_writer(rig)
    assert rig.supervisor.messages[-1]["type"] == "meeting.capture.prepare"
    assert rig.supervisor.messages[-1]["far"] == {"kind": "none"}
    rig.reply("prepared")
    await_gui_writer(rig)
    assert rig.supervisor.messages[-1]["type"] == "meeting.capture.open"


def test_room_started_retries_after_capture_releases_store_lock(
    room: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepare_room(room, monkeypatch)
    controller = room.recording.controller
    directory = room.store.root / MID
    committed = (directory / "meeting.json").stat().st_ino
    waiting, released = threading.Event(), threading.Event()
    stop = Mock(wraps=controller.stop)

    def wait(interrupt: threading.Event, seconds: float) -> bool:
        waiting.set()
        if not released.wait(WATCHDOG):
            raise OSError("test lock release watchdog")
        return interrupt.is_set()

    with monkeypatch.context() as patch:
        patch.setattr(controller, "_metadata_retry_wait", wait)
        patch.setattr(controller, "stop", stop)
        with store_lock(directory):
            room.reply("started")
            assert waiting.wait(WATCHDOG)
            # The controller must release the GUI writer lease between attempts.
            with room.store.writer(MID, Writer.GUI):
                pass
        released.set()
        await_gui_writer(room)
        assert controller.error_code == ""
        assert room.bridge.overlay["state"] == "recording"
        assert (directory / "meeting.json").stat().st_ino != committed
        stop.assert_not_called()


@pytest.mark.parametrize("failure", ["busy", "io"])
def test_room_started_only_retries_busy(
    room: Rig, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    prepare_room(room, monkeypatch)
    controller = room.recording.controller
    retry = RetryTime()
    write = Mock(wraps=room.store.write)
    stop = Mock(wraps=controller.stop)

    def disk_error(*args: object, **kwargs: object) -> None:
        raise OSError(errno.EIO, "test disk error")

    with monkeypatch.context() as patch:
        patch.setattr(meeting_session, "METADATA_RETRY_SECONDS", 0.075)
        patch.setattr(controller, "_metadata_retry_clock", retry.clock)
        patch.setattr(controller, "_metadata_retry_wait", retry.wait_for_gui)
        patch.setattr(room.store, "write", write)
        patch.setattr(controller, "stop", stop)
        if failure == "busy":
            with store_lock(room.store.root / MID):
                room.reply("started")
                await_gui_writer(room)
            assert write.call_count == 4
            assert retry.delays == pytest.approx([0.01, 0.02, 0.04, 0.005])
        else:
            patch.setattr(MeetingWriter, "_publish", disk_error)
            room.reply("started")
            await_gui_writer(room)
            assert write.call_count == 1
            assert retry.delays == []
        assert controller.error_code == "storage-unavailable"
        assert controller.state == "stopping"
        assert stop.call_args_list == [call("disk")]


def bind_exit_jobs(rig: Rig) -> MeetingJobs:
    jobs = MeetingJobs(rig.store, Mock(side_effect=AssertionError("exit must not start a role")))
    rig.recording.bind_jobs(jobs)
    return jobs


def test_exit_retries_final_snapshot_until_drained(
    room: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepare_room(room, monkeypatch)
    room.reply("started")
    await_gui_writer(room)

    controller = room.recording.controller
    jobs = bind_exit_jobs(room)
    room.recording._exit_stop_timeout = WATCHDOG
    waiting, released = threading.Event(), threading.Event()
    recorded_states: list[MeetingState] = []
    on_recorded = jobs.on_recorded

    def observe_queued(meeting_id: str) -> bool:
        meeting = room.store.read(meeting_id, "meeting.json")
        assert isinstance(meeting, Meeting)
        recorded_states.append(meeting.state)
        return on_recorded(meeting_id)

    def wait(interrupt: threading.Event, seconds: float) -> bool:
        waiting.set()
        if not released.wait(WATCHDOG):
            raise OSError("test exit join watchdog")
        return interrupt.is_set()

    with monkeypatch.context() as patch:
        patch.setattr(controller, "_metadata_retry_wait", wait)
        patch.setattr(jobs, "on_recorded", observe_queued)
        with store_lock(room.store.root / MID) as fd:
            finish(room)
            assert waiting.wait(WATCHDOG)
            assert not controller.saved
            thread = controller._writer._thread
            assert thread is not None
            join = thread.join

            def release_at_join(timeout: float | None = None) -> None:
                fcntl.flock(fd, fcntl.LOCK_UN)
                released.set()
                join(timeout)

            patch.setattr(thread, "join", release_at_join)
            room.recording.shutdown()
        assert controller.saved
        assert recorded_states == [MeetingState.RECORDED]
        meeting = room.store.read(MID, "meeting.json")
        assert isinstance(meeting, Meeting) and meeting.state == MeetingState.TRANSCRIBING


def test_exit_deadline_interrupts_wait_and_later_snapshot_tries_once(
    room: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepare_room(room, monkeypatch)
    bind_exit_jobs(room)
    controller = room.recording.controller
    room.recording._exit_stop_timeout = 0.02
    waiting, drained = threading.Event(), threading.Event()
    states: list[MeetingState] = []
    write = room.store.write

    def observe_write(meeting_id: str, filename: str, document: Meeting, *, writer: Writer) -> None:
        states.append(document.state)
        write(meeting_id, filename, document, writer=writer)

    def wait(interrupt: threading.Event, seconds: float) -> bool:
        waiting.set()
        # Model an in-progress wait when the exit budget expires.
        return interrupt.wait(WATCHDOG)

    with monkeypatch.context() as patch:
        patch.setattr(controller, "_metadata_retry_wait", wait)
        patch.setattr(room.store, "write", observe_write)
        with store_lock(room.store.root / MID):
            try:
                room.reply("started")
                assert waiting.wait(WATCHDOG)
                finish(room)
                controller._writer.submit(drained.set)
                room.recording.shutdown()
                assert drained.wait(1), "metadata writer did not wake on exit timeout"
                assert states == [MeetingState.RECORDING, MeetingState.RECORDED]
                assert not controller.saved
            finally:
                controller._metadata_retry_stop.set()
                assert controller.close(timeout=WATCHDOG)


@contextmanager
def archive(
    tmp_path: Path, retry: RetryTime, wait: Callable[[float], None] | None = None
) -> Iterator[
    tuple[MeetingStore, meeting_capture._Archive, list[meeting_capture.Message], threading.Event]
]:
    store = MeetingStore(tmp_path / "meetings")
    store.create(Meeting(id=MID, started_at="2026-11-01T12:00:00+00:00"))
    events: list[meeting_capture.Message] = []
    terminal = threading.Event()

    def emit(event: meeting_capture.Message) -> None:
        events.append(event)
        if event["type"] in {"disk-failed", "finalized"}:
            terminal.set()

    owner = meeting_capture._Archive(
        store.root / MID,
        {"meeting_id": MID, "meeting_generation": 0, "capture_id": "capture1", "state_rev": 1},
        0,
        os.fsync,
        emit,
        retry_clock=retry.clock,
        retry_wait=wait or retry.wait,
    )
    try:
        yield store, owner, events, terminal
    finally:
        owner.put("quit", None)
        owner.thread.join(WATCHDOG)
        assert not owner.thread.is_alive()


def open_archive(owner: meeting_capture._Archive) -> None:
    assert owner.put(
        "start",
        {
            "kind": "open",
            "source_kind": "mic_only",
            "t_mono_ms": 100000,
            "recorded_ms": 0,
            "reason": None,
        },
    )


def close_archive(owner: meeting_capture._Archive) -> None:
    assert owner.put(
        "close",
        {
            "kind": "close",
            "t_mono_ms": 100100,
            "recorded_ms": 100,
            "state_rev": 2,
            "reason": "stop",
        },
    )
    assert owner.put("quit", None)


@pytest.mark.parametrize("target", ["edge", "checkpoint"])
def test_capture_retries_busy_without_releasing_its_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target: str
) -> None:
    retry = RetryTime()
    waiting, released, opened = threading.Event(), threading.Event(), threading.Event()
    write = MeetingWriter.write
    seen: list[tuple[str, bool]] = []

    def observe_write(
        owner: MeetingWriter, filename: str, document: SegmentSidecar | CaptureJournal
    ) -> None:
        try:
            write(owner, filename, document)
        except MeetingStoreBusy:
            seen.append((filename, False))
            raise
        seen.append((filename, True))
        if filename == "capture.json":
            opened.set()

    def wait(seconds: float) -> None:
        retry.wait(seconds)
        waiting.set()
        if not released.wait(WATCHDOG):
            raise OSError("test capture lock release watchdog")

    with (
        monkeypatch.context() as patch,
        archive(tmp_path, retry, wait) as (store, owner, events, terminal),
    ):
        patch.setattr(MeetingWriter, "write", observe_write)
        if target == "checkpoint":
            open_archive(owner)
            assert opened.wait(WATCHDOG)
        with store_lock(owner.directory):
            if target == "edge":
                open_archive(owner)
            else:
                assert owner.put("pcm", ("mic", bytes(3200), ("pulse", 100, 100100), 0))
            assert waiting.wait(WATCHDOG)
            with pytest.raises(MeetingStoreBusy), store.writer(MID, Writer.CAPTURE):
                pytest.fail("capture lease was released during retry")
            # quit is set while close remains queued; it must never cancel a write.
            close_archive(owner)
        released.set()
        assert terminal.wait(WATCHDOG)
        assert not owner.failed.is_set()
        assert events[-1]["type"] == "finalized" and events[-1]["status"] == "succeeded"
        filename = "capture.json" if target == "edge" else "audio/mic-0000.json"
        assert (filename, False) in seen
        assert seen[seen.index((filename, False)) + 1] == (filename, True)
        assert len(retry.delays) == 1
        journal = store.read(MID, "capture.json")
        assert isinstance(journal, CaptureJournal)
        assert [edge["kind"] for edge in journal.boundaries] == ["open", "close"]
        if target == "checkpoint":
            sidecar = store.read(MID, filename)
            assert isinstance(sidecar, SegmentSidecar) and sidecar.closed
            assert sidecar.timeline == [[0, 100000], [1600, 100100]]


@pytest.mark.parametrize("failure", ["busy", "generation"])
def test_capture_failure_does_not_claim_finalized_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    retry = RetryTime()
    opened = threading.Event()
    write = MeetingWriter.write
    errors: list[type[MeetingStoreError]] = []

    def observe_write(owner: MeetingWriter, filename: str, document: CaptureJournal) -> None:
        try:
            write(owner, filename, document)
        except MeetingStoreError as exc:
            errors.append(type(exc))
            raise
        if document.boundaries[-1]["kind"] == "open":
            opened.set()

    with (
        monkeypatch.context() as patch,
        archive(tmp_path, retry) as (store, owner, events, terminal),
    ):
        patch.setattr(MeetingWriter, "write", observe_write)
        open_archive(owner)
        assert opened.wait(WATCHDOG)

        if failure == "busy":
            patch.setattr(meeting_capture, "ARCHIVE_WRITE_RETRY_SECONDS", 0.035)
            with store_lock(owner.directory):
                close_archive(owner)
                assert terminal.wait(WATCHDOG)
            assert errors == [MeetingStoreBusy] * 3
            assert retry.delays == pytest.approx([0.01, 0.02, 0.005])
        else:
            # Stale capture generation rejected by the actual _publish checks.
            owner.journal.meeting_generation += 1
            close_archive(owner)
            assert terminal.wait(WATCHDOG)
            assert errors == [MeetingStoreError]
            assert retry.delays == []

        owner.thread.join(WATCHDOG)
        assert not owner.thread.is_alive()
        assert owner.failed.is_set()
        assert events == [{"type": "disk-failed"}]
        journal = store.read(MID, "capture.json")
        assert isinstance(journal, CaptureJournal)
        assert [edge["kind"] for edge in journal.boundaries] == ["open"]


def test_capture_retry_budget_leaves_one_second_of_pcm_per_track() -> None:
    bytes_per_second = 16000 * 2  # mono s16le
    buffer_seconds = meeting_capture.MAX_TRACK_PCM_BYTES / bytes_per_second
    retry_seconds = meeting_capture.ARCHIVE_WRITE_RETRY_SECONDS
    assert 0 < retry_seconds <= 0.5
    assert buffer_seconds - retry_seconds >= 1
