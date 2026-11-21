"""Metadata-only libpulse adapter, one serialized _PulseAsync mainloop.

The observer owns its context and mainloop and uses NOAUTOSPAWN. Info callbacks
are pinned until their operations are cancelled and released. No audio API is
called.
"""

from __future__ import annotations

import ctypes as C
import math
import threading
import time
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, cast

from astra_voice.worker.meeting_detector import (
    INVALID_INDEX,
    MAX_PENDING,
    MAX_PROPERTY_BYTES,
    PROPERTY_KEYS,
    SUBSCRIPTION_MASK,
    Change,
    Direction,
    Facility,
    MetadataCapacity,
    MetadataDisconnected,
    MetadataOpenFailed,
    MetadataRequestTimeout,
    MetadataUnavailable,
    Notice,
    RawClient,
    RawStream,
    TimeRemaining,
    check_picture_size,
    project_properties,
)
from astra_voice.worker.monitor_stream import _ChannelMap, _InputInfo, _ServerInfo, _Volume
from astra_voice.worker.pulse_stream import (
    PA_CONTEXT_FAILED,
    PA_CONTEXT_NOAUTOSPAWN,
    PA_CONTEXT_READY,
    PA_CONTEXT_TERMINATED,
    SubscribeCallback,
    _PulseAsync,
)

OPERATION_SECONDS = 0.7
PUMP_SECONDS = 0.005
PUMP_ITERATIONS = 64


def _time_left(deadline: float, remaining: TimeRemaining | None) -> float:
    left = deadline - time.monotonic()
    if remaining is not None:
        left = min(left, remaining())
    if left <= 0:
        raise MetadataRequestTimeout()
    return left


# PulseAudio 16/17 public introspect.h prefixes. Source-output's volume/mute
# follow corked, unlike sink-input; never read name/driver string pointers.
class _SinkInput(C.Structure):
    _fields_ = [*_InputInfo._fields_, ("corked", C.c_int)]


class _SourceOutput(C.Structure):
    _fields_ = [
        ("index", C.c_uint32),
        ("name", C.c_char_p),
        ("owner", C.c_uint32),
        ("client", C.c_uint32),
        ("source", C.c_uint32),
        ("format", C.c_int),
        ("rate", C.c_uint32),
        ("channels", C.c_uint8),
        ("channel_map", _ChannelMap),
        ("buffer_usec", C.c_uint64),
        ("source_usec", C.c_uint64),
        ("resample", C.c_char_p),
        ("driver", C.c_char_p),
        ("proplist", C.c_void_p),
        ("corked", C.c_int),
        ("volume", _Volume),
        ("mute", C.c_int),
    ]


class _ClientInfo(C.Structure):
    _fields_ = [
        ("index", C.c_uint32),
        ("name", C.c_char_p),
        ("owner", C.c_uint32),
        ("driver", C.c_char_p),
        ("proplist", C.c_void_p),
    ]


def _declare(pulse: Any) -> None:
    for name, args, result in (
        ("pa_context_get_index", [C.c_void_p], C.c_uint32),
        ("pa_proplist_get", [C.c_void_p, C.c_char_p, C.c_void_p, C.c_void_p], C.c_int),
        *(
            (f"pa_context_get_{kind}_info_list", [C.c_void_p, C.c_void_p, C.c_void_p], C.c_void_p)
            for kind in ("client", "sink_input", "source_output")
        ),
        *(
            (
                f"pa_context_get_{kind}_info",
                [C.c_void_p, C.c_uint32, C.c_void_p, C.c_void_p],
                C.c_void_p,
            )
            for kind in ("client", "sink_input", "source_output")
        ),
    ):
        function = getattr(pulse.lib, name)
        function.argtypes, function.restype = args, result


class PulseMetadata:
    """Private notice queue and bounded by-index/list introspection."""

    def __init__(
        self,
        *,
        pulse: Any = None,
        server: bytes | None = None,
        subscribe: bool = True,
        deadline: float | None = None,
        context_name: bytes = b"astra-voice-meeting-metadata",
        binary_limit: int = MAX_PROPERTY_BYTES,
    ) -> None:
        self._lock = threading.RLock()
        self._deadline = deadline
        self._binary_limit = binary_limit
        self._pulse: Any = pulse if pulse is not None else _PulseAsync()
        self._context: int | None = None
        self._callback: Any = SubscribeCallback(self._notice) if subscribe else None
        self._notices: deque[Notice] = deque()
        self._overflow = False
        self._closing = False
        self._callback_detached = not subscribe
        self._disconnected = False
        self._unreferenced = False
        self._mainloop_freed = False
        self._callbacks: list[Any] = []
        self.own_index = INVALID_INDEX
        _declare(self._pulse)
        try:
            self._pulse.mainloop = self._pulse.mainloop_new()
            if not self._pulse.mainloop:
                raise MetadataUnavailable()
            api = self._pulse.mainloop_get_api(self._pulse.mainloop)
            if not api:
                raise MetadataUnavailable()
            self._context = self._pulse.context_new(api, context_name)
            connect_deadline = self._operation_deadline()
            _time_left(connect_deadline, None)
            if (
                not self._context
                or self._pulse.context_connect(self._context, server, PA_CONTEXT_NOAUTOSPAWN, None)
                < 0
            ):
                raise MetadataDisconnected()
            self._wait(
                lambda: self._pulse.context_get_state(self._context) == PA_CONTEXT_READY,
                connect_deadline,
                connecting=True,
            )
            self._check()
            self.own_index = int(self._pulse.lib.pa_context_get_index(self._context))
            if not 0 <= self.own_index < INVALID_INDEX:
                raise MetadataUnavailable()
            if subscribe:
                self._pulse.context_set_subscribe_callback(self._context, self._callback, None)
                self._subscribe(SUBSCRIPTION_MASK)
        except BaseException as error:
            try:
                self.close()
            except Exception:
                if isinstance(error, Exception):
                    raise MetadataOpenFailed(self) from None
                raise error from None
            raise

    def _operation_deadline(self) -> float:
        return (
            self._deadline if self._deadline is not None else time.monotonic() + OPERATION_SECONDS
        )

    def _check(self, *, connecting: bool = False) -> None:
        if self._closing or not self._context:
            raise MetadataDisconnected()
        state = self._pulse.context_get_state(self._context)
        if state in (PA_CONTEXT_FAILED, PA_CONTEXT_TERMINATED) or (
            not connecting and state != PA_CONTEXT_READY
        ):
            raise MetadataDisconnected()

    def _iterate(self, timeout_us: int) -> int:
        try:
            return cast(int, self._pulse.iterate(timeout_us))
        except Exception:
            raise MetadataDisconnected() from None

    def _wait(
        self,
        ready: Any,
        deadline: float,
        *,
        remaining: TimeRemaining | None = None,
        connecting: bool = False,
    ) -> None:
        while not ready():
            self._check(connecting=connecting)
            left = _time_left(deadline, remaining)
            micros = left * 1_000_000
            self._iterate(
                min(5000, int(micros) if self._deadline is not None else math.ceil(micros))
            )
        _time_left(deadline, remaining)

    @contextmanager
    def _request(self) -> Iterator[None]:
        # A reply or op-creation failure can coincide with a context state change.
        self._check()
        try:
            yield
        except MetadataUnavailable:
            self._check()
            raise
        except Exception:
            self._check()
            raise MetadataUnavailable() from None
        self._check()

    @contextmanager
    def _operation(self, op: int | None) -> Iterator[None]:
        if not op:
            raise MetadataUnavailable()
        try:
            yield
        finally:
            self._pulse.operation_cancel(op)
            self._pulse.operation_unref(op)

    def _subscribe(self, mask: int) -> None:
        success: int | None = None

        def receive(context: int, ok: int, unused: int) -> None:
            nonlocal success
            success = int(ok)

        callback = C.CFUNCTYPE(None, C.c_void_p, C.c_int, C.c_void_p)(receive)
        self._callbacks.append(callback)
        try:
            with self._request():
                deadline = self._operation_deadline()
                _time_left(deadline, None)
                op = self._pulse.lib.pa_context_subscribe(self._context, mask, callback, None)
                with self._operation(op):
                    self._wait(lambda: success is not None, deadline)
                    if success != 1:
                        raise MetadataUnavailable()
        finally:
            self._callbacks.remove(callback)

    def _notice(self, context: int, event: int, index: int, unused: int) -> None:
        if self._closing or context != self._context:
            return
        facility = {
            2: Facility.OUTPUT,
            3: Facility.MICROPHONE,
            5: Facility.CLIENT,
            7: Facility.SERVER,
        }.get(event & 15)
        change = {0: Change.NEW, 16: Change.CHANGE, 32: Change.REMOVE}.get(event & 48)
        if facility is None or change is None:
            return
        # Server callbacks use PA_INVALID_INDEX, unlike stream/client callbacks.
        index = 0 if facility is Facility.SERVER else int(index)
        if self._overflow:
            return
        if len(self._notices) >= MAX_PENDING:
            self._notices.clear()
            self._overflow = True
            return
        self._notices.append(Notice(facility, change, index))

    def _properties(self, proplist: int) -> tuple[dict[str, object], bool]:
        properties: dict[str, object] = {}
        valid = True
        if proplist:
            for name in PROPERTY_KEYS:
                pointer, size = C.c_void_p(), C.c_size_t()
                if (
                    self._pulse.lib.pa_proplist_get(
                        proplist, name.encode("ascii"), C.byref(pointer), C.byref(size)
                    )
                    < 0
                ):
                    continue
                # Bound BEFORE copying bytes; never strlen()/decode a raw name.
                limit = (
                    self._binary_limit
                    if name == "application.process.binary"
                    else MAX_PROPERTY_BYTES
                )
                if not pointer.value or not 0 < size.value <= limit + 1:
                    valid = False
                    continue
                raw = C.string_at(pointer, size.value)
                if not raw.endswith(b"\0"):
                    valid = False
                    continue
                try:
                    properties[name] = raw[:-1].decode("utf-8", "strict")
                except UnicodeError:
                    valid = False
        result, normalized = project_properties(properties)
        return result, valid and normalized

    def _query(
        self,
        kind: str,
        index: int | None,
        *,
        remaining: TimeRemaining | None,
        prior_rows: int = 0,
    ) -> tuple[Any, ...]:
        structure = {
            "client": _ClientInfo,
            "sink_input": _SinkInput,
            "source_output": _SourceOutput,
        }[kind]
        callback_type = C.CFUNCTYPE(None, C.c_void_p, C.POINTER(structure), C.c_int, C.c_void_p)
        rows: list[RawClient | RawStream] = []
        done = False
        failure: MetadataUnavailable | None = None

        def receive(context: int, info: Any, eol: int, unused: int) -> None:
            nonlocal done, failure
            if done:
                return
            try:
                if self._deadline is not None:
                    _time_left(self._deadline, remaining)
                if eol:
                    done = True
                    # PA_ERR_NOENTITY (5) is a normal removal during by-index lookup.
                    if eol < 0:
                        if index is not None and self._pulse.context_errno(self._context) == 5:
                            rows.clear()  # negative evidence overrides any partial row
                        else:
                            failure = MetadataUnavailable()
                elif info:
                    if index is None:
                        check_picture_size(prior_rows + len(rows) + 1)
                    elif rows:
                        raise MetadataUnavailable()
                    row = info.contents
                    props, valid = self._properties(row.proplist)
                    if kind == "client":
                        rows.append(RawClient(int(row.index), props, valid))
                    else:
                        rows.append(
                            RawStream(
                                int(row.index),
                                int(row.client),
                                Direction.OUTPUT if kind == "sink_input" else Direction.MICROPHONE,
                                props,
                                bool(row.corked),
                                bool(row.mute),
                                None,
                                valid,
                            )
                        )
            except MetadataUnavailable as error:
                failure = error
                done = True
            except Exception:
                failure = MetadataUnavailable()
                done = True

        callback = callback_type(receive)
        self._callbacks.append(callback)
        try:
            with self._request():
                method = getattr(
                    self._pulse.lib,
                    f"pa_context_get_{kind}_info" + ("_list" if index is None else ""),
                )
                deadline = self._operation_deadline()
                _time_left(deadline, remaining)
                op = (
                    method(self._context, callback, None)
                    if index is None
                    else method(self._context, index, callback, None)
                )
                with self._operation(op):
                    try:
                        self._wait(lambda: done, deadline, remaining=remaining)
                    except MetadataRequestTimeout:
                        if isinstance(failure, MetadataCapacity):
                            raise failure from None
                        raise
                    if failure is not None:
                        raise failure
            return tuple(rows)
        finally:
            self._callbacks.remove(callback)

    def snapshot(
        self, *, remaining: TimeRemaining | None = None
    ) -> tuple[tuple[RawClient, ...], tuple[RawStream, ...]]:
        with self._lock:
            clients = cast(tuple[RawClient, ...], self._query("client", None, remaining=remaining))
            outputs = self._query("sink_input", None, remaining=remaining, prior_rows=len(clients))
            inputs = self._query(
                "source_output", None, remaining=remaining, prior_rows=len(clients) + len(outputs)
            )
            return clients, cast(tuple[RawStream, ...], outputs + inputs)

    def server_cookie(self) -> int:
        callback_type = C.CFUNCTYPE(None, C.c_void_p, C.POINTER(_ServerInfo), C.c_void_p)
        cookie: int | None = None
        done = False

        @callback_type
        def receive(context: int, info: Any, unused: int) -> None:
            nonlocal cookie, done
            if info:
                cookie = int(info.contents.cookie)
            done = True

        with self._lock, self._request():
            deadline = self._operation_deadline()
            _time_left(deadline, None)
            method = self._pulse.lib.pa_context_get_server_info
            method.argtypes = [C.c_void_p, callback_type, C.c_void_p]
            method.restype = C.c_void_p
            with self._operation(method(self._context, receive, None)):
                self._wait(lambda: done, deadline)
            if cookie is None:
                raise MetadataUnavailable()
            return cookie

    def client(self, index: int, *, remaining: TimeRemaining | None = None) -> RawClient | None:
        with self._lock:
            rows = self._query("client", index, remaining=remaining)
            if len(rows) > 1 or (rows and rows[0].index != index):
                raise MetadataUnavailable()
            return cast(RawClient, rows[0]) if rows else None

    def stream(
        self, direction: Direction, index: int, *, remaining: TimeRemaining | None = None
    ) -> RawStream | None:
        with self._lock:
            rows = self._query(
                "sink_input" if direction is Direction.OUTPUT else "source_output",
                index,
                remaining=remaining,
            )
            if len(rows) > 1 or (rows and rows[0].index != index):
                raise MetadataUnavailable()
            return cast(RawStream, rows[0]) if rows else None

    def poll(self) -> tuple[tuple[Notice, ...], bool]:
        with self._lock:
            self._check()
            # A dispatch may consume only the packet header. Drain ready work,
            # bounded by time/count, without waiting or issuing info queries.
            deadline = time.monotonic() + PUMP_SECONDS
            for _ in range(PUMP_ITERATIONS):
                if not self._iterate(0) or time.monotonic() >= deadline:
                    break
            self._check()
            notices = tuple(self._notices)
            self._notices.clear()
            return notices, self.take_overflow()

    def take_overflow(self) -> bool:
        """Consume the recorded loss without dispatching or draining notices."""
        with self._lock:
            overflow = self._overflow
            self._overflow = False
            return overflow

    def has_pending_notices(self) -> bool:
        with self._lock:
            return bool(self._notices)

    def close(self) -> None:
        with self._lock:
            self._closing = True
            # The next attempt resumes at the first unfinished step. In particular,
            # never release a context or loop when callback detachment failed.
            if not self._callback_detached:
                if self._context:
                    self._pulse.context_set_subscribe_callback(self._context, None, None)
                self._callback_detached = True
                self._callback = None
            if not self._disconnected:
                if self._context:
                    self._pulse.context_disconnect(self._context)
                self._disconnected = True
            if not self._unreferenced:
                if self._context:
                    self._pulse.context_unref(self._context)
                self._unreferenced = True
                self._context = None
            if not self._mainloop_freed:
                if self._pulse.mainloop:
                    self._pulse.mainloop_free(self._pulse.mainloop)
                self._mainloop_freed = True
                self._pulse.mainloop = None
            self._notices.clear()
            self._overflow = False
            self._callbacks.clear()
