"""Проверенные каталоги и атомарная запись снимков без неявного обновления refs."""

from __future__ import annotations

import hashlib
import os
import pwd
import secrets
import stat
import warnings
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


def _check_directory(path: Path) -> None:
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


def _ensure_directory(path: Path) -> None:
    """Проверить конечный каталог и каждое вновь созданное звено."""
    try:
        try:
            _check_directory(path)
            return
        except FileNotFoundError:
            pass
        # Существующие родители explicit/ref не входят в границу проверки.
        if not path.parent.exists():
            _ensure_directory(path.parent)
        try:
            path.mkdir(mode=0o700)
        except FileExistsError:
            pass  # Другой писатель мог успеть создать звено; всё равно проверяем.
        _check_directory(path)
    except OSError as error:
        raise ValueError(f"Каталог снимков {path}: {error.strerror or error}") from error


def _safe_name(name: str) -> None:
    if not name or name in {".", ".."} or "/" in name or "\0" in name:
        raise ValueError(f"Недопустимое имя снимка: {name!r}")


def snapshot_dir(
    env_var: str, set_name: str, refs_default: str, *, default: Path | None = None
) -> Path:
    """Выбрать и проверить каталог; относительный env считается от REPO.

    По умолчанию: passwd-home/.cache/astra-voice/snapshots/<tree-key>/<set>.
    HOME и XDG_CACHE_HOME игнорируются. В cache проверяется вся цепочка
    начиная с cache root; explicit/ref и переданный default проверяются в листе.
    Только ASTRA_VOICE_UPDATE_REFS=1 разрешает обновлять design/refs.
    """
    design = (REPO / "design").resolve()
    refs = (REPO / "design/refs").resolve()
    if os.environ.get("ASTRA_VOICE_UPDATE_REFS") == "1":
        destination = REPO / refs_default
        if not destination.resolve().is_relative_to(refs):
            raise ValueError("refs_default должен находиться внутри design/refs")
        _ensure_directory(destination)
        return destination

    explicit = os.environ.get(env_var)
    selected = REPO / explicit if explicit else default
    redirected = selected is not None and selected.resolve().is_relative_to(design)
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
    _ensure_directory(directory)
    flags = os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_WRONLY | os.O_CLOEXEC
    for _ in range(10):
        temporary = directory / f".{name}.{secrets.token_hex(12)}.tmp"
        try:
            fd = os.open(temporary, flags, 0o600)
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
        destination = directory / name
        os.replace(temporary, destination)
        return destination
    finally:
        temporary.unlink(missing_ok=True)


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
