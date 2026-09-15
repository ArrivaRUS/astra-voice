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
from astra_voice.core import paths as paths_mod
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
        self.shell.rootObjects.return_value = [Mock()]
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
        monkeypatch.setattr(paths_mod, "settings_path", lambda: tmp_path / "settings.json")
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


@pytest.mark.parametrize(
    "policy",
    [
        Policy(),
        Policy(
            values={"hotkey": "Ctrl+Shift+Space", "hotkey_mode": "toggle", "device": "admin mic"},
            locked_keys=frozenset({"language"}),
            status=policy_mod.PolicyStatus.OK,
        ),
    ],
)
def test_settings_bridge_receives_stored_runtime_mirror_and_policy_values(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, policy: Policy
) -> None:
    from astra_voice.ui import bridges

    bridge_factory = Mock(wraps=bridges.SettingsBridge)
    monkeypatch.setattr(bridges, "SettingsBridge", bridge_factory)
    monkeypatch.setattr(policy_mod, "load", lambda: policy)

    assert app_mod.main([]) == 7

    bridge_factory.assert_called_once()
    args, kwargs = bridge_factory.call_args
    assert len(args) == 1
    assert args[0] is rig.settings
    runtime_settings = rig.factory.call_args.kwargs["settings"]
    assert runtime_settings is not rig.settings
    assert kwargs["mirror"] is runtime_settings
    assert set(kwargs["locked"]) == set(policy.values)
    assert isinstance(kwargs["apply"], app_mod._RuntimeSettingsApply)
    context = rig.shell.rootContext()
    properties = dict(item.args for item in context.setContextProperty.call_args_list)
    bridge = properties["settingsBridge"]
    assert bridge.lockedSettings == sorted(policy.values)
    assert bridge.device == (runtime_settings.extra.get("device") or "")
    assert bridge.hotkey == runtime_settings.hotkey


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


@pytest.mark.parametrize("done", [None, False, 1, True])
@pytest.mark.parametrize("runtime_available", [False, True])
def test_onboarding_context(rig: Rig, done: int | None, runtime_available: bool) -> None:
    from PyQt5.QtQml import QQmlEngine

    from astra_voice.ui.bridges import OnboardingController

    rig.settings.extra["onboarding_done"] = done
    rig.settings.extra["onboarding_step"] = 3
    if not runtime_available:
        rig.runtime.start.side_effect = RuntimeError()
    assert app_mod.main([]) == 7
    properties = dict(
        item.args for item in rig.shell.rootContext().setContextProperty.call_args_list
    )
    assert properties["showOnboarding"] is (done is not True)
    assert "settingsBridge" in properties
    if done is True:
        assert "onboarding" not in properties
    else:
        controller = properties["onboarding"]
        assert isinstance(controller, OnboardingController)
        assert QQmlEngine.objectOwnership(controller) == QQmlEngine.CppOwnership
        assert controller.step == 3
        controller.beginCapture()
        assert controller.captureState == ("capturing" if runtime_available else "not-grabbed")
        controller.cancelCapture()


def test_onboarding_host_delegates_and_hides_root(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    from astra_voice.ui import notify

    notification = Mock()
    monkeypatch.setattr(notify, "notify_onboarding_ready", notification)
    root = Mock()
    rig.shell.rootObjects.return_value = [root]
    host = app_mod._RuntimeOnboardingHost(rig.runtime, rig.shell)
    rig.runtime.begin_hotkey_capture.return_value = True
    rig.runtime.hotkey.probe.return_value.code = "busy"
    rig.runtime.hotkey.free_candidates.return_value = ["Ctrl+Alt+D"]
    rig.runtime.apply_hotkey.return_value = "ok"
    assert host.begin_capture() is True
    host.end_capture()
    assert host.probe("Ctrl+Space") == "busy"
    assert host.free_candidates(["Ctrl+Alt+D"]) == ["Ctrl+Alt+D"]
    assert host.apply_hotkey("Ctrl+Alt+D", "toggle") == "ok"
    host.notify_ready("Ctrl+Alt+D")
    host.hide_window()
    rig.runtime.begin_hotkey_capture.assert_called_once_with()
    rig.runtime.end_hotkey_capture.assert_called_once_with()
    rig.runtime.hotkey.probe.assert_called_once_with("Ctrl+Space")
    rig.runtime.hotkey.free_candidates.assert_called_once_with(["Ctrl+Alt+D"])
    rig.runtime.apply_hotkey.assert_called_once_with("Ctrl+Alt+D", "toggle")
    notification.assert_called_once_with("Ctrl+Alt+D")
    root.hide.assert_called_once_with()
    rig.shell.rootObjects.return_value = []
    host.hide_window()


def test_finishing_onboarding_updates_context(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    from astra_voice.ui import bridges, notify

    monkeypatch.setattr(notify, "notify_onboarding_ready", Mock())
    model = Mock(
        spec_set=bridges.ModelPort,
        recommended=Mock(return_value=None),
        installed_ok=Mock(return_value=True),
        broken=Mock(return_value=False),
        allowed=Mock(return_value=(False, "Сеть отключена в тесте")),
        disk_ok=Mock(return_value=True),
        ram_ok=Mock(return_value=True),
        download=Mock(side_effect=AssertionError("Неожиданная загрузка модели")),
        install_from_staging=Mock(side_effect=AssertionError("Неожиданная установка модели")),
        install_from_path=Mock(side_effect=AssertionError("Неожиданная установка модели")),
    )
    monkeypatch.setattr(bridges, "ModelService", Mock(return_value=model))

    def exec_loop() -> int:
        properties = dict(
            item.args for item in rig.shell.rootContext().setContextProperty.call_args_list
        )
        properties["onboarding"].finish()
        return 0

    rig.app.exec_.side_effect = exec_loop
    assert app_mod.main([]) == 0
    model.installed_ok.assert_called_once_with()
    assert rig.settings.extra["onboarding_done"] is True
    rig.shell.rootContext().setContextProperty.assert_called_with("showOnboarding", False)
