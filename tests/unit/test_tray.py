"""Настоящее меню Qt с фейками значка, D-Bus, уведомлений и часов."""

from __future__ import annotations

import inspect
import logging
import os
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, get_type_hints
from unittest.mock import Mock

import pytest

from astra_voice.ui.tray_icons import TrayIconProvider, TrayState

if TYPE_CHECKING:
    from PyQt5.QtWidgets import QApplication, QMenu

    from astra_voice.ui.tray import Tray

pytestmark = pytest.mark.unit
SERVICE = "org.kde.StatusNotifierWatcher"


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
    watcher_factory: Mock
    connection: Mock
    banner: Mock
    flush: Mock
    send: Mock
    clock: Clock

    @property
    def menu(self) -> QMenu:
        menu: QMenu = self.icon.setContextMenu.call_args.args[0]
        return menu


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt5.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    assert isinstance(app, QApplication)
    return app


@pytest.fixture
def harness(qapp: QApplication, monkeypatch: pytest.MonkeyPatch) -> Iterator[Harness]:
    from astra_voice.ui import notify
    from astra_voice.ui import tray as module

    # Даже случайный обход фабрики не должен попасть на панель заказчика.
    monkeypatch.setattr(
        module, "QSystemTrayIcon", Mock(side_effect=AssertionError("Real tray forbidden"))
    )
    connection = Mock()
    monkeypatch.setattr(module, "QDBusConnection", connection)
    watcher = Mock(serviceRegistered=Signal(), serviceUnregistered=Signal())
    watcher_factory = Mock(return_value=watcher)
    watcher_factory.WatchForRegistration = 1
    watcher_factory.WatchForUnregistration = 2
    monkeypatch.setattr(module, "QDBusServiceWatcher", watcher_factory)
    banner = Mock()
    monkeypatch.setattr(notify, "notify_tray_unavailable", banner)
    notify.reset_state()
    send = Mock(return_value=1)
    monkeypatch.setattr(notify, "_send", send)
    flush = Mock(wraps=notify.flush_pending)
    monkeypatch.setattr(notify, "flush_pending", flush)
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
        watcher_factory,
        connection,
        banner,
        flush,
        send,
        clock,
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
    harness.watcher_factory.assert_called_once_with(
        SERVICE, harness.connection.sessionBus(), 3, harness.tray
    )
    harness.tray.start()
    harness.clock.advance(60_000)
    harness.icon.show.assert_called_once_with()
    harness.banner.assert_not_called()
    harness.tray.stop()
    assert not harness.tray.registered
    harness.watcher.setWatchedServices.assert_called_once_with([])
    harness.watcher.deleteLater.assert_called_once_with()
    harness.watcher.serviceRegistered.emit(SERVICE)
    harness.watcher.serviceUnregistered.emit(SERVICE)
    harness.clock.advance(60_000)
    harness.icon.show.assert_called_once_with()
    harness.banner.assert_not_called()


def test_watcher_return_after_timeout_registers_and_keeps_banner_once(harness: Harness) -> None:
    harness.icon.isSystemTrayAvailable.return_value = False
    harness.tray.start()
    assert not harness.tray.registered
    harness.clock.advance(999)
    assert harness.icon.isSystemTrayAvailable.call_count == 1
    harness.clock.advance(1)
    assert harness.icon.isSystemTrayAvailable.call_count == 2
    harness.clock.advance(28_999)
    assert harness.icon.isSystemTrayAvailable.call_count == 30
    harness.banner.assert_not_called()
    harness.tray.start()  # Повторный start не продлевает текущий срок.
    harness.clock.advance(1)
    harness.banner.assert_called_once_with()
    assert not harness.tray.registered
    harness.icon.show.assert_not_called()
    count = harness.icon.isSystemTrayAvailable.call_count
    harness.clock.advance(60_000)
    assert harness.icon.isSystemTrayAvailable.call_count == count
    harness.icon.isSystemTrayAvailable.return_value = True
    harness.watcher.serviceRegistered.emit(SERVICE)
    assert harness.tray.registered
    harness.icon.show.assert_called_once_with()
    harness.clock.advance(60_000)
    harness.banner.assert_called_once_with()
    harness.icon.isSystemTrayAvailable.return_value = False
    harness.watcher.serviceUnregistered.emit(SERVICE)
    assert not harness.tray.registered
    harness.clock.advance(30_000)
    harness.banner.assert_called_once_with()
    harness.tray.stop()
    harness.tray.start()
    harness.clock.advance(30_000)
    harness.banner.assert_called_once_with()


def test_watcher_registration_retries_immediately(harness: Harness) -> None:
    harness.icon.isSystemTrayAvailable.return_value = False
    harness.tray.start()
    harness.clock.advance(250)
    harness.icon.isSystemTrayAvailable.return_value = True
    harness.watcher.serviceRegistered.emit("unrelated.service")
    assert not harness.tray.registered
    harness.watcher.serviceRegistered.emit(SERVICE)
    assert harness.tray.registered
    assert harness.clock.now == 250
    harness.icon.show.assert_called_once_with()
    harness.clock.advance(60_000)
    harness.banner.assert_not_called()


def test_periodic_retry_can_register(harness: Harness) -> None:
    harness.icon.isSystemTrayAvailable.return_value = False
    harness.tray.start()
    harness.icon.isSystemTrayAvailable.return_value = True
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
    harness.icon.isSystemTrayAvailable.return_value = False
    harness.clock.advance(29_999)
    harness.banner.assert_not_called()
    harness.clock.advance(1)
    harness.banner.assert_called_once_with()


def test_two_plasmashell_restarts_register_again(harness: Harness) -> None:
    harness.tray.start()
    assert harness.tray.registered
    for restart in range(2):
        # Сигнал потери должен сработать даже при устаревшем ответе Qt.
        harness.watcher.serviceUnregistered.emit(SERVICE)
        assert not harness.tray.registered
        assert not harness.icon.isVisible()
        harness.icon.isSystemTrayAvailable.return_value = False
        harness.clock.advance(2000)
        assert not harness.tray.registered
        harness.icon.isSystemTrayAvailable.return_value = True
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
    harness.icon.isSystemTrayAvailable.return_value = False
    harness.tray.start()
    # Сигнал службы пришёл раньше доставки просроченного timeout.
    harness.clock.now = 30_001
    harness.icon.isSystemTrayAvailable.return_value = True
    harness.watcher.serviceRegistered.emit(SERVICE)
    assert harness.tray.registered
    harness.icon.show.assert_called_once_with()
    harness.clock.advance(60_000)
    harness.banner.assert_not_called()


def test_delayed_retry_does_not_extend_deadline(harness: Harness) -> None:
    harness.icon.isSystemTrayAvailable.return_value = False
    harness.tray.start()
    # В отличие от нового Watcher, запоздалый обычный ретрай сохраняет срок.
    harness.clock.now = 30_001
    harness.icon.isSystemTrayAvailable.return_value = True
    harness.clock.timers[1].timeout.emit()
    assert not harness.tray.registered
    harness.icon.show.assert_not_called()
    harness.banner.assert_called_once_with()


@pytest.mark.parametrize("loss", ["availability", "hidden"])
def test_registered_rechecks_tray_without_bus_signal(harness: Harness, loss: str) -> None:
    harness.tray.start()
    assert harness.tray.registered
    if loss == "availability":
        harness.icon.isSystemTrayAvailable.return_value = False
    else:
        harness.icon.isVisible.return_value = False
    assert harness.tray.registered is False
    assert not harness.icon.isVisible()
    harness.icon.isSystemTrayAvailable.return_value = True
    harness.clock.advance(999)
    harness.icon.show.assert_called_once_with()
    harness.clock.advance(1)
    assert harness.tray.registered
    assert harness.icon.show.call_count == 2


def test_repeated_registered_checks_do_not_extend_retry_deadline(harness: Harness) -> None:
    harness.tray.start()
    harness.icon.isSystemTrayAvailable.return_value = False
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
        harness.icon.isSystemTrayAvailable.return_value = False
    elif failure == "empty_icon":
        harness.provider.has_icon.return_value = False
    else:
        harness.watcher.serviceUnregistered.emit(SERVICE)
    registered = harness.tray.registered
    assert registered is False
    assert not harness.icon.isVisible()
    assert not indicator_visible(pill_visible=False, tray_registered=registered)


def test_stop_cancels_pending_timers(harness: Harness) -> None:
    harness.icon.isSystemTrayAvailable.return_value = False
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
