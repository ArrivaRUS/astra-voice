"""Владелец имени plasmashell для изолированного D-Bus-сценария."""

from __future__ import annotations

import ctypes
import os
import resource
import signal
import sys

from PyQt5.QtCore import QCoreApplication, QTimer
from PyQt5.QtDBus import QDBusConnection


def main() -> int:
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    parent_pid = int(sys.argv[1])
    libc = ctypes.CDLL(None, use_errno=True)
    PR_SET_PDEATHSIG = 1
    if libc.prctl(PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "prctl(PR_SET_PDEATHSIG) failed")
    if os.getppid() != parent_pid:
        return 0
    address = os.environ.get("DBUS_SESSION_BUS_ADDRESS")
    assert address and address != "unix:path=/nonexistent"
    app = QCoreApplication([])
    bus = QDBusConnection.sessionBus()
    assert bus.isConnected()
    assert bus.registerService("org.kde.plasmashell")
    timer = QTimer(app)
    timer.setInterval(200)

    def check_connection() -> None:
        if not bus.isConnected():
            app.quit()

    timer.timeout.connect(check_connection)
    timer.start()
    print("PLASMA_OWNER_READY", flush=True)
    return int(app.exec_())


if __name__ == "__main__":
    raise SystemExit(main())
