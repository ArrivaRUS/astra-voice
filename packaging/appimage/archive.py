#!/usr/bin/env python3
"""Один AppImage в воспроизводимом .AppImage.tar.gz с режимом 0755.

    python3 packaging/appimage/archive.py <AppImage>
    python3 packaging/appimage/archive.py --check <AppImage>

gzip level 1: AppImage уже сжат. Архив проверяется потоком без распаковки
на диск и без исполнения. SOURCE_DATE_EPOCH задаёт дату tar (по умолчанию 0).
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import os
import re
import stat
import tarfile
import tempfile
from pathlib import Path
from typing import IO

BLOCK_SIZE = 1 << 20


def digest(stream: IO[bytes]) -> str:
    result = hashlib.sha256()
    while block := stream.read(BLOCK_SIZE):
        result.update(block)
    return result.hexdigest()


def check(archive: Path, image: Path) -> None:
    """Сверить единственный обычный файл, его имя, права и байты с raw AppImage."""
    with image.open("rb") as source, gzip.open(archive, "rb") as decompressed:
        with tarfile.open(fileobj=decompressed, mode="r|") as bundle:
            member = bundle.next()
            if (
                member is None
                or not member.isfile()
                or member.name != image.name
                or member.mode != 0o755
                or member.size != os.fstat(source.fileno()).st_size
                or member.uid != 0
                or member.gid != 0
            ):
                raise ValueError("архив должен содержать один AppImage basename с режимом 0755")
            payload = bundle.extractfile(member)
            if payload is None:
                raise ValueError("в архиве нет содержимого AppImage")
            with payload:
                if digest(payload) != digest(source):
                    raise ValueError("байты AppImage в архиве не совпадают с исходным файлом")
            if bundle.next() is not None:
                raise ValueError("в архиве есть посторонние файлы")
        # Tar EOF наступает раньше gzip EOF: дочитываем поток для проверки CRC,
        # размера и наличия trailer. Закрытие GzipFile само по себе этого не делает.
        while decompressed.read(BLOCK_SIZE):
            pass


def create(image: Path, epoch: int) -> Path:
    """Создать архив рядом с образом и заменить результат после проверки."""
    if not 0 <= epoch < 1 << 33:
        raise ValueError("SOURCE_DATE_EPOCH вне диапазона tar USTAR")
    archive = image.with_name(image.name + ".tar.gz")
    with tempfile.NamedTemporaryFile(
        prefix=".appimage-archive-", dir=image.parent, delete=False
    ) as raw:
        temporary = Path(raw.name)
        try:
            descriptor = os.open(image, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(descriptor, "rb") as source:
                metadata = os.fstat(source.fileno())
                if not stat.S_ISREG(metadata.st_mode):
                    raise ValueError("AppImage должен быть обычным файлом")
                member = tarfile.TarInfo(image.name)
                member.size = metadata.st_size
                member.mode = 0o755
                member.mtime = epoch
                with gzip.GzipFile(
                    filename="", mode="wb", fileobj=raw, compresslevel=1, mtime=0
                ) as gz:
                    with tarfile.open(fileobj=gz, mode="w|", format=tarfile.USTAR_FORMAT) as bundle:
                        bundle.addfile(member, source)
            raw.flush()
            check(temporary, image)
            os.chmod(temporary, 0o644)
            os.replace(temporary, archive)
        finally:
            temporary.unlink(missing_ok=True)
    return archive


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", type=Path)
    parser.add_argument("--check", action="store_true", help="проверить уже созданный архив")
    args = parser.parse_args()
    image: Path = args.image.absolute()
    try:
        if not re.fullmatch(r"Astra_Voice-[0-9A-Za-z.+~_-]+-x86_64\.AppImage", image.name):
            raise ValueError("ожидается имя Astra_Voice-<версия>-x86_64.AppImage")
        if image.is_symlink() or not image.is_file():
            raise ValueError("AppImage должен быть обычным файлом, не символической ссылкой")
        archive = image.with_name(image.name + ".tar.gz")
        if args.check:
            if archive.is_symlink():
                raise ValueError("архив не должен быть символической ссылкой")
            check(archive, image)
        else:
            archive = create(image, int(os.environ.get("SOURCE_DATE_EPOCH", "0")))
    except (OSError, ValueError, tarfile.TarError, EOFError) as exc:
        parser.exit(1, f"ОШИБКА: {exc}\n")
    print(f"AppImage tar.gz: {archive}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
