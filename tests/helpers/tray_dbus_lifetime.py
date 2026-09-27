"""Подпроцессные сценарии QtDBus с собственной сессионной шиной."""

from __future__ import annotations

import faulthandler
import os
import resource
import select
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from time import monotonic, sleep

from PyQt5.QtCore import QCoreApplication, QEvent, QTimer
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


def plasma_reply() -> None:
    owner = subprocess.Popen(
        [sys.executable, str(Path(__file__).with_name("tray_dbus_owner.py"))],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert owner.stdout is not None
        ready, _, _ = select.select([owner.stdout], [], [], 5)
        assert ready and owner.stdout.readline().strip() == "PLASMA_OWNER_READY"
        app = QApplication([])
        tray = Tray(IconProvider())  # type: ignore[arg-type]
        deadline = monotonic() + 20
        cycles = 0
        waiting_for_close = False

        def poll() -> None:
            nonlocal cycles, waiting_for_close
            if monotonic() >= deadline:
                app.exit(2)
                return
            if waiting_for_close:
                if tray._worker is None and not tray_module._bus_threads:
                    waiting_for_close = False
                    cycles += 1
                    if cycles == 7:
                        app.quit()
                    else:
                        tray.start()
            elif tray._plasma_known:
                assert tray._plasma_alive
                tray.stop()
                waiting_for_close = True

        timer = QTimer()
        timer.setInterval(5)
        timer.timeout.connect(poll)
        tray.start()
        timer.start()
        try:
            assert app.exec_() == 0, "не дождались ответа plasmashell"
            assert cycles == 7
        finally:
            timer.stop()
            tray.stop()
        pump_until(lambda: not tray_module._bus_threads, monotonic() + 2)
    finally:
        owner.terminate()
        try:
            owner.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            owner.kill()
            owner.communicate(timeout=3)


def thread_cleanup() -> None:
    app = QApplication([])
    deadline = monotonic() + 20
    cycles = 0
    tray: Tray | None = None

    def step() -> None:
        nonlocal cycles, tray
        if monotonic() >= deadline:
            app.exit(2)
            return
        if tray is not None:
            tray.stop()
            tray.deleteLater()
            tray = None
            QTimer.singleShot(100, step)
        elif cycles < 20:
            cycles += 1
            tray = Tray(IconProvider())  # type: ignore[arg-type]
            tray.start()
            QTimer.singleShot(300, step)
        elif not tray_module._bus_threads:
            app.quit()
        else:
            QTimer.singleShot(10, step)

    QTimer.singleShot(0, step)
    result = app.exec_()
    assert result == 0, (
        f"thread_cleanup: истёк срок ожидания после {cycles} циклов; "
        f"осталось потоков: {len(tray_module._bus_threads)}"
    )
    assert cycles == 20 and not tray_module._bus_threads


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
    address = os.environ.get("DBUS_SESSION_BUS_ADDRESS")
    assert address and address != "unix:path=/nonexistent", "нужна изолированная сессионная шина"
    scenario = sys.argv[1]
    if scenario == "plasma_reply":
        plasma_reply()
    elif scenario == "thread_cleanup":
        thread_cleanup()
    elif scenario == "exit_after_stop":
        exit_after_stop()
    else:
        raise ValueError(scenario)
    print(f"TRAY_DBUS_OK:{scenario}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
