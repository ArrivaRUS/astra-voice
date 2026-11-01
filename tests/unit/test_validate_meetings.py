"""The meetings stand is explicit, bounded and reports 0/1/2 unchanged."""

from __future__ import annotations

import json
import runpy
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import pytest

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def cli(monkeypatch: pytest.MonkeyPatch) -> tuple[Callable[[list[str]], int], list[dict[str, Any]]]:
    monkeypatch.setattr(sys, "path", sys.path.copy())
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    calls: list[dict[str, Any]] = []
    module = ModuleType("tools.meeting_stand")

    def run_stand(**kwargs: Any) -> tuple[int, dict[str, Any]]:
        calls.append(kwargs)
        return 0, {"exit_code": 0, "verdict": "pass", "criteria": {}}

    module.__dict__["run_stand"] = run_stand
    monkeypatch.setitem(sys.modules, "tools.meeting_stand", module)
    namespace = runpy.run_path(str(ROOT / "tools/validate"))
    return cast(Callable[[list[str]], int], namespace["main"]), calls


@pytest.mark.parametrize("server", ["pipewire", "pulseaudio"])
def test_meetings_stand_arguments_reach_isolated_runner(
    cli: tuple[Callable[[list[str]], int], list[dict[str, Any]]],
    capsys: pytest.CaptureFixture[str],
    server: str,
) -> None:
    main, calls = cli
    assert (
        main(["meetings", "--stand", "--minutes", "2", "--server", server, "--json", "--keep"]) == 0
    )
    assert calls == [{"minutes": 2.0, "server": server, "keep": True}]
    captured = capsys.readouterr()
    assert len(captured.out.splitlines()) == 1 and not captured.err
    assert json.loads(captured.out) == {"exit_code": 0, "verdict": "pass", "criteria": {}}


def test_meetings_default_is_sixty_minutes_without_retained_processes(
    cli: tuple[Callable[[list[str]], int], list[dict[str, Any]]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    main, calls = cli
    assert main(["meetings", "--stand", "--json"]) == 0
    assert calls == [{"minutes": 60.0, "server": "pipewire", "keep": False}]
    capsys.readouterr()


def test_meetings_profile_reaches_only_explicit_stand_runner(
    cli: tuple[Callable[[list[str]], int], list[dict[str, Any]]],
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    main, calls = cli
    profile = tmp_path / "capture.pstats"
    assert main(["meetings", "--stand", "--profile", str(profile), "--json"]) == 0
    assert calls == [{"minutes": 60.0, "server": "pipewire", "keep": False, "profile": profile}]
    capsys.readouterr()
    with pytest.raises(SystemExit) as error:
        main(["meetings", "--profile", str(profile)])
    assert error.value.code == 2
    assert len(calls) == 1


@pytest.mark.parametrize("code", [0, 1, 2])
def test_meetings_preserves_runner_verdict_and_return_code(
    cli: tuple[Callable[[list[str]], int], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    code: int,
) -> None:
    main, _calls = cli
    facts = {"exit_code": code, "verdict": ["pass", "fail", "unavailable"][code]}
    monkeypatch.setattr(
        sys.modules["tools.meeting_stand"], "run_stand", lambda **kwargs: (code, facts)
    )
    assert main(["meetings", "--stand", "--json"]) == code
    assert json.loads(capsys.readouterr().out) == facts


@pytest.mark.parametrize(
    "arguments",
    [
        [],
        ["--minutes", "0"],
        ["--minutes", "-1"],
        ["--minutes", "nan"],
        ["--minutes", "inf"],
        ["--server", "remote"],
        ["--minutes", "text"],
    ],
)
def test_meetings_rejects_implicit_or_invalid_stand_without_running(
    cli: tuple[Callable[[list[str]], int], list[dict[str, Any]]], arguments: list[str]
) -> None:
    main, calls = cli
    with pytest.raises(SystemExit) as error:
        main(["meetings", *([] if not arguments else ["--stand"]), *arguments])
    assert error.value.code == 2 and not calls


def test_meetings_text_mode_reports_verdict(
    cli: tuple[Callable[[list[str]], int], list[dict[str, Any]]], capsys: pytest.CaptureFixture[str]
) -> None:
    main, _calls = cli
    assert main(["meetings", "--stand"]) == 0
    assert "pass" in capsys.readouterr().out


def test_profile_help_identifies_json_not_pstats(
    cli: tuple[Callable[[list[str]], int], list[dict[str, Any]]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    main, _ = cli
    with pytest.raises(SystemExit) as result:
        main(["meetings", "--help"])
    assert result.value.code == 0
    help_text = capsys.readouterr().out
    assert "JSON capture-worker" in help_text
    assert "pstats" not in help_text
