"""Проверенные каталоги и атомарная запись снимков без неявного обновления refs."""

from __future__ import annotations

import hashlib
import os
import pwd
import secrets
import stat
import warnings
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from PyQt5.QtCore import QIODevice

REPO = Path(__file__).resolve().parents[2]


class SnapshotImage(Protocol):
    def save(self, device: QIODevice, format: str, /) -> bool: ...


def real_home() -> Path:
    """Домашний каталог владельца процесса, независимо от HOME/XDG тестов."""
    return Path(pwd.getpwuid(os.getuid()).pw_dir)


def tree_key() -> str:
    return hashlib.sha256(str(REPO.resolve()).encode()).hexdigest()[:16]


def _check_directory(path: Path, info: os.stat_result | None = None) -> os.stat_result:
    if info is None:
        info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode):
        raise ValueError(f"Небезопасный каталог снимков {path}: символическая ссылка")
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError(f"Небезопасный каталог снимков {path}: не каталог")
    if info.st_uid != os.getuid():
        raise ValueError(f"Небезопасный каталог снимков {path}: чужой владелец")
    if info.st_mode & 0o022:
        raise ValueError(
            f"Небезопасный каталог снимков {path}: доступен для записи группе/остальным"
        )
    return info


def _directory_boundary(path: Path) -> Path:
    absolute = Path(os.path.abspath(path))
    repo = Path(os.path.abspath(REPO))
    if absolute.is_relative_to(repo):
        return repo
    # Системные /, /tmp и /home не принадлежат пользователю; sticky /tmp допустим.
    # В namespace их uid может отличаться от 0. После первого пользовательского
    # звена проверяем ВСЮ цепочку: иначе общий родитель позволяет подменить leaf.
    for parent in reversed((absolute, *absolute.parents)):
        if parent in {Path("/"), Path("/tmp"), Path("/home")}:
            continue
        try:
            info = os.lstat(parent)
        except FileNotFoundError:
            return parent
        if info.st_uid == os.getuid():
            return parent
    return absolute  # Готовая чужая leaf тоже обязана пройти проверку владельца.


@contextmanager
def _open_directory(path: Path, *, create: bool = True) -> Iterator[int]:
    """Открыть каждый исходный компонент без ссылок и удержать каталог записи."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    directory_fd = None
    current = Path(path.absolute().anchor)
    try:
        try:
            boundary = _directory_boundary(path)
            directory_fd = os.open(current, flags)
            # Не resolve: ссылка перед '..' также должна быть отвергнута.
            for part in path.absolute().parts[1:]:
                current = current / part
                try:
                    info = os.lstat(current)
                except FileNotFoundError:
                    if not create:
                        raise
                    try:
                        os.mkdir(part, 0o700, dir_fd=directory_fd)
                    except FileExistsError:
                        pass
                    info = os.lstat(current)
                checked = Path(os.path.abspath(current)).is_relative_to(boundary)
                if checked:
                    info = _check_directory(current)
                child_fd = os.open(part, flags, dir_fd=directory_fd)
                try:
                    opened = os.fstat(child_fd)
                    if checked:
                        _check_directory(current, opened)
                    if (info.st_dev, info.st_ino) != (opened.st_dev, opened.st_ino):
                        raise ValueError(f"Каталог снимков {current}: подменён при открытии")
                except BaseException:
                    os.close(child_fd)
                    raise
                os.close(directory_fd)
                directory_fd = child_fd
            _check_directory(path, os.fstat(directory_fd))
        except OSError as error:
            raise ValueError(f"Каталог снимков {current}: {error.strerror or error}") from error
        yield directory_fd
    finally:
        if directory_fd is not None:
            os.close(directory_fd)


def _ensure_directory(path: Path) -> None:
    with _open_directory(path):
        pass


def _safe_name(name: str) -> None:
    if not name or name in {".", ".."} or "/" in name or "\0" in name:
        raise ValueError(f"Недопустимое имя снимка: {name!r}")


def snapshot_dir(
    env_var: str, set_name: str, refs_default: str, *, default: Path | None = None
) -> Path:
    """Выбрать и проверить каталог; относительный env считается от REPO.

    По умолчанию: passwd-home/.cache/astra-voice/snapshots/<tree-key>/<set>.
    HOME и XDG_CACHE_HOME игнорируются. Проверяется вся пользовательская
    цепочка родителей, для путей внутри репозитория — начиная с REPO.
    Только ASTRA_VOICE_UPDATE_REFS=1 разрешает обновлять design/refs.
    """
    design = (REPO / "design").resolve()
    refs = REPO.resolve() / "design/refs"
    if os.environ.get("ASTRA_VOICE_UPDATE_REFS") == "1":
        destination = REPO / refs_default
        if not destination.resolve().is_relative_to(refs):
            raise ValueError("refs_default должен находиться внутри design/refs")
        _ensure_directory(destination)
        return destination

    explicit = os.environ.get(env_var)
    selected = REPO / explicit if explicit else default
    resolved = selected.resolve() if selected is not None else None
    redirected = resolved is not None and (
        resolved.is_relative_to(design)
        or any(
            first == "design" and second == "refs"
            for first, second in zip(resolved.parts, resolved.parts[1:], strict=False)
        )
    )
    if selected is not None and not redirected:
        _ensure_directory(selected)
        return selected

    _safe_name(set_name)
    cache = real_home() / ".cache"
    key = tree_key()
    destination = cache / "astra-voice" / "snapshots" / key / set_name
    if destination.resolve().is_relative_to(design):
        raise ValueError(f"Каталог снимков {destination}: cache указывает в design/")
    current = cache
    _ensure_directory(current)
    for part in ("astra-voice", "snapshots", key, set_name):
        current = current / part
        _ensure_directory(current)
    if redirected:
        warnings.warn(
            f"Снимки направлены в каталог кеша {destination}; эталоны design/refs "
            "не тронуты. Для их обновления задайте ASTRA_VOICE_UPDATE_REFS=1.",
            UserWarning,
            stacklevel=2,
        )
    return destination


def _encode_png(image: SnapshotImage | bytes) -> bytes:
    if isinstance(image, bytes):
        return image

    # Qt не нужен для выбора каталогов, проверки refs и записи готовых bytes.
    from PyQt5.QtCore import QBuffer, QIODevice

    buffer = QBuffer()
    try:
        if not buffer.open(QIODevice.WriteOnly) or not image.save(buffer, "PNG"):
            raise OSError("Не удалось закодировать снимок PNG в память")
        return bytes(buffer.data())
    finally:
        buffer.close()


def write_snapshot(directory: Path, name: str, image: SnapshotImage | bytes) -> Path:
    """Закодировать в память и опубликовать bytes через защищённый дескриптор."""
    _safe_name(name)
    data = _encode_png(image)
    flags = os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_WRONLY | os.O_CLOEXEC
    with _open_directory(directory) as directory_fd:
        for _ in range(10):
            temporary = f".{name}.{secrets.token_hex(12)}.tmp"
            try:
                fd = os.open(temporary, flags, 0o600, dir_fd=directory_fd)
            except FileExistsError:
                continue
            break
        else:
            raise FileExistsError(f"Не удалось создать уникальный временный снимок в {directory}")
        try:
            try:
                remaining = memoryview(data)
                while remaining:
                    written = os.write(fd, remaining)
                    if written <= 0:
                        raise OSError(f"Не удалось записать снимок {temporary}")
                    remaining = remaining[written:]
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(temporary, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
            return directory / name
        finally:
            _unlink_snapshot(directory_fd, temporary)


def _unlink_snapshot(directory_fd: int, name: str) -> None:
    try:
        os.unlink(name, dir_fd=directory_fd)
    except FileNotFoundError:
        pass


def publish_snapshot(source: Path, directory: Path, name: str) -> Path:
    """Публикация staging-файла между двумя проверенными каталогами."""
    _safe_name(name)
    _safe_name(source.name)
    with _open_directory(source.parent, create=False) as source_fd:
        with _open_directory(directory) as destination_fd:
            os.replace(source.name, name, src_dir_fd=source_fd, dst_dir_fd=destination_fd)
    return directory / name


def delete_snapshot(directory: Path, name: str) -> None:
    """Удалить имя относительно проверенного каталога, не следуя подмене пути."""
    _safe_name(name)
    with _open_directory(directory, create=False) as directory_fd:
        _unlink_snapshot(directory_fd, name)


@contextmanager
def snapshot_staging(directory: Path, prefix: str) -> Iterator[Path]:
    """Приватный плоский staging модуля; создание и очистка привязаны к fd."""
    _safe_name(prefix)
    with _open_directory(directory) as parent_fd:
        for _ in range(10):
            name = f"{prefix}{secrets.token_hex(12)}"
            try:
                os.mkdir(name, 0o700, dir_fd=parent_fd)
            except FileExistsError:
                continue
            break
        else:
            raise FileExistsError(f"Не удалось создать staging в {directory}")
        path = directory / name
        original = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        _check_directory(path, original)
        staging_fd = None
        checked = False
        try:
            staging_fd = os.open(
                name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent_fd
            )
            opened = _check_directory(path, os.fstat(staging_fd))
            if (opened.st_dev, opened.st_ino) != (original.st_dev, original.st_ino):
                raise ValueError(f"Каталог снимков {path}: подменён при открытии")
            checked = True
            yield path
        finally:
            try:
                # В staging лежат только PNG/временные файлы; ссылки не обходим.
                if staging_fd is not None and checked:
                    for child in os.listdir(staging_fd):
                        _unlink_snapshot(staging_fd, child)
                try:
                    current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    # Подставленный вместо staging каталог не удаляем.
                    if (current.st_dev, current.st_ino) == (original.st_dev, original.st_ino):
                        os.rmdir(name, dir_fd=parent_fd)
            finally:
                if staging_fd is not None:
                    os.close(staging_fd)


RefsInventory = dict[str, tuple[str, str]]


def refs_inventory(root: Path) -> RefsInventory:
    """Типы и SHA256 regular files только в refs; ссылки не обходятся."""
    inventory: RefsInventory = {}

    def visit(path: Path) -> None:
        info = os.lstat(path)
        relative = path.relative_to(root).as_posix()
        if stat.S_ISLNK(info.st_mode):
            inventory[relative] = ("symlink", os.readlink(path))
        elif stat.S_ISDIR(info.st_mode):
            inventory[relative] = ("dir", "")
            for child in sorted(path.iterdir()):
                visit(child)
        elif stat.S_ISREG(info.st_mode):
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
            with os.fdopen(fd, "rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            inventory[relative] = ("file", digest)
        else:
            inventory[relative] = ("special", str(stat.S_IFMT(info.st_mode)))

    try:
        os.lstat(root)
    except FileNotFoundError:
        return inventory
    visit(root)
    return inventory


@dataclass(frozen=True)
class RefsGuard:
    root: Path
    before: RefsInventory
    update_refs: bool

    @classmethod
    def capture(cls) -> RefsGuard:
        root = REPO / "design/refs"
        return cls(root, refs_inventory(root), os.environ.get("ASTRA_VOICE_UPDATE_REFS") == "1")

    def changed_paths(self) -> list[str]:
        if self.update_refs:
            return []
        after = refs_inventory(self.root)
        return sorted(
            name
            for name in self.before.keys() | after.keys()
            if self.before.get(name) != after.get(name)
        )

    def failure_message(self, limit: int = 20) -> str | None:
        changed = self.changed_paths()
        if not changed:
            return None
        listed = "\n".join(f"  design/refs/{name}" for name in changed[:limit])
        rest = f"\n  … ещё {len(changed) - limit}" if len(changed) > limit else ""
        return (
            "IB42: design/refs изменён без ASTRA_VOICE_UPDATE_REFS=1 "
            f"({len(changed)} путей):\n{listed}{rest}"
        )
