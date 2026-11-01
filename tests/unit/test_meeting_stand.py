"""Offline orchestration contracts; no connection to a user's audio server."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest
from tools import meeting_stand as stand

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("filesystem_euid")]


def test_private_environment_ignores_inherited_endpoints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for key in (
        "PULSE_SERVER",
        "DBUS_SESSION_BUS_ADDRESS",
        "NOTIFY_SOCKET",
        "PIPEWIRE_REMOTE",
        "DISPLAY",
        "LD_PRELOAD",
    ):
        monkeypatch.setenv(key, "host-secret")
    env = stand.private_environment(tmp_path)
    assert "host-secret" not in env.values()
    assert env["PULSE_SERVER"] == f"unix:{tmp_path}/runtime/pulse/native"
    assert env["PIPEWIRE_RUNTIME_DIR"] == env["XDG_RUNTIME_DIR"]
    assert env["XDG_CURRENT_DESKTOP"] == "FLY"
    for key in ("HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_RUNTIME_DIR"):
        assert Path(env[key]).is_relative_to(tmp_path)
        assert Path(env[key]).stat().st_mode & 0o777 == 0o700


def test_missing_binary_fails_without_start(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("tools.meeting_stand.shutil.which", lambda *args, **kwargs: None)
    spawn = Mock(side_effect=AssertionError("must not start"))
    monkeypatch.setattr("tools.meeting_stand.subprocess.Popen", spawn)
    rc, report = stand.run_stand(minutes=1)
    assert rc == 2
    assert report["diagnostics"] == ["missing-binary"]
    spawn.assert_not_called()


@pytest.mark.parametrize("minutes", [0, -1, float("nan"), float("inf")])
def test_invalid_duration_does_not_start(minutes: float) -> None:
    rc, report = stand.run_stand(minutes=minutes)
    assert rc == 2 and report["diagnostics"] == ["invalid-arguments"]


def test_cleanup_never_signals_recycled_pid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = stand.ProcessOwner(stand.private_environment(tmp_path))
    process = Mock(pid=987654)
    owner.register(process)
    monkeypatch.setattr(owner, "_owned_group", lambda child: False)
    killpg = Mock()
    monkeypatch.setattr("tools.meeting_stand.os.killpg", killpg)
    owner.close()
    killpg.assert_not_called()
    process.wait.assert_called()


def test_cleanup_signals_only_verified_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = stand.ProcessOwner(stand.private_environment(tmp_path))
    process = Mock(pid=987654)
    owner.register(process)
    monkeypatch.setattr(owner, "_owned_group", lambda child: True)
    killpg = Mock()
    monkeypatch.setattr("tools.meeting_stand.os.killpg", killpg)
    owner.close()
    assert [call.args for call in killpg.call_args_list] == [
        (987654, signal.SIGTERM),
        (987654, signal.SIGKILL),
    ]


def test_subprocess_is_new_session_and_private_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = stand.private_environment(tmp_path)
    popen = Mock(return_value=Mock(pid=987654))
    monkeypatch.setattr("tools.meeting_stand.subprocess.Popen", popen)
    owner = stand.ProcessOwner(env)
    result = owner.start(["/bin/true"])
    assert owner.processes == [result]
    assert popen.call_args.kwargs["start_new_session"] is True
    assert popen.call_args.kwargs["env"] == env


def test_worker_command_resets_environment(tmp_path: Path) -> None:
    env = stand.private_environment(tmp_path)
    argv = stand.capture_command(9, env)
    assert argv[:2] == ["/usr/bin/env", "-i"]
    assert f"PULSE_SERVER={env['PULSE_SERVER']}" in argv
    assert argv[-2:] == ["meeting-capture", "9"]
    assert os.environ.get("PULSE_SERVER") != env["PULSE_SERVER"]


def _stub_profile_grant(output: Path) -> str:
    # Measurement-only fixtures grant themselves; the separate real-parent test
    # exercises the external grant and observes the whole uninstrumented startup.
    return (
        "import os, pathlib, select, signal\n"
        f"gate = pathlib.Path({str(output) + '.start'!r})\n"
        "gate.write_text(f'start:{os.getpid()}')\n"
        "gate.chmod(0o600)\n"
        "os.kill(os.getpid(), signal.SIGUSR1)\n"
        "select.select([], [], [], 0)\n"
    )


def test_profile_command_retains_worker_isolation_and_bootstrap(tmp_path: Path) -> None:
    env = stand.private_environment(tmp_path / "private")
    output = tmp_path / "capture.pstats"
    plain = stand.capture_command(9, env)
    profiled = stand.capture_command(9, env, profile=output, profile_gate=Path(f"{output}.start"))
    assert profiled[: len(plain) - 3] == plain[:-3]
    assert profiled[-5:] == [str(output), f"{output}.start", *plain[-3:]]
    assert Path(profiled[-6]).name == "meeting_stand_profile.py"


def test_profile_paths_keep_normal_output_and_separate_other_scenarios(tmp_path: Path) -> None:
    output = tmp_path / "capture.pstats"
    paths = stand.capture_profile_paths(output)
    assert paths == {
        "normal": output,
        "target-gone": tmp_path / "capture.pstats.target-gone",
        "fallback": tmp_path / "capture.pstats.fallback",
        "crash": tmp_path / "capture.pstats.crash",
    }
    assert len(set(paths.values())) == 4


def test_runner_passes_profile_to_exercise_and_reports_overhead(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(stand, "preflight", lambda server: None)
    exercise = Mock(return_value={"rc": 0, "criteria": []})
    monkeypatch.setattr(stand, "exercise", exercise)
    output = tmp_path / "capture.pstats"
    rc, report = stand.run_stand(minutes=3, profile=output)
    assert rc == 0
    assert exercise.call_args.args[-1] == output
    assert report["profiling"]["paths"]["normal"] == str(output)
    assert report["profiling"]["resource_measurements_include_profiler_overhead"] is True
    assert report["profiling"]["format"] == "astra-capture-sampling-v1"
    assert report["profiling"]["backend"] == "stdlib-sampling"


@pytest.mark.parametrize("exit_code", [0, 7])
def test_profile_dump_contains_worker_thread_in_real_isolated_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exit_code: int
) -> None:
    env = stand.private_environment(tmp_path / "private")
    output = tmp_path / "capture.pstats"
    bootstrap = tmp_path / "stub_bootstrap.py"
    bootstrap.write_text(
        "import json, os, sys, threading, time\n"
        "assert sys.flags.isolated == 1\n"
        "assert sys.argv[1:] == ['meeting-capture', '9']\n"
        "assert 'DISPLAY' not in os.environ\n"
        "assert os.environ['PULSE_SERVER'].startswith('unix:' + os.environ['XDG_RUNTIME_DIR'])\n"
        "def capture_cpu_hot_path():\n"
        "    end = time.thread_time() + 0.25\n"
        "    while time.thread_time() < end: sum(i * i for i in range(2000))\n"
        "    time.sleep(0.3)\n"
        "release_waiter = threading.Event()\n"
        "def capture_wait_path():\n"
        "    release_waiter.wait(4.5)\n"
        "assert sys.getprofile() is None and threading.getprofile() is None\n"
        + _stub_profile_grant(output)
        + "targets = (capture_cpu_hot_path, capture_wait_path)\n"
        "threads = [threading.Thread(target=f) for f in targets]\n"
        "for thread in threads: thread.start()\n"
        "deadline = time.monotonic() + 4\n"
        "observed = False\n"
        "try:\n"
        "    while time.monotonic() < deadline:\n"
        f"        checkpoint = json.loads(pathlib.Path({str(output)!r}).read_text())\n"
        "        waiting = [row for row in checkpoint['stacks']\n"
        "                   if any('capture_wait_path' in f for f in row['frames'])]\n"
        "        if (sum(row['wall_samples'] for row in waiting) >= 3\n"
        "                and checkpoint['samples'] >= 5):\n"
        "            observed = True\n"
        "            break\n"
        "        time.sleep(0.02)\n"
        "finally:\n"
        "    release_waiter.set()\n"
        "for thread in threads: thread.join()\n"
        "assert observed, 'sampling checkpoint did not observe waiting thread in 4s'\n"
        f"raise SystemExit({exit_code})\n"
    )
    monkeypatch.setattr(
        stand,
        "worker_command",
        lambda fd, **kwargs: [sys.executable, "-I", str(bootstrap), "meeting-capture", str(fd)],
    )
    process = subprocess.run(
        stand.capture_command(9, env, profile=output, profile_gate=Path(f"{output}.start")),
        env={"DISPLAY": "host-secret", "PYTHONPATH": "host-secret"},
        capture_output=True,
        timeout=10,
    )
    assert process.returncode == exit_code, process.stderr
    assert process.stderr == b""
    data = json.loads(output.read_text())
    assert data["format"] == "astra-capture-sampling-v1"
    assert data["status"] == ("complete" if exit_code == 0 else "partial")
    assert data["cpu_tick_seconds"] > 0
    busy = [
        row for row in data["stacks"] if any("capture_cpu_hot_path" in f for f in row["frames"])
    ]
    waiting = [
        row for row in data["stacks"] if any("capture_wait_path" in f for f in row["frames"])
    ]
    assert busy and waiting
    busy_tid = int(busy[0]["frames"][0].removeprefix("<thread:").removesuffix(">"))
    waiting_tid = int(waiting[0]["frames"][0].removeprefix("<thread:").removesuffix(">"))
    thread_cpu = {row["native_tid"]: row["cpu_seconds"] for row in data["threads"]}
    assert thread_cpu[busy_tid] > 0.15
    assert thread_cpu[waiting_tid] < 0.05
    assert sum(row["wall_samples"] for row in waiting) >= 3
    assert sum(row["cpu_seconds"] for row in waiting) < 0.05
    assert data["sampler_cpu_seconds"] >= 0
    assert data["process_cpu_seconds"] >= data["sampler_cpu_seconds"]
    assert data["samples"] >= 5
    assert output.stat().st_mode & 0o777 == 0o600


def test_profile_exact_sections_survive_bursty_work_waits_and_exceptions(tmp_path: Path) -> None:
    wrapper = Path(stand.__file__).with_name("meeting_stand_profile.py")
    output = tmp_path / "sections.json"
    package = tmp_path / "astra_voice" / "worker"
    package.mkdir(parents=True)
    (package.parent / "__init__.py").touch()
    (package / "__init__.py").touch()
    (package / "meeting_capture.py").write_text(
        "import time\n"
        "class MeetingCaptureState:\n"
        "    def poll(self):\n"
        "        end = time.thread_time() + 0.005\n"
        "        while time.thread_time() < end: pass\n"
        "        return self\n"
        "    def handle(self, fail=False):\n"
        "        if fail: raise ValueError('SECRET_PCM')\n"
        "        time.sleep(0.095)\n"
        "        return self\n"
    )
    bootstrap = tmp_path / "sections_stub.py"
    bootstrap.write_text(
        "import sys\n"
        "assert 'astra_voice.worker.meeting_capture' not in sys.modules\n"
        f"sys.path.insert(0, {str(tmp_path)!r})\n"
        "from astra_voice.worker.meeting_capture import MeetingCaptureState\n"
        "from astra_voice.worker.meeting_capture import MeetingCaptureState as Again\n"
        "assert Again is MeetingCaptureState\n"
        "state = MeetingCaptureState()\n" + _stub_profile_grant(output) + "for _ in range(20):\n"
        "    assert state.poll() is state\n"
        "    assert state.handle() is state\n"
        "try: state.handle(fail=True)\n"
        "except ValueError as exc: assert str(exc) == 'SECRET_PCM'\n"
        "else: raise AssertionError('exception changed')\n"
    )
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            str(wrapper),
            str(output),
            f"{output}.start",
            str(bootstrap),
            "meeting-capture",
            "9",
        ],
        env=stand.private_environment(tmp_path / "private"),
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 0 and not result.stderr, result.stderr
    data = json.loads(output.read_text())
    sections = {row["label"]: row for row in data["sections"]}
    busy = sections["MeetingCaptureState.poll"]
    waiting = sections["MeetingCaptureState.handle"]
    assert busy["calls"] == 20 and busy["exceptions"] == 0
    assert 0.095 <= busy["cpu_seconds"] < 0.15
    assert waiting["calls"] == 21 and waiting["exceptions"] == 1
    assert waiting["cpu_seconds"] < 0.025
    assert waiting["wall_seconds"] >= 1.8
    assert data["sections_timing"] == "inclusive-do-not-sum-nested-sections"
    assert "SECRET_PCM" not in output.read_text()


def test_profile_low_duty_cpu_is_not_charged_to_sleep(tmp_path: Path) -> None:
    wrapper = Path(stand.__file__).with_name("meeting_stand_profile.py")
    output = tmp_path / "duty.json"
    bootstrap = tmp_path / "duty.py"
    bootstrap.write_text(
        "import time\n" + _stub_profile_grant(output) + "for _ in range(20):\n"
        "    end = time.thread_time() + 0.005\n"
        "    while time.thread_time() < end: pass\n"
        "    time.sleep(0.095)\n"
    )
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            str(wrapper),
            str(output),
            f"{output}.start",
            str(bootstrap),
            "meeting-capture",
            "9",
        ],
        env=stand.private_environment(tmp_path / "private"),
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 0 and not result.stderr, result.stderr
    data = json.loads(output.read_text())
    assert sum(t["cpu_seconds"] for t in data["threads"]) >= 0.08
    assert data["unattributed_cpu_seconds"] > 0
    assert data["cpu_attribution"] == "approximate-running-stack-only"


def test_profile_preserves_worker_thread_exception_behavior(tmp_path: Path) -> None:
    wrapper = Path(stand.__file__).with_name("meeting_stand_profile.py")
    output = tmp_path / "exceptions.pstats"
    bootstrap = tmp_path / "stub.py"
    bootstrap.write_text(
        "import threading\n" + _stub_profile_grant(output) + "errors = []\n"
        "threading.excepthook = lambda args: errors.append(args.exc_type.__name__)\n"
        "def capture_failure():\n"
        "    raise ValueError('expected-stub-failure')\n"
        "thread = threading.Thread(target=capture_failure)\n"
        "thread.start()\n"
        "thread.join()\n"
        "assert errors == ['ValueError'], errors\n"
    )
    process = subprocess.run(
        [
            sys.executable,
            "-I",
            str(wrapper),
            str(output),
            f"{output}.start",
            str(bootstrap),
            "meeting-capture",
            "9",
        ],
        env=stand.private_environment(tmp_path / "private"),
        capture_output=True,
        timeout=10,
    )
    assert process.returncode == 0 and not process.stderr, process.stderr
    assert json.loads(output.read_text())["status"] == "complete"


def test_profile_early_bootstrap_error_retains_safe_dump(tmp_path: Path) -> None:
    wrapper = Path(stand.__file__).with_name("meeting_stand_profile.py")
    output = tmp_path / "early.json"
    bootstrap = tmp_path / "early.py"
    bootstrap.write_text("raise RuntimeError('SECRET_RAW_DEVICE_PATH')\n")
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            str(wrapper),
            str(output),
            f"{output}.start",
            str(bootstrap),
            "meeting-capture",
            "9",
        ],
        env=stand.private_environment(tmp_path / "private"),
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 2
    assert result.stderr == b"capture-profile-bootstrap-failed\n"
    data = json.loads(output.read_text())
    assert data["status"] == "not-started"
    assert "SECRET_RAW_DEVICE_PATH" not in output.read_text()


def test_profile_checkpoint_survives_sigkill(tmp_path: Path) -> None:
    wrapper = Path(stand.__file__).with_name("meeting_stand_profile.py")
    output = tmp_path / "checkpoint.json"
    ready = tmp_path / "ready"
    bootstrap = tmp_path / "stub.py"
    bootstrap.write_text(
        _stub_profile_grant(output)
        + f"import pathlib, time\npathlib.Path({str(ready)!r}).touch()\ntime.sleep(30)\n"
    )
    process = subprocess.Popen(
        [
            sys.executable,
            "-I",
            str(wrapper),
            str(output),
            f"{output}.start",
            str(bootstrap),
            "meeting-capture",
            "9",
        ],
        env=stand.private_environment(tmp_path / "private"),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 4
        while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists()
        assert json.loads(output.read_text())["status"] in {"activating", "running"}
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if json.loads(output.read_text())["samples"] > 0:
                break
            time.sleep(0.02)
        assert json.loads(output.read_text())["samples"] > 0
        process.kill()
        process.wait(timeout=2)
        assert json.loads(output.read_text())["status"] == "running"
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=2)


def test_profile_histogram_is_bounded_and_keeps_overflow_cpu(tmp_path: Path) -> None:
    from tools.meeting_stand_profile import Sampler

    sampler = Sampler(tmp_path / "bounded.json", max_stacks=3)
    for index in range(1000):
        sampler.record((f"unique{index}",), 0.01)
    data = sampler.snapshot("running")
    assert len(data["stacks"]) == 3
    assert data["overflow"]["wall_samples"] == 997
    assert data["overflow"]["cpu_seconds"] == pytest.approx(9.97)
    assert sum(row["cpu_seconds"] for row in data["stacks"]) == pytest.approx(0.03)


@pytest.mark.parametrize(
    "code, expected", [("admission-stale", "admission-stale"), ("/private/audio.wav", "unknown")]
)
def test_capture_failure_has_safe_phase_and_code(tmp_path: Path, code: str, expected: str) -> None:
    capture = stand.Capture(Mock(spec=stand.ProcessOwner), tmp_path / "meeting")
    capture.supervisor = Mock()
    capture.events = [{"type": "error", "code": code, "message": "secret audio data"}]
    with pytest.raises(stand.StandUnavailable, match=f"capture-operation-failed:open:{expected}"):
        capture.wait("meeting.capture.started")


def test_profile_stop_waits_for_delayed_dump_before_supervisor_can_signal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "capture.pstats"
    owner = Mock(spec=stand.ProcessOwner)
    capture = stand.Capture(owner, tmp_path / "meeting", profile=output)
    capture.profile_activated = True
    process = Mock(returncode=0)
    supervisor = Mock(process=process)
    capture.supervisor = supervisor
    events: list[str] = []

    def send(action: str) -> str:
        events.append(action)
        return action

    def wait(kind: str, req: str, timeout: float) -> dict[str, str]:
        events.append(kind)
        if kind == "meeting.capture.bye":
            # pump can already reap this handle when bye and EOF arrive together.
            supervisor.process = None
        return {"status": "succeeded"}

    def flush(*args: Any, **kwargs: Any) -> int:
        events.append("profile-flushed")
        output.write_text(json.dumps({"format": "astra-capture-sampling-v1", "status": "complete"}))
        return 0

    def stop() -> None:
        # Closing IPC followed by SIGTERM before this point can kill a delayed
        # profile writer after the worker restores its default signal handler.
        assert json.loads(output.read_text())["status"] == "complete"
        events.append("supervisor-stop")

    monkeypatch.setattr(capture, "send", send)
    monkeypatch.setattr(capture, "wait", wait)
    process.wait.side_effect = flush
    supervisor.stop.side_effect = stop
    capture.stop()
    assert events == [
        "meeting.capture.stop",
        "meeting.capture.finalized",
        "meeting.capture.shutdown",
        "meeting.capture.bye",
        "profile-flushed",
        "supervisor-stop",
    ]
    process.wait.assert_called_once_with(timeout=2)
    owner.terminate.assert_called_once_with(process)


@pytest.mark.parametrize("status", ["running", "partial"])
def test_profile_stop_rejects_unfinished_checkpoint(tmp_path: Path, status: str) -> None:
    output = tmp_path / "incomplete.json"
    output.write_text(json.dumps({"format": "astra-capture-sampling-v1", "status": status}))
    capture = stand.Capture(Mock(spec=stand.ProcessOwner), tmp_path / "meeting", profile=output)
    capture.supervisor = Mock(process=Mock(returncode=0))
    capture.send = Mock(return_value="request")  # type: ignore[method-assign]
    capture.wait = Mock(return_value={"status": "succeeded"})  # type: ignore[method-assign]
    with pytest.raises(stand.StandUnavailable, match="capture-profile-incomplete"):
        capture.stop()


def test_profile_exit_timeout_is_bounded_and_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capture = stand.Capture(
        Mock(spec=stand.ProcessOwner), tmp_path / "meeting", profile=tmp_path / "capture.pstats"
    )
    process = Mock()
    capture.supervisor = Mock(process=process)
    monkeypatch.setattr(capture, "send", lambda action: action)
    monkeypatch.setattr(capture, "wait", lambda *args, **kwargs: {"status": "succeeded"})
    process.wait.side_effect = subprocess.TimeoutExpired("stand-worker", 2)
    with pytest.raises(stand.StandUnavailable, match="capture-profile-exit-timeout"):
        capture.stop()
    process.wait.assert_called_once_with(timeout=2)
    # run_stand finally owns termination on failure; stop must not kill a
    # successful dump early via the supervisor's EOF -> SIGTERM sequence.
    capture.supervisor.stop.assert_not_called()


def test_profile_real_worker_graceful_stop_without_audio(tmp_path: Path) -> None:
    owner = stand.ProcessOwner(stand.private_environment(tmp_path / "private"))
    output = tmp_path / "capture.pstats"
    baseline = stand.Capture(owner, stand.meeting_directory(tmp_path))
    try:
        baseline.start()
    except stand.StandUnavailable:
        pytest.skip("unprofiled hardened worker unavailable in this environment")
    finally:
        baseline.supervisor.stop()
        owner.close()
    capture = stand.Capture(owner, stand.meeting_directory(tmp_path), profile=output)
    try:
        # hello follows strict meeting entry-point hardening. No prepare/open is
        # sent, so this exercises the real role without connecting any audio.
        capture.start()
        process = capture.supervisor.process
        assert process is not None
        req = capture.send("meeting.capture.shutdown")
        capture.wait("meeting.capture.bye", req)
        process.wait(timeout=2)
        capture.supervisor.stop()
        data = json.loads(output.read_text())
        assert data["status"] == "not-started"
        assert data["samples"] == 0
        assert process.returncode == 0
    finally:
        capture.supervisor.stop()
        owner.close()


@pytest.mark.parametrize("keep", [False, True])
def test_failure_always_cleans_processes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, keep: bool
) -> None:
    monkeypatch.setattr(stand, "preflight", lambda server: None)
    monkeypatch.setattr(
        "tools.meeting_stand.tempfile.mkdtemp", lambda **kwargs: str(tmp_path / "private")
    )
    close = Mock()
    monkeypatch.setattr(stand.ProcessOwner, "close", close)
    failure = RuntimeError("secret device host")

    def origin() -> None:
        raise failure

    def exercise(*args: object) -> None:
        origin()

    monkeypatch.setattr(stand, "exercise", exercise)
    rc, report = stand.run_stand(minutes=1, keep=keep)
    traceback = failure.__traceback__
    assert traceback is not None
    while traceback.tb_next is not None:
        traceback = traceback.tb_next
    assert traceback.tb_frame.f_code.co_name == "origin"
    assert traceback.tb_lineno > 0
    assert rc == 2 and report["diagnostics"] == [
        f"stand-error:RuntimeError@test_meeting_stand.py:{traceback.tb_lineno}"
    ]
    close.assert_called_once()
    assert (tmp_path / "private").exists() is keep
    assert "secret" not in str(report)


@pytest.mark.parametrize(
    "filename,basename",
    [
        ("/sensitive/user/worker.py", "worker.py"),
        ("/sensitive/user/секрет.py", "unknown"),
        ("/sensitive/user/worker\nsecret.py", "unknown"),
        ("/sensitive/user/" + "x" * 129 + ".py", "unknown"),
    ],
)
def test_unexpected_failure_diagnostic_sanitizes_traceback_filename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, filename: str, basename: str
) -> None:
    failure = KeyError("private sound device")

    def origin(*args: object) -> None:
        raise failure

    origin.__code__ = origin.__code__.replace(co_filename=filename)
    monkeypatch.setattr(stand, "preflight", lambda server: None)
    monkeypatch.setattr(
        "tools.meeting_stand.tempfile.mkdtemp", lambda **kwargs: str(tmp_path / "private")
    )
    monkeypatch.setattr(stand.ProcessOwner, "close", Mock())
    monkeypatch.setattr(stand, "exercise", origin)
    rc, report = stand.run_stand(minutes=1)
    traceback = failure.__traceback__
    assert traceback is not None
    while traceback.tb_next is not None:
        traceback = traceback.tb_next
    assert traceback.tb_lineno > 0
    assert rc == 2
    assert report["diagnostics"] == [f"stand-error:KeyError@{basename}:{traceback.tb_lineno}"]
    assert "sensitive" not in str(report)
    assert "private sound" not in str(report)


def test_interrupt_restores_signal_handler_and_cleans(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(stand, "preflight", lambda server: None)
    monkeypatch.setattr(
        "tools.meeting_stand.tempfile.mkdtemp", lambda **kwargs: str(tmp_path / "private")
    )
    close = Mock()
    monkeypatch.setattr(stand.ProcessOwner, "close", close)
    monkeypatch.setattr(stand, "exercise", Mock(side_effect=KeyboardInterrupt))
    previous = signal.getsignal(signal.SIGTERM)
    rc, report = stand.run_stand(minutes=1)
    assert rc == 2 and report["diagnostics"] == ["interrupted"]
    assert signal.getsignal(signal.SIGTERM) == previous
    close.assert_called_once()


def test_signal_during_spawn_is_deferred_until_child_registered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = stand.ProcessOwner(stand.private_environment(tmp_path))
    process = Mock(pid=987654)

    def spawn(*args: object, **kwargs: object) -> Mock:
        os.kill(os.getpid(), signal.SIGTERM)
        return process

    monkeypatch.setattr("tools.meeting_stand.subprocess.Popen", spawn)
    with stand.interruption(owner):
        with pytest.raises(KeyboardInterrupt):
            owner.start(["/bin/true"])
    assert owner.processes == [process]


def test_signal_during_cleanup_is_ignored_and_handler_restored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(stand, "preflight", lambda server: None)
    monkeypatch.setattr(
        "tools.meeting_stand.tempfile.mkdtemp", lambda **kwargs: str(tmp_path / "private")
    )
    monkeypatch.setattr(stand, "exercise", Mock(side_effect=KeyboardInterrupt))
    completed: list[bool] = []

    def close(self: stand.ProcessOwner) -> None:
        os.kill(os.getpid(), signal.SIGTERM)
        completed.append(True)

    monkeypatch.setattr(stand.ProcessOwner, "close", close)
    previous = signal.getsignal(signal.SIGTERM)
    rc, report = stand.run_stand(minutes=1)
    assert rc == 2 and report["cleanup"] is True and completed
    assert signal.getsignal(signal.SIGTERM) == previous


def test_capture_commands_use_real_wire_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from astra_voice.worker.meeting_contract import validate_message

    directory = stand.meeting_directory(tmp_path)
    owner = stand.ProcessOwner(stand.private_environment(tmp_path))
    capture = stand.Capture(owner, directory)
    messages: list[dict[str, object]] = []

    def send(message: dict[str, object]) -> None:
        messages.append(validate_message(message, "meeting-capture"))

    monkeypatch.setattr(capture.supervisor, "send", send)
    capture.send(
        "meeting.capture.prepare",
        dir=str(directory),
        far={"kind": "sink_monitor"},
        mic_device="stand_mic.monitor",
        segment_s=300,
        fsync_s=10,
        segment_index_from=0,
    )
    capture.identity.update(capture_id="capture-1", state_rev=0)
    capture.send("meeting.capture.open", blocked_epoch=0, admission_mono_ms=1)
    capture.send("meeting.capture.keepalive", seq=1)
    capture.send("meeting.capture.stop")
    assert len(messages) == 4
    assert (directory / "meeting.json").is_file()
    assert (directory / "audio").is_dir()


@pytest.mark.parametrize("prepared", [False, True])
def test_profile_shutdown_uses_unscoped_real_wire_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, prepared: bool
) -> None:
    from astra_voice.worker import ipc

    capture = stand.Capture(Mock(spec=stand.ProcessOwner), tmp_path, profile=tmp_path / "p.json")
    if prepared:
        capture.identity.update(capture_id="capture-1", state_rev=3)
    messages: list[dict[str, Any]] = []

    def send(message: dict[str, Any]) -> None:
        messages.append(
            ipc.decode(ipc.encode(message, role="meeting-capture")[4:], role="meeting-capture")
        )

    monkeypatch.setattr(capture.supervisor, "send", send)
    req = capture.send("meeting.capture.shutdown")
    assert messages == [
        {"type": "meeting.capture.shutdown", "request_type": "meeting.capture.shutdown", "req": req}
    ]


def test_profile_section_names_are_available_to_json_consumers() -> None:
    from tools.meeting_stand_profile import Sections

    rows = Sections().snapshot()
    assert rows
    assert all(row.get("name") == row["label"] for row in rows)


def test_train_has_silent_startup_and_termination_margin(tmp_path: Path) -> None:
    import wave

    path = tmp_path / "beeps.wav"
    onsets = stand.write_signal(path, 90, train_end_s=55)
    assert onsets == list(range(10, 55, 5))
    with wave.open(str(path), "rb") as wav:
        assert not any(wav.readframes(160000))
        assert any(wav.readframes(2400))
        wav.setpos(55 * 16000)
        assert not any(wav.readframes(35 * 16000))


@pytest.mark.parametrize("field", ["sink", "object.serial", "volume", "argument"])
def test_snapshot_detects_persistent_route_and_module_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:

    owner = stand.ProcessOwner(stand.private_environment(tmp_path))
    server = stand.AudioServer(owner, tmp_path, "pipewire")
    inputs: list[dict[str, Any]] = [
        {
            "index": 10,
            "sink": 2,
            "mute": False,
            "volume": {"mono": {"value": 65536}},
            "properties": {"application.process.id": "42", "object.serial": "77"},
        }
    ]
    modules: list[dict[str, Any]] = [
        {"index": 4, "name": "module-null-sink", "argument": "rate=16000"}
    ]

    def rows(kind: str) -> list[dict[str, Any]]:
        return inputs if kind == "sink-inputs" else modules if kind == "modules" else []

    monkeypatch.setattr(server, "rows", rows)
    monkeypatch.setattr(server, "pactl", lambda *args: b"stand\n")
    before = server.snapshot()
    if field == "argument":
        modules[0][field] = "rate=48000"
    elif field == "object.serial":
        inputs[0]["properties"][field] = "78"
    else:
        inputs[0][field] = 9
    assert before != server.snapshot()


@pytest.fixture(params=["pulseaudio-16.1", "pipewire-pulse"])
def snapshot_server(
    request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[stand.AudioServer, dict[str, list[dict[str, Any]]]]:
    fixture = Path(__file__).parents[1] / "fixtures/meeting_stand" / f"{request.param}.json"
    rows: dict[str, list[dict[str, Any]]] = json.loads(fixture.read_text())["rows"]
    owner = stand.ProcessOwner(stand.private_environment(tmp_path))
    server = stand.AudioServer(owner, tmp_path, "pipewire")
    monkeypatch.setattr(server, "rows", lambda kind: rows[kind])
    monkeypatch.setattr(server, "pactl", lambda *args: b"stand\n")
    return server, rows


def test_snapshot_accepts_pactl_modules_without_index_and_ignores_order(
    snapshot_server: tuple[stand.AudioServer, dict[str, list[dict[str, Any]]]],
) -> None:
    server, rows = snapshot_server
    assert all("index" not in module for module in rows["modules"])
    before = server.snapshot()
    for items in rows.values():
        items.reverse()
    assert server.snapshot() == before


@pytest.mark.parametrize("kind", ["sinks", "sources", "sink-inputs", "source-outputs"])
@pytest.mark.parametrize("identity", ["id", "fallback"])
def test_snapshot_accepts_alternate_identity_without_index(
    snapshot_server: tuple[stand.AudioServer, dict[str, list[dict[str, Any]]]],
    kind: str,
    identity: str,
) -> None:
    server, rows = snapshot_server
    for row in rows[kind]:
        index = row.pop("index")
        if identity == "id":
            row["id"] = index
    before = server.snapshot()
    rows[kind].reverse()
    assert server.snapshot() == before
    if identity == "id":
        rows[kind][0]["id"] = 999
    elif kind in {"sinks", "sources"}:
        rows[kind][0]["name"] = "replacement"
    else:
        rows[kind][0]["properties"]["application.process.id"] = "999"
    assert server.snapshot() != before


@pytest.mark.parametrize("kind", ["modules", "sinks", "sources", "sink-inputs", "source-outputs"])
def test_snapshot_preserves_duplicate_multiplicity(
    snapshot_server: tuple[stand.AudioServer, dict[str, list[dict[str, Any]]]], kind: str
) -> None:
    server, rows = snapshot_server
    before = server.snapshot()
    rows[kind].append(dict(rows[kind][0]))
    assert server.snapshot() != before


@pytest.mark.parametrize("kind", ["sink-inputs", "source-outputs"])
@pytest.mark.parametrize("field", ["route", "pid", "serial", "mute", "volume"])
def test_snapshot_compares_both_stream_directions(
    snapshot_server: tuple[stand.AudioServer, dict[str, list[dict[str, Any]]]],
    kind: str,
    field: str,
) -> None:
    server, rows = snapshot_server
    before = server.snapshot()
    stream = rows[kind][0]
    if field == "route":
        stream["sink" if kind == "sink-inputs" else "source"] = 999
    elif field in {"pid", "serial"}:
        stream["properties"]["application.process.id" if field == "pid" else "object.serial"] = (
            "999"
        )
    else:
        stream[field] = True if field == "mute" else {"mono": {"value": 42}}
    assert server.snapshot() != before


@pytest.mark.parametrize("field", ["argument", "arguments", "driver", "name", "id", "index"])
def test_snapshot_compares_module_identity_and_arguments(
    snapshot_server: tuple[stand.AudioServer, dict[str, list[dict[str, Any]]]], field: str
) -> None:
    server, rows = snapshot_server
    module = rows["modules"][0]
    if field == "arguments":
        module[field] = module.pop("argument")
    elif field in {"id", "index"}:
        module[field] = 1
    before = server.snapshot()
    module[field] = 999 if field in {"id", "index"} else "changed"
    assert server.snapshot() != before


@pytest.mark.parametrize("kind", ["modules", "sinks", "sources", "sink-inputs", "source-outputs"])
@pytest.mark.parametrize("invalid", [{}, {"index": None, "id": ""}, {"index": False}])
def test_snapshot_missing_identity_is_unavailable_instead_of_false_pass(
    snapshot_server: tuple[stand.AudioServer, dict[str, list[dict[str, Any]]]],
    kind: str,
    invalid: dict[str, Any],
) -> None:
    server, rows = snapshot_server
    rows[kind] = [invalid.copy()]
    with pytest.raises(stand.StandUnavailable, match="^snapshot-identity-unavailable$"):
        server.snapshot()


@pytest.mark.parametrize("kind", ["sink-inputs", "source-outputs"])
def test_snapshot_stream_pid_alone_is_not_sufficient_identity(
    snapshot_server: tuple[stand.AudioServer, dict[str, list[dict[str, Any]]]], kind: str
) -> None:
    server, rows = snapshot_server
    rows[kind] = [{"properties": {"application.process.id": "42"}}]
    with pytest.raises(stand.StandUnavailable, match="^snapshot-identity-unavailable$"):
        server.snapshot()


@pytest.mark.parametrize("kind", ["sink-inputs", "source-outputs"])
@pytest.mark.parametrize("same_route", [False, True])
@pytest.mark.parametrize("serial", [None, "", False, -1])
def test_snapshot_ambiguous_pid_fallback_cannot_hide_swapped_routes(
    snapshot_server: tuple[stand.AudioServer, dict[str, list[dict[str, Any]]]],
    kind: str,
    same_route: bool,
    serial: str | int | None,
) -> None:
    server, rows = snapshot_server
    route = "sink" if kind == "sink-inputs" else "source"
    rows[kind] = [
        {
            "driver": "protocol-native.c",
            route: 1 if same_route else index,
            "properties": {
                "application.process.id": "42",
                "media.name": name,
                "object.serial": serial,
            },
        }
        for index, name in [(1, "stream-A"), (2, "stream-B")]
    ]
    with pytest.raises(stand.StandUnavailable, match="^snapshot-identity-unavailable$"):
        server.snapshot()


@pytest.mark.parametrize(
    "phase,code",
    [
        ("mkdtemp", "workspace-create-failed"),
        ("environment", "environment-create-failed"),
        ("write", "artifact-write-failed"),
        ("delete", "artifact-delete-failed"),
    ],
)
def test_filesystem_failure_returns_sanitized_report_and_cleans(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str, code: str
) -> None:
    monkeypatch.setattr(stand, "preflight", lambda server: None)
    monkeypatch.setattr(
        "tools.meeting_stand.tempfile.mkdtemp", lambda **kwargs: str(tmp_path / "private")
    )
    monkeypatch.setattr(stand, "exercise", lambda *args: {"rc": 0, "criteria": []})
    close = Mock()
    monkeypatch.setattr(stand.ProcessOwner, "close", close)
    failure = Mock(side_effect=OSError("sensitive host/device path"))
    if phase == "mkdtemp":
        monkeypatch.setattr("tools.meeting_stand.tempfile.mkdtemp", failure)
    elif phase == "environment":
        monkeypatch.setattr(stand, "private_environment", failure)
    elif phase == "delete":
        monkeypatch.setattr("tools.meeting_stand.shutil.rmtree", failure)
    else:
        original = Path.write_text

        def write(self: Path, data: str, *args: object, **kwargs: object) -> int:
            if self.name == "report.json":
                raise OSError("sensitive host/device path")
            return original(self, data)

        monkeypatch.setattr(Path, "write_text", write)
    rc, report = stand.run_stand(minutes=1, keep=phase == "write")
    assert rc == 2 and report["exit_code"] == 2 and report["verdict"] == "unavailable"
    assert code in report["diagnostics"]
    assert report["cleanup"] is True
    assert "sensitive" not in str(report)
    close.assert_called_once()


@pytest.mark.parametrize("profiled", [False, True])
def test_exercise_complete_sequence_keeps_sentinel_and_uses_all_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, profiled: bool
) -> None:

    from tools import meeting_stand_metrics as metrics

    now = [100.0]
    events: list[str] = []
    players: list[Any] = []
    captures: list[Any] = []
    analyses: list[str] = []
    owner = Mock(spec=stand.ProcessOwner)
    owner._owned_group.return_value = True

    def terminate(process: Any) -> None:
        events.append(f"terminate:{process.pid}")
        process.live = False

    owner.terminate.side_effect = terminate
    monkeypatch.setattr("tools.meeting_stand.time.monotonic", lambda: now[0])
    monkeypatch.setattr(
        "tools.meeting_stand.time.sleep", lambda seconds: now.__setitem__(0, now[0] + seconds)
    )
    server = Mock()
    server.daemons = [Mock(pid=99)]
    server.versions = {"pipewire": "1.6.2"}

    def play(path: Path, sink: str, *, foreign: bool = False) -> Any:
        process = Mock(pid=len(players) + 1000)
        process.live = True
        process.poll.side_effect = lambda: None if process.live else 0
        process.foreign = foreign
        process.sink = sink
        players.append(process)
        events.append("sentinel" if foreign else "play")
        return process

    server.play.side_effect = play
    server.target.side_effect = lambda process: {
        "kind": "app_stream",
        "sink_input": process.pid,
        "object_serial": 7,
    }
    server.rows.side_effect = lambda kind: (
        [{"index": c.supervisor.process.pid} for c in captures if c.active]
        if kind == "source-outputs"
        else []
    )
    snapshots: list[tuple[int, ...]] = []

    def snapshot() -> dict[str, Any]:
        live = tuple(p.pid for p in players if p.live)
        snapshots.append(live)
        return {"routes": live, "outputs": len([c for c in captures if c.active])}

    server.snapshot.side_effect = snapshot
    monkeypatch.setattr(stand, "AudioServer", lambda *args: server)

    class FakeCapture:
        def __init__(self, owner: Any, directory: Path, *, profile: Path | None = None) -> None:
            self.directory = directory
            self.profile = profile
            self.active = False
            self.supervisor = Mock(process=Mock(pid=2000 + len(captures)))
            self.supervisor.stop.side_effect = self.stopped
            captures.append(self)

        def start(self) -> None:
            events.append("start")

        def prepare(self, target: dict[str, Any]) -> None:
            events.append("prepare:" + target["kind"])
            assert players[0].foreign and players[0].live

        def open(self) -> None:
            events.append("open")
            self.active = True
            (self.directory / "audio/far-0000.wav").write_bytes(b"fixed-evidence")

        def tick(self) -> None:
            assert players[0].live
            now[0] += 0.25

        def stopped(self) -> None:
            self.active = False

        def stop(self) -> None:
            events.append("stop")
            self.stopped()

        def wait(self, kind: str, **kwargs: Any) -> dict[str, str]:
            assert players[0].live
            events.append("target-ended")
            return {"track": "far", "state": "ended"}

        def intervals(self) -> list[dict[str, int]]:
            return []

    def killpg(pid: int, number: int) -> None:
        assert number == signal.SIGKILL
        assert players[0].live and sum(p.live for p in players) == 3
        events.append("kill9")
        for capture in captures:
            if capture.supervisor.process.pid == pid:
                capture.active = False

    monkeypatch.setattr("tools.meeting_stand.os.killpg", killpg)
    monkeypatch.setattr(stand, "Capture", FakeCapture)
    monkeypatch.setattr(stand, "_has_signal", lambda directory: True)
    sampler = Mock()
    sampler.sample.side_effect = lambda roles, **kwargs: {"t_mono_s": kwargs["now_s"]}
    monkeypatch.setattr(metrics, "ProcSampler", lambda: sampler)

    def analyze(path: Path, **kwargs: Any) -> dict[str, Any]:
        analyses.append("crash" if "crash_mono_ms" in kwargs else "normal")
        return {"criteria": [{"name": analyses[-1], "status": "pass"}]}

    def exclusive(path: Path, **kwargs: Any) -> dict[str, Any]:
        analyses.append("exclusive")
        assert players[0].live and kwargs["expected_beeps_ms"]
        return {"criteria": [{"name": "farforeign_rms_dbfs", "status": "pass"}]}

    def resources(samples: list[dict[str, Any]]) -> dict[str, Any]:
        assert samples[-1]["t_mono_s"] - samples[0]["t_mono_s"] >= 120
        return {"criteria": [], "full_soak": False, "projected": True}

    monkeypatch.setattr(metrics, "analyze_recording", analyze)
    monkeypatch.setattr(metrics, "analyze_exclusivity", exclusive, raising=False)
    monkeypatch.setattr(metrics, "summarize_resources", resources)
    profile = tmp_path / "capture.pstats" if profiled else None
    result = stand.exercise(owner, tmp_path, 2, "pipewire", profile)
    assert [capture.profile for capture in captures] == (
        list(stand.capture_profile_paths(profile).values()) if profile else [None] * 4
    )
    assert result["rc"] == 0
    import wave

    with wave.open(str(tmp_path / "beeps.wav"), "rb") as wav:
        assert wav.getnframes() / wav.getframerate() >= 120 + 180
    assert events[0] == "sentinel"
    assert analyses == ["normal", "exclusive", "crash"]
    assert events.count("prepare:app_stream") == 3
    assert events.count("prepare:sink_monitor") == 1
    assert events.count("kill9") == 1
    assert all(1000 in snapshot for snapshot in snapshots)
    assert any(len(snapshot) == 3 for snapshot in snapshots)
    assert len(captures) == 4
    names = {item["name"] for item in result["criteria"]}
    assert {
        "prepare_zero_pcm",
        "normal_graph_unchanged",
        "crash_graph_unchanged",
        "foreign_audio_rejected",
        "farforeign_rms_dbfs",
    } <= names
    assert now[0] < 340  # --minutes 2 plus all side scenarios remains under four minutes.


@pytest.mark.parametrize("status", ["failed", "partial"])
def test_authoritative_finalization_failure_is_fail_not_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    capture = stand.Capture(stand.ProcessOwner({}), tmp_path / "meeting")
    monkeypatch.setattr(capture, "send", lambda *args, **kwargs: "stop-request")
    monkeypatch.setattr(capture, "wait", lambda *args, **kwargs: {"status": status})
    monkeypatch.setattr(stand, "preflight", lambda server: None)
    monkeypatch.setattr(
        "tools.meeting_stand.tempfile.mkdtemp", lambda **kwargs: str(tmp_path / "private")
    )
    monkeypatch.setattr(stand, "exercise", lambda *args: capture.stop())
    rc, report = stand.run_stand(minutes=1)
    assert rc == 1 and report["exit_code"] == 1 and report["verdict"] == "fail"
    assert report["cleanup"] is True
    assert "capture-finalization-failed" in report["diagnostics"]
    assert any(
        row["name"] == "capture_session" and row["status"] == "fail" for row in report["criteria"]
    )


def test_profile_waits_for_external_started_grant(tmp_path: Path) -> None:
    wrapper = Path(stand.__file__).with_name("meeting_stand_profile.py")
    output, gate, ready = (tmp_path / name for name in ("profile.json", "grant", "started"))
    bootstrap = tmp_path / "startup.py"
    bootstrap.write_text(
        "import builtins, os, pathlib, select, sys, threading, time, types\n"
        "assert isinstance(builtins.__import__, types.BuiltinFunctionType)\n"
        "assert sys.getprofile() is None and threading.getprofile() is None\n"
        "assert len(threading.enumerate()) == 1\n"
        "assert not any(n.startswith('astra_voice') for n in sys.modules)\n"
        f"assert not pathlib.Path({str(tmp_path / 'pcm')!r}).exists()\n"
        "print('prepared', flush=True)\n"
        "assert sys.stdin.readline() == 'open\\n'\n"
        f"pathlib.Path({str(ready)!r}).touch()\n"
        "time.sleep(0.3)\n"
        "assert len(threading.enumerate()) == 1\n"
        f"pathlib.Path({str(tmp_path / 'pcm')!r}).touch()\n"
        "print('meeting.capture.started', flush=True)\n"
        "for _ in range(30): select.select([], [], [], 0.05)\n"
    )
    process = subprocess.Popen(
        [
            sys.executable,
            "-I",
            str(wrapper),
            str(output),
            str(gate),
            str(bootstrap),
            "meeting-capture",
            "9",
        ],
        env=stand.private_environment(tmp_path / "private"),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        assert process.stdout is not None
        assert process.stdout.readline() == b"prepared\n"
        assert not (tmp_path / "pcm").exists()
        assert process.stdin is not None
        process.stdin.write(b"open\n")
        process.stdin.flush()
        assert process.stdout.readline() == b"meeting.capture.started\n"
        before = json.loads(output.read_text())
        assert before["status"] == "waiting-for-start"
        assert before["samples"] == 0
        gate.write_text(f"start:{process.pid}")
        gate.chmod(0o600)
        process.send_signal(signal.SIGUSR1)
        stdout, stderr = process.communicate(timeout=5)
        assert process.returncode == 0, stderr
        data = json.loads(output.read_text())
        assert data["activation"] == "after-meeting.capture.started"
        assert data["status"] == "complete" and data["samples"] > 0
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=2)


def test_profile_never_started_is_not_complete(tmp_path: Path) -> None:
    wrapper = Path(stand.__file__).with_name("meeting_stand_profile.py")
    output, gate = tmp_path / "profile.json", tmp_path / "grant"
    bootstrap = tmp_path / "stub.py"
    bootstrap.write_text("raise RuntimeError('SECRET')\n")
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            str(wrapper),
            str(output),
            str(gate),
            str(bootstrap),
            "meeting-capture",
            "9",
        ],
        env=stand.private_environment(tmp_path / "private"),
        capture_output=True,
        timeout=5,
    )
    assert result.returncode == 2
    data = json.loads(output.read_text())
    assert data["status"] == "not-started" and data["samples"] == 0
    assert data["errors"] == ["capture-profile-bootstrap-failed"]
    assert "SECRET" not in output.read_text()


def test_profile_open_grants_only_after_started(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capture = stand.Capture(Mock(spec=stand.ProcessOwner), tmp_path, profile=tmp_path / "p.json")
    events: list[str] = []
    monkeypatch.setattr(capture, "send", lambda *a, **kw: "open-req")
    monkeypatch.setattr(capture, "wait", lambda kind, req: events.append(kind))
    monkeypatch.setattr(capture, "activate_profile", lambda: events.append("grant"), raising=False)
    capture.open()
    assert events == ["meeting.capture.started", "grant"]


def test_profile_failed_open_finishes_without_grant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capture = stand.Capture(Mock(spec=stand.ProcessOwner), tmp_path, profile=tmp_path / "p.json")
    monkeypatch.setattr(capture, "send", lambda *a, **kw: "open-req")

    def failed(*args: Any, **kwargs: Any) -> None:
        raise stand.StandUnavailable("capture-operation-failed:open:audio-failed")

    monkeypatch.setattr(capture, "wait", failed)
    activate, finish = Mock(), Mock()
    monkeypatch.setattr(capture, "activate_profile", activate, raising=False)
    monkeypatch.setattr(capture, "finish_failed_profile", finish, raising=False)
    with pytest.raises(stand.StandUnavailable, match="open:audio-failed"):
        capture.open()
    activate.assert_not_called()
    finish.assert_called_once_with("capture-operation-failed:open:audio-failed")


def test_profile_signal_only_requests_main_control_activation(tmp_path: Path) -> None:
    import threading

    from tools.meeting_stand_profile import Activation

    activation = Activation(tmp_path / "p.json", tmp_path / "grant")
    activate = Mock()
    activation.activate = activate  # type: ignore[method-assign]
    activation.request(signal.SIGUSR1, None)
    activate.assert_not_called()
    thread = threading.Thread(target=lambda: activation.select([], [], [], 0))
    thread.start()
    thread.join(timeout=1)
    activate.assert_not_called()
    activation.select([], [], [], 0)
    activate.assert_called_once_with()


def test_profile_failed_thread_start_is_never_activated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tools.meeting_stand_profile import Activation, Sampler

    output, gate = tmp_path / "p.json", tmp_path / "grant"
    gate.write_text(f"start:{os.getpid()}")
    gate.chmod(0o600)
    activation = Activation(output, gate)

    def fail_start(self: Any) -> None:
        raise RuntimeError("cannot allocate thread")

    monkeypatch.setattr(Sampler, "start", fail_start)
    activation.activate()
    activation.close()
    data = json.loads(output.read_text())
    assert data["status"] == "partial"
    assert data["activated"] is False and data["samples"] == 0
    assert data["errors"] == ["capture-profile-activation-failed"]


@pytest.mark.parametrize("failure", [RuntimeError, OSError, MemoryError])
def test_profile_thread_unavailable_keeps_exact_sections_without_more_threads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: type[Exception]
) -> None:
    import threading
    import types

    from tools.meeting_stand_profile import Activation

    class MeetingCaptureState:
        def poll(self) -> int:
            return 42

    original = MeetingCaptureState.poll
    module = types.ModuleType("astra_voice.worker.meeting_capture")
    module.MeetingCaptureState = MeetingCaptureState  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, module.__name__, module)
    starts = []

    def cannot_start(thread: threading.Thread) -> None:
        starts.append(thread)
        raise failure("PRIVATE resource diagnostic")

    monkeypatch.setattr(threading.Thread, "start", cannot_start)
    output, gate = tmp_path / "p.json", tmp_path / "grant"
    gate.write_text(f"start:{os.getpid()}")
    gate.chmod(0o600)
    activation = Activation(output, gate)
    try:
        activation.activate()
        running = json.loads(output.read_text())
        assert running["status"] == "running" and running["activated"] is True
        assert running["mode"] == "sections-only"
        assert running["sampling_status"] == "unavailable-thread-start"
        assert running["checkpoint_interval_seconds"] is None
        assert MeetingCaptureState().poll() == 42
    finally:
        activation.close()
    assert MeetingCaptureState.poll is original
    data = json.loads(output.read_text())
    assert data["status"] == "complete" and data["activated"] is True
    assert data["mode"] == "sections-only" and data["samples"] == 0
    assert data["errors"] == []
    assert len(starts) == 1 and starts[0].ident is None
    row = next(row for row in data["sections"] if row["label"] == "MeetingCaptureState.poll")
    assert row["calls"] == 1 and row["installed"] is True
    assert row["cpu_seconds"] >= 0 and row["wall_seconds"] >= 0
    assert "PRIVATE" not in output.read_text()
