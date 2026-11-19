"""M8 step 3 timing, offer continuity and the metadata boundary."""

from __future__ import annotations

import logging
from dataclasses import replace
from unittest.mock import Mock

import pytest

from astra_voice.meetings.apps import AppClass, AppDefinition
from astra_voice.meetings.detector_rule import CallEnded, Candidate, MeetingDetectorRule
from astra_voice.worker import meeting_detector
from astra_voice.worker.meeting_detector import (
    Direction,
    EventKind,
    MeetingDetectorSource,
    MetadataBackend,
    NormalizedEvent,
    PictureTiming,
    ProgramKey,
    RawClient,
    RawStream,
    StreamKey,
)

pytestmark = pytest.mark.unit

DION = ProgramKey("dion", "dion")
BROWSER = ProgramKey("browser", "firefox")
MIC = Direction.MICROPHONE
OUT = Direction.OUTPUT


class Clock:
    now = 0.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def rule(clock: Clock) -> MeetingDetectorRule:
    return MeetingDetectorRule(clock=clock)


def stream(
    direction: Direction,
    at: float = 0.0,
    *,
    program: ProgramKey | None = DION,
    incarnation: int,
) -> NormalizedEvent:
    return NormalizedEvent(
        EventKind.UPSERT, at, StreamKey(1, direction, 7, incarnation, None), program, False, False
    )


def tick(rule: MeetingDetectorRule, clock: Clock, at: float) -> tuple[Candidate | CallEnded, ...]:
    clock.now = at
    return rule.update()


def test_on_boundary_starts_when_both_directions_are_active(
    rule: MeetingDetectorRule, clock: Clock
) -> None:
    assert rule.update((stream(OUT, 120.0, incarnation=1),)) == ()
    assert tick(rule, clock, 130.0) == ()
    assert rule.update((stream(MIC, 130.125, incarnation=2),)) == ()
    assert tick(rule, clock, 134.124) == ()
    assert tick(rule, clock, 136.125) == (Candidate(DION, False),)
    assert tick(rule, clock, 137.0) == ()


def test_on_counts_from_first_applied_picture_even_if_pending(
    rule: MeetingDetectorRule,
) -> None:
    events = (stream(MIC, incarnation=1), stream(OUT, incarnation=2))
    assert rule.update(events, timing=PictureTiming(0.0, 0.0, False)) == ()
    assert rule.update(timing=PictureTiming(3.999, 0.0, False)) == ()
    decisions = rule.update(timing=PictureTiming(5.0, 0.0, False))
    decisions += rule.update(timing=PictureTiming(5.25, 0.0, False))
    assert decisions == (Candidate(DION, False),)
    assert rule.candidate(DION) == Candidate(DION, False)

    assert rule.update(timing=PictureTiming(7.0, 0.0, True)) == ()
    assert rule.candidate(DION) == Candidate(DION, False)
    assert rule.update(timing=PictureTiming(8.0, 8.0, False)) == ()
    assert rule.candidate(DION) == Candidate(DION, False)
    assert rule.mark_offered(DION)


def test_off_boundary_starts_on_removal_of_last_microphone(
    rule: MeetingDetectorRule, clock: Clock
) -> None:
    mic = stream(MIC, incarnation=1)
    other_mic = stream(MIC, incarnation=2)
    out = stream(OUT, incarnation=3)
    rule.update((mic, other_mic, out))
    assert tick(rule, clock, 6.0) == (Candidate(DION, False),)
    assert rule.mark_offered(DION)

    rule.update((replace(mic, kind=EventKind.REMOVE, at=6.0),))
    assert tick(rule, clock, 16.0) == ()
    rule.update((replace(other_mic, kind=EventKind.REMOVE, at=17.125),))
    assert tick(rule, clock, 26.124) == ()
    assert tick(rule, clock, 28.125) == (CallEnded(DION),)
    assert tick(rule, clock, 29.0) == ()


@pytest.mark.parametrize(
    "direction, corked, muted, running",
    [
        pytest.param(OUT, False, True, None, id="muted-output"),
        pytest.param(MIC, True, True, None, id="corked-mic"),
        pytest.param(OUT, True, False, None, id="corked-output"),
        pytest.param(MIC, False, True, False, id="silent-mic"),
        pytest.param(OUT, False, False, False, id="silent-output"),
        pytest.param(MIC, None, False, None, id="unknown-cork-mic"),
        pytest.param(OUT, None, False, None, id="unknown-cork-output"),
    ],
)
def test_non_active_stream_breaks_on_interval(
    rule: MeetingDetectorRule,
    clock: Clock,
    direction: Direction,
    corked: bool | None,
    muted: bool | None,
    running: bool | None,
) -> None:
    selected = stream(direction, incarnation=1)
    other = stream(OUT if direction is MIC else MIC, incarnation=2)
    rule.update((selected, other))
    assert (
        rule.update((replace(selected, at=4.0, corked=corked, muted=muted, running=running),)) == ()
    )
    assert tick(rule, clock, 14.0) == ()
    assert rule.update((replace(selected, at=15.0, running=True),)) == ()
    assert tick(rule, clock, 18.999) == ()
    assert tick(rule, clock, 21.0) == (Candidate(DION, False),)


@pytest.mark.parametrize("muted", [True, None], ids=["muted", "unknown-mute"])
def test_call_can_start_with_muted_microphone(
    rule: MeetingDetectorRule, clock: Clock, muted: bool | None
) -> None:
    mic = replace(stream(MIC, incarnation=1), muted=muted)
    out = replace(stream(OUT, incarnation=2), muted=None)
    assert rule.update((mic, out)) == ()
    assert tick(rule, clock, 3.999) == ()
    assert tick(rule, clock, 6.0) == (Candidate(DION, False),)
    assert rule.mark_offered(DION)
    assert rule.update((replace(mic, at=6.0, muted=False),)) == ()
    assert rule.update((replace(mic, at=7.0, muted=True),)) == ()
    assert tick(rule, clock, 20.0) == ()
    assert rule.candidate(DION) == Candidate(DION, True)


def test_four_second_catalogued_episode_is_not_a_call(
    rule: MeetingDetectorRule, clock: Clock
) -> None:
    mic = stream(MIC, incarnation=1)
    out = stream(OUT, incarnation=2)
    rule.update((mic, out))
    assert rule.update((replace(mic, kind=EventKind.REMOVE, at=3.999),)) == ()
    assert tick(rule, clock, 20.0) == ()
    assert not rule.mark_offered(DION)


def test_change_of_stream_within_one_batch_keeps_on_interval(
    rule: MeetingDetectorRule, clock: Clock
) -> None:
    mic = stream(MIC, incarnation=1)
    rule.update((mic, stream(OUT, incarnation=2)))
    assert (
        rule.update(
            (
                replace(mic, kind=EventKind.REMOVE, at=4.0),
                stream(MIC, 4.0, incarnation=3),
            )
        )
        == ()
    )
    assert tick(rule, clock, 6.0) == (Candidate(DION, False),)


@pytest.mark.parametrize("muted", [False, True], ids=["live-mic", "muted-corked-mic"])
def test_lost_output_does_not_end_call_with_microphone_present(
    rule: MeetingDetectorRule, clock: Clock, muted: bool
) -> None:
    mic = stream(MIC, incarnation=1)
    out = stream(OUT, incarnation=2)
    rule.update((mic, out))
    assert tick(rule, clock, 6.0) == (Candidate(DION, False),)
    assert rule.mark_offered(DION)
    assert rule.candidate(DION) == Candidate(DION, True)
    assert not rule.mark_offered(DION)

    assert (
        rule.update(
            (
                replace(out, kind=EventKind.REMOVE, at=6.0),
                replace(mic, at=6.0, muted=muted, corked=muted, running=not muted),
            )
        )
        == ()
    )
    assert tick(rule, clock, 25.0) == ()
    assert rule.update((stream(MIC, 26.0, incarnation=1), stream(OUT, 26.0, incarnation=3))) == ()
    assert tick(rule, clock, 32.0) == (Candidate(DION, True),)


@pytest.mark.parametrize("offered", [False, True], ids=["pending-offer", "already-offered"])
@pytest.mark.parametrize("disruption", ["reset", "unavailable", "both"])
def test_reset_and_reconnect_keep_offer_continuity(
    rule: MeetingDetectorRule, clock: Clock, offered: bool, disruption: str
) -> None:
    mic = stream(MIC, incarnation=1)
    out = stream(OUT, incarnation=2)
    rule.update((mic, out))
    assert tick(rule, clock, 6.0) == (Candidate(DION, False),)
    if offered:
        assert rule.mark_offered(DION)

    at = 6.0
    if disruption != "reset":
        unavailable: tuple[NormalizedEvent, ...] = (NormalizedEvent(EventKind.UNAVAILABLE, at),)
        if disruption == "both":
            unavailable = (NormalizedEvent(EventKind.RESET, at), *unavailable)
        assert rule.update(unavailable) == ()
        assert rule.candidate(DION) is None
        assert tick(rule, clock, 60.0) == ()
        at = 61.0

    new_mic = replace(mic, at=at, key=StreamKey(2, MIC, 7, 3, 123))
    new_out = replace(out, at=at, key=StreamKey(2, OUT, 7, 4, 456))
    assert rule.update((NormalizedEvent(EventKind.RESET, at), new_mic, new_out)) == ()
    assert tick(rule, clock, at + 3.999) == ()
    assert tick(rule, clock, at + 6.0) == (Candidate(DION, offered),)
    assert tick(rule, clock, at + 7.0) == ()


def test_pending_off_interval_restarts_after_unavailable_until_empty_snapshot(
    rule: MeetingDetectorRule, clock: Clock
) -> None:
    mic = stream(MIC, incarnation=1)
    rule.update((mic, stream(OUT, incarnation=2)))
    assert tick(rule, clock, 6.0) == (Candidate(DION, False),)
    assert rule.mark_offered(DION)
    rule.update((replace(mic, kind=EventKind.REMOVE, at=6.0),))
    assert rule.update((NormalizedEvent(EventKind.UNAVAILABLE, 7.0),)) == ()
    assert tick(rule, clock, 30.0) == ()
    assert rule.update((NormalizedEvent(EventKind.RESET, 31.0),)) == ()
    assert tick(rule, clock, 39.999) == ()
    assert tick(rule, clock, 42.0) == (CallEnded(DION),)


def test_unavailable_discards_off_deadline_at_revocation(
    rule: MeetingDetectorRule, clock: Clock
) -> None:
    mic = stream(MIC, incarnation=1)
    rule.update((mic, stream(OUT, incarnation=2)))
    assert tick(rule, clock, 6.0) == (Candidate(DION, False),)
    assert rule.mark_offered(DION)
    rule.update((replace(mic, kind=EventKind.REMOVE, at=6.0),))
    assert rule._programs[DION].off_deadline is not None
    assert rule.update((NormalizedEvent(EventKind.UNAVAILABLE, 14.0),)) == ()
    # Check revocation itself before a fresh RESET can also clear the deadline.
    assert rule._programs[DION].off_deadline is None
    assert rule._programs[DION].offered


def test_empty_reset_removes_old_streams_and_ends_call_once(
    rule: MeetingDetectorRule, clock: Clock
) -> None:
    rule.update((stream(MIC, incarnation=1), stream(OUT, incarnation=2)))
    assert tick(rule, clock, 6.0) == (Candidate(DION, False),)
    assert rule.mark_offered(DION)
    assert rule.update((NormalizedEvent(EventKind.RESET, 6.0),)) == ()
    assert tick(rule, clock, 11.0) == ()
    assert tick(rule, clock, 14.999) == ()
    assert tick(rule, clock, 17.0) == (CallEnded(DION),)
    assert tick(rule, clock, 18.0) == ()


def test_unknown_program_does_not_qualify_and_can_replace_a_catalogued_stream(
    rule: MeetingDetectorRule, clock: Clock
) -> None:
    mic = stream(MIC, program=None, incarnation=1)
    out = stream(OUT, program=None, incarnation=2)
    rule.update((mic, out))
    assert tick(rule, clock, 15.0) == ()
    rule.update((replace(mic, program=DION, at=16.0), replace(out, program=DION, at=16.0)))
    rule.update((replace(mic, at=20.0),))
    assert tick(rule, clock, 35.0) == ()


@pytest.mark.parametrize(
    "first", [DION, ProgramKey("browser", "chromium")], ids=["classes", "bins"]
)
def test_programs_have_independent_timers_and_offer_status(
    rule: MeetingDetectorRule, clock: Clock, first: ProgramKey
) -> None:
    first_mic = stream(MIC, program=first, incarnation=1)
    browser_out = stream(OUT, program=BROWSER, incarnation=2)
    rule.update((first_mic, browser_out))
    assert tick(rule, clock, 10.0) == ()
    rule.update((stream(OUT, 11.0, program=first, incarnation=3),))
    rule.update((stream(MIC, 14.0, program=BROWSER, incarnation=4),))
    assert tick(rule, clock, 17.0) == (Candidate(first, False),)
    assert rule.mark_offered(first)
    assert tick(rule, clock, 20.0) == (Candidate(BROWSER, False),)
    rule.update((replace(first_mic, kind=EventKind.REMOVE, at=21.0),))
    assert tick(rule, clock, 32.0) == (CallEnded(first),)
    assert rule.candidate(BROWSER) == Candidate(BROWSER, False)


@pytest.mark.parametrize("close_newer", [False, True], ids=["close-older", "close-newer"])
def test_browser_keeps_on_interval_and_offer_across_two_outputs(
    rule: MeetingDetectorRule, clock: Clock, close_newer: bool
) -> None:
    mic = stream(MIC, program=BROWSER, incarnation=1)
    first = stream(OUT, program=BROWSER, incarnation=2)
    other = stream(OUT, 4.0, program=BROWSER, incarnation=3)
    rule.update((mic, first))
    assert rule.update((other,)) == ()
    closed, remaining = (other, first) if close_newer else (first, other)
    assert rule.update((replace(closed, kind=EventKind.REMOVE, at=4.5),)) == ()
    assert tick(rule, clock, 6.0) == (Candidate(BROWSER, False),)
    assert rule.mark_offered(BROWSER)

    new_output = stream(OUT, 7.0, program=BROWSER, incarnation=4)
    assert rule.update((new_output,)) == ()
    closed = new_output if close_newer else remaining
    assert rule.update((replace(closed, kind=EventKind.REMOVE, at=8.0),)) == ()
    assert tick(rule, clock, 20.0) == ()
    assert rule.candidate(BROWSER) == Candidate(BROWSER, True)


def test_microphone_return_before_off_deadline_keeps_offer_continuity(
    rule: MeetingDetectorRule, clock: Clock
) -> None:
    mic = stream(MIC, incarnation=1)
    rule.update((mic, stream(OUT, incarnation=2)))
    assert tick(rule, clock, 6.0) == (Candidate(DION, False),)
    assert rule.mark_offered(DION)
    assert rule.update((replace(mic, kind=EventKind.REMOVE, at=6.0),)) == ()
    assert rule.update((stream(MIC, 14.999, incarnation=3),)) == ()
    assert tick(rule, clock, 16.0) == ()
    assert tick(rule, clock, 20.999) == (Candidate(DION, True),)


def test_microphone_return_after_confirmed_end_starts_a_new_call(
    rule: MeetingDetectorRule, clock: Clock
) -> None:
    mic = stream(MIC, incarnation=1)
    rule.update((mic, stream(OUT, incarnation=2)))
    assert tick(rule, clock, 6.0) == (Candidate(DION, False),)
    assert rule.mark_offered(DION)
    assert rule.update((replace(mic, kind=EventKind.REMOVE, at=6.0),)) == ()
    assert tick(rule, clock, 17.0) == (CallEnded(DION),)
    assert rule.update((stream(MIC, 18.0, incarnation=3),)) == ()
    assert not rule.mark_offered(DION)
    assert tick(rule, clock, 21.999) == ()
    assert tick(rule, clock, 24.0) == (Candidate(DION, False),)


def test_source_excludes_own_context_even_when_it_claims_a_catalogued_program(
    rule: MeetingDetectorRule, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    dion = AppDefinition(DION.app_id, "DION", (DION.binary,), (), AppClass.DION)
    monkeypatch.setattr(meeting_detector, "load_apps", lambda: (dion,))
    backend = Mock(spec=MetadataBackend, own_index=1)
    backend.snapshot.return_value = (
        (
            RawClient(1, {"application.process.id": 111, "application.process.binary": "dion"}),
            RawClient(2, {"application.process.id": 222, "application.process.binary": "dion"}),
        ),
        (
            RawStream(71, 1, MIC, corked=False, muted=False),
            RawStream(72, 2, OUT, corked=False, muted=False),
        ),
    )
    backend.take_overflow.return_value = False
    backend.has_pending_notices.return_value = False
    source = MeetingDetectorSource(factory=lambda: backend, clock=clock)
    events = source.start()
    assert [event.program for event in events] == [None, DION]
    assert rule.update(events, timing=source.picture_timing) == ()
    assert tick(rule, clock, 15.0) == ()


def test_decisions_state_repr_and_logs_are_anonymized(
    rule: MeetingDetectorRule, clock: Clock, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    mic = replace(
        stream(MIC, incarnation=1),
        key=StreamKey(8901234, MIC, 9012345, 1, 9123456),
    )
    rule.update((mic, stream(OUT, incarnation=2)))
    decisions = tick(rule, clock, 6.0)
    assert decisions == (Candidate(DION, False),)
    assert rule.mark_offered(DION)
    surfaces = (
        repr(rule),
        repr(vars(rule)),
        repr(decisions),
        repr(rule.candidate(DION)),
        caplog.text,
    )
    for surface in surfaces:
        assert all(
            marker not in surface for marker in ("StreamKey", "8901234", "9012345", "9123456")
        )
    assert not caplog.records
