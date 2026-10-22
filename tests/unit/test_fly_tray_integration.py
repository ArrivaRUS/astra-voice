"""Step B integration with private IPC, real QML and temporary Fly files."""

from __future__ import annotations

import json
import os
import pwd
import socket
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest
from PyQt5.QtCore import QEventLoop, Qt, QTimer
from PyQt5.QtTest import QTest
from test_app_shutdown import Rig as AppRig
from test_fly_tray_controller import pump
from test_fly_tray_controller import setup as setup
from test_fly_tray_settings import FakeFlyTray, general_qml

from astra_voice import app as app_module
from astra_voice.platform import command_hotkey
from astra_voice.platform.fly_tray import FlyTrayPriority
from astra_voice.platform.session import SessionKind
from astra_voice.worker import command_hotkey as broker_module

pytestmark = pytest.mark.unit


def test_ib74_general_keyboard_space_and_tab() -> None:
    control = FakeFlyTray()
    with general_qml(control) as (root, _row, toggle, bridge, save, messages):
        window = root.window()
        window.setHeight(int(root.height()) + 100)
        window.requestActivate()
        toggle.forceActiveFocus(Qt.TabFocusReason)
        QTest.qWait(30)
        assert toggle.hasActiveFocus()
        QTest.keyClick(window, Qt.Key_Space)
        QTest.qWait(30)
        assert not bridge.flyTrayIcon and toggle.property("checked") is False
        control.set_enabled.assert_called_once_with(False, explicit=True)
        QTest.keyClick(window, Qt.Key_Tab)
        QTest.qWait(30)
        assert not toggle.hasActiveFocus()
        QTest.keyClick(window, Qt.Key_Backtab)
        QTest.qWait(30)
        assert toggle.hasActiveFocus()
        QTest.keyClick(window, Qt.Key_Space)
        QTest.qWait(30)
        assert bridge.flyTrayIcon and save.call_count == 2
        assert not messages, messages


@pytest.mark.parametrize("origin", ["normal", "hidden", "register", "wizard", "update"])
def test_ib73_main_wires_launch_origin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, origin: str
) -> None:
    rig = AppRig(monkeypatch, tmp_path)
    rig.runtime.fly_tray = None
    rig.settings.extra["onboarding_done"] = origin != "wizard"
    monkeypatch.setattr(app_module, "detect", lambda: SessionKind.FLY)
    monkeypatch.delenv("ASTRA_VOICE_AFTER_UPDATE", raising=False)
    if origin == "update":
        monkeypatch.setenv("ASTRA_VOICE_AFTER_UPDATE", "1")
    args = ["--" + origin] if origin in {"hidden", "register"} else []
    assert app_module.main(args) == 7
    rig.runtime.start_fly_tray.assert_called_once_with(automatic=origin == "normal")
    if origin == "update":
        assert "ASTRA_VOICE_AFTER_UPDATE" not in os.environ


def test_ib73_unknown_keeps_real_theme_and_provenance(
    setup: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(pwd, "getpwuid", lambda uid: SimpleNamespace(pw_dir=str(tmp_path)))
    fly = tmp_path / ".fly"
    fly.mkdir(mode=0o700)
    theme = fly / "theme"
    theme.mkdir(mode=0o700)
    target = theme / "current.themerc"
    original = b"TaskbarColor=#112233\nTrayPriorityList=one:fly-vkbd\n"
    target.write_bytes(original)
    backend = FlyTrayPriority()
    clock = [0.0]

    def wait(seconds: float) -> bool:
        clock[0] += seconds
        return False

    ctl, host, _, notices, runs = setup(
        backend=backend, owner=lambda: 10, clock=lambda: clock[0], wait=wait
    )
    ctl.start()
    pump(lambda: notices == ["unknown"])
    assert clock[0] == pytest.approx(30.0)
    assert runs == ["FLYWM_RESTART"] and host.releases == 1
    expected = original.replace(b"fly-vkbd", b"fly-vkbd:astra-voice")
    assert target.read_bytes() == expected
    assert json.loads(backend.state_path.read_bytes()) == {
        "added": True,
        "inserted": ":astra-voice",
    }
    for _ in range(3):
        ctl.set_enabled(False)
        QTest.qWait(2)
    assert target.read_bytes() == expected and runs == ["FLYWM_RESTART"]


@pytest.mark.parametrize("delay_ms", [700, 5000])
def test_ib72_real_broker_protocol_heartbeat_during_slow_command(
    setup: Any, monkeypatch: pytest.MonkeyPatch, delay_ms: int
) -> None:
    class PacketSocket(socket.socket):
        __slots__ = ()

        def send(self, data: Any, flags: int = 0) -> int:
            # The sandbox blocks send/sendto; sendmsg preserves native packet IPC.
            return self.sendmsg([data], [], flags)

    entered, release = threading.Event(), threading.Event()

    def slow(command: str) -> None:
        entered.set()
        assert release.wait(7)

    ctl, host, _, notices, _ = setup(runner=slow)
    x_backend = Mock()
    x_backend.fileno.return_value = -1
    x_backend._x.pending_events.return_value = 0
    monkeypatch.setattr(broker_module, "X11HotkeyBackend", lambda: x_backend)
    gui_socket, broker_socket = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    # Reuse native endpoints: wrapping an existing fd is also sandbox-restricted.
    gui_socket.__class__ = broker_socket.__class__ = PacketSocket
    gui_socket.setblocking(False)
    broker_socket.setblocking(False)
    broker = broker_module.Broker(broker_socket)
    client = command_hotkey.CommandHotkeyBackend()
    client._connection = gui_socket
    client._last_reply = client._last_ping = command_hotkey.boot_time()
    initial_reply = client._last_reply
    errors: list[Exception] = []

    def serve() -> None:
        try:
            broker.run()
        except (OSError, ValueError) as error:
            if not client._closed:
                errors.append(error)
        except Exception as error:
            errors.append(error)

    server = threading.Thread(target=serve, name="test-private-command-broker")
    timer, release_timer, watchdog = QTimer(), QTimer(), QTimer()
    loop = QEventLoop()
    ticks = [0]

    def pulse() -> None:
        client._pump()
        ticks[0] += 1
        if notices:
            loop.quit()

    timer.timeout.connect(pulse)
    release_timer.setSingleShot(True)
    release_timer.setTimerType(Qt.PreciseTimer)
    release_timer.timeout.connect(release.set)
    watchdog.setSingleShot(True)
    watchdog.timeout.connect(loop.quit)
    server.start()
    try:
        timer.start(10)
        ctl.start()
        pump(entered.is_set)
        release_timer.start(delay_ms)
        watchdog.start(6500)
        loop.exec_()
        assert notices == ["show"] and host.releases == 1
        assert ticks[0] >= 20
        assert client._connection is gui_socket and server.is_alive(), errors
        assert client._last_reply > initial_reply + 0.5
        assert not errors
    finally:
        release.set()
        timer.stop()
        release_timer.stop()
        watchdog.stop()
        client.close()
        server.join(2)
        assert not server.is_alive()
