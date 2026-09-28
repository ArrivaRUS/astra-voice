"""T-163: способ установки и пути трека AppImage (arch/appimage.md §1–§2)."""

from __future__ import annotations

from pathlib import Path

import pytest

from astra_voice.core import paths
from helpers.appimage_bundle import KEY, make_bundle, module_path

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.delenv(paths.APPIMAGE_DIR_ENV, raising=False)
    monkeypatch.delenv(paths.RESOURCES_ENV, raising=False)
    return home


def _run_from(monkeypatch: pytest.MonkeyPatch, bundle: Path) -> None:
    here = module_path(bundle)
    monkeypatch.setattr(paths, "_code_file", lambda: here)


def test_source_tree_is_source() -> None:
    assert paths.install_kind() is paths.InstallKind.SOURCE
    assert paths.bundle_root() is None
    assert not paths.InstallKind.SOURCE.is_appimage


def test_usr_lib_is_deb(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    here = paths.INSTALL_LIB_DIR / "astra_voice" / "core" / "paths.py"
    monkeypatch.setattr(paths, "_code_file", lambda: here)
    # Даже переменная бандла из окружения не делает пакет AppImage-копией.
    make_bundle(tmp_path / "bundle")
    monkeypatch.setenv(paths.APPIMAGE_DIR_ENV, str(tmp_path / "bundle"))
    assert paths.install_kind() is paths.InstallKind.DEB
    assert paths.resource_root() == paths.INSTALL_SHARE_DIR


def test_mount_is_portable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    bundle = make_bundle(tmp_path / ".mount_abc123")
    _run_from(monkeypatch, bundle)
    assert paths.bundle_root() == bundle
    assert paths.install_kind() is paths.InstallKind.APPIMAGE_PORTABLE
    assert paths.install_kind().is_appimage
    assert paths.resource_root() == bundle / "usr" / "share" / "astra-voice"


def test_installed_copy(monkeypatch: pytest.MonkeyPatch, isolated_home: Path) -> None:
    app = isolated_home / ".local" / "share" / "astra-voice" / "app"
    bundle = make_bundle(app / KEY)
    (bundle / paths.INSTALLED_MARKER).write_bytes(b"")
    _run_from(monkeypatch, bundle)
    monkeypatch.setenv(paths.APPIMAGE_DIR_ENV, str(bundle))
    assert paths.install_kind() is paths.InstallKind.APPIMAGE_INSTALLED


def test_copy_without_marker_is_portable(
    monkeypatch: pytest.MonkeyPatch, isolated_home: Path
) -> None:
    """Недокопированный каталог в app/ установленной копией не считается."""
    bundle = make_bundle(isolated_home / ".local" / "share" / "astra-voice" / "app" / KEY)
    _run_from(monkeypatch, bundle)
    assert paths.install_kind() is paths.InstallKind.APPIMAGE_PORTABLE


def test_installed_via_current_symlink(
    monkeypatch: pytest.MonkeyPatch, isolated_home: Path
) -> None:
    app = isolated_home / ".local" / "share" / "astra-voice" / "app"
    bundle = make_bundle(app / KEY)
    (bundle / paths.INSTALLED_MARKER).write_bytes(b"")
    (app / "current").symlink_to(KEY)
    # AppRun разрешает свой путь через readlink -f, модуль — через resolve().
    _run_from(monkeypatch, (app / "current").resolve())
    monkeypatch.setenv(paths.APPIMAGE_DIR_ENV, str(app / "current"))
    assert paths.bundle_root() == bundle
    assert paths.install_kind() is paths.InstallKind.APPIMAGE_INSTALLED


def test_xdg_data_home_moves_app_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    data = tmp_path / "data"
    monkeypatch.setenv("XDG_DATA_HOME", str(data))
    assert paths.appimage_app_dir() == data / "astra-voice" / "app"
    assert paths.appimage_current_link() == data / "astra-voice" / "app" / "current"
    assert paths.appimage_previous_link() == data / "astra-voice" / "app" / "previous"
    assert paths.appimage_current_apprun() == data / "astra-voice" / "app" / "current" / "AppRun"
    # Вычисление путей ничего не создаёт.
    assert not data.exists()


def test_env_without_marker_ignored(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Переменная бандла без маркера или вне расположения кода — не бандл."""
    fake = tmp_path / "fake"
    fake.mkdir()
    monkeypatch.setenv(paths.APPIMAGE_DIR_ENV, str(fake))
    assert paths.bundle_root() is None
    make_bundle(fake)
    # Маркер есть, но код работает не из этого каталога.
    assert paths.bundle_root() is None
    assert paths.install_kind() is paths.InstallKind.SOURCE


def test_resource_env_still_wins(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    bundle = make_bundle(tmp_path / ".mount_x")
    _run_from(monkeypatch, bundle)
    monkeypatch.setenv(paths.RESOURCES_ENV, str(tmp_path / "res"))
    assert paths.resource_root() == tmp_path / "res"


def test_check_launcher_is_passthrough_until_t1() -> None:
    """Точка расширения MJ-1: до 01.10 путь возвращается как есть."""
    target = paths.appimage_current_apprun()
    assert paths.check_appimage_launcher(target) == target


def test_tmp_copy_is_not_installed(monkeypatch: pytest.MonkeyPatch, isolated_home: Path) -> None:
    """P3-4: временная копия при смоуке (app/.tmp-*) — не установленная копия."""
    app = isolated_home / ".local" / "share" / "astra-voice" / "app"
    bundle = make_bundle(app / f".tmp-{KEY}.abc123")
    (bundle / paths.INSTALLED_MARKER).write_bytes(b"")
    _run_from(monkeypatch, bundle)
    assert paths.install_kind() is paths.InstallKind.APPIMAGE_PORTABLE


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (" /data", None),  # не абсолютный как есть → по умолчанию, как `case /*` в AppRun
        ("/data ", "/data "),  # хвостовой пробел — часть пути, как в AppRun
        ("", None),
        ("relative", None),
    ],
)
def test_xdg_value_taken_as_is(
    monkeypatch: pytest.MonkeyPatch, isolated_home: Path, value: str, expected: str | None
) -> None:
    """P3-3: XDG_DATA_HOME без strip — та же семантика, что в AppRun."""
    monkeypatch.setenv("XDG_DATA_HOME", value)
    base = Path(expected) if expected else isolated_home / ".local" / "share"
    assert paths.appimage_app_dir() == base / "astra-voice" / "app"
