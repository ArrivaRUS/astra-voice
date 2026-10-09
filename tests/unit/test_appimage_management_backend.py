"""Author checks: real QObject, worker and filesystem, isolated fake AppImage only."""

from __future__ import annotations

import queue
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from PyQt5.QtCore import QLockFile

from astra_voice.core import paths
from astra_voice.platform import userinstall
from astra_voice.ui import appimage_management as module
from helpers.appimage_bundle import KEY, make_bundle
from helpers.qt_app import get_qapplication

pytestmark = pytest.mark.unit


class IdleRuntime:
    busy = ""
    reserved = False

    def appimage_remove_busy_reason(self) -> str:
        return self.busy

    def reserve_appimage_removal(self) -> bool:
        if self.busy or self.reserved:
            return False
        self.reserved = True
        return True

    def release_appimage_removal(self) -> None:
        self.reserved = False


def settle(controller: module.AppImageManagement) -> None:
    app = get_qapplication()
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline:
        app.processEvents()
        controller._poll()
        if not controller.worker_threads and controller.state != "checking":
            return
        time.sleep(0.005)
    raise AssertionError(f"worker did not settle: {controller.state}")


@pytest.fixture
def setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    get_qapplication()
    monkeypatch.setenv("HOME", str(tmp_path))
    for name in ("DATA", "CONFIG", "CACHE", "STATE"):
        monkeypatch.setenv(f"XDG_{name}_HOME", str(tmp_path / name.lower()))
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    monkeypatch.setattr(paths, "_runtime_dir_candidates", lambda **kwargs: (runtime,))
    monkeypatch.setattr(paths, "SYSTEM_EXECUTABLE", tmp_path / "missing-deb")
    monkeypatch.setattr(paths, "install_kind", lambda: paths.InstallKind.APPIMAGE_INSTALLED)
    monkeypatch.setattr(userinstall, "refuse_root", lambda: None)
    monkeypatch.setattr(userinstall, "_sync_filesystem", lambda path: None)
    copy = make_bundle(paths.appimage_app_dir() / KEY, icons=True)
    (copy / paths.INSTALLED_MARKER).touch()
    (paths.appimage_app_dir() / "current").symlink_to(KEY)
    (runtime / userinstall.RUNNING_KEY_NAME).write_text(KEY)
    menu, _ = userinstall._desktop_paths()
    menu.parent.mkdir(parents=True)
    menu.write_bytes(userinstall.menu_entry_bytes(copy / "AppRun", "0.2.0"))
    lock = QLockFile(str(runtime / "lock"))
    assert lock.tryLock(0)
    idle = IdleRuntime()
    controller = module.AppImageManagement(idle, own_lock_path=runtime / "lock", running_key=KEY)
    settle(controller)
    assert controller.state == "ready", controller.errorText
    yield controller, idle, copy, menu
    assert controller.close()
    lock.unlock()


def test_qobject_contract_cancel_is_read_only(setup: Any) -> None:
    controller, _, copy, menu = setup
    expected = {
        "state",
        "appPath",
        "canUnregister",
        "canRemove",
        "busyReason",
        "resultText",
        "errorText",
        "confirmationId",
        "confirmationAction",
        "confirmationMessage",
        "installKind",
    }
    meta = controller.metaObject()
    actual = {meta.property(i).name() for i in range(meta.propertyCount())}
    assert expected <= actual
    controller.requestAction("remove")
    settle(controller)
    token = controller.confirmationId
    assert token and str(copy.parent) in controller.confirmationMessage
    controller.cancelConfirmation(token)
    controller.confirmAction(token)
    assert copy.exists() and menu.exists() and controller.state == "ready"


def test_remove_keeps_own_copy_and_reservation_until_shutdown(setup: Any) -> None:
    controller, idle, copy, menu = setup
    quit_calls: list[bool] = []
    controller.quitRequested.connect(lambda: quit_calls.append(True))
    controller.requestAction("remove")
    settle(controller)
    token = controller.confirmationId
    controller.confirmAction(token)
    controller.confirmAction(token)
    settle(controller)
    assert controller.state == "exiting", controller.errorText
    assert quit_calls == [True]
    assert idle.reserved and copy.exists()
    assert (copy / userinstall.REMOVE_ON_EXIT).exists()
    assert not menu.exists()
    assert not controller.worker_threads
    assert (
        "подготовлено" in controller.resultText and "Программа удалена" not in controller.resultText
    )


def test_busy_at_confirmation_never_mutates(setup: Any) -> None:
    controller, idle, copy, menu = setup
    controller.requestAction("remove")
    settle(controller)
    idle.busy = "занято"
    controller.confirmAction(controller.confirmationId)
    assert controller.state == "busy"
    assert not idle.reserved and copy.exists() and menu.exists()


def test_changed_consent_releases_reservation_and_retry_needs_new_token(setup: Any) -> None:
    controller, idle, copy, menu = setup
    controller.requestAction("remove")
    settle(controller)
    token = controller.confirmationId
    menu.write_bytes(menu.read_bytes() + b"Comment=changed\n")
    controller.confirmAction(token)
    settle(controller)
    assert controller.state == "error" and "изменилось" in controller.errorText
    assert not idle.reserved and copy.exists() and menu.exists()
    controller.retry()
    settle(controller)
    assert controller.state == "confirming" and controller.confirmationId != token
    assert not idle.reserved


def test_unregister_does_not_reserve_runtime_or_quit(setup: Any) -> None:
    controller, idle, copy, menu = setup
    idle.busy = "диктовка"
    controller.requestAction("unregister")
    settle(controller)
    controller.confirmAction(controller.confirmationId)
    settle(controller)
    assert controller.state == "busy", controller.errorText
    assert copy.exists() and not menu.exists() and not idle.reserved
    assert "системную версию" not in controller.resultText


def test_strict_status_error_disables_actions(setup: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    controller, _, _, _ = setup

    def fail() -> Any:
        raise PermissionError()

    monkeypatch.setattr(userinstall, "management_snapshot", fail)
    controller.refresh()
    settle(controller)
    assert controller.state == "error"
    assert not controller.canRemove and not controller.canUnregister


def test_partial_failure_does_not_quit(setup: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    controller, idle, copy, menu = setup

    def fail(**kwargs: Any) -> Any:
        kwargs["validate"]()
        menu.unlink()
        raise PermissionError()

    monkeypatch.setattr(userinstall, "remove_program", fail)
    controller.requestAction("remove")
    settle(controller)
    controller.confirmAction(controller.confirmationId)
    settle(controller)
    assert controller.state == "error" and "не полностью" in controller.errorText
    assert not idle.reserved and copy.exists() and not menu.exists()


def test_absent_app_still_validates_and_prepares(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(paths, "appimage_app_dir", lambda: tmp_path / "absent")
    monkeypatch.setattr(userinstall, "validate_remove_paths", lambda *args: None)
    monkeypatch.setattr(userinstall, "refuse_root", lambda: None)
    calls: list[str] = []
    monkeypatch.setattr(userinstall, "_unregister", lambda: calls.append("mutation"))

    def prepare() -> tuple[Path, ...]:
        calls.append("prepare")
        return ()

    userinstall.remove_program(validate=lambda: calls.append("validate"), prepare=prepare)
    assert calls == ["validate", "prepare", "mutation"]


def test_thread_result_waits_for_actual_exit(setup: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    controller, _, _, _ = setup
    emitted = threading.Event()
    release = threading.Event()
    snapshot = controller._snapshot

    def worker(*args: Any) -> None:
        output: queue.Queue[module._Reply] = args[-1]
        output.put(module._Reply("refresh", snapshot))
        emitted.set()
        assert release.wait(2)

    monkeypatch.setattr(module, "_run", worker)
    controller.refresh()
    try:
        assert emitted.wait(1)
        controller._poll()
        assert controller.state == "checking" and controller.worker_threads
    finally:
        release.set()
        settle(controller)


def test_deb_outcome_bound_to_consent(
    setup: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    controller, _, copy, menu = setup
    deb = tmp_path / "deb"
    monkeypatch.setattr(paths, "SYSTEM_EXECUTABLE", deb)
    controller.requestAction("remove")
    settle(controller)
    deb.write_text("#!/bin/sh\n")
    deb.chmod(0o755)
    controller.confirmAction(controller.confirmationId)
    settle(controller)
    assert controller.state == "error" and copy.exists() and menu.exists()


def test_foreign_icon_does_not_enable_unregister(setup: Any) -> None:
    controller, _, _, menu = setup
    menu.unlink()
    _, icons = userinstall._desktop_paths()
    icon = icons / "48x48/apps/astravoice.png"
    icon.parent.mkdir(parents=True)
    icon.write_bytes(b"foreign")
    controller.refresh()
    settle(controller)
    assert not controller.canUnregister and controller.canRemove


@pytest.mark.parametrize(
    "field",
    [
        "_pending_test",
        "_pending_switch",
        "_capture_watchdog",
        "_mouse_capture_token",
        "_preview_send",
        "_input_owner",
    ],
)
def test_runtime_busy_blocks_reservation(field: str, monkeypatch: pytest.MonkeyPatch) -> None:
    from test_runtime import Rig

    from astra_voice.platform.hotkey import HotkeyState

    rig = Rig(monkeypatch)
    rig.hotkey.has_pending_press = False
    rig.hotkey.fsm.state = HotkeyState.IDLE
    assert not rig.runtime.appimage_remove_busy_reason()
    setattr(rig.runtime, field, object())
    assert rig.runtime.appimage_remove_busy_reason()
    assert not rig.runtime.reserve_appimage_removal()
    assert not rig.runtime._appimage_removal_reserved


def test_runtime_reservation_blocks_new_work(monkeypatch: pytest.MonkeyPatch) -> None:
    from test_runtime import Rig

    from astra_voice.platform.hotkey import HotkeyState

    rig = Rig(monkeypatch)
    rig.hotkey.has_pending_press = False
    rig.hotkey.fsm.state = HotkeyState.IDLE
    runtime = rig.runtime
    assert runtime.reserve_appimage_removal()
    runtime._input_cancel()
    assert runtime._input_owner == "appimage-removal"
    assert not runtime._reserve_input("text")
    assert not runtime.begin_hotkey_capture()
    assert runtime.begin_command_mouse_capture() is None
    assert not runtime.start_test("device", lambda update: None)
    assert not runtime.start_level_monitor("device", lambda update: None)
    runtime.reload_model()
    runtime.switch_model(min_ram_mb=1)
    runtime.restart_worker()
    rig.supervisor.start.assert_not_called()
    assert not runtime._switch_active()
    runtime.release_appimage_removal()
    assert not runtime.appimage_remove_busy_reason()


def test_runtime_finishing_and_unknown_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    from test_runtime import Rig

    from astra_voice.core.dictation import DictationPhase
    from astra_voice.platform.hotkey import HotkeyState

    rig = Rig(monkeypatch)
    rig.hotkey.has_pending_press = False
    rig.hotkey.fsm.state = HotkeyState.IDLE
    rig.runtime.orchestrator._phase = DictationPhase.FINISHING
    assert not rig.runtime.reserve_appimage_removal()
    rig.runtime.orchestrator._phase = DictationPhase.IDLE
    rig.hotkey.has_pending_press = None
    assert not rig.runtime.reserve_appimage_removal()


def test_close_joins_worker_and_suppresses_late_quit(setup: Any) -> None:
    controller, _, _, _ = setup
    quits: list[bool] = []
    controller.quitRequested.connect(lambda: quits.append(True))
    controller.requestAction("remove")
    settle(controller)
    controller.confirmAction(controller.confirmationId)
    assert controller.close()
    get_qapplication().processEvents()
    assert not quits and not controller.worker_threads
    assert controller.close()


@pytest.mark.parametrize(
    ("action", "portable"),
    [
        ("unregister", False),
        ("remove", False),
        ("remove", True),
    ],
)
def test_remaining_owned_icon_is_partial_even_after_source_copy_removed(
    setup: Any,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
    portable: bool,
) -> None:
    controller, idle, copy, menu = setup
    _, icons = userinstall._desktop_paths()
    icon = icons / "48x48/apps/astravoice.png"
    icon.parent.mkdir(parents=True)
    icon.write_bytes((copy / "usr/share/icons/hicolor/48x48/apps/astravoice.png").read_bytes())
    original = userinstall._owned_icons

    def skip_tolerant_scan(root: Path, *, strict: bool = False) -> Any:
        # The mutation's legacy scan can swallow an IO error; the strict scans work.
        return original(root, strict=True) if strict else iter(())

    monkeypatch.setattr(userinstall, "_owned_icons", skip_tolerant_scan)
    if portable:
        controller._kind = "appimage-portable"
        controller._running_key = None
        (controller._own_lock.parent / userinstall.RUNNING_KEY_NAME).unlink()
    quits: list[bool] = []
    controller.quitRequested.connect(lambda: quits.append(True))
    controller.requestAction(action)
    settle(controller)
    controller.confirmAction(controller.confirmationId)
    settle(controller)
    assert controller.state == "error"
    assert "не полностью" in controller.errorText
    assert not controller.resultText and not quits and not idle.reserved
    assert not menu.exists() and icon.exists()
    assert copy.exists() is not portable
    if portable:
        # No remaining bundle can identify the icon in a fresh strict scan.
        assert not userinstall.management_snapshot().can_unregister
