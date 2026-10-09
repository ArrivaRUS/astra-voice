"""Command UI: separate settings, collision refusal and private preview lifecycle."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest
from PyQt5.QtCore import QCoreApplication

from astra_voice.core.settings import Settings, from_dict, is_valid_command_combo
from astra_voice.platform.hotkey import HotkeyManager, X11HotkeyBackend
from astra_voice.platform.x11 import X11Display
from astra_voice.ui.bridges import SettingsBridge
from astra_voice.ui.hotkey_capture import HotkeyCapture

pytestmark = pytest.mark.unit


def test_command_settings_do_not_reinterpret_text_hotkey() -> None:
    settings = from_dict({"hotkey": "Super_L", "command_hotkey": "super_r"})
    assert settings.hotkey == "Ctrl+Space"
    assert settings.command_hotkey == "super_r"
    assert settings.command_enabled
    assert not settings.command_preview
    assert not is_valid_command_combo("Shift+Space")


def _host() -> Any:
    return SimpleNamespace(
        command_installed=True,
        command_available=True,
        command_status="Astra Cowork найден · доступен",
        command_feedback=None,
        check_command_hotkey=Mock(return_value="ok"),
        reload_command_hotkey=Mock(),
        refresh_command_status=Mock(),
        confirm_command=Mock(),
        reject_command=Mock(),
        command_preview_shown=Mock(),
        _command_snapshot=Mock(return_value=SimpleNamespace(allowed=True)),
    )


def test_command_setting_collision_does_not_save_or_change_text() -> None:
    app = QCoreApplication.instance() or QCoreApplication([])
    assert app is not None
    settings = Settings()
    save = Mock()
    host = _host()
    host.check_command_hotkey.return_value = "duplicate"
    bridge = SettingsBridge(settings, save=save)
    bridge.bind_command_host(host)
    bridge.setProperty("commandHotkey", "Control+Space")
    assert bridge.commandHotkey == "Super_L"
    assert settings.hotkey == "Ctrl+Space"
    assert bridge.captureState == "duplicate"
    save.assert_not_called()
    host.reload_command_hotkey.assert_not_called()


def test_command_save_failure_does_not_apply_runtime_setting() -> None:
    settings = Settings()
    mirror = Settings()
    save = Mock(side_effect=OSError("PRIVATE CONTENT"))
    host = _host()
    bridge = SettingsBridge(settings, mirror=mirror, save=save)
    bridge.bind_command_host(host)
    bridge.setProperty("commandHotkey", "Super_R")
    assert settings.command_hotkey == mirror.command_hotkey == bridge.commandHotkey == "Super_L"
    host.reload_command_hotkey.assert_not_called()


def test_preview_only_exposes_explicit_text_and_clears_after_cancel() -> None:
    host = _host()
    bridge = SettingsBridge(Settings(), save=Mock())
    bridge.bind_command_host(host)
    bridge.update_command_preview("PRIVATE COMMAND")
    assert bridge.commandPreviewText == ""
    bridge.setProperty("commandPreview", True)
    bridge.update_command_preview("PRIVATE COMMAND")
    bridge.commandPreviewShown()
    assert bridge.commandPreviewText == "PRIVATE COMMAND"
    assert bridge.commandFeedbackText == ""
    host.command_preview_shown.assert_called_once_with()
    bridge.rejectCommand()
    assert bridge.commandPreviewText == ""
    host.reject_command.assert_called_once_with()
    host.confirm_command.assert_not_called()


def test_capture_role_keeps_single_super_out_of_text() -> None:
    host = Mock()
    host.begin_capture.return_value = True
    host.probe.return_value = "ok"
    host.free_candidates.return_value = []
    save = Mock(return_value="ok")
    capture = HotkeyCapture(host)
    capture.begin(save, role="text")
    capture.end("Super_L")
    save.assert_not_called()
    capture.begin(save, role="command")
    capture.end("Super_L")
    save.assert_called_once_with("Super_L", False)
    host.set_capture_role.assert_called_with("command")


def test_real_keycode_signature_resolves_aliases_without_grab() -> None:
    x11 = X11Display()
    x11.d = Mock()
    x11.d.keysym_to_keycode.return_value = 133
    backend = X11HotkeyBackend(x11)
    manager = HotkeyManager(backend)
    assert manager.signature("Win") == manager.signature("Super_L") == (133, 0)
    assert manager.signature("Control+Space") == manager.signature("Ctrl+space") == (133, 4)
    x11.d.grab_key.assert_not_called()


@pytest.mark.parametrize("key", [False, True, None, 1, [], {}, "", "garbage", "Shift+Space"])
def test_invalid_saved_command_key_disables_without_default_win(key: object) -> None:
    settings = from_dict({"command_hotkey": key, "command_enabled": True})
    assert settings.command_hotkey == ""
    assert not settings.command_enabled
    assert settings.hotkey == "Ctrl+Space"
    assert from_dict({}).command_hotkey == "Super_L"


def test_invalid_saved_key_reason_is_visible_even_while_mode_disabled() -> None:
    bridge = SettingsBridge(from_dict({"command_hotkey": False}), save=Mock())
    assert not bridge.commandEnabled
    assert bridge.commandStatus == "Клавиша команды не распознана — выберите другую"
