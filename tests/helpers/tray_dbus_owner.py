"""Владелец имени plasmashell для изолированного D-Bus-сценария."""

from __future__ import annotations

import os
import resource

from PyQt5.QtCore import QCoreApplication
from PyQt5.QtDBus import QDBusConnection


def main() -> int:
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    address = os.environ.get("DBUS_SESSION_BUS_ADDRESS")
    assert address and address != "unix:path=/nonexistent"
    app = QCoreApplication([])
    bus = QDBusConnection.sessionBus()
    assert bus.isConnected()
    assert bus.registerService("org.kde.plasmashell")
    print("PLASMA_OWNER_READY", flush=True)
    return int(app.exec_())


if __name__ == "__main__":
    raise SystemExit(main())
