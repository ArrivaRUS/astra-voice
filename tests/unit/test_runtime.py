"""Проводка runtime с настоящим оркестратором и фейковыми внешними ресурсами."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any
from unittest.mock import Mock, call

import pytest
from PyQt5.QtCore import QEventLoop, Qt

from astra_voice import runtime as module
from astra_voice.core.dictation import BUSY_RETRY_MS, DictationPhase
from astra_voice.core.settings import Settings
from astra_voice.platform.hotkey import (
    DEFAULT_CANDIDATES,
    RECORD_LIMIT_S,
    GrabResult,
    HotkeyFsm,
    HotkeyMode,
    HotkeyState,
    ResultCode,
)
from astra_voice.platform.paste import PasteMode, PasteOutcomeKind
from astra_voice.platform.session import SessionKind
from astra_voice.runtime import DictationRuntime
from astra_voice.ui.pill import PillState
from astra_voice.ui.tray_icons import TrayState

pytestmark = pytest.mark.unit
MARKER = "ГЕЛИОТРОП-7"


class Signal:
    """Синхронная доставка сигнала без объектов Qt."""

    def __init__(self) -> None:
        self.callbacks: list[Callable[..., None]] = []

    def connect(self, callback: Callable[..., None]) -> None:
        self.callbacks.append(callback)

    def emit(self, *args: object) -> None:
        for callback in self.callbacks:
            callback(*args)


class FakeTimer:
    """Ручной таймер с настоящей отменой доставки и наблюдаемым удалением."""

    def __init__(self, record: Callable[[str], None], name: str) -> None:
        self.timeout = Signal()
        self.record = record
        self.name = name
        self.active = False
        self.deleted = False
        self.single_shot = False
        self.timer_type: object = None
        self.interval = 0

    def setSingleShot(self, value: bool) -> None:
        self.single_shot = value

    def setTimerType(self, value: object) -> None:
        self.timer_type = value

    def start(self, ms: int) -> None:
        self.interval = ms
        self.active = True

    def stop(self) -> None:
        self.record(f"{self.name}.stop")
        self.active = False

    def deleteLater(self) -> None:
        self.record(f"{self.name}.delete")
        self.deleted = True

    def fire(self) -> None:
        if self.active and not self.deleted:
            if self.single_shot:
                self.active = False
            self.timeout.emit()


class Rig:
    """Общий журнал порядка действий и управляемые отказы каждого ресурса."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, settings: Settings | None = None) -> None:
        self.trace: list[str] = []
        self.fail_at: str | None = None
        self.timers: list[FakeTimer] = []
        self.now = 10.0
        self.pill = Mock()
        self.tray = Mock()
        self.guard = Mock()
        self.stats = Mock()
        self.provider = Mock()
        self.x11 = Mock()
        self.x11.active_window.return_value = 4321
        self.x11.open.side_effect = lambda: self.record("x11.open")
        self.x11.close.side_effect = lambda: self.record("x11.close")
        self.supervisor = Mock(state="new", generation=1)
        self.supervisor.start.side_effect = self.start_worker
        self.supervisor.stop.side_effect = self.stop_worker
        self.supervisor.send.side_effect = self.send
        self.hotkey = Mock()
        self.hotkey.fileno.return_value = 17
        self.hotkey.grab.side_effect = self.grab
        self.grab_code: ResultCode = "ok"
        self.hotkey.free_candidates.return_value = [DEFAULT_CANDIDATES[0]]
        self.hotkey.ungrab.side_effect = lambda: self.record("hotkey.ungrab")
        self.hotkey.backend.ungrab_escape.side_effect = lambda: self.record("escape.ungrab")
        self.hotkey.backend.close.side_effect = lambda: self.record("backend.close")
        self.guard.stop.side_effect = lambda: self.record("guard.stop")
        self.tray.stop.side_effect = lambda: self.record("tray.stop")
        self.notifier = Mock(activated=Signal())
        self.notifier.setEnabled.side_effect = lambda value: self.record("notifier.disable")
        self.notifier.deleteLater.side_effect = lambda: self.record("notifier.delete")
        self.clipboard = Mock()
        self.application = Mock()
        self.application.clipboard.return_value = self.clipboard
        self.notify = Mock()
        self.paste = Mock(return_value=Mock(kind=PasteOutcomeKind.PASTED))
        self.supervisor_factory = Mock(return_value=self.supervisor)
        self.pill_factory = Mock(return_value=self.pill)
        self.tray_factory = Mock(return_value=self.tray)
        self.guard_factory = Mock(return_value=self.guard)
        self.create_notifier = Mock(return_value=self.notifier)
        monkeypatch.setattr(DictationRuntime, "_create_timer", lambda runtime: self.create_timer())
        monkeypatch.setattr(DictationRuntime, "_create_notifier", self.create_notifier)
        monkeypatch.setattr(
            DictationRuntime, "_drain_worker_events", lambda runtime: self.record("worker.drain")
        )
        monkeypatch.setattr(module, "monotonic", lambda: self.now)
        monkeypatch.setattr(module, "QApplication", self.application)
        monkeypatch.setattr(module, "notify", self.notify)
        # Любое случайное создание реального таймера/наблюдателя ломает тест.
        monkeypatch.setattr(module, "QTimer", Mock(side_effect=AssertionError("Реальный таймер")))
        monkeypatch.setattr(
            module, "QSocketNotifier", Mock(side_effect=AssertionError("Реальный наблюдатель"))
        )
        self.runtime = DictationRuntime(
            settings=settings if settings is not None else Settings(),
            session_kind=SessionKind.FLY,
            supervisor_factory=self.supervisor_factory,
            pill_factory=self.pill_factory,
            tray_factory=self.tray_factory,
            hotkey_factory=Mock(return_value=self.hotkey),
            stats_factory=Mock(return_value=self.stats),
            paste_func=self.paste,
            x11_factory=Mock(return_value=self.x11),
            guard_factory=self.guard_factory,
            provider_factory=Mock(return_value=self.provider),
        )

    def record(self, action: str) -> None:
        self.trace.append(action)
        if action == self.fail_at:
            raise RuntimeError(MARKER)

    def create_timer(self) -> FakeTimer:
        timer = FakeTimer(self.record, "tick" if not self.timers else "scheduled")
        self.timers.append(timer)
        return timer

    def start_worker(self) -> None:
        self.record("worker.start")
        self.supervisor.state = "running"

    def stop_worker(self) -> None:
        self.record("worker.stop")
        self.supervisor.state = "stopped"

    def send(self, message: dict[str, Any], **kwargs: object) -> None:
        self.record(str(message["type"]))

    def grab(self, combo: str, mode: HotkeyMode) -> GrabResult:
        self.record("hotkey.grab")
        self.hotkey.fsm = HotkeyFsm(mode, self.hotkey.on_state)
        return GrabResult(self.grab_code)

    def event(self, **event: object) -> None:
        callback = self.supervisor_factory.call_args.kwargs["on_event"]
        callback({"generation": self.supervisor.generation, **event})

    def recognize(self, text: str = MARKER) -> None:
        self.hotkey.fsm.press(self.now)
        self.hotkey.fsm.release(self.now + 1)
        self.event(type="result", text=text)


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch) -> Rig:
    return Rig(monkeypatch)


def assert_phase(runtime: DictationRuntime, expected: DictationPhase) -> None:
    """Читает фазу заново после событий, не сохраняя сужение типа mypy."""
    assert runtime.phase == expected


def test_start_wires_resources_and_real_dictation(rig: Rig) -> None:
    """Реальные переходы проверяют обе стороны проводки, вставку и статистику."""
    runtime = rig.runtime
    rig.x11.open.assert_not_called()
    runtime.start()
    runtime.start()
    rig.supervisor.start.assert_called_once_with()
    rig.x11.open.assert_called_once_with()
    rig.tray.start.assert_called_once_with()
    rig.hotkey.grab.assert_called_once_with("Ctrl+Space", HotkeyMode.PTT)
    rig.pill_factory.assert_called_once_with(session=SessionKind.FLY, parent=runtime)
    rig.tray_factory.assert_called_once_with(rig.provider, hotkey="Ctrl+Space", parent=runtime)
    rig.guard_factory.assert_called_once_with(rig.pill, rig.tray, parent=runtime)
    rig.supervisor_factory.assert_called_once_with(
        on_event=runtime.orchestrator.on_worker_event, use_qt=True
    )
    assert rig.hotkey.on_state == runtime.orchestrator.on_hotkey_state
    assert rig.guard.on_stop_recording == runtime.orchestrator.on_indicators_lost
    rig.create_notifier.assert_called_once_with(17)
    rig.notifier.activated.emit(17)
    rig.hotkey.process_pending.assert_called_once_with()
    assert rig.timers[0].interval == 200
    rig.recognize()
    rig.paste.assert_called_once_with(MARKER, 4321, PasteMode.AUTO)
    assert runtime.last_text == MARKER
    assert_phase(runtime, DictationPhase.FINISHING)
    assert rig.hotkey.fsm.state == HotkeyState.IDLE
    rig.tray.set_has_last_text.assert_called_once_with(True)
    rig.stats.append.assert_called_once()
    rig.timers[-1].fire()
    assert_phase(runtime, DictationPhase.IDLE)


@pytest.mark.parametrize("source", ["pill", "tray"])
def test_cancel_callbacks(rig: Rig, monkeypatch: pytest.MonkeyPatch, source: str) -> None:
    """Обе кнопки вызывают единую отмену с правильным источником."""
    cancel = Mock()
    monkeypatch.setattr(rig.runtime.orchestrator, "cancel", cancel)
    callback = rig.pill.on_cancel_clicked if source == "pill" else rig.tray.on_cancel
    callback()
    cancel.assert_called_once_with(source)


def test_quit_callback_is_set_by_app_after_construction(rig: Rig) -> None:
    rig.tray.on_quit()
    requested = Mock()
    rig.runtime.on_quit_requested = requested
    rig.tray.on_quit()
    requested.assert_called_once_with()


@pytest.mark.parametrize("code", ["not-grabbed", "busy", "bad-combo", "duplicate"])
def test_failed_grab_keeps_app_running(
    rig: Rig, caplog: pytest.LogCaptureFixture, code: ResultCode
) -> None:
    rig.grab_code = code
    rig.hotkey.fileno.return_value = -1
    with caplog.at_level(logging.INFO):
        rig.runtime.start()
        rig.runtime.start()
    rig.tray.set_state.assert_called_once_with(TrayState.NOKEY)
    rig.notify.notify_hotkey_not_grabbed.assert_called_once_with()
    rig.notify.notify_tray_unavailable.assert_not_called()
    rig.hotkey.free_candidates.assert_called_once_with(list(DEFAULT_CANDIDATES))
    assert DEFAULT_CANDIDATES[0] in caplog.text
    assert rig.supervisor.state == "running"
    rig.create_notifier.assert_not_called()


def test_candidate_probe_failure_does_not_abort_start(rig: Rig) -> None:
    rig.grab_code = "busy"
    rig.hotkey.free_candidates.side_effect = RuntimeError(MARKER)
    rig.runtime.start()
    assert rig.runtime.tick_timer is not None
    rig.notify.notify_hotkey_not_grabbed.assert_called_once_with()


def test_tick_enforces_record_limit(rig: Rig) -> None:
    rig.runtime.start()
    rig.hotkey.fsm.press(rig.now)
    assert_phase(rig.runtime, DictationPhase.RECORDING)
    rig.now += RECORD_LIMIT_S
    rig.timers[0].fire()
    assert_phase(rig.runtime, DictationPhase.PROCESSING)
    assert [item.args[0]["type"] for item in rig.supervisor.send.call_args_list] == [
        "record.start",
        "record.stop",
        "recognize",
    ]


def test_settings_are_used_for_recording_and_paste(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(
        hotkey="Ctrl+Alt+D",
        hotkey_mode="toggle",
        pill_enabled=False,
        extra={"device": "микрофон", "silence_db": -50.0, "paste_clipboard_only": True},
    )
    rig = Rig(monkeypatch, settings)
    rig.runtime.start()
    rig.pill.set_enabled.assert_called_once_with(False)
    rig.hotkey.grab.assert_called_once_with(settings.hotkey, HotkeyMode.TOGGLE)
    rig.hotkey.fsm.press(rig.now)
    message = rig.supervisor.send.call_args.args[0]
    assert {key: message[key] for key in ("device", "limit_s", "silence_db", "insert")} == {
        "device": "микрофон",
        "limit_s": RECORD_LIMIT_S,
        "silence_db": -50.0,
        "insert": True,
    }
    rig.hotkey.fsm.press(rig.now + 1)
    rig.event(type="result", text=MARKER)
    rig.paste.assert_called_once_with(MARKER, 4321, PasteMode.CLIPBOARD_ONLY)
    settings.extra.clear()
    assert rig.runtime.record_params() == {
        "device": None,
        "limit_s": RECORD_LIMIT_S,
        "silence_db": -45.0,
        "insert": True,
    }
    assert rig.runtime.paste_mode() == PasteMode.AUTO


def test_schedule_cancel_stops_timer_and_queued_delivery(rig: Rig) -> None:
    callback = Mock()
    timer = rig.runtime.schedule(200, callback)
    assert timer is rig.timers[-1]
    fake = rig.timers[-1]
    assert fake.single_shot
    assert fake.timer_type == Qt.PreciseTimer
    assert fake.interval == 200
    rig.runtime.cancel_timer(timer)
    assert not fake.active
    assert fake.deleted
    assert rig.trace[-2:] == ["tick.stop", "tick.delete"]
    fake.fire()
    fake.timeout.emit()
    callback.assert_not_called()


def test_fired_timer_is_deleted(rig: Rig) -> None:
    callback = Mock()
    rig.runtime.schedule(10, callback)
    rig.timers[-1].fire()
    callback.assert_called_once_with()
    assert rig.timers[-1].deleted
    assert not rig.runtime.timers


def test_copy_last_only_touches_clipboard(rig: Rig, caplog: pytest.LogCaptureFixture) -> None:
    """Маркерная фраза не уходит в журнал и уведомления даже при копировании."""
    with caplog.at_level(logging.DEBUG):
        rig.tray.on_copy_last()
        rig.application.clipboard.assert_not_called()
        rig.runtime.start()
        rig.recognize()
        rig.tray.on_copy_last()
    rig.application.clipboard.assert_called_once_with()
    rig.clipboard.setText.assert_called_once_with(MARKER)
    rig.notify.assert_not_called()
    assert rig.notify.mock_calls == []
    assert MARKER not in caplog.text


SHUTDOWN_ORDER = [
    "orchestrator.shutdown",
    "guard.stop",
    "notifier.disable",
    "notifier.delete",
    "tick.stop",
    "tick.delete",
    "hotkey.ungrab",
    "escape.ungrab",
    "backend.close",
    "audio.close",
    "worker.drain",
    "worker.stop",
    "tray.stop",
    "x11.close",
]


@pytest.mark.parametrize("failure", [None, *SHUTDOWN_ORDER])
def test_shutdown_order_and_independent_failures(
    rig: Rig,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failure: str | None,
) -> None:
    """Ошибка любого шага не мешает последующим; повторный вход не делает ничего."""
    rig.runtime.start()
    monkeypatch.setattr(
        rig.runtime.orchestrator, "shutdown", lambda: rig.record("orchestrator.shutdown")
    )
    rig.trace.clear()
    rig.fail_at = failure
    rig.runtime.shutdown()
    assert rig.trace == SHUTDOWN_ORDER
    rig.hotkey.ungrab.assert_called_once_with()
    rig.x11.close.assert_called_once_with()
    rig.supervisor.send.assert_called_once_with({"type": "audio.close"})
    rig.runtime.shutdown()
    rig.runtime.start()
    assert rig.trace == SHUTDOWN_ORDER
    assert MARKER not in caplog.text
    assert bool(caplog.records) == (failure is not None)


def test_shutdown_during_recording_cancels_before_closing_audio(rig: Rig) -> None:
    rig.runtime.start()
    rig.hotkey.fsm.press(rig.now)
    assert_phase(rig.runtime, DictationPhase.RECORDING)
    rig.trace.clear()
    rig.runtime.shutdown()
    assert rig.trace.index("record.cancel") < rig.trace.index("guard.stop")
    assert rig.trace.index("record.cancel") < rig.trace.index("audio.close")
    assert rig.trace.index("audio.close") < rig.trace.index("worker.stop")
    rig.pill.hide.assert_called_once_with()
    assert_phase(rig.runtime, DictationPhase.IDLE)
    assert rig.guard.set_recording.call_args == call(False)
    before = rig.trace.copy()
    rig.notifier.activated.emit(17)
    rig.timers[0].timeout.emit()
    rig.runtime.shutdown()
    assert rig.trace == before


def test_shutdown_cancels_pending_paste_retry(rig: Rig) -> None:
    """О1: даже уже доставленный сигнал таймера не вставляет текст после выхода."""
    rig.runtime.start()
    rig.paste.return_value.kind = PasteOutcomeKind.BUSY
    rig.recognize()
    pending = rig.timers[-1]
    assert pending.interval == BUSY_RETRY_MS
    rig.runtime.shutdown()
    assert not pending.active
    assert pending.deleted
    pending.fire()
    pending.timeout.emit()
    assert rig.paste.call_count == 1
    assert not rig.runtime.timers


@pytest.mark.parametrize("state", ["new", "stopped"])
def test_shutdown_without_running_worker(rig: Rig, state: str) -> None:
    rig.supervisor.state = state
    rig.runtime.shutdown()
    rig.supervisor.send.assert_not_called()
    assert "worker.drain" not in rig.trace
    rig.supervisor.stop.assert_called_once_with()
    rig.x11.close.assert_called_once_with()


def test_indicators_lost_reaches_orchestrator(rig: Rig) -> None:
    rig.runtime.start()
    rig.hotkey.fsm.press(rig.now)
    rig.guard.on_stop_recording()
    assert rig.supervisor.send.call_args.args[0]["type"] == "record.cancel"
    rig.event(type="cancelled")
    rig.pill.show_state.assert_called_with(PillState.CANCELLED)


def test_backend_without_optional_cleanup_methods(rig: Rig) -> None:
    rig.hotkey.backend = object()
    rig.runtime.shutdown()
    rig.supervisor.stop.assert_called_once_with()
    rig.x11.close.assert_called_once_with()


def test_qt_resource_factories_use_runtime_as_parent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Штатные методы передают владельца Qt; сами конструкторы заменены фейками."""
    owner = Mock()
    timer_factory = Mock()
    notifier_factory = Mock()
    monkeypatch.setattr(module, "QTimer", timer_factory)
    monkeypatch.setattr(module, "QSocketNotifier", notifier_factory)
    assert DictationRuntime._create_timer(owner) is timer_factory.return_value
    timer_factory.assert_called_once_with(owner)
    assert DictationRuntime._create_notifier(owner, 17) is notifier_factory.return_value
    notifier_factory.assert_called_once_with(17, notifier_factory.Read, owner)


def test_hotkey_done_uses_current_fsm_and_monotonic_time(rig: Rig) -> None:
    """grab заменяет автомат, поэтому done должен обращаться к актуальному fsm."""
    rig.runtime.start()
    rig.hotkey.fsm.press(rig.now)
    rig.hotkey.fsm.release(rig.now + 1)
    done = Mock(wraps=rig.hotkey.fsm.done)
    rig.hotkey.fsm.done = done
    rig.now = 42.0
    rig.event(type="result", text=MARKER)
    done.assert_called_once_with(42.0)
    assert rig.hotkey.fsm.state == HotkeyState.IDLE


def test_drain_gives_worker_time_before_processing_qt_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Прокачка ограничена по времени и не включает пользовательский ввод."""
    owner = Mock()
    application = Mock()
    monkeypatch.setattr(module, "QCoreApplication", application)
    DictationRuntime._drain_worker_events(owner)
    owner.supervisor.pump.assert_called_once_with(timeout=0.05)
    application.processEvents.assert_called_once_with(QEventLoop.ExcludeUserInputEvents, 50)


def test_shutdown_reentrant_from_event_drain(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    """Вложенная обработка событий не запускает освобождение ресурсов повторно."""
    rig.runtime.start()
    monkeypatch.setattr(rig.runtime, "_drain_worker_events", rig.runtime.shutdown)
    rig.runtime.shutdown()
    rig.supervisor.stop.assert_called_once_with()
    rig.hotkey.ungrab.assert_called_once_with()
    rig.x11.close.assert_called_once_with()
