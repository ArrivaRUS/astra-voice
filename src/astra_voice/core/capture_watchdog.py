"""Независимый от цикла GUI сторож захвата клавиатуры поля комбинации (У47)."""

from __future__ import annotations

import logging
import math
import os
import select
import socket
import threading
from collections.abc import Callable
from time import monotonic

from astra_voice.platform.x11 import X11Display

log = logging.getLogger(__name__)
_JOIN_TIMEOUT_S = 0.5
_OPEN_TIMEOUT_S = 1.0


class CaptureFieldWatchdog:
    """Владеет страховочным X-соединением в собственном daemon-потоке.

    Xlib не потокобезопасен: фабрика вызывается внутри потока, запущенного
    open(), и ВСЕ обращения к созданному соединению происходят только там.
    Фабрика обязана создавать новое соединение, отдельное от основного.
    GUI получает факт захвата и события клавиш через потокобезопасный колбэк;
    соединение наружу не передаётся. Клавиши читает сам сторож, потому что
    XGrabKeyboard направляет их только захватившему X-клиенту.

    По дедлайну monotonic первым действием разрывается сокет: снятие захвата
    сервером не зависит от ответа на ungrab или работоспособности потока GUI.
    on_expired вызывается ИЗ ПОТОКА-СТОРОЖА после закрытия соединения. Колбэк
    обязан быть потокобезопасным: в Qt только queued-сигнал либо
    QMetaObject.invokeMethod; прямые действия с виджетами запрещены.

    Экземпляр одноразовый: после close/stop, отказа или истечения срока open
    возвращает False. Повторный open во время захвата не продлевает дедлайн.
    close/stop будит поток через pipe и ждёт join до 0,5 с; соединение
    закрывает сам поток. Зависший вызов фабрики/Xlib не задерживает close
    дольше этого срока; после возвращения вызова поток завершит очистку.
    """

    def __init__(
        self,
        *,
        display_factory: Callable[[], X11Display] = X11Display,
        poll_ms: int = 500,
        timeout_s: float = 30.0,
        on_expired: Callable[[], None] | None = None,
        on_key_event: Callable[[str, str], None] | None = None,
    ) -> None:
        if not 0 < poll_ms <= 1000:
            raise ValueError("Интервал опроса должен быть от 1 до 1000 мс")
        if not math.isfinite(timeout_s) or timeout_s <= 0:
            raise ValueError("Срок захвата должен быть конечным положительным числом")
        self._display_factory = display_factory
        self._poll_s = poll_ms / 1000
        self._timeout_s = min(timeout_s, 30.0)
        self.on_expired = on_expired
        self.on_key_event = on_key_event
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._active = threading.Event()
        self._thread: threading.Thread | None = None
        self._closed = False
        self._result_sent = False
        self._wake_read = self._wake_write = -1
        self._wake_closed = True

    def _close_wake_pipe(self) -> None:
        with self._lock:
            self._close_wake_pipe_unlocked()

    @property
    def active(self) -> bool:
        """Захват ещё держится, включая просроченный до закрытия соединения."""
        return self._active.is_set()

    def open(self, window_id: int | None = None) -> bool:
        """Запускает поток и ждёт факт захвата не более секунды; ошибка → False."""
        with self._lock:
            if self._closed:
                return False
            if self._thread is None:
                self._thread = threading.Thread(
                    target=self._run,
                    args=(window_id,),
                    name="capture-field-watchdog",
                    daemon=True,
                )
                try:
                    self._wake_read, self._wake_write = os.pipe()
                    self._wake_closed = False
                    self._thread.start()
                except Exception:
                    self._closed = True
                    self._stop.set()
                    self._close_wake_pipe_unlocked()
                    log.warning("Не удалось запустить поток сторожа поля захвата")
                    return False
        if self._ready.wait(_OPEN_TIMEOUT_S) and self.active and not self._stop.is_set():
            return True
        self.close()
        return False

    def close(self) -> None:
        """Идемпотентно останавливает поток без исключений и повторного запуска."""
        with self._lock:
            was_closed = self._closed
            self._closed = True
            self._stop.set()
            thread = self._thread
            if not was_closed and not self._wake_closed:
                try:
                    os.write(self._wake_write, b"x")
                except OSError:
                    log.debug("Не удалось разбудить поток сторожа", exc_info=True)
            if thread is None:
                self._close_wake_pipe_unlocked()
        if thread is not None and thread is not threading.current_thread():
            try:
                thread.join(_JOIN_TIMEOUT_S)
            except Exception:
                log.warning("Не удалось дождаться остановки сторожа поля захвата")

    def _close_wake_pipe_unlocked(self) -> None:
        if not self._wake_closed:
            self._wake_closed = True
            os.close(self._wake_read)
            os.close(self._wake_write)

    def stop(self) -> None:
        """Синоним close; экземпляр после остановки повторно не используется."""
        self.close()

    def _run(self, window_id: int | None) -> None:
        display: X11Display | None = None
        expired = False
        try:
            if self._stop.is_set():
                return
            display = self._display_factory()
            if not display.open() or self._stop.is_set():
                return
            deadline = monotonic() + self._timeout_s
            if not display.grab_keyboard(window_id, self._timeout_s):
                return
            self._active.set()
            self._ready.set()
            while not self._stop.is_set():
                if monotonic() >= deadline:
                    expired = True
                    break
                interval = min(self._poll_s, max(0.0, deadline - monotonic()))
                if self._result_sent:
                    self._stop.wait(interval)
                else:
                    self._poll_keys(display, interval, deadline)
        except Exception:
            log.warning("Ошибка сторожа поля захвата")
        finally:
            if display is not None:
                self._close_display(display)
            self._close_wake_pipe()
            self._active.clear()
            self._ready.set()
        if expired and self.on_expired is not None:
            try:
                self.on_expired()
            except Exception:
                log.warning("Не удалось уведомить поле об истечении захвата")

    def _poll_keys(
        self, display: X11Display, timeout: float, deadline: float | None = None
    ) -> None:
        """Читает все ожидающие события с соединения, удерживающего захват."""
        if not callable(getattr(display.d, "pending_events", None)):
            self._stop.wait(timeout)
            return
        if deadline is not None and monotonic() >= deadline:
            return
        try:
            pending = display.pending_events()
        except (AttributeError, ValueError, OSError):
            log.debug("Не удалось проверить события X11", exc_info=True)
            self._stop.wait(timeout)
            return
        if not pending:
            try:
                fd = display.fileno()
            except (AttributeError, ValueError, OSError):
                log.debug("Не удалось получить дескриптор X11", exc_info=True)
                self._stop.wait(timeout)
                return
            if fd >= 0:
                try:
                    select.select([fd, self._wake_read], [], [], timeout)
                except (ValueError, OSError):
                    log.debug("Не удалось ждать дескриптор X11", exc_info=True)
                    self._stop.wait(timeout)
                    return
            else:
                self._stop.wait(timeout)
        if deadline is not None and monotonic() >= deadline:
            return
        from Xlib import X

        while not self._stop.is_set():
            if deadline is not None and monotonic() >= deadline:
                return
            result: tuple[str, str] | None = None
            try:
                if not display.pending_events():
                    return
                event = display.next_event()
                if event.type == X.MappingNotify:
                    if event.request in (X.MappingKeyboard, X.MappingModifier):
                        display.refresh_keyboard_mapping(event)
                elif event.type == X.KeyPress:
                    result = self._key_event(display, int(event.detail), int(event.state))
            except (AttributeError, ValueError, OSError):
                log.debug("Не удалось прочитать событие X11", exc_info=True)
                self._stop.wait(timeout)
                return
            if result is not None and self.on_key_event is not None:
                self.on_key_event(*result)
                if result[0] in ("combo", "cancel"):
                    self._result_sent = True
                    return

    @staticmethod
    def _key_event(display: X11Display, keycode: int, state: int) -> tuple[str, str] | None:
        """Переводит клавишу X11 в формат моста или подсказку."""
        from Xlib import XK, X

        conn = display.d
        if conn is None:
            return None
        symbols = [int(conn.keycode_to_keysym(keycode, index) or 0) for index in range(4)]
        mods = display.semantic_modifiers(state)
        if XK.XK_Escape in symbols and not mods & (
            X.ControlMask | X.ShiftMask | X.Mod1Mask | X.Mod4Mask
        ):
            return "cancel", ""
        modifiers = {
            XK.string_to_keysym(name)
            for name in (
                "Control_L",
                "Control_R",
                "Shift_L",
                "Shift_R",
                "Alt_L",
                "Alt_R",
                "Meta_L",
                "Meta_R",
                "Super_L",
                "Super_R",
                "Mode_switch",
                "Caps_Lock",
            )
        } | {0xFE03}  # ISO_Level3_Shift / AltGr
        if any(symbol in modifiers for symbol in symbols):
            return None
        if any(0xFF80 <= symbol <= 0xFFBD for symbol in symbols):
            return "hint", "Эта клавиша не поддерживается. Выберите букву, цифру, пробел или F1–F12"
        key = next(
            (
                chr(symbol).upper()
                for symbol in symbols
                if 48 <= symbol <= 57 or 65 <= symbol <= 90 or 97 <= symbol <= 122
            ),
            None,
        )
        if key is None and XK.XK_space in symbols:
            key = "Space"
        if key is None:
            key = next(
                (
                    f"F{symbol - XK.XK_F1 + 1}"
                    for symbol in symbols
                    if XK.XK_F1 <= symbol <= XK.XK_F12
                ),
                None,
            )
        if key is None:
            return "hint", "Эта клавиша не поддерживается. Выберите букву, цифру, пробел или F1–F12"
        if not mods & (X.ControlMask | X.Mod1Mask | X.Mod4Mask):
            return "hint", "Добавьте к клавише Ctrl, Alt или Win"
        parts = [
            label
            for mask, label in (
                (X.ControlMask, "Ctrl"),
                (X.ShiftMask, "Shift"),
                (X.Mod1Mask, "Alt"),
                (X.Mod4Mask, "Super"),
            )
            if mods & mask
        ]
        return "combo", "+".join([*parts, key])

    @staticmethod
    def _close_display(display: X11Display) -> None:
        """Разрывает транспорт перед close, который в X11Display делает sync.

        python-xlib хранит сокет в Display.display.socket. Нельзя ограничиться
        X11Display.close(): его предварительный ungrab/sync ждёт сервер.
        Доступ к транспорту, как и к обёртке, остаётся в потоке-владельце.
        """
        try:
            if display.d is not None:
                transport = display.d.display.socket
                try:
                    transport.shutdown(socket.SHUT_RDWR)
                finally:
                    transport.close()
        except Exception:
            log.warning("Ошибка разрыва сокета поля захвата")
        finally:
            try:
                display.close()
            except Exception:
                log.warning("Не удалось закрыть соединение поля захвата")
