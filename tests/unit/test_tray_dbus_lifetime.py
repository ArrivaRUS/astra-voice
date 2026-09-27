"""Проверки времени жизни D-Bus-ответов и потоков в отдельной шине."""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[2]
_SCENARIO = _ROOT / "tests" / "helpers" / "tray_dbus_lifetime.py"


@pytest.mark.parametrize(
    ("scenario", "repeat"),
    [("plasma_reply", 1), ("thread_cleanup", 1), ("exit_after_stop", 20)],
)
def test_tray_dbus_lifetime(scenario: str, repeat: int) -> None:
    if shutil.which("dbus-run-session") is None:
        reason = "нужен dbus-run-session (пакет dbus-daemon)"
        if os.environ.get("ASTRA_VOICE_REQUIRE_QT") == "1":
            pytest.fail(reason)
        pytest.skip(reason)
    env = os.environ.copy()
    for name in ("DISPLAY", "WAYLAND_DISPLAY", "DBUS_SESSION_BUS_ADDRESS"):
        env.pop(name, None)
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["XDG_CURRENT_DESKTOP"] = "KDE"
    env["PYTHONPATH"] = os.pathsep.join(
        filter(None, (str(_ROOT / "src"), env.get("PYTHONPATH", "")))
    )
    for attempt in range(1, repeat + 1):
        proc = subprocess.Popen(
            ["dbus-run-session", "--", sys.executable, str(_SCENARIO), scenario],
            start_new_session=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        try:
            try:
                stdout, stderr = proc.communicate(timeout=30)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                stdout, stderr = proc.communicate()
                pytest.fail(
                    f"{scenario} ({attempt}/{repeat}): таймаут 30 с; "
                    f"stdout={stdout!r}; stderr={stderr!r}"
                )
        finally:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        if proc.returncode != 0 or f"TRAY_DBUS_OK:{scenario}" not in stdout:
            pytest.fail(
                f"{scenario} ({attempt}/{repeat}): rc={proc.returncode}"
                f"\n--- stdout ---\n{stdout}\n--- stderr ---\n{stderr}",
                pytrace=False,
            )
