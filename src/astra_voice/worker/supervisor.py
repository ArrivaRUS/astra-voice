"""Супервизор дочернего воркера с поколениями, дедлайнами и адаптером Qt."""

from __future__ import annotations

import math
import select
import socket
import subprocess
import sys
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from astra_voice import bootstrap
from astra_voice.worker import ipc

MAX_RESTARTS = 3
RESTART_WINDOW_S = 600
_POLL_INTERVAL_S = 0.05
Message = dict[str, Any]
RequestKey = tuple[str, str]
_CORRELATION: dict[str, RequestKey] = {
    "ping": ("reply", "pong"),
    "model.load": ("reply", "model.loaded"),
    "measure": ("reply", "measured"),
    "audio.close": ("reply", "audio.closed"),
    "transcribe.file": ("utterance", "file"),
}
# Уровень и тишина — индикация во время записи; record.limit означает автоостановку,
# но распознавание этого utterance_id ещё впереди. audio.ready подтверждает открытие
# устройства, а не завершение команды: эти уведомления не снимают ожидание.
_NOTIFICATIONS = {"level", "silent", "record.limit", "audio.ready"}


@dataclass
class _Pending:
    """Общее ожидание цепочки команд и номер последнего ответа при отправке."""

    deadline: float | None
    activity: int
    request_type: str
    timeout: float | None
    request_types: set[str] = field(default_factory=set)


def _correlation(message: Message) -> RequestKey:
    """Нормализует запрос, ответ и коррелированную ошибку в один ключ."""
    if "utterance_id" in message:
        return ("utterance", str(message["utterance_id"]))
    kind = str(message.get("request_type", message["type"]))
    return _CORRELATION.get(kind, ("reply", kind))


def worker_command(fd: int) -> list[str]:
    """Находит единый бутстрап относительно установленного модуля (И7)."""
    return [sys.executable, "-I", str(Path(bootstrap.__file__).resolve()), "worker", str(fd)]


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

    Единственный ключ корреляции задаёт ``_correlation``: явный utterance_id либо
    таблица ``_CORRELATION``. Поэтому transcribe.file ожидает result/cancelled с
    id "file"; этот служебный id также нельзя повторять в пределах поколения.
    Команды одной диктовки разделяют одно ожидание и одно завершающее событие.
    Промежуточные level/silent/record.limit/audio.ready передаются потребителю,
    сохраняя ожидание и продлевая его дедлайн на заданный таймаут при совпадении ключа.
    Ошибка с utterance_id завершает точное ожидание; request_type без id допустим,
    если определяет единственное ожидание. Ошибка без корреляции завершает только
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
        command_factory: Callable[[int], list[str]] = worker_command,
        use_qt: bool = True,
    ) -> None:
        self.generation = 1
        self.dropped_late = 0
        self.state: Literal["new", "running", "stopped"] = "new"
        self.process: subprocess.Popen[bytes] | None = None
        self._on_event = on_event
        self._clock = clock
        self._command_factory = command_factory
        self._use_qt = use_qt
        self._socket: socket.socket | None = None
        self._reader = ipc.FrameReader()
        self._outgoing = bytearray()
        self._pending: dict[RequestKey, _Pending] = {}
        self._finished: set[RequestKey] = set()
        self._activity = 0
        self._restarts: deque[float] = deque()
        self._retired: list[subprocess.Popen[bytes]] = []
        self._notifier: Any = None
        self._timer: Any = None

    def start(self) -> None:
        """Запускает воркер один раз; повторный вызов при работе безопасен."""
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
        parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            parent.setblocking(False)
            process = subprocess.Popen(
                self._command_factory(child.fileno()), pass_fds=[child.fileno()]
            )
        except BaseException:
            parent.close()
            raise
        finally:
            child.close()
        self.process = process
        self._socket = parent
        self._reader = ipc.FrameReader()
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
        """Ставит кадр в неблокирующую очередь и регистрирует ожидание ответа."""
        if self.state != "running":
            raise RuntimeError("Воркер не запущен.")
        if timeout is not None and (not math.isfinite(timeout) or timeout < 0):
            raise ValueError("Таймаут должен быть конечным и неотрицательным.")
        frame = ipc.encode(message)
        request = {**ipc.decode(frame[4:]), "generation": self.generation}
        uid = request.get("utterance_id")
        kind = str(request["type"])
        key = _correlation(request)
        if key[0] == "utterance" and key in self._finished and kind != "record.cancel":
            raise ValueError("Идентификатор уже завершён в текущем поколении.")
        if uid is None and key in self._pending:
            raise ValueError("Ответ на предыдущий запрос этого типа ещё ожидается.")
        deadline = None if timeout is None else self._clock() + timeout
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
            self._finished.discard(key)
        self._outgoing.extend(frame)
        self._flush()
        return request

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
        self._retired = [process for process in self._retired if process.poll() is None]
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
                    self._restart()
                    return
                self._receive(data, generation)
        except BlockingIOError:
            pass
        except OSError:
            if self._socket is connection:
                self._restart()
        if self.state == "running" and self.process is not None and self.process.poll() is not None:
            self._restart()

    def _receive(self, data: bytes, generation: int) -> None:
        """Разбирает каждое тело отдельно, сохраняя кадры после безопасной ошибки."""
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
                message = ipc.decode(frame)
            except ipc.FrameError as exc:
                self._emit(ipc.error(exc.code, exc.message))
                continue
            self._accept(message, generation)

    def _accept(self, message: Message, generation: int) -> None:
        """Снимает ожидания до callback, отбрасывая повторные завершения (У35)."""
        kind = message["type"]
        if generation != self.generation:
            if kind in ("result", "cancelled", "error"):
                self.dropped_late += 1
            return
        key = _correlation(message)
        if kind == "error":
            if "utterance_id" in message:
                keys = [key] if key in self._pending else []
            elif "request_type" in message:
                keys = [
                    k
                    for k, p in self._pending.items()
                    if message["request_type"] in p.request_types
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
            for event in events:
                self._emit(event, generation=generation)
            return
        if kind in _NOTIFICATIONS:
            pending = self._pending.get(key)
            if pending is not None and pending.timeout is not None:
                pending.deadline = self._clock() + pending.timeout
        elif key in self._pending:
            self._finish(key, message)
        elif kind in ("result", "cancelled") or key in self._finished:
            self.dropped_late += 1
            return
        if kind != "hello":
            self._activity += 1
        self._emit(message)

    def _finish(self, key: RequestKey, message: Message) -> Message:
        """Завершает ожидание и дополняет ошибку корреляцией для потребителя."""
        pending = self._pending.pop(key)
        self._finished.add(key)
        event = dict(message)
        if message["type"] == "error":
            event["request_type"] = pending.request_type
            if key[0] == "utterance":
                event["utterance_id"] = key[1]
            else:
                event["response_type"] = key[1]
        return event

    def _expire(self) -> None:
        """Снимает истёкшие ожидания до вызова callback, без ожидания воркера."""
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

    def _restart(self, *, report: bool = True, failure_code: str = "worker-crashed") -> None:
        """Убивает повреждённый воркер и применяет скользящее окно рестартов."""
        generation = self.generation
        failure = ipc.error(
            failure_code,
            "Воркер не ответил до истечения дедлайна."
            if failure_code == "load-timeout"
            else "Распознавание перезапущено.",
        )
        events = [self._finish(key, failure) for key in list(self._pending)]
        self._close_connection()
        if self.process is not None and self.process.poll() is None:
            self.process.kill()
            # Не ждём завершения в GUI; poll следующих pump соберёт статус.
            self._retired.append(self.process)
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
        if report and not events:
            self._emit(ipc.error("worker-crashed", "Распознавание перезапущено."))

    def stop(self) -> None:
        """Закрывает IPC, посылает SIGTERM; через две секунды применяет SIGKILL."""
        self.state = "stopped"
        self._close_connection()
        processes = list(self._retired)
        self._retired.clear()
        if self.process is not None and self.process not in processes:
            processes.append(self.process)
        for process in processes:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
