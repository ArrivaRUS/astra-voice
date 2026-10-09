"""Завершение ресурсов перед удалением установленного кода, без запуска GUI."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock

import pytest

from astra_voice import app
from astra_voice.core import paths
from astra_voice.platform import external, sound, userinstall
from astra_voice.runtime import DictationRuntime
from astra_voice.worker.supervisor import WorkerSupervisor
from helpers.appimage_bundle import KEY, make_bundle, module_path

pytestmark = pytest.mark.unit


@pytest.fixture
def copy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    for name in ("DATA", "CONFIG", "STATE", "CACHE"):
        monkeypatch.setenv(f"XDG_{name}_HOME", str(tmp_path / name.lower()))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    monkeypatch.setattr(paths, "FALLBACK_TMP_DIR", tmp_path)
    monkeypatch.setattr(paths, "USER_RUNTIME_ROOT", tmp_path / "sessions")
    monkeypatch.setattr(paths, "SYSTEM_EXECUTABLE", tmp_path / "no-deb")
    root = make_bundle(paths.appimage_app_dir() / KEY)
    (root / paths.INSTALLED_MARKER).touch()
    monkeypatch.setattr(paths, "_code_file", lambda: module_path(root))
    monkeypatch.setattr(userinstall, "refuse_root", lambda: None)
    return root


def test_deleted_code_cannot_publish_running_key(copy: Path) -> None:
    shutil.rmtree(copy)
    assert not app._mark_running_copy()
    assert userinstall.read_running_key() is None


def test_finish_after_worker_exit_removes_pending_copy(copy: Path) -> None:
    assert app._mark_running_copy()
    assert userinstall.remove_program().kept == (KEY,)
    app._finish_running_copy(KEY, [Mock(poll=Mock(return_value=0))], [], stopped=True)
    assert not copy.exists()
    assert userinstall.read_running_key() is None


@pytest.mark.parametrize("reason", ["worker", "thread", "shutdown-error"])
def test_incomplete_shutdown_protects_code_from_later_cleanup(copy: Path, reason: str) -> None:
    assert app._mark_running_copy()
    userinstall.remove_program()
    processes = [Mock(poll=Mock(return_value=None))] if reason == "worker" else []
    threads = [Mock(is_alive=Mock(return_value=True))] if reason == "thread" else []
    app._finish_running_copy(KEY, processes, threads, stopped=reason != "shutdown-error")
    assert (copy / userinstall.SHUTDOWN_INCOMPLETE).is_file()
    assert userinstall.read_running_key() == KEY
    # Даже потеря stale runtime-маркера не разрешает удалить непроверенный код.
    (paths.runtime_dir() / userinstall.RUNNING_KEY_NAME).unlink()
    assert userinstall.cleanup() == []
    with pytest.raises(userinstall.UserInstallError, match="не подтверждена"):
        userinstall.remove_program()
    with pytest.raises(userinstall.UserInstallError, match="не подтверждена"):
        userinstall.install_from_dir(copy)
    assert copy.is_dir()


def test_capture_keeps_workers_and_threads_even_if_shutdown_forgets_them() -> None:
    current, retired, candidate_process = Mock(), Mock(), Mock()
    model_thread, update_thread = Mock(), Mock()
    supervisor = SimpleNamespace(process=current, _retired=[retired])
    candidate = SimpleNamespace(process=candidate_process, _retired=[])
    runtime = SimpleNamespace(supervisor=supervisor, _switch_candidate=candidate)
    downloads = SimpleNamespace(_model_thread=model_thread)
    checker = SimpleNamespace(_thread=update_thread)
    processes, threads = app._copy_shutdown_resources(runtime, downloads, checker)
    supervisor._retired.clear()
    downloads._model_thread = checker._thread = None
    assert processes == [retired, current, candidate_process]
    assert threads == [model_thread, update_thread]


def _sleep_command() -> list[str]:
    return [sys.executable, "-I", "-c", "import time; time.sleep(60)"]


def _stop_test_child(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


@pytest.mark.parametrize("launcher", ["sound", "external"])
def test_external_launch_contract_allows_ordinary_exit(copy: Path, launcher: str) -> None:
    children: list[subprocess.Popen[bytes]] = []

    def spawn(_command: list[str], **kwargs: Any) -> subprocess.Popen[bytes]:
        child = subprocess.Popen(_sleep_command(), **kwargs)
        children.append(child)
        return child

    try:
        if launcher == "sound":
            assert sound._spawn(["unused-settings-command"], popen=spawn)
        else:
            assert external.open_external("file:///unused-test-location", popen=spawn)
        child = children[0]
        assert os.getsid(child.pid) != os.getsid(0)
        assert child.poll() is None
        assert app._mark_running_copy()
        app._finish_running_copy(KEY, [], [], stopped=True)
        assert copy.is_dir()
        assert not (copy / userinstall.SHUTDOWN_INCOMPLETE).exists()
        assert userinstall.read_running_key() is None
        # Обычный выход с открытым окном хоста не блокирует последующий uninstall.
        assert KEY in userinstall.remove_program().removed
    finally:
        for child in children:
            _stop_test_child(child)


def test_failed_switch_forgotten_worker_still_protects_code(
    copy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = WorkerSupervisor(
        on_event=lambda event: None, command_factory=lambda fd: _sleep_command(), use_qt=False
    )
    candidate._launch()
    child = candidate.process
    assert child is not None
    try:
        assert os.getsid(child.pid) == os.getsid(0)
        monkeypatch.setattr(
            candidate, "stop", Mock(side_effect=subprocess.TimeoutExpired("test worker", 2))
        )
        state = SimpleNamespace(
            _switch_in_progress=True,
            _switch_timer=None,
            _switch_candidate=candidate,
            _cleanup=DictationRuntime._cleanup,
            on_switch_finished=None,
            supervisor=SimpleNamespace(process=None, _retired=[]),
        )
        # Настоящий путь неудачного switch забывает candidate после проглоченной
        # ошибки stop. Текущие поля runtime уже не позволяют найти живого ребёнка.
        DictationRuntime._finish_switch(cast(DictationRuntime, state), "failed")
        assert state._switch_candidate is None
        processes, threads = app._copy_shutdown_resources(state, None, None)
        assert child not in processes
        assert child.poll() is None
        assert app._mark_running_copy()
        userinstall.remove_program()
        app._finish_running_copy(KEY, processes, threads, stopped=True)
        assert copy.is_dir()
        assert (copy / userinstall.SHUTDOWN_INCOMPLETE).is_file()
    finally:
        candidate._close_connection()
        _stop_test_child(child)
