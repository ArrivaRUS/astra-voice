"""Independent AppImage consent, resource-barrier and input ownership contracts.

Only temporary HOME trees are mutated; no installed application is launched.
External runtime resources use the established Rig, but DictationRuntime,
DictationOrchestrator, HotkeyFsm, QObject, filesystem locks and backend are real.
"""

from __future__ import annotations

import ast
import fcntl
import inspect
import os
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest
from PyQt5.QtCore import QLockFile, QMetaObject, Qt
from test_runtime import Rig

from astra_voice import app as composition
from astra_voice.core import paths
from astra_voice.core.dictation import DictationPhase
from astra_voice.platform import userinstall
from astra_voice.platform.hotkey import HotkeyFsm
from astra_voice.ui import appimage_management as management
from helpers.appimage_bundle import make_bundle, tree_snapshot
from helpers.qt_app import get_qapplication

pytestmark = pytest.mark.unit
KEY = "0.2.0-aaaaaaaaaaaa"
OTHER = "0.3.0-bbbbbbbbbbbb"


def wait_for(predicate: Callable[[], bool], seconds: float = 4.0) -> None:
    application = get_qapplication()
    deadline = time.monotonic() + seconds
    while not predicate() and time.monotonic() < deadline:
        application.processEvents()
        time.sleep(0.005)
    assert predicate(), "Qt/worker state failed to settle"


class Profile:
    def __init__(self, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.root = root
        self.home = root / "дом с пробелами"
        self.home.mkdir(mode=0o700)
        monkeypatch.setenv("HOME", str(self.home))
        for name in ("DATA", "CONFIG", "CACHE", "STATE"):
            monkeypatch.delenv(f"XDG_{name}_HOME", raising=False)
        self.runtime_root = root / "runtime"
        self.runtime_root.mkdir(mode=0o700)
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(self.runtime_root))
        monkeypatch.setattr(paths, "FALLBACK_TMP_DIR", root / "fallback")
        monkeypatch.setattr(paths, "USER_RUNTIME_ROOT", root / "sessions")
        monkeypatch.setattr(paths, "SYSTEM_EXECUTABLE", root / "deb-astra-voice")
        monkeypatch.setattr(paths, "install_kind", lambda: paths.InstallKind.APPIMAGE_PORTABLE)
        self.app = paths.appimage_app_dir()
        self.app.mkdir(parents=True, mode=0o700)
        self.copy(KEY)
        self.menu = self.home / ".local/share/applications/astra-voice.desktop"
        self.menu.parent.mkdir(parents=True)
        self.menu.write_text("[Desktop Entry]\nX-AstraVoice-Managed=true\nExec=AppRun\n")
        self.models = paths.model_store_dir_path()
        self.models.mkdir(parents=True)
        (self.models / "weights.onnx").write_bytes(b"model preserved")
        self.settings = paths.config_dir_path()
        self.settings.mkdir(parents=True)
        (self.settings / "settings.json").write_bytes(b"settings preserved")
        self.logs = paths.log_dir_path()
        self.logs.mkdir()
        (self.logs / "voice.log").write_bytes(b"logs preserved")
        self.source = self.home / "Astra Voice.AppImage"
        self.source.write_bytes(b"original source preserved")
        self.own = self.runtime_root / paths.APP_NAME
        self.own.mkdir(mode=0o700)
        self.lock = QLockFile(str(self.own / "lock"))
        self.lock.setStaleLockTime(0)
        assert self.lock.tryLock(0)
        self.bridges: list[management.AppImageManagement] = []

    def copy(self, key: str) -> None:
        version, build = key.rsplit("-", 1)
        target = make_bundle(self.app / key, version=version, build_id=build)
        (target / paths.INSTALLED_MARKER).touch()

    def bridge(
        self, runtime: Any | None = None, key: str | None = None
    ) -> management.AppImageManagement:
        if runtime is None:
            runtime = Mock()
            runtime.appimage_remove_busy_reason.return_value = ""
            runtime.reserve_appimage_removal.return_value = True
        bridge = management.AppImageManagement(
            runtime, own_lock_path=self.own / "lock", running_key=key
        )
        self.bridges.append(bridge)
        wait_for(lambda: bridge.state not in ("checking",))
        assert bridge.state in ("ready", "absent", "busy", "hidden")
        return bridge

    def preserved(self) -> tuple[Any, ...]:
        return (
            tree_snapshot(self.models),
            tree_snapshot(self.settings),
            tree_snapshot(self.logs),
            self.source.read_bytes(),
        )


@pytest.fixture
def profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Profile]:
    get_qapplication()
    candidate = Profile(tmp_path, monkeypatch)
    try:
        yield candidate
    finally:
        for bridge in candidate.bridges:
            assert bridge.close(), "independent test left an operation thread alive"
        candidate.lock.unlock()


def consent(bridge: management.AppImageManagement, action: str = "remove") -> str:
    bridge.requestAction(action)
    wait_for(lambda: bridge.state != "checking")
    assert bridge.state == "confirming", bridge.errorText
    token = bridge.confirmationId
    assert isinstance(token, str)
    assert token and bridge.appPath in bridge.confirmationMessage
    return token


def finish(bridge: management.AppImageManagement) -> None:
    wait_for(lambda: bridge.state not in ("checking", "removing", "unregistering"))


def test_real_qobject_public_contract(profile: Profile) -> None:
    bridge = profile.bridge()
    meta = bridge.metaObject()
    for name in (
        "installKind",
        "state",
        "canRemove",
        "canUnregister",
        "appPath",
        "busyReason",
        "errorText",
        "resultText",
        "confirmationId",
        "confirmationMessage",
        "confirmationAction",
    ):
        index = meta.indexOfProperty(name)
        assert index >= 0, name
        assert meta.property(index).hasNotifySignal(), name
    for signature in (
        "refresh()",
        "requestAction(QString)",
        "confirmAction(QString)",
        "cancelConfirmation(QString)",
        "retry()",
    ):
        assert meta.indexOfMethod(signature.encode()) >= 0, signature
    QMetaObject.invokeMethod(bridge, "refresh", Qt.DirectConnection)
    finish(bridge)
    assert bridge.canRemove


@pytest.mark.parametrize("action", ["remove", "unregister"])
def test_cancel_and_forged_tokens_do_not_write(profile: Profile, action: str) -> None:
    bridge = profile.bridge()
    token = consent(bridge, action)
    before = tree_snapshot(profile.root)
    bridge.confirmAction("not-the-dialog-token")
    bridge.cancelConfirmation("not-the-dialog-token")
    assert bridge.state == "confirming"
    bridge.cancelConfirmation(token)
    bridge.confirmAction(token)
    assert bridge.state == "ready"
    assert tree_snapshot(profile.root) == before
    assert not bridge.worker_threads


@pytest.mark.parametrize(
    "change", ["path", "deb-added", "deb-mode", "copies", "copy-replaced", "menu"]
)
def test_stale_consent_never_mutates_changed_installation(
    profile: Profile, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    bridge = profile.bridge()
    token = consent(bridge)
    if change == "path":
        moved = profile.app.with_name("other-app")
        profile.app.rename(moved)
        monkeypatch.setattr(paths, "appimage_app_dir", lambda: moved)
    elif change == "deb-added":
        paths.SYSTEM_EXECUTABLE.write_bytes(b"package executable")
        paths.SYSTEM_EXECUTABLE.chmod(0o755)
    elif change == "deb-mode":
        paths.SYSTEM_EXECUTABLE.write_bytes(b"package executable")
        paths.SYSTEM_EXECUTABLE.chmod(0o644)
    elif change == "copies":
        profile.copy(OTHER)
    elif change == "copy-replaced":
        (profile.app / KEY).rename(profile.app / "saved-copy")
        profile.copy(KEY)
    else:
        profile.menu.write_bytes(b"foreign menu replacement")
    before = tree_snapshot(profile.root)
    quit_spy = Mock()
    bridge.quitRequested.connect(quit_spy)
    bridge.confirmAction(token)
    finish(bridge)
    assert bridge.state == "error"
    assert "изменилось" in bridge.errorText
    assert tree_snapshot(profile.root) == before
    quit_spy.assert_not_called()
    bridge.confirmAction(token)
    assert not bridge.worker_threads
    bridge.retry()
    finish(bridge)
    assert bridge.state == "confirming"
    assert bridge.confirmationId != token
    bridge.cancelConfirmation(bridge.confirmationId)


@pytest.mark.parametrize("kind", [paths.InstallKind.DEB, paths.InstallKind.SOURCE])
def test_non_appimage_track_has_no_destructive_action(
    profile: Profile, monkeypatch: pytest.MonkeyPatch, kind: paths.InstallKind
) -> None:
    monkeypatch.setattr(paths, "install_kind", lambda: kind)
    bridge = profile.bridge()
    before = tree_snapshot(profile.root)
    bridge.requestAction("remove")
    assert bridge.state == "hidden"
    assert not bridge.canRemove and not bridge.canUnregister
    assert not bridge.confirmationId
    assert tree_snapshot(profile.root) == before


def test_double_confirm_single_mutation_real_backend(
    profile: Profile, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge = profile.bridge()
    token = consent(bridge)
    original = userinstall.remove_program
    tracked = Mock(wraps=original)
    monkeypatch.setattr(userinstall, "remove_program", tracked)
    quit_spy = Mock()
    bridge.quitRequested.connect(quit_spy)
    before = profile.preserved()
    bridge.confirmAction(token)
    bridge.confirmAction(token)
    finish(bridge)
    tracked.assert_called_once()
    quit_spy.assert_called_once()
    assert bridge.state == "exiting"
    assert "подготовлено" in bridge.resultText
    assert "Программа удалена" not in bridge.resultText
    assert not (profile.app / KEY).exists()
    assert profile.preserved() == before
    assert profile.app.is_dir() and (profile.app / ".install.lock").exists()


def real_runtime(monkeypatch: pytest.MonkeyPatch) -> Rig:
    rig = Rig(monkeypatch, cowork_installed=False)
    rig.hotkey.has_pending_press = False
    rig.hotkey.fsm = HotkeyFsm(on_state=rig.runtime._on_hotkey_state)
    assert rig.runtime.appimage_remove_busy_reason() == ""
    return rig


def test_recording_starts_after_request_blocks_confirmation(
    profile: Profile, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = real_runtime(monkeypatch)
    bridge = profile.bridge(rig.runtime)
    token = consent(bridge)
    before = tree_snapshot(profile.root)
    rig.runtime.orchestrator._phase = DictationPhase.RECORDING
    bridge.confirmAction(token)
    assert bridge.state == "busy"
    assert not bridge.confirmationId and not bridge.worker_threads
    assert rig.runtime.orchestrator.phase is DictationPhase.RECORDING
    assert tree_snapshot(profile.root) == before
    rig.runtime.orchestrator._phase = DictationPhase.IDLE
    bridge.confirmAction(token)
    assert not bridge.worker_threads
    rig.runtime.shutdown()


@pytest.mark.parametrize(
    "phase",
    [
        DictationPhase.RECORDING,
        DictationPhase.PROCESSING,
        DictationPhase.PASTING,
        DictationPhase.DELIVERING,
        DictationPhase.FINISHING,
    ],
)
def test_real_runtime_inflight_phases_deny_removal(
    profile: Profile, monkeypatch: pytest.MonkeyPatch, phase: DictationPhase
) -> None:
    rig = real_runtime(monkeypatch)
    rig.runtime.orchestrator._phase = phase
    assert rig.runtime.appimage_remove_busy_reason()
    assert not rig.runtime.reserve_appimage_removal()
    assert rig.runtime.orchestrator.phase is phase
    rig.runtime.orchestrator._phase = DictationPhase.IDLE
    rig.runtime.shutdown()


@pytest.mark.parametrize(
    "field,value",
    [
        ("_preview_send", lambda: None),
        ("_pending_test", ("device", Mock())),
        ("_loading_model", True),
        ("_selfcheck", "running"),
        ("_capture_watchdog", object()),
        ("_mouse_capture_token", object()),
    ],
)
def test_real_runtime_pending_work_denies_removal(
    profile: Profile, monkeypatch: pytest.MonkeyPatch, field: str, value: Any
) -> None:
    rig = real_runtime(monkeypatch)
    original = getattr(rig.runtime, field)
    setattr(rig.runtime, field, value)
    assert rig.runtime.appimage_remove_busy_reason()
    assert not rig.runtime.reserve_appimage_removal()
    setattr(rig.runtime, field, original)
    rig.runtime.shutdown()


def test_real_runtime_reservation_blocks_new_input_and_restart(
    profile: Profile, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = real_runtime(monkeypatch)
    runtime = rig.runtime
    runtime._started = True
    rig.supervisor.state = "running"
    assert runtime.reserve_appimage_removal()
    before = rig.supervisor.send.call_count
    rig.hotkey.fsm.press(10.0)
    assert runtime.orchestrator.phase is DictationPhase.IDLE
    assert rig.supervisor.send.call_count == before
    for source in ("text", "command", "mouse", "capture"):
        assert not runtime._reserve_input(source)
        runtime._release_input(source)
    assert not runtime.reserve_appimage_removal()
    callback = Mock()
    assert not runtime.start_test("device", callback)
    assert not runtime.start_level_monitor("device", callback)
    runtime.restart_worker()
    runtime.reload_model()
    rig.supervisor.start.assert_not_called()
    runtime.release_appimage_removal()
    assert runtime.appimage_remove_busy_reason() == ""
    assert runtime.reserve_appimage_removal()
    runtime.release_appimage_removal()
    runtime.shutdown()


def test_partial_failure_does_not_claim_rollback_or_quit(
    profile: Profile, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = real_runtime(monkeypatch)
    bridge = profile.bridge(rig.runtime)
    token = consent(bridge)
    original = userinstall._unregister

    def partially_unregister(**kwargs: Any) -> Any:
        original(**kwargs)
        raise PermissionError("failure after real menu removal")

    monkeypatch.setattr(userinstall, "_unregister", partially_unregister)
    quit_spy = Mock()
    bridge.quitRequested.connect(quit_spy)
    preserved = profile.preserved()
    bridge.confirmAction(token)
    finish(bridge)
    assert bridge.state == "error"
    assert "не полностью" in bridge.errorText
    assert "ничего не изменилось" not in bridge.errorText
    assert "всё сохранено" not in bridge.errorText
    assert not profile.menu.exists()
    assert (profile.app / KEY).exists()
    assert profile.preserved() == preserved
    assert rig.runtime.appimage_remove_busy_reason() == ""
    quit_spy.assert_not_called()
    rig.runtime.shutdown()


def test_foreign_autostart_preserved_on_unregister(profile: Profile) -> None:
    auto = profile.home / ".config/autostart/astra-voice.desktop"
    auto.parent.mkdir()
    auto.write_bytes(b"[Desktop Entry]\nExec=/custom/app\nX-AstraVoice-Managed=false\n")
    before = auto.read_bytes()
    bridge = profile.bridge()
    token = consent(bridge, "unregister")
    assert "Чужие записи сохранятся" in bridge.confirmationMessage
    bridge.confirmAction(token)
    finish(bridge)
    assert bridge.state == "ready"
    assert auto.read_bytes() == before
    assert (profile.app / KEY).exists()
    assert not profile.menu.exists()
    bridge.requestAction("unregister")
    finish(bridge)
    assert not bridge.confirmationId
    assert "уже убрана" in bridge.resultText


@pytest.mark.parametrize("key", [OTHER, None])
def test_foreign_held_lock_protects_known_copy_or_refuses_unknown(
    profile: Profile, key: str | None
) -> None:
    profile.copy(OTHER)
    foreign = paths.FALLBACK_TMP_DIR / f"{paths.APP_NAME}-{os.getuid()}"
    foreign.mkdir(parents=True, mode=0o700)
    if key is not None:
        (foreign / userinstall.RUNNING_KEY_NAME).write_text(key + "\n")
    lock = QLockFile(str(foreign / "lock"))
    lock.setStaleLockTime(0)
    assert lock.tryLock(0)
    try:
        bridge = profile.bridge()
        token = consent(bridge)
        before = profile.preserved()
        bridge.confirmAction(token)
        finish(bridge)
        assert (profile.app / OTHER).exists()
        if key is None:
            assert bridge.state == "error"
            assert "работающую копию" in bridge.errorText
            assert (profile.app / KEY).exists() and profile.menu.exists()
        else:
            assert bridge.state == "exiting"
            assert (profile.app / OTHER / userinstall.REMOVE_ON_EXIT).exists()
            assert "Другие работающие копии" in bridge.resultText
            assert not (profile.app / KEY).exists()
        assert profile.preserved() == before
    finally:
        lock.unlock()


def test_install_lock_timeout_requires_new_consent(profile: Profile) -> None:
    bridge = profile.bridge()
    token = consent(bridge)
    with (profile.app / ".install.lock").open("r+") as holder:
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        before = tree_snapshot(profile.root)
        bridge.confirmAction(token)
        finish(bridge)
        assert bridge.state == "error" and "занята" in bridge.errorText
        assert tree_snapshot(profile.root) == before
        fcntl.flock(holder, fcntl.LOCK_UN)
    bridge.confirmAction(token)
    assert not bridge.worker_threads


def delayed_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[threading.Event, threading.Event]:
    output_ready, release = threading.Event(), threading.Event()
    original = management._run

    def run(*args: Any) -> None:
        try:
            original(*args)
            output_ready.set()
            assert release.wait(10), "test did not release worker"
        finally:
            output_ready.set()

    monkeypatch.setattr(management, "_run", run)
    return output_ready, release


def test_queued_success_is_not_thread_completion(
    profile: Profile, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge = profile.bridge()
    token = consent(bridge)
    ready, release = delayed_worker(monkeypatch)
    quits = Mock()
    bridge.quitRequested.connect(quits)
    try:
        bridge.confirmAction(token)
        assert ready.wait(4)
        assert bridge.worker_threads[0].is_alive()
        for _ in range(4):
            get_qapplication().processEvents()
            time.sleep(0.05)
        assert bridge.state == "removing"
        quits.assert_not_called()
        _, threads = composition._copy_shutdown_resources(None, None, None, bridge)
        assert threads == list(bridge.worker_threads)
        assert not composition._thread_stopped(threads[0])
    finally:
        release.set()
    finish(bridge)
    quits.assert_called_once()


def test_shutdown_with_worker_alive_preserves_active_copy(
    profile: Profile, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(paths, "install_kind", lambda: paths.InstallKind.APPIMAGE_INSTALLED)
    (profile.own / userinstall.RUNNING_KEY_NAME).write_text(KEY + "\n")
    bridge = profile.bridge(key=KEY)
    token = consent(bridge)
    ready, release = delayed_worker(monkeypatch)
    quits = Mock()
    bridge.quitRequested.connect(quits)
    try:
        bridge.confirmAction(token)
        assert ready.wait(4)
        assert (profile.app / KEY / userinstall.REMOVE_ON_EXIT).exists()
        _, threads = composition._copy_shutdown_resources(None, None, None, bridge)
        assert not bridge.close()
        composition._finish_running_copy(KEY, [], threads, stopped=True)
        assert (profile.app / KEY).exists()
        assert (profile.app / KEY / userinstall.SHUTDOWN_INCOMPLETE).exists()
        quits.assert_not_called()
    finally:
        release.set()
        for thread in bridge.worker_threads:
            thread.join(timeout=4)
    assert bridge.close()
    get_qapplication().processEvents()
    quits.assert_not_called()


def test_shutdown_consumes_unpolled_success_without_releasing_reservation(
    profile: Profile, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = real_runtime(monkeypatch)
    bridge = profile.bridge(rig.runtime)
    token = consent(bridge)
    bridge.confirmAction(token)
    for thread in bridge.worker_threads:
        thread.join(timeout=4)
        assert not thread.is_alive()
    assert bridge.close()
    assert rig.runtime.appimage_remove_busy_reason()
    assert not rig.runtime.reserve_appimage_removal()
    rig.runtime.shutdown()


@pytest.mark.parametrize("action", ["unregister", "remove"])
def test_unregister_one_shot_icon_read_failure_reports_remaining_registration(
    profile: Profile, monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    source_icon = profile.app / KEY / "usr/share/icons/hicolor/scalable/apps/astravoice.svg"
    source_icon.parent.mkdir(parents=True)
    source_icon.write_bytes(b"owned icon exact bytes")
    installed_icon = profile.home / ".local/share/icons/hicolor/scalable/apps/astravoice.svg"
    installed_icon.parent.mkdir(parents=True)
    installed_icon.write_bytes(source_icon.read_bytes())
    bridge = profile.bridge()
    token = consent(bridge, action)
    original_unregister = userinstall._unregister
    original_read = Path.read_bytes
    armed = False
    failures = 0

    def arm_then_unregister(**kwargs: Any) -> Any:
        nonlocal armed
        armed = True
        return original_unregister(**kwargs)

    def read(path: Path) -> bytes:
        nonlocal failures
        if armed and path == source_icon and failures == 0:
            failures += 1
            raise OSError("one transient icon read error")
        return original_read(path)

    monkeypatch.setattr(userinstall, "_unregister", arm_then_unregister)
    monkeypatch.setattr(Path, "read_bytes", read)
    bridge.confirmAction(token)
    finish(bridge)
    assert failures == 1
    assert not profile.menu.exists()
    assert installed_icon.exists()
    assert userinstall.management_snapshot().can_unregister is (action == "unregister")
    assert (profile.app / KEY).exists() is (action == "unregister")
    assert bridge.state == "error"
    assert "не полностью" in bridge.errorText
    assert bridge.resultText != "Регистрация AppImage убрана. Копии программы и данные сохранены."


@pytest.mark.parametrize("close_raises", [False, True])
@pytest.mark.parametrize("runtime_raises", [False, True])
def test_actual_finalizer_keeps_session_lock_until_worker_finished(
    profile: Profile, monkeypatch: pytest.MonkeyPatch, close_raises: bool, runtime_raises: bool
) -> None:
    """Execute actual main.finally, without application startup or host services."""
    tree = ast.parse(inspect.getsource(composition.main))
    function = tree.body[0]
    assert isinstance(function, ast.FunctionDef)
    final_try = function.body[-1]
    assert isinstance(final_try, ast.Try) and final_try.finalbody
    finalizer = compile(
        ast.Module(body=final_try.finalbody, type_ignores=[]), "<actual-finalizer>", "exec"
    )
    release = threading.Event()
    runtime_stopped = threading.Event()
    finalizer_done = threading.Event()
    events: list[str] = []
    errors: list[BaseException] = []
    close_calls = 0

    def work() -> None:
        assert release.wait(4), "finalizer test failed to release management worker"
        events.append("worker-finished")

    worker = threading.Thread(target=work, daemon=False)
    worker.start()

    def close() -> bool:
        nonlocal close_calls
        close_calls += 1
        events.append("close")
        if close_raises:
            raise RuntimeError("simulated controller close failure")
        return not worker.is_alive()

    def stop_runtime() -> None:
        events.append("runtime-stop")
        runtime_stopped.set()
        if runtime_raises:
            raise RuntimeError("simulated resource shutdown failure")

    def finish(_key: str) -> None:
        assert not worker.is_alive()
        events.append("finish")

    def preserve(_key: str) -> None:
        assert not worker.is_alive()
        events.append("preserve")
        raise userinstall.LockTimeoutError("simulated unavailable durable marker")

    def cleanup(_server: object, lock: QLockFile) -> None:
        assert not worker.is_alive(), "session lock released while management worker alive"
        events.append("unlock")
        lock.unlock()

    monkeypatch.setattr(userinstall, "finish_running_copy", finish)
    monkeypatch.setattr(userinstall, "preserve_incomplete_shutdown", preserve)
    monkeypatch.setattr(composition, "_children_stopped", lambda: True)
    runtime = SimpleNamespace(
        supervisor=SimpleNamespace(_retired=[], process=None),
        _switch_candidate=None,
        stats=None,
        shutdown=stop_runtime,
    )
    namespace = dict(vars(composition))
    namespace.update(
        runtime=runtime,
        appimage_management=SimpleNamespace(worker_threads=(worker,), close=close),
        downloads=None,
        update_checker=None,
        focuser=Mock(),
        onboarding=None,
        timer=Mock(),
        theme_bridge=None,
        close_watcher=Mock(),
        server=None,
        lock=profile.lock,
        _installed_code_key=lambda: KEY,
        shutdown_notify_dispatch=Mock(),
        shutdown_bus_threads=Mock(),
        _cleanup=cleanup,
    )

    def finalize() -> None:
        try:
            exec(finalizer, namespace)
        except BaseException as error:
            errors.append(error)
        finally:
            finalizer_done.set()

    finalizer_thread = threading.Thread(target=finalize, daemon=False)
    contender = QLockFile(str(profile.own / "lock"))
    contender.setStaleLockTime(0)
    finalizer_thread.start()
    try:
        assert runtime_stopped.wait(2)
        assert not contender.tryLock(0), "lock must remain held after runtime stop"
        assert not finalizer_done.wait(0.1), "finalizer must wait for file operation"
        assert worker.is_alive()
        release.set()
        finalizer_thread.join(timeout=3)
        assert not finalizer_thread.is_alive()
        assert errors == []
        assert (
            events.index("runtime-stop") < events.index("worker-finished") < events.index("unlock")
        )
        assert ("preserve" in events) is (close_raises or runtime_raises)
        assert ("finish" in events) is not (close_raises or runtime_raises)
        assert close_calls == (1 if close_raises else 2)
        assert contender.tryLock(0), "lock must release after file operation completion"
    finally:
        release.set()
        worker.join(timeout=4)
        finalizer_thread.join(timeout=4)
        contender.unlock()
