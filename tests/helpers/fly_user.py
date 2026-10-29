"""Model an ordinary Fly user consistently when pytest itself runs as root."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

REAL_GETUID = os.getuid


def user_stat(info: os.stat_result, *, kernel_uid: int, user_uid: int) -> os.stat_result:
    """Translate only the test owner's uid; retain times, permissions and foreign owners."""
    if info.st_uid != kernel_uid:
        return info
    fields = list(info)
    fields[4] = user_uid
    tuple_fields = {"st_mode", "st_ino", "st_dev", "st_nlink", "st_uid", "st_gid", "st_size"}
    # stat_result's sequence truncates timestamps; its named extras preserve nanoseconds.
    extras = {
        name: getattr(info, name)
        for name in dir(info)
        if name.startswith("st_") and name not in tuple_fields
    }
    return os.stat_result(fields, extras)


@pytest.fixture(autouse=True)
def fly_user(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Ordinary-user scenarios must not inherit the CI container's root identity.

    The process remains unchanged. Only metadata of this test's private tree is
    translated, so root-owned system executables and intentional foreign owners
    retain their original security meaning. Tests can still override either uid
    or the stat functions afterwards to exercise refusal paths.
    """
    normal_user(tmp_path, monkeypatch)


def normal_user(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Install the identity model; also callable for a synthetic root-kernel test."""
    kernel_uid = os.getuid()
    user_uid = kernel_uid or 1000
    monkeypatch.setattr(os, "getuid", lambda: user_uid)
    monkeypatch.setattr(os, "geteuid", lambda: user_uid)
    if kernel_uid != 0:
        return

    real_stat, real_fstat = os.stat, os.fstat
    home = tmp_path.resolve()

    def scoped(path: Any, *, dir_fd: int | None = None) -> bool:
        try:
            if isinstance(path, int):
                target = Path(os.readlink(f"/proc/self/fd/{path}"))
            else:
                target = Path(os.fsdecode(path))
                if not target.is_absolute() and dir_fd is not None:
                    target = Path(os.readlink(f"/proc/self/fd/{dir_fd}")) / target
                target = Path(os.path.realpath(target))
            return target.is_relative_to(home)
        except (OSError, TypeError, ValueError):
            return False

    def stat_user(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        info = real_stat(path, *args, **kwargs)
        return (
            user_stat(info, kernel_uid=kernel_uid, user_uid=user_uid)
            if scoped(path, dir_fd=kwargs.get("dir_fd"))
            else info
        )

    def fstat_user(fd: int) -> os.stat_result:
        info = real_fstat(fd)
        return user_stat(info, kernel_uid=kernel_uid, user_uid=user_uid) if scoped(fd) else info

    monkeypatch.setattr(os, "stat", stat_user)
    monkeypatch.setattr(os, "fstat", fstat_user)
