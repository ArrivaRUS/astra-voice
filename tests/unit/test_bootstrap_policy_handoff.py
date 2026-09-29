"""Запрет appimage=deny не ломает системную версию (arch/appimage.md §5, ревью P2-1).

Установленная и зарегистрированная копия во временном HOME, политика deny, запуск:
есть «системная версия» (подменный лаунчер вместо /usr/bin/astra-voice) — exec туда
с чистым окружением; нет — код 3; ``--unregister`` запрет не блокирует.
Настоящий /usr/bin/astra-voice не запускается никогда: путь подменён, execve — Mock
(кроме одного теста, где exec идёт в подменный sh-скрипт в дочернем процессе).
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from astra_voice import bootstrap
from astra_voice.core import audio_env, paths, policy
from astra_voice.core.logging import LOG_FILE_NAME, _clear_own_handlers
from astra_voice.platform import autostart, userinstall
from helpers.appimage_bundle import KEY, make_bundle, module_path

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
DENY = "[astra-voice]\nappimage = deny\n"


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for name in ("DATA", "CONFIG", "STATE", "CACHE"):
        monkeypatch.setenv(f"XDG_{name}_HOME", str(home / name.lower()))
    monkeypatch.setenv("XDG_CONFIG_DIRS", str(home / "system-config"))
    runtime = home / "run"
    runtime.mkdir(mode=0o700)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    monkeypatch.setattr(paths, "FALLBACK_TMP_DIR", tmp_path)
    for name in (paths.APPIMAGE_DIR_ENV, paths.PORTABLE_ENV, paths.RESOURCES_ENV):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv(bootstrap.HANDOFF_ENV, raising=False)
    monkeypatch.setattr(policy, "POLICY_PATH", tmp_path / "policy.conf")
    yield home
    _clear_own_handlers(logging.getLogger())


def _registered(home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Установленная копия (current → KEY), меню, значки и наш автозапуск на неё."""
    copy = make_bundle(paths.appimage_app_dir() / KEY, icons=True)
    (copy / paths.INSTALLED_MARKER).touch()
    (copy.parent / "current").symlink_to(KEY)
    monkeypatch.setattr(paths, "_code_file", lambda: module_path(copy))
    assert paths.install_kind() is paths.InstallKind.APPIMAGE_INSTALLED
    startup = home / "config/autostart/astra-voice.desktop"
    startup.parent.mkdir(parents=True)
    startup.write_bytes(autostart.entry_bytes(str(paths.appimage_current_apprun())))
    userinstall.register()
    assert userinstall.status().menu == "ours"
    return copy


def _system(tmp_path: Path) -> Path:
    system = tmp_path / "deb" / "astra-voice"
    system.parent.mkdir()
    system.write_text("#!/bin/sh\nexit 0\n", encoding="ascii")
    system.chmod(0o755)
    return system


@pytest.fixture
def entries(monkeypatch: pytest.MonkeyPatch) -> dict[str, Mock]:
    """Точки входа и подготовка процессов подменены: без Qt, GUI и установки."""
    mocks = {name: Mock(return_value=17) for name in bootstrap.COMMANDS}
    monkeypatch.setitem(sys.modules, "astra_voice.app", SimpleNamespace(main=mocks["app"]))
    monkeypatch.setattr(userinstall, "selfinstall_main", mocks["selfinstall"])
    for name in ("_setup_sys_path", "_harden", "_setup_render_env"):
        monkeypatch.setattr(bootstrap, name, Mock())
    monkeypatch.setattr(audio_env, "deny_pulse_autospawn", Mock())
    for name in (*bootstrap.SOFTWARE_RENDER_ENV, "QT_QUICK_CONTROLS_STYLE"):
        monkeypatch.delenv(name, raising=False)
    return mocks


def test_denied_copy_execs_system_version_with_clean_env(
    home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    entries: dict[str, Mock],
) -> None:
    copy = _registered(home, monkeypatch)
    policy.POLICY_PATH.write_text(DENY, encoding="utf-8")
    system = _system(tmp_path)
    monkeypatch.setattr(paths, "SYSTEM_EXECUTABLE", system)
    execve = Mock()
    monkeypatch.setattr(os, "execve", execve)
    # Окружение, как его оставляет AppRun установленной копии (режим А).
    monkeypatch.setenv("APPIMAGE", "/home/u/Astra_Voice.AppImage")
    monkeypatch.setenv("ASTRA_VOICE_APPIMAGE_DIR", str(copy))
    monkeypatch.setenv("ASTRA_VOICE_RESOURCES", str(copy / "usr/share/astra-voice"))
    monkeypatch.setenv("SSL_CERT_FILE", str(copy / "cacert.pem"))
    monkeypatch.setenv("ASTRA_VOICE_ORIG_SSL_CERT_FILE", "/etc/corp/ca.pem")
    monkeypatch.setenv("ASTRA_VOICE_ORIG_UNSET", "PYTHONPATH")
    monkeypatch.setenv("PYTHONPATH", str(copy / "lib"))
    monkeypatch.setenv("LANG", "ru_RU.UTF-8")

    assert bootstrap.main(["app", "--hidden"]) == bootstrap.EXIT_POLICY_DENIED

    execve.assert_called_once()
    program, argv, env = execve.call_args.args
    assert program == str(system)
    assert argv == [str(system), "--hidden"]
    assert env[bootstrap.HANDOFF_ENV] == "1"
    assert [name for name in env if name.startswith(("ASTRA_VOICE_", "APPIMAGE"))] == [
        bootstrap.HANDOFF_ENV
    ]
    assert env["SSL_CERT_FILE"] == "/etc/corp/ca.pem"
    assert "PYTHONPATH" not in env and "TMPDIR" not in env
    assert env["LANG"] == "ru_RU.UTF-8" and env["HOME"] == str(home)
    for entry in entries.values():
        entry.assert_not_called()
    assert capsys.readouterr().err == ""
    journal = (home / "data/astra-voice/logs" / LOG_FILE_NAME).read_text(encoding="utf-8")
    assert journal.count(bootstrap.HANDOFF_MESSAGE) == 1
    # Регистрация не тронута: снять её — отдельное действие пользователя.
    assert userinstall.status().menu == "ours"


@pytest.mark.parametrize("command", [["app", "--hidden"], ["selfinstall", "/bundle", "--hidden"]])
def test_denied_without_system_version_exits_3(
    home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    entries: dict[str, Mock],
    command: list[str],
) -> None:
    _registered(home, monkeypatch)
    policy.POLICY_PATH.write_text(DENY, encoding="utf-8")
    missing = tmp_path / "no-deb" / "astra-voice"
    not_executable = tmp_path / "astra-voice"
    not_executable.write_text("#!/bin/sh\n", encoding="ascii")
    execve = Mock()
    monkeypatch.setattr(os, "execve", execve)
    for system in (missing, not_executable):
        monkeypatch.setattr(paths, "SYSTEM_EXECUTABLE", system)
        assert bootstrap.main(command) == 3
        assert capsys.readouterr().err == policy.APPIMAGE_DENIED_MESSAGE + "\n"
    execve.assert_not_called()
    for entry in entries.values():
        entry.assert_not_called()


def test_selfinstall_with_system_version_copies_nothing(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entries: dict[str, Mock]
) -> None:
    """selfinstall под запретом не копирует и отдаёт запуск команде app (код «не нужна»)."""
    monkeypatch.setattr(paths, "install_kind", lambda: paths.InstallKind.APPIMAGE_PORTABLE)
    policy.POLICY_PATH.write_text(DENY, encoding="utf-8")
    monkeypatch.setattr(paths, "SYSTEM_EXECUTABLE", _system(tmp_path))
    execve = Mock()
    monkeypatch.setattr(os, "execve", execve)
    assert bootstrap.main(["selfinstall", "/bundle", "--hidden"]) == userinstall.EXIT_SKIPPED
    execve.assert_not_called()
    entries["selfinstall"].assert_not_called()
    assert not paths.appimage_app_dir().exists()


def test_handoff_marker_prevents_loop(
    home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    entries: dict[str, Mock],
) -> None:
    """«Системная версия» сама оказалась AppImage под запретом — дальше не передаём."""
    monkeypatch.setattr(paths, "install_kind", lambda: paths.InstallKind.APPIMAGE_PORTABLE)
    policy.POLICY_PATH.write_text(DENY, encoding="utf-8")
    monkeypatch.setattr(paths, "SYSTEM_EXECUTABLE", _system(tmp_path))
    monkeypatch.setenv(bootstrap.HANDOFF_ENV, "1")
    execve = Mock()
    monkeypatch.setattr(os, "execve", execve)
    assert bootstrap.main(["app", "--hidden"]) == 3
    assert capsys.readouterr().err == policy.APPIMAGE_DENIED_MESSAGE + "\n"
    execve.assert_not_called()


@pytest.mark.parametrize("kind", [paths.InstallKind.DEB, paths.InstallKind.SOURCE])
def test_deb_track_never_hands_over(
    home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    entries: dict[str, Mock],
    kind: paths.InstallKind,
) -> None:
    """Защита от петли: exec только из трека AppImage."""
    monkeypatch.setattr(paths, "install_kind", lambda: kind)
    policy.POLICY_PATH.write_text(DENY, encoding="utf-8")
    monkeypatch.setattr(paths, "SYSTEM_EXECUTABLE", _system(tmp_path))
    execve = Mock()
    monkeypatch.setattr(os, "execve", execve)
    assert bootstrap.main(["app", "--hidden"]) == 17
    execve.assert_not_called()
    entries["app"].assert_called_once_with(["--hidden"])


@pytest.mark.parametrize("flag", sorted(bootstrap.UNREGISTER_FLAGS))
@pytest.mark.parametrize("command", ["app", "selfinstall"])
def test_unregister_flags_pass_denial(
    home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    entries: dict[str, Mock],
    flag: str,
    command: str,
) -> None:
    monkeypatch.setattr(paths, "install_kind", lambda: paths.InstallKind.APPIMAGE_INSTALLED)
    policy.POLICY_PATH.write_text(DENY, encoding="utf-8")
    monkeypatch.setattr(paths, "SYSTEM_EXECUTABLE", _system(tmp_path))
    execve = Mock()
    monkeypatch.setattr(os, "execve", execve)
    args = [command, flag] if command == "app" else [command, "/bundle", flag]
    assert bootstrap.main(args) == 17
    entries[command].assert_called_once_with(args[1:])
    execve.assert_not_called()


def _wrapper(tmp_path: Path, system: Path) -> Path:
    """bootstrap в дочернем процессе: путь политики, трек и «системная версия» внедрены в код."""
    wrapper = tmp_path / "boot.py"
    wrapper.write_text(
        "import sys\n"
        f"sys.path.insert(0, {str(REPO_ROOT / 'src')!r})\n"
        "from pathlib import Path\n"
        "from astra_voice import bootstrap\n"
        "from astra_voice.core import paths, policy\n"
        f"policy.POLICY_PATH = Path({str(policy.POLICY_PATH)!r})\n"
        f"paths.SYSTEM_EXECUTABLE = Path({str(system)!r})\n"
        "paths.install_kind = lambda: paths.InstallKind.APPIMAGE_INSTALLED\n"
        "raise SystemExit(bootstrap.main())\n",
        encoding="utf-8",
    )
    return wrapper


def _child_env(home: Path) -> dict[str, str]:
    names = ("HOME", "XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME")
    env = {name: os.environ[name] for name in names}
    env.update(
        PATH=os.environ.get("PATH", "/usr/bin:/bin"),
        XDG_CONFIG_DIRS=str(home / "system-config"),
        XDG_RUNTIME_DIR=str(home / "run"),
        LANG="C.UTF-8",
        QT_QPA_PLATFORM="offscreen",
        DBUS_SESSION_BUS_ADDRESS="unix:path=/nonexistent",
        PULSE_SERVER="unix:/nonexistent",
    )
    return env


def test_real_exec_reaches_system_version(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Сквозь настоящий execve: подменный лаунчер получает аргументы и чистое окружение."""
    policy.POLICY_PATH.write_text(DENY, encoding="utf-8")
    out = tmp_path / "system.out"
    system = tmp_path / "deb" / "astra-voice"
    system.parent.mkdir()
    system.write_text(
        f'#!/bin/sh\nprintf "%s\\n" "$@" > {out}\nenv >> {out}\nexit 7\n', encoding="ascii"
    )
    system.chmod(0o755)
    env = {
        **_child_env(home),
        "APPIMAGE": "/x.AppImage",
        "ASTRA_VOICE_APPIMAGE_DIR": "/x",
        "SSL_CERT_FILE": "/bundle/cacert.pem",
        "ASTRA_VOICE_ORIG_SSL_CERT_FILE": "/etc/corp/ca.pem",
    }
    proc = subprocess.run(
        [sys.executable, "-I", str(_wrapper(tmp_path, system)), "app", "--hidden", "a b"],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        stdin=subprocess.DEVNULL,
    )
    assert proc.returncode == 7, proc.stderr
    lines = out.read_text(encoding="utf-8").splitlines()
    assert lines[:2] == ["--hidden", "a b"]
    child = dict(line.split("=", 1) for line in lines[2:] if "=" in line)
    assert child["SSL_CERT_FILE"] == "/etc/corp/ca.pem"
    assert child[bootstrap.HANDOFF_ENV] == "1"
    assert not [
        name
        for name in child
        if name.startswith(("ASTRA_VOICE_", "APPIMAGE")) and name != bootstrap.HANDOFF_ENV
    ]


def test_unregister_under_denial_removes_entries(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--unregister под запретом проходит до app.main и снимает меню, значки и автозапуск."""
    _registered(home, monkeypatch)
    startup = home / "config/autostart/astra-voice.desktop"
    icons = home / "data/icons/hicolor"
    assert startup.exists() and list(icons.rglob("astravoice.*"))
    policy.POLICY_PATH.write_text(DENY, encoding="utf-8")
    system = tmp_path / "no-deb" / "astra-voice"
    proc = subprocess.run(
        [sys.executable, "-I", str(_wrapper(tmp_path, system)), "app", "--unregister"],
        env=_child_env(home),
        capture_output=True,
        text=True,
        timeout=120,
        stdin=subprocess.DEVNULL,
    )
    assert proc.returncode == 0, proc.stderr
    assert "убран из меню и автозапуска" in proc.stdout
    assert policy.APPIMAGE_DENIED_MESSAGE not in proc.stderr
    assert userinstall.status().menu == "none"
    assert not list(icons.rglob("astravoice.*"))
    assert not startup.exists()
    assert (paths.appimage_app_dir() / KEY).is_dir()  # программа на месте: только снятие


def test_appimage_internal_flags_not_passed_to_system_version(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entries: dict[str, Mock]
) -> None:
    """Повторный запуск .AppImage: AppRun добавляет --register, .deb его не знает (код 2)."""
    monkeypatch.setattr(paths, "install_kind", lambda: paths.InstallKind.APPIMAGE_INSTALLED)
    policy.POLICY_PATH.write_text(DENY, encoding="utf-8")
    system = _system(tmp_path)
    monkeypatch.setattr(paths, "SYSTEM_EXECUTABLE", system)
    execve = Mock()
    monkeypatch.setattr(os, "execve", execve)
    bootstrap.main(["app", "--register", "--hidden"])
    execve.assert_called_once()
    assert execve.call_args.args[1] == [str(system), "--hidden"]
