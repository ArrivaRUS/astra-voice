"""US-2.7 на стороне воркера: запись B во время распознавания A (T-274, T-275).

Воркер 0.1.6 уже умеет состояние recording+processing и очередь «одно задание
выполняется + одно ждёт» с FIFO-выдачей результатов. T-274 — guard этого контракта,
на нём держится порядок вставок US-2.7 (зелёный сейчас, должен остаться зелёным).
T-275 (вопрос Q5 отчёта tester) — красный: при ``engine-failed`` фразы A воркер
молча останавливает идущую запись B (``WorkerState._finish``: ``_stop_recording()``
без события ``record.limit``), и остаток речи B теряется, хотя пользователь ещё
держит клавишу. Предлагаемое изменение: при ``engine-failed`` не трогать текущую
запись (ожидающее задание — по решению Q5).
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path
from queue import Queue
from threading import Event
from typing import Any

import pytest

from astra_voice.worker.ipc import decode, encode
from astra_voice.worker.state import Message, State, WorkerState

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fakes import FakeCancelToken, FakeEngine  # noqa: E402

pytestmark = pytest.mark.unit


class FailFirstEngine(FakeEngine):
    """Первое распознавание (фраза A) падает, следующие проходят."""

    def transcribe(self, audio: Any, cancel: Any) -> Any:
        self.raises = self.transcribe_calls == 0
        return super().transcribe(audio, cancel)


@pytest.fixture(autouse=True)
def no_vad(monkeypatch: pytest.MonkeyPatch) -> None:
    """Без настоящего VAD: распознавание всей записи одним куском."""
    import types

    module = types.ModuleType("astra_voice.worker.vad")
    module.vad_available = lambda: False  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, module.__name__, module)


@pytest.fixture
def make_worker(tmp_path: Path) -> Iterator[Any]:
    created: list[tuple[WorkerState, FakeEngine]] = []

    def create(engine: FakeEngine) -> tuple[WorkerState, Queue[Message]]:
        events: Queue[Message] = Queue()

        def on_event(event: Message) -> None:
            assert decode(encode(event)[4:]) == event
            events.put(event)

        worker = WorkerState(
            engine_factory=lambda layout: engine,
            cancel_factory=FakeCancelToken,
            data_dir_factory=lambda: tmp_path,
            on_event=on_event,
            audio_factory=list,
        )
        created.append((worker, engine))
        loaded = worker.handle(
            {
                "type": "model.load",
                "id": "gigaam",
                "revision": "r1",
                "dir": "/model",
                "layout": "fake",
                "variant": "int8",
                "threads": 2,
                "min_ram_mb": 512,
            }
        )
        assert loaded[0]["type"] == "model.loaded"
        return worker, events

    yield create
    for worker, engine in created:
        if engine.gate is not None:
            engine.gate.set()
        worker.close()


def cmd(worker: WorkerState, kind: str, uid: str) -> list[Message]:
    return worker.handle({"type": kind, "utterance_id": uid})


def test_second_recording_during_recognition_is_fifo(make_worker: Any) -> None:
    """T-274 guard: B пишется целиком, пока A распознаётся; результаты строго A, затем B."""
    gate = Event()
    engine = FakeEngine(gate=gate)
    worker, events = make_worker(engine)
    assert cmd(worker, "record.start", "a") == []
    worker.feed_audio("a", [0.1, 0.2])
    assert cmd(worker, "recognize", "a") == []
    assert engine.started.wait(2)
    assert cmd(worker, "record.start", "b") == []
    assert worker.state is State.recording_processing
    worker.feed_audio("b", [0.3, 0.4, 0.5])
    assert cmd(worker, "record.stop", "b") == []
    assert cmd(worker, "recognize", "b") == []
    gate.set()
    first, second = events.get(timeout=2), events.get(timeout=2)
    assert [first["utterance_id"], second["utterance_id"]] == ["a", "b"]
    assert [first["type"], second["type"]] == ["result", "result"]
    assert list(engine.audios[1]) == pytest.approx([0.3, 0.4, 0.5])


def test_engine_failure_of_first_keeps_second_recording(make_worker: Any) -> None:
    """T-275 (Q5): engine-failed фразы A не обрывает идущую запись B."""
    gate = Event()
    engine = FailFirstEngine(gate=gate)
    worker, events = make_worker(engine)
    assert cmd(worker, "record.start", "a") == []
    worker.feed_audio("a", [0.1])
    assert cmd(worker, "recognize", "a") == []
    assert engine.started.wait(2)
    assert cmd(worker, "record.start", "b") == []
    worker.feed_audio("b", [0.2])
    gate.set()
    failure = events.get(timeout=2)
    # Ошибка без id: корреляцию с A по ожиданию recognize добавляет супервизор.
    assert (failure["type"], failure.get("code")) == ("error", "engine-failed")
    assert worker.state is State.recording, "запись B остановлена молча после ошибки A"
    worker.feed_audio("b", [0.3])
    assert list(worker.buffers["b"]) == pytest.approx([0.2, 0.3])
    assert cmd(worker, "record.stop", "b") == []
    assert cmd(worker, "recognize", "b") == []
    result = events.get(timeout=2)
    assert (result["type"], result["utterance_id"]) == ("result", "b")


def test_engine_failure_reports_queued_phrase_as_error(make_worker: Any) -> None:
    """Решение 5: ожидавшая очереди B при engine-failed A — ошибка с её id, не «cancelled».

    PCM B сохраняется до срока хранения остановленных буферов, как у упавшей A.
    """
    gate = Event()
    engine = FailFirstEngine(gate=gate)
    worker, events = make_worker(engine)
    assert cmd(worker, "record.start", "a") == []
    worker.feed_audio("a", [0.1])
    assert cmd(worker, "recognize", "a") == []
    assert engine.started.wait(2)
    assert cmd(worker, "record.start", "b") == []
    worker.feed_audio("b", [0.2])
    assert cmd(worker, "record.stop", "b") == []
    assert cmd(worker, "recognize", "b") == []
    gate.set()
    first, second = events.get(timeout=2), events.get(timeout=2)
    assert (first["type"], first.get("code"), "utterance_id" in first) == (
        "error",
        "engine-failed",
        False,
    )
    assert (second["type"], second.get("code"), second.get("utterance_id")) == (
        "error",
        "engine-failed",
        "b",
    )
    assert worker.state is State.idle
    assert list(worker.buffers["b"]) == pytest.approx([0.2])
    # Повтор распознавания B возможен: буфер цел.
    assert cmd(worker, "recognize", "b") == []
    result = events.get(timeout=2)
    assert (result["type"], result["utterance_id"]) == ("result", "b")
