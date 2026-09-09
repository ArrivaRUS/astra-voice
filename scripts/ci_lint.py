#!/usr/bin/env python3
"""Гейт T-06: инварианты GitHub Actions проверяются машиной, а не глазами.

Что и почему запрещено (`docs/threat-model.md` §5, строка M1; У32):

* **`pull_request_target`** — запускает workflow с секретами репозитория в
  контексте форка. Одного такого триггера достаточно, чтобы утёк ключ подписи;
* **`uses:` без SHA** — тег `@v4` подвижен: владелец Action (или тот, кто угнал
  его аккаунт) меняет код под тем же тегом. Пин по 40 hex — единственная фиксация;
* **широкие `permissions`** — `GITHUB_TOKEN` по умолчанию слишком силён;
  `contents: write` нужен только job'у релиза;
* **секреты вне Environment** — Environment `release` даёт required reviewer и
  ограничение по ветке/тегу; секрет в обычном job такой защиты не имеет;
* **`${{ … }}` внутри `run:`** — подстановка идёт до запуска shell, поэтому
  заголовок PR вида `"; rm -rf /` исполняется. Значения передаются через `env:`.

    scripts/ci_lint.py .github/workflows/*.yml
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any

import yaml

#: Job'ы, которым разрешено писать в репозиторий (публикация релиза).
ALLOWED_WRITE_JOBS = frozenset({"release"})
#: Разрешённые значения прав на уровне workflow.
READ_ONLY = {"read", "none"}

_SHA_RE = re.compile(r"^[^@\s]+/[^@\s]+@[0-9a-f]{40}(\s|$)")
_LOCAL_RE = re.compile(r"^\./")
_DOCKER_RE = re.compile(r"^docker://")
_EXPR_RE = re.compile(r"\$\{\{\s*(?P<expr>[^}]+?)\s*\}\}")
#: Что нельзя подставлять прямо в shell: всё, что пишет посторонний человек.
_UNSAFE_IN_RUN = re.compile(
    r"\b(github\.event\b|github\.head_ref\b|inputs\.|github\.actor\b)",
)


def _jobs(doc: dict[str, Any]) -> dict[str, dict[str, Any]]:
    jobs = doc.get("jobs") or {}
    return {k: v for k, v in jobs.items() if isinstance(v, dict)}


def _triggers(doc: dict[str, Any]) -> Any:
    # YAML 1.1 разбирает голое `on:` как булево True — отсюда два ключа.
    raw: dict[Any, Any] = doc
    return raw.get("on", raw.get(True))


def _perm_problems(where: str, perms: Any, job_id: str | None) -> list[str]:
    out: list[str] = []
    if perms is None:
        return out
    if isinstance(perms, str):
        if perms != "read-all":
            out.append(f"{where}: permissions: {perms!r} — допустимо только read-all")
        return out
    if not isinstance(perms, dict):
        out.append(f"{where}: permissions неожиданного вида: {perms!r}")
        return out
    for scope, value in perms.items():
        if value in READ_ONLY:
            continue
        if job_id in ALLOWED_WRITE_JOBS and scope == "contents":
            continue
        out.append(f"{where}: {scope}: {value} — запись разрешена только job'у релиза")
    return out


def check_workflow(path: Path) -> list[str]:
    """Вернуть список нарушений в одном файле workflow (пустой = чисто)."""
    problems: list[str] = []
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        return [f"{path}: не разобрался YAML: {exc}"]
    if not isinstance(doc, dict):
        return [f"{path}: ожидался словарь на верхнем уровне"]

    name = path.name

    triggers = _triggers(doc)
    trigger_names = (
        set(triggers)
        if isinstance(triggers, dict)
        else {triggers}
        if isinstance(triggers, str)
        else set(triggers or [])
    )
    if "pull_request_target" in trigger_names:
        problems.append(f"{name}: pull_request_target запрещён (секреты в контексте форка)")

    if "permissions" not in doc:
        problems.append(f"{name}: нет permissions на верхнем уровне (нужен contents: read)")
    else:
        problems += _perm_problems(name, doc["permissions"], None)

    for job_id, job in _jobs(doc).items():
        where = f"{name}:{job_id}"
        problems += _perm_problems(where, job.get("permissions"), job_id)

        text = yaml.safe_dump(job, allow_unicode=True)
        uses_secret = "secrets." in text
        if uses_secret and not job.get("environment"):
            problems.append(f"{where}: использует secrets без environment (нужен `release`)")
        if job_id in ALLOWED_WRITE_JOBS:
            cond = str(job.get("if", ""))
            if "refs/tags/" not in cond:
                problems.append(f"{where}: job релиза должен быть ограничен тегом (`if:`)")
            if job.get("environment") != "release":
                problems.append(f"{where}: job релиза должен идти в environment `release`")

        for i, step in enumerate(job.get("steps") or []):
            if not isinstance(step, dict):
                continue
            label = step.get("name") or f"шаг {i + 1}"
            uses = step.get("uses")
            if isinstance(uses, str) and not (
                _SHA_RE.match(uses) or _LOCAL_RE.match(uses) or _DOCKER_RE.match(uses)
            ):
                problems.append(f"{where}/{label}: uses без пина по SHA: {uses}")
            run = step.get("run")
            if isinstance(run, str):
                for m in _EXPR_RE.finditer(run):
                    expr = m.group("expr")
                    if _UNSAFE_IN_RUN.search(expr):
                        problems.append(
                            f"{where}/{label}: ${{{{ {expr} }}}} прямо в run — "
                            "подставлять через env, иначе это инъекция в shell"
                        )
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="проверка инвариантов GitHub Actions (T-06)")
    ap.add_argument("files", nargs="+", type=Path)
    args = ap.parse_args(argv)

    all_problems: list[str] = []
    for path in args.files:
        if not path.is_file():
            all_problems.append(f"{path}: нет такого файла")
            continue
        found = check_workflow(path)
        print(f"{path}: {'ok' if not found else f'{len(found)} нарушений'}")
        all_problems += found

    if all_problems:
        print("\nнарушения:", file=sys.stderr)
        for p in all_problems:
            print(f"  - {p}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
