"""Выбор устройств записи и источники PCM16: PulseAudio и WAV-файл."""

from __future__ import annotations

import ctypes
import json
import logging
import math
import os
import subprocess
import sys
import threading
import time
import unicodedata
import wave
from array import array
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from astra_voice.worker.state import WorkerState

RATE = 16_000
CHANNELS = 1
SAMPLE_BYTES = 2  # s16le
CHUNK_MS = 20
CHUNK_BYTES = RATE * CHANNELS * SAMPLE_BYTES * CHUNK_MS // 1000
SILENCE_DBFS = -60.0
SILENCE_HOLD_S = 2.0
LEVEL_RATE_HZ = 30
OPEN_FIRST_RETRIES = 3
OPEN_FIRST_PAUSE_S = 1.0
STOP_WATCHDOG_S = 5.0

PA_SAMPLE_S16LE = 3
PA_STREAM_RECORD = 2
U32_MAX = 0xFFFFFFFF
PA_ERR_ACCESS = 1
PA_ERR_NOENTITY = 5
PA_ERR_BUSY = 26

OPEN_RETRIES = 3
OPEN_RETRY_MS = 300
OPEN_DEADLINE_S = 2.0
OPEN_TOTAL_DEADLINE_S = 8.0
MAX_DEVICE_LABEL = 120

ERROR_NO_DEVICE = "audio-no-device"
ERROR_BUSY = "audio-busy"
ERROR_FAILED = "audio-failed"

logger = logging.getLogger(__name__)


class AudioError(Exception):
    """Ошибка звука с машинным кодом и понятным человеку пояснением."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code: str = code
        self.message: str = message


class CaptureStopTimeout(RuntimeError):
    """Поток захвата не вышел после отмены; источник нельзя закрывать извне."""


class _OpenDeadline:
    """Общий бюджет подготовки источника по часам владельца захвата."""

    def __init__(self, clock: Callable[[], float]) -> None:
        self._clock = clock
        self._end = clock() + OPEN_TOTAL_DEADLINE_S

    def remaining(self, limit: float) -> float:
        """Ограничивает ожидание остатком бюджета или сообщает об истечении."""
        remaining = self._end - self._clock()
        if remaining <= 0:
            raise AudioError(ERROR_FAILED, "Не удалось вовремя подготовить запись звука.")
        return min(limit, remaining)


def _clean_device_description(description: str) -> str:
    """Сворачивает пробелы и удаляет управляющие и форматные символы чужого ввода."""
    normalized = " ".join(description.split())
    cleaned = "".join(char for char in normalized if unicodedata.category(char) not in {"Cc", "Cf"})
    return " ".join(cleaned.split())


def _device_label(description: str, suffix: str = "") -> str:
    """Обрезает подпись по символам, сохраняя явную пометку монитора."""
    limit = MAX_DEVICE_LABEL - len(suffix)
    if len(description) > limit:
        description = description[: limit - 1].rstrip() + "…"
    return description + suffix


@dataclass(frozen=True)
class AudioDevice:
    """Источник записи с техническим именем и подписью для выбора."""

    index: int
    name: str
    description: str
    monitor: bool

    @property
    def label(self) -> str:
        """Возвращает подпись с явным предупреждением о записи звука колонок."""
        return _device_label(self.description, " (звук системы)" if self.monitor else "")


def _device_descriptions(
    run: Callable[..., subprocess.CompletedProcess[str]],
    env: dict[str, str],
    deadline: _OpenDeadline | None = None,
) -> dict[str, str]:
    """Дополняет список описаниями, сохраняя работоспособность без поддержки JSON."""
    try:
        result = run(
            ["pactl", "-f", "json", "list", "sources"],
            capture_output=True,
            text=True,
            timeout=5 if deadline is None else deadline.remaining(5),
            env=env,
            shell=False,
            check=False,
        )
        if result.returncode != 0:
            logger.debug("Не удалось получить описания устройств записи.")
            return {}
        payload: object = json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, ValueError, RecursionError):
        # Ответ и исключение могут содержать технические имена и пути.
        logger.debug("Не удалось прочитать описания устройств записи.")
        return {}

    if not isinstance(payload, list):
        logger.debug("Неверный формат списка описаний устройств записи.")
        return {}
    descriptions: dict[str, str] = {}
    for item in payload:
        if not isinstance(item, dict):
            logger.debug("Пропущено некорректное описание устройства записи.")
            continue
        name = item.get("name")
        description = item.get("description")
        if isinstance(name, str) and isinstance(description, str):
            description = _clean_device_description(description)
            if description:
                descriptions[name] = description
                continue
        logger.debug("Пропущено некорректное описание устройства записи.")
    return descriptions


def list_devices(
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    deadline: _OpenDeadline | None = None,
) -> list[AudioDevice]:
    """Возвращает источники из краткого списка, по возможности дополняя описаниями."""
    env = {**os.environ, "LC_ALL": "C"}
    message = "Не удалось получить список устройств записи."
    try:
        result = run(
            ["pactl", "list", "short", "sources"],
            capture_output=True,
            text=True,
            timeout=5 if deadline is None else deadline.remaining(5),
            env=env,
            shell=False,
            check=False,
        )
    except (OSError, subprocess.SubprocessError, UnicodeError):
        raise AudioError(ERROR_FAILED, message) from None
    if result.returncode != 0:
        raise AudioError(ERROR_FAILED, message)

    descriptions = _device_descriptions(run, env, deadline)
    devices: list[AudioDevice] = []
    fallback_counts: dict[str, int] = {}
    for line in result.stdout.splitlines():
        fields = line.split("\t")
        if len(fields) < 2 or any(not field.strip() for field in fields[:2]):
            continue
        try:
            index = int(fields[0])
        except ValueError:
            continue
        if index < 0:
            continue
        name = fields[1]
        monitor = name.endswith(".monitor")
        description = descriptions.get(name)
        if description is None:
            fallback = "Звук системы" if monitor else "Микрофон"
            count = fallback_counts.get(fallback, 0) + 1
            fallback_counts[fallback] = count
            description = fallback if count == 1 else f"{fallback} {count}"
        devices.append(
            AudioDevice(
                index=index,
                name=name,
                description=description,
                monitor=monitor,
            )
        )
    return devices


def default_device(
    devices: list[AudioDevice],
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    deadline: _OpenDeadline | None = None,
) -> AudioDevice:
    """Проверяет фактическое умолчание, запрещая неявную запись звука системы."""
    message = "Микрофон не найден. Выберите устройство записи в настройках."
    try:
        result = run(
            ["pactl", "get-default-source"],
            capture_output=True,
            text=True,
            timeout=5 if deadline is None else deadline.remaining(5),
            env={**os.environ, "LC_ALL": "C"},
            shell=False,
            check=False,
        )
    except (OSError, subprocess.SubprocessError, UnicodeError):
        logger.debug("Не удалось узнать устройство записи по умолчанию.")
        raise AudioError(ERROR_NO_DEVICE, message) from None
    if result.returncode != 0:
        logger.debug("Не удалось узнать устройство записи по умолчанию.")
        raise AudioError(ERROR_NO_DEVICE, message)

    name = result.stdout.strip()
    if not name or name in ("@DEFAULT_SOURCE@", "@NONE@"):
        logger.debug("Не задано устройство записи по умолчанию: %r", name)
        raise AudioError(ERROR_NO_DEVICE, message)
    for device in devices:
        if device.name == name:
            if device.monitor:
                logger.debug("Устройство записи по умолчанию — монитор: %r", name)
                raise AudioError(
                    ERROR_NO_DEVICE,
                    "Микрофон не найден: звук по умолчанию — это звук системы. "
                    "Выберите микрофон в настройках.",
                )
            return device
    logger.debug("Устройство записи по умолчанию отсутствует в списке: %r", name)
    raise AudioError(ERROR_NO_DEVICE, message)


def resolve_device(name: str | None, devices: list[AudioDevice]) -> AudioDevice | None:
    """Проверяет явный выбор до открытия; отсутствие выбора обозначается через None."""
    if name is None:
        return None
    for device in devices:
        if device.name == name:
            return device
    # S5-R1: иначе сервер может молча открыть другой микрофон (У39, T-35).
    raise AudioError(
        ERROR_NO_DEVICE,
        "Выбранный микрофон недоступен. Выберите устройство записи в настройках.",
    )


def describe_change(previous: AudioDevice | None, current: AudioDevice | None) -> str | None:
    """Сообщает о смене выбора или описания; None означает выбор по умолчанию."""
    if previous is None and current is None:
        return None
    if previous is not None and current is not None:
        if (previous.name, previous.description, previous.monitor) == (
            current.name,
            current.description,
            current.monitor,
        ):
            return None
    label = current.label if current is not None else "устройство по умолчанию"
    return f"Источник звука изменился: {label}"


class AudioSource(Protocol):
    """Источник порций PCM16, моно, 16 кГц с явным временем жизни."""

    def open(self, device: str | None) -> None:
        """Открывает выбранный источник или сообщает об ошибке."""
        ...

    def read_chunk(self) -> bytes | None:
        """Возвращает CHUNK_BYTES байт либо None при завершении чтения."""
        ...

    def flush(self) -> None:
        """Сбрасывает звук, накопленный до начала текущей записи."""
        ...

    def close(self) -> None:
        """Освобождает источник; повторное закрытие допустимо."""
        ...

    @property
    def is_open(self) -> bool:
        """Показывает, открыт ли источник."""
        ...

    @property
    def ended(self) -> bool:
        """Показывает, что данные закончились штатно, без сбоя."""
        ...

    @property
    def device_name(self) -> str | None:
        """Возвращает имя, запрошенное при открытии."""
        ...


def dbfs(peak: float) -> float:
    """Переводит амплитуду PCM16 в дБFS с конечным значением для нуля."""
    if peak <= 0:
        return -120.0
    return round(20 * math.log10(peak / 32768.0), 1)


def normalize(chunk: bytes) -> array[float]:
    """Преобразует s16le в нормализованные отсчёты без промежуточных файлов."""
    samples = array("h")
    samples.frombytes(chunk)
    if sys.byteorder != "little":
        samples.byteswap()
    return array("f", (sample / 32768.0 for sample in samples))


class AudioCapture:
    """Открывает и читает источник в потоке, передавая владение PCM автомату.

    Владелец последовательно вызывает start и stop. Каждая запись получает
    собственный флаг остановки; новый поток ждёт выхода предыдущего внутри себя.
    Callback-и должны быстро передавать отсчёты и события владельцу.
    """

    def __init__(
        self,
        *,
        source: AudioSource,
        on_samples: Callable[[str, array[float]], bool],
        on_event: Callable[[dict[str, Any]], None],
        on_error: Callable[[str, str, str], None],
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._source = source
        self._on_samples = on_samples
        self._on_event = on_event
        self._on_error = on_error
        self._clock = clock
        self._sleep = sleep
        self._running = threading.Event()
        self._thread: threading.Thread | None = None
        self._watchdog_lock = threading.Lock()
        self._stop_deadlines: dict[threading.Thread, float] = {}
        self._first_open = True
        self._previous_device: AudioDevice | None = None

    def start(self, utterance_id: str, device: str | None) -> None:
        """Запускает запись, не ожидая открытия устройства в вызывающем потоке."""
        self.request_stop()
        self.check_stop_watchdog()
        previous = self._thread
        running = threading.Event()
        running.set()
        self._running = running
        self._thread = threading.Thread(
            target=self._run,
            args=(utterance_id, device, running, previous),
            name="audio-capture",
            daemon=True,
        )
        self._thread.start()

    def _open(self, device: str | None, running: threading.Event) -> bool | None:
        """Возвращает факт нового открытия или None при остановке.

        Повторяет первое открытие при гонке автозапуска со звуковой службой.
        """
        deadline = _OpenDeadline(self._clock)
        retries = OPEN_FIRST_RETRIES if self._first_open else 0
        for attempt in range(retries + 1):
            if not running.is_set():
                return None
            deadline.remaining(OPEN_TOTAL_DEADLINE_S)
            try:
                opened_now = False
                if not self._source.is_open:
                    if isinstance(self._source, PulseSimpleSource):
                        self._source.open(device, deadline=deadline, running=running)
                    else:
                        self._source.open(device)
                    opened_now = True
            except AudioError:
                if not running.is_set():
                    return None
                deadline.remaining(OPEN_TOTAL_DEADLINE_S)
                if attempt == retries:
                    raise
                # S5-R1/У39: при ERROR_NO_DEVICE ждём появления выбранного имени
                # после перезапуска службы; устройство по умолчанию не подставляем.
                self._sleep(deadline.remaining(OPEN_FIRST_PAUSE_S))
            else:
                if not running.is_set():
                    return None
                deadline.remaining(OPEN_TOTAL_DEADLINE_S)
                self._first_open = False
                return opened_now
        return None

    def _run(
        self,
        uid: str,
        device: str | None,
        running: threading.Event,
        previous: threading.Thread | None,
    ) -> None:
        """Владеет циклом чтения; лимит и хранение отсчётов остаются у автомата."""
        try:
            if previous is not None:
                previous.join()
            opened_now = self._open(device, running)
            if opened_now is None:
                return
            if opened_now:
                current = (
                    self._source.selected_device
                    if isinstance(self._source, PulseSimpleSource)
                    else None
                )
                changed = describe_change(self._previous_device, current)
                self._previous_device = current
                if changed is not None:
                    changed = _device_label(changed)
                label = getattr(self._source, "device_label", None)
                if isinstance(label, str) and label.strip():
                    label = _device_label(label)
                    logger.info("%s", _device_label(f"Источник записи готов: {label}"))
                else:
                    label = None
                self._on_event(WorkerState.audio_ready(device=label, changed=changed))
            else:
                self._source.flush()
            self._read(uid, running)
        except AudioError as err:
            if running.is_set():
                self._on_error(uid, err.code, err.message)
        finally:
            # Даже pa_simple_free может зависнуть: сторож действует до выхода потока.
            self._mark_stopping(threading.current_thread(), running)
            self._source.close()

    def _read(self, uid: str, running: threading.Event) -> None:
        """Передаёт PCM, прореживает уровни по часам и один раз сообщает о тишине."""
        silence_since = self._clock()
        silent_sent = False
        last_level: float | None = None
        while running.is_set():
            chunk = self._source.read_chunk()
            if not running.is_set():
                return
            if chunk is None:
                if self._source.ended:
                    return
                self._source.close()
                raise AudioError(ERROR_FAILED, "Запись звука прервалась.")
            samples = array("h")
            samples.frombytes(chunk)
            if sys.byteorder != "little":
                samples.byteswap()
            peak = max((abs(sample) for sample in samples), default=0)
            rms = (
                math.sqrt(sum(sample * sample for sample in samples) / len(samples))
                if samples
                else 0.0
            )
            if not self._on_samples(uid, normalize(chunk)) or not running.is_set():
                return
            now = self._clock()
            peak_dbfs = dbfs(peak)
            if last_level is None or now - last_level >= 1 / LEVEL_RATE_HZ:
                self._on_event(
                    {
                        "type": "level",
                        "utterance_id": uid,
                        "rms_dbfs": dbfs(rms),
                        "peak_dbfs": peak_dbfs,
                    }
                )
                last_level = now
            if peak_dbfs >= SILENCE_DBFS:
                silence_since = now
            elif not silent_sent and now - silence_since >= SILENCE_HOLD_S:
                self._on_event({"type": "silent", "utterance_id": uid})
                silent_sent = True

    def request_stop(self) -> None:
        """Снимает флаг работы без ожидания потока и обращения к источнику."""
        self._mark_stopping(self._thread, self._running)

    def _mark_stopping(self, thread: threading.Thread | None, running: threading.Event) -> None:
        """Сохраняет первый срок остановки; новая запись не скрывает старый поток."""
        with self._watchdog_lock:
            running.clear()
            if thread is not None:
                self._stop_deadlines.setdefault(thread, time.monotonic() + STOP_WATCHDOG_S)

    def check_stop_watchdog(self) -> None:
        """Сообщает владельцу о зависании, в том числе после фоновой отмены."""
        with self._watchdog_lock:
            for thread, deadline in list(self._stop_deadlines.items()):
                if not thread.is_alive():
                    del self._stop_deadlines[thread]
                elif time.monotonic() >= deadline:
                    raise CaptureStopTimeout("Поток захвата не завершился после отмены.")

    def stop(self) -> None:
        """Ждёт выхода владельца до срока сторожа; живой источник не трогает."""
        self.request_stop()
        thread = self._thread
        if thread is None:
            # Поток никогда не запускался: конкурирующего владельца заведомо нет.
            self._source.close()
            return
        if thread is threading.current_thread():
            raise RuntimeError("Поток захвата не может ожидать сам себя.")
        while thread.is_alive():
            self.check_stop_watchdog()
            with self._watchdog_lock:
                deadline = min(self._stop_deadlines.values(), default=time.monotonic())
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
        self.check_stop_watchdog()

    @property
    def active(self) -> bool:
        """Показывает, запрошены ли открытие или чтение текущей записи."""
        return self._running.is_set()


class _PaSampleSpec(ctypes.Structure):
    """Формат отсчётов в представлении libpulse."""

    _fields_ = [
        ("format", ctypes.c_int),
        ("rate", ctypes.c_uint32),
        ("channels", ctypes.c_uint8),
    ]


class _PaBufferAttr(ctypes.Structure):
    """Параметры серверного буфера в представлении libpulse."""

    _fields_ = [
        ("maxlength", ctypes.c_uint32),
        ("tlength", ctypes.c_uint32),
        ("prebuf", ctypes.c_uint32),
        ("minreq", ctypes.c_uint32),
        ("fragsize", ctypes.c_uint32),
    ]


class _Pulse:
    """Обёртка libpulse; создаётся только при первом открытии источника."""

    def __init__(self) -> None:
        try:
            self.lib = ctypes.CDLL("libpulse-simple.so.0", use_errno=True)
            self.libpulse = ctypes.CDLL("libpulse.so.0", use_errno=True)
        except OSError:
            raise AudioError(ERROR_FAILED, "Звуковая подсистема недоступна.") from None

        self.lib.pa_simple_new.argtypes = [
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.POINTER(_PaSampleSpec),
            ctypes.c_void_p,
            ctypes.POINTER(_PaBufferAttr),
            ctypes.POINTER(ctypes.c_int),
        ]
        # Без restype указатель усекается до 32 бит и чтение завершается SIGSEGV.
        self.lib.pa_simple_new.restype = ctypes.c_void_p
        self.lib.pa_simple_read.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_int),
        ]
        self.lib.pa_simple_read.restype = ctypes.c_int
        self.lib.pa_simple_flush.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)]
        self.lib.pa_simple_flush.restype = ctypes.c_int
        self.lib.pa_simple_get_latency.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)]
        self.lib.pa_simple_get_latency.restype = ctypes.c_uint64
        self.lib.pa_simple_free.argtypes = [ctypes.c_void_p]
        self.lib.pa_simple_free.restype = None
        self.libpulse.pa_strerror.argtypes = [ctypes.c_int]
        self.libpulse.pa_strerror.restype = ctypes.c_char_p

    def strerror(self, code: int) -> str:
        """Возвращает технический текст ошибки только для диагностики."""
        message: bytes | None = self.libpulse.pa_strerror(code)
        return message.decode("utf-8", "replace") if message else f"код {code}"

    def open(
        self, device: str, *, timeout: float = OPEN_DEADLINE_S
    ) -> tuple[ctypes.c_void_p | None, int]:
        """Открывает в потоке владельца и отбрасывает опоздавший дескриптор."""
        deadline = time.monotonic() + min(OPEN_DEADLINE_S, timeout)
        spec = _PaSampleSpec(PA_SAMPLE_S16LE, RATE, CHANNELS)
        attr = _PaBufferAttr(U32_MAX, U32_MAX, U32_MAX, U32_MAX, CHUNK_BYTES)
        err = ctypes.c_int(0)
        # C-вызов нельзя прервать дедлайном. При зависании после отмены воркер
        # завершит процесс по сторожу; отдельный бесхозный pulse-open недопустим.
        raw = self.lib.pa_simple_new(
            None,
            b"astra-voice",
            PA_STREAM_RECORD,
            device.encode("utf-8"),
            b"dictation",
            ctypes.byref(spec),
            None,
            ctypes.byref(attr),
            ctypes.byref(err),
        )
        opened = ctypes.c_void_p(raw) if raw else None
        if time.monotonic() > deadline:
            if opened is not None:
                self.lib.pa_simple_free(opened)
            logger.debug("Истёк дедлайн открытия устройства записи.")
            return None, 0
        return opened, err.value


class PulseSimpleSource:
    """Источник записи через libpulse-simple; вызовы владельца последовательны."""

    def __init__(
        self,
        *,
        devices: Callable[[], list[AudioDevice]] | None = None,
        default: Callable[..., AudioDevice] = default_device,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._devices = devices
        self._default = default
        self._sleep = sleep
        self._pulse: _Pulse | None = None
        self._handle: ctypes.c_void_p | None = None
        self._buffer = (ctypes.c_char * CHUNK_BYTES)()
        self._device_name: str | None = None
        self._selected_device: AudioDevice | None = None
        self.device_label: str | None = None

    def open(
        self,
        device: str | None,
        *,
        deadline: _OpenDeadline | None = None,
        running: threading.Event | None = None,
    ) -> None:
        """Проверяет выбор и отмену перед каждой попыткой открытия."""
        if running is not None and not running.is_set():
            return
        self.close()
        devices = list_devices(deadline=deadline) if self._devices is None else self._devices()
        if deadline is not None:
            deadline.remaining(OPEN_TOTAL_DEADLINE_S)
        # У44: проверяем умолчание до загрузки libpulse и открываем только конкретное имя.
        selected = (
            self._default(devices, deadline=deadline)
            if device is None
            else resolve_device(device, devices)
        )
        assert selected is not None
        if deadline is not None:
            deadline.remaining(OPEN_TOTAL_DEADLINE_S)
        if selected.monitor:
            logger.info("Выбран источник записи: %s", selected.label)
        if self._pulse is None:
            self._pulse = _Pulse()
        error = 0
        for attempt in range(OPEN_RETRIES):
            if running is not None and not running.is_set():
                return
            timeout = OPEN_DEADLINE_S if deadline is None else deadline.remaining(OPEN_DEADLINE_S)
            handle, error = self._pulse.open(selected.name, timeout=timeout)
            if handle is not None:
                self._handle = handle
                self._device_name = selected.name
                self._selected_device = selected
                self.device_label = selected.label
                return
            if running is not None and not running.is_set():
                return
            logger.debug("Не удалось открыть запись: %s", self._pulse.strerror(error))
            if attempt + 1 < OPEN_RETRIES:
                pause = OPEN_RETRY_MS / 1000
                self._sleep(pause if deadline is None else deadline.remaining(pause))
        if error in (PA_ERR_BUSY, PA_ERR_ACCESS):
            raise AudioError(ERROR_BUSY, "Микрофон занят другой программой.")
        if error == PA_ERR_NOENTITY:
            raise AudioError(
                ERROR_NO_DEVICE,
                "Выбранный микрофон недоступен. Выберите устройство записи в настройках.",
            )
        raise AudioError(ERROR_FAILED, "Не удалось включить запись звука.")

    def read_chunk(self) -> bytes | None:
        """Читает одну порцию в переиспользуемый буфер; при ошибке возвращает None."""
        if self._handle is None or self._pulse is None:
            return None
        err = ctypes.c_int(0)
        rc = self._pulse.lib.pa_simple_read(
            self._handle, self._buffer, CHUNK_BYTES, ctypes.byref(err)
        )
        if rc < 0:
            logger.debug("Ошибка чтения звука: %s", self._pulse.strerror(err.value))
            return None
        return bytes(self._buffer)

    def flush(self) -> None:
        """Сбрасывает серверный буфер перед повторным использованием источника."""
        if self._handle is None or self._pulse is None:
            return
        err = ctypes.c_int(0)
        if self._pulse.lib.pa_simple_flush(self._handle, ctypes.byref(err)) < 0:
            logger.debug("Ошибка сброса звука: %s", self._pulse.strerror(err.value))
            raise AudioError(ERROR_FAILED, "Не удалось подготовить запись звука.")

    def close(self) -> None:
        """Освобождает дескриптор ровно один раз и сбрасывает выбранное устройство."""
        handle, self._handle = self._handle, None
        self._device_name = None
        self._selected_device = None
        self.device_label = None
        if handle is not None and self._pulse is not None:
            self._pulse.lib.pa_simple_free(handle)

    @property
    def is_open(self) -> bool:
        """Показывает, принадлежит ли источнику открытый дескриптор."""
        return self._handle is not None

    @property
    def ended(self) -> bool:
        """Микрофон не завершает поток данных штатно сам по себе."""
        return False

    @property
    def device_name(self) -> str | None:
        """Возвращает конкретное имя, переданное libpulse при открытии."""
        return self._device_name

    @property
    def selected_device(self) -> AudioDevice | None:
        """Возвращает проверенное устройство текущего открытия, None — источник закрыт."""
        return self._selected_device

    def latency_us(self) -> int | None:
        """Возвращает задержку в микросекундах либо None при отсутствии значения."""
        if self._handle is None or self._pulse is None:
            return None
        err = ctypes.c_int(0)
        latency = int(self._pulse.lib.pa_simple_get_latency(self._handle, ctypes.byref(err)))
        if err.value or latency == (1 << 64) - 1:
            logger.debug("Не удалось получить задержку: %s", self._pulse.strerror(err.value))
            return None
        return latency


class WavFileSource:
    """Читает существующий WAV PCM16 без создания файлов и записи на диск."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._wav: wave.Wave_read | None = None
        self._device_name: str | None = None
        self._ended = False

    def open(self, device: str | None) -> None:
        """Открывает файл на чтение и проверяет формат до получения отсчётов."""
        self.close()
        self._ended = False
        message = "Звуковой файл должен быть WAV PCM 16 бит, моно, 16 кГц."
        try:
            wav = wave.open(str(self._path), "rb")
        except (wave.Error, EOFError, ValueError):
            raise AudioError(ERROR_FAILED, message) from None
        except OSError:
            raise AudioError(ERROR_FAILED, "Не удалось открыть звуковой файл.") from None
        if (wav.getframerate(), wav.getsampwidth(), wav.getnchannels(), wav.getcomptype()) != (
            RATE,
            SAMPLE_BYTES,
            CHANNELS,
            "NONE",
        ):
            wav.close()
            raise AudioError(ERROR_FAILED, message)
        self._wav = wav
        self._device_name = device

    def read_chunk(self) -> bytes | None:
        """Читает порцию, дополняя последнюю нулями до CHUNK_BYTES байт."""
        if self._wav is None:
            return None
        data = self._wav.readframes(CHUNK_BYTES // (SAMPLE_BYTES * CHANNELS))
        if not data:
            self._ended = True
            return None
        return data.ljust(CHUNK_BYTES, b"\x00")

    def flush(self) -> None:
        """Не требует сброса: файл не накапливает звук во время паузы."""

    def close(self) -> None:
        """Закрывает файл; повторный вызов ничего не делает."""
        wav, self._wav = self._wav, None
        self._device_name = None
        if wav is not None:
            wav.close()

    @property
    def is_open(self) -> bool:
        """Показывает, открыт ли файл, в том числе после конца данных."""
        return self._wav is not None

    @property
    def ended(self) -> bool:
        """Показывает, достигнут ли конец данных при чтении файла."""
        return self._ended

    @property
    def device_name(self) -> str | None:
        """Возвращает сохранённое имя, не влияющее на выбор WAV-файла."""
        return self._device_name
