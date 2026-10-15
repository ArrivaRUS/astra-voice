"""Сокет команды ``show``: соединения обслуживают слоты сервера, без лямбд (урок 021)."""

from __future__ import annotations

import gc
import inspect
import time
from collections.abc import Callable
from pathlib import Path
from unittest.mock import Mock

import pytest
from PyQt5.QtCore import QCoreApplication, QEvent
from PyQt5.QtNetwork import QLocalSocket

from astra_voice import app as app_mod
from helpers.qt_app import get_qapplication

pytestmark = pytest.mark.unit


def _pump(predicate: Callable[[], bool]) -> None:
    deadline = time.monotonic() + 3
    while not predicate():
        assert time.monotonic() < deadline, "истёк срок ожидания событий Qt"
        QCoreApplication.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        time.sleep(0.002)


def test_show_command_is_served_by_server_slots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    get_qapplication()
    # Только свой сокет во временном каталоге: сокет запущенного приложения не трогаем.
    path = tmp_path / "ipc"
    monkeypatch.setattr(app_mod, "ipc_socket_path", lambda: path)
    on_show = Mock()
    server = app_mod.ShowServer(on_show)
    try:
        client = QLocalSocket()
        client.connectToServer(str(path))
        assert client.waitForConnected(2000)
        _pump(lambda: len(server._connections) == 1)
        client.write(b"show 123\n")
        client.flush()
        _pump(lambda: on_show.called)
        on_show.assert_called_once_with(123)
        # Сервер сам закрывает соединение после команды и забывает его.
        _pump(lambda: not server._connections)
        client.abort()
        del client
        gc.collect()
    finally:
        server.close()


def test_show_server_connects_no_lambdas() -> None:
    source = inspect.getsource(app_mod.ShowServer)
    assert ".connect(lambda" not in source


@pytest.mark.parametrize("payload", [b"show\n", b"show 123\n"])
def test_menu_show_command_reopens_hidden_window_without_starting_another_gui(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, payload: bytes
) -> None:
    from PyQt5 import QtCore, QtNetwork

    get_qapplication()
    path = tmp_path / "ipc"
    monkeypatch.setattr(app_mod, "ipc_socket_path", lambda: path)
    listener = Mock()
    listener.isListening.return_value = False
    monkeypatch.setattr(QtNetwork, "QLocalServer", Mock(return_value=listener))
    timer = Mock()
    monkeypatch.setattr(QtCore, "QTimer", Mock(return_value=timer))
    window = Mock(
        spec=[
            "windowStates",
            "setWindowStates",
            "isVisible",
            "setVisible",
            "raise_",
            "requestActivate",
            "winId",
        ]
    )
    window.windowStates.return_value = QtCore.Qt.WindowNoState
    window.isVisible.return_value = False
    window.setVisible.side_effect = lambda value: setattr(window.isVisible, "return_value", value)
    # Нулевой id исключает X11 даже в случае ненулевой метки show.
    window.winId.return_value = 0
    focuser = app_mod._WindowFocuser(window)
    server = app_mod.ShowServer(focuser.focus_shell)
    connection = Mock()
    connection.readLine.return_value = payload
    try:
        server._on_ready_read(connection)
        assert window.isVisible()
        window.setVisible.assert_called_once_with(True)
        window.raise_.assert_called_once_with()
        timer.start.assert_called_once_with()
        connection.disconnectFromServer.assert_called_once_with()
        connection.close.assert_called_once_with()
    finally:
        server.close()
        focuser.stop()
