"""Бутстрап: диспетчер команд, sys.path, обе раскладки, любой cwd."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from astra_voice import bootstrap

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
DEV_BOOTSTRAP = REPO_ROOT / "src" / "astra_voice" / "bootstrap.py"


@pytest.fixture(autouse=True)
def isolated_xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("CACHE", "CONFIG", "DATA", "STATE"):
        monkeypatch.setenv(f"XDG_{name}_HOME", str(tmp_path / name.lower()))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))


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


def test_dev_layout_dispatches_worker_without_fd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ASTRA_VOICE_IPC_FD", raising=False)
    proc = _run(DEV_BOOTSTRAP, ["worker"], cwd=tmp_path)
    assert proc.returncode == 2
    assert "astra-voice worker: требуется числовой IPC fd" in proc.stderr


def test_worker_bootstrap_sets_pulse_clientconfig(tmp_path: Path) -> None:
    script = tmp_path / "check_bootstrap.py"
    script.write_text(
        "import os, runpy\n"
        "from pathlib import Path\n"
        f"bootstrap = runpy.run_path({str(DEV_BOOTSTRAP)!r})\n"
        "assert bootstrap['main'](['worker']) == 2\n"
        "assert os.environ['PULSE_SERVER'] == 'unix:/nonexistent'\n"
        "path = Path(os.environ['PULSE_CLIENTCONFIG'])\n"
        "print(path)\n"
        "print(path.read_text(encoding='utf-8'))\n",
        encoding="utf-8",
    )
    env = dict(os.environ)
    env.pop("PULSE_CLIENTCONFIG", None)
    env.pop("ASTRA_VOICE_IPC_FD", None)
    env["PULSE_SERVER"] = "unix:/nonexistent"
    proc = subprocess.run(
        [sys.executable, "-I", str(script)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    filename, content = proc.stdout.split("\n", 1)
    path = Path(filename)
    assert path.is_absolute() and path.is_relative_to(tmp_path)
    assert path == tmp_path / "cache" / "astra-voice" / "pulse-client.conf"
    assert "autospawn = no" in content


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


# ── выбор рендера Qt Quick (RSS: 167 740 → 102 580 кБ, spikes/m1_live/rss.md) ──


def _clear_render_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(bootstrap.RENDER_ENV, raising=False)
    for name in bootstrap.SOFTWARE_RENDER_ENV:
        monkeypatch.delenv(name, raising=False)


def test_render_defaults_to_software(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_render_env(monkeypatch)
    bootstrap._setup_render_env()
    assert os.environ["QT_QUICK_BACKEND"] == "software"
    assert os.environ["QT_XCB_GL_INTEGRATION"] == "none"


@pytest.mark.parametrize("value", ["gl", "GL", " gl "])
def test_render_gl_sets_nothing(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    _clear_render_env(monkeypatch)
    monkeypatch.setenv(bootstrap.RENDER_ENV, value)
    bootstrap._setup_render_env()
    assert "QT_QUICK_BACKEND" not in os.environ
    assert "QT_XCB_GL_INTEGRATION" not in os.environ


def test_render_unknown_value_falls_back_to_software(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_render_env(monkeypatch)
    monkeypatch.setenv(bootstrap.RENDER_ENV, "чепуха")
    bootstrap._setup_render_env()
    assert os.environ["QT_QUICK_BACKEND"] == "software"


def test_render_keeps_user_values(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_render_env(monkeypatch)
    monkeypatch.setenv("QT_QUICK_BACKEND", "openvg")
    bootstrap._setup_render_env()
    assert os.environ["QT_QUICK_BACKEND"] == "openvg"  # выбор пользователя не перебит
    assert os.environ["QT_XCB_GL_INTEGRATION"] == "none"


def test_app_command_sets_render_env(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_render_env(monkeypatch)
    assert bootstrap.main(["app", "--version"]) == 0
    assert os.environ["QT_QUICK_BACKEND"] == "software"
    assert os.environ["QT_QUICK_CONTROLS_STYLE"] == "Default"


def test_worker_command_leaves_render_env_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_render_env(monkeypatch)
    monkeypatch.delenv("QT_QUICK_CONTROLS_STYLE", raising=False)
    assert bootstrap.main(["worker"]) == 2
    assert "QT_QUICK_BACKEND" not in os.environ
    assert "QT_XCB_GL_INTEGRATION" not in os.environ
    assert "QT_QUICK_CONTROLS_STYLE" not in os.environ
