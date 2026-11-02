"""T-478: tray «Выход» asks only during a recording; signals never ask."""

from __future__ import annotations

import signal
from pathlib import Path
from unittest.mock import Mock

import pytest
from PyQt5.QtTest import QSignalSpy
from test_meeting_notice import _GracefulRole
from test_meeting_overlay import finish, start
from test_meeting_recording_bridge import MID, Rig
from test_meeting_recording_bridge import rig as rig

from astra_voice import app
from astra_voice.meetings.events import MeetingState
from astra_voice.meetings.model import Meeting

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("filesystem_euid")]


def test_exit_without_recording_quits_without_question(rig: Rig) -> None:
    quit_app = Mock()
    rig.bridge.request_quit(quit_app)
    quit_app.assert_called_once_with()
    assert not rig.bridge.exitConfirmOpen


def test_cancel_keeps_recording_and_program(rig: Rig) -> None:
    start(rig)
    quit_app = Mock()
    confirmed = QSignalSpy(rig.bridge.exitConfirmed)
    rig.bridge.request_quit(quit_app)
    rig.bridge.request_quit(quit_app)
    assert rig.bridge.exitConfirmOpen
    quit_app.assert_not_called()
    # The dialog does not block W2/tray control.
    rig.bridge.on_overlay_pause_clicked()
    assert rig.bridge.overlay["state"] == "paused"
    rig.bridge.cancelExit()
    assert not rig.bridge.exitConfirmOpen and len(confirmed) == 0
    assert not any(m["type"] == "meeting.capture.stop" for m in rig.supervisor.messages)
    assert rig.bridge.overlay["active"]


def test_recording_end_closes_question_and_next_exit_is_ordinary(rig: Rig) -> None:
    start(rig)
    quit_app = Mock()
    rig.bridge.request_quit(quit_app)
    assert rig.bridge.exitConfirmOpen
    finish(rig)
    assert not rig.bridge.exitConfirmOpen
    rig.bridge.confirmExit()
    quit_app.assert_not_called()
    rig.bridge.request_quit(quit_app)
    quit_app.assert_called_once_with()


def test_confirm_quits_and_exit_stops_and_finalizes(tmp_path: Path) -> None:
    from helpers.qt_app import get_qapplication

    get_qapplication()
    rig = Rig(tmp_path, role_type=_GracefulRole)
    rig.recording._exit_stop_timeout = 3.0
    role = rig.supervisor
    assert isinstance(role, _GracefulRole)
    role.rig = rig
    start(rig)
    quits: list[str] = []
    # app.quit() leads to the `finally` cleanup, i.e. MeetingBridge.shutdown().
    rig.bridge.exitConfirmed.connect(lambda: quits.append("quit"))
    rig.bridge.request_quit(lambda: quits.append("direct"))
    assert quits == [] and rig.bridge.exitConfirmOpen
    rig.bridge.confirmExit()
    assert quits == ["quit"] and not rig.bridge.exitConfirmOpen
    rig.bridge.shutdown()
    kinds = [message["type"] for message in role.messages]
    assert kinds.index("meeting.capture.stop") < kinds.index("meeting.capture.shutdown")
    saved = rig.store.read(MID, "meeting.json")
    assert isinstance(saved, Meeting) and saved.state == MeetingState.RECORDED


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT])
def test_signals_quit_directly_without_question(signum: signal.Signals) -> None:
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
    qt_app = Mock()
    try:
        timer = app._install_signal_handlers(qt_app)
        handler = signal.getsignal(signum)
        assert callable(handler)
        handler(signum, None)
        qt_app.quit.assert_called_once_with()
        timer.stop()
    finally:
        for sig, value in previous.items():
            signal.signal(sig, value)


def test_tray_quit_is_routed_through_the_meeting_question() -> None:
    source = Path(app.__file__).read_text(encoding="utf-8")
    assert "runtime.on_quit_requested = lambda: bound_bridge.request_quit(app.quit)" in source
    assert "meeting_bridge.exitConfirmed.connect(app.quit)" in source


def test_repeated_exit_raises_the_open_question_again(rig: Rig) -> None:
    """Review 3/3: the window with the question was closed; «Выход» must show it again."""
    start(rig)
    changed = QSignalSpy(rig.bridge.changed)
    quit_app = Mock()
    rig.bridge.request_quit(quit_app)
    first = len(changed)
    rig.bridge.request_quit(quit_app)
    assert len(changed) > first and rig.bridge.exitConfirmOpen
    quit_app.assert_not_called()
