"""Конфигурация libpulse без обращения к звуку и настоящему профилю."""

from __future__ import annotations

import logging
import os
import stat
import tempfile
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import Mock

import pytest

from astra_voice.core import audio_env, paths

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True, params=[None, "unix:/nonexistent"])
def isolated_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> Iterator[None]:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.delenv("PULSE_CLIENTCONFIG", raising=False)
    server = request.param
    if server is None:
        monkeypatch.delenv("PULSE_SERVER", raising=False)
    else:
        monkeypatch.setenv("PULSE_SERVER", server)
    yield
    assert os.environ.get("PULSE_SERVER") == server


def test_creates_private_config_without_include(tmp_path: Path) -> None:
    audio_env.deny_pulse_autospawn()

    path = tmp_path / "cache" / "astra-voice" / "pulse-client.conf"
    assert os.environ["PULSE_CLIENTCONFIG"] == str(path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[0].startswith("#") and "Astra Voice" in lines[0]
    assert lines[1:] == ["autospawn = no", "autospawn = no"]


@pytest.mark.parametrize("config_home", ["xdg", "empty", "unset"])
def test_includes_existing_user_config_between_denials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, config_home: str
) -> None:
    if config_home == "xdg":
        directory = tmp_path / "config"
    else:
        directory = tmp_path / "home" / ".config"
        if config_home == "empty":
            monkeypatch.setenv("XDG_CONFIG_HOME", "")
        else:
            monkeypatch.delenv("XDG_CONFIG_HOME")
    user_config = directory / "pulse" / "client.conf"
    user_config.parent.mkdir(parents=True)
    original = "autospawn = yes\ndefault-sink = custom\n"
    user_config.write_text(original, encoding="utf-8")

    audio_env.deny_pulse_autospawn()

    path = Path(os.environ["PULSE_CLIENTCONFIG"])
    assert path.read_text(encoding="utf-8").splitlines()[1:] == [
        "autospawn = no",
        f".include {user_config}",
        "autospawn = no",
    ]
    assert user_config.read_text(encoding="utf-8") == original


@pytest.mark.parametrize("character", ["#", ";", "\n", "\r"])
def test_unsafe_include_path_is_skipped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    character: str,
) -> None:
    directory = tmp_path / f"config{character}suffix"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(directory))
    user_config = directory / "pulse" / "client.conf"
    user_config.parent.mkdir(parents=True)
    user_config.write_text("autospawn = yes\n", encoding="utf-8")
    caplog.set_level(logging.DEBUG, logger=audio_env.__name__)

    audio_env.deny_pulse_autospawn()

    path = Path(os.environ["PULSE_CLIENTCONFIG"])
    assert path.read_text(encoding="utf-8").splitlines()[1:] == [
        "autospawn = no",
        "autospawn = no",
    ]
    assert len(caplog.records) == 1
    assert caplog.records[0].levelno == logging.DEBUG
    assert "путь содержит" in caplog.records[0].getMessage()


@pytest.mark.parametrize("clear_env", [False, True])
def test_repeated_call_does_not_rewrite(monkeypatch: pytest.MonkeyPatch, clear_env: bool) -> None:
    audio_env.deny_pulse_autospawn()
    value = os.environ["PULSE_CLIENTCONFIG"]
    path = Path(value)
    original = path.read_bytes()
    # Делаем проверку времени чувствительной даже на ФС с грубой точностью часов.
    os.utime(path, ns=(1_000_000_000, 1_000_000_000))
    before = path.stat()
    if clear_env:
        monkeypatch.delenv("PULSE_CLIENTCONFIG")

    audio_env.deny_pulse_autospawn()

    assert os.environ["PULSE_CLIENTCONFIG"] == value
    assert path.read_bytes() == original
    assert path.stat().st_mtime_ns == before.st_mtime_ns
    assert path.stat().st_ino == before.st_ino


@pytest.mark.parametrize("value", ["/admin/client.conf", " "])
def test_existing_environment_is_respected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    value: str,
) -> None:
    monkeypatch.setenv("PULSE_CLIENTCONFIG", value)
    caplog.set_level(logging.DEBUG, logger=audio_env.__name__)

    audio_env.deny_pulse_autospawn()

    assert os.environ["PULSE_CLIENTCONFIG"] == value
    assert not (tmp_path / "cache").exists()
    assert len(caplog.records) == 1
    assert caplog.records[0].levelno == logging.DEBUG


def test_empty_environment_is_replaced(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PULSE_CLIENTCONFIG", "")
    audio_env.deny_pulse_autospawn()
    assert Path(os.environ["PULSE_CLIENTCONFIG"]).is_file()


@pytest.mark.parametrize("failure", ["replace", "mkstemp", "cache_dir", "home"])
def test_failure_is_logged_without_setting_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failure: str,
) -> None:
    if failure == "replace":
        monkeypatch.setattr(os, "replace", Mock(side_effect=OSError("запись")))
    elif failure == "mkstemp":
        monkeypatch.setattr(tempfile, "mkstemp", Mock(side_effect=OSError("файл")))
    elif failure == "cache_dir":
        monkeypatch.setattr(paths, "cache_dir", Mock(side_effect=paths.PathError("каталог")))
    else:
        monkeypatch.setattr(Path, "home", Mock(side_effect=RuntimeError("домашний каталог")))

    audio_env.deny_pulse_autospawn()

    assert "PULSE_CLIENTCONFIG" not in os.environ
    assert len(caplog.records) == 1
    record = caplog.records[0]
    assert record.levelno == logging.WARNING
    assert record.exc_info is not None
    assert "Не удалось запретить автозапуск звукового сервера" in record.getMessage()
    assert not list((tmp_path / "cache").rglob("*pulse-client.conf*"))
