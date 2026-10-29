"""Shared private-file I/O, extended from net.update_cache for dirfd-based stores.

Directory handles pin the hierarchy across rename races. Exclusive temporary
files are always removed on failure; replacement follows file fsync and precedes
directory fsync. No helper includes file contents in its own error messages.
"""

from __future__ import annotations

import contextlib
import errno
import os
import stat
import uuid
from collections.abc import Iterator
from pathlib import Path


def check_private_file(info: os.stat_result) -> None:
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_nlink != 1
    ):
        raise ValueError("Небезопасный файл данных")


@contextlib.contextmanager
def private_directory(
    path: Path, *, create: bool = False, private_from: Path | None = None
) -> Iterator[int]:
    """Open all components without links, requiring private owned leaf/subtree.

    Existing unsafe permissions are refused rather than silently changing them.
    Newly created directories are 0700 regardless of the caller's umask.
    All symlink ancestors, including symlink-HOME, are deliberately refused.
    Callers needing such a home must supply a physical XDG_DATA_HOME instead.
    The older update-cache path API does not use this directory walker.
    """
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("Недопустимый каталог данных")
    private_from = path if private_from is None else private_from
    if not path.is_relative_to(private_from):
        raise ValueError("Недопустимая граница каталога")
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    current = Path("/")
    try:
        for component in path.parts[1:]:
            current /= component
            created = False
            if create:
                try:
                    os.mkdir(component, mode=0o700, dir_fd=descriptor)
                    created = True
                    os.fsync(descriptor)
                except FileExistsError:
                    pass
            child = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = child
            if created:
                os.fchmod(descriptor, 0o700)
            if current.is_relative_to(private_from):
                info = os.fstat(descriptor)
                if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
                    raise ValueError("Небезопасный каталог данных")
        yield descriptor
    finally:
        os.close(descriptor)


def read_private(
    path: Path, limit: int, *, directory_fd: int | None = None, private: bool = False
) -> bytes:
    """Bounded regular-file read; no symlink/FIFO/device following."""
    info = (
        path.lstat()
        if directory_fd is None
        else os.stat(path, dir_fd=directory_fd, follow_symlinks=False)
    )
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("тип файла")
    try:
        descriptor = os.open(
            path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=directory_fd
        )
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise ValueError("тип файла") from None
        raise
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("тип файла")
        if private:
            check_private_file(info)
        if info.st_size > limit:
            raise ValueError("размер")
        chunks = bytearray()
        while len(chunks) <= limit:
            chunk = os.read(descriptor, min(65536, limit + 1 - len(chunks)))
            if not chunk:
                break
            chunks.extend(chunk)
        if len(chunks) > limit:
            raise ValueError("размер")
        return bytes(chunks)
    finally:
        os.close(descriptor)


def _check_destination(path: Path, directory_fd: int, *, create_only: bool, private: bool) -> None:
    try:
        info = os.stat(path, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if create_only:
        raise FileExistsError("Файл уже существует")
    if private:
        check_private_file(info)


def write_private(
    path: Path,
    raw: bytes,
    *,
    directory_fd: int | None = None,
    create_only: bool = False,
    private: bool = False,
) -> None:
    """Atomic private write, optionally relative to an already pinned directory.

    Without dirfd this retains the update-cache helper's public interface.
    create_only publishes via link(O_EXCL semantics), never replacing an entry.
    """
    if directory_fd is None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            write_private(
                Path(path.name),
                raw,
                directory_fd=descriptor,
                create_only=create_only,
                private=private,
            )
        finally:
            os.close(descriptor)
        return
    if path.name != str(path) or path.name in {"", ".", ".."}:
        raise ValueError("Недопустимое имя файла")
    _check_destination(path, directory_fd, create_only=create_only, private=private)
    temporary = f".{path.stem}-{uuid.uuid4().hex}.tmp"
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o600,
        dir_fd=directory_fd,
    )
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            os.fchmod(descriptor, 0o600)
            stream.write(raw)
            stream.flush()
            os.fsync(descriptor)
        _check_destination(path, directory_fd, create_only=create_only, private=private)
        if create_only:
            os.link(
                temporary,
                path,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
                follow_symlinks=False,
            )
            os.unlink(temporary, dir_fd=directory_fd)
        else:
            os.replace(temporary, path, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        os.fsync(directory_fd)
    finally:
        os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
