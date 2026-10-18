"""ИБ-30: ошибки задания через настоящий супервизор, runtime и оркестратор."""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path
from queue import Queue
from threading import Event
from typing import Any

import pytest
from test_runtime import Rig

from astra_voice.core.dictation import DictationPhase
from astra_voice.core.settings import from_dict
from astra_voice.platform.hotkey import HotkeyState
from astra_voice.worker import ipc
from astra_voice.worker.state import Message, WorkerState
from astra_voice.worker.supervisor import WorkerSupervisor

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fakes import FakeCancelToken, FakeEngine  # noqa: E402

pytestmark = pytest.mark.unit


@pytest.fixture
def supervisor_rig(monkeypatch: pytest.MonkeyPatch) -> Iterator[Rig]:
    # Транспорт не запускает процесс; корреляция, дедлайны и декодер настоящие.
    monkeypatch.setattr(WorkerSupervisor, "_launch", lambda s: setattr(s, "state", "running"))
    holder: dict[str, Rig] = {}

    def factory(**kwargs: Any) -> WorkerSupervisor:
        return WorkerSupervisor(
            on_event=kwargs["on_event"], use_qt=False, clock=lambda: holder["rig"].now
        )

    rig = Rig(monkeypatch, from_dict({"model_dir": "/tmp/model"}), supervisor_factory=factory)
    holder["rig"] = rig
    rig.hotkey.has_pending_press = False
    rig.runtime.start()
    receive(rig, ipc.make_hello())
    receive(
        rig,
        {
            "type": "model.loaded",
            "id": "x",
            "revision": "r",
            "variant": "v",
            "load_ms": 1,
            "engine_version": "1",
        },
    )
    receive(
        rig,
        {
            "type": "result",
            "utterance_id": "file",
            "text": "проверка",
            "t_ms": 1,
            "infer_ms": 1.0,
            "audio_ms": 1.0,
        },
    )
    assert rig.runtime._selfcheck == "ok"
    try:
        yield rig
    finally:
        rig.runtime.shutdown()


def receive(rig: Rig, message: Message) -> None:
    supervisor = rig.runtime.supervisor
    supervisor._receive(ipc.encode(message), supervisor.generation)


@pytest.mark.parametrize("with_measure", [False, True])
@pytest.mark.parametrize("after_timeout", [False, True])
def test_job_error_preserves_recording_through_supervisor(
    supervisor_rig: Rig,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    with_measure: bool,
    after_timeout: bool,
) -> None:
    """Ошибка A не выключает B и не забирает ожидание замера (MN-3)."""
    rig = supervisor_rig
    supervisor = rig.runtime.supervisor
    sent: list[Message] = []
    original_send = supervisor.send

    def send(message: Message, **kwargs: Any) -> Message:
        sent.append(dict(message))
        return original_send(message, **kwargs)

    monkeypatch.setattr(supervisor, "send", send)
    gate = Event()
    engine = FakeEngine(raises=True, gate=gate, respect_cancel=False)
    events: Queue[Message] = Queue()
    worker = WorkerState(
        engine_factory=lambda layout: engine,
        cancel_factory=FakeCancelToken,
        data_dir_factory=lambda: tmp_path,
        on_event=events.put,
        audio_factory=list,
        clock=lambda: rig.now,
    )
    worker.handle(
        {
            "type": "model.load",
            "id": "x",
            "revision": "r",
            "dir": "/model",
            "layout": "fake",
            "variant": "v",
            "threads": 1,
            "min_ram_mb": 1,
        }
    )
    try:
        rig.hotkey.fsm.press(rig.now)
        a = str(sent[-1]["utterance_id"])
        assert worker.handle(sent[-1]) == []
        worker.feed_audio(a, [0.1, 0.2])
        rig.now += 2
        rig.release(rig.now)
        for message in sent[-2:]:
            assert message["type"] in {"record.stop", "recognize"}
            for reply in worker.handle(message):
                receive(rig, reply)
            if message["type"] == "record.stop":
                # Без AudioCapture нет callback-а закрытия устройства.
                receive(rig, {"type": "record.stopped", "utterance_id": a})
        assert engine.started.wait(2)
        rig.now += 1
        rig.hotkey.fsm.press(rig.now)
        b = str(sent[-1]["utterance_id"])
        assert a != b and sent[-1]["type"] == "record.start"
        assert worker.handle(sent[-1]) == []
        if after_timeout:
            for _ in range(3):
                rig.now += 10
                receive(
                    rig, {"type": "level", "utterance_id": b, "peak_dbfs": -20.0, "rms_dbfs": -30.0}
                )
            supervisor._expire()
            assert any(m["type"] == "record.cancel" and m.get("utterance_id") == a for m in sent)
            # Отмена ещё в пути: ошибка уже выполняющегося задания пересекает её.
        if with_measure:
            supervisor.send({"type": "measure"}, timeout=30)
        rig.guard.set_recording.reset_mock()
        gate.set()
        failure = events.get(timeout=2)
        assert failure["type"] == "error" and failure["code"] == "engine-failed"
        assert failure.get("utterance_id") == a
        receive(rig, failure)
        assert rig.runtime.orchestrator.phase is DictationPhase.RECORDING
        assert rig.hotkey.fsm.state is HotkeyState.RECORDING
        assert not any(call.args == (False,) for call in rig.guard.set_recording.call_args_list)
        assert worker._recording == b
        if after_timeout:
            for reply in worker.handle({"type": "record.cancel", "utterance_id": a}):
                receive(rig, reply)
            dropped = supervisor.dropped_late
            receive(rig, failure)
            assert supervisor.dropped_late == dropped + 1
        else:
            assert ("utterance", a) not in supervisor._pending
            assert all(item._utterance_id != a for item in rig.runtime.orchestrator._items)
        if with_measure:
            assert ("reply", "measured") in supervisor._pending
        # Даже некоррелированная ошибка старого воркера не адресуется B.
        receive(rig, ipc.error("engine-failed", "Ошибка распознавания движка."))
        assert rig.runtime.orchestrator.phase is DictationPhase.RECORDING
        assert rig.hotkey.fsm.state is HotkeyState.RECORDING
        assert not any(call.args == (False,) for call in rig.guard.set_recording.call_args_list)
    finally:
        gate.set()
        worker.close()
