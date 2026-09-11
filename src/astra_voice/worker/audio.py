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
        if isinstance(name, str) and isinstance(description, str) and description.strip():
            descriptions[name] = description
        else:
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
        self._first_open = True
        self._previous_device: AudioDevice | None = None

    def start(self, utterance_id: str, device: str | None) -> None:
        """Запускает запись, не ожидая открытия устройства в вызывающем потоке."""
        self._running.clear()
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
                        self._source.open(device, deadline=deadline)
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
                    # stop мог закрыть источник, пока блокирующее open ещё выполнялось.
                    self._source.close()
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
                    logger.info("Источник записи готов: %s", label)
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
            running.clear()
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
        self._running.clear()

    def stop(self) -> None:
        """Снимает флаг, ждёт поток не более двух секунд и закрывает источник."""
        self.request_stop()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        self._source.close()

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
        self, device: str | None, *, timeout: float = OPEN_DEADLINE_S
    ) -> tuple[ctypes.c_void_p | None, int]:
        """Ограничивает одну попытку дедлайном и освобождает опоздавший дескриптор."""
        done = threading.Event()
        lock = threading.Lock()
        abandoned = False
        handle: ctypes.c_void_p | None = None
        error = 0
        deadline = time.monotonic() + min(OPEN_DEADLINE_S, timeout)

        def connect() -> None:
            """Владеет дескриптором до передачи ожидающему вызывающему коду."""
            nonlocal handle, error
            spec = _PaSampleSpec(PA_SAMPLE_S16LE, RATE, CHANNELS)
            attr = _PaBufferAttr(U32_MAX, U32_MAX, U32_MAX, U32_MAX, CHUNK_BYTES)
            err = ctypes.c_int(0)
            raw = self.lib.pa_simple_new(
                None,
                b"astra-voice",
                PA_STREAM_RECORD,
                device.encode("utf-8") if device is not None else None,
                b"dictation",
                ctypes.byref(spec),
                None,
                ctypes.byref(attr),
                ctypes.byref(err),
            )
            opened = ctypes.c_void_p(raw) if raw else None
            with lock:
                # Решение о передаче и отказ по дедлайну не могут обогнать друг друга.
                if not abandoned and time.monotonic() <= deadline:
                    handle, error = opened, err.value
                    done.set()
                    return
            if opened is not None:
                self.lib.pa_simple_free(opened)

        threading.Thread(target=connect, name="pulse-open", daemon=True).start()
        done.wait(max(0.0, deadline - time.monotonic()))
        with lock:
            if done.is_set():
                return handle, error
            abandoned = True
        logger.debug("Истёк дедлайн открытия устройства записи.")
        return None, 0


class PulseSimpleSource:
    """Источник записи через libpulse-simple; вызовы владельца последовательны."""

    def __init__(
        self,
        *,
        devices: Callable[[], list[AudioDevice]] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._devices = devices
        self._sleep = sleep
        self._pulse: _Pulse | None = None
        self._handle: ctypes.c_void_p | None = None
        self._buffer = (ctypes.c_char * CHUNK_BYTES)()
        self._device_name: str | None = None
        self._selected_device: AudioDevice | None = None
        self.device_label: str | None = None

    def open(self, device: str | None, *, deadline: _OpenDeadline | None = None) -> None:
        """Проверяет выбор до открытия и повторяет ограниченные по времени попытки."""
        self.close()
        devices = list_devices(deadline=deadline) if self._devices is None else self._devices()
        if deadline is not None:
            deadline.remaining(OPEN_TOTAL_DEADLINE_S)
        selected = resolve_device(device, devices)
        if selected is not None and selected.monitor:
            logger.info("Выбран источник записи: %s", selected.label)
        if self._pulse is None:
            self._pulse = _Pulse()
        error = 0
        for attempt in range(OPEN_RETRIES):
            timeout = OPEN_DEADLINE_S if deadline is None else deadline.remaining(OPEN_DEADLINE_S)
            handle, error = self._pulse.open(device, timeout=timeout)
            if handle is not None:
                self._handle = handle
                self._device_name = device
                self._selected_device = selected
                self.device_label = selected.label if selected is not None else None
                latency = self.latency_us()
                if latency is not None:
                    logger.debug("%d", latency)
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
        """Возвращает запрошенное имя; libpulse-simple не сообщает фактическое."""
        return self._device_name

    @property
    def selected_device(self) -> AudioDevice | None:
        """Возвращает проверенное устройство текущего открытия, None — выбор по умолчанию."""
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
