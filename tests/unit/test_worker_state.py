"""Автомат, очередь, отмена, PCM и замеры на фейках без тяжёлого рантайма."""

from __future__ import annotations

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
from typing import Any

import pytest

from astra_voice.core.version import __version__
from astra_voice.worker.ipc import BAD_FIELD, FrameError, decode, encode
from astra_voice.worker.state import (
    LIMIT_S_DEFAULT,
    SAMPLE_RATE,
    STOPPED_TTL_S_DEFAULT,
    Message,
    State,
    WorkerState,
)

# tests не пакет; общий каталог фейков добавляется без нового conftest.py.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fakes import FakeAudioSource, FakeCancelToken, FakeEngine, FakeLoadResult  # noqa: E402

pytestmark = pytest.mark.unit
Factory = Callable[..., tuple[WorkerState, FakeEngine, Queue[Message]]]


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
            cancel_factory=FakeCancelToken,
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
