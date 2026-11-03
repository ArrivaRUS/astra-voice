"""W2/tray consume published capture state and preserve click-only admission."""

from __future__ import annotations

from unittest.mock import Mock

import pytest
from PyQt5.QtTest import QSignalSpy
from test_meeting_recording_bridge import Rig
from test_meeting_recording_bridge import rig as rig

from astra_voice import app
from astra_voice.core.command_mode import SessionSnapshot
from astra_voice.platform.session import SessionKind
from astra_voice.ui.meeting_recording import MeetingRecording
from astra_voice.ui.overlay import WINDOW_FLAGS
from astra_voice.ui.tray_icons import TrayState

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("filesystem_euid")]


def start(rig: Rig, *, mid: str | None = None) -> None:
    rig.bridge.requestRecording()
    rig.bridge.on_start_clicked()
    fields = {"meeting_id": mid} if mid else {}
    rig.reply("prepared", **fields)
    rig.reply("started", **fields)
    assert rig.bridge.overlay["state"] == "recording"


def finish(rig: Rig) -> None:
    rig.bridge.on_overlay_stop_clicked()
    rig.reply("closed", tracks=["mic"], state_rev=2)
    rig.reply(
        "finalized",
        segments={"mic": 0, "far": 0},
        intervals=[],
        status="succeeded",
        state_rev=2,
    )
    role = rig.supervisor
    role.ready, role.exited = False, True
    role.on_exit(0)
    rig.recording.tick()


@pytest.mark.parametrize(
    ("enabled", "allowed"), [(False, False), (False, True), (True, False), (True, True)]
)
def test_overlay_actions_are_inert_without_a_committed_capture(
    enabled: bool, allowed: bool
) -> None:
    from helpers.qt_app import get_qapplication

    get_qapplication()
    role = Mock(generation=1)
    recording = MeetingRecording(
        Mock(),
        enabled=lambda: enabled,
        policy_allowed=lambda: allowed,
        session_kind=SessionKind.KDE,
        supervisor_factory=lambda *_: role,
        session_factory=Mock(),
        audio_probe=Mock(),
        storage_probe=Mock(),
    )
    try:
        assert recording.overlay["visible"] is False
        for action in ("pause", "resume", "stop", "participants", "extend"):
            recording.overlay_action(action)
        role.start.assert_not_called()
        role.send.assert_not_called()
    finally:
        recording.shutdown()


def test_tray_record_opens_w4_and_reveals_shell_without_capture(rig: Rig) -> None:
    tray, show = Mock(), Mock()
    disconnect = app._wire_tray_meetings(tray, rig.bridge, show)
    try:
        tray.on_meeting_record()
        assert rig.bridge.startDialogOpen
        show.assert_called_once_with()
        assert not any(
            message["type"].endswith(("prepare", "open")) for message in rig.supervisor.messages
        )
    finally:
        disconnect()


def test_disabled_pill_cannot_hide_w2_or_recording_tray(rig: Rig) -> None:
    tray = Mock()
    disconnect = app._wire_tray_meetings(tray, rig.bridge, Mock())
    try:
        rig.bridge._stored.pill_enabled = False
        start(rig)
        assert rig.bridge.overlay["visible"] and rig.bridge.overlay["active"]
        assert tray.set_meeting_state.call_args.args == (TrayState.MEETING_REC,)
        assert rig.bridge.overlay["elapsed"] == "0:00:00"
        rig.now += 125
        rig.recording.tick()
        assert rig.bridge.overlay["elapsed"] == "0:02:05"
        rig.bridge.on_overlay_pause_clicked()
        assert rig.bridge.overlay["state"] == "paused"
        assert tray.set_meeting_state.call_args.args == (TrayState.MEETING_PAUSED,)
        rig.reply("closed", tracks=["mic"], state_rev=2)
        rig.reply(
            "finalized",
            segments={"mic": 0, "far": 0},
            intervals=[],
            status="succeeded",
            state_rev=2,
        )
        clock = rig.bridge.overlay["elapsed"]
        rig.now += 15
        rig.recording.tick()
        assert rig.bridge.overlay["elapsed"] == clock
    finally:
        disconnect()


def test_stop_and_participant_signal_remain_available_during_pause(rig: Rig) -> None:
    start(rig)
    notice = QSignalSpy(rig.recording.participantsRequested)
    rig.bridge.on_overlay_pause_clicked()
    assert rig.supervisor.messages[-1]["type"] == "meeting.capture.pause"
    rig.bridge.on_overlay_participants_clicked()
    assert len(notice) == 1
    rig.bridge.on_overlay_stop_clicked()
    assert rig.supervisor.messages[-1]["type"] == "meeting.capture.stop"
    assert rig.bridge.overlay["state"] == "stopping"
    assert rig.bridge.overlay["visible"]


def test_paused_lock_never_resumes_from_overlay_refresh_or_unlock(rig: Rig) -> None:
    start(rig)
    rig.session.value = SessionSnapshot(known=True, locked=True, blocked_epoch=3)
    rig.session.changed.emit(rig.session.value)
    assert rig.bridge.overlay["state"] == "paused-lock"
    assert rig.bridge.overlay["secondary"] == "Экран был заблокирован — продолжите вручную"
    assert not rig.bridge.overlay["canResume"]
    rig.session.value = SessionSnapshot(known=True, locked=False, blocked_epoch=3)
    rig.session.changed.emit(rig.session.value)
    rig.recording.tick()
    assert rig.bridge.overlay["state"] == "paused-lock"
    assert not any(m["type"] == "meeting.capture.resume" for m in rig.supervisor.messages)


@pytest.mark.parametrize("reason", ["indicators", "lease"])
def test_indicator_or_lease_pause_shows_plain_spec_paused_state(rig: Rig, reason: str) -> None:
    # spec §3.5 has no own W2 row for these reasons; a visible W2 shows plain «Пауза».
    start(rig)
    controller = rig.recording.controller
    controller.state = "paused"
    controller.pause_reasons = {reason}
    view = rig.recording.overlay
    assert view["state"] == "paused"
    assert view["secondary"] == ""


def test_limit_warning_extend_and_four_hour_ceiling(rig: Rig) -> None:
    start(rig)
    rig.now += 6900
    rig.recording.tick()
    view = rig.bridge.overlay
    assert view["state"] == "limit-soon" and view["canExtend"]
    assert view["secondary"] == "Остановится через 5:00"
    rig.bridge.on_overlay_extend_clicked()
    assert rig.supervisor.messages[-1]["type"] == "meeting.capture.extend"
    assert rig.supervisor.messages[-1]["max_ms"] == 10800000
    rig.now += 3600
    rig.recording.tick()
    rig.bridge.on_overlay_extend_clicked()
    assert rig.supervisor.messages[-1]["max_ms"] == 14400000
    rig.now += 3600
    rig.recording.tick()
    assert rig.bridge.overlay["state"] == "limit-soon"
    assert not rig.bridge.overlay["canExtend"]
    before = len(rig.supervisor.messages)
    rig.bridge.on_overlay_extend_clicked()
    assert len(rig.supervisor.messages) == before


def test_published_microphone_loss_has_reason_but_no_invented_candidate(rig: Rig) -> None:
    start(rig)
    rig.reply("track", track="mic", state="lost", state_rev=2, kind="device-lost", at_recorded_ms=0)
    assert rig.bridge.overlay["state"] == "mic-paused"
    assert rig.bridge.overlay["secondary"] == "Ваш голос не записывается: микрофон сменился"
    assert rig.bridge.overlay["microphoneName"] == ""
    assert rig.bridge.overlay["secondaryAction"] == ""
    rig.bridge.on_overlay_stop_clicked()
    assert rig.supervisor.messages[-1]["type"] == "meeting.capture.stop"


@pytest.mark.parametrize("previous", ["far-ended", "audio-error"])
def test_transient_reason_cannot_cross_into_new_capture(rig: Rig, previous: str) -> None:
    next_id = "2026-11-01_120100-abce"
    ids = iter(["2026-11-01_120000-abcd", next_id])
    rig.recording.controller.id_factory = lambda: next(ids)
    start(rig)
    if previous == "far-ended":
        rig.reply(
            "track", track="far", state="live", state_rev=1, kind="reopened", at_recorded_ms=0
        )
        rig.reply(
            "track",
            track="far",
            state="ended",
            state_rev=2,
            kind="target-removed",
            at_recorded_ms=0,
        )
        assert rig.bridge.overlay["state"] == "app-closed"
        finish(rig)
    else:
        rig.recording.controller.handle_event(
            {
                "type": "error",
                "code": "audio-failed",
                "req": rig.supervisor.messages[-1]["req"],
                "request_type": rig.supervisor.messages[-1]["type"],
                "message": "meeting-command-failed",
                "meeting_id": "2026-11-01_120000-abcd",
                "meeting_generation": 0,
            }
        )
        rig.supervisor.ready, rig.supervisor.exited = False, True
        rig.supervisor.on_exit(1)
        rig.recording.tick()
        assert rig.bridge.overlay["state"] == "error"
        assert not rig.bridge.overlay["active"]
    start(rig, mid=next_id)
    assert rig.bridge.overlay["state"] == "recording"
    assert rig.bridge.overlay["secondary"] == ""
    assert rig.bridge.overlay["active"]


def test_mic_only_alive_far_ended_is_not_a_closed_call(rig: Rig) -> None:
    start(rig)
    rig.reply(
        "alive",
        seq=1,
        state="recording",
        recorded_ms=1000,
        tracks={"mic": "live", "far": "ended"},
        free_mb=1000,
        cpu_pct=0,
        pause_reasons=[],
        manual_resume_required=False,
        blocked_epoch=1,
        state_rev=1,
    )
    assert rig.bridge.overlay["state"] == "recording"
    assert rig.bridge.overlay["secondary"] == ""


@pytest.mark.parametrize("phase", ["locked", "unlocked-pending", "ready"])
def test_resume_admission_does_not_call_click_controller_while_locked(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    start(rig)
    rig.session.value = SessionSnapshot(known=True, locked=True, blocked_epoch=3)
    rig.session.changed.emit(rig.session.value)
    if phase != "locked":
        # The unlock is explicit; resuming remains a user action.
        rig.session.value = SessionSnapshot(known=True, locked=False, blocked_epoch=3)
        rig.session.changed.emit(rig.session.value)
    if phase == "ready":
        rig.reply(
            "paused",
            state_rev=2,
            blocked_epoch=3,
            pause_reasons=["lock"],
            manual_resume_required=True,
            at_recorded_ms=0,
            segments_closed=0,
        )
        rig.reply("closed", tracks=["mic"], state_rev=2)
        rig.reply(
            "finalized",
            segments={"mic": 0, "far": 0},
            intervals=[],
            status="succeeded",
            state_rev=2,
        )
    assert rig.bridge.overlay["state"] == "paused-lock"
    assert rig.bridge.overlay["canResume"] is (phase == "ready")
    resume = Mock(wraps=rig.recording.clicks.on_resume_clicked)
    monkeypatch.setattr(rig.recording.clicks, "on_resume_clicked", resume)
    rig.recording.overlay_action("resume")
    if phase != "ready":
        resume.assert_not_called()
    else:
        resume.assert_called_once_with()
        assert rig.supervisor.messages[-1]["type"] == "meeting.capture.resume"


@pytest.mark.parametrize("dpr", [1, 2])
def test_x11_input_region_preserves_shadow_and_updates_surface_dimensions(
    monkeypatch: pytest.MonkeyPatch, dpr: int
) -> None:
    """Only the input shape changes; bounding shape must keep painting the shadow."""
    from PyQt5.QtCore import QObject
    from PyQt5.QtGui import QRegion
    from Xlib import X
    from Xlib.ext import shape

    from astra_voice.ui import meeting_overlay

    owner = meeting_overlay.MeetingOverlay.__new__(meeting_overlay.MeetingOverlay)
    QObject.__init__(owner)
    owner._closed = False
    owner._input_native_window_id = None
    dimensions = {"pillWidth": 311.25, "pillHeight": 73.1}
    owner._root = Mock()
    owner._root.property.side_effect = dimensions.__getitem__
    owner._view = Mock()
    owner._view.mask.return_value = QRegion()
    owner._view.devicePixelRatio.return_value = dpr
    owner._view.winId.return_value = 7
    connection = Mock()
    connection.has_extension.return_value = True
    window = connection.create_resource_object.return_value
    x11 = Mock(d=connection, _error_count=0)
    monkeypatch.setattr(owner, "_x11", x11, raising=False)
    monkeypatch.setattr(meeting_overlay, "QGuiApplication", Mock(platformName=lambda: "xcb"))
    for width, height in [(312, 74), (88, 100)]:
        owner._update_mask()
        assert owner._input_region == QRegion(18, 18, width, height)
        window.shape_rectangles.assert_called_with(
            shape.SO.Set,
            shape.SK.Input,
            X.Unsorted,
            0,
            0,
            [(18 * dpr, 18 * dpr, width * dpr, height * dpr)],
        )
        dimensions.update(pillWidth=87.5, pillHeight=100.0)
    assert window.shape_rectangles.call_count == 2
    assert all(call.args[1] == shape.SK.Input for call in window.shape_rectangles.call_args_list)
    owner._view.setMask.assert_not_called()
    x11.open.assert_not_called()
    # Recover a previous Qt Bounding fallback only after Input was confirmed.
    owner._view.mask.return_value = QRegion(18, 18, 312, 74)
    connection.sync.reset_mock()
    window.shape_rectangles.reset_mock()

    def clear_fallback(region: QRegion) -> None:
        assert region.isEmpty()
        window.shape_rectangles.assert_called_once()
        connection.sync.assert_called_once()

    owner._view.setMask.side_effect = clear_fallback
    owner._update_mask()
    owner._view.setMask.assert_called_once_with(QRegion())
    assert owner._input_region == QRegion(18, 18, 88, 100)
    assert window.shape_rectangles.call_args.args[1] == shape.SK.Input


@pytest.mark.parametrize("failure", ["no-shape", "request-exception", "server-error"])
def test_x11_input_shape_failure_keeps_body_mask_fallback(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    from PyQt5.QtCore import QObject
    from PyQt5.QtGui import QRegion

    from astra_voice.ui import meeting_overlay

    owner = meeting_overlay.MeetingOverlay.__new__(meeting_overlay.MeetingOverlay)
    QObject.__init__(owner)
    owner._closed = False
    owner._input_native_window_id = None
    owner._root = Mock()
    owner._root.property.side_effect = {"pillWidth": 311.25, "pillHeight": 73.1}.__getitem__
    owner._view = Mock()
    owner._view.flags.return_value = WINDOW_FLAGS
    monkeypatch.setattr(owner, "_prepare_window", Mock())
    owner._view.devicePixelRatio.return_value = 1
    owner._view.winId.return_value = 7
    connection = Mock()
    connection.has_extension.return_value = failure != "no-shape"
    x11 = Mock(d=connection, _error_count=0)
    if failure == "request-exception":
        connection.create_resource_object.return_value.shape_rectangles.side_effect = RuntimeError()
    elif failure == "server-error":

        def server_error() -> None:
            x11._error_count += 1

        connection.sync.side_effect = server_error
    monkeypatch.setattr(owner, "_x11", x11, raising=False)
    monkeypatch.setattr(meeting_overlay, "QGuiApplication", Mock(platformName=lambda: "xcb"))
    owner._update_mask()
    owner._view.setMask.assert_called_once_with(QRegion(18, 18, 312, 74))


def test_native_input_failure_after_expansion_resets_stale_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from PyQt5.QtCore import QObject, QPoint, Qt
    from PyQt5.QtGui import QRegion

    from astra_voice.ui import meeting_overlay

    owner = meeting_overlay.MeetingOverlay.__new__(meeting_overlay.MeetingOverlay)
    QObject.__init__(owner)
    owner._closed = False
    owner._input_native_window_id = None
    dimensions = {"pillWidth": 88.0, "pillHeight": 36.0}
    owner._root = Mock()
    owner._root.property.side_effect = dimensions.__getitem__
    owner._view = Mock()
    owner._view.devicePixelRatio.return_value = 1
    owner._view.winId.return_value = 7
    original_flags = WINDOW_FLAGS
    current_flags = original_flags
    bounding = QRegion()
    explicit_input: QRegion | None = None
    operations: list[object] = []

    def set_bounding(region: QRegion) -> None:
        nonlocal bounding
        bounding = region
        operations.append(("bounding", region))

    def set_flags(flags: object) -> None:
        nonlocal explicit_input, current_flags
        operations.append(("flags", flags))
        current_flags = flags
        # Qt's transparent-input toggle clears the previous explicit X input shape.
        explicit_input = None

    def native_shape(*args: object) -> None:
        nonlocal explicit_input
        rectangles = args[-1]
        assert isinstance(rectangles, list)
        explicit_input = QRegion(*rectangles[0])

    owner._view.setMask.side_effect = set_bounding
    owner._view.mask.side_effect = lambda: bounding
    owner._view.flags.side_effect = lambda: current_flags
    owner._view.setFlags.side_effect = set_flags
    connection = Mock()
    connection.has_extension.return_value = True
    connection.create_resource_object.return_value.shape_rectangles.side_effect = native_shape
    x11 = Mock(d=connection, _error_count=0)
    monkeypatch.setattr(owner, "_x11", x11, raising=False)
    monkeypatch.setattr(meeting_overlay, "QGuiApplication", Mock(platformName=lambda: "xcb"))
    monkeypatch.setattr(owner, "_prepare_window", lambda: operations.append("metadata"))
    owner._update_mask()
    assert explicit_input == QRegion(18, 18, 88, 36)
    connection.create_resource_object.return_value.shape_rectangles.side_effect = RuntimeError()
    for width in (482, 540):
        operations.clear()
        dimensions["pillWidth"] = float(width)
        owner._update_mask()
        point = QPoint(18 + width - 1, 18 + 35)
        effective_input = (
            bounding if explicit_input is None else bounding.intersected(explicit_input)
        )
        assert effective_input.contains(point), (
            "expanded controls remain outside stale native Input"
        )
        assert operations == [
            ("bounding", QRegion(18, 18, width, 36)),
            ("flags", original_flags | Qt.WindowTransparentForInput),
            ("flags", original_flags),
            "metadata",
        ]
        assert current_flags == original_flags
        assert owner._input_region == QRegion(18, 18, width, 36)


def test_native_input_without_xfixes_uses_qt_fallback_from_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from PyQt5.QtCore import QObject
    from PyQt5.QtGui import QRegion

    from astra_voice.ui import meeting_overlay

    owner = meeting_overlay.MeetingOverlay.__new__(meeting_overlay.MeetingOverlay)
    QObject.__init__(owner)
    owner._closed = False
    owner._input_native_window_id = None
    owner._root = Mock()
    owner._root.property.side_effect = {"pillWidth": 482.0, "pillHeight": 36.0}.__getitem__
    owner._view = Mock()
    owner._view.mask.return_value = QRegion()
    owner._view.flags.return_value = WINDOW_FLAGS
    owner._view.devicePixelRatio.return_value = 1
    owner._view.winId.return_value = 7
    connection = Mock()
    connection.has_extension.side_effect = lambda name: name == "SHAPE"
    monkeypatch.setattr(owner, "_x11", Mock(d=connection, _error_count=0), raising=False)
    monkeypatch.setattr(meeting_overlay, "QGuiApplication", Mock(platformName=lambda: "xcb"))
    monkeypatch.setattr(owner, "_prepare_window", Mock())
    owner._update_mask()
    connection.create_resource_object.return_value.shape_rectangles.assert_not_called()
    owner._view.setMask.assert_called_once_with(QRegion(18, 18, 482, 36))
