#!/usr/bin/env python3
"""Гейт T-06: инварианты GitHub Actions проверяются машиной, а не глазами.

Что и почему запрещено (`docs/threat-model.md` §5, строка M1; У32):

* **`pull_request_target`** — запускает workflow с секретами репозитория в
  контексте форка. Одного такого триггера достаточно, чтобы утёк ключ подписи;
* **`uses:` без SHA** — тег `@v4` подвижен: владелец Action (или тот, кто угнал
  его аккаунт) меняет код под тем же тегом. Пин по 40 hex — единственная фиксация;
* **широкие `permissions`** — `GITHUB_TOKEN` по умолчанию слишком силён;
  `contents: write` нужен только job'у релиза;
* **секреты вне двух заданий** — Environment `release` с required reviewer:
  `ci.yml:release` по тегу и `catalog-sign.yml:sign` с main после проверки данных;
* **`${{ … }}` внутри `run:`** — подстановка идёт до запуска shell, поэтому
  заголовок PR вида `"; rm -rf /` исполняется. Значения передаются через `env:`;
* **широкий `paths-ignore`** — позволяет вывести код из-под проверок;
* **релиз без `needs: engine`** — позволяет выпустить пакет без проверки движка;
* **локальные послабления сборки** (`ASTRA_VOICE_APPIMAGE_ALLOW_TODO_HASH`) — пускают в
  образ незакреплённые колёса; в workflow их имени быть не должно (arch/appimage.md §9.2).

    scripts/ci_lint.py .github/workflows/*.yml
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from pathlib import Path
from typing import Any

import yaml

#: Job'ы, которым разрешено писать в репозиторий (публикация релиза).
# Обновлять вручную в том же PR с ИБ-ревью; точные байты, включая комментарии.
CATALOG_SIGN_SHA256 = "22c96337107cdc8cdaf32cc24fea12cbf70981144e3d3672033cb851a51380f0"
CATALOG_SEAL_MESSAGE = (
    "workflow подписи изменён — обнови закреплённую сумму в том же PR и пройди ИБ-ревью"
)

ALLOWED_WRITE_JOBS = frozenset({"release"})
# ci.yml.proposed — проверяемый офлайн-шаблон того же ci.yml, не workflow GitHub.
RELEASE_JOBS = frozenset({("ci.yml", "release"), ("ci.yml.proposed", "release")})
SIGNING_JOBS = RELEASE_JOBS | {("catalog-sign.yml", "sign")}
#: Разрешённые значения прав на уровне workflow.
READ_ONLY = {"read", "none"}
ALLOWED_PATHS_IGNORE = frozenset(
    {
        "HEARTBEAT.md",
        "decisions/**",
        ".patches/**",
        "research/**",
        "docs/status.md",
        "docs/plans.md",
        "docs/test-plan.md",
        # Архитектура и спайки не попадают ни в .deb, ни в AppImage (arch/appimage.md §9.4 п.3).
        "arch/**",
        "spikes/**",
    }
)

#: Переменные окружения только для локальной проверки: в CI запрещены.
LOCAL_ONLY_ENV = ("ASTRA_VOICE_APPIMAGE_ALLOW_TODO_HASH",)

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
        if (where.split(":")[0], job_id) in RELEASE_JOBS and scope == "contents":
            continue
        out.append(f"{where}: {scope}: {value} — запись разрешена только job'у релиза")
    return out


def _secret_reference(value: Any) -> bool:
    """Все формы контекста secrets, включая format()/toJSON() и secrets: inherit."""
    if isinstance(value, dict):
        return any(
            (isinstance(key, str) and key.casefold() == "secrets")
            or (
                key == "if"
                and isinstance(item, str)
                and bool(re.search(r"\bsecrets\b", item, re.I))
            )
            or _secret_reference(key)
            or _secret_reference(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_secret_reference(item) for item in value)
    return (
        isinstance(value, str) and "${{" in value and bool(re.search(r"\bsecrets\b", value, re.I))
    )


def _secret_problems(name: str, doc: dict[str, Any]) -> list[str]:
    out: list[str] = []
    if _secret_reference({key: value for key, value in doc.items() if key != "jobs"}):
        out.append(f"{name}: секрет подписи вне шага импорта (уровень workflow)")
    for job_id, job in _jobs(doc).items():
        where = f"{name}:{job_id}"
        if _secret_reference({key: value for key, value in job.items() if key != "steps"}):
            out.append(f"{where}: секрет подписи вне шага импорта (уровень задания)")
        steps = job.get("steps") or []
        import_name = (
            "Импорт S1 и подпись"
            if name == "catalog-sign.yml"
            else "Отсоединённая подпись SHA256SUMS"
        )
        import_steps = [
            step for step in steps if isinstance(step, dict) and step.get("name") == import_name
        ]
        for step in steps:
            if not isinstance(step, dict):
                continue
            cleaned = dict(step)
            env = step.get("env")
            if (
                (name, job_id) in SIGNING_JOBS
                and len(import_steps) == 1
                and step is import_steps[0]
                and step.get("shell") == "bash"
                and isinstance(step.get("run"), str)
                and "uses" not in step
                and isinstance(env, dict)
                and env.get("GPG_SIGNING_KEY") == "${{ secrets.GPG_SIGNING_KEY }}"
            ):
                cleaned["env"] = {
                    key: value for key, value in env.items() if key != "GPG_SIGNING_KEY"
                }
            if _secret_reference(cleaned):
                out.append(
                    f"{where}/{step.get('name', 'шаг')}: секрет подписи вне шага импорта; "
                    "secrets допустим только как точное env GPG_SIGNING_KEY шага импорта"
                )
    return out


def check_workflow(path: Path) -> list[str]:
    """Вернуть список нарушений в одном файле workflow (пустой = чисто)."""
    problems: list[str] = []
    raw = path.read_bytes()
    if path.name == "catalog-sign.yml" and hashlib.sha256(raw).hexdigest() != CATALOG_SIGN_SHA256:
        problems.append(f"{path.name}: {CATALOG_SEAL_MESSAGE}")
    text = raw.decode("utf-8")
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        return problems + [f"{path}: не разобрался YAML: {exc}"]
    if not isinstance(doc, dict):
        return problems + [f"{path}: ожидался словарь на верхнем уровне"]

    name = path.name
    problems += _secret_problems(name, doc)
    for variable in LOCAL_ONLY_ENV:
        if variable in text:
            problems.append(f"{name}: {variable} — только для локальной сборки, в CI запрещена")

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
    if isinstance(triggers, dict):
        for event, options in triggers.items():
            if not isinstance(options, dict) or "paths-ignore" not in options:
                continue
            ignored = options["paths-ignore"]
            where = f"{name}:on.{event}.paths-ignore"
            if not isinstance(ignored, list):
                problems.append(f"{where}: ожидался список разрешённых шаблонов")
                continue
            for pattern in ignored:
                if not isinstance(pattern, str) or pattern not in ALLOWED_PATHS_IGNORE:
                    problems.append(
                        f"{where}: шаблон {pattern!r} вне белого списка — "
                        "может вывести код из-под проверок"
                    )

    if "permissions" not in doc:
        problems.append(f"{name}: нет permissions на верхнем уровне (нужен contents: read)")
    else:
        problems += _perm_problems(name, doc["permissions"], None)

    workflow_scope = {key: value for key, value in doc.items() if key != "jobs"}
    if re.search(r"\bsecrets\b|GPG_SIGNING_KEY", yaml.safe_dump(workflow_scope)):
        problems.append(f"{name}: секрет подписи вне шага импорта (уровень workflow)")

    if name == "catalog-sign.yml":
        problems += _catalog_problems(doc, trigger_names)

    for job_id, job in _jobs(doc).items():
        where = f"{name}:{job_id}"
        problems += _perm_problems(where, job.get("permissions"), job_id)

        text = yaml.safe_dump(job, allow_unicode=True)
        uses_secret = bool(re.search(r"\bsecrets\b", text))
        if "uses" in job:
            problems.append(f"{where}: jobs.<id>.uses — переиспользуемые workflow запрещены")
        environment = job.get("environment")
        environment_name = environment.get("name") if isinstance(environment, dict) else environment
        if environment is not None and (
            not isinstance(environment_name, str) or "${{" in environment_name
        ):
            problems.append(f"{where}: вычисляемое имя Environment запрещено")
        if environment_name == "release" and (name, job_id) not in SIGNING_JOBS:
            problems.append(f"{where}: environment release разрешён только двум заданиям подписи")
        if uses_secret and (name, job_id) not in SIGNING_JOBS:
            problems.append(f"{where}: secrets разрешены только двум заданиям подписи")
        if uses_secret and not job.get("environment"):
            problems.append(f"{where}: использует secrets без environment (нужен `release`)")
        if job_id in ALLOWED_WRITE_JOBS:
            needs = job.get("needs", [])
            if needs != "engine" and (not isinstance(needs, list) or "engine" not in needs):
                problems.append(f"{where}: needs задачи релиза должен содержать `engine`")
            cond = str(job.get("if", ""))
            if "refs/tags/" not in cond:
                problems.append(f"{where}: job релиза должен быть ограничен тегом (`if:`)")
            if job.get("environment") != "release":
                problems.append(f"{where}: job релиза должен идти в environment `release`")

        job_scope = {key: value for key, value in job.items() if key != "steps"}
        if re.search(r"\bsecrets\b|GPG_SIGNING_KEY", yaml.safe_dump(job_scope)):
            problems.append(f"{where}: секрет подписи вне шага импорта (уровень задания)")

        for scope in (doc.get("env", {}), job.get("env", {})):
            if "GPG_SIGNING_KEY" in yaml.safe_dump(scope):
                problems.append(f"{where}: секрет подписи вне шага импорта")

        for i, step in enumerate(job.get("steps") or []):
            if not isinstance(step, dict):
                continue
            label = step.get("name") or f"шаг {i + 1}"
            uses = step.get("uses")
            if isinstance(uses, str) and not (
                _SHA_RE.match(uses) or _LOCAL_RE.match(uses) or _DOCKER_RE.match(uses)
            ):
                problems.append(f"{where}/{label}: uses без пина по SHA: {uses}")
            if "GPG_SIGNING_KEY" in yaml.safe_dump(step):
                import_name = (
                    "Импорт S1 и подпись"
                    if name == "catalog-sign.yml"
                    else "Отсоединённая подпись SHA256SUMS"
                )
                if (name, job_id) not in SIGNING_JOBS or step.get("name") != import_name:
                    problems.append(f"{where}/{label}: секрет подписи вне шага импорта")
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


def _catalog_problems(doc: dict[str, Any], triggers: set[str]) -> list[str]:
    """Дополнительная граница доверия: оракул данных → одобренная подпись."""
    out: list[str] = []

    def require(condition: bool, message: str) -> None:
        if not condition:
            out.append("catalog-sign.yml: " + message)

    require(triggers == {"workflow_dispatch"}, "единственный триггер — workflow_dispatch")
    trigger = _triggers(doc)
    dispatch = trigger.get("workflow_dispatch") if isinstance(trigger, dict) else None
    inputs = dispatch.get("inputs", {}) if isinstance(dispatch, dict) else {}
    require(
        set(inputs) == {"commit", "sha256"}
        and all(
            isinstance(value, dict)
            and value.get("required") is True
            and value.get("type") == "string"
            for value in inputs.values()
        ),
        "workflow_dispatch: нужны обязательные строковые commit и sha256",
    )
    require(doc.get("permissions") == {"contents": "read"}, "права workflow: только contents read")
    require(
        doc.get("concurrency") == {"group": "catalog-sign", "cancel-in-progress": False},
        "запуски подписи не должны накладываться",
    )
    require(not doc.get("env"), "env workflow запрещён: секрет только в шаге импорта")
    jobs = _jobs(doc)
    require(set(jobs) == {"check", "sign"}, "нужны ровно задания check и sign")
    for job_id in ("check", "sign"):
        job = jobs.get(job_id, {})
        rendered = yaml.safe_dump(job)
        require(
            job.get("permissions") == {"contents": "read"},
            f"{job_id}: права только contents read, без записи",
        )
        require(not job.get("env"), f"{job_id}: env задания запрещён")
        require("container" not in job, f"{job_id}: container запрещён, нужны штатные инструменты")
        require(not job.get("uses"), f"{job_id}: reusable workflow запрещён")
        if job_id == "check":
            require("environment" not in job, "check: Environment запрещён")
            require(
                not re.search(r"\bsecrets\b|GPG_SIGNING_KEY", rendered), "check: секреты запрещены"
            )
        else:
            require(job.get("environment") == "release", "sign: нужен Environment release")
            require(job.get("needs") == "check", "sign: нужен needs check")
        steps = job.get("steps") or []
        guard = (
            "if [ \"$GITHUB_REF\" != 'refs/heads/main' ]; then\n"
            "  echo '::error::Подпись каталога разрешена только с ветки main'\n"
            "  exit 1\nfi\n"
            "for program in git python3 gpg gpgv gpgconf sha256sum cut mktemp chmod rm base64; do\n"
            '  command -v "$program" >/dev/null || { echo "::error::Нет штатной программы '
            '$program; подпись отменена"; exit 1; }\n'
            "done"
        )
        require(
            bool(steps)
            and str(steps[0].get("run", "")).strip() == guard
            and steps[0].get("shell") == "bash"
            and "if" not in steps[0]
            and not steps[0].get("continue-on-error"),
            f"{job_id}: первым шагом нужен явный отказ вне main",
        )
        checkouts = []
        for step in steps:
            uses = step.get("uses", "")
            if uses:
                require(bool(_SHA_RE.fullmatch(uses)), f"{job_id}: все действия только по SHA")
                require(
                    uses.split("@")[0]
                    in {"actions/checkout", "actions/upload-artifact", "actions/download-artifact"},
                    f"{job_id}: недоверенное действие",
                )
            if uses.startswith("actions/checkout@"):
                checkouts.append(step)
                options = step.get("with", {})
                require(
                    options.get("ref") == "${{ github.sha }}"
                    and options.get("persist-credentials") is False
                    and "repository" not in options,
                    f"{job_id}: checkout только main снимка запуска, без произвольной ссылки",
                )
                if job_id == "sign":
                    require(
                        options.get("sparse-checkout-cone-mode") is False
                        and set(options.get("sparse-checkout", "").split())
                        == {
                            "/data/keys/release.gpg",
                            "/scripts/check_signing_secret.py",
                            "/src/astra_voice/__init__.py",
                            "/src/astra_voice/security/__init__.py",
                            "/src/astra_voice/security/verify.py",
                        },
                        "sign: checkout ограничен связкой и проверяющим кодом из main",
                    )
            step_text = yaml.safe_dump(step)
            if re.search(r"\bsecrets\b|GPG_SIGNING_KEY", step_text):
                require(
                    job_id == "sign"
                    and step.get("name") == "Импорт S1 и подпись"
                    and step.get("id") == "signing"
                    and step.get("env") == {"GPG_SIGNING_KEY": "${{ secrets.GPG_SIGNING_KEY }}"},
                    f"{job_id}: секрет допускается только в шаге импорта",
                )
            run = step.get("run", "")
            require(
                not re.search(r"\b(?:apt(?:-get)?|pip[0-9.]*)\b", run),
                f"{job_id}: установка пакетов запрещена",
            )
            tree_commands = "checkout|switch|clone|reset|restore|archive"
            if job_id == "sign":
                tree_commands += "|fetch|show"
            require(
                not re.search(
                    r"\bgit\s+(?:-[^\n]*\s+)?[\"']?(?:" + tree_commands + r")[\"']?\b", run
                ),
                f"{job_id}: извлечение произвольной ссылки запрещено",
            )
            if job_id == "sign":
                require(
                    not re.search(
                        r"\bgit\s+(?:-[^\n]*\s+)?"
                        r"(?:checkout|switch|fetch|clone|reset|restore|show|archive)\b",
                        run,
                    ),
                    "sign: извлечение произвольной ссылки запрещено",
                )
                require(
                    not re.search(r"\b(?:git\s+push|gh\s+release)\b", run),
                    "sign: push и публикация запрещены",
                )
        require(len(checkouts) == 1, f"{job_id}: нужен один checkout main")
        if job_id == "sign":
            imports = [i for i, step in enumerate(steps) if step.get("id") == "signing"]
            digests = [
                i for i, step in enumerate(steps) if step.get("name") == "Сверка точных байтов"
            ]
            require(
                len(imports) == len(digests) == 1 and digests[0] + 1 == imports[0],
                "sign: одна сверка байтов перед единственным импортом",
            )
        for step in steps:
            if step.get("id") in {"check", "signing"} or step.get("name") == "Сверка точных байтов":
                require(
                    "if" not in step and not step.get("continue-on-error"),
                    f"{job_id}: проверку или подпись нельзя пропустить/игнорировать",
                )
            if step.get("uses", "").startswith("actions/download-artifact@"):
                require(
                    job_id == "sign"
                    and step.get("with")
                    == {"name": "catalog-candidate", "path": "${{ runner.temp }}/catalog-to-sign"},
                    "sign: артефакт только из check этого запуска",
                )
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="проверка инвариантов GitHub Actions (T-06)")
    ap.add_argument("files", nargs="*", type=Path, default=[Path(".github/workflows")])
    args = ap.parse_args(argv)

    all_problems: list[str] = []
    paths: set[Path] = set()
    for entry in args.files:
        directory = entry if entry.is_dir() else entry.parent
        # Даже прежний вызов с *.yml не оставляет соседний *.yaml вне гейта.
        paths.update(directory.glob("*.yml"))
        paths.update(directory.glob("*.yaml"))
        if not entry.is_dir():
            paths.add(entry)
    for path in sorted(paths):
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
