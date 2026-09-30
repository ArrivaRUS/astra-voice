"""Запись через собственный pa_stream; всем соединением владеет поток захвата."""

from __future__ import annotations

import argparse
import ctypes
import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any, cast

from astra_voice.worker.audio import (
    CHANNELS,
    CHUNK_BYTES,
    DEVICE_CHANGE_BUDGET_S,
    ERROR_BUSY,
    ERROR_FAILED,
    ERROR_NO_DEVICE,
    OPEN_DEADLINE_S,
    OPEN_RETRIES,
    OPEN_RETRY_MS,
    OPEN_TOTAL_DEADLINE_S,
    PA_ERR_ACCESS,
    PA_ERR_BUSY,
    PA_ERR_NOENTITY,
    PA_SAMPLE_S16LE,
    RATE,
    REASON_KILLED,
    REASON_MISMATCH,
    REASON_MOVED,
    REASON_REMOVED,
    U32_MAX,
    AudioApiUnavailable,
    AudioDevice,
    AudioError,
    DeviceChange,
    FreshDevices,
    ManagedSource,
    PulseSimpleSource,
    _OpenDeadline,
    _PaBufferAttr,
    _PaSampleSpec,
    classify_change,
    default_device,
    invalidate_device_cache,
    list_devices,
    select_device,
)

logger = logging.getLogger(__name__)

PA_CONTEXT_UNCONNECTED = 0
PA_CONTEXT_CONNECTING = 1
PA_CONTEXT_AUTHORIZING = 2
PA_CONTEXT_SETTING_NAME = 3
PA_CONTEXT_READY = 4
PA_CONTEXT_FAILED = 5
PA_CONTEXT_TERMINATED = 6
PA_STREAM_UNCONNECTED = 0
PA_STREAM_CREATING = 1
PA_STREAM_READY = 2
PA_STREAM_FAILED = 3
PA_STREAM_TERMINATED = 4
PA_OPERATION_RUNNING = 0
PA_OPERATION_DONE = 1
PA_OPERATION_CANCELLED = 2
PA_CONTEXT_NOAUTOSPAWN = 0x1
PA_STREAM_INTERPOLATE_TIMING = 0x2
PA_STREAM_AUTO_TIMING_UPDATE = 0x8
PA_STREAM_DONT_MOVE = 0x200
PA_STREAM_ADJUST_LATENCY = 0x2000
PA_SUBSCRIPTION_MASK_SOURCE = 0x2
PA_SUBSCRIPTION_MASK_SERVER = 0x80
PA_SUBSCRIPTION_EVENT_FACILITY_MASK = 0x0F
PA_SUBSCRIPTION_EVENT_SOURCE = 0x1
PA_SUBSCRIPTION_EVENT_SERVER = 0x7
PA_SUBSCRIPTION_EVENT_TYPE_MASK = 0x30
PA_SUBSCRIPTION_EVENT_NEW = 0
PA_SUBSCRIPTION_EVENT_CHANGE = 0x10
PA_SUBSCRIPTION_EVENT_REMOVE = 0x20
PA_ERR_KILLED = 12
PA_INVALID_INDEX = U32_MAX
SIZE_MAX = ctypes.c_size_t(-1).value
POLL_US = 50_000
KILLED_WAIT_S = 0.250
SERVER_WAIT_S = 0.200
# В очередь попадают только REMOVE источников и moved; SERVER CHANGE хранится флагом.
EVENT_QUEUE_LIMIT = 64

SubscribeCallback = ctypes.CFUNCTYPE(
    None, ctypes.c_void_p, ctypes.c_int, ctypes.c_uint32, ctypes.c_void_p
)
MovedCallback = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p)


class _PulseFailure(RuntimeError):
    """Нативный вызов завершился ошибкой; пользовательское сообщение задаёт источник."""


class _OpenCancelled(Exception):
    """Владелец снял running во время подготовки."""


class _PulseAsync:
    """Явный ABI libpulse и небольшие помощники однопоточного цикла."""

    def __init__(self) -> None:
        self.mainloop: int | None = None
        try:
            self.lib = ctypes.CDLL("libpulse.so.0", use_errno=True)
            self._declare()
        except (OSError, AttributeError) as exc:
            # AttributeError — в библиотеке нет нужного символа (старая или чужая сборка).
            raise AudioApiUnavailable(ERROR_FAILED, "Звуковая подсистема недоступна.") from exc

    def _declare(self) -> None:
        """Объявляет argtypes/restype; отсутствие символа даёт AttributeError."""
        self.lib.pa_mainloop_new.argtypes = []
        self.lib.pa_mainloop_new.restype = ctypes.c_void_p
        self.lib.pa_mainloop_free.argtypes = [ctypes.c_void_p]
        self.lib.pa_mainloop_free.restype = None
        self.lib.pa_mainloop_get_api.argtypes = [ctypes.c_void_p]
        self.lib.pa_mainloop_get_api.restype = ctypes.c_void_p
        self.lib.pa_mainloop_prepare.argtypes = [ctypes.c_void_p, ctypes.c_int]
        self.lib.pa_mainloop_prepare.restype = ctypes.c_int
        self.lib.pa_mainloop_poll.argtypes = [ctypes.c_void_p]
        self.lib.pa_mainloop_poll.restype = ctypes.c_int
        self.lib.pa_mainloop_dispatch.argtypes = [ctypes.c_void_p]
        self.lib.pa_mainloop_dispatch.restype = ctypes.c_int
        self.lib.pa_context_new.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        self.lib.pa_context_new.restype = ctypes.c_void_p
        self.lib.pa_context_connect.argtypes = [
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_void_p,
        ]
        self.lib.pa_context_connect.restype = ctypes.c_int
        self.lib.pa_context_get_state.argtypes = [ctypes.c_void_p]
        self.lib.pa_context_get_state.restype = ctypes.c_int
        self.lib.pa_context_errno.argtypes = [ctypes.c_void_p]
        self.lib.pa_context_errno.restype = ctypes.c_int
        self.lib.pa_context_set_subscribe_callback.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        self.lib.pa_context_set_subscribe_callback.restype = None
        self.lib.pa_context_subscribe.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        self.lib.pa_context_subscribe.restype = ctypes.c_void_p
        self.lib.pa_context_disconnect.argtypes = [ctypes.c_void_p]
        self.lib.pa_context_disconnect.restype = None
        self.lib.pa_context_unref.argtypes = [ctypes.c_void_p]
        self.lib.pa_context_unref.restype = None
        self.lib.pa_stream_new.argtypes = [
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        self.lib.pa_stream_new.restype = ctypes.c_void_p
        self.lib.pa_stream_set_moved_callback.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        self.lib.pa_stream_set_moved_callback.restype = None
        self.lib.pa_stream_connect_record.argtypes = [
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.c_void_p,
            ctypes.c_uint32,
        ]
        self.lib.pa_stream_connect_record.restype = ctypes.c_int
        self.lib.pa_stream_get_state.argtypes = [ctypes.c_void_p]
        self.lib.pa_stream_get_state.restype = ctypes.c_int
        self.lib.pa_stream_peek.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
        self.lib.pa_stream_peek.restype = ctypes.c_int
        self.lib.pa_stream_drop.argtypes = [ctypes.c_void_p]
        self.lib.pa_stream_drop.restype = ctypes.c_int
        self.lib.pa_stream_readable_size.argtypes = [ctypes.c_void_p]
        self.lib.pa_stream_readable_size.restype = ctypes.c_size_t
        self.lib.pa_stream_get_device_index.argtypes = [ctypes.c_void_p]
        self.lib.pa_stream_get_device_index.restype = ctypes.c_uint32
        self.lib.pa_stream_get_device_name.argtypes = [ctypes.c_void_p]
        self.lib.pa_stream_get_device_name.restype = ctypes.c_char_p
        self.lib.pa_stream_flush.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
        self.lib.pa_stream_flush.restype = ctypes.c_void_p
        self.lib.pa_stream_get_latency.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        self.lib.pa_stream_get_latency.restype = ctypes.c_int
        self.lib.pa_stream_disconnect.argtypes = [ctypes.c_void_p]
        self.lib.pa_stream_disconnect.restype = ctypes.c_int
        self.lib.pa_stream_unref.argtypes = [ctypes.c_void_p]
        self.lib.pa_stream_unref.restype = None
        self.lib.pa_operation_get_state.argtypes = [ctypes.c_void_p]
        self.lib.pa_operation_get_state.restype = ctypes.c_int
        self.lib.pa_operation_unref.argtypes = [ctypes.c_void_p]
        self.lib.pa_operation_unref.restype = None
        self.lib.pa_operation_cancel.argtypes = [ctypes.c_void_p]
        self.lib.pa_operation_cancel.restype = None
        self.lib.pa_strerror.argtypes = [ctypes.c_int]
        self.lib.pa_strerror.restype = ctypes.c_char_p

    def mainloop_new(self) -> int | None:
        return cast(int | None, self.lib.pa_mainloop_new())

    def mainloop_free(self, m: int) -> None:
        self.lib.pa_mainloop_free(m)

    def mainloop_get_api(self, m: int) -> int | None:
        return cast(int | None, self.lib.pa_mainloop_get_api(m))

    def mainloop_prepare(self, m: int, timeout_us: int) -> int:
        return cast(int, self.lib.pa_mainloop_prepare(m, timeout_us))

    def mainloop_poll(self, m: int) -> int:
        return cast(int, self.lib.pa_mainloop_poll(m))

    def mainloop_dispatch(self, m: int) -> int:
        return cast(int, self.lib.pa_mainloop_dispatch(m))

    def context_new(self, api: int, name: bytes) -> int | None:
        return cast(int | None, self.lib.pa_context_new(api, name))

    def context_connect(self, ctx: int, server: bytes | None, flags: int, spawn: None) -> int:
        return cast(int, self.lib.pa_context_connect(ctx, server, flags, spawn))

    def context_get_state(self, ctx: int) -> int:
        return cast(int, self.lib.pa_context_get_state(ctx))

    def context_errno(self, ctx: int) -> int:
        return cast(int, self.lib.pa_context_errno(ctx))

    def context_set_subscribe_callback(self, ctx: int, callback: Any, userdata: None) -> None:
        self.lib.pa_context_set_subscribe_callback(ctx, callback, userdata)

    def context_subscribe(self, ctx: int, mask: int, callback: None, userdata: None) -> int | None:
        return cast(int | None, self.lib.pa_context_subscribe(ctx, mask, callback, userdata))

    def context_disconnect(self, ctx: int) -> None:
        self.lib.pa_context_disconnect(ctx)

    def context_unref(self, ctx: int) -> None:
        self.lib.pa_context_unref(ctx)

    def stream_new(self, ctx: int, name: bytes, spec: Any, channel_map: None) -> int | None:
        return cast(int | None, self.lib.pa_stream_new(ctx, name, spec, channel_map))

    def stream_set_moved_callback(self, stream: int, callback: Any, userdata: None) -> None:
        self.lib.pa_stream_set_moved_callback(stream, callback, userdata)

    def stream_connect_record(self, stream: int, name: bytes, attr: Any, flags: int) -> int:
        return cast(int, self.lib.pa_stream_connect_record(stream, name, attr, flags))

    def stream_get_state(self, stream: int) -> int:
        return cast(int, self.lib.pa_stream_get_state(stream))

    def stream_peek(self, stream: int, data: Any, size: Any) -> int:
        return cast(int, self.lib.pa_stream_peek(stream, data, size))

    def stream_drop(self, stream: int) -> int:
        return cast(int, self.lib.pa_stream_drop(stream))

    def stream_readable_size(self, stream: int) -> int:
        return cast(int, self.lib.pa_stream_readable_size(stream))

    def stream_get_device_index(self, stream: int) -> int:
        return cast(int, self.lib.pa_stream_get_device_index(stream))

    def stream_get_device_name(self, stream: int) -> bytes | None:
        return cast(bytes | None, self.lib.pa_stream_get_device_name(stream))

    def stream_flush(self, stream: int, callback: None, userdata: None) -> int | None:
        return cast(int | None, self.lib.pa_stream_flush(stream, callback, userdata))

    def stream_get_latency(self, stream: int, usec: Any, negative: Any) -> int:
        return cast(int, self.lib.pa_stream_get_latency(stream, usec, negative))

    def stream_disconnect(self, stream: int) -> int:
        return cast(int, self.lib.pa_stream_disconnect(stream))

    def stream_unref(self, stream: int) -> None:
        self.lib.pa_stream_unref(stream)

    def operation_get_state(self, op: int) -> int:
        return cast(int, self.lib.pa_operation_get_state(op))

    def operation_unref(self, op: int) -> None:
        self.lib.pa_operation_unref(op)

    def operation_cancel(self, op: int) -> None:
        self.lib.pa_operation_cancel(op)

    def strerror(self, error: int) -> bytes | None:
        return cast(bytes | None, self.lib.pa_strerror(error))

    def iterate(self, timeout_us: int) -> None:
        """Одна итерация; timeout у prepare измеряется в микросекундах."""
        assert self.mainloop is not None
        if self.mainloop_prepare(self.mainloop, timeout_us) < 0:
            raise _PulseFailure
        if self.mainloop_poll(self.mainloop) < 0:
            raise _PulseFailure
        if self.mainloop_dispatch(self.mainloop) < 0:
            raise _PulseFailure

    def peek(self, stream: int) -> bytes | int | None:
        """Копирует данные до drop; int означает дыру, None — пустой буфер."""
        data = ctypes.c_void_p()
        size = ctypes.c_size_t()
        if self.stream_peek(stream, ctypes.byref(data), ctypes.byref(size)) < 0:
            raise _PulseFailure
        if size.value == 0:
            return None
        result = ctypes.string_at(data, size.value) if data.value else size.value
        if self.stream_drop(stream) < 0:
            raise _PulseFailure
        return result

    def wait_op(self, op: int, deadline: _OpenDeadline) -> None:
        """Ждёт операцию в пределах бюджета; отменяет незавершённую и всегда unref."""
        try:
            while True:
                state = self.operation_get_state(op)
                if state == PA_OPERATION_DONE:
                    return
                if state == PA_OPERATION_CANCELLED:
                    raise _PulseFailure
                self.iterate(max(1, int(deadline.remaining(POLL_US / 1_000_000) * 1_000_000)))
        except (AudioError, _PulseFailure):
            if self.operation_get_state(op) == PA_OPERATION_RUNNING:
                self.operation_cancel(op)
            raise
        finally:
            self.operation_unref(op)


@dataclass(frozen=True)
class _Event:
    """Снимок факта в колбэке, без принятия решения о смене."""

    moved: bool
    event: int
    index: int
    name: str | None
    cutoff: int


class PulseStreamSource:
    """Управляемый источник PCM16 без собственных потоков и фоновых соединений."""

    def __init__(
        self,
        *,
        pulse_factory: Callable[[], _PulseAsync] | None = None,
        devices: Callable[[], list[AudioDevice]] | None = None,
        default: Callable[..., AudioDevice] = default_device,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._factory = pulse_factory or _PulseAsync
        self._devices = devices
        self._default = default
        self._clock = clock
        self._sleep = sleep
        self._pulse: _PulseAsync | None = None
        self._mainloop: int | None = None
        self._context: int | None = None
        self._stream: int | None = None
        self._running: threading.Event | None = None
        self._events: deque[_Event] = deque()
        # Очередь переполнилась (или REMOVE не приписать): сверяем устройство и список.
        self._overflow = False
        self._overflow_cutoff = 0
        self._subscribe_callback = SubscribeCallback(self._on_subscribe)
        self._moved_callback = MovedCallback(self._on_moved)
        self._buffer = bytearray()
        self._received = 0
        self._delivered = 0
        self._cutoff: int | None = None
        self._baseline_known = False
        self._index = PA_INVALID_INDEX
        self._default_mode = False
        self._server_changed = False
        self._server_waited = False
        self._failed = False
        self._opened = False
        self.device_label: str | None = None
        self._selected_device: AudioDevice | None = None
        self._device_name: str | None = None
        self.device_change: DeviceChange | None = None
        self._probe_logging = False
        self._trace_states: dict[str, int] = {}
        self._trace_device: tuple[int, str | None] = (PA_INVALID_INDEX, None)

    def _trace_state(self, object_name: str, state: int) -> None:
        """Отмечает наблюдаемые переходы, не добавляя вызовов libpulse."""
        if not self._probe_logging:
            return
        previous = self._trace_states.get(object_name)
        if previous != state:
            logger.debug("%s state: %s -> %s", object_name, previous, state)
            self._trace_states[object_name] = state

    @property
    def is_open(self) -> bool:
        return self._opened

    @property
    def ended(self) -> bool:
        return False

    @property
    def live(self) -> bool:
        return True

    @property
    def selected_device(self) -> AudioDevice | None:
        return self._selected_device

    @property
    def device_name(self) -> str | None:
        return self._device_name

    def _check_open(self, deadline: _OpenDeadline) -> None:
        if self._running is not None and not self._running.is_set():
            raise _OpenCancelled
        deadline.remaining(OPEN_TOTAL_DEADLINE_S)

    def _open_error(self) -> AudioError:
        error = (
            self._pulse.context_errno(self._context)
            if self._pulse is not None and self._context is not None
            else 0
        )
        if error in (PA_ERR_BUSY, PA_ERR_ACCESS):
            return AudioError(ERROR_BUSY, "Микрофон занят другой программой.")
        if error == PA_ERR_NOENTITY:
            return AudioError(
                ERROR_NO_DEVICE,
                "Выбранный микрофон недоступен. Выберите устройство записи в настройках.",
            )
        return AudioError(ERROR_FAILED, "Не удалось включить запись звука.")

    def open(
        self,
        device: str | None,
        *,
        deadline: _OpenDeadline | None = None,
        running: threading.Event | None = None,
    ) -> None:
        """Выбирает устройство до libpulse, подписывается и создаёт закреплённый поток."""
        self.close()
        self._running = running
        budget = deadline or _OpenDeadline(self._clock)
        try:
            self._check_open(budget)

            def devices_fn(*, deadline: _OpenDeadline | None) -> list[AudioDevice]:
                return list_devices(deadline=deadline) if self._devices is None else self._devices()

            selected, _ = select_device(
                device,
                devices_fn=devices_fn,
                default_fn=self._default,
                clock=self._clock,
                deadline=budget,
                use_cache=self._devices is None,
            )
            self._check_open(budget)
            self._pulse = self._factory()
            for attempt in range(OPEN_RETRIES):
                self._check_open(budget)
                self._selected_device = selected
                self._device_name = selected.name
                self.device_label = selected.label
                self._default_mode = device is None
                try:
                    self._connect(selected, budget)
                    logger.info("Источник записи открыт: %s", selected.label)
                    logger.debug("Открыт pa_stream для %s", selected.name)
                    return
                except _PulseFailure:
                    error = self._open_error()
                except AudioError as exc:
                    error = exc
                self._release()
                invalidate_device_cache()
                if attempt + 1 == OPEN_RETRIES:
                    raise error
                # Сохраняем паузу повтора, но замечаем отмену и внутри неё.
                pause = OPEN_RETRY_MS / 1000
                while pause > 0:
                    self._check_open(budget)
                    duration = budget.remaining(min(0.050, pause))
                    self._sleep(duration)
                    pause -= duration
        except _OpenCancelled:
            self.close()
        except BaseException as exc:
            self.close()
            # Отказ API не говорит о смене устройств: кэш нужен запасному simple.
            if isinstance(exc, AudioError) and not isinstance(exc, AudioApiUnavailable):
                invalidate_device_cache()
            raise

    def _connect(self, selected: AudioDevice, budget: _OpenDeadline) -> None:
        assert self._pulse is not None
        pulse = self._pulse
        attempt = _OpenDeadline(self._clock, budget.remaining(OPEN_DEADLINE_S))
        self._mainloop = pulse.mainloop_new()
        if not self._mainloop:
            raise _PulseFailure
        pulse.mainloop = self._mainloop
        api = pulse.mainloop_get_api(self._mainloop)
        if not api:
            raise _PulseFailure
        self._context = pulse.context_new(api, b"astra-voice")
        if not self._context:
            raise _PulseFailure
        pulse.context_set_subscribe_callback(self._context, self._subscribe_callback, None)
        if pulse.context_connect(self._context, None, PA_CONTEXT_NOAUTOSPAWN, None) < 0:
            raise _PulseFailure
        self._wait_ready(attempt, stream=False)
        op = pulse.context_subscribe(
            self._context, PA_SUBSCRIPTION_MASK_SOURCE | PA_SUBSCRIPTION_MASK_SERVER, None, None
        )
        if not op:
            raise _PulseFailure
        pulse.operation_unref(op)
        spec = _PaSampleSpec(PA_SAMPLE_S16LE, RATE, CHANNELS)
        self._stream = pulse.stream_new(self._context, b"dictation", ctypes.byref(spec), None)
        if not self._stream:
            raise _PulseFailure
        pulse.stream_set_moved_callback(self._stream, self._moved_callback, None)
        attr = _PaBufferAttr(U32_MAX, U32_MAX, U32_MAX, U32_MAX, CHUNK_BYTES)
        flags = (
            PA_STREAM_INTERPOLATE_TIMING
            | PA_STREAM_ADJUST_LATENCY
            | PA_STREAM_AUTO_TIMING_UPDATE
            | PA_STREAM_DONT_MOVE
        )
        if (
            pulse.stream_connect_record(
                self._stream, selected.name.encode(), ctypes.byref(attr), flags
            )
            < 0
        ):
            raise _PulseFailure
        self._wait_ready(attempt, stream=True)
        idx = pulse.stream_get_device_index(self._stream)
        name = self._stream_name()
        self._baseline_known = idx != PA_INVALID_INDEX
        if self._baseline_known:
            self._index = idx
        else:
            # Запасной индекс годится, только если он из той же нумерации, что события.
            self._index = selected.index if selected.index_exact else PA_INVALID_INDEX
        self._trace_device = (idx, name)
        if self._probe_logging:
            logger.debug(
                "Базовая линия READY: index=%s name=%r known=%s comparison_index=%s",
                idx,
                name,
                self._baseline_known,
                self._index,
            )
        self._opened = True
        if name is not None and name != selected.name:
            self._change(REASON_MISMATCH, 0, name)

    def _wait_ready(self, deadline: _OpenDeadline, *, stream: bool) -> None:
        assert self._pulse is not None and self._context is not None
        while True:
            self._check_open(deadline)
            context_state = self._pulse.context_get_state(self._context)
            self._trace_state("context", context_state)
            if context_state in (PA_CONTEXT_FAILED, PA_CONTEXT_TERMINATED):
                raise _PulseFailure
            if stream:
                assert self._stream is not None
                state = self._pulse.stream_get_state(self._stream)
                self._trace_state("stream", state)
                if state in (PA_STREAM_FAILED, PA_STREAM_TERMINATED):
                    raise _PulseFailure
                if state == PA_STREAM_READY:
                    return
            elif context_state == PA_CONTEXT_READY:
                return
            self._pulse.iterate(max(1, int(deadline.remaining(0.050) * 1_000_000)))

    def _stream_name(self) -> str | None:
        assert self._pulse is not None and self._stream is not None
        name = self._pulse.stream_get_device_name(self._stream)
        return name.decode("utf-8", errors="replace") if name is not None else None

    def _event_cutoff(self) -> int:
        size = 0
        if self._pulse is not None and self._stream is not None:
            size = self._pulse.stream_readable_size(self._stream)
            if size == SIZE_MAX or self._pulse.stream_get_state(self._stream) == PA_STREAM_FAILED:
                size = 0
        return self._received + size

    def _enqueue(self, event: _Event) -> None:
        """Ограниченная очередь не вытесняет старые факты: лишнее отмечается переполнением."""
        if len(self._events) < EVENT_QUEUE_LIMIT:
            self._events.append(event)
        elif not self._overflow:
            self._overflow = True
            self._overflow_cutoff = event.cutoff

    def _on_subscribe(self, context: int, event: int, idx: int, userdata: int) -> None:
        try:
            facility = event & PA_SUBSCRIPTION_EVENT_FACILITY_MASK
            kind = event & PA_SUBSCRIPTION_EVENT_TYPE_MASK
            if self._probe_logging:
                own_index = self._index
                logger.debug(
                    "Подписка: object=%s kind=%s index=%s %s",
                    {
                        PA_SUBSCRIPTION_EVENT_SOURCE: "SOURCE",
                        PA_SUBSCRIPTION_EVENT_SERVER: "SERVER",
                    }.get(facility, str(facility)),
                    {0: "NEW", 0x10: "CHANGE", 0x20: "REMOVE"}.get(kind, str(kind)),
                    idx,
                    "наш"
                    if facility == PA_SUBSCRIPTION_EVENT_SOURCE and idx == own_index
                    else "чужой",
                )
            if facility == PA_SUBSCRIPTION_EVENT_SERVER and kind == PA_SUBSCRIPTION_EVENT_CHANGE:
                self._server_changed = True
            elif facility == PA_SUBSCRIPTION_EVENT_SOURCE and kind == PA_SUBSCRIPTION_EVENT_REMOVE:
                # NEW/CHANGE источников не нужны: их шторм не должен вытеснять наш REMOVE.
                self._enqueue(_Event(False, event, idx, None, self._event_cutoff()))
        except Exception:
            logger.debug("Не удалось сохранить событие подписки pa_stream", exc_info=True)

    def _on_moved(self, stream: int, userdata: int) -> None:
        try:
            assert self._pulse is not None and self._stream is not None
            idx = self._pulse.stream_get_device_index(self._stream)
            name = self._stream_name()
            if self._probe_logging:
                logger.debug(
                    "moved: old_index=%s old_name=%r new_index=%s new_name=%r",
                    *self._trace_device,
                    idx,
                    name,
                )
                self._trace_device = (idx, name)
            self._enqueue(_Event(True, 0, idx, name, self._event_cutoff()))
        except Exception:
            logger.debug("Не удалось сохранить событие moved pa_stream", exc_info=True)

    def _change(
        self,
        reason: str,
        cutoff: int,
        name: str | None = None,
        *,
        fresh: FreshDevices | None = None,
    ) -> None:
        if self.device_change is not None:
            return
        self._cutoff = cutoff
        self.device_change = DeviceChange(
            reason,
            self.selected_device,
            self._default_mode,
            moved_to_name=name,
            server_changed=self._server_changed,
            fresh=fresh,
        )
        logger.info("Смена источника записи: %s", reason)
        if name is not None:
            logger.debug("Новое имя источника: %s", name)

    def _process_moved(self, event: _Event) -> None:
        if self._baseline_known:
            if event.index != self._index:
                self._change(REASON_MOVED, event.cutoff, event.name)
        elif event.name is not None:
            if event.name != self.device_name:
                self._change(REASON_MOVED, event.cutoff, event.name)
            elif event.index != PA_INVALID_INDEX:
                self._index = event.index
                self._baseline_known = True

    def _process_events(self, *, killed: bool = False) -> None:
        while self._events:
            event = self._events.popleft()
            if event.moved:
                self._process_moved(event)
            elif self._index != PA_INVALID_INDEX and event.index == self._index:
                self._change(REASON_KILLED if killed else REASON_REMOVED, event.cutoff)
            elif self._index == PA_INVALID_INDEX and not self._overflow:
                # Свой индекс неизвестен: REMOVE не приписать, проверяем по имени.
                self._overflow = True
                self._overflow_cutoff = event.cutoff
        if self._overflow and self.device_change is None and not killed and self._keep_running():
            self._overflow = False
            self._recheck(self._overflow_cutoff)
        if (
            self._server_changed
            and self.device_change is not None
            and not self.device_change.server_changed
        ):
            self.device_change = replace(self.device_change, server_changed=True)

    def _recheck(self, cutoff: int) -> None:
        """Часть фактов потеряна: сверяем устройство потока и ищем наше имя в списке."""
        assert self._pulse is not None and self._stream is not None
        logger.debug("События pa_stream потеряны или не приписаны: сверка устройства")
        self._process_moved(
            _Event(
                True,
                0,
                self._pulse.stream_get_device_index(self._stream),
                self._stream_name(),
                cutoff,
            )
        )
        if self.device_change is None:
            # Сбой списка при живом потоке — не повод обрывать запись.
            self._check_list(REASON_REMOVED, cutoff, error_missing=False)

    def _check_list(self, reason: str, cutoff: int, *, error_missing: bool) -> bool:
        """Ищет наше имя в свежем списке; при пропаже фиксирует смену вместе со списком.

        Список и умолчание читаются в одном бюджете и передаются дальше в DeviceChange,
        чтобы классификация не спрашивала службу второй раз.
        """
        budget = _OpenDeadline(self._clock, DEVICE_CHANGE_BUDGET_S)
        devices: list[AudioDevice] | None
        try:
            devices = list_devices(deadline=budget) if self._devices is None else self._devices()
            budget.remaining(DEVICE_CHANGE_BUDGET_S)
        except AudioError:
            devices = None
        if devices is None:
            missing = error_missing
        else:
            missing = all(item.name != self.device_name for item in devices)
        if not missing:
            return False
        default: AudioDevice | None = None
        if devices is not None and self._default_mode:
            try:
                default = self._default(devices, deadline=budget)
                budget.remaining(DEVICE_CHANGE_BUDGET_S)
            except AudioError:
                default = None
        fresh = FreshDevices(tuple(devices) if devices is not None else None, default)
        self._change(reason, cutoff, fresh=fresh)
        return True

    def _keep_running(self) -> bool:
        return self._running is None or self._running.is_set()

    def _context_ok(self) -> bool:
        assert self._pulse is not None and self._context is not None
        state = self._pulse.context_get_state(self._context)
        self._trace_state("context", state)
        return state not in (
            PA_CONTEXT_FAILED,
            PA_CONTEXT_TERMINATED,
        )

    def _check_state(self) -> bool:
        """Отделяет сбой службы и убийство из микшера от пропажи устройства."""
        assert self._pulse is not None and self._stream is not None
        if not self._context_ok():
            return False
        state = self._pulse.stream_get_state(self._stream)
        self._trace_state("stream", state)
        assert self._context is not None
        killed = (
            state == PA_STREAM_FAILED and self._pulse.context_errno(self._context) == PA_ERR_KILLED
        )
        if state in (PA_STREAM_FAILED, PA_STREAM_TERMINATED) and not killed:
            return False
        self._process_events(killed=killed)
        if not killed or self.device_change is not None:
            return True
        end = self._clock() + KILLED_WAIT_S
        while self._clock() < end and self._keep_running():
            self._pulse.iterate(max(1, min(POLL_US, int((end - self._clock()) * 1_000_000))))
            if not self._context_ok():
                return False
            self._process_events(killed=True)
            if self.device_change is not None:
                return True
        if not self._keep_running():
            # Запись уже остановлена или отменена: классифицировать нечего и некому.
            return False
        # Поток мёртв: сбой списка считаем пропажей, иначе запись оборвётся без объяснения.
        return self._check_list(REASON_KILLED, self._received, error_missing=True)

    def _finish_change(self) -> None:
        """Даёт серверу короткое окно для обновления умолчания после пропажи."""
        assert self.device_change is not None and self._pulse is not None
        if (
            self._default_mode
            and not self._server_changed
            and not self._server_waited
            and self.device_change.fresh is None
            and self.device_change.reason in (REASON_REMOVED, REASON_KILLED)
        ):
            self._server_waited = True
            end = self._clock() + SERVER_WAIT_S
            while not self._server_changed and self._clock() < end and self._keep_running():
                self._pulse.iterate(max(1, min(POLL_US, int((end - self._clock()) * 1_000_000))))
                if not self._context_ok():
                    raise _PulseFailure
                self._process_events()
        self._buffer.clear()

    def read_chunk(self) -> bytes | None:
        """Возвращает ровную порцию, пустой опрос или окончание с фактом смены."""
        if not self.is_open or self._failed:
            return None
        assert self._pulse is not None and self._stream is not None
        try:
            if not self._check_state():
                raise _PulseFailure
            if len(self._buffer) < CHUNK_BYTES:
                if self.device_change is None:
                    self._pulse.iterate(POLL_US)
                    if not self._check_state():
                        raise _PulseFailure
                if self._pulse.stream_get_state(self._stream) == PA_STREAM_READY:
                    while self._cutoff is None or self._received < self._cutoff:
                        fragment = self._pulse.peek(self._stream)
                        if fragment is None:
                            break
                        if isinstance(fragment, bytes):
                            allowed = (
                                len(fragment)
                                if self._cutoff is None
                                else self._cutoff - self._received
                            )
                            self._buffer.extend(fragment[:allowed])
                            self._received += len(fragment)
            available = len(self._buffer)
            if self._cutoff is not None:
                available = min(available, self._cutoff - self._delivered)
            if available >= CHUNK_BYTES:
                chunk = bytes(self._buffer[:CHUNK_BYTES])
                del self._buffer[:CHUNK_BYTES]
                self._delivered += CHUNK_BYTES
                return chunk
            if self.device_change is not None:
                self._finish_change()
                return None
            return b""
        except _PulseFailure:
            self._failed = True
            self.device_change = None
            self._buffer.clear()
            return None

    def flush(self) -> None:
        """Сбрасывает серверный и внутренний буферы в пределах срока подготовки."""
        if not self.is_open:
            return
        assert self._pulse is not None and self._stream is not None
        try:
            op = self._pulse.stream_flush(self._stream, None, None)
            if not op:
                raise _PulseFailure
            self._pulse.wait_op(op, _OpenDeadline(self._clock, OPEN_DEADLINE_S))
        except (AudioError, _PulseFailure) as exc:
            raise AudioError(ERROR_FAILED, "Не удалось подготовить запись звука.") from exc
        self._buffer.clear()
        self._received = self._delivered = 0
        # Факты, поступившие во время flush, сохраняем, но сброшенного PCM уже нет.
        self._events = deque(replace(event, cutoff=0) for event in self._events)
        self._overflow_cutoff = 0
        if self._cutoff is not None:
            self._cutoff = 0

    def latency_us(self) -> int | None:
        """Отрицательная задержка округляется к нулю; ошибка не является значением."""
        if not self.is_open:
            return None
        assert self._pulse is not None and self._stream is not None
        usec, negative = ctypes.c_uint64(), ctypes.c_int()
        if (
            self._pulse.stream_get_latency(self._stream, ctypes.byref(usec), ctypes.byref(negative))
            < 0
        ):
            return None
        return 0 if negative.value else usec.value

    def _release(self) -> None:
        """Снимает колбэки до unref; используется и между попытками открытия."""
        stream, self._stream = self._stream, None
        context, self._context = self._context, None
        mainloop, self._mainloop = self._mainloop, None
        self._opened = False
        if self._pulse is not None:
            if stream is not None:
                self._pulse.stream_set_moved_callback(stream, None, None)
                self._pulse.stream_disconnect(stream)
                self._pulse.stream_unref(stream)
            if context is not None:
                self._pulse.context_set_subscribe_callback(context, None, None)
                self._pulse.context_disconnect(context)
                self._pulse.context_unref(context)
            if mainloop is not None:
                self._pulse.mainloop_free(mainloop)
            self._pulse.mainloop = None
        self._events.clear()
        self._overflow = False
        self._overflow_cutoff = 0
        self._buffer.clear()
        self._received = self._delivered = 0
        self._cutoff = None
        self._index = PA_INVALID_INDEX
        self._baseline_known = False
        self._trace_states.clear()
        self._trace_device = (PA_INVALID_INDEX, None)
        self._server_changed = self._server_waited = self._failed = False
        self.device_change = None

    def close(self) -> None:
        """Идемпотентно освобождает соединение и забывает выбор устройства."""
        self._release()
        self._pulse = None
        self._running = None
        self._device_name = None
        self._selected_device = None
        self.device_label = None
        self._default_mode = False


class StreamWithFallback:
    """pa_stream по умолчанию; один раз уходит на simple, если нет libpulse или символа.

    Ошибки микрофона (занят, нет устройства, сбой службы) не маскируются:
    откат только на AudioApiUnavailable, остальное идёт владельцу как есть.
    """

    def __init__(
        self,
        primary: ManagedSource | None = None,
        fallback: Callable[[], ManagedSource] = PulseSimpleSource,
        *,
        on_fallback: Callable[[], None] | None = None,
    ) -> None:
        self._active: ManagedSource = primary if primary is not None else PulseStreamSource()
        self._fallback: Callable[[], ManagedSource] | None = fallback
        # Сообщаем об откате только после первого успешного открытия simple:
        # без libpulse.so.0 simple тоже не откроется, и «simple работает» было бы неправдой.
        self._on_fallback = on_fallback

    def open(
        self,
        device: str | None,
        *,
        deadline: _OpenDeadline | None = None,
        running: threading.Event | None = None,
    ) -> None:
        try:
            self._active.open(device, deadline=deadline, running=running)
        except AudioApiUnavailable:
            fallback = self._fallback
            if fallback is None:
                raise
            self._fallback = None
            # Без libpulse.so.0 simple тоже не откроется и сообщит audio-failed; откат
            # полезен, когда в libpulse нет символа, нужного только pa_stream.
            logger.warning(
                "Бэкенд записи stream недоступен (нет libpulse или символа); пробуем simple."
            )
            self._active.close()
            self._active = fallback()
            self._active.open(device, deadline=deadline, running=running)
        self._announce_fallback()

    def _announce_fallback(self) -> None:
        """Однократно после успешного открытия запасного источника."""
        if self._fallback is not None or self._on_fallback is None:
            return
        on_fallback, self._on_fallback = self._on_fallback, None
        on_fallback()

    def read_chunk(self) -> bytes | None:
        return self._active.read_chunk()

    def flush(self) -> None:
        self._active.flush()

    def close(self) -> None:
        self._active.close()

    @property
    def is_open(self) -> bool:
        return self._active.is_open

    @property
    def ended(self) -> bool:
        return self._active.ended

    @property
    def live(self) -> bool:
        return self._active.live

    @property
    def device_name(self) -> str | None:
        return self._active.device_name

    @property
    def selected_device(self) -> AudioDevice | None:
        return self._active.selected_device

    @property
    def device_label(self) -> str | None:
        label = getattr(self._active, "device_label", None)
        return label if isinstance(label, str) else None

    @property
    def device_change(self) -> DeviceChange | None:
        change = getattr(self._active, "device_change", None)
        return change if isinstance(change, DeviceChange) else None


def _probe_seconds(value: str) -> float:
    """Принимает только конечный положительный срок не больше 30 секунд."""
    try:
        seconds = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("seconds должно быть числом") from exc
    if not 0 < seconds <= 30:
        raise argparse.ArgumentTypeError("seconds должно быть больше 0 и не больше 30")
    return seconds


def main(
    argv: list[str] | None = None,
    *,
    source_factory: Callable[[], PulseStreamSource] = PulseStreamSource,
    clock: Callable[[], float] = time.monotonic,
    devices_fn: Callable[..., list[AudioDevice]] = list_devices,
    default_fn: Callable[..., AudioDevice] = default_device,
) -> int:
    """Проверяет явный микрофон или умолчание; выводит диагностику без PCM и распознавания."""
    parser = argparse.ArgumentParser(
        description="Короткая диагностика pa_stream без сохранения звука"
    )
    parser.add_argument(
        "--probe",
        required=True,
        metavar="ИМЯ|@default",
        help="имя источника или @default для устройства по умолчанию",
    )
    parser.add_argument("--seconds", type=_probe_seconds, default=5.0)
    args = parser.parse_args(argv)
    default_mode = args.probe == "@default"
    started = clock()

    class ProbeFormatter(logging.Formatter):
        """Добавляет время от входа в пробу по тем же часам, что ограничивают чтение."""

        def format(self, record: logging.LogRecord) -> str:
            return f"{(clock() - started) * 1000:.1f} ms DEBUG {record.getMessage()}"

    handler = logging.StreamHandler()
    handler.setLevel(logging.DEBUG)
    handler.addFilter(lambda record: record.levelno == logging.DEBUG)
    handler.setFormatter(ProbeFormatter())
    old_level, old_propagate = logger.level, logger.propagate
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    source: PulseStreamSource | None = None
    old_probe_logging = False
    byte_count = chunks = 0
    try:
        logger.debug("mode=%s", "default" if default_mode else "explicit")
        try:
            source = source_factory()
            old_probe_logging = source._probe_logging
            source._probe_logging = True
            source.open(None if default_mode else args.probe)
        except AudioError as exc:
            logger.debug("Ошибка открытия: %s", exc.code)
            return 2
        end = clock() + args.seconds
        while clock() < end:
            chunk = source.read_chunk()
            if chunk is None:
                break
            if chunk:
                byte_count += len(chunk)
                chunks += 1
        change = source.device_change
        if change is None:
            logger.debug("device_change: нет; classify_change: нет смены")
        else:
            logger.debug(
                "device_change: reason=%s server_changed=%s moved_to_name=%r",
                change.reason,
                change.server_changed,
                change.moved_to_name,
            )
            budget = _OpenDeadline(clock, DEVICE_CHANGE_BUDGET_S)
            devices = None
            try:
                devices = devices_fn(deadline=budget)
                budget.remaining(DEVICE_CHANGE_BUDGET_S)
            except AudioError as exc:
                logger.debug("Свежий список недоступен: %s", exc.code)
                devices = None
            default = None
            if devices is not None and change.default_mode:
                try:
                    default = default_fn(devices, deadline=budget)
                    budget.remaining(DEVICE_CHANGE_BUDGET_S)
                except AudioError as exc:
                    logger.debug("Новое умолчание недоступно: %s", exc.code)
                    default = None
            kind, label = classify_change(change, devices, default)
            logger.debug("classify_change: kind=%s label=%r", kind, label)
        return 0
    finally:
        try:
            logger.debug("Получено: bytes=%s chunks=%s", byte_count, chunks)
            if source is not None:
                source.close()
        finally:
            if source is not None:
                source._probe_logging = old_probe_logging
            logger.removeHandler(handler)
            handler.close()
            logger.setLevel(old_level)
            logger.propagate = old_propagate


if __name__ == "__main__":
    raise SystemExit(main())
