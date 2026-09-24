#!/usr/bin/env python3
"""Isolated asynchronous Qt 5 client for S9."""
import argparse
import json
import os
import statistics
import sys
import time
import uuid


def isolated():
    address = os.environ.get("DBUS_SESSION_BUS_ADDRESS", "")
    return (os.environ.get("S9_ISOLATED") == "1" and address.startswith("unix:")
            and address != f"unix:path=/run/user/{os.getuid()}/bus")


if not isolated():
    print("S9 guard: private unix D-Bus session required", file=sys.stderr)
    sys.exit(2)

from PyQt5.QtCore import QCoreApplication, QEventLoop, QMetaType, QObject, QTimer, pyqtSlot
from PyQt5.QtDBus import QDBusArgument, QDBusConnection, QDBusError, QDBusMessage, QDBusVariant

SERVICE = "ru.astralinux.Cowork"
PATH = "/ru/astralinux/Cowork"
INTERFACE = "ru.astralinux.Cowork.Command1"
parser = argparse.ArgumentParser()
parser.add_argument("scenario")
args = parser.parse_args()
app = QCoreApplication(sys.argv)
bus = QDBusConnection.sessionBus()


def typed(value, kind):
    ids = {"i": QMetaType.Int, "u": QMetaType.UInt,
           "x": QMetaType.LongLong, "t": QMetaType.ULongLong}
    if kind == "plain":
        return value
    return QDBusArgument(value, ids[kind])


def plain(value):
    if isinstance(value, QDBusVariant):
        return plain(value.variant())
    if isinstance(value, QDBusArgument):
        try:
            return plain(value.asVariant())
        except Exception:
            return repr(value)
    if isinstance(value, dict):
        return {str(k): plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    return value


class Callbacks(QObject):
    def __init__(self, loop):
        super().__init__()
        self.loop = loop
        self.terminal = []

    @pyqtSlot(QDBusMessage)
    def ok(self, reply):
        self.terminal.append(("ok", reply))
        self.loop.quit()

    @pyqtSlot(QDBusError)
    def err(self, error):
        self.terminal.append(("err", error))
        self.loop.quit()


def call(method="Submit", options=None, command="probe", interface=INTERFACE,
         budget=300, kind="plain", linger_ms=0):
    start_mono = time.monotonic_ns() // 1_000_000
    if options is None:
        options = {"source": "astra-voice", "contract": 1,
                   "request_id": uuid.uuid4().hex,
                   "sent_mono_ms": start_mono,
                   "deadline_mono_ms": start_mono + budget}
    options = dict(options)
    if method == "Submit":
        for key in ("sent_mono_ms", "deadline_mono_ms"):
            if type(options.get(key)) is int:
                options[key] = typed(options[key], kind)
    msg = QDBusMessage.createMethodCall(SERVICE, PATH, interface, method)
    msg.setAutoStartService(False)
    msg.setArguments([command, options] if method == "Submit" else [])
    loop = QEventLoop()
    callbacks = Callbacks(loop)
    terminal = callbacks.terminal
    started = time.monotonic_ns()
    sent = bus.callWithCallback(msg, callbacks.ok, callbacks.err, budget)
    guard = QTimer()
    guard.setSingleShot(True)
    guard.timeout.connect(loop.quit)
    guard.start(budget + 650)
    if not terminal:
        loop.exec()
    guard.stop()
    elapsed = (time.monotonic_ns() - started) / 1_000_000
    if linger_ms:
        late = QEventLoop()
        QTimer.singleShot(linger_ms, late.quit)
        late.exec()
    result = {"method": method, "sent": sent, "callbacks": len(terminal),
              "elapsed_ms": round(elapsed, 3), "error_name": "", "error_type": ""}
    if terminal:
        which, payload = terminal[0]
        if which == "err":
            result["error_name"] = payload.name()
            result["error_type"] = str(payload.type())
            suffix = result["error_name"].rsplit(".", 1)[-1]
            result["outcome"] = {"ServiceUnknown": "not_running",
                                 "NameHasNoOwner": "not_running",
                                 "NoReply": "unknown/NoReply",
                                 "UnknownInterface": "no_contract/UnknownInterface",
                                 "UnknownMethod": "no_contract/UnknownMethod"}.get(suffix, "unknown/error")
        else:
            if hasattr(payload, "arguments"):
                values = payload.arguments()
                result["signature"] = payload.signature()
            else:
                values = [payload]
                result["signature"] = "callback_value"
            value = plain(values[0]) if len(values) == 1 else None
            result["reply"] = value
            if method == "Status":
                result["outcome"] = "ready" if isinstance(value, dict) and value.get("state") == "ready" else "unknown/bad_reply"
            elif isinstance(value, dict) and value.get("status") in ("accepted", "busy", "refused", "expired"):
                result["outcome"] = value["status"]
            else:
                result["outcome"] = "unknown/bad_reply"
    else:
        result["outcome"] = "unknown/no_callback"
    print("CALL", json.dumps(result, ensure_ascii=False, sort_keys=True), flush=True)
    return result


def check(name, expected, actual, more=True):
    ok = expected == actual and more
    print(f"SCENARIO {name} EXPECTED {expected} RESULT {actual} {'PASS' if ok else 'FAIL'}", flush=True)
    return ok


def percentile(values, pct):
    ordered = sorted(values)
    at = (len(ordered) - 1) * pct / 100
    low = int(at)
    return round(ordered[low] + (ordered[min(low + 1, len(ordered) - 1)] - ordered[low]) * (at - low), 3)


def main():
    s = args.scenario
    good = True
    if s == "1a":
        r = call(method="Status")
        good = check(s, "ready", r["outcome"], r["callbacks"] == 1 and r.get("signature") == "a{sv}")
    elif s == "1b":
        r = call()
        good = check(s, "accepted", r["outcome"], r["callbacks"] == 1 and r.get("signature") == "a{sv}" and r.get("reply", {}).get("uid") == os.getuid())
    elif s == "1c":
        for kind in ("i", "u", "x", "t", "plain"):
            r = call(kind=kind)
            good &= check(f"1c_{kind}", "accepted", r["outcome"], r["callbacks"] == 1)
        big = 2**31 + 5
        big_options = {"source": "astra-voice", "contract": 1,
                       "request_id": uuid.uuid4().hex,
                       "sent_mono_ms": big, "deadline_mono_ms": big + 300}
        try:
            r = call(options=big_options, command="probe_plain_big")
            echoed = r.get("reply", {}).get("sent_mono_ms")
            print("INT_OBSERVATION 1c_plain_big", json.dumps({"sent": big, "echoed": echoed,
                                                               "reply": r.get("reply"),
                                                               "outcome": r["outcome"]}, sort_keys=True), flush=True)
            good &= check("1c_plain_big", "accepted/echo=2147483653",
                          f'{r["outcome"]}/echo={echoed}', r["callbacks"] == 1)
        except Exception as exc:
            print("EXCEPTION 1c_plain_big", type(exc).__name__, str(exc), flush=True)
            good &= check("1c_plain_big", "accepted/echo=2147483653",
                          f"exception/{type(exc).__name__}")
        big_options["request_id"] = uuid.uuid4().hex
        try:
            r = call(options=big_options, command="probe_i_big", kind="i")
            print("INT_OBSERVATION 1c_i_big", json.dumps({"sent": big,
                                                           "reply": r.get("reply"),
                                                           "outcome": r["outcome"],
                                                           "callbacks": r["callbacks"]}, sort_keys=True), flush=True)
            good &= check("1c_i_big", "one callback or exception",
                          "one callback or exception" if r["callbacks"] == 1 else f'{r["callbacks"]} callbacks',
                          r["callbacks"] == 1)
        except Exception as exc:
            print("EXCEPTION 1c_i_big", type(exc).__name__, str(exc), flush=True)
            good &= check("1c_i_big", "one callback or exception", "one callback or exception")
        for kind, bad in (("bool", True), ("float", 1.5)):
            now = time.monotonic_ns() // 1_000_000
            options = {"source": "astra-voice", "contract": 1, "request_id": uuid.uuid4().hex,
                       "sent_mono_ms": bad, "deadline_mono_ms": now + 300}
            r = call(options=options)
            good &= check(f"1c_{kind}", "refused/invalid_options", r["outcome"] + "/" + r.get("reply", {}).get("reason", ""), r["callbacks"] == 1)
    elif s == "1d":
        r = call(command="line\nbreak")
        good = check(s, "refused/invalid_text", r["outcome"] + "/" + r.get("reply", {}).get("reason", ""), r["callbacks"] == 1)
    elif s in ("2a", "2b", "2c", "2d", "2e", "4a", "4b",
               "3e_quit", "3e_guard", "3e_exit"):
        kw = {}
        if s == "2b":
            kw["interface"] = "ru.astralinux.Cowork.Unknown1"
        if s == "2c":
            kw["method"] = "Unknown"
        if s in ("2e", "4a"):
            kw["linger_ms"] = 450
        r = call(**kw)
        # With auto-start disabled, this bus returned NameHasNoOwner for an absent
        # service; ServiceUnknown has the same proved-not-running meaning in V1.
        expected = {"2a": "not_running", "2b": "no_contract/UnknownInterface",
                    "2c": "no_contract/UnknownMethod", "2d": "unknown/bad_reply",
                    "2e": "unknown/NoReply", "4a": "unknown/NoReply",
                    "4b": "accepted", "3e_quit": "accepted",
                    "3e_guard": "accepted", "3e_exit": "observed"}[s]
        if s in ("3e_quit", "3e_exit"):
            print(f"OBSERVED {s} outcome={r['outcome']} error={r['error_name']}", flush=True)
            good = check(s, "one callback; outcome observed", "one callback; outcome observed",
                         r["callbacks"] == 1)
        else:
            good = check(s, expected, r["outcome"],
                         r["callbacks"] == 1 and (s != "4b" or r["elapsed_ms"] < 300))
    elif s == "3a":
        key = uuid.uuid4().hex
        def option():
            now = time.monotonic_ns() // 1_000_000
            return {"source": "astra-voice", "contract": 1, "request_id": key,
                    "sent_mono_ms": now, "deadline_mono_ms": now + 300}
        replies = [call(options=option())]
        time.sleep(0.1)
        replies.append(call(options=option()))
        time.sleep(0.25)
        replies.append(call(options=option()))
        good = check(s, "accepted,accepted/already,accepted/already",
                     ",".join(x["outcome"] + ("/already" if x.get("reply", {}).get("already") else "") for x in replies),
                     all(x["callbacks"] == 1 for x in replies))
    elif s == "3b":
        key = uuid.uuid4().hex
        def option():
            now = time.monotonic_ns() // 1_000_000
            return {"source": "astra-voice", "contract": 1, "request_id": key,
                    "sent_mono_ms": now, "deadline_mono_ms": now + 300}
        first = call(options=option())
        second = call(options=option(), command="other")
        good = check(s, "refused/request_conflict", second["outcome"] + "/" + second.get("reply", {}).get("reason", ""), first["outcome"] == "accepted" and second["callbacks"] == 1)
    elif s == "3c":
        first, second = call(), call()
        good = check(s, "busy/rate_limited", second["outcome"] + "/" + second.get("reply", {}).get("reason", ""), first["outcome"] == "accepted" and second["callbacks"] == 1)
    elif s == "3d":
        results = []
        for i in range(33):
            if i:
                time.sleep(0.5)
            results.append(call())
        tally = {"accepted": sum(x["outcome"] == "accepted" for x in results),
                 "queue_full": sum(x["outcome"] == "busy" and x.get("reply", {}).get("reason") == "queue_full" for x in results),
                 "callbacks": sum(x["callbacks"] for x in results)}
        print("QUEUE_TALLY", json.dumps(tally, sort_keys=True), flush=True)
        good = check(s, "3/30/33", f'{tally["accepted"]}/{tally["queue_full"]}/{tally["callbacks"]}')
    elif s == "5":
        submits = [call() for _ in range(100)]
        statuses = [call(method="Status") for _ in range(100)]
        submit = [r["elapsed_ms"] for r in submits]
        status = [r["elapsed_ms"] for r in statuses]
        print("MEASURE Submit", json.dumps({"n": 100, "p50_ms": percentile(submit, 50),
                                            "p95_ms": percentile(submit, 95), "max_ms": round(max(submit), 3)}), flush=True)
        print("MEASURE Status", json.dumps({"n": 100, "p50_ms": percentile(status, 50),
                                            "p95_ms": percentile(status, 95), "max_ms": round(max(status), 3)}), flush=True)
        good = check(s, "100+100 callbacks",
                     f'{sum(r["callbacks"] for r in submits)}+'
                     f'{sum(r["callbacks"] for r in statuses)} callbacks',
                     all(r["callbacks"] == 1 and r["outcome"] == "accepted" for r in submits)
                     and all(r["callbacks"] == 1 and r["outcome"] == "ready" for r in statuses))
    else:
        parser.error(s)
    return 0 if good else 1


sys.exit(main())
