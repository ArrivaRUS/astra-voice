"""Процесс воркера: самозащита, связь с родителем и транспорт автомата."""

from __future__ import annotations

import ctypes
import logging
import os
import resource
import select
import signal
import socket
import sys
from pathlib import Path
from queue import Empty, SimpleQueue

from astra_voice.core.logging import setup_logging
from astra_voice.worker import ipc
from astra_voice.worker.state import Message, WorkerState

logger = logging.getLogger(__name__)
PR_SET_PDEATHSIG = 1
PR_SET_DUMPABLE = 4
MIN_ADDRESS_SPACE = 3 * 1024 * 1024 * 1024
# Запас к дедлайну в секунду на отправку и освобождение устройства.
POLL_INTERVAL = 0.25
MAX_SEND_BUFFER = 4 * 1024 * 1024


def _prctl(option: int, value: int) -> None:
    """Вызывает prctl и превращает отказ libc в исключение."""
    libc = ctypes.CDLL("libc.so.6", use_errno=True)
    if libc.prctl(option, value, 0, 0, 0) != 0:
        errno = ctypes.get_errno()
        raise OSError(errno, os.strerror(errno))


def set_oom_score_adj(path: Path = Path("/proc/self/oom_score_adj")) -> None:
    """Повышает приоритет завершения воркера при нехватке памяти."""
    try:
        path.write_text("500", encoding="ascii")
    except Exception:
        try:
            if path.read_text(encoding="ascii").strip() == "500":
                logger.debug("значение уже установлено родительским бутстрапом")
                return
        except (OSError, UnicodeError):
            pass
        logger.warning("Не удалось установить oom_score_adj.")


def harden_process(parent_pid: int) -> bool:
    """Привязывает процесс к родителю и запрещает дампы; ошибки не фатальны."""
    try:
        _prctl(PR_SET_PDEATHSIG, signal.SIGTERM)
    except Exception:
        logger.warning("Не удалось установить сигнал смерти родителя.")
    # Проверка закрывает гонку между запоминанием pid и установкой prctl.
    if os.getppid() != parent_pid or parent_pid == 1:
        return False
    try:
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    except Exception:
        logger.warning("Не удалось запретить core dump через RLIMIT_CORE.")
    set_oom_score_adj()
    try:
        _prctl(PR_SET_DUMPABLE, 0)
    except Exception:
        logger.warning("Не удалось запретить дампы через prctl.")
    return True


def apply_address_space_limit(min_ram_mb: int | None) -> int:
    """Ставит мягкий RLIMIT_AS, сохраняя жёсткий; возвращает действующий предел."""
    requested = max((min_ram_mb or 0) * 3 * 1024 * 1024, MIN_ADDRESS_SPACE)
    soft, hard = resource.getrlimit(resource.RLIMIT_AS)
    # Конечный жёсткий предел, заданный родителем, нельзя повысить без привилегий.
    actual = requested if hard == resource.RLIM_INFINITY else min(requested, hard)
    try:
        resource.setrlimit(resource.RLIMIT_AS, (actual, hard))
    except Exception:
        logger.warning("Не удалось установить RLIMIT_AS.")
        return soft
    return actual


class WorkerLoop:
    """Обслуживает готовый сокет; события рабочего потока идут через очередь."""

    def __init__(self, connection: socket.socket, *, parent_pid: int | None = None) -> None:
        self.connection = connection
        self.parent_pid = os.getppid() if parent_pid is None else parent_pid
        self._events: SimpleQueue[Message] = SimpleQueue()
        self.worker = WorkerState(on_event=self._events.put)
        self._reader = ipc.FrameReader()
        self._send_buffer = bytearray()

    def _send(self, message: Message) -> None:
        """Ставит кадр в ограниченный буфер, не записывая его в журнал."""
        frame = ipc.encode(message)
        if len(self._send_buffer) + len(frame) > MAX_SEND_BUFFER:
            raise BufferError("Переполнен буфер отправки: родитель не читает IPC.")
        self._send_buffer.extend(frame)

    def _flush_send(self) -> None:
        """Делает одну неблокирующую отправку, сохраняя недописанный остаток."""
        if not self._send_buffer:
            return
        try:
            sent = self.connection.send(self._send_buffer)
        except BlockingIOError:
            return
        if sent == 0:
            raise ConnectionError("Сокет больше не принимает данные.")
        del self._send_buffer[:sent]

    def _receive(self, data: bytes) -> bool:
        """Обрабатывает кадры, восстанавливаясь после безопасных ошибок протокола."""
        while True:
            try:
                messages = self._reader.feed(data)
                break
            except ipc.FrameError as exc:
                self._send(ipc.error(exc.code, exc.message))
                if exc.code == ipc.FRAME_TOO_LARGE:
                    return False
                # feed сохраняет соседние кадры: обрабатываем их без нового recv.
                data = b""
        for message in messages:
            if os.getppid() != self.parent_pid:
                return False
            if message["type"] == "model.load":
                apply_address_space_limit(int(message["min_ram_mb"]))
            replies = self.worker.handle(message)
            for reply in replies:
                self._send(reply)
        return True

    def run(self) -> int:
        """Шлёт hello, обслуживает IPC и освобождает ресурсы при потере родителя."""
        try:
            self.connection.setblocking(False)
            self._send(ipc.make_hello())
            while os.getppid() == self.parent_pid:
                # Ограничиваем порцию, чтобы события не вытеснили проверку родителя.
                for _ in range(64):
                    try:
                        event = self._events.get_nowait()
                    except Empty:
                        break
                    self._send(event)
                readable, writable, _ = select.select(
                    [self.connection],
                    [self.connection] if self._send_buffer else [],
                    [],
                    POLL_INTERVAL,
                )
                if os.getppid() != self.parent_pid:
                    break
                if readable:
                    try:
                        data = self.connection.recv(64 * 1024)
                    except BlockingIOError:
                        pass
                    else:
                        if not data:
                            break
                        if not self._receive(data):
                            # Ошибку протокола отправляем без ожидания читателя.
                            self._flush_send()
                            break
                if writable:
                    self._flush_send()
        except BufferError:
            logger.warning("Переполнен буфер отправки: родитель не читает IPC.")
        except OSError:
            logger.warning("Соединение с родителем закрыто или недоступно.")
        finally:
            try:
                self.worker.close()
            finally:
                self.connection.close()
        return 0


def main(argv: list[str] | None = None) -> int:
    """Разбирает унаследованный fd, защищает процесс и запускает цикл воркера."""
    parent_pid = os.getppid()
    args = sys.argv[1:] if argv is None else argv
    raw_fd = args[0] if args else os.environ.get("ASTRA_VOICE_IPC_FD")
    try:
        if raw_fd is None:
            raise ValueError
        fd = int(raw_fd)
        if fd < 0:
            raise ValueError
    except ValueError:
        sys.stderr.write(
            "astra-voice worker: требуется числовой IPC fd (argv или ASTRA_VOICE_IPC_FD)\n"
        )
        return 2

    setup_logging(session_kind="WORKER")
    if not harden_process(parent_pid):
        return 0
    apply_address_space_limit(None)
    try:
        connection = socket.socket(fileno=fd)
    except (OSError, ValueError):
        sys.stderr.write("astra-voice worker: IPC fd не является доступным сокетом\n")
        return 2
    if connection.family != socket.AF_UNIX or connection.type != socket.SOCK_STREAM:
        connection.close()
        sys.stderr.write("astra-voice worker: требуется сокет AF_UNIX/SOCK_STREAM\n")
        return 2
    return WorkerLoop(connection, parent_pid=parent_pid).run()
