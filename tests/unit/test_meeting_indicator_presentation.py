"""BL-4 view evidence and the eight-second explanation after recovery."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock

import pytest
from PyQt5.QtCore import QPoint, QRect, Qt, QTimer
from PyQt5.QtGui import QGuiApplication, QRegion
from test_meeting_overlay import start
from test_meeting_recording_bridge import Rig
from test_meeting_recording_bridge import rig as rig

from astra_voice.ui.meeting_overlay import MeetingOverlay

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("filesystem_euid")]


@pytest.mark.parametrize(
    "case", ["shown", "hidden", "minimized", "offscreen", "shadow-only", "unexposed"]
)
def test_w2_indicator_requires_visible_surface_on_screen(case: str) -> None:
    from helpers.qt_app import get_qapplication

    get_qapplication()
    assert QGuiApplication.platformName() == "offscreen"
    screen = QGuiApplication.primaryScreen().geometry()
    position = screen.topLeft() + QPoint(20, 20)
    # Native window bounds can touch the display through the shadow alone.
    if case == "shadow-only":
        position = QPoint(screen.right() - 2, screen.top())
    elif case == "offscreen":
        position = QPoint(screen.right() + 500, screen.bottom() + 500)
    view = Mock()
    view.isVisible.return_value = case != "hidden"
    view.windowState.return_value = Qt.WindowMinimized if case == "minimized" else Qt.WindowNoState
    view.position.return_value = position
    view.isExposed.return_value = case != "unexposed"
    owner = SimpleNamespace(
        _closed=False, _view=view, _input_region=QRegion(QRect(10, 10, 250, 48))
    )
    assert cast(Any, MeetingOverlay.indicator_visible).fget(owner) is (case == "shown")


@pytest.mark.parametrize("reason,fragment", [("indicators", "индикатор"), ("lease", "не отвечала")])
def test_recovery_reason_visible_eight_seconds_without_republication_extension(
    rig: Rig, reason: str, fragment: str
) -> None:
    start(rig)
    rig.recording.controller.indicator_recovery = {
        "capture_id": "capture1",
        "state_rev": 4,
        "reason": reason,
        "duration_ms": 14000,
    }
    rig.recording.tick()
    assert fragment in rig.recording.overlay["secondary"]
    assert "0:00:14" in rig.recording.overlay["secondary"]
    rig.now += 7.99
    rig.recording.tick()
    assert fragment in rig.recording.overlay["secondary"]
    rig.now += 0.02
    rig.recording.tick()
    assert fragment not in rig.recording.overlay["secondary"]


def test_recording_shutdown_stops_timers_and_late_guard_callbacks(rig: Rig) -> None:
    r = rig.recording
    shown = [True]
    r.bind_indicators(overlay_visible=lambda: shown[0], tray_visible=lambda: False)
    r.initialize()
    start(rig)
    timers = r.findChildren(QTimer)
    assert timers and any(t.isActive() for t in timers)
    r.shutdown()
    assert not any(t.isActive() for t in timers)
    before = len(rig.supervisor.messages)
    shown[0] = False
    rig.now += 100
    r.tick()
    assert len(rig.supervisor.messages) == before
