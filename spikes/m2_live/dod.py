#!/usr/bin/env python3
"""Офлайн-стенд M2: serve, rlimit-load, cpu-wall и soak.

Пример: python3 spikes/m2_live/dod.py soak --model-dir /путь/к/модели --json
В режиме --json stdout содержит JSON Lines: один объект на событие или замер.
Коды: 0 — критерии выполнены, 1 — нарушены, 2 — прогон невозможен.

Soak использует штатный IPC напрямую: WorkerState допускает последовательные
transcribe.file, а WorkerSupervisor запрещает повторное использование id file.
Процесс, модель и соединение остаются одними на всю серию; перезапуска нет.
PCM снаружи не наблюдаем: очистку проверяет test_soak_releases_pcm в unit-тестах.
measured.sessions — снимок LoadResult последней загрузки, не текущий обход GC.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import resource
import select
import signal
import socket
import statistics
import subprocess
import sys
import time
from collections import Counter, deque
from collections.abc import Callable
from pathlib import Path
from types import FrameType
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
os.environ["QT_QPA_PLATFORM"] = "offscreen"

from astra_voice.core import policy, settings  # noqa: E402
from astra_voice.worker import ipc  # noqa: E402
from astra_voice.worker.supervisor import (  # noqa: E402
    Message,
    WorkerSupervisor,
    worker_command,
)

POLL_S = 0.01
LOAD_DEADLINE_S = 10.0


class CannotRun(RuntimeError):
    """Ожидаемая невозможность получить достоверный замер."""


class Reporter:
    """Выводит факты без журналирования диктовки и без буферизации stdout."""

    def __init__(self, command: str, as_json: bool) -> None:
        self.command = command
        self.as_json = as_json

    def emit(self, event: str, explanation: str, **facts: Any) -> None:
        """Сохраняет числа числами в JSON; человек получает пояснение с единицами."""
        data = {"command": self.command, "event": event, **facts, "explanation": explanation}
        print(json.dumps(data, ensure_ascii=False) if self.as_json else explanation, flush=True)


def verdict(passed: bool) -> str:
    """Возвращает одинаковые вердикты для всех порогов."""
    return "уложились" if passed else "не уложились"


def positive_int(raw: str) -> int:
    """Отклоняет пустую серию и неположительное число потоков."""
    value = int(raw)
    if value < 1:
        raise argparse.ArgumentTypeError("Нужно целое число от 1.")
    return value


def model_request(args: argparse.Namespace) -> Message:
    """Выбирает модель по правилам отладочного CLI, не изменяя настройки."""
    config_root = os.environ.get("XDG_CONFIG_HOME", "")
    config = Path(config_root) if config_root.startswith("/") else Path.home() / ".config"
    path = config / "astra-voice/settings.json"
    raw = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    if not isinstance(raw, dict):
        raise CannotRun("Настройки должны быть JSON-объектом.")
    effective = policy.effective(settings.from_dict(raw), policy.load()).to_dict()

    def option(name: str, default: Any = None) -> Any:
        """Приоритет: аргумент, новое имя настройки, старое имя, дефолт."""
        for value in (
            getattr(args, name, None),
            effective.get(f"model_{name}"),
            effective.get(name),
        ):
            if value is not None:
                return value
        return default

    directory = option("dir")
    model_id, revision = option("id"), option("revision")
    if args.model_dir is not None:
        directory = args.model_dir.expanduser().resolve()
        model_id, revision = directory.parent.name or "local-model", directory.name or "local"
    elif not model_id or not revision:
        raise CannotRun("Модель не настроена. Укажите --model-dir.")
    if directory is None:
        data_root = os.environ.get("XDG_DATA_HOME", "")
        data = Path(data_root) if data_root.startswith("/") else Path.home() / ".local/share"
        directory = data / "astra-voice/store" / model_id / revision
    directory = Path(directory).expanduser().resolve()
    if not directory.is_dir():
        raise CannotRun("Каталог модели не найден. Проверьте --model-dir.")
    request: Message = {
        "type": "model.load",
        "id": model_id,
        "revision": revision,
        "dir": str(directory),
        "layout": option("layout", "onnx-asr-gigaam-v3"),
        "variant": option("variant", "gigaam-v3-e2e-rnnt"),
        "threads": option("threads", 2),
        "min_ram_mb": option("min_ram_mb", 768),
    }
    if args.command in ("cpu-wall", "rlimit-load"):
        required = 2 if args.command == "cpu-wall" else 4
        if args.threads is not None and args.threads != required:
            raise CannotRun(f"Сценарий {args.command} требует --threads {required}.")
        request["threads"] = required
    ipc.encode(request)
    if request["threads"] < 1 or request["min_ram_mb"] < 1:
        raise CannotRun("Число потоков и объём памяти должны быть положительными.")
    return request


def limited_command(fd: int, limit_bytes: int) -> list[str]:
    """Запускает обёртку в дочернем процессе через публичный command_factory."""
    return [
        sys.executable,
        "-I",
        str(Path(__file__).resolve()),
        "--_rlimit-worker",
        str(fd),
        str(limit_bytes),
    ]


def limited_worker(fd: int, limit_bytes: int) -> None:
    """Устанавливает жёсткий предел перед exec штатного bootstrap; pid сохраняется."""
    resource.setrlimit(resource.RLIMIT_AS, (limit_bytes, limit_bytes))
    command = worker_command(fd)
    os.execv(command[0], command)


class Client:
    """Общий цикл ожидания для супервизора и последовательного IPC в soak."""

    def __init__(
        self,
        *,
        direct: bool = False,
        limit_bytes: int | None = None,
        on_event: Callable[[Message], None] = lambda event: None,
    ) -> None:
        self.events: deque[tuple[float, Message]] = deque()
        self.on_event = on_event
        self.connection: socket.socket | None = None
        self.child: subprocess.Popen[bytes] | None = None
        self.reader = ipc.FrameReader()
        self.supervisor = (
            None
            if direct
            else WorkerSupervisor(
                on_event=self.receive,
                use_qt=False,
                command_factory=(
                    worker_command
                    if limit_bytes is None
                    else lambda fd: limited_command(fd, limit_bytes)
                ),
            )
        )

    def receive(self, event: Message) -> None:
        """Ставит метку до пользовательского вывода."""
        self.events.append((time.monotonic(), event))
        self.on_event(event)

    @property
    def process(self) -> subprocess.Popen[bytes]:
        """Возвращает текущий процесс, не обращаясь к закрытым полям супервизора."""
        process = self.supervisor.process if self.supervisor is not None else self.child
        if process is None:
            raise CannotRun("Воркер не запущен: проверьте разрешение на сокеты и подпроцессы.")
        return process

    def start(self) -> None:
        """Создаёт только локальную пару Unix-сокетов и один дочерний процесс."""
        if self.supervisor is not None:
            self.supervisor.start()
            return
        parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self.child = subprocess.Popen(worker_command(child.fileno()), pass_fds=[child.fileno()])
        except BaseException:
            parent.close()
            raise
        finally:
            child.close()
        parent.settimeout(1)
        self.connection = parent

    def send(self, message: Message, timeout: float = 10) -> None:
        """Передаёт команду; прямой IPC использует внешний дедлайн wait."""
        if self.supervisor is not None:
            self.supervisor.send(message, timeout=timeout)
        else:
            if self.connection is None:
                raise CannotRun("Соединение с воркером не открыто.")
            self.connection.sendall(ipc.encode(message))

    def pump(self, timeout: float = POLL_S) -> None:
        """Обслуживает IPC, обнаруживая EOF и гибель прямого воркера."""
        if self.supervisor is not None:
            self.supervisor.pump(timeout)
            return
        if self.connection is None:
            raise CannotRun("Соединение с воркером не открыто.")
        readable, _, _ = select.select([self.connection], [], [], timeout)
        if readable:
            data = self.connection.recv(ipc.MAX_FRAME_BYTES)
            if not data:
                raise CannotRun("Воркер закрыл соединение до завершения сценария.")
            for event in self.reader.feed(data):
                self.receive(event)

    def wait(
        self, kind: str, timeout: float, *, allow_error: bool = False
    ) -> tuple[float, Message]:
        """Возвращает событие и время callback, никогда не ждёт бесконечно."""
        deadline = time.monotonic() + timeout
        while True:
            while self.events:
                received, event = self.events.popleft()
                if event["type"] == "error":
                    if allow_error:
                        return received, event
                    raise CannotRun(f"Воркер сообщил ошибку: {event['code']}.")
                if event["type"] == kind:
                    return received, event
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CannotRun(f"Не дождались события {kind} за {timeout:g} с.")
            if self.supervisor is not None and self.supervisor.state == "stopped":
                raise CannotRun("Супервизор остановился.")
            self.pump(min(POLL_S, remaining))

    def hello(self) -> None:
        """Проверяет наличие рантайма в том Python, которым запущен воркер."""
        self.start()
        _, event = self.wait("hello", 10)
        if event["runtime"]["onnxruntime"] is None:
            raise CannotRun("Движок onnxruntime недоступен в выбранном Python.")

    def load(self, request: Message, report: Reporter) -> None:
        """Загружает модель и печатает время, версию и фактическое число потоков."""
        self.send(request, LOAD_DEADLINE_S)
        _, event = self.wait("model.loaded", 12)
        report.emit(
            "model.loaded",
            f"Модель загружена: {event['load_ms']:.3f} мс; threads={request['threads']}; "
            f"{event['engine_version']}.",
            load_ms=event["load_ms"],
            threads=request["threads"],
            variant=request["variant"],
            engine_version=event["engine_version"],
            worker_pid=self.process.pid,
        )

    def stop(self) -> None:
        """Закрывает IPC и обязательно собирает дочерний процесс."""
        if self.supervisor is not None:
            self.supervisor.stop()
            return
        if self.connection is not None:
            self.connection.close()
        if self.child is not None:
            if self.child.poll() is None:
                self.child.terminate()
            try:
                self.child.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.child.kill()
                self.child.wait(timeout=2)


def serve(request: Message, report: Reporter) -> int:
    """Живёт до сигнала, считает ошибки и перезагружает модель после нового hello."""
    errors: Counter[int] = Counter()

    def received(event: Message) -> None:
        """Печатает каждую наружную ошибку, ничего не подавляя в стенде."""
        if event["type"] == "error":
            generation = int(event["generation"])
            errors[generation] += 1
            report.emit(
                "error",
                f"error #{errors[generation]} поколения {generation}: "
                + json.dumps(event, ensure_ascii=False),
                worker_event=event,
                count=errors[generation],
            )

    client = Client(on_event=received)
    supervisor = client.supervisor
    assert supervisor is not None
    stopping = False

    def stop_signal(signum: int, frame: FrameType | None) -> None:
        """Оставляет освобождение ресурсов обычному потоку исполнения."""
        nonlocal stopping
        stopping = True

    old_term = signal.signal(signal.SIGTERM, stop_signal)
    old_int = signal.signal(signal.SIGINT, stop_signal)
    try:
        # Асинхронная загрузка позволяет SIGTERM завершить даже зависший model.load.
        client.start()
        process = client.process
        generation = supervisor.generation
        last_alive = time.monotonic()
        heartbeat = last_alive
        startup_deadline = last_alive + 10
        loaded = False
        report.emit(
            "started",
            f"pid воркера={process.pid}; pid стенда={os.getpid()}; поколение={generation}.",
            worker_pid=process.pid,
            pid=os.getpid(),
            generation=generation,
        )
        while not stopping:
            before_poll = time.monotonic()
            if process.poll() is None:
                last_alive = before_poll
            client.pump()
            now = time.monotonic()
            if supervisor.generation != generation:
                old_pid, old_generation = process.pid, generation
                process, generation = client.process, supervisor.generation
                # Смерть случилась ПОСЛЕ последнего живого poll: это верхняя граница.
                elapsed = now - last_alive
                passed = elapsed <= 1
                report.emit(
                    "restarted",
                    f"Новый pid={process.pid}, поколение={generation}; после смерти прошло "
                    f"не более {elapsed:.6f} с; порог ≤ 1 с: {verdict(passed)}. "
                    f"Ошибок старого поколения: {errors[old_generation]}; ожидание 1: "
                    f"{verdict(errors[old_generation] == 1)}.",
                    worker_pid=process.pid,
                    generation=generation,
                    old_pid=old_pid,
                    old_generation=old_generation,
                    restart_upper_bound_s=elapsed,
                    threshold_s=1,
                    passed=passed,
                    error_count=errors[old_generation],
                    expected_errors=1,
                    errors_passed=errors[old_generation] == 1,
                    measurement="от последнего живого poll до обнаружения нового pid",
                )
                last_alive = now
                startup_deadline = now + 10
                loaded = False
            while client.events:
                _, event = client.events.popleft()
                if event["type"] == "hello" and event["generation"] == generation:
                    if event["runtime"]["onnxruntime"] is None:
                        raise CannotRun("Движок onnxruntime недоступен в выбранном Python.")
                    client.send(request, LOAD_DEADLINE_S)
                    startup_deadline = now + 12
                elif event["type"] == "model.loaded" and event["generation"] == generation:
                    loaded = True
                    report.emit(
                        "model.loaded",
                        f"Модель загружена в pid={process.pid}, поколение={generation}, "
                        f"load_ms={event['load_ms']:.3f}.",
                        worker_pid=process.pid,
                        generation=generation,
                        load_ms=event["load_ms"],
                    )
            if not loaded and now >= startup_deadline and supervisor.state == "running":
                raise CannotRun("Не дождались загрузки модели после запуска воркера.")
            if now >= heartbeat:
                report.emit(
                    "alive",
                    f"жив; pid={os.getpid()}; worker_pid={process.pid}; "
                    f"state={supervisor.state}; модель загружена={loaded}.",
                    pid=os.getpid(),
                    worker_pid=process.pid,
                    state=supervisor.state,
                    model_loaded=loaded,
                )
                heartbeat = now + 1
            if supervisor.state == "stopped":
                time.sleep(POLL_S)
        report.emit("stopped", "Стенд получил сигнал остановки.", errors=dict(errors))
        return 0
    finally:
        client.stop()
        signal.signal(signal.SIGTERM, old_term)
        signal.signal(signal.SIGINT, old_int)


def rlimit_load(request: Message, report: Reporter, rlimit_as_mb: int = 768) -> int:
    """Проверяет настоящий жёсткий RLIMIT_AS и дедлайн model.load через супервизор."""
    limit_bytes = rlimit_as_mb * 1024 * 1024
    client = Client(limit_bytes=limit_bytes)
    try:
        client.hello()
        limits = Path(f"/proc/{client.process.pid}/limits").read_text(encoding="ascii")
        line = next(line for line in limits.splitlines() if line.startswith("Max address space"))
        soft, hard = (int(value) for value in line.split()[3:5])
        if (soft, hard) != (limit_bytes, limit_bytes):
            raise CannotRun(
                f"Не удалось подтвердить жёсткий RLIMIT_AS {rlimit_as_mb} МиБ у воркера."
            )
        report.emit(
            "limit",
            f"RLIMIT_AS воркера: мягкий={rlimit_as_mb} МиБ, жёсткий={rlimit_as_mb} МиБ; "
            "threads=4; дедлайн=10 с.",
            soft_bytes=soft,
            hard_bytes=hard,
            threads=4,
            deadline_s=LOAD_DEADLINE_S,
            worker_pid=client.process.pid,
        )
        started = time.monotonic()
        client.send(request, LOAD_DEADLINE_S)
        try:
            received, event = client.wait("model.loaded", 12, allow_error=True)
        except CannotRun:
            received, event = time.monotonic(), {"type": "no-event"}
        elapsed = received - started
        # Таймер запускает kill/restart в 10 с; доставка callback имеет свою задержку.
        tolerance = 0.1
        passed = event.get("code") == "load-timeout" and abs(elapsed - LOAD_DEADLINE_S) <= tolerance
        report.emit(
            "summary",
            f"Пришло {event['type']} ({event.get('code', 'без кода')}) за {elapsed:.6f} с; "
            f"ожидание load-timeout по дедлайну 10 с "
            f"(допуск доставки ±{tolerance:g} с): {verdict(passed)}.",
            received=event,
            elapsed_s=elapsed,
            deadline_s=LOAD_DEADLINE_S,
            observation_tolerance_s=tolerance,
            passed=passed,
            exit_code=int(not passed),
        )
        return int(not passed)
    finally:
        client.stop()


def cpu_seconds(pid: int) -> float:
    """Читает utime+stime: имя процесса в скобках может содержать пробелы и скобки."""
    stat = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    fields = stat[stat.rindex(")") + 2 :].split()
    # fields[0] — поле 3 (state), utime/stime — поля 14/15.
    return (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK")


def cpu_wall(request: Message, report: Reporter) -> int:
    """Измеряет CPU всего процесса на одном transcribe.file с двумя потоками."""
    wav = ROOT / "data/test/test-ru-20s.wav"
    if not wav.is_file():
        raise CannotRun("Нет data/test/test-ru-20s.wav.")
    client = Client()
    try:
        client.hello()
        client.load(request, report)
        pid = client.process.pid
        cpu_before = cpu_seconds(pid)
        started = time.monotonic()
        client.send({"type": "transcribe.file", "path": str(wav)}, 120)
        received, event = client.wait("result", 122)
        cpu_after = cpu_seconds(pid)
        if client.process.pid != pid:
            raise CannotRun("Во время замера сменился процесс воркера.")
        wall = received - started
        cpu = cpu_after - cpu_before
        ratio = cpu / wall
        passed = ratio <= 2
        report.emit(
            "summary",
            f"cpu_ms={cpu * 1000:.3f}; wall_ms={wall * 1000:.3f}; "
            f"cpu/wall={ratio:.6f}; порог ≤ 2 при threads=2: {verdict(passed)}. "
            "Интервал включает чтение WAV и доставку результата IPC.",
            cpu_ms=cpu * 1000,
            wall_ms=wall * 1000,
            ratio=ratio,
            threshold=2,
            threads=2,
            t_ms=event["t_ms"],
            worker_pid=pid,
            cpu_tick_ms=1000 / os.sysconf("SC_CLK_TCK"),
            passed=passed,
            exit_code=int(not passed),
        )
        return int(not passed)
    finally:
        client.stop()


def measure(client: Client, report: Reporter, stage: str) -> Message:
    """Получает VmHWM/Pss из самого воркера, где /proc/self доступен после hardening."""
    client.send({"type": "measure"})
    _, event = client.wait("measured", 12)
    report.emit(
        "measured",
        f"{stage}: VmHWM={event['vm_hwm_kb']} кБ; Pss={event['pss_kb']} кБ; "
        f"sessions={event.get('sessions', 'не передано')}.",
        stage=stage,
        vm_hwm_kb=event["vm_hwm_kb"],
        pss_kb=event["pss_kb"],
        sessions=event.get("sessions"),
    )
    return event


def soak(request: Message, runs: int, report: Reporter) -> int:
    """Выполняет серию в одном процессе и выдаёт измерения без выдуманного порога утечки."""
    wav = ROOT / "data/test/test-ru-6s.wav"
    if not wav.is_file():
        raise CannotRun("Нет data/test/test-ru-6s.wav.")
    client = Client(direct=True)
    try:
        client.hello()
        client.load(request, report)
        pid = client.process.pid
        report.emit(
            "method",
            "Серия: штатный IPC напрямую, transcribe.file; один pid и одна загрузка модели. "
            "Супервизор не используется: служебный id file нельзя повторять в его поколении.",
            mode="transcribe.file",
            transport="direct-ipc",
            worker_pid=pid,
            runs=runs,
        )
        before = measure(client, report, "до серии")
        durations: list[float] = []
        for index in range(runs):
            client.send({"type": "transcribe.file", "path": str(wav)}, 120)
            _, event = client.wait("result", 120)
            if event["utterance_id"] != "file" or client.process.pid != pid:
                raise CannotRun("Результат не принадлежит ожидаемой серии.")
            durations.append(float(event["t_ms"]))
            report.emit(
                "run",
                f"Прогон {index + 1}/{runs}: t_ms={durations[-1]:.3f}.",
                run=index + 1,
                t_ms=durations[-1],
                worker_pid=pid,
            )
        after = measure(client, report, "после серии")
        growth = int(after["vm_hwm_kb"]) - int(before["vm_hwm_kb"])
        median = statistics.median(durations)
        p95 = sorted(durations)[math.ceil(0.95 * runs) - 1]
        passed = before.get("sessions") == after.get("sessions") == 3
        report.emit(
            "summary",
            f"VmHWM до/после: {before['vm_hwm_kb']}/{after['vm_hwm_kb']} кБ; "
            f"прирост={growth} кБ (порог прироста в плане не задан). "
            f"sessions до/после={before.get('sessions')}/{after.get('sessions')}; "
            f"ожидание 3: {verdict(passed)}. "
            f"t_ms={durations}; медиана={median:.3f} мс; p95={p95:.3f} мс (ближайший ранг). "
            "Это t_ms всего запроса, не чистого инференса tools/benchmark. "
            "sessions — снимок последней загрузки, не текущий счётчик живых сессий. "
            "PCM: снаружи не наблюдаемо; очистка покрыта unit-тестом test_soak_releases_pcm.",
            worker_pid=pid,
            runs=runs,
            vm_hwm_before_kb=before["vm_hwm_kb"],
            vm_hwm_after_kb=after["vm_hwm_kb"],
            vm_hwm_growth_kb=growth,
            vm_hwm_growth_threshold_kb=None,
            pss_before_kb=before["pss_kb"],
            pss_after_kb=after["pss_kb"],
            sessions_before=before.get("sessions"),
            sessions_after=after.get("sessions"),
            expected_sessions=3,
            sessions_source="LoadResult последней загрузки",
            t_ms=durations,
            median_ms=median,
            p95_ms=p95,
            p95_method="nearest-rank",
            pcm_buffers="снаружи не наблюдаемо",
            pcm_unit_test="tests/unit/test_worker_state.py::test_soak_releases_pcm",
            passed=passed,
            exit_code=int(not passed),
        )
        return int(not passed)
    finally:
        client.stop()


def main(argv: list[str] | None = None) -> int:
    """Предоставляет четыре сценария с общими аргументами после подкоманды."""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("serve", "жить до сигнала, показывать ошибки и перезапуски"),
        ("rlimit-load", "дедлайн загрузки при ограничении памяти и 4 потоках"),
        ("cpu-wall", "отношение CPU/wall при 2 потоках"),
        ("soak", "серия распознаваний для проверки памяти"),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--model-dir", type=Path, help="каталог модели вместо настройки")
        command.add_argument("--variant", help="вариант модели вместо настройки")
        command.add_argument(
            "--threads", type=positive_int, help="потоки (cpu-wall=2, rlimit-load=4)"
        )
        command.add_argument("--runs", type=positive_int, default=20, help="длина серии soak")
        command.add_argument("--json", action="store_true", help="JSON Lines в stdout")
        if name == "rlimit-load":
            command.add_argument(
                "--rlimit-as-mb",
                type=positive_int,
                default=768,
                help="предел адресного пространства в МиБ (по умолчанию 768 — значение из плана)",
            )
    args = parser.parse_args(argv)
    report = Reporter(args.command, args.json)
    try:
        request = model_request(args)
        if args.command == "serve":
            return serve(request, report)
        if args.command == "rlimit-load":
            return rlimit_load(request, report, args.rlimit_as_mb)
        if args.command == "cpu-wall":
            return cpu_wall(request, report)
        return soak(request, args.runs, report)
    except (OSError, TypeError, ValueError, RuntimeError, ipc.FrameError, StopIteration) as exc:
        report.emit("unavailable", f"Прогнать не удалось: {exc}", exit_code=2, passed=None)
        return 2
    except KeyboardInterrupt:
        report.emit("interrupted", "Прогон прерван пользователем.", exit_code=2, passed=None)
        return 2


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--_rlimit-worker":
        limited_worker(int(sys.argv[2]), int(sys.argv[3]))
    else:
        raise SystemExit(main())
