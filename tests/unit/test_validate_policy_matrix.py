"""Source CLI policy validation: subprocess, no display/audio or network needed."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


def run_cli(*args: str, script: str | None = None) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k not in {"DISPLAY", "WAYLAND_DISPLAY"}}
    env.update(
        PYTHONPATH=str(ROOT / "src") + os.pathsep + str(ROOT),
        DBUS_SESSION_BUS_ADDRESS="unix:path=/nonexistent",
    )
    command = (
        [sys.executable, "tools/validate"] if script is None else [sys.executable, "-c", script]
    )
    return subprocess.run(
        [*command, "meetings", "--policy-matrix", *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_cli_policy_matrix_json_and_no_gui_or_network() -> None:
    result = run_cli(
        "--json",
        script="""
import runpy, socket, sys
sys.modules['PyQt5'] = None
def denied(*args, **kwargs):
    raise AssertionError('network access')
socket.socket.connect = denied
socket.create_connection = denied
sys.argv[0] = 'tools/validate'
runpy.run_path('tools/validate', run_name='__main__')
""",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report["ok"] and report["cases"] >= 57 and not report["failures"]
    assert report["invariant_combinations"] == 1200
    assert len(report["rows"]) == report["cases"] * 7
    assert {row["key"] for row in report["rows"]} == {
        "meeting_record",
        "meeting_diarization",
        "meeting_ai_endpoint",
        "meeting_ai_allow_external",
        "meeting_ai_allow_plaintext",
        "meeting_audio_retention_days",
        "meeting_notice_text",
    }
    assert any(row["reason"] == "meeting-policy-unsafe" for row in report["rows"])


def test_cli_policy_matrix_table() -> None:
    result = run_cli()
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ключ · значение · решение · locked · reason-код" in result.stdout
    assert (
        "record-manual · meeting_record · manual · manual · True · meeting-policy-record-admin"
        in result.stdout
    )
    assert "PASS:" in result.stdout


def test_cli_policy_matrix_failure_returns_one() -> None:
    result = run_cli(
        "--json",
        script="""
import runpy, sys
from tools import meeting_policy_check as check
original = check.matrix_cases
rows = original()
rows[0].expect['decisions']['meeting_record'] = ('off', True, 'meeting-policy-record-admin')
check.matrix_cases = lambda: rows
sys.argv[0] = 'tools/validate'
runpy.run_path('tools/validate', run_name='__main__')
""",
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert not json.loads(result.stdout)["ok"]
    assert "decision meeting_record" in result.stdout


def test_cli_policy_matrix_rejects_stand_options() -> None:
    result = run_cli("--server", "pipewire")
    assert result.returncode == 2 and "только --json" in result.stderr


def test_cli_checks_invariants_even_without_table_rows() -> None:
    result = run_cli(
        "--json",
        script="""
import runpy, sys
from tools import meeting_policy_check as check
check.matrix_cases = lambda: []
original = check.effective_settings
def bad_opt_in(user, policy):
    result = original(user, policy)
    result.settings.meetings_enabled = True
    return result
check.effective_settings = bad_opt_in
sys.argv[0] = 'tools/validate'
runpy.run_path('tools/validate', run_name='__main__')
""",
    )
    assert result.returncode == 1, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report["cases"] == 0 and report["invariant_combinations"] == 1200
    assert any(failure.endswith("human-opt-in") for failure in report["failures"])
