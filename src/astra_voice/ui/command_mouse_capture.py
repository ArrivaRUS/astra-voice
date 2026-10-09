"""Local mouse assignment with an explicit, runtime-owned admission lease."""

from __future__ import annotations

import logging
import math
import time
import weakref
from collections.abc import Callable
from functools import partial
from typing import Protocol

from PyQt5 import sip
from PyQt5.QtCore import QEvent, QObject, Qt, QTimer, pyqtSignal

from astra_voice.core.settings import is_valid_command_mouse_button
from astra_voice.platform.mouse_button import qt_button_to_logical

log = logging.getLogger(__name__)


class CommandMouseHost(Protocol):
    """All methods and notifications run on the GUI thread; see command-mouse.md."""

    command_mouse_status: str
    command_mouse_status_message: str
    command_mouse_can_edit: bool
    on_command_mouse_changed: Callable[[], None] | None

    def begin_command_mouse_capture(self) -> object | None: ...

    def command_mouse_capture_valid(self, token: object) -> bool: ...

    def end_command_mouse_capture(self, token: object) -> None: ...

    def reload_command_mouse(self) -> None: ...


def mouse_button_label(button: int) -> str:
    if button == 2:
        return "Средняя кнопка"
    if button in (8, 9):
        return f"Дополнительная кнопка {button - 7}"
    return f"Кнопка мыши {button}" if is_valid_command_mouse_button(button) else ""


class _Lease:
    """Non-QObject cleanup survives destruction of the controller's C++ object."""

    def __init__(self) -> None:
        self.host: CommandMouseHost | None = None
        self.token: object | None = None

    def release(self, *_: object) -> None:
        host, token = self.host, self.token
        # Invalidate before external code: end() may synchronously notify/reenter.
        self.host, self.token = None, None
        if host is not None and token is not None:
            try:
                host.end_command_mouse_capture(token)
            except Exception:
                log.warning("Не удалось завершить выбор кнопки мыши")


def _host_notification(ref: weakref.ReferenceType[CommandMouseCapture], epoch: int) -> None:
    controller = ref()
    if controller is not None and not sip.isdeleted(controller):
        controller.hostChanged.emit(epoch)


class CommandMouseCapture(QObject):
    """No global input grabs; QML forwards only events from its local field."""

    changed = pyqtSignal()
    availabilityChanged = pyqtSignal()
    hostChanged = pyqtSignal(int)
    TIMEOUT_MS = 30_000

    def __init__(
        self,
        parent: QObject | None = None,
        *,
        keyboard_active: Callable[[], bool] = lambda: False,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__(parent)
        self.state = "idle"
        self.pending_button = 0
        self.message = ""
        self.host: CommandMouseHost | None = None
        self._keyboard_active = keyboard_active
        self._clock = clock
        self._lease = _Lease()
        self._epoch = 0
        self._host_epoch = 0
        self._acquiring = False
        self._pressed = 0
        self._await_release = False
        self._deadline = 0.0
        self._window: QObject | None = None
        self._reload_failed = False
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._expire)
        self.hostChanged.connect(self._host_changed)
        self.destroyed.connect(self._lease.release)

    @property
    def generation(self) -> int:
        return self._epoch

    @property
    def active(self) -> bool:
        return self._acquiring or self._lease.token is not None

    @property
    def can_edit(self) -> bool:
        try:
            return bool(
                self.host is not None
                and self.host.command_mouse_can_edit is True
                and not self._keyboard_active()
            )
        except Exception:
            return False

    @property
    def status(self) -> str:
        try:
            status = self.host.command_mouse_status if self.host is not None else "unavailable"
            if not self._reload_failed and status in {
                "disabled",
                "ready",
                "busy",
                "unavailable",
                "suspended",
            }:
                return status
        except Exception:
            pass
        return "unavailable"

    @property
    def status_message(self) -> str:
        if self.host is None or self._reload_failed:
            return "Управление кнопкой мыши пока недоступно"
        try:
            message = self.host.command_mouse_status_message
            return message if isinstance(message, str) else ""
        except Exception:
            return "Управление кнопкой мыши пока недоступно"

    def bind_host(self, host: CommandMouseHost | None) -> None:
        self._host_epoch += 1
        self.cancel()
        self.host = None
        self._reload_failed = False
        methods = (
            "begin_command_mouse_capture",
            "end_command_mouse_capture",
            "command_mouse_capture_valid",
            "reload_command_mouse",
        )
        try:
            if host is not None and all(callable(getattr(host, name, None)) for name in methods):
                if (
                    type(host.command_mouse_can_edit) is bool
                    and isinstance(host.command_mouse_status, str)
                    and isinstance(host.command_mouse_status_message, str)
                ):
                    host.on_command_mouse_changed = partial(
                        _host_notification, weakref.ref(self), self._host_epoch
                    )
                    self.host = host
        except Exception:
            log.warning("Порт выбора кнопки мыши недоступен")
        self.availabilityChanged.emit()

    def _host_changed(self, epoch: int) -> None:
        if epoch != self._host_epoch:
            return
        self._reload_failed = False
        if self._lease.token is not None:
            self.validate()
        self.availabilityChanged.emit()

    def _set(self, state: str, button: int = 0, message: str = "") -> None:
        self.state, self.pending_button, self.message = state, button, message
        self.changed.emit()

    def validate(self) -> bool:
        host, token = self._lease.host, self._lease.token
        epoch = self._epoch
        try:
            valid = (
                host is not None
                and host is self.host
                and token is not None
                and self.can_edit
                and (not self._deadline or self._clock() < self._deadline)
                and host.command_mouse_capture_valid(token) is True
            )
        except Exception:
            valid = False
        valid = bool(valid and epoch == self._epoch and token is self._lease.token)
        if not valid and self.active and epoch == self._epoch:
            self.cancel()
        return valid

    def begin(self) -> bool:
        if self.active:
            self.validate()
            return False
        self.cancel()
        if not self.can_edit:
            self._set("error", message="Выбор кнопки сейчас недоступен")
            return False
        host, epoch = self.host, self._epoch
        assert host is not None
        self._acquiring = True
        try:
            token = host.begin_command_mouse_capture()
        except Exception:
            token = None
        self._acquiring = False
        if token is None or isinstance(token, bool):
            if epoch == self._epoch:
                self._set("error", message="Выбор кнопки сейчас недоступен")
            return False
        lease = _Lease()
        lease.host, lease.token = host, token
        if epoch != self._epoch or host is not self.host:
            lease.release()
            return False
        self._lease.host, self._lease.token = host, token
        if not self.validate():
            return False
        self._deadline = self._clock() + self.TIMEOUT_MS / 1000
        self._timer.start(self.TIMEOUT_MS)
        self._set("capturing", message="Нажмите среднюю или дополнительную кнопку")
        return epoch == self._epoch and self.validate()

    def press(self, qt_button: int) -> None:
        if not self.active or not self.validate():
            return
        button = qt_button_to_logical(qt_button)
        if self._await_release or button is None or self.state == "ready":
            self._pressed = 0
            self._await_release = True
            self._set("error", message="Отпустите все кнопки и выберите одну допустимую кнопку")
            return
        self._pressed = qt_button
        self._await_release = True
        self._set("pressed", button, "Отпустите кнопку мыши")

    def release(self, qt_button: int, qt_buttons_remaining: int) -> None:
        if not self.active or not self.validate():
            return
        complete = (
            self.state == "pressed"
            and type(qt_button) is int
            and qt_button == self._pressed
            and type(qt_buttons_remaining) is int
            and qt_buttons_remaining == 0
        )
        self._pressed = 0
        self._await_release = qt_buttons_remaining != 0
        if complete:
            self._set("ready", self.pending_button)
        else:
            self._set("error", message="Отпустите все кнопки и повторите выбор")

    def select(self, button: int) -> bool:
        if self.active:
            self.validate()
            return False
        if not is_valid_command_mouse_button(button) or not self.begin():
            return False
        epoch = self._epoch
        if not self.validate():
            return False
        self._set("ready", button)
        return epoch == self._epoch and self.validate()

    def ready(self) -> bool:
        return bool(
            self.validate()
            and self.state == "ready"
            and not self._await_release
            and is_valid_command_mouse_button(self.pending_button)
        )

    def save_failed(self) -> None:
        # Keep the released candidate and lease for retry; never treat it as saved.
        if self.ready():
            self._set(
                "ready", self.pending_button, "Не удалось сохранить настройки. Повторите попытку"
            )

    def reload(self) -> None:
        try:
            if self.host is not None:
                self.host.reload_command_mouse()
        except Exception:
            self._reload_failed = True
            log.warning("Не удалось применить настройку кнопки мыши")
        self.availabilityChanged.emit()

    def cancel(self, *_: object) -> None:
        self._epoch += 1
        self._acquiring = False
        self._timer.stop()
        self._deadline = 0
        self._pressed = 0
        self._await_release = False
        epoch = self._epoch
        self.state, self.pending_button, self.message = "idle", 0, ""
        self._lease.release()
        if epoch == self._epoch:
            self.changed.emit()

    def _expire(self) -> None:
        if not self.active:
            return
        remaining = self._deadline - self._clock()
        if remaining > 0:
            # A queued timeout from the previous editor cannot cancel a new lease.
            self._timer.start(max(1, math.ceil(remaining * 1000)))
            return
        self.cancel()

    def attach_window(self, window: QObject) -> None:
        if window is self._window:
            return
        self.cancel()
        if self._window is not None and not sip.isdeleted(self._window):
            self._window.removeEventFilter(self)
            self._window.destroyed.disconnect(self._window_destroyed)
        self._window = window
        window.installEventFilter(self)
        window.destroyed.connect(self._window_destroyed)

    def _window_destroyed(self, *_: object) -> None:
        self._window = None
        self.cancel()

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:  # noqa: N802
        if obj is self._window:
            minimized = event.type() == QEvent.WindowStateChange and bool(
                getattr(obj, "windowState", lambda: 0)() & Qt.WindowMinimized
            )
            escape = (
                event.type() == QEvent.KeyPress
                and getattr(event, "key", lambda: 0)() == Qt.Key_Escape
            )
            if (
                event.type() in (QEvent.Hide, QEvent.Close, QEvent.WindowDeactivate)
                or minimized
                or escape
            ):
                self.cancel()
        return bool(super().eventFilter(obj, event))
