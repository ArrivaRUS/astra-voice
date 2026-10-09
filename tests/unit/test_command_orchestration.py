"""Command role never inserts into focused windows; all outputs share a session gate."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from test_dictation import Rig

from astra_voice.core.command_mode import CommandFeedback, CommandMode, SessionSnapshot
from astra_voice.core.dictation import DictationPhase
from astra_voice.platform.cowork import DeliveryResult
from astra_voice.platform.hotkey import HotkeyState

pytestmark = pytest.mark.unit


class Client:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.callback: Callable[[DeliveryResult], None] | None = None

    def submit(self, text: str, callback: Callable[[DeliveryResult], None]) -> None:
        self.calls.append(text)
        self.callback = callback

    def cancel(self) -> None:
        if self.callback is not None:
            self.callback(DeliveryResult("unknown", "suspended"))


@pytest.mark.parametrize("outcome", ["delivered", "undelivered", "unknown"])
@pytest.mark.parametrize("lock_at_reply", [False, True])
def test_command_never_pastes_and_checks_publication_at_reply(
    outcome: str, lock_at_reply: bool
) -> None:
    rig = Rig()
    client = Client()
    snapshot = SessionSnapshot(known=True, locked=False)
    published: list[str] = []
    presented: list[CommandFeedback] = []

    def publish(text: str) -> bool:
        published.append(text)
        return True

    mode = CommandMode(
        client=client,
        session=lambda: snapshot,
        model_trusted=lambda: True,
        publish=publish,
        present=presented.append,
    )
    rig.core.command_mode = mode
    rig.core.on_hotkey_state(HotkeyState.RECORDING, "press", mode="command")
    assert str(rig.core.phase.value) == "recording"
    # No target-window query even before recognition.
    assert not any(item[0] == "active-window" for item in rig.trace)
    rig.core.on_hotkey_state(HotkeyState.PROCESSING, "limit", mode="command")
    rig.result("Проверь\nзадачу")
    assert str(rig.core.phase.value) == "delivering"
    assert client.calls == ["Проверь задачу"]
    assert rig.pasted == []
    rig.core.cancel("escape")
    assert str(rig.core.phase.value) == "delivering"
    if lock_at_reply:
        snapshot = SessionSnapshot(known=True, locked=True)
    assert client.callback is not None
    client.callback(DeliveryResult(outcome, "none" if outcome == "delivered" else "timeout"))
    assert str(rig.core.phase.value) == "finishing"
    assert published == ([] if lock_at_reply or outcome == "delivered" else ["Проверь задачу"])
    assert bool(presented) is (not lock_at_reply)
    assert rig.pasted == []
    assert rig.core.last_text == "Проверь задачу"
    assert rig.stats.events[-1]["mode"] == "command"
    assert rig.stats.events[-1]["result"] == outcome


def test_unknown_session_does_not_start_command_recording() -> None:
    rig = Rig()
    rig.core.command_mode = CommandMode(
        client=Client(),
        session=SessionSnapshot,
        model_trusted=lambda: True,
        publish=lambda text: pytest.fail("clipboard"),
        present=lambda feedback: pytest.fail("UI"),
    )
    rig.core.on_hotkey_state(HotkeyState.RECORDING, "press", mode="command")
    assert rig.commands() == []
    assert rig.core.phase == DictationPhase.IDLE


def test_preview_keeps_current_phrase_for_recovery_without_submission() -> None:
    rig = Rig()
    client = Client()
    snapshot = SessionSnapshot(known=True, locked=False)
    copied: list[str] = []

    def publish(text: str) -> bool:
        copied.append(text)
        return True

    mode = CommandMode(
        client=client,
        session=lambda: snapshot,
        model_trusted=lambda: True,
        publish=publish,
        present=lambda feedback: None,
    )
    mode.remember("прежняя команда")
    rig.core.command_mode = mode
    rig.core._command_preview = lambda text, send: True
    rig.core.on_hotkey_state(HotkeyState.RECORDING, "press", mode="command")
    rig.core.on_hotkey_state(HotkeyState.PROCESSING, "limit", mode="command")
    rig.result("Новая\nкоманда")
    assert rig.core.last_text == mode.last_text == "Новая команда"
    assert client.calls == []
    snapshot = SessionSnapshot(known=True, locked=True)
    assert not mode.recover()
    assert copied == []
    snapshot = SessionSnapshot(known=True, locked=False)
    assert mode.recover()
    assert copied == ["Новая команда"]


@pytest.mark.parametrize("failure", ["generation", "watchdog", "microphone", "no-model"])
def test_locked_command_terminal_failure_has_no_pill(failure: str) -> None:
    rig = Rig()
    snapshot = SessionSnapshot(known=True, locked=False)
    mode = CommandMode(
        client=Client(),
        session=lambda: snapshot,
        model_trusted=lambda: True,
        publish=lambda text: pytest.fail("clipboard"),
        present=lambda feedback: pytest.fail("presentation"),
    )
    rig.core.command_mode = mode
    rig.core.on_hotkey_state(HotkeyState.RECORDING, "press", mode="command")
    rig.core.on_hotkey_state(HotkeyState.PROCESSING, "limit", mode="command")
    rig.pill.calls.clear()
    snapshot = SessionSnapshot(known=True, locked=True)
    if failure == "generation":
        rig.worker_generation += 1
        rig.core.on_worker_event({"type": "hello", "generation": rig.worker_generation})
    elif failure == "watchdog":
        rig.core._watchdog()
    else:
        rig.core._error("audio-failed" if failure == "microphone" else "no-model")
    assert rig.pill.calls == []
    assert rig.pill.hidden
