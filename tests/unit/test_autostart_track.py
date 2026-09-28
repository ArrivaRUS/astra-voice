"""T-165 (часть шага 1): запись автозапуска по треку установки (arch/appimage.md §3).

Прежние тесты автозапуска (test_autostart.py) не меняются: для .deb и исходников
запись байт в байт как в v0.1.
"""

from __future__ import annotations

from pathlib import Path

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
