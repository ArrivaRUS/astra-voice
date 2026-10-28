"""ИБ-73: real runtime idle reservation, fake input/audio and no X server."""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock

import pytest
from test_runtime import Rig

from astra_voice import app as app_module
from astra_voice.core.command_mode import SessionSnapshot
from astra_voice.core.dictation import DictationPhase
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


# Attribute paths keep every cause independently observable, including identity
# checks that intentionally differ from ordinary truthiness.
RESERVATION_CASES = [
    ("_closed", True, "closed"),
    ("_appimage_removal_reserved", True, "appimage_removal"),
    ("_fly_tray_reserved", True, "fly_tray_reserved"),
    ("supervisor.restarting", True, "supervisor_restarting"),
    ("orchestrator.phase", DictationPhase.PROCESSING, "phase:processing"),
    ("_input_owner", "keyboard-capture", "input_owner:keyboard-capture"),
    ("_capture_watchdog", 0, "capture_watchdog"),
    ("_mouse_capture_token", 0, "mouse_capture"),
    ("_preview_send", 0, "preview_send"),
    ("_pending_test", 0, "pending_test"),
    ("orchestrator.test_active", True, "test_active"),
    ("orchestrator.level_active", True, "level_active"),
    ("orchestrator.cleanup_pending", True, "cleanup_pending"),
    ("_pending_switch", 0, "pending_switch"),
    ("_switch_in_progress", True, "switch_active"),
    ("_loading_model", True, "loading_model"),
    ("_selfcheck", "retrying", "selfcheck:retrying"),
    ("hotkey.has_pending_press", None, "hotkey_pending_press"),
    ("hotkey.fsm.state", HotkeyState.PROCESSING, "hotkey_fsm:processing"),
    ("command_hotkey.has_pending_press", None, "command_hotkey_pending_press"),
    ("command_hotkey.fsm.state", HotkeyState.RECORDING, "command_hotkey_fsm:recording"),
    ("command_mouse.state", MouseHoldState.PENDING, "command_mouse:pending"),
    ("hotkey.physical_key_down", True, "physical_key_down"),
    ("command_hotkey.physical_key_down", True, "command_physical_key_down"),
]


@pytest.fixture
def reservation_runtime(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    runtime = rig.runtime
    with monkeypatch.context() as patch:
        patch.setattr(
            runtime,
            "orchestrator",
            SimpleNamespace(
                phase=DictationPhase.IDLE,
                test_active=False,
                level_active=False,
                cleanup_pending=False,
            ),
        )
        for name in ("hotkey", "command_hotkey"):
            patch.setattr(
                runtime,
                name,
                SimpleNamespace(
                    has_pending_press=False,
                    physical_key_down=False,
                    fsm=SimpleNamespace(state=HotkeyState.IDLE),
                ),
            )
        patch.setattr(runtime, "command_mouse", SimpleNamespace(state=MouseHoldState.IDLE))
        saved = []
        for path, _, _ in RESERVATION_CASES:
            target = runtime
            for part in path.split(".")[:-1]:
                target = getattr(target, part)
            saved.append((path, getattr(target, path.split(".")[-1])))
        try:
            yield runtime
        finally:
            for path, value in saved:
                set_reservation_value(runtime, path, value)


def set_reservation_value(runtime: Any, path: str, value: Any) -> None:
    parts = path.split(".")
    for part in parts[:-1]:
        runtime = getattr(runtime, part)
    setattr(runtime, parts[-1], value)


def test_reservation_blockers_idle(reservation_runtime: Any) -> None:
    assert reservation_runtime.reservation_blockers() == ()
    assert reservation_runtime._work_pending() is False
    assert reservation_runtime._reservation_pending() is False


@pytest.mark.parametrize(("path", "value", "blocker"), RESERVATION_CASES)
def test_reservation_blockers_each_existing_predicate(
    reservation_runtime: Any, path: str, value: Any, blocker: str
) -> None:
    set_reservation_value(reservation_runtime, path, value)
    assert reservation_runtime.reservation_blockers() == (blocker,)
    assert reservation_runtime._reservation_pending() is True
    assert reservation_runtime._work_pending() is ("physical_key_down" not in blocker)


def test_reservation_blockers_all_causes_keep_formula_order(reservation_runtime: Any) -> None:
    for path, value, _ in RESERVATION_CASES[1:]:
        set_reservation_value(reservation_runtime, path, value)
    assert reservation_runtime.reservation_blockers() == tuple(
        blocker for _, _, blocker in RESERVATION_CASES[1:]
    )


@pytest.mark.parametrize("prefix", ["hotkey", "command_hotkey"])
@pytest.mark.parametrize("value", [False, True, None, 0, 1, Mock()])
def test_reservation_blockers_preserve_identity_checks(
    reservation_runtime: Any, prefix: str, value: Any
) -> None:
    set_reservation_value(reservation_runtime, prefix + ".has_pending_press", value)
    label = "hotkey_pending_press" if prefix == "hotkey" else "command_hotkey_pending_press"
    expected = () if value is False else (label,)
    assert reservation_runtime.reservation_blockers() == expected
    assert reservation_runtime._work_pending() is (value is not False)
    set_reservation_value(reservation_runtime, prefix + ".has_pending_press", False)
    set_reservation_value(reservation_runtime, prefix + ".physical_key_down", value)
    label = "physical_key_down" if prefix == "hotkey" else "command_physical_key_down"
    assert reservation_runtime.reservation_blockers() == ((label,) if value is True else ())
    assert reservation_runtime._work_pending() is False
    assert reservation_runtime._reservation_pending() is (value is True)


@pytest.mark.parametrize(
    "owner",
    [
        "text",
        "keyboard",
        "mouse",
        "mouse-capture",
        "keyboard-capture",
        "appimage-removal",
        "fly-tray",
    ],
)
def test_reservation_blockers_allow_only_fixed_input_owners(
    reservation_runtime: Any, owner: str
) -> None:
    reservation_runtime._input_owner = owner
    assert reservation_runtime.reservation_blockers() == ("input_owner:" + owner,)


@pytest.mark.parametrize(
    ("path", "label"),
    [
        ("orchestrator.phase", "phase"),
        ("_input_owner", "input_owner"),
        ("_selfcheck", "selfcheck"),
        ("hotkey.fsm.state", "hotkey_fsm"),
        ("command_hotkey.fsm.state", "command_hotkey_fsm"),
        ("command_mouse.state", "command_mouse"),
    ],
)
@pytest.mark.parametrize("value", ["PRIVATE-PATH-AND-TEXT", Mock(value="PRIVATE-VALUE"), 0])
def test_reservation_blockers_never_format_unknown_values(
    reservation_runtime: Any, path: str, label: str, value: Any
) -> None:
    set_reservation_value(reservation_runtime, path, value)
    assert reservation_runtime.reservation_blockers() == (label + ":unknown",)


@pytest.mark.parametrize("phase", list(DictationPhase)[1:])
def test_reservation_blockers_phase_enum_values(
    reservation_runtime: Any, phase: DictationPhase
) -> None:
    reservation_runtime.orchestrator.phase = phase
    assert reservation_runtime.reservation_blockers() == ("phase:" + phase.value,)


@pytest.mark.parametrize("state", list(HotkeyState)[1:])
def test_reservation_blockers_hotkey_enum_values(
    reservation_runtime: Any, state: HotkeyState
) -> None:
    reservation_runtime.hotkey.fsm.state = state
    reservation_runtime.command_hotkey.fsm.state = state
    assert reservation_runtime.reservation_blockers() == (
        "hotkey_fsm:" + state.value,
        "command_hotkey_fsm:" + state.value,
    )


@pytest.mark.parametrize("state", list(MouseHoldState)[1:])
def test_reservation_blockers_mouse_enum_values(
    reservation_runtime: Any, state: MouseHoldState
) -> None:
    reservation_runtime.command_mouse.state = state
    assert reservation_runtime.reservation_blockers() == ("command_mouse:" + state.value,)


@pytest.mark.parametrize("state", ["idle", "ok", "failed", "running", "retrying"])
def test_reservation_blockers_selfcheck_values(reservation_runtime: Any, state: str) -> None:
    reservation_runtime._selfcheck = state
    assert reservation_runtime.reservation_blockers() == (
        ("selfcheck:" + state,) if state in ("running", "retrying") else ()
    )


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
    # The cancelled gesture is no model work; disruptive reservations must still
    # wait for the physical release so they cannot lose that input boundary.
    assert not rig.runtime._work_pending()
    assert not rig.runtime.reserve_fly_tray_restart()
    assert rig.runtime.appimage_remove_busy_reason()
    assert not rig.runtime.reserve_appimage_removal()
    held.handle_event(HotkeyEvent("KeyRelease", 65, 2000, mods=4), rig.now + 1)
    assert not held.physical_key_down
    assert not rig.runtime.appimage_remove_busy_reason()
    assert rig.runtime.reserve_appimage_removal()
    rig.runtime.release_appimage_removal()
    assert rig.runtime.reserve_fly_tray_restart()
    rig.runtime.release_fly_tray_restart()


@pytest.mark.parametrize("reason", ["register", "hidden", "wizard", "update", "policy", "kde"])
def test_ib73_all_fly_startup_modes_are_eligible_unless_policy_disabled(reason: str) -> None:
    kind = SessionKind.KDE if reason == "kde" else SessionKind.FLY
    assert app_module._fly_tray_automatic_allowed(kind, policy_disabled=reason == "policy") is (
        reason not in {"policy", "kde"}
    )


def test_ib73_normal_fly_startup_is_eligible() -> None:
    assert app_module._fly_tray_automatic_allowed(SessionKind.FLY)


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


@pytest.mark.parametrize("closed", [False, True])
@pytest.mark.parametrize(
    "path",
    [
        "supervisor",
        "orchestrator",
        "hotkey",
        "hotkey.fsm",
        "command_hotkey.fsm",
    ],
)
@pytest.mark.parametrize("missing", [False, True])
def test_reservation_partial_required_objects(
    reservation_runtime: Any,
    monkeypatch: pytest.MonkeyPatch,
    closed: bool,
    path: str,
    missing: bool,
) -> None:
    with monkeypatch.context() as patch:
        target = reservation_runtime
        for part in path.split(".")[:-1]:
            target = getattr(target, part)
        if missing:
            patch.delattr(target, path.split(".")[-1])
        else:
            patch.setattr(target, path.split(".")[-1], None)
        patch.setattr(reservation_runtime, "_closed", closed)
        assert reservation_runtime.reservation_blockers() == (
            ("closed",) if closed else ("teardown",)
        )
        assert reservation_runtime._reservation_pending() is True
        assert reservation_runtime._work_pending() is True


@pytest.mark.parametrize(
    "path",
    [case[0] for case in RESERVATION_CASES]
    + [
        "command_hotkey",
        "command_mouse",
    ],
)
def test_reservation_missing_required_fields(
    reservation_runtime: Any,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
) -> None:
    with monkeypatch.context() as patch:
        target = reservation_runtime
        for part in path.split(".")[:-1]:
            target = getattr(target, part)
        patch.delattr(target, path.split(".")[-1])
        assert reservation_runtime.reservation_blockers() == ("teardown",)


def test_reservation_optional_devices_none_is_idle(
    reservation_runtime: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with monkeypatch.context() as patch:
        patch.setattr(reservation_runtime, "command_hotkey", None)
        patch.setattr(reservation_runtime, "command_mouse", None)
        assert reservation_runtime.reservation_blockers() == ()
