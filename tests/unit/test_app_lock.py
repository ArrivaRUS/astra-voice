"""Single-instance: блокировка, протокол ``show``, отказ на прочих командах.

Два настоящих процесса без дисплея (``QT_QPA_PLATFORM=offscreen``), ресурсы
подменены пустым каталогом — GUI поднимается на запасном виджете.
"""

from __future__ import annotations

import importlib.util
import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

# Тестам нужен настоящий процесс GUI, то есть PyQt5 (дисплей не нужен —
# `QT_QPA_PLATFORM=offscreen`). На машине разработчика без PyQt5 пропускаем,
# а в CI требуем: молчаливый пропуск там означал бы ложную зелень.
_QT_MISSING = importlib.util.find_spec("PyQt5.QtWidgets") is None

pytestmark = [
    pytest.mark.unit,
    pytest.mark.skipif(
        _QT_MISSING and not os.environ.get("CI"),
        reason="нужен python3-pyqt5",
    ),
]

REPO_ROOT = Path(__file__).resolve().parents[2]
BOOTSTRAP = REPO_ROOT / "src" / "astra_voice" / "bootstrap.py"
LOG_FILE = "logs/astra-voice.log"
START_TIMEOUT_S = 30.0


def _env(tmp_path: Path) -> dict[str, str]:
    env = dict(os.environ)
    env.pop("DISPLAY", None)
    env.pop("WAYLAND_DISPLAY", None)
    env.update(
        {
            "QT_QPA_PLATFORM": "offscreen",
            "XDG_RUNTIME_DIR": str(tmp_path / "run"),
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
            "XDG_DATA_HOME": str(tmp_path / "data"),
            "XDG_CACHE_HOME": str(tmp_path / "cache"),
            "ASTRA_VOICE_RESOURCES": str(tmp_path / "resources"),
        }
    )
    (tmp_path / "resources").mkdir(exist_ok=True)
    return env


def _run(args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-I", str(BOOTSTRAP), "app", *args],
        env=env,
        cwd=str(REPO_ROOT.parent),
        capture_output=True,
        text=True,
        timeout=60,
    )


def _report(proc: subprocess.Popen[str]) -> str:
    """Что успел сказать дочерний процесс: код возврата, stderr, stdout."""
    if proc.poll() is None:
        proc.terminate()
    try:
        out, err = proc.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, err = proc.communicate()
    return f"код возврата: {proc.returncode}\nstderr:\n{err}\nstdout:\n{out}"


def _wait_for(path: Path, proc: subprocess.Popen[str], timeout: float = START_TIMEOUT_S) -> None:
    """Ждёт файл; если процесс умер или не дождались — печатает его stderr."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        if proc.poll() is not None:
            raise AssertionError(f"дочерний процесс завершился до {path}\n{_report(proc)}")
        time.sleep(0.05)
    raise AssertionError(f"не дождался появления {path}\n{_report(proc)}")


def _log_text(tmp_path: Path) -> str:
    log = tmp_path / "data" / "astra-voice" / LOG_FILE
    return log.read_text(encoding="utf-8") if log.exists() else ""


def _wait_for_log(tmp_path: Path, needle: str, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if needle in _log_text(tmp_path):
            return
        time.sleep(0.05)
    raise AssertionError(f"в журнале нет {needle!r}; журнал:\n{_log_text(tmp_path)}")


@pytest.fixture
def first_instance(tmp_path: Path) -> Iterator[tuple[subprocess.Popen[str], dict[str, str]]]:
    env = _env(tmp_path)
    proc = subprocess.Popen(
        [sys.executable, "-I", str(BOOTSTRAP), "app", "--hidden"],
        env=env,
        cwd=str(REPO_ROOT.parent),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _wait_for(Path(env["XDG_RUNTIME_DIR"]) / "astra-voice" / "ipc", proc)
        assert proc.poll() is None, _report(proc)
        yield proc, env
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_version_needs_no_display(tmp_path: Path) -> None:
    env = _env(tmp_path)
    env["QT_QPA_PLATFORM"] = "нет-такой-платформы"
    proc = _run(["--version"], env)
    assert proc.returncode == 0
    assert proc.stdout.startswith("astra-voice ")


def test_lock_is_created(
    first_instance: tuple[subprocess.Popen[str], dict[str, str]], tmp_path: Path
) -> None:
    _, env = first_instance
    assert (Path(env["XDG_RUNTIME_DIR"]) / "astra-voice" / "lock").exists()


def test_second_instance_exits_zero_and_first_gets_show(
    first_instance: tuple[subprocess.Popen[str], dict[str, str]], tmp_path: Path
) -> None:
    proc, env = first_instance
    second = _run([], env)
    assert second.returncode == 0, second.stderr
    _wait_for_log(tmp_path, "получена команда show")
    assert proc.poll() is None, "первый процесс не должен завершаться"


def test_unknown_command_is_rejected(
    first_instance: tuple[subprocess.Popen[str], dict[str, str]], tmp_path: Path
) -> None:
    proc, env = first_instance
    ipc = Path(env["XDG_RUNTIME_DIR"]) / "astra-voice" / "ipc"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.connect(str(ipc))
        sock.sendall(b"quit\n")
        sock.settimeout(5)
        try:
            sock.recv(16)
        except (TimeoutError, OSError):
            pass
    _wait_for_log(tmp_path, "отброшена неизвестная команда")
    assert "получена команда show" not in _log_text(tmp_path)
    assert proc.poll() is None, "неизвестная команда не должна ронять приложение"


def test_ipc_socket_is_private(
    first_instance: tuple[subprocess.Popen[str], dict[str, str]], tmp_path: Path
) -> None:
    ipc = Path(first_instance[1]["XDG_RUNTIME_DIR"]) / "astra-voice" / "ipc"
    assert ipc.stat().st_mode & 0o077 == 0
