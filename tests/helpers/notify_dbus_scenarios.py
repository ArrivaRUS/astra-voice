"""Сценарии Notify на собственной шине без GUI и пользовательских служб."""

from __future__ import annotations

import faulthandler
import json
import logging
import os
import resource
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from time import monotonic, sleep

from PyQt5.QtCore import QCoreApplication
from PyQt5.QtDBus import QDBusConnection, QDBusMessage

from astra_voice.ui import notify


class Records(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def pump_until(predicate: Callable[[], bool], timeout: float = 4) -> None:
    deadline = monotonic() + timeout
    while not predicate():
        assert monotonic() < deadline, "истёк срок ожидания ответа D-Bus"
        QCoreApplication.processEvents()
        sleep(0.005)


def pump_for(duration: float) -> None:
    deadline = monotonic() + duration
    while monotonic() < deadline:
        QCoreApplication.processEvents()
        sleep(0.005)


def dbus_call(method: str, argument: str | None = None) -> QDBusMessage:
    request = QDBusMessage.createMethodCall(
        "org.freedesktop.DBus",
        "/org/freedesktop/DBus",
        "org.freedesktop.DBus",
        method,
    )
    if argument is not None:
        request.setArguments([argument])
    return QDBusConnection.sessionBus().call(request)


def assert_server_owns_name(server: subprocess.Popen[str]) -> None:
    owner = dbus_call("GetNameOwner", "org.freedesktop.Notifications")
    assert owner.type() == QDBusMessage.ReplyMessage, owner.errorName()
    assert owner.signature() == "s", owner.signature()
    process = dbus_call("GetConnectionUnixProcessID", owner.arguments()[0])
    assert process.type() == QDBusMessage.ReplyMessage, process.errorName()
    assert process.signature() == "u", process.signature()
    assert process.arguments() == [server.pid], (process.arguments(), server.pid)


def assert_no_owner_error() -> None:
    owner = dbus_call("GetNameOwner", "org.freedesktop.Notifications")
    assert owner.type() == QDBusMessage.ErrorMessage
    assert owner.errorName() == "org.freedesktop.DBus.Error.NameHasNoOwner"
    request = QDBusMessage.createMethodCall(
        "org.freedesktop.Notifications",
        "/org/freedesktop/Notifications",
        "org.freedesktop.Notifications",
        "Notify",
    )
    reply = QDBusConnection.sessionBus().call(request)
    assert reply.type() == QDBusMessage.ErrorMessage
    assert reply.errorName() in {
        "org.freedesktop.DBus.Error.ServiceUnknown",
        "org.freedesktop.DBus.Error.NameHasNoOwner",
    }, reply.errorName()


def events(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def shown(path: Path) -> list[dict[str, object]]:
    return [event for event in events(path) if event["event"] == "shown"]


def start_server(
    path: Path, commands: Path, delay: int, mode: str = "normal"
) -> subprocess.Popen[str]:
    server = subprocess.Popen(
        [
            sys.executable,
            str(Path(__file__).with_name("notify_fake_server.py")),
            str(path),
            str(commands),
            str(delay),
            mode,
        ],
        stdout=subprocess.DEVNULL,
        stderr=None,
        text=True,
    )
    try:
        pump_until(lambda: Path(str(path) + ".ready").exists() or server.poll() is not None)
        assert server.poll() is None, f"fake server exited with rc={server.returncode}"
        assert_server_owns_name(server)
    except Exception:
        if server.poll() is None:
            server.terminate()
            try:
                server.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                server.kill()
                server.communicate()
        raise
    return server


def main() -> int:
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    faulthandler.enable()
    assert os.environ.get("DBUS_SESSION_BUS_ADDRESS", "").startswith("unix:")
    scenario = sys.argv[1]
    app = QCoreApplication([])
    assert app is QCoreApplication.instance()
    notify.reset_state()
    records = Records()
    logger = logging.getLogger(notify.__name__)
    logger.addHandler(records)
    logger.setLevel(logging.DEBUG)
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "events.jsonl"
        commands = Path(directory) / "commands.jsonl"
        server: subprocess.Popen[str] | None = None
        try:
            if scenario != "no_owner":
                delay = 300 if scenario == "timeouts" else 600
                if scenario == "late_beyond_wait":
                    delay = 2000
                server = start_server(
                    path,
                    commands,
                    delay,
                    "invalid" if scenario == "invalid_args" else "normal",
                )
            if scenario == "late_reply":
                notify.notify("Поздний ответ", "Секретное тело")
                pump_until(lambda: notify.last_delivery_ok())
                assert notify.pending_count() == 0
                assert notify.flush_pending() == 0
                pump_for(0.2)
                assert len(shown(path)) == 1
                assert not [r for r in records.records if r.levelno >= logging.WARNING]
            elif scenario == "late_reply_then_next":
                late_called: list[int] = []
                notify.set_action_handler("details", lambda: late_called.append(1))
                actions = [("details", "Подробности")]
                notify.notify("Первое с действием", actions=actions)
                pump_until(lambda: notify.last_delivery_ok())
                late_id = shown(path)[0]["id"]
                notify.notify("Второе с действием", actions=actions)
                pump_until(lambda: len(shown(path)) == 2)
                pump_until(lambda: notify._in_flight_seq is None)
                assert shown(path)[1]["replaces_id"] == late_id
                with commands.open("a", encoding="utf-8") as stream:
                    for _ in range(2):
                        stream.write(
                            json.dumps({"event": "action", "id": late_id, "key": "details"}) + "\n"
                        )
                pump_until(lambda: len(late_called) == 1)
                pump_for(0.2)
                assert late_called == [1]
            elif scenario == "late_beyond_wait":
                late_hits: list[str] = []
                notify.set_action_handler("a", lambda: late_hits.append("a"))
                notify.set_action_handler("b", lambda: late_hits.append("b"))
                started = monotonic()
                notify.notify("A", actions=[("a", "A")])
                notify.notify("B", actions=[("b", "B")])
                pump_until(lambda: len(shown(path)) == 2, 5)
                assert monotonic() - started >= notify._WAIT_FOR_ID_MS / 1000
                assert [
                    (event["summary"], event["id"], event["replaces_id"]) for event in shown(path)
                ] == [("A", 100, 0), ("B", 101, 0)]
                pump_until(lambda: not notify._sent, 6)
                assert notify._last_id == 101
                with commands.open("a", encoding="utf-8") as stream:
                    for action_id, key in ((100, "a"), (101, "b")):
                        for _ in range(2):
                            stream.write(
                                json.dumps({"event": "action", "id": action_id, "key": key}) + "\n"
                            )
                pump_until(lambda: len(late_hits) == 2)
                pump_for(0.2)
                assert late_hits == ["a", "b"]
            elif scenario == "action":
                called: list[int] = []
                notify.set_action_handler("details", lambda: called.append(1))
                notify.notify("Действие", "Секретное тело", actions=[("details", "Подробности")])
                pump_until(lambda: notify.last_delivery_ok())
                notification_id = shown(path)[0]["id"]
                with commands.open("a", encoding="utf-8") as stream:
                    stream.write(
                        json.dumps({"event": "action", "id": notification_id, "key": "details"})
                        + "\n"
                    )
                pump_until(lambda: len(called) == 1)
                assert called == [1]
            elif scenario == "replacement":
                notify.notify("Первое")
                pump_until(lambda: notify.last_delivery_ok())
                first_id = shown(path)[0]["id"]
                notify.notify("Второе")
                pump_until(lambda: len(shown(path)) == 2)
                assert shown(path)[1]["replaces_id"] == first_id
            elif scenario == "no_owner":
                assert_no_owner_error()
                notify.notify("Отложенное")
                pump_until(lambda: notify.pending_count() == 1)
                server = start_server(path, commands, 0)
                assert notify.flush_pending() == 1
                pump_until(lambda: notify.last_delivery_ok())
                assert len(shown(path)) == 1
                assert notify.pending_count() == 0
            elif scenario == "invalid_args":
                notify.notify("Неверный вызов", "Секретное тело")
                pump_until(lambda: bool(records.records))
                pump_until(lambda: notify._in_flight_seq is None)
                assert notify.pending_count() == 0
                assert any(
                    "org.freedesktop.DBus.Error.InvalidArgs" in r.getMessage()
                    for r in records.records
                    if r.levelno >= logging.WARNING
                )
            elif scenario == "fast_return":
                notify.notify("Прогрев подписок")
                pump_until(lambda: not notify._sent)
                assert notify.last_delivery_ok()
                start = monotonic()
                notify.notify("Быстрый возврат")
                assert (monotonic() - start) < 0.050
                pump_until(lambda: not notify._sent)
                assert notify.last_delivery_ok()
            elif scenario == "timeouts":
                notify._CALL_TIMEOUT_MS = 60
                for index in range(3):
                    notify.notify(f"Таймаут {index}")
                    pump_until(lambda: notify._in_flight_seq is None, 3)
                assert notify.pending_count() == 0
                assert len([r for r in records.records if r.levelno == logging.WARNING]) == 1
            elif scenario == "slot":
                notify.notify("A")
                notify.notify("B")
                notify.notify("C")
                pump_until(lambda: len(shown(path)) == 2)
                assert [e["summary"] for e in shown(path)] == ["A", "C"]
                assert shown(path)[1]["replaces_id"] == shown(path)[0]["id"]
            else:
                raise ValueError(scenario)
            assert "Секретное тело" not in "\n".join(r.getMessage() for r in records.records)
            assert "errorMessage" not in "\n".join(r.getMessage() for r in records.records)
        finally:
            notify.reset_state()
            if server is not None:
                server_alive = server.poll() is None
                if server_alive:
                    server.terminate()
                    try:
                        server.communicate(timeout=2)
                    except subprocess.TimeoutExpired:
                        server.kill()
                        server.communicate()
                assert server_alive, f"fake server crashed with rc={server.returncode}"
    print(f"NOTIFY_DBUS_OK:{scenario}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
