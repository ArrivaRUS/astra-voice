"""T417 scenario: real isolated D-Bus watcher, fake native tray/audio surfaces."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Any, cast

from PyQt5.QtCore import Q_CLASSINFO, QObject, pyqtProperty, pyqtSignal
from PyQt5.QtDBus import QDBusConnection
from PyQt5.QtWidgets import QSystemTrayIcon

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "unit"))
from test_meeting_indicator_lease import Integrated  # noqa: E402

from astra_voice.platform.session import SessionKind  # noqa: E402
from astra_voice.ui.meeting_indicator_guard import MeetingIndicatorGuard  # noqa: E402
from astra_voice.ui.tray import Tray, shutdown_bus_threads  # noqa: E402
from astra_voice.ui.tray_icons import TrayIconProvider, TrayState  # noqa: E402
from helpers.qt_app import get_qapplication  # noqa: E402


class Watcher(QObject):
    Q_CLASSINFO("D-Bus Interface", "org.kde.StatusNotifierWatcher")
    StatusNotifierHostRegistered = pyqtSignal()
    StatusNotifierItemRegistered = pyqtSignal(str)
    StatusNotifierItemUnregistered = pyqtSignal(str)

    @pyqtProperty(bool)
    def IsStatusNotifierHostRegistered(self) -> bool:
        return True

    @pyqtProperty("QStringList")
    def RegisteredStatusNotifierItems(self) -> list[str]:
        return [f"org.kde.StatusNotifierItem-{os.getpid()}-1/StatusNotifierItem"]


class NativeTray(QObject):
    """No panel/X11 access; registration evidence comes from the actual bus."""

    activated = pyqtSignal(QSystemTrayIcon.ActivationReason)

    def __init__(self) -> None:
        super().__init__()
        self.visible = False

    def setContextMenu(self, value: Any) -> None:
        pass

    def setIcon(self, value: Any) -> None:
        pass

    def setToolTip(self, value: str) -> None:
        pass

    def show(self) -> None:
        self.visible = True

    def hide(self) -> None:
        self.visible = False

    def isVisible(self) -> bool:
        return self.visible

    def isSystemTrayAvailable(self) -> bool:
        return True


class NoXEmbed:
    def owner(self) -> None:
        return None

    def close(self) -> None:
        pass


def main() -> None:
    app = get_qapplication()
    bus = QDBusConnection.sessionBus()
    assert bus.isConnected()
    watcher = Watcher()
    assert bus.registerService("org.kde.StatusNotifierWatcher")
    assert bus.registerService("org.kde.plasmashell")
    assert bus.registerService(f"org.kde.StatusNotifierItem-{os.getpid()}-1")
    assert bus.registerObject("/StatusNotifierWatcher", watcher, QDBusConnection.ExportAllContents)
    native = NativeTray()
    tray = Tray(
        TrayIconProvider(SessionKind.KDE),
        tray_factory=lambda: native,
        xembed_probe=cast(Any, NoXEmbed()),
    )
    r = Integrated(Path(sys.argv[1]))
    shown = [True]
    guard = MeetingIndicatorGuard(
        active=lambda: r.controller.state in {"recording", "paused", "opening"},
        overlay_visible=lambda: shown[0],
        tray_visible=lambda: tray.meeting_indicator_visible,
        set_visible=r.controller.set_indicators_visible,
        pause=lambda: r.controller.pause("indicators"),
        capturing=lambda: r.controller.capturing_without_indicator_pause,
        clock=r.capture.clock,
    )

    def pump_until(predicate: Any) -> None:
        deadline = time.monotonic() + 5
        while not predicate():
            assert time.monotonic() < deadline, "private-indicator-timeout"
            app.processEvents()
            guard.check()
            r.tick()
            time.sleep(0.005)

    try:
        tray.set_meeting_state(TrayState.MEETING_REC)
        tray.start()
        pump_until(lambda: tray.meeting_indicator_visible)
        r.start()
        # Full-screen occlusion is represented by an unavailable W2. A real
        # registered tray must keep both capture endpoints recording.
        shown[0] = False
        guard.check()
        r.now += 2
        guard.check()
        r.tick()
        assert r.controller.state == "recording" and len(r.live_sources()) == 2
        assert bus.unregisterService("org.kde.StatusNotifierWatcher")
        pump_until(lambda: not tray.meeting_indicator_visible)
        guard.check()
        r.now += 1.001
        guard.check()
        r.finalized_pause()
        assert not r.live_sources()
        assert r.controller.pause_reasons == {"indicators"}
        shown[0] = True
        r.now += 14
        guard.check()
        r.recovered()
        assert len(r.live_sources()) == 2
        shown[0] = False
        guard.check()
        r.role.pump()
        r.now += 1.001
        guard.check()
        r.finalized_pause()
        r.now += 3600
        r.until(lambda: r.controller.state == "stopped" and r.controller._capture_finalized)
        r.drain_metadata()
        assert r.controller.saved and not r.live_sources()
        print("MEETING_INDICATORS_PRIVATE_BUS_OK", flush=True)
    finally:
        guard.shutdown()
        tray.stop()
        shutdown_bus_threads()
        r.close()
        bus.unregisterObject("/StatusNotifierWatcher")


if __name__ == "__main__":
    main()
