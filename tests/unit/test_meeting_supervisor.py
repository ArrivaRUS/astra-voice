"""Meeting supervision without processes, sockets, Qt or a real bus."""

from __future__ import annotations

import json
import signal
import struct
import subprocess
from typing import Any
from unittest.mock import Mock

import pytest
from test_meeting_ipc import CAPTURE, EVENTS, IDENTITY, MID, ack, command

from astra_voice.worker import ipc
from astra_voice.worker.supervisor import WorkerSupervisor, _correlation

pytestmark = pytest.mark.unit


class Clock:
    now = 10.0

    def __call__(self) -> float:
        return self.now


def detached(role: str = "meeting-capture") -> tuple[WorkerSupervisor, list[dict[str, Any]], Clock]:
    clock = Clock()
    events: list[dict[str, Any]] = []
    supervisor = WorkerSupervisor(events.append, role=role, clock=clock, use_qt=False)
    supervisor.state = "running"
    supervisor._receive(ipc.encode(ipc.make_hello(role=role), role=role), 1)
    supervisor.set_meeting_generation(MID, 7)
    events.clear()
    return supervisor, events, clock


def receive(s: WorkerSupervisor, message: dict[str, Any], generation: int = 1) -> None:
    s._receive(ipc.encode(message, role=s.role), generation)


def test_req_correlation_and_only_exact_ack_finishes() -> None:
    s, events, _ = detached()
    first = command(s.role, "meeting.capture.keepalive", "one")
    second = command(s.role, "meeting.capture.keepalive", "two")
    assert _correlation(first) == ("req", "one", first["type"])
    s.send(first)
    s.send(second)
    receive(s, {**ack("meeting.ack", "meeting.capture.keepalive", **CAPTURE), "req": "one"})
    assert set(s._pending) == {("req", "two", second["type"])}
    assert events[-1]["req"] == "one"
    receive(s, {**ack("pong", "ping"), "req": "two"})
    assert len(s._pending) == 1 and s.dropped_late == 1


def test_live_timeout_only_one_wait_and_heartbeat_does_not_extend() -> None:
    s, events, clock = detached()
    s.send(command(s.role, "meeting.capture.keepalive", "one"))
    clock.now += 0.5
    s.send(command(s.role, "meeting.capture.keepalive", "two"))
    receive(s, EVENTS[s.role][2])
    clock.now += 0.5
    s._expire()
    assert events[-1]["code"] == "timeout" and events[-1]["req"] == "one"
    assert set(s._pending) == {("req", "two", "meeting.capture.keepalive")}
    assert s.generation == 1


def test_kill_is_not_closed_and_no_replay_after_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    s, events, clock = detached()
    process = Mock(spec=subprocess.Popen)
    process.poll.return_value = None
    process.wait.side_effect = subprocess.TimeoutExpired("fake-role", 0.2)
    s.process = process
    launch = Mock()
    monkeypatch.setattr(s, "_launch", launch)
    s.send(command(s.role, "meeting.capture.stop"))
    receive(s, EVENTS[s.role][2])  # Even a live role cannot postpone physical close.
    clock.now += 1
    s._expire()
    process.kill.assert_called_once_with()
    assert s.close_state == "closing-unknown" and s.restarting and not s.exited
    assert events[-1]["type"] == "meeting.capture.alive"
    assert s.generation == 1
    with pytest.raises(RuntimeError):
        s.send(command(s.role, "meeting.capture.open", "next"))
    process.poll.return_value = -signal.SIGKILL
    s.pump()
    assert s.close_state == "closed" and s.exited
    launch.assert_not_called()  # Capture never auto-restarts.
    assert s.meeting_generations[MID] == 7
    assert not any(e["type"] == "meeting.capture.finalized" for e in events)


def test_closed_then_finalized_are_distinct_deadlines() -> None:
    s, events, clock = detached()
    s.send(command(s.role, "meeting.capture.stop"))
    receive(s, ack("meeting.capture.closed", "meeting.capture.stop", **CAPTURE, tracks=["mic"]))
    assert s.close_state == "closed" and s._pending
    clock.now += 1.1
    s._expire()
    assert s._pending and not s.restarting
    receive(
        s,
        ack(
            "meeting.capture.finalized",
            "meeting.capture.stop",
            **CAPTURE,
            segments={"mic": 1, "far": 0},
            intervals=[],
            status="succeeded",
        ),
    )
    assert not s._pending and s.close_state == "finalized"


def test_old_process_and_meeting_generation_cannot_touch_pending() -> None:
    s, events, _ = detached("meeting-transcribe")
    s.send(command(s.role, "meeting.job.credit"))
    before = dict(s._pending)
    event = EVENTS[s.role][1]
    receive(s, event, generation=0)
    receive(s, {**event, "meeting_generation": 6})
    assert not events and s._pending == before
    receive(s, event)
    assert events[-1]["meeting_generation"] == 7 and s._pending == before


def test_idempotent_requests_conflict_and_cached_outcome() -> None:
    s, events, clock = detached()
    request = command(s.role, "meeting.capture.keepalive")
    s.send(request)
    original = next(iter(s._pending.values())).deadline
    clock.now += 0.1
    s.send(request)
    assert next(iter(s._pending.values())).deadline == original
    s.send({**request, "seq": 2})
    assert events[-1]["code"] == "req-conflict" and len(s._pending) == 1
    response = ack("meeting.ack", "meeting.capture.keepalive", **CAPTURE)
    receive(s, response)
    s.send(request)
    assert events[-1] == {**response, "generation": 1} and not s._pending


@pytest.mark.parametrize("field,value", [("family", "foreign"), ("protocol", 4)])
def test_incompatible_hello_terminal_without_retry(field: str, value: object) -> None:
    s, events, _ = detached()
    body = json.dumps({**ipc.make_hello(role=s.role), field: value}).encode()
    s._receive(struct.pack(">I", len(body)) + body, 1)
    assert s.state == "stopped" and not s.ready
    assert events[-1]["code"] == ipc.PROTOCOL_MISMATCH and not s._restarts


def test_missing_hello_terminal_at_three_seconds() -> None:
    clock = Clock()
    events: list[dict[str, Any]] = []
    s = WorkerSupervisor(events.append, role="meeting-transcribe", clock=clock, use_qt=False)
    s.state = "running"
    clock.now += 3
    s._expire()
    assert str(s.state) == "stopped" and events[-1]["code"] == "hello-timeout"


def test_long_operation_ack_has_independent_absolute_timeout() -> None:
    s, events, clock = detached("meeting-diar")
    s.send(command(s.role, "diar.block"))
    receive(s, ack("meeting.ack", "diar.block", **IDENTITY))
    deadline = next(iter(s._pending.values())).deadline
    assert deadline == clock.now + 360  # min(900,max(120,3*1200*.1))
    clock.now += 360
    s._expire()
    assert events[-1]["code"] == "timeout" and not s._pending


def test_transcribe_restart_preserves_meeting_generation_but_never_replays(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    s, events, _ = detached("meeting-transcribe")
    s.send(command(s.role, "meeting.job.start"))
    process = Mock(spec=subprocess.Popen)
    process.poll.return_value = -9
    s.process = process
    launch = Mock()
    monkeypatch.setattr(s, "_launch", launch)
    s._restart()
    assert s.generation == 2 and s.meeting_generations[MID] == 7
    assert not s._pending and not s._outgoing
    launch.assert_called_once_with()


def test_unknown_role_rejected_before_process_work() -> None:
    with pytest.raises(ValueError):
        WorkerSupervisor(lambda e: None, role="unknown", use_qt=False)


def test_stub_capture_error_is_received_end_to_end() -> None:
    from astra_voice.worker.meeting_roles import MeetingRole

    s, events, _ = detached()
    request = command(s.role, "meeting.capture.open")
    s.send(request)
    reply = MeetingRole(s.role, clock=lambda: 10.0).handle(request)[0]
    receive(s, reply)
    assert events[-1]["code"] == "admission-stale" and not s._pending


@pytest.mark.parametrize(
    "role,kind", [("meeting-diar", "diar.block"), ("meeting-capture", "meeting.capture.stop")]
)
def test_duplicate_long_receipt_never_substitutes_final_or_closed(role: str, kind: str) -> None:
    s, _, _ = detached(role)
    s.send(command(role, kind))
    receipt = ack("meeting.ack", kind, **(CAPTURE if role == "meeting-capture" else IDENTITY))
    receive(s, receipt)
    first = next(iter(s._pending.values())).deadline
    receive(s, receipt)
    assert len(s._pending) == 1 and next(iter(s._pending.values())).deadline == first
    if role == "meeting-capture":
        assert s._meeting_close_deadlines and s.close_state == "closing"


@pytest.mark.parametrize("changed_generation", [6, 8])
def test_req_conflict_cannot_change_meeting_generation_or_original_wait(
    changed_generation: int,
) -> None:
    s, events, _ = detached("meeting-transcribe")
    request = command(s.role, "meeting.job.start")
    s.send(request)
    original = dict(s._pending)
    s.send({**request, "meeting_generation": changed_generation})
    assert events[-1]["code"] == "req-conflict"
    assert s.meeting_generations[MID] == 7 and s._pending == original


def test_incoming_command_cannot_ack_capture_close() -> None:
    s, events, _ = detached()
    request = command(s.role, "meeting.capture.stop")
    s.send(request)
    receive(s, request)
    assert s._pending and s._meeting_close_deadlines and not events


def test_stop_error_does_not_prove_physical_close() -> None:
    from astra_voice.worker.meeting_roles import MeetingRole

    s, events, _ = detached()
    process = Mock(spec=subprocess.Popen)
    process.poll.return_value = None
    process.wait.side_effect = subprocess.TimeoutExpired("fake", 0.2)
    s.process = process
    request = command(s.role, "meeting.capture.stop")
    s.send(request)
    receive(s, MeetingRole(s.role).handle(request)[0])
    assert s.close_state == "closing-unknown" and s.restarting and not s.exited
    process.kill.assert_called_once_with()
    assert not events


@pytest.mark.parametrize(
    "role,kind", [("meeting-transcribe", "meeting.job.cancel"), ("meeting-diar", "diar.cancel")]
)
def test_cancel_receipt_still_requires_cancelled(role: str, kind: str) -> None:
    s, _, _ = detached(role)
    s.send(command(role, kind))
    receive(s, ack("meeting.ack", kind, **IDENTITY))
    assert s._pending and next(iter(s._pending.values())).deadline == 11.0


def test_generation_advance_cannot_erase_physical_close_deadline() -> None:
    s, _, _ = detached()
    process = Mock(spec=subprocess.Popen)
    process.poll.return_value = None
    process.wait.side_effect = subprocess.TimeoutExpired("fake", 0.2)
    s.process = process
    s.send(command(s.role, "meeting.capture.stop"))
    s.set_meeting_generation(MID, 8)
    assert s.close_state == "closing-unknown" and s.restarting
    process.kill.assert_called_once_with()
    with pytest.raises(RuntimeError):
        s.send({**command(s.role, "meeting.capture.open", "new"), "meeting_generation": 8})


def test_diar_does_not_restart_by_default() -> None:
    s, _, _ = detached("meeting-diar")
    assert not s.auto_restart


@pytest.mark.parametrize(
    "role,kind", [("meeting-transcribe", "meeting.job.cancel"), ("meeting-diar", "diar.cancel")]
)
def test_cancel_deadline_kills_even_live_role(role: str, kind: str) -> None:
    s, _, clock = detached(role)
    process = Mock(spec=subprocess.Popen)
    process.poll.return_value = None
    process.wait.side_effect = subprocess.TimeoutExpired("fake", 0.2)
    s.process = process
    s.send(command(role, kind))
    receive(s, ack("meeting.ack", kind, **IDENTITY))
    clock.now += 1
    s._expire()
    process.kill.assert_called_once_with()
    assert s.restarting and not s.exited


@pytest.mark.parametrize(
    "role,kind,ceiling",
    [
        ("meeting-capture", "meeting.capture.prepare", 5),
        ("meeting-diar", "diar.block", 360),
        ("meeting-transcribe", "model.load", 60),
    ],
)
def test_caller_cannot_extend_operation_ceiling(role: str, kind: str, ceiling: int) -> None:
    s, _, clock = detached(role)
    request = command(role, kind)
    s.send(request, timeout=10000)
    identity = CAPTURE if "capture_id" in request else IDENTITY if "meeting_id" in request else {}
    receive(s, ack("meeting.ack", kind, **identity))
    assert next(iter(s._pending.values())).deadline == clock.now + ceiling


def test_meeting_group_kill_and_exit_callback_wait_for_poll(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    s, _, _ = detached("meeting-diar")
    process = Mock(spec=subprocess.Popen)
    process.pid = 123456
    process.poll.return_value = None
    process.wait.side_effect = subprocess.TimeoutExpired("fake", 0.2)
    s.process = process
    s._process_group = True
    kill_group = Mock()
    monkeypatch.setattr("astra_voice.worker.supervisor.os.killpg", kill_group)
    callback = Mock()
    s._on_exit = callback
    s._restart()
    kill_group.assert_called_once_with(123456, signal.SIGKILL)
    process.kill.assert_not_called()
    callback.assert_not_called()
    process.poll.return_value = -signal.SIGKILL
    s.pump()
    callback.assert_called_once_with(-signal.SIGKILL)


def test_revision_cancelled_generation_cannot_replay_cached_result() -> None:
    s, events, _ = detached("meeting-transcribe")
    request = command(s.role, "meeting.job.credit")
    s.send(request)
    receive(s, ack("meeting.ack", "meeting.job.credit", **IDENTITY))
    s.set_meeting_generation(MID, 8)
    events.clear()
    s.send(request)
    assert events == [] and not s._pending and s.meeting_generations[MID] == 8
    s.send({**request, "n": 1})
    assert events[-1]["code"] == "req-conflict"


def test_revision_limit_outcome_survives_freed_reservation(monkeypatch: pytest.MonkeyPatch) -> None:
    from astra_voice.worker import meeting_contract as mc

    monkeypatch.setattr(mc, "MAX_CACHE_BYTES", mc._REPLY_RESERVATION + 1024)
    s, events, _ = detached("meeting-transcribe")
    first = command(s.role, "meeting.job.credit", "first")
    refused = command(s.role, "meeting.job.credit", "refused")
    s.send(first)
    s.send(refused)
    rejection = events[-1]
    assert rejection["code"] == "limit"
    receive(s, {**ack("meeting.ack", "meeting.job.credit", **IDENTITY), "req": "first"})
    events.clear()
    ledger = s._meeting_ledger
    assert ledger.used_bytes + ledger.reserved_bytes + mc._REPLY_RESERVATION <= mc.MAX_CACHE_BYTES
    s.send(refused)
    assert events == [rejection] and not s._pending
    s.send({**refused, "n": 1})
    assert events[-1]["code"] == "req-conflict" and not s._pending


def test_revision_bye_waits_for_exit_and_keeps_absolute_deadline() -> None:
    s, events, clock = detached()
    process = Mock(spec=subprocess.Popen)
    process.poll.return_value = None
    process.wait.side_effect = subprocess.TimeoutExpired("fake", 0.2)
    s.process = process
    s.send(command(s.role, "meeting.capture.shutdown"))
    clock.now += 0.9
    receive(s, ack("meeting.capture.bye", "meeting.capture.shutdown"))
    assert not s._pending and not s.exited
    assert not s.ready
    before = dict(s._meeting_ledger.requests)
    with pytest.raises(RuntimeError):
        s.send(command(s.role, "meeting.capture.open", "new"))
    assert s._meeting_ledger.requests == before
    clock.now = 12.0  # Original shutdown deadline, not bye time + another 2s.
    s._expire()
    process.kill.assert_called_once_with()
    assert s.close_state == "closing-unknown" and s.restarting
    assert [event["type"] for event in events] == ["meeting.capture.bye"]
    process.poll.return_value = -signal.SIGKILL
    s.pump()
    assert s.exited and s.close_state == "closed" and not s.restarting


def test_revision_bye_confirmed_exit_is_clean_and_not_a_crash() -> None:
    s, events, _ = detached()
    process = Mock(spec=subprocess.Popen)
    process.poll.return_value = None
    s.process = process
    callback = Mock()
    s._on_exit = callback
    s.send(command(s.role, "meeting.capture.shutdown"))
    receive(s, ack("meeting.capture.bye", "meeting.capture.shutdown"))
    process.poll.return_value = 0
    s.pump()
    assert s.state == "stopped" and s.close_state == "closed" and s.exited
    assert [event["type"] for event in events] == ["meeting.capture.bye"]
    callback.assert_called_once_with(0)
    s.pump()
    callback.assert_called_once_with(0)


@pytest.mark.parametrize("action", ["stop", "kill", "pump"])
def test_revision_exit_callback_is_safe_to_reenter(
    monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    s, _, _ = detached("meeting-transcribe")
    process = Mock(spec=subprocess.Popen)
    process.poll.return_value = None
    process.wait.side_effect = subprocess.TimeoutExpired("fake", 0.2)
    s.process = process
    launch = Mock()
    monkeypatch.setattr(s, "_launch", launch)
    seen: list[int] = []

    def on_exit(code: int) -> None:
        assert not s.restarting
        seen.append(code)
        getattr(s, action)()

    s._on_exit = on_exit
    s._restart()
    process.poll.return_value = -signal.SIGKILL
    s.pump()
    assert seen == [-signal.SIGKILL] and not s.restarting
    if action in {"stop", "kill"}:
        assert s.state == "stopped" and s.generation == 1
        launch.assert_not_called()
    else:
        assert s.generation == 2
        launch.assert_called_once_with()


@pytest.mark.parametrize("action", ["stop", "kill", "restart"])
def test_revision_timeout_batch_is_finished_before_callbacks(
    monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    s, events, clock = detached("meeting-transcribe")
    s.send(command(s.role, "meeting.job.credit", "one"))
    s.send(command(s.role, "meeting.job.credit", "two"))
    receive(s, EVENTS[s.role][0])
    events.clear()
    monkeypatch.setattr(s, "_launch", Mock())

    def on_event(event: dict[str, Any]) -> None:
        events.append(event)
        if event.get("code") == "timeout" and event.get("req") == "one":
            assert not s._pending
            if action == "restart":
                s._restart()
            else:
                getattr(s, action)()

    s._on_event = on_event
    clock.now += 1
    s._expire()
    errors = [event for event in events if event.get("code") == "timeout"]
    assert [event["req"] for event in errors] == ["one", "two"]
    assert all(event["generation"] == 1 for event in errors)
    assert not s._pending


@pytest.mark.parametrize("action", ["stop", "kill"])
@pytest.mark.parametrize("delayed", [False, True])
def test_revision_terminal_capture_closes_only_after_every_exit(action: str, delayed: bool) -> None:
    s, _, _ = detached()
    process = Mock(spec=subprocess.Popen)
    retired = Mock(spec=subprocess.Popen)
    process.poll.return_value = None
    retired.poll.return_value = None
    s.process = process
    s.adopt_retired([retired])

    def wait_current(*, timeout: float) -> int:
        process.poll.return_value = 0
        return 0

    def wait_retired(*, timeout: float) -> int:
        if not delayed:
            retired.poll.return_value = 0
        return 0

    process.wait.side_effect = wait_current
    retired.wait.side_effect = wait_retired
    getattr(s, action)()
    assert s.close_state == ("closing-unknown" if delayed else "closed")
    assert s.exited is not delayed
    if delayed:
        s.pump()
        assert s.close_state == "closing-unknown" and not s.exited
        retired.poll.return_value = -signal.SIGKILL
        s.pump()
        assert s.close_state == "closed" and s.exited


def test_revision_cached_bye_replay_does_not_extend_exit_deadline() -> None:
    s, events, clock = detached()
    process = Mock(spec=subprocess.Popen)
    process.poll.return_value = None
    process.wait.side_effect = subprocess.TimeoutExpired("fake", 0.2)
    s.process = process
    request = command(s.role, "meeting.capture.shutdown")
    s.send(request)
    reply = ack("meeting.capture.bye", "meeting.capture.shutdown")
    receive(s, reply)
    clock.now += 0.5
    s.send(request)
    assert events[-1] == {**reply, "generation": 1}
    s.send(command(s.role, "ping", request["req"]))
    assert events[-1]["code"] == "req-conflict"
    assert s._meeting_exit_deadline == 12.0
    clock.now = 12.0
    s._expire()
    process.kill.assert_called_once_with()


@pytest.mark.parametrize("action", ["stop", "kill", "bye"])
def test_revision_fast_exit_updates_closed_state(action: str) -> None:
    s, _, _ = detached()
    process = Mock(spec=subprocess.Popen)
    process.poll.return_value = 0
    s.process = process
    if action == "bye":
        s.send(command(s.role, "meeting.capture.shutdown"))
        receive(s, ack("meeting.capture.bye", "meeting.capture.shutdown"))
        s.pump()
    else:
        s.close_state = "closing"
        getattr(s, action)()
    assert s.exited and s.close_state == "closed"
