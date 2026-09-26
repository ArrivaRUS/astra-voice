#!/usr/bin/env python3
"""Воспроизводимая сборка встроенного каталога моделей.

Источник истины — `data/catalog-source.json` (что показываем человеку) плюс
метаданные Hugging Face по закреплённой ревизии (сколько весит файл и какая у
него sha256). Метаданные лежат снимком в `data/catalog-hf-cache.json`, поэтому
сборка и проверка работают без сети; сеть нужна только для `--refresh`.

    python3 scripts/build_manifest.py --refresh   # обновить снимок метаданных (сеть)
    python3 scripts/build_manifest.py             # пересобрать data/catalog.json
    python3 scripts/build_manifest.py --check     # сравнить с репозиторием (CI, без сети)
    python3 scripts/build_manifest.py --sign --homedir <каталог GnuPG с подключом S1>

Правило оперативной памяти — из `research/catalog-numbers.md`: сумма байт
`.onnx`-файлов рантайма, умноженная на 1,85 и округлённая вверх до целых МБ.
Это оценка, а не замер, поэтому у записи стоит `ram_estimated: true`.

Веса моделей скрипт не качает: размеры и sha256 LFS-файлов отдаёт само API.
Мелкие не-LFS файлы (конфиги, словари) API отдаёт только с git-sha1, поэтому
для них `--refresh` скачивает сам файл и считает sha256; размер ограничен
`MAX_INLINE_BYTES`, больше — отказ.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import subprocess  # nosec B404 — вызывается только явный gpg с фиксированным argv
import sys
import tempfile
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
DATA = REPO / "data"
SOURCE_PATH = DATA / "catalog-source.json"
CACHE_PATH = DATA / "catalog-hf-cache.json"
CATALOG_PATH = DATA / "catalog.json"
SCHEMA_PATH = DATA / "catalog.schema.json"
SIGNATURE_PATH = DATA / "catalog.json.sig"
KEYRING_PATH = DATA / "keys" / "release.gpg"

API_BASE = "https://huggingface.co/api/models"
RESOLVE_BASE = "https://huggingface.co"
MAX_INLINE_BYTES = 1 << 20
RAM_FACTOR = 1.85
REQUEST_TIMEOUT_S = 60.0


class BuildError(Exception):
    """Ошибка сборки каталога с понятным сообщением."""


def _read_json(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as stream:
            document: Any = json.loads(stream.read().decode("utf-8"))
    except OSError as exc:
        raise BuildError(f"Не удалось прочитать {path}: {exc}") from exc
    except ValueError as exc:
        raise BuildError(f"Не удалось разобрать JSON {path}: {exc}") from exc
    if not isinstance(document, dict):
        raise BuildError(f"В {path} ожидался объект JSON.")
    return document


def _dump(document: dict[str, Any]) -> bytes:
    """Детерминированный текст: те же байты при тех же входных данных."""
    return (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _cache_key(repo: str, revision: str) -> str:
    return f"{repo}@{revision}"


def _get(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "astra-voice-build-manifest"})
    if not url.startswith("https://huggingface.co/"):
        raise BuildError(f"Запрещённый адрес: {url}")
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_S) as response:  # nosec B310
        data: bytes = response.read(MAX_INLINE_BYTES + 1)
    return data


def _tree(repo: str, revision: str) -> list[dict[str, Any]]:
    url = f"{API_BASE}/{repo}/tree/{revision}?recursive=true"
    request = urllib.request.Request(url, headers={"User-Agent": "astra-voice-build-manifest"})
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_S) as response:  # nosec B310
        payload: Any = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, list):
        raise BuildError(f"Неожиданный ответ дерева {repo}@{revision}.")
    return [item for item in payload if isinstance(item, dict)]


def refresh_cache(source: dict[str, Any]) -> dict[str, Any]:
    """Тянет размеры и sha256 нужных файлов по закреплённым ревизиям."""
    repos: dict[str, dict[str, Any]] = {}
    for model in source["models"]:
        key = _cache_key(model["repo"], model["revision"])
        repos.setdefault(key, {"repo": model["repo"], "revision": model["revision"], "files": {}})
        wanted = repos[key]["files"]
        for path in model["files"]:
            wanted.setdefault(path, None)
    for key in sorted(repos):
        entry = repos[key]
        tree = {
            item["path"]: item for item in _tree(entry["repo"], entry["revision"]) if "path" in item
        }
        files: dict[str, dict[str, Any]] = {}
        for path in sorted(entry["files"]):
            item = tree.get(path)
            if item is None:
                raise BuildError(f"{key}: файла {path} нет на закреплённой ревизии.")
            size = int(item["size"])
            lfs = item.get("lfs")
            if isinstance(lfs, dict) and isinstance(lfs.get("oid"), str):
                files[path] = {"size": size, "sha256": lfs["oid"], "lfs": True}
                continue
            if size > MAX_INLINE_BYTES:
                raise BuildError(
                    f"{key}: {path} не в LFS и больше {MAX_INLINE_BYTES} Б — sha256 не посчитать."
                )
            raw = _get(f"{RESOLVE_BASE}/{entry['repo']}/resolve/{entry['revision']}/{path}")
            if len(raw) != size:
                raise BuildError(f"{key}: размер {path} не совпал с метаданными.")
            files[path] = {"size": size, "sha256": hashlib.sha256(raw).hexdigest(), "lfs": False}
        entry["files"] = files
    return {
        "$comment": (
            "Снимок метаданных Hugging Face по закреплённым ревизиям: размеры и sha256. "
            "Обновляется scripts/build_manifest.py --refresh; сборка и --check сеть не трогают."
        ),
        "fetched_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "repos": {key: repos[key] for key in sorted(repos)},
    }


def _min_ram_mb(files: list[dict[str, Any]]) -> int:
    weights = sum(file["size"] for file in files if file["path"].endswith(".onnx"))
    if weights <= 0:
        raise BuildError("В записи нет ни одного файла весов .onnx.")
    return math.ceil(weights * RAM_FACTOR / 1_000_000)


def _entry(model: dict[str, Any], cache: dict[str, Any], sources: dict[str, str]) -> dict[str, Any]:
    key = _cache_key(model["repo"], model["revision"])
    known = cache["repos"].get(key)
    if known is None:
        raise BuildError(f"В снимке метаданных нет {key}; выполните --refresh.")
    files: list[dict[str, Any]] = []
    for path in model["files"]:
        meta = known["files"].get(path)
        if meta is None:
            raise BuildError(f"В снимке метаданных нет файла {path} для {key}.")
        files.append(
            {
                "path": path,
                "sha256": meta["sha256"],
                "size": meta["size"],
                "url_path": f"/{model['repo']}/resolve/{model['revision']}/{path}",
            }
        )
    entry: dict[str, Any] = {
        "id": model["id"],
        "revision": model["revision"],
        "name": model["name"],
        "description": model["description"],
        "size_bytes": sum(file["size"] for file in files),
        "min_ram_mb": _min_ram_mb(files),
        "ram_estimated": bool(model["ram_estimated"]),
        "layout": model["layout"],
        "variant": model["variant"],
        "recommended": bool(model["recommended"]),
        "host": model["host"],
        "vendor": model["vendor"],
        "vendor_short": model["vendor_short"],
        "languages": list(model["languages"]),
        "language_tag": model["language_tag"],
        "punctuation": bool(model["punctuation"]),
        "license": model["license"],
        "domestic": bool(model["domestic"]),
    }
    mirrors = [str(host) for host in model.get("mirrors", [])]
    if mirrors:
        # Порядок источников берём из таблицы как есть: hf → github → корпоративный.
        if len(set(mirrors)) != len(mirrors) or model["host"] in mirrors:
            raise BuildError(f"Повторяющийся запасной источник у записи {model['id']}.")
        entry["mirrors"] = mirrors
    metrics = {
        name: {"value": value, "source": sources[name]}
        for name, value in sorted(model["metrics"].items())
        if name in sources
    }
    if metrics:
        entry["metrics"] = metrics
    entry["files"] = files
    return entry


def build(
    source: dict[str, Any], cache: dict[str, Any], *, serial: int, generated_at: str
) -> dict[str, Any]:
    """Собирает документ каталога; порядок ключей фиксирован."""
    schema_sha256 = hashlib.sha256(SCHEMA_PATH.read_bytes()).hexdigest()
    sources: dict[str, str] = source["metric_sources"]
    entries = [_entry(model, cache, sources) for model in source["models"]]
    if len({entry["id"] for entry in entries}) != len(entries):
        raise BuildError("В таблице-источнике повторяются идентификаторы моделей.")
    if sum(1 for entry in entries if entry["recommended"]) != 1:
        raise BuildError("Рекомендованная запись должна быть ровно одна.")
    return {
        "manifest_version": int(source["manifest_version"]),
        "schema_sha256": schema_sha256,
        "serial": serial,
        "trust_epoch": int(source["trust_epoch"]),
        "generated_at": generated_at,
        "publisher": source["publisher"],
        "revoked": list(source["revoked"]),
        "models": entries,
    }


def _payload(document: dict[str, Any]) -> dict[str, Any]:
    """Часть документа, по которой решается, менялся ли каталог."""
    return {key: value for key, value in document.items() if key not in {"serial", "generated_at"}}


def _current() -> dict[str, Any] | None:
    if not CATALOG_PATH.is_file():
        return None
    return _read_json(CATALOG_PATH)


def _sign(homedir: Path, key: str) -> None:
    expected = key.removesuffix("!").upper()
    if re.fullmatch(r"[0-9A-F]{40}", expected) is None:
        raise BuildError("--key должен быть отпечатком подключа (40 hex)")
    argv = [
        "gpg",
        "--homedir",
        str(homedir),
        "--batch",
        "--yes",
        "--local-user",
        expected + "!",
        "--detach-sign",
        "--output",
        str(SIGNATURE_PATH),
        str(CATALOG_PATH),
    ]
    result = subprocess.run(argv, check=False)  # nosec B603 — argv фиксирован, shell не нужен
    if result.returncode != 0:
        raise BuildError("gpg не подписал каталог.")
    with tempfile.TemporaryDirectory(prefix="astra-voice-gpgv-") as empty_home:
        verify_argv = [
            "gpgv",
            "--status-fd",
            "1",
            "--homedir",
            empty_home,
            "--keyring",
            str(KEYRING_PATH.resolve()),
            str(SIGNATURE_PATH),
            str(CATALOG_PATH),
        ]
        try:
            verified = subprocess.run(  # nosec B603 — argv фиксирован, shell не нужен
                verify_argv, capture_output=True, text=True, check=False
            )
        except OSError as exc:
            raise BuildError(f"gpgv не подтвердил подпись встроенной связкой: {exc}") from exc
    valid = [
        line.split()[2].upper()
        for line in verified.stdout.splitlines()
        if line.startswith("[GNUPG:] VALIDSIG ") and len(line.split()) > 2
    ]
    if verified.returncode != 0 or len(valid) != 1:
        raise BuildError("gpgv не подтвердил подпись встроенной связкой")
    if valid[0] != expected:
        raise BuildError(f"каталог подписан не тем ключом: {valid[0]}, ожидался {expected}")
    print(f"подписан: {SIGNATURE_PATH.relative_to(REPO)}")


def main(argv: list[str] | None = None) -> int:
    """Разбирает аргументы и выполняет одну из трёх операций."""
    parser = argparse.ArgumentParser(description="Сборка data/catalog.json.")
    parser.add_argument(
        "--refresh", action="store_true", help="обновить снимок метаданных Hugging Face (сеть)"
    )
    parser.add_argument(
        "--check", action="store_true", help="сравнить с репозиторием, ничего не записывать"
    )
    parser.add_argument("--sign", action="store_true", help="подписать каталог после сборки")
    parser.add_argument("--homedir", type=Path, help="каталог GnuPG с закрытым ключом подписи")
    parser.add_argument(
        "--key",
        default="7602A029F0E34CD2344A9CDA657ED04689FF4D79",
        help="отпечаток ключа подписи",
    )
    args = parser.parse_args(argv)
    if args.check and args.refresh:
        parser.error("--check и --refresh вместе не работают.")
    if args.sign and args.homedir is None:
        parser.error("--sign требует --homedir с каталогом GnuPG.")

    try:
        source = _read_json(SOURCE_PATH)
        if args.refresh:
            CACHE_PATH.write_bytes(_dump(refresh_cache(source)))
            print(f"обновлён снимок: {CACHE_PATH.relative_to(REPO)}")
            return 0
        cache = _read_json(CACHE_PATH)
        current = _current()
        if args.check:
            if current is None:
                raise BuildError("data/catalog.json отсутствует — сравнивать не с чем.")
            document = build(
                source,
                cache,
                serial=int(current["serial"]),
                generated_at=str(current["generated_at"]),
            )
            if _dump(document) != CATALOG_PATH.read_bytes():
                print("каталог в репозитории отличается от пересобранного", file=sys.stderr)
                return 1
            print(
                f"регенерация чистая: {len(document['models'])} записей, "
                f"серийный номер {document['serial']}"
            )
            return 0
        serial = 1
        generated_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        if current is not None:
            serial = int(current["serial"])
            probe = build(source, cache, serial=serial, generated_at=str(current["generated_at"]))
            if _payload(probe) == _payload(current):
                generated_at = str(current["generated_at"])
            else:
                serial += 1
        document = build(source, cache, serial=serial, generated_at=generated_at)
        CATALOG_PATH.write_bytes(_dump(document))
        print(
            f"записан {CATALOG_PATH.relative_to(REPO)}: "
            f"{len(document['models'])} записей, серийный номер {serial}"
        )
        if args.sign:
            _sign(args.homedir, args.key)
        return 0
    except BuildError as exc:
        print(f"ошибка: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
