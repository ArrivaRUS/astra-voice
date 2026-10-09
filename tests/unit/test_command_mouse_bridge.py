"""Author checks of the QML mouse contract and transactional settings."""

from __future__ import annotations

from dataclasses import replace
from typing import Any
from unittest.mock import Mock

import pytest
from PyQt5.QtTest import QSignalSpy
from test_command_mouse_capture import MouseHost

from astra_voice.core.settings import Settings
from astra_voice.ui.bridges import OnboardingController, SettingsBridge
from astra_voice.ui.hotkey_capture import CaptureHost
from helpers.qt_app import get_qapplication

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def application() -> None:
    get_qapplication()


def make_bridge(**kwargs: Any) -> tuple[SettingsBridge, Settings, Settings, Mock, MouseHost]:
    settings = Settings()
    mirror = replace(settings)
    save = Mock()
    bridge = SettingsBridge(settings, mirror=mirror, save=save, **kwargs)
    host = MouseHost()
    bridge.bind_command_host(host)
    return bridge, settings, mirror, save, host


def test_metaobject_exposes_exact_qml_contract() -> None:
    bridge = SettingsBridge(Settings(), save=Mock())
    meta = bridge.metaObject()
    groups = {
        "commandMouseEnabledChanged": {"commandMouseEnabled": "bool"},
        "commandMouseButtonChanged": {
            "commandMouseButton": "int",
            "commandMouseButtonLabel": "QString",
        },
        "commandMouseStateChanged": {
            "commandMouseStatus": "QString",
            "commandMouseStatusMessage": "QString",
            "commandMouseCanEdit": "bool",
        },
        "commandMouseCaptureChanged": {
            "commandMouseCaptureState": "QString",
            "commandMousePendingButton": "int",
            "commandMousePendingButtonLabel": "QString",
            "commandMouseCaptureMessage": "QString",
        },
    }
    for signal, members in groups.items():
        for name, kind in members.items():
            prop = meta.property(meta.indexOfProperty(name))
            assert prop.typeName() == kind
            assert bytes(prop.notifySignal().name()).decode() == signal
            assert prop.isWritable() == (name == "commandMouseEnabled")
    for signature in (
        "beginCommandMouseCapture()",
        "commandMouseCapturePressed(int)",
        "commandMouseCaptureReleased(int,int)",
        "selectCommandMouseButton(int)",
        "applyCommandMouseButton()",
        "cancelCommandMouseCapture()",
    ):
        assert meta.indexOfMethod(signature.encode()) >= 0
    assert bridge.commandMouseEnabled is False
    assert bridge.commandMouseButton == 2
    assert not bridge.commandMouseCanEdit
    assert not bridge.beginCommandMouseCapture()
    assert not bridge.selectCommandMouseButton(8)


def test_preset_stages_and_apply_persists_without_enabling() -> None:
    bridge, settings, mirror, save, host = make_bridge()
    assert bridge.selectCommandMouseButton(8)
    assert bridge.commandMousePendingButton == 8
    assert bridge.commandMouseButton == 2
    assert settings.command_mouse_button == mirror.command_mouse_button == 2
    save.assert_not_called()
    assert bridge.applyCommandMouseButton()
    save.assert_called_once_with(settings)
    assert settings.command_mouse_button == mirror.command_mouse_button == 8
    assert bridge.commandMouseEnabled is False
    assert bridge.commandMouseCaptureState == "idle"
    assert host.reloads == 1 and len(host.ended) == 1


def test_gesture_cannot_apply_before_release_or_after_cancel() -> None:
    bridge, _, _, save, _ = make_bridge()
    assert bridge.beginCommandMouseCapture()
    bridge.commandMouseCapturePressed(4)
    assert not bridge.applyCommandMouseButton()
    save.assert_not_called()
    bridge.commandMouseCaptureReleased(4, 0)
    assert bridge.commandMousePendingButton == 2
    bridge.cancelCommandMouseCapture()
    bridge.commandMouseCaptureReleased(4, 0)
    assert not bridge.applyCommandMouseButton()
    save.assert_not_called()


def test_save_failure_preserves_candidate_and_retries() -> None:
    bridge, settings, mirror, save, host = make_bridge()
    spy = QSignalSpy(bridge.saveErrorChanged)
    assert bridge.selectCommandMouseButton(9)
    save.side_effect = OSError("private-path")
    assert not bridge.applyCommandMouseButton()
    assert (
        bridge.commandMouseButton
        == settings.command_mouse_button
        == mirror.command_mouse_button
        == 2
    )
    assert bridge.commandMousePendingButton == 9
    assert bridge.commandMouseCaptureState == "ready"
    assert bridge.commandMouseCaptureMessage
    assert len(spy) == 1 and host.reloads == 0 and not host.ended
    save.side_effect = None
    assert bridge.applyCommandMouseButton()
    assert bridge.commandMouseButton == mirror.command_mouse_button == 9
    assert bridge.saveError == "" and len(spy) == 2


def test_enable_policy_and_disable_during_own_operation() -> None:
    bridge, settings, mirror, save, host = make_bridge()
    host.command_mouse_status = "busy"  # external grab conflict still allows editing
    assert bridge.commandMouseCanEdit
    assert bridge.setProperty("commandMouseEnabled", True)
    assert settings.command_mouse_enabled and mirror.command_mouse_enabled
    assert bridge.commandMouseStatus == "busy"
    host.command_mouse_can_edit = False  # own input operation
    host.command_mouse_status = "suspended"
    host.notify()
    assert not bridge.commandMouseCanEdit
    assert bridge.setProperty("commandMouseEnabled", False)
    assert not settings.command_mouse_enabled and not mirror.command_mouse_enabled
    assert save.call_count == 2 and host.reloads == 2
    assert bridge.setProperty("commandMouseEnabled", True)
    assert not bridge.commandMouseEnabled
    assert save.call_count == 2


def test_enable_failure_rolls_back_and_capture_cannot_enable() -> None:
    bridge, settings, mirror, save, host = make_bridge()
    save.side_effect = OSError()
    bridge.setProperty("commandMouseEnabled", True)
    assert not bridge.commandMouseEnabled
    assert not settings.command_mouse_enabled and not mirror.command_mouse_enabled
    assert host.reloads == 0
    save.reset_mock()
    save.side_effect = None
    assert bridge.selectCommandMouseButton(8)
    bridge.setProperty("commandMouseEnabled", True)
    assert not bridge.commandMouseEnabled
    save.assert_not_called()
    bridge.cancelCommandMouseCapture()


@pytest.mark.parametrize("locked", ["command_mouse_enabled", "command_mouse_button"])
def test_locked_settings_are_not_written(locked: str) -> None:
    bridge, _, _, save, _ = make_bridge(locked=[locked])
    if locked.endswith("enabled"):
        bridge.setProperty("commandMouseEnabled", True)
        assert not bridge.commandMouseEnabled
    else:
        assert not bridge.commandMouseCanEdit
        assert not bridge.selectCommandMouseButton(8)
        assert not bridge.beginCommandMouseCapture()
    save.assert_not_called()


def test_mouse_capture_blocks_both_keyboard_roles_and_wizard() -> None:
    keyboard = Mock(spec=CaptureHost)
    keyboard.begin_capture.return_value = True
    bridge, settings, _, _, _ = make_bridge(capture_host=keyboard)
    wizard = OnboardingController(bridge, settings=settings, device_provider=lambda: [])
    assert bridge.beginCommandMouseCapture()
    bridge.beginCapture()
    bridge.beginCommandCapture()
    wizard.beginCapture()
    wizard.beginCommandCapture()
    keyboard.begin_capture.assert_not_called()
    bridge.cancelCommandMouseCapture()
    bridge.beginCapture()
    keyboard.begin_capture.assert_called_once()
    assert not bridge.beginCommandMouseCapture()
    assert not bridge.selectCommandMouseButton(8)
    bridge.cancelCapture()


def test_invalid_session_cancels_before_apply_without_notification() -> None:
    bridge, _, _, save, host = make_bridge()
    assert bridge.selectCommandMouseButton(8)
    host.valid = False
    assert not bridge.applyCommandMouseButton()
    save.assert_not_called()
    assert bridge.commandMouseCaptureState == "idle"
    assert bridge.commandMousePendingButton == 0


def test_missing_host_cannot_enable_but_can_disable_existing_setting() -> None:
    settings = Settings(command_mouse_enabled=True)
    save = Mock()
    bridge = SettingsBridge(settings, save=save)
    assert bridge.commandMouseStatus == "unavailable"
    bridge.setProperty("commandMouseEnabled", False)
    assert not settings.command_mouse_enabled
    bridge.setProperty("commandMouseEnabled", True)
    assert not settings.command_mouse_enabled
    save.assert_called_once()


@pytest.mark.parametrize("action", ["begin", "select", "apply", "press", "release"])
def test_blocked_operation_clears_lease_before_any_action(action: str) -> None:
    bridge, _, _, save, host = make_bridge()
    assert bridge.selectCommandMouseButton(8)
    host.command_mouse_can_edit = False
    if action == "begin":
        assert not bridge.beginCommandMouseCapture()
    elif action == "select":
        assert not bridge.selectCommandMouseButton(9)
    elif action == "apply":
        assert not bridge.applyCommandMouseButton()
    elif action == "press":
        bridge.commandMouseCapturePressed(8)
    else:
        bridge.commandMouseCaptureReleased(8, 0)
    save.assert_not_called()
    assert bridge.commandMouseCaptureState == "idle" and len(host.ended) == 1


def test_saved_notification_cannot_cancel_replacement_editor() -> None:
    bridge, _, _, _, host = make_bridge()
    assert bridge.selectCommandMouseButton(8)

    def reopen() -> None:
        bridge.cancelCommandMouseCapture()
        assert bridge.beginCommandMouseCapture()

    bridge.commandMouseButtonChanged.connect(reopen)
    assert bridge.applyCommandMouseButton()
    assert bridge.commandMouseButton == 8
    assert bridge.commandMouseCaptureState == "capturing"
    assert bridge.commandMousePendingButton == 0 and len(host.ended) == 1
    bridge.cancelCommandMouseCapture()


def test_reload_failure_never_reports_optimistic_ready() -> None:
    bridge, settings, _, _, host = make_bridge()

    def fail() -> None:
        raise OSError("private detail")

    host.reload_command_mouse = fail  # type: ignore[method-assign]
    bridge.setProperty("commandMouseEnabled", True)
    assert settings.command_mouse_enabled
    assert bridge.commandMouseStatus == "unavailable"
    assert "private detail" not in bridge.commandMouseStatusMessage
