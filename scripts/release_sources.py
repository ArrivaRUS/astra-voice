#!/usr/bin/env python3
"""Архив исходников выпуска AppImage (arch/appimage.md R3.6, T-188) — без сети.

    release_sources.py build --lock packaging/appimage.lock --cache <кэш> --root <репо> \\
        --version 0.2.0 --out dist/astra-voice-0.2.0-sources.tar.xz
    release_sources.py check dist/astra-voice-0.2.0-sources.tar.xz

`build`: все записи `# source:` lock проверяются `debverify.py sources` (подпись индекса
Debian, sha256 из lock, полнота архивов `.dsc`) и кладутся в `upstream/<id>/`; исходники
программы — `git archive` текущего коммита (с lock, ключами поставщиков, vendor-патчами и
инструкциями сборки), рабочее дерево обязано быть чистым. Первым членом архива идёт
`MANIFEST.txt` (sha256, размер, путь, происхождение каждого файла). Порядок, владельцы,
права и время нормализованы (SOURCE_DATE_EPOCH) — архив воспроизводим.

`check`: потоковая проверка (архив не читается в память целиком): только обычные файлы
внутри верхнего каталога, первым — манифест, каждый файл совпадает с ним по размеру и
sha256, все строки манифеста покрыты ровно один раз. Печатает sha256 всего архива.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import io
import lzma
import os
import stat
import subprocess
import sys
import tarfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import IO, Any, Literal

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = "MANIFEST.txt"
README = "README.txt"
#: Предел архива исходников: отдельный от обычных ассетов (16 МиБ) и образов (512 МиБ).
SOURCES_LIMIT = 1 << 30
XZ_PRESET: Literal[6] = 6

_README = """Исходный код выпуска Astra Voice {version} (AppImage)
====================================================

MANIFEST.txt   sha256, размер, путь и происхождение каждого файла этого архива.
astra-voice/   исходники программы — git archive коммита {commit}: код, packaging/
               (сборщик AppImage, appimage.lock со всеми пинами, ключи проверки
               поставщиков), vendor/patches, инструкции (packaging/appimage/, arch/).
upstream/      исходники поставляемых сторонних компонентов ровно тех версий, что в
               образе (закреплены в packaging/appimage.lock, проверены по sha256 и,
               где есть, по подписи): Python 3.11 (Debian .dsc, orig, debian), Qt
               5.15.19 (qtbase, qtdeclarative, qtquickcontrols2, qtsvg), PyQt5 5.15.11,
               загрузчик AppImage type2-runtime и libfuse 3.15.0.

Сборка образа: packaging/appimage/build.sh --fetch (сеть), затем packaging/appimage/build.sh.
"""


def _load(name: str, path: Path) -> Any:
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


class SourcesError(RuntimeError):
    """Архив исходников неполон или не совпадает с манифестом."""


@dataclass(frozen=True)
class Entry:
    path: str  # относительно верхнего каталога архива
    sha256: str
    size: int
    origin: str


def _digest(path: Path) -> tuple[str, int]:
    h = hashlib.sha256()
    size = 0
    with path.open("rb") as fh:
        while chunk := fh.read(1 << 20):
            h.update(chunk)
            size += len(chunk)
    return h.hexdigest(), size


def _git(root: Path, *args: str) -> bytes:
    return subprocess.run(
        ["git", "-c", f"safe.directory={root}", "-C", str(root), *args],
        capture_output=True,
        check=True,
    ).stdout


def _tarinfo(name: str, size: int, epoch: int) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.size = size
    info.mtime = epoch
    info.mode = 0o644
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    info.type = tarfile.REGTYPE
    return info


def build(lock_path: Path, cache: Path, root: Path, version: str, out: Path, epoch: int) -> str:
    """Собрать архив; вернуть его sha256."""
    lockfile = _load("lockfile", root / "packaging" / "appimage" / "lockfile.py")
    debverify = _load("appimage_debverify", root / "packaging" / "appimage" / "debverify.py")
    lock = lockfile.load(lock_path)
    try:
        debverify.Verifier(lock, cache, root).verify_sources()
    except debverify.VerifyError as exc:
        raise SourcesError(f"исходники не прошли проверку: {exc}") from None
    if _git(root, "status", "--porcelain", "--untracked-files=no").strip():
        raise SourcesError("рабочее дерево не чистое: архив собирается только из коммита")
    commit = _git(root, "rev-parse", "HEAD").decode().strip()
    code = _git(root, "archive", "--format=tar", f"--prefix=astra-voice-{version}/", commit)

    top = f"astra-voice-{version}-sources"
    readme = _README.format(version=version, commit=commit).encode()
    files: list[tuple[Entry, Path | bytes]] = [
        (Entry(README, hashlib.sha256(readme).hexdigest(), len(readme), "astra-voice"), readme),
        (
            Entry(
                f"astra-voice/astra-voice-{version}-{commit[:12]}.tar",
                hashlib.sha256(code).hexdigest(),
                len(code),
                f"git {commit}",
            ),
            code,
        ),
    ]
    for source in sorted(lock.sources, key=lambda s: s.id):
        path = cache / source.cache_path
        sha, size = _digest(path)
        if (sha, size) != (source.sha256, source.size):
            raise SourcesError(f"{source.id}: файл в кэше изменился после проверки")
        files.append((Entry(f"upstream/{source.id}/{source.file}", sha, size, source.url), path))
    files.sort(key=lambda item: item[0].path)
    manifest = "".join(f"{e.sha256}\t{e.size}\t{e.path}\t{e.origin}\n" for e, _ in files).encode()

    out.parent.mkdir(parents=True, exist_ok=True)
    part = out.with_name(out.name + ".part")
    with tarfile.open(part, "w:xz", preset=XZ_PRESET, format=tarfile.GNU_FORMAT) as tar:
        tar.addfile(_tarinfo(f"{top}/{MANIFEST}", len(manifest), epoch), io.BytesIO(manifest))
        for entry, content in files:
            info = _tarinfo(f"{top}/{entry.path}", entry.size, epoch)
            if isinstance(content, bytes):
                tar.addfile(info, io.BytesIO(content))
            else:
                with content.open("rb") as fh:
                    tar.addfile(info, fh)
    part.replace(out)
    return _digest(out)[0]


class _HashingReader(io.RawIOBase):
    """Поток файла с подсчётом sha256 и предела размера."""

    def __init__(self, fh: IO[bytes], limit: int) -> None:
        self._fh = fh
        self._limit = limit
        self.size = 0
        self.sha = hashlib.sha256()

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int:
        data = self._fh.read(len(buffer))
        self.size += len(data)
        if self.size > self._limit:
            raise SourcesError("архив больше предела")
        self.sha.update(data)
        buffer[: len(data)] = data
        return len(data)


def _open_regular(path: Path) -> IO[bytes]:
    """Открыть обычный файл без следования за ссылкой."""
    if stat.S_ISLNK(path.lstat().st_mode):
        raise SourcesError("не обычный файл")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise SourcesError("не обычный файл")
    return os.fdopen(fd, "rb")


def _members(tar: tarfile.TarFile) -> Iterator[tarfile.TarInfo]:
    while (member := tar.next()) is not None:
        yield member


def check(path: Path, limit: int = SOURCES_LIMIT) -> tuple[str, int]:
    """Потоковая проверка архива; вернуть (sha256 архива, число файлов)."""
    with _open_regular(path) as raw:
        if os.fstat(raw.fileno()).st_size > limit:
            raise SourcesError("архив больше предела")
        reader = _HashingReader(raw, limit)
        stream = io.BufferedReader(reader, 1 << 20)
        try:
            # Распаковка своим LZMAFile: tarfile в режиме «r|xz» не замечает обрезанный
            # конец потока xz, а LZMAFile при дочитывании бросает EOFError.
            with lzma.LZMAFile(stream) as unpacked:
                with tarfile.open(fileobj=unpacked, mode="r|") as tar:
                    count = _check_members(tar)
                while unpacked.read(1 << 20):
                    pass
        except (tarfile.TarError, EOFError, OSError, lzma.LZMAError) as exc:
            raise SourcesError(f"архив повреждён: {exc}") from None
        while stream.read(1 << 20):  # хвост после конца tar — тоже в sha256
            pass
    return reader.sha.hexdigest(), count


def _check_members(tar: tarfile.TarFile) -> int:
    manifest: dict[str, tuple[str, int]] | None = None
    top: str | None = None
    seen: set[str] = set()
    for member in _members(tar):
        name = PurePosixPath(member.name)
        if name.is_absolute() or ".." in name.parts or len(name.parts) < 2:
            raise SourcesError(f"недопустимый путь: {member.name}")
        if not member.isreg():
            raise SourcesError(f"не обычный файл: {member.name}")
        if top is None:
            top = name.parts[0]
        if name.parts[0] != top:
            raise SourcesError(f"вне каталога {top}: {member.name}")
        rel = "/".join(name.parts[1:])
        fh = tar.extractfile(member)
        assert fh is not None
        if manifest is None:
            if rel != MANIFEST:
                raise SourcesError("первым в архиве должен идти MANIFEST.txt")
            manifest = _parse_manifest(fh.read(16 << 20).decode("utf-8"))
            continue
        if rel in seen:
            raise SourcesError(f"повтор файла: {rel}")
        seen.add(rel)
        expected = manifest.get(rel)
        if expected is None:
            raise SourcesError(f"файла нет в манифесте: {rel}")
        h = hashlib.sha256()
        while chunk := fh.read(1 << 20):
            h.update(chunk)
        if (h.hexdigest(), member.size) != expected:
            raise SourcesError(f"не совпал с манифестом: {rel}")
    if manifest is None:
        raise SourcesError("пустой архив")
    missing = sorted(set(manifest) - seen)
    if missing:
        raise SourcesError(f"нет файлов из манифеста: {', '.join(missing[:5])}")
    if README not in seen or not any(p.startswith("astra-voice/") for p in seen):
        raise SourcesError("нет README.txt или исходников программы")
    if not any(p.startswith("upstream/") for p in seen):
        raise SourcesError("нет исходников сторонних компонентов")
    return len(seen)


def _parse_manifest(text: str) -> dict[str, tuple[str, int]]:
    entries: dict[str, tuple[str, int]] = {}
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) != 4 or len(parts[0]) != 64 or not parts[1].isdigit():
            raise SourcesError(f"неверная строка манифеста: {line[:80]}")
        if parts[2] in entries:
            raise SourcesError(f"повтор в манифесте: {parts[2]}")
        entries[parts[2]] = (parts[0], int(parts[1]))
    if not entries:
        raise SourcesError("пустой манифест")
    return entries


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="архив исходников выпуска AppImage")
    sub = parser.add_subparsers(dest="command", required=True)
    b = sub.add_parser("build")
    b.add_argument("--lock", type=Path, required=True)
    b.add_argument("--cache", type=Path, required=True)
    b.add_argument("--root", type=Path, default=ROOT)
    b.add_argument("--version", required=True)
    b.add_argument("--out", type=Path, required=True)
    c = sub.add_parser("check")
    c.add_argument("archive", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "build":
            epoch = int(os.environ.get("SOURCE_DATE_EPOCH") or 0)
            sha = build(args.lock, args.cache, args.root, args.version, args.out, epoch)
            print(f"исходники: {args.out} ({args.out.stat().st_size} байт, sha256 {sha})")
        else:
            sha, count = check(args.archive)
            print(f"исходники: {count} файлов по манифесту, sha256 {sha}")
    # ValueError — в том числе LockError из lockfile.py.
    except (OSError, ValueError, SourcesError, subprocess.CalledProcessError) as exc:
        print(f"ОШИБКА: исходники: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
