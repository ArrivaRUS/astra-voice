"""W2 partial notices from the observer's playback picture."""

from __future__ import annotations

import threading
from dataclasses import replace
from typing import Any

import pytest
from test_meeting_capture import Rig
from test_meeting_m8_call_service import ALLOWED, ANCHOR, CheckDouble, checked_card, of_type
from test_meeting_m8_call_service import service as service
from test_meeting_m8_start import (
    CaptureEvents,
    admission,
    app_far,
    assert_far_track,
    open_capture,
    pinned_outputs,
    prepare_allowed,
    prepared,
    request_prepare,
)
from test_meeting_m8_start import capture_events as capture_events

from astra_voice.meetings.apps import AppClass
from astra_voice.worker import ipc
from astra_voice.worker.meeting_detector import (
    Direction,
    EventKind,
    NormalizedEvent,
    PictureTiming,
    ProgramKey,
    StreamKey,
)
from astra_voice.worker.meeting_identity_check import PinnedStream

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("filesystem_euid")]


def output(
    rig: Rig, stream: PinnedStream, *, corked: bool = False, program: ProgramKey = ANCHOR.program
) -> NormalizedEvent:
    return NormalizedEvent(
        EventKind.UPSERT,
        rig.clock(),
        StreamKey(
            1,
            Direction.OUTPUT,
            stream.sink_input,
            stream.sink_input,
            stream.stream_serial,
        ),
        program,
        corked=corked,
    )


def picture(
    rig: Rig,
    *outputs: NormalizedEvent,
    kind: EventKind = EventKind.RESET,
    confirmed: bool = True,
    age: float = 0,
) -> None:
    at = rig.clock() - age
    observer = rig.state.observer
    events = (NormalizedEvent(kind, at), *(replace(event, at=at) for event in outputs))
    with observer.rule_lock:
        observer.rule.update(events, timing=PictureTiming(at, None, confirmed))


def decode_tracks(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    role = "meeting-capture"
    reader = ipc.FrameReader(role=role)
    data = b"".join(ipc.encode(event, role=role) for event in of_type(events, "capture.track"))
    return [ipc.decode(body, role=role) for body in reader.feed_frames(data)]


@pytest.mark.parametrize("reused_index", [False, True], ids=["new-index", "new-serial"])
def test_dion_marks_unpinned_far_once_on_alive_tick(
    service: tuple[Rig, CheckDouble], capture_events: CaptureEvents, reused_index: bool
) -> None:
    rig, checks = service
    target = pinned_outputs(1, rig.clock())
    pinned, new, later = pinned_outputs(3, rig.clock()).streams
    far = app_far(rig, checks)
    picture(rig, output(rig, pinned, corked=True))
    prepare_allowed(rig, checks, far, target)
    open_capture(rig, capture_events)
    assert not decode_tracks(rig.inbox)
    assert rig.state._prepared_anchor is None

    def audio_calls() -> tuple[object, ...]:
        return (
            len(checks.requests),
            len(rig.backend.prepare_calls),
            tuple(rig.backend.sources),
            tuple(rig.backend.source_targets),
        )

    before, threads = audio_calls(), set(threading.enumerate())
    rig.clock.now += 1
    sounding = output(rig, pinned)
    picture(rig, sounding)
    tick = rig.poll()
    assert of_type(tick, "capture.alive") and not decode_tracks(tick)

    rig.clock.now += 0.5
    if reused_index:
        new = replace(new, sink_input=pinned.sink_input)
    # The observer supplies cork/mute for Pulse output audibility.
    unpinned = replace(output(rig, new), running=False)
    rows = (unpinned,) if reused_index else (sounding, unpinned)
    picture(rig, *rows)
    assert not decode_tracks(rig.poll())
    assert not rig.state._far_incomplete

    rig.clock.now += 0.5
    tick = rig.poll()
    assert of_type(tick, "capture.alive")
    (notice,) = decode_tracks(tick)
    assert (notice["track"], notice["state"], notice["kind"]) == (
        "far",
        "live",
        "partial",
    )
    assert set(notice) == {
        "type",
        "meeting_id",
        "meeting_generation",
        "capture_id",
        "state_rev",
        "track",
        "state",
        "kind",
        "at_recorded_ms",
    }

    rig.send("keepalive", seq=1, indicators_visible=True, **admission(rig))
    rig.clock.now += 1
    assert not decode_tracks(rig.poll())
    rig.clock.now += 1
    picture(rig, kind=EventKind.UNAVAILABLE)
    assert not decode_tracks(rig.poll())
    assert rig.state._far_incomplete
    rig.clock.now += 1
    picture(rig, *rows, output(rig, later))
    assert not decode_tracks(rig.poll())
    assert rig.state.capture_target is target
    assert audio_calls() == before
    assert set(threading.enumerate()) <= threads


@pytest.mark.parametrize(
    "kind",
    [
        "other-program",
        "corked",
        "unknown-cork",
        "muted",
        "without-serial",
        "microphone",
    ],
)
def test_far_tick_ignores_rows_outside_audible_call_outputs(
    service: tuple[Rig, CheckDouble], capture_events: CaptureEvents, kind: str
) -> None:
    rig, checks = service
    target = pinned_outputs(1, rig.clock())
    pinned, other = pinned_outputs(2, rig.clock()).streams
    prepare_allowed(rig, checks, app_far(rig, checks), target)
    open_capture(rig, capture_events)

    rig.clock.now += 1
    event = output(rig, other)
    key = event.key
    assert key is not None
    variants = {
        "other-program": replace(event, program=ProgramKey("player", "player")),
        "corked": replace(event, corked=True),
        "unknown-cork": replace(event, corked=None),
        "muted": replace(event, muted=True),
        "without-serial": replace(event, key=replace(key, serial=None)),
        "microphone": replace(event, key=replace(key, direction=Direction.MICROPHONE)),
    }
    picture(rig, output(rig, pinned), variants[kind])
    assert not decode_tracks(rig.poll())
    assert not rig.state._far_incomplete


@pytest.mark.parametrize(
    "case", ["unavailable", "unconfirmed", "stale", "wrong-cookie", "automation-off"]
)
def test_far_tick_ignores_unusable_observer_picture(
    service: tuple[Rig, CheckDouble], capture_events: CaptureEvents, case: str
) -> None:
    rig, checks = service
    target = pinned_outputs(1, rig.clock())
    pinned, new = pinned_outputs(2, rig.clock()).streams
    prepare_allowed(rig, checks, app_far(rig, checks), target)
    open_capture(rig, capture_events)

    rig.clock.now += 1
    picture(
        rig,
        output(rig, pinned),
        output(rig, new),
        kind=EventKind.UNAVAILABLE if case == "unavailable" else EventKind.RESET,
        confirmed=case != "unconfirmed",
        age=2.001 if case == "stale" else 0,
    )
    observer = rig.state.observer
    if case == "wrong-cookie":
        observer.registry.set("observer", (42, target.server_cookie + 1))
    if case == "automation-off":
        observer.automation_off.set()
    assert not decode_tracks(rig.poll())
    assert not rig.state._far_incomplete

    rig.clock.now += 1
    observer.automation_off.clear()
    observer.registry.set("observer", (42, target.server_cookie))
    picture(rig, output(rig, pinned), output(rig, new), age=2)
    assert_far_track(rig.poll(), "live", "partial")


def test_card_can_start_far_without_an_observer_picture(
    service: tuple[Rig, CheckDouble], capture_events: CaptureEvents
) -> None:
    rig, checks = service
    observer = rig.state.observer
    observer.registry.set("observer", None)
    target = pinned_outputs(1, rig.clock())
    rig.backend.context = (51, target.server_cookie)
    prepare_allowed(rig, checks, app_far(rig, checks), target)
    assert open_capture(rig, capture_events)["tracks"] == ["mic", "far"]
    assert observer._thread is None
    rig.clock.now += 1
    assert not decode_tracks(rig.inbox + rig.poll())
    assert not rig.state._far_incomplete


def test_browser_compares_picture_at_first_open(
    service: tuple[Rig, CheckDouble], capture_events: CaptureEvents
) -> None:
    rig, checks = service
    target = pinned_outputs(2, rig.clock())
    program = ProgramKey("browser", "chromium")
    decision = replace(
        ALLOWED, app_class=AppClass.BROWSER, anchor=replace(ANCHOR, program=program), target=target
    )
    card, _ = checked_card(rig, checks, "card", decision)
    assert card["app_class"] == "browser" and card["variant1"]
    run = checks.plan(decision)
    request_prepare(rig, {"kind": "app_stream", "call_id": card["call_id"]})
    run.complete()
    prepared(rig, rig.poll())
    picture(rig, *(output(rig, stream, program=program) for stream in pinned_outputs(3).streams))
    open_capture(rig, capture_events)
    assert not of_type(rig.inbox, "capture.alive")
    assert_far_track(rig.inbox, "live", "partial")


def test_far_skips_paused_tick_and_compares_picture_on_resume(
    service: tuple[Rig, CheckDouble],
    capture_events: CaptureEvents,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig, checks = service
    target = pinned_outputs(1, rig.clock())
    pinned, new = pinned_outputs(2, rig.clock()).streams
    prepare_allowed(rig, checks, app_far(rig, checks), target)
    open_capture(rig, capture_events)

    source = rig.backend.sources[0]
    shutdown = source.shutdown
    close_allowed = threading.Event()

    def shutdown_after_tick() -> None:
        assert close_allowed.wait(2)
        shutdown()

    with monkeypatch.context() as patch:
        patch.setattr(source, "shutdown", shutdown_after_tick)
        try:
            rig.clock.now += 0.5
            rig.send("pause", reason="user")
            assert rig.state.state == "paused" and rig.state.track_states["far"] == "live"
            rig.clock.now += 0.5
            picture(rig, output(rig, pinned), output(rig, new))
            tick = rig.poll()
            assert of_type(tick, "capture.alive") and not decode_tracks(tick)
            assert not rig.state._far_incomplete
        finally:
            close_allowed.set()
        capture_events.wait("closed")
        rig.poll()
        capture_events.finish("finalized", "meeting.capture.finalized")

    assert rig.send("resume", **admission(rig))[0]["type"] == "meeting.ack"
    capture_events.finish("opened", "meeting.capture.started")
    assert len(of_type(rig.inbox, "capture.alive")) == 1
    assert_far_track(rig.inbox, "live", "partial")


def test_far_tick_uses_the_program_pinned_on_prepare(
    service: tuple[Rig, CheckDouble],
    capture_events: CaptureEvents,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig, checks = service
    target = pinned_outputs(1, rig.clock())
    pinned, new = pinned_outputs(2, rig.clock()).streams
    prepare_allowed(rig, checks, app_far(rig, checks), target)
    open_capture(rig, capture_events)

    def unexpected_anchor(_call_id: str) -> None:
        pytest.fail("the active capture looked up its call anchor")

    rig.clock.now += 1
    picture(rig, output(rig, pinned), output(rig, new))
    with monkeypatch.context() as patch:
        patch.setattr(rig.state.calls, "anchor_for", unexpected_anchor)
        assert_far_track(rig.poll(), "live", "partial")


def test_far_tick_skips_the_ended_track(
    service: tuple[Rig, CheckDouble], capture_events: CaptureEvents
) -> None:
    rig, checks = service
    target = pinned_outputs(1, rig.clock())
    new = pinned_outputs(2, rig.clock()).streams[1]
    prepare_allowed(rig, checks, app_far(rig, checks), target)
    open_capture(rig, capture_events)
    source = rig.backend.sources[0]
    source.ended = True
    source.chunks.append(None)
    capture_events.wait("track-ended")
    assert_far_track(rig.poll(), "ended", "target-removed")

    rig.clock.now += 1
    picture(rig, output(rig, new))
    assert not decode_tracks(rig.poll())
    assert rig.state.state == "recording"
    assert not rig.state._far_incomplete


@pytest.mark.parametrize("first", ["loss", "unpinned"])
def test_far_tick_shares_incomplete_flag_with_reader_losses(
    service: tuple[Rig, CheckDouble], capture_events: CaptureEvents, first: str
) -> None:
    rig, checks = service
    target = pinned_outputs(2, rig.clock())
    lost, survivor, new = pinned_outputs(3, rig.clock()).streams
    prepare_allowed(rig, checks, app_far(rig, checks), target)
    open_capture(rig, capture_events)

    def lose_output() -> list[dict[str, Any]]:
        rig.backend.sources[0].losses.put((lost,))
        capture_events.wait("far-partial")
        return rig.poll()

    if first == "loss":
        assert_far_track(lose_output(), "live", "partial")
    rig.clock.now += 1
    picture(rig, output(rig, survivor), output(rig, new))
    tick = rig.poll()
    if first == "loss":
        assert not decode_tracks(tick)
    else:
        assert_far_track(tick, "live", "partial")
        assert rig.state.capture_target is target
        rig.clock.now += 1
        assert_far_track(lose_output(), "live", "partial")
    current = rig.state.capture_target
    assert current is not None and current.streams == (survivor,)
