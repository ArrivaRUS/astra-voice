"""Запуск и завершение GUI с подменой дисплея, воркера и файловых ресурсов."""

from __future__ import annotations

import logging
import signal
from pathlib import Path
from unittest.mock import Mock, call

import pytest
from PyQt5 import QtCore, QtWidgets

from astra_voice import app as app_mod
from astra_voice import runtime as runtime_mod
from astra_voice.core import policy as policy_mod
from astra_voice.core import settings as settings_mod
from astra_voice.core.policy import Policy
from astra_voice.core.settings import Settings
from astra_voice.platform.session import SessionKind
from astra_voice.runtime import DictationRuntime

pytestmark = pytest.mark.unit


class Rig:
    """Подменённые ресурсы main() и общий журнал порядка освобождения."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        self.calls = Mock()
        self.app = Mock()
        self.app.exec_.return_value = 7
        self.lock = Mock()
        self.lock.tryLock.return_value = True
        self.server = Mock()
        self.timer = Mock()
        self.theme = Mock()
        self.shell = Mock()
        self.show = Mock()
        self.cleanup = Mock()
        self.runtime = Mock()
        self.runtime.on_quit_requested = None
        self.runtime.tray.on_quit = lambda: DictationRuntime._quit_requested(self.runtime)
        self.factory = Mock(return_value=self.runtime)
        self.signals = Mock()
        self.settings = Settings(hotkey="Alt+Space")
        self.calls.attach_mock(self.runtime.start, "start")
        self.calls.attach_mock(self.app.exec_, "exec")
        self.calls.attach_mock(self.app.quit, "quit")
        self.calls.attach_mock(self.runtime.shutdown, "shutdown")
        self.calls.attach_mock(self.timer.stop, "timer_stop")
        self.calls.attach_mock(self.theme.source.stop, "theme_stop")
        self.calls.attach_mock(self.cleanup, "cleanup")

        monkeypatch.setattr(QtCore, "QLockFile", Mock(return_value=self.lock))
        monkeypatch.setattr(QtCore, "QTimer", Mock(return_value=self.timer))
        monkeypatch.setattr(QtWidgets, "QApplication", Mock(return_value=self.app))
        monkeypatch.setattr(signal, "signal", self.signals)
        monkeypatch.setattr(app_mod, "lock_path", lambda: tmp_path / "lock")
        monkeypatch.setattr(app_mod, "detect", lambda: SessionKind.KDE)
        monkeypatch.setattr(app_mod, "setup_logging", Mock())
        monkeypatch.setattr(app_mod, "_install_qt_message_handler", Mock())
        monkeypatch.setattr(policy_mod, "load", lambda: Policy())
        monkeypatch.setattr(settings_mod, "load", lambda: self.settings)
        monkeypatch.setattr(app_mod, "_ensure_settings_file", Mock())
        monkeypatch.setattr(app_mod, "_make_app_info", Mock())
        monkeypatch.setattr(app_mod, "_make_theme_bridge", Mock(return_value=self.theme))
        monkeypatch.setattr(app_mod, "_load_qml", Mock(return_value=self.shell))
        monkeypatch.setattr(app_mod, "_wire_close", Mock())
        monkeypatch.setattr(app_mod, "ShowServer", Mock(return_value=self.server))
        monkeypatch.setattr(app_mod, "_show", self.show)
        monkeypatch.setattr(app_mod, "_cleanup", self.cleanup)
        monkeypatch.setattr(runtime_mod, "DictationRuntime", self.factory)


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Rig:
    return Rig(monkeypatch, tmp_path)


def test_shutdown_once_before_other_cleanup(rig: Rig) -> None:
    assert app_mod.main([]) == 7

    rig.factory.assert_called_once_with(settings=rig.settings, session_kind=SessionKind.KDE)
    rig.app.setQuitOnLastWindowClosed.assert_called_once_with(False)
    assert rig.calls.mock_calls == [
        call.start(),
        call.exec(),
        call.shutdown(),
        call.timer_stop(),
        call.theme_stop(),
        call.cleanup(rig.server, rig.lock),
    ]


@pytest.mark.parametrize("failure", ["constructor", "start"])
def test_runtime_failure_keeps_event_loop_running(
    rig: Rig, caplog: pytest.LogCaptureFixture, failure: str
) -> None:
    failing = rig.factory if failure == "constructor" else rig.runtime.start
    failing.side_effect = RuntimeError("Ошибка запуска")

    with caplog.at_level(logging.WARNING, logger=app_mod.__name__):
        assert app_mod.main([]) == 7

    rig.app.exec_.assert_called_once_with()
    rig.show.assert_called_once_with(rig.shell)
    assert any(
        record.levelno == logging.WARNING and "без неё" in record.getMessage()
        for record in caplog.records
    )
    if failure == "constructor":
        rig.runtime.shutdown.assert_not_called()
    else:
        rig.runtime.shutdown.assert_called_once_with()
        assert rig.calls.mock_calls.index(call.shutdown()) < rig.calls.mock_calls.index(
            call.cleanup(rig.server, rig.lock)
        )
    rig.cleanup.assert_called_once_with(rig.server, rig.lock)


def test_tray_quit_calls_app_quit_and_shuts_down(rig: Rig) -> None:
    def exec_loop() -> int:
        rig.runtime.tray.on_quit()
        return 0

    rig.app.exec_.side_effect = exec_loop
    assert app_mod.main([]) == 0

    rig.app.quit.assert_called_once_with()
    rig.runtime.shutdown.assert_called_once_with()
    assert rig.calls.mock_calls.index(call.quit()) < rig.calls.mock_calls.index(call.shutdown())


@pytest.mark.parametrize("action", ["on_settings", "on_about"])
def test_tray_opens_main_window(rig: Rig, action: str) -> None:
    def exec_loop() -> int:
        getattr(rig.runtime.tray, action)()
        return 0

    rig.app.exec_.side_effect = exec_loop
    assert app_mod.main(["--hidden"]) == 0
    rig.show.assert_called_once_with(rig.shell)


def test_pill_details_opens_main_window(rig: Rig) -> None:
    def exec_loop() -> int:
        rig.runtime.pill.on_details_clicked()
        return 0

    rig.app.exec_.side_effect = exec_loop
    assert app_mod.main(["--hidden"]) == 0
    rig.show.assert_called_once_with(rig.shell)


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT])
def test_signal_uses_same_shutdown_path(rig: Rig, signum: signal.Signals) -> None:
    def exec_loop() -> int:
        handlers = dict(item.args for item in rig.signals.call_args_list)
        handlers[signum](signum, None)
        return 0

    rig.app.exec_.side_effect = exec_loop
    assert app_mod.main([]) == 0

    rig.app.quit.assert_called_once_with()
    rig.runtime.shutdown.assert_called_once_with()
    rig.cleanup.assert_called_once_with(rig.server, rig.lock)
    assert rig.calls.mock_calls.index(call.quit()) < rig.calls.mock_calls.index(call.shutdown())
    assert rig.calls.mock_calls.index(call.shutdown()) < rig.calls.mock_calls.index(
        call.cleanup(rig.server, rig.lock)
    )


def test_event_loop_failure_still_shuts_down(rig: Rig) -> None:
    rig.app.exec_.side_effect = RuntimeError("Ошибка цикла событий")

    with pytest.raises(RuntimeError, match="Ошибка цикла событий"):
        app_mod.main([])

    rig.runtime.shutdown.assert_called_once_with()
    rig.cleanup.assert_called_once_with(rig.server, rig.lock)


def test_shutdown_failure_does_not_skip_cleanup(rig: Rig) -> None:
    rig.runtime.shutdown.side_effect = RuntimeError("Ошибка завершения")

    assert app_mod.main([]) == 7

    rig.runtime.shutdown.assert_called_once_with()
    rig.timer.stop.assert_called_once_with()
    rig.theme.source.stop.assert_called_once_with()
    rig.cleanup.assert_called_once_with(rig.server, rig.lock)
