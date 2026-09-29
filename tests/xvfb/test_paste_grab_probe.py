"""Проба захвата перед XTest не снимает активацию с окна фокуса (P0 22.09, Enter 29.09).

Проба «клавиатура занята чужим захватом?» идёт на своём override-redirect окне
(``X11Display.probe_window``). Правило X11 для захвата на окне G при фокусе F:
при захвате F получает FocusOut/NotifyGrab, при отпускании — FocusIn/NotifyUngrab.
Оба режима KWin 5.27 пропускает (``focusOutEvent`` отбрасывает Grab,
``focusInEvent`` — Ungrab). Прежние места захвата отвергнуты:

* корень — корень получает FocusIn, fly-wm отвечает перезахватом (22.09);
* само окно фокуса (F == G) — сервер всё равно шлёт пары событий, и отпускание
  даёт F **FocusOut/NotifyUngrab**: KWin принимает его за потерю фокуса,
  ``_NET_ACTIVE_WINDOW`` обнуляется при живом X-фокусе, утилиты неактивного
  приложения прячутся (Enter в форме быстрого ввода Cowork, 29.09).

Контрольный кейс — старая проба на окне фокуса: FocusOut/NotifyUngrab обязан
появиться, иначе тест не отличил бы «правило соблюдается» от «стенд не видит событий».
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
#: Детали Focus-событий (X.NotifyAncestor … X.NotifyDetailNone).
_DETAIL_NAMES = {
    0: "NotifyAncestor",
    1: "NotifyVirtual",
    2: "NotifyInferior",
    3: "NotifyNonlinear",
    4: "NotifyNonlinearVirtual",
    5: "NotifyPointer",
    6: "NotifyPointerRoot",
    7: "NotifyDetailNone",
}
#: Единственные Focus-события окна фокуса, допустимые за пробу: KWin 5.27 их
#: пропускает, а Qt xcb останавливает таймер деактивации на FocusIn.
_ALLOWED = {"FocusOut/NotifyGrab", "FocusIn/NotifyUngrab"}


def _window_id(event: Any) -> int:
    """Поле window события — ресурс Xlib либо целое, в зависимости от версии python-xlib."""
    window = getattr(event, "window", 0)
    return int(getattr(window, "id", window))


@dataclass(frozen=True)
class FocusRecord:
    """Focus-событие любого окна стенда: окно, «Вид/Режим» и деталь."""

    window: int
    name: str
    detail: str


def _names(records: list[FocusRecord], window: int) -> list[str]:
    """«Вид/Режим» событий одного окна в порядке прихода."""
    return [record.name for record in records if record.window == window]


@dataclass
class FocusWatcher:
    """Отдельный X-клиент: владеет окнами и читает Focus-события своей очередью.

    Подписан и на корень: проба не должна давать корню ни одного Focus-события
    (FocusIn корню — повод для перезахвата fly-wm, фикс 22.09).
    """

    conn: Any
    target: Any
    other: Any

    def target_id(self) -> int:
        return int(self.target.id)

    def other_id(self) -> int:
        return int(self.other.id)

    def root_id(self) -> int:
        return int(self.conn.screen().root.id)

    def _read(self) -> list[FocusRecord]:
        """Забрать очередь до конца; вернуть Focus-события всех окон стенда.

        Режим важнее факта события: при захвате сервер шлёт FocusOut/FocusIn с
        NotifyGrab/NotifyUngrab даже на самом окне фокуса (CI 23.09).
        """
        records: list[FocusRecord] = []
        self.conn.sync()
        while self.conn.pending_events():
            event = self.conn.next_event()
            kind = type(event).__name__
            if kind not in {"FocusIn", "FocusOut"}:
                continue
            mode = _MODE_NAMES.get(int(getattr(event, "mode", -1)), "?")
            detail = _DETAIL_NAMES.get(int(getattr(event, "detail", -1)), "?")
            records.append(FocusRecord(_window_id(event), f"{kind}/{mode}", detail))
        return records

    def settle(self) -> None:
        """Опустошить очередь и убедиться, что новые события перестали приходить."""
        deadline = time.monotonic() + 2.0
        quiet_until = time.monotonic() + SETTLE_MS / 1000
        while time.monotonic() < quiet_until:
            if self._read():
                quiet_until = time.monotonic() + SETTLE_MS / 1000
            assert time.monotonic() < deadline, "очередь стенда не успокоилась за 2 с"
            time.sleep(POLL_MS / 1000)

    def collect_all(self, duration_ms: int) -> list[FocusRecord]:
        """Focus-события всех окон стенда за окно наблюдения."""
        records: list[FocusRecord] = []
        deadline = time.monotonic() + duration_ms / 1000
        while True:
            records.extend(self._read())
            if time.monotonic() >= deadline:
                return records
            time.sleep(POLL_MS / 1000)

    def collect(self, duration_ms: int) -> list[str]:
        """Имена Focus-событий мишени за окно наблюдения."""
        return _names(self.collect_all(duration_ms), self.target_id())

    def focus_id(self) -> int:
        focus = self.conn.get_input_focus().focus
        return int(getattr(focus, "id", focus))

    def foreign_grab_status(self) -> str:
        """Захват клавиатуры чужим клиентом: имя статуса сервера; успешный сразу снимается.

        Зовётся после сбора событий: сама проверка тоже порождает Focus-события мишени.
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
        # Корень — тоже под наблюдением: у него за пробу событий быть не должно.
        root.change_attributes(event_mask=X.FocusChangeMask)
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


def _probe(xdisplay: X11Display) -> str:
    """Проба так же, как в цепочке вставки: окно создаётся заранее, затем захват."""
    return _keyboard_busy(xdisplay, xdisplay.probe_window())


@dataclass
class FakeWm:
    """Клиент с SubstructureRedirect на корне — так корень слушает оконный менеджер.

    Окно без override-redirect при map() дало бы ему MapRequest; окно пробы не должно.
    """

    conn: Any

    def events(self) -> list[tuple[str, int, Any]]:
        """Все события очереди как (вид, окно, флаг override-redirect)."""
        out: list[tuple[str, int, Any]] = []
        self.conn.sync()
        while self.conn.pending_events():
            event = self.conn.next_event()
            out.append((type(event).__name__, _window_id(event), getattr(event, "override", None)))
        return out


@pytest.fixture
def fake_wm(watcher: FocusWatcher) -> Iterator[FakeWm]:
    """После отображения окон стенда: иначе их map() тоже ушёл бы в MapRequest."""
    from Xlib import X, error
    from Xlib import display as xlib_display

    conn = xlib_display.Display()
    try:
        catcher = error.CatchError(error.BadAccess)
        conn.screen().root.change_attributes(
            event_mask=X.SubstructureRedirectMask | X.SubstructureNotifyMask, onerror=catcher
        )
        conn.sync()
        assert catcher.get_error() is None, "на стенде уже есть оконный менеджер"
        yield FakeWm(conn)
    finally:
        conn.close()


def test_probe_leaves_focus_window_active(watcher: FocusWatcher, xdisplay: X11Display) -> None:
    """За пробу окно фокуса не получает FocusOut с NotifyUngrab/NotifyNormal, фокус на месте."""
    watcher.settle()
    assert _probe(xdisplay) == "", "проба обязана была захватить"
    assert xdisplay.keyboard_grab_deadline is None
    records = watcher.collect_all(WATCH_MS)
    root_events = _names(records, watcher.root_id())
    assert root_events == [], f"корень получил Focus-события (fly-wm перезахватит): {root_events}"
    events = _names(records, watcher.target_id())
    unexpected = [name for name in events if name not in _ALLOWED]
    assert unexpected == [], f"KWin снял бы активацию: {unexpected} (все события: {events})"
    # Стенд видит события захвата, а последним окну фокуса пришёл FocusIn.
    assert "FocusOut/NotifyGrab" in events, f"стенд не увидел захвата: {events}"
    assert events[-1] == "FocusIn/NotifyUngrab", f"фокус не вернулся: {events}"
    assert watcher.focus_id() == watcher.target_id()
    assert watcher.foreign_grab_status() == "GrabSuccess"


def test_grab_on_focus_window_sends_ungrab_focus_out(
    watcher: FocusWatcher, xdisplay: X11Display
) -> None:
    """Контроль: прежняя проба на самом окне фокуса даёт ему FocusOut/NotifyUngrab.

    Именно это событие KWin 5.27 (``events.cpp``, ``focusOutEvent``) считает
    потерей фокуса. Без контроля первый тест прошёл бы и на глухом стенде.
    """
    watcher.settle()
    assert xdisplay.grab_keyboard(watcher.target_id()), "захват на окне фокуса не удался"
    xdisplay.ungrab_keyboard()
    assert xdisplay.keyboard_grab_deadline is None
    events = watcher.collect(WATCH_MS)
    assert "FocusOut/NotifyUngrab" in events, f"стенд не воспроизвёл триггер KWin: {events}"
    assert watcher.focus_id() == watcher.target_id()


def test_foreign_grab_refuses_probe(watcher: FocusWatcher, xdisplay: X11Display) -> None:
    """Чужой активный захват (меню, блокировщик, зажатый хоткей) → grab-refused."""
    from Xlib import X

    status = watcher.other.grab_keyboard(False, X.GrabModeAsync, X.GrabModeAsync, X.CurrentTime)
    assert status == X.GrabSuccess, "стенд не смог захватить клавиатуру чужим клиентом"
    try:
        watcher.conn.sync()
        assert _probe(xdisplay) == "grab-refused"
        assert xdisplay.keyboard_grab_deadline is None
    finally:
        watcher.conn.ungrab_keyboard(X.CurrentTime)
        watcher.conn.sync()
    # Чужой захват снят — проба снова проходит, на том же окне.
    assert _probe(xdisplay) == ""
    assert xdisplay.keyboard_grab_deadline is None


def test_probe_window_is_invisible_to_window_manager(
    watcher: FocusWatcher, fake_wm: FakeWm, xdisplay: X11Display
) -> None:
    """Окно пробы: override-redirect InputOnly вне экрана, WM не получает MapRequest.

    В CI оконного менеджера нет, поэтому ``_NET_CLIENT_LIST`` обычно отсутствует;
    главное доказательство — отсутствие MapRequest у держателя SubstructureRedirect:
    без него WM окно не ведёт и в список клиентов не вносит.
    """
    from Xlib import X, Xatom

    fake_wm.events()
    assert _probe(xdisplay) == ""
    probe = xdisplay.probe_window()
    assert probe is not None
    assert _probe(xdisplay) == ""
    assert xdisplay.probe_window() == probe, "окно пробы должно жить с соединением"
    events = fake_wm.events()
    assert [e for e in events if e[0] == "MapRequest"] == [], f"WM увидел окно: {events}"
    created = [e for e in events if e[0] == "CreateNotify" and e[1] == probe]
    assert len(created) == 1, f"окно пробы создано не один раз: {events}"
    assert all(bool(e[2]) for e in events if e[1] == probe), events

    window = watcher.conn.create_resource_object("window", probe)
    attrs = window.get_attributes()
    assert attrs.override_redirect
    assert attrs.win_class == X.InputOnly
    assert attrs.map_state == X.IsViewable
    geometry = window.get_geometry()
    assert geometry.x + geometry.width <= 0 and geometry.y + geometry.height <= 0

    root = watcher.conn.screen().root
    clients = root.get_full_property(watcher.conn.intern_atom("_NET_CLIENT_LIST"), Xatom.WINDOW)
    assert clients is None or probe not in [int(w) for w in clients.value]
    # Фокус ввода не ушёл ни к окну пробы, ни к корню.
    assert watcher.focus_id() == watcher.target_id()


def test_probe_with_nested_focus_leaves_toplevel_active(
    watcher: FocusWatcher, xdisplay: X11Display
) -> None:
    """Фокус у дочернего виджета: верхнее окно получает только NonlinearVirtual Grab/Ungrab.

    KWin слушает окно клиента (верхнее), приложение — окно с фокусом; FocusOut с
    NotifyUngrab не должен прийти ни одному из них, ни корню — ничего.
    """
    from Xlib import X

    root = watcher.conn.screen().root
    parent = root.create_window(400, 0, 120, 80, 0, X.CopyFromParent, event_mask=X.FocusChangeMask)
    try:
        child = parent.create_window(
            10, 10, 40, 30, 0, X.CopyFromParent, event_mask=X.FocusChangeMask
        )
        parent.map_sub_windows()
        parent.map()
        watcher.conn.sync()
        deadline = time.monotonic() + 2.0
        while child.get_attributes().map_state != X.IsViewable:
            assert time.monotonic() < deadline, "вложенное окно не стало видимым за 2 с"
            time.sleep(POLL_MS / 1000)
        child.set_input_focus(X.RevertToParent, X.CurrentTime)
        watcher.conn.sync()
        assert watcher.focus_id() == int(child.id), "фокус не перешёл к вложенному окну"
        watcher.settle()

        assert _probe(xdisplay) == "", "проба обязана была захватить"
        assert xdisplay.keyboard_grab_deadline is None
        records = watcher.collect_all(WATCH_MS)

        root_events = _names(records, watcher.root_id())
        assert root_events == [], f"корень получил Focus-события: {root_events}"
        ungrab_out = [r for r in records if r.name == "FocusOut/NotifyUngrab"]
        assert ungrab_out == [], f"FocusOut/NotifyUngrab снял бы активацию: {records}"

        child_events = _names(records, int(child.id))
        assert [n for n in child_events if n not in _ALLOWED] == [], records
        assert child_events[-1:] == ["FocusIn/NotifyUngrab"], f"фокус не вернулся: {records}"

        parent_records = [r for r in records if r.window == int(parent.id)]
        assert parent_records, f"стенд не увидел событий верхнего окна: {records}"
        assert {r.name for r in parent_records} <= _ALLOWED, records
        assert {r.detail for r in parent_records} == {"NotifyNonlinearVirtual"}, records

        assert watcher.focus_id() == int(child.id)
    finally:
        parent.destroy()
        watcher.target.set_input_focus(X.RevertToParent, X.CurrentTime)
        watcher.conn.sync()
