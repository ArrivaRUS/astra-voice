"""Окно пробы захвата клавиатуры: своё, невидимое WM, одно на соединение.

Соединение Xlib подменено целиком: даже при DISPLAY=:0 к серверу не обращаемся.
Поведение самого сервера (Focus-события, _NET_CLIENT_LIST) — в
``tests/xvfb/test_paste_grab_probe.py``.
"""

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import MagicMock

import pytest

from astra_voice.platform.x11 import X11Display

PROBE_ID = 0x4200007


@pytest.fixture
def connection(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    pytest.importorskip("Xlib")
    from Xlib import X
    from Xlib import display as xdisplay

    conn = MagicMock()
    conn.get_modifier_mapping.return_value = [[] for _ in range(8)]
    root = conn.screen.return_value.root
    window = root.create_window.return_value
    window.id = PROBE_ID
    window.get_attributes.return_value.map_state = X.IsViewable
    monkeypatch.setattr(xdisplay, "Display", lambda name=None: conn)
    return conn


@pytest.fixture
def display(connection: MagicMock) -> Iterator[X11Display]:
    x = X11Display()
    assert x.open(":isolated-mock")
    try:
        yield x
    finally:
        x.close()


def test_probe_window_is_offscreen_input_only_override_redirect(
    display: X11Display, connection: MagicMock
) -> None:
    """Как nullFocus KWin: InputOnly 1×1 в (-1, -1), override-redirect, отображено."""
    from Xlib import X

    assert display.probe_window() == PROBE_ID
    root = connection.screen.return_value.root
    root.create_window.assert_called_once_with(
        -1,
        -1,
        1,
        1,
        0,
        0,
        window_class=X.InputOnly,
        visual=X.CopyFromParent,
        override_redirect=True,
    )
    window = root.create_window.return_value
    window.map.assert_called_once_with()
    window.destroy.assert_not_called()
    # Корень не захватывается и не получает фокус: fly-wm не на что отвечать.
    root.grab_keyboard.assert_not_called()
    root.set_input_focus.assert_not_called()


def test_probe_window_is_created_once_per_connection(
    display: X11Display, connection: MagicMock
) -> None:
    root = connection.screen.return_value.root
    assert display.probe_window() == PROBE_ID
    assert display.probe_window() == PROBE_ID
    root.create_window.assert_called_once()
    root.create_window.return_value.map.assert_called_once_with()


def test_probe_window_not_viewable_is_destroyed(display: X11Display, connection: MagicMock) -> None:
    """Невидимое окно захват не примет (GrabNotViewable): не выдаём его за рабочее."""
    from Xlib import X

    window = connection.screen.return_value.root.create_window.return_value
    window.get_attributes.return_value.map_state = X.IsUnviewable
    assert display.probe_window() is None
    window.destroy.assert_called_once_with()
    # Следующая попытка создаёт окно заново, а не возвращает сломанное.
    window.get_attributes.return_value.map_state = X.IsViewable
    assert display.probe_window() == PROBE_ID
    assert connection.screen.return_value.root.create_window.call_count == 2


def test_probe_window_async_x_error_is_refused(display: X11Display, connection: MagicMock) -> None:
    window = connection.screen.return_value.root.create_window.return_value

    def fail_map() -> None:
        display._on_error(RuntimeError("BadAlloc"), None)

    window.map.side_effect = fail_map
    assert display.probe_window() is None
    window.destroy.assert_called_once_with()


def test_probe_window_exception_is_contained(
    display: X11Display, connection: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    connection.screen.return_value.root.create_window.side_effect = RuntimeError("ПРИВАТНО")
    with caplog.at_level("DEBUG", logger="astra_voice.platform.x11"):
        assert display.probe_window() is None
    assert "ПРИВАТНО" not in caplog.text


def test_probe_window_forgotten_on_close(connection: MagicMock) -> None:
    """Окно — ресурс соединения; после переоткрытия создаётся новое."""
    x = X11Display()
    assert x.open(":isolated-mock")
    assert x.probe_window() == PROBE_ID
    x.close()
    assert x.probe_window() is None
    assert x.open(":isolated-mock")
    try:
        assert x.probe_window() == PROBE_ID
        assert connection.screen.return_value.root.create_window.call_count == 2
    finally:
        x.close()


def test_probe_window_without_connection() -> None:
    assert X11Display().probe_window() is None
