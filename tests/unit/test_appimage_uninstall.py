"""Завершение ресурсов перед удалением установленного кода, без запуска GUI."""

from __future__ import annotations

import shutil
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from astra_voice import app
from astra_voice.core import paths
from astra_voice.platform import userinstall
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
