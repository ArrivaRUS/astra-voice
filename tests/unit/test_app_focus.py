"""Показ и фокус главного окна без запуска GUI."""

from __future__ import annotations

from unittest.mock import Mock, call

import pytest
from PyQt5 import QtCore

from astra_voice import app
from astra_voice.platform import x11

pytestmark = pytest.mark.unit


def _window(states: QtCore.Qt.WindowStates, visible: bool = True) -> Mock:
    root = Mock(
        spec=[
            "windowStates",
            "setWindowStates",
            "isVisible",
            "setVisible",
            "showNormal",
            "raise_",
            "requestActivate",
            "winId",
        ]
    )
    root.windowStates.return_value = states
    root.isVisible.return_value = visible
    root.winId.return_value = 42
    return root


def _focuser(monkeypatch: pytest.MonkeyPatch, root: Mock) -> tuple[app._WindowFocuser, Mock]:
    timer = Mock()
    monkeypatch.setattr(QtCore, "QTimer", Mock(return_value=timer))
    focuser = app._WindowFocuser(root)
    timer.timeout.connect.assert_called_once_with(focuser._activate)
    return focuser, timer


def test_visible_maximized_window_keeps_state(monkeypatch: pytest.MonkeyPatch) -> None:
    root = _window(QtCore.Qt.WindowMaximized)
    focuser, timer = _focuser(monkeypatch, root)
    focuser.focus_shell()
    timer.start.assert_called_once_with()
    root.raise_.assert_called_once_with()
    root.showNormal.assert_not_called()
    root.setWindowStates.assert_not_called()
    root.setVisible.assert_not_called()


def test_minimized_maximized_window_restores_maximized(monkeypatch: pytest.MonkeyPatch) -> None:
    root = _window(QtCore.Qt.WindowMinimized | QtCore.Qt.WindowMaximized)
    focuser, _ = _focuser(monkeypatch, root)
    focuser.focus_shell()
    root.setWindowStates.assert_called_once_with(QtCore.Qt.WindowMaximized)
    root.showNormal.assert_not_called()


def test_hidden_window_is_shown(monkeypatch: pytest.MonkeyPatch) -> None:
    root = _window(QtCore.Qt.WindowMaximized, visible=False)
    focuser, _ = _focuser(monkeypatch, root)
    focuser.focus_shell()
    root.setVisible.assert_called_once_with(True)
    root.setWindowStates.assert_not_called()
    root.showNormal.assert_not_called()


@pytest.mark.parametrize("sent", [True, False])
def test_focus_requests_qt_activation_after_x11(
    monkeypatch: pytest.MonkeyPatch, sent: bool
) -> None:
    sequence = Mock()
    root = _window(QtCore.Qt.WindowMaximized)
    sequence.attach_mock(root.raise_, "raise_window")
    sequence.attach_mock(root.requestActivate, "qt_activate")
    focus_window = Mock(return_value=sent)
    sequence.attach_mock(focus_window, "x11_focus")
    monkeypatch.setattr(x11, "focus_window", focus_window)
    focuser, timer = _focuser(monkeypatch, root)

    focuser.focus_shell()
    timer.start.assert_called_once_with()
    assert sequence.mock_calls == [call.raise_window()]
    focuser._activate()
    assert sequence.mock_calls == [call.raise_window(), call.x11_focus(42), call.qt_activate()]


def test_focus_shell_timestamp_is_set_before_activation(monkeypatch: pytest.MonkeyPatch) -> None:
    root = _window(QtCore.Qt.WindowNoState)
    sequence = Mock()
    user_time = Mock(return_value=True)
    focus_window = Mock(return_value=True)
    sequence.attach_mock(user_time, "user_time")
    sequence.attach_mock(focus_window, "x11_focus")
    sequence.attach_mock(root.requestActivate, "qt_activate")
    monkeypatch.setattr(x11, "set_user_time", user_time)
    monkeypatch.setattr(x11, "focus_window", focus_window)
    focuser, _ = _focuser(monkeypatch, root)

    focuser.focus_shell(1234)
    focuser._activate()
    assert sequence.mock_calls == [
        call.user_time(42, 1234),
        call.x11_focus(42),
        call.qt_activate(),
    ]


def test_zero_native_id_still_requests_qt_activation(monkeypatch: pytest.MonkeyPatch) -> None:
    root = _window(QtCore.Qt.WindowNoState)
    root.winId.return_value = 0
    focus_window = Mock()
    monkeypatch.setattr(x11, "focus_window", focus_window)
    focuser, timer = _focuser(monkeypatch, root)

    focuser.focus_shell()
    focuser._activate()
    timer.start.assert_called_once_with()
    focus_window.assert_not_called()
    root.requestActivate.assert_called_once_with()


def test_stop_cancels_pending_focus_and_prevents_new_focus(monkeypatch: pytest.MonkeyPatch) -> None:
    root = _window(QtCore.Qt.WindowNoState)
    focuser, timer = _focuser(monkeypatch, root)
    focuser.focus_shell()
    focuser.stop()
    timer.stop.assert_called_once_with()
    assert focuser._root is None
    root.reset_mock()
    timer.start.reset_mock()

    focuser.focus_shell()
    focuser.focus(root)
    focuser._activate()
    assert root.mock_calls == []
    timer.start.assert_not_called()
