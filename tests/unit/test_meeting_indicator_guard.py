"""BL-4: physical indicator evidence and the strict one-second grace period."""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture
def guard_rig() -> Iterator[Any]:
    from astra_voice.ui.meeting_indicator_guard import MeetingIndicatorGuard
    from helpers.qt_app import get_qapplication

    get_qapplication()
    state = SimpleNamespace(
        now=100.0, active=True, overlay=True, tray=False, capturing=False, values=[], pauses=[]
    )
    guard = MeetingIndicatorGuard(
        active=lambda: state.active,
        overlay_visible=lambda: state.overlay,
        tray_visible=lambda: state.tray,
        set_visible=state.values.append,
        pause=lambda: state.pauses.append(state.now),
        capturing=lambda: state.capturing,
        clock=lambda: state.now,
    )
    state.guard = guard
    try:
        yield state
    finally:
        guard.shutdown()


@pytest.mark.parametrize("overlay,tray", [(True, False), (False, True), (True, True)])
def test_one_live_indicator_prevents_pause(guard_rig: Any, overlay: bool, tray: bool) -> None:
    r = guard_rig
    r.overlay, r.tray = overlay, tray
    r.guard.check()
    r.now += 10
    r.guard.check()
    assert r.values == [True]


def test_both_lost_strictly_over_one_second_and_one_recovery(guard_rig: Any) -> None:
    r = guard_rig
    r.guard.check()
    r.overlay = False
    r.guard.check()
    r.now += 1
    r.guard.check()
    assert r.values == [True, False]
    assert not r.pauses
    r.now += 0.001
    r.guard.check()
    r.guard.check()
    assert r.values == [True, False]
    assert len(r.pauses) == 1
    r.tray = True
    r.guard.check()
    r.guard.check()
    assert r.values == [True, False, True]


def test_transient_loss_does_not_accumulate_across_recovery(guard_rig: Any) -> None:
    r = guard_rig
    r.guard.check()
    for _ in range(3):
        r.overlay = False
        r.guard.check()
        r.now += 0.9
        r.guard.check()
        r.overlay = True
        r.guard.check()
    assert not r.pauses


def test_inactive_meeting_does_not_inherit_previous_loss_deadline(guard_rig: Any) -> None:
    r = guard_rig
    r.overlay = False
    r.guard.check()
    r.active = False
    r.now += 3
    r.guard.check()
    r.active = True
    r.guard.check()
    assert not r.pauses
    r.now += 1.001
    r.guard.check()
    assert len(r.pauses) == 1


def test_capture_leaving_indicator_pause_rearms_while_still_invisible(guard_rig: Any) -> None:
    r = guard_rig
    r.guard.check()
    r.overlay = False
    r.guard.check()
    r.now += 1.001
    r.guard.check()
    assert r.pauses == [r.now]
    # A manual resume reopens capture without any visible indicator.
    r.capturing = True
    r.guard.check()
    r.now += 1
    r.guard.check()
    assert len(r.pauses) == 1
    r.now += 0.001
    r.capturing = False
    r.guard.check()
    assert len(r.pauses) == 2


def test_shutdown_prevents_late_callbacks(guard_rig: Any) -> None:
    r = guard_rig
    r.guard.check()
    r.guard.shutdown()
    before = list(r.values), list(r.pauses)
    r.overlay = r.tray = False
    r.now += 20
    r.guard.check()
    assert (r.values, r.pauses) == before
