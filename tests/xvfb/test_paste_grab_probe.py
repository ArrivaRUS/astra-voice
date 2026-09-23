"""Проба захвата перед XTest не шевелит фокус мишени (фикс 22.09, P0 «текст в буфере»).

Правило Xlib: захват клавиатуры на **самом окне фокуса** не порождает FocusIn/FocusOut —
сервер генерирует события только для перехода фокуса F→G, а при F == G перехода нет.
На этом держится вставка: приложение, в которое вставляем (Kate, Electron, fly-wm),
на FocusOut теряет выделение, закрывает всплывашки и перехватывает Ctrl+V себе.

Контрольный кейс — проба на другом окне: события обязаны появиться. Без него тест
не отличил бы «правило соблюдается» от «стенд вообще не видит Focus-событий».
"""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from astra_voice.platform.paste import _keyboard_busy
from astra_voice.platform.x11 import X11Display

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from helpers import xdisplay as xdisplay  # noqa: E402

pytestmark = pytest.mark.xvfb

if os.environ.get("DISPLAY", "").strip() in {"", ":0", ":0.0"}:
    pytest.skip(
        "тест требует изолированного X: на :0 живая сессия заказчика", allow_module_level=True
    )
pytest.importorskip("Xlib")

#: Окно наблюдения за очередью мишени после пробы: события захвата приходят сразу,
#: но 300 мс покрывают и отложенный перезахват оконного менеджера.
WATCH_MS = 300
#: Тишина в очереди дольше этого срока означает, что стенд успокоился.
SETTLE_MS = 50
POLL_MS = 5


#: Режимы Focus-событий по протоколу X11 (X.NotifyNormal … X.NotifyWhileGrabbed).
_MODE_NAMES = {0: "NotifyNormal", 1: "NotifyGrab", 2: "NotifyUngrab", 3: "NotifyWhileGrabbed"}
#: Режимы, которые приложение (Qt xcb, GTK) не считает сменой фокуса.
_GRAB_MODES = {"NotifyGrab", "NotifyUngrab"}


def _window_id(event: Any) -> int:
    """Поле window события — ресурс Xlib либо целое, в зависимости от версии python-xlib."""
    window = getattr(event, "window", 0)
    return int(getattr(window, "id", window))


@dataclass
class FocusWatcher:
    """Отдельный X-клиент: владеет окнами и читает их Focus-события своей очередью."""

    conn: Any
    target: Any
    other: Any

    def target_id(self) -> int:
        return int(self.target.id)

    def other_id(self) -> int:
        return int(self.other.id)

    def _read(self) -> list[str]:
        """Забрать очередь до конца; вернуть Focus-события мишени как «Вид/Режим».

        Режим важнее факта события: при захвате сервер шлёт FocusOut/FocusIn с
        NotifyGrab/NotifyUngrab даже на самом окне фокуса (CI 23.09), а тулкиты
        (Qt xcb, GTK) такие события не считают сменой фокуса.
        """
        names: list[str] = []
        self.conn.sync()
        while self.conn.pending_events():
            event = self.conn.next_event()
            if _window_id(event) != self.target_id():
                continue
            kind = type(event).__name__
            if kind in {"FocusIn", "FocusOut"}:
                names.append(f"{kind}/{_MODE_NAMES.get(int(getattr(event, 'mode', -1)), '?')}")
        return names

    def settle(self) -> None:
        """Опустошить очередь и убедиться, что новые события перестали приходить."""
        deadline = time.monotonic() + 2.0
        quiet_until = time.monotonic() + SETTLE_MS / 1000
        while time.monotonic() < quiet_until:
            if self._read():
                quiet_until = time.monotonic() + SETTLE_MS / 1000
            assert time.monotonic() < deadline, "очередь стенда не успокоилась за 2 с"
            time.sleep(POLL_MS / 1000)

    def collect(self, duration_ms: int) -> list[str]:
        """Имена Focus-событий мишени за окно наблюдения."""
        names: list[str] = []
        deadline = time.monotonic() + duration_ms / 1000
        while True:
            names.extend(self._read())
            if time.monotonic() >= deadline:
                return names
            time.sleep(POLL_MS / 1000)

    def focus_id(self) -> int:
        focus = self.conn.get_input_focus().focus
        return int(getattr(focus, "id", focus))

    def foreign_grab_status(self) -> str:
        """Захват клавиатуры чужим клиентом: имя статуса сервера; успешный сразу снимается.

        Захват здесь — на окне фокуса, поэтому сама проверка Focus-событий не порождает.
        """
        from Xlib import X

        status = int(
            self.target.grab_keyboard(False, X.GrabModeAsync, X.GrabModeAsync, X.CurrentTime)
        )
        if status == X.GrabSuccess:
            self.conn.ungrab_keyboard(X.CurrentTime)
            self.conn.sync()
            return "GrabSuccess"
        return {
            X.AlreadyGrabbed: "AlreadyGrabbed",
            X.GrabInvalidTime: "GrabInvalidTime",
            X.GrabNotViewable: "GrabNotViewable",
            X.GrabFrozen: "GrabFrozen",
        }.get(status, f"status={status}")


@pytest.fixture
def watcher() -> Iterator[FocusWatcher]:
    """Два голых окна с маской FocusChange; фокус ввода — на мишени."""
    from Xlib import X
    from Xlib import display as xlib_display

    conn = xlib_display.Display()
    windows: list[Any] = []
    try:
        root = conn.screen().root
        for left in (0, 200):
            windows.append(
                root.create_window(
                    left,
                    0,
                    120,
                    80,
                    0,
                    X.CopyFromParent,
                    event_mask=X.FocusChangeMask | X.StructureNotifyMask,
                )
            )
            windows[-1].map()
        conn.sync()
        deadline = time.monotonic() + 2.0
        while any(window.get_attributes().map_state != X.IsViewable for window in windows):
            assert time.monotonic() < deadline, "окна стенда не стали видимыми за 2 с"
            time.sleep(POLL_MS / 1000)
        target, other = windows
        target.set_input_focus(X.RevertToParent, X.CurrentTime)
        conn.sync()
        watcher = FocusWatcher(conn, target, other)
        assert watcher.focus_id() == watcher.target_id(), "фокус ввода не перешёл к мишени"
        yield watcher
    finally:
        try:
            for window in windows:
                window.destroy()
            conn.sync()
        finally:
            conn.close()


def test_probe_on_focus_window_is_silent(watcher: FocusWatcher, xdisplay: X11Display) -> None:
    """Проба на окне фокуса: только события захвата (NotifyGrab/NotifyUngrab), фокус на месте.

    Обычной смены фокуса (NotifyNormal/NotifyWhileGrabbed) приложение видеть не должно.
    """
    watcher.settle()
    assert _keyboard_busy(xdisplay, watcher.target_id()) == "", "проба обязана была захватить"
    assert xdisplay.keyboard_grab_deadline is None
    events = watcher.collect(WATCH_MS)
    unexpected = [name for name in events if name.split("/")[1] not in _GRAB_MODES]
    assert unexpected == [], f"смена фокуса вне захвата: {unexpected} (все события: {events})"
    assert watcher.focus_id() == watcher.target_id()
    assert watcher.foreign_grab_status() == "GrabSuccess"


def test_probe_on_other_window_moves_focus(watcher: FocusWatcher, xdisplay: X11Display) -> None:
    """Контроль: проба на чужом окне — мишень получает и FocusOut, и FocusIn."""
    watcher.settle()
    assert _keyboard_busy(xdisplay, watcher.other_id()) == "", "проба обязана была захватить"
    assert xdisplay.keyboard_grab_deadline is None
    events = watcher.collect(WATCH_MS)
    kinds = {name.split("/")[0] for name in events}
    assert {"FocusOut", "FocusIn"} <= kinds, f"стенд не увидел смены фокуса: {events}"
    assert watcher.focus_id() == watcher.target_id()
    assert watcher.foreign_grab_status() == "GrabSuccess"
