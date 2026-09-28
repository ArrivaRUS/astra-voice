"""Интеграция Notify только через собственную dbus-run-session."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from helpers.private_bus import assert_private_bus_result, run_on_private_bus

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[2]
_SCENARIO = _ROOT / "tests" / "helpers" / "notify_dbus_scenarios.py"


@pytest.mark.parametrize(
    "scenario",
    [
        "late_reply",
        "late_reply_then_next",
        "late_beyond_wait",
        "action",
        "replacement",
        "no_owner",
        "invalid_args",
        "fast_return",
        "timeouts",
        "slot",
    ],
)
def test_notify_dbus(scenario: str, tmp_path: Path) -> None:
    result = run_on_private_bus(
        [sys.executable, str(_SCENARIO), scenario], tmp_dir=tmp_path, timeout=30
    )
    assert_private_bus_result(result, marker=f"NOTIFY_DBUS_OK:{scenario}")
