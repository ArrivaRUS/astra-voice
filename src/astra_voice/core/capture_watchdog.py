"""Независимый от цикла GUI сторож захвата клавиатуры поля комбинации (У47)."""

from __future__ import annotations

import logging
import math
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
    GUI получает только факт захвата через Event (active и результат open),
    соединение наружу не передаётся. Чтение клавиш будущим полем должно идти
    его обычным путём; этот класс владеет только страховочным соединением.

    По дедлайну monotonic первым действием разрывается сокет: снятие захвата
    сервером не зависит от ответа на ungrab или работоспособности потока GUI.
    on_expired вызывается ИЗ ПОТОКА-СТОРОЖА после закрытия соединения. Колбэк
    обязан быть потокобезопасным: в Qt только queued-сигнал либо
    QMetaObject.invokeMethod; прямые действия с виджетами запрещены.

    Экземпляр одноразовый: после close/stop, отказа или истечения срока open
    возвращает False. Повторный open во время захвата не продлевает дедлайн.
    close/stop будит поток через Event и ждёт join до 0,5 с; соединение
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
    ) -> None:
        if not 0 < poll_ms <= 1000:
            raise ValueError("Интервал опроса должен быть от 1 до 1000 мс")
        if not math.isfinite(timeout_s) or timeout_s <= 0:
            raise ValueError("Срок захвата должен быть конечным положительным числом")
        self._display_factory = display_factory
        self._poll_s = poll_ms / 1000
        self._timeout_s = min(timeout_s, 30.0)
        self._on_expired = on_expired
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._active = threading.Event()
        self._thread: threading.Thread | None = None
        self._closed = False

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
                    self._thread.start()
                except Exception:
                    self._closed = True
                    self._stop.set()
                    log.warning("Не удалось запустить поток сторожа поля захвата")
                    return False
        if self._ready.wait(_OPEN_TIMEOUT_S) and self.active and not self._stop.is_set():
            return True
        self.close()
        return False

    def close(self) -> None:
        """Идемпотентно останавливает поток без исключений и повторного запуска."""
        with self._lock:
            self._closed = True
            self._stop.set()
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            try:
                thread.join(_JOIN_TIMEOUT_S)
            except Exception:
                log.warning("Не удалось дождаться остановки сторожа поля захвата")

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
            while not self._stop.wait(self._poll_s):
                if monotonic() >= deadline:
                    expired = True
                    break
        except Exception:
            log.warning("Ошибка сторожа поля захвата")
        finally:
            if display is not None:
                self._close_display(display)
            self._active.clear()
            self._ready.set()
        if expired and self._on_expired is not None:
            try:
                self._on_expired()
            except Exception:
                log.warning("Не удалось уведомить поле об истечении захвата")

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
