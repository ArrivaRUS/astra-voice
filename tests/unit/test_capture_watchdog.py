"""Сторож поля захвата: ручные часы и таймеры, без настоящего X-дисплея."""

from __future__ import annotations

import inspect
from collections.abc import Callable
from dataclasses import dataclass

import pytest

from astra_voice.core.capture_watchdog import CaptureFieldWatchdog
from astra_voice.platform.x11 import X11Display

pytestmark = pytest.mark.unit


class FakeDisplay(X11Display):
    """Имитирует отдельный сокет и сохраняет дедлайн при отказе снятия захвата."""

    def __init__(self, clock: Callable[[], float]) -> None:
        super().__init__()
        self.clock = clock
        self.calls: list[str] = []
        self.grab_args: tuple[int | None, float] | None = None
        self.open_ok = True
        self.grab_ok = True
        self.ungrab_ok = True
        self.raise_ungrab = False
        self.raise_close = False
        self.socket_open = False

    def open(self, display_name: str | None = None) -> bool:
        self.calls.append("open")
        self.socket_open = self.open_ok
        return self.open_ok

    def grab_keyboard(self, window_id: int | None = None, timeout_s: float = 30.0) -> bool:
        self.calls.append("grab")
        self.grab_args = (window_id, timeout_s)
        if self.grab_ok:
            self.keyboard_grab_deadline = self.clock() + timeout_s
        return self.grab_ok

    def keyboard_grab_expired(self) -> bool:
        self.calls.append("expired")
        return (
            self.keyboard_grab_deadline is not None and self.clock() >= self.keyboard_grab_deadline
        )

    def ungrab_keyboard(self) -> None:
        self.calls.append("ungrab")
        if self.raise_ungrab:
            raise RuntimeError("Ошибка снятия захвата")
        if self.ungrab_ok:
            self.keyboard_grab_deadline = None

    def close(self) -> None:
        self.calls.append("close")
        self.socket_open = False
        self.keyboard_grab_deadline = None
        if self.raise_close:
            raise RuntimeError("Ошибка закрытия")


@dataclass
class Timer:
    """Однократный ручной таймер; допускает доставку уже отменённого вызова."""

    delay: int
    callback: Callable[[], None]
    cancelled: bool = False


class Rig:
    """Создаёт соединения фабрикой, записывает таймеры и уведомления."""

    def __init__(self) -> None:
        self.now = 100.0
        self.displays: list[FakeDisplay] = []
        self.timers: list[Timer] = []
        self.expirations = 0
        self.configure: Callable[[FakeDisplay], None] = lambda display: None
        self.watchdog = CaptureFieldWatchdog(
            display_factory=self.factory,
            schedule=self.schedule,
            cancel_timer=self.cancel_timer,
            on_expired=self.on_expired,
        )

    def factory(self) -> FakeDisplay:
        display = FakeDisplay(lambda: self.now)
        self.configure(display)
        self.displays.append(display)
        return display

    def schedule(self, delay: int, callback: Callable[[], None]) -> object:
        timer = Timer(delay, callback)
        self.timers.append(timer)
        return timer

    def cancel_timer(self, handle: object) -> None:
        assert isinstance(handle, Timer)
        handle.cancelled = True

    def on_expired(self) -> None:
        assert not self.watchdog.active
        assert not self.displays[-1].socket_open
        self.expirations += 1


def test_open_and_periodic_poll() -> None:
    rig = Rig()
    assert not rig.watchdog.active
    assert rig.watchdog.open(42)
    assert rig.watchdog.active
    assert rig.displays[0].grab_args == (42, 30.0)
    assert rig.displays[0].calls == ["open", "grab"]
    for _ in range(3):
        timer = rig.timers[-1]
        assert timer.delay == 500
        rig.now += timer.delay / 1000
        timer.callback()
    assert len(rig.timers) == 4
    assert rig.displays[0].calls.count("expired") == 3
    assert rig.watchdog.active


def test_grab_failure_closes_connection() -> None:
    rig = Rig()
    rig.configure = lambda display: setattr(display, "grab_ok", False)
    assert not rig.watchdog.open()
    assert rig.displays[0].calls[-1] == "close"
    assert not rig.displays[0].socket_open
    assert not rig.watchdog.active
    assert not rig.timers


@pytest.mark.parametrize("via_timer", [False, True])
def test_expiry_closes_connection_and_notifies_once(via_timer: bool) -> None:
    rig = Rig()
    assert rig.watchdog.open()
    timer = rig.timers[-1]
    rig.now += 29.999
    rig.watchdog.tick()
    assert rig.watchdog.active
    assert rig.expirations == 0
    rig.now = 130.0
    assert rig.watchdog.active  # Само истечение срока ещё не снимает захват.
    if via_timer:
        timer.callback()
    else:
        rig.watchdog.tick()
        assert timer.cancelled
    assert rig.displays[0].calls[-2:] == ["ungrab", "close"]
    assert not rig.displays[0].socket_open
    assert not rig.watchdog.active
    assert rig.expirations == 1
    rig.watchdog.tick()
    timer.callback()
    rig.watchdog.close()
    assert len(rig.timers) == 1
    assert rig.expirations == 1
    assert rig.displays[0].calls.count("close") == 1


@pytest.mark.parametrize("expires", [False, True])
@pytest.mark.parametrize("raises", [False, True])
def test_failed_ungrab_still_closes_socket(expires: bool, raises: bool) -> None:
    rig = Rig()
    assert rig.watchdog.open()
    display = rig.displays[0]
    display.ungrab_ok = False
    display.raise_ungrab = raises
    if expires:
        rig.now += 30.0
        rig.watchdog.tick()
    else:
        rig.watchdog.close()
    assert display.calls[-2:] == ["ungrab", "close"]
    assert not display.socket_open
    assert not rig.watchdog.active
    assert rig.timers[-1].cancelled
    assert rig.expirations == int(expires)


def test_close_is_idempotent() -> None:
    rig = Rig()
    rig.watchdog.close()
    assert rig.watchdog.open()
    rig.watchdog.close()
    calls = rig.displays[0].calls.copy()
    rig.watchdog.close()
    rig.timers[0].callback()
    assert rig.displays[0].calls == calls
    assert calls[-2:] == ["ungrab", "close"]
    assert rig.timers[0].cancelled
    assert not rig.watchdog.active
    assert rig.expirations == 0


def test_close_suppresses_errors_and_cancels_timer() -> None:
    rig = Rig()
    assert rig.watchdog.open()
    rig.displays[0].raise_close = True
    rig.watchdog.close()
    rig.watchdog.close()
    assert rig.timers[0].cancelled
    assert not rig.watchdog.active


@pytest.mark.parametrize("poll_ms", [1001, 2000, 0, -1])
def test_invalid_poll_interval(poll_ms: int) -> None:
    rig = Rig()
    with pytest.raises(ValueError):
        CaptureFieldWatchdog(schedule=rig.schedule, cancel_timer=rig.cancel_timer, poll_ms=poll_ms)


def test_custom_interval_and_timeout() -> None:
    rig = Rig()
    watchdog = CaptureFieldWatchdog(
        display_factory=rig.factory,
        schedule=rig.schedule,
        cancel_timer=rig.cancel_timer,
        poll_ms=1000,
        timeout_s=2.0,
    )
    assert watchdog.open()
    assert rig.displays[0].grab_args == (None, 2.0)
    assert rig.timers[0].delay == 1000
    rig.now += 2.0
    rig.timers[0].callback()
    assert not watchdog.active
    assert not rig.displays[0].socket_open


def test_no_x_is_safe() -> None:
    rig = Rig()
    rig.configure = lambda display: setattr(display, "open_ok", False)
    assert not rig.watchdog.open()
    assert "grab" not in rig.displays[0].calls
    assert rig.displays[0].calls[-1] == "close"
    rig.watchdog.tick()
    rig.watchdog.close()
    assert not rig.watchdog.active
    assert not rig.timers


def test_factory_creates_dedicated_connections() -> None:
    rig = Rig()
    main_display = FakeDisplay(lambda: rig.now)
    assert not rig.displays
    assert rig.watchdog.open()
    assert rig.displays[0] is not main_display
    rig.watchdog.close()
    assert rig.watchdog.open()
    assert len(rig.displays) == 2
    assert rig.displays[1] is not rig.displays[0]
    rig.watchdog.close()
    assert main_display.calls == []
    parameters = inspect.signature(CaptureFieldWatchdog).parameters
    assert parameters["display_factory"].default is X11Display
    assert "display" not in parameters
    assert "main_display" not in parameters


def test_reopen_ignores_stale_timer_and_does_not_extend_active_grab() -> None:
    rig = Rig()
    assert rig.watchdog.open()
    rig.now += 10.0
    assert rig.watchdog.open()
    assert len(rig.displays) == len(rig.timers) == 1
    assert rig.displays[0].keyboard_grab_deadline == 130.0
    stale = rig.timers[0]
    rig.watchdog.close()
    assert rig.watchdog.open()
    rig.now = 130.0
    stale.callback()
    assert rig.watchdog.active
    assert rig.displays[1].calls == ["open", "grab"]
    assert len(rig.timers) == 2
    rig.now = 140.0
    rig.timers[1].callback()
    assert not rig.watchdog.active
    assert rig.expirations == 1
