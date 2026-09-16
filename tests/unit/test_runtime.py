"""Проводка runtime с настоящим оркестратором и фейковыми внешними ресурсами."""

from __future__ import annotations

import atexit
import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import Mock, call

import pytest
from PyQt5 import sip
from PyQt5.QtCore import QEventLoop, QObject, Qt

from astra_voice import runtime as module
from astra_voice.core import paths
from astra_voice.core import stats as stats_module
from astra_voice.core.dictation import (
    BUSY_RETRY_MS,
    CANCEL_RESTART_MS,
    CANCEL_TIMEOUT_MS,
    RECOGNIZE_TIMEOUT_S,
    DictationPhase,
)
from astra_voice.core.settings import Settings, from_dict
from astra_voice.models.store import ModelRecord, ModelState, ModelStore
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
from astra_voice.platform.paste import PasteMode, PasteOutcomeKind, normalize
from astra_voice.platform.session import SessionKind
from astra_voice.runtime import DictationRuntime
from astra_voice.ui.pill import (
    ERROR_MODEL_LOAD_FAILED,
    ERROR_MODEL_NOT_LOADED,
    ERROR_SELFCHECK_FAILED,
    PillState,
)
from astra_voice.ui.tray_icons import TrayState
from astra_voice.worker import ipc
from astra_voice.worker.supervisor import WorkerSupervisor

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
        model_store: ModelStore | None = None,
        hotkey_factory: Callable[[], HotkeyManager] | None = None,
        session_kind: SessionKind = SessionKind.FLY,
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
        self.set_action_handler = Mock(name="set_action_handler")
        self.notify.set_action_handler = self.set_action_handler
        self.paste = Mock(return_value=Mock(kind=PasteOutcomeKind.PASTED))
        self.publish_clipboard = Mock(return_value=True)
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
        self.data_dir = Mock(return_value=Path("/tmp/astra-voice-test-data"))
        monkeypatch.setattr(paths, "data_dir", self.data_dir)
        monkeypatch.setattr("PyQt5.QtWidgets.QApplication.clipboard", self.application.clipboard)
        monkeypatch.setattr(module, "publish_clipboard", self.publish_clipboard)
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
            session_kind=session_kind,
            model_store=model_store,
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


@pytest.fixture(autouse=True)
def smoke_wav(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    wav = tmp_path / "smoke-ru.wav"
    wav.touch()
    monkeypatch.setattr(module, "smoke_wav_path", lambda: wav)
    return wav


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch) -> Rig:
    return Rig(monkeypatch)


@pytest.mark.parametrize("event_kind", ["selected", "changed", "lost"])
def test_microphone_notification_wiring(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, event_kind: str
) -> None:
    monkeypatch.setattr(rig.runtime.orchestrator, "_clock", lambda: rig.now)
    rig.runtime.settings.extra["device"] = "alsa_input.usb-headset"
    rig.runtime.start()
    rig.hotkey.fsm.press(rig.now)
    if event_kind == "lost":
        rig.event(type="error", code="audio-no-device")
        assert rig.notify.mock_calls == [call.notify_microphone_lost()]
    else:
        # Даже позднее первое открытие означает выбор микрофона.
        rig.now += 1.0
        rig.event(
            type="audio.ready",
            device="Встроенный микрофон",
            changed="Источник звука изменился: Встроенный микрофон",
        )
        assert rig.notify.mock_calls == [call.notify_microphone_selected("Встроенный микрофон")]
        if event_kind == "changed":
            rig.event(
                type="audio.ready",
                device="USB-гарнитура",
                changed="Источник звука изменился: USB-гарнитура",
            )
            assert rig.notify.mock_calls == [
                call.notify_microphone_selected("Встроенный микрофон"),
                call.notify_microphone_changed("USB-гарнитура"),
            ]


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
    rig.supervisor_factory.assert_called_once_with(on_event=runtime._on_worker_event, use_qt=True)
    assert rig.hotkey.on_state == runtime._on_hotkey_state
    assert rig.guard.on_stop_recording == runtime.orchestrator.on_indicators_lost
    rig.create_notifier.assert_called_once_with(17)
    rig.notifier.activated.emit(17)
    rig.hotkey.process_pending.assert_called_once_with()
    assert rig.timers[0].interval == 200
    assert runtime._regrab_timer is None
    rig.stats.append.assert_called_once_with(
        "hotkey_grab", key_role="text", result="ok", attempts=1
    )
    rig.stats.append.reset_mock()
    rig.recognize()
    rig.paste.assert_called_once_with(MARKER, 4321, PasteMode.AUTO)
    assert runtime.last_text == MARKER
    assert_phase(runtime, DictationPhase.FINISHING)
    assert rig.hotkey.fsm.state == HotkeyState.IDLE
    rig.tray.set_has_last_text.assert_called_once_with(True)
    rig.stats.append.assert_called_once()
    rig.timers[-1].fire()
    assert_phase(runtime, DictationPhase.IDLE)


def test_notification_actions_show_window_and_are_removed_on_shutdown(rig: Rig) -> None:
    rig.set_action_handler.assert_not_called()
    rig.runtime.start()
    rig.runtime.start()
    handlers = {
        key: callback
        for key, callback in (item.args for item in rig.set_action_handler.call_args_list)
    }
    assert set(handlers) == {"choose-hotkey", "show-details", "choose-microphone"}
    assert rig.set_action_handler.call_count == 3
    # Назначение окна после start тоже работает; до него нажатие безопасно.
    for callback in handlers.values():
        callback()
    show = Mock()
    rig.runtime.on_show_requested = show
    for callback in handlers.values():
        callback()
    assert show.call_args_list == [call(), call(), call()]
    rig.set_action_handler.reset_mock()
    rig.runtime.shutdown()
    rig.runtime.shutdown()
    rig.runtime.start()
    assert rig.set_action_handler.call_args_list == [call(key, None) for key in handlers]
    assert rig.runtime.on_show_requested is None
    for callback in handlers.values():
        callback()  # Уже поставленные в очередь вызовы после shutdown не открывают окно.
    assert show.call_count == 3


def test_notification_actions_registered_before_startup_notification(rig: Rig) -> None:
    rig.grab_code = "not-grabbed"
    show = Mock()
    rig.runtime.on_show_requested = show

    def click_on_notification(combo: str) -> None:
        handlers = dict(item.args for item in rig.set_action_handler.call_args_list)
        handlers["choose-hotkey"]()

    rig.notify.notify_hotkey_not_grabbed.side_effect = click_on_notification
    rig.runtime.start()
    show.assert_called_once_with()


def test_partial_start_unregisters_notification_actions(rig: Rig) -> None:
    rig.x11.open.side_effect = RuntimeError("Ошибка запуска")
    with pytest.raises(RuntimeError, match="Ошибка запуска"):
        rig.runtime.start()
    assert rig.set_action_handler.call_count == 3
    keys = [item.args[0] for item in rig.set_action_handler.call_args_list]
    rig.set_action_handler.reset_mock()
    rig.runtime.shutdown()
    assert rig.set_action_handler.call_args_list == [call(key, None) for key in keys]


@pytest.mark.parametrize("metadata", [{}, {"model_id": "gigaam", "model_revision": "v3"}])
def test_start_loads_configured_model(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, metadata: dict[str, str]
) -> None:
    rig = Rig(
        monkeypatch,
        from_dict(
            {
                "model_dir": str(tmp_path),
                "model_variant": "int8",
                "model_threads": 4,
                **metadata,
            }
        ),
    )
    rig.runtime.start()
    rig.runtime.start()
    rig.supervisor.send.assert_not_called()
    rig.pill.show_state.assert_not_called()
    rig.event(type="hello")
    rig.supervisor.send.assert_called_once()
    request = rig.supervisor.send.call_args.args[0]
    assert request["type"] == "model.load"
    assert request["dir"] == str(tmp_path)
    if metadata:
        assert request["id"] == "gigaam"
        assert request["revision"] == "v3"
    assert request["variant"] == "int8"
    assert request["threads"] == 4
    assert rig.supervisor.send.call_args.kwargs == {"timeout": 10.0}
    assert rig.trace.index("worker.start") < rig.trace.index("model.load")
    rig.pill.show_state.assert_called_once_with(PillState.LOADING_MODEL)
    rig.tray.set_state.assert_not_called()


@pytest.mark.parametrize("field", ["threads", "min_ram_mb"])
@pytest.mark.parametrize("value", [0, -1, "invalid", "4", True])
def test_invalid_model_parameters_are_not_sent(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    field: str,
    value: object,
) -> None:
    rig = Rig(
        monkeypatch,
        Settings(extra={"model_dir": f"/tmp/{MARKER}", f"model_{field}": value}),
    )
    rig.runtime.start()
    with caplog.at_level(logging.WARNING, logger=module.__name__):
        rig.event(type="hello")

    rig.supervisor.send.assert_not_called()
    rig.pill.show_state.assert_called_once_with(PillState.ERROR, text=ERROR_MODEL_LOAD_FAILED)
    rig.tray.set_state.assert_called_once_with(TrayState.ERROR)
    assert not rig.runtime._loading_model
    assert rig.runtime._model_load_failures == 1
    assert [(record.levelno, record.getMessage()) for record in caplog.records] == [
        (logging.WARNING, "параметры модели заданы неверно")
    ]
    assert MARKER not in caplog.text
    assert "/tmp/" not in caplog.text


def test_invalid_model_parameters_stop_validation_after_two_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = Rig(monkeypatch, Settings(extra={"model_dir": "/tmp/model", "model_threads": 0}))
    encode = Mock(wraps=ipc.encode)
    monkeypatch.setattr(ipc, "encode", encode)
    rig.runtime.start()
    for _ in range(3):
        rig.event(type="hello")
        rig.supervisor.generation += 1
    assert encode.call_count == 2
    assert rig.runtime._model_load_failures == 2
    assert not rig.runtime._loading_model
    rig.supervisor.send.assert_not_called()


def test_start_loads_model_from_store(monkeypatch: pytest.MonkeyPatch) -> None:
    rig = Rig(monkeypatch, from_dict({"model_id": "gigaam", "model_revision": "v3"}))
    rig.runtime.start()
    rig.supervisor.send.assert_not_called()
    rig.event(type="hello")
    rig.supervisor.send.assert_called_once()
    request = rig.supervisor.send.call_args.args[0]
    assert request["type"] == "model.load"
    assert request["id"] == "gigaam"
    assert request["revision"] == "v3"
    assert request["dir"] == str(paths.model_store_dir() / "gigaam" / "v3")


def test_start_loads_current_revision_without_model_settings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = ModelStore(root=tmp_path)
    rig = Rig(monkeypatch, model_store=store)
    staging = store.staging_dir("installed-model", "r2")
    (staging / "model.onnx").write_bytes(b"model data")
    directory = store.commit("installed-model", "r2")
    store.set_current("installed-model", "r2")

    rig.runtime.start()
    rig.supervisor.send.assert_not_called()
    rig.event(type="hello")

    rig.supervisor.send.assert_called_once()
    request = rig.supervisor.send.call_args.args[0]
    assert request["type"] == "model.load"
    assert request["id"] == "installed-model"
    assert request["revision"] == "r2"
    assert request["dir"] == str(directory)
    ipc.encode(request)


def test_start_without_model_does_not_load(rig: Rig, caplog: pytest.LogCaptureFixture) -> None:
    rig.runtime.start()
    with caplog.at_level(logging.INFO, logger=module.__name__):
        rig.event(type="hello")
    rig.supervisor.send.assert_not_called()
    rig.pill.show_state.assert_not_called()
    assert [(record.levelno, record.getMessage()) for record in caplog.records] == [
        (logging.INFO, "модель в настройках не указана")
    ]
    rig.recognize()
    rig.paste.assert_called_once_with(MARKER, 4321, PasteMode.AUTO)


@pytest.mark.parametrize("fields", [{"load_ms": 123.5}, {}, {"load_ms": MARKER}])
def test_model_loaded_hides_pill_and_forwards_event(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    fields: dict[str, object],
) -> None:
    rig = Rig(monkeypatch, Settings(extra={"model_dir": f"/tmp/{MARKER}"}))
    rig.runtime.start()
    rig.event(type="hello")
    on_event = Mock()
    monkeypatch.setattr(rig.runtime.orchestrator, "on_worker_event", on_event)
    caplog.set_level(logging.INFO, logger=module.__name__)
    event = {"type": "model.loaded", "generation": 1, "dir": MARKER, **fields}
    rig.supervisor_factory.call_args.kwargs["on_event"](event)
    rig.event(type="result", utterance_id="file", text="Проверка связи")
    on_event.assert_called_once_with(event)
    assert on_event.call_args.args[0] is event
    rig.pill.hide.assert_called_once_with()
    rig.tray.set_state.assert_called_once_with(TrayState.IDLE)
    assert "модель загружена" in caplog.text
    if fields.get("load_ms") == 123.5:
        assert "модель загружена за 124 мс" in caplog.text
    assert MARKER not in caplog.text
    rig.hotkey.fsm.press(rig.now)
    assert rig.supervisor.send.call_args.args[0]["type"] == "record.start"


@pytest.mark.parametrize(
    "code", ["engine-failed", "load-timeout", "bad-state", "busy", "restart-required", MARKER]
)
@pytest.mark.parametrize(
    "correlation", [{"request_type": "model.load"}, {"response_type": "model.loaded"}]
)
def test_model_load_error_shows_reason_and_releases_hotkey(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    code: str,
    correlation: dict[str, str],
) -> None:
    rig = Rig(monkeypatch, Settings(extra={"model_dir": f"/tmp/{MARKER}"}))
    rig.runtime.start()
    rig.event(type="hello")
    rig.event(type="error", code=code, message=MARKER, **correlation)
    assert [(record.levelno, record.getMessage()) for record in caplog.records] == [
        (logging.WARNING, "Не удалось загрузить модель")
    ]
    rig.pill.show_state.assert_called_with(PillState.ERROR, text=ERROR_MODEL_LOAD_FAILED)
    rig.tray.set_state.assert_called_once_with(TrayState.ERROR)
    rig.notify.assert_not_called()
    assert not rig.notify.mock_calls
    rig.hotkey.fsm.press(rig.now)
    assert rig.supervisor.send.call_args.args[0]["type"] == "record.start"


@pytest.mark.parametrize("restart", [False, True])
def test_model_load_send_failure_shows_error_and_releases_hotkey(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, restart: bool
) -> None:
    rig = Rig(monkeypatch, Settings(extra={"model_dir": f"/tmp/{MARKER}"}))
    if restart:
        rig.runtime.start()
        rig.event(type="hello")
        rig.event(type="model.loaded")
        rig.event(type="result", utterance_id="file", text="Проверка связи")
        rig.tray.set_state.assert_called_once_with(TrayState.IDLE)
        rig.tray.set_state.reset_mock()
    rig.supervisor.send.side_effect = RuntimeError(f"Воркер не запущен: /tmp/{MARKER}")
    with caplog.at_level(logging.WARNING, logger=module.__name__):
        if restart:
            rig.runtime.restart_worker()
        else:
            rig.runtime.start()
        rig.event(type="hello")
    rig.pill.show_state.assert_called_with(PillState.ERROR, text=ERROR_MODEL_LOAD_FAILED)
    rig.tray.set_state.assert_called_once_with(TrayState.ERROR)
    assert any(
        record.name == module.__name__ and record.levelno == logging.WARNING
        for record in caplog.records
    )
    assert MARKER not in caplog.text
    assert "/tmp/" not in caplog.text
    rig.supervisor.send.side_effect = rig.send
    rig.hotkey.fsm.press(rig.now)
    assert rig.supervisor.send.call_args.args[0]["type"] == "record.start"
    assert_phase(rig.runtime, DictationPhase.RECORDING)


@pytest.mark.parametrize("code", ["load-timeout", "worker-crashed"])
def test_supervisor_restart_loads_model_on_new_hello(
    monkeypatch: pytest.MonkeyPatch, code: str
) -> None:
    rig = Rig(monkeypatch, from_dict({"model_id": "gigaam", "model_revision": "v3"}))
    rig.runtime.start()
    rig.event(type="hello")
    request = rig.supervisor.send.call_args.args[0]
    # Как в супервизоре: поколение меняется до доставки ошибки старой попытки.
    rig.supervisor.generation += 1
    rig.event(type="error", generation=1, code=code, request_type="model.load")
    rig.pill.show_state.assert_called_with(PillState.ERROR, text=ERROR_MODEL_LOAD_FAILED)
    rig.supervisor.send.assert_called_once_with(request, timeout=10.0)
    rig.event(type="hello")
    assert rig.supervisor.send.call_args_list == [call(request, timeout=10.0)] * 2
    rig.pill.show_state.assert_called_with(PillState.LOADING_MODEL)
    rig.supervisor_factory.assert_called_once()


def test_model_load_timeouts_stop_after_two_failures(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    rig = Rig(monkeypatch, from_dict({"model_dir": f"/tmp/{MARKER}"}))
    rig.runtime.start()
    rig.event(type="hello")
    for generation in (1, 2):
        assert rig.supervisor.send.call_count == generation
        rig.supervisor.generation += 1
        rig.event(
            type="error", generation=generation, code="load-timeout", request_type="model.load"
        )
        rig.pill.show_state.reset_mock()
        rig.tray.set_state.reset_mock()
        rig.event(type="hello")
    assert rig.supervisor.send.call_count == 2
    rig.pill.show_state.assert_called_once_with(PillState.ERROR, text=ERROR_MODEL_LOAD_FAILED)
    rig.tray.set_state.assert_called_once_with(TrayState.ERROR)
    assert any(
        record.name == module.__name__ and record.levelno == logging.WARNING
        for record in caplog.records
    )
    assert MARKER not in caplog.text
    assert "/tmp/" not in caplog.text

    # Явный перезапуск снимает ограничение, но ждёт hello нового процесса.
    rig.runtime.restart_worker()
    assert rig.supervisor.send.call_count == 2
    rig.event(type="hello")
    assert rig.supervisor.send.call_count == 3
    rig.pill.show_state.assert_called_with(PillState.LOADING_MODEL)


def test_model_loaded_resets_consecutive_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    rig = Rig(monkeypatch, from_dict({"model_dir": "/tmp/model"}))
    rig.runtime.start()
    rig.event(type="hello")
    rig.event(type="error", code="load-timeout", request_type="model.load")
    rig.tray.set_state.assert_called_once_with(TrayState.ERROR)
    rig.supervisor.generation += 1
    rig.event(type="hello")
    rig.event(type="model.loaded")
    rig.event(type="result", utterance_id="file", text="Проверка связи")
    assert rig.tray.set_state.call_args_list == [call(TrayState.ERROR), call(TrayState.IDLE)]
    rig.supervisor.generation += 1
    rig.event(type="hello")
    assert rig.supervisor.send.call_count == 4
    rig.event(type="error", code="load-timeout", request_type="model.load")
    rig.supervisor.generation += 1
    rig.event(type="hello")
    assert rig.supervisor.send.call_count == 5
    rig.pill.show_state.assert_called_with(PillState.LOADING_MODEL)


def test_press_retries_model_load_after_two_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    rig = Rig(monkeypatch, Settings(extra={"model_dir": "/tmp/model"}))
    rig.runtime.start()
    rig.event(type="hello")
    rig.supervisor.generation += 1
    rig.event(type="error", generation=1, code="load-timeout", request_type="model.load")
    rig.event(type="hello")
    rig.event(type="error", code="engine-failed", request_type="model.load")
    assert rig.runtime._model_load_failures == 2
    assert not rig.runtime._loading_model
    request = rig.supervisor.send.call_args.args[0]
    rig.event(type="hello")
    assert rig.supervisor.send.call_args_list == [call(request, timeout=10.0)] * 2

    rig.hotkey.fsm.press(rig.now)
    assert rig.supervisor.send.call_args_list == [call(request, timeout=10.0)] * 3
    assert rig.runtime._model_load_failures == 0
    assert rig.runtime._loading_model
    assert rig.runtime._model_load_generation == rig.supervisor.generation
    assert_phase(rig.runtime, DictationPhase.IDLE)
    rig.pill.show_state.assert_called_with(PillState.LOADING_MODEL)
    rig.supervisor_factory.assert_called_once()
    rig.supervisor.stop.assert_not_called()

    rig.hotkey.on_state(HotkeyState.RECORDING, "press")
    rig.hotkey.fsm.release(rig.now + 1)
    rig.event(type="hello")
    assert rig.supervisor.send.call_count == 3
    rig.event(type="model.loaded")
    rig.event(type="result", utterance_id="file", text="Проверка связи")
    rig.pill.hide.assert_called_once_with()
    rig.tray.set_state.assert_called_with(TrayState.IDLE)
    rig.hotkey.fsm.escape(rig.now + 2)
    rig.hotkey.fsm.press(rig.now + 3)
    assert rig.supervisor.send.call_args.args[0]["type"] == "record.start"
    assert_phase(rig.runtime, DictationPhase.RECORDING)


@pytest.mark.parametrize("generation", [1, 3])
@pytest.mark.parametrize("kind", ["model.loaded", "error"])
def test_foreign_model_response_is_only_forwarded(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    generation: int,
    kind: str,
) -> None:
    rig = Rig(monkeypatch, from_dict({"model_dir": "/tmp/model"}))
    rig.runtime.start()
    rig.event(type="hello")
    rig.event(type="error", code="load-timeout", request_type="model.load")
    rig.supervisor.generation += 1
    rig.event(type="hello")
    rig.pill.reset_mock()
    rig.tray.reset_mock()
    on_event = Mock()
    monkeypatch.setattr(rig.runtime.orchestrator, "on_worker_event", on_event)
    event = {
        "type": kind,
        "generation": generation,
        "code": "load-timeout",
        "request_type": "model.load",
    }
    caplog.clear()
    with caplog.at_level(logging.INFO, logger=module.__name__):
        rig.supervisor_factory.call_args.kwargs["on_event"](event)
    on_event.assert_called_once_with(event)
    assert on_event.call_args.args[0] is event
    assert rig.pill.mock_calls == []
    assert rig.tray.mock_calls == []
    assert caplog.records == []
    rig.hotkey.fsm.press(rig.now)
    assert rig.supervisor.send.call_count == 2
    rig.pill.show_state.assert_called_once_with(PillState.LOADING_MODEL)
    rig.event(type="error", code="load-timeout", request_type="model.load")
    rig.supervisor.generation += 1
    rig.event(type="hello")
    assert rig.supervisor.send.call_count == 2
    rig.pill.show_state.assert_called_with(PillState.ERROR, text=ERROR_MODEL_LOAD_FAILED)


def test_duplicate_or_stale_hello_does_not_reload_model(monkeypatch: pytest.MonkeyPatch) -> None:
    rig = Rig(monkeypatch, from_dict({"model_dir": "/tmp/model"}))
    rig.runtime.start()
    rig.event(type="hello")
    rig.event(type="hello")
    rig.event(type="hello", generation=0)
    rig.supervisor.send.assert_called_once()
    rig.event(type="model.loaded")
    rig.event(type="result", utterance_id="file", text="Проверка связи")
    rig.event(type="hello")
    assert [c.args[0]["type"] for c in rig.supervisor.send.call_args_list] == [
        "model.load",
        "transcribe.file",
    ]


def test_restart_loads_model_again(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    rig = Rig(monkeypatch, Settings(extra={"model_dir": str(tmp_path)}))
    rig.runtime.start()
    rig.event(type="hello")
    request = rig.supervisor.send.call_args.args[0]
    rig.event(type="model.loaded")
    rig.event(type="result", utterance_id="file", text="Проверка связи")
    new = Mock(state="running", generation=1)
    rig.supervisor_factory.return_value = new
    rig.runtime.restart_worker()
    rig.supervisor.stop.assert_called_once_with()
    new.start.assert_called_once_with()
    new.send.assert_not_called()
    rig.event(type="hello", generation=new.generation)
    new.send.assert_called_once_with(request, timeout=10.0)
    rig.pill.show_state.assert_called_with(PillState.LOADING_MODEL)
    rig.pill.hide.reset_mock()
    rig.supervisor_factory.call_args.kwargs["on_event"](
        {"type": "model.loaded", "generation": new.generation}
    )
    rig.event(type="result", generation=new.generation, utterance_id="file", text="Проверка связи")
    rig.pill.hide.assert_called_once_with()


@pytest.mark.parametrize("restart", [False, True])
def test_hotkey_during_model_load_only_blocks_recording(
    monkeypatch: pytest.MonkeyPatch, restart: bool
) -> None:
    rig = Rig(monkeypatch, Settings(extra={"model_dir": "/tmp/model"}))
    rig.runtime.start()
    rig.event(type="hello")
    if restart:
        rig.event(type="model.loaded")
        rig.event(type="result", utterance_id="file", text="Проверка связи")
        rig.runtime.restart_worker()
        rig.event(type="hello")
    on_state = Mock()
    monkeypatch.setattr(rig.runtime.orchestrator, "on_hotkey_state", on_state)
    rig.pill.show_state.reset_mock()
    rig.supervisor.send.reset_mock()
    rig.hotkey.fsm.press(rig.now)
    rig.hotkey.fsm.release(rig.now + 1)
    rig.pill.show_state.assert_called_once_with(PillState.LOADING_MODEL)
    on_state.assert_called_once_with(HotkeyState.PROCESSING, "release (1.000 с)")
    on_state.reset_mock()
    rig.hotkey.fsm.escape(rig.now + 2)
    on_state.assert_called_once_with(HotkeyState.IDLE, "escape-cancel")
    on_state.reset_mock()
    rig.supervisor.send.assert_not_called()
    assert_phase(rig.runtime, DictationPhase.IDLE)
    rig.event(type="error", code="busy", request_type="audio.open")
    rig.hotkey.on_state(HotkeyState.RECORDING, "press")
    on_state.assert_not_called()
    rig.event(type="model.loaded")
    rig.event(type="result", utterance_id="file", text="Проверка связи")
    rig.hotkey.on_state(HotkeyState.RECORDING, "press")
    on_state.assert_called_once_with(HotkeyState.RECORDING, "press")


@pytest.mark.parametrize("loaded_before_release", [False, True])
def test_model_load_preserves_recording_started_before_hello(
    monkeypatch: pytest.MonkeyPatch, loaded_before_release: bool
) -> None:
    rig = Rig(monkeypatch, Settings(extra={"model_dir": "/tmp/model"}))
    rig.runtime.start()
    rig.hotkey.fsm.press(rig.now)
    assert_phase(rig.runtime, DictationPhase.RECORDING)
    record_start = rig.supervisor.send.call_args.args[0]
    assert record_start["type"] == "record.start"
    rig.event(type="hello")
    assert rig.runtime._loading_model
    rig.pill.show_state.assert_called_once_with(PillState.LISTENING)
    rig.tray.set_state.assert_called_once_with(TrayState.LISTENING)
    rig.guard.set_recording.assert_called_once_with(True)

    rig.hotkey.on_state(HotkeyState.RECORDING, "press")
    assert_phase(rig.runtime, DictationPhase.RECORDING)
    rig.pill.show_state.assert_called_once_with(PillState.LISTENING)
    assert [entry.args[0]["type"] for entry in rig.supervisor.send.call_args_list] == [
        "record.start",
        "model.load",
    ]

    if loaded_before_release:
        rig.event(type="model.loaded")
        rig.event(type="result", utterance_id="file", text="Проверка связи")
        assert_phase(rig.runtime, DictationPhase.RECORDING)
        rig.pill.show_state.assert_called_once_with(PillState.LISTENING)
        rig.tray.set_state.assert_called_once_with(TrayState.LISTENING)
        rig.guard.set_recording.assert_called_once_with(True)
        rig.pill.hide.assert_not_called()

    rig.hotkey.fsm.release(rig.now + 1)
    assert_phase(rig.runtime, DictationPhase.PROCESSING)
    rig.supervisor.send.assert_any_call(
        {"type": "record.stop", "utterance_id": record_start["utterance_id"]}, timeout=None
    )
    if not loaded_before_release:
        rig.event(type="model.loaded")
        rig.event(type="result", utterance_id="file", text="Проверка связи")
    assert not rig.runtime._loading_model
    rig.pill.hide.assert_not_called()
    assert rig.pill.show_state.call_args_list == [
        call(PillState.LISTENING),
        call(PillState.PROCESSING),
    ]
    assert rig.tray.set_state.call_args_list == [
        call(TrayState.LISTENING),
        call(TrayState.PROCESSING),
    ]
    assert rig.guard.set_recording.call_args_list == [call(True), call(False)]


@pytest.mark.parametrize(
    "correlation", [{"request_type": "model.load"}, {"response_type": "model.loaded"}]
)
def test_model_load_error_preserves_recording_and_hotkey_retries(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    correlation: dict[str, str],
) -> None:
    rig = Rig(monkeypatch, Settings(extra={"model_dir": f"/tmp/{MARKER}"}))
    rig.runtime.start()
    rig.event(type="hello")
    rig.event(type="error", code="load-timeout", **correlation)
    assert rig.runtime._model_load_failures == 1
    rig.supervisor.generation += 1
    rig.hotkey.fsm.press(rig.now)
    assert_phase(rig.runtime, DictationPhase.RECORDING)
    rig.pill.show_state.assert_called_with(PillState.LISTENING)
    rig.tray.set_state.assert_called_with(TrayState.LISTENING)
    rig.event(type="hello")
    request = rig.supervisor.send.call_args.args[0]
    assert request["type"] == "model.load"
    assert rig.runtime._loading_model
    rig.pill.reset_mock()
    rig.tray.reset_mock()
    caplog.clear()

    # Ошибка загрузки не относится к текущей фразе.
    rig.event(type="error", code="engine-failed", utterance_id=None, message=MARKER, **correlation)
    assert_phase(rig.runtime, DictationPhase.RECORDING)
    assert rig.pill.mock_calls == []
    assert rig.tray.mock_calls == []
    rig.guard.set_recording.assert_called_once_with(True)
    assert rig.runtime._model_load_failures == 2
    assert not rig.runtime._loading_model
    assert [(record.levelno, record.getMessage()) for record in caplog.records] == [
        (logging.WARNING, "Не удалось загрузить модель")
    ]

    rig.hotkey.fsm.release(rig.now + 1)
    assert_phase(rig.runtime, DictationPhase.PROCESSING)
    rig.event(type="result", text=MARKER)
    rig.paste.assert_called_once_with(MARKER, 4321, PasteMode.AUTO)
    rig.timers[-1].fire()
    assert_phase(rig.runtime, DictationPhase.IDLE)
    assert rig.runtime._model_load_failures == 2
    rig.supervisor.send.reset_mock()

    rig.hotkey.fsm.press(rig.now + 2)
    rig.supervisor.send.assert_called_once_with(request, timeout=10.0)
    assert rig.runtime._model_load_failures == 0
    assert rig.runtime._loading_model
    assert_phase(rig.runtime, DictationPhase.IDLE)
    rig.pill.show_state.assert_called_with(PillState.LOADING_MODEL)


def test_press_during_recording_after_two_model_load_failures_cancels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = Rig(monkeypatch, Settings(extra={"model_dir": "/tmp/model"}))
    rig.runtime.start()
    rig.event(type="hello")
    rig.event(type="error", code="load-timeout", request_type="model.load")
    assert rig.runtime._model_load_failures == 1
    rig.supervisor.generation += 1
    rig.hotkey.fsm.press(rig.now)
    record_start = rig.supervisor.send.call_args.args[0]
    assert record_start["type"] == "record.start"
    rig.event(type="hello")
    rig.event(type="error", code="engine-failed", utterance_id=None, request_type="model.load")
    assert_phase(rig.runtime, DictationPhase.RECORDING)
    assert rig.runtime._model_load_failures == 2
    assert not rig.runtime._loading_model
    rig.supervisor.send.reset_mock()

    rig.hotkey.on_state(HotkeyState.RECORDING, "press")

    rig.supervisor.send.assert_called_once_with(
        {"type": "record.cancel", "utterance_id": record_start["utterance_id"]}, timeout=None
    )
    assert rig.runtime._model_load_failures == 2
    assert not rig.runtime._loading_model


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
    rig.notify.notify_hotkey_not_grabbed.assert_called_once_with("Ctrl+Space")
    rig.notify.notify_tray_unavailable.assert_not_called()
    rig.hotkey.free_candidates.assert_called_once_with(list(DEFAULT_CANDIDATES))
    assert DEFAULT_CANDIDATES[0] in caplog.text
    assert rig.supervisor.state == "running"
    rig.create_notifier.assert_not_called()
    timer = rig.timers[0]
    assert rig.runtime._regrab_timer is timer
    assert timer.interval == 30000 == module.REGRAB_INTERVAL_MS
    assert not timer.single_shot
    assert timer.active and not timer.deleted
    rig.stats.append.assert_called_once_with(
        "hotkey_grab", key_role="text", result="busy", attempts=1
    )


def test_regrab_busy_ticks_are_silent(rig: Rig, caplog: pytest.LogCaptureFixture) -> None:
    rig.grab_code = "busy"
    rig.runtime.start()
    timer = rig.timers[0]
    rig.notify.reset_mock()
    with caplog.at_level(logging.DEBUG):
        caplog.clear()
        for attempt in range(1, 21):
            timer.fire()
            assert rig.runtime._regrab_attempts == attempt
            assert timer.active and not timer.deleted
    assert not caplog.records
    assert not rig.notify.mock_calls
    assert rig.hotkey.grab.call_count == 21
    rig.hotkey.free_candidates.assert_called_once()
    rig.stats.append.assert_called_once_with(
        "hotkey_grab", key_role="text", result="busy", attempts=1
    )


def test_regrab_logs_only_result_changes(rig: Rig, caplog: pytest.LogCaptureFixture) -> None:
    rig.grab_code = "busy"
    rig.runtime.start()
    timer = rig.timers[0]
    rig.notify.reset_mock()
    with caplog.at_level(logging.DEBUG):
        caplog.clear()
        for code in ("busy", "not-grabbed", "not-grabbed", "busy", "busy"):
            rig.grab_code = code
            timer.fire()
    assert len(caplog.records) == 2
    assert "busy → not-grabbed" in caplog.records[0].getMessage()
    assert "not-grabbed → busy" in caplog.records[1].getMessage()
    assert rig.runtime._regrab_attempts == 5
    assert not rig.notify.mock_calls


@pytest.mark.parametrize("busy_attempts", [0, 2, 20])
def test_regrab_success_restores_dictation_once(
    rig: Rig, caplog: pytest.LogCaptureFixture, busy_attempts: int
) -> None:
    rig.grab_code = "busy"
    rig.runtime.start()
    timer = rig.timers[0]
    for _ in range(busy_attempts):
        timer.fire()
    rig.notify.reset_mock()
    rig.tray.set_state.reset_mock()
    rig.grab_code = "ok"
    with caplog.at_level(logging.INFO):
        caplog.clear()
        timer.fire()
        timer.timeout.emit()  # Уже доставленный сигнал после удаления безопасен.
    assert len(caplog.records) == 1
    rig.hotkey.grab.assert_called_with("Ctrl+Space", HotkeyMode.PTT)
    assert rig.hotkey.grab.call_count == busy_attempts + 2
    assert rig.runtime._regrab_timer is None
    assert timer.deleted and not timer.active
    rig.tray.set_state.assert_called_once_with(TrayState.IDLE)
    assert rig.notify.mock_calls == [call.notify_hotkey_regrabbed("Ctrl+Space")]
    assert rig.stats.append.call_args_list == [
        call("hotkey_grab", key_role="text", result="busy", attempts=1),
        call("hotkey_grab", key_role="text", result="regrabbed", attempts=busy_attempts + 1),
    ]
    rig.recognize()
    rig.paste.assert_called_once_with(MARKER, 4321, PasteMode.AUTO)


@pytest.mark.parametrize("blocked", ["selfcheck", "recording"])
@pytest.mark.parametrize("via_apply", [False, True])
def test_regrab_success_preserves_busy_or_failed_tray(
    rig: Rig, blocked: str, via_apply: bool
) -> None:
    rig.grab_code = "busy"
    if blocked == "selfcheck":
        rig.runtime.settings.extra["model_dir"] = "/tmp/model"
    rig.runtime.start()
    if blocked == "selfcheck":
        rig.event(type="hello")
        rig.event(type="model.loaded")
        rig.event(type="result", utterance_id="file", text="")
        rig.tray.set_state.assert_called_with(TrayState.ERROR)
        rig.pill.show_state.assert_called_with(PillState.ERROR, text=ERROR_SELFCHECK_FAILED)
        rig.tray.set_model_recheck_enabled.assert_called_with(True)
        rig.notify.notify_selfcheck_failed.assert_called_once_with()
    else:
        rig.runtime.orchestrator.on_hotkey_state(HotkeyState.RECORDING, "press")
    rig.tray.set_state.reset_mock()
    rig.grab_code = "ok"
    if via_apply:
        rig.runtime.apply_hotkey("Ctrl+Shift+Space", "ptt")
    else:
        rig.timers[0].fire()
    rig.tray.set_state.assert_not_called()
    assert rig.runtime._regrab_timer is None
    if blocked == "selfcheck":
        assert_recording_blocked(rig)
        rig.tray.set_model_recheck_enabled.assert_called_with(True)


@pytest.mark.parametrize("code", ["ok", "busy"])
def test_start_survives_hotkey_stats_failure(rig: Rig, code: ResultCode) -> None:
    rig.grab_code = code
    rig.stats.append.side_effect = OSError(MARKER)
    rig.runtime.start()
    assert rig.runtime.tick_timer is not None
    assert rig.runtime.stats_timer is not None
    assert rig.supervisor.state == "running"
    assert (rig.runtime._regrab_timer is not None) == (code == "busy")


def test_regrab_success_survives_stats_failure(rig: Rig) -> None:
    rig.grab_code = "busy"
    rig.runtime.start()
    rig.stats.append.side_effect = OSError(MARKER)
    rig.grab_code = "ok"
    rig.timers[0].fire()
    assert rig.runtime._regrab_timer is None
    rig.notify.notify_hotkey_regrabbed.assert_called_once_with("Ctrl+Space")
    rig.tray.set_state.assert_called_with(TrayState.IDLE)


def test_regrab_keeps_nokey_when_model_becomes_ready(rig: Rig) -> None:
    rig.grab_code = "busy"
    rig.runtime.start()
    rig.runtime._model_ready()
    rig.tray.set_state.assert_called_with(TrayState.NOKEY)
    assert rig.runtime._regrab_timer is not None


@pytest.mark.parametrize("mode", list(HotkeyMode))
def test_regrab_restores_real_hotkey_manager(
    monkeypatch: pytest.MonkeyPatch, mode: HotkeyMode
) -> None:
    backend = Mock(spec=HotkeyBackend)
    backend.grab_combo.return_value = GrabResult("busy")
    backend.grab_escape.return_value = GrabResult("ok", keycode=9)
    backend.fileno.return_value = 17
    hotkey = HotkeyManager(backend)
    rig = Rig(monkeypatch, Settings(hotkey_mode=mode.value), hotkey_factory=lambda: hotkey)
    rig.runtime.start()
    timer = rig.timers[0]
    timer.fire()
    assert hotkey.last_result.code == "busy"
    backend.grab_combo.return_value = GrabResult("ok", keycode=65, mods=4)
    timer.fire()
    assert hotkey.last_result.ok
    assert timer.deleted and not timer.active
    hotkey.handle_event(HotkeyEvent("KeyPress", 65, 1000, mods=4), rig.now)
    assert_phase(rig.runtime, DictationPhase.RECORDING)
    hotkey.handle_event(HotkeyEvent("KeyRelease", 65, 2000, mods=4), rig.now + 1)
    if mode == HotkeyMode.TOGGLE:
        hotkey.handle_event(HotkeyEvent("KeyPress", 65, 3000, mods=4), rig.now + 2)
    assert_phase(rig.runtime, DictationPhase.PROCESSING)
    rig.event(type="result", text=MARKER)
    rig.paste.assert_called_once_with(MARKER, 4321, PasteMode.AUTO)
    rig.notify.notify_hotkey_regrabbed.assert_called_once_with("Ctrl+Space")
    rig.stats.append.assert_any_call("hotkey_grab", key_role="text", result="regrabbed", attempts=2)


def test_apply_hotkey_stops_regrab_on_success(rig: Rig) -> None:
    rig.grab_code = "busy"
    rig.runtime.start()
    timer = rig.timers[0]
    rig.grab_code = "ok"
    assert rig.runtime.apply_hotkey("Ctrl+Shift+Space", "toggle") == "ok"
    assert rig.runtime._regrab_timer is None
    assert timer.deleted and not timer.active
    rig.hotkey.grab.reset_mock()
    timer.timeout.emit()
    rig.hotkey.grab.assert_not_called()


def test_apply_hotkey_failure_retries_new_settings(rig: Rig) -> None:
    rig.runtime.start()
    rig.grab_code = "busy"
    assert rig.runtime.apply_hotkey("Ctrl+Shift+Space", "toggle") == "busy"
    timer = rig.timers[-1]
    assert rig.runtime._regrab_timer is timer
    assert timer.interval == 30000 and not timer.single_shot
    timer.fire()
    rig.hotkey.grab.assert_called_with("Ctrl+Shift+Space", HotkeyMode.TOGGLE)
    rig.runtime.apply_hotkey("Ctrl+Shift+Space", "toggle")
    assert rig.runtime._regrab_timer is timer
    assert rig.runtime._regrab_attempts == 1
    rig.runtime.apply_hotkey("Win+Space", "ptt")
    assert timer.deleted and not timer.active
    assert rig.runtime._regrab_attempts == 0
    replacement = rig.timers[-1]
    assert rig.runtime._regrab_timer is replacement
    assert replacement is not timer
    rig.grab_code = "ok"
    replacement.fire()
    rig.hotkey.grab.assert_called_with("Win+Space", HotkeyMode.PTT)
    rig.notify.notify_hotkey_regrabbed.assert_called_once_with("Win+Space")
    rig.stats.append.assert_called_with(
        "hotkey_grab", key_role="text", result="regrabbed", attempts=1
    )


def test_shutdown_stops_regrab_and_ignores_late_tick(rig: Rig) -> None:
    rig.grab_code = "busy"
    rig.runtime.start()
    timer = rig.timers[0]
    rig.runtime.shutdown()
    assert rig.runtime._regrab_timer is None
    assert timer.deleted and not timer.active
    rig.hotkey.grab.reset_mock()
    rig.notify.reset_mock()
    timer.timeout.emit()
    rig.runtime._start_regrab("busy")
    assert rig.runtime._regrab_timer is None
    rig.hotkey.grab.assert_not_called()
    assert not rig.notify.mock_calls


def test_candidate_probe_failure_does_not_abort_start(rig: Rig) -> None:
    rig.grab_code = "busy"
    rig.hotkey.free_candidates.side_effect = RuntimeError(MARKER)
    rig.runtime.start()
    assert rig.runtime.tick_timer is not None
    rig.notify.notify_hotkey_not_grabbed.assert_called_once_with("Ctrl+Space")


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
    assert message == {
        "type": "record.start",
        "utterance_id": message["utterance_id"],
        "device": "микрофон",
    }
    assert rig.supervisor.send.call_args.kwargs == {
        "timeout": RECORD_LIMIT_S + RECOGNIZE_TIMEOUT_S,
    }
    assert rig.runtime.record_params() == {
        "device": "микрофон",
        "limit_s": RECORD_LIMIT_S,
    }
    rig.hotkey.fsm.press(rig.now + 1)
    rig.event(type="result", text=MARKER)
    rig.paste.assert_called_once_with(MARKER, 4321, PasteMode.CLIPBOARD_ONLY)
    settings.extra.clear()
    assert rig.runtime.record_params() == {
        "device": None,
        "limit_s": RECORD_LIMIT_S,
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
@pytest.mark.parametrize("session_kind", [SessionKind.KDE, SessionKind.FLY])
@pytest.mark.parametrize("published", [True, False])
def test_copy_last_only_touches_clipboard(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    text: str,
    session_kind: SessionKind,
    published: bool,
) -> None:
    """T-58: публикация для текущего сеанса; отказ безопасен, фраза не раскрывается."""
    rig = Rig(monkeypatch, session_kind=session_kind)
    rig.publish_clipboard.return_value = published
    with caplog.at_level(logging.DEBUG):
        assert rig.runtime.last_text is None
        rig.tray.on_copy_last()
        rig.publish_clipboard.assert_not_called()
        rig.application.clipboard.assert_not_called()
        assert rig.clipboard.mock_calls == []
        assert caplog.records == []
        rig.runtime.start()
        rig.recognize(text)
        rig.tray.on_copy_last()
    rig.publish_clipboard.assert_called_once_with(normalize(text), session_kind=session_kind)
    rig.application.clipboard.assert_not_called()
    rig.clipboard.setMimeData.assert_not_called()
    rig.clipboard.setText.assert_not_called()
    assert rig.clipboard.mock_calls == []
    warnings = [record for record in caplog.records if record.levelno >= logging.WARNING]
    if published:
        assert warnings == []
    else:
        assert len(warnings) == 1
        assert warnings[0].name == module.__name__
        assert warnings[0].levelno == logging.WARNING
        assert warnings[0].getMessage() == "Не удалось скопировать последний текст в буфер обмена"
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
    rig.supervisor_factory.assert_called_with(on_event=runtime._on_worker_event, use_qt=True)
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


@pytest.mark.parametrize("state", ["ok", "broken"])
def test_model_store_constructor_wiring(rig: Rig, state: ModelState) -> None:
    record = ModelRecord(
        id="selected-model",
        revision="r2",
        dir=Path("~/selected/model/r2"),
        layout="selected-layout",
        variant="selected-variant",
        size_bytes=0,
        state=state,
    )

    class Store(ModelStore):
        def current(self) -> ModelRecord:
            return record

    store = Store(root=Path("/unused"))
    runtime = DictationRuntime(
        settings=from_dict({}),
        session_kind=SessionKind.FLY,
        model_store=store,
        supervisor_factory=rig.supervisor_factory,
        pill_factory=rig.pill_factory,
        tray_factory=rig.tray_factory,
        hotkey_factory=Mock(return_value=rig.hotkey),
        stats_factory=Mock(return_value=rig.stats),
        paste_func=rig.paste,
        restore_paste=rig.restore_paste,
        x11_factory=Mock(return_value=rig.x11),
        guard_factory=rig.guard_factory,
        provider_factory=Mock(return_value=rig.provider),
        capture_watchdog_factory=rig.capture_watchdog_factory,
    )
    assert runtime.model_store is store
    runtime.start()
    rig.supervisor.send.assert_not_called()

    runtime._on_worker_event({"type": "hello", "generation": rig.supervisor.generation})

    if state == "ok":
        rig.supervisor.send.assert_called_once()
        request = rig.supervisor.send.call_args.args[0]
        assert request == {
            "type": "model.load",
            "id": record.id,
            "revision": record.revision,
            "dir": str(Path(record.dir).expanduser()),
            "layout": record.layout,
            "variant": record.variant,
            "threads": 2,
            "min_ram_mb": 768,
        }
        ipc.encode(request)
        assert rig.supervisor.send.call_args.kwargs == {"timeout": 10.0}
        rig.pill.show_state.assert_called_once_with(PillState.LOADING_MODEL)
    else:
        rig.supervisor.send.assert_not_called()
        rig.pill.show_state.assert_not_called()
        assert runtime._model_load_failures == 0
    runtime.shutdown()


@pytest.fixture
def checking_rig(monkeypatch: pytest.MonkeyPatch) -> Rig:
    rig = Rig(monkeypatch, from_dict({"model_dir": "/tmp/model"}))
    monkeypatch.setattr(module, "_cpu_model", lambda: "Test CPU")
    rig.runtime.start()
    rig.stats.append.reset_mock()
    rig.event(type="hello")
    rig.runtime._model_load_failures = 1
    rig.event(type="model.loaded", id="gigaam", revision="r3", engine_version="1.24.4", load_ms=123)
    return rig


def assert_selfcheck_log(
    caplog: pytest.LogCaptureFixture, reason: str, attempt: int, *, retry: bool = False
) -> None:
    assert (
        module.__name__,
        logging.INFO if reason == "ok" else logging.WARNING,
        f"самопроверка модели: причина={reason}, попытка={attempt}, повтор={retry}",
    ) in caplog.record_tuples


def assert_recording_blocked(rig: Rig, *, loading: bool = False, offset: int = 0) -> None:
    """Жест хоткея не открывает микрофон и не отправляет даже неудачную команду."""
    sent = list(rig.supervisor.send.call_args_list)
    captures = len(rig.capture_watchdogs)
    rig.hotkey.fsm.press(rig.now + offset)
    assert_phase(rig.runtime, DictationPhase.IDLE)
    if loading:
        rig.pill.show_state.assert_called_with(PillState.LOADING_MODEL)
    else:
        rig.pill.show_state.assert_called_with(PillState.ERROR, text=ERROR_MODEL_NOT_LOADED)
    rig.hotkey.fsm.release(rig.now + offset + 1)
    rig.hotkey.fsm.escape(rig.now + offset + 2)
    assert_phase(rig.runtime, DictationPhase.IDLE)
    assert rig.supervisor.send.call_args_list == sent
    assert len(rig.capture_watchdogs) == captures


def test_selfcheck_waits_for_match_before_ready(
    checking_rig: Rig, smoke_wav: Path, caplog: pytest.LogCaptureFixture
) -> None:
    rig = checking_rig
    assert module.SELFCHECK_TIMEOUT_S == 3.0
    assert module.SELFCHECK_WATCHDOG_MS == 3000
    rig.pill.show_state.assert_called_with(PillState.LOADING_MODEL)
    rig.tray.set_model_recheck_enabled.assert_called_with(False)
    assert rig.runtime._loading_model
    assert rig.runtime._model_load_failures == 1
    rig.supervisor.send.assert_called_with(
        {"type": "transcribe.file", "path": str(smoke_wav)}, timeout=module.SELFCHECK_TIMEOUT_S
    )
    timer = rig.timers[-1]
    assert timer.interval == module.SELFCHECK_WATCHDOG_MS
    rig.pill.show_state.assert_called_once_with(PillState.LOADING_MODEL)
    rig.pill.hide.assert_not_called()
    rig.tray.set_state.assert_not_called()
    rig.stats.append.assert_not_called()
    assert_recording_blocked(rig, loading=True)
    with caplog.at_level(logging.DEBUG):
        rig.event(type="result", utterance_id="file", text=f"ПРОВЕРКА {MARKER}")
    rig.tray.set_model_recheck_enabled.assert_called_with(False)
    assert not rig.runtime._loading_model
    assert rig.runtime._model_load_failures == 0
    assert timer.deleted and not timer.active
    assert not rig.runtime.timers
    rig.pill.hide.assert_called_once_with()
    rig.tray.set_state.assert_called_once_with(TrayState.IDLE)
    rig.stats.append.assert_called_once_with(
        "model_selfcheck",
        model_id="gigaam",
        revision="r3",
        engine_version="1.24.4",
        cpu_model="Test CPU",
        result="ok",
    )
    assert "модель загружена за 123 мс" in caplog.text
    assert_selfcheck_log(caplog, "ok", 1)
    assert MARKER not in caplog.text
    assert MARKER not in repr(rig.stats.mock_calls + rig.notify.mock_calls + rig.pill.mock_calls)
    assert rig.runtime.last_text is None
    rig.paste.assert_not_called()
    rig.notify.notify_selfcheck_failed.assert_not_called()
    timer.timeout.emit()
    rig.event(type="result", utterance_id="file", text="")
    rig.stats.append.assert_called_once()
    rig.hotkey.fsm.press(rig.now)
    assert_phase(rig.runtime, DictationPhase.RECORDING)
    assert rig.supervisor.send.call_args.args[0]["type"] == "record.start"


@pytest.mark.parametrize(
    ("event", "reason"),
    [
        ({"type": "result", "text": MARKER}, "no-match"),
        ({"type": "result", "text": ""}, "no-match"),
        ({"type": "result", "text": None}, "no-match"),
        ({"type": "error", "code": "engine-failed", "message": MARKER}, "worker-error"),
        ({"type": "error", "request_type": "transcribe.file", "message": MARKER}, "worker-error"),
    ],
)
def test_selfcheck_failure_blocks_dictation_once(
    checking_rig: Rig,
    caplog: pytest.LogCaptureFixture,
    event: dict[str, object],
    reason: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = checking_rig
    on_event = Mock()
    monkeypatch.setattr(rig.runtime.orchestrator, "on_worker_event", on_event)
    timer = rig.timers[-1]
    with caplog.at_level(logging.DEBUG):
        correlation = {} if "request_type" in event else {"utterance_id": "file"}
        rig.event(**correlation, **event)
    rig.tray.set_model_recheck_enabled.assert_called_with(True)
    assert not rig.runtime._loading_model
    assert rig.runtime._model_load_failures == 1
    assert timer.deleted and not timer.active
    assert not rig.runtime.timers
    rig.tray.set_state.assert_called_once_with(TrayState.ERROR)
    rig.pill.show_state.assert_called_with(PillState.ERROR, text=ERROR_SELFCHECK_FAILED)
    rig.pill.hide.assert_not_called()
    rig.notify.notify_selfcheck_failed.assert_called_once_with()
    rig.stats.append.assert_called_once_with(
        "model_selfcheck",
        model_id="gigaam",
        revision="r3",
        engine_version="1.24.4",
        cpu_model="Test CPU",
        result="fail",
    )
    assert rig.trace.count("transcribe.file") == 1
    assert_selfcheck_log(caplog, reason, 1)
    rig.supervisor.stop.assert_not_called()
    timer.timeout.emit()
    rig.event(type="result", utterance_id="file", text="проверка")
    rig.event(type="error", utterance_id="file", message=MARKER)
    rig.tray.set_model_recheck_enabled.assert_called_with(True)
    on_event.assert_not_called()
    rig.notify.notify_selfcheck_failed.assert_called_once_with()
    rig.stats.append.assert_called_once()
    for offset in (0, 3):
        assert_recording_blocked(rig, offset=offset)
    assert "record.start" not in rig.trace
    assert_phase(rig.runtime, DictationPhase.IDLE)
    rig.paste.assert_not_called()
    assert rig.runtime.last_text is None
    assert MARKER not in caplog.text
    assert "модель загружена" not in caplog.text
    assert MARKER not in repr(rig.stats.mock_calls + rig.notify.mock_calls + rig.pill.mock_calls)


def interrupt_selfcheck(rig: Rig, outcome: str) -> None:
    """Доставляет оба вида дедлайна супервизора, сторожа или отмену."""
    if outcome == "watchdog":
        rig.timers[-1].fire()
    elif outcome == "cancelled":
        rig.event(type="cancelled", utterance_id="file")
    else:
        generation = rig.supervisor.generation
        if outcome == "load-timeout":
            # Супервизор меняет поколение до callback с ошибкой старого процесса.
            rig.supervisor.generation += 1
        rig.event(
            type="error",
            request_type="transcribe.file",
            code=outcome,
            message=MARKER,
            generation=generation,
        )


def resume_selfcheck_retry(rig: Rig) -> FakeTimer:
    rig.pill.show_state.assert_called_with(PillState.LOADING_MODEL)
    rig.tray.set_model_recheck_enabled.assert_called_with(False)
    assert rig.runtime._loading_model
    assert_recording_blocked(rig, loading=True)
    rig.event(type="hello")
    rig.event(type="hello")
    rig.event(type="model.loaded", id="gigaam", revision="r3", engine_version="1.24.4", load_ms=123)
    rig.event(type="model.loaded")  # Дубликат не запускает третье распознавание.
    rig.pill.show_state.assert_called_with(PillState.LOADING_MODEL)
    rig.tray.set_model_recheck_enabled.assert_called_with(False)
    # Номер попытки проверяется журналом её исхода в вызывающем тесте.
    assert rig.trace.count("model.load") == 2
    assert rig.trace.count("transcribe.file") == 2
    assert_recording_blocked(rig, loading=True)
    timer = rig.timers[-1]
    assert timer.active and not timer.deleted
    assert timer.interval == 3000
    return timer


@pytest.mark.parametrize("outcome", ["watchdog", "timeout", "load-timeout", "cancelled"])
def test_selfcheck_transient_failure_retries_then_succeeds(
    checking_rig: Rig, caplog: pytest.LogCaptureFixture, outcome: str
) -> None:
    rig = checking_rig
    timer = rig.timers[-1]
    with caplog.at_level(logging.DEBUG):
        interrupt_selfcheck(rig, outcome)
    reason = "cancelled" if outcome == "cancelled" else "timeout"
    assert_selfcheck_log(caplog, reason, 1, retry=True)
    rig.pill.show_state.assert_called_with(PillState.LOADING_MODEL)
    rig.tray.set_model_recheck_enabled.assert_called_with(False)
    assert rig.runtime._loading_model
    assert timer.deleted and not timer.active
    assert not rig.runtime.timers
    assert rig.supervisor.generation == 2
    assert rig.supervisor.stop.call_count == (0 if outcome == "load-timeout" else 1)
    assert rig.supervisor_factory.call_count == (1 if outcome == "load-timeout" else 2)
    assert rig.trace.count("transcribe.file") == 1
    rig.notify.notify_selfcheck_failed.assert_not_called()
    rig.pill.hide.assert_not_called()
    rig.tray.set_state.assert_not_called()
    rig.stats.append.assert_called_once_with(
        "model_selfcheck",
        model_id="gigaam",
        revision="r3",
        engine_version="1.24.4",
        cpu_model="Test CPU",
        result="fail",
    )
    assert_recording_blocked(rig, loading=True)
    rig.event(type="model.loaded", generation=1)
    assert rig.trace.count("transcribe.file") == 1
    retry_timer = resume_selfcheck_retry(rig)
    timer.timeout.emit()
    for kind in ("result", "cancelled", "error"):
        rig.event(type=kind, utterance_id="file", generation=1, text="проверка", code="timeout")
    rig.pill.show_state.assert_called_with(PillState.LOADING_MODEL)
    rig.tray.set_model_recheck_enabled.assert_called_with(False)
    assert retry_timer.active and not retry_timer.deleted
    assert rig.stats.append.call_count == 1
    assert_recording_blocked(rig, loading=True)
    with caplog.at_level(logging.DEBUG):
        rig.event(type="result", utterance_id="file", text=f"связи {MARKER}")
    rig.tray.set_model_recheck_enabled.assert_called_with(False)
    assert not rig.runtime._loading_model
    assert rig.runtime._model_load_failures == 0
    assert retry_timer.deleted and not retry_timer.active
    assert not rig.runtime.timers
    rig.pill.hide.assert_called_once_with()
    rig.tray.set_state.assert_called_once_with(TrayState.IDLE)
    rig.notify.notify_selfcheck_failed.assert_not_called()
    assert rig.stats.append.call_count == 2
    rig.stats.append.assert_called_with(
        "model_selfcheck",
        model_id="gigaam",
        revision="r3",
        engine_version="1.24.4",
        cpu_model="Test CPU",
        result="ok",
    )
    retry_timer.timeout.emit()
    assert_selfcheck_log(caplog, "ok", 2)
    rig.event(type="result", utterance_id="file", text="")
    assert rig.stats.append.call_count == 2
    rig.paste.assert_not_called()
    assert rig.runtime.last_text is None
    assert MARKER not in caplog.text
    assert MARKER not in repr(rig.stats.mock_calls + rig.notify.mock_calls + rig.pill.mock_calls)
    rig.hotkey.fsm.press(rig.now)
    assert_phase(rig.runtime, DictationPhase.RECORDING)
    assert rig.supervisor.send.call_args.args[0]["type"] == "record.start"


@pytest.mark.parametrize("first", ["watchdog", "timeout", "load-timeout", "cancelled"])
@pytest.mark.parametrize("second", ["watchdog", "timeout", "load-timeout", "cancelled"])
def test_selfcheck_second_transient_failure_blocks_without_third_attempt(
    checking_rig: Rig,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    first: str,
    second: str,
) -> None:
    rig = checking_rig
    interrupt_selfcheck(rig, first)
    timer = resume_selfcheck_retry(rig)
    rig.pill.show_state.reset_mock()
    on_event = Mock()
    monkeypatch.setattr(rig.runtime.orchestrator, "on_worker_event", on_event)
    with caplog.at_level(logging.DEBUG):
        interrupt_selfcheck(rig, second)
    reason = "cancelled" if second == "cancelled" else "timeout"
    assert_selfcheck_log(caplog, "cancelled" if first == "cancelled" else "timeout", 1, retry=True)
    assert_selfcheck_log(caplog, reason, 2)
    rig.tray.set_model_recheck_enabled.assert_called_with(True)
    assert not rig.runtime._loading_model
    assert rig.runtime._model_load_failures == 1
    assert timer.deleted and not timer.active
    assert not rig.runtime.timers
    rig.tray.set_state.assert_called_once_with(TrayState.ERROR)
    rig.pill.show_state.assert_called_once_with(PillState.ERROR, text=ERROR_SELFCHECK_FAILED)
    rig.pill.hide.assert_not_called()
    rig.notify.notify_selfcheck_failed.assert_called_once_with()
    assert rig.stats.append.call_args_list == [
        call(
            "model_selfcheck",
            model_id="gigaam",
            revision="r3",
            engine_version="1.24.4",
            cpu_model="Test CPU",
            result="fail",
        )
        for _ in (first, second)
    ]
    timer.timeout.emit()
    rig.event(type="result", utterance_id="file", text="проверка")
    rig.event(type="error", utterance_id="file", code="timeout", message=MARKER)
    on_event.assert_not_called()
    rig.event(type="hello")
    rig.event(type="model.loaded")
    for offset in (0, 3):
        assert_recording_blocked(rig, offset=offset)
    rig.tray.set_model_recheck_enabled.assert_called_with(True)
    assert rig.trace.count("transcribe.file") == 2
    assert rig.trace.count("model.load") == 2
    assert "record.start" not in rig.trace
    assert_phase(rig.runtime, DictationPhase.IDLE)
    rig.paste.assert_not_called()
    assert rig.runtime.last_text is None
    assert rig.stats.append.call_count == 2
    rig.notify.notify_selfcheck_failed.assert_called_once_with()
    assert MARKER not in caplog.text
    assert "модель загружена" not in caplog.text
    assert MARKER not in repr(rig.stats.mock_calls + rig.notify.mock_calls + rig.pill.mock_calls)


@pytest.mark.parametrize(
    ("event", "reason"),
    [
        ({"type": "result", "text": ""}, "no-match"),
        ({"type": "error", "code": "engine-failed", "message": MARKER}, "worker-error"),
    ],
)
def test_selfcheck_retry_engine_failure_blocks_immediately(
    checking_rig: Rig, caplog: pytest.LogCaptureFixture, event: dict[str, object], reason: str
) -> None:
    rig = checking_rig
    interrupt_selfcheck(rig, "cancelled")
    timer = resume_selfcheck_retry(rig)
    rig.event(utterance_id="file", **event)
    rig.tray.set_model_recheck_enabled.assert_called_with(True)
    assert timer.deleted and not timer.active
    assert not rig.runtime.timers
    assert rig.stats.append.call_count == 2
    assert rig.stats.append.call_args.kwargs["result"] == "fail"
    assert_selfcheck_log(caplog, reason, 2)
    assert rig.trace.count("transcribe.file") == 2
    rig.tray.set_state.assert_called_once_with(TrayState.ERROR)
    rig.pill.show_state.assert_called_with(PillState.ERROR, text=ERROR_SELFCHECK_FAILED)
    rig.pill.hide.assert_not_called()
    rig.notify.notify_selfcheck_failed.assert_called_once_with()
    assert_recording_blocked(rig)
    assert "record.start" not in rig.trace
    assert_phase(rig.runtime, DictationPhase.IDLE)
    assert MARKER not in caplog.text


@pytest.mark.parametrize("operation", ["restart", "start", "load", "load-send", "send", "no-wav"])
def test_selfcheck_retry_setup_failure_blocks(
    checking_rig: Rig, smoke_wav: Path, caplog: pytest.LogCaptureFixture, operation: str
) -> None:
    rig = checking_rig
    if operation == "restart":
        rig.supervisor_factory.side_effect = RuntimeError(MARKER)
    with caplog.at_level(logging.DEBUG):
        interrupt_selfcheck(rig, "cancelled")
        if operation == "start":
            rig.event(type="error", code="worker-start", message=MARKER)
        elif operation in ("load", "load-send", "send", "no-wav"):
            if operation == "load-send":
                rig.supervisor.send.side_effect = RuntimeError(MARKER)
            rig.event(type="hello")
            if operation == "load":
                rig.event(type="error", request_type="model.load", code="engine-failed")
            elif operation != "load-send":
                if operation == "send":
                    rig.supervisor.send.side_effect = RuntimeError(MARKER)
                else:
                    smoke_wav.unlink()
                rig.event(type="model.loaded")
    rig.tray.set_model_recheck_enabled.assert_called_with(True)
    assert not rig.runtime._loading_model
    assert not rig.runtime.timers
    assert rig.stats.append.call_count == 2
    reason = {
        "restart": "worker-error",
        "start": "worker-error",
        "load": "load-failed",
        "load-send": "load-failed",
        "send": "worker-error",
        "no-wav": "no-wav",
    }[operation]
    assert_selfcheck_log(caplog, reason, 2)
    assert rig.stats.append.call_args.kwargs["result"] == "fail"
    rig.tray.set_state.assert_called_once_with(TrayState.ERROR)
    rig.pill.show_state.assert_called_with(PillState.ERROR, text=ERROR_SELFCHECK_FAILED)
    rig.notify.notify_selfcheck_failed.assert_called_once_with()
    assert_recording_blocked(rig)
    assert "record.start" not in rig.trace
    assert MARKER not in caplog.text


@pytest.mark.parametrize("outcome", ["watchdog", "timeout", "load-timeout", "cancelled"])
def test_selfcheck_retry_uses_real_supervisor_correlation(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, outcome: str
) -> None:
    caplog.set_level(logging.INFO, logger=module.__name__)
    # Настоящие send/_accept/_expire проверяют запрет повтора id "file".
    # Подменяем только запуск процесса и запись в сокет, без Qt и движка.
    monkeypatch.setattr(
        WorkerSupervisor, "_launch", lambda supervisor: setattr(supervisor, "state", "running")
    )
    monkeypatch.setattr(WorkerSupervisor, "_flush", lambda supervisor: None)
    rig.runtime.settings.extra["model_dir"] = "/tmp/model"
    workers: list[WorkerSupervisor] = []

    def factory(**kwargs: Any) -> WorkerSupervisor:
        supervisor = WorkerSupervisor(
            on_event=kwargs["on_event"], use_qt=False, clock=lambda: rig.now
        )
        workers.append(supervisor)
        return supervisor

    rig.runtime._supervisor_factory = factory
    first = factory(on_event=rig.runtime._on_worker_event)
    rig.runtime.supervisor = first
    rig.runtime.start()
    first._accept({"type": "hello"}, 1)
    first._accept({"type": "model.loaded"}, 1)
    if outcome == "watchdog":
        rig.timers[-1].fire()
    elif outcome == "cancelled":
        first._accept({"type": "cancelled", "utterance_id": "file"}, 1)
    else:
        if outcome == "timeout":
            first._accept({"type": "pong"}, 1)  # Живой воркер: timeout без рестарта.
        rig.now += 3
        first._expire()
    second = rig.runtime.supervisor
    assert second.generation == 2
    assert len(workers) == (1 if outcome == "load-timeout" else 2)
    send = Mock(wraps=second.send)
    monkeypatch.setattr(second, "send", send)
    second._accept({"type": "hello"}, 2)
    second._accept({"type": "model.loaded"}, 2)
    rig.pill.show_state.assert_called_with(PillState.LOADING_MODEL)
    rig.tray.set_model_recheck_enabled.assert_called_with(False)
    assert [entry.args[0]["type"] for entry in send.call_args_list] == [
        "model.load",
        "transcribe.file",
    ]
    rig.pill.hide.assert_not_called()
    rig.hotkey.fsm.press(rig.now)
    assert_phase(rig.runtime, DictationPhase.IDLE)
    rig.hotkey.fsm.release(rig.now + 1)
    rig.hotkey.fsm.escape(rig.now + 2)
    assert send.call_count == 2
    second._accept({"type": "result", "utterance_id": "file", "text": "проверка"}, 2)
    rig.tray.set_model_recheck_enabled.assert_called_with(False)
    assert not rig.runtime._loading_model
    assert rig.stats.append.call_args.kwargs["result"] == "ok"
    assert_selfcheck_log(
        caplog, "cancelled" if outcome == "cancelled" else "timeout", 1, retry=True
    )
    assert_selfcheck_log(caplog, "ok", 2)
    rig.tray.set_state.assert_called_with(TrayState.IDLE)
    rig.pill.hide.assert_called_once_with()
    rig.notify.notify_selfcheck_failed.assert_not_called()
    rig.hotkey.fsm.press(rig.now + 3)
    assert_phase(rig.runtime, DictationPhase.RECORDING)
    assert send.call_args.args[0]["type"] == "record.start"
    rig.runtime.shutdown()


@pytest.mark.parametrize(
    "reason", ["ok", "no-match", "worker-error", "no-wav", "timeout", "cancelled"]
)
def test_selfcheck_outcome_is_logged_and_prd_stats_roundtrip(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    smoke_wav: Path,
    caplog: pytest.LogCaptureFixture,
    reason: str,
) -> None:
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(stats_module, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(module, "_cpu_model", lambda: "Test CPU")
    rig = Rig(monkeypatch, Settings(extra={"model_dir": "/tmp/model"}))
    rig.runtime.start()
    stats = stats_module.Stats()
    rig.runtime.stats = stats
    rig.event(type="hello")
    if reason == "no-wav":
        smoke_wav.unlink()
    rig.event(type="model.loaded", id="gigaam", revision="r3", engine_version="1.24.4")
    if reason in ("timeout", "cancelled"):
        interrupt_selfcheck(rig, reason)
        resume_selfcheck_retry(rig)
        interrupt_selfcheck(rig, reason)
    elif reason == "worker-error":
        rig.event(type="error", utterance_id="file", code="engine-failed", message=MARKER)
    elif reason != "no-wav":
        rig.event(
            type="result",
            utterance_id="file",
            text=f"проверка {MARKER}" if reason == "ok" else MARKER,
        )
    expected = [
        {
            "type": "model_selfcheck",
            "model_id": "gigaam",
            "revision": "r3",
            "engine_version": "1.24.4",
            "cpu_model": "Test CPU",
            "result": "ok" if reason == "ok" else "fail",
        }
        for _ in range(2 if reason in ("timeout", "cancelled") else 1)
    ]
    stats.flush()
    contents = (tmp_path / "stats.json").read_text()
    assert MARKER not in contents
    stored = json.loads(contents)["events"]
    assert [
        {key: value for key, value in event.items() if key != "ts"} for event in stored
    ] == expected
    assert stats_module.Stats().events() == stats.events() == stored
    assert all(isinstance(event["ts"], float) for event in stored)
    assert "неизвестное поле статистики отброшено" not in caplog.text
    assert MARKER not in caplog.text
    assert_selfcheck_log(caplog, reason, 1, retry=reason in ("timeout", "cancelled"))
    if reason in ("timeout", "cancelled"):
        assert_selfcheck_log(caplog, reason, 2)
    assert not rig.runtime._loading_model
    if reason == "ok":
        rig.tray.set_state.assert_called_with(TrayState.IDLE)
        rig.pill.hide.assert_called_once_with()
        rig.notify.notify_selfcheck_failed.assert_not_called()
        rig.hotkey.fsm.press(rig.now)
        assert_phase(rig.runtime, DictationPhase.RECORDING)
        assert "record.start" in rig.trace
    else:
        rig.tray.set_state.assert_called_with(TrayState.ERROR)
        rig.pill.show_state.assert_called_with(PillState.ERROR, text=ERROR_SELFCHECK_FAILED)
        rig.pill.hide.assert_not_called()
        rig.notify.notify_selfcheck_failed.assert_called_once_with()
        rig.hotkey.fsm.press(rig.now)
        assert_phase(rig.runtime, DictationPhase.IDLE)
        assert "record.start" not in rig.trace
    rig.runtime.shutdown()


def test_selfcheck_missing_reference_blocks_dictation_with_warning(
    monkeypatch: pytest.MonkeyPatch, smoke_wav: Path, caplog: pytest.LogCaptureFixture
) -> None:
    smoke_wav.unlink()
    rig = Rig(monkeypatch, Settings(extra={"model_dir": "/tmp/model"}))
    monkeypatch.setattr(module, "_cpu_model", lambda: "Test CPU")
    rig.runtime.start()
    rig.event(type="hello")
    with caplog.at_level(logging.INFO):
        rig.event(type="model.loaded", load_ms=42)
    assert (logging.WARNING, "эталон самопроверки не найден") in [
        (record.levelno, record.getMessage()) for record in caplog.records
    ]
    assert "модель загружена" not in caplog.text
    assert_selfcheck_log(caplog, "no-wav", 1)
    rig.tray.set_model_recheck_enabled.assert_called_with(True)
    assert not rig.runtime._loading_model
    rig.pill.hide.assert_not_called()
    rig.pill.show_state.assert_called_with(PillState.ERROR, text=ERROR_SELFCHECK_FAILED)
    rig.tray.set_state.assert_called_once_with(TrayState.ERROR)
    rig.notify.notify_selfcheck_failed.assert_called_once_with()
    assert "transcribe.file" not in rig.trace
    assert not rig.runtime.timers
    assert rig.stats.append.call_args_list == [
        call("hotkey_grab", key_role="text", result="ok", attempts=1),
        call(
            "model_selfcheck",
            model_id="",
            revision="",
            engine_version="",
            cpu_model="Test CPU",
            result="fail",
        ),
    ]
    rig.event(type="model.loaded", load_ms=42)
    rig.event(type="result", utterance_id="file", text="проверка")
    for offset in (0, 3):
        assert_recording_blocked(rig, offset=offset)
    assert "record.start" not in rig.trace
    assert_phase(rig.runtime, DictationPhase.IDLE)
    assert rig.stats.append.call_count == 2
    rig.notify.notify_selfcheck_failed.assert_called_once_with()
    rig.paste.assert_not_called()


def test_selfcheck_send_error_is_private_failure(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    rig = Rig(monkeypatch, Settings(extra={"model_dir": "/tmp/model"}))
    rig.runtime.start()
    rig.event(type="hello")
    rig.supervisor.send.side_effect = RuntimeError(MARKER)
    with caplog.at_level(logging.DEBUG):
        rig.event(type="model.loaded")
    rig.tray.set_model_recheck_enabled.assert_called_with(True)
    assert rig.runtime._model_load_failures == 0
    assert not rig.runtime.timers
    rig.notify.notify_selfcheck_failed.assert_called_once_with()
    assert rig.stats.append.call_args.kwargs["result"] == "fail"
    assert_selfcheck_log(caplog, "worker-error", 1)
    assert rig.stats.append.call_args.kwargs["engine_version"] == ""
    rig.tray.set_state.assert_called_with(TrayState.ERROR)
    rig.pill.show_state.assert_called_with(PillState.ERROR, text=ERROR_SELFCHECK_FAILED)
    rig.pill.hide.assert_not_called()
    assert_recording_blocked(rig)
    assert "record.start" not in rig.trace
    assert_phase(rig.runtime, DictationPhase.IDLE)
    assert MARKER not in caplog.text


@pytest.mark.parametrize("action", ["restart", "reload", "shutdown"])
def test_selfcheck_reset_cancels_watchdog(
    checking_rig: Rig, caplog: pytest.LogCaptureFixture, action: str
) -> None:
    rig = checking_rig
    timer = rig.timers[-1]
    if action == "shutdown":
        rig.runtime.shutdown()
    elif action == "restart":
        rig.runtime.restart_worker()
    else:
        rig.supervisor.generation += 1
        rig.event(type="hello")
    assert timer.deleted and not timer.active
    assert all(timer.deleted and not timer.active for timer in rig.timers[2:])
    assert not rig.runtime.timers
    if action != "shutdown":
        rig.tray.set_model_recheck_enabled.assert_called_with(False)
        assert rig.runtime._loaded_model == {}
        assert rig.runtime._model_load_ms is None
    rig.pill.hide.reset_mock()
    sent = list(rig.supervisor.send.call_args_list)
    tray_calls = list(rig.tray.mock_calls)
    pill_calls = list(rig.pill.mock_calls)
    timer.timeout.emit()
    rig.event(type="result", utterance_id="file", generation=1, text="проверка")
    rig.stats.append.assert_not_called()
    rig.notify.notify_selfcheck_failed.assert_not_called()
    rig.pill.hide.assert_not_called()
    assert rig.supervisor.send.call_args_list == sent
    assert rig.tray.mock_calls == tray_calls
    assert rig.pill.mock_calls == pill_calls
    if action != "shutdown":
        # Сброс разрешает новую проверку с первой попытки, старый сторож ей не мешает.
        if action == "restart":
            rig.event(type="hello")
        assert rig.trace.count("transcribe.file") == 1
        assert_recording_blocked(rig, loading=True)
        rig.event(type="model.loaded")
        assert rig.trace.count("transcribe.file") == 2
        with caplog.at_level(logging.INFO):
            rig.event(type="result", utterance_id="file", text="проверка")
        assert_selfcheck_log(caplog, "ok", 1)
        rig.tray.set_state.assert_called_with(TrayState.IDLE)
        rig.pill.hide.assert_called_once_with()
        rig.hotkey.fsm.press(rig.now)
        assert_phase(rig.runtime, DictationPhase.RECORDING)
        assert rig.supervisor.send.call_args.args[0]["type"] == "record.start"


@pytest.mark.parametrize("running", [False, True])
def test_selfcheck_shutdown_during_retry_cancels_all_work(checking_rig: Rig, running: bool) -> None:
    rig = checking_rig
    interrupt_selfcheck(rig, "timeout")
    if running:
        resume_selfcheck_retry(rig)
    rig.runtime.shutdown()
    assert all(timer.deleted and not timer.active for timer in rig.timers[2:])
    assert not rig.runtime.timers
    rig.pill.hide.reset_mock()
    sent = list(rig.supervisor.send.call_args_list)
    tray_calls = list(rig.tray.mock_calls)
    pill_calls = list(rig.pill.mock_calls)
    starts = rig.supervisor.start.call_count
    for timer in rig.timers:
        timer.timeout.emit()
    rig.event(type="hello")
    rig.event(type="model.loaded")
    rig.event(type="result", utterance_id="file", text="проверка")
    rig.event(type="error", utterance_id="file", code="timeout")
    assert rig.stats.append.call_count == 1
    assert rig.trace.count("transcribe.file") == (2 if running else 1)
    rig.notify.notify_selfcheck_failed.assert_not_called()
    rig.pill.hide.assert_not_called()
    assert rig.supervisor.send.call_args_list == sent
    assert rig.supervisor.start.call_count == starts
    assert rig.tray.mock_calls == tray_calls
    assert rig.pill.mock_calls == pill_calls


def test_selfcheck_tray_recheck_restores_retry_budget(
    checking_rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger=module.__name__)
    rig = checking_rig
    interrupt_selfcheck(rig, "timeout")
    resume_selfcheck_retry(rig)
    interrupt_selfcheck(rig, "timeout")
    rig.tray.set_state.assert_called_with(TrayState.ERROR)
    rig.pill.show_state.assert_called_with(PillState.ERROR, text=ERROR_SELFCHECK_FAILED)
    rig.tray.set_model_recheck_enabled.assert_called_with(True)
    assert_recording_blocked(rig)
    rig.tray.on_model_recheck()
    rig.tray.set_model_recheck_enabled.assert_called_with(False)
    rig.tray.set_state.assert_called_with(TrayState.IDLE)
    assert_recording_blocked(rig, loading=True)
    assert rig.trace.count("transcribe.file") == 2
    caplog.clear()
    rig.event(type="hello")
    rig.event(type="model.loaded")
    interrupt_selfcheck(rig, "timeout")
    rig.pill.show_state.assert_called_with(PillState.LOADING_MODEL)
    rig.tray.set_model_recheck_enabled.assert_called_with(False)
    assert rig.stats.append.call_count == 3
    assert_selfcheck_log(caplog, "timeout", 1, retry=True)
    assert_recording_blocked(rig, loading=True)
    assert rig.trace.count("transcribe.file") == 3
    rig.event(type="hello")
    rig.event(type="model.loaded")
    assert rig.trace.count("transcribe.file") == 4
    rig.event(type="result", utterance_id="file", text="проверка")
    rig.tray.set_model_recheck_enabled.assert_called_with(False)
    rig.tray.set_state.assert_called_with(TrayState.IDLE)
    assert rig.stats.append.call_count == 4
    rig.pill.hide.assert_called_once_with()
    assert_selfcheck_log(caplog, "ok", 2)
    rig.hotkey.fsm.press(rig.now)
    assert_phase(rig.runtime, DictationPhase.RECORDING)
    assert rig.supervisor.send.call_args.args[0]["type"] == "record.start"


@pytest.mark.parametrize("exhausted", [False, True])
@pytest.mark.parametrize("success", [False, True])
def test_selfcheck_tray_recheck_recovers_or_blocks_again(
    checking_rig: Rig,
    smoke_wav: Path,
    caplog: pytest.LogCaptureFixture,
    exhausted: bool,
    success: bool,
) -> None:
    caplog.set_level(logging.INFO, logger=module.__name__)
    rig = checking_rig
    if exhausted:
        interrupt_selfcheck(rig, "timeout")
        resume_selfcheck_retry(rig)
        interrupt_selfcheck(rig, "timeout")
    else:
        rig.event(type="result", utterance_id="file", text="")
    rig.tray.set_state.assert_called_with(TrayState.ERROR)
    rig.pill.show_state.assert_called_with(PillState.ERROR, text=ERROR_SELFCHECK_FAILED)
    rig.tray.set_model_recheck_enabled.assert_called_with(True)
    sent = list(rig.supervisor.send.call_args_list)
    rig.hotkey.fsm.press(rig.now)
    assert_phase(rig.runtime, DictationPhase.IDLE)
    rig.pill.show_state.assert_called_with(PillState.ERROR, text=ERROR_MODEL_NOT_LOADED)
    rig.hotkey.fsm.release(rig.now + 1)
    assert rig.supervisor.send.call_args_list == sent
    # PROCESSING должен сбросить именно жест меню, а не тестовый Escape.
    assert rig.hotkey.fsm.state == HotkeyState.PROCESSING
    assert "record.start" not in rig.trace

    generation = rig.supervisor.generation
    rig.supervisor.stop.reset_mock()
    rig.supervisor.start.reset_mock()
    rig.supervisor.send.reset_mock()
    rig.tray.on_model_recheck()
    caplog.clear()
    rig.supervisor.send.assert_not_called()
    assert rig.runtime._model_load_failures == 0
    assert rig.runtime._loading_model
    assert rig.hotkey.fsm.state == HotkeyState.IDLE
    rig.tray.set_model_recheck_enabled.assert_called_with(False)
    rig.tray.set_state.assert_called_with(TrayState.IDLE)
    rig.pill.show_state.assert_called_with(PillState.LOADING_MODEL)
    rig.supervisor.stop.assert_called_once_with()
    rig.supervisor.start.assert_called_once_with()
    assert rig.supervisor.generation == generation + 1

    # Повторный сигнал меню и поздний ответ старой проверки ничего не меняют.
    rig.tray.on_model_recheck()
    rig.event(type="result", utterance_id="file", generation=generation, text="проверка")
    rig.supervisor.start.assert_called_once_with()
    assert_recording_blocked(rig, loading=True, offset=3)
    rig.supervisor.send.assert_not_called()
    assert_phase(rig.runtime, DictationPhase.IDLE)

    rig.event(type="hello")
    request = rig.supervisor.send.call_args.args[0]
    assert request["type"] == "model.load"
    assert request["dir"] == "/tmp/model"
    ipc.encode(request)
    rig.event(type="model.loaded")
    rig.pill.show_state.assert_called_with(PillState.LOADING_MODEL)
    rig.tray.set_model_recheck_enabled.assert_called_with(False)
    rig.supervisor.send.assert_called_with(
        {"type": "transcribe.file", "path": str(smoke_wav)}, timeout=module.SELFCHECK_TIMEOUT_S
    )
    assert [entry.args[0]["type"] for entry in rig.supervisor.send.call_args_list] == [
        "model.load",
        "transcribe.file",
    ]
    assert_recording_blocked(rig, loading=True, offset=6)
    assert "record.start" not in rig.trace

    rig.event(type="result", utterance_id="file", text="проверка" if success else "")
    # Сброс бюджета наблюдаем по номеру новой попытки в журнале.
    assert_selfcheck_log(caplog, "ok" if success else "no-match", 1)
    assert not rig.runtime._loading_model
    rig.tray.set_model_recheck_enabled.assert_called_with(not success)
    if success:
        rig.tray.set_state.assert_called_with(TrayState.IDLE)
        rig.pill.hide.assert_called_once_with()
    else:
        rig.tray.set_state.assert_called_with(TrayState.ERROR)
        rig.pill.show_state.assert_called_with(PillState.ERROR, text=ERROR_SELFCHECK_FAILED)
        assert rig.notify.notify_selfcheck_failed.call_count == 2
    if success:
        rig.hotkey.fsm.press(rig.now + 9)
        assert rig.supervisor.send.call_args.args[0]["type"] == "record.start"
    else:
        assert_recording_blocked(rig, offset=9)
    assert_phase(rig.runtime, DictationPhase.RECORDING if success else DictationPhase.IDLE)
    assert ("record.start" in rig.trace) == success


def test_selfcheck_tray_recheck_worker_start_failure_remains_available(checking_rig: Rig) -> None:
    rig = checking_rig
    rig.event(type="result", utterance_id="file", text="")
    rig.fail_at = "worker.start"
    rig.tray.on_model_recheck()
    assert not rig.runtime._loading_model
    rig.tray.set_model_recheck_enabled.assert_called_with(True)
    rig.tray.set_state.assert_called_with(TrayState.ERROR)
    rig.pill.show_state.assert_called_with(PillState.ERROR, text=ERROR_SELFCHECK_FAILED)
    assert rig.notify.notify_selfcheck_failed.call_count == 2
    assert_recording_blocked(rig)
    assert "record.start" not in rig.trace


def test_selfcheck_ignores_foreign_results_and_unrelated_errors(
    checking_rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = checking_rig
    on_event = Mock()
    monkeypatch.setattr(rig.runtime.orchestrator, "on_worker_event", on_event)
    for generation in (0, 2):
        rig.event(type="result", utterance_id="file", generation=generation, text="проверка")
    on_event.assert_not_called()
    rig.event(type="result", utterance_id="dictation", text=MARKER)
    rig.event(type="error", request_type="audio.open")
    assert on_event.call_count == 2
    rig.pill.show_state.assert_called_with(PillState.LOADING_MODEL)
    rig.tray.set_model_recheck_enabled.assert_called_with(False)
    rig.stats.append.assert_not_called()
    rig.pill.hide.assert_not_called()
    rig.notify.notify_selfcheck_failed.assert_not_called()
    assert_recording_blocked(rig, loading=True)
    rig.event(type="result", utterance_id="file", text="связи")
    rig.tray.set_model_recheck_enabled.assert_called_with(False)
    rig.tray.set_state.assert_called_with(TrayState.IDLE)
    assert on_event.call_count == 2
    rig.pill.hide.assert_called_once_with()
    rig.hotkey.fsm.press(rig.now + 3)
    assert_phase(rig.runtime, DictationPhase.RECORDING)
    assert rig.supervisor.send.call_args.args[0]["type"] == "record.start"


@pytest.mark.parametrize("result", ["проверка", ""])
def test_selfcheck_runs_after_each_worker_load(
    checking_rig: Rig, caplog: pytest.LogCaptureFixture, result: str
) -> None:
    caplog.set_level(logging.INFO, logger=module.__name__)
    rig = checking_rig
    rig.event(type="result", utterance_id="file", text=result)
    rig.pill.hide.reset_mock()
    rig.supervisor.generation += 1
    rig.event(type="hello")
    rig.tray.set_model_recheck_enabled.assert_called_with(False)
    assert rig.supervisor.send.call_args.args[0]["type"] == "model.load"
    assert rig.trace.count("transcribe.file") == 1
    assert_recording_blocked(rig, loading=True)
    rig.event(type="model.loaded", id="next-model", revision="r4")
    rig.pill.show_state.assert_called_with(PillState.LOADING_MODEL)
    rig.tray.set_model_recheck_enabled.assert_called_with(False)
    assert rig.supervisor.send.call_args.args[0]["type"] == "transcribe.file"
    assert_recording_blocked(rig, loading=True)
    rig.pill.hide.assert_not_called()
    caplog.clear()
    rig.event(type="result", utterance_id="file", text="связи")
    rig.tray.set_model_recheck_enabled.assert_called_with(False)
    rig.tray.set_state.assert_called_with(TrayState.IDLE)
    assert rig.stats.append.call_count == 2
    assert rig.stats.append.call_args.kwargs == {
        "model_id": "next-model",
        "revision": "r4",
        "engine_version": "",
        "cpu_model": "Test CPU",
        "result": "ok",
    }
    assert rig.trace.count("transcribe.file") == 2
    assert_selfcheck_log(caplog, "ok", 1)
    rig.pill.hide.assert_called_once_with()
    rig.hotkey.fsm.press(rig.now)
    assert_phase(rig.runtime, DictationPhase.RECORDING)
    assert rig.supervisor.send.call_args.args[0]["type"] == "record.start"


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("processor : 0\nmodel name\t: First CPU\nmodel name : Second CPU\n", "First CPU"),
        ("Hardware : unknown\n", ""),
    ],
)
def test_cpu_model_reads_first_name(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, content: str, expected: str
) -> None:
    cpuinfo = tmp_path / "cpuinfo"
    cpuinfo.write_text(content)
    path_factory = Mock(return_value=cpuinfo)
    monkeypatch.setattr(module, "Path", path_factory)
    assert module._cpu_model() == expected
    path_factory.assert_called_once_with("/proc/cpuinfo")


@pytest.mark.parametrize("error", [OSError(MARKER), UnicodeError(MARKER)])
def test_cpu_model_read_error_is_empty(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, error: Exception
) -> None:
    path = Mock()
    path.open.side_effect = error
    monkeypatch.setattr(module, "Path", Mock(return_value=path))
    assert module._cpu_model() == ""
    assert MARKER not in caplog.text


@pytest.mark.parametrize("value", [False, True])
def test_apply_pill_enabled_updates_pill_and_tray(rig: Rig, value: bool) -> None:
    rig.runtime.apply_pill_enabled(value)
    rig.pill.set_enabled.assert_called_once_with(value)
    rig.tray.set_pill_enabled.assert_called_once_with(value)
    rig.supervisor.send.assert_not_called()


@pytest.mark.parametrize("code", ["ok", "busy", "bad-combo", "duplicate", "not-grabbed"])
@pytest.mark.parametrize("phase", [DictationPhase.IDLE, DictationPhase.RECORDING])
@pytest.mark.parametrize(
    ("mode", "expected"),
    [("ptt", HotkeyMode.PTT), ("toggle", HotkeyMode.TOGGLE), ("unknown", HotkeyMode.PTT)],
)
def test_apply_hotkey_regrabs_and_updates_tray(
    rig: Rig, code: ResultCode, phase: DictationPhase, mode: str, expected: HotkeyMode
) -> None:
    rig.grab_code = code
    if phase == DictationPhase.RECORDING:
        rig.runtime.orchestrator.on_hotkey_state(HotkeyState.RECORDING, "press")
        rig.trace.clear()
        rig.tray.set_state.reset_mock()
        rig.supervisor.send.reset_mock()
    assert rig.runtime.phase == phase
    assert rig.runtime.apply_hotkey("Ctrl+Shift+Space", mode) == code
    assert rig.trace == ["hotkey.ungrab", "hotkey.grab"]
    rig.hotkey.ungrab.assert_called_once_with()
    rig.hotkey.grab.assert_called_once_with("Ctrl+Shift+Space", expected)
    if code != "ok":
        rig.tray.set_state.assert_called_once_with(TrayState.NOKEY)
    elif phase == DictationPhase.IDLE:
        rig.tray.set_state.assert_called_once_with(TrayState.IDLE)
    else:
        rig.tray.set_state.assert_not_called()
    rig.supervisor.send.assert_not_called()


@pytest.mark.parametrize("value", [None, "USB mic"])
def test_apply_device_defers_until_next_record(
    rig: Rig, value: str | None, caplog: pytest.LogCaptureFixture
) -> None:
    rig.runtime.settings.extra["device"] = value
    with caplog.at_level(logging.INFO):
        rig.runtime.apply_device(value)
    rig.supervisor.send.assert_not_called()
    assert rig.runtime.record_params()["device"] == value
    assert len(caplog.records) == 1
    assert caplog.records[0].levelno == logging.INFO
    assert rig.pill.mock_calls == rig.tray.mock_calls == rig.hotkey.mock_calls == []
