"""Проверки обновлений через локальный сервер отказов."""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import requests

from astra_voice.core.policy import Policy, PolicyStatus
from astra_voice.core.settings import Settings
from astra_voice.net import http
from astra_voice.net.gate import NetworkGate
from astra_voice.net.http import HttpClient, NetworkError
from astra_voice.net.update_cache import UpdateCache
from helpers.http_fault_server import FaultServer

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Запрещает сетевой транспорт вне адреса локального сервера."""
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


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> Iterator[FaultServer]:
    with FaultServer() as local:
        monkeypatch.setattr(http, "ALLOWED_HOSTS", ("127.0.0.1",))
        monkeypatch.setattr(http, "ALLOWED_SCHEMES", ("http",))
        monkeypatch.setattr(
            http, "ALLOWED_PORTS", (None, local.server.server_port if local.server else 0)
        )
        yield local


@pytest.fixture
def client() -> HttpClient:
    return HttpClient(
        NetworkGate(Settings(check_app_updates=True), Policy()), user_agent="astra-voice/test"
    )


def test_etag_and_304(server: FaultServer, client: HttpClient, tmp_path: Path) -> None:
    cache = UpdateCache(tmp_path / "cache.json")
    first = client.get_check(
        server.url + "/etag", cache_key="app:github", cache=cache, cancel=threading.Event()
    )
    second = client.get_check(
        server.url + "/etag", cache_key="app:github", cache=cache, cancel=threading.Event()
    )
    assert first.state == "fresh" and second.state == "not-modified"
    assert first.body == second.body
    assert server.requests[1][1]["If-None-Match"] == '"revision-one"'
    assert cache.get("app:github").failures == 0


@pytest.mark.parametrize(
    "path", ["/github", "/ratelimit-old", "/ratelimit-new", "/retry-seconds", "/retry-date"]
)
def test_rate_limit(server: FaultServer, client: HttpClient, tmp_path: Path, path: str) -> None:
    cache = UpdateCache(tmp_path / "cache.json")
    first = client.get_check(
        server.url + path, cache_key="key", cache=cache, cancel=threading.Event()
    )
    second = client.get_check(
        server.url + path,
        cache_key="key",
        cache=cache,
        cancel=threading.Event(),
        ignore_backoff=True,
    )
    assert first.state == second.state == "rate-limited"
    assert first.requests_made == 1 and second.requests_made == 0
    assert len(server.requests) == 1
    assert cache.get("key").rate_limited_until is not None


def test_failures_and_retry(server: FaultServer, client: HttpClient, tmp_path: Path) -> None:
    cache = UpdateCache(tmp_path / "cache.json")
    first = client.get_check(
        server.url + "/503", cache_key="key", cache=cache, cancel=threading.Event()
    )
    second = client.get_check(
        server.url + "/503",
        cache_key="key",
        cache=cache,
        cancel=threading.Event(),
        ignore_backoff=True,
    )
    assert first.state == second.state == "unavailable"
    assert first.requests_made == second.requests_made == 2
    assert cache.get("key").failures == 2
    assert 599 <= (second.retry_at or 0) - time.time() <= 601


def test_failure_obeys_backoff(server: FaultServer, client: HttpClient, tmp_path: Path) -> None:
    cache = UpdateCache(tmp_path / "cache.json")
    first = client.get_check(
        server.url + "/503", cache_key="key", cache=cache, cancel=threading.Event()
    )
    requests_after_failure = len(server.requests)
    second = client.get_check(
        server.url + "/503", cache_key="key", cache=cache, cancel=threading.Event()
    )
    assert first.state == "unavailable"
    assert second.state == "backoff"
    assert second.requests_made == 0
    assert len(server.requests) == requests_after_failure


@pytest.mark.parametrize(
    "path", ["/407", "/broken", "/slow", "/evil", "/chain/6", "/large", "/gzip"]
)
def test_failures(server: FaultServer, client: HttpClient, tmp_path: Path, path: str) -> None:
    started = time.monotonic()
    result = client.get_check(
        server.url + path,
        cache_key="key",
        cache=UpdateCache(tmp_path / "cache.json"),
        cancel=threading.Event(),
    )
    assert result.state == "unavailable"
    assert time.monotonic() - started <= 3.5
    assert len(server.requests) <= 6


@pytest.mark.parametrize(
    "path", ["/broken", "/declared-large", "/declared-invalid", "/declared-multiple"]
)
def test_bad_content_length_does_not_cache_body(
    server: FaultServer, client: HttpClient, tmp_path: Path, path: str
) -> None:
    cache = UpdateCache(tmp_path / "cache.json")
    result = client.get_check(
        server.url + path, cache_key="key", cache=cache, cancel=threading.Event()
    )
    entry = cache.get("key")
    assert result.state == "unavailable"
    assert entry.body is None
    assert entry.failures == 1
    assert entry.backoff_until is not None


def test_http_proxy_407(
    server: FaultServer,
    client: HttpClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("HTTP_PROXY", server.url)
    monkeypatch.setattr(requests.utils, "should_bypass_proxies", lambda url, no_proxy: False)
    started = time.monotonic()
    with caplog.at_level(logging.DEBUG, logger="astra_voice.net.http"):
        result = client.get_check(
            server.url + "/etag",
            cache_key="key",
            cache=UpdateCache(tmp_path / "cache.json"),
            cancel=threading.Event(),
        )
    assert result.state == "unavailable"
    assert result.http_status == 407
    assert len(server.requests) == 1
    assert time.monotonic() - started < 3
    assert server.server is not None
    assert str(server.server.server_port) not in caplog.text


def test_connect_proxy_407(
    server: FaultServer, client: HttpClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(http, "ALLOWED_SCHEMES", ("http", "https"))
    monkeypatch.setenv("HTTPS_PROXY", server.url)
    monkeypatch.setattr(requests.utils, "should_bypass_proxies", lambda url, no_proxy: False)
    target = server.url.replace("http://", "https://") + "/etag"
    result = client.get_check(
        target,
        cache_key="key",
        cache=UpdateCache(tmp_path / "cache.json"),
        cancel=threading.Event(),
    )
    assert result.state == "unavailable"
    assert len(server.requests) == 1


def test_header_allowlist(client: HttpClient) -> None:
    with pytest.raises(ValueError):
        client.get_stream(
            "https://huggingface.co/file",
            deadline_s=1,
            cancel=threading.Event(),
            extra_headers={"Authorization": "secret"},
        )
    for value in ("é", "x\r\nInjected: yes", "x" * 257):
        with pytest.raises(ValueError):
            client.get_stream(
                "https://huggingface.co/file",
                deadline_s=1,
                cancel=threading.Event(),
                extra_headers={"Accept": value},
            )


def test_ca_environment_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    explicit = tmp_path / "explicit.pem"
    requests_bundle = tmp_path / "requests.pem"
    curl_bundle = tmp_path / "curl.pem"
    system = tmp_path / "system.pem"
    for path in (explicit, requests_bundle, curl_bundle, system):
        path.write_text("CA")
    monkeypatch.setattr(http, "_SYSTEM_CA_BUNDLE", system)
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(requests_bundle))
    monkeypatch.setenv("CURL_CA_BUNDLE", str(curl_bundle))
    assert http._verify_bundle(explicit) == str(explicit)
    assert http._verify_bundle(None) == str(requests_bundle)
    requests_bundle.unlink()
    assert http._verify_bundle(None) == str(curl_bundle)
    curl_bundle.unlink()
    assert http._verify_bundle(None) == str(system)


def test_no_network_and_not_allowed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cache = UpdateCache(tmp_path / "cache.json")
    requests_seen: list[str] = []
    monkeypatch.setattr(
        requests.Session, "get", lambda self, url, **kwargs: requests_seen.append(url)
    )
    client = HttpClient(NetworkGate(Settings(check_app_updates=False), Policy()), user_agent="test")
    with pytest.raises(NetworkError, match="выключена"):
        client.get_check(
            "https://huggingface.co/file", cache_key="key", cache=cache, cancel=threading.Event()
        )
    client = HttpClient(NetworkGate(Settings(check_app_updates=True), Policy()), user_agent="test")
    with pytest.raises(NetworkError) as error:
        client.get_check(
            "http://evil.example/", cache_key="key", cache=cache, cancel=threading.Event()
        )
    assert error.value.code == "not-allowed"
    assert requests_seen == []
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    with pytest.raises(NetworkError) as error:
        client.get_check(
            "https://huggingface.co/file", cache_key="key", cache=cache, cancel=threading.Event()
        )
    assert error.value.code == "no-network"
    assert requests_seen == []


def test_admin_offline_blocks_check(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    requests_seen: list[str] = []
    monkeypatch.setattr(
        requests.Session, "get", lambda self, url, **kwargs: requests_seen.append(url)
    )
    policy = Policy(values={"offline": True}, status=PolicyStatus.OK)
    client = HttpClient(NetworkGate(Settings(check_app_updates=True), policy), user_agent="test")
    with pytest.raises(NetworkError) as error:
        client.get_check(
            "https://huggingface.co/file",
            cache_key="key",
            cache=UpdateCache(tmp_path / "cache.json"),
            cancel=threading.Event(),
        )
    assert error.value.code == "no-network"
    assert requests_seen == []


def test_jitter_cancel_and_gate_recheck(
    client: HttpClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = UpdateCache(tmp_path / "cache.json")
    seen: list[float] = []
    requests_seen: list[str] = []
    monkeypatch.setattr(
        requests.Session, "get", lambda self, url, **kwargs: requests_seen.append(url)
    )

    def cancel_wait(delay: float) -> bool:
        seen.append(delay)
        return True

    with pytest.raises(NetworkError) as error:
        client.get_check(
            "https://huggingface.co/file",
            cache_key="key",
            cache=cache,
            cancel=threading.Event(),
            jitter_max_s=10,
            wait=cancel_wait,
        )
    assert error.value.code == "cancelled"
    assert len(seen) == 1 and 0 <= seen[0] <= 10
    assert requests_seen == []

    def offline_wait(delay: float) -> bool:
        monkeypatch.setenv("HF_HUB_OFFLINE", "1")
        return False

    with pytest.raises(NetworkError) as error:
        client.get_check(
            "https://huggingface.co/file",
            cache_key="key",
            cache=cache,
            cancel=threading.Event(),
            wait=offline_wait,
        )
    assert error.value.code == "no-network"
    assert requests_seen == []


def test_netrc_and_log(
    server: FaultServer,
    client: HttpClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / ".netrc").write_text("machine 127.0.0.1 login secret password secret")
    monkeypatch.setenv("HOME", str(home))
    with caplog.at_level(logging.DEBUG, logger="astra_voice.net.http"):
        result = client.get_check(
            server.url + "/etag?private-query",
            cache_key="key",
            cache=UpdateCache(tmp_path / "cache.json"),
            cancel=threading.Event(),
        )
    assert result.state == "fresh"
    assert "Authorization" not in server.requests[0][1]
    assert "Cookie" not in server.requests[0][1]
    assert "private-query" not in caplog.text
    assert "revision-one" not in caplog.text
    assert "version" not in caplog.text
