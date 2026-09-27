"""Запрос переноса и фокуса KWin без настоящего X-сервера."""

from __future__ import annotations

import sys
from unittest.mock import Mock, call

import pytest

from astra_voice.platform import x11

pytestmark = pytest.mark.unit


def _display(
    monkeypatch: pytest.MonkeyPatch, window_desktop: int | None
) -> tuple[Mock, Mock, Mock]:
    events = Mock()
    monkeypatch.setitem(
        sys.modules,
        "Xlib",
        Mock(X=Mock(SubstructureRedirectMask=1, SubstructureNotifyMask=2), Xatom=Mock(CARDINAL=6)),
    )
    monkeypatch.setitem(sys.modules, "Xlib.protocol", Mock(event=events))
    display = Mock()
    display._require_display.return_value = display.d
    display.d.intern_atom.side_effect = [71, 72, 73]
    display.root.get_full_property.return_value = Mock(format=32, value=[3])
    display.d.create_resource_object.return_value.get_full_property.return_value = (
        Mock(format=32, value=[window_desktop]) if window_desktop is not None else None
    )
    display._error_count = 0
    display.__enter__ = Mock(return_value=display)
    display.__exit__ = Mock(return_value=None)
    factory = Mock(return_value=display)
    monkeypatch.setattr(x11, "X11Display", factory)
    return display, events, factory


def test_focus_window_moves_then_activates_on_one_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    display, events, factory = _display(monkeypatch, 1)
    move = Mock(name="move_message")
    activate = Mock(name="activate_message")
    events.ClientMessage.side_effect = [move, activate]

    assert x11.focus_window(42)
    factory.assert_called_once_with()
    display.d.intern_atom.assert_has_calls(
        [
            call("_NET_CURRENT_DESKTOP", only_if_exists=True),
            call("_NET_WM_DESKTOP", only_if_exists=True),
            call("_NET_ACTIVE_WINDOW"),
        ]
    )
    display.d.create_resource_object.assert_called_once_with("window", 42)
    assert events.ClientMessage.call_args_list == [
        call(window=42, client_type=72, data=(32, [3, 2, 0, 0, 0])),
        call(window=42, client_type=73, data=(32, [2, 0, 0, 0, 0])),
    ]
    assert display.root.send_event.call_args_list == [
        call(move, event_mask=3),
        call(activate, event_mask=3),
    ]
    display.d.sync.assert_called_once_with()
    display.d.flush.assert_not_called()


@pytest.mark.parametrize("window_desktop", [3, 0xFFFFFFFF, None])
def test_focus_window_skips_move_but_activates(
    monkeypatch: pytest.MonkeyPatch, window_desktop: int | None
) -> None:
    display, events, _ = _display(monkeypatch, window_desktop)

    assert x11.focus_window(42)
    events.ClientMessage.assert_called_once_with(
        window=42, client_type=73, data=(32, [2, 0, 0, 0, 0])
    )
    display.root.send_event.assert_called_once_with(events.ClientMessage.return_value, event_mask=3)
    display.d.sync.assert_called_once_with()
    display.d.flush.assert_not_called()


def test_focus_window_reports_x_error(monkeypatch: pytest.MonkeyPatch) -> None:
    display, _, _ = _display(monkeypatch, 1)
    display.d.sync.side_effect = lambda: setattr(display, "_error_count", 1)
    assert not x11.focus_window(42)


def test_focus_window_without_display_or_xlib(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DISPLAY", raising=False)
    assert not x11.focus_window(42)
    monkeypatch.setenv("DISPLAY", ":unused")
    monkeypatch.setitem(sys.modules, "Xlib", None)
    assert not x11.focus_window(42)
