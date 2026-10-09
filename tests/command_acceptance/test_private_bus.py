"""Private, activation-free D-Bus acceptance; never the user's session bus."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from helpers.private_bus import assert_private_bus_result, run_on_private_bus

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "scenario",
    [
        "accepted",
        "busy",
        "bad_echo",
        "malformed",
        "Failed",
        "foreign_error",
        "late",
        "owner_loss",
        "sleep",
        "no_owner",
        "long_uptime",
        "queued_deadline",
        "guard",
        "held_timeout",
        "held_sleep",
    ],
)
def test_real_qtdbus_single_attempt_delivery(scenario: str, tmp_path: Path) -> None:
    result = run_on_private_bus(
        [
            sys.executable,
            str(Path(__file__).with_name("bus_scenarios.py")),
            scenario,
            str(tmp_path / "events.jsonl"),
        ],
        tmp_dir=tmp_path,
        timeout=8,
    )
    assert_private_bus_result(result, marker="COMMAND_ACCEPTANCE_OK")
