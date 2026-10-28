"""GUI reservation and worker-only Fly tray changes, process and watchdog.

Workers own no Qt objects or callbacks into the controller. A GUI timer drains a
plain Python mailbox only after its producer has terminated and been joined.
Stopping/deleting Qt therefore cannot race a late Qt invocation from a worker.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from PyQt5.QtCore import QObject, QTimer, pyqtSignal

from astra_voice.platform.fly_tray import FlyTrayPriority, TrayPriorityStatus
from astra_voice.platform.fly_tray_loop import FlyTrayLoopGuard
from astra_voice.platform.fly_wmfunc import FlyWmError
from astra_voice.platform.session import SessionKind
from astra_voice.ui.notify import notify_fly_tray

_LOG = logging.getLogger(__name__)
_REASON_FAILED = "Не удалось настроить значок на панели Fly. Подробности в журнале."
_REASON_LOOP = "Автоматическая настройка панели приостановлена. Переключите настройку ещё раз."


class FlyTrayHost(Protocol):
    @property
    def session_kind(self) -> SessionKind: ...

    def reserve_fly_tray_restart(self) -> bool: ...
    def reservation_blockers(self) -> tuple[str, ...]: ...
    def release_fly_tray_restart(self) -> None: ...
    def refresh_fly_tray_icon(self) -> None: ...


class LoopGuard(Protocol):
    def reset(self) -> None: ...
    def allow(self, disappeared: bool) -> bool: ...
    def restart_started(self) -> None: ...
    def restart_confirmed(self) -> None: ...


def _preflight() -> list[str]:
    from astra_voice.platform import fly_wmfunc

    argv = fly_wmfunc.check_binary("FLYWM_RESTART")
    fly_wmfunc.check_session()
    return argv


def _run(command: str) -> None:
    from astra_voice.platform import fly_wmfunc

    fly_wmfunc.run(command)


def _owner() -> int:
    from astra_voice.platform import fly_wmfunc

    return fly_wmfunc.tray_owner()


def _owner_budgeted(timeout: float) -> int:
    from astra_voice.platform import fly_wmfunc

    return fly_wmfunc.tray_owner(timeout=timeout)


@dataclass(frozen=True)
class _Request:
    enabled: bool
    explicit: bool


@dataclass(frozen=True)
class _Reply:
    backend: FlyTrayPriority | None = None
    status: TrayPriorityStatus | None = None
    notice: str = ""
    failed: bool = False
    blocked: bool = False
    restarted_at: float | None = None
    returned: bool = False


@dataclass(frozen=True)
class _Work:
    backend: FlyTrayPriority | None
    request: _Request | None
    cancel: threading.Event
    mailbox: queue.SimpleQueue[_Reply]
    preflight: Callable[[], list[str]]
    runner: Callable[[str], None]
    owner: Callable[[], int]
    owner_budgeted: Callable[[float], int] | None
    clock: Callable[[], float]
    wait: Callable[[float], bool]
    guard: LoopGuard | None
    watch_timeout: float


def _log(stage: str, code: str) -> None:
    # Neither exceptions, file paths nor externally provided codes enter the log.
    _LOG.info("fly_tray_controller stage=%s code=%s", stage, code)


def _execute(work: _Work) -> _Reply:
    backend = work.backend if work.backend is not None else FlyTrayPriority()
    status = backend.status()
    request = work.request
    if request is None or work.cancel.is_set():
        return _Reply(backend=backend, status=status)
    if status.kind == "unavailable":
        _log("status", "unavailable")
        return _Reply(status=status, notice="fail", failed=True)
    noop = (request.enabled and status.kind in {"present_ours", "present_foreign"}) or (
        not request.enabled
        and status.kind != "present_ours"
        and not (status.kind == "absent" and status.applied)
    )
    if work.cancel.is_set():
        return _Reply(status=status)
    # For a mutation even guard persistence must follow binary/session preflight.
    if not noop and work.preflight() != backend.restart_command():
        _log("preflight", "argv")
        return _Reply(status=status, notice="fail", failed=True)
    guard = work.guard if work.guard is not None else FlyTrayLoopGuard(backend.state_path)
    if request.explicit:
        guard.reset()
    elif not guard.allow(status.kind == "absent" and status.applied and not noop):
        _log("loop", "blocked")
        return _Reply(status=status, blocked=True)
    if noop or work.cancel.is_set():
        return _Reply(status=status)
    old_owner = work.owner()
    if work.cancel.is_set():
        return _Reply(status=status)
    result = backend.apply() if request.enabled else backend.revert()
    if not result.ok:
        _log("apply", "failed")
        return _Reply(status=status, notice="fail", failed=True)
    if not result.changed or work.cancel.is_set():
        return _Reply(status=backend.status())
    # Persist before sending the command: an interrupted worker/process must not
    # automatically repeat a restart whose effect cannot be established.
    guard.restart_started()
    if work.cancel.is_set():
        return _Reply(status=backend.status())
    restarted_at = work.clock()
    uncertain = False
    try:
        work.runner("FLYWM_RESTART")
    except FlyWmError as error:
        if error.code not in {"timeout", "exit_nonzero"}:
            raise
        _log("run", "uncertain")
        uncertain = True
    deadline = work.clock() + work.watch_timeout
    owner_error_logged = False
    while not work.cancel.is_set():
        remaining = deadline - work.clock()
        if remaining <= 0:
            break
        try:
            current = (
                work.owner_budgeted(min(0.75, remaining))
                if work.owner_budgeted is not None
                else work.owner()
            )
        except FlyWmError:
            # The WM may be between X11 owners; retain the reservation for the
            # bounded watchdog, just as when the selection temporarily has no owner.
            current = 0
            if not owner_error_logged:
                _log("watch", "owner_unavailable")
                owner_error_logged = True
        if work.clock() >= deadline:
            break
        if current and current != old_owner:
            if not uncertain:
                guard.restart_confirmed()
            return _Reply(
                status=backend.status(),
                notice="unknown" if uncertain else ("show" if request.enabled else "revert"),
                blocked=uncertain,
                restarted_at=restarted_at,
                returned=True,
            )
        remaining = deadline - work.clock()
        if remaining <= 0:
            break
        if work.wait(min(0.1, remaining)):
            return _Reply(restarted_at=restarted_at)
    if work.cancel.is_set():
        return _Reply(restarted_at=restarted_at)
    _log("watch", "timeout")
    return _Reply(
        status=backend.status(),
        notice="unknown",
        blocked=True,
        restarted_at=restarted_at,
    )


def _worker(work: _Work) -> None:
    try:
        reply = _execute(work)
    except Exception:  # No exception text: backend/process errors may carry private paths.
        _log("worker", "failed")
        reply = _Reply(notice="fail", failed=True)
    if not work.cancel.is_set():
        work.mailbox.put(reply)


class FlyTrayController(QObject):
    """One automatic attempt; explicit requests coalesce while reserved/busy.

    The owner calls shutdown before Qt teardown, then joins worker_threads after
    the event loop exits. Remaining workers touch Python/OS resources only.
    """

    changed = pyqtSignal()

    def __init__(
        self,
        host: FlyTrayHost,
        enabled: bool,
        *,
        automatic: bool = True,
        backend: FlyTrayPriority | None = None,
        parent: QObject | None = None,
        preflight: Callable[[], list[str]] = _preflight,
        runner: Callable[[str], None] = _run,
        owner: Callable[[], int] = _owner,
        owner_budgeted: Callable[[float], int] | None = None,
        clock: Callable[[], float] = time.monotonic,
        wait: Callable[[float], bool] | None = None,
        notify: Callable[[str], None] = notify_fly_tray,
        loop_guard: LoopGuard | None = None,
        watch_timeout: float = 30.0,
        poll_interval_ms: int = 25,
    ) -> None:
        super().__init__(parent)
        self._host = host
        self._visible = host.session_kind is SessionKind.FLY
        self._available = False
        self._reason = ""
        self._enabled = enabled
        self._automatic = automatic
        self._backend = backend
        self._preflight, self._runner, self._owner = preflight, runner, owner
        self._owner_budgeted = owner_budgeted
        if owner_budgeted is None and owner is _owner:
            self._owner_budgeted = _owner_budgeted
        self._clock, self._notify, self._guard = clock, notify, loop_guard
        self._watch_timeout = watch_timeout
        self._cancel = threading.Event()
        self._wait = wait if wait is not None else self._cancel.wait
        self._mailbox: queue.SimpleQueue[_Reply] = queue.SimpleQueue()
        self._thread: threading.Thread | None = None
        self._threads: list[threading.Thread] = []
        self._pending: _Request | None = None
        self._started = False
        self._stopped = False
        self._fused = False
        self._polling = False
        self._reserved = False
        self._status: TrayPriorityStatus | None = None
        self._last_restart: float | None = None
        self._reserve_busy_since: float | None = None
        self._reserve_busy_warning_at: float | None = None
        self._timer = QTimer(self)
        self._timer.setInterval(poll_interval_ms)
        self._timer.timeout.connect(self._poll)
        # A retained Event method has no QObject receiver and performs no Qt work.
        self.destroyed.connect(self._cancel.set)

    @property
    def visible(self) -> bool:
        return self._visible

    @property
    def available(self) -> bool:
        return self._available

    @property
    def reason(self) -> str:
        return self._reason

    @property
    def worker_threads(self) -> tuple[threading.Thread, ...]:
        return tuple(self._threads)

    def start(self) -> None:
        if self._started or self._stopped or not self.visible:
            return
        self._started = True
        if self._pending is None and self._automatic:
            self._pending = _Request(self._enabled, False)
        self._launch(None)
        self._timer.start()

    def set_enabled(self, enabled: bool, *, explicit: bool = True) -> None:
        self._enabled = enabled
        if self._stopped or self._fused or not self.visible:
            return
        if not explicit:
            # Configuration synchronization must not manufacture another startup action.
            if self._pending is not None:
                self._pending = _Request(enabled, self._pending.explicit)
            return
        self._pending = _Request(enabled, True)
        # The timer coalesces every toggle burst before acquiring the host reservation.

    def _launch(self, request: _Request | None) -> None:
        work = _Work(
            self._backend,
            request,
            self._cancel,
            self._mailbox,
            self._preflight,
            self._runner,
            self._owner,
            self._owner_budgeted,
            self._clock,
            self._wait,
            self._guard,
            self._watch_timeout,
        )
        thread = threading.Thread(target=_worker, args=(work,), name="fly-tray-worker")
        self._thread = thread
        self._threads.append(thread)
        thread.start()

    def _release(self) -> None:
        if self._reserved:
            self._reserved = False
            self._host.release_fly_tray_restart()

    def _poll(self) -> None:
        if self._stopped or self._polling:
            return
        self._polling = True
        try:
            self._poll_once()
        finally:
            self._polling = False

    def _poll_once(self) -> None:
        if self._thread is not None:
            if self._thread.is_alive():
                return
            self._thread.join()
            self._thread = None
            try:
                reply = self._mailbox.get_nowait()
            except queue.Empty:
                self._release()
                return
            try:
                if reply.backend is not None:
                    self._backend = reply.backend
                if reply.status is not None:
                    self._status = reply.status
                    self._available = reply.status.kind != "unavailable"
                    self._reason = reply.status.message
                if reply.failed:
                    self._fused = True
                    self._pending = None
                    self._available = False
                    if reply.status is None or reply.status.kind != "unavailable":
                        self._reason = _REASON_FAILED
                if reply.blocked:
                    self._available = True
                    self._reason = _REASON_LOOP
                if reply.restarted_at is not None:
                    self._last_restart = reply.restarted_at
                if reply.returned:
                    self._host.refresh_fly_tray_icon()
                if reply.notice:
                    self._notify(reply.notice)
                self.changed.emit()
            finally:
                self._release()
        if self._stopped or self._pending is None or self._fused:
            return
        if self._last_restart is not None and self._clock() - self._last_restart < 10.0:
            return
        if not self._host.reserve_fly_tray_restart():
            self._warn_reservation_busy()
            return
        self._reserve_busy_since = None
        self._reserve_busy_warning_at = None
        self._reserved = True
        request, self._pending = self._pending, None
        try:
            self._launch(request)
        except Exception:
            self._release()
            self._fused = True
            self._notify("fail")

    def _warn_reservation_busy(self) -> None:
        now = self._clock()
        if self._reserve_busy_since is None:
            self._reserve_busy_since = now
        if now - self._reserve_busy_since < 30.0:
            return
        if (
            self._reserve_busy_warning_at is not None
            and now - self._reserve_busy_warning_at < 300.0
        ):
            return
        diagnostics = getattr(self._host, "reservation_blockers", None)
        blockers = diagnostics() if diagnostics is not None else ("unknown",)
        _LOG.warning(
            "fly_tray_controller stage=reserve code=busy blockers=%s",
            ",".join(blockers) or "none",
        )
        self._reserve_busy_warning_at = now

    def shutdown(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        self._cancel.set()
        self._pending = None
        self._timer.stop()
        self._release()
        for thread in self._threads:
            if not thread.is_alive():
                thread.join()
