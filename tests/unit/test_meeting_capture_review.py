"""Reproductions of the five M2 step 2 round-one review findings."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from test_meeting_capture import Rig, wait_until
from test_meeting_capture_session import admit

from astra_voice.worker.meeting_session import Login1Source, MeetingSession

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("filesystem_euid")]


@pytest.fixture
def rig(tmp_path: Path) -> Any:
    instance = Rig(tmp_path, fsync=lambda fd: None)
    yield instance
    instance.cleanup()


def pause_microphone(rig: Rig) -> None:
    rig.mics[0].device_change = object()
    rig.mics[0].chunks.append(None)
    rig.until("meeting.capture.track")
    wait_until(lambda: not rig.mics[0].opened)
    assert rig.state.track_states == {"mic": "paused", "far": "live"}


def visible_keepalive(rig: Rig, seq: int = 1) -> list[dict[str, Any]]:
    """M3 lease: only a fresh keepalive attesting a visible indicator may reopen."""
    return rig.send(
        "keepalive",
        seq=seq,
        indicators_visible=True,
        blocked_epoch=rig.state.session.snapshot().blocked_epoch,
        admission_mono_ms=int(rig.clock() * 1000),
    )


def test_lease_resume_preserves_microphone_device_pause(rig: Rig) -> None:
    rig.prepare()
    rig.open()
    pause_microphone(rig)
    rig.clock.now += 4.05
    rig.until("meeting.capture.closed")
    rig.until("meeting.capture.finalized")
    assert visible_keepalive(rig)[0]["type"] == "meeting.ack"
    wait_until(lambda: (rig.poll(), rig.state.state == "recording")[1])
    assert len(rig.mics) == 1, "lease reopened a microphone awaiting confirmation"
    assert len(rig.backend.sources) == 2
    assert rig.state.track_states == {"mic": "paused", "far": "live"}
    assert (
        admit(rig, "track.resume", track="mic", mic_device="approved")[0]["type"] == "meeting.ack"
    )
    rig.until("meeting.capture.track")
    assert len(rig.mics) == 2


def test_track_resume_expired_after_open_completes_request(rig: Rig) -> None:
    rig.prepare()
    rig.open()
    pause_microphone(rig)
    factory = rig.state.mic_factory

    def slow_microphone() -> Any:
        source = factory()
        original = source.open

        def slow_open(device: str | None = None) -> None:
            original(device)
            rig.clock.now += 1.01

        source.open = slow_open  # type: ignore[method-assign]
        return source

    rig.state.mic_factory = slow_microphone
    assert (
        admit(rig, "track.resume", track="mic", mic_device="approved")[0]["type"] == "meeting.ack"
    )
    request = rig.commands[-1]
    assert rig.state.open_thread is not None
    rig.state.open_thread.join(timeout=1)
    assert not rig.state.open_thread.is_alive()
    rig.poll()
    failures = [e for e in rig.events if e.get("req") == request["req"] and e["type"] == "error"]
    assert failures and failures[-1]["code"] == "admission-stale"
    assert failures[-1]["request_type"] == "meeting.capture.track.resume"
    assert not rig.state.opening
    assert not rig.mics[-1].opened
    try:
        rig.until("meeting.capture.finalized")
    except AssertionError as exc:
        raise AssertionError(
            f"finalization: state={rig.state.state} exit={rig.state.exit_code} "
            f"closing={rig.state.close_deadline is not None} saving={rig.state.finalizing}"
        ) from exc
    rig.state.mic_factory = factory
    assert admit(rig)[0]["type"] == "meeting.ack"
    rig.until("meeting.capture.started")


def test_confirmed_sleep_survives_following_session_timeout(rig: Rig) -> None:
    rig.prepare()
    rig.open()
    calls = 0

    def run(argv: list[str], **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls == 1:
            return SimpleNamespace(returncode=0, stdout='o "/org/freedesktop/login1/session/c1"\n')
        if calls == 2:
            return SimpleNamespace(returncode=0, stdout="b true\n")
        raise subprocess.TimeoutExpired("fake-login1", 0.075)

    monitor = MeetingSession(rig.state.session, source=Login1Source(run=run, uid=1000))
    monitor.tick()
    assert rig.state.session.snapshot().sleeping, "confirmed manager sleep was discarded"
    rig.until("meeting.capture.closed")
    rig.until("meeting.capture.finalized")
    assert rig.state.state == "stopped" and rig.state.session_terminal


@pytest.mark.parametrize("boundary", ["create", "connect"])
def test_native_open_serializes_with_block_epoch(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    import threading
    from unittest.mock import Mock

    from astra_voice.worker import pulse_stream
    from astra_voice.worker.monitor_stream import _meeting_pulse_type

    lib = Mock()
    lib.pa_proplist_new.return_value = 10
    lib.pa_proplist_sets.return_value = 0
    order: list[str] = []

    def create(*args: Any) -> int:
        order.append("native")
        return 22

    def connect(*args: Any) -> int:
        order.append("native")
        return 0

    lib.pa_stream_new_with_proplist.side_effect = create
    lib.pa_stream_connect_record.side_effect = connect
    monkeypatch.setattr(
        pulse_stream._PulseAsync, "__init__", lambda self: setattr(self, "lib", lib)
    )
    native = _meeting_pulse_type()()
    rig.state._guard_source(native, {"blocked_epoch": 0}, threading.Event())
    check = native.admission_check
    checked, published = threading.Event(), threading.Event()

    def publish_lock() -> None:
        assert checked.wait(1)
        rig.state.session.signal("Lock")
        order.append("lock")
        published.set()

    def interleaved_check() -> bool:
        allowed = check()
        checked.set()
        # Old code lets the publisher finish between check and native call.
        # The fixed short gate makes it wait until native call completion.
        published.wait(0.05)
        return bool(allowed)

    native.admission_check = interleaved_check
    publisher = threading.Thread(target=publish_lock)
    publisher.start()
    try:
        if boundary == "create":
            native.stream_new(1, b"meeting", None, None)
        else:
            native.stream_connect_record(22, b"source", None, 0)
        publisher.join(timeout=1)
        assert not publisher.is_alive()
        assert order == ["native", "lock"], "native open ran after the blocking epoch was published"
        assert not rig.state.session.allowed(0)
    finally:
        checked.set()
        publisher.join(timeout=1)


def helper_stub(ready: Path) -> str:
    return (
        "import os,signal,time\n"
        "from pathlib import Path\n"
        "signal.signal(signal.SIGTERM,signal.SIG_IGN)\n"
        f"Path({str(ready)!r}).write_text(str(os.getpid()))\n"
        "print('ready',flush=True)\n"
        "while True: time.sleep(1)\n"
    )


def test_helper_dies_and_is_reaped_after_capture_sigkill(tmp_path: Path) -> None:
    import sys

    ready = tmp_path / "helper-ready"
    source = str(Path(__file__).resolve().parents[2] / "src")
    stub = helper_stub(ready)
    parent_script = f"""
import sys
sys.path.insert(0,{source!r})
from astra_voice.worker import meeting_session as module
real_popen = module.subprocess.Popen
def spawn(argv, **kwargs):
    target = argv.index('/usr/bin/dbus-monitor')
    return real_popen(argv[:target] + [sys.executable,'-I','-B','-c',{stub!r}], **kwargs)
module.subprocess.Popen = spawn
module.MeetingSession(module.SessionAdmission(kind='KDE'))._signals()
"""
    harness = f"""
import ctypes,os,signal,subprocess,sys,time
from pathlib import Path
assert ctypes.CDLL(None,use_errno=True).prctl(36,1,0,0,0)==0
parent=subprocess.Popen([sys.executable,'-I','-B','-c',{parent_script!r}],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
helper=None
reaped=False
try:
    ready=Path({str(ready)!r})
    deadline=time.monotonic()+3
    while time.monotonic()<deadline:
        if ready.exists() and ready.read_text().strip().isdigit(): break
        time.sleep(.005)
    assert ready.exists(),'helper never became ready'
    helper=int(ready.read_text())
    parent.kill()
    parent.wait(timeout=1)
    deadline=time.monotonic()+1
    while time.monotonic()<deadline:
        pid,status=os.waitpid(helper,os.WNOHANG)
        if pid:
            reaped=True
            assert os.waitstatus_to_exitcode(status)==-signal.SIGKILL
            break
        time.sleep(.005)
    assert reaped,'helper survived SIGKILL of capture'
finally:
    if parent.poll() is None: parent.kill()
    parent.wait(timeout=1)
    if helper is not None and not reaped:
        try: os.kill(helper,signal.SIGKILL)
        except ProcessLookupError: pass
        os.waitpid(helper,0)
"""
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", harness], capture_output=True, timeout=8, check=False
    )
    assert result.returncode == 0, result.stderr.decode()


def test_helper_cleanup_kills_and_reaps_term_resistant_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sys
    import threading

    from astra_voice.worker import meeting_session as module

    ready = tmp_path / "helper-ready"
    stub = helper_stub(ready)
    real_popen = subprocess.Popen

    def spawn(argv: list[str], **kwargs: Any) -> Any:
        target = argv.index("/usr/bin/dbus-monitor")
        return real_popen(argv[:target] + [sys.executable, "-I", "-B", "-c", stub], **kwargs)

    monkeypatch.setattr(subprocess, "Popen", spawn)
    monitor = MeetingSession(module.SessionAdmission(kind="KDE"))
    worker = threading.Thread(target=monitor._signals, daemon=True)
    worker.start()
    try:
        wait_until(lambda: ready.exists() and ready.read_text().strip().isdigit())
        process = monitor.process
        assert process is not None
        monitor.close()
        try:
            process.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            pass
        assert process.returncode is not None, "cleanup left a TERM-resistant helper alive"
        worker.join(timeout=1)
        assert not worker.is_alive()
    finally:
        process = monitor.process
        if process is not None:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=1)
        worker.join(timeout=1)


def test_native_admission_gate_is_released_before_ready_wait(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading
    from unittest.mock import Mock

    from astra_voice.worker import pulse_stream
    from astra_voice.worker.monitor_stream import NativePulse, _meeting_pulse_type

    lib = Mock()
    lib.pa_proplist_new.return_value = 10
    lib.pa_proplist_sets.return_value = 0
    lib.pa_stream_new_with_proplist.return_value = 22
    lib.pa_stream_connect_record.return_value = 0
    lib.pa_stream_disconnect.return_value = 0
    monkeypatch.setattr(
        pulse_stream._PulseAsync, "__init__", lambda self: setattr(self, "lib", lib)
    )
    pulse = _meeting_pulse_type()()
    rig.state._guard_source(pulse, {"blocked_epoch": 0}, threading.Event())
    native = NativePulse.__new__(NativePulse)
    native.pulse = pulse
    native.stream = None
    native.buffer = bytearray()
    native.context = 1
    native.monitor_supported = True
    published = threading.Event()

    def publish() -> None:
        rig.state.session.signal("Lock")
        published.set()

    def ready_wait(ready: Any) -> None:
        publisher = threading.Thread(target=publish)
        publisher.start()
        try:
            assert published.wait(0.5), "READY wait held the session admission gate"
        finally:
            publisher.join(timeout=1)

    native._wait = ready_wait  # type: ignore[method-assign]
    try:
        native.open_record("exact.monitor", None, "run")
        assert published.is_set()
    finally:
        native.close_record()


def test_late_device_loss_is_removed_from_lease_resume_tracks(rig: Rig) -> None:
    rig.prepare()
    rig.open()
    source = rig.mics[0]
    original = source.shutdown

    def shutdown() -> None:
        rig.state.messages.put({"type": "track-ended", "track": "mic", "reason": "mic_change"})
        original()

    source.shutdown = shutdown  # type: ignore[method-assign]
    rig.clock.now += 4.05
    rig.until("meeting.capture.closed")
    rig.until("meeting.capture.finalized")
    visible_keepalive(rig)
    wait_until(lambda: (rig.poll(), rig.state.state == "recording")[1])
    assert len(rig.mics) == 1
    assert rig.state.track_states == {"mic": "paused", "far": "live"}


def test_helper_kill_wait_timeout_is_private_and_not_success(rig: Rig) -> None:
    from unittest.mock import Mock

    process = Mock(spec=subprocess.Popen)
    process.wait.side_effect = subprocess.TimeoutExpired(["PRIVATE-HELPER-ARGV"], 0.2)
    monitor = MeetingSession(rig.state.session)
    monitor.process = process
    assert monitor.close() is False
    process.terminate.assert_called_once_with()
    process.kill.assert_called_once_with()
    assert process.wait.call_count == 2
    assert rig.state.session.snapshot().reason == "unknown"
