"""Read-only, event-driven login1 cache. Unknown state never permits admission.

Construction does not connect to a bus. Production must explicitly supply a
QtLogin1Transport with a system-bus connection; tests supply a memory transport.
"""

from __future__ import annotations

import contextlib
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from PyQt5.QtCore import QObject, Qt, QThread, QTimer, pyqtSignal, pyqtSlot
from PyQt5.QtDBus import QDBusConnection, QDBusError, QDBusMessage, QDBusObjectPath, QDBusVariant

from astra_voice.core.command_mode import SessionSnapshot
from astra_voice.platform.cowork import resolve_bus_address

LOGIN = "org.freedesktop.login1"
MANAGER_PATH = "/org/freedesktop/login1"
MANAGER = LOGIN + ".Manager"
SESSION = LOGIN + ".Session"
PROPERTIES = "org.freedesktop.DBus.Properties"
LOGIN_PATH_PREFIX = "/org/freedesktop/login1/session/"
#: Name of the private system-bus connection opened by :class:`QtLogin1Transport`.
CONNECTION_NAME = "astra-voice-login1"
# Properties that decide admission (§9): a change to any of them forces a
# re-read; other Session properties (IdleHint, …) do not churn the cache.
_CHECKED_PROPERTIES = frozenset(
    {"LockedHint", "Active", "User", "Class", "Type", "Remote", "Seat", "Display", "Desktop"}
)
ReadCallback = Callable[[Any, bool], None]
# Unknown is not sticky (Voice answer 28.09, п. 8б): after a read error/timeout or
# a Disconnected, re-read (reconnect) after 1 s, doubling up to 30 s, until the
# first good answer. A steady state issues no reads at all.
RETRY_FIRST_MS = 1000
RETRY_MAX_MS = 30_000


def monotonic_ms() -> int:
    return time.monotonic_ns() // 1_000_000


def _plain(value: Any) -> Any:
    if isinstance(value, QDBusVariant):
        return _plain(value.variant())
    if isinstance(value, QDBusObjectPath):
        return value.path()
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    return value


class Login1Transport(Protocol):
    def read(
        self, path: str, interface: str, method: str, arguments: list[Any], callback: ReadCallback
    ) -> None: ...

    def subscribe(
        self, service: str, path: str, interface: str, signal: str, callback: Callable[..., None]
    ) -> Callable[[], None]: ...

    # Optional: ``reconnect() -> None`` re-opens a dead connection (Disconnected);
    # ``close() -> None`` releases the connection on stop (review n4, idempotent).


class _Read(QObject):
    def __init__(self, parent: QObject, callback: ReadCallback, timeout: int) -> None:
        super().__init__(parent)
        self.callback = callback
        self.done = False
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.timer.timeout.connect(lambda: self.finish(None, True))
        self.timer.start(timeout)

    def finish(self, value: Any, error: bool) -> None:
        if self.done:
            return
        self.done = True
        self.timer.stop()
        self.timer.deleteLater()
        self.deleteLater()
        self.callback(value, error)

    @pyqtSlot(QDBusMessage)
    def ok(self, message: QDBusMessage) -> None:
        values = message.arguments()
        self.finish(_plain(values[0]) if len(values) == 1 else None, len(values) != 1)

    @pyqtSlot(QDBusError, QDBusMessage)
    def error(self, error: QDBusError, message: QDBusMessage) -> None:
        self.finish(None, True)


class _Signal(QObject):
    def __init__(self, parent: QObject, callback: Callable[..., None]) -> None:
        super().__init__(parent)
        self.callback = callback

    @pyqtSlot(QDBusMessage)
    def receive(self, message: QDBusMessage) -> None:
        # SessionState wraps every callback (_guarded): malformed arguments close
        # admission instead of escaping a PyQt5 slot (which would qFatal).
        self.callback(*_plain(message.arguments()))


class QtLogin1Transport(QObject):
    """Thin Qt transport on an explicitly supplied SYSTEM connection.

    No implicit systemBus/sessionBus, introspecting interface, subprocess, or poll.
    All calls disable service activation and have bounded asynchronous lifetimes.
    """

    def __init__(
        self, connection: Any, parent: QObject | None = None, *, timeout_ms: int = 1000
    ) -> None:
        super().__init__(parent)
        self.connection = connection
        self.timeout_ms = timeout_ms
        # Only the named connection opened here is ours to close; a connection
        # supplied by the caller (tests, private bus) stays the caller's.
        self._owns_connection = False

    @classmethod
    def connect_system(cls, parent: QObject | None = None) -> QtLogin1Transport:
        """Explicit production entry point; never called by cache/unit tests."""
        connection = QDBusConnection.connectToBus(
            QDBusConnection.BusType.SystemBus, CONNECTION_NAME
        )
        transport = cls(connection, parent)
        transport._owns_connection = True
        return transport

    def reconnect(self) -> None:
        """Re-open the named system connection after Disconnected (п. 8б)."""
        QDBusConnection.disconnectFromBus(CONNECTION_NAME)
        self.connection = QDBusConnection.connectToBus(
            QDBusConnection.BusType.SystemBus, CONNECTION_NAME
        )
        self._owns_connection = True

    def close(self) -> None:
        """Close the named system connection if this transport opened it (review n4).

        Idempotent: a second call (double ``SessionState.stop``) does nothing."""
        if not self._owns_connection:
            return
        self._owns_connection = False
        QDBusConnection.disconnectFromBus(CONNECTION_NAME)

    def read(
        self, path: str, interface: str, method: str, arguments: list[Any], callback: ReadCallback
    ) -> None:
        message = QDBusMessage.createMethodCall(LOGIN, path, interface, method)
        message.setAutoStartService(False)
        message.setArguments(arguments)
        pending = _Read(self, callback, self.timeout_ms)
        if not self.connection.callWithCallback(
            message, pending.ok, pending.error, self.timeout_ms
        ):
            pending.finish(None, True)

    def subscribe(
        self, service: str, path: str, interface: str, signal: str, callback: Callable[..., None]
    ) -> Callable[[], None]:
        receiver = _Signal(self, callback)
        if not self.connection.connect(service, path, interface, signal, receiver.receive):
            receiver.deleteLater()
            raise RuntimeError("login1_subscription_failed")

        def cancel() -> None:
            self.connection.disconnect(service, path, interface, signal, receiver.receive)
            receiver.deleteLater()

        return cancel


class SessionState(QObject):
    changed = pyqtSignal()
    sleep_changed = pyqtSignal(bool)

    def __init__(
        self,
        transport: Login1Transport,
        parent: QObject | None = None,
        *,
        uid: int | None = None,
        now_mono_ms: Callable[[], int] = monotonic_ms,
    ) -> None:
        super().__init__(parent)
        self.transport = transport
        self.uid = os.getuid() if uid is None else uid
        self.now_mono_ms = now_mono_ms
        self.locked = True
        self.known = False
        self.active = False
        self.suspending = False
        self.sleep_start_mono_ms: int | None = None
        self.generation = 0
        self.session_path: str | None = None
        # Session.Desktop (XDG_SESSION_DESKTOP) of the graphical session, from the
        # same GetAll that reads LockedHint — no extra bus call. Only a detection
        # source for the desktop (Fly, document 1.6); it never opens admission.
        # Kept across re-reads that fail: a stale "fly" stays fail-closed.
        self.desktop: str | None = None
        # Lock latch (review M2): a Lock request closes admission until either a
        # re-read triggered by Unlock or an observed LockedHint true→false. Other
        # re-reads (SessionNew, owner change, resume, Seat/Display) with a stale
        # LockedHint=false must not reopen it — Fly may never set LockedHint.
        self.lock_requested = False
        self._unlock_requested = False
        self._hint_true_seen = False
        self._subscriptions: list[Callable[[], None]] = []
        self._session_subscriptions: list[Callable[[], None]] = []
        self._started = False
        self._retry_ms = RETRY_FIRST_MS
        self._retry_reconnect = False
        self._retry_timer = QTimer(self)
        self._retry_timer.setSingleShot(True)
        self._retry_timer.timeout.connect(self._retry)

    def is_admissible(self) -> bool:
        return self.known and self.active and not self.locked and not self.suspending

    def lock_unconfirmed(self) -> bool:
        """Lock latch holds without confirmation (review minor-4, ИБ T2 Ф1).

        A ``Lock`` request arrived, but login1 never reported ``LockedHint=true``:
        admission stays closed until ``Unlock`` or a restart. ``Status.session``
        still says ``locked`` (contract 1.5 fixes three values); doctor recognises
        the same state from outside as "Status locked, LockedHint false"."""
        return self.lock_requested and not self._hint_true_seen

    def _unknown(self) -> None:
        self.known = False
        self.active = False
        self.changed.emit()

    def _invalidate(self) -> None:
        self.generation += 1
        self._unknown()

    def _guarded(self, callback: Callable[..., None]) -> Callable[..., None]:
        """A signal with unexpected arguments closes admission instead of raising."""

        def run(*args: Any) -> None:
            try:
                callback(*args)
            except Exception:
                # Any exception closes admission; none may escape a Qt slot.
                self._invalidate()

        return run

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        if not self._subscribe_manager():
            self._invalidate()
            self._schedule_retry(reconnect=True)
            return
        self.refresh()

    def _subscribe_manager(self) -> bool:
        try:
            for service, path, interface, signal, callback in (
                (
                    "org.freedesktop.DBus",
                    "/org/freedesktop/DBus",
                    "org.freedesktop.DBus",
                    "NameOwnerChanged",
                    self._owner_changed,
                ),
                (
                    "",
                    "/org/freedesktop/DBus/Local",
                    "org.freedesktop.DBus.Local",
                    "Disconnected",
                    self._disconnected,
                ),
                (LOGIN, MANAGER_PATH, MANAGER, "PrepareForSleep", self._prepare_sleep),
                (LOGIN, MANAGER_PATH, MANAGER, "SessionRemoved", self._session_removed),
                (LOGIN, MANAGER_PATH, MANAGER, "SessionNew", self._session_new),
            ):
                self._subscriptions.append(
                    self.transport.subscribe(
                        service, path, interface, signal, self._guarded(callback)
                    )
                )
        except Exception:
            self._cancel_subscriptions()
            return False
        return True

    def _cancel_subscriptions(self) -> None:
        for cancel in self._session_subscriptions + self._subscriptions:
            # A dead connection cannot disconnect; there is nothing else to do.
            with contextlib.suppress(Exception):
                cancel()
        self._session_subscriptions.clear()
        self._subscriptions.clear()
        self.session_path = None

    def stop(self) -> None:
        self._started = False
        self._retry_timer.stop()
        self._invalidate()
        self._cancel_subscriptions()
        # Release the named system connection (review n4); optional in the
        # protocol, idempotent, and a failure must not escape the daemon's _quit.
        close = getattr(self.transport, "close", None)
        if close is not None:
            with contextlib.suppress(Exception):
                close()

    def _schedule_retry(self, *, reconnect: bool = False) -> None:
        if not self._started:
            return
        self._retry_reconnect = self._retry_reconnect or reconnect
        if self._retry_timer.isActive():
            return
        self._retry_timer.start(self._retry_ms)
        self._retry_ms = min(self._retry_ms * 2, RETRY_MAX_MS)

    @pyqtSlot()
    def _retry(self) -> None:
        try:
            if not self._started:
                return
            if self._retry_reconnect:
                self._retry_reconnect = False
                self._cancel_subscriptions()
                reconnect = getattr(self.transport, "reconnect", None)
                if reconnect is not None:
                    reconnect()
                if not self._subscribe_manager():
                    self._invalidate()
                    self._schedule_retry(reconnect=True)
                    return
            self.refresh()
        except Exception:
            self._invalidate()
            self._schedule_retry(reconnect=True)

    def _read(
        self,
        generation: int,
        path: str,
        interface: str,
        method: str,
        arguments: list[Any],
        callback: Callable[[Any], None],
    ) -> None:
        def finish(value: Any, error: bool) -> None:
            if generation != self.generation or not self._started:
                # Contract §9 explicitly closes the cache on stale replies too.
                # The newer read may already have finished (a slow login1 reply
                # or timeout arriving late), so "unknown" must not stick: schedule
                # the ordinary retry (idempotent; a good _apply cancels it).
                self._unknown()
                self._schedule_retry()
                return
            if error:
                self._invalidate()
                self._schedule_retry()
                return
            try:
                callback(value)
            except Exception:
                self._invalidate()
                self._schedule_retry()

        self.transport.read(path, interface, method, arguments, finish)

    def refresh(self) -> None:
        if not self._started:
            return
        self._invalidate()
        generation = self.generation

        def sleeping(value: Any) -> None:
            if type(value) is not bool:
                raise ValueError
            self._set_sleep(value)
            self._read(generation, MANAGER_PATH, MANAGER, "GetSession", ["auto"], selected)

        def selected(path: Any) -> None:
            if not isinstance(path, str) or not path.startswith(LOGIN_PATH_PREFIX):
                raise ValueError
            if path != self.session_path or not self._session_subscriptions:
                # Re-subscribe only when the session path changes (review minor).
                for cancel in self._session_subscriptions:
                    cancel()
                self._session_subscriptions.clear()
                self.session_path = path
                try:
                    for interface, signal, callback in (
                        (PROPERTIES, "PropertiesChanged", self._properties_changed),
                        (SESSION, "Lock", self._lock),
                        (SESSION, "Unlock", self._unlock),
                    ):
                        # Concrete path, before ANY session GetAll (incl. LockedHint).
                        self._session_subscriptions.append(
                            self.transport.subscribe(
                                LOGIN, path, interface, signal, self._guarded(callback)
                            )
                        )
                except RuntimeError:
                    for cancel in self._session_subscriptions:
                        cancel()
                    self._session_subscriptions.clear()
                    self.session_path = None
                    self._invalidate()
                    self._schedule_retry()
                    return
            self._read(generation, MANAGER_PATH, MANAGER, "ListSessions", [], sessions)

        def sessions(rows: Any) -> None:
            if not isinstance(rows, (list, tuple)):
                raise ValueError
            paths = set()
            for row in rows:
                if not isinstance(row, (list, tuple)) or len(row) != 5:
                    raise ValueError
                if type(row[1]) is not int:
                    raise ValueError
                if row[1] == self.uid:
                    if not isinstance(row[4], str) or not row[4].startswith(LOGIN_PATH_PREFIX):
                        raise ValueError
                    paths.add(row[4])
            if self.session_path not in paths:
                raise ValueError
            remaining = sorted(paths)
            snapshots: dict[str, dict[str, Any]] = {}

            def next_session() -> None:
                if not remaining:
                    self._apply(snapshots)
                    return
                path = remaining.pop()

                def got(properties: Any) -> None:
                    if not isinstance(properties, dict):
                        raise ValueError
                    snapshots[path] = properties
                    next_session()

                self._read(generation, path, PROPERTIES, "GetAll", [SESSION], got)

            next_session()

        self._read(
            generation, MANAGER_PATH, PROPERTIES, "Get", [MANAGER, "PreparingForSleep"], sleeping
        )

    def _apply(self, snapshots: dict[str, dict[str, Any]]) -> None:
        graphical = []
        for path, properties in snapshots.items():
            user = properties["User"]
            if not isinstance(user, (list, tuple)) or len(user) != 2:
                raise ValueError
            if type(user[0]) is not int or user[0] != self.uid:
                raise ValueError
            if properties["Type"] in {"x11", "wayland"}:
                graphical.append(path)
        if graphical != [self.session_path]:
            raise ValueError
        props = snapshots[graphical[0]]
        seat = props["Seat"]
        if (
            props["Class"] != "user"
            or props["Remote"] is not False
            or not isinstance(seat, (tuple, list))
            or len(seat) != 2
            or not isinstance(seat[0], str)
            or not seat[0]
            or not isinstance(seat[1], str)
            or not seat[1].startswith("/org/freedesktop/login1/seat/")
            or not isinstance(props["Display"], str)
            or not props["Display"]
            or type(props["LockedHint"]) is not bool
            or type(props.get("Active")) is not bool
        ):
            raise ValueError
        hint = props["LockedHint"]
        desktop = props.get("Desktop")
        self.desktop = desktop if isinstance(desktop, str) else None
        if self.lock_requested:
            if hint:
                self._hint_true_seen = True
            elif self._unlock_requested or self._hint_true_seen:
                # Re-read after Unlock, or an observed LockedHint true→false.
                self.lock_requested = False
                self._unlock_requested = False
                self._hint_true_seen = False
        self.locked = hint or self.lock_requested
        self.active = props["Active"]
        self.known = True
        self._retry_ms = RETRY_FIRST_MS
        self._retry_timer.stop()
        self._retry_reconnect = False
        self.changed.emit()

    def _lock(self) -> None:
        self.generation += 1  # A pre-Lock read must not reopen admission.
        self.locked = True
        self.lock_requested = True
        self._unlock_requested = False
        self._hint_true_seen = False
        self.changed.emit()

    def _unlock(self) -> None:
        if self.lock_requested:
            self._unlock_requested = True
        self.refresh()

    def _properties_changed(self, interface: str, changed: Any, invalidated: Any) -> None:
        if interface != SESSION:
            return
        if not isinstance(changed, dict) or not isinstance(invalidated, (list, tuple)):
            raise ValueError
        if changed.get("LockedHint") is True:
            self._lock()
            self._hint_true_seen = True
        elif _CHECKED_PROPERTIES.intersection(changed) or _CHECKED_PROPERTIES.intersection(
            invalidated
        ):
            # LockedHint=false is trusted only after a full re-read (§9).
            self.refresh()

    def _set_sleep(self, sleeping: bool) -> None:
        previous = self.suspending
        self.suspending = sleeping
        if sleeping and not previous:
            self.sleep_start_mono_ms = self.now_mono_ms()
        if previous != sleeping:
            self.sleep_changed.emit(sleeping)
            self.changed.emit()

    def _prepare_sleep(self, sleeping: bool) -> None:
        if type(sleeping) is not bool:
            raise ValueError
        self._invalidate()
        self._set_sleep(sleeping)
        if not sleeping:
            self.refresh()

    def _owner_changed(self, name: str, old: str, new: str) -> None:
        if name == LOGIN:
            self._invalidate()
            if new:
                self.refresh()

    def _disconnected(self) -> None:
        self._invalidate()
        self._schedule_retry(reconnect=True)

    def _session_removed(self, session_id: str, path: str) -> None:
        self._invalidate()
        if path != self.session_path:
            self.refresh()

    def _session_new(self, session_id: str, path: str) -> None:
        self.refresh()


def _fly_process_present(proc: Path = Path("/proc")) -> bool:
    """No GUI probing; unreadable process inventory fails closed until K5."""
    try:
        entries = list(proc.iterdir())
    except OSError:
        return True
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            if (
                entry.stat().st_uid == os.getuid()
                and (entry / "comm").read_bytes().strip() == b"fly-wm"
            ):
                return True
        except OSError:
            continue
    return False


class _SessionWorker(QObject):
    updated = pyqtSignal(object)

    def __init__(self) -> None:
        super().__init__()
        self.snapshot = SessionSnapshot()
        self.state: SessionState | None = None
        self.screen_bus: Any = None
        self.screen_name = CONNECTION_NAME + "-screensaver"
        self._fly_environment = any(
            "fly" in os.environ.get(name, "").lower()
            for name in ("XDG_CURRENT_DESKTOP", "DESKTOP_SESSION")
        )
        self._fly_process = True
        self._fly_checked = 0.0
        self._blocked_epoch = 0
        self._cue_epoch = 0
        self._sleep_epoch = 0

    @pyqtSlot()
    def start(self) -> None:
        self.state = SessionState(QtLogin1Transport.connect_system(self), self)
        self.state.changed.connect(self._changed)
        address = resolve_bus_address()
        if address is not None:
            self.screen_bus = QDBusConnection.connectToBus(address, self.screen_name)
            self.screen_bus.connect(
                "org.freedesktop.ScreenSaver",
                "",
                "org.freedesktop.ScreenSaver",
                "ActiveChanged",
                self._screen,
            )
        self.state.start()

    @pyqtSlot(bool)
    def _screen(self, active: bool) -> None:
        if self.state is None:
            return
        if active:
            self.state._lock()
        else:
            # A signal alone cannot grant admission: refresh the concrete session.
            self.state._unlock()

    def _changed(self) -> None:
        assert self.state is not None
        now = time.monotonic()
        if now - self._fly_checked > 30.0:
            self._fly_process = _fly_process_present()
            self._fly_checked = now
        unsupported = (
            self._fly_environment
            or self._fly_process
            or "fly" in (self.state.desktop or "").lower()
        )
        cue_allowed = self.state.is_admissible() and not self.state.lock_requested
        previous_cue_allowed = (
            self.snapshot.known
            and self.snapshot.active
            and not self.snapshot.locked
            and not self.snapshot.lock_requested
            and not self.snapshot.preparing_for_sleep
        )
        # Fly may always be unsupported for Cowork. Cue invalidation must still
        # latch known→unknown/inactive/locked until queued GUI callbacks arrive.
        if previous_cue_allowed and not cue_allowed:
            self._cue_epoch += 1
        allowed = self.state.is_admissible() and not unsupported
        if (
            (self.snapshot.allowed and not allowed)
            or (self.state.lock_requested and not self.snapshot.lock_requested)
            or (self.state.suspending and not self.snapshot.preparing_for_sleep)
        ):
            self._blocked_epoch += 1
        if self.state.suspending and not self.snapshot.preparing_for_sleep:
            self._sleep_epoch += 1
        self.snapshot = SessionSnapshot(
            known=self.state.known,
            locked=self.state.locked,
            active=self.state.active,
            preparing_for_sleep=self.state.suspending,
            lock_requested=self.state.lock_requested,
            supported=not unsupported,
            blocked_epoch=self._blocked_epoch,
            cue_epoch=self._cue_epoch,
            sleep_epoch=self._sleep_epoch,
        )
        self.updated.emit(self.snapshot)

    @pyqtSlot()
    def stop(self) -> None:
        if self.state is not None:
            self.state.stop()
        self.snapshot = SessionSnapshot()
        if self.screen_bus is not None:
            self.screen_bus.disconnect(
                "org.freedesktop.ScreenSaver",
                "",
                "org.freedesktop.ScreenSaver",
                "ActiveChanged",
                self._screen,
            )
            QDBusConnection.disconnectFromBus(self.screen_name)
        QThread.currentThread().quit()


class SessionMonitor(QObject):
    """Worker-owned login1 subscriptions, immutable snapshot visible to both threads."""

    changed = pyqtSignal(object)
    _stop = pyqtSignal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._thread = QThread(self)
        self._worker = _SessionWorker()
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.start)
        self._thread.finished.connect(self._worker.deleteLater)
        self._worker.updated.connect(self.changed)
        self._stop.connect(self._worker.stop)
        self._closed = False

    def snapshot(self) -> SessionSnapshot:
        return self._worker.snapshot

    def start(self) -> None:
        if not self._closed and not self._thread.isRunning():
            self._thread.start()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._thread.isRunning():
            self._stop.emit()
            self._thread.wait(2000)
