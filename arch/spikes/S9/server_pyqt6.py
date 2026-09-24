#!/usr/bin/env python3
"""Isolated Qt 6 side of S9. No real command execution."""
import argparse
import hashlib
import os
import re
import sys
import time


def isolated():
    address = os.environ.get("DBUS_SESSION_BUS_ADDRESS", "")
    return (os.environ.get("S9_ISOLATED") == "1" and address.startswith("unix:")
            and address != f"unix:path=/run/user/{os.getuid()}/bus")


if not isolated():
    print("S9 guard: private unix D-Bus session required", file=sys.stderr)
    sys.exit(2)

from PyQt6.QtCore import QCoreApplication, QObject, QTimer, pyqtClassInfo, pyqtSlot
from PyQt6.QtDBus import QDBusAbstractAdaptor, QDBusConnection, QDBusError, QDBusMessage

SERVICE = "ru.astralinux.Cowork"
PATH = "/ru/astralinux/Cowork"
INTERFACE = "ru.astralinux.Cowork.Command1"
INT_MAX = 2**64 - 1

parser = argparse.ArgumentParser()
parser.add_argument("--no-limits", action="store_true")
parser.add_argument("--block-before-ms", type=int, default=0)
parser.add_argument("--block-after-reply-ms", type=int, default=0)
parser.add_argument("--quit-after-reply", action="store_true")
parser.add_argument("--shutdown-after-reply", action="store_true")
parser.add_argument("--exit-after-reply", action="store_true")
parser.add_argument("--echo-ints", action="store_true")
parser.add_argument("--bad-reply", action="store_true")
parser.add_argument("--executor", choices=("never", "instant"), default="instant")
args = parser.parse_args()
app = QCoreApplication(sys.argv)
bus = QDBusConnection.sessionBus()


def ms():
    return time.monotonic_ns() // 1_000_000


def log(*items):
    print(*items, file=sys.stderr, flush=True)


class UIDCallbacks(QObject):
    def __init__(self, parent, finish):
        super().__init__(parent)
        self.finish = finish

    @pyqtSlot(QDBusMessage)
    def ok(self, reply):
        values = reply.arguments()
        try:
            self.finish(int(values[0]))
        except (TypeError, ValueError, IndexError):
            self.finish(error="invalid_uid_reply")

    @pyqtSlot(QDBusError)
    def err(self, error):
        self.finish(error=error.name())


# In PyQt6 the class-body call leaves this classInfo absent; use the decorator.
@pyqtClassInfo("D-Bus Interface", INTERFACE)
class API(QDBusAbstractAdaptor):
    def __init__(self, parent):
        super().__init__(parent)
        self.seen = {}
        self.running = 0
        self.starts = 0
        self.last_accepted = -10**15
        self.pending = {}
        self.shutting_down = False

    @pyqtSlot(result="QVariantMap")
    def Status(self):
        return {"contract": 1, "state": "ready", "starts": self.starts, "running": self.running}

    def reply(self, message, result, accepted=False, text=None):
        if args.bad_reply:
            result = {"broken": True}
        outgoing = message.createReply([result])
        sent = bus.send(outgoing)
        log("REPLY", result.get("status", "bad_reply"), result.get("reason", "none"),
            "signature", outgoing.signature(), "sent", sent, "starts", self.starts)
        if accepted:
            if args.exit_after_reply:
                os._exit(0)
            if args.shutdown_after_reply:
                self.shutting_down = True
                QTimer.singleShot(0, app.quit)
            if args.quit_after_reply:
                QTimer.singleShot(0, app.quit)
            QTimer.singleShot(0, lambda: self.work(text))
        if args.block_after_reply_ms and accepted:
            time.sleep(args.block_after_reply_ms / 1000)

    def work(self, text):
        if self.shutting_down:
            self.running -= 1
            log("WORK_SKIPPED_SHUTDOWN", "running", self.running)
            return
        self.starts += 1
        fingerprint = hashlib.sha256(text.encode()).hexdigest()[:8]
        log("WORK_START", self.starts, "length", len(text), "sha8", fingerprint,
            "running", self.running)
        if args.executor == "instant":
            self.running -= 1
            log("WORK_DONE", self.starts, "running", self.running)

    @pyqtSlot(str, "QVariantMap", QDBusMessage, result="QVariantMap")
    def Submit(self, text, options, message):
        message.setDelayedReply(True)
        if args.block_before_ms:
            time.sleep(args.block_before_ms / 1000)
        result = self.validate(text, options)
        if result:
            self.reply(message, result)
            return {}
        deadline = options["deadline_mono_ms"]
        if ms() > deadline:
            self.reply(message, {"status": "expired", "reason": "deadline"})
            return {}
        limit = min(100, deadline - ms() - 20)
        if limit <= 0:
            self.reply(message, {"status": "refused", "reason": "unverified_sender"})
            return {}
        query = QDBusMessage.createMethodCall("org.freedesktop.DBus", "/org/freedesktop/DBus",
                                                "org.freedesktop.DBus", "GetConnectionUnixUser")
        query.setArguments([message.service()])
        began = time.monotonic_ns()
        done = {"value": False}

        def finish(uid=None, error=None):
            if done["value"]:
                return
            done["value"] = True
            timer.stop()
            self.pending.pop(id(message), None)
            callbacks.deleteLater()
            elapsed = (time.monotonic_ns() - began) / 1_000_000
            log("UID_LOOKUP", f"{elapsed:.3f}", "ms", "uid", uid,
                "error", error or "none", "limit", limit)
            if error or uid != os.getuid():
                self.reply(message, {"status": "refused", "reason": "unverified_sender"})
            else:
                self.admit(text, options, message, uid)

        callbacks = UIDCallbacks(self, finish)
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.timeout.connect(lambda: finish(error="uid_timeout"))
        timer.start(limit)
        self.pending[id(message)] = (message, timer, callbacks)
        called = bus.callWithCallback(query, callbacks.ok, callbacks.err, limit)
        if not called:
            finish(error="callWithCallback_false")
        return {}

    def validate(self, text, options):
        if not isinstance(options, dict):
            return {"status": "refused", "reason": "invalid_options"}
        fields = ("contract", "sent_mono_ms", "deadline_mono_ms")
        if any(type(options.get(field)) is not int or not 0 <= options[field] <= INT_MAX
               for field in fields):
            return {"status": "refused", "reason": "invalid_options"}
        request_id = options.get("request_id")
        if not isinstance(request_id, str) or re.fullmatch(r"[0-9a-fA-F]{32}", request_id) is None:
            return {"status": "refused", "reason": "invalid_options"}
        if options["contract"] > 1:
            return {"status": "refused", "reason": "unsupported_contract"}
        if options["contract"] != 1 or options["deadline_mono_ms"] < options["sent_mono_ms"]:
            return {"status": "refused", "reason": "invalid_options"}
        if options.get("source") != "astra-voice":
            return {"status": "refused", "reason": "bad_source"}
        if options.get("mode", "submit") != "submit":
            return {"status": "refused", "reason": "bad_mode"}
        if (not isinstance(text, str) or not 1 <= len(text) <= 4000 or not text.strip()
                or any(ord(c) < 32 or 127 <= ord(c) <= 159 or c in "\u2028\u2029" for c in text)):
            return {"status": "refused", "reason": "invalid_text"}
        return None

    def admit(self, text, options, message, uid):
        key = options["request_id"]
        fingerprint = hashlib.sha256(text.encode()).hexdigest()
        if key in self.seen:
            if self.seen[key] == fingerprint:
                self.reply(message, {"status": "accepted", "already": True, "uid": uid})
            else:
                self.reply(message, {"status": "refused", "reason": "request_conflict"})
            return
        now = ms()
        if now > options["deadline_mono_ms"]:
            self.reply(message, {"status": "expired", "reason": "deadline"})
        elif not args.no_limits and now - self.last_accepted < 500:
            self.reply(message, {"status": "busy", "reason": "rate_limited"})
        elif not args.no_limits and self.running >= 3:
            self.reply(message, {"status": "busy", "reason": "queue_full"})
        else:
            self.seen[key] = fingerprint
            self.running += 1
            self.last_accepted = now
            result = {"status": "accepted", "already": False, "uid": uid}
            if args.echo_ints:
                result["sent_mono_ms"] = options["sent_mono_ms"]
            self.reply(message, result, accepted=True, text=text)


root = QObject()
api = API(root)
if not bus.isConnected() or not bus.registerObject(PATH, root, QDBusConnection.RegisterOption.ExportAdaptors):
    log("REGISTER_OBJECT_FAILED", bus.lastError().name(), bus.lastError().message())
    sys.exit(1)
if not bus.registerService(SERVICE):
    log("REGISTER_SERVICE_FAILED", bus.lastError().name(), bus.lastError().message())
    sys.exit(1)
print("READY", flush=True)
sys.exit(app.exec())
