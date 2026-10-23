"""The restart-loop guard never follows user-controlled links or shares provenance."""

from __future__ import annotations

import json
import os
import pwd
import stat
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from astra_voice.platform.fly_themerc import ThemercError
from astra_voice.platform.fly_tray_loop import FlyTrayLoopGuard

pytestmark = pytest.mark.unit


@pytest.fixture
def guard(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FlyTrayLoopGuard:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(pwd, "getpwuid", lambda uid: SimpleNamespace(pw_dir=str(tmp_path)))
    return FlyTrayLoopGuard(tmp_path / "private/fly-tray.json")


def test_count_persists_and_blocks_fourth_then_explicit_reset(guard: FlyTrayLoopGuard) -> None:
    for expected in (True, True, True, False, False):
        assert FlyTrayLoopGuard(guard.path).allow(True) is expected
    assert json.loads(guard.path.read_bytes()) == {
        "count": 4,
        "blocked": True,
        "restart_in_progress": False,
    }
    assert stat.S_IMODE(guard.path.stat().st_mode) == 0o600
    guard.reset()
    assert guard.allow(False)
    assert json.loads(guard.path.read_bytes()) == {
        "count": 0,
        "blocked": False,
        "restart_in_progress": False,
    }


@pytest.mark.parametrize("attack", ["symlink", "hardlink", "fifo", "mode", "large", "json"])
def test_unsafe_files_fail_closed(guard: FlyTrayLoopGuard, attack: str) -> None:
    guard.path.parent.mkdir(mode=0o700)
    target = guard.path.parent / "target"
    target.write_bytes(b"untouched")
    if attack == "symlink":
        guard.path.symlink_to(target)
    elif attack == "hardlink":
        os.link(target, guard.path)
    elif attack == "fifo":
        os.mkfifo(guard.path, 0o600)
    else:
        guard.path.write_bytes(b"X" * 257 if attack == "large" else b"broken")
        guard.path.chmod(0o644 if attack == "mode" else 0o600)
    with pytest.raises((OSError, ThemercError)):
        guard.allow(True)
    assert target.read_bytes() == b"untouched"


def test_parent_symlink_is_rejected(guard: FlyTrayLoopGuard, tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    guard.path.parent.symlink_to(target, target_is_directory=True)
    with pytest.raises((OSError, ThemercError)):
        guard.reset()
    assert list(target.iterdir()) == []


def test_never_overwrites_backend_provenance(guard: FlyTrayLoopGuard) -> None:
    guard.path.parent.mkdir(mode=0o700)
    provenance = guard.path.with_name("fly-tray.json")
    provenance.write_bytes(b"not-loop-guard-data")
    guard.allow(True)
    guard.reset()
    assert provenance.read_bytes() == b"not-loop-guard-data"


def test_noop_reset_and_check_do_not_create_files(guard: FlyTrayLoopGuard) -> None:
    guard.reset()
    assert guard.allow(False)
    assert not guard.path.parent.exists()


def test_empty_existing_file_fails_closed(guard: FlyTrayLoopGuard) -> None:
    guard.path.parent.mkdir(mode=0o700)
    guard.path.touch(mode=0o600)
    with pytest.raises(ThemercError):
        guard.allow(True)


def test_automatic_present_cannot_clear_sticky_block(guard: FlyTrayLoopGuard) -> None:
    for _ in range(4):
        guard.allow(True)
    assert not guard.allow(False)
    assert not guard.allow(True)
    assert json.loads(guard.path.read_bytes()) == {
        "count": 4,
        "blocked": True,
        "restart_in_progress": False,
    }


def test_present_resets_unblocked_disappearance_streak(guard: FlyTrayLoopGuard) -> None:
    guard.allow(True)
    guard.allow(True)
    assert guard.allow(False)
    assert json.loads(guard.path.read_bytes()) == {
        "count": 0,
        "blocked": False,
        "restart_in_progress": False,
    }


def test_restart_in_progress_persists_until_confirmed_or_explicit_reset(
    guard: FlyTrayLoopGuard,
) -> None:
    guard.restart_started()
    reopened = FlyTrayLoopGuard(guard.path)
    assert not reopened.allow(True)
    assert not reopened.allow(False)
    assert json.loads(guard.path.read_bytes())["restart_in_progress"] is True
    reopened.restart_confirmed()
    assert FlyTrayLoopGuard(guard.path).allow(False)
    guard.restart_started()
    reopened.reset()
    assert FlyTrayLoopGuard(guard.path).allow(True)


def test_legacy_guard_state_migrates_without_unblocking(guard: FlyTrayLoopGuard) -> None:
    guard.path.parent.mkdir(mode=0o700)
    guard.path.write_text('{"count": 4, "blocked": true}')
    guard.path.chmod(0o600)
    assert not guard.allow(False)
    assert json.loads(guard.path.read_bytes())["restart_in_progress"] is False


@pytest.mark.parametrize("progress", [1, "true", None, []])
def test_invalid_restart_marker_fails_closed(guard: FlyTrayLoopGuard, progress: Any) -> None:
    guard.path.parent.mkdir(mode=0o700)
    guard.path.write_text(
        json.dumps({"count": 0, "blocked": False, "restart_in_progress": progress})
    )
    guard.path.chmod(0o600)
    with pytest.raises(ThemercError):
        guard.allow(False)
