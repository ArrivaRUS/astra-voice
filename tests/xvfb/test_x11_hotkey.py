"""Захваты разных клиентов и доставка XTest на настоящем изолированном X11."""

from __future__ import annotations

import gc
import os
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from astra_voice.platform.hotkey import HotkeyManager, HotkeyMode, HotkeyState, X11HotkeyBackend
from astra_voice.platform.paste import PasteFlow, PasteMode, PasteOutcomeKind, PasteRestore
from astra_voice.platform.x11 import USER_TIME_ATOM, X11Display, set_user_time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from helpers import clipboard_owner, paste_target, wait_until  # noqa: E402
from helpers import xapp as xapp  # noqa: E402
from helpers import xdisplay as xdisplay  # noqa: E402

pytestmark = pytest.mark.xvfb

if os.environ.get("DISPLAY", "").strip() in {"", ":0", ":0.0"}:
    pytest.skip(
        "тест требует изолированного X: на :0 живая сессия заказчика", allow_module_level=True
    )
pytest.importorskip("Xlib")
pytest.importorskip("PyQt5.QtWidgets")

COMBO = "Ctrl+Shift+F11"


def test_user_time_zero_before_map_and_negative_rejected(xdisplay: X11Display) -> None:
    """Ноль существует до map; отрицательная метка не портит ноль и положительную."""
    from Xlib import X, Xatom

    window = xdisplay.root.create_window(0, 0, 100, 100, 0, X.CopyFromParent)
    try:
        xdisplay.d.sync()
        assert window.get_attributes().map_state == X.IsUnmapped
        for timestamp in (0, 123456):
            assert set_user_time(int(window.id), timestamp)
            atom = xdisplay.d.intern_atom(USER_TIME_ATOM, only_if_exists=True)
            assert atom
            for reject_negative in (False, True):
                if reject_negative:
                    assert not set_user_time(int(window.id), -1)
                prop = window.get_full_property(atom, Xatom.CARDINAL)
                assert prop is not None and prop.format == 32
                assert list(prop.value) == [timestamp]
    finally:
        window.destroy()
        xdisplay.d.sync()


def test_net_workarea_matches_root(xdisplay: X11Display) -> None:
    """Отсутствие WM допустимо; существующая область обязана помещаться на экране."""
    from Xlib import Xatom

    atom = xdisplay.d.intern_atom("_NET_WORKAREA", only_if_exists=True)
    prop = xdisplay.root.get_full_property(atom, Xatom.CARDINAL) if atom else None
    area = xdisplay.net_workarea()
    if prop is None:
        assert area is None
        return
    assert prop.format == 32 and len(prop.value) >= 4
    assert isinstance(area, tuple) and len(area) == 4
    assert all(type(value) is int for value in area)
    x, y, width, height = area
    geometry = xdisplay.root.get_geometry()
    assert 0 < width <= geometry.width and 0 < height <= geometry.height
    assert 0 <= x <= geometry.width - width
    assert 0 <= y <= geometry.height - height


@pytest.mark.parametrize(
    "values, desktop, expected",
    [
        (None, None, None),
        ([], None, None),
        ([0, 0, 800], None, None),
        ([0, 0, 0, 600], None, None),
        ([0, 0, 800, 0], None, None),
        ([10, 20, 800, 600], None, (10, 20, 800, 600)),
        ([10, 20, 800, 600, 30, 40, 640, 480], 0, (10, 20, 800, 600)),
        ([10, 20, 800, 600, 30, 40, 640, 480], 1, (30, 40, 640, 480)),
        ([10, 20, 800, 600, 30, 40, 640, 480], 2, (10, 20, 800, 600)),
        ([10, 20, 800, 600, 30, 40, 640], 1, (10, 20, 800, 600)),
    ],
)
def test_net_workarea_desktop_and_fallback(
    xdisplay: X11Display,
    values: list[int] | None,
    desktop: int | None,
    expected: tuple[int, int, int, int] | None,
) -> None:
    """Проверяет разбор даже без WM, восстанавливая свойства изолированного корня."""
    from Xlib import X, Xatom

    atoms = [xdisplay.d.intern_atom(name) for name in ("_NET_WORKAREA", "_NET_CURRENT_DESKTOP")]
    previous = [xdisplay.root.get_full_property(atom, X.AnyPropertyType) for atom in atoms]
    try:
        for atom, value in zip(
            atoms, (values, None if desktop is None else [desktop]), strict=True
        ):
            if value is None:
                xdisplay.root.delete_property(atom)
            else:
                xdisplay.root.change_property(atom, Xatom.CARDINAL, 32, value)
        xdisplay.d.sync()
        assert xdisplay.net_workarea() == expected
    finally:
        for atom, prop in zip(atoms, previous, strict=True):
            if prop is None:
                xdisplay.root.delete_property(atom)
            else:
                xdisplay.root.change_property(atom, prop.property_type, prop.format, prop.value)
        xdisplay.d.sync()


def test_cardinal_and_fly_animations_off(xdisplay: X11Display) -> None:
    """Свойства читаются тем же соединением; Fly сбрасывает оба ненулевых значения."""
    from Xlib import X, Xatom

    window = xdisplay.root.create_window(0, 0, 100, 100, 0, X.CopyFromParent)
    names = ("_FLY_WM_WINDOW_MAP_ANIMATION", "_FLY_WM_FADE_SHOW")
    try:
        for value in (0, 42):
            assert xdisplay.set_cardinal(int(window.id), "_ASTRA_VOICE_TEST_CARDINAL", value)
            prop = window.get_full_property(
                xdisplay.d.intern_atom("_ASTRA_VOICE_TEST_CARDINAL"), Xatom.CARDINAL
            )
            assert prop is not None and prop.format == 32
            assert list(prop.value) == [value]
        for name in names:
            assert xdisplay.set_cardinal(int(window.id), name, 1)
        assert xdisplay.set_fly_animations_off(int(window.id))
        for name in names:
            atom = xdisplay.d.intern_atom(name, only_if_exists=True)
            assert atom
            prop = window.get_full_property(atom, Xatom.CARDINAL)
            assert prop is not None and prop.format == 32
            assert list(prop.value) == [0]
    finally:
        window.destroy()
        xdisplay.d.sync()
    assert not xdisplay.set_cardinal(int(window.id), names[0])
    assert not xdisplay.set_fly_animations_off(int(window.id))


@pytest.fixture
def backend() -> Iterator[X11HotkeyBackend]:
    """После каждого сценария проверить освобождение комбинации новым клиентом."""
    current = X11HotkeyBackend()
    try:
        yield current
    finally:
        current.ungrab_escape()
        current.ungrab_combo(COMBO)
        current.close()
        probe = X11HotkeyBackend()
        try:
            assert probe.grab_combo(COMBO).ok, "после теста остался захват комбинации"
            assert probe.grab_escape().ok, "после теста остался захват Escape"
        finally:
            probe.ungrab_escape()
            probe.ungrab_combo(COMBO)
            probe.close()


def test_masks_follow_server_modifier_mapping(xdisplay: X11Display) -> None:
    """Число вариантов выводится из реальной раскладки, включая отсутствие Scroll Lock."""
    from Xlib import XK, X

    lock_symbols = {
        XK.string_to_keysym(name) for name in ("Caps_Lock", "Shift_Lock", "Num_Lock", "Scroll_Lock")
    }
    bits = []
    for index, codes in enumerate(xdisplay.d.get_modifier_mapping()):
        symbols = {
            int(symbol)
            for code in codes
            if code
            for symbol in xdisplay.d.get_keyboard_mapping(int(code), 1)[0]
        }
        if symbols & lock_symbols:
            bits.append(1 << index)
    expected = {X.ControlMask}
    for bit in bits:
        expected |= {mask | bit for mask in expected}
    actual = xdisplay.mask_variants(X.ControlMask)
    assert len(actual) == len(expected)
    assert set(actual) == expected


def test_grab_ungrab_and_grab_again(backend: X11HotkeyBackend, xdisplay: X11Display) -> None:
    """Два захвата подряд в одном процессе, с проверкой освобождения другим клиентом."""
    parsed = xdisplay.parse_combo(COMBO)
    for _ in range(2):
        try:
            assert backend.grab_combo(COMBO).ok
            busy = xdisplay.grab_key(parsed.keycode, parsed.mods)
            assert not busy.ok and busy.bad_access
            assert backend.ungrab_combo(COMBO).ok
            assert xdisplay.grab_key(parsed.keycode, parsed.mods).ok
        finally:
            xdisplay.ungrab_key(parsed.keycode, parsed.mods)
            backend.ungrab_combo(COMBO)


def test_bad_access_rolls_back_and_recovers(
    backend: X11HotkeyBackend, xdisplay: X11Display
) -> None:
    """Одна занятая маска вызывает BadAccess и откат остальных масок."""
    from Xlib import X

    rival = X11Display()
    parsed = xdisplay.parse_combo(COMBO)
    try:
        assert rival.open()
        mask = xdisplay.mask_variants(parsed.mods)[-1]
        rival.root.grab_key(parsed.keycode, mask, True, X.GrabModeAsync, X.GrabModeAsync)
        rival.d.sync()
        report = xdisplay.grab_key(parsed.keycode, parsed.mods)
        assert not report.ok and report.bad_access
        assert (mask, "BadAccess") in report.per_mask
        assert backend.grab_combo(COMBO).code == "busy"
        # Все ранее свободные маски должны быть доступны после полного отката.
        assert rival.grab_key(parsed.keycode, parsed.mods).ok
        rival.ungrab_key(parsed.keycode, parsed.mods)
        for _ in range(2):
            assert backend.grab_combo(COMBO).ok
            assert backend.ungrab_combo(COMBO).ok
        assert xdisplay.grab_key(parsed.keycode, parsed.mods).ok
    finally:
        backend.ungrab_combo(COMBO)
        xdisplay.ungrab_key(parsed.keycode, parsed.mods)
        rival.ungrab_key(parsed.keycode, parsed.mods)
        rival.close()


def test_escape_is_temporary(backend: X11HotkeyBackend, xdisplay: X11Display) -> None:
    """Escape занят в записи и обработке, свободен после возврата в простой."""
    manager = HotkeyManager(backend)
    escape = xdisplay.parse_combo("Escape")
    try:
        assert manager.grab(COMBO, HotkeyMode.PTT).ok
        for _ in range(2):
            assert xdisplay.grab_key(escape.keycode, escape.mods).ok
            xdisplay.ungrab_key(escape.keycode, escape.mods)
            assert xdisplay.send_combo(["Control_L", "Shift_L"], "F11")

            def recording() -> bool:
                manager.process_pending()
                return manager.fsm.state == HotkeyState.RECORDING

            wait_until(recording)
            assert manager.escape_result.ok
            assert xdisplay.grab_key(escape.keycode, escape.mods).bad_access
            manager.fsm.stop(time.monotonic(), "проверка")
            assert manager.fsm.state.value == HotkeyState.PROCESSING.value
            assert xdisplay.grab_key(escape.keycode, escape.mods).bad_access
            manager.fsm.done(time.monotonic())
            assert manager.fsm.state.value == HotkeyState.IDLE.value
            assert xdisplay.grab_key(escape.keycode, escape.mods).ok
            xdisplay.ungrab_key(escape.keycode, escape.mods)
    finally:
        xdisplay.ungrab_key(escape.keycode, escape.mods)
        manager.ungrab()


def test_keyboard_grab_explicit_release_and_deadline(xdisplay: X11Display) -> None:
    """Явное снятие идемпотентно; дедлайн обслуживается обычным чтением EWMH."""
    rival = X11Display()
    try:
        assert rival.open()
        started = time.monotonic()
        assert xdisplay.grab_keyboard()
        assert xdisplay.keyboard_grab_deadline is not None
        assert started + 30 <= xdisplay.keyboard_grab_deadline <= time.monotonic() + 30
        assert not rival.grab_keyboard()
        xdisplay.ungrab_keyboard()
        xdisplay.ungrab_keyboard()
        assert xdisplay.keyboard_grab_deadline is None
        assert rival.grab_keyboard()
        rival.ungrab_keyboard()
        for _ in range(2):
            started = time.monotonic()
            assert xdisplay.grab_keyboard(timeout_s=0.02)
            assert xdisplay.keyboard_grab_deadline is not None
            assert started + 0.02 <= xdisplay.keyboard_grab_deadline <= time.monotonic() + 0.02
            wait_until(xdisplay.keyboard_grab_expired, timeout=1)
            assert not rival.grab_keyboard()
            xdisplay.active_window()
            assert not xdisplay.keyboard_grab_expired()
            assert rival.grab_keyboard()
            rival.ungrab_keyboard()
    finally:
        rival.ungrab_keyboard()
        rival.close()
        xdisplay.ungrab_keyboard()


@pytest.mark.parametrize("raises", [False, True])
def test_keyboard_context_releases_on_server(xdisplay: X11Display, raises: bool) -> None:
    """Другой клиент получает клавиатуру после выхода из контекста, включая исключение."""
    with X11Display() as rival:
        try:
            with xdisplay.keyboard_grab() as grabbed:
                assert grabbed
                assert not rival.grab_keyboard()
                if raises:
                    raise RuntimeError("закрытие поля")
        except RuntimeError:
            assert raises
        assert rival.grab_keyboard()


@pytest.mark.parametrize("cleanup", ["close", "finalize"])
def test_keyboard_connection_cleanup_releases_on_server(xdisplay: X11Display, cleanup: str) -> None:
    """Сервер освобождает захват при close и сборке забывшего закрыться владельца."""
    owner = X11Display()
    assert owner.open()
    try:
        assert owner.grab_keyboard()
        assert not xdisplay.grab_keyboard()
        if cleanup == "close":
            owner.close()
        else:
            del owner
            gc.collect()
        assert xdisplay.grab_keyboard()
    finally:
        if cleanup == "close":
            owner.close()
        xdisplay.ungrab_keyboard()


@pytest.mark.parametrize("combo, modifiers", [("Escape", []), ("Ctrl+Escape", ["Control_L"])])
def test_escape_hotkey_second_press_stops(
    xdisplay: X11Display, combo: str, modifiers: list[str]
) -> None:
    """Обе комбинации с Escape проходят реальную очередь и останавливают запись."""
    backend = X11HotkeyBackend()
    manager = HotkeyManager(backend)
    reasons: list[str] = []
    manager.on_state = lambda state, reason: reasons.append(reason)
    try:
        assert manager.grab(combo, HotkeyMode.TOGGLE).ok
        for expected in (HotkeyState.RECORDING, HotkeyState.PROCESSING):
            assert xdisplay.send_combo(modifiers, "Escape")

            def reached(expected: HotkeyState = expected) -> bool:
                manager.process_pending()
                return manager.fsm.state == expected

            wait_until(reached)
        assert reasons == ["press", "toggle-off"]
    finally:
        manager.ungrab()
        backend.close()


@pytest.mark.parametrize("modifiers", [["Control_L"], ["Control_L", "Shift_L"]])
def test_held_modifiers_prevent_all_synthetic_events(
    xdisplay: X11Display, modifiers: list[str]
) -> None:
    """Удержание из PTT/toggle запрещает синтез; очередь чужого окна остаётся пустой."""
    from Xlib import XK, X

    codes = [int(xdisplay.d.keysym_to_keycode(XK.string_to_keysym(name))) for name in modifiers]
    assert all(codes)
    with X11Display() as receiver:
        assert receiver.d is not None
        window = receiver.root.create_window(
            0,
            0,
            100,
            100,
            0,
            X.CopyFromParent,
            override_redirect=True,
            event_mask=X.KeyPressMask | X.KeyReleaseMask,
        )
        try:
            window.map()
            window.set_input_focus(X.RevertToParent, X.CurrentTime)
            receiver.d.sync()
            assert int(receiver.d.get_input_focus().focus.id) == int(window.id)
            for code in codes:
                assert xdisplay.fake_key(code, True)
            keymap = xdisplay.d.query_keymap()
            assert all(keymap[code // 8] & (1 << (code % 8)) for code in codes)
            receiver.d.sync()
            while receiver.pending_events():
                receiver.next_event()
            assert not xdisplay.send_combo(["Control_L"], "v")
            receiver.d.sync()
            assert receiver.pending_events() == 0
            # Отказ не должен отпускать зажатые пользователем клавиши.
            keymap = xdisplay.d.query_keymap()
            assert all(keymap[code // 8] & (1 << (code % 8)) for code in codes)
        finally:
            for code in reversed(codes):
                xdisplay.fake_key(code, False)
            window.destroy()
            receiver.d.sync()


def test_held_hotkey_trigger_blocks_combo_and_paste(
    tmp_path: Path, xapp: Any, xdisplay: X11Display
) -> None:
    """T-49/У50: пассивный grab активен, Space удержан, модификаторы уже отпущены."""
    from Xlib import XK

    combo = xdisplay.parse_combo("Ctrl+Shift+space")
    modifiers = [
        int(xdisplay.d.keysym_to_keycode(XK.string_to_keysym(name)))
        for name in ("Control_L", "Shift_L")
    ]
    assert all(modifiers)
    clipboard = xapp.clipboard()
    with paste_target(tmp_path / "target") as target:
        try:
            target.activate(xdisplay)
            clipboard.setText("Прежний буфер")
            assert xdisplay.grab_key(combo.keycode, combo.mods).ok
            for code in [*modifiers, combo.keycode]:
                assert xdisplay.fake_key(code, True)
            for code in reversed(modifiers):
                assert xdisplay.fake_key(code, False)
            keymap = xdisplay.d.query_keymap()
            assert keymap[combo.keycode // 8] & (1 << (combo.keycode % 8))
            assert not any(keymap[code // 8] & (1 << (code % 8)) for code in modifiers)
            assert not xdisplay.modifiers_held()
            # Другой клиент подтверждает, что наш пассивный grab уже активирован.
            with X11Display() as rival:
                assert rival.d is not None
                assert not rival.grab_keyboard()
            while xdisplay.pending_events():
                xdisplay.next_event()
            with patch.object(xdisplay, "fake_key", wraps=xdisplay.fake_key) as fake:
                assert not xdisplay.send_combo(["Control_L"], "v")
                fake.assert_not_called()
            keymap = xdisplay.d.query_keymap()
            assert keymap[combo.keycode // 8] & (1 << (combo.keycode % 8))
            outcome = PasteFlow().run("Наша фраза", target.window, PasteMode.AUTO)
            assert outcome.kind == PasteOutcomeKind.WINDOW_CHANGED
            assert outcome.restore == PasteRestore.KEPT_OURS
            assert clipboard.text() == "Наша фраза"
            target.expect_text("")

            assert xdisplay.fake_key(combo.keycode, False)
            # Убираем hint первого отказа, чтобы следующий pasted восстановил обычный буфер.
            clipboard.setText("Прежний буфер")
            with patch.object(
                X11Display, "send_combo", autospec=True, side_effect=X11Display.send_combo
            ) as send:
                outcome = PasteFlow().run("Наша фраза", target.window, PasteMode.AUTO)
                send.assert_called_once()
            assert outcome.kind == PasteOutcomeKind.PASTED
            target.expect_text("Наша фраза")
            assert clipboard.text() == "Прежний буфер"
        finally:
            for code in [combo.keycode, *reversed(modifiers)]:
                xdisplay.fake_key(code, False)
            xdisplay.ungrab_key(combo.keycode, combo.mods)
            clipboard.clear()
            xapp.processEvents()


@pytest.mark.parametrize("modifiers", [["Control_L"], ["Control_L", "Shift_L"]])
def test_modifiers_held_uses_keymap_when_pointer_mask_is_zero(
    xdisplay: X11Display, monkeypatch: pytest.MonkeyPatch, modifiers: list[str]
) -> None:
    """Настоящие зажатые клавиши видны даже при принудительно нулевой маске."""
    from Xlib import XK

    monkeypatch.setattr(xdisplay.root, "query_pointer", lambda: SimpleNamespace(mask=0))
    assert xdisplay.semantic_modifiers(int(xdisplay.root.query_pointer().mask)) == 0
    assert not xdisplay.modifiers_held()
    codes = [int(xdisplay.d.keysym_to_keycode(XK.string_to_keysym(name))) for name in modifiers]
    assert all(codes)
    try:
        for code in codes:
            assert xdisplay.fake_key(code, True)
        keymap = xdisplay.d.query_keymap()
        assert all(keymap[code // 8] & (1 << (code % 8)) for code in codes)
        assert xdisplay.semantic_modifiers(int(xdisplay.root.query_pointer().mask)) == 0
        assert xdisplay.modifiers_held()
    finally:
        for code in reversed(codes):
            xdisplay.fake_key(code, False)
    assert not xdisplay.modifiers_held()


def test_xtest_delivers_ctrl_v(tmp_path: Path, xapp: Any, xdisplay: X11Display) -> None:
    """Два Ctrl+V действительно доходят до QPlainTextEdit другого процесса."""
    with paste_target(tmp_path / "target") as target:
        with clipboard_owner(tmp_path / "clipboard", "Проверка, ёж."):
            try:
                target.activate(xdisplay)
                for count in (1, 2):
                    assert xdisplay.send_combo(["Control_L"], "v")
                    target.expect_text("Проверка, ёж." * count)
            finally:
                xapp.clipboard().clear()


def test_active_window_and_wm_class(tmp_path: Path, xapp: Any, xdisplay: X11Display) -> None:
    """Сравнить EWMH и WM_CLASS с winId и заданным именем реального Qt-окна."""
    with paste_target(tmp_path / "target") as target:
        target.activate(xdisplay)
        assert xdisplay.active_window() == target.window
        assert xdisplay.wm_class(target.window) == ("astra-paste-target", "astra-paste-target")
