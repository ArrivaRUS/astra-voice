"""Playback is entirely fake; PCM checks never access a sound server."""

from __future__ import annotations

import math
import os
import struct
import subprocess
import threading
import time
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest
from test_dictation import Rig

from astra_voice.core import recording_cues as cues
from astra_voice.core.settings import Settings, from_dict
from astra_voice.ui.bridges import SettingsBridge
from astra_voice.worker import ipc

pytestmark = pytest.mark.unit


def wait_until(predicate: Any) -> None:
    deadline = time.monotonic() + 2
    while not predicate():
        assert time.monotonic() < deadline
        time.sleep(0.005)


class Child:
    def __init__(self, finish: threading.Event) -> None:
        self.finish = finish
        self.stdin = BytesIO()
        self.stdout = self.stderr = None
        self.returncode: int | None = None
        self.payload = b""
        self.terminated = False

    def communicate(self, input: bytes | None = None, timeout: float = 0) -> None:
        if input is not None:
            self.payload += input
        if not self.finish.wait(timeout):
            raise subprocess.TimeoutExpired("fake", timeout)
        self.returncode = 0

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    def kill(self) -> None:
        self.returncode = -9

    def wait(self, timeout: float) -> int:
        if self.returncode is None:
            raise subprocess.TimeoutExpired("fake", timeout)
        return self.returncode


class Factory:
    def __init__(self) -> None:
        self.children: list[Child] = []
        self.calls: list[dict[str, Any]] = []
        self.finish = threading.Event()

    def __call__(self, argv: list[str], **kwargs: Any) -> Child:
        assert argv[0] == "/usr/bin/paplay"
        assert kwargs["shell"] is False and kwargs["close_fds"] is True
        config = Path(kwargs["env"]["PULSE_CLIENTCONFIG"])
        assert config.read_text() == "autospawn = no\n"
        assert config.stat().st_mode & 0o777 == 0o600
        import fcntl

        fd = kwargs["pass_fds"][0]
        assert str(config) == f"/proc/self/fd/{fd}"
        assert fcntl.fcntl(fd, fcntl.F_GET_SEALS) & fcntl.F_SEAL_WRITE
        with pytest.raises(OSError):
            os.write(fd, b"autospawn = yes\n")
        self.calls.append(kwargs)
        child = Child(self.finish)
        self.children.append(child)
        return child


@pytest.mark.parametrize("kind", ["start", "stop"])
def test_pcm_bounds_and_pitch_direction(kind: str) -> None:
    data = cues.cue_pcm(kind)
    values = struct.unpack(f"<{len(data) // 2}h", data)
    assert 0.1 <= len(values) / cues.RATE <= 0.12
    assert max(abs(value) for value in values) <= math.ceil(32767 * 10 ** (-18 / 20))
    assert values[0] == values[-1] == 0
    windows = (values[480:1920], values[3360:4800])
    crossings = [
        sum(a <= 0 < b for a, b in zip(window, window[1:], strict=False)) for window in windows
    ]
    assert (crossings[0] < crossings[1]) == (kind == "start")


@pytest.mark.parametrize("value", [None, "true", 1, [], {}, False])
def test_nonboolean_optin_is_off(value: object) -> None:
    assert from_dict({"sound_cues_enabled": value}).sound_cues_enabled is False


def test_default_silent_and_missing_backend_truthful() -> None:
    factory = Factory()
    player = cues.RecordingCues(popen=factory, available=lambda: False)
    player.play("start", (1, "u"))
    assert player.thread is None and not factory.children
    assert player.status == cues.UNAVAILABLE
    player.configure(enabled=True)
    player.play("start", (1, "u"))
    assert player.thread is None
    player.shutdown()


def test_fifo_duplicate_stale_and_disable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PULSE_CLIENTCONFIG", "/invalid/foreign-autospawn.conf")
    monkeypatch.setenv("PULSE_SERVER", "unix:/private-test-socket")
    factory = Factory()
    player = cues.RecordingCues(enabled=True, popen=factory, available=lambda: True)
    try:
        player.play("start", (1, "u"))
        wait_until(lambda: len(factory.children) == 1 and factory.children[0].payload)
        player.play("start", (1, "u"))
        player.play("stop", (1, "u"))
        player.play("stop", (1, "u"))
        factory.finish.set()
        wait_until(lambda: len(factory.children) == 2 and player.process is None)
        assert [child.payload for child in factory.children] == [
            cues.cue_pcm("start"),
            cues.cue_pcm("stop"),
        ]
        assert factory.calls[0]["env"]["PULSE_SERVER"] == "unix:/private-test-socket"
        player.configure(enabled=False)
        player.play("start", (1, "v"))
        assert len(factory.children) == 2
    finally:
        player.shutdown()
    assert player.thread is not None and not player.thread.is_alive()
    assert not os.path.exists(factory.calls[0]["env"]["PULSE_CLIENTCONFIG"])


def test_late_queued_start_cancelled_when_recording_stops() -> None:
    factory = Factory()
    player = cues.RecordingCues(enabled=True, popen=factory, available=lambda: True)
    try:
        player.play("start", (1, "one"))
        wait_until(lambda: len(factory.children) == 1 and factory.children[0].payload)
        player.play("start", (1, "two"))
        player.play("stop", (1, "two"))
        factory.finish.set()
        wait_until(lambda: len(factory.children) == 2 and player.process is None)
        assert factory.children[1].payload == cues.cue_pcm("stop")
    finally:
        player.shutdown()


def test_disable_invalidates_inflight_queue_and_session() -> None:
    factory = Factory()
    player = cues.RecordingCues(enabled=True, popen=factory, available=lambda: True)
    player.play("start", (1, "u"))
    wait_until(lambda: len(factory.children) == 1)
    player.play("stop", (1, "u"))
    player.configure(enabled=False)
    wait_until(lambda: player.process is None)
    player.configure(enabled=True)
    player.play("stop", (1, "u"))
    player.shutdown()
    assert len(factory.children) == 1 and factory.children[0].terminated


def test_shutdown_during_spawn_registers_then_reaps() -> None:
    entered, release = threading.Event(), threading.Event()
    child = Child(threading.Event())

    def spawn(*args: Any, **kwargs: Any) -> Child:
        entered.set()
        assert release.wait(2)
        return child

    player = cues.RecordingCues(enabled=True, popen=spawn, available=lambda: True)
    player.play("start", (1, "u"))
    assert entered.wait(1)
    with pytest.raises(RuntimeError, match="завершить"):
        player.shutdown()
    assert player.thread is not None and player.thread.is_alive()
    release.set()
    wait_until(lambda: not player.thread.is_alive())
    player.shutdown()
    assert child.terminated and not child.payload and player.process is None


def test_timeout_is_bounded_and_failure_visible(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cues, "PLAY_TIMEOUT", 0.08)
    factory = Factory()
    player = cues.RecordingCues(enabled=True, popen=factory, available=lambda: True)
    try:
        player.play("start", (1, "u"))
        wait_until(lambda: factory.children and factory.children[0].terminated)
        assert player.status == cues.FAILED
    finally:
        player.shutdown()


def test_bridge_persists_and_rolls_back() -> None:
    saved: list[dict[str, Any]] = []
    settings = Settings()
    bridge = SettingsBridge(settings, save=lambda s: saved.append(s.to_dict()))
    assert bridge.property("soundCuesEnabled") is False
    bridge.setProperty("soundCuesEnabled", True)
    assert saved[-1]["sound_cues_enabled"] is True
    assert SettingsBridge(from_dict(saved[-1])).property("soundCuesEnabled") is True

    def denied(settings: Settings) -> None:
        raise OSError("fake")

    bridge._save = denied
    bridge.setProperty("soundCuesEnabled", False)
    assert bridge.property("soundCuesEnabled") is True and settings.sound_cues_enabled
    assert bridge.saveError


def boundary(rig: Rig, kind: str, uid: str | None = None, generation: int = 1) -> None:
    rig.core.on_worker_event(
        {
            "type": "record." + kind,
            "utterance_id": uid or rig.core._utterance_id,
            "generation": generation,
        }
    )


@pytest.mark.parametrize("mode", ["text", "command"])
def test_orchestrator_boundaries_not_phase_and_exactly_once(mode: str) -> None:
    from unittest.mock import Mock

    from astra_voice.platform.hotkey import HotkeyState

    rig = Rig()
    rig.core.command_mode = Mock(allowed=True, publication_allowed=True)
    seen: list[str] = []
    rig.core._recording_cue = lambda kind, key: seen.append(kind)
    rig.core.on_hotkey_state(HotkeyState.RECORDING, "press", mode=mode)
    assert seen == []
    boundary(rig, "started", uid="foreign")
    boundary(rig, "started", generation=0)
    assert seen == []
    boundary(rig, "started")
    boundary(rig, "started")
    rig.core.on_hotkey_state(HotkeyState.PROCESSING, "release")
    assert seen == ["start"]
    rig.core.cancel("escape")
    assert seen == ["start"]
    boundary(rig, "stopped")
    boundary(rig, "stopped")
    assert seen == ["start", "stop"]


def test_test_recording_never_emits_cues() -> None:
    rig = Rig()
    seen: list[str] = []
    rig.core._recording_cue = lambda kind, key: seen.append(kind)
    assert rig.core.start_test("fake", lambda update: None)
    boundary(rig, "started")
    boundary(rig, "stopped")
    assert seen == []


@pytest.mark.parametrize("kind", ["record.started", "record.stopped"])
def test_boundary_ipc_roundtrip(kind: str) -> None:
    message = {"type": kind, "utterance_id": "u"}
    assert ipc.FrameReader().feed(ipc.encode(message)) == [message]
    with pytest.raises(ipc.FrameError):
        ipc.encode({"type": kind})


@pytest.mark.parametrize("close_fails", [False, True])
def test_capture_markers_follow_open_and_successful_close(close_fails: bool) -> None:
    from unittest.mock import Mock

    from astra_voice.worker.audio import AudioCapture

    source = Mock()
    source.is_open = False
    source.device_change = None
    source.device_label = None
    source.live = False
    source.ended = True
    source.selected_device = None

    def opened(device: object, **kwargs: Any) -> None:
        source.is_open = True

    def closed() -> None:
        if close_fails:
            raise RuntimeError("fake close failure")
        source.is_open = False

    source.open.side_effect = opened
    source.close.side_effect = closed
    source.read_chunk.return_value = None
    events: list[tuple[str, bool]] = []

    def event(message: dict[str, Any]) -> None:
        events.append((message["type"], source.is_open))
        if message["type"].startswith("record."):
            assert message["utterance_id"] == "u"

    capture = AudioCapture(
        source=source, on_samples=lambda uid, pcm: True, on_event=event, on_error=Mock()
    )
    running = threading.Event()
    running.set()
    if close_fails:
        with pytest.raises(RuntimeError, match="fake close"):
            capture._run("u", None, running, None, time.monotonic() + 1)
        assert events[-1] == ("record.started", True)
    else:
        capture._run("u", None, running, None, time.monotonic() + 1)
        assert events[-2:] == [("record.started", True), ("record.stopped", False)]


def test_supervisor_boundaries_preserve_pending_and_survive_terminal() -> None:
    from astra_voice.worker.supervisor import WorkerSupervisor, _Pending

    events: list[dict[str, Any]] = []
    supervisor = WorkerSupervisor(events.append, use_qt=False)
    supervisor.generation = 1
    key = ("utterance", "u")
    supervisor._pending[key] = _Pending(None, 0, "record.start", None)
    supervisor._accept({"type": "record.started", "utterance_id": "u"}, 1)
    assert key in supervisor._pending
    supervisor._accept({"type": "cancelled", "utterance_id": "u"}, 1)
    assert key not in supervisor._pending
    supervisor._accept({"type": "record.stopped", "utterance_id": "u"}, 1)
    assert [event["type"] for event in events] == ["record.started", "cancelled", "record.stopped"]
    assert all(event["generation"] == 1 for event in events)


@pytest.mark.parametrize("terminal", ["error", "cancelled"])
def test_stop_after_ui_terminal_and_no_stop_on_shutdown(terminal: str) -> None:
    from astra_voice.platform.hotkey import HotkeyState

    rig = Rig()
    seen: list[str] = []
    rig.core._recording_cue = lambda kind, key: seen.append(kind)
    rig.core.on_hotkey_state(HotkeyState.RECORDING, "press")
    boundary(rig, "started")
    if terminal == "cancelled":
        rig.core.cancel("escape")
    rig.core.on_worker_event(
        {
            "type": terminal,
            "utterance_id": rig.core._utterance_id,
            "generation": 1,
            "code": "audio-failed",
        }
    )
    assert seen == ["start"]
    boundary(rig, "stopped")
    assert seen == ["start", "stop"]
    rig.core.shutdown()
    boundary(rig, "started")
    boundary(rig, "stopped")
    assert seen == ["start", "stop"]


def test_failure_to_spawn_is_status_and_does_not_raise() -> None:
    def fail(*args: Any, **kwargs: Any) -> Any:
        raise FileNotFoundError("fake")

    player = cues.RecordingCues(enabled=True, popen=fail, available=lambda: True)
    try:
        player.play("start", (1, "u"))
        wait_until(lambda: player.status == cues.FAILED)
        assert player.process is None
    finally:
        player.shutdown()


def test_session_lock_invalidates_queue_and_old_stop() -> None:
    factory = Factory()
    player = cues.RecordingCues(enabled=True, popen=factory, available=lambda: True)
    try:
        player.play("start", (1, "u"))
        wait_until(lambda: factory.children)
        player.configure(enabled=True, allowed=False)
        player.play("start", (1, "locked"))
        wait_until(lambda: player.process is None)
        player.configure(enabled=True, allowed=True)
        player.play("stop", (1, "u"))
        assert len(factory.children) == 1
    finally:
        player.shutdown()


def test_latest_session_epoch_checked_without_gui_dispatch() -> None:
    factory = Factory()
    current = (True, 0, 0)
    player = cues.RecordingCues(
        enabled=True, popen=factory, available=lambda: True, admission=lambda: current
    )
    try:
        player.play("start", (1, "u"))
        wait_until(lambda: factory.children)
        current = (True, 1, 0)  # lock/unlock happened, queued GUI callback not handled yet
        wait_until(lambda: player.process is None)
        player.play("stop", (1, "u"))
        assert len(factory.children) == 1
    finally:
        player.shutdown()


def test_runtime_uses_one_lazy_session_monitor_without_cowork(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import Mock

    from test_runtime import Rig as RuntimeRig

    import astra_voice.runtime as runtime_module
    from astra_voice.core.command_mode import SessionSnapshot

    rig = RuntimeRig(monkeypatch, cowork_installed=False)
    monitor = Mock()
    monitor.snapshot.return_value = SessionSnapshot()
    factory = Mock(return_value=monitor)
    monkeypatch.setattr(runtime_module, "SessionMonitor", factory)
    assert rig.runtime._command_session is None
    rig.runtime.apply_sound_cues_enabled(True)
    factory.assert_called_once_with(rig.runtime)
    monitor.start.assert_called_once_with()
    assert rig.runtime._cue_admission()[0] is False
    assert "неизвестно" in rig.runtime.sound_cues_status
    # Fly's unsupported flag belongs to Cowork only.
    monitor.snapshot.return_value = SessionSnapshot(known=True, locked=False, supported=False)
    rig.runtime._session_changed(monitor.snapshot())
    assert rig.runtime._cue_admission()[0] is True
    rig.runtime._ensure_session_monitor()
    factory.assert_called_once()
    rig.runtime.apply_sound_cues_enabled(False)
    rig.runtime.apply_sound_cues_enabled(True)
    factory.assert_called_once()
    rig.runtime.shutdown()
    monitor.close.assert_called_once()


def test_shutdown_resource_snapshot_includes_unreaped_cue_child_and_thread() -> None:
    from types import SimpleNamespace

    from astra_voice.app import _copy_shutdown_resources

    process, thread = object(), object()
    runtime = SimpleNamespace(
        recording_cues=SimpleNamespace(process=process, thread=thread),
        supervisor=None,
        _switch_candidate=None,
    )
    processes, threads = _copy_shutdown_resources(runtime, None, None)
    assert process in processes and thread in threads


def test_sealed_config_survives_parent_close_in_offline_child() -> None:
    import sys

    # Deliberately not paplay: only verifies exec fd inheritance, no sound/bus.
    script = (
        "import os,sys; sys.stdin.buffer.read(1); "
        "sys.stdout.write(open(os.environ['PULSE_CLIENTCONFIG']).read())"
    )
    with cues._pulse_config() as fd:
        child = subprocess.Popen(
            [sys.executable, "-I", "-c", script],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            close_fds=True,
            pass_fds=(fd,),
            env={"PULSE_CLIENTCONFIG": f"/proc/self/fd/{fd}"},
        )
    try:
        out, err = child.communicate(b"x", timeout=2)
        assert child.returncode == 0 and not err
        assert out == b"autospawn = no\n"
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=2)


def test_unreaped_child_is_retained_and_blocks_shutdown() -> None:
    from unittest.mock import Mock

    child = Mock()
    child.poll.return_value = None
    child.communicate.return_value = None
    child.returncode = 0
    child.wait.side_effect = subprocess.TimeoutExpired("fake", 0.2)
    player = cues.RecordingCues(enabled=True, popen=lambda *a, **kw: child, available=lambda: True)
    player.play("start", (1, "u"))
    wait_until(lambda: player.thread is not None and not player.thread.is_alive())
    assert player.process is child and player.status == cues.CLEANUP_FAILED
    child.terminate.assert_called_once()
    child.kill.assert_called_once()
    with pytest.raises(RuntimeError, match="завершить"):
        player.shutdown()


def test_optional_session_failure_does_not_fail_settings_apply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import Mock

    from test_runtime import Rig as RuntimeRig

    import astra_voice.runtime as runtime_module

    rig = RuntimeRig(monkeypatch, cowork_installed=False)
    monkeypatch.setattr(runtime_module, "SessionMonitor", Mock(side_effect=RuntimeError("fake")))
    rig.runtime.apply_sound_cues_enabled(True)
    assert rig.runtime.settings.sound_cues_enabled
    assert not rig.runtime._closed
    assert "неизвестно" in rig.runtime.sound_cues_status
    rig.runtime.shutdown()
