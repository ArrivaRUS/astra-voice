"""US-8.6 step A: attack tests derived from T1 ИБ-67…ИБ-76, no live Fly."""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import logging
import os
import pwd
import stat
import struct
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace
from typing import Any

import pytest

from astra_voice.platform.fly_tray import FlyTrayError, FlyTrayPriority
from helpers.fly_deadline import fifo_deadline
from helpers.fly_user import REAL_GETUID
from helpers.fly_user import fly_user as fly_user

pytestmark = pytest.mark.unit


@pytest.fixture
def backend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FlyTrayPriority:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(pwd, "getpwuid", lambda uid: SimpleNamespace(pw_dir=str(tmp_path)))
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


def owner_sectioned_themerc(*, newline: bytes = b"\n", bom: bytes = b"") -> bytes:
    """Synthetic Fly 1.8.4 shape confirmed on 27.10; priority is line 344."""
    lines = [b"[General]", b"Name=keep", b"[Other]", b"Other=unchanged"]
    lines.extend([b"# padding"] * (342 - len(lines)))
    lines.extend(
        [
            b"[Variables]",
            b"TrayPriorityList=fly-notifications:qbat:fly-vkbd:",
            b"TaskbarColor=#123456",
            b"[AfterPriority]",
            b"Other2=keep",
        ]
    )
    assert lines[343].startswith(b"TrayPriorityList=")
    return bom + newline.join(lines) + newline


@pytest.mark.parametrize("newline", [b"\n", b"\r\n"], ids=["lf", "crlf"])
@pytest.mark.parametrize("bom", [b"", b"\xef\xbb\xbf"], ids=["plain", "bom"])
def test_ib67_confirmed_variables_owner_shape_byte_exact_roundtrip(
    backend: FlyTrayPriority, newline: bytes, bom: bytes
) -> None:
    original = owner_sectioned_themerc(newline=newline, bom=bom)
    expected = original.replace(b"fly-vkbd:", b"fly-vkbd:astra-voice:")
    backend.themerc.write_bytes(original)
    original_hash = hashlib.sha256(original).hexdigest()
    assert backend.status().kind == "absent"
    applied = backend.apply()
    assert applied.ok and applied.changed
    assert backend.themerc.read_bytes() == expected
    assert backend.status().kind == "present_ours"
    reverted = backend.revert()
    assert reverted.ok and reverted.changed
    assert hashlib.sha256(backend.themerc.read_bytes()).hexdigest() == original_hash
    assert not backend.state_path.exists()


def test_ib74_confirmed_variables_foreign_then_absent(backend: FlyTrayPriority) -> None:
    original = owner_sectioned_themerc()
    foreign = original.replace(b"fly-vkbd:", b"fly-vkbd:astra-voice:")
    backend.themerc.write_bytes(foreign)
    status = backend.status()
    assert (status.kind, status.code) == ("present_foreign", "foreign")
    result = backend.apply()
    assert result.ok and not result.changed
    assert backend.themerc.read_bytes() == foreign
    assert not backend.state_path.exists()
    backend.themerc.write_bytes(original)
    assert backend.status().kind == "absent"


@pytest.mark.parametrize("policy", [b"UseSystemTray=false", b"TrayBlackList=astra:"])
def test_ib74_confirmed_variables_keeps_administrator_hiding_policy(
    backend: FlyTrayPriority, policy: bytes
) -> None:
    source = b"[Variables]\nTrayPriorityList=one:\n" + policy + b"\n"
    backend.themerc.write_bytes(source)
    before = identity(backend.themerc)
    assert backend.status().code == "hidden"
    assert not backend.apply().changed
    assert identity(backend.themerc) == before
    assert not backend.backups_dir.exists()


@pytest.mark.parametrize(
    "header",
    [b"[Tray]", b"[General]", b"[unknown]", b"[ Variables ]", b"[variables]", b"[Variables] "],
)
def test_ib67_unconfirmed_priority_section_never_writes(
    backend: FlyTrayPriority, header: bytes
) -> None:
    backend.themerc.write_bytes(header + b"\nTrayPriorityList=one:\n")
    before = identity(backend.themerc)
    assert backend.status().code == "section"
    with pytest.raises(FlyTrayError, match="section"):
        backend.prepare("show")
    assert identity(backend.themerc) == before
    assert not backend.backups_dir.exists()


@pytest.mark.parametrize(
    "source",
    [
        b"[Variables]\nTrayPriorityList=one:\n[Tray]\nTrayPriorityList=two:\n",
        b"[Tray]\nTrayPriorityList=one:\n[Variables]\nTrayPriorityList=two:\n",
    ],
)
def test_ib67_duplicate_priority_across_sections_is_ambiguous(
    backend: FlyTrayPriority, source: bytes
) -> None:
    backend.themerc.write_bytes(source)
    before = identity(backend.themerc)
    assert backend.status().code == "ambiguous"
    with pytest.raises(FlyTrayError, match="ambiguous"):
        backend.prepare("show")
    assert identity(backend.themerc) == before
    assert not backend.backups_dir.exists()


@pytest.mark.parametrize(
    "source",
    [
        b"[General]\nOther=keep\n \t[Variables]\nTrayPriorityList=one:\n",
        b"[Variables]\n# [Tray]\n; [General]\nTrayPriorityList=one:\n[Tray]\n",
        b"TrayPriorityList=one:\n[Tray]\nOther=keep\n",
    ],
)
def test_ib67_only_last_real_header_before_priority_controls_section(
    backend: FlyTrayPriority, source: bytes
) -> None:
    backend.themerc.write_bytes(source)
    assert backend.status().kind == "absent"
    assert backend.apply().changed
    assert backend.themerc.read_bytes() == source.replace(b"one:", b"one:astra-voice:")
    assert backend.revert().changed
    assert backend.themerc.read_bytes() == source


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
    with fifo_deadline():
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
        backend.themerc.write_bytes(backend.themerc.read_bytes() + f"#change{index}\n".encode())
        original = backend.themerc.read_bytes()
        previous = set(backend.backups_dir.glob("current.themerc.*"))
        assert perform(backend, "show" if index % 2 == 0 else "revert").ok
        copies = list(backend.backups_dir.glob("current.themerc.*"))
        assert len(copies) == min(index + 1, 5)
        created = set(copies) - previous
        assert len(created) == 1
        assert (
            hashlib.sha256(created.pop().read_bytes()).digest() == hashlib.sha256(original).digest()
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
    link = backend.backups_dir / ("current.themerc.20000101T000000Z." + "a" * 16)
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
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
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


@pytest.mark.parametrize("setting", [b"UseSystemTray=false", b"TrayBlackList=astra-voice:"])
def test_ib74_hidden_by_fly_settings(backend: FlyTrayPriority, setting: bytes) -> None:
    backend.themerc.write_bytes(setting + b"\nTrayPriorityList=fly-vkbd:\n")
    assert backend.status().code == "hidden"
    with pytest.raises(FlyTrayError):
        backend.prepare("show")


def test_ib74_no_tray_apprc(backend: FlyTrayPriority) -> None:
    (Path.home() / ".fly/apprc").write_bytes(b"astra-voice NO_TRAY\n")
    assert backend.status().code == "hidden"


@pytest.mark.parametrize(
    ("rules", "hidden"),
    [
        (b"astra:", True),
        (b"other:astra:unrelated:", True),
        (b"ASTRA:", True),
        (b"astra-voice-foo-other:", False),
        (b"other-astra-voice-app:", False),
        (b"::", False),
    ],
)
def test_review4_blacklist_rule_matches_inside_our_class(
    backend: FlyTrayPriority, rules: bytes, hidden: bool
) -> None:
    backend.themerc.write_bytes(b"TrayBlackList=" + rules + b"\nTrayPriorityList=fly-vkbd:\n")
    before = identity(backend.themerc)
    assert (backend.status().code == "hidden") is hidden
    if hidden:
        assert not backend.apply().ok
    assert identity(backend.themerc) == before


@pytest.mark.parametrize(
    ("rules", "hidden"),
    [
        (b"astra NO_TRAY\n", True),
        (b"ASTRA NO_TRAY\r\n", True),
        (b"astra NO_FOCUS NO_TRAY\n", True),
        (b"astra-voice-foo-other NO_TRAY\n", False),
        (b"other NO_TRAY astra-voice\n", False),
        (b"astra-voice NOT_NO_TRAY\n", False),
        (b"astra-voice NO_FOCUS # NO_TRAY\n", False),
        (b"# astra NO_TRAY\nastra NO_FOCUS\nother NO_TRAY\n", False),
    ],
)
def test_review4_apprc_matches_class_rule_and_exact_flag(
    backend: FlyTrayPriority, rules: bytes, hidden: bool
) -> None:
    (Path.home() / ".fly/apprc").write_bytes(rules)
    before = identity(backend.themerc)
    assert (backend.status().code == "hidden") is hidden
    if hidden:
        assert not backend.apply().ok
    assert identity(backend.themerc) == before


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
    if not late:
        expected = source
    elif not source:
        expected = late[1:]
    else:
        expected = source.rstrip(b":") + late
    assert backend.themerc.read_bytes() == b"TrayPriorityList=" + expected + b"\n"


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


@pytest.mark.parametrize("action", ["show", "revert"])
def test_ib69_state_directory_fsync_failure_restores_provenance(
    backend: FlyTrayPriority, monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    if action == "revert":
        assert backend.apply().ok
    original = backend.themerc.read_bytes()
    previous_state = backend.state_path.read_bytes() if backend.state_path.exists() else None
    real_fsync = os.fsync
    fired = False

    def fail(fd: int) -> None:
        nonlocal fired
        real_fsync(fd)
        if (
            not fired
            and backend.state_path.parent.exists()
            and os.fstat(fd).st_ino == backend.state_path.parent.stat().st_ino
            and backend.themerc.read_bytes() != original
        ):
            fired = True
            raise OSError(errno.EIO, "state fsync failed")

    monkeypatch.setattr(os, "fsync", fail)
    assert not perform(backend, action).ok
    assert fired and backend.themerc.read_bytes() == original
    assert (
        backend.state_path.read_bytes() if backend.state_path.exists() else None
    ) == previous_state
    assert backend.status().kind == ("present_ours" if action == "revert" else "absent")


def test_ib69_temp_replaced_after_creation_refuses(
    backend: FlyTrayPriority, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = backend.themerc.read_bytes()
    victim = Path.home() / "victim"
    victim.write_bytes(b"keep")
    real_fsync = os.fsync
    fired = False

    def swap(fd: int) -> None:
        nonlocal fired
        real_fsync(fd)
        files = list(backend.themerc.parent.glob(".current.themerc.astra-voice.*.tmp"))
        if not fired and files:
            fired = True
            files[0].unlink()
            files[0].symlink_to(victim)

    monkeypatch.setattr(os, "fsync", swap)
    assert not backend.apply().ok
    assert fired and not backend.themerc.is_symlink()
    assert backend.themerc.read_bytes() == original and victim.read_bytes() == b"keep"


@pytest.mark.parametrize("boundary", range(1, 10))
def test_ib69_every_fsync_boundary_is_recoverable(
    backend: FlyTrayPriority, monkeypatch: pytest.MonkeyPatch, boundary: int
) -> None:
    original = backend.themerc.read_bytes()
    real_fsync = os.fsync
    calls = 0

    def fail_once(fd: int) -> None:
        nonlocal calls
        calls += 1
        if calls == boundary:
            raise OSError(errno.EIO, "injected")
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fail_once)
    assert not backend.apply().ok
    assert calls >= boundary
    assert backend.themerc.read_bytes() == original
    assert not backend.state_path.exists()
    assert not list(backend.themerc.parent.glob(".*.tmp"))
    assert not list(backend.state_path.parent.glob(".*.tmp"))


@pytest.mark.parametrize("existing", [False, True])
def test_ib69_independent_instances_serialized(backend: FlyTrayPriority, existing: bool) -> None:
    if existing:
        assert backend.apply().ok
    other = FlyTrayPriority()
    first = backend.prepare("show")
    second = other.prepare("revert" if existing else "show")
    barrier = Barrier(2)

    def run(pair: tuple[FlyTrayPriority, str]) -> Any:
        barrier.wait(timeout=2)
        return pair[0].confirm(pair[1])

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, [(backend, first), (other, second)]))
    assert sum(result.changed for result in results) == 1
    assert backend.status().kind == ("absent" if existing else "present_ours")


def test_ib68_other_supplementary_group_preserved(backend: FlyTrayPriority) -> None:
    groups = [group for group in os.getgroups() if group != os.getgid()]
    if not groups:
        pytest.skip("No supplementary group available to this uid")
    try:
        os.chown(backend.themerc, -1, groups[0])
    except OSError as error:
        if error.errno in {errno.EINVAL, errno.EPERM}:
            pytest.skip("Supplementary gid is not mapped/available in this sandbox")
        raise
    assert backend.apply().ok
    assert backend.themerc.stat().st_gid == groups[0]


def _acl() -> bytes:
    # Linux POSIX ACL xattr, owner=rw, named user=r, group=r, mask=r, other=none.
    entries = [
        (1, 6, 0xFFFFFFFF),
        # This ACL is passed to the real kernel, not the fixture's logical uid.
        # unshare -r maps only uid 0; a synthetic uid 1000 would be unmapped.
        (2, 4, REAL_GETUID()),
        (4, 4, 0xFFFFFFFF),
        (16, 4, 0xFFFFFFFF),
        (32, 0, 0xFFFFFFFF),
    ]
    return struct.pack("<I", 2) + b"".join(struct.pack("<HHI", *entry) for entry in entries)


@pytest.mark.parametrize("default_acl", [False, True])
def test_ib68_real_posix_acl_and_inherited_default_acl(
    backend: FlyTrayPriority, default_acl: bool
) -> None:
    attribute = "system.posix_acl_default" if default_acl else "system.posix_acl_access"
    target = backend.themerc.parent if default_acl else backend.themerc
    try:
        os.setxattr(target, attribute, _acl())
    except OSError as error:
        if error.errno in {errno.ENOTSUP, errno.EOPNOTSUPP}:
            pytest.skip("Filesystem does not support POSIX ACL")
        raise
    names = os.listxattr(backend.themerc)
    before = {name: os.getxattr(backend.themerc, name) for name in names}
    mode = stat.S_IMODE(backend.themerc.stat().st_mode)
    assert backend.apply().ok
    after = {name: os.getxattr(backend.themerc, name) for name in os.listxattr(backend.themerc)}
    assert after == before and stat.S_IMODE(backend.themerc.stat().st_mode) == mode


def test_ib69_foreign_metadata_after_replace_not_rolled_back(
    backend: FlyTrayPriority, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_fsync = os.fsync
    fired = False

    def mutate(fd: int) -> None:
        nonlocal fired
        real_fsync(fd)
        if (
            not fired
            and stat.S_ISDIR(os.fstat(fd).st_mode)
            and b"astra-voice" in backend.themerc.read_bytes()
        ):
            fired = True
            os.setxattr(backend.themerc, "user.foreign", b"keep")

    monkeypatch.setattr(os, "fsync", mutate)
    assert not backend.apply().ok
    assert fired and os.getxattr(backend.themerc, "user.foreign") == b"keep"


@pytest.mark.parametrize("setting", [b"UseSystemTray=false", b"TrayBlackList=astra-voice:"])
def test_ib74_bom_does_not_hide_administrator_setting(
    backend: FlyTrayPriority, setting: bytes
) -> None:
    backend.themerc.write_bytes(b"\xef\xbb\xbf" + setting + b"\nTrayPriorityList=fly-vkbd:\n")
    original = identity(backend.themerc)
    assert backend.status().code == "hidden"
    assert not backend.apply().ok
    assert identity(backend.themerc) == original


def test_ib69_flock_blocks_an_independent_instance(backend: FlyTrayPriority) -> None:
    backend.backups_dir.mkdir(parents=True, mode=0o700)
    lock = backend.backups_dir / "fly-theme.lock"
    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        before = identity(backend.themerc)
        result = FlyTrayPriority().apply()
        assert not result.ok and result.code == "busy"
        assert identity(backend.themerc) == before and not backend.state_path.exists()
    finally:
        os.close(fd)
    assert FlyTrayPriority().apply().ok


@pytest.mark.parametrize("kind", ["symlink", "fifo", "hardlink", "mode"])
def test_ib69_untrusted_lock_refuses(backend: FlyTrayPriority, kind: str) -> None:
    backend.backups_dir.mkdir(parents=True, mode=0o700)
    lock = backend.backups_dir / "fly-theme.lock"
    victim = Path.home() / "victim"
    victim.write_bytes(b"keep")
    if kind == "symlink":
        lock.symlink_to(victim)
    elif kind == "fifo":
        os.mkfifo(lock)
    elif kind == "hardlink":
        os.link(victim, lock)
    else:
        lock.write_bytes(b"")
        lock.chmod(0o666)
    before = identity(backend.themerc)
    assert not backend.apply().ok
    assert identity(backend.themerc) == before and victim.read_bytes() == b"keep"


def test_ib75_failure_does_not_log_exception_text(
    backend: FlyTrayPriority, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def fail(*args: Any, **kwargs: Any) -> None:
        raise OSError(errno.ENOSPC, "PRIVATE_EXCEPTION_SECRET /private/source")

    monkeypatch.setattr(os, "fsync", fail)
    with caplog.at_level(logging.DEBUG):
        assert not backend.apply().ok
    assert "PRIVATE_EXCEPTION_SECRET" not in caplog.text and "/private/source" not in caplog.text
    assert "errno=28" in caplog.text


@pytest.mark.parametrize("write_number", [2, 3, 4])
@pytest.mark.parametrize("failure", ["raise", "zero"])
def test_ib69_temp_and_state_write_failure(
    backend: FlyTrayPriority, monkeypatch: pytest.MonkeyPatch, write_number: int, failure: str
) -> None:
    original = backend.themerc.read_bytes()
    real_write = os.write
    calls = 0

    def fail_once(fd: int, data: Any) -> int:
        nonlocal calls
        calls += 1
        if calls == write_number:
            if failure == "zero":
                return 0
            raise OSError(errno.EIO, "scoped write")
        return real_write(fd, data)

    monkeypatch.setattr(os, "write", fail_once)
    assert not backend.apply().ok
    assert calls >= write_number and backend.themerc.read_bytes() == original
    assert not backend.state_path.exists()
    assert not list(backend.themerc.parent.glob(".*.tmp"))
    assert not list(backend.state_path.parent.glob(".*.tmp"))


def test_ib69_short_writes_complete_without_truncation(
    backend: FlyTrayPriority, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = backend.themerc.read_bytes()
    real_write = os.write

    def short_write(fd: int, data: Any) -> int:
        return real_write(fd, data[:3])

    monkeypatch.setattr(os, "write", short_write)
    added = backend.apply()
    assert added.ok and added.changed
    assert backend.themerc.read_bytes() == original.replace(b"fly-vkbd:", b"fly-vkbd:astra-voice:")
    assert json.loads(backend.state_path.read_bytes()) == {
        "added": True,
        "inserted": "astra-voice:",
    }
    removed = backend.revert()
    assert removed.ok and removed.changed and not backend.state_path.exists()
    assert backend.themerc.read_bytes() == original


def test_ib69_state_replace_failure_restores_source(
    backend: FlyTrayPriority, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = backend.themerc.read_bytes()
    real_replace = os.replace

    def fail_state(src: Any, dst: Any, **kwargs: Any) -> None:
        if dst == backend.state_path.name:
            raise OSError(errno.EIO, "state replace")
        real_replace(src, dst, **kwargs)

    monkeypatch.setattr(os, "replace", fail_state)
    assert not backend.apply().ok
    assert backend.themerc.read_bytes() == original and not backend.state_path.exists()


def test_ib68_supplementary_group_copy_with_mocked_kernel(
    backend: FlyTrayPriority, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_inode = backend.themerc.stat().st_ino
    real_stat = os.fstat
    target_gid = os.getgid() + 1
    copied: set[int] = set()

    def group_stat(fd: int) -> Any:
        info = real_stat(fd)
        if info.st_ino == original_inode or info.st_ino in copied:
            values = {
                name: getattr(info, name)
                for name in dir(info)
                if name.startswith("st_") and name != "st_gid"
            }
            return SimpleNamespace(**values, st_gid=target_gid)
        return info

    def chown(fd: int, uid: int, gid: int) -> None:
        assert uid == os.getuid() and gid == target_gid
        copied.add(real_stat(fd).st_ino)

    monkeypatch.setattr(os, "fstat", group_stat)
    monkeypatch.setattr(os, "fchown", chown)
    assert backend.apply().ok
    assert backend.themerc.stat().st_ino in copied


@pytest.mark.parametrize("elapsed", [59.999, 60.0])
def test_ib74_exact_operation_expiration(
    backend: FlyTrayPriority, monkeypatch: pytest.MonkeyPatch, elapsed: float
) -> None:
    clock = [10.0]
    with monkeypatch.context() as patch:
        patch.setattr(time, "monotonic", lambda: clock[0])
        op = backend.prepare("show")
        clock[0] += elapsed
        result = backend.confirm(op)
    assert result.ok == (elapsed < 60.0)


def test_ib74_same_content_new_inode_invalidates_prepare(backend: FlyTrayPriority) -> None:
    op = backend.prepare("show")
    replacement = backend.themerc.with_name("replacement")
    replacement.write_bytes(backend.themerc.read_bytes())
    replacement.replace(backend.themerc)
    before = identity(backend.themerc)
    assert backend.confirm(op).code == "changed"
    assert identity(backend.themerc) == before


@pytest.mark.parametrize("overflow", [0, 1])
def test_ib67_line_limit_includes_inserted_token(backend: FlyTrayPriority, overflow: int) -> None:
    prefix = b"TrayPriorityList="
    value_size = 8192 + overflow - len(prefix) - len(b":astra-voice") - 1
    original = prefix + b"a" * value_size + b"\n"
    backend.themerc.write_bytes(original)
    before = identity(backend.themerc)
    result = backend.apply()
    assert result.ok == (overflow == 0)
    if overflow:
        assert result.code == "too-large" and identity(backend.themerc) == before
        assert not backend.backups_dir.exists()
    else:
        assert len(backend.themerc.read_bytes()) == 8192
        assert backend.revert().ok and backend.themerc.read_bytes() == original


@pytest.mark.parametrize("action", ["show", "revert"])
@pytest.mark.parametrize("fail_at", ["theme", "state"])
def test_ib69_persistent_enospc_after_theme_commit_needs_no_rollback_write(
    backend: FlyTrayPriority, monkeypatch: pytest.MonkeyPatch, action: str, fail_at: str
) -> None:
    if action == "revert":
        assert backend.apply().ok
    original = backend.themerc.read_bytes()
    old_state = backend.state_path.read_bytes() if backend.state_path.exists() else None
    real_write, real_fsync = os.write, os.fsync
    exhausted = False

    def full(fd: int, data: Any) -> int:
        if exhausted:
            raise OSError(errno.ENOSPC, "persistent disk full")
        return real_write(fd, data)

    def fail_after_commit(fd: int) -> None:
        nonlocal exhausted
        target = backend.themerc.parent if fail_at == "theme" else backend.state_path.parent
        if (
            stat.S_ISDIR(os.fstat(fd).st_mode)
            and os.fstat(fd).st_ino == target.stat().st_ino
            and backend.themerc.read_bytes() != original
        ):
            exhausted = True
            raise OSError(errno.ENOSPC, "persistent fsync failure")
        real_fsync(fd)

    monkeypatch.setattr(os, "write", full)
    monkeypatch.setattr(os, "fsync", fail_after_commit)
    assert not perform(backend, action).ok
    assert exhausted and backend.themerc.read_bytes() == original
    assert (backend.state_path.read_bytes() if backend.state_path.exists() else None) == old_state
