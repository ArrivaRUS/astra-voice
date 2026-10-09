"""Actual app finalizer without Qt event loop; OS file-lock handoff probe."""

from __future__ import annotations

import ast
import fcntl
import inspect
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest

from astra_voice import app
from astra_voice.core import policy as policy_mod
from astra_voice.core.policy import Policy
from astra_voice.platform import userinstall

pytestmark = pytest.mark.unit


def exit_body(namespace: dict[str, Any]) -> Any:
    source = ast.parse(inspect.getsource(app.main)).body[0]
    assert isinstance(source, ast.FunctionDef)
    finalizer = next(node for node in reversed(source.body) if isinstance(node, ast.Try))
    index = source.body.index(finalizer)
    body = ast.parse("close_watcher = None").body + finalizer.finalbody + source.body[index + 1 :]
    fn = ast.FunctionDef(
        name="exit_body",
        args=ast.arguments(posonlyargs=[], args=[], kwonlyargs=[], kw_defaults=[], defaults=[]),
        body=body,
        decorator_list=[],
    )
    # The production finalizer tolerates unbound optional state via locals().get;
    # this harness keeps it in its supplied namespace.
    namespace["locals"] = lambda: namespace
    module = ast.fix_missing_locations(ast.Module(body=[fn], type_ignores=[]))
    exec(compile(module, inspect.getfile(app), "exec"), namespace)
    return namespace["exit_body"]()


def namespace(
    monkeypatch: pytest.MonkeyPatch, events: list[str], *, fault: str = ""
) -> dict[str, Any]:
    monkeypatch.setattr(app, "_installed_code_key", lambda: "key")
    monkeypatch.setattr(app, "_children_stopped", lambda: True)
    monkeypatch.setattr(app, "_copy_shutdown_resources", lambda *args: ([], []))

    def finish(key: str) -> None:
        events.append("finish")
        if fault == "finish":
            raise OSError("failed")

    monkeypatch.setattr(userinstall, "finish_running_copy", finish)
    monkeypatch.setattr(
        userinstall, "preserve_incomplete_shutdown", lambda key: events.append("preserve")
    )

    def runtime_stop() -> None:
        events.append("runtime-stop")
        if fault == "runtime":
            raise RuntimeError("failed")

    def close(timeout: float | None = None) -> bool:
        events.append("cancel" if timeout == 0 else "join")
        return timeout is None

    def launch(pending: Any, *, shutdown_complete: bool) -> int:
        events.append("launch" if shutdown_complete else "refuse-launch")
        return 0 if shutdown_complete else 1

    program = SimpleNamespace(
        worker_threads=(), close=close, pending=object(), preparer=SimpleNamespace(launch=launch)
    )

    def cleanup(*args: Any) -> bool:
        events.append("unlock")
        return fault != "cleanup"

    result = dict(vars(app))
    result.update(
        runtime=SimpleNamespace(shutdown=runtime_stop),
        downloads=None,
        update_checker=None,
        onboarding=None,
        appimage_management=None,
        program_updates=program,
        focuser=Mock(),
        timer=Mock(),
        theme_bridge=None,
        close_watcher=None,
        server=None,
        lock=None,
        exit_code=7,
        shutdown_notify_dispatch=lambda: events.append("notify-stop"),
        shutdown_bus_threads=lambda: events.append("bus-stop"),
        _cleanup=cleanup,
    )
    return result


@pytest.mark.parametrize("fault", ["", "finish", "runtime", "cleanup"])
def test_actual_finalizer_order_and_fail_closed(
    monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    events: list[str] = []
    result = exit_body(namespace(monkeypatch, events, fault=fault))
    assert result == (1 if fault else 0)
    assert events.index("cancel") < events.index("runtime-stop") < events.index("join")
    assert events.index("join") < events.index("preserve" if fault == "runtime" else "finish")
    assert events[-2:] == ["unlock", "refuse-launch" if fault else "launch"]


def test_os_lock_remains_held_through_finish_and_is_free_before_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock_file = tmp_path / "instance.lock"
    stream = lock_file.open("w")
    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def probe() -> bool:
        code = (
            "import fcntl,sys\nf=open(sys.argv[1],'r')\n"
            "try: fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)\n"
            "except BlockingIOError: sys.exit(2)\n"
        )
        return (
            subprocess.run(
                [sys.executable, "-c", code, str(lock_file)], timeout=2, check=False
            ).returncode
            == 0
        )

    class Lock:
        locked = True

        def unlock(self) -> None:
            fcntl.flock(stream, fcntl.LOCK_UN)
            self.locked = False

        def isLocked(self) -> bool:
            return self.locked

    events: list[str] = []
    ns = namespace(monkeypatch, events)

    def finish(key: str) -> None:
        assert not probe()
        events.append("finish")

    monkeypatch.setattr(userinstall, "finish_running_copy", finish)

    def launch(pending: Any, *, shutdown_complete: bool) -> int:
        assert shutdown_complete and probe()
        events.append("launch")
        return 0

    ns["program_updates"].preparer.launch = launch
    ns["_cleanup"] = app._cleanup
    ns["lock"] = Lock()
    monkeypatch.setattr(app, "ipc_socket_path", lambda: tmp_path / "socket")
    try:
        assert exit_body(ns) == 0
        assert events[-2:] == ["finish", "launch"]
    finally:
        stream.close()


def test_runtime_reservation_released_on_preparation_error_and_stale_callbacks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = object.__new__(app._ProgramUpdates)
    owner._closed = False
    owner._attempt = 2
    owner._reserved = True
    owner._runtime = Mock()
    owner._models = None
    owner._app = Mock()
    owner.bridge = Mock()
    owner.pending = None
    monkeypatch.setattr(policy_mod, "load", lambda: Policy())
    owner._prepared(1, None, "prepare-failed")
    owner.bridge.set_install_error.assert_not_called()
    owner._prepared(2, None, "hash-mismatch")
    owner._runtime.release_appimage_removal.assert_called_once()
    owner.bridge.set_install_error.assert_called_once_with("hash-mismatch", retryable=True)
    owner._app.quit.assert_not_called()


@pytest.mark.parametrize("throws", [False, True])
def test_quit_failure_keeps_ui_and_releases_reservation(
    monkeypatch: pytest.MonkeyPatch, throws: bool
) -> None:
    owner = object.__new__(app._ProgramUpdates)
    owner._closed = False
    owner._attempt = 1
    owner._reserved = True
    owner._runtime = Mock()
    owner._models = None
    owner._app = Mock()
    owner._app.quit.return_value = False
    if throws:
        owner._app.quit.side_effect = RuntimeError("quit failed")
    owner.bridge = Mock()
    owner.preparer = Mock()
    pending = Mock()
    monkeypatch.setattr(policy_mod, "load", lambda: Policy())
    owner._prepared(1, pending, "")
    assert owner.pending is None
    owner._runtime.release_appimage_removal.assert_called_once()
    owner.bridge.set_install_error.assert_called_once_with("shutdown-incomplete", retryable=True)


@pytest.mark.parametrize("listening", [False, True])
def test_ipc_close_covers_accepted_connections(listening: bool) -> None:
    server = SimpleNamespace(_server=Mock(), _connections=[Mock(), Mock()])
    server._server.isListening.return_value = listening
    if listening:
        with pytest.raises(RuntimeError, match="IPC listener"):
            app.ShowServer.close(server)  # type: ignore[arg-type]
    else:
        app.ShowServer.close(server)  # type: ignore[arg-type]
    server._server.close.assert_called_once()
    for connection in server._connections:
        connection.close.assert_called_once()


def test_backoff_expiry_notifies_subscriber_once_without_download_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from astra_voice.ui.updates_bridge import UpdatesBridge
    from astra_voice.updates.checker import UpdateStatus
    from astra_voice.updates.download import DownloadStatus
    from astra_voice.updates.release import VerifiedArtifact, VerifiedRelease

    now = [1000.0]
    monkeypatch.setattr(time, "time", lambda: now[0])
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    controller = Mock()
    controller.start.return_value = 1
    release = VerifiedRelease(
        "0.2.0",
        "v0.2.0",
        "",
        "",
        "",
        VerifiedArtifact("deb", "astra-voice_0.2.0_amd64.deb", "a" * 64, 10, ""),
        b"sums",
        b"sig",
        b"latest",
    )
    bridge = UpdatesBridge(controller=controller, clock=lambda: now[0])
    bridge.set_status(
        UpdateStatus(
            "available", version=release.version, raw_tag=release.raw_tag, verified_release=release
        )
    )
    bridge.download()
    status = DownloadStatus(operation_id=1, phase="error", error="rate-limited", retry_at=1060.0)
    observed: list[tuple[bool, str]] = []
    bridge.downloadChanged.connect(
        lambda: observed.append((bridge.canRetryDownload, bridge.downloadRetryText))
    )
    bridge.set_download_status(status)
    assert len(observed) == 1 and observed[-1][0] is False and observed[-1][1]
    owner = object.__new__(app._ProgramUpdates)
    owner._closed = False
    owner._last_refresh = 0.0
    owner._retry_notification = None
    controller.status = status
    owner.download = controller
    owner.bridge = bridge
    owner.refresh()
    assert len(observed) == 1
    now[0] = 1061.0
    owner.refresh()
    assert observed[-1] == (True, "")
    assert len(observed) == 2
    now[0] += 10.0
    owner.refresh()
    assert len(observed) == 2
    owner._closed = True
    owner.refresh()
    assert len(observed) == 2


def failing_cleanup_resources(
    monkeypatch: pytest.MonkeyPatch, failure: str, events: list[str]
) -> tuple[Mock, Mock]:
    def step(name: str, result: Any = None) -> Any:
        events.append(name)
        if failure == name:
            raise RuntimeError("deliberate cleanup failure")
        return result

    ipc = Mock()
    ipc.unlink.side_effect = lambda **kwargs: step("unlink")
    monkeypatch.setattr(app, "ipc_socket_path", lambda: step("path", ipc))
    server = Mock()
    server.close.side_effect = lambda: step("server")
    lock = Mock()
    lock.unlock.side_effect = lambda: step("unlock")
    lock.isLocked.side_effect = lambda: step("query", False)
    return server, lock


@pytest.mark.parametrize("failure", ["server", "path", "unlink", "unlock", "query"])
def test_cleanup_failure_attempts_remaining_steps_and_records_shutdown_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    import json

    from astra_voice.updates.restart import PendingRestart, RestartPreparer

    events: list[str] = []
    ns = namespace(monkeypatch, [])
    server, lock = failing_cleanup_resources(monkeypatch, failure, events)
    preparer = RestartPreparer(
        Mock(), "0.1.1", policy=Policy, record_path=tmp_path / "records" / "attempt.json"
    )
    download = Mock()
    download.release.version = "0.2.0"
    pending = PendingRestart(download, "a" * 32, "b" * 64, ())
    preparer._record(pending, "prepared")
    ns["program_updates"].preparer = preparer
    ns["program_updates"].pending = pending
    ns.update(server=server, lock=lock, _cleanup=app._cleanup)
    popen = Mock()
    monkeypatch.setattr("astra_voice.updates.restart.subprocess.Popen", popen)
    assert exit_body(ns) == 1
    assert events == [
        "server",
        "path",
        *([] if failure == "path" else ["unlink"]),
        "unlock",
        "query",
    ]
    record = json.loads(preparer.record_path.read_bytes())
    assert record["stage"] == "shutdown_failed"
    assert record["error"] == "shutdown-incomplete"
    popen.assert_not_called()


def test_unexpected_cleanup_exception_is_a_failed_restart_barrier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json

    from astra_voice.updates.restart import PendingRestart, RestartPreparer

    ns = namespace(monkeypatch, [])
    preparer = RestartPreparer(
        Mock(), "0.1.1", policy=Policy, record_path=tmp_path / "records" / "attempt.json"
    )
    download = Mock()
    download.release.version = "0.2.0"
    pending = PendingRestart(download, "a" * 32, "b" * 64, ())
    preparer._record(pending, "prepared")
    ns["program_updates"].preparer = preparer
    ns["program_updates"].pending = pending
    ns["_cleanup"] = Mock(side_effect=ValueError("unexpected failure"))
    popen = Mock()
    monkeypatch.setattr("astra_voice.updates.restart.subprocess.Popen", popen)
    assert exit_body(ns) == 1
    record = json.loads(preparer.record_path.read_bytes())
    assert record["stage"] == "shutdown_failed"
    assert record["error"] == "shutdown-incomplete"
    popen.assert_not_called()


@pytest.mark.parametrize("locked,expected", [(False, True), (True, False), (None, False)])
def test_cleanup_requires_confirmed_unlock(
    monkeypatch: pytest.MonkeyPatch, locked: bool | None, expected: bool
) -> None:
    events: list[str] = []
    server, lock = failing_cleanup_resources(monkeypatch, "", events)
    lock.isLocked.side_effect = None
    lock.isLocked.return_value = locked
    assert app._cleanup(server, lock) is expected
    assert events == ["server", "path", "unlink", "unlock"]
    lock.isLocked.assert_called_once_with()
