"""Определение речи и разбиение записей локальной моделью Silero VAD.

Импорт модуля не требует numpy и onnxruntime. При недоступности модели
запись передаётся дальше целиком, без обрезки и разбиения.
"""

from __future__ import annotations

import logging as _logging
import math as _math
import threading as _threading
from typing import TYPE_CHECKING
from typing import Any as _Any

from astra_voice.core import paths as _paths
from astra_voice.worker.onnx_scan import scan_model_dir as _scan_model_dir

if TYPE_CHECKING:
    import numpy as np
    import numpy.typing as npt

    from astra_voice.worker.engine import CancelToken

__all__ = [
    "VADError",
    "VADUnavailableError",
    "segment",
    "trim_trailing_silence",
    "unload",
    "vad_available",
]

_SAMPLE_RATE = 16000
_FRAME = 512
_CONTEXT = 64
_MIN_SEGMENT = 2 * _SAMPLE_RATE
_SHORT_RECORDING = 20 * _SAMPLE_RATE
_TAIL = int(0.2 * _SAMPLE_RATE)
_SPEECH_THRESHOLD = 0.5  # Поиск последнего речевого кадра при обрезке хвоста.
_PAUSE_THRESHOLD = 0.35  # Независимый порог поиска пауз при разбиении записи.
_MAX_PCM_AMPLITUDE = 10.0  # Десятикратный запас от [-1, 1]; настоящая диктовка столько не даёт.
_logger = _logging.getLogger(__name__)
_lock = _threading.Lock()
# Any ограничен границей необязательной зависимости onnxruntime.
_session: _Any = None
_load_attempted = False
_unavailable_warned = False
_inference_warned = False
_nonfinite_warned = False


class VADError(Exception):
    """Ошибка определения речи с машинным кодом."""

    code: str = "vad-error"


class VADUnavailableError(VADError):
    """Локальная модель отсутствует или не может обработать запись."""

    code = "vad-unavailable"


def _mark_unavailable() -> None:
    """Отключает модель и предупреждает один раз; вызывается под замком."""
    global _session, _load_attempted, _unavailable_warned
    _session = None
    _load_attempted = True
    _logger.debug("Определение речи недоступно", exc_info=True)
    if not _unavailable_warned:
        _unavailable_warned = True
        _logger.warning("Не удалось включить определение речи. Запись будет обработана целиком.")


def _load_session() -> bool:
    """Лениво загружает проверенную модель; вызывается только под замком."""
    global _session, _load_attempted
    if _load_attempted:
        return _session is not None
    _load_attempted = True
    try:
        model_dir = _paths.data_dir_static() / "vad"
        model_path = model_dir / "silero_vad.onnx"
        if not model_path.is_file():
            raise VADUnavailableError("Модель определения речи отсутствует")
        # У7: проверка обязательна до создания любой сессии.
        _scan_model_dir(model_dir, ["silero_vad.onnx"])
        import numpy as np
        import onnxruntime as rt  # type: ignore[import-not-found]

        # Проверяем и доступность numpy, даже если пока нужен лишь статус.
        np.zeros((), dtype=np.float32)
        options = rt.SessionOptions()
        options.intra_op_num_threads = 2
        options.inter_op_num_threads = 1
        options.add_session_config_entry("session.intra_op.allow_spinning", "0")
        _session = rt.InferenceSession(
            str(model_path), sess_options=options, providers=["CPUExecutionProvider"]
        )
    except Exception:
        _mark_unavailable()
    return _session is not None


def vad_available() -> bool:
    """Возвращает доступность модели, при необходимости загружая её."""
    with _lock:
        return _load_session()


def unload() -> None:
    """Освобождает сессию и разрешает новую загрузку; повторный вызов безопасен."""
    global _session, _load_attempted
    with _lock:
        _session = None
        _load_attempted = False
        # Флаги предупреждений сохраняются на всё время жизни процесса.


def _validate_audio(audio: object, sample_rate: int) -> bool:
    """Проверяет вход; False означает недоступность зависимости или испорченные отсчёты."""
    global _nonfinite_warned
    if sample_rate != _SAMPLE_RATE:
        raise ValueError("Поддерживается только частота записи 16000 Гц")
    try:
        import numpy as np
    except Exception:
        with _lock:
            _mark_unavailable()
        return False
    if not isinstance(audio, np.ndarray) or audio.ndim != 1 or audio.dtype != np.float32:
        raise ValueError("Ожидается одномерный numpy.ndarray с dtype float32")
    peak = float(np.abs(audio).max(initial=0.0))
    if not _math.isfinite(peak) or peak > _MAX_PCM_AMPLITUDE:
        _logger.debug(
            "В записи обнаружены некорректные отсчёты: максимум модуля=%s, "
            "допустимый предел=%s (NaN и inf недопустимы)",
            peak,
            _MAX_PCM_AMPLITUDE,
        )
        with _lock:
            if not _nonfinite_warned:
                _nonfinite_warned = True
                _logger.warning(
                    "В записи обнаружены некорректные значения. Она будет обработана целиком."
                )
        return False
    return True


def _probabilities(
    audio: "npt.NDArray[np.float32]",  # noqa: UP037 — строка для ленивых типов numpy
    cancel: CancelToken | None = None,
) -> list[float] | None:
    """Считает вероятности с контекстом и состоянием, общими только внутри записи."""
    global _inference_warned
    if cancel is not None and cancel.cancelled:
        return None
    with _lock:
        if not _load_session():
            return None
        session = _session
    # Локальная ссылка сохраняет сессию живой при параллельном unload().
    try:
        import numpy as np

        state = np.zeros((2, 1, 128), dtype=np.float32)
        model_input = np.zeros((1, _CONTEXT + _FRAME), dtype=np.float32)
        sr = np.array(_SAMPLE_RATE, dtype=np.int64)
        probabilities: list[float] = []
        for index, start in enumerate(range(0, len(audio), _FRAME)):
            if index % 16 == 0 and cancel is not None and cancel.cancelled:
                return None
            frame = audio[start : start + _FRAME]
            model_input[:, _CONTEXT:] = 0
            model_input[0, _CONTEXT : _CONTEXT + len(frame)] = frame
            output, state = session.run(
                ["output", "stateN"], {"input": model_input, "state": state, "sr": sr}
            )
            if (
                not isinstance(output, np.ndarray)
                or output.shape != (1, 1)
                or not isinstance(state, np.ndarray)
                or state.shape != (2, 1, 128)
                or state.dtype != np.float32
                or not np.isfinite(state).all()
            ):
                raise VADError("Модель вернула некорректный результат")
            probability = float(output[0, 0])
            if not _math.isfinite(probability) or not 0.0 <= probability <= 1.0:
                raise VADError("Модель вернула некорректную вероятность")
            probabilities.append(probability)
            model_input[:, :_CONTEXT] = model_input[:, -_CONTEXT:]
        if cancel is not None and cancel.cancelled:
            return None
        return probabilities
    except Exception:
        _logger.debug("Не удалось выполнить определение речи для записи", exc_info=True)
        with _lock:
            if not _inference_warned:
                _inference_warned = True
                _logger.warning(
                    "Не удалось разобрать запись по паузам; она будет обработана целиком."
                )
        return None


def _pauses(probabilities: list[float], n: int) -> list[tuple[int, int]]:
    """Находит максимальные серии кадров с вероятностью речи ниже порога паузы."""
    pauses: list[tuple[int, int]] = []
    start: int | None = None
    for index, probability in enumerate(probabilities):
        if probability < _PAUSE_THRESHOLD:
            if start is None:
                start = index * _FRAME
        elif start is not None:
            pauses.append((start, min(index * _FRAME, n)))
            start = None
    if start is not None:
        pauses.append((start, n))
    return pauses


def segment(
    audio: "npt.NDArray[np.float32]",  # noqa: UP037 — строка для ленивых типов numpy
    sample_rate: int = 16000,
    max_window_s: float = 24.0,
    cancel: CancelToken | None = None,
) -> list[tuple[int, int]]:
    """Делит длинную запись по паузам, сохраняя все отсчёты ровно один раз.

    Окно должно быть конечным и не короче двух секунд. Записи до 20 секунд
    и записи без доступной модели возвращаются целиком независимо от окна.
    Записи не длиннее окна возвращаются целиком без обращения к модели.
    Последний остаток может быть короче двух секунд.
    """
    valid = _validate_audio(audio, sample_rate)
    if not _math.isfinite(max_window_s) or max_window_s < 2.0:
        raise ValueError("Максимальная длина отрезка должна быть конечной и не меньше 2 секунд")
    max_window = int(max_window_s * sample_rate)
    n = len(audio)
    if n == 0:
        return []
    if not valid or n <= max(_SHORT_RECORDING, max_window):
        return [(0, n)]
    probabilities = _probabilities(audio, cancel)
    if probabilities is None:
        return [(0, n)]
    pauses = _pauses(probabilities, n)
    segments: list[tuple[int, int]] = []
    cursor = 0
    warned = False
    while n - cursor > max_window:
        lower = cursor + _MIN_SEGMENT
        upper = cursor + max_window
        candidates = [
            (end - start, (start + end) // 2)
            for start, end in pauses
            if 2 * lower <= start + end <= 2 * upper
        ]
        if candidates:
            _, boundary = max(candidates)
        else:
            first_frame = (lower + _FRAME - 1) // _FRAME
            last_frame = upper // _FRAME
            if first_frame <= last_frame:
                index = min(range(first_frame, last_frame + 1), key=probabilities.__getitem__)
                boundary = index * _FRAME
            else:
                # В очень узком окне может не оказаться ни одной границы кадра.
                boundary = upper
            if not warned:
                _logger.warning("Запись пришлось разделить посередине речи")
                warned = True
        # Защита прогресса и предела длины, включая округление до отсчётов.
        boundary = max(cursor + 1, min(boundary, upper))
        segments.append((cursor, boundary))
        cursor = boundary
    segments.append((cursor, n))
    return segments


def trim_trailing_silence(
    audio: "npt.NDArray[np.float32]",  # noqa: UP037 — строка для ленивых типов numpy
    sample_rate: int = 16000,
    cancel: CancelToken | None = None,
) -> "npt.NDArray[np.float32]":  # noqa: UP037 — строка для ленивых типов numpy
    """Убирает тишину после последнего речевого кадра с запасом 200 мс.

    Результат всегда отдельный массив и не зависит от исходного буфера.
    Без модели или без найденной речи возвращает копию всей записи.
    """
    if not _validate_audio(audio, sample_rate) or len(audio) == 0:
        return audio.copy()
    probabilities = _probabilities(audio, cancel)
    if probabilities is None:
        return audio.copy()
    for index in range(len(probabilities) - 1, -1, -1):
        if probabilities[index] >= _SPEECH_THRESHOLD:
            end = min((index + 1) * _FRAME + _TAIL, len(audio))
            return audio[:end].copy()
    return audio.copy()
