"""ИБ-47/48/50: terminal runtime, real input managers and fake hardware ports."""

from __future__ import annotations

from unittest.mock import call

import pytest
from test_command_mouse_runtime import MouseRuntimeRig
from test_runtime import Rig
from test_runtime import smoke_wav as smoke_wav  # noqa: F401
from test_runtime_t2b_fixes import ready_keyboard, stop_transport

from astra_voice.core.dictation import DictationPhase
from astra_voice.platform.hotkey import GrabResult, HotkeyEvent, HotkeyMode, HotkeyState
from astra_voice.platform.mouse_button import MouseHoldState
from astra_voice.ui.pill import PillState
from astra_voice.ui.tray_icons import TrayState
from astra_voice.worker import ipc

pytestmark = pytest.mark.unit


def mismatch(rig: Rig) -> int:
    factories = rig.supervisor_factory.call_count
    stop_transport(rig)
    rig.event(type="error", code=ipc.PROTOCOL_MISMATCH)
    return factories


def assert_rejected(rig: Rig, factories: int) -> None:
    assert rig.supervisor_factory.call_count == factories
    assert rig.supervisor.state == "stopped"
    assert rig.runtime._terminated_generation == rig.supervisor.generation
    assert rig.runtime.phase in (DictationPhase.IDLE, DictationPhase.FINISHING)
    assert not any(c.args[0]["type"] == "record.start" for c in rig.supervisor.send.call_args_list)
    rig.pill.show_state.assert_called_with(PillState.ERROR, text=ipc.PROTOCOL_MISMATCH_MESSAGE)
    assert rig.tray.set_state.call_args_list
    assert all(c == call(TrayState.ERROR) for c in rig.tray.set_state.call_args_list)


@pytest.mark.parametrize("parked", [False, True])
@pytest.mark.parametrize("gesture", ["text", "command", "mouse"])
def test_every_terminal_gesture_repeats_reason_without_new_worker(
    monkeypatch: pytest.MonkeyPatch, parked: bool, gesture: str
) -> None:
    mouse = MouseRuntimeRig(monkeypatch)
    rig, runtime = mouse.rig, mouse.runtime
    runtime._model_parked = parked
    try:
        factories = mismatch(rig)
        for _ in range(2):
            if runtime._mouse_retry_timer is not None:
                runtime._mouse_retry_timer.fire()
            rig.pill.show_state.reset_mock()
            rig.tray.set_state.reset_mock()
            if gesture == "text":
                runtime.hotkey.handle_event(HotkeyEvent("KeyPress", 65, 1, mods=4), rig.now)
                runtime.hotkey.handle_event(HotkeyEvent("KeyRelease", 65, 2, mods=4), rig.now + 1)
            elif gesture == "command":
                assert runtime.command_hotkey is not None
                runtime.command_hotkey.handle_event(HotkeyEvent("KeyPress", 133, 1), rig.now)
                rig.now += 0.31
                runtime._tick_hotkey()
                runtime.command_hotkey.handle_event(HotkeyEvent("KeyRelease", 133, 2), rig.now)
            else:
                mouse.press()
                mouse.release()
            assert_rejected(rig, factories)
            assert not runtime._loading_model
            assert runtime._model_parked is parked
            assert runtime.hotkey.fsm.state is HotkeyState.IDLE
            assert runtime.command_hotkey is not None
            assert runtime.command_hotkey.fsm.state is HotkeyState.IDLE
            assert mouse.manager.state is MouseHoldState.IDLE
            rig.now += 1
    finally:
        runtime.shutdown()


@pytest.mark.parametrize("gesture", ["text", "command", "mouse"])
def test_mismatch_discards_preexisting_deferred_gestures(
    monkeypatch: pytest.MonkeyPatch, gesture: str
) -> None:
    mouse = MouseRuntimeRig(monkeypatch)
    rig, runtime = mouse.rig, mouse.runtime
    runtime._model_parked = True
    try:
        if gesture == "mouse":
            # Pending mouse hold was admitted before terminality.
            runtime._model_parked = False
            mouse.press()
            runtime._model_parked = True
            pending_generation = mouse.manager.generation
            timer = runtime._mouse_tick_timer
            assert timer is not None
            late_callbacks = list(timer.timeout.callbacks)
        else:
            manager = runtime.hotkey if gesture == "text" else runtime.command_hotkey
            assert manager is not None
            if gesture == "text":
                runtime.settings.hotkey = "Super_R"
                manager.defer_single_super = True
                manager.grab("Super_R", HotkeyMode.PTT)
            manager.handle_event(
                HotkeyEvent(
                    "KeyPress",
                    65 if gesture == "text" else 133,
                    1,
                    mods=4 if gesture == "text" else 0,
                ),
                rig.now,
            )
            assert manager.has_pending_press
            late_callbacks = []
        factories = mismatch(rig)
        rig.now += 0.31
        if gesture != "mouse":
            assert manager is not None
            assert not manager.has_pending_press
        runtime._tick_hotkey()
        for callback in late_callbacks:
            callback()
        if gesture == "mouse":
            rig.pill.show_state.reset_mock()
            runtime._mouse_pending(pending_generation)
        # The original mismatch or the rejected queued callback keeps the reason.
        assert_rejected(rig, factories)
        assert mouse.manager.deadline is None
        assert runtime._wake_generation is None
    finally:
        runtime.shutdown()


@pytest.mark.parametrize("restore", ["mapping", "retry", "apply"])
def test_terminal_mapping_failure_arms_retry_and_restore_keeps_error(
    monkeypatch: pytest.MonkeyPatch, restore: str
) -> None:
    wake = ready_keyboard(monkeypatch)
    rig, runtime = wake.rig, wake.rig.runtime
    try:
        factories = mismatch(rig)
        rig.tray.set_state.reset_mock()
        if restore == "retry":
            wake.mapped(GrabResult("busy"))
        else:
            runtime._on_hotkey_state(HotkeyState.IDLE, "mapping-regrab:busy")
        timer = runtime._regrab_timer
        assert timer is not None and timer.active
        assert_rejected(rig, factories)
        if restore == "mapping":
            runtime._on_hotkey_state(HotkeyState.IDLE, "mapping-regrab:ok")
        elif restore == "apply":
            assert (
                runtime.apply_hotkey(runtime.settings.hotkey, runtime.settings.hotkey_mode) == "ok"
            )
        else:
            rig.hotkey.backend.grab_combo.return_value = GrabResult("ok", keycode=65, mods=4)
            timer.fire()
        assert runtime._regrab_timer is None
        assert_rejected(rig, factories)
    finally:
        runtime.shutdown()


def test_terminal_pending_rearm_failure_keeps_retry_until_key_released(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mouse = MouseRuntimeRig(monkeypatch)
    rig, runtime = mouse.rig, mouse.runtime
    manager = runtime.command_hotkey
    assert manager is not None
    try:
        manager.handle_event(HotkeyEvent("KeyPress", 133, 1), rig.now)
        assert manager.has_pending_press
        monkeypatch.setattr(mouse.command_backend, "key_is_down", lambda code: True, raising=False)
        factories = mismatch(rig)
        assert not manager.has_pending_press
        assert runtime._command_regrab_timer is not None
        assert_rejected(rig, factories)
        monkeypatch.setattr(mouse.command_backend, "key_is_down", lambda code: False)
        runtime._command_regrab_timer.fire()
        assert runtime._command_regrab_timer is None
        assert_rejected(rig, factories)
    finally:
        runtime.shutdown()


def test_terminal_wake_is_blocked_until_explicit_restart(monkeypatch: pytest.MonkeyPatch) -> None:
    mouse = MouseRuntimeRig(monkeypatch)
    rig, runtime = mouse.rig, mouse.runtime
    runtime._model_parked = True
    try:
        factories = mismatch(rig)
        assert not runtime._wake_model()
        assert not runtime._wake_model(gesture=False)
        assert_rejected(rig, factories)
        generation = rig.supervisor.generation
        rig.supervisor.send.side_effect = rig.send
        runtime.restart_worker()
        assert runtime._terminated_generation is None
        assert rig.supervisor_factory.call_count == factories + 1
        assert rig.supervisor.generation > generation
    finally:
        runtime.shutdown()
