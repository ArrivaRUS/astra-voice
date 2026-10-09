"""Защита работающей AppImage-копии от чистки (arch/appimage.md §1, §4; Р5)."""

from __future__ import annotations

import ast
import inspect
import logging
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest

from astra_voice import app
from astra_voice.core import paths
from astra_voice.platform import autostart, userinstall
from helpers.appimage_bundle import KEY, make_bundle, module_path, tree_snapshot

pytestmark = pytest.mark.unit


def _command_runtime() -> SimpleNamespace:
    return SimpleNamespace(
        supervisor=None,
        _switch_candidate=None,
        command_hotkey=SimpleNamespace(backend=SimpleNamespace(process=None, _menu_thread=None)),
        _command_client=None,
        _command_session=None,
        shutdown=lambda: None,
    )


def _run_exit(runtime: Any, monkeypatch: pytest.MonkeyPatch, management: Any = None) -> list[str]:
    """Execute main's actual finalizer without starting GUI, buses or services."""
    from astra_voice.ui import tray

    events: list[str] = []
    monkeypatch.setattr(tray, "_bus_transport", None)
    monkeypatch.setattr(app, "_installed_code_key", lambda: KEY)
    monkeypatch.setattr(app, "_children_stopped", lambda: True)
    monkeypatch.setattr(userinstall, "finish_running_copy", lambda key: events.append("finish"))
    monkeypatch.setattr(
        userinstall, "preserve_incomplete_shutdown", lambda key: events.append("preserve")
    )
    namespace = dict(vars(app))
    namespace.update(
        runtime=runtime,
        downloads=None,
        update_checker=None,
        onboarding=None,
        appimage_management=management,
        focuser=Mock(),
        timer=Mock(),
        theme_bridge=None,
        close_watcher=None,
        server=None,
        lock=None,
        shutdown_notify_dispatch=lambda: events.append("notify-stop"),
        shutdown_bus_threads=lambda: events.append("bus-stop"),
        _cleanup=lambda server, lock: events.append("unlock"),
    )
    main = ast.parse(inspect.getsource(app.main)).body[0]
    assert isinstance(main, ast.FunctionDef)
    finalizer = next(node for node in reversed(main.body) if isinstance(node, ast.Try))
    code = ast.Module(body=finalizer.finalbody, type_ignores=[])
    exec(compile(code, inspect.getfile(app), "exec"), namespace)
    return events


@pytest.mark.parametrize("slot", ["_command_client", "_command_session"])
@pytest.mark.parametrize("alive", [False, True])
def test_cowork_qthread_exit_barrier(
    slot: str, alive: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    from PyQt5.QtCore import QThread

    done = threading.Event()
    entered = threading.Event()

    class WaitingThread(QThread):
        def run(self) -> None:
            entered.set()
            done.wait(5)

    thread = WaitingThread()
    runtime = _command_runtime()
    setattr(runtime, slot, SimpleNamespace(_thread=thread))
    thread.start()
    assert entered.wait(2)
    if not alive:
        done.set()
        assert thread.wait(2000)
    try:
        # Simulate timeout followed by owner forgetting its QThread reference.
        runtime.shutdown = lambda: setattr(runtime, slot, None)
        assert _run_exit(runtime, monkeypatch) == [
            "notify-stop",
            "bus-stop",
            "preserve" if alive else "finish",
            "unlock",
        ]
    finally:
        done.set()
        assert thread.wait(2000)


@pytest.mark.parametrize("late", [False, True])
def test_menu_restore_thread_exit_barrier(late: bool, monkeypatch: pytest.MonkeyPatch) -> None:
    done = threading.Event()
    thread = threading.Thread(target=done.wait, args=(5,))
    runtime = _command_runtime()
    backend = runtime.command_hotkey.backend

    def launch() -> None:
        backend._menu_thread = thread
        thread.start()

    if late:
        runtime.shutdown = launch
    else:
        launch()
        runtime.shutdown = lambda: setattr(backend, "_menu_thread", None)
    try:
        assert _run_exit(runtime, monkeypatch) == ["notify-stop", "bus-stop", "preserve", "unlock"]
    finally:
        done.set()
        thread.join(2)
        assert not thread.is_alive()


@pytest.mark.parametrize("slot", ["_command_client", "_command_session"])
def test_qthread_finished_signal_is_not_joined(slot: str, monkeypatch: pytest.MonkeyPatch) -> None:
    from PyQt5.QtCore import Qt, QThread

    entered = threading.Event()
    release = threading.Event()

    class FinishingThread(QThread):
        def run(self) -> None:
            pass

    def hold_finished() -> None:
        entered.set()
        release.wait(5)

    thread = FinishingThread()
    thread.finished.connect(hold_finished, Qt.DirectConnection)
    runtime = _command_runtime()
    setattr(runtime, slot, SimpleNamespace(_thread=thread))
    thread.start()
    try:
        assert entered.wait(2)
        assert thread.isRunning() is False
        assert thread.wait(0) is False
        assert _run_exit(runtime, monkeypatch) == ["notify-stop", "bus-stop", "preserve", "unlock"]
    finally:
        release.set()
        assert thread.wait(2000)
    assert _run_exit(runtime, monkeypatch) == ["notify-stop", "bus-stop", "finish", "unlock"]


def test_broker_reference_survives_shutdown(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = _command_runtime()
    # An owned process remains owned even if it uses a separate session.
    with subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stdin.buffer.read(1)"],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    ) as child:
        runtime.command_hotkey.backend.process = child
        runtime.shutdown = lambda: setattr(runtime.command_hotkey.backend, "process", None)
        try:
            assert _run_exit(runtime, monkeypatch) == [
                "notify-stop",
                "bus-stop",
                "preserve",
                "unlock",
            ]
            assert child.poll() is None
        finally:
            assert child.stdin is not None
            child.stdin.close()
            child.wait(timeout=2)


@pytest.mark.parametrize("fault", ["unknown", "raises", "non-bool"])
def test_unproved_thread_stop_preserves_copy(fault: str, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = _command_runtime()
    query = (
        Mock(side_effect=RuntimeError("unavailable")) if fault == "raises" else Mock(return_value=1)
    )
    thread = object() if fault == "unknown" else SimpleNamespace(wait=query)
    runtime._command_client = SimpleNamespace(_thread=thread)
    assert _run_exit(runtime, monkeypatch) == ["notify-stop", "bus-stop", "preserve", "unlock"]
    if fault != "unknown":
        query.assert_called_once_with(0)


@pytest.mark.parametrize("failed_snapshot", [1, 2])
def test_unreadable_resource_snapshot_preserves_copy(
    failed_snapshot: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = app._copy_shutdown_resources
    count = 0

    def capture(*args: Any) -> tuple[list[Any], list[Any]]:
        nonlocal count
        count += 1
        if count == failed_snapshot:
            raise RuntimeError("resource owner unavailable")
        return snapshot(*args)

    monkeypatch.setattr(app, "_copy_shutdown_resources", capture)
    assert _run_exit(_command_runtime(), monkeypatch) == [
        "notify-stop",
        "bus-stop",
        "preserve",
        "unlock",
    ]


@pytest.mark.parametrize("late", [False, True])
def test_management_worker_survives_controller_close(
    late: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    done = threading.Event()
    runtime = _command_runtime()

    class Worker(threading.Thread):
        def join(self, timeout: float | None = None) -> None:
            assert runtime.stopped
            done.set()
            super().join(timeout)

    thread = Worker(target=done.wait, args=(5,))
    runtime.stopped = False
    runtime.shutdown = lambda: setattr(runtime, "stopped", True)
    management = SimpleNamespace(worker_threads=(), close=lambda: True)

    def close() -> bool:
        if late:
            management.worker_threads = (thread,)
            thread.start()
        else:
            management.worker_threads = ()
        return True  # Snapshot must independently detect a faulty close contract.

    management.close = close
    if not late:
        management.worker_threads = (thread,)
        thread.start()
    try:
        assert _run_exit(runtime, monkeypatch, management) == [
            "notify-stop",
            "bus-stop",
            "finish",
            "unlock",
        ]
    finally:
        done.set()
        thread.join(2)
        assert not thread.is_alive()


@pytest.mark.parametrize("close_raises", [False, True])
@pytest.mark.parametrize("runtime_raises", [False, True])
def test_management_join_holds_lock_after_audio_stop(
    close_raises: bool, runtime_raises: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = threading.Event()
    entered = threading.Event()
    order: list[str] = []

    def work() -> None:
        entered.set()
        release.wait(5)
        order.append("worker-finished")

    class Worker(threading.Thread):
        def join(self, timeout: float | None = None) -> None:
            assert "runtime-stopped" in order
            assert timeout is None
            order.append("join")
            release.set()
            super().join(timeout)
            order.append("joined")

    def close() -> bool:
        was_pending = "close" in order
        order.append("close")
        if close_raises:
            raise RuntimeError("close failed")
        return was_pending

    def stop_runtime() -> None:
        order.append("runtime-stopped")
        if runtime_raises:
            raise RuntimeError("runtime failed")

    worker = Worker(target=work)
    management = SimpleNamespace(worker_threads=(worker,), close=close)
    runtime = _command_runtime()
    runtime.shutdown = stop_runtime
    worker.start()
    assert entered.wait(2)
    try:
        events = _run_exit(runtime, monkeypatch, management)
        assert order[:2] == ["close", "runtime-stopped"]
        assert "joined" in order
        assert order.index("join") < order.index("worker-finished") < order.index("joined")
        assert events == [
            "notify-stop",
            "bus-stop",
            "preserve" if close_raises or runtime_raises else "finish",
            "unlock",
        ]
        assert not worker.is_alive()
    finally:
        release.set()
        threading.Thread.join(worker, timeout=2)


@pytest.mark.parametrize("result", [True, False, None, RuntimeError("close failed")])
def test_management_close_precedes_runtime_and_controls_barrier(
    result: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    order: list[str] = []

    def close() -> object:
        order.append("management")
        if isinstance(result, Exception):
            raise result
        return result

    management = SimpleNamespace(worker_threads=(), close=close)
    runtime = _command_runtime()
    runtime.shutdown = lambda: order.append("runtime")
    assert _run_exit(runtime, monkeypatch, management) == [
        "notify-stop",
        "bus-stop",
        "finish" if result is True else "preserve",
        "unlock",
    ]
    assert order == ["management", "runtime"] + (["management"] if result is False else [])


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("HOME", str(home))
    for name in ("DATA", "CONFIG", "CACHE", "STATE"):
        monkeypatch.setenv(f"XDG_{name}_HOME", str(home / name.lower()))
    runtime = home / "run"
    runtime.mkdir(mode=0o700)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    monkeypatch.setattr(paths, "FALLBACK_TMP_DIR", tmp_path)
    monkeypatch.delenv(paths.APPIMAGE_DIR_ENV, raising=False)
    monkeypatch.delenv(paths.PORTABLE_ENV, raising=False)
    monkeypatch.delenv(paths.RESOURCES_ENV, raising=False)
    return home


@pytest.fixture
def installed_bundle(monkeypatch: pytest.MonkeyPatch) -> Path:
    bundle = make_bundle(paths.appimage_app_dir() / KEY)
    (bundle / paths.INSTALLED_MARKER).write_bytes(b"")
    monkeypatch.setattr(paths, "_code_file", lambda: module_path(bundle))
    assert paths.install_kind() is paths.InstallKind.APPIMAGE_INSTALLED
    return bundle


def test_installed_copy_writes_own_key(installed_bundle: Path, isolated_home: Path) -> None:
    """Даже после переключения current защищаем каталог работающего кода."""
    (installed_bundle.parent / "current").symlink_to("0.3.0-aaaaaaaaaaaa")

    app._mark_running_copy()

    marker = isolated_home / "run" / "astra-voice" / "running-key"
    assert marker.read_text(encoding="ascii") == KEY + "\n"


@pytest.mark.parametrize(
    "kind",
    [paths.InstallKind.DEB, paths.InstallKind.SOURCE, paths.InstallKind.APPIMAGE_PORTABLE],
)
def test_other_tracks_do_not_write(
    kind: paths.InstallKind,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_home: Path,
) -> None:
    if kind is paths.InstallKind.DEB:
        here = paths.INSTALL_LIB_DIR / "astra_voice" / "core" / "paths.py"
    elif kind is paths.InstallKind.APPIMAGE_PORTABLE:
        here = module_path(make_bundle(tmp_path / "portable"))
    else:
        here = tmp_path / "source" / "astra_voice" / "core" / "paths.py"
    monkeypatch.setattr(paths, "_code_file", lambda: here)
    assert paths.install_kind() is kind
    write = Mock()
    monkeypatch.setattr(userinstall, "write_running_key", write)
    before = tree_snapshot(tmp_path)

    app._mark_running_copy()

    write.assert_not_called()
    assert not (isolated_home / "run" / "astra-voice" / "running-key").exists()
    assert tree_snapshot(tmp_path) == before


@pytest.mark.parametrize("error_type", [OSError, paths.PathError, ValueError])
def test_write_failure_warns_without_home_path(
    error_type: type[Exception],
    installed_bundle: Path,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    marker = isolated_home / "run" / "astra-voice" / "running-key"
    write = Mock(side_effect=error_type(f"не удалось записать {marker}"))
    monkeypatch.setattr(userinstall, "write_running_key", write)

    with caplog.at_level(logging.WARNING, logger=app.__name__):
        app._mark_running_copy()

    write.assert_called_once_with(installed_bundle.name)
    assert len(caplog.records) == 1
    record = caplog.records[0]
    assert record.levelno == logging.WARNING
    assert str(isolated_home) not in record.getMessage()
    assert "~/run/astra-voice/running-key" in record.getMessage()
    assert record.exc_info is None
    assert not marker.exists()


def test_main_marks_only_after_lock_and_logging_before_qapplication() -> None:
    """Проверяем порядок и выход второго экземпляра без запуска main или Qt."""
    main = ast.parse(inspect.getsource(app.main)).body[0]
    assert isinstance(main, ast.FunctionDef)
    lock_guard = next(
        node
        for node in main.body
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.UnaryOp)
        and isinstance(node.test.op, ast.Not)
        and isinstance(node.test.operand, ast.Call)
        and isinstance(node.test.operand.func, ast.Attribute)
        and node.test.operand.func.attr == "tryLock"
    )
    assert len(lock_guard.body) == 1
    second_instance = lock_guard.body[0]
    assert isinstance(second_instance, ast.Return)
    # Второй экземпляр: с --hidden молча выходит, иначе просит показать окно.
    choice = second_instance.value
    assert isinstance(choice, ast.IfExp)
    assert ast.unparse(choice.test) == "args.hidden"
    assert isinstance(choice.body, ast.Constant) and choice.body.value == 0
    assert isinstance(choice.orelse, ast.Call)
    assert isinstance(choice.orelse.func, ast.Name)
    assert choice.orelse.func.id == "_send_show"
    calls = {
        node.func.id: node
        for node in ast.walk(main)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    mark = calls["_mark_running_copy"]
    assert lock_guard.end_lineno is not None
    assert lock_guard.end_lineno < calls["setup_logging"].lineno < mark.lineno
    assert mark.lineno < calls["QApplication"].lineno
    # Единственный вызов — на верхнем уровне main, сразу после журналирования.
    marks = [
        node
        for node in ast.walk(main)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_mark_running_copy"
    ]
    assert marks == [mark]
    statement = next(
        node
        for node in main.body
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.UnaryOp)
        and node.test.operand is mark
    )
    assert any(isinstance(node, ast.Return) for node in statement.body)
    previous = main.body[main.body.index(statement) - 1]
    assert isinstance(previous, ast.Expr)
    assert previous.value is calls["setup_logging"]


def test_register_argument_is_exact() -> None:
    assert app._parse_args(["--register"]).register
    assert not app._parse_args([]).register
    with pytest.raises(SystemExit) as error:
        app._parse_args(["--reg"])
    assert error.value.code == 2
    assert "--register" not in userinstall.SERVICE_FLAGS


@pytest.mark.parametrize("kind", [paths.InstallKind.SOURCE, paths.InstallKind.APPIMAGE_INSTALLED])
def test_register_precedes_lock_and_second_instance_show(
    kind: paths.InstallKind, monkeypatch: pytest.MonkeyPatch
) -> None:
    from PyQt5 import QtCore

    events: list[str] = []
    monkeypatch.setattr(paths, "install_kind", lambda: kind)
    register = Mock(side_effect=lambda: events.append("register"))
    monkeypatch.setattr(userinstall, "register", register)

    class Lock:
        def __init__(self, _path: str) -> None:
            events.append("lock")

        def tryLock(self, _timeout: int) -> bool:
            return False

    def show() -> int:
        events.append("show")
        return 0

    monkeypatch.setattr(QtCore, "QLockFile", Lock)
    monkeypatch.setattr(app, "_send_show", show)
    assert app.main(["--register"]) == 0
    assert events == (["register"] if kind is paths.InstallKind.APPIMAGE_INSTALLED else []) + [
        "lock",
        "show",
    ]
    assert register.call_count == (kind is paths.InstallKind.APPIMAGE_INSTALLED)


@pytest.mark.parametrize(
    "error_type",
    [userinstall.UserInstallError, OSError, paths.PathError, autostart.AutostartError],
)
def test_register_failure_warns_and_continues_without_qt(
    error_type: type[Exception],
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(paths, "install_kind", lambda: paths.InstallKind.APPIMAGE_INSTALLED)
    monkeypatch.setattr(
        userinstall, "register", Mock(side_effect=error_type(str(isolated_home / "bad")))
    )
    assert app.main(["--register", "--version"]) == 0
    output = capsys.readouterr()
    assert "Не удалось добавить Astra Voice в меню: ~/bad" in output.err
    assert str(isolated_home) not in output.err
    assert output.out.startswith("astra-voice ")


def test_unregister_is_service_flag_only_for_appimage(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """--unregister снимает регистрацию без GUI; в треке .deb и исходниках — отказ."""
    assert app._parse_args(["--unregister"]).unregister
    assert "--unregister" in userinstall.SERVICE_FLAGS
    unregister = Mock()
    monkeypatch.setattr(userinstall, "unregister", unregister)
    for kind in (paths.InstallKind.DEB, paths.InstallKind.SOURCE):
        monkeypatch.setattr(paths, "install_kind", lambda kind=kind: kind)
        assert app.main(["--unregister"]) == 2
    unregister.assert_not_called()
    for kind in (paths.InstallKind.APPIMAGE_INSTALLED, paths.InstallKind.APPIMAGE_PORTABLE):
        monkeypatch.setattr(paths, "install_kind", lambda kind=kind: kind)
        assert app.main(["--unregister", "--hidden"]) == 0
    assert unregister.call_count == 2
    assert capsys.readouterr().out.count("убран из меню и автозапуска") == 2


@pytest.mark.parametrize(
    "error_type",
    [userinstall.UserInstallError, OSError, paths.PathError, autostart.AutostartError, ValueError],
)
def test_unregister_failure_is_reported(
    error_type: type[Exception],
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(paths, "install_kind", lambda: paths.InstallKind.APPIMAGE_INSTALLED)
    monkeypatch.setattr(
        userinstall, "unregister", Mock(side_effect=error_type(str(isolated_home / "bad")))
    )
    assert app.main(["--unregister"]) == 1
    output = capsys.readouterr()
    assert "Не удалось убрать Astra Voice из меню и автозапуска: ~/bad" in output.err
    assert output.out == ""


def test_register_unencodable_path_warns_and_continues(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Ревью P3: путь не в UTF-8 (UnicodeEncodeError ⊂ ValueError) не роняет запуск."""
    monkeypatch.setattr(paths, "install_kind", lambda: paths.InstallKind.APPIMAGE_INSTALLED)
    error = UnicodeEncodeError("utf-8", "/home/u/\udcff", 8, 9, "surrogates not allowed")
    monkeypatch.setattr(userinstall, "register", Mock(side_effect=error))
    assert app.main(["--register", "--version"]) == 0
    output = capsys.readouterr()
    assert "Не удалось добавить Astra Voice в меню" in output.err
    assert output.out.startswith("astra-voice ")


def test_menu_entry_for_unencodable_path_raises_value_error() -> None:
    """Откуда берётся ValueError: запись меню кодируется в UTF-8 строго."""
    with pytest.raises(ValueError):
        userinstall.menu_entry_bytes(Path("/home/u/\udcff/AppRun"), "0.2.0")


@pytest.mark.parametrize(("argv", "shown"), [(["--hidden"], False), ([], True)])
def test_second_instance_hidden_does_not_show_window(
    argv: list[str], shown: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Автозапуск (--hidden) при уже работающей копии не выводит окно во фронт."""
    from PyQt5 import QtCore

    class Lock:
        def __init__(self, _path: str) -> None:
            pass

        def tryLock(self, _timeout: int) -> bool:
            return False

    show = Mock(return_value=0)
    monkeypatch.setattr(QtCore, "QLockFile", Lock)
    monkeypatch.setattr(app, "_send_show", show)
    assert app.main(argv) == 0
    assert show.called is shown
