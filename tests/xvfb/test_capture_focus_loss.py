"""Сторож отпускает XGrabKeyboard при смене активного окна EWMH."""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from typing import Any

import pytest

from astra_voice.core.capture_watchdog import CaptureFieldWatchdog
from astra_voice.platform.x11 import X11Display

pytestmark = pytest.mark.xvfb

if os.environ.get("DISPLAY", "").strip() in {"", ":0", ":0.0"}:
    pytest.skip(
        "тест требует изолированного X: на :0 живая сессия заказчика", allow_module_level=True
    )

pytest.importorskip("Xlib")
from Xlib import X, Xatom  # noqa: E402
from Xlib import display as xdisplay  # noqa: E402


@pytest.fixture
def active_windows() -> Iterator[tuple[Any, Any, Any, Any, int]]:
    conn = xdisplay.Display()
    root = conn.screen().root
    atom = conn.intern_atom("_NET_ACTIVE_WINDOW")
    previous = root.get_full_property(atom, X.AnyPropertyType)
    first = root.create_window(0, 0, 80, 80, 0, X.CopyFromParent)
    second = root.create_window(100, 0, 80, 80, 0, X.CopyFromParent)
    try:
        first.map()
        second.map()
        root.change_property(atom, Xatom.WINDOW, 32, [first.id])
        conn.sync()
        yield conn, root, first, second, atom
    finally:
        if previous is None:
            root.delete_property(atom)
        else:
            root.change_property(atom, previous.property_type, previous.format, previous.value)
        first.destroy()
        second.destroy()
        conn.sync()
        conn.close()


def _foreign_grab(root: Any, conn: Any) -> int:
    status = root.grab_keyboard(False, X.GrabModeAsync, X.GrabModeAsync, X.CurrentTime)
    if status == X.GrabSuccess:
        conn.ungrab_keyboard(X.CurrentTime)
    conn.sync()
    return int(status)


def test_active_window_change_releases_grab_within_200_ms(
    active_windows: tuple[Any, Any, Any, Any, int],
) -> None:
    conn, root, first, second, atom = active_windows
    received: list[tuple[str, str]] = []
    watchdog = CaptureFieldWatchdog(
        display_factory=X11Display,
        on_key_event=lambda action, value: received.append((action, value)),
    )
    try:
        assert watchdog.open()
        assert _foreign_grab(root, conn) != X.GrabSuccess
        root.change_property(atom, Xatom.WINDOW, 32, [first.id])
        conn.sync()
        time.sleep(0.03)
        assert _foreign_grab(root, conn) != X.GrabSuccess
        assert watchdog.active

        started = time.monotonic()
        root.change_property(atom, Xatom.WINDOW, 32, [second.id])
        conn.sync()
        deadline = started + 0.2
        released = False
        while time.monotonic() < deadline:
            released = _foreign_grab(root, conn) == X.GrabSuccess
            if released and received:
                break
            time.sleep(0.005)
        assert released
        assert not watchdog.active
        assert time.monotonic() - started < 0.2
        assert received == [("cancel", "")]
    finally:
        watchdog.close()
