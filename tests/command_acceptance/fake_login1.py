"""Standard-typed login1 peer on a PRIVATE SESSION bus, never system bus."""

from __future__ import annotations

import os
import sys
from importlib import import_module
from pathlib import Path
from typing import Any

# Dynamic imports keep the optional peer dependency out of normal test collection.
dbus = import_module("dbus")
service = import_module("dbus.service")
mainloop = import_module("dbus.mainloop.glib")
GLib = import_module("gi.repository.GLib")
mainloop.DBusGMainLoop(set_as_default=True)
assert os.environ["DBUS_SESSION_BUS_ADDRESS"].startswith("unix:")
bus = dbus.SessionBus()
name = service.BusName("org.freedesktop.login1", bus)
UID = os.getuid()
SESSION_PATH = "/org/freedesktop/login1/session/test"


class Manager(service.Object):  # type: ignore[name-defined]
    @service.method("org.freedesktop.login1.Manager", in_signature="s", out_signature="o")
    def GetSession(self, session_id: str) -> str:
        return SESSION_PATH

    @service.method("org.freedesktop.login1.Manager", out_signature="a(susso)")
    def ListSessions(self) -> list[Any]:
        return [("test", UID, "test-user", "seat0", SESSION_PATH)]

    @service.method("org.freedesktop.DBus.Properties", in_signature="ss", out_signature="v")
    def Get(self, interface: str, property_name: str) -> Any:
        return dbus.Boolean(False)


class Session(service.Object):  # type: ignore[name-defined]
    @service.method("org.freedesktop.DBus.Properties", in_signature="s", out_signature="a{sv}")
    def GetAll(self, interface: str) -> dict[str, Any]:
        return {
            "User": dbus.Struct((dbus.UInt32(UID), dbus.ObjectPath("/user/test")), signature="uo"),
            "Seat": dbus.Struct(
                ("seat0", dbus.ObjectPath("/org/freedesktop/login1/seat/seat0")), signature="so"
            ),
            "Type": "x11",
            "Class": "user",
            "Remote": dbus.Boolean(False),
            "Display": ":99",
            "LockedHint": dbus.Boolean(False),
            "Active": dbus.Boolean(True),
        }


manager = Manager(bus, "/org/freedesktop/login1")
session = Session(bus, SESSION_PATH)
Path(sys.argv[1]).write_text("ready", encoding="ascii")
GLib.MainLoop().run()
