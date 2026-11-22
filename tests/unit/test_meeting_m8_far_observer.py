"""Capture notices across observer I/O, stop and connection changes."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from test_meeting_capture import Rig
from test_meeting_m8_call_service import ANCHOR, CheckDouble, of_type
from test_meeting_m8_call_service import service as service
from test_meeting_m8_detector_source import Backend
from test_meeting_m8_locality import detector
from test_meeting_m8_role_observer import ObserverSteps
from test_meeting_m8_start import (
    CaptureEvents,
    app_far,
    assert_far_track,
    open_capture,
    pinned_outputs,
    prepare_allowed,
)
from test_meeting_m8_start import capture_events as capture_events
from test_meeting_m8_unpinned_far import decode_tracks, output, picture

from astra_voice.meetings.detector_rule import OutputSnapshot
from astra_voice.worker.meeting_detector import (
    Direction,
    Facility,
    NormalizedEvent,
    PictureConnection,
    PictureTiming,
    ProgramKey,
    RawClient,
    RawStream,
    TimeRemaining,
)
from astra_voice.worker.meeting_identity_check import PinnedStream, PinnedTarget

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("filesystem_euid")]


def recording(rig: Rig, checks: CheckDouble, capture_events: CaptureEvents) -> PinnedTarget:
    target = pinned_outputs(1, rig.clock())
    prepare_allowed(rig, checks, app_far(rig, checks), target)
    open_capture(rig, capture_events)
    return target


def test_far_tick_skips_busy_rule_lock(
    service: tuple[Rig, CheckDouble], capture_events: CaptureEvents, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig, checks = service
    recording(rig, checks, capture_events)
    picture(rig, output(rig, pinned_outputs(2).streams[1]))
    rig.clock.now += 1
    lock = rig.state.observer.rule_lock
    held, release, attempted, finished = (threading.Event() for _ in range(4))
    blocking_calls: list[bool] = []
    result: list[dict[str, Any]] = []

    class ObservedLock:
        def acquire(self, blocking: bool = True) -> bool:
            blocking_calls.append(blocking)
            attempted.set()
            return lock.acquire(blocking=blocking)

        def release(self) -> None:
            lock.release()

        def __enter__(self) -> None:
            self.acquire()

        def __exit__(self, *exc: object) -> None:
            self.release()

    def hold() -> None:
        with lock:
            held.set()
            release.wait(3)

    def poll() -> None:
        result.extend(rig.poll())
        finished.set()

    holder = threading.Thread(target=hold)
    holder.start()
    with monkeypatch.context() as patch:
        patch.setattr(rig.state.observer, "rule_lock", ObservedLock())
        worker = threading.Thread(target=poll)
        try:
            assert held.wait(2)
            worker.start()
            assert attempted.wait(2)
            assert blocking_calls == [False]
            assert finished.wait(2)
            assert lock.locked() and not release.is_set()
            assert of_type(result, "capture.alive") and not decode_tracks(result)
            assert not rig.state._far_incomplete
        finally:
            release.set()
            holder.join(2)
            if worker.ident is not None:
                worker.join(2)
        assert not holder.is_alive() and not worker.is_alive()
    rig.clock.now += 1
    assert_far_track(rig.poll(), "live", "partial")


def test_far_tick_reads_picture_under_rule_lock(
    service: tuple[Rig, CheckDouble], capture_events: CaptureEvents, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig, checks = service
    recording(rig, checks, capture_events)
    picture(rig, output(rig, pinned_outputs(2).streams[1]))
    observer = rig.state.observer
    read = observer.rule.outputs

    def locked_read(program: ProgramKey, now: float) -> OutputSnapshot | None:
        assert observer.rule_lock.locked()
        return read(program, now)

    rig.clock.now += 1
    with monkeypatch.context() as patch:
        patch.setattr(observer.rule, "outputs", locked_read)
        assert_far_track(rig.poll(), "live", "partial")
    assert not observer.rule_lock.locked()


class PlaybackBackend(Backend):
    def __init__(self, cookie: int, *streams: PinnedStream) -> None:
        super().__init__()
        self.cookie = cookie
        self.streams = {
            (Direction.OUTPUT, stream.sink_input): RawStream(
                stream.sink_input,
                10,
                Direction.OUTPUT,
                {"object.serial": stream.stream_serial},
                False,
            )
            for stream in streams
        }

    def server_cookie(self) -> int:
        return self.cookie


@contextmanager
def observing(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, *backends: PlaybackBackend
) -> Iterator[ObserverSteps]:
    steps = ObserverSteps()
    steps.clock = rig.clock
    steps.tenths = round(rig.clock() * 10)
    observer = rig.state.observer
    connections = iter(backends)
    with monkeypatch.context() as patch:
        patch.setattr(observer, "_factory", lambda *_: next(connections))
        patch.setattr(observer, "_wait", steps.wait)
        detector(rig, "start", "start")
        try:
            steps.ready()
            yield steps
        finally:
            observer.stop(exiting=True)
            steps.advance.set()
            observer.join(2)


def outputs(rig: Rig) -> OutputSnapshot | None:
    observer = rig.state.observer
    with observer.rule_lock:
        return observer.rule.outputs(ANCHOR.program, rig.clock())


@pytest.mark.parametrize("transition", ["rebuild", "stop"])
def test_far_skips_during_observer_rebuild_or_stop(
    service: tuple[Rig, CheckDouble],
    capture_events: CaptureEvents,
    monkeypatch: pytest.MonkeyPatch,
    transition: str,
) -> None:
    rig, checks = service
    target = recording(rig, checks, capture_events)
    pinned, new = pinned_outputs(2).streams
    backend = PlaybackBackend(target.server_cookie, pinned, new)
    armed, refreshing, release = (threading.Event() for _ in range(3))
    snapshot, close = backend.snapshot, backend.close

    def await_release() -> None:
        refreshing.set()
        assert release.wait(3)

    def blocked_snapshot(
        *, remaining: TimeRemaining | None = None
    ) -> tuple[tuple[RawClient, ...], tuple[RawStream, ...]]:
        if armed.is_set():
            await_release()
        return snapshot(remaining=remaining)

    def blocked_close() -> None:
        await_release()
        close()

    with monkeypatch.context() as patch:
        patch.setattr(backend, "snapshot", blocked_snapshot)
        with observing(rig, monkeypatch, backend) as steps:
            assert (before := outputs(rig)) is not None
            assert before.connection == PictureConnection(target.server_cookie, 1)
            assert (new.sink_input, new.stream_serial) in before.streams
            rig.poll()
            try:
                if transition == "rebuild":
                    armed.set()
                    backend.streams.pop((Direction.OUTPUT, new.sink_input))
                    backend.notify(Facility.SERVER, index=0)
                else:
                    patch.setattr(backend, "close", blocked_close)
                    detector(rig, "stop", "stop")
                    # The observer has not been allowed to leave its wait.
                    assert not backend.closed and outputs(rig) is None
                    rig.clock.now += 1
                    stopping = rig.poll()
                    assert of_type(stopping, "detector.stopped") and not decode_tracks(stopping)
                steps.tenths += 10
                steps.advance.set()
                assert refreshing.wait(2)
                if transition == "stop":
                    rig.clock.now += 1
                result = rig.poll()
                assert of_type(result, "capture.alive") and not decode_tracks(result)
                assert not rig.state._far_incomplete
                assert outputs(rig) is None
            finally:
                release.set()
            if transition == "rebuild":
                steps.ready()
                assert (fresh := outputs(rig)) is not None
                assert fresh.connection == before.connection
                assert fresh.streams == {(pinned.sink_input, pinned.stream_serial)}


def test_far_does_not_republish_inflight_picture_from_stopped_epoch(
    service: tuple[Rig, CheckDouble], capture_events: CaptureEvents, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig, checks = service
    target = recording(rig, checks, capture_events)
    pinned, new = pinned_outputs(2).streams
    backend = PlaybackBackend(target.server_cookie, pinned)
    observer = rig.state.observer
    armed, entered, release, updated, finish = (threading.Event() for _ in range(5))
    snapshot, update = backend.snapshot, observer._update

    def blocked_snapshot(
        *, remaining: TimeRemaining | None = None
    ) -> tuple[tuple[RawClient, ...], tuple[RawStream, ...]]:
        if armed.is_set():
            entered.set()
            assert release.wait(3)
        return snapshot(remaining=remaining)

    def after_update(
        events: tuple[NormalizedEvent, ...], timing: PictureTiming, epoch: int
    ) -> None:
        update(events, timing, epoch)
        updated.set()
        assert finish.wait(3)

    with monkeypatch.context() as patch:
        patch.setattr(backend, "snapshot", blocked_snapshot)
        with observing(rig, monkeypatch, backend) as steps:
            rig.poll()
            armed.set()
            patch.setattr(observer, "_update", after_update)
            backend.streams.update(PlaybackBackend(target.server_cookie, new).streams)
            backend.notify(Facility.SERVER, index=0)
            try:
                steps.tenths += 10
                steps.advance.set()
                assert entered.wait(2)
                detector(rig, "stop", "stop")
                detector(rig, "start", "restart")
                release.set()
                assert updated.wait(2)
                result = rig.poll()
                assert of_type(result, "capture.alive") and not decode_tracks(result)
                assert not rig.state._far_incomplete
                assert outputs(rig) is None
            finally:
                release.set()
                finish.set()


def test_far_refuses_foreign_picture_when_control_cookie_matches(
    service: tuple[Rig, CheckDouble], capture_events: CaptureEvents, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig, checks = service
    target = recording(rig, checks, capture_events)
    observer = rig.state.observer
    other_cookie = target.server_cookie + 1
    observer.registry.set("control", (51, target.server_cookie))
    observer.registry.set("observer", (42, other_cookie))
    picture(rig, output(rig, pinned_outputs(2).streams[1]), cookie=other_cookie)
    read = observer.rule.outputs

    def disconnect_after_read(program: ProgramKey, now: float) -> OutputSnapshot:
        rows = read(program, now)
        assert rows is not None and rows.connection.cookie == other_cookie
        observer.registry.set("observer", None)
        return rows

    rig.clock.now += 1
    with monkeypatch.context() as patch:
        patch.setattr(observer.rule, "outputs", disconnect_after_read)
        assert not decode_tracks(rig.poll())
    assert observer.registry.snapshot().server_cookie == target.server_cookie
    assert not rig.state._far_incomplete


def test_picture_provenance_changes_with_observer_connection(
    service: tuple[Rig, CheckDouble], monkeypatch: pytest.MonkeyPatch
) -> None:
    rig, _ = service
    pinned, new = pinned_outputs(2).streams
    first, second = PlaybackBackend(901, pinned), PlaybackBackend(902, new)
    with observing(rig, monkeypatch, first, second) as steps:
        snap = outputs(rig)
        assert snap is not None
        assert snap.connection == PictureConnection(901, 1)
        assert snap.streams == {(pinned.sink_input, pinned.stream_serial)}
        first.fail = True
        steps.step()
        assert outputs(rig) is None
        steps.step(11)
        snap = outputs(rig)
        assert snap is not None
        assert snap.connection == PictureConnection(902, 2)
        assert snap.streams == {(new.sink_input, new.stream_serial)}
