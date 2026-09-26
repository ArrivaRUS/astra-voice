"""Общее состояние назначения горячей клавиши для мастера и настроек."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Protocol, cast

from PyQt5.QtCore import QEvent, QObject, Qt, pyqtSignal, pyqtSlot
from PyQt5.QtGui import QGuiApplication

from astra_voice.core.settings import is_valid_combo
from astra_voice.platform.hotkey import DEFAULT_CANDIDATES

log = logging.getLogger(__name__)


class CaptureHost(Protocol):
    """Только действия системы, необходимые полю захвата."""

    def begin_capture(self) -> bool: ...

    def set_capture_callback(self, callback: Callable[[str, str], None]) -> None: ...

    def end_capture(self) -> None: ...

    def probe(self, combo: str) -> str: ...

    def free_candidates(self, prefer: list[str]) -> list[str]: ...


class HotkeyCapture(QObject):
    """Одна машина состояний; X11-события принимает только через очередь Qt."""

    captureStateChanged = pyqtSignal()
    captureMessageChanged = pyqtSignal()
    pendingComboChanged = pyqtSignal()
    freeCandidatesChanged = pyqtSignal()
    keyEvent = pyqtSignal(str, str)
    # Подключать только через Qt.ConnectionType.DirectConnection: несёт сырой QEvent.
    windowEvent = pyqtSignal(QObject, QEvent)

    _MESSAGES = {
        "conflict": "Эта комбинация занята другой программой. Можно оставить её или выбрать другую",
        "duplicate": "Эта комбинация уже назначена",
        "not-grabbed": "Не удалось назначить комбинацию. Выберите другую",
    }

    def __init__(self, host: CaptureHost | None = None, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.host = host
        self.state = "idle"
        self.hint = ""
        self.pending_combo = ""
        self.free_candidates: list[str] = []
        self._save: Callable[[str, bool], str] | None = None
        self.keep_busy = False
        self._window: QObject | None = None
        self._application: QGuiApplication | None = None
        self.keyEvent.connect(self._handle_key_event, Qt.QueuedConnection)

    def attach_window(self, window: QObject) -> None:
        if self._window is window:
            return
        if self._window is not None:
            self._window.removeEventFilter(self)
        self._window = window
        window.installEventFilter(self)
        application = QGuiApplication.instance()
        if (
            application is not None
            and QGuiApplication.platformName() != "xcb"
            and hasattr(application, "applicationStateChanged")
            and application is not self._application
        ):
            if self._application is not None:
                self._application.applicationStateChanged.disconnect(
                    self._application_state_changed
                )
            self._application = cast(QGuiApplication, application)
            self._application.applicationStateChanged.connect(self._application_state_changed)

    def _application_state_changed(self, state: Qt.ApplicationState) -> None:
        if self.state == "capturing" and state != Qt.ApplicationActive:
            self.cancel()

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:  # noqa: N802
        minimized = event.type() == QEvent.WindowStateChange and bool(
            getattr(obj, "windowState", lambda: 0)() & Qt.WindowMinimized
        )
        if obj is self._window and (event.type() in (QEvent.Hide, QEvent.Close) or minimized):
            if self.state == "capturing":
                self.cancel()
        # На xcb наш XGrabKeyboard(root) сам даёт FocusOut(NotifyGrab),
        # который Qt превращает в WindowDeactivate; реальную смену ловит сторож.
        if (
            self.state == "capturing"
            and QGuiApplication.platformName() != "xcb"
            and obj is self._window
            and event.type() == QEvent.WindowDeactivate
        ):
            self.cancel()
        if obj is self._window and event.type() in (
            QEvent.Show,
            QEvent.Hide,
            QEvent.Close,
            QEvent.WindowStateChange,
        ):
            self.windowEvent.emit(obj, event)
        return bool(super().eventFilter(obj, event))

    @property
    def message(self) -> str:
        return self.hint or self._MESSAGES.get(self.state, "")

    def _set_state(self, state: str) -> None:
        if state != self.state:
            previous = self.message
            self.state = state
            self.hint = ""
            self.captureStateChanged.emit()
            if previous != self.message:
                self.captureMessageChanged.emit()

    def _set_hint(self, hint: str) -> None:
        previous = self.message
        self.hint = hint
        if previous != self.message:
            self.captureMessageChanged.emit()

    def _set_pending(self, combo: str) -> None:
        if combo != self.pending_combo:
            self.pending_combo = combo
            self.pendingComboChanged.emit()

    def _end(self) -> None:
        if self.host is not None:
            try:
                self.host.end_capture()
            except Exception:
                log.warning("Не удалось завершить захват клавиатуры", exc_info=True)

    def begin(self, save: Callable[[str, bool], str]) -> None:
        self._save = save
        self._set_hint("")
        self._set_pending("")
        try:
            if self.host is not None:
                set_callback = getattr(self.host, "set_capture_callback", None)
                if set_callback is None:
                    log.warning("Хост захвата не поддерживает передачу клавиш")
                else:
                    set_callback(self.keyEvent.emit)
            available = self.host is not None and self.host.begin_capture()
        except Exception:
            log.warning("Не удалось начать захват клавиатуры", exc_info=True)
            available = False
        if not available:
            self._end()
        self._set_state("capturing" if available else "not-grabbed")

    def end(self, combo: str, save: Callable[[str, bool], str] | None = None) -> None:
        if save is not None:
            self._save = save
        # Захват снимается до пробы, сохранения и применения.
        self._end()
        self._set_pending(combo)
        if not combo:
            self._set_state("idle")
            return
        if not is_valid_combo(combo):
            self._set_state("capturing")
            self._set_hint("Добавьте к клавише Ctrl, Alt или Win")
            self.refresh_candidates()
            return
        self._set_state("captured")
        try:
            code = self.host.probe(combo) if self.host is not None else "not-grabbed"
        except Exception:
            log.warning("Не удалось проверить сочетание клавиш", exc_info=True)
            code = "not-grabbed"
        if code == "ok":
            self._save_combo()
        else:
            self._set_state({"busy": "conflict", "duplicate": "duplicate"}.get(code, "not-grabbed"))

    def cancel(self) -> None:
        self._end()
        self._set_hint("")
        self._set_pending("")
        self._set_state("idle")

    def _save_combo(self, *, keep: bool = False) -> None:
        if self.host is None or self._save is None:
            self._set_state("not-grabbed")
            return
        try:
            self.keep_busy = keep
            try:
                code = self._save(self.pending_combo, keep)
            finally:
                self.keep_busy = False
        except Exception:
            log.warning("Не удалось применить сочетание клавиш", exc_info=True)
            code = "not-grabbed"
        if code == "ok":
            self._set_state("success")
        else:
            self._set_state({"busy": "conflict", "duplicate": "duplicate"}.get(code, "not-grabbed"))

    def keep(self) -> None:
        if self.state == "conflict" and self.pending_combo:
            self._save_combo(keep=True)

    def refresh_candidates(self) -> None:
        try:
            candidates = (
                self.host.free_candidates(list(DEFAULT_CANDIDATES)) if self.host is not None else []
            )
        except Exception:
            log.warning("Не удалось найти свободные сочетания клавиш", exc_info=True)
            candidates = []
            self._set_state("not-grabbed")
        if self.host is None:
            self._set_state("not-grabbed")
        if candidates != self.free_candidates:
            self.free_candidates = list(candidates)
            self.freeCandidatesChanged.emit()

    @pyqtSlot(str, str)
    def _handle_key_event(self, action: str, value: str) -> None:
        if self.state != "capturing":
            return
        if action == "combo":
            self.end(value)
        elif action == "cancel":
            self.cancel()
        elif action == "hint":
            self._set_hint(value)
        elif action == "expired":
            self._end()
            self._set_state("not-grabbed")
            self._set_hint("Время вышло — нажмите «Изменить» ещё раз")
