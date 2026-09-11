"""Контракт движка, раскладки GigaAM v3 и сторожевой таймер (S3, M2).

Импорт модуля не требует установленных numpy, onnxruntime или onnx-asr.
"""

from __future__ import annotations

import gc
import logging
import sys
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Generic, Literal, Protocol, TypeVar

from astra_voice.worker.onnx_scan import scan_model_dir as scan_model_dir

if TYPE_CHECKING:
    import numpy as np
    import numpy.typing as npt

LOAD_TIMEOUT_S = 10.0
INFER_TIMEOUT_S = 120.0
CANCEL_TIMEOUT_S = 5.0
UNLOAD_TIMEOUT_S = 30.0
DEFAULT_THREADS = 2

T = TypeVar("T")


class EngineError(Exception):
    """Ошибка движка с машинным кодом и сообщением для пользователя."""

    code: str = "engine-error"

    def __init__(self, message: str) -> None:
        super().__init__(message)


class LoadTimeoutError(EngineError):
    """Загрузка не завершилась за отведённое время."""

    code = "load-timeout"


class ExtraFileError(EngineError):
    """В каталоге варианта обнаружены недопустимые файлы."""

    code = "extra-file"


class ExternalDataError(EngineError):
    """Внешние данные ONNX нарушают границы раскладки модели."""

    code = "external-data"

    def __init__(self, message: str) -> None:
        """Оставляет подробности сканера, включая внешние пути, только в DEBUG."""
        logging.getLogger(__name__).debug("Ошибка проверки данных модели: %s", message)
        super().__init__(
            "Не удалось проверить файлы модели: они повреждены или содержат недопустимые ссылки"
        )


class ModelMissingError(EngineError):
    """Каталог модели или обязательные файлы отсутствуют."""

    code = "model-missing"


class EngineUnavailableError(EngineError):
    """Рантайм недоступен или движок нельзя безопасно использовать."""

    code = "engine-unavailable"


class CancelToken:
    """Потокобезопасный одноразовый сигнал отмены распознавания."""

    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        """Запрашивает отмену; повторный вызов безопасен."""
        self._event.set()

    def wait(self, timeout: float | None = None) -> bool:
        """Ждёт отмены; возвращает True, если она запрошена."""
        return self._event.wait(timeout)

    @property
    def cancelled(self) -> bool:
        """Возвращает признак запрошенной отмены."""
        return self._event.is_set()


@dataclass(frozen=True)
class LoadResult:
    """Время загрузки, версия движка и число его сессий."""

    load_ms: float
    engine_version: str
    sessions: int


@dataclass(frozen=True)
class TranscribeResult:
    """Результат распознавания с длительностью и признаком отмены."""

    text: str
    infer_ms: float
    cancelled: bool


class Engine(Protocol):
    """Общий интерфейс загрузки, распознавания и выгрузки модели."""

    def load(self, model_dir: Path, layout: str, variant: str, threads: int) -> LoadResult: ...

    def transcribe(
        self,
        audio: "npt.NDArray[np.float32]",  # noqa: UP037 — строка для ленивых типов numpy
        cancel: CancelToken,
    ) -> TranscribeResult: ...

    def unload(self) -> None: ...


@dataclass(frozen=True)
class VariantSpec:
    """Имя модели onnx-asr, состав каталога и ожидаемое число сессий."""

    onnx_asr_name: str
    required: tuple[str, ...]
    optional: tuple[str, ...]
    sessions: int


# S3 §2.1–2.3: int8-веса и конфигурация обязательны, YAML не читается рантаймом.
LAYOUTS: dict[str, dict[str, VariantSpec]] = {
    "onnx-asr-gigaam-v3": {
        "gigaam-v3-e2e-rnnt": VariantSpec(
            onnx_asr_name="gigaam-v3-e2e-rnnt",
            required=(
                "v3_e2e_rnnt_encoder.int8.onnx",
                "v3_e2e_rnnt_decoder.int8.onnx",
                "v3_e2e_rnnt_joint.int8.onnx",
                "v3_e2e_rnnt_vocab.txt",
                "config.json",
            ),
            optional=("v3_e2e_rnnt.yaml",),
            sessions=3,
        ),
        "gigaam-v3-e2e-ctc": VariantSpec(
            onnx_asr_name="gigaam-v3-e2e-ctc",
            required=("v3_e2e_ctc.int8.onnx", "v3_e2e_ctc_vocab.txt", "config.json"),
            optional=("v3_e2e_ctc.yaml",),
            sessions=1,
        ),
        "gigaam-v3-rnnt": VariantSpec(
            onnx_asr_name="gigaam-v3-rnnt",
            required=(
                "v3_rnnt_encoder.int8.onnx",
                "v3_rnnt_decoder.int8.onnx",
                "v3_rnnt_joint.int8.onnx",
                "v3_vocab.txt",
                "config.json",
            ),
            optional=("v3_rnnt.yaml",),
            sessions=3,
        ),
    },
}


def check_layout(model_dir: Path, layout: str, variant: str) -> None:
    """Проверяет состав каталога ровно одного варианта модели (S3-R6)."""
    if layout not in LAYOUTS:
        raise ValueError(f"Неизвестная раскладка модели: {layout!r}")
    if variant not in LAYOUTS[layout]:
        raise ValueError(f"Неизвестный вариант {variant!r} для раскладки {layout!r}")
    spec = LAYOUTS[layout][variant]
    if not model_dir.is_dir():
        logging.getLogger(__name__).debug("Каталог модели отсутствует: %s", model_dir)
        raise ModelMissingError("Каталог модели отсутствует или не является каталогом")

    entries = list(model_dir.iterdir())
    allowed = set(spec.required + spec.optional)
    extra = sorted(
        entry.name
        for entry in entries
        if entry.name not in allowed or entry.is_symlink() or not entry.is_file()
    )
    if extra:
        logging.getLogger(__name__).debug("Недопустимые файлы в %s: %s", model_dir, extra)
        raise ExtraFileError(
            "В каталоге модели есть лишние файлы, подкаталоги или ссылки. "
            "Оставьте только файлы одного варианта модели"
        )
    missing = sorted(set(spec.required) - {entry.name for entry in entries})
    if missing:
        logging.getLogger(__name__).debug("Отсутствуют файлы в %s: %s", model_dir, missing)
        raise ModelMissingError(
            f"В каталоге модели отсутствуют обязательные файлы: {', '.join(missing)}"
        )


def layout_files(model_dir: Path, layout: str, variant: str) -> list[str]:
    """Возвращает присутствующие файлы раскладки после успешной check_layout."""
    spec = LAYOUTS[layout][variant]
    return [name for name in spec.required + spec.optional if (model_dir / name).is_file()]


class _PendingCall(Generic[T]):
    """Хранит поток и его результат до явного освобождения владельцем."""

    def __init__(self, fn: Callable[[], T]) -> None:
        result: Future[T] = Future()
        self._result: Future[T] | None = result

        def run() -> None:
            try:
                result.set_result(fn())
            except BaseException as exc:
                result.set_exception(exc)

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()

    def wait(self, timeout_s: float | None) -> bool:
        """Ждёт завершения потока; False означает истечение предела."""
        self._thread.join(timeout_s)
        return not self._thread.is_alive()

    def result(self) -> T:
        """Передаёт результат или исключение ещё не освобождённой операции."""
        assert self._result is not None
        return self._result.result()

    def discard_result(self) -> None:
        """Забирает поздний результат завершённого потока и снимает ссылки."""
        if self._result is not None:
            # Поздняя ошибка не должна мешать unload. Не поднимаем её повторно,
            # чтобы не добавлять ссылки на движок в traceback исключения.
            if self._result.exception() is None:
                self._result.result()
            # Объект ожидания может оставаться в traceback ошибки таймаута.
            self._result = None


def _run_with_deadline(
    fn: Callable[[], T],
    timeout_s: float,
    on_timeout: Callable[[_PendingCall[T]], EngineError | None],
) -> T:
    """Передаёт обработчику таймаута владение незавершённой операцией.

    Обработчик либо возвращает ошибку, либо дожидается завершения операции.
    """
    pending = _PendingCall(fn)
    if not pending.wait(timeout_s):
        error = on_timeout(pending)
        if error is not None:
            raise error
    return pending.result()


def run_with_deadline(
    fn: Callable[[], T], timeout_s: float, on_timeout: Callable[[], EngineError | None]
) -> T:
    """Выполняет функцию в демон-потоке и передаёт результат или исключение.

    После таймаута поток не останавливается: при зависании ORT под RLIMIT_AS
    процесс завершает супервизор (S3 §6). Если обработчик вернул None,
    ждём завершения функции после запрошенной обработчиком отмены.
    """

    def timeout(pending: _PendingCall[T]) -> EngineError | None:
        error = on_timeout()
        if error is None:
            pending.wait(None)
        return error

    return _run_with_deadline(fn, timeout_s, timeout)


def active_sessions() -> int:
    """Считает живые сессии через GC, не импортируя рантайм."""
    runtime = sys.modules.get("onnxruntime")
    if runtime is None:
        return 0
    session_type = getattr(runtime, "InferenceSession", None)
    if session_type is None:
        return 0
    return sum(isinstance(obj, session_type) for obj in gc.get_objects())


def _find_sessions(model: Any, session_type: type[T]) -> list[T]:
    """Находит сессии по типу в адаптере и препроцессорах, без повторов."""
    sessions: dict[int, T] = {}
    for holder in (model.asr, getattr(model, "resampler", None)):
        if holder is None:
            continue
        attributes = vars(holder)
        values = list(attributes.values())
        preprocessors = attributes.get("_preprocessors")
        if isinstance(preprocessors, dict):
            values.extend(preprocessors.values())
        for value in values:
            if isinstance(value, session_type):
                sessions[id(value)] = value
    return list(sessions.values())


_CancelMode = Literal["vendor-patch", "fallback-wrapper", "unavailable"]


class _RecognitionCancelled(Exception):
    """Внутренний сигнал отмены между вызовами ORT, без кода ошибки IPC."""


def _guard_run(
    original: Callable[..., Any], run_options: Any, cancel: CancelToken
) -> Callable[..., Any]:
    """Добавляет проверку отмены перед каждым коротким шагом декодера."""

    def run(names: Any, feed: Any, options: Any = None, **kwargs: Any) -> Any:
        if cancel.cancelled or run_options.terminate:
            raise _RecognitionCancelled
        # Ключевой аргумент run_options тоже поддерживается, но опции этого
        # распознавания обязательны: иначе сторож не прервёт текущий run.
        kwargs.pop("run_options", None)
        return original(names, feed, run_options, **kwargs)

    return run


class OnnxAsrEngine:
    """Офлайн-адаптер GigaAM v3 с отменой и освобождением сессий ORT."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        # Any ограничен границей необязательной зависимости onnx-asr.
        self._model: Any = None
        self._cancel_mode: _CancelMode = "unavailable"
        self._fallback_warned = False
        self._pending: _PendingCall[Any] | None = None
        self._poison_reason = ""

    def load(self, model_dir: Path, layout: str, variant: str, threads: int) -> LoadResult:
        """Загружает проверенную локальную модель на CPU за ограниченное время."""
        with self._lock:
            if self._pending is not None:
                raise EngineUnavailableError(self._poison_reason)
            self.unload()
            started = time.perf_counter()

            def timeout(pending: _PendingCall[tuple[Any, str]]) -> EngineError:
                self._pending = pending
                self._poison_reason = (
                    "Предыдущая загрузка не завершилась; вызовите unload(). "
                    "Супервизору необходимо перезапустить воркер."
                )
                return LoadTimeoutError(f"Загрузка модели превысила {LOAD_TIMEOUT_S:g} с")

            # Фоновая загрузка не меняет self: даже после таймаута её поздний
            # результат не воскресит выгруженную модель и не затрёт новую.
            model, version = _run_with_deadline(
                lambda: self._load_model(model_dir, layout, variant, threads),
                LOAD_TIMEOUT_S,
                timeout,
            )
            import onnxruntime as rt  # type: ignore[import-not-found]

            self._model = model
            found = len(_find_sessions(model, rt.InferenceSession))
            actual = active_sessions()
            expected = LAYOUTS[layout][variant].sessions
            if found != expected or actual != expected:
                logging.getLogger(__name__).error(
                    "Число сессий ORT не соответствует раскладке: ожидалось %d, "
                    "найдено по типу %d, active_sessions()=%d",
                    expected,
                    found,
                    actual,
                )
            if callable(getattr(model, "cancellable", None)):
                self._cancel_mode = "vendor-patch"
            elif found:
                self._cancel_mode = "fallback-wrapper"
            else:
                self._cancel_mode = "unavailable"
                logging.getLogger(__name__).error(
                    "Сессии ORT не найдены; отмена работать не будет, "
                    "распознавание нельзя прервать (cancel=unavailable)."
                )
            if self._cancel_mode == "fallback-wrapper" and not self._fallback_warned:
                logging.getLogger(__name__).warning(
                    "Отмена работает через обходной путь с поиском сессий по типу "
                    "onnx-asr (fallback-wrapper); в vendor-сборке должен применяться "
                    "патч cancellable()."
                )
                self._fallback_warned = True
            return LoadResult(
                (time.perf_counter() - started) * 1000,
                f"{version}; cancel={self._cancel_mode}",
                actual,
            )

    @staticmethod
    def _load_model(model_dir: Path, layout: str, variant: str, threads: int) -> tuple[Any, str]:
        """Создаёт адаптер без сетевых обращений и сессий ресемплера."""
        check_layout(model_dir, layout, variant)
        scan_model_dir(model_dir, layout_files(model_dir, layout, variant))
        try:
            import onnx_asr  # type: ignore[import-not-found]
            import onnxruntime as rt
            from onnx_asr.loader import Manager  # type: ignore[import-not-found]
            from onnx_asr.preprocessors.resampler import (  # type: ignore[import-not-found]
                Resampler,
            )
            from onnx_asr.utils import ModelLoadingError  # type: ignore[import-not-found]
        except ImportError as exc:
            raise EngineUnavailableError("Рантайм onnxruntime или onnx-asr недоступен") from exc

        class Pcm16Resampler(Resampler):
            """Пропускает только PCM 16 кГц, не создавая InferenceSession."""

            def __init__(self, sample_rate: int) -> None:
                if sample_rate != 16000:
                    raise ValueError("Движок поддерживает только модели с частотой 16000 Гц")
                self._target_sample_rate = sample_rate
                self._preprocessors: dict[int, Any] = {}

            def __call__(
                self,
                waveforms: npt.NDArray[np.float32],
                waveforms_lens: npt.NDArray[np.int64],
                sample_rate: int,
            ) -> tuple[npt.NDArray[np.float32], npt.NDArray[np.int64]]:
                """Отклоняет другую частоту вместо молчаливого искажения аудио."""
                if sample_rate != 16000:
                    raise ValueError(f"Ожидается аудио 16000 Гц, получено {sample_rate} Гц")
                return waveforms, waveforms_lens

        class Pcm16Manager(Manager):
            """Исключает семь неиспользуемых сессий ресемплера (S3-R4)."""

            def _create_resampler(self, sample_rate: int) -> Pcm16Resampler:
                return Pcm16Resampler(sample_rate)

        try:
            sess_options = rt.SessionOptions()
            sess_options.intra_op_num_threads = threads if threads > 0 else DEFAULT_THREADS
            sess_options.inter_op_num_threads = 1
            sess_options.add_session_config_entry("session.intra_op.allow_spinning", "0")
            # load_model не принимает offline. Manager передаёт offline в Resolver,
            # который при local_dir и offline=True не вызывает huggingface_hub.
            # CPU задан в конфигурации Manager и явно в конфигурации модели;
            # numpy-препроцессор не создаёт дополнительных сессий.
            manager = Pcm16Manager(
                sess_options=sess_options,
                providers=["CPUExecutionProvider"],
                preprocessor_config={"use_numpy_preprocessors": True, "max_concurrent_workers": 1},
            )
            model = manager.create_asr(
                LAYOUTS[layout][variant].onnx_asr_name,
                model_dir,
                quantization="int8",
                offline=True,
                config={"sess_options": sess_options, "providers": ["CPUExecutionProvider"]},
            )
            return model, f"onnx-asr {onnx_asr.__version__}/onnxruntime {rt.__version__}"
        except (EngineError, _RecognitionCancelled):
            raise
        except Exception as exc:
            # Ошибки конфигурации onnx-asr тоже наследуют ValueError,
            # но не относятся к нашему контракту аргументов.
            if isinstance(exc, ValueError) and not isinstance(exc, ModelLoadingError):
                raise
            logging.getLogger(__name__).debug(
                "Ошибка создания адаптера распознавания", exc_info=True
            )
            raise ModelMissingError(
                "Не удалось загрузить модель распознавания: "
                "файлы модели повреждены или не подходят движку"
            ) from exc

    def transcribe(
        self,
        audio: "npt.NDArray[np.float32]",  # noqa: UP037 — строка для ленивых типов numpy
        cancel: CancelToken,
    ) -> TranscribeResult:
        """Распознаёт моно float32 16 кГц; таймаут завершает через отмену."""
        with self._lock:
            if cancel.cancelled:
                return TranscribeResult("", 0.0, True)
            if self._pending is not None:
                raise EngineUnavailableError(self._poison_reason)
            if self._model is None:
                raise EngineUnavailableError("Модель не загружена")
            try:
                import numpy as np
            except ImportError as exc:
                raise EngineUnavailableError("Рантайм numpy недоступен") from exc
            if not isinstance(audio, np.ndarray) or audio.dtype != np.float32 or audio.ndim != 1:
                raise ValueError("Ожидается одномерный numpy.ndarray с dtype float32")
            started = time.perf_counter()

            def timeout(pending: _PendingCall[tuple[str, bool]]) -> EngineError | None:
                cancel.cancel()
                logging.getLogger(__name__).warning(
                    "Распознавание превысило %g с; запрошена отмена", INFER_TIMEOUT_S
                )
                if pending.wait(CANCEL_TIMEOUT_S):
                    return None
                self._pending = pending
                self._poison_reason = (
                    "Распознавание не отвечает на отмену; необходим перезапуск воркера "
                    "супервизором. Для освобождения модели вызовите unload()."
                )
                return EngineUnavailableError(self._poison_reason)

            text, cancelled = _run_with_deadline(
                lambda: self._recognize(audio, cancel), INFER_TIMEOUT_S, timeout
            )
            return TranscribeResult(text, (time.perf_counter() - started) * 1000, cancelled)

    def _recognize(self, audio: npt.NDArray[np.float32], cancel: CancelToken) -> tuple[str, bool]:
        """Возвращает текст и факт прерывания, восстанавливая методы сессий."""
        try:
            try:
                import onnxruntime as rt
                from onnxruntime.capi.onnxruntime_pybind11_state import (  # type: ignore[import-not-found]
                    Fail,
                )
            except ImportError as exc:
                raise EngineUnavailableError("Рантайм onnxruntime недоступен") from exc

            model = self._model
            sessions = _find_sessions(model, rt.InferenceSession)
            # Сохраняем именно атрибут экземпляра: bound method, записанный обратно
            # вместо удаления обёртки, образовал бы цикл session -> run -> session.
            originals = [(session, vars(session).get("run")) for session in sessions]
            context = (
                model.cancellable()
                if self._cancel_mode == "vendor-patch"
                else nullcontext(rt.RunOptions())
            )
            terminate_requested = threading.Event()
            try:
                with context as run_options:
                    for session in sessions:
                        session.run = _guard_run(session.run, run_options, cancel)
                    stopped = threading.Event()

                    def watch() -> None:
                        while not stopped.is_set():
                            # Отмена будит сразу; завершение проверяем раз в 50 мс.
                            if cancel.wait(timeout=0.05):
                                run_options.terminate = True
                                terminate_requested.set()
                                return

                    watcher = None
                    if self._cancel_mode != "unavailable":
                        watcher = threading.Thread(target=watch, daemon=True)
                        watcher.start()
                    try:
                        if cancel.cancelled:
                            return "", True
                        result = model.recognize(audio, sample_rate=16000)
                        if not isinstance(result, str):
                            raise EngineError(
                                "Адаптер распознавания вернул результат вместо строки"
                            )
                        return result, False
                    finally:
                        stopped.set()
                        if watcher is not None:
                            watcher.join()
            except _RecognitionCancelled:
                cancel.cancel()
                return "", True
            except Fail:
                # Сторож уже завершён: этот признак сохраняется, даже если
                # vendor-контекст сбросил terminate при выходе. Текст Fail не важен.
                if not terminate_requested.is_set():
                    raise
                cancel.cancel()
                return "", True
            finally:
                # Выполняется после выхода из vendor-контекста, который тоже
                # восстанавливает session.run. Снимаем обёртки обоих путей.
                for session, original in originals:
                    if original is None:
                        vars(session).pop("run", None)
                    else:
                        session.run = original
        except (EngineError, ValueError, _RecognitionCancelled):
            raise
        except Exception as exc:
            logging.getLogger(__name__).debug("Ошибка рантайма распознавания", exc_info=True)
            raise EngineUnavailableError(
                "Распознавание не выполнено: ошибка рантайма распознавания"
            ) from exc

    def unload(self) -> None:
        """Ждёт позднюю операцию с пределом и освобождает модель; повторяемо."""
        with self._lock:
            if self._pending is not None:
                if not self._pending.wait(UNLOAD_TIMEOUT_S):
                    logging.getLogger(__name__).error(
                        "Фоновая операция не завершилась за %g с при unload(); "
                        "сессии остались у осиротевшего потока, "
                        "процесс должен перезапустить супервизор",
                        UNLOAD_TIMEOUT_S,
                    )
                    return
                self._pending.discard_result()
            self._model = None
            self._cancel_mode = "unavailable"
            gc.collect()
            self._pending = None
            self._poison_reason = ""


def make_engine(layout: str) -> Engine:
    """Создаёт адаптер для поддерживаемой раскладки."""
    if layout == "onnx-asr-gigaam-v3":
        return OnnxAsrEngine()
    raise ValueError(f"Неизвестная раскладка модели: {layout!r}")
