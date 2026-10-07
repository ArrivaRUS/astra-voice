"""The proposed AppImage workflow preserves CI and isolates release credentials."""

from __future__ import annotations

import copy
import difflib
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

yaml = pytest.importorskip("yaml")
pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]
CURRENT = ROOT / ".github/workflows/ci.yml"
PROPOSED = ROOT / "packaging/appimage/ci.yml.proposed"


def workflows() -> tuple[dict[str, Any], dict[str, Any]]:
    return yaml.safe_load(CURRENT.read_text()), yaml.safe_load(PROPOSED.read_text())


def test_existing_ci_jobs_and_triggers_are_preserved() -> None:
    current, proposed = workflows()
    # The owner applies this exact file in GitHub; that PR must also pass CI.
    if current == proposed:
        return
    assert {key: value for key, value in current.items() if key != "jobs"} == {
        key: value for key, value in proposed.items() if key != "jobs"
    }
    assert set(proposed["jobs"]) == set(current["jobs"]) | {"appimage"}
    for name, job in current["jobs"].items():
        if name == "release":
            continue
        candidate = copy.deepcopy(proposed["jobs"][name])
        for step in candidate["steps"]:
            if step.get("uses", "").startswith("actions/checkout@"):
                assert step["with"].pop("persist-credentials") is False
                if not step["with"]:
                    del step["with"]
        assert candidate == job, name
    release = proposed["jobs"]["release"]
    assert {k: v for k, v in release.items() if k not in {"steps", "needs"}} == {
        k: v for k, v in current["jobs"]["release"].items() if k not in {"steps", "needs"}
    }
    assert release["needs"] == [*current["jobs"]["release"]["needs"], "appimage"]
    old_actions = {
        step["uses"] for job in current["jobs"].values() for step in job["steps"] if "uses" in step
    }
    assert all(
        step["uses"] in old_actions
        for job in proposed["jobs"].values()
        for step in job["steps"]
        if "uses" in step
    )
    for name in (
        "Версия тега и changelog",
        "Тег стоит на коммите из main",
        "Связка ключей в белом списке",
    ):
        assert next(step for step in release["steps"] if step.get("name") == name) == next(
            step for step in current["jobs"]["release"]["steps"] if step.get("name") == name
        )


def test_release_credentials_and_debian_gh_are_scoped() -> None:
    _, proposed = workflows()
    assert "secrets." not in json.dumps(proposed.get("env", {}))
    steps = proposed["jobs"]["release"]["steps"]
    runs = [step.get("run", "") for step in steps]
    build = runs.index("scripts/release_build.sh")
    assets = runs.index("scripts/release_assets.sh dist")
    signing = [i for i, step in enumerate(steps) if "secrets." in json.dumps(step)]
    publishing = [i for i, step in enumerate(steps) if "GH_TOKEN" in step.get("env", {})]
    assert len(signing) == len(publishing) == 1
    assert build < assets < signing[0] < publishing[0]
    sign_step = steps[signing[0]]
    assert sign_step["env"] == {"GPG_SIGNING_KEY": "${{ secrets.GPG_SIGNING_KEY }}"}
    assert sign_step["shell"] == "bash"
    assert "trap " in sign_step["run"] and " EXIT" in sign_step["run"]
    assert 'rm -rf -- "$GNUPGHOME"' in sign_step["run"]
    for name, job in proposed["jobs"].items():
        assert "secrets." not in json.dumps(job.get("env", {}))
        if name != "release":
            assert "secrets." not in json.dumps(job)
    install = next(step for step in steps if step.get("name") == "Инструменты сборки")
    assert "apt-get install -y --no-install-recommends" in install["run"]
    assert "gh" in install["run"].split()
    assert not install.get("env")
    assert "cli.github.com" not in PROPOSED.read_text()
    publish = steps[publishing[0]]["run"]
    assert "gh release create" in publish and "$(cat dist/assets.txt)" in publish
    assert not any(command in publish for command in ("apt-get", "curl", "wget"))


def test_proposed_shell_steps_parse_without_execution() -> None:
    _, proposed = workflows()
    for name, job in proposed["jobs"].items():
        for step in job["steps"]:
            if "run" not in step:
                continue
            result = subprocess.run(
                ["bash", "-n"], input=step["run"], text=True, capture_output=True, check=False
            )
            assert result.returncode == 0, (name, step.get("name"), result.stderr)


def test_document_contains_current_exact_diff() -> None:
    if CURRENT.read_text() == PROPOSED.read_text():
        return  # The documented patch has already been applied.
    document = (ROOT / "packaging/appimage/ci-workflow-patch.md").read_text()
    actual = document.split("```diff\n", 1)[1].split("\n```", 1)[0] + "\n"
    expected = "".join(
        difflib.unified_diff(
            CURRENT.read_text().splitlines(keepends=True),
            PROPOSED.read_text().splitlines(keepends=True),
            fromfile=".github/workflows/ci.yml",
            tofile=".github/workflows/ci.yml",
        )
    )
    assert actual == expected
