"""Mapping loss cannot leave command capture running; all hardware ports are fake."""

from __future__ import annotations

from typing import cast
from unittest.mock import Mock

import pytest
from PyQt5.QtCore import QTimer
from test_runtime import FakeTimer, Rig

from astra_voice.core.command_mode import CommandMode, SessionSnapshot
from astra_voice.core.dictation import DictationPhase
from astra_voice.platform.cowork import DeliveryResult
from astra_voice.platform.hotkey import (
    GrabResult,
    HotkeyBackend,
    HotkeyEvent,
    HotkeyManager,
    HotkeyMode,
    HotkeyState,
    MappingEvent,
)
from astra_voice.platform.session import SessionKind

pytestmark = pytest.mark.unit


class CommandRig:
    def __init__(self, monkeypatch: pytest.MonkeyPatch, mode: HotkeyMode = HotkeyMode.PTT) -> None:
        self.rig = Rig(monkeypatch, session_kind=SessionKind.KDE)
        self.runtime = self.rig.runtime
        self.snapshot = SessionSnapshot(known=True, locked=False)
        self.runtime._command_session = Mock(snapshot=lambda: self.snapshot)
        self.runtime._selfcheck = "ok"
        self.runtime.settings.hotkey_mode = mode.value
        self.rig.hotkey.signature.return_value = (65, 4)
        self.backend = Mock(spec=HotkeyBackend)
        self.backend.grab_combo.return_value = GrabResult("ok", keycode=133)
        self.backend.grab_escape.return_value = GrabResult("ok", keycode=9)
        self.backend.poll_events.return_value = []
        self.backend.fileno.return_value = -1
        self.manager = HotkeyManager(self.backend, clock=lambda: self.rig.now)
        monkeypatch.setattr(self.manager, "signature", Mock(return_value=(133, 0)))
        monkeypatch.setattr(QTimer, "singleShot", lambda *args: None)
        self.manager.on_state = self.runtime._on_command_hotkey_state
        self.runtime.command_hotkey = self.manager
        self.client = Mock()
        command = CommandMode(
            client=self.client,
            session=lambda: self.snapshot,
            model_trusted=lambda: True,
            publish=lambda text: True,
            present=lambda feedback: None,
        )
        self.runtime._command_mode = command
        self.runtime.orchestrator.command_mode = command
        self.runtime.orchestrator._command_preview = self.runtime._preview_command
        self.runtime.on_command_preview = Mock()
        assert self.runtime.reload_command_hotkey()

    def press(self, *, pending: bool = False) -> None:
        self.manager.handle_event(HotkeyEvent("KeyPress", 133, 1), self.rig.now)
        if not pending:
            self.rig.now += 0.31
            self.manager.tick(self.rig.now)

    def processing(self) -> None:
        self.press()
        self.manager.fsm.stop(self.rig.now, "limit")
        self.rig.fire_tail()
        assert self.runtime.orchestrator.phase == DictationPhase.PROCESSING

    def mapping(self, *, escape: bool = False) -> None:
        event = MappingEvent(
            {"Super_L": GrabResult("ok", keycode=133) if escape else GrabResult("busy")},
            GrabResult("busy") if escape else None,
        )
        self.backend.poll_events.return_value = [event]
        self.manager.process_pending()
        self.backend.poll_events.return_value = []

    def timer(self) -> FakeTimer:
        timer = self.runtime._command_regrab_timer
        assert timer is not None
        return cast(FakeTimer, timer)


@pytest.mark.parametrize("mode", [HotkeyMode.PTT, HotkeyMode.TOGGLE])
@pytest.mark.parametrize("escape", [False, True])
def test_mapping_loss_cancels_recording_without_waiting_for_release(
    monkeypatch: pytest.MonkeyPatch, mode: HotkeyMode, escape: bool
) -> None:
    r = CommandRig(monkeypatch, mode)
    r.press()
    assert r.runtime.orchestrator.phase == DictationPhase.RECORDING
    r.mapping(escape=escape)
    assert r.manager.fsm.state == HotkeyState.IDLE
    assert r.rig.trace.count("record.cancel") == 1
    r.rig.guard.set_recording.assert_called_with(False)
    r.manager.handle_event(HotkeyEvent("KeyRelease", 133, 2), r.rig.now + 1)
    r.runtime.orchestrator.on_worker_event(
        {
            "type": "result",
            "generation": r.rig.supervisor.generation,
            "text": "поздний результат отменённой записи",
        }
    )
    r.client.submit.assert_not_called()
    assert r.timer().active
    r.runtime.shutdown()


def test_pending_300ms_and_old_timer_cannot_start_after_loss_or_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    r = CommandRig(monkeypatch)
    r.press(pending=True)
    r.mapping()
    r.manager.tick(r.rig.now + 1)
    assert "record.start" not in r.rig.trace
    timer = r.timer()
    r.backend.grab_combo.return_value = GrabResult("busy")
    timer.fire()
    assert r.runtime._command_regrab_timer is timer
    r.backend.grab_combo.return_value = GrabResult("ok", keycode=133)
    timer.fire()
    assert r.runtime._command_regrab_timer is None
    assert not timer.active and timer.deleted
    r.manager.tick(r.rig.now + 2)
    assert "record.start" not in r.rig.trace
    r.press()
    assert r.rig.trace.count("record.start") == 1
    r.runtime.shutdown()


def test_processing_loss_cancels_and_drops_late_recognition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    r = CommandRig(monkeypatch)
    r.processing()
    r.mapping()
    assert r.rig.trace.count("record.cancel") == 1
    r.runtime.orchestrator.on_worker_event(
        {
            "type": "result",
            "generation": r.rig.supervisor.generation,
            "text": "после отмены",
        }
    )
    r.client.submit.assert_not_called()
    r.runtime.shutdown()


@pytest.mark.parametrize("preview", [False, True])
@pytest.mark.parametrize("escape", [False, True])
def test_delivery_loss_cancels_only_preview_not_actual_submit(
    monkeypatch: pytest.MonkeyPatch, preview: bool, escape: bool
) -> None:
    r = CommandRig(monkeypatch)
    r.runtime.settings.command_preview = preview
    r.processing()
    r.runtime.orchestrator._result("проверочная команда")
    assert r.runtime.orchestrator.phase == DictationPhase.DELIVERING
    r.mapping(escape=escape)
    r.runtime.confirm_command()  # Includes a stale preview timer.
    if preview:
        assert r.runtime._preview_send is None
        r.client.submit.assert_not_called()
    else:
        assert r.runtime.orchestrator.phase == DictationPhase.DELIVERING
        r.client.submit.assert_called_once()
        r.client.cancel.assert_not_called()
        r.client.submit.call_args.args[1](DeliveryResult("delivered", "none"))
        assert str(r.runtime.orchestrator.phase.value) == "finishing"
    r.runtime.shutdown()


@pytest.mark.parametrize("gate", ["lock", "unknown", "sleep", "disabled", "collision", "shutdown"])
def test_retry_cannot_reopen_command_when_admission_closes(
    monkeypatch: pytest.MonkeyPatch, gate: str
) -> None:
    r = CommandRig(monkeypatch)
    r.press(pending=True)
    r.mapping()
    timer = r.timer()
    r.backend.grab_combo.reset_mock()
    if gate in ("lock", "unknown", "sleep"):
        r.snapshot = (
            SessionSnapshot(known=True, locked=True)
            if gate == "lock"
            else SessionSnapshot(preparing_for_sleep=gate == "sleep")
        )
        r.runtime._command_session_changed()
    elif gate == "disabled":
        r.runtime.settings.command_enabled = False
    elif gate == "collision":
        r.rig.hotkey.signature.return_value = (133, 0)
    else:
        r.runtime.shutdown()
    timer.fire()
    r.runtime.reload_command_hotkey()  # Queued timeout after stop/delete is also harmless.
    r.backend.grab_combo.assert_not_called()
    r.manager.tick(r.rig.now + 1)
    assert "record.start" not in r.rig.trace
    assert r.runtime._command_regrab_timer is None
    r.runtime.shutdown()


def test_command_loss_does_not_cancel_active_text_recording(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    r = CommandRig(monkeypatch)
    r.runtime.orchestrator.on_hotkey_state(HotkeyState.RECORDING, "press")
    assert r.runtime.orchestrator.mode == "text"
    r.mapping()
    assert r.runtime.orchestrator.phase == DictationPhase.RECORDING
    assert "record.cancel" not in r.rig.trace
    r.rig.hotkey.ungrab.assert_not_called()
    r.runtime.shutdown()


@pytest.mark.parametrize("pending", [False, True])
def test_successful_new_physical_key_cannot_orphan_a_held_command_ptt(
    monkeypatch: pytest.MonkeyPatch, pending: bool
) -> None:
    r = CommandRig(monkeypatch)
    r.press(pending=pending)
    cast(Mock, r.manager.signature).return_value = (134, 0)
    r.backend.poll_events.return_value = [
        MappingEvent({"Super_L": GrabResult("ok", keycode=134)}, None, keycode_changed=True)
    ]
    r.manager.process_pending()
    r.backend.poll_events.return_value = []
    r.manager.handle_event(HotkeyEvent("KeyRelease", 133, 2), r.rig.now + 1)
    r.manager.tick(r.rig.now + 2)
    assert r.manager.fsm.state == HotkeyState.IDLE
    assert r.rig.trace.count("record.cancel") == (0 if pending else 1)
    assert r.rig.trace.count("record.start") == (0 if pending else 1)
    r.client.submit.assert_not_called()
    r.runtime.shutdown()


def test_successful_unchanged_mapping_does_not_cancel_active_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    r = CommandRig(monkeypatch)
    r.press()
    r.backend.poll_events.return_value = [
        MappingEvent({"Super_L": GrabResult("ok", keycode=133)}, GrabResult("ok", keycode=9))
    ]
    r.manager.process_pending()
    r.backend.poll_events.return_value = []
    assert r.manager.fsm.state == HotkeyState.RECORDING
    assert "record.cancel" not in r.rig.trace
    r.manager.handle_event(HotkeyEvent("KeyRelease", 133, 2), r.rig.now + 1)
    assert str(r.manager.fsm.state.value) == "processing"
    r.runtime.shutdown()
