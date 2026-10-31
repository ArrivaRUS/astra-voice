"""Durable capture boundaries and sample clocks; WAV capture is a later milestone.

Samples are absolute offsets within one epoch. Timeline knots use server-derived
CLOCK_MONOTONIC milliseconds, never Python buffer-arrival times. A discontinuous
sidecar is conservatively unusable for interpolation; capture splits at gaps.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from astra_voice.meetings import MeetingValidationError
from astra_voice.meetings.events import PauseReason
from astra_voice.meetings.model import (
    BOOL,
    IDENTIFIER,
    MEETING_ID,
    MILLISECONDS,
    UINT,
    VERSION,
    JsonModel,
    array,
    choices,
    nullable,
    obj,
)

CLOCKS = ("pulse", "pipewire", "monotonic")
CLOSE_REASONS = (
    *PauseReason,
    "segment",
    "stop",
    "limit",
    "sleep",
    "session",
    "clock_change",
    "crash",
    "target_changed",
    "far_ended",
)


@dataclass
class ClockAnchor(JsonModel):
    server_ms: int = 0
    t_mono_ms: int = 0
    SCHEMA = obj({"server_ms": UINT, "t_mono_ms": UINT})


@dataclass
class SegmentSidecar(JsonModel):
    meeting_id: str
    epoch_id: str
    schema: int = 1
    meeting_generation: int = 0
    capture_id: str = "unspecified"
    state_rev: int = 0
    manual_resume_required: bool = False
    blocked_epoch: int = 0
    track: str = "mic"
    index: int = 0
    rate: int = 16000
    channels: int = 1
    width: int = 2
    opened_t_mono_ms: int = 0
    recorded_ms_at_open: int = 0
    closed: bool = False
    frames: int = 0
    frames_at_checkpoint: int = 0
    timeline: list[list[int]] = field(default_factory=list)
    source_kind: str = "mic_only"
    target_changed: bool = False
    lost_estimate_ms: int = 0
    clock: str = "pulse"
    clock_anchor: ClockAnchor = field(default_factory=ClockAnchor)
    sample_start: int = 0
    sample_end: int = 0
    discontinuity: bool = False
    close_reason: str | None = None
    pause_reasons: list[str] = field(default_factory=list)
    SCHEMA = obj(
        {
            "schema": VERSION,
            "meeting_id": MEETING_ID,
            "meeting_generation": UINT,
            "capture_id": IDENTIFIER,
            "state_rev": UINT,
            "manual_resume_required": BOOL,
            "blocked_epoch": UINT,
            "track": choices("mic", "far"),
            "index": {**UINT, "maximum": 9999},
            "rate": {"type": "integer", "const": 16000},
            "channels": {"type": "integer", "const": 1},
            "width": {"type": "integer", "const": 2},
            "opened_t_mono_ms": UINT,
            "recorded_ms_at_open": MILLISECONDS,
            "closed": BOOL,
            "frames": {**UINT, "maximum": 4_800_000},
            "frames_at_checkpoint": UINT,
            "timeline": array({"type": "array", "items": UINT, "minItems": 2, "maxItems": 2}, 302),
            "source_kind": choices("app_stream", "mic_only", "sink_monitor"),
            "target_changed": BOOL,
            "lost_estimate_ms": {**UINT, "maximum": 10000},
            "epoch_id": IDENTIFIER,
            "clock": choices(*CLOCKS),
            "clock_anchor": ClockAnchor.SCHEMA,
            "sample_start": UINT,
            "sample_end": UINT,
            "discontinuity": BOOL,
            "close_reason": nullable(choices(*CLOSE_REASONS)),
            "pause_reasons": array(choices(*PauseReason), len(PauseReason), unique=True),
        }
    )

    @classmethod
    def _check(cls, data: dict[str, Any]) -> None:
        super()._check(data)
        if (
            data["sample_end"] - data["sample_start"] != data["frames"]
            or data["frames_at_checkpoint"] > data["frames"]
        ):
            raise MeetingValidationError("Недопустимый диапазон отсчётов")
        points = data["timeline"]
        for index, (sample, mono) in enumerate(points):
            if not data["sample_start"] <= sample <= data["sample_end"]:
                raise MeetingValidationError("Точка вне эпохи сегмента")
            if index and (sample <= points[index - 1][0] or mono <= points[index - 1][1]):
                raise MeetingValidationError("Немонотонные часы сегмента")


SEGMENT_SCHEMA = SegmentSidecar.SCHEMA
