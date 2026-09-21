"""Обязательный статичный индикатор записи и меню системного лотка."""

from __future__ import annotations

import logging
from collections.abc import Callable
from functools import partial
from time import monotonic
from typing import Any

from PyQt5.QtCore import QObject, Qt, QThread, QTimer, pyqtSignal, pyqtSlot
from PyQt5.QtDBus import (
    QDBusConnection,
    QDBusError,
    QDBusMessage,
    QDBusVariant,
)
from PyQt5.QtWidgets import QAction, QActionGroup, QMenu, QSystemTrayIcon

from astra_voice.platform.session import SessionKind, detect
from astra_voice.ui import notify
from astra_voice.ui.formatting import clean_display_name
from astra_voice.ui.tray_icons import TrayIconProvider
from astra_voice.ui.tray_icons import TrayState as TrayState

_WATCHER_SERVICE = "org.kde.StatusNotifierWatcher"
_WATCHER_PATH = "/StatusNotifierWatcher"
_PLASMA_SERVICE = "org.kde.plasmashell"
_PROPERTIES_INTERFACE = "org.freedesktop.DBus.Properties"
_HOST_PROPERTY = "IsStatusNotifierHostRegistered"
_DBUS_SERVICE = "org.freedesktop.DBus"
_DBUS_PATH = "/org/freedesktop/DBus"
_UNAVAILABLE_SUMMARY = "Значок не появился на панели"
_logger = logging.getLogger(__name__)


class _BusRequest(QObject):
    """Типизированные Qt-слоты для одного запроса без Introspect и ожидания."""

    def __init__(self, callback: Callable[[QDBusMessage | None], None], parent: QObject) -> None:
        super().__init__(parent)
        self._callback = callback

    @pyqtSlot(QDBusMessage)
    def finished(self, message: QDBusMessage) -> None:
        self._callback(message)

    @pyqtSlot(QDBusError, QDBusMessage)
    def failed(self, error: QDBusError, message: QDBusMessage) -> None:
        self._callback(None)


class _BusWorker(QObject):
    """Все QtDBus-объекты живут и удаляются вне GUI, включая получателей сигналов."""

    command = pyqtSignal(str, object)
    result = pyqtSignal(str, object)
    finished = pyqtSignal()

    def __init__(self) -> None:
        super().__init__()
        self.command.connect(self._command)
        self._bus: QDBusConnection | None = None
        self._name = f"astra-voice-tray-{id(self):x}"
        self._requests: dict[int, _BusRequest] = {}
        self._connections: set[str] = set()
        self._matches: set[str] = set()
        self._setup_id = 0
        self._setup_timer: QTimer | None = None
        self._is_kde = False

    @pyqtSlot(str, object)
    def _command(self, operation: str, payload: Any) -> None:
        try:
            if operation == "setup":
                self._setup()
            elif operation == "request":
                serial, message = payload
                self._request(serial, message)
            elif operation == "cancel":
                self._cancel(payload)
            elif operation == "stop":
                self._close()
        except Exception:
            _logger.exception("Ошибка наблюдения за панелью через D-Bus")
            if operation == "request":
                serial, _ = payload
                self._cancel(serial)
                self.result.emit("reply", (serial, None))
            else:
                self._setup_expired()

    def _setup(self) -> None:
        self._setup_id += 1
        setup_id = self._setup_id
        if self._setup_timer is None:
            self._setup_timer = QTimer(self)
            self._setup_timer.setSingleShot(True)
            self._setup_timer.timeout.connect(self._setup_expired)
        self._setup_timer.start(500)
        self._is_kde = detect() == SessionKind.KDE
        if self._bus is not None and not self._bus.isConnected():
            QDBusConnection.disconnectFromBus(self._name)
            self._bus = None
            self._connections.clear()
            self._matches.clear()
        if self._bus is None:
            self._bus = QDBusConnection.connectToBus(QDBusConnection.SessionBus, self._name)
        # singleShot(0) в GUI лишь переносит зависание на следующий оборот.
        # Поэтому даже connectToBus/connect и уничтожение получателей вынесены
        # в рабочий Qt-поток. connect() не доказывает принятие AddMatch демоном:
        # каждое правило дополнительно подтверждаем асинхронно за <= 500 мс.
        subscriptions: list[tuple[str, str, str, str, Callable[..., None]]] = [
            (_DBUS_SERVICE, _DBUS_PATH, _DBUS_SERVICE, "NameOwnerChanged", self._owner_changed),
            *[
                (_WATCHER_SERVICE, _WATCHER_PATH, _WATCHER_SERVICE, member, self._host_changed)
                for member in ("StatusNotifierHostRegistered", "StatusNotifierHostUnregistered")
            ],
            (
                _WATCHER_SERVICE,
                _WATCHER_PATH,
                _PROPERTIES_INTERFACE,
                "PropertiesChanged",
                self._properties_changed,
            ),
        ]
        rules = set()
        for service, path, interface, member, slot in subscriptions:
            rule = (
                f"type='signal',sender='{service}',path='{path}',"
                f"interface='{interface}',member='{member}'"
            )
            rules.add(rule)
            if rule not in self._connections:
                if not self._bus.connect(service, path, interface, member, slot):
                    self._setup_expired()
                    return
                self._connections.add(rule)
        for index, rule in enumerate(sorted(rules - self._matches), 1):
            message = QDBusMessage.createMethodCall(
                _DBUS_SERVICE, _DBUS_PATH, _DBUS_SERVICE, "AddMatch"
            )
            message.setArguments([rule])
            self._request(-index, message, partial(self._match_reply, setup_id, rule, rules))
            if setup_id != self._setup_id:
                return
        if rules <= self._matches:
            self._setup_ready()

    def _match_reply(
        self, setup_id: int, rule: str, rules: set[str], message: QDBusMessage | None
    ) -> None:
        if setup_id != self._setup_id:
            return
        if message is None or message.type() != QDBusMessage.ReplyMessage:
            self._setup_expired()
            return
        self._matches.add(rule)
        if rules <= self._matches:
            self._setup_ready()

    def _setup_ready(self) -> None:
        self._setup_id += 1
        if self._setup_timer is not None:
            self._setup_timer.stop()
        self.result.emit("ready", self._is_kde)

    def _setup_expired(self) -> None:
        self._setup_id += 1
        if self._setup_timer is not None:
            self._setup_timer.stop()
        for serial in tuple(self._requests):
            if serial < 0:
                self._cancel(serial)
        self.result.emit("failed", None)

    def _request(
        self,
        serial: int,
        message: QDBusMessage,
        callback: Callable[[QDBusMessage | None], None] | None = None,
    ) -> None:
        def finished(reply: QDBusMessage | None) -> None:
            if self._requests.get(serial) is not request:
                return
            self._cancel(serial)
            if callback is None:
                self.result.emit("reply", (serial, reply))
            else:
                callback(reply)

        self._cancel(serial)
        request = _BusRequest(finished, self)
        self._requests[serial] = request
        if self._bus is None or not self._bus.callWithCallback(
            message, request.finished, request.failed, 500
        ):
            finished(None)

    def _cancel(self, serial: int) -> None:
        request = self._requests.pop(serial, None)
        if request is not None:
            request.deleteLater()

    @pyqtSlot(str, str, str)
    def _owner_changed(self, service: str, old_owner: str, new_owner: str) -> None:
        self.result.emit("owner", (service, old_owner, new_owner))

    @pyqtSlot()
    def _host_changed(self) -> None:
        self.result.emit("host", None)

    @pyqtSlot(str, "QVariantMap", "QStringList")
    def _properties_changed(
        self, interface: str, changed: dict[str, Any], invalidated: list[str]
    ) -> None:
        self.result.emit("properties", (interface, changed, invalidated))

    def _close(self) -> None:
        if self._setup_timer is not None:
            self._setup_timer.stop()
        self._setup_id += 1
        for serial in tuple(self._requests):
            self._cancel(serial)
        # Отдельное соединение снимает также наши подтверждающие AddMatch.
        try:
            QDBusConnection.disconnectFromBus(self._name)
        finally:
            self._bus = None
            self.result.emit("closed", None)
            self.finished.emit()


# stop() не ждёт поток и не уничтожает QThread, зависший внутри QtDBus.
# Сильные ссылки живут до finished; один Tray не плодит потоки при ретраях.
_bus_threads: dict[_BusWorker, QThread] = {}


def _start_bus_worker(worker: _BusWorker) -> None:
    thread = QThread()
    _bus_threads[worker] = thread
    worker.moveToThread(thread)
    worker.finished.connect(thread.quit, Qt.DirectConnection)
    thread.finished.connect(worker.deleteLater)
    thread.finished.connect(thread.deleteLater)
    thread.finished.connect(lambda: _bus_threads.pop(worker, None))
    thread.start()


class Tray(QObject):
    """Владеет значком; stop() предназначен для завершения работы приложения.

    Фабрика создаёт единственный значок. Готовность панели проверяется через D-Bus:
    ранний isSystemTrayAvailable() отравляет кэш generic/Fly-темы Qt.
    Тесты также подменяют QDBusConnection до start().
    """

    def __init__(
        self,
        provider: TrayIconProvider,
        *,
        hotkey: str = "Ctrl+Space",
        tray_factory: Callable[[], Any] | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.on_cancel: Callable[[], None] | None = None
        self.on_settings: Callable[[], None] | None = None
        self.on_copy_last: Callable[[], None] | None = None
        self.on_check_updates: Callable[[], None] | None = None
        self.on_model_recheck: Callable[[], None] | None = None
        self.on_about: Callable[[], None] | None = None
        self.on_quit: Callable[[], None] | None = None
        self.on_model_selected: Callable[[str], None] | None = None
        self._provider = provider
        self._hotkey = hotkey
        factory = tray_factory if tray_factory is not None else lambda: QSystemTrayIcon()
        self._tray = factory()
        self._tray.setParent(self)
        self._registered = False
        self._running = False
        self._banner_shown = False
        self._deadline: float | None = None
        self._worker: _BusWorker | None = None
        self._worker_closing = False
        self._subscriptions_ready = False
        self._setup_pending = False
        self._setup_deadline = 0.0
        self._serial = 0
        self._requests: dict[str, tuple[int, Callable[[QDBusMessage | None], None]]] = {}
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
        self._retry_timer.timeout.connect(self._try_register)
        self._deadline_timer = QTimer(self)
        self._deadline_timer.setSingleShot(True)
        self._deadline_timer.setTimerType(Qt.PreciseTimer)
        self._deadline_timer.timeout.connect(self._registration_expired)

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

    def _add_action(self, label: str, callback: Callable[[], None]) -> QAction:
        action = QAction(label, self._menu)
        action.triggered.connect(callback)
        self._menu.addAction(action)
        return action

    def set_state(self, state: TrayState, tooltip: str | None = None) -> None:
        self._done_timer.stop()
        self._state = state
        has_icon = self._has_icon()
        if has_icon:
            self._tray.setIcon(self._provider.icon(state))
        else:
            self._invalidate_registration()
        self._update_tooltip(tooltip)
        self._update_menu()
        if state == TrayState.DONE:
            self._done_timer.start(800)
        if self._running and not self.registered:
            if self._deadline is None:
                self._begin_retry()
            if has_icon:
                self._try_register()

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
            {
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

    def set_models(self, models: list[str], active: str | None) -> None:
        self._model_menu.clear()
        self._model_action.setMenu(None)
        self._model_action.setEnabled(len(models) > 1)
        if not models:
            self._model_action.setText("Модель не установлена")
        elif len(models) == 1:
            self._model_action.setText(f"Модель: {models[0]}")
        else:
            self._model_action.setText("Модель")
            self._model_action.setMenu(self._model_menu)
            for name in models:
                action = QAction(name, self._model_menu)
                action.setCheckable(True)
                action.setChecked(name == active)
                self._model_group.addAction(action)
                action.triggered.connect(partial(self._select_model, name))
                self._model_menu.addAction(action)
        self._update_menu()

    def _select_model(self, name: str, checked: bool = False) -> None:
        if self.on_model_selected is not None:
            self.on_model_selected(name)

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
            not self._panel_ready() or not self._tray.isVisible() or not self._has_icon()
        ):
            self._invalidate_registration()
        return self._registered

    def _panel_ready(self) -> bool:
        # KDED переживает plasmashell и продолжает сообщать host=True.
        return (
            self._subscriptions_ready
            and self._host_registered
            and (not self._is_kde or self._plasma_alive)
        )

    def _has_icon(self) -> bool:
        if self._provider.has_icon(self._state):
            return True
        _logger.warning(
            "Пустой значок трея для состояния %s; регистрация отложена", self._state.value
        )
        return False

    def _invalidate_registration(self) -> None:
        self._registered = False
        self._tray.hide()
        if self._running and self._deadline is None:
            self._begin_retry()

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._begin_retry()
        self._try_register()

    def _setup_bus(self) -> None:
        if self._setup_pending or self._worker_closing:
            return
        if self._worker is None:
            self._worker = _BusWorker()
            self._worker.result.connect(self._bus_event)
            _start_bus_worker(self._worker)
        self._setup_pending = True
        self._setup_deadline = monotonic() + 0.5
        self._worker.command.emit("setup", None)

    @pyqtSlot(str, object)
    def _bus_event(self, event: str, payload: Any) -> None:
        if self.sender() is not self._worker:
            return
        if event == "closed":
            self._worker = None
            self._worker_closing = False
            if self._running:
                self._try_register()
            return
        if not self._running or self._worker_closing:
            return
        if event in ("ready", "failed"):
            self._setup_pending = False
            self._subscriptions_ready = event == "ready" and monotonic() < self._setup_deadline
            if not self._subscriptions_ready:
                self._host_registered = False
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
        callback: Callable[[QDBusMessage | None], None],
    ) -> None:
        if self._worker is None or not self._subscriptions_ready or service in self._requests:
            return
        self._serial += 1
        # Номер не сбрасывается в stop(): ответ старого владельца или worker
        # не совпадёт с запросом нового цикла, даже если отмена ещё в очереди.
        self._requests[service] = (self._serial, callback)
        self._worker.command.emit("request", (self._serial, message))

    def _cancel_request(self, service: str) -> None:
        request = self._requests.pop(service, None)
        if request is not None and self._worker is not None:
            self._worker.command.emit("cancel", request[0])

    def _query_host(self) -> None:
        message = QDBusMessage.createMethodCall(
            _WATCHER_SERVICE, _WATCHER_PATH, _PROPERTIES_INTERFACE, "Get"
        )
        message.setArguments([_WATCHER_SERVICE, _HOST_PROPERTY])
        self._request(_WATCHER_SERVICE, message, self._host_reply)

    def _host_reply(self, message: QDBusMessage | None) -> None:
        args = message.arguments() if message is not None else []
        value = args[0] if len(args) == 1 else None
        if isinstance(value, QDBusVariant):
            value = value.variant()
        self._host_registered = value is True
        if self._host_registered:
            if self._deadline is None:
                self._begin_retry()
            self._try_register()
        else:
            self._invalidate_registration()

    def _plasma_reply(self, message: QDBusMessage | None) -> None:
        args = message.arguments() if message is not None else []
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
        self._cancel_request(_WATCHER_SERVICE)
        self._host_registered = False
        self._invalidate_registration()
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

    def _begin_retry(self) -> None:
        self._deadline = monotonic() + 30.0
        self._deadline_timer.start(30_000)
        self._retry_timer.start()

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
            self._setup_bus()
        elif not self._host_registered:
            self._query_host()
        elif not self._panel_ready() or not self._has_icon():
            self._invalidate_registration()
        else:
            # После refresh() провайдера прежняя иконка могла остаться пустой.
            self._tray.setIcon(self._provider.icon(self._state))
            self._tray.show()
            self._registered = bool(self._tray.isVisible())
            if self._registered:
                self._end_retry()
                notify.drop_pending(_UNAVAILABLE_SUMMARY)
                delivered = notify.flush_pending()
                _logger.info(
                    "Доставлено отложенных уведомлений после регистрации значка: %d", delivered
                )
                self._warn_panel_dependency()

    def _registration_expired(self) -> None:
        if not self._running or self._deadline is None:
            return
        self._end_retry()
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
            self._set_plasma_alive(True)

    def _service_unregistered(self, service: str) -> None:
        if not self._running:
            return
        if service == _WATCHER_SERVICE:
            self._cancel_request(service)
            self._host_registered = False
            self._invalidate_registration()
        elif self._is_kde and service == _PLASMA_SERVICE:
            self._cancel_request(service)
            self._plasma_known = True
            self._set_plasma_alive(False)

    def _service_owner_changed(self, service: str, old_owner: str, new_owner: str) -> None:
        if old_owner == new_owner:
            return
        if new_owner:
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
        self._end_retry()
        self._done_timer.stop()
        for service in tuple(self._requests):
            self._cancel_request(service)
        if self._worker is not None and not self._worker_closing:
            self._worker_closing = True
            self._worker.command.emit("stop", None)
        self._tray.hide()
