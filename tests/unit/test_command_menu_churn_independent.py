"""Independent gesture/menu lease contracts with real runtime and state machines.

Only fake external broker/X11/DBus/config-command ports are used. KWin lease
journals and configuration are real temporary files; no host settings change.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest
from test_command_mouse_runtime import MouseRuntimeRig
from test_kwin_command_lease import ConfigRunner

from astra_voice.core.dictation import DictationPhase
from astra_voice.platform.command_hotkey import CommandHotkeyBackend
from astra_voice.platform.cowork import DeliveryResult
from astra_voice.platform.hotkey import GrabResult, HotkeyEvent, HotkeyManager
from astra_voice.platform.kwin_command_lease import KWinCommandLease, MetaValue
from astra_voice.ui.pill import STATE_DURATION_MS, PillState
from helpers.qt_app import get_qapplication

pytestmark = pytest.mark.unit
_REAL_GETEUID = os.geteuid


class MenuBench:
    def __init__(self, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        get_qapplication()
        config = root / "config"
        config.mkdir(mode=0o700)
        self.runner = ConfigRunner(config / "kwinrc", MetaValue(False))
        self.lease = KWinCommandLease(
            state_dir=root / "lease",
            config_path=self.runner.config,
            runner=self.runner,
            timeout=0.2,
        )
        assert self.lease.recover() is False
        monkeypatch.setattr(
            "astra_voice.platform.kwin_command_lease.KWinCommandLease", lambda: self.lease
        )
        connection = Mock()
        connection.isConnected.return_value = True
        connection.registerObject.return_value = True
        connection.baseService.return_value = ":1.77"
        monkeypatch.setattr(
            "PyQt5.QtDBus.QDBusConnection",
            SimpleNamespace(
                connectToBus=lambda *_args: connection, disconnectFromBus=Mock(), ExportAllSlots=1
            ),
        )
        monkeypatch.setattr(
            "astra_voice.platform.cowork.resolve_bus_address", lambda: "unix:path=/nonexistent"
        )
        self.mouse = MouseRuntimeRig(monkeypatch)
        self.runtime = self.mouse.runtime
        assert self.runtime.command_hotkey is not None
        self.runtime.command_hotkey.ungrab()
        self.backend = CommandHotkeyBackend(manage_menu=True)
        self.fail_grab = False
        self.win_held = False
        self.operations: list[str] = []
        monkeypatch.setattr(self.backend, "_request", self.request)
        monkeypatch.setattr(self.backend, "poll_events", lambda: [])
        # No broker process/socket is launched. These are external heartbeat/IPC
        # ports; product lease handling, backend methods and FSM remain real.
        monkeypatch.setattr(self.backend, "_pump", lambda *_args: None)
        self.manager = HotkeyManager(self.backend, clock=lambda: self.mouse.rig.now)
        self.manager.on_state = self.runtime._on_command_hotkey_state
        self.runtime.command_hotkey = self.manager
        assert self.runtime.reload_command_hotkey()
        self.join_menu()
        self.active_meta = self.runner.meta
        assert self.active_meta.present and self.active_meta != MetaValue(False)
        self.baseline = self.reconfigures()
        assert self.baseline == 1

    def request(self, operation: str, **fields: object) -> dict[str, Any]:
        self.operations.append(operation)
        if self.backend._connection is None:
            self.backend._connection = Mock()
        combo = str(fields.get("combo", "Super_L"))
        key, mods = (133, 0) if combo == "Super_L" else (45, 12)
        if operation == "signature":
            return {"value": [key, mods]}
        if operation == "held":
            return {"value": self.win_held}
        result = GrabResult(
            "not-grabbed" if operation == "grab" and self.fail_grab else "ok",
            keycode=key,
            mods=mods,
        )
        return {"kind": "reply", "value": asdict(result)}

    def join_menu(self) -> None:
        thread = self.backend._menu_thread
        if thread is not None:
            thread.join(timeout=2)
            assert not thread.is_alive(), "menu lease restore worker did not stop"

    def reconfigures(self) -> int:
        return sum(Path(call[0]).name == "qdbus" for call in self.runner.calls)

    def churn(self) -> int:
        self.join_menu()
        return self.reconfigures() - self.baseline

    def restore_inputs(self) -> None:
        for _ in range(2):
            for timer in list(self.mouse.rig.timers):
                if (
                    timer.active
                    and timer is not self.runtime._mouse_tick_timer
                    and timer.interval in (0, 1000)
                ):
                    timer.fire()
            self.join_menu()
        if self.manager._combo is None:
            self.runtime.reload_command_hotkey()
        self.join_menu()

    def keyboard_press(self) -> None:
        self.manager.handle_event(HotkeyEvent("KeyPress", 133, 1), self.mouse.rig.now)
        self.mouse.rig.now += 0.301
        self.manager.tick()

    def keyboard_release(self) -> None:
        self.manager.handle_event(HotkeyEvent("KeyRelease", 133, 2), self.mouse.rig.now)

    def complete_command(self) -> None:
        self.mouse.rig.fire_tail()
        self.runtime.orchestrator._result("synthetic command")
        self.runtime.orchestrator._command_finished(
            DeliveryResult(outcome="delivered", reason="accepted")
        )
        self.restore_inputs()
        # Completion publishes feedback for COMMAND_DONE's normal duration.
        # Our manual timers must advance that phase before asserting full idle;
        # no direct phase assignment or product callback substitution is used.
        if self.runtime.phase is DictationPhase.FINISHING:
            duration = STATE_DURATION_MS[PillState.COMMAND_DONE]
            feedback = [
                timer
                for timer in self.mouse.rig.timers
                if timer.active and timer.interval == duration
            ]
            assert len(feedback) == 1, "exactly one completion feedback timer expected"
            churn = self.churn()
            feedback[0].fire()
            assert self.runtime.orchestrator.phase is DictationPhase.IDLE
            assert self.churn() == churn, "feedback timeout must not reconfigure KWin"
            self.restore_inputs()

    def close(self) -> None:
        self.runtime.shutdown()
        self.join_menu()
        self.backend.close()
        self.join_menu()


@pytest.fixture
def bench(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, nonroot_euid: None
) -> Iterator[MenuBench]:
    # The shared fixture fakes1000 even on root CI. These real temporary lease
    # files must use the kernel identity that owns them; keep all guards real.
    monkeypatch.setattr(os, "geteuid", _REAL_GETEUID)
    assert tmp_path.stat().st_uid == os.geteuid()
    candidate = MenuBench(tmp_path, monkeypatch)
    try:
        yield candidate
    finally:
        candidate.close()


@pytest.mark.parametrize("source", ["win", "mouse", "short-mouse", "short-win"])
def test_ordinary_gesture_keeps_menu_lease_without_reconfigure(
    bench: MenuBench, source: str
) -> None:
    if source == "win":
        bench.keyboard_press()
        assert bench.runtime.phase is DictationPhase.RECORDING
    elif source == "mouse":
        bench.mouse.record_mouse()
    elif source == "short-mouse":
        bench.mouse.press()
    else:
        bench.manager.handle_event(HotkeyEvent("KeyPress", 133, 1), bench.mouse.rig.now)
    assert bench.churn() == 0, "gesture start must not reconfigure KWin"
    assert bench.runner.meta == bench.active_meta
    if source in ("win", "short-win"):
        bench.keyboard_release()
    else:
        bench.mouse.release()
    if source in ("win", "mouse"):
        bench.complete_command()
    else:
        bench.restore_inputs()
        assert not bench.mouse.sent("record.start")
    assert bench.runtime.phase is DictationPhase.IDLE
    assert bench.churn() == 0, "gesture completion must not reconfigure KWin"
    assert bench.runner.meta == bench.active_meta


def test_unchanged_settings_refresh_keeps_menu_lease(bench: MenuBench) -> None:
    for _ in range(3):
        assert bench.runtime.reload_command_hotkey()
        bench.join_menu()
    assert bench.churn() == 0
    assert bench.runner.meta == bench.active_meta


@pytest.mark.parametrize("paused", [False, True])
@pytest.mark.parametrize(
    "gate",
    [
        "shutdown",
        "disabled",
        "absence",
        "locked",
        "unknown",
        "sleep",
        "different-hotkey",
        "empty-hotkey",
        "backend-failure",
    ],
)
def test_safety_gate_restores_menu_once_even_while_paused(
    bench: MenuBench, monkeypatch: pytest.MonkeyPatch, paused: bool, gate: str
) -> None:
    if paused:
        bench.mouse.press()
        assert bench.churn() == 0
        assert bench.runner.meta == bench.active_meta
    if gate == "shutdown":
        bench.runtime.shutdown()
    elif gate == "disabled":
        bench.runtime.settings.command_enabled = False
        bench.runtime.reload_command_hotkey()
    elif gate == "absence":
        monkeypatch.setattr("astra_voice.runtime.is_installed", lambda: False)
        bench.runtime.refresh_command_status()
    elif gate in ("locked", "unknown", "sleep"):
        values = {
            "locked": {"locked": True},
            "unknown": {"known": False},
            "sleep": {"preparing_for_sleep": True},
        }[gate]
        bench.mouse.snapshot = replace(bench.mouse.snapshot, blocked_epoch=1, **values)
        bench.runtime._command_session_changed(bench.mouse.snapshot)
    elif gate == "different-hotkey":
        bench.runtime.settings.command_hotkey = "Ctrl+Alt+K"
        bench.runtime.reload_command_hotkey()
    elif gate == "empty-hotkey":
        bench.runtime.settings.command_hotkey = ""
        bench.runtime.reload_command_hotkey()
    else:
        bench.backend._fail()
    bench.join_menu()
    assert bench.runner.meta == MetaValue(False), gate
    assert bench.churn() == 1, "full release must restore menu exactly once"
    if gate != "different-hotkey":
        bench.close()
        assert bench.churn() == 1, "idempotent teardown must not restore twice"


def test_mouse_pending_cannot_cross_trigger_win_or_text(bench: MenuBench) -> None:
    bench.mouse.press()
    assert bench.runtime._input_owner == "mouse"
    bench.keyboard_press()
    bench.keyboard_release()
    bench.runtime.hotkey.handle_event(HotkeyEvent("KeyPress", 65, 3, mods=4), bench.mouse.rig.now)
    bench.runtime.hotkey.handle_event(HotkeyEvent("KeyRelease", 65, 4, mods=4), bench.mouse.rig.now)
    assert not bench.mouse.sent("record.start")
    bench.mouse.release()
    bench.restore_inputs()
    assert bench.runtime._input_owner is None
    assert not bench.mouse.sent("record.start")
    bench.keyboard_press()
    assert len(bench.mouse.sent("record.start")) == 1
    assert bench.runtime.orchestrator.mode == "command"
    bench.keyboard_release()
    bench.complete_command()
    assert bench.churn() == 0


def test_win_recording_cannot_cross_trigger_mouse(bench: MenuBench) -> None:
    bench.keyboard_press()
    before = len(bench.mouse.sent("record.start"))
    assert before == 1
    bench.mouse.press()
    bench.mouse.rig.now += 0.4
    if bench.runtime._mouse_tick_timer is not None:
        bench.mouse.fire_deadline()
    bench.mouse.release()
    assert len(bench.mouse.sent("record.start")) == before
    bench.keyboard_release()
    bench.complete_command()
    assert bench.churn() == 0
    bench.mouse.record_mouse()
    assert len(bench.mouse.sent("record.start")) == before + 1
    bench.mouse.release()
    bench.complete_command()
    assert bench.churn() == 0


def test_release_after_resume_rejects_stale_held_win(bench: MenuBench) -> None:
    bench.mouse.press()
    bench.win_held = True
    bench.manager.handle_event(HotkeyEvent("KeyPress", 133, 1), bench.mouse.rig.now)
    bench.mouse.release()
    bench.restore_inputs()
    bench.mouse.rig.now += 0.4
    bench.manager.tick()
    assert not bench.mouse.sent("record.start"), "held Win must not become a fresh gesture"
    bench.win_held = False
    bench.keyboard_release()
    bench.restore_inputs()
    bench.keyboard_press()
    assert len(bench.mouse.sent("record.start")) == 1
    bench.keyboard_release()
    bench.complete_command()


def test_failed_rearm_still_restores_menu(bench: MenuBench) -> None:
    bench.mouse.press()
    bench.fail_grab = True
    bench.mouse.release()
    bench.restore_inputs()
    assert bench.runtime.phase is DictationPhase.IDLE
    assert not bench.mouse.sent("record.start")
    assert bench.runner.meta == MetaValue(False)
    # The retry timer can attempt another acquisition; disable it before counting
    # the completed release. Every attempt must leave no leaked menu lease.
    bench.runtime.settings.command_enabled = False
    bench.runtime.reload_command_hotkey()
    bench.join_menu()
    assert bench.runner.meta == MetaValue(False)
