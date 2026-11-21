"""W1 CARD correlation, unlock scheduling and session admission."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest
from PyQt5.QtCore import QCoreApplication, QEvent, Qt
from test_meeting_m8_offer import (
    CALL,
    SHOW,
    deliver,
    ended,
    memory,
    offer_ready,
    real_supervisor,
    replace_role,
    show_offer,
    started,
    tick_at,
)
from test_meeting_m8_role_ownership import command_types, leave
from test_meeting_recording_bridge import (
    DETECTOR_START,
    DETECTOR_STOP,
    Rig,
    deliver_detector,
    deliver_remote,
)
from test_meeting_recording_bridge import rig as rig

from astra_voice.core.command_mode import SessionSnapshot
from astra_voice.platform.session import SessionKind
from astra_voice.worker import ipc
from helpers.qt_app import get_qapplication

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("filesystem_euid")]
QUERY = "meeting.call.query"


def sleep_or_lock(rig: Rig, reason: str) -> SessionSnapshot:
    allowed = rig.session.value
    rig.session.value = replace(
        allowed,
        known=reason != "unknown",
        locked=reason == "locked",
        preparing_for_sleep=reason == "sleep",
        blocked_epoch=allowed.blocked_epoch + 1,
        sleep_epoch=allowed.sleep_epoch + int(reason == "sleep"),
    )
    rig.recording.tick()
    return replace(
        allowed,
        blocked_epoch=rig.session.value.blocked_epoch,
        sleep_epoch=rig.session.value.sleep_epoch,
    )


def unlock(rig: Rig, *, reason: str = "locked") -> int:
    rig.session.value = sleep_or_lock(rig, reason)
    rig.recording.tick()
    return rig.recording.offer.now_ms()


def query_after_unlock(rig: Rig) -> str:
    now = unlock(rig)
    tick_at(rig, now + 5_000)
    req = rig.recording._offer_query_req
    assert req and rig.supervisor.messages[-1]["req"] == req
    return req


def reply(
    rig: Rig,
    req: str,
    *,
    call_id: str = "card-id",
    app_class: str = "dion",
    measured: int | None = None,
    variant1: bool = True,
) -> dict[str, Any]:
    return {
        **rig.call_state(app_class, req=req),
        "call_id": call_id,
        "measured_mono_ms": rig.recording.offer.now_ms() if measured is None else measured,
        "variant1": variant1,
    }


@pytest.mark.parametrize("variant1", [False, True])
def test_u3_only_current_own_card_req_can_show_without_w4(rig: Rig, variant1: bool) -> None:
    view = offer_ready(rig)
    req = query_after_unlock(rig)
    deliver(rig, reply(rig, "other-req"))
    rig.supervisor.event({**reply(rig, req), "generation": rig.supervisor.generation - 1})
    rig.recording.tick()
    assert view.calls == [] and rig.recording._offer_query_req == req
    deliver(rig, {"type": "meeting.ack", "req": req, "request_type": QUERY})
    assert rig.recording._offer_query_req == req
    deliver(rig, reply(rig, req, variant1=variant1))
    assert rig.recording.offer.state == "pending" and not rig.recording._offer_query_req
    rig.recording.tick()
    assert view.calls == [SHOW] and not rig.bridge.startDialogOpen
    assert not rig.recording.call_id and not rig.recording.app_class and not rig.recording.variant1
    deliver(rig, ended("card-id"))
    deliver(rig, reply(rig, req))
    rig.recording.tick()
    assert view.calls == [SHOW, ("close",)]


def test_u3_rejects_old_measured_stamp_even_for_own_req(rig: Rig) -> None:
    view = offer_ready(rig)
    req = query_after_unlock(rig)
    deliver(rig, reply(rig, req, measured=103_999))
    rig.recording.tick()
    assert not rig.recording._offer_query_req and view.calls == []
    req2 = query_after_unlock(rig)
    assert req2 != req
    deliver(rig, reply(rig, req2, measured=109_000))
    rig.recording.tick()
    assert view.calls == [SHOW]


def test_old_card_reply_rejected_after_stopping_and_restarting_detector(rig: Rig) -> None:
    view = offer_ready(rig)
    old_req = query_after_unlock(rig)
    rig.bridge.setSetting("meeting_offer_on_call", False)
    stop = rig.supervisor.messages[-1]
    assert stop["type"] == DETECTOR_STOP
    deliver_detector(rig.supervisor, stop, "meeting.detector.stopped")
    # Restore all of D5 in the same role before the late response.
    rig.bridge.setSetting("meeting_offer_on_call", True)
    rig.recording.tick()
    start = rig.supervisor.messages[-1]
    assert start["type"] == DETECTOR_START
    deliver_detector(rig.supervisor, start, "meeting.detector.started")
    assert rig.recording._offer_eligible()
    deliver(rig, reply(rig, old_req))
    rig.recording.tick()
    assert view.calls == []
    deliver(rig, reply(rig, query_after_unlock(rig)))
    rig.recording.tick()
    assert view.calls == [SHOW]


def test_replacement_resets_card_and_filters_old_role_even_at_same_generation(rig: Rig) -> None:
    view = offer_ready(rig)
    old = rig.supervisor
    old_req = query_after_unlock(rig)
    replace_role(rig)
    assert rig.supervisor.generation == old.generation
    new_req = query_after_unlock(rig)
    old.event(reply(rig, new_req))
    deliver(rig, reply(rig, old_req))
    rig.recording.tick()
    assert view.calls == [] and rig.recording._offer_query_req == new_req
    deliver(rig, reply(rig, new_req))
    rig.recording.tick()
    assert view.calls == [SHOW]


@pytest.mark.parametrize("status", ["inactive", "error"])
def test_card_final_replies_clear_wait_without_capture_dispatch_or_retry(
    rig: Rig, status: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    view = offer_ready(rig)
    req = query_after_unlock(rig)
    answer: dict[str, Any] = {"req": req, "request_type": QUERY}
    if status == "inactive":
        answer.update(type="meeting.call.state", active=False)
    else:
        answer.update(type="error", code="limit", message="meeting-command-failed")
    handler = Mock()
    with monkeypatch.context() as patch:
        patch.setattr(rig.recording.controller, "handle_event", handler)
        deliver(rig, answer)
        deliver(rig, reply(rig, req))
        tick_at(rig, 105_001)
        tick_at(rig, 110_001)
        handler.assert_not_called()
    assert command_types(rig.supervisor) == [DETECTOR_START, QUERY]
    assert not rig.recording._offer_query_req and view.calls == []
    deliver(rig, reply(rig, query_after_unlock(rig)))
    rig.recording.tick()
    assert view.calls == [SHOW]


@pytest.mark.parametrize("error", [RuntimeError("meeting-role-exiting"), ValueError("bad-field")])
def test_card_send_exception_clears_slot_and_does_not_retry(
    rig: Rig, error: Exception, monkeypatch: pytest.MonkeyPatch
) -> None:
    view = offer_ready(rig)
    now = unlock(rig)
    sender = Mock(side_effect=error)
    with monkeypatch.context() as patch:
        patch.setattr(rig.supervisor, "send", sender)
        tick_at(rig, now + 5_000)
        assert not rig.recording._offer_query_req
        tick_at(rig, now + 10_000)
        sender.assert_called_once()
    assert view.calls == []
    deliver(rig, reply(rig, query_after_unlock(rig)))
    rig.recording.tick()
    assert view.calls == [SHOW]


def test_real_send_synchronous_queue_limit_clears_preassigned_card_slot(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    view = offer_ready(rig)
    role = real_supervisor(rig)
    full_queue = 4 * ipc.MAX_FRAME_BYTES
    role._outgoing.extend(bytes(full_queue))
    delivered = Mock(wraps=rig.recording._event)

    with monkeypatch.context() as patch:
        patch.setattr(role, "process", Mock(poll=Mock(return_value=None)))
        patch.setattr(role, "_on_event", delivered)
        patch.setattr(rig.recording.controller, "supervisor", role)
        assert role.ready and rig.recording._offer_eligible()
        now = unlock(rig)
        tick_at(rig, now + 5_000)
        delivered.assert_called_once()
        rejection = delivered.call_args.args[0]
        assert (rejection["type"], rejection["request_type"], rejection["code"]) == (
            "error",
            QUERY,
            "limit",
        )
        assert not rig.recording._offer_query_req and not role._pending
        assert len(role._outgoing) == full_queue and view.calls == []
        role._outgoing.clear()
        tick_at(rig, now + 5_001)
        tick_at(rig, now + 10_001)
        assert not role._outgoing
        delivered.assert_called_once()

        now = unlock(rig)
        tick_at(rig, now + 5_000)
        assert role._outgoing
        request = ipc.decode(bytes(role._outgoing[4:]), role=role.role)
        assert request["req"] == rig.recording._offer_query_req != rejection["req"]


@pytest.mark.parametrize("reason", ["locked", "unknown", "sleep"])
def test_u4_unlock_queries_after_five_seconds_and_refreshes_offer(rig: Rig, reason: str) -> None:
    view = offer_ready(rig)
    tick_at(rig, 110_000)  # Initial allow snapshot is a baseline, not an unlock.
    assert command_types(rig.supervisor) == [DETECTOR_START]
    show_offer(rig)
    tick_at(rig, 115_000)
    now = unlock(rig, reason=reason)
    closed = [SHOW, ("remaining", 55), ("close",)]
    assert view.calls == closed and memory(rig) == "lock_closed"
    deliver(rig, started("new-id", measured=now))
    tick_at(rig, now + 4_999)
    assert command_types(rig.supervisor) == [DETECTOR_START]
    assert view.calls == closed
    tick_at(rig, now + 5_000)
    assert command_types(rig.supervisor) == [DETECTOR_START, QUERY]
    assert view.calls == closed
    deliver(rig, reply(rig, rig.recording._offer_query_req))
    rig.recording.tick()
    assert view.calls == [*closed, SHOW]
    tick_at(rig, now + 5_100)
    assert not rig.recording.press_offer("start", now + 5_100)
    tick_at(rig, now + 5_600)
    assert not rig.recording.press_offer("start", 110_600)
    assert rig.recording.offer.visible
    tick_at(rig, now + 64_999)
    assert view.calls[-1] == ("remaining", 1)
    tick_at(rig, now + 65_000)
    assert view.calls[-2:] == [("remaining", 1), ("close",)]
    assert memory(rig) == "refused"


@pytest.mark.parametrize("initial_active", [True, False])
def test_first_login1_snapshot_is_not_an_unlock(rig: Rig, initial_active: bool) -> None:
    allowed = rig.session.value
    rig.session.value = SessionSnapshot(active=initial_active)
    view = offer_ready(rig, allowed=False)
    rig.session.value = allowed
    rig.session.changed.emit(allowed)
    tick_at(rig, 100_100)
    tick_at(rig, 110_100)
    assert command_types(rig.supervisor) == [DETECTOR_START]
    deliver(rig, reply(rig, query_after_unlock(rig)))
    rig.recording.tick()
    assert view.calls == [SHOW]
    assert command_types(rig.supervisor) == [DETECTOR_START, QUERY]


@pytest.mark.parametrize("cancel", ["lock", "epoch", "dialog", "remote", "role"])
def test_scheduled_card_is_canceled_by_a_new_gate_or_epoch(rig: Rig, cancel: str) -> None:
    view = offer_ready(rig)
    now = unlock(rig)
    tick_at(rig, now + 4_000)
    before = rig.supervisor
    if cancel == "lock":
        allowed = sleep_or_lock(rig, "locked")
    elif cancel == "epoch":
        rig.session.value = replace(rig.session.value, sleep_epoch=3)
        rig.recording.tick()
    elif cancel == "dialog":
        rig.bridge.requestRecording()
        rig.bridge.closeStartDialog()
    elif cancel == "remote":
        deliver_remote(before)
    else:
        replace_role(rig)
    tick_at(rig, now + 5_000 if cancel != "role" else now + 10_000)
    assert command_types(before).count(QUERY) == int(cancel == "dialog")
    assert view.calls == [] and not rig.recording._offer_query_req
    if cancel == "lock":
        rig.session.value = allowed
        rig.recording.tick()
    if cancel == "remote":
        assert rig.recording._automation_off
        return
    deliver(rig, reply(rig, query_after_unlock(rig)))
    rig.recording.tick()
    assert view.calls == [SHOW]


@pytest.mark.parametrize("selected_class", ["dion", "browser", None])
def test_unlock_card_respects_per_class_memory(rig: Rig, selected_class: str | None) -> None:
    view = offer_ready(rig)
    show_offer(rig)
    tick_at(rig, 100_500)
    assert rig.recording.press_offer("refuse", 100_500)
    rig.recording.tick()
    if selected_class is None:
        show_offer(rig, call_id="browser-call", app_class="browser")
        tick_at(rig, 101_000)
        assert rig.recording.press_offer("refuse", 101_000)
        rig.recording.tick()

    now = unlock(rig)
    tick_at(rig, now + 5_000)
    if selected_class is None:
        assert command_types(rig.supervisor) == [DETECTOR_START]
        deliver(rig, ended("browser-call"))
        deliver(rig, reply(rig, query_after_unlock(rig), app_class="browser"))
        rig.recording.tick()
        assert view.calls[-1] == ("show", "call-browser", 60)
    else:
        assert command_types(rig.supervisor) == [DETECTOR_START, QUERY]
        req = rig.recording._offer_query_req
        deliver(rig, reply(rig, req, app_class=selected_class))
        rig.recording.tick()
        if selected_class == "dion":
            assert view.calls == [SHOW, ("close",)]
            assert "card-id" in rig.recording.offer._memory["dion"].call_ids
            deliver(rig, ended("card-id"))
            deliver(rig, reply(rig, query_after_unlock(rig), app_class="dion"))
            rig.recording.tick()
            assert view.calls[-1] == SHOW and rig.recording.offer.visible
        else:
            assert view.calls[-1] == ("show", "call-browser", 60)


def test_old_card_origin_suppresses_offer_beyond_fifteen_seconds_until_class_ends(rig: Rig) -> None:
    view = offer_ready(rig)
    deliver(rig, reply(rig, query_after_unlock(rig)))
    rig.recording.tick()
    tick_at(rig, 105_500)
    assert rig.recording.press_offer("refuse", 105_500)
    rig.recording.tick()
    tick_at(rig, 121_500)
    deliver(rig, started("same-class", measured=121_500))
    rig.recording.tick()
    assert view.calls == [SHOW, ("close",)]
    deliver(rig, ended("same-class"))
    show_offer(rig, call_id="next-call")
    assert view.calls == [SHOW, ("close",), SHOW]


@pytest.mark.parametrize("stage", ["pending", "in-flight"])
def test_remote_revokes_pending_offer_and_old_card(rig: Rig, stage: str) -> None:
    view = offer_ready(rig)
    req = query_after_unlock(rig)
    if stage == "pending":
        deliver(rig, reply(rig, req))
        assert rig.recording.offer.state == "pending"
    role = rig.supervisor
    deliver_remote(role, stale=True)
    assert not rig.recording._automation_off
    deliver_remote(role)
    assert rig.recording.offer.state == "idle" and not rig.recording._offer_query_req
    deliver(rig, reply(rig, req))
    rig.recording.tick()
    assert view.calls == []
    leave(role)
    rig.bridge.requestRecording()
    assert rig.bridge.startDialogOpen and rig.bridge.startCanStart


@pytest.mark.parametrize("known", [False, True])
def test_fly_requires_real_lock_support_for_offer_and_card(tmp_path: Path, known: bool) -> None:
    get_qapplication()
    fly = Rig(tmp_path / "fly", kind=SessionKind.FLY)
    kde = Rig(tmp_path / "kde", kind=SessionKind.KDE)
    unsupported = SessionSnapshot(known=known, locked=False, supported=False)
    try:
        fly.session.value = unsupported
        view = offer_ready(fly, allowed=False)
        assert fly.recording.controller._allowed() is True
        deliver(fly, started())
        fly.recording.tick()
        assert view.calls == []
        # The user may lose desktop support while CARD is already in flight.
        fly.session.value = kde.session.value
        fly.recording.tick()
        req = query_after_unlock(fly)
        fly.session.value = unsupported
        deliver(fly, reply(fly, req))
        fly.recording.tick()
        assert view.calls == []
        assert not fly.recording._offer_query_req
        fly.bridge.requestRecording()
        assert fly.bridge.startCanStart
        fly.bridge.on_start_clicked()
        assert fly.supervisor.messages[-1]["type"] == "meeting.capture.prepare"

        kde_view = offer_ready(kde)
        show_offer(kde)
        deliver(kde, ended())
        deliver(kde, reply(kde, query_after_unlock(kde)))
        kde.recording.tick()
        assert kde_view.calls == [SHOW, ("close",), SHOW]

    finally:
        fly.bridge.shutdown()
        kde.bridge.shutdown()


def test_fly_with_supported_unlocked_snapshot_uses_the_same_gate(tmp_path: Path) -> None:
    get_qapplication()
    fly = Rig(tmp_path, kind=SessionKind.FLY)
    try:
        assert fly.session.value.allowed
        view = offer_ready(fly)
        show_offer(fly)
        assert view.calls == [SHOW]
    finally:
        fly.bridge.shutdown()


def test_server_change_cancels_card_wait_and_preserves_class_memory(rig: Rig) -> None:
    view = offer_ready(rig)
    show_offer(rig)
    tick_at(rig, 100_500)
    assert rig.recording.press_offer("refuse", 100_500)
    rig.recording.tick()
    req = query_after_unlock(rig)
    deliver(rig, ended(reason="server_changed"))
    assert not rig.recording._offer_query_req and memory(rig) == "refused"
    deliver(rig, reply(rig, req, app_class="browser"))
    rig.recording.tick()
    assert view.calls == [SHOW, ("close",)]
    deliver(rig, reply(rig, query_after_unlock(rig), app_class="dion"))
    rig.recording.tick()
    assert view.calls == [SHOW, ("close",)]
    deliver(rig, reply(rig, query_after_unlock(rig), app_class="browser"))
    rig.recording.tick()
    assert view.calls[-1] == ("show", "call-browser", 60)


@pytest.mark.parametrize("monitor_stays_allowed", [True, False])
def test_queued_lock_and_unlock_use_delivered_snapshots(
    rig: Rig, monitor_stays_allowed: bool
) -> None:
    view = offer_ready(rig)
    show_offer(rig)
    rig.session.changed.disconnect(rig.recording._session_changed)
    rig.session.changed.connect(rig.recording._session_changed, Qt.QueuedConnection)
    allowed = replace(rig.session.value, blocked_epoch=rig.session.value.blocked_epoch + 1)
    rig.session.value = allowed
    rig.session.changed.emit(replace(allowed, locked=True))
    rig.session.changed.emit(allowed)
    assert view.calls == [SHOW]
    QCoreApplication.sendPostedEvents(None, QEvent.MetaCall)
    assert view.calls == [SHOW, ("close",)] and memory(rig) == "lock_closed"
    tick_at(rig, 104_999)
    assert command_types(rig.supervisor) == [DETECTOR_START]

    if not monitor_stays_allowed:
        rig.session.value = replace(allowed, locked=True, blocked_epoch=allowed.blocked_epoch + 1)
    tick_at(rig, 105_000)
    if monitor_stays_allowed:
        assert command_types(rig.supervisor) == [DETECTOR_START, QUERY]
        req = rig.recording._offer_query_req
    else:
        assert command_types(rig.supervisor) == [DETECTOR_START]
        rig.session.value = replace(rig.session.value, locked=False)
        rig.recording.tick()
        tick_at(rig, 110_000)
        req = rig.recording._offer_query_req
    deliver(rig, reply(rig, req))
    rig.recording.tick()
    tick_at(rig, 111_000)
    assert command_types(rig.supervisor) == [DETECTOR_START, QUERY]
    assert [call for call in view.calls if call[0] == "show"] == [SHOW, SHOW]


def test_card_before_first_offer_of_new_role_still_ends_inheritance(rig: Rig) -> None:
    view = offer_ready(rig)
    show_offer(rig)
    tick_at(rig, 100_500)
    assert rig.recording.press_offer("refuse", 100_500)
    rig.recording.tick()
    replace_role(rig)
    deadline = rig.recording.offer.now_ms() + 15_000
    req = query_after_unlock(rig)
    deliver(rig, reply(rig, req, call_id="replacement-id"))
    rig.recording.tick()
    entry = rig.recording.offer._memory["dion"]
    assert entry.call_ids == {"replacement-id"} and entry.deadline == deadline

    deliver(rig, started("replacement-id", measured=rig.recording.offer.now_ms()))
    rig.recording.tick()
    assert entry.deadline is None and entry.role_serial == rig.recording._role_serial
    assert entry.state == "refused"
    tick_at(rig, deadline - 5_000)
    for now in (deadline, deadline + 5_000):
        req = query_after_unlock(rig)
        assert rig.recording.offer.now_ms() == now
        deliver(rig, reply(rig, req, call_id="replacement-id"))
        deliver(rig, started("replacement-id", measured=now))
        rig.recording.tick()
        assert view.calls == [SHOW, ("close",)] and memory(rig) == "refused"
    deliver(rig, ended("replacement-id"))
    show_offer(rig, call_id="next-call")


def test_positive_w4_card_tracks_call_end_without_showing_w1(rig: Rig) -> None:
    view = offer_ready(rig)
    deliver(rig, reply(rig, query_after_unlock(rig), call_id="old-id"))
    rig.recording.tick()
    tick_at(rig, 105_500)
    assert rig.recording.press_offer("refuse", 105_500)
    rig.recording.tick()
    deliver(rig, ended("old-id", reason="evicted"))
    assert memory(rig) == "refused"
    rig.bridge.requestRecording()
    deliver(rig, reply(rig, rig.recording._query_req, call_id="w4-id"))
    assert rig.recording.call_id == "w4-id"
    rig.bridge.closeStartDialog()
    rig.recording.tick()
    assert view.calls == [SHOW, ("close",)]

    deliver(rig, ended("w4-id"))
    show_offer(rig, call_id="next-call")
    assert view.calls == [SHOW, ("close",), SHOW]


def test_server_change_revokes_redisplay_of_lock_closed_call(rig: Rig) -> None:
    view = offer_ready(rig)
    show_offer(rig)
    allowed = sleep_or_lock(rig, "locked")
    assert memory(rig) == "lock_closed"
    deliver(rig, ended(CALL, reason="server_changed"))
    rig.session.value = allowed
    rig.recording.tick()
    tick_at(rig, 105_000)
    assert command_types(rig.supervisor) == [DETECTOR_START, QUERY]
    deliver(rig, reply(rig, rig.recording._offer_query_req, call_id="changed-id"))
    rig.recording.tick()
    assert view.calls == [SHOW, ("close",)] and memory(rig) == "shown"

    deliver(rig, ended("changed-id"))
    show_offer(rig, call_id="next-call")
    assert view.calls == [SHOW, ("close",), SHOW]


def test_evictions_keep_only_live_call_ids_for_continuing_call(rig: Rig) -> None:
    view = offer_ready(rig)
    old_id = "participant-0"
    deliver(rig, reply(rig, query_after_unlock(rig), call_id=old_id))
    rig.recording.tick()
    tick_at(rig, 105_500)
    assert rig.recording.press_offer("refuse", 105_500)
    rig.recording.tick()
    live = [old_id]
    for index in range(1, 33):
        req = query_after_unlock(rig)
        if len(live) == 8:
            deliver(rig, ended(live.pop(0), reason="evicted"))
        call_id = f"participant-{index}"
        live.append(call_id)
        deliver(rig, reply(rig, req, call_id=call_id))
        rig.recording.tick()
        assert set(rig.recording.offer._calls) == set(live)
        assert rig.recording.offer._memory["dion"].call_ids == set(live)
        assert view.calls == [SHOW, ("close",)] and memory(rig) == "refused"

    deliver(rig, ended(live[-1]))
    show_offer(rig, call_id="next-call")
    assert view.calls == [SHOW, ("close",), SHOW]
