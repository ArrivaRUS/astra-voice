"""Отдельный владелец Notifications на изолированной тестовой шине."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from PyQt5.QtCore import QCoreApplication, QObject, QTimer, QVariant, pyqtSlot
from PyQt5.QtDBus import QDBusConnection, QDBusMessage


class Notifications(QObject):
    def __init__(self, log_path: Path, commands: Path, delay_ms: int) -> None:
        super().__init__()
        self.log_path = log_path
        self.commands = commands
        self.delay_ms = delay_ms
        self.next_id = 100
        self.next_reply = 0
        self.pending_replies: dict[int, QDBusMessage] = {}
        self.command_pos = 0
        self.bus = QDBusConnection.sessionBus()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.read_commands)
        self.timer.start(10)

    def log(self, event: dict[str, object]) -> None:
        with self.log_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, ensure_ascii=False) + "\n")

    @pyqtSlot(str, "uint", str, str, str, "QStringList", "QVariantMap", int, QDBusMessage)
    def Notify(
        self,
        app: str,
        replaces_id: int,
        icon: str,
        summary: str,
        body: str,
        actions: list[str],
        hints: dict[str, object],
        expire: int,
        message: QDBusMessage,
    ) -> None:
        del app, icon, body, hints, expire
        notification_id = replaces_id or self.next_id
        if not replaces_id:
            self.next_id += 1
        self.log(
            {
                "event": "shown",
                "id": notification_id,
                "replaces_id": replaces_id,
                "summary": summary,
                "actions": actions,
            }
        )
        message.setDelayedReply(True)
        value = QVariant(notification_id)
        value.convert(QVariant.UInt)
        self.next_reply += 1
        reply_key = self.next_reply
        self.pending_replies[reply_key] = QDBusMessage(message.createReply(value))

        def answer() -> None:
            assert self.bus.send(self.pending_replies.pop(reply_key))

        QTimer.singleShot(self.delay_ms, answer)

    def read_commands(self) -> None:
        if not self.commands.exists():
            return
        with self.commands.open(encoding="utf-8") as stream:
            stream.seek(self.command_pos)
            lines = stream.readlines()
            self.command_pos = stream.tell()
        for line in lines:
            command = json.loads(line)
            if command["event"] == "action":
                signal = QDBusMessage.createSignal(
                    "/org/freedesktop/Notifications",
                    "org.freedesktop.Notifications",
                    "ActionInvoked",
                )
                notification_id = QVariant(command["id"])
                notification_id.convert(QVariant.UInt)
                signal.setArguments([notification_id, command["key"]])
                assert self.bus.send(signal)


class InvalidNotifications(QObject):
    @pyqtSlot(str, "uint", str, str, str, "QStringList", "QVariantMap", int, QDBusMessage)
    def Notify(
        self,
        app: str,
        replaces_id: int,
        icon: str,
        summary: str,
        body: str,
        actions: list[str],
        hints: dict[str, object],
        expire: int,
        message: QDBusMessage,
    ) -> None:
        del app, replaces_id, icon, summary, body, actions, hints, expire
        message.setDelayedReply(True)
        assert QDBusConnection.sessionBus().send(
            message.createErrorReply("org.freedesktop.DBus.Error.InvalidArgs", "Секретное тело")
        )


def main() -> int:
    log_path, commands, delay, mode = sys.argv[1:]
    app = QCoreApplication([])
    bus = QDBusConnection.sessionBus()
    assert bus.isConnected()
    server = (
        InvalidNotifications()
        if mode == "invalid"
        else Notifications(Path(log_path), Path(commands), int(delay))
    )
    assert bus.registerObject(
        "/org/freedesktop/Notifications",
        "org.freedesktop.Notifications",
        server,
        QDBusConnection.ExportAllSlots,
    )
    assert bus.registerService("org.freedesktop.Notifications")
    Path(log_path + ".ready").touch()
    return int(app.exec_())


if __name__ == "__main__":
    raise SystemExit(main())
