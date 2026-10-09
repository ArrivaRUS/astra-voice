"""A delayed command notification must be discarded if the session locks."""

from __future__ import annotations

from collections import deque
from typing import Any

import pytest

from astra_voice.ui import notify

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("allowed", [False, True])
def test_queued_notice_rechecks_publication_gate(
    monkeypatch: pytest.MonkeyPatch, allowed: bool
) -> None:
    notice = notify._Notice(
        "Помощник недоступен", "Проверьте помощник", 1, (), 0.0, False, lambda: allowed
    )
    sent: list[Any] = []
    monkeypatch.setattr(notify, "_in_flight_seq", None)
    monkeypatch.setattr(notify, "_slot", notice)
    monkeypatch.setattr(notify, "_queued", deque())
    monkeypatch.setattr(notify, "_pending", deque())
    monkeypatch.setattr(notify, "_sent", {})

    def send(seq: int, item: Any, replacement: int) -> None:
        sent.append(item)
        notify._in_flight_seq = None

    monkeypatch.setattr(notify, "_send", send)
    notify._drain()
    assert bool(sent) is allowed
    assert notify._slot is None


def test_old_notification_cannot_revive_after_unlock_or_new_recording(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    epoch = 0
    allowed = True
    monkeypatch.setattr(notify, "_command_guard", lambda: allowed)
    monkeypatch.setattr(notify, "_command_epoch", lambda: epoch)
    old_notice_guard = notify._command_notice_guard()
    assert old_notice_guard()
    allowed = False
    epoch += 1
    assert not old_notice_guard()
    allowed = True
    assert not old_notice_guard()
    new_notice_guard = notify._command_notice_guard()
    assert new_notice_guard()
    assert not old_notice_guard()
