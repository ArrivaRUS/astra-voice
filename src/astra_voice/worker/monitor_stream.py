"""Meeting-only Pulse recording and introspection. No changes to the audio graph.

B-prime is bound to an immutable local target identity. A is an explicit choice;
unsupported B-prime is reported during prepare, never silently widened to A.
Native imports are lazy: constructing capture control creates no PCM context.
"""

from __future__ import annotations

import ctypes
import os
import sys
import threading
import time
import uuid
from array import array
from collections import deque
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Protocol, cast

from astra_voice.meetings.identity import ProcReader, ProcUnavailable
from astra_voice.worker.meeting_audio_local import CONNECT_LOCK
from astra_voice.worker.meeting_detector import INVALID_INDEX, project_properties

if TYPE_CHECKING:
    from astra_voice.worker.meeting_identity_check import PinnedStream, PinnedTarget

_PID = "application.process.id"
_SERIAL = "object.serial"
_PA_ERR_NOENTITY = 5


class _Timeval(ctypes.Structure):
    _fields_ = [("tv_sec", ctypes.c_long), ("tv_usec", ctypes.c_long)]


class _TimingInfo(ctypes.Structure):
    # Public pa_timing_info ABI from pulse/def.h (including its complete tail).
    _fields_ = [
        ("timestamp", _Timeval),
        ("synchronized_clocks", ctypes.c_int),
        ("sink_usec", ctypes.c_uint64),
        ("source_usec", ctypes.c_uint64),
        ("transport_usec", ctypes.c_uint64),
        ("playing", ctypes.c_int),
        ("write_index_corrupt", ctypes.c_int),
        ("write_index", ctypes.c_int64),
        ("read_index_corrupt", ctypes.c_int),
        ("read_index", ctypes.c_int64),
        ("configured_sink_usec", ctypes.c_uint64),
        ("configured_source_usec", ctypes.c_uint64),
        ("since_underrun", ctypes.c_int64),
    ]


_TimingCallback = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p)
_CLOCK_MAX_AGE_US = 3000000  # AUTO_TIMING_UPDATE's maximum interval is 1.5s.
_CLOCK_JITTER_US = 50000


@dataclass(frozen=True)
class _ClockObservation:
    observed_us: int
    sample_us: int
    write_index: int
    wall_offset_us: int


@dataclass(frozen=True, repr=False)
class Target:
    index: int
    serial: int
    pid: int = field(repr=False)
    cookie: int = field(repr=False)
    monitor: str
    client: int
    starttime: int = field(repr=False)


class MonitorPulse(Protocol):
    monitor_supported: bool

    def resolve(
        self, index: int, *, expected_pid: int | None = None, confirm_loss: bool = False
    ) -> Target | None: ...
    def default_monitor(self) -> str: ...
    def open_record(self, monitor: str, index: int | None, token: str) -> None: ...
    def close_record(self) -> None: ...
    def read_chunk(self) -> bytes | None: ...
    def poll_events(self) -> list[tuple[str, int]]: ...
    def sample_clock(self) -> tuple[str, int, int]: ...


def _resolve_stream(
    pulse: MonitorPulse, cookie: int, stream: PinnedStream, *, confirm_loss: bool = False
) -> Target:
    fresh = pulse.resolve(
        stream.sink_input, expected_pid=stream.process.pid, confirm_loss=confirm_loss
    )
    if fresh is None:
        raise ValueError("target-gone")
    if (
        fresh.cookie != cookie
        or fresh.index != stream.sink_input
        or fresh.serial != stream.stream_serial
        or fresh.client != stream.client
        or fresh.pid != stream.process.pid
        or fresh.starttime != stream.process.starttime
    ):
        raise ValueError("target-mismatch")
    return fresh


class MonitorStreamSource:
    def __init__(
        self, pulse: MonitorPulse, token: str, *, target: PinnedTarget | None = None
    ) -> None:
        self.pulse, self.target, self.token = pulse, target, token
        self.lock = threading.RLock()
        self.monitor = ""
        self.ended = False
        self.change: str | None = None
        self.opened = False
        self.admission_check: Callable[[], bool] = lambda: True
        self.admission_guard: Callable[[], AbstractContextManager[Any]] = nullcontext
        if isinstance(pulse, NativePulse):
            pulse.admission_check = lambda: self.admission_check()
            pulse.admission_guard = lambda: self.admission_guard()

    def open(self, device: str | None = None) -> None:
        with self.lock:
            self._open(device)

    def _open(self, device: str | None = None) -> None:
        if self.target is not None:
            if not self.pulse.monitor_supported:
                raise ValueError("monitor-stream-unsupported")
            fresh = self._resolve()
            self._open_record(fresh.monitor, fresh.index)
        else:
            self._open_record(self.pulse.default_monitor(), None)
        self.ended = False

    def _resolve(self) -> Target:
        assert self.target is not None
        (stream,) = self.target.streams
        return _resolve_stream(self.pulse, self.target.server_cookie, stream, confirm_loss=True)

    def _open_record(self, monitor: str, index: int | None) -> None:
        if not self.admission_check():
            raise ValueError("admission-stale")
        try:
            self.pulse.open_record(monitor, index, self.token)
        except Exception as connect_error:
            try:
                self.close()
            finally:
                if isinstance(self.pulse, NativePulse) and not self.pulse.context_ready():
                    raise ValueError("audio-failed") from connect_error
            if self.target is not None and str(connect_error) != "admission-stale":
                try:
                    self._resolve()
                except Exception as target_error:
                    if isinstance(target_error, ValueError) and str(target_error) in {
                        "target-gone",
                        "target-mismatch",
                    }:
                        raise target_error from connect_error
                    # A broken context cannot provide evidence of a vanished target.
            raise
        try:
            if self.target is not None:
                self._resolve()
            if not self.admission_check():
                raise ValueError("admission-stale")
        except Exception:
            self.close()
            raise
        self.monitor = monitor
        self.opened = True

    def read_chunk(self) -> bytes | None:
        with self.lock:
            return self._read_chunk()

    def _read_chunk(self) -> bytes | None:
        if self.ended or not self.opened:
            return None
        self.change = None
        for kind, index in self.pulse.poll_events():
            if self.ended:
                break
            if self.target is not None:
                target_index = self.target.streams[0].sink_input
                if kind == "removed" and index == target_index:
                    self.ended = True
                elif kind == "server" or index == target_index:
                    self._check_recording_target()
                    if self.ended or not self.opened:
                        return None
                    if self.change:
                        return b""
            elif kind == "server":
                fresh_monitor = self.pulse.default_monitor()
                if fresh_monitor != self.monitor:
                    self.pulse.close_record()
                    self.monitor = fresh_monitor
                    if not self.admission_check():
                        self.opened = False
                        return None
                    self.pulse.open_record(self.monitor, None, self.token)
                    if not self.admission_check():
                        self.close()
                        return None
                    self.change = "sink-moved"
                    return b""
        if self.ended:
            self.close()
            return None
        return self.pulse.read_chunk()

    def _check_recording_target(self) -> None:
        assert self.target is not None
        try:
            fresh = self._resolve()
            if fresh.monitor != self.monitor:
                self.close()
                self._open_record(fresh.monitor, fresh.index)
                self.change = "sink-moved"
        except ValueError as exc:
            if str(exc) == "admission-stale":
                return
            if str(exc) not in {"target-gone", "target-mismatch"}:
                raise
            if self.opened:
                self.close()
            self.ended = True

    def sample_clock(self) -> tuple[str, int, int]:
        with self.lock:
            return self.pulse.sample_clock()

    def close(self) -> None:
        with self.lock:
            self.pulse.close_record()
            self.opened = False

    def shutdown(self) -> None:
        with self.lock:
            self.close()
            close = getattr(self.pulse, "close", None)
            if close is not None:
                close()


class _ServerInfo(ctypes.Structure):
    _fields_ = [
        ("user", ctypes.c_char_p),
        ("host", ctypes.c_char_p),
        ("version", ctypes.c_char_p),
        ("name", ctypes.c_char_p),
        ("format", ctypes.c_int),
        ("rate", ctypes.c_uint32),
        ("channels", ctypes.c_uint8),
        ("sink", ctypes.c_char_p),
        ("source", ctypes.c_char_p),
        ("cookie", ctypes.c_uint32),
    ]


# pa_sink_input_info prefix, through proplist. Keep full nested ABI sizes.
class _Volume(ctypes.Structure):
    _fields_ = [("channels", ctypes.c_uint8), ("values", ctypes.c_uint32 * 32)]


class _ChannelMap(ctypes.Structure):
    _fields_ = [("channels", ctypes.c_uint8), ("map", ctypes.c_int * 32)]


class _InputInfo(ctypes.Structure):
    _fields_ = [
        ("index", ctypes.c_uint32),
        ("name", ctypes.c_char_p),
        ("owner", ctypes.c_uint32),
        ("client", ctypes.c_uint32),
        ("sink", ctypes.c_uint32),
        ("format", ctypes.c_int),
        ("rate", ctypes.c_uint32),
        ("channels", ctypes.c_uint8),
        ("channel_map", _ChannelMap),
        ("volume", _Volume),
        ("buffer_usec", ctypes.c_uint64),
        ("sink_usec", ctypes.c_uint64),
        ("resample", ctypes.c_char_p),
        ("driver", ctypes.c_char_p),
        ("mute", ctypes.c_int),
        ("proplist", ctypes.c_void_p),
    ]


class _ClientInfo(ctypes.Structure):
    _fields_ = [
        ("index", ctypes.c_uint32),
        ("name", ctypes.c_char_p),
        ("owner", ctypes.c_uint32),
        ("driver", ctypes.c_char_p),
        ("proplist", ctypes.c_void_p),
    ]


class _SinkInfo(ctypes.Structure):
    _fields_ = [
        ("name", ctypes.c_char_p),
        ("index", ctypes.c_uint32),
        ("description", ctypes.c_char_p),
        ("format", ctypes.c_int),
        ("rate", ctypes.c_uint32),
        ("channels", ctypes.c_uint8),
        ("channel_map", _ChannelMap),
        ("owner", ctypes.c_uint32),
        ("volume", _Volume),
        ("mute", ctypes.c_int),
        ("monitor", ctypes.c_uint32),
        ("monitor_name", ctypes.c_char_p),
    ]


def _meeting_pulse_type() -> Any:
    """Extend the existing Pulse ABI only inside a meeting role's first open."""
    from astra_voice.worker.audio import _PaBufferAttr
    from astra_voice.worker.pulse_stream import _PulseAsync

    class MeetingPulse(_PulseAsync):
        def __init__(self) -> None:
            super().__init__()
            lib = self.lib
            signatures: dict[str, tuple[Any, list[Any]]] = {
                "pa_stream_new_with_proplist": (
                    ctypes.c_void_p,
                    [
                        ctypes.c_void_p,
                        ctypes.c_char_p,
                        ctypes.c_void_p,
                        ctypes.c_void_p,
                        ctypes.c_void_p,
                    ],
                ),
                "pa_proplist_new": (ctypes.c_void_p, []),
                "pa_proplist_sets": (
                    ctypes.c_int,
                    [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p],
                ),
                "pa_proplist_get": (
                    ctypes.c_int,
                    [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_void_p],
                ),
                "pa_proplist_free": (None, [ctypes.c_void_p]),
                "pa_stream_get_time": (ctypes.c_int, [ctypes.c_void_p, ctypes.c_void_p]),
                "pa_stream_get_timing_info": (ctypes.POINTER(_TimingInfo), [ctypes.c_void_p]),
                "pa_stream_set_latency_update_callback": (
                    None,
                    [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p],
                ),
                "pa_context_get_server_info": (
                    ctypes.c_void_p,
                    [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p],
                ),
                "pa_context_get_index": (ctypes.c_uint32, [ctypes.c_void_p]),
                "pa_context_get_server": (ctypes.c_char_p, [ctypes.c_void_p]),
                "pa_context_get_sink_input_info": (
                    ctypes.c_void_p,
                    [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_void_p],
                ),
                "pa_context_get_client_info": (
                    ctypes.c_void_p,
                    [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_void_p],
                ),
                "pa_context_get_sink_info_by_index": (
                    ctypes.c_void_p,
                    [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_void_p],
                ),
                "pa_context_get_sink_info_by_name": (
                    ctypes.c_void_p,
                    [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_void_p],
                ),
            }
            for name, (restype, argtypes) in signatures.items():
                fn = getattr(lib, name)
                fn.restype, fn.argtypes = restype, argtypes
            self.monitor_supported = hasattr(lib, "pa_stream_set_monitor_stream")
            if self.monitor_supported:
                lib.pa_stream_set_monitor_stream.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
                lib.pa_stream_set_monitor_stream.restype = ctypes.c_int
            self.clock_observations: dict[int, _ClockObservation] = {}
            self.clock_wall_offset_us = (time.time_ns() - time.monotonic_ns()) // 1000
            self.clock_valid_after_us = 0
            # Hold the ctypes callback until every stream has been unregistered.
            self.timing_callback = _TimingCallback(self._timing_updated)
            self.token = uuid.uuid4().hex
            self.admission_check: Callable[[], bool] = lambda: True
            self.admission_guard: Callable[[], AbstractContextManager[Any]] = nullcontext

        def context_new(self, api: int, name: bytes) -> int | None:
            with CONNECT_LOCK, self.admission_guard():
                if not self.admission_check():
                    raise ValueError("admission-stale")
                return super().context_new(api, name)

        def context_connect(self, ctx: int, server: bytes | None, flags: int, spawn: None) -> int:
            with CONNECT_LOCK, self.admission_guard():
                if not self.admission_check():
                    raise ValueError("admission-stale")
                return super().context_connect(ctx, server, flags, spawn)

        def iterate(self, timeout_us: int) -> int:
            # One libpulse socket dispatch can consume only a packet header or
            # body. Drain ready work before the reader's 100ms cadence wait, or
            # PCM and timing replies accumulate behind that wait. Only the
            # first iteration may block; bound subsequent work by count/time so
            # close and device handling retain their existing deadlines.
            dispatched = super().iterate(timeout_us)
            if not dispatched:
                return dispatched
            deadline = time.monotonic_ns() + 5_000_000
            for _ in range(63):
                if time.monotonic_ns() >= deadline:
                    break
                ready = super().iterate(0)
                dispatched += ready
                if not ready:
                    break
            return dispatched

        def _timing_updated(self, stream: int, userdata: Any) -> None:
            # Pulse computes transport at dispatch, before calling this callback.
            # It includes mainloop delay. Avoid get_time/get_latency: both contain
            # the same per-stream smoother, whose error would enter our timeline.
            self.clock_observations.pop(stream, None)
            now = time.monotonic_ns() // 1000
            wall = time.time_ns() // 1000
            offset = wall - now
            shifted = abs(offset - self.clock_wall_offset_us) > _CLOCK_JITTER_US
            self.clock_wall_offset_us = offset
            if shifted:
                self.clock_valid_after_us = now
            pointer = self.lib.pa_stream_get_timing_info(stream)
            if not pointer or shifted:
                return
            info = pointer.contents
            # 'playing' describes playback underruns, not recording validity.
            timestamp = info.timestamp.tv_sec * 1000000 + info.timestamp.tv_usec
            if (
                not info.synchronized_clocks
                or info.write_index_corrupt
                or info.read_index_corrupt
                or info.write_index < 0
                or info.read_index < 0
                or info.transport_usec > _CLOCK_MAX_AGE_US
                or abs(wall - timestamp - info.transport_usec) > _CLOCK_JITTER_US
            ):
                return
            observed = now - info.transport_usec
            # More than one timing request may predate a wall-clock jump. The
            # next delayed reply must not restore a pre-jump observation merely
            # because the current wall/mono offset has already been updated.
            if observed < self.clock_valid_after_us:
                return
            self.clock_observations[stream] = _ClockObservation(
                observed,
                observed - info.source_usec + info.sink_usec,
                info.write_index,
                offset,
            )

        def capture_clock(self, stream: int, buffered: int) -> tuple[int, int] | None:
            observation = self.clock_observations.get(stream)
            now = time.monotonic_ns() // 1000
            wall_offset = time.time_ns() // 1000 - now
            if (
                observation is None
                or not 0 <= now - observation.observed_us <= _CLOCK_MAX_AGE_US
                or abs(wall_offset - observation.wall_offset_us) > _CLOCK_JITTER_US
            ):
                return None
            pointer = self.lib.pa_stream_get_timing_info(stream)
            if (
                not pointer
                or pointer.contents.read_index_corrupt
                or pointer.contents.write_index_corrupt
            ):
                return None
            delivered = pointer.contents.read_index - buffered
            if delivered < 0:
                return None
            mono = observation.sample_us + (delivered - observation.write_index) * 1000000 // 32000
            return delivered * 1000 // 32000, max(0, mono // 1000)

        def stream_unref(self, stream: int) -> None:
            self.lib.pa_stream_set_latency_update_callback(stream, None, None)
            self.clock_observations.pop(stream, None)
            super().stream_unref(stream)

        def peek(self, stream: int) -> bytes | int | None:
            fragment = super().peek(stream)
            if isinstance(fragment, int) or (fragment and len(fragment) > 64000):
                raise ValueError("audio-gap")
            return fragment

        def stream_new(self, ctx: int, name: bytes, spec: Any, channel_map: None) -> int | None:
            prop = self.lib.pa_proplist_new()
            if not prop:
                raise ValueError("audio-failed")
            try:
                for key, value in (
                    (b"application.name", b"Astra Voice"),
                    (b"media.name", "Запись встречи".encode()),
                    (b"astra-voice.run", self.token.encode("ascii")),
                ):
                    if self.lib.pa_proplist_sets(prop, key, value) < 0:
                        raise ValueError("audio-failed")
                # The gate only covers this nonblocking native enqueue. It is
                # shared with epoch publication, never with READY or storage.
                with self.admission_guard():
                    if not self.admission_check():
                        raise ValueError("admission-stale")
                    stream = cast(
                        int | None,
                        self.lib.pa_stream_new_with_proplist(ctx, b"meeting", spec, None, prop),
                    )
                    if stream:
                        self.lib.pa_stream_set_latency_update_callback(
                            stream, self.timing_callback, None
                        )
                    return stream
            finally:
                self.lib.pa_proplist_free(prop)

        def stream_connect_record(self, stream: int, name: bytes, attr: Any, flags: int) -> int:
            meeting_attr = _PaBufferAttr(64000, 0xFFFFFFFF, 0xFFFFFFFF, 0xFFFFFFFF, 3200)
            with self.admission_guard():
                if not self.admission_check():
                    raise ValueError("admission-stale")
                return super().stream_connect_record(
                    stream, name, ctypes.byref(meeting_attr), flags
                )

    return MeetingPulse


class NativePulse:
    """One serialized native context. Callback payloads copied before returning."""

    wait_us = 10000
    skip_unconnected_disconnect = False

    def __init__(
        self,
        *,
        admission_check: Callable[[], bool] = lambda: True,
        admission_guard: Callable[[], AbstractContextManager[Any]] = nullcontext,
    ) -> None:
        from astra_voice.worker.pulse_stream import SubscribeCallback

        self.admission_check = admission_check
        self.admission_guard = admission_guard
        self.pulse: Any = _meeting_pulse_type()()
        self.pulse.admission_check = lambda: self.admission_check()
        self.pulse.admission_guard = lambda: self.admission_guard()
        self.context: int | None = None
        self.stream: int | None = None
        self.events: deque[tuple[str, int]] = deque(maxlen=64)
        self.overflow = False
        self.callback = SubscribeCallback(self._event)
        self.monitor_supported = bool(self.pulse.monitor_supported)
        self.cookie = 0
        self.server_kind = "pulseaudio"
        self.server_local = False
        self.default_sink = b""
        self.anchor: tuple[int, int] | None = None
        self.buffer = bytearray()
        self._connect()

    def _connect(self) -> None:
        from astra_voice.worker.pulse_stream import PA_CONTEXT_NOAUTOSPAWN

        p = self.pulse
        try:
            p.mainloop = p.mainloop_new()
            if not p.mainloop:
                raise ValueError("audio-failed")
            api = p.mainloop_get_api(p.mainloop)
            self.context = p.context_new(api, b"astra-voice-meeting")
            if (
                not self.context
                or p.context_connect(self.context, None, PA_CONTEXT_NOAUTOSPAWN, None) < 0
            ):
                raise ValueError("audio-failed")
            self._wait(lambda: p.context_get_state(self.context) == 4)
            p.context_set_subscribe_callback(self.context, self.callback, None)
            op = p.context_subscribe(self.context, (1 << 2) | (1 << 7), None, None)
            if not op:
                raise ValueError("audio-failed")
            p.operation_unref(op)
            self._server()
        except BaseException:
            self.close()
            raise

    def _wait(self, ready: Any) -> None:
        deadline = time.monotonic() + 0.7
        while not ready():
            if time.monotonic() >= deadline or self.pulse.context_get_state(self.context) in (5, 6):
                raise ValueError("audio-failed")
            self.pulse.iterate(5000)

    def context_ready(self) -> bool:
        return self.context is not None and self.pulse.context_get_state(self.context) == 4

    def _event(self, context: int, event: int, index: int, userdata: int) -> None:
        facility = event & 15
        if facility not in (2, 7):
            return
        if len(self.events) == self.events.maxlen:
            self.overflow = True
        self.events.append(
            ("server" if facility == 7 else "removed" if event & 48 == 32 else "changed", index)
        )

    def _server(self) -> None:
        callback_type = ctypes.CFUNCTYPE(
            None, ctypes.c_void_p, ctypes.POINTER(_ServerInfo), ctypes.c_void_p
        )
        seen = False

        def receive(ctx: int, info: Any, unused: int) -> None:
            nonlocal seen
            if info:
                row = info.contents
                self.cookie = int(row.cookie)
                self.default_sink = row.sink or b""
                self.server_kind = "pipewire" if b"PipeWire" in (row.name or b"") else "pulseaudio"
                seen = True

        callback = callback_type(receive)
        op = self.pulse.lib.pa_context_get_server_info(self.context, callback, None)
        if not op:
            raise ValueError("audio-failed")
        try:
            self._wait(lambda: seen)
        finally:
            # Cancel before dropping the callback even on timeout.
            self.pulse.operation_cancel(op)
            self.pulse.operation_unref(op)
        server = self.pulse.lib.pa_context_get_server(self.context) or b""
        self.server_local = server.startswith((b"unix:", b"/"))

    def _identity_properties(
        self, proplist: int | None, names: tuple[str, ...]
    ) -> dict[str, object] | None:
        props: dict[str, object] = {}
        if proplist:
            for name in names:
                pointer, size = ctypes.c_void_p(), ctypes.c_size_t()
                if (
                    self.pulse.lib.pa_proplist_get(
                        proplist, name.encode("ascii"), ctypes.byref(pointer), ctypes.byref(size)
                    )
                    < 0
                ):
                    continue
                # A decimal PID or serial has at most 20 digits followed by NUL.
                if not pointer.value or not 1 <= size.value <= 21:
                    return None
                raw = ctypes.string_at(pointer, size.value)
                if not raw.endswith(b"\0"):
                    return None
                props[name] = raw[:-1].decode("ascii", "replace")
        projected, valid = project_properties(props)
        return projected if valid else None

    def _info(self, function: str, argument: Any, structure: Any) -> Any:
        callback_type = ctypes.CFUNCTYPE(
            None, ctypes.c_void_p, ctypes.POINTER(structure), ctypes.c_int, ctypes.c_void_p
        )
        result: Any = None
        done = False
        unavailable = False

        def receive(ctx: int, info: Any, eol: int, unused: int) -> None:
            nonlocal result, done, unavailable
            if eol:
                if eol < 0:
                    result = None
                    unavailable = (
                        structure is not _InputInfo
                        or self.pulse.context_errno(ctx) != _PA_ERR_NOENTITY
                    )
                else:
                    unavailable = result is None
                done = True
            elif info:
                row = info.contents
                if structure is _InputInfo:
                    props = self._identity_properties(row.proplist, (_SERIAL, _PID))
                    result = (int(row.index), int(row.sink), int(row.client), props)
                elif structure is _ClientInfo:
                    result = (int(row.index), self._identity_properties(row.proplist, (_PID,)))
                else:
                    try:
                        result = (row.monitor_name or b"").decode("utf-8", "strict")
                    except UnicodeDecodeError:
                        result = None

        callback = callback_type(receive)
        op = getattr(self.pulse.lib, function)(self.context, argument, callback, None)
        if not op:
            raise ValueError("audio-failed")
        try:
            self._wait(lambda: done)
        finally:
            self.pulse.operation_cancel(op)
            self.pulse.operation_unref(op)
        if unavailable:
            raise ValueError("audio-failed")
        return result

    def resolve(
        self, index: int, *, expected_pid: int | None = None, confirm_loss: bool = False
    ) -> Target | None:
        def unavailable() -> Target | None:
            if confirm_loss:
                raise ValueError("audio-failed")
            return None

        self._server()
        row = self._info("pa_context_get_sink_input_info", index, _InputInfo)
        if row is None:
            return None
        idx, sink, client, props = row
        if props is None or client == INVALID_INDEX:
            return unavailable()
        serial, stream_pid = props.get(_SERIAL), props.get(_PID)
        if not isinstance(serial, int):
            return unavailable()
        client_row = self._info("pa_context_get_client_info", client, _ClientInfo)
        if client_row is None or client_row[0] != client or client_row[1] is None:
            return unavailable()
        client_pid = client_row[1].get(_PID)
        if client_pid is not None and stream_pid is not None and client_pid != stream_pid:
            return unavailable()
        pid = client_pid if stream_pid is None else stream_pid
        if not isinstance(pid, int):
            return unavailable()
        if stream_pid is None and expected_pid is not None and pid != expected_pid:
            if confirm_loss:
                raise ValueError("target-mismatch")
            return None
        try:
            starttime = ProcReader(uid=os.geteuid()).starttime(pid)
        except (OSError, ProcUnavailable):
            return unavailable()
        monitor = self._info("pa_context_get_sink_info_by_index", sink, _SinkInfo)
        if not monitor:
            return unavailable()
        return Target(idx, serial, pid, self.cookie, monitor, client, starttime)

    def default_monitor(self) -> str:
        self._server()
        monitor = self._info("pa_context_get_sink_info_by_name", self.default_sink, _SinkInfo)
        if not monitor:
            raise ValueError("audio-failed")
        return str(monitor)

    def open_record(self, monitor: str, index: int | None, token: str) -> None:
        from astra_voice.worker.audio import _PaSampleSpec
        from astra_voice.worker.pulse_stream import (
            PA_STREAM_AUTO_TIMING_UPDATE,
            PA_STREAM_DONT_MOVE,
            PA_STREAM_INTERPOLATE_TIMING,
        )

        self.close_record()
        p = self.pulse
        p.token = token
        spec = _PaSampleSpec(3, 16000, 1)
        try:
            self.stream = p.stream_new(self.context, b"meeting", ctypes.byref(spec), None)
            if not self.stream:
                raise ValueError("audio-failed")
            if index is not None and (
                not self.monitor_supported
                or p.lib.pa_stream_set_monitor_stream(self.stream, index) < 0
            ):
                raise ValueError("monitor-stream-unsupported")
            flags = (
                PA_STREAM_AUTO_TIMING_UPDATE | PA_STREAM_INTERPOLATE_TIMING | PA_STREAM_DONT_MOVE
            )
            if p.stream_connect_record(self.stream, monitor.encode(), None, flags) < 0:
                raise ValueError("audio-failed")
            self._wait(lambda: p.stream_get_state(self.stream) == 2)
            self.anchor = None
        except BaseException:
            self.close_record()
            raise

    def poll_events(self) -> list[tuple[str, int]]:
        events, overflow = self._poll_events()
        if overflow:
            events.append(("server", 0))
        return events

    def _poll_events(self) -> tuple[list[tuple[str, int]], bool]:
        self.pulse.iterate(0)
        if self.pulse.context_get_state(self.context) != 4:
            raise ValueError("audio-failed")
        events = list(self.events)
        self.events.clear()
        overflow, self.overflow = self.overflow, False
        return events, overflow

    def read_chunk(self) -> bytes | None:
        if self.stream is None:
            return None
        self.pulse.iterate(self.wait_us)
        if (
            self.pulse.context_get_state(self.context) != 4
            or self.pulse.stream_get_state(self.stream) != 2
        ):
            raise ValueError("audio-failed")
        for _ in range(20):
            fragment = self.pulse.peek(self.stream)
            if fragment is None:
                break
            if isinstance(fragment, int):
                raise ValueError("audio-gap")
            self.buffer.extend(fragment)
            if len(self.buffer) > 64000:
                raise ValueError("audio-gap")
        result = bytes(self.buffer[:32000])
        del self.buffer[: len(result)]
        return result

    def sample_clock(self) -> tuple[str, int, int]:
        if self.stream is None:
            raise ValueError("audio-failed")
        return _sample_clock(self.pulse, self.stream, len(self.buffer), self)

    def close_record(self) -> None:
        stream, self.stream = self.stream, None
        self.buffer.clear()
        if stream is None:
            return
        try:
            if self.skip_unconnected_disconnect and self.pulse.stream_get_state(stream) == 0:
                # PA_STREAM_UNCONNECTED can be unref'd without a disconnect.
                return
            if self.pulse.stream_disconnect(stream) < 0:
                raise ValueError("audio-close-failed")
            self._wait(lambda: self.pulse.stream_get_state(stream) == 4)
        except ValueError:
            # Disconnecting the entire context also physically revokes its streams.
            self.close()
            raise
        finally:
            self.pulse.stream_unref(stream)

    def close(self) -> None:
        if self.stream is not None:
            self.close_record()
        context, self.context = self.context, None
        if context is not None:
            self.pulse.context_set_subscribe_callback(context, None, None)
            self.pulse.context_disconnect(context)
            self.pulse.context_unref(context)
        if self.pulse.mainloop is not None:
            self.pulse.mainloop_free(self.pulse.mainloop)
            self.pulse.mainloop = None


_FRAMES_PER_MS = 16  # 16 kHz mono s16le
_MAX_OUTPUT_FRAMES = 1000 * _FRAMES_PER_MS
_MAX_BUFFER_FRAMES = 1200 * _FRAMES_PER_MS
_SILENCE_MS = 200


def _check_stream_count(target: PinnedTarget) -> None:
    if not target.streams:
        raise ValueError("target-gone")
    if len(target.streams) > 8:
        raise ValueError("target-mismatch")


def _monotonic_ms() -> int:
    return time.monotonic_ns() // 1_000_000


class _MixedMemberSource(MonitorStreamSource):
    def _resolve(self) -> Target:
        if not self.admission_check():
            raise ValueError("admission-stale")
        return super()._resolve()


@dataclass
class _FarMember:
    source: _MixedMemberSource
    # Positions are in frames. Unreleased blocks are disjoint and ordered.
    blocks: deque[tuple[int, bytes]] = field(default_factory=deque)
    end: int | None = None
    mono_ms: int | None = None
    pending_since: int | None = None
    close_started: bool = False
    closer: threading.Thread | None = None
    close_error: Exception | None = None

    def close_native(self) -> None:
        try:
            self.source.shutdown()
        except Exception as exc:
            self.close_error = exc

    @property
    def buffered_end(self) -> int | None:
        if not self.blocks:
            return None
        start, data = self.blocks[-1]
        return start + len(data) // 2

    def discard_before(self, pos: int) -> None:
        while self.blocks and self.blocks[0][0] < pos:
            start, data = self.blocks.popleft()
            if start + len(data) // 2 > pos:
                self.blocks.appendleft((pos, data[(pos - start) * 2 :]))
                break


class MixedFarSource:
    """A single far track for a fixed, independently verified set of streams."""

    def __init__(
        self,
        target: PinnedTarget,
        token: str,
        *,
        factory: Callable[..., NativePulse],
        now_ms: Callable[[], int] = _monotonic_ms,
    ) -> None:
        _check_stream_count(target)
        if len(target.streams) == 1:
            raise ValueError("mixed-source-needs-multiple-streams")
        self.target, self.token = target, token
        self._factory, self._now_ms = factory, now_ms
        self._lock = threading.RLock()
        self._close_gate = threading.Lock()
        self._close_requests = 0
        self._members: list[_FarMember] = []
        self.strict = True
        self.admission_check: Callable[[], bool] = lambda: True
        self.admission_guard: Callable[[], AbstractContextManager[Any]] = nullcontext
        self._reset_mix()

    def _reset_mix(self) -> None:
        self.ended = self.partial = False
        self.lost_streams: tuple[PinnedStream, ...] = ()
        self.change: str | None = None
        self.reanchors = self.late_bytes = 0
        self._out_pos = 0
        self._has_output = False
        self._axis: tuple[int, int] | None = None
        self._leader: _FarMember | None = None
        self._next_pending_since = 0
        self._stamp = self._now_ms()

    def _can_operate(self) -> bool:
        if not self.admission_check():
            return False
        with self._close_gate:
            return self._close_requests == 0

    def open(self, device: str | None = None) -> None:
        with self._lock:
            self._shutdown_members()
            self._reset_mix()
            try:
                for stream in self.target.streams:
                    if not self._can_operate():
                        raise ValueError("admission-stale")
                    pulse = self._factory(
                        admission_check=self._can_operate, admission_guard=self.admission_guard
                    )
                    pulse.skip_unconnected_disconnect = True
                    source = _MixedMemberSource(
                        pulse, self.token, target=replace(self.target, streams=(stream,))
                    )
                    source.admission_check = self._can_operate
                    source.admission_guard = lambda: self.admission_guard()
                    self._members.append(_FarMember(source))
                    try:
                        source.open(device)
                    except ValueError as exc:
                        if self.strict or str(exc) not in {"target-gone", "target-mismatch"}:
                            raise
                        self.lost_streams += (stream,)
                        source.shutdown()
                        self._members.pop()
                if not self._members:
                    self.ended = True
                    raise ValueError("target-gone")
                if self.strict:
                    self._verify_opened_members()
                self.partial = bool(self.lost_streams)
            except BaseException as open_error:
                try:
                    self._shutdown_members()
                except Exception as close_error:
                    raise open_error from close_error
                raise

    def _verify_opened_members(self) -> None:
        for member in self._members:
            if not self._can_operate():
                raise ValueError("admission-stale")
            source = member.source
            assert source.target is not None
            index = source.target.streams[0].sink_input
            events = source.pulse.poll_events()
            if ("removed", index) in events:
                raise ValueError("target-gone")
            if (
                any(kind == "server" or event_index == index for kind, event_index in events)
                and source._resolve().monitor != source.monitor
            ):
                # The identity still matches. Reopen the moved member before its first PCM.
                member.pending_since = self._next_pending_since
                self._next_pending_since += 1

    def read_chunk(self) -> bytes | None:
        with self._lock:
            self.change = None
            if not self._members:
                return None
            if not self._can_operate():
                return b""
            for member in self._members:
                if member.close_error is not None:
                    raise member.close_error
            live = [member for member in self._members if not member.source.ended]
            allow_reopen = True
            chunks: list[tuple[_FarMember, bytes, int]] = []
            for member in sorted(
                live, key=lambda m: (m.pending_since is None, m.pending_since or 0)
            ):
                if not self._can_operate():
                    return b""
                source = member.source
                with source.lock:
                    leader = self._leader if self._leader is not None else live[0]
                    cast(NativePulse, source.pulse).wait_us = 10000 if member is leader else 0
                    data, attempted = self._read_member(member, allow_reopen=allow_reopen)
                    allow_reopen = allow_reopen and not attempted
                    if data is None:
                        if not source.ended:
                            raise ValueError("audio-failed")
                        assert source.target is not None
                        self.lost_streams += source.target.streams
                        live.remove(member)
                        if self._leader is member:
                            self._leader = None
                        if not self._start_close(member):
                            member.close_started = True
                            member.close_native()
                    elif data:
                        if not self._can_operate():
                            return b""
                        _, _, mono_ms = source.sample_clock()
                        chunks.append((member, data, mono_ms))

            self.partial = bool(live and self.lost_streams)
            if not live:
                self.ended = True
                return None
            if not self._can_operate():
                return b""

            if self._leader is None and chunks:
                self._leader = chunks[0][0]
            # Project the other clocks against the latest block of the elected
            # leader. The first member with PCM keeps that role until it is lost.
            chunks.sort(key=lambda chunk: chunk[0] is not self._leader)
            for member, data, mono_ms in chunks:
                self._place(member, data, mono_ms, leading=member is self._leader)
            if self.change:
                return b""
            return self._release(live, self._now_ms())

    def _read_member(self, member: _FarMember, *, allow_reopen: bool) -> tuple[bytes | None, bool]:
        source = member.source
        assert source.target is not None
        target_index = source.target.streams[0].sink_input
        events, overflow = cast(NativePulse, source.pulse)._poll_events()
        if ("removed", target_index) in events:
            member.blocks.clear()
            source.ended = True
            return None, False

        if overflow or any(kind == "server" or index == target_index for kind, index in events):
            if member.pending_since is None:
                member.pending_since = self._next_pending_since
                self._next_pending_since += 1

        attempted = False
        if member.pending_since is not None:
            member.blocks.clear()
            if not allow_reopen:
                return b"", False
            member.pending_since = None
            try:
                fresh = source._resolve()
                if fresh.monitor != source.monitor:
                    attempted = True
                    source.close()
                    source._open_record(fresh.monitor, fresh.index)
                    member.mono_ms = None
                    self.change = "sink-moved"
                    return b"", attempted
            except ValueError as exc:
                if str(exc) == "admission-stale":
                    return b"", attempted
                if str(exc) not in {"target-gone", "target-mismatch"}:
                    raise
                source.ended = True
                return None, attempted

        if not self._can_operate():
            return b"", attempted
        return source.pulse.read_chunk(), attempted

    def _place(self, member: _FarMember, data: bytes, mono_ms: int, *, leading: bool) -> None:
        frames = len(data) // 2
        previous_end, previous_ms = member.end, member.mono_ms
        if leading:
            # A real clock step is visible to the archive, while the leader
            # continues at its next sample position.
            start = self._out_pos if previous_end is None else previous_end
            if self._has_output:
                start = max(self._out_pos, start)
            self._axis = start + frames, mono_ms
        else:
            # Same per-block tolerance as the archive's PCM clock check.
            duration = frames / _FRAMES_PER_MS
            discontinuous = (
                previous_ms is not None
                and abs(mono_ms - previous_ms - duration) > 250 + duration * 200 / 1_000_000
            )
            if discontinuous:
                member.blocks.clear()
                self.reanchors += 1
            assert self._axis is not None
            if previous_end is None or previous_ms is None or discontinuous:
                axis_end, axis_ms = self._axis
                start = axis_end + (mono_ms - axis_ms) * _FRAMES_PER_MS - frames
            else:
                start = previous_end

        end = start + frames
        member.end, member.mono_ms = end, mono_ms
        start_kept = max(start, self._out_pos) if self._has_output else start
        self.late_bytes += (min(end, start_kept) - start) * 2
        buffered_end = member.buffered_end
        if buffered_end is not None:
            start_kept = max(start_kept, buffered_end)
        if start_kept < end:
            member.blocks.append((start_kept, data[(start_kept - start) * 2 : frames * 2]))
        if member.blocks and member.blocks[0][0] < end - _MAX_BUFFER_FRAMES:
            member.discard_before(end - _MAX_BUFFER_FRAMES)
            if not leading:
                member.mono_ms = None
            self.reanchors += 1

    def _release(self, live: list[_FarMember], now: int) -> bytes:
        spans = sorted(
            (pos, pos + len(data) // 2) for member in live for pos, data in member.blocks
        )
        if not spans:
            return b""
        assert self._axis is not None
        axis_end, axis_ms = self._axis
        start = spans[0][0]
        end = start
        for pos, block_end in spans:
            if pos > end:
                break
            end = max(end, block_end)
        end = min(end, start + _MAX_OUTPUT_FRAMES)
        cutoff = axis_end + (now - axis_ms - _SILENCE_MS) * _FRAMES_PER_MS
        for member in live:
            frontier = member.end if member.end is not None else start
            end = min(end, max(frontier, cutoff))
        if end <= start:
            return b""
        data = self._mix(live, start, end)
        self._out_pos = end
        self._has_output = True
        self._stamp = axis_ms + (end - axis_end) // _FRAMES_PER_MS
        for member in live:
            member.discard_before(end)
        return data

    def _mix(self, live: list[_FarMember], start: int, end: int) -> bytes:
        total = array("i", [0]) * (end - start)
        for member in live:
            for pos, block in member.blocks:
                if pos >= end:
                    break
                first = max(pos, start)
                last = min(pos + len(block) // 2, end)
                samples = array("h")
                samples.frombytes(block[(first - pos) * 2 : (last - pos) * 2])
                if sys.byteorder != "little":
                    samples.byteswap()
                for offset, sample in enumerate(samples, first - start):
                    total[offset] += sample
        result = array("h", (min(32767, max(-32768, sample)) for sample in total))
        if sys.byteorder != "little":
            result.byteswap()
        return result.tobytes()

    def sample_clock(self) -> tuple[str, int, int]:
        with self._lock:
            return "monotonic", self._stamp, self._stamp

    def close(self) -> None:
        self.shutdown()

    def shutdown(self) -> None:
        # Publish the stop before waiting for an in-flight native call.
        with self._close_gate:
            self._close_requests += 1
        try:
            with self._lock:
                self._shutdown_members()
        finally:
            with self._close_gate:
                self._close_requests -= 1

    def _start_close(self, member: _FarMember) -> bool:
        thread = threading.Thread(target=member.close_native, daemon=True)
        try:
            thread.start()
        except (OSError, RuntimeError):
            return False
        member.closer = thread
        member.close_started = True
        return True

    def _shutdown_members(self) -> None:
        start_workers = True
        for member in self._members:
            if member.close_started:
                continue
            if start_workers:
                start_workers = self._start_close(member)
            if not start_workers:
                member.close_started = True
                member.close_native()
        for member in self._members:
            if member.closer is not None:
                member.closer.join()
        members, self._members = self._members, []
        self._leader = None
        for member in members:
            member.blocks.clear()
        for member in members:
            if member.close_error is not None:
                raise member.close_error


def _sample_clock(pulse: Any, stream: int, buffered: int, owner: Any) -> tuple[str, int, int]:
    """Use fresh unsmoothed server observations; missing timing is explicit local time."""
    observed = pulse.capture_clock(stream, buffered)
    if observed is not None:
        server, mono = observed
        owner.anchor = (server, mono)
        return "pulse", server, mono
    # Local fallback cannot claim server accuracy. Native readable bytes are in
    # get_latency already; the undelivered Python queue is the only extra delay.
    now = time.monotonic_ns() // 1000000
    latency, negative = ctypes.c_uint64(), ctypes.c_int()
    latency_ok = (
        pulse.stream_get_latency(stream, ctypes.byref(latency), ctypes.byref(negative)) >= 0
    )
    delay = (
        (-(latency.value // 1000) if negative.value else latency.value // 1000) if latency_ok else 0
    )
    mono = max(0, now - delay - buffered * 1000 // 32000)
    return "monotonic", mono, mono


def microphone_source() -> Any:
    """Reuse dictation's source with meeting-only proplist/100ms fragment ABI."""
    from astra_voice.worker.pulse_stream import PulseStreamSource

    class MeetingMicrophone(PulseStreamSource):
        _chunk_bytes = 3200
        _max_chunk_bytes = 32000
        _poll_buffered = True
        admission_check: Callable[[], bool]
        admission_guard: Callable[[], AbstractContextManager[Any]]
        anchor: tuple[int, int] | None = None

        def open(self, device: str | None = None, **kwargs: Any) -> None:
            self.anchor = None
            super().open(device, **kwargs)

        def sample_clock(self) -> tuple[str, int, int]:
            with self._ctx_lock:
                if self._pulse is None or self._stream is None:
                    raise ValueError("audio-failed")
                return _sample_clock(self._pulse, self._stream, len(self._buffer), self)

    def pulse_factory() -> Any:
        pulse = _meeting_pulse_type()()
        pulse.admission_check = lambda: source.admission_check()
        pulse.admission_guard = lambda: source.admission_guard()
        return pulse

    source = MeetingMicrophone(pulse_factory=pulse_factory)
    source.admission_check = lambda: True
    source.admission_guard = nullcontext
    return source


class CapturePulseBackend:
    def __init__(self) -> None:
        self.control: NativePulse | None = None
        self.lock = threading.RLock()
        self.token = uuid.uuid4().hex

    def _control(self) -> NativePulse:
        if self.control is None:
            self.control = NativePulse()
        return self.control

    def prepare(self, far: dict[str, Any], *, target: PinnedTarget | None = None) -> dict[str, Any]:
        with self.lock:
            return self._prepare(far, target=target)

    def _prepare(self, far: dict[str, Any], *, target: PinnedTarget | None) -> dict[str, Any]:
        p = self._control()
        available = True
        reason = None
        if far["kind"] == "app_stream":
            if target is None:
                raise ValueError("target-gone")
            _check_stream_count(target)
            if not p.monitor_supported:
                available, reason = False, "monitor-stream-unsupported"
            else:
                for stream in target.streams:
                    _resolve_stream(p, target.server_cookie, stream)
        return {
            "server_local": p.server_local,
            "server_kind": p.server_kind,
            "far_available": available,
            **({"far_reason": reason} if reason else {}),
        }

    def source(
        self, far: dict[str, Any], *, target: PinnedTarget | None = None
    ) -> MonitorStreamSource | MixedFarSource:
        if far["kind"] != "app_stream":
            return MonitorStreamSource(NativePulse(), self.token)
        if target is None:
            raise ValueError("target-gone")
        if len(target.streams) == 1:
            return MonitorStreamSource(NativePulse(), self.token, target=target)
        return MixedFarSource(target, self.token, factory=NativePulse)

    def own_context(self) -> tuple[int, int] | None:
        with self.lock:
            control = self.control
            if control is None or control.context is None:
                return None
            index = int(control.pulse.lib.pa_context_get_index(control.context))
            return None if index == 0xFFFFFFFF else (index, control.cookie)

    def close(self) -> None:
        with self.lock:
            control, self.control = self.control, None
            if control is not None:
                control.close()
