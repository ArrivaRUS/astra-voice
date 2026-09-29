"""T-165 (часть шага 1): запись автозапуска по треку установки (arch/appimage.md §3).

Прежние тесты автозапуска (test_autostart.py) не меняются: для .deb и исходников
запись байт в байт как в v0.1.
"""

from __future__ import annotations

import os
import stat
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import Mock

import pytest

from astra_voice.core import paths
from astra_voice.platform import autostart

pytestmark = pytest.mark.unit

DEB_ENTRY = (
    b"[Desktop Entry]\nType=Application\nName=Astra Voice\n"
    b"Exec=/usr/bin/astra-voice --hidden\nIcon=astravoice\nNoDisplay=true\n"
    b"X-KDE-autostart-after=panel\n"
    b"X-AstraVoice-Managed=true\n"
)


@pytest.fixture
def user_entry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CONFIG_DIRS", str(tmp_path / "system"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    return tmp_path / "config" / "autostart" / "astra-voice.desktop"


def _track(monkeypatch: pytest.MonkeyPatch, kind: paths.InstallKind) -> None:
    monkeypatch.setattr(paths, "install_kind", lambda: kind)


@pytest.mark.parametrize("kind", [paths.InstallKind.DEB, paths.InstallKind.SOURCE])
def test_deb_and_source_unchanged(
    monkeypatch: pytest.MonkeyPatch, user_entry: Path, kind: paths.InstallKind
) -> None:
    _track(monkeypatch, kind)
    assert autostart.executable() == "/usr/bin/astra-voice"
    assert autostart.entry_bytes() == DEB_ENTRY
    assert autostart._ENTRY == DEB_ENTRY
    autostart.set_enabled(True)
    assert user_entry.read_bytes() == DEB_ENTRY


def test_installed_appimage_points_to_current(
    monkeypatch: pytest.MonkeyPatch, user_entry: Path, tmp_path: Path
) -> None:
    _track(monkeypatch, paths.InstallKind.APPIMAGE_INSTALLED)
    apprun = tmp_path / "data" / "astra-voice" / "app" / "current" / "AppRun"
    assert autostart.executable() == str(apprun)
    autostart.set_enabled(True)
    assert user_entry.read_bytes() == (
        b"[Desktop Entry]\nType=Application\nName=Astra Voice\n"
        + f"Exec={apprun} --hidden\nTryExec={apprun}\n".encode()
        + b"Icon=astravoice\nNoDisplay=true\nX-KDE-autostart-after=panel\n"
        b"X-AstraVoice-Managed=true\n"
    )
    assert autostart.state() == autostart.AutostartState(True, "ours", False, False)
    autostart.set_enabled(False)
    assert not user_entry.exists()


def test_portable_appimage_writes_nothing(
    monkeypatch: pytest.MonkeyPatch, user_entry: Path
) -> None:
    """Без установки запись на $APPIMAGE/монтирование не пишется (MJ-1, §3)."""
    _track(monkeypatch, paths.InstallKind.APPIMAGE_PORTABLE)
    with pytest.raises(autostart.AutostartUnavailableError, match="после установки"):
        autostart.set_enabled(True)
    assert not user_entry.exists()
    assert not user_entry.parent.exists()


def test_portable_can_still_remove_own_entry(
    monkeypatch: pytest.MonkeyPatch, user_entry: Path
) -> None:
    """Выключить уже записанный автозапуск можно и без установки: запись удаляется."""
    user_entry.parent.mkdir(parents=True)
    user_entry.write_bytes(DEB_ENTRY)
    _track(monkeypatch, paths.InstallKind.APPIMAGE_PORTABLE)
    autostart.set_enabled(False)
    assert not user_entry.exists()


@pytest.mark.parametrize(
    ("path", "exec_value", "try_exec"),
    [
        ("/home/u/.local/share/astra-voice/app/current/AppRun", None, True),
        ("/home/Иван Петров/app/current/AppRun", '"/home/Иван Петров/app/current/AppRun"', False),
        ("/home/u/100%/AppRun", "/home/u/100%%/AppRun", False),
        ("/home/a b%/AppRun", '"/home/a b%%/AppRun"', False),
        ('/home/a"b/AppRun', '"/home/a\\\\"b/AppRun"', False),
        ("/home/a$b/AppRun", '"/home/a\\\\$b/AppRun"', False),
        ("/home/a\\b/AppRun", '"/home/a\\\\\\\\b/AppRun"', False),
    ],
)
def test_exec_escaping(path: str, exec_value: str | None, try_exec: bool) -> None:
    """MN-2: кавычки по спецификации Desktop Entry, «%» → «%%»."""
    data = autostart.entry_bytes(path).decode()
    assert f"Exec={exec_value or path} --hidden\n" in data
    assert (f"TryExec={path}\n" in data) is try_exec


@pytest.mark.parametrize("bad", ["/home/u/a\nb/AppRun", "/home/u/\x1b/AppRun", "/home/\x7f/x"])
def test_control_characters_refused(bad: str) -> None:
    with pytest.raises(autostart.AutostartError, match="управляющие"):
        autostart.entry_bytes(bad)


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Все домашние каталоги, включая запасные пути, остаются внутри tmp_path."""
    for variable, directory in (
        ("HOME", "home"),
        ("XDG_CONFIG_HOME", "config"),
        ("XDG_CONFIG_DIRS", "system"),
        ("XDG_DATA_HOME", "data"),
        ("XDG_RUNTIME_DIR", "runtime"),
    ):
        monkeypatch.setenv(variable, str(tmp_path / directory))


@pytest.mark.parametrize(
    ("kind", "entry", "expected"),
    [
        (paths.InstallKind.DEB, "deb", "ours-this"),
        (paths.InstallKind.SOURCE, "deb", "ours-this"),
        (paths.InstallKind.APPIMAGE_INSTALLED, "appimage", "ours-this"),
        (paths.InstallKind.DEB, "appimage", "ours-other"),
        (paths.InstallKind.APPIMAGE_INSTALLED, "deb", "ours-other"),
        (paths.InstallKind.APPIMAGE_PORTABLE, "appimage", "ours-other"),
        (paths.InstallKind.DEB, "foreign", "foreign"),
        (paths.InstallKind.DEB, "system", "system"),
        (paths.InstallKind.DEB, "none", "none"),
    ],
)
def test_target_by_track(
    user_entry: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: paths.InstallKind,
    entry: str,
    expected: str,
) -> None:
    """§§2–3: пользовательская запись имеет приоритет независимо от Hidden."""
    _track(monkeypatch, kind)
    system = tmp_path / "system" / "autostart" / "astra-voice.desktop"
    if entry != "none":
        system.parent.mkdir(parents=True)
        system.write_bytes(DEB_ENTRY + b"Hidden=true\n")
    if entry not in ("system", "none"):
        data = (
            autostart.entry_bytes(str(paths.appimage_current_apprun()))
            if entry == "appimage"
            else b'[Desktop Entry]\nExec=sh -c "sleep 8; astra-voice"\n'
            if entry == "foreign"
            else DEB_ENTRY
        )
        user_entry.parent.mkdir(parents=True)
        user_entry.write_bytes(data + b"Hidden=true\n")
    assert autostart.state().target == expected
    assert not autostart.state().enabled
    if entry in ("system", "none"):
        assert not user_entry.parent.exists()


def test_state_survives_strict_launcher_check(
    user_entry: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Строгая check_appimage_launcher (01.10) бросает PathError — state() не падает."""
    _track(monkeypatch, paths.InstallKind.APPIMAGE_INSTALLED)
    user_entry.parent.mkdir(parents=True)
    user_entry.write_bytes(autostart.entry_bytes(str(paths.appimage_current_apprun())))
    check = Mock(side_effect=paths.PathError("путь вне app/"))
    monkeypatch.setattr(paths, "check_appimage_launcher", check)
    result = autostart.state()
    check.assert_called_once_with(paths.appimage_current_apprun())
    assert result == autostart.AutostartState(True, "ours", False, False)
    assert result.target == "ours-other"
    assert "путь вне app/" in caplog.text


def test_target_is_read_only_and_ignored_in_comparisons() -> None:
    current = autostart.AutostartState(True, "ours", False, False, "ours-this")
    assert current == autostart.AutostartState(True, "ours", False, False)
    assert hash(current) == hash(autostart.AutostartState(True, "ours", False, False))
    with pytest.raises(FrozenInstanceError):
        current.target = "ours-other"  # type: ignore[misc]


@pytest.mark.parametrize(
    "directory", ["plain", "Иван Петров", "100%f", "a b%", 'a"b', "a$b", "a\\b", "a`b", "a'b"]
)
def test_target_decodes_desktop_exec(
    user_entry: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, directory: str
) -> None:
    """MN-2: два уровня escape и %% читаются обратно, аргументы не часть пути."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / directory))
    _track(monkeypatch, paths.InstallKind.APPIMAGE_INSTALLED)
    user_entry.parent.mkdir(parents=True)
    user_entry.write_bytes(autostart.entry_bytes())
    assert autostart.state().target == "ours-this"


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        (b'"/usr/bin/astra-voice" --hidden', "ours-this"),
        (b'/usr/bin/astra-voice --ignored="argument"', "ours-this"),
        (b"/usr/bin/astra-voice-other --hidden", "ours-other"),
        (b'"/usr/bin/astra-voice --hidden', "ours-other"),
        (b'"/usr/bin/astra-voice"suffix', "ours-other"),
        (b"'/usr/bin/astra-voice'", "ours-other"),
        (b"/usr/bin/astra-voice%f", "ours-other"),
        (b"\xff", "ours-other"),
        (b"", "ours-other"),
    ],
)
def test_target_uses_first_program(
    user_entry: Path, monkeypatch: pytest.MonkeyPatch, command: bytes, expected: str
) -> None:
    _track(monkeypatch, paths.InstallKind.DEB)
    user_entry.parent.mkdir(parents=True)
    user_entry.write_bytes(DEB_ENTRY.replace(b"/usr/bin/astra-voice --hidden", command))
    assert autostart.state().target == expected


@pytest.mark.parametrize("hidden", [b"", b"Hidden=true\n"])
def test_retarget_round_trip(user_entry: Path, hidden: bytes) -> None:
    """§3: .deb → AppImage → .deb без потери байтов, Hidden и прав 0600."""
    user_entry.parent.mkdir(parents=True)
    original = DEB_ENTRY + hidden
    user_entry.write_bytes(original)
    user_entry.chmod(0o600)
    before = user_entry.stat()
    assert autostart.retarget(autostart.DEB_EXECUTABLE) is False
    assert user_entry.stat().st_mtime_ns == before.st_mtime_ns
    assert user_entry.stat().st_ino == before.st_ino
    launcher = str(paths.appimage_current_apprun())
    assert autostart.retarget(launcher) is True
    assert user_entry.read_bytes() == autostart.entry_bytes(launcher) + hidden
    assert stat.S_IMODE(user_entry.stat().st_mode) == 0o600
    before = user_entry.stat()
    assert autostart.retarget(launcher) is False
    assert user_entry.stat().st_mtime_ns == before.st_mtime_ns
    assert user_entry.stat().st_ino == before.st_ino
    assert autostart.retarget(autostart.DEB_EXECUTABLE) is True
    assert user_entry.read_bytes() == original
    assert stat.S_IMODE(user_entry.stat().st_mode) == 0o600


@pytest.mark.parametrize("final_newline", [True, False])
def test_retarget_preserves_layout(user_entry: Path, final_newline: bool) -> None:
    original = (
        b"\xef\xbb\xbf[Desktop Entry]\r\n# Exec=comment\r\nName=Custom\r\n"
        b"Exec=old --argument\r\n# TryExec=comment\r\nHidden=true\r\n"
        b"X-AstraVoice-Managed=true\r\n[Other]\r\nExec=untouched\r\nTryExec=untouched\r\n"
    )
    if not final_newline:
        original = original.removesuffix(b"\r\n")
    user_entry.parent.mkdir(parents=True)
    user_entry.write_bytes(original)
    launcher = str(paths.appimage_current_apprun())
    assert autostart.retarget(launcher) is True
    assert user_entry.read_bytes() == original.replace(
        b"Exec=old --argument\r\n", f"Exec={launcher} --hidden\r\nTryExec={launcher}\r\n".encode()
    )
    assert autostart.retarget(autostart.DEB_EXECUTABLE) is True
    assert user_entry.read_bytes() == original.replace(
        b"Exec=old --argument", b"Exec=/usr/bin/astra-voice --hidden"
    )


def test_retarget_exec_at_end_without_newline(user_entry: Path) -> None:
    original = b"[Desktop Entry]\nX-AstraVoice-Managed=true\nExec=/usr/bin/astra-voice --hidden"
    user_entry.parent.mkdir(parents=True)
    user_entry.write_bytes(original)
    launcher = str(paths.appimage_current_apprun())
    assert autostart.retarget(launcher) is True
    assert user_entry.read_bytes().endswith(f"--hidden\nTryExec={launcher}".encode())
    assert autostart.retarget(autostart.DEB_EXECUTABLE) is True
    assert user_entry.read_bytes() == original


@pytest.mark.parametrize("directory", ["plain", "with space", "100%"])
def test_retarget_existing_tryexec(
    user_entry: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, directory: str
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / directory))
    original = DEB_ENTRY.replace(b"Name=Astra Voice\n", b"TryExec=old\nName=Astra Voice\n")
    user_entry.parent.mkdir(parents=True)
    user_entry.write_bytes(original)
    launcher = str(paths.appimage_current_apprun())
    assert autostart.retarget(launcher) is True
    expected_exec = f'"{launcher}"' if directory == "with space" else launcher.replace("%", "%%")
    assert user_entry.read_bytes() == original.replace(
        b"Exec=/usr/bin/astra-voice --hidden", f"Exec={expected_exec} --hidden".encode()
    ).replace(b"TryExec=old\n", f"TryExec={launcher}\n".encode() if directory == "plain" else b"")


def test_retarget_defaults_to_executable_and_checks_launcher(
    user_entry: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _track(monkeypatch, paths.InstallKind.APPIMAGE_INSTALLED)
    check = Mock(wraps=paths.check_appimage_launcher)
    monkeypatch.setattr(paths, "check_appimage_launcher", check)
    user_entry.parent.mkdir(parents=True)
    user_entry.write_bytes(DEB_ENTRY)
    assert autostart.retarget() is True
    check.assert_called_with(paths.appimage_current_apprun())
    assert user_entry.read_bytes() == autostart.entry_bytes()


def test_retarget_foreign_unchanged(user_entry: Path) -> None:
    original = b'[Desktop Entry]\nExec=sh -c "sleep 8; astra-voice"\n'
    user_entry.parent.mkdir(parents=True)
    user_entry.write_bytes(original)
    before = user_entry.stat()
    assert autostart.retarget(str(paths.appimage_current_apprun())) is False
    assert user_entry.read_bytes() == original
    assert user_entry.stat().st_mtime_ns == before.st_mtime_ns
    assert user_entry.stat().st_ino == before.st_ino


def test_retarget_absent_does_not_create(user_entry: Path) -> None:
    assert autostart.retarget(str(paths.appimage_current_apprun())) is False
    assert not user_entry.exists()
    assert not user_entry.parent.exists()


@pytest.mark.parametrize(
    "relative", ["Downloads/Astra.AppImage", ".mount_x/AppRun", "app/version/AppRun"]
)
def test_retarget_rejects_other_paths(user_entry: Path, tmp_path: Path, relative: str) -> None:
    user_entry.parent.mkdir(parents=True)
    user_entry.write_bytes(DEB_ENTRY)
    with pytest.raises(autostart.AutostartError):
        autostart.retarget(str(tmp_path / relative))
    assert user_entry.read_bytes() == DEB_ENTRY


@pytest.mark.parametrize("control", ["\n", "\t", "\x1b", "\x7f"])
def test_retarget_rejects_control_characters(
    user_entry: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, control: str
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / f"bad{control}path"))
    user_entry.parent.mkdir(parents=True)
    user_entry.write_bytes(DEB_ENTRY)
    with pytest.raises(autostart.AutostartError, match="управляющие"):
        autostart.retarget(str(paths.appimage_current_apprun()))
    assert user_entry.read_bytes() == DEB_ENTRY


@pytest.mark.parametrize("dangling", [False, True])
def test_retarget_skips_symlink(
    user_entry: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture, dangling: bool
) -> None:
    """Ревью P3: ссылку не трогаем и не падаем — предупреждение в журнал, False."""
    target = tmp_path / "target.desktop"
    if not dangling:
        target.write_bytes(DEB_ENTRY)
    user_entry.parent.mkdir(parents=True)
    user_entry.symlink_to(target)
    assert autostart.retarget(str(paths.appimage_current_apprun())) is False
    assert "симлинк" in caplog.text
    assert user_entry.is_symlink()
    assert not target.exists() if dangling else target.read_bytes() == DEB_ENTRY


def test_state_preserves_bytes_and_mtime(user_entry: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """У27/F10.1: обнаружение другого трека не перезаписывает запись при старте."""
    _track(monkeypatch, paths.InstallKind.APPIMAGE_INSTALLED)
    original = b"\xef\xbb\xbf" + (DEB_ENTRY + b"Hidden=true").replace(b"\n", b"\r\n")
    user_entry.parent.mkdir(parents=True)
    user_entry.write_bytes(original)
    os.utime(user_entry, ns=(1_000_000_000, 1_000_000_000))
    before = user_entry.stat()
    assert autostart.state().target == "ours-other"
    assert user_entry.read_bytes() == original
    assert user_entry.stat().st_mtime_ns == before.st_mtime_ns
    assert user_entry.stat().st_ino == before.st_ino
