"""Автомат воркера, владение PCM и замеры памяти без транспорта и интерфейса.

Состояния idle, recording, processing и recording+processing отражают только
текущую запись и выполняющееся задание. Остановленные utterance хранятся отдельно:
``record.stop`` сохраняет PCM, но не запускает распознавание. ``recognize`` берёт
сохранённую запись; для совместимости он также может завершить текущую запись.
Очередь содержит максимум одно ожидающее задание сверх выполняющегося.

PCM хранится в array("f") при 16 кГц. Предел фразы по умолчанию — 120 секунд:
достижение предела останавливает запись и выдаёт ``record.limit``, лишние отсчёты
отбрасываются. WAV длиннее предела отклоняется с bad-audio до чтения PCM;
нормализация не создаёт промежуточного списка float. Движок получает ndarray
float32 без копирования PCM, через лениво импортируемый numpy.frombuffer.

Переход в idle ничего не удаляет. Буфер освобождается после result, cancelled,
record.cancel, model.unload или close (граница жизни поколения воркера).
При ошибке распознавания PCM снова ожидает запроса. Остановленный буфер хранится
300 секунд по умолчанию; просрочка проверяется при следующем сообщении по
инъектируемым монотонным часам. Просрочка и новый record.start с тем же id
удаляют старый остановленный буфер с записью в журнал без текста диктовки.

Отмена неизвестного id возвращает error с кодом bad-state. Отмена существующей
записываемой, остановленной, выполняемой или ожидающей utterance выдаёт cancelled.
Повторная отмена выдаёт тот же ответ: помним отменённые id без PCM до повторного
использования id, model.unload или close. Поздний результат отменённого задания
не выдаётся. Callback вызывается под блокировкой автомата, в том числе из
рабочего потока: он должен быстро передать событие транспорту.
"""

from __future__ import annotations

import importlib
import json
import logging
import math
import time
import wave
from array import array
from collections.abc import Callable, Iterable
from concurrent.futures import Executor, ThreadPoolExecutor
from dataclasses import dataclass
from enum import Enum
from importlib.metadata import PackageNotFoundError, version
from itertools import islice
from pathlib import Path
from threading import RLock
from types import ModuleType
from typing import TYPE_CHECKING, Any, Protocol, cast

from astra_voice.core.logging import RedactTextFilter
from astra_voice.core.paths import data_dir
from astra_voice.core.version import __version__
from astra_voice.worker.ipc import UNKNOWN_MESSAGE, FrameError, encode, error

if TYPE_CHECKING:
    from astra_voice.worker.audio import AudioCapture

logger = logging.getLogger(__name__)
logger.addFilter(RedactTextFilter())
Message = dict[str, Any]
SAMPLE_RATE = 16_000
LIMIT_S_DEFAULT = 120.0
STOPPED_TTL_S_DEFAULT = 300.0
_vad_unavailable_warned = False
_vad_availability_lock = RLock()


def join_segment_texts(parts: list[str]) -> str:
    """Склеивает фрагменты, убирая лишние пробелы и повтор знака на стыке."""
    text = ""
    for part in parts:
        part = " ".join(part.split())
        if text and text[-1] in ".!?…":
            while part.startswith(text[-1]):
                part = part[1:].lstrip()
        if not part:
            continue
        separator = " " if text and part[0] not in ".,!?;:…" else ""
        text += separator + part
    return text


def _available_vad() -> ModuleType | None:
    """Лениво проверяет VAD; учитывает его собственное предупреждение об отказе."""
    global _vad_unavailable_warned

    def warning_once(record: logging.LogRecord) -> bool:
        global _vad_unavailable_warned
        if record.levelno != logging.WARNING:
            return True
        with _vad_availability_lock:
            if _vad_unavailable_warned:
                return False
            _vad_unavailable_warned = True
            return True

    with _vad_availability_lock:
        vad_logger = logging.getLogger("astra_voice.worker.vad")
        # Только проверка доступности: предупреждения сегментации не фильтруем.
        vad_logger.addFilter(warning_once)
        try:
            module = importlib.import_module("astra_voice.worker.vad")
            if module.vad_available():
                return module
        except Exception:
            pass
        finally:
            vad_logger.removeFilter(warning_once)
        if not _vad_unavailable_warned:
            _vad_unavailable_warned = True
            logger.warning("Определение речи недоступно. Запись будет обработана целиком.")
    return None


class Cancellation(Protocol):
    """Структурный контракт токена; не требует наличия engine.py при проверке типов."""

    def cancel(self) -> None:
        """Запрашивает отмену."""
        ...

    @property
    def cancelled(self) -> bool:
        """Показывает, запрошена ли отмена."""
        ...


class LoadInfo(Protocol):
    """Используемые автоматом поля результата загрузки."""

    @property
    def load_ms(self) -> float: ...

    @property
    def engine_version(self) -> str: ...

    @property
    def sessions(self) -> int: ...


class Transcript(Protocol):
    """Используемые автоматом поля результата распознавания."""

    @property
    def text(self) -> str: ...

    @property
    def cancelled(self) -> bool: ...


class EngineBackend(Protocol):
    """Граница инъекции движка; ndarray создаётся только в ленивом конвертере."""

    def load(self, model_dir: Path, layout: str, variant: str, threads: int) -> LoadInfo: ...

    def transcribe(self, audio: Any, cancel: Cancellation) -> Transcript: ...

    def unload(self) -> None: ...


class State(Enum):
    """Четыре состояния записи и обработки."""

    idle = "idle"
    recording = "recording"
    processing = "processing"
    recording_processing = "recording+processing"


class AudioSource(Protocol):
    """Минимальный контракт источника до подключения звука в M3."""

    def close(self) -> None:
        """Освобождает устройство."""
        ...


def _make_engine(layout: str) -> EngineBackend:
    """Импортирует реализацию только при загрузке модели."""
    module = importlib.import_module("astra_voice.worker.engine")
    return cast(EngineBackend, module.make_engine(layout))


def _make_cancel() -> Cancellation:
    """Создаёт токен движка без импорта рантайма при импорте автомата."""
    module = importlib.import_module("astra_voice.worker.engine")
    return cast(Cancellation, module.CancelToken())


def _numpy_audio(samples: array[float]) -> Any:
    """Лениво превращает нормализованный моно PCM в float32 ndarray."""
    np = importlib.import_module("numpy")
    return np.frombuffer(samples, dtype=np.float32)


def _runtime() -> str:
    """Читает версию установленного рантайма без загрузки его библиотек."""
    try:
        return version("onnxruntime")
    except PackageNotFoundError:
        return "none"


@dataclass
class _Job:
    """Задание с собственным токеном и временем приёма запроса."""

    utterance_id: str
    cancel: Cancellation
    started: float
    path: Path | None = None
    notified: bool = False


class WorkerState:
    """Потокобезопасная логика; владелец завершает её методом ``close``.

    ``feed_audio`` принимает нормализованные отсчёты PCM 16 кГц, моно.
    ``audio_factory`` и ``cancel_factory`` позволяют тестировать без numpy и ORT.
    Переданный извне executor остаётся собственностью вызывающего кода.
    Callback-и capture направляются в on_samples и on_error этого автомата;
    события захвата передаются тому же on_event, что и события автомата.
    Идентичность модели сохраняется после unload: VmHWM относится ко всему процессу.
    """

    def __init__(
        self,
        *,
        engine_factory: Callable[[str], EngineBackend] = _make_engine,
        audio_source: AudioSource | None = None,
        capture: AudioCapture | None = None,
        executor: Executor | None = None,
        data_dir_factory: Callable[[], Path] = data_dir,
        on_event: Callable[[Message], None] = lambda msg: None,
        cancel_factory: Callable[[], Cancellation] = _make_cancel,
        audio_factory: Callable[[array[float]], Any] = _numpy_audio,
        status_path: Path = Path("/proc/self/status"),
        smaps_path: Path = Path("/proc/self/smaps_rollup"),
        measurements_path: Path | None = None,
        runtime: str | None = None,
        limit_s: float = LIMIT_S_DEFAULT,
        stopped_ttl_s: float = STOPPED_TTL_S_DEFAULT,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not math.isfinite(limit_s) or limit_s < 1 / SAMPLE_RATE:
            raise ValueError("Предел записи должен вмещать хотя бы один отсчёт.")
        if not math.isfinite(stopped_ttl_s) or stopped_ttl_s <= 0:
            raise ValueError("Срок хранения должен быть конечным положительным числом.")
        self.state = State.idle
        self.buffers: dict[str, array[float]] = {}
        self._limit_samples = int(limit_s * SAMPLE_RATE)
        self._stopped_ttl_s = stopped_ttl_s
        self._clock = clock
        self._stopped: dict[str, float] = {}
        self._cancelled: set[str] = set()
        self.min_ram_mb = 0
        self._engine_factory = engine_factory
        self._source = audio_source
        self._capture = capture
        self._device: str | None = None
        self._executor = executor if executor is not None else ThreadPoolExecutor(max_workers=1)
        self._owns_executor = executor is None
        self._data_dir = data_dir_factory
        self._on_event = on_event
        self._cancel_factory = cancel_factory
        self._audio_factory = audio_factory
        self._status_path = status_path
        self._smaps_path = smaps_path
        self._measurements_path = measurements_path
        self._runtime = _runtime() if runtime is None else runtime
        self._lock = RLock()
        self._engine_lock = RLock()
        self._engine: EngineBackend | None = None
        self._identity: tuple[str, str, int] | None = None
        self._loaded: Message | None = None
        self._sessions: int | None = None
        self._recording: str | None = None
        self._active: _Job | None = None
        self._pending: _Job | None = None
        self._unloading = False
        self._closed = False

    def set_capture(self, capture: AudioCapture) -> None:
        """Подключает захват с callback-ами этого автомата до начала записи."""
        with self._lock:
            if self._closed or self._recording is not None or self._capture is not None:
                raise RuntimeError("Захват можно подключить только к свободному автомату.")
            self._capture = capture

    def handle(self, msg: Message) -> list[Message]:
        """Принимает IPC сообщение и выполняет ожидания после снятия блокировки."""
        deferred: list[Callable[[], None]] = []
        with self._lock:
            replies = self._handle(msg, deferred)
        for action in deferred:
            action()
        return replies

    def _handle(self, msg: Message, deferred: list[Callable[[], None]]) -> list[Message]:
        """Меняет состояние под блокировкой; ожидание захвата откладывает в handle."""
        if self._closed:
            return [error("bad-state", "Воркер закрыт.")]
        self._expire_stopped()
        kind = msg["type"]
        if kind == "ping":
            return [{"type": "pong"}]
        if kind == "model.load":
            return self._load(msg)
        if kind == "model.unload":
            self._unload(deferred)
            return []
        if kind == "measure":
            return self._measure()
        if kind == "audio.close":
            if self._capture is not None:
                if self._recording is not None:
                    self._stop_recording(deferred)
                else:
                    self._stop_capture(deferred)
            if self._source is not None:
                deferred.append(self._source.close)
            return [{"type": "audio.closed"}]
        if kind == "transcribe.file":
            return self._recognize("file", Path(msg["path"]))
        uid = str(msg.get("utterance_id", ""))
        if kind == "record.start":
            if self._recording is not None or self._has_job(uid):
                return [error("bad-state", "Запись уже существует.")]
            if uid in self._stopped:
                self._stopped.pop(uid)
                logger.info("Остановленный буфер вытеснен новой записью: %s.", uid)
            self._cancelled.discard(uid)
            self.buffers[uid] = array("f")
            self._recording = uid
            self._update_state()
            if self._capture is not None:
                self._device = msg.get("device")
                self._capture.start(uid, self._device)
            return []
        if kind == "record.stop":
            if self._recording != uid:
                return [error("bad-state", "Нет такой активной записи.")]
            self._stop_recording(deferred)
            return []
        if kind == "record.cancel":
            return self._cancel(uid, deferred)
        if kind == "recognize":
            return self._recognize(uid, deferred=deferred)
        return [error(UNKNOWN_MESSAGE, "Неизвестная команда воркера.")]

    def feed_audio(self, utterance_id: str, samples: Iterable[float]) -> None:
        """Дописывает кадр только в текущую запись; поздние кадры отбрасываются."""
        with self._lock:
            if self._recording == utterance_id:
                buffer = self.buffers[utterance_id]
                buffer.extend(islice(samples, self._limit_samples - len(buffer)))
                if len(buffer) == self._limit_samples:
                    self._stop_recording()
                    self._emit({"type": "record.limit", "utterance_id": utterance_id})

    def on_samples(self, utterance_id: str, samples: array[float]) -> bool:
        """Принимает PCM захвата и останавливает чтение при завершении записи."""
        with self._lock:
            self.feed_audio(utterance_id, samples)
            return not self._closed and self._recording == utterance_id

    def on_error(self, utterance_id: str, code: str, message: str) -> None:
        """Удаляет неудавшуюся запись и передаёт ошибку захвата событием."""
        with self._lock:
            if self._closed or self._recording != utterance_id:
                return
            self.buffers.pop(utterance_id, None)
            self._stopped.pop(utterance_id, None)
            self._recording = None
            self._update_state()
            self._emit({**error(code, message), "utterance_id": utterance_id})

    def _stop_capture(self, deferred: list[Callable[[], None]] | None = None) -> None:
        """Под блокировкой только просит остановку; callback-и не ждут свой поток."""
        if self._capture is not None:
            self._capture.request_stop()
            if deferred is not None:
                deferred.append(self._capture.stop)

    def check_capture_watchdog(self) -> None:
        """Проверяет остановку после фоновых событий, не ожидая под RLock."""
        if self._capture is not None:
            self._capture.check_stop_watchdog()

    def _stop_recording(self, deferred: list[Callable[[], None]] | None = None) -> None:
        assert self._recording is not None
        self._stop_capture(deferred)
        self._stopped[self._recording] = self._clock()
        self._recording = None
        self._update_state()

    def _expire_stopped(self) -> None:
        now = self._clock()
        for uid, stopped_at in list(self._stopped.items()):
            if now - stopped_at >= self._stopped_ttl_s:
                self._stopped.pop(uid)
                self.buffers.pop(uid, None)
                logger.info("Истёк срок хранения остановленного буфера: %s.", uid)

    @staticmethod
    def audio_ready(device: str | None = None, changed: str | None = None) -> Message:
        """Формирует событие после успешного открытия устройства в M3."""
        ready: Message = {"type": "audio.ready"}
        if device is not None:
            ready["device"] = device
        if changed is not None:
            ready["changed"] = changed
        return ready

    def _has_job(self, uid: str) -> bool:
        return any(
            job is not None and job.utterance_id == uid for job in (self._active, self._pending)
        )

    def _update_state(self) -> None:
        if self._recording is not None:
            self.state = State.recording_processing if self._active else State.recording
        else:
            self.state = State.processing if self._active else State.idle

    def _load(self, msg: Message) -> list[Message]:
        identity = (str(msg["id"]), str(msg["revision"]), int(msg["threads"]))
        if self._identity is not None and identity != self._identity:
            return [error("restart-required", "Смена модели или потоков требует перезапуска.")]
        if self._unloading:
            return [error("busy", "Предыдущая выгрузка ещё не завершена.")]
        if self._engine is not None and self._loaded is not None:
            return [dict(self._loaded)]
        if self.state is not State.idle:
            return [error("bad-state", "Загрузка допустима только в простое.")]
        self._identity = identity
        self.min_ram_mb = int(msg["min_ram_mb"])
        engine: EngineBackend | None = None
        try:
            engine = self._engine_factory(msg["layout"])
            with self._engine_lock:
                result = engine.load(Path(msg["dir"]), msg["layout"], msg["variant"], identity[2])
        except Exception:
            if engine is not None:
                self._release_engine(engine)
            logger.warning("Не удалось загрузить движок.")
            return [error("engine-failed", "Не удалось загрузить движок.")]
        self._engine = engine
        self._sessions = result.sessions
        self._loaded = {
            "type": "model.loaded",
            "id": identity[0],
            "revision": identity[1],
            "variant": msg["variant"],
            "load_ms": result.load_ms,
            "engine_version": result.engine_version,
        }
        return [dict(self._loaded)]

    def _recognize(
        self,
        uid: str,
        path: Path | None = None,
        *,
        deferred: list[Callable[[], None]] | None = None,
    ) -> list[Message]:
        if self._engine is None:
            return [error("no-model", "Модель не загружена.")]
        if self._has_job(uid):
            return [error("bad-state", "Эта запись уже обрабатывается.")]
        if self._pending is not None:
            return [error("busy", "Очередь распознавания заполнена.")]
        if path is None and uid not in self.buffers:
            return [error("bad-state", "Буфер записи отсутствует.")]
        if path is not None and uid in self.buffers:
            return [error("bad-state", "Служебный идентификатор занят записью.")]
        job = _Job(uid, self._cancel_factory(), self._clock(), path)
        self._cancelled.discard(uid)
        self._stopped.pop(uid, None)
        if self._recording == uid:
            self._stop_capture(deferred)
            self._recording = None
        if self._active is not None:
            self._pending = job
        else:
            self._launch(job)
        self._update_state()
        return []

    def _launch(self, job: _Job) -> None:
        assert self._engine is not None
        self._active = job
        try:
            self._executor.submit(self._run, job, self._engine)
        except Exception:
            self._finish(job, error("engine-failed", "Не удалось запустить распознавание."))

    @staticmethod
    def _read_wav(
        path: Path, limit_samples: int = int(LIMIT_S_DEFAULT * SAMPLE_RATE)
    ) -> array[float]:
        """Читает PCM16 mono 16 кГц; превышение предела отвергает до чтения PCM."""
        import sys

        with wave.open(str(path), "rb") as wav:
            if (wav.getframerate(), wav.getsampwidth(), wav.getnchannels(), wav.getcomptype()) != (
                SAMPLE_RATE,
                2,
                1,
                "NONE",
            ):
                raise ValueError("Неподдерживаемый формат WAV.")
            if wav.getnframes() > limit_samples:
                raise ValueError("WAV превышает предел длительности фразы.")
            frames = wav.readframes(wav.getnframes())
            if len(frames) != wav.getnframes() * 2:
                raise ValueError("Неполный WAV.")
        samples = array("h")
        samples.frombytes(frames)
        if sys.byteorder != "little":
            samples.byteswap()
        return array("f", (sample / 32768.0 for sample in samples))

    def _run(self, job: _Job, engine: EngineBackend) -> None:
        msg: Message
        try:
            if job.path is not None:
                try:
                    samples = self._read_wav(job.path, self._limit_samples)
                except (OSError, EOFError, wave.Error, ValueError):
                    self._finish(job, error("bad-audio", "Ожидается WAV PCM16 mono 16 кГц."))
                    return
            else:
                with self._lock:
                    samples = self.buffers.get(job.utterance_id, array("f"))
            with self._engine_lock:
                if job.cancel.cancelled:
                    msg = {"type": "cancelled", "utterance_id": job.utterance_id}
                else:
                    audio = self._audio_factory(samples)
                    text, cancelled = self._transcribe_audio(audio, job, engine)
                    msg = (
                        {"type": "cancelled", "utterance_id": job.utterance_id}
                        if cancelled
                        else {
                            "type": "result",
                            "utterance_id": job.utterance_id,
                            "text": text,
                            "t_ms": int((self._clock() - job.started) * 1000),
                        }
                    )
                    # Лимиты и безопасные сообщения об ошибках принадлежат IPC.
                    try:
                        encode(msg)
                    except FrameError as exc:
                        msg = error(exc.code, exc.message)
        except Exception:
            logger.warning("Ошибка распознавания движка.")
            msg = error("engine-failed", "Ошибка распознавания движка.")
        self._finish(job, msg)

    def _transcribe_audio(self, audio: Any, job: _Job, engine: EngineBackend) -> tuple[str, bool]:
        """Обрезает хвост и распознаёт сегменты под замком движка из _run."""
        boundaries: list[tuple[int, int]] = []
        # Инъекция audio_factory может возвращать список вместо ndarray.
        if getattr(audio, "ndim", None) == 1 and getattr(audio, "dtype", None) == "float32":
            vad = _available_vad()
            if job.cancel.cancelled:
                return "", True
            if vad is not None:
                audio = vad.trim_trailing_silence(audio, SAMPLE_RATE, cancel=job.cancel)
                if job.cancel.cancelled:
                    return "", True
                if len(audio) / SAMPLE_RATE > 20.0:
                    boundaries = vad.segment(
                        audio, SAMPLE_RATE, max_window_s=24.0, cancel=job.cancel
                    )
        if job.cancel.cancelled:
            return "", True
        if len(boundaries) <= 1:
            result = engine.transcribe(audio, job.cancel)
            return result.text, job.cancel.cancelled or result.cancelled
        parts: list[str] = []
        for start, stop in boundaries:
            if job.cancel.cancelled:
                return "", True
            result = engine.transcribe(audio[start:stop], job.cancel)
            if job.cancel.cancelled or result.cancelled:
                return "", True
            parts.append(result.text)
        return join_segment_texts(parts), False

    def _finish(self, job: _Job, msg: Message) -> None:
        with self._lock:
            if self._active is not job:
                return
            if job.cancel.cancelled:
                msg = {"type": "cancelled", "utterance_id": job.utterance_id}
            if msg["type"] in {"result", "cancelled"}:
                self.buffers.pop(job.utterance_id, None)
                if msg["type"] == "cancelled":
                    self._cancelled.add(job.utterance_id)
            elif job.utterance_id in self.buffers:
                self._stopped[job.utterance_id] = self._clock()
            self._active = None
            discarded: list[Message] = []
            if msg.get("code") == "engine-failed":
                if self._pending is not None:
                    discarded = self._cancel(self._pending.utterance_id)
                if self._recording is not None:
                    self._stop_recording()
            elif self._pending is not None:
                pending, self._pending = self._pending, None
                self._launch(pending)
            self._update_state()
            if not job.notified:
                self._emit(msg)
            for event in discarded:
                self._emit(event)

    def _emit(self, msg: Message) -> None:
        try:
            self._on_event(msg)
        except Exception:
            logger.warning("Не удалось передать событие воркера.")

    def _cancel(self, uid: str, deferred: list[Callable[[], None]] | None = None) -> list[Message]:
        if uid not in self.buffers and not self._has_job(uid) and uid not in self._cancelled:
            return [error("bad-state", "Нет такой utterance для отмены.")]
        for job in (self._active, self._pending):
            if job is not None and job.utterance_id == uid:
                job.cancel.cancel()
                job.notified = True
                if job is self._pending:
                    self._pending = None
        if self._recording == uid:
            self._stop_capture(deferred)
            self._recording = None
        self.buffers.pop(uid, None)
        self._stopped.pop(uid, None)
        self._cancelled.add(uid)
        self._update_state()
        return [{"type": "cancelled", "utterance_id": uid}]

    def _release_engine(self, engine: EngineBackend) -> None:
        """Выгружает сессии после окончания использующего их вызова."""
        with self._engine_lock:
            try:
                engine.unload()
            except Exception:
                logger.warning("Ошибка выгрузки движка.")

    def _unload(self, deferred: list[Callable[[], None]] | None = None) -> None:
        self._stop_capture(deferred)
        running = self._active is not None
        for job in (self._active, self._pending):
            if job is not None:
                job.cancel.cancel()
        self._active = self._pending = None
        self._recording = None
        self.buffers.clear()
        self._stopped.clear()
        self._cancelled.clear()
        self._update_state()
        engine, self._engine = self._engine, None
        self._loaded = None
        self._sessions = None
        if engine is not None:
            if running:
                self._unloading = True
                self._executor.submit(self._deferred_unload, engine)
            else:
                self._release_engine(engine)

    def _deferred_unload(self, engine: EngineBackend) -> None:
        """Завершает выгрузку перед разрешением следующей загрузки."""
        self._release_engine(engine)
        with self._lock:
            self._unloading = False

    @staticmethod
    def _read_kb(path: Path, field: str) -> int:
        for line in path.read_text(encoding="utf-8").splitlines():
            name, _, value = line.partition(":")
            if name == field:
                return int(value.split()[0])
        raise ValueError("Показатель памяти отсутствует.")

    def _measure(self) -> list[Message]:
        try:
            vm_hwm = self._read_kb(self._status_path, "VmHWM")
            try:
                pss: int | None = self._read_kb(self._smaps_path, "Pss")
            except FileNotFoundError:
                pss = None
            msg: Message = {"type": "measured", "vm_hwm_kb": vm_hwm, "pss_kb": pss}
            if self._sessions is not None:
                msg["sessions"] = self._sessions
            path = self._measurements_path
            if path is None:
                path = self._data_dir() / "measurements.json"
            entries = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            if not isinstance(entries, dict):
                raise ValueError("Замеры должны быть JSON-объектом.")
            identity = self._identity or ("none", "none", 0)
            key = json.dumps([*identity, self._runtime, __version__], ensure_ascii=False)
            entries[key] = {
                "id": identity[0],
                "revision": identity[1],
                "threads": identity[2],
                "runtime": self._runtime,
                "build": __version__,
                "vm_hwm_kb": vm_hwm,
                "pss_kb": pss,
            }
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
            return [msg]
        except (OSError, ValueError, IndexError):
            return [error("measure-failed", "Не удалось прочитать или сохранить замер памяти.")]

    def close(self) -> None:
        """Отменяет задания, освобождает источник и завершает собственный executor."""
        deferred: list[Callable[[], None]] = []
        with self._lock:
            self._close(deferred)
        for action in deferred:
            action()

    def _close(self, deferred: list[Callable[[], None]]) -> None:
        """Закрывает автомат под блокировкой, откладывая ожидания потоков."""
        if self._closed:
            return
        self._closed = True
        self._unload(deferred)
        if self._source is not None:
            deferred.append(self._source.close)
        if self._owns_executor:
            deferred.append(lambda: self._executor.shutdown(wait=True))
