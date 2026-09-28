"""Запуск тестовых сценариев на закрытой сессионной D-Bus шине."""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_BUS_CONFIG = """\
<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-Bus Bus Configuration 1.0//EN" "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig>
  <type>session</type>
  <listen>unix:tmpdir=/tmp</listen>
  <auth>EXTERNAL</auth>
  <policy context="default">
    <allow send_destination="*" eavesdrop="true"/>
    <allow eavesdrop="true"/>
    <allow own="*"/>
  </policy>
</busconfig>
"""


def require_private_bus() -> None:
    """Пропустить тест без dbus-run-session или провалить обязательный прогон."""
    if shutil.which("dbus-run-session") is not None:
        return
    reason = "нужен dbus-run-session (пакет dbus-daemon)"
    if os.environ.get("ASTRA_VOICE_REQUIRE_QT") == "1":
        pytest.fail(reason)
    pytest.skip(reason)


def _living_processes_in_group(pgrp: int) -> list[int]:
    """Найти живых потомков группы по stat без поиска по командной строке."""
    found: list[int] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdecimal():
            continue
        try:
            stat = (entry / "stat").read_text(encoding="utf-8")
            fields = stat[stat.rfind(")") + 1 :].split()
            # После comm идут state (поле 3), ppid (4), pgrp (5).
            if fields[0] not in {"Z", "X"} and int(fields[2]) == pgrp:
                found.append(int(entry.name))
        except (FileNotFoundError, ProcessLookupError, PermissionError, ValueError, IndexError):
            continue
    return sorted(found)


def run_on_private_bus(
    argv: Sequence[str],
    *,
    tmp_dir: Path,
    timeout: float,
    extra_env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Запустить сценарий; при успехе проверить отсутствие процессов его группы."""
    require_private_bus()
    config = tmp_dir / "isolated-bus.conf"
    config.write_text(_BUS_CONFIG, encoding="utf-8")
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    for name in (
        "DISPLAY",
        "WAYLAND_DISPLAY",
        "DBUS_SESSION_BUS_ADDRESS",
        "AT_SPI_BUS_ADDRESS",
        "QT_ACCESSIBILITY",
    ):
        env.pop(name, None)
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["PYTHONPATH"] = os.pathsep.join(
        filter(None, (str(_ROOT / "src"), env.get("PYTHONPATH", "")))
    )
    command = ["dbus-run-session", f"--config-file={config}", "--", *argv]
    proc = subprocess.Popen(
        command,
        start_new_session=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        stdout, stderr = proc.communicate()
        pytest.fail(f"таймаут {timeout:g} с; stdout={stdout!r}; stderr={stderr!r}")
    leaked = _living_processes_in_group(proc.pid)
    if leaked:
        for pid in leaked:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        pytest.fail(f"утечка процессов сценария (pgrp={proc.pid}): {leaked}")
    return subprocess.CompletedProcess(command, proc.returncode, stdout, stderr)


def assert_private_bus_result(result: subprocess.CompletedProcess[str], *, marker: str) -> None:
    """Проверить код возврата, маркер сценария и отсутствие активации служб."""
    if (
        result.returncode != 0
        or marker not in result.stdout
        or "Activating service" in result.stderr
    ):
        pytest.fail(
            f"rc={result.returncode}\n--- stdout ---\n{result.stdout}"
            f"\n--- stderr ---\n{result.stderr}",
            pytrace=False,
        )
