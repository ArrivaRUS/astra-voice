"""ИБ-76: both old consumers must reject unbounded and blocking inputs."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import pytest

from astra_voice.platform import fly_themerc
from astra_voice.platform.paste import FLY_FALLBACK_TYPE, _fly_blacklist_type
from astra_voice.ui.tray_icons import _theme_variables, fly_panel_dark
from helpers.fly_deadline import fifo_deadline

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("kind", ["fifo", "symlink", "large", "directory", "growing"])
def test_ib76_both_consumers_fail_fast(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    path = tmp_path / "current.themerc"
    if kind == "fifo":
        os.mkfifo(path)
    elif kind == "symlink":
        path.symlink_to("/dev/zero")
    elif kind == "large":
        path.write_bytes(b"x" * (2 * 1024**2))
    elif kind == "directory":
        path.mkdir()
    else:
        path.write_bytes(b"TaskbarColor=#ffffff\nClipboardManagerTypesBlacklist=secret-type:\n")
        real_read = os.read

        def growing(fd: int, count: int) -> bytes:
            data = real_read(fd, count)
            if data:
                with path.open("ab") as stream:
                    stream.write(b"grows")
            return data

        monkeypatch.setattr(os, "read", growing)
    start = time.monotonic()
    with fifo_deadline():
        assert fly_panel_dark((path,)) is None
        assert _theme_variables(path) is None
        assert _fly_blacklist_type((path,)) == FLY_FALLBACK_TYPE
    assert time.monotonic() - start < 0.1


def test_ib76_size_limit_uses_read_bytes_not_only_stat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "current.themerc"
    path.write_bytes(b"small")
    calls = 0

    def read_forever(fd: int, count: int) -> bytes:
        nonlocal calls
        calls += 1
        return b"x" * count

    # Restore before pytest's PulseAudio /proc guard runs its teardown hook.
    with monkeypatch.context() as patch:
        patch.setattr(os, "read", read_forever)
        assert fly_themerc.read_themerc(path) is None
        assert calls < 30


def test_ib76_symlink_ancestor_rejected(tmp_path: Path) -> None:
    folder = tmp_path / "real"
    folder.mkdir()
    (folder / "current.themerc").write_bytes(b"TaskbarColor=#ffffff\n")
    alias = tmp_path / "link"
    alias.symlink_to(folder)
    assert fly_themerc.read_themerc(alias / "current.themerc") is None


def test_ib76_normal_consumers_unchanged(tmp_path: Path) -> None:
    path = tmp_path / "current.themerc"
    path.write_bytes(b"TaskbarColor=#ffffff\nClipboardManagerTypesBlacklist=application/first:\n")
    assert fly_panel_dark((path,)) is False
    assert _fly_blacklist_type((path,)) == "application/first"


@pytest.mark.parametrize("newline", [b"\n", b"\r\n"], ids=["lf", "crlf"])
def test_ib76_sectioned_variables_are_read_by_existing_consumers(
    tmp_path: Path, newline: bytes
) -> None:
    path = tmp_path / "current.themerc"
    source = b"\xef\xbb\xbf" + newline.join(
        [
            b"[General]",
            b"Name=Fly 1.8.4 fixture",
            b"[Variables]",
            b"TaskbarColor=#202020",
            b"TrayBlackList=unrelated-app:",
            b"UseSystemTray=true",
            b"ClipboardManagerTypesBlacklist=application/section-private:",
            b"TrayPriorityList=fly-notifications:qbat:fly-vkbd:",
            b"[Other]",
            b"Untouched=keep",
            b"",
        ]
    )
    path.write_bytes(source)
    assert fly_themerc.read_themerc(path) == source
    values = _theme_variables(path)
    assert values is not None
    assert values["TaskbarColor"] == "#202020"
    assert values["TrayBlackList"] == "unrelated-app:"
    assert values["UseSystemTray"] == "true"
    assert fly_panel_dark((path,)) is True
    assert _fly_blacklist_type((path,)) == "application/section-private"
    assert path.read_bytes() == source


def test_ib76_regular_read_error_is_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "current.themerc"
    path.write_bytes(b"hello")

    def fail(*args: Any, **kwargs: Any) -> bytes:
        raise OSError("private")

    monkeypatch.setattr(os, "read", fail)
    assert fly_themerc.read_themerc(path) is None
