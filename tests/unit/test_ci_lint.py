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
    path = tmp_path / "wf.yml"
    path.write_text(text, encoding="utf-8")
    return path


def test_real_workflows_are_clean() -> None:
    files = sorted((ROOT / ".github" / "workflows").glob("*.yml"))
    assert files, "не нашёл ни одного workflow"
    for path in files:
        assert ci_lint.check_workflow(path) == [], path.name


def test_good_workflow_passes(tmp_path: Path) -> None:
    assert ci_lint.check_workflow(_write(tmp_path, GOOD)) == []


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
