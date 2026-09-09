"""Пути: XDG, приватный runtime-каталог, корень ресурсов."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from astra_voice.core import paths

pytestmark = pytest.mark.unit


def test_xdg_dirs_follow_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "dat"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cch"))
    assert paths.config_dir() == tmp_path / "cfg" / "astra-voice"
    assert paths.data_dir() == tmp_path / "dat" / "astra-voice"
    assert paths.cache_dir() == tmp_path / "cch" / "astra-voice"
    assert paths.settings_path().name == "settings.json"
    assert stat.S_IMODE(paths.config_dir().stat().st_mode) == 0o700


def test_relative_xdg_value_ignored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", "relative/path")
    assert paths.config_dir() == tmp_path / ".config" / "astra-voice"


def test_runtime_dir_uses_xdg_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    rt = paths.runtime_dir()
    assert rt == tmp_path / "run" / "astra-voice"
    assert stat.S_IMODE(rt.stat().st_mode) == 0o700
    assert paths.lock_path() == rt / "lock"
    assert paths.ipc_socket_path() == rt / "ipc"


def test_runtime_dir_fallback_to_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.setattr(paths, "FALLBACK_TMP_DIR", tmp_path)
    rt = paths.runtime_dir()
    assert rt == tmp_path / f"astra-voice-{os.getuid()}"
    assert stat.S_IMODE(rt.stat().st_mode) == 0o700


def test_runtime_dir_symlink_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.setattr(paths, "FALLBACK_TMP_DIR", tmp_path)
    target = tmp_path / "elsewhere"
    target.mkdir()
    (tmp_path / f"astra-voice-{os.getuid()}").symlink_to(target)
    with pytest.raises(paths.PathError):
        paths.runtime_dir()


def test_runtime_dir_bad_xdg_falls_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bad = tmp_path / "run"
    bad.mkdir()
    (bad / "astra-voice").symlink_to(tmp_path)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(bad))
    monkeypatch.setattr(paths, "FALLBACK_TMP_DIR", tmp_path / "tmp")
    (tmp_path / "tmp").mkdir()
    assert paths.runtime_dir() == tmp_path / "tmp" / f"astra-voice-{os.getuid()}"


def test_loose_mode_is_tightened(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    (tmp_path / "run" / "astra-voice").mkdir(parents=True, mode=0o755)
    assert stat.S_IMODE(paths.runtime_dir().stat().st_mode) == 0o700


def test_resource_root_env_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(paths.RESOURCES_ENV, str(tmp_path))
    assert paths.resource_root() == tmp_path
    assert paths.qml_dir() == tmp_path / "qml"
    assert paths.data_dir_static() == tmp_path / "data"


def test_resource_root_dev_is_repo_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(paths.RESOURCES_ENV, raising=False)
    root = paths.resource_root()
    assert (root / "src" / "astra_voice").is_dir()
    assert (root / "pyproject.toml").is_file()
