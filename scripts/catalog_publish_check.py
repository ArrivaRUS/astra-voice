#!/usr/bin/env python3
"""Офлайн-гейт подписи, точных байтов и защиты каталога выпуска от отката."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from astra_voice.core.release_identity import RELEASES_REPO, schema_id  # noqa: E402
from astra_voice.models.catalog import CatalogError, load_builtin  # noqa: E402
from astra_voice.security.verify import Verifier  # noqa: E402


class PublishError(Exception):
    """Нарушен инвариант публикации каталога."""


def _git(root: Path, *args: str) -> bytes:
    result = subprocess.run(["git", *args], cwd=root, capture_output=True, check=False, timeout=30)
    if result.returncode:
        raise PublishError(
            f"Не удалось прочитать Git ({' '.join(args)}): "
            f"{result.stderr.decode('utf-8', errors='replace').strip()}"
        )
    return result.stdout


def _previous_tag(root: Path) -> str:
    try:
        return (
            _git(root, "describe", "--tags", "--match", "v*", "--abbrev=0", "HEAD^")
            .decode()
            .strip()
        )
    except PublishError as exc:
        raise PublishError(
            "Не найден прошлый тег v* от HEAD^. Для первого выпуска с гейтом нужен "
            "v0.1.6; укажите --previous-tag v0.1.6, если он не является ближайшим тегом."
        ) from exc


def _pair(raw: bytes) -> tuple[int, int]:
    try:
        document = json.loads(raw)
    except ValueError as exc:
        raise PublishError("Каталог прошлого тега содержит неверный JSON.") from exc
    if not isinstance(document, dict):
        raise PublishError("В каталоге прошлого тега ожидался объект JSON.")
    epoch, serial = document.get("trust_epoch"), document.get("serial")
    if type(epoch) is not int or type(serial) is not int or epoch < 1 or serial < 1:
        raise PublishError("В каталоге прошлого тега неверные trust_epoch / serial.")
    return epoch, serial


def _revocations(raw: bytes) -> set[str]:
    # Старый каталог разбираем без новой схемы: смена схемы не снимает отзывы.
    document = json.loads(raw)
    revoked = document.get("revoked")
    if not isinstance(revoked, list):
        raise PublishError("В каталоге прошлого тега неверный список revoked.")
    pairs: set[str] = set()
    for item in revoked:
        if not isinstance(item, dict) or any(
            not isinstance(item.get(key), str) or not item[key] or "@" in item[key]
            for key in ("model_id", "revision")
        ):
            raise PublishError("В каталоге прошлого тега неверная пара revoked.")
        pairs.add(f"{item['model_id']}@{item['revision']}")
    return pairs


def check(
    root: Path,
    *,
    dist: Path | None = None,
    previous_tag: str | None = None,
    allow_unrevoke: tuple[str, ...] = (),
) -> None:
    repository = os.environ.get("GITHUB_REPOSITORY")
    if repository is not None and repository != RELEASES_REPO:
        raise PublishError(
            f"GITHUB_REPOSITORY={repository!r} не совпадает с RELEASES_REPO={RELEASES_REPO!r}."
        )
    data = root / "data"
    catalog = load_builtin(
        Verifier("catalog", keyring=data / "keys" / "release.gpg"),
        root=data,
        require_schema=True,
    )
    raw = (data / "catalog.json").read_bytes()
    name = f"catalog-{schema_id((data / 'catalog.schema.json').read_bytes())}.json"
    if dist is not None:
        for source, target in (("catalog.json", name), ("catalog.json.sig", name + ".sig")):
            if (dist / target).read_bytes() != (data / source).read_bytes():
                raise PublishError(f"Ассет {dist / target} побайтно не совпадает с data/{source}.")

    tag = _previous_tag(root) if previous_tag is None else previous_tag
    # Полное имя refs/tags исключает интерпретацию тега как опции или ревизии ветки.
    previous = _git(root, "show", f"refs/tags/{tag}:data/catalog.json")
    old_pair = _pair(previous)
    pair = catalog.trust_epoch, catalog.serial
    if pair[0] < old_pair[0]:
        raise PublishError(f"trust_epoch убыла: {pair[0]} < {old_pair[0]} (тег {tag}).")
    if raw != previous and pair <= old_pair:
        if pair == old_pair:
            raise PublishError(
                f"Равная пара {pair} при разных байтах / sha256: "
                f"{hashlib.sha256(raw).hexdigest()} != {hashlib.sha256(previous).hexdigest()}."
            )
        raise PublishError(f"Откат каталога: пара {pair} не больше {old_pair} (тег {tag}).")
    removed = _revocations(previous) - {
        f"{entry.model_id}@{entry.revision}" for entry in catalog.revoked
    }
    forbidden = removed - set(allow_unrevoke)
    if forbidden:
        raise PublishError("; ".join(f"потеря отзыва: {entry}" for entry in sorted(forbidden)))
    for entry in sorted(removed):
        print(f"Явно разрешено снятие отзыва: {entry} (--allow-unrevoke).")
    print(f"Гейт каталога пройден: {name}; прошлый тег {tag}, {old_pair} → {pair}.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist", type=Path, help="каталог ассетов выпуска")
    parser.add_argument("--previous-tag", help="тег сравнения (первый выпуск с гейтом: v0.1.6)")
    parser.add_argument(
        "--allow-unrevoke",
        action="append",
        default=[],
        metavar="ID@REV",
        help="явно разрешить снятие конкретного отзыва (можно повторять; только ручной выпуск)",
    )
    args = parser.parse_args(argv)
    try:
        check(
            ROOT,
            dist=args.dist,
            previous_tag=args.previous_tag,
            allow_unrevoke=tuple(args.allow_unrevoke),
        )
    except (PublishError, CatalogError, OSError, subprocess.SubprocessError) as exc:
        print(f"ОШИБКА: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
