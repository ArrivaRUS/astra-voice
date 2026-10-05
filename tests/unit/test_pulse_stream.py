"""Сценарии pa_stream и ABI без звуковой службы, pactl и нативных вызовов."""

from __future__ import annotations

import ctypes
import gc
import subprocess
import threading
import time
import weakref
from collections.abc import Iterator
from typing import Any, NoReturn, cast
from unittest.mock import Mock

import pytest

from astra_voice.worker import audio
from astra_voice.worker import pulse_stream as ps
from astra_voice.worker.audio import default_device, list_devices
from helpers.pulse_fakes import (
    MIC,
    OTHER,
    REMOVE,
    SERVER,
    Clock,
    FakeLibrary,
    FakePulse,
    Step,
    opened,
    source_for,
)

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def reset_device_provider() -> Iterator[None]:
    audio.set_device_introspection(None)
    yield
    audio.set_device_introspection(None)


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
        # Часть B добавляет два необязательных символа с указателем pa_operation.
        "context_get_source_info_list",
        "context_get_server_info",
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
    # close теперь сохраняет контекст; полное освобождение проверяем на shutdown.
    assert pulse.names().count("stream_unref") == 1
    assert "context_unref" not in pulse.names() and "mainloop_free" not in pulse.names()
    source.shutdown()
    names = pulse.names()
    for kind in ("stream", "context"):
        unref = names.index(kind + "_unref")
        setter = (
            "stream_set_moved_callback" if kind == "stream" else "context_set_subscribe_callback"
        )
        detached = [
            i
            for i, (name, args) in enumerate(pulse.calls[:unref])
            if name == setter and args[1] is None
        ]
        assert detached
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
    # FAILED stream нельзя безопасно закрыть: повтор начинается с нового context.
    assert pulse.names().count("stream_unref") == audio.OPEN_RETRIES
    assert pulse.names().count("context_new") == audio.OPEN_RETRIES
    assert pulse.names().count("context_unref") == audio.OPEN_RETRIES
    assert pulse.names().count("mainloop_free") == audio.OPEN_RETRIES
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
        # NULL stream не оставляет server stream; CREATING требует fail-closed.
        expected = (
            0 if failed == "stream_new" and name != "stream_unref" else count * audio.OPEN_RETRIES
        )
        assert pulse.names().count(name) == expected
    source.shutdown()
    for name, count in (("context_unref", contexts), ("mainloop_free", mainloops)):
        expected = count if failed == "stream_new" else count * audio.OPEN_RETRIES
        assert pulse.names().count(name) == expected
    assert not source.is_open


@pytest.mark.parametrize("stage", ["context", "stream"])
def test_deadline(stage: str) -> None:
    pulse = FakePulse()
    setattr(pulse, "hold_" + stage, True)
    source = source_for(pulse)
    with pytest.raises(audio.AudioError):
        source.open(MIC.name, deadline=audio._OpenDeadline(pulse.clock, 0.12))
    assert pulse.clock.now == pytest.approx(0.12, abs=0.000002)
    # CREATING после дедлайна тоже не должен ожить на сервере позднее.
    assert pulse.names().count("mainloop_free") == 1
    assert pulse.names().count("context_unref") == 1
    assert pulse.names().count("stream_unref") == (stage == "stream")
    source.shutdown()
    assert pulse.names().count("mainloop_free") == 1
    assert pulse.names().count("context_unref") == 1


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
    # Отмена stream сохраняет готовый контекст, отмена context убирает частичный.
    assert pulse.names().count("mainloop_free") == (stage == "context")
    assert pulse.names().count("stream_unref") == (stage == "stream")
    source.shutdown()
    assert pulse.names().count("mainloop_free") == (stage != "before")


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
    # Неблокирующая выборка событий относится к open; чтение по-прежнему ждёт 50 мс.
    calls_before_read = len(pulse.calls)
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
    assert all(
        args[1] == 50_000
        for name, args in pulse.calls[calls_before_read:]
        if name == "mainloop_prepare"
    )
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
    # Успешно созданный контекст остаётся до shutdown даже после холодного повтора.
    assert pulse.names().count("mainloop_free") == 1
    source.shutdown()
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


SOURCE_CHANGE = ps.PA_SUBSCRIPTION_EVENT_SOURCE | ps.PA_SUBSCRIPTION_EVENT_CHANGE
SOURCE_NEW = ps.PA_SUBSCRIPTION_EVENT_SOURCE | ps.PA_SUBSCRIPTION_EVENT_NEW


def test_own_remove_survives_source_change_storm() -> None:
    """P2-1: шторм NEW/CHANGE после нашего REMOVE не вытесняет его из очереди."""
    pulse = FakePulse()
    devices = Mock(return_value=[MIC])
    source = source_for(pulse, devices=devices)
    source.open(MIC.name)
    pulse.steps.append(Step(events=[(REMOVE, MIC.index)] + [(SOURCE_CHANGE, OTHER.index)] * 64))
    pulse.iterate(ps.POLL_US)
    # В очереди только сам REMOVE: NEW/CHANGE источников туда не попадают.
    assert len(source._events) == 1 and not source._overflow
    assert source.read_chunk() is None
    assert source.device_change is not None and source.device_change.reason == audio.REASON_REMOVED
    assert devices.call_count == 1
    source.close()


def test_foreign_storm_is_not_a_change() -> None:
    """Шторм чужих событий (включая переполнение очереди) не даёт ложной смены."""
    pulse = FakePulse()
    devices = Mock(return_value=[MIC, OTHER])
    source = source_for(pulse, devices=devices)
    source.open(MIC.name)
    pulse.steps.append(
        Step(
            fragments=[b"a" * 640],
            events=[(SOURCE_CHANGE, OTHER.index)] * 200
            + [(SOURCE_NEW, OTHER.index)] * 200
            + [(SERVER, 0)] * 200
            + [(REMOVE, OTHER.index)] * 200,
        )
    )
    assert source.read_chunk() == b"a" * 640
    assert source.device_change is None
    assert not source._overflow and not source._events
    # Переполнение проверено ровно одной сверкой списка; наше имя в нём есть.
    assert devices.call_count == 2
    pulse.steps.append(Step(fragments=[b"b" * 640]))
    assert source.read_chunk() == b"b" * 640
    assert devices.call_count == 2
    source.close()


@pytest.mark.parametrize("fresh", ["absent", "present", "error"])
def test_overflow_checks_device_list(fresh: str) -> None:
    """Наш REMOVE потерян при переполнении: смену находит сверка по списку."""
    pulse = FakePulse()
    devices = Mock(return_value=[MIC])
    source = source_for(pulse, devices=devices)
    source.open(MIC.name)
    if fresh == "error":
        devices.side_effect = audio.AudioError(audio.ERROR_FAILED, "нет списка")
    else:
        devices.return_value = [OTHER] if fresh == "absent" else [MIC, OTHER]
    pulse.steps.append(
        Step(
            fragments=[b"a" * 640],
            events=[(REMOVE, OTHER.index)] * ps.EVENT_QUEUE_LIMIT + [(REMOVE, MIC.index)],
            after=[b"Z" * 640],
        )
    )
    first = source.read_chunk()
    if fresh == "absent":
        # Точка обрезки — момент переполнения: звук после неё не выдаётся.
        assert first == b"a" * 640
        assert source.read_chunk() is None
        change = source.device_change
        assert change is not None and change.reason == audio.REASON_REMOVED
        assert change.fresh == audio.FreshDevices((OTHER,), None)
    else:
        # Сбой списка при живом потоке запись не обрывает.
        assert first == b"a" * 640
        assert source.device_change is None
        assert source.read_chunk() == b"Z" * 640
    assert devices.call_count == 2
    source.close()


def test_overflow_detects_lost_moved() -> None:
    """moved, не поместившийся в очередь, находится сверкой устройства потока."""
    pulse = FakePulse()
    devices = Mock(return_value=[MIC, OTHER])
    source = source_for(pulse, devices=devices)
    source.open(MIC.name)
    pulse.steps.append(
        Step(
            events=[(REMOVE, 99)] * ps.EVENT_QUEUE_LIMIT,
            moved=[(OTHER.index, OTHER.name.encode())],
        )
    )
    assert source.read_chunk() is None
    change = source.device_change
    assert change is not None and change.reason == audio.REASON_MOVED
    assert change.moved_to_name == OTHER.name
    # Смену нашли по самому потоку; список не понадобился.
    assert devices.call_count == 1
    source.close()


def test_server_change_is_a_flag_not_a_queue_entry() -> None:
    """SERVER CHANGE не занимает очередь и всё равно отмечается в смене."""
    pulse = FakePulse()
    source = opened(pulse, default=True)
    pulse.steps.append(Step(events=[(SERVER, 0)] * 500 + [(REMOVE, MIC.index)]))
    pulse.iterate(ps.POLL_US)
    assert len(source._events) == 1 and not source._overflow
    before = pulse.clock.now
    assert source.read_chunk() is None
    # Умолчание уже сменилось: окно ожидания SERVER CHANGE не открывается.
    assert pulse.clock.now - before < ps.SERVER_WAIT_S
    change = source.device_change
    assert change is not None and change.reason == audio.REASON_REMOVED and change.server_changed
    source.close()


def test_killed_default_reads_devices_once_within_budget() -> None:
    """P3: на пути KILLED без REMOVE список читается один раз, итог ≤ 1 с (T-g)."""
    pulse = FakePulse()
    other_default = audio.AudioDevice(9, "mic.next", "Новый микрофон", False)
    lists = 0

    def devices() -> list[audio.AudioDevice]:
        nonlocal lists
        lists += 1
        if lists > 1:
            pulse.clock.sleep(0.600)
            return [other_default]
        return [MIC]

    def default(found: list[audio.AudioDevice], **_kwargs: object) -> audio.AudioDevice:
        if lists > 1:
            pulse.clock.sleep(0.050)
            return other_default
        return MIC

    source = ps.PulseStreamSource(
        pulse_factory=lambda: pulse,
        devices=devices,
        default=default,
        clock=pulse.clock,
        sleep=pulse.clock.sleep,
    )
    marks: list[float] = []
    changed = Mock(side_effect=lambda *_: marks.append(pulse.clock.now))
    capture_lists = Mock(side_effect=AssertionError("повторный запрос списка"))
    capture_default = Mock(side_effect=AssertionError("повторный запрос умолчания"))
    pulse.steps.extend(
        [
            Step(),
            Step(),
            Step(
                stream=ps.PA_STREAM_FAILED,
                error=ps.PA_ERR_KILLED,
                action=lambda: marks.append(pulse.clock.now),
            ),
        ]
    )
    capture = audio.AudioCapture(
        source=source,
        on_samples=Mock(return_value=True),
        on_event=Mock(),
        on_error=Mock(),
        on_device_change=changed,
        list_devices_fn=capture_lists,
        default_device_fn=capture_default,
        clock=pulse.clock,
        sleep=pulse.clock.sleep,
    )
    capture.start("killed", None, limit_s=30)
    thread = capture._thread
    assert thread is not None
    thread.join(5)
    assert not thread.is_alive()
    changed.assert_called_once_with("killed", audio.KIND_SWITCHED, other_default.label)
    capture_lists.assert_not_called()
    capture_default.assert_not_called()
    assert lists == 2
    killed_at, reported_at = marks
    assert reported_at - killed_at <= 1.0


def test_killed_wait_skips_list_after_stop() -> None:
    """Запись остановлена во время ожидания REMOVE: список не читается."""
    pulse = FakePulse()
    devices = Mock(return_value=[MIC])
    running = threading.Event()
    running.set()
    source = source_for(pulse, devices=devices)
    source.open(MIC.name, running=running)
    pulse.steps.extend(
        [
            Step(stream=ps.PA_STREAM_FAILED, error=ps.PA_ERR_KILLED),
            Step(action=running.clear),
        ]
    )
    assert source.read_chunk() is None
    assert source.device_change is None
    assert devices.call_count == 1
    source.close()


@pytest.mark.parametrize("exact", [True, False])
def test_fallback_index_only_when_exact(exact: bool) -> None:
    """P3: номер узла из pw-dump не годится для сверки REMOVE; ищем по имени."""
    mic = audio.AudioDevice(MIC.index, MIC.name, MIC.description, False, index_exact=exact)
    pulse = FakePulse()
    pulse.index = ps.PA_INVALID_INDEX
    devices = Mock(return_value=[mic])
    source = source_for(pulse, devices=devices)
    source.open(mic.name)
    assert source._index == (MIC.index if exact else ps.PA_INVALID_INDEX)
    # При неточном индексе чужой REMOVE с тем же номером узла запись не обрывает.
    pulse.steps.append(Step(fragments=[b"a" * 640], events=[(REMOVE, MIC.index)]))
    assert source.read_chunk() == b"a" * 640
    if exact:
        assert source.read_chunk() is None
        assert source.device_change is not None
        assert devices.call_count == 1
        source.close()
        return
    assert source.device_change is None
    assert source.read_chunk() == b""
    # Настоящая пропажа при неизвестном индексе находится по имени в списке.
    devices.return_value = [OTHER]
    pulse.steps.append(Step(events=[(REMOVE, 12345)]))
    assert source.read_chunk() is None
    assert source.device_change is not None
    assert source.device_change.reason == audio.REASON_REMOVED
    source.close()


def test_missing_symbol_is_audio_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """P3: в libpulse нет символа — понятная ошибка записи, а не AttributeError."""
    monkeypatch.setattr(ctypes, "CDLL", Mock(return_value=Mock(spec=[])))
    with pytest.raises(audio.AudioError, match="Звуковая подсистема недоступна") as exc:
        ps._PulseAsync()
    assert exc.value.code == audio.ERROR_FAILED
    assert isinstance(exc.value.__cause__, AttributeError)


@pytest.mark.parametrize("cancel", [False, True])
def test_change_reported_when_stop_races_remove(cancel: bool) -> None:
    """P3: REMOVE и отпускание клавиши в одной итерации — смена всё равно сообщается."""
    pulse = FakePulse()
    source = source_for(pulse)
    changed, lists = Mock(), Mock(return_value=[MIC, OTHER])
    capture = audio.AudioCapture(
        source=source,
        on_samples=Mock(return_value=True),
        on_event=Mock(),
        on_error=Mock(),
        on_device_change=changed,
        list_devices_fn=lists,
        default_device_fn=lambda *_a, **_kw: OTHER,
        clock=pulse.clock,
        sleep=pulse.clock.sleep,
    )
    pulse.steps.extend(
        [
            Step(),
            Step(),
            Step(
                fragments=[b"a" * 640],
                events=[(REMOVE, MIC.index)],
                action=lambda: capture.request_stop(cancel=cancel),
            ),
        ]
    )
    capture.start("race", MIC.name, limit_s=30)
    thread = capture._thread
    assert thread is not None
    thread.join(5)
    assert not thread.is_alive()
    if cancel:
        changed.assert_not_called()
        lists.assert_not_called()
    else:
        changed.assert_called_once_with("race", audio.KIND_DEVICE_LOST, None)
    assert not source.is_open


WARNING_FALLBACK = "Бэкенд записи stream недоступен (нет libpulse или символа); пробуем simple."


def _unavailable_primary() -> ps.PulseStreamSource:
    """Настоящий источник, у которого загрузка libpulse заканчивается отказом API."""
    return ps.PulseStreamSource(
        pulse_factory=ps._PulseAsync,
        devices=lambda: [MIC],
        default=lambda *_args, **_kwargs: MIC,
    )


def _warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.message for r in caplog.records if r.levelname == "WARNING"]


def test_fallback_to_simple_when_symbol_missing(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """В libpulse нет символа pa_stream — один WARNING, запись идёт через simple."""
    monkeypatch.setattr(ctypes, "CDLL", Mock(return_value=Mock(spec=[])))
    invalidate = Mock()
    monkeypatch.setattr(ps, "invalidate_device_cache", invalidate)
    simple = Mock(spec=audio.PulseSimpleSource)
    simple.read_chunk.return_value = b"\0" * audio.CHUNK_BYTES
    factory, notified = Mock(return_value=simple), Mock()
    source = ps.StreamWithFallback(_unavailable_primary(), fallback=factory, on_fallback=notified)
    running = threading.Event()
    running.set()
    with caplog.at_level("WARNING", logger=ps.__name__):
        source.open(MIC.name, running=running)
        source.open(MIC.name, running=running)
    factory.assert_called_once_with()
    notified.assert_called_once_with()
    assert simple.open.call_count == 2
    simple.open.assert_called_with(MIC.name, deadline=None, running=running)
    assert source.read_chunk() == b"\0" * audio.CHUNK_BYTES
    assert _warnings(caplog) == [WARNING_FALLBACK]
    # P3-2: отказ API не сбрасывает кэш устройств.
    invalidate.assert_not_called()


def test_no_library_gives_audio_failed_through_real_simple(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Нет libpulse.so.0: simple тоже не открывается, владелец получает прежний audio-failed."""
    monkeypatch.setattr(ctypes, "CDLL", Mock(side_effect=OSError("нет библиотеки")))
    source = ps.StreamWithFallback(
        _unavailable_primary(),
        fallback=lambda: audio.PulseSimpleSource(
            devices=lambda: [MIC], default=lambda *_a, **_kw: MIC
        ),
    )
    clock = Clock()
    errors = Mock()
    capture = audio.AudioCapture(
        source=source,
        on_samples=Mock(return_value=True),
        on_event=Mock(),
        on_error=errors,
        clock=clock,
        sleep=clock.sleep,
    )
    with caplog.at_level("WARNING", logger=ps.__name__):
        capture.start("nolib", MIC.name, limit_s=30)
        thread = capture._thread
        assert thread is not None
        thread.join(5)
        assert not thread.is_alive()
    errors.assert_called_once()
    uid, code, message = errors.call_args.args
    assert (uid, code) == ("nolib", audio.ERROR_FAILED)
    assert isinstance(message, str) and message
    assert _warnings(caplog) == [WARNING_FALLBACK]
    assert not source.is_open


def test_fallback_announced_only_after_simple_opens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """on_fallback — только после первого успешного открытия simple и один раз."""
    monkeypatch.setattr(ctypes, "CDLL", Mock(side_effect=OSError("нет библиотеки")))
    simple = Mock(spec=audio.PulseSimpleSource)
    simple.open.side_effect = [audio.AudioError(audio.ERROR_FAILED, "нет"), None, None]
    notified = Mock()
    source = ps.StreamWithFallback(
        _unavailable_primary(), fallback=Mock(return_value=simple), on_fallback=notified
    )
    with pytest.raises(audio.AudioError):
        source.open(MIC.name)
    notified.assert_not_called()
    source.open(MIC.name)
    notified.assert_called_once_with()
    source.open(MIC.name)
    notified.assert_called_once_with()


def test_fallback_happens_once(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Если и запасной путь сообщает об отказе API, повторного отката нет."""
    monkeypatch.setattr(ctypes, "CDLL", Mock(side_effect=OSError("нет библиотеки")))
    simple = Mock(spec=audio.PulseSimpleSource)
    simple.open.side_effect = audio.AudioApiUnavailable(audio.ERROR_FAILED, "нет")
    factory = Mock(return_value=simple)
    source = ps.StreamWithFallback(_unavailable_primary(), fallback=factory)
    with caplog.at_level("WARNING", logger=ps.__name__):
        for _ in range(2):
            with pytest.raises(audio.AudioApiUnavailable):
                source.open(MIC.name)
    factory.assert_called_once_with()
    assert _warnings(caplog) == [WARNING_FALLBACK]


def test_fallback_passes_real_deadline() -> None:
    """Запасной источник получает тот же _OpenDeadline и флаг, что и pa_stream."""
    primary = Mock(spec=ps.PulseStreamSource)
    primary.open.side_effect = audio.AudioApiUnavailable(audio.ERROR_FAILED, "нет")
    simple = Mock(spec=audio.PulseSimpleSource)
    source = ps.StreamWithFallback(primary, fallback=lambda: simple)
    clock = Clock()
    deadline = audio._OpenDeadline(clock)
    running = threading.Event()
    running.set()
    source.open(MIC.name, deadline=deadline, running=running)
    primary.open.assert_called_once_with(MIC.name, deadline=deadline, running=running)
    simple.open.assert_called_once_with(MIC.name, deadline=deadline, running=running)
    assert simple.open.call_args.kwargs["deadline"] is deadline


def test_close_before_and_after_fallback() -> None:
    """close до отката закрывает pa_stream; при откате он закрыт, дальше закрывается simple."""
    primary = Mock(spec=ps.PulseStreamSource)
    simple = Mock(spec=audio.PulseSimpleSource)
    source = ps.StreamWithFallback(primary, fallback=lambda: simple)
    source.close()
    primary.close.assert_called_once_with()
    primary.open.side_effect = audio.AudioApiUnavailable(audio.ERROR_FAILED, "нет")
    source.open(MIC.name)
    # Отброшенный pa_stream закрыт при откате.
    assert primary.close.call_count == 2
    simple.close.assert_not_called()
    source.close()
    simple.close.assert_called_once_with()
    assert primary.close.call_count == 2


@pytest.mark.parametrize("code", [audio.ERROR_BUSY, audio.ERROR_NO_DEVICE, audio.ERROR_FAILED])
def test_microphone_errors_are_not_masked(
    code: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Обычная ошибка микрофона идёт владельцу как есть, simple не подставляется."""
    primary = Mock(spec=ps.PulseStreamSource)
    primary.open.side_effect = audio.AudioError(code, "Микрофон занят другой программой.")
    factory = Mock()
    source = ps.StreamWithFallback(primary, fallback=factory)
    with caplog.at_level("WARNING", logger=ps.__name__):
        with pytest.raises(audio.AudioError) as exc:
            source.open(MIC.name)
    assert exc.value.code == code
    assert not isinstance(exc.value, audio.AudioApiUnavailable)
    factory.assert_not_called()
    assert not _warnings(caplog)


def test_fallback_wrapper_delegates_to_stream() -> None:
    """Без отказа API обёртка прозрачна: устройство, метка и закрытие — у pa_stream."""
    pulse = FakePulse()
    source = ps.StreamWithFallback(source_for(pulse), fallback=Mock(side_effect=AssertionError))
    assert isinstance(source, audio.ManagedSource)
    assert not source.is_open
    source.open(MIC.name)
    assert source.is_open
    assert source.selected_device == MIC
    assert source.device_name == MIC.name
    assert source.device_label == MIC.label
    assert source.device_change is None
    assert source.live and not source.ended
    source.close()
    assert not source.is_open
    assert source.device_label is None


def test_device_change_through_wrapper_in_capture() -> None:
    """Смена устройства pa_stream видна AudioCapture и через обёртку."""
    pulse = FakePulse()
    source = ps.StreamWithFallback(source_for(pulse), fallback=Mock(side_effect=AssertionError))
    changed, lists = Mock(), Mock(return_value=[OTHER])
    capture = audio.AudioCapture(
        source=source,
        on_samples=Mock(return_value=True),
        on_event=Mock(),
        on_error=Mock(),
        on_device_change=changed,
        list_devices_fn=lists,
        default_device_fn=lambda *_a, **_kw: OTHER,
        clock=pulse.clock,
        sleep=pulse.clock.sleep,
    )
    pulse.steps.extend(
        [
            Step(),
            Step(),
            Step(fragments=[b"a" * 640], events=[(REMOVE, MIC.index)]),
        ]
    )
    capture.start("moved", MIC.name, limit_s=30)
    thread = capture._thread
    assert thread is not None
    thread.join(5)
    assert not thread.is_alive()
    changed.assert_called_once_with("moved", audio.KIND_DEVICE_LOST, None)
    assert not source.is_open


@pytest.mark.parametrize("fail", [None, "context_connect"])
def test_warm_up_never_creates_stream(fail: str | None, caplog: pytest.LogCaptureFixture) -> None:
    """Холодный старт (жалоба 30.09): прогрев без микрофона — только опрос и контекст (PRD §9.5)."""
    pulse = FakePulse()
    if fail is not None:
        pulse.fail[fail] = -1
    source = source_for(pulse)
    with caplog.at_level("INFO", logger="astra_voice.worker.pulse_stream"):
        source.warm_up(None)
    names = pulse.names()
    assert "stream_new" not in names and "stream_connect_record" not in names
    assert names.count("context_connect") == 1
    # Успешный прогрев сохраняет READY-контекст; сбой убирает частичные ресурсы.
    assert names.count("context_unref") == (fail is not None)
    assert names.count("mainloop_free") == (fail is not None)
    if fail is None:
        assert pulse.context_state == ps.PA_CONTEXT_READY
    assert not source.is_open and source.selected_device is None
    lines = [r.getMessage() for r in caplog.records if r.levelname == "INFO"]
    assert len(lines) == 1 and lines[0].startswith("Прогрев пути записи без микрофона: t_ms=")
    assert lines[0].endswith("итог=" + ("ok" if fail is None else audio.ERROR_FAILED))
    assert MIC.name not in lines[0]
    # Прогрев не мешает настоящему открытию тем же источником.
    pulse.fail.clear()
    source.open(MIC.name)
    assert source.is_open
    source.close()


def test_warm_up_api_unavailable_does_not_start_fallback() -> None:
    primary = Mock(spec=ps.PulseStreamSource)
    primary.warm_up.side_effect = audio.AudioApiUnavailable(audio.ERROR_FAILED, "secret")
    simple = Mock()
    fallback = Mock(return_value=simple)
    source = ps.StreamWithFallback(primary, fallback=fallback)
    with pytest.raises(audio.AudioApiUnavailable):
        source.warm_up(None)
    primary.warm_up.assert_called_once_with(None)
    fallback.assert_not_called()
    primary.open.assert_not_called()
    simple.warm_up.assert_not_called()
    simple.open.assert_not_called()


def test_warm_up_factory_api_unavailable_keeps_its_code(
    caplog: pytest.LogCaptureFixture,
) -> None:
    source = ps.PulseStreamSource(
        pulse_factory=Mock(side_effect=audio.AudioApiUnavailable("api-unavailable", "private")),
        devices=lambda: [MIC],
        default=lambda *_args, **_kwargs: MIC,
    )
    with caplog.at_level("INFO", logger=ps.__name__):
        source.warm_up(None)
    lines = [r.getMessage() for r in caplog.records if r.levelname == "INFO"]
    assert len(lines) == 1 and lines[0].endswith("итог=api-unavailable")
    assert "private" not in lines[0]


def test_warm_up_context_timeout_releases_resources(caplog: pytest.LogCaptureFixture) -> None:
    pulse = FakePulse()
    pulse.hold_context = True
    with caplog.at_level("INFO", logger=ps.__name__):
        source_for(pulse).warm_up(None)
    assert pulse.clock.now == pytest.approx(audio.OPEN_DEADLINE_S)
    names = pulse.names()
    assert names.count("context_disconnect") == 1
    assert names.count("context_unref") == 1
    assert names.count("mainloop_free") == 1
    assert [r.getMessage() for r in caplog.records if r.levelname == "INFO"] == [
        "Прогрев пути записи без микрофона: t_ms=2000 итог=audio-failed"
    ]


def test_warm_up_context_new_exception_does_not_leak_to_info(
    caplog: pytest.LogCaptureFixture,
) -> None:
    pulse = FakePulse()
    pulse.fail["context_new"] = RuntimeError("private context failure")
    with caplog.at_level("INFO", logger=ps.__name__):
        source_for(pulse).warm_up(None)
    assert pulse.names().count("mainloop_free") == 1
    lines = [r.getMessage() for r in caplog.records if r.levelname == "INFO"]
    assert len(lines) == 1 and lines[0].endswith("итог=audio-failed")
    assert all("private context failure" not in line for line in lines)


@pytest.mark.parametrize("service_fail", [False, True])
def test_warm_up_select_error_still_connects_service(
    service_fail: bool, caplog: pytest.LogCaptureFixture
) -> None:
    pulse = FakePulse()
    if service_fail:
        pulse.fail["context_connect"] = -1
    with caplog.at_level("INFO", logger=ps.__name__):
        source_for(pulse, devices=lambda: []).warm_up(MIC.name)
    names = pulse.names()
    assert names.count("context_connect") == 1
    if not service_fail:
        assert ps.PA_CONTEXT_READY == pulse.context_state
    assert "stream_new" not in names
    lines = [r.getMessage() for r in caplog.records if r.levelname == "INFO"]
    result = "audio-failed" if service_fail else "ok"
    assert len(lines) == 1 and lines[0].endswith(f"итог=device-audio-no-device,service-{result}")
    assert all("Выбранный микрофон" not in line for line in lines)


@pytest.mark.parametrize("warmup", [False, True])
def test_persistent_context_across_three_recordings(
    warmup: bool, caplog: pytest.LogCaptureFixture
) -> None:
    pulse = FakePulse()
    source = source_for(pulse)
    if warmup:
        thread = threading.Thread(target=source.warm_up, args=(MIC.name,), name="audio-warmup")
        thread.start()
        thread.join(2)
        assert not thread.is_alive()
        assert pulse.context_state == ps.PA_CONTEXT_READY
        assert not any(name.startswith("stream_") for name in pulse.names())
    with caplog.at_level("INFO", logger=ps.__name__):
        for index in range(3):
            before = len(pulse.calls)
            source.open(MIC.name)
            if warmup or index:
                assert not {"mainloop_new", "context_new", "context_connect"}.intersection(
                    name for name, _ in pulse.calls[before:]
                )
            source.close()
            before_idle = list(pulse.calls)
            source.warm_up(MIC.name)
            source.close()
            assert not any(
                name.startswith("stream_") for name, _ in pulse.calls[len(before_idle) :]
            )
    names = pulse.names()
    assert names.count("context_new") == names.count("context_connect") == 1
    assert names.count("mainloop_new") == names.count("context_subscribe") == 1
    assert names.count("stream_new") == names.count("stream_unref") == 3
    assert names.count("context_unref") == names.count("mainloop_free") == 0
    # Контекст может создать audio-warmup; все stream_* принадлежат захвату.
    assert pulse.stream_threads == {threading.get_ident()}
    logs = [
        r.getMessage()
        for r in caplog.records
        if r.getMessage().startswith("Источник записи открыт")
    ]
    assert len(logs) == 3
    assert logs[0].endswith("контекст=" + ("тёплый" if warmup else "холодный"))
    assert all(line.endswith("контекст=тёплый") for line in logs[1:])
    source.shutdown()
    assert pulse.names().count("context_unref") == pulse.names().count("mainloop_free") == 1


@pytest.mark.parametrize("state", [ps.PA_CONTEXT_FAILED, ps.PA_CONTEXT_TERMINATED])
@pytest.mark.parametrize("when", ["idle", "idle-dispatch", "recording"])
def test_dead_context_recreated_only_on_next_open(
    state: int, when: str, caplog: pytest.LogCaptureFixture
) -> None:
    pulse = FakePulse()
    source = opened(pulse)
    if when == "recording":
        pulse.steps.append(Step(context=state))
        assert source.read_chunk() is None
        assert source._failed and source._context_dead
    source.close()
    if when == "idle":
        pulse.context_state = state
    elif when == "idle-dispatch":
        pulse.idle_steps.append(Step(context=state))
    before = len(pulse.calls)
    source.warm_up(MIC.name)
    pulse.clock.sleep(10)
    source.warm_up(MIC.name)
    assert "context_new" not in pulse.names()[before:]
    assert not any(name.startswith("stream_") for name in pulse.names()[before:])
    with caplog.at_level("INFO", logger=ps.__name__):
        source.open(MIC.name)
    assert source.is_open
    assert pulse.names().count("context_new") == pulse.names().count("mainloop_new") == 2
    assert pulse.names().count("context_unref") == pulse.names().count("mainloop_free") == 1
    assert [r.getMessage() for r in caplog.records if r.getMessage().startswith("Соединение")] == [
        "Соединение со звуковой службой восстановлено"
    ]
    assert not source._context_dead
    source.shutdown()


def test_idle_subscription_events_do_not_classify_next_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pulse = FakePulse()
    source = opened(pulse, default=True)
    source.close()
    invalidated = Mock()
    monkeypatch.setattr(ps, "invalidate_device_cache", invalidated)

    def check_idle() -> None:
        assert source._stream is None
        assert source._devices_changed
        assert not source._events and not source._overflow and not source._server_changed

    for _ in range(3):
        pulse.idle_steps.append(Step(events=[(REMOVE, MIC.index), (SERVER, 0)], action=check_idle))
    before = len(pulse.calls)
    source.open(None)
    calls = pulse.calls[before:]
    stream_new = next(i for i, (name, _) in enumerate(calls) if name == "stream_new")
    assert not any(name.startswith("stream_") for name, _ in calls[:stream_new])
    assert [args[1] for name, args in calls[:stream_new] if name == "mainloop_prepare"] == [0] * 4
    assert not pulse.idle_steps
    assert invalidated.call_count == 6
    assert not source._events and not source._overflow and not source._server_changed
    assert source.read_chunk() == b""
    assert source.device_change is None
    source.shutdown()


def test_idle_dispatch_is_bounded() -> None:
    pulse = FakePulse()
    source = source_for(pulse)
    source.warm_up(None)
    pulse.idle_steps.extend(Step(events=[(SERVER, 0)]) for _ in range(ps.IDLE_DISPATCH_LIMIT + 1))
    before = len(pulse.calls)
    source.open(MIC.name)
    assert len(pulse.idle_steps) == 1
    assert (
        sum(name == "mainloop_prepare" and args[1] == 0 for name, args in pulse.calls[before:])
        == ps.IDLE_DISPATCH_LIMIT
    )
    assert source.device_change is None
    source.shutdown()


def test_failed_stream_ready_retry_recreates_context() -> None:
    pulse = FakePulse()
    pulse.steps.extend([Step(), Step(stream=ps.PA_STREAM_FAILED), Step()])
    source = source_for(pulse)
    source.open(MIC.name)
    assert source.is_open
    names = pulse.names()
    assert names.count("context_new") == names.count("mainloop_new") == 2
    assert names.count("context_connect") == names.count("context_subscribe") == 2
    assert names.count("stream_new") == 2 and names.count("stream_unref") == 1
    assert names.count("context_unref") == names.count("mainloop_free") == 1
    source.shutdown()
    assert pulse.names().count("stream_unref") == 2
    assert pulse.names().count("context_unref") == pulse.names().count("mainloop_free") == 2


@pytest.mark.parametrize("open_stream", [False, True])
def test_shutdown_idempotent_and_allows_reopen(open_stream: bool) -> None:
    pulse = FakePulse()
    source = source_for(pulse)
    source.warm_up(None)
    if open_stream:
        source.open(MIC.name)
    callbacks = source._subscribe_callback, source._moved_callback
    source.shutdown()
    names = pulse.names()
    assert names.count("context_disconnect") == names.count("context_unref") == 1
    assert names.count("mainloop_free") == 1
    assert names.count("stream_unref") == int(open_stream)
    unref = names.index("context_unref")
    assert pulse.calls[unref - 2] == ("context_set_subscribe_callback", (0x100000003, None, None))
    assert names[unref - 1] == "context_disconnect"
    assert names.index("context_unref") < names.index("mainloop_free")
    if open_stream:
        assert names.index("stream_unref") < names.index("context_unref")
    assert source._pulse is None
    assert source._context is source._mainloop is None
    calls = list(pulse.calls)
    source.shutdown()
    assert calls == pulse.calls
    assert callbacks == (source._subscribe_callback, source._moved_callback)
    source.open(MIC.name)
    assert source.is_open
    assert pulse.names().count("context_new") == 2
    source.shutdown()


@pytest.mark.parametrize("ack_polls", [0, 3])
def test_close_waits_for_server_disconnect_ack(ack_polls: int) -> None:
    """Постоянный context не оставляет серверный микрофон между записями (§9.5)."""
    pulse = FakePulse()
    pulse.async_disconnect = True
    pulse.disconnect_ack_polls = ack_polls
    source = opened(pulse)
    assert pulse.server_recording
    before = len(pulse.calls)
    source.close()
    assert not source.is_open
    assert not pulse.server_recording, "close вернул управление до закрытия микрофона сервером"
    assert pulse.stream_state == ps.PA_STREAM_TERMINATED
    assert "context_disconnect" not in pulse.names()[before:]
    assert pulse.names().count("context_new") == 1
    calls = pulse.names()[before:]
    assert calls.index("mainloop_dispatch") < calls.index("stream_unref")
    source.open(MIC.name)
    assert source.is_open and pulse.server_recording
    assert pulse.names().count("context_new") == 1
    source.shutdown()
    assert not pulse.server_recording


@pytest.mark.parametrize("failure", ["timeout", "poll", "disconnect"])
def test_close_disconnect_failure_drops_context_and_can_reopen(failure: str) -> None:
    """Неотвечающая служба не удерживает микрофон и не блокирует будущую запись."""
    pulse = FakePulse()
    pulse.async_disconnect = True
    source = opened(pulse)
    if failure == "timeout":
        pulse.hold_disconnect = True
    elif failure == "poll":
        pulse.fail["mainloop_poll"] = -1
    else:
        pulse.fail["stream_disconnect"] = -1
    started = pulse.clock.now
    source.close()
    assert not source.is_open and not pulse.server_recording
    assert 0 <= pulse.clock.now - started <= 0.250
    assert pulse.names().count("context_disconnect") == 1
    assert pulse.names().count("context_unref") == pulse.names().count("mainloop_free") == 1
    assert pulse.names().count("stream_unref") == 1
    pulse.fail.clear()
    pulse.hold_disconnect = False
    source.open(MIC.name)
    assert source.is_open and pulse.server_recording
    assert pulse.names().count("context_new") == 2
    source.shutdown()
    assert not pulse.server_recording


def test_concurrent_close_waits_for_inflight_server_disconnect_ack() -> None:
    """Нулевой Python stream после начала close ещё не означает закрытый микрофон."""
    pulse = FakePulse()
    pulse.async_disconnect = True
    source = opened(pulse)
    entered, release = threading.Event(), threading.Event()
    second_started, second_finished = threading.Event(), threading.Event()
    failures: list[BaseException] = []
    library = cast(FakeLibrary, pulse.lib)
    native_call = library.call

    def hold_ack(symbol: str, *args: object) -> object:
        if symbol == "pa_mainloop_dispatch" and pulse.disconnect_pending:
            entered.set()
            assert release.wait(2)
        return native_call(symbol, *args)

    def close_source(*, second: bool = False) -> None:
        if second:
            second_started.set()
        try:
            source.close()
        except BaseException as exc:
            failures.append(exc)
        finally:
            if second:
                second_finished.set()

    library.call = hold_ack
    first = threading.Thread(target=close_source)
    second = threading.Thread(target=lambda: close_source(second=True))
    first.start()
    try:
        assert entered.wait(1)
        assert pulse.server_recording
        assert "stream_unref" not in pulse.names()
        second.start()
        assert second_started.wait(1)
        assert not second_finished.wait(0.100), (
            "второй close вернулся при открытом серверном потоке"
        )
    finally:
        release.set()
        first.join(2)
        if second.ident is not None:
            second.join(2)
    assert not first.is_alive() and not second.is_alive() and not failures
    assert second_finished.is_set()
    assert not pulse.server_recording and not source.is_open
    assert pulse.names().count("stream_disconnect") == pulse.names().count("stream_unref") == 1
    assert "context_disconnect" not in pulse.names()
    source.shutdown()


def test_cancel_pending_stream_creation_drops_context_on_disconnect_failure() -> None:
    """Отмена CREATING не оставляет серверу поздний запрос на открытие записи."""
    pulse = FakePulse()
    pulse.async_disconnect = True
    source = source_for(pulse)
    source.warm_up(MIC.name)
    running = threading.Event()
    running.set()
    pulse.hold_stream = True
    pulse.fail["stream_disconnect"] = -1
    pulse.steps.append(Step(action=running.clear))
    source.open(MIC.name, running=running)
    assert not source.is_open and not pulse.server_recording
    assert pulse.names().count("context_disconnect") == 1
    assert pulse.names().count("context_unref") == 1
    assert pulse.names().count("stream_unref") == 1
    source.shutdown()


def test_warmup_skips_busy_or_recording_source() -> None:
    pulse = FakePulse()
    source = opened(pulse)
    before = list(pulse.calls)
    with source._ctx_lock:
        thread = threading.Thread(target=source.warm_up, args=(None,))
        thread.start()
        thread.join(1)
        assert not thread.is_alive()
    source.warm_up(None)
    assert pulse.calls == before
    source.shutdown()


def test_warmup_and_capture_serialize_native_calls() -> None:
    pulse = FakePulse()
    source = source_for(pulse)
    entered, release = threading.Event(), threading.Event()
    failures: list[BaseException] = []
    library = cast(FakeLibrary, pulse.lib)
    native_call = library.call

    def checked_call(symbol: str, *args: object) -> object:
        # Python 3.11 RLock не имеет locked(); проверяем владение текущим потоком.
        assert cast(Any, source._ctx_lock)._is_owned()
        if symbol == "pa_context_new":
            entered.set()
            assert release.wait(2)
        return native_call(symbol, *args)

    def capture() -> None:
        try:
            source.open(MIC.name)
            source.read_chunk()
            source.flush()
            source.latency_us()
            source.close()
        except BaseException as exc:
            failures.append(exc)

    library.call = checked_call
    warm = threading.Thread(target=source.warm_up, args=(None,), name="audio-warmup")
    owner = threading.Thread(target=capture, name="audio-capture")
    warm.start()
    try:
        assert entered.wait(2)
        owner.start()
        assert "stream_new" not in pulse.names()
    finally:
        release.set()
        warm.join(2)
        owner.join(2)
    assert not failures and not warm.is_alive() and not owner.is_alive()
    assert pulse.names().count("context_new") == 1
    assert pulse.stream_threads == {owner.ident}
    assert pulse.threads == {owner.ident, warm.ident}
    source.shutdown()


@pytest.mark.parametrize("finish", ["deadline", "cancel"])
def test_open_waiting_for_context_lock_honors_deadline_and_cancel(finish: str) -> None:
    """Занятый прогрев не растягивает бюджет и не оставляет отменённое open ждать."""
    pulse = FakePulse()
    source = ps.PulseStreamSource(
        pulse_factory=lambda: pulse,
        devices=lambda: [MIC],
        default=lambda *_a, **_kw: MIC,
    )
    running = threading.Event()
    running.set()
    entered, finished = threading.Event(), threading.Event()
    failures: list[BaseException] = []

    def open_source() -> None:
        entered.set()
        try:
            source.open(
                MIC.name,
                running=running,
                deadline=audio._OpenDeadline(time.monotonic, 0.050 if finish == "deadline" else 2),
            )
        except BaseException as exc:
            failures.append(exc)
        finally:
            finished.set()

    thread = threading.Thread(target=open_source)
    with source._ctx_lock:
        thread.start()
        try:
            assert entered.wait(1)
            if finish == "cancel":
                running.clear()
            assert finished.wait(0.400), "open игнорирует отмену/дедлайн, ожидая чужой context lock"
            assert not pulse.calls, "неполученный lock не разрешает менять нативные ресурсы"
        finally:
            running.clear()
    thread.join(2)
    assert not thread.is_alive()
    if finish == "deadline":
        assert len(failures) == 1 and isinstance(failures[0], audio.AudioError)
    else:
        assert not failures
    assert not source.is_open
    source.shutdown()


def test_fallback_shutdown_releases_primary_and_optional_simple() -> None:
    primary = Mock(spec=ps.PulseStreamSource)
    primary.open.side_effect = audio.AudioApiUnavailable(audio.ERROR_FAILED, "private")
    simple = Mock(spec=ps.PulseStreamSource)
    wrapper = ps.StreamWithFallback(primary, fallback=lambda: simple)
    wrapper.open(None)
    wrapper.shutdown()
    primary.shutdown.assert_called_once_with()
    simple.shutdown.assert_called_once_with()


def test_reconnect_failure_logs_once_without_native_error(caplog: pytest.LogCaptureFixture) -> None:
    pulse = FakePulse()
    source = opened(pulse)
    source.close()
    pulse.context_state = ps.PA_CONTEXT_FAILED
    pulse.fail["context_connect"] = -1
    with caplog.at_level("INFO", logger=ps.__name__), pytest.raises(audio.AudioError):
        source.open(MIC.name)
    assert [r.getMessage() for r in caplog.records] == ["Соединение со звуковой службой потеряно"]
    assert pulse.names().count("context_new") == 1 + audio.OPEN_RETRIES
    assert pulse.names().count("context_unref") == 1 + audio.OPEN_RETRIES
    source.shutdown()


@pytest.mark.parametrize("error", [audio.PA_ERR_ACCESS, audio.PA_ERR_BUSY, audio.PA_ERR_NOENTITY])
def test_warmup_native_failure_keeps_previous_result_code(
    error: int, caplog: pytest.LogCaptureFixture
) -> None:
    pulse = FakePulse()
    pulse.error = error
    pulse.fail["context_connect"] = -1
    with caplog.at_level("INFO", logger=ps.__name__):
        source_for(pulse).warm_up(MIC.name)
    assert len(caplog.records) == 1
    assert caplog.records[0].getMessage().endswith("итог=audio-failed")
    assert pulse.names().count("context_unref") == pulse.names().count("mainloop_free") == 1


@pytest.fixture
def introspection(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[ps.PulseStreamSource, FakePulse]:
    """Боевой выбор и настоящий реестр поверх ctypes-фейка, процессы запрещены."""
    monkeypatch.setattr(ps, "list_devices", list_devices)
    monkeypatch.setattr(audio, "list_devices", list_devices)
    monkeypatch.setattr(
        subprocess, "run", Mock(side_effect=AssertionError("Не должно быть subprocess.run"))
    )
    pulse = FakePulse()
    source = ps.PulseStreamSource(
        pulse_factory=lambda: pulse, clock=pulse.clock, sleep=pulse.clock.sleep
    )
    return source, pulse


@pytest.mark.parametrize("device", ["нет.такого", None])
def test_production_selection_error_does_not_retry(
    introspection: tuple[ps.PulseStreamSource, FakePulse],
    monkeypatch: pytest.MonkeyPatch,
    device: str | None,
) -> None:
    source, pulse = introspection
    if device is None:
        pulse.sources = [(9, b"sound.monitor", b"Sound")]
        pulse.default_source = b"sound.monitor"
    pause = Mock(wraps=pulse.clock.sleep)
    monkeypatch.setattr(source, "_sleep", pause)
    invalidated = Mock()
    monkeypatch.setattr(ps, "invalidate_device_cache", invalidated)
    with pytest.raises(audio.AudioError) as exc:
        source.open(device)
    assert exc.value.code == audio.ERROR_NO_DEVICE
    assert "stream_new" not in pulse.names()
    assert pulse.names().count("context_get_source_info_list") == 1
    assert pulse.names().count("context_new") == 1
    pause.assert_not_called()
    # Только READY контекста и две операции интроспекции, без OPEN_RETRY_MS.
    assert pulse.clock.now == pytest.approx(3 * ps.POLL_US / 1_000_000)
    invalidated.assert_called_once_with()
    assert not source.is_open and source.selected_device is None
    source.shutdown()


@pytest.mark.parametrize("recreate_context", [False, True])
def test_production_retry_reuses_selected_device(
    introspection: tuple[ps.PulseStreamSource, FakePulse],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    recreate_context: bool,
) -> None:
    source, pulse = introspection
    select = Mock(wraps=source._select_device)
    monkeypatch.setattr(source, "_select_device", select)
    pulse.steps.extend(
        [
            Step(),
            Step(),
            Step(),
            Step(
                context=ps.PA_CONTEXT_FAILED if recreate_context else None,
                stream=ps.PA_STREAM_FAILED,
            ),
        ]
    )
    with caplog.at_level("INFO", logger=ps.__name__):
        source.open(MIC.name)
    select.assert_called_once()
    assert source.is_open and source.selected_device == MIC
    assert pulse.names().count("context_new") == 2
    assert pulse.names().count("stream_new") == 2
    assert pulse.names().count("context_get_source_info_list") == 1
    selection_ms = round(2 * ps.POLL_US / 1000)
    connection_ms = round(pulse.clock.now * 1000) - selection_ms
    assert caplog.messages[-1] == (
        f"Источник записи открыт: {MIC.label} выбор_ms={selection_ms} "
        f"подключение_ms={connection_ms} попытка=2 контекст=холодный"
    )
    source.shutdown()


@pytest.mark.skipif(ctypes.sizeof(ctypes.c_void_p) != 8, reason="Смещения ABI для 64 бит")
def test_introspection_prefix_offsets() -> None:
    assert ps._PaSourceInfoPrefix.description.offset == 16
    assert ps._PaServerInfoPrefix.default_source_name.offset == 56
    assert ps._PaServerInfoPrefix.sample_spec.offset == 32
    assert ps._PaServerInfoPrefix.default_sink_name.offset == 48


@pytest.mark.parametrize(
    "missing", ["pa_context_get_source_info_list", "pa_context_get_server_info"]
)
def test_optional_introspection_abi(monkeypatch: pytest.MonkeyPatch, missing: str) -> None:
    class OldLibrary(FakeLibrary):
        def __getattr__(self, name: str) -> Any:
            if name == missing:
                raise AttributeError(name)
            return super().__getattr__(name)

    lib = OldLibrary()
    monkeypatch.setattr(ctypes, "CDLL", Mock(return_value=lib))
    pulse = ps._PulseAsync()
    assert not pulse.has_introspection
    assert lib.functions["pa_stream_new"].assigned["restype"] is ctypes.c_void_p


def test_introspection_callback_abi(monkeypatch: pytest.MonkeyPatch) -> None:
    lib = FakeLibrary()
    monkeypatch.setattr(ctypes, "CDLL", Mock(return_value=lib))
    assert ps._PulseAsync().has_introspection
    for symbol, callback in (
        ("pa_context_get_source_info_list", ps.SourceInfoCallback),
        ("pa_context_get_server_info", ps.ServerInfoCallback),
    ):
        assert lib.functions[symbol].assigned == {
            "restype": ctypes.c_void_p,
            "argtypes": [ctypes.c_void_p, callback, ctypes.c_void_p],
        }
    assert tuple(ps.SourceInfoCallback._argtypes_) == (
        ctypes.c_void_p,
        ctypes.POINTER(ps._PaSourceInfoPrefix),
        ctypes.c_int,
        ctypes.c_void_p,
    )
    assert tuple(ps.ServerInfoCallback._argtypes_) == (
        ctypes.c_void_p,
        ctypes.POINTER(ps._PaServerInfoPrefix),
        ctypes.c_void_p,
    )
    assert ps.SourceInfoCallback._restype_ is ps.ServerInfoCallback._restype_ is None


@pytest.mark.parametrize("warmup", [False, True])
def test_introspection_cached_across_openings_without_processes(
    introspection: tuple[ps.PulseStreamSource, FakePulse],
    warmup: bool,
    caplog: pytest.LogCaptureFixture,
) -> None:
    source, pulse = introspection
    callbacks = source._source_info_callback, source._server_info_callback
    with caplog.at_level("INFO"):
        if warmup:
            thread = threading.Thread(target=source.warm_up, args=(None,), name="audio-warmup")
            thread.start()
            thread.join(2)
            assert not thread.is_alive()
            assert not any(name.startswith("stream_") for name in pulse.names())
        for _ in range(4):
            source.open(None)
            assert source.selected_device == MIC
            source.close()
            before = len(pulse.calls)
            pulse.clock.sleep(100)  # У событийного кэша нет TTL.
            assert list_devices() == [MIC]
            assert default_device([MIC]) == MIC
            source.warm_up(None)
            assert not any(name.startswith("stream_") for name in pulse.names()[before:])
    assert pulse.names().count("context_get_source_info_list") == 1
    assert pulse.names().count("context_get_server_info") == 1
    assert pulse.names().count("stream_new") == pulse.names().count("stream_unref") == 4
    assert pulse.names().count("context_new") == 1
    opened_lines = [line for line in caplog.messages if line.startswith("Источник записи открыт:")]
    assert len(opened_lines) == 4
    assert all(
        line.startswith(f"Источник записи открыт: {MIC.label} выбор_ms=") for line in opened_lines
    )
    assert all(MIC.name not in line for line in caplog.messages)
    assert callbacks == (source._source_info_callback, source._server_info_callback)
    source.shutdown()


@pytest.mark.parametrize("event", [ps.PA_SUBSCRIPTION_EVENT_SOURCE, REMOVE, SERVER])
def test_idle_event_refreshes_devices_and_default(
    introspection: tuple[ps.PulseStreamSource, FakePulse],
    event: int,
) -> None:
    source, pulse = introspection
    source.open(None)
    source.close()
    pulse.sources.append((OTHER.index, OTHER.name.encode(), OTHER.description.encode()))
    pulse.default_source = pulse.name = OTHER.name.encode()
    pulse.index = OTHER.index
    pulse.idle_steps.append(Step(events=[(event, MIC.index)]))
    source.open(None)
    assert source.selected_device == OTHER
    assert pulse.names().count("context_get_source_info_list") == 2
    assert pulse.names().count("context_get_server_info") == 2
    assert source.device_change is None
    source.shutdown()


def test_provider_dispatches_idle_events_before_cached_list(
    introspection: tuple[ps.PulseStreamSource, FakePulse],
) -> None:
    source, pulse = introspection
    source.warm_up(None)
    pulse.sources = [(OTHER.index, OTHER.name.encode(), OTHER.description.encode())]
    pulse.default_source = OTHER.name.encode()
    pulse.idle_steps.append(Step(events=[(SERVER, 0)]))
    assert list_devices() == [OTHER]
    assert default_device([MIC]) == OTHER  # Имя и список принадлежат одному снимку.
    assert pulse.names().count("context_get_server_info") == 2
    assert not any(name.startswith("stream_") for name in pulse.names())
    source.shutdown()


def test_provider_idle_dispatch_limit(
    introspection: tuple[ps.PulseStreamSource, FakePulse],
) -> None:
    source, pulse = introspection
    source.warm_up(None)
    pulse.idle_steps.extend(Step() for _ in range(ps.IDLE_DISPATCH_LIMIT + 1))
    assert list_devices() == [MIC]
    assert len(pulse.idle_steps) == 1
    source.shutdown()


@pytest.mark.parametrize(
    "error", ["eol", "no-eol", "null-server", "null-name", "null-op", "timeout"]
)
def test_introspection_errors_cancel_and_keep_cache_stale(
    introspection: tuple[ps.PulseStreamSource, FakePulse],
    error: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    source, pulse = introspection
    source.warm_up(None)
    source._introspection_stale = True
    if error == "eol":
        pulse.source_eol = -1
    elif error == "no-eol":
        pulse.source_eol = 0
    elif error == "null-server":
        pulse.server_info_null = True
    elif error == "null-name":
        pulse.sources = [(1, None, b"private description")]
    elif error == "null-op":
        pulse.fail["context_get_source_info_list"] = None
    else:
        pulse.hold_introspection = True
    before = len(pulse.calls)
    with caplog.at_level("DEBUG"):
        assert source._provide_devices(audio._OpenDeadline(pulse.clock, 0.1)) is None
    assert source._introspection_stale
    assert source._info_rows is None
    assert not any(name.startswith("stream_") for name in pulse.names()[before:])
    if error == "timeout":
        assert pulse.names().count("operation_cancel") == 1
        assert pulse.calls[-1][0] == "operation_unref"
    assert "private description" not in caplog.text
    source.shutdown()


def test_introspection_decodes_and_sorts(
    introspection: tuple[ps.PulseStreamSource, FakePulse],
) -> None:
    source, pulse = introspection
    pulse.sources = [
        (9, b"speaker.monitor", b" Sound \n system\x00"),
        (2, b"mic.\xff", b"  USB\xe2\x80\x8b \xff\n mic "),
    ]
    pulse.default_source = b"mic.\xff"
    source.warm_up(None)
    devices = list_devices()
    assert devices == [
        audio.AudioDevice(2, "mic.�", "USB � mic", False),
        audio.AudioDevice(9, "speaker.monitor", "Sound system", True),
    ]
    assert all(device.index_exact for device in devices)
    assert default_device(devices) == devices[0]
    source.shutdown()


def test_recording_check_list_reuses_lock_after_dispatch(
    introspection: tuple[ps.PulseStreamSource, FakePulse],
) -> None:
    source, pulse = introspection
    source.open(None)
    pulse.sources = [(OTHER.index, OTHER.name.encode(), OTHER.description.encode())]
    pulse.default_source = OTHER.name.encode()
    dispatch = pulse.dispatch
    inside_dispatch = False

    def guarded_dispatch() -> None:
        nonlocal inside_dispatch
        assert not inside_dispatch, "mainloop dispatch не реентерабелен"
        inside_dispatch = True
        try:
            dispatch()
        finally:
            inside_dispatch = False

    pulse.dispatch = guarded_dispatch  # type: ignore[method-assign]
    # Переполнение сверяет список из read_chunk под уже взятым RLock.
    pulse.steps.append(Step(events=[(REMOVE, MIC.index + 100)] * (ps.EVENT_QUEUE_LIMIT + 1)))
    assert source.read_chunk() is None
    change = source.device_change
    assert change is not None and change.fresh is not None
    assert change.fresh.devices == (OTHER,) and change.fresh.default == OTHER
    assert pulse.names().count("context_get_source_info_list") == 2
    assert pulse.names().count("context_get_server_info") == 2
    source.shutdown()


def test_provider_busy_from_another_thread_falls_back(
    introspection: tuple[ps.PulseStreamSource, FakePulse],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, pulse = introspection
    source.open(None)
    run = Mock(return_value=subprocess.CompletedProcess([], 0, MIC.name, ""))
    monkeypatch.setattr(subprocess, "run", run)
    result: list[audio.AudioDevice] = []
    with source._ctx_lock:
        started = time.monotonic()
        thread = threading.Thread(target=lambda: result.append(default_device([MIC])))
        thread.start()
        thread.join(1)
        assert not thread.is_alive()
        assert 0.15 <= time.monotonic() - started < 1
    assert result == [MIC]
    assert run.call_args.args[0] == ["pactl", "get-default-source"]
    assert pulse.names().count("context_get_server_info") == 1
    source.shutdown()


def test_shutdown_does_not_unregister_another_source(
    introspection: tuple[ps.PulseStreamSource, FakePulse],
) -> None:
    first, pulse = introspection
    first.warm_up(None)
    other_pulse = FakePulse()
    second = ps.PulseStreamSource(pulse_factory=lambda: other_pulse, clock=other_pulse.clock)
    second.warm_up(None)
    first.shutdown()
    assert list_devices() == [MIC]
    assert other_pulse.names().count("context_get_source_info_list") == 1
    second.shutdown()
    assert audio._device_introspection is None


@pytest.mark.parametrize("service_fail", [False, True])
def test_production_warmup_selection_errors_keep_result_codes(
    introspection: tuple[ps.PulseStreamSource, FakePulse],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    service_fail: bool,
) -> None:
    source, pulse = introspection
    pulse.sources = []
    if service_fail:
        pulse.fail["context_connect"] = -1
        monkeypatch.setattr(
            subprocess, "run", Mock(return_value=subprocess.CompletedProcess([], 0, "", ""))
        )
    with caplog.at_level("INFO"):
        source.warm_up("missing.private")
    result = "audio-failed" if service_fail else "ok"
    assert caplog.messages == [
        f"Прогрев пути записи без микрофона: t_ms={round(pulse.clock.now * 1000)} "
        f"итог=device-audio-no-device,service-{result}"
    ]
    assert not any(name.startswith("stream_") for name in pulse.names())
    source.shutdown()


@pytest.mark.parametrize("wrapped", [False, True])
def test_capture_introspection_ready_and_report_log_labels_without_names(
    introspection: tuple[ps.PulseStreamSource, FakePulse],
    wrapped: bool,
    caplog: pytest.LogCaptureFixture,
) -> None:
    source, pulse = introspection
    active = ps.StreamWithFallback(source) if wrapped else source
    events, errors, changed = Mock(), Mock(), Mock()
    capture = audio.AudioCapture(
        source=active,
        on_samples=Mock(return_value=False),
        on_event=events,
        on_error=errors,
        on_device_change=changed,
        clock=pulse.clock,
    )
    pulse.fragments.append(b"\x01\x00" * 320)
    with caplog.at_level("INFO"):
        capture.start("private", None, limit_s=60)
        thread = capture._thread
        assert thread is not None
        thread.join(2)
        assert not thread.is_alive()
        ready = next(
            call.args[0] for call in events.call_args_list if call.args[0]["type"] == "audio.ready"
        )
        assert ready["device"] == MIC.label
        pulse.sources = [(OTHER.index, OTHER.name.encode(), OTHER.description.encode())]
        pulse.default_source = OTHER.name.encode()
        pulse.idle_steps.append(Step(events=[(REMOVE, MIC.index), (SERVER, 0)]))
        # Обычный callback захвата — после read_chunk; провайдер берёт свой замок.
        capture._report_device_change(
            "private", audio.DeviceChange(audio.REASON_REMOVED, MIC, True), threading.Event()
        )
    errors.assert_not_called()
    changed.assert_called_once_with("private", audio.KIND_SWITCHED, OTHER.label)
    assert pulse.names().count("context_get_source_info_list") == 2
    assert pulse.names().count("context_get_server_info") == 2
    assert any(
        line.startswith(f"Источник записи готов: {MIC.label} t_ms=") for line in caplog.messages
    )
    assert any(
        line.startswith("Смена устройства записи:") and line.endswith(f"label={OTHER.label}")
        for line in caplog.messages
    )
    assert all(MIC.name not in line and OTHER.name not in line for line in caplog.messages)
    capture.shutdown()


@pytest.mark.parametrize("stage", ["source", "server"])
def test_failed_introspection_uses_pactl_without_retrying_provider(
    introspection: tuple[ps.PulseStreamSource, FakePulse],
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    source, pulse = introspection
    if stage == "source":
        pulse.source_eol = -1
    else:
        pulse.server_info_null = True

    def run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if args == ["pactl", "list", "short", "sources"]:
            output = f"{MIC.index}\t{MIC.name}"
        elif args == ["pactl", "-f", "json", "list", "sources"]:
            output = '[{"name": "mic.test", "description": "Тестовый микрофон"}]'
        else:
            assert args == ["pactl", "get-default-source"]
            output = MIC.name
        return subprocess.CompletedProcess(args, 0, output, "")

    commands = Mock(wraps=run)
    monkeypatch.setattr(subprocess, "run", commands)
    source.open(None)
    assert source.selected_device == MIC
    assert commands.call_count == 3
    assert pulse.names().count("context_get_source_info_list") == 1
    assert pulse.names().count("context_get_server_info") == int(stage == "server")
    source.shutdown()


def test_event_during_introspection_keeps_cache_stale(
    introspection: tuple[ps.PulseStreamSource, FakePulse],
) -> None:
    source, pulse = introspection
    source.warm_up(None)
    source._introspection_stale = True
    pulse.steps.append(Step(events=[(SERVER, 0)]))
    assert list_devices() == [MIC]
    assert source._introspection_stale
    assert list_devices() == [MIC]
    assert not source._introspection_stale
    assert pulse.names().count("context_get_source_info_list") == 3
    source.shutdown()


@pytest.mark.parametrize("state", [ps.PA_CONTEXT_FAILED, ps.PA_CONTEXT_TERMINATED])
def test_reconnected_context_refreshes_default_without_subscription_event(
    introspection: tuple[ps.PulseStreamSource, FakePulse], state: int
) -> None:
    """После обрыва прежние имя/индекс не переживают новый снимок службы."""
    source, pulse = introspection
    source.open(None)
    source.close()
    pulse.context_state = state
    pulse.sources = [(OTHER.index, OTHER.name.encode(), OTHER.description.encode())]
    pulse.default_source = pulse.name = OTHER.name.encode()
    pulse.index = OTHER.index
    source.open(None)
    assert source.selected_device == OTHER
    assert pulse.names().count("context_new") == 2
    assert pulse.names().count("context_get_source_info_list") == 2
    assert pulse.names().count("context_get_server_info") == 2
    assert source.device_change is None
    source.shutdown()


def test_subscribe_callback_survives_gc_between_recordings(
    introspection: tuple[ps.PulseStreamSource, FakePulse],
) -> None:
    """Python-владелец сохраняет callback, когда libpulse держит лишь адрес."""
    source, pulse = introspection
    source.warm_up(None)
    callback = pulse.subscribe_callback
    assert callback is not None
    callback_ref = weakref.ref(callback)
    pulse.subscribe_callback = weakref.proxy(callback)
    # История фейка также не должна случайно удерживать Python callback.
    pulse.calls.clear()
    del callback
    gc.collect()
    assert callback_ref() is not None
    pulse.sources = [(OTHER.index, OTHER.name.encode(), OTHER.description.encode())]
    pulse.default_source = pulse.name = OTHER.name.encode()
    pulse.index = OTHER.index
    pulse.idle_steps.append(Step(events=[(SERVER, 0)]))
    source.open(None)
    assert source.selected_device == OTHER
    source.close()
    pulse.calls.clear()
    gc.collect()
    assert callback_ref() is not None
    pulse.sources = [(MIC.index, MIC.name.encode(), MIC.description.encode())]
    pulse.default_source = pulse.name = MIC.name.encode()
    pulse.index = MIC.index
    pulse.idle_steps.append(Step(events=[(SERVER, 0)]))
    source.open(None)
    assert source.selected_device == MIC
    source.shutdown()
