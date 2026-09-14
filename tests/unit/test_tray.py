"""Настоящее меню Qt с фейками значка, D-Bus, уведомлений и часов."""

from __future__ import annotations

import inspect
import logging
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event, get_ident
from time import monotonic, sleep
from typing import TYPE_CHECKING, Any, get_type_hints
from unittest.mock import Mock, call

import pytest
from PyQt5.QtCore import QTimer as QtTimer
from PyQt5.QtDBus import QDBusMessage, QDBusVariant

from astra_voice.platform.session import SessionKind
from astra_voice.ui.tray import _start_bus_worker
from astra_voice.ui.tray_icons import TrayIconProvider, TrayState

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from helpers.qt_app import get_qapplication  # noqa: E402

if TYPE_CHECKING:
    from PyQt5.QtWidgets import QApplication, QMenu

    from astra_voice.ui.tray import Tray

pytestmark = pytest.mark.unit
SERVICE = "org.kde.StatusNotifierWatcher"
PLASMA = "org.kde.plasmashell"
UNAVAILABLE = "Значок не появился на панели"


@dataclass
class PendingCall:
    message: QDBusMessage
    success: Callable[[QDBusMessage], None]
    error: Callable[..., None]

    def reply(self, value: object) -> None:
        if self.message.member() == "Get":
            value = QDBusVariant(value)
        self.success(self.message.createReply([value]))

    def fail(self) -> None:
        self.error(Mock(), self.message)


class Bus:
    """Подмена транспорта: ответ может задержаться и пережить смену владельца."""

    def __init__(self) -> None:
        self.host_registered: object = True
        self.plasma_owner = ":1.42"
        self.auto_reply = True
        self.auto_match_reply = True
        self.calls: list[PendingCall] = []
        self.matches: list[PendingCall] = []
        self.signals: dict[str, Callable[..., None]] = {}
        self.transport = Mock()
        self.transport.callWithCallback.side_effect = self.submit
        self.transport.connect.side_effect = self.connect
        self.transport.call.side_effect = AssertionError("Synchronous D-Bus forbidden")
        self.transport.interface.side_effect = AssertionError("D-Bus interface forbidden")
        self.watcher = Mock(
            serviceRegistered=Signal(), serviceUnregistered=Signal(), serviceOwnerChanged=Signal()
        )

    def submit(
        self,
        message: QDBusMessage,
        success: Callable[[QDBusMessage], None],
        error: Callable[..., None],
        timeout: int,
    ) -> bool:
        assert 0 < timeout <= 500
        pending = PendingCall(message, success, error)
        if message.member() == "AddMatch":
            self.matches.append(pending)
            if self.auto_match_reply:
                pending.success(message.createReply([]))
            return True
        self.calls.append(pending)
        if self.auto_reply:
            if message.member() == "Get":
                pending.reply(self.host_registered)
            elif self.plasma_owner:
                pending.reply(self.plasma_owner)
            else:
                pending.fail()
        return True

    def connect(
        self, service: str, path: str, interface: str, member: str, slot: Callable[..., None]
    ) -> bool:
        if member == "NameOwnerChanged":
            assert service == interface == "org.freedesktop.DBus"
            assert path == "/org/freedesktop/DBus"
            self.watcher.serviceRegistered.connect(lambda name: slot(name, "", ":1.42"))
            self.watcher.serviceUnregistered.connect(lambda name: slot(name, ":1.42", ""))
            self.watcher.serviceOwnerChanged.connect(slot)
        else:
            assert service == SERVICE
            assert path == "/StatusNotifierWatcher"
            assert interface in (SERVICE, "org.freedesktop.DBus.Properties")
        self.signals[member] = slot
        return True

    def host_changed(self, value: bool) -> None:
        self.host_registered = value
        member = "StatusNotifierHostRegistered" if value else "StatusNotifierHostUnregistered"
        self.signals[member]()


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
        while True:
            pending = [t for t in self.timers if t.due is not None and t.due <= end]
            if not pending:
                break
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
        self.interval = 0
        self.single_shot = False
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


@dataclass
class Harness:
    tray: Tray
    icon: Mock
    provider: Mock
    factory: Mock
    watcher: Mock
    connection: Mock
    banner: Mock
    flush: Mock
    send: Mock
    clock: Clock
    bus: Bus
    detect: Mock
    drop: Mock
    panel_warning: Mock
    start_worker: Mock

    @property
    def menu(self) -> QMenu:
        menu: QMenu = self.icon.setContextMenu.call_args.args[0]
        return menu


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    return get_qapplication()


@pytest.fixture
def harness(qapp: QApplication, monkeypatch: pytest.MonkeyPatch) -> Iterator[Harness]:
    from astra_voice.ui import notify
    from astra_voice.ui import tray as module

    # Даже случайный обход фабрики не должен попасть на панель заказчика.
    monkeypatch.setattr(
        module, "QSystemTrayIcon", Mock(side_effect=AssertionError("Real tray forbidden"))
    )
    connection = Mock()
    bus = Bus()
    connection.sessionBus.return_value = bus.transport
    connection.connectToBus.return_value = bus.transport
    monkeypatch.setattr(module, "QDBusConnection", connection)
    monkeypatch.setattr(
        module,
        "QDBusServiceWatcher",
        Mock(side_effect=AssertionError("Real service watcher forbidden")),
        raising=False,
    )
    detect = Mock(return_value=SessionKind.OTHER)
    monkeypatch.setattr(module, "detect", detect)
    watcher = bus.watcher
    # В поведенческих тестах очередь рабочего потока исполняется сразу.
    # Отдельные тесты ниже запускают настоящий QThread с тем же фейком шины.
    start_worker = Mock()
    monkeypatch.setattr(module, "_start_bus_worker", start_worker)
    banner = Mock()
    monkeypatch.setattr(notify, "notify_tray_unavailable", banner)
    notify.reset_state()
    send = Mock(return_value=1)
    monkeypatch.setattr(notify, "_send", send)
    flush = Mock(wraps=notify.flush_pending)
    monkeypatch.setattr(notify, "flush_pending", flush)
    drop = Mock(wraps=notify.drop_pending)
    monkeypatch.setattr(notify, "drop_pending", drop)
    panel_warning = Mock()
    monkeypatch.setattr(notify, "notify_tray_depends_on_panel", panel_warning)
    forbidden_bus = Mock(side_effect=AssertionError("Real notification bus forbidden"))
    forbidden_bus.sessionBus.side_effect = AssertionError("Real notification bus forbidden")
    monkeypatch.setattr(notify, "QDBusConnection", forbidden_bus)
    monkeypatch.setattr(notify, "QDBusInterface", forbidden_bus)
    clock = Clock()
    monkeypatch.setattr(module, "QTimer", clock.timer)
    monkeypatch.setattr(module, "monotonic", lambda: clock.now / 1000)
    provider = Mock(spec=TrayIconProvider)
    provider.has_icon.return_value = True
    provider.icon.side_effect = lambda state: f"icon:{state.value}"
    provider.tooltip.side_effect = lambda state, *, hotkey: f"tip:{state.value}:{hotkey}"
    icon = Mock()
    icon.isSystemTrayAvailable.return_value = True
    icon.isVisible.return_value = False
    icon.show.side_effect = lambda: setattr(icon.isVisible, "return_value", True)
    icon.hide.side_effect = lambda: setattr(icon.isVisible, "return_value", False)
    factory = Mock(return_value=icon)
    tray = module.Tray(provider, hotkey="Alt+F9", tray_factory=factory)
    yield Harness(
        tray,
        icon,
        provider,
        factory,
        watcher,
        connection,
        banner,
        flush,
        send,
        clock,
        bus,
        detect,
        drop,
        panel_warning,
        start_worker,
    )
    tray.stop()
    tray.deleteLater()
    notify.reset_state()


def menu_labels(harness: Harness) -> list[str | None]:
    return [None if action.isSeparator() else action.text() for action in harness.menu.actions()]


def test_exact_menu_and_shortcuts(harness: Harness) -> None:
    assert menu_labels(harness) == [
        "Готов",
        None,
        "Отмена",
        "Модель не установлена",
        "Скопировать последний текст",
        None,
        "Настройки…",
        "Проверить обновления",
        "О программе",
        None,
        "Выход",
    ]
    assert [a.shortcut().toString() for a in harness.menu.actions()] == [
        "",
        "",
        "",
        "",
        "",
        "",
        "Ctrl+,",
        "",
        "",
        "",
        "Ctrl+Q",
    ]
    harness.factory.assert_called_once_with()
    harness.icon.show.assert_not_called()
    harness.connection.sessionBus.assert_not_called()
    harness.connection.connectToBus.assert_not_called()


@pytest.mark.parametrize(
    ("state", "status", "can_cancel"),
    [
        (TrayState.IDLE, "Готов", False),
        (TrayState.LISTENING, "Слушаю…", True),
        (TrayState.PROCESSING, "Распознаю…", True),
        (TrayState.DONE, "Готов", False),
        (TrayState.ERROR, "Микрофон недоступен", False),
        (TrayState.NOKEY, "Горячая клавиша не захвачена", False),
    ],
)
def test_state_menu(harness: Harness, state: TrayState, status: str, can_cancel: bool) -> None:
    before = menu_labels(harness)
    harness.tray.set_state(state)
    actions = harness.menu.actions()
    assert actions[0].text() == status
    assert not actions[0].isEnabled()
    assert actions[2].isEnabled() == can_cancel
    assert menu_labels(harness)[1:] == before[1:]


def test_flags_refresh_without_changing_menu(harness: Harness) -> None:
    before = menu_labels(harness)
    for value in (False, True, False):
        harness.tray.set_has_last_text(value)
        harness.tray.set_updates_enabled(value)
        assert harness.menu.actions()[4].isEnabled() == value
        assert harness.menu.actions()[7].isEnabled() == value
        assert menu_labels(harness) == before
        assert not harness.menu.actions()[0].isEnabled()


def test_models_zero_one_many_and_reset(harness: Harness) -> None:
    action = harness.menu.actions()[3]
    for models, active, label in [
        ([], None, "Модель не установлена"),
        (["GigaAM"], None, "Модель: GigaAM"),
    ]:
        harness.tray.set_models(models, active)
        assert action.text() == label
        assert not action.isEnabled()
        assert action.menu() is None

    harness.tray.set_models(["A", "B", "C"], "B")
    assert action.text() == "Модель"
    assert action.isEnabled()
    submenu = action.menu()
    assert submenu is not None
    assert [a.text() for a in submenu.actions()] == ["A", "B", "C"]
    assert all(a.isCheckable() for a in submenu.actions())
    assert [a.isChecked() for a in submenu.actions()] == [False, True, False]
    callback = Mock()
    harness.tray.on_model_selected = callback
    submenu.actions()[2].trigger()
    callback.assert_called_once_with("C")
    assert [a.isChecked() for a in submenu.actions()] == [False, False, True]
    harness.tray.set_models(["A", "B", "C"], "A")
    assert [a.isChecked() for a in submenu.actions()] == [True, False, False]
    harness.tray.on_model_selected = None
    submenu.actions()[1].trigger()
    harness.tray.set_models([], None)
    assert action.menu() is None
    assert not action.isEnabled()
    assert action.text() == "Модель не установлена"
    assert len(harness.menu.actions()) == 11


@pytest.mark.parametrize(
    ("index", "attribute"),
    [
        (2, "on_cancel"),
        (4, "on_copy_last"),
        (6, "on_settings"),
        (7, "on_check_updates"),
        (8, "on_about"),
        (10, "on_quit"),
    ],
)
def test_callbacks_and_none(harness: Harness, index: int, attribute: str) -> None:
    harness.tray.set_state(TrayState.LISTENING)
    harness.tray.set_has_last_text(True)
    harness.tray.set_updates_enabled(True)
    action = harness.menu.actions()[index]
    assert getattr(harness.tray, attribute) is None
    action.trigger()
    callback = Mock()
    setattr(harness.tray, attribute, callback)
    action.trigger()
    callback.assert_called_once_with()
    setattr(harness.tray, attribute, None)
    action.trigger()
    callback.assert_called_once_with()


@pytest.mark.parametrize("state", list(TrayState))
def test_icons_and_tooltips(harness: Harness, state: TrayState) -> None:
    harness.tray.set_state(state)
    harness.icon.setIcon.assert_called_with(f"icon:{state.value}")
    harness.icon.setToolTip.assert_called_with(f"tip:{state.value}:Alt+F9")
    harness.provider.tooltip.assert_called_with(state, hotkey="Alt+F9")
    for override in ("Собственная подсказка", ""):
        harness.tray.set_state(state, override)
        harness.icon.setToolTip.assert_called_with(override)
    harness.tray.set_state(state)
    harness.icon.setToolTip.assert_called_with(f"tip:{state.value}:Alt+F9")


def test_done_returns_to_idle_at_800_ms(harness: Harness) -> None:
    harness.tray.set_state(TrayState.DONE)
    count = harness.icon.setIcon.call_count
    harness.clock.advance(799)
    assert harness.icon.setIcon.call_count == count
    harness.clock.advance(1)
    harness.icon.setIcon.assert_called_with("icon:idle")
    harness.icon.setToolTip.assert_called_with("tip:idle:Alt+F9")
    harness.clock.advance(5000)
    assert harness.icon.setIcon.call_count == count + 1


@pytest.mark.parametrize("state", list(TrayState))
def test_new_state_cancels_or_restarts_done(harness: Harness, state: TrayState) -> None:
    harness.tray.set_state(TrayState.DONE)
    harness.clock.advance(400)
    harness.tray.set_state(state)
    count = harness.icon.setIcon.call_count
    harness.clock.advance(400)
    assert harness.icon.setIcon.call_count == count
    harness.clock.advance(400)
    expected = "idle" if state == TrayState.DONE else state.value
    harness.icon.setIcon.assert_called_with(f"icon:{expected}")


def test_register_immediately_and_stop(harness: Harness) -> None:
    assert not harness.tray.registered
    harness.tray.start()
    assert harness.tray.registered
    harness.icon.show.assert_called_once_with()
    harness.connection.connectToBus.assert_called_once()
    assert len(harness.bus.matches) == 4
    assert set(harness.bus.signals) == {
        "NameOwnerChanged",
        "StatusNotifierHostRegistered",
        "StatusNotifierHostUnregistered",
        "PropertiesChanged",
    }
    harness.tray.start()
    harness.clock.advance(60_000)
    harness.icon.show.assert_called_once_with()
    harness.banner.assert_not_called()
    harness.tray.stop()
    assert not harness.tray.registered
    harness.connection.disconnectFromBus.assert_called_once_with(
        harness.connection.connectToBus.call_args.args[1]
    )
    harness.watcher.serviceRegistered.emit(SERVICE)
    harness.watcher.serviceUnregistered.emit(SERVICE)
    harness.clock.advance(60_000)
    harness.icon.show.assert_called_once_with()
    harness.banner.assert_not_called()


def test_watcher_return_after_timeout_registers_and_keeps_banner_once(harness: Harness) -> None:
    harness.bus.host_registered = False
    harness.tray.start()
    assert not harness.tray.registered
    harness.clock.advance(999)
    assert len(harness.bus.calls) == 1
    harness.clock.advance(1)
    assert len(harness.bus.calls) == 2
    harness.clock.advance(28_999)
    assert len(harness.bus.calls) == 30
    harness.banner.assert_not_called()
    harness.tray.start()  # Повторный start не продлевает текущий срок.
    harness.clock.advance(1)
    harness.banner.assert_called_once_with()
    assert not harness.tray.registered
    harness.icon.show.assert_not_called()
    count = harness.bus.transport.callWithCallback.call_count
    harness.clock.advance(60_000)
    assert harness.bus.transport.callWithCallback.call_count == count
    harness.bus.host_registered = True
    harness.watcher.serviceRegistered.emit(SERVICE)
    assert harness.tray.registered
    harness.icon.show.assert_called_once_with()
    harness.clock.advance(60_000)
    harness.banner.assert_called_once_with()
    harness.bus.host_registered = False
    harness.watcher.serviceUnregistered.emit(SERVICE)
    assert not harness.tray.registered
    harness.clock.advance(30_000)
    harness.banner.assert_called_once_with()
    harness.tray.stop()
    harness.tray.start()
    harness.clock.advance(30_000)
    harness.banner.assert_called_once_with()


def test_watcher_registration_retries_immediately(harness: Harness) -> None:
    harness.bus.host_registered = False
    harness.tray.start()
    harness.clock.advance(250)
    harness.bus.host_registered = True
    harness.watcher.serviceRegistered.emit("unrelated.service")
    assert not harness.tray.registered
    harness.watcher.serviceRegistered.emit(SERVICE)
    assert harness.tray.registered
    assert harness.clock.now == 250
    harness.icon.show.assert_called_once_with()
    harness.clock.advance(60_000)
    harness.banner.assert_not_called()


def test_periodic_retry_can_register(harness: Harness) -> None:
    harness.bus.host_registered = False
    harness.tray.start()
    harness.bus.host_registered = True
    harness.clock.advance(1000)
    assert harness.tray.registered
    harness.clock.advance(60_000)
    harness.banner.assert_not_called()


def test_loss_clears_registered_and_starts_a_fresh_retry_window(harness: Harness) -> None:
    harness.tray.start()
    harness.clock.advance(60_000)
    harness.watcher.serviceUnregistered.emit("unrelated.service")
    assert harness.tray.registered
    # Даже если Qt пока сообщает старую доступность, О4 сразу видит потерю.
    harness.watcher.serviceUnregistered.emit(SERVICE)
    assert not harness.tray.registered
    harness.icon.hide.assert_called_once_with()
    harness.bus.host_registered = False
    harness.clock.advance(29_999)
    harness.banner.assert_not_called()
    harness.clock.advance(1)
    harness.banner.assert_called_once_with()


def test_two_watcher_restarts_register_again(harness: Harness) -> None:
    harness.tray.start()
    assert harness.tray.registered
    for restart in range(2):
        # Сигнал потери должен сработать даже при устаревшем ответе Qt.
        harness.watcher.serviceUnregistered.emit(SERVICE)
        assert not harness.tray.registered
        assert not harness.icon.isVisible()
        harness.bus.host_registered = False
        harness.clock.advance(2000)
        assert not harness.tray.registered
        harness.bus.host_registered = True
        harness.watcher.serviceRegistered.emit(SERVICE)
        assert harness.tray.registered
        assert harness.icon.isVisible()
        assert harness.icon.show.call_count == restart + 2
        assert harness.flush.call_count == restart + 2
        harness.tray.set_state(TrayState.LISTENING)
        harness.icon.setIcon.assert_called_with("icon:listening")
        assert harness.menu.actions()[0].text() == "Слушаю…"
        harness.tray.set_state(TrayState.DONE)
        harness.clock.advance(800)
        harness.icon.setIcon.assert_called_with("icon:idle")
        assert harness.tray.registered
        harness.clock.advance(60_000)
    harness.banner.assert_not_called()


def test_watcher_return_restarts_expired_deadline_before_timeout_delivery(harness: Harness) -> None:
    harness.bus.host_registered = False
    harness.tray.start()
    # Сигнал службы пришёл раньше доставки просроченного timeout.
    harness.clock.now = 30_001
    harness.bus.host_registered = True
    harness.watcher.serviceRegistered.emit(SERVICE)
    assert harness.tray.registered
    harness.icon.show.assert_called_once_with()
    harness.clock.advance(60_000)
    harness.banner.assert_not_called()


def test_delayed_retry_does_not_extend_deadline(harness: Harness) -> None:
    harness.bus.host_registered = False
    harness.tray.start()
    # В отличие от нового Watcher, запоздалый обычный ретрай сохраняет срок.
    harness.clock.now = 30_001
    harness.bus.host_registered = True
    harness.clock.timers[1].timeout.emit()
    assert not harness.tray.registered
    harness.icon.show.assert_not_called()
    harness.banner.assert_called_once_with()


@pytest.mark.parametrize("loss", ["host", "hidden"])
def test_registered_rechecks_tray_without_bus_signal(harness: Harness, loss: str) -> None:
    harness.tray.start()
    assert harness.tray.registered
    if loss == "host":
        harness.bus.host_changed(False)
    else:
        harness.icon.isVisible.return_value = False
    assert harness.tray.registered is False
    assert not harness.icon.isVisible()
    harness.bus.host_registered = True
    harness.clock.advance(999)
    harness.icon.show.assert_called_once_with()
    harness.clock.advance(1)
    assert harness.tray.registered
    assert harness.icon.show.call_count == 2


def test_repeated_registered_checks_do_not_extend_retry_deadline(harness: Harness) -> None:
    harness.tray.start()
    harness.bus.host_changed(False)
    assert not harness.tray.registered
    for _ in range(60):
        harness.clock.advance(500)
        assert not harness.tray.registered
    harness.banner.assert_called_once_with()


@pytest.mark.parametrize("state", list(TrayState))
def test_empty_icon_retries_until_icon_appears(
    harness: Harness, caplog: pytest.LogCaptureFixture, state: TrayState
) -> None:
    harness.provider.has_icon.return_value = False
    harness.tray.set_state(state)
    harness.tray.start()
    assert harness.tray.registered is False
    harness.icon.show.assert_not_called()
    harness.flush.assert_not_called()
    harness.provider.has_icon.assert_called_with(state)
    assert any(
        record.name == "astra_voice.ui.tray" and record.levelno == logging.WARNING
        for record in caplog.records
    )
    harness.clock.advance(1000)
    assert harness.tray.registered is False
    harness.icon.show.assert_not_called()
    harness.provider.has_icon.return_value = True
    # Иконку после refresh() нужно установить заново перед show().
    harness.provider.icon.side_effect = lambda state: f"refreshed:{state.value}"
    harness.clock.advance(1000)
    assert harness.tray.registered
    current = TrayState.IDLE if state == TrayState.DONE else state
    harness.icon.setIcon.assert_called_with(f"refreshed:{current.value}")
    harness.icon.show.assert_called_once_with()
    harness.flush.assert_called_once_with()


@pytest.mark.parametrize("change_state", [False, True])
def test_icon_loss_hides_registered_tray_and_retries(
    harness: Harness, change_state: bool, caplog: pytest.LogCaptureFixture
) -> None:
    harness.tray.start()
    assert harness.tray.registered
    harness.provider.has_icon.return_value = False
    harness.icon.setIcon.reset_mock()
    if change_state:
        harness.tray.set_state(TrayState.LISTENING)
        # Не ждём чтения registered: исчезнувшая иконка снимается сразу.
        assert not harness.icon.isVisible()
        harness.icon.setIcon.assert_not_called()
    assert harness.tray.registered is False
    assert not harness.icon.isVisible()
    assert any(record.levelno == logging.WARNING for record in caplog.records)
    harness.provider.has_icon.return_value = True
    harness.clock.advance(1000)
    assert harness.tray.registered
    assert harness.icon.show.call_count == 2


def test_state_change_after_timeout_can_register_restored_icon(harness: Harness) -> None:
    harness.provider.has_icon.return_value = False
    harness.tray.start()
    harness.clock.advance(30_000)
    harness.banner.assert_called_once_with()
    harness.provider.has_icon.return_value = True
    harness.tray.set_state(TrayState.LISTENING)
    assert harness.tray.registered
    harness.clock.advance(60_000)
    harness.banner.assert_called_once_with()


def test_show_failure_does_not_register_or_flush(harness: Harness) -> None:
    harness.icon.show.side_effect = None
    harness.tray.start()
    assert harness.tray.registered is False
    harness.flush.assert_not_called()
    harness.clock.advance(1000)
    assert harness.tray.registered is False
    assert harness.icon.show.call_count == 2
    harness.clock.advance(29_000)
    harness.banner.assert_called_once_with()


def test_each_registration_flushes_pending_notifications(
    harness: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    from astra_voice.ui import notify

    caplog.set_level(logging.INFO, logger="astra_voice.ui.tray")
    for registration in range(3):
        harness.send.return_value = 0
        notify.notify_indicators_lost()
        notify.notify_hotkey_not_grabbed()
        assert notify.pending_count() == 2
        assert not notify.last_delivery_ok()
        harness.send.return_value = 1
        if registration == 0:
            harness.tray.start()
        else:
            harness.watcher.serviceRegistered.emit(SERVICE)
        assert harness.tray.registered
        assert harness.flush.call_count == registration + 1
        harness.flush.assert_called_with()
        assert notify.pending_count() == 0
        assert notify.last_delivery_ok()
        assert [call.args[0] for call in harness.send.call_args_list[-2:]] == [
            "Запись остановлена",
            "Горячая клавиша не захвачена",
        ]
        assert (
            len(
                [
                    record
                    for record in caplog.records
                    if record.name == "astra_voice.ui.tray" and record.args == (2,)
                ]
            )
            == registration + 1
        )
        harness.watcher.serviceUnregistered.emit(SERVICE)


@pytest.mark.parametrize("failure", ["no_tray", "empty_icon", "service_gone"])
def test_o4_never_accepts_an_invisible_tray(harness: Harness, failure: str) -> None:
    from astra_voice.ui.indicators import indicator_visible

    harness.tray.start()
    assert harness.tray.registered
    if failure == "no_tray":
        harness.bus.host_changed(False)
    elif failure == "empty_icon":
        harness.provider.has_icon.return_value = False
    else:
        harness.watcher.serviceUnregistered.emit(SERVICE)
    registered = harness.tray.registered
    assert registered is False
    assert not harness.icon.isVisible()
    assert not indicator_visible(pill_visible=False, tray_registered=registered)


def test_stop_cancels_pending_timers(harness: Harness) -> None:
    harness.bus.host_registered = False
    harness.tray.start()
    harness.tray.set_state(TrayState.DONE)
    harness.tray.stop()
    count = harness.icon.setIcon.call_count
    harness.clock.advance(60_000)
    assert harness.icon.setIcon.call_count == count
    harness.banner.assert_not_called()
    assert not harness.tray.registered


def test_only_presence_flag_is_stored(harness: Harness) -> None:
    hints: dict[str, Any] = get_type_hints(harness.tray.set_has_last_text)
    assert hints == {"value": bool, "return": type(None)}
    assert list(inspect.signature(harness.tray.set_has_last_text).parameters) == ["value"]
    before = vars(harness.tray).copy()
    harness.tray.set_has_last_text(True)
    changed = {key: value for key, value in vars(harness.tray).items() if before[key] != value}
    assert changed == {"_has_last_text": True}
    assert {k: v for k, v in vars(harness.tray).items() if "text" in k} == {"_has_last_text": True}


def test_state_reexport(harness: Harness) -> None:
    from astra_voice.ui.tray import TrayState as ReexportedState

    assert ReexportedState is TrayState


@pytest.mark.parametrize("kind", list(SessionKind))
def test_only_kde_requires_plasmashell(harness: Harness, kind: SessionKind) -> None:
    harness.detect.return_value = kind
    harness.bus.plasma_owner = ""
    harness.tray.start()
    assert harness.tray.registered is (kind != SessionKind.KDE)
    if kind == SessionKind.KDE:
        owner_calls = [c for c in harness.bus.calls if c.message.member() == "GetNameOwner"]
        assert len(owner_calls) == 1
        message = owner_calls[0].message
        assert message.service() == "org.freedesktop.DBus"
        assert message.path() == "/org/freedesktop/DBus"
        assert message.interface() == "org.freedesktop.DBus"
        assert message.arguments() == [PLASMA]
    else:
        assert all(c.message.member() == "Get" for c in harness.bus.calls)
    harness.watcher.serviceRegistered.emit(PLASMA)
    assert harness.tray.registered
    harness.watcher.serviceUnregistered.emit(PLASMA)
    assert harness.tray.registered is (kind != SessionKind.KDE)


def test_kded_survives_two_plasmashell_crashes(harness: Harness) -> None:
    harness.detect.return_value = SessionKind.KDE
    harness.tray.start()
    for restart in range(2):
        assert harness.tray.registered
        harness.clock.advance(60_000)
        # KDED по-прежнему владеет watcher и отвечает host=True.
        harness.watcher.serviceOwnerChanged.emit(PLASMA, ":1.42", "")
        assert harness.tray.registered is False
        harness.clock.advance(29_999)
        if restart == 0:
            harness.banner.assert_not_called()
        harness.clock.advance(1)
        harness.banner.assert_called_once_with()
        harness.watcher.serviceOwnerChanged.emit(PLASMA, "", ":1.43")
        assert harness.tray.registered
        assert harness.icon.show.call_count == restart + 2
    # Владелец plasmashell запрошен ровно один раз, дальше работают сигналы.
    assert sum(c.message.member() == "GetNameOwner" for c in harness.bus.calls) == 1


@pytest.mark.parametrize("value", [False, None, 1, "true", [], {}])
def test_host_requires_boolean_true_reply(harness: Harness, value: object) -> None:
    harness.bus.auto_reply = False
    harness.tray.start()
    harness.watcher.serviceRegistered.emit(SERVICE)
    harness.bus.calls[-1].reply(value)
    assert not harness.tray.registered
    harness.icon.show.assert_not_called()
    harness.icon.isSystemTrayAvailable.assert_not_called()
    harness.clock.advance(1000)
    request = harness.bus.calls[-1]
    assert request.message.service() == SERVICE
    assert request.message.path() == "/StatusNotifierWatcher"
    assert request.message.interface() == "org.freedesktop.DBus.Properties"
    assert request.message.member() == "Get"
    assert request.message.arguments() == [SERVICE, "IsStatusNotifierHostRegistered"]
    request.reply(True)
    assert harness.tray.registered
    harness.icon.show.assert_called_once_with()


@pytest.mark.parametrize("member", ["StatusNotifierHostRegistered", "PropertiesChanged"])
def test_host_signal_refreshes_cache_with_get(harness: Harness, member: str) -> None:
    harness.bus.host_registered = False
    harness.tray.start()
    harness.clock.advance(30_000)
    harness.bus.auto_reply = False
    if member == "PropertiesChanged":
        harness.bus.signals[member](SERVICE, {"IsStatusNotifierHostRegistered": True}, [])
    else:
        harness.bus.signals[member]()
    assert not harness.tray.registered
    harness.icon.show.assert_not_called()
    harness.bus.calls[-1].reply(True)
    assert harness.tray.registered
    harness.bus.host_changed(False)
    assert not harness.tray.registered


def test_host_property_invalidation_refreshes_but_item_changes_do_not(harness: Harness) -> None:
    harness.tray.start()
    requests = len(harness.bus.calls)
    properties_changed = harness.bus.signals["PropertiesChanged"]
    # Добавление нашего SNI не должно вызывать hide/show и новую смену списка SNI.
    properties_changed(SERVICE, {"RegisteredStatusNotifierItems": ["astra"]}, [])
    properties_changed("unrelated.interface", {"IsStatusNotifierHostRegistered": False}, [])
    assert harness.tray.registered
    assert len(harness.bus.calls) == requests
    harness.icon.show.assert_called_once_with()
    harness.bus.host_registered = False
    properties_changed(SERVICE, {}, ["IsStatusNotifierHostRegistered"])
    assert not harness.tray.registered
    assert len(harness.bus.calls) == requests + 1


@pytest.mark.parametrize("kind", [SessionKind.FLY, SessionKind.OTHER])
def test_first_negative_qt_cache_is_never_poisoned(harness: Harness, kind: SessionKind) -> None:
    harness.detect.return_value = kind
    harness.bus.host_registered = False
    cached: bool | None = None
    register_calls = 0

    def qt_available() -> bool:
        nonlocal cached
        if cached is None:
            cached = harness.bus.host_registered is True
        return cached

    def qt_show() -> None:
        nonlocal register_calls
        # Qt вызывает доступность также внутри show(), поэтому он тоже отложен.
        if harness.icon.isSystemTrayAvailable():
            register_calls += 1
            harness.icon.isVisible.return_value = True

    harness.icon.isSystemTrayAvailable.side_effect = qt_available
    harness.icon.show.side_effect = qt_show
    harness.tray.start()
    harness.clock.advance(30_000)
    assert cached is None
    assert register_calls == 0
    harness.icon.isSystemTrayAvailable.assert_not_called()
    for _ in range(3):
        harness.bus.host_changed(True)
        assert harness.tray.registered
        assert cached is True
        harness.bus.host_registered = False
        harness.watcher.serviceUnregistered.emit(SERVICE)
        assert not harness.tray.registered
        harness.clock.advance(30_000)
    assert register_calls == 3


@pytest.mark.parametrize("ready", [False, True])
def test_registered_is_nonblocking_with_hung_owners(harness: Harness, ready: bool) -> None:
    harness.detect.return_value = SessionKind.KDE
    harness.bus.auto_reply = ready
    harness.tray.start()
    harness.bus.auto_reply = False  # Подменённые владельцы перестали отвечать.

    def blocked(*args: object) -> bool:
        sleep(0.6)
        return False

    harness.icon.isSystemTrayAvailable.side_effect = blocked
    harness.bus.transport.call.side_effect = blocked
    harness.bus.transport.interface.side_effect = blocked
    requests_before = len(harness.bus.calls)
    started = monotonic()
    assert harness.tray.registered is ready
    elapsed = monotonic() - started
    assert elapsed < 0.5
    assert len(harness.bus.calls) == requests_before
    harness.icon.isSystemTrayAvailable.assert_not_called()
    harness.bus.transport.call.assert_not_called()
    harness.bus.transport.interface.assert_not_called()


def test_owner_replacement_discards_cache_and_stale_reply(harness: Harness) -> None:
    harness.tray.start()
    harness.bus.auto_reply = False
    harness.watcher.serviceOwnerChanged.emit(SERVICE, ":1.10", ":1.20")
    assert not harness.tray.registered
    previous = harness.bus.calls[-1]
    harness.watcher.serviceOwnerChanged.emit(SERVICE, ":1.20", ":1.30")
    current = harness.bus.calls[-1]
    previous.reply(True)
    assert not harness.tray.registered
    current.reply(True)
    assert harness.tray.registered
    previous.fail()
    assert harness.tray.registered
    harness.watcher.serviceOwnerChanged.emit(SERVICE, ":1.30", "")
    assert not harness.tray.registered


def test_initial_plasma_reply_cannot_undo_owner_loss(harness: Harness) -> None:
    harness.detect.return_value = SessionKind.KDE
    harness.bus.auto_reply = False
    harness.tray.start()
    owner, host = harness.bus.calls
    host.reply(True)
    harness.watcher.serviceUnregistered.emit(PLASMA)
    owner.reply(":1.42")
    assert not harness.tray.registered
    harness.watcher.serviceRegistered.emit(PLASMA)
    assert harness.tray.registered


def test_late_reply_cannot_cross_stop_and_start(harness: Harness) -> None:
    harness.bus.auto_reply = False
    harness.tray.start()
    old = harness.bus.calls[-1]
    harness.tray.stop()
    old.reply(True)
    assert not harness.tray.registered
    harness.connection.disconnectFromBus.assert_called_once_with(
        harness.connection.connectToBus.call_args.args[1]
    )
    harness.tray.start()
    old.reply(True)
    assert not harness.tray.registered
    harness.bus.calls[-1].reply(True)
    assert harness.tray.registered


def test_failed_async_request_is_retried_without_blocking(harness: Harness) -> None:
    harness.bus.auto_reply = False
    harness.tray.start()
    harness.clock.advance(2000)
    assert len(harness.bus.calls) == 1  # Не плодим параллельные запросы.
    harness.bus.calls[-1].fail()
    assert not harness.tray.registered
    harness.clock.advance(1000)
    assert len(harness.bus.calls) == 2
    harness.bus.calls[-1].reply(True)
    assert harness.tray.registered


def test_send_failure_does_not_leave_request_pending(harness: Harness) -> None:
    harness.bus.transport.callWithCallback.side_effect = lambda message, success, error, timeout: (
        False if message.member() == "Get" else harness.bus.submit(message, success, error, timeout)
    )
    harness.tray.start()
    assert len(harness.bus.matches) == 4
    assert not harness.tray.registered
    harness.bus.transport.callWithCallback.side_effect = harness.bus.submit
    harness.clock.advance(1000)
    assert harness.tray.registered


def test_recovery_drops_only_obsolete_banner_before_flush(harness: Harness) -> None:
    from astra_voice.ui import notify

    calls = Mock()
    calls.attach_mock(harness.drop, "drop")
    calls.attach_mock(harness.flush, "flush")
    harness.send.return_value = 0
    notify.notify(UNAVAILABLE)
    notify.notify_indicators_lost()
    notify.notify_hotkey_not_grabbed()
    assert notify.pending_count() == 3
    harness.send.reset_mock()
    harness.send.return_value = 1
    harness.tray.start()
    assert calls.mock_calls == [call.drop(UNAVAILABLE), call.flush()]
    assert [c.args[0] for c in harness.send.call_args_list] == [
        "Запись остановлена",
        "Горячая клавиша не захвачена",
    ]
    assert notify.pending_count() == 0


@pytest.mark.parametrize("kind", list(SessionKind))
@pytest.mark.parametrize("pill_enabled", [False, True])
def test_panel_dependency_warning_once(
    harness: Harness, kind: SessionKind, pill_enabled: bool
) -> None:
    harness.detect.return_value = kind
    harness.tray.set_pill_enabled(pill_enabled)
    harness.panel_warning.assert_not_called()
    harness.tray.start()
    for _ in range(2):
        harness.watcher.serviceUnregistered.emit(SERVICE)
        harness.watcher.serviceRegistered.emit(SERVICE)
        harness.tray.set_pill_enabled(pill_enabled)
    harness.tray.stop()
    harness.tray.start()
    if kind == SessionKind.KDE and not pill_enabled:
        harness.panel_warning.assert_called_once_with()
    else:
        harness.panel_warning.assert_not_called()


def test_disabling_pill_after_registration_warns_once(harness: Harness) -> None:
    harness.detect.return_value = SessionKind.KDE
    harness.tray.start()
    harness.panel_warning.assert_not_called()
    harness.tray.set_pill_enabled(False)
    harness.tray.set_pill_enabled(True)
    harness.tray.set_pill_enabled(False)
    harness.panel_warning.assert_called_once_with()


def wait_for(qapp: QApplication, predicate: Callable[[], bool], timeout: float = 2.0) -> None:
    deadline = monotonic() + timeout
    while not predicate() and monotonic() < deadline:
        qapp.processEvents()
        sleep(0.001)
    assert predicate()


@pytest.mark.parametrize("kind", [SessionKind.KDE, SessionKind.OTHER])
@pytest.mark.parametrize("blocked_at", ["connection", "subscription"])
def test_public_methods_do_not_wait_for_hung_bus(
    harness: Harness,
    qapp: QApplication,
    monkeypatch: pytest.MonkeyPatch,
    kind: SessionKind,
    blocked_at: str,
) -> None:
    from astra_voice.ui import tray as module

    harness.detect.return_value = kind
    harness.bus.host_registered = False
    harness.bus.plasma_owner = ""
    harness.start_worker.side_effect = _start_bus_worker
    # Только таймер worker настоящий; часы ретраев GUI остаются управляемыми.
    monkeypatch.setattr(module, "QTimer", QtTimer)
    entered = Event()
    release = Event()
    gui_thread = get_ident()
    callers: list[int] = []

    def blocked(*args: object) -> bool:
        callers.append(get_ident())
        entered.set()
        # При ошибочном вызове из GUI снять задержку некому: он прождёт 5 с.
        release.wait(5.0)
        return False

    def connect_bus(*args: object) -> Mock:
        blocked(*args)
        return harness.bus.transport

    for name in ("call", "connect", "disconnect", "interface", "isConnected", "send"):
        getattr(harness.bus.transport, name).side_effect = blocked
    for name in ("addWatchedService", "setWatchedServices"):
        getattr(harness.watcher, name).side_effect = blocked
    harness.connection.sessionBus.side_effect = connect_bus
    harness.connection.disconnectFromBus.side_effect = blocked
    if blocked_at == "connection":
        harness.connection.connectToBus.side_effect = connect_bus

    try:
        started = monotonic()
        harness.tray.start()
        assert monotonic() - started < 1.0
        wait_for(qapp, entered.is_set)
        heartbeat = Event()
        QtTimer.singleShot(0, heartbeat.set)
        wait_for(qapp, heartbeat.is_set)
        assert not release.is_set()  # Цикл GUI работает и во время зависшей подписки.
        for action in (
            lambda: harness.tray.registered,
            lambda: harness.tray.set_state(TrayState.LISTENING),
        ):
            started = monotonic()
            action()
            assert monotonic() - started < 0.5
        assert not harness.tray.registered
        harness.clock.advance(2000)
        assert harness.clock.timers[1].due == 3000  # Ретраи не остановлены.
        harness.start_worker.assert_called_once()
        harness.icon.show.assert_not_called()
        started = monotonic()
        harness.tray.stop()
        assert monotonic() - started < 0.5
        # Повторный цикл не плодит потоки, пока прежний занят очисткой.
        harness.tray.start()
        harness.clock.advance(1000)
        harness.start_worker.assert_called_once()
        assert not harness.tray.registered
    finally:
        harness.tray.stop()
        release.set()
        wait_for(qapp, lambda: not module._bus_threads)
    assert callers and gui_thread not in callers
    harness.icon.show.assert_not_called()
    harness.bus.transport.call.assert_not_called()
    harness.bus.transport.interface.assert_not_called()
    harness.watcher.addWatchedService.assert_not_called()


@pytest.mark.parametrize(
    "member",
    [
        "NameOwnerChanged",
        "StatusNotifierHostRegistered",
        "StatusNotifierHostUnregistered",
        "PropertiesChanged",
    ],
)
@pytest.mark.parametrize("failure", ["connect", "send", "error", "timeout"])
def test_subscription_failure_is_closed_and_retried(
    harness: Harness,
    member: str,
    failure: str,
) -> None:
    if failure == "connect":
        harness.bus.transport.connect.side_effect = lambda service, path, interface, name, slot: (
            False if name == member else harness.bus.connect(service, path, interface, name, slot)
        )
    elif failure == "send":
        harness.bus.transport.callWithCallback.side_effect = (
            lambda message, success, error, timeout: (
                False
                if f"member='{member}'" in str(message.arguments())
                else harness.bus.submit(message, success, error, timeout)
            )
        )
    else:
        harness.bus.auto_match_reply = False
    harness.tray.start()
    previous = list(harness.bus.matches)
    if failure in ("error", "timeout"):
        for pending in previous:
            if f"member='{member}'" not in pending.message.arguments()[0]:
                pending.success(pending.message.createReply([]))
            elif failure == "error":
                pending.fail()
        harness.clock.advance(500)
        # Даже запоздалый успех просроченного AddMatch не доказывает подписку.
        for pending in previous:
            pending.success(pending.message.createReply([]))
    assert not harness.tray.registered
    assert not harness.bus.calls  # Get(true) не обходит неудавшуюся подписку.
    harness.icon.show.assert_not_called()
    harness.flush.assert_not_called()
    harness.bus.transport.connect.side_effect = harness.bus.connect
    harness.bus.transport.callWithCallback.side_effect = harness.bus.submit
    harness.bus.auto_match_reply = True
    harness.clock.advance(1000)
    assert harness.tray.registered
    harness.icon.show.assert_called_once_with()


def test_late_subscription_setup_cannot_register(harness: Harness) -> None:
    def slow_connect(
        service: str,
        path: str,
        interface: str,
        member: str,
        slot: Callable[..., None],
    ) -> bool:
        harness.clock.now += 501
        return harness.bus.connect(service, path, interface, member, slot)

    harness.bus.transport.connect.side_effect = slow_connect
    harness.tray.start()
    assert not harness.tray.registered
    harness.icon.show.assert_not_called()
    harness.bus.transport.connect.side_effect = harness.bus.connect
    # Не двигаем часы назад к пропущенным GUI-таймерам.
    harness.clock.timers[1].timeout.emit()
    assert harness.tray.registered


def test_failed_plasma_query_retries_with_confirmed_host(harness: Harness) -> None:
    harness.detect.return_value = SessionKind.KDE
    harness.bus.plasma_owner = ""
    harness.tray.start()
    assert not harness.tray.registered
    harness.bus.plasma_owner = ":1.50"
    harness.clock.advance(1000)
    assert harness.tray.registered
    assert sum(c.message.member() == "GetNameOwner" for c in harness.bus.calls) == 2


def test_dead_connection_is_replaced_on_subscription_retry(harness: Harness) -> None:
    harness.bus.transport.connect.return_value = False
    harness.bus.transport.connect.side_effect = None
    harness.bus.transport.isConnected.return_value = False
    harness.tray.start()
    assert not harness.tray.registered
    harness.bus.transport.connect.side_effect = harness.bus.connect
    harness.bus.transport.isConnected.return_value = True
    # Первое чтение состояния старого соединения сообщает потерю шины.
    harness.bus.transport.isConnected.side_effect = [False, True]
    harness.clock.advance(1000)
    assert harness.tray.registered
    assert harness.connection.connectToBus.call_count == 2
    harness.connection.disconnectFromBus.assert_called_once()


def test_real_worker_delivers_to_gui_and_cleans_up(
    harness: Harness,
    qapp: QApplication,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from astra_voice.ui import tray as module

    harness.start_worker.side_effect = _start_bus_worker
    monkeypatch.setattr(module, "QTimer", QtTimer)
    gui_thread = get_ident()
    deliveries: list[int] = []
    cleanup_entered = Event()
    release = Event()
    cleanup_threads: list[int] = []

    def show() -> None:
        deliveries.append(get_ident())
        harness.icon.isVisible.return_value = True

    def blocked(*args: object) -> bool:
        cleanup_threads.append(get_ident())
        cleanup_entered.set()
        release.wait(5.0)
        return False

    harness.icon.show.side_effect = show
    try:
        harness.tray.start()
        wait_for(qapp, lambda: harness.tray.registered)
        assert deliveries == [gui_thread]
        assert len(harness.bus.matches) == 4
        for name in ("call", "connect", "disconnect", "interface"):
            getattr(harness.bus.transport, name).side_effect = blocked
        harness.connection.disconnectFromBus.side_effect = blocked
        for action in (
            lambda: harness.tray.registered,
            lambda: harness.tray.set_state(TrayState.LISTENING),
        ):
            started = monotonic()
            action()
            assert monotonic() - started < 0.5
        assert harness.tray.registered
        started = monotonic()
        harness.tray.stop()
        assert monotonic() - started < 0.5
        assert not harness.tray.registered
        wait_for(qapp, cleanup_entered.is_set)
    finally:
        harness.tray.stop()
        release.set()
        wait_for(qapp, lambda: not module._bus_threads)
    harness.connection.disconnectFromBus.assert_called_once()
    assert cleanup_threads and gui_thread not in cleanup_threads
