"""Оркестрация с фейковыми портами и ручными таймерами, без звука и дисплея."""

from __future__ import annotations

import logging
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pytest

from astra_voice.core.dictation import (
    BUSY_RETRY_MS,
    CANCEL_TIMEOUT_MS,
    PROCESSING_WATCHDOG_MS,
    RECOGNIZE_TIMEOUT_S,
    DictationOrchestrator,
    DictationPhase,
    level_from_dbfs,
)
from astra_voice.platform.hotkey import HotkeyFsm, HotkeyMode, HotkeyState
from astra_voice.platform.paste import (
    PasteMethod,
    PasteMode,
    PasteOutcome,
    PasteOutcomeKind,
    PasteRestore,
)
from astra_voice.ui.pill import (
    ERROR_BUFFER_CLEARED,
    ERROR_MICROPHONE_UNAVAILABLE,
    ERROR_REASONS,
    ERROR_RECOGNITION_FAILED,
    ERROR_RECOGNITION_RESTARTED,
    STATE_DURATION_MS,
    PillState,
)
from astra_voice.ui.tray_icons import TrayState

pytestmark = pytest.mark.unit
MARKER = "ГЕЛИОТРОП-7"


class FakePill:
    """Записывает состояния, одновременно проверяя контракт подписей."""

    def __init__(self, trace: list[tuple[Any, ...]]) -> None:
        self.trace = trace
        self.calls: list[tuple[PillState, str | None, float | None]] = []
        self.hidden = False

    def show_state(
        self, state: PillState, *, text: str | None = None, level: float | None = None
    ) -> None:
        assert text is None or (state == PillState.ERROR and text in ERROR_REASONS)
        self.calls.append((state, text, level))
        self.trace.append(("pill", state, text, level))

    def hide(self) -> None:
        self.hidden = True
        self.trace.append(("hide",))


class FakeTray:
    """Хранит только состояние и доступность последнего текста."""

    def __init__(self, trace: list[tuple[Any, ...]]) -> None:
        self.trace = trace
        self.state = TrayState.IDLE
        self.has_last_text = False

    def set_state(self, state: TrayState, tooltip: str | None = None) -> None:
        self.state = state
        self.trace.append(("tray", state, tooltip))

    def set_has_last_text(self, value: bool) -> None:
        self.has_last_text = value
        self.trace.append(("has_last_text", value))


class FakeStats:
    """Принимает статистику без записи на диск."""

    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []

    def append(self, event_type: str, **fields: object) -> None:
        self.events.append({"type": event_type, **fields})


@dataclass
class Timer:
    """Ручка специально нехешируема; callback можно вызвать даже после отмены."""

    delay: int
    callback: Callable[[], None]
    cancelled: bool = False
    fired: bool = False

    def fire(self) -> None:
        assert not self.cancelled and not self.fired
        self.fired = True
        self.callback()


class Rig:
    """Все внешние эффекты наблюдаемы, время движется только явно."""

    def __init__(self) -> None:
        self.trace: list[tuple[Any, ...]] = []
        self.now = 10.0
        self.worker_generation = 1
        self.window: int | None = 42
        self.mode = PasteMode.AUTO
        self.params: dict[str, Any] = {
            "device": "fake-device",
            "limit_s": 120.0,
            "silence_db": -40.0,
            "insert": True,
        }
        self.sent: list[tuple[dict[str, Any], float | None]] = []
        self.pasted: list[tuple[str, int | None, PasteMode]] = []
        self.outcomes: deque[PasteOutcomeKind] = deque()
        self.during_paste: Callable[[], None] | None = None
        self.during_send: Callable[[dict[str, Any]], None] | None = None
        self.timers: list[Timer] = []
        self.done = 0
        self.recording = False
        self.notifications: list[tuple[str, str]] = []
        self.pill = FakePill(self.trace)
        self.tray = FakeTray(self.trace)
        self.stats = FakeStats()
        self.core = DictationOrchestrator(
            send=self.send,
            generation=lambda: self.worker_generation,
            pill=self.pill,
            tray=self.tray,
            paste=self.paste,
            active_window=self.active_window,
            schedule=self.schedule,
            cancel_timer=self.cancel_timer,
            hotkey_done=self.hotkey_done,
            set_recording=self.set_recording,
            notify=lambda summary, body: self.notifications.append((summary, body)),
            paste_mode=lambda: self.mode,
            record_params=lambda: self.params,
            clock=self.clock,
            stats=self.stats,
            log=logging.getLogger("test.dictation"),
        )
        self.fsm = HotkeyFsm(HotkeyMode.PTT, self.core.on_hotkey_state)

    def clock(self) -> float:
        self.trace.append(("clock", self.now))
        return self.now

    def active_window(self) -> int | None:
        self.trace.append(("active_window", self.window))
        return self.window

    def send(self, message: dict[str, Any], *, timeout: float | None = None) -> None:
        self.sent.append((message, timeout))
        self.trace.append(("send", message["type"]))
        if self.during_send is not None:
            self.during_send(message)

    def paste(self, text: str, target: int | None, mode: PasteMode) -> PasteOutcome:
        self.pasted.append((text, target, mode))
        self.trace.append(("paste",))
        if self.during_paste is not None:
            self.during_paste()
        self.now += 0.125
        kind = self.outcomes.popleft() if self.outcomes else PasteOutcomeKind.PASTED
        return PasteOutcome(kind, PasteMethod.NONE, PasteRestore.KEPT_OURS, None, 0, 0, 0.0)

    def schedule(self, delay: int, callback: Callable[[], None]) -> object:
        timer = Timer(delay, callback)
        self.timers.append(timer)
        return timer

    def cancel_timer(self, handle: object) -> None:
        assert isinstance(handle, Timer)
        handle.cancelled = True

    def hotkey_done(self) -> None:
        self.done += 1
        self.trace.append(("hotkey_done",))
        self.fsm.done(self.now)

    def set_recording(self, value: bool) -> None:
        self.recording = value
        self.trace.append(("recording", value))

    def timer(self, delay: int) -> Timer:
        matching = [t for t in self.timers if t.delay == delay and not t.cancelled and not t.fired]
        assert len(matching) == 1
        return matching[0]

    def start(self) -> None:
        self.core.on_hotkey_state(HotkeyState.RECORDING, "press")

    def stop(self) -> None:
        self.now += 2.0
        self.core.on_hotkey_state(HotkeyState.PROCESSING, "release")

    @property
    def uid(self) -> str:
        return str([m for m, _ in self.sent if m["type"] == "record.start"][-1]["utterance_id"])

    def event(self, kind: str, **fields: object) -> None:
        self.core.on_worker_event(
            {"type": kind, "generation": self.worker_generation, "utterance_id": self.uid, **fields}
        )

    def result(self, text: str = MARKER) -> None:
        self.now += 0.25
        self.event("result", text=text)

    def commands(self) -> list[str]:
        return [str(m["type"]) for m, _ in self.sent]


@pytest.fixture
def rig() -> Rig:
    return Rig()


def test_full_ptt_cycle_and_timings(rig: Rig) -> None:
    rig.fsm.press(rig.now)
    uid = rig.uid
    assert rig.sent == [({**rig.params, "type": "record.start", "utterance_id": uid}, 150.0)]
    assert rig.trace[:6] == [
        ("active_window", 42),
        ("send", "record.start"),
        ("pill", PillState.LISTENING, None, None),
        ("tray", TrayState.LISTENING, None),
        ("recording", True),
        ("clock", 10.0),
    ]
    assert "utterance_id" not in rig.params
    rig.event("level", peak_dbfs=-30.0)
    assert rig.pill.calls[-1:] == [(PillState.LISTENING, None, 0.5)]
    rig.event("silent")
    assert rig.pill.calls[-1:] == [(PillState.LISTENING_SILENT, None, None)]
    rig.event("audio.ready")
    rig.now += 2.0
    rig.fsm.release(rig.now)
    assert rig.core.phase.value == "processing"
    assert rig.sent[1:] == [
        ({"type": "record.stop", "utterance_id": uid}, None),
        ({"type": "recognize", "utterance_id": uid}, RECOGNIZE_TIMEOUT_S),
    ]
    assert rig.trace[-6:] == [
        ("clock", 12.0),
        ("send", "record.stop"),
        ("send", "recognize"),
        ("recording", False),
        ("pill", PillState.PROCESSING, None, None),
        ("tray", TrayState.PROCESSING, None),
    ]
    watchdog = rig.timer(PROCESSING_WATCHDOG_MS)
    rig.window = 99
    rig.result()
    assert rig.pasted == [(MARKER, 42, PasteMode.AUTO)]
    assert rig.pill.calls[-1] == (PillState.DONE, None, None)
    assert rig.tray.state == TrayState.DONE
    assert rig.core.last_text == MARKER and rig.tray.has_last_text
    assert rig.core.phase == DictationPhase.FINISHING
    assert rig.fsm.state == HotkeyState.IDLE and rig.done == 1
    assert not rig.recording and watchdog.cancelled
    assert rig.stats.events == [
        {
            "type": "dictation",
            "result": "ok",
            "t_ms": 250.0,
            "audio_ms": 2000.0,
            "paste_ms": 125.0,
            "cold": True,
        }
    ]
    rig.timer(STATE_DURATION_MS[PillState.DONE]).fire()
    assert rig.core.phase.value == "idle"
    assert rig.tray.state.value == "idle"


@pytest.mark.parametrize("first_result", ["ok", "empty", "cancelled"])
def test_first_dictation_only_is_cold_and_finishing_can_restart(
    rig: Rig, first_result: str
) -> None:
    rig.start()
    first_uid = rig.uid
    rig.stop()
    if first_result == "cancelled":
        rig.core.cancel("tray")
        rig.event("cancelled")
    else:
        rig.result("" if first_result == "empty" else MARKER)
    tail = rig.timers[-1]
    rig.start()
    assert rig.uid != first_uid and MARKER not in rig.uid
    assert tail.cancelled
    tail.callback()
    assert rig.core.phase == DictationPhase.RECORDING
    rig.stop()
    rig.result()
    assert [e["cold"] for e in rig.stats.events] == [True, False]


@pytest.mark.parametrize("phase", ["recording", "processing", "pasting", "busy"])
def test_single_slot_ignores_repeated_presses(rig: Rig, phase: str) -> None:
    rig.start()
    if phase != "recording":
        rig.stop()
    if phase == "pasting":
        rig.during_paste = rig.start
        rig.result()
    elif phase == "busy":
        rig.outcomes.append(PasteOutcomeKind.BUSY)
        rig.result()
        rig.start()
    else:
        rig.start()
    assert rig.commands().count("record.start") == 1


@pytest.mark.parametrize(
    ("kind", "state", "reason", "stat_result"),
    [
        (PasteOutcomeKind.PASTED, PillState.DONE, None, "ok"),
        (PasteOutcomeKind.CLIPBOARD_ONLY, PillState.CLIPBOARD_ONLY, None, "ok"),
        (PasteOutcomeKind.WINDOW_CHANGED, PillState.CLIPBOARD_ONLY, None, "ok"),
        (PasteOutcomeKind.REFUSED_SECRET, PillState.ERROR, ERROR_BUFFER_CLEARED, "ok"),
        (PasteOutcomeKind.FAILED, PillState.ERROR, ERROR_RECOGNITION_FAILED, None),
    ],
)
def test_paste_outcomes(
    rig: Rig, kind: PasteOutcomeKind, state: PillState, reason: str | None, stat_result: str | None
) -> None:
    rig.start()
    rig.stop()
    rig.outcomes.append(kind)
    rig.result()
    assert rig.pill.calls[-1] == (state, reason, None)
    assert rig.core.phase == DictationPhase.FINISHING and rig.done == 1
    assert rig.core.last_text == MARKER
    assert [e["result"] for e in rig.stats.events] == ([] if stat_result is None else [stat_result])


@pytest.mark.parametrize("second", [PasteOutcomeKind.BUSY, PasteOutcomeKind.PASTED])
def test_busy_has_one_retry_per_dictation(rig: Rig, second: PasteOutcomeKind) -> None:
    for _ in range(2):
        rig.start()
        rig.stop()
        rig.outcomes.extend((PasteOutcomeKind.BUSY, second))
        before = len(rig.stats.events)
        rig.result()
        assert rig.core.phase == DictationPhase.PASTING
        assert len(rig.stats.events) == before
        rig.now += 0.2
        rig.timer(BUSY_RETRY_MS).fire()
        expected = PillState.DONE if second == PasteOutcomeKind.PASTED else PillState.CLIPBOARD_ONLY
        assert rig.pill.calls[-1][0] == expected
        assert rig.stats.events[-1]["t_ms"] == pytest.approx(250.0)
        assert rig.stats.events[-1]["paste_ms"] == pytest.approx(250.0)
        assert not any(
            t.delay == BUSY_RETRY_MS and not t.cancelled and not t.fired for t in rig.timers
        )
    assert len(rig.pasted) == 4
    assert all(call == (MARKER, 42, PasteMode.AUTO) for call in rig.pasted)


@pytest.mark.parametrize("text", ["", " \n\t  "])
def test_empty_result(rig: Rig, text: str) -> None:
    rig.start()
    rig.stop()
    rig.result(text)
    assert not rig.pasted and rig.core.last_text is None
    assert not rig.tray.has_last_text
    assert rig.pill.calls[-1][0] == PillState.EMPTY
    assert rig.stats.events[-1]["result"] == "empty"
    assert rig.stats.events[-1]["cold"] is True


@pytest.mark.parametrize("phase", ["recording", "processing", "busy"])
def test_cancel_blocks_all_later_results_and_retries(rig: Rig, phase: str) -> None:
    rig.start()
    if phase != "recording":
        rig.stop()
    if phase == "busy":
        rig.outcomes.append(PasteOutcomeKind.BUSY)
        rig.result()
    old_timers = list(rig.timers)
    # При BUSY первый вызов уже состоялся, но публикации текста ещё не было.
    attempts = len(rig.pasted)
    rig.core.cancel("pill")
    assert rig.sent[-1][0] == {"type": "record.cancel", "utterance_id": rig.uid}
    assert not rig.recording and rig.tray.state == TrayState.IDLE
    assert all(t.cancelled for t in old_timers)
    rig.core.cancel("tray")
    assert rig.commands().count("record.cancel") == 1
    rig.result()
    for timer in old_timers:
        timer.callback()
    assert len(rig.pasted) == attempts
    assert rig.core.last_text is None
    assert rig.pill.calls[-1][0] != PillState.CANCELLED
    fallback = rig.timer(CANCEL_TIMEOUT_MS)
    rig.event("cancelled")
    assert fallback.cancelled
    assert rig.pill.calls[-1:] == [(PillState.CANCELLED, None, None)]
    assert rig.stats.events[-1]["result"] == "cancelled"
    assert rig.done == 1
    rig.result()
    fallback.callback()
    assert len(rig.pasted) == attempts and len(rig.stats.events) == 1


def test_cancel_idle_and_finishing_is_noop(rig: Rig) -> None:
    rig.core.cancel("tray")
    assert not rig.sent and not rig.timers and not rig.pill.calls
    rig.start()
    rig.stop()
    rig.result()
    before = list(rig.sent)
    rig.core.cancel("tray")
    assert rig.sent == before


def test_escape_and_indicator_loss_use_cancel(rig: Rig) -> None:
    rig.fsm.press(rig.now)
    rig.fsm.escape(rig.now)
    assert rig.commands()[-1] == "record.cancel"
    rig.event("cancelled")
    rig.start()
    rig.core.on_indicators_lost()
    assert rig.commands().count("record.cancel") == 2
    assert not rig.notifications


def test_cancel_timeout_and_synchronous_ack(rig: Rig) -> None:
    rig.start()
    rig.core.cancel("tray")
    rig.timer(CANCEL_TIMEOUT_MS).fire()
    assert rig.pill.calls[-1][0] == PillState.CANCELLED
    assert rig.core.phase == DictationPhase.FINISHING and rig.done == 1
    rig.start()

    def ack(message: dict[str, Any]) -> None:
        if message["type"] == "record.cancel":
            rig.event("cancelled")

    rig.during_send = ack
    rig.core.cancel("tray")
    assert rig.core.phase == DictationPhase.FINISHING and rig.done == 2
    assert all(t.cancelled or t.fired for t in rig.timers if t.delay == CANCEL_TIMEOUT_MS)


@pytest.mark.parametrize("phase", ["recording", "processing"])
def test_old_generation_result_is_discarded_without_interrupting_dictation(
    rig: Rig, phase: str, caplog: pytest.LogCaptureFixture
) -> None:
    rig.worker_generation = 2
    rig.start()
    if phase == "processing":
        rig.stop()
    before = list(rig.trace)
    pending = list(rig.timers)
    caplog.set_level(logging.DEBUG, logger="test.dictation")
    caplog.clear()

    rig.event("result", text=MARKER, generation=1)

    assert rig.core.phase.value == phase
    assert rig.trace == before
    assert rig.timers == pending and all(not t.cancelled for t in pending)
    assert not rig.pasted and rig.core.last_text is None
    assert all(state != PillState.ERROR for state, _, _ in rig.pill.calls)
    assert len(caplog.records) == 1 and caplog.records[0].levelno == logging.DEBUG
    assert not caplog.records[0].args and MARKER not in caplog.text

    if phase == "recording":
        rig.stop()
    rig.result("Текущая диктовка")
    assert rig.pasted == [("Текущая диктовка", 42, PasteMode.AUTO)]
    assert rig.pill.calls[-1] == (PillState.DONE, None, None)
    assert rig.core.phase == DictationPhase.FINISHING and rig.done == 1
    assert [e["result"] for e in rig.stats.events] == ["ok"]


@pytest.mark.parametrize("phase", ["recording", "processing", "busy", "cancelling_recording"])
def test_supervisor_generation_change_cancels_pending_work(rig: Rig, phase: str) -> None:
    rig.start()
    if phase in ("processing", "busy"):
        rig.stop()
    if phase == "busy":
        rig.outcomes.append(PasteOutcomeKind.BUSY)
        rig.result()
    elif phase == "cancelling_recording":
        rig.core.cancel("tray")
        assert rig.core.phase == DictationPhase.RECORDING
        rig.timer(CANCEL_TIMEOUT_MS)
    attempts = len(rig.pasted)
    pending = list(rig.timers)
    rig.worker_generation = 2
    # Событие ещё от прежнего воркера: перезапуск определяет супервизор.
    rig.event("audio.ready", generation=1, utterance_id="other")

    assert rig.core.phase == DictationPhase.FINISHING
    assert rig.pill.calls[-1] == (PillState.ERROR, ERROR_RECOGNITION_RESTARTED, None)
    assert rig.tray.state == TrayState.ERROR and rig.done == 1 and not rig.recording
    assert all(t.cancelled for t in pending)
    before = list(rig.trace)
    for timer in pending:
        timer.callback()
    rig.result()
    assert rig.trace == before
    assert len(rig.pasted) == attempts
    assert rig.core.last_text is None and not rig.stats.events


def test_newer_generation_result_fails_restarted(rig: Rig) -> None:
    rig.start()
    rig.stop()
    watchdog = rig.timer(PROCESSING_WATCHDOG_MS)
    rig.event("result", text=MARKER, generation=2)
    assert rig.worker_generation == 1
    assert not rig.pasted and rig.core.last_text is None
    assert rig.pill.calls[-1] == (PillState.ERROR, ERROR_RECOGNITION_RESTARTED, None)
    assert rig.core.phase == DictationPhase.FINISHING and rig.done == 1
    assert watchdog.cancelled
    watchdog.callback()
    assert rig.pill.calls[-1] == (PillState.ERROR, ERROR_RECOGNITION_RESTARTED, None)


def test_wrong_utterance_and_late_duplicate_are_ignored(rig: Rig) -> None:
    rig.start()
    rig.stop()
    rig.event("result", text=MARKER, utterance_id="other")
    assert not rig.pasted and rig.core.phase == DictationPhase.PROCESSING
    rig.result()
    old_uid = rig.uid
    rig.result()
    assert len(rig.pasted) == 1 and len(rig.stats.events) == 1
    rig.start()
    rig.stop()
    rig.event("result", text=MARKER, utterance_id=old_uid)
    assert len(rig.pasted) == 1 and rig.core.phase == DictationPhase.PROCESSING


@pytest.mark.parametrize("phase", ["recording", "processing", "busy"])
@pytest.mark.parametrize("code", ["worker-crashed", "restart-limit", "worker-start"])
def test_worker_crash_cancels_pending_work(rig: Rig, phase: str, code: str) -> None:
    rig.start()
    if phase != "recording":
        rig.stop()
    if phase == "busy":
        rig.outcomes.append(PasteOutcomeKind.BUSY)
        rig.result()
    attempts = len(rig.pasted)
    pending = list(rig.timers)
    rig.event("error", code=code)
    assert rig.pill.calls[-1] == (PillState.ERROR, ERROR_RECOGNITION_RESTARTED, None)
    assert rig.tray.state == TrayState.ERROR and rig.done == 1 and not rig.recording
    assert all(t.cancelled for t in pending)
    for timer in pending:
        timer.callback()
    rig.result()
    assert len(rig.pasted) == attempts and not rig.stats.events


def test_generation_change_before_busy_retry_prevents_paste(rig: Rig) -> None:
    rig.start()
    rig.stop()
    rig.outcomes.append(PasteOutcomeKind.BUSY)
    rig.result()
    rig.worker_generation += 1
    rig.timer(BUSY_RETRY_MS).fire()
    assert len(rig.pasted) == 1
    assert rig.pill.calls[-1] == (PillState.ERROR, ERROR_RECOGNITION_RESTARTED, None)


def test_processing_watchdog_releases_hotkey_without_empty_stat(rig: Rig) -> None:
    rig.fsm.press(rig.now)
    rig.now += 1
    rig.fsm.release(rig.now)
    rig.timer(PROCESSING_WATCHDOG_MS).fire()
    assert rig.pill.calls[-1] == (PillState.ERROR, ERROR_RECOGNITION_FAILED, None)
    assert rig.fsm.state == HotkeyState.IDLE and rig.done == 1
    assert not rig.stats.events
    rig.result()
    assert not rig.pasted


@pytest.mark.parametrize(
    ("code", "reason", "kind"),
    [
        ("audio-no-device", ERROR_MICROPHONE_UNAVAILABLE, "none"),
        ("audio-busy", ERROR_MICROPHONE_UNAVAILABLE, "busy"),
        ("audio-failed", ERROR_MICROPHONE_UNAVAILABLE, "other"),
        ("timeout", ERROR_RECOGNITION_FAILED, None),
        ("load-timeout", ERROR_RECOGNITION_FAILED, None),
        (MARKER, ERROR_RECOGNITION_FAILED, None),
    ],
)
def test_error_mapping(rig: Rig, code: str, reason: str, kind: str | None) -> None:
    rig.start()
    rig.event("error", code=code, message=MARKER)
    assert rig.pill.calls[-1] == (PillState.ERROR, reason, None)
    assert rig.stats.events == (
        [] if kind is None else [{"type": "mic_error", "kind": kind, "recovered_by": "none"}]
    )
    assert rig.done == 1 and not rig.recording


def test_limit_stops_once_and_late_levels_do_not_replace_processing(rig: Rig) -> None:
    rig.start()
    rig.now += 120
    rig.event("record.limit")
    assert [s for s, _, _ in rig.pill.calls][-2:] == [PillState.LIMIT, PillState.PROCESSING]
    assert rig.commands() == ["record.start", "record.stop", "recognize"]
    rig.core.on_hotkey_state(HotkeyState.PROCESSING, "limit")
    rig.event("record.limit")
    rig.event("level", peak_dbfs=-10)
    rig.event("silent")
    assert len(rig.sent) == 3 and rig.pill.calls[-1][0] == PillState.PROCESSING
    rig.result()
    assert rig.stats.events[-1]["audio_ms"] == 120000.0


@pytest.mark.parametrize("nested", ["result", "error", "cancel", "escape"])
def test_reentrant_delivery_is_deferred_and_cannot_overwrite_terminal_state(
    rig: Rig, nested: str
) -> None:
    rig.start()
    rig.stop()
    before = list(rig.pill.calls)

    def reenter() -> None:
        if nested == "cancel":
            rig.core.cancel("tray")
        elif nested == "escape":
            rig.core.on_hotkey_state(HotkeyState.IDLE, "escape-cancel")
        else:
            rig.event(nested, text=MARKER, code="worker-crashed")
        assert rig.pill.calls == before
        assert rig.commands() == ["record.start", "record.stop", "recognize"]
        assert rig.core.phase == DictationPhase.PASTING
        assert rig.done == 0

    rig.during_paste = reenter
    rig.result()
    assert len(rig.pasted) == 1
    if nested in ("cancel", "escape"):
        assert rig.commands()[-1] == "record.cancel"
        rig.timer(CANCEL_TIMEOUT_MS).fire()
        assert rig.pill.calls[-1][0] == PillState.CANCELLED
    elif nested == "error":
        assert rig.pill.calls[-1] == (PillState.ERROR, ERROR_RECOGNITION_RESTARTED, None)
        assert rig.core.last_text is None
    else:
        assert rig.pill.calls[-1][0] == PillState.DONE
        assert len(rig.stats.events) == 1
    assert rig.core.phase == DictationPhase.FINISHING and rig.done == 1


def test_reentrant_queue_is_fifo_and_copies_event(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    rig.start()
    rig.stop()

    def reenter() -> None:
        event = {"type": "audio.ready", "generation": 1, "utterance_id": rig.uid}
        rig.core.on_worker_event(event)
        event["type"] = "cancelled"
        rig.event("error", code="worker-crashed")
        assert "audio.ready" not in caplog.text

    rig.during_paste = reenter
    rig.result()
    assert "audio.ready" in caplog.text
    assert rig.pill.calls[-1] == (PillState.ERROR, ERROR_RECOGNITION_RESTARTED, None)
    assert not rig.stats.events


@pytest.mark.parametrize("phase", ["idle", "recording", "processing", "busy", "finishing"])
def test_shutdown_cancels_all_timers_and_ignores_future_delivery(rig: Rig, phase: str) -> None:
    if phase != "idle":
        rig.start()
    if phase in ("processing", "busy", "finishing"):
        rig.stop()
    if phase == "busy":
        rig.outcomes.append(PasteOutcomeKind.BUSY)
        rig.result()
    elif phase == "finishing":
        rig.result()
    pending = list(rig.timers)
    rig.core.shutdown()
    assert rig.pill.hidden and not rig.recording and rig.core.phase == DictationPhase.IDLE
    assert all(t.cancelled for t in pending)
    if phase in ("recording", "processing", "busy"):
        assert rig.commands()[-1] == "record.cancel"
    before = list(rig.trace)
    rig.core.shutdown()
    rig.start()
    rig.core.on_worker_event({"type": "result", "text": MARKER, "generation": 1})
    for timer in pending:
        timer.callback()
    assert rig.trace == before


def test_shutdown_during_paste_cannot_resurrect_ui(rig: Rig) -> None:
    rig.start()
    rig.stop()
    rig.during_paste = rig.core.shutdown
    rig.result()
    assert rig.pill.hidden and rig.core.phase == DictationPhase.IDLE
    assert not rig.stats.events and rig.core.last_text is None


def test_shutdown_continues_when_dependencies_raise(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    rig.start()
    rig.stop()

    def fail(*args: object, **kwargs: object) -> None:
        raise RuntimeError(MARKER)

    monkeypatch.setattr(rig.core, "_send", fail)
    monkeypatch.setattr(rig.core, "_cancel_timer", fail)
    monkeypatch.setattr(rig.core, "_set_recording", fail)
    monkeypatch.setattr(rig.core, "_hotkey_done", fail)
    rig.core.shutdown()
    assert rig.pill.hidden
    assert MARKER not in caplog.text


def test_paste_exception_cannot_leak_text_or_leave_input_suspended(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    rig.start()
    rig.stop()

    def fail() -> None:
        rig.start()
        raise RuntimeError(MARKER)

    rig.during_paste = fail
    rig.result()
    assert rig.pill.calls[-1] == (PillState.ERROR, ERROR_RECOGNITION_FAILED, None)
    assert MARKER not in caplog.text and rig.commands().count("record.start") == 1
    rig.during_paste = None
    rig.start()
    assert rig.commands().count("record.start") == 2


@pytest.mark.parametrize("kind", list(PasteOutcomeKind))
def test_text_never_reaches_logs_indicators_notifications_or_stats(
    rig: Rig, caplog: pytest.LogCaptureFixture, kind: PasteOutcomeKind
) -> None:
    caplog.set_level(logging.DEBUG)
    rig.outcomes.extend((kind, kind))
    rig.mode = PasteMode.CLIPBOARD_ONLY
    rig.window = None
    rig.start()
    rig.event("audio.ready", text=MARKER)
    rig.stop()
    rig.result()
    if kind == PasteOutcomeKind.BUSY:
        rig.timer(BUSY_RETRY_MS).fire()
    assert rig.pasted[0] == (MARKER, None, PasteMode.CLIPBOARD_ONLY)
    assert MARKER not in caplog.text
    assert MARKER not in repr(rig.trace)
    assert MARKER not in repr(rig.pill.calls)
    assert MARKER not in repr(rig.notifications)
    assert MARKER not in repr(rig.stats.events)
    assert MARKER not in repr(rig.sent)


@pytest.mark.parametrize(
    ("dbfs", "expected"),
    [
        (-100.0, 0.0),
        (-60.0, 0.0),
        (-30.0, 0.5),
        (0.0, 1.0),
        (10.0, 1.0),
        (float("-inf"), 0.0),
        (float("inf"), 1.0),
        (float("nan"), 0.0),
    ],
)
def test_level_scale(dbfs: float, expected: float) -> None:
    assert level_from_dbfs(dbfs) == expected
