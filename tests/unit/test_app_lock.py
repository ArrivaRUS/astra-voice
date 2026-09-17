"""Single-instance: блокировка, протокол ``show``, отказ на прочих командах.

Два настоящих процесса без дисплея (``QT_QPA_PLATFORM=offscreen``), ресурсы
подменены пустым каталогом — GUI поднимается на запасном виджете.
"""

from __future__ import annotations

import importlib.util
import logging
import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import Mock

import pytest

# Тестам нужен настоящий процесс GUI, то есть PyQt5 (дисплей не нужен —
# `QT_QPA_PLATFORM=offscreen`). На машине разработчика без PyQt5 пропускаем,
# а в CI требуем: молчаливый пропуск там означал бы ложную зелень.
# find_spec подмодуля бросает ModuleNotFoundError, если нет родительского пакета.
_QT_MISSING = (
    importlib.util.find_spec("PyQt5") is None or importlib.util.find_spec("PyQt5.QtWidgets") is None
)

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
    # Qt Multimedia иначе запускает отдельный PulseAudio в тестовом runtime:
    # он переживает GUI-процесс и забирает звуковую карту у рабочего PipeWire.
    # Этим тестам нужен только IPC, поэтому аудиосервер полностью изолирован.
    pulse_config = tmp_path / "pulse-client.conf"
    pulse_config.write_text("autospawn = no\n", encoding="utf-8")
    env.update(
        {
            "QT_QPA_PLATFORM": "offscreen",
            "PULSE_CLIENTCONFIG": str(pulse_config),
            "PULSE_SERVER": f"unix:{tmp_path / 'no-pulse-server'}",
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


# ── протокол сокета (чистая функция, Qt не нужен) ──────────────────────────


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        (b"show", 0),
        (b"show\n", 0),
        (b"  show  \n", 0),
        (b"show 12345", 12345),
        (b"show 12345\n", 12345),
        (b"show 0", 0),
    ],
)
def test_parse_command_accepts(line: bytes, expected: int) -> None:
    from astra_voice.app import parse_command

    assert parse_command(line) == expected


@pytest.mark.parametrize(
    "line",
    [
        b"",
        b"\n",
        b"quit",
        b"Show",
        b"show!",
        b"showtime",
        b"show abc",
        b"show 12 34",
        b"show -5",
        b"show 12x",
        b"exec rm -rf /",
    ],
)
def test_parse_command_rejects(line: bytes) -> None:
    from astra_voice.app import parse_command

    assert parse_command(line) is None


# ── источник темы по виду сеанса ───────────────────────────────────────────


def test_theme_source_matches_session_kind() -> None:
    from astra_voice.app import make_theme_source
    from astra_voice.core.theme import FlyThemeSource, KdeThemeSource
    from astra_voice.platform.session import SessionKind

    assert isinstance(make_theme_source(SessionKind.FLY), FlyThemeSource)
    assert isinstance(make_theme_source(SessionKind.KDE), KdeThemeSource)
    assert isinstance(make_theme_source(SessionKind.OTHER), KdeThemeSource)


def test_close_to_tray_hides_window_without_quitting(monkeypatch: pytest.MonkeyPatch) -> None:
    from PyQt5.QtCore import QEvent, QObject

    from astra_voice import app as app_mod

    assert app_mod.CLOSE_TO_TRAY is True
    app = Mock()
    window = QObject()
    hide = Mock()
    monkeypatch.setattr(window, "hide", hide, raising=False)
    watcher = app_mod._wire_close(app, window, is_tray_ready=lambda: True)
    assert watcher is not None
    event = QEvent(QEvent.Close)

    assert watcher.eventFilter(window, event) is True
    assert not event.isAccepted()
    hide.assert_called_once_with()
    app.quit.assert_not_called()


@pytest.mark.parametrize("tray_state", ["unregistered", "missing", "error"])
def test_close_without_tray_quits_with_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, tray_state: str
) -> None:
    from PyQt5.QtCore import QEvent, QObject

    from astra_voice import app as app_mod

    app = Mock()
    window = QObject()
    hide = Mock()
    monkeypatch.setattr(window, "hide", hide, raising=False)
    predicate = Mock(return_value=False)
    if tray_state == "error":
        predicate.side_effect = RuntimeError("Tray /private/path")
    if tray_state == "missing":
        watcher = app_mod._wire_close(app, window)
    else:
        watcher = app_mod._wire_close(app, window, is_tray_ready=predicate)
    assert watcher is not None
    predicate.assert_not_called()
    event = QEvent(QEvent.Close)

    with caplog.at_level(logging.WARNING, logger=app_mod.__name__):
        assert watcher.eventFilter(window, event) is False

    assert event.isAccepted()
    hide.assert_not_called()
    app.quit.assert_called_once_with()
    assert caplog.record_tuples == [
        (
            app_mod.__name__,
            logging.WARNING,
            "Трей недоступен — закрытие окна завершает приложение",
        )
    ]
    assert all(record.exc_info is None for record in caplog.records)


def test_close_checks_tray_on_every_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    from PyQt5.QtCore import QEvent, QObject

    from astra_voice import app as app_mod

    app = Mock()
    window = QObject()
    hide = Mock()
    monkeypatch.setattr(window, "hide", hide, raising=False)
    predicate = Mock(return_value=False)
    watcher = app_mod._wire_close(app, window, is_tray_ready=predicate)
    assert watcher is not None
    predicate.assert_not_called()

    first_event = QEvent(QEvent.Close)
    assert watcher.eventFilter(window, first_event) is False
    assert first_event.isAccepted()
    predicate.assert_called_once_with()
    hide.assert_not_called()
    app.quit.assert_called_once_with()

    predicate.return_value = True
    app.quit.reset_mock()
    second_event = QEvent(QEvent.Close)
    assert watcher.eventFilter(window, second_event) is True
    assert not second_event.isAccepted()
    assert predicate.call_count == 2
    hide.assert_called_once_with()
    app.quit.assert_not_called()


# ── первый запуск создаёт settings.json ────────────────────────────────────


def test_settings_file_created_on_first_start(
    first_instance: tuple[subprocess.Popen[str], dict[str, str]], tmp_path: Path
) -> None:
    settings = tmp_path / "config" / "astra-voice" / "settings.json"
    _wait_for(settings, first_instance[0], timeout=10.0)
    assert settings.stat().st_mode & 0o777 == 0o600
    import json

    data = json.loads(settings.read_text(encoding="utf-8"))
    assert data["schema_version"] == 1
    assert data["check_app_updates"] is False
