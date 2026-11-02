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
def test_local_fallback_is_marked_when_no_native_observation(
    monkeypatch: pytest.MonkeyPatch, hours: int
) -> None:
    import ctypes
    from types import SimpleNamespace
    from unittest.mock import Mock

    from astra_voice.worker.monitor_stream import _sample_clock

    elapsed = 0
    origin = 987654321
    pulse = Mock()
    pulse.capture_clock.return_value = None

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
        assert clock == "monotonic" and server >= 0
        assert abs(mono - (origin + elapsed - 35)) <= 50


@pytest.mark.parametrize("latency_ms", [-20, 20])
@pytest.mark.parametrize(
    "invalid",
    [
        "unsynchronized",
        "read-corrupt",
        "write-corrupt",
        "negative-read",
        "negative-write",
        "stale",
        "wall-before",
        "wall-after",
        "callback-delay",
        "playing-unused",
    ],
)
def test_native_timing_observations_cancel_smoother_and_dispatch_errors(
    monkeypatch: pytest.MonkeyPatch,
    invalid: str,
    latency_ms: int,
) -> None:
    """Independent Pulse ABI fake: latency is smoothed time minus read index."""
    import ctypes
    from types import SimpleNamespace
    from unittest.mock import Mock

    from astra_voice.worker import monitor_stream, pulse_stream

    class Timeval(ctypes.Structure):
        _fields_ = [("tv_sec", ctypes.c_long), ("tv_usec", ctypes.c_long)]

    class Timing(ctypes.Structure):
        _fields_ = [
            ("timestamp", Timeval),
            ("synchronized_clocks", ctypes.c_int),
            ("sink_usec", ctypes.c_uint64),
            ("source_usec", ctypes.c_uint64),
            ("transport_usec", ctypes.c_uint64),
            ("playing", ctypes.c_int),
            ("write_index_corrupt", ctypes.c_int),
            ("write_index", ctypes.c_int64),
            ("read_index_corrupt", ctypes.c_int),
            ("read_index", ctypes.c_int64),
        ]

    timing = Timing()
    now_us = 100000000
    wall_offset_us = 1700000000000000
    smooth_us = 0
    lib = Mock()
    lib.pa_proplist_new.return_value = 10
    lib.pa_proplist_sets.return_value = 0
    lib.pa_stream_new_with_proplist.return_value = 22
    lib.pa_stream_get_timing_info.return_value = ctypes.pointer(timing)

    def get_time(stream: int, output: object) -> int:
        ctypes.cast(output, ctypes.POINTER(ctypes.c_uint64)).contents.value = smooth_us  # type: ignore[arg-type]
        return 0

    def get_latency(stream: int, output: object, negative: object) -> int:
        delay = smooth_us - timing.read_index * 1000000 // 32000
        ctypes.cast(output, ctypes.POINTER(ctypes.c_uint64)).contents.value = abs(delay)  # type: ignore[arg-type]
        ctypes.cast(negative, ctypes.POINTER(ctypes.c_int)).contents.value = int(delay < 0)  # type: ignore[arg-type]
        return 0

    lib.pa_stream_get_time.side_effect = get_time
    lib.pa_stream_get_latency.side_effect = get_latency
    monkeypatch.setattr(
        pulse_stream._PulseAsync, "__init__", lambda self: setattr(self, "lib", lib)
    )
    monkeypatch.setattr("time.monotonic_ns", lambda: now_us * 1000)
    monkeypatch.setattr("time.time_ns", lambda: (wall_offset_us + now_us) * 1000)
    native = monitor_stream._meeting_pulse_type()()
    assert native.stream_new(1, b"meeting", None, None) == 22
    owner = SimpleNamespace(anchor=None)
    for elapsed_ms in (1000, 510000, 3600000, 7200000):
        # Card runs -200ppm. Snapshot timestamp is server observation; transport
        # includes 100ms of mainloop dispatch delay. No resampling is performed.
        observed_us = 100000000 + elapsed_ms * 1000
        timing.timestamp.tv_sec, timing.timestamp.tv_usec = divmod(
            wall_offset_us + observed_us, 1000000
        )
        timing.synchronized_clocks = timing.playing = 1
        timing.source_usec = 25000 if latency_ms > 0 else 5000
        timing.sink_usec = 5000 if latency_ms > 0 else 25000
        timing.transport_usec = 100000
        timing.write_index = round((elapsed_ms - latency_ms) * 16 * 0.9998) * 2
        now_us = observed_us + timing.transport_usec
        if lib.pa_stream_set_latency_update_callback.called:
            callback = lib.pa_stream_set_latency_update_callback.call_args.args[1]
            callback(22, None)
        now_us = observed_us + 137000
        delivered_us = (elapsed_ms + 87) * 1000
        timing.read_index = round(delivered_us * 0.9998 * 16000 / 1000000) * 2 + 320
        # An independent smoother error would shift the old now-latency axis.
        smooth_us = round((elapsed_ms + 137) * 1000 * 0.9998) + 100000
        clock, _, mono = monitor_stream._sample_clock(native, 22, 320, owner)
        assert clock == "pulse"
        assert abs(mono - (100000000 + delivered_us) / 1000) <= 1

    # Replay 50 delivered 20ms fragments against the last native observation.
    before_reads = lib.pa_stream_get_timing_info.call_count
    for _ in range(50):
        now_us += 20000
        timing.read_index += 640
        assert monitor_stream._sample_clock(native, 22, 320, owner)[0] == "pulse"
    assert lib.pa_stream_get_timing_info.call_count - before_reads == 50
    # Healthy PCM stamps use one cheap timing-info read, with no smoother calls.
    lib.pa_stream_get_time.assert_not_called()
    lib.pa_stream_get_latency.assert_not_called()
    callback = lib.pa_stream_set_latency_update_callback.call_args.args[1]
    now_us = observed_us + timing.transport_usec
    if invalid == "unsynchronized":
        timing.synchronized_clocks = 0
    elif invalid == "read-corrupt":
        timing.read_index_corrupt = 1
    elif invalid == "write-corrupt":
        timing.write_index_corrupt = 1
    elif invalid == "negative-read":
        timing.read_index = -1
    elif invalid == "negative-write":
        timing.write_index = -1
    elif invalid == "wall-before":
        wall_offset_us += 1000000
    elif invalid == "callback-delay":
        now_us += 100000
    elif invalid == "playing-unused":
        timing.playing = 0
    callback(22, None)
    if invalid == "wall-before":
        # Another request already in flight can carry a pre-jump timestamp.
        # Pulse can still label it synchronized after a forward wall jump;
        # its inflated transport must not re-enable a one-second wrong clock.
        now_us += 100000
        timing.transport_usec += 1100000
        callback(22, None)
    if invalid == "stale":
        now_us += 4000000
    elif invalid == "wall-after":
        wall_offset_us += 3600000000
    expected_clock = "pulse" if invalid == "playing-unused" else "monotonic"
    assert monitor_stream._sample_clock(native, 22, 320, owner)[0] == expected_clock
    native.stream_unref(22)
    lib.pa_stream_set_latency_update_callback.assert_called_with(22, None, None)
    assert not native.clock_observations


def test_native_timing_info_abi_prefix_matches_public_linux_layout() -> None:
    import ctypes

    from astra_voice.worker.monitor_stream import _TimingInfo

    if ctypes.sizeof(ctypes.c_void_p) != 8 or ctypes.sizeof(ctypes.c_long) != 8:
        pytest.skip("fixed offsets below are the Linux LP64 ABI")
    assert ctypes.sizeof(_TimingInfo) == 104
    assert _TimingInfo.write_index.offset == 56
    assert _TimingInfo.read_index.offset == 72
    assert _TimingInfo.since_underrun.offset == 96
