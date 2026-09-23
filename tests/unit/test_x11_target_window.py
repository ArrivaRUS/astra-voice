"""Окно-цель при старте записи: EWMH, иначе клиент фокуса ввода (журнал 23.09, no-target)."""

from __future__ import annotations

import logging
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
