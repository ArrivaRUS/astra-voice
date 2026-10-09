"""Whole recognition -> command outcome path, reusing existing hardware-free rig."""

from __future__ import annotations

from importlib import import_module
from typing import Any

import pytest

from astra_voice.core.command_mode import SessionSnapshot
from astra_voice.core.dictation import DictationPhase
from astra_voice.platform.cowork import DeliveryResult

from .test_publication import PHRASE
from .test_publication import Rig as CommandRig

DictationRig = import_module("unit.test_dictation").Rig
pytestmark = pytest.mark.unit


def setup_command() -> tuple[Any, CommandRig]:
    dictation = DictationRig()
    command = CommandRig()
    dictation.core.command_mode = command.mode
    dictation.hotkey.on_state = lambda state, reason: dictation.core.on_hotkey_state(
        state, reason, mode="command"
    )
    return dictation, command


@pytest.mark.parametrize(
    "outcome",
    [
        DeliveryResult("delivered", "none"),
        DeliveryResult("unknown", "timeout"),
        DeliveryResult("undelivered", "busy"),
    ],
)
def test_command_recognition_never_uses_active_window_or_paste_and_records_safe_outcome(
    outcome: DeliveryResult,
) -> None:
    rig, command = setup_command()
    rig.start()
    rig.stop()
    rig.result(PHRASE)
    assert rig.core.phase == DictationPhase.DELIVERING
    assert rig.pasted == []
    assert not [event for event in rig.trace if event[0] == "active_window"]
    assert len(command.client.calls) == 1
    command.client.reply(outcome)
    assert rig.pasted == []
    assert command.published == ([] if outcome.outcome == "delivered" else [PHRASE])
    event = [event for event in rig.stats.events if event["type"] == "dictation"][-1]
    assert event["mode"] == "command"
    assert event["result"] == outcome.outcome
    assert event["deliver_reason"] == outcome.reason
    assert event["deliver_ms"] >= 0
    assert PHRASE not in repr(rig.stats.events)


def test_user_cancel_during_delivery_does_not_claim_command_cancelled() -> None:
    rig, command = setup_command()
    rig.start()
    rig.stop()
    rig.result(PHRASE)
    before = list(rig.sent)
    rig.core.cancel("pill")
    assert rig.core.phase == DictationPhase.DELIVERING
    assert rig.sent == before
    assert command.client.cancels == 0
    command.client.reply(DeliveryResult("delivered", "none"))
    assert rig.stats.events[-1]["result"] == "delivered"


def test_locked_before_start_never_opens_recording() -> None:
    rig, command = setup_command()
    command.snapshot = SessionSnapshot(known=True, locked=True)
    rig.start()
    assert "record.start" not in rig.commands()
    assert command.client.calls == []
    assert not rig.recording


def test_lock_during_delivery_keeps_publication_closed_through_orchestrator_finish() -> None:
    rig, command = setup_command()
    rig.start()
    rig.stop()
    rig.result(PHRASE)
    command.snapshot = SessionSnapshot(known=True, locked=True)
    rig.core.command_session_changed()
    rig.pill.calls.clear()
    command.client.reply(DeliveryResult("unknown", "timeout"))
    assert command.published == []
    assert command.presented == []
    assert rig.pill.calls == []
    assert rig.core.last_text == PHRASE
    assert rig.pasted == []
