"""Живая T-one на 8 кГц: захват 16 кГц понижает сам движок (M6 блок 4).

T-one — единственная запись каталога, у которой `onnx-asr` сообщает частоту
8000 Гц. Захват остаётся 16 кГц, децимацию 2:1 делает `worker/resample`.
Модель в репозиторий не кладётся: укажите каталог ревизии переменной
`ASTRA_VOICE_TEST_MODEL_DIR_TONE` либо положите её в
`~/.cache/astra-voice-spike/t-one`; без неё тест пропускается.
"""

from __future__ import annotations

import os
import wave
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from astra_voice.worker.engine import (
    LAYOUTS,
    CancelToken,
    Engine,
    LoadResult,
    make_engine,
)

if TYPE_CHECKING:
    import numpy as np
    import numpy.typing as npt

pytestmark = pytest.mark.engine

LAYOUT = "onnx-asr-t-one"
VARIANT = "t-one-ctc"
THREADS = 2
REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MODEL_DIR = "~/.cache/astra-voice-spike/t-one"


@pytest.fixture(scope="module", autouse=True)
def runtime() -> None:
    pytest.importorskip("onnxruntime", reason="Для engine-тестов нужен onnxruntime")
    pytest.importorskip("onnx_asr", reason="Для engine-тестов нужен onnx-asr")
    pytest.importorskip("numpy", reason="Для чтения PCM в engine-тестах нужен numpy")


@pytest.fixture(scope="module")
def model_dir() -> Path:
    directory = Path(
        os.environ.get("ASTRA_VOICE_TEST_MODEL_DIR_TONE", DEFAULT_MODEL_DIR)
    ).expanduser()
    if not directory.is_dir():
        pytest.skip(
            "Каталог модели T-one отсутствует; скачивание из теста запрещено. "
            f"Положите ревизию в {directory} или задайте ASTRA_VOICE_TEST_MODEL_DIR_TONE."
        )
    for name in LAYOUTS[LAYOUT][VARIANT].required:
        if not (directory / name).is_file():
            pytest.skip(f"Отсутствует обязательный файл модели T-one: {directory / name}")
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
        pcm = wav.readframes(wav.getnframes())
    audio = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
    audio /= 32768.0
    return audio


@pytest.fixture(scope="module")
def audio_6s() -> npt.NDArray[np.float32]:
    return _read_wav("test-ru-6s.wav")


@pytest.fixture(scope="module")
def loaded_engine(model_dir: Path) -> Iterator[tuple[Engine, LoadResult]]:
    engine = make_engine(LAYOUT)
    try:
        yield engine, engine.load(model_dir, LAYOUT, VARIANT, THREADS)
    finally:
        engine.unload()


def test_model_reports_eight_kilohertz(model_dir: Path) -> None:
    """Проверяем допущение: именно из-за 8000 Гц нужна децимация."""
    from onnx_asr.models.tone import TOneCtc  # type: ignore[import-not-found]

    assert TOneCtc._get_sample_rate() == 8000


def test_load_creates_no_resampler_sessions(loaded_engine: tuple[Engine, LoadResult]) -> None:
    """Сессии ресемплера onnx-asr не поднимаются: частоту понижает numpy (S3-R4)."""
    _, loaded = loaded_engine

    assert loaded.sessions == LAYOUTS[LAYOUT][VARIANT].sessions
    assert loaded.load_ms < 10000
    assert "onnx-asr" in loaded.engine_version


def test_transcribe_sixteen_kilohertz_capture(
    loaded_engine: tuple[Engine, LoadResult], audio_6s: npt.NDArray[np.float32]
) -> None:
    """Движок получает захват 16 кГц и сам отдаёт модели 8 кГц."""
    engine, _ = loaded_engine

    result = engine.transcribe(audio_6s, CancelToken())

    assert result.cancelled is False
    assert result.infer_ms > 0
    assert "проверка" in result.text.casefold(), f"text={result.text!r}"


def test_cancel_still_works(
    loaded_engine: tuple[Engine, LoadResult], audio_6s: npt.NDArray[np.float32]
) -> None:
    """Понижение частоты не ломает отмену и тайминги."""
    engine, _ = loaded_engine
    token = CancelToken()
    token.cancel()

    result = engine.transcribe(audio_6s, token)

    assert result.cancelled is True
    assert result.text == ""
    assert engine.transcribe(audio_6s, CancelToken()).cancelled is False
