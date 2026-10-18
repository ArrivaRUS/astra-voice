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

from astra_voice.core.dictation import RELEASE_TAIL_MS, DictationPhase
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

    def __init__(self) -> None:
        self.generation = 1
        self.captures: set[str] = set()
        self.sent: list[tuple[dict[str, Any], int]] = []

    def send(self, message: dict[str, Any]) -> None:
        self.sent.append((dict(message), self.generation))
        kind = message["type"]
        if kind == "record.start":
            self.captures.add(str(message["utterance_id"]))
        elif kind in {"record.stop", "record.cancel", "recognize"}:
            self.close(str(message["utterance_id"]))
        elif kind == "audio.close":
            self.captures.clear()

    def close(self, uid: str) -> None:
        self.captures.discard(uid)

    def restart(self) -> None:
        self.generation += 1
        self.captures.clear()


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
    delivered: set[str] = set()
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
            and str(m["utterance_id"]) not in delivered
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
    delivered.add(a_uid)
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
                delivered.add(uid)
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
                delivered.add(uid)
                worker.close(uid)
                event("cancelled", uid)
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
                delivered.add(uid)
                if kind == "record.cancel":
                    worker.close(uid)
                    event("cancelled", uid)
                else:
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
