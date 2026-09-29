#!/usr/bin/env python3
"""Создать указатель latest.json (схема 2) для артефактов выпуска.

Формат — arch/appimage.md §6: общие поля и блок `artifacts` с именем, sha256 и
размером каждого артефакта; `appimage` — только с флагом `--appimage` (его ставит
`scripts/release_assets.sh` при `packaging/appimage/ENABLED`). `latest.json` — лишь
указатель: имя ассета продукт строит из версии и сверяет с подписанными суммами (T1 MJ-2).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Минимальная поддерживаемая ОС — Astra Linux SE 1.8 (PRD/ТЗ).
# Пакет собирается под debian:12 = ALSE 1.8; см. packaging/Containerfile.
MIN_ASTRA = "1.8"
SCHEMA = 2
REPOSITORY = "ArrivaRUS/astra-voice"


def published_at(raw: str | None) -> str:
    """Вернуть дату в строгом UTC ISO 8601 без долей секунды."""
    if raw is None:
        return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", raw):
        raise ValueError("--published-at должен иметь вид YYYY-MM-DDTHH:MM:SSZ")
    datetime.strptime(raw, "%Y-%m-%dT%H:%M:%SZ")
    return raw


def appimage_name(version: str) -> str:
    """Имя ассета AppImage строится из версии (T1 MJ-2)."""
    return f"Astra_Voice-{version}-x86_64.AppImage"


def _artifact(path: Path) -> dict[str, Any]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as fh:
        while block := fh.read(1 << 20):
            digest.update(block)
            size += len(block)
    return {"name": path.name, "sha256": digest.hexdigest(), "size": size}


def generate(
    dist: Path, version: str, date: str | None = None, *, appimage: bool = False
) -> dict[str, Any]:
    """Найти артефакты версии и записать указатель."""
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        raise ValueError("версия должна иметь вид X.Y.Z")
    debs = sorted(dist.glob(f"astra-voice_{version}_*.deb"))
    if len(debs) != 1:
        raise ValueError(f"ожидался ровно один astra-voice_{version}_*.deb, найдено {len(debs)}")
    artifacts: dict[str, dict[str, Any]] = {"deb": _artifact(debs[0])}
    images = sorted(dist.glob("Astra_Voice-*.AppImage"))
    if appimage:
        image = dist / appimage_name(version)
        if not image.is_file() or images != [image]:
            raise ValueError(f"ожидался ровно один {image.name}, найдено: {len(images)}")
        artifacts["appimage"] = _artifact(image)
    elif images:
        raise ValueError("в каталоге есть AppImage, но выпуск без него (нет --appimage)")
    result: dict[str, Any] = {
        "schema": SCHEMA,
        "version": version,
        "published_at": published_at(date),
        "min_astra": MIN_ASTRA,
        "release_url": f"https://github.com/{REPOSITORY}/releases/tag/v{version}",
        "artifacts": artifacts,
    }
    (dist / "latest.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8"
    )
    return result


def main() -> int:
    """Разобрать аргументы командной строки."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--published-at")
    parser.add_argument(
        "--appimage", action="store_true", help="добавить artifacts.appimage (обязателен файл)"
    )
    args = parser.parse_args()
    try:
        generate(args.dist, args.version, args.published_at, appimage=args.appimage)
    except (OSError, ValueError) as exc:
        parser.exit(1, f"ОШИБКА: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
