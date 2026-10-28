"""Bounded, nonblocking themerc reads; no Qt, decoding, or implicit writes."""

from __future__ import annotations

import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

MAX_BYTES = 1024 * 1024
READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


class ThemercError(Exception):
    """A path-free failure code, safe to present or log."""

    def __init__(self, code: str, error_number: int = 0) -> None:
        super().__init__(code)
        self.code = code
        self.error_number = error_number


def fingerprint(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


@contextmanager
def open_directory(path: Path, *, home: Path | None = None, create: bool = False) -> Iterator[int]:
    """Pin every component. With home, check ownership/mode from that anchor."""
    path = path.absolute()
    if ".." in path.parts or (home is not None and not path.is_relative_to(home)):
        raise ThemercError("administrator")
    fd = os.open(path.anchor, DIRECTORY_FLAGS)
    try:
        for index, part in enumerate(path.parts[1:], 1):
            checked = home is not None and index >= len(home.parts) - 1
            try:
                child = os.open(part, DIRECTORY_FLAGS, dir_fd=fd)
            except FileNotFoundError:
                if not create or not checked:
                    raise
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
                child = os.open(part, DIRECTORY_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = child
            if checked:
                info = os.fstat(fd)
                if info.st_uid != os.getuid() or info.st_mode & 0o022:
                    raise ThemercError("administrator")
        yield fd
    finally:
        os.close(fd)


def read_fd(fd: int, *, limit: int = MAX_BYTES) -> tuple[bytes, os.stat_result]:
    before = os.fstat(fd)
    if not stat.S_ISREG(before.st_mode):
        raise ThemercError("not-regular")
    if before.st_size > limit:
        raise ThemercError("too-large")
    chunks: list[bytes] = []
    size = 0
    while True:
        chunk = os.read(fd, min(65536, limit + 1 - size))
        size += len(chunk)
        if size > limit:
            raise ThemercError("too-large")
        if fingerprint(os.fstat(fd)) != fingerprint(before):
            raise ThemercError("changed")
        if not chunk:
            break
        chunks.append(chunk)
    data = b"".join(chunks)
    if len(data) != before.st_size:
        raise ThemercError("changed")
    return data, before


def read_themerc(path: Path) -> bytes | None:
    """Return bytes or a fallback sentinel. Never open FIFO in blocking mode."""
    try:
        with open_directory(path.parent) as directory:
            fd = os.open(path.name, READ_FLAGS, dir_fd=directory)
            try:
                return read_fd(fd)[0]
            finally:
                os.close(fd)
    except (OSError, ThemercError, ValueError):
        return None
