"""Проверки времени жизни D-Bus-ответов и потоков в отдельной шине."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from helpers.private_bus import assert_private_bus_result, run_on_private_bus

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[2]
_SCENARIO = _ROOT / "tests" / "helpers" / "tray_dbus_lifetime.py"


@pytest.mark.parametrize(
    ("scenario", "repeat"),
    [("plasma_reply", 1), ("invariants", 1), ("thread_cleanup", 1), ("exit_after_stop", 20)],
)
def test_tray_dbus_lifetime(scenario: str, repeat: int, tmp_path: Path) -> None:
    for _ in range(repeat):
        result = run_on_private_bus(
            [sys.executable, str(_SCENARIO), scenario],
            tmp_dir=tmp_path,
            timeout=60,
            extra_env={"XDG_CURRENT_DESKTOP": "KDE"},
        )
        assert_private_bus_result(result, marker=f"TRAY_DBUS_OK:{scenario}")
