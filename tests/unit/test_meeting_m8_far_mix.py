"""Mix pinned Pulse streams using scripted PCM and monotonic observations."""

from __future__ import annotations

import gc
import struct
import threading
import weakref
from collections import deque
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from typing import Any
from unittest.mock import Mock

import pytest
from test_monitor_stream import MISMATCHES, PINNED, RESOLVED

from astra_voice.worker import monitor_stream
from astra_voice.worker.monitor_stream import MixedFarSource, Target

pytestmark = pytest.mark.unit


def tone(value: int, ms: int) -> bytes:
    return struct.pack("<h", value) * (16 * ms)


class ScriptedPulse(monitor_stream.NativePulse):
    def __init__(self, target: Target) -> None:
        self.target: Target | None = target
        self.after_open: list[Target | None] = []
        self.open_error: Exception | None = None
        self.calls: list[tuple[object, ...]] = []
        self.events = deque()
        self.monitor_supported = True
        self.chunk = b""
        self.clock = ("pulse", 1000, 1000)
        self.read_error: str | None = None
        self.poll_error: str | None = None
        self.on_read: Callable[[], None] = lambda: None
        self.on_close: Callable[[], None] = lambda: None
        self.destroyed = False
        self.destroyed_event = threading.Event()
        self.recording = False

    def give(self, chunk: bytes, mono: int, kind: str = "pulse") -> None:
        self.chunk = chunk
        self.clock = kind, mono, mono

    def context_ready(self) -> bool:
        return not self.destroyed

    def resolve(
        self, index: int, *, expected_pid: int | None = None, confirm_loss: bool = False
    ) -> Target | None:
        self.calls.append(("resolve", index))
        return self.target

    def open_record(self, monitor: str, index: int | None, token: str) -> None:
        self.calls.append((monitor, index, token))
        if self.after_open:
            self.target = self.after_open.pop(0)
        if self.open_error:
            raise self.open_error
        self.recording = True

    def close_record(self) -> None:
        try:
            if self.recording:
                self.on_close()
        finally:
            self.recording = False
            self.calls.append(("closed",))

    def close(self) -> None:
        assert not self.recording
        self.destroyed = True
        self.destroyed_event.set()

    def read_chunk(self) -> bytes:
        assert self.recording
        self.calls.append(("read",))
        self.on_read()
        if self.read_error:
            raise ValueError(self.read_error)
        chunk, self.chunk = self.chunk, b""
        return chunk

    def _poll_events(self) -> tuple[list[tuple[str, int]], bool]:
        if self.poll_error:
            raise ValueError(self.poll_error)
        events = list(self.events)
        self.events.clear()
        return events, False

    def sample_clock(self) -> tuple[str, int, int]:
        return self.clock


class MixCase:
    def __init__(self, count: int) -> None:
        self.now = 1000
        self.pulses: list[ScriptedPulse] = []
        streams = tuple(
            replace(PINNED.streams[0], sink_input=7 + i, stream_serial=11 + i) for i in range(count)
        )
        self.target = replace(PINNED, streams=streams)
        self.before_open: Callable[[ScriptedPulse, int], None] = lambda pulse, i: None

        class Factory(ScriptedPulse):
            def __init__(pulse) -> None:
                i = len(self.pulses) % count
                stream = streams[i]
                super().__init__(
                    replace(RESOLVED, index=stream.sink_input, serial=stream.stream_serial)
                )
                self.pulses.append(pulse)
                self.before_open(pulse, i)

        self.source = MixedFarSource(self.target, "run1", factory=Factory, now_ms=lambda: self.now)

    def open(self) -> MixedFarSource:
        self.source.open()
        return self.source


@pytest.fixture
def mixer() -> Iterator[Callable[[int], MixCase]]:
    cases: list[MixCase] = []

    def make(count: int) -> MixCase:
        case = MixCase(count)
        cases.append(case)
        return case

    yield make
    for case in cases:
        case.source.shutdown()


def test_alignment_and_jitter_use_sample_count(mixer: Callable[[int], MixCase]) -> None:
    case = mixer(2)
    source = case.open()
    leader, follower = case.pulses
    impulse = b"\x37\x00"
    follower_pcm = impulse + tone(0, 100)[2:]
    for leader_end, follower_end in [(1100, 1137), (1200, 1242), (1300, 1332)]:
        leader.give(tone(0, 100), leader_end)
        follower.give(follower_pcm, follower_end)
        assert source.read_chunk() == tone(0, 37) + impulse + tone(0, 63)[2:]


@pytest.mark.parametrize("leader_shift", [-37, 37])
def test_alignment_when_follower_delivers_first(
    mixer: Callable[[int], MixCase], leader_shift: int
) -> None:
    case = mixer(2)
    source = case.open()
    leader, follower = case.pulses
    follower.give(tone(2, 100), 1100)
    assert source.read_chunk() == b""
    leader.give(tone(1, 100), 1100 + leader_shift)
    assert source.read_chunk() == tone(1 if leader_shift < 0 else 2, 37) + tone(3, 63)


def test_mix_saturates_after_sum_without_attenuation(mixer: Callable[[int], MixCase]) -> None:
    case = mixer(3)
    source = case.open()
    for pulse, values in zip(
        case.pulses, [(30000, -30000, 12000), (30000, -30000, 13000), (0, 0, -1000)], strict=True
    ):
        pulse.give(struct.pack("<3h", *values), 1100)
    assert source.read_chunk() == struct.pack("<3h", 32767, -32768, 24000)
    # Saturation before the third contribution would give the wrong sum.
    for pulse, value in zip(case.pulses, [30000, 30000, -30000], strict=True):
        pulse.give(tone(value, 100), 1200)
    assert source.read_chunk() == tone(30000, 100)


@pytest.mark.parametrize("speaker", [0, 1], ids=["leader", "follower"])
def test_silent_member_contributes_zero(mixer: Callable[[int], MixCase], speaker: int) -> None:
    case = mixer(2)
    source = case.open()
    case.now = 1301
    payload = tone(-1200, 100)
    case.pulses[speaker].give(payload, 1100)
    assert source.read_chunk() == payload


def test_no_data_no_output_or_clock_advance(mixer: Callable[[int], MixCase]) -> None:
    case = mixer(2)
    source = case.open()
    for pulse in case.pulses:
        pulse.give(tone(17, 100), 1100)
    assert source.read_chunk() == tone(34, 100)
    stamp = source.sample_clock()
    case.now += 1000
    assert source.read_chunk() == b""
    assert source.sample_clock() == stamp


def test_reopen_starts_with_no_previous_pcm(mixer: Callable[[int], MixCase]) -> None:
    case = mixer(2)
    source = case.open()
    case.pulses[0].give(tone(31, 200), 1200)
    case.pulses[1].give(tone(2, 100), 1100)
    assert source.read_chunk() == tone(33, 100)
    source.close()
    case.now = 4000
    source.open()
    assert source.sample_clock() == ("monotonic", 4000, 4000)
    assert source.read_chunk() == b""
    for pulse in case.pulses[2:]:
        pulse.give(tone(5, 100), 4100)
    assert source.read_chunk() == tone(10, 100)
    assert all(pulse.destroyed for pulse in case.pulses[:2])


def test_nonleader_clock_step_discards_only_its_tail(mixer: Callable[[int], MixCase]) -> None:
    case = mixer(2)
    source = case.open()
    leader, follower = case.pulses
    leader.give(tone(1, 100), 1100)
    follower.give(tone(50, 200), 1200)
    assert source.read_chunk() == tone(51, 100)
    leader.give(tone(2, 100), 1200)
    follower.give(tone(70, 100), 1560)
    assert source.read_chunk() == tone(2, 100)
    assert source.reanchors == 1 and source.late_bytes == 0

    leader.give(tone(3, 400), 1600)
    follower.give(tone(90, 100), 1660, "monotonic")
    assert source.read_chunk() == tone(3, 260) + tone(73, 100) + tone(93, 40)
    assert source.reanchors == 1 and source.late_bytes == 0


def test_member_buffer_keeps_only_latest_twelve_hundred_ms(
    mixer: Callable[[int], MixCase],
) -> None:
    case = mixer(2)
    source = case.open()
    leader, follower = case.pulses
    leader.give(tone(10, 700), 1700)
    assert source.read_chunk() == b""
    leader.give(tone(20, 700), 2400)
    assert source.read_chunk() == b""
    assert source.reanchors == 1 and source.late_bytes == 0
    follower.give(tone(10, 1000), 2000)
    assert source.read_chunk() == tone(10, 200) + tone(20, 500) + tone(30, 300)


def test_wholly_late_nonleader_packet_stays_out_of_pcm(mixer: Callable[[int], MixCase]) -> None:
    case = mixer(2)
    source = case.open()
    leader, follower = case.pulses
    case.now = 1500
    leader.give(tone(1, 300), 1300)
    assert source.read_chunk() == tone(1, 300)
    leader.give(tone(2, 100), 1400)
    follower.give(tone(300, 100), 1100)
    assert source.read_chunk() == b""
    assert source.late_bytes == 3200 and source.reanchors == 0
    case.now = 1802
    leader.give(tone(3, 100), 1500)
    assert source.read_chunk() == tone(2, 100) + tone(3, 100)


def test_silence_window_and_maximum_output_size(mixer: Callable[[int], MixCase]) -> None:
    case = mixer(2)
    source = case.open()
    leader, _ = case.pulses
    case.now = 2000
    leader.give(tone(1, 1000), 2000)
    assert source.read_chunk() == tone(1, 800)
    assert source.sample_clock() == ("monotonic", 1800, 1800)
    case.now = 3100
    leader.give(tone(2, 1000), 3000)
    out = source.read_chunk()
    assert out is not None and len(out) == 32000
    assert out == tone(1, 200) + tone(2, 800)
    assert source.sample_clock() == ("monotonic", 2800, 2800)


def test_single_member_releases_immediately_with_real_stamp() -> None:
    pulse = ScriptedPulse(RESOLVED)
    source = monitor_stream.MonitorStreamSource(pulse, "direct", target=PINNED)
    source.open()
    try:
        payload = struct.pack("<5h", 2, -12, 32767, -32768, 0) * 320
        for end in [1080, 1182, 1279, 800]:
            pulse.give(payload, end, "pulse")
            assert source.read_chunk() == payload
            assert source.sample_clock() == ("pulse", end, end)
    finally:
        source.shutdown()


def test_member_loss_keeps_survivors_until_all_are_removed(
    mixer: Callable[[int], MixCase],
) -> None:
    case = mixer(3)
    source = case.open()
    first, second, third = case.pulses
    assert not source.partial and source.lost_streams == ()
    second.events.append(("removed", 8))
    first.give(tone(1, 100), 1100)
    third.give(tone(3, 100), 1100)
    assert source.read_chunk() == tone(4, 100)
    assert source.partial and not source.ended
    assert source.lost_streams == case.target.streams[1:2]
    assert second.destroyed_event.wait(2) and not second.recording
    assert source.read_chunk() == b""
    first.events.append(("removed", 7))
    third.events.append(("removed", 9))
    assert source.read_chunk() is None
    assert source.ended and not source.partial and source.read_chunk() is None
    lost = case.target.streams
    assert source.lost_streams == (lost[1], lost[0], lost[2])
    source.shutdown()
    assert source.lost_streams == (lost[1], lost[0], lost[2])


@pytest.mark.parametrize("step", [0, 400])
def test_new_leader_uses_its_real_clock_when_previous_leader_is_lost(
    mixer: Callable[[int], MixCase], step: int
) -> None:
    case = mixer(2)
    source = case.open()
    leader, follower = case.pulses
    leader.give(tone(10, 100), 1100)
    follower.give(tone(3, 100), 1100)
    assert source.read_chunk() == tone(13, 100)
    leader.events.append(("removed", 7))
    follower.give(tone(4, 100), 1200 + step)
    assert source.read_chunk() == tone(4, 100)
    assert source.sample_clock() == ("monotonic", 1200 + step, 1200 + step)
    assert follower.wait_us == 10000


def opens(pulse: ScriptedPulse) -> list[tuple[object, ...]]:
    return [call for call in pulse.calls if len(call) == 3]


def move(pulse: ScriptedPulse) -> None:
    assert pulse.target is not None
    pulse.target = replace(pulse.target, monitor="sink-b.monitor")
    pulse.events.append(("changed", pulse.target.index))


def test_sink_move_reopens_only_its_member_and_yields_no_pcm(
    mixer: Callable[[int], MixCase],
) -> None:
    case = mixer(3)
    source = case.open()
    first, second, third = case.pulses
    move(second)
    first.give(tone(1, 100), 1100)
    third.give(tone(3, 100), 1100)
    stamp = source.sample_clock()
    assert source.read_chunk() == b"" and source.change == "sink-moved"
    assert source.sample_clock() == stamp
    assert [len(opens(p)) for p in case.pulses] == [1, 2, 1]
    assert second.calls[-4:] == [
        ("resolve", 8),
        ("closed",),
        ("sink-b.monitor", 8, "run1"),
        ("resolve", 8),
    ]
    second.give(tone(2, 100), 1100)
    assert source.read_chunk() == tone(6, 100) and source.change is None


@pytest.mark.parametrize("i", [0, 1, 2], ids=["first", "middle", "last"])
@pytest.mark.parametrize(
    "field",
    [field for field, _ in MISMATCHES],
)
def test_each_member_has_its_own_verification_before_pcm(
    mixer: Callable[[int], MixCase], i: int, field: str
) -> None:
    good = mixer(3)
    source = good.open()
    for pulse in good.pulses:
        pulse.give(tone(1, 100), 1100)
    assert source.read_chunk() == tone(3, 100)
    for index, pulse in enumerate(good.pulses, 7):
        assert pulse.calls == [
            ("resolve", index),
            ("sink-a.monitor", index, "run1"),
            ("resolve", index),
            ("read",),
        ]

    bad = mixer(3)

    def swap(pulse: ScriptedPulse, slot: int) -> None:
        assert pulse.target is not None
        if slot == i:
            pulse.after_open.append(
                replace(pulse.target, **{field: getattr(pulse.target, field) + 1})
            )

    bad.before_open = swap
    with pytest.raises(ValueError, match="^target-mismatch$"):
        bad.open()
    assert len(bad.pulses) == i + 1
    assert all(p.destroyed and ("read",) not in p.calls for p in bad.pulses)
    assert bad.source.read_chunk() is None


@pytest.mark.parametrize("code", ["target-gone", "audio-failed"])
def test_open_is_strict_and_shuts_down_every_created_member(
    mixer: Callable[[int], MixCase], code: str
) -> None:
    case = mixer(3)
    source = case.source

    def configure(pulse: ScriptedPulse, i: int) -> None:
        if i != 1:
            return
        if code == "target-gone":
            pulse.target = None
        else:
            pulse.open_error = ValueError(code)

    case.before_open = configure
    with pytest.raises(ValueError, match=f"^{code}$"):
        source.open()
    assert len(case.pulses) == 2
    assert all(p.destroyed and not p.recording for p in case.pulses)
    calls = [p.calls.copy() for p in case.pulses]
    source.shutdown()
    assert [p.calls for p in case.pulses] == calls


@pytest.mark.parametrize("all_gone", [False, True])
def test_soft_open_closes_missing_members_and_never_reads_them(
    mixer: Callable[[int], MixCase], all_gone: bool
) -> None:
    case = mixer(3)
    source = case.source
    source.strict = False

    def configure(pulse: ScriptedPulse, i: int) -> None:
        if i == 0 or (all_gone and i == 2):
            pulse.target = None
        elif i == 1:
            assert pulse.target is not None
            pulse.after_open.append(replace(pulse.target, serial=999))

    case.before_open = configure
    if all_gone:
        with pytest.raises(ValueError, match="^target-gone$"):
            case.open()
        assert source.ended and not source.partial
        assert source.lost_streams == case.target.streams
        assert source.read_chunk() is None
    else:
        case.open()
        assert source.partial and not source.ended
        assert source.lost_streams == case.target.streams[:2]
        case.pulses[2].give(tone(3, 100), 1100)
        assert source.read_chunk() == tone(3, 100)
    assert all(p.destroyed and ("read",) not in p.calls for p in case.pulses[:2])


def test_events_recheck_only_own_index_and_server_rechecks_everyone(
    mixer: Callable[[int], MixCase],
) -> None:
    case = mixer(3)
    source = case.open()
    for pulse in case.pulses:
        pulse.calls.clear()
        pulse.events.append(("changed", 9))
    assert source.read_chunk() == b""
    assert [p.calls for p in case.pulses] == [[("read",)], [("read",)], [("resolve", 9), ("read",)]]
    for pulse in case.pulses:
        pulse.calls.clear()
        pulse.events.append(("server", 0))
    assert source.read_chunk() == b""
    assert [p.calls for p in case.pulses] == [
        [("resolve", 7), ("read",)],
        [("resolve", 8), ("read",)],
        [("resolve", 9), ("read",)],
    ]


def test_only_leader_waits_in_native_read(
    mixer: Callable[[int], MixCase], monkeypatch: pytest.MonkeyPatch
) -> None:
    case = mixer(3)
    source = case.open()
    natives: list[Mock] = []
    with monkeypatch.context() as patch:
        for member in case.pulses:
            native = Mock()
            natives.append(native)
            native.context_get_state.return_value = 4
            native.stream_get_state.return_value = 2
            native.peek.return_value = None
            member.pulse, member.context, member.stream, member.buffer = native, 1, 2, bytearray()
            patch.setattr(
                member, "read_chunk", monitor_stream.NativePulse.read_chunk.__get__(member)
            )
        assert source.read_chunk() == b""
    for native, delay in zip(natives, [10000, 0, 0], strict=True):
        native.iterate.assert_called_once_with(delay)


@pytest.mark.parametrize("operation", ["close", "shutdown"])
def test_close_waits_for_inflight_read(
    mixer: Callable[[int], MixCase], monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    case = mixer(2)
    source = case.open()
    pulse, _ = case.pulses
    entered, attempted, release, read_done, closed = (threading.Event() for _ in range(5))
    lock = source._lock
    close_thread: threading.Thread | None = None

    class ObservableLock:
        def __enter__(self) -> None:
            if threading.current_thread() is close_thread:
                attempted.set()
            lock.acquire()

        def __exit__(self, *_args: object) -> None:
            lock.release()

    def on_read() -> None:
        entered.set()
        assert release.wait(2)
        read_done.set()

    def on_close() -> None:
        attempted.set()
        assert read_done.is_set()
        closed.set()

    def close() -> None:
        nonlocal close_thread
        close_thread = threading.current_thread()
        getattr(source, operation)()

    pulse.on_read, pulse.on_close = on_read, on_close
    with monkeypatch.context() as patch, ThreadPoolExecutor(max_workers=2) as executor:
        patch.setattr(source, "_lock", ObservableLock())
        reading = executor.submit(source.read_chunk)
        try:
            assert entered.wait(2)
            closing = executor.submit(close)
            assert attempted.wait(2)
        finally:
            release.set()
        assert reading.result(timeout=2) == b""
        closing.result(timeout=2)
    assert closed.is_set() and pulse.destroyed
    old_calls = pulse.calls.copy()
    source.close()
    source.shutdown()
    assert pulse.calls == old_calls


def test_drift_remains_in_output_clock_without_accumulated_reanchors(
    mixer: Callable[[int], MixCase],
) -> None:
    case = mixer(2)
    source = case.open()
    payload = tone(1, 1000)
    expected = tone(2, 1000)
    for elapsed in range(1000, 720_001, 1000):
        case.now = 1000 + elapsed + elapsed * 1500 // 1_000_000
        for pulse in case.pulses:
            pulse.give(payload, case.now)
        assert source.read_chunk() == expected
    assert source.sample_clock() == ("monotonic", 722080, 722080)
    assert source.reanchors == source.late_bytes == 0


def test_leader_clock_step_and_clock_kind_change_do_not_reposition_pcm(
    mixer: Callable[[int], MixCase],
) -> None:
    case = mixer(2)
    source = case.open()
    leader, _ = case.pulses
    payload = tone(19, 1000)
    for elapsed in range(1000, 72_001, 1000):
        end = 1000 + elapsed + elapsed * 150 // 1_000_000
        case.now = end + 200
        leader.give(payload, end)
        assert source.read_chunk() == payload
    before = source.sample_clock()[2]
    case.now += 400
    assert source.read_chunk() == b"" and source.sample_clock()[2] == before
    case.now += 100
    payload = tone(-17, 100)
    leader.give(payload, before + 500, "monotonic")
    assert source.read_chunk() == payload
    assert source.sample_clock() == ("monotonic", before + 500, before + 500)
    # Repositioning by raw time would overlap already released frames here.
    backstep = case.now - 400
    leader.give(payload, backstep)
    assert source.read_chunk() == payload
    assert source.sample_clock() == ("monotonic", backstep, backstep)
    assert source.reanchors == source.late_bytes == 0


@pytest.mark.parametrize("fault", ["audio-failed", "audio-gap"])
def test_member_context_or_buffer_fault_escapes_as_track_failure(
    mixer: Callable[[int], MixCase], fault: str
) -> None:
    case = mixer(2)
    source = case.open()
    leader, follower = case.pulses
    if fault == "audio-failed":
        follower.poll_error = fault
    else:
        follower.read_error = fault
    stamp = source.sample_clock()
    leader.give(tone(1, 100), 1100)
    with pytest.raises(ValueError, match=f"^{fault}$"):
        source.read_chunk()
    assert not source.partial and not source.ended
    assert source.sample_clock() == stamp


@pytest.mark.parametrize("event", ["mismatch", "gone", "removed", "sink-moved", "moved-mismatch"])
def test_discards_buffer_on_loss_or_sink_move(mixer: Callable[[int], MixCase], event: str) -> None:
    case = mixer(2)
    source = case.open()
    leader, follower = case.pulses
    leader.give(tone(1, 100), 1100)
    follower.give(tone(4, 200), 1200)
    assert source.read_chunk() == tone(5, 100)

    leader.give(tone(2, 100), 1200)
    assert follower.target is not None
    if event in {"sink-moved", "moved-mismatch"}:
        move(follower)
        if event == "moved-mismatch":
            follower.after_open.append(replace(follower.target, serial=999))
    else:
        follower.events.append(("removed" if event == "removed" else "changed", 8))
        follower.target = replace(follower.target, serial=999) if event == "mismatch" else None

    if event == "sink-moved":
        assert source.read_chunk() == b"" and source.change == "sink-moved"
        case.now = 1500
        leader.give(tone(3, 100), 1300)
        assert source.read_chunk() == tone(2, 100) + tone(3, 100)
        assert not source.partial
    else:
        assert source.read_chunk() == tone(2, 100)
        assert source.partial
    assert not source.ended


def test_factory_checks_admission_between_successful_creations(
    mixer: Callable[[int], MixCase], monkeypatch: pytest.MonkeyPatch
) -> None:
    case = mixer(3)
    source = case.source
    allowed = True
    native_open = monitor_stream.MonitorStreamSource.open

    def revoke_after_first(member: monitor_stream.MonitorStreamSource, device: str | None) -> None:
        nonlocal allowed
        native_open(member, device)
        allowed = False

    assert case.pulses == []
    source.admission_check = lambda: allowed
    with monkeypatch.context() as patch:
        patch.setattr(monitor_stream.MonitorStreamSource, "open", revoke_after_first)
        with pytest.raises(ValueError, match="^admission-stale$"):
            source.open()
    assert len(case.pulses) == 1
    assert case.pulses[0].destroyed and len(opens(case.pulses[0])) == 1


@pytest.mark.parametrize("operation", ["close", "shutdown"])
def test_all_member_closers_run_in_parallel_and_are_joined(
    mixer: Callable[[int], MixCase], operation: str
) -> None:
    count = 8
    case = mixer(count)
    source = case.open()
    barrier = threading.Barrier(count, timeout=2)
    threads: list[threading.Thread] = []

    def at_close() -> None:
        threads.append(threading.current_thread())
        barrier.wait()

    for pulse in case.pulses:
        pulse.on_close = at_close
    getattr(source, operation)()
    assert len(set(threads)) == count and all(t.daemon and not t.is_alive() for t in threads)
    assert all(p.destroyed for p in case.pulses)
    source.shutdown()


@pytest.mark.parametrize("first_fails", [False, True])
def test_defers_other_reopens_and_reads_unaffected_members_in_same_tick(
    mixer: Callable[[int], MixCase], first_fails: bool
) -> None:
    case = mixer(4)
    source = case.open()
    first, second, third, unaffected = case.pulses
    for pulse in (first, second, third):
        move(pulse)
    if first_fails:
        first.after_open.append(None)
    unaffected.give(tone(12, 100), 1100)
    assert source.read_chunk() == b""
    assert source.change == (None if first_fails else "sink-moved")
    assert [len(opens(p)) for p in case.pulses] == [2, 1, 1, 1]
    assert ("read",) not in second.calls and ("read",) not in third.calls
    assert ("read",) in unaffected.calls

    assert source.read_chunk() == b"" and source.change == "sink-moved"
    assert [len(opens(p)) for p in case.pulses] == [2, 2, 1, 1]
    assert ("read",) not in third.calls

    assert source.read_chunk() == b"" and source.change == "sink-moved"
    assert [len(opens(p)) for p in case.pulses] == [2, 2, 2, 1]
    second.give(tone(2, 100), 1100)
    third.give(tone(3, 100), 1100)
    if not first_fails:
        first.give(tone(1, 100), 1100)
    assert source.read_chunk() == tone(17 if first_fails else 18, 100)


def test_eight_simultaneous_sink_moves_are_not_lost(mixer: Callable[[int], MixCase]) -> None:
    case = mixer(8)
    source = case.open()
    for pulse in case.pulses:
        move(pulse)
    for reopened in range(1, 9):
        assert source.read_chunk() == b"" and source.change == "sink-moved"
        assert [len(opens(p)) for p in case.pulses] == [2] * reopened + [1] * (8 - reopened)
    for pulse in case.pulses:
        pulse.give(tone(7, 100), 1100)
    assert source.read_chunk() == tone(56, 100) and source.change is None


def test_pending_move_precedes_new_event_even_when_leader_gets_it(
    mixer: Callable[[int], MixCase],
) -> None:
    case = mixer(3)
    source = case.open()
    first, second, third = case.pulses
    move(second)
    move(third)
    assert source.read_chunk() == b"" and source.change == "sink-moved"
    assert [len(opens(p)) for p in case.pulses] == [1, 2, 1]
    move(first)
    assert source.read_chunk() == b"" and source.change == "sink-moved"
    assert [len(opens(p)) for p in case.pulses] == [1, 2, 2]
    assert source.read_chunk() == b"" and source.change == "sink-moved"
    assert [len(opens(p)) for p in case.pulses] == [2, 2, 2]


@pytest.mark.parametrize("verdict", ["verified", "mismatch", "removed"])
def test_discards_deferred_pcm_before_verdict(
    mixer: Callable[[int], MixCase], verdict: str
) -> None:
    case = mixer(3)
    source = case.open()
    first, pending, survivor = case.pulses
    first.give(tone(1, 100), 1100)
    pending.give(tone(20, 200), 1200)
    survivor.give(tone(3, 100), 1100)
    assert source.read_chunk() == tone(24, 100)

    move(first)
    first.after_open.append(None)
    pending.events.append(("changed", 8))
    survivor.give(tone(3, 100), 1200)
    assert source.read_chunk() == tone(3, 100)

    if verdict == "removed":
        pending.events.append(("removed", 8))
    elif verdict == "mismatch":
        assert pending.target is not None
        pending.target = replace(pending.target, serial=999)
    else:
        pending.give(tone(21, 100), 1300)
    survivor.give(tone(3, 100), 1300)
    assert source.read_chunk() == tone(24 if verdict == "verified" else 3, 100)


@pytest.mark.parametrize("removed", [False, True])
def test_real_queue_overflow_discards_unreleased_pcm(
    mixer: Callable[[int], MixCase], monkeypatch: pytest.MonkeyPatch, removed: bool
) -> None:
    case = mixer(2)
    source = case.open()
    survivor, overflowing = case.pulses
    survivor.give(tone(2, 100), 1100)
    overflowing.give(tone(13, 200), 1200)
    assert source.read_chunk() == tone(15, 100)

    overflowing.events = deque(maxlen=64)
    overflowing.overflow = False
    overflowing.pulse = native = Mock()
    overflowing.context = 1
    native.context_get_state.return_value = 4

    # Lose the only own changed event. All remaining events are unrelated
    # unless this case also removes the pinned target.
    overflowing._event(1, 2, 8, 0)
    for i in range(64):
        overflowing._event(1, 2, 100 + i, 0)
    if removed:
        overflowing._event(1, 2 | 32, 8, 0)
    assert overflowing.overflow and ("changed", 8) not in overflowing.events
    if not removed:
        overflowing.give(tone(14, 100), 1300)
    survivor.give(tone(2, 100), 1200)
    with monkeypatch.context() as patch:
        patch.setattr(
            overflowing,
            "_poll_events",
            monitor_stream.NativePulse._poll_events.__get__(overflowing),
        )
        assert source.read_chunk() == tone(2, 100)
        assert not overflowing.overflow
        if not removed:
            survivor.give(tone(2, 100), 1300)
            assert source.read_chunk() == tone(16, 100)
    assert source.partial is removed


@pytest.mark.parametrize("wrapped", [False, True], ids=["direct", "mixed"])
def test_review_role_stop_during_sink_move_is_not_an_audio_fault(
    mixer: Callable[[int], MixCase], monkeypatch: pytest.MonkeyPatch, wrapped: bool
) -> None:
    from types import SimpleNamespace

    from astra_voice.worker.meeting_capture import MeetingCaptureState

    source: MixedFarSource | monitor_stream.MonitorStreamSource
    if wrapped:
        case = mixer(2)
        source = case.open()
        pulse, _ = case.pulses
    else:
        pulse = ScriptedPulse(RESOLVED)
        source = monitor_stream.MonitorStreamSource(pulse, "direct", target=PINNED)
        source.open()
    stop = threading.Event()
    source.admission_check = lambda: not stop.is_set()
    resolve = pulse.resolve

    def revoke(index: int, **kwargs: Any) -> Target | None:
        stop.set()
        return resolve(index, **kwargs)

    move(pulse)
    role: Any = SimpleNamespace(archive=Mock(), messages=Mock())
    with monkeypatch.context() as patch:
        patch.setattr(pulse, "resolve", revoke)
        try:
            MeetingCaptureState._read(role, "far", source, stop)
        finally:
            source.shutdown()
    assert stop.is_set() and len(opens(pulse)) == 1
    role.messages.put.assert_not_called()
    role.archive.put.assert_not_called()


@pytest.mark.parametrize("same_tick", [False, True])
def test_review_start_includes_earliest_verified_block(
    mixer: Callable[[int], MixCase], same_tick: bool
) -> None:
    case = mixer(2)
    source = case.open()
    leader, early = case.pulses
    early.give(tone(2, 100), 1063)
    if not same_tick:
        assert source.read_chunk() == b""
    leader.give(tone(1, 100), 1100)
    assert source.read_chunk() == tone(2, 37) + tone(3, 63)
    assert source.sample_clock() == ("monotonic", 1063, 1063)


def test_review_regular_delivery_of_old_pcm_does_not_hold_fresh_data(
    mixer: Callable[[int], MixCase],
) -> None:
    case = mixer(2)
    source = case.open()
    leader, lagging = case.pulses
    for pulse in case.pulses:
        pulse.give(tone(2, 100), 1100)
    case.now = 1100
    assert source.read_chunk() == tone(4, 100)
    for now in range(1200, 3001, 100):
        case.now = now
        leader.give(tone(2, 100), now)
        lagging.give(tone(2, 100), now - 500)
        source.read_chunk()
    assert source.sample_clock() == ("monotonic", 2800, 2800)


@pytest.mark.parametrize("same_tick", [False, True], ids=["buffered", "ready"])
def test_review_skips_uncovered_gap_and_stamps_the_real_next_pcm(
    mixer: Callable[[int], MixCase], same_tick: bool
) -> None:
    case = mixer(2)
    source = case.open()
    _, follower = case.pulses
    for pulse in case.pulses:
        pulse.give(tone(2, 100), 1100)
    case.now = 1100
    assert source.read_chunk() == tone(4, 100)
    case.now = 5000
    assert source.read_chunk() == b""
    assert source.sample_clock() == ("monotonic", 1100, 1100)
    follower.give(tone(3, 100), 5100)
    if not same_tick:
        # The frontier still falls before the first new sample.
        assert source.read_chunk() == b""
    case.now = 5400
    assert source.read_chunk() == tone(3, 100)
    assert source.sample_clock() == ("monotonic", 5100, 5100)


def test_first_member_with_pcm_keeps_the_clock_when_others_begin(
    mixer: Callable[[int], MixCase],
) -> None:
    case = mixer(2)
    source = case.open()
    nominal_first, follower = case.pulses
    packet = tone(3, 1000)
    for elapsed in range(1000, 72_001, 1000):
        end = 1000 + elapsed + elapsed * 1500 // 1_000_000
        case.now = end + 201
        follower.give(packet, end)
        assert source.read_chunk() == packet
    assert source.sample_clock() == ("monotonic", 73108, 73108)
    case.now = 74400
    nominal_first.give(tone(2, 1000), 74158)
    follower.give(packet, 74110)
    assert source.read_chunk() == tone(3, 48) + tone(5, 952)
    assert source.sample_clock() == ("monotonic", 74110, 74110)
    assert nominal_first.wait_us == 0 and follower.wait_us == 10000


@pytest.mark.parametrize("event", ["removed", "mismatch"])
def test_last_loss_discards_tail_and_ends(mixer: Callable[[int], MixCase], event: str) -> None:
    case = mixer(2)
    source = case.open()
    first, last = case.pulses
    first.give(tone(2, 100), 1100)
    last.give(tone(3, 200), 1200)
    assert source.read_chunk() == tone(5, 100)
    stamp = source.sample_clock()
    first.events.append(("removed", 7))
    if event == "removed":
        last.events.append(("removed", 8))
    else:
        last.events.append(("changed", 8))
        last.target = None
    assert source.read_chunk() is None and source.ended and not source.partial
    assert source.sample_clock() == stamp


def test_review_oldest_deferred_move_precedes_repeat_of_a_lower_index(
    mixer: Callable[[int], MixCase],
) -> None:
    case = mixer(3)
    source = case.open()
    first, _, last = case.pulses
    for pulse in case.pulses:
        move(pulse)
    assert source.read_chunk() == b"" and source.change == "sink-moved"
    assert first.target is not None
    first.target = replace(first.target, monitor="sink-a.monitor")
    first.events.append(("changed", 7))
    assert source.read_chunk() == b"" and source.change == "sink-moved"
    assert [len(opens(p)) for p in case.pulses] == [2, 2, 1]
    assert source.read_chunk() == b"" and source.change == "sink-moved"
    assert len(opens(last)) == 2 and len(opens(first)) == 2
    assert source.read_chunk() == b"" and source.change == "sink-moved"
    assert len(opens(first)) == 3


@pytest.mark.parametrize("trigger", ["admission", "shutdown"])
def test_review_does_not_start_next_resolve_after_revocation_or_close(
    mixer: Callable[[int], MixCase], monkeypatch: pytest.MonkeyPatch, trigger: str
) -> None:
    case = mixer(8)
    source = case.open()
    first, *others = case.pulses
    resolve = first.resolve
    entered, close_waiting = threading.Event(), threading.Event()
    allowed = True
    lock = source._lock
    closer: threading.Thread | None = None
    source.admission_check = lambda: allowed

    class ObservableLock:
        def __enter__(self) -> None:
            if threading.current_thread() is closer:
                close_waiting.set()
            lock.acquire()

        def __exit__(self, *_args: object) -> None:
            lock.release()

    def first_resolve(index: int, **kwargs: Any) -> Target | None:
        nonlocal allowed
        entered.set()
        if trigger == "admission":
            allowed = False
        else:
            assert close_waiting.wait(2)
        return resolve(index, **kwargs)

    def close() -> None:
        nonlocal closer
        closer = threading.current_thread()
        source.shutdown()

    for pulse in case.pulses:
        pulse.calls.clear()
        pulse.events.append(("server", 0))
    with monkeypatch.context() as patch, ThreadPoolExecutor(max_workers=2) as executor:
        patch.setattr(source, "_lock", ObservableLock())
        patch.setattr(first, "resolve", first_resolve)
        reading = executor.submit(source.read_chunk)
        assert entered.wait(2)
        closing = executor.submit(close) if trigger == "shutdown" else None
        assert reading.result(timeout=3) == b""
        if closing is not None:
            closing.result(timeout=2)
    assert [call for p in others for call in p.calls if call[0] == "resolve"] == []


def test_review_revocation_during_native_open_skips_second_resolve(
    mixer: Callable[[int], MixCase], monkeypatch: pytest.MonkeyPatch
) -> None:
    case = mixer(2)
    source = case.open()
    pulse, _ = case.pulses
    stop = threading.Event()
    source.admission_check = lambda: not stop.is_set()
    open_record = pulse.open_record

    def open_then_revoke(monitor: str, index: int | None, token: str) -> None:
        open_record(monitor, index, token)
        stop.set()

    move(pulse)
    pulse.calls.clear()
    with monkeypatch.context() as patch:
        patch.setattr(pulse, "open_record", open_then_revoke)
        assert source.read_chunk() == b""
    assert source.change is None and not source.ended
    assert [call for call in pulse.calls if call[0] == "resolve"] == [("resolve", 7)]
    assert len(opens(pulse)) == 1 and not pulse.recording


def test_review_lost_context_is_closed_before_global_shutdown(
    mixer: Callable[[int], MixCase], monkeypatch: pytest.MonkeyPatch
) -> None:
    case = mixer(2)
    source = case.open()
    survivor, lost = case.pulses
    lost.events.append(("removed", 8))
    survivor.give(tone(2, 100), 1100)
    closed = threading.Event()
    native_close = lost.close

    def close() -> None:
        native_close()
        closed.set()

    with monkeypatch.context() as patch:
        patch.setattr(lost, "close", close)
        assert source.read_chunk() == tone(2, 100)
        assert closed.wait(2)
    assert lost.destroyed and survivor.recording and source.partial


def test_review_lost_context_close_runs_while_survivor_is_read(
    mixer: Callable[[int], MixCase],
) -> None:
    case = mixer(2)
    source = case.open()
    lost, survivor = case.pulses
    closing, release = threading.Event(), threading.Event()

    def on_close() -> None:
        closing.set()
        assert release.wait(2)

    lost.on_close = on_close
    lost.events.append(("removed", 7))
    survivor.give(tone(2, 100), 1100)
    with ThreadPoolExecutor(max_workers=1) as executor:
        reading = executor.submit(source.read_chunk)
        try:
            assert closing.wait(2)
            assert reading.result(timeout=2) == tone(2, 100)
        finally:
            release.set()
    assert lost.destroyed_event.wait(2)


def test_review_thread_start_failure_closes_every_member_and_joins(
    mixer: Callable[[int], MixCase], monkeypatch: pytest.MonkeyPatch
) -> None:
    case = mixer(3)
    source = case.open()
    real_start = threading.Thread.start
    real_join = threading.Thread.join
    tries: list[threading.Thread] = []
    joined: list[threading.Thread] = []
    ran_on: list[threading.Thread] = []

    def fail_second(thread: threading.Thread) -> None:
        tries.append(thread)
        if len(tries) == 2:
            raise RuntimeError("can't start new thread")
        real_start(thread)

    def observe_join(thread: threading.Thread, timeout: float | None = None) -> None:
        joined.append(thread)
        real_join(thread, timeout)

    for pulse in case.pulses:
        pulse.on_close = lambda: ran_on.append(threading.current_thread())

    error = None
    with monkeypatch.context() as patch:
        patch.setattr(threading.Thread, "start", fail_second)
        patch.setattr(threading.Thread, "join", observe_join)
        try:
            source.shutdown()
        except RuntimeError as exc:
            error = exc
    source.shutdown()
    assert all(p.destroyed and not p.recording for p in case.pulses)
    assert error is None
    assert len(tries) == 2 and not tries[0].is_alive()
    assert joined == tries[:1]
    assert ran_on.count(tries[0]) == 1 and ran_on.count(threading.current_thread()) == 2


def test_review_sample_clock_waits_for_the_inflight_pcm(
    mixer: Callable[[int], MixCase], monkeypatch: pytest.MonkeyPatch
) -> None:
    case = mixer(2)
    source = case.open()
    pulse, other = case.pulses
    pulse.give(tone(7, 100), 1100)
    other.give(tone(0, 100), 1100)
    entered, attempted, resume = threading.Event(), threading.Event(), threading.Event()
    original_lock = source._lock
    clock_thread: threading.Thread | None = None

    class ObservableLock:
        def __enter__(self) -> None:
            if threading.current_thread() is clock_thread:
                attempted.set()
            original_lock.acquire()

        def __exit__(self, *_args: object) -> None:
            original_lock.release()

    def on_read() -> None:
        entered.set()
        assert resume.wait(2)

    def clock() -> tuple[str, int, int]:
        nonlocal clock_thread
        clock_thread = threading.current_thread()
        try:
            return source.sample_clock()
        finally:
            attempted.set()

    pulse.on_read = on_read
    with monkeypatch.context() as patch, ThreadPoolExecutor(max_workers=2) as executor:
        patch.setattr(source, "_lock", ObservableLock())
        reading = executor.submit(source.read_chunk)
        try:
            assert entered.wait(2)
            stamp = executor.submit(clock)
            assert attempted.wait(2)
        finally:
            resume.set()
        assert reading.result(timeout=2) == tone(7, 100)
        assert stamp.result(timeout=2) == ("monotonic", 1100, 1100)


def test_buffered_survivor_can_release_after_other_member_is_lost(
    mixer: Callable[[int], MixCase],
) -> None:
    case = mixer(3)
    source = case.open()
    first, pending, survivor = case.pulses
    first.give(tone(1, 100), 1100)
    pending.give(tone(2, 100), 1100)
    survivor.give(tone(3, 200), 1200)
    assert source.read_chunk() == tone(6, 100)

    move(first)
    first.after_open.append(None)
    pending.events.append(("changed", 8))
    assert source.read_chunk() == b""
    pending.events.append(("removed", 8))
    assert source.read_chunk() == tone(3, 100)
    assert source.partial and not source.ended


def test_shutdown_releases_unread_pcm_and_leader(mixer: Callable[[int], MixCase]) -> None:
    case = mixer(2)
    source = case.open()
    _, first_with_pcm = case.pulses
    first_with_pcm.give(tone(7, 100), 1100)
    assert source.read_chunk() == b""
    member = source._members[1]
    assert source._leader is member and sum(len(block) for _, block in member.blocks) == 3200

    reference = weakref.ref(member)
    source.shutdown()
    assert not member.blocks
    assert source._leader is None and not source._members
    del member
    gc.collect()
    assert reference() is None


def test_shutdown_joins_context_already_retired_by_read(
    mixer: Callable[[int], MixCase], monkeypatch: pytest.MonkeyPatch
) -> None:
    case = mixer(2)
    source = case.open()
    lost, survivor = case.pulses
    closing, release, arrived, joined = (threading.Event() for _ in range(4))
    retired_thread: threading.Thread | None = None
    join = threading.Thread.join

    def on_close() -> None:
        nonlocal retired_thread
        retired_thread = threading.current_thread()
        closing.set()
        assert release.wait(2)

    def trace_join(thread: threading.Thread, timeout: float | None = None) -> None:
        if thread is retired_thread:
            joined.set()
            arrived.set()
        join(thread, timeout)

    def finish() -> None:
        try:
            source.shutdown()
        finally:
            arrived.set()

    lost.on_close = on_close
    lost.events.append(("removed", 7))
    survivor.give(tone(2, 100), 1100)
    with monkeypatch.context() as patch, ThreadPoolExecutor(max_workers=1) as executor:
        patch.setattr(threading.Thread, "join", trace_join)
        try:
            assert source.read_chunk() == tone(2, 100)
            assert closing.wait(2)
            shutdown = executor.submit(finish)
            assert arrived.wait(2)
            assert joined.is_set()
        finally:
            release.set()
        shutdown.result(timeout=2)
    assert all(p.destroyed and not p.recording for p in case.pulses)


def test_removal_closes_context_when_its_closer_cannot_start(
    mixer: Callable[[int], MixCase], monkeypatch: pytest.MonkeyPatch
) -> None:
    case = mixer(2)
    source = case.open()
    lost, survivor = case.pulses
    lost.events.append(("removed", 7))
    survivor.give(tone(2, 100), 1100)

    def fail(_thread: threading.Thread) -> None:
        raise RuntimeError("can't start new thread")

    with monkeypatch.context() as patch:
        patch.setattr(threading.Thread, "start", fail)
        assert source.read_chunk() == tone(2, 100)
        assert lost.destroyed and not lost.recording and survivor.recording and source.partial
    source.shutdown()
    assert all(p.destroyed and not p.recording for p in case.pulses)
