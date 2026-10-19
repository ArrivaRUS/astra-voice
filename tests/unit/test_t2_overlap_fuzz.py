"""Фиксированный фазз ИБ: О4, очередь, автомат клавиши и приватность (US-2.7)."""

from __future__ import annotations

import logging
import random
from collections.abc import Callable
from typing import Any, cast
from unittest.mock import Mock

import pytest
from test_dictation import MARKER, Rig, Timer
from test_indicators import FakePill, FakeTimer, FakeTray

from astra_voice.core.dictation import (
    CANCEL_TIMEOUT_MS,
    RELEASE_TAIL_MS,
    DictationPhase,
)
from astra_voice.platform.hotkey import HotkeyMode, HotkeyState
from astra_voice.platform.paste import PasteOutcomeKind
from astra_voice.ui import indicators
from astra_voice.ui.indicators import IndicatorGuard
from astra_voice.ui.pill import PillState
from astra_voice.ui.tray_icons import TrayState

pytestmark = pytest.mark.unit
LISTENING = {PillState.LISTENING, PillState.LISTENING_SILENT, PillState.LIMIT}


class WorkerModel:
    """Захват на стороне воркера: только wire-команды и физические границы.

    Модель не читает состояние оркестратора/FSM/стража. В частности, cancel(A)
    закрывает только A, а глобальная ошибка сама по себе не закрывает B.
    """

    def __init__(self, *, defer_stop: bool = False) -> None:
        self.generation = 1
        self.captures: set[str] = set()
        self.known: set[str] = set()
        self.defer_stop = defer_stop
        self.stop_requests: set[str] = set()
        self.cancel_replies: dict[str, dict[str, Any]] = {}
        self.sent: list[tuple[dict[str, Any], int]] = []

    def send(self, message: dict[str, Any]) -> None:
        self.sent.append((dict(message), self.generation))
        kind = message["type"]
        if kind == "record.start":
            uid = str(message["utterance_id"])
            self.captures.add(uid)
            self.known.add(uid)
        elif kind in {"record.stop", "record.cancel", "recognize"}:
            uid = str(message["utterance_id"])
            if kind == "record.cancel":
                self.cancel_replies[uid] = (
                    {"type": "cancelled"}
                    if uid in self.known
                    else {"type": "error", "code": "bad-state", "request_type": kind}
                )
                self.known.discard(uid)
            if self.defer_stop:
                self.stop_requests.add(uid)
            else:
                self.close(uid)
        elif kind == "audio.close":
            self.captures.clear()

    def close(self, uid: str) -> None:
        self.captures.discard(uid)

    def restart(self) -> None:
        self.generation += 1
        self.captures.clear()
        self.known.clear()
        self.stop_requests.clear()
        self.cancel_replies.clear()


@pytest.mark.parametrize("mode", [HotkeyMode.PTT, HotkeyMode.TOGGLE])
@pytest.mark.parametrize("tray_faults", [False, True])
def test_overlap_fixed_seed_invariants(
    mode: HotkeyMode,
    tray_faults: bool,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Логика fuzz_overlap.py: 128 последовательностей по 80 шагов, затем досчёт."""
    caplog.set_level(logging.DEBUG, logger="test.dictation")
    for seed in range(32):
        run_sequence(mode, caplog, seed, tray_faults, monkeypatch)


def run_sequence(
    mode: HotkeyMode,
    caplog: pytest.LogCaptureFixture,
    seed: int,
    tray_faults: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    caplog.clear()
    rng = random.Random(0xA57A_2026 + seed)
    rig = Rig(mode)
    pill = FakePill()
    tray = FakeTray()
    raising = False
    original_set_state = tray.set_state

    def set_state(state: TrayState, tooltip: str | None = None) -> None:
        if raising and state is TrayState.LISTENING:
            raise RuntimeError(MARKER)
        original_set_state(state, tooltip)

    monkeypatch.setattr(tray, "set_state", set_state)
    monkeypatch.setattr(indicators, "monotonic", lambda: rig.now)
    monkeypatch.setattr(
        indicators,
        "QTimer",
        Mock(side_effect=FakeTimer, singleShot=lambda delay, callback: None),
    )
    # Нулевой грейс позволяет проверять строгий О4 на каждом шаге; реальные
    # границы грейса отдельно проверены в test_indicators и runtime T2.
    guard = IndicatorGuard(cast(Any, pill), cast(Any, tray), grace_ms=0)
    rig.core._pill = pill
    rig.core._tray = tray
    rig.core._set_tray_trusted = guard.set_tray_trusted
    guard.on_stop_recording = rig.core.on_indicators_lost

    def set_recording(value: bool) -> None:
        rig.set_recording(value)
        guard.set_recording(value)

    rig.core._set_recording = set_recording
    worker = WorkerModel()
    sent = worker.sent
    delivered: set[tuple[str, str]] = set()
    history: list[str] = []
    rig.during_send = worker.send
    tail_deadlines: dict[int, tuple[Timer, float]] = {}

    def schedule(delay: int, callback: Callable[[], None]) -> object:
        timer = cast(Timer, rig.schedule(delay, callback))
        if delay == RELEASE_TAIL_MS:
            tail_deadlines[id(timer)] = (timer, rig.now + delay / 1000)
        return timer

    rig.core._schedule = schedule

    def advance(seconds: float) -> None:
        # Длинный случайный скачок не растягивает физический хвост записи.
        target = rig.now + seconds
        for timer, deadline in sorted(tail_deadlines.values(), key=lambda pair: pair[1]):
            if not timer.cancelled and not timer.fired and deadline <= target:
                rig.now = max(rig.now, deadline)
                timer.fire()
        rig.now = max(rig.now, target)

    def recording() -> list[str]:
        return sorted(worker.captures)

    def pending(kind: str) -> list[str]:
        return [
            str(m["utterance_id"])
            for m, generation in sent
            if m["type"] == kind
            and generation == rig.worker_generation
            and (kind, str(m["utterance_id"])) not in delivered
        ]

    def event(event_type: str, uid: str | None = None, **fields: object) -> None:
        message: dict[str, Any] = {
            "type": event_type,
            "generation": rig.worker_generation,
            **fields,
        }
        if uid is not None:
            message["utterance_id"] = uid
        rig.core.on_worker_event(message)

    def check() -> None:
        guard.check()
        guard.check()
        context = f"seed={seed} mode={mode.name} actions={history[-20:]}"
        active = recording()
        assert len(active) <= 1, context
        for uid in active:
            assert guard.recording and rig.recording, context
            assert (pill.visible and pill.state in LISTENING) or (
                guard._tray_trusted and tray.registered and tray.state is TrayState.LISTENING
            ), context
            tail = any(c._utterance_id == uid and c._tail_pending for c in rig.core._contexts)
            bounded_tail = tail and any(
                not timer.cancelled
                and not timer.fired
                and rig.now <= deadline <= rig.now + RELEASE_TAIL_MS / 1000
                for timer, deadline in tail_deadlines.values()
            )
            assert rig.fsm.state is HotkeyState.RECORDING or bounded_tail, context
        assert not any(MARKER in record.getMessage() for record in caplog.records), context
        assert not any(MARKER in str(entry) for entry in rig.stats.events), context
        if rig.core.phase is DictationPhase.IDLE:
            assert not rig.core._items, context

    def phrase_state() -> tuple[object, ...]:
        # Только семантика фраз/команд/индикации; clock() и прочая трассировка
        # не являются частью контракта изоляции глобальной ошибки.
        return (
            [(c._utterance_id, c._phase) for c in rig.core._items],
            list(sent),
            set(worker.captures),
            rig.fsm.state,
            guard.recording,
            rig.recording,
            pill.visible,
            pill.state,
            tray.state,
            list(rig.stats.events),
            list(rig.pasted),
        )

    def global_error(code: str = "engine-failed") -> None:
        # О4 допускает безопасную отмену B при потере индикации. Здесь другой
        # контракт: без id нет владельца ошибки, значит отменять B тоже нельзя.
        before = phrase_state()
        event("error", code=code)
        context = f"idless error changed phrase: seed={seed} mode={mode.name} actions={history}"
        assert phrase_state() == before, context

    # Каждый seed гарантирует timeout A и позднюю ошибку во время записи B.
    # Нечётные seed также проверяют угадывание PROCESSING A старым _route.
    history.append("directed:start_A")
    rig.fsm.press(rig.now)
    check()
    advance(rng.uniform(0.5, 1.0))
    history.append("directed:stop_A")
    if mode is HotkeyMode.TOGGLE:
        rig.fsm.press(rig.now)
    else:
        rig.fsm.release(rig.now)
    check()
    advance(RELEASE_TAIL_MS / 1000)
    check()
    (a_uid,) = pending("recognize")
    advance(rng.uniform(0.5, 1.0))
    history.append("directed:start_B")
    rig.fsm.press(rig.now)
    (b_uid,) = recording()
    assert b_uid != a_uid
    check()
    if seed % 2:
        history.append("directed:global_engine_error")
        global_error()
        check()
    history.append("directed:timeout_A")
    event("error", a_uid, code="timeout", request_type="recognize")
    delivered.add(("recognize", a_uid))
    assert any(m["type"] == "record.cancel" and m.get("utterance_id") == a_uid for m, _ in sent)
    assert recording() == [b_uid]
    check()
    history.append("directed:late_global_engine_error_while_B_records")
    global_error()
    assert recording() == [b_uid]
    check()

    actions = (
        "press",
        "release_short",
        "release_long",
        "escape",
        "tick_limit",
        "timer",
        "timer",
        "result",
        "empty",
        "error",
        "global_error",
        "cancelled",
        "limit",
        "device",
        "restart",
        "pill",
        "audio_closed",
        "level",
        "outcome",
        "pill_enabled",
        *(["tray_raise"] if tray_faults else []),
    )
    for _ in range(80):
        action = rng.choice(actions)
        history.append(action)
        if action == "press":
            advance(0.5)
            rig.fsm.press(rig.now)
        elif action in {"release_short", "release_long"}:
            advance(0.1 if action == "release_short" else 1.0)
            rig.fsm.release(rig.now)
        elif action == "escape":
            rig.fsm.escape(rig.now)
        elif action == "tick_limit":
            advance(200)
            rig.fsm.tick(rig.now)
        elif action == "timer":
            timers = [t for t in rig.timers if not t.cancelled and not t.fired]
            if timers:
                timer = rng.choice(timers)
                advance(timer.delay / 1000)
                if not timer.cancelled and not timer.fired:
                    timer.fire()
        elif action in {"result", "empty", "error"}:
            candidates = pending("recognize")
            if candidates:
                uid = rng.choice(candidates)
                delivered.add(("recognize", uid))
                worker.known.discard(uid)
                advance(0.3)
                if action == "error":
                    event(
                        "error",
                        uid,
                        code=rng.choice(["engine-failed", "timeout", "busy"]),
                        request_type="recognize",
                    )
                else:
                    event("result", uid, text="" if action == "empty" else MARKER)
        elif action == "global_error":
            global_error(rng.choice(["engine-failed", "timeout", "busy"]))
        elif action == "cancelled":
            candidates = pending("record.cancel")
            if candidates:
                uid = rng.choice(candidates)
                delivered.add(("record.cancel", uid))
                worker.close(uid)
                reply = worker.cancel_replies[uid]
                event(str(reply["type"]), uid, **{k: v for k, v in reply.items() if k != "type"})
        elif action in {"limit", "device"}:
            active = recording()
            if active:
                uid = active[0]
                worker.close(uid)
                if action == "limit":
                    event("record.limit", uid)
                else:
                    event(
                        "audio.device.changed",
                        uid,
                        kind=rng.choice(["device-lost", "switched"]),
                        label="X",
                        audio_ms=rng.choice([100, 1000]),
                    )
        elif action == "restart":
            worker.restart()
            rig.worker_generation = worker.generation
            event("error", code="worker-crashed")
        elif action == "pill":
            pill.available = not pill.available
        elif action == "pill_enabled":
            pill.enabled = not pill.enabled
        elif action == "tray_raise":
            raising = not raising
        elif action == "audio_closed":
            worker.captures.clear()
            event("audio.closed")
        elif action == "level":
            active = recording()
            if active:
                event("level", active[0], peak_dbfs=-20.0)
        elif action == "outcome":
            rig.outcomes.append(rng.choice(list(PasteOutcomeKind)))
        check()

    raising = False
    for _ in range(200):
        progressed = False
        if rig.fsm.state is HotkeyState.RECORDING:
            advance(1)
            if rig.fsm.mode is HotkeyMode.TOGGLE:
                rig.fsm.press(rig.now)
            else:
                rig.fsm.release(rig.now)
            progressed = True
        for kind in ("recognize", "record.cancel"):
            candidates = pending(kind)
            if candidates:
                uid = candidates[0]
                delivered.add((kind, uid))
                if kind == "record.cancel":
                    worker.close(uid)
                    reply = worker.cancel_replies[uid]
                    event(
                        str(reply["type"]), uid, **{k: v for k, v in reply.items() if k != "type"}
                    )
                else:
                    worker.known.discard(uid)
                    event("result", uid, text=MARKER)
                progressed = True
        timers = [t for t in rig.timers if not t.cancelled and not t.fired]
        if timers:
            timer = min(timers, key=lambda t: t.delay)
            advance(timer.delay / 1000)
            if not timer.cancelled and not timer.fired:
                timer.fire()
            progressed = True
        check()
        if not progressed:
            break
    context = f"seed={seed} mode={mode.name} actions={history[-20:]}"
    assert not rig.core._items, context
    assert rig.core.phase is DictationPhase.IDLE, context
    assert rig.fsm.state is HotkeyState.IDLE, context
    assert not guard.recording and not rig.recording and not recording(), context


class CaptureReplay:
    """Wire replay: время, физический захват и доставка ACK независимы от GUI."""

    def __init__(self, mode: HotkeyMode, monkeypatch: pytest.MonkeyPatch) -> None:
        self.rig = Rig(mode)
        self.rig.ack_stop = False
        self.worker = WorkerModel(defer_stop=True)
        self.rig.during_send = self.worker.send
        self.pill = FakePill()
        self.tray = FakeTray()
        monkeypatch.setattr(indicators, "monotonic", lambda: self.rig.now)
        monkeypatch.setattr(
            indicators,
            "QTimer",
            Mock(side_effect=FakeTimer, singleShot=lambda delay, callback: None),
        )
        self.guard = IndicatorGuard(cast(Any, self.pill), cast(Any, self.tray), grace_ms=0)
        self.rig.core._pill = self.pill
        self.rig.core._tray = self.tray
        self.rig.core._set_tray_trusted = self.guard.set_tray_trusted
        self.guard.on_stop_recording = self.rig.core.on_indicators_lost

        def set_recording(value: bool) -> None:
            self.rig.set_recording(value)
            self.guard.set_recording(value)

        self.rig.core._set_recording = set_recording
        self.deadlines: list[tuple[Timer, float]] = []

        def schedule(delay: int, callback: Callable[[], None]) -> object:
            timer = cast(Timer, self.rig.schedule(delay, callback))
            self.deadlines.append((timer, self.rig.now + delay / 1000))
            return timer

        self.rig.core._schedule = schedule

    def event(self, event_type: str, uid: str | None = None, **fields: object) -> None:
        message: dict[str, Any] = {
            "type": event_type,
            "generation": self.worker.generation,
            **fields,
        }
        if uid is not None:
            message["utterance_id"] = uid
        self.rig.core.on_worker_event(message)

    def advance(self, seconds: float) -> None:
        target = self.rig.now + seconds
        while True:
            due = [
                (timer, deadline)
                for timer, deadline in self.deadlines
                if not timer.cancelled and not timer.fired and deadline <= target
            ]
            if not due:
                break
            timer, deadline = min(due, key=lambda pair: pair[1])
            self.rig.now = max(self.rig.now, deadline)
            timer.fire()
            self.assert_o4()
        self.rig.now = max(self.rig.now, target)

    def start(self) -> str | None:
        before = set(self.worker.captures)
        self.rig.fsm.press(self.rig.now)
        started = self.worker.captures - before
        self.assert_o4()
        return next(iter(started)) if started else None

    def stop(self) -> None:
        if self.rig.fsm.mode is HotkeyMode.PTT:
            self.rig.fsm.release(self.rig.now)
        else:
            self.rig.fsm.press(self.rig.now)
        self.advance(RELEASE_TAIL_MS / 1000)
        self.assert_o4()

    def physical_stop(self, uid: str, *, acknowledge: bool) -> None:
        self.worker.close(uid)
        if acknowledge:
            self.event("record.stopped", uid)

    def result(self, uid: str, text: str) -> None:
        # Настоящий WorkerState забывает законченное задание до отправки result.
        self.worker.known.discard(uid)
        self.event("result", uid, text=text)
        self.assert_o4()

    def cancel_reply(self, uid: str) -> bool:
        reply = self.worker.cancel_replies.pop(uid, None)
        if reply is None:
            return False
        assert uid not in self.worker.captures, "cancel ACK до физического закрытия"
        self.event(str(reply["type"]), uid, **{k: v for k, v in reply.items() if k != "type"})
        self.assert_o4()
        return True

    def assert_o4(self) -> None:
        self.guard.check()
        assert len(self.worker.captures) <= 1, "два физических захвата одновременно"
        if not self.worker.captures:
            return
        assert self.guard.recording and self.rig.recording, "захват жив, страж выключен"
        assert (self.pill.visible and self.pill.state in LISTENING) or (
            self.guard._tray_trusted
            and self.tray.registered
            and self.tray.state is TrayState.LISTENING
        ), "физический захват без видимого состояния «слушаю»"


@pytest.mark.parametrize("mode", [HotkeyMode.PTT, HotkeyMode.TOGGLE])
@pytest.mark.parametrize("boundary", ["cold", "limit", "device"])
@pytest.mark.parametrize("stop_ack", ["missing", "delayed"])
def test_capture_cleanup_replay_never_closes_next_phrase(
    mode: HotkeyMode,
    boundary: str,
    stop_ack: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """ИБ-36: неизвестная A отвечает bad-state, B переживает все таймеры A."""
    caplog.set_level(logging.DEBUG, logger="test.dictation")
    replay = CaptureReplay(mode, monkeypatch)
    a_uid = replay.start()
    assert a_uid is not None
    replay.advance(0.5)
    if boundary == "cold":
        # Нет record.started: холодное открытие завершилось уже после stop.
        # Физическое закрытие быстрое, потеряно только его подтверждение.
        replay.worker.defer_stop = False
        replay.stop()
        replay.worker.defer_stop = True
        replay.physical_stop(a_uid, acknowledge=False)
    else:
        replay.event("record.started", a_uid)
        replay.physical_stop(a_uid, acknowledge=False)
        if boundary == "limit":
            replay.event("record.limit", a_uid)
        else:
            replay.event("audio.device.changed", a_uid, kind="switched", audio_ms=1000)
        # После автоостановки пользователь отпускает PTT / завершает toggle.
        replay.stop()
    assert any(
        m["type"] == "recognize" and m.get("utterance_id") == a_uid for m, _ in replay.worker.sent
    )

    # Без ACK можно отказать новому нажатию. Если B уже принята (старый код),
    # результат A и её отрицательный ACK всё равно не имеют права обрывать B.
    replay.advance(0.5)
    b_uid = replay.start()
    # Пустое распознавание тоже терминально; очистка A начинается до конца B.
    replay.result(a_uid, "")
    if a_uid in replay.worker.cancel_replies:
        assert replay.worker.cancel_replies[a_uid] == {
            "type": "error",
            "code": "bad-state",
            "request_type": "record.cancel",
        }
        replay.cancel_reply(a_uid)
    if stop_ack == "delayed":
        replay.event("record.stopped", a_uid)
    if b_uid is None:
        replay.advance(0.5)
        b_uid = replay.start()
    assert b_uid is not None, "после физического ACK следующее нажатие должно начать B"
    replay.event("record.started", b_uid)
    command_boundary = len(replay.worker.sent)
    replay.advance(CANCEL_TIMEOUT_MS / 1000 + 0.1)

    assert bool(replay.worker.captures == {b_uid}), "очистка A оборвала физический захват B"
    assert not any(m["type"] == "audio.close" for m, _ in replay.worker.sent[command_boundary:]), (
        "таймер A отправил глобальный audio.close во время B"
    )
    replay.rig.restart_worker.assert_not_called()
    replay.assert_o4()
    replay.stop()
    replay.physical_stop(b_uid, acknowledge=True)
    pasted_before_b = len(replay.rig.pasted)
    replay.result(b_uid, MARKER)
    assert len(replay.rig.pasted) == pasted_before_b + 1, "результат B потерян"
    assert replay.rig.pasted[-1][0] == MARKER
    assert not any(MARKER in record.getMessage() for record in caplog.records)
    assert not any(MARKER in str(entry) for entry in replay.rig.stats.events)


@pytest.mark.parametrize("mode", [HotkeyMode.PTT, HotkeyMode.TOGGLE])
@pytest.mark.parametrize("ack", ["record.stopped", "cancelled", "audio.closed"])
def test_delayed_cancel_replay_keeps_o4_until_physical_ack(
    mode: HotkeyMode,
    ack: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Esc не означает, что поток уже закрыл микрофон; таймаут тоже не ACK."""
    replay = CaptureReplay(mode, monkeypatch)
    uid = replay.start()
    assert uid is not None
    replay.event("record.started", uid)
    replay.advance(0.5)
    replay.rig.fsm.escape(replay.rig.now)
    assert uid in replay.worker.stop_requests
    # Новое нажатие в ожидании ACK не создаёт второй физический захват.
    assert replay.start() is None
    replay.assert_o4()
    replay.advance(CANCEL_TIMEOUT_MS / 1000 - 0.1)
    assert replay.worker.captures == {uid}
    if ack == "audio.closed":
        replay.advance(0.2)
        assert not replay.worker.captures
        # audio.close отправлен, но ответ ещё в пути: индикация остаётся.
        assert replay.guard.recording and replay.rig.recording
        replay.event("audio.closed")
    else:
        replay.physical_stop(uid, acknowledge=ack == "record.stopped")
        if ack == "cancelled":
            assert replay.cancel_reply(uid)
    assert not replay.guard.recording and not replay.rig.recording
    replay.rig.restart_worker.assert_not_called()


@pytest.mark.parametrize("mode", [HotkeyMode.PTT, HotkeyMode.TOGGLE])
def test_capture_fixed_seed_async_ack_invariants(
    mode: HotkeyMode,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Фиксированные перестановки stop/result/cancel ACK и поздних событий A."""
    caplog.set_level(logging.DEBUG, logger="test.dictation")
    for seed in range(24):
        rng = random.Random(0xAC4_2026 + seed)
        replay = CaptureReplay(mode, monkeypatch)
        uid = replay.start()
        assert uid is not None
        if rng.choice([False, True]):
            replay.event("record.started", uid)
        replay.advance(rng.uniform(0.35, 0.8))
        if rng.choice([False, True]):
            replay.rig.fsm.escape(replay.rig.now)
            replay.assert_o4()
            replay.advance(rng.uniform(0.01, 1.0))
            replay.physical_stop(uid, acknowledge=False)
            deliveries = ["stopped", "cancel"]
        else:
            replay.stop()
            replay.advance(rng.uniform(0.01, 0.3))
            replay.physical_stop(uid, acknowledge=False)
            deliveries = ["stopped", "result"]
        rng.shuffle(deliveries)
        for delivery in deliveries:
            if delivery == "stopped":
                replay.event("record.stopped", uid)
            elif delivery == "cancel":
                replay.cancel_reply(uid)
            else:
                replay.result(uid, MARKER if seed % 2 else "")
            replay.assert_o4()
            replay.advance(rng.uniform(0.01, 0.2))
        replay.cancel_reply(uid)
        replay.advance(0.5)
        next_uid = replay.start()
        assert next_uid is not None, f"seed={seed}: ACK не разблокировал запись"
        assert next_uid != uid
        replay.event("record.started", next_uid)
        before = len(replay.worker.sent)
        # Повторные старые события и все таймеры отмены A во время B.
        replay.event("record.stopped", uid)
        replay.event("error", uid, code="bad-state", request_type="record.cancel")
        replay.advance(4.0)
        assert replay.worker.captures == {next_uid}, f"seed={seed}: A оборвала B"
        assert not any(m["type"] == "audio.close" for m, _ in replay.worker.sent[before:])
        replay.rig.restart_worker.assert_not_called()
        replay.assert_o4()
        replay.stop()
        replay.physical_stop(next_uid, acknowledge=True)
        pasted_before_b = len(replay.rig.pasted)
        replay.result(next_uid, MARKER)
        assert len(replay.rig.pasted) == pasted_before_b + 1, f"seed={seed}: B потеряна"
        assert replay.rig.pasted[-1][0] == MARKER
    assert not any(MARKER in record.getMessage() for record in caplog.records)


@pytest.mark.parametrize("mode", [HotkeyMode.PTT, HotkeyMode.TOGGLE])
def test_device_event_before_physical_stop_keeps_listening_indicator(
    mode: HotkeyMode,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Событие смены устройства ещё не подтверждает закрытие старого источника."""
    replay = CaptureReplay(mode, monkeypatch)
    replay.tray.registered = False
    uid = replay.start()
    assert uid is not None
    replay.event("record.started", uid)
    replay.advance(0.5)
    replay.event("audio.device.changed", uid, kind="switched", label="USB", audio_ms=1000)
    assert uid in replay.worker.captures
    replay.assert_o4()
    assert replay.pill.visible and replay.pill.state in LISTENING
    replay.physical_stop(uid, acknowledge=True)
    assert not replay.guard.recording and not replay.rig.recording
    replay.result(uid, MARKER)
    assert replay.rig.pasted[-1][0] == MARKER


@pytest.mark.parametrize("mode", [HotkeyMode.PTT, HotkeyMode.TOGGLE])
def test_old_result_during_next_physical_stop_keeps_listening_indicator(
    mode: HotkeyMode,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Исход A не перекрывает единственную индикацию B, ждущей physical ACK."""
    replay = CaptureReplay(mode, monkeypatch)
    replay.tray.registered = False
    a_uid = replay.start()
    assert a_uid is not None
    replay.advance(0.5)
    replay.worker.defer_stop = False
    replay.stop()
    replay.worker.defer_stop = True
    replay.physical_stop(a_uid, acknowledge=True)
    replay.advance(0.5)
    b_uid = replay.start()
    assert b_uid is not None
    replay.event("record.started", b_uid)
    replay.advance(0.5)
    replay.stop()
    assert b_uid in replay.worker.captures
    replay.result(a_uid, "")
    replay.assert_o4()
    assert replay.pill.visible and replay.pill.state in LISTENING
    replay.physical_stop(b_uid, acknowledge=True)
    replay.result(b_uid, MARKER)
    assert replay.rig.pasted[-1][0] == MARKER


@pytest.mark.parametrize("mode", [HotkeyMode.PTT, HotkeyMode.TOGGLE])
def test_reentrant_timeout_completion_cannot_close_new_capture(
    mode: HotkeyMode,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ACK и новое нажатие из UI completion не дают старому timeout закрыть B."""
    replay = CaptureReplay(mode, monkeypatch)
    a_uid = replay.start()
    assert a_uid is not None
    replay.event("record.started", a_uid)
    replay.advance(0.5)
    replay.rig.fsm.escape(replay.rig.now)
    replay.assert_o4()
    original_finish = replay.rig.core._finish
    next_uids: list[str | None] = []

    def finish_then_receive_ack(state: PillState, *, tray: TrayState = TrayState.IDLE) -> None:
        # Реальное завершение UI выполнено; вложенный event-loop доставляет ACK
        # и жест пользователя до возврата в старый timeout. Источник A уже закрыт.
        original_finish(state, tray=tray)
        replay.physical_stop(a_uid, acknowledge=True)
        next_uids.append(replay.start())

    with monkeypatch.context() as callback_patch:
        callback_patch.setattr(replay.rig.core, "_finish", finish_then_receive_ack)
        replay.advance(CANCEL_TIMEOUT_MS / 1000 + 0.01)
    assert len(next_uids) == 1 and next_uids[0] is not None, "вложенный жест не начал B"
    b_uid = next_uids[0]
    assert b_uid in replay.worker.captures, "старый timeout закрыл B после вложенного ACK"
    assert not any(m["type"] == "audio.close" for m, _ in replay.worker.sent)
    replay.advance(4.0)
    assert b_uid in replay.worker.captures, "сторож отмены A перезапустил захват B"
    replay.rig.restart_worker.assert_not_called()
    replay.assert_o4()
    replay.stop()
    replay.physical_stop(b_uid, acknowledge=True)
    replay.result(b_uid, MARKER)
    assert replay.rig.pasted[-1][0] == MARKER
