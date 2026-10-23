"""ИБ-73: real runtime idle reservation, fake input/audio and no X server."""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock

import pytest
from test_runtime import Rig

from astra_voice import app as app_module
from astra_voice.core.command_mode import SessionSnapshot
from astra_voice.core.dictation import DictationPhase
from astra_voice.core.settings import Settings
from astra_voice.platform.hotkey import (
    GrabResult,
    HotkeyBackend,
    HotkeyEvent,
    HotkeyFsm,
    HotkeyManager,
    HotkeyMode,
    HotkeyState,
)
from astra_voice.platform.mouse_button import MouseHoldState
from astra_voice.platform.session import SessionKind

pytestmark = pytest.mark.unit


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch) -> Iterator[Rig]:
    result = Rig(monkeypatch)
    result.hotkey.has_pending_press = False
    result.runtime._selfcheck = "ok"
    result.runtime._loading_model = False
    result.runtime.apply_hotkey(result.runtime.settings.hotkey, "ptt")
    yield result
    result.runtime._preview_send = None
    result.runtime._pending_test = None
    result.runtime.orchestrator._level_callback = None
    result.runtime.orchestrator._phase = DictationPhase.IDLE
    result.runtime.shutdown()


@pytest.mark.parametrize(
    "busy",
    [
        "recording",
        "recognizing",
        "paste",
        "test",
        "level",
        "command",
        "capture",
        "removal",
        "model",
    ],
)
def test_ib73_fly_reservation_waits_for_all_existing_work(rig: Rig, busy: str) -> None:
    runtime = rig.runtime
    if busy in {"recording", "recognizing"}:
        runtime.orchestrator._phase = (
            DictationPhase.RECORDING if busy == "recording" else DictationPhase.PROCESSING
        )
    elif busy == "paste":
        runtime._preview_send = Mock()
    elif busy == "test":
        runtime._pending_test = ("device", Mock())
    elif busy == "level":
        runtime.orchestrator._level_callback = Mock()
    elif busy == "command":
        runtime.command_hotkey = Mock(has_pending_press=True)
    elif busy == "capture":
        runtime._input_owner = "keyboard-capture"
    elif busy == "removal":
        runtime._appimage_removal_reserved = True
    else:
        runtime._loading_model = True
    rig.hotkey.ungrab.reset_mock()
    assert not runtime.reserve_fly_tray_restart()
    rig.hotkey.ungrab.assert_not_called()


def test_ib73_reservation_ungrabs_and_blocks_input_until_release(rig: Rig) -> None:
    runtime = rig.runtime
    runtime.command_hotkey = Mock(has_pending_press=False, fsm=Mock(state=HotkeyState.IDLE))
    runtime.command_hotkey.fileno.return_value = -1
    assert runtime.reserve_fly_tray_restart()
    assert runtime._input_owner == "fly-tray"
    rig.hotkey.ungrab.assert_called()
    runtime.command_hotkey.ungrab.assert_called_once()
    assert not runtime.reserve_appimage_removal()
    assert not runtime._reserve_input("text")
    runtime._release_input("fly-tray")
    assert runtime._input_owner == "fly-tray"
    rig.hotkey.grab.reset_mock()
    assert not runtime._grab_hotkey().ok
    runtime.release_fly_tray_restart()
    assert runtime._input_owner is None
    rig.hotkey.grab.assert_called_once()


def test_ib73_actual_input_callbacks_are_blocked_and_command_rearms(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = rig.runtime
    snapshot = SessionSnapshot(known=True, locked=False)
    monkeypatch.setattr(runtime, "_command_snapshot", lambda: snapshot)
    command = Mock(has_pending_press=False)
    command.fsm = HotkeyFsm(HotkeyMode.PTT, runtime._on_command_hotkey_state)
    command.fileno.return_value = -1
    command.signature.return_value = (134, 0)
    command.rearm.return_value = GrabResult("ok")
    runtime.command_hotkey = command
    rig.hotkey.grab.reset_mock()
    assert runtime.reserve_fly_tray_restart()
    rig.supervisor.send.reset_mock()
    rig.hotkey.fsm.press(rig.now)
    command.fsm.press(rig.now)
    mouse = Mock(generation=7)
    runtime.command_mouse = mouse
    runtime._mouse_state(MouseHoldState.RECORDING, "hold")
    assert runtime.phase is DictationPhase.IDLE
    assert runtime._input_owner == "fly-tray"
    rig.supervisor.send.assert_not_called()
    runtime.command_mouse = None
    runtime.release_fly_tray_restart()
    for timer in tuple(rig.timers):
        if timer.single_shot and timer.interval == 0:
            timer.fire()
    command.rearm.assert_called_with(runtime.settings.command_hotkey, HotkeyMode.PTT)
    runtime.release_fly_tray_restart()
    rig.hotkey.grab.assert_called_once()


def test_ib73_shutdown_cancels_controller_without_regrabbing(rig: Rig) -> None:
    controller = Mock(worker_threads=[])
    rig.runtime.fly_tray = controller
    assert rig.runtime.reserve_fly_tray_restart()
    rig.hotkey.grab.reset_mock()
    rig.runtime.shutdown()
    controller.shutdown.assert_called_once()
    rig.runtime.release_fly_tray_restart()
    rig.hotkey.grab.assert_not_called()


@pytest.mark.parametrize("source", ["text", "command"])
def test_review6_cancelled_fsm_waits_for_physical_release(rig: Rig, source: str) -> None:
    backend = Mock(spec=HotkeyBackend)
    backend.grab_combo.return_value = GrabResult("ok", keycode=65, mods=4)
    backend.grab_escape.return_value = GrabResult("ok", keycode=9)
    backend.fileno.return_value = -1
    held = HotkeyManager(backend)
    held.grab("Ctrl+Space", HotkeyMode.PTT)
    held.handle_event(HotkeyEvent("KeyPress", 65, 1000, mods=4), rig.now)
    held.fsm.escape(rig.now)
    assert held.fsm.state is HotkeyState.IDLE and held.physical_key_down
    if source == "text":
        rig.runtime.hotkey = held
    else:
        rig.runtime.command_hotkey = held
    assert not rig.runtime.reserve_fly_tray_restart()
    held.handle_event(HotkeyEvent("KeyRelease", 65, 2000, mods=4), rig.now + 1)
    assert not held.physical_key_down
    assert rig.runtime.reserve_fly_tray_restart()
    rig.runtime.release_fly_tray_restart()


@pytest.mark.parametrize("reason", ["register", "hidden", "wizard", "update", "policy", "kde"])
def test_ib73_startup_modes_never_arm_automatic_fly_action(reason: str) -> None:
    args = SimpleNamespace(register=reason == "register", hidden=reason == "hidden")
    settings = Settings()
    settings.extra["onboarding_done"] = reason != "wizard"
    kind = SessionKind.KDE if reason == "kde" else SessionKind.FLY
    assert not app_module._fly_tray_automatic_allowed(
        args, settings, kind, after_update=reason == "update", policy_disabled=reason == "policy"
    )


def test_ib73_normal_completed_fly_startup_is_eligible() -> None:
    args = SimpleNamespace(register=False, hidden=False)
    settings = Settings(extra={"onboarding_done": True})
    assert app_module._fly_tray_automatic_allowed(args, settings, SessionKind.FLY)


def test_ib73_runtime_refreshes_tray_after_return(rig: Rig) -> None:
    rig.runtime.refresh_fly_tray_icon()
    rig.tray.refresh_after_panel_restart.assert_called_once()


def test_ib73_worker_threads_are_in_update_shutdown_barrier() -> None:
    worker = Mock()
    runtime = Mock(
        fly_tray=Mock(worker_threads=[worker]),
        _switch_candidate=None,
        command_hotkey=None,
        _command_client=None,
        _command_session=None,
    )
    runtime.recording_cues = None
    runtime.supervisor.process = None
    runtime.supervisor._retired = []
    _, threads = app_module._copy_shutdown_resources(runtime, None, None)
    assert worker in threads


def test_ib73_updated_image_marks_origin_without_changing_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from astra_voice.updates import restart

    launcher = Mock()
    download = SimpleNamespace(path=tmp_path / "image", identity="identity")
    pending = SimpleNamespace(download=download, environment=(("TMPDIR", str(tmp_path)),))
    preparer = Mock()
    monkeypatch.setattr(restart, "_identity", lambda path: "identity")
    monkeypatch.setattr(restart, "_private_directory", Mock())
    monkeypatch.setattr(subprocess, "Popen", launcher)
    assert (
        restart.RestartPreparer.launch(
            preparer, cast(restart.PendingRestart, pending), shutdown_complete=True
        )
        == 0
    )
    assert launcher.call_args.kwargs["env"]["ASTRA_VOICE_AFTER_UPDATE"] == "1"
    assert launcher.call_args.args[0] == [str(download.path), "--appimage-extract-and-run"]
