"""Meeting-only 100ms PCM batches and bounded reader cadence, without sound."""

from __future__ import annotations

import queue
import threading
from types import SimpleNamespace
from typing import Any

import pytest

from astra_voice.worker import meeting_capture, monitor_stream
from helpers.pulse_fakes import MIC, REMOVE, FakePulse, Step, opened

pytestmark = pytest.mark.unit


def meeting_mic(monkeypatch: pytest.MonkeyPatch) -> tuple[Any, FakePulse]:
    pulse = FakePulse()
    source = monitor_stream.microphone_source()
    source._factory = lambda: pulse
    source._devices = lambda: [MIC]
    source._clock = pulse.clock
    source._sleep = pulse.clock.sleep
    source.open(MIC.name)
    return source, pulse


def test_meeting_mic_emits_100ms_without_changing_dictation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, pulse = meeting_mic(monkeypatch)
    payload = bytes(range(256)) * 25  # Two 100ms frames, byte order is observable.
    try:
        pulse.steps.append(Step(fragments=[payload]))
        assert source.read_chunk() == payload
        assert source._delivered == len(payload)
        assert not source._buffer
    finally:
        source.shutdown()
    ordinary_pulse = FakePulse()
    ordinary = opened(ordinary_pulse)
    try:
        ordinary_pulse.steps.append(Step(fragments=[payload]))
        assert ordinary.read_chunk() == payload[:640]
    finally:
        ordinary.shutdown()


def test_meeting_mic_partial_chunk_still_dispatches_device_removal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, pulse = meeting_mic(monkeypatch)
    try:
        pulse.steps.append(Step(fragments=[b"a" * 640]))
        assert source.read_chunk() == b""
        pulse.steps.append(Step(events=[(REMOVE, MIC.index)]))
        before = pulse.clock()
        assert source.read_chunk() == b"a" * 640
        assert source.read_chunk() is None
        assert source.device_change is not None
        assert pulse.clock() - before <= 0.7
    finally:
        source.shutdown()


def test_meeting_mic_backlog_dispatches_removal_before_next_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, pulse = meeting_mic(monkeypatch)
    try:
        pulse.steps.append(Step(fragments=[b"a" * 64000]))
        assert source.read_chunk() == b"a" * 32000
        pulse.idle_steps.append(Step(events=[(REMOVE, MIC.index)], after=[b"b" * 3200]))
        before = pulse.clock()
        assert source.read_chunk() == b"a" * 32000
        assert source.device_change is not None
        assert source.read_chunk() is None
        assert pulse.clock() - before <= 0.7
    finally:
        source.shutdown()


def test_meeting_mic_catchup_clock_subtracts_retained_pcm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, pulse = meeting_mic(monkeypatch)
    observed: list[int] = []

    def capture_clock(stream: int, buffered: int) -> tuple[int, int]:
        observed.append(buffered)
        return (64000 - buffered) // 32, 100000 + (64000 - buffered) // 32

    pulse.capture_clock = capture_clock  # type: ignore[attr-defined]
    try:
        payload = bytes(range(256)) * 250
        pulse.steps.append(Step(fragments=[payload]))
        first = source.read_chunk()
        assert source.sample_clock() == ("pulse", 1000, 101000)
        second = source.read_chunk()
        assert source.sample_clock() == ("pulse", 2000, 102000)
        assert first + second == payload
        assert observed == [32000, 0]
    finally:
        source.shutdown()


def test_native_far_preserves_partial_tail_without_waiting_for_full_batch() -> None:
    from unittest.mock import Mock

    native = monitor_stream.NativePulse.__new__(monitor_stream.NativePulse)
    native.context, native.stream = 1, 2
    native.buffer = bytearray()
    native.pulse = Mock()
    native.pulse.context_get_state.return_value = 4
    native.pulse.stream_get_state.return_value = 2
    native.pulse.peek.side_effect = [b"a" * 640, None, b"b" * 2560, None]
    assert native.read_chunk() == b"a" * 640
    assert not native.buffer
    assert native.read_chunk() == b"b" * 2560
    assert not native.buffer


class ReaderClock:
    def __init__(self, duration: float = 10.0) -> None:
        self.now = 0.0
        self.end = duration
        self.waits: list[float] = []

    def is_set(self) -> bool:
        return self.now >= self.end

    def wait(self, seconds: float) -> bool:
        self.waits.append(seconds)
        self.now = min(self.end, self.now + max(seconds, 0.0))
        return self.is_set()


@pytest.mark.parametrize("empty", [False, True])
def test_reader_polls_at_100ms_and_stop_interrupts_wait(
    monkeypatch: pytest.MonkeyPatch, empty: bool
) -> None:
    clock = ReaderClock(9.95)
    calls = 0
    packets: list[bytes] = []

    def read() -> bytes:
        nonlocal calls
        calls += 1
        clock.now += 0.00001
        return b"" if empty else b"a" * 3200

    def put(kind: str, payload: Any) -> bool:
        if kind == "pcm":
            packets.append(payload[1])
        return True

    state = SimpleNamespace(
        archive=SimpleNamespace(put=put), messages=queue.SimpleQueue(), _recorded=lambda: 0
    )
    source = SimpleNamespace(
        read_chunk=read, sample_clock=lambda: ("pulse", 0, 100000), shutdown=lambda: None
    )
    monkeypatch.setattr(meeting_capture, "time", SimpleNamespace(monotonic=lambda: clock.now))
    meeting_capture.MeetingCaptureState._read(state, "mic", source, clock)  # type: ignore[arg-type]
    assert calls <= 101, f"unnecessary native polls: {calls}"
    assert all(0 <= wait <= 0.1 for wait in clock.waits)
    assert len(packets) == (0 if empty else calls)
    assert state.messages.empty()


def test_reader_device_end_is_seen_within_100ms_cadence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = ReaderClock()
    closed: list[float] = []
    source = SimpleNamespace(
        read_chunk=lambda: None if clock.now >= 0.001 else b"",
        shutdown=lambda: closed.append(clock.now),
        device_change=None,
    )
    state = SimpleNamespace(
        archive=SimpleNamespace(put=lambda *_: True),
        messages=queue.SimpleQueue(),
        _recorded=lambda: 0,
    )

    def read() -> bytes | None:
        if clock.now >= 0.001:
            source.device_change = object()
            return None
        return b""

    source.read_chunk = read
    monkeypatch.setattr(meeting_capture, "time", SimpleNamespace(monotonic=lambda: clock.now))
    meeting_capture.MeetingCaptureState._read(state, "mic", source, clock)  # type: ignore[arg-type]
    assert closed and closed[0] <= 0.1
    assert state.messages.get_nowait()["reason"] == "mic_change"


def test_reader_stop_wakes_real_event_wait_without_waiting_for_next_poll() -> None:
    entered = threading.Event()

    class Stop(threading.Event):
        def wait(self, timeout: float | None = None) -> bool:
            entered.set()
            return super().wait(timeout)

    stop = Stop()
    state = SimpleNamespace(
        archive=SimpleNamespace(put=lambda *_: True),
        messages=queue.SimpleQueue(),
        _recorded=lambda: 0,
    )
    source = SimpleNamespace(read_chunk=lambda: b"")
    thread = threading.Thread(
        target=meeting_capture.MeetingCaptureState._read,
        args=(state, "mic", source, stop),
    )
    thread.start()
    try:
        assert entered.wait(1)
        stop.set()
        thread.join(0.2)
        assert not thread.is_alive()
        assert state.messages.empty()
    finally:
        stop.set()
        thread.join(1)
