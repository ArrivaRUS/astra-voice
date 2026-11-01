"""The read-only W4 audio helper cannot outlive its GUI parent."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


def test_audio_preview_helper_dies_with_gui_stub(tmp_path: Path) -> None:
    executable = tmp_path / "pactl"
    pid_file = tmp_path / "helper.pid"
    executable.write_text(
        "#!/usr/bin/python3\n"
        "import os, time\n"
        "from pathlib import Path\n"
        "Path(os.environ['MEETING_TEST_PID']).write_text(str(os.getpid()))\n"
        "time.sleep(30)\n"
    )
    executable.chmod(0o700)
    source = """
import ctypes, os, signal, subprocess, sys, time
from pathlib import Path
assert ctypes.CDLL(None).prctl(36, 1, 0, 0, 0) == 0
code = (
    "import sys; sys.path.insert(0, sys.argv[1]); "
    "from astra_voice.ui.meeting_recording import probe_audio; probe_audio()"
)
parent = subprocess.Popen(
    [sys.executable, "-c", code, sys.argv[1]],
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
pid_path = Path(os.environ["MEETING_TEST_PID"])
helper = None
try:
    deadline = time.monotonic() + 8
    while not pid_path.exists() and time.monotonic() < deadline:
        if parent.poll() is not None:
            raise RuntimeError("preview-parent-exited")
        time.sleep(.005)
    helper = int(pid_path.read_text())
    parent.kill()
    parent.wait(timeout=2)
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        child, status = os.waitpid(helper, os.WNOHANG)
        if child:
            helper = None
            assert os.WIFSIGNALED(status) and os.WTERMSIG(status) == signal.SIGKILL
            print("helper-killed")
            break
        time.sleep(.005)
    else:
        raise AssertionError("helper-survived-gui")
finally:
    if parent.poll() is None:
        parent.kill()
    parent.wait(timeout=2)
    if helper is not None:
        os.kill(helper, signal.SIGKILL)
        os.waitpid(helper, 0)
"""
    environment = {
        **os.environ,
        "PATH": str(tmp_path) + os.pathsep + os.environ.get("PATH", ""),
        "MEETING_TEST_PID": str(pid_file),
    }
    result = subprocess.run(
        [sys.executable, "-c", source, str(Path(__file__).resolve().parents[2] / "src")],
        env=environment,
        capture_output=True,
        timeout=12,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode()
    assert result.stdout.strip() == b"helper-killed"
