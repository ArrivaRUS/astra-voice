"""Author ownership/Escape interleaving regressions; fake hardware only."""

from __future__ import annotations

from typing import cast
from unittest.mock import Mock

import pytest
from test_command_mouse_runtime import MouseRuntimeRig
from test_runtime import FakeTimer, Signal

from astra_voice.core.dictation import DictationPhase
from astra_voice.platform.hotkey import (
    GrabResult,
    HotkeyEvent,
    HotkeyManager,
    HotkeyMode,
    HotkeyState,
    MappingEvent,
)
from astra_voice.platform.mouse_button import MouseHoldState

pytestmark = pytest.mark.unit


def test_external_escape_without_combo_and_stale_release() -> None:
    backend = MouseRuntimeRig.keyboard_backend(65, 4)
    manager = HotkeyManager(backend)
    first, second = object(), object()
    callback = Mock()
    assert manager.acquire_external_escape(first, callback).ok
    manager.handle_event(HotkeyEvent("KeyPress", 9, 1, escape=True), 0)
    callback.assert_called_once()
    assert manager.fsm.state is HotkeyState.IDLE
    manager.release_external_escape(first)
    assert manager.acquire_external_escape(second, callback).ok
    manager.release_external_escape(first)
    assert manager._external_escape_token is second
    manager.release_external_escape(second)


@pytest.mark.parametrize("action", ["ungrab", "replace", "mapping"])
def test_resource_loss_cancels_external_owner_and_handles_reentrant_release(action: str) -> None:
    backend = MouseRuntimeRig.keyboard_backend(65, 4)
    manager = HotkeyManager(backend)
    assert manager.grab("Ctrl+Space", HotkeyMode.PTT).ok
    token = object()
    events: list[str] = []

    def cancel() -> None:
        events.append("cancel")
        manager.release_external_escape(token)
        assert not manager.acquire_external_escape(object(), lambda: None).ok

    assert manager.acquire_external_escape(token, cancel).ok
    if action == "ungrab":
        manager.ungrab()
    elif action == "replace":
        manager.grab("Ctrl+Alt+Space", HotkeyMode.PTT)
    else:
        manager._handle_mapping(MappingEvent({}, GrabResult("busy")))
    assert events == ["cancel"]
    assert manager._external_escape_token is None


def test_foreign_keyboard_idle_does_not_remove_mouse_escape() -> None:
    backend = MouseRuntimeRig.keyboard_backend(65, 4)
    manager = HotkeyManager(backend)
    token = object()
    assert manager.acquire_external_escape(token, Mock()).ok
    manager._on_state(HotkeyState.IDLE, "foreign-cancel")
    backend.ungrab_escape.assert_not_called()
    manager.release_external_escape(token)


def test_mouse_pending_reserves_before_text_command_and_microphone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bench = MouseRuntimeRig(monkeypatch)
    bench.press()
    epoch = bench.runtime._input_epoch
    bench.runtime.hotkey.handle_event(HotkeyEvent("KeyPress", 65, 1, mods=4), bench.rig.now)
    assert bench.runtime.command_hotkey is not None
    bench.runtime.command_hotkey.fsm.press(bench.rig.now)
    assert bench.runtime._input_owner == "mouse" and bench.runtime._input_epoch == epoch
    assert bench.runtime._mouse_escape_token is not None
    assert not bench.runtime.start_test("", Mock())
    assert not bench.runtime.begin_hotkey_capture()
    assert not bench.sent("record.start")
    bench.rig.now += 0.31
    bench.fire_deadline()
    assert len(bench.sent("record.start")) == 1
    bench.runtime.shutdown()


@pytest.mark.parametrize("source", ["text", "keyboard"])
def test_keyboard_first_blocks_mouse_and_foreign_release(
    monkeypatch: pytest.MonkeyPatch, source: str
) -> None:
    bench = MouseRuntimeRig(monkeypatch)
    manager = bench.runtime.hotkey if source == "text" else bench.runtime.command_hotkey
    assert manager is not None
    manager.fsm.press(bench.rig.now)
    assert bench.runtime._input_owner == source
    assert bench.runtime.phase is DictationPhase.RECORDING
    bench.press()
    bench.release()
    assert bench.runtime.phase is DictationPhase.RECORDING
    assert len(bench.sent("record.start")) == 1
    assert not bench.runtime.begin_hotkey_capture()
    assert bench.runtime.begin_command_mouse_capture() is None
    bench.runtime.shutdown()


def test_stale_mouse_timer_cannot_start_new_gesture(monkeypatch: pytest.MonkeyPatch) -> None:
    bench = MouseRuntimeRig(monkeypatch)
    bench.press()
    old = cast(FakeTimer, bench.runtime._mouse_tick_timer)
    bench.escape()
    bench.backend.held = False
    bench.runtime.reload_command_mouse()
    bench.press()
    epoch = bench.runtime._input_epoch
    bench.rig.now += 1
    old.timeout.emit()
    assert bench.runtime._input_epoch == epoch
    assert bench.manager.state is MouseHoldState.PENDING
    assert not bench.sent("record.start")
    bench.runtime.shutdown()


def test_held_button_after_cancel_requires_release_and_new_gesture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bench = MouseRuntimeRig(monkeypatch)
    bench.press()
    bench.escape()
    bench.runtime.reload_command_mouse()
    assert bench.runtime.command_mouse_status == "unavailable"
    assert bench.backend.grabbed is None
    bench.backend.held = False
    timer = bench.runtime._mouse_retry_timer
    assert timer is not None
    cast(FakeTimer, timer).fire()
    assert bench.runtime.command_mouse_status == "ready"
    assert not bench.sent("record.start")
    bench.runtime.shutdown()


def test_text_manager_ungrab_cancels_mouse_before_losing_escape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bench = MouseRuntimeRig(monkeypatch)
    bench.record_mouse()
    bench.runtime.hotkey.ungrab()
    assert bench.manager.state is MouseHoldState.IDLE
    assert bench.sent("record.cancel")
    bench.client.submit.assert_not_called()
    bench.runtime.shutdown()


def test_pending_keyboard_prevents_capture_and_stale_hold_timer_is_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bench = MouseRuntimeRig(monkeypatch)
    manager = bench.runtime.command_hotkey
    assert manager is not None
    manager.handle_event(HotkeyEvent("KeyPress", 133, 1), bench.rig.now)
    old = bench.rig.timers[-1]
    assert manager.has_pending_press
    assert not bench.runtime.begin_hotkey_capture()
    assert bench.runtime.begin_command_mouse_capture() is None
    manager.handle_event(HotkeyEvent("KeyRelease", 133, 2), bench.rig.now + 0.1)
    bench.runtime.reload_command_hotkey()
    manager.handle_event(HotkeyEvent("KeyPress", 133, 3), bench.rig.now)
    bench.rig.now += 0.31
    old.timeout.emit()
    assert not bench.sent("record.start")
    bench.runtime.shutdown()


def test_stale_retry_cannot_clear_replacement_timer(monkeypatch: pytest.MonkeyPatch) -> None:
    bench = MouseRuntimeRig(monkeypatch)
    bench.backend.held = True
    bench.runtime.reload_command_mouse()
    old = cast(FakeTimer, bench.runtime._mouse_retry_timer)
    bench.runtime.reload_command_mouse()
    new = bench.runtime._mouse_retry_timer
    assert new is not None and new is not old
    old.timeout.emit()
    assert bench.runtime._mouse_retry_timer is new
    bench.runtime.shutdown()


def test_cancel_is_sent_before_external_escape_is_removed(monkeypatch: pytest.MonkeyPatch) -> None:
    bench = MouseRuntimeRig(monkeypatch)
    bench.record_mouse()
    events: list[str] = []
    bench.text_backend.ungrab_escape.side_effect = lambda: events.append("ungrab")
    bench.rig.supervisor.send.side_effect = lambda message, **kwargs: events.append(message["type"])
    bench.runtime.hotkey.ungrab()
    assert events.index("record.cancel") < events.index("ungrab")
    bench.runtime.shutdown()


def test_reopened_mouse_fd_replaces_only_its_notifier(monkeypatch: pytest.MonkeyPatch) -> None:
    bench = MouseRuntimeRig(monkeypatch)
    first = Mock(activated=Signal())
    second = Mock(activated=Signal())
    bench.rig.create_notifier.side_effect = [first, second]
    fd = [41]
    bench.backend.fileno = lambda: fd[0]  # type: ignore[method-assign]
    bench.runtime.reload_command_mouse()
    assert bench.runtime._mouse_notifier is first
    fd[0] = 42
    bench.runtime.reload_command_mouse()
    assert bench.runtime._mouse_notifier is second
    first.deleteLater.assert_called_once()
    assert len(second.activated.callbacks) == 1
    bench.runtime.shutdown()


def test_reopened_text_fd_keeps_one_escape_reader(monkeypatch: pytest.MonkeyPatch) -> None:
    bench = MouseRuntimeRig(monkeypatch)
    first = Mock(activated=Signal())
    second = Mock(activated=Signal())
    bench.rig.create_notifier.side_effect = [first, second]
    bench.text_backend.fileno.return_value = 41
    bench.press()
    assert bench.runtime.notifier is first
    bench.escape()
    bench.backend.held = False
    bench.runtime.reload_command_mouse()
    bench.text_backend.fileno.return_value = 42
    bench.press()
    assert bench.runtime.notifier is second
    first.deleteLater.assert_called_once()
    assert len(second.activated.callbacks) == 1
    bench.runtime.shutdown()
