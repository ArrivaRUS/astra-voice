"""Temporary command-input handoff must retain only supervised menu resources."""

from __future__ import annotations

import socket
from collections.abc import Iterator
from dataclasses import asdict
from typing import Any
from unittest.mock import Mock

import pytest

from astra_voice.platform.command_hotkey import HEALTH_LIMIT_S, CommandHotkeyBackend
from astra_voice.platform.hotkey import GrabResult, HotkeyEvent, HotkeyManager, HotkeyMode

pytestmark = pytest.mark.unit


@pytest.fixture
def port(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[CommandHotkeyBackend, HotkeyManager, Mock]]:
    backend = CommandHotkeyBackend(manage_menu=True)
    peer, connection = socket.socketpair()
    backend._connection = connection
    backend._menu_token = "synthetic-lease"
    backend._combo = "Super_L"
    release = Mock()
    monkeypatch.setattr(backend, "_release_menu", release)
    monkeypatch.setattr(backend, "_pump", Mock())
    monkeypatch.setattr(backend, "poll_events", lambda: [])

    def request(operation: str, **fields: object) -> dict[str, Any]:
        if operation == "held":
            return {"value": False}
        if operation == "grab":
            return {"value": asdict(GrabResult("ok", keycode=133))}
        return {"value": None}

    monkeypatch.setattr(backend, "_request", Mock(side_effect=request))
    manager = HotkeyManager(backend)
    manager._combo = "Super_L"
    manager._keycode = 133
    manager.defer_single_super = True
    try:
        yield backend, manager, release
    finally:
        backend.close()
        peer.close()


def test_unchanged_rearm_keeps_registration(port: Any) -> None:
    backend, manager, release = port
    assert manager.rearm("Super_L", HotkeyMode.PTT).ok
    assert [c.args[0] for c in backend._request.call_args_list] == ["cancel", "held"]
    release.assert_not_called()
    assert manager.grab("Super_L", HotkeyMode.PTT).code == "duplicate"


def test_pause_rearm_preserves_broker_and_lease_but_discards_old_press(port: Any) -> None:
    backend, manager, release = port
    connection = backend._connection
    manager.handle_event(HotkeyEvent("KeyPress", 133, 1), 1.0)
    assert manager.has_pending_press
    manager.suspend()
    assert not manager.has_pending_press
    assert manager._combo is None
    assert backend._connection is connection
    assert backend._menu_token == "synthetic-lease"
    assert backend._suspended
    assert not manager.handle_event(HotkeyEvent("KeyPress", 133, 2), 2.0).ok
    assert manager.rearm("Super_L", HotkeyMode.PTT).ok
    assert manager._combo == "Super_L"
    assert not backend._suspended
    release.assert_not_called()
    assert [c.args[0] for c in backend._request.call_args_list] == ["ungrab", "grab", "held"]
    manager.handle_event(HotkeyEvent("KeyPress", 133, 3), 3.0)
    assert manager.has_pending_press


def test_full_ungrab_of_paused_manager_releases_menu(port: Any) -> None:
    backend, manager, release = port
    manager.suspend()
    manager.ungrab()
    release.assert_called_once_with()
    assert backend._connection is None
    assert manager._suspended_combo is None
    manager.ungrab()
    release.assert_called_once_with()


@pytest.mark.parametrize("combo,mode", [("F8", HotkeyMode.PTT), ("Super_L", HotkeyMode.TOGGLE)])
def test_changed_binding_closes_previous_paused_lease(
    port: Any, combo: str, mode: HotkeyMode
) -> None:
    backend, manager, release = port
    manager.suspend()
    backend.grab_combo = Mock(return_value=GrabResult("busy"))
    assert not manager.rearm(combo, mode).ok
    release.assert_called_once_with()
    assert backend._connection is None


def test_suspended_heartbeat_failure_releases_menu(
    port: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend, manager, release = port
    manager.suspend()
    backend._last_reply = 1.0
    monkeypatch.setattr("astra_voice.platform.command_hotkey.boot_time", lambda: 1 + HEALTH_LIMIT_S)
    CommandHotkeyBackend._pump(backend)
    release.assert_called_once_with()
    assert backend._connection is None
    assert not backend._suspended


def test_failed_resume_releases_menu(port: Any) -> None:
    backend, manager, release = port
    manager.suspend()
    backend._request = Mock(return_value={"value": asdict(GrabResult("busy"))})
    assert not manager.rearm("Super_L", HotkeyMode.PTT).ok
    release.assert_called_once_with()
    assert backend._connection is None


def test_unacknowledged_pause_fails_closed(port: Any) -> None:
    backend, manager, release = port
    backend._request = Mock(return_value={})
    manager.suspend()
    release.assert_called_once_with()
    assert manager._combo is None
    assert manager._suspended_combo is None
    assert backend._connection is None


def test_held_key_cannot_be_rearmed_as_fresh_gesture(port: Any) -> None:
    backend, manager, release = port
    backend._request = Mock(return_value={"value": True})
    assert not manager.rearm("Super_L", HotkeyMode.PTT).ok
    release.assert_called_once_with()
    assert manager._combo is None


def test_rearm_discards_deferred_and_queued_old_gesture(port: Any) -> None:
    backend, manager, release = port
    manager.handle_event(HotkeyEvent("KeyPress", 133, 1), 1.0)
    backend.poll_events = Mock(return_value=[HotkeyEvent("KeyPress", 133, 2, confirmed_hold=True)])
    assert manager.rearm("Super_L", HotkeyMode.PTT).ok
    assert not manager.has_pending_press
    manager.tick(10.0)
    assert manager.fsm.state.value == "idle"
    release.assert_not_called()
