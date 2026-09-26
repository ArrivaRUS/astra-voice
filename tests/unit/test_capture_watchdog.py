"""Сторож в настоящем потоке: события и управляемые часы, без X-дисплея."""

from __future__ import annotations

import logging
import os
import socket
import threading
import time
from collections.abc import Iterator
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from astra_voice.core import capture_watchdog as module
from astra_voice.core.capture_watchdog import CaptureFieldWatchdog
from astra_voice.platform.x11 import X11Display

pytestmark = pytest.mark.unit
WAIT_S = 1.0


def test_unused_watchdog_does_not_open_pipe(monkeypatch: pytest.MonkeyPatch) -> None:
    pipe = Mock(wraps=os.pipe)
    monkeypatch.setattr("astra_voice.core.capture_watchdog.os.pipe", pipe)
    watchdog = CaptureFieldWatchdog()
    pipe.assert_not_called()
    watchdog.close()
    pipe.assert_not_called()


def test_pipe_failure_does_not_create_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    thread = Mock()
    monkeypatch.setattr(
        "astra_voice.core.capture_watchdog.os.pipe", Mock(side_effect=OSError("pipe failed"))
    )
    monkeypatch.setattr("astra_voice.core.capture_watchdog.threading.Thread", thread)
    watchdog = CaptureFieldWatchdog()
    assert not watchdog.open()
    assert watchdog._thread is None
    thread.assert_not_called()
    assert watchdog._wake_closed


@pytest.mark.parametrize("failure", ["select", "next_event"])
def test_event_read_failure_waits(monkeypatch: pytest.MonkeyPatch, failure: str) -> None:
    display = Mock(spec=X11Display)
    display.d = SimpleNamespace(pending_events=lambda: 0)
    display.pending_events.return_value = 0 if failure == "select" else 1
    display.fileno.return_value = 42
    display.next_event.side_effect = OSError("Ошибка чтения")
    monkeypatch.setattr(
        "astra_voice.core.capture_watchdog.select.select",
        Mock(side_effect=OSError("Ошибка ожидания")),
    )
    watchdog = CaptureFieldWatchdog()
    wait = Mock()
    monkeypatch.setattr(watchdog._stop, "wait", wait)
    watchdog._poll_keys(display, 0.25)
    wait.assert_called_once_with(0.25)


def test_caps_lock_is_silent() -> None:
    from Xlib import XK

    display = X11Display()
    display.d = SimpleNamespace(keycode_to_keysym=lambda _code, _index: XK.XK_Caps_Lock)
    assert CaptureFieldWatchdog._key_event(display, 66, 0) is None


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


class PropertyDisplay(FakeDisplay):
    """PropertyNotify будит настоящий select через fd фейкового соединения."""

    def __init__(self, rig: Rig, *, property_present: bool) -> None:
        super().__init__(rig)
        self.property_present = property_present
        self.active_id: int | None = 42 if property_present else None
        self.events: list[object] = []
        self.read_fd, self.write_fd = os.pipe()

    def open(self, display_name: str | None = None) -> bool:
        opened = super().open(display_name)
        if opened:
            self.d.pending_events = lambda: len(self.events)
        return opened

    def watch_active_window(self) -> tuple[int, int | None] | None:
        self.record("watch")
        return (1, self.active_id) if self.property_present else None

    def active_window(self) -> int | None:
        self.record("active_window")
        return self.active_id

    def fileno(self) -> int:
        return self.read_fd

    def pending_events(self) -> int:
        return len(self.events)

    def next_event(self) -> object:
        os.read(self.read_fd, 1)
        return self.events.pop(0)

    def publish(self, atom: int, active_id: int | None = None) -> None:
        from Xlib import X

        if atom == 1:
            self.active_id = active_id
        self.events.append(SimpleNamespace(type=X.PropertyNotify, atom=atom))
        os.write(self.write_fd, b"x")

    def close(self) -> None:
        try:
            super().close()
        finally:
            os.close(self.read_fd)
            os.close(self.write_fd)


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


@pytest.mark.parametrize("next_window", [43, 0, None])
def test_active_window_change_closes_socket_then_cancels(rig: Rig, next_window: int | None) -> None:
    received: list[tuple[str, str]] = []
    notified = threading.Event()
    callback_saw_closed: list[bool] = []

    def factory() -> PropertyDisplay:
        display = PropertyDisplay(rig, property_present=True)
        rig.displays.append(display)
        return display

    def on_key(action: str, value: str) -> None:
        received.append((action, value))
        callback_saw_closed.append(rig.closed.is_set() and not rig.watchdog.active)
        notified.set()

    rig.watchdog = CaptureFieldWatchdog(display_factory=factory, poll_ms=1000, on_key_event=on_key)
    assert rig.watchdog.open()
    display = rig.displays[0]
    assert isinstance(display, PropertyDisplay)
    display.publish(2)
    display.publish(1, 42)
    assert not notified.wait(0.03)
    assert rig.watchdog.active
    assert not display.transport.closed

    start = time.monotonic()
    display.publish(1, next_window)
    assert notified.wait(WAIT_S)
    assert time.monotonic() - start < 0.1
    rig.watchdog.close()
    rig.assert_stopped()
    assert received == [("cancel", "")]
    assert callback_saw_closed == [True]
    assert display.transport.closed
    assert not rig.expired.is_set()


@pytest.mark.parametrize("error", [AttributeError, ValueError, OSError])
def test_active_window_read_failure_cancels_capture(rig: Rig, error: type[Exception]) -> None:
    received: list[tuple[str, str]] = []
    notified = threading.Event()
    callback_saw_closed: list[bool] = []

    class BrokenReadDisplay(PropertyDisplay):
        def active_window(self) -> int | None:
            raise error("private X details")

    def factory() -> BrokenReadDisplay:
        display = BrokenReadDisplay(rig, property_present=True)
        rig.displays.append(display)
        return display

    def on_key(action: str, value: str) -> None:
        received.append((action, value))
        callback_saw_closed.append(rig.closed.is_set() and not rig.watchdog.active)
        notified.set()

    rig.watchdog = CaptureFieldWatchdog(display_factory=factory, poll_ms=1000, on_key_event=on_key)
    assert rig.watchdog.open()
    display = rig.displays[0]
    assert isinstance(display, BrokenReadDisplay)
    display.publish(1, 42)
    assert notified.wait(WAIT_S)
    rig.watchdog.close()
    assert received == [("cancel", "")]
    assert callback_saw_closed == [True]
    assert display.transport.closed
    assert not rig.expired.is_set()


def test_missing_active_window_property_only_expires(rig: Rig) -> None:
    received: list[tuple[str, str]] = []

    def factory() -> PropertyDisplay:
        display = PropertyDisplay(rig, property_present=False)
        rig.displays.append(display)
        return display

    rig.watchdog = CaptureFieldWatchdog(
        display_factory=factory,
        poll_ms=1,
        on_expired=rig.on_expired,
        on_key_event=lambda action, value: received.append((action, value)),
    )
    assert rig.watchdog.open()
    display = rig.displays[0]
    assert isinstance(display, PropertyDisplay)
    display.publish(1, 43)
    assert not rig.closed.wait(0.03)
    assert rig.watchdog.active
    rig.elapsed.set()
    assert rig.expired.wait(WAIT_S)
    rig.watchdog.close()
    assert received == []
    assert display.transport.closed


def test_active_window_subscription_failure_keeps_capture(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    class BrokenWatchDisplay(FakeDisplay):
        def watch_active_window(self) -> tuple[int, int | None] | None:
            self.record("watch")
            raise RuntimeError("private X details")

    def factory() -> BrokenWatchDisplay:
        display = BrokenWatchDisplay(rig)
        rig.displays.append(display)
        return display

    rig.watchdog = CaptureFieldWatchdog(
        display_factory=factory, poll_ms=1, on_expired=rig.on_expired
    )
    with caplog.at_level(logging.WARNING, logger=module.__name__):
        assert rig.watchdog.open()
        assert rig.watchdog.active
        assert rig.displays[0].grab_args == (None, 30.0)
        assert not rig.closed.is_set()
        rig.elapsed.set()
        assert rig.expired.wait(WAIT_S)
    warnings = [record.message for record in caplog.records if record.levelno == logging.WARNING]
    assert warnings == ["Не удалось подписаться на смену активного окна: захват снимется по сроку"]


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


@pytest.mark.parametrize(
    ("keysym", "state", "expected"),
    [
        ("Control_L", 0, []),
        ("k", 4, [("combo", "Ctrl+K")]),
        ("Escape", 0, [("cancel", "")]),
        (
            "Escape",
            4,
            [("hint", "Эта клавиша не поддерживается. Выберите букву, цифру, пробел или F1–F12")],
        ),
        (
            "Return",
            4,
            [("hint", "Эта клавиша не поддерживается. Выберите букву, цифру, пробел или F1–F12")],
        ),
        (
            "Tab",
            4,
            [("hint", "Эта клавиша не поддерживается. Выберите букву, цифру, пробел или F1–F12")],
        ),
        (
            "KP_1",
            4,
            [("hint", "Эта клавиша не поддерживается. Выберите букву, цифру, пробел или F1–F12")],
        ),
        ("a", 0, [("hint", "Добавьте к клавише Ctrl, Alt или Win")]),
    ],
)
def test_watchdog_reads_grabbed_keypress(
    keysym: str, state: int, expected: list[tuple[str, str]]
) -> None:
    from Xlib import XK, X

    class EventDisplay(X11Display):
        def __init__(self) -> None:
            super().__init__()
            self.events = [SimpleNamespace(type=X.KeyPress, detail=38, state=state)]
            self.d = SimpleNamespace(
                pending_events=lambda: len(self.events),
                keycode_to_keysym=lambda _code, index: (
                    XK.string_to_keysym(keysym) if index == 0 else 0
                ),
            )

        def pending_events(self) -> int:
            return len(self.events)

        def next_event(self) -> object:
            return self.events.pop(0)

    received: list[tuple[str, str]] = []
    watchdog = CaptureFieldWatchdog(
        on_key_event=lambda action, value: received.append((action, value))
    )
    watchdog._poll_keys(EventDisplay(), 0)
    assert received == expected


def test_watchdog_refreshes_mapping_and_orders_modifiers() -> None:
    from Xlib import XK, X

    class EventDisplay(X11Display):
        def __init__(self) -> None:
            super().__init__()
            self.events = [
                SimpleNamespace(type=X.MappingNotify, request=X.MappingKeyboard),
                SimpleNamespace(
                    type=X.KeyPress,
                    detail=38,
                    state=X.Mod4Mask | X.Mod1Mask | X.ShiftMask | X.ControlMask | X.LockMask,
                ),
            ]
            self.d = SimpleNamespace(
                pending_events=lambda: len(self.events),
                keycode_to_keysym=lambda _code, _index: XK.XK_F12,
            )
            self.refreshed = False

        def pending_events(self) -> int:
            return len(self.events)

        def next_event(self) -> object:
            return self.events.pop(0)

        def refresh_keyboard_mapping(self, event: object) -> bool:
            self.refreshed = True
            return True

    received: list[tuple[str, str]] = []
    display = EventDisplay()
    watchdog = CaptureFieldWatchdog(
        on_key_event=lambda action, value: received.append((action, value))
    )
    watchdog._poll_keys(display, 0)
    assert display.refreshed
    assert received == [("combo", "Ctrl+Shift+Alt+Super+F12")]


def test_close_wakes_real_select_without_x_event() -> None:
    reading, writing = os.pipe()
    selected = threading.Event()
    transport = SimpleNamespace(shutdown=lambda _how: None, close=lambda: os.close(reading))

    class SilentDisplay(X11Display):
        def open(self, display_name: str | None = None) -> bool:
            self.d = SimpleNamespace(
                display=SimpleNamespace(socket=transport),
                pending_events=lambda: 0,
            )
            return True

        def grab_keyboard(self, window_id: int | None = None, timeout_s: float = 30.0) -> bool:
            return True

        def fileno(self) -> int:
            selected.set()
            return reading

        def pending_events(self) -> int:
            return 0

        def close(self) -> None:
            self.d = None

    watchdog = CaptureFieldWatchdog(display_factory=SilentDisplay, poll_ms=1000)
    try:
        assert watchdog.open()
        assert selected.wait(WAIT_S)
        start = time.monotonic()
        watchdog.close()
        elapsed = time.monotonic() - start
        assert elapsed < 0.1
        assert watchdog._thread is not None and not watchdog._thread.is_alive()
        with pytest.raises(OSError):
            os.fstat(watchdog._wake_read)
        with pytest.raises(OSError):
            os.fstat(watchdog._wake_write)
    finally:
        watchdog.close()
        os.close(writing)


def test_watchdog_thread_reads_key_after_real_select() -> None:
    from Xlib import XK, X

    reading, writing = os.pipe()
    received: list[tuple[str, str]] = []
    delivered = threading.Event()
    transport = SimpleNamespace(shutdown=lambda _how: None, close=lambda: os.close(reading))

    def on_key(action: str, value: str) -> None:
        received.append((action, value))
        delivered.set()

    class EventDisplay(X11Display):
        def __init__(self) -> None:
            super().__init__()
            self.events: list[object] = []

        def open(self, display_name: str | None = None) -> bool:
            self.d = SimpleNamespace(
                display=SimpleNamespace(socket=transport),
                pending_events=lambda: len(self.events),
                keycode_to_keysym=lambda _code, index: XK.XK_a if index == 0 else 0,
            )
            return True

        def grab_keyboard(self, window_id: int | None = None, timeout_s: float = 30.0) -> bool:
            return True

        def fileno(self) -> int:
            return reading

        def pending_events(self) -> int:
            return len(self.events)

        def next_event(self) -> object:
            os.read(reading, 1)
            return self.events.pop(0)

        def close(self) -> None:
            self.d = None

    display = EventDisplay()
    watchdog = CaptureFieldWatchdog(
        display_factory=lambda: display,
        on_key_event=on_key,
        poll_ms=1000,
    )
    try:
        assert watchdog.open()
        display.events.append(SimpleNamespace(type=X.KeyPress, detail=38, state=X.ControlMask))
        os.write(writing, b"x")
        assert delivered.wait(WAIT_S)
        assert received == [("combo", "Ctrl+A")]
        watchdog.close()
        assert watchdog._thread is not None and not watchdog._thread.is_alive()
    finally:
        watchdog.close()
        os.close(writing)
