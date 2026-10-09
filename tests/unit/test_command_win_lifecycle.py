"""Resource release and installation gates; no live X11, microphone or buses."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest

from astra_voice.platform.hotkey import X11HotkeyBackend
from astra_voice.platform.session import SessionKind
from astra_voice.platform.x11 import ParsedCombo, X11Display
from astra_voice.runtime import DictationRuntime


@pytest.mark.unit
def test_force_releases_implicit_grab_without_explicit_grab_deadline() -> None:
    display = X11Display()
    display.d = Mock()
    assert display.keyboard_grab_deadline is None
    display.cancel_keyboard_grab()
    display.d.ungrab_keyboard.assert_called_once_with(0)
    display.d.sync.assert_called_once_with()


@pytest.mark.unit
def test_force_release_does_not_open_an_x_connection() -> None:
    display = X11Display()
    display.open = Mock()  # type: ignore[method-assign]
    display.cancel_keyboard_grab()
    display.open.assert_not_called()


@pytest.mark.parametrize("mapping_already_lost", [False, True])
@pytest.mark.unit
def test_backend_ungrab_releases_implicit_grab_even_after_mapping_loss(
    mapping_already_lost: bool,
) -> None:
    display = X11Display()
    display.d = Mock()
    display.root = Mock()
    backend = X11HotkeyBackend(display)
    if not mapping_already_lost:
        backend._combos["Super_L"] = ParsedCombo(0, 133, "Super_L")
    backend.ungrab_combo("Super_L")
    backend.cancel_keyboard_grab()
    display.d.ungrab_keyboard.assert_called_once_with(0)


def host(installed: bool) -> Any:
    return SimpleNamespace(
        command_installed=installed,
        session_kind=SessionKind.KDE,
        start_command_mode=Mock(),
        reload_command_hotkey=Mock(),
        _command_client=Mock(),
        _stop_command_regrab=Mock(),
        reject_command=Mock(),
        command_hotkey=Mock(),
        _command_changed=Mock(),
        # Mouse integration ports: this fixture isolates the existing Win lifecycle.
        reload_command_mouse=Mock(),
        _stop_mouse=Mock(),
        _invalidate_mouse_capture=Mock(),
        _command_hold_epoch=0,
        _input_owner=None,
        _schedule_command_hold=Mock(),
    )


@pytest.mark.unit
def test_uninstall_refresh_releases_win_and_drops_pending_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = host(True)
    monkeypatch.setattr("astra_voice.runtime.is_installed", lambda: False)
    DictationRuntime.refresh_command_status(runtime)
    assert not runtime.command_available
    runtime.command_hotkey.ungrab.assert_called_once_with()
    runtime._stop_command_regrab.assert_called_once_with()
    runtime.reject_command.assert_called_once_with()
    runtime._command_client.status.assert_not_called()
    status = runtime.command_status
    DictationRuntime._command_status_changed(runtime, {"state": "ready"})
    DictationRuntime._command_owner_changed(runtime, True)
    assert runtime.command_status == status


@pytest.mark.unit
def test_reinstall_refresh_requires_new_grab(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = host(False)
    monkeypatch.setattr("astra_voice.runtime.is_installed", lambda: True)
    DictationRuntime.refresh_command_status(runtime)
    runtime.reload_command_hotkey.assert_called_once_with()
    runtime._command_client.status.assert_called_once_with()


@pytest.mark.unit
def test_unchanged_installed_refresh_does_not_cancel_active_hold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = host(True)
    monkeypatch.setattr("astra_voice.runtime.is_installed", lambda: True)
    DictationRuntime.refresh_command_status(runtime)
    runtime.reload_command_hotkey.assert_not_called()
    runtime.command_hotkey.ungrab.assert_not_called()


@pytest.mark.unit
def test_retry_cannot_regrab_after_uninstall() -> None:
    runtime = host(False)
    runtime._closed = False
    runtime._create_timer = Mock()
    DictationRuntime._start_command_regrab(runtime)
    runtime._create_timer.assert_not_called()
    assert not DictationRuntime.reload_command_hotkey(runtime)
    runtime.command_hotkey.ungrab.assert_called_once_with()
    runtime.command_hotkey.rearm.assert_not_called()


@pytest.fixture
def broker_keyboard() -> Any:
    import os
    import time

    from Xlib import XK, X, display
    from Xlib.ext import xtest

    from astra_voice.platform.command_hotkey import CommandHotkeyBackend
    from astra_voice.platform.hotkey import HotkeyManager, HotkeyMode

    if os.environ.get("ASTRA_VOICE_TEST_X11_ISOLATED") != "1" or os.environ.get("DISPLAY", "") in {
        "",
        ":0",
        ":0.0",
    }:
        pytest.skip("requires an explicitly isolated Xvfb display")
    backend = CommandHotkeyBackend()
    manager = HotkeyManager(backend)
    manager.defer_single_super = True
    source, wm = display.Display(), display.Display()
    codes = {
        name: source.keysym_to_keycode(XK.string_to_keysym(name))
        for name in (
            "Super_L",
            "Control_L",
            "Shift_L",
            "l",
            "Escape",
        )
    }
    states: list[str] = []
    manager.on_state = lambda state, reason: (
        states.append(state.value) if reason == "press" else None
    )
    assert manager.grab("Super_L", HotkeyMode.PTT).ok

    def key(name: str, pressed: bool) -> None:
        xtest.fake_input(source, X.KeyPress if pressed else X.KeyRelease, codes[name])
        source.sync()

    def pump(seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            manager.process_pending()
            manager.tick()
            time.sleep(0.005)

    rig = SimpleNamespace(
        backend=backend,
        manager=manager,
        source=source,
        wm=wm,
        codes=codes,
        states=states,
        key=key,
        pump=pump,
    )
    try:
        yield rig
    finally:
        backend.close()
        for code in codes.values():
            xtest.fake_input(source, X.KeyRelease, code)
        source.sync()
        source.close()
        wm.close()


@pytest.mark.xvfb
@pytest.mark.parametrize("modifier", [None, "Control_L", "Shift_L"])
def test_broker_replays_initial_super_to_real_root_wm_grab(
    broker_keyboard: Any, modifier: str | None
) -> None:
    from Xlib import X

    k = broker_keyboard
    mask = X.Mod4Mask | {None: 0, "Control_L": X.ControlMask, "Shift_L": X.ShiftMask}[modifier]
    k.wm.screen().root.grab_key(k.codes["l"], mask, False, X.GrabModeAsync, X.GrabModeAsync)
    k.wm.sync()
    k.key("Super_L", True)
    if modifier:
        k.key(modifier, True)
    k.key("l", True)
    k.pump(0.15)
    presses = []
    k.wm.sync()
    while k.wm.pending_events():
        event = k.wm.next_event()
        if event.type == X.KeyPress:
            assert event.window.id == k.wm.screen().root.id
            presses.append(event.detail)
    assert presses == [k.codes["l"]]
    assert k.states == []


@pytest.mark.xvfb
def test_broker_short_tap_then_hold_repeat_release_and_probe(broker_keyboard: Any) -> None:
    from astra_voice.platform.hotkey import HotkeyState

    k = broker_keyboard
    k.key("Super_L", True)
    k.key("Super_L", False)
    k.pump(0.4)
    assert k.states == []
    assert k.manager.probe("Ctrl+Shift+F11").ok
    k.key("Super_L", True)
    k.pump(1.1)  # native server autorepeat must not produce another deferred gesture
    assert k.states == ["recording"]
    k.key("Super_L", False)
    k.pump(0.1)
    assert k.manager.fsm.state == HotkeyState.PROCESSING


@pytest.mark.xvfb
@pytest.mark.parametrize("how", ["kill", "stop", "parent_stall"])
def test_broker_peer_failure_releases_keyboard_and_requires_new_gesture(
    broker_keyboard: Any,
    how: str,
) -> None:
    import os
    import signal
    import time

    from astra_voice.platform.hotkey import MappingEvent

    k = broker_keyboard
    k.key("Super_L", True)
    k.pump(0.1)
    child = k.backend.process
    assert child is not None
    if how == "parent_stall":
        time.sleep(0.75)  # no Qt/poll heartbeat, child must close its own X connection
    else:
        os.kill(child.pid, signal.SIGKILL if how == "kill" else signal.SIGSTOP)
        deadline = time.monotonic() + 0.8
        while time.monotonic() < deadline:
            k.backend._pump()
            time.sleep(0.01)
    events = k.backend.poll_events()
    assert any(
        isinstance(event, MappingEvent) and not event.combos["Super_L"].ok for event in events
    )
    assert child.poll() is not None
    with X11Display() as rival:
        assert rival.grab_keyboard()
        rival.ungrab_keyboard()
    assert k.states == []
    k.manager.ungrab()
    k.key("Super_L", False)
    from astra_voice.platform.hotkey import HotkeyMode

    assert k.manager.grab("Super_L", HotkeyMode.PTT).ok
    k.key("Super_L", True)
    k.pump(0.4)
    assert k.states == ["recording"]


@pytest.mark.unit
def test_session_epoch_change_drops_queued_confirmed_hold_before_drain() -> None:
    from astra_voice.core.command_mode import SessionSnapshot

    runtime: Any = SimpleNamespace(
        _closed=False,
        command_hotkey=Mock(),
        _command_grab_epoch=1,
        _command_snapshot=lambda: SessionSnapshot(known=True, locked=False, blocked_epoch=2),
        reload_command_hotkey=Mock(),
    )
    DictationRuntime._process_command_hotkey(runtime)
    runtime.command_hotkey.ungrab.assert_called_once_with()
    runtime.command_hotkey.process_pending.assert_not_called()
    runtime.reload_command_hotkey.assert_called_once_with()


@pytest.mark.unit
def test_broker_same_key_mapping_regrab_reports_lost_release() -> None:
    from Xlib import X

    from astra_voice.platform.hotkey import GrabResult, MappingEvent
    from astra_voice.worker.command_hotkey import Broker

    broker = Broker(Mock())
    broker.combo, broker.keycode, broker.active = "Super_L", 133, True
    broker.backend = Mock()
    broker.backend._x.pending_events.side_effect = [1, 0]
    broker.backend._x.next_event.return_value = SimpleNamespace(
        type=X.MappingNotify, request=X.MappingModifier
    )
    broker.backend._refresh_mapping.return_value = MappingEvent(
        {"Super_L": GrabResult("ok", keycode=133)},
        None,
        masks_changed=True,
        regrabbed=True,
    )
    sent: list[dict[str, Any]] = []
    broker.send = lambda kind, **fields: sent.append({"kind": kind, **fields})  # type: ignore[method-assign]
    broker.drain_x()
    assert not broker.active
    assert sent[0]["event"]["combos"]["Super_L"]["code"] == "not-grabbed"
    broker.backend.cancel_keyboard_grab.assert_called_once_with()


@pytest.mark.unit
def test_expired_broker_lease_does_not_read_queued_heartbeat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from astra_voice.worker.command_hotkey import Broker

    broker = Broker(Mock())
    broker.backend = Mock()
    broker.heartbeat_deadline = 1
    monkeypatch.setattr("astra_voice.worker.command_hotkey.boot_time", lambda: 2)
    read = Mock(side_effect=AssertionError("expired lease must not read queued input"))
    monkeypatch.setattr("astra_voice.worker.command_hotkey.select.select", read)
    broker.run()
    read.assert_not_called()
    broker.backend.close.assert_called_once_with()


@pytest.mark.xvfb
def test_gui_sigstop_cannot_keep_broker_grab_or_deliver_old_hold(broker_keyboard: Any) -> None:
    import os
    import select
    import signal
    import subprocess
    import sys
    import time
    from pathlib import Path

    import astra_voice

    k = broker_keyboard
    k.manager.ungrab()
    script = """
import sys, time
sys.path.insert(0, sys.argv[1])
from astra_voice.platform.command_hotkey import CommandHotkeyBackend
from astra_voice.platform.hotkey import HotkeyManager, HotkeyMode
backend = CommandHotkeyBackend()
manager = HotkeyManager(backend)
manager.defer_single_super = True
manager.on_press = lambda: print("START", flush=True)
assert manager.grab("Super_L", HotkeyMode.PTT).ok
print("READY", flush=True)
while True:
    manager.process_pending()
    manager.tick()
    time.sleep(.005)
"""
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(Path(astra_voice.__file__).resolve().parent.parent)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        assert process.stdout is not None
        assert select.select([process.stdout], [], [], 2)[0]
        assert process.stdout.readline() == b"READY\n"
        k.key("Super_L", True)
        time.sleep(0.04)
        os.kill(process.pid, signal.SIGSTOP)
        time.sleep(0.75)
        with X11Display() as rival:
            assert rival.grab_keyboard()
            rival.ungrab_keyboard()
        os.kill(process.pid, signal.SIGCONT)
        time.sleep(0.15)
        assert not select.select([process.stdout], [], [], 0)[0], (
            "stale confirmed hold was dispatched"
        )
    finally:
        process.kill()
        process.communicate(timeout=2)


@pytest.mark.unit
def test_bootstrap_command_broker_hardens_without_audio_or_app_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from astra_voice import bootstrap
    from astra_voice.core import audio_env
    from astra_voice.worker import command_hotkey

    order: list[str] = []
    monkeypatch.setattr(bootstrap, "_setup_sys_path", lambda path: None)
    monkeypatch.setattr(bootstrap, "_refuse_root_in_bundle", lambda: None)
    monkeypatch.setattr(bootstrap, "_harden", lambda command: order.append(command))
    forbidden = Mock(side_effect=AssertionError("broker must not initialize audio or app policy"))
    monkeypatch.setattr(bootstrap, "_appimage_policy_gate", forbidden)
    monkeypatch.setattr(audio_env, "deny_pulse_autospawn", forbidden)

    def entry(argv: list[str] | None = None) -> int:
        assert order == ["command-hotkey"]
        assert argv == ["99"]
        return 17

    monkeypatch.setattr(command_hotkey, "main", entry)
    assert bootstrap.main(["command-hotkey", "99"]) == 17
    forbidden.assert_not_called()


@pytest.mark.unit
def test_lock_unlock_inside_grab_rpc_cannot_rebind_old_gesture_to_new_epoch() -> None:
    from astra_voice.core.command_mode import SessionSnapshot
    from astra_voice.platform.hotkey import GrabResult

    snapshots = [SessionSnapshot(known=True, locked=False, blocked_epoch=1)]
    runtime = host(True)
    runtime._closed = False
    runtime.settings = SimpleNamespace(
        command_hotkey="Super_L", hotkey="Ctrl+Space", hotkey_mode="ptt", command_enabled=True
    )
    runtime._command_snapshot = lambda: snapshots[0]
    runtime.command_hotkey.signature.return_value = (133, 0)
    runtime.hotkey = Mock()
    runtime.hotkey.signature.return_value = (65, 4)
    runtime._start_command_regrab = Mock()
    runtime.command_hotkey.fileno.return_value = -1

    def grab(*args: object) -> GrabResult:
        snapshots[0] = SessionSnapshot(known=True, locked=False, blocked_epoch=2)
        return GrabResult("ok", keycode=133)

    runtime.command_hotkey.rearm.side_effect = grab
    assert not DictationRuntime.reload_command_hotkey(runtime)
    assert runtime._command_grab_epoch is None
    runtime.command_hotkey.ungrab.assert_called_once_with()


@pytest.mark.unit
def test_menu_endpoint_and_verified_lease_precede_grab_and_restore_after_cancel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dataclasses import asdict

    from astra_voice.platform.command_hotkey import CommandHotkeyBackend
    from astra_voice.platform.hotkey import GrabResult

    trace: list[str] = []
    connection = Mock()
    connection.isConnected.return_value = True
    connection.baseService.return_value = ":1.55"

    def register(*args: object) -> bool:
        trace.append("endpoint")
        return True

    connection.registerObject.side_effect = register
    fake_bus = SimpleNamespace(
        connectToBus=lambda *args: connection,
        disconnectFromBus=Mock(),
        ExportAllSlots=1,
    )
    monkeypatch.setattr("PyQt5.QtDBus.QDBusConnection", fake_bus)
    monkeypatch.setattr(
        "astra_voice.platform.cowork.resolve_bus_address", lambda: "unix:path=/fake"
    )
    lease = Mock()
    lease.recover.side_effect = lambda: trace.append("recover")
    lease.acquire.side_effect = lambda *args: trace.append("acquire")
    lease.release.side_effect = lambda token: trace.append("restore")
    monkeypatch.setattr("astra_voice.platform.kwin_command_lease.KWinCommandLease", lambda: lease)
    backend = CommandHotkeyBackend(manage_menu=True)

    def request(operation: str, **fields: object) -> dict[str, Any]:
        trace.append(operation)
        return {"kind": "reply", "value": asdict(GrabResult("ok", keycode=133))}

    backend._request = request  # type: ignore[method-assign]
    try:
        assert backend.grab_combo("Super_L").ok
        assert trace == ["recover", "endpoint", "acquire", "lease", "grab"]
        token = backend._menu_token
        assert token is not None
        # The port has no actual X child in this unit test. Cancellation drops
        # the registered combo before the configuration-only restore thread runs.
        backend.ungrab_combo("Super_L")
        assert backend._menu_thread is not None
        backend._menu_thread.join(timeout=1)
        assert backend._combo is None
        lease.release.assert_called_once_with(token)
        backend._finish_menu_release()
        fake_bus.disconnectFromBus.assert_called_once()
    finally:
        backend.close()


@pytest.mark.unit
def test_broker_menu_restore_runs_only_after_x_and_ipc_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from astra_voice.platform.kwin_command_lease import lease_token
    from astra_voice.worker.command_hotkey import Broker

    trace: list[str] = []
    connection = Mock()
    connection.close.side_effect = lambda: trace.append("ipc-close")
    broker = Broker(connection)
    broker.backend = Mock()
    broker.backend.cancel_keyboard_grab.side_effect = lambda: trace.append("ungrab")
    broker.backend.close.side_effect = lambda: trace.append("x-close")
    broker.lease_token = lease_token(":1.55", "a" * 32)
    broker.heartbeat_deadline = 0
    lease = Mock()
    lease.release.side_effect = lambda token: trace.append("restore")
    monkeypatch.setattr("astra_voice.platform.kwin_command_lease.KWinCommandLease", lambda: lease)
    broker.run()
    assert trace == ["ungrab", "x-close", "ipc-close", "restore"]


@pytest.mark.unit
def test_broker_rejects_arbitrary_paths_as_menu_lease() -> None:
    from astra_voice.worker.command_hotkey import Broker

    broker = Broker(Mock())
    with pytest.raises(ValueError, match="invalid menu lease"):
        broker.command({"op": "lease", "id": 1, "token": "/tmp/some-config"})


@pytest.mark.unit
@pytest.mark.parametrize("failure", ["endpoint", "acquire"])
def test_menu_failure_never_registers_command_key(
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    from astra_voice.platform.command_hotkey import CommandHotkeyBackend
    from astra_voice.platform.kwin_command_lease import KWinLeaseError

    connection = Mock()
    connection.isConnected.return_value = True
    connection.baseService.return_value = ":1.55"
    connection.registerObject.return_value = failure != "endpoint"
    monkeypatch.setattr(
        "PyQt5.QtDBus.QDBusConnection",
        SimpleNamespace(
            connectToBus=lambda *args: connection,
            disconnectFromBus=Mock(),
            ExportAllSlots=1,
        ),
    )
    monkeypatch.setattr(
        "astra_voice.platform.cowork.resolve_bus_address", lambda: "unix:path=/fake"
    )
    lease = Mock()
    lease.acquire.side_effect = KWinLeaseError("failure")
    monkeypatch.setattr("astra_voice.platform.kwin_command_lease.KWinCommandLease", lambda: lease)
    backend = CommandHotkeyBackend(manage_menu=True)
    request = Mock()
    backend._request = request  # type: ignore[method-assign]
    try:
        assert not backend.grab_combo("Super_L").ok
        request.assert_not_called()
        if failure == "endpoint":
            lease.acquire.assert_not_called()
    finally:
        backend.close()
    assert backend._menu_token is None
    assert backend._menu_connection is None


@pytest.mark.unit
@pytest.mark.parametrize("installed", [False, True])
def test_startup_recovers_menu_with_cowork_absent_or_command_disabled(
    monkeypatch: pytest.MonkeyPatch,
    installed: bool,
) -> None:
    runtime = host(installed)
    runtime._closed = False
    runtime._recover_command_menu = True
    runtime.settings = SimpleNamespace(command_enabled=False)
    lease = Mock()
    monkeypatch.setattr("astra_voice.platform.kwin_command_lease.KWinCommandLease", lambda: lease)
    DictationRuntime.start_command_mode(runtime)
    lease.recover.assert_called_once_with()
    assert not runtime._recover_command_menu
    runtime.command_hotkey.rearm.assert_not_called()


@pytest.mark.unit
def test_quit_after_restore_failure_keeps_token_for_startup_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from astra_voice.platform.command_hotkey import CommandHotkeyBackend
    from astra_voice.platform.kwin_command_lease import KWinLeaseError, lease_token

    lease = Mock()
    lease.release.side_effect = KWinLeaseError("still busy")
    monkeypatch.setattr("astra_voice.platform.kwin_command_lease.KWinCommandLease", lambda: lease)
    monkeypatch.setattr("astra_voice.platform.command_hotkey.time.sleep", lambda delay: None)
    backend = CommandHotkeyBackend(manage_menu=True)
    token = lease_token(":1.55", "a" * 32)
    backend._menu_token = token
    backend.close()
    assert backend.fileno() == -1
    assert lease.release.call_count == 3
    assert backend._menu_token == token  # Never report restoration that failed.


@pytest.mark.unit
@pytest.mark.parametrize("raises", [False, True])
def test_helper_launch_failure_releases_already_acquired_menu(
    monkeypatch: pytest.MonkeyPatch,
    raises: bool,
) -> None:
    from astra_voice.platform.command_hotkey import CommandHotkeyBackend
    from astra_voice.platform.kwin_command_lease import lease_token

    backend = CommandHotkeyBackend(manage_menu=True)
    backend._menu_token = lease_token(":1.55", "a" * 32)
    monkeypatch.setattr(backend, "_acquire_menu", lambda: True)
    launch = Mock(side_effect=OSError("socketpair failed")) if raises else Mock(return_value=False)
    monkeypatch.setattr(backend, "_launch", launch)
    restore = Mock()
    monkeypatch.setattr(backend, "_release_menu", restore)
    try:
        assert not backend.grab_combo("Super_L").ok
        restore.assert_called_once_with()
        assert backend.process is None
    finally:
        backend.close()


@pytest.mark.unit
@pytest.mark.parametrize(
    "message",
    [
        {"kind": "mapping", "event": {"combos": [], "escape": None}},
        {
            "kind": "key",
            "event": {
                "kind": "KeyPress",
                "keycode": True,
                "time": 1,
                "mods": 0,
                "escape": False,
                "confirmed_hold": True,
            },
        },
        {
            "kind": "key",
            "event": {
                "kind": "KeyPress",
                "keycode": 133,
                "time": 1,
                "mods": 0,
                "escape": False,
                "confirmed_hold": "true",
            },
        },
        {"kind": "reply", "id": 1, "value": {"code": "ok"}},
        {"kind": "pong", "unexpected": True},
    ],
)
def test_malformed_broker_reply_fails_closed_without_escaping_qt_slot(
    monkeypatch: pytest.MonkeyPatch,
    message: dict[str, Any],
) -> None:
    from astra_voice.platform.command_hotkey import CommandHotkeyBackend
    from astra_voice.platform.hotkey import MappingEvent

    backend = CommandHotkeyBackend()
    backend._connection = Mock()
    backend._combo = "Super_L"
    backend._sequence = 1
    backend._operation = "grab"
    backend._last_reply = backend._last_ping = 1
    monkeypatch.setattr("astra_voice.platform.command_hotkey.boot_time", lambda: 1)
    monkeypatch.setattr(
        "astra_voice.platform.command_hotkey.select.select", lambda *args: ([1], [], [])
    )
    monkeypatch.setattr("astra_voice.platform.command_hotkey.receive_packet", lambda *args: message)
    try:
        backend._pump()
        assert backend._connection is None
        events = backend.poll_events()
        assert len(events) == 1 and isinstance(events[0], MappingEvent)
        assert not events[0].combos["Super_L"].ok
    finally:
        backend.close()


@pytest.mark.unit
def test_deeply_nested_broker_json_becomes_protocol_error() -> None:
    from astra_voice.platform.command_hotkey import receive_packet

    connection = Mock()
    connection.recv.return_value = b"[" * 1500 + b"0" + b"]" * 1500
    with pytest.raises(ValueError, match="nested broker packet"):
        receive_packet(connection)
