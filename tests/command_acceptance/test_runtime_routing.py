"""Runtime wiring with existing fully fake hardware factories, never start_command_mode."""

from __future__ import annotations

from importlib import import_module
from unittest.mock import Mock

import pytest

from astra_voice.core.command_mode import SessionSnapshot
from astra_voice.core.dictation import DictationPhase
from astra_voice.core.settings import from_dict
from astra_voice.platform.session import SessionKind

from .test_publication import PHRASE
from .test_publication import Rig as CommandRig

RuntimeRig = import_module("unit.test_runtime").Rig
DictationRig = import_module("unit.test_dictation").Rig
pytestmark = pytest.mark.unit


def test_normal_text_dictation_never_calls_assistant_even_with_command_port_available() -> None:
    rig = DictationRig()
    command = CommandRig()
    rig.core.command_mode = command.mode
    rig.start()
    rig.stop()
    rig.result(PHRASE)
    assert len(rig.pasted) == 1
    assert rig.pasted[0][0] == PHRASE
    assert command.client.calls == []
    assert command.published == []
    assert command.presented == []
    assert rig.core.mode == "text"


@pytest.mark.parametrize(
    "closed",
    [
        SessionSnapshot(known=True, locked=True),
        SessionSnapshot(known=False, locked=False),
        SessionSnapshot(known=True, locked=False, preparing_for_sleep=True),
    ],
)
def test_runtime_lock_unknown_or_sleep_clears_preview_and_removes_fake_grabs(
    monkeypatch: pytest.MonkeyPatch, closed: SessionSnapshot
) -> None:
    rig = RuntimeRig(
        monkeypatch, from_dict({"command_preview": True}), session_kind=SessionKind.KDE
    )
    command = CommandRig()
    runtime = rig.runtime
    runtime._command_session = Mock(snapshot=lambda: command.snapshot)
    runtime._command_mode = command.mode
    runtime.orchestrator.command_mode = command.mode
    runtime.orchestrator.mode = "command"
    runtime.orchestrator._phase = DictationPhase.DELIVERING
    runtime.command_hotkey = Mock()
    previews: list[str] = []
    runtime.on_command_preview = previews.append
    sent = Mock()
    assert runtime._preview_command(PHRASE, sent)
    runtime.command_preview_shown()
    timer = runtime._preview_timer
    assert timer is not None
    assert previews == [PHRASE]
    command.snapshot = closed
    runtime._command_session_changed()
    assert previews == [PHRASE, ""]
    assert runtime._preview_send is None and runtime._preview_timer is None
    assert not timer.active and timer.deleted
    runtime.command_hotkey.ungrab.assert_called_once()
    if closed.preparing_for_sleep or (closed.known and closed.locked):
        rig.hotkey.ungrab.assert_called_once()
    assert command.client.calls == []
    assert command.published == []
    assert command.presented == []
    command.snapshot = SessionSnapshot(known=True, locked=False)
    runtime.confirm_command()  # late confirmation/timer after resume cannot send cleared preview
    sent.assert_not_called()


def test_runtime_copy_last_uses_command_recovery_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    rig = RuntimeRig(monkeypatch)
    command = CommandRig()
    rig.runtime._command_mode = command.mode
    rig.runtime.orchestrator.mode = "command"
    rig.runtime.orchestrator.last_text_is_command = True
    command.mode.last_text = PHRASE
    command.snapshot = SessionSnapshot(known=True, locked=True)
    rig.runtime._copy_last()
    assert command.published == []
    command.snapshot = SessionSnapshot(known=True, locked=False)
    rig.runtime._copy_last()
    assert command.published == [PHRASE]
    assert command.client.calls == []
    rig.publish_clipboard.assert_not_called()
