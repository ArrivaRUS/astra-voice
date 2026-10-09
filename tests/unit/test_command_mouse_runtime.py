"""Author integration tests with real FSM/orchestrator and fake hardware only."""

from __future__ import annotations

from dataclasses import replace
from typing import Any, cast
from unittest.mock import Mock

import pytest
from test_command_mouse_backend import Backend
from test_runtime import FakeTimer, Rig

from astra_voice.core.command_mode import CommandMode, SessionSnapshot
from astra_voice.core.dictation import DictationPhase
from astra_voice.platform.hotkey import (
    GrabResult,
    HotkeyBackend,
    HotkeyEvent,
    HotkeyManager,
    HotkeyState,
    MappingEvent,
)
from astra_voice.platform.mouse_button import MouseButtonEvent, MouseHoldManager, MouseHoldState
from astra_voice.platform.session import SessionKind
from astra_voice.ui.bridges import SettingsBridge

pytestmark = pytest.mark.unit


class MouseRuntimeRig:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.rig = Rig(monkeypatch, session_kind=SessionKind.KDE)
        self.runtime = runtime = self.rig.runtime
        self.snapshot = SessionSnapshot(known=True, locked=False)
        runtime._command_session = Mock(snapshot=lambda: self.snapshot)
        runtime.command_installed = True
        runtime._selfcheck = "ok"
        runtime._started = True
        self.rig.supervisor.state = "running"
        runtime._model_load_generation = self.rig.supervisor.generation
        runtime._model_load_request = {"id": "model", "revision": "revision", "dir": "/tmp/trusted"}
        self.record = Mock(
            id="model",
            revision="revision",
            dir="/tmp/trusted",
            state="ok",
            recheck=False,
            metadata_ok=True,
        )
        runtime.model_store = Mock(current=lambda: self.record)
        runtime._revoked_check = lambda _id, revision: False
        self.client = Mock()
        mode = CommandMode(
            client=self.client,
            session=runtime._command_snapshot,
            model_trusted=runtime._command_model_trusted,
            publish=lambda text: True,
            present=lambda feedback: None,
        )
        runtime._command_mode = mode
        runtime.orchestrator.command_mode = mode
        runtime.orchestrator._command_preview = runtime._preview_command
        self.text_backend = self.keyboard_backend(65, 4)
        self.command_backend = self.keyboard_backend(133, 0)
        runtime.hotkey = HotkeyManager(self.text_backend, clock=lambda: self.rig.now)
        runtime.hotkey.on_state = runtime._on_hotkey_state
        runtime.command_hotkey = HotkeyManager(self.command_backend, clock=lambda: self.rig.now)
        runtime.command_hotkey.on_state = runtime._on_command_hotkey_state
        runtime._grab_hotkey()
        self.backend = Backend()
        self.manager = MouseHoldManager(self.backend, clock=lambda: self.rig.now)
        runtime._mouse_factory = lambda: self.manager
        runtime.settings.command_mouse_enabled = True
        assert runtime.reload_command_hotkey()
        assert runtime.command_mouse_status == "ready"

    @staticmethod
    def keyboard_backend(keycode: int, mods: int) -> Any:
        backend = Mock(spec=HotkeyBackend)
        backend.grab_combo.return_value = GrabResult("ok", keycode=keycode, mods=mods)
        backend.grab_escape.return_value = GrabResult("ok", keycode=9)
        backend.poll_events.return_value = []
        backend.ungrab_combo.return_value = GrabResult("ok")
        backend.combo_signature = Mock(return_value=(keycode, mods))
        backend.fileno.return_value = -1
        return backend

    def press(self) -> None:
        self.backend.held = True
        self.backend.events.append(MouseButtonEvent("press", 2))
        self.runtime._process_mouse()

    def record_mouse(self) -> None:
        self.press()
        self.rig.now += 0.301
        self.fire_deadline()
        assert self.runtime.phase is DictationPhase.RECORDING

    def fire_deadline(self) -> None:
        timer = self.runtime._mouse_tick_timer
        assert timer is not None
        cast(FakeTimer, timer).fire()

    def release(self) -> None:
        self.backend.held = False
        self.backend.events.append(MouseButtonEvent("release", 2))
        self.runtime._process_mouse()

    def escape(self) -> None:
        self.runtime.hotkey.handle_event(HotkeyEvent("KeyPress", 9, 10, escape=True), self.rig.now)

    def sent(self, kind: str) -> list[dict[str, Any]]:
        return [
            call.args[0]
            for call in self.rig.supervisor.send.call_args_list
            if call.args[0]["type"] == kind
        ]


@pytest.mark.parametrize("elapsed", [0.1, 0.299, 0.31, 10.0])
def test_pending_release_is_drained_before_delayed_timer(
    monkeypatch: pytest.MonkeyPatch, elapsed: float
) -> None:
    bench = MouseRuntimeRig(monkeypatch)
    bench.press()
    assert bench.runtime._input_owner == "mouse"
    assert not bench.sent("record.start")
    bench.rig.now += elapsed
    bench.backend.held = False
    bench.backend.events.append(MouseButtonEvent("release", 2))
    bench.fire_deadline()
    assert bench.manager.state is MouseHoldState.IDLE
    assert bench.runtime._input_owner is None
    assert not bench.sent("record.start")
    bench.client.submit.assert_not_called()
    bench.runtime.shutdown()


def test_hold_is_ptt_even_when_keyboard_is_toggle(monkeypatch: pytest.MonkeyPatch) -> None:
    bench = MouseRuntimeRig(monkeypatch)
    bench.runtime.settings.hotkey_mode = "toggle"
    bench.record_mouse()
    assert bench.sent("record.start")
    assert bench.runtime.hotkey.fsm.state is HotkeyState.IDLE
    bench.release()
    bench.rig.fire_tail()
    assert bench.runtime.phase is DictationPhase.PROCESSING
    assert bench.sent("record.stop")
    bench.runtime.shutdown()


@pytest.mark.parametrize("phase", ["pending", "recording", "processing", "preview"])
def test_escape_uses_existing_text_reader_and_never_submits(
    monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    bench = MouseRuntimeRig(monkeypatch)
    if phase == "pending":
        bench.press()
    else:
        bench.record_mouse()
    if phase in ("processing", "preview"):
        bench.release()
        bench.rig.fire_tail()
    if phase == "preview":
        bench.runtime.settings.command_preview = True
        bench.runtime.on_command_preview = Mock()
        bench.runtime.orchestrator._result("test phrase")
        assert bench.runtime._preview_send is not None
    bench.escape()
    assert bench.manager.state is MouseHoldState.IDLE
    assert bench.runtime._mouse_escape_token is None
    assert bench.runtime._preview_send is None
    bench.runtime.confirm_command()
    bench.client.submit.assert_not_called()
    if phase == "pending":
        assert not bench.sent("record.start")
    bench.runtime.shutdown()


@pytest.mark.parametrize(
    "blocked",
    [
        "locked",
        "unknown",
        "sleep",
        "disabled",
        "uninstalled",
        "untrusted",
        "selfcheck",
        "generation",
    ],
)
def test_guard_change_after_pending_cannot_start_microphone(
    monkeypatch: pytest.MonkeyPatch, blocked: str
) -> None:
    bench = MouseRuntimeRig(monkeypatch)
    bench.press()
    if blocked == "locked":
        bench.snapshot = replace(bench.snapshot, locked=True, blocked_epoch=1)
    elif blocked == "unknown":
        bench.snapshot = SessionSnapshot()
    elif blocked == "sleep":
        bench.snapshot = replace(bench.snapshot, preparing_for_sleep=True)
    elif blocked == "disabled":
        bench.runtime.settings.command_mouse_enabled = False
    elif blocked == "uninstalled":
        bench.runtime.command_installed = False
    elif blocked == "untrusted":
        bench.record.metadata_ok = False
    elif blocked == "selfcheck":
        bench.runtime._selfcheck = "running"
    else:
        bench.rig.supervisor.generation += 1
    bench.rig.now += 0.31
    bench.fire_deadline()
    assert not bench.sent("record.start")
    assert bench.manager.state is MouseHoldState.IDLE
    bench.runtime.shutdown()


def test_keyboard_conflict_does_not_disable_mouse_and_escape_failure_refuses_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bench = MouseRuntimeRig(monkeypatch)
    bench.command_backend.grab_combo.return_value = GrabResult("busy")
    assert not bench.runtime.reload_command_hotkey()
    assert bench.runtime.command_mouse_status == "ready"
    bench.text_backend.grab_escape.return_value = GrabResult("busy")
    bench.press()
    assert bench.runtime.command_mouse_status == "unavailable"
    assert not bench.sent("record.start")
    assert bench.runtime._input_owner is None
    bench.runtime.shutdown()


def test_real_bridge_capture_stages_saves_and_restores_with_stale_token_isolation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bench = MouseRuntimeRig(monkeypatch)
    save = Mock()
    bridge = SettingsBridge(bench.runtime.settings, save=save)
    bridge.bind_command_host(bench.runtime)
    assert bridge.selectCommandMouseButton(8)
    first = bench.runtime._mouse_capture_token
    assert first is not None and bench.backend.grabbed is None
    assert bridge.commandMouseCanEdit
    assert bridge.applyCommandMouseButton()
    assert bench.runtime.settings.command_mouse_button == 8
    save.assert_called_once()
    assert bridge.selectCommandMouseButton(9)
    second = bench.runtime._mouse_capture_token
    assert second is not None and second is not first
    bench.runtime.end_command_mouse_capture(first)
    assert bench.runtime.command_mouse_capture_valid(second)
    bench.snapshot = replace(bench.snapshot, locked=True, blocked_epoch=1)
    bench.runtime._command_session_changed(bench.snapshot)
    assert bridge.commandMouseCaptureState == "idle"
    assert not bench.runtime.command_mouse_capture_valid(second)
    bench.runtime.shutdown()


def test_keyboard_and_microphone_cannot_enter_mouse_capture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bench = MouseRuntimeRig(monkeypatch)
    token = bench.runtime.begin_command_mouse_capture()
    assert token is not None
    bench.runtime.hotkey.handle_event(HotkeyEvent("KeyPress", 65, 1, mods=4), bench.rig.now)
    assert not bench.sent("record.start")
    assert not bench.runtime.begin_hotkey_capture(role="text")
    assert not bench.runtime.start_test("", Mock())
    assert not bench.runtime.start_level_monitor("", Mock())
    assert bench.runtime.command_mouse_capture_valid(token)
    bench.runtime.end_command_mouse_capture(token)
    bench.runtime.shutdown()


@pytest.mark.parametrize("stage", ["trust", "grab"])
def test_reentrant_lock_unlock_cannot_arm_under_old_admission(
    monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    bench = MouseRuntimeRig(monkeypatch)

    def change_epoch() -> None:
        bench.snapshot = replace(bench.snapshot, blocked_epoch=1)

    if stage == "trust":

        def revoke(_id: str, revision: str) -> bool:
            change_epoch()
            return False

        bench.runtime._revoked_check = revoke
    else:
        original = bench.backend.grab

        def grab(button: int) -> Any:
            result = original(button)
            change_epoch()
            return result

        bench.backend.grab = grab  # type: ignore[method-assign]
    bench.runtime.reload_command_mouse()
    assert bench.runtime.command_mouse_status != "ready"
    assert bench.backend.grabbed is None
    bench.runtime.shutdown()


def test_mouse_limit_stops_without_release_tail(monkeypatch: pytest.MonkeyPatch) -> None:
    bench = MouseRuntimeRig(monkeypatch)
    bench.record_mouse()
    bench.rig.now += 120
    bench.fire_deadline()
    assert bench.sent("record.stop")
    assert bench.runtime.phase is DictationPhase.PROCESSING
    bench.runtime.shutdown()


@pytest.mark.parametrize("phase", ["pending", "recording", "processing"])
def test_shutdown_closes_mouse_and_cancels_all_mouse_timers(
    monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    bench = MouseRuntimeRig(monkeypatch)
    if phase == "pending":
        bench.press()
    else:
        bench.record_mouse()
        if phase == "processing":
            bench.release()
            bench.rig.fire_tail()
    bench.runtime.shutdown()
    assert bench.backend.closed
    assert bench.runtime._mouse_escape_token is None
    assert bench.runtime._mouse_tick_timer is None
    assert bench.runtime._mouse_retry_timer is None
    starts = len(bench.sent("record.start"))
    for timer in bench.rig.timers:
        timer.timeout.emit()
    assert len(bench.sent("record.start")) == starts
    bench.client.submit.assert_not_called()


def test_cancel_after_submit_does_not_cancel_already_published_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bench = MouseRuntimeRig(monkeypatch)
    bench.record_mouse()
    bench.release()
    bench.rig.fire_tail()
    bench.runtime.orchestrator._result("test phrase")
    assert bench.client.submit.call_count == 1
    bench.escape()
    assert bench.runtime.phase is DictationPhase.DELIVERING
    assert bench.client.submit.call_count == 1
    assert not bench.sent("record.cancel")
    bench.runtime.shutdown()


def test_record_start_send_failure_releases_mouse_before_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bench = MouseRuntimeRig(monkeypatch)
    released: list[tuple[MouseHoldState, int | None, object]] = []

    def observe() -> None:
        if bench.runtime._input_owner is None:
            released.append(
                (bench.manager.state, bench.backend.grabbed, bench.runtime._mouse_tick_timer)
            )

    bench.runtime.on_command_mouse_changed = observe
    bench.rig.fail_at = "record.start"
    bench.press()
    bench.rig.now += 0.301
    bench.fire_deadline()
    assert bench.runtime.phase is DictationPhase.FINISHING
    assert bench.manager.state is MouseHoldState.IDLE
    assert bench.runtime._input_owner is None
    assert bench.backend.grabbed is None
    assert bench.runtime._mouse_tick_timer is None
    assert released and all(
        state is MouseHoldState.IDLE and button is None and timer is None
        for state, button, timer in released
    )
    bench.runtime.shutdown()


def test_throwing_mouse_notification_cannot_abort_shutdown(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    bench = MouseRuntimeRig(monkeypatch)
    bench.record_mouse()

    def fail() -> None:
        raise RuntimeError("PRIVATE_UI_DETAIL")

    bench.runtime.on_command_mouse_changed = fail
    bench.runtime.shutdown()
    bench.runtime.shutdown()
    assert bench.backend.closed
    assert bench.sent("audio.close")
    assert bench.sent("record.cancel")
    bench.rig.supervisor.stop.assert_called_once()
    assert bench.runtime._mouse_tick_timer is None
    assert bench.runtime._mouse_escape_token is None
    assert "PRIVATE_UI_DETAIL" not in caplog.text


def test_throwing_mouse_notification_does_not_orphan_capture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bench = MouseRuntimeRig(monkeypatch)

    def fail() -> None:
        raise RuntimeError("PRIVATE_UI_DETAIL")

    bench.runtime.on_command_mouse_changed = fail
    token = bench.runtime.begin_command_mouse_capture()
    assert token is not None
    assert bench.runtime.command_mouse_capture_valid(token)
    bench.runtime.end_command_mouse_capture(token)
    assert bench.runtime._input_owner is None
    assert bench.runtime._mouse_capture_token is None
    bench.runtime.shutdown()


@pytest.mark.parametrize("failure", ["factory", "open"])
def test_keyboard_capture_acquisition_exception_rolls_back_owner(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    bench = MouseRuntimeRig(monkeypatch)
    if failure == "factory":
        bench.runtime._capture_watchdog_factory = Mock(side_effect=RuntimeError("PRIVATE_FACTORY"))
    else:
        watchdog = Mock()
        watchdog.open.side_effect = RuntimeError("PRIVATE_OPEN")
        bench.runtime._capture_watchdog_factory = Mock(return_value=watchdog)
    assert not bench.runtime.begin_hotkey_capture()
    bench.runtime.end_hotkey_capture()
    assert bench.runtime._input_owner is None
    assert bench.runtime._capture_watchdog is None
    assert bench.runtime.command_mouse_can_edit
    if failure == "open":
        watchdog.close.assert_called_once()
    bench.runtime.shutdown()


def test_keyboard_capture_close_exception_still_releases_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bench = MouseRuntimeRig(monkeypatch)
    assert bench.runtime.begin_hotkey_capture()
    watchdog = bench.runtime._capture_watchdog
    assert watchdog is not None
    cast(Mock, watchdog).close.side_effect = RuntimeError("PRIVATE_CLOSE")
    bench.runtime.end_hotkey_capture()
    assert bench.runtime._input_owner is None
    assert bench.runtime._capture_watchdog is None
    assert bench.runtime.command_mouse_can_edit
    bench.runtime.shutdown()


@pytest.mark.parametrize("owner", ["mouse", "mouse-capture", "keyboard-capture"])
def test_text_mapping_retry_waits_for_input_owner_and_restores_dictation(
    monkeypatch: pytest.MonkeyPatch, owner: str
) -> None:
    bench = MouseRuntimeRig(monkeypatch)
    runtime = bench.runtime
    token = None
    if owner == "mouse":
        bench.press()
    elif owner == "mouse-capture":
        token = runtime.begin_command_mouse_capture()
        assert token is not None
    else:
        assert runtime.begin_hotkey_capture()
    assert runtime._input_owner == owner
    escape_token = runtime._mouse_escape_token
    bench.text_backend.poll_events.return_value = [
        MappingEvent({runtime.settings.hotkey: GrabResult("busy")}, GrabResult("ok", keycode=9))
    ]
    runtime._process_hotkey()
    bench.text_backend.poll_events.return_value = []
    assert runtime.hotkey.last_result.code == "busy"
    retry = runtime._regrab_timer
    assert retry is not None
    grabs = bench.text_backend.grab_combo.call_count
    cast(FakeTimer, retry).fire()
    assert bench.text_backend.grab_combo.call_count == grabs
    assert runtime._input_owner == owner
    assert runtime._mouse_escape_token is escape_token
    assert runtime._regrab_attempts == 0
    assert not bench.sent("record.start")
    if owner == "mouse":
        assert bench.manager.state is MouseHoldState.PENDING
        bench.release()
    elif owner == "mouse-capture":
        runtime.end_command_mouse_capture(token)
    else:
        runtime.end_hotkey_capture()
    assert runtime._input_owner is None
    cast(FakeTimer, retry).fire()
    assert runtime._regrab_timer is None
    assert runtime.hotkey.last_result.ok
    runtime.hotkey.handle_event(HotkeyEvent("KeyPress", 65, 30, mods=4), bench.rig.now)
    assert runtime._input_owner == "text"
    assert bench.sent("record.start")
    runtime.shutdown()


@pytest.mark.parametrize("next_action", ["hold", "escape"])
def test_old_finish_tail_preserves_new_mouse_pending(
    monkeypatch: pytest.MonkeyPatch, next_action: str
) -> None:
    bench = MouseRuntimeRig(monkeypatch)
    runtime = bench.runtime
    bench.record_mouse()
    bench.escape()
    bench.rig.event(type="cancelled")
    assert runtime.phase.value == "finishing"
    assert runtime._input_owner is None
    finish_timer = next(handle for handle, _ in runtime.orchestrator._timers.values())
    bench.backend.held = False
    runtime.reload_command_mouse()
    bench.press()
    assert bench.manager.state.value == "pending"
    epoch, token = runtime._input_epoch, runtime._mouse_escape_token
    pending_timer = runtime._mouse_tick_timer
    assert pending_timer is not None and token is not None
    starts = len(bench.sent("record.start"))
    cast(FakeTimer, finish_timer).fire()
    assert runtime.phase.value == "idle"
    assert runtime._input_owner == "mouse"
    assert runtime._input_epoch == epoch
    assert runtime._mouse_escape_token is token
    assert bench.manager.state.value == "pending"
    if next_action == "escape":
        bench.escape()
        assert runtime._input_owner is None
    bench.rig.now += 0.301
    cast(FakeTimer, pending_timer).fire()
    if next_action == "hold":
        assert runtime.phase is DictationPhase.RECORDING
        assert len(bench.sent("record.start")) == starts + 1
    else:
        assert bench.manager.state is MouseHoldState.IDLE
        assert len(bench.sent("record.start")) == starts
    runtime.shutdown()
