"""E13.M8 4v: fresh role checks, public frames and private call lifetimes."""

from __future__ import annotations

import ast
import logging
import os
import queue
import re
import socket
import struct
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

import pytest
from test_meeting_capture import Rig
from test_meeting_m8_detector_source import Backend as DetectorBackend
from test_meeting_m8_identity_check import (
    APP,
    CLIENT,
    COOKIE,
    EXE,
    KEY,
    MIC,
    OUT,
    PID,
    PROGRAM,
    Harness,
    Pulse,
    extra_claimant,
    picture,
)
from test_meeting_m8_identity_check import h as h
from test_meeting_m8_locality import ObserverPulse, ObserverPulses, detector
from test_meeting_m8_repr import repr_without_object_addresses
from test_meeting_m8_role_observer import ObserverSteps
from test_meeting_m8_ss import SERVER
from test_meeting_supervisor import detached, receive

from astra_voice.meetings.apps import AppClass, AppDataError, AppDefinition
from astra_voice.meetings.detector_rule import Candidate
from astra_voice.meetings.events import DetectorCode
from astra_voice.meetings.identity import ProcessKey, Reason
from astra_voice.worker import (
    ipc,
    meeting_call_service,
    meeting_capture,
    meeting_detector,
    meeting_identity_check,
    pulse_stream,
)
from astra_voice.worker.meeting_audio_local import CONNECT_LOCK, LocalAudioServer
from astra_voice.worker.meeting_call_service import result_code, role_checker
from astra_voice.worker.meeting_detector import (
    Direction,
    EventKind,
    MetadataDisconnected,
    NormalizedEvent,
    PictureTiming,
    ProgramKey,
    StreamKey,
)
from astra_voice.worker.meeting_identity_check import (
    CHECK_SECONDS,
    CallAnchor,
    CheckRequest,
    CheckResult,
    PinnedStream,
    PinnedTarget,
    Purpose,
    Unsettled,
    VerificationCode,
    _Member,
)
from astra_voice.worker.meeting_observer import ObserverConnect, ObserverServer, observer_metadata
from astra_voice.worker.pulse_stream import PA_CONTEXT_FAILED

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("filesystem_euid")]
LOCAL = LocalAudioServer(b"unix:" + SERVER, "pulse_server")
MEMBER = _Member(Direction.MICROPHONE, MIC, 3030, CLIENT, 2727, KEY)
ANCHOR = CallAnchor(PROGRAM, COOKIE, frozenset({KEY}), frozenset({MEMBER}), False)
TARGET = PinnedTarget(COOKIE, (PinnedStream(OUT, 3131, CLIENT, 2727, KEY),), 98.765)
ALLOWED = CheckResult(True, None, AppClass.DION, True, True, (), 98765, 1, ANCHOR, TARGET)
DENIED = CheckResult(True, None, AppClass.OTHER, False, False, (), 0, 1, None, None)
QUERY = "meeting.call.query"


def request(req: str) -> dict[str, str]:
    return {"type": QUERY, "request_type": QUERY, "req": req}


def wire(events: list[dict[str, Any]]) -> list[bytes]:
    return [ipc.encode(event, role="meeting-capture") for event in events]


def of_type(events: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    return [event for event in events if event["type"] == "meeting." + kind]


def diagnostic(purpose: str, code: str, duration: int = 0) -> dict[str, Any]:
    return {
        "type": "meeting.detector.code",
        "purpose": purpose,
        "code": code,
        "duration_ms": duration,
    }


def ended(call_id: str, reason: str) -> dict[str, Any]:
    return {"type": "meeting.call.ended", "call_id": call_id, "reason": reason}


def refused(req: str) -> dict[str, Any]:
    return {"type": "meeting.call.state", "request_type": QUERY, "req": req, "active": False}


def candidate(rig: Rig, *, reset: bool = False) -> Candidate:
    rule = rig.state.observer.rule
    with rig.state.observer.rule_lock:
        now = rig.clock.now
        rule.update(
            (NormalizedEvent(EventKind.RESET, now),) * int(reset)
            + tuple(
                NormalizedEvent(
                    EventKind.UPSERT,
                    now,
                    StreamKey(1, direction, index, index, index),
                    PROGRAM,
                    corked=False,
                )
                for direction, index in ((Direction.OUTPUT, OUT), (Direction.MICROPHONE, MIC))
            )
        )
        rig.clock.now += 6  # A full second past the qualification boundary.
        (decision,) = rule.update()
        assert isinstance(decision, Candidate)
        return decision


def observation(rig: Rig, kind: str, **fields: Any) -> None:
    observer = rig.state.observer
    epoch = observer.epoch
    if kind == "call-ended":
        with observer.rule_lock:
            observer._call_ended(fields["program"], epoch)
    else:
        rig.state.messages.put({"type": "detector-" + kind, "epoch": epoch, **fields})


def server_observation(rig: Rig, cookie: int) -> None:
    observation(rig, "server-changed", server=ObserverServer(cookie))


def on_server(rig: Rig, cookie: int) -> CheckResult:
    return replace(
        ALLOWED,
        anchor=replace(ANCHOR, server_cookie=cookie),
        measured_mono_ms=int(rig.clock() * 1000),
    )


def submit_candidate(rig: Rig, decision: Candidate | None = None) -> list[dict[str, Any]]:
    decision = decision or candidate(rig)
    observation(rig, "candidate", program=decision.program, offered=decision.offered)
    return rig.poll()


class SignalQueue(queue.SimpleQueue[dict[str, Any]]):
    def __init__(self, kind: str) -> None:
        super().__init__()
        self.kind = kind
        self.arrived = threading.Event()

    def put(
        self, item: dict[str, Any], /, block: bool = True, timeout: float | None = None
    ) -> None:
        super().put(item, block, timeout)
        if item["type"] == self.kind:
            self.arrived.set()


@dataclass
class Run:
    result: CheckResult | Exception
    release: threading.Event = field(default_factory=threading.Event)
    entered: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None
    request: CheckRequest | None = None
    timed_out: bool = False

    def started(self) -> CheckRequest:
        assert self.entered.wait(2), "check did not start"
        assert self.thread is not None and self.thread.daemon
        assert self.thread is not threading.current_thread()
        assert self.request is not None
        return self.request

    def complete(self) -> None:
        self.started()
        self.release.set()
        assert self.thread is not None
        self.thread.join(2)
        assert not self.thread.is_alive() and not self.timed_out


class CheckDouble:
    def __init__(self) -> None:
        self.runs: list[Run] = []
        self.requests: list[CheckRequest] = []
        self.created: list[threading.Thread] = []
        self.deadlines: list[float] = []
        self.shutdown_results: list[bool] = []
        self.shutdown_default = True

    def factory(self, *_: object) -> CheckDouble:
        self.created.append(threading.current_thread())
        return self

    def plan(self, result: CheckResult | Exception = ALLOWED) -> Run:
        run = Run(result)
        self.runs.append(run)
        return run

    def run(self, request: CheckRequest) -> CheckResult:
        index = len(self.requests)
        self.requests.append(request)
        run = self.runs[index]
        run.request = request
        run.thread = threading.current_thread()
        run.entered.set()
        run.timed_out = not run.release.wait(2)
        if isinstance(run.result, Exception):
            raise run.result
        return run.result

    def shutdown(self, deadline: float) -> bool:
        self.deadlines.append(deadline)
        return self.shutdown_results.pop(0) if self.shutdown_results else self.shutdown_default

    def release_all(self) -> None:
        for run in self.runs:
            run.release.set()
        for run in self.runs:
            if run.thread is not None and run.thread is not threading.current_thread():
                run.thread.join(2)
                assert not run.thread.is_alive()


@pytest.fixture
def service(tmp_path: Path) -> Iterator[tuple[Rig, CheckDouble]]:
    checks = CheckDouble()
    rig = Rig(tmp_path, observer_address=lambda: LOCAL, check_factory=checks.factory)
    rig.state.observer.registry.set("observer", (42, COOKIE))
    try:
        yield rig, checks
    finally:
        checks.release_all()
        rig.cleanup()


def checked_card(
    rig: Rig, checks: CheckDouble, req: str, outcome: CheckResult = ALLOWED
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    run = checks.plan(outcome)
    assert [item["type"] for item in rig.state.handle(request(req))] == ["meeting.ack"]
    run.complete()
    events = rig.poll()
    (card,) = of_type(events, "call.state")
    assert card["req"] == req
    return card, events


@pytest.mark.parametrize(
    ("outcome", "code"),
    [
        (ALLOWED, None),
        (replace(ALLOWED, app_class=AppClass.BROWSER, measured_mono_ms=12345), None),
        (replace(ALLOWED, variant1=False, journal=(Reason.COUNTER,)), "identity_mismatch_counter"),
        (replace(ALLOWED, journal=(Reason.EXE,)), "identity_mismatch"),
        (replace(ALLOWED, variant1=False, unsettled=Unsettled.PICTURE_CHANGED), "picture_changed"),
    ],
)
def test_offer_projects_result_and_marks_once(
    service: tuple[Rig, CheckDouble], outcome: CheckResult, code: str | None
) -> None:
    rig, checks = service
    run = checks.plan(outcome)
    decision = candidate(rig)
    start = rig.clock()
    assert submit_candidate(rig, decision) == []
    assert run.started() == CheckRequest(Purpose.OFFER, PROGRAM, None, start + CHECK_SECONDS)
    assert rig.state.observer.rule.candidate(PROGRAM) == Candidate(PROGRAM, False)
    rig.clock.now += 0.45
    run.complete()
    events = rig.poll()
    assert rig.clock() - start <= CHECK_SECONDS + 0.1
    (started,) = of_type(events, "call.started")
    assert ipc.decode(wire([started])[0][4:], role="meeting-capture") == {
        "type": "meeting.call.started",
        "call_id": started["call_id"],
        "app_class": outcome.app_class,
        "variant1": outcome.variant1,
        "measured_mono_ms": outcome.measured_mono_ms,
    }
    assert re.fullmatch("[0-9a-f]{32}", started["call_id"])
    assert rig.state.calls.anchor_for(started["call_id"]) is ANCHOR
    assert rig.state.observer.rule.candidate(PROGRAM) == Candidate(PROGRAM, True)
    codes = of_type(events, "detector.code")
    assert [item["code"] for item in codes] == ([code] if code else [])
    if codes:
        assert codes[0]["duration_ms"] == pytest.approx(450, abs=1)
    rig.clock.now += 2
    assert not of_type(submit_candidate(rig, decision), "call.started")
    assert checks.requests == [run.request]
    assert not rig.state.calls._offers


@pytest.mark.parametrize(
    "code", [Unsettled.PICTURE_CHANGED, Unsettled.BUDGET, Unsettled.BUSY, Unsettled.PULSE_FAILED]
)
def test_offer_retries_three_fresh_deadlines_then_resets_for_next_candidate(
    service: tuple[Rig, CheckDouble], code: Unsettled
) -> None:
    rig, checks = service
    denied = replace(DENIED, settled=False, unsettled=code)
    runs = [checks.plan(denied) for _ in range(4)]
    decision = candidate(rig)
    assert submit_candidate(rig, decision) == []
    for attempt, run in enumerate(runs[:3], start=1):
        assert run.started().deadline == rig.clock() + CHECK_SECONDS
        rig.clock.now += 0.125
        run.complete()
        events = rig.poll()
        assert not of_type(events, "call.started")
        assert of_type(events, "detector.code") == [diagnostic("offer", code, 125)]
        rig.clock.now += 0.75
        rig.poll()
        assert len(checks.requests) == attempt
        rig.clock.now += 0.25
        rig.poll()
    rig.clock.now += 2
    assert rig.poll() == []
    assert len(checks.requests) == 3
    assert not rig.mics and not rig.backend.sources and not rig.backend.prepare_calls
    observation(rig, "call-ended", program=PROGRAM)
    assert rig.poll() == []
    submit_candidate(rig, candidate(rig, reset=True))
    assert runs[3].started().deadline == rig.clock() + CHECK_SECONDS
    runs[3].complete()
    assert not of_type(rig.poll(), "call.started")
    assert not rig.mics and not rig.backend.sources and not rig.backend.prepare_calls


def test_busy_retry_publishes_one_offer_without_opening_sources(
    service: tuple[Rig, CheckDouble],
) -> None:
    rig, checks = service
    busy = checks.plan(replace(DENIED, unsettled=Unsettled.BUSY))
    allowed = checks.plan()
    decision = candidate(rig)
    submit_candidate(rig, decision)
    busy.complete()
    assert rig.poll() == [diagnostic("offer", "busy")]
    rig.clock.now += 0.75
    assert rig.poll() == [] and len(checks.requests) == 1
    rig.clock.now += 0.25
    assert rig.poll() == []
    allowed.complete()
    (started,) = of_type(rig.poll(), "call.started")
    assert rig.state.calls.anchor_for(started["call_id"]) is ANCHOR
    assert submit_candidate(rig, decision) == []
    assert len(checks.requests) == 2
    assert not rig.mics and not rig.backend.sources and not rig.backend.prepare_calls


def test_prepare_failure_cancels_scheduled_offer_retry(
    service: tuple[Rig, CheckDouble], monkeypatch: pytest.MonkeyPatch
) -> None:
    rig, checks = service
    busy = checks.plan(replace(DENIED, unsettled=Unsettled.BUSY))
    submit_candidate(rig)
    busy.complete()
    assert rig.poll() == [diagnostic("offer", "busy")]
    inbox = SignalQueue("prepare-failed")
    with monkeypatch.context() as patch:
        patch.setattr(rig.state, "messages", inbox)
        patch.setattr(rig.state, "free_mb", lambda: 599)
        assert rig.request_prepare({"kind": "none"})[0]["type"] == "meeting.ack"
        assert inbox.arrived.wait(2)
        (error,) = rig.poll()
        assert error["type"] == "error" and error["code"] == "limit"
        rig.clock.now += 1
        assert rig.poll() == []
        assert len(checks.requests) == 1


def test_two_busy_retries_then_server_change_do_not_launch_fourth_offer(
    service: tuple[Rig, CheckDouble],
) -> None:
    rig, checks = service
    server_observation(rig, COOKIE)
    assert rig.poll() == []
    busy = replace(DENIED, unsettled=Unsettled.BUSY)
    first, second, third = (checks.plan(busy), checks.plan(busy), checks.plan())
    submit_candidate(rig)
    for run in (first, second):
        run.complete()
        assert len(of_type(rig.poll(), "detector.code")) == 1
        rig.clock.now += 2
        assert rig.poll() == []
    third.started()
    server_observation(rig, COOKIE + 1)
    third.complete()
    assert rig.poll() == []
    assert not rig.state.calls._instances
    rig.clock.now += 2
    assert rig.poll() == []
    assert len(checks.requests) == 3


@pytest.mark.parametrize(
    ("outcome", "code"),
    [
        (DENIED, "no_call"),
        (replace(DENIED, journal=(Reason.EXE,)), "identity_mismatch"),
        (replace(DENIED, unsettled=Unsettled.RATE), "rate"),
        (replace(DENIED, unsettled=Unsettled.REMOTE), "remote"),
    ],
)
def test_refused_offer_does_not_publish_or_retry(
    service: tuple[Rig, CheckDouble], outcome: CheckResult, code: str
) -> None:
    rig, checks = service
    run = checks.plan(outcome)
    submit_candidate(rig)
    run.complete()
    assert rig.poll() == [diagnostic("offer", code)]
    rig.clock.now += 3
    assert rig.poll() == []
    assert not rig.state.calls._offers
    assert len(checks.requests) == 1


@pytest.mark.parametrize(
    "change",
    [
        "command-stop",
        "epoch-only",
        "lock-unlock",
        "mic-end",
        "cookie",
        "server-event",
        "preparing",
        "prepare-failed",
    ],
)
def test_offer_revalidates_before_marking(
    service: tuple[Rig, CheckDouble], change: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig, checks = service
    state = rig.state
    messages = SignalQueue("prepare-failed")

    with monkeypatch.context() as patch:
        if change == "prepare-failed":
            patch.setattr(state, "messages", messages)
            patch.setattr(state, "free_mb", lambda: 599)
        run = checks.plan()
        submit_candidate(rig)
        # Leave a completed check in the queue while later changes invalidate it.
        run.complete()
        if change == "command-stop":
            detector(rig, "stop", "cancel-offer")
        elif change == "epoch-only":
            # No detector thread clears the rule, so the publication gate must
            # reject an old epoch even when mark_offered would succeed.
            state.observer.stop()
        elif change == "lock-unlock":
            state.session.update(known=True, active=True, locked=True, sleeping=False)
            state.session.update(known=True, active=True, locked=False, sleeping=False)
        elif change == "mic-end":
            observation(rig, "call-ended", program=PROGRAM)
        elif change in {"cookie", "server-event"}:
            if change == "cookie":
                state.observer.registry.set("observer", (42, COOKIE + 1))
            server_observation(rig, COOKIE + 1)
        elif change == "preparing":
            state.preparing = True
        elif change == "prepare-failed":
            assert rig.request_prepare({"kind": "none"})[0]["type"] == "meeting.ack"
            assert messages.arrived.wait(2)
        assert state.observer.rule.candidate(PROGRAM) == Candidate(PROGRAM, False)
        events = rig.poll()
        assert not of_type(events, "call.started")
        if change == "prepare-failed":
            (error,) = events
            assert error["type"] == "error" and error["code"] == "limit"
            assert not state.preparing
    assert state.observer.rule.candidate(PROGRAM) == Candidate(PROGRAM, False)
    assert not state.calls._instances
    if change == "prepare-failed":
        followup = checks.plan()
        submit_candidate(rig, candidate(rig, reset=True))
        followup.complete()
        assert len(of_type(rig.poll(), "call.started")) == 1


def test_prepare_refused_before_work_does_not_invalidate_offer(
    service: tuple[Rig, CheckDouble],
) -> None:
    rig, checks = service
    run = checks.plan()
    submit_candidate(rig)
    run.complete()
    rig.state.used_meetings.add(rig.identity["meeting_id"])
    (error,) = rig.request_prepare({"kind": "none"})
    assert error["type"] == "error" and error["code"] == "not-prepared"
    assert len(of_type(rig.poll(), "call.started")) == 1


@pytest.mark.parametrize("change", ["stop", "busy", "candidate-lost", "marked", "off", "epoch"])
def test_invalidated_offer_retry_is_removed(service: tuple[Rig, CheckDouble], change: str) -> None:
    rig, checks = service
    run = checks.plan(replace(DENIED, unsettled=Unsettled.PICTURE_CHANGED))
    submit_candidate(rig)
    run.complete()
    rig.poll()
    observer = rig.state.observer
    if change == "stop":
        detector(rig, "stop", "cancel-retry")
    elif change == "busy":
        rig.state.preparing = True
    elif change == "candidate-lost":
        with observer.rule_lock:
            observer.rule.update((NormalizedEvent(EventKind.UNAVAILABLE, rig.clock()),))
    elif change == "marked":
        with observer.rule_lock:
            assert observer.rule.mark_offered(PROGRAM)
    elif change == "off":
        observer.automation_off.set()
    elif change == "epoch":
        observer.stop()
    rig.poll()
    rig.clock.now += 2
    rig.poll()
    assert len(checks.requests) == 1 and not rig.state.calls._offers


EXPECTED_CODES = {
    Reason.NATIVE: None,
    Reason.CONNECTION: None,
    Reason.OWN: None,
    Reason.OWN_NATIVE: None,
    Reason.UNKNOWN: None,
    Reason.DECLARED_ONLY: None,
    Reason.UNAVAILABLE: "identity_mismatch",
    Reason.LIMIT: None,
    Reason.PROPERTIES: None,
    Reason.PID: "identity_mismatch",
    Reason.DEAD: "identity_mismatch",
    Reason.UID: "identity_mismatch",
    Reason.EXE: "identity_mismatch",
    Reason.BINARY: "identity_mismatch",
    Reason.REUSED: "identity_mismatch",
    Reason.CONFLICT: "identity_mismatch",
    Reason.SS_UNAVAILABLE: "verification=unavailable",
    Reason.SS_TIMEOUT: "verification=failed",
    Reason.SS_LIMIT: "verification=failed",
    Reason.SS_INSUFFICIENT: "verification=failed",
    Reason.NO_CONNECTION: "identity_mismatch",
    Reason.COUNTER: "identity_mismatch_counter",
    VerificationCode.FAILED: "verification=failed",
    VerificationCode.UNAVAILABLE: "verification=unavailable",
    Unsettled.PICTURE_CHANGED: "picture_changed",
    Unsettled.PULSE_FAILED: "pulse_failed",
    Unsettled.BUDGET: "budget",
    Unsettled.BUSY: "busy",
    Unsettled.RATE: "rate",
    Unsettled.REMOTE: "remote",
}


def test_result_code_has_explicit_coverage_for_every_enum_member() -> None:
    assert set(Reason) | set(VerificationCode) | set(Unsettled) == EXPECTED_CODES.keys()


@pytest.mark.parametrize(("reason", "code"), EXPECTED_CODES.items(), ids=list(EXPECTED_CODES))
def test_result_code_whitelist(
    reason: Reason | VerificationCode | Unsettled, code: str | None
) -> None:
    assert code is None or code in {member.value for member in DetectorCode}
    for base in (ALLOWED, DENIED):
        result = (
            replace(base, unsettled=reason)
            if isinstance(reason, Unsettled)
            else replace(base, journal=(reason,))
        )
        assert result_code(result) == (code or (None if base.offer else "no_call"))


@pytest.mark.parametrize(
    ("journal", "code"),
    [
        ((Reason.SS_TIMEOUT, Reason.COUNTER), "verification=failed"),
        ((Reason.COUNTER, Reason.EXE), "identity_mismatch"),
        ((Reason.COUNTER, Reason.NATIVE), "identity_mismatch_counter"),
        ((VerificationCode.FAILED, VerificationCode.UNAVAILABLE), "verification=failed"),
        ((Reason.LIMIT,), "no_call"),
    ],
)
def test_result_code_priority(journal: tuple[Reason | VerificationCode, ...], code: str) -> None:
    assert result_code(replace(DENIED, journal=journal)) == code
    assert result_code(replace(DENIED, journal=journal, unsettled=Unsettled.BUDGET)) == "budget"


def test_unlisted_diagnostic_value_never_reaches_wire(service: tuple[Rig, CheckDouble]) -> None:
    rig, checks = service
    outcome = replace(DENIED, unsettled=cast(Unsettled, "private-outcome"))
    _, events = checked_card(rig, checks, "unknown-outcome", outcome)
    assert of_type(events, "detector.code") == [diagnostic("card", "no_call")]


def test_card_ledger_replays_identical_frames_and_new_request_checks_again(
    service: tuple[Rig, CheckDouble],
) -> None:
    rig, checks = service
    outcome = replace(ALLOWED, app_class=AppClass.BROWSER, measured_mono_ms=4321)
    run = checks.plan(outcome)
    req = request("with-call")
    ack = rig.state.handle(req)
    assert ack == [{"type": "meeting.ack", "request_type": QUERY, "req": req["req"]}]
    assert run.started() == CheckRequest(Purpose.CARD, None, None, rig.clock() + CHECK_SECONDS)
    assert wire(rig.state.handle(req)) == wire(ack)
    run.complete()
    (state,) = rig.poll()
    assert ipc.decode(wire([state])[0][4:], role="meeting-capture") == {
        "type": "meeting.call.state",
        "active": True,
        "req": req["req"],
        "request_type": QUERY,
        "call_id": state["call_id"],
        "app_class": outcome.app_class,
        "variant1": outcome.variant1,
        "measured_mono_ms": outcome.measured_mono_ms,
    }
    assert wire(rig.state.handle(req)) == wire([*ack, state])
    assert len(checks.requests) == 1
    rig.clock.now += 2
    denied, events = checked_card(rig, checks, "fresh", DENIED)
    assert denied == refused("fresh")
    assert of_type(events, "detector.code") == [diagnostic("card", "no_call")]
    assert checks.requests[-1].deadline == rig.clock() + CHECK_SECONDS


def test_fresh_cards_publish_their_own_class_and_time_for_the_same_anchor(
    service: tuple[Rig, CheckDouble],
) -> None:
    rig, checks = service
    changed = replace(ALLOWED, app_class=AppClass.BROWSER, measured_mono_ms=100765)
    first, _ = checked_card(rig, checks, "first-sample", ALLOWED)
    second, _ = checked_card(rig, checks, "second-sample", changed)
    assert first["call_id"] == second["call_id"]
    for frame, expected in zip(wire([first, second]), (ALLOWED, changed), strict=True):
        event = ipc.decode(frame[4:], role="meeting-capture")
        assert event["active"] and event["app_class"] == expected.app_class
        assert event["measured_mono_ms"] == expected.measured_mono_ms


def test_card_round_trip_through_supervisor(
    service: tuple[Rig, CheckDouble], monkeypatch: pytest.MonkeyPatch
) -> None:
    rig, checks = service
    supervisor, events, clock = detached()
    frames: list[dict[str, Any]] = []

    def flush() -> None:
        while supervisor._outgoing:
            (size,) = struct.unpack(">I", supervisor._outgoing[:4])
            command = ipc.decode(bytes(supervisor._outgoing[4 : 4 + size]), role=supervisor.role)
            del supervisor._outgoing[: 4 + size]
            frames.append(command)
            for item in rig.state.handle(command):
                receive(supervisor, item)

    query = request("supervised")
    with monkeypatch.context() as patch:
        patch.setattr(supervisor, "_flush", flush)
        run = checks.plan()
        supervisor.send(query)
        assert frames == [query]
        (ack,) = events
        assert ack["type"] == "meeting.ack"
        supervisor.send(query)
        assert events == [ack] and frames == [query]
        clock.now += 0.55
        run.complete()
        for item in rig.poll():
            receive(supervisor, item)
        state = events[-1]
        assert state["type"] == "meeting.call.state" and state["active"]
        clock.now += 1
        supervisor._expire()
        assert events == [ack, state]
        supervisor.send(query)
        assert events == [ack, state, state] and frames == [query]
        assert len(checks.requests) == 1


def test_blocked_checks_do_not_hold_up_keepalive_and_share_one_factory(
    service: tuple[Rig, CheckDouble],
) -> None:
    rig, checks = service
    offer = checks.plan()
    submit_candidate(rig)
    offer.started()
    first = checks.plan()
    assert rig.state.handle(request("running"))[0]["type"] == "meeting.ack"
    first.started()
    assert rig.state.handle(request("running"))[0]["type"] == "meeting.ack"
    second = checks.plan()
    assert rig.state.handle(request("new"))[0]["type"] == "meeting.ack"
    second.started()
    # Prepare proceeds while both kinds of identity checks are still waiting.
    rig.prepare("none")
    assert rig.send("keepalive", seq=1)[0]["type"] == "meeting.ack"
    assert not any(run.release.is_set() for run in (offer, first, second))
    assert checks.created == [threading.current_thread()]
    for run in (offer, first, second):
        run.complete()
    events = rig.poll()
    assert len(of_type(events, "call.state")) == 2
    assert not of_type(events, "call.started")


def test_off_card_does_not_connect_and_code_is_not_replayed(
    service: tuple[Rig, CheckDouble], monkeypatch: pytest.MonkeyPatch
) -> None:
    rig, checks = service
    rig.state.observer.automation_off.set()
    with monkeypatch.context() as patch:
        pulses = ObserverPulses(patch)
        for req in ("off", "off", "second-off"):
            assert rig.state.handle(request(req)) == [refused(req)]
        assert rig.poll() == [diagnostic("card", "remote")]
        assert rig.state.handle(request("off")) == [refused("off")]
        assert rig.poll() == []
        rig.clock.now += 2
        assert rig.state.handle(request("third-off")) == [refused("third-off")]
        assert rig.poll() == [diagnostic("card", "remote")]
        assert pulses.count("pa_context_connect") == pulses.count("pa_context_new") == 0
    assert not checks.created and not checks.requests


def test_remote_card_disables_all_automation_and_drops_late_offer(
    service: tuple[Rig, CheckDouble], monkeypatch: pytest.MonkeyPatch
) -> None:
    rig, checks = service
    existing, _ = checked_card(rig, checks, "first")
    offer = checks.plan()
    submit_candidate(rig)
    offer.started()
    remote, events = checked_card(
        rig, checks, "transport", replace(DENIED, unsettled=Unsettled.REMOTE)
    )
    assert remote == refused("transport")
    assert of_type(events, "call.ended") == [ended(existing["call_id"], "detector_off")]
    assert of_type(events, "detector.code") == [diagnostic("card", "remote")]
    assert rig.state.observer.automation_off.is_set()
    assert rig.state.observer.epoch == 1
    offer.complete()
    assert rig.poll() == []
    assert not rig.state.calls._instances
    with monkeypatch.context() as patch:
        pulses = ObserverPulses(patch)
        detector(rig, "start", "refuse-observer-start")
        assert of_type(rig.poll(), "detector.code") == [diagnostic("observe", "remote")]
        rig.clock.now += 2
        assert rig.state.handle(request("new-remote")) == [refused("new-remote")]
        assert rig.state.handle(request("transport"))[-1] == remote
        assert of_type(rig.poll(), "detector.code") == [diagnostic("card", "remote")]
        assert not pulses.pulses
    assert len(checks.created) == 1 and len(checks.requests) == 3


@pytest.mark.parametrize("pending", ["completed-offer", "candidate", "stale-offer"])
def test_remote_disables_automation_before_dispatching_the_rest_of_poll(
    service: tuple[Rig, CheckDouble], pending: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig, checks = service
    remote = replace(DENIED, unsettled=Unsettled.REMOTE)
    if pending == "stale-offer":
        run = checks.plan(remote)
        submit_candidate(rig)
        run.started()
        detector(rig, "stop", "stop-during-offer")
        run.complete()
        events = rig.poll()
        assert rig.state.observer.automation_off.is_set()
        assert not of_type(events, "call.started") and not of_type(events, "detector.code")
        assert rig.state.handle(request("after-offer")) == [refused("after-offer")]
        assert rig.poll() == [diagnostic("card", "remote")]
        assert len(checks.requests) == 1
        return

    decision = candidate(rig)
    if pending == "completed-offer":
        offer = checks.plan()
        submit_candidate(rig, decision)
        offer.complete()
    run = checks.plan(remote)
    rig.state.handle(request("remote-batch"))
    run.complete()
    if pending == "candidate":
        checks.plan()  # Release even an unexpected second check during fixture cleanup.
        observation(rig, "candidate", program=decision.program, offered=decision.offered)
    launched: list[Purpose] = []
    launch = rig.state.calls._launch

    def record_launch(attempt: Any) -> None:
        launched.append(Purpose(attempt.purpose))
        launch(attempt)

    count = len(checks.requests)
    with monkeypatch.context() as patch:
        patch.setattr(rig.state.calls, "_launch", record_launch)
        assert rig.poll() == [diagnostic("card", "remote"), refused("remote-batch")]
    assert not launched and len(checks.requests) == count
    assert rig.state.observer.automation_off.is_set()
    assert not rig.state.calls._instances


def test_remote_batch_finishes_each_card_and_preserves_detector_stop(
    service: tuple[Rig, CheckDouble],
) -> None:
    rig, checks = service
    remote = replace(DENIED, unsettled=Unsettled.REMOTE)
    cards = [
        (request(name), checks.plan(outcome))
        for name, outcome in (
            ("allowed-before", ALLOWED),
            ("remote-a", remote),
            ("allowed-after", ALLOWED),
            ("remote-b", remote),
        )
    ]
    acknowledgements = []
    for command, run in cards:
        ack = rig.state.handle(command)
        assert [event["type"] for event in ack] == ["meeting.ack"]
        acknowledgements.append(ack)
        run.started()
    stop = {"type": "meeting.detector.stop", "request_type": "meeting.detector.stop", "req": "stop"}
    (stop_ack,) = rig.state.handle(stop)
    epoch = rig.state.observer.epoch
    for _, run in cards:
        run.complete()

    events = rig.poll()
    (stopped,) = of_type(events, "detector.stopped")
    assert stopped == {**stop, "type": "meeting.detector.stopped"}
    outcomes = of_type(events, "call.state")
    assert outcomes == [refused(command["req"]) for command, _ in cards]
    assert of_type(events, "detector.code") == [diagnostic("card", "remote")]
    assert rig.state.observer.epoch == epoch + 1
    assert rig.state.observer.automation_off.is_set()
    assert not of_type(events, "call.started") and not rig.state.calls._instances

    assert wire(rig.state.handle(stop)) == wire([stop_ack, stopped])
    for (command, _), ack, outcome in zip(cards, acknowledgements, outcomes, strict=True):
        assert wire(rig.state.handle(command)) == wire([*ack, outcome])
    assert rig.poll() == [] and len(checks.requests) == 4


def test_observer_remote_retires_instances_once(service: tuple[Rig, CheckDouble]) -> None:
    rig, checks = service
    card, _ = checked_card(rig, checks, "first")
    rig.state.observer.automation_off.set()
    assert rig.poll() == [ended(card["call_id"], "detector_off")]
    assert rig.poll() == [] and not rig.state.calls._instances


def test_observer_remote_refuses_card_in_the_current_epoch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checks = CheckDouble()
    fd_views = iter(({}, {40: 391}))
    with monkeypatch.context() as patch:
        pulses = ObserverPulses(patch)
        rig = Rig(
            tmp_path,
            observer_address=lambda: LOCAL,
            check_factory=checks.factory,
            observer_factory=observer_metadata,
            observer_fds=lambda: next(fd_views),
            observer_family=lambda *_: socket.AF_INET,
        )
        inbox = SignalQueue("detector-code")
        rig.state.messages = inbox
        try:
            epoch = rig.state.observer.epoch
            previous, _ = checked_card(rig, checks, "before-remote")
            assert previous["active"]
            run = checks.plan()
            command = request("in-flight")
            ack = rig.state.handle(command)
            run.started()
            detector(rig, "start", "remote-observer")
            assert inbox.arrived.wait(2)
            assert rig.state.observer.automation_off.is_set()
            assert rig.state.observer.epoch == epoch
            run.complete()
            events = rig.poll()
            assert of_type(events, "call.ended") == [ended(previous["call_id"], "detector_off")]
            assert of_type(events, "detector.code") == [diagnostic("observe", "remote")]
            assert of_type(events, "call.state") == [refused("in-flight")]
            assert not rig.state.calls._instances
            assert wire(rig.state.handle(command)) == wire([*ack, *of_type(events, "call.state")])
        finally:
            checks.release_all()
            rig.cleanup()
        assert pulses.count("pa_context_connect") == 1
        pulses.assert_no_pcm()


def test_offer_and_card_use_pair_specific_code_rate_limit(
    service: tuple[Rig, CheckDouble],
) -> None:
    rig, checks = service
    _, first = checked_card(rig, checks, "first", DENIED)
    _, second = checked_card(rig, checks, "second", DENIED)
    assert of_type(first, "detector.code") == [diagnostic("card", "no_call")]
    assert not of_type(second, "detector.code")
    _, third = checked_card(rig, checks, "other-code", replace(DENIED, journal=(Reason.EXE,)))
    assert of_type(third, "detector.code") == [diagnostic("card", "identity_mismatch")]
    run = checks.plan(DENIED)
    decision = Candidate(PROGRAM, False)
    with rig.state.observer.rule_lock:
        already = rig.state.observer.rule.candidate(PROGRAM)
    assert already is None
    # Qualify without advancing the card clock at the time of the offer.
    compare_at = rig.clock()
    rig.clock.now -= 6
    assert candidate(rig) == decision
    assert rig.clock() == compare_at
    submit_candidate(rig, decision)
    run.complete()
    assert rig.poll() == [diagnostic("offer", "no_call")]
    rig.clock.now += 2
    _, fourth = checked_card(rig, checks, "later", DENIED)
    assert of_type(fourth, "detector.code") == [diagnostic("card", "no_call")]


@pytest.mark.parametrize(
    "different",
    [
        replace(ANCHOR, program=ProgramKey("other", "private-binary-label")),
        replace(ANCHOR, server_cookie=COOKIE + 1),
        replace(ANCHOR, processes=frozenset({ProcessKey(PID, KEY.starttime + 1)})),
        replace(ANCHOR, members=frozenset({replace(MEMBER, index=MIC + 1)})),
        replace(ANCHOR, ambiguous=True),
    ],
    ids=["program", "cookie", "process", "microphone-member", "ambiguous"],
)
def test_equal_anchor_reuses_instance_and_each_field_matters(
    service: tuple[Rig, CheckDouble], different: CallAnchor
) -> None:
    rig, checks = service
    first, _ = checked_card(rig, checks, "first")
    equal, events = checked_card(rig, checks, "equal", replace(ALLOWED, anchor=replace(ANCHOR)))
    assert first["call_id"] == equal["call_id"]
    assert rig.state.calls.anchor_for(first["call_id"]) is ANCHOR
    assert not of_type(events, "call.started")
    outcome = replace(ALLOWED, anchor=different, measured_mono_ms=int(rig.clock() * 1000))
    if different.server_cookie != COOKIE:
        refused_card, events = checked_card(rig, checks, "cookie-changed", outcome)
        assert events == [ended(first["call_id"], "server_changed"), refused("cookie-changed")]
        assert refused_card == refused("cookie-changed")
        assert rig.state.calls.anchor_for(first["call_id"]) is None
    other, events = checked_card(rig, checks, "different", outcome)
    assert first["call_id"] != other["call_id"]
    assert rig.state.calls.anchor_for(other["call_id"]) is different
    assert not of_type(events, "call.ended")


def test_calls_reuse_across_purposes_but_not_across_roles(
    service: tuple[Rig, CheckDouble], tmp_path: Path
) -> None:
    rig, checks = service
    first, _ = checked_card(rig, checks, "card")
    run = checks.plan()
    submit_candidate(rig)
    run.complete()
    (started,) = of_type(rig.poll(), "call.started")
    assert started["call_id"] == first["call_id"]
    other = Rig(tmp_path / "other", observer_address=lambda: LOCAL, check_factory=checks.factory)
    try:
        card, _ = checked_card(other, checks, "other-card")
        assert card["call_id"] != first["call_id"]
    finally:
        checks.release_all()
        other.cleanup()


def test_new_instances_use_uuid4_and_equal_anchor_keeps_the_id(
    service: tuple[Rig, CheckDouble], monkeypatch: pytest.MonkeyPatch
) -> None:
    rig, checks = service
    first = UUID("ce651b2e-5417-4799-b270-7eb0563d4821")
    second = UUID("15807400-ee6c-4f3e-81d2-bdd16a1fe8b5")
    generated = iter((first, second))
    with monkeypatch.context() as patch:
        patch.setattr("astra_voice.worker.meeting_call_service.uuid.uuid4", lambda: next(generated))
        run = checks.plan()
        submit_candidate(rig)
        run.complete()
        (offer,) = of_type(rig.poll(), "call.started")
        assert offer["call_id"] == first.hex
        same, _ = checked_card(rig, checks, "same-anchor")
        assert same["call_id"] == first.hex
        different = replace(ALLOWED, anchor=replace(ANCHOR, ambiguous=True))
        card, _ = checked_card(rig, checks, "new-anchor", different)
        assert card["call_id"] == second.hex
    assert next(generated, None) is None


def test_ninth_instance_evicts_oldest_even_if_reused_recently(
    service: tuple[Rig, CheckDouble],
) -> None:
    rig, checks = service
    ids = []
    outcomes = [
        replace(ALLOWED, anchor=replace(ANCHOR, members=frozenset({replace(MEMBER, index=n)})))
        for n in range(9)
    ]
    for i, outcome in enumerate(outcomes[:8]):
        card, events = checked_card(rig, checks, f"card-{i}", outcome)
        ids.append(card["call_id"])
        assert not of_type(events, "call.ended")
        rig.clock.now += 2
    again, _ = checked_card(rig, checks, "reused-first", outcomes[0])
    assert again["call_id"] == ids[0]
    ninth, events = checked_card(rig, checks, "ninth", outcomes[8])
    assert of_type(events, "call.ended") == [ended(ids[0], "evicted")]
    assert list(rig.state.calls._instances) == [*ids[1:], ninth["call_id"]]


@pytest.mark.parametrize(
    "change",
    ["server-event", "server-event-no-registry", "card-cookie", "offer-cookie", "stop", "mic"],
)
def test_instance_lifecycle(service: tuple[Rig, CheckDouble], change: str) -> None:
    rig, checks = service
    first, _ = checked_card(rig, checks, "first")
    other_anchor = replace(ANCHOR, program=ProgramKey("other", "private-binary-label"))
    other, _ = checked_card(rig, checks, "other", replace(ALLOWED, anchor=other_anchor))
    ids = [first["call_id"], other["call_id"]]
    new_generation = on_server(rig, COOKIE + 1)
    expected_live = []
    if change in {"server-event", "server-event-no-registry"}:
        context = (42, COOKIE + 1) if change == "server-event" else None
        rig.state.observer.registry.set("observer", context)
        server_observation(rig, COOKIE + 1)
        events = rig.poll()
    elif change == "card-cookie":
        card, events = checked_card(rig, checks, "new-cookie", new_generation)
        assert card == refused("new-cookie")
    elif change == "offer-cookie":
        run = checks.plan(new_generation)
        submit_candidate(rig)
        run.complete()
        events = rig.poll()
        assert not of_type(events, "call.started")
        rig.clock.now += 2
        assert not of_type(rig.poll(), "call.started")
        assert not rig.state.calls._offers
        assert len(checks.requests) == 3
    elif change == "stop":
        detector(rig, "stop", "stop-detector")
        events = rig.poll()
    else:
        observation(rig, "call-ended", program=PROGRAM)
        events = rig.poll()
        ids, expected_live = ids[:1], ids[1:]
    reason = {"stop": "detector_off", "mic": "mic_released"}.get(change, "server_changed")
    assert of_type(events, "call.ended") == [ended(call_id, reason) for call_id in ids]
    assert list(rig.state.calls._instances) == expected_live
    assert all(rig.state.calls.anchor_for(call_id) is None for call_id in ids)


def test_late_cards_cannot_cross_generation_or_retire_new_instance(
    service: tuple[Rig, CheckDouble],
) -> None:
    rig, checks = service
    original, _ = checked_card(rig, checks, "old-server")
    rig.state.observer.registry.set("observer", None)
    changed = on_server(rig, COOKIE + 1)
    runs = (checks.plan(changed), checks.plan(changed), checks.plan())
    for number, run in enumerate(runs):
        rig.state.handle(request(f"overlap-{number}"))
        run.started()

    runs[0].complete()
    events = rig.poll()
    assert events == [ended(original["call_id"], "server_changed"), refused("overlap-0")]
    assert not rig.state.calls._instances
    first, _ = checked_card(rig, checks, "fresh-generation", changed)
    assert first["active"]
    runs[1].complete()
    assert rig.poll() == [refused("overlap-1")]
    runs[2].complete()
    assert rig.poll() == [refused("overlap-2")]
    assert list(rig.state.calls._instances) == [first["call_id"]]
    assert rig.state.calls.anchor_for(first["call_id"]) is changed.anchor


@pytest.mark.parametrize("change", ["mic-end", "server"])
def test_card_completes_inactive_if_call_ended_or_server_changed(
    service: tuple[Rig, CheckDouble], change: str
) -> None:
    rig, checks = service
    first, _ = checked_card(rig, checks, "current")
    run = checks.plan()
    req = request("in-flight")
    ack = rig.state.handle(req)
    run.started()
    if change == "mic-end":
        observation(rig, "call-ended", program=PROGRAM)
        reason = "mic_released"
    else:
        rig.state.observer.registry.set("observer", (42, COOKIE + 1))
        server_observation(rig, COOKIE + 1)
        reason = "server_changed"
    run.complete()
    assert rig.poll() == [ended(first["call_id"], reason), refused("in-flight")]
    assert not rig.state.calls._instances
    assert rig.state.calls.anchor_for(first["call_id"]) is None
    assert rig.state.handle(req) == [*ack, refused("in-flight")]


@pytest.mark.parametrize("condition", ["locked", "preparing"])
def test_card_preserves_allowed_result_during_session_or_prepare_transition(
    service: tuple[Rig, CheckDouble], condition: str
) -> None:
    rig, checks = service
    run = checks.plan()
    rig.state.handle(request("before-transition"))
    run.started()
    if condition == "locked":
        rig.state.session.update(known=True, active=True, locked=True, sleeping=False)
    else:
        rig.state.preparing = True
    run.complete()
    (card,) = of_type(rig.poll(), "call.state")
    assert card["active"]
    later, _ = checked_card(rig, checks, "after-transition")
    assert later["active"] and later["call_id"] == card["call_id"]


def release_call(rig: Rig, epoch: int) -> None:
    observer = rig.state.observer
    now = rig.clock()
    removed = NormalizedEvent(
        EventKind.REMOVE, now, StreamKey(1, Direction.MICROPHONE, MIC, MIC, MIC), PROGRAM
    )
    observer._update((removed,), PictureTiming(now, None, True), epoch)
    rig.clock.now += 11
    observer._update((), PictureTiming(rig.clock(), None, True), epoch)


def test_card_started_after_queued_call_end_can_find_the_next_call(
    service: tuple[Rig, CheckDouble],
) -> None:
    rig, checks = service
    first, _ = checked_card(rig, checks, "old-call")
    candidate(rig)
    release_call(rig, rig.state.observer.epoch)
    # The old instance is still live in the role until poll() reads the event.
    assert rig.state.calls.anchor_for(first["call_id"]) is ANCHOR
    fresh = replace(on_server(rig, COOKIE), anchor=replace(ANCHOR, ambiguous=True))
    new_card, events = checked_card(rig, checks, "new-call", fresh)
    assert events == [ended(first["call_id"], "mic_released"), new_card]
    assert new_card["active"] and new_card["call_id"] != first["call_id"]
    assert rig.state.calls.anchor_for(new_card["call_id"]) is fresh.anchor

    offer = checks.plan(fresh)
    submit_candidate(rig, candidate(rig, reset=True))
    offer.complete()
    (started,) = of_type(rig.poll(), "call.started")
    assert started["call_id"] == new_card["call_id"]


def test_stale_observer_end_does_not_invalidate_new_epoch_card(
    service: tuple[Rig, CheckDouble], monkeypatch: pytest.MonkeyPatch
) -> None:
    rig, checks = service
    observer = rig.state.observer
    epoch = observer.epoch
    candidate(rig)
    inbox = SignalQueue("detector-call-ended")
    with monkeypatch.context() as patch:
        patch.setattr(rig.state, "messages", inbox)
        detector(rig, "stop", "stop-old-epoch")
        assert rig.poll()[0]["type"] == "meeting.detector.stopped"
        assert observer.epoch == epoch + 1
        run = checks.plan()
        rig.state.handle(request("live-call"))
        run.started()
        release_call(rig, epoch)
        assert not inbox.arrived.is_set()
        with observer.rule_lock:
            assert observer.end_revision(PROGRAM) == 0
        run.complete()
        (card,) = rig.poll()
        assert card["active"]
        assert rig.state.calls.anchor_for(card["call_id"]) is ANCHOR


def test_second_end_of_same_program_invalidates_pending_card(
    service: tuple[Rig, CheckDouble],
) -> None:
    rig, checks = service
    epoch = rig.state.observer.epoch
    candidate(rig)
    first, _ = checked_card(rig, checks, "first-call")
    release_call(rig, epoch)
    assert rig.poll() == [ended(first["call_id"], "mic_released")]

    candidate(rig, reset=True)
    second, _ = checked_card(rig, checks, "second-call")
    assert second["call_id"] != first["call_id"]
    command = request("second-in-flight")
    run = checks.plan()
    ack = rig.state.handle(command)
    run.started()
    release_call(rig, epoch)
    run.complete()
    events = rig.poll()
    assert events == [ended(second["call_id"], "mic_released"), refused("second-in-flight")]
    assert wire(rig.state.handle(command)) == wire([*ack, events[-1]])
    assert not rig.state.calls._instances


def test_card_ignores_end_of_another_program(service: tuple[Rig, CheckDouble]) -> None:
    rig, checks = service
    run = checks.plan()
    rig.state.handle(request("other-program-ended"))
    run.started()
    observation(rig, "call-ended", program=ProgramKey("other", "other-binary"))
    run.complete()
    (card,) = rig.poll()
    assert card["active"]
    assert rig.state.calls.anchor_for(card["call_id"]) is ANCHOR


@pytest.mark.parametrize("reverse_completion", [False, True])
@pytest.mark.parametrize("known_server", [False, True])
def test_batch_with_conflicting_cookies_rejects_both_cards(
    service: tuple[Rig, CheckDouble], reverse_completion: bool, known_server: bool
) -> None:
    rig, checks = service
    if known_server:
        card, _ = checked_card(rig, checks, "existing")
        server_observation(rig, COOKIE)
    first = checks.plan(on_server(rig, COOKIE))
    old_req = request("server-a")
    old_ack = rig.state.handle(old_req)
    first.started()
    outcome = on_server(rig, COOKIE + 1)
    second = checks.plan(outcome)
    new_req = request("server-b")
    new_ack = rig.state.handle(new_req)
    second.started()
    for run in (second, first) if reverse_completion else (first, second):
        run.complete()
    events = rig.poll()
    by_req = {event["req"]: event for event in of_type(events, "call.state")}
    assert by_req.keys() == {"server-a", "server-b"}
    if known_server:
        assert len(events) == 3
        assert of_type(events, "call.ended") == [ended(card["call_id"], "server_changed")]
    else:
        assert len(events) == 2
    assert by_req["server-a"] == refused("server-a")
    assert by_req["server-b"] == refused("server-b")
    assert not rig.state.calls._instances
    assert wire(rig.state.handle(old_req)) == wire([*old_ack, by_req["server-a"]])
    assert wire(rig.state.handle(new_req)) == wire([*new_ack, by_req["server-b"]])
    confirmed = ALLOWED if known_server else outcome
    fresh, _ = checked_card(rig, checks, "confirmed-after-reset", confirmed)
    assert fresh["active"]
    assert rig.state.calls.anchor_for(fresh["call_id"]) is confirmed.anchor


def test_observed_change_closes_instances_and_refuses_pending_card(
    service: tuple[Rig, CheckDouble],
) -> None:
    rig, checks = service
    first, _ = checked_card(rig, checks, "old-server")
    other_anchor = replace(ANCHOR, program=ProgramKey("other", "private-binary-label"))
    second, _ = checked_card(rig, checks, "old-server-other", replace(ALLOWED, anchor=other_anchor))
    command = request("old-server-in-flight")
    old = checks.plan()
    ack = rig.state.handle(command)
    old.started()
    server_observation(rig, COOKIE + 1)
    old.complete()
    events = rig.poll()
    assert events == [
        ended(first["call_id"], "server_changed"),
        ended(second["call_id"], "server_changed"),
        refused("old-server-in-flight"),
    ]
    assert not rig.state.calls._instances
    assert wire(rig.state.handle(command)) == wire([*ack, events[-1]])

    result = on_server(rig, COOKIE + 1)
    current, events = checked_card(rig, checks, "new-server", result)
    assert events == [current]
    assert current["active"]
    assert rig.state.calls.anchor_for(current["call_id"]) is result.anchor


def test_first_new_card_cannot_revive_cookie_before_observed_change(
    service: tuple[Rig, CheckDouble],
) -> None:
    rig, checks = service
    original, _ = checked_card(rig, checks, "server-a")
    server_observation(rig, COOKIE + 1)
    assert rig.poll() == [ended(original["call_id"], "server_changed")]

    state, events = checked_card(rig, checks, "old-cookie-after-change")
    assert events == [state]
    assert state == refused("old-cookie-after-change")
    assert not rig.state.calls._instances


def test_old_offer_does_not_retire_the_new_servers_instance(
    service: tuple[Rig, CheckDouble],
) -> None:
    rig, checks = service
    previous, _ = checked_card(rig, checks, "old-server")
    offer = checks.plan(on_server(rig, COOKIE))
    submit_candidate(rig)
    offer.started()
    server_observation(rig, COOKIE + 1)
    assert rig.poll() == [ended(previous["call_id"], "server_changed")]
    result = on_server(rig, COOKIE + 1)
    current, _ = checked_card(rig, checks, "new-server", result)

    offer.complete()
    assert rig.poll() == []
    assert list(rig.state.calls._instances) == [current["call_id"]]
    assert rig.state.calls.anchor_for(current["call_id"]) is result.anchor


class NewServer(DetectorBackend):
    def __init__(self) -> None:
        super().__init__()
        self.clients.clear()
        self.streams.clear()

    def server_cookie(self) -> int:
        return COOKIE + 1


def test_observer_uses_own_cookie_even_if_first_snapshot_disconnects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class DisconnectedServer(NewServer):
        def snapshot(self, **kwargs: Any) -> Any:
            raise MetadataDisconnected()

    steps = ObserverSteps()
    checks = CheckDouble()
    with monkeypatch.context() as patch:
        patch.setattr(meeting_detector, "load_apps", lambda: (APP,))
        rig = Rig(
            tmp_path,
            observer_address=lambda: LOCAL,
            check_factory=checks.factory,
            observer_factory=lambda *_: DisconnectedServer(),
            observer_wait=steps.wait,
        )
        steps.clock = rig.clock
        observer = rig.state.observer
        observer.registry.set("control", (51, COOKIE))
        try:
            previous, _ = checked_card(rig, checks, "old-server")
            old = checks.plan(ALLOWED)
            rig.state.handle(request("old-card-pending"))
            old.started()
            detector(rig, "start", "broken-snapshot")
            steps.ready()
            assert observer.registry.snapshot().server_cookie == COOKIE
            old.complete()
            events = rig.poll()
            assert of_type(events, "call.ended") == [ended(previous["call_id"], "server_changed")]
            assert of_type(events, "call.state") == [refused("old-card-pending")]
            assert rig.state.calls.anchor_for(previous["call_id"]) is None
        finally:
            checks.release_all()
            rig.state.terminate()
            steps.advance.set()
            rig.cleanup()


def test_first_observer_connection_retires_card_server_without_retrying_offer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    steps = ObserverSteps()
    checks = CheckDouble()
    with monkeypatch.context() as patch:
        patch.setattr(meeting_detector, "load_apps", lambda: (APP,))
        rig = Rig(
            tmp_path,
            observer_address=lambda: LOCAL,
            check_factory=checks.factory,
            observer_factory=lambda *_: NewServer(),
            observer_wait=steps.wait,
        )
        steps.clock = rig.clock
        try:
            previous, _ = checked_card(rig, checks, "before-observer")
            detector(rig, "start", "start-new-server")
            steps.ready()
            decision = candidate(rig)
            first = checks.plan(on_server(rig, COOKIE + 1))
            events = submit_candidate(rig, decision)
            assert of_type(events, "call.ended") == [ended(previous["call_id"], "server_changed")]
            assert rig.state.calls.anchor_for(previous["call_id"]) is None
            first.complete()
            assert rig.poll() == []
            rig.clock.now += 2
            assert rig.poll() == []
            assert len(checks.requests) == 2
            assert not rig.state.calls._instances
            pending = rig.state.observer.rule.candidate(PROGRAM)
            assert pending is not None and not pending.offered

            # A later candidate on the confirmed server can still offer a call.
            outcome = on_server(rig, COOKIE + 1)
            new_offer = checks.plan(outcome)
            assert submit_candidate(rig, candidate(rig, reset=True)) == []
            new_offer.complete()
            (started,) = of_type(rig.poll(), "call.started")
            assert rig.state.calls.anchor_for(started["call_id"]) is outcome.anchor
        finally:
            checks.release_all()
            rig.state.terminate()
            steps.advance.set()
            rig.cleanup()


def test_observer_reconnect_keeps_instance_card_already_found_on_new_server(
    service: tuple[Rig, CheckDouble],
) -> None:
    rig, checks = service
    first, _ = checked_card(rig, checks, "old-server")
    registry = rig.state.observer.registry
    registry.set("observer", None)
    outcome = on_server(rig, COOKIE + 1)
    card, events = checked_card(rig, checks, "server-change-discovered", outcome)
    assert events == [ended(first["call_id"], "server_changed"), card]
    assert card == refused("server-change-discovered")
    current, events = checked_card(rig, checks, "new-server", outcome)
    assert current["active"] and current["call_id"] != first["call_id"]
    assert events == [current]
    registry.set("observer", (42, COOKIE + 1))
    rig.clock.now += 1
    server_observation(rig, COOKIE + 1)
    assert rig.poll() == []
    assert rig.state.calls.anchor_for(current["call_id"]) is outcome.anchor


def test_reconnecting_to_same_observer_cookie_retires_cards_from_other_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    steps = ObserverSteps()
    checks = CheckDouble()
    with monkeypatch.context() as patch:
        pulses = ObserverPulses(patch, cookies=(COOKIE,), setup=lambda pulse: pulse.streams.clear())
        rig = Rig(
            tmp_path,
            observer_address=lambda: LOCAL,
            check_factory=checks.factory,
            observer_factory=observer_metadata,
            observer_fds=lambda: {},
            observer_wait=steps.wait,
        )
        steps.clock = rig.clock
        try:
            detector(rig, "start", "first-A")
            steps.ready()
            assert [m["type"] for m in rig.poll()] == ["meeting.detector.started"]
            pulses.pulses[0].state = PA_CONTEXT_FAILED
            steps.step()
            assert not rig.state.observer.registry.for_observer(0)

            outcome = on_server(rig, COOKIE + 1)
            conflict, _ = checked_card(rig, checks, "first-B", outcome)
            assert conflict == refused("first-B")
            earlier, _ = checked_card(rig, checks, "second-B", outcome)
            assert earlier["active"]
            assert rig.state.calls.anchor_for(earlier["call_id"]) is outcome.anchor

            run = checks.plan(outcome)
            command = request("pending-B")
            ack = rig.state.handle(command)
            run.started()
            steps.step(11)
            assert rig.state.observer.registry.for_observer(0) == {43}
            run.complete()
            events = rig.poll()
            assert events == [ended(earlier["call_id"], "server_changed"), refused("pending-B")]
            assert wire(rig.state.handle(command)) == wire([*ack, events[-1]])
            assert not rig.state.calls._instances
            assert rig.state.calls.anchor_for(earlier["call_id"]) is None
        finally:
            checks.release_all()
            rig.state.terminate()
            steps.advance.set()
            rig.cleanup()
        pulses.assert_no_pcm()


def test_server_reset_preserves_rule_offer_marker(service: tuple[Rig, CheckDouble]) -> None:
    rig, checks = service
    run = checks.plan()
    submit_candidate(rig)
    run.complete()
    (started,) = of_type(rig.poll(), "call.started")
    server_observation(rig, COOKIE + 1)
    rig.state.observer.registry.set("observer", (42, COOKIE + 1))
    assert rig.poll() == [ended(started["call_id"], "server_changed")]
    qualified = candidate(rig, reset=True)
    assert qualified.offered
    events = submit_candidate(rig, qualified)
    assert not of_type(events, "call.started") and len(checks.requests) == 1


def test_detector_stop_does_not_shutdown_checker_and_old_card_completes(
    service: tuple[Rig, CheckDouble], monkeypatch: pytest.MonkeyPatch
) -> None:
    rig, checks = service
    with monkeypatch.context() as patch:
        # Keep detector start local and synchronous here. 4b covers its native thread.
        patch.setattr(rig.state.observer, "start", lambda: None)
        detector(rig, "start", "start")
        old = checks.plan()
        req = request("while-stopping")
        ack = rig.state.handle(req)
        old.started()
        detector(rig, "stop", "stop")
        detector(rig, "start", "restart")
        old.complete()
        (state,) = of_type(rig.poll(), "call.state")
        assert state["req"] == req["req"] and state["active"]
        assert rig.state.calls.anchor_for(state["call_id"]) is None
        assert not rig.state.calls._instances
        assert rig.state.handle(req) == [*ack, state]
        fresh, _ = checked_card(rig, checks, "after-restart")
        assert fresh["active"] and fresh["call_id"] != state["call_id"]
        assert rig.state.calls.anchor_for(fresh["call_id"]) is ANCHOR
    assert not checks.deadlines


@pytest.mark.parametrize("case", ["different-cookie", "different-generation", "matching"])
@pytest.mark.parametrize("queued_before_stop", [False, True])
def test_old_card_checks_measurement_cookie_and_server_generation(
    service: tuple[Rig, CheckDouble], case: str, queued_before_stop: bool
) -> None:
    rig, checks = service
    confirmed_cookie = COOKIE if case == "matching" else COOKIE + 1
    if case == "different-generation":
        server_observation(rig, COOKIE)
        assert rig.poll() == []
    measured_cookie = COOKIE if case == "different-cookie" else confirmed_cookie
    run = checks.plan(on_server(rig, measured_cookie))
    command = request("across-stop")
    ack = rig.state.handle(command)
    run.started()
    if queued_before_stop:
        run.complete()
    detector(rig, "stop", "stop")
    server_observation(rig, confirmed_cookie)
    if not queued_before_stop:
        run.complete()

    events = rig.poll()
    (state,) = of_type(events, "call.state")
    if case == "matching":
        assert state["active"]
        assert rig.state.calls.anchor_for(state["call_id"]) is None
    else:
        assert state == refused("across-stop")
    assert not rig.state.calls._instances
    assert wire(rig.state.handle(command)) == wire([*ack, state])


def test_old_card_result_queue_repr_hides_measurement_cookie(
    service: tuple[Rig, CheckDouble],
) -> None:
    rig, checks = service
    cookies = (COOKIE, COOKIE + 1)
    runs = [checks.plan(on_server(rig, cookie)) for cookie in cookies]
    for index, run in enumerate(runs):
        rig.state.handle(request(f"private-cookie-{index}"))
        run.started()
    detector(rig, "stop", "stop")
    assert [event["type"] for event in rig.poll()] == ["meeting.detector.stopped"]

    representations = []
    for run in runs:
        run.complete()
        event = rig.state.messages.get_nowait()
        assert event["type"] == "check-result" and event["result"].anchor is None
        representations.append(repr_without_object_addresses(event))
        rig.state.messages.put(event)
        (state,) = rig.poll()
        assert state["active"]
    assert representations[0] == representations[1]
    assert not rig.state.calls._instances


def guarded_result(accesses: list[str]) -> CheckResult:
    class GuardedResult(CheckResult):
        def __getattribute__(self, name: str) -> Any:
            if name in {"anchor", "target"}:
                accesses.append(name)
            return super().__getattribute__(name)

    return GuardedResult(**vars(ALLOWED))


def test_terminate_finishes_waiting_card_and_does_not_read_late_result(
    service: tuple[Rig, CheckDouble], monkeypatch: pytest.MonkeyPatch
) -> None:
    rig, checks = service
    # Check cleanup order and repeat semantics without changing actual time.
    state = rig.state
    seen: list[object] = []
    stop = state.observer.stop
    shutdown = checks.shutdown

    def observed_stop(*, exiting: bool) -> None:
        seen.append(("stop", state.terminating, exiting))
        stop(exiting=exiting)

    def observed_shutdown(deadline: float) -> bool:
        seen.append(("shutdown", state.observer.epoch))
        return shutdown(deadline)

    with monkeypatch.context() as patch:
        patch.setattr(state.observer, "stop", observed_stop)
        patch.setattr(checks, "shutdown", observed_shutdown)
        checks.shutdown_results = [False, True]
        req = request("pending")
        accesses: list[str] = []
        run = checks.plan(guarded_result(accesses))
        ack = state.handle(req)
        run.started()
        state.terminate()
        assert rig.poll() == [refused("pending")]
        assert state.handle(req) == [*ack, refused("pending")]
        run.complete()
        assert rig.poll() == [] and not accesses
        assert not state.calls._instances and state.exit_code == 0
        rig.cleanup()
        assert seen == [("stop", True, True), ("shutdown", 1), ("shutdown", 1)]
        assert len(checks.deadlines) == 2 and len(set(checks.deadlines)) == 1


def test_result_already_in_queue_at_role_exit_is_discarded(
    service: tuple[Rig, CheckDouble],
) -> None:
    rig, checks = service
    run = checks.plan()
    submit_candidate(rig)
    run.complete()
    rig.state.terminate()
    assert rig.poll() == []
    assert not rig.state.calls._instances


def test_stop_flag_alone_blocks_an_already_queued_offer(
    service: tuple[Rig, CheckDouble],
) -> None:
    rig, checks = service
    run = checks.plan()
    submit_candidate(rig)
    run.complete()
    # Exercise the guard with the candidate, outstanding check and epoch
    # still present, before the rest of terminate() removes them.
    rig.state.terminating = True
    try:
        assert rig.poll() == [] and not rig.state.calls._instances
        assert rig.state.observer.rule.candidate(PROGRAM) == Candidate(PROGRAM, False)
    finally:
        rig.state.terminating = False


def test_foreign_epoch_offer_does_not_read_anchor_or_target(
    service: tuple[Rig, CheckDouble],
) -> None:
    rig, checks = service
    accesses: list[str] = []
    run = checks.plan(guarded_result(accesses))
    submit_candidate(rig)
    run.started()
    rig.state.observer.stop()
    run.complete()
    assert rig.poll() == [] and not accesses and not rig.state.calls._instances


def test_shutdown_retries_only_within_cleanup_budget(
    service: tuple[Rig, CheckDouble], monkeypatch: pytest.MonkeyPatch
) -> None:
    rig, checks = service
    checked_card(rig, checks, "instantiate")
    checks.shutdown_default = False
    end = rig.clock() + 1

    def sleep(seconds: float) -> None:
        assert 0 < seconds <= end - rig.clock()
        rig.clock.now += seconds

    virtual_time = SimpleNamespace(monotonic=rig.clock, sleep=sleep)
    with monkeypatch.context() as patch:
        patch.setattr(meeting_capture, "time", virtual_time)
        patch.setattr(meeting_call_service, "time", virtual_time)
        rig.cleanup()
    assert rig.clock() == end
    assert len(checks.deadlines) > 1 and set(checks.deadlines) == {end}
    checks.shutdown_default = True


def test_failed_factory_completes_card_and_does_not_retry_offer(tmp_path: Path) -> None:
    calls: list[threading.Thread] = []

    def broken_factory(*_: object) -> Any:
        calls.append(threading.current_thread())
        raise OSError("private exception")

    rig = Rig(tmp_path, observer_address=lambda: LOCAL, check_factory=broken_factory)
    try:
        events = submit_candidate(rig)
        assert of_type(events, "detector.code") == [diagnostic("offer", "pulse_failed")]
        rig.clock.now += 3
        assert rig.poll() == []
        assert [m["type"] for m in rig.state.handle(request("factory-error"))] == ["meeting.ack"]
        assert rig.poll() == [diagnostic("card", "pulse_failed"), refused("factory-error")]
        assert not rig.state.stopped and rig.state.exit_code == 0
    finally:
        rig.cleanup()
    assert calls == [threading.current_thread()] * 2


def test_check_exception_becomes_public_pulse_failed(service: tuple[Rig, CheckDouble]) -> None:
    rig, checks = service
    run = checks.plan(OSError("private check exception"))
    rig.state.handle(request("runner-error"))
    run.complete()
    assert rig.poll() == [diagnostic("card", "pulse_failed"), refused("runner-error")]
    assert not rig.state.stopped and rig.state.exit_code == 0


def test_private_anchor_and_target_are_not_public_and_target_is_dropped(
    service: tuple[Rig, CheckDouble],
) -> None:
    rig, checks = service
    hidden_name = "private-anchor-program-marker"
    private = replace(ANCHOR, program=ProgramKey(hidden_name, hidden_name))
    run = checks.plan(replace(ALLOWED, anchor=private))
    submit_candidate(rig)
    run.complete()
    queued = rig.state.messages.get_nowait()
    assert queued["result"].target is None
    rig.state.messages.put(queued)
    events = rig.poll()
    (started,) = of_type(events, "call.started")
    assert rig.state.calls.anchor_for(started["call_id"]) is private
    assert hidden_name.encode() not in b"".join(wire(events))
    for item in (rig.state, rig.state.calls, *rig.state.calls._instances.values()):
        assert hidden_name not in repr_without_object_addresses(item)


def test_check_result_queue_repr_hides_programs_from_ending_revisions(
    service: tuple[Rig, CheckDouble],
) -> None:
    rig, checks = service
    private = ProgramKey("private-revision-program", "secret-previous-binary")
    observation(rig, "call-ended", program=private)
    run = checks.plan()
    rig.state.handle(request("with-end-revision"))
    run.complete()
    call_ended = rig.state.messages.get_nowait()
    result = rig.state.messages.get_nowait()
    assert call_ended["type"] == "detector-call-ended" and result["type"] == "check-result"
    assert result["attempt"].end_revisions == {private: 1}
    represented = repr_without_object_addresses(result)
    assert private.app_id not in represented and private.binary not in represented
    rig.state.messages.put(call_ended)
    rig.state.messages.put(result)
    (card,) = of_type(rig.poll(), "call.state")
    assert card["active"]


def test_observer_diagnostic_snapshot_is_private() -> None:
    src = Path(__file__).resolve().parents[2] / "src" / "astra_voice"
    detector_module = src / "worker" / "meeting_detector.py"
    external: list[tuple[Path, int]] = []
    for path in src.rglob("*.py"):
        if path == detector_module:
            continue
        tree = ast.parse(path.read_bytes(), filename=str(path.relative_to(src)))
        external.extend(
            (path.relative_to(src), node.lineno)
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute) and node.attr == "diagnostic_snapshot"
        )
    assert not external


def test_real_checker_through_role_has_no_private_fields_or_audio(
    tmp_path: Path, h: Harness, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    caplog.set_level(logging.DEBUG)
    title = "private-meeting-title\x1b\u202e"
    oversized = ("private-binary-name" * 600)[: 10 * 1024]
    clients, streams = picture()
    # Include private, oversized and malformed props alongside a valid claimant.
    marked = (
        (
            replace(
                clients[0],
                properties={
                    **clients[0].properties,
                    "application.name": title,
                    "media.name": title,
                },
            ),
            replace(
                clients[0],
                index=40301,
                properties={
                    **clients[0].properties,
                    "application.process.binary": oversized,
                    "application.name": title,
                    "media.name": title,
                },
            ),
        ),
        tuple(
            replace(
                stream,
                properties={**stream.properties, "media.name": title},
                corked=False,
                muted=False,
            )
            for stream in streams
        ),
    )

    def observer_picture(pulse: ObserverPulse) -> None:
        pulse.clients = list(marked[0])
        pulse.streams = list(marked[1])

    steps = ObserverSteps()
    with monkeypatch.context() as patch:
        patch.setattr(meeting_detector, "load_apps", lambda: (APP,))
        observer = ObserverPulses(patch, cookies=(COOKIE,), setup=observer_picture)
        rig = Rig(
            tmp_path / "capture",
            observer_address=lambda: LOCAL,
            observer_factory=observer_metadata,
            observer_fds=lambda: {},
            observer_wait=steps.wait,
            check_factory=lambda *_: h.checker,
        )
        steps.clock = rig.clock
        h.checker._own_contexts = rig.state.observer.registry.snapshot
        try:
            detector(rig, "start", "observe-markers")
            steps.ready()
            steps.step(61)
            h.clock.now = rig.clock.now
            h.queue_pictures(marked)
            start = rig.until("meeting.call.started")
            assert len(observer.pulses) == 1
            assert observer.count("pa_context_get_client_info_list") > 0

            h.queue_pictures(marked)
            req = request("real-card")
            rig.state.handle(req)
            card = rig.until("meeting.call.state")
            assert card["call_id"] == start["call_id"]
            assert rig.state.handle(req)[-1] == card
            assert len(h.pulses) == 2

            h.pulse_failure = "query"
            rig.state.handle(request("pulse-error"))
            assert rig.until("meeting.call.state") == refused("pulse-error")
            h.pulse_failure = None
            altered = extra_claimant(marked)
            for _ in range(2):
                h.queue_pictures(marked, altered)
            rig.state.handle(request("changed"))
            changed = rig.until("meeting.call.state")
            assert changed["active"] and not changed["variant1"]
            assert {event["code"] for event in of_type(rig.events, "detector.code")} == {
                "pulse_failed",
                "picture_changed",
                "identity_mismatch_counter",
            }

            trace = (
                b"\n".join(frame[4:] for frame in wire(rig.events)).decode("utf-8") + caplog.text
            )
            trace += "".join(
                repr_without_object_addresses(item)
                for item in (rig.state, rig.state.calls, *rig.state.calls._instances.values())
            )
            for private in (
                title,
                "private-meeting-title",
                "\x1b",
                "\u202e",
                "private-binary-name",
                EXE,
                SERVER.decode(),
            ):
                assert private not in trace
            for private_number in (PID, CLIENT, OUT, MIC, COOKIE, 30001, 40301):
                # Standalone numbers avoid matches inside a randomly allocated UUID.
                assert not re.search(rf"(?<!\w){private_number}(?!\w)", trace), private_number
            assert len(h.pulses) >= 4
            assert all(
                not name.startswith(("pa_stream_", "pa_simple_"))
                for pulse in h.pulses
                for name in pulse.calls
            )
            assert not rig.mics and not rig.backend.sources
            observer.assert_no_pcm()
        finally:
            rig.state.terminate()
            steps.advance.set()
            rig.cleanup()


def test_role_checker_explicit_address_and_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with monkeypatch.context() as patch:
        constructed: list[tuple[bytes, dict[str, Any]]] = []
        checks = CheckDouble()

        def constructor(*, server: bytes, **kwargs: Any) -> CheckDouble:
            constructed.append((server, kwargs))
            return checks

        patch.setattr(meeting_identity_check, "IdentityCheck", constructor)
        patch.setattr(meeting_identity_check, "load_apps", lambda: (APP,))
        rig = Rig(tmp_path, observer_address=lambda: LOCAL, check_factory=role_checker)
        try:
            assert not constructed
            registry = rig.state.observer.registry
            registry.set("observer", (42, COOKIE))
            state, _ = checked_card(rig, checks, "role-factory")
            assert state["active"]
            ((server, options),) = constructed
            assert server == LOCAL.address and options["apps"] == (APP,)
            assert "output_limit" not in options
            assert options["clock"] is rig.clock
            assert options["own_contexts"]() == registry.snapshot()
            registry.set("control", (51, COOKIE))
            assert options["own_contexts"]().indexes == {42, 51}
        finally:
            checks.release_all()
            rig.cleanup()


@pytest.mark.parametrize("first", [Purpose.CARD, Purpose.OFFER])
@pytest.mark.parametrize("failure", ["exception", "empty"])
def test_role_checker_reloads_catalog_after_a_transient_error(
    tmp_path: Path,
    h: Harness,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    first: Purpose,
    failure: str,
) -> None:
    loads: list[bool] = []
    created: list[tuple[AppDefinition, ...]] = []
    private_error = "private transient catalog error"

    def load_apps() -> tuple[AppDefinition, ...]:
        loads.append(True)
        if len(loads) == 1:
            if failure == "exception":
                raise AppDataError(private_error)
            return ()
        return (APP,)

    def build(*args: Any, **kwargs: Any) -> meeting_identity_check.IdentityCheck:
        # Preserve the constructor's interpretation of a missing apps argument.
        kwargs.update(proc=h.proc, metadata=h.metadata, clock=h.clock)
        checker = type(h.checker)(*args, **kwargs)
        created.append(checker._apps)
        return checker

    with monkeypatch.context() as patch, caplog.at_level(logging.DEBUG):
        patch.setattr(meeting_identity_check, "load_apps", load_apps)
        patch.setattr(meeting_identity_check, "IdentityCheck", build)
        rig = Rig(tmp_path / "capture", observer_address=lambda: LOCAL, check_factory=role_checker)
        messages = SignalQueue("check-result")
        patch.setattr(rig.state, "messages", messages)
        rig.clock.now = h.clock()
        rig.state.observer.registry.set("observer", (42, COOKIE))
        try:
            if first == Purpose.CARD:
                assert rig.state.handle(request("catalog-error"))[0]["type"] == "meeting.ack"
                assert rig.poll() == [diagnostic("card", "pulse_failed"), refused("catalog-error")]
            else:
                assert submit_candidate(rig) == [diagnostic("offer", "pulse_failed")]
                rig.clock.now += 2
                assert rig.poll() == []
            assert loads == [True] and not created
            assert private_error not in caplog.text
            h.clock.now = rig.clock.now
            messages.arrived.clear()
            assert rig.state.handle(request("catalog-restored"))[0]["type"] == "meeting.ack"
            assert messages.arrived.wait(2)
            (restored,) = of_type(rig.poll(), "call.state")
            assert restored["req"] == "catalog-restored" and restored["active"]
            assert loads == [True, True] and created == [(APP,)]
        finally:
            rig.cleanup()


def test_real_checker_connect_uses_shared_lock_but_ready_and_lists_do_not(
    tmp_path: Path, h: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    check_connecting, proceed = threading.Event(), threading.Event()
    observer_waiting, observer_connected = threading.Event(), threading.Event()
    observer_unlocked = threading.Event()
    lock_states: list[tuple[str, bool]] = []
    connect_inputs: list[tuple[Any, ...]] = []
    catalog_env: list[str | None] = []

    def load_apps() -> tuple[AppDefinition, ...]:
        catalog_env.append(os.environ.get("ASTRA_VOICE_RESOURCES"))
        return (APP,)

    conf = tmp_path / "client.conf"
    conf.write_bytes(b"default-server = tcp:private-test-host:4713\n")
    fallback = conf.read_bytes().partition(b"=")[2].strip()

    class CheckNative(Pulse):
        def __init__(self) -> None:
            super().__init__(h, (picture(), picture()))
            h.pulses.append(self)

        def call(self, name: str, *args: Any) -> Any:
            if name in {"pa_context_new", "pa_context_connect", "pa_context_get_state"} or (
                name.endswith("_info_list")
            ):
                lock_states.append((name, CONNECT_LOCK.locked()))
            if name == "pa_context_connect":
                connect_inputs.append(args[1:] if args[1] is not None else (fallback, *args[2:]))
                check_connecting.set()
                if not proceed.wait(2):
                    raise TimeoutError
            return super().call(name, *args)

    class ObserverStub(DetectorBackend):
        def server_cookie(self) -> int:
            return COOKIE

    def observer_factory(server: bytes, connect: ObserverConnect) -> ObserverStub:
        def native() -> int:
            observer_connected.set()
            return 0

        # Use the real observer's connect gate. Its metadata stays independent
        # of the check Harness's virtual Pulse clock during the race.
        observer_waiting.set()
        connect(native)
        observer_unlocked.set()
        return ObserverStub()

    def constructor(**options: Any) -> meeting_identity_check.IdentityCheck:
        checker = h.checker
        checker._metadata = options["metadata"]
        return checker

    with monkeypatch.context() as patch:
        patch.setenv("ASTRA_VOICE_RESOURCES", str(tmp_path / "foreign-resources"))
        patch.delenv("ASTRA_VOICE_RESOURCES", raising=False)
        patch.setattr(meeting_identity_check, "load_apps", load_apps)
        patch.setattr(meeting_detector, "load_apps", load_apps)
        patch.setattr(pulse_stream, "_PulseAsync", CheckNative)
        patch.setattr(meeting_identity_check, "IdentityCheck", constructor)
        # The Harness subclass uses this module binding in its annotation only.
        rig = Rig(
            tmp_path / "capture",
            observer_address=lambda: LOCAL,
            check_factory=role_checker,
            observer_factory=observer_factory,
            observer_fds=lambda: {},
        )
        rig.clock.now = h.clock()
        try:
            assert rig.state.handle(request("lock"))[0]["type"] == "meeting.ack"
            assert check_connecting.wait(2)
            detector(rig, "start", "observer-waits-for-check")
            assert observer_waiting.wait(2)
            assert not observer_connected.is_set()
            proceed.set()
            assert observer_connected.wait(2)
            assert rig.until("meeting.call.state")["req"] == "lock"
            assert observer_unlocked.wait(2)
            first_count = len(lock_states)
            assert rig.state.handle(request("no-competing-connect"))[0]["type"] == "meeting.ack"
            assert rig.until("meeting.call.state")["req"] == "no-competing-connect"
        finally:
            proceed.set()
            rig.cleanup()
    assert catalog_env == [None, None]
    assert connect_inputs == [(LOCAL.address, pulse_stream.PA_CONTEXT_NOAUTOSPAWN, None)] * 2
    locked = {"pa_context_new", "pa_context_connect"}
    assert locked <= {name for name, held in lock_states if held}
    assert all(held for name, held in lock_states if name in locked)
    isolated = lock_states[first_count:]
    assert {"pa_context_get_state", "pa_context_get_client_info_list"} <= {
        name for name, held in isolated if not held
    }
    assert all(not held for name, held in isolated if name not in locked)


def test_nonunix_transport_from_real_checker_closes_context(tmp_path: Path, h: Harness) -> None:
    h.family = socket.AF_INET
    rig = Rig(
        tmp_path / "capture", observer_address=lambda: LOCAL, check_factory=lambda *_: h.checker
    )
    rig.clock.now = h.clock()
    try:
        assert rig.state.handle(request("real-remote"))[0]["type"] == "meeting.ack"
        assert rig.until("meeting.call.state") == refused("real-remote")
        assert of_type(rig.events, "detector.code")[0]["code"] == "remote"
        assert rig.state.observer.automation_off.is_set()
        assert not h.connected and h.control_closed == 1
        assert rig.state.handle(request("second")) == [refused("second")]
        assert len(h.pulses) == 1
    finally:
        rig.cleanup()
