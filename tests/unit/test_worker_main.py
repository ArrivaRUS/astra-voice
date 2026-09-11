"""Транспорт воркера и самозащита без fork, дисплея и тяжёлого рантайма."""

from __future__ import annotations

import ctypes
import errno
import logging
import os
import resource
import select
import signal
import socket
import struct
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from threading import Thread
from typing import NoReturn
from unittest.mock import Mock, call

import pytest

from astra_voice import bootstrap
from astra_voice.worker import ipc
from astra_voice.worker import main as worker_main
from astra_voice.worker.audio import AudioCapture, CaptureStopTimeout
from astra_voice.worker.state import Message, WorkerState

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def no_onnxruntime(monkeypatch: pytest.MonkeyPatch) -> None:
    """Запрещает реальный импорт ORT даже при установленном пакете."""
    monkeypatch.setitem(sys.modules, "onnxruntime", None)


def receive(peer: socket.socket, count: int = 1) -> list[Message]:
    """Читает указанное число полных ответов с ограничением времени."""
    reader = ipc.FrameReader()
    messages: list[Message] = []
    while len(messages) < count:
        data = peer.recv(65536)
        assert data, "Неожиданный EOF"
        messages.extend(reader.feed(data))
    return messages


@contextmanager
def running_loop(
    *,
    send_buffer_size: int | None = None,
) -> Iterator[tuple[worker_main.WorkerLoop, socket.socket, Thread]]:
    """Запускает только поток и гарантированно освобождает оба конца сокета."""
    connection, peer = socket.socketpair()
    if send_buffer_size is not None:
        connection.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, send_buffer_size)
    peer.settimeout(2)
    loop = worker_main.WorkerLoop(connection)
    results: list[int] = []
    thread = Thread(target=lambda: results.append(loop.run()), daemon=True)
    thread.start()
    try:
        yield loop, peer, thread
    finally:
        peer.close()
        thread.join(2)
        assert not thread.is_alive()
        assert results == [0]
        assert connection.fileno() == -1


def test_main_missing_fd(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Без аргумента и окружения возвращается ошибка использования."""
    monkeypatch.delenv("ASTRA_VOICE_IPC_FD", raising=False)
    assert worker_main.main([]) == 2
    assert "IPC fd" in capsys.readouterr().err


def test_main_invalid_fd(capsys: pytest.CaptureFixture[str]) -> None:
    """Нечисловой аргумент не запускает самозащиту процесса тестов."""
    assert worker_main.main(["не-число"]) == 2
    assert "IPC fd" in capsys.readouterr().err


@pytest.mark.parametrize("use_env", [False, True])
def test_main_inherited_fd(monkeypatch: pytest.MonkeyPatch, use_env: bool) -> None:
    """main принимает fd из окружения; позиционный аргумент имеет приоритет."""
    connection, peer = socket.socketpair()
    fd = connection.detach()
    setup = Mock()
    harden = Mock(return_value=True)
    limit = Mock(return_value=worker_main.MIN_ADDRESS_SPACE)
    monkeypatch.setattr(worker_main, "setup_logging", setup)
    monkeypatch.setattr(worker_main, "harden_process", harden)
    monkeypatch.setattr(worker_main, "apply_address_space_limit", limit)
    monkeypatch.setenv("ASTRA_VOICE_IPC_FD", str(fd) if use_env else "не-число")

    def run(loop: worker_main.WorkerLoop) -> int:
        assert loop.connection.fileno() == fd
        assert loop.connection.family == socket.AF_UNIX
        assert loop.connection.type == socket.SOCK_STREAM
        loop.worker.close()
        loop.connection.close()
        return 0

    monkeypatch.setattr(worker_main.WorkerLoop, "run", run)
    try:
        assert worker_main.main([] if use_env else [str(fd)]) == 0
    finally:
        peer.close()
    setup.assert_called_once_with(session_kind="WORKER")
    harden.assert_called_once()
    limit.assert_called_once_with(None)


def test_hello_then_ping() -> None:
    """Первый кадр — hello версии 1; разделённый на части ping даёт pong."""
    with running_loop() as (_, peer, _):
        hello = receive(peer)[0]
        assert hello["type"] == "hello"
        assert hello["protocol"] == 1
        frame = ipc.encode({"type": "ping"})
        peer.sendall(frame[:2])
        peer.sendall(frame[2:])
        assert receive(peer) == [{"type": "pong"}]


@pytest.mark.parametrize("failure", ["poll", "command", "disconnect", "parent_changed"])
def test_capture_timeout_terminates_worker_without_cleanup(
    failure: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """T-40: сторож в цикле и при завершении ведёт к fatal, flush, затем _exit."""

    class Terminated(BaseException):
        """Заменяет выход процесса только в тесте."""

    calls: list[str | int] = []
    handler = logging.Handler()
    monkeypatch.setattr(handler, "emit", lambda record: calls.append("fatal"))
    monkeypatch.setattr(handler, "flush", lambda: calls.append("flush"))
    monkeypatch.setattr(worker_main.logger, "handlers", [handler])

    def terminate(code: int) -> NoReturn:
        calls.append(code)
        raise Terminated

    connection = Mock(spec=socket.socket)
    connection.recv.return_value = (
        ipc.encode({"type": "audio.close"}) if failure == "command" else b""
    )
    monkeypatch.setattr(select, "select", lambda *args: ([connection], [], []))
    loop = worker_main.WorkerLoop(
        connection,
        capture=False,
        parent_pid=-1 if failure == "parent_changed" else os.getppid(),
        terminate=terminate,
    )
    capture = Mock(spec=AudioCapture)
    if failure == "poll":
        capture.check_stop_watchdog.side_effect = CaptureStopTimeout
    else:
        capture.stop.side_effect = CaptureStopTimeout
    loop.worker.set_capture(capture)
    close = Mock(wraps=loop.worker.close)
    monkeypatch.setattr(loop.worker, "close", close)
    try:
        with pytest.raises(Terminated):
            loop.run()
        assert calls == ["fatal", "flush", 1]
        assert (
            worker_main.__name__,
            logging.CRITICAL,
            "Поток захвата завис в libpulse после отмены. Воркер завершается.",
        ) in caplog.record_tuples
        assert close.call_count == int(failure in {"disconnect", "parent_changed"})
        connection.close.assert_not_called()
        # audio.closed нельзя выдать, если ожидание владельца завершилось сторожем.
        assert [
            message["type"] for message in ipc.FrameReader().feed(bytes(loop._send_buffer))
        ] == ["hello"]
    finally:
        capture.stop.side_effect = None
        loop.worker.close()
        # При имитации _exit закрываем созданный executor вручную после проверок.
        loop.worker._executor.shutdown(wait=True)


@pytest.mark.parametrize("kind", ["record.start", "record.stop", "record.cancel", "recognize"])
def test_command_error_inherits_correlation(kind: str) -> None:
    """У45/T-44: общая ветка bad-state коррелируется для каждой команды с id."""
    loop = worker_main.WorkerLoop(Mock(spec=socket.socket), capture=False)
    loop.worker.close()
    requests = [{"type": kind, "utterance_id": uid} for uid in ("u1", "u2")]
    assert loop._receive(b"".join(ipc.encode(request) for request in requests))
    replies = ipc.FrameReader().feed(bytes(loop._send_buffer))
    assert replies == [
        {
            **ipc.error("bad-state", "Воркер закрыт."),
            "utterance_id": request["utterance_id"],
            "request_type": kind,
        }
        for request in requests
    ]


@pytest.mark.parametrize(
    "reply",
    [
        {**ipc.error("bad-state", "Тест."), "utterance_id": "original"},
        {
            **ipc.error("bad-state", "Тест."),
            "utterance_id": "original",
            "request_type": "recognize",
        },
        {"type": "audio.ready"},
        {"type": "cancelled", "utterance_id": "u1"},
    ],
)
def test_receive_preserves_correlated_errors_and_other_replies(
    monkeypatch: pytest.MonkeyPatch, reply: Message
) -> None:
    """Дополнение У45 сохраняет исходную корреляцию и ответы других типов."""
    loop = worker_main.WorkerLoop(Mock(spec=socket.socket), capture=False)
    original = dict(reply)
    monkeypatch.setattr(loop.worker, "handle", Mock(return_value=[reply]))
    try:
        assert loop._receive(ipc.encode({"type": "record.start", "utterance_id": "u1"}))
        assert ipc.FrameReader().feed(bytes(loop._send_buffer)) == [original]
        assert reply == original
    finally:
        loop.worker.close()


def test_error_without_request_id_stays_uncorrelated() -> None:
    """Ошибке команды без id транспорт не приписывает корреляцию диктовки."""
    loop = worker_main.WorkerLoop(Mock(spec=socket.socket), capture=False)
    loop.worker.close()
    assert loop._receive(ipc.encode({"type": "ping"}))
    assert ipc.FrameReader().feed(bytes(loop._send_buffer)) == [
        ipc.error("bad-state", "Воркер закрыт.")
    ]


def test_slow_reader_preserves_pending_frames() -> None:
    """Пауза чтения дольше прежнего таймаута не мешает дослать кадры по порядку."""
    with running_loop(send_buffer_size=4096) as (loop, peer, thread):
        receive(peer)
        events = [
            {"type": "result", "utterance_id": f"u{i}", "text": "я" * 16000, "t_ms": i}
            for i in range(16)
        ]
        for event in events:
            loop._events.put(event)
        thread.join(2 * worker_main.POLL_INTERVAL)
        assert thread.is_alive()
        assert 0 < len(loop._send_buffer) < sum(len(ipc.encode(event)) for event in events)
        assert receive(peer, len(events)) == events
        peer.sendall(ipc.encode({"type": "ping"}))
        assert receive(peer) == [{"type": "pong"}]


@pytest.mark.parametrize("parent_changed", [False, True])
def test_disconnect_with_pending_output(
    monkeypatch: pytest.MonkeyPatch, parent_changed: bool
) -> None:
    """EOF и смена родителя завершают цикл за секунду при заполненном сокете."""
    parent = [123]
    monkeypatch.setattr(os, "getppid", lambda: parent[0])
    with running_loop(send_buffer_size=4096) as (loop, peer, thread):
        receive(peer)
        for i in range(16):
            loop._events.put(
                {"type": "result", "utterance_id": f"u{i}", "text": "я" * 16000, "t_ms": i}
            )
        thread.join(2 * worker_main.POLL_INTERVAL)
        assert thread.is_alive()
        assert loop._send_buffer
        started = time.monotonic()
        if parent_changed:
            parent[0] = 1
        else:
            # Полузакрытие даёт именно EOF, сохраняя непрочитанные ответы.
            peer.shutdown(socket.SHUT_WR)
        thread.join(1)
        assert not thread.is_alive()
        assert time.monotonic() - started <= 1
        assert loop.connection.fileno() == -1


def test_nonblocking_send_and_receive(monkeypatch: pytest.MonkeyPatch) -> None:
    """EAGAIN и частичная запись сохраняют остаток и не останавливают цикл."""
    connection = Mock(spec=socket.socket)
    hello = ipc.make_hello()
    frame = ipc.encode(hello)
    attempts: list[bytes] = []

    def send(data: bytearray) -> int:
        attempts.append(bytes(data))
        if len(attempts) == 1:
            raise BlockingIOError(errno.EAGAIN, "Сокет временно занят")
        return 3 if len(attempts) == 2 else len(data)

    connection.send.side_effect = send
    connection.recv.side_effect = [BlockingIOError(errno.EAGAIN, "Нет данных"), b""]
    readiness = Mock(
        side_effect=[
            ([], [connection], []),
            ([], [connection], []),
            ([], [connection], []),
            ([connection], [], []),
            ([connection], [], []),
        ]
    )
    monkeypatch.setattr(select, "select", readiness)
    monkeypatch.setattr(ipc, "make_hello", lambda: hello)
    loop = worker_main.WorkerLoop(connection)
    assert loop.run() == 0
    assert attempts == [frame, frame, frame[3:]]
    assert not loop._send_buffer
    assert (
        readiness.call_args_list
        == [
            call([connection], [connection], [], worker_main.POLL_INTERVAL),
        ]
        * 3
        + [call([connection], [], [], worker_main.POLL_INTERVAL)] * 2
    )
    connection.setblocking.assert_called_once_with(False)
    connection.settimeout.assert_not_called()
    connection.close.assert_called_once_with()


def test_oversized_event_becomes_error_and_loop_continues(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Длинная подпись не роняет воркер и не мешает следующим событиям и ping."""
    connection = Mock(spec=socket.socket)
    sent = bytearray()

    def send(data: bytearray) -> int:
        sent.extend(data)
        return len(data)

    connection.send.side_effect = send
    connection.recv.side_effect = [ipc.encode({"type": "ping"}), b""]
    monkeypatch.setattr(
        select,
        "select",
        Mock(side_effect=[([connection], [connection], []), ([connection], [], [])]),
    )
    loop = worker_main.WorkerLoop(connection, capture=False)
    label = "Личная метка " + "я" * ipc.MAX_FRAME_BYTES
    loop._events.put({"type": "audio.ready", "device": label})
    loop._events.put({"type": "audio.ready", "device": "Микрофон"})
    with caplog.at_level(logging.WARNING, logger="astra_voice.worker.main"):
        assert loop.run() == 0
    messages = ipc.FrameReader().feed(bytes(sent))
    assert messages[0]["type"] == "hello"
    assert messages[1:] == [
        ipc.error(ipc.FRAME_TOO_LARGE, "Тело кадра превышает 64 КиБ."),
        {"type": "audio.ready", "device": "Микрофон"},
        {"type": "pong"},
    ]
    assert caplog.messages == ["Не удалось отправить событие воркера."]
    assert "Личная метка" not in caplog.text
    connection.close.assert_called_once_with()


@pytest.mark.parametrize("error_number", [errno.EPIPE, errno.ECONNRESET])
def test_fatal_send_error_closes_loop(monkeypatch: pytest.MonkeyPatch, error_number: int) -> None:
    """Настоящий обрыв при записи завершает цикл и освобождает ресурсы."""
    connection = Mock(spec=socket.socket)
    connection.send.side_effect = OSError(error_number, "Обрыв соединения")
    monkeypatch.setattr(select, "select", Mock(return_value=([], [connection], [])))
    loop = worker_main.WorkerLoop(connection)
    close = Mock()
    monkeypatch.setattr(loop.worker, "close", close)
    assert loop.run() == 0
    close.assert_called_once_with()
    connection.close.assert_called_once_with()


def test_send_buffer_overflow_closes_loop(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Буфер ограничен 4 МиБ; переполнение завершает цикл с предупреждением."""
    connection = Mock(spec=socket.socket)
    loop = worker_main.WorkerLoop(connection)
    event = {"type": "result", "utterance_id": "u1", "text": "я" * 16000, "t_ms": 1}
    assert worker_main.MAX_SEND_BUFFER == 4 * 1024 * 1024
    for _ in range(worker_main.MAX_SEND_BUFFER // len(ipc.encode(event)) + 1):
        loop._events.put(event)
    readiness = Mock(return_value=([], [], []))
    monkeypatch.setattr(select, "select", readiness)
    close = Mock()
    monkeypatch.setattr(loop.worker, "close", close)
    assert loop.run() == 0
    assert len(loop._send_buffer) <= worker_main.MAX_SEND_BUFFER
    assert readiness.call_count >= 2
    assert (
        worker_main.__name__,
        logging.WARNING,
        "Переполнен буфер отправки: родитель не читает IPC.",
    ) in caplog.record_tuples
    close.assert_called_once_with()
    connection.close.assert_called_once_with()


def test_eof_closes_audio_and_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    """EOF во время записи закрывает источник, сокет и цикл быстрее секунды."""
    source = Mock()

    def make_state(*, on_event: Callable[[Message], None]) -> WorkerState:
        return WorkerState(audio_source=source, on_event=on_event)

    monkeypatch.setattr(worker_main, "WorkerState", make_state)
    with running_loop() as (loop, peer, thread):
        receive(peer)
        peer.sendall(ipc.encode({"type": "record.start", "utterance_id": "u1"}))
        peer.sendall(ipc.encode({"type": "ping"}))
        assert receive(peer) == [{"type": "pong"}]
        started = time.monotonic()
        peer.close()
        thread.join(1)
        assert not thread.is_alive()
        assert time.monotonic() - started <= 1
        assert loop.connection.fileno() == -1
        source.close.assert_called_once_with()
        assert not loop.worker.buffers


def test_parent_changed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Смена родителя завершает цикл, даже когда IPC ещё открыт."""
    parent = [123]
    monkeypatch.setattr(os, "getppid", lambda: parent[0])
    with running_loop() as (loop, peer, thread):
        receive(peer)
        started = time.monotonic()
        parent[0] = 1
        thread.join(1)
        assert not thread.is_alive()
        assert time.monotonic() - started <= 1
        assert loop.connection.fileno() == -1
        assert peer.recv(1) == b""


def test_oversized_frame_disconnects() -> None:
    """Заявленные 2 ГиБ отвергаются по заголовку без чтения тела."""
    with running_loop() as (_, peer, thread):
        receive(peer)
        peer.sendall(struct.pack(">I", 2 * 1024**3))
        response = receive(peer)[0]
        assert response["type"] == "error"
        assert response["code"] == ipc.FRAME_TOO_LARGE
        thread.join(1)
        assert not thread.is_alive()
        assert peer.recv(1) == b""


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        (b"{", ipc.BAD_FRAME),
        (b'{"type":"unknown"}', ipc.UNKNOWN_MESSAGE),
        (b'{"type":"record.start"}', ipc.BAD_FIELD),
    ],
)
def test_recoverable_error_preserves_frames(payload: bytes, code: str) -> None:
    """Ошибочный кадр между двумя ping не теряет соседние сообщения."""
    with running_loop() as (_, peer, _):
        receive(peer)
        ping = ipc.encode({"type": "ping"})
        peer.sendall(ping + struct.pack(">I", len(payload)) + payload + ping)
        responses = receive(peer, 3)
        assert responses[0]["code"] == code
        assert responses[1:] == [{"type": "pong"}, {"type": "pong"}]
        peer.sendall(ping)
        assert receive(peer) == [{"type": "pong"}]


def test_events_from_background_thread(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Callback автомата передаёт результат целым кадром и не раскрывает текст в журнале."""
    callbacks: list[Callable[[Message], None]] = []

    def make_state(*, on_event: Callable[[Message], None]) -> WorkerState:
        callbacks.append(on_event)
        return WorkerState(on_event=on_event)

    monkeypatch.setattr(worker_main, "WorkerState", make_state)
    with running_loop() as (_, peer, _):
        receive(peer)
        event = {"type": "result", "utterance_id": "u1", "text": "секретная фраза", "t_ms": 1}
        sender = Thread(target=callbacks[0], args=(event,))
        sender.start()
        peer.sendall(ipc.encode({"type": "ping"}))
        responses = receive(peer, 2)
        sender.join(1)
        assert not sender.is_alive()
        assert event in responses
        assert {"type": "pong"} in responses
    assert "секретная фраза" not in caplog.text


def test_model_load_updates_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Лимит из сообщения применяется строго до handle и до загрузки движка."""
    calls = Mock()
    original_handle = WorkerState.handle

    def handle(state: WorkerState, message: Message) -> list[Message]:
        calls.limit.assert_called_once_with(2048)
        assert state.min_ram_mb != 2048
        calls.handle(message)
        return original_handle(state, message)

    calls.engine.side_effect = RuntimeError

    def make_state(*, on_event: Callable[[Message], None]) -> WorkerState:
        return WorkerState(on_event=on_event, engine_factory=calls.engine)

    monkeypatch.setattr(WorkerState, "handle", handle)
    monkeypatch.setattr(worker_main, "WorkerState", make_state)
    monkeypatch.setattr(worker_main, "apply_address_space_limit", calls.limit)
    with running_loop() as (_, peer, _):
        receive(peer)
        peer.sendall(
            ipc.encode(
                {
                    "type": "model.load",
                    "id": "fake",
                    "revision": "r1",
                    "dir": "/unused",
                    "layout": "fake",
                    "variant": "int8",
                    "threads": 2,
                    "min_ram_mb": 2048,
                }
            )
        )
        assert receive(peer)[0]["code"] == "engine-failed"
        assert [entry[0] for entry in calls.mock_calls] == ["limit", "handle", "engine"]


@pytest.mark.parametrize("write_fails", [False, True])
def test_oom_score_adj(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    write_fails: bool,
) -> None:
    """Попытка записи никогда не переключает dumpable; отказ даёт предупреждение."""
    path = (tmp_path / "missing" if write_fails else tmp_path) / "oom_score_adj"
    calls = Mock()
    calls.write.side_effect = path.write_text
    monkeypatch.setattr(worker_main, "_prctl", calls.prctl)
    monkeypatch.setattr(Path, "write_text", calls.write)
    caplog.set_level(logging.DEBUG, logger=worker_main.__name__)

    worker_main.set_oom_score_adj(path)

    assert calls.mock_calls == [call.write("500", encoding="ascii")]
    warning = (worker_main.__name__, logging.WARNING, "Не удалось установить oom_score_adj.")
    if write_fails:
        assert warning in caplog.record_tuples
    else:
        assert warning not in caplog.record_tuples
        assert path.read_text(encoding="ascii") == "500"


@pytest.mark.parametrize("current_value", ["500\n", "0\n"])
def test_oom_score_adj_write_denied(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    current_value: str,
) -> None:
    """При отказе записи предупреждаем только о неустановленном значении."""
    path = tmp_path / "oom_score_adj"
    path.write_text(current_value, encoding="ascii")
    write = Mock(side_effect=PermissionError)
    monkeypatch.setattr(Path, "write_text", write)
    caplog.set_level(logging.DEBUG, logger=worker_main.__name__)

    worker_main.set_oom_score_adj(path)

    write.assert_called_once_with("500", encoding="ascii")
    warning = (worker_main.__name__, logging.WARNING, "Не удалось установить oom_score_adj.")
    debug = (
        worker_main.__name__,
        logging.DEBUG,
        "значение уже установлено родительским бутстрапом",
    )
    assert (warning in caplog.record_tuples) == (current_value == "0\n")
    assert (debug in caplog.record_tuples) == (current_value == "500\n")
    assert path.read_text(encoding="ascii") == current_value


@pytest.mark.parametrize("command", ["worker", "app"])
@pytest.mark.parametrize("failure", [None, "core", "write", "prctl"])
def test_bootstrap_hardening_order(
    monkeypatch: pytest.MonkeyPatch, command: str, failure: str | None
) -> None:
    """Только worker пишет oom_score_adj между RLIMIT_CORE и запретом дампов."""
    calls = Mock()
    if failure is not None:
        getattr(calls, failure).side_effect = OSError
    monkeypatch.setattr(resource, "setrlimit", calls.core)
    monkeypatch.setattr(Path, "write_text", calls.write)
    monkeypatch.setattr(ctypes, "CDLL", Mock(return_value=calls))

    bootstrap._harden(command)

    expected = [call.core(resource.RLIMIT_CORE, (0, 0))]
    if command == "worker":
        expected.append(call.write("500", encoding="ascii"))
    expected.append(call.prctl(4, 0, 0, 0, 0))
    assert calls.mock_calls == expected


def test_bootstrap_worker_writes_oom_before_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    """Диспетчер передаёт команду в защиту и пишет именно /proc/self/oom_score_adj."""
    calls = Mock()

    def write(path: Path, text: str, *, encoding: str) -> int:
        calls.write(path, text, encoding=encoding)
        return len(text)

    monkeypatch.setattr(resource, "setrlimit", calls.core)
    monkeypatch.setattr(Path, "write_text", write)
    monkeypatch.setattr(ctypes, "CDLL", Mock(return_value=calls))
    monkeypatch.setattr(bootstrap, "_setup_sys_path", Mock())
    monkeypatch.setattr(worker_main, "main", calls.entry)
    calls.entry.return_value = 0

    assert bootstrap.main(["worker", "123"]) == 0
    assert calls.mock_calls == [
        call.core(resource.RLIMIT_CORE, (0, 0)),
        call.write(Path("/proc/self/oom_score_adj"), "500", encoding="ascii"),
        call.prctl(4, 0, 0, 0, 0),
        call.entry(["123"]),
    ]


@pytest.mark.parametrize(("min_ram_mb", "gib"), [(None, 3), (1024, 3), (2048, 6)])
def test_address_space_limit(
    monkeypatch: pytest.MonkeyPatch, min_ram_mb: int | None, gib: int
) -> None:
    """Возвращаемое значение проверяется без изменения лимитов процесса pytest."""
    set_limit = Mock()
    monkeypatch.setattr(resource, "setrlimit", set_limit)
    monkeypatch.setattr(resource, "getrlimit", lambda _: (-1, -1))
    assert worker_main.apply_address_space_limit(min_ram_mb) == gib * 1024**3
    set_limit.assert_called_once_with(resource.RLIMIT_AS, (gib * 1024**3, -1))


def test_address_space_preserves_hard_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Конечный жёсткий предел сохраняется, возвращается фактический мягкий."""
    set_limit = Mock()
    monkeypatch.setattr(resource, "setrlimit", set_limit)
    monkeypatch.setattr(resource, "getrlimit", lambda _: (1024**3, 2 * 1024**3))
    assert worker_main.apply_address_space_limit(2048) == 2 * 1024**3
    set_limit.assert_called_once_with(resource.RLIMIT_AS, (2 * 1024**3, 2 * 1024**3))


def test_hardening_checks_parent_immediately(monkeypatch: pytest.MonkeyPatch) -> None:
    """Проверка гонки идёт сразу после prctl, до остальных мер самозащиты."""
    calls: list[tuple[int, int] | str] = []
    monkeypatch.setattr(worker_main, "_prctl", lambda option, value: calls.append((option, value)))

    def getppid() -> int:
        calls.append("getppid")
        return 456

    monkeypatch.setattr(os, "getppid", getppid)
    core = Mock()
    monkeypatch.setattr(resource, "setrlimit", core)
    assert not worker_main.harden_process(123)
    assert calls == [(worker_main.PR_SET_PDEATHSIG, signal.SIGTERM), "getppid"]
    core.assert_not_called()


def test_hardening_failures_are_independent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Отказ каждой защиты не мешает применить остальные меры."""
    prctl = Mock(side_effect=OSError)
    core = Mock(side_effect=ValueError)
    oom = Mock()
    monkeypatch.setattr(os, "getppid", lambda: 123)
    monkeypatch.setattr(worker_main, "_prctl", prctl)
    monkeypatch.setattr(resource, "setrlimit", core)
    monkeypatch.setattr(worker_main, "set_oom_score_adj", oom)
    assert worker_main.harden_process(123)
    assert prctl.call_count == 2
    core.assert_called_once_with(resource.RLIMIT_CORE, (0, 0))
    oom.assert_called_once_with()
