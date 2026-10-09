"""Process lifecycle only: these tests never import Qt or open X11/audio/D-Bus."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from supervisor import supervise

PROBE = Path(__file__).with_name("probe.py")


def argv(mode: str) -> list[str]:
    return [
        sys.executable,
        str(PROBE),
        "--child",
        "--parent-pid",
        str(os.getpid()),
        "--simulate",
        mode,
    ]


def test_default_is_dry_run_even_with_display_environment() -> None:
    result = subprocess.run(
        [sys.executable, str(PROBE)],
        capture_output=True,
        text=True,
        env={**os.environ, "DISPLAY": ":99", "DBUS_SESSION_BUS_ADDRESS": "unix:path=/nonexistent"},
        timeout=2,
        check=False,
    )
    assert result.returncode == 0
    assert json.loads(result.stdout)["event"] == "dry_run_no_X"


@pytest.mark.parametrize(
    "args",
    [
        ["--confirm-live"],
        ["--live-display", ":99"],
        ["--duration", "61"],
        ["--live-display", "remote:0", "--confirm-live"],
    ],
)
def test_invalid_cli_never_reaches_live_child(args: list[str]) -> None:
    result = subprocess.run(
        [sys.executable, str(PROBE), *args], capture_output=True, text=True, timeout=2, check=False
    )
    assert result.returncode == 2
    assert "supervisor_started" not in result.stdout


def test_normal_exit_is_reaped() -> None:
    events: list[dict[str, object]] = []
    assert supervise(argv("exit"), emit=events.append) == 0
    assert events[-1]["reason"] == "child_exit"
    assert events[-1]["child_rc"] == 0


def test_hard_deadline_reaps_even_healthy_child() -> None:
    events: list[dict[str, object]] = []
    started = time.monotonic()
    assert supervise(argv("heartbeat"), duration=0.3, emit=events.append) == 2
    assert time.monotonic() - started < 1.0
    assert events[-1]["reason"] == "deadline"
    assert events[-1]["child_rc"] == -signal.SIGTERM


@pytest.mark.parametrize("mode", ["silent", "stop"])
def test_watchdog_kills_unresponsive_child_without_waiting_for_gui(mode: str) -> None:
    events: list[dict[str, object]] = []
    started = time.monotonic()
    assert supervise(argv(mode), duration=3, emit=events.append) == 2
    assert time.monotonic() - started < 1.15
    assert events[-1]["reason"] == "heartbeat_timeout"
    assert events[-1]["child_rc"] == -signal.SIGKILL
    assert {"event": "kill_child"} in events
    child_pid = int(str(events[0]["child_pid"]))
    assert not Path(f"/proc/{child_pid}").exists()  # wait() reaped only our child


def test_parent_death_kills_child_even_when_child_is_stopped(tmp_path: Path) -> None:
    code = (
        "import os, subprocess, sys, time; "
        f"p=subprocess.Popen([sys.executable, {str(PROBE)!r}, '--child', '--parent-pid', "
        "str(os.getpid()), '--simulate', 'stop'], stdout=subprocess.PIPE); "
        "assert p.stdout.readline() == b'HB\\n'; print(p.pid, flush=True); time.sleep(10)"
    )
    parent = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    child_pid = None
    try:
        assert parent.stdout is not None
        child_pid = int(parent.stdout.readline())
        parent.kill()
        parent.wait(timeout=1)
        until = time.monotonic() + 1
        while time.monotonic() < until:
            status = Path(f"/proc/{child_pid}/stat")
            if not status.exists() or status.read_text().split(") ", 1)[1].startswith("Z"):
                break
            time.sleep(0.02)
        else:
            pytest.fail("child survived parent death")
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait()
        if parent.stdout is not None:
            parent.stdout.close()
