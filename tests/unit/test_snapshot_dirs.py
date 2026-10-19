"""Каталоги и атомарная запись проверяются без Qt и настоящих эталонов."""

from __future__ import annotations

import hashlib
import os
import pwd
import secrets
import stat
import sys
import tempfile
import warnings
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from helpers import snapshots  # noqa: E402

pytestmark = pytest.mark.unit
ENV_VAR = "ASTRA_VOICE_SNAPSHOT_DIR_TEST"
REFS_DEFAULT = "design/refs/impl/test"


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    def forbidden_gettempdir() -> str:
        raise AssertionError("snapshot_dir не должен использовать tempfile.gettempdir()")

    root = tmp_path / "repo"
    root.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(snapshots, "REPO", root)
    monkeypatch.setattr(snapshots, "real_home", lambda: home)
    monkeypatch.setattr(tempfile, "gettempdir", forbidden_gettempdir)
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    monkeypatch.delenv(ENV_VAR, raising=False)
    monkeypatch.delenv("ASTRA_VOICE_UPDATE_REFS", raising=False)
    return root


def cache_dir(repo: Path) -> Path:
    key = hashlib.sha256(str(repo.resolve()).encode()).hexdigest()[:16]
    return repo.parent / "home/.cache/astra-voice/snapshots" / key / "test"


@pytest.mark.parametrize("flag", [None, "", "0", "true"])
def test_default_uses_home_cache(
    repo: Path, monkeypatch: pytest.MonkeyPatch, flag: str | None
) -> None:
    if flag is not None:
        monkeypatch.setenv("ASTRA_VOICE_UPDATE_REFS", flag)
    actual = snapshots.snapshot_dir(ENV_VAR, "test", REFS_DEFAULT)
    assert actual == cache_dir(repo)
    for directory in (actual, *list(actual.parents)[:4]):
        assert stat.S_IMODE(directory.lstat().st_mode) == 0o700
    assert not (repo / "design").exists()


@pytest.mark.parametrize("xdg", ["", "relative/cache", "/unusable/absolute/cache"])
def test_home_and_xdg_are_ignored(repo: Path, monkeypatch: pytest.MonkeyPatch, xdg: str) -> None:
    monkeypatch.setenv("HOME", str(repo / "fake-home"))
    monkeypatch.setenv("XDG_CACHE_HOME", xdg)
    assert snapshots.snapshot_dir(ENV_VAR, "test", REFS_DEFAULT) == cache_dir(repo)
    assert not (repo / "fake-home").exists()


def test_real_home_uses_passwd_not_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))
    monkeypatch.setattr(os, "getuid", lambda: 12345)
    lookup = Mock(return_value=SimpleNamespace(pw_dir=str(tmp_path / "passwd-home")))
    monkeypatch.setattr(pwd, "getpwuid", lookup)
    assert snapshots.real_home() == tmp_path / "passwd-home"
    lookup.assert_called_once_with(12345)


def test_tree_key_is_stable_and_isolates_worktrees(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = snapshots.snapshot_dir(ENV_VAR, "test", REFS_DEFAULT)
    assert snapshots.snapshot_dir(ENV_VAR, "test", REFS_DEFAULT) == first
    assert first.parent.name == hashlib.sha256(str(repo.resolve()).encode()).hexdigest()[:16]
    other = repo.parent / "other-worktree"
    other.mkdir()
    monkeypatch.setattr(snapshots, "REPO", other)
    second = snapshots.snapshot_dir(ENV_VAR, "test", REFS_DEFAULT)
    assert second != first and second.parent.parent == first.parent.parent
    alias = repo.parent / "alias"
    alias.symlink_to(repo, target_is_directory=True)
    monkeypatch.setattr(snapshots, "REPO", alias)
    assert snapshots.snapshot_dir(ENV_VAR, "test", REFS_DEFAULT) == first


@pytest.mark.parametrize("relative", [False, True])
def test_explicit_directory_outside_refs_is_used(
    repo: Path, monkeypatch: pytest.MonkeyPatch, relative: bool
) -> None:
    explicit = "artifacts/test" if relative else str(repo.parent / "outside")
    monkeypatch.setenv(ENV_VAR, explicit)
    monkeypatch.chdir(repo.parent)
    with warnings.catch_warnings(record=True) as caught:
        actual = snapshots.snapshot_dir(ENV_VAR, "test", REFS_DEFAULT)
    assert actual == repo / explicit
    assert caught == []
    assert stat.S_IMODE(actual.lstat().st_mode) == 0o700


@pytest.mark.parametrize("explicit", [None, "artifacts/test", "design/refs/impl/other"])
def test_update_flag_selects_default_refs_and_ignores_env(
    repo: Path, monkeypatch: pytest.MonkeyPatch, explicit: str | None
) -> None:
    monkeypatch.setenv("ASTRA_VOICE_UPDATE_REFS", "1")
    if explicit is not None:
        monkeypatch.setenv(ENV_VAR, explicit)
    with warnings.catch_warnings(record=True) as caught:
        actual = snapshots.snapshot_dir(ENV_VAR, "test", REFS_DEFAULT)
    assert actual == repo / REFS_DEFAULT
    assert caught == []
    assert actual.is_dir()


@pytest.mark.parametrize("explicit", ["design/refs", "design/refs/impl/other", "design/other"])
@pytest.mark.parametrize("relative", [False, True])
def test_explicit_design_directory_is_redirected(
    repo: Path, monkeypatch: pytest.MonkeyPatch, explicit: str, relative: bool
) -> None:
    monkeypatch.setenv(ENV_VAR, explicit if relative else str(repo / explicit))
    with pytest.warns(UserWarning, match="эталоны.*не тронуты"):
        actual = snapshots.snapshot_dir(ENV_VAR, "test", REFS_DEFAULT)
    assert actual == cache_dir(repo)
    assert not (repo / "design").exists()


def test_symlink_into_refs_is_redirected(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (repo / "alias").symlink_to(repo / "design/refs", target_is_directory=True)
    monkeypatch.setenv(ENV_VAR, "alias/impl/test")
    with pytest.warns(UserWarning, match="эталоны.*не тронуты"):
        actual = snapshots.snapshot_dir(ENV_VAR, "test", REFS_DEFAULT)
    assert actual == cache_dir(repo)
    assert not (repo / "design").exists()


def test_update_flag_rejects_default_outside_refs(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ASTRA_VOICE_UPDATE_REFS", "1")
    with pytest.raises(ValueError, match="refs_default"):
        snapshots.snapshot_dir(ENV_VAR, "test", "artifacts/test")


@pytest.mark.parametrize("leaf", ["", "astra-voice", "astra-voice/snapshots", "tree", "set"])
@pytest.mark.parametrize("problem", ["symlink", "owner", "mode", "file"])
def test_default_rejects_unsafe_chain(
    repo: Path, monkeypatch: pytest.MonkeyPatch, leaf: str, problem: str
) -> None:
    root = repo.parent / "home/.cache"
    unsafe = (
        cache_dir(repo)
        if leaf == "set"
        else (cache_dir(repo).parent if leaf == "tree" else root / leaf)
    )
    unsafe.parent.mkdir(parents=True, exist_ok=True)
    if problem == "symlink":
        unsafe.symlink_to(repo, target_is_directory=True)
    elif problem == "file":
        unsafe.write_text("not a directory")
    else:
        unsafe.mkdir()
        _fake_stat(monkeypatch, unsafe, problem)
    with pytest.raises(ValueError) as caught:
        snapshots.snapshot_dir(ENV_VAR, "test", REFS_DEFAULT)
    assert str(unsafe) in str(caught.value)


def _fake_stat(monkeypatch: pytest.MonkeyPatch, target: Path, problem: str) -> None:
    original = os.lstat

    def lstat(path: str | Path, *, dir_fd: int | None = None) -> os.stat_result:
        result = original(path, dir_fd=dir_fd)
        if Path(path) != target:
            return result
        values = list(result)
        if problem == "owner":
            values[4] = os.getuid() + 1
        else:
            values[0] = int(values[0]) | 0o020
        return os.stat_result(values)

    monkeypatch.setattr(os, "lstat", lstat)


@pytest.mark.parametrize("mode", ["explicit", "refs", "fallback"])
@pytest.mark.parametrize("problem", ["symlink", "owner", "mode"])
def test_selected_leaf_is_checked(
    repo: Path, monkeypatch: pytest.MonkeyPatch, mode: str, problem: str
) -> None:
    destination = repo / REFS_DEFAULT if mode == "refs" else repo / "outside"
    destination.parent.mkdir(parents=True, exist_ok=True)
    if problem == "symlink":
        target = destination.parent / "target"
        target.mkdir()
        destination.symlink_to(target, target_is_directory=True)
    else:
        destination.mkdir()
        _fake_stat(monkeypatch, destination, problem)
    if mode == "explicit":
        monkeypatch.setenv(ENV_VAR, str(destination))
    elif mode == "refs":
        monkeypatch.setenv("ASTRA_VOICE_UPDATE_REFS", "1")
    with pytest.raises(ValueError) as caught:
        snapshots.snapshot_dir(ENV_VAR, "test", REFS_DEFAULT, default=destination)
    assert str(destination) in str(caught.value)


@pytest.mark.parametrize("mode", ["explicit", "refs", "fallback"])
def test_existing_external_parent_is_not_checked(
    repo: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    destination = repo / REFS_DEFAULT if mode == "refs" else repo / "parent/leaf"
    destination.parent.mkdir(parents=True)
    _fake_stat(monkeypatch, destination.parent, "mode")
    if mode == "explicit":
        monkeypatch.setenv(ENV_VAR, str(destination))
    elif mode == "refs":
        monkeypatch.setenv("ASTRA_VOICE_UPDATE_REFS", "1")
    assert snapshots.snapshot_dir(ENV_VAR, "test", REFS_DEFAULT, default=destination) == destination


def test_cache_alias_into_design_is_rejected(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cache = repo.parent / "home/.cache"
    cache.symlink_to(repo / "design", target_is_directory=True)
    with pytest.raises(ValueError, match="cache указывает в design"):
        snapshots.snapshot_dir(ENV_VAR, "test", REFS_DEFAULT)
    assert not (repo / "design").exists()


def test_creation_error_has_path_and_reason(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def denied(*args: object, **kwargs: object) -> None:
        raise PermissionError(13, "permission denied")

    monkeypatch.setattr(Path, "mkdir", denied)
    with pytest.raises(ValueError, match="permission denied") as caught:
        snapshots.snapshot_dir(ENV_VAR, "test", REFS_DEFAULT)
    assert str(repo.parent / "home/.cache") in str(caught.value)


def test_created_directory_is_checked_again(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    destination = repo / "outside"
    monkeypatch.setenv(ENV_VAR, str(destination))
    _fake_stat(monkeypatch, destination, "owner")
    with pytest.raises(ValueError, match="чужой владелец"):
        snapshots.snapshot_dir(ENV_VAR, "test", REFS_DEFAULT)


def test_atomic_write_ignores_old_tmp_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.write_bytes(b"untouched")
    stale = tmp_path / ".image.png.tmp"
    stale.symlink_to(target)
    destination = snapshots.write_snapshot(tmp_path, "image.png", b"PNG")
    assert destination.read_bytes() == b"PNG"
    assert target.read_bytes() == b"untouched"
    assert stale.is_symlink()
    assert list(tmp_path.glob(".*.tmp")) == [stale]


def test_descriptor_flags_unique_names_mode_and_fsync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_open, original_write, original_fsync, original_close = (
        os.open,
        os.write,
        os.fsync,
        os.close,
    )
    opened: list[Path] = []
    events: list[tuple[str, int]] = []

    def open_file(path: Path, flags: int, mode: int) -> int:
        assert path.parent == tmp_path and path.name.startswith(".image.png.")
        assert flags == os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_WRONLY | os.O_CLOEXEC
        assert mode == 0o600
        fd = original_open(path, flags, mode)
        assert stat.S_IMODE(os.fstat(fd).st_mode) == 0o600
        opened.append(path)
        return fd

    def write(fd: int, data: Any) -> int:
        events.append(("write", fd))
        return original_write(fd, data[:2])  # Exercise partial writes.

    def fsync(fd: int) -> None:
        events.append(("fsync", fd))
        original_fsync(fd)

    def close(fd: int) -> None:
        events.append(("close", fd))
        original_close(fd)

    with monkeypatch.context() as patch:
        patch.setattr(os, "open", open_file)
        patch.setattr(os, "write", write)
        patch.setattr(os, "fsync", fsync)
        patch.setattr(os, "close", close)
        snapshots.write_snapshot(tmp_path, "image.png", b"first")
        result = snapshots.write_snapshot(tmp_path, "image.png", b"second")
    assert result.read_bytes() == b"second"
    assert len(set(opened)) == 2
    assert [event for event, _ in events] == [
        "write",
        "write",
        "write",
        "fsync",
        "close",
        "write",
        "write",
        "write",
        "fsync",
        "close",
    ]
    assert not list(tmp_path.glob(".*.tmp"))


def test_collision_does_not_remove_existing_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "target"
    target.write_bytes(b"untouched")
    collision = tmp_path / ".image.png.collision.tmp"
    collision.symlink_to(target)
    names = iter(["collision", "unique"])
    monkeypatch.setattr(secrets, "token_hex", lambda size: next(names))
    snapshots.write_snapshot(tmp_path, "image.png", b"PNG")
    assert collision.is_symlink()
    assert target.read_bytes() == b"untouched"
    assert list(tmp_path.glob(".*.tmp")) == [collision]


@pytest.mark.parametrize("failure", ["encode", "write", "zero-write", "fsync", "replace"])
def test_failed_write_cleans_temporary_and_preserves_previous(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    destination = tmp_path / "image.png"
    destination.write_bytes(b"previous")

    def fail(*args: object) -> Any:
        raise OSError("simulated failure")

    class BrokenImage:
        def save(self, device: Any, format: str) -> bool:
            raise OSError("encoding failure")

    with monkeypatch.context() as patch:
        if failure in {"write", "fsync", "replace"}:
            patch.setattr(os, failure, fail)
        elif failure == "zero-write":
            patch.setattr(os, "write", lambda fd, data: 0)
        with pytest.raises(OSError):
            snapshots.write_snapshot(
                tmp_path, "image.png", BrokenImage() if failure == "encode" else b"new PNG"
            )
    assert destination.read_bytes() == b"previous"
    assert list(tmp_path.iterdir()) == [destination]


def test_image_save_uses_qbuffer_and_atomic_replace(tmp_path: Path) -> None:
    from PyQt5.QtCore import QBuffer

    class Image:
        def save(self, device: Any, format: str) -> bool:
            assert isinstance(device, QBuffer)
            assert format == "PNG"
            assert device.isOpen()
            assert device.write(b"new") == 3
            return True

    target = tmp_path / "old-target"
    target.write_bytes(b"old")
    (tmp_path / "image.png").symlink_to(target)
    result = snapshots.write_snapshot(tmp_path, "image.png", Image())
    assert result.read_bytes() == b"new"
    assert not result.is_symlink()
    assert target.read_bytes() == b"old"
    assert not list(tmp_path.glob(".*.tmp"))


def test_real_qimage_encoding_needs_no_application(tmp_path: Path) -> None:
    from PyQt5.QtGui import QImage

    image = QImage(2, 2, QImage.Format_ARGB32)
    image.fill(0xFF112233)
    result = snapshots.write_snapshot(tmp_path, "image.png", image)
    assert result.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    decoded = QImage(str(result))
    assert decoded.size() == image.size()
    assert decoded.pixel(0, 0) == image.pixel(0, 0)


def test_failed_qbuffer_encoding_closes_buffer(tmp_path: Path) -> None:
    buffers: list[Any] = []

    class Image:
        def save(self, device: Any, format: str) -> bool:
            buffers.append(device)
            device.write(b"partial")
            return False

    with pytest.raises(OSError, match="PNG"):
        snapshots.write_snapshot(tmp_path, "image.png", Image())
    assert not buffers[0].isOpen()
    assert not list(tmp_path.iterdir())


def test_ready_bytes_do_not_import_qt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import builtins

    original = builtins.__import__

    def guarded_import(name: str, *args: Any, **kwargs: Any) -> Any:
        assert not name.startswith("PyQt5")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    assert snapshots.write_snapshot(tmp_path, "image.png", b"PNG").read_bytes() == b"PNG"


@pytest.mark.parametrize(
    "name", ["", ".", "..", "../image.png", "nested/image.png", "/image.png", "x\0y"]
)
def test_unsafe_names_are_rejected(tmp_path: Path, name: str) -> None:
    with pytest.raises(ValueError, match="имя снимка"):
        snapshots.write_snapshot(tmp_path, name, b"PNG")
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("mutation", ["change", "add", "remove", "file-to-dir", "symlink"])
def test_ib42_detects_refs_changes(repo: Path, mutation: str) -> None:
    refs = repo / "design/refs"
    refs.mkdir(parents=True)
    file = refs / "ref.png"
    file.write_bytes(b"original")
    guard = snapshots.RefsGuard.capture()
    if mutation == "change":
        file.write_bytes(b"changed")
    elif mutation == "add":
        (refs / "new.png").write_bytes(b"new")
    else:
        file.unlink()
        if mutation == "file-to-dir":
            file.mkdir()
        elif mutation == "symlink":
            file.symlink_to(repo / "missing")
    expected = "new.png" if mutation == "add" else "ref.png"
    assert guard.changed_paths() == [expected]
    assert f"design/refs/{expected}" in (guard.failure_message() or "")


@pytest.mark.parametrize("existing", [False, True])
def test_ib42_unchanged_returns_empty(repo: Path, existing: bool) -> None:
    if existing:
        (repo / "design/refs").mkdir(parents=True)
    guard = snapshots.RefsGuard.capture()
    (repo / "unrelated.txt").write_text("outside refs")
    assert guard.changed_paths() == []
    assert guard.failure_message() is None


def test_ib42_flag_at_session_start_allows_updates(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ASTRA_VOICE_UPDATE_REFS", "1")
    guard = snapshots.RefsGuard.capture()
    monkeypatch.delenv("ASTRA_VOICE_UPDATE_REFS")
    (repo / "design/refs").mkdir(parents=True)
    (repo / "design/refs/new.png").write_bytes(b"new")
    assert guard.changed_paths() == []


def test_ib42_late_flag_does_not_bypass_guard(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    guard = snapshots.RefsGuard.capture()
    monkeypatch.setenv("ASTRA_VOICE_UPDATE_REFS", "1")
    (repo / "design/refs").mkdir(parents=True)
    assert guard.changed_paths() == ["."]


def test_ib42_inventory_does_not_follow_symlinks(repo: Path) -> None:
    refs = repo / "design/refs"
    refs.mkdir(parents=True)
    outside = repo / "outside"
    outside.mkdir()
    (outside / "private").write_bytes(b"outside")
    (refs / "link").symlink_to(outside, target_is_directory=True)
    (refs / "broken").symlink_to(repo / "absent")
    (refs / "subdir").mkdir()
    (refs / "subdir/image.png").write_bytes(b"PNG")
    inventory = snapshots.refs_inventory(refs)
    assert set(inventory) == {".", "link", "broken", "subdir", "subdir/image.png"}
    assert inventory["link"] == ("symlink", str(outside))
    assert inventory["subdir"] == ("dir", "")
    assert inventory["subdir/image.png"] == ("file", hashlib.sha256(b"PNG").hexdigest())
    guard = snapshots.RefsGuard.capture()
    (outside / "private").write_bytes(b"different outside")
    assert guard.changed_paths() == []


def test_ib42_root_symlink_is_not_followed(repo: Path) -> None:
    (repo / "design").mkdir()
    (repo / "design/refs").symlink_to(repo, target_is_directory=True)
    assert snapshots.refs_inventory(repo / "design/refs") == {".": ("symlink", str(repo))}


def test_ib42_report_is_bounded(repo: Path) -> None:
    refs = repo / "design/refs"
    refs.mkdir(parents=True)
    guard = snapshots.RefsGuard.capture()
    for number in range(25):
        (refs / f"{number:02}.png").write_bytes(b"PNG")
    message = guard.failure_message()
    assert message is not None
    assert "design/refs/00.png" in message and "design/refs/19.png" in message
    assert "design/refs/20.png" not in message
    assert "ещё 5" in message


def test_ib42_sessionfinish_fails_and_reports_changed_refs(repo: Path) -> None:
    import conftest

    guard = snapshots.RefsGuard.capture()
    (repo / "design/refs").mkdir(parents=True)
    reporter = Mock()
    stash = pytest.Stash()
    stash[conftest._REFS_GUARD] = guard
    session = SimpleNamespace(
        config=SimpleNamespace(stash=stash, pluginmanager=Mock()), exitstatus=pytest.ExitCode.OK
    )
    session.config.pluginmanager.get_plugin.return_value = reporter
    conftest.pytest_sessionfinish(cast(pytest.Session, session))
    assert session.exitstatus == pytest.ExitCode.TESTS_FAILED
    assert "IB42" in reporter.write_sep.call_args.args[1]
