"""Нет трея: настоящее Qt-меню, фейки значка, D-Bus, уведомления и времени.

xvfb не установлен; запуск: QT_QPA_PLATFORM=offscreen QT_QUICK_BACKEND=software
pytest -m xvfb. Урок .patches/002: offscreen не изолирует сессионную шину.
Здесь реальный QSystemTrayIcon запрещён, подключение и наблюдатель D-Bus подменены
до создания Tray; виртуальные 30 секунд не требуют реального ожидания.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import Mock

import pytest

pytestmark = pytest.mark.xvfb


class Signal:
    def __init__(self) -> None:
        self.callbacks: list[Callable[..., None]] = []

    def connect(self, callback: Callable[..., None]) -> None:
        self.callbacks.append(callback)

    def emit(self, *args: object) -> None:
        for callback in self.callbacks:
            callback(*args)


@dataclass
class Clock:
    now: int = 0
    timers: list[Timer] = field(default_factory=list)

    def timer(self, parent: object) -> Timer:
        timer = Timer(self)
        self.timers.append(timer)
        return timer

    def advance(self, milliseconds: int) -> None:
        end = self.now + milliseconds
        while pending := [t for t in self.timers if t.due is not None and t.due <= end]:
            timer = min(pending, key=lambda t: t.due if t.due is not None else end)
            assert timer.due is not None
            self.now = timer.due
            timer.due = None if timer.single_shot else self.now + timer.interval
            timer.timeout.emit()
        self.now = end


class Timer:
    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.timeout = Signal()
        self.single_shot = False
        self.interval = 0
        self.due: int | None = None

    def setSingleShot(self, value: bool) -> None:
        self.single_shot = value

    def setTimerType(self, value: object) -> None:
        pass

    def setInterval(self, value: int) -> None:
        self.interval = value

    def start(self, milliseconds: int | None = None) -> None:
        if milliseconds is not None:
            self.interval = milliseconds
        assert self.interval > 0
        self.due = self.clock.now + self.interval

    def stop(self) -> None:
        self.due = None


@pytest.fixture(scope="module")
def tray_app() -> Any:
    pytest.importorskip("PyQt5.QtWidgets")
    from PyQt5.QtWidgets import QApplication

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    assert isinstance(app, QApplication)
    return app


def test_unavailable_tray_recovers_after_timeout_and_two_shell_restarts(
    tray_app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from PyQt5 import sip
    from PyQt5.QtWidgets import QMenu

    from astra_voice.ui import notify
    from astra_voice.ui import tray as module
    from astra_voice.ui.indicators import indicator_visible
    from astra_voice.ui.tray_icons import TrayIconProvider, TrayState

    forbidden_tray = Mock(side_effect=AssertionError("реальный QSystemTrayIcon запрещён"))
    monkeypatch.setattr(module, "QSystemTrayIcon", forbidden_tray)
    connection = Mock(name="fake_dbus_connection")
    monkeypatch.setattr(module, "QDBusConnection", connection)
    watcher = Mock(serviceRegistered=Signal(), serviceUnregistered=Signal())
    watcher_factory = Mock(return_value=watcher)
    watcher_factory.WatchForRegistration = 1
    watcher_factory.WatchForUnregistration = 2
    monkeypatch.setattr(module, "QDBusServiceWatcher", watcher_factory)
    clock = Clock()
    monkeypatch.setattr(module, "QTimer", clock.timer)
    monkeypatch.setattr(module, "monotonic", lambda: clock.now / 1000)
    notification = Mock()
    monkeypatch.setattr(notify, "notify_tray_unavailable", notification)
    notify.reset_state()
    send = Mock(return_value=0)
    monkeypatch.setattr(notify, "_send", send)
    flush = Mock(wraps=notify.flush_pending)
    monkeypatch.setattr(notify, "flush_pending", flush)
    forbidden_bus = Mock(side_effect=AssertionError("реальная шина уведомлений запрещена"))
    forbidden_bus.sessionBus.side_effect = AssertionError("реальная шина уведомлений запрещена")
    monkeypatch.setattr(notify, "QDBusConnection", forbidden_bus)
    monkeypatch.setattr(notify, "QDBusInterface", forbidden_bus)
    icon = Mock(name="unavailable_tray")
    icon.isSystemTrayAvailable.return_value = False
    icon.isVisible.return_value = False
    icon.show.side_effect = lambda: setattr(icon.isVisible, "return_value", True)
    icon.hide.side_effect = lambda: setattr(icon.isVisible, "return_value", False)
    factory = Mock(return_value=icon)
    provider = Mock(spec=TrayIconProvider)
    provider.has_icon.return_value = True
    tray = module.Tray(provider, tray_factory=factory)
    menu = icon.setContextMenu.call_args.args[0]
    try:
        assert isinstance(menu, QMenu)
        assert not menu.isVisible()
        assert not tray.registered
        tray.start()
        factory.assert_called_once_with()
        connection.sessionBus.assert_called_once_with()
        watcher_factory.assert_called_once_with(
            "org.kde.StatusNotifierWatcher", connection.sessionBus.return_value, 3, tray
        )
        assert not tray.registered
        assert icon.isSystemTrayAvailable.call_count == 1
        clock.advance(999)
        assert icon.isSystemTrayAvailable.call_count == 1
        clock.advance(1)
        assert icon.isSystemTrayAvailable.call_count == 2
        clock.advance(28_999)
        assert icon.isSystemTrayAvailable.call_count == 30
        assert not tray.registered
        notification.assert_not_called()
        tray.start()  # Повторный start не должен сдвигать дедлайн.
        clock.advance(1)
        assert clock.now == 30_000
        notification.assert_called_once_with()
        assert not tray.registered
        attempts = icon.isSystemTrayAvailable.call_count
        clock.advance(60_000)
        assert icon.isSystemTrayAvailable.call_count == attempts
        icon.show.assert_not_called()
        flush.assert_not_called()
        notify.notify_indicators_lost()
        assert notify.pending_count() == 1
        send.return_value = 1
        icon.isSystemTrayAvailable.return_value = True
        watcher.serviceRegistered.emit("org.kde.StatusNotifierWatcher")
        assert tray.registered
        assert icon.isVisible()
        icon.show.assert_called_once_with()
        flush.assert_called_once_with()
        assert notify.pending_count() == 0
        assert notify.last_delivery_ok()
        clock.advance(60_000)
        notification.assert_called_once_with()

        for restart in range(2):
            watcher.serviceUnregistered.emit("org.kde.StatusNotifierWatcher")
            assert tray.registered is False
            assert not icon.isVisible()
            icon.isSystemTrayAvailable.return_value = False
            clock.advance(30_000)
            notification.assert_called_once_with()
            icon.isSystemTrayAvailable.return_value = True
            watcher.serviceRegistered.emit("org.kde.StatusNotifierWatcher")
            assert tray.registered
            assert icon.isVisible()
            assert icon.show.call_count == restart + 2
            assert flush.call_count == restart + 2
            tray.set_state(TrayState.LISTENING)
            assert menu.actions()[0].text() == "Слушаю…"
            assert not menu.actions()[0].isEnabled()
            tray.set_state(TrayState.DONE)
            clock.advance(800)
            assert menu.actions()[0].text() == "Готов"

        # Таблица О4: страж не должен принимать невидимый трей за индикатор.
        for failure in ("no_tray", "empty_icon", "service_gone"):
            if failure == "no_tray":
                icon.isSystemTrayAvailable.return_value = False
            elif failure == "empty_icon":
                provider.has_icon.return_value = False
            else:
                watcher.serviceUnregistered.emit("org.kde.StatusNotifierWatcher")
            registered = tray.registered
            assert registered is False, failure
            assert not icon.isVisible(), failure
            assert not indicator_visible(pill_visible=False, tray_registered=registered), failure
            icon.isSystemTrayAvailable.return_value = True
            provider.has_icon.return_value = True
            clock.advance(1000)
            assert tray.registered, failure

        # Баннер остаётся однократным и после нового цикла запуска.
        icon.isSystemTrayAvailable.return_value = False
        tray.stop()
        tray.start()
        clock.advance(30_000)
        notification.assert_called_once_with()
        assert not tray.registered
        forbidden_tray.assert_not_called()
        forbidden_bus.assert_not_called()
        forbidden_bus.sessionBus.assert_not_called()
    finally:
        tray.stop()
        sip.delete(tray)
        sip.delete(menu)
        tray_app.processEvents()
        notify.reset_state()
