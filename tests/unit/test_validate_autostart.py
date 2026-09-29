"""`tools/validate autostart`: песочница XDG и чтение установленной записи на фикстуре."""

from __future__ import annotations

import json
import os
import runpy
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest

from astra_voice.platform import autostart

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
CANONICAL = (
    b"[Desktop Entry]\nType=Application\nName=Astra Voice\n"
    b"Exec=/usr/bin/astra-voice --hidden\nIcon=astravoice\nNoDisplay=true\n"
    b"X-KDE-autostart-after=panel\nX-AstraVoice-Managed=true\n"
)


@pytest.fixture
def config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """«Настоящий» каталог настроек теста — фикстура, не ~/.config."""
    home = tmp_path / "home-config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_DIRS", str(tmp_path / "home-xdg"))
    return home


@pytest.fixture
def validate(config: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[[list[str]], Any]:
    monkeypatch.setenv("QT_QPA_PLATFORM", os.environ.get("QT_QPA_PLATFORM", "offscreen"))
    monkeypatch.setattr(sys, "path", sys.path.copy())
    namespace = runpy.run_path(str(ROOT / "tools/validate"))
    main = cast(Callable[[list[str]], int], namespace["main"])

    def run(argv: list[str]) -> Any:
        return main(argv)

    return run


def report(
    validate: Callable[[list[str]], Any], argv: list[str], capsys: pytest.CaptureFixture[str]
) -> tuple[int, Any]:
    code = validate(["autostart", *argv, "--json"])
    facts = json.loads(capsys.readouterr().out)
    assert facts["exit_code"] == code
    return code, facts


def test_sandbox_checks_pass_and_leave_config_alone(
    validate: Callable[[list[str]], Any], config: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    canary = config / "autostart" / "astra-voice.desktop"
    canary.parent.mkdir(parents=True)
    canary.write_bytes(b"[Desktop Entry]\nExec=canary\n")
    before = canary.stat().st_mtime_ns

    code, facts = report(validate, [], capsys)

    assert code == 0
    assert [check["name"] for check in facts["checks"]] == [
        "initial",
        "enable",
        "enable-again",
        "disable",
        "disable-again",
        "foreign",
    ]
    assert all(check["passed"] for check in facts["checks"])
    assert os.environ["XDG_CONFIG_HOME"] == str(config)
    assert canary.read_bytes() == b"[Desktop Entry]\nExec=canary\n"
    assert canary.stat().st_mtime_ns == before
    assert sorted(path.name for path in canary.parent.iterdir()) == ["astra-voice.desktop"]


def test_sandbox_fails_on_non_canonical_entry(
    validate: Callable[[list[str]], Any],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(autostart, "_ENTRY", CANONICAL + b"OnlyShowIn=KDE;\n")

    code, facts = report(validate, [], capsys)

    failed = {check["name"] for check in facts["checks"] if not check["passed"]}
    assert code == 1
    assert failed == {"enable"}
    assert "есть OnlyShowIn" in facts["checks"][1]["actual"]


def test_sandbox_fails_when_foreign_file_touched(
    validate: Callable[[list[str]], Any],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = autostart.set_enabled

    def clumsy(enabled: bool) -> None:
        original(enabled)
        handy = autostart._paths()[0].parent / "handy.desktop"
        handy.write_bytes(handy.read_bytes() + b"Hidden=true\n")

    monkeypatch.setattr(autostart, "set_enabled", clumsy)

    code, facts = report(validate, [], capsys)

    assert code == 1
    assert not facts["checks"][-1]["passed"]


@pytest.mark.parametrize(
    ("content", "code", "problem"),
    [
        (CANONICAL, 0, ""),
        (CANONICAL + b"OnlyShowIn=KDE;\n", 1, "есть OnlyShowIn"),
        (CANONICAL + b"Hidden=true\n", 1, "Hidden=true"),
        (CANONICAL.replace(b"--hidden", b""), 1, "Exec=/usr/bin/astra-voice"),
        (None, 1, "нет файла"),
    ],
)
def test_check_installed_reads_only(
    validate: Callable[[list[str]], Any],
    config: Path,
    capsys: pytest.CaptureFixture[str],
    content: bytes | None,
    code: int,
    problem: str,
) -> None:
    entry = config / "autostart" / "astra-voice.desktop"
    entry.parent.mkdir(parents=True)
    if content is not None:
        entry.write_bytes(content)
        before = entry.stat()

    result, facts = report(validate, ["--check-installed"], capsys)

    assert result == code
    assert problem in " ".join(check["actual"] for check in facts["checks"])
    if content is None:
        assert not entry.exists()
    else:
        after = entry.stat()
        assert entry.read_bytes() == content
        assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)
    assert [path.name for path in entry.parent.iterdir()] == (
        [] if content is None else ["astra-voice.desktop"]
    )


def test_check_installed_symlink_is_separate_error(
    validate: Callable[[list[str]], Any],
    config: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    real = tmp_path / "real.desktop"
    real.write_bytes(CANONICAL)
    entry = config / "autostart" / "astra-voice.desktop"
    entry.parent.mkdir(parents=True)
    entry.symlink_to(real)

    code, facts = report(validate, ["--check-installed"], capsys)

    assert code == 1
    assert [check["name"] for check in facts["checks"]] == ["symlink"]
    assert "символическая ссылка" in facts["checks"][0]["actual"]
    assert entry.is_symlink() and real.read_bytes() == CANONICAL


def test_sandbox_path_not_substituted_gives_exit_2(
    validate: Callable[[list[str]], Any],
    config: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = config / "autostart" / "astra-voice.desktop"
    monkeypatch.setattr(autostart, "_paths", lambda: (real, []))

    code, facts = report(validate, [], capsys)

    assert code == 2
    assert facts["checks"] == []
    assert "временный XDG_CONFIG_HOME" in facts["explanation"]
    assert not real.exists()
