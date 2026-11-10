"""Модули встреч импортируются без numpy: смоук установленного дерева (CI PR #65)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

SRC = Path(__file__).resolve().parents[2] / "src"

# numpy — зависимость времени выполнения роли; импорт модуля не должен её требовать.
_PROBE = """
import importlib
import sys

sys.path.insert(0, sys.argv[1])
sys.modules["numpy"] = None
importlib.import_module(sys.argv[2])
"""


@pytest.mark.parametrize(
    "module",
    [
        "astra_voice.meetings.echo_gate",
        "astra_voice.worker.meeting_transcribe",
        "astra_voice.models.diarization",
        "astra_voice.worker.meeting_diar",
    ],
)
def test_module_imports_without_numpy(module: str) -> None:
    result = subprocess.run(
        [sys.executable, "-I", "-c", _PROBE, str(SRC), module],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
