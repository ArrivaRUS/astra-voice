"""Автомат, очередь, отмена, PCM и замеры на фейках без тяжёлого рантайма."""

from __future__ import annotations

import importlib
import json
import logging
import struct
import subprocess
import sys
import wave
from array import array
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from itertools import repeat
from pathlib import Path
from queue import Empty, Queue
from threading import Event
from types import ModuleType
from typing import Any
from unittest.mock import Mock

import pytest

from astra_voice.core.version import __version__
from astra_voice.worker import state as state_module
from astra_voice.worker.ipc import BAD_FIELD, FrameError, decode, encode
from astra_voice.worker.state import (
    LIMIT_S_DEFAULT,
    SAMPLE_RATE,
    STOPPED_TTL_S_DEFAULT,
    Message,
    State,
    WorkerState,
    join_segment_texts,
)

# tests не пакет; общий каталог фейков добавляется без нового conftest.py.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fakes import (  # noqa: E402
    Cancellation,
    FakeAudioSource,
    FakeCancelToken,
    FakeEngine,
    FakeLoadResult,
    FakeTranscribeResult,
)

pytestmark = pytest.mark.unit
Factory = Callable[..., tuple[WorkerState, FakeEngine, Queue[Message]]]


class FakeVAD(ModuleType):
    """Подменяет весь модуль, исключая загрузку настоящей модели в тестах автомата."""

    def __init__(self) -> None:
        super().__init__("astra_voice.worker.vad")
        self.vad_available = Mock(return_value=False)
        self.trim_trailing_silence = Mock(side_effect=lambda audio, *args, **kwargs: audio)
        self.segment = Mock(return_value=[])


class SegmentEngine(FakeEngine):
    """Выдаёт тексты по порядку и позволяет отменить работу на границе вызовов."""

    def __init__(self, parts: list[str], after_segment: Callable[[], None] = lambda: None) -> None:
        super().__init__()
        self.parts = parts
        self.after_segment = after_segment

    def transcribe(self, audio: Any, cancel: Cancellation) -> FakeTranscribeResult:
        self.text = self.parts[self.transcribe_calls]
        result = super().transcribe(audio, cancel)
        self.after_segment()
        return result


@pytest.fixture(autouse=True)
def fake_vad(monkeypatch: pytest.MonkeyPatch) -> FakeVAD:
    module = FakeVAD()
    monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.setattr(state_module, "_vad_unavailable_warned", False)
    return module


def load_message(**changes: Any) -> Message:
    """Создаёт валидную команду загрузки с возможностью менять поля."""
    return {
        "type": "model.load",
        "id": "gigaam",
        "revision": "r1",
        "dir": "/model",
        "layout": "fake",
        "variant": "int8",
        "threads": 2,
        "min_ram_mb": 512,
        **changes,
    }


@pytest.fixture
def factory(tmp_path: Path) -> Iterator[Factory]:
    """Освобождает фоновые потоки даже при неудачной проверке теста."""
    workers: list[tuple[WorkerState, FakeEngine]] = []

    def create(
        engine: FakeEngine | None = None, *, loaded: bool = True, **kwargs: Any
    ) -> tuple[WorkerState, FakeEngine, Queue[Message]]:
        fake = engine if engine is not None else FakeEngine()
        events: Queue[Message] = Queue()
        if kwargs.pop("numpy_audio", False) is False:
            kwargs.setdefault("audio_factory", list)
        worker = WorkerState(
            engine_factory=lambda layout: fake,
            cancel_factory=kwargs.pop("cancel_factory", FakeCancelToken),
            data_dir_factory=lambda: tmp_path,
            on_event=events.put,
            **kwargs,
        )
        workers.append((worker, fake))
        if loaded:
            assert worker.handle(load_message())[0]["type"] == "model.loaded"
        return worker, fake, events

    yield create
    for worker, fake in workers:
        if fake.gate is not None:
            fake.gate.set()
        worker.close()


def assert_state(worker: WorkerState, expected: State) -> None:
    """Проверяет состояние заново после команды или фонового завершения."""
    assert worker.state is expected


def command(worker: WorkerState, kind: str, uid: str = "u1") -> list[Message]:
    """Посылает команду одной utterance."""
    return worker.handle({"type": kind, "utterance_id": uid})


def start(worker: WorkerState, uid: str = "u1") -> None:
    """Создаёт и наполняет отдельный буфер."""
    assert command(worker, "record.start", uid) == []
    worker.feed_audio(uid, [0.25, -0.5])


def recognize(worker: WorkerState, uid: str = "u1") -> None:
    """Ставит распознавание без синхронного результата."""
    assert command(worker, "recognize", uid) == []


def record_samples(worker: WorkerState, count: int) -> None:
    """Проводит запись через record.stop до запроса распознавания."""
    assert command(worker, "record.start") == []
    worker.feed_audio("u1", repeat(0.25, count))
    assert command(worker, "record.stop") == []


@pytest.mark.parametrize(
    "parts,expected",
    [
        ([], ""),
        (["", "  ", "\n"], ""),
        (["  раз  два ", "", " три\nчетыре "], "раз два три четыре"),
        (["Привет", " , мир", "."], "Привет, мир."),
        (["Готово.", " . Далее"], "Готово. Далее"),
        (["Готово.", "...", " Далее"], "Готово. Далее"),
        (["Да!", "! Конечно"], "Да! Конечно"),
        (["Да?", "? Правда"], "Да? Правда"),
        (["Жду…", "… Продолжение"], "Жду… Продолжение"),
        (["Да?", "!"], "Да?!"),
        (["Первое", "; второе", ": третье", "…"], "Первое; второе: третье…"),
    ],
)
def test_join_segment_texts(parts: list[str], expected: str) -> None:
    assert join_segment_texts(parts) == expected
    assert "  " not in join_segment_texts(parts)


@pytest.mark.parametrize(
    "parts,expected",
    [
        (["  Привет ", " ,  мир. ", " .  Дальше! "], "Привет, мир. Дальше!"),
        (["  Первый ", " . Второй ", " , третий. "], "Первый. Второй, третий."),
    ],
)
def test_vad_segments_real_recognition_path(
    factory: Factory, fake_vad: FakeVAD, parts: list[str], expected: str
) -> None:
    np = pytest.importorskip("numpy")
    now = 10.0

    def advance_clock() -> None:
        nonlocal now
        now += 0.25

    token = FakeCancelToken()
    worker, engine, events = factory(
        SegmentEngine(parts, advance_clock),
        numpy_audio=True,
        cancel_factory=lambda: token,
        clock=lambda: now,
    )
    fake_vad.vad_available.return_value = True
    trimmed = np.arange(49 * SAMPLE_RATE, dtype=np.float32)
    fake_vad.trim_trailing_silence.side_effect = None
    fake_vad.trim_trailing_silence.return_value = trimmed
    boundaries = [
        (0, 20 * SAMPLE_RATE),
        (20 * SAMPLE_RATE, 40 * SAMPLE_RATE),
        (40 * SAMPLE_RATE, len(trimmed)),
    ]
    fake_vad.segment.return_value = boundaries
    record_samples(worker, 50 * SAMPLE_RATE)
    assert engine.transcribe_calls == 0
    recognize(worker)
    assert events.get(timeout=2) == {
        "type": "result",
        "utterance_id": "u1",
        "text": expected,
        "t_ms": 750,
    }
    fake_vad.trim_trailing_silence.assert_called_once()
    trim_call = fake_vad.trim_trailing_silence.call_args
    assert len(trim_call.args[0]) == 50 * SAMPLE_RATE
    assert trim_call.args[1:] == (SAMPLE_RATE,)
    assert trim_call.kwargs == {"cancel": token}
    fake_vad.segment.assert_called_once_with(trimmed, SAMPLE_RATE, max_window_s=24.0, cancel=token)
    assert engine.transcribe_calls == 3
    assert engine.tokens == [token] * 3
    for audio, (start_index, stop_index) in zip(engine.audios, boundaries, strict=True):
        np.testing.assert_array_equal(audio, trimmed[start_index:stop_index])
        assert np.shares_memory(audio, trimmed)
    assert worker.buffers == {}


@pytest.mark.parametrize("count", [0, SAMPLE_RATE, 20 * SAMPLE_RATE])
def test_vad_short_recording_transcribed_once(
    factory: Factory, fake_vad: FakeVAD, count: int
) -> None:
    pytest.importorskip("numpy")
    fake_vad.vad_available.return_value = True
    fake_vad.segment.return_value = [(0, 1), (1, count)]
    worker, engine, events = factory(numpy_audio=True)
    record_samples(worker, count)
    recognize(worker)
    assert events.get(timeout=2)["text"] == engine.text
    assert engine.transcribe_calls == 1
    assert len(engine.audios[0]) == count
    fake_vad.trim_trailing_silence.assert_called_once()
    fake_vad.segment.assert_not_called()


def test_vad_duration_uses_trimmed_audio(factory: Factory, fake_vad: FakeVAD) -> None:
    np = pytest.importorskip("numpy")
    fake_vad.vad_available.return_value = True
    trimmed = np.zeros(20 * SAMPLE_RATE, dtype=np.float32)
    fake_vad.trim_trailing_silence.side_effect = None
    fake_vad.trim_trailing_silence.return_value = trimmed
    worker, engine, events = factory(numpy_audio=True)
    record_samples(worker, 25 * SAMPLE_RATE)
    recognize(worker)
    assert events.get(timeout=2)["type"] == "result"
    fake_vad.segment.assert_not_called()
    assert engine.transcribe_calls == 1
    assert engine.audios[0] is trimmed


@pytest.mark.parametrize("boundaries", [[], [(0, 25 * SAMPLE_RATE)]])
def test_vad_empty_or_single_segment_keeps_whole_audio(
    factory: Factory, fake_vad: FakeVAD, boundaries: list[tuple[int, int]]
) -> None:
    pytest.importorskip("numpy")
    fake_vad.vad_available.return_value = True
    fake_vad.segment.return_value = boundaries
    worker, engine, events = factory(FakeEngine("  как раньше  "), numpy_audio=True)
    record_samples(worker, 25 * SAMPLE_RATE)
    recognize(worker)
    assert events.get(timeout=2)["text"] == "  как раньше  "
    fake_vad.segment.assert_called_once()
    assert engine.transcribe_calls == 1
    assert len(engine.audios[0]) == 25 * SAMPLE_RATE


@pytest.mark.parametrize("cancel_by_result", [False, True])
def test_vad_cancel_between_segments(
    factory: Factory, fake_vad: FakeVAD, cancel_by_result: bool
) -> None:
    pytest.importorskip("numpy")
    token = FakeCancelToken()
    engine = SegmentEngine(["первый", "второй", "третий"])
    if cancel_by_result:
        engine.cancelled_result = True
    else:
        engine.after_segment = token.cancel
    fake_vad.vad_available.return_value = True
    fake_vad.segment.return_value = [
        (0, 10 * SAMPLE_RATE),
        (10 * SAMPLE_RATE, 20 * SAMPLE_RATE),
        (20 * SAMPLE_RATE, 30 * SAMPLE_RATE),
    ]
    worker, _, events = factory(engine, numpy_audio=True, cancel_factory=lambda: token)
    record_samples(worker, 30 * SAMPLE_RATE)
    recognize(worker)
    assert events.get(timeout=1) == {"type": "cancelled", "utterance_id": "u1"}
    assert engine.transcribe_calls == 1
    assert len(engine.audios[0]) == 10 * SAMPLE_RATE
    assert worker.buffers == {}
    assert events.empty()


@pytest.mark.parametrize("stage", ["trim_trailing_silence", "segment"])
def test_vad_cancellation_skips_engine(factory: Factory, fake_vad: FakeVAD, stage: str) -> None:
    pytest.importorskip("numpy")
    token = FakeCancelToken()
    fake_vad.vad_available.return_value = True

    def cancel_during_vad(audio: Any, *args: Any, **kwargs: Any) -> Any:
        token.cancel()
        return audio if stage == "trim_trailing_silence" else []

    getattr(fake_vad, stage).side_effect = cancel_during_vad
    worker, engine, events = factory(numpy_audio=True, cancel_factory=lambda: token)
    record_samples(worker, 25 * SAMPLE_RATE)
    recognize(worker)
    assert events.get(timeout=1) == {"type": "cancelled", "utterance_id": "u1"}
    assert engine.transcribe_calls == 0
    if stage == "trim_trailing_silence":
        fake_vad.segment.assert_not_called()


@pytest.mark.parametrize("failure", ["unavailable", "own_warning", "import", "availability"])
def test_vad_unavailable_warns_once_per_process(
    factory: Factory,
    fake_vad: FakeVAD,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failure: str,
) -> None:
    pytest.importorskip("numpy")
    caplog.set_level(logging.WARNING)
    if failure == "import":
        real_import = importlib.import_module

        def fail_vad_import(name: str, package: str | None = None) -> ModuleType:
            if name == "astra_voice.worker.vad":
                raise RuntimeError("ошибка инициализации модуля")
            return real_import(name, package)

        monkeypatch.setattr(importlib, "import_module", fail_vad_import)
    elif failure == "own_warning":

        def unavailable_with_warning() -> bool:
            logging.getLogger(fake_vad.__name__).warning(
                "Не удалось включить определение речи. Запись будет обработана целиком."
            )
            return False

        fake_vad.vad_available.side_effect = unavailable_with_warning
    elif failure == "availability":
        fake_vad.vad_available.side_effect = RuntimeError("ошибка проверки доступности")

    for _ in range(2):
        # Новый автомат также не должен сбрасывать флаг уровня процесса.
        worker, engine, events = factory(FakeEngine("  исходный текст  "), numpy_audio=True)
        record_samples(worker, 25 * SAMPLE_RATE)
        recognize(worker)
        assert events.get(timeout=2)["text"] == "  исходный текст  "
        assert engine.transcribe_calls == 1
        assert len(engine.audios[0]) == 25 * SAMPLE_RATE
        assert len(caplog.records) == 1
        assert caplog.records[0].levelno == logging.WARNING
        assert "Запись будет обработана целиком" in caplog.text
    fake_vad.trim_trailing_silence.assert_not_called()
    fake_vad.segment.assert_not_called()


def test_vad_skips_list_audio(factory: Factory, fake_vad: FakeVAD) -> None:
    fake_vad.vad_available.return_value = True
    worker, engine, events = factory()
    record_samples(worker, 25 * SAMPLE_RATE)
    recognize(worker)
    assert events.get(timeout=2)["text"] == engine.text
    assert engine.transcribe_calls == 1
    assert isinstance(engine.audios[0], list)
    fake_vad.trim_trailing_silence.assert_not_called()
    fake_vad.segment.assert_not_called()


def test_all_states_and_pcm(factory: Factory) -> None:
    gate = Event()
    source = FakeAudioSource([[0.25], [-0.5]])
    worker, engine, events = factory(FakeEngine(gate=gate), audio_source=source)
    assert set(State) == {State.idle, State.recording, State.processing, State.recording_processing}
    assert_state(worker, State.idle)
    assert command(worker, "record.start") == []
    assert worker.buffers == {"u1": array("f")}
    source.feed("u1", worker.feed_audio)
    assert worker.buffers == {"u1": array("f", [0.25, -0.5])}
    assert_state(worker, State.recording)
    recognize(worker)
    assert engine.started.wait(2)
    assert_state(worker, State.processing)
    start(worker, "u2")
    assert_state(worker, State.recording_processing)
    assert command(worker, "record.stop", "u2") == []
    assert_state(worker, State.processing)
    gate.set()
    msg = events.get(timeout=2)
    assert msg["type"] == "result"
    assert msg["utterance_id"] == "u1"
    assert type(msg["t_ms"]) is int
    assert msg["t_ms"] >= 0
    assert engine.audios == [[0.25, -0.5]]
    assert_state(worker, State.idle)
    assert worker.buffers == {"u2": array("f", [0.25, -0.5])}


def test_stop_without_recognize_preserves_pcm(factory: Factory) -> None:
    worker, _, _ = factory()
    start(worker)
    assert command(worker, "record.stop") == []
    assert_state(worker, State.idle)
    saved = worker.buffers["u1"]
    assert saved == array("f", [0.25, -0.5])
    worker.feed_audio("u1", [1.0])
    assert worker.buffers["u1"] is saved
    assert saved == array("f", [0.25, -0.5])


def test_stop_then_recognize_cleans_only_after_result(factory: Factory) -> None:
    gate = Event()
    worker, engine, events = factory(FakeEngine(gate=gate))
    start(worker)
    saved = worker.buffers["u1"]
    assert command(worker, "record.stop") == []
    assert_state(worker, State.idle)
    assert worker.buffers["u1"] is saved
    assert engine.transcribe_calls == 0
    assert events.empty()
    recognize(worker)
    assert engine.started.wait(2)
    assert worker.buffers["u1"] is saved
    gate.set()
    assert events.get(timeout=2)["type"] == "result"
    assert engine.audios == [[0.25, -0.5]]
    assert worker.buffers == {}


def test_cancel_stopped_is_idempotent(factory: Factory) -> None:
    worker, engine, events = factory()
    start(worker)
    assert command(worker, "record.stop") == []
    for _ in range(2):
        assert command(worker, "record.cancel") == [{"type": "cancelled", "utterance_id": "u1"}]
        assert worker.buffers == {}
    assert engine.transcribe_calls == 0
    assert events.empty()


@pytest.mark.parametrize("ttl", [STOPPED_TTL_S_DEFAULT, 2.0])
def test_stopped_expires_on_next_message(
    factory: Factory, caplog: pytest.LogCaptureFixture, ttl: float
) -> None:
    caplog.set_level(logging.INFO)
    now = 10.0
    options = {} if ttl == STOPPED_TTL_S_DEFAULT else {"stopped_ttl_s": ttl}
    worker, _, events = factory(clock=lambda: now, **options)
    start(worker)
    command(worker, "record.stop")
    saved = worker.buffers["u1"]
    now += ttl - 0.5
    assert worker.handle({"type": "ping"}) == [{"type": "pong"}]
    assert worker.buffers["u1"] is saved
    now += 1.0
    assert worker.buffers["u1"] is saved
    assert worker.handle({"type": "ping"}) == [{"type": "pong"}]
    assert worker.buffers == {}
    assert "Истёк срок хранения" in caplog.text
    assert events.empty()
    assert command(worker, "recognize")[0]["code"] == "bad-state"


def test_restart_stopped_replaces_pcm(factory: Factory, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    worker, _, _ = factory()
    start(worker)
    old = worker.buffers["u1"]
    command(worker, "record.stop")
    assert command(worker, "record.start") == []
    assert worker.buffers["u1"] is not old
    assert worker.buffers["u1"] == array("f")
    assert "вытеснен новой записью" in caplog.text
    assert_state(worker, State.recording)


@pytest.mark.parametrize("extra", [0, 100])
@pytest.mark.parametrize("limit_s", [LIMIT_S_DEFAULT, 0.01])
def test_record_limit_stops_and_discards_extra(
    factory: Factory, limit_s: float, extra: int
) -> None:
    options = {} if limit_s == LIMIT_S_DEFAULT else {"limit_s": limit_s}
    worker, engine, events = factory(**options)
    start(worker)
    saved = worker.buffers["u1"]
    count = int(limit_s * SAMPLE_RATE)
    worker.feed_audio("u1", repeat(0.5, count - len(saved) + extra))
    assert worker.buffers["u1"] is saved
    assert len(saved) == count
    assert_state(worker, State.idle)
    event = events.get_nowait()
    assert event == {"type": "record.limit", "utterance_id": "u1"}
    assert decode(encode(event)[4:]) == event
    worker.feed_audio("u1", repeat(1.0, count))
    assert len(saved) == count
    assert saved[-1] == 0.5
    assert events.empty()
    assert engine.transcribe_calls == 0
    recognize(worker)
    assert events.get(timeout=2)["type"] == "result"
    assert worker.buffers == {}


def test_record_limit_requires_utterance_id() -> None:
    with pytest.raises(FrameError) as exc:
        encode({"type": "record.limit"})
    assert exc.value.code == BAD_FIELD


def test_pcm_uses_four_bytes_per_sample(factory: Factory) -> None:
    worker, _, _ = factory()
    command(worker, "record.start")
    saved = worker.buffers["u1"]
    worker.feed_audio("u1", repeat(0.25, SAMPLE_RATE))
    assert worker.buffers["u1"] is saved
    assert isinstance(saved, array)
    assert saved.typecode == "f"
    assert saved.itemsize == 4
    assert len(saved) == SAMPLE_RATE
    assert memoryview(saved).nbytes <= 64 * 1024


def test_real_wav_is_compact_float_array() -> None:
    path = Path(__file__).resolve().parents[2] / "data/test/test-ru-6s.wav"
    samples = WorkerState._read_wav(path)
    assert isinstance(samples, array)
    assert samples.typecode == "f"
    assert len(samples) == 96_000
    assert memoryview(samples).nbytes == 96_000 * 4
    assert sys.getsizeof(samples) < 6 * 64 * 1024
    assert all(-1.0 <= value < 1.0 for value in samples)


def test_long_wav_returns_bad_audio(factory: Factory, tmp_path: Path) -> None:
    path = tmp_path / "long.wav"
    with wave.open(str(path), "wb") as wav:
        wav.setparams((1, 2, SAMPLE_RATE, 0, "NONE", ""))
        wav.writeframes(b"\0\0" * (SAMPLE_RATE + 1))
    worker, engine, events = factory(limit_s=1.0)
    assert worker.handle({"type": "transcribe.file", "path": str(path)}) == []
    assert events.get(timeout=2)["code"] == "bad-audio"
    assert engine.transcribe_calls == 0
    assert worker.buffers == {}


def test_cancel_unknown_returns_bad_state(factory: Factory) -> None:
    worker, _, events = factory()
    assert command(worker, "record.cancel", "unknown")[0]["code"] == "bad-state"
    assert worker.buffers == {}
    assert events.empty()


def test_reusing_cancelled_id_starts_new_lifetime(factory: Factory) -> None:
    worker, _, events = factory()
    start(worker)
    command(worker, "record.cancel")
    start(worker)
    recognize(worker)
    assert events.get(timeout=2)["type"] == "result"
    assert command(worker, "record.cancel")[0]["code"] == "bad-state"


@pytest.mark.parametrize("close", [False, True])
def test_unload_or_close_cleans_stopped(factory: Factory, close: bool) -> None:
    worker, _, _ = factory()
    start(worker)
    command(worker, "record.stop")
    if close:
        worker.close()
    else:
        worker.handle({"type": "model.unload"})
    assert worker.buffers == {}
    assert_state(worker, State.idle)


def test_retention_applies_only_to_stopped_buffers(factory: Factory) -> None:
    gate = Event()
    now = 0.0
    worker, engine, events = factory(FakeEngine(gate=gate), clock=lambda: now)
    start(worker)
    command(worker, "record.stop")
    recognize(worker)
    assert engine.started.wait(2)
    start(worker, "u2")
    command(worker, "record.stop", "u2")
    recognize(worker, "u2")
    start(worker, "u3")
    now += STOPPED_TTL_S_DEFAULT + 1
    worker.handle({"type": "ping"})
    assert set(worker.buffers) == {"u1", "u2", "u3"}
    gate.set()
    assert [events.get(timeout=2)["utterance_id"] for _ in range(2)] == ["u1", "u2"]
    assert set(worker.buffers) == {"u3"}
    assert_state(worker, State.recording)


def test_automatic_stop_also_expires(factory: Factory) -> None:
    now = 0.0
    worker, _, events = factory(limit_s=0.01, stopped_ttl_s=1.0, clock=lambda: now)
    start(worker)
    worker.feed_audio("u1", repeat(0.0, SAMPLE_RATE))
    assert events.get_nowait()["type"] == "record.limit"
    now = 1.0
    worker.handle({"type": "ping"})
    assert worker.buffers == {}


def test_engine_error_preserves_pcm_for_retry(factory: Factory) -> None:
    worker, engine, events = factory(FakeEngine(raises=True))
    start(worker)
    command(worker, "record.stop")
    saved = worker.buffers["u1"]
    recognize(worker)
    assert events.get(timeout=2)["code"] == "engine-failed"
    assert worker.buffers["u1"] is saved
    engine.raises = False
    recognize(worker)
    assert events.get(timeout=2)["type"] == "result"
    assert worker.buffers == {}


def test_unload_forgets_cancelled_ids(factory: Factory) -> None:
    worker, _, _ = factory()
    start(worker)
    command(worker, "record.cancel")
    worker.handle({"type": "model.unload"})
    assert command(worker, "record.cancel")[0]["code"] == "bad-state"


def test_completion_preserves_other_recording(factory: Factory) -> None:
    gate = Event()
    worker, engine, events = factory(FakeEngine(gate=gate))
    start(worker)
    recognize(worker)
    assert engine.started.wait(2)
    start(worker, "u2")
    gate.set()
    assert events.get(timeout=2)["utterance_id"] == "u1"
    assert_state(worker, State.recording)
    assert worker.buffers == {"u2": array("f", [0.25, -0.5])}
    recognize(worker, "u2")
    assert events.get(timeout=2)["utterance_id"] == "u2"
    assert worker.buffers == {}


def test_one_pending_job_and_second_is_busy(factory: Factory) -> None:
    gate = Event()
    worker, engine, events = factory(FakeEngine(gate=gate))
    start(worker)
    recognize(worker)
    assert engine.started.wait(2)
    start(worker, "u2")
    recognize(worker, "u2")
    start(worker, "u3")
    assert command(worker, "recognize", "u3")[0]["code"] == "busy"
    assert engine.transcribe_calls == 1
    command(worker, "record.cancel", "u3")
    gate.set()
    assert [events.get(timeout=2)["utterance_id"] for _ in range(2)] == ["u1", "u2"]
    assert engine.transcribe_calls == 2
    assert worker.buffers == {}


@pytest.mark.parametrize("respect_cancel", [True, False])
def test_cancel_processing_suppresses_late_result(factory: Factory, respect_cancel: bool) -> None:
    gate = Event()
    with ThreadPoolExecutor(max_workers=1) as executor:
        worker, engine, events = factory(
            FakeEngine(gate=gate, respect_cancel=respect_cancel), executor=executor
        )
        try:
            start(worker)
            recognize(worker)
            assert engine.started.wait(2)
            assert command(worker, "record.cancel") == [{"type": "cancelled", "utterance_id": "u1"}]
            assert engine.tokens[0].cancelled
            assert worker.buffers == {}
        finally:
            gate.set()
        executor.submit(lambda: None).result(timeout=2)
        assert_state(worker, State.idle)
        assert worker.buffers == {}
        assert events.empty()
        assert command(worker, "record.cancel") == [{"type": "cancelled", "utterance_id": "u1"}]
        worker.close()


def test_cancel_pending_does_not_reach_engine(factory: Factory) -> None:
    gate = Event()
    worker, engine, events = factory(FakeEngine(gate=gate))
    start(worker)
    recognize(worker)
    assert engine.started.wait(2)
    start(worker, "u2")
    recognize(worker, "u2")
    assert command(worker, "record.cancel", "u2")[0]["type"] == "cancelled"
    assert command(worker, "record.cancel", "u2")[0]["type"] == "cancelled"
    assert "u2" not in worker.buffers
    gate.set()
    assert events.get(timeout=2)["utterance_id"] == "u1"
    assert engine.transcribe_calls == 1
    assert worker.buffers == {}


def test_cancel_recording_is_idempotent(factory: Factory) -> None:
    worker, _, events = factory()
    start(worker)
    for _ in range(3):
        assert command(worker, "record.cancel") == [{"type": "cancelled", "utterance_id": "u1"}]
        assert worker.buffers == {}
        assert_state(worker, State.idle)
    assert events.empty()


def test_engine_cancelled_result_cleans_pcm(factory: Factory) -> None:
    worker, _, events = factory(FakeEngine(cancelled_result=True))
    start(worker)
    recognize(worker)
    assert events.get(timeout=2) == {"type": "cancelled", "utterance_id": "u1"}
    assert worker.buffers == {}
    assert_state(worker, State.idle)
    assert command(worker, "record.cancel")[0]["type"] == "cancelled"


def test_unload_cleans_recording(factory: Factory) -> None:
    worker, engine, _ = factory()
    start(worker)
    assert worker.handle({"type": "model.unload"}) == []
    assert engine.unload_calls == 1
    assert_state(worker, State.idle)
    assert worker.buffers == {}
    assert command(worker, "recognize")[0]["code"] == "no-model"


def test_unload_processing_suppresses_result(factory: Factory) -> None:
    gate = Event()
    with ThreadPoolExecutor(max_workers=1) as executor:
        worker, engine, events = factory(
            FakeEngine(gate=gate, respect_cancel=False), executor=executor
        )
        try:
            start(worker)
            recognize(worker)
            assert engine.started.wait(2)
            start(worker, "u2")
            recognize(worker, "u2")
            worker.handle({"type": "model.unload"})
            assert_state(worker, State.idle)
            assert worker.buffers == {}
            assert worker.handle(load_message())[0]["code"] == "busy"
            assert worker.handle({"type": "ping"}) == [{"type": "pong"}]
        finally:
            gate.set()
        executor.submit(lambda: None).result(timeout=2)
        assert engine.unload_calls == 1
        assert engine.transcribe_calls == 1
        assert events.empty()
        assert worker.handle(load_message())[0]["type"] == "model.loaded"
        worker.close()


def test_no_model_and_bad_states(factory: Factory) -> None:
    worker, _, _ = factory(loaded=False)
    assert command(worker, "recognize")[0]["code"] == "no-model"
    assert worker.handle({"type": "transcribe.file", "path": "/none"})[0]["code"] == "no-model"
    start(worker)
    assert command(worker, "record.start")[0]["code"] == "bad-state"
    assert command(worker, "record.start", "u2")[0]["code"] == "bad-state"
    assert command(worker, "record.stop", "u2")[0]["code"] == "bad-state"


def test_engine_exception_returns_idle(factory: Factory, caplog: pytest.LogCaptureFixture) -> None:
    worker, _, events = factory(FakeEngine(raises=True, text="секрет исключения"))
    start(worker)
    recognize(worker)
    msg = events.get(timeout=2)
    assert msg["code"] == "engine-failed"
    assert_state(worker, State.idle)
    assert worker.buffers == {"u1": array("f", [0.25, -0.5])}
    assert "секрет исключения" not in caplog.text
    assert "секрет исключения" not in str(msg)


@pytest.mark.parametrize("changes", [{"id": "other"}, {"revision": "r2"}, {"threads": 4}])
def test_model_identity_requires_restart(factory: Factory, changes: Message) -> None:
    worker, engine, _ = factory()
    assert worker.handle(load_message(**changes))[0]["code"] == "restart-required"
    worker.handle({"type": "model.unload"})
    assert worker.handle(load_message(**changes))[0]["code"] == "restart-required"
    assert engine.load_calls == 1


def test_model_loaded_and_repeat(factory: Factory) -> None:
    worker, engine, _ = factory(loaded=False)
    expected = {
        "type": "model.loaded",
        "id": "gigaam",
        "revision": "r1",
        "variant": "int8",
        "load_ms": 1.5,
        "engine_version": "fake-1",
    }
    assert worker.handle(load_message()) == [expected]
    assert worker.min_ram_mb == 512
    assert engine.load_args == (Path("/model"), "fake", "int8", 2)
    assert worker.handle(load_message()) == [expected]
    assert engine.load_calls == 1
    worker.handle({"type": "model.unload"})
    assert worker.handle(load_message()) == [expected]
    assert engine.load_calls == 2


def test_load_failure_is_sanitized(factory: Factory, caplog: pytest.LogCaptureFixture) -> None:
    worker, engine, _ = factory(FakeEngine(load_raises=True, text="секрет загрузки"), loaded=False)
    msg = worker.handle(load_message())[0]
    assert msg["code"] == "engine-failed"
    assert engine.unload_calls == 1
    assert "секрет загрузки" not in caplog.text + str(msg)


def test_ping_during_slow_recognition(factory: Factory) -> None:
    worker, engine, events = factory(FakeEngine(delay=0.3))
    start(worker)
    recognize(worker)
    assert engine.started.wait(2)
    assert worker.handle({"type": "ping"}) == [{"type": "pong"}]
    assert not engine.finished.is_set()
    assert events.get(timeout=2)["t_ms"] >= 300


def test_audio_close_ready(factory: Factory) -> None:
    source = FakeAudioSource()
    worker, _, _ = factory(audio_source=source)
    assert worker.handle({"type": "audio.close"}) == [{"type": "audio.closed"}]
    assert source.close_calls == 1
    assert worker.audio_ready() == {"type": "audio.ready"}


@pytest.mark.parametrize("device", [None, "Микрофон"])
@pytest.mark.parametrize("changed", [None, "Источник звука изменился: Микрофон"])
def test_audio_ready_optional_fields(device: str | None, changed: str | None) -> None:
    """Единый конструктор опускает отсутствующие поля и сохраняет готовый текст пилюли."""
    expected: Message = {"type": "audio.ready"}
    if device is not None:
        expected["device"] = device
    if changed is not None:
        expected["changed"] = changed
    event = WorkerState.audio_ready(device=device, changed=changed)
    assert event == expected
    assert decode(encode(event)[4:]) == expected


@pytest.mark.parametrize("missing_smaps", [False, True])
def test_measure_proc_fixtures(factory: Factory, tmp_path: Path, missing_smaps: bool) -> None:
    status, smaps = tmp_path / "status", tmp_path / "smaps_rollup"
    status.write_text("Name: worker\nVmHWM:  123456 kB\n", encoding="utf-8")
    if not missing_smaps:
        smaps.write_text("Rss: 1 kB\nPss:   65432 kB\n", encoding="utf-8")
    worker, _, _ = factory(status_path=status, smaps_path=smaps, runtime="fake-ort")
    path = tmp_path / "measurements.json"
    path.write_text('{"previous": {"vm_hwm_kb": 1}}', encoding="utf-8")
    msg = worker.handle({"type": "measure"})[0]
    pss = None if missing_smaps else 65432
    assert msg == {"type": "measured", "vm_hwm_kb": 123456, "pss_kb": pss, "sessions": 3}
    encode(msg)
    entries = json.loads(path.read_text(encoding="utf-8"))
    assert entries.pop("previous") == {"vm_hwm_kb": 1}
    key, entry = next(iter(entries.items()))
    assert json.loads(key) == ["gigaam", "r1", 2, "fake-ort", __version__]
    assert entry == {
        "id": "gigaam",
        "revision": "r1",
        "threads": 2,
        "runtime": "fake-ort",
        "build": __version__,
        "vm_hwm_kb": 123456,
        "pss_kb": pss,
    }


def test_measure_explicit_destination(factory: Factory, tmp_path: Path) -> None:
    status = tmp_path / "status"
    status.write_text("VmHWM:  123456 kB\n", encoding="utf-8")
    target = tmp_path / "custom" / "memory.json"
    worker, _, _ = factory(
        status_path=status, smaps_path=tmp_path / "missing", measurements_path=target
    )
    assert worker.handle({"type": "measure"})[0]["type"] == "measured"
    assert target.is_file()
    assert not (tmp_path / "measurements.json").exists()


@pytest.mark.parametrize("sessions", [0, 3, 10])
def test_measured_sessions_follow_last_load(
    factory: Factory, tmp_path: Path, sessions: int
) -> None:
    """IPC сохраняет фактическое число сессий только пока модель загружена."""

    class CountingEngine(FakeEngine):
        """Возвращает разные числа при последовательных загрузках."""

        def load(self, model_dir: Path, layout: str, variant: str, threads: int) -> FakeLoadResult:
            result = super().load(model_dir, layout, variant, threads)
            return FakeLoadResult(
                result.load_ms, result.engine_version, sessions + self.load_calls - 1
            )

    status = tmp_path / "status"
    status.write_text("VmHWM: 123 kB\n", encoding="utf-8")
    worker, engine, _ = factory(
        CountingEngine(), loaded=False, status_path=status, smaps_path=tmp_path / "missing"
    )

    def measure() -> Message:
        """Проверяет поле после настоящего кодирования и декодирования IPC."""
        return decode(encode(worker.handle({"type": "measure"})[0])[4:])

    assert "sessions" not in measure()
    for index in (1, 2):
        assert worker.handle(load_message())[0]["type"] == "model.loaded"
        assert measure()["sessions"] == sessions + index - 1
        assert worker.handle(load_message())[0]["type"] == "model.loaded"
        assert engine.load_calls == index
        assert measure()["sessions"] == sessions + index - 1
        worker.handle({"type": "model.unload"})
        assert "sessions" not in measure()
    engine.load_raises = True
    assert worker.handle(load_message())[0]["type"] == "error"
    assert "sessions" not in measure()


@pytest.mark.parametrize("from_file", [False, True])
def test_soak_releases_pcm(factory: Factory, tmp_path: Path, from_file: bool) -> None:
    """Двадцать распознаваний не оставляют PCM в автомате одного воркера."""
    path = tmp_path / "soak.wav"
    with wave.open(str(path), "wb") as wav:
        wav.setparams((1, 2, SAMPLE_RATE, 0, "NONE", "not compressed"))
        wav.writeframes(struct.pack("<hh", 8192, -16384))
    worker, engine, events = factory()
    for index in range(20):
        if from_file:
            assert worker.handle({"type": "transcribe.file", "path": str(path)}) == []
        else:
            uid = f"soak-{index}"
            start(worker, uid)
            command(worker, "record.stop", uid)
            recognize(worker, uid)
        assert events.get(timeout=2)["type"] == "result"
        assert worker.buffers == {}
        assert_state(worker, State.idle)
    assert engine.load_calls == 1
    assert engine.transcribe_calls == 20


@pytest.mark.parametrize("huge", [False, True])
def test_text_never_logged(factory: Factory, caplog: pytest.LogCaptureFixture, huge: bool) -> None:
    caplog.set_level(logging.DEBUG)
    worker, engine, events = factory(FakeEngine(unsafe_text=True, huge_text=huge))
    start(worker)
    recognize(worker)
    msg = events.get(timeout=2)
    if huge:
        assert msg["code"] == BAD_FIELD
    else:
        assert msg["text"] == "ls\n rm -rf ~"
    assert engine.text not in caplog.text
    assert all("text" not in record.__dict__ for record in caplog.records)
    if huge:
        assert worker.buffers == {"u1": array("f", [0.25, -0.5])}
    else:
        assert worker.buffers == {}


@pytest.mark.parametrize(
    "rate,width,channels", [(16000, 2, 1), (8000, 2, 1), (16000, 1, 1), (16000, 2, 2)]
)
def test_transcribe_file(
    factory: Factory, tmp_path: Path, rate: int, width: int, channels: int
) -> None:
    path = tmp_path / "audio.wav"
    with wave.open(str(path), "wb") as wav:
        wav.setframerate(rate)
        wav.setsampwidth(width)
        wav.setnchannels(channels)
        wav.writeframes(struct.pack("<hh", 8192, -16384))
    worker, engine, events = factory()
    assert worker.handle({"type": "transcribe.file", "path": str(path)}) == []
    msg = events.get(timeout=2)
    if (rate, width, channels) == (16000, 2, 1):
        assert msg["type"] == "result"
        assert msg["utterance_id"] == "file"
        assert engine.audios == [[0.25, -0.5]]
    else:
        assert msg["code"] == "bad-audio"
        assert engine.transcribe_calls == 0
    assert_state(worker, State.idle)
    assert worker.buffers == {}


def test_invalid_wav(factory: Factory, tmp_path: Path) -> None:
    path = tmp_path / "bad.wav"
    path.write_bytes(b"not a wav")
    worker, _, events = factory()
    worker.handle({"type": "transcribe.file", "path": str(path)})
    assert events.get(timeout=2)["code"] == "bad-audio"
    assert worker.buffers == {}


def test_default_audio_factory_is_lazy_numpy(factory: Factory) -> None:
    np = pytest.importorskip("numpy")
    worker, engine, events = factory(numpy_audio=True)
    start(worker)
    samples = worker.buffers["u1"]
    recognize(worker)
    assert events.get(timeout=2)["type"] == "result"
    assert isinstance(engine.audios[0], np.ndarray)
    assert engine.audios[0].dtype == np.float32
    assert engine.audios[0].ndim == 1
    assert np.shares_memory(engine.audios[0], np.frombuffer(samples, dtype=np.float32))
    assert engine.audios[0].tolist() == [0.25, -0.5]


def test_import_without_engine_numpy_ort() -> None:
    script = """
import sys
sys.path.insert(0, sys.argv[1])
sys.modules['numpy'] = None
sys.modules['onnxruntime'] = None
sys.modules['astra_voice.worker.engine'] = None
sys.modules['astra_voice.worker.vad'] = None
from astra_voice.worker.state import WorkerState
worker = WorkerState(runtime='none')
assert worker.handle({'type': 'ping'}) == [{'type': 'pong'}]
worker.close()
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(Path(__file__).resolve().parents[2] / "src")],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr


def test_close_is_idempotent(factory: Factory) -> None:
    worker, engine, events = factory()
    worker.close()
    worker.close()
    assert engine.unload_calls == 1
    assert worker.handle({"type": "ping"})[0]["code"] == "bad-state"
    with pytest.raises(Empty):
        events.get_nowait()
