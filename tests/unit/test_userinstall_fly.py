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

from astra_voice.core import paths
from astra_voice.platform import userinstall
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
    monkeypatch.setattr(shutil, "which", Mock(return_value="/usr/bin/fly-wmfunc"))
    run = Mock(return_value=subprocess.CompletedProcess([], 0))
    monkeypatch.setattr(subprocess, "run", run)
    return run


def section(home: Path, tree: str, name: str = "tools", properties: bytes = b"") -> Path:
    directory = home / ".fly" / tree / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / ".directory").write_bytes(b"[Desktop Entry]\n" + properties)
    return directory


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
        ["fly-wmfunc", "FLYWM_UPDATE_STARTMENU"],
        timeout=5,
        check=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    shutil.which.assert_called_once_with("fly-wmfunc")  # type: ignore[attr-defined]
    before = {p: p.stat().st_mtime_ns for p in entries}
    again = userinstall.register()
    assert again.fly_written == () and again.fly_skipped == entries
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


@pytest.mark.parametrize("failure", ["missing", "oserror", "timeout", "nonzero"])
def test_refresh_failures_nonfatal(
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    refresh: Mock,
    caplog: pytest.LogCaptureFixture,
    failure: str,
) -> None:
    section(home, "startmenu2", "tools", b"Name=Utility\n")
    if failure == "missing":
        monkeypatch.setattr(shutil, "which", Mock(return_value=None))
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
    assert refresh.call_count == (0 if failure == "missing" else 2)


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
