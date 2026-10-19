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
from threading import Event, Thread
from typing import Any, cast

import pytest

from astra_voice.worker.audio import ERROR_FAILED, AudioCapture, AudioError
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


@pytest.mark.parametrize(
    ("finish", "kind"),
    [("capture-error", kind) for kind in ("record.cancel", "record.stop", "recognize")]
    + [("limit", "record.cancel")],
)
def test_cancel_waits_for_physical_close(make_worker: Any, finish: str, kind: str) -> None:
    """bad-state/cancelled выдаётся лишь после закрытия, вне lock автомата."""
    worker, events = make_worker(FakeEngine())
    closing, release, waiting, replied = Event(), Event(), Event(), Event()

    class FaultSource:
        is_open = False
        live = False

        def open(self, device: str | None) -> None:
            self.is_open = True

        def read_chunk(self) -> bytes | None:
            if finish == "capture-error":
                raise AudioError(ERROR_FAILED, "Ошибка чтения.")
            return b"\x00\x20"

        def close(self) -> None:
            closing.set()
            assert release.wait(2)
            self.is_open = False

    source = FaultSource()
    capture = AudioCapture(
        source=cast(Any, source),
        on_samples=worker.on_samples,
        on_event=events.put,
        on_error=worker.on_error,
    )
    worker.set_capture(capture)
    original_wait = capture.wait_stopped

    def wait_stopped(uid: str) -> None:
        waiting.set()
        original_wait(uid)

    capture.wait_stopped = wait_stopped  # type: ignore[method-assign,assignment]
    replies: list[Message] = []

    def cancel() -> None:
        replies.extend(cmd(worker, kind, "a"))
        replied.set()

    cancelling = Thread(target=cancel)
    try:
        assert (
            worker.handle({"type": "record.start", "utterance_id": "a", "limit_s": 1 / 16000}) == []
        )
        assert closing.wait(2)
        assert [events.get(timeout=2)["type"] for _ in range(3)] == [
            "audio.ready",
            "record.started",
            "error" if finish == "capture-error" else "record.limit",
        ]
        assert ("a" in worker.buffers) is (finish == "limit")
        cancelling.start()
        assert waiting.wait(2)
        assert not replied.is_set() and source.is_open
        # Отменяющий поток не держит lock: цикл IPC/фоновые callback не заблокированы.
        assert worker.handle({"type": "ping"}) == [{"type": "pong", "request_type": "ping"}]
        release.set()
        cancelling.join(2)
        assert not cancelling.is_alive() and replied.is_set()
        assert replies[0]["request_type"] == kind
        assert replies[0]["utterance_id"] == "a"
        if finish == "capture-error":
            assert replies[0]["code"] == "bad-state"
        else:
            assert replies == [
                {"type": "cancelled", "utterance_id": "a", "request_type": "record.cancel"}
            ]
        assert worker.buffers == {}
        assert not source.is_open
        assert events.get(timeout=2) == {"type": "record.stopped", "utterance_id": "a"}
    finally:
        release.set()
        if cancelling.ident is not None:
            cancelling.join(2)
        capture.stop()


@pytest.mark.parametrize("kind", ["record.cancel", "record.stop", "recognize"])
def test_unknown_cancel_does_not_stop_another_capture(make_worker: Any, kind: str) -> None:
    """Адресное ожидание неизвестной A сохраняет работающий захват B."""
    worker, events = make_worker(FakeEngine())
    reading, release = Event(), Event()

    class Source:
        is_open = False
        live = False

        def open(self, device: str | None) -> None:
            self.is_open = True

        def read_chunk(self) -> bytes | None:
            reading.set()
            assert release.wait(2)
            return None

        def close(self) -> None:
            self.is_open = False

    source = Source()
    capture = AudioCapture(
        source=cast(Any, source),
        on_samples=worker.on_samples,
        on_event=events.put,
        on_error=worker.on_error,
    )
    worker.set_capture(capture)
    try:
        assert cmd(worker, "record.start", "b") == []
        assert reading.wait(2)
        assert cmd(worker, kind, "unknown-a")[0]["code"] == "bad-state"
        assert source.is_open and capture.active
        worker.feed_audio("b", [0.2, 0.3])
        assert list(worker.buffers["b"]) == pytest.approx([0.2, 0.3])
        assert worker.state is State.recording
    finally:
        capture.request_stop()
        release.set()
        capture.stop()


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
    # Фоновая ошибка несёт id задания: супервизор не угадывает его по ожиданиям.
    assert (failure["type"], failure.get("code"), failure.get("utterance_id")) == (
        "error",
        "engine-failed",
        "a",
    )
    assert worker.state is State.recording, "запись B остановлена молча после ошибки A"
    worker.feed_audio("b", [0.3])
    assert list(worker.buffers["b"]) == pytest.approx([0.2, 0.3])
    assert cmd(worker, "record.stop", "b") == []
    assert cmd(worker, "recognize", "b") == []
    result = events.get(timeout=2)
    assert (result["type"], result["utterance_id"]) == ("result", "b")


def test_engine_failure_reports_queued_phrase_as_error(make_worker: Any) -> None:
    """Решение 5: ожидавшая очереди B при engine-failed A — ошибка с её id, не «cancelled».

    ИБ-34: PCM ожидавшей B сразу освобождается; A остаётся для retry до TTL.
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
    assert (first["type"], first.get("code"), first.get("utterance_id")) == (
        "error",
        "engine-failed",
        "a",
    )
    assert (second["type"], second.get("code"), second.get("utterance_id")) == (
        "error",
        "engine-failed",
        "b",
    )
    assert worker.state is State.idle
    assert "b" not in worker.buffers
    assert "b" not in worker._stopped
    assert cmd(worker, "recognize", "b")[0]["code"] == "bad-state"
    assert engine.transcribe_calls == 1
