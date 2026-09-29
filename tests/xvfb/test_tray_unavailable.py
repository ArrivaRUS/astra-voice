"""Нет трея: настоящее Qt-меню, фейки значка, D-Bus, уведомления и времени.

xvfb не установлен; запуск: QT_QPA_PLATFORM=offscreen QT_QUICK_BACKEND=software
pytest -m xvfb. Урок .patches/002: offscreen не изолирует сессионную шину.
Здесь реальный QSystemTrayIcon запрещён, транспорт D-Bus подменён
до создания Tray; виртуальные 30 секунд не требуют реального ожидания.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest
from PyQt5.QtCore import QCoreApplication
from PyQt5.QtDBus import QDBus, QDBusMessage, QDBusVariant

from astra_voice.platform.session import SessionKind

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from helpers.qt_app import get_qapplication  # noqa: E402

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
    return get_qapplication()


@pytest.mark.parametrize("kind", list(SessionKind))
def test_unavailable_tray_recovers_after_timeout_and_two_shell_restarts(
    tray_app: Any, monkeypatch: pytest.MonkeyPatch, kind: SessionKind
) -> None:
    from PyQt5 import sip
    from PyQt5.QtWidgets import QMenu

    from astra_voice.ui import notify
    from astra_voice.ui import tray as module
    from astra_voice.ui.indicators import indicator_visible
    from astra_voice.ui.tray_icons import TrayIconProvider, TrayState

    forbidden_tray = Mock(side_effect=AssertionError("реальный QSystemTrayIcon запрещён"))
    monkeypatch.setattr(module, "QSystemTrayIcon", forbidden_tray)
    host_registered = False
    plasma_alive = False
    registered_before_host_reply: list[bool] = []

    def query(message: QDBusMessage, mode: object, timeout: int) -> QDBusMessage:
        assert mode == QDBus.Block
        assert timeout == 500
        if message.member() == "AddMatch":
            return message.createReply([])
        if message.member() == "Get":
            assert message.arguments() == [
                "org.kde.StatusNotifierWatcher",
                "IsStatusNotifierHostRegistered",
            ]
            registered_before_host_reply.append(tray.registered)
            return message.createReply([QDBusVariant(host_registered)])
        assert message.member() == "GetNameOwner"
        assert message.arguments() == ["org.kde.plasmashell"]
        if plasma_alive:
            return message.createReply([":1.42"])
        return message.createErrorReply(
            "org.freedesktop.DBus.Error.NameHasNoOwner", "нет владельца"
        )

    watcher = Mock(
        serviceRegistered=Signal(), serviceUnregistered=Signal(), serviceOwnerChanged=Signal()
    )

    def connect(
        service: str, path: str, interface: str, member: str, slot: Callable[..., None]
    ) -> bool:
        if member == "NameOwnerChanged":
            assert service == interface == "org.freedesktop.DBus"
            assert path == "/org/freedesktop/DBus"
            watcher.serviceRegistered.connect(lambda name: slot(name, "", ":1.42"))
            watcher.serviceUnregistered.connect(lambda name: slot(name, ":1.42", ""))
            watcher.serviceOwnerChanged.connect(slot)
        else:
            assert service == "org.kde.StatusNotifierWatcher"
            assert path == "/StatusNotifierWatcher"
            assert interface in (
                "org.kde.StatusNotifierWatcher",
                "org.freedesktop.DBus.Properties",
            )
        return True

    transport = Mock(name="fake_dbus_transport")
    transport.isConnected.return_value = True
    transport.call.side_effect = query
    transport.connect.side_effect = connect
    transport.interface.side_effect = AssertionError("интерфейс D-Bus запрещён")
    connection = Mock(name="fake_dbus_connection")
    connection.connectToBus.return_value = transport
    connection.sessionBus.side_effect = AssertionError("реальная сессионная шина запрещена")
    monkeypatch.setattr(module, "detect", lambda: kind)
    monkeypatch.setattr(module, "QDBusConnection", connection)
    # Как в unit-тестах: настоящий исполнитель без запуска потока демона.
    executor = module._TrayBusTransport(module._get_bus_receiver())

    def command(generation: int, operation: str, payload: Any) -> None:
        executor.execute(generation, operation, payload)
        QCoreApplication.sendPostedEvents(module._bus_receiver, module._bus_event_type)

    monkeypatch.setattr(module, "_send_bus_command", command)
    monkeypatch.setattr(module, "_bus_transport", None)
    clock = Clock()
    monkeypatch.setattr(module, "QTimer", clock.timer)
    monkeypatch.setattr(module, "monotonic", lambda: clock.now / 1000)
    notification = Mock(wraps=notify.notify_tray_unavailable)
    monkeypatch.setattr(notify, "notify_tray_unavailable", notification)
    notify.reset_state()
    send = Mock(return_value=0)

    def send_stub(seq: int, notice: Any, replaces_id: int) -> None:
        send(notice.summary, notice.body, notice.urgency, replaces_id, notice.actions)
        if send.return_value:
            notify._on_reply(seq, 41, None)
        else:
            notify._on_reply(seq, None, "org.freedesktop.DBus.Error.Disconnected")

    monkeypatch.setattr(notify, "_send", send_stub)
    flush = Mock(wraps=notify.flush_pending)
    monkeypatch.setattr(notify, "flush_pending", flush)
    drop = Mock(wraps=notify.drop_pending)
    monkeypatch.setattr(notify, "drop_pending", drop)
    deliveries = Mock()
    deliveries.attach_mock(drop, "drop")
    deliveries.attach_mock(flush, "flush")
    monkeypatch.setattr(notify, "notify_tray_depends_on_panel", Mock())
    forbidden_bus = Mock(side_effect=AssertionError("реальная шина уведомлений запрещена"))
    forbidden_bus.sessionBus.side_effect = AssertionError("реальная шина уведомлений запрещена")
    monkeypatch.setattr(notify, "QDBusConnection", forbidden_bus)
    monkeypatch.setattr(notify, "QDBusInterface", forbidden_bus)
    icon = Mock(name="unavailable_tray")
    cached_available: bool | None = None
    register_calls = 0

    def qt_available() -> bool:
        nonlocal cached_available
        if cached_available is None:
            cached_available = host_registered
        return cached_available

    def qt_show() -> None:
        nonlocal register_calls
        if icon.isSystemTrayAvailable():
            register_calls += 1
            icon.isVisible.return_value = True

    icon.isSystemTrayAvailable.side_effect = qt_available
    icon.isVisible.return_value = False
    icon.show.side_effect = qt_show
    icon.hide.side_effect = lambda: setattr(icon.isVisible, "return_value", False)
    factory = Mock(return_value=icon)
    provider = Mock(spec=TrayIconProvider)
    provider.has_icon.return_value = True
    # Подпись значка чистится как внешняя строка, поэтому провайдер обязан
    # вернуть именно текст, а не заглушку.
    provider.tooltip.return_value = "Astra Voice"
    tray = module.Tray(provider, tray_factory=factory)
    menu = icon.setContextMenu.call_args.args[0]
    try:
        assert isinstance(menu, QMenu)
        assert not menu.isVisible()
        assert not tray.registered
        tray.start()
        factory.assert_called_once_with()
        connection.connectToBus.assert_called_once()
        bus_name = connection.connectToBus.call_args.args[1]
        assert isinstance(bus_name, str)
        assert bus_name.startswith("astra-voice-tray-")
        connection.connectToBus.assert_called_once_with(connection.SessionBus, bus_name)
        assert transport.connect.call_count == 4
        assert not tray.registered
        tray_app.processEvents()
        for _ in range(29):
            clock.advance(1000)
            tray_app.processEvents()
        clock.advance(999)
        assert cached_available is None
        icon.isSystemTrayAvailable.assert_not_called()
        assert not tray.registered
        notification.assert_not_called()
        assert sum(call.args[0].member() == "Get" for call in transport.call.call_args_list) == 30
        tray.start()  # Повторный start не должен сдвигать дедлайн.
        clock.advance(1)
        assert clock.now == 30_000
        notification.assert_called_once_with()
        assert not tray.registered
        attempts = transport.call.call_count
        clock.advance(60_000)
        assert transport.call.call_count == attempts
        icon.show.assert_not_called()
        flush.assert_not_called()
        notify.notify_indicators_lost()
        assert notify.pending_count() == 2  # Баннер отсутствия трея и остановка записи.
        send.return_value = 1
        host_registered = True
        previous_gets = len(registered_before_host_reply)
        watcher.serviceRegistered.emit("org.kde.StatusNotifierWatcher")
        assert len(registered_before_host_reply) == previous_gets + 1
        # Сигнал ещё не является ответом Get: проверяем состояние до возврата ответа.
        assert registered_before_host_reply[-1] is False
        tray_app.processEvents()
        if kind == SessionKind.KDE:
            assert not tray.registered  # KDED есть, plasmashell пока нет.
            plasma_alive = True
            watcher.serviceRegistered.emit("org.kde.plasmashell")
        assert tray.registered
        assert icon.isVisible()
        icon.show.assert_called_once_with()
        flush.assert_called_once_with()
        drop.assert_called_once_with("Значок не появился на панели")
        assert [c[0] for c in deliveries.mock_calls] == ["drop", "flush"]
        assert send.call_args.args[0] == "Запись остановлена"
        assert cached_available is True
        assert register_calls > 0
        assert notify.pending_count() == 0
        assert notify.last_delivery_ok()
        clock.advance(60_000)
        notification.assert_called_once_with()

        for restart in range(2):
            # В KDE падает только plasmashell, KDED и его host=True остаются.
            service = (
                "org.kde.plasmashell"
                if kind == SessionKind.KDE
                else "org.kde.StatusNotifierWatcher"
            )
            if kind != SessionKind.KDE:
                host_registered = False
            watcher.serviceOwnerChanged.emit(service, ":1.42", "")
            assert tray.registered is False
            assert not icon.isVisible()
            clock.advance(30_000)
            tray_app.processEvents()
            notification.assert_called_once_with()
            host_registered = True
            watcher.serviceOwnerChanged.emit(service, "", ":1.43")
            tray_app.processEvents()
            assert tray.registered
            assert icon.isVisible()
            assert register_calls == restart + 2
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
                icon.isVisible.return_value = False
            elif failure == "empty_icon":
                provider.has_icon.return_value = False
            else:
                watcher.serviceUnregistered.emit("org.kde.StatusNotifierWatcher")
            registered = tray.registered
            assert registered is False, failure
            assert not icon.isVisible(), failure
            assert not indicator_visible(pill_visible=False, tray_registered=registered), failure
            provider.has_icon.return_value = True
            clock.advance(1000)
            tray_app.processEvents()
            assert tray.registered, failure

        # Баннер остаётся однократным и после нового цикла запуска.
        host_registered = False
        tray.stop()
        tray.start()
        tray_app.processEvents()
        clock.advance(30_000)
        notification.assert_called_once_with()
        assert not tray.registered
        forbidden_tray.assert_not_called()
        forbidden_bus.assert_not_called()
        forbidden_bus.sessionBus.assert_not_called()
        connection.sessionBus.assert_not_called()
        transport.interface.assert_not_called()
    finally:
        tray.stop()
        sip.delete(tray)
        sip.delete(menu)
        tray_app.processEvents()
        notify.reset_state()
    connection.connectToBus.assert_called_once_with(connection.SessionBus, bus_name)
    assert transport.connect.call_count == 4
    transport.disconnect.assert_not_called()
    connection.disconnectFromBus.assert_not_called()
