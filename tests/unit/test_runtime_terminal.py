"""ИБ-47/48/50: terminal runtime, real input managers and fake hardware ports."""

from __future__ import annotations

import json
import struct
import subprocess
from typing import Any
from unittest.mock import Mock, call

import pytest
from test_command_mouse_runtime import MouseRuntimeRig
from test_runtime import Rig, SwitchRig
from test_runtime import smoke_wav as smoke_wav  # noqa: F401
from test_runtime_t2b_fixes import ready_keyboard, stop_transport

from astra_voice.core.dictation import CANCEL_TIMEOUT_MS, DictationPhase
from astra_voice.platform.hotkey import GrabResult, HotkeyEvent, HotkeyMode, HotkeyState
from astra_voice.platform.mouse_button import MouseHoldState
from astra_voice.ui.pill import ERROR_MODEL_NOT_LOADED, STATE_DURATION_MS, PillState
from astra_voice.ui.tray_icons import TrayState
from astra_voice.worker import ipc
from astra_voice.worker.supervisor import WorkerSupervisor

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


@pytest.mark.parametrize("restart", ["direct", "tray"])
def test_terminal_wake_is_blocked_until_explicit_restart(
    monkeypatch: pytest.MonkeyPatch, restart: str
) -> None:
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
        rig.tray.set_model_recheck_enabled.assert_called_with(True)
        if restart == "direct":
            runtime.restart_worker()
        else:
            rig.tray.on_model_recheck()
        assert runtime._terminated_generation is None
        assert rig.supervisor_factory.call_count == factories + 1
        assert rig.supervisor.generation > generation
        if restart == "tray":
            assert runtime._loading_model
            rig.supervisor.send.assert_not_called()
        runtime._model_parked = False
        runtime._selfcheck = "ok"
        runtime._model_ready()
        runtime.hotkey.handle_event(HotkeyEvent("KeyPress", 65, 1, mods=4), rig.now)
        assert any(c.args[0]["type"] == "record.start" for c in rig.supervisor.send.call_args_list)
    finally:
        runtime.shutdown()


@pytest.mark.parametrize("parked", [False, True])
@pytest.mark.parametrize("action", ["start_test", "reload_model", "switch_model"])
def test_terminal_model_actions_repeat_reason_without_new_worker(
    monkeypatch: pytest.MonkeyPatch, parked: bool, action: str
) -> None:
    mouse = MouseRuntimeRig(monkeypatch)
    rig, runtime = mouse.rig, mouse.runtime
    runtime._model_parked = parked
    callback = Mock()
    runtime.on_switch_finished = callback
    resolve = Mock(side_effect=AssertionError("Terminal generation must not resolve models"))
    monkeypatch.setattr(runtime, "_resolve_model_request", resolve)
    try:
        factories = mismatch(rig)
        requests = list(rig.supervisor.send.call_args_list)
        for _ in range(2):
            callback.reset_mock()
            rig.pill.show_state.reset_mock()
            rig.tray.set_state.reset_mock()
            if action == "start_test":
                assert not runtime.start_test("test-mic", callback)
                callback.assert_called_once()
                update = callback.call_args.args[0]
                assert update.state == "error"
                assert update.message == ipc.PROTOCOL_MISMATCH_MESSAGE
            elif action == "reload_model":
                runtime.reload_model()
            else:
                runtime.switch_model(min_ram_mb=1, pause=False)
                callback.assert_called_once_with("failed")
            assert_rejected(rig, factories)
            assert rig.supervisor.send.call_args_list == requests
            resolve.assert_not_called()
            assert runtime._pending_test is None
            assert runtime._pending_switch is None
            assert not runtime._switch_active()
            assert not runtime._loading_model
            assert runtime._model_parked is parked
    finally:
        runtime.shutdown()


def test_protocol3_hello_terminates_new_worker_in_protocol4_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert ipc.PROTOCOL_VERSION == 4
    monkeypatch.setattr(
        WorkerSupervisor, "_launch", lambda supervisor: setattr(supervisor, "state", "running")
    )
    supervisors: list[WorkerSupervisor] = []

    def factory(**kwargs: Any) -> WorkerSupervisor:
        supervisor = WorkerSupervisor(on_event=kwargs["on_event"], use_qt=False)
        supervisors.append(supervisor)
        return supervisor

    rig = Rig(monkeypatch, supervisor_factory=factory)
    runtime = rig.runtime
    runtime.start()
    try:
        runtime.restart_worker(wait_for_model=True)
        supervisor = runtime.supervisor
        generation = supervisor.generation
        send = Mock(wraps=supervisor.send)
        monkeypatch.setattr(supervisor, "send", send)
        # Decode an old hello frame instead of injecting a pre-made mismatch event.
        hello = {**ipc.make_hello(), "protocol": 3}
        payload = json.dumps(hello).encode("utf-8")
        supervisor._receive(struct.pack(">I", len(payload)) + payload, generation)
        assert supervisor.state == "stopped"
        assert runtime._terminated_generation == generation
        assert runtime.phase == DictationPhase.IDLE
        rig.pill.show_state.assert_called_with(PillState.ERROR, text=ipc.PROTOCOL_MISMATCH_MESSAGE)
        rig.tray.set_state.assert_called_with(TrayState.ERROR)
        rig.tray.set_state.reset_mock()
        for offset in (0, 2):
            rig.hotkey.fsm.press(rig.now + offset)
            rig.release(rig.now + offset + 1)
            runtime.reload_model()
            runtime.switch_model(min_ram_mb=1, pause=False)
            assert not runtime.start_test("test-mic", Mock())
        # A late compatible hello cannot revive the terminated process.
        supervisor._receive(ipc.encode(ipc.make_hello()), generation)
        runtime._on_worker_event({"type": "hello", "generation": generation})
        runtime._on_worker_event({"type": "model.loaded", "generation": generation})
        assert len(supervisors) == 2
        assert supervisor.state == "stopped"
        assert runtime._terminated_generation == generation
        send.assert_not_called()
        assert not supervisor._outgoing
        assert rig.tray.set_state.call_args_list
        assert all(c == call(TrayState.ERROR) for c in rig.tray.set_state.call_args_list)
        rig.pill.show_state.assert_called_with(PillState.ERROR, text=ipc.PROTOCOL_MISMATCH_MESSAGE)
    finally:
        runtime.shutdown()


@pytest.mark.parametrize("loaded_at", ["recording", "idle-cleanup"])
def test_candidate_waits_for_capture_cleanup_after_error_tail_then_promotes(
    monkeypatch: pytest.MonkeyPatch,
    loaded_at: str,
) -> None:
    bench = SwitchRig(monkeypatch)
    rig, runtime = bench.rig, bench.rig.runtime
    old = runtime.supervisor
    process = Mock()
    process.poll.return_value = None
    process.wait.side_effect = subprocess.TimeoutExpired("worker", 2)
    transport = WorkerSupervisor(lambda event: None, use_qt=False)
    transport.state = "running"
    transport.process = process
    old.stop.side_effect = transport.stop
    try:
        bench.switch()
        candidate = bench.workers[-1]
        SwitchRig.emit(candidate, type="hello")
        rig.hotkey.fsm.press(rig.now)
        uid = old.send.call_args.args[0]["utterance_id"]
        assert runtime.phase == DictationPhase.RECORDING
        if loaded_at == "recording":
            SwitchRig.emit(candidate, type="model.loaded", id="whisper")
        assert runtime.supervisor is old
        rig.job_error(uid, code="audio-failed")
        next(t for t in rig.timers if t.active and t.interval == CANCEL_TIMEOUT_MS).fire()
        next(
            t for t in rig.timers if t.active and t.interval == STATE_DURATION_MS[PillState.ERROR]
        ).fire()
        if loaded_at == "idle-cleanup":
            SwitchRig.emit(candidate, type="model.loaded", id="whisper")
        assert runtime.supervisor is old
        assert runtime.phase == DictationPhase.IDLE
        assert old.generation < candidate.generation
        assert runtime.orchestrator.cleanup_pending
        assert runtime.orchestrator._capture_open
        rig.guard.set_recording.assert_called_with(True)
        old.stop.assert_not_called()
        assert process.poll() is None
        # The old process has now exited; its own cancellation ACK is delivered
        # after the ERROR tail already consumed the transition to IDLE.
        process.poll.return_value = 0
        process.wait.side_effect = None
        rig.event(type="cancelled", utterance_id=uid, request_type="record.cancel")
        assert not runtime.orchestrator.cleanup_pending
        assert not runtime.orchestrator._capture_open
        assert runtime.supervisor is candidate
        old.stop.assert_called_once_with()
        assert runtime._loading_model
        bench.finished.assert_not_called()
    finally:
        runtime.shutdown()


@pytest.mark.parametrize("requested_at", ["recording", "idle-cleanup"])
def test_pending_switch_waits_for_microphone_cleanup_then_starts(
    monkeypatch: pytest.MonkeyPatch,
    requested_at: str,
) -> None:
    bench = SwitchRig(monkeypatch)
    rig, runtime = bench.rig, bench.rig.runtime
    old = runtime.supervisor
    try:
        assert runtime.start_test("test-mic", Mock())
        uid = old.send.call_args.args[0]["utterance_id"]
        if requested_at == "recording":
            bench.switch()
            assert runtime._pending_switch is not None
        rig.job_error(uid, code="audio-failed")
        assert runtime.phase == DictationPhase.IDLE
        assert runtime.orchestrator.cleanup_pending
        if requested_at == "idle-cleanup":
            bench.switch()
        assert runtime._pending_switch is not None
        assert not bench.workers
        rig.guard.set_recording.assert_called_with(True)
        old.stop.assert_not_called()
        rig.event(type="cancelled", utterance_id=uid, request_type="record.cancel")
        assert not runtime.orchestrator.cleanup_pending
        assert runtime._pending_switch is None
        assert len(bench.workers) == 1
        candidate = bench.workers[-1]
        SwitchRig.emit(candidate, type="hello")
        SwitchRig.emit(candidate, type="model.loaded", id="whisper")
        assert runtime.supervisor is candidate
        old.stop.assert_called_once_with()
    finally:
        runtime.shutdown()


def test_candidate_is_not_promoted_when_old_process_stop_times_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bench = SwitchRig(monkeypatch)
    runtime = bench.rig.runtime
    old = runtime.supervisor
    process = Mock()
    process.poll.return_value = None
    process.wait.side_effect = subprocess.TimeoutExpired("worker", 2)
    transport = WorkerSupervisor(lambda event: None, use_qt=False)
    transport.state = "running"
    transport.process = process
    old.stop.side_effect = transport.stop
    try:
        bench.switch()
        candidate = bench.workers[-1]
        SwitchRig.emit(candidate, type="hello")
        SwitchRig.emit(candidate, type="model.loaded", id="whisper")
        assert process.poll() is None
        assert process.wait.call_count == 2
        process.kill.assert_called_once_with()
        assert runtime.supervisor is old
        assert not runtime._switch_active()
        candidate.stop.assert_called_once_with()
        bench.finished.assert_called_once_with("failed")
        assert runtime._selfcheck == "failed"
        bench.rig.tray.set_state.assert_called_with(TrayState.ERROR)
        bench.rig.tray.set_model_recheck_enabled.assert_called_with(True)
        old.send.reset_mock()
        bench.rig.hotkey.fsm.press(bench.rig.now)
        assert not any(c.args[0]["type"] == "record.start" for c in old.send.call_args_list)
        bench.rig.pill.show_state.assert_called_with(PillState.ERROR, text=ERROR_MODEL_NOT_LOADED)
        bench.rig.hotkey.fsm.escape(bench.rig.now)
        # После подтверждённого выхода старого процесса доступен явный повтор.
        process.poll.return_value = -9
        process.wait.side_effect = None
        runtime._recheck_model()
        restored = bench.workers[-1]
        assert restored is not candidate
        assert runtime.supervisor is restored
        restored.start.assert_called_once_with()
        SwitchRig.emit(restored, type="hello")
        assert bench.loads(restored) == ["gigaam"]
        bench.finished.assert_called_once_with("failed")
    finally:
        runtime.shutdown()


@pytest.mark.parametrize("blocked", ["open", "closed", "reserved", "terminating"])
def test_restart_worker_refuses_pending_supervisor_restart_without_stopping_it(
    monkeypatch: pytest.MonkeyPatch,
    blocked: str,
) -> None:
    rig = Rig(monkeypatch)
    runtime = rig.runtime
    runtime.start()
    rig.supervisor.restarting = True
    factories = rig.supervisor_factory.call_count
    generation = rig.supervisor.generation
    runtime._model_parked = True
    runtime._closed = blocked == "closed"
    runtime._appimage_removal_reserved = blocked == "reserved"
    runtime._terminating_worker = blocked == "terminating"
    runtime._terminated_generation = generation
    before = vars(runtime).copy()
    callbacks = ("_drop_wake", "_stop_mouse", "_invalidate_mouse_capture", "_reset_selfcheck")
    operations = [Mock() for _ in callbacks]
    for name, operation in zip(callbacks, operations, strict=True):
        monkeypatch.setattr(runtime, name, operation)
    try:
        with pytest.raises(RuntimeError, match="перезапускается"):
            runtime.restart_worker(wait_for_model=True)
        rig.supervisor.stop.assert_not_called()
        assert runtime.supervisor is rig.supervisor
        assert rig.supervisor.generation == generation
        assert rig.supervisor_factory.call_count == factories
        assert runtime._model_parked
        assert all(vars(runtime)[name] == value for name, value in before.items())
        for operation in operations:
            operation.assert_not_called()
    finally:
        rig.supervisor.restarting = False
        runtime._closed = False
        runtime._appimage_removal_reserved = False
        runtime._terminating_worker = False
        runtime.shutdown()


def test_level_monitor_reports_pending_worker_restart_without_opening_microphone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = Rig(monkeypatch)
    runtime = rig.runtime
    runtime.start()
    rig.supervisor.restarting = True
    callback = Mock()
    rig.supervisor.send.reset_mock()
    try:
        assert not runtime.start_level_monitor("fake-mic", callback)
        callback.assert_called_once()
        update = callback.call_args.args[0]
        assert update.state == "error" and "перезапускается" in update.message
        assert not runtime.orchestrator.level_active
        rig.supervisor.send.assert_not_called()
        rig.supervisor.stop.assert_not_called()
    finally:
        rig.supervisor.restarting = False
        runtime.shutdown()


def restart_transport(
    monkeypatch: pytest.MonkeyPatch, rig: Rig, *, hello: bool = True
) -> WorkerSupervisor:
    """Настоящий супервизор с ручными IPC/процессом, без создания subprocess."""
    runtime = rig.runtime
    supervisor = WorkerSupervisor(
        runtime._on_worker_event,
        use_qt=False,
        clock=lambda: rig.now,
        launch_allowed=lambda: not runtime._closed and not runtime._appimage_removal_reserved,
    )
    supervisor.state = "running"
    runtime.supervisor = supervisor
    monkeypatch.setattr(supervisor, "_launch", Mock())
    if hello:
        supervisor._receive(ipc.encode(ipc.make_hello()), supervisor.generation)
    return supervisor


@pytest.mark.parametrize("level", [False, True], ids=["dictation", "level"])
def test_protocol_mismatch_keeps_runtime_capture_guard_until_process_exit(
    monkeypatch: pytest.MonkeyPatch, level: bool
) -> None:
    rig = Rig(monkeypatch)
    runtime = rig.runtime
    runtime.start()
    supervisor = restart_transport(monkeypatch, rig)
    process = Mock(poll=Mock(return_value=None))
    process.wait.side_effect = subprocess.TimeoutExpired("worker", 0.2)
    supervisor.process = process
    send = Mock(wraps=supervisor.send)
    monkeypatch.setattr(supervisor, "send", send)
    try:
        if level:
            assert runtime.start_level_monitor("fake-mic", Mock())
        else:
            rig.hotkey.fsm.press(rig.now)
        rig.guard.set_recording.assert_called_with(True)
        before = rig.guard.set_recording.call_count
        payload = json.dumps({**ipc.make_hello(), "protocol": 3}).encode()
        supervisor._receive(struct.pack(">I", len(payload)) + payload, supervisor.generation)
        for _ in range(3):
            supervisor.pump()
            assert supervisor.state == "stopped" and supervisor.restarting
            assert supervisor.generation == 1
            assert rig.guard.set_recording.call_count == before
            rig.guard.set_recording.assert_called_with(True)
            assert runtime._terminated_generation is None
            supervisor._launch.assert_not_called()
        process.poll.return_value = -9
        process.wait.side_effect = None
        supervisor.pump()
        assert not supervisor.restarting and supervisor.generation == 1
        assert runtime._terminated_generation == 1
        assert not runtime.orchestrator.cleanup_pending
        rig.guard.set_recording.assert_called_with(False)
        rig.pill.show_state.assert_called_with(PillState.ERROR, text=ipc.PROTOCOL_MISMATCH_MESSAGE)
        send.reset_mock()
        rig.hotkey.fsm.press(rig.now + 1)
        rig.pill.show_state.assert_called_with(PillState.ERROR, text=ipc.PROTOCOL_MISMATCH_MESSAGE)
        assert not any(c.args[0]["type"] == "record.start" for c in send.call_args_list)
        supervisor._launch.assert_not_called()
    finally:
        process.poll.return_value = -9
        process.wait.side_effect = None
        runtime.shutdown()


def test_level_monitor_before_worker_hello_keeps_indicator_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = Rig(monkeypatch)
    runtime = rig.runtime
    runtime.start()
    supervisor = restart_transport(monkeypatch, rig, hello=False)
    callback = Mock()
    rig.guard.set_recording.reset_mock()
    send = Mock(wraps=supervisor.send)
    monkeypatch.setattr(supervisor, "send", send)
    try:
        assert not runtime.start_level_monitor("fake-mic", callback)
        callback.assert_called_once()
        assert callback.call_args.args[0].state == "error"
        assert "не готов" in callback.call_args.args[0].message
        assert not runtime.orchestrator.level_active
        assert not supervisor._outgoing and not supervisor._pending
        rig.guard.set_recording.assert_not_called()
        send.assert_not_called()
        supervisor._receive(ipc.encode(ipc.make_hello()), supervisor.generation)
        assert runtime.start_level_monitor("fake-mic", Mock())
        rig.guard.set_recording.assert_called_with(True)
        assert send.call_args.args[0]["type"] == "record.start"
    finally:
        runtime.shutdown()


@pytest.mark.parametrize("pending", ["empty", "selfcheck-file"])
def test_pending_switch_resumes_after_unreported_restart_exit(
    monkeypatch: pytest.MonkeyPatch, pending: str
) -> None:
    bench = SwitchRig(monkeypatch)
    rig, runtime = bench.rig, bench.rig.runtime
    old = restart_transport(monkeypatch, rig)
    process = Mock(poll=Mock(return_value=None))
    process.wait.side_effect = subprocess.TimeoutExpired("worker", 0.2)
    old.process = process
    if pending == "selfcheck-file":
        old.send({"type": "transcribe.file", "path": "/tmp/selfcheck.wav"})
    old._restart(report=False)
    try:
        bench.switch()
        assert runtime._pending_switch is not None and not bench.workers
        for _ in range(3):
            runtime._run_pending_switch()
            old.pump()
            assert runtime._pending_switch is not None and not bench.workers
        process.poll.return_value = -9
        process.wait.side_effect = None
        old.pump()
        assert runtime._pending_switch is None
        assert len(bench.workers) == 1
        assert runtime._switch_active()
        candidate = bench.workers[-1]
        SwitchRig.emit(candidate, type="hello")
        assert bench.loads(candidate) == ["whisper"]
    finally:
        process.poll.return_value = -9
        process.wait.side_effect = None
        runtime.shutdown()


def test_candidate_promotion_waits_for_restarting_supervisor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bench = SwitchRig(monkeypatch)
    runtime = bench.rig.runtime
    old = runtime.supervisor
    try:
        bench.switch()
        candidate = bench.workers[-1]
        SwitchRig.emit(candidate, type="hello")
        old.restarting = True
        SwitchRig.emit(candidate, type="model.loaded", id="whisper")
        for _ in range(3):
            runtime._promote_candidate()
            runtime._run_pending_switch()
            assert runtime.supervisor is old and runtime._switch_candidate is candidate
            old.stop.assert_not_called()
            bench.finished.assert_not_called()
        old.restarting = False
        runtime._on_worker_event({"type": "error", "code": "worker-crashed", "generation": 0})
        assert runtime.supervisor is candidate
        old.stop.assert_called_once_with()
    finally:
        old.restarting = False
        runtime.shutdown()


@pytest.mark.parametrize("blocked", ["closed", "reserved"])
def test_deferred_worker_launch_is_forbidden_by_runtime_lifecycle(
    monkeypatch: pytest.MonkeyPatch, blocked: str
) -> None:
    seen: list[WorkerSupervisor] = []

    def factory(**kwargs: Any) -> WorkerSupervisor:
        supervisor = WorkerSupervisor(**{**kwargs, "use_qt": False})
        seen.append(supervisor)
        return supervisor

    monkeypatch.setattr(
        WorkerSupervisor, "_launch", lambda supervisor: setattr(supervisor, "state", "running")
    )
    rig = Rig(monkeypatch, supervisor_factory=factory)
    runtime = rig.runtime
    runtime.start()
    supervisor = runtime.supervisor
    process = Mock(poll=Mock(return_value=None))
    process.wait.side_effect = subprocess.TimeoutExpired("worker", 0.2)
    supervisor.process = process
    supervisor._restart(report=False)
    launch = Mock()
    monkeypatch.setattr(supervisor, "_launch", launch)
    runtime._closed = blocked == "closed"
    runtime._appimage_removal_reserved = blocked == "reserved"
    try:
        process.poll.return_value = -9
        process.wait.side_effect = None
        supervisor.pump()
        assert supervisor.state == "stopped" and supervisor.generation == 1
        assert len(seen) == 1
        launch.assert_not_called()
    finally:
        runtime._closed = False
        runtime._appimage_removal_reserved = False
        runtime.shutdown()


def test_candidate_stop_failure_after_close_does_not_publish_load_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bench = SwitchRig(monkeypatch)
    rig, runtime = bench.rig, bench.rig.runtime
    old = runtime.supervisor

    def closed_during_stop() -> None:
        runtime._closed = True
        raise subprocess.TimeoutExpired("worker", 2)

    old.stop.side_effect = closed_during_stop
    try:
        bench.switch()
        candidate = bench.workers[-1]
        SwitchRig.emit(candidate, type="hello")
        rig.pill.show_state.reset_mock()
        rig.tray.set_state.reset_mock()
        rig.tray.set_model_recheck_enabled.reset_mock()
        SwitchRig.emit(candidate, type="model.loaded", id="whisper")
        assert runtime._closed and not runtime._switch_active()
        assert runtime.supervisor is old
        rig.pill.show_state.assert_not_called()
        rig.tray.set_state.assert_not_called()
        rig.tray.set_model_recheck_enabled.assert_not_called()
    finally:
        old.stop.side_effect = None
        runtime._closed = False
        runtime.shutdown()
