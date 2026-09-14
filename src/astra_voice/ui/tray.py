"""Обязательный статичный индикатор записи и меню системного лотка."""

from __future__ import annotations

import logging
from collections.abc import Callable
from functools import partial
from time import monotonic
from typing import Any

from PyQt5.QtCore import QObject, Qt, QTimer
from PyQt5.QtDBus import QDBusConnection, QDBusServiceWatcher
from PyQt5.QtWidgets import QAction, QActionGroup, QMenu, QSystemTrayIcon

from astra_voice.ui import notify
from astra_voice.ui.tray_icons import TrayIconProvider
from astra_voice.ui.tray_icons import TrayState as TrayState

_WATCHER_SERVICE = "org.kde.StatusNotifierWatcher"
_logger = logging.getLogger(__name__)


class Tray(QObject):
    """Владеет значком; stop() предназначен для завершения работы приложения.

    Фабрика создаёт единственный значок и предоставляет isSystemTrayAvailable().
    Тесты также подменяют QDBusConnection/QDBusServiceWatcher до start().
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
        self.on_about: Callable[[], None] | None = None
        self.on_quit: Callable[[], None] | None = None
        self.on_model_selected: Callable[[str], None] | None = None
        self._provider = provider
        self._hotkey = hotkey
        factory = tray_factory if tray_factory is not None else lambda: QSystemTrayIcon()
        self._tray = factory()
        self._tray.setParent(self)
        self._is_available: Callable[[], bool] = self._tray.isSystemTrayAvailable
        self._registered = False
        self._running = False
        self._banner_shown = False
        self._deadline: float | None = None
        self._watcher: QDBusServiceWatcher | None = None
        self._has_last_text = False
        self._updates_enabled = False

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
        self._menu.addSeparator()
        self._cancel_action = self._add_action("Отмена", lambda: self._invoke(self.on_cancel))
        self._model_action = self._menu.addAction("Модель не установлена")
        self._model_menu = QMenu("Модель", self._menu)
        self._model_group = QActionGroup(self._model_menu)
        self._model_group.setExclusive(True)
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
        self._tray.setToolTip(
            self._provider.tooltip(state, hotkey=self._hotkey) if tooltip is None else tooltip
        )
        self._update_menu()
        if state == TrayState.DONE:
            self._done_timer.start(800)
        if self._running and not self.registered:
            if self._deadline is None:
                self._begin_retry()
            if has_icon:
                self._try_register()

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

    @property
    def registered(self) -> bool:
        """Доступен ли значок для инварианта видимой индикации О4."""
        if self._registered and (
            not self._is_available() or not self._tray.isVisible() or not self._has_icon()
        ):
            self._invalidate_registration()
        return self._registered

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
        self._watcher = QDBusServiceWatcher(
            _WATCHER_SERVICE,
            QDBusConnection.sessionBus(),
            QDBusServiceWatcher.WatchForRegistration | QDBusServiceWatcher.WatchForUnregistration,
            self,
        )
        self._watcher.serviceRegistered.connect(self._service_registered)
        self._watcher.serviceUnregistered.connect(self._service_unregistered)
        self._begin_retry()
        self._try_register()

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
        elif not self._has_icon() or not self._is_available():
            self._invalidate_registration()
        else:
            # После refresh() провайдера прежняя иконка могла остаться пустой.
            self._tray.setIcon(self._provider.icon(self._state))
            self._tray.show()
            self._registered = bool(self._tray.isVisible())
            if self._registered:
                self._end_retry()
                delivered = notify.flush_pending()
                _logger.info(
                    "Доставлено отложенных уведомлений после регистрации значка: %d", delivered
                )

    def _registration_expired(self) -> None:
        if not self._running or self._deadline is None:
            return
        self._end_retry()
        if not self._banner_shown:
            self._banner_shown = True
            notify.notify_tray_unavailable()

    def _service_registered(self, service: str) -> None:
        if self._running and service == _WATCHER_SERVICE:
            self._begin_retry()
            self._try_register()

    def _service_unregistered(self, service: str) -> None:
        if self._running and service == _WATCHER_SERVICE:
            # Не проверяем синхронно: Qt ещё может отдавать прежнюю доступность.
            self._invalidate_registration()

    def stop(self) -> None:
        self._running = False
        self._registered = False
        self._end_retry()
        self._done_timer.stop()
        if self._watcher is not None:
            self._watcher.setWatchedServices([])
            self._watcher.deleteLater()
            self._watcher = None
        self._tray.hide()
