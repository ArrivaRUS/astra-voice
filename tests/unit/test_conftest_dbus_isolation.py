"""Процесс pytest не подключается к пользовательской сессионной шине."""

from __future__ import annotations

import os

import pytest
from PyQt5.QtDBus import QDBusConnection

from helpers.qt_app import get_qapplication

pytestmark = pytest.mark.unit


def test_session_bus_is_isolated() -> None:
    if os.environ.get("ASTRA_VOICE_TEST_ALLOW_SESSION_BUS") == "1":
        pytest.skip("сессионная шина явно разрешена для этого прогона")
    assert os.environ["DBUS_SESSION_BUS_ADDRESS"] == "unix:path=/nonexistent"
    assert "QT_ACCESSIBILITY" not in os.environ
    assert "AT_SPI_BUS_ADDRESS" not in os.environ
    get_qapplication()
    assert QDBusConnection.sessionBus().isConnected() is False
