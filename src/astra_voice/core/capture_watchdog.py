"""Активный опрос срока захвата клавиатуры отдельным полем комбинации (У47)."""

from __future__ import annotations

import logging
from collections.abc import Callable

from astra_voice.platform.x11 import X11Display

log = logging.getLogger(__name__)


class CaptureFieldWatchdog:
    """Владеет отдельным соединением поля и закрывает его по сроку захвата.

    Фабрика должна создавать новое соединение, не используемое приложением.
    schedule откладывает однократный вызов в потоке этого соединения;
    cancel_timer отменяет полученную ручку. Прямые вызовы также идут в этом
    потоке: Xlib не потокобезопасен. При остановке потока или процесса такой
    планировщик не может обслуживать срок.
    """

    def __init__(
        self,
        *,
        display_factory: Callable[[], X11Display] = X11Display,
        schedule: Callable[[int, Callable[[], None]], object],
        cancel_timer: Callable[[object], None],
        poll_ms: int = 500,
        timeout_s: float = 30.0,
        on_expired: Callable[[], None] | None = None,
    ) -> None:
        if not 0 < poll_ms <= 1000:
            raise ValueError("Интервал опроса должен быть от 1 до 1000 мс")
        self._display_factory = display_factory
        self._schedule = schedule
        self._cancel_timer = cancel_timer
        self._poll_ms = poll_ms
        self._timeout_s = timeout_s
        self._display: X11Display | None = None
        self._timer: tuple[object, object] | None = None
        self.on_expired = on_expired

    @property
    def active(self) -> bool:
        """Захват ещё держится, включая просроченный до фактического снятия."""
        return self._display is not None and self._display.keyboard_grab_deadline is not None

    def open(self, window_id: int | None = None) -> bool:
        """Открывает своё соединение и захват; повтор не продлевает срок."""
        if self.active:
            return True
        self.close()
        try:
            display = self._display_factory()
            self._display = display
            if display.open() and display.grab_keyboard(window_id, self._timeout_s):
                self._arm_timer()
                return True
        except Exception:
            log.warning("Не удалось запустить сторож поля захвата")
        self.close()
        return False

    def tick(self) -> None:
        """Проверяет срок; при истечении закрывает соединение и уведомляет поле."""
        display = self._display
        if display is None or not self.active:
            return
        if display.keyboard_grab_expired():
            self.close()
            if self.on_expired is not None:
                try:
                    self.on_expired()
                except Exception:
                    log.warning("Не удалось уведомить поле об истечении захвата")

    def close(self) -> None:
        """Снимает захват, закрывает соединение и отменяет опрос без исключений."""
        display, self._display = self._display, None
        timer, self._timer = self._timer, None
        if display is not None:
            try:
                display.ungrab_keyboard()
            except Exception:
                log.warning("Не удалось снять захват поля; закрываем соединение")
            finally:
                # При ошибке ungrab дедлайн остаётся заданным. Закрытие нужно
                # безусловно: сервер снимет захват при закрытии сокета.
                try:
                    display.close()
                except Exception:
                    log.warning("Не удалось закрыть соединение поля захвата")
        if timer is not None:
            try:
                self._cancel_timer(timer[1])
            except Exception:
                log.warning("Не удалось отменить таймер поля захвата")

    def _arm_timer(self) -> None:
        """Ставит один опрос; устаревшие вызовы после отмены ничего не делают."""
        token = object()

        def fire() -> None:
            if self._timer is None or self._timer[0] is not token:
                return
            self._timer = None
            self.tick()
            if self.active and self._timer is None:
                try:
                    self._arm_timer()
                except Exception:
                    log.warning("Не удалось продолжить опрос поля захвата")
                    self.close()

        self._timer = (token, self._schedule(self._poll_ms, fire))
