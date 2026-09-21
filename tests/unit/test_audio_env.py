"""Конфигурация libpulse без обращения к звуку и настоящему профилю."""

from __future__ import annotations

import logging
import os
import stat
import subprocess
import sys
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
    monkeypatch.setattr(audio_env, "SYSTEM_CLIENT_CONFIG", tmp_path / "system" / "client.conf")
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
    assert lines == ["autospawn = no", "autospawn = no"]


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
    assert path.read_text(encoding="utf-8").splitlines() == [
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
    assert path.read_text(encoding="utf-8").splitlines() == [
        "autospawn = no",
        "autospawn = no",
    ]
    assert len(caplog.records) == 1
    assert caplog.records[0].levelno == logging.DEBUG
    assert "путь содержит" in caplog.records[0].getMessage()


@pytest.mark.parametrize("user_exists", [False, True])
def test_system_config_is_only_fallback(tmp_path: Path, user_exists: bool) -> None:
    system_config = audio_env.SYSTEM_CLIENT_CONFIG
    system_config.parent.mkdir(parents=True)
    system_config.write_text("default-server = unix:/admin/pulse\nautospawn = yes\n")
    additions = system_config.with_name("client.conf.d")
    additions.mkdir()
    (additions / "autospawn.conf").write_text("autospawn = yes\n")
    user_config = tmp_path / "config" / "pulse" / "client.conf"
    if user_exists:
        user_config.parent.mkdir(parents=True)
        user_config.write_text("default-server = unix:/user/pulse\n")

    audio_env.deny_pulse_autospawn()

    path = Path(os.environ["PULSE_CLIENTCONFIG"])
    assert path.read_text().splitlines() == [
        "autospawn = no",
        f".include {user_config if user_exists else system_config}",
        "autospawn = no",
    ]
    assert system_config.read_text() == "default-server = unix:/admin/pulse\nautospawn = yes\n"


@pytest.mark.parametrize("damage", ["missing", "missing-directory", "changed"])
def test_own_environment_rechecks_config(damage: str) -> None:
    audio_env.deny_pulse_autospawn()
    value = os.environ["PULSE_CLIENTCONFIG"]
    path = Path(value)
    expected = path.read_bytes()
    if damage == "changed":
        path.write_text("autospawn = yes\n")
    else:
        path.unlink()
        if damage == "missing-directory":
            path.parent.rmdir()

    audio_env.deny_pulse_autospawn()

    assert os.environ["PULSE_CLIENTCONFIG"] == value
    assert path.read_bytes() == expected == b"autospawn = no\nautospawn = no\n"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize(
    "kind", ["fifo", "symlink", "directory", "nonempty-directory", "oversized"]
)
def test_unsafe_config_is_replaced_without_blocking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    path = paths.cache_dir() / "pulse-client.conf"
    target = tmp_path / "target.conf"
    original = b"autospawn = yes\n"
    target.write_bytes(original)
    if kind == "fifo":
        os.mkfifo(path, 0o666)
    elif kind == "symlink":
        path.symlink_to(target)
    elif kind in {"directory", "nonempty-directory"}:
        path.mkdir()
        if kind == "nonempty-directory":
            (path / "keep.conf").write_bytes(original)
            (path / "link.conf").symlink_to(target)
    else:
        path.write_bytes(b"x" * (64 * 1024 + 1))
        path.chmod(0o600)
    monkeypatch.setenv("PULSE_CLIENTCONFIG", str(path))
    # Отдельный процесс ограничивает время проверки даже при регрессии чтения FIFO.
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys\n"
            "sys.path.insert(0, sys.argv[2])\n"
            "from pathlib import Path\n"
            "from astra_voice.core import audio_env\n"
            "audio_env.SYSTEM_CLIENT_CONFIG = Path(sys.argv[1])\n"
            "audio_env.deny_pulse_autospawn()\n",
            str(audio_env.SYSTEM_CLIENT_CONFIG),
            str(Path(audio_env.__file__).resolve().parents[2]),
        ],
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert stat.S_ISREG(path.lstat().st_mode)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert path.read_bytes() == b"autospawn = no\nautospawn = no\n"
    assert target.read_bytes() == original
    if kind in {"directory", "nonempty-directory"}:
        displaced = list(path.parent.glob(".pulse-client.conf.replaced-*"))
        assert len(displaced) == 1
        assert stat.S_IMODE(displaced[0].stat().st_mode) == 0o700
        if kind == "nonempty-directory":
            assert (displaced[0] / path.name / "keep.conf").read_bytes() == original
            assert (displaced[0] / path.name / "link.conf").is_symlink()


def test_config_read_is_bounded_and_does_not_read_oversized_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "config"
    content = b"x" * (64 * 1024)
    path.write_bytes(content)
    path.chmod(0o600)
    read = Mock(wraps=os.read)
    open_config = Mock(wraps=os.open)
    monkeypatch.setattr(os, "read", read)
    monkeypatch.setattr(os, "open", open_config)
    assert audio_env._read_config(path) == content
    open_config.assert_called_once_with(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    read.assert_called_once()
    assert read.call_args.args[1] == 64 * 1024
    read.reset_mock()
    path.write_bytes(content + b"x")
    assert audio_env._read_config(path) is None
    read.assert_not_called()


def test_foreign_config_with_same_filename_is_respected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "pulse-client.conf"
    original = b"default-server = unix:/admin/server\n"
    path.write_bytes(original)
    monkeypatch.setenv("PULSE_CLIENTCONFIG", str(path))

    audio_env.deny_pulse_autospawn()

    assert os.environ["PULSE_CLIENTCONFIG"] == str(path)
    assert path.read_bytes() == original
    assert not (tmp_path / "cache" / "astra-voice" / "pulse-client.conf").exists()


def test_write_syncs_file_then_published_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "client.conf"
    synced: list[str] = []
    fsync = os.fsync

    def sync(fd: int) -> None:
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            assert path.read_bytes() == b"autospawn = no\n"
            synced.append("directory")
        else:
            assert not path.exists()
            synced.append("file")
        fsync(fd)

    monkeypatch.setattr(os, "fsync", sync)
    audio_env._write_config(path, b"autospawn = no\n")
    assert synced == ["file", "directory"]


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
