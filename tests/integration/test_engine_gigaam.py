"""Интеграционные проверки реальной GigaAM v3 e2e RNN-T (S3 §4.1, §5.3)."""

from __future__ import annotations

import os
import time
import wave
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from astra_voice.worker.engine import (
    LAYOUTS,
    CancelToken,
    Engine,
    LoadResult,
    ModelMissingError,
    active_sessions,
    make_engine,
)

if TYPE_CHECKING:
    import numpy as np
    import numpy.typing as npt

pytestmark = pytest.mark.engine

LAYOUT = "onnx-asr-gigaam-v3"
VARIANT = "gigaam-v3-e2e-rnnt"
THREADS = 2
REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module", autouse=True)
def runtime() -> None:
    # Ленивые импорты: сбор unit-тестов не требует рантайма и модели.
    pytest.importorskip("onnxruntime", reason="Для engine-тестов нужен onnxruntime")
    pytest.importorskip("onnx_asr", reason="Для engine-тестов нужен onnx-asr")
    pytest.importorskip("numpy", reason="Для чтения PCM в engine-тестах нужен numpy")


@pytest.fixture(scope="module")
def model_dir() -> Path:
    directory = Path(
        os.environ.get(
            "ASTRA_VOICE_TEST_MODEL_DIR", "~/.cache/astra-voice-spike/gigaam-v3/e2e_rnnt"
        )
    ).expanduser()
    if not directory.is_dir():
        pytest.skip(f"Каталог модели GigaAM отсутствует: {directory}")
    for name in LAYOUTS[LAYOUT][VARIANT].required:
        if not (directory / name).is_file():
            pytest.skip(f"Отсутствует обязательный файл модели GigaAM: {directory / name}")
    return directory


def _read_wav(name: str) -> npt.NDArray[np.float32]:
    import numpy as np

    path = REPO_ROOT / "data" / "test" / name
    if not path.is_file():
        path = Path("~/.cache/astra-voice-spike/wav").expanduser() / name
    if not path.is_file():
        pytest.skip(f"Тестовый WAV отсутствует в data/test и кеше: {path}")
    with wave.open(str(path), "rb") as wav:
        assert wav.getframerate() == 16000, f"Ожидается WAV 16000 Гц: {path}"
        assert wav.getnchannels() == 1, f"Ожидается моно WAV: {path}"
        assert wav.getsampwidth() == 2, f"Ожидается PCM int16: {path}"
        assert wav.getcomptype() == "NONE", f"Ожидается несжатый PCM: {path}"
        pcm = wav.readframes(wav.getnframes())
    audio = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
    audio /= 32768.0
    return audio


@pytest.fixture(scope="module")
def audio_6s() -> npt.NDArray[np.float32]:
    return _read_wav("test-ru-6s.wav")


@pytest.fixture(scope="module")
def audio_20s() -> npt.NDArray[np.float32]:
    return _read_wav("test-ru-20s.wav")


@pytest.fixture(scope="module")
def loaded_engine(model_dir: Path) -> Iterator[tuple[Engine, LoadResult]]:
    engine = make_engine(LAYOUT)
    try:
        loaded = engine.load(model_dir, LAYOUT, VARIANT, THREADS)
        yield engine, loaded
    finally:
        engine.unload()


def test_load(loaded_engine: tuple[Engine, LoadResult]) -> None:
    _, loaded = loaded_engine
    # S3 §4.1: около 667 мс при двух потоках; контракт допускает 10 с.
    assert loaded.load_ms < 10000
    assert loaded.sessions == 3  # S3-R4: только encoder, decoder и joint.
    assert loaded.engine_version
    assert "onnx-asr" in loaded.engine_version


def test_transcribe(
    loaded_engine: tuple[Engine, LoadResult], audio_6s: npt.NDArray[np.float32]
) -> None:
    engine, _ = loaded_engine
    result = engine.transcribe(audio_6s, CancelToken())
    assert "проверка" in result.text.casefold()
    assert result.cancelled is False
    assert result.infer_ms > 0


def test_cancel_and_transcribe_again(
    loaded_engine: tuple[Engine, LoadResult],
    audio_6s: npt.NDArray[np.float32],
    audio_20s: npt.NDArray[np.float32],
) -> None:
    engine, _ = loaded_engine
    cancel = CancelToken()
    with ThreadPoolExecutor(max_workers=1) as executor:
        started = time.perf_counter()
        future = executor.submit(engine.transcribe, audio_20s, cancel)
        try:
            time.sleep(0.150)
            cancel.cancel()
            result = future.result(timeout=1.0)
            elapsed_ms = (time.perf_counter() - started) * 1000
        finally:
            cancel.cancel()
    assert result.cancelled is True
    # Суммарное время, включая 150 мс до запроса отмены (S3 §5.3).
    assert elapsed_ms <= 1000, f"Возврат после отмены занял {elapsed_ms:.1f} мс"

    recovered = engine.transcribe(audio_6s, CancelToken())
    assert "проверка" in recovered.text.casefold()
    assert recovered.cancelled is False
    assert recovered.infer_ms > 0


def test_unknown_variant(model_dir: Path) -> None:
    engine = make_engine(LAYOUT)
    try:
        with pytest.raises(ValueError, match="Неизвестный вариант"):
            engine.load(model_dir, LAYOUT, "unknown", THREADS)
    finally:
        engine.unload()


def test_missing_model_directory(tmp_path: Path) -> None:
    engine = make_engine(LAYOUT)
    try:
        with pytest.raises(ModelMissingError):
            engine.load(tmp_path / "absent", LAYOUT, VARIANT, THREADS)
    finally:
        engine.unload()


# Последний тест использует собственный движок, не выгружая модульную фикстуру.
def test_unload_releases_sessions(model_dir: Path, audio_6s: npt.NDArray[np.float32]) -> None:
    baseline = active_sessions()
    engine = make_engine(LAYOUT)
    try:
        engine.load(model_dir, LAYOUT, VARIANT, THREADS)
        assert active_sessions() == baseline + 3
        # Проверяем также освобождение обёрток сессий, созданных распознаванием.
        result = engine.transcribe(audio_6s, CancelToken())
        assert "проверка" in result.text.casefold()
        engine.unload()
        assert active_sessions() == baseline
        engine.unload()
        assert active_sessions() == baseline
    finally:
        engine.unload()
