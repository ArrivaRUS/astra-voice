"""Тесты `scripts/ci_lint.py` — гейт T-06 проверяется сам.

Линтер, который ничего не ловит, опаснее отсутствия линтера: он создаёт
ощущение проверки. Поэтому на каждый инвариант — синтетический workflow
с нарушением, плюс проверка, что настоящий `.github/workflows/ci.yml` чист.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location("ci_lint", ROOT / "scripts" / "ci_lint.py")
assert _SPEC and _SPEC.loader
ci_lint = importlib.util.module_from_spec(_SPEC)
sys.modules["ci_lint"] = ci_lint
try:
    _SPEC.loader.exec_module(ci_lint)
except ModuleNotFoundError as exc:  # pragma: no cover - нет pyyaml
    pytest.skip(f"нужен PyYAML: {exc}", allow_module_level=True)

GOOD = """
name: CI
on:
  push:
    branches: [main]
permissions:
  contents: read
jobs:
  unit:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@fbc6f3992d24b796d5a048ff273f7fcc4a7b6c09
      - run: make test
"""


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "ci.yml"
    path.write_text(text, encoding="utf-8")
    return path


def test_real_workflows_are_clean() -> None:
    files = sorted(
        path for path in (ROOT / ".github/workflows").iterdir() if path.suffix in {".yml", ".yaml"}
    )
    assert files, "не нашёл ни одного workflow"
    for path in files:
        assert ci_lint.check_workflow(path) == [], path.name


def test_catalog_gate_precedes_release_signing_and_publication() -> None:
    workflow = ci_lint.yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    release = workflow["jobs"]["release"]
    steps = release["steps"]
    names = [step.get("name") for step in steps]
    assets = names.index("Ассеты выпуска")
    gate = names.index("Гейт каталога")
    signing = names.index("Отсоединённая подпись SHA256SUMS")
    publishing = names.index("Публикация")
    assert assets < gate < signing < publishing
    assert steps[gate]["run"] == "python3 scripts/catalog_publish_check.py --dist dist"
    assert "env" not in steps[gate]
    assert "GPG_SIGNING_KEY" not in release.get("env", {})
    assert '--repo "$GITHUB_REPOSITORY"' in steps[publishing]["run"]
    secret_steps = [step for step in steps if "GPG_SIGNING_KEY" in step.get("env", {})]
    assert secret_steps == [steps[signing]]


def test_good_workflow_passes(tmp_path: Path) -> None:
    assert ci_lint.check_workflow(_write(tmp_path, GOOD)) == []


@pytest.mark.parametrize("event", ["push", "pull_request"])
@pytest.mark.parametrize("pattern", ["src/**", "**.md", "docs/**", "tests/**", "*"])
def test_paths_ignore_extra_pattern_rejected(tmp_path: Path, event: str, pattern: str) -> None:
    text = GOOD.replace(
        "  push:\n    branches: [main]",
        f"  {event}:\n    paths-ignore:\n      - 'HEARTBEAT.md'\n      - '{pattern}'",
    )
    problems = ci_lint.check_workflow(_write(tmp_path, text))
    assert len(problems) == 1
    assert f"on.{event}.paths-ignore" in problems[0]
    assert repr(pattern) in problems[0]
    assert "вне белого списка" in problems[0]


def test_paths_ignore_allowlist_passes(tmp_path: Path) -> None:
    patterns = (
        "HEARTBEAT.md",
        "decisions/**",
        ".patches/**",
        "research/**",
        "docs/status.md",
        "docs/plans.md",
        "docs/test-plan.md",
        "arch/**",
        "spikes/**",
    )
    text = GOOD.replace(
        "    branches: [main]",
        "    paths-ignore:\n" + "\n".join(f"      - '{pattern}'" for pattern in patterns),
    )
    assert ci_lint.check_workflow(_write(tmp_path, text)) == []


@pytest.mark.parametrize("ignored", ["'src/**'", "null", "[null]", "[{src: '**'}]"])
def test_paths_ignore_invalid_type_rejected(tmp_path: Path, ignored: str) -> None:
    text = GOOD.replace("    branches: [main]", f"    paths-ignore: {ignored}")
    problems = ci_lint.check_workflow(_write(tmp_path, text))
    assert len(problems) == 1
    assert "paths-ignore" in problems[0]


@pytest.mark.parametrize(
    "needs", ["", "    needs: [lint, unit, deb]\n", "    needs: engine-extra\n"]
)
def test_release_needs_engine(tmp_path: Path, needs: str) -> None:
    text = GOOD.replace(
        "  unit:\n",
        "  release:\n"
        "    if: startsWith(github.ref, 'refs/tags/v')\n"
        "    environment: release\n" + needs,
    )
    problems = ci_lint.check_workflow(_write(tmp_path, text))
    assert len(problems) == 1
    assert "release" in problems[0] and "needs" in problems[0] and "engine" in problems[0]


@pytest.mark.parametrize("needs", ["engine", "[lint, engine, unit]"])
def test_release_with_engine_passes(tmp_path: Path, needs: str) -> None:
    text = GOOD.replace(
        "  unit:\n",
        "  release:\n"
        "    if: startsWith(github.ref, 'refs/tags/v')\n"
        "    environment: release\n"
        f"    needs: {needs}\n",
    )
    assert ci_lint.check_workflow(_write(tmp_path, text)) == []


def test_pull_request_target_rejected(tmp_path: Path) -> None:
    text = GOOD.replace("  push:\n    branches: [main]", "  pull_request_target:")
    problems = ci_lint.check_workflow(_write(tmp_path, text))
    assert any("pull_request_target" in p for p in problems)


def test_unpinned_action_rejected(tmp_path: Path) -> None:
    text = GOOD.replace(
        "actions/checkout@fbc6f3992d24b796d5a048ff273f7fcc4a7b6c09", "actions/checkout@v4"
    )
    problems = ci_lint.check_workflow(_write(tmp_path, text))
    assert any("без пина по SHA" in p for p in problems)


def test_missing_top_level_permissions_rejected(tmp_path: Path) -> None:
    text = GOOD.replace("permissions:\n  contents: read\n", "")
    problems = ci_lint.check_workflow(_write(tmp_path, text))
    assert any("нет permissions" in p for p in problems)


def test_write_permission_outside_release_rejected(tmp_path: Path) -> None:
    text = GOOD.replace(
        "  unit:\n    runs-on: ubuntu-latest",
        "  unit:\n    runs-on: ubuntu-latest\n    permissions:\n      contents: write",
    )
    problems = ci_lint.check_workflow(_write(tmp_path, text))
    assert any("запись разрешена только" in p for p in problems)


def test_secrets_without_environment_rejected(tmp_path: Path) -> None:
    text = GOOD.replace(
        "      - run: make test",
        "      - run: echo build\n        env:\n          K: ${{ secrets.GPG_SIGNING_KEY }}",
    )
    problems = ci_lint.check_workflow(_write(tmp_path, text))
    assert any("secrets без environment" in p for p in problems)


def test_release_job_must_be_tag_gated(tmp_path: Path) -> None:
    text = GOOD.replace(
        "  unit:\n    runs-on: ubuntu-latest",
        "  release:\n    runs-on: ubuntu-latest\n    environment: release",
    )
    problems = ci_lint.check_workflow(_write(tmp_path, text))
    assert any("ограничен тегом" in p for p in problems)


def test_expression_injection_in_run_rejected(tmp_path: Path) -> None:
    text = GOOD.replace(
        "      - run: make test", "      - run: echo ${{ github.event.head_commit.message }}"
    )
    problems = ci_lint.check_workflow(_write(tmp_path, text))
    assert any("инъекция в shell" in p for p in problems)


def test_proposed_workflow_passes() -> None:
    """Пакет правки ci.yml для заказчика (packaging/appimage/ci-workflow-patch.md) чист.

    Заказчик вносит его в браузере один раз; гейт T-06 обязан пройти до отправки.
    """
    proposed = Path(__file__).resolve().parents[2] / "packaging/appimage/ci.yml.proposed"
    assert ci_lint.check_workflow(proposed) == []


def test_local_only_build_variable_rejected(tmp_path: Path) -> None:
    """P2-2: послабление локальной сборки AppImage в workflow запрещено."""
    proposed = Path(__file__).resolve().parents[2] / "packaging/appimage/ci.yml.proposed"
    text = proposed.read_text(encoding="utf-8").replace(
        "        run: packaging/appimage/build.sh\n",
        "        run: packaging/appimage/build.sh\n"
        "        env:\n          ASTRA_VOICE_APPIMAGE_ALLOW_TODO_HASH: '1'\n",
        1,
    )
    path = tmp_path / "ci.yml"
    path.write_text(text, encoding="utf-8")
    problems = ci_lint.check_workflow(path)
    assert problems == [
        "ci.yml: ASTRA_VOICE_APPIMAGE_ALLOW_TODO_HASH — только для локальной сборки, в CI запрещена"
    ]


@pytest.mark.parametrize(
    ("keys", "value", "message"),
    [
        ([True, "push"], {}, "единственный триггер"),
        (["jobs", "sign", "outputs", "key"], "${{ secrets.GPG_SIGNING_KEY }}", "уровень задания"),
        (
            [True, "workflow_dispatch", "inputs", "commit", "required"],
            False,
            "обязательные строковые",
        ),
        (["jobs", "sign", "steps", 4, "if"], "false", "нельзя пропустить"),
        (["jobs", "sign", "steps", 3, "with", "run-id"], "123", "артефакт только из check"),
        (["jobs", "sign", "steps", 5, "id"], "other", "единственным импортом"),
        ([True, "pull_request_target"], {}, "pull_request_target"),
        (["permissions", "contents"], "write", "права workflow"),
        (["concurrency", "group"], "catalog-${{ inputs.commit }}", "не должны накладываться"),
        (["env", "KEY"], "${{ secrets.GPG_SIGNING_KEY }}", "секрет только в шаге"),
        (["jobs", "check", "environment"], "release", "check: Environment запрещён"),
        (["jobs", "check", "environment"], "other", "check: Environment запрещён"),
        (["jobs", "check", "env", "KEY"], "${{ secrets.GPG_SIGNING_KEY }}", "check: секреты"),
        (["jobs", "check", "env", "KEY"], "${{ secrets['GPG_SIGNING_KEY'] }}", "check: секреты"),
        (["jobs", "sign", "permissions", "contents"], "write", "sign: права"),
        (["jobs", "sign", "permissions", "actions"], "write", "sign: права"),
        (["jobs", "sign", "environment"], "other", "sign: нужен Environment"),
        (["jobs", "sign", "needs"], [], "sign: нужен needs"),
        (["jobs", "check", "steps", 0, "run"], "true", "первым шагом"),
        (["jobs", "check", "steps", 0, "if"], "false", "первым шагом"),
        (["jobs", "sign", "steps", 0, "continue-on-error"], True, "первым шагом"),
        (
            ["jobs", "sign", "steps", 2, "with", "ref"],
            "${{ inputs.commit }}",
            "checkout только main",
        ),
        (
            ["jobs", "check", "steps", 2, "with", "ref"],
            "${{ inputs.commit }}",
            "checkout только main",
        ),
        (
            ["jobs", "sign", "steps", 2, "with", "repository"],
            "attacker/fork",
            "checkout только main",
        ),
        (["jobs", "sign", "steps", 2, "with", "persist-credentials"], True, "checkout только main"),
        (["jobs", "sign", "steps", 2, "with", "sparse-checkout"], "/", "checkout ограничен"),
        (["jobs", "sign", "steps", 3, "uses"], "actions/download-artifact@v4", "по SHA"),
        (["jobs", "check", "steps", 4, "uses"], "./local-action", "по SHA"),
        (["jobs", "sign", "steps", 3, "uses"], "docker://evil", "по SHA"),
        (
            ["jobs", "sign", "steps", 3, "env", "KEY"],
            "${{ secrets.GPG_SIGNING_KEY }}",
            "только в шаге импорта",
        ),
        (
            ["jobs", "sign", "env", "GPG_SIGNING_KEY"],
            "${{ secrets.GPG_SIGNING_KEY }}",
            "env задания запрещён",
        ),
        (
            ["jobs", "sign", "steps", 4, "run"],
            'git checkout "$INPUT_COMMIT"',
            "извлечение произвольной",
        ),
        (
            ["jobs", "sign", "steps", 4, "run"],
            "git -C work fetch origin main",
            "извлечение произвольной",
        ),
        (["jobs", "sign", "steps", 4, "run"], "git push origin main", "push и публикация"),
        (["jobs", "sign", "steps", 4, "run"], "gh release create v1", "push и публикация"),
        (
            ["jobs", "sign", "steps", 5, "env", "OTHER"],
            "${{ secrets.OTHER }}",
            "только в шаге импорта",
        ),
    ],
)
def test_catalog_sign_negative_rules(
    tmp_path: Path,
    keys: list[object],
    value: object,
    message: str,
) -> None:
    from typing import Any

    doc = ci_lint.yaml.safe_load((ROOT / ".github/workflows/catalog-sign.yml").read_text())
    target: Any = doc
    for key in keys[:-1]:
        if isinstance(target, dict):
            target = target.setdefault(key, {})
        else:
            target = target[key]
    target[keys[-1]] = value
    path = tmp_path / "catalog-sign.yml"
    path.write_text(ci_lint.yaml.safe_dump(doc, allow_unicode=True))
    problems = ci_lint.check_workflow(path)
    assert any(message in problem for problem in problems), problems


@pytest.mark.parametrize(
    "filename,job_id",
    [("other.yml", "release"), ("ci.yml", "sign"), ("catalog-sign.yml", "release")],
)
def test_release_environment_is_file_and_job_scoped(
    tmp_path: Path,
    filename: str,
    job_id: str,
) -> None:
    text = GOOD.replace("  unit:", f"  {job_id}:").replace(
        "    runs-on: ubuntu-latest", "    runs-on: ubuntu-latest\n    environment: release"
    )
    path = tmp_path / filename
    path.write_text(text)
    assert any("разрешён только двум" in p for p in ci_lint.check_workflow(path))


def test_signing_secret_outside_release_import_step(tmp_path: Path) -> None:
    doc = ci_lint.yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    doc["jobs"]["release"]["steps"][0]["env"] = {"KEY": "${{ secrets.GPG_SIGNING_KEY }}"}
    path = tmp_path / "ci.yml"
    path.write_text(ci_lint.yaml.safe_dump(doc))
    assert any("секрет подписи вне шага импорта" in p for p in ci_lint.check_workflow(path))


@pytest.mark.parametrize("filename", ["ci.yml", "other.yaml"])
@pytest.mark.parametrize(
    "expression",
    [
        "${{ toJSON(secrets) }}",
        "${{ secrets[format('GPG_{0}_KEY', 'SIGNING')] }}",
        "${{ secrets['GPG_SIGNING_KEY'] }}",
        "${{ secrets.OTHER }}",
        "${{ toJSON(SECRETS) }}",
        "${{ secrets.GPG_SIGNING_KEY }}",
    ],
)
def test_all_secret_context_forms_rejected_outside_import(
    tmp_path: Path,
    filename: str,
    expression: str,
) -> None:
    # Даже внутри разрешённого release всё вне единственного env импорта запрещено.
    if filename == "ci.yml":
        doc = ci_lint.yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
        doc["jobs"]["release"]["steps"][0]["env"] = {"ALL": expression}
        text = ci_lint.yaml.safe_dump(doc)
    else:
        text = GOOD.replace(
            "      - run: make test",
            f"      - run: make test\n        env:\n          ALL: {expression}",
        )
    path = tmp_path / filename
    path.write_text(text)
    assert any("секрет подписи вне шага импорта" in p for p in ci_lint.check_workflow(path))


@pytest.mark.parametrize(
    "expression",
    [
        "${{ toJSON(secrets) }}",
        "${{ secrets['GPG_SIGNING_KEY'] }}",
        "${{ secrets[format('GPG_{0}_KEY', 'SIGNING')] }}",
    ],
)
def test_import_step_accepts_only_literal_secret_expression(
    tmp_path: Path, expression: str
) -> None:
    doc = ci_lint.yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    step = next(
        step
        for step in doc["jobs"]["release"]["steps"]
        if step.get("name") == "Отсоединённая подпись SHA256SUMS"
    )
    step["env"]["GPG_SIGNING_KEY"] = expression
    path = tmp_path / "ci.yml"
    path.write_text(ci_lint.yaml.safe_dump(doc))
    assert any("только как точное env" in p for p in ci_lint.check_workflow(path))


@pytest.mark.parametrize("step_mutation", ["duplicate", "run", "with"])
def test_secret_cannot_escape_unique_import_env(tmp_path: Path, step_mutation: str) -> None:
    import copy

    doc = ci_lint.yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    steps = doc["jobs"]["release"]["steps"]
    step = next(step for step in steps if step.get("name") == "Отсоединённая подпись SHA256SUMS")
    if step_mutation == "duplicate":
        steps.append(copy.deepcopy(step))
    elif step_mutation == "run":
        step["run"] += "\necho '${{ toJSON(secrets) }}'\n"
    else:
        step["with"] = {"all": "${{ secrets.GPG_SIGNING_KEY }}"}
    path = tmp_path / "ci.yml"
    path.write_text(ci_lint.yaml.safe_dump(doc))
    assert any("секрет подписи вне шага импорта" in p for p in ci_lint.check_workflow(path))


@pytest.mark.parametrize(
    "uses",
    ["org/repo/.github/workflows/ci.yml@v1", "org/repo/.github/workflows/ci.yml@" + "a" * 40],
)
def test_reusable_workflows_and_inherited_secrets_rejected(tmp_path: Path, uses: str) -> None:
    doc = ci_lint.yaml.safe_load(GOOD)
    doc["jobs"]["unit"] = {"uses": uses, "secrets": "inherit"}
    path = tmp_path / "other.yaml"
    path.write_text(ci_lint.yaml.safe_dump(doc))
    problems = ci_lint.check_workflow(path)
    assert any("переиспользуемые workflow запрещены" in p for p in problems)
    assert any("секрет подписи вне шага импорта" in p for p in problems)


@pytest.mark.parametrize("environment", ["${{ 'release' }}", {"name": "${{ 'release' }}"}])
def test_computed_environment_rejected_in_all_workflows(
    tmp_path: Path, environment: object
) -> None:
    doc = ci_lint.yaml.safe_load(GOOD)
    doc["jobs"]["unit"]["environment"] = environment
    path = tmp_path / "other.yaml"
    path.write_text(ci_lint.yaml.safe_dump(doc))
    assert any("вычисляемое имя Environment" in p for p in ci_lint.check_workflow(path))


@pytest.mark.parametrize(
    "job_id,body",
    [
        ("check", 'git checkout "$CANDIDATE"\npython3 candidate/evil.py'),
        ("sign", 'git "fetch" origin "$CANDIDATE"'),
        ("sign", 'git "checkout" "$CANDIDATE"'),
        ("sign", "true"),
        ("sign", 'printf corrupted > "$RUNNER_TEMP/catalog-to-sign/catalog.json"'),
    ],
)
def test_catalog_seal_rejects_review_scenarios(tmp_path: Path, job_id: str, body: str) -> None:
    doc = ci_lint.yaml.safe_load((ROOT / ".github/workflows/catalog-sign.yml").read_text())
    steps = doc["jobs"][job_id]["steps"]
    if body == "true":
        next(step for step in steps if step.get("name") == "Сверка точных байтов")["run"] = body
    else:
        steps.insert(-2 if job_id == "sign" else -1, {"run": body})
    path = tmp_path / "catalog-sign.yml"
    path.write_text(ci_lint.yaml.safe_dump(doc))
    assert any(ci_lint.CATALOG_SEAL_MESSAGE in p for p in ci_lint.check_workflow(path))


def test_catalog_seal_covers_exact_bytes(tmp_path: Path) -> None:
    import hashlib

    original = (ROOT / ".github/workflows/catalog-sign.yml").read_bytes()
    assert ci_lint.CATALOG_SIGN_SHA256 == hashlib.sha256(original).hexdigest()
    path = tmp_path / "catalog-sign.yml"
    path.write_bytes(original + b"\n# changed comment\n")
    assert any(ci_lint.CATALOG_SEAL_MESSAGE in p for p in ci_lint.check_workflow(path))


@pytest.mark.parametrize("body", ['git "checkout" "$CANDIDATE"', 'git "fetch" origin main'])
def test_structural_git_rule_survives_updated_seal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    body: str,
) -> None:
    import hashlib

    doc = ci_lint.yaml.safe_load((ROOT / ".github/workflows/catalog-sign.yml").read_text())
    doc["jobs"]["sign"]["steps"].insert(3, {"run": body})
    path = tmp_path / "catalog-sign.yml"
    path.write_text(ci_lint.yaml.safe_dump(doc))
    monkeypatch.setattr(
        ci_lint, "CATALOG_SIGN_SHA256", hashlib.sha256(path.read_bytes()).hexdigest()
    )
    problems = ci_lint.check_workflow(path)
    assert not any(ci_lint.CATALOG_SEAL_MESSAGE in p for p in problems)
    assert any("извлечение произвольной ссылки" in p for p in problems)


@pytest.mark.parametrize("argument", ["file", "directory"])
def test_cli_includes_yaml_even_when_given_only_yml(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    argument: str,
) -> None:
    path = _write(tmp_path, GOOD)
    (tmp_path / "bypass.yaml").write_text(GOOD.replace("  push:", "  pull_request_target:"))
    assert ci_lint.main([str(path if argument == "file" else tmp_path)]) == 1
    output = capsys.readouterr()
    assert "bypass.yaml" in output.out
    assert "pull_request_target" in output.err


def test_ci_lint_invocation_scans_directory_for_both_extensions() -> None:
    workflow = ci_lint.yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    assert workflow["jobs"]["ci-lint"]["steps"][-1]["run"] == (
        "python3 scripts/ci_lint.py .github/workflows"
    )


def test_implicit_if_secret_context_rejected(tmp_path: Path) -> None:
    text = GOOD.replace(
        "      - run: make test", "      - run: make test\n        if: toJSON(secrets)"
    )
    path = tmp_path / "other.yaml"
    path.write_text(text)
    assert any("секрет подписи вне шага импорта" in p for p in ci_lint.check_workflow(path))
