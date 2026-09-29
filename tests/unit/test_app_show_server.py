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
