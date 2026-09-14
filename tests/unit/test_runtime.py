"""Проводка runtime с настоящим оркестратором и фейковыми внешними ресурсами."""

from __future__ import annotations

import atexit
import logging
from collections.abc import Callable
from typing import Any
from unittest.mock import Mock, call

import pytest
from PyQt5 import sip
from PyQt5.QtCore import QEventLoop, QMimeData, QObject, Qt

from astra_voice import runtime as module
from astra_voice.core.dictation import (
    BUSY_RETRY_MS,
    CANCEL_RESTART_MS,
    CANCEL_TIMEOUT_MS,
    DictationPhase,
)
from astra_voice.core.settings import Settings
from astra_voice.platform import paste as paste_module
from astra_voice.platform.hotkey import (
    DEFAULT_CANDIDATES,
    RECORD_LIMIT_S,
    GrabResult,
    HotkeyBackend,
    HotkeyEvent,
    HotkeyFsm,
    HotkeyManager,
    HotkeyMode,
    HotkeyState,
    ResultCode,
)
from astra_voice.platform.paste import KDE_HINT, PasteMode, PasteOutcomeKind, normalize
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

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        settings: Settings | None = None,
        *,
        hotkey_factory: Callable[[], HotkeyManager] | None = None,
    ) -> None:
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
        self.capture_watchdogs: list[Mock] = []
        self.capture_open_ok = True
        self.capture_watchdog_factory = Mock(side_effect=self.create_capture_watchdog)
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
        self.restore_paste = Mock(side_effect=self.restore_pending)
        self.atexit_register = Mock()
        self.atexit_unregister = Mock(side_effect=lambda callback: self.record("atexit.unregister"))
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
        monkeypatch.setattr(atexit, "register", self.atexit_register)
        monkeypatch.setattr(atexit, "unregister", self.atexit_unregister)
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
            hotkey_factory=hotkey_factory
            if hotkey_factory is not None
            else Mock(return_value=self.hotkey),
            stats_factory=Mock(return_value=self.stats),
            paste_func=self.paste,
            restore_paste=self.restore_paste,
            x11_factory=Mock(return_value=self.x11),
            guard_factory=self.guard_factory,
            provider_factory=Mock(return_value=self.provider),
            capture_watchdog_factory=self.capture_watchdog_factory,
        )

    def create_capture_watchdog(self) -> Mock:
        watchdog = Mock(active=False)

        def open_capture() -> bool:
            self.record("capture.open")
            watchdog.active = self.capture_open_ok
            return self.capture_open_ok

        def close_capture() -> None:
            watchdog.active = False
            self.record("capture.close")

        watchdog.open.side_effect = open_capture
        watchdog.close.side_effect = close_capture
        self.capture_watchdogs.append(watchdog)
        return watchdog

    def record(self, action: str) -> None:
        self.trace.append(action)
        if action == self.fail_at:
            raise RuntimeError(MARKER)

    def create_timer(self) -> FakeTimer:
        name = ("tick", "stats")[len(self.timers)] if len(self.timers) < 2 else "scheduled"
        timer = FakeTimer(self.record, name)
        self.timers.append(timer)
        return timer

    def restore_pending(self) -> bool:
        self.record("restore_paste")
        return False

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


@pytest.mark.parametrize("failure", ["pill", "provider", "tray", "guard", "stats", "supervisor"])
@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_failed_build_releases_resources_and_cannot_restart(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failure: str,
    cleanup_fails: bool,
) -> None:
    """Откат сохраняет исходную ошибку, удаляет Qt и выдерживает повторный вход."""
    trace: list[str] = []
    children: list[QObject] = []
    resources = {name: Mock() for name in ("x11", "hotkey", "pill", "tray", "guard", "stats")}
    error = RuntimeError("Ошибка сборки")
    runtime = DictationRuntime.__new__(DictationRuntime)

    def record(action: str) -> None:
        trace.append(action)
        # Даже повторный вход из уборки не должен начать запуск или второй откат.
        runtime.start()
        runtime.shutdown()
        if cleanup_fails:
            raise RuntimeError(MARKER)

    for name, method in (
        ("x11", "close"),
        ("hotkey", "ungrab"),
        ("pill", "hide"),
        ("tray", "stop"),
        ("guard", "stop"),
        ("stats", "flush"),
    ):
        getattr(resources[name], method).side_effect = lambda action=f"{name}.{method}": record(
            action
        )
    resources["hotkey"].backend.ungrab_escape.side_effect = lambda: record("escape.ungrab")
    resources["hotkey"].backend.close.side_effect = lambda: record("backend.close")
    orchestrator = Mock()
    orchestrator.shutdown.side_effect = lambda: record("orchestrator.shutdown")
    monkeypatch.setattr(module, "DictationOrchestrator", Mock(return_value=orchestrator))

    def factory(name: str, *args: object, parent: QObject | None = None, **kw: object) -> Mock:
        if parent is not None:
            # Как Pill: QObject уже привязан к runtime, но фабрика ещё может упасть.
            child = QObject(parent)
            child.destroyed.connect(lambda: trace.append(f"{name}.delete"))
            children.append(child)
        if name == failure:
            raise error
        return resources.get(name, Mock())

    factories = {
        name: Mock(side_effect=lambda *args, name=name, **kw: factory(name, *args, **kw))
        for name in ("x11", "hotkey", "pill", "provider", "tray", "guard", "stats", "supervisor")
    }
    with pytest.raises(RuntimeError) as caught:
        DictationRuntime.__init__(
            runtime,
            settings=Settings(),
            session_kind=SessionKind.FLY,
            x11_factory=factories["x11"],
            hotkey_factory=factories["hotkey"],
            pill_factory=factories["pill"],
            provider_factory=factories["provider"],
            tray_factory=factories["tray"],
            guard_factory=factories["guard"],
            stats_factory=factories["stats"],
            supervisor_factory=factories["supervisor"],
        )
    assert caught.value is error

    expected: list[str] = []
    if failure == "supervisor":
        expected.extend(("orchestrator.shutdown", "stats.flush"))
    if failure in ("stats", "supervisor"):
        expected.append("guard.stop")
    if failure in ("guard", "stats", "supervisor"):
        expected.append("tray.stop")
    if failure != "pill":
        expected.append("pill.hide")
    expected.extend(("hotkey.ungrab", "escape.ungrab", "backend.close", "x11.close"))
    if failure in ("guard", "stats", "supervisor"):
        expected.append("guard.delete")
    if failure not in ("pill", "provider"):
        expected.append("tray.delete")
    expected.append("pill.delete")
    assert trace == expected
    assert all(sip.isdeleted(child) for child in children)
    assert runtime.children() == []
    assert MARKER not in caplog.text
    assert bool(caplog.records) == cleanup_fails

    runtime.shutdown()
    runtime.shutdown()
    runtime.start()
    assert trace == expected
    resources["x11"].open.assert_not_called()
    resources["hotkey"].grab.assert_not_called()


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


@pytest.mark.parametrize("mode", list(HotkeyMode))
def test_pill_cancel_resets_real_hotkey_and_allows_next_dictation(
    monkeypatch: pytest.MonkeyPatch, mode: HotkeyMode
) -> None:
    backend = Mock(spec=HotkeyBackend)
    backend.grab_combo.return_value = GrabResult("ok", keycode=65, mods=4)
    backend.grab_escape.return_value = GrabResult("ok", keycode=9)
    backend.fileno.return_value = 17
    hotkey = HotkeyManager(backend)
    rig = Rig(monkeypatch, Settings(hotkey_mode=mode.value), hotkey_factory=lambda: hotkey)
    runtime = rig.runtime
    runtime.start()

    hotkey.handle_event(HotkeyEvent("KeyPress", 65, 1000, mods=4), rig.now)
    assert hotkey.fsm.state.value == "recording"
    assert_phase(runtime, DictationPhase.RECORDING)
    first_uid = rig.supervisor.send.call_args.args[0]["utterance_id"]
    backend.grab_escape.assert_called_once_with()

    assert runtime.pill.on_cancel_clicked is not None
    runtime.pill.on_cancel_clicked()
    assert hotkey.fsm.state == HotkeyState.IDLE
    backend.ungrab_escape.assert_called_once_with()
    assert [item.args[0]["type"] for item in rig.supervisor.send.call_args_list] == [
        "record.start",
        "record.cancel",
    ]
    rig.event(type="cancelled", utterance_id=first_uid)
    rig.pill.show_state.assert_called_with(PillState.CANCELLED)

    rig.now += 1
    hotkey.handle_event(HotkeyEvent("KeyRelease", 65, 2000, mods=4), rig.now)
    rig.now += 1
    hotkey.handle_event(HotkeyEvent("KeyPress", 65, 3000, mods=4), rig.now)
    assert hotkey.fsm.state.value == "recording"
    assert_phase(runtime, DictationPhase.RECORDING)
    commands = [item.args[0] for item in rig.supervisor.send.call_args_list]
    assert [command["type"] for command in commands] == [
        "record.start",
        "record.cancel",
        "record.start",
    ]
    assert commands[-1]["utterance_id"] != first_uid
    rig.pill.show_state.assert_called_with(PillState.LISTENING)
    assert backend.grab_escape.call_count == 2


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


@pytest.mark.parametrize("text", [MARKER, f"{MARKER}\r\nстрока\rещё\n\x00\t\x1b"])
def test_copy_last_only_touches_clipboard(
    rig: Rig, caplog: pytest.LogCaptureFixture, text: str
) -> None:
    """T-58 (docs/test-plan.md): MIME с secret, без фразы в логе/уведомлениях; Fly TODO."""
    with caplog.at_level(logging.DEBUG):
        assert rig.runtime.last_text is None
        rig.tray.on_copy_last()
        rig.application.clipboard.assert_not_called()
        assert rig.clipboard.mock_calls == []
        rig.runtime.start()
        rig.recognize(text)
        rig.tray.on_copy_last()
    rig.application.clipboard.assert_called_once_with()
    rig.clipboard.setMimeData.assert_called_once()
    md = rig.clipboard.setMimeData.call_args.args[0]
    assert isinstance(md, QMimeData)
    assert md.hasFormat("text/plain")
    assert bytes(md.data("text/plain")) == normalize(text).encode("utf-8")
    assert md.text() == normalize(text)
    assert md.hasFormat(KDE_HINT)
    assert bytes(md.data(KDE_HINT)) == b"secret"
    rig.clipboard.setMimeData.assert_called_once_with(md)
    rig.clipboard.setText.assert_not_called()
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
    "stats.stop",
    "stats.delete",
    "scheduled.stop",
    "scheduled.delete",
    "restore_paste",
    "atexit.unregister",
    "audio.close",
    "worker.drain",
    "worker.stop",
    "hotkey.ungrab",
    "escape.ungrab",
    "capture.close",
    "backend.close",
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
    assert rig.runtime.begin_hotkey_capture()
    callback = Mock()
    rig.runtime.schedule(100, callback)
    monkeypatch.setattr(
        rig.runtime.orchestrator, "shutdown", lambda: rig.record("orchestrator.shutdown")
    )
    rig.trace.clear()
    rig.fail_at = failure
    rig.runtime.shutdown()
    assert rig.trace == SHUTDOWN_ORDER
    rig.hotkey.ungrab.assert_called_once_with()
    rig.capture_watchdogs[0].close.assert_called_once_with()
    rig.x11.close.assert_called_once_with()
    rig.supervisor.send.assert_called_once_with({"type": "audio.close"})
    rig.restore_paste.assert_called_once_with()
    rig.atexit_unregister.assert_called_once_with(rig.restore_paste)
    assert not rig.runtime.timers
    rig.timers[-1].fire()
    rig.timers[-1].timeout.emit()
    callback.assert_not_called()
    rig.runtime.shutdown()
    rig.runtime.start()
    assert rig.trace == SHUTDOWN_ORDER
    assert MARKER not in caplog.text
    assert bool(caplog.records) == (failure is not None)


def test_shutdown_during_pasting_restores_clipboard_once(rig: Rig) -> None:
    """Выход внутри AUTO-вставки возвращает снимок до завершения воркера."""
    saved_text = "Прежний буфер"
    phases: list[DictationPhase] = []

    def restore() -> bool:
        rig.record("restore_paste")
        rig.clipboard.setText(saved_text)
        return True

    def paste(text: str, target: int | None, mode: PasteMode) -> Mock:
        phases.append(rig.runtime.phase)
        rig.runtime.shutdown()
        return Mock(kind=PasteOutcomeKind.WINDOW_CHANGED)

    rig.restore_paste.side_effect = restore
    rig.paste.side_effect = paste
    rig.runtime.start()
    rig.recognize()
    assert phases == [DictationPhase.PASTING]
    rig.restore_paste.assert_called_once_with()
    rig.runtime.shutdown()
    rig.restore_paste.assert_called_once_with()
    rig.clipboard.setText.assert_called_once_with(saved_text)
    assert rig.trace.index("tick.delete") < rig.trace.index("restore_paste")
    assert rig.trace.index("restore_paste") < rig.trace.index("audio.close")
    assert rig.trace.index("audio.close") < rig.trace.index("worker.stop")
    assert rig.trace.index("worker.stop") < rig.trace.index("hotkey.ungrab")


def test_shutdown_after_clipboard_only_does_not_touch_qt_clipboard(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Без ожидания возврата намеренно оставленная фраза сохраняется."""
    restore_results: list[bool] = []

    def restore() -> bool:
        rig.record("restore_paste")
        restored = paste_module.restore_pending()
        restore_results.append(restored)
        return restored

    monkeypatch.setattr("PyQt5.QtWidgets.QApplication.clipboard", rig.application.clipboard)
    rig.restore_paste.side_effect = restore
    rig.paste.return_value.kind = PasteOutcomeKind.CLIPBOARD_ONLY
    rig.runtime.start()
    rig.recognize()
    rig.pill.show_state.assert_called_with(PillState.CLIPBOARD_ONLY)
    assert not paste_module.has_pending()
    rig.runtime.shutdown()
    rig.restore_paste.assert_called_once_with()
    assert restore_results == [False]
    assert not paste_module.has_pending()
    rig.application.clipboard.assert_not_called()
    assert rig.clipboard.mock_calls == []


def test_atexit_registers_only_restore_and_shutdown_unregisters_it(rig: Rig) -> None:
    """Аварийная ручка вызывает лишь восстановление, штатный выход снимает её."""
    rig.atexit_register.assert_not_called()
    rig.runtime.start()
    rig.runtime.start()
    rig.atexit_register.assert_called_once_with(rig.restore_paste)
    rig.atexit_unregister.assert_not_called()
    callback = rig.atexit_register.call_args.args[0]
    rig.trace.clear()
    assert callback() is False
    assert rig.trace == ["restore_paste"]
    rig.application.clipboard.assert_not_called()
    assert rig.clipboard.mock_calls == []
    rig.restore_paste.reset_mock()
    rig.runtime.shutdown()
    rig.runtime.shutdown()
    rig.restore_paste.assert_called_once_with()
    rig.atexit_unregister.assert_called_once_with(callback)


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


def test_begin_and_end_hotkey_capture_use_dedicated_watchdog(rig: Rig) -> None:
    runtime = rig.runtime
    rig.capture_watchdog_factory.assert_not_called()
    runtime.end_hotkey_capture()
    assert runtime.begin_hotkey_capture()
    watchdog = rig.capture_watchdogs[0]
    rig.capture_watchdog_factory.assert_called_once_with()
    watchdog.open.assert_called_once_with()
    assert watchdog.active
    assert runtime.begin_hotkey_capture()
    watchdog.open.assert_called_once_with()
    rig.capture_watchdog_factory.assert_called_once_with()
    runtime.end_hotkey_capture()
    runtime.end_hotkey_capture()
    watchdog.close.assert_called_once_with()
    assert not watchdog.active
    assert runtime.begin_hotkey_capture()
    assert len(rig.capture_watchdogs) == 2
    assert rig.capture_watchdogs[1] is not watchdog
    runtime.end_hotkey_capture()
    rig.capture_watchdogs[1].close.assert_called_once_with()
    assert rig.x11.mock_calls == [], "Захват поля затронул основное X-соединение"
    assert rig.hotkey.mock_calls == [], "Захват поля затронул соединение хоткея"
    assert not rig.timers, "Сторож поля использовал GUI-таймер"


def test_begin_hotkey_capture_failure_cleans_up_and_allows_retry(rig: Rig) -> None:
    rig.capture_open_ok = False
    assert not rig.runtime.begin_hotkey_capture()
    failed = rig.capture_watchdogs[0]
    failed.close.assert_called_once_with()
    rig.runtime.end_hotkey_capture()
    failed.close.assert_called_once_with()
    rig.capture_open_ok = True
    assert rig.runtime.begin_hotkey_capture()
    assert len(rig.capture_watchdogs) == 2
    rig.runtime.end_hotkey_capture()


def test_begin_hotkey_capture_after_expiry_replaces_watchdog(rig: Rig) -> None:
    assert rig.runtime.begin_hotkey_capture()
    expired = rig.capture_watchdogs[0]
    expired.active = False
    assert rig.runtime.begin_hotkey_capture()
    expired.close.assert_called_once_with()
    assert len(rig.capture_watchdogs) == 2
    rig.runtime.end_hotkey_capture()


def test_shutdown_stops_capture_even_without_runtime_start(rig: Rig) -> None:
    assert rig.runtime.begin_hotkey_capture()
    watchdog = rig.capture_watchdogs[0]
    rig.runtime.shutdown()
    rig.runtime.shutdown()
    rig.runtime.end_hotkey_capture()
    watchdog.close.assert_called_once_with()
    assert not watchdog.active
    assert not rig.runtime.begin_hotkey_capture()
    rig.capture_watchdog_factory.assert_called_once_with()


def test_cancel_restart_replaces_supervisor_and_routes_new_cycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    drain = DictationRuntime._drain_worker_events
    rig = Rig(monkeypatch)
    runtime = rig.runtime
    runtime.start()
    old = rig.supervisor
    old.generation = 7
    rig.hotkey.fsm.press(rig.now)
    runtime.orchestrator.cancel("tray")
    next(t for t in rig.timers if t.interval == CANCEL_TIMEOUT_MS).fire()
    new = Mock(state="running", generation=1)
    rig.supervisor_factory.return_value = new
    rig.trace.clear()
    new.start.side_effect = lambda: rig.record("new.start")
    next(t for t in rig.timers if t.interval == CANCEL_RESTART_MS).fire()
    assert rig.trace[:2] == ["worker.stop", "new.start"]
    assert runtime.supervisor is new
    old.stop.assert_called_once_with()
    new.start.assert_called_once_with()
    assert new.generation == 8
    rig.supervisor_factory.assert_called_with(
        on_event=runtime.orchestrator.on_worker_event, use_qt=True
    )
    old.send.reset_mock()
    rig.hotkey.fsm.press(rig.now + 3)
    assert new.send.call_args.args[0]["type"] == "record.start"
    rig.hotkey.fsm.release(rig.now + 4)
    assert [c.args[0]["type"] for c in new.send.call_args_list] == [
        "record.start",
        "record.stop",
        "recognize",
    ]
    callback = rig.supervisor_factory.call_args.kwargs["on_event"]
    callback({"type": "result", "generation": 7, "text": "старый результат"})
    rig.paste.assert_not_called()
    callback({"type": "result", "generation": 8, "text": MARKER})
    rig.paste.assert_called_once_with(MARKER, 4321, PasteMode.AUTO)
    monkeypatch.setattr(module, "QCoreApplication", Mock())
    drain(runtime)
    new.pump.assert_called_once_with(timeout=0.05)
    old.pump.assert_not_called()
    runtime.shutdown()
    new.stop.assert_called_once_with()
    old.send.assert_not_called()
    rig.supervisor_factory.reset_mock()
    runtime.restart_worker()
    rig.supervisor_factory.assert_not_called()
