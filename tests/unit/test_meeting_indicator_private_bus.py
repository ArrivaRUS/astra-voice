"""T417 uses the no-service-activation bus harness, never a user session bus."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from helpers.private_bus import assert_private_bus_result, run_on_private_bus

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("filesystem_euid")]


def test_t417_real_watcher_loss_and_w2_recovery(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    result = run_on_private_bus(
        [
            sys.executable,
            str(root / "tests/helpers/meeting_indicator_private_bus.py"),
            str(tmp_path),
        ],
        tmp_dir=tmp_path,
        timeout=30,
        extra_env={
            "XDG_CURRENT_DESKTOP": "KDE",
            "PYTHONPATH": os.pathsep.join([str(root / "tests"), str(root / "src")]),
        },
    )
    evidence = tmp_path / "private-bus-result.txt"
    evidence.write_text(
        f"rc={result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}",
        encoding="utf-8",
    )
    denied_bind = (
        result.returncode == 127
        and "Failed to bind socket" in result.stderr
        and "Operation not permitted" in result.stderr
    )
    if denied_bind and os.environ.get("ASTRA_VOICE_REQUIRE_QT") != "1":
        pytest.skip(f"sandbox denies private D-Bus AF_UNIX bind; evidence: {evidence}")
    assert_private_bus_result(result, marker="MEETING_INDICATORS_PRIVATE_BUS_OK")
