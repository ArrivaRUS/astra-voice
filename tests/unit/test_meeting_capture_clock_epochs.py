"""S4g: batched readers must drain ready protocol work, never starve clocks/PCM."""

from __future__ import annotations

import ctypes
import json
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest
from test_meeting_capture import Rig, wait_until

from astra_voice.worker import monitor_stream, pulse_stream

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("filesystem_euid")]


class ProtocolQueue:
    """Pulse socket transport reads a packet header and its body separately."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.now_us = 100_000_000
        self.origin_us = self.now_us
        self.wall_offset_us = 1_700_000_000_000_000
        self.events: deque[Any] = deque()
        self.pcm: deque[bytes] = deque()
        self.generated = 0
        self.delivered = 0
        self.calls = 0
        self.timing = monitor_stream._TimingInfo()
        self.lib = Mock()
        self.lib.pa_stream_get_timing_info.return_value = ctypes.pointer(self.timing)
        monkeypatch.setattr(
            pulse_stream._PulseAsync, "__init__", lambda native: setattr(native, "lib", self.lib)
        )
        monkeypatch.setattr(pulse_stream._PulseAsync, "iterate", self.iterate)
        monkeypatch.setattr(
            monitor_stream,
            "time",
            SimpleNamespace(
                monotonic_ns=lambda: self.now_us * 1000,
                time_ns=lambda: (self.wall_offset_us + self.now_us) * 1000,
            ),
        )
        self.native = monitor_stream._meeting_pulse_type()()
        # The initial READY timing sample is valid before the first PCM packet.
        self.update(self.now_us, 0)
        self.native.stream_get_latency = lambda *args: -1

    def iterate(self, timeout_us: int) -> int:
        self.calls += 1
        if not self.events:
            return 0
        action = self.events.popleft()
        action()
        return 1

    def update(self, observed_us: int, written: int) -> None:
        info = self.timing
        info.timestamp.tv_sec, info.timestamp.tv_usec = divmod(
            self.wall_offset_us + observed_us, 1_000_000
        )
        info.synchronized_clocks = 1
        info.transport_usec = self.now_us - observed_us
        info.write_index = written
        info.read_index = self.delivered
        self.native._timing_updated(1, None)

    def packet(self, data: bytes) -> None:
        self.generated += len(data)
        self.events.extend((lambda: None, lambda: self.pcm.append(data)))

    def timing_packet(self) -> None:
        observed = self.now_us
        written = (self.now_us - self.origin_us) * 32000 // 1000000
        self.events.extend((lambda: None, lambda: self.update(observed, written)))

    def read(self) -> bytes:
        self.native.iterate(0)
        data = b"".join(self.pcm)
        self.pcm.clear()
        self.delivered += len(data)
        self.timing.read_index = self.delivered
        return data


@pytest.mark.parametrize("packet_ms", [100, 1000])
def test_100ms_cadence_drains_protocol_and_keeps_one_epoch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, packet_ms: int
) -> None:
    protocol = ProtocolQueue(monkeypatch)
    rig = Rig(tmp_path, fsync=lambda fd: None)
    try:
        rig.prepare("none")
        rig.open()
        archive = rig.state.archive
        assert archive is not None
        owner = SimpleNamespace(anchor=None)
        for elapsed in range(100, 12001, 100):
            protocol.now_us = protocol.origin_us + elapsed * 1000
            if elapsed % packet_ms == 0:
                protocol.packet(b"\0" * (packet_ms * 32))
            if elapsed % 1500 == 0:
                protocol.timing_packet()
            data = protocol.read()
            if data:
                assert archive.put(
                    "pcm",
                    (
                        "mic",
                        data,
                        monitor_stream._sample_clock(protocol.native, 1, 0, owner),
                        elapsed,
                    ),
                )
                wait_until(lambda: archive.pending_bytes == 0)
        rig.stop()
        sides = [
            json.loads(path.read_text()) for path in (rig.directory / "audio").glob("mic-*.json")
        ]
        assert (protocol.delivered, len(sides)) == (12000 * 32, 1), (
            "ready PCM/timing packets were left behind the 100ms cadence"
        )
        assert sides[0]["frames"] == 12000 * 16
        assert sides[0]["clock"] == "pulse"
        assert not sides[0]["discontinuity"]
        assert sides[0]["timeline"][-1] == [192000, 112000]
    finally:
        rig.cleanup()


@pytest.mark.parametrize("jump_ms", [-1000, 1000])
def test_batched_observed_clock_jump_still_splits_epoch(tmp_path: Path, jump_ms: int) -> None:
    rig = Rig(tmp_path, fsync=lambda fd: None)
    try:
        rig.prepare("none")
        rig.open()
        archive = rig.state.archive
        assert archive is not None
        for end in range(100, 2100, 100):
            mono = 100000 + end + (jump_ms if end > 1000 else 0)
            assert archive.put("pcm", ("mic", b"\0" * 3200, ("pulse", end, mono), end))
            wait_until(lambda: archive.pending_bytes == 0)
        rig.stop()
        sides = [
            json.loads(path.read_text())
            for path in sorted((rig.directory / "audio").glob("mic-*.json"))
        ]
        assert len(sides) == 2
        assert sides[0]["discontinuity"] and sides[0]["close_reason"] == "clock_change"
        assert sides[0]["epoch_id"] != sides[1]["epoch_id"]
    finally:
        rig.cleanup()


def test_ready_native_drain_has_count_and_time_budgets(monkeypatch: pytest.MonkeyPatch) -> None:
    protocol = ProtocolQueue(monkeypatch)
    protocol.events.extend([lambda: None] * 1000)
    before = protocol.calls
    protocol.native.iterate(0)
    assert 1 < protocol.calls - before <= 64
    assert len(protocol.events) >= 936
    protocol.events.clear()

    def costly() -> None:
        protocol.now_us += 1000

    protocol.events.extend([costly] * 1000)
    before_time = protocol.now_us
    protocol.native.iterate(0)
    # One initial 1ms dispatch plus at most 5ms of additional nonblocking work.
    assert protocol.now_us - before_time <= 6000


def test_idle_native_pump_does_not_spin(monkeypatch: pytest.MonkeyPatch) -> None:
    protocol = ProtocolQueue(monkeypatch)
    protocol.native.iterate(0)
    assert protocol.calls == 1
