"""Real settings/controller/runtime lease; fake physical input and worker ports only."""

from __future__ import annotations

from dataclasses import replace
from typing import Any
from unittest.mock import Mock

import pytest
from PyQt5 import sip
from PyQt5.QtCore import QCoreApplication, QEvent

from astra_voice.core.command_mode import SessionSnapshot
from astra_voice.ui.bridges import SettingsBridge

from .test_mouse_input_ownership import Rig

pytestmark = pytest.mark.unit


@pytest.fixture
def capture(monkeypatch: pytest.MonkeyPatch) -> Any:
    r = Rig(monkeypatch)
    r.arm_mouse()
    mirror = replace(r.runtime.settings)
    saved = Mock()
    bridge = SettingsBridge(
        r.runtime.settings, mirror=mirror, save=saved, device_provider=lambda: []
    )
    bridge.bind_command_host(r.runtime)
    bridge._mouse_capture._clock = lambda: r.hardware.now
    try:
        yield r, bridge, mirror, saved
    finally:
        r.close()
        sip.delete(bridge)


def test_assignment_reserves_all_inputs_and_never_opens_audio(capture: Any) -> None:
    r, bridge, mirror, saved = capture
    assert bridge.beginCommandMouseCapture()
    r.key("command", "KeyPress")
    r.key("text", "KeyPress")
    r.mouse_event("press")
    r.hardware.now += 0.31
    r.mouse.tick()
    assert r.commands() == [] and r.client.calls == []
    bridge.commandMouseCapturePressed(4)
    assert bridge.commandMouseCaptureState == "pressed"
    assert not bridge.applyCommandMouseButton()
    bridge.commandMouseCaptureReleased(4, 1)
    assert not bridge.applyCommandMouseButton()
    assert r.runtime.settings.command_mouse_button == mirror.command_mouse_button == 2
    saved.assert_not_called()
    bridge.commandMouseCaptureReleased(1, 0)
    bridge.commandMouseCapturePressed(1 << 3)
    bridge.commandMouseCaptureReleased(1 << 3, 0)
    assert bridge.commandMouseCaptureState == "ready" and bridge.commandMousePendingButton == 8
    assert bridge.applyCommandMouseButton()
    assert r.runtime.settings.command_mouse_button == mirror.command_mouse_button == 8
    assert r.commands() == [] and r.client.calls == []
    assert saved.call_count == 1


def test_preset_save_failure_then_retry_does_not_enable_mouse(capture: Any) -> None:
    r, bridge, mirror, saved = capture
    bridge.commandMouseEnabled = False
    saved.reset_mock()
    assert bridge.selectCommandMouseButton(31)
    assert bridge.commandMouseButton == 2
    saved.assert_not_called()
    saved.side_effect = OSError("synthetic save failure")
    assert not bridge.applyCommandMouseButton()
    assert bridge.commandMousePendingButton == 31 and bridge.commandMouseCaptureState == "ready"
    assert (
        bridge.commandMouseButton
        == r.runtime.settings.command_mouse_button
        == mirror.command_mouse_button
        == 2
    )
    assert not bridge.commandMouseEnabled and not mirror.command_mouse_enabled
    saved.side_effect = None
    assert bridge.applyCommandMouseButton()
    assert bridge.commandMouseButton == mirror.command_mouse_button == 31
    assert not bridge.commandMouseEnabled and not r.runtime.settings.command_mouse_enabled
    assert r.commands() == [] and r.client.calls == []


def test_old_host_token_and_timeout_cannot_release_replacement_capture(capture: Any) -> None:
    r, bridge, _mirror, saved = capture
    assert bridge.beginCommandMouseCapture()
    old = bridge._mouse_capture._lease.token
    bridge.cancelCommandMouseCapture()
    r.hardware.now += 1
    assert bridge.selectCommandMouseButton(9)
    current = bridge._mouse_capture._lease.token
    assert current is not None and current is not old
    r.runtime.end_command_mouse_capture(old)
    bridge._mouse_capture._timer.timeout.emit()  # delayed old Qt timeout, new deadline not reached
    assert r.runtime.command_mouse_capture_valid(current)
    assert bridge.commandMouseCaptureState == "ready"
    saved.assert_not_called()
    assert bridge.applyCommandMouseButton()
    assert r.commands() == []


@pytest.mark.parametrize("event", [QEvent.Hide, QEvent.WindowDeactivate, QEvent.Close])
def test_window_lifecycle_releases_runtime_capture_before_late_apply(
    capture: Any, event: QEvent.Type
) -> None:
    from PyQt5.QtGui import QWindow

    r, bridge, _mirror, saved = capture
    window = QWindow()
    bridge.attach_window(window)
    assert bridge.selectCommandMouseButton(9)
    token = bridge._mouse_capture._lease.token
    QCoreApplication.sendEvent(window, QEvent(event))
    assert bridge.commandMouseCaptureState == "idle"
    assert not r.runtime.command_mouse_capture_valid(token)
    assert not bridge.applyCommandMouseButton()
    saved.assert_not_called()
    assert r.commands() == []
    sip.delete(window)


def test_session_lock_revokes_real_controller_and_rejects_late_release(capture: Any) -> None:
    r, bridge, _mirror, saved = capture
    assert bridge.beginCommandMouseCapture()
    bridge.commandMouseCapturePressed(4)
    r.snapshot = SessionSnapshot(known=True, locked=True)
    r.runtime._command_session_changed()
    bridge.commandMouseCaptureReleased(4, 0)
    assert bridge.commandMouseCaptureState == "idle"
    assert not bridge.applyCommandMouseButton()
    saved.assert_not_called()
    assert r.commands() == [] and r.client.calls == []


def test_expired_capture_does_not_save_or_reuse_lease(capture: Any) -> None:
    r, bridge, _mirror, saved = capture
    assert bridge.selectCommandMouseButton(31)
    token = bridge._mouse_capture._lease.token
    r.hardware.now += bridge._mouse_capture.TIMEOUT_MS / 1000
    bridge._mouse_capture._timer.timeout.emit()
    assert bridge.commandMouseCaptureState == "idle"
    assert not r.runtime.command_mouse_capture_valid(token)
    assert not bridge.applyCommandMouseButton()
    saved.assert_not_called()
    assert r.commands() == [] and r.client.calls == []


def test_controller_destruction_releases_actual_runtime_token(capture: Any) -> None:
    r, bridge, _mirror, saved = capture
    assert bridge.selectCommandMouseButton(9)
    token = bridge._mouse_capture._lease.token
    sip.delete(bridge._mouse_capture)
    assert not r.runtime.command_mouse_capture_valid(token)
    # The bridge is deliberately no longer usable; stop host callbacks before
    # fixture shutdown because its child controller's Qt signals are destroyed.
    r.runtime.on_command_mouse_changed = None
    saved.assert_not_called()
    assert r.commands() == [] and r.client.calls == []


@pytest.mark.parametrize("failure", ["factory", "open", "close"])
def test_failed_keyboard_capture_cannot_block_later_mouse_assignment(
    capture: Any, failure: str
) -> None:
    r, bridge, _mirror, saved = capture
    watchdog = r.hardware.create_capture_watchdog()
    r.hardware.capture_watchdog_factory.side_effect = None
    r.hardware.capture_watchdog_factory.return_value = watchdog
    if failure == "factory":
        r.hardware.capture_watchdog_factory.side_effect = RuntimeError("factory failure")
    elif failure == "open":
        watchdog.open.side_effect = RuntimeError("open failure")
    else:
        watchdog.close.side_effect = RuntimeError("close failure")
    if failure == "close":
        assert r.runtime.begin_hotkey_capture(role="text")
        r.runtime.end_hotkey_capture()
    else:
        assert not r.runtime.begin_hotkey_capture(role="text")
    assert bridge.selectCommandMouseButton(31)
    token = bridge._mouse_capture._lease.token
    r.runtime.end_hotkey_capture()  # late old-editor cleanup cannot release mouse owner
    assert r.runtime.command_mouse_capture_valid(token)
    assert bridge.applyCommandMouseButton()
    assert saved.call_count == 1 and bridge.commandMouseButton == 31
    assert r.commands() == [] and r.client.calls == []
