"""ИБ-37/39: physical PTT release loss and terminal worker generation.

Use the real hotkey manager/orchestrator with fake external ports. Commands,
capture acknowledgements and manually delivered timers are separate boundaries.
"""

from __future__ import annotations

import json
import struct
from typing import Any
from unittest.mock import Mock, call

import pytest
from test_model_idle_unload import PhysicalHotkeyRig, load_model, make_rig, sent
from test_model_wake_hold import WakeRig, unloaded
from test_runtime import FakeTimer, Rig, assert_phase
from test_runtime import smoke_wav as smoke_wav  # noqa: F401
from test_t2_supervisor_runtime import supervisor_rig as supervisor_rig  # noqa: F401

from astra_voice.core.dictation import (
    CANCEL_RESTART_MS,
    CANCEL_TIMEOUT_MS,
    DictationPhase,
    MicrophoneLevelUpdate,
    MicrophoneTestUpdate,
)
from astra_voice.core.settings import from_dict
from astra_voice.platform.hotkey import GrabResult, HotkeyEvent, HotkeyMode, HotkeyState
from astra_voice.ui.pill import PillState
from astra_voice.worker import ipc

pytestmark = pytest.mark.unit


def ready_keyboard(monkeypatch: pytest.MonkeyPatch, *, mode: str = "ptt") -> WakeRig:
    rig = PhysicalHotkeyRig(
        monkeypatch,
        from_dict({"model_dir": "/tmp/model", "hotkey_mode": mode}),
    )
    rig.runtime.start()
    load_model(rig)
    batches: list[Any] = []
    rig.hotkey.backend.poll_events.side_effect = lambda timeout=0.0: (
        [] if timeout or not batches else batches.pop(0)
    )
    return WakeRig(rig, rig.hotkey, batches)


def recording_id(rig: Rig) -> str:
    starts = [
        c.args[0] for c in rig.supervisor.send.call_args_list if c.args[0]["type"] == "record.start"
    ]
    assert starts
    return str(starts[-1]["utterance_id"])


def active_scheduled(rig: Rig, delay: int | None = None) -> list[FakeTimer]:
    return [
        timer
        for timer in rig.timers
        if timer.name == "scheduled"
        and timer.active
        and not timer.deleted
        and (delay is None or timer.interval == delay)
    ]


def fire_delay(rig: Rig, delay: int) -> None:
    timers = active_scheduled(rig, delay)
    assert timers, f"Expected armed {delay} ms timer"
    for timer in timers:
        timer.fire()


def drain_scheduled(rig: Rig) -> None:
    """Deliver every outstanding one-shot, including any newly armed callbacks."""
    for _ in range(12):
        timers = active_scheduled(rig)
        if not timers:
            return
        rig.now += max(t.interval for t in timers) / 1000 + 1
        for timer in timers:
            timer.fire()
    pytest.fail("Scheduled callbacks never settled")


@pytest.mark.parametrize("start", ["ready-model", "wake-autostart"])
@pytest.mark.parametrize("regrab", ["new-keycode", "busy"])
def test_lost_physical_release_stops_text_ptt_after_capture_ack(
    monkeypatch: pytest.MonkeyPatch, start: str, regrab: str
) -> None:
    wake = unloaded(monkeypatch) if start == "wake-autostart" else ready_keyboard(monkeypatch)
    rig = wake.rig
    try:
        wake.key("KeyPress")
        if start == "wake-autostart":
            rig.now += 1
            wake.loaded()
            wake.ready()
        uid = recording_id(rig)
        assert wake.hotkey.physical_key_down
        assert_phase(rig.runtime, DictationPhase.RECORDING)
        rig.guard.set_recording.assert_called_with(True)
        rig.guard.set_recording.reset_mock()
        before = len(sent(rig))
        # Deliberately do not acknowledge record.stop from the fake send port.
        rig.supervisor.send.side_effect = lambda message, **kwargs: rig.record(message["type"])
        wake.mapped(
            GrabResult("ok", keycode=66, mods=4) if regrab == "new-keycode" else GrabResult("busy")
        )
        assert wake.hotkey.mapping_release_lost
        wake.key("KeyRelease", after=1, code=65)
        rig.fire_tail()
        assert sent(rig)[before:] == ["record.stop", "recognize"]
        assert wake.hotkey.fsm.state is not HotkeyState.RECORDING
        assert_phase(rig.runtime, DictationPhase.PROCESSING)
        assert call(False) not in rig.guard.set_recording.call_args_list
        rig.event(type="record.stopped", utterance_id=uid)
        rig.guard.set_recording.assert_called_with(False)
        rig.event(type="result", utterance_id=uid, text="PTT regression result")
        rig.paste.assert_called_once()
        assert not rig.runtime.orchestrator.cleanup_pending
        assert rig.runtime.phase is not DictationPhase.RECORDING
    finally:
        rig.runtime.shutdown()


@pytest.mark.parametrize("mode", ["ptt", "toggle"])
def test_same_keycode_regrab_preserves_recording_until_normal_stop(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    wake = ready_keyboard(monkeypatch, mode=mode)
    rig = wake.rig
    try:
        wake.key("KeyPress")
        wake.mapped(GrabResult("ok", keycode=65, mods=4))
        assert not wake.hotkey.mapping_release_lost
        assert rig.runtime.phase is DictationPhase.RECORDING
        assert "record.stop" not in sent(rig)
        wake.key("KeyRelease", after=1)
        if mode == "toggle":
            assert rig.runtime.phase is DictationPhase.RECORDING
            wake.key("KeyPress", after=1)
        rig.fire_tail()
        assert sent(rig).count("record.stop") == 1
        assert sent(rig).count("recognize") == 1
    finally:
        rig.runtime.shutdown()


def test_new_keycode_regrab_does_not_stop_configured_toggle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wake = ready_keyboard(monkeypatch, mode="toggle")
    rig = wake.rig
    try:
        wake.key("KeyPress")
        wake.mapped(GrabResult("ok", keycode=66, mods=4))
        assert not wake.hotkey.mapping_release_lost
        wake.key("KeyRelease", after=1)
        rig.fire_tail()
        assert rig.runtime.phase is DictationPhase.RECORDING
        assert wake.hotkey.fsm.mode is HotkeyMode.TOGGLE
        assert sent(rig).count("record.stop") == 0
        rig.now += 1
        wake.hotkey.handle_event(HotkeyEvent("KeyPress", 66, int(rig.now * 1000), mods=4), rig.now)
        rig.fire_tail()
        assert sent(rig).count("record.stop") == 1
    finally:
        rig.runtime.shutdown()


def stop_transport(rig: Rig) -> None:
    """Model supervisor's intentional stop after it has killed its process."""
    rig.supervisor.state = "stopped"
    rig.supervisor.send.side_effect = RuntimeError("Воркер не запущен.")


def assert_terminal(rig: Rig, factories: int) -> None:
    assert rig.supervisor_factory.call_count == factories
    assert rig.supervisor.state == "stopped"
    assert not rig.runtime.orchestrator.cleanup_pending
    assert rig.runtime.phase in (DictationPhase.IDLE, DictationPhase.FINISHING)
    assert rig.hotkey.fsm.state is not HotkeyState.RECORDING
    rig.guard.set_recording.assert_called_with(False)
    rig.pill.show_state.assert_called_with(PillState.ERROR, text=ipc.PROTOCOL_MISMATCH_MESSAGE)


@pytest.mark.parametrize("stage", ["recording", "cleanup-armed", "restart-armed"])
@pytest.mark.parametrize("first", ["utterance", "idless"])
def test_protocol_mismatch_is_terminal_even_with_preexisting_cleanup_timers(
    monkeypatch: pytest.MonkeyPatch, stage: str, first: str
) -> None:
    rig = make_rig(monkeypatch, minutes=0)
    try:
        rig.press(rig.now)
        uid = recording_id(rig)
        if stage != "recording":
            rig.runtime.orchestrator.cancel("test")
            assert rig.runtime.orchestrator.cleanup_pending
            assert active_scheduled(rig, CANCEL_TIMEOUT_MS)
        if stage == "restart-armed":
            fire_delay(rig, CANCEL_TIMEOUT_MS)
            assert "audio.close" in sent(rig)
            assert active_scheduled(rig, CANCEL_RESTART_MS)
        factories = rig.supervisor_factory.call_count
        stop_transport(rig)
        events = [{"utterance_id": uid, "request_type": "record.start"}, {}]
        if first == "idless":
            events.reverse()
        for fields in events:
            rig.event(type="error", code=ipc.PROTOCOL_MISMATCH, **fields)
        # No gesture is delivered while every armed callback passes its deadline.
        drain_scheduled(rig)
        assert_terminal(rig, factories)
        # A duplicate late failure cannot replace the persistent instruction.
        rig.event(type="error", code=ipc.PROTOCOL_MISMATCH, utterance_id=uid)
        assert_terminal(rig, factories)
    finally:
        rig.runtime.shutdown()


@pytest.mark.parametrize("address", ["first", "second", "unknown", "idless"])
def test_protocol_mismatch_ends_entire_overlapping_generation(
    monkeypatch: pytest.MonkeyPatch, address: str
) -> None:
    rig = make_rig(monkeypatch, minutes=0)
    try:
        rig.press(rig.now)
        first = recording_id(rig)
        rig.now += 1
        rig.release(rig.now)
        rig.now += 1
        rig.press(rig.now)
        second = recording_id(rig)
        assert first != second
        assert rig.runtime.phase is DictationPhase.RECORDING
        factories = rig.supervisor_factory.call_count
        stop_transport(rig)
        fields = (
            {}
            if address == "idless"
            else {
                "utterance_id": {"first": first, "second": second, "unknown": "absent-phrase"}[
                    address
                ]
            }
        )
        rig.event(type="error", code=ipc.PROTOCOL_MISMATCH, **fields)
        drain_scheduled(rig)
        assert_terminal(rig, factories)
        rig.paste.assert_not_called()
    finally:
        rig.runtime.shutdown()


@pytest.mark.parametrize("addressed", [False, True])
def test_old_generation_protocol_mismatch_does_not_end_current_recording(
    monkeypatch: pytest.MonkeyPatch, addressed: bool
) -> None:
    rig = make_rig(monkeypatch, minutes=0)
    try:
        old = rig.supervisor.generation
        rig.runtime.restart_worker()
        load_model(rig)
        rig.press(rig.now)
        uid = recording_id(rig)
        factories = rig.supervisor_factory.call_count
        before = list(sent(rig))
        rig.guard.set_recording.reset_mock()
        rig.pill.show_state.reset_mock()
        rig.event(
            type="error",
            code=ipc.PROTOCOL_MISMATCH,
            generation=old,
            **({"utterance_id": uid} if addressed else {}),
        )
        assert rig.runtime.phase is DictationPhase.RECORDING
        assert rig.hotkey.fsm.state is HotkeyState.RECORDING
        assert call(False) not in rig.guard.set_recording.call_args_list
        rig.pill.show_state.assert_not_called()
        assert sent(rig) == before
        assert rig.supervisor_factory.call_count == factories
    finally:
        rig.runtime.shutdown()


def test_nonterminal_capture_failure_still_escalates_to_worker_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = make_rig(monkeypatch, minutes=0)
    try:
        rig.press(rig.now)
        factories = rig.supervisor_factory.call_count
        rig.event(type="error", code="audio-failed", utterance_id=recording_id(rig))
        assert rig.runtime.orchestrator.cleanup_pending
        fire_delay(rig, CANCEL_TIMEOUT_MS)
        fire_delay(rig, CANCEL_RESTART_MS)
        assert rig.supervisor_factory.call_count == factories + 1
    finally:
        rig.runtime.shutdown()


@pytest.mark.parametrize("pending_switch", [False, True])
def test_terminal_mismatch_cancels_idle_unload_and_pending_model_switch(
    monkeypatch: pytest.MonkeyPatch, pending_switch: bool
) -> None:
    rig = make_rig(monkeypatch, minutes=5)
    try:
        assert active_scheduled(rig, 300_000)
        rig.press(rig.now)
        if pending_switch:
            rig.runtime.switch_model(min_ram_mb=100, pause=True)
            assert rig.runtime._pending_switch is not None
        factories = rig.supervisor_factory.call_count
        stop_transport(rig)
        rig.event(type="error", code=ipc.PROTOCOL_MISMATCH, utterance_id=recording_id(rig))
        # Time must pass the five-minute idle deadline as well as cleanup timers.
        rig.now += 301
        drain_scheduled(rig)
        assert_terminal(rig, factories)
    finally:
        rig.runtime.shutdown()


def test_terminal_mismatch_during_selfcheck_cannot_retry_after_watchdog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = PhysicalHotkeyRig(monkeypatch, from_dict({"model_dir": "/tmp/model"}))
    rig.runtime.start()
    try:
        rig.event(type="hello")
        rig.event(type="model.loaded")
        assert sent(rig)[-1] == "transcribe.file"
        assert active_scheduled(rig)
        factories = rig.supervisor_factory.call_count
        stop_transport(rig)
        rig.event(type="error", code=ipc.PROTOCOL_MISMATCH, utterance_id="file")
        drain_scheduled(rig)
        assert_terminal(rig, factories)
        assert not rig.runtime._loading_model
    finally:
        rig.runtime.shutdown()


@pytest.mark.parametrize("capture", ["level", "microphone-test"])
def test_terminal_mismatch_finishes_microphone_callback_without_reentrant_restart(
    monkeypatch: pytest.MonkeyPatch, capture: str
) -> None:
    rig = make_rig(monkeypatch, minutes=0)
    updates: list[MicrophoneLevelUpdate | MicrophoneTestUpdate] = []
    reentrant_updates: list[MicrophoneTestUpdate] = []
    try:

        def updated(update: MicrophoneLevelUpdate | MicrophoneTestUpdate) -> None:
            updates.append(update)
            if update.state == "error":
                # UI owners may try to retry while processing the final callback.
                rig.runtime.restart_worker()
                rig.hotkey.fsm.press(rig.now + 1)
                assert not rig.runtime.start_test("", reentrant_updates.append)

        if capture == "level":
            assert rig.runtime.start_level_monitor("", updated)
        else:
            assert rig.runtime.start_test("", updated)
        assert sent(rig)[-1] == "record.start"
        uid = recording_id(rig)
        factories = rig.supervisor_factory.call_count
        stop_transport(rig)
        rig.event(type="error", code=ipc.PROTOCOL_MISMATCH, utterance_id=uid)
        drain_scheduled(rig)
        assert_terminal(rig, factories)
        assert [u.state for u in updates].count("error") == 1
        assert updates[-1].message == ipc.PROTOCOL_MISMATCH_MESSAGE
        assert sent(rig).count("record.start") == 1
        assert not rig.runtime.orchestrator.test_active
        assert not rig.runtime.orchestrator.level_active
        assert rig.runtime._pending_test is None
        assert rig.runtime._preparing_timer is None
        assert all(update.state == "error" for update in reentrant_updates)
    finally:
        rig.runtime.shutdown()


def test_real_supervisor_protocol_mismatch_kills_process_and_never_restarts(
    supervisor_rig: Rig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real framing/correlation and kill/wait handling, with no child process."""
    rig = supervisor_rig
    runtime = rig.runtime
    supervisor = runtime.supervisor
    process = Mock()
    process.poll.return_value = None
    process.wait.side_effect = lambda **kwargs: setattr(process.poll, "return_value", -9)
    supervisor.process = process
    factory = Mock(wraps=runtime._supervisor_factory)
    monkeypatch.setattr(runtime, "_supervisor_factory", factory)
    rig.hotkey.fsm.press(rig.now)
    assert runtime.phase is DictationPhase.RECORDING
    bad_hello = ipc.make_hello()
    bad_hello["protocol"] = ipc.PROTOCOL_VERSION + 1
    # The normal encoder rejects bad protocol versions before they reach the wire.
    body = json.dumps(bad_hello).encode("utf-8")
    supervisor._receive(struct.pack("!I", len(body)) + body, supervisor.generation)
    process.kill.assert_called_once_with()
    process.wait.assert_any_call(timeout=0.2)
    assert supervisor.state == "stopped"
    drain_scheduled(rig)
    factory.assert_not_called()
    assert runtime.supervisor is supervisor
    assert not runtime.orchestrator.cleanup_pending
    assert runtime.phase in (DictationPhase.IDLE, DictationPhase.FINISHING)
    rig.guard.set_recording.assert_called_with(False)
    rig.pill.show_state.assert_called_with(PillState.ERROR, text=ipc.PROTOCOL_MISMATCH_MESSAGE)


def test_terminal_generation_discards_late_events_until_explicit_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = make_rig(monkeypatch, minutes=0)
    try:
        rig.press(rig.now)
        uid = recording_id(rig)
        factories = rig.supervisor_factory.call_count
        stop_transport(rig)
        rig.event(type="error", code=ipc.PROTOCOL_MISMATCH, utterance_id=uid)
        before = list(sent(rig))
        for event in (
            {"type": "hello"},
            {"type": "model.loaded"},
            {"type": "result", "utterance_id": "file", "text": "проверка"},
            {"type": "record.stopped", "utterance_id": uid},
            {"type": "result", "utterance_id": uid, "text": "late result"},
        ):
            rig.event(**event)
            drain_scheduled(rig)
            assert_terminal(rig, factories)
        assert sent(rig) == before
        rig.paste.assert_not_called()
        # A later explicit restart is a new generation, not a cleanup retry.
        generation = rig.supervisor.generation
        rig.supervisor.send.side_effect = rig.send
        rig.runtime.restart_worker()
        assert rig.supervisor_factory.call_count == factories + 1
        assert rig.supervisor.generation > generation
        load_model(rig)
        rig.release(rig.now + 1)
        rig.now += 2
        rig.press(rig.now)
        assert_phase(rig.runtime, DictationPhase.RECORDING)
        rig.guard.set_recording.assert_called_with(True)
    finally:
        rig.runtime.shutdown()
