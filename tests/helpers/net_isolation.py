"""Изоляция сетевых тестов: только локальный fault-server, без прокси и HF_HUB_OFFLINE.

Модуль импортирует ``requests``: тесты вызывают ``pytest.importorskip("requests")``
до его импорта.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from urllib.parse import urlsplit

import pytest
import requests

from astra_voice.net import http
from helpers.http_fault_server import FaultServer


def local_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """Запрещает сетевой транспорт вне 127.0.0.1 и убирает прокси из окружения."""
    for name in tuple(os.environ):
        if name.lower().endswith("_proxy"):
            monkeypatch.delenv(name)
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    original: Callable[..., requests.Response] = requests.adapters.HTTPAdapter.send

    def local_send(
        self: requests.adapters.HTTPAdapter, request: requests.PreparedRequest, **kwargs: object
    ) -> requests.Response:
        assert urlsplit(request.url).hostname == "127.0.0.1"
        return original(self, request, **kwargs)

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", local_send)


@contextmanager
def fault_server(monkeypatch: pytest.MonkeyPatch) -> Iterator[FaultServer]:
    """Запускает fault-server и разрешает клиенту только его адрес и порт."""
    with FaultServer() as local:
        monkeypatch.setattr(http, "ALLOWED_HOSTS", ("127.0.0.1",))
        monkeypatch.setattr(http, "ALLOWED_SCHEMES", ("http",))
        monkeypatch.setattr(
            http, "ALLOWED_PORTS", (None, local.server.server_port if local.server else 0)
        )
        yield local
