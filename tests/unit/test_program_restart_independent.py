"""Independent handoff across the actual application finalizer and OS resources.

No Qt loop, microphone, installed AppImage or user services are started. The
signed child is a disposable /tmp script and all locks/sockets belong to the test.
"""

from __future__ import annotations

import fcntl
import importlib
import json
import os
import socket
import subprocess
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest

from astra_voice import app
from astra_voice.core import policy as policy_mod
from astra_voice.core.policy import Policy

# Runtime-only reuse of ephemeral signature infrastructure and source harness.
_trust = importlib.import_module("updates.test_program_restart_trust_independent")
signing_keys = _trust.signing_keys
signed_restart = _trust.signed_restart
_exit_body = importlib.import_module("unit.test_restart_lifecycle").exit_body

pytestmark = _trust.pytestmark


@pytest.mark.parametrize("shutdown_failure", [False, True])
def test_real_child_handoff_only_after_actual_finalizer_releases_lock_and_ipc(
    signed_restart: Any, monkeypatch: pytest.MonkeyPatch, shutdown_failure: bool
) -> None:
    case = signed_restart
    events: list[str] = []
    children: list[subprocess.Popen[bytes]] = []
    inherited = (case.bundle.parent / "inheritable-descriptor").open("wb")
    os.set_inheritable(inherited.fileno(), True)
    monkeypatch.setenv("TEST_RESTART_FD", str(inherited.fileno()))
    monkeypatch.setenv("APPIMAGE", "/test-only/current-image")
    monkeypatch.setenv("APPIMAGE_EXTRACT_AND_RUN", "1")
    monkeypatch.setenv("ASTRA_VOICE_ORIG_UNSET", "PYTHONHOME")
    monkeypatch.setenv("PYTHONHOME", "/test-only/bundle-python")
    pending = case.prepare()
    lock_stream = case.lock_path.open("wb")
    fcntl.flock(lock_stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
    ipc = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    ipc.bind(str(case.ipc_path))
    real_popen = subprocess.Popen

    def launch(argv: Any, **kwargs: Any) -> subprocess.Popen[bytes]:
        assert events[-1] == "unlock"
        assert not case.ipc_path.exists()
        record = json.loads(case.preparer.record_path.read_bytes())
        assert record["stage"] == "launch_requested"
        events.append("launch")
        child = real_popen(argv, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(subprocess, "Popen", launch)

    class Lock:
        locked = True

        def unlock(self) -> None:
            events.append("unlock")
            fcntl.flock(lock_stream, fcntl.LOCK_UN)
            self.locked = False

        def isLocked(self) -> bool:
            return self.locked

    class Server:
        def close(self) -> None:
            events.append("ipc-close")
            ipc.close()

    def stop_runtime() -> None:
        events.append("runtime-stop")
        if shutdown_failure:
            raise RuntimeError("test-only refused teardown")

    def close_updates(timeout: float | None = None) -> bool:
        events.append("cancel" if timeout == 0 else "join")
        return True

    monkeypatch.setattr(app, "_installed_code_key", lambda: None)
    monkeypatch.setattr(app, "_copy_shutdown_resources", lambda *args: ([], []))
    monkeypatch.setattr(app, "_children_stopped", lambda: True)
    monkeypatch.setattr(app, "ipc_socket_path", lambda: case.ipc_path)
    namespace = dict(vars(app))
    namespace.update(
        runtime=SimpleNamespace(shutdown=stop_runtime),
        downloads=None,
        update_checker=None,
        onboarding=None,
        appimage_management=None,
        program_updates=SimpleNamespace(
            worker_threads=(),
            pending=pending,
            preparer=case.preparer,
            close=close_updates,
        ),
        focuser=Mock(),
        timer=Mock(),
        theme_bridge=None,
        server=Server(),
        lock=Lock(),
        exit_code=9,
        shutdown_notify_dispatch=lambda: events.append("notify-stop"),
        shutdown_bus_threads=lambda: events.append("bus-stop"),
    )
    try:
        result = _exit_body(namespace)
        assert result == (1 if shutdown_failure else 0)
        assert events.index("cancel") < events.index("runtime-stop") < events.index("join")
        assert events.index("join") < events.index("ipc-close") < events.index("unlock")
        if shutdown_failure:
            assert not children and not case.output.exists()
            record = json.loads(case.preparer.record_path.read_bytes())
            assert record["stage"] == "shutdown_failed"
            assert record["error"] == "shutdown-incomplete"
        else:
            assert len(children) == 1
            assert children[0].wait(timeout=3) == 0
            report = json.loads(case.output.read_bytes())
            assert report["argv"] == [str(case.image), "--appimage-extract-and-run"]
            assert report["lock_free"] and report["ipc_absent"] and report["session"]
            assert not report["fd_inherited"] and report["bundle_vars"] == []
            assert report["pythonhome"] is None and report["private_mode"] == 0o700
            assert report["tmp"] == dict(pending.environment)["TMPDIR"]
            record = json.loads(case.preparer.record_path.read_bytes())
            assert record["stage"] == "launch_requested"  # spawn is not installed success
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=3)
        inherited.close()
        lock_stream.close()
        ipc.close()


def test_duplicate_and_late_prepared_events_cannot_reuse_runtime_reservation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = object.__new__(app._ProgramUpdates)
    owner._closed = False
    owner._attempt = 10
    owner._reserved = True
    owner._runtime = Mock()
    owner._models = None
    owner._app = Mock()
    owner.bridge = Mock()
    owner.pending = None
    owner.preparer = Mock()
    owner.download = Mock()
    owner.restart = Mock()
    owner.download.close.return_value = True
    owner.restart.close.return_value = False
    monkeypatch.setattr(policy_mod, "load", lambda: Policy())
    owner._prepared(10, None, "hash-mismatch")
    owner._prepared(10, Mock(), "")
    assert owner.pending is None and not owner._reserved
    owner._runtime.release_appimage_removal.assert_called_once()
    owner._app.quit.assert_not_called()
    owner.bridge.set_install_error.assert_called_once_with("hash-mismatch", retryable=True)
    # A later explicit attempt owns a fresh reservation. Closing revokes callbacks.
    owner._attempt = 12
    owner._reserved = True
    assert not owner.close(0)
    owner._prepared(12, Mock(), "")
    assert owner.pending is None and not owner._reserved
    assert owner._runtime.release_appimage_removal.call_count == 2
    owner.download.close.assert_called_once_with(0)
    owner.restart.close.assert_called_once_with(0)
    owner._app.quit.assert_not_called()
