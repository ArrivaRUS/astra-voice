"""Бутстрап: диспетчер команд, sys.path, обе раскладки, любой cwd."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from astra_voice import bootstrap

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
DEV_BOOTSTRAP = REPO_ROOT / "src" / "astra_voice" / "bootstrap.py"


def _run(script: Path, args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-I", str(script), *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _installed_layout(tmp_path: Path) -> Path:
    """Раскладка /usr/lib/astra-voice: bootstrap.py рядом с каталогом astra_voice."""
    root = tmp_path / "usr-lib-astra-voice"
    shutil.copytree(
        REPO_ROOT / "src" / "astra_voice",
        root / "astra_voice",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    shutil.copy2(DEV_BOOTSTRAP, root / "bootstrap.py")
    return root


def test_dev_layout_dispatches_worker_stub(tmp_path: Path) -> None:
    proc = _run(DEV_BOOTSTRAP, ["worker"], cwd=tmp_path)
    assert proc.returncode == 2
    assert "not implemented" in proc.stderr


def test_installed_layout_dispatches_helper_stub(tmp_path: Path) -> None:
    root = _installed_layout(tmp_path)
    proc = _run(root / "bootstrap.py", ["helper"], cwd=tmp_path)
    assert proc.returncode == 2
    assert "not implemented" in proc.stderr


def test_unknown_command_and_no_args(tmp_path: Path) -> None:
    for args in ([], ["nonsense"]):
        proc = _run(DEV_BOOTSTRAP, args, cwd=tmp_path)
        assert proc.returncode == 2
        assert "usage" in proc.stderr


def test_setup_sys_path_installed_prefers_vendor(tmp_path: Path) -> None:
    root = tmp_path / "usr-lib-astra-voice"
    (root / "astra_voice").mkdir(parents=True)
    (root / "vendor").mkdir()
    saved = list(sys.path)
    try:
        bootstrap._setup_sys_path(root)
        assert sys.path[0] == str(root / "vendor")
        assert sys.path[1] == str(root)
    finally:
        sys.path[:] = saved


def test_setup_sys_path_dev_uses_parent(tmp_path: Path) -> None:
    here = tmp_path / "src" / "astra_voice"
    here.mkdir(parents=True)
    saved = list(sys.path)
    try:
        bootstrap._setup_sys_path(here)
        assert sys.path[0] == str(tmp_path / "src")
        assert str(here / "vendor") not in sys.path
    finally:
        sys.path[:] = saved
