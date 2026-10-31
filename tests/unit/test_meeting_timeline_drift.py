"""T-453: independent sample rate, offset, pauses and epochs; no PCM resampling."""

from __future__ import annotations

import pytest

from astra_voice.meetings import MeetingValidationError
from astra_voice.meetings.segments import SegmentSidecar
from astra_voice.meetings.timeline import SegmentTimeline, monotonic_to_recorded

pytestmark = pytest.mark.unit
MID = "2026-10-31_120000-ab12"


@pytest.mark.parametrize("hours", [1, 2])
@pytest.mark.parametrize("ppm", [-200, 0])
def test_hour_signal_drift_offsets_pause_epochs(hours: int, ppm: int) -> None:
    intervals = [
        {"t_mono_ms": 100000, "recorded_ms": 0},
        {"t_mono_ms": 100000 + hours * 3600000, "recorded_ms": hours * 3600000},
        {"t_mono_ms": 160000 + hours * 3600000, "recorded_ms": hours * 3600000},
        {"t_mono_ms": 170000 + hours * 3600000, "recorded_ms": hours * 3600000 + 10000},
    ]
    for epoch in range(hours * 12):
        start = 100000 + epoch * 300000 + 170  # independent starting offset
        frames = round(300 * 16000 * (1 + ppm / 1e6))
        segment = SegmentSidecar(
            meeting_id=MID,
            epoch_id=f"epoch-{epoch}",
            frames=frames,
            sample_end=frames,
            timeline=[[round(t * frames / 300), start + t * 1000] for t in range(301)],
        )
        timeline = SegmentTimeline.from_segment(segment)
        for tick in range(1, 299):
            t = tick + 0.137
            sample = round(t * frames / 300)
            mono = timeline.sample_to_monotonic(sample)
            assert abs(mono - (start + t * 1000)) <= 50
            assert abs(monotonic_to_recorded(intervals, mono) - (mono - 100000)) <= 2
        with pytest.raises(MeetingValidationError):
            timeline.sample_to_monotonic(frames + 1)
    assert monotonic_to_recorded(intervals, 165000 + hours * 3600000) == hours * 3600000 + 5000
    with pytest.raises(MeetingValidationError):
        monotonic_to_recorded(intervals, 120000 + hours * 3600000)


@pytest.mark.parametrize("hours", [1, 2])
def test_native_card_clock_minus_200ppm_does_not_accumulate_drift(
    monkeypatch: pytest.MonkeyPatch, hours: int
) -> None:
    import ctypes
    from types import SimpleNamespace
    from unittest.mock import Mock

    from astra_voice.worker.monitor_stream import _sample_clock

    elapsed = 0
    origin = 987654321
    pulse = Mock()

    def server_time(stream: int, output: object) -> int:
        value = ctypes.cast(output, ctypes.POINTER(ctypes.c_uint64))  # type: ignore[arg-type]
        value.contents.value = round(elapsed * 1000 * 0.9998)
        return 0

    def latency(stream: int, output: object, negative: object) -> int:
        ctypes.cast(output, ctypes.POINTER(ctypes.c_uint64)).contents.value = 25000  # type: ignore[arg-type]
        ctypes.cast(negative, ctypes.POINTER(ctypes.c_int)).contents.value = 0  # type: ignore[arg-type]
        return 0

    pulse.lib.pa_stream_get_time.side_effect = server_time
    pulse.stream_get_latency.side_effect = latency
    monkeypatch.setattr("time.monotonic_ns", lambda: (origin + elapsed) * 1000000)
    owner = SimpleNamespace(anchor=None)
    for elapsed in range(137, hours * 3600000, 7137):
        clock, server, mono = _sample_clock(pulse, 1, 320, owner)
        assert clock == "pulse" and server >= 0
        assert abs(mono - (origin + elapsed - 35)) <= 50
