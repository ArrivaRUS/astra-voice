"""ИБ-31 и MN-7: отложенная проверка и восстановление после выгрузки."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path
from textwrap import dedent
from unittest.mock import Mock

import pytest
from test_indicators import Harness
from test_indicators import harness as indicator_harness  # noqa: F401
from test_model_idle_unload import fire_idle, make_rig, park, sent
from test_runtime import MARKER

from astra_voice.core.dictation import TEST_FAILED, DictationPhase, MicrophoneTestUpdate
from astra_voice.platform.hotkey import HotkeyState
from astra_voice.platform.paste import PasteTarget, snapshot_target
from astra_voice.ui.bridges import OnboardingController, SettingsBridge
from astra_voice.ui.tray_icons import TrayState

pytestmark = pytest.mark.unit


def fail_listening(state: TrayState) -> None:
    if state is TrayState.LISTENING:
        raise RuntimeError(MARKER)


def test_deferred_microphone_tray_failure_finishes_bridge(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """ИБ-31: реальный runtime/оркестратор, модель готова, трей падает на старте."""
    rig = make_rig(monkeypatch)
    park(rig)
    controller = OnboardingController(
        SettingsBridge(rig.runtime.settings, save=Mock()),
        settings=rig.runtime.settings,
        device_provider=lambda: [],
    )
    assert rig.runtime.start_test("selected-mic", controller._test_updated)
    assert controller.testState == "preparing"
    rig.tray.set_state.side_effect = fail_listening
    rig.event(type="model.loaded")
    with caplog.at_level(logging.DEBUG):
        rig.event(type="result", utterance_id="file", text="проверка")
    assert controller.testState == "error"
    assert controller.testMessage and MARKER not in controller.testMessage
    assert "record.cancel" in sent(rig)
    assert rig.runtime._pending_test is None
    assert rig.runtime._preparing_timer is None
    assert not rig.runtime.orchestrator.test_active
    assert MARKER not in caplog.text
    rig.runtime.shutdown()


def test_deferred_microphone_unexpected_start_exception_is_caught(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Внешний порт может бросить даже после safe path оркестратора."""
    rig = make_rig(monkeypatch)
    park(rig)
    updates: list[MicrophoneTestUpdate] = []
    terminal = False

    def receive(update: MicrophoneTestUpdate) -> None:
        nonlocal terminal
        if not terminal:
            updates.append(update)
            terminal = update.state in ("idle", "error", "done")

    assert rig.runtime.start_test("", receive)
    start = rig.runtime.orchestrator.start_test

    def start_then_fail(device: str, callback: object) -> bool:
        start(device, receive)
        raise RuntimeError(MARKER)

    monkeypatch.setattr(rig.runtime.orchestrator, "start_test", start_then_fail)
    rig.event(type="model.loaded")
    rig.event(type="result", utterance_id="file", text="проверка")
    assert updates[-1] == MicrophoneTestUpdate("error", message=TEST_FAILED)
    assert "record.cancel" in sent(rig)
    assert rig.runtime._pending_test is None
    assert rig.runtime._preparing_timer is None
    assert MARKER not in caplog.text
    rig.runtime.shutdown()


def test_deferred_microphone_failure_in_actual_qt_slot_does_not_abort() -> None:
    """Исключение настоящего PyQt slot раньше завершало subprocess SIGABRT."""
    root = Path(__file__).resolve().parents[1]
    script = dedent(
        f"""
        import resource
        import sys
        sys.path.insert(0, {str(root / "unit")!r})
        sys.path.insert(0, {str(root.parent / "src")!r})
        import pytest
        from PyQt5.QtCore import QCoreApplication, QTimer
        from test_model_idle_unload import make_rig, park, sent
        from test_runtime_t2_fixes import fail_listening
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        app = QCoreApplication([])
        patches = pytest.MonkeyPatch()
        rig = make_rig(patches)
        park(rig)
        updates = []
        assert rig.runtime.start_test('', updates.append)
        rig.tray.set_state.side_effect = fail_listening
        rig.event(type='model.loaded')
        def ready():
            rig.event(type='result', utterance_id='file', text='проверка')
            assert updates[-1].state == 'error'
            assert 'record.cancel' in sent(rig)
            assert rig.runtime._pending_test is None
            assert rig.runtime._preparing_timer is None
            rig.runtime.shutdown()
            print('slot completed', flush=True)
            app.quit()
        QTimer.singleShot(0, ready)
        QTimer.singleShot(2000, app.quit)
        app.exec_()
        patches.undo()
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=15,
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
    )
    assert result.returncode not in (134, -6), result.stderr
    assert result.returncode == 0, result.stderr
    assert "slot completed" in result.stdout
    assert MARKER not in result.stderr


@pytest.mark.parametrize("failure", ["raise", "worker-start"])
def test_unload_worker_start_failure_next_gesture_restarts_once(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    """MN-7: отказ именно при выгрузке, новый жест восстанавливает новую generation."""
    rig = make_rig(monkeypatch)
    generation = rig.supervisor.generation

    def cannot_start() -> None:
        rig.supervisor.state = "stopped"
        if failure == "raise":
            raise OSError(MARKER)
        rig.event(type="error", code="worker-start")

    rig.supervisor.start.side_effect = cannot_start
    rig.now += 300
    fire_idle(rig)
    assert rig.runtime._model_parked and rig.supervisor.state == "stopped"
    assert rig.supervisor.generation == generation + 1
    attempts = rig.supervisor_factory.call_count
    rig.press(rig.now)
    assert rig.supervisor_factory.call_count == attempts + 1
    assert rig.supervisor.generation == generation + 2
    assert rig.runtime._model_parked and not rig.runtime._loading_model
    assert rig.hotkey.fsm.state is HotkeyState.IDLE
    rig.supervisor.start.side_effect = rig.start_worker
    rig.release(rig.now + 0.5)
    rig.press(rig.now + 1)
    assert rig.supervisor_factory.call_count == attempts + 2
    assert rig.supervisor.generation == generation + 3
    assert rig.runtime._wake_generation == generation + 3
    assert rig.runtime._loading_model and not rig.runtime._model_parked
    rig.event(type="hello")
    rig.event(type="model.loaded")
    rig.event(type="result", utterance_id="file", text="проверка")
    assert rig.runtime.phase is DictationPhase.RECORDING
    assert sent(rig)[-1] == "record.start"
    rig.runtime.shutdown()


def test_wake_keeps_title_fingerprint_from_initial_gesture(monkeypatch: pytest.MonkeyPatch) -> None:
    rig = make_rig(monkeypatch)
    park(rig)
    initial = snapshot_target(rig.x11, 4321)
    rig.press(rig.now)
    title = rig.x11.d.create_resource_object.return_value.get_full_property.return_value
    title.value = b"Different browser tab"
    rig.event(type="model.loaded")
    rig.event(type="result", utterance_id="file", text="проверка")
    rig.release(rig.now + 1)
    rig.event(type="result", text=MARKER)
    target = rig.paste.call_args.args[1]
    assert isinstance(target, PasteTarget)
    assert target == initial
    assert target.title_digest != snapshot_target(rig.x11, 4321).title_digest
    rig.runtime.shutdown()


@pytest.mark.parametrize("overlap", [False, True])
@pytest.mark.parametrize("loss", ["disabled", "occluded"])
def test_runtime_untrusted_tray_and_pill_settings_preserve_recording_indicator(
    monkeypatch: pytest.MonkeyPatch,
    indicator_harness: Harness,  # noqa: F811 — pytest внедряет импортированную фикстуру
    overlap: bool,
    loss: str,
) -> None:
    """MJ-2: путь настроек runtime и настоящий guard, включая одиночную запись."""
    rig = make_rig(monkeypatch)
    harness = indicator_harness
    # Сохраняем runtime-проводку портов, делегируя UI и guard в управляемый
    # локальный harness без окон, шины и звукового устройства.
    rig.pill.show_state.side_effect = harness.pill.show_state
    rig.pill.hide.side_effect = harness.pill.hide
    rig.pill.set_enabled.side_effect = lambda enabled: setattr(harness.pill, "enabled", enabled)
    rig.guard.set_recording.side_effect = harness.guard.set_recording
    rig.guard.set_tray_trusted.side_effect = harness.guard.set_tray_trusted
    harness.guard.on_stop_recording = rig.runtime.orchestrator.on_indicators_lost
    rig.tray.set_state.side_effect = harness.tray.set_state
    try:
        if overlap:
            rig.press(rig.now)
            rig.release(rig.now + 1)
        previous = [
            call.args[0]["utterance_id"]
            for call in rig.supervisor.send.call_args_list
            if call.args[0]["type"] == "record.start"
        ]
        rig.tray.set_state.side_effect = fail_listening
        rig.press(rig.now + 2)
        current = rig.supervisor.send.call_args.args[0]["utterance_id"]
        assert harness.guard.recording and harness.pill.visible and harness.pill.forced
        assert harness.tray.state is not TrayState.LISTENING
        rig.runtime.apply_pill_enabled(False)
        rig.pill.set_enabled.assert_called_with(False)
        rig.tray.set_pill_enabled.assert_called_with(False)
        assert not harness.pill.enabled
        assert harness.guard.check() and harness.pill.visible
        assert "record.cancel" not in sent(rig)
        if loss == "occluded":
            harness.pill.available = False
            harness.clock.advance(1001)
            harness.guard.check()
            cancelled = [
                call.args[0]["utterance_id"]
                for call in rig.supervisor.send.call_args_list
                if call.args[0]["type"] == "record.cancel"
            ]
            assert cancelled == [current]
            assert not set(previous) & set(cancelled)
            # Повторный тик не должен отправить повторную отмену.
            harness.guard.check()
            assert sent(rig).count("record.cancel") == 1
        else:
            harness.clock.advance(1001)
            assert harness.guard.check() and harness.guard.recording
            assert "record.cancel" not in sent(rig)
    finally:
        rig.runtime.shutdown()
