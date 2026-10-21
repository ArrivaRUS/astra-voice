"""US-8.6 step A: attack tests derived from T1 ИБ-67…ИБ-76, no live Fly."""

from __future__ import annotations

import errno
import hashlib
import json
import logging
import os
import stat
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from astra_voice.platform import fly_tray
from astra_voice.platform.fly_tray import FlyTrayError, FlyTrayPriority

pytestmark = pytest.mark.unit


@pytest.fixture
def backend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FlyTrayPriority:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(fly_tray.pwd, "getpwuid", lambda uid: SimpleNamespace(pw_dir=str(tmp_path)))
    theme = tmp_path / ".fly/theme/current.themerc"
    theme.parent.mkdir(parents=True, mode=0o700)
    theme.write_bytes(b"TaskbarColor=#123456\nTrayPriorityList=fly-notifications:qbat:fly-vkbd:\n")
    theme.chmod(0o644)
    return FlyTrayPriority()


def perform(backend: FlyTrayPriority, action: str = "show") -> Any:
    return backend.apply() if action == "show" else backend.revert()


def identity(path: Path) -> tuple[bytes, int, int]:
    info = path.stat()
    return path.read_bytes(), info.st_ino, info.st_mtime_ns


@pytest.mark.parametrize(
    ("source", "shown"),
    [
        (b"TrayPriorityList=one:fly-vkbd:\n", b"TrayPriorityList=one:fly-vkbd:astra-voice:\n"),
        (b"TrayPriorityList=one:fly-vkbd\n", b"TrayPriorityList=one:fly-vkbd:astra-voice\n"),
        (b"TrayPriorityList=\n", b"TrayPriorityList=astra-voice:\n"),
        (b"TrayPriorityList=one:\r\n", b"TrayPriorityList=one:astra-voice:\r\n"),
        (b"TrayPriorityList=one", b"TrayPriorityList=one:astra-voice"),
        (b" \tTrayPriorityList \t= one: \t\n", b" \tTrayPriorityList \t= one:astra-voice: \t\n"),
        (
            b"\xef\xbb\xbfTrayPriorityList=one:\n",
            b"\xef\xbb\xbfTrayPriorityList=one:astra-voice:\n",
        ),
        (
            b"TrayPriorityList=other-astra-voice\n",
            b"TrayPriorityList=other-astra-voice:astra-voice\n",
        ),
        (
            b"#TrayPriorityList=comment\nTrayPriorityList=one:\n",
            b"#TrayPriorityList=comment\nTrayPriorityList=one:astra-voice:\n",
        ),
    ],
)
def test_ib67_byte_exact_roundtrip(backend: FlyTrayPriority, source: bytes, shown: bytes) -> None:
    prefix, suffix = b"#\xd4\xc5\xcd\xc1\r\nOther=keep\n", b"Other2=unchanged\n"
    # A last line without LF must really be last.
    bom = b"\xef\xbb\xbf" if source.startswith(b"\xef\xbb\xbf") else b""
    original = bom + prefix + source[len(bom) :] + (suffix if source.endswith(b"\n") else b"")
    expected = bom + prefix + shown[len(bom) :] + (suffix if source.endswith(b"\n") else b"")
    backend.themerc.write_bytes(original)
    assert backend.status().kind == "absent"
    result = perform(backend)
    assert result.ok and result.changed
    assert backend.themerc.read_bytes() == expected
    assert backend.status().kind == "present_ours"
    state = json.loads(backend.state_path.read_bytes())
    assert state["added"] is True and state["inserted"]
    assert stat.S_IMODE(backend.state_path.stat().st_mode) == 0o600
    assert perform(backend, "revert").ok
    assert backend.themerc.read_bytes() == original
    assert not backend.state_path.exists()


@pytest.mark.parametrize(
    "source",
    [
        b"Other=only\n",
        b"#TrayPriorityList=one\n",
        b";TrayPriorityList=one\n",
        b"TrayPriorityList=a\nTrayPriorityList=b\n",
        b"[one]\nTrayPriorityList=a\n[two]\nTrayPriorityList=b\n",
        b"[unknown]\nTrayPriorityList=a\n",
        b'TrayPriorityList="a:"\n',
        b"TrayPriorityList=a#b\n",
        b"TrayPriorityList=a;b\n",
        b"TrayPriorityList=a\x00b\n",
        b"TrayPriorityList=a\x01b\n",
        b"TrayPriorityList=a\\\n",
        b"TrayPriorityList=" + b"a" * 9000 + b"\n",
        b"TrayPriorityList=a::b\n",
        b"TrayPriorityList=a: b\n",
        b"TrayPriorityList=a\rb\n",
    ],
)
def test_ib67_ambiguous_grammar_never_writes(backend: FlyTrayPriority, source: bytes) -> None:
    backend.themerc.write_bytes(source)
    before = identity(backend.themerc)
    assert backend.status().kind == "unavailable"
    with pytest.raises(FlyTrayError):
        backend.prepare("show")
    assert identity(backend.themerc) == before
    assert not backend.backups_dir.exists()


@pytest.mark.parametrize(
    "kind",
    [
        "symlink",
        "zero",
        "kiosk",
        "fifo",
        "directory",
        "hardlink",
        "large",
        "empty",
        "readonly",
        "missing",
    ],
)
def test_ib68_unsafe_file(backend: FlyTrayPriority, kind: str) -> None:
    path = backend.themerc
    victim = path.parent / "default.themerc.fly-kiosk"
    victim.write_bytes(b"victim")
    path.unlink()
    if kind in {"symlink", "zero", "kiosk"}:
        path.symlink_to("/dev/zero" if kind == "zero" else victim)
    elif kind == "fifo":
        os.mkfifo(path)
    elif kind == "directory":
        path.mkdir()
    elif kind == "hardlink":
        os.link(victim, path)
    elif kind == "large":
        with path.open("wb") as stream:
            stream.truncate(2 * 1024**3)
    elif kind in {"empty", "readonly"}:
        path.write_bytes(b"" if kind == "empty" else b"TrayPriorityList=a:\n")
        if kind == "readonly":
            path.chmod(0o444)
    start = time.monotonic()
    status = backend.status()
    assert time.monotonic() - start < 0.1
    assert status.kind == "unavailable"
    assert str(path) not in status.message
    with pytest.raises(FlyTrayError):
        backend.prepare("show")
    assert victim.read_bytes() == b"victim"
    assert not backend.backups_dir.exists()


@pytest.mark.parametrize("parent", [".fly", ".fly/theme"])
@pytest.mark.parametrize("kind", ["symlink", "world-writable"])
def test_ib68_unsafe_theme_ancestor(backend: FlyTrayPriority, parent: str, kind: str) -> None:
    path = Path.home() / parent
    if kind == "symlink":
        moved = path.with_name(path.name + "-real")
        path.rename(moved)
        path.symlink_to(moved, target_is_directory=True)
    else:
        path.chmod(0o777)
    assert backend.status().kind == "unavailable"


@pytest.mark.parametrize("kind", ["home", "root", "euid", "owner", "write-probe"])
def test_ib68_identity_and_administrator_policy(
    backend: FlyTrayPriority, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    if kind == "home":
        monkeypatch.setenv("HOME", str(Path.home() / "wrong"))
    elif kind == "root":
        monkeypatch.setattr(os, "getuid", lambda: 0)
        monkeypatch.setattr(os, "geteuid", lambda: 0)
    elif kind == "euid":
        monkeypatch.setattr(os, "geteuid", lambda: os.getuid() + 1)
    elif kind == "owner":
        real_stat = os.fstat

        def foreign(fd: int) -> Any:
            info = real_stat(fd)
            if stat.S_ISREG(info.st_mode):
                return SimpleNamespace(
                    **{
                        name: getattr(info, name)
                        for name in dir(info)
                        if name.startswith("st_") and name != "st_uid"
                    },
                    st_uid=os.getuid() + 1,
                )
            return info

        monkeypatch.setattr(os, "fstat", foreign)
    else:
        real_open = os.open

        def denied(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
            if str(path) == "current.themerc" and flags & os.O_WRONLY:
                raise PermissionError(errno.EPERM, "private-path-marker")
            return real_open(path, flags, *args, **kwargs)

        monkeypatch.setattr(os, "open", denied)
    assert backend.status().kind == "unavailable"
    assert not backend.backups_dir.exists()


def test_ib68_preserves_mode_group_xattr(backend: FlyTrayPriority) -> None:
    backend.themerc.chmod(0o640)
    os.setxattr(backend.themerc, "user.test", b"label")
    before = backend.themerc.stat()
    assert perform(backend).ok
    after = backend.themerc.stat()
    assert (stat.S_IMODE(after.st_mode), after.st_uid, after.st_gid) == (
        0o640,
        before.st_uid,
        before.st_gid,
    )
    assert os.getxattr(backend.themerc, "user.test") == b"label"


@pytest.mark.parametrize("attribute", ["security.test", "system.posix_acl_access"])
def test_ib68_metadata_copy_failure_refuses(
    backend: FlyTrayPriority, monkeypatch: pytest.MonkeyPatch, attribute: str
) -> None:
    before = identity(backend.themerc)
    monkeypatch.setattr(os, "listxattr", lambda fd: [attribute])
    monkeypatch.setattr(os, "getxattr", lambda fd, key: b"label")

    def denied(*args: Any, **kwargs: Any) -> None:
        raise PermissionError(errno.EPERM, "label")

    monkeypatch.setattr(os, "setxattr", denied)
    assert not perform(backend).ok
    assert identity(backend.themerc) == before
    assert not list(backend.themerc.parent.glob("*.tmp"))


@pytest.mark.parametrize("kind", ["content", "metadata"])
def test_ib69_concurrent_change_before_replace(
    backend: FlyTrayPriority, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    op = backend.prepare("show")
    real_fsync = os.fsync
    changed = False

    def mutate(fd: int) -> None:
        nonlocal changed
        real_fsync(fd)
        if not changed and stat.S_ISREG(os.fstat(fd).st_mode):
            changed = True
            if kind == "content":
                backend.themerc.write_bytes(backend.themerc.read_bytes().replace(b"qbat", b"evil"))
            else:
                os.setxattr(backend.themerc, "user.acl-test", b"changed")

    monkeypatch.setattr(os, "fsync", mutate)
    result = backend.confirm(op)
    assert not result.ok and result.code == "changed"
    if kind == "content":
        assert b"evil" in backend.themerc.read_bytes()
    else:
        assert os.getxattr(backend.themerc, "user.acl-test") == b"changed"
    assert b"astra-voice" not in backend.themerc.read_bytes()


@pytest.mark.parametrize("boundary", ["write", "fsync", "replace"])
def test_ib69_io_failure_is_atomic(
    backend: FlyTrayPriority, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    before = identity(backend.themerc)

    def fail(*args: Any, **kwargs: Any) -> Any:
        raise OSError(errno.EIO, "secret path")

    monkeypatch.setattr(os, boundary, fail)
    assert not perform(backend).ok
    assert identity(backend.themerc) == before
    assert not list(backend.themerc.parent.glob(".*.tmp"))


def test_ib69_parallel_confirm_exactly_one(backend: FlyTrayPriority) -> None:
    op = backend.prepare("show")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(backend.confirm, [op, op]))
    assert sum(result.changed for result in results) == 1
    assert backend.themerc.read_bytes().count(b"astra-voice") == 1


def test_ib70_backup_bytes_modes_and_retention(backend: FlyTrayPriority) -> None:
    for index in range(6):
        original = backend.themerc.read_bytes()
        assert perform(backend, "show" if index % 2 == 0 else "revert").ok
        copies = list(backend.backups_dir.glob("current.themerc.*"))
        assert len(copies) == min(index + 1, 5)
        assert any(
            hashlib.sha256(p.read_bytes()).digest() == hashlib.sha256(original).digest()
            for p in copies
        )
        assert all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in copies)
    assert stat.S_IMODE(backend.backups_dir.stat().st_mode) == 0o700


@pytest.mark.parametrize("kind", ["ancestor-symlink", "mode", "enospc"])
def test_ib70_backup_failure_leaves_source(
    backend: FlyTrayPriority, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    before = identity(backend.themerc)
    if kind == "ancestor-symlink":
        local = Path.home() / ".local"
        target = Path.home() / "local-target"
        target.mkdir()
        local.symlink_to(target, target_is_directory=True)
    elif kind == "mode":
        backend.backups_dir.mkdir(parents=True)
        backend.backups_dir.chmod(0o777)
    else:

        def full(*args: Any, **kwargs: Any) -> Any:
            raise OSError(errno.ENOSPC, "private-marker")

        monkeypatch.setattr(os, "write", full)
    assert not perform(backend).ok
    assert identity(backend.themerc) == before


def test_ib70_retention_never_follows_symlinks(backend: FlyTrayPriority) -> None:
    backend.backups_dir.mkdir(parents=True, mode=0o700)
    victim = Path.home() / "victim"
    victim.write_bytes(b"keep")
    link = backend.backups_dir / "current.themerc.20000101T000000Z.abcd"
    link.symlink_to(victim)
    for index in range(6):
        assert perform(backend, "show" if index % 2 == 0 else "revert").ok
    assert link.is_symlink() and victim.read_bytes() == b"keep"


def test_ib71_revert_preserves_later_foreign_changes(backend: FlyTrayPriority) -> None:
    assert perform(backend).ok
    backend.themerc.write_bytes(
        backend.themerc.read_bytes()
        .replace(b"#123456", b"#abcdef")
        .replace(b"astra-voice:", b"astra-voice:new-app:")
    )
    assert perform(backend, "revert").ok
    assert b"#abcdef" in backend.themerc.read_bytes()
    assert b"fly-vkbd:new-app:" in backend.themerc.read_bytes()
    assert b"astra-voice" not in backend.themerc.read_bytes()


@pytest.mark.parametrize("token", [b"astra-voice", b"ASTRA-VOICE"])
def test_ib71_foreign_token_show_is_noop(backend: FlyTrayPriority, token: bytes) -> None:
    backend.themerc.write_bytes(b"TrayPriorityList=one:" + token + b":\n")
    before = identity(backend.themerc)
    assert backend.status().kind == "present_foreign"
    result = perform(backend)
    assert result.ok and not result.changed
    assert not backend.state_path.exists() and not backend.backups_dir.exists()
    with pytest.raises(FlyTrayError):
        backend.prepare("revert")
    assert identity(backend.themerc) == before


def test_ib71_duplicate_token_refused_and_disappeared_token_clears_state(
    backend: FlyTrayPriority,
) -> None:
    assert perform(backend).ok
    backend.themerc.write_bytes(b"TrayPriorityList=astra-voice:ASTRA-VOICE:\n")
    assert backend.status().kind == "unavailable"
    with pytest.raises(FlyTrayError):
        backend.prepare("revert")
    backend.themerc.write_bytes(b"TrayPriorityList=fly-vkbd:\n")
    assert backend.status().kind == "absent"
    assert perform(backend, "revert").ok
    assert not backend.state_path.exists()


def test_ib72_restart_interface_is_fixed(
    backend: FlyTrayPriority, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", "/fake")
    command = backend.restart_command()
    assert command == ["/usr/bin/fly-wmfunc", "FLYWM_RESTART"]
    command.append("injection")
    assert backend.restart_command() == ["/usr/bin/fly-wmfunc", "FLYWM_RESTART"]


@pytest.mark.parametrize("kind", ["missing", "foreign", "expired", "consumed", "changed"])
def test_ib74_confirmation_bound_to_file_and_single_use(
    backend: FlyTrayPriority, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    clock = [1.0]
    monkeypatch.setattr(fly_tray.time, "monotonic", lambda: clock[0])
    op = "unprepared" if kind == "missing" else backend.prepare("show")
    if kind == "foreign":
        op = "foreign-path-marker"
    elif kind == "expired":
        clock[0] += 61
    elif kind == "consumed":
        assert backend.confirm(op).ok
    elif kind == "changed":
        backend.themerc.write_bytes(backend.themerc.read_bytes().replace(b"qbat", b"evil"))
    before = identity(backend.themerc)
    assert not backend.confirm(op).ok
    assert identity(backend.themerc) == before


@pytest.mark.parametrize(
    "setting", [b"UseSystemTray=false", b"TrayBlackList=other-astra-voice-app:"]
)
def test_ib74_hidden_by_fly_settings(backend: FlyTrayPriority, setting: bytes) -> None:
    backend.themerc.write_bytes(setting + b"\nTrayPriorityList=fly-vkbd:\n")
    assert backend.status().code == "hidden"
    with pytest.raises(FlyTrayError):
        backend.prepare("show")


def test_ib74_no_tray_apprc(backend: FlyTrayPriority) -> None:
    (Path.home() / ".fly/apprc").write_bytes(b"astra-voice NO_TRAY\n")
    assert backend.status().code == "hidden"


def test_ib74_no_background_mutations(backend: FlyTrayPriority) -> None:
    before = identity(backend.themerc)
    for _ in range(3):
        backend.status()
    backend.prepare("show")
    assert identity(backend.themerc) == before
    assert not backend.backups_dir.exists() and not backend.state_path.exists()


def test_ib75_logs_only_codes(backend: FlyTrayPriority, caplog: pytest.LogCaptureFixture) -> None:
    backend.themerc.write_bytes(b"Other=NEIGHBOR_SECRET\nTrayPriorityList=LIST_SECRET:\n")
    with caplog.at_level(logging.DEBUG):
        assert perform(backend).ok
        assert not backend.confirm("ATTACKER_SECRET").ok
    assert "fly_tray" in caplog.text
    for secret in (
        "NEIGHBOR_SECRET",
        "LIST_SECRET",
        "ATTACKER_SECRET",
        str(Path.home()),
        "current.themerc",
    ):
        assert secret not in caplog.text


@pytest.mark.parametrize("token", ["x", "astra-voice;evil", "../evil", ""])
def test_ib67_token_cannot_be_supplied_by_ui(token: str) -> None:
    with pytest.raises(ValueError):
        FlyTrayPriority(token=token)


def test_ib74_runtime_facade_and_persistent_minimal_provenance(backend: FlyTrayPriority) -> None:
    original = backend.themerc.read_bytes()
    assert backend.apply().changed
    state = json.loads(backend.state_path.read_bytes())
    assert state == {"added": True, "inserted": "astra-voice:"}
    another = FlyTrayPriority()
    assert another.status().kind == "present_ours"
    assert another.apply().ok and not another.apply().changed
    assert another.revert().changed
    assert backend.themerc.read_bytes() == original


@pytest.mark.parametrize("source", [b"", b"one", b"one:"])
@pytest.mark.parametrize("late", [b"", b":", b":late", b":late:"])
def test_ib71_revert_later_list_forms(backend: FlyTrayPriority, source: bytes, late: bytes) -> None:
    backend.themerc.write_bytes(b"TrayPriorityList=" + source + b"\n")
    assert perform(backend).ok
    added = backend.themerc.read_bytes().rstrip(b"\n")
    if late:
        added = added.rstrip(b":") + late
    backend.themerc.write_bytes(added + b"\n")
    assert perform(backend, "revert").ok
    assert b"astra-voice" not in backend.themerc.read_bytes()
    if b"late" in late:
        assert b"late" in backend.themerc.read_bytes()


def test_ib69_temp_name_collision_never_unlinks_foreign_link(
    backend: FlyTrayPriority, monkeypatch: pytest.MonkeyPatch
) -> None:
    op = backend.prepare("show")
    victim = Path.home() / "victim"
    victim.write_bytes(b"keep")
    before = identity(backend.themerc)
    real_open = os.open

    def collision(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        if str(path).startswith(".current.themerc.astra-voice."):
            os.symlink(str(victim), path, dir_fd=kwargs["dir_fd"])
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", collision)
    assert not backend.confirm(op).ok
    assert victim.read_bytes() == b"keep"
    assert identity(backend.themerc) == before
    assert next(backend.themerc.parent.glob(".*.tmp")).is_symlink()


@pytest.mark.parametrize("foreign", [False, True])
def test_ib69_postreplace_verify_failure_only_restores_our_output(
    backend: FlyTrayPriority, monkeypatch: pytest.MonkeyPatch, foreign: bool
) -> None:
    original = backend.themerc.read_bytes()
    real_fsync = os.fsync
    triggered = False

    def fail_after_replace(fd: int) -> None:
        nonlocal triggered
        real_fsync(fd)
        if (
            not triggered
            and stat.S_ISDIR(os.fstat(fd).st_mode)
            and b"astra-voice" in backend.themerc.read_bytes()
        ):
            triggered = True
            if foreign:
                backend.themerc.write_bytes(b"TrayPriorityList=FOREIGN:\n")
            raise OSError(errno.EIO, "verify failed")

    monkeypatch.setattr(os, "fsync", fail_after_replace)
    assert not perform(backend).ok
    assert triggered
    assert backend.themerc.read_bytes() == (b"TrayPriorityList=FOREIGN:\n" if foreign else original)


def test_ib69_crash_leftovers_cleanup_only_own_regular_files(backend: FlyTrayPriority) -> None:
    folder = backend.themerc.parent
    stale = folder / (".current.themerc.astra-voice." + "a" * 32 + ".tmp")
    stale.write_bytes(b"stale")
    link = folder / (".current.themerc.astra-voice." + "b" * 32 + ".tmp")
    link.symlink_to(stale)
    unrelated = folder / ".some-other.tmp"
    unrelated.write_bytes(b"keep")
    assert perform(backend).ok
    assert not stale.exists() and link.is_symlink() and unrelated.read_bytes() == b"keep"


@pytest.mark.parametrize("kind", ["symlink", "fifo", "bad-json", "wrong-mode", "false"])
def test_ib71_untrusted_provenance_refuses(backend: FlyTrayPriority, kind: str) -> None:
    assert perform(backend).ok
    state = backend.state_path
    if kind in {"symlink", "fifo"}:
        state.unlink()
        if kind == "symlink":
            state.symlink_to(backend.themerc)
        else:
            os.mkfifo(state)
    elif kind == "bad-json":
        state.write_bytes(b"{")
    elif kind == "wrong-mode":
        state.chmod(0o644)
    else:
        state.write_bytes(b'{"added":false,"inserted":"astra-voice:"}')
    before = identity(backend.themerc)
    assert backend.status().kind == "unavailable"
    with pytest.raises(FlyTrayError):
        backend.prepare("revert")
    assert identity(backend.themerc) == before
