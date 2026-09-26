import pytest

pytest.importorskip("requests")

# Импорт зависимости должен предшествовать импорту проверяемого модуля.
# ruff: noqa: E402
import errno
import hashlib
import os
import stat
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import BinaryIO, Literal, cast
from unittest.mock import MagicMock, Mock
from urllib.parse import urlsplit

import requests

from astra_voice.core.policy import Policy
from astra_voice.core.settings import Settings
from astra_voice.models import downloader
from astra_voice.models.catalog import CatalogEntry, FileSpec
from astra_voice.models.downloader import Downloader, DownloadError, Progress, fail_reason
from astra_voice.models.store import ModelStore
from astra_voice.net import http
from astra_voice.net.gate import NetworkGate, NetworkKind
from astra_voice.net.http import HttpClient, StreamResponse

pytestmark = pytest.mark.unit


@dataclass
class Fault:
    """Тело и управляемые отклонения ответа локального сервера."""

    body: bytes
    mode: str = "normal"
    content_range: str | None = None
    status: int = 200


@dataclass
class LocalServer:
    """Настройки ответов, сетевые запросы и аргументы настоящего HTTP-клиента."""

    host: str
    faults: dict[str, Fault]
    requests: list[tuple[str, dict[str, str]]] = field(default_factory=list)
    calls: list[tuple[str, int | None, float, threading.Event]] = field(default_factory=list)
    responses: list[StreamResponse] = field(default_factory=list)
    release: threading.Event = field(default_factory=threading.Event)


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Повторяет изоляцию test_net_http: только loopback, без внешних прокси."""
    for name in tuple(os.environ):
        if name.lower().endswith("_proxy"):
            monkeypatch.delenv(name)
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    original_send = requests.adapters.HTTPAdapter.send

    def local_send(
        self: requests.adapters.HTTPAdapter, request: requests.PreparedRequest, **kwargs: object
    ) -> requests.Response:
        assert urlsplit(request.url).hostname == "127.0.0.1", "Запрещён выход во внешнюю сеть"
        return original_send(self, request, **kwargs)

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", local_send)


@pytest.fixture
def payloads() -> dict[str, bytes]:
    return {
        "weights/model.bin": b"0123456789abcdef",
        "tokens.txt": "астра голос\n".encode(),
        "config/model.json": b'{"sample_rate":16000}\n',
    }


@pytest.fixture
def entry(payloads: dict[str, bytes]) -> CatalogEntry:
    return CatalogEntry(
        id="tiny-model",
        revision="rev-1",
        name="Маленькая модель",
        description="Синтетическая запись для проверки загрузки",
        size_bytes=sum(map(len, payloads.values())),
        min_ram_mb=1,
        layout="gigaam-rnnt",
        variant="int8",
        recommended=True,
        host="huggingface.co",
        files=tuple(
            FileSpec(path, hashlib.sha256(body).hexdigest(), len(body), f"/resolve/rev-1/{path}")
            for path, body in payloads.items()
        ),
    )


@pytest.fixture
def local_server(
    monkeypatch: pytest.MonkeyPatch, entry: CatalogEntry, payloads: dict[str, bytes]
) -> Iterator[LocalServer]:
    """Настоящий TCP fault-сервер; запрет сокетов песочницей остаётся видимой ошибкой."""
    state = LocalServer("", {file.url_path: Fault(payloads[file.path]) for file in entry.files})

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            state.requests.append((self.path, dict(self.headers)))
            fault = state.faults[self.path]
            body, status = fault.body, fault.status
            headers: dict[str, str] = {}
            range_header = self.headers.get("Range")
            if fault.mode == "redirect":
                status = 302
                headers["Location"] = "https://evil.example/model.bin"
            elif range_header is not None and fault.mode != "ignore-range":
                offset = int(range_header.removeprefix("bytes=").removesuffix("-"))
                body, status = body[offset:], 206
                headers["Content-Range"] = (
                    fault.content_range
                    if fault.content_range is not None
                    else f"bytes {offset}-{len(fault.body) - 1}/{len(fault.body)}"
                )
                if fault.mode == "missing-range":
                    del headers["Content-Range"]
                elif fault.mode == "other-success":
                    status = fault.status
            elif fault.mode == "other-success":
                status = 200
            if fault.mode == "truncate" and range_header is None:
                body = body[: len(body) // 2]
            try:
                if fault.mode == "slow-headers":
                    state.release.wait(2)
                self.send_response(status)
                # Chunked намеренно сочетается с ложным Content-Length для проверки У20.
                self.send_header(
                    "Content-Length",
                    str(
                        1
                        if fault.mode == "chunked"
                        else len(fault.body)
                        if fault.mode == "truncate" and range_header is None
                        else len(body)
                    ),
                )
                if fault.mode in {"chunked", "disconnect", "slow-body"}:
                    self.send_header("Transfer-Encoding", "chunked")
                for name, value in headers.items():
                    self.send_header(name, value)
                self.end_headers()
                if fault.mode in {"disconnect", "slow-body"}:
                    prefix = body[:4] if fault.mode == "slow-body" else body[: len(body) // 2]
                    self.wfile.write(f"{len(prefix):x}\r\n".encode() + prefix + b"\r\n")
                    self.wfile.flush()
                    if fault.mode == "disconnect":
                        # Нет завершающего chunk: настоящий обрыв виден и в urllib3 1.26.
                        self.close_connection = True
                    else:
                        state.release.wait(2)
                        tail = body[len(prefix) :]
                        self.wfile.write(f"{len(tail):x}\r\n".encode() + tail + b"\r\n0\r\n\r\n")
                elif fault.mode == "chunked":
                    self.wfile.write(f"{len(body):x}\r\n".encode() + body + b"\r\n0\r\n\r\n")
                else:
                    self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                # Загрузчик имеет право прекратить чтение при отказе, отмене или таймауте.
                pass

        def log_message(self, format: str, *args: object) -> None:
            """Служебный журнал HTTP не засоряет вывод тестов."""

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    state.host = f"127.0.0.1:{server.server_port}"
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
    # Те же три monkeypatch, что и в test_net_http; продовая политика не меняется.
    monkeypatch.setattr(http, "ALLOWED_HOSTS", ("127.0.0.1",))
    monkeypatch.setattr(http, "ALLOWED_SCHEMES", ("http",))
    monkeypatch.setattr(http, "ALLOWED_PORTS", (None, server.server_port))
    monkeypatch.setattr(downloader, "_CHUNK_SIZE", 4)
    original_get_stream = HttpClient.get_stream

    def local_get_stream(
        self: HttpClient,
        url: str,
        *,
        range_from: int | None = None,
        deadline_s: float,
        cancel: threading.Event,
        kind: NetworkKind = "download",
        idle_timeout_s: float | None = None,
        scope: Literal["public", "corp"] = "public",
    ) -> StreamResponse:
        # Загрузчик обязан собрать HTTPS буквально. Только тест переводит его в HTTP.
        assert url.startswith(f"https://{state.host}/")
        state.calls.append((url, range_from, deadline_s, cancel))
        response = original_get_stream(
            self,
            "http://" + url.removeprefix("https://"),
            range_from=range_from,
            deadline_s=deadline_s,
            cancel=cancel,
            kind=kind,
            idle_timeout_s=idle_timeout_s,
            scope=scope,
        )
        state.responses.append(response)
        return response

    monkeypatch.setattr(HttpClient, "get_stream", local_get_stream)
    thread.start()
    try:
        yield state
    finally:
        state.release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        assert not thread.is_alive()


@pytest.fixture
def local_entry(entry: CatalogEntry, local_server: LocalServer) -> CatalogEntry:
    return replace(entry, host=local_server.host)


@pytest.fixture
def single_entry(local_entry: CatalogEntry) -> CatalogEntry:
    return replace(local_entry, files=local_entry.files[:1], size_bytes=local_entry.files[0].size)


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ModelStore:
    result = ModelStore(tmp_path / "models")
    monkeypatch.setattr(result, "disk_ok", Mock(return_value=True))
    monkeypatch.setattr(result, "ram_ok", Mock(side_effect=AssertionError("ОЗУ решает вызывающий")))
    return result


@pytest.fixture
def client() -> HttpClient:
    return HttpClient(NetworkGate(Settings(), Policy()), user_agent="Astra-Voice/1.0")


@pytest.fixture
def loader(client: HttpClient, store: ModelStore) -> Downloader:
    return Downloader(client, store)


def _part(store: ModelStore, entry: CatalogEntry, body: bytes) -> Path:
    path = store.staging_dir(entry.id, entry.revision) / (entry.files[0].path + ".part")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return path


def _download(loader: Downloader, entry: CatalogEntry) -> Path:
    return loader.download(entry, progress=lambda state: None, cancel=threading.Event())


def _assert_error(loader: Downloader, entry: CatalogEntry, code: str) -> DownloadError:
    with pytest.raises(DownloadError) as caught:
        _download(loader, entry)
    assert caught.value.code == code
    assert str(caught.value) == caught.value.message
    return caught.value


def test_all_files_and_cached_download(
    loader: Downloader,
    store: ModelStore,
    local_entry: CatalogEntry,
    local_server: LocalServer,
    payloads: dict[str, bytes],
) -> None:
    progress: list[Progress] = []
    cancel = threading.Event()
    result = loader.download(local_entry, progress=progress.append, cancel=cancel)
    assert result == store.staging_dir(local_entry.id, local_entry.revision)
    assert {file.path: (result / file.path).read_bytes() for file in local_entry.files} == payloads
    assert list(result.rglob("*.part")) == []
    assert [path for path, _ in local_server.requests] == [f.url_path for f in local_entry.files]
    assert [call[0] for call in local_server.calls] == [
        "https://" + local_entry.host + file.url_path for file in local_entry.files
    ]
    assert all(call[1] is None and call[3] is cancel for call in local_server.calls)
    assert all(21599 < call[2] <= 21600 for call in local_server.calls)
    assert all(state.bytes_total == local_entry.size_bytes for state in progress)
    assert progress[-1].bytes_done == local_entry.size_bytes
    assert sorted({state.file_index for state in progress}) == [1, 2, 3]
    assert [state.file_index for state in progress] == sorted(s.file_index for s in progress)
    assert all(state.file_count == 3 for state in progress)
    assert [state.bytes_done for state in progress] == sorted(s.bytes_done for s in progress)
    assert all(response._response.raw.closed for response in local_server.responses)

    progress.clear()
    assert loader.download(local_entry, progress=progress.append, cancel=cancel) == result
    assert len(local_server.requests) == len(local_server.calls) == 3
    assert [state.bytes_done for state in progress] == [
        sum(file.size for file in local_entry.files[:index]) for index in range(1, 4)
    ]
    assert all(state.speed_bps == 0 and state.eta_s is None for state in progress)


@pytest.mark.parametrize("mode", ("normal", "ignore-range"))
def test_resume_or_restart_on_200(
    loader: Downloader,
    store: ModelStore,
    single_entry: CatalogEntry,
    local_server: LocalServer,
    mode: str,
) -> None:
    file = single_entry.files[0]
    fault = local_server.faults[file.url_path]
    fault.mode = mode
    # При 200 старый префикс заведомо неверен: простое приписывание не пройдёт тест.
    part = _part(store, single_entry, fault.body[:5] if mode == "normal" else b"xxxxx")
    result = _download(loader, single_entry)
    assert (result / file.path).read_bytes() == fault.body
    assert not part.exists()
    assert [headers.get("Range") for _, headers in local_server.requests] == ["bytes=5-"]


@pytest.mark.parametrize(
    "content_range",
    ("bytes 0-15/16", "bytes 5-15/100", "bytes 5-2/16", "bytes 5-16/16", "bytes 5-15/*", "garbage"),
)
def test_foreign_or_malformed_range_restarts(
    loader: Downloader,
    store: ModelStore,
    single_entry: CatalogEntry,
    local_server: LocalServer,
    content_range: str,
) -> None:
    file = single_entry.files[0]
    fault = local_server.faults[file.url_path]
    fault.content_range = content_range
    part = _part(store, single_entry, b"xxxxx")
    result = _download(loader, single_entry)
    assert (result / file.path).read_bytes() == fault.body
    assert not part.exists()
    assert [headers.get("Range") for _, headers in local_server.requests] == ["bytes=5-", None]
    assert all(response._response.raw.closed for response in local_server.responses)


@pytest.mark.parametrize("status", (201, 202, 204))
def test_other_success_restarts_without_range(
    loader: Downloader,
    store: ModelStore,
    single_entry: CatalogEntry,
    local_server: LocalServer,
    status: int,
) -> None:
    file = single_entry.files[0]
    fault = local_server.faults[file.url_path]
    fault.mode, fault.status = "other-success", status
    _part(store, single_entry, fault.body[:5])
    result = _download(loader, single_entry)
    assert (result / file.path).read_bytes() == fault.body
    assert [headers.get("Range") for _, headers in local_server.requests] == ["bytes=5-", None]


def test_missing_content_range_restarts(
    loader: Downloader, store: ModelStore, single_entry: CatalogEntry, local_server: LocalServer
) -> None:
    file = single_entry.files[0]
    fault = local_server.faults[file.url_path]
    fault.mode = "missing-range"
    _part(store, single_entry, fault.body[:5])
    result = _download(loader, single_entry)
    assert (result / file.path).read_bytes() == fault.body
    assert [headers.get("Range") for _, headers in local_server.requests] == ["bytes=5-", None]


def test_disconnect_keeps_part_and_next_call_resumes(
    loader: Downloader, store: ModelStore, single_entry: CatalogEntry, local_server: LocalServer
) -> None:
    file = single_entry.files[0]
    fault = local_server.faults[file.url_path]
    fault.mode = "disconnect"
    _assert_error(loader, single_entry, "host-unreachable")
    part = store.staging_dir(single_entry.id, single_entry.revision) / (file.path + ".part")
    assert part.read_bytes() == fault.body[:8]
    assert local_server.responses[-1]._response.raw.closed
    fault.mode = "normal"
    result = _download(loader, single_entry)
    assert (result / file.path).read_bytes() == fault.body
    assert local_server.requests[-1][1]["Range"] == "bytes=8-"


def test_truncate_moves_to_next_source_with_range(
    loader: Downloader,
    single_entry: CatalogEntry,
    local_server: LocalServer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    file = single_entry.files[0]
    local_server.faults[file.url_path].mode = "truncate"
    # Два источника доступны через один loopback fault-сервер.
    item = replace(single_entry, mirrors=(local_server.host,))
    monkeypatch.setattr(downloader, "_PROGRESS_INTERVAL_S", 0)
    progress: list[Progress] = []
    result = loader.download(item, progress=progress.append, cancel=threading.Event())
    assert (result / file.path).read_bytes() == local_server.faults[file.url_path].body
    assert [headers.get("Range") for _, headers in local_server.requests] == [
        None,
        f"bytes={file.size // 2}-",
    ]
    assert [state.bytes_done for state in progress] == sorted(
        state.bytes_done for state in progress
    )


def test_truncate_last_source_keeps_part_and_retry_resumes(
    loader: Downloader,
    store: ModelStore,
    single_entry: CatalogEntry,
    local_server: LocalServer,
) -> None:
    file = single_entry.files[0]
    fault = local_server.faults[file.url_path]
    fault.mode = "truncate"
    _assert_error(loader, single_entry, "short-read")
    part = store.staging_dir(single_entry.id, single_entry.revision) / (file.path + ".part")
    assert part.read_bytes() == fault.body[: file.size // 2]
    fault.mode = "normal"
    result = _download(loader, single_entry)
    assert (result / file.path).read_bytes() == fault.body
    assert local_server.requests[-1][1]["Range"] == f"bytes={file.size // 2}-"


@pytest.mark.parametrize("corruption", ("same-size", "short", "resume-prefix"))
def test_bad_checksum_and_short_read_handling(
    loader: Downloader,
    store: ModelStore,
    single_entry: CatalogEntry,
    local_server: LocalServer,
    corruption: str,
) -> None:
    file = single_entry.files[0]
    fault = local_server.faults[file.url_path]
    part = _part(store, single_entry, b"xxxxx" if corruption == "resume-prefix" else b"")
    if corruption == "same-size":
        fault.body = b"x" * file.size
    elif corruption == "short":
        fault.body = fault.body[:-1]
    if corruption == "resume-prefix":
        assert (_download(loader, single_entry) / file.path).read_bytes() == fault.body
        assert not part.exists()
        return
    _assert_error(loader, single_entry, "short-read" if corruption == "short" else "bad-checksum")
    if corruption == "short":
        assert part.read_bytes() == fault.body
    else:
        assert not part.exists()
    assert not part.with_suffix("").exists()
    assert local_server.responses[-1]._response.raw.closed


def _observe_writes(monkeypatch: pytest.MonkeyPatch, part: Path) -> list[int]:
    """Считает байты через настоящий write, включая данные до удаления .part."""
    counts: list[int] = []
    original_open = Path.open

    def observed_open(path: Path, mode: str = "r") -> BinaryIO:
        stream = cast(BinaryIO, original_open(path, mode))
        if path != part or "+" not in mode:
            return stream
        wrapper = MagicMock(wraps=stream)
        wrapper.__enter__.return_value = wrapper
        wrapper.__exit__.side_effect = stream.__exit__

        def write(block: bytes) -> int:
            count = stream.write(block)
            counts.append(count)
            return count

        wrapper.write.side_effect = write
        return cast(BinaryIO, wrapper)

    monkeypatch.setattr(Path, "open", observed_open)
    return counts


@pytest.mark.parametrize("resumed", (False, True))
def test_one_extra_byte_never_written_despite_false_content_length(
    loader: Downloader,
    store: ModelStore,
    single_entry: CatalogEntry,
    local_server: LocalServer,
    monkeypatch: pytest.MonkeyPatch,
    resumed: bool,
) -> None:
    file = single_entry.files[0]
    fault = local_server.faults[file.url_path]
    existing = 4 if resumed else 0
    part = _part(store, single_entry, fault.body[:existing])
    fault.body += b"!"
    fault.mode = "chunked"
    if resumed:
        fault.content_range = f"bytes {existing}-{file.size - 1}/{file.size}"
    counts = _observe_writes(monkeypatch, part)
    _assert_error(loader, single_entry, "too-large")
    assert counts, "Тест должен проверить запись блоков перед превышением лимита"
    assert 0 < sum(counts) + existing <= file.size
    assert not part.exists()
    assert not part.with_suffix("").exists()
    assert local_server.responses[-1]._response.raw.closed


def test_total_staging_budget_includes_unrelated_files_and_partial_files(
    loader: Downloader,
    store: ModelStore,
    single_entry: CatalogEntry,
    local_server: LocalServer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    file = single_entry.files[0]
    part = _part(store, single_entry, local_server.faults[file.url_path].body[:4])
    staging = store.staging_dir(single_entry.id, single_entry.revision)
    (staging / "old.bin").write_bytes(b"x" * single_entry.size_bytes)
    (staging / "other.part").write_bytes(b"x" * 8)
    counts = _observe_writes(monkeypatch, part)
    _assert_error(loader, single_entry, "too-large")
    assert sum(counts) == 4
    assert single_entry.size_bytes + 8 + 4 + sum(counts) == 2 * single_entry.size_bytes
    assert not part.exists()


def test_reading_stops_at_first_excess_byte(
    loader: Downloader,
    single_entry: CatalogEntry,
    local_server: LocalServer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Длинный хвост не читается даже до конца очередного блока HTTP-клиента."""
    file = single_entry.files[0]
    fault = local_server.faults[file.url_path]
    fault.body += b"!" * 100
    fault.mode = "chunked"
    original_chunks = StreamResponse.iter_chunks
    read_sizes: list[int] = []

    def observed_chunks(
        self: StreamResponse, chunk_size: int = 65536, *, limit: int | None = None
    ) -> Iterator[bytes]:
        for block in original_chunks(self, chunk_size, limit=limit):
            read_sizes.append(len(block))
            yield block

    monkeypatch.setattr(StreamResponse, "iter_chunks", observed_chunks)
    _assert_error(loader, single_entry, "too-large")
    assert sum(read_sizes) == file.size + 1
    assert local_server.responses[-1]._response.raw.closed


def test_redirect_to_foreign_host_tries_next_source_without_requesting_foreign_host(
    store: ModelStore,
    entry: CatalogEntry,
    payloads: dict[str, bytes],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    file = entry.files[0]
    item = replace(entry, files=(file,), size_bytes=file.size, mirrors=("github.com",))
    hosts: list[str] = []

    def send(
        adapter: requests.adapters.HTTPAdapter,
        request: requests.PreparedRequest,
        **kwargs: object,
    ) -> requests.Response:
        host = urlsplit(request.url).hostname or ""
        hosts.append(host)
        response = requests.Response()
        response.status_code = 302 if host == "huggingface.co" else 200
        response.raw = BytesIO(b"" if response.status_code == 302 else payloads[file.path])
        if response.status_code == 302:
            response.headers["Location"] = "https://evil.example/file"
        return response

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", send)
    client = HttpClient(NetworkGate(Settings(), Policy()), user_agent="Astra-Voice/test")
    loader = Downloader(client, store)
    single = replace(item, mirrors=())
    _assert_error(loader, single, "bad-status")
    assert hosts == ["huggingface.co"]

    result = loader.download(item, progress=lambda state: None, cancel=threading.Event())
    assert (result / file.path).read_bytes() == payloads[file.path]
    assert hosts == ["huggingface.co", "huggingface.co", "github.com"]


def test_unicode_redirect_host_tries_next_source_without_socket(
    store: ModelStore,
    entry: CatalogEntry,
    payloads: dict[str, bytes],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    file = entry.files[0]
    item = replace(entry, files=(file,), size_bytes=file.size, mirrors=("github.com",))
    urls: list[str] = []

    def send(
        adapter: requests.adapters.HTTPAdapter,
        request: requests.PreparedRequest,
        **kwargs: object,
    ) -> requests.Response:
        urls.append(request.url)
        host = urlsplit(request.url).hostname
        assert host in {"huggingface.co", "github.com"}
        response = requests.Response()
        response.status_code = 302 if host == "huggingface.co" else 200
        response.raw = BytesIO(b"" if response.status_code == 302 else payloads[file.path])
        if response.status_code == 302:
            response.headers["Location"] = "https://faß.example/x"
        return response

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", send)
    client = HttpClient(NetworkGate(Settings(), Policy()), user_agent="Astra-Voice/test")
    loader = Downloader(client, store)
    sources: list[str] = []
    result = loader.download(
        item, progress=lambda state: None, cancel=threading.Event(), source=sources.append
    )
    assert (result / file.path).read_bytes() == payloads[file.path]
    assert sources == ["hf", "github"]
    assert [urlsplit(url).hostname for url in urls] == ["huggingface.co", "github.com"]
    assert all(url.isascii() and "xn--" not in url.lower() for url in urls)


def test_source_policy_error_keeps_code_and_message(
    loader: Downloader, entry: CatalogEntry
) -> None:
    error = _assert_error(loader, replace(entry, host="untrusted.example"), "not-allowed")
    assert error.message == "Источник не разрешён: сервер отсутствует в списке."
    assert DownloadError("not-allowed").message == "Источник загрузки не разрешён."
    assert DownloadError("bad-path").message != error.message


@pytest.mark.parametrize("size", (16, 65534, 65536, 98304))
@pytest.mark.parametrize("resumed", (False, True))
def test_oversize_read_limit_without_network(
    loader: Downloader,
    store: ModelStore,
    entry: CatalogEntry,
    monkeypatch: pytest.MonkeyPatch,
    size: int,
    resumed: bool,
) -> None:
    file = replace(entry.files[0], size=size)
    entry = replace(entry, files=(file,), size_bytes=size)
    offset = 5 if resumed else 0
    part = _part(store, entry, b"x" * offset)
    received = requests.Response()
    received.status_code = 206 if resumed else 200
    received.headers["Content-Length"] = str(size - offset)
    if resumed:
        received.headers["Content-Range"] = f"bytes {offset}-{size - 1}/{size}"
    body = BytesIO(b"x" * (size - offset + 10000))
    received.raw = body
    original_read = body.read
    read_sizes: list[int] = []

    def read(count: int) -> bytes:
        block = original_read(count)
        read_sizes.append(len(block))
        return block

    monkeypatch.setattr(received.raw, "read", read)
    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", Mock(return_value=received))
    _assert_error(loader, entry, "too-large")
    assert sum(read_sizes) == size - offset + 1
    chunk_size = downloader._CHUNK_SIZE
    # BytesIO отдаёт полные блоки; iter_chunks уменьшает последний до остатка
    # лимита size - offset + 1, без дополнительного чтения EOF.
    assert len(read_sizes) == (size - offset + chunk_size) // chunk_size
    assert not part.exists()
    assert not part.with_suffix("").exists()
    assert received.raw.closed


def test_disk_full_prevents_all_requests(
    loader: Downloader,
    store: ModelStore,
    single_entry: CatalogEntry,
    local_server: LocalServer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    disk_ok = Mock(return_value=False)
    monkeypatch.setattr(store, "disk_ok", disk_ok)
    _assert_error(loader, single_entry, "disk-full")
    disk_ok.assert_called_once_with(single_entry.size_bytes)
    assert local_server.requests == []
    assert local_server.calls == []
    assert not store.root.exists()


def test_network_gate_code_is_preserved(
    loader: Downloader,
    single_entry: CatalogEntry,
    local_server: LocalServer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    _assert_error(loader, single_entry, "no-network")
    assert local_server.requests == []


def test_cancel_in_middle_keeps_part(
    loader: Downloader,
    store: ModelStore,
    single_entry: CatalogEntry,
    local_server: LocalServer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cancel = threading.Event()
    now = [0.0]

    def monotonic() -> float:
        now[0] += 0.25
        return now[0]

    monkeypatch.setattr(downloader, "time", SimpleNamespace(monotonic=monotonic))

    def progress(state: Progress) -> None:
        assert 0 < state.bytes_done < state.bytes_total
        cancel.set()

    with pytest.raises(DownloadError) as caught:
        loader.download(single_entry, progress=progress, cancel=cancel)
    assert caught.value.code == "cancelled"
    file = single_entry.files[0]
    part = store.staging_dir(single_entry.id, single_entry.revision) / (file.path + ".part")
    assert part.read_bytes() == local_server.faults[file.url_path].body[:4]
    assert local_server.responses[-1]._response.raw.closed


def test_cancel_before_next_file(
    loader: Downloader, local_entry: CatalogEntry, local_server: LocalServer
) -> None:
    cancel = threading.Event()

    def progress(state: Progress) -> None:
        if state.bytes_done == local_entry.files[0].size:
            cancel.set()

    with pytest.raises(DownloadError) as caught:
        loader.download(local_entry, progress=progress, cancel=cancel)
    assert caught.value.code == "cancelled"
    assert len(local_server.requests) == 1


def test_broken_progress_callback_is_logged_and_does_not_stop_download(
    loader: Downloader,
    local_entry: CatalogEntry,
    caplog: pytest.LogCaptureFixture,
    payloads: dict[str, bytes],
) -> None:
    callback = Mock(side_effect=RuntimeError("Ошибка индикатора"))
    result = loader.download(local_entry, progress=callback, cancel=threading.Event())
    assert callback.call_count >= 3
    assert "Не удалось сообщить о ходе загрузки" in caplog.text
    assert {file.path: (result / file.path).read_bytes() for file in local_entry.files} == payloads


@pytest.mark.parametrize("existing", (b"", b"x" * 16, b"x" * 25))
def test_empty_complete_or_oversized_part_restarts(
    loader: Downloader,
    store: ModelStore,
    single_entry: CatalogEntry,
    local_server: LocalServer,
    existing: bytes,
) -> None:
    part = _part(store, single_entry, existing)
    file = single_entry.files[0]
    result = _download(loader, single_entry)
    assert (result / file.path).read_bytes() == local_server.faults[file.url_path].body
    assert "Range" not in local_server.requests[0][1]
    assert not part.exists()


@pytest.mark.parametrize("wrong_size", (False, True))
def test_bad_completed_file_is_replaced(
    loader: Downloader,
    store: ModelStore,
    single_entry: CatalogEntry,
    local_server: LocalServer,
    wrong_size: bool,
) -> None:
    file = single_entry.files[0]
    target = store.staging_dir(single_entry.id, single_entry.revision) / file.path
    target.parent.mkdir(parents=True)
    target.write_bytes(b"x" * (file.size - int(wrong_size)))
    _download(loader, single_entry)
    assert target.read_bytes() == local_server.faults[file.url_path].body
    assert len(local_server.requests) == 1


@pytest.mark.parametrize("status", (201, 206, 404, 503))
def test_bad_status_does_not_loop(
    loader: Downloader, single_entry: CatalogEntry, local_server: LocalServer, status: int
) -> None:
    local_server.faults[single_entry.files[0].url_path].status = status
    _assert_error(loader, single_entry, "bad-status")
    assert len(local_server.requests) == (2 if status < 400 else 1)


def test_fsync_precedes_replace_and_directory_fsync_follows(
    loader: Downloader,
    single_entry: CatalogEntry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operations: list[str] = []
    original_fsync, original_replace = os.fsync, os.replace

    def fsync(descriptor: int) -> None:
        operations.append("каталог" if stat.S_ISDIR(os.fstat(descriptor).st_mode) else "файл")
        original_fsync(descriptor)

    def replace_file(source: Path, target: Path) -> None:
        assert source.read_bytes() == b"0123456789abcdef"
        operations.append("переименование")
        original_replace(source, target)

    monkeypatch.setattr(os, "fsync", fsync)
    monkeypatch.setattr(os, "replace", replace_file)
    _download(loader, single_entry)
    assert operations[-3:] == ["файл", "переименование", "каталог"]


def test_disk_full_during_fsync_is_translated(
    loader: Downloader, single_entry: CatalogEntry, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os, "fsync", Mock(side_effect=OSError(errno.ENOSPC, "Нет места")))
    _assert_error(loader, single_entry, "disk-full")


@pytest.mark.parametrize("mode", ("slow-headers", "slow-body"))
def test_timeout_preserves_existing_part(
    loader: Downloader,
    store: ModelStore,
    single_entry: CatalogEntry,
    local_server: LocalServer,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    local_server.faults[single_entry.files[0].url_path].mode = mode
    part = _part(store, single_entry, b"0123")
    monkeypatch.setattr(downloader, "_FILE_DEADLINE_S", 0.1)
    _assert_error(loader, single_entry, "timeout")
    assert part.read_bytes() == (b"0123" if mode == "slow-headers" else b"01234567")


@pytest.mark.parametrize("escape", ("parent", "absolute", "directory-link", "part-link"))
def test_paths_resolve_inside_staging_before_mkdir_or_network(
    loader: Downloader,
    store: ModelStore,
    entry: CatalogEntry,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    escape: str,
) -> None:
    staging = store.staging_dir(entry.id, entry.revision)
    outside = tmp_path / "outside"
    outside.mkdir()
    forbidden = Mock(side_effect=AssertionError("Проверка пути должна предшествовать запросу"))
    monkeypatch.setattr(HttpClient, "get_stream", forbidden)
    if escape == "parent":
        path = "../../../outside/new/model.bin"
    elif escape == "absolute":
        path = str(outside / "new" / "model.bin")
    elif escape == "directory-link":
        (staging / "link").symlink_to(outside, target_is_directory=True)
        path = "link/new/model.bin"
    else:
        path = "model.bin"
        (staging / "model.bin.part").symlink_to(outside / "model.bin")
    file = replace(entry.files[0], path=path)
    error = _assert_error(loader, replace(entry, files=(file,), size_bytes=file.size), "bad-path")
    assert error.message == "Путь к файлу выходит за пределы каталога загрузки."
    assert list(outside.iterdir()) == []
    forbidden.assert_not_called()


def test_cancel_before_start_makes_no_request(
    loader: Downloader, entry: CatalogEntry, monkeypatch: pytest.MonkeyPatch
) -> None:
    forbidden = Mock(side_effect=AssertionError("Отменённая загрузка не должна обращаться к сети"))
    monkeypatch.setattr(HttpClient, "get_stream", forbidden)
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(DownloadError) as caught:
        loader.download(entry, progress=lambda state: None, cancel=cancel)
    assert caught.value.code == "cancelled"
    forbidden.assert_not_called()


def test_progress_throttle_window_and_cached_bytes(
    entry: CatalogEntry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Управляемые часы проверяют окно 5 с и обязательный отчёт даже раньше 200 мс."""
    now = [0.0]
    monkeypatch.setattr(downloader, "time", SimpleNamespace(monotonic=lambda: now[0]))
    progress: list[Progress] = []
    reporter = downloader._Reporter(entry, progress.append)
    reporter.done += 5  # Проверенные байты с диска не считаются сетевой скоростью.
    reporter.report(1, finished=True)
    assert progress[-1].speed_bps == 0
    assert progress[-1].eta_s is None
    now[0] = 0.1
    reporter.written(4)
    reporter.report(1)
    assert len(progress) == 1
    now[0] = 0.2
    reporter.written(4)
    reporter.report(1)
    assert len(progress) == 2
    assert progress[-1].speed_bps == pytest.approx(40)
    assert progress[-1].eta_s == pytest.approx((entry.size_bytes - 13) / 40)
    now[0] = 5.15
    reporter.written(2)
    reporter.report(2)
    assert progress[-1].speed_bps == pytest.approx(6 / 5)
    now[0] = 10.2
    reporter.report(2)
    assert progress[-1].speed_bps == 0
    assert progress[-1].eta_s is None
    reporter.report(3, finished=True)
    assert progress[-1].file_index == 3


# ── перебор источников каталога (mirrors) ──────────────────────────────────


class FakeResponse:
    """Ответ фейкового транспорта: ровно то, что читает загрузчик."""

    def __init__(self, body: bytes, status: int = 200) -> None:
        self.status = status
        self.headers: dict[str, str] = {}
        self._body = body
        self.closed = False

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self.closed = True

    def iter_chunks(self, size: int, *, limit: int) -> Iterator[bytes]:
        for start in range(0, len(self._body), size):
            yield self._body[start : start + size]


@dataclass
class FakeTransport:
    """Отдаёт файл только с перечисленных хостов; остальные «не отвечают»."""

    bodies: dict[str, bytes]
    working: set[str]
    failure: Exception
    urls: list[str] = field(default_factory=list)
    scopes: list[str] = field(default_factory=list)

    def get_stream(
        self,
        url: str,
        *,
        range_from: int | None = None,
        deadline_s: float,
        cancel: threading.Event,
        kind: str = "download",
        idle_timeout_s: float | None = None,
        scope: Literal["public", "corp"] = "public",
    ) -> FakeResponse:
        self.urls.append(url)
        self.scopes.append(scope)
        host = urlsplit(url).hostname or ""
        if host not in self.working:
            raise self.failure
        return FakeResponse(self.bodies[urlsplit(url).path])


@pytest.fixture
def mirrored(entry: CatalogEntry) -> CatalogEntry:
    return replace(
        entry,
        host="huggingface.co",
        mirrors=("github.com", "mirror.example"),
        files=entry.files[:1],
        size_bytes=entry.files[0].size,
    )


def _fake_loader(
    store: ModelStore,
    entry: CatalogEntry,
    payloads: dict[str, bytes],
    working: set[str],
    failure: Exception,
) -> tuple[Downloader, FakeTransport]:
    bodies = {file.url_path: payloads[file.path] for file in entry.files}
    transport = FakeTransport(bodies, working, failure)
    return Downloader(cast(HttpClient, transport), store), transport


@pytest.mark.parametrize(
    "failure",
    [
        http.NetworkError("host-unreachable", "Не удалось связаться с сервером."),
        http.NetworkError("timeout", "Время ожидания истекло."),
        DownloadError("bad-status"),
    ],
)
def test_network_failure_moves_to_next_source(
    store: ModelStore,
    mirrored: CatalogEntry,
    payloads: dict[str, bytes],
    failure: Exception,
) -> None:
    loader, transport = _fake_loader(store, mirrored, payloads, {"mirror.example"}, failure)
    progress: list[Progress] = []

    staging = loader.download(mirrored, progress=progress.append, cancel=threading.Event())

    file = mirrored.files[0]
    assert (staging / file.path).read_bytes() == payloads[file.path]
    assert transport.urls == [
        "https://huggingface.co" + file.url_path,
        "https://github.com" + file.url_path,
        "https://mirror.example" + file.url_path,
    ]
    # Прогресс не удваивается из-за неудачных источников.
    assert progress[-1].bytes_done == mirrored.size_bytes


def test_last_source_error_is_reported(
    store: ModelStore, mirrored: CatalogEntry, payloads: dict[str, bytes]
) -> None:
    loader, transport = _fake_loader(
        store, mirrored, payloads, set(), http.NetworkError("host-unreachable", "Нет сервера.")
    )

    error = _assert_error(loader, mirrored, "host-unreachable")

    assert len(transport.urls) == 3
    assert "huggingface" not in error.message and "github" not in error.message


@pytest.mark.parametrize(
    ("failure", "code"),
    [
        (http.NetworkError("not-allowed", "Источник не разрешён."), "not-allowed"),
        (http.NetworkError("no-network", "Нет доступа к сети."), "no-network"),
        (http.NetworkError("cancelled", "Отменено."), "cancelled"),
    ],
)
def test_non_network_failure_does_not_try_mirrors(
    store: ModelStore,
    mirrored: CatalogEntry,
    payloads: dict[str, bytes],
    failure: Exception,
    code: str,
) -> None:
    loader, transport = _fake_loader(store, mirrored, payloads, set(), failure)

    _assert_error(loader, mirrored, code)

    assert transport.urls == ["https://huggingface.co" + mirrored.files[0].url_path]


def test_checksum_is_checked_for_every_source(
    store: ModelStore, mirrored: CatalogEntry, payloads: dict[str, bytes]
) -> None:
    """Запасной источник не освобождён от проверки байтов."""
    file = mirrored.files[0]
    # Столько же байт, сколько обещает каталог, но другие: остаётся только sha256.
    bodies = {file.url_path: bytes(file.size)}
    transport = FakeTransport(
        bodies,
        {"github.com", "mirror.example"},
        http.NetworkError("host-unreachable", "Нет сервера."),
    )
    loader = Downloader(cast(HttpClient, transport), store)

    _assert_error(loader, mirrored, "bad-checksum")

    assert transport.urls[-1] == "https://mirror.example" + file.url_path


def test_poisoned_prefix_retries_same_source_from_zero(
    store: ModelStore,
    mirrored: CatalogEntry,
    payloads: dict[str, bytes],
    caplog: pytest.LogCaptureFixture,
) -> None:
    file = mirrored.files[0]
    body = payloads[file.path]
    seen: list[tuple[str, int | None]] = []

    class Poisoned(FakeResponse):
        def iter_chunks(self, size: int, *, limit: int) -> Iterator[bytes]:
            yield b"XX"
            raise http.NetworkError("host-unreachable", "Обрыв chunked-ответа.")

    def get_stream(url: str, *, range_from: int | None, **kwargs: object) -> FakeResponse:
        host = urlsplit(url).hostname or ""
        seen.append((host, range_from))
        if host == "huggingface.co":
            return Poisoned(body)
        assert host == "github.com"
        if range_from is not None:
            response = FakeResponse(body[range_from:], 206)
            response.headers["Content-Range"] = f"bytes {range_from}-{len(body) - 1}/{len(body)}"
            return response
        return FakeResponse(body)

    transport = Mock()
    transport.get_stream.side_effect = get_stream
    loader = Downloader(cast(HttpClient, transport), store)
    result = loader.download(mirrored, progress=lambda state: None, cancel=threading.Event())
    assert (result / file.path).read_bytes() == body
    assert seen == [("huggingface.co", None), ("github.com", 2), ("github.com", None)]
    assert "Источник 2 из 3 (github) отдал файл с неверной контрольной суммой" in caplog.text
    assert list(result.rglob("*.part")) == []


def test_bad_checksum_full_file_uses_next_source(
    store: ModelStore,
    mirrored: CatalogEntry,
    payloads: dict[str, bytes],
    caplog: pytest.LogCaptureFixture,
) -> None:
    file = mirrored.files[0]
    body = payloads[file.path]
    seen: list[str] = []

    def get_stream(url: str, **kwargs: object) -> FakeResponse:
        host = urlsplit(url).hostname or ""
        seen.append(host)
        return FakeResponse(b"x" * file.size if host == "huggingface.co" else body)

    transport = Mock()
    transport.get_stream.side_effect = get_stream
    loader = Downloader(cast(HttpClient, transport), store)
    result = loader.download(mirrored, progress=lambda state: None, cancel=threading.Event())
    assert (result / file.path).read_bytes() == body
    assert seen == ["huggingface.co", "github.com"]
    assert "Источник 1 из 3 (hf) отдал файл с неверной контрольной суммой" in caplog.text
    assert "huggingface.co" not in caplog.text


def test_all_sources_bad_checksum_removes_part(
    store: ModelStore, mirrored: CatalogEntry, payloads: dict[str, bytes]
) -> None:
    file = mirrored.files[0]
    transport = Mock()
    transport.get_stream.return_value = FakeResponse(b"x" * file.size)
    loader = Downloader(cast(HttpClient, transport), store)
    _assert_error(loader, mirrored, "bad-checksum")
    part = store.root / mirrored.id / f"{mirrored.revision}.partial" / (file.path + ".part")
    assert not part.exists()
    assert transport.get_stream.call_count == 3


def test_too_large_discards_part_and_uses_next_source(
    store: ModelStore, mirrored: CatalogEntry, payloads: dict[str, bytes]
) -> None:
    file = mirrored.files[0]
    body = payloads[file.path]
    seen: list[str] = []

    def get_stream(url: str, **kwargs: object) -> FakeResponse:
        host = urlsplit(url).hostname or ""
        seen.append(host)
        return FakeResponse(body + b"x" if host == "huggingface.co" else body)

    transport = Mock()
    transport.get_stream.side_effect = get_stream
    loader = Downloader(cast(HttpClient, transport), store)
    result = loader.download(mirrored, progress=lambda state: None, cancel=threading.Event())
    assert (result / file.path).read_bytes() == body
    assert seen == ["huggingface.co", "github.com"]
    assert list(result.rglob("*.part")) == []


def test_second_file_bad_checksum_keeps_verified_first_file(
    store: ModelStore, entry: CatalogEntry, payloads: dict[str, bytes]
) -> None:
    first, second = entry.files[:2]
    item = replace(entry, files=(first, second), size_bytes=first.size + second.size)
    transport = Mock()

    def get_stream(url: str, **kwargs: object) -> FakeResponse:
        path = urlsplit(url).path
        if path == first.url_path:
            return FakeResponse(payloads[first.path])
        return FakeResponse(b"x" * second.size)

    transport.get_stream.side_effect = get_stream
    loader = Downloader(cast(HttpClient, transport), store)
    _assert_error(loader, item, "bad-checksum")
    staging = store.root / item.id / f"{item.revision}.partial"
    assert (staging / first.path).read_bytes() == payloads[first.path]
    assert not (staging / (second.path + ".part")).exists()


def test_entry_without_mirrors_uses_single_source(
    store: ModelStore, entry: CatalogEntry, payloads: dict[str, bytes]
) -> None:
    entry = replace(
        entry,
        host="huggingface.co",
        files=entry.files[:1],
        size_bytes=entry.files[0].size,
    )
    loader, transport = _fake_loader(
        store, entry, payloads, set(), http.NetworkError("host-unreachable", "Нет сервера.")
    )

    _assert_error(loader, entry, "host-unreachable")

    assert transport.urls == ["https://huggingface.co" + entry.files[0].url_path]


@pytest.mark.parametrize("status", (429, 404, 403, 500))
def test_http_status_moves_to_next_source(
    store: ModelStore, mirrored: CatalogEntry, payloads: dict[str, bytes], status: int
) -> None:
    failure = http.NetworkError("bad-status", "Отказ сервера.", status=status)
    loader, transport = _fake_loader(store, mirrored, payloads, {"github.com"}, failure)
    kinds: list[str] = []
    loader.download(
        mirrored, progress=lambda state: None, cancel=threading.Event(), source=kinds.append
    )
    assert kinds == ["hf", "github"]
    assert [urlsplit(url).hostname for url in transport.urls] == ["huggingface.co", "github.com"]


def test_next_source_log_records_code_and_status_without_url(
    store: ModelStore,
    mirrored: CatalogEntry,
    payloads: dict[str, bytes],
    caplog: pytest.LogCaptureFixture,
) -> None:
    loader, _ = _fake_loader(
        store,
        mirrored,
        payloads,
        {"github.com"},
        http.NetworkError("bad-status", "Отказ сервера.", status=503),
    )
    loader.download(mirrored, progress=lambda state: None, cancel=threading.Event())
    assert "Источник 1 из 3 (hf) не ответил: bad-status 503; пробуем следующий" in caplog.text
    assert "huggingface.co" not in caplog.text
    assert mirrored.files[0].url_path not in caplog.text


def test_corp_is_first_and_uses_escaped_components(
    store: ModelStore, entry: CatalogEntry, caplog: pytest.LogCaptureFixture
) -> None:
    body = b"safe"
    file = FileSpec("folder name/a#b.bin", hashlib.sha256(body).hexdigest(), len(body), "/old")
    item = replace(
        entry,
        id="model.id",
        revision="rev-1",
        host="hf.co",
        mirrors=("objects.githubusercontent.com",),
        files=(file,),
        size_bytes=len(body),
    )
    corp_path = "/base/model.id/rev-1/folder%20name/a%23b.bin"
    transport = FakeTransport(
        {corp_path: body}, {"corp.example"}, http.NetworkError("host-unreachable", "Нет сервера.")
    )
    loader = Downloader(cast(HttpClient, transport), store, corp_base="https://corp.example/base/")
    kinds: list[str] = []
    result = loader.download(
        item, progress=lambda state: None, cancel=threading.Event(), source=kinds.append
    )
    assert (result / file.path).read_bytes() == body
    assert kinds == ["corp"]
    assert transport.scopes == ["corp"]
    assert transport.urls[0] == "https://corp.example" + corp_path
    assert ".." not in transport.urls[0]
    assert "corp.example" not in caplog.text
    assert "hf.co" not in caplog.text
    assert "objects.githubusercontent.com" not in caplog.text


def test_corp_failure_falls_back_to_hf_then_github(
    store: ModelStore, mirrored: CatalogEntry, payloads: dict[str, bytes]
) -> None:
    file = mirrored.files[0]
    transport = FakeTransport(
        {file.url_path: payloads[file.path]},
        {"github.com"},
        http.NetworkError("host-unreachable", "Нет сервера."),
    )
    loader = Downloader(cast(HttpClient, transport), store, corp_base="https://corp.example")
    kinds: list[str] = []
    loader.download(
        mirrored, progress=lambda state: None, cancel=threading.Event(), source=kinds.append
    )
    assert kinds == ["corp", "hf", "github"]
    assert transport.scopes == ["corp", "public", "public"]
    assert [urlsplit(url).hostname for url in transport.urls] == [
        "corp.example",
        "huggingface.co",
        "github.com",
    ]


@pytest.mark.parametrize(
    "base",
    (
        None,
        "http://corp.example",
        "https://user:pass@corp.example",
        "https://user@corp.example",
        "https://corp.example/..",
        "https://corp.example/%2e%2e/x",
        "https://corp.example/%252e%252e/x",
        "https://faß.example",
        "https://corp.example\\x",
        "https://corp.example/a b",
        "https:///x",
        "https://corp.example:abc",
        "https://corp.example:0",
        "https://corp.example?x=1",
        "https://corp.example#f",
    ),
)
def test_invalid_or_absent_corp_is_not_used(
    store: ModelStore,
    mirrored: CatalogEntry,
    payloads: dict[str, bytes],
    base: str | None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    item = replace(mirrored, mirrors=("github.com",))
    loader, transport = _fake_loader(
        store, item, payloads, set(), http.NetworkError("host-unreachable", "Нет сервера.")
    )
    loader = Downloader(cast(HttpClient, transport), store, corp_base=base)
    _assert_error(loader, item, "host-unreachable")
    assert [urlsplit(url).hostname for url in transport.urls] == ["huggingface.co", "github.com"]
    assert base is None or base not in caplog.text
    assert "Корпоративный источник не используется" not in caplog.text


def test_corp_rejects_dot_segment_before_request(store: ModelStore, entry: CatalogEntry) -> None:
    file = replace(entry.files[0], path="folder/../weights.bin")
    item = replace(entry, files=(file,), size_bytes=file.size)
    transport = Mock()
    loader = Downloader(cast(HttpClient, transport), store, corp_base="https://corp.example")
    _assert_error(loader, item, "bad-path")
    transport.get_stream.assert_not_called()


@pytest.mark.parametrize(
    ("host", "kind"),
    (
        ("huggingface.co", "hf"),
        ("cdn.huggingface.co", "hf"),
        ("hf.co", "hf"),
        ("cdn.hf.co", "hf"),
        ("github.com", "github"),
        ("objects.githubusercontent.com", "github"),
        ("api.github.com", "github"),
    ),
)
def test_source_kind_for_catalog_hosts(host: str, kind: str) -> None:
    assert downloader._source_kind(host) == kind


def test_source_callback_failure_does_not_stop_or_log_exception(
    store: ModelStore,
    mirrored: CatalogEntry,
    payloads: dict[str, bytes],
    caplog: pytest.LogCaptureFixture,
) -> None:
    loader, _ = _fake_loader(
        store,
        mirrored,
        payloads,
        {"github.com"},
        http.NetworkError("host-unreachable", "Нет сервера."),
    )

    def broken(kind: str) -> None:
        raise RuntimeError("secret-host")

    loader.download(mirrored, progress=lambda state: None, cancel=threading.Event(), source=broken)
    assert "Не удалось сообщить об источнике" in caplog.text
    assert "secret-host" not in caplog.text


@pytest.mark.parametrize(
    ("code", "reason"),
    (
        ("no-network", "no-network"),
        ("host-unreachable", "no-network"),
        ("short-read", "no-network"),
        ("not-allowed", "not-allowed"),
        ("bad-path", "not-allowed"),
        ("bad-checksum", "bad-sha"),
        ("too-large", "bad-sha"),
        ("disk-full", "no-space"),
        ("timeout", "timeout"),
        ("bad-status", "server"),
        ("other", "server"),
    ),
)
def test_fail_reason_is_closed(code: str, reason: str) -> None:
    assert fail_reason(code) == reason


def test_cancelled_has_no_fail_reason() -> None:
    with pytest.raises(ValueError):
        fail_reason("cancelled")


def test_second_source_resumes_half_and_progress_stays_monotonic(
    store: ModelStore,
    mirrored: CatalogEntry,
    payloads: dict[str, bytes],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = payloads[mirrored.files[0].path]
    seen: list[tuple[str, int | None]] = []

    class BrokenResponse(FakeResponse):
        def iter_chunks(self, size: int, *, limit: int) -> Iterator[bytes]:
            yield body[: len(body) // 2]
            raise http.NetworkError("timeout", "Обрыв.")

    def get_stream(url: str, *, range_from: int | None, **kwargs: object) -> FakeResponse:
        host = urlsplit(url).hostname or ""
        seen.append((host, range_from))
        if host == "huggingface.co":
            return BrokenResponse(body)
        assert host == "github.com" and range_from == len(body) // 2
        response = FakeResponse(body[range_from:], 206)
        response.headers["Content-Range"] = f"bytes {range_from}-{len(body) - 1}/{len(body)}"
        return response

    transport = Mock()
    transport.get_stream.side_effect = get_stream
    loader = Downloader(cast(HttpClient, transport), store)
    monkeypatch.setattr(downloader, "_PROGRESS_INTERVAL_S", 0)
    progress: list[Progress] = []
    result = loader.download(mirrored, progress=progress.append, cancel=threading.Event())
    assert (result / mirrored.files[0].path).read_bytes() == body
    assert seen == [("huggingface.co", None), ("github.com", len(body) // 2)]
    amounts = [state.bytes_done for state in progress]
    assert amounts == sorted(amounts)
    assert max(amounts) == len(body)


def test_short_read_uses_next_source_with_range_without_socket(
    store: ModelStore,
    mirrored: CatalogEntry,
    payloads: dict[str, bytes],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    file = mirrored.files[0]
    body = payloads[file.path]
    seen: list[tuple[str, int | None]] = []

    def get_stream(url: str, *, range_from: int | None, **kwargs: object) -> FakeResponse:
        host = urlsplit(url).hostname or ""
        seen.append((host, range_from))
        if host == "huggingface.co":
            return FakeResponse(body[:7])
        assert host == "github.com" and range_from == 7
        response = FakeResponse(body[7:], 206)
        response.headers["Content-Range"] = f"bytes 7-{len(body) - 1}/{len(body)}"
        return response

    transport = Mock()
    transport.get_stream.side_effect = get_stream
    loader = Downloader(cast(HttpClient, transport), store)
    monkeypatch.setattr(downloader, "_PROGRESS_INTERVAL_S", 0)
    progress: list[Progress] = []
    result = loader.download(mirrored, progress=progress.append, cancel=threading.Event())
    assert (result / file.path).read_bytes() == body
    assert seen == [("huggingface.co", None), ("github.com", 7)]
    assert [state.bytes_done for state in progress] == sorted(
        state.bytes_done for state in progress
    )


def test_short_read_last_source_keeps_part_for_retry_without_socket(
    store: ModelStore, entry: CatalogEntry, payloads: dict[str, bytes]
) -> None:
    file = entry.files[0]
    item = replace(entry, files=(file,), size_bytes=file.size)
    body = payloads[file.path]
    seen: list[int | None] = []

    def get_stream(url: str, *, range_from: int | None, **kwargs: object) -> FakeResponse:
        seen.append(range_from)
        if range_from is None:
            return FakeResponse(body[:7])
        response = FakeResponse(body[range_from:], 206)
        response.headers["Content-Range"] = f"bytes {range_from}-{len(body) - 1}/{len(body)}"
        return response

    transport = Mock()
    transport.get_stream.side_effect = get_stream
    loader = Downloader(cast(HttpClient, transport), store)
    _assert_error(loader, item, "short-read")
    part = store.root / item.id / f"{item.revision}.partial" / (file.path + ".part")
    assert part.read_bytes() == body[:7]
    result = _download(loader, item)
    assert (result / file.path).read_bytes() == body
    assert seen == [None, 7]


def test_last_source_idle_timeout_preserves_part(
    store: ModelStore,
    entry: CatalogEntry,
    payloads: dict[str, bytes],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    file = entry.files[0]
    item = replace(entry, files=(file,), size_bytes=file.size)
    body = payloads[file.path]

    class StalledResponse(FakeResponse):
        def iter_chunks(self, size: int, *, limit: int) -> Iterator[bytes]:
            yield body[:4]
            raise http.NetworkError("timeout", "Простой.")

    transport = Mock()
    transport.get_stream.return_value = StalledResponse(body)
    loader = Downloader(cast(HttpClient, transport), store)
    monkeypatch.setattr(downloader, "INACTIVITY_TIMEOUT_S", 0.2)
    _assert_error(loader, item, "timeout")
    part = store.staging_dir(item.id, item.revision) / (file.path + ".part")
    assert part.read_bytes() == body[:4]
    assert transport.get_stream.call_args.kwargs["idle_timeout_s"] == 0.2


def test_real_stream_idle_timeout_keeps_written_part_without_socket(
    client: HttpClient,
    store: ModelStore,
    entry: CatalogEntry,
    payloads: dict[str, bytes],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    file = entry.files[0]
    item = replace(entry, files=(file,), size_bytes=file.size)
    body = payloads[file.path]

    class StalledBody(BytesIO):
        reads = 0

        def read(self, size: int | None = -1) -> bytes:
            self.reads += 1
            if self.reads == 2:
                time.sleep(0.3)
            return super().read(size)

    response = requests.Response()
    response.status_code = 200
    response.headers["Content-Length"] = str(len(body))
    response.raw = StalledBody(body)
    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", Mock(return_value=response))
    monkeypatch.setattr(downloader, "_CHUNK_SIZE", 4)
    monkeypatch.setattr(downloader, "INACTIVITY_TIMEOUT_S", 0.2)
    _assert_error(Downloader(client, store), item, "timeout")
    part = store.staging_dir(item.id, item.revision) / (file.path + ".part")
    assert part.read_bytes() == body[:4]


def test_disk_check_pauses_with_part_and_next_call_resumes(
    store: ModelStore,
    entry: CatalogEntry,
    payloads: dict[str, bytes],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    file = entry.files[0]
    item = replace(entry, files=(file,), size_bytes=file.size)
    body = payloads[file.path]
    offsets: list[int | None] = []

    def get_stream(url: str, *, range_from: int | None, **kwargs: object) -> FakeResponse:
        offsets.append(range_from)
        if range_from is None:
            return FakeResponse(body)
        response = FakeResponse(body[range_from:], 206)
        response.headers["Content-Range"] = f"bytes {range_from}-{len(body) - 1}/{len(body)}"
        return response

    transport = Mock()
    transport.get_stream.side_effect = get_stream
    loader = Downloader(cast(HttpClient, transport), store)
    monkeypatch.setattr(downloader, "_CHUNK_SIZE", 4)
    now = [0.0]

    def monotonic() -> float:
        now[0] += 0.6
        return now[0]

    monkeypatch.setattr(downloader, "time", SimpleNamespace(monotonic=monotonic))
    disk_ok = Mock(side_effect=[True, True, False, True, True, True, True])
    monkeypatch.setattr(store, "disk_ok", disk_ok)
    _assert_error(loader, item, "disk-full")
    part = store.staging_dir(item.id, item.revision) / (file.path + ".part")
    assert part.read_bytes() == body[:4]
    assert (store.staging_dir(item.id, item.revision) / file.path).exists() is False
    result = _download(loader, item)
    assert (result / file.path).read_bytes() == body
    assert offsets == [None, 4]
    assert disk_ok.call_args_list[0].args == (item.size_bytes,)
    assert disk_ok.call_args_list[2].args == (item.size_bytes - 4,)
    assert disk_ok.call_args_list[3].args == (item.size_bytes - 4,)


def test_remaining_bytes_uses_sizes_without_reading_files(
    store: ModelStore, entry: CatalogEntry, monkeypatch: pytest.MonkeyPatch
) -> None:
    staging = store.staging_dir(entry.id, entry.revision)
    ready, partial, invalid = entry.files
    ready_path = staging / ready.path
    ready_path.parent.mkdir(parents=True, exist_ok=True)
    ready_path.write_bytes(b"x" * ready.size)
    partial_path = staging / (partial.path + ".part")
    partial_path.parent.mkdir(parents=True, exist_ok=True)
    partial_path.write_bytes(b"ab")
    invalid_path = staging / (invalid.path + ".part")
    invalid_path.parent.mkdir(parents=True, exist_ok=True)
    invalid_path.write_bytes(b"x" * (invalid.size + 1))

    monkeypatch.setattr(downloader, "_ready", Mock(side_effect=AssertionError("SHA check")))
    monkeypatch.setattr(Path, "open", Mock(side_effect=AssertionError("file read")))

    assert downloader.remaining_bytes(store, entry) == partial.size - 2 + invalid.size
