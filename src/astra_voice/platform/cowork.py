"""Local, single-attempt Command1 client. No command text enters diagnostics.

The Qt connection and callbacks run in a dedicated QThread. Construction has no
bus side effects; start() is the explicit connection boundary. No service is
activated, and the deadline includes time spent waiting for the worker thread.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from typing import Any
from uuid import uuid4

from PyQt5.QtCore import QMetaType, QObject, Qt, QThread, QTimer, pyqtSignal, pyqtSlot
from PyQt5.QtDBus import QDBusArgument, QDBusConnection, QDBusError, QDBusMessage, QDBusVariant

SERVICE = "ru.astralinux.Cowork"
OBJECT_PATH = "/ru/astralinux/Cowork"
INTERFACE = "ru.astralinux.Cowork.Command1"
BUDGET_MS = 300
DELIVERY_REASONS = (
    "none",
    "not_installed",
    "no_bus",
    "not_running",
    "no_contract",
    "busy",
    "refused",
    "expired",
    "timeout",
    "disconnected",
    "bad_reply",
    "locked",
    "suspended",
    "model_untrusted",
    "clipboard_failed",
)
TEXT_FORBIDDEN_RANGES = (
    (0x0, 0x1F),
    (0x7F, 0x9F),
    (0xAD, 0xAD),
    (0x34F, 0x34F),
    (0x600, 0x605),
    (0x61C, 0x61C),
    (0x6DD, 0x6DD),
    (0x70F, 0x70F),
    (0x890, 0x891),
    (0x8E2, 0x8E2),
    (0x115F, 0x1160),
    (0x17B4, 0x17B5),
    (0x180B, 0x180F),
    (0x200B, 0x200B),
    (0x200E, 0x200F),
    (0x2028, 0x202E),
    (0x2060, 0x206F),
    (0x2800, 0x2800),
    (0x3164, 0x3164),
    (0xFEFF, 0xFEFF),
    (0xFFA0, 0xFFA0),
    (0xFFF0, 0xFFFB),
    (0x110BD, 0x110BD),
    (0x110CD, 0x110CD),
    (0x13430, 0x1343F),
    (0x1BCA0, 0x1BCA3),
    (0x1D173, 0x1D17A),
    (0xE0000, 0xE0FFF),
)
SERVER_REASONS = frozenset(
    (
        "shutting_down",
        "rate_limited",
        "locked",
        "suspending",
        "queue_full",
        "invalid_options",
        "invalid_text",
        "unsupported_contract",
        "bad_source",
        "bad_mode",
        "unverified_sender",
        "request_conflict",
        "policy_disabled",
        "not_ready",
        "session_unsupported",
        "deadline",
        "suspended",
    )
)
_ERROR_PREFIX = "org.freedesktop.DBus.Error."
_ERROR_REASONS = {
    **dict.fromkeys(("NoReply", "Timeout", "TimedOut"), ("unknown", "timeout")),
    "Disconnected": ("unknown", "disconnected"),
    **dict.fromkeys(("NameHasNoOwner", "ServiceUnknown"), ("undelivered", "not_running")),
    **dict.fromkeys(
        ("UnknownInterface", "UnknownMethod", "UnknownObject"), ("undelivered", "no_contract")
    ),
    **dict.fromkeys(
        ("InvalidArgs", "InvalidSignature", "AccessDenied", "LimitsExceeded"),
        ("undelivered", "refused"),
    ),
}
ERROR_ALLOWLIST = {_ERROR_PREFIX + name: result for name, result in _ERROR_REASONS.items()}


@dataclass(frozen=True)
class DeliveryResult:
    outcome: str
    reason: str
    error_name: str = ""
    server_reason: str = ""

    @property
    def kind(self) -> str:
        return self.outcome


def monotonic_ms() -> int:
    return time.monotonic_ns() // 1_000_000


def normalize_text(text: str) -> str:
    """Contract 1.7 normalization, with a fixed Unicode 15.1 range table."""
    text = re.sub(r"[\r\n\u0085\u2028\u2029]", " ", text)
    text = "".join(c for c in text if not any(a <= ord(c) <= b for a, b in TEXT_FORBIDDEN_RANGES))
    text = re.sub(r"([\ufe00-\ufe0f])[\ufe00-\ufe0f]+", r"\1", text)
    return " ".join(text.split())


def valid_text(text: str) -> bool:
    return (
        1 <= len(text) <= 4000
        and not any(0xD800 <= ord(c) <= 0xDFFF for c in text)
        and any(
            not c.isspace() and c not in "\u200c\u200d" and not 0xFE00 <= ord(c) <= 0xFE0F
            for c in text
        )
    )


def resolve_bus_address(environ: Mapping[str, str] | None = None) -> str | None:
    env = os.environ if environ is None else environ
    address = env.get("DBUS_SESSION_BUS_ADDRESS")
    if not address:
        runtime = env.get("XDG_RUNTIME_DIR")
        if not runtime:
            return None
        path = Path(runtime) / "bus"
        try:
            if not stat.S_ISSOCK(path.stat().st_mode):
                return None
        except OSError:
            return None
        # D-Bus escaping, including delimiters in an unusual runtime directory.
        encoded = "".join(
            c
            if c.isascii() and (c.isalnum() or c in "/_-.")
            else "".join(f"%{b:02x}" for b in c.encode())
            for c in str(path.resolve())
        )
        address = "unix:path=" + encoded
    elements = address.split(";")
    if not elements or any(not e.startswith("unix:") or not e[5:] for e in elements):
        return None
    # Reject malformed properties as well as mixed or executable transports.
    for element in elements:
        fields = element[5:].split(",")
        if any(not re.fullmatch(r"[A-Za-z_]+=[A-Za-z0-9_./\\%:-]+", f) for f in fields):
            return None
        if not any(f.startswith(("path=", "abstract=")) for f in fields):
            return None
        if re.search(r"%(?![0-9a-fA-F]{2})", element):
            return None
    return address


def is_installed(environ: Mapping[str, str] | None = None) -> bool:
    env = os.environ if environ is None else environ
    home = Path(env.get("HOME", str(Path.home())))
    return any(
        shutil.which(name, path=env.get("PATH", ""))
        for name in ("astra-cowork-daemon", "astra-cowork")
    ) or any(
        p.is_file()
        for p in (
            Path("/usr/share/applications/astra-cowork.desktop"),
            Path("/usr/lib/systemd/user/astra-cowork-daemon.service"),
            Path("/usr/share/dbus-1/services/ru.astralinux.Cowork.service"),
            home / ".local/share/applications/astra-cowork.desktop",
            home / ".config/systemd/user/astra-cowork-daemon.service",
            home / ".config/autostart/astra-cowork.desktop",
        )
    )


def _plain(value: Any) -> Any:
    if isinstance(value, QDBusVariant):
        return _plain(value.variant())
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    return value


def parse_reply(payload: object, request_id: str) -> DeliveryResult:
    payload = _plain(payload)
    bad = DeliveryResult("unknown", "bad_reply")
    if not isinstance(payload, dict):
        return bad
    status = payload.get("status")
    contract = payload.get("contract")
    if type(contract) is not int or contract < 1:
        return bad
    if "request_id" in payload and payload["request_id"] != request_id:
        return bad
    if "duplicate" in payload and type(payload["duplicate"]) is not bool:
        return bad
    reason = payload.get("reason", "")
    if not isinstance(reason, str):
        return bad
    if status == "accepted":
        return (
            DeliveryResult("delivered", "none")
            if payload.get("request_id") == request_id and not reason
            else bad
        )
    if status in ("busy", "refused", "expired") and reason:
        return DeliveryResult(
            "undelivered",
            status,
            server_reason=reason if reason in SERVER_REASONS else "unknown_reason",
        )
    return bad


def classify_error(name: str, *, sent: bool = True, connected: bool = True) -> DeliveryResult:
    safe_name = (
        name if name in ERROR_ALLOWLIST or name == _ERROR_PREFIX + "Failed" else "remote_error"
    )
    if not sent:
        return DeliveryResult("undelivered", "refused" if connected else "no_bus", safe_name)
    outcome, reason = ERROR_ALLOWLIST.get(name, ("unknown", "bad_reply"))
    return DeliveryResult(outcome, reason, safe_name)


@dataclass
class _Ticket:
    lock: Any = field(default_factory=Lock)
    sent: bool = False
    terminal: bool = False


class _Call(QObject):
    def __init__(self, parent: QObject, callback: Callable[[Any, str], None], timeout: int) -> None:
        super().__init__(parent)
        self.callback = callback
        self.done = False
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setTimerType(Qt.PreciseTimer)
        self.timer.timeout.connect(lambda: self.finish(None, _ERROR_PREFIX + "Timeout"))
        self.timer.start(timeout)

    def finish(self, payload: Any, error: str = "") -> None:
        if self.done:
            return
        self.done = True
        self.timer.stop()
        self.callback(payload, error)
        self.deleteLater()

    @pyqtSlot(QDBusMessage)
    def ok(self, message: QDBusMessage) -> None:
        args = message.arguments()
        self.finish(
            args[0] if message.type() == QDBusMessage.ReplyMessage and len(args) == 1 else None
        )

    @pyqtSlot(QDBusError, QDBusMessage)
    def error(self, error: QDBusError, message: QDBusMessage) -> None:
        self.finish(None, error.name())


def method(
    service: str, path: str, interface: str, name: str, arguments: list[Any]
) -> QDBusMessage:
    message = QDBusMessage.createMethodCall(service, path, interface, name)
    message.setAutoStartService(False)
    message.setArguments(arguments)
    return message


class _Worker(QObject):
    result = pyqtSignal(int, object)
    owner_changed = pyqtSignal(bool)
    status_changed = pyqtSignal(object)

    def __init__(self, address: str | None) -> None:
        super().__init__()
        self.address = address
        self.connection: Any = None
        self.connection_name = "astra-voice-cowork-" + uuid4().hex
        self.owner = ""
        self.owner_generation = 0
        self.pending: dict[int, tuple[str, int, _Call]] = {}
        self.closed = False
        self.admission_guard: Callable[[], bool] = lambda: True
        self.model_guard: Callable[[], bool] = lambda: True

    @pyqtSlot()
    def start(self) -> None:
        if not self.address:
            return
        self.connection = QDBusConnection.connectToBus(self.address, self.connection_name)
        if not self.connection.isConnected():
            return
        if not self.connection.connect(
            "org.freedesktop.DBus",
            "/org/freedesktop/DBus",
            "org.freedesktop.DBus",
            "NameOwnerChanged",
            self._owner,
        ):
            return
        generation = self.owner_generation

        def got(payload: Any, error: str) -> None:
            if generation == self.owner_generation:
                self.owner = payload if isinstance(payload, str) and not error else ""
                self.owner_changed.emit(bool(self.owner))

        call = _Call(self, got, BUDGET_MS)
        message = method(
            "org.freedesktop.DBus",
            "/org/freedesktop/DBus",
            "org.freedesktop.DBus",
            "GetNameOwner",
            [SERVICE],
        )
        if not self.connection.callWithCallback(message, call.ok, call.error, BUDGET_MS):
            call.finish(None, "local_failure")

    @pyqtSlot(str, str, str)
    def _owner(self, service: str, old: str, new: str) -> None:
        if service != SERVICE:
            return
        self.owner_generation += 1
        self.owner = new
        self.owner_changed.emit(bool(new))
        for serial in tuple(self.pending):
            self._finish(serial, DeliveryResult("unknown", "timeout"))

    def _finish(self, serial: int, result: DeliveryResult) -> None:
        pending = self.pending.pop(serial, None)
        if pending is not None:
            pending[2].done = True
            pending[2].timer.stop()
            pending[2].deleteLater()
            self.result.emit(serial, result)

    @pyqtSlot(int, str, object)
    def submit(self, serial: int, text: str, payload: tuple[dict[str, Any], _Ticket]) -> None:
        options, ticket = payload
        if self.closed or ticket.terminal:
            return
        if self.connection is None or not self.connection.isConnected():
            self.result.emit(serial, DeliveryResult("undelivered", "no_bus"))
            return
        if not self.owner:
            self.result.emit(serial, DeliveryResult("undelivered", "not_running"))
            return
        remaining = options["deadline_mono_ms"] - monotonic_ms()
        if remaining <= 0:
            self.result.emit(serial, DeliveryResult("undelivered", "timeout"))
            return
        if not self.admission_guard():
            self.result.emit(serial, DeliveryResult("undelivered", "locked"))
            return
        request_id = options["request_id"]

        def done(payload: Any, error: str) -> None:
            result = classify_error(error) if error else parse_reply(payload, request_id)
            if monotonic_ms() > options["deadline_mono_ms"]:
                result = DeliveryResult("unknown", "timeout")
            self._finish(serial, result)

        call = _Call(self, done, remaining)
        self.pending[serial] = (request_id, options["deadline_mono_ms"], call)
        wire = dict(options)
        for key in ("sent_mono_ms", "deadline_mono_ms", "sent_boot_ms"):
            wire[key] = QDBusArgument(wire[key], QMetaType.LongLong)
        # Pin to the observed unique owner; a replacement cannot receive an old request.
        message = method(self.owner, OBJECT_PATH, INTERFACE, "Submit", [text, wire])
        try:
            trusted = self.model_guard()
        except Exception:
            trusted = False
        if not trusted:
            self._finish(serial, DeliveryResult("undelivered", "model_untrusted"))
            return
        with ticket.lock:
            remaining = options["deadline_mono_ms"] - monotonic_ms()
            if ticket.terminal or remaining <= 0:
                self._finish(serial, DeliveryResult("undelivered", "timeout"))
                return
            if not self.admission_guard():
                self._finish(serial, DeliveryResult("undelivered", "locked"))
                return
            ticket.sent = True
            queued = self.connection.callWithCallback(message, call.ok, call.error, remaining)
            if not queued:
                ticket.sent = False
        if not queued:
            self._finish(
                serial,
                classify_error(
                    self.connection.lastError().name(),
                    sent=False,
                    connected=self.connection.isConnected(),
                ),
            )

    @pyqtSlot(int)
    def cancel(self, serial: int) -> None:
        self._finish(serial, DeliveryResult("unknown", "suspended"))

    @pyqtSlot()
    def status(self) -> None:
        if self.connection is None or not self.owner:
            self.status_changed.emit(None)
            return
        call = _Call(
            self,
            lambda payload, error: self.status_changed.emit(None if error else _plain(payload)),
            BUDGET_MS,
        )
        if not self.connection.callWithCallback(
            method(self.owner, OBJECT_PATH, INTERFACE, "Status", []), call.ok, call.error, BUDGET_MS
        ):
            call.finish(None, "local_failure")

    @pyqtSlot()
    def close(self) -> None:
        self.closed = True
        for serial in tuple(self.pending):
            self._finish(serial, DeliveryResult("unknown", "disconnected"))
        if self.connection is not None:
            self.connection.disconnect(
                "org.freedesktop.DBus",
                "/org/freedesktop/DBus",
                "org.freedesktop.DBus",
                "NameOwnerChanged",
                self._owner,
            )
            QDBusConnection.disconnectFromBus(self.connection_name)
        QThread.currentThread().quit()


class CoworkClient(QObject):
    """GUI-side facade. submit() invokes its callback exactly once, without retry."""

    owner_changed = pyqtSignal(bool)
    status_changed = pyqtSignal(object)
    _submit = pyqtSignal(int, str, object)
    _cancel = pyqtSignal(int)
    _status = pyqtSignal()
    _close = pyqtSignal()

    def __init__(self, parent: QObject | None = None, *, address: str | None = None) -> None:
        super().__init__(parent)
        self._thread = QThread(self)
        self._worker = _Worker(
            resolve_bus_address({"DBUS_SESSION_BUS_ADDRESS": address})
            if address is not None
            else resolve_bus_address()
        )
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.start)
        self._thread.finished.connect(self._worker.deleteLater)
        self._submit.connect(self._worker.submit)
        self._cancel.connect(self._worker.cancel)
        self._status.connect(self._worker.status)
        self._close.connect(self._worker.close)
        self._worker.result.connect(self._result)
        self._worker.owner_changed.connect(self.owner_changed)
        self._worker.status_changed.connect(self.status_changed)
        self._callbacks: dict[int, Callable[[DeliveryResult], None]] = {}
        self._tickets: dict[int, tuple[_Ticket, QTimer]] = {}
        self._serial = 0
        self._closed = False

    def set_admission_guard(self, guard: Callable[[], bool]) -> None:
        """Guard must read immutable/thread-safe state; checked immediately before send."""
        self._worker.admission_guard = guard

    def set_model_guard(self, guard: Callable[[], bool]) -> None:
        """Read-only, thread-safe model trust check at the actual send boundary."""
        self._worker.model_guard = guard

    def start(self) -> None:
        if not self._closed and not self._thread.isRunning():
            self._thread.start()

    def submit(self, text: str, callback: Callable[[DeliveryResult], None]) -> None:
        if self._closed or not self._thread.isRunning():
            callback(DeliveryResult("undelivered", "no_bus"))
            return
        self._serial += 1
        serial = self._serial
        text = normalize_text(text)
        if not valid_text(text):
            callback(DeliveryResult("undelivered", "refused", server_reason="invalid_text"))
            return
        self._callbacks[serial] = callback
        ticket = _Ticket()
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.setTimerType(Qt.PreciseTimer)
        timer.timeout.connect(lambda: self._expire(serial))
        self._tickets[serial] = (ticket, timer)
        sent = monotonic_ms()
        timer.start(BUDGET_MS)
        self._submit.emit(
            serial,
            text,
            (
                {
                    "source": "astra-voice",
                    "contract": 1,
                    "request_id": uuid4().hex,
                    "sent_mono_ms": sent,
                    "deadline_mono_ms": sent + BUDGET_MS,
                    "sent_boot_ms": time.clock_gettime_ns(time.CLOCK_BOOTTIME) // 1_000_000,
                    "mode": "submit",
                },
                ticket,
            ),
        )

    def _expire(self, serial: int, reason: str = "timeout") -> None:
        entry = self._tickets.get(serial)
        if entry is None:
            return
        ticket, _ = entry
        with ticket.lock:
            ticket.terminal = True
            outcome = "unknown" if ticket.sent else "undelivered"
        self._cancel.emit(serial)
        self._result(serial, DeliveryResult(outcome, reason))

    @pyqtSlot(int, object)
    def _result(self, serial: int, result: DeliveryResult) -> None:
        entry = self._tickets.pop(serial, None)
        if entry is not None:
            ticket, timer = entry
            with ticket.lock:
                ticket.terminal = True
            timer.stop()
            timer.deleteLater()
        callback = self._callbacks.pop(serial, None)
        if callback is not None:
            callback(result)

    def cancel(self) -> None:
        """Only suspension/teardown invalidates callbacks; user cancellation does not."""
        for serial in tuple(self._callbacks):
            self._expire(serial, "suspended")

    def status(self) -> None:
        self._status.emit()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.cancel()
        if self._thread.isRunning():
            self._close.emit()
            self._thread.wait(2000)


def launcher_arguments() -> list[str] | None:
    """Return only fixed launchers; user desktop files are detection-only."""
    unit_paths = (
        Path("/usr/lib/systemd/user/astra-cowork-daemon.service"),
        Path("/lib/systemd/user/astra-cowork-daemon.service"),
        Path.home() / ".config/systemd/user/astra-cowork-daemon.service",
    )
    if any(path.is_file() for path in unit_paths):
        return ["systemctl", "--user", "start", "astra-cowork-daemon.service"]
    elif Path("/usr/share/applications/astra-cowork.desktop").is_file():
        return ["gio", "launch", "/usr/share/applications/astra-cowork.desktop"]
    return None


def launch_cowork(*, popen: Callable[..., object] | None = None) -> bool:
    """Explicit user action only; never execute a user-supplied desktop Exec."""
    import subprocess

    from astra_voice.core.childenv import clean_env

    if popen is None:
        popen = subprocess.Popen
    arguments = launcher_arguments()
    if arguments is None:
        return False
    try:
        popen(
            arguments,
            shell=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            env=clean_env(),
        )
    except OSError:
        return False
    return True
