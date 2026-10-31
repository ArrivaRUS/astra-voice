"""Capture-owned login1 admission. No Qt, PCM, GUI, or third-party DBus package.

The systemd utilities are optional OS transports, never new Python dependencies.
A missing/unresponsive transport is unknown (closed on KDE). Fly explicitly has
no reliable lock signal, but a known inactive session or sleep still terminates.
Constructors do no I/O; the role starts the bounded polling/signal workers.
"""

from __future__ import annotations

import contextlib
import os
import shlex
import subprocess
import sys
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol

from astra_voice.platform.session import SessionKind, detect

POLL_SECONDS = 0.5
READ_TIMEOUT = 0.075  # Three calls, <=225ms; never wait for DBus in capture control.
FRESH_SECONDS = 0.8
LOGIN = "org.freedesktop.login1"
MANAGER_PATH = "/org/freedesktop/login1"
SESSION_PREFIX = MANAGER_PATH + "/session/"


@dataclass(frozen=True)
class SessionSnapshot:
    known: bool = False
    active: bool = False
    locked: bool = False
    sleeping: bool = False
    blocked_epoch: int = 0
    lock_signal_unavailable: bool = False
    reason: str | None = "unknown"
    observed_at: float = 0.0

    @property
    def admissible(self) -> bool:
        return self.reason is None


class SessionAdmission:
    """Thread-safe pure state; an update never waits for native audio or storage."""

    def __init__(
        self,
        *,
        kind: str,
        clock: Callable[[], float] = time.monotonic,
        max_age: float = FRESH_SECONDS,
    ) -> None:
        self.clock = clock
        self.max_age = max_age
        self.lock = threading.RLock()
        self.generation = 0
        self.lock_requested = False
        self.unlock_requested = False
        self.hint_seen = False
        self.value = SessionSnapshot(
            lock_signal_unavailable=kind == SessionKind.FLY,
            reason=None if kind == SessionKind.FLY else "unknown",
            observed_at=clock(),
        )
        self.events: deque[SessionSnapshot] = deque()

    def _publish(self, value: SessionSnapshot) -> None:
        # Repeated identical polls refresh age without creating block epochs.
        reason = (
            "sleep"
            if value.sleeping
            else "session"
            if value.known and not value.active
            else None
            if value.lock_signal_unavailable
            else "unknown"
            if not value.known
            else "lock"
            if value.locked
            else None
        )
        epoch = self.value.blocked_epoch + int(reason is not None and reason != self.value.reason)
        updated = replace(value, reason=reason, blocked_epoch=epoch)
        changed = replace(updated, observed_at=0) != replace(self.value, observed_at=0)
        self.value = updated
        if changed:
            self.events.append(updated)

    def update(
        self,
        *,
        known: bool,
        active: bool,
        locked: bool,
        sleeping: bool,
        generation: int | None = None,
    ) -> None:
        with self.lock:
            if generation is not None and generation != self.generation:
                return  # A pre-Lock reply cannot reopen admission or clear sleep.
            if self.lock_requested:
                if locked:
                    self.hint_seen = True
                elif known and (self.unlock_requested or self.hint_seen):
                    self.lock_requested = self.unlock_requested = self.hint_seen = False
            self._publish(
                replace(
                    self.value,
                    known=known,
                    active=active,
                    locked=locked or self.lock_requested,
                    sleeping=sleeping,
                    observed_at=self.clock(),
                )
            )

    def signal(self, name: str, value: bool | None = None) -> None:
        with self.lock:
            self.generation += 1
            if name == "Lock":
                self.lock_requested = True
                self.unlock_requested = self.hint_seen = False
                self._publish(replace(self.value, locked=True, observed_at=self.clock()))
            elif name == "Unlock":
                self.unlock_requested = True
                # A signal alone cannot grant admission. The next read confirms it.
            elif name == "PrepareForSleep":
                self._publish(
                    replace(self.value, sleeping=value is not False, observed_at=self.clock())
                )
            else:
                self._publish(replace(self.value, known=False, observed_at=self.clock()))

    def snapshot(self) -> SessionSnapshot:
        with self.lock:
            if self.clock() - self.value.observed_at > self.max_age and self.value.known:
                self.generation += 1
                self._publish(replace(self.value, known=False))
            return self.value

    def allowed(self, epoch: int) -> bool:
        snapshot = self.snapshot()
        return snapshot.admissible and snapshot.blocked_epoch == epoch

    def drain(self) -> list[SessionSnapshot]:
        with self.lock:
            self.snapshot()
            events = list(self.events)
            self.events.clear()
            return events


class SessionSource(Protocol):
    def read(self) -> dict[str, bool]: ...


class Login1Source:
    """Read the concrete current login1 session, with the same UID/local checks.

    busctl's ordinary typed output works without its newer JSON option. Only
    fixed property names are requested; all errors use a constant code. No
    session/user identifiers or command output is ever logged.
    """

    def __init__(self, *, run: Callable[..., Any] = subprocess.run, uid: int | None = None) -> None:
        self.run = run
        self.uid = os.getuid() if uid is None else uid
        self.path: str | None = None

    def _call(self, *args: str) -> list[list[str]]:
        completed = self.run(
            ["/usr/bin/busctl", "--system", "--auto-start=no", "--timeout=0.075", *args],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=READ_TIMEOUT,
            check=False,
        )
        if completed.returncode or len(completed.stdout) > 16384:
            raise ValueError("session-unavailable")
        return [shlex.split(line) for line in completed.stdout.splitlines()]

    def read(self) -> dict[str, bool]:
        path = self._call(
            "call", LOGIN, MANAGER_PATH, LOGIN + ".Manager", "GetSession", "s", "auto"
        )
        if (
            len(path) != 1
            or len(path[0]) != 2
            or path[0][0] != "o"
            or not path[0][1].startswith(SESSION_PREFIX)
        ):
            raise ValueError("session-unavailable")
        selected = path[0][1]
        # Publish the pinned path BEFORE reading properties: a Lock arriving
        # during GetAll/get-property must invalidate that in-flight admission.
        self.path = selected
        sleeping = self._call(
            "get-property", LOGIN, MANAGER_PATH, LOGIN + ".Manager", "PreparingForSleep"
        )
        # Manager sleep is already authoritative and terminal. Do not make it
        # depend on a later Session read which can time out or disappear.
        if sleeping == [["b", "true"]]:
            return dict(known=False, active=False, locked=False, sleeping=True)
        props = self._call(
            "get-property",
            LOGIN,
            selected,
            LOGIN + ".Session",
            "LockedHint",
            "Active",
            "User",
            "Remote",
            "Type",
            "Class",
            "Seat",
        )
        if (
            len(props) != 7
            or sleeping not in ([["b", "false"]], [["b", "true"]])
            or props[0] not in (["b", "false"], ["b", "true"])
            or props[1] not in (["b", "false"], ["b", "true"])
            or len(props[2]) != 3
            or props[2][0] != "(uo)"
            or props[2][1] != str(self.uid)
            or props[3] != ["b", "false"]
            or props[4] not in (["s", "x11"], ["s", "wayland"])
            or props[5] != ["s", "user"]
            or len(props[6]) != 3
            or props[6][0] != "(so)"
            or not props[6][1]
            or not props[6][2].startswith(MANAGER_PATH + "/seat/")
        ):
            raise ValueError("session-unavailable")
        self.path = selected
        return dict(
            known=True,
            active=props[1][1] == "true",
            locked=props[0][1] == "true",
            sleeping=sleeping[0][1] == "true",
        )


class MeetingSession:
    """Independent 0.5s login1 polling plus optional system-bus signal reader."""

    def __init__(
        self,
        state: SessionAdmission,
        *,
        source: SessionSource | None = None,
    ) -> None:
        self.state = state
        self.source = source or Login1Source()
        self.stop = threading.Event()
        self.process: subprocess.Popen[str] | None = None
        self.helper_lock = threading.Lock()
        self.thread: threading.Thread | None = None
        self.signals_ready = threading.Event()
        self.signals_disabled = threading.Event()

    @classmethod
    def production(cls) -> MeetingSession:
        return cls(SessionAdmission(kind=detect().value))

    def tick(self) -> None:
        generation = self.state.generation
        try:
            values = self.source.read()
        except Exception:
            self.state.update(
                known=False,
                active=False,
                locked=False,
                sleeping=self.state.snapshot().sleeping,
                generation=generation,
            )
        else:
            self.state.update(**values, generation=generation)

    def start(self) -> None:
        if self.thread is not None:
            return

        def poll() -> None:
            # A helper that starts but never reports readiness must not prevent
            # Fly from discovering sleep/inactive via the independent poll path.
            if not self.signals_ready.wait(0.1):
                self.signals_disabled.set()
                if self.process is not None:
                    self._reap_helper(self.process)
            while not self.stop.is_set():
                started = time.monotonic()
                self.tick()
                self.stop.wait(max(0, POLL_SECONDS - (time.monotonic() - started)))

        self.thread = threading.Thread(target=poll, name="meeting-session", daemon=True)
        threading.Thread(target=self._signals, name="meeting-session-signals", daemon=True).start()
        self.thread.start()

    def _signals(self) -> None:
        # dbus-monitor match resolves the well-known sender on the SYSTEM bus.
        # Missing dbus-monitor is supported: the bounded property polls remain.
        try:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-I",
                    "-B",
                    str(Path(__file__).with_name("meeting_session_helper.py")),
                    str(os.getpid()),
                    "/usr/bin/dbus-monitor",
                    "--system",
                    "type='signal',sender='org.freedesktop.login1',interface='org.freedesktop.login1.Session'",
                    "type='signal',sender='org.freedesktop.login1',interface='org.freedesktop.login1.Manager'",
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
            )
        except OSError:
            self.signals_ready.set()  # Optional helper unavailable: poll-only mode.
            return
        self.process = process
        if self.stop.is_set() or self.signals_disabled.is_set():
            self._reap_helper(process)
        assert process.stdout is not None
        pending_sleep = False
        try:
            for line in process.stdout:
                # dbus-monitor prints queued messages only after installing its
                # matches. Until then initial KDE admission remains unknown.
                self.signals_ready.set()
                if self.stop.is_set() or self.signals_disabled.is_set():
                    break
                if len(line) > 4096:
                    self.state.signal("unknown")
                    continue
                if line.startswith("signal "):
                    pending_sleep = False
                    path = getattr(self.source, "path", None)
                    if (
                        "interface=" + LOGIN + ".Manager;"
                    ) in line and "member=PrepareForSleep" in line:
                        pending_sleep = True
                    elif (path is None or ("path=" + path + ";") in line) and (
                        "interface=" + LOGIN + ".Session;"
                    ) in line:
                        if "member=Lock\n" in line:
                            self.state.signal("Lock")
                        elif path is not None and "member=Unlock\n" in line:
                            self.state.signal("Unlock")
                elif pending_sleep and line.strip() in {"boolean true", "boolean false"}:
                    self.state.signal("PrepareForSleep", line.strip() == "boolean true")
                    pending_sleep = False
        finally:
            self.signals_ready.set()  # Denied helper exits: retain property polling.
            process.stdout.close()
            self._reap_helper(process)

    def _reap_helper(self, process: subprocess.Popen[str]) -> bool:
        # close(), startup fallback and reader EOF can race. One bounded owner
        # performs TERM -> KILL -> wait; no session/audio lock is held here.
        with self.helper_lock:
            with contextlib.suppress(OSError):
                process.terminate()
            try:
                process.wait(timeout=0.1)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(OSError):
                    process.kill()
                try:
                    process.wait(timeout=0.2)
                except subprocess.TimeoutExpired:
                    # TimeoutExpired carries argv; do not let a helper thread
                    # print it. Kill was sent, but reap is not yet confirmed.
                    self.state.signal("unknown")
                    return False
            return True

    def close(self) -> bool:
        self.stop.set()
        if self.process is not None:
            return self._reap_helper(self.process)
        return True
