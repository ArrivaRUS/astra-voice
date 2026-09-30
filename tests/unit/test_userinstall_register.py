"""Меню, значки и снятие AppImage-регистрации (arch/appimage.md §2–4, T-166)."""

from __future__ import annotations

import os
import shutil
import stat
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import Mock

import pytest

from astra_voice.core import paths
from astra_voice.platform import autostart, userinstall
from helpers.appimage_bundle import KEY, VERSION, make_bundle, module_path, tree_snapshot

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for name in ("DATA", "CONFIG", "STATE", "CACHE"):
        monkeypatch.setenv(f"XDG_{name}_HOME", str(home / name.lower()))
    monkeypatch.setenv("XDG_CONFIG_DIRS", str(home / "system-config"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(home / "run"))
    monkeypatch.setattr(paths, "FALLBACK_TMP_DIR", tmp_path)
    for name in (paths.APPIMAGE_DIR_ENV, paths.PORTABLE_ENV, paths.RESOURCES_ENV):
        monkeypatch.delenv(name, raising=False)
    return home


def installed(monkeypatch: pytest.MonkeyPatch) -> Path:
    copy = make_bundle(paths.appimage_app_dir() / KEY, icons=True)
    (copy / paths.INSTALLED_MARKER).touch()
    (copy.parent / "current").symlink_to(KEY)
    monkeypatch.setattr(paths, "_code_file", lambda: module_path(copy))
    return copy


def menu_path(home: Path) -> Path:
    return home / "data/applications/astra-voice.desktop"


def put(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def mtimes(root: Path) -> dict[str, int]:
    return {str(p.relative_to(root)): p.lstat().st_mtime_ns for p in root.rglob("*")}


def test_menu_template_matches_shipped_entry() -> None:
    shipped = (Path(__file__).resolve().parents[2] / "data/astra-voice.desktop").read_bytes()
    generated = userinstall.menu_entry_bytes(Path("/installed/AppRun"), VERSION)
    excluded = (b"Exec=", b"TryExec=", b"X-AstraVoice-Managed=", b"X-AppImage-Version=")
    assert [line for line in generated.splitlines() if not line.startswith(excluded)] == [
        line for line in shipped.splitlines() if not line.startswith(excluded)
    ]
    assert b"Exec=/installed/AppRun\nTryExec=/installed/AppRun\n" in generated


@pytest.mark.parametrize("suffix", ["data", "data space", 'data % $"'])
def test_register_writes_current_menu_icons_and_is_idempotent(
    home: Path, monkeypatch: pytest.MonkeyPatch, suffix: str
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(home / suffix))
    copy = installed(monkeypatch)
    monkeypatch.setenv("APPIMAGE", "/tmp/never-use-this.AppImage")
    refuse = Mock()
    check = Mock(side_effect=lambda launcher: launcher)
    monkeypatch.setattr(userinstall, "refuse_root", refuse)
    monkeypatch.setattr(paths, "check_appimage_launcher", check)
    result = userinstall.register()
    refuse.assert_called_once_with()
    launcher = paths.appimage_current_apprun()
    check.assert_called_with(launcher)
    menu = home / suffix / "applications/astra-voice.desktop"
    data = menu.read_bytes()
    assert data == userinstall.menu_entry_bytes(launcher, VERSION)
    assert f"Exec={autostart._exec_argument(str(launcher))}\n".encode() in data
    assert (b"TryExec=" in data) == (suffix == "data")
    assert b"X-AppImage-Version=0.2.0\n" in data
    assert b"never-use-this" not in data
    assert result.menu == "written"
    assert len(result.icons_written) == 2
    for icon in result.icons_written:
        assert (
            icon.read_bytes() == (copy / "usr/share" / icon.relative_to(home / suffix)).read_bytes()
        )
        assert stat.S_IMODE(icon.parent.stat().st_mode) == 0o755
        assert stat.S_IMODE(icon.parent.parent.stat().st_mode) == 0o755
    assert not (home / suffix / "icons/hicolor/22x22").exists()
    before, times = tree_snapshot(home), mtimes(home)
    again = userinstall.register()
    assert again.menu == "unchanged"
    assert again.icons_written == ()
    assert again.icons_skipped == result.icons_written
    assert not again.autostart
    assert tree_snapshot(home) == before
    assert mtimes(home) == times
    assert userinstall.status().menu == "ours"
    assert userinstall.status().icons


@pytest.mark.parametrize(
    "bad", ["absent", "directory", "dangling", "outside", "key-link", "no-marker"]
)
def test_register_refuses_invalid_current_without_writes(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad: str
) -> None:
    app = paths.appimage_app_dir()
    if bad != "absent":
        copy = installed(monkeypatch)
        current = app / "current"
        if bad == "no-marker":
            (copy / paths.INSTALLED_MARKER).unlink()
        elif bad == "key-link":
            outside = tmp_path / "outside"
            copy.rename(outside)
            copy.symlink_to(outside)
        else:
            current.unlink()
            if bad == "directory":
                current.mkdir()
            else:
                current.symlink_to("0.3.0-aaaaaaaaaaaa" if bad == "dangling" else tmp_path)
    before = tree_snapshot(tmp_path)
    check = Mock()
    refuse = Mock()
    monkeypatch.setattr(userinstall, "refuse_root", refuse)
    monkeypatch.setattr(paths, "check_appimage_launcher", check)
    with pytest.raises(
        userinstall.UserInstallError, match="Программа ещё не установлена в домашнюю папку."
    ):
        userinstall.register()
    refuse.assert_called_once_with()
    check.assert_not_called()
    assert tree_snapshot(tmp_path) == before


@pytest.mark.parametrize("kind", ["foreign", "symlink", "directory"])
def test_register_preserves_foreign_menu(
    home: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, kind: str
) -> None:
    installed(monkeypatch)
    menu = menu_path(home)
    menu.parent.mkdir(parents=True)
    if kind == "foreign":
        menu.write_bytes(b'[Desktop Entry]\nExec=sh -c "sleep 8; custom"\n')
    elif kind == "symlink":
        menu.symlink_to(
            put(home / "original.desktop", userinstall.menu_entry_bytes(Path("/old"), "old"))
        )
    else:
        menu.mkdir()
    before = tree_snapshot(menu.parent)
    assert userinstall.register().menu == "foreign"
    assert tree_snapshot(menu.parent) == before
    assert userinstall.status().menu == "foreign"
    assert "Чужая запись меню сохранена" in caplog.text
    assert str(home) not in caplog.text


def test_register_replaces_our_old_menu(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    installed(monkeypatch)
    put(menu_path(home), userinstall.menu_entry_bytes(Path("/old"), "old"))
    assert userinstall.register().menu == "written"
    assert menu_path(home).read_bytes() == userinstall.menu_entry_bytes(
        paths.appimage_current_apprun(), VERSION
    )


@pytest.mark.parametrize("ours", [True, False])
def test_register_retargets_only_our_autostart_t166(
    home: Path, monkeypatch: pytest.MonkeyPatch, ours: bool
) -> None:
    installed(monkeypatch)
    data = (
        autostart.entry_bytes(autostart.DEB_EXECUTABLE)
        if ours
        else b'[Desktop Entry]\nExec=sh -c "sleep 8; custom"\n'
    )
    startup = put(home / "config/autostart/astra-voice.desktop", data)
    assert userinstall.register().autostart is ours
    assert startup.read_bytes() == (
        autostart.entry_bytes(str(paths.appimage_current_apprun())) if ours else data
    )
    before = mtimes(home)
    assert not userinstall.register().autostart
    assert mtimes(home) == before


@pytest.mark.parametrize(
    "component", ["usr", "share", "icons", "hicolor", "48x48", "apps", "astravoice.png"]
)
def test_icon_sources_never_follow_symlinks(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, component: str
) -> None:
    copy = installed(monkeypatch)
    parts = ["usr", "share", "icons", "hicolor", "48x48", "apps", "astravoice.png"]
    source = copy.joinpath(*parts[: parts.index(component) + 1])
    outside = tmp_path / "outside"
    source.rename(outside)
    source.symlink_to(outside)
    before = tree_snapshot(outside) if outside.is_dir() else outside.read_bytes()
    userinstall.register()
    assert not (home / "data/icons/hicolor/48x48/apps/astravoice.png").exists()
    assert (tree_snapshot(outside) if outside.is_dir() else outside.read_bytes()) == before


@pytest.mark.parametrize("deb", ["executable", "missing", "not-executable"])
def test_unregister_removes_ours_and_restores_deb(
    home: Path, monkeypatch: pytest.MonkeyPatch, deb: str
) -> None:
    installed(monkeypatch)
    startup = put(
        home / "config/autostart/astra-voice.desktop",
        autostart.entry_bytes(autostart.DEB_EXECUTABLE),
    )
    userinstall.register()
    icons = home / "data/icons/hicolor"
    foreign = put(icons / "48x48/apps/other.png", b"other")
    tray = put(icons / "22x22/status/astravoice-tray-idle.svg", b"tray")
    directory = icons / "64x64/apps/astravoice.png"
    directory.mkdir(parents=True)
    target = put(home / "target", b"target")
    link = icons / "48x48/apps/astravoice.svg"
    link.symlink_to(target)
    executable = home / "fake-deb"
    if deb != "missing":
        executable.write_bytes(b"never run")
        executable.chmod(0o755 if deb == "executable" else 0o644)
    result = userinstall.unregister(deb_executable=executable)
    assert result.menu and result.autostart
    assert len(result.icons) == 3
    assert not menu_path(home).exists()
    assert directory.is_dir() and directory.parent.is_dir()
    assert foreign.read_bytes() == b"other"
    assert tray.read_bytes() == b"tray"
    assert target.read_bytes() == b"target"
    assert not link.is_symlink()
    if deb == "executable":
        assert startup.read_bytes() == autostart.entry_bytes(autostart.DEB_EXECUTABLE)
    else:
        assert not startup.exists()
    before, times = tree_snapshot(home), mtimes(home)
    assert userinstall.unregister(deb_executable=executable) == userinstall.UnregisterResult(
        False, (), False
    )
    assert tree_snapshot(home) == before
    assert mtimes(home) == times


@pytest.mark.parametrize("kind", ["foreign", "symlink", "directory", "absent"])
def test_unregister_preserves_foreign_menu_and_autostart(home: Path, kind: str) -> None:
    for path in (menu_path(home), home / "config/autostart/astra-voice.desktop"):
        path.parent.mkdir(parents=True, exist_ok=True)
        if kind == "foreign":
            path.write_bytes(b"[Desktop Entry]\nExec=foreign\n")
        elif kind == "symlink":
            path.symlink_to(
                put(home / "ours.desktop", autostart.entry_bytes(autostart.DEB_EXECUTABLE))
            )
        elif kind == "directory":
            path.mkdir()
    before = tree_snapshot(home)
    userinstall.unregister(deb_executable=home / "missing")
    assert tree_snapshot(home) == before


def test_remove_program_keeps_all_user_data_and_foreign_entries(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    copy = installed(monkeypatch)
    second_key = "0.3.0-aaaaaaaaaaaa"
    second = make_bundle(copy.parent / second_key)
    (copy.parent / "previous").symlink_to(second_key)
    put(copy.parent / ".tmp-abandoned/data", b"temporary")
    put(copy.parent / ".tmp-file", b"temporary")
    (copy.parent / ".tmp-link").symlink_to(tmp_path)
    notes = put(copy.parent / "notes.txt", b"notes")
    outside = tmp_path / "outside"
    put(outside / "precious", b"keep")
    alien = copy.parent / "0.4.0-bbbbbbbbbbbb"
    alien.symlink_to(outside)
    for path in (
        "data/astra-voice/models/model",
        "data/astra-voice/logs/log",
        "data/astra-voice/measurements.json",
        "config/astra-voice/settings.json",
        "state/astra-voice/state",
        "cache/astra-voice/cache",
    ):
        put(home / path, path.encode())
    userinstall.write_running_key(KEY)
    userinstall.register()
    protected = [home / name for name in ("config", "state", "cache", "run")]
    protected += [home / "data/astra-voice" / name for name in ("models", "logs")]
    snapshots = {path: tree_snapshot(path) for path in protected}
    measurements = (home / "data/astra-voice/measurements.json").read_bytes()
    outside_before = tree_snapshot(outside)
    before = userinstall.status()
    assert (before.current, before.previous, before.running) == (KEY, second_key, KEY)
    assert before.installed == (KEY, second_key)
    result = userinstall.remove_program(keep=KEY)
    assert result.kept == (KEY,)
    assert second_key in result.removed
    assert copy.is_dir() and not second.exists()
    assert not (copy.parent / "current").is_symlink()
    assert not (copy.parent / "previous").is_symlink()
    assert not list(copy.parent.glob(".tmp-*"))
    assert notes.read_bytes() == b"notes"
    assert alien.is_symlink() and os.readlink(alien) == str(outside)
    assert tree_snapshot(outside) == outside_before
    assert {path: tree_snapshot(path) for path in protected} == snapshots
    assert (home / "data/astra-voice/measurements.json").read_bytes() == measurements
    after = userinstall.status()
    assert after.installed == (KEY,)
    assert after.current is after.previous is None
    assert after.running == KEY
    assert after.menu == "none" and not after.icons


def test_status_and_remove_absent_install_create_nothing(home: Path) -> None:
    before = tree_snapshot(home)
    assert userinstall.status() == userinstall.InstallStatus(
        None, None, None, (), "none", False, "none"
    )
    assert tree_snapshot(home) == before
    assert userinstall.remove_program().removed == ()
    assert tree_snapshot(home) == before


def test_status_autostart_read_error_is_none(
    home: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(
        autostart, "state", Mock(side_effect=autostart.AutostartError(str(home / "bad")))
    )
    assert userinstall.status().autostart == "none"
    assert "~/bad" in caplog.text and str(home) not in caplog.text


@pytest.mark.parametrize("guard", ["root", "launcher"])
def test_register_guards_run_before_any_write(
    home: Path, monkeypatch: pytest.MonkeyPatch, guard: str
) -> None:
    installed(monkeypatch)
    before = tree_snapshot(home)
    order: list[str] = []

    def refuse() -> None:
        order.append("root")
        if guard == "root":
            raise userinstall.RootRefusedError("отказ")

    def launcher(path: Path) -> Path:
        order.append("launcher")
        assert path == paths.appimage_current_apprun()
        raise paths.PathError("отказ")

    monkeypatch.setattr(userinstall, "refuse_root", refuse)
    monkeypatch.setattr(paths, "check_appimage_launcher", launcher)
    with pytest.raises((userinstall.RootRefusedError, paths.PathError)):
        userinstall.register()
    assert order == (["root"] if guard == "root" else ["root", "launcher"])
    assert tree_snapshot(home) == before


def test_remove_program_uses_install_lock_and_never_follows_copy_links(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from contextlib import contextmanager

    copy = installed(monkeypatch)
    outside = tmp_path / "outside"
    put(outside / "precious", b"keep")
    (copy / "external").symlink_to(outside)
    before = tree_snapshot(outside)
    original_lock = userinstall._install_lock
    original_remove = shutil.rmtree
    locked = False

    @contextmanager
    def lock(app: Path, timeout: float) -> Iterator[None]:
        nonlocal locked
        assert app == copy.parent
        with original_lock(app, timeout):
            locked = True
            yield
            locked = False

    def remove(path: Path) -> None:
        assert locked
        original_remove(path)

    monkeypatch.setattr(userinstall, "_install_lock", lock)
    monkeypatch.setattr(shutil, "rmtree", remove)
    result = userinstall.remove_program()
    assert result.kept == () and KEY in result.removed
    assert not copy.exists()
    assert tree_snapshot(outside) == before


def _two_copies(monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    copy = installed(monkeypatch)
    second = make_bundle(copy.parent / "0.3.0-aaaaaaaaaaaa")
    (copy.parent / "previous").symlink_to(second.name)
    return copy, second


def test_remove_program_without_keep_spares_running_copy(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Р5: защита работающей копии не зависит от того, передал ли вызывающий keep."""
    copy, second = _two_copies(monkeypatch)
    userinstall.write_running_key(second.name)
    result = userinstall.remove_program()
    assert result.kept == (second.name,)
    assert KEY in result.removed and second.name not in result.removed
    assert second.is_dir() and not copy.exists()


def test_remove_program_keep_adds_to_running_copy(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    copy, second = _two_copies(monkeypatch)
    third = make_bundle(copy.parent / "0.4.0-bbbbbbbbbbbb")
    userinstall.write_running_key(second.name)
    result = userinstall.remove_program(keep=KEY)
    assert result.kept == (KEY, second.name)
    assert third.name in result.removed
    assert copy.is_dir() and second.is_dir() and not third.exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="root пишет и в каталог 0o500")
def test_running_key_read_follows_write_fallback(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Чтение выбирает тот же каталог, что запись, даже если XDG-каталог создать нельзя."""
    readonly = tmp_path / "readonly"
    readonly.mkdir(mode=0o500)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(readonly / "run"))
    try:
        assert userinstall.read_running_key() is None
        userinstall.write_running_key(KEY)
        fallback = tmp_path / f"astra-voice-{os.getuid()}"
        assert paths.runtime_dir() == fallback
        assert userinstall.running_key_path() == fallback / "running-key"
        assert userinstall.read_running_key() == KEY
        assert not (readonly / "run").exists()
    finally:
        readonly.chmod(0o700)


def test_status_reads_existing_runtime_without_chmod(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installed(monkeypatch)
    marker = put(home / "run/astra-voice/running-key", (KEY + "\n").encode())
    marker.parent.chmod(0o755)
    before, times = tree_snapshot(home), mtimes(home)
    assert userinstall.status().running == KEY
    assert tree_snapshot(home) == before
    assert mtimes(home) == times


@pytest.mark.parametrize("dangling", [False, True])
def test_autostart_symlink_does_not_stop_register_unregister_remove(
    home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    dangling: bool,
) -> None:
    """Ревью P3: ссылка вместо записи автозапуска — предупреждение, остальное доделывается."""
    installed(monkeypatch)
    target = tmp_path / "linked.desktop"
    if not dangling:
        target.write_bytes(autostart.entry_bytes(autostart.DEB_EXECUTABLE))
    startup = home / "config/autostart/astra-voice.desktop"
    startup.parent.mkdir(parents=True)
    startup.symlink_to(target)
    deb = put(home / "fake-deb", b"never run")
    deb.chmod(0o755)

    result = userinstall.register()
    assert result.menu == "written" and len(result.icons_written) == 2
    assert result.autostart is False
    assert "симлинк" in caplog.text and str(home) not in caplog.text

    unregistered = userinstall.unregister(deb_executable=deb)
    assert unregistered.menu and len(unregistered.icons) == 2
    assert unregistered.autostart is False

    userinstall.register()
    monkeypatch.setattr(paths, "SYSTEM_EXECUTABLE", deb)
    removed = userinstall.remove_program()
    assert removed.unregistered.menu and KEY in removed.removed
    assert not (paths.appimage_app_dir() / KEY).exists()
    assert startup.is_symlink()
    assert (
        not target.exists()
        if dangling
        else target.read_bytes() == autostart.entry_bytes(autostart.DEB_EXECUTABLE)
    )


def test_icons_are_best_effort_and_autostart_still_retargeted(
    home: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Ревью P3: сбой значка — предупреждение; меню и автозапуск делаются независимо."""
    installed(monkeypatch)
    startup = put(
        home / "config/autostart/astra-voice.desktop",
        autostart.entry_bytes(autostart.DEB_EXECUTABLE),
    )
    put(home / "data/icons/hicolor/48x48/apps", b"not a directory")
    result = userinstall.register()
    assert result.menu == "written"
    assert [icon.name for icon in result.icons_written] == ["astravoice.svg"]
    assert result.autostart is True
    assert startup.read_bytes() == autostart.entry_bytes(str(paths.appimage_current_apprun()))
    assert "Значок astravoice.png не добавлен" in caplog.text
    assert str(home) not in caplog.text


def test_icons_walk_failure_does_not_stop_register(
    home: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    installed(monkeypatch)
    startup = put(
        home / "config/autostart/astra-voice.desktop",
        autostart.entry_bytes(autostart.DEB_EXECUTABLE),
    )

    def broken(_root: Path, *, links: bool = False) -> Iterator[Path]:
        raise PermissionError("нет доступа")
        yield _root  # pragma: no cover — генератор

    monkeypatch.setattr(userinstall, "_icon_files", broken)
    result = userinstall.register()
    assert result.icons_written == () and result.autostart is True
    assert startup.read_bytes() == autostart.entry_bytes(str(paths.appimage_current_apprun()))
    assert "Значки программы не добавлены" in caplog.text


@pytest.mark.parametrize("xdg", [None, "", "relative/run"])
def test_running_key_read_without_xdg_checks_session_dir(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, xdg: str | None
) -> None:
    """Ревью P3: без XDG_RUNTIME_DIR чтение смотрит и /run/user/<uid>/astra-voice."""
    root = tmp_path / "run-user"
    monkeypatch.setattr(paths, "USER_RUNTIME_ROOT", root)
    session = root / str(os.getuid()) / "astra-voice"
    session.mkdir(parents=True, mode=0o700)
    put(session / "running-key", (KEY + "\n").encode())
    if xdg is None:
        monkeypatch.delenv("XDG_RUNTIME_DIR")
    else:
        monkeypatch.setenv("XDG_RUNTIME_DIR", xdg)
    assert userinstall.running_key_path() == session / "running-key"
    assert userinstall.read_running_key() == KEY
    # Запись по-прежнему — в запасной каталог: /run/user без XDG не создаём и не трогаем.
    assert paths.runtime_dir() == tmp_path / f"astra-voice-{os.getuid()}"


def test_running_key_session_dir_ignored_with_xdg(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "run-user"
    monkeypatch.setattr(paths, "USER_RUNTIME_ROOT", root)
    session = root / str(os.getuid()) / "astra-voice"
    session.mkdir(parents=True, mode=0o700)
    put(session / "running-key", (KEY + "\n").encode())
    assert userinstall.read_running_key() is None


def test_running_key_found_in_fallback_when_session_dir_has_none(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ревью P3: /run/user/<uid>/astra-voice есть, но без отметки — ищем дальше, в /tmp."""
    root = tmp_path / "run-user"
    monkeypatch.setattr(paths, "USER_RUNTIME_ROOT", root)
    (root / str(os.getuid()) / "astra-voice").mkdir(parents=True, mode=0o700)
    monkeypatch.delenv("XDG_RUNTIME_DIR")
    userinstall.write_running_key(KEY)  # копия без XDG_RUNTIME_DIR пишет в запасной каталог
    fallback = tmp_path / f"astra-voice-{os.getuid()}" / "running-key"
    assert fallback.is_file()
    assert userinstall.running_key_path() == fallback
    assert userinstall.read_running_key() == KEY
