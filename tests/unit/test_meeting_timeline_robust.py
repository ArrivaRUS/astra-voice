"""Independent linear clocks with centered noise and sparse acquisition outliers."""

from __future__ import annotations

import random

import pytest

from astra_voice.meetings import MeetingValidationError
from astra_voice.meetings.segments import SegmentSidecar
from astra_voice.meetings.timeline import SegmentTimeline

pytestmark = pytest.mark.unit


def noisy_segment(*, sample_origin: int = 0, mono_origin: int = 100000) -> SegmentSidecar:
    rng = random.Random(20261101)
    points = []
    for second in range(301):
        # Uniform, independent, centered jitter; 5% isolated, signed outliers.
        error = rng.randint(-40, 40)
        if second % 19 == 0:
            error += 180 if second % 38 == 0 else -180
        points.append(
            [sample_origin + round(second * 16000 * 0.9998), mono_origin + second * 1000 + error]
        )
    frames = points[-1][0] - sample_origin
    return SegmentSidecar(
        meeting_id="2026-10-31_120000-ab12",
        epoch_id="independent-clock",
        frames=frames,
        sample_start=sample_origin,
        sample_end=sample_origin + frames,
        timeline=points,
    )


@pytest.mark.parametrize("origin", [0, 2**63])
def test_linear_minus_200ppm_with_jitter_and_outliers(origin: int) -> None:
    segment = noisy_segment(sample_origin=origin, mono_origin=origin + 100000)
    original = [point[:] for point in segment.timeline]
    view = SegmentTimeline.from_segment(segment)
    errors = []
    for tick in range(1201):
        elapsed_ms = tick * 250
        sample = origin + round(elapsed_ms * 16 * 0.9998)
        errors.append(view.sample_to_monotonic(sample) - (origin + 100000 + elapsed_ms))
    assert max(map(abs, errors)) <= 10
    assert view.mapping_method == "robust-linear"
    assert segment.timeline == original
    assert view.points == tuple(map(tuple, original))


def test_short_startup_is_explicit_piecewise_without_precision_claim() -> None:
    view = SegmentTimeline(0, 640000, False, ((0, 100), (320000, 20140), (640000, 40060)))
    assert view.mapping_method == "piecewise"
    assert view.sample_to_monotonic(320000) == 20140
    assert view.sample_to_monotonic(160000) == 10120


def test_balanced_two_mode_jitter_is_not_misclassified_as_outliers() -> None:
    segment = noisy_segment()
    segment.timeline = [
        [round(t * 16000 * 0.9998), 100000 + t * 1000 + (40 if t % 2 else -40)] for t in range(301)
    ]
    view = SegmentTimeline.from_segment(segment)
    assert (
        max(
            abs(view.sample_to_monotonic(s) - (100000 + t * 1000))
            for t, (s, _) in enumerate(segment.timeline)
        )
        <= 10
    )


def test_fitted_clock_preserves_observed_prefix_before_boundary() -> None:
    segment = noisy_segment()
    segment.timeline.pop()
    view = SegmentTimeline.from_segment(segment)
    with pytest.raises(MeetingValidationError):
        view.sample_to_monotonic(segment.sample_end)
    segment.discontinuity = True
    boundary_view = SegmentTimeline.from_segment(segment)
    assert boundary_view.mapping_method == "robust-linear"
    assert abs(boundary_view.sample_to_monotonic(segment.timeline[100][0]) - 200000) <= 10
    with pytest.raises(MeetingValidationError):
        boundary_view.sample_to_monotonic(segment.sample_end)


@pytest.mark.parametrize(
    "points",
    [((0, 100), (0, 200)), ((0, 200), (100, 100)), ((0, 100), (True, 200))],
)
def test_direct_snapshot_constructor_cannot_bypass_validation(
    points: tuple[tuple[int, int], ...],
) -> None:
    with pytest.raises(MeetingValidationError):
        SegmentTimeline(0, 100, False, points)


def adjacent_segments() -> list[SegmentSidecar]:
    rng = random.Random(6112026)
    points = [
        [round(t * 16000 * 0.9998), 100000 + t * 1000 + rng.randint(-40, 40)] for t in range(601)
    ]
    result = []
    for index in range(2):
        knots = [point[:] for point in points[index * 300 : (index + 1) * 300 + 1]]
        result.append(
            SegmentSidecar(
                meeting_id="2026-10-31_120000-ab12",
                epoch_id="continuous",
                index=index,
                frames=knots[-1][0] - knots[0][0],
                sample_start=knots[0][0],
                sample_end=knots[-1][0],
                timeline=knots,
            )
        )
    return result


def test_shared_epoch_fit_has_one_clock_at_rotation_and_large_origin() -> None:
    from astra_voice.meetings.timeline import EpochTimeline

    segments = adjacent_segments()
    for segment in segments:
        segment.sample_start += 2**63
        segment.sample_end += 2**63
        segment.timeline = [[s + 2**63, m + 2**63] for s, m in segment.timeline]
    epoch = EpochTimeline.from_segments(segments)
    assert epoch.mapping_method == "robust-linear"
    boundary = segments[0].sample_end
    assert boundary == segments[1].sample_start
    assert abs(epoch.sample_to_monotonic(boundary) - (2**63 + 400000)) <= 10
    stamps = [epoch.sample_to_monotonic(boundary + offset) for offset in range(-32, 33)]
    assert stamps == sorted(stamps)
    for second in range(601):
        sample = 2**63 + round(second * 16000 * 0.9998)
        assert abs(epoch.sample_to_monotonic(sample) - (2**63 + 100000 + second * 1000)) <= 10


@pytest.mark.parametrize(
    "field,value",
    [
        ("epoch_id", "other"),
        ("track", "far"),
        ("capture_id", "other"),
        ("meeting_generation", 1),
        ("meeting_id", "2026-10-31_120000-cd34"),
        ("clock", "monotonic"),
        ("index", 2),
    ],
)
def test_epoch_rejects_cross_identity(field: str, value: object) -> None:
    from astra_voice.meetings.timeline import EpochTimeline

    segments = adjacent_segments()
    setattr(segments[1], field, value)
    with pytest.raises(MeetingValidationError):
        EpochTimeline.from_segments(segments)


@pytest.mark.parametrize("gap", ["sample", "clock", "missing-boundary"])
def test_epoch_rejects_missing_or_inconsistent_shared_observation(gap: str) -> None:
    from astra_voice.meetings.timeline import EpochTimeline

    segments = adjacent_segments()
    if gap == "sample":
        segments[1].sample_start += 16000
        segments[1].sample_end += 16000
        segments[1].timeline = [[s + 16000, m] for s, m in segments[1].timeline]
    elif gap == "clock":
        segments[1].timeline[0][1] += 20
    else:
        segments[0].timeline.pop()
    with pytest.raises(MeetingValidationError):
        EpochTimeline.from_segments(segments)
