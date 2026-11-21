"""E13.M8 6a-1: START admission, pinned capture and role outcomes."""

from __future__ import annotations

import ctypes
import queue
import re
import threading
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest
from test_meeting_capture import MID, Backend, Rig, Source
from test_meeting_m8_call_service import (
    ALLOWED,
    ANCHOR,
    LOCAL,
    TARGET,
    CheckDouble,
    checked_card,
    observation,
    of_type,
    request,
    server_observation,
    wire,
)
from test_meeting_m8_call_service import service as service
from test_meeting_m8_identity_check import APP, CLIENT, COOKIE, OUT, PID, Harness, picture, props
from test_meeting_m8_identity_check import h as h
from test_meeting_m8_repr import repr_without_object_addresses
from test_monitor_stream import RESOLVED, Pulse, fake_native_control

from astra_voice.meetings.identity import ProcReader, Reason
from astra_voice.worker import meeting_identity_check, monitor_stream
from astra_voice.worker.meeting_call_service import role_checker
from astra_voice.worker.meeting_identity_check import (
    CHECK_SECONDS,
    CallAnchor,
    PinnedTarget,
    Purpose,
    TargetError,
    Unsettled,
)
from astra_voice.worker.monitor_stream import MonitorStreamSource, Target

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("filesystem_euid")]


class CaptureEvents(queue.SimpleQueue[dict[str, Any]]):
    def __init__(self, rig: Rig, *kinds: str) -> None:
        super().__init__()
        self.rig = rig
        self.signals = {kind: threading.Event() for kind in kinds}

    def put(
        self, item: dict[str, Any], /, block: bool = True, timeout: float | None = None
    ) -> None:
        super().put(item, block, timeout)
        ready = self.signals.get(item["type"])
        if ready is not None:
            ready.set()

    def wait(self, kind: str) -> None:
        ready = self.signals[kind]
        assert ready.wait(3), kind
        ready.clear()

    def finish(self, kind: str, reply: str) -> dict[str, Any]:
        self.wait(kind)
        self.rig.poll()
        # One poll may publish several replies, including those we wait for next.
        for index in range(len(self.rig.inbox) - 1, -1, -1):
            if self.rig.inbox[index]["type"] == reply:
                return self.rig.inbox.pop(index)
        pytest.fail(f"expected role reply {reply}")


@pytest.fixture
def capture_events(
    service: tuple[Rig, CheckDouble], monkeypatch: pytest.MonkeyPatch
) -> Iterator[CaptureEvents]:
    rig, _ = service
    events = CaptureEvents(
        rig,
        "prepared",
        "prepare-failed",
        "opened",
        "open-failed",
        "closed",
        "finalized",
        "track-ended",
        "audio-failed",
    )
    with monkeypatch.context() as patch:
        patch.setattr(rig.state, "messages", events)
        yield events


def app_far(rig: Rig, checks: CheckDouble) -> dict[str, Any]:
    card, _ = checked_card(rig, checks, "card", ALLOWED)
    assert card["active"] and card["variant1"]
    return {"kind": "app_stream", "call_id": card["call_id"]}


def request_prepare(rig: Rig, far: dict[str, Any]) -> None:
    identity = rig.identity
    rig.identity = {key: identity[key] for key in ("meeting_id", "meeting_generation")}
    try:
        assert [item["type"] for item in rig.request_prepare(far)] == ["meeting.ack"]
    finally:
        rig.identity = identity


def prepared(rig: Rig, events: list[dict[str, Any]]) -> dict[str, Any]:
    (reply,) = of_type(events, "capture.prepared")
    rig.identity.update({key: reply[key] for key in ("capture_id", "state_rev")})
    return reply


def prepare_allowed(
    rig: Rig, checks: CheckDouble, far: dict[str, Any], target: PinnedTarget
) -> dict[str, Any]:
    run = checks.plan(replace(ALLOWED, target=target))
    request_prepare(rig, far)
    run.complete()
    return prepared(rig, rig.poll())


def errors(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [event for event in events if event["type"] == "error"]


def diagnostics(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [event for event in of_type(events, "detector.code") if event["purpose"] == "start"]


def admission(rig: Rig) -> dict[str, int]:
    return {
        "blocked_epoch": rig.state.session.snapshot().blocked_epoch,
        "admission_mono_ms": int(rig.clock() * 1000),
    }


def open_capture(rig: Rig, events: CaptureEvents) -> dict[str, Any]:
    assert rig.send("open", **admission(rig))[0]["type"] == "meeting.ack"
    return events.finish("opened", "meeting.capture.started")


TWO_STREAMS = replace(
    TARGET, streams=(*TARGET.streams, replace(TARGET.streams[0], sink_input=4321))
)


@pytest.mark.parametrize(
    "changes,code",
    [
        ({"settled": False}, "target-mismatch"),
        ({"variant1": False}, "target-mismatch"),
        ({"target": None}, "target-mismatch"),
        ({"target": replace(TARGET, streams=())}, "target-mismatch"),
        ({"target": TWO_STREAMS}, "target-mismatch"),
        ({"variant1": False, "offer": False}, "target-mismatch"),
        ({"variant1": False, "journal": (Reason.EXE,)}, "identity_mismatch"),
        ({"variant1": False, "journal": (Reason.COUNTER,)}, "identity_mismatch_counter"),
        ({}, None),
    ],
    ids=[
        "unsettled",
        "no-variant1",
        "no-target",
        "empty",
        "two",
        "no-call",
        "identity",
        "counter",
        "ok",
    ],
)
def test_start_admission_and_diagnostics(
    service: tuple[Rig, CheckDouble], changes: dict[str, Any], code: str | None
) -> None:
    rig, checks = service
    far = app_far(rig, checks)
    check = replace(ALLOWED, target=replace(TARGET, measured_at=rig.clock()))
    run = checks.plan(replace(check, **changes))
    request_prepare(rig, far)
    assert not rig.backend.prepare_calls and not rig.mics and not rig.backend.sources
    run.complete()
    events = rig.poll()
    if code is None:
        prepared(rig, events)
        assert rig.backend.prepare_calls == [far]
        assert rig.backend.prepare_targets[0] is check.target is rig.state.prepared_target
        assert not diagnostics(events)
    else:
        (error,) = errors(events)
        assert (error["request_type"], error["code"]) == (
            "meeting.capture.prepare",
            "target-mismatch",
        )
        assert not rig.backend.prepare_calls and rig.state.backend is None
        assert rig.state.prepared_target is None
        (diagnostic,) = diagnostics(events)
        assert diagnostic["code"] == code
    assert not rig.mics and not rig.backend.sources


@pytest.mark.parametrize("code", [reason for reason in Unsettled if reason is not Unsettled.REMOTE])
def test_start_preserves_concrete_check_diagnostic(
    service: tuple[Rig, CheckDouble], code: Unsettled
) -> None:
    rig, checks = service
    far = app_far(rig, checks)
    run = checks.plan(replace(ALLOWED, settled=False, variant1=False, target=None, unsettled=code))
    request_prepare(rig, far)
    run.complete()
    events = rig.poll()
    (error,) = errors(events)
    (diagnostic,) = diagnostics(events)
    assert error["code"] == "target-mismatch" and diagnostic["code"] == code


def test_start_missing_anchor_never_runs_checker_or_backend(
    service: tuple[Rig, CheckDouble], capture_events: CaptureEvents
) -> None:
    rig, checks = service
    request_prepare(rig, {"kind": "app_stream", "call_id": "unknown"})
    error = capture_events.finish("prepare-failed", "error")
    assert error["code"] == "target-gone"
    (diagnostic,) = diagnostics(rig.events)
    assert diagnostic["code"] == "target-gone" and diagnostic["duration_ms"] == 0
    assert not checks.requests and not checks.created
    assert rig.state.backend is None and not rig.backend.prepare_calls
    assert not rig.mics and not rig.backend.sources


def test_start_uses_arrival_deadline_and_current_anchor(
    service: tuple[Rig, CheckDouble], monkeypatch: pytest.MonkeyPatch
) -> None:
    rig, checks = service
    far = app_far(rig, checks)
    rig.backend.context = (51, COOKIE)
    anchor_for = rig.state.calls.anchor_for
    anchor = anchor_for(far["call_id"])
    control_thread = threading.current_thread()

    def delayed_anchor(call_id: str) -> CallAnchor | None:
        assert threading.current_thread() is control_thread
        found = anchor_for(call_id)
        rig.clock.now += 0.2
        return found

    assert rig.clock.now == 100.0 and anchor is ANCHOR
    run = checks.plan(replace(ALLOWED, target=replace(TARGET, measured_at=100.2)))
    with monkeypatch.context() as patch:
        # Exercise lazy START creation even though CARD has already published an anchor.
        patch.setattr(rig.state.calls, "_checker", None)
        patch.setattr(rig.state.calls, "anchor_for", delayed_anchor)
        request_prepare(rig, far)
        check = run.started()
        assert check.purpose is Purpose.START and check.program is None and check.anchor is anchor
        assert check.deadline == 100.0 + CHECK_SECONDS
        assert checks.created == [control_thread, control_thread]
        assert run.thread is not None and run.thread.name == "meeting-prepare"
        assert rig.state.observer.registry.snapshot().indexes == {42}
        run.complete()
        prepared(rig, rig.poll())
    assert rig.state.observer.registry.snapshot().indexes == {42, 51}


def test_start_replays_same_request_and_measures_each_new_request(
    service: tuple[Rig, CheckDouble],
) -> None:
    rig, checks = service
    far = app_far(rig, checks)
    first = replace(TARGET, measured_at=rig.clock())
    run = checks.plan(replace(ALLOWED, target=first))
    request_prepare(rig, far)
    message = rig.commands[-1]
    run.started()
    assert [item["type"] for item in rig.state.handle(message)] == ["meeting.ack"]
    run.complete()
    reply = prepared(rig, rig.poll())
    assert rig.state.handle(message)[-1] == reply
    assert [check.purpose for check in checks.requests] == [Purpose.CARD, Purpose.START]
    rig.clock.now += 1
    second = replace(
        first,
        measured_at=rig.clock(),
        streams=(replace(first.streams[0], stream_serial=first.streams[0].stream_serial + 1),),
    )
    prepare_allowed(rig, checks, far, second)
    assert rig.backend.prepare_targets[0] is first
    assert rig.backend.prepare_targets[1] is second is rig.state.prepared_target
    assert [check.purpose for check in checks.requests] == [
        Purpose.CARD,
        Purpose.START,
        Purpose.START,
    ]


@pytest.mark.parametrize("age", [2.999, 3.0])
def test_first_open_checks_target_age_separately_from_admission(
    service: tuple[Rig, CheckDouble], capture_events: CaptureEvents, age: float
) -> None:
    rig, checks = service
    far = app_far(rig, checks)
    target = replace(TARGET, measured_at=rig.clock())
    prepare_allowed(rig, checks, far, target)
    rig.clock.now += age
    if age < 3.0:
        assert open_capture(rig, capture_events)["tracks"] == ["mic", "far"]
        assert rig.backend.source_targets[0] is target
    else:
        for _ in range(2):
            (error,) = rig.send("open", **admission(rig))
            assert (
                error["request_type"] == "meeting.capture.open" and error["code"] == "target-gone"
            )
        assert rig.state.prepared_target is None and rig.state.state == "prepared"
        assert not rig.backend.source_targets and not rig.mics and not rig.state.used_meetings
        assert not of_type(rig.poll(), "capture.closed")


@pytest.mark.parametrize("boundary", ["before-prepared", "before-open"])
def test_server_change_invalidates_start_target(
    service: tuple[Rig, CheckDouble], boundary: str
) -> None:
    rig, checks = service
    far = app_far(rig, checks)
    run = checks.plan(replace(ALLOWED, target=replace(TARGET, measured_at=rig.clock())))
    request_prepare(rig, far)
    run.complete()
    if boundary == "before-open":
        prepared(rig, rig.poll())
    server_observation(rig, COOKIE + 1)
    events = rig.poll()
    (ended,) = of_type(events, "call.ended")
    assert ended["reason"] == "server_changed"
    assert rig.state.calls.anchor_for(far["call_id"]) is None
    if boundary == "before-prepared":
        assert not of_type(events, "capture.prepared")
        (diagnostic,) = diagnostics(events)
        assert diagnostic["code"] == "target-gone"
        (error,) = errors(events)
        assert error["request_type"] == "meeting.capture.prepare"
    else:
        (error,) = rig.send("open", **admission(rig))
        assert error["request_type"] == "meeting.capture.open"
    assert error["code"] == "target-gone"
    assert not rig.mics and not rig.backend.source_targets
    assert rig.state.prepared_target is None


def test_session_lock_after_prepare_refuses_open_before_any_source(
    service: tuple[Rig, CheckDouble],
) -> None:
    rig, checks = service
    far = app_far(rig, checks)
    prepare_allowed(rig, checks, far, replace(TARGET, measured_at=rig.clock()))
    press = admission(rig)
    rig.state.session.signal("Lock")
    (error,) = rig.send("open", **press)
    assert error["code"] == "admission-stale"
    assert rig.state.prepared_target is None and not rig.mics and not rig.backend.source_targets


def test_lock_during_start_invalidates_first_open_even_after_unlock(
    service: tuple[Rig, CheckDouble],
) -> None:
    rig, checks = service
    far = app_far(rig, checks)
    run = checks.plan(replace(ALLOWED, target=replace(TARGET, measured_at=rig.clock())))
    request_prepare(rig, far)
    run.started()
    session = rig.state.session
    session.signal("Lock")
    session.signal("Unlock")
    session.update(known=True, active=True, locked=False, sleeping=False)
    rig.poll()
    run.complete()
    prepared(rig, rig.poll())
    (error,) = rig.send("open", **admission(rig))
    assert error["code"] == "admission-stale"
    assert rig.state.prepared_target is None and not rig.mics and not rig.backend.source_targets


@pytest.mark.parametrize("discard", ["failure", "stop", "shutdown", "replace"])
def test_prepared_target_is_discarded(
    service: tuple[Rig, CheckDouble], capture_events: CaptureEvents, discard: str
) -> None:
    rig, checks = service
    far = app_far(rig, checks)
    prepare_allowed(rig, checks, far, replace(TARGET, measured_at=rig.clock()))
    if discard == "failure":
        rig.clock.now += 1
        run = checks.plan(replace(ALLOWED, variant1=False, target=None))
        request_prepare(rig, far)
        run.complete()
        (error,) = errors(rig.poll())
        assert error["code"] == "target-mismatch"
        assert rig.send("open", **admission(rig))[0]["code"] == "not-prepared"
    elif discard == "replace":
        capture_events.wait("prepared")
        request_prepare(rig, {"kind": "sink_monitor"})
        capture_events.finish("prepared", "meeting.capture.prepared")
        assert rig.backend.prepare_targets[-1] is None
    else:
        rig.send(discard)
    assert rig.state.prepared_target is None and not rig.mics and not rig.backend.source_targets


@pytest.mark.parametrize("target_error", [TargetError.GONE, TargetError.MISMATCH])
def test_start_preserves_target_error_from_anchor_check(
    service: tuple[Rig, CheckDouble], target_error: TargetError
) -> None:
    rig, checks = service
    far = app_far(rig, checks)
    run = checks.plan(
        replace(ALLOWED, offer=False, variant1=False, target=None, target_error=target_error)
    )
    request_prepare(rig, far)
    run.complete()
    events = rig.poll()
    (error,) = errors(events)
    (diagnostic,) = diagnostics(events)
    assert error["code"] == diagnostic["code"] == target_error


def test_start_remote_disables_automation(
    service: tuple[Rig, CheckDouble], monkeypatch: pytest.MonkeyPatch
) -> None:
    rig, checks = service
    far = app_far(rig, checks)
    run = checks.plan(
        replace(ALLOWED, settled=False, variant1=False, target=None, unsettled=Unsettled.REMOTE)
    )
    stop = Mock(wraps=rig.state.observer.stop)
    with monkeypatch.context() as patch:
        patch.setattr(rig.state.observer, "stop", stop)
        request_prepare(rig, far)
        run.complete()
        events = rig.poll()
        stop.assert_called_once_with()
    (error,) = errors(events)
    assert error["code"] == "target-mismatch"
    (diagnostic,) = diagnostics(events)
    assert diagnostic["code"] == "remote"
    (ended,) = of_type(events, "call.ended")
    assert ended["reason"] == "detector_off"
    assert rig.state.observer.automation_off.is_set()
    (card,) = rig.state.handle(request("card-after-remote"))
    assert card["type"] == "meeting.call.state" and card["active"] is False
    assert [check.purpose for check in checks.requests] == [Purpose.CARD, Purpose.START]


@pytest.mark.parametrize("failure", ["factory", "runner"])
def test_start_check_exceptions_are_reported_from_poll(
    service: tuple[Rig, CheckDouble],
    capture_events: CaptureEvents,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    rig, checks = service
    far = app_far(rig, checks)
    emit = Mock(wraps=rig.state.calls._emit_code)
    threads: list[threading.Thread] = []
    original_emit = rig.state.calls._emit_code

    def observe_emit(*args: Any) -> None:
        threads.append(threading.current_thread())
        original_emit(*args)

    emit.side_effect = observe_emit
    with monkeypatch.context() as patch:
        patch.setattr(rig.state.calls, "_emit_code", emit)
        if failure == "factory":
            patch.setattr(rig.state.calls, "_checker", None)
            patch.setattr(rig.state.calls, "_factory", Mock(side_effect=OSError("private failure")))
        else:
            run = checks.plan(OSError("private failure"))
        request_prepare(rig, far)
        if failure == "runner":
            run.complete()
        capture_events.wait("prepare-failed")
        emit.assert_not_called()
        events = rig.poll()
    (error,) = errors(events)
    (diagnostic,) = diagnostics(events)
    assert error["code"] == "target-mismatch" and diagnostic["code"] == "pulse_failed"
    assert threads == [threading.current_thread()]


def test_private_start_target_is_only_delivered_to_role(
    service: tuple[Rig, CheckDouble], caplog: pytest.LogCaptureFixture
) -> None:
    rig, checks = service
    far = app_far(rig, checks)
    process = replace(TARGET.streams[0].process, pid=7654321, starttime=654321987)
    stream = replace(TARGET.streams[0], stream_serial=987654321, client=876543, process=process)
    target = replace(TARGET, server_cookie=54321987, streams=(stream,), measured_at=rig.clock())
    rig.backend.context = (51, target.server_cookie)
    run = checks.plan(replace(ALLOWED, target=target))
    request_prepare(rig, far)
    run.complete()
    event = rig.state.messages.get_nowait()
    assert event["target"] is target and "target" not in event["info"] and "target" not in far
    rig.state.messages.put(event)
    prepared(rig, rig.poll())
    assert rig.state.prepared_target is target
    trace = b"\n".join(wire(rig.events)).decode("latin-1") + "\n" + caplog.text
    trace += "".join(
        repr_without_object_addresses(item)
        for item in (rig.state, rig.state.calls, target, stream, event, run.result)
    )
    for number in (
        stream.stream_serial,
        stream.client,
        process.pid,
        process.starttime,
        target.server_cookie,
    ):
        assert not re.search(rf"(?<!\w){number}(?!\w)", trace)
    assert not diagnostics(rig.events)


@pytest.mark.parametrize("outputs,client_pid_only", [(1, False), (2, False), (1, True)])
def test_native_identity_check_limits_start_to_one_output(
    tmp_path: Path, h: Harness, monkeypatch: pytest.MonkeyPatch, outputs: int, client_pid_only: bool
) -> None:
    def build(*args: Any, **kwargs: Any) -> meeting_identity_check.IdentityCheck:
        kwargs.update(proc=h.proc, metadata=h.metadata, clock=h.clock)
        return type(h.checker)(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(meeting_identity_check, "IdentityCheck", build)
        patch.setattr(meeting_identity_check, "load_apps", lambda: (APP,))
        rig = Rig(tmp_path / "capture", observer_address=lambda: LOCAL, check_factory=role_checker)
        events = CaptureEvents(rig, "check-result", "prepared", "prepare-failed")
        patch.setattr(rig.state, "messages", events)
        rig.state.observer.registry.set("observer", (42, COOKIE))
        h.clock.now = rig.clock.now
        clients, streams = picture()
        if client_pid_only:
            streams = tuple(
                replace(
                    stream,
                    properties={
                        name: value
                        for name, value in stream.properties.items()
                        if name != "application.process.id"
                    },
                )
                for stream in streams
            )
            h.queue_pictures((clients, streams))
            native = fake_native_control(
                patch,
                Target(OUT, 201, PID, COOKIE, RESOLVED.monitor, CLIENT, 91),
                stream_props={b"object.serial": b"201"},
                client_props={b"application.process.id": str(PID).encode()},
                server_local=False,
            )
            reader = ProcReader(h.proc.root, uid=h.proc.root.stat().st_uid)
            patch.setattr(monitor_stream, "ProcReader", lambda **_: reader)
            patch.setattr(monitor_stream, "NativePulse", lambda: native)
            patch.setattr(rig.state, "backend_factory", monitor_stream.CapturePulseBackend)
        try:
            rig.state.handle(request("real-card"))
            card = events.finish("check-result", "meeting.call.state")
            assert card["variant1"]
            if outputs == 2:
                streams = (
                    *streams,
                    replace(streams[0], index=OUT + 1, properties=props(serial=202)),
                )
            h.queue_pictures((clients, streams))
            far = {"kind": "app_stream", "call_id": card["call_id"]}
            request_prepare(rig, far)
            if outputs == 1:
                events.wait("prepared")
                reply = prepared(rig, rig.poll())
                assert reply["far_available"]
                target = rig.state.prepared_target
                assert target is not None
                assert len(target.streams) == 1
                if client_pid_only:
                    assert reply["server_local"] is False
                    assert native.resolve(OUT, expected_pid=PID) == resolved_target(target)
                else:
                    assert rig.backend.prepare_targets[0] is target
            else:
                assert events.finish("prepare-failed", "error")["code"] == "target-mismatch"
                assert not rig.backend.prepare_calls
            assert len(h.pulses) == 2
            assert not rig.mics and not rig.backend.sources
        finally:
            rig.cleanup()


def pause_capture(rig: Rig, events: CaptureEvents, action: str) -> None:
    if action == "resume":
        rig.send("pause", reason="user")
    else:
        rig.send("keepalive", seq=1, indicators_visible=True, **admission(rig))
        rig.clock.now += 4
        (paused,) = of_type(rig.poll(), "capture.autopaused")
        assert paused["reason"] == "lease"
    events.wait("closed")
    rig.poll()
    events.finish("finalized", "meeting.capture.finalized")


@pytest.mark.parametrize("code", [None, "target-gone", "target-mismatch"])
@pytest.mark.parametrize("action", ["resume", "keepalive"])
def test_resume_after_target_age_limit_uses_same_target_and_keeps_mic(
    service: tuple[Rig, CheckDouble],
    capture_events: CaptureEvents,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
    code: str | None,
) -> None:
    rig, checks = service
    far = app_far(rig, checks)
    target = replace(TARGET, measured_at=rig.clock())
    prepare_allowed(rig, checks, far, target)
    open_capture(rig, capture_events)
    pause_capture(rig, capture_events, action)
    rig.clock.now += 3
    source = rig.backend.source

    def reopen(far: dict[str, Any], *, target: PinnedTarget) -> Source:
        stream = source(far, target=target)
        if code is not None:
            stream.open = Mock(side_effect=ValueError(code))  # type: ignore[method-assign]
        return stream

    with monkeypatch.context() as patch:
        patch.setattr(rig.backend, "source", reopen)
        fields = {"seq": 2, "indicators_visible": True} if action == "keepalive" else {}
        assert rig.send(action, **fields, **admission(rig))[0]["type"] == "meeting.ack"
        capture_events.wait("opened")
        events = rig.poll()
    assert rig.backend.source_targets[0] is rig.backend.source_targets[1] is target
    assert rig.state.prepared_target is target
    assert rig.state.track_states["mic"] == "live" and rig.mics[-1].opened
    assert rig.state.state == "recording" and not rig.state.manual_resume_required
    assert not errors(events) and not of_type(events, "capture.autopaused")
    if code is None:
        assert rig.state.track_states["far"] == "live"
        assert rig.backend.sources[-1].opened
        assert not of_type(events, "capture.track")
    else:
        (ended,) = of_type(events, "capture.track")
        assert (ended["track"], ended["state"], ended["kind"]) == ("far", "ended", "target-removed")
        assert not rig.backend.sources[-1].opened
        assert rig.state.track_states["far"] == "ended"
    if action == "resume":
        (started,) = of_type(events, "capture.started")
        assert started["tracks"] == (["mic", "far"] if code is None else ["mic"])
        if code == "target-mismatch":
            pause_capture(rig, capture_events, action)
            assert (
                rig.send(action, tracks=["mic", "far"], **admission(rig))[0]["type"]
                == "meeting.ack"
            )
            started = capture_events.finish("opened", "meeting.capture.started")
            assert started["tracks"] == ["mic"] and len(rig.backend.source_targets) == 2
    else:
        assert not of_type(events, "capture.started")


def resolved_target(pinned: PinnedTarget) -> Target:
    (stream,) = pinned.streams
    return replace(
        RESOLVED,
        index=stream.sink_input,
        serial=stream.stream_serial,
        pid=stream.process.pid,
        cookie=pinned.server_cookie,
        client=stream.client,
        starttime=stream.process.starttime,
    )


@pytest.mark.parametrize("code", ["target-gone", "target-mismatch"])
@pytest.mark.parametrize("connect_fails", [False, True])
def test_open_preserves_backend_target_error(
    service: tuple[Rig, CheckDouble],
    capture_events: CaptureEvents,
    monkeypatch: pytest.MonkeyPatch,
    code: str,
    connect_fails: bool,
) -> None:
    rig, checks = service
    far = app_far(rig, checks)
    target = replace(TARGET, measured_at=rig.clock())
    prepare_allowed(rig, checks, far, target)
    pulse = Pulse()
    initial = resolved_target(target)
    pulse.target = initial
    pulse.after_open.append(None if code == "target-gone" else replace(initial, serial=123))
    if connect_fails:
        pulse.open_error = ValueError("audio-failed")
    mic = rig.mic()
    mic.chunks.append(b"\1\0" * 1600)
    read_mic = Mock(wraps=mic.read_chunk)

    def make_source(selection: dict[str, Any], *, target: PinnedTarget) -> MonitorStreamSource:
        assert selection == far and target is rig.state.prepared_target
        assert mic.opened
        return MonitorStreamSource(pulse, "run1", target=target)

    with monkeypatch.context() as patch:
        patch.setattr(rig.state, "mic_factory", lambda: mic)
        patch.setattr(rig.backend, "source", make_source)
        patch.setattr(mic, "read_chunk", read_mic)
        assert rig.send("open", **admission(rig))[0]["type"] == "meeting.ack"
        error = capture_events.finish("open-failed", "error")
        closed = capture_events.finish("closed", "meeting.capture.closed")
        capture_events.finish("finalized", "meeting.capture.finalized")
    assert (error["request_type"], error["code"]) == ("meeting.capture.open", code)
    assert closed["tracks"] == ["mic", "far"] and not mic.opened
    assert rig.state.prepared_target is None and MID in rig.state.used_meetings
    assert not capture_events.signals["opened"].is_set() and not of_type(
        rig.events, "capture.started"
    )
    read_mic.assert_not_called()
    assert list((rig.directory / "audio").iterdir()) == []
    assert ("read",) not in pulse.calls
    # The request is a new prepare for the same meeting, without capture identity.
    rig.identity = {"meeting_id": MID, "meeting_generation": 0}
    assert rig.request_prepare(far)[0]["code"] == "not-prepared"
    assert [check.purpose for check in checks.requests] == [Purpose.CARD, Purpose.START]


@pytest.mark.parametrize(
    "loss", ["removed", "client", "starttime", "missing", "connect-missing", "connect-client"]
)
def test_far_removed_ends_without_reopening_or_affecting_mic(
    service: tuple[Rig, CheckDouble],
    capture_events: CaptureEvents,
    monkeypatch: pytest.MonkeyPatch,
    loss: str,
) -> None:
    rig, checks = service
    far = app_far(rig, checks)
    target = replace(TARGET, measured_at=rig.clock())
    prepare_allowed(rig, checks, far, target)
    initial = resolved_target(target)
    pulse = Pulse()
    pulse.target = initial
    change = threading.Event()
    mic_archived = threading.Event()
    read_allowed = threading.Event()

    def read_mic() -> bytes:
        if read_allowed.is_set():
            return b"\1\0" * 1600
        return b""

    def pulse_events() -> list[tuple[str, int]]:
        if not change.is_set():
            return []
        change.clear()
        if loss == "removed":
            pulse.target = None
            return [("removed", initial.index)]
        moved = replace(initial, monitor="sink-b.monitor")
        pulse.target = moved
        replacement = {
            "missing": None,
            "client": replace(moved, client=moved.client + 1),
            "starttime": replace(moved, starttime=moved.starttime + 1),
        }
        if loss.startswith("connect-"):
            pulse.open_error = ValueError("audio-failed")
        pulse.after_open.append(replacement[loss.removeprefix("connect-")])
        return [("changed", initial.index)]

    with monkeypatch.context() as patch:
        source = Mock(
            side_effect=lambda _far, *, target: MonitorStreamSource(pulse, "run1", target=target)
        )
        patch.setattr(rig.backend, "source", source)
        patch.setattr(pulse, "poll_events", pulse_events)

        def read_far() -> bytes:
            pulse.calls.append(("read", pulse.target.monitor if pulse.target else None))
            return (
                b"\2\0" * 1600 if any(call[0] == "sink-b.monitor" for call in pulse.calls) else b""
            )

        patch.setattr(pulse, "read_chunk", read_far)
        open_capture(rig, capture_events)
        patch.setattr(rig.mics[0], "read_chunk", read_mic)
        assert rig.state.archive is not None
        put = rig.state.archive.put

        def archive_pcm(kind: str, payload: Any) -> bool:
            accepted = put(kind, payload)
            if accepted and kind == "pcm" and payload[0] == "mic":
                mic_archived.set()
            return accepted

        patch.setattr(rig.state.archive, "put", archive_pcm)
        change.set()
        track = capture_events.finish("track-ended", "meeting.capture.track")
        read_allowed.set()
        assert mic_archived.wait(2)
        rig.send("stop")
        capture_events.wait("closed")
        rig.poll()
        finalized = capture_events.finish("finalized", "meeting.capture.finalized")
        source.assert_called_once_with(far, target=target)
    assert (track["track"], track["state"], track["kind"]) == ("far", "ended", "target-removed")
    assert rig.state.track_states["far"] == "ended" and not rig.state.manual_resume_required
    assert not errors(rig.events) and not of_type(rig.events, "capture.autopaused")
    assert finalized["segments"] == {"mic": 1, "far": 0}
    assert ("read", "sink-b.monitor") not in pulse.calls
    assert len([call for call in pulse.calls if len(call) == 3]) == (1 if loss == "removed" else 2)


@pytest.mark.parametrize(
    "revocation", ["call-ended", "server", "server-same-batch", "detector-off"]
)
def test_anchor_revoked_while_first_open_waits_for_microphone(
    service: tuple[Rig, CheckDouble],
    capture_events: CaptureEvents,
    monkeypatch: pytest.MonkeyPatch,
    revocation: str,
) -> None:
    rig, checks = service
    far = app_far(rig, checks)
    target = replace(TARGET, measured_at=rig.clock())
    prepare_allowed(rig, checks, far, target)
    mic, far_source = rig.mic(), Source()
    mic_open = mic.open
    mic_entered, release = threading.Event(), threading.Event()
    read_far = Mock(return_value=b"\1\0" * 1600)

    def open_mic(device: str | None = None) -> None:
        mic_entered.set()
        assert release.wait(2)
        mic_open(device)

    with monkeypatch.context() as patch:
        patch.setattr(mic, "open", open_mic)
        patch.setattr(rig.state, "mic_factory", lambda: mic)
        patch.setattr(far_source, "read_chunk", read_far)
        patch.setattr(rig.backend, "source", Mock(return_value=far_source))
        assert rig.send("open", **admission(rig))[0]["type"] == "meeting.ack"
        try:
            assert mic_entered.wait(2)
            if revocation == "call-ended":
                observation(rig, "call-ended", program=ANCHOR.program)
                assert of_type(rig.poll(), "call.ended")
            elif revocation == "server":
                server_observation(rig, COOKIE + 1)
                assert of_type(rig.poll(), "call.ended")
            elif revocation == "detector-off":
                rig.state.observer.automation_off.set()
            else:
                release.set()
                capture_events.wait("opened")
                # The terminal event is queued before the server event in the same batch.
                server_observation(rig, COOKIE + 1)
        finally:
            release.set()
        if revocation != "server-same-batch":
            capture_events.wait("opened")
        (error,) = errors(rig.poll())
        assert error["request_type"] == "meeting.capture.open" and error["code"] == "target-gone"
        closed = capture_events.finish("closed", "meeting.capture.closed")
        finalized = capture_events.finish("finalized", "meeting.capture.finalized")
    assert closed["tracks"] == ["mic", "far"]
    assert not of_type(rig.events, "capture.started")
    assert not mic.opened and not far_source.opened
    read_far.assert_not_called()
    assert finalized["segments"] == {"mic": 0, "far": 0}
    assert list((rig.directory / "audio").iterdir()) == []


@pytest.mark.parametrize("action", ["resume", "keepalive"])
def test_resume_with_no_open_sources_keeps_pause_and_archive_unchanged(
    service: tuple[Rig, CheckDouble],
    capture_events: CaptureEvents,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
) -> None:
    rig, checks = service
    far = app_far(rig, checks)
    prepare_allowed(rig, checks, far, replace(TARGET, measured_at=rig.clock()))
    mic, mic_lost = rig.mic(), threading.Event()

    def read_mic() -> bytes | None:
        if mic_lost.is_set():
            mic.device_change = object()
            return None
        return b""

    with monkeypatch.context() as patch:
        patch.setattr(mic, "read_chunk", read_mic)
        patch.setattr(rig.state, "mic_factory", lambda: mic)
        open_capture(rig, capture_events)
        mic_lost.set()
        lost = capture_events.finish("track-ended", "meeting.capture.track")
        assert (lost["track"], lost["state"], lost["kind"]) == ("mic", "paused", "device-lost")
        pause_capture(rig, capture_events, action)
        recorded = rig.state._recorded()
        reasons = rig.state.pause_reasons.copy()
        failed = Source()
        patch.setattr(failed, "open", Mock(side_effect=ValueError("target-gone")))
        patch.setattr(rig.backend, "source", Mock(return_value=failed))
        assert rig.state.archive is not None
        archive = Mock(wraps=rig.state.archive.put)
        patch.setattr(rig.state.archive, "put", archive)
        fields = (
            {"seq": 2, "indicators_visible": True} if action == "keepalive" else {"tracks": ["far"]}
        )
        assert rig.send(action, **fields, **admission(rig))[0]["type"] == "meeting.ack"
        capture_events.wait("opened")
        events = rig.poll()
        (ended,) = of_type(events, "capture.track")
        assert (ended["track"], ended["state"], ended["kind"]) == ("far", "ended", "target-removed")
        assert not of_type(events, "capture.started")
        if action == "resume":
            (error,) = errors(events)
            assert (
                error["request_type"] == "meeting.capture.resume"
                and error["code"] == "not-prepared"
            )
        else:
            assert not errors(events)
        rig.clock.now += 1
        (alive,) = of_type(rig.poll(), "capture.alive")
        assert alive["state"] == "paused" and alive["recorded_ms"] == recorded
        assert rig.state.pause_reasons == reasons and not rig.state.sources
        assert not any(call.args[0] == "start" for call in archive.call_args_list)


@pytest.mark.parametrize("change_during_prepare", [False, True])
def test_server_change_retires_control_and_next_prepare_opens_fresh_context(
    service: tuple[Rig, CheckDouble],
    monkeypatch: pytest.MonkeyPatch,
    change_during_prepare: bool,
) -> None:
    rig, checks = service
    far = app_far(rig, checks)
    first = replace(TARGET, measured_at=rig.clock())
    new_cookie = COOKIE + 1
    second = replace(first, server_cookie=new_cookie)
    retired = threading.Event()
    with monkeypatch.context() as patch:
        contexts = []
        for target in (first, second):
            resolved = resolved_target(target)
            native = fake_native_control(
                patch,
                resolved,
                stream_props={b"object.serial": str(resolved.serial).encode()},
                client_props={b"application.process.id": str(resolved.pid).encode()},
            )
            contexts.append(native)
        patch.setattr(contexts[0], "close", retired.set)
        contexts[1].pulse.lib.pa_context_get_index.return_value = 52
        factory = Mock(side_effect=contexts)
        patch.setattr(monitor_stream, "NativePulse", factory)
        patch.setattr(
            monitor_stream,
            "ProcReader",
            Mock(return_value=Mock(starttime=Mock(return_value=resolved.starttime))),
        )
        patch.setattr(rig.state, "backend_factory", monitor_stream.CapturePulseBackend)
        prepare_allowed(rig, checks, far, first)
        assert factory.call_count == 1
        registry = rig.state.observer.registry
        assert registry.snapshot().indexes == {42, 51}

        if change_during_prepare:
            late = checks.plan(replace(ALLOWED, target=first))
            request_prepare(rig, far)
            late.started()
        server_observation(rig, new_cookie)
        assert of_type(rig.poll(), "call.ended")
        assert registry.snapshot().indexes == {42}
        if change_during_prepare:
            late.complete()
            (error,) = errors(rig.poll())
            assert error["code"] == "target-gone"
            assert registry.snapshot().indexes == {42}
        assert retired.wait(2)

        registry.set("observer", (43, new_cookie))
        result = replace(ALLOWED, anchor=replace(ANCHOR, server_cookie=new_cookie), target=second)
        card, _ = checked_card(rig, checks, "after-restart", result)
        fresh = checks.plan(result)
        request_prepare(rig, {"kind": "app_stream", "call_id": card["call_id"]})
        fresh.complete()
        assert prepared(rig, rig.poll())["far_available"]
        assert factory.call_count == 2
        assert registry.snapshot().indexes == {43, 52}


@pytest.mark.parametrize("moving", [False, True], ids=["first-open", "sink-moved"])
@pytest.mark.parametrize("fault", ["freed", "monitor", "client", "stream-denied", "removed"])
def test_native_connect_failure_reports_only_confirmed_target_loss(
    service: tuple[Rig, CheckDouble],
    capture_events: CaptureEvents,
    monkeypatch: pytest.MonkeyPatch,
    moving: bool,
    fault: str,
) -> None:
    rig, checks = service
    far = app_far(rig, checks)
    target = replace(TARGET, measured_at=rig.clock())
    prepare_allowed(rig, checks, far, target)
    resolved = resolved_target(target)
    move, failed = threading.Event(), threading.Event()
    lookups: list[int | None] = []
    with monkeypatch.context() as patch:
        native = fake_native_control(
            patch,
            resolved,
            stream_props={
                b"object.serial": str(resolved.serial).encode(),
                b"application.process.id": str(resolved.pid).encode(),
            },
            client_props={},
        )
        pulse = native.pulse
        native.stream, native.buffer, pulse.mainloop = None, bytearray(), 2
        pulse.context_get_state.return_value = 4
        pulse.context_errno.return_value = 5 if fault in {"removed", "monitor"} else 1
        pulse.stream_new.return_value = 3
        pulse.lib.pa_stream_set_monitor_stream.return_value = 0
        pulse.lib.pa_context_get_server.return_value = b"unix:/fake"

        def server_info(context: int | None, receive: Any, data: Any) -> int:
            lookups.append(context)
            assert context is not None
            info = monitor_stream._ServerInfo(cookie=resolved.cookie)
            receive(context, ctypes.pointer(info), data)
            return 1

        pulse.lib.pa_context_get_server_info.side_effect = server_info
        patch.setattr(native, "_server", monitor_stream.NativePulse._server.__get__(native))

        def connect(*_args: object) -> int:
            if moving and not move.is_set():
                return 0
            failed.set()
            return -1

        pulse.stream_connect_record.side_effect = connect
        pulse.stream_disconnect.side_effect = lambda _: (
            -1 if failed.is_set() and fault == "freed" else 0
        )
        patch.setattr(
            monitor_stream,
            "ProcReader",
            lambda **_: Mock(starttime=Mock(return_value=resolved.starttime)),
        )
        patch.setattr(native, "sample_clock", lambda: ("pulse", 100, 100100))
        patch.setattr(native, "read_chunk", lambda: b"")
        patch.setattr(
            native, "poll_events", lambda: [("changed", resolved.index)] if move.is_set() else []
        )

        def failed_lookup(context: int, index: int, receive: Any, data: Any) -> int:
            receive(context, None, -1, data)
            return 1

        def monitor_info(context: int, index: int, receive: Any, data: Any) -> int:
            if failed.is_set() and fault == "monitor":
                return failed_lookup(context, index, receive, data)
            name = b"sink-b.monitor" if move.is_set() else resolved.monitor.encode()
            receive(context, ctypes.pointer(monitor_stream._SinkInfo(monitor_name=name)), 0, data)
            receive(context, None, 1, data)
            return 1

        pulse.lib.pa_context_get_sink_info_by_index.side_effect = monitor_info
        for operation, faults in (
            (pulse.lib.pa_context_get_sink_input_info, {"removed", "stream-denied"}),
            (pulse.lib.pa_context_get_client_info, {"client"}),
        ):
            serve = operation.side_effect
            if fault in faults:
                operation.side_effect = lambda *args, serve=serve: (
                    failed_lookup(*args) if failed.is_set() else serve(*args)
                )

        source = MonitorStreamSource(native, "run1", target=target)
        patch.setattr(rig.backend, "source", Mock(return_value=source))
        if moving:
            open_capture(rig, capture_events)
            move.set()
            if fault == "removed":
                event = capture_events.finish("track-ended", "meeting.capture.track")
                assert (event["track"], event["state"], event["kind"]) == (
                    "far",
                    "ended",
                    "target-removed",
                )
                assert rig.state.state == "recording" and rig.mics[0].opened
            else:
                event = capture_events.finish("audio-failed", "meeting.capture.autopaused")
                assert event["reason"] == "audio-failed"
        else:
            assert rig.send("open", **admission(rig))[0]["type"] == "meeting.ack"
            event = capture_events.finish("open-failed", "error")
            assert event["request_type"] == "meeting.capture.open"
            assert event["code"] == ("target-gone" if fault == "removed" else "audio-failed")
        assert source.ended is (moving and fault == "removed")
        assert not rig.state.stopped
        if fault == "freed":
            assert native.context is None and lookups and None not in lookups
            pulse.context_unref.assert_called_once_with(1)
        if not moving or fault != "removed":
            capture_events.wait("closed")
            rig.poll()
            capture_events.wait("finalized")
            rig.poll()
            assert not any(
                item.get("kind") == "target-removed"
                for item in of_type(rig.events, "capture.track")
            )
        else:
            rig.send("stop")
            capture_events.wait("closed")
            rig.poll()
            capture_events.finish("finalized", "meeting.capture.finalized")


@pytest.mark.parametrize("kind", ["none", "sink_monitor"])
@pytest.mark.parametrize("existing", [False, True], ids=["first-context", "replace-context"])
def test_prepare_uses_server_generation_after_free_space_check(
    service: tuple[Rig, CheckDouble],
    capture_events: CaptureEvents,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    existing: bool,
) -> None:
    rig, _ = service
    far = {"kind": kind}
    server_observation(rig, COOKIE)
    rig.poll()
    backend = Backend()
    backend.context = (52, COOKIE + 1)
    in_free_space, release, retired = (threading.Event() for _ in range(3))

    def free_mb() -> int:
        in_free_space.set()
        assert release.wait(2)
        return 1024

    with monkeypatch.context() as patch:
        factory = Mock(return_value=backend)
        if existing:
            rig.backend.context = (51, COOKIE)
            request_prepare(rig, far)
            capture_events.finish("prepared", "meeting.capture.prepared")
            assert rig.state.observer.registry.snapshot().indexes == {42, 51}
            patch.setattr(rig.backend, "close", retired.set)
        patch.setattr(rig.state, "backend_factory", factory)
        patch.setattr(rig.state, "free_mb", free_mb)
        request_prepare(rig, far)
        try:
            assert in_free_space.wait(2)
            server_observation(rig, COOKIE + 1)
            rig.poll()
            assert rig.state.observer.registry.snapshot().indexes == {42}
            rig.state.observer.registry.set("observer", (43, COOKIE + 1))
        finally:
            release.set()
        capture_events.finish("prepared", "meeting.capture.prepared")
        assert rig.state.observer.registry.snapshot().indexes == {43, 52}
        factory.assert_called_once_with()
        assert backend.prepare_calls == [far] and not errors(rig.events)
        if existing:
            assert retired.wait(2)


def test_stop_consumes_late_first_open_in_same_poll_as_close(
    service: tuple[Rig, CheckDouble], capture_events: CaptureEvents
) -> None:
    rig, checks = service
    far = app_far(rig, checks)
    prepare_allowed(rig, checks, far, replace(TARGET, measured_at=rig.clock()))
    assert rig.send("open", **admission(rig))[0]["type"] == "meeting.ack"
    capture_events.wait("opened")
    assert rig.send("stop")[0]["type"] == "meeting.ack"
    capture_events.wait("closed")
    (closed,) = of_type(rig.poll(), "capture.closed")
    assert closed["tracks"] == ["mic", "far"] and not errors(rig.events)
    finished = capture_events.finish("finalized", "meeting.capture.stopped")
    assert finished["segments"] == {"mic": 0, "far": 0}
    assert rig.state.state == "stopped" and rig.state.close_reason == "stop"
    assert not rig.state.opening and rig.state.close_deadline is None
    rig.poll()
    assert len(of_type(rig.events, "capture.closed")) == 1
    assert not of_type(rig.events, "capture.started") and not errors(rig.events)


def test_other_program_ending_during_first_open_keeps_selected_pcm(
    service: tuple[Rig, CheckDouble],
    capture_events: CaptureEvents,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig, checks = service
    far = app_far(rig, checks)
    other = replace(ANCHOR.program, app_id="another-program", binary="another-program")
    card, _ = checked_card(
        rig, checks, "other", replace(ALLOWED, anchor=replace(ANCHOR, program=other))
    )
    assert card["active"] and card["call_id"] != far["call_id"]
    prepare_allowed(rig, checks, far, replace(TARGET, measured_at=rig.clock()))
    mic, far_source = rig.mic(), Source()
    mic_open = mic.open
    mic_entered, release, archived = (threading.Event() for _ in range(3))
    pcm = b"\2\0" * 1600
    far_source.chunks.append(pcm)

    def open_mic(device: str | None = None) -> None:
        mic_entered.set()
        assert release.wait(2)
        mic_open(device)

    with monkeypatch.context() as patch:
        patch.setattr(mic, "open", open_mic)
        patch.setattr(rig.state, "mic_factory", lambda: mic)
        patch.setattr(rig.backend, "source", Mock(return_value=far_source))
        assert rig.send("open", **admission(rig))[0]["type"] == "meeting.ack"
        try:
            assert mic_entered.wait(2)
            observation(rig, "call-ended", program=other)
            (ended,) = of_type(rig.poll(), "call.ended")
            assert ended["call_id"] == card["call_id"]
            assert rig.state.archive is not None
            put = rig.state.archive.put

            def archive_pcm(kind: str, payload: Any) -> bool:
                accepted = put(kind, payload)
                if kind == "pcm" and accepted and payload[:2] == ("far", pcm):
                    archived.set()
                return accepted

            patch.setattr(rig.state.archive, "put", archive_pcm)
        finally:
            release.set()
        started = capture_events.finish("opened", "meeting.capture.started")
        assert started["tracks"] == ["mic", "far"] and archived.wait(2)
        rig.send("stop")
        capture_events.wait("closed")
        rig.poll()
        finalized = capture_events.finish("finalized", "meeting.capture.finalized")
    assert finalized["segments"]["far"] == 1 and not errors(rig.events)


def test_server_reset_during_recording_keeps_mic_pcm(
    service: tuple[Rig, CheckDouble],
    capture_events: CaptureEvents,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig, checks = service
    far = app_far(rig, checks)
    rig.backend.context = (51, COOKIE)
    prepare_allowed(rig, checks, far, replace(TARGET, measured_at=rig.clock()))
    mic = rig.mic()
    first_pcm, next_pcm, retired = (threading.Event() for _ in range(3))
    before, after = b"\1\0" * 1600, b"\2\0" * 1600

    with monkeypatch.context() as patch:
        patch.setattr(rig.state, "mic_factory", lambda: mic)
        patch.setattr(rig.backend, "close", retired.set)
        assert rig.send("open", **admission(rig))[0]["type"] == "meeting.ack"
        assert rig.state.archive is not None
        put = rig.state.archive.put

        def archive_pcm(kind: str, payload: Any) -> bool:
            accepted = put(kind, payload)
            if kind == "pcm" and accepted and payload[0] == "mic":
                (first_pcm if payload[1] == before else next_pcm).set()
            return accepted

        patch.setattr(rig.state.archive, "put", archive_pcm)
        mic.chunks.append(before)
        capture_events.finish("opened", "meeting.capture.started")
        assert first_pcm.wait(2)
        server_observation(rig, COOKIE + 1)
        rig.poll()
        assert retired.wait(2)
        assert rig.state.backend is None
        assert rig.state.observer.registry.snapshot().indexes == {42}
        assert rig.state.state == "recording" and mic.opened
        assert not of_type(rig.events, "capture.autopaused")
        assert not of_type(rig.events, "capture.closed")
        mic.chunks.append(after)
        assert next_pcm.wait(2)
        rig.send("stop")
        capture_events.wait("closed")
        rig.poll()
        finalized = capture_events.finish("finalized", "meeting.capture.finalized")
    assert finalized["segments"]["mic"] == 1 and not errors(rig.events)
