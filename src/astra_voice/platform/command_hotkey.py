"""Private, bounded command-key broker. Ordinary dictation never uses this port."""

from __future__ import annotations

import json
import math
import os
import select
import socket
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from typing import Any

from astra_voice.platform.hotkey import GrabResult, HotkeyEvent, MappingEvent

PACKET_LIMIT = 4096
HEARTBEAT_S = 0.1
HEALTH_LIMIT_S = 0.6
RPC_LIMIT_S = 0.4


def boot_time() -> float:
    return time.clock_gettime(time.CLOCK_BOOTTIME)


def send_packet(connection: socket.socket, message: dict[str, Any]) -> None:
    packet = json.dumps(message, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    if len(packet) > PACKET_LIMIT:
        raise ValueError("oversized broker packet")
    if connection.send(packet) != len(packet):
        raise OSError("short broker packet")


def receive_packet(connection: socket.socket) -> dict[str, Any]:
    packet = connection.recv(PACKET_LIMIT + 1)
    if not packet or len(packet) > PACKET_LIMIT:
        raise ValueError("invalid broker packet")
    try:
        message = json.loads(packet)
    except RecursionError as exc:
        raise ValueError("nested broker packet") from exc
    if not isinstance(message, dict):
        raise ValueError("invalid broker message")
    return message


def _integer(value: Any, lower: int, upper: int) -> bool:
    return type(value) is int and lower <= value <= upper


def _grab_result(value: Any) -> GrabResult:
    if not isinstance(value, dict) or set(value) != {"code", "owner_hint", "keycode", "mods"}:
        raise ValueError("invalid broker grab")
    if (
        value["code"] not in ("ok", "busy", "bad-combo", "duplicate", "not-grabbed")
        or (
            value["owner_hint"] is not None
            and (not isinstance(value["owner_hint"], str) or len(value["owner_hint"]) > 512)
        )
        or (value["keycode"] is not None and not _integer(value["keycode"], 8, 255))
        or not _integer(value["mods"], 0, 255)
    ):
        raise ValueError("invalid broker grab fields")
    return GrabResult(**value)


def _key_event(value: Any) -> HotkeyEvent:
    if not isinstance(value, dict) or set(value) != {
        "kind",
        "keycode",
        "time",
        "escape",
        "mods",
        "confirmed_hold",
    }:
        raise ValueError("invalid broker key")
    if (
        value["kind"] not in ("KeyPress", "KeyRelease")
        or not _integer(value["keycode"], 8, 255)
        or not _integer(value["time"], 0, 2**32 - 1)
        or not _integer(value["mods"], 0, 255)
        or type(value["escape"]) is not bool
        or type(value["confirmed_hold"]) is not bool
        or (value["confirmed_hold"] and value["kind"] != "KeyPress")
    ):
        raise ValueError("invalid broker key fields")
    return HotkeyEvent(**value)


def _mapping_event(value: Any) -> MappingEvent:
    if not isinstance(value, dict) or set(value) != {
        "combos",
        "escape",
        "request",
        "first",
        "count",
        "keycode_changed",
        "masks_changed",
        "regrabbed",
        "elapsed_ms",
    }:
        raise ValueError("invalid broker mapping")
    if (
        not isinstance(value["combos"], dict)
        or len(value["combos"]) > 8
        or any(not isinstance(key, str) or not 0 < len(key) <= 128 for key in value["combos"])
        or not isinstance(value["request"], str)
        or len(value["request"]) > 32
        or not _integer(value["first"], 0, 255)
        or not _integer(value["count"], 0, 256)
        or any(
            type(value[field]) is not bool
            for field in ("keycode_changed", "masks_changed", "regrabbed")
        )
        or type(value["elapsed_ms"]) not in (int, float)
        or not 0 <= value["elapsed_ms"] <= 60_000
        or not math.isfinite(value["elapsed_ms"])
    ):
        raise ValueError("invalid broker mapping fields")
    return MappingEvent(
        **{
            **value,
            "combos": {key: _grab_result(result) for key, result in value["combos"].items()},
            "escape": None if value["escape"] is None else _grab_result(value["escape"]),
        }
    )


class CommandHotkeyBackend:
    """GUI-owned port; only the subprocess accesses the command X connection.

    A stable wake pipe keeps the runtime's notifier valid across child failure.
    Every child has a new private socket and generation; stale messages cannot
    enter a replacement child. Without Qt, tests must call poll_events regularly.
    """

    def __init__(self, *, manage_menu: bool = False) -> None:
        self._connection: socket.socket | None = None
        self.process: subprocess.Popen[bytes] | None = None
        self._read, self._write = os.pipe2(os.O_NONBLOCK | os.O_CLOEXEC)
        self._events: deque[HotkeyEvent | MappingEvent] = deque()
        self._replies: dict[int, dict[str, Any]] = {}
        self._sequence = 0
        self._operation = ""
        self._last_reply = self._last_ping = 0.0
        self._combo: str | None = None
        self._defer_super = False
        self._closed = False
        self._notifier: Any = None
        self._timer: Any = None
        self._manage_menu = manage_menu
        self._menu_token: str | None = None
        self._menu_recovered = False
        self._menu_thread: threading.Thread | None = None
        self._menu_connection: Any = None
        self._menu_connection_name = ""
        self._menu_object: Any = None
        self._menu_path = ""

    def _finish_menu_release(self) -> None:
        if self._menu_thread is not None and self._menu_thread.is_alive():
            return
        if self._menu_token is None and self._menu_connection is not None:
            from PyQt5.QtDBus import QDBusConnection

            self._menu_connection.unregisterObject(self._menu_path)
            QDBusConnection.disconnectFromBus(self._menu_connection_name)
            self._menu_connection = self._menu_object = None

    def _release_menu(self) -> None:
        token = self._menu_token
        if token is None or (self._menu_thread is not None and self._menu_thread.is_alive()):
            return
        from astra_voice.platform.kwin_command_lease import KWinCommandLease, KWinLeaseError

        def restore() -> None:
            # Configuration I/O only: never touch Qt objects or Xlib in this thread.
            for _ in range(3):
                try:
                    KWinCommandLease().release(token)
                    self._menu_token = None
                    return
                except KWinLeaseError:
                    time.sleep(0.2)

        self._menu_thread = threading.Thread(
            target=restore, name="command-menu-restore", daemon=True
        )
        self._menu_thread.start()

    def _acquire_menu(self) -> bool:
        self._finish_menu_release()
        if self._menu_thread is not None and self._menu_thread.is_alive():
            return False
        if self._menu_token is not None:
            self._release_menu()
            return False
        # Signature lookups may have started an idle child. Configuration calls
        # can take seconds; no broker heartbeat or X grab may exist during them.
        self._fail()
        from PyQt5.QtCore import QObject, pyqtSlot
        from PyQt5.QtDBus import QDBusConnection

        from astra_voice.platform.cowork import resolve_bus_address
        from astra_voice.platform.kwin_command_lease import (
            NOOP_INTERFACE,
            KWinCommandLease,
            KWinLeaseError,
            lease_token,
            object_path,
        )

        class Endpoint(QObject):
            @pyqtSlot()
            def Ignore(self) -> None:
                pass

        try:
            lease = KWinCommandLease()
            if not self._menu_recovered:
                # Production constructs this port only after the app instance lock.
                lease.recover()
                self._menu_recovered = True
            address = resolve_bus_address()
            if address is None:
                return False
            generation = uuid.uuid4().hex
            self._menu_connection_name = "astra-voice-command-meta-" + generation
            connection = QDBusConnection.connectToBus(address, self._menu_connection_name)
            self._menu_connection = connection
            self._menu_path = object_path(generation)
            self._menu_object = Endpoint()
            if not connection.isConnected() or not connection.registerObject(
                self._menu_path,
                NOOP_INTERFACE,
                self._menu_object,
                QDBusConnection.ExportAllSlots,
            ):
                self._finish_menu_release()
                return False
            # Remember the exact token even if acquire fails halfway through.
            self._menu_token = lease_token(connection.baseService(), generation)
            lease.acquire(connection.baseService(), generation)
            return True
        except KWinLeaseError:
            self._release_menu()
            self._finish_menu_release()
            return False

    def configure(self, *, defer_super: bool) -> None:
        self._defer_super = defer_super

    def _launch(self) -> bool:
        if self._closed:
            return False
        if self._connection is not None:
            return True
        from astra_voice.worker.supervisor import launcher_path

        parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        try:
            self.process = subprocess.Popen(
                [sys.executable, "-I", str(launcher_path()), "command-hotkey", str(child.fileno())],
                pass_fds=(child.fileno(),),
                close_fds=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError:
            parent.close()
            return False
        finally:
            child.close()
        parent.setblocking(False)
        self._connection = parent
        self._last_reply = self._last_ping = boot_time()
        from PyQt5.QtCore import QCoreApplication, QSocketNotifier, QTimer

        if QCoreApplication.instance() is not None:
            self._notifier = QSocketNotifier(parent.fileno(), QSocketNotifier.Read)
            self._notifier.activated.connect(self._pump)
            if self._timer is None:
                self._timer = QTimer()
                self._timer.setInterval(int(HEARTBEAT_S * 1000))
                self._timer.timeout.connect(self._pump)
                self._timer.start()
        return True

    def _queue(self, event: HotkeyEvent | MappingEvent) -> None:
        if len(self._events) >= 128:
            raise ValueError("broker event overflow")
        self._events.append(event)
        try:
            os.write(self._write, b"x")
        except BlockingIOError:
            pass

    def _fail(self) -> None:
        connection, self._connection = self._connection, None
        if self._notifier is not None:
            self._notifier.setEnabled(False)
            self._notifier.deleteLater()
            self._notifier = None
        if connection is not None:
            connection.close()
        process, self.process = self.process, None
        if process is not None:
            if process.poll() is None:
                process.kill()  # SIGKILL also terminates a SIGSTOP'd broker.
            process.wait(timeout=0.5)
        self._events.clear()
        self._replies.clear()
        if self._combo is not None:
            self._queue(MappingEvent({self._combo: GrabResult("not-grabbed")}, None))
        self._release_menu()

    def _pump(self, *args: object) -> None:
        self._finish_menu_release()
        connection = self._connection
        if connection is None:
            return
        # Check elapsed BOOTTIME before reading queued ACKs after resume/stall.
        if boot_time() - self._last_reply >= HEALTH_LIMIT_S:
            self._fail()
            return
        try:
            if boot_time() - self._last_ping >= HEARTBEAT_S:
                send_packet(connection, {"op": "ping"})
                self._last_ping = boot_time()
            for _ in range(128):
                if not select.select([connection], [], [], 0)[0]:
                    return
                message = receive_packet(connection)
                kind = message.get("kind")
                if kind == "pong" and set(message) == {"kind"}:
                    self._last_reply = boot_time()
                elif (
                    kind == "reply"
                    and set(message) == {"kind", "id", "value"}
                    and type(message.get("id")) is int
                    and message["id"] == self._sequence
                ):
                    value = message["value"]
                    if self._operation in ("grab", "probe", "escape"):
                        _grab_result(value)
                    elif self._operation == "signature":
                        if value is not None and not (
                            isinstance(value, list)
                            and len(value) == 2
                            and _integer(value[0], 8, 255)
                            and _integer(value[1], 0, 255)
                        ):
                            raise ValueError("invalid broker signature")
                    elif self._operation == "held":
                        if type(value) is not bool:
                            raise ValueError("invalid broker held state")
                    elif value is not None:
                        raise ValueError("invalid broker acknowledgment")
                    self._replies[message["id"]] = message
                elif kind == "key" and set(message) == {"kind", "event"}:
                    self._queue(_key_event(message["event"]))
                elif kind == "mapping" and set(message) == {"kind", "event"}:
                    self._queue(_mapping_event(message["event"]))
                else:
                    raise ValueError("invalid broker reply")
            raise ValueError("broker flood")
        except (OSError, ValueError, TypeError, KeyError):
            self._fail()

    def _request(self, operation: str, **fields: object) -> dict[str, Any]:
        try:
            launched = self._connection is not None or self._launch()
        except OSError:
            launched = False
        if not launched:
            self._fail()
            return {}
        self._sequence += 1
        self._operation = operation
        sequence = self._sequence
        deadline = boot_time() + RPC_LIMIT_S
        try:
            assert self._connection is not None
            send_packet(self._connection, {"op": operation, "id": sequence, **fields})
            while self._connection is not None and boot_time() < deadline:
                self._pump()
                if sequence in self._replies:
                    return self._replies.pop(sequence)
                if self._connection is not None:
                    select.select(
                        [self._connection], [], [], min(0.02, max(0, deadline - boot_time()))
                    )
        except (OSError, ValueError):
            pass
        self._fail()
        return {}

    def combo_signature(self, combo: str) -> tuple[int, int] | None:
        value = self._request("signature", combo=combo).get("value")
        return (
            (int(value[0]), int(value[1])) if isinstance(value, list) and len(value) == 2 else None
        )

    def grab_combo(self, combo: str) -> GrabResult:
        needs_menu = self._manage_menu and combo.lower() in ("super_l", "super_r", "super", "win")
        if needs_menu:
            if not self._acquire_menu():
                return GrabResult(
                    "not-grabbed", owner_hint="Не удалось настроить клавишу меню — повторяем"
                )
            if not self._request("lease", token=self._menu_token):
                return GrabResult("not-grabbed")
        value = self._request("grab", combo=combo, defer=self._defer_super).get("value")
        result = GrabResult(**value) if isinstance(value, dict) else GrabResult("not-grabbed")
        if result.ok:
            self._combo = combo
        elif needs_menu:
            self._fail()
        return result

    def ungrab_combo(self, combo: str) -> GrabResult:
        if self._combo == combo:
            self._combo = None
        if self._connection is not None:
            self._request("ungrab", combo=combo)
        self._events.clear()
        if self._combo is None and self._menu_token is not None:
            self._fail()
        return GrabResult("ok")

    def probe_combo(self, combo: str) -> GrabResult:
        value = self._request("probe", combo=combo).get("value")
        return GrabResult(**value) if isinstance(value, dict) else GrabResult("not-grabbed")

    def cancel_keyboard_grab(self) -> None:
        if self._connection is not None:
            self._request("cancel")

    def key_is_down(self, keycode: int) -> bool:
        return self._request("held", keycode=keycode).get("value") is not False

    def grab_escape(self) -> GrabResult:
        value = self._request("escape").get("value")
        return GrabResult(**value) if isinstance(value, dict) else GrabResult("not-grabbed")

    def ungrab_escape(self) -> None:
        if self._connection is not None:
            self._request("unescape")

    def poll_events(self, timeout: float = 0.0) -> list[HotkeyEvent | MappingEvent]:
        self._pump()
        if timeout and not self._events and self._connection is not None:
            select.select([self._connection], [], [], min(timeout, 0.02))
            self._pump()
        try:
            while os.read(self._read, 4096):
                pass
        except BlockingIOError:
            pass
        events = list(self._events)
        self._events.clear()
        return events

    def fileno(self) -> int:
        return self._read if not self._closed else -1

    def close(self) -> None:
        if self._closed:
            return
        self._combo = None
        self._fail()
        self._closed = True
        if self._menu_thread is not None:
            self._menu_thread.join(timeout=8.0)
        self._finish_menu_release()
        if self._timer is not None:
            self._timer.stop()
            self._timer.deleteLater()
            self._timer = None
        os.close(self._read)
        os.close(self._write)
