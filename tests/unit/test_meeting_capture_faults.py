"""M2 adverse storage, protocol, role and epoch regressions, no audio devices."""

from __future__ import annotations

import errno
import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import pytest
from test_meeting_capture import Rig, wait_until

from astra_voice.worker import ipc

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("filesystem_euid")]


def test_real_commands_replay_pending_and_completed_outcomes(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    try:
        rig.prepare()
        prepared = rig.state.handle(rig.commands[-1])
        assert prepared[-1]["capture_id"] == rig.identity["capture_id"]
        rig.open()
        opened = rig.state.handle(rig.commands[-1])
        assert opened[-1]["type"] == "meeting.capture.started"
        assert len(rig.mics) == len(rig.backend.sources) == 1
        rig.send("pause", reason="user")
        request = rig.commands[-1]
        assert rig.state.handle(request)[0]["type"] == "meeting.ack"
        rig.until("meeting.capture.finalized")
        outcomes = rig.state.handle(request)
        assert {r["type"] for r in outcomes} >= {
            "meeting.capture.closed",
            "meeting.capture.finalized",
        }
        assert rig.state.handle({**request, "reason": "lease"})[0]["code"] == "req-conflict"
        journal = json.loads((rig.directory / "capture.json").read_text())
        assert [e["kind"] for e in journal["boundaries"]] == ["open", "close"]
        rig.send("pause", reason="user")
        assert len(json.loads((rig.directory / "capture.json").read_text())["boundaries"]) == 2
    finally:
        rig.cleanup()


@pytest.mark.parametrize("code", [errno.ENOSPC, errno.EDQUOT, errno.EIO])
def test_fsync_failure_closes_pcm_and_does_not_claim_success(tmp_path: Path, code: int) -> None:
    def fail(fd: int) -> None:
        raise OSError(code, "PRIVATE_PATH_DEVICE_HOST")

    rig = Rig(tmp_path, fsync=fail)
    try:
        rig.prepare()
        rig.open()
        rig.until("meeting.capture.closed")
        final = rig.until("meeting.capture.finalized")
        assert final["status"] == "failed"
        assert not rig.mics[0].opened and not rig.backend.sources[0].opened
        assert "PRIVATE_PATH_DEVICE_HOST" not in json.dumps(rig.events)
    finally:
        rig.cleanup()


def test_cancel_prepare_without_open_returns_closed_and_finalized(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    try:
        rig.prepare()
        rig.stop()
        assert not rig.mics and not rig.backend.sources
        assert list((rig.directory / "audio").iterdir()) == []
    finally:
        rig.cleanup()


def test_new_pcm_files_private_and_existing_symlink_untouched(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    try:
        rig.prepare("none")
        victim = tmp_path / "untouched"
        victim.write_bytes(b"original")
        (rig.directory / "audio/mic-0000.wav").symlink_to(victim)
        rig.open()
        rig.until("meeting.capture.closed")
        assert rig.until("meeting.capture.finalized")["status"] == "failed"
        assert victim.read_bytes() == b"original"
    finally:
        rig.cleanup()


def test_written_files_have_private_modes(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    try:
        rig.prepare()
        rig.open()
        rig.stop()
        for path in (rig.directory / "audio").iterdir():
            assert path.stat().st_mode & 0o777 == 0o600
        assert (rig.directory / "capture.json").stat().st_mode & 0o777 == 0o600
        assert (rig.directory / "audio").stat().st_mode & 0o777 == 0o700
    finally:
        rig.cleanup()


def test_pause_during_pending_open_cannot_open_after_closed(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    entered, release = threading.Event(), threading.Event()
    original = rig.mic

    def factory() -> Any:
        source = original()

        def open_source(device: str | None = None) -> None:
            entered.set()
            release.wait(3)
            source.opened = True

        source.open = open_source  # type: ignore[method-assign]
        return source

    rig.state.mic_factory = factory
    try:
        rig.prepare()
        rig.send("open", blocked_epoch=0, admission_mono_ms=100000)
        assert entered.wait(1)
        rig.send("pause", reason="user")
        assert not any(e["type"] == "meeting.capture.closed" for e in rig.poll())
        release.set()
        rig.until("meeting.capture.closed")
        assert not rig.mics[0].opened
        assert not any(s.opened for s in rig.backend.sources)
    finally:
        release.set()
        rig.cleanup()


def test_capture_import_gate_and_v4_meeting_fields() -> None:
    source = str(Path(__file__).resolve().parents[2] / "src")
    script = f"""
import sys
sys.path.insert(0,{source!r})
from astra_voice.worker.meeting_capture import MeetingCaptureState
from astra_voice.worker.meeting_session import SessionAdmission
state=MeetingCaptureState(session=SessionAdmission(kind="KDE"))
for name in ('PyQt5','PySide6','onnxruntime','numpy','astra_voice.worker.pulse_stream',
             'astra_voice.worker.audio','astra_voice.worker.state'):
    assert name not in sys.modules,name
assert not state.sources and state.backend is None and state.archive is None
"""
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", script], capture_output=True, timeout=10, check=False
    )
    assert result.returncode == 0, result.stderr.decode()
    assert ipc.make_hello()["protocol"] == 4
    for fields in (
        {"far": {"kind": "sink_monitor"}},
        {"meeting_id": "meeting"},
        {"capture_id": "capture"},
    ):
        wire = ipc.encode({"type": "record.start", "utterance_id": "u1", **fields})
        assert ipc.decode(wire[4:]) == {"type": "record.start", "utterance_id": "u1"}


def test_two_hour_recorded_limit_ignores_wall_clock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import time

    rig = Rig(tmp_path)
    try:
        rig.prepare("none")
        rig.open()
        monkeypatch.setattr(time, "time", lambda: -1000000000.0)
        rig.clock.now += 7200
        rig.send("keepalive", seq=1)
        rig.until("meeting.capture.closed")
        final = rig.until("meeting.capture.finalized")
        assert final["intervals"][-1]["recorded_ms"] == 7200000
    finally:
        rig.cleanup()


def test_failed_finalization_never_becomes_success_on_stop(tmp_path: Path) -> None:
    def fail(fd: int) -> None:
        raise OSError(errno.ENOSPC, "private")

    rig = Rig(tmp_path, fsync=fail)
    try:
        rig.prepare()
        rig.open()
        assert rig.until("meeting.capture.finalized")["status"] == "failed"
        replies = rig.send("stop")
        assert (
            next(e for e in replies if e["type"] == "meeting.capture.finalized")["status"]
            == "failed"
        )
        assert rig.send("resume", blocked_epoch=0, admission_mono_ms=100000)[0]["type"] == "error"
    finally:
        rig.cleanup()


def test_prepare_is_nonblocking_and_timeout_is_terminal(tmp_path: Path) -> None:
    import time

    rig = Rig(tmp_path)
    entered, release = threading.Event(), threading.Event()

    def blocked_prepare(far: dict[str, Any]) -> dict[str, Any]:
        entered.set()
        release.wait(3)
        return {"server_local": True, "server_kind": "pulseaudio", "far_available": True}

    rig.backend.prepare = blocked_prepare  # type: ignore[method-assign]
    try:
        start = time.monotonic()
        assert (
            rig.send(
                "prepare",
                dir=str(rig.directory),
                far={"kind": "none"},
                segment_s=300,
                fsync_s=10,
                segment_index_from=0,
            )[0]["type"]
            == "meeting.ack"
        )
        assert time.monotonic() - start < 1 and entered.wait(1)
        stopped = {
            "type": "meeting.detector.stop",
            "request_type": "meeting.detector.stop",
            "req": "det",
        }
        assert rig.state.handle(stopped)[0]["type"] == "error"
        rig.clock.now += 6
        rig.poll()
        assert rig.state.stopped and rig.state.exit_code != 0
        assert (
            rig.send(
                "prepare",
                dir=str(rig.directory),
                far={"kind": "none"},
                segment_s=300,
                fsync_s=10,
                segment_index_from=0,
            )[0]["type"]
            == "error"
        )
    finally:
        release.set()
        rig.cleanup()


def test_old_state_revision_cannot_reopen_after_pause(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    try:
        rig.prepare("none")
        rig.open()
        previous = dict(rig.identity)
        rig.send("pause", reason="user")
        rig.until("meeting.capture.finalized")
        result = rig.send("resume", **previous, blocked_epoch=0, admission_mono_ms=100000)
        assert result[0]["code"] == "not-prepared"
        assert not rig.mics[0].opened and len(rig.mics) == 1
    finally:
        rig.cleanup()


def test_segment_rotation_checkpoint_and_epoch_ranges(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    try:
        rig.prepare("none")
        rig.open()
        archive = rig.state.archive
        assert archive is not None
        for second in range(1, 302):
            stamp = ("pulse", second * 1000, 100000 + second * 1000)
            assert archive.put("pcm", ("mic", b"\0\0" * 16000, stamp, (second - 1) * 1000))
            wait_until(lambda: archive.pending_bytes == 0)
            if second == 10:
                checkpoint = json.loads((rig.directory / "audio/mic-0000.json").read_text())
                assert not checkpoint["closed"] and checkpoint["frames_at_checkpoint"] >= 128000
        rig.stop()
        first = json.loads((rig.directory / "audio/mic-0000.json").read_text())
        second_segment = json.loads((rig.directory / "audio/mic-0001.json").read_text())
        assert first["frames"] == 4800000 and first["close_reason"] == "segment"
        assert second_segment["sample_start"] == 4800000
        assert second_segment["epoch_id"] == first["epoch_id"]
        assert second_segment["frames"] == 16000
    finally:
        rig.cleanup()


def test_real_child_role_hardening_and_segv_no_core(tmp_path: Path) -> None:
    import signal
    import time

    root = Path(__file__).resolve().parents[2] / "src"
    script = f"""
import sys,os,resource,signal,json
sys.path.insert(0,{str(root)!r})
from astra_voice.worker.main import harden_process,harden_meeting_limits,_get_dumpable
assert harden_process(os.getppid(),strict=True,oom_score=500)
assert harden_meeting_limits('meeting-capture')
print(json.dumps([resource.getrlimit(resource.RLIMIT_CORE)[0],
 resource.getrlimit(resource.RLIMIT_AS)[0],_get_dumpable(),
 int(open('/proc/self/oom_score_adj').read())]),flush=True)
sys.stdin.readline()
os.kill(os.getpid(),signal.SIGSEGV)
"""
    child = subprocess.Popen(
        [sys.executable, "-I", "-B", "-c", script],
        cwd=tmp_path,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None and child.stdin is not None
        import select

        assert select.select([child.stdout], [], [], 5)[0], "hardening-child-timeout"
        assert json.loads(child.stdout.readline()) == [0, 512 * 1024**2, 0, 500]
        if os.geteuid() != 0:
            with pytest.raises(PermissionError):
                with open(f"/proc/{child.pid}/mem", "rb"):
                    pass
        child.stdin.write("segv\n")
        child.stdin.flush()
        deadline = time.monotonic() + 5
        while True:
            pid, status = os.waitpid(child.pid, os.WNOHANG)
            if pid:
                break
            assert time.monotonic() < deadline
            time.sleep(0.01)
        child.returncode = os.waitstatus_to_exitcode(status)
        assert os.WIFSIGNALED(status) and os.WTERMSIG(status) == signal.SIGSEGV
        assert not os.WCOREDUMP(status)
        assert not list(tmp_path.glob("core*"))
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)


@pytest.mark.parametrize("action", ["stop", "pause", "shutdown"])
def test_close_after_opened_accepts_pre_open_revision(tmp_path: Path, action: str) -> None:
    import time

    rig = Rig(tmp_path)
    try:
        rig.prepare()
        pre_open = dict(rig.identity)
        rig.send("open", blocked_epoch=0, admission_mono_ms=100000)
        # Reproduce the control-loop order: poll opened before handling the
        # closing command already issued by the GUI with its prepared revision.
        rig.until("meeting.capture.started")
        assert rig.identity["state_rev"] > pre_open["state_rev"]
        sources = [rig.mics[0], rig.backend.sources[0]]
        started = time.monotonic()
        fields = {} if action == "shutdown" else {**pre_open}
        if action == "pause":
            fields["reason"] = "user"
        response = rig.send(action, **fields)
        assert response[0]["type"] == "meeting.ack", response
        rig.until("meeting.capture.closed")
        assert time.monotonic() - started < 1
        assert all(not source.opened for source in sources)
        if action == "shutdown":
            rig.until("meeting.capture.bye")
        else:
            rig.until("meeting.capture.finalized")
    finally:
        rig.cleanup()


def test_stale_extend_and_foreign_capture_cannot_use_close_revision_exception(
    tmp_path: Path,
) -> None:
    rig = Rig(tmp_path)
    try:
        rig.prepare("none")
        old_revision = rig.identity["state_rev"]
        assert (
            rig.send("open", state_rev=old_revision + 1, blocked_epoch=0, admission_mono_ms=100000)[
                0
            ]["code"]
            == "not-prepared"
        )
        assert not rig.mics
        rig.open()
        assert (
            rig.send(
                "extend",
                state_rev=old_revision,
                blocked_epoch=0,
                admission_mono_ms=100000,
                max_ms=10800000,
            )[0]["code"]
            == "not-prepared"
        )
        for identity in ({"capture_id": "foreign"}, {"meeting_generation": 1}):
            assert rig.send("stop", state_rev=old_revision, **identity)[0]["code"] == "not-prepared"
        assert rig.mics[0].opened
        rig.stop()
    finally:
        rig.cleanup()


def test_pcm_overflow_failure_survives_archive_finalization_and_stop(tmp_path: Path) -> None:
    entered, release = threading.Event(), threading.Event()

    def blocked_fsync(fd: int) -> None:
        entered.set()
        assert release.wait(5)

    rig = Rig(tmp_path, fsync=blocked_fsync)
    try:
        rig.prepare()
        rig.open()
        assert entered.wait(1)
        # Four seconds fit in the bounded queue; the fifth second is lost.
        rig.mics[0].chunks.extend([b"\0\0" * 16000 for _ in range(5)])
        rig.until("meeting.capture.autopaused")
        rig.until("meeting.capture.closed")
        assert rig.state.final_status == "failed"
        archive = rig.state.archive
        assert archive is not None and not archive.failed.is_set()
        release.set()
        # The healthy disk writer can persist the accepted prefix successfully;
        # that cannot turn the capture with rejected PCM into a success.
        final = rig.until("meeting.capture.finalized")
        assert final["status"] == "failed"
        assert rig.state.final_status == "failed"
        assert rig.send("resume", blocked_epoch=0, admission_mono_ms=100000)[0]["code"] == "io"
        stopped = rig.send("stop")
        assert (
            next(e for e in stopped if e["type"] == "meeting.capture.finalized")["status"]
            == "failed"
        )
        assert (
            next(e for e in stopped if e["type"] == "meeting.capture.stopped")["lost_estimate_ms"]
            == 1000
        )
    finally:
        release.set()
        rig.cleanup()


@pytest.mark.parametrize("jump", ["both", "monotonic", "server"])
def test_forward_clock_jump_splits_epoch_without_gap_interpolation(
    tmp_path: Path, jump: str
) -> None:
    from astra_voice.meetings import MeetingValidationError
    from astra_voice.meetings.segments import SegmentSidecar
    from astra_voice.meetings.timeline import SegmentTimeline

    rig = Rig(tmp_path)
    try:
        rig.prepare("none")
        rig.open()
        archive = rig.state.archive
        assert archive is not None
        assert archive.put("pcm", ("mic", b"\0\0" * 16000, ("pulse", 1000, 101000), 0))
        wait_until(lambda: archive.pending_bytes == 0)
        end_server = 11000 if jump in {"both", "server"} else 2000
        end_mono = 111000 if jump in {"both", "monotonic"} else 102000
        assert archive.put("pcm", ("mic", b"\0\0" * 16000, ("pulse", end_server, end_mono), 1000))
        wait_until(lambda: archive.pending_bytes == 0)
        rig.stop()
        segments = [
            SegmentSidecar.from_dict(json.loads(path.read_text()))
            for path in sorted((rig.directory / "audio").glob("mic-*.json"))
        ]
        last = segments[-1]
        midpoint = SegmentTimeline.from_segment(last).sample_to_monotonic(last.sample_end - 8000)
        assert midpoint == end_mono - 500
        assert len(segments) == 2
        first, second = segments
        assert first.discontinuity and first.close_reason == "clock_change"
        assert first.epoch_id != second.epoch_id and second.sample_start == 0
        assert second.opened_t_mono_ms == end_mono - 1000
        assert not second.discontinuity
        with pytest.raises(MeetingValidationError):
            SegmentTimeline.from_segment(first).sample_to_monotonic(8000)
    finally:
        rig.cleanup()


def test_clock_discontinuity_tolerates_fragment_jitter_and_minus_200ppm(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    try:
        rig.prepare("none")
        rig.open()
        archive = rig.state.archive
        assert archive is not None
        for second in range(1, 21):
            jitter = (0, 100, -100)[(second - 1) % 3]
            stamp = ("pulse", round(second * 1000 * 0.9998), 100000 + second * 1000 + jitter)
            assert archive.put("pcm", ("mic", b"\0\0" * 16000, stamp, (second - 1) * 1000))
            wait_until(lambda: archive.pending_bytes == 0)
        rig.stop()
        paths = list((rig.directory / "audio").glob("mic-*.json"))
        assert len(paths) == 1
        sidecar = json.loads(paths[0].read_text())
        assert sidecar["frames"] == 320000
        assert not sidecar["discontinuity"] and sidecar["close_reason"] == "stop"
    finally:
        rig.cleanup()
