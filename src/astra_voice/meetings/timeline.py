"""Pure sample-clock mapping within a validated, continuous capture boundary."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from statistics import median, quantiles
from typing import Self

from astra_voice.meetings import MeetingValidationError
from astra_voice.meetings.model import UINT_MAX
from astra_voice.meetings.segments import SegmentSidecar


@dataclass(frozen=True)
class SegmentTimeline:
    """Immutable per-segment clock estimate; raw observations remain in points.

    A >=60s, >=61-observation continuous snapshot gets a robust affine clock.
    This assumes approximately constant rate, centered acquisition noise and
    sparse outliers, not an arbitrary bounded/adversarial error guarantee.
    Short startup snapshots retain exact piecewise interpolation, explicitly
    exposed by mapping_method. Construct once and reuse after loading.
    """

    sample_start: int
    sample_end: int
    discontinuity: bool
    points: tuple[tuple[int, int], ...]
    _fit: tuple[float, float] | None = field(default=None, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if (
            type(self.sample_start) is not int
            or type(self.sample_end) is not int
            or not 0 <= self.sample_start <= self.sample_end <= UINT_MAX
            or self.sample_end - self.sample_start > 4_800_000
            or type(self.discontinuity) is not bool
            or not isinstance(self.points, tuple)
            or len(self.points) > 302
        ):
            raise MeetingValidationError("Недопустимый диапазон отсчётов")
        for index, point in enumerate(self.points):
            if (
                not isinstance(point, tuple)
                or len(point) != 2
                or any(type(value) is not int or not 0 <= value <= UINT_MAX for value in point)
                or not self.sample_start <= point[0] <= self.sample_end
                or (
                    index
                    and (
                        point[0] <= self.points[index - 1][0]
                        or point[1] <= self.points[index - 1][1]
                    )
                )
            ):
                raise MeetingValidationError("Недопустимые опорные точки часов")
        if (
            not self.discontinuity
            and len(self.points) >= 61
            and self.points[-1][1] - self.points[0][1] >= 60_000
        ):
            object.__setattr__(self, "_fit", _robust_fit(self.points))

    @property
    def mapping_method(self) -> str:
        """Estimator eligibility, not a bound on timestamp uncertainty."""
        return "robust-linear" if self._fit is not None else "piecewise"

    @classmethod
    def from_segment(cls, segment: SegmentSidecar) -> Self:
        segment.to_dict()
        return cls(
            segment.sample_start,
            segment.sample_end,
            segment.discontinuity,
            tuple((sample, mono) for sample, mono in segment.timeline),
        )

    def sample_to_monotonic(self, sample: int) -> int:
        """Estimate only inside observed knots; never extrapolate over gaps."""
        if (
            type(sample) is not int
            or self.discontinuity
            or not self.sample_start <= sample <= self.sample_end
            or not self.points
            or not self.points[0][0] <= sample <= self.points[-1][0]
        ):
            raise MeetingValidationError("Нельзя отобразить отсчёт через разрыв")
        return _map(self.points, self._fit, sample)


@dataclass(frozen=True, init=False)
class EpochTimeline:
    """One immutable clock for contiguous segments of one capture/track/epoch.

    Consumers with several segments (M4/M7) must reuse this shared estimate:
    independently fitted segment clocks can disagree at their shared sample.
    Construction uses O(total knots) storage, bounded passes and <=302 raw
    observations per segment. It never joins different identities or gaps.
    """

    points: tuple[tuple[int, int], ...]
    _fit: tuple[float, float] | None = field(repr=False, compare=False)

    def __init__(self, segments: Sequence[SegmentSidecar]) -> None:
        if not segments:
            raise MeetingValidationError("Нет сегментов эпохи")
        points: list[tuple[int, int]] = []
        previous: SegmentSidecar | None = None
        for segment in segments:
            view = SegmentTimeline.from_segment(segment)
            if view.discontinuity or not view.points:
                raise MeetingValidationError("Нельзя отобразить отсчёт через разрыв")
            if previous is not None and (
                any(
                    getattr(segment, name) != getattr(previous, name)
                    for name in (
                        "meeting_id",
                        "capture_id",
                        "meeting_generation",
                        "track",
                        "epoch_id",
                        "clock",
                    )
                )
                or segment.index != previous.index + 1
                or segment.sample_start != previous.sample_end
                or points[-1][0] != previous.sample_end
                or view.points[0][0] != segment.sample_start
                or view.points[0] != points[-1]
            ):
                raise MeetingValidationError("Несмежные сегменты эпохи")
            points.extend(view.points if previous is None else view.points[1:])
            previous = segment
        frozen = tuple(points)
        object.__setattr__(self, "points", frozen)
        object.__setattr__(self, "_fit", _robust_fit(frozen))

    @classmethod
    def from_segments(cls, segments: Sequence[SegmentSidecar]) -> Self:
        return cls(segments)

    @property
    def mapping_method(self) -> str:
        return "robust-linear" if self._fit is not None else "piecewise"

    def sample_to_monotonic(self, sample: int) -> int:
        if type(sample) is not int or not self.points[0][0] <= sample <= self.points[-1][0]:
            raise MeetingValidationError("Нет опорных точек часов")
        return _map(self.points, self._fit, sample)


def _map(points: tuple[tuple[int, int], ...], fit: tuple[float, float] | None, sample: int) -> int:
    if fit is not None:
        slope, intercept = fit
        # Subtract integer origins BEFORE float conversion and restore the
        # integer mono origin AFTER rounding, even near uint64 boundaries.
        return points[0][1] + round(intercept + slope * (sample - points[0][0]))
    for knot_sample, mono in points:
        if sample == knot_sample:
            return mono
    for (left, start), (right, end) in zip(points, points[1:], strict=False):
        if left < sample < right:
            return start + (sample - left) * (end - start) // (right - left)
    raise MeetingValidationError("Нет опорных точек часов")


def _robust_fit(points: tuple[tuple[int, int], ...]) -> tuple[float, float] | None:
    """Median seed + robust scale rejection + four centered least-squares passes.

    Paired half-segment slopes avoid the instability of neighboring knots.
    3 * max(1.4826 MAD, IQR/1.349) is a robust 3-sigma rejection threshold;
    IQR prevents zero MAD for balanced two-mode quantized acquisition jitter.
    The 1ms floor accounts for quantized observations. These constants are
    estimator choices, independent of the stand's 10/50ms acceptance limits.
    There are <=302 observations per segment; fits are cached on the snapshot.
    No claim is made for substantial contamination (>=25%) or persistent bias.
    """
    if len(points) < 61 or points[-1][1] - points[0][1] < 60_000:
        return None
    first_sample, first_mono = points[0]
    xy = [(sample - first_sample, mono - first_mono) for sample, mono in points]
    half = len(xy) // 2
    slope = median(
        (yb - ya) / (xb - xa) for (xa, ya), (xb, yb) in zip(xy[:half], xy[-half:], strict=True)
    )
    intercept = median(y - slope * x for x, y in xy)
    for _ in range(4):
        residuals = [y - (intercept + slope * x) for x, y in xy]
        center = median(residuals)
        q1, _, q3 = quantiles(residuals, n=4, method="inclusive")
        scale = max(
            1.0,
            3 * 1.4826 * median(abs(r - center) for r in residuals),
            3 * (q3 - q1) / 1.349,
        )
        kept = [pair for pair, r in zip(xy, residuals, strict=True) if abs(r - center) <= scale]
        if len(kept) < 61 or kept[-1][1] - kept[0][1] < 60_000:
            return None
        mean_x = sum(x for x, _ in kept) / len(kept)
        mean_y = sum(y for _, y in kept) / len(kept)
        slope = sum((x - mean_x) * (y - mean_y) for x, y in kept) / sum(
            (x - mean_x) ** 2 for x, _ in kept
        )
        intercept = mean_y - slope * mean_x
    if (
        slope <= 0
        or first_mono + round(intercept) < 0
        or first_mono + round(intercept + slope * xy[-1][0]) > UINT_MAX
    ):
        return None
    return slope, intercept


def sample_to_monotonic(segment: SegmentSidecar, sample: int) -> int:
    """Convenience for mutable sidecars; repeated queries should reuse a snapshot."""
    return SegmentTimeline.from_segment(segment).sample_to_monotonic(sample)


def monotonic_to_recorded(intervals: list[dict[str, int]], mono_ms: int) -> int:
    """Map paired open/close boundaries, rejecting time inside a pause or gap."""
    for left, right in zip(intervals[::2], intervals[1::2], strict=False):
        if left["t_mono_ms"] <= mono_ms <= right["t_mono_ms"]:
            return left["recorded_ms"] + mono_ms - left["t_mono_ms"]
    raise MeetingValidationError("meeting-timeline-gap")
