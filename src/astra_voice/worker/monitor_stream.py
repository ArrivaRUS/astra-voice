"""Meeting-only Pulse recording and introspection. No changes to the audio graph.

B-prime is bound to an immutable local target identity. A is an explicit choice;
unsupported B-prime is reported during prepare, never silently widened to A.
Native imports are lazy: constructing capture control creates no PCM context.
"""

from __future__ import annotations

import ctypes
import os
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass
from typing import Any, Protocol, cast


@dataclass(frozen=True)
class Target:
    index: int
    serial: int
    pid: int
    cookie: int
    monitor: str


class MonitorPulse(Protocol):
    monitor_supported: bool

    def resolve(self, index: int) -> Target | None: ...
    def default_monitor(self) -> str: ...
    def open_record(self, monitor: str, index: int | None, token: str) -> None: ...
    def close_record(self) -> None: ...
    def read_chunk(self) -> bytes | None: ...
    def poll_events(self) -> list[tuple[str, int]]: ...
    def sample_clock(self) -> tuple[str, int, int]: ...


class MonitorStreamSource:
    def __init__(self, pulse: MonitorPulse, far: dict[str, Any], token: str) -> None:
        self.pulse, self.far, self.token = pulse, far, token
        self.lock = threading.RLock()
        self.target: Target | None = None
        self.monitor = ""
        self.ended = False
        self.change: str | None = None
        self.opened = False

    def open(self, device: str | None = None) -> None:
        with self.lock:
            self._open(device)

    def _open(self, device: str | None = None) -> None:
        if self.far["kind"] == "app_stream":
            if not self.pulse.monitor_supported:
                raise ValueError("monitor-stream-unsupported")
            target = self.pulse.resolve(int(self.far["sink_input"]))
            if target is None or target.serial != self.far["object_serial"] or target.pid <= 0:
                raise ValueError("target-mismatch")
            if self.target is not None and (
                target.index,
                target.serial,
                target.pid,
                target.cookie,
            ) != (self.target.index, self.target.serial, self.target.pid, self.target.cookie):
                raise ValueError("target-mismatch")
            self.target = target
            self.monitor = target.monitor
        else:
            self.target = None
            self.monitor = self.pulse.default_monitor()
        self.pulse.open_record(self.monitor, self.target.index if self.target else None, self.token)
        self.opened = True
        self.ended = False

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
                if kind == "removed" and index == self.target.index:
                    self.ended = True
                elif kind == "server" or index == self.target.index:
                    fresh = self.pulse.resolve(self.target.index)
                    old = self.target
                    if fresh is None or (fresh.index, fresh.serial, fresh.pid, fresh.cookie) != (
                        old.index,
                        old.serial,
                        old.pid,
                        old.cookie,
                    ):
                        self.ended = True
                    elif fresh.monitor != self.monitor:
                        self.pulse.close_record()
                        self.target = fresh
                        self.monitor = fresh.monitor
                        self.pulse.open_record(self.monitor, fresh.index, self.token)
                        self.change = "sink-moved"
                        return b""
            elif kind == "server":
                fresh_monitor = self.pulse.default_monitor()
                if fresh_monitor != self.monitor:
                    self.pulse.close_record()
                    self.monitor = fresh_monitor
                    self.pulse.open_record(self.monitor, None, self.token)
                    self.change = "sink-moved"
                    return b""
        if self.ended:
            self.close()
            return None
        return self.pulse.read_chunk()

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
                "pa_proplist_gets": (ctypes.c_char_p, [ctypes.c_void_p, ctypes.c_char_p]),
                "pa_proplist_free": (None, [ctypes.c_void_p]),
                "pa_stream_get_time": (ctypes.c_int, [ctypes.c_void_p, ctypes.c_void_p]),
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
            self.token = uuid.uuid4().hex

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
                return cast(
                    int | None,
                    self.lib.pa_stream_new_with_proplist(ctx, b"meeting", spec, None, prop),
                )
            finally:
                self.lib.pa_proplist_free(prop)

        def stream_connect_record(self, stream: int, name: bytes, attr: Any, flags: int) -> int:
            meeting_attr = _PaBufferAttr(64000, 0xFFFFFFFF, 0xFFFFFFFF, 0xFFFFFFFF, 3200)
            return super().stream_connect_record(stream, name, ctypes.byref(meeting_attr), flags)

    return MeetingPulse


class NativePulse:
    """One serialized native context. Callback payloads copied before returning."""

    def __init__(self) -> None:
        from astra_voice.worker.pulse_stream import SubscribeCallback

        self.pulse: Any = _meeting_pulse_type()()
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

    def _info(self, function: str, argument: Any, structure: Any) -> Any:
        callback_type = ctypes.CFUNCTYPE(
            None, ctypes.c_void_p, ctypes.POINTER(structure), ctypes.c_int, ctypes.c_void_p
        )
        result: Any = None
        done = False

        def receive(ctx: int, info: Any, eol: int, unused: int) -> None:
            nonlocal result, done
            if eol:
                done = True
            elif info:
                row = info.contents
                if structure is _InputInfo:
                    props: dict[str, str] = {}
                    for key in ("object.serial", "application.process.id"):
                        raw = self.pulse.lib.pa_proplist_gets(row.proplist, key.encode())
                        props[key] = raw.decode("ascii", "replace") if raw else ""
                    result = (int(row.index), int(row.sink), props)
                else:
                    result = (row.monitor_name or b"").decode("utf-8", "strict")

        callback = callback_type(receive)
        op = getattr(self.pulse.lib, function)(self.context, argument, callback, None)
        if not op:
            raise ValueError("audio-failed")
        try:
            self._wait(lambda: done)
        finally:
            self.pulse.operation_cancel(op)
            self.pulse.operation_unref(op)
        return result

    def resolve(self, index: int) -> Target | None:
        self._server()
        row = self._info("pa_context_get_sink_input_info", index, _InputInfo)
        if row is None:
            return None
        idx, sink, props = row
        try:
            serial, pid = int(props["object.serial"]), int(props["application.process.id"])
        except ValueError:
            return None
        # Local process identity is checked here, never taken from an IPC PID.
        try:
            if os.stat(f"/proc/{pid}").st_uid != os.geteuid():
                return None
        except OSError:
            return None
        monitor = self._info("pa_context_get_sink_info_by_index", sink, _SinkInfo)
        return Target(idx, serial, pid, self.cookie, monitor) if monitor else None

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
        self.pulse.iterate(0)
        if self.pulse.context_get_state(self.context) != 4:
            raise ValueError("audio-failed")
        events = list(self.events)
        self.events.clear()
        if self.overflow:
            events.append(("server", 0))
            self.overflow = False
        return events

    def read_chunk(self) -> bytes | None:
        if self.stream is None:
            return None
        self.pulse.iterate(10000)
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


def _sample_clock(pulse: Any, stream: int, buffered: int, owner: Any) -> tuple[str, int, int]:
    """Map the delivered sample end using current server time and signed latency.

    Pulse time is in the card clock domain (not monotonic seconds). Re-anchor
    each observation rather than assuming this domain ticks at exactly 1 MHz.
    get_latency includes native queued data; only our Python queue is extra.
    """
    now = time.monotonic_ns() // 1000000
    usec, latency, negative = ctypes.c_uint64(), ctypes.c_uint64(), ctypes.c_int()
    server_ok = pulse.lib.pa_stream_get_time(stream, ctypes.byref(usec)) >= 0
    latency_ok = (
        pulse.stream_get_latency(stream, ctypes.byref(latency), ctypes.byref(negative)) >= 0
    )
    delay = (
        (-(latency.value // 1000) if negative.value else latency.value // 1000) if latency_ok else 0
    )
    mono = max(0, now - delay - buffered * 1000 // 32000)
    if not server_ok or not latency_ok:
        return "monotonic", mono, mono
    server = max(0, usec.value // 1000 - delay - buffered * 1000 // 32000)
    owner.anchor = (server, mono)
    return "pulse", server, mono


def microphone_source() -> Any:
    """Reuse dictation's source with meeting-only proplist/100ms fragment ABI."""
    from astra_voice.worker.pulse_stream import PulseStreamSource

    class MeetingMicrophone(PulseStreamSource):
        anchor: tuple[int, int] | None = None

        def open(self, device: str | None = None, **kwargs: Any) -> None:
            self.anchor = None
            super().open(device, **kwargs)

        def sample_clock(self) -> tuple[str, int, int]:
            with self._ctx_lock:
                if self._pulse is None or self._stream is None:
                    raise ValueError("audio-failed")
                return _sample_clock(self._pulse, self._stream, len(self._buffer), self)

    return MeetingMicrophone(pulse_factory=_meeting_pulse_type())


class CapturePulseBackend:
    def __init__(self) -> None:
        self.control: NativePulse | None = None
        self.prepared_target: Target | None = None
        self.lock = threading.RLock()
        self.detector_stop_event = threading.Event()
        self.detector_thread: threading.Thread | None = None
        self.token = uuid.uuid4().hex

    def _control(self) -> NativePulse:
        if self.control is None:
            self.control = NativePulse()
        return self.control

    def prepare(self, far: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            return self._prepare(far)

    def _prepare(self, far: dict[str, Any]) -> dict[str, Any]:
        self.prepared_target = None
        p = self._control()
        available = True
        reason = None
        if far["kind"] == "app_stream":
            if not p.monitor_supported:
                available, reason = False, "monitor-stream-unsupported"
            else:
                target = p.resolve(int(far["sink_input"]))
                if target is None or target.serial != far["object_serial"]:
                    raise ValueError("target-mismatch")
                self.prepared_target = target
        return {
            "server_local": p.server_local,
            "server_kind": p.server_kind,
            "far_available": available,
            **({"far_reason": reason} if reason else {}),
        }

    def source(self, far: dict[str, Any]) -> MonitorStreamSource:
        pulse = NativePulse()
        source = MonitorStreamSource(pulse, far, self.token)
        source.target = self.prepared_target
        return source

    def detector_start(self) -> int:
        with self.lock:
            control = self._control()
            index = int(control.pulse.lib.pa_context_get_index(control.context))
            if index == 0xFFFFFFFF:
                raise ValueError("audio-failed")
            if self.detector_thread is None:
                self.detector_stop_event.clear()
                self.detector_thread = threading.Thread(
                    target=self._observe, name="meeting-pulse-events", daemon=True
                )
                self.detector_thread.start()
            return index

    def _observe(self) -> None:
        while not self.detector_stop_event.wait(0.1):
            try:
                with self.lock:
                    if self.control:
                        self.control.poll_events()  # subscription only; M8 decides calls
            except Exception:
                return

    def detector_stop(self) -> None:
        self.close()

    def close(self) -> None:
        self.detector_stop_event.set()
        thread, self.detector_thread = self.detector_thread, None
        if thread:
            thread.join()
        with self.lock:
            control, self.control = self.control, None
            if control is not None:
                control.close()
