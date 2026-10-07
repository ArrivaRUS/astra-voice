"""Command startup and settings must coexist with ordinary text dictation."""

from __future__ import annotations

from unittest.mock import Mock

import pytest
from test_runtime import Rig

from astra_voice.core.command_mode import SessionSnapshot
from astra_voice.platform.hotkey import GrabResult
from astra_voice.platform.session import SessionKind

pytestmark = pytest.mark.unit


def test_late_unlock_installs_notifier_after_lazy_backend_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = Rig(monkeypatch, session_kind=SessionKind.KDE)
    runtime = rig.runtime
    session = Mock()
    session.snapshot.return_value = SessionSnapshot()
    runtime._command_session = session
    manager = Mock()
    manager.fileno.return_value = -1
    manager.signature.return_value = (133, 0)
    rig.hotkey.signature.return_value = (65, 4)
    runtime.command_hotkey = manager
    assert not runtime.reload_command_hotkey()
    rig.create_notifier.assert_not_called()

    def grab(*args: object) -> GrabResult:
        manager.fileno.return_value = 23
        return GrabResult("ok")

    manager.grab.side_effect = grab
    session.snapshot.return_value = SessionSnapshot(known=True, locked=False)
    runtime._command_session_changed()
    rig.create_notifier.assert_called_once_with(23)
    assert runtime._command_notifier is rig.notifier
    # Subsequent property reads don't accumulate duplicate observers.
    runtime._command_session_changed()
    rig.create_notifier.assert_called_once_with(23)
    rig.notifier.activated.emit(23)
    manager.process_pending.assert_called_once()
    runtime.shutdown()


@pytest.mark.parametrize("snapshot", [SessionSnapshot(), SessionSnapshot(known=True, locked=False)])
def test_fly_text_capture_remains_available(
    monkeypatch: pytest.MonkeyPatch, snapshot: SessionSnapshot
) -> None:
    rig = Rig(monkeypatch, session_kind=SessionKind.FLY)
    rig.runtime._command_session = Mock(snapshot=lambda: snapshot)
    assert rig.runtime.begin_hotkey_capture(role="text")
    rig.runtime.end_hotkey_capture()
    assert not rig.runtime.begin_hotkey_capture(role="command")
    rig.runtime.shutdown()


@pytest.mark.parametrize(
    "snapshot",
    [
        SessionSnapshot(lock_requested=True),
        SessionSnapshot(known=True, locked=True),
        SessionSnapshot(preparing_for_sleep=True),
    ],
)
def test_text_capture_cannot_reopen_after_lock(
    monkeypatch: pytest.MonkeyPatch, snapshot: SessionSnapshot
) -> None:
    rig = Rig(monkeypatch, session_kind=SessionKind.FLY)
    rig.runtime._command_session = Mock(snapshot=lambda: snapshot)
    assert not rig.runtime.begin_hotkey_capture(role="text")
    rig.runtime.shutdown()


def test_hardware_escape_cancels_preview_timer_before_submit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from astra_voice.core.command_mode import CommandMode
    from astra_voice.core.dictation import DictationPhase
    from astra_voice.platform.hotkey import HotkeyBackend, HotkeyEvent, HotkeyManager, HotkeyMode

    rig = Rig(monkeypatch, session_kind=SessionKind.KDE)
    runtime = rig.runtime
    runtime._command_session = Mock(snapshot=lambda: SessionSnapshot(known=True, locked=False))
    runtime._selfcheck = "ok"
    runtime.settings.command_preview = True
    runtime.on_command_preview = Mock()
    client = Mock()
    mode = CommandMode(
        client=client,
        session=runtime._command_snapshot,
        model_trusted=lambda: True,
        publish=lambda text: True,
        present=lambda feedback: None,
    )
    runtime._command_mode = mode
    runtime.orchestrator.command_mode = mode
    runtime.orchestrator._command_preview = runtime._preview_command
    backend = Mock(spec=HotkeyBackend)
    backend.grab_combo.return_value = GrabResult("ok", keycode=133, mods=0)
    backend.grab_escape.return_value = GrabResult("ok", keycode=9)
    backend.poll_events.return_value = []
    manager = HotkeyManager(backend, clock=lambda: rig.now)
    runtime.command_hotkey = manager
    manager.on_state = runtime._on_command_hotkey_state
    assert manager.grab("Super_L", HotkeyMode.PTT).ok
    manager.handle_event(HotkeyEvent("KeyPress", 133, 1), 1.0)
    manager.handle_event(HotkeyEvent("KeyRelease", 133, 2), 2.0)
    rig.fire_tail()
    runtime.orchestrator._result("не отправлять после Escape")
    assert runtime.orchestrator.phase == DictationPhase.DELIVERING
    runtime.command_preview_shown()
    timer = runtime._preview_timer
    assert timer is not None
    manager.handle_event(HotkeyEvent("KeyPress", 9, 3, escape=True), 3.0)
    assert runtime._preview_send is None
    assert runtime._preview_timer is None
    # A stale timer callback must not Submit either.
    runtime.confirm_command()
    client.submit.assert_not_called()
    assert str(runtime.orchestrator.phase.value) == "finishing"
    runtime.shutdown()


@pytest.mark.parametrize("command_signature", [None, (65, 4)])
def test_mapping_change_revokes_command_grab_if_roles_become_ambiguous(
    monkeypatch: pytest.MonkeyPatch, command_signature: tuple[int, int] | None
) -> None:
    from astra_voice.platform.hotkey import HotkeyState

    rig = Rig(monkeypatch, session_kind=SessionKind.KDE)
    runtime = rig.runtime
    runtime.command_hotkey = Mock()
    runtime.command_hotkey.signature.return_value = command_signature
    rig.hotkey.signature.return_value = (65, 4)
    runtime._on_command_hotkey_state(HotkeyState.IDLE, "mapping-regrab:ok")
    runtime.command_hotkey.ungrab.assert_called_once()
    assert "раскладки" in runtime.command_status
    runtime.shutdown()
