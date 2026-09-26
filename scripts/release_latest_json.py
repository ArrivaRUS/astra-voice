#!/usr/bin/env python3
"""Создать указатель latest.json для одного deb в каталоге релиза."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path

# Минимальная поддерживаемая ОС — Astra Linux SE 1.8 (PRD/ТЗ).
# Пакет собирается под debian:12 = ALSE 1.8; см. packaging/Containerfile.
MIN_ASTRA = "1.8"


def published_at(raw: str | None) -> str:
    """Вернуть дату в строгом UTC ISO 8601 без долей секунды."""
    if raw is None:
        return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", raw):
        raise ValueError("--published-at должен иметь вид YYYY-MM-DDTHH:MM:SSZ")
    datetime.strptime(raw, "%Y-%m-%dT%H:%M:%SZ")
    return raw


def generate(dist: Path, version: str, date: str | None = None) -> dict[str, str]:
    """Найти единственный пакет и записать указатель."""
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        raise ValueError("версия должна иметь вид X.Y.Z")
    debs = sorted(dist.glob(f"astra-voice_{version}_*.deb"))
    if len(debs) != 1:
        raise ValueError(f"ожидался ровно один astra-voice_{version}_*.deb, найдено {len(debs)}")
    deb = debs[0]
    digest = hashlib.sha256(deb.read_bytes()).hexdigest()
    result = {
        "version": version,
        "deb": deb.name,
        "sha256": digest,
        "published_at": published_at(date),
        "min_astra": MIN_ASTRA,
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
    args = parser.parse_args()
    try:
        generate(args.dist, args.version, args.published_at)
    except (OSError, ValueError) as exc:
        parser.exit(1, f"ОШИБКА: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
