"""Окно-цель при старте записи: EWMH, иначе клиент фокуса ввода (журнал 23.09, no-target)."""

from __future__ import annotations

import logging
from collections.abc import Collection
from typing import Any, cast
from unittest.mock import Mock

import pytest

from astra_voice.platform.x11 import X11Display

pytestmark = pytest.mark.unit


def _window(wid: int, parent: object | None = None, *, override: bool = False) -> Mock:
    window = Mock()
    window.id = wid
    window.get_attributes.return_value.override_redirect = override
    window.query_tree.return_value.parent = parent
    return window


def _display(focus: Mock, clients: list[int]) -> X11Display:
    x = X11Display()
    x.d = Mock()
    x.root = _window(2)
    x.d.get_input_focus.return_value.focus = focus
    x.client_list_stacking = Mock(return_value=clients)  # type: ignore[method-assign]
    return x


def test_focus_client_climbs_to_ewmh_client() -> None:
    root = _window(2)
    frame = _window(500, root)
    client = _window(42, frame)
    widget = _window(4200, client)
    x = _display(widget, [7, 42])
    assert x.focus_client() == 42


def test_focus_client_rejects_override_redirect_and_unknown() -> None:
    root = _window(2)
    popup = _window(77, root, override=True)
    assert _display(popup, [42]).focus_client() is None
    stray = _window(90, root)
    assert _display(stray, [42]).focus_client() is None
    assert _display(_window(1), [42]).focus_client() is None
    assert _display(_window(4200, _window(42, root)), []).focus_client() is None


def test_focus_client_survives_x_errors() -> None:
    x = X11Display()
    x.d = Mock()
    x.root = _window(2)
    x.d.get_input_focus.side_effect = RuntimeError("boom")
    x.client_list_stacking = Mock(return_value=[42])  # type: ignore[method-assign]
    assert x.focus_client() is None


def test_target_window_prefers_active_then_focus(caplog: pytest.LogCaptureFixture) -> None:
    root = _window(2)
    client = _window(42, root)
    x = _display(_window(4200, client), [42])
    x.active_window = Mock(return_value=99)  # type: ignore[method-assign]
    assert x.target_window() == 99
    x.active_window = Mock(return_value=None)  # type: ignore[method-assign]
    with caplog.at_level(logging.INFO, logger="astra_voice.platform.x11"):
        assert x.target_window() == 42
    assert any("из фокуса ввода" in record.message for record in caplog.records)


def test_target_window_none_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    x = _display(_window(1), [])
    x.active_window = Mock(return_value=None)  # type: ignore[method-assign]
    with caplog.at_level(logging.INFO, logger="astra_voice.platform.x11"):
        assert x.target_window() is None
    assert any("не найдено" in record.message for record in caplog.records)


# T-280 (Fly 1.8.4, журнал 20.10): fly-wm делает активной нашу видимую пилюлю, фокус
# ввода при этом остаётся в Writer. Контракт: ``target_window(exclude=...)`` — XID своих
# окон (текущий XID пилюли); активное окно из ``exclude`` не считается целью.
PILL_XID = 0x3A00007


def _target(x: X11Display, exclude: Collection[int]) -> int | None:
    """Вызов нового контракта; ``cast`` держит mypy зелёным до появления параметра."""
    window: int | None = cast(Any, x).target_window(exclude=exclude)
    return window


def test_target_window_skips_own_pill_and_takes_focus_client(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Активное окно EWMH — наша пилюля → цель — клиент фокуса ввода (Writer)."""
    root = _window(2)
    writer = _window(42, root)
    x = _display(_window(4200, writer), [42, PILL_XID])
    x.active_window = Mock(return_value=PILL_XID)  # type: ignore[method-assign]
    with caplog.at_level(logging.INFO, logger="astra_voice.platform.x11"):
        assert _target(x, {PILL_XID}) == 42
    assert any("из фокуса ввода" in record.message for record in caplog.records)


@pytest.mark.parametrize("focus", ["unknown", "pill"])
def test_target_window_own_pill_without_focus_client_is_none(
    caplog: pytest.LogCaptureFixture, focus: str
) -> None:
    """Активна пилюля, клиента фокуса нет (или это тоже пилюля) → None (ветка no-target).

    Пилюлю нельзя отдать целью ни из активного окна, ни из фокуса ввода: Ctrl+V ушёл
    бы в наше окно, а фраза — только в буфер без пометки «окно сменилось».
    """
    root = _window(2)
    focused = _window(1) if focus == "unknown" else _window(PILL_XID, root)
    x = _display(focused, [42, PILL_XID])
    x.active_window = Mock(return_value=PILL_XID)  # type: ignore[method-assign]
    with caplog.at_level(logging.INFO, logger="astra_voice.platform.x11"):
        assert _target(x, {PILL_XID}) is None
    assert any("не найдено" in record.message for record in caplog.records)


def test_target_window_foreign_active_window_unchanged_by_exclude() -> None:
    """Контроль: активное окно ≠ пилюля → как раньше, активное окно; фокус не спрашиваем."""
    root = _window(2)
    x = _display(_window(4200, _window(42, root)), [42, 99, PILL_XID])
    x.active_window = Mock(return_value=99)  # type: ignore[method-assign]
    x.focus_client = Mock(return_value=42)  # type: ignore[method-assign]
    assert _target(x, {PILL_XID}) == 99
    assert _target(x, ()) == 99
    x.focus_client.assert_not_called()
