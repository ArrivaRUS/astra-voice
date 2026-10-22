"""ИБ-73: reconstruct the Qt tray after Fly manager returns, no real tray."""

from __future__ import annotations

from unittest.mock import Mock

import pytest
from test_tray import Harness
from test_tray import forbid_real_x as forbid_real_x
from test_tray import harness as harness
from test_tray import qapp as qapp

pytestmark = pytest.mark.unit


def test_ib73_panel_return_recreates_icon_keeps_menu_and_state(harness: Harness) -> None:
    h = harness
    h.tray.start()
    old = h.icon
    menu = h.menu
    fresh = Mock()
    fresh.isVisible.return_value = False
    fresh.isSystemTrayAvailable.return_value = True
    fresh.show.side_effect = lambda: setattr(fresh.isVisible, "return_value", True)
    h.factory.return_value = fresh
    h.tray.refresh_after_panel_restart()
    assert h.factory.call_count == 2
    assert h.tray._tray is fresh
    fresh.activated.connect.assert_called_once()
    fresh.setContextMenu.assert_called_once_with(menu)
    fresh.setIcon.assert_called()
    fresh.setToolTip.assert_called()
    old.hide.assert_called()
    h.provider.refresh.assert_called_once()
    assert h.tray._running
