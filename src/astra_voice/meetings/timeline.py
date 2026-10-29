"""Pure sample-clock mapping within a validated, continuous capture boundary."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Self

from astra_voice.meetings import MeetingValidationError
from astra_voice.meetings.segments import SegmentSidecar


@dataclass(frozen=True)
class SegmentTimeline:
    """Immutable clock snapshot. Construct once with from_segment() after loading."""

    sample_start: int
    sample_end: int
    discontinuity: bool
    points: tuple[tuple[int, int], ...]

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
        """Interpolate between this epoch's knots; never extrapolate over gaps."""
        if (
            type(sample) is not int
            or self.discontinuity
            or not self.sample_start <= sample <= self.sample_end
        ):
            raise MeetingValidationError("Нельзя отобразить отсчёт через разрыв")
        for knot_sample, mono in self.points:
            if sample == knot_sample:
                return mono
        for (left, start), (right, end) in zip(self.points, self.points[1:], strict=False):
            if left < sample < right:
                return start + (sample - left) * (end - start) // (right - left)
        raise MeetingValidationError("Нет опорных точек часов")


def sample_to_monotonic(segment: SegmentSidecar, sample: int) -> int:
    """Convenience for mutable sidecars; repeated queries should reuse a snapshot."""
    return SegmentTimeline.from_segment(segment).sample_to_monotonic(sample)
