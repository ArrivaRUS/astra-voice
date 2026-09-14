"""Сторож в настоящем потоке: события и управляемые часы, без X-дисплея."""

from __future__ import annotations

import socket
import threading
from collections.abc import Iterator
from types import SimpleNamespace

import pytest

from astra_voice.core import capture_watchdog as module
from astra_voice.core.capture_watchdog import CaptureFieldWatchdog
from astra_voice.platform.x11 import X11Display

pytestmark = pytest.mark.unit
WAIT_S = 1.0


class FakeSocket:
    """Наблюдаемый транспорт, который закрывается до вежливой очистки Xlib."""

    def __init__(self, owner: FakeDisplay) -> None:
        self.owner = owner
        self.closed = False

    def shutdown(self, how: int) -> None:
        self.owner.record("socket.shutdown")
        assert how == socket.SHUT_RDWR

    def close(self) -> None:
        self.owner.record("socket.close")
        self.closed = True


class FakeDisplay(X11Display):
    """Оставляет дедлайн при отказе ungrab; все вызовы записаны с их потоком."""

    def __init__(self, rig: Rig) -> None:
        super().__init__()
        self.rig = rig
        self.calls: list[tuple[str, int]] = []
        self.record("create")
        self.transport = FakeSocket(self)
        self.grab_args: tuple[int | None, float] | None = None
        self.deadline_at_close: float | None = None

    def record(self, action: str) -> None:
        self.calls.append((action, threading.get_ident()))

    def open(self, display_name: str | None = None) -> bool:
        self.record("open")
        if self.rig.open_ok:
            self.d = SimpleNamespace(display=SimpleNamespace(socket=self.transport))
        return self.rig.open_ok

    def grab_keyboard(self, window_id: int | None = None, timeout_s: float = 30.0) -> bool:
        self.record("grab")
        self.grab_args = window_id, timeout_s
        if self.rig.grab_ok:
            self.keyboard_grab_deadline = 100.0 + timeout_s
        return self.rig.grab_ok

    def ungrab_keyboard(self) -> None:
        self.record("ungrab")
        if self.rig.raise_ungrab:
            raise RuntimeError("Ошибка снятия захвата")
        if self.rig.ungrab_ok:
            self.keyboard_grab_deadline = None

    def close(self) -> None:
        self.record("close")
        try:
            # Имитируем X11Display.close: вежливая очистка может не сработать.
            self.ungrab_keyboard()
        finally:
            self.deadline_at_close = self.keyboard_grab_deadline
            self.d = None
            self.rig.closed.set()
        if self.rig.raise_close:
            raise RuntimeError("Ошибка закрытия")


class Rig:
    """Фабрика выполняется в стороже, Event переводит часы за дедлайн."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.elapsed = threading.Event()
        self.polled = threading.Event()
        self.closed = threading.Event()
        self.expired = threading.Event()
        self.clock_calls = 0
        self.displays: list[FakeDisplay] = []
        self.callback_threads: list[int] = []
        self.callback_saw_closed = False
        self.open_ok = True
        self.grab_ok = True
        self.ungrab_ok = True
        self.raise_ungrab = False
        self.raise_close = False
        self.raise_callback = False
        monkeypatch.setattr(module, "monotonic", self.clock)
        self.watchdog = CaptureFieldWatchdog(
            display_factory=self.factory, poll_ms=1, on_expired=self.on_expired
        )

    def clock(self) -> float:
        self.clock_calls += 1
        if self.clock_calls > 1:
            self.polled.set()
        return 130.0 if self.elapsed.is_set() else 100.0

    def factory(self) -> FakeDisplay:
        display = FakeDisplay(self)
        self.displays.append(display)
        return display

    def on_expired(self) -> None:
        self.callback_threads.append(threading.get_ident())
        self.callback_saw_closed = self.closed.is_set() and not self.watchdog.active
        self.expired.set()
        if self.raise_callback:
            raise RuntimeError("Ошибка уведомления")

    def assert_stopped(self) -> None:
        thread = self.watchdog._thread
        assert thread is not None, "Поток сторожа не был создан"
        assert not thread.is_alive(), "Поток сторожа не остановился после close()"
        assert not self.watchdog.active


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch) -> Iterator[Rig]:
    instance = Rig(monkeypatch)
    try:
        yield instance
    finally:
        instance.watchdog.close()


def test_expiry_closes_socket_without_gui_event_loop(rig: Rig) -> None:
    assert rig.watchdog.open(42)
    assert rig.watchdog.active
    assert rig.polled.wait(WAIT_S), "Сторож не начал самостоятельный опрос"
    assert not rig.closed.is_set(), "Соединение закрыто до дедлайна"
    assert rig.watchdog.open(42)
    assert len(rig.displays) == 1, "Повторный open создал лишний захват"
    rig.elapsed.set()
    # Вызывающий поток только ждёт: ни tick(), ни доставки Qt-таймеров.
    assert rig.closed.wait(WAIT_S), "Сторож не закрыл соединение по дедлайну"
    assert rig.expired.wait(WAIT_S), "Сторож не уведомил об истечении срока"
    rig.watchdog.close()
    rig.assert_stopped()
    display = rig.displays[0]
    assert display.grab_args == (42, 30.0)
    actions = [action for action, _ in display.calls]
    assert actions == [
        "create",
        "open",
        "grab",
        "socket.shutdown",
        "socket.close",
        "close",
        "ungrab",
    ]
    assert display.transport.closed
    assert rig.callback_saw_closed
    thread = rig.watchdog._thread
    assert thread is not None and thread.daemon
    assert rig.callback_threads == [thread.ident]
    assert thread.ident != threading.get_ident()
    assert not rig.watchdog.open(), "Одноразовый сторож запустился после истечения срока"


@pytest.mark.parametrize("expires", [False, True])
@pytest.mark.parametrize("raises", [False, True])
def test_failed_ungrab_still_closes_socket(rig: Rig, expires: bool, raises: bool) -> None:
    rig.ungrab_ok = False
    rig.raise_ungrab = raises
    assert rig.watchdog.open()
    if expires:
        rig.elapsed.set()
        assert rig.expired.wait(WAIT_S), "Сбой ungrab помешал сторожу завершить захват"
    rig.watchdog.close()
    rig.assert_stopped()
    display = rig.displays[0]
    assert display.deadline_at_close == 130.0, "Фейк должен сохранить дедлайн при сбое ungrab"
    assert display.transport.closed, "Отказ ungrab оставил сокет открытым"
    assert rig.closed.is_set()
    assert rig.expired.is_set() is expires


def test_close_stops_thread_and_is_idempotent(rig: Rig) -> None:
    assert rig.watchdog.open()
    rig.watchdog.close()
    rig.assert_stopped()
    calls = rig.displays[0].calls.copy()
    rig.watchdog.close()
    rig.watchdog.stop()
    assert rig.displays[0].calls == calls
    assert rig.displays[0].transport.closed
    assert not rig.expired.is_set()
    assert not rig.watchdog.open()


def test_close_before_open_is_safe_and_terminal(rig: Rig) -> None:
    rig.watchdog.close()
    rig.watchdog.close()
    assert not rig.watchdog.open()
    assert not rig.displays


def test_grab_failure_leaves_no_thread(rig: Rig) -> None:
    rig.grab_ok = False
    assert not rig.watchdog.open()
    rig.assert_stopped()
    assert rig.closed.is_set()
    assert rig.displays[0].transport.closed
    assert not rig.expired.is_set()


def test_no_x_is_safe(rig: Rig) -> None:
    rig.open_ok = False
    assert not rig.watchdog.open()
    rig.watchdog.close()
    rig.assert_stopped()
    assert rig.closed.is_set()
    assert "grab" not in [action for action, _ in rig.displays[0].calls]


def test_factory_failure_leaves_no_thread(rig: Rig) -> None:
    def fail() -> X11Display:
        raise RuntimeError("X недоступен")

    rig.watchdog = CaptureFieldWatchdog(display_factory=fail, poll_ms=1)
    assert not rig.watchdog.open()
    rig.watchdog.close()
    rig.assert_stopped()


def test_close_and_callback_errors_do_not_escape(rig: Rig) -> None:
    rig.raise_close = True
    rig.raise_callback = True
    assert rig.watchdog.open()
    rig.elapsed.set()
    assert rig.expired.wait(WAIT_S), "Ошибка закрытия подавила уведомление"
    rig.watchdog.close()
    rig.watchdog.close()
    rig.assert_stopped()
    assert rig.displays[0].transport.closed


@pytest.mark.parametrize("poll_ms", [1001, 2000, 0, -1])
def test_invalid_poll_interval(poll_ms: int) -> None:
    with pytest.raises(ValueError, match="Интервал опроса"):
        CaptureFieldWatchdog(poll_ms=poll_ms)


def test_max_poll_interval_does_not_delay_stop(rig: Rig) -> None:
    rig.watchdog = CaptureFieldWatchdog(display_factory=rig.factory, poll_ms=1000, timeout_s=2.0)
    assert rig.watchdog.open()
    assert rig.displays[0].grab_args == (None, 2.0)
    rig.watchdog.close()
    rig.assert_stopped()


def test_connection_is_created_and_used_only_by_watchdog_thread(rig: Rig) -> None:
    main_display = FakeDisplay(rig)
    main_calls = main_display.calls.copy()
    assert not rig.displays
    assert rig.watchdog.open()
    # active читает Event, не свойства Xlib в потоке GUI.
    assert rig.watchdog.active
    rig.watchdog.close()
    rig.assert_stopped()
    display = rig.displays[0]
    assert display is not main_display
    thread = rig.watchdog._thread
    assert thread is not None and thread.ident != threading.get_ident()
    assert {ident for _, ident in display.calls} == {thread.ident}
    assert main_display.calls == main_calls
