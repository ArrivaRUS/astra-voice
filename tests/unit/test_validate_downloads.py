"""`tools/validate downloads --faults`: сервер-вредитель только на 127.0.0.1."""

from __future__ import annotations

import json
import os
import runpy
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
# Пункты строки Validation M6: «hf→github→corp, 429/404, size+1, нет места, зеркала ≠ байты».
REQUIRED = {
    "fallback-hf-github",
    "fallback-corp-hf-github",
    "http-429",
    "http-404",
    "size-plus-one",
    "resume-range",
    "resume-mirror",
    "no-space-before",
    "no-space-during",
    "no-space-enospc",
    "mirror-bytes-differ",
    "redirect-evil",
}


@pytest.fixture
def validate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Any]:
    for name in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
        monkeypatch.setenv(name, str(tmp_path / name))
    monkeypatch.setenv("QT_QPA_PLATFORM", os.environ.get("QT_QPA_PLATFORM", "offscreen"))
    monkeypatch.setattr(sys, "path", sys.path.copy())
    namespace = runpy.run_path(str(ROOT / "tools/validate"))
    return cast(dict[str, Any], namespace["main"].__globals__)


def run_json(validate: dict[str, Any], capsys: pytest.CaptureFixture[str]) -> tuple[int, Any]:
    main = cast(Callable[[list[str]], int], validate["main"])
    code = main(["downloads", "--faults", "--json"])
    return code, json.loads(capsys.readouterr().out)


def test_all_fault_scenarios_pass(
    validate: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    code, report = run_json(validate, capsys)

    failed = [check for check in report["checks"] if not check["passed"]]
    assert failed == []
    assert code == report["exit_code"] == 0
    assert report["passed"] == report["total"] == len(report["checks"])
    names = {check["name"] for check in report["checks"]}
    assert REQUIRED <= names
    assert all(check["title"] for check in report["checks"])


def test_scenario_table_covers_plan(validate: dict[str, Any]) -> None:
    scenarios = validate["download_scenarios"](65536)
    by_name = {scenario.name: scenario for scenario in scenarios}

    assert REQUIRED <= set(by_name)
    assert by_name["fallback-corp-hf-github"].kinds == ("corp", "hf", "github")
    # 429 и 404 ведут к следующему источнику ровно одним запросом к первому.
    for name in ("http-429", "http-404"):
        assert by_name[name].requests[0][0] == "hf"
        assert [prefix for prefix, _, _ in by_name[name].requests].count("hf") == 1
    assert by_name["mirror-bytes-differ"].install_refused
    assert {by_name[name].disk for name in by_name if name.startswith("no-space")} == {
        "before",
        "during",
        "enospc",
    }


def test_failed_scenario_gives_nonzero_exit(
    validate: dict[str, Any],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import astra_voice.models.downloader as downloader

    # Загрузчик без перебора источников: сценарии зеркал обязаны провалиться.
    monkeypatch.setattr(downloader, "_MIRROR_CODES", frozenset())

    code, report = run_json(validate, capsys)

    failed = {check["name"] for check in report["checks"] if not check["passed"]}
    assert code == report["exit_code"] == 1
    assert {"fallback-hf-github", "http-429", "http-404", "resume-mirror"} <= failed


def test_plain_output_has_titles_and_verdict(
    validate: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    main = cast(Callable[[list[str]], int], validate["main"])

    code = main(["downloads", "--faults"])

    out = capsys.readouterr().out
    assert code == 0
    assert "http-429 (hf 429 с Retry-After" in out
    assert out.rstrip().endswith("ожидания совпали; код 0.")
