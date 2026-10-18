"""Обязательный статичный индикатор записи и меню системного лотка."""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Sequence
from functools import partial
from itertools import count
from queue import Empty, Queue
from threading import Lock, Thread
from time import monotonic
from typing import Any

from PyQt5 import sip
from PyQt5.QtCore import (
    QCoreApplication,
    QEvent,
    QMetaObject,
    QObject,
    Qt,
    QTimer,
    pyqtSlot,
)
from PyQt5.QtDBus import (
    QDBus,
    QDBusArgument,
    QDBusConnection,
    QDBusMessage,
    QDBusVariant,
)
from PyQt5.QtWidgets import QAction, QActionGroup, QMenu, QSystemTrayIcon

from astra_voice.platform.session import SessionKind, detect
from astra_voice.platform.x11 import XEmbedTrayProbe
from astra_voice.ui import notify
from astra_voice.ui.formatting import clean_display_name
from astra_voice.ui.tray_icons import TrayIconProvider
from astra_voice.ui.tray_icons import TrayState as TrayState

_WATCHER_SERVICE = "org.kde.StatusNotifierWatcher"
_WATCHER_PATH = "/StatusNotifierWatcher"
_PLASMA_SERVICE = "org.kde.plasmashell"
_PROPERTIES_INTERFACE = "org.freedesktop.DBus.Properties"
_HOST_PROPERTY = "IsStatusNotifierHostRegistered"
_ITEMS_PROPERTY = "RegisteredStatusNotifierItems"
_DBUS_SERVICE = "org.freedesktop.DBus"
_DBUS_PATH = "/org/freedesktop/DBus"
_UNAVAILABLE_SUMMARY = "Значок не появился на панели"
_CALL_TIMEOUT_MS = 500
# Setup последователен: соединение и до 6 AddMatch по _CALL_TIMEOUT_MS каждый, причём
# за ним в очереди могут стоять запросы. Готовность засчитывается, только если ответ
# пришёл за _SETUP_FRESH_S (медленная шина = панель не готова, повтор по таймеру 1 с).
_SETUP_FRESH_S = 0.5
# Если результат setup так и не пришёл (демон умер, команда отброшена), повторяем setup.
_SETUP_STALE_S = 5.0
_logger = logging.getLogger(__name__)
_BusReply = tuple[int, list[Any]]


def _plain_dbus_value(value: Any) -> Any:
    if isinstance(value, QDBusArgument):
        _logger.warning("Непреобразованный аргумент D-Bus отброшен")
        return None
    if isinstance(value, QDBusVariant):
        return _plain_dbus_value(value.variant())
    if isinstance(value, list):
        return [_plain_dbus_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_plain_dbus_value(item) for item in value)
    if isinstance(value, dict):
        return {key: _plain_dbus_value(item) for key, item in value.items()}
    return value


# Получатель и именованные соединения намеренно живут до выхода из процесса:
# QtDBus может обращаться к QObject из своего потока под внутренними мьютексами.
_bus_receiver: _TrayBusReceiver | None = None
_bus_transport: _TrayBusTransport | None = None
_bus_generations = count(1)
_bus_names = count(1)
# Демон будит получателя через invokeMethod: в очереди Qt лежит только C++-событие
# MetaCall, для удаления которого (в т. ч. в ~QApplication) GIL не нужен.
# Это просто QEvent.MetaCall; имя — для тестов, которые доставляют посты синхронно
# через sendPostedEvents(_bus_receiver, _bus_wake_event_type).
_bus_wake_event_type = QEvent.MetaCall
_bus_results: Queue[tuple[int, str, Any]] = Queue()
_BusCommand = tuple[int, str, Any]


class _TrayBusReceiver(QObject):
    """Бессмертный получатель хуков и ответов, всегда в потоке GUI."""

    def __init__(self) -> None:
        super().__init__()
        self.tray: Tray | None = None

    def _deliver(self, generation: int, event: str, payload: Any) -> None:
        tray = self.tray
        if tray is not None and not sip.isdeleted(tray) and tray._running:
            tray._bus_event(generation, event, payload)

    @pyqtSlot()
    def _drain(self) -> None:
        while True:
            try:
                result = _bus_results.get_nowait()
            except Empty:
                return
            self._deliver(*result)

    def _hook(self, event: str, payload: Any) -> None:
        tray = self.tray
        if tray is not None and not sip.isdeleted(tray):
            self._deliver(tray._bus_generation, event, payload)

    @pyqtSlot(str, str, str)
    def _owner_changed(self, service: str, old_owner: str, new_owner: str) -> None:
        self._hook("owner", (service, old_owner, new_owner))

    @pyqtSlot()
    def _host_changed(self) -> None:
        self._hook("host", None)

    @pyqtSlot(str)
    def _item_registered(self, item: str) -> None:
        self._hook("item_registered", item)

    @pyqtSlot(str)
    def _item_unregistered(self, item: str) -> None:
        self._hook("item_unregistered", item)

    @pyqtSlot(str, "QVariantMap", "QStringList")
    def _properties_changed(
        self, interface: str, changed: dict[str, Any], invalidated: list[str]
    ) -> None:
        self._hook("properties", (interface, _plain_dbus_value(changed), invalidated))


def _get_bus_receiver() -> _TrayBusReceiver:
    global _bus_receiver
    if _bus_receiver is None:
        app = QCoreApplication.instance()
        if app is None or app.thread() != app.thread().currentThread():
            raise RuntimeError("Получатель D-Bus должен создаваться в потоке GUI")
        _bus_receiver = _TrayBusReceiver()
        sip.transferto(_bus_receiver, None)
    return _bus_receiver


def _post_bus_result(generation: int, event: str, payload: Any) -> None:
    """Положить результат и разбудить получатель; демон зовёт только через затвор."""
    _bus_results.put((generation, event, payload))
    # Получатель создан до запуска демона и никогда не удаляется.
    assert _bus_receiver is not None
    QMetaObject.invokeMethod(_bus_receiver, "_drain", Qt.ConnectionType.QueuedConnection)


class _TrayBusTransport:
    """Один Python-демон; всё блокирующее общение с шиной выполняется здесь."""

    def __init__(self, receiver: _TrayBusReceiver) -> None:
        self.receiver = receiver
        self.commands: Queue[_BusCommand | None] = Queue()
        self.thread = Thread(target=self._run, daemon=True, name="astra-voice-tray-dbus")
        self.bus: QDBusConnection | None = None
        self.bus_name: str | None = None
        self.connections: set[str] = set()
        self.matches: set[str] = set()
        self.stopping = False
        self.is_kde = False
        # Затвор постов в Qt: после close() демон не трогает очередь событий Qt,
        # даже если пережил join (иначе взаимная блокировка с ~QApplication).
        self.gate = Lock()
        self.closed = False

    def post(self, generation: int, event: str, payload: Any) -> None:
        with self.gate:
            if not self.closed:
                _post_bus_result(generation, event, payload)

    def close(self) -> None:
        with self.gate:
            self.closed = True

    def _run(self) -> None:
        command = self.commands.get()
        while command is not None:
            # Склеиваем только соседние setup, сохраняя порядок запросов и остановки.
            following: _BusCommand | None = None
            has_following = False
            if command[1] == "setup":
                while True:
                    try:
                        following = self.commands.get_nowait()
                    except Empty:
                        break
                    if following is not None and following[1] == "setup":
                        command = following
                    else:
                        has_following = True
                        break
            if self.closed:
                # После close() команды из очереди не выполняются: демон только выходит.
                return
            self.execute(*command)
            command = following if has_following else self.commands.get()

    def execute(self, generation: int, operation: str, payload: Any) -> None:
        try:
            if operation == "setup":
                ready = self._setup()
                self.post(generation, "ready" if ready else "failed", ready and self.is_kde)
            elif operation == "request":
                serial, message = payload
                reply = self._call(message)
                self.post(generation, "reply", (serial, reply))
        except Exception:
            _logger.exception("Ошибка наблюдения за панелью через D-Bus")
            if operation == "request":
                self.post(generation, "reply", (payload[0], None))
            else:
                self.post(generation, "failed", None)

    def _call(self, message: QDBusMessage) -> _BusReply | None:
        if self.bus is None:
            return None
        reply = self.bus.call(message, QDBus.Block, _CALL_TIMEOUT_MS)
        if reply.type() == QDBusMessage.ErrorMessage:
            return int(reply.type()), [reply.errorName()]
        return int(reply.type()), [_plain_dbus_value(arg) for arg in reply.arguments()]

    def _setup(self) -> bool:
        self.is_kde = detect() == SessionKind.KDE
        if self.bus is not None and not self.bus.isConnected():
            if self.connections:
                # К мёртвому соединению привязаны хуки получателя: его не разбираем,
                # оно остаётся у QtDBus до выхода процесса, берём новое имя.
                self.bus_name = None
            elif self.bus_name is not None:
                # Хуков не было — освобождаем имя и переподключаемся под ним же,
                # иначе каждая неудачная попытка оставляла бы новое соединение.
                QDBusConnection.disconnectFromBus(self.bus_name)
            self.bus = None
            self.connections.clear()
            self.matches.clear()
        if self.bus is None:
            if self.bus_name is None:
                self.bus_name = f"astra-voice-tray-{next(_bus_names)}"
            self.bus = QDBusConnection.connectToBus(QDBusConnection.SessionBus, self.bus_name)
        if not self.bus.isConnected():
            return False
        subscriptions: list[tuple[str, str, str, str, Callable[..., None]]] = [
            (
                _DBUS_SERVICE,
                _DBUS_PATH,
                _DBUS_SERVICE,
                "NameOwnerChanged",
                self.receiver._owner_changed,
            ),
            *[
                (
                    _WATCHER_SERVICE,
                    _WATCHER_PATH,
                    _WATCHER_SERVICE,
                    member,
                    self.receiver._host_changed,
                )
                for member in ("StatusNotifierHostRegistered", "StatusNotifierHostUnregistered")
            ],
            *[
                (
                    _WATCHER_SERVICE,
                    _WATCHER_PATH,
                    _WATCHER_SERVICE,
                    member,
                    slot,
                )
                for member, slot in (
                    ("StatusNotifierItemRegistered", self.receiver._item_registered),
                    ("StatusNotifierItemUnregistered", self.receiver._item_unregistered),
                )
            ],
            (
                _WATCHER_SERVICE,
                _WATCHER_PATH,
                _PROPERTIES_INTERFACE,
                "PropertiesChanged",
                self.receiver._properties_changed,
            ),
        ]
        for service, path, interface, member, slot in subscriptions:
            rule = (
                f"type='signal',sender='{service}',path='{path}',"
                f"interface='{interface}',member='{member}'"
            )
            if rule not in self.connections:
                if not self.bus.connect(service, path, interface, member, slot):
                    return False
                self.connections.add(rule)
            if rule not in self.matches:
                message = QDBusMessage.createMethodCall(
                    _DBUS_SERVICE, _DBUS_PATH, _DBUS_SERVICE, "AddMatch"
                )
                message.setArguments([rule])
                reply = self._call(message)
                if reply is None or reply[0] != QDBusMessage.ReplyMessage:
                    return False
                self.matches.add(rule)
        return True


def _send_bus_command(generation: int, operation: str, payload: Any) -> None:
    """Точка подмены транспорта; получатель уже привязан в GUI до отправки."""
    global _bus_transport
    if _bus_transport is None:
        _bus_transport = _TrayBusTransport(_get_bus_receiver())
        _bus_transport.thread.start()
    if not _bus_transport.stopping:
        _bus_transport.commands.put((generation, operation, payload))


def shutdown_bus_threads(timeout_ms: int = 1000) -> None:
    """Остановить демон с ограниченным ожиданием, не трогая QtDBus и получателя.

    Вызывать до разрушения QApplication: затвор закрывается здесь, и только после
    этого запоздалые ответы демона гарантированно не попадают в очередь событий Qt.
    """
    transport = _bus_transport
    if transport is None:
        return
    # Затвор закрывается до join: после возврата демон уже не постит в Qt.
    transport.close()
    if not transport.stopping:
        transport.stopping = True
        transport.commands.put(None)
    transport.thread.join(max(0, timeout_ms) / 1000)
    if transport.thread.is_alive():
        _logger.warning("Поток D-Bus не завершился за время ожидания")


class Tray(QObject):
    """Владеет значком; stop() предназначен для завершения работы приложения.

    Фабрика создаёт единственный значок; Qt выбирает бэкенд в его конструкторе.
    Доказательство О4 — наш элемент в списке SNI; XEmbed лишь пытается показать
    значок. isSystemTrayAvailable() не вызывается: он меняет кэш generic/Fly Qt.
    Тесты подменяют отправку команд транспорта до start().
    """

    def __init__(
        self,
        provider: TrayIconProvider,
        *,
        hotkey: str = "Ctrl+Space",
        tray_factory: Callable[[], Any] | None = None,
        xembed_probe: XEmbedTrayProbe | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.on_cancel: Callable[[], None] | None = None
        self.on_open: Callable[[], None] | None = None
        self.on_settings: Callable[[], None] | None = None
        self.on_copy_last: Callable[[], None] | None = None
        self.on_command_details: Callable[[], None] | None = None
        self.on_launch_cowork: Callable[[], None] | None = None
        self._command_status = ""
        self._command_mode = False
        self.on_check_updates: Callable[[], None] | None = None
        self.on_model_recheck: Callable[[], None] | None = None
        self.on_about: Callable[[], None] | None = None
        self.on_quit: Callable[[], None] | None = None
        self.on_model_selected: Callable[[str], None] | None = None
        self._provider = provider
        self._hotkey = hotkey
        factory = tray_factory if tray_factory is not None else lambda: QSystemTrayIcon()
        self._tray = factory()
        # Qt выбирает бэкенд в конструкторе значка; stop/start его не меняют.
        self._sni_backend_proven = False
        self._tray.setParent(self)
        self._tray.activated.connect(self._on_activated)
        self._registered = False
        self._running = False
        self._banner_shown = False
        self._deadline: float | None = None
        self._retry_expired = False
        self._xembed_probe = xembed_probe if xembed_probe is not None else XEmbedTrayProbe()
        self._xembed_owner_id: int | None = None
        self._xembed_allowed = False
        self._shown_unconfirmed = False
        self._xembed_polling = False
        self._xembed_poll_at = 0.0
        self._item_registered = False
        self._item_names: list[str] = []
        self._item_pids: dict[str, bool] = {}
        self._pending_item_signals: dict[str, tuple[str, bool]] = {}
        # QtDBusTrayIcon может иметь отдельное соединение: принимаем только PID.
        self._item_prefix = f"org.kde.StatusNotifierItem-{os.getpid()}-"
        self._path: str | None = None
        self._bus_generation = 0
        self._subscriptions_ready = False
        self._setup_pending = False
        self._setup_deadline = 0.0
        self._serial = 0
        self._requests: dict[str, tuple[int, Callable[[_BusReply | None], None]]] = {}
        self._host_registered = False
        self._is_kde = False
        self._plasma_alive = False
        self._plasma_known = False
        self._plasma_retry_at = 0.0
        self._pill_enabled = True
        self._panel_warning_shown = False
        self._has_last_text = False
        self._updates_enabled = False
        self._model_recheck_enabled = False

        self._done_timer = QTimer(self)
        self._done_timer.setSingleShot(True)
        self._done_timer.setTimerType(Qt.PreciseTimer)
        self._done_timer.timeout.connect(self._done_expired)
        self._retry_timer = QTimer(self)
        self._retry_timer.setInterval(1000)
        self._retry_timer.timeout.connect(self._retry_tick)
        self._deadline_timer = QTimer(self)
        self._deadline_timer.setSingleShot(True)
        self._deadline_timer.setTimerType(Qt.PreciseTimer)
        self._deadline_timer.timeout.connect(self._registration_expired)
        self._xembed_timer = QTimer(self)
        self._xembed_timer.setInterval(10_000)
        self._xembed_timer.timeout.connect(self._poll_xembed_owner)

        # Сохраняем QMenu: у него нет QWidget-родителя, а значок им не владеет.
        self._menu = QMenu()
        self._menu.setObjectName("astravoice-tray-menu")
        self._status_action = self._menu.addAction("Готов")
        self._status_action.setEnabled(False)
        self._download_action = QAction(self._menu)
        self._download_action.setEnabled(False)
        self._download_action.setVisible(False)
        self._menu.addSeparator()
        self._cancel_action = self._add_action("Отмена", lambda: self._invoke(self.on_cancel))
        self._model_action = self._menu.addAction("Модель не установлена")
        self._model_menu = QMenu("Модель", self._menu)
        self._model_group = QActionGroup(self._model_menu)
        self._model_group.setExclusive(True)
        self._model_recheck_action = self._add_action(
            "Проверить модель ещё раз", lambda: self._invoke(self.on_model_recheck)
        )
        self._copy_action = self._add_action(
            "Скопировать последний текст", lambda: self._invoke(self.on_copy_last)
        )
        self._command_details_action = self._add_action(
            "Что случилось с командой", lambda: self._invoke(self.on_command_details)
        )
        self._command_launch_action = self._add_action(
            "Запустить Astra Cowork", lambda: self._invoke(self.on_launch_cowork)
        )
        self.set_command_feedback("")
        self._menu.addSeparator()
        settings = self._add_action("Настройки…", lambda: self._invoke(self.on_settings))
        settings.setShortcut("Ctrl+,")
        self._updates_action = self._add_action(
            "Проверить обновления", lambda: self._invoke(self.on_check_updates)
        )
        self._add_action("О программе", lambda: self._invoke(self.on_about))
        self._menu.addSeparator()
        quit_action = self._add_action("Выход", lambda: self._invoke(self.on_quit))
        quit_action.setShortcut("Ctrl+Q")
        self._tray.setContextMenu(self._menu)
        self.set_state(TrayState.IDLE)
        self.set_models([], None)

    @staticmethod
    def _invoke(callback: Callable[[], None] | None) -> None:
        if callback is not None:
            callback()

    @pyqtSlot(QSystemTrayIcon.ActivationReason)
    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            self._invoke(self.on_open)

    def _add_action(self, label: str, callback: Callable[[], None]) -> QAction:
        action = QAction(label, self._menu)
        action.triggered.connect(callback)
        self._menu.addAction(action)
        return action

    def set_state(self, state: TrayState, tooltip: str | None = None) -> None:
        self._done_timer.stop()
        self._state = state
        self._command_status = (
            tooltip
            if tooltip
            in ("Слушаю команду", "Слушаю команду…", "Отправляю помощнику…", "Передано помощнику")
            else ""
        )
        has_icon = self._has_icon()
        if has_icon:
            self._tray.setIcon(self._display_icon())
        else:
            self._invalidate_registration()
        self._update_tooltip(tooltip)
        self._update_menu()
        if state == TrayState.DONE:
            self._done_timer.start(800)
        if self._running and not self.registered and not self._shown_unconfirmed:
            if self._deadline is None:
                self._begin_retry()
            if has_icon:
                self._try_register()

    def _display_icon(self) -> Any:
        if self._command_mode:
            return self._provider.command_icon(self._state)
        return self._provider.icon(self._state)

    def set_command_mode(self, active: bool) -> None:
        """Роль принадлежит runtime и сохраняется до новой текстовой записи.

        Её смена не меняет статус записи, состояние последнего текста или меню
        восстановления. Поэтому обработка, доставка и восстановление не теряют
        командную метку при переходе базового состояния в IDLE/ERROR.
        """
        active = bool(active)
        if active == self._command_mode:
            return
        self._command_mode = active
        if self._has_icon():
            self._tray.setIcon(self._display_icon())
        else:
            self._invalidate_registration()

    def set_command_feedback(self, text: str, *, can_launch: bool = False) -> None:
        """Действия восстановления доступны даже без службы уведомлений."""
        self._menu.removeAction(self._command_details_action)
        self._menu.removeAction(self._command_launch_action)
        self._command_details_action.setVisible(bool(text))
        self._command_launch_action.setVisible(bool(text) and can_launch)
        if text:
            actions = self._menu.actions()
            after_copy = actions[actions.index(self._copy_action) + 1]
            self._menu.insertAction(after_copy, self._command_details_action)
            if can_launch:
                self._menu.insertAction(after_copy, self._command_launch_action)

    def set_download_status(self, text: str) -> None:
        """Показывает прогресс сразу под состоянием, пока есть текст загрузки."""
        text = clean_display_name(text, for_menu=True)
        self._download_action.setText(text)
        self._download_action.setVisible(bool(text))
        # Отсутствующий прогресс не меняет состав обычного меню и его сочетания.
        if text:
            if self._download_action not in self._menu.actions():
                self._menu.insertAction(self._menu.actions()[1], self._download_action)
        else:
            self._menu.removeAction(self._download_action)
        self._update_tooltip()

    def _update_tooltip(self, tooltip: str | None = None) -> None:
        if self._download_action.isVisible():
            tooltip = "Astra Voice — загружается модель"
        elif tooltip is None:
            tooltip = self._provider.tooltip(self._state, hotkey=self._hotkey)
        self._tray.setToolTip(clean_display_name(tooltip))

    def _done_expired(self) -> None:
        if self._state == TrayState.DONE:
            self.set_state(TrayState.IDLE)

    def _update_menu(self) -> None:
        self._status_action.setText(
            self._command_status
            or {
                TrayState.LISTENING: "Слушаю…",
                TrayState.PROCESSING: "Распознаю…",
                TrayState.ERROR: "Микрофон недоступен",
                TrayState.NOKEY: "Горячая клавиша не захвачена",
            }.get(self._state, "Готов")
        )
        self._cancel_action.setEnabled(self._state in (TrayState.LISTENING, TrayState.PROCESSING))
        self._copy_action.setEnabled(self._has_last_text)
        self._updates_action.setEnabled(self._updates_enabled)
        self._model_recheck_action.setEnabled(self._model_recheck_enabled)
        self._model_recheck_action.setVisible(self._model_recheck_enabled)

    def set_models(self, models: Sequence[str | tuple[str, str]], active: str | None) -> None:
        """Пары (id, имя из настроек); прежние строки используют имя как id."""
        entries = [(item, item) if isinstance(item, str) else item for item in models]
        self._active_model = active
        self._model_menu.clear()
        self._model_action.setMenu(None)
        self._model_action.setEnabled(len(entries) > 1)
        if not entries:
            self._model_action.setText("Модель не установлена")
        elif len(entries) == 1:
            self._model_action.setText(
                f"Модель: {clean_display_name(entries[0][1], for_menu=True)}"
            )
        else:
            self._model_action.setText("Модель")
            self._model_action.setMenu(self._model_menu)
            for model_id, name in entries:
                action = QAction(clean_display_name(name, for_menu=True), self._model_menu)
                action.setData(model_id)
                action.setCheckable(True)
                action.setChecked(model_id == active)
                self._model_group.addAction(action)
                action.triggered.connect(partial(self._select_model, action))
                self._model_menu.addAction(action)
        self._update_menu()

    def _select_model(self, action: QAction, checked: bool = False) -> None:
        model_id = str(action.data())
        # QAction ставит галочку до triggered; отказ/пауза переключателя не должны
        # оставлять её у модели, которая не стала рабочей в настройках.
        for item in self._model_menu.actions():
            item.setChecked(item.data() == self._active_model)
        if self.on_model_selected is not None:
            self.on_model_selected(model_id)

    def set_has_last_text(self, value: bool) -> None:
        self._has_last_text = bool(value)
        self._update_menu()

    def set_updates_enabled(self, value: bool) -> None:
        self._updates_enabled = value
        self._update_menu()

    def set_model_recheck_enabled(self, value: bool) -> None:
        self._model_recheck_enabled = value
        self._update_menu()

    def set_pill_enabled(self, value: bool) -> None:
        """Сообщить, включён ли отдельный указатель записи в настройках."""
        self._pill_enabled = value
        self._warn_panel_dependency()

    def _warn_panel_dependency(self) -> None:
        if (
            self._is_kde
            and not self._pill_enabled
            and not self._panel_warning_shown
            and self.registered
        ):
            self._panel_warning_shown = True
            notify.notify_tray_depends_on_panel()

    @property
    def registered(self) -> bool:
        """О4: только кэш шины и локальное состояние Qt, без запросов к владельцу."""
        if self._registered and (
            not self._registration_ready() or not self._tray.isVisible() or not self._has_icon()
        ):
            self._invalidate_registration()
        return self._registered

    @property
    def visible_for_close(self) -> bool:
        """Локальный показ достаточен для закрытия окна, но не для О4."""
        if self.registered:
            return True
        if self._shown_unconfirmed and (not self._tray.isVisible() or not self._has_icon()):
            self._invalidate_registration()
        return self._shown_unconfirmed

    def _registration_ready(self) -> bool:
        return self._panel_ready() and self._item_registered

    def _set_path(self, path: str) -> None:
        if self._path != path:
            self._path = path
            suffix = (
                " (без подтверждения видимости — индикация через пилюлю)"
                if path == "XEmbed"
                else ""
            )
            _logger.info("трей: путь %s%s", path, suffix)

    def _poll_xembed_owner(self) -> None:
        """Редкая сверка SNI и владельца XEmbed, всегда из GUI-потока."""
        if (
            not self._running
            or self._retry_expired
            or not (self._registered or self._shown_unconfirmed)
        ):
            self._xembed_polling = False
            self._xembed_timer.stop()
            return
        if self._path == "XEmbed" and not self._is_kde and self._refresh_xembed_owner():
            self._show_available_path()
        if self._subscriptions_ready and (self._registered or self._shown_unconfirmed):
            # _host_reply запрашивает список элементов; один Get на свойство.
            self._query_host()

    def _refresh_xembed_owner(self) -> bool:
        if self._is_kde or monotonic() < self._xembed_poll_at:
            return False
        self._xembed_poll_at = monotonic() + 10.0
        try:
            owner = self._xembed_probe.owner()
        except Exception:
            self._xembed_probe.close()
            owner = None
        if owner != self._xembed_owner_id:
            self._xembed_owner_id = owner
            if self._path == "XEmbed":
                self._invalidate_registration()
            return True
        return False

    def _panel_ready(self) -> bool:
        # KDED переживает plasmashell и продолжает сообщать host=True.
        return (
            self._subscriptions_ready
            and self._host_registered
            and (not self._is_kde or self._plasma_alive)
        )

    def _has_icon(self) -> bool:
        if (
            not self._display_icon().isNull()
            if self._command_mode
            else self._provider.has_icon(self._state)
        ):
            return True
        _logger.warning(
            "Пустой значок трея для состояния %s; регистрация отложена", self._state.value
        )
        return False

    def _invalidate_registration(self, *, preserve_shown: bool = False) -> None:
        keep_shown = (
            preserve_shown
            and not self._sni_backend_proven
            and (self._registered or self._shown_unconfirmed)
        )
        self._registered = False
        self._item_registered = False
        self._cancel_request(_ITEMS_PROPERTY)
        self._item_names = []
        self._shown_unconfirmed = bool(keep_shown and self._tray.isVisible())
        if not self._shown_unconfirmed:
            self._tray.hide()
        if self._running and self._deadline is None:
            self._begin_retry()

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._bus_generation = next(_bus_generations)
        _get_bus_receiver().tray = self
        self._retry_expired = False
        self._begin_retry()
        self._try_register()

    def _setup_bus(self) -> None:
        if self._setup_pending:
            return
        self._setup_pending = True
        self._setup_deadline = monotonic() + _SETUP_FRESH_S
        _send_bus_command(self._bus_generation, "setup", None)

    def _bus_event(self, generation: int, event: str, payload: Any) -> None:
        if generation != self._bus_generation or not self._running:
            return
        if event in ("ready", "failed"):
            # Новое соединение шины может повторно использовать unique names.
            self._clear_item_pids()
            self._setup_pending = False
            self._subscriptions_ready = event == "ready" and monotonic() < self._setup_deadline
            if not self._subscriptions_ready:
                self._host_registered = False
                self._item_registered = False
                self._xembed_allowed = False
                self._plasma_alive = False
                self._plasma_known = False
                for service in tuple(self._requests):
                    self._cancel_request(service)
                self._invalidate_registration()
                return
            self._is_kde = bool(payload)
            self._try_register()
        elif not self._subscriptions_ready:
            return
        elif event == "reply":
            serial, reply = payload
            for service, (pending, callback) in tuple(self._requests.items()):
                if serial == pending:
                    del self._requests[service]
                    callback(reply)
                    break
        elif event == "host":
            self._host_changed()
        elif event in ("item_registered", "item_unregistered"):
            self._items_changed(payload, removed=event == "item_unregistered")
        elif event == "properties":
            self._properties_changed(*payload)
        elif event == "owner":
            self._service_owner_changed(*payload)

    def _query_plasma(self) -> None:
        if self._is_kde and not self._plasma_known and monotonic() >= self._plasma_retry_at:
            self._plasma_retry_at = monotonic() + 1.0
            message = QDBusMessage.createMethodCall(
                "org.freedesktop.DBus",
                "/org/freedesktop/DBus",
                "org.freedesktop.DBus",
                "GetNameOwner",
            )
            message.setArguments([_PLASMA_SERVICE])
            self._request(_PLASMA_SERVICE, message, self._plasma_reply)

    def _request(
        self,
        service: str,
        message: QDBusMessage,
        callback: Callable[[_BusReply | None], None],
    ) -> None:
        if not self._subscriptions_ready or service in self._requests:
            return
        self._serial += 1
        # Номер не сбрасывается в stop(): ответ старого владельца или цикла
        # не совпадёт с запросом нового цикла, даже если он ещё в очереди.
        self._requests[service] = (self._serial, callback)
        _send_bus_command(self._bus_generation, "request", (self._serial, message))

    def _cancel_request(self, service: str) -> None:
        self._requests.pop(service, None)

    def _query_host(self) -> None:
        message = QDBusMessage.createMethodCall(
            _WATCHER_SERVICE, _WATCHER_PATH, _PROPERTIES_INTERFACE, "Get"
        )
        message.setArguments([_WATCHER_SERVICE, _HOST_PROPERTY])
        self._request(_WATCHER_SERVICE, message, self._host_reply)

    def _host_reply(self, reply: _BusReply | None) -> None:
        if self._retry_expired:
            return
        if self._deadline is not None and monotonic() >= self._deadline:
            self._registration_expired()
            return
        args = reply[1] if reply is not None and reply[0] == QDBusMessage.ReplyMessage else []
        value = args[0] if len(args) == 1 else None
        self._host_registered = value is True
        self._xembed_allowed = not self._is_kde and (
            value is False
            or bool(
                reply is not None
                and reply[0] == QDBusMessage.ErrorMessage
                and reply[1]
                in (
                    ["org.freedesktop.DBus.Error.ServiceUnknown"],
                    ["org.freedesktop.DBus.Error.NameHasNoOwner"],
                )
            )
        )
        if not self._host_registered:
            self._item_registered = False
            self._cancel_request(_ITEMS_PROPERTY)
        if self._xembed_allowed:
            self._refresh_xembed_owner()
        if self._registered and not self._registration_ready():
            self._invalidate_registration()
        if self._shown_unconfirmed and self._path == "XEmbed" and not self._panel_ready():
            self._end_retry()
        elif not self._registered:
            self._begin_retry()
            self._show_available_path()
        if self._host_registered:
            self._query_items()

    @staticmethod
    def _item_service(item: Any) -> str | None:
        if not isinstance(item, str):
            return None
        service, separator, path = item.partition("/")
        return service if separator and path == "StatusNotifierItem" else None

    @staticmethod
    def _ascii_decimal(value: str) -> bool:
        return value.isascii() and value.isdecimal()

    def _unique_item_service(self, item: Any) -> str | None:
        service = self._item_service(item)
        if service is not None and service.startswith(":1."):
            if self._ascii_decimal(service[3:]):
                return service
        return None

    def _is_our_item(self, item: Any) -> bool:
        # Только локальный кэш: чтение registered никогда не вызывает D-Bus.
        service = self._item_service(item)
        if service is None:
            return False
        if service.startswith(self._item_prefix):
            return self._ascii_decimal(service[len(self._item_prefix) :])
        return self._item_pids.get(service, False)

    def _clear_item_pids(self) -> None:
        for key in tuple(self._requests):
            if key.startswith("pid:"):
                self._cancel_request(key)
        self._item_pids.clear()
        self._pending_item_signals.clear()

    def _query_item_pid(self, service: str) -> None:
        if service in self._item_pids:
            return
        message = QDBusMessage.createMethodCall(
            _DBUS_SERVICE, _DBUS_PATH, _DBUS_SERVICE, "GetConnectionUnixProcessID"
        )
        message.setArguments([service])
        self._request("pid:" + service, message, partial(self._item_pid_reply, service))

    def _item_pid_reply(self, service: str, reply: _BusReply | None) -> None:
        args = reply[1] if reply is not None and reply[0] == QDBusMessage.ReplyMessage else []
        self._item_pids[service] = (
            len(args) == 1 and type(args[0]) is int and args[0] == os.getpid()
        )
        signal = self._pending_item_signals.pop(service, None)
        if signal is not None and self._item_pids[service]:
            self._items_changed(signal[0], removed=signal[1])
        else:
            self._update_item_registration()

    def _query_items(self) -> None:
        if not self._host_registered or self._retry_expired:
            return
        message = QDBusMessage.createMethodCall(
            _WATCHER_SERVICE, _WATCHER_PATH, _PROPERTIES_INTERFACE, "Get"
        )
        message.setArguments([_WATCHER_SERVICE, _ITEMS_PROPERTY])
        self._request(_ITEMS_PROPERTY, message, self._items_reply)

    def _items_reply(self, reply: _BusReply | None) -> None:
        if self._retry_expired or not self._host_registered:
            return
        args = reply[1] if reply is not None and reply[0] == QDBusMessage.ReplyMessage else []
        items = args[0] if len(args) == 1 else None
        self._item_names = (
            [item for item in items if isinstance(item, str)] if isinstance(items, list) else []
        )
        for item in self._item_names:
            service = self._unique_item_service(item)
            if service is not None:
                self._query_item_pid(service)
        self._update_item_registration()

    def _update_item_registration(self) -> None:
        if self._retry_expired or not self._host_registered:
            return
        if self._deadline is not None and monotonic() >= self._deadline:
            self._registration_expired()
            return
        self._item_registered = self._tray.isVisible() and any(
            self._is_our_item(item) for item in self._item_names
        )
        if self._registered and not self._item_registered:
            self._invalidate_registration(preserve_shown=True)
        if not self._registered:
            self._begin_retry()
            if self._tray.isVisible():
                self._show_available_path()

    def _items_changed(self, item: str | None = None, *, removed: bool = False) -> None:
        our_item = self._is_our_item(item)
        service = self._unique_item_service(item)
        if service is not None and service not in self._item_pids and isinstance(item, str):
            self._pending_item_signals[service] = (item, removed)
            self._query_item_pid(service)
            # Ответ может быть синхронным в тестовом транспорте.
            our_item = self._is_our_item(item)
        # Чужой сигнал не открывает новый цикл после окончательного таймаута.
        if self._retry_expired and not our_item:
            return
        self._cancel_request(_ITEMS_PROPERTY)
        self._item_names = []
        if item is None or (removed and our_item):
            # PropertiesChanged может касаться чужого элемента: сохраняем сам
            # значок Qt до свежего списка, но не считаем доказанный SNI видимым.
            self._item_registered = False
            self._shown_unconfirmed = not self._sni_backend_proven and (
                self._shown_unconfirmed or (self._registered and bool(self._tray.isVisible()))
            )
            self._registered = False
        if not self._registered:
            if our_item:
                self._retry_after_change()
            elif not self._shown_unconfirmed:
                self._begin_retry()
        self._query_items()

    def _plasma_reply(self, reply: _BusReply | None) -> None:
        args = reply[1] if reply is not None and reply[0] == QDBusMessage.ReplyMessage else []
        self._plasma_known = len(args) == 1 and isinstance(args[0], str) and bool(args[0])
        self._set_plasma_alive(self._plasma_known)

    def _set_plasma_alive(self, alive: bool) -> None:
        self._plasma_alive = alive
        if not alive:
            self._invalidate_registration()
        elif not self._registered:
            self._begin_retry()
            self._try_register()

    @pyqtSlot()
    def _host_changed(self) -> None:
        if not self._running:
            return
        self._clear_item_pids()
        self._cancel_request(_WATCHER_SERVICE)
        self._cancel_request(_ITEMS_PROPERTY)
        self._item_registered = False
        self._host_registered = False
        self._xembed_allowed = False
        self._retry_after_change()
        self._invalidate_registration(preserve_shown=self._shown_unconfirmed)
        self._begin_retry()
        # Сигнал — только повод обновить кэш; разрешение show() даёт ответ Get.
        self._query_host()

    @pyqtSlot(str, "QVariantMap", "QStringList")
    def _properties_changed(
        self, interface: str, changed: dict[str, Any], invalidated: list[str]
    ) -> None:
        if interface == _WATCHER_SERVICE and (
            _HOST_PROPERTY in changed or _HOST_PROPERTY in invalidated
        ):
            self._host_changed()
        elif interface == _WATCHER_SERVICE and (
            _ITEMS_PROPERTY in changed or _ITEMS_PROPERTY in invalidated
        ):
            self._items_changed()

    def _begin_retry(self) -> None:
        if self._deadline is not None or self._retry_expired:
            return
        self._deadline = monotonic() + 30.0
        self._deadline_timer.start(30_000)
        self._retry_timer.start()

    def _retry_after_change(self) -> None:
        # Явное изменение панели разрешает новый цикл после истечения старого;
        # обычные ответы и тики текущий дедлайн никогда не продлевают.
        if self._deadline is not None and monotonic() >= self._deadline:
            self._end_retry()
        self._retry_expired = False
        self._begin_retry()

    def _end_retry(self) -> None:
        self._deadline = None
        self._retry_timer.stop()
        self._deadline_timer.stop()

    def _try_register(self) -> None:
        if not self._running or self._deadline is None:
            return
        if monotonic() >= self._deadline:
            self._registration_expired()
            return
        if self._subscriptions_ready:
            self._query_plasma()
        if self._deadline is None:
            return
        if not self._subscriptions_ready:
            if self._setup_pending and monotonic() > self._setup_deadline + _SETUP_STALE_S:
                self._setup_pending = False
            self._setup_bus()
            return
        elif not self._host_registered:
            self._query_host()
            return
        self._show_available_path()
        self._query_items()

    def _retry_tick(self) -> None:
        if not self._running or self._deadline is None:
            return
        if monotonic() >= self._deadline:
            self._registration_expired()
            return
        self._try_register()

    def _show_available_path(self) -> None:
        # Ответ может прийти уже после deadline либо синхронно внутри запроса.
        if not self._running or self._registered or self._deadline is None:
            return
        if monotonic() >= self._deadline:
            self._registration_expired()
            return
        path = (
            "SNI"
            if self._panel_ready()
            else "XEmbed"
            if self._xembed_allowed and self._xembed_owner_id
            else "нет"
        )
        was_shown = self._shown_unconfirmed
        self._set_path(path)
        if not self._has_icon():
            self._invalidate_registration()
        elif path == "нет":
            self._invalidate_registration(preserve_shown=True)
        else:
            # После refresh() провайдера прежняя иконка могла остаться пустой.
            self._tray.setIcon(self._display_icon())
            if not self._tray.isVisible():
                # hide() удаляет SNI-сервис Qt; новый show требует нового Get.
                self._item_registered = False
                self._cancel_request(_ITEMS_PROPERTY)
                self._tray.show()
            self._registered = bool(self._tray.isVisible() and self._registration_ready())
            if self._registered:
                self._sni_backend_proven = True
            self._shown_unconfirmed = bool(self._tray.isVisible()) and not self._sni_backend_proven
            if (self._shown_unconfirmed or self._registered) and not self._xembed_polling:
                self._xembed_polling = True
                self._xembed_timer.start()
            if self._shown_unconfirmed and path == "XEmbed":
                self._end_retry()
                if not was_shown:
                    notify.drop_pending(_UNAVAILABLE_SUMMARY)
            if self._registered:
                self._end_retry()
                notify.drop_pending(_UNAVAILABLE_SUMMARY)
                delivered = notify.flush_pending()
                _logger.info(
                    "Отправлено отложенных уведомлений после регистрации значка: %d", delivered
                )
                self._warn_panel_dependency()

    def _registration_expired(self) -> None:
        if not self._running or self._deadline is None:
            return
        if self.registered:
            self._end_retry()
            return
        self._retry_expired = True
        self._end_retry()
        self._registered = False
        self._item_registered = False
        for service in tuple(self._requests):
            self._cancel_request(service)
        self._item_names = []
        self._pending_item_signals.clear()
        self._xembed_polling = False
        self._xembed_timer.stop()
        if self.visible_for_close:
            return
        self._shown_unconfirmed = False
        self._tray.hide()
        self._set_path("нет")
        if not self._banner_shown:
            self._banner_shown = True
            notify.notify_tray_unavailable()

    def _service_registered(self, service: str) -> None:
        if not self._running:
            return
        if service == _WATCHER_SERVICE:
            self._host_changed()
        elif self._is_kde and service == _PLASMA_SERVICE:
            self._cancel_request(service)
            self._plasma_known = True
            if not self._registered:
                self._retry_after_change()
            self._set_plasma_alive(True)

    def _service_unregistered(self, service: str) -> None:
        if not self._running:
            return
        if service == _WATCHER_SERVICE:
            self._clear_item_pids()
            self._cancel_request(service)
            self._cancel_request(_ITEMS_PROPERTY)
            self._item_registered = False
            self._host_registered = False
            self._xembed_allowed = not self._is_kde
            self._retry_expired = False
            if self._xembed_allowed:
                self._refresh_xembed_owner()
            self._invalidate_registration()
            if self._xembed_allowed and self._xembed_owner_id:
                self._show_available_path()
            else:
                self._set_path("нет")
        elif self._is_kde and service == _PLASMA_SERVICE:
            self._cancel_request(service)
            self._plasma_known = True
            self._set_plasma_alive(False)

    def _service_owner_changed(self, service: str, old_owner: str, new_owner: str) -> None:
        if old_owner == new_owner:
            return
        if service.startswith(":1."):
            affected = any(self._item_service(item) == service for item in self._item_names)
            if not (
                affected
                or service in self._item_pids
                or service in self._pending_item_signals
                or "pid:" + service in self._requests
            ):
                return
            was_ours = self._item_pids.pop(service, False)
            self._cancel_request("pid:" + service)
            self._pending_item_signals.pop(service, None)
            if not new_owner:
                # Имя уже исчезло: старый snapshot не должен запросить PID снова.
                self._item_pids[service] = False
            if affected:
                self._item_names = [
                    item for item in self._item_names if self._item_service(item) != service
                ]
                self._cancel_request(_ITEMS_PROPERTY)
            if was_ours and old_owner:
                self._invalidate_registration(preserve_shown=True)
                self._retry_after_change()
                self._query_items()
            elif affected:
                self._update_item_registration()
            return
        if new_owner:
            if (
                old_owner
                and self._sni_backend_proven
                and self._is_kde
                and service == _PLASMA_SERVICE
            ):
                # Новый shell ещё не подтверждает прежний SNI-элемент.
                self._invalidate_registration()
            self._service_registered(service)
        else:
            self._service_unregistered(service)

    def stop(self) -> None:
        self._running = False
        self._registered = False
        self._host_registered = False
        self._plasma_alive = False
        self._plasma_known = False
        self._plasma_retry_at = 0.0
        self._subscriptions_ready = False
        self._setup_pending = False
        self._xembed_allowed = False
        self._xembed_owner_id = None
        self._shown_unconfirmed = False
        self._xembed_polling = False
        self._xembed_poll_at = 0.0
        self._item_registered = False
        self._item_names = []
        self._item_pids.clear()
        self._pending_item_signals.clear()
        self._xembed_timer.stop()
        self._xembed_probe.close()
        self._end_retry()
        self._done_timer.stop()
        for service in tuple(self._requests):
            self._cancel_request(service)
        if _bus_receiver is not None and _bus_receiver.tray is self:
            _bus_receiver.tray = None
        self._tray.hide()
