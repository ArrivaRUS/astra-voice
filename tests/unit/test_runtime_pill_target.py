"""T-280: окно-цель фразы не может быть нашей пилюлей (Fly 1.8.4, журнал 20.10).

fly-wm делает видимую пилюлю (обычное управляемое окно) активным окном EWMH, фокус
ввода при этом остаётся в Writer. Без исключения XID пилюли цель фразы = пилюля →
вставка уходит в буфер с ``window-changed``. Runtime снимает цель в двух местах:
нажатие (``active_window`` оркестратора) и пробуждение модели по хоткею (US-2.6,
``_wake_for_hotkey``) — оба должны передать текущий XID пилюли в
``X11Display.target_window(exclude=...)``.

Контракт для разработчика: ``Pill.window_id`` (свойство, ``int | None`` — текущий
``_wid``, меняется при смене флагов) и ``X11Display.target_window(exclude:
Collection[int] = ())``. Фейк X11 ниже ведёт себя как fly-wm: активное окно — пилюля,
пока её XID не исключён; иначе — окно ввода 4321.
"""

from __future__ import annotations

from collections.abc import Collection

import pytest
from test_model_idle_unload import make_rig, park
from test_runtime import MARKER, Rig, assert_phase

from astra_voice.core.dictation import DictationPhase
from astra_voice.platform.paste import snapshot_target

pytestmark = pytest.mark.unit

INPUT_XID = 4321  # rig.target: окно ввода (Writer)
PILL_XID = 0x3A00007
REMAPPED_PILL_XID = 0x3A00011


def fly_wm_active_pill(rig: Rig) -> None:
    """``target_window`` как на Fly: пилюля активна, фокус ввода — в окне 4321."""

    def target_window(*args: Collection[int], **kwargs: Collection[int]) -> int | None:
        exclude = set(kwargs.get("exclude", args[0] if args else ()))
        pill = rig.pill.window_id
        return INPUT_XID if pill in exclude else pill

    rig.x11.target_window.side_effect = target_window


def test_press_target_excludes_visible_pill(monkeypatch: pytest.MonkeyPatch) -> None:
    """Нажатие при видимой пилюле → фраза вставляется в окно ввода, не в пилюлю."""
    rig = make_rig(monkeypatch)
    rig.pill.window_id = PILL_XID
    fly_wm_active_pill(rig)
    rig.recognize()
    assert rig.paste.call_args.args[0] == MARKER
    assert rig.paste.call_args.args[1] == rig.target, "окно-цель фразы — наша пилюля"


def test_press_target_uses_current_pill_xid_after_remap(monkeypatch: pytest.MonkeyPatch) -> None:
    """XID пилюли меняется при смене флагов — исключается текущий, не запомненный при старте."""
    rig = make_rig(monkeypatch)
    rig.pill.window_id = PILL_XID
    fly_wm_active_pill(rig)
    rig.recognize()
    assert rig.paste.call_args.args[1] == rig.target
    rig.pill.window_id = REMAPPED_PILL_XID
    rig.now += 5
    rig.recognize()
    assert rig.paste.call_count == 2
    assert rig.paste.call_args.args[1] == rig.target, "исключён устаревший XID пилюли"


def test_wake_target_excludes_visible_pill(monkeypatch: pytest.MonkeyPatch) -> None:
    """US-2.6: нажатие при выгруженной модели (пилюля «Загружаю модель…») — цель тоже
    снимается без пилюли; позже фокус ушёл — вставка в окно на момент нажатия."""
    rig = make_rig(monkeypatch)
    park(rig)
    rig.pill.window_id = PILL_XID
    fly_wm_active_pill(rig)
    rig.press(rig.now)
    # Снимок цели — на нажатии; дальнейшие вызовы видят уже другое окно.
    rig.x11.target_window.side_effect = None
    rig.x11.target_window.return_value = 9999
    rig.event(type="model.loaded")
    rig.event(type="result", utterance_id="file", text="проверка")
    assert_phase(rig.runtime, DictationPhase.RECORDING)
    rig.release(rig.now + 2)
    rig.event(type="result", text=MARKER)
    assert rig.paste.call_count == 1
    assert rig.paste.call_args.args[1] == rig.target, "окно-цель пробуждения — наша пилюля"
    assert rig.paste.call_args.args[1] != snapshot_target(rig.x11, PILL_XID)


def test_press_target_without_pill_xid_is_active_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """Контроль: у пилюли нет XID (не xcb / окно не создано) — цель как раньше."""
    rig = make_rig(monkeypatch)
    rig.pill.window_id = None
    rig.x11.target_window.side_effect = lambda *args, **kwargs: INPUT_XID
    rig.recognize()
    assert rig.paste.call_args.args[1] == rig.target
