"""D2: lease traffic cannot consume the ability to close capture streams."""

from __future__ import annotations

import json
import re
import struct
import time
from pathlib import Path
from typing import Any

import pytest
from test_meeting_capture import Rig, wait_until

from astra_voice.worker import ipc
from astra_voice.worker import meeting_contract as contract
from astra_voice.worker.supervisor import WorkerSupervisor

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("filesystem_euid")]


@pytest.fixture
def rig(tmp_path: Path) -> Any:
    instance = Rig(tmp_path)
    yield instance
    instance.cleanup()


def test_five_hour_lease_traffic_does_not_consume_control_ledger(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig.prepare()
    rig.open()
    events: list[dict[str, Any]] = []
    supervisor = WorkerSupervisor(
        events.append, role="meeting-capture", clock=rig.clock, use_qt=False
    )
    supervisor.state = "running"
    supervisor._receive(
        ipc.encode(ipc.make_hello(role="meeting-capture"), role="meeting-capture"), 1
    )
    supervisor.set_meeting_generation(rig.identity["meeting_id"], 0)

    def receive(replies: list[dict[str, Any]]) -> None:
        rig._identity(replies)
        for reply in replies:
            supervisor._receive(ipc.encode(reply, role="meeting-capture"), 1)

    def flush() -> None:
        while supervisor._outgoing:
            size = struct.unpack(">I", supervisor._outgoing[:4])[0]
            raw = bytes(supervisor._outgoing[4 : size + 4])
            del supervisor._outgoing[: size + 4]
            receive(rig.state.handle(ipc.decode(raw, role="meeting-capture")))

    monkeypatch.setattr(supervisor, "_flush", flush)
    # Validate each distinct heartbeat shape/value set once; the 18,000-step
    # stress still traverses real framing, supervisor and capture state/ledger.
    # Only req/seq vary, checked below; all other traffic uses the real schema.
    validate = contract.validate_message
    validated: set[str] = set()

    def validate_repeated(message: dict[str, Any], role: str) -> dict[str, Any]:
        if message.get("request_type") == "meeting.capture.keepalive" and message["type"] in {
            "meeting.capture.keepalive",
            "meeting.ack",
        }:
            assert re.fullmatch(r"[A-Za-z0-9_-]{1,64}", message["req"])
            if "seq" in message:
                assert type(message["seq"]) is int and message["seq"] >= 0
            shape = json.dumps(
                {key: value for key, value in message.items() if key not in {"req", "seq"}},
                sort_keys=True,
            )
            if shape in validated:
                return dict(message)
            validated.add(shape)
        return validate(message, role)

    monkeypatch.setattr(contract, "validate_message", validate_repeated)
    sequence = 0

    def send(action: str, **fields: Any) -> None:
        nonlocal sequence
        sequence += 1
        kind = "meeting.capture." + action
        supervisor.send(
            {
                "type": kind,
                "request_type": kind,
                "req": f"wire-{sequence}",
                **rig.identity,
                **fields,
            }
        )
        assert events[-1]["type"] != "error", events[-1]

    def drain_until(kind: str) -> None:
        def ready() -> bool:
            receive(rig.state.poll())
            return any(event["type"] == kind for event in events)

        wait_until(ready)
        events.clear()

    for limit in (10800000, 14400000):
        send("extend", max_ms=limit, blocked_epoch=0, admission_mono_ms=100000)
    heartbeat = 0
    for phase, seconds in (("recording", 7200), ("paused", 3600), ("recording", 7200)):
        for second in range(seconds):
            rig.clock.now += 1
            heartbeat += 1
            send("keepalive", seq=heartbeat)
            assert events[-1]["type"] == "meeting.ack"
            events.clear()
            if second % 100 == 0:
                receive(rig.state.poll())
                events.clear()
        if phase == "paused":
            send("resume", blocked_epoch=0, admission_mono_ms=int(rig.clock() * 1000))
            drain_until("meeting.capture.started")
        elif heartbeat == 7200:
            send("pause", reason="user")
            drain_until("meeting.capture.finalized")
    assert heartbeat == 18000
    before = time.monotonic()
    send("stop")
    drain_until("meeting.capture.closed")
    assert time.monotonic() - before < 1
    assert not rig.state.sources
    assert len(rig.state.ledger.requests) < 16
    assert len(supervisor._meeting_ledger.requests) < 16
    assert len(supervisor._finished) < 16


@pytest.mark.parametrize("exhausted", [False, True])
@pytest.mark.parametrize("action", ["pause", "stop", "shutdown"])
def test_closing_controls_survive_full_and_exhausted_ledger(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, action: str, exhausted: bool
) -> None:
    rig.prepare()
    rig.open()
    monkeypatch.setattr(contract, "MAX_REQUESTS", 0)
    if exhausted:
        monkeypatch.setattr(contract, "MAX_REJECTIONS", 1)
        for request in ("refuse-1", "refuse-2"):
            assert (
                rig.state.handle({"type": "ping", "request_type": "ping", "req": request})[0][
                    "code"
                ]
                == "limit"
            )
        assert rig.state.ledger.exhausted
    before = time.monotonic()
    replies = rig.send(action, **({"reason": "user"} if action == "pause" else {}))
    assert replies[0]["type"] == "meeting.ack", replies
    rig.until("meeting.capture.closed")
    assert time.monotonic() - before < 1
    assert not rig.state.sources


def test_exhaustion_preserves_known_request_outcome_and_conflict() -> None:
    ledger = contract.RequestLedger()
    command = {"type": "ping", "request_type": "ping", "req": "known"}
    outcome = [{"type": "pong", "request_type": "ping", "req": "known"}]
    assert ledger.begin(command)[0] == "new"
    ledger.finish("known", outcome)
    ledger.exhausted = True
    assert ledger.begin(command) == ("same", outcome)
    assert (
        ledger.begin(
            {**command, "type": "meeting.call.query", "request_type": "meeting.call.query"}
        )[0]
        == "conflict"
    )


def test_keepalive_keeps_wire_validation_and_conflicts_with_retained_request(rig: Rig) -> None:
    rig.prepare()
    rig.open()
    ping = {"type": "ping", "request_type": "ping", "req": "known"}
    assert rig.state.handle(ping)[0]["type"] == "pong"
    heartbeat = {
        **rig.identity,
        "type": "meeting.capture.keepalive",
        "request_type": "meeting.capture.keepalive",
        "req": "known",
        "seq": 1,
    }
    assert rig.state.handle(heartbeat)[0]["code"] == "req-conflict"
    with pytest.raises(ipc.FrameError):
        rig.state.handle({**heartbeat, "request_type": "meeting.capture.stop"})


def test_emergency_closing_waiters_are_bounded(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    rig.prepare()
    rig.open()
    monkeypatch.setattr(contract, "MAX_REQUESTS", 0)
    # Leave the physical close message queued, as a blocked archive would do
    # after close; repeated closing commands must not allocate a lifetime cache.
    for index in range(100):
        replies = rig.send(
            "pause" if index % 2 == 0 else "stop", **({"reason": "user"} if index % 2 == 0 else {})
        )
        assert replies[0]["type"] == "meeting.ack"
    assert len(rig.state.close_waiters) <= 2
    assert len(rig.state.pending) <= 2
    rig.until("meeting.capture.closed")
    rig.until("meeting.capture.finalized")


def test_unknown_regular_completion_is_not_silently_ignored() -> None:
    ledger = contract.RequestLedger()
    with pytest.raises(KeyError):
        ledger.finish("unknown", [{"type": "pong", "req": "unknown", "request_type": "ping"}])


@pytest.mark.parametrize("exhausted", [False, True])
@pytest.mark.parametrize("action", ["pause", "stop", "shutdown"])
def test_supervisor_closing_controls_survive_full_and_exhausted_ledger(
    monkeypatch: pytest.MonkeyPatch, action: str, exhausted: bool
) -> None:
    from test_meeting_ipc import command
    from test_meeting_supervisor import detached

    supervisor, events, _ = detached()
    monkeypatch.setattr(supervisor, "_flush", supervisor._outgoing.clear)
    monkeypatch.setattr(contract, "MAX_REQUESTS", 0)
    monkeypatch.setattr(contract, "MAX_REJECTIONS", 1)
    supervisor.send(command(supervisor.role, "ping", "refuse-1"))
    if exhausted:
        supervisor.send(command(supervisor.role, "ping", "refuse-2"))
        assert supervisor._meeting_ledger.exhausted
    events.clear()
    supervisor.send(command(supervisor.role, "meeting.capture." + action, "close"))
    assert not events
    assert any(request["req"] == "close" for request in supervisor._meeting_requests.values())
    assert supervisor.state == "running"


def test_supervisor_emergency_closing_pending_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    from test_meeting_ipc import command
    from test_meeting_supervisor import detached

    supervisor, events, clock = detached()
    monkeypatch.setattr(supervisor, "_flush", supervisor._outgoing.clear)
    monkeypatch.setattr(contract, "MAX_REQUESTS", 0)
    for index in range(100):
        supervisor.send(command(supervisor.role, "meeting.capture.stop", f"close-{index}"))
        clock.now += 0.001
        assert not events
    assert len(supervisor._pending) == 1
    assert len(supervisor._meeting_requests) == 1
    assert list(supervisor._meeting_close_deadlines.values()) == [11.0]


def test_supervisor_stateless_heartbeat_has_bounded_inflight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from test_meeting_ipc import command
    from test_meeting_supervisor import detached

    supervisor, events, _ = detached()
    monkeypatch.setattr(supervisor, "_flush", supervisor._outgoing.clear)
    for index in range(100):
        supervisor.send(command(supervisor.role, "meeting.capture.keepalive", f"lease-{index}"))
    assert len(supervisor._meeting_requests) <= 16
    assert not supervisor._meeting_ledger.requests
    events.clear()
    supervisor.send(command(supervisor.role, "meeting.capture.stop", "stop"))
    assert not events
    assert any(request["req"] == "stop" for request in supervisor._meeting_requests.values())


def test_supervisor_keepalive_correlation_only_after_ack() -> None:
    from test_meeting_ipc import CAPTURE, ack, command
    from test_meeting_supervisor import detached, receive

    supervisor, events, clock = detached()
    heartbeat = command(supervisor.role, "meeting.capture.keepalive", "lease")
    supervisor.send(heartbeat)
    deadline = next(iter(supervisor._pending.values())).deadline
    clock.now += 0.1
    supervisor.send(heartbeat)
    assert next(iter(supervisor._pending.values())).deadline == deadline
    supervisor.send({**heartbeat, "seq": 2})
    assert events[-1]["code"] == "req-conflict"
    receive(supervisor, {**ack("meeting.ack", heartbeat["type"], **CAPTURE), "req": "lease"})
    assert not supervisor._pending and not supervisor._meeting_ledger.requests
    supervisor.send({**heartbeat, "seq": 2})
    assert next(iter(supervisor._pending.values())).deadline != deadline
