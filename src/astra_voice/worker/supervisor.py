"""Супервизор дочернего воркера с поколениями, дедлайнами и адаптером Qt."""

from __future__ import annotations

import math
import os
import select
import signal
import socket
import subprocess
import sys
import time
from collections import deque
from collections.abc import Callable, Iterable
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import astra_voice
from astra_voice.worker import ipc

MAX_RESTARTS = 3
RESTART_WINDOW_S = 600
_POLL_INTERVAL_S = 0.05
_KILL_RETRY_INTERVAL_S = 2.0
Message = dict[str, Any]
RequestKey = tuple[str, str] | tuple[str, str, str]
_CORRELATION: dict[str, RequestKey] = {
    "ping": ("reply", "pong"),
    "model.load": ("reply", "model.loaded"),
    "measure": ("reply", "measured"),
    "audio.close": ("reply", "audio.closed"),
    "transcribe.file": ("utterance", "file", "transcribe.file"),
}
# Уровень и тишина — индикация во время записи; record.limit означает автоостановку,
# но распознавание этого utterance_id ещё впереди. audio.ready подтверждает открытие
# устройства, а не завершение команды; audio.device.changed — автоостановку из-за смены
# микрофона, после которой GUI ещё распознаёт записанное: эти уведомления не снимают ожидание.
_NOTIFICATIONS = {
    "level",
    "silent",
    "record.limit",
    "audio.ready",
    "audio.device.changed",
    "record.started",
    "record.stopped",
}


@dataclass
class _Pending:
    """Ожидание одного типа команды и номер последнего ответа при отправке."""

    deadline: float | None
    activity: int
    request_type: str
    timeout: float | None
    request_types: set[str] = field(default_factory=set)


def _correlation(message: Message) -> RequestKey:
    """Нормализует запрос, ответ и коррелированную ошибку в один ключ."""
    kind = str(message.get("request_type", message["type"]))
    if "req" in message:
        return ("req", str(message["req"]), kind)
    if "utterance_id" in message:
        return ("utterance", str(message["utterance_id"]), kind)
    return _CORRELATION.get(kind, ("reply", kind))


def launcher_path() -> Path:
    """Ищет лаунчер при вызове: сначала рядом с пакетом (И7), затем внутри него."""
    pkg = Path(astra_voice.__file__).resolve().parent
    dev_launcher = pkg / "bootstrap.py"
    for candidate in (pkg.parent / "bootstrap.py", dev_launcher):
        if candidate.is_file():
            return candidate
    return dev_launcher


def worker_command(fd: int, *, role: str = "worker") -> list[str]:
    """Составляет команду запуска воркера через лаунчер в изолированном Python."""
    from astra_voice.worker.meeting_contract import ROLES

    if role not in ROLES | {"worker"}:
        raise ValueError("worker-role-invalid")
    return [sys.executable, "-I", str(launcher_path()), role, str(fd)]


class WorkerSupervisor:
    """Обслуживается в потоке GUI: ``start()``, ``send()``, ``stop()``.

    В GUI ``use_qt=True`` требует уже созданного QCoreApplication. Без Qt нужно
    регулярно вызывать ``pump()``. Поколение первого запуска равно 1. ``stopped``
    терминален: для нового сеанса создаётся новый супервизор.

    ``send`` возвращает локальную копию запроса с поколением. На проводе поколение
    отсутствует: источником истины служит соединение. Идентификатор диктовки можно
    продолжать командами stop/recognize/cancel до первого завершения или таймаута.
    Повторное использование завершённого id в том же поколении запрещено,
    кроме record.cancel: отмена всегда отправляется и создаёт или переиспользует
    ожидание ответа, поскольку ошибка могла оставить микрофон открытым (У45).
    Для запросов без id допускается одно ожидание каждого типа ответа.

    Корреляция диктовки включает utterance_id и request_type; каждое ожидание
    имеет собственный дедлайн. transcribe.file использует служебный id "file".
    Результат завершает предыдущие стадии записи, но не отдельную отмену.
    Промежуточные level/silent/record.limit/audio.ready передаются потребителю,
    сохраняя ожидания фразы и продлевая их дедлайны, кроме ожидания отмены.
    Ошибка с utterance_id и request_type завершает точное ожидание; тип без id допустим,
    если определяет единственное ожидание. Ошибка захвата только с id передаётся
    отдельно без приписывания типа; снимается лишь единственное ожидание не-отмены
    (совместимость со старыми ответами). Ошибка без корреляции завершает только
    ожидания без диктовки (ключи reply) отдельными коррелированными событиями,
    сохраняя ожидания utterance для остановки и отмены записи (У45). Если ожиданий
    reply нет, такая ошибка всё равно передаётся потребителю через _emit.
    Коррелированные ошибки без ожидания и повторные завершения отбрасываются.
    Без уникального id протокол не позволяет отличить поздний ответ от ответа
    на повторный запрос того же типа.

    Дедлайн model.load всегда означает зависание; прочий дедлайн — если после
    отправки не принято ни одного ответа (hello не считается). Тогда SIGKILL и
    обычная политика рестартов завершают ожидания ошибками load-timeout со старым
    поколением. Если воркер отвечал, истекает только нужное ожидание с timeout.
    """

    def __init__(
        self,
        on_event: Callable[[Message], None],
        *,
        clock: Callable[[], float] = time.monotonic,
        command_factory: Callable[[int], list[str]] | None = None,
        use_qt: bool = True,
        launch_allowed: Callable[[], bool] = lambda: True,
        role: str = "worker",
        auto_restart: bool | None = None,
        on_exit: Callable[[int], None] = lambda code: None,
    ) -> None:
        from astra_voice.worker.meeting_contract import ROLES, RequestLedger

        if role not in ROLES | {"worker"}:
            raise ValueError("worker-role-invalid")
        self.role = role
        self.auto_restart = (
            role in {"worker", "meeting-transcribe"} if auto_restart is None else auto_restart
        )
        self._on_exit = on_exit
        self._hello_deadline = clock() + 3.0
        self._process_group = False
        self.close_state = "idle"
        self.meeting_generations: dict[str, int] = {}
        self._meeting_ledger = RequestLedger()
        self._meeting_requests: dict[RequestKey, Message] = {}
        self._meeting_end_deadlines: dict[RequestKey, float] = {}
        self._meeting_close_deadlines: dict[RequestKey, float] = {}
        self._meeting_exit_deadline: float | None = None
        self._exit_notifying = False
        self._stop_revision = 0
        self.generation = 1
        self.dropped_late = 0
        self.state: Literal["new", "running", "stopped"] = "new"
        self.process: subprocess.Popen[bytes] | None = None
        self._on_event = on_event
        self._clock = clock
        self._command_factory = command_factory or (lambda fd: worker_command(fd, role=role))
        self._use_qt = use_qt
        self._launch_allowed = launch_allowed
        self._hello_accepted = False
        self._socket: socket.socket | None = None
        self._reader = ipc.FrameReader(role=role)
        self._outgoing = bytearray()
        self._pending: dict[RequestKey, _Pending] = {}
        self._finished: set[RequestKey] = set()
        self._activity = 0
        self._restarts: deque[float] = deque()
        self._retired: list[subprocess.Popen[bytes]] = []
        self._restart_pending: tuple[int, list[Message], bool, bool] | None = None
        self._restart_kill_deadline: float | None = None
        self._notifier: Any = None
        self._timer: Any = None

    @property
    def restarting(self) -> bool:
        """Перезапуск ждёт подтверждения завершения прежнего процесса."""
        return self._restart_pending is not None

    @property
    def exited(self) -> bool:
        """Все процессы супервизора, включая ``_retired``, подтвердили выход (poll)."""
        processes = list(self._retired)
        if self.process is not None:
            processes.append(self.process)
        return all(process.poll() is not None for process in processes)

    @property
    def ready(self) -> bool:
        """Текущий воркер подтвердил совместимость протокола через hello."""
        return (
            self.state == "running"
            and not self.restarting
            and self._hello_accepted
            and self._meeting_exit_deadline is None
            and not self._exit_notifying
        )

    def start(self) -> None:
        """Запускает воркер один раз; повторный вызов при работе безопасен."""
        if self._exit_notifying:
            raise RuntimeError("meeting-exit-notifying")
        if self.state == "stopped":
            raise RuntimeError("Супервизор остановлен.")
        if self.state == "running":
            return
        if self._use_qt:
            from PyQt5.QtCore import QCoreApplication

            if QCoreApplication.instance() is None:
                raise RuntimeError("Нужен QCoreApplication или use_qt=False.")
        try:
            self._launch()
        except OSError:
            self.state = "stopped"
            self._emit(ipc.error("worker-start", "Не удалось запустить воркер."))

    def _launch(self) -> None:
        """Передаёт только дочерний fd и закрывает его копию в родителе."""
        self._hello_accepted = False
        self._hello_deadline = self._clock() + 3.0
        parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            parent.setblocking(False)
            options: dict[str, Any] = {"pass_fds": [child.fileno()]}
            if self.role != "worker":
                options["start_new_session"] = True
                options["stderr"] = subprocess.DEVNULL
                environment = dict(os.environ)
                if self.role == "meeting-diar":
                    environment.pop("HOME", None)
                    environment["TMPDIR"] = "/nonexistent/astra-voice-diar"
                options["env"] = environment
            process = subprocess.Popen(self._command_factory(child.fileno()), **options)
        except BaseException:
            parent.close()
            raise
        finally:
            child.close()
        self.process = process
        self._process_group = self.role != "worker"
        self._socket = parent
        self._reader = ipc.FrameReader(role=self.role)
        self.state = "running"
        if self._use_qt:
            self._attach_qt()

    def _attach_qt(self) -> None:
        """Подключает чтение и проверку дедлайнов к существующему циклу Qt."""
        from PyQt5.QtCore import QSocketNotifier, QTimer

        assert self._socket is not None
        self._notifier = QSocketNotifier(self._socket.fileno(), QSocketNotifier.Read)
        self._notifier.activated.connect(self._qt_ready)
        self._timer = QTimer()
        self._timer.timeout.connect(self.pump)
        self._timer.start(int(_POLL_INTERVAL_S * 1000))

    def _qt_ready(self, *args: Any) -> None:
        """Передаёт уведомление Qt общему неблокирующему обработчику."""
        self.pump()

    def _emit(self, message: Message, *, generation: int | None = None) -> None:
        """Отдаёт событие с локальным поколением, не записывая содержимое в лог."""
        self._on_event(
            {**message, "generation": self.generation if generation is None else generation}
        )

    def send(self, message: Message, *, timeout: float | None = None) -> Message:
        """Ставит кадр в очередь и регистрирует ожидание ответа.

        Без явного timeout команда с utterance_id, включая record.cancel,
        наследует срок последнего соседнего ожидания с таймаутом того же id.
        Это исходный абсолютный срок, а не новый интервал от момента отмены.
        """
        if self.state != "running" or self._restart_pending is not None or self._exit_notifying:
            raise RuntimeError("Воркер не запущен.")
        if timeout is not None and (not math.isfinite(timeout) or timeout < 0):
            raise ValueError("Таймаут должен быть конечным и неотрицательным.")
        if self.role != "worker":
            return self._send_meeting(message, timeout=timeout)
        frame = ipc.encode(message)
        request = {**ipc.decode(frame[4:]), "generation": self.generation}
        uid = request.get("utterance_id")
        kind = str(request["type"])
        if not self._hello_accepted and kind.startswith(("record.", "audio.")):
            raise RuntimeError("Воркер ещё не готов.")
        key = _correlation(request)
        if key[0] == "utterance" and key[:2] in self._finished and kind != "record.cancel":
            raise ValueError("Идентификатор уже завершён в текущем поколении.")
        if uid is None and key in self._pending:
            raise ValueError("Ответ на предыдущий запрос этого типа ещё ожидается.")
        inherited = None
        if uid is not None and timeout is None:
            inherited = next(
                (
                    p
                    for k, p in reversed(self._pending.items())
                    if k[:2] == key[:2] and p.timeout is not None
                ),
                None,
            )
            if inherited is not None:
                timeout = inherited.timeout
        deadline = (
            inherited.deadline
            if inherited is not None
            else None
            if timeout is None
            else self._clock() + timeout
        )
        expects_reply = uid is not None or kind in _CORRELATION or timeout is not None
        if expects_reply:
            pending = self._pending.get(key)
            if pending is None:
                pending = _Pending(deadline, self._activity, kind, timeout)
                self._pending[key] = pending
            elif timeout is not None:
                pending.deadline = deadline
                pending.timeout = timeout
                pending.activity = self._activity
            pending.request_type = kind
            pending.request_types.add(kind)
            self._finished.discard(key[:2])
        self._outgoing.extend(frame)
        self._flush()
        return request

    def set_meeting_generation(self, meeting_id: str, generation: int) -> None:
        """Controller-owned generation; process restarts never advance it.

        Cancellation/deletion must call this before allowing late events. No
        open/resume/extend (or any other command) is replayed by _launch.
        """
        from astra_voice.meetings.model import MEETING_ID, UINT
        from astra_voice.models.schema import validate

        validate(meeting_id, MEETING_ID)
        validate(generation, UINT)
        if generation < self.meeting_generations.get(meeting_id, 0):
            raise ValueError("meeting-generation-stale")
        self.meeting_generations[meeting_id] = generation
        if any(
            request.get("meeting_id") == meeting_id
            and request["meeting_generation"] != generation
            and key in self._meeting_close_deadlines
            for key, request in self._meeting_requests.items()
        ):
            # A cancellation generation cannot erase the physical-close duty.
            self._restart(report=False, failure_code="timeout")
            return
        # Retire old waits as well as results; they cannot time out and kill the
        # new job later. A new generation also requires fresh req identifiers.
        for key, request in list(self._meeting_requests.items()):
            if (
                request.get("meeting_id") == meeting_id
                and request["meeting_generation"] != generation
            ):
                self._finish(
                    key,
                    {
                        "type": "error",
                        "code": "cancelled-by-delete",
                        "message": "meeting-generation-retired",
                    },
                )

    def _send_meeting(self, message: Message, *, timeout: float | None) -> Message:
        from astra_voice.worker.meeting_contract import COMMAND_FIELDS, operation_timeout
        from astra_voice.worker.meeting_roles import failure

        if not self._hello_accepted:
            raise RuntimeError("meeting-role-not-ready")
        frame = ipc.encode(message, role=self.role)
        request = ipc.decode(frame[4:], role=self.role)
        if request["type"] not in COMMAND_FIELDS[self.role]:
            raise ValueError("meeting-command-invalid")
        if (
            self._meeting_exit_deadline is not None
            and request["req"] not in self._meeting_ledger.requests
        ):
            # Existing outcomes/conflicts remain queryable; never reserve new
            # work while waiting for process exit after bye.
            raise RuntimeError("meeting-role-exiting")
        if (
            ("req" not in request or request["req"] not in self._meeting_ledger.requests)
            and "meeting_id" in request
            and request["meeting_generation"]
            < self.meeting_generations.get(request["meeting_id"], 0)
        ):
            raise ValueError("meeting-generation-stale")
        state, cached = self._meeting_ledger.begin(request)
        if state == "same":
            for reply in cached or []:
                mid = reply.get("meeting_id")
                if mid is not None and self.meeting_generations.get(mid) != reply.get(
                    "meeting_generation"
                ):
                    self.dropped_late += 1
                    continue
                self._emit(reply)
            return {**request, "generation": self.generation}
        if state == "exhausted":
            generation = self.generation
            self._restart(report=False, failure_code="limit", relaunch=False)
            self._emit(failure(request, "limit"), generation=generation)
            return {**request, "generation": generation}
        if state in {"conflict", "limit"}:
            self._emit(failure(request, "req-conflict" if state == "conflict" else "limit"))
            return {**request, "generation": self.generation}
        if "meeting_id" in request:
            self.set_meeting_generation(request["meeting_id"], request["meeting_generation"])
            if self.state != "running" or self.restarting:
                raise RuntimeError("meeting-closing-unknown")
        if len(self._outgoing) + len(frame) > 4 * ipc.MAX_FRAME_BYTES:
            self._meeting_ledger.finish(request["req"], [failure(request, "limit")])
            self._emit(failure(request, "limit"))
            return {**request, "generation": self.generation}
        key = _correlation(request)
        ack_timeout = 1.0 if timeout is None else min(timeout, 1.0)
        self._pending[key] = _Pending(
            self._clock() + ack_timeout, self._activity, request["type"], ack_timeout
        )
        self._meeting_requests[key] = request
        self._meeting_end_deadlines[key] = self._clock() + (
            operation_timeout(request)
            if timeout is None
            else min(timeout, operation_timeout(request))
        )
        if request["type"] in {"meeting.capture.stop", "meeting.capture.pause"}:
            self._meeting_close_deadlines[key] = self._clock() + 1.0
            self.close_state = "closing"
        self._outgoing.extend(frame)
        self._flush()
        return {**request, "generation": self.generation}

    def _accept_meeting(self, message: Message, generation: int) -> None:
        from astra_voice.worker.meeting_contract import COMMAND_FIELDS

        if generation != self.generation:
            self.dropped_late += 1
            return
        kind = message["type"]
        if kind in COMMAND_FIELDS[self.role]:
            self.dropped_late += 1
            return
        if kind == "hello":
            self._hello_accepted = True
            self._emit(message)
            return
        mid = message.get("meeting_id")
        if mid is not None and self.meeting_generations.get(mid) != message.get(
            "meeting_generation"
        ):
            self.dropped_late += 1
            return
        if "req" not in message:
            self._activity += 1
            self._emit(message)
            return
        key = _correlation(message)
        if key not in self._pending:
            self.dropped_late += 1
            return
        request = self._meeting_requests[key]
        if any(
            request.get(field) != message.get(field)
            for field in ("meeting_id", "meeting_generation")
            + (() if kind == "error" else ("capture_id",))
            if field in request
        ):
            self.dropped_late += 1
            return
        self._activity += 1
        if kind == "error" and key in self._meeting_close_deadlines:
            self._restart(report=False, failure_code="timeout")
            return
        if kind == "meeting.capture.closed":
            self.close_state = "closed"
            self._meeting_close_deadlines.pop(key, None)
            self._pending[key].deadline = self._meeting_end_deadlines[key]
        elif kind == "meeting.capture.finalized":
            if key in self._meeting_close_deadlines:
                self.dropped_late += 1
                return
            self.close_state = "finalized"
            message = self._finish(key, message)
        elif kind in {"meeting.capture.paused", "meeting.capture.stopped"}:
            # Legacy aggregate notifications never substitute closed/finalized.
            self._pending[key].deadline = self._meeting_end_deadlines[key]
        elif kind == "meeting.ack":
            if request["type"] in {
                "meeting.capture.keepalive",
                "meeting.job.credit",
                "meeting.job.applied",
            }:
                message = self._finish(key, message)
            else:
                self._pending[key].deadline = self._meeting_end_deadlines[key]
        elif kind == "meeting.capture.bye":
            # Receipt never proves exit. Keep the original shutdown ceiling,
            # independently of the command wait removed by _finish.
            self._meeting_exit_deadline = self._meeting_end_deadlines[key]
            if not self.exited:
                self.close_state = "closing-unknown"
            self._update_capture_closed(terminal=True)
            message = self._finish(key, message)
        else:
            message = self._finish(key, message)
        self._emit(message)

    def _expire_meeting(self) -> None:
        if self.state != "running" or self.restarting:
            return
        now = self._clock()
        if self._meeting_exit_deadline is not None:
            if self.exited:
                code = int(self.process.poll() or 0) if self.process is not None else None
                self.state = "stopped"
                self.process = None
                self._close_connection()
                self._update_capture_closed(terminal=True)
                if code is not None:
                    self._notify_exit(code)
            elif now >= self._meeting_exit_deadline:
                self._restart(report=False, failure_code="timeout", relaunch=False)
            return
        if not self._hello_accepted and now >= self._hello_deadline:
            self._restart(report=False, failure_code="hello-timeout", relaunch=False)
            return
        if any(deadline <= now for deadline in self._meeting_close_deadlines.values()):
            self.close_state = "closing-unknown"
            self._restart(report=False, failure_code="timeout")
            return
        expired = [
            key
            for key, pending in self._pending.items()
            if pending.deadline is not None and pending.deadline <= now
        ]
        if any(
            self._pending[key].activity == self._activity
            or self._pending[key].request_type in {"meeting.job.cancel", "diar.cancel"}
            for key in expired
        ):
            self._restart(report=False, failure_code="timeout")
            return
        generation = self.generation
        events = [
            self._finish(key, ipc.error("timeout", "meeting-command-timeout")) for key in expired
        ]
        for event in events:
            self._emit(event, generation=generation)

    def _update_capture_closed(self, *, terminal: bool = False) -> None:
        if (
            self.role == "meeting-capture"
            and self.exited
            and (
                self.close_state == "closing-unknown"
                or terminal
                and self.close_state != "finalized"
            )
        ):
            self.close_state = "closed"

    def _notify_exit(self, code: int) -> None:
        """Exit state is committed before a user callback can reenter control."""
        self._exit_notifying = True
        try:
            self._on_exit(code)
        finally:
            self._exit_notifying = False

    def _kill_current(self) -> None:
        """Only launcher-created meeting processes have a private process group."""
        if self.process is None:
            return
        if self._process_group:
            with suppress(ProcessLookupError):
                os.killpg(self.process.pid, signal.SIGKILL)
        else:
            self.process.kill()

    def _flush(self) -> None:
        """Отправляет доступную порцию; остаток дописывается следующим pump."""
        if self._socket is None or not self._outgoing:
            return
        try:
            sent = self._socket.send(self._outgoing)
        except BlockingIOError:
            return
        except OSError:
            self._restart()
            return
        del self._outgoing[:sent]

    def pump(self, timeout: float = 0.0) -> None:
        """Читает порцию IPC через select; ожидание ограничено 50 мс и дедлайном."""
        if not math.isfinite(timeout) or timeout < 0:
            raise ValueError("Таймаут pump должен быть конечным и неотрицательным.")
        if self._exit_notifying:
            return
        self._retired = [process for process in self._retired if process.poll() is None]
        self._update_capture_closed()
        if self._restart_pending is not None:
            self._poll_restart()
            return
        self._expire()
        connection = self._socket
        if self.state != "running" or connection is None:
            return
        generation = self.generation
        delay = min(timeout, _POLL_INTERVAL_S)
        deadlines = [p.deadline for p in self._pending.values() if p.deadline is not None]
        if deadlines:
            delay = min(delay, max(0.0, min(deadlines) - self._clock()))
        try:
            readable, writable, _ = select.select(
                [connection], [connection] if self._outgoing else [], [], delay
            )
            self._expire()
            if self._socket is not connection:
                return
            if writable:
                self._flush()
            if readable and self._socket is connection:
                data = connection.recv(ipc.MAX_FRAME_BYTES)
                if not data:
                    if self.role != "worker" and self._meeting_exit_deadline is not None:
                        self._expire_meeting()
                        return
                    self._restart()
                    return
                self._receive(data, generation)
        except BlockingIOError:
            pass
        except OSError:
            if self._socket is connection:
                self._restart()
        if self.state == "running" and self.process is not None and self.process.poll() is not None:
            if self.role != "worker" and self._meeting_exit_deadline is not None:
                self._expire_meeting()
            else:
                self._restart()

    def _receive(self, data: bytes, generation: int) -> None:
        """Разбирает каждое тело отдельно, сохраняя кадры после безопасной ошибки."""
        if generation != self.generation:
            self.dropped_late += 1
            return
        try:
            frames = self._reader.feed_frames(data)
        except ipc.FrameError as exc:
            self._emit(ipc.error(exc.code, exc.message))
            if self.state == "running" and self.generation == generation:
                self._restart(report=False)
            return
        for frame in frames:
            if self.state != "running" or self.generation != generation:
                return
            try:
                message = ipc.decode(frame, role=self.role)
            except ipc.FrameError as exc:
                if exc.code == ipc.PROTOCOL_MISMATCH:
                    self._restart(report=False, failure_code=exc.code, relaunch=False)
                    return
                self._emit(ipc.error(exc.code, exc.message))
                continue
            self._accept(message, generation)

    def _accept(self, message: Message, generation: int) -> None:
        """Снимает ожидания до callback, отбрасывая повторные завершения (У35)."""
        if self.role != "worker":
            self._accept_meeting(message, generation)
            return
        kind = message["type"]
        if generation != self.generation:
            if kind in ("result", "cancelled", "error"):
                self.dropped_late += 1
            return
        key = _correlation(message)
        uid = message.get("utterance_id")
        utterance_keys = (
            [k for k in self._pending if k[0] == "utterance" and k[1] == str(uid)]
            if uid is not None
            else []
        )
        if kind in _NOTIFICATIONS:
            for k in utterance_keys:
                pending = self._pending[k]
                if pending.timeout is not None and pending.request_type != "record.cancel":
                    pending.deadline = self._clock() + pending.timeout
        elif kind == "error":
            if uid is not None and "request_type" not in message:
                # Ошибка захвата — независимое событие, не ACK последней команды.
                keys = [
                    k for k in utterance_keys if self._pending[k].request_type != "record.cancel"
                ]
                if not keys and not utterance_keys:
                    self.dropped_late += 1
                    return
                if len(utterance_keys) == 1 and len(keys) == 1:
                    self._finish(keys[0], message)
                self._activity += 1
                self._emit(message, generation=generation)
                return
            if uid is not None:
                keys = [key] if key in self._pending else []
            elif "request_type" in message:
                keys = [
                    k for k, p in self._pending.items() if message["request_type"] == p.request_type
                ]
                if len(keys) != 1:
                    keys = []
            else:
                keys = [k for k in self._pending if k[0] == "reply"]
                if not keys:
                    self._activity += 1
                    self._emit(message, generation=generation)
                    return
            if not keys:
                self.dropped_late += 1
                return
            self._activity += 1
            events = [self._finish(k, message) for k in keys]
            for k in keys:
                if len(k) == 3 and (
                    k[2] in {"recognize", "transcribe.file"}
                    or (k[2] == "record.cancel" and message.get("code") == "bad-state")
                ):
                    self._retire_chain(k[1])
            for event in events:
                self._emit(event, generation=generation)
            return
        else:
            if uid is not None and "request_type" not in message:
                # Ответ без типа совместим лишь с прежней цепочкой распознавания.
                # Ни result, ни cancelled без типа не являются ACK явной отмены.
                candidates = [
                    k for k in utterance_keys if self._pending[k].request_type != "record.cancel"
                ]
                if candidates and kind in {"result", "cancelled"}:
                    key = candidates[-1]
            if key in self._pending:
                self._finish(key, message)
                if key[0] == "utterance" and kind in {"result", "cancelled"}:
                    self._retire_chain(key[1])
            elif kind in ("result", "cancelled") or key[:2] in self._finished:
                self.dropped_late += 1
                return
        if kind == "hello":
            self._hello_accepted = True
        else:
            self._activity += 1
        self._emit(message)

    def _retire_chain(self, uid: str) -> None:
        """Результат завершает предыдущие стадии, но не независимую отмену."""
        for key in list(self._pending):
            if (
                key[0] == "utterance"
                and key[1] == uid
                and self._pending[key].request_type != "record.cancel"
            ):
                del self._pending[key]
        self._finished.add(("utterance", uid))

    def _finish(self, key: RequestKey, message: Message) -> Message:
        """Завершает ожидание и дополняет ошибку корреляцией для потребителя."""
        pending = self._pending.pop(key)
        if key[0] == "req":
            request = self._meeting_requests.pop(key)
            self._meeting_close_deadlines.pop(key, None)
            self._meeting_end_deadlines.pop(key, None)
            identity: dict[str, Any] = {
                field: request[field]
                for field in ("req", "request_type", "meeting_id", "meeting_generation")
                if field in request
            }
            event: Message = {**message, **identity}
            self._meeting_ledger.finish(key[1], [event])
            self._finished.add(key)
            return event
        self._finished.add(key[:2])
        event = dict(message)
        if message["type"] == "error":
            event.setdefault("request_type", pending.request_type)
            if key[0] == "utterance":
                event["utterance_id"] = key[1]
            else:
                event["response_type"] = key[1]
        return event

    def _expire(self) -> None:
        """Снимает истёкшие ожидания до вызова callback, без ожидания воркера."""
        if self.role != "worker":
            self._expire_meeting()
            return
        now = self._clock()
        generation = self.generation
        expired = [
            key
            for key, pending in self._pending.items()
            if pending.deadline is not None and pending.deadline <= now
        ]
        if any(
            "model.load" in self._pending[key].request_types
            or self._pending[key].activity == self._activity
            for key in expired
        ):
            self._restart(report=False, failure_code="load-timeout")
            return
        failure = ipc.error("timeout", "Истекло время ожидания ответа воркера.")
        events = [self._finish(key, failure) for key in expired]
        for event in events:
            self._emit(event, generation=generation)

    def _close_connection(self) -> None:
        """Отключает Qt до закрытия fd и аннулирует ожидания поколения."""
        self._hello_accepted = False
        if self._notifier is not None:
            self._notifier.setEnabled(False)
            self._notifier.deleteLater()
            self._notifier = None
        if self._timer is not None:
            self._timer.stop()
            self._timer.deleteLater()
            self._timer = None
        if self._socket is not None:
            self._socket.close()
            self._socket = None
        self._pending.clear()
        self._finished.clear()
        self._outgoing.clear()
        if self.role != "worker":
            from astra_voice.worker.meeting_contract import RequestLedger

            self._meeting_ledger = RequestLedger()
            self._meeting_requests.clear()
            self._meeting_end_deadlines.clear()
            self._meeting_close_deadlines.clear()
            self._meeting_exit_deadline = None

    def _restart(
        self,
        *,
        report: bool = True,
        failure_code: str = "worker-crashed",
        relaunch: bool = True,
    ) -> None:
        """Убивает воркер; смена поколения и события ждут подтверждения выхода."""
        if self._restart_pending is not None or self._exit_notifying:
            return
        if self.role != "worker":
            relaunch = relaunch and self.auto_restart
            if self.role == "meeting-capture" and not self.exited:
                self.close_state = "closing-unknown"
        generation = self.generation
        failure = ipc.error(
            failure_code,
            ipc.PROTOCOL_MISMATCH_MESSAGE + "."
            if failure_code == ipc.PROTOCOL_MISMATCH
            else "Воркер не ответил до истечения дедлайна."
            if failure_code == "load-timeout"
            else "Распознавание перезапущено.",
        )
        events = [self._finish(key, failure) for key in list(self._pending)]
        if not relaunch:
            self.state = "stopped"
            events.append(failure)
        self._close_connection()
        self._restart_pending = (generation, events, report, relaunch)
        if self.process is not None and self.process.poll() is None:
            self._kill_current()
            self._restart_kill_deadline = self._clock() + _KILL_RETRY_INTERVAL_S
            with suppress(Exception):
                self.process.wait(timeout=0.2)
        self._poll_restart()
        if self._restart_pending is not None and self._use_qt:
            from PyQt5.QtCore import QTimer

            # IPC уже закрыт, но D-state процесс ещё может владеть микрофоном.
            self._timer = QTimer()
            self._timer.timeout.connect(self.pump)
            self._timer.start(int(_POLL_INTERVAL_S * 1000))

    def _poll_restart(self) -> None:
        """Продолжает рестарт лишь после выхода; последующие wait неблокирующие."""
        if self._restart_pending is None:
            return
        if self.process is not None:
            with suppress(Exception):
                self.process.wait(timeout=0)
            if self.process.poll() is None:
                now = self._clock()
                if self._restart_kill_deadline is not None and now >= self._restart_kill_deadline:
                    # Повторяем сигнал с ограниченной частотой; это не ACK выхода.
                    self._restart_kill_deadline = now + _KILL_RETRY_INTERVAL_S
                    with suppress(Exception):
                        self._kill_current()
                return
        exit_code = (
            int(self.process.poll() or 0)
            if self.role != "worker" and self.process is not None
            else None
        )
        generation, events, _report, relaunch = self._restart_pending
        self._restart_pending = None
        self._restart_kill_deadline = None
        self._close_connection()
        if self.role != "worker":
            self.process = None
            self._update_capture_closed(terminal=True)
            stop_revision = self._stop_revision
            if exit_code is not None:
                self._notify_exit(exit_code)
            if self._stop_revision != stop_revision:
                return
        if not relaunch or not self._launch_allowed():
            self.state = "stopped"
            for event in events:
                self._emit(event, generation=generation)
            if not events:
                self._emit(
                    ipc.error("worker-crashed", "Распознавание перезапущено."),
                    generation=generation,
                )
            return
        now = self._clock()
        while self._restarts and now - self._restarts[0] >= RESTART_WINDOW_S:
            self._restarts.popleft()
        if len(self._restarts) >= MAX_RESTARTS:
            self.state = "stopped"
            for event in events:
                self._emit(event, generation=generation)
            self._emit(ipc.error("restart-limit", "Превышен лимит перезапусков воркера."))
            return
        self._restarts.append(now)
        self.generation += 1
        try:
            self._launch()
        except OSError:
            self.state = "stopped"
            for event in events:
                self._emit(event, generation=generation)
            self._emit(ipc.error("worker-start", "Не удалось перезапустить воркер."))
            return
        for event in events:
            self._emit(event, generation=generation)
        # Даже report=False должен разбудить отложенную смену модели после выхода.
        if not events:
            self._emit(ipc.error("worker-crashed", "Распознавание перезапущено."))

    def adopt_retired(self, processes: Iterable[subprocess.Popen[bytes]]) -> None:
        """Принимает процессы прежнего супервизора для сбора через pump/stop."""
        for process in processes:
            if not any(retired is process for retired in self._retired):
                self._retired.append(process)

    def stop(self) -> None:
        """Закрывает IPC, посылает SIGTERM; через две секунды применяет SIGKILL."""
        self._stop_revision += 1
        if self.role == "meeting-capture" and not self.exited:
            self.close_state = "closing-unknown"
        self.state = "stopped"
        self._restart_pending = None
        self._restart_kill_deadline = None
        self._close_connection()
        processes = list(self._retired)
        if self.process is not None and self.process not in processes:
            processes.append(self.process)
        for process in processes:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                if process is self.process:
                    self._kill_current()
                else:
                    process.kill()
                process.wait(timeout=2)
        if self.role == "worker":
            self._retired.clear()
        else:
            self._retired = [process for process in processes if process.poll() is None]
            self._update_capture_closed(terminal=True)

    def kill(self) -> None:
        """Закрывает IPC и убивает процессы, ожидая каждый до 0,2 с, без callback.

        Терминальна, как ``stop()``: отложенный рестарт отменяется без событий. SIGKILL
        не подтверждает выход — процессы остаются в ``_retired``, ``exited`` сообщает,
        завершились ли они; менять поколение до этого вызывающий не вправе.
        """
        self._stop_revision += 1
        if self.role == "meeting-capture" and not self.exited:
            self.close_state = "closing-unknown"
        self.state = "stopped"
        self._restart_pending = None
        self._restart_kill_deadline = None
        self._hello_accepted = False
        notifier, self._notifier = self._notifier, None
        timer, self._timer = self._timer, None
        if notifier is not None:
            with suppress(RuntimeError):
                notifier.setEnabled(False)
            with suppress(RuntimeError):
                notifier.deleteLater()
        if timer is not None:
            with suppress(RuntimeError):
                timer.stop()
            with suppress(RuntimeError):
                timer.deleteLater()
        connection, self._socket = self._socket, None
        if connection is not None:
            with suppress(OSError):
                connection.close()
        self._pending.clear()
        self._finished.clear()
        self._outgoing.clear()
        if self.process is not None and self.process not in self._retired:
            self._retired.append(self.process)
        for process in self._retired:
            with suppress(OSError):
                if process.poll() is None:
                    if process is self.process:
                        self._kill_current()
                    else:
                        process.kill()
            with suppress(Exception):
                process.wait(timeout=0.2)
        self._update_capture_closed(terminal=True)
