"""Машинная приёмка Ц2 и два тапа настоящего автомата без звука и воркера."""

from __future__ import annotations

import json
import os
import runpy
import sys
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest

from astra_voice.platform.hotkey import PTT_THRESHOLD_S, HotkeyFsm, HotkeyMode, HotkeyState

pytestmark = pytest.mark.unit


@pytest.fixture
def validate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Callable[[list[str]], int]:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    # tools/validate настраивает окружение самостоятельного процесса; изолируем импорт.
    monkeypatch.setenv("QT_QPA_PLATFORM", os.environ.get("QT_QPA_PLATFORM", "offscreen"))
    monkeypatch.setattr(sys, "path", sys.path.copy())
    module = runpy.run_path(str(Path(__file__).resolve().parents[2] / "tools/validate"))
    return cast(Callable[[list[str]], int], module["main"])


def write_stats(tmp_path: Path, events: list[dict[str, object]]) -> None:
    directory = tmp_path / "astra-voice"
    directory.mkdir(exist_ok=True)
    (directory / "stats.json").write_text(
        json.dumps({"schema_version": 1, "events": events}), encoding="utf-8"
    )


@pytest.mark.parametrize(
    ("timing", "target", "code"),
    [(310.0, 500, 0), (500.0, 500, 0), (500.01, 500, 1), (600.0, 750, 0), (310.0, 200, 1)],
)
def test_stats_target(
    validate: Callable[[list[str]], int],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    timing: float,
    target: int,
    code: int,
) -> None:
    write_stats(
        tmp_path,
        [
            {"type": "dictation", "ts": 1, "cold": False, "t_ms": timing, "result": "ok"},
            {"type": "dictation", "ts": 2, "cold": True, "t_ms": 9000, "result": "empty"},
            {"type": "mic_error", "ts": 3, "kind": "busy"},
        ],
    )
    assert validate(["stats", "--json", "--target-ms", str(target)]) == code
    captured = capsys.readouterr()
    assert captured.err == ""
    assert len(captured.out.splitlines()) == 1
    assert json.loads(captured.out) == {
        "events": 3,
        "dictations": 2,
        "p50_ms": timing,
        "p95_ms": timing,
        "results": {"ok": 1, "empty": 1, "cancelled": 0},
        "mic_errors": 1,
        "target_ms": target,
        "verdict": "уложились" if code == 0 else "не уложились",
    }


@pytest.mark.parametrize("kind", ["empty", "missing", "cold", "no-timing", "mic-error"])
def test_stats_without_warm_timings(
    validate: Callable[[list[str]], int],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    kind: str,
) -> None:
    events: list[dict[str, object]] = []
    if kind == "cold":
        events = [{"type": "dictation", "ts": 1, "cold": True, "t_ms": 10}]
    elif kind == "no-timing":
        events = [{"type": "dictation", "ts": 1, "cold": False}]
    elif kind == "mic-error":
        events = [{"type": "mic_error", "ts": 1, "kind": "none"}]
    if kind != "missing":
        write_stats(tmp_path, events)
    assert validate(["stats", "--json"]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["p50_ms"] is None
    assert payload["p95_ms"] is None
    assert payload["target_ms"] == 500
    assert payload["verdict"] == "нет данных"
    assert {"dictations", "results", "mic_errors"} <= payload.keys()


def test_stats_since_excludes_history(
    validate: Callable[[list[str]], int], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_stats(
        tmp_path,
        [
            {"type": "dictation", "ts": 1, "cold": False, "t_ms": 10, "result": "ok"},
            {"type": "mic_error", "ts": 2, "kind": "busy"},
            {"type": "dictation", "ts": 3, "cold": False, "t_ms": 501, "result": "ok"},
        ],
    )
    assert validate(["stats", "--json", "--since", "2"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["dictations"] == payload["events"] == 1
    assert payload["p95_ms"] == 501
    assert payload["mic_errors"] == 0
    assert validate(["stats", "--json", "--since", "3"]) == 2
    assert json.loads(capsys.readouterr().out)["dictations"] == 0


@pytest.mark.parametrize("target", ["-1", "nan", "inf"])
def test_stats_invalid_target(validate: Callable[[list[str]], int], target: str) -> None:
    with pytest.raises(SystemExit) as exc:
        validate(["stats", "--target-ms", target])
    assert exc.value.code == 2


@pytest.mark.parametrize("release_first", [False, True])
def test_toggle_second_press_processes_without_idle(release_first: bool) -> None:
    states: list[HotkeyState] = []
    reasons: list[str] = []

    def on_state(state: HotkeyState, reason: str) -> None:
        states.append(state)
        reasons.append(reason)

    fsm = HotkeyFsm(HotkeyMode.TOGGLE, on_state=on_state)
    fsm.press(0)
    assert fsm.state == HotkeyState.RECORDING
    if release_first:
        fsm.release(PTT_THRESHOLD_S / 2)
    fsm.tick(PTT_THRESHOLD_S * 2)
    assert fsm.state == HotkeyState.RECORDING
    assert states == [HotkeyState.RECORDING]
    fsm.press(1)
    assert fsm.state.value == "processing"
    fsm.release(1 + PTT_THRESHOLD_S / 2)
    assert fsm.state.value == "processing"
    assert states == [HotkeyState.RECORDING, HotkeyState.PROCESSING]
    assert reasons == ["press", "toggle-off"]
    assert fsm.limit_deadline is None
