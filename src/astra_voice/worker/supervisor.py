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


def worker_command(fd: int) -> list[str]:
    """Составляет команду запуска воркера через лаунчер в изолированном Python."""
    return [sys.executable, "-I", str(launcher_path()), "worker", str(fd)]


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
        command_factory: Callable[[int], list[str]] = worker_command,
        use_qt: bool = True,
        launch_allowed: Callable[[], bool] = lambda: True,
    ) -> None:
        self.generation = 1
        self.dropped_late = 0
        self.state: Literal["new", "running", "stopped"] = "new"
        self.process: subprocess.Popen[bytes] | None = None
        self._on_event = on_event
        self._clock = clock
        self._command_factory = command_factory
        self._use_qt = use_qt
        self._launch_allowed = launch_allowed
        self._hello_accepted = False
        self._socket: socket.socket | None = None
        self._reader = ipc.FrameReader()
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
    def ready(self) -> bool:
        """Текущий воркер подтвердил совместимость протокола через hello."""
        return self.state == "running" and not self.restarting and self._hello_accepted

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
        self._hello_accepted = False
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
        """Ставит кадр в очередь и регистрирует ожидание ответа.

        Без явного timeout команда с utterance_id, включая record.cancel,
        наследует срок последнего соседнего ожидания с таймаутом того же id.
        Это исходный абсолютный срок, а не новый интервал от момента отмены.
        """
        if self.state != "running" or self._restart_pending is not None:
            raise RuntimeError("Воркер не запущен.")
        if timeout is not None and (not math.isfinite(timeout) or timeout < 0):
            raise ValueError("Таймаут должен быть конечным и неотрицательным.")
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
                if exc.code == ipc.PROTOCOL_MISMATCH:
                    self._restart(report=False, failure_code=exc.code, relaunch=False)
                    return
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

    def _restart(
        self,
        *,
        report: bool = True,
        failure_code: str = "worker-crashed",
        relaunch: bool = True,
    ) -> None:
        """Убивает воркер; смена поколения и события ждут подтверждения выхода."""
        if self._restart_pending is not None:
            return
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
            self.process.kill()
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
                        self.process.kill()
                return
        generation, events, _report, relaunch = self._restart_pending
        self._restart_pending = None
        self._restart_kill_deadline = None
        self._close_connection()
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

    def stop(self) -> None:
        """Закрывает IPC, посылает SIGTERM; через две секунды применяет SIGKILL."""
        self.state = "stopped"
        self._restart_pending = None
        self._restart_kill_deadline = None
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
