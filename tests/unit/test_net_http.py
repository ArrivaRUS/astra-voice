import pytest

pytest.importorskip("requests")

# Импорт зависимости должен предшествовать импорту проверяемого модуля.
# ruff: noqa: E402
import gzip
import logging
import os
import shutil
import ssl
import subprocess
import threading
import time
import urllib.request
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import urlsplit

import requests
from requests.packages.urllib3.connection import HTTPSConnection
from requests.packages.urllib3.response import HTTPResponse
from urllib3.exceptions import ReadTimeoutError

from astra_voice.core.policy import Policy, PolicyStatus
from astra_voice.core.settings import Settings
from astra_voice.net import http
from astra_voice.net.gate import NetworkGate, NetworkKind
from astra_voice.net.http import HttpClient, NetworkError

pytestmark = pytest.mark.unit

_BODY = b"Astra Voice " * 64
_USER_AGENT = "Astra-Voice/1.0"


@dataclass
class LocalServer:
    """Адрес, журнал запросов и сигнал завершения медленных ответов."""

    url: str
    requests: list[tuple[str, dict[str, str]]]
    release: threading.Event


@dataclass
class LocalTLSServer(LocalServer):
    ca_bundle: Path
    dripping: threading.Event


@pytest.fixture(autouse=True)
def isolated_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[..., requests.Response]:
    """Убирает внешние прокси и запрещает настоящий транспорт вне локального сервера."""
    for name in tuple(os.environ):
        if name.lower().endswith("_proxy"):
            monkeypatch.delenv(name)
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    original_send: Callable[..., requests.Response] = requests.adapters.HTTPAdapter.send

    def local_send(
        self: requests.adapters.HTTPAdapter, request: requests.PreparedRequest, **kwargs: object
    ) -> requests.Response:
        assert urlsplit(request.url).hostname == "127.0.0.1", "Запрещён выход во внешнюю сеть"
        return original_send(self, request, **kwargs)

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", local_send)
    return original_send


@pytest.fixture
def local_server(monkeypatch: pytest.MonkeyPatch) -> Iterator[LocalServer]:
    """Сервер на случайном локальном порту; послабления политики действуют только здесь."""
    seen: list[tuple[str, dict[str, str]]] = []
    release = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            seen.append((self.path, dict(self.headers)))
            body = _BODY
            status = 200
            headers: dict[str, str] = {}
            if self.path.startswith("/redirect/"):
                status = int(self.path.rsplit("/", 1)[1])
                headers = {"Location": "../ok", "Set-Cookie": "sessionid=dummy-cookie"}
            elif self.path.startswith("/chain/"):
                remaining = int(self.path.rsplit("/", 1)[1])
                if remaining:
                    status = 302
                    headers["Location"] = f"/chain/{remaining - 1}"
            elif self.path == "/evil":
                status = 302
                headers["Location"] = "https://evil.example/model.onnx"
            elif self.path == "/userinfo":
                status = 302
                headers["Location"] = "https://private-user:private-password@huggingface.co/file"
            elif self.path == "/missing-location":
                status = 302
            elif self.path == "/empty-location":
                status = 302
                headers["Location"] = ""
            elif self.path == "/broken-location":
                status = 302
                headers["Location"] = "https://["
            elif self.path == "/range" and "Range" in self.headers:
                offset = int(self.headers["Range"].removeprefix("bytes=").removesuffix("-"))
                body = body[offset:]
                status = 206
                headers["Content-Range"] = f"bytes {offset}-{len(_BODY) - 1}/{len(_BODY)}"
            elif self.path in ("/404", "/503"):
                status = int(self.path[1:])
            elif self.path == "/slow-headers":
                release.wait(2)
            elif self.path == "/slow-redirect":
                status = 302
                headers["Location"] = "/ok"
            elif self.path == "/drip":
                body = b"x" * (2 * 65536)
            elif self.path.startswith("/gzip/"):
                status = int(self.path.rsplit("/", 1)[1])
                body = gzip.compress(b"x" * (1024 * 1024))
                headers = {"Content-Encoding": "gzip", "Location": "/ok"}
            try:
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                for name, value in headers.items():
                    self.send_header(name, value)
                self.end_headers()
                if self.path == "/drip":
                    for byte in body:
                        self.wfile.write(bytes([byte]))
                        self.wfile.flush()
                        if release.wait(0.2):
                            return
                    return
                elif self.path == "/slow-body":
                    self.wfile.write(body[:4])
                    self.wfile.flush()
                    release.wait(2)
                    body = body[4:]
                elif self.path == "/slow-redirect":
                    release.wait(2)
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                # Клиент закрыл соединение при отмене, дедлайне или перенаправлении.
                pass

        def log_message(self, format: str, *args: object) -> None:
            """Не пишет служебный журнал сервера в вывод тестов."""

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    # server_close() должен дождаться и обработчиков запросов, включая каплю.
    server.daemon_threads = False
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
    thread.start()
    monkeypatch.setattr(http, "ALLOWED_HOSTS", ("127.0.0.1",))
    monkeypatch.setattr(http, "ALLOWED_SCHEMES", ("http",))
    monkeypatch.setattr(http, "ALLOWED_PORTS", (None, server.server_port))
    try:
        yield LocalServer(f"http://127.0.0.1:{server.server_port}", seen, release)
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        assert not thread.is_alive()


@pytest.fixture(scope="session")
def tls_cert_pair(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    """Генерирует временный сертификат и ключ один раз за сессию TLS-тестов."""
    openssl = shutil.which("openssl")
    if openssl is None:
        pytest.skip("Для TLS-тестов нужен openssl")
    directory = tmp_path_factory.mktemp("tls")
    cert = directory / "cert.pem"
    key = directory / "key.pem"
    subprocess.run(
        [
            openssl,
            "req",
            "-x509",
            "-newkey",
            "ec",
            "-pkeyopt",
            "ec_paramgen_curve:prime256v1",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-days",
            "1",
            "-subj",
            "/CN=127.0.0.1",
            "-addext",
            "subjectAltName=IP:127.0.0.1",
        ],
        check=True,
        capture_output=True,
    )
    return cert, key


@pytest.fixture
def local_tls_server(
    monkeypatch: pytest.MonkeyPatch, tls_cert_pair: tuple[Path, Path]
) -> Iterator[LocalTLSServer]:
    """Настоящий TLS с доверенным тестовым сертификатом и незавершённой каплей."""
    cert, key = tls_cert_pair
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    seen: list[tuple[str, dict[str, str]]] = []
    workers: list[threading.Thread] = []
    release = threading.Event()
    dripping = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            workers.append(threading.current_thread())
            seen.append((self.path, dict(self.headers)))
            try:
                if self.path.startswith("/redirect/"):
                    remaining = int(self.path.rsplit("/", 1)[1]) - 1
                    self.send_response(302)
                    self.send_header(
                        "Location", f"/redirect/{remaining}" if remaining else "/drip-headers"
                    )
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if self.path == "/drip-headers":
                    self.wfile.write(b"HTTP/1.1 200 OK\r\nX-Drip: ")
                    # Не заканчиваем ни строку заголовка, ни блок заголовков.
                else:
                    assert self.path == "/drip-body"
                    self.send_response(200)
                    self.send_header("Content-Length", str(2 * 65536))
                    self.end_headers()
                while not release.is_set():
                    self.wfile.write(b"x")
                    self.wfile.flush()
                    dripping.set()
                    if release.wait(0.05):
                        return
            except OSError:
                # shutdown клиента штатно обрывает TLS-запись сервера.
                pass

        def log_message(self, format: str, *args: object) -> None:
            """Не пишет служебный журнал сервера в вывод тестов."""

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = False
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
    # При регрессии прекращаем каплю позже проверяемых пределов, чтобы тест не висел.
    failsafe = threading.Timer(5, release.set)
    monkeypatch.setattr(http, "ALLOWED_HOSTS", ("127.0.0.1",))
    monkeypatch.setattr(http, "ALLOWED_SCHEMES", ("https",))
    monkeypatch.setattr(http, "ALLOWED_PORTS", (server.server_port,))
    thread.start()
    failsafe.start()
    try:
        yield LocalTLSServer(
            f"https://127.0.0.1:{server.server_port}", seen, release, cert, dripping
        )
    finally:
        release.set()
        failsafe.cancel()
        failsafe.join(timeout=3)
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        assert not failsafe.is_alive()
        assert not thread.is_alive()
        assert all(not worker.is_alive() for worker in workers)


@pytest.fixture
def client() -> HttpClient:
    return HttpClient(NetworkGate(Settings(), Policy()), user_agent=_USER_AGENT)


@pytest.fixture
def sessions(monkeypatch: pytest.MonkeyPatch) -> list[requests.Session]:
    """Сохраняет настоящие сессии для проверки закрытия и политики окружения."""
    created: list[requests.Session] = []
    original_session = http._Session

    def make_session() -> requests.Session:
        session = original_session()
        session.close = Mock(wraps=session.close)
        session.get = Mock(wraps=session.get)
        created.append(session)
        return session

    monkeypatch.setattr(http, "_Session", make_session)
    return created


@pytest.mark.parametrize("kind", ("download", "check_app", "check_models"))
@pytest.mark.parametrize("block", ("policy", "environment"))
def test_closed_gate_never_requests(
    local_server: LocalServer,
    monkeypatch: pytest.MonkeyPatch,
    kind: NetworkKind,
    block: str,
    sessions: list[requests.Session],
) -> None:
    policy = Policy()
    if block == "policy":
        policy = Policy(values={"offline": True}, status=PolicyStatus.OK)
    else:
        monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    gate = NetworkGate(Settings(), policy)
    client = HttpClient(gate, user_agent="проверка")
    proxy_lookup = Mock(side_effect=AssertionError("Прокси прочитаны до проверки гейта"))
    monkeypatch.setattr(urllib.request, "getproxies", proxy_lookup)
    with pytest.raises(NetworkError) as caught:
        client.get_stream(local_server.url, deadline_s=1, cancel=threading.Event(), kind=kind)
    assert caught.value.code == "no-network"
    assert str(caught.value) == caught.value.message == gate.allowed(kind)[1]
    assert local_server.requests == []
    assert sessions == []
    proxy_lookup.assert_not_called()


def test_gate_precedes_url_deadline_and_cancellation() -> None:
    gate = NetworkGate(Settings(), Policy(values={"offline": True}, status=PolicyStatus.OK))
    client = HttpClient(gate, user_agent="проверка")
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(NetworkError) as caught:
        client.get_stream("не адрес", deadline_s=-1, cancel=cancel)
    assert caught.value.code == "no-network"


def test_download_and_headers(
    client: HttpClient, local_server: LocalServer, sessions: list[requests.Session]
) -> None:
    with client.get_stream(
        local_server.url + "/ok", deadline_s=2, cancel=threading.Event()
    ) as response:
        assert response.url == local_server.url + "/ok"
        assert response.status == 200
        assert response.headers["content-length"] == str(len(_BODY))
        assert b"".join(response.iter_chunks(7)) == _BODY
    response.close()
    sessions[0].close.assert_called_once()
    headers = local_server.requests[0][1]
    assert headers["User-Agent"] == _USER_AGENT
    assert headers["Accept-Encoding"] == "identity"
    assert headers["Connection"] == "close"
    assert "Authorization" not in headers
    assert "Cookie" not in headers


def test_stream_socket_is_available(client: HttpClient, local_server: LocalServer) -> None:
    with client.get_stream(
        local_server.url + "/ok", deadline_s=2, cancel=threading.Event()
    ) as response:
        assert response._socket is not None


def test_stream_without_socket_warns_once(
    client: HttpClient, transport: Mock, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger=http.__name__):
        with client.get_stream(
            "https://huggingface.co/file", deadline_s=2, cancel=threading.Event()
        ) as response:
            assert response._socket is None
            assert b"".join(response.iter_chunks()) == _BODY
    assert caplog.record_tuples == [
        (
            http.__name__,
            logging.WARNING,
            "Не удалось получить сокет потока: отмена может срабатывать медленнее.",
        )
    ]


@pytest.mark.parametrize(("path", "status"), [("/range", 206), ("/ok", 200)])
def test_range_response_is_preserved(
    client: HttpClient, local_server: LocalServer, path: str, status: int
) -> None:
    with client.get_stream(
        local_server.url + path, range_from=11, deadline_s=2, cancel=threading.Event()
    ) as response:
        assert response.status == status
        if status == 206:
            assert response.headers["Content-Range"] == f"bytes 11-{len(_BODY) - 1}/{len(_BODY)}"
        assert b"".join(response.iter_chunks()) == (_BODY[11:] if status == 206 else _BODY)
    assert local_server.requests[0][1]["Range"] == "bytes=11-"


@pytest.mark.parametrize("status", (301, 302, 303, 307, 308))
def test_relative_redirect_without_cookies(
    client: HttpClient, local_server: LocalServer, status: int
) -> None:
    with client.get_stream(
        f"{local_server.url}/redirect/{status}", deadline_s=2, cancel=threading.Event()
    ) as response:
        assert response.url == local_server.url + "/ok"
        assert b"".join(response.iter_chunks()) == _BODY
    assert len(local_server.requests) == 2
    assert "Cookie" not in local_server.requests[-1][1]
    assert "Authorization" not in local_server.requests[-1][1]


@pytest.mark.parametrize(
    "path", ("/evil", "/userinfo", "/missing-location", "/empty-location", "/broken-location")
)
def test_invalid_redirect_stops_loading(
    client: HttpClient,
    local_server: LocalServer,
    sessions: list[requests.Session],
    path: str,
) -> None:
    with pytest.raises(NetworkError) as caught:
        client.get_stream(local_server.url + path, deadline_s=2, cancel=threading.Event())
    assert caught.value.code == "not-allowed"
    assert "private-user" not in str(caught.value)
    assert "private-password" not in str(caught.value)
    assert len(local_server.requests) == 1
    sessions[0].close.assert_called_once()


@pytest.mark.parametrize("count", (5, 6))
def test_redirect_limit(client: HttpClient, local_server: LocalServer, count: int) -> None:
    if count == 6:
        with pytest.raises(NetworkError, match="Слишком много перенаправлений") as caught:
            client.get_stream(
                f"{local_server.url}/chain/{count}", deadline_s=2, cancel=threading.Event()
            )
        assert caught.value.code == "not-allowed"
    else:
        with client.get_stream(
            f"{local_server.url}/chain/{count}", deadline_s=2, cancel=threading.Event()
        ) as response:
            assert b"".join(response.iter_chunks()) == _BODY
    assert len(local_server.requests) == 6


@pytest.mark.parametrize(
    "url",
    (
        "http://huggingface.co/file",
        "https://huggingface.co:8443/file",
        "https://huggingface.co:непорт/file",
        "https://private-user:private-password@huggingface.co/file",
        "https://private-user@huggingface.co/file",
        "https://private-user:private-password@[",
        "https://huggingface.co.evil.example/file",
        "https://evil-huggingface.co/file",
        "https://подмена-huggingface.co/file",
        "https://evil-hf.co/file",
        "https://hf.co.evil.example/file",
        "https://cdn.github.com/file",
        "https://cdn.api.github.com/file",
        "https://cdn.objects.githubusercontent.com/file",
        "https://huggingface.co./file",
        "https://.huggingface.co/file",
        "https://a..huggingface.co/file",
        "https://huggingface.co\\@evil.example/file",
        "https://huggingface.co\n.evil.example/file",
        "//huggingface.co/file",
        "https:///file",
        "https://[",
    ),
)
def test_production_url_policy_denies_without_network(
    client: HttpClient, url: str, caplog: pytest.LogCaptureFixture
) -> None:
    with pytest.raises(NetworkError) as caught:
        client.get_stream(url, deadline_s=1, cancel=threading.Event())
    assert caught.value.code == "not-allowed"
    assert "private-user" not in str(caught.value)
    assert "private-password" not in str(caught.value)

    assert "private-user" not in caplog.text
    assert "private-password" not in caplog.text


@pytest.mark.parametrize(
    "host",
    (
        "github.com",
        "api.github.com",
        "objects.githubusercontent.com",
        "huggingface.co",
        "cdn-lfs.huggingface.co",
        "a.b.huggingface.co",
        "hf.co",
        "cdn.hf.co",
        "a.b.hf.co",
    ),
)
def test_production_hosts_allowed_without_network(host: str) -> None:
    assert http._validate_url(f"https://{host}/file") == host
    assert http._validate_url(f"HTTPS://{host.upper()}:443/file") == host


@pytest.mark.parametrize("status", (404, 503))
def test_bad_status(
    client: HttpClient, local_server: LocalServer, sessions: list[requests.Session], status: int
) -> None:
    with pytest.raises(NetworkError, match=str(status)) as caught:
        client.get_stream(f"{local_server.url}/{status}", deadline_s=2, cancel=threading.Event())
    assert caught.value.code == "bad-status"
    sessions[0].close.assert_called_once()


@pytest.mark.parametrize("path", ("/slow-headers", "/slow-body"))
def test_slow_server_times_out(
    client: HttpClient, local_server: LocalServer, sessions: list[requests.Session], path: str
) -> None:
    with pytest.raises(NetworkError) as caught:
        with client.get_stream(
            local_server.url + path, deadline_s=0.1, cancel=threading.Event()
        ) as response:
            list(response.iter_chunks(4))
    assert caught.value.code == "timeout"
    sessions[0].close.assert_called_once()


@pytest.fixture
def drip_server(local_server: LocalServer) -> Iterator[LocalServer]:
    """Ограничивает зависание регрессии; таймер не участвует в успешном чтении."""
    failsafe = threading.Timer(3, local_server.release.set)
    failsafe.start()
    try:
        yield local_server
    finally:
        local_server.release.set()
        failsafe.cancel()
        failsafe.join()


@pytest.mark.parametrize("reason", ("cancelled", "timeout"))
@pytest.mark.parametrize("redirects", (0, 2))
def test_tls_drip_headers_stop_before_response(
    local_tls_server: LocalTLSServer,
    sessions: list[requests.Session],
    reason: str,
    redirects: int,
) -> None:
    """ИБ-2в: отмена и общий дедлайн прерывают TLS-чтение незавершённых заголовков."""
    client = HttpClient(
        NetworkGate(Settings(), Policy()),
        ca_bundle=local_tls_server.ca_bundle,
        user_agent=_USER_AGENT,
    )
    cancel = threading.Event()
    stop_canceller = threading.Event()
    cancelled_at: list[float] = []
    threads_before = set(threading.enumerate())

    def cancel_request() -> None:
        if local_tls_server.dripping.wait(2) and not stop_canceller.wait(0.2):
            cancelled_at.append(time.monotonic())
            cancel.set()

    canceller = threading.Thread(target=cancel_request) if reason == "cancelled" else None
    deadline = 30.0 if reason == "cancelled" else 1.0
    path = f"/redirect/{redirects}" if redirects else "/drip-headers"
    started = time.monotonic()
    if canceller is not None:
        canceller.start()
    try:
        with pytest.raises(NetworkError) as caught:
            with client.get_stream(local_tls_server.url + path, deadline_s=deadline, cancel=cancel):
                pytest.fail("Клиент вернул ответ с незавершёнными заголовками")
        finished = time.monotonic()
        assert local_tls_server.dripping.is_set()
        assert len(local_tls_server.requests) == redirects + 1
        assert caught.value.code == reason
        if reason == "cancelled":
            assert cancelled_at
            assert 0 <= finished - cancelled_at[0] <= 2.0
            assert str(caught.value) == "Загрузка отменена."
        else:
            assert deadline <= finished - started <= deadline + 2.0
            assert str(caught.value) == "Время ожидания загрузки истекло."
        sessions[0].close.assert_called_once()
    finally:
        local_tls_server.release.set()
        stop_canceller.set()
        if canceller is not None:
            canceller.join(timeout=3)
            assert not canceller.is_alive()
        assert not [
            thread
            for thread in threading.enumerate()
            if thread not in threads_before
            and thread.name in ("http-request-watchdog", "http-stream-watchdog")
        ]


def test_tls_drip_body_cancel_interrupts_ssl_read(
    local_tls_server: LocalTLSServer, sessions: list[requests.Session]
) -> None:
    """ИБ-2г: сторож сохраняет SSLSocket и прерывает каплю тела по HTTPS."""
    client = HttpClient(
        NetworkGate(Settings(), Policy()),
        ca_bundle=local_tls_server.ca_bundle,
        user_agent=_USER_AGENT,
    )
    cancel = threading.Event()
    cancelled_at: list[float] = []
    threads_before = set(threading.enumerate())

    def cancel_request() -> None:
        cancelled_at.append(time.monotonic())
        cancel.set()

    timer = threading.Timer(0.2, cancel_request)
    with client.get_stream(
        local_tls_server.url + "/drip-body", deadline_s=30, cancel=cancel
    ) as response:
        assert isinstance(response._socket, ssl.SSLSocket)
        assert int(response.headers["Content-Length"]) > 65536
        started = time.monotonic()
        timer.start()
        try:
            with pytest.raises(NetworkError) as caught:
                next(response.iter_chunks(65536))
            finished = time.monotonic()
            assert caught.value.code == "cancelled"
            assert cancelled_at
            assert 0 <= finished - cancelled_at[0] <= 2.0
            assert 0.2 <= finished - started <= 2.5
            assert response._response.raw.closed
            assert not response._watchdog.is_alive()
            sessions[0].close.assert_called_once()
        finally:
            local_tls_server.release.set()
            timer.cancel()
            timer.join(timeout=3)
            assert not timer.is_alive()
    assert not [
        thread
        for thread in threading.enumerate()
        if thread not in threads_before
        and thread.name in ("http-request-watchdog", "http-stream-watchdog")
    ]


def test_drip_cancel_interrupts_block_read(
    client: HttpClient, drip_server: LocalServer, sessions: list[requests.Session]
) -> None:
    cancel = threading.Event()
    timer = threading.Timer(0.3, cancel.set)
    with client.get_stream(drip_server.url + "/drip", deadline_s=30, cancel=cancel) as response:
        assert int(response.headers["Content-Length"]) > 65536
        started = time.monotonic()
        timer.start()
        try:
            with pytest.raises(NetworkError) as caught:
                next(response.iter_chunks(65536))
            elapsed = time.monotonic() - started
            assert caught.value.code == "cancelled"
            assert 0.3 <= elapsed <= 2.0
            assert response._response.raw.closed
            assert not response._watchdog.is_alive()
            sessions[0].close.assert_called_once()
        finally:
            timer.cancel()
            timer.join()


def test_drip_deadline_interrupts_block_read(
    client: HttpClient, drip_server: LocalServer, sessions: list[requests.Session]
) -> None:
    deadline = 1.0
    started = time.monotonic()
    with pytest.raises(NetworkError) as caught:
        with client.get_stream(
            drip_server.url + "/drip", deadline_s=deadline, cancel=threading.Event()
        ) as response:
            next(response.iter_chunks(65536))
    elapsed = time.monotonic() - started
    assert caught.value.code == "timeout"
    assert deadline <= elapsed <= deadline + 1.0
    assert response._response.raw.closed
    assert not response._watchdog.is_alive()
    sessions[0].close.assert_called_once()


@pytest.mark.parametrize("chunk_size", (1, 65536))
def test_drip_minimum_speed_expires_before_deadline(
    client: HttpClient,
    drip_server: LocalServer,
    monkeypatch: pytest.MonkeyPatch,
    chunk_size: int,
) -> None:
    # Ускоряем только окно наблюдения, оставляя производственный порог скорости.
    monkeypatch.setattr(http, "_MIN_SPEED_GRACE_S", 0.6)
    started = time.monotonic()
    with pytest.raises(NetworkError, match="Слишком низкая скорость") as caught:
        with client.get_stream(
            drip_server.url + "/drip", deadline_s=30, cancel=threading.Event()
        ) as response:
            list(response.iter_chunks(chunk_size))
    elapsed = time.monotonic() - started
    assert caught.value.code == "timeout"
    assert 0.6 <= elapsed <= 2.0
    assert response._response.raw.closed
    assert not response._watchdog.is_alive()


def test_close_from_other_threads_interrupts_block_read(
    client: HttpClient, drip_server: LocalServer, sessions: list[requests.Session]
) -> None:
    response = client.get_stream(drip_server.url + "/drip", deadline_s=30, cancel=threading.Event())
    errors: list[BaseException] = []

    def close() -> None:
        try:
            response.close()
        except BaseException as error:
            errors.append(error)

    closers = [threading.Timer(0.3, close) for _ in range(3)]
    started = time.monotonic()
    for closer in closers:
        closer.start()
    try:
        assert list(response.iter_chunks(65536)) == []
        for closer in closers:
            closer.join(timeout=1)
        assert time.monotonic() - started <= 2.0
        assert all(not closer.is_alive() for closer in closers)
        assert errors == []
        assert not response._watchdog.is_alive()
        assert response._response.raw.closed
        sessions[0].close.assert_called_once()
        response.close()
    finally:
        drip_server.release.set()
        for closer in closers:
            closer.cancel()
            closer.join(timeout=3)
        response.close()


def test_redirect_body_is_not_read(client: HttpClient, local_server: LocalServer) -> None:
    with client.get_stream(
        local_server.url + "/slow-redirect", deadline_s=0.5, cancel=threading.Event()
    ) as response:
        assert b"".join(response.iter_chunks()) == _BODY
    assert len(local_server.requests) == 2


@pytest.mark.parametrize("status", (200, 301, 302, 303, 307, 308))
def test_gzip_server_rejected_before_body_read(
    client: HttpClient,
    local_server: LocalServer,
    monkeypatch: pytest.MonkeyPatch,
    sessions: list[requests.Session],
    status: int,
) -> None:
    read = Mock(side_effect=AssertionError("Началось чтение сжатого тела"))
    iterate = Mock(side_effect=AssertionError("Началась распаковка сжатого тела"))
    monkeypatch.setattr(HTTPResponse, "read", read)
    monkeypatch.setattr(requests.Response, "iter_content", iterate)
    received: list[requests.Response] = []
    original_close = requests.Response.close

    def close(response: requests.Response) -> None:
        received.append(response)
        original_close(response)

    monkeypatch.setattr(requests.Response, "close", close)
    with pytest.raises(NetworkError) as caught:
        client.get_stream(
            f"{local_server.url}/gzip/{status}", deadline_s=2, cancel=threading.Event()
        )
    assert caught.value.code == "not-allowed"
    read.assert_not_called()
    iterate.assert_not_called()
    assert len(received) == 1
    assert received[0].raw.closed
    assert len(local_server.requests) == 1
    assert local_server.requests[0][1]["Accept-Encoding"] == "identity"
    sessions[0].close.assert_called_once()


@pytest.mark.parametrize("reason", ("cancelled", "timeout"))
def test_stop_between_chunks(
    client: HttpClient,
    local_server: LocalServer,
    sessions: list[requests.Session],
    monkeypatch: pytest.MonkeyPatch,
    reason: str,
) -> None:
    cancel = threading.Event()
    with client.get_stream(local_server.url + "/ok", deadline_s=2, cancel=cancel) as response:
        chunks = response.iter_chunks(4)
        assert next(chunks) == _BODY[:4]
        if reason == "cancelled":
            cancel.set()
        else:
            future = time.monotonic() + 3
            monkeypatch.setattr(http, "time", SimpleNamespace(monotonic=lambda: future))
        with pytest.raises(NetworkError) as caught:
            next(chunks)
        assert caught.value.code == reason
        assert response._response.raw.closed
    sessions[0].close.assert_called_once()


@pytest.mark.parametrize("reason", ("cancelled", "timeout"))
def test_stop_before_request(client: HttpClient, local_server: LocalServer, reason: str) -> None:
    cancel = threading.Event()
    if reason == "cancelled":
        cancel.set()
    with pytest.raises(NetworkError) as caught:
        client.get_stream(
            local_server.url, deadline_s=0 if reason == "timeout" else 2, cancel=cancel
        )
    assert caught.value.code == reason
    assert local_server.requests == []


def test_netrc_and_environment_do_not_supply_credentials(
    client: HttpClient,
    local_server: LocalServer,
    sessions: list[requests.Session],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("NETRC", raising=False)
    netrc = tmp_path / ".netrc"
    netrc.write_text("machine 127.0.0.1 login private-user password private-password\n")
    netrc.chmod(0o600)
    monkeypatch.setenv("HF_ENDPOINT", "https://evil.example")
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(tmp_path / "несуществующий-файл"))
    monkeypatch.setenv("CURL_CA_BUNDLE", str(tmp_path / "тоже-несуществующий-файл"))
    with client.get_stream(
        local_server.url + "/redirect/302", deadline_s=2, cancel=threading.Event()
    ) as response:
        assert response.url == local_server.url + "/ok"
        assert b"".join(response.iter_chunks()) == _BODY
    assert sessions[0].trust_env is False
    assert [path for path, _ in local_server.requests] == ["/redirect/302", "/ok"]
    for _, headers in local_server.requests:
        assert "Authorization" not in headers
        assert "Cookie" not in headers


def test_close_without_reading_releases_connection(
    client: HttpClient, local_server: LocalServer, sessions: list[requests.Session]
) -> None:
    response = client.get_stream(local_server.url + "/ok", deadline_s=2, cancel=threading.Event())
    response.close()
    response.close()
    assert response._response.raw.closed
    assert list(response.iter_chunks()) == []
    sessions[0].close.assert_called_once()


@pytest.mark.parametrize(
    ("error", "code"),
    (
        (requests.exceptions.ConnectionError("секрет"), "host-unreachable"),
        (requests.exceptions.SSLError("секрет"), "host-unreachable"),
        (requests.exceptions.ConnectTimeout("секрет"), "timeout"),
        (requests.exceptions.ReadTimeout("секрет"), "timeout"),
    ),
)
def test_transport_errors_are_translated(
    client: HttpClient,
    monkeypatch: pytest.MonkeyPatch,
    sessions: list[requests.Session],
    error: requests.exceptions.RequestException,
    code: str,
) -> None:
    monkeypatch.setattr(requests.Session, "get", Mock(side_effect=error))
    with pytest.raises(NetworkError) as caught:
        client.get_stream("https://huggingface.co/file", deadline_s=2, cancel=threading.Event())
    assert caught.value.code == code
    assert "секрет" not in str(caught.value)
    assert "requests" not in str(caught.value)
    assert caught.value.__suppress_context__
    if code == "host-unreachable":
        assert str(caught.value) == "Не удалось связаться с huggingface.co."
    sessions[0].close.assert_called_once()


def _response(status: int = 200, location: str | None = None) -> requests.Response:
    """Настоящий объект ответа с телом в памяти вместо сокета."""
    response = requests.Response()
    response.status_code = status
    response.raw = BytesIO(_BODY)
    response.close = Mock(wraps=response.close)
    response.headers["Content-Length"] = str(len(_BODY))
    if location is not None:
        response.headers["Location"] = location
    return response


@pytest.fixture
def transport(monkeypatch: pytest.MonkeyPatch) -> Mock:
    """Подменяет только транспорт, сохраняя подготовку запросов настоящей сессией."""
    send = Mock(side_effect=lambda *args, **kwargs: _response())
    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", send)
    return send


@pytest.mark.parametrize("limit", (0, 1, 1023, 1024, 1025, 65535, 65536, 65537, 131072))
def test_stream_limit_bounds_actual_reads_in_blocks(
    client: HttpClient, transport: Mock, monkeypatch: pytest.MonkeyPatch, limit: int
) -> None:
    received = _response()
    received.raw = BytesIO(b"x" * 131072)
    read = Mock(wraps=received.raw.read)
    monkeypatch.setattr(received.raw, "read", read)
    transport.side_effect = None
    transport.return_value = received
    with client.get_stream(
        "https://huggingface.co/file", deadline_s=2, cancel=threading.Event()
    ) as response:
        chunks = list(response.iter_chunks(limit=limit))
    expected_sizes = [65536] * (limit // 65536)
    if limit % 65536:
        expected_sizes.append(limit % 65536)
    assert [len(chunk) for chunk in chunks] == expected_sizes
    assert [call.args[0] for call in read.call_args_list] == expected_sizes
    assert received.raw.closed
    assert not response._watchdog.is_alive()


def test_stream_reads_requested_chunk_size(
    client: HttpClient, transport: Mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    received = _response()
    body = b"x" * (2 * 65536 + 17)
    received.raw = BytesIO(body)
    received.headers["Content-Length"] = str(len(body))
    read = Mock(wraps=received.raw.read)
    monkeypatch.setattr(received.raw, "read", read)
    transport.side_effect = None
    transport.return_value = received
    with client.get_stream(
        "https://huggingface.co/file", deadline_s=2, cancel=threading.Event()
    ) as response:
        chunks = list(response.iter_chunks(chunk_size=65536))
    assert [len(chunk) for chunk in chunks] == [65536, 65536, 17]
    assert b"".join(chunks) == body
    # Три блока данных и одно чтение EOF, все запрошены размером 64 КиБ.
    assert [call.args[0] for call in read.call_args_list] == [65536] * 4
    received.close.assert_called_once()
    assert not response._watchdog.is_alive()


@pytest.mark.parametrize("short_size", (1, 7, 257))
def test_stream_limit_short_reads_bound_iterators(
    client: HttpClient,
    transport: Mock,
    monkeypatch: pytest.MonkeyPatch,
    short_size: int,
) -> None:
    chunk_size = 65536
    limit = 4097
    received = _response()
    body = bytes(range(256)) * 32
    received.raw = BytesIO(body)
    received.headers["Content-Length"] = str(len(body))
    original_read = received.raw.read
    positions: list[int] = []

    def short_read(size: int) -> bytes:
        # Даже запрос чтения не должен выходить за оставшийся лимит.
        assert 0 < size <= limit - received.raw.tell()
        data: bytes = original_read(min(size, short_size))
        positions.append(received.raw.tell())
        return data

    monkeypatch.setattr(received.raw, "read", short_read)
    iterate = Mock(wraps=received.iter_content)
    monkeypatch.setattr(received, "iter_content", iterate)
    transport.side_effect = None
    transport.return_value = received
    with client.get_stream(
        "https://huggingface.co/file", deadline_s=2, cancel=threading.Event()
    ) as response:
        chunks = list(response.iter_chunks(chunk_size=chunk_size, limit=limit))
    assert b"".join(chunks) == body[:limit]
    assert positions[-1] == limit
    assert iterate.call_count <= chunk_size.bit_length() + 1
    assert received.raw.closed
    assert not response._watchdog.is_alive()


def test_allowed_hosts_override_is_used(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(http, "ALLOWED_HOSTS", ("127.0.0.1",))
    assert http._validate_url("https://127.0.0.1/file") == "127.0.0.1"
    with pytest.raises(NetworkError) as caught:
        http._validate_url("https://huggingface.co/file")
    assert caught.value.code == "not-allowed"


@pytest.mark.parametrize("status", (200, 301, 302, 303, 307, 308))
@pytest.mark.parametrize(
    "encoding",
    (
        "gzip",
        " GZiP \t",
        "br",
        "deflate",
        "gzip, identity",
        "identity, gzip",
        "identity, identity",
        ",",
        "identity,",
    ),
)
def test_content_encoding_rejected_without_reading(
    client: HttpClient,
    transport: Mock,
    sessions: list[requests.Session],
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    encoding: str,
) -> None:
    received = _response(status, "https://hf.co/file")
    received.headers["Content-Encoding"] = encoding
    read = Mock(side_effect=AssertionError("Началось чтение запрещённого тела"))
    monkeypatch.setattr(received.raw, "read", read)
    iterate = Mock(side_effect=AssertionError("Началась распаковка запрещённого тела"))
    monkeypatch.setattr(received, "iter_content", iterate)
    transport.side_effect = [received]
    with pytest.raises(NetworkError) as caught:
        client.get_stream("https://huggingface.co/file", deadline_s=2, cancel=threading.Event())
    assert caught.value.code == "not-allowed"
    read.assert_not_called()
    iterate.assert_not_called()
    transport.assert_called_once()
    assert received.raw.closed
    received.close.assert_called_once()
    sessions[0].close.assert_called_once()


@pytest.mark.parametrize("encoding", (None, "", " \t ", "identity", " IdEnTiTy \t"))
def test_identity_encoding_is_read(
    client: HttpClient, transport: Mock, encoding: str | None
) -> None:
    responses = [_response(302, "https://hf.co/file"), _response()]
    for received in responses:
        if encoding is not None:
            received.headers["Content-Encoding"] = encoding
    transport.side_effect = responses
    with client.get_stream(
        "https://huggingface.co/file", deadline_s=2, cancel=threading.Event()
    ) as response:
        chunks = list(response.iter_chunks(7))
        assert all(len(chunk) <= 7 for chunk in chunks)
        assert b"".join(chunks) == _BODY
    assert transport.call_count == 2


@pytest.mark.parametrize(
    ("explicit_kind", "system_exists", "environment_kind", "selected"),
    (
        ("file", True, "file", "explicit"),
        ("missing", True, "file", "system"),
        ("directory", True, "file", "system"),
        ("none", True, "file", "system"),
        ("none", False, "file", "environment"),
        ("missing", False, "file", "environment"),
        ("none", False, "missing", "default"),
        ("none", False, "directory", "default"),
        ("none", False, "none", "default"),
    ),
)
def test_ca_priority_and_ignored_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    transport: Mock,
    explicit_kind: str,
    system_exists: bool,
    environment_kind: str,
    selected: str,
) -> None:
    explicit = tmp_path / "explicit.pem"
    system = tmp_path / "system.pem"
    environment = tmp_path / "environment.pem"
    for path, kind in ((explicit, explicit_kind), (environment, environment_kind)):
        if kind == "file":
            path.write_text("Тестовый файл сертификатов", encoding="utf-8")
        elif kind == "directory":
            path.mkdir()
    if system_exists:
        system.write_text("Тестовый системный файл сертификатов", encoding="utf-8")
    monkeypatch.setattr(http, "_SYSTEM_CA_BUNDLE", system)
    if environment_kind == "none":
        monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    else:
        monkeypatch.setenv("SSL_CERT_FILE", str(environment))
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(tmp_path / "ложный-файл"))
    monkeypatch.setenv("CURL_CA_BUNDLE", str(tmp_path / "другой-ложный-файл"))
    monkeypatch.setenv("HF_ENDPOINT", "https://evil.example")
    client = HttpClient(
        NetworkGate(Settings(), Policy()),
        ca_bundle=None if explicit_kind == "none" else explicit,
        user_agent=_USER_AGENT,
    )
    with client.get_stream(
        "https://huggingface.co/file", deadline_s=2, cancel=threading.Event()
    ) as response:
        assert b"".join(response.iter_chunks()) == _BODY
    verify = transport.call_args.kwargs["verify"]
    assert verify is not False
    expected: dict[str, str | bool] = {
        "explicit": str(explicit),
        "system": str(system),
        "environment": str(environment),
        "default": True,
    }
    assert verify == expected[selected]
    assert transport.call_args.args[0].url == "https://huggingface.co/file"


@pytest.mark.parametrize("no_proxy", ("huggingface.co", "hf.co", None))
def test_explicit_proxies_on_every_redirect(
    client: HttpClient,
    monkeypatch: pytest.MonkeyPatch,
    transport: Mock,
    sessions: list[requests.Session],
    no_proxy: str | None,
) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:3128")
    # ALL_PROXY тоже не должен вернуть прокси после совпадения с NO_PROXY.
    monkeypatch.setenv("ALL_PROXY", "http://fallback.example:3128")
    if no_proxy is not None:
        monkeypatch.setenv("NO_PROXY", no_proxy)
    proxy_lookup = Mock(wraps=urllib.request.getproxies)
    monkeypatch.setattr(urllib.request, "getproxies", proxy_lookup)
    first = _response(302, "https://hf.co/file")
    second = _response()
    transport.side_effect = [first, second]
    with client.get_stream(
        "https://huggingface.co/file", deadline_s=2, cancel=threading.Event()
    ) as response:
        assert b"".join(response.iter_chunks()) == _BODY
    assert proxy_lookup.call_count == 2
    assert transport.call_count == 2
    assert first.raw.closed
    first.close.assert_called_once()
    second.close.assert_called_once()
    for call in transport.call_args_list:
        url = call.args[0].url
        expected = None if urlsplit(url).hostname == no_proxy else "http://proxy.example:3128"
        assert requests.utils.select_proxy(url, call.kwargs["proxies"]) == expected
        if expected is None:
            assert call.kwargs["proxies"] == {}
        assert call.kwargs["stream"] is True
    for call in sessions[0].get.call_args_list:
        url = call.args[0]
        expected = None if urlsplit(url).hostname == no_proxy else "http://proxy.example:3128"
        assert requests.utils.select_proxy(url, call.kwargs["proxies"]) == expected
        assert call.kwargs["allow_redirects"] is False
    assert sessions[0].trust_env is False


@pytest.mark.parametrize("proxy_scheme", ("http", "https"))
def test_proxy_authorization_only_in_connect(
    client: HttpClient,
    monkeypatch: pytest.MonkeyPatch,
    isolated_environment: Callable[..., requests.Response],
    proxy_scheme: str,
) -> None:
    monkeypatch.setenv(
        "HTTPS_PROXY", f"{proxy_scheme}://proxy-user:proxy-password@proxy.example:3128"
    )
    sockets: list[Mock] = []
    replies = iter(
        (
            b"HTTP/1.1 302 Found\r\nLocation: https://hf.co/file\r\nContent-Length: 0\r\n\r\n",
            b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok",
        )
    )

    def connect(connection: HTTPSConnection) -> None:
        # Подменяем сокет/TLS, сохраняя настоящий CONNECT, сериализацию GET,
        # HTTPAdapter и выбор прокси. Сетевых соединений в этом тесте нет.
        sock = Mock()
        sock.makefile.side_effect = [
            BytesIO(b"HTTP/1.1 200 Connection established\r\n\r\n"),
            BytesIO(next(replies)),
        ]
        sockets.append(sock)
        connection.sock = sock
        connection._tunnel()
        connection.is_verified = True
        connection.proxy_is_verified = True

    monkeypatch.setattr(HTTPSConnection, "connect", connect)
    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", isolated_environment)
    with client.get_stream(
        "https://huggingface.co/file", deadline_s=2, cancel=threading.Event()
    ) as response:
        assert b"".join(response.iter_chunks()) == b"ok"
    assert len(sockets) == 2
    for sock, host in zip(sockets, ("huggingface.co", "hf.co"), strict=True):
        wire = b"".join(call.args[0] for call in sock.sendall.call_args_list)
        tunnel, origin, rest = wire.split(b"\r\n\r\n")
        assert tunnel.startswith(f"CONNECT {host}:443 HTTP/".encode())
        assert b"Proxy-Authorization: Basic cHJveHktdXNlcjpwcm94eS1wYXNzd29yZA==" in tunnel
        assert origin.startswith(b"GET /file HTTP/")
        assert f"Host: {host}".encode() in origin
        assert b"authorization:" not in origin.lower()
        assert b"proxy-user" not in origin
        assert b"proxy-password" not in origin
        assert rest == b""


def test_cross_origin_has_no_credentials(
    client: HttpClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    transport: Mock,
    sessions: list[requests.Session],
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("NETRC", raising=False)
    netrc = tmp_path / ".netrc"
    netrc.write_text(
        "machine huggingface.co login private-user password private-password\n"
        "machine hf.co login private-user password private-password\n"
    )
    netrc.chmod(0o600)

    def send(request: requests.PreparedRequest, **kwargs: object) -> requests.Response:
        assert "Authorization" not in request.headers
        assert "Cookie" not in request.headers
        if request.url == "https://huggingface.co/file":
            # Имитируем уже сохранённый серверный cookie для следующего origin.
            sessions[0].cookies.set("sessionid", "dummy-cookie", domain="hf.co", path="/")
            return _response(302, "https://hf.co/file")
        assert request.url == "https://hf.co/file"
        return _response()

    transport.side_effect = send
    with client.get_stream(
        "https://huggingface.co/file", range_from=0, deadline_s=2, cancel=threading.Event()
    ) as response:
        assert response.url == "https://hf.co/file"
        assert b"".join(response.iter_chunks()) == _BODY
    assert sessions[0].trust_env is False
    assert transport.call_count == 2
    for call in transport.call_args_list:
        assert call.args[0].headers["Range"] == "bytes=0-"


@pytest.mark.parametrize("location", ("https://", "https:/file", "//", "https://[", " ", ""))
def test_malformed_redirect_is_rejected(client: HttpClient, transport: Mock, location: str) -> None:
    redirected = _response(302, location)
    transport.side_effect = None
    transport.return_value = redirected
    with pytest.raises(NetworkError) as caught:
        client.get_stream("https://huggingface.co/file", deadline_s=2, cancel=threading.Event())
    assert caught.value.code == "not-allowed"
    transport.assert_called_once()
    assert redirected.raw.closed


@pytest.mark.parametrize("deadline", (0.5, 10.0, 120.0))
def test_timeouts_derive_from_remaining_deadline(
    client: HttpClient, monkeypatch: pytest.MonkeyPatch, transport: Mock, deadline: float
) -> None:
    now = [100.0]
    monkeypatch.setattr(http, "time", SimpleNamespace(monotonic=lambda: now[0]))

    def send(request: requests.PreparedRequest, **kwargs: object) -> requests.Response:
        if request.url == "https://huggingface.co/file":
            now[0] += deadline / 2
            return _response(302, "https://hf.co/file")
        return _response()

    transport.side_effect = send
    with client.get_stream(
        "https://huggingface.co/file", deadline_s=deadline, cancel=threading.Event()
    ) as response:
        assert b"".join(response.iter_chunks()) == _BODY
    assert transport.call_args_list[0].kwargs["timeout"] == (min(3, deadline), min(30, deadline))
    remaining = deadline / 2
    assert transport.call_args_list[1].kwargs["timeout"] == (min(3, remaining), min(30, remaining))


@pytest.mark.parametrize("reason", ("timeout", "cancelled"))
@pytest.mark.parametrize("status", (200, 302))
def test_stop_after_headers_closes_response(
    client: HttpClient,
    monkeypatch: pytest.MonkeyPatch,
    transport: Mock,
    sessions: list[requests.Session],
    reason: str,
    status: int,
) -> None:
    now = [100.0]
    monkeypatch.setattr(http, "time", SimpleNamespace(monotonic=lambda: now[0]))
    cancel = threading.Event()
    received = _response(status, "https://hf.co/file")

    def send(request: requests.PreparedRequest, **kwargs: object) -> requests.Response:
        if reason == "cancelled":
            cancel.set()
        else:
            now[0] += 3
        return received

    transport.side_effect = send
    with pytest.raises(NetworkError) as caught:
        client.get_stream("https://huggingface.co/file", deadline_s=2, cancel=cancel)
    assert caught.value.code == reason
    transport.assert_called_once()
    assert received.raw.closed
    sessions[0].close.assert_called_once()


def test_deadline_includes_time_spent_in_gate(
    monkeypatch: pytest.MonkeyPatch, transport: Mock
) -> None:
    now = [100.0]
    monkeypatch.setattr(http, "time", SimpleNamespace(monotonic=lambda: now[0]))
    gate = NetworkGate(Settings(), Policy())

    def allowed(kind: NetworkKind) -> tuple[bool, str]:
        now[0] += 3
        return True, ""

    monkeypatch.setattr(gate, "allowed", allowed)
    client = HttpClient(gate, user_agent=_USER_AGENT)
    with pytest.raises(NetworkError) as caught:
        client.get_stream("https://huggingface.co/file", deadline_s=2, cancel=threading.Event())
    assert caught.value.code == "timeout"
    transport.assert_not_called()


@pytest.mark.parametrize("reason", ("timeout", "cancelled"))
@pytest.mark.parametrize("during_read", (False, True))
def test_stream_stops_before_delivering_late_chunk(
    client: HttpClient,
    monkeypatch: pytest.MonkeyPatch,
    transport: Mock,
    sessions: list[requests.Session],
    reason: str,
    during_read: bool,
) -> None:
    now = [100.0]
    monkeypatch.setattr(http, "time", SimpleNamespace(monotonic=lambda: now[0]))
    cancel = threading.Event()
    received = _response()
    transport.side_effect = None
    transport.return_value = received

    def stop() -> None:
        if reason == "cancelled":
            cancel.set()
        else:
            now[0] += 3

    original_read = received.raw.read

    def read(size: int) -> bytes:
        block: bytes = original_read(size)
        stop()
        return block

    with client.get_stream("https://huggingface.co/file", deadline_s=2, cancel=cancel) as response:
        chunks = response.iter_chunks(4)
        assert next(chunks) == _BODY[:4]
        if during_read:
            monkeypatch.setattr(received.raw, "read", read)
        else:
            stop()
        with pytest.raises(NetworkError) as caught:
            next(chunks)
        assert caught.value.code == reason
        assert received.raw.closed
        response.close()
    received.close.assert_called_once()
    sessions[0].close.assert_called_once()


def test_minimum_speed_budget_recovers_and_excludes_consumer_pauses(
    client: HttpClient, monkeypatch: pytest.MonkeyPatch, transport: Mock
) -> None:
    now = [100.0]
    monkeypatch.setattr(http, "time", SimpleNamespace(monotonic=lambda: now[0]))
    received = _response()
    transport.side_effect = None
    transport.return_value = received
    original_read = received.raw.read
    durations = iter((29.0, 0.0, 2.5, 2.0))

    def read(size: int) -> bytes:
        now[0] += next(durations)
        block: bytes = original_read(size)
        return block

    monkeypatch.setattr(received.raw, "read", read)
    with client.get_stream(
        "https://huggingface.co/file", deadline_s=200, cancel=threading.Event()
    ) as response:
        chunks = response.iter_chunks(128)
        # После 29 с чтения первый блок оставляет 2 с запаса; мгновенный второй
        # пополняет его до 3 с. Пауза потребителя не расходует запас.
        assert next(chunks) == _BODY[:128]
        now[0] += 31
        assert next(chunks) == _BODY[128:256]
        assert next(chunks) == _BODY[256:384]
        with pytest.raises(NetworkError, match="Слишком низкая скорость") as caught:
            next(chunks)
        assert caught.value.code == "timeout"
    assert received.raw.closed
    assert not response._watchdog.is_alive()


@pytest.mark.parametrize(
    ("error", "code"),
    (
        (requests.exceptions.ConnectionError("секрет"), "host-unreachable"),
        (requests.exceptions.SSLError("секрет"), "host-unreachable"),
        (requests.exceptions.ReadTimeout("секрет"), "timeout"),
        (ReadTimeoutError(None, "/file", "секрет"), "timeout"),
    ),
)
def test_stream_errors_close_connection(
    client: HttpClient,
    transport: Mock,
    sessions: list[requests.Session],
    error: Exception,
    code: str,
) -> None:
    def chunks() -> Iterator[bytes]:
        yield b"1234"
        raise error

    # Сохраняем настоящий iter_content, включая его обёртку над таймаутом чтения.
    raw = Mock(spec=["stream", "close"])
    raw.stream.return_value = chunks()
    received = _response()
    received.raw = raw
    transport.side_effect = None
    transport.return_value = received
    with client.get_stream(
        "https://huggingface.co/file", deadline_s=2, cancel=threading.Event()
    ) as response:
        stream = response.iter_chunks(4)
        assert next(stream) == b"1234"
        with pytest.raises(NetworkError) as caught:
            next(stream)
        assert caught.value.code == code
        assert "секрет" not in str(caught.value)
    raw.close.assert_called_once()
    received.close.assert_called_once()
    sessions[0].close.assert_called_once()
