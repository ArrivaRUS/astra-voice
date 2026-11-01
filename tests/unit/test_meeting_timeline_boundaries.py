"""D11: a terminal clock boundary preserves the preceding observed PCM axis."""

from __future__ import annotations

import pytest

from astra_voice.meetings import MeetingValidationError
from astra_voice.meetings.model import UINT_MAX
from astra_voice.meetings.segments import ClockAnchor, SegmentSidecar
from astra_voice.meetings.timeline import EpochTimeline, SegmentTimeline

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("view_type", [SegmentTimeline, EpochTimeline])
@pytest.mark.parametrize("origin", [100_000, UINT_MAX - 400_000])
def test_terminal_robust_fit_stays_inside_observed_times(
    view_type: type[SegmentTimeline] | type[EpochTimeline], origin: int
) -> None:
    segment = terminated_epoch("pulse")[0]
    segment.timeline = [[16000 * second, origin + 1000 * second] for second in range(301)]
    segment.timeline[-1][1] -= 50
    segment.discontinuity = True
    segment.close_reason = "clock_change"
    view = (
        SegmentTimeline.from_segment(segment)
        if view_type is SegmentTimeline
        else EpochTimeline.from_segments([segment])
    )
    lower, upper = segment.timeline[0][1], segment.timeline[-1][1]
    assert view.sample_to_monotonic(segment.sample_end) == upper
    # Include both sides of the clamp, not just knots or the exact endpoint.
    samples = [0, 1, 16000 * 150, *range(segment.sample_end - 1600, segment.sample_end + 1)]
    times = [view.sample_to_monotonic(sample) for sample in samples]
    assert all(lower <= mono <= upper for mono in times)
    assert times == sorted(times)
    assert view.sample_to_monotonic(16000 * 150) == origin + 150_000
    with pytest.raises(MeetingValidationError):
        view.sample_to_monotonic(segment.sample_end + 1)


def terminated_epoch(clock: str) -> list[SegmentSidecar]:
    # Shape of huvrk9v8: far has three 300s segments plus a 183s tail;
    # the transient monotonic mic epoch contains only 320 frames (20ms).
    lengths = [4_800_000] * 3 + [2_928_000] if clock == "pulse" else [320]
    start, origin = 0, 1_641_315_336
    result = []
    for index, frames in enumerate(lengths):
        end = start + frames
        samples = list(range(start, end, 16000)) + [end]
        result.append(
            SegmentSidecar(
                meeting_id="2026-11-01_143310-pqtp",
                capture_id="capture-boundary",
                meeting_generation=1,
                track="far" if clock == "pulse" else "mic",
                epoch_id=f"boundary-{clock}",
                index=index,
                clock=clock,
                clock_anchor=ClockAnchor(start // 16, origin + start // 16),
                opened_t_mono_ms=origin + start // 16,
                recorded_ms_at_open=start // 16,
                sample_start=start,
                sample_end=end,
                frames=frames,
                frames_at_checkpoint=frames,
                timeline=[[sample, origin + sample // 16] for sample in samples],
                closed=True,
                close_reason="segment",
            )
        )
        start = end
    result[-1].discontinuity = True
    result[-1].close_reason = "clock_change"
    return result


@pytest.mark.parametrize("clock", ["pulse", "monotonic"])
def test_terminal_clock_change_preserves_every_observed_segment(clock: str) -> None:
    segments = terminated_epoch(clock)
    epoch = EpochTimeline.from_segments(segments)
    original = [segment.to_dict() for segment in segments]
    for segment in segments:
        view = SegmentTimeline.from_segment(segment)
        for sample in (
            segment.sample_start,
            (segment.sample_start + segment.sample_end) // 2,
            segment.sample_end,
        ):
            expected = 1_641_315_336 + sample // 16
            assert abs(view.sample_to_monotonic(sample) - expected) <= 1
            assert abs(epoch.sample_to_monotonic(sample) - expected) <= 1
    assert [segment.to_dict() for segment in segments] == original
    assert epoch.mapping_method == ("robust-linear" if clock == "pulse" else "piecewise")
    for sample in (-1, segments[-1].sample_end + 1):
        with pytest.raises(MeetingValidationError):
            epoch.sample_to_monotonic(sample)


@pytest.mark.parametrize("clock", ["pulse", "monotonic"])
def test_single_segment_clock_change_keeps_observed_prefix(clock: str) -> None:
    segment = terminated_epoch(clock)[-1]
    view = SegmentTimeline.from_segment(segment)
    sample = (segment.sample_start + segment.sample_end) // 2
    assert abs(view.sample_to_monotonic(sample) - (1_641_315_336 + sample // 16)) <= 1
    assert view.mapping_method == ("robust-linear" if clock == "pulse" else "piecewise")


def test_terminal_boundary_does_not_license_unobserved_tail() -> None:
    segment = terminated_epoch("pulse")[-1]
    segment.timeline.pop()
    view = SegmentTimeline.from_segment(segment)
    epoch = EpochTimeline.from_segments([segment])
    last_observed = segment.timeline[-1][0]
    assert view.sample_to_monotonic(last_observed) == epoch.sample_to_monotonic(last_observed)
    for sample in (last_observed + 1, segment.sample_end):
        with pytest.raises(MeetingValidationError):
            view.sample_to_monotonic(sample)
        with pytest.raises(MeetingValidationError):
            epoch.sample_to_monotonic(sample)


@pytest.mark.parametrize("boundary", ["same-epoch", "new-epoch"])
def test_epoch_never_appends_samples_beyond_terminal_discontinuity(boundary: str) -> None:
    segments = terminated_epoch("pulse")
    previous = segments[-1]
    start, mono = previous.timeline[-1]
    following = SegmentSidecar(
        meeting_id=previous.meeting_id,
        capture_id=previous.capture_id,
        meeting_generation=previous.meeting_generation,
        track=previous.track,
        epoch_id=previous.epoch_id if boundary == "same-epoch" else "next-epoch",
        clock=previous.clock,
        index=previous.index + 1,
        sample_start=start,
        sample_end=start + 16000,
        frames=16000,
        timeline=[[start, mono], [start + 16000, mono + 1000]],
    )
    with pytest.raises(MeetingValidationError):
        EpochTimeline.from_segments([*segments, following])
