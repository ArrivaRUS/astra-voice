"""Подпроцессные сценарии QtDBus с собственной сессионной шиной."""

from __future__ import annotations

import faulthandler
import gc
import os
import resource
import select
import subprocess
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from time import monotonic, sleep

from PyQt5 import sip
from PyQt5.QtCore import QCoreApplication, QEvent, QTimer
from PyQt5.QtDBus import QDBusConnection
from PyQt5.QtGui import QIcon
from PyQt5.QtWidgets import QApplication

from astra_voice.ui import tray as tray_module
from astra_voice.ui.tray import Tray
from astra_voice.ui.tray_icons import TrayState


class IconProvider:
    def icon(self, state: TrayState) -> QIcon:
        return QIcon()

    def has_icon(self, state: TrayState) -> bool:
        return True

    def tooltip(self, state: TrayState, *, hotkey: str) -> str:
        return "Astra Voice test"


_exit_app: QApplication | None = None
_exit_tray: Tray | None = None


def pump_until(predicate: Callable[[], bool], deadline: float) -> None:
    while not predicate():
        assert monotonic() < deadline, "истёк срок ожидания Qt-событий"
        QCoreApplication.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        sleep(0.005)


def wait_for_subscriptions(tray: Tray, deadline: float) -> None:
    pump_until(lambda: tray._subscriptions_ready, deadline)


def plasma_reply() -> None:
    owner = subprocess.Popen(
        [sys.executable, str(Path(__file__).with_name("tray_dbus_owner.py")), str(os.getpid())],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert owner.stdout is not None
        ready, _, _ = select.select([owner.stdout], [], [], 5)
        assert ready and owner.stdout.readline().strip() == "PLASMA_OWNER_READY"
        global _exit_app
        app = QApplication([])
        _exit_app = app
        tray = Tray(IconProvider())  # type: ignore[arg-type]
        target_cycles = int(os.environ.get("ASTRA_VOICE_TRAY_CYCLES", "300"))
        deadline = monotonic() + max(20, target_cycles * 0.08)
        cycles = 0
        timer = QTimer()
        timer.setInterval(5)

        def poll() -> None:
            nonlocal cycles
            if monotonic() >= deadline:
                app.exit(2)
            elif tray._plasma_known:
                assert tray._plasma_alive
                tray.stop()
                cycles += 1
                faulthandler.dump_traceback_later(25, exit=True)
                if cycles % 100 == 0:
                    print(f"plasma_reply: {cycles}/{target_cycles}", flush=True)
                if cycles == target_cycles:
                    app.quit()
                else:
                    tray.start()

        timer.timeout.connect(poll)
        try:
            faulthandler.dump_traceback_later(25, exit=True)
            tray.start()
            timer.start()
            assert app.exec_() == 0, "не дождались ответа plasmashell"
            assert cycles == target_cycles
        finally:
            timer.stop()
            tray.stop()
            tray_module.shutdown_bus_threads()
    finally:
        owner.terminate()
        try:
            owner.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            owner.kill()
            owner.communicate(timeout=3)


def tray_threads() -> list[threading.Thread]:
    return [thread for thread in threading.enumerate() if thread.name == "astra-voice-tray-dbus"]


def thread_cleanup() -> None:
    global _exit_app
    _exit_app = QApplication([])
    receiver = None
    for _ in range(20):
        faulthandler.dump_traceback_later(25, exit=True)
        tray = Tray(IconProvider())  # type: ignore[arg-type]
        tray.start()
        wait_for_subscriptions(tray, monotonic() + 5)
        if receiver is None:
            receiver = tray_module._bus_receiver
        assert receiver is tray_module._bus_receiver
        tray.stop()
        tray.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    assert len(tray_threads()) == 1
    assert receiver is not None and not sip.isdeleted(receiver)
    tray_module.shutdown_bus_threads()
    assert not tray_threads()


def invariants() -> None:
    global _exit_app
    _exit_app = QApplication([])
    original = QDBusConnection
    connects = 0
    disconnects = 0

    class CountingConnection:
        SessionBus = original.SessionBus

        @staticmethod
        def connectToBus(bus_type: QDBusConnection.BusType, name: str) -> QDBusConnection:
            nonlocal connects
            connects += 1
            return original.connectToBus(bus_type, name)

        @staticmethod
        def disconnectFromBus(name: str) -> None:
            nonlocal disconnects
            disconnects += 1
            original.disconnectFromBus(name)

    tray_module.QDBusConnection = CountingConnection  # type: ignore[attr-defined]
    tray = Tray(IconProvider())  # type: ignore[arg-type]
    receiver = None
    baseline_python = baseline_native = 0
    cycles = int(os.environ.get("ASTRA_VOICE_TRAY_CYCLES", "200"))
    deadline = monotonic() + max(20, cycles * 0.08)
    try:
        for cycle in range(cycles + 5):
            faulthandler.dump_traceback_later(25, exit=True)
            if cycle >= cycles:
                tray.deleteLater()
                tray = Tray(IconProvider())  # type: ignore[arg-type]
            tray.start()
            wait_for_subscriptions(tray, deadline)
            tray.stop()
            QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
            if receiver is None:
                receiver = tray_module._bus_receiver
                baseline_python = len(threading.enumerate())
                baseline_native = len(os.listdir("/proc/self/task"))
            assert receiver is tray_module._bus_receiver
            assert receiver is not None and not sip.isdeleted(receiver)
            assert len(tray_threads()) == 1
            assert len(threading.enumerate()) <= baseline_python
            assert len(os.listdir("/proc/self/task")) <= baseline_native
            assert connects == 1 and disconnects == 0
            if (cycle + 1) % 100 == 0:
                print(f"invariants: {cycle + 1}/{cycles + 5}", flush=True)
    finally:
        tray.stop()
        tray_module.shutdown_bus_threads()
        tray_module.QDBusConnection = original  # type: ignore[attr-defined]
    assert connects == 1 and disconnects == 0


def exit_after_stop() -> None:
    global _exit_app, _exit_tray
    _exit_app = QApplication([])
    tray = Tray(IconProvider())  # type: ignore[arg-type]
    _exit_tray = tray
    tray.start()
    deadline = monotonic() + 5
    while not tray._subscriptions_ready and monotonic() < deadline:
        QCoreApplication.processEvents()
    assert tray._subscriptions_ready, "не дождались подписок D-Bus"
    tray.stop()
    tray_module.shutdown_bus_threads()
    print("TRAY_DBUS_OK:exit_after_stop", flush=True)
    sys.exit(0)


def main() -> int:
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    faulthandler.enable()
    faulthandler.dump_traceback_later(25, exit=True)
    if interval := os.environ.get("ASTRA_VOICE_TRAY_SWITCH"):
        sys.setswitchinterval(float(interval))
    if threshold := os.environ.get("ASTRA_VOICE_TRAY_GC"):
        gc.set_threshold(int(threshold), 2, 2)
    address = os.environ.get("DBUS_SESSION_BUS_ADDRESS")
    assert address and address != "unix:path=/nonexistent", "нужна изолированная сессионная шина"
    scenario = sys.argv[1]
    try:
        if scenario == "plasma_reply":
            plasma_reply()
        elif scenario == "invariants":
            invariants()
        elif scenario == "thread_cleanup":
            thread_cleanup()
        elif scenario == "exit_after_stop":
            exit_after_stop()
        else:
            raise ValueError(scenario)
        print(f"TRAY_DBUS_OK:{scenario}", flush=True)
        return 0
    finally:
        faulthandler.cancel_dump_traceback_later()


if __name__ == "__main__":
    raise SystemExit(main())
