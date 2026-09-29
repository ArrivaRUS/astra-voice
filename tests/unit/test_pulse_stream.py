"""Сценарии pa_stream и ABI без звуковой службы, pactl и нативных вызовов."""

from __future__ import annotations

import ctypes
import subprocess
import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, NoReturn, cast
from unittest.mock import Mock

import pytest

from astra_voice.worker import audio
from astra_voice.worker import pulse_stream as ps

pytestmark = pytest.mark.unit
MIC = audio.AudioDevice(7, "mic.test", "Тестовый микрофон", False)
OTHER = audio.AudioDevice(8, "mic.other", "Другой микрофон", False)
REMOVE = ps.PA_SUBSCRIPTION_EVENT_SOURCE | ps.PA_SUBSCRIPTION_EVENT_REMOVE
SERVER = ps.PA_SUBSCRIPTION_EVENT_SERVER | ps.PA_SUBSCRIPTION_EVENT_CHANGE


@pytest.fixture(autouse=True)
def no_native_audio(monkeypatch: pytest.MonkeyPatch) -> None:
    """Любой случайный выход к настоящему звуку немедленно роняет тест."""

    def forbidden(*args: object, **kwargs: object) -> NoReturn:
        raise AssertionError("Запрещены нативные библиотеки и внешние процессы")

    monkeypatch.setattr(ctypes, "CDLL", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(ps, "list_devices", forbidden)
    monkeypatch.setattr(audio, "list_devices", forbidden)
    audio.invalidate_device_cache()


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


def test_abi(monkeypatch: pytest.MonkeyPatch) -> None:
    """Точный набор символов и типов, включая все указатели и void."""
    lib = FakeLibrary()
    load = Mock(return_value=lib)
    monkeypatch.setattr(ctypes, "CDLL", load)
    ps._PulseAsync()
    load.assert_called_once_with("libpulse.so.0", use_errno=True)
    pointer = {
        "mainloop_new",
        "mainloop_get_api",
        "context_new",
        "stream_new",
        "context_subscribe",
        "stream_flush",
    }
    void = {
        "mainloop_free",
        "context_set_subscribe_callback",
        "context_disconnect",
        "context_unref",
        "stream_set_moved_callback",
        "stream_unref",
        "operation_unref",
        "operation_cancel",
    }
    integer = {
        "mainloop_prepare",
        "mainloop_poll",
        "mainloop_dispatch",
        "context_connect",
        "context_get_state",
        "context_errno",
        "stream_connect_record",
        "stream_get_state",
        "stream_peek",
        "stream_drop",
        "stream_get_latency",
        "stream_disconnect",
        "operation_get_state",
    }
    other = {
        "stream_readable_size": ctypes.c_size_t,
        "stream_get_device_index": ctypes.c_uint32,
        "stream_get_device_name": ctypes.c_char_p,
        "strerror": ctypes.c_char_p,
    }
    expected = {
        **dict.fromkeys(pointer, ctypes.c_void_p),
        **dict.fromkeys(void),
        **dict.fromkeys(integer, ctypes.c_int),
        **other,
    }
    assert set(lib.functions) == {"pa_" + name for name in expected}
    for name, restype in expected.items():
        assigned = lib.functions["pa_" + name].assigned
        assert assigned["restype"] is restype, name
        assert isinstance(assigned["argtypes"], list)
        # Единственная функция без аргументов в приложении А.
        assert bool(assigned["argtypes"]) == (name != "mainloop_new")
    assert lib.functions["pa_context_connect"].assigned["argtypes"] == [
        ctypes.c_void_p,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_void_p,
    ]
    assert lib.functions["pa_stream_connect_record"].assigned["argtypes"] == [
        ctypes.c_void_p,
        ctypes.c_char_p,
        ctypes.c_void_p,
        ctypes.c_uint32,
    ]
    assert lib.functions["pa_context_subscribe"].assigned["argtypes"] == [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    assert tuple(ps.SubscribeCallback._argtypes_) == (
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_uint32,
        ctypes.c_void_p,
    )
    assert tuple(ps.MovedCallback._argtypes_) == (ctypes.c_void_p, ctypes.c_void_p)


def test_load_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ctypes, "CDLL", Mock(side_effect=OSError("нет библиотеки")))
    with pytest.raises(audio.AudioError, match="Звуковая подсистема недоступна") as exc:
        ps._PulseAsync()
    assert exc.value.code == audio.ERROR_FAILED


def test_open_and_close() -> None:
    pulse = FakePulse()
    source = opened(pulse)
    assert isinstance(source, audio.ManagedSource)
    assert source.is_open and source.live and not source.ended
    assert source.selected_device == MIC and source.device_name == MIC.name
    assert source.device_label == MIC.label and source.device_change is None
    names = pulse.names()
    assert names.index("context_subscribe") < names.index("stream_new")
    assert pulse.spec == (audio.PA_SAMPLE_S16LE, 16000, 1)
    assert pulse.buffer_attr == (audio.U32_MAX,) * 4 + (640,)
    connect = next(args for name, args in pulse.calls if name == "context_connect")
    assert connect[1:] == (None, ps.PA_CONTEXT_NOAUTOSPAWN, None)
    record = next(args for name, args in pulse.calls if name == "stream_connect_record")
    assert record[1] == MIC.name.encode()
    assert record[3] == 0x220A
    source.close()
    names = pulse.names()
    for kind in ("stream", "context"):
        unref = names.index(kind + "_unref")
        setter = (
            "stream_set_moved_callback" if kind == "stream" else "context_set_subscribe_callback"
        )
        assert pulse.calls[unref - 2][0] == setter
        assert pulse.calls[unref - 2][1][1] is None
        assert names.count(kind + "_unref") == 1
    assert names.count("mainloop_free") == 1
    calls = list(pulse.calls)
    source.close()
    assert pulse.calls == calls
    assert not source.is_open
    assert (
        source.selected_device
        is source.device_name
        is source.device_label
        is source.device_change
        is None
    )


@pytest.mark.parametrize(
    ("code", "expected", "message"),
    [
        (audio.PA_ERR_BUSY, audio.ERROR_BUSY, "Микрофон занят другой программой."),
        (audio.PA_ERR_ACCESS, audio.ERROR_BUSY, "Микрофон занят другой программой."),
        (
            audio.PA_ERR_NOENTITY,
            audio.ERROR_NO_DEVICE,
            "Выбранный микрофон недоступен. Выберите устройство записи в настройках.",
        ),
        (99, audio.ERROR_FAILED, "Не удалось включить запись звука."),
    ],
)
def test_open_errors(
    code: int, expected: str, message: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    pulse = FakePulse()
    pulse.error = code
    pulse.fail["stream_get_state"] = ps.PA_STREAM_FAILED
    source = source_for(pulse)
    invalidated = Mock()
    monkeypatch.setattr(ps, "invalidate_device_cache", invalidated)
    with pytest.raises(audio.AudioError) as exc:
        source.open(MIC.name)
    assert (exc.value.code, exc.value.message) == (expected, message)
    assert invalidated.called
    for name in ("stream_unref", "context_unref", "mainloop_free"):
        assert pulse.names().count(name) == audio.OPEN_RETRIES
    assert pulse.clock.now == pytest.approx(0.750)
    calls = list(pulse.calls)
    source.close()
    assert pulse.calls == calls


@pytest.mark.parametrize(
    ("failed", "value", "mainloops", "contexts", "streams"),
    [
        ("mainloop_new", None, 0, 0, 0),
        ("mainloop_get_api", None, 1, 0, 0),
        ("context_new", None, 1, 0, 0),
        ("context_connect", -1, 1, 1, 0),
        ("context_get_state", ps.PA_CONTEXT_FAILED, 1, 1, 0),
        ("context_subscribe", None, 1, 1, 0),
        ("stream_new", None, 1, 1, 0),
        ("stream_connect_record", -1, 1, 1, 1),
        ("mainloop_prepare", -1, 1, 1, 0),
        ("mainloop_poll", -1, 1, 1, 0),
        ("mainloop_dispatch", -1, 1, 1, 0),
    ],
)
def test_partial_open_cleanup(
    failed: str, value: int | None, mainloops: int, contexts: int, streams: int
) -> None:
    pulse = FakePulse()
    pulse.fail[failed] = value
    source = source_for(pulse)
    with pytest.raises(audio.AudioError):
        source.open(MIC.name)
    for name, count in (
        ("mainloop_free", mainloops),
        ("context_unref", contexts),
        ("stream_unref", streams),
    ):
        assert pulse.names().count(name) == count * audio.OPEN_RETRIES
    assert not source.is_open


@pytest.mark.parametrize("stage", ["context", "stream"])
def test_deadline(stage: str) -> None:
    pulse = FakePulse()
    setattr(pulse, "hold_" + stage, True)
    source = source_for(pulse)
    with pytest.raises(audio.AudioError):
        source.open(MIC.name, deadline=audio._OpenDeadline(pulse.clock, 0.12))
    assert pulse.clock.now == pytest.approx(0.12, abs=0.000002)
    assert pulse.names().count("mainloop_free") == 1
    assert pulse.names().count("context_unref") == 1
    assert pulse.names().count("stream_unref") == (stage == "stream")


@pytest.mark.parametrize("stage", ["before", "context", "stream"])
def test_cancel_open(stage: str) -> None:
    pulse = FakePulse()
    running = threading.Event()
    running.set()
    if stage == "before":
        running.clear()
    else:
        if stage == "stream":
            pulse.steps.append(Step())
        pulse.steps.append(Step(action=running.clear))
    source = source_for(pulse)
    source.open(MIC.name, running=running)
    assert not source.is_open
    assert pulse.names().count("mainloop_free") == (stage != "before")
    assert pulse.names().count("stream_unref") == (stage == "stream")


def test_selection_precedes_factory() -> None:
    factory = Mock(side_effect=AssertionError("libpulse вызвана до проверки выбора"))
    source = ps.PulseStreamSource(
        pulse_factory=factory, devices=lambda: [], default=lambda *_a, **_kw: MIC
    )
    with pytest.raises(audio.AudioError) as exc:
        source.open(MIC.name)
    assert exc.value.code == audio.ERROR_NO_DEVICE
    factory.assert_not_called()


def test_fragments_hole_and_timeout() -> None:
    pulse = FakePulse()
    source = opened(pulse)
    pulse.steps.extend(
        [
            Step(fragments=[b"a", 55, b"b" * 100, b"c" * 1000]),
            Step(fragments=[b"d" * 179]),
            Step(),
        ]
    )
    expected = b"a" + b"b" * 100 + b"c" * 1000 + b"d" * 179
    first = source.read_chunk()
    assert first == expected[:640]
    second = source.read_chunk()
    assert second == expected[640:]
    assert source.read_chunk() == b""
    assert pulse.names().count("stream_drop") == 5
    assert all(args[1] == 50_000 for name, args in pulse.calls if name == "mainloop_prepare")
    source.close()


@pytest.mark.parametrize("part", [None, 77, b"x" * 13])
def test_peek_helper(part: bytes | int | None) -> None:
    pulse = FakePulse()
    if part is not None:
        pulse.fragments.append(part)
    assert pulse.peek(4) == part
    assert pulse.names().count("stream_drop") == (part is not None)


@pytest.mark.parametrize(
    "failed",
    ["mainloop_prepare", "mainloop_poll", "mainloop_dispatch", "stream_peek", "stream_drop"],
)
def test_read_failures(failed: str) -> None:
    pulse = FakePulse()
    source = opened(pulse)
    pulse.fragments.append(b"x")
    pulse.fail[failed] = -1
    assert source.read_chunk() is None
    assert source.device_change is None
    source.close()


def test_foreign_events_do_not_change_source(caplog: pytest.LogCaptureFixture) -> None:
    pulse = FakePulse()
    source = opened(pulse)
    caplog.clear()
    pulse.steps.append(
        Step(
            fragments=[b"a" * 640],
            events=[
                (REMOVE, OTHER.index),
                (SERVER, 0),
                (ps.PA_SUBSCRIPTION_EVENT_SOURCE | ps.PA_SUBSCRIPTION_EVENT_CHANGE, MIC.index),
                (ps.PA_SUBSCRIPTION_EVENT_SOURCE, MIC.index),
            ],
        )
    )
    assert source.read_chunk() == b"a" * 640
    assert source.device_change is None
    assert not caplog.records
    source.close()


def test_first_moved_unknown_index_is_accepted() -> None:
    pulse = FakePulse()
    pulse.index = ps.PA_INVALID_INDEX
    source = opened(pulse)
    pulse.steps.append(Step(moved=[(MIC.index, MIC.name.encode())], fragments=[b"a" * 640]))
    assert source.read_chunk() == b"a" * 640
    assert source.device_change is None and source._baseline_known
    pulse.steps.append(Step(moved=[(OTHER.index, OTHER.name.encode())]))
    assert source.read_chunk() is None
    assert source.device_change is not None and source.device_change.reason == audio.REASON_MOVED
    source.close()


@pytest.mark.parametrize(
    ("baseline", "idx", "name", "change"),
    [
        (ps.PA_INVALID_INDEX, OTHER.index, OTHER.name.encode(), True),
        (MIC.index, OTHER.index, MIC.name.encode(), True),
        (MIC.index, MIC.index, OTHER.name.encode(), False),
        (ps.PA_INVALID_INDEX, ps.PA_INVALID_INDEX, None, False),
    ],
)
def test_moved_identity(baseline: int, idx: int, name: bytes | None, change: bool) -> None:
    pulse = FakePulse()
    pulse.index = baseline
    source = opened(pulse)
    pulse.steps.append(Step(moved=[(idx, name)]))
    assert source.read_chunk() == (None if change else b"")
    assert (source.device_change is not None) == change
    source.close()


def test_mismatch_after_ready() -> None:
    pulse = FakePulse()
    pulse.name = OTHER.name.encode()
    source = opened(pulse)
    assert source.is_open
    assert source.device_change == audio.DeviceChange(audio.REASON_MISMATCH, MIC, False, OTHER.name)
    assert source.read_chunk() is None
    source.close()


@pytest.mark.parametrize("event", ["removed", "moved"])
def test_cutoff_counts_buffer_and_readable(event: str) -> None:
    pulse = FakePulse()
    source = opened(pulse)
    pulse.steps.append(Step(fragments=[b"a" * 641]))
    assert source.read_chunk() == b"a" * 640
    step = Step(fragments=[b"b" * 650], after=[b"Z" * 2000])
    if event == "removed":
        step.events = [(REMOVE, MIC.index)]
    else:
        step.moved = [(OTHER.index, OTHER.name.encode())]
    pulse.steps.append(step)
    assert source.read_chunk() == b"a" + b"b" * 639
    assert source.read_chunk() is None
    assert source.device_change is not None and source.device_change.reason == event
    assert source.read_chunk() is None
    source.close()


@pytest.mark.parametrize("readable", [ps.SIZE_MAX, 0])
def test_invalid_readable_never_leaks_data(readable: int) -> None:
    pulse = FakePulse()
    source = opened(pulse)
    pulse.steps.append(Step(events=[(REMOVE, MIC.index)], after=[b"Z" * 640], readable=readable))
    assert source.read_chunk() is None
    assert source.device_change is not None
    source.close()


@pytest.mark.parametrize("delay", [0, 1, 4])
def test_killed_remove_cutoff(delay: int) -> None:
    pulse = FakePulse()
    source = opened(pulse)
    pulse.steps.append(Step(fragments=[b"a" * 700]))
    assert source.read_chunk() == b"a" * 640
    failure = Step(stream=ps.PA_STREAM_FAILED, error=ps.PA_ERR_KILLED, after=[b"Z" * 640])
    if delay == 0:
        failure.events = [(REMOVE, MIC.index)]
    pulse.steps.append(failure)
    if delay:
        pulse.steps.extend(Step() for _ in range(delay - 1))
        pulse.steps.append(Step(events=[(REMOVE, MIC.index)]))
    assert source.read_chunk() is None
    assert source.device_change is not None and source.device_change.reason == audio.REASON_KILLED
    assert source._buffer == b""
    source.close()


@pytest.mark.parametrize("fresh", ["present", "absent", "error"])
def test_killed_without_remove(fresh: str) -> None:
    pulse = FakePulse()
    devices = Mock(return_value=[MIC])
    source = source_for(pulse, devices=devices)
    source.open(MIC.name)
    if fresh == "error":
        devices.side_effect = audio.AudioError(audio.ERROR_FAILED, "нет списка")
    else:
        devices.return_value = [MIC] if fresh == "present" else [OTHER]
    pulse.steps.append(Step(stream=ps.PA_STREAM_FAILED, error=ps.PA_ERR_KILLED))
    before = pulse.clock.now
    assert source.read_chunk() is None
    assert pulse.clock.now - before == pytest.approx(0.300, abs=0.000002)
    assert devices.call_count == 2
    if fresh == "present":
        assert source.device_change is None
    else:
        assert (
            source.device_change is not None and source.device_change.reason == audio.REASON_KILLED
        )
    source.close()


@pytest.mark.parametrize(("delay", "changed"), [(0, True), (1, True), (3, True), (None, False)])
def test_default_waits_for_server(delay: int | None, changed: bool) -> None:
    pulse = FakePulse()
    source = opened(pulse, default=True)
    events = [(REMOVE, MIC.index)] + ([(SERVER, 0)] if delay == 0 else [])
    pulse.steps.append(Step(events=events))
    if delay:
        pulse.steps.extend(Step() for _ in range(delay - 1))
        pulse.steps.append(Step(events=[(SERVER, 0)]))
    before = pulse.clock.now
    assert source.read_chunk() is None
    assert pulse.clock.now - before <= 0.250001
    assert source.device_change is not None and source.device_change.server_changed == changed
    assert source.device_change.default_mode
    source.close()


@pytest.mark.parametrize(
    ("context", "stream", "error"),
    [
        (ps.PA_CONTEXT_FAILED, ps.PA_STREAM_FAILED, ps.PA_ERR_KILLED),
        (ps.PA_CONTEXT_TERMINATED, ps.PA_STREAM_READY, 0),
        (ps.PA_CONTEXT_READY, ps.PA_STREAM_FAILED, audio.PA_ERR_BUSY),
        (ps.PA_CONTEXT_READY, ps.PA_STREAM_TERMINATED, 0),
    ],
)
def test_service_failure_is_not_device_change(context: int, stream: int, error: int) -> None:
    pulse = FakePulse()
    source = opened(pulse)
    pulse.steps.append(
        Step(context=context, stream=stream, error=error, events=[(REMOVE, MIC.index)])
    )
    assert source.read_chunk() is None
    assert source.device_change is None
    source.close()


def test_storm_and_callback_exception(caplog: pytest.LogCaptureFixture) -> None:
    pulse = FakePulse()
    source = opened(pulse)
    pulse.steps.append(Step(events=[(REMOVE, OTHER.index)] * 200))
    pulse.iterate(50_000)
    assert len(source._events) == 64
    assert source.read_chunk() == b""
    assert source.device_change is None
    caplog.clear()
    with caplog.at_level("DEBUG"):
        pulse.fail["stream_readable_size"] = ValueError("снимок недоступен")
        pulse.subscribe_callback(3, REMOVE, MIC.index, None)
        pulse.moved_callback(4, None)
    assert len(source._events) == 0
    assert len(caplog.records) == 2
    assert [record.levelname for record in caplog.records] == ["DEBUG", "DEBUG"]
    assert [record.getMessage() for record in caplog.records] == [
        "Не удалось сохранить событие подписки pa_stream",
        "Не удалось сохранить событие moved pa_stream",
    ]
    source.close()


@pytest.mark.parametrize("outcome", ["done", "cancelled", "timeout", "iterate"])
def test_wait_op(outcome: str) -> None:
    pulse = FakePulse()
    pulse.mainloop = 1
    if outcome == "cancelled":
        pulse.op_state = ps.PA_OPERATION_CANCELLED
    else:
        pulse.op_state = ps.PA_OPERATION_RUNNING
    if outcome == "done":
        pulse.steps.append(Step(action=lambda: setattr(pulse, "op_state", ps.PA_OPERATION_DONE)))
    if outcome == "iterate":
        pulse.fail["mainloop_poll"] = -1
    deadline = audio._OpenDeadline(pulse.clock, 0.100)
    if outcome == "done":
        pulse.wait_op(5, deadline)
    else:
        with pytest.raises((ps._PulseFailure, audio.AudioError)):
            pulse.wait_op(5, deadline)
    assert pulse.names().count("operation_unref") == 1
    assert pulse.names().count("operation_cancel") == (outcome in {"timeout", "iterate"})


@pytest.mark.parametrize("failure", [None, "null", "timeout"])
def test_flush(failure: str | None) -> None:
    pulse = FakePulse()
    source = opened(pulse)
    pulse.steps.append(Step(fragments=[b"a" * 650]))
    assert source.read_chunk() == b"a" * 640
    if failure == "null":
        pulse.fail["stream_flush"] = None
    if failure == "timeout":
        pulse.op_state = ps.PA_OPERATION_RUNNING
    if failure is None:
        source.flush()
        assert source._buffer == b""
        assert source._received == source._delivered == 0
    else:
        with pytest.raises(audio.AudioError, match="Не удалось подготовить запись звука") as exc:
            source.flush()
        assert exc.value.code == audio.ERROR_FAILED
    source.close()


@pytest.mark.parametrize(("rc", "negative", "expected"), [(0, 0, 1234), (0, 1, 0), (-1, 0, None)])
def test_latency(rc: int, negative: int, expected: int | None) -> None:
    pulse = FakePulse()
    source = opened(pulse)
    pulse.negative = negative
    if rc:
        pulse.fail["stream_get_latency"] = rc
    assert source.latency_us() == expected
    source.close()
    assert source.latency_us() is None


def test_capture_stop_and_single_thread() -> None:
    """Настоящий AudioCapture: внешний поток только снимает флаг, звук закрывает владелец."""
    pulse = FakePulse()
    source = source_for(pulse)
    entered, release = threading.Event(), threading.Event()
    errors, samples = Mock(), Mock(return_value=True)

    def hold() -> None:
        entered.set()
        assert release.wait(5)

    pulse.steps.extend([Step(), Step(), Step(action=hold)])
    capture = audio.AudioCapture(
        source=source,
        on_samples=samples,
        on_event=Mock(),
        on_error=errors,
        list_devices_fn=lambda **_: [MIC],
        default_device_fn=lambda *_a, **_kw: MIC,
        clock=pulse.clock,
        sleep=pulse.clock.sleep,
    )
    capture.start("stop-test", MIC.name, limit_s=30)
    try:
        assert entered.wait(5)
        before = pulse.clock.now
        capture.request_stop()
    finally:
        release.set()
        thread = capture._thread
        assert thread is not None
        thread.join(5)
        assert not thread.is_alive()
    assert pulse.clock.now - before <= 0.100
    assert pulse.threads == {thread.ident}
    assert threading.get_ident() not in pulse.threads
    errors.assert_not_called()
    samples.assert_not_called()
    assert not source.is_open
    assert pulse.names().count("stream_unref") == 1


def test_retry_succeeds_with_fresh_connection() -> None:
    pulse = FakePulse()
    source = source_for(pulse)
    pulse.steps.extend([Step(context=ps.PA_CONTEXT_FAILED), Step(), Step()])
    source.open(MIC.name)
    assert source.is_open
    assert pulse.names().count("mainloop_new") == 2
    assert pulse.names().count("mainloop_free") == 1
    source.close()
    assert pulse.names().count("mainloop_free") == 2
    assert pulse.names().count("context_unref") == 2
    assert pulse.names().count("stream_unref") == 1


def test_cancel_during_device_selection() -> None:
    pulse = FakePulse()
    running = threading.Event()
    running.set()

    def devices() -> list[audio.AudioDevice]:
        running.clear()
        return [MIC]

    source = source_for(pulse, devices=devices)
    source.open(MIC.name, running=running)
    assert not source.is_open
    assert not pulse.calls


def test_fallback_index_matches_remove() -> None:
    pulse = FakePulse()
    pulse.index = ps.PA_INVALID_INDEX
    source = opened(pulse)
    pulse.steps.append(Step(events=[(REMOVE, MIC.index)]))
    assert source.read_chunk() is None
    assert source.device_change is not None and source.device_change.reason == audio.REASON_REMOVED
    source.close()


def test_change_during_open_is_not_lost() -> None:
    pulse = FakePulse()
    pulse.steps.extend([Step(), Step(events=[(REMOVE, MIC.index)])])
    source = opened(pulse)
    assert source.read_chunk() is None
    assert source.device_change is not None and source.device_change.reason == audio.REASON_REMOVED
    source.close()


def test_context_failure_during_server_wait() -> None:
    pulse = FakePulse()
    source = opened(pulse, default=True)
    pulse.steps.extend([Step(events=[(REMOVE, MIC.index)]), Step(context=ps.PA_CONTEXT_FAILED)])
    assert source.read_chunk() is None
    assert source.device_change is None
    source.close()


def test_flush_preserves_pending_change() -> None:
    pulse = FakePulse()
    source = opened(pulse)
    pulse.op_state = ps.PA_OPERATION_RUNNING
    pulse.steps.append(
        Step(
            events=[(REMOVE, MIC.index)],
            action=lambda: setattr(pulse, "op_state", ps.PA_OPERATION_DONE),
        )
    )
    source.flush()
    pulse.fragments.append(b"Z" * 640)
    assert source.read_chunk() is None
    assert source.device_change is not None and source.device_change.reason == audio.REASON_REMOVED
    source.close()


def test_capture_mismatch_skips_ready() -> None:
    pulse = FakePulse()
    pulse.name = OTHER.name.encode()
    source = source_for(pulse)
    events, errors, changed = Mock(), Mock(), Mock()
    capture = audio.AudioCapture(
        source=source,
        on_samples=Mock(return_value=True),
        on_event=events,
        on_error=errors,
        on_device_change=changed,
        list_devices_fn=lambda **_: [MIC, OTHER],
        default_device_fn=lambda *_a, **_kw: MIC,
        clock=pulse.clock,
        sleep=pulse.clock.sleep,
    )
    capture.start("mismatch", MIC.name, limit_s=30)
    thread = capture._thread
    assert thread is not None
    thread.join(5)
    assert not thread.is_alive()
    events.assert_not_called()
    errors.assert_not_called()
    changed.assert_called_once_with("mismatch", audio.KIND_SWITCHED, OTHER.label)
    assert pulse.threads == {thread.ident}
    assert not source.is_open


def test_cancel_during_retry_pause() -> None:
    pulse = FakePulse()
    pulse.fail["context_connect"] = -1
    running = threading.Event()
    running.set()

    def sleep(seconds: float) -> None:
        running.clear()
        pulse.clock.sleep(seconds)

    source = ps.PulseStreamSource(
        pulse_factory=lambda: pulse,
        devices=lambda: [MIC],
        default=lambda *_a, **_kw: MIC,
        clock=pulse.clock,
        sleep=sleep,
    )
    source.open(MIC.name, running=running)
    assert not source.is_open
    assert pulse.clock.now <= 0.100
    assert pulse.names().count("mainloop_free") == 1


@pytest.mark.parametrize("seconds", [None, "0.1", "30"])
def test_probe_duration_and_no_pcm(seconds: str | None, capsys: pytest.CaptureFixture[str]) -> None:
    """Проба читает настоящий источник на фейковом ABI и печатает только счётчики."""
    pulse = FakePulse()
    secret = (b"PRIVATE_PCM_12345" * 40)[: audio.CHUNK_BYTES]
    assert len(secret) == audio.CHUNK_BYTES
    pulse.steps.extend([Step(), Step(), Step(fragments=[secret])])
    source = source_for(pulse)
    args = ["--probe", MIC.name]
    logging_before = (ps.logger.level, ps.logger.propagate, list(ps.logger.handlers))
    if seconds is not None:
        args += ["--seconds", seconds]
    assert ps.main(args, source_factory=lambda: source, clock=pulse.clock) == 0
    duration = 5.0 if seconds is None else float(seconds)
    assert pulse.clock.now == pytest.approx(0.1 + duration, abs=0.05)
    output = capsys.readouterr()
    assert output.out == ""
    assert "PRIVATE_PCM" not in output.err
    assert repr(secret) not in output.err
    assert "bytes=640 chunks=1" in output.err
    assert "ms DEBUG" in output.err
    assert all(" ms DEBUG " in line for line in output.err.splitlines())
    assert "context state: 1 -> 4" in output.err
    assert "stream state: 1 -> 2" in output.err
    assert "Базовая линия READY: index=7 name='mic.test' known=True" in output.err
    assert "device_change: нет; classify_change: нет смены" in output.err
    assert not source.is_open
    assert not source._probe_logging
    assert (ps.logger.level, ps.logger.propagate, ps.logger.handlers) == logging_before
    assert pulse.names().count("stream_unref") == 1


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["--probe"],
        ["--probe", MIC.name, "--unknown"],
        *[
            ["--probe", MIC.name, "--seconds", value]
            for value in ("0", "-1", "30.001", "nan", "inf", "oops")
        ],
    ],
)
def test_probe_invalid_arguments_do_not_create_source(args: list[str]) -> None:
    factory = Mock(side_effect=AssertionError("Источник не должен создаваться"))
    with pytest.raises(SystemExit) as caught:
        ps.main(args, source_factory=factory)
    assert caught.value.code == 2
    factory.assert_not_called()


@pytest.mark.parametrize("missing", [True, False])
def test_probe_open_error_returns_code(missing: bool, capsys: pytest.CaptureFixture[str]) -> None:
    """Имя проверяется обычным путём до libpulse; отказ подключения тоже даёт 2."""
    pulse = FakePulse()
    source = source_for(pulse)
    if not missing:
        pulse.fail["context_connect"] = -1
        pulse.error = audio.PA_ERR_ACCESS
    assert (
        ps.main(
            ["--probe", "missing" if missing else MIC.name],
            source_factory=lambda: source,
            clock=pulse.clock,
        )
        == 2
    )
    output = capsys.readouterr()
    assert output.out == ""
    assert (
        f"Ошибка открытия: {audio.ERROR_NO_DEVICE if missing else audio.ERROR_BUSY}" in output.err
    )
    assert "bytes=0 chunks=0" in output.err
    assert not source.is_open
    if missing:
        assert pulse.calls == []
    else:
        assert pulse.names().count("context_unref") == audio.OPEN_RETRIES


@pytest.mark.parametrize("moved", [False, True])
def test_probe_events_and_fresh_classification(
    moved: bool, capsys: pytest.CaptureFixture[str]
) -> None:
    """В stderr есть свои/чужие события и перенос; список для итога запрашивается свежим."""
    pulse = FakePulse()
    pulse.steps.extend(
        [
            Step(),
            Step(),
            Step(events=[(REMOVE, OTHER.index), (SERVER, 0)]),
            Step(moved=[(OTHER.index, OTHER.name.encode())])
            if moved
            else Step(events=[(REMOVE, MIC.index)]),
        ]
    )
    source = source_for(pulse)
    listing = Mock(return_value=[MIC, OTHER] if moved else [OTHER])
    assert (
        ps.main(
            ["--probe", MIC.name],
            source_factory=lambda: source,
            clock=pulse.clock,
            devices_fn=listing,
        )
        == 0
    )
    listing.assert_called_once()
    assert isinstance(listing.call_args.kwargs["deadline"], audio._OpenDeadline)
    output = capsys.readouterr().err
    assert "object=SOURCE kind=REMOVE index=8 чужой" in output
    assert "object=SERVER kind=CHANGE index=0 чужой" in output
    assert f"device_change: reason={'moved' if moved else 'removed'} server_changed=True" in output
    if moved:
        assert "moved: old_index=7 old_name='mic.test' new_index=8 new_name='mic.other'" in output
        assert "moved_to_name='mic.other'" in output
        assert "classify_change: kind=switched label='Другой микрофон'" in output
    else:
        assert "object=SOURCE kind=REMOVE index=7 наш" in output
        assert "classify_change: kind=device-lost label=None" in output
    assert pulse.names().count("stream_unref") == 1


@pytest.mark.parametrize(
    ("probe", "expected_device", "mode"),
    [("@default", None, "default"), (MIC.name, MIC.name, "explicit")],
)
def test_probe_mode_opens_selected_device(
    probe: str,
    expected_device: str | None,
    mode: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Маркер умолчания передаётся как None, явное имя остаётся неизменным."""
    pulse = FakePulse()
    source = source_for(pulse)
    open_spy = Mock(wraps=source.open)
    monkeypatch.setattr(source, "open", open_spy)
    listing = Mock(side_effect=AssertionError("Классификация без смены не нужна"))
    default = Mock(side_effect=AssertionError("Классификация без смены не нужна"))

    assert (
        ps.main(
            ["--probe", probe, "--seconds", "0.1"],
            source_factory=lambda: source,
            clock=pulse.clock,
            devices_fn=listing,
            default_fn=default,
        )
        == 0
    )
    open_spy.assert_called_once_with(expected_device)
    listing.assert_not_called()
    default.assert_not_called()
    assert f"mode={mode}" in capsys.readouterr().err


@pytest.mark.parametrize("default_error", [False, True])
def test_probe_default_removed_classifies_new_default(
    default_error: bool, capsys: pytest.CaptureFixture[str]
) -> None:
    """После REMOVE проба запрашивает новое умолчание в общем бюджете."""
    pulse = FakePulse()
    pulse.steps.extend([Step(), Step(), Step(events=[(SERVER, 0), (REMOVE, MIC.index)])])
    source = source_for(pulse)
    listing = Mock(return_value=[OTHER])
    default = Mock(
        side_effect=audio.AudioError(audio.ERROR_NO_DEVICE, "Нет умолчания")
        if default_error
        else None,
        return_value=OTHER,
    )

    assert (
        ps.main(
            ["--probe", "@default"],
            source_factory=lambda: source,
            clock=pulse.clock,
            devices_fn=listing,
            default_fn=default,
        )
        == 0
    )
    listing.assert_called_once()
    default.assert_called_once_with([OTHER], deadline=listing.call_args.kwargs["deadline"])
    assert isinstance(listing.call_args.kwargs["deadline"], audio._OpenDeadline)
    output = capsys.readouterr().err
    assert "mode=default" in output
    assert "device_change: reason=removed server_changed=True" in output
    if default_error:
        assert "classify_change: kind=device-lost label=None" in output
    else:
        assert "classify_change: kind=switched label='Другой микрофон'" in output
