"""Супервизор: реальные дочерние процессы без Qt, поколения и дедлайны (T-21)."""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock

import pytest

from astra_voice import bootstrap
from astra_voice.worker import ipc
from astra_voice.worker.supervisor import (
    MAX_RESTARTS,
    RESTART_WINDOW_S,
    Message,
    WorkerSupervisor,
    worker_command,
)

pytestmark = pytest.mark.unit
ECHO_WORKER = Path(__file__).resolve().parents[1] / "echo_worker.py"


class Clock:
    """Монотонные часы с ручным продвижением без реального ожидания."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def echo_command(fd: int) -> list[str]:
    """Подменяет только команду, сохраняя реальные socketpair и pass_fds."""
    return [sys.executable, str(ECHO_WORKER), str(fd)]


def wait_for(supervisor: WorkerSupervisor, predicate: Callable[[], bool], reason: str) -> None:
    """Ограничивает любое ожидание событий одной реальной секундой."""
    deadline = time.monotonic() + 1
    while not predicate() and time.monotonic() < deadline:
        supervisor.pump(0.01)
    assert predicate(), f"За одну секунду не произошло событие: {reason}"


@contextmanager
def running(
    clock: Callable[[], float] = time.monotonic,
) -> Iterator[tuple[WorkerSupervisor, list[Message]]]:
    """Запускает эхо-воркер и завершает процессы даже при ошибке проверки."""
    events: list[Message] = []
    supervisor = WorkerSupervisor(
        events.append, clock=clock, command_factory=echo_command, use_qt=False
    )
    try:
        supervisor.start()
        assert supervisor.state == "running", f"Не удалось запустить эхо-воркер: {events}"
        wait_for(supervisor, lambda: any(e["type"] == "hello" for e in events), "hello")
        yield supervisor, events
    finally:
        supervisor.stop()


def control(supervisor: WorkerSupervisor, command: str) -> None:
    """Передаёт тестовое управление без ожидания результата расшифровки файла."""
    supervisor._outgoing.extend(ipc.encode({"type": "transcribe.file", "path": command}))
    supervisor._flush()


def crash(supervisor: WorkerSupervisor) -> None:
    """Посылает настоящий SIGKILL и ждёт нового поколения или остановки."""
    process = supervisor.process
    assert process is not None
    generation = supervisor.generation
    os.kill(process.pid, signal.SIGKILL)
    wait_for(
        supervisor,
        lambda: supervisor.generation > generation or supervisor.state == "stopped",
        "перезапуск после SIGKILL",
    )


def test_default_command_is_relocatable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Команда использует путь модуля бутстрапа и изолированный Python."""
    monkeypatch.setattr(bootstrap, "__file__", "/tmp/relocated/astra_voice/bootstrap.py")
    assert worker_command(123) == [
        sys.executable,
        "-I",
        "/tmp/relocated/astra_voice/bootstrap.py",
        "worker",
        "123",
    ]


def test_import_does_not_load_qt() -> None:
    """Свежий интерпретатор импортирует модуль без Qt и QApplication."""
    source = Path(__file__).resolve().parents[2] / "src"
    code = (
        f"import sys; sys.path.insert(0, {str(source)!r}); "
        "import astra_voice.worker.supervisor; "
        "assert not any(name.startswith('PyQt5') for name in sys.modules)"
    )
    result = subprocess.run([sys.executable, "-I", "-c", code], timeout=3, capture_output=True)
    assert result.returncode == 0, result.stderr.decode()


def test_hello_ping_and_stop() -> None:
    """Hello версии 1, ping/pong и идемпотентная остановка реального процесса."""
    with running() as (supervisor, events):
        assert events[0]["protocol"] == 1
        assert supervisor.generation == 1
        process = supervisor.process
        assert process is not None
        supervisor.start()
        assert supervisor.process is process
        request = supervisor.send({"type": "ping"}, timeout=1)
        assert request is not None
        assert request["generation"] == 1
        wait_for(supervisor, lambda: any(e["type"] == "pong" for e in events), "pong")
        supervisor.stop()
        assert process.poll() is not None
        assert supervisor.state == "stopped"
        supervisor.stop()
        supervisor.pump()
        with pytest.raises(RuntimeError, match="остановлен"):
            supervisor.start()


def test_sigkill_restarts_with_new_pid_within_one_second() -> None:
    """О1: после SIGKILL поколение и pid меняются менее чем за секунду."""
    with running() as (supervisor, events):
        process = supervisor.process
        assert process is not None
        start = time.monotonic()
        crash(supervisor)
        assert time.monotonic() - start < 1
        assert supervisor.generation == 2
        assert supervisor.process is not None
        assert supervisor.process.pid != process.pid
        assert process.poll() is not None
        wait_for(
            supervisor,
            lambda: any(e["type"] == "hello" and e["generation"] == 2 for e in events),
            "hello нового поколения",
        )
        assert sum(e.get("code") == "worker-crashed" for e in events) == 1


@pytest.mark.parametrize("kind", ["result", "cancelled"])
def test_terminal_response_is_accepted_exactly_once(kind: str) -> None:
    """Повторный result/cancelled и второй тип завершения не проходят в GUI."""
    with running() as (supervisor, events):
        supervisor.send({"type": "record.start", "utterance_id": "u1"})
        supervisor.send({"type": "recognize", "utterance_id": "u1"})
        control(supervisor, f"{kind}:u1")
        control(supervisor, f"{kind}:u1")
        other = "cancelled" if kind == "result" else "result"
        control(supervisor, f"{other}:u1")
        wait_for(supervisor, lambda: supervisor.dropped_late == 2, "отброс двух повторов")
        accepted = [e for e in events if e.get("utterance_id") == "u1"]
        assert len(accepted) == 1
        assert accepted[0]["type"] == kind
        assert accepted[0]["generation"] == 1
        with pytest.raises(ValueError, match="завершён"):
            supervisor.send({"type": "recognize", "utterance_id": "u1"})


def test_old_generation_and_unknown_id_are_dropped() -> None:
    """Старые ожидания очищаются; отложенный callback привязан к старому сокету."""
    with running() as (supervisor, events):
        supervisor.send({"type": "recognize", "utterance_id": "old"})
        old_generation = supervisor.generation
        crash(supervisor)
        control(supervisor, "result:old")
        control(supervisor, "result:unknown")
        wait_for(supervisor, lambda: supervisor.dropped_late == 2, "старый и неизвестный id")
        assert not any(e["type"] == "result" for e in events)
        supervisor.send({"type": "recognize", "utterance_id": "old"})
        # Моделируем уже разобранное сообщение, задержанное прошлым соединением.
        supervisor._accept(
            {"type": "result", "utterance_id": "old", "text": "Поздний", "t_ms": 1},
            old_generation,
        )
        assert supervisor.dropped_late == 3
        assert not any(e["type"] == "result" for e in events)
        control(supervisor, "result:old")
        wait_for(supervisor, lambda: any(e["type"] == "result" for e in events), "новый результат")
        assert [e["generation"] for e in events if e["type"] == "result"] == [2]


def test_fourth_restart_is_terminal() -> None:
    """Четвёртый рестарт за 600 секунд запрещён навсегда для этого супервизора."""
    clock = Clock()
    with running(clock) as (supervisor, events):
        for _ in range(MAX_RESTARTS):
            crash(supervisor)
            assert supervisor.state == "running"
        process = supervisor.process
        generation = supervisor.generation
        crash(supervisor)
        assert supervisor.state == "stopped"
        assert supervisor.process is process
        assert supervisor.generation == generation
        assert sum(e.get("code") == "restart-limit" for e in events) == 1
        clock.now += RESTART_WINDOW_S + 1
        supervisor.pump()
        assert supervisor.state == "stopped"
        assert supervisor.process is process


def test_restart_outside_sliding_window_is_allowed() -> None:
    """Старые рестарты выбывают из скользящего окна до достижения лимита."""
    clock = Clock()
    with running(clock) as (supervisor, events):
        crash(supervisor)
        clock.now += RESTART_WINDOW_S / 2
        crash(supervisor)
        crash(supervisor)
        clock.now += RESTART_WINDOW_S / 2 + 1
        crash(supervisor)
        assert supervisor.generation == 5
        assert supervisor.state == "running"
        assert not any(e.get("code") == "restart-limit" for e in events)
        crash(supervisor)
        assert str(supervisor.state) == "stopped"


def test_timeout_drops_late_result_without_real_wait() -> None:
    """Истечение дедлайна выдаёт одну ошибку и запрещает запоздалый результат."""
    clock = Clock()
    with running(clock) as (supervisor, events):
        supervisor.send({"type": "recognize", "utterance_id": "slow"}, timeout=5)
        supervisor.send({"type": "ping"})
        wait_for(supervisor, lambda: any(e["type"] == "pong" for e in events), "живой воркер")
        clock.now += 4
        supervisor.pump()
        assert not any(e.get("code") == "timeout" for e in events)
        clock.now += 1
        supervisor.pump()
        supervisor.pump()
        errors = [e for e in events if e.get("code") == "timeout"]
        assert len(errors) == 1
        assert errors[0]["utterance_id"] == "slow"
        assert errors[0]["generation"] == 1
        control(supervisor, "result:slow")
        wait_for(supervisor, lambda: supervisor.dropped_late == 1, "поздний ответ после таймаута")
        assert not any(e["type"] == "result" for e in events)


def test_silent_worker_ping_timeout() -> None:
    """Молчание в ответ на ping означает зависание и требует рестарта."""
    clock = Clock()
    with running(clock) as (supervisor, events):
        control(supervisor, "silence")
        supervisor.send({"type": "ping"}, timeout=2)
        clock.now += 2
        supervisor.pump()
        errors = [e for e in events if e.get("code") == "load-timeout"]
        assert len(errors) == 1
        assert errors[0]["response_type"] == "pong"
        assert errors[0]["generation"] == 1
        assert supervisor.generation == 2
        assert not any(e["type"] == "pong" for e in events)


def test_success_and_restart_clear_deadlines() -> None:
    """Ответ и смена поколения снимают дедлайн без последующей ошибки timeout."""
    clock = Clock()
    with running(clock) as (supervisor, events):
        supervisor.send({"type": "ping"}, timeout=2)
        wait_for(supervisor, lambda: any(e["type"] == "pong" for e in events), "pong")
        supervisor.send({"type": "recognize", "utterance_id": "lost"}, timeout=2)
        crash(supervisor)
        clock.now += 3
        supervisor.pump()
        assert not any(e.get("code") == "timeout" for e in events)


def test_bad_frame_preserves_following_frame() -> None:
    """Невалидный JSON даёт error, соседний pong не теряется и pid сохраняется."""
    with running() as (supervisor, events):
        process = supervisor.process
        control(supervisor, "bad-frame")
        wait_for(supervisor, lambda: any(e["type"] == "pong" for e in events), "pong после мусора")
        assert any(e.get("code") == ipc.BAD_FRAME for e in events)
        assert supervisor.process is process
        assert supervisor.generation == 1


def test_oversized_frame_restarts_worker() -> None:
    """T-21: заголовка на 2 ГиБ достаточно для отключения и рестарта."""
    with running() as (supervisor, events):
        process = supervisor.process
        control(supervisor, "oversized")
        wait_for(supervisor, lambda: supervisor.generation == 2, "рестарт после 2 ГиБ")
        assert any(e.get("code") == ipc.FRAME_TOO_LARGE for e in events)
        assert supervisor.process is not process
        assert supervisor.state == "running"


def test_stop_escalates_to_sigkill() -> None:
    """Зависший процесс получает SIGKILL после двухсекундного ожидания SIGTERM."""
    process = Mock(spec=subprocess.Popen)
    process.poll.return_value = None
    process.wait.side_effect = [subprocess.TimeoutExpired("worker", 2), 0]
    supervisor = WorkerSupervisor(lambda message: None, use_qt=False)
    supervisor.process = process
    supervisor.stop()
    process.terminate.assert_called_once_with()
    process.kill.assert_called_once_with()
    assert process.wait.call_count == 2
    assert process.wait.call_args_list[0].kwargs == {"timeout": 2}


@pytest.mark.parametrize("timeout", [None, 120.0])
def test_transcribe_file_correlates_with_service_id(timeout: float | None) -> None:
    """Запрос без id получает result с контрактным id file, без dropped_late."""
    with running() as (supervisor, events):
        supervisor.send({"type": "transcribe.file", "path": "/tmp/test.wav"}, timeout=timeout)
        wait_for(supervisor, lambda: any(e["type"] == "result" for e in events), "результат файла")
        assert [e for e in events if e["type"] == "result"] == [
            {
                "type": "result",
                "utterance_id": "file",
                "text": "Проверка файла",
                "t_ms": 1,
                "generation": 1,
            }
        ]
        assert supervisor.dropped_late == 0
        assert not supervisor._pending


@pytest.mark.parametrize("mode", ["legacy", "utterance", "request"])
def test_recognize_error_finishes_once(mode: str) -> None:
    """Ошибка снимает дедлайн; новый recognize допускается, поздний result отброшен."""
    clock = Clock()
    with running(clock) as (supervisor, events):
        control(supervisor, f"errors:{mode}")
        supervisor.send({"type": "recognize", "utterance_id": "failed"}, timeout=2)
        wait_for(supervisor, lambda: any(e["type"] == "error" for e in events), "ошибка recognize")
        clock.now += 3
        supervisor.pump()
        control(supervisor, "result:failed")
        wait_for(supervisor, lambda: supervisor.dropped_late == 1, "поздний result после error")
        terminal = [e for e in events if e.get("utterance_id") == "failed"]
        assert len(terminal) == 1
        assert terminal[0]["code"] == "engine-failed"
        assert not any(e.get("code") in ("timeout", "load-timeout") for e in events)
        assert supervisor.send({"type": "recognize", "utterance_id": "next"}) is not None
        wait_for(
            supervisor,
            lambda: any(e.get("utterance_id") == "next" for e in events),
            "ошибка следующего recognize",
        )


@pytest.mark.parametrize("mode", ["legacy", "request"])
def test_ping_error_without_deadline_allows_next_ping(mode: str) -> None:
    """Ошибка снимает ожидание ping даже без дедлайна и позволяет повторный ping."""
    with running() as (supervisor, events):
        control(supervisor, f"errors:{mode}")
        supervisor.send({"type": "ping"}, timeout=None)
        wait_for(supervisor, lambda: any(e["type"] == "error" for e in events), "ошибка ping")
        assert supervisor.send({"type": "ping"}, timeout=None) is not None
        wait_for(
            supervisor,
            lambda: sum(e["type"] == "error" for e in events) == 2,
            "ошибка второго ping",
        )
        assert not supervisor._pending


def model_load_request() -> Message:
    """Минимальная валидная команда загрузки, не требующая настоящей модели."""
    return {
        "type": "model.load",
        "id": "test",
        "revision": "test",
        "dir": "/tmp/no-model",
        "layout": "test",
        "variant": "test",
        "threads": 1,
        "min_ram_mb": 768,
    }


def test_model_load_deadline_kills_and_restarts_worker() -> None:
    """Дедлайн загрузки убивает процесс даже при доступном ping, часы подменены."""
    clock = Clock()
    with running(clock) as (supervisor, events):
        process = supervisor.process
        assert process is not None
        supervisor.send(model_load_request(), timeout=10)
        supervisor.send({"type": "ping"})
        wait_for(supervisor, lambda: any(e["type"] == "pong" for e in events), "pong при загрузке")
        clock.now += 10
        supervisor.pump()
        assert supervisor.generation == 2
        assert supervisor.process is not process
        wait_for(supervisor, lambda: process.poll() is not None, "завершение убитого воркера")
        assert process.poll() == -signal.SIGKILL
        errors = [e for e in events if e["type"] == "error"]
        assert len(errors) == 1
        assert errors[0]["code"] == "load-timeout"
        assert errors[0]["request_type"] == "model.load"
        assert errors[0]["generation"] == 1
        clock.now += 100
        supervisor.pump()
        assert [e for e in events if e["type"] == "error"] == errors


def test_model_load_deadline_obeys_restart_limit() -> None:
    """Дедлайны расходуют тот же бюджет трёх рестартов за десять минут."""
    clock = Clock()
    with running(clock) as (supervisor, events):
        for attempt in range(MAX_RESTARTS + 1):
            supervisor.send(model_load_request(), timeout=10)
            clock.now += 10
            supervisor.pump()
            assert sum(e.get("code") == "load-timeout" for e in events) == attempt + 1
        assert supervisor.state == "stopped"
        assert supervisor.generation == MAX_RESTARTS + 1
        assert sum(e.get("code") == "restart-limit" for e in events) == 1


def test_cancel_after_result_is_ignored(caplog: pytest.LogCaptureFixture) -> None:
    """Поздняя отмена не пишет кадр, возвращает None и оставляет запись debug."""
    with running() as (supervisor, events):
        supervisor.send({"type": "recognize", "utterance_id": "done"})
        control(supervisor, "result:done")
        wait_for(supervisor, lambda: any(e["type"] == "result" for e in events), "result")
        with caplog.at_level(logging.DEBUG, logger="astra_voice.worker.supervisor"):
            assert supervisor.send({"type": "record.cancel", "utterance_id": "done"}) is None
        assert "Отмена завершённой диктовки пропущена" in caplog.text
        assert not supervisor._outgoing
        assert not supervisor._pending
        with pytest.raises(ValueError, match="завершён"):
            supervisor.send({"type": "recognize", "utterance_id": "done"})


def detached(clock: Clock) -> tuple[WorkerSupervisor, list[Message]]:
    """Готовит проверки корреляции без сокетов, процессов и подмены модулей."""
    events: list[Message] = []
    supervisor = WorkerSupervisor(events.append, clock=clock, use_qt=False)
    supervisor.state = "running"
    return supervisor, events


@pytest.mark.parametrize("timeout", [None, 1.0])
def test_record_notifications_keep_pending_until_result(timeout: float | None) -> None:
    """Индикация записи доходит до callback, а ожидание снимает только result."""
    supervisor, events = detached(Clock())
    supervisor.send({"type": "record.start", "utterance_id": "one"}, timeout=timeout)
    key = ("utterance", "one")
    notifications: list[Message] = [
        {"type": "level", "utterance_id": "one", "rms_dbfs": level, "peak_dbfs": -1}
        for level in (-30, -20, -10)
    ]
    notifications.extend(
        [
            {"type": "silent", "utterance_id": "one"},
            {"type": "record.limit", "utterance_id": "one"},
            {"type": "audio.ready"},
        ]
    )
    for notification in notifications:
        supervisor._receive(ipc.encode(notification), 1)
        assert key in supervisor._pending
        assert key not in supervisor._finished
    result: Message = {"type": "result", "utterance_id": "one", "text": "Тест", "t_ms": 1}
    supervisor._receive(ipc.encode(result), 1)
    assert not supervisor._pending
    assert key in supervisor._finished
    assert events == [{**event, "generation": 1} for event in [*notifications, result]]
    assert supervisor.dropped_late == 0

    # Даже уведомления после завершения не считаются поздними ответами.
    for notification in notifications:
        supervisor._receive(ipc.encode(notification), 1)
    assert events[-len(notifications) :] == [{**event, "generation": 1} for event in notifications]
    assert not supervisor._pending
    assert supervisor.dropped_late == 0


@pytest.mark.parametrize("continuation_timeout", [None, 0.5])
def test_level_stream_extends_record_deadline(continuation_timeout: float | None) -> None:
    """Поток level сохраняет долгую запись; после паузы ожидание всё же истекает."""
    clock = Clock()
    supervisor, events = detached(clock)
    supervisor.send({"type": "record.start", "utterance_id": "one"}, timeout=0.25)
    supervisor.send({"type": "recognize", "utterance_id": "one"}, timeout=continuation_timeout)
    timeout = 0.25 if continuation_timeout is None else continuation_timeout
    for _ in range(20):
        clock.now += 0.125
        supervisor._expire()
        supervisor._receive(
            ipc.encode({"type": "level", "utterance_id": "one", "rms_dbfs": -20, "peak_dbfs": -1}),
            1,
        )
        supervisor._expire()
        assert ("utterance", "one") in supervisor._pending
        assert supervisor.generation == 1
    assert len(events) == 20
    assert all(event["type"] == "level" for event in events)
    assert supervisor.dropped_late == 0

    clock.now += timeout
    supervisor._expire()
    assert not supervisor._pending
    assert supervisor.generation == 1
    assert len(events) == 21
    assert events[-1]["code"] == "timeout"
    assert events[-1]["utterance_id"] == "one"


@pytest.mark.parametrize(
    "correlation",
    [{"utterance_id": "one"}, {"request_type": "recognize"}],
)
def test_correlated_error_finishes_only_matching_request(correlation: dict[str, str]) -> None:
    """Необязательные поля проходят IPC и снимают только нужное ожидание."""
    clock = Clock()
    supervisor, events = detached(clock)
    supervisor.send({"type": "recognize", "utterance_id": "one"}, timeout=2)
    supervisor.send({"type": "ping"}, timeout=2)
    error = {**ipc.error("engine-failed", "Тестовая ошибка."), **correlation}
    supervisor._receive(ipc.encode(error), supervisor.generation)
    assert len(events) == 1
    assert events[0]["utterance_id"] == "one"
    clock.now += 2
    supervisor._expire()
    assert len(events) == 2
    assert events[1]["code"] == "timeout"
    assert events[1]["request_type"] == "ping"
    assert supervisor.generation == 1
    supervisor._receive(ipc.encode(error), supervisor.generation)
    assert len(events) == 2
    assert supervisor.dropped_late == 1


def test_file_error_by_request_type_uses_same_correlation() -> None:
    """Ошибка transcribe.file без id и поздний result используют один ключ file."""
    supervisor, events = detached(Clock())
    supervisor.send({"type": "transcribe.file", "path": "/tmp/test.wav"})
    supervisor._receive(
        ipc.encode({**ipc.error("bad-audio", "Тест."), "request_type": "transcribe.file"}), 1
    )
    supervisor._receive(
        ipc.encode({"type": "result", "utterance_id": "file", "text": "Тест", "t_ms": 1}), 1
    )
    assert len(events) == 1
    assert events[0]["utterance_id"] == "file"
    assert events[0]["code"] == "bad-audio"
    assert supervisor.dropped_late == 1


@pytest.mark.parametrize("terminal", ["result", "cancelled", "error", "timeout"])
def test_each_request_has_one_terminal_event(terminal: str) -> None:
    """Любое завершение исключает повторный error/result/cancelled и таймаут."""
    clock = Clock()
    supervisor, events = detached(clock)
    supervisor.send({"type": "recognize", "utterance_id": "one"}, timeout=1)
    supervisor.send({"type": "ping"})
    supervisor._receive(ipc.encode({"type": "pong"}), 1)
    events.clear()
    responses: dict[str, Message] = {
        "result": {"type": "result", "utterance_id": "one", "text": "Тест", "t_ms": 1},
        "cancelled": {"type": "cancelled", "utterance_id": "one"},
        "error": {**ipc.error("engine-failed", "Тест."), "utterance_id": "one"},
    }
    if terminal != "timeout":
        supervisor._receive(ipc.encode(responses[terminal]), 1)
    clock.now += 1
    supervisor._expire()
    for response in responses.values():
        supervisor._receive(ipc.encode(response), 1)
    supervisor._receive(ipc.encode(ipc.error("engine-failed", "Поздняя ошибка.")), 1)
    supervisor._expire()
    assert len(events) == 1
    assert events[0]["utterance_id"] == "one"
    assert events[0]["type"] == ("error" if terminal == "timeout" else terminal)
    if terminal == "timeout":
        assert events[0]["code"] == "timeout"


def test_uncorrelated_error_completes_all_pending_before_callback() -> None:
    """Общая ошибка атомарно завершает старые ожидания, сохраняя новое из callback."""
    clock = Clock()
    events: list[Message] = []

    def on_event(event: Message) -> None:
        events.append(event)
        if len(events) == 1:
            supervisor.send({"type": "ping"})

    supervisor = WorkerSupervisor(on_event, clock=clock, use_qt=False)
    supervisor.state = "running"
    supervisor.send({"type": "recognize", "utterance_id": "one"}, timeout=1)
    supervisor.send({"type": "ping"}, timeout=1)
    supervisor._receive(ipc.encode(ipc.error("engine-failed", "Общая ошибка.")), 1)
    assert len(events) == 2
    assert {e["request_type"] for e in events} == {"recognize", "ping"}
    clock.now += 2
    supervisor._expire()
    supervisor._receive(ipc.encode({"type": "pong"}), 1)
    assert [e["type"] for e in events] == ["error", "error", "pong"]


def test_unknown_correlated_error_does_not_finish_other_request() -> None:
    """Неизвестный id и старое поколение не включают правило общей ошибки."""
    supervisor, events = detached(Clock())
    supervisor.send({"type": "recognize", "utterance_id": "one"})
    supervisor._receive(
        ipc.encode({**ipc.error("engine-failed", "Тест."), "utterance_id": "unknown"}), 1
    )
    supervisor._accept({**ipc.error("engine-failed", "Тест."), "utterance_id": "one"}, 0)
    assert events == []
    assert supervisor.dropped_late == 2
    supervisor._receive(
        ipc.encode({"type": "result", "utterance_id": "one", "text": "Тест", "t_ms": 1}), 1
    )
    assert len(events) == 1
    assert events[0]["type"] == "result"


@pytest.mark.parametrize("kind", ["model.load", "ping", "recognize"])
def test_unresponsive_deadline_kills_before_reporting(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Без сокета проверяет SIGKILL, смену поколения и единственное завершение."""
    clock = Clock()
    supervisor, events = detached(clock)
    old_process = Mock(spec=subprocess.Popen)
    old_process.poll.return_value = None
    supervisor.process = old_process
    new_process = Mock(spec=subprocess.Popen)

    def launch() -> None:
        old_process.kill.assert_called_once_with()
        supervisor.process = new_process
        supervisor.state = "running"

    monkeypatch.setattr(supervisor, "_launch", launch)
    request = model_load_request() if kind == "model.load" else {"type": kind}
    if kind == "recognize":
        request["utterance_id"] = "one"
    supervisor.send(request, timeout=10)
    clock.now += 10
    supervisor._expire()
    assert supervisor.generation == 2
    assert supervisor.process is new_process
    assert len(events) == 1
    assert events[0]["code"] == "load-timeout"
    assert events[0]["request_type"] == kind
    assert events[0]["generation"] == 1
    supervisor._expire()
    assert len(events) == 1
    old_process.wait.assert_not_called()


def test_expired_batch_is_removed_before_callback() -> None:
    """Новый ping из callback не конфликтует с другим уже истёкшим ожиданием."""
    clock = Clock()
    events: list[Message] = []

    def on_event(event: Message) -> None:
        events.append(event)
        if event.get("utterance_id") == "one":
            supervisor.send({"type": "ping"})

    supervisor = WorkerSupervisor(on_event, clock=clock, use_qt=False)
    supervisor.state = "running"
    supervisor.send({"type": "recognize", "utterance_id": "one"}, timeout=1)
    supervisor.send({"type": "ping"}, timeout=1)
    supervisor._receive(ipc.encode({"type": "audio.ready"}), 1)
    events.clear()
    clock.now += 1
    supervisor._expire()
    supervisor._receive(ipc.encode({"type": "pong"}), 1)
    assert [e["type"] for e in events] == ["error", "error", "pong"]
    assert all(e["code"] == "timeout" for e in events[:2])
    assert not supervisor._pending
