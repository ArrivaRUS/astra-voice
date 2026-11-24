"""Capture reads the observer rule under its lock without waiting."""

from __future__ import annotations

import threading
from typing import Any

import pytest
from test_meeting_capture import Rig
from test_meeting_m8_call_service import CheckDouble, of_type
from test_meeting_m8_call_service import service as service
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

from astra_voice.worker.meeting_detector import ProgramKey

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("filesystem_euid")]


def recording(rig: Rig, checks: CheckDouble, capture_events: CaptureEvents) -> None:
    target = pinned_outputs(1, rig.clock())
    prepare_allowed(rig, checks, app_far(rig, checks), target)
    open_capture(rig, capture_events)


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

    def locked_read(program: ProgramKey, now: float) -> frozenset[tuple[int, int]] | None:
        assert observer.rule_lock.locked()
        return read(program, now)

    rig.clock.now += 1
    with monkeypatch.context() as patch:
        patch.setattr(observer.rule, "outputs", locked_read)
        assert_far_track(rig.poll(), "live", "partial")
    assert not observer.rule_lock.locked()
