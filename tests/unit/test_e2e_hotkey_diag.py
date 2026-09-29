"""Хелпер e2e: поиск процесса приложения по точному argv без pgrep/регулярок."""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/e2e/hotkey_diag.py"

pytestmark = pytest.mark.unit


def load_diag() -> ModuleType:
    spec = importlib.util.spec_from_file_location("hotkey_diag", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["hotkey_diag"] = module
    spec.loader.exec_module(module)
    return module


def add_process(proc: Path, pid: str, argv: list[str]) -> None:
    entry = proc / pid
    entry.mkdir()
    (entry / "cmdline").write_bytes(b"".join(part.encode() + b"\0" for part in argv))


def test_app_pids_match_exact_argv_only(tmp_path: Path) -> None:
    diag = load_diag()
    app = ["/usr/bin/python3", "-I", "/usr/lib/astra-voice/bootstrap.py", "app"]
    add_process(tmp_path, "101", app)
    add_process(tmp_path, "102", [*app, "--hidden"])
    add_process(tmp_path, "103", [*app, "--stats"])
    add_process(tmp_path, "104", ["/usr/bin/python3", "-m", "astra_voice"])
    add_process(tmp_path, "105", ["sh", "-c", " ".join(app)])
    add_process(tmp_path, "106", ["/usr/bin/python3", "-I", "/usr/lib/astra-voice/bootstrap.py"])
    (tmp_path / "self").mkdir()
    assert diag.app_pids(tmp_path, uid=os.getuid()) == [101, 102]
    assert diag.app_pids(tmp_path, uid=os.getuid() + 1) == []
