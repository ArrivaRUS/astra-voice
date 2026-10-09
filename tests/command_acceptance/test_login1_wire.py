"""Optional typed login1 peer proves native Qt5 demarshalling, isolated from system bus."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from helpers.private_bus import assert_private_bus_result, run_on_private_bus

pytestmark = pytest.mark.unit


def test_native_login1_complex_types_open_only_verified_session(tmp_path: Path) -> None:
    if importlib.util.find_spec("dbus") is None or importlib.util.find_spec("gi") is None:
        pytest.skip("typed login1 peer requires optional dbus-python and gi")
    result = run_on_private_bus(
        [
            sys.executable,
            str(Path(__file__).with_name("login1_scenario.py")),
            str(tmp_path / "login.ready"),
        ],
        tmp_dir=tmp_path,
        timeout=8,
    )
    assert_private_bus_result(result, marker="TYPED_LOGIN1_OK")
