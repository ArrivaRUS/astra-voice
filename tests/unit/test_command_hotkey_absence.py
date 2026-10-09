"""Author regression checks for revoked command keyboard settings."""

from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest

from astra_voice.core.settings import Settings
from astra_voice.ui.bridges import OnboardingController, SettingsBridge
from astra_voice.ui.hotkey_capture import CaptureHost
from helpers.qt_app import get_qapplication

pytestmark = pytest.mark.unit


def setup_bridge() -> tuple[SettingsBridge, Any, Mock, Mock]:
    get_qapplication()
    capture = Mock(spec=CaptureHost)
    capture.begin_capture.return_value = True
    capture.probe.return_value = "busy"
    capture.free_candidates.return_value = []
    saved = Mock()
    bridge = SettingsBridge(
        Settings(command_hotkey="Super_R"),
        save=saved,
        capture_host=capture,
        device_provider=lambda: [],
    )
    host = SimpleNamespace(
        command_installed=True,
        command_available=True,
        command_status="Не запущен",
        check_command_hotkey=Mock(return_value="busy"),
        reload_command_hotkey=Mock(),
        command_mouse_can_edit=False,
    )
    bridge.bind_command_host(host)
    return bridge, host, capture, saved


@pytest.mark.parametrize("wizard", [False, True])
@pytest.mark.parametrize("notify", [False, True])
def test_absence_rejects_stale_conflict_keep(wizard: bool, notify: bool) -> None:
    bridge, host, capture, saved = setup_bridge()
    screen: Any = (
        OnboardingController(
            bridge,
            settings=Settings(extra={"onboarding_language_set": True}),
            device_provider=lambda: [],
        )
        if wizard
        else bridge
    )
    screen.beginCommandCapture()
    screen.endCapture("Ctrl+Alt+Space")
    assert bridge.captureState == "conflict"
    host.command_installed = False
    if notify:
        bridge.update_command_state()
    screen.keepCombo()
    saved.assert_not_called()
    assert bridge.commandHotkey == "Super_R"
    assert bridge.captureState == "idle"
    assert bridge.pendingCombo == ""
    capture.end_capture.assert_called()


@pytest.mark.parametrize(
    "state", ["capturing", "captured", "conflict", "duplicate", "not-grabbed", "success"]
)
def test_absence_cancels_every_command_capture_state(state: str) -> None:
    bridge, host, _, saved = setup_bridge()
    bridge.beginCommandCapture()
    bridge.capture._set_pending("Ctrl+Alt+Space")
    bridge.capture._set_state(state)
    host.command_installed = False
    bridge.update_command_state()
    assert bridge.captureState == "idle"
    assert bridge.pendingCombo == ""
    saved.assert_not_called()


@pytest.mark.parametrize("action", ["begin", "end", "save", "setter", "win", "candidates"])
def test_absence_blocks_direct_command_paths(action: str) -> None:
    bridge, host, capture, saved = setup_bridge()
    bridge.beginCommandCapture()
    host.command_installed = False
    capture.reset_mock()
    if action == "begin":
        bridge.beginCommandCapture()
    elif action == "end":
        bridge.endCapture("Ctrl+Alt+Space")
    elif action == "save":
        assert bridge.save_command_capture_combo("Super_R", False) != "ok"
    elif action == "setter":
        host.check_command_hotkey.return_value = "ok"
        bridge.setProperty("commandHotkey", "Super_L")
    elif action == "win":
        host.check_command_hotkey.return_value = "ok"
        bridge.useCommandWin()
    else:
        bridge.refreshCandidates()
    saved.assert_not_called()
    capture.begin_capture.assert_not_called()
    capture.probe.assert_not_called()
    capture.free_candidates.assert_not_called()
    assert bridge.commandHotkey == "Super_R"
    assert bridge.captureState == "idle"


def test_absence_does_not_cancel_text_capture() -> None:
    bridge, host, _, saved = setup_bridge()
    bridge.beginCapture()
    host.command_installed = False
    bridge.update_command_state()
    assert bridge.captureRole == "text"
    assert bridge.captureState == "capturing"
    bridge.cancelCapture()
    saved.assert_not_called()


def test_installed_but_not_running_allows_command_keep() -> None:
    bridge, _, _, saved = setup_bridge()
    bridge.beginCommandCapture()
    bridge.endCapture("Ctrl+Alt+Space")
    bridge.keepCombo()
    saved.assert_called_once()
    assert bridge.commandHotkey == "Ctrl+Alt+Space"
    assert bridge.captureState == "success"


def test_missing_host_cannot_change_saved_command_key() -> None:
    get_qapplication()
    saved = Mock()
    bridge = SettingsBridge(Settings(command_hotkey="Super_R"), save=saved)
    bridge.setProperty("commandHotkey", "Super_L")
    bridge.beginCommandCapture()
    bridge.useCommandWin()
    saved.assert_not_called()
    assert bridge.commandHotkey == "Super_R"
    assert bridge.captureState == "idle"


def test_absence_during_host_check_prevents_save() -> None:
    bridge, host, _, saved = setup_bridge()

    def lose_host(_combo: str) -> str:
        host.command_installed = False
        return "ok"

    host.check_command_hotkey.side_effect = lose_host
    bridge.setProperty("commandHotkey", "Super_L")
    saved.assert_not_called()
    assert bridge.commandHotkey == "Super_R"


def test_absence_cancel_tolerates_synchronous_host_notification() -> None:
    bridge, host, capture, saved = setup_bridge()
    bridge.beginCommandCapture()
    host.command_installed = False
    capture.end_capture.reset_mock()

    def notify() -> None:
        # Bound a broken recursive implementation without exhausting the stack.
        if capture.end_capture.call_count < 3:
            bridge.update_command_state()

    capture.end_capture.side_effect = notify
    bridge.update_command_state()
    capture.end_capture.assert_called_once()
    assert bridge.captureState == "idle"
    assert bridge.pendingCombo == ""
    saved.assert_not_called()
