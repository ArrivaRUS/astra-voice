"""Интеграционные проверки локальной Silero VAD на синтетическом и настоящем PCM."""

from __future__ import annotations

import gc
import time
import wave
from collections.abc import Iterator
from itertools import pairwise
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from astra_voice.core import paths
from astra_voice.worker import vad
from astra_voice.worker.engine import active_sessions

if TYPE_CHECKING:
    import numpy as np
    import numpy.typing as npt

pytestmark = pytest.mark.engine

SAMPLE_RATE = 16000
REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module", autouse=True)
def runtime() -> None:
    """Сбор тестов не требует необязательных зависимостей движка."""
    pytest.importorskip("onnxruntime", reason="Для engine-тестов нужен onnxruntime")
    pytest.importorskip("numpy", reason="Для подготовки PCM нужен numpy")


@pytest.fixture(scope="module")
def model_dir() -> Path:
    """Использует только поставляемую локальную модель, без скачивания."""
    directory = REPO_ROOT / "data" / "vad"
    if not (directory / "silero_vad.onnx").is_file():
        pytest.skip(f"Модель Silero VAD отсутствует: {directory}")
    return directory


@pytest.fixture(autouse=True)
def isolated_vad(runtime: None, model_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Изолирует флаги и сессию, освобождает модель даже после ошибки теста."""
    monkeypatch.setattr(paths, "data_dir_static", lambda: model_dir.parent)
    monkeypatch.setattr(vad, "_session", None)
    monkeypatch.setattr(vad, "_load_attempted", False)
    monkeypatch.setattr(vad, "_unavailable_warned", False)
    monkeypatch.setattr(vad, "_inference_warned", False)
    monkeypatch.setattr(vad, "_nonfinite_warned", False)
    try:
        yield
    finally:
        vad.unload()
        gc.collect()


@pytest.fixture(scope="module")
def audio_20s(runtime: None) -> npt.NDArray[np.float32]:
    """Читает настоящий моно WAV 16 кГц и переводит int16 в float32."""
    import numpy as np

    path = REPO_ROOT / "data" / "test" / "test-ru-20s.wav"
    if not path.is_file():
        pytest.skip(f"Тестовый WAV отсутствует: {path}")
    with wave.open(str(path), "rb") as wav:
        assert wav.getframerate() == SAMPLE_RATE
        assert wav.getnchannels() == 1
        assert wav.getsampwidth() == 2
        assert wav.getcomptype() == "NONE"
        pcm = wav.readframes(wav.getnframes())
    audio = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
    audio /= 32768.0
    assert len(audio) == 20 * SAMPLE_RATE
    return audio


def _assert_coverage(segments: list[tuple[int, int]], length: int) -> None:
    """Отрезки полностью покрывают запись без пропусков и нахлёстов."""
    assert len(segments) > 1
    assert segments[0][0] == 0
    assert segments[-1][1] == length
    assert all(0 < end - start <= 24 * SAMPLE_RATE for start, end in segments)
    assert all(left[1] == right[0] for left, right in pairwise(segments))


def test_synthetic_speech_and_pauses() -> None:
    """Модель различает речь и паузы и выбирает раннюю длинную паузу."""
    import numpy as np

    # Гармонический источник с меняющимися формантами имитирует гласные.
    # Модулированный белый шум Silero почти целиком считает тишиной.
    t = np.arange(8 * SAMPLE_RATE, dtype=np.float64) / SAMPLE_RATE
    fundamental = 120 + 8 * np.sin(2 * np.pi * 2 * t)
    phase = 2 * np.pi * np.cumsum(fundamental) / SAMPLE_RATE
    formants = (
        600 + 200 * np.sin(2 * np.pi * 1.7 * t),
        1400 + 400 * np.sin(2 * np.pi * 1.3 * t),
        np.full_like(t, 2500),
    )
    speech = np.zeros_like(t)
    for harmonic in range(1, 65):
        frequency = harmonic * fundamental
        weight = sum(
            np.exp(-0.5 * ((frequency - center) / width) ** 2)
            for center, width in zip(formants, (80, 110, 160), strict=True)
        )
        speech += weight * np.sin(harmonic * phase) / harmonic
    speech *= 0.5 / np.max(np.abs(speech))
    speech *= 0.6 + 0.4 * np.sin(2 * np.pi * 3 * t)
    audio = np.concatenate(
        (speech, np.zeros(3 * SAMPLE_RATE), speech, np.zeros(SAMPLE_RATE), speech)
    ).astype(np.float32)
    speech_intervals = [(0, 8), (11, 19), (20, 28)]
    pauses = [(8, 11), (19, 20)]

    assert vad.vad_available()
    probabilities = vad._probabilities(audio)
    assert probabilities is not None
    assert len(probabilities) == (len(audio) + 511) // 512
    frame_times = np.arange(len(probabilities)) * 512 / SAMPLE_RATE
    scores = np.asarray(probabilities)
    # Проверяем каждый участок целиком, включая переходные кадры;
    # нулевая или постоянная вероятность не проходит оба порога.
    for start, end in speech_intervals:
        mean = float(scores[(frame_times >= start) & (frame_times < end)].mean())
        assert mean >= 0.7, f"Речь {start}–{end} с: средняя вероятность {mean:.3f} < 0.7"
    for start, end in pauses:
        mean = float(scores[(frame_times >= start) & (frame_times < end)].mean())
        assert mean <= 0.2, f"Пауза {start}–{end} с: средняя вероятность {mean:.3f} > 0.2"

    segments = vad.segment(audio)

    _assert_coverage(segments, len(audio))
    tolerance = int(0.3 * SAMPLE_RATE)
    first_boundary = segments[0][1]
    # Обе паузы внутри первого окна 24 с. Выбор последней дал бы 19.5 с.
    assert abs(first_boundary - int(9.5 * SAMPLE_RATE)) <= tolerance
    assert not 19 * SAMPLE_RATE - tolerance <= first_boundary <= 20 * SAMPLE_RATE + tolerance
    for _, boundary in segments[:-1]:
        assert any(
            start * SAMPLE_RATE - tolerance <= boundary <= end * SAMPLE_RATE + tolerance
            for start, end in pauses
        ), boundary / SAMPLE_RATE


def test_short_real_speech(audio_20s: npt.NDArray[np.float32]) -> None:
    """Настоящая запись ровно на пороге возвращается одним куском без загрузки."""
    assert vad.segment(audio_20s) == [(0, len(audio_20s))]
    assert vad._session is None
    assert vad._load_attempted is False


def test_long_real_speech(audio_20s: npt.NDArray[np.float32]) -> None:
    """Семь повторов настоящей речи сегментируются менее чем за две секунды."""
    import numpy as np

    audio = np.tile(audio_20s, 7)
    assert vad.vad_available()
    started = time.perf_counter()
    segments = vad.segment(audio)
    elapsed = time.perf_counter() - started
    print(f"Silero VAD: segment({len(audio) / SAMPLE_RATE:.0f} с) занял {elapsed:.3f} с")

    _assert_coverage(segments, len(audio))
    assert elapsed < 2.0, f"Сегментация заняла {elapsed:.3f} с при пределе 2 с"


def test_trim_appended_silence(audio_20s: npt.NDArray[np.float32]) -> None:
    """Удаляет хотя бы четыре секунды добавленной тишины, сохраняя полезную речь."""
    import numpy as np

    audio = np.concatenate((audio_20s, np.zeros(5 * SAMPLE_RATE, dtype=np.float32)))
    assert vad.vad_available()

    trimmed = vad.trim_trailing_silence(audio)

    assert len(trimmed) <= len(audio) - 4 * SAMPLE_RATE
    assert len(trimmed) >= len(audio_20s) - SAMPLE_RATE
    np.testing.assert_array_equal(trimmed, audio[: len(trimmed)])
    assert trimmed is not audio
    assert not np.shares_memory(trimmed, audio)
    snapshot = trimmed.copy()
    audio[:] = 0.75
    np.testing.assert_array_equal(trimmed, snapshot)


def test_unload_releases_session() -> None:
    """Загрузка добавляет ровно одну сессию, выгрузка возвращает исходное число."""
    gc.collect()
    baseline = active_sessions()
    assert vad.vad_available()
    assert active_sessions() == baseline + 1
    assert vad.vad_available()
    assert active_sessions() == baseline + 1

    vad.unload()
    gc.collect()

    assert active_sessions() == baseline
