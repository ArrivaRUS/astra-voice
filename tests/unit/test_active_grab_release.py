"""Forced cancellation releases an activated passive grab without a live display."""

from __future__ import annotations

from unittest.mock import Mock, call

import pytest
from test_command_mapping import CommandRig
from test_hotkey_fsm import FakeBackend

from astra_voice.core.command_mode import SessionSnapshot
from astra_voice.platform.hotkey import (
    GrabResult,
    HotkeyEvent,
    HotkeyManager,
    HotkeyMode,
    HotkeyState,
    MappingEvent,
    X11HotkeyBackend,
)
from astra_voice.platform.x11 import ParsedCombo, X11Display

pytestmark = pytest.mark.unit


def test_forced_ungrab_has_no_capture_deadline_and_syncs_same_connection() -> None:
    x = X11Display()
    conn = Mock()
    x.d = conn
    assert x.keyboard_grab_deadline is None
    x.ungrab_keyboard()
    conn.assert_not_called()
    assert conn.mock_calls == []
    x.cancel_keyboard_grab()
    assert conn.mock_calls == [call.ungrab_keyboard(0), call.sync()]


def test_forced_ungrab_preserves_capture_retry_on_error() -> None:
    x = X11Display()
    x.d = Mock()
    x.d.sync.side_effect = RuntimeError("connection lost")
    x.keyboard_grab_deadline = 100.0
    x.cancel_keyboard_grab()
    assert x.keyboard_grab_deadline == 100.0
    x.d.sync.side_effect = None
    x.cancel_keyboard_grab()
    assert x.keyboard_grab_deadline is None
    X11Display().cancel_keyboard_grab()  # Closed connection remains harmless.


@pytest.mark.parametrize("lost", [False, True])
def test_manager_cancels_active_grab_even_after_passive_mapping_loss(
    monkeypatch: pytest.MonkeyPatch, lost: bool
) -> None:
    x = X11Display()
    conn = Mock()
    x.d = conn
    x.root = conn.root
    conn.pending_events.return_value = 0
    monkeypatch.setattr(x, "parse_combo", lambda combo: ParsedCombo(0, 133, combo))
    backend = X11HotkeyBackend(x)
    manager = HotkeyManager(backend)
    assert manager.grab("Super_L", HotkeyMode.PTT).ok
    if lost:
        backend._combos.clear()
    conn.reset_mock()
    manager.ungrab()
    calls = conn.mock_calls
    release = calls.index(call.ungrab_keyboard(0))
    assert calls[release + 1] == call.sync()
    assert calls.index(call.pending_events()) > release + 1
    assert not backend._requested_combos
    assert manager.fsm.state == HotkeyState.IDLE


class HeldBackend(FakeBackend):
    def __init__(self) -> None:
        super().__init__()
        self.held = False
        self.cancellations = 0

    def key_is_down(self, keycode: int) -> bool:
        return self.held

    def cancel_keyboard_grab(self) -> None:
        self.cancellations += 1


def test_probe_and_unchanged_mapping_do_not_cancel_keyboard_grab() -> None:
    backend = HeldBackend()
    manager = HotkeyManager(backend)
    assert manager.grab("Super_L", HotkeyMode.PTT).ok
    manager.handle_event(HotkeyEvent("KeyPress", 65, 1), 0.0)
    assert manager.probe("F8").ok
    manager._handle_mapping(MappingEvent({"Super_L": GrabResult("ok", keycode=65)}, None))
    assert manager.fsm.state == HotkeyState.RECORDING
    assert backend.cancellations == 0


@pytest.mark.parametrize("mapping_loss", [False, True])
def test_held_key_recovery_requires_release_then_fresh_press(mapping_loss: bool) -> None:
    backend = HeldBackend()
    manager = HotkeyManager(backend)
    manager.defer_single_super = True
    manager.on_press = Mock()
    assert manager.grab("Super_L", HotkeyMode.PTT).ok
    manager.handle_event(HotkeyEvent("KeyPress", 65, 1), 0.0)
    backend.held = True
    if mapping_loss:
        backend.grabbed.clear()
        manager._handle_mapping(MappingEvent({"Super_L": GrabResult("busy")}, None))
    else:
        manager.ungrab()
        assert backend.cancellations == 1
    assert manager.grab("Super_L", HotkeyMode.PTT).code == "not-grabbed"
    backend.events = [
        HotkeyEvent("KeyPress", 65, 2),
        HotkeyEvent("KeyRelease", 65, 3),
        HotkeyEvent("KeyPress", 65, 3),  # Core X11 autorepeat pair, not a fresh gesture.
    ]
    manager.process_pending(now=1.0)
    manager.tick(2.0)
    manager.on_press.assert_not_called()
    backend.held = False
    # Release went to another X11 client: no KeyRelease reaches this manager.
    assert manager.grab("Super_L", HotkeyMode.PTT).ok
    manager.tick(4.0)
    manager.on_press.assert_not_called()
    manager.handle_event(HotkeyEvent("KeyPress", 65, 5), 5.0)
    manager.tick(5.31)
    manager.on_press.assert_called_once()


def test_cancel_and_rearm_inside_callback_discards_old_batch() -> None:
    backend = HeldBackend()
    manager = HotkeyManager(backend)
    assert manager.grab("Super_L", HotkeyMode.PTT).ok
    starts: list[float] = []

    def rearm() -> None:
        starts.append(1.0)
        manager.ungrab()
        assert manager.grab("Super_L", HotkeyMode.PTT).ok

    manager.on_press = rearm
    backend.events = [HotkeyEvent("KeyPress", 65, 1), HotkeyEvent("KeyPress", 65, 2)]
    manager.process_pending(now=0.0)
    assert starts == [1.0]
    assert manager.fsm.state == HotkeyState.IDLE


@pytest.mark.parametrize(
    "snapshot",
    [
        SessionSnapshot(lock_requested=True),
        SessionSnapshot(known=True, locked=True),
        SessionSnapshot(preparing_for_sleep=True),
        SessionSnapshot(),
    ],
)
@pytest.mark.parametrize("pending", [False, True])
def test_runtime_session_cancellation_releases_active_grab(
    monkeypatch: pytest.MonkeyPatch, snapshot: SessionSnapshot, pending: bool
) -> None:
    r = CommandRig(monkeypatch)
    cancel = Mock()
    r.backend.cancel_keyboard_grab = cancel
    r.press(pending=pending)
    r.snapshot = snapshot
    r.runtime._command_session_changed()
    cancel.assert_called_once()
    r.manager.tick(r.rig.now + 1)
    assert r.manager.fsm.state == HotkeyState.IDLE
    assert r.rig.trace.count("record.start") == (0 if pending else 1)
    r.client.submit.assert_not_called()
    r.runtime.shutdown()


def test_x11_physical_key_check_and_unknown_state() -> None:
    x = X11Display()
    x.d = Mock()
    backend = X11HotkeyBackend(x)
    keymap = bytearray(32)
    x.d.query_keymap.return_value = keymap
    assert not backend.key_is_down(133)
    keymap[133 // 8] |= 1 << (133 % 8)
    assert backend.key_is_down(133)
    x.d.query_keymap.side_effect = RuntimeError("connection lost")
    assert backend.key_is_down(133)
