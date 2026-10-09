"""Observable command mode behaviour with independent port fakes, no Qt event loop."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

import pytest

from astra_voice.core.command_mode import CommandFeedback, CommandMode, SessionSnapshot
from astra_voice.platform.cowork import DeliveryResult

pytestmark = pytest.mark.unit
PHRASE = "открой папку приёмки"


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Callable[[DeliveryResult], None]]] = []
        self.cancels = 0

    def submit(self, text: str, callback: Callable[[DeliveryResult], None]) -> None:
        self.calls.append((text, callback))

    def cancel(self) -> None:
        self.cancels += 1
        if self.calls:
            self.calls[-1][1](DeliveryResult("unknown", "suspended"))

    def reply(self, result: DeliveryResult) -> None:
        self.calls[-1][1](result)


class Rig:
    def __init__(self) -> None:
        self.snapshot = SessionSnapshot(known=True, locked=False)
        self.trusted = True
        self.clipboard_ok = True
        self.published: list[str] = []
        self.presented: list[CommandFeedback] = []
        self.results: list[DeliveryResult] = []
        self.client = FakeClient()
        self.mode = CommandMode(
            client=self.client,
            session=lambda: self.snapshot,
            model_trusted=lambda: self.trusted,
            publish=self.publish,
            present=self.presented.append,
        )

    def publish(self, text: str) -> bool:
        self.published.append(text)
        return self.clipboard_ok

    def deliver(self) -> bool:
        return self.mode.deliver(PHRASE, self.results.append)


@pytest.mark.parametrize(
    "snapshot",
    [
        SessionSnapshot(known=True, locked=True),
        SessionSnapshot(known=False, locked=False),
        SessionSnapshot(known=True, locked=False, active=False),
        SessionSnapshot(known=True, locked=False, preparing_for_sleep=True),
    ],
)
def test_closed_session_before_send_keeps_phrase_only_in_memory(snapshot: SessionSnapshot) -> None:
    rig = Rig()
    rig.snapshot = snapshot
    assert rig.deliver()
    assert rig.client.calls == []
    assert rig.published == []
    assert rig.presented == []
    assert rig.mode.last_text == PHRASE
    assert not rig.mode.recover()
    assert len(rig.results) == 1
    assert rig.results[0].outcome == "undelivered"


def test_untrusted_model_never_reaches_assistant_but_keeps_manual_copy() -> None:
    rig = Rig()
    rig.trusted = False
    assert rig.deliver()
    assert rig.client.calls == []
    assert rig.published == [PHRASE]
    assert rig.results == [DeliveryResult("undelivered", "model_untrusted")]
    assert "модели, заданной вручную" in rig.presented[0].text
    assert rig.mode.last_text == PHRASE


def test_trust_is_rechecked_for_each_phrase() -> None:
    rig = Rig()
    rig.deliver()
    rig.client.reply(DeliveryResult("delivered", "none"))
    rig.trusted = False
    rig.deliver()
    assert len(rig.client.calls) == 1
    assert rig.results[-1].reason == "model_untrusted"


def test_accepted_never_publishes_clipboard_and_presentation_never_contains_phrase() -> None:
    rig = Rig()
    assert rig.deliver()
    rig.client.reply(DeliveryResult("delivered", "none"))
    assert rig.published == []
    assert len(rig.client.calls) == len(rig.results) == len(rig.presented) == 1
    assert rig.presented[0].text == "Передано помощнику"
    assert PHRASE not in repr(rig.presented)


@pytest.mark.parametrize(
    "result",
    [
        DeliveryResult("undelivered", "busy"),
        DeliveryResult("undelivered", "refused"),
        DeliveryResult("undelivered", "no_bus"),
        DeliveryResult("unknown", "timeout"),
        DeliveryResult("unknown", "bad_reply"),
    ],
)
def test_fallback_publishes_once_without_automatic_resubmit(result: DeliveryResult) -> None:
    rig = Rig()
    rig.deliver()
    assert not rig.deliver()  # second gesture while delivering does not overwrite memory
    rig.client.reply(result)
    rig.client.reply(DeliveryResult("delivered", "none"))  # transport late callback
    assert rig.published == [PHRASE]
    assert rig.results == [result]
    assert len(rig.presented) == len(rig.client.calls) == 1
    assert rig.mode.recover()
    assert rig.published == [PHRASE, PHRASE]
    assert len(rig.client.calls) == 1


@pytest.mark.parametrize("known", [False, True])
@pytest.mark.parametrize(
    "result",
    [
        DeliveryResult("delivered", "none"),
        DeliveryResult("undelivered", "busy", server_reason="locked"),
        DeliveryResult("undelivered", "no_bus"),
        DeliveryResult("unknown", "timeout"),
    ],
)
def test_lock_or_unknown_between_send_and_answer_suppresses_all_publication(
    known: bool, result: DeliveryResult
) -> None:
    rig = Rig()
    rig.deliver()
    rig.snapshot = SessionSnapshot(known=known, locked=known)
    rig.mode.session_changed()
    rig.client.reply(result)
    assert rig.published == []
    assert rig.presented == []
    assert rig.mode.last_text == PHRASE
    assert rig.results == [result]
    assert rig.client.cancels == 0  # a lock cannot retract an accepted request


def test_unlock_signal_alone_does_not_publish_and_confirmed_unlock_needs_manual_action() -> None:
    rig = Rig()
    rig.snapshot = SessionSnapshot()
    rig.deliver()
    rig.snapshot = replace(rig.snapshot, locked=False)  # an Unlock, no fresh known state
    rig.mode.session_changed()
    assert not rig.mode.recover()
    assert rig.published == []
    rig.snapshot = SessionSnapshot(known=True, locked=False)
    rig.mode.session_changed()
    assert rig.published == []
    assert rig.mode.recover()
    assert rig.published == [PHRASE]
    assert rig.client.calls == []


def test_sleep_during_send_drops_late_acceptance_after_resume() -> None:
    rig = Rig()
    rig.deliver()
    rig.snapshot = replace(rig.snapshot, preparing_for_sleep=True)
    rig.mode.session_changed()
    assert rig.client.cancels == 1
    assert rig.results == [DeliveryResult("unknown", "suspended")]
    rig.snapshot = SessionSnapshot(known=True, locked=False)
    rig.mode.session_changed()
    rig.client.reply(DeliveryResult("delivered", "none"))
    assert rig.published == []
    assert rig.presented == []
    assert len(rig.results) == len(rig.client.calls) == 1
    assert rig.mode.recover()


def test_failed_clipboard_is_reported_and_phrase_can_be_recovered_later() -> None:
    rig = Rig()
    rig.clipboard_ok = False
    rig.deliver()
    rig.client.reply(DeliveryResult("unknown", "timeout"))
    assert rig.results == [DeliveryResult("unknown", "clipboard_failed")]
    assert rig.presented[0].copied is False
    assert "не удалось скопировать" in rig.presented[0].text
    assert rig.mode.last_text == PHRASE
    rig.clipboard_ok = True
    assert rig.mode.recover()
    assert len(rig.client.calls) == 1


def test_publication_rechecks_session_after_clipboard_callback() -> None:
    rig = Rig()

    def lock_while_publishing(text: str) -> bool:
        rig.published.append(text)
        rig.snapshot = SessionSnapshot(known=True, locked=True)
        return True

    rig.mode.publish = lock_while_publishing
    rig.deliver()
    rig.client.reply(DeliveryResult("undelivered", "not_running"))
    assert rig.published == [PHRASE]
    assert rig.presented == []


def test_old_terminal_callback_cannot_complete_next_phrase() -> None:
    rig = Rig()
    rig.deliver()
    old_callback = rig.client.calls[0][1]
    old_callback(DeliveryResult("unknown", "timeout"))
    rig.mode.deliver("вторая команда", rig.results.append)
    old_callback(DeliveryResult("delivered", "none"))
    assert rig.results == [DeliveryResult("unknown", "timeout")]
    rig.client.reply(DeliveryResult("delivered", "none"))
    assert rig.results == [
        DeliveryResult("unknown", "timeout"),
        DeliveryResult("delivered", "none"),
    ]
    assert rig.published == [PHRASE]
    assert rig.mode.last_text == "вторая команда"
