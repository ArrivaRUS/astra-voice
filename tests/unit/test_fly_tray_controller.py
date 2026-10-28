"""Offline lifecycle/serialization tests for the Fly tray coordinator."""

from __future__ import annotations

import fcntl
import json
import os
import pwd
import queue
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal

import pytest
from PyQt5.QtCore import QCoreApplication, QEventLoop, Qt, QTimer

from astra_voice.platform.fly_tray import FlyTrayPriority, Result, TrayPriorityStatus
from astra_voice.platform.fly_tray_loop import FlyTrayLoopGuard
from astra_voice.platform.session import SessionKind
from astra_voice.ui.fly_tray_controller import FlyTrayController
from helpers.fly_user import fly_user as fly_user
from helpers.qt_app import get_qapplication

pytestmark = pytest.mark.unit


class Backend:
    def __init__(self, path: Path) -> None:
        self.state_path = path / "fly-tray.json"
        self.value = TrayPriorityStatus("absent", "absent", "absent")
        self.calls: list[str] = []
        self.threads: list[int] = []
        self.fail = False

    @contextmanager
    def hold_lock(self, *, create: bool = True) -> Iterator[bool]:
        self.threads.append(threading.get_ident())
        yield True

    def status(self) -> TrayPriorityStatus:
        self.threads.append(threading.get_ident())
        return self.value

    @staticmethod
    def restart_command() -> list[str]:
        return ["/usr/bin/fly-wmfunc", "FLYWM_RESTART"]

    def apply(self) -> Result:
        self.threads.append(threading.get_ident())
        self.calls.append("show")
        if self.fail:
            return Result(False, False, "io", "private error must never escape")
        self.value = TrayPriorityStatus("present_ours", "already", "present", True)
        return Result(True, True, "ok", "ok")

    def revert(self) -> Result:
        self.threads.append(threading.get_ident())
        self.calls.append("revert")
        self.value = TrayPriorityStatus("absent", "absent", "absent")
        return Result(True, True, "ok", "ok")


class Guard:
    def __init__(self) -> None:
        self.count = 0
        self.blocked = False
        self.in_progress = False

    def restart_started(self) -> None:
        self.in_progress = True

    def restart_confirmed(self) -> None:
        self.in_progress = False

    def reset(self) -> None:
        self.count = 0
        self.blocked = False
        self.in_progress = False

    def allow(self, disappeared: bool) -> bool:
        if disappeared and not self.blocked:
            self.count += 1
            self.blocked = self.count > 3
        elif not disappeared and not self.blocked:
            self.count = 0
        return not self.blocked and not self.in_progress


class Host:
    session_kind = SessionKind.FLY

    def __init__(self) -> None:
        self.busy = False
        self.held = False
        self.releases = 0
        self.refreshes = 0
        self.thread = threading.get_ident()

    def released_once(self) -> bool:
        return self.releases == 1

    def reserve_fly_tray_restart(self) -> bool:
        assert threading.get_ident() == self.thread
        if self.busy:
            return False
        assert not self.held
        self.held = True
        return True

    def release_fly_tray_restart(self) -> None:
        assert threading.get_ident() == self.thread
        assert self.held
        self.held = False
        self.releases += 1

    def refresh_fly_tray_icon(self) -> None:
        assert threading.get_ident() == self.thread
        self.refreshes += 1


def pump(until: Callable[[], bool]) -> None:
    end = time.monotonic() + 3
    while not until() and time.monotonic() < end:
        QCoreApplication.processEvents()
        threading.Event().wait(0.001)
    assert until()


@pytest.fixture
def setup(tmp_path: Path) -> Iterator[Any]:
    get_qapplication()
    controllers: list[FlyTrayController] = []

    def make(**kwargs: Any) -> tuple[FlyTrayController, Host, Backend, list[str], list[str]]:
        host = kwargs.pop("host", Host())
        backend = kwargs.pop("backend", Backend(tmp_path))
        notices: list[str] = []
        runs: list[str] = []
        owner = iter([10, 11] * 20)
        ctl = FlyTrayController(
            host,
            kwargs.pop("enabled", True),
            backend=backend,
            notify=notices.append,
            runner=kwargs.pop("runner", runs.append),
            owner=kwargs.pop("owner", lambda: next(owner)),
            preflight=kwargs.pop("preflight", lambda: backend.restart_command()),
            loop_guard=kwargs.pop("loop_guard", Guard()),
            poll_interval_ms=1,
            **kwargs,
        )
        controllers.append(ctl)
        return ctl, host, backend, notices, runs

    yield make
    for ctl in controllers:
        ctl.shutdown()
        for thread in ctl.worker_threads:
            thread.join(3)
            assert not thread.is_alive()


def test_start_once_and_worker_only_io(setup: Any) -> None:
    ctl, host, backend, notices, runs = setup()
    ctl.start()
    ctl.start()
    pump(lambda: bool(notices))
    assert backend.calls == ["show"]
    assert runs == ["FLYWM_RESTART"]
    assert notices == ["show"]
    assert host.refreshes == host.releases == 1
    assert all(ident != threading.get_ident() for ident in backend.threads)


def test_busy_wait_and_last_pending(setup: Any) -> None:
    ctl, host, backend, notices, runs = setup()
    host.busy = True
    ctl.start()
    pump(lambda: ctl.available)
    ctl.set_enabled(False)
    ctl.set_enabled(True)
    assert not backend.calls
    host.busy = False
    pump(lambda: bool(notices))
    assert backend.calls == ["show"]
    assert len(runs) == 1


@pytest.mark.parametrize("automatic", [True, False])
def test_reserve_busy_warning_deadline_rate_and_success_reset(
    setup: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    automatic: bool,
) -> None:
    now = [0.0]
    ctl, host, backend, notices, runs = setup(automatic=automatic, clock=lambda: now[0])
    # Exercise polling deterministically: no worker, event loop or real wait.
    launches: list[Any] = []
    monkeypatch.setattr(ctl, "_launch", launches.append)
    calls: list[int] = []

    def blockers() -> tuple[str, ...]:
        assert threading.get_ident() == host.thread
        calls.append(1)
        return ("phase:processing", "preview_send")

    monkeypatch.setattr(host, "reservation_blockers", blockers, raising=False)
    host.busy = True
    ctl.start()
    ctl._timer.stop()
    if not automatic:
        now[0] = 100.0
        ctl._poll()
        assert not caplog.records and not calls
        ctl.set_enabled(True)
    start = now[0]
    ctl._poll()
    now[0] = start + 29.999
    ctl._poll()
    assert not caplog.records and not calls
    now[0] = start + 30.0
    ctl._poll()
    expected = "fly_tray_controller stage=reserve code=busy blockers=phase:processing,preview_send"
    assert [record.getMessage() for record in caplog.records] == [expected]
    assert caplog.records[0].levelname == "WARNING"
    now[0] += 240.0
    ctl._poll()
    assert len(caplog.records) == len(calls) == 1
    now[0] += 60.0
    ctl._poll()
    assert [record.getMessage() for record in caplog.records] == [expected, expected]
    assert len(calls) == 2
    assert not backend.calls and not notices and not runs and not host.held

    host.busy = False
    ctl._poll()
    assert host.held and len(launches) == 2
    ctl._release()
    host.busy = True
    ctl.set_enabled(False)
    ctl._poll()
    now[0] += 29.999
    ctl._poll()
    assert len(caplog.records) == 2
    now[0] = start + 360.0
    ctl._poll()
    assert [record.getMessage() for record in caplog.records] == [expected] * 3


@pytest.mark.parametrize("available", [False, True])
def test_reserve_busy_warning_missing_or_empty_diagnostics(
    setup: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    available: bool,
) -> None:
    now = [0.0]
    ctl, host, _, _, _ = setup(clock=lambda: now[0])
    monkeypatch.setattr(ctl, "_launch", lambda request: None)
    if available:
        monkeypatch.setattr(host, "reservation_blockers", lambda: (), raising=False)
    host.busy = True
    ctl.start()
    ctl._timer.stop()
    ctl._poll()
    now[0] = 30.0
    ctl._poll()
    assert [record.getMessage() for record in caplog.records] == [
        "fly_tray_controller stage=reserve code=busy blockers="
        + ("none" if available else "unknown")
    ]


def test_suppressed_auto_still_reads_and_explicit_works(setup: Any) -> None:
    ctl, host, backend, notices, runs = setup(automatic=False)
    ctl.start()
    pump(lambda: ctl.available)
    assert backend.threads and not backend.calls and not runs
    ctl.set_enabled(True)
    pump(lambda: bool(notices))
    assert backend.calls == ["show"]


def test_kde_does_not_touch_backend(setup: Any) -> None:
    host = Host()
    host.session_kind = SessionKind.KDE
    ctl, _, backend, notices, runs = setup(host=host)
    ctl.start()
    ctl.set_enabled(True)
    QCoreApplication.processEvents()
    assert not ctl.visible and not ctl.worker_threads
    assert not backend.threads and not backend.calls and not notices and not runs


@pytest.mark.parametrize(
    ("enabled", "kind"),
    [
        (True, "present_foreign"),
        (True, "present_ours"),
        (False, "present_foreign"),
        (False, "absent"),
    ],
)
def test_noop(setup: Any, enabled: bool, kind: Any) -> None:
    ctl, host, backend, notices, runs = setup(enabled=enabled)
    backend.value = TrayPriorityStatus(kind, "already", "present")
    ctl.start()
    pump(lambda: ctl.available and not any(t.is_alive() for t in ctl.worker_threads))
    assert not backend.calls and not runs and not notices


def test_failure_fuse_and_releases(setup: Any) -> None:
    ctl, host, backend, notices, runs = setup()
    backend.fail = True
    ctl.start()
    pump(lambda: bool(notices))
    ctl.set_enabled(True)
    QCoreApplication.processEvents()
    assert notices == ["fail"] and backend.calls == ["show"] and not runs
    assert host.releases == 1 and not host.held


def test_process_keeps_gui_alive_shutdown_no_late_callback(setup: Any) -> None:
    entered, release = threading.Event(), threading.Event()

    def run(command: str) -> None:
        entered.set()
        assert release.wait(3)

    ctl, host, backend, notices, _ = setup(runner=run)
    ticks: list[int] = []
    timer = QTimer()
    timer.timeout.connect(lambda: ticks.append(1))
    timer.start(1)
    try:
        ctl.start()
        pump(entered.is_set)
        pump(lambda: len(ticks) > 4)
        ctl.shutdown()
        assert not host.held
        release.set()
        for thread in ctl.worker_threads:
            thread.join(3)
        QCoreApplication.processEvents()
        assert not notices and not host.refreshes and host.releases == 1
    finally:
        release.set()
        timer.stop()


def test_timeout_zero_then_old_owner_does_not_count_as_restart(setup: Any) -> None:
    now = [0.0]
    owners = iter([10, 0, 10, 10, 10])

    def wait(seconds: float) -> bool:
        now[0] += seconds
        return False

    ctl, host, backend, notices, _ = setup(
        owner=lambda: next(owners, 10),
        clock=lambda: now[0],
        wait=wait,
        watch_timeout=0.2,
    )
    ctl.start()
    pump(lambda: bool(notices))
    assert notices == ["unknown"]
    assert backend.calls == ["show"]
    assert host.releases == 1 and host.refreshes == 0 and not host.held
    ctl.set_enabled(False)
    QCoreApplication.processEvents()
    assert backend.calls == ["show"]


@pytest.mark.parametrize("stage", ["preflight", "process", "owner", "argv"])
def test_errors_fixed_notice_release_no_retry(setup: Any, stage: str) -> None:
    def fail(*args: Any) -> Any:
        raise RuntimeError("PRIVATE-PATH-TOKEN")

    kwargs: dict[str, Any] = {}
    if stage == "preflight":
        kwargs["preflight"] = fail
    elif stage == "process":
        kwargs["runner"] = fail
    elif stage == "owner":
        kwargs["owner"] = fail
    else:
        kwargs["preflight"] = lambda: ["/tmp/evil", "FLYWM_RESTART"]
    ctl, host, backend, notices, runs = setup(**kwargs)
    ctl.start()
    pump(lambda: bool(notices))
    assert notices == ["fail"] and host.releases == 1 and not host.held
    assert backend.calls == (["show"] if stage == "process" else [])
    ctl.start()
    ctl.set_enabled(False)
    QCoreApplication.processEvents()
    assert notices == ["fail"]


def test_single_flight_coalesces_and_enforces_restart_interval(setup: Any) -> None:
    entered, release = threading.Event(), threading.Event()
    now = [100.0]
    runs: list[str] = []

    def run(command: str) -> None:
        runs.append(command)
        if len(runs) == 1:
            entered.set()
            assert release.wait(3)

    ctl, host, backend, notices, _ = setup(runner=run, clock=lambda: now[0])
    ctl.start()
    try:
        pump(entered.is_set)
        ctl.set_enabled(False)
        ctl.set_enabled(True)
        ctl.set_enabled(False)
        assert backend.calls == ["show"]
        release.set()
        pump(lambda: len(notices) == 1)
        assert not host.held
        now[0] = 109.0
        ctl.set_enabled(False)
        QCoreApplication.processEvents()
        assert backend.calls == ["show"]
        now[0] = 110.0
        pump(lambda: len(notices) == 2)
        assert backend.calls == ["show", "revert"]
        assert notices == ["show", "revert"] and host.releases == 2
    finally:
        release.set()


def test_four_disappearances_block_until_explicit_toggle(setup: Any) -> None:
    guard = Guard()
    for number in range(1, 5):
        ctl, host, backend, notices, runs = setup(loop_guard=guard)
        backend.value = TrayPriorityStatus("absent", "absent", "absent", True)
        ctl.start()
        pump(host.released_once)
        assert guard.count == number
        assert backend.calls == (["show"] if number <= 3 else [])
        assert len(runs) == int(number <= 3)
        if number <= 3:
            ctl.shutdown()
    assert not notices and guard.blocked
    ctl.set_enabled(True)
    pump(lambda: bool(notices))
    assert guard.count == 0 and not guard.blocked and backend.calls == ["show"]


def test_suppressed_auto_does_not_reset_guard(setup: Any) -> None:
    guard = Guard()
    guard.count, guard.blocked = 4, True
    ctl, _, backend, _, _ = setup(loop_guard=guard, automatic=False)
    backend.value = TrayPriorityStatus("present_ours", "already", "present", True)
    ctl.start()
    pump(lambda: ctl.available)
    assert guard.count == 4 and guard.blocked
    ctl.set_enabled(True)
    pump(lambda: guard.count == 0)


def test_present_token_resets_automatic_disappearance_counter(setup: Any) -> None:
    guard = Guard()
    guard.count, guard.blocked = 3, False
    ctl, host, backend, notices, runs = setup(loop_guard=guard)
    backend.value = TrayPriorityStatus("present_ours", "already", "present", True)
    ctl.start()
    pump(lambda: host.releases == 1)
    assert guard.count == 0 and not guard.blocked
    assert not backend.calls and not notices and not runs


def test_deleted_qobject_worker_has_no_late_qt_touch(setup: Any) -> None:
    from PyQt5 import sip

    entered, release = threading.Event(), threading.Event()

    def run(command: str) -> None:
        entered.set()
        assert release.wait(3)

    ctl, host, _, notices, _ = setup(runner=run)
    ctl.start()
    try:
        pump(entered.is_set)
        threads = ctl.worker_threads
        ctl.shutdown()
        sip.delete(ctl)
        release.set()
        for thread in threads:
            thread.join(3)
            assert not thread.is_alive()
        QCoreApplication.processEvents()
        assert notices == [] and host.releases == 1
    finally:
        release.set()


def test_real_backend_applied_is_provenance_even_when_token_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import pwd
    from types import SimpleNamespace

    from astra_voice.platform.fly_tray import FlyTrayPriority

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(pwd, "getpwuid", lambda uid: SimpleNamespace(pw_dir=str(tmp_path)))
    theme = tmp_path / ".fly/theme/current.themerc"
    theme.parent.mkdir(parents=True, mode=0o700)
    theme.parent.parent.chmod(0o700)
    theme.write_bytes(b"TrayPriorityList=one:\n")
    backend = FlyTrayPriority()
    assert not backend.status().applied
    assert backend.apply().ok
    assert backend.status().applied and backend.status().kind == "present_ours"
    state = backend.state_path.read_bytes()
    theme.write_bytes(b"TrayPriorityList=one:\n")
    status = backend.status()
    assert status.kind == "absent" and status.applied
    assert backend.state_path.read_bytes() == state
    assert TrayPriorityStatus("absent", "absent", "absent").applied is False


def test_changed_signal_reentrant_toggle_is_serialized(setup: Any) -> None:
    ctl, host, backend, notices, runs = setup()
    fired = [False]

    def changed() -> None:
        if backend.calls and not fired[0]:
            fired[0] = True
            ctl.set_enabled(True)

    ctl.changed.connect(changed)
    ctl.start()
    pump(lambda: bool(notices))
    assert fired[0] and not host.held and backend.calls == ["show"]


def test_worker_exception_logs_only_fixed_code(
    setup: Any, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    def fail() -> list[str]:
        raise RuntimeError("PRIVATE-EXCEPTION-DETAIL")

    ctl, _, _, notices, _ = setup(preflight=fail)
    with caplog.at_level(logging.INFO):
        ctl.start()
        pump(lambda: bool(notices))
    assert "PRIVATE-EXCEPTION-DETAIL" not in caplog.text
    assert "stage=worker code=failed" in caplog.text


@pytest.mark.parametrize(
    ("outcome", "body"),
    [
        ("show", "Настройка сохранена, панель Fly перезапущена."),
        ("revert", "Настройка сохранена, панель Fly перезапущена."),
        ("fail", "Не удалось настроить значок на панели Fly. Подробности в журнале."),
        (
            "unknown",
            "Не удалось убедиться, что панель Fly вернулась. "
            "Если значков нет — выйдите из сеанса и войдите снова.",
        ),
    ],
)
def test_notification_uses_fixed_text_without_retry(
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
    body: str,
) -> None:
    from astra_voice.ui import notify

    calls: list[Any] = []
    monkeypatch.setattr(notify, "notify", lambda *args, **kwargs: calls.append((args, kwargs)))
    notify.notify_fly_tray(outcome)
    assert calls == [(("Панель Fly", body), {"retry": False})]


def test_blocked_present_then_missing_remains_blocked(setup: Any) -> None:
    guard = Guard()
    guard.count, guard.blocked = 4, True
    for kind in ("present_ours", "absent"):
        ctl, host, backend, notices, runs = setup(loop_guard=guard)
        backend.value = TrayPriorityStatus(kind, "already", "present", True)
        ctl.start()
        pump(host.released_once)
        assert guard.count == 4 and guard.blocked
        assert not backend.calls and not notices and not runs
        ctl.shutdown()


def test_ready_toggle_burst_waits_for_timer_and_coalesces(setup: Any) -> None:
    ctl, host, backend, notices, runs = setup(automatic=False)
    ctl.start()
    pump(lambda: ctl.available)
    ctl.set_enabled(True)
    ctl.set_enabled(False)
    assert not host.held and not backend.calls
    pump(host.released_once)
    assert not backend.calls and not notices and not runs


def test_process_timeout_is_unknown_and_rate_limited(setup: Any) -> None:
    from astra_voice.platform.fly_wmfunc import FlyWmError

    def run(command: str) -> None:
        raise FlyWmError("timeout")

    ctl, host, backend, notices, _ = setup(runner=run)
    ctl.start()
    pump(lambda: bool(notices))
    assert notices == ["unknown"] and host.releases == 1
    ctl.set_enabled(False)
    QCoreApplication.processEvents()
    assert backend.calls == ["show"]


def test_preflight_failure_never_resets_persistent_guard(setup: Any) -> None:
    guard = Guard()
    guard.count, guard.blocked = 4, True

    def fail() -> list[str]:
        raise RuntimeError("invalid binary")

    ctl, host, backend, notices, _ = setup(
        automatic=False,
        loop_guard=guard,
        preflight=fail,
    )
    ctl.start()
    pump(lambda: ctl.available)
    ctl.set_enabled(True)
    pump(lambda: bool(notices))
    assert guard.count == 4 and guard.blocked
    assert not backend.calls and host.releases == 1


@pytest.mark.parametrize("delay_ms", [700, 5000])
def test_blocked_process_preserves_gui_and_simulated_broker_heartbeats(
    setup: Any,
    delay_ms: int,
) -> None:
    entered, release = threading.Event(), threading.Event()
    elapsed: list[float] = []
    gui_ticks: list[int] = []
    broker_ticks: list[int] = []

    def run(command: str) -> None:
        begin = time.monotonic()
        entered.set()
        assert release.wait(7)
        elapsed.append(time.monotonic() - begin)

    ctl, host, _, notices, _ = setup(runner=run)
    loop = QEventLoop()
    gui_timer, broker_timer, release_timer, watchdog = QTimer(), QTimer(), QTimer(), QTimer()
    gui_timer.timeout.connect(lambda: gui_ticks.append(1))
    broker_timer.timeout.connect(lambda: broker_ticks.append(1))
    release_timer.setSingleShot(True)
    release_timer.setTimerType(Qt.PreciseTimer)
    release_timer.timeout.connect(release.set)
    watchdog.setSingleShot(True)
    watchdog.timeout.connect(loop.quit)

    def changed() -> None:
        if notices:
            loop.quit()

    ctl.changed.connect(changed)
    ctl.start()
    try:
        pump(entered.is_set)
        gui_timer.start(10)
        broker_timer.start(17)
        release_timer.start(delay_ms)
        watchdog.start(6000)
        loop.exec_()
        assert elapsed and elapsed[0] >= delay_ms / 1000 - 0.01
        assert len(gui_ticks) >= 20 and len(broker_ticks) >= 20
        assert notices == ["show"] and host.releases == 1
    finally:
        release.set()
        gui_timer.stop()
        broker_timer.stop()
        release_timer.stop()
        watchdog.stop()


@pytest.mark.parametrize("code", ["timeout", "exit_nonzero"])
def test_uncertain_process_keeps_reservation_until_owner_returns(setup: Any, code: str) -> None:
    from astra_voice.platform.fly_wmfunc import FlyWmError

    watching, returned = threading.Event(), threading.Event()

    def run(command: str) -> None:
        raise FlyWmError(code)

    def wait(seconds: float) -> bool:
        watching.set()
        assert returned.wait(3)
        return False

    ctl, host, backend, notices, _ = setup(
        runner=run,
        owner=lambda: 11 if returned.is_set() else 10,
        wait=wait,
    )
    ctl.start()
    try:
        pump(lambda: watching.is_set() or bool(notices))
        assert watching.is_set() and host.held and host.releases == 0 and not notices
        returned.set()
        pump(lambda: bool(notices))
        assert notices == ["unknown"] and host.refreshes == host.releases == 1
        ctl.set_enabled(False)
        QCoreApplication.processEvents()
        assert backend.calls == ["show"]
    finally:
        returned.set()


def test_hidden_reason_survives_failed_automatic_attempt(setup: Any) -> None:
    ctl, host, backend, notices, _ = setup()
    backend.value = TrayPriorityStatus("unavailable", "hidden", "Значок скрыт настройками Fly")
    ctl.start()
    pump(lambda: bool(notices))
    assert not ctl.available
    assert ctl.reason == "Значок скрыт настройками Fly"
    assert notices == ["fail"] and host.releases == 1


def test_transient_owner_error_keeps_watch_until_new_owner(setup: Any) -> None:
    from astra_voice.platform.fly_wmfunc import FlyWmError

    calls = [0]
    now = [0.0]
    held_during_wait: list[bool] = []

    def owner() -> int:
        calls[0] += 1
        if calls[0] == 2:
            raise FlyWmError("display_unavailable")
        return 10 if calls[0] == 1 else 11

    def wait(seconds: float) -> bool:
        held_during_wait.append(host.held)
        now[0] += seconds
        return False

    ctl, host, _, notices, _ = setup(owner=owner, clock=lambda: now[0], wait=wait)
    ctl.start()
    pump(lambda: bool(notices))
    assert notices == ["show"] and held_during_wait == [True]
    assert host.releases == host.refreshes == 1


def test_persistent_owner_error_holds_reservation_until_watch_deadline(setup: Any) -> None:
    from astra_voice.platform.fly_wmfunc import FlyWmError

    calls = [0]
    now = [0.0]
    held_during_wait: list[bool] = []

    def owner() -> int:
        calls[0] += 1
        if calls[0] > 1:
            raise FlyWmError("display_unavailable")
        return 10

    def wait(seconds: float) -> bool:
        held_during_wait.append(host.held)
        now[0] += seconds
        return False

    ctl, host, _, notices, _ = setup(
        owner=owner,
        clock=lambda: now[0],
        wait=wait,
        watch_timeout=0.3,
    )
    ctl.start()
    pump(lambda: bool(notices))
    assert notices == ["unknown"] and now[0] >= 0.3
    assert len(held_during_wait) >= 3 and all(held_during_wait)
    assert host.releases == 1 and host.refreshes == 0


def test_watch_probe_receives_remaining_deadline_budget(setup: Any) -> None:
    now = [0.0]
    budgets: list[float] = []

    def owner_budgeted(timeout: float) -> int:
        budgets.append(timeout)
        now[0] += timeout
        return 0

    def wait(seconds: float) -> bool:
        now[0] += seconds
        return False

    ctl, host, _, notices, _ = setup(
        owner=lambda: 10,
        owner_budgeted=owner_budgeted,
        clock=lambda: now[0],
        wait=wait,
        watch_timeout=1.1,
    )
    ctl.start()
    pump(lambda: bool(notices))
    assert budgets == pytest.approx([0.75, 0.25])
    assert now[0] == pytest.approx(1.1)
    assert notices == ["unknown"] and host.releases == 1 and host.refreshes == 0


def test_default_owner_uses_budgeted_helper_without_live_probe(
    setup: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from astra_voice.platform import fly_wmfunc
    from astra_voice.ui import fly_tray_controller

    now = [0.0]
    budgets: list[float] = []

    def helper_owner(*, timeout: float = 0.75) -> int:
        budgets.append(timeout)
        if len(budgets) == 1:
            return 10
        now[0] += timeout
        return 0

    def wait(seconds: float) -> bool:
        now[0] += seconds
        return False

    monkeypatch.setattr(fly_wmfunc, "tray_owner", helper_owner)
    ctl, host, _, notices, _ = setup(
        owner=fly_tray_controller._owner,
        clock=lambda: now[0],
        wait=wait,
        watch_timeout=1.1,
    )
    ctl.start()
    pump(lambda: bool(notices))
    assert budgets == pytest.approx([0.75, 0.75, 0.25])
    assert now[0] == pytest.approx(1.1)
    assert notices == ["unknown"] and host.releases == 1


@pytest.fixture
def real_guard(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FlyTrayLoopGuard:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(pwd, "getpwuid", lambda uid: SimpleNamespace(pw_dir=str(tmp_path)))
    return FlyTrayLoopGuard(tmp_path / "private/fly-tray.json")


def test_unknown_persistently_blocks_next_start_with_missing_token(
    setup: Any,
    real_guard: FlyTrayLoopGuard,
) -> None:
    ctl, host, backend, notices, runs = setup(
        loop_guard=real_guard,
        owner=lambda: 10,
        watch_timeout=0,
    )
    ctl.start()
    pump(lambda: bool(notices))
    assert notices == ["unknown"] and runs == ["FLYWM_RESTART"]
    assert json.loads(real_guard.path.read_bytes())["restart_in_progress"] is True
    ctl.shutdown()
    backend.value = TrayPriorityStatus("absent", "absent", "absent", True)
    again, new_host, _, new_notices, new_runs = setup(
        backend=backend,
        loop_guard=FlyTrayLoopGuard(real_guard.path),
    )
    again.start()
    pump(new_host.released_once)
    assert backend.calls == ["show"] and not new_runs and not new_notices
    assert again.available


def test_explicit_disable_after_unknown_reverts_under_idle_and_interval(
    setup: Any,
    real_guard: FlyTrayLoopGuard,
) -> None:
    clock = [0.0]
    ctl, host, backend, notices, runs = setup(
        loop_guard=real_guard,
        owner=lambda: 10,
        watch_timeout=0,
        clock=lambda: clock[0],
    )
    ctl.start()
    pump(lambda: bool(notices))
    assert notices == ["unknown"] and ctl.available
    host.busy = True
    ctl.set_enabled(False)
    clock[0] = 9.0
    ctl._poll()
    assert backend.calls == ["show"]
    clock[0] = 10.0
    ctl._poll()
    assert backend.calls == ["show"]
    host.busy = False
    pump(lambda: len(notices) == 2)
    assert backend.calls == ["show", "revert"] and len(runs) == 2
    assert host.releases == 2 and not host.held


@pytest.mark.parametrize("outcome", ["confirmed", "cancelled", "exception"])
def test_restart_marker_is_durable_before_command_and_only_confirmation_clears(
    setup: Any,
    real_guard: FlyTrayLoopGuard,
    outcome: str,
) -> None:
    observed: list[bool] = []

    def run(command: str) -> None:
        observed.append(json.loads(real_guard.path.read_bytes())["restart_in_progress"])
        if outcome == "cancelled":
            ctl._cancel.set()
        elif outcome == "exception":
            raise RuntimeError("command state unknown")

    ctl, host, backend, notices, runs = setup(loop_guard=real_guard, runner=run)
    ctl.start()
    pump(host.released_once)
    assert observed == [True]
    assert json.loads(real_guard.path.read_bytes())["restart_in_progress"] is (
        outcome != "confirmed"
    )


def test_disable_lost_token_clears_provenance_and_preserves_later_foreign_token(
    setup: Any,
    real_guard: FlyTrayLoopGuard,
    tmp_path: Path,
) -> None:
    theme = tmp_path / ".fly/theme/current.themerc"
    theme.parent.mkdir(parents=True, mode=0o700)
    theme.parent.parent.chmod(0o700)
    original = b"TrayPriorityList=one:\n"
    theme.write_bytes(original)
    backend = FlyTrayPriority()
    assert backend.apply().ok
    theme.write_bytes(original)
    assert backend.status().applied and backend.status().kind == "absent"
    ctl, host, _, notices, runs = setup(backend=backend, enabled=False)
    ctl.start()
    pump(host.released_once)
    assert not backend.status().applied
    assert not backend.state_path.exists() and theme.read_bytes() == original
    assert not runs and not notices
    ctl.shutdown()
    foreign = b"TrayPriorityList=one:astra-voice:\n"
    theme.write_bytes(foreign)
    assert backend.status().kind == "present_foreign"
    again, new_host, _, new_notices, new_runs = setup(backend=backend, enabled=False)
    again.start()
    pump(new_host.released_once)
    assert theme.read_bytes() == foreign and not new_runs and not new_notices


@pytest.mark.parametrize("explicit", [False, True])
def test_panel_deferred_preserves_real_files_and_guard(
    setup: Any,
    real_guard: FlyTrayLoopGuard,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    explicit: bool,
) -> None:
    from unittest.mock import Mock

    from astra_voice.ui import fly_tray_controller

    theme = tmp_path / ".fly/theme/current.themerc"
    theme.parent.mkdir(parents=True, mode=0o700)
    theme.parent.parent.chmod(0o700)
    theme.write_bytes(b"TrayPriorityList=one:\n")
    backend = FlyTrayPriority()
    assert backend.apply().ok
    theme.write_bytes(b"TrayPriorityList=one:\n")
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    factory = Mock(side_effect=AssertionError("guard must not be instantiated"))
    monkeypatch.setattr(fly_tray_controller, "FlyTrayLoopGuard", factory)
    now = [0.0]
    ctl, host, _, notices, runs = setup(
        backend=backend,
        automatic=not explicit,
        loop_guard=None,
        owner=lambda: 0,
        clock=lambda: now[0],
        watch_timeout=0,
    )
    if explicit:
        ctl.set_enabled(True)
    ctl.start()
    pump(host.released_once)
    assert ctl._pending is not None and not ctl._fused
    assert not runs and not notices and not host.held
    assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before
    factory.assert_not_called()


@pytest.mark.parametrize("explicit", [False, True])
def test_panel_deferred_rate_then_owner_appears(setup: Any, explicit: bool) -> None:
    now = [0.0]
    probes: list[float] = []

    def owner() -> int:
        probes.append(now[0])
        return 0 if now[0] < 7 else 10 + len(probes)

    ctl, host, backend, notices, runs = setup(
        owner=owner,
        clock=lambda: now[0],
        automatic=not explicit,
        watch_timeout=0.01,
    )
    if explicit:
        ctl.set_enabled(True)
    ctl.start()
    pump(host.released_once)
    assert probes == [0.0] and not host.held and not backend.calls
    for value in (0.1, 1.0, 4.999):
        now[0] = value
        ctl._poll()
    assert probes == [0.0]
    now[0] = 5.0
    ctl._poll()
    pump(lambda: host.releases == 2)
    assert probes == [0.0, 5.0]
    now[0] = 7.0
    ctl._poll()
    assert not backend.calls
    now[0] = 10.0
    pump(lambda: bool(notices))
    assert backend.calls == ["show"] and runs == ["FLYWM_RESTART"]
    assert notices == ["show"]


@pytest.mark.parametrize("busy", [False, True])
def test_panel_deferred_deadline_drops_once_and_allows_new_request(
    setup: Any,
    caplog: pytest.LogCaptureFixture,
    busy: bool,
) -> None:
    now = [0.0]
    ctl, host, backend, notices, runs = setup(
        owner=lambda: 0,
        clock=lambda: now[0],
        watch_timeout=0,
    )
    ctl.start()
    pump(host.released_once)
    host.busy = busy
    now[0] = 300.0
    ctl._poll()
    ctl._poll()
    assert [r.getMessage() for r in caplog.records] == [
        "fly_tray_controller stage=panel code=not_ready"
    ]
    assert ctl._pending is None and not ctl._fused and not host.held
    assert not backend.calls and not runs and not notices
    assert ctl._reserve_busy_since is ctl._reserve_busy_warning_at is None
    host.busy = False
    owners = iter([10, 11])
    ctl._owner = lambda: next(owners)
    ctl._watch_timeout = 1
    ctl.set_enabled(True)
    pump(lambda: bool(notices))
    assert notices == ["show"]


def test_panel_deferred_old_worker_preserves_new_pending(setup: Any) -> None:
    entered, proceed = threading.Event(), threading.Event()
    now = [0.0]

    def owner() -> int:
        entered.set()
        assert proceed.wait(3)
        return 0

    ctl, host, backend, notices, runs = setup(owner=owner, watch_timeout=0, clock=lambda: now[0])
    ctl.start()
    try:
        pump(entered.is_set)
        ctl.set_enabled(False)
        proceed.set()
        pump(host.released_once)
        assert ctl._pending is not None and ctl._pending.enabled is False
        now[0] = 4.999
        ctl._poll()
        assert host.releases == 1 and not host.held
        now[0] = 5.0
        pump(lambda: host.releases >= 2)
        assert not backend.calls and not runs and not notices
        assert ctl._pending is None
    finally:
        proceed.set()


def test_new_pending_starts_new_busy_warning_episode(
    setup: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    now = [0.0]
    ctl, host, _, _, _ = setup(clock=lambda: now[0])
    monkeypatch.setattr(ctl, "_launch", lambda request: None)
    host.busy = True
    ctl.start()
    ctl._timer.stop()
    ctl._poll()
    now[0] = 29
    ctl.set_enabled(False)
    ctl._poll()
    now[0] = 30
    ctl._poll()
    assert not caplog.records
    now[0] = 59
    ctl._poll()
    assert len(caplog.records) == 1
    ctl.shutdown()
    assert ctl._reserve_busy_since is ctl._reserve_busy_warning_at is None


@pytest.mark.parametrize(
    "diagnostic",
    [
        lambda: ("/home/SECRET",),
        lambda: ("phase:SECRET",),
        lambda: ("input_owner:/home/SECRET",),
        lambda: ["phase:processing"],
        lambda: "SECRET",
        lambda: (object(),),
        lambda: None,
        lambda: (_ for _ in ()).throw(RuntimeError("SECRET")),
        lambda: (type("Private", (str,), {})("SECRET"),),
    ],
)
def test_busy_warning_sanitizes_host_boundary(
    setup: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    diagnostic: Any,
) -> None:
    now = [0.0]
    ctl, host, _, _, _ = setup(clock=lambda: now[0])
    monkeypatch.setattr(ctl, "_launch", lambda request: None)
    monkeypatch.setattr(host, "reservation_blockers", diagnostic, raising=False)
    host.busy = True
    ctl.start()
    ctl._timer.stop()
    ctl._poll()
    now[0] = 30
    ctl._poll()
    assert [r.getMessage() for r in caplog.records] == [
        "fly_tray_controller stage=reserve code=busy blockers=unknown"
    ]


def test_new_explicit_preserves_panel_retry_and_total_deadline(
    setup: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    now = [0.0]
    probes: list[float] = []

    def owner() -> int:
        probes.append(now[0])
        return 0

    ctl, host, _, _, _ = setup(owner=owner, clock=lambda: now[0], watch_timeout=0)
    ctl.start()
    pump(host.released_once)
    now[0] = 4.999
    ctl.set_enabled(True)
    ctl._poll()
    assert probes == [0.0]
    now[0] = 5.0
    pump(lambda: host.releases == 2)
    now[0] = 299.999
    ctl.set_enabled(True)
    pump(lambda: host.releases == 3)
    assert ctl._pending is not None and not caplog.records
    now[0] = 300.0
    ctl._poll()
    assert ctl._pending is None and not ctl._fused
    assert [r.getMessage() for r in caplog.records] == [
        "fly_tray_controller stage=panel code=not_ready"
    ]


@pytest.mark.parametrize(
    "outcome",
    [
        "confirmed",
        "timeout",
        "cancelled",
        "runner_exception",
        "watchdog_exception",
        "prepare_exception",
        "apply_failure",
    ],
)
def test_worker_flock_covers_prepare_restart_watchdog_and_releases(
    setup: Any,
    real_guard: FlyTrayLoopGuard,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
) -> None:
    from astra_voice.platform.fly_tray import Action

    theme = tmp_path / ".fly/theme/current.themerc"
    theme.parent.mkdir(parents=True, mode=0o700)
    theme.parent.parent.chmod(0o700)
    theme.write_bytes(b"TrayPriorityList=one:\n")
    backend = FlyTrayPriority()
    lock_path = backend.backups_dir / "fly-theme.lock"
    observations: list[tuple[str, bool]] = []
    competitors: list[str] = []
    now = [0.0]
    owner_calls = 0

    def probe(stage: str) -> None:
        try:
            fd = os.open(lock_path, os.O_RDWR)
        except FileNotFoundError:
            observations.append((stage, False))
            return
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                observations.append((stage, True))
            else:
                observations.append((stage, False))
        finally:
            os.close(fd)

    prepare = backend.prepare

    def checked_prepare(action: Action) -> str:
        probe("prepare")
        if outcome == "prepare_exception":
            raise RuntimeError("prepare interrupted")
        return prepare(action)

    monkeypatch.setattr(backend, "prepare", checked_prepare)
    if outcome == "apply_failure":
        monkeypatch.setattr(backend, "confirm", lambda op: Result(False, False, "io", "io"))

    def run(command: str) -> None:
        probe("runner")
        competitors.append(FlyTrayPriority().apply().code)
        if outcome == "runner_exception":
            raise RuntimeError("restart interrupted")

    def owner() -> int:
        nonlocal owner_calls
        owner_calls += 1
        if owner_calls == 1:
            return 10
        probe("watchdog")
        if outcome == "cancelled":
            ctl._cancel.set()
        if outcome == "watchdog_exception":
            raise RuntimeError("watchdog interrupted")
        return 10 if outcome in {"timeout", "cancelled"} else 11

    def wait(delay: float) -> bool:
        now[0] += delay
        return False

    ctl, host, _, notices, _ = setup(
        backend=backend,
        loop_guard=real_guard,
        runner=run,
        owner=owner,
        clock=lambda: now[0],
        wait=wait,
        watch_timeout=0.2,
    )
    ctl.start()
    pump(host.released_once)
    assert observations and all(held for _, held in observations), observations
    stages = [stage for stage, _ in observations]
    assert stages[0] == "prepare"
    if outcome not in {"prepare_exception", "apply_failure"}:
        assert "runner" in stages and competitors == ["busy"]
        if outcome != "runner_exception":
            assert "watchdog" in stages
    expected_notice = {
        "confirmed": ["show"],
        "timeout": ["unknown"],
        "cancelled": [],
    }.get(outcome, ["fail"])
    assert notices == expected_notice
    fd = os.open(lock_path, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(fd)
    assert FlyTrayPriority().apply().ok


def test_status_rechecked_under_operation_lock_before_noop_and_guard(
    setup: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = Backend(tmp_path)
    backend.value = TrayPriorityStatus("absent", "absent", "absent", True)
    guard = Guard()
    held = False
    checks: list[bool] = []

    @contextmanager
    def lock(*, create: bool = True) -> Iterator[bool]:
        nonlocal held
        if not create:
            yield False
            return
        # Another operation completed between initial status and lock acquisition.
        backend.value = TrayPriorityStatus("present_foreign", "foreign", "foreign")
        held = True
        try:
            yield True
        finally:
            held = False

    def allow(disappeared: bool) -> bool:
        assert held
        checks.append(disappeared)
        return True

    monkeypatch.setattr(backend, "hold_lock", lock)
    monkeypatch.setattr(guard, "allow", allow)
    ctl, host, _, notices, runs = setup(backend=backend, loop_guard=guard)
    ctl.start()
    pump(host.released_once)
    assert checks == [False]
    assert not backend.calls and not runs and not notices
    assert not held


@pytest.mark.parametrize("explicit", [True, False], ids=["explicit", "automatic"])
@pytest.mark.parametrize("second_kind", ["absent", "present_foreign", "unavailable"])
def test_cancel_during_locked_status_preserves_guard_and_releases_lock(
    real_guard: FlyTrayLoopGuard,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    explicit: bool,
    second_kind: Literal["absent", "present_foreign", "unavailable"],
) -> None:
    from astra_voice.ui import fly_tray_controller as module

    theme = tmp_path / ".fly/theme/current.themerc"
    theme.parent.mkdir(parents=True, mode=0o700)
    theme.parent.parent.chmod(0o700)
    original = b"TrayPriorityList=one:\n"
    theme.write_bytes(original)
    backend = FlyTrayPriority()
    competitor = FlyTrayPriority()
    assert real_guard.allow(True)
    real_guard.restart_started()
    guard_before = real_guard.path.read_bytes()
    cancel = threading.Event()
    statuses: list[TrayPriorityStatus] = []
    calls: list[str] = []
    noop_calls: list[str] = []
    status = backend.status
    noop = module._noop

    def cancelling_status() -> TrayPriorityStatus:
        snapshot = status()
        if statuses:
            assert competitor.apply().code == "busy"
            cancel.set()
            snapshot = TrayPriorityStatus(second_kind, second_kind, "absent")
        statuses.append(snapshot)
        return snapshot

    def checked_noop(request: Any, snapshot: TrayPriorityStatus) -> bool:
        noop_calls.append(snapshot.kind)
        return noop(request, snapshot)

    def reset() -> None:
        calls.append("reset")

    def allow(disappeared: bool) -> bool:
        calls.append("allow")
        return True

    def mutation() -> Result:
        calls.append("mutation")
        return Result(True, True, "ok", "ok")

    monkeypatch.setattr(backend, "status", cancelling_status)
    monkeypatch.setattr(module, "_noop", checked_noop)
    monkeypatch.setattr(real_guard, "reset", reset)
    monkeypatch.setattr(real_guard, "allow", allow)
    monkeypatch.setattr(backend, "apply", mutation)
    monkeypatch.setattr(backend, "revert", mutation)
    reply = module._execute(
        module._Work(
            backend=backend,
            request=module._Request(enabled=True, explicit=explicit),
            cancel=cancel,
            mailbox=queue.SimpleQueue(),
            preflight=backend.restart_command,
            runner=calls.append,
            owner=lambda: 10,
            owner_budgeted=None,
            clock=time.monotonic,
            wait=cancel.wait,
            guard=real_guard,
            watch_timeout=0,
        )
    )
    # A different backend must acquire the same real flock after cancellation.
    with competitor.hold_lock(create=False) as held:
        assert held
    assert len(statuses) == 2 and cancel.is_set()
    assert calls == []
    assert noop_calls == ["absent"]
    assert reply == module._Reply(status=statuses[-1])
    assert theme.read_bytes() == original
    assert real_guard.path.read_bytes() == guard_before
    assert not backend.state_path.exists()


@pytest.mark.parametrize("explicit", [True, False], ids=["explicit", "automatic"])
def test_first_lock_busy_fails_without_theme_guard_or_restart_changes(
    setup: Any,
    real_guard: FlyTrayLoopGuard,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    explicit: bool,
) -> None:
    theme = tmp_path / ".fly/theme/current.themerc"
    theme.parent.mkdir(parents=True, mode=0o700)
    theme.parent.parent.chmod(0o700)
    original = b"TrayPriorityList=one:\n"
    theme.write_bytes(original)
    backend = FlyTrayPriority()
    competitor = FlyTrayPriority()
    real_guard.restart_started()
    guard_before = real_guard.path.read_bytes()
    attempts: list[bool] = []
    hold_lock = backend.hold_lock

    @contextmanager
    def checked_lock(*, create: bool = True) -> Iterator[bool]:
        attempts.append(create)
        with hold_lock(create=create) as held:
            yield held

    monkeypatch.setattr(backend, "hold_lock", checked_lock)
    ctl, host, _, notices, runs = setup(
        backend=backend, loop_guard=real_guard, automatic=not explicit
    )
    with competitor.hold_lock():
        if explicit:
            ctl.set_enabled(True)
        ctl.start()
        pump(host.released_once)
        assert notices == ["fail"]
        assert attempts == [False]
        assert not runs and not host.held
        assert theme.read_bytes() == original
        assert real_guard.path.read_bytes() == guard_before
        assert not backend.state_path.exists()
    with backend.hold_lock(create=False) as held:
        assert held


def test_initial_noop_cannot_clear_concurrent_restart_marker(
    setup: Any,
    real_guard: FlyTrayLoopGuard,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    theme = tmp_path / ".fly/theme/current.themerc"
    theme.parent.mkdir(parents=True, mode=0o700)
    theme.parent.parent.chmod(0o700)
    theme.write_bytes(b"TrayPriorityList=one:\n")
    idle_backend = FlyTrayPriority()
    observed = threading.Event()
    resume_noop = threading.Event()
    restarting = threading.Event()
    resume_restart = threading.Event()
    status = idle_backend.status
    status_calls = 0

    def paused_status() -> TrayPriorityStatus:
        nonlocal status_calls
        status_calls += 1
        snapshot = status()
        if status_calls == 1:  # Controller initialization only reads status.
            return snapshot
        observed.set()
        assert resume_noop.wait(3)
        return snapshot

    def paused_runner(command: str) -> None:
        restarting.set()
        assert resume_restart.wait(3)

    monkeypatch.setattr(idle_backend, "status", paused_status)
    idle, idle_host, _, idle_notices, _ = setup(
        backend=idle_backend,
        enabled=False,
        automatic=False,
        loop_guard=real_guard,
    )
    active, active_host, _, active_notices, _ = setup(
        backend=FlyTrayPriority(),
        loop_guard=real_guard,
        runner=paused_runner,
        owner=lambda: 10,
        watch_timeout=0,
    )
    try:
        idle.set_enabled(False)  # Explicit no-op must not reset a concurrent marker.
        idle.start()
        pump(observed.is_set)
        assert not idle_backend.backups_dir.exists()
        active.start()
        pump(restarting.is_set)
        assert json.loads(real_guard.path.read_bytes())["restart_in_progress"] is True
        resume_noop.set()
        pump(idle_host.released_once)
        assert not idle_notices
        assert json.loads(real_guard.path.read_bytes())["restart_in_progress"] is True
        resume_restart.set()
        pump(active_host.released_once)
        assert active_notices == ["unknown"]
        assert json.loads(real_guard.path.read_bytes())["restart_in_progress"] is True
    finally:
        resume_noop.set()
        resume_restart.set()
