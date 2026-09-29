"""Длинные циклы трея на частной шине, вне CI.

Запуск: pytest -m stress tests/stress/test_tray_dbus_stress.py.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from helpers.private_bus import assert_private_bus_result, run_on_private_bus

pytestmark = pytest.mark.stress
_SCENARIO = Path(__file__).resolve().parents[1] / "helpers" / "tray_dbus_lifetime.py"


@pytest.mark.parametrize("amplified", [False, True], ids=["normal", "frequent-switch-gc"])
def test_tray_dbus_cycles(request: pytest.FixtureRequest, tmp_path: Path, amplified: bool) -> None:
    if "stress" not in (request.config.option.markexpr or ""):
        pytest.skip("только по явному -m stress")
    env = {
        "XDG_CURRENT_DESKTOP": "KDE",
        "ASTRA_VOICE_TRAY_CYCLES": "3000",
        "ASTRA_VOICE_TRAY_SWITCH": "1e-6" if amplified else "",
        "ASTRA_VOICE_TRAY_GC": "1" if amplified else "",
    }
    result = run_on_private_bus(
        [sys.executable, str(_SCENARIO), "plasma_reply"],
        tmp_dir=tmp_path,
        timeout=300,
        extra_env=env,
    )
    assert_private_bus_result(result, marker="TRAY_DBUS_OK:plasma_reply")
