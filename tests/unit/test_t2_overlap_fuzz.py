"""ИБ-46/51: настоящий IPC, WorkerState, AudioCapture и оркестратор.

ОС заменена каналом в памяти и управляемым источником PCM; UI и часы — Rig.
Предыдущие model/replay проверки без ослабления сохранены в test_t2_overlap_replay.
"""

from __future__ import annotations

import logging
import random
import sys
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from queue import Empty, Queue
from threading import Event
from typing import Any, cast
from unittest.mock import Mock

import pytest
from test_dictation import MARKER, Rig
from test_indicators import FakePill, FakeTimer, FakeTray

from astra_voice.core.dictation import CANCEL_RESTART_MS, CANCEL_TIMEOUT_MS, DictationPhase
from astra_voice.platform.hotkey import HotkeyMode, HotkeyState
from astra_voice.ui import indicators
from astra_voice.ui.indicators import IndicatorGuard
from astra_voice.ui.pill import PillState
from astra_voice.ui.tray_icons import TrayState
from astra_voice.worker import ipc
from astra_voice.worker import supervisor as supervisor_module
from astra_voice.worker.audio import ERROR_FAILED, AudioCapture, AudioError
from astra_voice.worker.main import WorkerLoop
from astra_voice.worker.state import WorkerState
from astra_voice.worker.supervisor import WorkerSupervisor

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fakes import FakeCancelToken, FakeEngine  # noqa: E402

pytestmark = pytest.mark.unit


class Source:
    """Источник ОС: отдельно управляет чтением и физическим закрытием."""

    live = False
    ended = False

    def __init__(self) -> None:
        self.is_open = False
        self.opened = Event()
        self.closing = Event()
        self.release_close = Event()
        self.release_close.set()
        self.fault = Event()
        self.read_tick = Event()

    def open(self, device: str | None) -> None:
        assert not self.is_open, "два физических захвата"
        self.fault.clear()
        self.closing.clear()
        self.is_open = True
        self.opened.set()

    def read_chunk(self) -> bytes:
        self.read_tick.wait(0.001)
        if self.fault.is_set():
            raise AudioError(ERROR_FAILED, "Ошибка источника.")
        return b"\x00\x20" * 160

    def close(self) -> None:
        self.closing.set()
        assert self.release_close.wait(3), "тест не освободил close"
        self.is_open = False


class Channel:
    """Только транспорт вместо socket.send; фрейминг остаётся настоящим."""

    def __init__(self) -> None:
        self.reader = ipc.FrameReader()
        self.commands: deque[dict[str, Any]] = deque()

    def send(self, data: bytes) -> int:
        self.commands.extend(self.reader.feed(bytes(data)))
        return len(data)

    def close(self) -> None:
        pass


class Chain:
    def __init__(self, monkeypatch: pytest.MonkeyPatch, mode: HotkeyMode) -> None:
        self.rig = Rig(mode)
        self.rig.ack_stop = False
        self.pill, self.tray = FakePill(), FakeTray()
        monkeypatch.setattr(indicators, "monotonic", lambda: self.rig.now)
        monkeypatch.setattr(
            indicators, "QTimer", Mock(side_effect=FakeTimer, singleShot=lambda *_: None)
        )
        self.guard = IndicatorGuard(cast(Any, self.pill), cast(Any, self.tray), grace_ms=0)
        self.rig.core._pill = self.pill
        self.rig.core._tray = self.tray
        self.rig.core._set_tray_trusted = self.guard.set_tray_trusted
        self.guard.on_stop_recording = self.rig.core.on_indicators_lost

        def recording(value: bool) -> None:
            self.rig.set_recording(value)
            self.guard.set_recording(value)

        self.rig.core._set_recording = recording
        self.events: Queue[dict[str, Any]] = Queue()
        self.responses: Queue[dict[str, Any]] = Queue()
        self.command_progress: Queue[str] = Queue()
        self.delivered: list[dict[str, Any]] = []
        self.trace: list[str] = []
        self.seed: int | None = None
        self.channel = Channel()
        self.supervisor = WorkerSupervisor(
            self._receive_gui, use_qt=False, clock=lambda: self.rig.now
        )
        self.supervisor.state = "running"
        self.supervisor._receive(ipc.encode(ipc.make_hello()), self.supervisor.generation)
        self.delivered.clear()
        self.supervisor._socket = cast(Any, self.channel)
        # Наблюдатель Rig игнорирует ответ send(); cast снимает только тип возврата.
        self.rig.during_send = cast(Any, self.supervisor.send)
        self._new_worker()
        self.command_executor = ThreadPoolExecutor(max_workers=1)
        self.rig.restart_worker.side_effect = self.restart

    def _new_worker(self) -> None:
        self.worker_channel = Channel()
        self.loop = WorkerLoop(cast(Any, self.worker_channel), capture=False)
        self.loop.worker.close()
        self.loop._events = cast(Any, self.events)  # Queue вместо SimpleQueue: нужен get(timeout)
        self.source = Source()
        self.engine = FakeEngine(text=MARKER)
        self.worker = WorkerState(
            engine_factory=lambda _: self.engine,
            cancel_factory=FakeCancelToken,
            on_event=self.loop._put_event,
            audio_factory=list,
            clock=lambda: self.rig.now,
        )
        self.capture = AudioCapture(
            source=cast(Any, self.source),  # дублёр без flush/device_name: они здесь не вызываются
            on_samples=self.worker.on_samples,
            on_event=self.loop._put_event,
            on_error=self.worker.on_error,
        )
        self.worker.set_capture(self.capture)
        original_wait = self.capture.wait_stopped

        def observed_wait(uid: str) -> None:
            # Только наблюдение: настоящее ожидание и его дедлайн не меняются.
            self.command_progress.put("wait")
            original_wait(uid)

        self.capture.wait_stopped = observed_wait  # type: ignore[method-assign,assignment]
        self.loop.worker = self.worker
        loaded = self.worker.handle(
            dict(
                type="model.load",
                id="fake",
                revision="r",
                dir="/model",
                layout="fake",
                variant="int8",
                threads=1,
                min_ram_mb=512,
            )
        )
        assert loaded[0]["type"] == "model.loaded"

    def _receive_gui(self, event: dict[str, Any]) -> None:
        self.delivered.append(dict(event))
        self.rig.core.on_worker_event(event)

    def restart(self) -> None:
        # ОС закрывает ресурс завершённого процесса до смены поколения.
        self.source.release_close.set()
        self.capture.stop()
        self.worker.close()
        self.loop._wake_r.close()
        self.loop._wake_w.close()
        assert not self.source.is_open
        self.rig.worker_generation += 1
        self.supervisor.generation = self.rig.worker_generation
        self.supervisor._receive(ipc.encode(ipc.make_hello()), self.supervisor.generation)
        self.supervisor._pending.clear()
        self.supervisor._finished.clear()
        self.channel.commands.clear()
        for queue in (self.events, self.responses):
            while not queue.empty():
                queue.get_nowait()
        self._new_worker()

    def handle_one(self) -> list[dict[str, Any]]:
        command = self.channel.commands.popleft()
        # Настоящий main добавляет uid/request_type даже прежнему WorkerState.
        # Ответы уходят внутри _receive; события capture — в следующем обороте.
        assert self.loop._receive(ipc.encode(command))
        self.loop._flush_send()
        replies = list(self.worker_channel.commands)
        self.worker_channel.commands.clear()
        for reply in replies:
            self.responses.put(reply)
        self.command_progress.put("done")
        return replies

    def commands(self) -> None:
        while self.channel.commands:
            self.handle_one()

    def deliver(self, message: dict[str, Any]) -> None:
        self.supervisor._receive(ipc.encode(message), self.supervisor.generation)
        self.check()

    def drain_responses(self) -> None:
        while not self.responses.empty():
            self.deliver(self.responses.get_nowait())

    def serialize_events(self) -> list[dict[str, Any]]:
        """Начало оборота WorkerLoop: до _receive сериализовать до 64 событий."""
        for _ in range(64):
            try:
                message = self.events.get_nowait()
            except Empty:
                break
            self.loop._send(message)
        self.loop._flush_send()
        messages = list(self.worker_channel.commands)
        self.worker_channel.commands.clear()
        return messages

    def drain(self) -> None:
        self.drain_responses()
        for message in self.serialize_events():
            self.deliver(message)

    def start(self) -> str | None:
        before = len(self.rig.sent)
        self.rig.fsm.press(self.rig.now)
        self.commands()
        starts = [m for m, _ in self.rig.sent[before:] if m["type"] == "record.start"]
        if not starts:
            return None
        assert self.source.opened.wait(2)
        # record.started доказывает, что поток прошёл open, а не только вошёл в него.
        while True:
            message = self.events.get(timeout=2)
            self.deliver(message)
            if message["type"] == "record.started":
                break
        self.check()
        return str(starts[-1]["utterance_id"])

    def check(self) -> None:
        self.guard.check()
        if self.source.is_open:
            assert self.guard.recording and self.rig.recording, (
                f"O4: источник открыт, страж выключен; seed={self.seed}; trace={self.trace}"
            )
            assert (
                any(c._cancel_pending for c in self.rig.core._contexts)
                or (
                    self.pill.visible
                    and self.pill.state
                    in {PillState.LISTENING, PillState.LISTENING_SILENT, PillState.LIMIT}
                )
                or (
                    self.guard._tray_trusted
                    and self.tray.registered
                    and self.tray.state is TrayState.LISTENING
                )
            ), f"O4: нет индикации; seed={self.seed}; trace={self.trace}"

    def settle(self) -> None:
        self.source.release_close.set()
        self.rig.fsm.escape(self.rig.now)
        for _ in range(30):
            self.commands()
            # Дождаться настоящего задания движка, не подменяя его исход.
            if self.worker._active is not None:
                self.engine.finished.wait(2)
            self.drain()
            timers = [t for t in self.rig.timers if not t.cancelled and not t.fired]
            if not timers and not self.channel.commands:
                break
            for timer in timers:
                if not timer.cancelled and not timer.fired:
                    self.rig.now += timer.delay / 1000
                    timer.fire()
            self.check()
        assert not self.source.is_open
        assert not self.guard.recording and not self.rig.recording
        assert self.rig.core.phase is DictationPhase.IDLE
        assert self.rig.fsm.state is HotkeyState.IDLE

    def close(self) -> None:
        self.source.release_close.set()
        self.capture.stop()
        self.command_executor.shutdown(wait=True)
        self.worker.close()
        self.loop._wake_r.close()
        self.loop._wake_w.close()
        self.supervisor.stop()


@pytest.mark.parametrize("trigger", ["capture-error", "escape"])
def test_stop_reply_cannot_ack_pending_cancel(
    monkeypatch: pytest.MonkeyPatch, trigger: str
) -> None:
    """ИБ-46: реальный супервизор не переписывает stop и не теряет cancel."""
    chain = Chain(monkeypatch, HotkeyMode.PTT)
    try:
        uid = chain.start()
        assert uid is not None
        chain.source.release_close.clear()
        chain.rig.stop()
        # start -> stop -> recognize находятся в настоящем wire-канале.
        if trigger == "capture-error":
            chain.source.fault.set()
            assert chain.source.closing.wait(2)
            while True:
                message = chain.events.get(timeout=2)
                chain.deliver(message)
                if message["type"] == "error":
                    break
        else:
            chain.rig.fsm.escape(chain.rig.now)
        assert any(m["type"] == "record.cancel" for m in chain.channel.commands)
        # Даже ошибочный/прежний воркер может ответить раньше close: первая защита GUI.
        chain.deliver(
            dict(
                type="error",
                code="bad-state",
                message="Нет записи.",
                utterance_id=uid,
                request_type="record.stop",
            )
        )
        assert chain.delivered[-1]["request_type"] == "record.stop"
        assert chain.guard.recording and chain.rig.recording
        assert any(c._utterance_id == uid and c._cancel_pending for c in chain.rig.core._contexts)
        chain.source.release_close.set()
        chain.commands()
        chain.capture.wait_stopped(uid)
        chain.drain()
        assert any(e.get("request_type") == "record.cancel" for e in chain.delivered)
        assert not chain.guard.recording and not chain.rig.recording
        chain.rig.restart_worker.assert_not_called()
    finally:
        chain.close()


@pytest.mark.parametrize("kind", ["record.stop", "recognize"])
def test_bad_state_waits_through_real_chain(monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
    """ИБ-46/51: ответ WorkerState не выходит в IPC до физического close."""
    chain = Chain(monkeypatch, HotkeyMode.PTT)
    future: Future[list[dict[str, Any]]] | None = None
    try:
        uid = chain.start()
        assert uid is not None
        chain.source.release_close.clear()
        chain.source.fault.set()
        assert chain.source.closing.wait(2)
        # Ошибка пока в транспорте: GUI успевает отправить stop/recognize.
        chain.supervisor.send(dict(type=kind, utterance_id=uid))
        while not chain.command_progress.empty():
            chain.command_progress.get_nowait()
        future = chain.command_executor.submit(chain.handle_one)
        # Поток действительно вошёл в ожидание закрывающегося владельца,
        # а не просто ещё не получил процессорное время.
        assert chain.command_progress.get(timeout=2) == "wait"
        assert not future.done()
        chain.drain()
        chain.check()
        chain.source.release_close.set()
        replies = future.result(timeout=2)
        assert replies[0]["code"] == "bad-state"
        assert replies[0]["request_type"] == kind
        assert not chain.source.is_open
        chain.commands()
        chain.drain()
    finally:
        chain.close()


@pytest.mark.parametrize("finish", ["cancelled", "restart"])
def test_rejected_close_cannot_adopt_previous_phrases_ack(
    monkeypatch: pytest.MonkeyPatch, finish: str
) -> None:
    """F1: ручная доставка настоящих ответов A после отклонённого close B."""
    chain = Chain(monkeypatch, HotkeyMode.PTT)
    try:
        uid_a = chain.start()
        chain.rig.fsm.escape(chain.rig.now)
        chain.rig.timer(CANCEL_TIMEOUT_MS).fire()
        chain.commands()
        replies = []
        while not chain.responses.empty():
            replies.append(chain.responses.get_nowait())
        closed_a = next(r for r in replies if r["type"] == "audio.closed")
        cancel_a = next(
            r for r in replies if r["type"] == "cancelled" and r["utterance_id"] == uid_a
        )
        chain.deliver(cancel_a)
        chain.drain()
        chain.rig.now += 1
        uid_b = chain.start()
        assert uid_b is not None and uid_b != uid_a
        chain.rig.fsm.escape(chain.rig.now)
        chain.rig.timer(CANCEL_TIMEOUT_MS).fire()
        # Supervisor ещё ждёт close A и отклонил close B до отправки в wire.
        assert not any(c["type"] == "audio.close" for c in chain.channel.commands)
        context_b = next(c for c in chain.rig.core._contexts if c._utterance_id == uid_b)
        assert not context_b._awaiting_audio_closed
        chain.deliver(closed_a)
        assert chain.source.is_open and context_b._capture_open
        assert chain.guard.recording and chain.rig.recording and context_b._cancel_pending
        if finish == "cancelled":
            chain.commands()
            chain.drain()
            chain.rig.restart_worker.assert_not_called()
        else:
            chain.rig.timer(CANCEL_RESTART_MS).fire()
            chain.rig.restart_worker.assert_called_once_with()
        assert not chain.source.is_open and not context_b._capture_open
        assert not chain.guard.recording and not chain.rig.recording
        chain.settle()
    finally:
        chain.close()


def run_fixed_seed_chain(
    monkeypatch: pytest.MonkeyPatch,
    *,
    seed: int,
    mode: HotkeyMode,
    steps: int,
    tray_faults: bool = False,
    caplog: pytest.LogCaptureFixture | None = None,
) -> list[str]:
    """Общий генератор для обоих фаззов и mutation matrix.

    Сначала исследуется опасное окно: GUI уже послал stop+recognize, capture
    потерял PCM, но Source.close удерживается Event. Случайный планировщик
    выбирает порядок command-thread, события ошибки и Esc. Ответы из _receive
    могут обогнать ещё не сериализованные события capture (как в WorkerLoop).
    После окна — свободная случайная цепь жестов, таймеров и UI-сбоев.

    O4 проверяется немедленно после каждого перехода. Нарушения двух отдельных
    защит собираются до конца: они не заслоняют O4 на полностью старом коде.
    Возвращаемые BARRIER/PROVENANCE используются теми же mutation-тестами.
    """
    rng = random.Random(seed)
    chain = Chain(monkeypatch, mode)
    chain.seed = seed
    violations: list[str] = []
    original_tray = chain.tray.set_state
    fault_state = [False]

    def set_state(state: TrayState, tooltip: str | None = None) -> None:
        if fault_state[0] and state is TrayState.LISTENING:
            raise RuntimeError(MARKER)
        original_tray(state, tooltip)

    chain.tray.set_state = set_state  # type: ignore[method-assign]
    original_deliver = chain.deliver

    def deliver(message: dict[str, Any]) -> None:
        before = len(chain.delivered)
        chain.trace.append(f"wire:{message['type']}:{message.get('request_type', '-')}")
        original_deliver(message)  # O4 имеет приоритет над диагностикой контракта.
        if "request_type" in message:
            for delivered in chain.delivered[before:]:
                if delivered.get("request_type") != message["request_type"]:
                    violations.append("PROVENANCE: supervisor changed wire request_type")

    chain.deliver = deliver  # type: ignore[method-assign]
    future: Future[list[dict[str, Any]]] | None = None
    try:
        chain.trace.append("start")
        uid = chain.start()
        assert uid is not None
        chain.source.release_close.clear()
        chain.trace.append("hold-close")
        chain.check()
        chain.rig.now += rng.uniform(0.35, 0.8)
        chain.trace.append("gui-stop+recognize")
        chain.rig.stop()
        assert [c["type"] for c in chain.channel.commands] == ["record.stop", "recognize"]
        chain.check()
        chain.trace.append("capture-fault")
        chain.source.fault.set()
        assert chain.source.closing.wait(2)
        chain.check()

        # Эти действия — разные допустимые порядки двух циклов GUI/worker.
        # В варианте Esc событие может оставаться в очереди до ответа команды.
        via_error = rng.choice([False, True])
        actions = ["command", "capture-error" if via_error else "escape"]
        if not via_error and rng.choice([False, True]):
            actions.append("capture-error")
        rng.shuffle(actions)
        # Если GUI получит error, кадры уже отправлены ДО чтения команд воркером.
        # Пока _receive блокируется в wait, GUI может прочитать эти старые кадры,
        # но рабочий цикл не способен сериализовать новые события.
        staged_events = chain.serialize_events() if "capture-error" in actions else []
        while not chain.command_progress.empty():
            chain.command_progress.get_nowait()
        for action in actions:
            chain.trace.append(action)
            if action == "command":
                # По проводу идут stop, затем recognize; старый handle успевает
                # вернуть оба bad-state. Новый блокирует уже первую команду.
                for _ in range(2):
                    future = chain.command_executor.submit(chain.handle_one)
                    progress = chain.command_progress.get(timeout=2)
                    if progress == "wait":
                        chain.trace.append("command-waits")
                        break
                    replies = future.result(timeout=2)
                    if any(r.get("code") == "bad-state" for r in replies):
                        violations.append("BARRIER: bad-state before physical close")
                    chain.trace.append("command-replied-before-close")
                    chain.check()
            elif action == "escape":
                chain.rig.fsm.escape(chain.rig.now)
            else:
                # Сохраняем FIFO событий. Ошибка должна вызвать настоящий cancel.
                assert any(event["type"] == "error" for event in staged_events)
                for event in staged_events:
                    chain.deliver(event)
                staged_events.clear()
            chain.check()
        assert future is not None
        assert any(c["type"] == "record.cancel" for c in chain.channel.commands), (
            seed,
            chain.trace,
        )
        chain.trace.append("responses-before-close")
        # Если старый handle уже ответил, это реальные ответы _receive, а не
        # заранее изготовленный bad-state. Новый handle всё ещё ждёт close.
        chain.drain_responses()
        chain.check()
        chain.trace.append("release-close")
        chain.source.release_close.set()
        future.result(timeout=2)
        chain.check()
        # Перестановка выбирает границу следующего оборота рабочего цикла:
        # уже сериализованные ответы FIFO; накопленные события могут пойти
        # перед обработкой следующих команд либо после их синхронных ответов.
        if rng.choice([False, True]):
            chain.drain()
        chain.commands()
        chain.drain()
        chain.check()
        chain.settle()

        chain.rig.now += 0.5
        chain.trace.append("next-start")
        assert chain.start() is not None
        for step in range(steps):
            chain.rig.now += rng.uniform(0.35, 0.8)
            action = rng.choice(
                ("press", "release", "escape", "timer", "deliver", "fault", "level", "pill", "tray")
            )
            chain.trace.append(f"{step}:{action}")
            if action == "press":
                chain.rig.fsm.press(chain.rig.now)
            elif action == "release":
                chain.rig.fsm.release(chain.rig.now)
            elif action == "escape":
                chain.rig.fsm.escape(chain.rig.now)
            elif action == "timer":
                timers = [t for t in chain.rig.timers if not t.cancelled and not t.fired]
                if timers:
                    timer = rng.choice(timers)
                    chain.rig.now += timer.delay / 1000
                    timer.fire()
            elif action == "deliver":
                chain.drain()
            elif action == "fault" and chain.source.is_open:
                chain.source.fault.set()
                assert chain.source.closing.wait(2)
            elif action == "level":
                chain.capture.check_stop_watchdog()
            elif action == "pill":
                chain.pill.available = not chain.pill.available
            elif action == "tray" and tray_faults:
                fault_state[0] = not fault_state[0]
            chain.check()
            chain.commands()
            chain.check()
            if caplog is not None:
                assert not any(MARKER in r.getMessage() for r in caplog.records), (
                    seed,
                    chain.trace,
                )
            assert not any(MARKER in str(e) for e in chain.rig.stats.events), (seed, chain.trace)
        fault_state[0] = False
        chain.settle()
        return violations
    except AssertionError as exc:
        raise AssertionError(f"{exc}; seed={seed}; mode={mode}; trace={chain.trace}") from exc
    finally:
        # Освободить владельца до join command-thread даже при падении O4.
        chain.close()


def run_fixed_seed_corpus(
    monkeypatch: pytest.MonkeyPatch,
    *,
    first_seed: int,
    count: int,
    mode: HotkeyMode,
    steps: int,
    tray_faults: bool = False,
    caplog: pytest.LogCaptureFixture | None = None,
) -> list[str]:
    """Не дать контракту одного seed заслонить нарушение O4 в другом порядке."""
    violations: list[str] = []
    for seed in range(first_seed, first_seed + count):
        found = run_fixed_seed_chain(
            monkeypatch,
            seed=seed,
            mode=mode,
            steps=steps,
            tray_faults=tray_faults,
            caplog=caplog,
        )
        violations.extend(f"{violation}; seed={seed}" for violation in found)
        if caplog is not None:
            caplog.clear()
    return violations


@pytest.mark.parametrize("mode", [HotkeyMode.PTT, HotkeyMode.TOGGLE])
@pytest.mark.parametrize("tray_faults", [False, True])
def test_overlap_fixed_seed_invariants(
    mode: HotkeyMode,
    tray_faults: bool,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """ИБ-51: 32 seed × 80 случайных шагов × 4 варианта + окно slow-close."""
    caplog.set_level(logging.DEBUG, logger="test.dictation")
    violations = run_fixed_seed_corpus(
        monkeypatch,
        first_seed=0xA57A_2026,
        count=32,
        mode=mode,
        steps=80,
        tray_faults=tray_faults,
        caplog=caplog,
    )
    assert not violations, violations


@pytest.mark.parametrize("mode", [HotkeyMode.PTT, HotkeyMode.TOGGLE])
def test_capture_fixed_seed_async_ack_invariants(
    mode: HotkeyMode,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ИБ-51: 24 других seed × 2 режима: окно ACK и ещё 24 случайных шага."""
    violations = run_fixed_seed_corpus(
        monkeypatch, first_seed=0xAC4_2026, count=24, mode=mode, steps=24
    )
    assert not violations, violations


@pytest.mark.parametrize(
    ("seed", "mode"),
    [
        (900203, HotkeyMode.TOGGLE),
        (900432, HotkeyMode.TOGGLE),
        (900461, HotkeyMode.TOGGLE),
        (900518, HotkeyMode.PTT),
        (900518, HotkeyMode.TOGGLE),
    ],
)
def test_terminal_tail_fixed_seed_regressions(
    monkeypatch: pytest.MonkeyPatch, seed: int, mode: HotkeyMode
) -> None:
    """ИБ-53: точные параметры p_fuzz_wide/p_seed_fix, включая settle до IDLE."""
    violations = run_fixed_seed_chain(
        monkeypatch, seed=seed, mode=mode, steps=120, tray_faults=bool(seed % 2)
    )
    assert not violations, violations


def _mutate_last_request_type(monkeypatch: pytest.MonkeyPatch) -> None:
    """Вернуть ошибку provenance: ACK помечен последним запросом того же uid."""
    original = WorkerSupervisor._finish
    original_correlation = supervisor_module._correlation

    def correlation(message: dict[str, Any]) -> Any:
        key = original_correlation(message)
        return key[:2] if key[0] == "utterance" else key

    monkeypatch.setattr(supervisor_module, "_correlation", correlation)

    def overwrite(self: WorkerSupervisor, key: Any, message: dict[str, Any]) -> dict[str, Any]:
        latest = next(
            (p.request_type for k, p in reversed(self._pending.items()) if k[:2] == key[:2]),
            None,
        )
        event = original(self, key, message)
        if latest is not None:
            event["request_type"] = latest
        return event

    monkeypatch.setattr(WorkerSupervisor, "_finish", overwrite)


def _mutate_early_bad_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Прежний handle: нет добавленного wait для stop/recognize bad-state.

    Существующие deferred (в том числе ожидание cancel) остаются настоящими.
    Метаданные текущего handle сохранены: отключается только новый барьер.
    """

    def handle(self: WorkerState, message: dict[str, Any]) -> list[dict[str, Any]]:
        deferred: list[Any] = []
        with self._lock:
            replies = self._handle(message, deferred)
            for reply in replies:
                reply.setdefault("request_type", message["type"])
                if reply["type"] == "error" and "utterance_id" in message:
                    reply.setdefault("utterance_id", message["utterance_id"])
        for action in deferred:
            action()
        return replies

    monkeypatch.setattr(WorkerState, "handle", handle)


@pytest.mark.parametrize("mutation", ["provenance", "barrier", "both"])
@pytest.mark.parametrize("mode", [HotkeyMode.PTT, HotkeyMode.TOGGLE])
def test_fixed_seed_mutation_matrix(
    monkeypatch: pytest.MonkeyPatch, mutation: str, mode: HotkeyMode
) -> None:
    """Тот же генератор: одна защита держит O4, обе сняты — O4 нарушается."""
    with monkeypatch.context() as patch:
        if mutation in {"provenance", "both"}:
            _mutate_last_request_type(patch)
        if mutation in {"barrier", "both"}:
            _mutate_early_bad_state(patch)
        if mutation == "both":
            with pytest.raises(AssertionError, match="O4: источник открыт, страж выключен"):
                run_fixed_seed_corpus(patch, first_seed=0xA57A_2026, count=32, mode=mode, steps=80)
        else:
            violations = run_fixed_seed_corpus(
                patch, first_seed=0xA57A_2026, count=32, mode=mode, steps=80
            )
            assert any(v.startswith(mutation.upper()) for v in violations), violations
