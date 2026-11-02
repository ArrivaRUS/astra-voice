"""Application wiring publishes the inert meeting bridge without audio work."""

from __future__ import annotations

import ast
import inspect
import logging
import sys
import threading
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest

from astra_voice import app
from astra_voice.core.policy import Policy
from astra_voice.core.settings import Settings
from astra_voice.platform import userinstall
from astra_voice.platform.session import SessionKind

pytestmark = pytest.mark.unit


def test_factory_passes_raw_policy_shared_settings_and_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = ModuleType("astra_voice.ui.meeting_bridge")
    factory = Mock(return_value=object())
    module.MeetingBridge = factory  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, module.__name__, module)
    stored, effective, policy = Settings(), Settings(), Policy()
    bridge = app._make_meeting_bridge(stored, effective, policy, SessionKind.FLY)
    assert bridge is factory.return_value
    factory.assert_called_once_with(
        stored, mirror=effective, policy=policy, session=SessionKind.FLY
    )


def test_factory_error_does_not_log_raw_exception(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    module = ModuleType("astra_voice.ui.meeting_bridge")
    module.MeetingBridge = Mock(side_effect=RuntimeError("PRIVATE_MARKER"))  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, module.__name__, module)
    with caplog.at_level(logging.WARNING):
        assert app._make_meeting_bridge(Settings(), Settings(), Policy(), SessionKind.KDE) is None
    assert caplog.records and "PRIVATE_MARKER" not in caplog.text


def test_tray_bridge_updates_effective_visibility_and_recording_flag() -> None:
    from astra_voice.ui.tray_icons import TrayState

    tray = Mock()
    bridge = SimpleNamespace(
        changed=Mock(),
        enabled=False,
        recordingDevEnabled=False,
        overlay={},
        requestRecording=Mock(),
        on_start_clicked=Mock(),
        on_overlay_pause_clicked=Mock(),
        on_overlay_resume_clicked=Mock(),
        on_overlay_stop_clicked=Mock(),
        on_overlay_participants_clicked=Mock(),
        on_overlay_extend_clicked=Mock(),
        activateOverlayKeyboard=Mock(),
    )
    show = Mock()
    disconnect = app._wire_tray_meetings(tray, bridge, show)
    tray.set_meetings_enabled.assert_called_with(False)
    assert tray.set_meeting_state.call_args.args[0] is None
    tray.set_meeting_record_entry_enabled.assert_called_with(False)
    refresh = bridge.changed.connect.call_args.args[0]
    bridge.enabled, bridge.recordingDevEnabled = True, True
    bridge.overlay = {
        "visible": True,
        "active": True,
        "paused": False,
        "elapsedSeconds": 2530,
        "limitSoon": True,
        "canExtend": True,
        "canResume": True,
        "limitRemainingSeconds": 288,
    }
    refresh()
    tray.set_meetings_enabled.assert_called_with(True)
    tray.set_meeting_record_entry_enabled.assert_called_with(True)
    tray.set_meeting_state.assert_called_with(
        TrayState.MEETING_REC,
        elapsed_seconds=2530,
        limit_soon=True,
        can_extend=True,
        can_resume=True,
        mic_name="",
        limit_remaining_seconds=288,
    )
    tray.on_meeting_pause()
    bridge.on_overlay_pause_clicked.assert_called_once_with()
    bridge.overlay["paused"] = True
    refresh()
    assert tray.set_meeting_state.call_args.args[0] is TrayState.MEETING_PAUSED
    tray.on_meeting_pause()
    bridge.on_overlay_resume_clicked.assert_called_once_with()
    for callback, handler in [
        (tray.on_meeting_stop, bridge.on_overlay_stop_clicked),
        (tray.on_meeting_notice, bridge.on_overlay_participants_clicked),
        (tray.on_meeting_extend, bridge.on_overlay_extend_clicked),
        (tray.on_meeting_keyboard, bridge.activateOverlayKeyboard),
    ]:
        callback()
        handler.assert_called_once_with()
    tray.on_meeting_open()
    show.assert_called_once_with()
    disconnect()
    bridge.changed.disconnect.assert_called_once_with(refresh)
    for name in ("record", "open", "pause", "stop", "notice", "extend", "keyboard"):
        assert getattr(tray, "on_meeting_" + name) is None


@pytest.mark.parametrize(
    "state", ["connected", "no-tray", "no-bridge", "disconnect-fails", "shutdown-fails"]
)
def test_finalizer_meeting_cleanup_preserves_resource_and_lock_barriers(
    state: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Meeting failures must not bypass runtime stop or the file-worker barrier."""
    from astra_voice.ui import tray

    events: list[str] = []
    release = threading.Event()

    def work() -> None:
        release.wait(5)
        events.append("worker-finished")

    worker = threading.Thread(target=work, daemon=False)

    def disconnect() -> None:
        events.append("disconnect")
        if state == "disconnect-fails":
            raise RuntimeError("PRIVATE_DISCONNECT_MARKER")

    def shutdown() -> None:
        events.append("meeting-stop")
        if state == "shutdown-fails":
            raise RuntimeError("PRIVATE_SHUTDOWN_MARKER")

    def stop_runtime() -> None:
        events.append("runtime-stop")
        release.set()

    def cleanup(_server: object, _lock: object) -> bool:
        assert not worker.is_alive(), "instance lock released before file worker stopped"
        events.append("unlock")
        return True

    monkeypatch.setattr(tray, "_bus_transport", None)
    monkeypatch.setattr(app, "_installed_code_key", lambda: "test-running-copy")
    monkeypatch.setattr(app, "_children_stopped", lambda: True)
    monkeypatch.setattr(userinstall, "finish_running_copy", lambda _key: events.append("finish"))
    monkeypatch.setattr(
        userinstall, "preserve_incomplete_shutdown", lambda _key: events.append("preserve")
    )
    namespace = dict(vars(app))
    namespace.update(
        runtime=SimpleNamespace(supervisor=None, _switch_candidate=None, shutdown=stop_runtime),
        appimage_management=SimpleNamespace(
            worker_threads=(worker,), close=lambda: not worker.is_alive()
        ),
        meeting_bridge=None if state == "no-bridge" else SimpleNamespace(shutdown=shutdown),
        downloads=None,
        update_checker=None,
        onboarding=None,
        focuser=Mock(),
        timer=Mock(),
        theme_bridge=None,
        close_watcher=None,
        server=None,
        lock=None,
        shutdown_notify_dispatch=lambda: events.append("notify-stop"),
        shutdown_bus_threads=lambda: events.append("bus-stop"),
        _cleanup=cleanup,
    )
    if state != "no-tray":
        namespace["meeting_tray_disconnect"] = disconnect
    function = ast.parse(inspect.getsource(app.main)).body[0]
    assert isinstance(function, ast.FunctionDef)
    finalizers = [node for node in function.body if isinstance(node, ast.Try) and node.finalbody]
    assert len(finalizers) == 1
    code = compile(
        ast.Module(body=finalizers[0].finalbody, type_ignores=[]), "<actual-finalizer>", "exec"
    )
    worker.start()
    try:
        with caplog.at_level(logging.WARNING):
            exec(code, namespace)
    finally:
        release.set()
        worker.join(timeout=2)
    assert not worker.is_alive()
    failed = state in {"disconnect-fails", "shutdown-fails"}
    meeting_events = [] if state == "no-bridge" else ["meeting-stop"]
    if state not in {"no-tray", "no-bridge"}:
        meeting_events.insert(0, "disconnect")
    assert events[: len(meeting_events) + 1] == [*meeting_events, "runtime-stop"]
    assert events.index("runtime-stop") < events.index("worker-finished")
    assert events.index("worker-finished") < events.index("preserve" if failed else "finish")
    assert events[-2:] == ["preserve" if failed else "finish", "unlock"]
    assert "notify-stop" in events and "bus-stop" in events
    assert ("meeting-bridge-shutdown-failed" in caplog.text) is failed
    assert "PRIVATE_DISCONNECT_MARKER" not in caplog.text
    assert "PRIVATE_SHUTDOWN_MARKER" not in caplog.text


@pytest.mark.parametrize("can_resume", [False, True])
def test_tray_wiring_forwards_resume_admission(can_resume: bool) -> None:
    tray = Mock()
    bridge = Mock(enabled=True, recordingDevEnabled=True)
    bridge.overlay = {
        "visible": True,
        "active": True,
        "paused": True,
        "state": "paused-lock",
        "canResume": can_resume,
    }
    disconnect = app._wire_tray_meetings(tray, bridge, Mock())
    try:
        assert tray.set_meeting_state.call_args.kwargs["can_resume"] is can_resume
        tray.on_meeting_pause()
        if can_resume:
            bridge.on_overlay_resume_clicked.assert_called_once_with()
        else:
            bridge.on_overlay_resume_clicked.assert_not_called()
        bridge.overlay.update(paused=False, state="recording", canResume=False)
        refresh = bridge.changed.connect.call_args.args[0]
        refresh()
        tray.on_meeting_pause()
        bridge.on_overlay_pause_clicked.assert_called_once_with()
    finally:
        disconnect()
