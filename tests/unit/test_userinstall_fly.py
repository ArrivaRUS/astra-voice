"""Меню Fly: локальные деревья, безопасные операции и обновление без живой оболочки."""

from __future__ import annotations

import errno
import logging
import os
import shutil
import stat
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock

import pytest

from astra_voice.core import childenv, paths
from astra_voice.platform import autostart, session, userinstall
from helpers.appimage_bundle import KEY, make_bundle, module_path, tree_snapshot

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for name in ("DATA", "CONFIG", "STATE", "CACHE"):
        monkeypatch.setenv(f"XDG_{name}_HOME", str(home / name.lower()))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(home / "run"))
    monkeypatch.setattr(paths, "FALLBACK_TMP_DIR", tmp_path)
    monkeypatch.delenv(paths.APPIMAGE_DIR_ENV, raising=False)
    monkeypatch.delenv(paths.PORTABLE_ENV, raising=False)
    copy = make_bundle(paths.appimage_app_dir() / KEY, icons=True)
    (copy / paths.INSTALLED_MARKER).touch()
    (copy.parent / "current").symlink_to(KEY)
    monkeypatch.setattr(paths, "_code_file", lambda: module_path(copy))
    return home


@pytest.fixture(autouse=True)
def refresh(monkeypatch: pytest.MonkeyPatch) -> Mock:
    monkeypatch.setattr(session, "detect", lambda: session.SessionKind.FLY)
    original_lstat = os.lstat
    original_access = os.access
    monkeypatch.setattr(
        os,
        "lstat",
        lambda path, **kwargs: (
            os.stat_result((stat.S_IFREG | 0o755, 0, 0, 1, 0, 0, 0, 0, 0, 0))
            if path == "/usr/bin/fly-wmfunc"
            else original_lstat(path, **kwargs)
        ),
    )
    monkeypatch.setattr(
        os,
        "access",
        lambda path, mode: (
            True if str(path) == "/usr/bin/fly-wmfunc" else original_access(path, mode)
        ),
    )
    monkeypatch.setattr(shutil, "which", Mock(side_effect=AssertionError("PATH lookup forbidden")))
    run = Mock(return_value=subprocess.CompletedProcess([], 0))
    monkeypatch.setattr(subprocess, "run", run)
    return run


def section(home: Path, tree: str, name: str = "tools", properties: bytes = b"") -> Path:
    directory = home / ".fly" / tree / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / ".directory").write_bytes(b"[Desktop Entry]\n" + properties)
    return directory


@pytest.mark.parametrize("kind", list(session.SessionKind))
def test_ib44_refresh_only_in_fly_session(
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    refresh: Mock,
    caplog: pytest.LogCaptureFixture,
    kind: session.SessionKind,
) -> None:
    directory = section(home, "startmenu2", properties=b"Name=Utility\n")
    monkeypatch.setattr(session, "detect", lambda: kind)
    with caplog.at_level(logging.INFO):
        result = userinstall.register()
    assert result.fly_written == (directory / "astra-voice.desktop",)
    assert result.fly_written[0].is_file()
    if kind is session.SessionKind.FLY:
        refresh.assert_called_once()
        assert refresh.call_args.args == (["/usr/bin/fly-wmfunc", "FLYWM_UPDATE_STARTMENU"],)
    else:
        refresh.assert_not_called()
        assert "текущая сессия не Fly" in caplog.text
        assert str(home) not in caplog.text


def owned() -> bytes:
    return userinstall.menu_entry_bytes(Path("/old/AppRun"), "0.1.0")


def test_both_trees_exact_bytes_mode_and_idempotence(home: Path, refresh: Mock) -> None:
    new = section(home, "startmenu2", "level/tools", b"Name=Utility\n")
    old = section(home, "startmenu", "tools", b"Categories=Other;Utility;\n")
    assert userinstall._fly_menu_targets() == [new, old]
    result = userinstall.register()
    menu = home / "data/applications/astra-voice.desktop"
    expected = menu.read_bytes() + f"X-Fly-OriginFile={menu}\n".encode()
    entries = (new / menu.name, old / menu.name)
    assert result.fly_written == entries and result.fly_skipped == ()
    for entry in entries:
        assert entry.read_bytes() == expected
        assert stat.S_IMODE(entry.stat().st_mode) == 0o644
    refresh.assert_called_once_with(
        ["/usr/bin/fly-wmfunc", "FLYWM_UPDATE_STARTMENU"],
        shell=False,
        timeout=5,
        check=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=childenv.clean_env(),
    )
    shutil.which.assert_not_called()  # type: ignore[attr-defined]
    before = {p: p.stat().st_mtime_ns for p in entries}
    again = userinstall.register()
    assert again.fly_written == again.fly_skipped == ()
    assert again.fly_unchanged == entries
    assert {p: p.stat().st_mtime_ns for p in entries} == before
    assert refresh.call_count == 1


@pytest.mark.parametrize("tree", ["startmenu2", "startmenu"])
@pytest.mark.parametrize("valid", [True, False])
def test_category_tokens_and_depth_fallback(home: Path, tree: str, valid: bool) -> None:
    key = b"X-Categories" if tree == "startmenu2" else b"Categories"
    value = b"Other;Utility;" if valid else b"Other;NotUtility;"
    candidate = section(home, tree, "level/tools", key + b"=" + value + b"\n")
    section(home, tree, "level/deep/tools", key + b"=Utility;\n")
    expected = candidate if valid else home / ".fly" / tree
    assert userinstall._fly_menu_targets() == [expected]
    assert userinstall.register().fly_written == (expected / "astra-voice.desktop",)


def test_missing_fly_is_not_created(home: Path, refresh: Mock) -> None:
    userinstall.register()
    userinstall.unregister()
    assert not (home / ".fly").exists()
    refresh.assert_not_called()


@pytest.mark.parametrize("kind", ["foreign", "link", "directory", "fifo", "huge", "marker-group"])
def test_foreign_entries_preserved(
    home: Path, refresh: Mock, caplog: pytest.LogCaptureFixture, kind: str
) -> None:
    target = section(home, "startmenu2") / "astra-voice.desktop"
    # Без Utility запись должна оказаться в корне; проверяем чужую цель именно там.
    target = target.parent.parent / target.name
    if kind == "link":
        outside = home / "outside.desktop"
        outside.write_bytes(owned())
        target.symlink_to(outside)
    elif kind == "directory":
        target.mkdir()
    elif kind == "fifo":
        os.mkfifo(target)
    elif kind == "huge":
        target.write_bytes(owned() + b"x" * userinstall.DESKTOP_FILE_LIMIT)
    else:
        target.write_bytes(
            b"[Desktop Entry]\nExec=foreign\n"
            + (b"[Other]\nX-AstraVoice-Managed=true\n" if kind == "marker-group" else b"")
        )
    before = target.lstat()
    result = userinstall.register()
    assert result.fly_skipped == (target,)
    assert "Чужая запись меню Fly сохранена: ~/.fly/" in caplog.text
    assert str(home) not in caplog.text
    snapshot = userinstall.management_snapshot()
    assert str(target) in dict(snapshot.identities)
    userinstall.unregister()
    after = target.lstat()
    assert (after.st_ino, after.st_mode, after.st_mtime_ns, after.st_size) == (
        before.st_ino,
        before.st_mode,
        before.st_mtime_ns,
        before.st_size,
    )
    refresh.assert_not_called()


@pytest.mark.parametrize("operation", ["unregister", "remove_program"])
def test_moved_owned_removed_foreign_retained_and_depth_limited(
    home: Path, refresh: Mock, operation: str
) -> None:
    moved = []
    for tree in ("startmenu2", "startmenu"):
        directory = section(home, tree, "moved/deeper/tools")
        entry = directory / "astra-voice.desktop"
        entry.write_bytes(owned())
        moved.append(entry)
    foreign = home / ".fly/startmenu/astra-voice.desktop"
    foreign.write_bytes(b"[Desktop Entry]\nExec=foreign\n")
    deep = section(home, "startmenu2", "a/b/c/d") / "astra-voice.desktop"
    deep.write_bytes(owned())
    snapshot = userinstall.management_snapshot()
    assert snapshot.can_unregister and snapshot.menu == "ours"
    assert userinstall.status().menu == "ours"
    for entry in moved:
        assert str(entry) in dict(snapshot.identities)
    result = getattr(userinstall, operation)()
    removed = result if operation == "unregister" else result.unregistered
    assert set(removed.fly) == set(moved)
    assert all(not entry.exists() for entry in moved)
    assert foreign.read_bytes() == b"[Desktop Entry]\nExec=foreign\n"
    assert deep.read_bytes() == owned()
    assert userinstall.status().menu == "foreign"
    refresh.assert_called_once()
    getattr(userinstall, operation)()
    refresh.assert_called_once()


@pytest.mark.parametrize("failure", ["missing", "not-executable", "oserror", "timeout", "nonzero"])
def test_refresh_failures_nonfatal(
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    refresh: Mock,
    caplog: pytest.LogCaptureFixture,
    failure: str,
) -> None:
    section(home, "startmenu2", "tools", b"Name=Utility\n")
    if failure == "missing":
        original_lstat = os.lstat

        def missing(path: str | Path, **kwargs: object) -> os.stat_result:
            if str(path) == "/usr/bin/fly-wmfunc":
                raise FileNotFoundError(errno.ENOENT, "missing")
            return original_lstat(path, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(os, "lstat", missing)
    elif failure == "not-executable":
        original_access = os.access
        monkeypatch.setattr(
            os,
            "access",
            lambda path, mode: (
                False if str(path) == "/usr/bin/fly-wmfunc" else original_access(path, mode)
            ),
        )
    elif failure == "oserror":
        refresh.side_effect = OSError("ошибка")
    elif failure == "timeout":
        refresh.side_effect = subprocess.TimeoutExpired("fly-wmfunc", 5)
    else:
        refresh.return_value = subprocess.CompletedProcess([], 1)
    with caplog.at_level(logging.INFO):
        result = userinstall.register()
        removed = userinstall.unregister()
    assert len(result.fly_written) == len(removed.fly) == 1
    assert "Меню Fly" in caplog.text
    assert refresh.call_count == (0 if failure in ("missing", "not-executable") else 2)


@pytest.mark.parametrize("component", ["home", ".fly", "startmenu2", "section"])
def test_tree_and_section_symlinks_not_followed(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, refresh: Mock, component: str
) -> None:
    directory = section(home, "startmenu2", "tools", b"Name=Utility\n")
    (directory / "astra-voice.desktop").write_bytes(owned())
    target = {
        "home": home,
        ".fly": home / ".fly",
        "startmenu2": home / ".fly/startmenu2",
        "section": directory,
    }[component]
    outside = tmp_path / "outside"
    if component == "home":
        outside.symlink_to(home)
        monkeypatch.setenv("HOME", str(outside))
        protected = directory
    else:
        target.rename(outside)
        target.symlink_to(outside)
        protected = outside
    before = tree_snapshot(protected)
    result = userinstall.register()
    if component == "section":
        assert result.fly_written == (directory.parent / "astra-voice.desktop",)
    else:
        assert result.fly_written == ()
        refresh.assert_not_called()
    userinstall.unregister()
    assert tree_snapshot(protected) == before


@pytest.mark.parametrize("kind", ["symlink", "fifo", "huge", "wrong-group"])
def test_directory_metadata_safe_read_and_root_fallback(home: Path, kind: str) -> None:
    directory = section(home, "startmenu2", "tools", b"Name=Utility\n")
    metadata = directory / ".directory"
    metadata.unlink()
    if kind == "symlink":
        outside = home / "metadata"
        outside.write_bytes(b"[Desktop Entry]\nName=Utility\n")
        metadata.symlink_to(outside)
    elif kind == "fifo":
        os.mkfifo(metadata)
    elif kind == "huge":
        metadata.write_bytes(b"[Desktop Entry]\nName=Utility\n" + b"x" * 65536)
    else:
        metadata.write_bytes(b"[Other]\nName=Utility\n")
    assert userinstall._fly_menu_targets() == [directory.parent]


@pytest.mark.parametrize("component", [".fly", "startmenu2", "startmenu", "section"])
@pytest.mark.parametrize("insecurity", ["permissions", "owner"])
@pytest.mark.parametrize(
    "operation", ["validate_remove_paths", "management_snapshot", "_unregister", "unregister"]
)
def test_unsafe_fly_branch_skipped(
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    component: str,
    insecurity: str,
    operation: str,
) -> None:
    tree = component if component in ("startmenu2", "startmenu") else "startmenu2"
    directory = section(home, tree, "moved", b"Name=Utility\n")
    entry = directory / "astra-voice.desktop"
    entry.write_bytes(owned())
    unsafe = {
        ".fly": home / ".fly",
        "startmenu2": home / ".fly/startmenu2",
        "startmenu": home / ".fly/startmenu",
        "section": directory,
    }[component]
    safe_tree = "startmenu" if tree == "startmenu2" else "startmenu2"
    safe = section(home, safe_tree) / "astra-voice.desktop"
    safe.write_bytes(owned())
    xdg = home / "data/applications/astra-voice.desktop"
    xdg.parent.mkdir(parents=True)
    xdg.write_bytes(owned())
    before = tree_snapshot(directory)
    if insecurity == "permissions":
        unsafe.chmod(0o777)
    else:
        identity = unsafe.stat()
        original_fstat = os.fstat

        def foreign_owner(fd: int) -> os.stat_result:
            info = original_fstat(fd)
            if (info.st_dev, info.st_ino) == (identity.st_dev, identity.st_ino):
                values = list(info)
                values[4] = os.getuid() + 1
                return os.stat_result(values)
            return info

        monkeypatch.setattr(os, "fstat", foreign_owner)
    try:
        result = getattr(userinstall, operation)()
        if operation in ("_unregister", "unregister"):
            assert result.menu and not xdg.exists()
            assert safe.exists() is (component == ".fly")
        elif operation == "management_snapshot":
            assert result.menu == "ours"
            assert str(entry) not in dict(result.identities)
        assert entry.read_bytes() == owned()
        assert "папка меню Fly пропущена: ~/.fly" in caplog.text
        assert str(home) not in caplog.text and "/home" not in caplog.text
    finally:
        if insecurity == "permissions":
            unsafe.chmod(0o755)
    assert tree_snapshot(directory) == before


@pytest.mark.parametrize("component", [".fly", "tree", "section"])
@pytest.mark.parametrize(
    "operation", ["validate_remove_paths", "management_snapshot", "_unregister"]
)
def test_unsafe_fly_reopen_after_discovery_skipped(
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    component: str,
    operation: str,
) -> None:
    directory = section(home, "startmenu2", "moved")
    entry = directory / "astra-voice.desktop"
    entry.write_bytes(owned())
    unsafe = {
        ".fly": home / ".fly",
        "tree": directory.parent,
        "section": directory,
    }[component]
    target = directory if component == "section" else directory.parent

    def changed_after_discovery(depth: int, *, strict: bool = False) -> Iterator[Path]:
        unsafe.chmod(0o777)
        yield target

    monkeypatch.setattr(userinstall, "_fly_directories", changed_after_discovery)
    try:
        result = getattr(userinstall, operation)()
        if operation == "_unregister":
            assert result.fly == ()
        elif operation == "management_snapshot":
            assert str(entry) not in dict(result.identities)
        assert entry.read_bytes() == owned()
        assert "меню Fly" in caplog.text
        assert str(home) not in caplog.text and "/home" not in caplog.text
    finally:
        unsafe.chmod(0o755)


def test_fly_io_error_still_propagates(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    directory = section(home, "startmenu2")
    userinstall.register()
    autostart.set_enabled(True)
    menu, icons = userinstall._desktop_paths()
    registered = (menu, autostart._paths()[0], *userinstall._owned_icons(icons))
    assert len(registered) > 2
    before = {path: path.read_bytes() for path in registered}

    @contextmanager
    def broken(path: Path) -> Iterator[int]:
        raise OSError(errno.EIO, "ошибка чтения")
        yield 0  # pragma: no cover — контекстный менеджер

    monkeypatch.setattr(userinstall, "_fly_directory", broken)
    with pytest.raises(OSError, match="ошибка чтения"):
        with userinstall._optional_fly_directory(directory):
            pass
    with pytest.raises(OSError, match="ошибка чтения"):
        userinstall._unregister()
    # Неожиданный сбой Fly происходит до снятия остальной регистрации.
    assert {path: path.read_bytes() for path in registered} == before


def test_strict_menu_state_skips_symlink_replaced_after_discovery(
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    directory = section(home, "startmenu2")
    entry = directory / "astra-voice.desktop"
    entry.write_bytes(owned())
    outside = home / "outside"
    directory.rename(outside)
    directory.symlink_to(outside)
    before = tree_snapshot(outside)
    monkeypatch.setattr(
        userinstall, "_fly_directories", lambda depth, *, strict=False: iter((directory,))
    )
    assert list(userinstall._fly_menu_entries(strict=True)) == []
    assert tree_snapshot(outside) == before
    assert "папка меню Fly пропущена: ~/.fly" in caplog.text
    assert str(home) not in caplog.text


def test_foreign_xdg_prevents_fly_registration(
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    refresh: Mock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    for tree in ("startmenu2", "startmenu"):
        section(home, tree, properties=b"Name=Utility\nCategories=Utility;\n")
    xdg = home / "data/applications/astra-voice.desktop"
    xdg.parent.mkdir(parents=True)
    foreign = b"[Desktop Entry]\nExec=foreign\n"
    xdg.write_bytes(foreign)
    before = tree_snapshot(home / ".fly")
    register_fly = Mock(wraps=userinstall._register_fly)
    monkeypatch.setattr(userinstall, "_register_fly", register_fly)
    result = userinstall.register()
    assert result.menu == "foreign"
    assert result.fly_written == result.fly_skipped == ()
    assert xdg.read_bytes() == foreign
    assert tree_snapshot(home / ".fly") == before
    register_fly.assert_not_called()
    refresh.assert_not_called()
    shutil.which.assert_not_called()  # type: ignore[attr-defined]
    assert "foreign_xdg_entry" in caplog.text
    assert str(home) not in caplog.text


@pytest.mark.parametrize("error", [errno.EACCES, errno.EPERM])
def test_directory_open_access_denied_skipped(
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    error: int,
) -> None:
    directory = section(home, "startmenu2")
    entry = directory / "astra-voice.desktop"
    entry.write_bytes(owned())
    original_open = os.open

    def denied(
        path: str | Path, flags: int, mode: int = 0o777, *, dir_fd: int | None = None
    ) -> int:
        if path == "startmenu2":
            raise PermissionError(error, str(home / "private"))
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", denied)
    with userinstall._optional_fly_directory(directory) as fd:
        assert fd is None
    assert list(userinstall._fly_directories(3, strict=True)) == []
    assert userinstall._unregister().fly == ()
    assert entry.read_bytes() == owned()
    assert "папка меню Fly пропущена: ~/.fly" in caplog.text
    assert str(home) not in caplog.text


def test_file_access_error_is_not_swallowed_by_directory_guard(home: Path) -> None:
    directory = section(home, "startmenu2")
    with pytest.raises(PermissionError, match="ошибка файла"):
        with userinstall._optional_fly_directory(directory):
            raise PermissionError(errno.EACCES, "ошибка файла")


def test_snapshot_tracks_entry_under_root_owned_fly_parent(
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = section(home, "startmenu2").parent
    entry = directory / "astra-voice.desktop"
    entry.write_bytes(owned())
    parent = (home / ".fly").stat()
    original_fstat = os.fstat

    def root_owner(fd: int) -> os.stat_result:
        info = original_fstat(fd)
        if (info.st_dev, info.st_ino) == (parent.st_dev, parent.st_ino):
            values = list(info)
            values[4] = 0
            return os.stat_result(values)
        return info

    monkeypatch.setattr(os, "fstat", root_owner)
    snapshot = userinstall.management_snapshot()
    assert str(entry) in dict(snapshot.identities)
    entry.write_bytes(owned() + b"Comment=changed\n")
    assert userinstall.management_snapshot() != snapshot


def test_snapshot_detects_moved_entry_and_file_change(home: Path) -> None:
    directory = section(home, "startmenu2", "tools", b"Name=Utility\n")
    userinstall.register()
    before = userinstall.management_snapshot()
    entry = directory / "astra-voice.desktop"
    entry.write_bytes(owned())
    assert userinstall.management_snapshot() != before
    before = userinstall.management_snapshot()
    entry.rename(directory.parent / entry.name)
    assert userinstall.management_snapshot() != before


def test_unsafe_origin_cannot_inject_property(
    home: Path, monkeypatch: pytest.MonkeyPatch, refresh: Mock
) -> None:
    directory = section(home, "startmenu2", "tools", b"Name=Utility\n")
    monkeypatch.setattr(
        userinstall,
        "_desktop_paths",
        lambda: (home / "bad\nExec=foreign/app.desktop", home / "data/icons/hicolor"),
    )
    result = userinstall.register()
    assert result.fly_written == ()
    assert result.fly_skipped == (directory / "astra-voice.desktop",)
    assert not (directory / "astra-voice.desktop").exists()
    refresh.assert_not_called()


def test_status_keeps_xdg_menu_when_fly_is_unsafe(home: Path) -> None:
    userinstall.register()
    directory = section(home, "startmenu2", "tools", b"Name=Utility\n")
    directory.chmod(0o777)
    try:
        assert userinstall.status().menu == "ours"
    finally:
        directory.chmod(0o755)


def test_vanished_target_is_not_recreated(
    home: Path, monkeypatch: pytest.MonkeyPatch, refresh: Mock
) -> None:
    directory = section(home, "startmenu2", "tools", b"Name=Utility\n")
    monkeypatch.setattr(userinstall, "_fly_menu_targets", lambda: [directory])
    (directory / ".directory").unlink()
    directory.rmdir()
    result = userinstall.register()
    assert result.fly_skipped == (directory / "astra-voice.desktop",)
    assert not directory.exists()
    refresh.assert_not_called()


def test_target_replaced_with_link_after_discovery_is_not_written(
    home: Path, monkeypatch: pytest.MonkeyPatch, refresh: Mock
) -> None:
    directory = section(home, "startmenu2", "tools", b"Name=Utility\n")
    outside = home / "outside"
    directory.rename(outside)
    directory.symlink_to(outside)
    monkeypatch.setattr(userinstall, "_fly_menu_targets", lambda: [directory])
    before = tree_snapshot(outside)
    result = userinstall.register()
    assert result.fly_skipped == (directory / "astra-voice.desktop",)
    assert tree_snapshot(outside) == before
    refresh.assert_not_called()


def test_internal_unregister_removes_moved_copies_once(home: Path, refresh: Mock) -> None:
    moved = []
    for tree in ("startmenu2", "startmenu"):
        directory = section(home, tree, "moved/deeper/tools")
        entry = directory / "astra-voice.desktop"
        entry.write_bytes(owned())
        moved.append(entry)
    foreign = home / ".fly/startmenu/astra-voice.desktop"
    foreign.write_bytes(b"[Desktop Entry]\nExec=foreign\n")
    linked = home / ".fly/startmenu2/astra-voice.desktop"
    linked.symlink_to(foreign)
    result = userinstall._unregister(deb_executable=home / "missing-deb")
    assert set(result.fly) == set(moved)
    assert all(not entry.exists() for entry in moved)
    assert foreign.read_bytes() == b"[Desktop Entry]\nExec=foreign\n"
    assert linked.is_symlink()
    refresh.assert_called_once()
    assert userinstall._unregister(deb_executable=home / "missing-deb").fly == ()
    refresh.assert_called_once()


@pytest.mark.parametrize("component", ["home", "ancestor"])
def test_home_symlink_has_explicit_warning(
    home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    refresh: Mock,
    component: str,
) -> None:
    directory = section(home, "startmenu2", properties=b"Name=Utility\n")
    entry = directory / "astra-voice.desktop"
    entry.write_bytes(owned())
    link = tmp_path / "linked"
    link.symlink_to(home if component == "home" else tmp_path)
    monkeypatch.setenv("HOME", str(link if component == "home" else link / "home"))
    result = userinstall.register()
    assert result.fly_written == result.fly_unchanged == ()
    assert entry.read_bytes() == owned()
    assert any(
        record.levelno == logging.WARNING
        and record.message == "fly_menu_skipped reason=home_symlink"
        for record in caplog.records
    )
    refresh.assert_not_called()


@pytest.mark.parametrize("open_error", [errno.ENOTDIR, errno.ELOOP])
@pytest.mark.parametrize("stat_error", [errno.ENOENT, errno.EACCES, errno.EIO])
def test_home_ancestor_diagnostic_failure_preserves_open_error(
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    open_error: int,
    stat_error: int,
) -> None:
    directory = section(home, "startmenu2")
    ancestor = home.parent.name
    primary = OSError(open_error, "ошибка открытия предка")
    secondary = OSError(stat_error, "ошибка диагностической проверки")
    original_open = os.open
    original_stat = os.stat
    failed_fds: list[int] = []
    diagnostic_fds: list[int] = []

    def failing_open(
        path: str | Path, flags: int, mode: int = 0o777, *, dir_fd: int | None = None
    ) -> int:
        if path == ancestor and dir_fd is not None:
            failed_fds.append(dir_fd)
            raise primary
        return original_open(path, flags, mode, dir_fd=dir_fd)

    def failing_stat(
        path: str | Path, *, dir_fd: int | None = None, follow_symlinks: bool = True
    ) -> os.stat_result:
        if path == ancestor and dir_fd is not None and not follow_symlinks:
            diagnostic_fds.append(dir_fd)
            raise secondary
        return original_stat(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(os, "open", failing_open)
    monkeypatch.setattr(os, "stat", failing_stat)
    with pytest.raises(OSError) as caught:
        with userinstall._fly_directory(directory):
            pytest.fail("недоступный предок не должен открывать каталог меню")
    assert caught.value is primary
    assert len(failed_fds) == 1
    assert diagnostic_fds == failed_fds
    with pytest.raises(OSError) as closed:
        os.fstat(failed_fds[0])
    assert closed.value.errno == errno.EBADF


def test_refresh_uses_absolute_utility_and_clean_environment(
    home: Path, monkeypatch: pytest.MonkeyPatch, refresh: Mock
) -> None:
    monkeypatch.setenv("PATH", str(home / "untrusted-bin"))
    for name in (
        "APPDIR",
        "APPIMAGE",
        "ASTRA_VOICE_APPIMAGE_DIR",
        "LD_LIBRARY_PATH",
        "QT_PLUGIN_PATH",
    ):
        monkeypatch.setenv(name, "/bundle/private")
    monkeypatch.setenv(childenv.ORIG_UNSET, "LD_LIBRARY_PATH QT_PLUGIN_PATH")
    monkeypatch.delenv(childenv.ORIG_PREFIX + "LD_LIBRARY_PATH", raising=False)
    monkeypatch.delenv(childenv.ORIG_PREFIX + "QT_PLUGIN_PATH", raising=False)
    expected = childenv.clean_env()
    userinstall._refresh_fly_menu()
    assert refresh.call_args.args == (["/usr/bin/fly-wmfunc", "FLYWM_UPDATE_STARTMENU"],)
    actual = refresh.call_args.kwargs["env"]
    assert actual == expected
    assert not {"APPDIR", "APPIMAGE", "LD_LIBRARY_PATH", "QT_PLUGIN_PATH"}.intersection(actual)
    assert not any(name.startswith("ASTRA_VOICE_") for name in actual)
    assert os.environ["LD_LIBRARY_PATH"] == "/bundle/private"
    shutil.which.assert_not_called()  # type: ignore[attr-defined]


@pytest.mark.parametrize("tree", ["startmenu2", "startmenu"])
def test_existing_moved_copies_updated_without_new_tools_entry(
    home: Path, refresh: Mock, tree: str
) -> None:
    root = section(home, tree).parent
    original = userinstall.register()
    assert original.fly_written == (root / "astra-voice.desktop",)
    moved_dir = section(home, tree, "moved/deeper/custom")
    moved = moved_dir / "astra-voice.desktop"
    (root / moved.name).rename(moved)
    tools = section(
        home, tree, "Tools", b"Name=Utility\nCategories=Utility;\nX-Categories=Utility;\n"
    )
    # Появление штатного раздела не возвращает перенесённую пользователем запись.
    again = userinstall.register()
    assert again.fly_written == again.fly_skipped == ()
    assert again.fly_unchanged == (moved,)
    refresh.assert_called_once()
    moved.write_bytes(owned())
    second = section(home, tree, "another") / moved.name
    second.write_bytes(owned())
    other_tree = "startmenu" if tree == "startmenu2" else "startmenu2"
    other_tools = section(home, other_tree, "Tools", b"Name=Utility\nCategories=Utility;\n")
    updated = userinstall.register()
    assert set(updated.fly_written) == {moved, second, other_tools / moved.name}
    assert not (tools / moved.name).exists()
    assert not (root / moved.name).exists()
    assert moved.read_bytes() == second.read_bytes() == (other_tools / moved.name).read_bytes()
    before = {entry: entry.stat().st_mtime_ns for entry in updated.fly_written}
    final = userinstall.register()
    assert final.fly_written == final.fly_skipped == ()
    assert set(final.fly_unchanged) == set(updated.fly_written)
    assert {entry: entry.stat().st_mtime_ns for entry in before} == before
    assert refresh.call_count == 2


@pytest.mark.parametrize("kind", ["file", "directory", "tree", "readonly"])
@pytest.mark.parametrize(
    "operation", ["unregister", "_unregister", "validate_remove_paths", "management_snapshot"]
)
@pytest.mark.skipif(os.geteuid() == 0, reason="root обходит реальные права chmod")
def test_real_permissions_preserve_fly_and_complete_other_removal(
    home: Path, refresh: Mock, caplog: pytest.LogCaptureFixture, kind: str, operation: str
) -> None:
    directory = section(home, "startmenu2", properties=b"Name=Utility\n")
    userinstall.register()
    autostart.set_enabled(True)
    entry = directory / "astra-voice.desktop"
    xdg, icons = userinstall._desktop_paths()
    installed_icons = tuple(userinstall._owned_icons(icons))
    assert installed_icons
    auto = autostart._paths()[0]
    before = entry.read_bytes()
    protected = entry if kind == "file" else directory.parent if kind == "tree" else directory
    original_mode = stat.S_IMODE(protected.stat().st_mode)
    protected.chmod(0 if kind == "file" else 0o500 if kind == "readonly" else 0o400)
    try:
        required = os.R_OK if kind == "file" else os.W_OK if kind == "readonly" else os.X_OK
        if os.access(protected, required):
            pytest.skip("файловая система не применяет chmod")
        refresh.reset_mock()
        if operation in ("unregister", "_unregister"):
            result = getattr(userinstall, operation)(deb_executable=home / "missing-deb")
            assert result.menu and result.autostart and set(result.icons) == set(installed_icons)
            assert result.fly == ()
            assert not xdg.exists() and not auto.exists()
            assert all(not icon.exists() for icon in installed_icons)
            refresh.assert_not_called()
        else:
            result = getattr(userinstall, operation)()
            assert xdg.exists() and auto.exists()
            assert all(icon.exists() for icon in installed_icons)
            if operation == "management_snapshot" and kind == "file":
                assert len(dict(result.identities)[str(entry)]) == 4
        if kind != "readonly" or operation in ("unregister", "_unregister"):
            assert "меню Fly" in caplog.text
        assert str(home) not in caplog.text
    finally:
        protected.chmod(original_mode)
    assert entry.read_bytes() == before


@pytest.mark.skipif(os.geteuid() == 0, reason="root обходит реальные права chmod")
def test_real_unreadable_entry_state_and_identity(
    home: Path, caplog: pytest.LogCaptureFixture
) -> None:
    directory = section(home, "startmenu2")
    entry = directory / "astra-voice.desktop"
    entry.write_bytes(owned())
    entry.chmod(0)
    try:
        if os.access(entry, os.R_OK):
            pytest.skip("файловая система не применяет chmod")
        with userinstall._fly_directory(directory) as fd:
            info = entry.stat()
            assert userinstall._menu_state(entry, directory_fd=fd) == "foreign"
            assert userinstall._management_identity(entry, directory_fd=fd) == (
                info.st_dev,
                info.st_ino,
                info.st_mode,
                info.st_uid,
            )
        assert "Недоступная запись меню Fly сохранена" in caplog.text
        # Льготная обработка относится только к Fly, не к XDG.
        with pytest.raises(PermissionError):
            userinstall._menu_state(entry)
        with pytest.raises(PermissionError):
            userinstall._management_identity(entry)
    finally:
        entry.chmod(0o644)
    assert entry.read_bytes() == owned()


@pytest.mark.parametrize("error", [errno.ENOENT, errno.EACCES, errno.EPERM])
@pytest.mark.parametrize("stage", ["stat", "open"])
@pytest.mark.parametrize("strict", [True, False])
def test_child_discovery_error_keeps_parent_and_sibling(
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    error: int,
    stage: str,
    strict: bool,
) -> None:
    skipped = section(home, "startmenu2", "a-denied")
    sibling = section(home, "startmenu2", "b-allowed")
    original_stat = os.stat
    original_open = os.open

    def failing_stat(
        path: str | Path, *, dir_fd: int | None = None, follow_symlinks: bool = True
    ) -> os.stat_result:
        if path == skipped.name and dir_fd is not None:
            raise OSError(error, "недоступная запись")
        return original_stat(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)

    def failing_open(
        path: str | Path, flags: int, mode: int = 0o777, *, dir_fd: int | None = None
    ) -> int:
        if path == skipped.name and dir_fd is not None:
            raise OSError(error, "недоступный каталог")
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, stage, failing_stat if stage == "stat" else failing_open)
    assert list(userinstall._fly_directories(3, strict=strict)) == [skipped.parent, sibling]
    assert "a-denied" in caplog.text
    assert str(home) not in caplog.text


def test_register_result_preserves_positional_arguments(home: Path) -> None:
    result = userinstall.RegisterResult("written", (), (), False, (home,), ())
    assert result.fly_written == (home,)
    assert result.fly_skipped == result.fly_unchanged == ()


@pytest.mark.parametrize("error", [errno.EACCES, errno.EPERM, errno.EIO])
def test_fly_read_access_denied_uses_base_identity(
    home: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, error: int
) -> None:
    directory = section(home, "startmenu2")
    entry = directory / "astra-voice.desktop"
    entry.write_bytes(owned())
    info = entry.stat()
    original_fdopen = os.fdopen

    @contextmanager
    def denied_read(fd: int, mode: str) -> Iterator[Mock]:
        with original_fdopen(fd, mode) as stream:
            wrapped = Mock(wraps=stream)
            wrapped.read.side_effect = OSError(error, "чтение запрещено")
            yield wrapped

    monkeypatch.setattr(os, "fdopen", denied_read)
    with userinstall._fly_directory(directory) as fd:
        if error == errno.EIO:
            with pytest.raises(OSError, match="чтение запрещено"):
                userinstall._management_identity(entry, directory_fd=fd)
            with pytest.raises(OSError, match="чтение запрещено"):
                userinstall._menu_state(entry, directory_fd=fd)
        else:
            assert userinstall._management_identity(entry, directory_fd=fd) == (
                info.st_dev,
                info.st_ino,
                info.st_mode,
                info.st_uid,
            )
            assert userinstall._menu_state(entry, directory_fd=fd) == "foreign"
            assert "Недоступная запись меню Fly сохранена" in caplog.text
    assert entry.read_bytes() == owned()
