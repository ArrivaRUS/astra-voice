"""Review regressions at W1's IPC, session and input ordering boundaries."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
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
    replace_role,
    show_offer,
    started,
    tick_at,
)
from test_meeting_m8_offer_card import QUERY, reply, sleep_or_lock, unlock
from test_meeting_m8_role_ownership import command_types, leave
from test_meeting_recording_bridge import DETECTOR_START, Rig, Session, deliver_detector
from test_meeting_recording_bridge import rig as rig

from astra_voice.platform import session_state
from astra_voice.platform.session_state import MonitorSnapshot, SessionState
from command_acceptance.test_session_cache import SESSION_PATH, FakeLogin

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("filesystem_euid")]


def unlocked(revision: int = 1, epoch: int = 0) -> MonitorSnapshot:
    return MonitorSnapshot(known=True, locked=False, blocked_epoch=epoch, revision=revision)


def use_queued_session_signals(rig: Rig) -> None:
    rig.session.changed.disconnect(rig.recording._session_changed)
    rig.session.changed.connect(rig.recording._session_changed, Qt.QueuedConnection)


def queue_lock_cycle(rig: Rig) -> None:
    previous = rig.session.value
    assert isinstance(previous, MonitorSnapshot) and previous.allowed
    blocked = replace(
        previous,
        locked=True,
        blocked_epoch=previous.blocked_epoch + 1,
        revision=previous.revision + 1,
    )
    rig.session.value = blocked
    rig.session.changed.emit(blocked)
    allowed = replace(blocked, locked=False, revision=blocked.revision + 1)
    rig.session.value = allowed
    rig.session.changed.emit(allowed)


def deliver_session_signals() -> None:
    QCoreApplication.sendPostedEvents(None, QEvent.MetaCall)


def test_worker_publishes_revision_with_the_same_snapshot(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker = session_state._SessionWorker()
    state = Mock(
        spec=session_state.SessionState,
        known=True,
        locked=False,
        active=True,
        suspending=False,
        lock_requested=False,
        desktop="kde",
    )
    state.is_admissible.side_effect = lambda: not state.locked
    worker.state = state
    worker._fly_environment = worker._fly_process = False
    seen: list[MonitorSnapshot] = []

    def published(snapshot: MonitorSnapshot) -> None:
        assert worker.snapshot is snapshot
        seen.append(snapshot)

    worker.updated.connect(published)
    with monkeypatch.context() as patch:
        patch.setattr(session_state, "_fly_process_present", lambda: False)
        worker._changed()
        state.locked = True
        worker._changed()
        state.locked = False
        worker._changed()
    assert [(snap.revision, snap.blocked_epoch, snap.allowed) for snap in seen] == [
        (1, 0, True),
        (2, 1, False),
        (3, 1, True),
    ]


@pytest.mark.parametrize("deliver_before_show", [False, True])
def test_old_session_notifications_leave_a_fresh_offer_open(
    rig: Rig, deliver_before_show: bool
) -> None:
    rig.session.value = unlocked()
    view = offer_ready(rig)
    use_queued_session_signals(rig)
    queue_lock_cycle(rig)

    deliver(rig, started())
    if deliver_before_show:
        deliver_session_signals()
    rig.recording.tick()
    if not deliver_before_show:
        deliver_session_signals()
    assert view.calls == [SHOW] and memory(rig) == "shown"
    tick_at(rig, 105_000)
    assert command_types(rig.supervisor) == [DETECTOR_START]
    assert rig.recording.offer.visible


def test_late_initial_unknown_and_known_do_not_create_a_card(rig: Rig) -> None:
    rig.session.value = MonitorSnapshot()
    view = offer_ready(rig, allowed=False)
    use_queued_session_signals(rig)
    unknown = MonitorSnapshot(active=False, revision=1)
    rig.session.value = unknown
    rig.session.changed.emit(unknown)
    allowed = unlocked(revision=2)
    rig.session.value = allowed
    rig.session.changed.emit(allowed)
    rig.recording.tick()
    deliver_session_signals()
    tick_at(rig, 105_000)
    assert command_types(rig.supervisor) == [DETECTOR_START]
    assert view.calls == []

    queue_lock_cycle(rig)
    rig.recording.tick()
    tick_at(rig, 110_000)
    assert command_types(rig.supervisor) == [DETECTOR_START, QUERY]


def test_poll_recovers_one_card_after_a_missed_lock_cycle(rig: Rig) -> None:
    rig.session.value = unlocked()
    view = offer_ready(rig)
    show_offer(rig)
    use_queued_session_signals(rig)
    queue_lock_cycle(rig)
    rig.recording.tick()
    assert view.calls == [SHOW, ("close",)] and memory(rig) == "lock_closed"
    tick_at(rig, 104_999)
    assert command_types(rig.supervisor) == [DETECTOR_START]
    tick_at(rig, 105_000)
    assert command_types(rig.supervisor) == [DETECTOR_START, QUERY]
    req = rig.recording._offer_query_req

    deliver_session_signals()
    assert rig.recording._offer_query_req == req
    deliver(rig, reply(rig, req))
    rig.recording.tick()
    assert view.calls == [SHOW, ("close",), SHOW]
    tick_at(rig, 110_000)
    assert command_types(rig.supervisor) == [DETECTOR_START, QUERY]


@pytest.mark.parametrize("reuse_monitor", [True, False])
def test_readiness_survives_only_with_the_same_monitor(rig: Rig, reuse_monitor: bool) -> None:
    rig.recording.owns_session = not reuse_monitor
    old_monitor = rig.session
    rig.session.value = unlocked(revision=10)
    view = offer_ready(rig)
    old_role = rig.supervisor
    rig.bridge.setSetting("meeting_offer_on_call", False)
    rig.recording.tick()
    assert rig.recording._monitor is None
    assert old_monitor.closes == int(not reuse_monitor)
    leave(old_role)

    if not reuse_monitor:
        rig.session = Session()
    unknown = MonitorSnapshot(
        active=False, blocked_epoch=int(reuse_monitor), revision=11 if reuse_monitor else 1
    )
    rig.session.value = unknown
    rig.bridge.setSetting("meeting_offer_on_call", True)
    rig.recording.tick()
    assert rig.recording._monitor is rig.session
    assert (rig.session is old_monitor) is reuse_monitor
    new_role = rig.supervisor
    assert new_role is not old_role
    deliver_detector(new_role, new_role.messages[-1], "meeting.detector.started")
    rig.session.value = unlocked(unknown.revision + 1, unknown.blocked_epoch)
    rig.recording.tick()
    tick_at(rig, 105_000)
    if not reuse_monitor:
        assert command_types(new_role) == [DETECTOR_START] and view.calls == []
        queue_lock_cycle(rig)
        rig.recording.tick()
        tick_at(rig, 110_000)
    assert command_types(new_role) == [DETECTOR_START, QUERY]
    deliver(rig, reply(rig, rig.recording._offer_query_req))
    rig.recording.tick()
    assert view.calls == [SHOW]


def test_w4_consumes_candidate_waiting_for_inheritance_deadline(rig: Rig) -> None:
    view = offer_ready(rig)
    show_offer(rig)
    tick_at(rig, 100_500)
    assert rig.recording.press_offer("refuse", 100_500)
    rig.recording.tick()
    replace_role(rig)
    deadline = rig.recording.offer.now_ms() + 15_000
    tick_at(rig, deadline - 1)
    rig.now = deadline / 1000
    deliver(rig, started("replacement-id", measured=deadline))
    assert rig.recording.offer.state == "idle" and memory(rig) == "refused"

    rig.bridge.requestRecording()
    assert rig.bridge.startDialogOpen and memory(rig) == "shown"
    rig.bridge.closeStartDialog()
    now = unlock(rig)
    tick_at(rig, now + 5_000)
    req = rig.recording._offer_query_req
    assert req
    deliver(rig, reply(rig, req, call_id="replacement-id"))
    rig.recording.tick()
    assert view.calls == [SHOW, ("close",)]
    deliver(rig, ended("replacement-id"))
    show_offer(rig, call_id="next-call")


def test_unlock_with_both_classes_refused_does_not_schedule_late_card(rig: Rig) -> None:
    view = offer_ready(rig)
    show_offer(rig)
    tick_at(rig, 100_500)
    assert rig.recording.press_offer("refuse", 100_500)
    rig.recording.tick()
    show_offer(rig, call_id="browser-id", app_class="browser")
    tick_at(rig, 101_000)
    assert rig.recording.press_offer("refuse", 101_000)
    rig.recording.tick()
    assert memory(rig) == memory(rig, "browser") == "refused"

    now = unlock(rig)
    tick_at(rig, now + 1_000)
    deliver(rig, ended("browser-id"))
    tick_at(rig, now + 5_000)
    assert command_types(rig.supervisor) == [DETECTOR_START]

    now = unlock(rig)
    tick_at(rig, now + 5_000)
    assert command_types(rig.supervisor) == [DETECTOR_START, QUERY]
    deliver(rig, reply(rig, rig.recording._offer_query_req, app_class="browser"))
    rig.recording.tick()
    assert view.calls[-1] == ("show", "call-browser", 60)


@pytest.mark.parametrize("cancel", ["ended-in-pump", "lock-cycle"])
def test_next_tick_checks_call_end_and_full_lock_cycle_before_start(
    rig: Rig, cancel: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig.session.value = unlocked()
    view = offer_ready(rig)
    intent = Mock()
    rig.recording.openMeetingsRequested.connect(intent)
    show_offer(rig)
    rig.now = 100.600
    assert rig.recording.press_offer("start", 100_600)
    if cancel == "ended-in-pump":
        with monkeypatch.context() as patch:
            patch.setattr(rig.supervisor, "pump", lambda: deliver(rig, ended()))
            tick_at(rig, 100_650)
        assert memory(rig) is None
    else:
        use_queued_session_signals(rig)
        queue_lock_cycle(rig)
        assert view.calls == [SHOW]
        tick_at(rig, 100_650)
        deliver_session_signals()
        assert memory(rig) == "lock_closed"
    intent.assert_not_called()
    assert view.calls == [SHOW, ("close",)]
    assert not rig.bridge.startDialogOpen
    assert command_types(rig.supervisor) == [DETECTOR_START]


def test_press_before_timeout_is_accepted_on_a_tick_after_timeout(rig: Rig) -> None:
    view = offer_ready(rig)
    intent = Mock()
    rig.recording.openMeetingsRequested.connect(intent)
    show_offer(rig)
    rig.now = 159.999
    assert rig.recording.press_offer("start", 159_999)
    assert view.calls == [SHOW]
    tick_at(rig, 160_010)
    intent.assert_called_once_with()
    assert view.calls == [SHOW, ("close",)]
    assert memory(rig) == "accepted" and rig.bridge.startDialogOpen


@pytest.mark.parametrize(("first", "second"), [("refuse", "start"), ("start", "refuse")])
def test_first_press_keeps_its_action_until_tick(rig: Rig, first: str, second: str) -> None:
    view = offer_ready(rig)
    intent = Mock()
    rig.recording.openMeetingsRequested.connect(intent)
    show_offer(rig)
    rig.now = 100.600
    assert rig.recording.press_offer(first, 100_600)
    assert not rig.recording.press_offer(second, 100_600)
    assert view.calls == [SHOW]
    tick_at(rig, 100_650)
    accepted = first == "start"
    assert view.calls == [SHOW, ("close",)]
    assert memory(rig) == ("accepted" if accepted else "refused")
    assert intent.call_count == int(accepted) and rig.bridge.startDialogOpen is accepted


@pytest.mark.parametrize("action", ["refuse", "close"])
def test_lock_cancels_queued_refusal_before_resume_card(rig: Rig, action: str) -> None:
    view = offer_ready(rig)
    show_offer(rig)
    rig.now = 100.600
    assert rig.recording.press_offer(action, 100_600)
    assert view.calls == [SHOW]
    rig.session.value = sleep_or_lock(rig, "locked")
    assert memory(rig) == "lock_closed"
    rig.recording.tick()
    tick_at(rig, 105_600)
    assert command_types(rig.supervisor) == [DETECTOR_START, QUERY]
    deliver(rig, reply(rig, rig.recording._offer_query_req))
    rig.recording.tick()
    assert view.calls == [SHOW, ("close",), SHOW]


def test_role_exit_does_not_restart_expired_inheritance(rig: Rig) -> None:
    view = offer_ready(rig)
    show_offer(rig)
    tick_at(rig, 100_500)
    assert rig.recording.press_offer("refuse", 100_500)
    rig.recording.tick()
    replace_role(rig)

    deadline = rig.recording.offer.now_ms() + 15_000
    tick_at(rig, deadline - 1)
    assert memory(rig) == "refused"
    rig.now = deadline / 1000
    replace_role(rig)
    assert memory(rig) is None
    show_offer(rig, call_id="third-role-id")
    assert view.calls == [SHOW, ("close",), SHOW]


def test_eviction_of_visible_offer_removes_id_from_both_collections(rig: Rig) -> None:
    view = offer_ready(rig)
    show_offer(rig)
    assert rig.recording.offer._memory["dion"].call_ids == {CALL}
    deliver(rig, ended(reason="evicted"))
    assert view.calls == [SHOW, ("close",)]
    assert not rig.recording.offer._calls
    assert not rig.recording.offer._memory["dion"].call_ids and memory(rig) == "shown"


def test_first_known_locked_snapshot_enables_the_real_unlock_card(rig: Rig) -> None:
    rig.session.value = replace(unlocked(), locked=True)
    view = offer_ready(rig, allowed=False)
    tick_at(rig, 105_000)
    assert command_types(rig.supervisor) == [DETECTOR_START]
    rig.session.value = unlocked(revision=2)
    rig.recording.tick()
    tick_at(rig, 110_000)
    assert command_types(rig.supervisor) == [DETECTOR_START, QUERY]
    deliver(rig, reply(rig, rig.recording._offer_query_req))
    rig.recording.tick()
    assert view.calls == [SHOW]


@pytest.fixture
def login1_publications(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[SessionState, FakeLogin, list[MonitorSnapshot]]]:
    transport = FakeLogin()
    state = SessionState(transport, uid=1000, now_mono_ms=rig.recording.offer.now_ms)
    worker = session_state._SessionWorker()
    worker.state = state
    worker._fly_environment = worker._fly_process = False
    publications: list[MonitorSnapshot] = []

    def publish(snapshot: MonitorSnapshot) -> None:
        assert worker.snapshot is snapshot
        publications.append(snapshot)
        rig.session.value = snapshot
        rig.session.changed.emit(snapshot)

    def state_changed() -> None:
        with monkeypatch.context() as patch:
            patch.setattr(session_state, "_fly_process_present", lambda: False)
            worker._changed()

    worker.updated.connect(publish)
    state.changed.connect(state_changed)
    try:
        state.start()
        transport.complete()
        assert rig.session.snapshot().allowed
        yield state, transport, publications
    finally:
        state.changed.disconnect(state_changed)
        state.stop()


def login1_lock_cycle(transport: FakeLogin) -> None:
    transport.signal(SESSION_PATH, "Lock")
    transport.signal(SESSION_PATH, "Unlock")
    transport.complete()


def test_login1_refresh_revokes_visible_offer_without_changing_locked(
    rig: Rig, login1_publications: tuple[SessionState, FakeLogin, list[MonitorSnapshot]]
) -> None:
    state, transport, publications = login1_publications
    view = offer_ready(rig)
    show_offer(rig)
    before = publications[-1]
    assert before.known and before.allowed

    state.refresh()
    unknown = publications[-1]
    assert not unknown.known and not unknown.allowed
    assert unknown.locked is before.locked
    rig.recording.tick()
    assert view.calls == [SHOW, ("close",)] and memory(rig) == "lock_closed"
    assert unknown.revision == before.revision + 1

    transport.complete()
    restored = publications[-1]
    assert restored.known and restored.allowed and restored.revision == unknown.revision + 1
    tick_at(rig, 105_000)
    assert command_types(rig.supervisor) == [DETECTOR_START, QUERY]
    deliver(rig, reply(rig, rig.recording._offer_query_req))
    rig.recording.tick()
    assert view.calls == [SHOW, ("close",), SHOW]


def test_poll_recovers_one_card_after_three_login1_publications(
    rig: Rig, login1_publications: tuple[SessionState, FakeLogin, list[MonitorSnapshot]]
) -> None:
    _, transport, publications = login1_publications
    view = offer_ready(rig)
    show_offer(rig)
    use_queued_session_signals(rig)
    before = publications[-1]

    login1_lock_cycle(transport)
    assert [(snap.known, snap.locked, snap.allowed) for snap in publications[-3:]] == [
        (True, True, False),
        (False, True, False),
        (True, False, True),
    ]
    assert [snap.revision for snap in publications[-3:]] == [
        before.revision + 1,
        before.revision + 2,
        before.revision + 3,
    ]
    assert view.calls == [SHOW]
    rig.recording.tick()
    assert view.calls == [SHOW, ("close",)]
    deliver_session_signals()
    tick_at(rig, 104_999)
    assert command_types(rig.supervisor) == [DETECTOR_START]
    tick_at(rig, 105_000)
    assert command_types(rig.supervisor) == [DETECTOR_START, QUERY]
    deliver(rig, reply(rig, rig.recording._offer_query_req))
    rig.recording.tick()
    assert view.calls == [SHOW, ("close",), SHOW]
    tick_at(rig, 110_000)
    assert command_types(rig.supervisor) == [DETECTOR_START, QUERY]


def test_inflight_card_is_replaced_after_another_login1_lock_cycle(
    rig: Rig, login1_publications: tuple[SessionState, FakeLogin, list[MonitorSnapshot]]
) -> None:
    _, transport, _ = login1_publications
    view = offer_ready(rig)
    use_queued_session_signals(rig)
    login1_lock_cycle(transport)
    rig.recording.tick()
    deliver_session_signals()
    tick_at(rig, 105_000)
    assert command_types(rig.supervisor) == [DETECTOR_START, QUERY]
    first_request = rig.recording._offer_query_req
    assert first_request

    rig.now = 105.100
    login1_lock_cycle(transport)
    # Observe the new revision before the reply, while the old request is in flight.
    rig.recording.tick()
    deliver_session_signals()
    deliver(rig, reply(rig, first_request))
    rig.recording.tick()
    assert view.calls == []
    tick_at(rig, 110_099)
    assert command_types(rig.supervisor) == [DETECTOR_START, QUERY]
    tick_at(rig, 110_100)
    assert command_types(rig.supervisor) == [DETECTOR_START, QUERY, QUERY]
    new_request = rig.recording._offer_query_req
    assert new_request and new_request != first_request
    deliver(rig, reply(rig, new_request))
    rig.recording.tick()
    assert view.calls == [SHOW]
    tick_at(rig, 115_100)
    assert command_types(rig.supervisor) == [DETECTOR_START, QUERY, QUERY]
