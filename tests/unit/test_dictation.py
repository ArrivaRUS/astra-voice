"""Оркестрация с фейковыми портами и ручными таймерами, без звука и дисплея."""

from __future__ import annotations

import logging
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from unittest.mock import Mock, call

import pytest

from astra_voice.core.dictation import (
    BUSY_RETRY_MS,
    CANCEL_RESTART_MS,
    CANCEL_TIMEOUT_MS,
    LEVEL_LIMIT_MESSAGE,
    LEVEL_RECORD_LIMIT_S,
    LEVEL_TOTAL_LIMIT_S,
    PROCESSING_WATCHDOG_MS,
    RECOGNIZE_TIMEOUT_S,
    TEST_BUSY,
    TEST_RECORD_LIMIT_MS,
    DictationOrchestrator,
    DictationPhase,
    MicrophoneLevelUpdate,
    MicrophoneTestUpdate,
    level_from_dbfs,
)
from astra_voice.platform.hotkey import (
    GrabResult,
    HotkeyBackend,
    HotkeyEvent,
    HotkeyFsm,
    HotkeyManager,
    HotkeyMode,
    HotkeyState,
    MappingEvent,
)
from astra_voice.platform.paste import (
    PasteMethod,
    PasteMode,
    PasteOutcome,
    PasteOutcomeKind,
    PasteRestore,
    normalize,
)
from astra_voice.ui.pill import (
    CLIPBOARD_REASONS,
    CLIPBOARD_WINDOW_CHANGED,
    ERROR_BUFFER_CLEARED,
    ERROR_MICROPHONE_LOST,
    ERROR_MICROPHONE_UNAVAILABLE,
    ERROR_MODEL_NOT_LOADED,
    ERROR_REASONS,
    ERROR_RECOGNITION_FAILED,
    ERROR_RECOGNITION_RESTARTED,
    STATE_DURATION_MS,
    PillState,
)
from astra_voice.ui.tray_icons import TrayState
from astra_voice.worker import ipc

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
        assert (
            text is None
            or (state == PillState.ERROR and text in ERROR_REASONS)
            or (state == PillState.CLIPBOARD_ONLY and text in CLIPBOARD_REASONS)
        )
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

    def __init__(self, hotkey_mode: HotkeyMode = HotkeyMode.PTT) -> None:
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
        self.cancels = 0
        self.recording = False
        self.pill = FakePill(self.trace)
        self.tray = FakeTray(self.trace)
        self.stats = FakeStats()
        self.restart_worker = Mock()
        self.device_changed = Mock()
        self.device_lost = Mock()
        self.device_selected = Mock()
        self.device_resolved = Mock()
        self.backend = Mock(spec=HotkeyBackend)
        self.backend.grab_combo.return_value = GrabResult("ok", keycode=65)
        self.backend.grab_escape.return_value = GrabResult("ok", keycode=9)
        self.hotkey = HotkeyManager(self.backend, clock=lambda: self.now)
        assert self.hotkey.grab("Ctrl+Shift+Space", hotkey_mode).ok
        self.fsm: HotkeyFsm = self.hotkey.fsm
        self.core = DictationOrchestrator(
            send=self.send,
            generation=lambda: self.worker_generation,
            restart_worker=self.restart_worker,
            pill=self.pill,
            tray=self.tray,
            paste=self.paste,
            active_window=self.active_window,
            schedule=self.schedule,
            cancel_timer=self.cancel_timer,
            hotkey_done=self.hotkey_done,
            hotkey_cancel=self.hotkey_cancel,
            hotkey_idle=lambda: self.hotkey.fsm.state is HotkeyState.IDLE,
            set_recording=self.set_recording,
            paste_mode=lambda: self.mode,
            record_params=lambda: self.params,
            clock=self.clock,
            stats=self.stats,
            on_device_changed=self.device_changed,
            on_device_lost=self.device_lost,
            on_device_selected=self.device_selected,
            on_device_resolved=self.device_resolved,
            log=logging.getLogger("test.dictation"),
        )
        self.hotkey.on_state = self.core.on_hotkey_state

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

    def hotkey_cancel(self) -> None:
        self.cancels += 1
        self.fsm.escape(self.now)

    def set_recording(self, value: bool) -> None:
        self.recording = value
        self.trace.append(("recording", value))

    def timer(self, delay: int) -> Timer:
        matching = [t for t in self.timers if t.delay == delay and not t.cancelled and not t.fired]
        assert len(matching) == 1
        return matching[0]

    def start(self) -> None:
        self.fsm.press(self.now)

    def combo_key(self, *, pressed: bool) -> None:
        self.hotkey.handle_event(
            HotkeyEvent("KeyPress" if pressed else "KeyRelease", 65, int(self.now * 1000)),
            self.now,
        )

    def assert_hotkey_state(self, state: HotkeyState) -> None:
        assert self.fsm.state == state

    def stop(self) -> None:
        self.now += 2.0
        if self.fsm.mode == HotkeyMode.TOGGLE:
            self.fsm.press(self.now)
        else:
            self.fsm.release(self.now)

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
    assert rig.sent == [
        (
            {
                "type": "record.start",
                "utterance_id": uid,
                "device": "fake-device",
                "limit_s": 120.0,
            },
            150.0,
        )
    ]
    assert rig.trace[:6] == [
        ("active_window", 42),
        ("clock", 10.0),
        ("send", "record.start"),
        ("pill", PillState.LISTENING, None, None),
        ("tray", TrayState.LISTENING, None),
        ("recording", True),
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
            "open_ms": 0.0,
            "paste_ms": 125.0,
            "cold": True,
        }
    ]
    rig.timer(STATE_DURATION_MS[PillState.DONE]).fire()
    assert rig.core.phase.value == "idle"
    assert rig.tray.state.value == "idle"


def test_last_text_is_normalized_but_paste_receives_worker_text(rig: Rig) -> None:
    """T-58 (docs/test-plan.md), У59: меню получает текст без CR/LF и C0/C1."""
    text = f"{MARKER}\r\nстрока\rещё\nконец\x00\t\x1b\x1f\x7f\x85\x9f"
    rig.start()
    rig.stop()
    rig.result(text)
    assert rig.core.last_text == normalize(text)
    assert rig.core.last_text is not None
    assert all(ord(ch) >= 0x20 and not 0x7F <= ord(ch) <= 0x9F for ch in rig.core.last_text)
    assert rig.pasted == [(text, 42, PasteMode.AUTO)]


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
        (
            PasteOutcomeKind.WINDOW_CHANGED,
            PillState.CLIPBOARD_ONLY,
            CLIPBOARD_WINDOW_CHANGED,
            "ok",
        ),
        (PasteOutcomeKind.REFUSED_SECRET, PillState.ERROR, ERROR_BUFFER_CLEARED, "ok"),
        (PasteOutcomeKind.FAILED, PillState.ERROR, ERROR_RECOGNITION_FAILED, None),
    ],
)
def test_paste_outcomes(
    rig: Rig, kind: PasteOutcomeKind, state: PillState, reason: str | None, stat_result: str | None
) -> None:
    """T-58: смена окна уточняет подпись; оба исхода буфера сохраняют текст и result=ok."""
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
        assert rig.pill.calls[-1] == (expected, None, None)
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
    assert rig.fsm.state == HotkeyState.IDLE and rig.cancels == 1
    rig.backend.ungrab_escape.assert_called_once_with()
    assert all(t.cancelled for t in old_timers)
    rig.core.cancel("tray")
    assert rig.commands().count("record.cancel") == 1
    assert rig.cancels == 1
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


@pytest.mark.unit
@pytest.mark.parametrize("microphone_test", [False, True])
@pytest.mark.parametrize("phase", ["recording", "processing"])
def test_ib8_cancel_survives_persistent_ui_failures(
    rig: Rig,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    microphone_test: bool,
    phase: str,
) -> None:
    """ИБ-8: оба UI-порта всегда падают, отмена и её сторож всё равно работают."""
    if microphone_test:
        assert rig.core.start_test("", Mock())
        if phase == "processing":
            rig.core.stop_test()
    else:
        rig.start()
        if phase == "processing":
            rig.stop()
    old_timers = list(rig.timers)
    set_recording = Mock(side_effect=RuntimeError(MARKER))
    set_state = Mock(side_effect=RuntimeError(MARKER))
    monkeypatch.setattr(rig.core, "_set_recording", set_recording)
    monkeypatch.setattr(rig.tray, "set_state", set_state)
    at_send: list[tuple[int, int, list[Timer]]] = []

    def observe_send(message: dict[str, Any]) -> None:
        at_send.append(
            (
                set_recording.call_count,
                set_state.call_count,
                [timer for timer in rig.timers if not timer.cancelled and not timer.fired],
            )
        )

    rig.during_send = observe_send
    rig.core.cancel("tray")

    assert rig.sent[-1][0] == {"type": "record.cancel", "utterance_id": rig.uid}
    watchdog = rig.timer(CANCEL_TIMEOUT_MS)
    assert at_send == [(0, 0, [watchdog])]
    assert all(timer.cancelled for timer in old_timers)
    assert set_recording.call_count == set_state.call_count == 1
    assert len(caplog.records) == 2
    assert all(
        record.levelno == logging.WARNING and record.exc_info is not None
        for record in caplog.records
    )
    assert MARKER not in caplog.text


def test_cancel_idle_and_finishing_is_noop(rig: Rig) -> None:
    rig.core.cancel("tray")
    assert not rig.sent and not rig.timers and not rig.pill.calls
    rig.start()
    rig.stop()
    rig.result()
    before = list(rig.sent)
    rig.core.cancel("tray")
    assert rig.sent == before


@pytest.mark.parametrize("mode", list(HotkeyMode))
@pytest.mark.parametrize("source", ["pill", "tray", "indicators"])
def test_mouse_cancel_releases_escape_and_allows_next_dictation(
    mode: HotkeyMode, source: str
) -> None:
    rig = Rig(mode)
    rig.start()
    first_uid = rig.uid
    assert rig.fsm.state.value == "recording" and rig.recording
    rig.backend.grab_escape.assert_called_once_with()
    if source == "indicators":
        rig.core.on_indicators_lost()
    else:
        rig.core.cancel(source)
    assert rig.fsm.state == HotkeyState.IDLE and rig.cancels == 1
    rig.backend.ungrab_escape.assert_called_once_with()
    assert rig.commands() == ["record.start", "record.cancel"]
    rig.event("cancelled")
    assert rig.pill.calls[-1] == (PillState.CANCELLED, None, None)
    rig.fsm.release(rig.now + 1)
    rig.now += 2
    rig.start()
    assert rig.commands().count("record.start") == 2
    assert rig.uid != first_uid
    assert rig.fsm.state.value == "recording" and rig.recording
    assert rig.pill.calls[-1] == (PillState.LISTENING, None, None)
    assert rig.backend.grab_escape.call_count == 2


def test_escape_and_indicator_loss_use_cancel(rig: Rig) -> None:
    rig.fsm.press(rig.now)
    rig.fsm.escape(rig.now)
    assert rig.commands()[-1] == "record.cancel"
    rig.event("cancelled")
    rig.start()
    rig.core.on_indicators_lost()
    assert rig.commands().count("record.cancel") == 2
    assert rig.cancels == 2


def test_cancel_timeout_and_synchronous_ack(rig: Rig) -> None:
    rig.start()
    rig.core.cancel("tray")
    rig.timer(CANCEL_TIMEOUT_MS).fire()
    assert rig.pill.calls[-1][0] == PillState.CANCELLED
    assert rig.core.phase == DictationPhase.FINISHING and rig.done == 1
    rig.event("cancelled")
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


@pytest.mark.parametrize("send_result", ["ok", "error", "ack"])
def test_processing_watchdog_releases_hotkey_without_empty_stat(
    rig: Rig, send_result: str, caplog: pytest.LogCaptureFixture
) -> None:
    rig.fsm.press(rig.now)
    rig.now += 1
    rig.fsm.release(rig.now)

    def cancel_sent(message: dict[str, Any]) -> None:
        assert message == {"type": "record.cancel", "utterance_id": rig.uid}
        assert rig.pill.calls[-1][0] == PillState.PROCESSING
        if send_result == "error":
            raise RuntimeError(MARKER)
        if send_result == "ack":
            rig.event("cancelled")

    rig.during_send = cancel_sent
    rig.timer(PROCESSING_WATCHDOG_MS).fire()
    assert rig.commands() == ["record.start", "record.stop", "recognize", "record.cancel"]
    assert rig.pill.calls[-1] == (PillState.ERROR, ERROR_RECOGNITION_FAILED, None)
    assert rig.fsm.state == HotkeyState.IDLE and rig.done == 1
    rig.backend.ungrab_escape.assert_called_once_with()
    assert not rig.stats.events
    assert MARKER not in caplog.text
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
        ("no-model", ERROR_MODEL_NOT_LOADED, None),
        (MARKER, ERROR_RECOGNITION_FAILED, None),
    ],
)
def test_error_mapping(rig: Rig, code: str, reason: str, kind: str | None) -> None:
    rig.params["device"] = ""
    rig.start()
    rig.event("error", code=code, message=MARKER)
    assert rig.pill.calls[-1] == (PillState.ERROR, reason, None)
    assert rig.tray.state == TrayState.ERROR
    assert rig.stats.events == (
        [] if kind is None else [{"type": "mic_error", "kind": kind, "recovered_by": "none"}]
    )
    assert rig.done == 1 and not rig.recording


@pytest.mark.parametrize("mode", list(HotkeyMode))
@pytest.mark.parametrize("elapsed", [0.3, 1.5, 4.2, 7.9])
def test_late_first_audio_ready_keeps_recording_and_success_releases_hotkey(
    mode: HotkeyMode, elapsed: float
) -> None:
    rig = Rig(mode)
    rig.now = 0.0
    rig.combo_key(pressed=True)
    uid = rig.uid
    rig.now = elapsed

    def notified(name: str) -> None:
        assert name == "Встроенный микрофон"
        assert rig.commands() == ["record.start"]
        assert rig.recording and rig.core.phase == DictationPhase.RECORDING

    rig.device_selected.side_effect = notified
    rig.event("audio.ready", changed="Источник звука изменился: Встроенный микрофон")
    assert rig.core.phase.value == "recording" and rig.recording
    rig.assert_hotkey_state(HotkeyState.RECORDING)
    rig.backend.ungrab_escape.assert_not_called()
    rig.device_selected.assert_called_once_with("Встроенный микрофон")
    rig.device_changed.assert_not_called()
    rig.device_lost.assert_not_called()
    assert not rig.stats.events and rig.done == 0
    assert rig.pill.calls[-1:] == [(PillState.LISTENING, None, None)]
    rig.event("level", peak_dbfs=-20)
    assert rig.pill.calls[-1:] == [(PillState.LISTENING, None, 2 / 3)]
    rig.stop()
    assert rig.core.phase == DictationPhase.PROCESSING and not rig.recording
    assert rig.sent[1:] == [
        ({"type": "record.stop", "utterance_id": uid}, None),
        ({"type": "recognize", "utterance_id": uid}, RECOGNIZE_TIMEOUT_S),
    ]
    assert rig.pill.calls[-1:] == [(PillState.PROCESSING, None, None)]
    rig.result()
    assert rig.pasted == [(MARKER, 42, PasteMode.AUTO)]
    assert rig.stats.events == [
        {
            "type": "dictation",
            "result": "ok",
            "audio_ms": pytest.approx((elapsed + 2) * 1000),
            "open_ms": pytest.approx(elapsed * 1000),
            "t_ms": 250.0,
            "paste_ms": 125.0,
            "cold": True,
        }
    ]
    assert rig.pill.calls[-1] == (PillState.DONE, None, None)
    assert all(state != PillState.ERROR for state, _, _ in rig.pill.calls)
    assert rig.tray.state == TrayState.DONE
    rig.assert_hotkey_state(HotkeyState.IDLE)
    assert rig.done == 1
    rig.backend.ungrab_escape.assert_called_once_with()
    rig.timer(STATE_DURATION_MS[PillState.DONE]).fire()
    rig.combo_key(pressed=False)
    rig.now += 1
    rig.combo_key(pressed=True)
    assert rig.uid != uid
    rig.assert_hotkey_state(HotkeyState.RECORDING)
    assert rig.pill.calls[-1] == (PillState.LISTENING, None, None)


@pytest.mark.parametrize("elapsed", [0.0, 0.299, 1.5])
@pytest.mark.parametrize(
    "fields",
    [
        {"device": "USB-гарнитура", "changed": "Источник звука изменился: другое имя"},
        {"changed": "Источник звука изменился: USB-гарнитура"},
        {"device": "", "changed": "Источник звука изменился: USB-гарнитура"},
        {"device": "  USB-гарнитура  ", "changed": "смена"},
        {"device": "   ", "changed": "Источник звука изменился: USB-гарнитура  "},
        {"device": None, "changed": "Источник звука изменился: USB-гарнитура"},
        {"device": 42, "changed": "Источник звука изменился: USB-гарнитура"},
        {"changed": "Источник звука изменился: старое: USB-гарнитура"},
    ],
)
def test_microphone_selected_at_start_once(
    rig: Rig, elapsed: float, fields: dict[str, object]
) -> None:
    rig.start()
    rig.now += elapsed
    rig.event("audio.ready", **fields)
    rig.device_selected.assert_called_once_with("USB-гарнитура")
    rig.device_changed.assert_not_called()
    assert rig.commands() == ["record.start"]
    assert rig.recording and rig.core.phase == DictationPhase.RECORDING
    assert not rig.stats.events
    rig.stop()
    rig.result()
    rig.start()
    # При повторном открытии того же устройства воркер не присылает changed.
    rig.now += elapsed
    rig.event("audio.ready", device="USB-гарнитура")
    rig.device_selected.assert_called_once_with("USB-гарнитура")
    rig.device_changed.assert_not_called()
    assert rig.recording and rig.core.phase == DictationPhase.RECORDING
    rig.stop()
    rig.result()
    rig.start()
    rig.event("audio.ready", changed="Источник звука изменился: Встроенный микрофон")
    rig.device_selected.assert_called_once_with("USB-гарнитура")
    rig.device_changed.assert_called_once_with("Встроенный микрофон")
    assert rig.recording and rig.core.phase == DictationPhase.RECORDING
    assert rig.commands() == ["record.start", "record.stop", "recognize"] * 2 + ["record.start"]
    assert all(event["type"] == "dictation" for event in rig.stats.events)


@pytest.mark.parametrize("elapsed", [0.1, 2.0])
def test_default_microphone_without_changed_is_selected_once(
    rig: Rig, elapsed: float, caplog: pytest.LogCaptureFixture
) -> None:
    rig.params["device"] = None
    rig.start()
    rig.now += elapsed
    with caplog.at_level(logging.DEBUG, logger="test.dictation"):
        rig.event("audio.ready", device="USB-гарнитура")
    assert "диктовка: audio.ready" in caplog.text
    assert rig.commands() == ["record.start"]
    assert rig.recording
    assert not rig.stats.events
    rig.device_changed.assert_not_called()
    rig.device_selected.assert_called_once_with("USB-гарнитура")
    rig.stop()
    rig.result()
    assert rig.stats.events[-1]["open_ms"] == pytest.approx(elapsed * 1000)
    rig.start()
    rig.event("audio.ready", device="USB-гарнитура")
    rig.device_selected.assert_called_once_with("USB-гарнитура")
    rig.device_changed.assert_not_called()
    rig.stop()
    rig.result()
    rig.start()
    rig.event("audio.ready", device="Встроенный микрофон", changed="смена")
    rig.device_selected.assert_called_once_with("USB-гарнитура")
    rig.device_changed.assert_called_once_with("Встроенный микрофон")
    assert rig.recording


@pytest.mark.parametrize("hello_before_start", [False, True])
def test_new_generation_announces_same_microphone_as_selected(
    rig: Rig, hello_before_start: bool
) -> None:
    for generation in (1, 2, 3):
        rig.worker_generation = generation
        if hello_before_start:
            rig.core.on_worker_event({"type": "hello", "generation": generation})
        rig.start()
        rig.event("audio.ready", device="USB-гарнитура", changed="смена")
        rig.stop()
        rig.result()
    assert rig.device_selected.call_args_list == [call("USB-гарнитура")] * 3
    rig.device_changed.assert_not_called()


def test_explicit_device_selection_resets_announcement(rig: Rig) -> None:
    rig.start()
    rig.event("audio.ready", device="Встроенный микрофон")
    rig.stop()
    rig.result()
    rig.params["device"] = "alsa_input.usb-headset"
    rig.core.reset_device_announcement()
    rig.start()
    rig.event("audio.ready", device="USB-гарнитура", changed="смена")
    assert rig.device_selected.call_args_list == [
        call("Встроенный микрофон"),
        call("USB-гарнитура"),
    ]
    rig.device_changed.assert_not_called()


def test_device_change_is_announced_once_per_name_in_generation(rig: Rig) -> None:
    for name, changed in (
        ("Встроенный микрофон", {}),
        ("USB-гарнитура", {"changed": "смена"}),
        ("USB-гарнитура", {"changed": "смена"}),
        ("USB-гарнитура", {}),
        ("Встроенный микрофон", {"changed": "смена"}),
    ):
        rig.start()
        rig.event("audio.ready", device=name, **changed)
        rig.stop()
        rig.result()
    rig.device_selected.assert_called_once_with("Встроенный микрофон")
    assert rig.device_changed.call_args_list == [call("USB-гарнитура"), call("Встроенный микрофон")]


@pytest.mark.parametrize("elapsed", [0.1, 2.0])
@pytest.mark.parametrize("changed", ["", "Источник звука изменился: ", "без разделителя", None, 42])
def test_unknown_device_never_notifies(rig: Rig, elapsed: float, changed: object) -> None:
    for _ in range(2):
        rig.start()
        rig.now += elapsed
        rig.event("audio.ready", changed=changed)
        rig.device_changed.assert_not_called()
        rig.device_selected.assert_not_called()
        assert rig.recording and rig.core.phase == DictationPhase.RECORDING
        assert rig.commands()[-1] == "record.start"
        rig.stop()
        rig.result()
        assert rig.stats.events[-1]["open_ms"] == pytest.approx(elapsed * 1000)
    rig.device_changed.assert_not_called()
    rig.device_selected.assert_not_called()
    assert rig.commands() == ["record.start", "record.stop", "recognize"] * 2
    assert all(event["type"] == "dictation" for event in rig.stats.events)
    assert all(state != PillState.ERROR for state, _, _ in rig.pill.calls)


@pytest.mark.parametrize(
    "outcome",
    [PasteOutcomeKind.PASTED, PasteOutcomeKind.CLIPBOARD_ONLY, PasteOutcomeKind.WINDOW_CHANGED],
)
@pytest.mark.parametrize("text", [MARKER, ""])
@pytest.mark.parametrize("first_open", [True, False])
def test_microphone_notification_preserves_result(
    rig: Rig, outcome: PasteOutcomeKind, text: str, first_open: bool
) -> None:
    if not first_open:
        rig.start()
        rig.event("audio.ready", device="USB-гарнитура", changed="смена")
        rig.stop()
        rig.result("")
    rig.start()
    rig.now += 1
    rig.event("audio.ready", device="Встроенный микрофон", changed="смена")
    if first_open:
        rig.device_selected.assert_called_once_with("Встроенный микрофон")
        rig.device_changed.assert_not_called()
    else:
        rig.device_selected.assert_called_once_with("USB-гарнитура")
        rig.device_changed.assert_called_once_with("Встроенный микрофон")
    assert rig.core.phase == DictationPhase.RECORDING and rig.recording
    rig.stop()
    assert rig.pill.calls[-1] == (PillState.PROCESSING, None, None)
    rig.outcomes.append(outcome)
    rig.result(text)
    state = (
        PillState.EMPTY
        if not text
        else PillState.DONE
        if outcome == PasteOutcomeKind.PASTED
        else PillState.CLIPBOARD_ONLY
    )
    reason = (
        CLIPBOARD_WINDOW_CHANGED if text and outcome == PasteOutcomeKind.WINDOW_CHANGED else None
    )
    assert rig.pill.calls[-1] == (state, reason, None)
    assert all(shown != PillState.ERROR for shown, _, _ in rig.pill.calls)
    assert rig.tray.state == (TrayState.DONE if state == PillState.DONE else TrayState.IDLE)
    assert rig.pasted == ([(text, 42, PasteMode.AUTO)] if text else [])
    assert rig.stats.events[-1]["result"] == ("ok" if text else "empty")
    assert rig.stats.events[-1]["open_ms"] == 1000.0
    assert all(event["type"] == "dictation" for event in rig.stats.events)
    rig.assert_hotkey_state(HotkeyState.IDLE)
    rig.timer(STATE_DURATION_MS[state]).fire()
    assert rig.core.phase.value == "idle"


@pytest.mark.parametrize("command", ["record.stop", "recognize"])
def test_manual_stop_failure_after_audio_ready_releases_hotkey(rig: Rig, command: str) -> None:
    def fail(message: dict[str, Any]) -> None:
        if message["type"] == command:
            raise RuntimeError("send failed")

    rig.start()
    rig.during_send = fail
    rig.now += 1
    rig.event("audio.ready", device="Встроенный микрофон", changed="смена")
    assert rig.recording and rig.commands() == ["record.start"]
    assert rig.pill.calls[-1:] == [(PillState.LISTENING, None, None)]
    rig.stop()
    assert rig.pill.calls[-1] == (PillState.ERROR, ERROR_RECOGNITION_FAILED, None)
    rig.assert_hotkey_state(HotkeyState.IDLE)
    assert not rig.recording


def test_changed_notification_deduplicates_announced_names(rig: Rig) -> None:
    # Разные системные имена могут иметь одинаковое описание в audio.ready.
    names = [
        "USB-гарнитура",
        "USB-гарнитура",
        "USB-гарнитура",
        "Встроенный микрофон",
        "USB-гарнитура",
    ]
    changes = ["Встроенный микрофон", "USB-гарнитура"]

    def notified(name: str) -> None:
        assert rig.commands()[-1] == "record.start"
        assert rig.recording and rig.core.phase == DictationPhase.RECORDING

    rig.device_changed.side_effect = notified
    for index, name in enumerate(names):
        rig.start()
        rig.event("audio.ready", device=name, changed=f"Источник звука изменился: {name}")
        rig.device_selected.assert_called_once_with(names[0])
        assert rig.device_changed.call_args_list == [
            call(value) for value in changes[: max(0, index - 2)]
        ]
        assert rig.recording and rig.core.phase == DictationPhase.RECORDING
        rig.stop()
        rig.result()
    rig.device_selected.assert_called_once_with("USB-гарнитура")
    assert rig.device_changed.call_args_list == [call(name) for name in changes]
    assert rig.commands() == ["record.start", "record.stop", "recognize"] * len(names)
    assert all(event["type"] == "dictation" for event in rig.stats.events)
    assert all(state != PillState.ERROR for state, _, _ in rig.pill.calls)


def test_audio_ready_without_notification_callbacks_keeps_recording(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rig.core, "_on_device_selected", None)
    monkeypatch.setattr(rig.core, "_on_device_changed", None)
    for name in ("USB-гарнитура", "Встроенный микрофон"):
        rig.start()
        rig.now += 1.5
        rig.event("audio.ready", device=name, changed=f"Источник звука изменился: {name}")
        assert rig.recording and rig.core.phase == DictationPhase.RECORDING
        assert rig.commands()[-1] == "record.start"
        rig.stop()
        rig.result()
        assert rig.pill.calls[-1] == (PillState.DONE, None, None)
    rig.device_selected.assert_not_called()
    rig.device_changed.assert_not_called()


@pytest.mark.parametrize("elapsed", [None, 0.0, 0.75])
def test_open_ms_resets_for_each_recording(rig: Rig, elapsed: float | None) -> None:
    rig.start()
    rig.now += 1.5
    rig.event("audio.ready", device="USB-гарнитура", changed="смена")
    rig.stop()
    rig.result()
    assert rig.stats.events[-1]["open_ms"] == 1500.0
    assert rig.stats.events[-1]["audio_ms"] == 3500.0

    rig.start()
    if elapsed is not None:
        rig.now += elapsed
        rig.event("audio.ready", device="USB-гарнитура")
    rig.stop()
    rig.result()
    assert rig.stats.events[-1]["open_ms"] == (None if elapsed is None else elapsed * 1000)
    assert rig.stats.events[-1]["audio_ms"] == (2 + (elapsed or 0)) * 1000
    rig.device_selected.assert_called_once_with("USB-гарнитура")
    rig.device_changed.assert_not_called()


def test_duplicate_audio_ready_keeps_first_open_ms(rig: Rig) -> None:
    rig.start()
    rig.now += 1.5
    rig.event("audio.ready")
    rig.now += 1.0
    # Защита от повторной доставки события: это не переоткрытие потока.
    rig.event("audio.ready")
    assert rig.recording and rig.commands() == ["record.start"]
    rig.stop()
    rig.result()
    assert rig.stats.events[-1]["open_ms"] == 1500.0
    assert rig.stats.events[-1]["audio_ms"] == 4500.0
    rig.device_selected.assert_not_called()
    rig.device_changed.assert_not_called()


@pytest.mark.parametrize("microphone_test", [False, True])
@pytest.mark.parametrize(
    "fields",
    [
        {"device": " USB-гарнитура ", "changed": "Источник звука изменился: другое имя"},
        {"changed": "Источник звука изменился: USB-гарнитура"},
    ],
)
def test_audio_ready_resolves_device_and_reset_clears_it(
    rig: Rig, microphone_test: bool, fields: dict[str, str]
) -> None:
    assert rig.core.resolved_device == ""
    rig.core.reset_device_announcement()
    rig.device_resolved.assert_not_called()
    if microphone_test:
        assert rig.core.start_test("", Mock())
    else:
        rig.start()
    for _ in range(2):
        rig.event("audio.ready", **fields)
    assert rig.core.resolved_device == "USB-гарнитура"
    rig.device_resolved.assert_called_once_with("USB-гарнитура")
    rig.event("audio.ready")
    assert rig.core.resolved_device == "USB-гарнитура"
    if microphone_test:
        rig.device_selected.assert_not_called()
    else:
        rig.device_selected.assert_called_once_with("USB-гарнитура")
    rig.device_changed.assert_not_called()
    rig.core.reset_device_announcement()
    rig.core.reset_device_announcement()
    assert rig.core.resolved_device == ""
    assert rig.device_resolved.call_args_list == [call("USB-гарнитура"), call("")]


def test_audio_ready_after_stop_records_open_ms_without_notification(rig: Rig) -> None:
    rig.start()
    rig.stop()
    before = list(rig.pill.calls)
    rig.now += 0.5
    rig.event("audio.ready", device="USB-гарнитура", changed="смена")
    assert rig.core.phase == DictationPhase.PROCESSING
    assert rig.core.resolved_device == "USB-гарнитура"
    rig.device_resolved.assert_called_once_with("USB-гарнитура")
    assert rig.pill.calls == before
    assert rig.commands() == ["record.start", "record.stop", "recognize"]
    rig.device_selected.assert_not_called()
    rig.device_changed.assert_not_called()
    rig.result()
    assert rig.stats.events[-1]["open_ms"] == 2500.0
    assert rig.stats.events[-1]["audio_ms"] == 2000.0
    assert rig.pill.calls[-1] == (PillState.DONE, None, None)


@pytest.mark.parametrize("fields", [{"generation": 1}, {"utterance_id": "other"}])
def test_foreign_audio_ready_does_not_consume_first_open(
    rig: Rig, fields: dict[str, object]
) -> None:
    rig.worker_generation = 2
    rig.start()
    rig.now += 0.5
    rig.event("audio.ready", device="Встроенный микрофон", changed="смена", **fields)
    rig.device_selected.assert_not_called()
    rig.device_changed.assert_not_called()
    rig.now += 1.0
    rig.event("audio.ready", device="USB-гарнитура", changed="смена")
    rig.device_selected.assert_called_once_with("USB-гарнитура")
    rig.device_changed.assert_not_called()
    assert rig.recording and rig.commands() == ["record.start"]
    rig.stop()
    rig.result()
    assert rig.stats.events[-1]["open_ms"] == 1500.0


@pytest.mark.parametrize("device", ["alsa_input.usb-headset", "", None])
def test_missing_device_uses_selection_at_record_start(rig: Rig, device: str | None) -> None:
    rig.params["device"] = device
    rig.start()
    rig.params["device"] = None if device else "alsa_input.usb-headset"
    rig.event("error", code="audio-no-device")
    assert rig.stats.events == [
        {"type": "mic_error", "kind": "device-lost" if device else "none", "recovered_by": "none"}
    ]
    reason = ERROR_MICROPHONE_LOST if device else ERROR_MICROPHONE_UNAVAILABLE
    assert rig.pill.calls[-1] == (PillState.ERROR, reason, None)
    assert rig.device_lost.call_count == int(bool(device))
    assert not rig.recording and rig.done == 1
    rig.assert_hotkey_state(HotkeyState.IDLE)
    rig.backend.ungrab_escape.assert_called_once_with()


@pytest.mark.parametrize("elapsed", [0.0, 1.5])
def test_synchronous_audio_ready_records_open_ms(rig: Rig, elapsed: float) -> None:
    def opened(message: dict[str, Any]) -> None:
        if message["type"] == "record.start":
            rig.now += elapsed
            rig.event("audio.ready", changed="Источник звука изменился: USB-гарнитура")

    rig.during_send = opened
    rig.start()
    rig.device_selected.assert_called_once_with("USB-гарнитура")
    rig.device_changed.assert_not_called()
    assert rig.recording and rig.commands() == ["record.start"]
    rig.stop()
    rig.result()
    assert rig.stats.events[-1]["open_ms"] == elapsed * 1000
    assert rig.stats.events[-1]["audio_ms"] == (elapsed + 2) * 1000
    assert rig.pill.calls[-1] == (PillState.DONE, None, None)


def test_limit_stops_once_and_late_levels_do_not_replace_processing(rig: Rig) -> None:
    rig.start()
    rig.now += 120
    rig.event("record.limit")
    assert [s for s, _, _ in rig.pill.calls][-2:] == [PillState.LIMIT, PillState.PROCESSING]
    assert rig.commands() == ["record.start", "recognize"]
    rig.fsm.tick(rig.now)
    rig.event("record.limit")
    rig.event("level", peak_dbfs=-10)
    rig.event("silent")
    assert len(rig.sent) == 2 and rig.pill.calls[-1][0] == PillState.PROCESSING
    rig.result()
    assert rig.stats.events[-1]["audio_ms"] == 120000.0


@pytest.mark.parametrize("mode", list(HotkeyMode))
@pytest.mark.parametrize(
    "terminal",
    [
        "audio-no-device",
        "audio-busy",
        "audio-failed",
        "worker-crashed",
        "restart-limit",
        "worker-start",
        "timeout",
        "cancelled",
        "generation",
        "newer-generation",
        "record.limit",
    ],
)
def test_recording_terminal_releases_escape_and_next_combo_starts(
    mode: HotkeyMode, terminal: str
) -> None:
    """T-56: настоящий FSM должен освободить Escape и принять следующую комбинацию."""
    rig = Rig(mode)
    rig.combo_key(pressed=True)
    first_uid = rig.uid
    rig.assert_hotkey_state(HotkeyState.RECORDING)
    rig.backend.grab_escape.assert_called_once_with()

    if terminal == "generation":
        rig.worker_generation += 1
        rig.event("audio.ready", generation=1)
    elif terminal == "newer-generation":
        rig.event("audio.ready", generation=2)
    elif terminal == "record.limit":
        rig.now += 120
        rig.event("record.limit")
        # Воркер опередил tick клавиш: FSM всё ещё RECORDING до результата.
        rig.assert_hotkey_state(HotkeyState.RECORDING)
        rig.result()
        assert rig.pasted == [(MARKER, 42, PasteMode.AUTO)]
        assert rig.commands() == ["record.start", "recognize"]
    elif terminal == "cancelled":
        rig.event("cancelled")
    else:
        rig.event("error", code=terminal)

    assert rig.core.phase.value == "finishing"
    rig.assert_hotkey_state(HotkeyState.IDLE)
    assert not rig.recording
    rig.backend.ungrab_escape.assert_called_once_with()
    assert "record.cancel" not in rig.commands()
    rig.combo_key(pressed=False)
    rig.now += 1
    rig.combo_key(pressed=True)
    assert rig.commands().count("record.start") == 2
    assert rig.uid != first_uid
    assert rig.core.phase == DictationPhase.RECORDING
    rig.assert_hotkey_state(HotkeyState.RECORDING)
    assert rig.recording
    assert rig.backend.grab_escape.call_count == 2


@pytest.mark.parametrize("phase", ["recording", "processing", "pasting"])
def test_press_while_cancel_ack_pending_resets_hotkey(rig: Rig, phase: str) -> None:
    rig.start()
    if phase != "recording":
        rig.stop()
    if phase == "pasting":
        rig.outcomes.append(PasteOutcomeKind.BUSY)
        rig.result()
    assert rig.core.phase.value == phase
    rig.core.cancel("tray")
    rig.assert_hotkey_state(HotkeyState.IDLE)
    rig.backend.ungrab_escape.reset_mock()

    # Оркестратор ещё занят до cancelled, но FSM уже принимает новый press.
    rig.start()
    rig.assert_hotkey_state(HotkeyState.IDLE)
    rig.backend.ungrab_escape.assert_called_once_with()
    assert rig.commands().count("record.start") == 1
    assert rig.commands().count("record.cancel") == 1
    rig.event("cancelled")
    rig.start()
    assert rig.commands().count("record.start") == 2
    rig.assert_hotkey_state(HotkeyState.RECORDING)
    assert rig.recording


def test_short_ptt_tap_switches_to_toggle_without_cancelling(rig: Rig) -> None:
    rig.combo_key(pressed=True)
    rig.now += 0.1
    rig.combo_key(pressed=False)
    assert rig.fsm.mode == HotkeyMode.TOGGLE
    rig.assert_hotkey_state(HotkeyState.RECORDING)
    assert rig.core.phase == DictationPhase.RECORDING and rig.recording
    assert rig.commands() == ["record.start"]
    assert rig.cancels == 0
    rig.backend.ungrab_escape.assert_not_called()

    rig.now += 1
    rig.combo_key(pressed=True)
    rig.assert_hotkey_state(HotkeyState.PROCESSING)
    assert rig.commands() == ["record.start", "record.stop", "recognize"]
    rig.result()
    assert rig.pasted == [(MARKER, 42, PasteMode.AUTO)]
    rig.assert_hotkey_state(HotkeyState.IDLE)
    assert rig.cancels == 0


@pytest.mark.parametrize("mode", list(HotkeyMode))
def test_mapping_regrab_during_recording_does_not_cancel(mode: HotkeyMode) -> None:
    rig = Rig(mode)
    rig.start()
    rig.backend.poll_events.return_value = [
        MappingEvent(
            {"Ctrl+Shift+Space": GrabResult("ok", keycode=65)}, GrabResult("ok", keycode=9)
        )
    ]
    rig.hotkey.process_pending()
    rig.assert_hotkey_state(HotkeyState.RECORDING)
    assert rig.core.phase == DictationPhase.RECORDING and rig.recording
    assert rig.commands() == ["record.start"]
    assert rig.cancels == 0
    rig.backend.ungrab_escape.assert_not_called()
    rig.stop()
    rig.result()
    assert rig.pasted == [(MARKER, 42, PasteMode.AUTO)]


@pytest.mark.parametrize("terminal", ["error", "cancelled", "generation"])
def test_queued_press_after_terminal_transition_is_discarded(
    rig: Rig, terminal: str, caplog: pytest.LogCaptureFixture
) -> None:
    rig.start()
    rig.stop()
    caplog.set_level(logging.DEBUG, logger="test.dictation")

    def reenter() -> None:
        if terminal == "generation":
            rig.event("audio.ready", generation=2)
        else:
            rig.event(terminal, code="worker-crashed", message=MARKER)
        rig.fsm.escape(rig.now)
        rig.start()
        rig.assert_hotkey_state(HotkeyState.RECORDING)
        assert rig.core.phase == DictationPhase.PASTING

    rig.during_paste = reenter
    rig.result()
    assert rig.core.phase.value == "finishing"
    rig.assert_hotkey_state(HotkeyState.IDLE)
    assert rig.backend.ungrab_escape.call_count == 2
    assert rig.commands().count("record.start") == 1
    dropped = [r for r in caplog.records if "отложенные события" in r.message]
    assert len(dropped) == 1 and dropped[0].levelno == logging.DEBUG
    assert not dropped[0].args and MARKER not in caplog.text

    rig.during_paste = None
    rig.start()
    assert rig.commands().count("record.start") == 2
    rig.assert_hotkey_state(HotkeyState.RECORDING)
    assert rig.recording


@pytest.mark.parametrize("kind", [PasteOutcomeKind.PASTED, PasteOutcomeKind.BUSY])
def test_escape_and_press_during_paste_release_regrabbed_escape(
    rig: Rig, kind: PasteOutcomeKind
) -> None:
    rig.start()
    rig.stop()
    rig.outcomes.append(kind)

    def reenter() -> None:
        rig.fsm.escape(rig.now)
        rig.start()
        rig.assert_hotkey_state(HotkeyState.RECORDING)

    rig.during_paste = reenter
    rig.result()
    rig.assert_hotkey_state(HotkeyState.IDLE)
    assert rig.backend.ungrab_escape.call_count == 2
    assert rig.commands().count("record.start") == 1
    if kind == PasteOutcomeKind.BUSY:
        assert rig.commands().count("record.cancel") == 1
        rig.event("cancelled")
    else:
        assert "record.cancel" not in rig.commands()
        assert rig.cancels == 1
        assert rig.pill.calls[-1] == (PillState.DONE, None, None)
    assert rig.core.phase.value == "finishing"
    rig.during_paste = None
    rig.start()
    assert rig.commands().count("record.start") == 2
    rig.assert_hotkey_state(HotkeyState.RECORDING)
    assert rig.recording


@pytest.mark.parametrize("response", ["result", "error"])
def test_limit_recognizes_already_stopped_audio_and_releases_hotkey(
    rig: Rig, response: str
) -> None:
    """T-59: воркер отвергает повторный stop; t_ms отсчитывается от record.limit."""
    rig.start()
    rig.now += 120

    def worker(message: dict[str, Any]) -> None:
        if message["type"] == "record.stop":
            rig.event("error", code="bad-state")
        elif message["type"] == "recognize":
            rig.now += 0.375
            rig.event(response, text=MARKER, code="timeout")

    rig.during_send = worker
    rig.event("record.limit")
    assert rig.sent[1:] == [({"type": "recognize", "utterance_id": rig.uid}, RECOGNIZE_TIMEOUT_S)]
    assert rig.core.phase.value == "finishing"
    rig.assert_hotkey_state(HotkeyState.IDLE)
    assert not rig.recording
    rig.backend.ungrab_escape.assert_called_once_with()
    if response == "result":
        assert rig.pasted == [(MARKER, 42, PasteMode.AUTO)]
        assert rig.stats.events[-1]["t_ms"] == 375.0
        assert rig.stats.events[-1]["audio_ms"] == 120000.0
        assert rig.pill.calls[-1] == (PillState.DONE, None, None)
        assert all(state != PillState.ERROR for state, _, _ in rig.pill.calls)
    else:
        assert not rig.pasted
        assert rig.pill.calls[-1] == (PillState.ERROR, ERROR_RECOGNITION_FAILED, None)
    rig.during_send = None
    rig.start()
    assert rig.commands().count("record.start") == 2


@pytest.mark.parametrize(
    "kind",
    [PasteOutcomeKind.PASTED, PasteOutcomeKind.CLIPBOARD_ONLY, PasteOutcomeKind.WINDOW_CHANGED],
)
@pytest.mark.parametrize("nested", ["result", "error", "cancel", "escape"])
def test_reentrant_delivery_is_deferred_and_cannot_overwrite_terminal_state(
    rig: Rig, nested: str, kind: PasteOutcomeKind, caplog: pytest.LogCaptureFixture
) -> None:
    rig.start()
    rig.stop()
    before = list(rig.pill.calls)
    caplog.set_level(logging.DEBUG, logger="test.dictation")
    rig.outcomes.append(kind)
    state = PillState.DONE if kind == PasteOutcomeKind.PASTED else PillState.CLIPBOARD_ONLY

    def reenter() -> None:
        if nested == "cancel":
            rig.core.cancel("tray")
        elif nested == "escape":
            rig.fsm.escape(rig.now)
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
        assert rig.commands() == ["record.start", "record.stop", "recognize"]
        reason = CLIPBOARD_WINDOW_CHANGED if kind == PasteOutcomeKind.WINDOW_CHANGED else None
        assert rig.pill.calls[-1] == (state, reason, None)
        assert rig.tray.state == (
            TrayState.DONE if kind == PasteOutcomeKind.PASTED else TrayState.IDLE
        )
        assert rig.core.last_text == MARKER and rig.tray.has_last_text
        assert [e["result"] for e in rig.stats.events] == ["ok"]
        assert rig.fsm.state == HotkeyState.IDLE and rig.cancels == 0
        assert not any(t.delay == CANCEL_TIMEOUT_MS for t in rig.timers)
        assert "поздняя отмена после доставки" in caplog.text
    elif nested == "error":
        assert rig.pill.calls[-1] == (PillState.ERROR, ERROR_RECOGNITION_RESTARTED, None)
        assert rig.core.last_text is None
    else:
        assert rig.pill.calls[-1][0] == state
        assert len(rig.stats.events) == 1
    assert rig.core.phase == DictationPhase.FINISHING and rig.done == 1


@pytest.mark.parametrize("kind", [PasteOutcomeKind.BUSY, PasteOutcomeKind.FAILED])
def test_reentrant_cancel_without_delivery_still_cancels(rig: Rig, kind: PasteOutcomeKind) -> None:
    rig.start()
    rig.stop()
    rig.outcomes.append(kind)
    rig.during_paste = lambda: rig.core.cancel("tray")
    rig.result()
    assert rig.commands()[-1] == "record.cancel"
    assert rig.fsm.state == HotkeyState.IDLE and rig.cancels == 1
    rig.event("cancelled")
    assert rig.pill.calls[-1] == (PillState.CANCELLED, None, None)
    assert [e["result"] for e in rig.stats.events] == ["cancelled"]
    assert rig.core.last_text is None and not rig.tray.has_last_text
    assert len(rig.pasted) == 1


@pytest.mark.parametrize("fields", [{}, {"text": 123}, {"text": [MARKER]}])
def test_invalid_result_degrades_to_empty(
    rig: Rig, fields: dict[str, object], caplog: pytest.LogCaptureFixture
) -> None:
    rig.start()
    rig.stop()
    caplog.set_level(logging.DEBUG, logger="test.dictation")
    rig.event("result", **fields)
    assert rig.pill.calls[-1] == (PillState.EMPTY, None, None)
    assert rig.fsm.state == HotkeyState.IDLE
    assert not rig.pasted and rig.core.last_text is None
    assert [e["result"] for e in rig.stats.events] == ["empty"]
    assert "некорректное поле text" in caplog.text and MARKER not in caplog.text


@pytest.mark.parametrize(
    "fields", [{}, {"peak_dbfs": MARKER}, {"peak_dbfs": "-30"}, {"peak_dbfs": True}]
)
def test_invalid_level_is_ignored(
    rig: Rig, fields: dict[str, object], caplog: pytest.LogCaptureFixture
) -> None:
    rig.start()
    before = list(rig.trace)
    caplog.set_level(logging.DEBUG, logger="test.dictation")
    rig.event("level", **fields)
    assert rig.trace == before
    assert rig.fsm.state == HotkeyState.RECORDING and rig.recording
    assert len(caplog.records) == 1 and caplog.records[0].levelno == logging.DEBUG
    assert not caplog.records[0].args and MARKER not in caplog.text


@pytest.mark.parametrize("failure", ["params", "send"])
def test_start_failure_releases_hotkey_and_shows_error(
    rig: Rig, failure: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def fail(*args: object, **kwargs: object) -> None:
        if failure == "params":
            assert rig.core.phase == DictationPhase.IDLE
            raise RuntimeError(MARKER)
        # Ошибка транспорта не содержит распознанного текста.
        raise BrokenPipeError("Канал воркера закрыт")

    monkeypatch.setattr(rig.core, "_record_params" if failure == "params" else "_send", fail)
    rig.start()
    assert rig.fsm.state == HotkeyState.IDLE
    rig.backend.ungrab_escape.assert_called_once_with()
    assert not rig.sent and not rig.recording
    assert ("recording", True) not in rig.trace
    assert rig.pill.calls == [(PillState.ERROR, ERROR_RECOGNITION_FAILED, None)]
    assert rig.tray.state == TrayState.ERROR and not rig.stats.events
    assert MARKER not in caplog.text
    if failure == "send":
        record = caplog.records[-1]
        assert record.getMessage() == "диктовка: отправка команды record.start не удалась"
        assert record.exc_info is not None
        assert isinstance(record.exc_info[1], BrokenPipeError)
        assert "Канал воркера закрыт" in caplog.text
    monkeypatch.undo()
    rig.start()
    assert rig.commands() == ["record.start"]
    assert rig.fsm.state.value == HotkeyState.RECORDING.value and rig.recording


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
    assert rig.fsm.state == HotkeyState.IDLE
    assert rig.cancels == int(phase in ("recording", "processing", "busy"))
    if phase != "idle":
        rig.backend.ungrab_escape.assert_called_once_with()
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
def test_text_never_reaches_logs_indicators_or_stats(
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


def test_cancel_escalates_close_then_restart(rig: Rig) -> None:
    rig.start()
    rig.core.cancel("tray")
    assert "audio.close" not in rig.commands()
    rig.timer(CANCEL_TIMEOUT_MS).fire()
    assert rig.sent[-1][0] == {"type": "audio.close"}
    assert rig.pill.calls[-1][0] == PillState.CANCELLED
    rig.restart_worker.assert_not_called()
    rig.timer(CANCEL_RESTART_MS).fire()
    rig.restart_worker.assert_called_once_with()


@pytest.mark.parametrize("late", [False, True])
@pytest.mark.parametrize("tail_done", [False, True])
def test_cancel_ack_cancels_escalations(rig: Rig, late: bool, tail_done: bool) -> None:
    rig.start()
    rig.core.cancel("tray")
    if late:
        rig.timer(CANCEL_TIMEOUT_MS).fire()
        if tail_done:
            rig.timer(STATE_DURATION_MS[PillState.CANCELLED]).fire()
    pending = [t for t in rig.timers if not t.fired]
    rig.event("cancelled")
    for timer in pending:
        assert timer.cancelled
        timer.callback()
    assert rig.commands().count("audio.close") == int(late)
    rig.restart_worker.assert_not_called()
    assert len(rig.stats.events) == 1


@pytest.mark.parametrize("late", [False, True])
def test_shutdown_cancels_escalations(rig: Rig, late: bool) -> None:
    rig.start()
    rig.core.cancel("tray")
    if late:
        rig.timer(CANCEL_TIMEOUT_MS).fire()
    pending = [t for t in rig.timers if not t.fired]
    rig.core.shutdown()
    before = list(rig.sent)
    for timer in pending:
        assert timer.cancelled
        timer.callback()
    assert rig.sent == before
    rig.restart_worker.assert_not_called()


def test_unconfirmed_cancel_blocks_new_recording_without_losing_restart(rig: Rig) -> None:
    rig.start()
    rig.core.cancel("tray")
    rig.timer(CANCEL_TIMEOUT_MS).fire()
    rig.start()
    assert rig.commands().count("record.start") == 1
    rig.timer(STATE_DURATION_MS[PillState.CANCELLED]).fire()
    rig.start()
    assert rig.commands().count("record.start") == 1
    rig.timer(CANCEL_RESTART_MS).fire()
    rig.restart_worker.assert_called_once_with()
    rig.start()
    assert rig.commands().count("record.start") == 2


def test_synchronous_cancel_ack_on_audio_close_cancels_restart(rig: Rig) -> None:
    rig.start()
    rig.core.cancel("tray")

    def ack(message: dict[str, Any]) -> None:
        if message["type"] == "audio.close":
            rig.event("cancelled")

    rig.during_send = ack
    rig.timer(CANCEL_TIMEOUT_MS).fire()
    restart = next(t for t in rig.timers if t.delay == CANCEL_RESTART_MS)
    assert restart.cancelled
    restart.callback()
    rig.restart_worker.assert_not_called()


def test_audio_close_failure_still_restarts_worker(rig: Rig) -> None:
    rig.start()
    rig.core.cancel("tray")

    def fail(message: dict[str, Any]) -> None:
        raise OSError(MARKER)

    rig.during_send = fail
    rig.timer(CANCEL_TIMEOUT_MS).fire()
    rig.timer(CANCEL_RESTART_MS).fire()
    rig.restart_worker.assert_called_once_with()


def microphone_event(rig: Rig, kind: str, **fields: object) -> None:
    """События проходят настоящий IPC; generation добавляет супервизор."""
    message = {"type": kind, "utterance_id": rig.uid, **fields}
    decoded = ipc.FrameReader().feed(ipc.encode(message))[0]
    rig.core.on_worker_event({**decoded, "generation": rig.worker_generation})


@pytest.mark.parametrize("device", ["", "alsa_input.usb-mic"])
@pytest.mark.parametrize("stop", ["button", "silent", "limit", "record.limit"])
def test_microphone_test_cycle_is_private(
    rig: Rig, device: str, stop: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    updates: list[MicrophoneTestUpdate] = []

    def encode_command(message: dict[str, Any]) -> None:
        ipc.encode(message)

    rig.during_send = encode_command
    assert rig.core.start_test(device, updates.append)
    assert updates == [MicrophoneTestUpdate("recording")]
    assert rig.sent[0][0].get("device") == (device or None)
    assert rig.sent[0][0]["limit_s"] == 10.0
    assert rig.sent[0][1] == 40.0
    assert rig.recording
    assert not any(entry[0] == "active_window" for entry in rig.trace)
    microphone_event(rig, "audio.ready", device="USB-микрофон")
    microphone_event(rig, "level", peak_dbfs=-18.0, rms_dbfs=-26.0)
    assert updates[-1] == MicrophoneTestUpdate("recording", peak_dbfs=-18.0)
    if stop == "button":
        rig.core.stop_test()
    elif stop == "limit":
        rig.timer(TEST_RECORD_LIMIT_MS).fire()
    else:
        microphone_event(rig, stop)
    assert str(updates[-1].state) == "processing"
    assert not rig.recording
    assert rig.commands() == [
        "record.start",
        *([] if stop == "record.limit" else ["record.stop"]),
        "recognize",
    ]
    microphone_event(rig, "result", text=MARKER, t_ms=310)
    assert updates[-1] == MicrophoneTestUpdate("done", text=MARKER, duration_s=0.31)
    assert rig.core.phase == DictationPhase.IDLE
    assert not rig.core.test_active
    assert rig.core.last_text is None
    assert not rig.pasted
    assert not rig.stats.events
    assert not rig.pill.calls
    assert not rig.tray.has_last_text
    rig.device_selected.assert_not_called()
    rig.device_changed.assert_not_called()
    rig.device_lost.assert_not_called()
    assert MARKER not in caplog.text
    assert not [timer for timer in rig.timers if not timer.cancelled and not timer.fired]
    rig.core.stop_test()
    assert str(updates[-1].state) == "done"


@pytest.mark.parametrize("indicator", ["tray", "set_recording"])
def test_microphone_limit_timer_survives_indicator_failure(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, indicator: str
) -> None:
    updates: list[MicrophoneTestUpdate] = []
    target, name = (rig.tray, "set_state") if indicator == "tray" else (rig.core, "_set_recording")
    original = getattr(target, name)

    def fail_on_start(value: object) -> None:
        if value is True or value is TrayState.LISTENING:
            # Предел уже взведён до вызова неисправного индикатора.
            rig.timer(TEST_RECORD_LIMIT_MS)
            raise RuntimeError(MARKER)
        original(value)

    monkeypatch.setattr(target, name, fail_on_start)
    with pytest.raises(RuntimeError, match=MARKER):
        rig.core.start_test("", updates.append)
    assert rig.core.phase == DictationPhase.RECORDING
    assert rig.commands() == ["record.start"]
    assert updates == []
    rig.timer(TEST_RECORD_LIMIT_MS).fire()
    assert rig.commands() == ["record.start", "record.stop", "recognize"]
    assert rig.core.phase.value == "processing"
    assert not rig.recording
    assert updates == [MicrophoneTestUpdate("processing")]


@pytest.mark.parametrize("phase", ["recording", "processing"])
def test_microphone_test_excludes_hotkey_dictation(rig: Rig, phase: str) -> None:
    updates: list[MicrophoneTestUpdate] = []
    rig.core.start_test("", updates.append)
    if phase == "processing":
        rig.core.stop_test()
    before = rig.commands()
    rig.start()
    rig.stop()
    assert rig.commands() == before
    assert rig.core.phase.value == phase
    rig.assert_hotkey_state(HotkeyState.IDLE)
    rig.core.stop_test()
    if phase == "recording":
        microphone_event(rig, "result", text=MARKER, t_ms=310)
    else:
        microphone_event(rig, "cancelled")
    rig.start()
    assert rig.commands().count("record.start") == 2


@pytest.mark.parametrize("phase", ["recording", "processing"])
def test_dictation_cannot_be_interrupted_by_microphone_test(rig: Rig, phase: str) -> None:
    rig.start()
    if phase == "processing":
        rig.stop()
    commands = rig.commands()
    updates: list[MicrophoneTestUpdate] = []
    assert not rig.core.start_test("other", updates.append)
    assert str(updates[-1].state) == "error"
    assert "диктовку" in updates[-1].message
    rig.core.stop_test()
    rig.core.cancel_test()
    assert rig.commands() == commands
    if phase == "recording":
        rig.stop()
    rig.result()
    assert len(rig.pasted) == len(rig.stats.events) == 1


def test_microphone_processing_stop_cancels_and_discards_late_result(rig: Rig) -> None:
    updates: list[MicrophoneTestUpdate] = []
    rig.core.start_test("", updates.append)
    rig.core.stop_test()
    rig.core.stop_test()
    rig.core.stop_test()
    assert rig.commands() == ["record.start", "record.stop", "recognize", "record.cancel"]
    microphone_event(rig, "result", text=MARKER, t_ms=310)
    assert str(updates[-1].state) == "processing"
    microphone_event(rig, "cancelled")
    assert updates[-1] == MicrophoneTestUpdate("idle")
    microphone_event(rig, "result", text=MARKER, t_ms=310)
    assert str(updates[-1].state) == "idle"
    assert not rig.pasted
    assert not rig.stats.events


@pytest.mark.parametrize(
    "code", ["audio-no-device", "audio-busy", "audio-failed", "no-model", "engine-failed"]
)
def test_microphone_error_is_plain_and_private(
    rig: Rig, code: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    updates: list[MicrophoneTestUpdate] = []
    rig.core.start_test("alsa_input.usb-mic", updates.append)
    microphone_event(rig, "error", code=code, message=f"{MARKER} /private/path alsa_input.usb-mic")
    assert str(updates[-1].state) == "error"
    assert updates[-1].message
    assert not any(word in updates[-1].message for word in (MARKER, "alsa", "/", "engine"))
    assert MARKER not in caplog.text
    assert not rig.pasted
    assert not rig.stats.events
    assert not rig.pill.calls
    rig.device_lost.assert_not_called()
    assert not rig.recording


@pytest.mark.parametrize("failure", ["record.start", "record.stop", "recognize", "watchdog"])
def test_microphone_failures_release_recording_without_logging_exception(
    rig: Rig, failure: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)

    def send(message: dict[str, Any]) -> None:
        ipc.encode(message)
        if message["type"] == failure:
            raise RuntimeError(MARKER)

    rig.during_send = send
    updates: list[MicrophoneTestUpdate] = []
    rig.core.start_test("", updates.append)
    if failure != "record.start":
        rig.core.stop_test()
    if failure == "watchdog":
        rig.timer(PROCESSING_WATCHDOG_MS).fire()
    assert str(updates[-1].state) == "error"
    assert rig.commands()[-1] == "record.cancel"
    assert not rig.recording
    microphone_event(rig, "cancelled")
    assert not rig.pasted
    assert not rig.stats.events
    assert not rig.pill.calls
    assert MARKER not in caplog.text


def test_microphone_cancel_watchdogs_always_release_worker(rig: Rig) -> None:
    updates: list[MicrophoneTestUpdate] = []
    rig.core.start_test("", updates.append)
    rig.core.cancel_test()
    rig.timer(CANCEL_TIMEOUT_MS).fire()
    assert str(updates[-1].state) == "idle"
    assert rig.commands()[-1] == "audio.close"
    assert not rig.core.start_test("", Mock())
    rig.timer(CANCEL_RESTART_MS).fire()
    rig.restart_worker.assert_called_once_with()
    assert not rig.pasted
    assert not rig.stats.events
    assert not rig.pill.calls


@pytest.mark.parametrize("microphone_test", [False, True])
def test_stop_survives_persistent_ui_failures(
    rig: Rig,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    microphone_test: bool,
) -> None:
    if microphone_test:
        assert rig.core.start_test("", Mock())
        monkeypatch.setattr(rig.core, "_test_callback", Mock(side_effect=RuntimeError(MARKER)))
    else:
        rig.start()
    set_recording = Mock(side_effect=RuntimeError(MARKER))
    set_state = Mock(side_effect=RuntimeError(MARKER))
    show_state = Mock(side_effect=RuntimeError(MARKER))
    monkeypatch.setattr(rig.core, "_set_recording", set_recording)
    monkeypatch.setattr(rig.tray, "set_state", set_state)
    monkeypatch.setattr(rig.pill, "show_state", show_state)

    if microphone_test:
        rig.core.stop_test()
    else:
        rig.stop()

    assert rig.commands() == ["record.start", "record.stop", "recognize"]
    rig.timer(PROCESSING_WATCHDOG_MS)
    assert set_recording.call_count == (2 if microphone_test else 1)
    assert set_state.call_count == 1
    assert show_state.call_count == (0 if microphone_test else 1)
    assert len(caplog.records) == (4 if microphone_test else 3)
    assert all(record.exc_info is not None for record in caplog.records)
    assert MARKER not in caplog.text


@pytest.mark.parametrize("terminal", ["result", "timeout"])
def test_finish_microphone_test_survives_persistent_ui_failures(
    rig: Rig,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    terminal: str,
) -> None:
    assert rig.core.start_test("", Mock())
    if terminal == "result":
        rig.core.stop_test()
    else:
        rig.core.cancel("tray")
    callback = Mock(side_effect=RuntimeError(MARKER))
    monkeypatch.setattr(rig.core, "_test_callback", callback)
    monkeypatch.setattr(rig.core, "_set_recording", Mock(side_effect=RuntimeError(MARKER)))
    monkeypatch.setattr(rig.tray, "set_state", Mock(side_effect=RuntimeError(MARKER)))

    if terminal == "result":
        microphone_event(rig, "result", text=MARKER, t_ms=310)
    else:
        rig.timer(CANCEL_TIMEOUT_MS).fire()
        assert rig.sent[-1][0] == {"type": "audio.close"}
        rig.timer(CANCEL_RESTART_MS).fire()
        rig.restart_worker.assert_called_once_with()

    assert not rig.core.test_active
    assert callback.call_count == 1
    assert len(caplog.records) == 3
    assert all(record.exc_info is not None for record in caplog.records)
    assert MARKER not in caplog.text


def test_microphone_test_rejects_old_generation_and_preserves_last_dictation(rig: Rig) -> None:
    rig.start()
    rig.stop()
    rig.result("Предыдущая диктовка")
    previous = rig.core.last_text
    rig.stats.events.clear()
    rig.pasted.clear()
    updates: list[MicrophoneTestUpdate] = []
    rig.core.start_test("", updates.append)
    rig.core.stop_test()
    rig.core.on_worker_event(
        {"type": "result", "generation": 0, "utterance_id": rig.uid, "text": MARKER, "t_ms": 1}
    )
    assert str(updates[-1].state) == "processing"
    rig.worker_generation += 1
    microphone_event(rig, "result", text=MARKER, t_ms=1)
    assert str(updates[-1].state) == "error"
    assert rig.core.last_text == previous
    assert not rig.pasted
    assert not rig.stats.events


@pytest.mark.parametrize("action", ["result", "cancel-in-callback"])
def test_microphone_synchronous_result_and_stop_callbacks(rig: Rig, action: str) -> None:
    updates: list[MicrophoneTestUpdate] = []

    def receive(update: MicrophoneTestUpdate) -> None:
        updates.append(update)
        if action == "cancel-in-callback" and update.state == "processing":
            rig.core.stop_test()

    def send(message: dict[str, Any]) -> None:
        ipc.encode(message)
        if message["type"] == "recognize":
            microphone_event(rig, "result", text=MARKER, t_ms=310)
        elif message["type"] == "record.cancel":
            microphone_event(rig, "cancelled")

    rig.during_send = send
    rig.core.start_test("", receive)
    rig.core.stop_test()
    assert [update.state for update in updates] == [
        "recording",
        "processing",
        "done" if action == "result" else "idle",
    ]
    assert ("recognize" in rig.commands()) == (action == "result")
    assert not rig.recording
    assert not rig.stats.events
    assert not rig.pasted
    assert not [timer for timer in rig.timers if not timer.cancelled and not timer.fired]


def test_microphone_cancel_timeout_callback_cannot_restart_before_ack(rig: Rig) -> None:
    attempts: list[bool] = []

    def receive(update: MicrophoneTestUpdate) -> None:
        if update.state == "idle":
            attempts.append(rig.core.start_test("", Mock()))

    rig.core.start_test("", receive)
    rig.core.cancel_test()
    rig.timer(CANCEL_TIMEOUT_MS).fire()
    assert attempts == [False]
    assert rig.commands() == ["record.start", "record.cancel", "audio.close"]


@pytest.mark.parametrize("device", ["", "alsa_input.usb-mic"])
def test_level_monitor_records_without_recognition(rig: Rig, device: str) -> None:
    updates: list[MicrophoneLevelUpdate] = []
    assert rig.core.start_level_monitor(device, updates.append)
    assert updates == [MicrophoneLevelUpdate("listening")]
    assert rig.core.level_active and not rig.core.test_active
    assert rig.sent[0][0].get("device") == (device or None)
    assert rig.sent[0][0]["limit_s"] == LEVEL_RECORD_LIMIT_S
    assert rig.recording and rig.tray.state == TrayState.LISTENING
    assert (
        rig.trace.index(("pill", PillState.LISTENING, None, None))
        < rig.trace.index(("tray", TrayState.LISTENING, None))
        < rig.trace.index(("recording", True))
        < rig.trace.index(("send", "record.start"))
    )
    assert not any(entry[0] == "active_window" for entry in rig.trace)
    microphone_event(rig, "level", peak_dbfs=-18, rms_dbfs=-30)
    assert updates[-1] == MicrophoneLevelUpdate("listening", peak_dbfs=-18)
    microphone_event(rig, "silent")
    microphone_event(rig, "result", text=MARKER, t_ms=1)
    assert updates[-1].state == "listening"
    rig.core.stop_level_monitor()
    rig.core.stop_level_monitor()
    assert rig.commands() == ["record.start", "record.cancel"]
    assert not rig.recording and rig.tray.state == TrayState.IDLE
    microphone_event(rig, "cancelled")
    assert updates[-1] == MicrophoneLevelUpdate("idle")
    assert not rig.core.level_active and not rig.core.test_active
    assert not rig.stats.events and not rig.pasted
    assert rig.pill.calls == [
        (PillState.LISTENING, None, None),
        (PillState.LISTENING, None, 0.7),
    ]
    assert rig.pill.hidden
    assert rig.core.last_text is None


@pytest.mark.parametrize("synchronous", [False, True])
def test_level_limit_restarts_with_fresh_id(rig: Rig, synchronous: bool) -> None:
    updates: list[MicrophoneLevelUpdate] = []
    rig.core.start_level_monitor("chosen", updates.append)
    previous = rig.uid

    def send(message: dict[str, Any]) -> None:
        if message["type"] == "record.cancel" and synchronous:
            rig.core.on_worker_event({**message, "type": "cancelled", "generation": 1})

    rig.during_send = send
    microphone_event(rig, "record.limit")
    assert rig.uid != previous
    assert rig.commands() == ["record.start", "record.cancel", "record.start"]
    rig.core.on_worker_event({"type": "cancelled", "utterance_id": previous, "generation": 1})
    microphone_event(rig, "level", peak_dbfs=-20, rms_dbfs=-30)
    assert updates[-1] == MicrophoneLevelUpdate("listening", peak_dbfs=-20)
    assert rig.recording and rig.core.level_active
    assert rig.sent[-1][0]["device"] == "chosen"


@pytest.mark.parametrize("busy", ["dictation", "test", "level", "cancel", "closed"])
def test_level_monitor_rejects_busy(rig: Rig, busy: str) -> None:
    if busy in ("dictation", "cancel"):
        rig.start()
        if busy == "cancel":
            rig.core.cancel("test")
    elif busy == "test":
        rig.core.start_test("", Mock())
    elif busy == "level":
        rig.core.start_level_monitor("", Mock())
    else:
        rig.core.shutdown()
    before = rig.commands()
    updates: list[MicrophoneLevelUpdate] = []
    assert not rig.core.start_level_monitor("", updates.append)
    assert updates == [MicrophoneLevelUpdate("error", message=TEST_BUSY)]
    assert rig.commands() == before


def test_level_monitor_excludes_test_and_hotkey(rig: Rig) -> None:
    rig.core.start_level_monitor("", Mock())
    updates: list[MicrophoneTestUpdate] = []
    assert not rig.core.start_test("", updates.append)
    assert updates == [MicrophoneTestUpdate("error", message=TEST_BUSY)]
    rig.start()
    rig.stop()
    rig.core.stop_test()
    rig.core.cancel_test()
    assert rig.commands() == ["record.start"]
    assert rig.core.level_active and not rig.core.test_active


@pytest.mark.parametrize("action", ["cancel", "shutdown", "generation"])
def test_level_monitor_lifecycle_closes_microphone(rig: Rig, action: str) -> None:
    updates: list[MicrophoneLevelUpdate] = []
    rig.core.start_level_monitor("", updates.append)
    if action == "cancel":
        rig.core.cancel("tray")
        microphone_event(rig, "cancelled")
    elif action == "shutdown":
        rig.core.shutdown()
    else:
        rig.worker_generation += 1
        microphone_event(rig, "level", peak_dbfs=-10, rms_dbfs=-20)
    assert rig.commands()[-1] == "record.cancel"
    assert not rig.recording and rig.tray.state == TrayState.IDLE
    assert updates[-1].state == ("error" if action == "generation" else "idle")
    assert not rig.core.level_active
    assert not rig.stats.events
    assert rig.pill.calls == [(PillState.LISTENING, None, None)]
    assert rig.pill.hidden


@pytest.mark.parametrize("failure", ["start", "level", "indicator", "callback", "stop", "limit"])
def test_level_exceptions_close_without_leaking(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, failure: str
) -> None:
    updates: list[MicrophoneLevelUpdate] = []

    def fail(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError(MARKER)

    def receive(update: MicrophoneLevelUpdate) -> None:
        updates.append(update)
        if failure == "callback" and update.state == "listening":
            fail()

    if failure == "indicator":
        original = rig.core._set_recording

        def indicator(value: bool) -> None:
            if value:
                fail()
            original(value)

        monkeypatch.setattr(rig.core, "_set_recording", indicator)
    if failure == "start":
        rig.during_send = lambda msg: fail() if msg["type"] == "record.start" else None
    rig.core.start_level_monitor("", receive)
    if failure == "level":
        monkeypatch.setattr(rig.core, "_on_device_resolved", fail)
        microphone_event(rig, "audio.ready", device="ALSA /path")
    elif failure in ("stop", "limit"):
        rig.during_send = lambda msg: fail() if msg["type"] == "record.cancel" else None
        if failure == "stop":
            rig.core.stop_level_monitor()
        else:
            microphone_event(rig, "record.limit")
    assert updates[-1].state == "error"
    assert "record.cancel" in rig.commands()
    assert not rig.recording and not rig.core.level_active
    assert not rig.stats.events
    assert rig.pill.calls == [(PillState.LISTENING, None, None)]
    assert rig.pill.hidden
    assert MARKER not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


def test_level_cancel_waits_for_audio_closed_before_idle(rig: Rig) -> None:
    updates: list[MicrophoneLevelUpdate] = []
    rig.core.start_level_monitor("", updates.append)
    rig.core.stop_level_monitor()
    rig.timer(CANCEL_TIMEOUT_MS).fire()
    assert updates[-1].state == "listening"
    assert rig.commands()[-1] == "audio.close"
    assert not rig.core.start_test("", Mock())
    rig.core.on_worker_event({"type": "audio.closed", "generation": 1})
    assert str(updates[-1].state) == "idle"
    assert rig.core.start_test("", Mock())
    rig.restart_worker.assert_not_called()


def test_level_shutdown_callback_cannot_reopen_microphone(rig: Rig) -> None:
    attempts: list[bool] = []

    def receive(update: MicrophoneLevelUpdate) -> None:
        if update.state == "idle":
            attempts.append(rig.core.start_level_monitor("", Mock()))

    rig.core.start_level_monitor("", receive)
    rig.core.shutdown()
    assert attempts == [False]
    assert rig.commands() == ["record.start", "record.cancel"]
    assert not rig.recording


def test_level_late_limit_cannot_restart_after_stop(rig: Rig) -> None:
    rig.core.start_level_monitor("", Mock())
    rig.core.stop_level_monitor()
    microphone_event(rig, "record.limit")
    microphone_event(rig, "cancelled")
    microphone_event(rig, "record.limit")
    assert rig.commands() == ["record.start", "record.cancel"]
    assert not rig.core.level_active


@pytest.mark.parametrize("ack", ["cancelled", "audio.closed", "restart"])
def test_level_total_limit_survives_record_limits(rig: Rig, ack: str) -> None:
    updates: list[MicrophoneLevelUpdate] = []
    microphone_open = False

    def send(message: dict[str, Any]) -> None:
        nonlocal microphone_open
        ipc.encode(message)
        if message["type"] == "record.start":
            microphone_open = True
        elif message["type"] in ("record.cancel", "audio.close"):
            microphone_open = False

    rig.during_send = send
    assert LEVEL_TOTAL_LIMIT_S == 180.0
    assert rig.core.start_level_monitor("", updates.append)
    total = rig.timer(180000)
    for _ in range(8):
        microphone_event(rig, "record.limit")
        assert rig.timer(180000) is total
        assert microphone_open
    assert len([timer for timer in rig.timers if timer.delay == 180000]) == 1
    total.fire()
    assert rig.commands()[-1] == "record.cancel"
    assert not microphone_open and not rig.recording and not rig.core.level_active
    assert rig.tray.state == TrayState.IDLE and rig.pill.hidden
    starts = rig.commands().count("record.start")
    microphone_event(rig, "record.limit")
    if ack == "cancelled":
        microphone_event(rig, "cancelled")
    else:
        rig.timer(CANCEL_TIMEOUT_MS).fire()
        assert rig.commands()[-1] == "audio.close"
        if ack == "audio.closed":
            rig.core.on_worker_event({"type": "audio.closed", "generation": 1})
        else:
            rig.timer(CANCEL_RESTART_MS).fire()
            rig.restart_worker.assert_called_once_with()
    microphone_event(rig, "record.limit")
    assert rig.commands().count("record.start") == starts
    assert set(rig.commands()) <= {"record.start", "record.cancel", "audio.close"}
    assert updates[-1] == MicrophoneLevelUpdate("idle", message=LEVEL_LIMIT_MESSAGE)
    assert not any(update.state == "error" for update in updates)
    assert rig.core.phase == DictationPhase.IDLE
    assert not [timer for timer in rig.timers if not timer.cancelled and not timer.fired]
    assert not rig.pasted and not rig.stats.events

    # Продление разрешено только новым явным запуском; старый таймер уже безвреден.
    assert rig.core.start_level_monitor("", updates.append)
    total.callback()
    assert updates[-1] == MicrophoneLevelUpdate("listening")
    assert rig.core.level_active and microphone_open
    assert rig.timer(180000) is not total
    rig.core.stop_level_monitor()
    microphone_event(rig, "cancelled")
    assert updates[-1] == MicrophoneLevelUpdate("idle")


@pytest.mark.parametrize("stopping", [False, True])
def test_late_audio_closed_cannot_finish_new_level_episode(rig: Rig, stopping: bool) -> None:
    assert rig.core.start_level_monitor("", Mock())
    rig.core.stop_level_monitor()
    rig.timer(CANCEL_TIMEOUT_MS).fire()
    assert rig.commands()[-1] == "audio.close"
    old_watchdog = rig.timer(CANCEL_RESTART_MS)
    microphone_event(rig, "cancelled")
    assert old_watchdog.cancelled

    updates: list[MicrophoneLevelUpdate] = []
    assert rig.core.start_level_monitor("", updates.append)
    if stopping:
        rig.core.stop_level_monitor()
    watchdog = rig.timer(CANCEL_TIMEOUT_MS if stopping else int(LEVEL_TOTAL_LIMIT_S * 1000))
    before = rig.commands()
    rig.core.on_worker_event({"type": "audio.closed", "generation": 1})
    old_watchdog.callback()
    assert rig.commands() == before
    assert not watchdog.cancelled
    assert updates == [MicrophoneLevelUpdate("listening")]
    assert rig.core.phase == DictationPhase.RECORDING
    assert rig.core.level_active is not stopping
    watchdog.fire()
    if not stopping:
        rig.timer(CANCEL_TIMEOUT_MS).fire()
    assert rig.commands()[-1] == "audio.close"
    rig.timer(CANCEL_RESTART_MS).fire()
    rig.restart_worker.assert_called_once_with()
    assert updates[-1].state == "idle"


def test_level_audio_closed_can_arrive_synchronously(rig: Rig) -> None:
    updates: list[MicrophoneLevelUpdate] = []
    assert rig.core.start_level_monitor("", updates.append)
    rig.core.stop_level_monitor()

    def send(message: dict[str, Any]) -> None:
        if message["type"] == "audio.close":
            rig.core.on_worker_event({"type": "audio.closed", "generation": 1})

    rig.during_send = send
    rig.timer(CANCEL_TIMEOUT_MS).fire()
    assert updates[-1] == MicrophoneLevelUpdate("idle")
    assert not [timer for timer in rig.timers if not timer.cancelled and not timer.fired]
    rig.restart_worker.assert_not_called()


def test_level_callback_exception_after_finish_cannot_cancel_new_test(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    test_updates: list[MicrophoneTestUpdate] = []
    accepted: list[bool] = []

    def receive(update: MicrophoneLevelUpdate) -> None:
        if update.state == "idle":
            accepted.append(rig.core.start_test("", test_updates.append))
        elif update.peak_dbfs is not None:
            rig.core.stop_level_monitor()
            raise RuntimeError(MARKER)

    def send(message: dict[str, Any]) -> None:
        if message["type"] == "record.cancel":
            microphone_event(rig, "cancelled")

    rig.during_send = send
    assert rig.core.start_level_monitor("", receive)
    microphone_event(rig, "level", peak_dbfs=-20, rms_dbfs=-30)

    assert accepted == [True]
    assert not rig.core.level_active and rig.core.test_active
    assert rig.core.phase == DictationPhase.RECORDING
    assert rig.recording and not rig.core._cancel_pending
    assert rig.commands() == ["record.start", "record.cancel", "record.start"]
    rig.core.stop_test()
    microphone_event(rig, "result", text=MARKER, t_ms=500)
    assert test_updates[-1].state == "done"
    assert test_updates[-1].text == MARKER
    assert rig.core.phase.name == "IDLE"
    assert MARKER not in caplog.text
