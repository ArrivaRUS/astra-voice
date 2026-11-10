#!/usr/bin/env python3
"""Оракул подписи: код из main читает кандидат исключительно как данные Git."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]
from scripts.catalog_publish_check import (  # noqa: E402
    PublishError as PublishError,
)
from scripts.catalog_publish_check import (  # noqa: E402
    _pair,
    _revocations,
    check_history,
)

from astra_voice.models.catalog import CATALOG_MAX_BYTES as CATALOG_MAX_BYTES  # noqa: E402

HEX40 = re.compile(r"[0-9a-fA-F]{40}\Z")
HEX64 = re.compile(r"[0-9a-fA-F]{64}\Z")
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")


def validate_inputs(commit: str, sha256: str) -> None:
    if not HEX40.fullmatch(commit):
        raise PublishError("commit: требуется ровно 40 hex.")
    if not HEX64.fullmatch(sha256):
        raise PublishError("sha256: требуется ровно 64 hex.")


def git(root: Path, *args: str) -> bytes:
    result = subprocess.run(["git", *args], cwd=root, capture_output=True, check=False, timeout=60)
    if result.returncode:
        # Не выводим недоверенные сообщения Git в workflow commands / сводку.
        raise PublishError("Не удалось прочитать объект кандидата или базу сравнения Git.")
    return result.stdout


def blob(root: Path, ref: str, path: str) -> bytes:
    spec = f"{ref}:{path}"
    if git(root, "cat-file", "-t", spec).strip() != b"blob":
        raise PublishError("Ожидался файл Git (blob).")
    if int(git(root, "cat-file", "-s", spec)) > CATALOG_MAX_BYTES:
        raise PublishError("Превышен CATALOG_MAX_BYTES.")
    return git(root, "show", spec)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise PublishError("Повторяющийся ключ JSON.")
        result[key] = value
    return result


def document(raw: bytes) -> dict[str, Any]:
    if len(raw) > CATALOG_MAX_BYTES:
        raise PublishError("Превышен CATALOG_MAX_BYTES.")
    try:
        value = json.loads(raw, object_pairs_hook=_unique_object)
        # json.loads допускает NaN / Infinity; JSON их не допускает.
        json.dumps(value, allow_nan=False)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise PublishError("Неверный JSON.") from exc
    if not isinstance(value, dict):
        raise PublishError("Ожидался объект JSON.")
    return value


def models(doc: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    entries = doc.get("models")
    if not isinstance(entries, list):
        raise PublishError("models должен быть списком.")
    for entry in entries:
        identifier = entry.get("id") if isinstance(entry, dict) else None
        if not isinstance(identifier, str) or not IDENTIFIER.fullmatch(identifier):
            raise PublishError("Неверный идентификатор модели.")
        if identifier in result:
            raise PublishError("Повторяющийся идентификатор модели.")
        result[identifier] = entry
    return result


def code(value: object) -> str:
    # Каждый недоверенный символ кодируется числовой HTML-сущностью: ни Markdown,
    # ни HTML, ни переводы строк не могут подделать заголовки сводки владельца.
    text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    return "<code>" + "".join(f"&#{ord(char)};" for char in text) + "</code>"


def equal(before: Any, after: Any) -> bool:
    """Равенство JSON без потери типов: true != 1, 135 != 135.0."""
    if type(before) is not type(after):
        return False
    if isinstance(before, dict):
        return before.keys() == after.keys() and all(equal(before[k], after[k]) for k in before)
    if isinstance(before, list):
        return len(before) == len(after) and all(
            equal(a, b) for a, b in zip(before, after, strict=True)
        )
    return bool(before == after)


MISSING = object()


def value_code(value: Any) -> str:
    return "отсутствует" if value is MISSING else code(value)


def differences(before: Any, after: Any, path: str) -> list[str]:
    """Вложенные пути JSON Pointer; отсутствующий ключ отличается от null."""
    if before is after or (before is not MISSING and after is not MISSING and equal(before, after)):
        return []
    if isinstance(before, dict) and isinstance(after, dict):
        return [
            line
            for key in sorted(before.keys() | after.keys())
            for line in differences(
                before.get(key, MISSING),
                after.get(key, MISSING),
                path + "/" + key.replace("~", "~0").replace("/", "~1"),
            )
        ]
    if isinstance(before, list) and isinstance(after, list):
        return [
            line
            for i in range(max(len(before), len(after)))
            for line in differences(
                before[i] if i < len(before) else MISSING,
                after[i] if i < len(after) else MISSING,
                f"{path}/{i}",
            )
        ]
    return [f"- {code(path)}: {value_code(before)} → {value_code(after)}"]


def file_groups(value: Any) -> dict[str, list[dict[str, Any]]] | None:
    # Группы сохраняют даже повторяющиеся path и не скрывают элементы списка.
    if not isinstance(value, list):
        return None
    result: dict[str, list[dict[str, Any]]] = {}
    for entry in value:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            return None
        result.setdefault(entry["path"], []).append(entry)
    return result


def file_changes(before: Any, after: Any) -> list[str]:
    old, new = file_groups(before), file_groups(after)
    if old is None or new is None:
        return differences(before, after, "/files")
    lines: list[str] = []
    for path in sorted(old.keys() | new.keys()):
        left, right = old.get(path, MISSING), new.get(path, MISSING)
        if left is not MISSING and right is not MISSING and equal(left, right):
            continue
        status = "добавлен" if left is MISSING else "удалён" if right is MISSING else "изменён"
        lines += [
            f"Файл {code(path)}: {status}.",
            "",
            f"Было: {value_code(left)} → стало: {value_code(right)}",
            "",
        ]
        if status == "изменён":
            pointer = "/files/" + path.replace("~", "~0").replace("/", "~1")
            lines += differences(left, right, pointer) + [""]
    # Порядок списка файлов тоже не теряется, даже при одинаковом наборе path.
    if not equal([x["path"] for x in before], [x["path"] for x in after]):
        lines += [
            "Порядок paths: "
            + code([x["path"] for x in before])
            + " → "
            + code([x["path"] for x in after]),
            "",
        ]
    return lines


def summary(
    commit: str,
    digest: str,
    raw: bytes,
    bases: list[tuple[str, bytes]],
    main_commit: str,
) -> str:
    candidate = document(raw)
    new_models = models(candidate)
    main = document(bases[0][1])
    main_models = models(main)
    added = sorted(new_models.keys() - main_models.keys())
    removed = sorted(main_models.keys() - new_models.keys())
    changed = [
        k
        for k in new_models.keys() & main_models.keys()
        if not equal(main_models[k], new_models[k])
    ]
    rev_changed = "да" if not equal(main["revoked"], candidate["revoked"]) else "нет"
    lines = [
        "# Подпись каталога: проверка перед Approve",
        "",
        f"Относительно main: добавлено {len(added)}, удалено {len(removed)}, "
        f"изменено {len(changed)} записей.",
        "",
        f"Отзывы изменились: {rev_changed}. Всего записей в кандидате: {len(new_models)}.",
        "",
        f"SHA256 каталога: `{digest}`",
        "",
        f"Снимок main, с которым сравнили: `{main_commit}`",
        "",
        f"Коммит кандидата: `{commit}`",
        "",
        "Будет подписан только catalog.json из артефакта этого запуска.",
        "Сверьте полную сумму и изменения ниже перед Approve окружения release.",
    ]
    for label, old_raw in bases:
        old = document(old_raw)
        old_models = models(old)
        lines += [
            "",
            f"## Сравнение: {code(label)}",
            "",
            f"trust_epoch: {old['trust_epoch']} → {candidate['trust_epoch']}",
            "",
            f"serial: {old['serial']} → {candidate['serial']}",
            "",
            f"Записей: {len(old_models)} → {len(new_models)}",
            "",
            "Добавлены: " + code(sorted(new_models.keys() - old_models.keys())),
            "",
            "Удалены: " + code(sorted(old_models.keys() - new_models.keys())),
            "",
        ]
        for identifier in sorted(new_models.keys() | old_models.keys()):
            before: Any = old_models.get(identifier, MISSING)
            after: Any = new_models.get(identifier, MISSING)
            if before is after or (
                before is not MISSING and after is not MISSING and equal(before, after)
            ):
                continue
            status = (
                "добавлена" if before is MISSING else "удалена" if after is MISSING else "изменена"
            )
            lines += [f"### Запись {code(identifier)}: {status}", ""]
            if status != "изменена":
                lines += [f"Полное содержимое: {value_code(before)} → {value_code(after)}", ""]
            else:
                lines += differences(
                    {k: v for k, v in before.items() if k != "files"},
                    {k: v for k, v in after.items() if k != "files"},
                    "",
                ) + [""]
            lines += file_changes(
                before.get("files", MISSING) if before is not MISSING else [],
                after.get("files", MISSING) if after is not MISSING else [],
            )
        lines += ["### Поля верхнего уровня каталога: было → стало", ""]
        lines += differences(
            {k: v for k, v in old.items() if k not in {"models", "revoked"}},
            {k: v for k, v in candidate.items() if k not in {"models", "revoked"}},
            "",
        ) or ["- нет"]
        lines += ["", "### Изменения содержимого отзывов", ""]
        if equal(old["revoked"], candidate["revoked"]):
            lines.append("Отзывы не менялись.")
        else:
            # Полные списки и diff по индексам: дубли/перестановки в базе не теряются.
            lines += [
                "Полный список было: " + code(old["revoked"]),
                "",
                "Полный список стало: " + code(candidate["revoked"]),
                "",
            ]
            lines += differences(old["revoked"], candidate["revoked"], "/revoked")
    return "\n".join(lines) + "\n"


def check_data(raw: bytes, schema: bytes, digest: str, bases: list[tuple[str, bytes]]) -> None:
    if len(raw) > CATALOG_MAX_BYTES:
        raise PublishError("Превышен CATALOG_MAX_BYTES.")
    if hashlib.sha256(raw).hexdigest() != digest.lower():
        raise PublishError("sha256 каталога не совпадает с входом.")
    doc = document(raw)
    document(schema)  # Схема — тоже только JSON; ссылки из неё не загружаются.
    if doc.get("schema_sha256") != hashlib.sha256(schema).hexdigest():
        raise PublishError("schema_sha256 не совпадает с байтами схемы кандидата.")
    _pair(raw)
    _revocations(raw)
    pairs = [(item["model_id"], item["revision"]) for item in doc["revoked"]]
    if len(pairs) != len(set(pairs)):
        raise PublishError("Повторяющаяся пара model_id/revision в revoked кандидата.")
    models(doc)
    for label, previous in bases:
        document(previous)
        check_history(raw, previous, label=label, strict=True)


def check(root: Path, commit: str, digest: str, output: Path, main_commit: str) -> str:
    validate_inputs(commit, digest)
    if not HEX40.fullmatch(main_commit):
        raise PublishError("Неверная ссылка main запуска.")
    output = output.resolve()
    if output.is_relative_to(root.resolve()):
        raise PublishError("Файлы кандидата должны находиться вне рабочего дерева.")
    # Ровно объект commit; тег/дерево вместо коммита не принимается.
    if git(root, "cat-file", "-t", commit).strip() != b"commit":
        raise PublishError("Кандидат не является коммитом.")
    latest_tag = (
        git(root, "describe", "--tags", "--match", "v*", "--abbrev=0", main_commit).decode().strip()
    )
    bases = [
        ("main (снимок запуска)", blob(root, main_commit, "data/catalog.json")),
        ("последний тег " + latest_tag, blob(root, "refs/tags/" + latest_tag, "data/catalog.json")),
    ]
    raw = blob(root, commit, "data/catalog.json")
    schema = blob(root, commit, "data/catalog.schema.json")
    check_data(raw, schema, digest, bases)
    result = summary(commit.lower(), digest.lower(), raw, bases, main_commit.lower())
    if len(result.encode("utf-8")) > CATALOG_MAX_BYTES:
        raise PublishError("Сводка превышает лимит GitHub; разделите изменение каталога.")
    output.mkdir(parents=True, exist_ok=False)
    (output / "catalog.json").write_bytes(raw)
    (output / "catalog.schema.json").write_bytes(schema)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--main-commit", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = check(ROOT, args.commit, args.sha256, args.output, args.main_commit)
        with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a", encoding="utf-8") as stream:
            stream.write(result)
        with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as stream:
            stream.write(f"sha256={args.sha256.lower()}\n")
    except (PublishError, OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        print(f"ОШИБКА: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
