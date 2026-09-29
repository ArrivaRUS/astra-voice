"""Локальный HTTP-сервер с отказами для проверок обновлений."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import TracebackType


@dataclass
class FaultServer:
    """Контекст сервера и журнал, доступный только тесту."""

    requests: list[tuple[str, dict[str, str]]] = field(default_factory=list)
    release: threading.Event = field(default_factory=threading.Event)
    server: ThreadingHTTPServer | None = None
    thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        assert self.server is not None
        return f"http://127.0.0.1:{self.server.server_port}"

    def __enter__(self) -> FaultServer:
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_CONNECT(self) -> None:
                owner.requests.append((self.path, dict(self.headers)))
                self.send_response(407)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_GET(self) -> None:
                owner.requests.append((self.path, dict(self.headers)))
                path = self.path.split("?", 1)[0]
                if path.startswith("http://"):
                    self.send_response(407)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                body = b'{"version":"1.2"}'
                status = 200
                headers: dict[str, str] = {}
                if path == "/etag":
                    headers["ETag"] = '"revision-one"'
                    if self.headers.get("If-None-Match") == headers["ETag"]:
                        status = 304
                        body = b""
                elif path == "/etag-b":
                    body = b'{"version":"2.0"}'
                    headers["ETag"] = '"revision-two"'
                elif path == "/304":
                    status = 304
                    body = b""
                elif path == "/github":
                    status = 403
                    headers["X-RateLimit-Remaining"] = "0"
                    headers["X-RateLimit-Reset"] = str(int(time.time()) + 120)
                elif path == "/ratelimit-old":
                    status = 403
                    headers["RateLimit"] = "limit=60, remaining=0, reset=30"
                elif path == "/ratelimit-new":
                    status = 403
                    headers["RateLimit"] = '"api";r=0;t=55'
                elif path == "/retry-seconds":
                    status = 429
                    headers["Retry-After"] = "90"
                elif path == "/retry-zero":
                    status = 429
                    headers["Retry-After"] = "0"
                elif path == "/forbidden":
                    status = 403
                elif path == "/429":
                    status = 429
                elif path == "/retry-date":
                    status = 429
                    date = datetime.now(UTC) + timedelta(seconds=80)
                    headers["Retry-After"] = date.strftime("%a, %d %b %Y %H:%M:%S GMT")
                elif path == "/407":
                    status = 407
                elif path == "/503":
                    status = 503
                elif path == "/503-then-502":
                    status = 503 if sum(p == path for p, _ in owner.requests) == 1 else 502
                elif path == "/evil":
                    status = 302
                    headers["Location"] = "http://evil.example/"
                elif path.startswith("/chain/"):
                    remaining = int(path.rsplit("/", 1)[1])
                    if remaining:
                        status = 302
                        headers["Location"] = f"/chain/{remaining - 1}"
                elif path == "/large":
                    body = b"x" * (256 * 1024 + 1)
                elif path == "/gzip":
                    headers["Content-Encoding"] = "gzip"
                elif path == "/broken":
                    self.send_response(200)
                    self.send_header("Content-Length", "100")
                    self.end_headers()
                    self.wfile.write(b"short")
                    self.wfile.flush()
                    self.close_connection = True
                    return
                elif path == "/declared-large":
                    self.send_response(200)
                    self.send_header("Content-Length", "10000000")
                    self.end_headers()
                    try:
                        self.wfile.write(b"short")
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):
                        pass
                    self.close_connection = True
                    return
                elif path in ("/declared-invalid", "/declared-multiple"):
                    self.send_response(200)
                    self.send_header(
                        "Content-Length", "invalid" if path.endswith("invalid") else "5"
                    )
                    if path.endswith("multiple"):
                        self.send_header("Content-Length", "5")
                    self.end_headers()
                    try:
                        self.wfile.write(b"short")
                    except (BrokenPipeError, ConnectionResetError):
                        pass
                    return
                elif path == "/slow":
                    self.send_response(200)
                    self.send_header("Content-Length", "100")
                    self.end_headers()
                    self.wfile.write(b"a")
                    self.wfile.flush()
                    owner.release.wait(10)
                    return
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                for name, value in headers.items():
                    self.send_header(name, value)
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, format: str, *args: object) -> None:
                """Не пишет пути и заголовки в журнал тестов."""

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = False
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.01}
        )
        self.thread.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.release.set()
        assert self.server is not None and self.thread is not None
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        assert not self.thread.is_alive()
