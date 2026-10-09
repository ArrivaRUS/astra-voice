"""Minimal Command1 peer, own process/private bus; records no recognized text."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from PyQt5.QtCore import Q_CLASSINFO, QCoreApplication, QObject, QTimer, pyqtSlot
from PyQt5.QtDBus import QDBusAbstractAdaptor, QDBusConnection, QDBusMessage

SERVICE = "ru.astralinux.Cowork"
PATH = "/ru/astralinux/Cowork"
TRACE = Path(sys.argv[1])
MODE = sys.argv[2]


def trace(event: dict[str, Any]) -> None:
    with TRACE.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event) + "\n")


class Adaptor(QDBusAbstractAdaptor):
    Q_CLASSINFO("D-Bus Interface", "ru.astralinux.Cowork.Command1")

    @pyqtSlot(result="QVariantMap")
    def Status(self) -> dict[str, Any]:
        trace({"method": "Status"})
        return {"contract": 1, "state": "ready", "modes": ["submit"], "version": "test"}

    @pyqtSlot(str, "QVariantMap", QDBusMessage, result="QVariantMap")
    def Submit(self, text: str, options: dict[str, Any], message: QDBusMessage) -> dict[str, Any]:
        trace(
            {
                "method": "Submit",
                "source": options.get("source"),
                "contract": options.get("contract"),
                "request_id": options.get("request_id"),
                "sent_mono_ms": options.get("sent_mono_ms"),
                "deadline_mono_ms": options.get("deadline_mono_ms"),
                "sent_boot_ms": options.get("sent_boot_ms"),
                "text_len": len(text),
            }
        )
        message.setDelayedReply(True)
        saved = QDBusMessage(message)
        reply = {"contract": 1, "status": "accepted", "request_id": options["request_id"]}
        if MODE == "busy":
            reply.update(status="busy", reason="rate_limited")
        elif MODE == "bad_echo":
            reply["request_id"] = "f" * 32

        def respond() -> None:
            if MODE in ("Failed", "foreign_error"):
                name = (
                    "org.freedesktop.DBus.Error.Failed"
                    if MODE == "Failed"
                    else "com.example.Error.NoReply"
                )
                bus.send(saved.createErrorReply(name, "PRIVATE_REMOTE_SENTENCE"))
            elif MODE == "malformed":
                bus.send(saved.createReply(["accepted"]))
            else:
                bus.send(saved.createReply([reply]))

        if MODE == "owner_loss":
            bus.unregisterService(SERVICE)
        else:
            QTimer.singleShot(650 if MODE in ("late", "sleep") else 0, respond)
        return {}


assert os.environ["DBUS_SESSION_BUS_ADDRESS"].startswith("unix:")
app = QCoreApplication([])
bus = QDBusConnection.connectToBus(os.environ["DBUS_SESSION_BUS_ADDRESS"], "test-command-peer")
obj = QObject()
adaptor = Adaptor(obj)
assert bus.registerObject(PATH, obj, QDBusConnection.ExportAdaptors)
assert bus.registerService(SERVICE)
TRACE.with_suffix(".ready").write_text("ready", encoding="ascii")
raise SystemExit(app.exec_())
