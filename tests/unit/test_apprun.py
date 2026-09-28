"""Тонкий AppRun (packaging/appimage/AppRun): режимы, самоустановка, флаги без установки.

Настоящий AppRun в фиктивном AppDir; вместо бандлового Python — sh-заглушка:
команда ``selfinstall`` уходит в настоящий bootstrap.py (userinstall), команда
``app`` только записывает вызов в журнал — GUI не запускается. HOME и XDG — в tmp_path.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from astra_voice.platform import userinstall
from helpers.appimage_bundle import KEY, VERSION, make_bundle

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
APPRUN = REPO_ROOT / "packaging" / "appimage" / "AppRun"
DEV_BOOTSTRAP = REPO_ROOT / "src" / "astra_voice" / "bootstrap.py"

FAKE_PYTHON = """#!/bin/sh
[ "$1" = -I ] && shift
boot=$1
shift
cmd=$1
shift
printf '%s|%s|HERE=%s|EAR=%s|PP=%s\\n' "$cmd" "$*" "${ASTRA_VOICE_APPIMAGE_DIR:-}" \\
    "${APPIMAGE_EXTRACT_AND_RUN:-}" "${PYTHONPATH:-}" >> "$APPRUN_TEST_LOG"
case $cmd in
    selfinstall) exec "@REAL_PY@" -I "$boot" selfinstall "$@" ;;
    app)
        for arg do
            if [ "$arg" = --version ]; then
                echo "astra-voice ${APPRUN_TEST_VERSION:-@VERSION@}"
            fi
        done
        exit 0 ;;
esac
exit 99
"""


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    home = tmp_path / "home"
    home.mkdir()
    runtime = tmp_path / "run"
    runtime.mkdir(mode=0o700)
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "XDG_RUNTIME_DIR": str(runtime),
        "APPRUN_TEST_LOG": str(tmp_path / "calls.log"),
        "LANG": "C.UTF-8",
    }


def _bundle(root: Path) -> Path:
    bundle = make_bundle(root, apprun=APPRUN.read_text(encoding="utf-8"))
    python = bundle / "opt" / "python3.11" / "bin" / "python3.11"
    python.write_text(
        FAKE_PYTHON.replace("@REAL_PY@", sys.executable).replace("@VERSION@", VERSION),
        encoding="utf-8",
    )
    python.chmod(0o755)
    boot = bundle / "usr" / "lib" / "astra-voice" / "bootstrap.py"
    boot.unlink()
    boot.symlink_to(DEV_BOOTSTRAP)
    return bundle


def _run(
    launcher: Path, args: list[str], env: dict[str, str], shell: str | None = None
) -> subprocess.CompletedProcess[str]:
    command = [shell, str(launcher)] if shell else [str(launcher)]
    return subprocess.run(
        [*command, *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        stdin=subprocess.DEVNULL,
    )


def _calls(env: dict[str, str]) -> list[str]:
    log = Path(env["APPRUN_TEST_LOG"])
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


def _app(env: dict[str, str]) -> Path:
    return Path(env["HOME"]) / ".local" / "share" / "astra-voice" / "app"


SHELLS = [None] + (["dash"] if shutil.which("dash") else [])


@pytest.mark.parametrize("shell", SHELLS)
def test_extract_and_run_installs_and_starts_copy(
    tmp_path: Path, env: dict[str, str], shell: str | None
) -> None:
    """Режим В: самоустановка → exec установленной копии → распаковка удалена."""
    extracted = _bundle(tmp_path / "tmp" / "appimage_extracted_0123abcd")
    env = {**env, "APPIMAGE_EXTRACT_AND_RUN": "1", "PYTHONPATH": "/evil"}
    proc = _run(extracted / "AppRun", ["--hidden"], env, shell)
    assert proc.returncode == 0, proc.stderr
    app = _app(env)
    target = app / KEY
    assert os.readlink(app / "current") == KEY
    assert not extracted.exists()
    calls = _calls(env)
    assert calls[0] == f"selfinstall|{extracted} --hidden|HERE=|EAR=|PP="
    # Смоук новой копии до переключения current — из временного каталога рядом.
    assert re.fullmatch(
        rf"app\|--version\|HERE={re.escape(str(app))}/\.tmp-{re.escape(KEY)}\.[^|]+\|EAR=\|PP=",
        calls[1],
    )
    assert calls[2:] == [f"app|--hidden|HERE={target}|EAR=|PP="]


def test_second_run_takes_fast_path(tmp_path: Path, env: dict[str, str]) -> None:
    first = _bundle(tmp_path / "a" / "appimage_extracted_1")
    assert _run(first / "AppRun", [], env).returncode == 0
    Path(env["APPRUN_TEST_LOG"]).unlink()
    second = _bundle(tmp_path / "b" / "appimage_extracted_2")
    proc = _run(second / "AppRun", ["--hidden"], env)
    assert proc.returncode == 0, proc.stderr
    assert _calls(env) == [f"app|--hidden|HERE={_app(env) / KEY}|EAR=|PP="]
    assert not second.exists()


def test_installed_copy_runs_in_place(tmp_path: Path, env: dict[str, str]) -> None:
    """Режим А: меню и автозапуск идут через app/current/AppRun — без установки."""
    assert _run(_bundle(tmp_path / "appimage_extracted_1") / "AppRun", [], env).returncode == 0
    Path(env["APPRUN_TEST_LOG"]).unlink()
    proc = _run(_app(env) / "current" / "AppRun", ["--hidden"], env)
    assert proc.returncode == 0, proc.stderr
    assert _calls(env) == [f"app|--hidden|HERE={_app(env) / KEY}|EAR=|PP="]
    status = _run(_app(env) / "current" / "AppRun", ["--selfinstall-status"], env)
    assert status.stdout.splitlines()[0] == "MODE=А"


@pytest.mark.parametrize("flag", sorted(userinstall.SERVICE_FLAGS - {"--selfinstall-status"}))
def test_service_flags_do_not_install(tmp_path: Path, env: dict[str, str], flag: str) -> None:
    extracted = _bundle(tmp_path / "appimage_extracted_1")
    proc = _run(extracted / "AppRun", [flag], env)
    assert proc.returncode == 0, proc.stderr
    assert not (Path(env["HOME"]) / ".local").exists()
    assert extracted.exists()
    assert _calls(env) == [f"app|{flag}|HERE={extracted}|EAR=|PP="]


def test_portable_env_does_not_install(tmp_path: Path, env: dict[str, str]) -> None:
    extracted = _bundle(tmp_path / "appimage_extracted_1")
    proc = _run(extracted / "AppRun", ["--hidden"], {**env, "ASTRA_VOICE_PORTABLE": "1"})
    assert proc.returncode == 0, proc.stderr
    assert not (Path(env["HOME"]) / ".local").exists()
    assert _calls(env) == [f"app|--hidden|HERE={extracted}|EAR=|PP="]


def test_manual_extraction_runs_without_install(tmp_path: Path, env: dict[str, str]) -> None:
    """Режим Г (squashfs-root): обычный запуск, домашняя папка не меняется."""
    root = _bundle(tmp_path / "squashfs-root")
    status = _run(root / "AppRun", ["--selfinstall-status"], env)
    assert status.stdout.splitlines() == [
        "MODE=Г",
        f"HERE={root}",
        f"KEY={KEY}",
        f"ROOT={_app(env)}",
    ]
    proc = _run(root / "AppRun", ["--hidden"], env)
    assert proc.returncode == 0, proc.stderr
    assert not (Path(env["HOME"]) / ".local").exists()
    assert _calls(env) == [f"app|--hidden|HERE={root}|EAR=|PP="]


def test_failed_install_in_extract_mode_exits(tmp_path: Path, env: dict[str, str]) -> None:
    """Режим В: сломанная сборка не становится current, из распаковки не запускаемся."""
    extracted = _bundle(tmp_path / "appimage_extracted_1")
    proc = _run(extracted / "AppRun", ["--hidden"], {**env, "APPRUN_TEST_VERSION": "9.9.9"})
    assert proc.returncode == userinstall.EXIT_FAILED
    assert "не запустилась" in proc.stderr
    assert not os.path.lexists(_app(env) / "current")
    assert extracted.exists()
    assert not [call for call in _calls(env) if call.startswith("app|--hidden")]


def test_xdg_data_home_matches_python(tmp_path: Path, env: dict[str, str]) -> None:
    data = tmp_path / "data"
    extracted = _bundle(tmp_path / "appimage_extracted_1")
    env = {**env, "XDG_DATA_HOME": str(data)}
    assert _run(extracted / "AppRun", [], env).returncode == 0
    assert os.readlink(data / "astra-voice" / "app" / "current") == KEY
    assert not (Path(env["HOME"]) / ".local").exists()


def test_corrupt_build_info(tmp_path: Path, env: dict[str, str]) -> None:
    root = _bundle(tmp_path / "appimage_extracted_1")
    (root / ".astra-voice-build").write_text("VERSION=0.2.0/..\nBUILD_ID=x\n", encoding="ascii")
    proc = _run(root / "AppRun", [], env)
    assert proc.returncode == 1
    assert "Повреждены сведения о сборке" in proc.stderr
    assert _calls(env) == []


def test_service_flags_match_python() -> None:
    """Один список служебных флагов в AppRun и userinstall.SERVICE_FLAGS."""
    text = APPRUN.read_text(encoding="utf-8")
    match = re.search(r"^\s*(--version[^)]*)\) return 0 ;;$", text, re.MULTILINE)
    assert match is not None
    flags = {flag.strip() for flag in match.group(1).split("|")}
    assert flags == userinstall.SERVICE_FLAGS


def test_t1_markers_present() -> None:
    """Точки расширения на 01.10 в AppRun помечены (MJ-3, MN-1)."""
    text = APPRUN.read_text(encoding="utf-8")
    assert "# T1-01.10: MJ-3" in text
    assert "# T1-01.10: MN-1" in text
