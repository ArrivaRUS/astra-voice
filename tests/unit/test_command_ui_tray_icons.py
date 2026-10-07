"""Command role is a visible shape badge on every existing tray status."""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from astra_voice.platform.session import SessionKind
from astra_voice.ui.tray import Tray
from astra_voice.ui.tray_icons import TrayIconProvider, TrayState
from helpers.qt_app import get_qapplication

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("dark", [False, True])
@pytest.mark.parametrize("state", list(TrayState))
@pytest.mark.parametrize("size", [16, 22])
def test_command_badge_preserves_status_dots_and_changes_shape(
    state: TrayState, dark: bool, size: int
) -> None:
    get_qapplication()
    provider = TrayIconProvider(SessionKind.FLY, panel_dark=lambda: dark)
    original = provider.icon(state).pixmap(size, size).toImage()
    command = provider.command_icon(state).pixmap(size, size).toImage()
    assert not original.isNull() and not command.isNull()
    assert original.size() == command.size()
    # The coloured status dot and first two brand dots stay pixel-for-pixel intact.
    for y in range(int(size * 12 / 22)):
        for x in range(size):
            assert command.pixel(x, y) == original.pixel(x, y)
    changes = sum(
        command.pixel(x, y) != original.pixel(x, y)
        for y in range(int(size * 12 / 22), size)
        for x in range(size)
    )
    assert changes >= 12
    assert provider.command_icon(state) is provider.command_icon(state)


def test_command_badge_refreshes_after_panel_theme_change() -> None:
    get_qapplication()
    dark = False
    provider = TrayIconProvider(SessionKind.FLY, panel_dark=lambda: dark)
    light = provider.command_icon(TrayState.IDLE)
    light_pixels = light.pixmap(22, 22).toImage()
    dark = True
    assert provider.command_icon(TrayState.IDLE) is light
    provider.refresh()
    dark_icon = provider.command_icon(TrayState.IDLE)
    assert dark_icon is not light
    assert dark_icon.pixmap(22, 22).toImage() != light_pixels


def test_command_role_survives_processing_terminal_and_recovery_then_resets() -> None:
    get_qapplication()
    provider = TrayIconProvider(SessionKind.FLY, panel_dark=lambda: True)
    surface = Mock()
    tray = Tray(provider, tray_factory=lambda: surface)
    tray.set_has_last_text(True)
    tray.set_command_mode(True)
    for state in (
        TrayState.LISTENING,
        TrayState.PROCESSING,
        TrayState.DONE,
        TrayState.ERROR,
        TrayState.IDLE,
    ):
        tray.set_state(state)
        icon = surface.setIcon.call_args.args[0]
        assert icon is provider.command_icon(state)
    tray.set_command_feedback("Помощник не ответил вовремя")
    assert tray._copy_action.isEnabled()
    assert tray._command_details_action.isVisible()
    tray.set_command_mode(False)
    assert surface.setIcon.call_args.args[0] is provider.icon(TrayState.IDLE)
    # Role reset leaves the saved phrase and recovery actions intact.
    assert tray._copy_action.isEnabled()
    assert tray._command_details_action.isVisible()
    tray.stop()
