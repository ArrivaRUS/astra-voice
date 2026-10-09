"""Read actual a(susso)/(uo)/(so) through the production PyQt5 transport."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from PyQt5.QtCore import QCoreApplication
from PyQt5.QtDBus import QDBusConnection

from astra_voice.platform.session_state import QtLogin1Transport, ReadCallback, SessionState


class RecordingTransport(QtLogin1Transport):
    def __init__(self, connection: Any) -> None:
        super().__init__(connection)
        self.types: list[tuple[str, str]] = []

    def read(
        self, path: str, interface: str, method: str, arguments: list[Any], callback: ReadCallback
    ) -> None:
        def observed(value: Any, error: bool) -> None:
            self.types.append((method, type(value).__name__))
            callback(value, error)

        super().read(path, interface, method, arguments, observed)


def pump_until(predicate: Callable[[], bool], timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "login1 typed wire read failed"
        QCoreApplication.processEvents()
        time.sleep(0.002)


def main() -> None:
    assert os.environ["QT_QPA_PLATFORM"] == "offscreen" and "DISPLAY" not in os.environ
    app = QCoreApplication([])
    assert app is QCoreApplication.instance()
    ready = Path(sys.argv[1])
    peer = subprocess.Popen(
        [sys.executable, str(Path(__file__).with_name("fake_login1.py")), str(ready)], text=True
    )
    connection = QDBusConnection.connectToBus(os.environ["DBUS_SESSION_BUS_ADDRESS"], "typed-login")
    transport = RecordingTransport(connection)
    cache = SessionState(transport)
    try:
        pump_until(lambda: ready.exists() or peer.poll() is not None)
        assert peer.poll() is None
        cache.start()
        try:
            pump_until(lambda: cache.known)
        except AssertionError as error:
            raise AssertionError(
                f"cache stayed unknown; demarshalled types={transport.types}"
            ) from error
        assert cache.is_admissible()
        print("TYPED_LOGIN1_OK", flush=True)
    finally:
        cache.stop()
        QDBusConnection.disconnectFromBus("typed-login")
        peer.terminate()
        try:
            peer.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            peer.kill()
            peer.communicate()


if __name__ == "__main__":
    main()
