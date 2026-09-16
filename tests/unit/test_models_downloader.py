import pytest

pytest.importorskip("requests")

# Импорт зависимости должен предшествовать импорту проверяемого модуля.
# ruff: noqa: E402
import errno
import hashlib
import os
import stat
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import BinaryIO, cast
from unittest.mock import MagicMock, Mock
from urllib.parse import urlsplit

import requests

from astra_voice.core.policy import Policy
from astra_voice.core.settings import Settings
from astra_voice.models import downloader
from astra_voice.models.catalog import CatalogEntry, FileSpec
from astra_voice.models.downloader import Downloader, DownloadError, Progress
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
            try:
                if fault.mode == "slow-headers":
                    state.release.wait(2)
                self.send_response(status)
                # Chunked намеренно сочетается с ложным Content-Length для проверки У20.
                self.send_header("Content-Length", str(1 if fault.mode == "chunked" else len(body)))
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


@pytest.mark.parametrize("corruption", ("same-size", "short", "resume-prefix"))
def test_bad_checksum_removes_part(
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
    error = _assert_error(loader, single_entry, "bad-checksum")
    if corruption == "short":
        assert "не полностью" in error.message
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


def test_redirect_to_foreign_host_preserves_not_allowed(
    loader: Downloader, single_entry: CatalogEntry, local_server: LocalServer
) -> None:
    local_server.faults[single_entry.files[0].url_path].mode = "redirect"
    _assert_error(loader, single_entry, "not-allowed")
    assert len(local_server.requests) == 1


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
    assert len(read_sizes) == (size - offset + 1024) // 1024
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
