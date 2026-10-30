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
from collections.abc import Callable, Mapping
from pathlib import Path
from queue import Empty, SimpleQueue
from typing import TYPE_CHECKING, Any, NoReturn

from astra_voice.core.audio_env import deny_pulse_autospawn
from astra_voice.core.logging import setup_logging
from astra_voice.worker import ipc

if TYPE_CHECKING:
    from astra_voice.worker.audio import AudioSource
    from astra_voice.worker.state import WorkerState as WorkerStateType

Message = dict[str, Any]


def WorkerState(*, on_event: Callable[[Message], None]) -> WorkerStateType:
    """Legacy injection seam; state/audio imports are deferred to dictation."""
    from astra_voice.worker.state import WorkerState as State

    return State(on_event=on_event)


logger = logging.getLogger(__name__)
PR_SET_PDEATHSIG = 1
PR_SET_DUMPABLE = 4
PR_GET_DUMPABLE = 3
MIN_ADDRESS_SPACE = 3 * 1024 * 1024 * 1024
# Запас к дедлайну в секунду на отправку и освобождение устройства.
POLL_INTERVAL = 0.25
MAX_SEND_BUFFER = 4 * 1024 * 1024


def audio_backend(env: Mapping[str, str]) -> tuple[str, bool]:
    """Имя бэкенда записи и признак неизвестного значения переменной."""
    # С 29.09 по умолчанию stream (живой прогон 11/11); simple — запасной путь через env.
    value = env.get("ASTRA_VOICE_AUDIO_BACKEND")
    if value == "simple":
        return "simple", False
    return "stream", value not in (None, "stream")


def _log_backend_fallback() -> None:
    """По журналу видно, какой бэкенд записи работает на самом деле."""
    logger.info("Бэкенд записи: simple (откат)")


def select_audio_source(env: Mapping[str, str]) -> AudioSource:
    """Выбирает источник только по переданному окружению, не открывая устройство."""
    if audio_backend(env)[0] == "simple":
        from astra_voice.worker.audio import PulseSimpleSource

        return PulseSimpleSource()
    from astra_voice.worker.pulse_stream import StreamWithFallback

    return StreamWithFallback(on_fallback=_log_backend_fallback)


def _prctl(option: int, value: int) -> None:
    """Вызывает prctl и превращает отказ libc в исключение."""
    libc = ctypes.CDLL("libc.so.6", use_errno=True)
    if libc.prctl(option, value, 0, 0, 0) != 0:
        errno = ctypes.get_errno()
        raise OSError(errno, os.strerror(errno))


def _get_dumpable() -> int:
    libc = ctypes.CDLL("libc.so.6", use_errno=True)
    result = int(libc.prctl(PR_GET_DUMPABLE, 0, 0, 0, 0))
    if result < 0:
        raise OSError("meeting-hardening-failed")
    return result


def set_oom_score_adj(path: Path = Path("/proc/self/oom_score_adj"), *, score: int = 500) -> bool:
    """Повышает приоритет завершения воркера при нехватке памяти."""
    try:
        path.write_text(str(score), encoding="ascii")
    except Exception:
        try:
            if path.read_text(encoding="ascii").strip() == str(score):
                logger.debug("значение уже установлено родительским бутстрапом")
                return True
        except (OSError, UnicodeError):
            pass
        logger.warning("Не удалось установить oom_score_adj.")
        return False
    return True


def harden_process(parent_pid: int, *, strict: bool = False, oom_score: int = 500) -> bool:
    """Привязывает процесс к родителю и запрещает дампы; ошибки не фатальны."""
    try:
        _prctl(PR_SET_PDEATHSIG, signal.SIGTERM)
    except Exception:
        logger.warning("Не удалось установить сигнал смерти родителя.")
        if strict:
            return False
    # Проверка закрывает гонку между запоминанием pid и установкой prctl.
    if os.getppid() != parent_pid or parent_pid == 1:
        return False
    try:
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    except Exception:
        logger.warning("Не удалось запретить core dump через RLIMIT_CORE.")
        if strict:
            return False
    if strict:
        if not set_oom_score_adj(score=oom_score):
            return False
    else:
        set_oom_score_adj()
    try:
        _prctl(PR_SET_DUMPABLE, 0)
    except Exception:
        logger.warning("Не удалось запретить дампы через prctl.")
        if strict:
            return False
    if strict:
        try:
            if resource.getrlimit(resource.RLIMIT_CORE) != (0, 0) or _get_dumpable() != 0:
                return False
        except (OSError, ValueError):
            return False
    return True


def harden_meeting_limits(role: str) -> bool:
    """Initial role limits; M4 raises transcribe's floor from trusted model RAM."""
    requested = {
        "meeting-capture": 512 * 1024**2,
        "meeting-transcribe": 2 * 1024**3,
        "meeting-diar": 1536 * 1024**2,
    }.get(role)
    if requested is None:
        return False
    try:
        _, hard = resource.getrlimit(resource.RLIMIT_AS)
        actual = requested if hard == resource.RLIM_INFINITY else min(requested, hard)
        if actual <= 0:
            return False
        resource.setrlimit(resource.RLIMIT_AS, (actual, hard))
        if resource.getrlimit(resource.RLIMIT_AS) != (actual, hard):
            return False
        if role != "meeting-capture":
            current = os.getpriority(os.PRIO_PROCESS, 0)
            if current != 19:
                os.nice(19 - current)
            if os.getpriority(os.PRIO_PROCESS, 0) != 19:
                return False
    except (OSError, ValueError):
        return False
    return True


def meeting_main(role: str, argv: list[str]) -> int:
    """Strict inherited-socket entry point. No audio/model constructors or files.

    Bootstrap's role branch arrives here before legacy WorkerLoop construction.
    Unknown roles, malformed fd/environment and hardening failures refuse work.
    The only socket is the inherited private AF_UNIX/SOCK_STREAM connection.
    """
    from astra_voice.worker.meeting_contract import ROLES
    from astra_voice.worker.meeting_roles import run_role

    raw_fd = argv[0] if argv else os.environ.get("ASTRA_VOICE_IPC_FD")
    if (
        role not in ROLES
        or len(argv) > 1
        or raw_fd is None
        or len(raw_fd) > 10
        or not raw_fd.isascii()
        or not raw_fd.isdecimal()
    ):
        return 2
    fd = int(raw_fd)
    if fd < 3 or fd > 2147483647:
        return 2
    if role == "meeting-diar" and (
        "HOME" in os.environ or os.environ.get("TMPDIR") != "/nonexistent/astra-voice-diar"
    ):
        return 2
    parent_pid = os.getppid()
    if not harden_process(
        parent_pid, strict=True, oom_score=300 if role == "meeting-capture" else 500
    ):
        return 3
    if not harden_meeting_limits(role):
        return 3
    try:
        connection = socket.socket(fileno=fd)
    except (OSError, ValueError, OverflowError):
        return 2
    if connection.family != socket.AF_UNIX or connection.type != socket.SOCK_STREAM:
        connection.close()
        return 2
    return run_role(connection, role, parent_pid=parent_pid)


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

    def __init__(
        self,
        connection: socket.socket,
        *,
        parent_pid: int | None = None,
        capture: bool = True,
        terminate: Callable[[int], NoReturn] = os._exit,
    ) -> None:
        self.connection = connection
        self._terminate = terminate
        self.parent_pid = os.getppid() if parent_pid is None else parent_pid
        self._events: SimpleQueue[Message] = SimpleQueue()
        self._wake_r, self._wake_w = socket.socketpair()
        self._wake_r.setblocking(False)
        self._wake_w.setblocking(False)
        self.worker = WorkerState(on_event=self._put_event)
        if capture:
            from astra_voice.worker.audio import AudioCapture

            source = select_audio_source(os.environ)
            backend, unknown = audio_backend(os.environ)
            if unknown:
                # Значение не пишем: оно может быть длинным или содержать управляющие символы.
                logger.warning("Неизвестный ASTRA_VOICE_AUDIO_BACKEND; выбран stream.")
            logger.info("Бэкенд записи: %s", backend)

            self.worker.set_capture(
                AudioCapture(
                    source=source,
                    on_samples=self.worker.on_samples,
                    on_event=self._put_event,
                    on_error=self.worker.on_error,
                    on_device_change=self.worker.on_device_change,
                )
            )
        self._reader = ipc.FrameReader()
        self._send_buffer = bytearray()

    def _put_event(self, message: Message) -> None:
        """Ставит событие в очередь и будит цикл для немедленной отправки."""
        self._events.put(message)
        try:
            self._wake_w.send(b"\0")
        except (BlockingIOError, OSError):
            # Переполнение буфера означает, что будильник уже взведён;
            # закрытый сокет означает, что цикл завершается.
            pass

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
            # TODO (см. docs/status.md): join захвата перед заданием добавляет до 20 мс.
            replies = self.worker.handle(message)
            for reply in replies:
                if (
                    reply["type"] == "error"
                    and "utterance_id" in message
                    and "utterance_id" not in reply
                ):
                    reply = {
                        **reply,
                        "utterance_id": message["utterance_id"],
                    }
                    reply.setdefault("request_type", message["type"])
                self._send(reply)
        return True

    def _fatal_capture_timeout(self) -> NoReturn:
        """Записывает причину и завершает процесс, чтобы ядро закрыло аудиосокет."""
        try:
            logger.critical("Не удалось закрыть источник захвата. Воркер завершается.")
            current: logging.Logger | None = logger
            while current is not None:
                for handler in current.handlers:
                    handler.flush()
                current = current.parent if current.propagate else None
        finally:
            # Обычный выход раскручивает стек и запускает cleanup/ожидание потоков.
            # Они могут зависнуть или освободить дескриптор во время C-вызова.
            self._terminate(1)

    def run(self) -> int:
        """Шлёт hello, обслуживает IPC и освобождает ресурсы при потере родителя."""
        from astra_voice.worker.audio import CaptureStopTimeout

        fatal = False
        try:
            self.connection.setblocking(False)
            self._send(ipc.make_hello())
            while os.getppid() == self.parent_pid:
                self.worker.check_capture_watchdog()
                # Ограничиваем порцию, чтобы события не вытеснили проверку родителя.
                for _ in range(64):
                    try:
                        event = self._events.get_nowait()
                    except Empty:
                        break
                    try:
                        self._send(event)
                    except ipc.FrameError as exc:
                        logger.warning("Не удалось отправить событие воркера.")
                        self._send(ipc.error(exc.code, exc.message))
                readable, writable, _ = select.select(
                    [self.connection, self._wake_r],
                    [self.connection] if self._send_buffer else [],
                    [],
                    POLL_INTERVAL,
                )
                if os.getppid() != self.parent_pid:
                    break
                if self._wake_r in readable:
                    try:
                        self._wake_r.recv(4096)
                    except (BlockingIOError, OSError):
                        pass
                if self.connection in readable:
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
        except CaptureStopTimeout:
            fatal = True
            self._fatal_capture_timeout()
        except BufferError:
            logger.warning("Переполнен буфер отправки: родитель не читает IPC.")
        except OSError:
            logger.warning("Соединение с родителем закрыто или недоступно.")
        finally:
            if not fatal:
                try:
                    self.worker.close()
                except CaptureStopTimeout:
                    fatal = True
                    self._fatal_capture_timeout()
                finally:
                    if not fatal:
                        self.connection.close()
                        self._wake_r.close()
                        self._wake_w.close()
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
    deny_pulse_autospawn()
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
