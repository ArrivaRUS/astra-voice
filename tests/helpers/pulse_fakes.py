"""Фейковый ABI libpulse для сценариев pa_stream без звуковой службы.

Общий для test_pulse_stream.py и test_audio.py: нативных вызовов здесь нет.
"""

from __future__ import annotations

import ctypes
import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, cast

from astra_voice.worker import audio
from astra_voice.worker import pulse_stream as ps

MIC = audio.AudioDevice(7, "mic.test", "Тестовый микрофон", False)
OTHER = audio.AudioDevice(8, "mic.other", "Другой микрофон", False)
REMOVE = ps.PA_SUBSCRIPTION_EVENT_SOURCE | ps.PA_SUBSCRIPTION_EVENT_REMOVE
SERVER = ps.PA_SUBSCRIPTION_EVENT_SERVER | ps.PA_SUBSCRIPTION_EVENT_CHANGE


class FakeFunction:
    """Запоминает явные присваивания ABI и передаёт вызовы сценарию."""

    def __init__(self, callback: Callable[..., Any]) -> None:
        self.assigned: dict[str, object] = {}
        self.callback = callback

    def __setattr__(self, name: str, value: object) -> None:
        if name in {"argtypes", "restype"}:
            self.assigned[name] = value
        object.__setattr__(self, name, value)

    def __call__(self, *args: Any) -> Any:
        return self.callback(*args)


class FakeLibrary:
    """Библиотека целиком на Python, без открытия CDLL."""

    def __init__(self, call: Callable[..., Any] = lambda *_: 0) -> None:
        self.functions: dict[str, FakeFunction] = {}
        self.call = call

    def __getattr__(self, name: str) -> FakeFunction:
        if name not in self.functions:
            self.functions[name] = FakeFunction(lambda *args: self.call(name, *args))
        return self.functions[name]


class Clock:
    """Время движется только при poll и инъецированной паузе."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now = round(self.now + seconds, 9)


@dataclass
class Step:
    """Один dispatch: состояние, PCM, колбэки и данные после их снимков."""

    context: int | None = None
    stream: int | None = None
    error: int | None = None
    fragments: list[bytes | int] = field(default_factory=list)
    after: list[bytes | int] = field(default_factory=list)
    events: list[tuple[int, int]] = field(default_factory=list)
    moved: list[tuple[int, bytes | None]] = field(default_factory=list)
    readable: int | None = None
    action: Callable[[], None] | None = None


class FakePulse(ps._PulseAsync):
    """Настоящие тонкие обёртки и помощники работают поверх сценарного ABI."""

    def __init__(self) -> None:
        # lib у родителя аннотирована как CDLL; нативный объект здесь не создаётся.
        self.lib = cast(Any, FakeLibrary(self.call))
        self.mainloop: int | None = None
        self.clock = Clock()
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.threads: set[int] = set()
        self.steps: deque[Step] = deque()
        self.fragments: deque[bytes | int] = deque()
        self.context_state = ps.PA_CONTEXT_UNCONNECTED
        self.stream_state = ps.PA_STREAM_UNCONNECTED
        self.index = MIC.index
        self.name: bytes | None = MIC.name.encode()
        self.error = 0
        self.readable: int | None = None
        self.subscribe_callback: Any = None
        self.moved_callback: Any = None
        self.fail: dict[str, Any] = {}
        self.op_state = ps.PA_OPERATION_DONE
        self.latency = 1234
        self.negative = 0
        self.timeout_us = 0
        self.hold_context = False
        self.hold_stream = False
        self.native_buffer: Any = None
        self.buffer_attr: tuple[int, ...] | None = None
        self.spec: tuple[int, ...] | None = None

    def call(self, symbol: str, *args: Any) -> Any:
        """Все вызовы, включая освобождение и колбэки, отмечают поток-владелец."""
        name = symbol.removeprefix("pa_")
        self.calls.append((name, args))
        self.threads.add(threading.get_ident())
        if name in self.fail:
            result = self.fail[name]
            if isinstance(result, Exception):
                raise result
            return result
        if name == "mainloop_new":
            return 0x100000001
        if name == "mainloop_get_api":
            return 0x100000002
        if name == "context_new":
            self.context_state = ps.PA_CONTEXT_CONNECTING
            return 0x100000003
        if name == "stream_new":
            self.stream_state = ps.PA_STREAM_CREATING
            spec = ctypes.cast(args[2], ctypes.POINTER(audio._PaSampleSpec)).contents
            self.spec = (spec.format, spec.rate, spec.channels)
            return 0x100000004
        if name in {"context_subscribe", "stream_flush"}:
            return 0x100000005
        if name == "context_set_subscribe_callback":
            self.subscribe_callback = args[1]
        elif name == "stream_set_moved_callback":
            self.moved_callback = args[1]
        elif name == "stream_connect_record":
            attr = ctypes.cast(args[2], ctypes.POINTER(audio._PaBufferAttr)).contents
            self.buffer_attr = (
                attr.maxlength,
                attr.tlength,
                attr.prebuf,
                attr.minreq,
                attr.fragsize,
            )
        elif name == "mainloop_prepare":
            self.timeout_us = args[1]
        elif name == "mainloop_poll":
            self.clock.sleep(self.timeout_us / 1_000_000)
        elif name == "mainloop_dispatch":
            self.dispatch()
        elif name == "context_get_state":
            return self.context_state
        elif name == "stream_get_state":
            return self.stream_state
        elif name == "context_errno":
            return self.error
        elif name == "stream_get_device_index":
            return self.index
        elif name == "stream_get_device_name":
            return self.name
        elif name == "stream_readable_size":
            return (
                self.readable
                if self.readable is not None
                else sum(len(part) if isinstance(part, bytes) else part for part in self.fragments)
            )
        elif name == "stream_peek":
            part = self.fragments[0] if self.fragments else 0
            if isinstance(part, bytes):
                self.native_buffer = ctypes.create_string_buffer(part)
                args[1]._obj.value = ctypes.addressof(self.native_buffer)
                args[2]._obj.value = len(part)
            else:
                args[1]._obj.value = None
                args[2]._obj.value = part
        elif name == "stream_drop":
            self.fragments.popleft()
        elif name == "operation_get_state":
            return self.op_state
        elif name == "operation_cancel":
            self.op_state = ps.PA_OPERATION_CANCELLED
        elif name == "stream_get_latency":
            args[1]._obj.value = self.latency
            args[2]._obj.value = self.negative
        return 0

    def dispatch(self) -> None:
        """Порядок внутри шага позволяет проверить точку обрезки в колбэке."""
        if self.context_state == ps.PA_CONTEXT_CONNECTING and not self.hold_context:
            self.context_state = ps.PA_CONTEXT_READY
        if self.stream_state == ps.PA_STREAM_CREATING and not self.hold_stream:
            self.stream_state = ps.PA_STREAM_READY
        step = self.steps.popleft() if self.steps else Step()
        if step.context is not None:
            self.context_state = step.context
        if step.stream is not None:
            self.stream_state = step.stream
        if step.error is not None:
            self.error = step.error
        self.readable = step.readable
        self.fragments.extend(step.fragments)
        for event, idx in step.events:
            assert self.subscribe_callback is not None
            self.subscribe_callback(0x100000003, event, idx, None)
        for idx, name in step.moved:
            self.index, self.name = idx, name
            assert self.moved_callback is not None
            self.moved_callback(0x100000004, None)
        self.fragments.extend(step.after)
        if step.action is not None:
            step.action()

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]


def source_for(
    pulse: FakePulse, *, devices: Callable[[], list[audio.AudioDevice]] | None = None
) -> ps.PulseStreamSource:
    """Все пути выбора устройства инъецированы, включая режим умолчания."""
    return ps.PulseStreamSource(
        pulse_factory=lambda: pulse,
        devices=devices or (lambda: [MIC]),
        default=lambda *_args, **_kwargs: MIC,
        clock=pulse.clock,
        sleep=pulse.clock.sleep,
    )


def opened(pulse: FakePulse, *, default: bool = False) -> ps.PulseStreamSource:
    source = source_for(pulse)
    source.open(None if default else MIC.name)
    return source
