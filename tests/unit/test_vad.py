"""Проверки контракта VAD и разбиения без модели и onnxruntime."""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import Callable
from itertools import pairwise
from pathlib import Path

import numpy as np
import numpy.typing as npt
import pytest

from astra_voice.core import paths
from astra_voice.security.verify import sha256_file
from astra_voice.worker import vad
from astra_voice.worker.engine import CancelToken

pytestmark = pytest.mark.unit

SAMPLE_RATE = 16000
FRAME = 512


@pytest.fixture(autouse=True)
def isolated_vad(monkeypatch: pytest.MonkeyPatch) -> None:
    """Восстанавливает сессию и флаги после теста, запрещает импорт рантайма."""
    monkeypatch.setattr(vad, "_session", None)
    monkeypatch.setattr(vad, "_load_attempted", False)
    monkeypatch.setattr(vad, "_unavailable_warned", False)
    monkeypatch.setattr(vad, "_inference_warned", False)
    monkeypatch.setattr(vad, "_nonfinite_warned", False)
    monkeypatch.setitem(sys.modules, "onnxruntime", None)


class FakeSession:
    """Выполняет настоящий цикл кадров VAD с управляемым результатом run."""

    def __init__(
        self,
        probabilities: list[float],
        *,
        fail_every: int | None = None,
        cancel: CancelToken | None = None,
    ) -> None:
        self.probabilities = probabilities
        self.fail_every = fail_every
        self.cancel = cancel
        self.calls = 0

    def run(
        self, names: list[str], inputs: dict[str, npt.NDArray[np.generic]]
    ) -> tuple[npt.NDArray[np.float32], npt.NDArray[np.float32]]:
        self.calls += 1
        if self.fail_every is not None and self.calls % self.fail_every == 0:
            raise RuntimeError("Ошибка инференса на пятом кадре")
        if self.cancel is not None and self.calls == 5:
            self.cancel.cancel()
        probability = self.probabilities[(self.calls - 1) % len(self.probabilities)]
        return (
            np.array([[probability]], dtype=np.float32),
            np.zeros((2, 1, 128), dtype=np.float32),
        )


def _install_session(monkeypatch: pytest.MonkeyPatch, session: FakeSession) -> None:
    monkeypatch.setattr(vad, "_session", session)
    monkeypatch.setattr(vad, "_load_attempted", True)


def _assert_coverage(segments: list[tuple[int, int]], length: int, window: int) -> None:
    """Каждый отсчёт принадлежит ровно одному непустому отрезку в пределах окна."""
    assert segments
    assert segments[0][0] == 0
    assert segments[-1][1] == length
    assert all(0 < end - start <= window for start, end in segments)
    assert all(left[1] == right[0] for left, right in pairwise(segments))


def _assert_independent_copy(
    result: npt.NDArray[np.float32], audio: npt.NDArray[np.float32]
) -> None:
    assert result is not audio
    assert not np.shares_memory(result, audio)
    snapshot = result.copy()
    audio[:] = 0.75
    np.testing.assert_array_equal(result, snapshot)


def _warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == vad.__name__ and r.levelno >= logging.WARNING
    ]


def _pause_profile(length: int, pauses: list[tuple[int, int]]) -> list[float]:
    return [
        0.0 if any(start <= frame < end for start, end in pauses) else 1.0
        for frame in range(0, length, FRAME)
    ]


@pytest.mark.parametrize(
    ("seconds", "max_window_s"), [(1, 24.0), (20, 24.0), (24, 24.0), (20, 2.0)]
)
def test_short_recording_does_not_load_model(
    monkeypatch: pytest.MonkeyPatch, seconds: int, max_window_s: float
) -> None:
    """Пороги 20 секунд и длины окна включительны и не требуют модели."""

    def forbidden_load() -> bool:
        pytest.fail("Запись не должна вызывать загрузчик модели")

    monkeypatch.setattr(vad, "_load_session", forbidden_load)
    audio = np.zeros(seconds * SAMPLE_RATE, dtype=np.float32)
    assert vad.segment(audio, max_window_s=max_window_s) == [(0, len(audio))]
    assert vad._session is None
    assert vad._load_attempted is False


def test_long_recording_with_small_window_requests_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """24 секунды при окне 2 секунды уже должны запрашивать модель."""
    calls = 0

    def unavailable_load() -> bool:
        nonlocal calls
        calls += 1
        return False

    monkeypatch.setattr(vad, "_load_session", unavailable_load)
    audio = np.zeros(24 * SAMPLE_RATE, dtype=np.float32)
    assert vad.segment(audio, max_window_s=2.0) == [(0, len(audio))]
    assert calls == 1


def test_empty_audio(monkeypatch: pytest.MonkeyPatch) -> None:
    """Пустая запись не требует модели; обрезка возвращает отдельный массив."""

    def forbidden_load() -> bool:
        pytest.fail("Пустая запись не должна передаваться модели")

    monkeypatch.setattr(vad, "_load_session", forbidden_load)
    audio = np.empty(0, dtype=np.float32)
    assert vad.segment(audio) == []
    trimmed = vad.trim_trailing_silence(audio)
    np.testing.assert_array_equal(trimmed, audio)
    _assert_independent_copy(trimmed, audio)
    assert vad._session is None


@pytest.mark.parametrize("operation", [vad.segment, vad.trim_trailing_silence])
@pytest.mark.parametrize(
    ("dtype", "shape", "sample_rate"),
    [
        ("float64", (16,), 16000),
        ("int16", (16,), 16000),
        ("float32", (2, 16), 16000),
        ("float32", (16,), 8000),
    ],
    ids=["float64", "int16", "stereo", "8000-hz"],
)
def test_invalid_audio(
    operation: Callable[[npt.NDArray[np.float32], int], object],
    dtype: str,
    shape: tuple[int, ...],
    sample_rate: int,
) -> None:
    """Обе операции отвергают неверный тип, размерность и частоту."""
    audio = np.zeros(shape, dtype=dtype)
    with pytest.raises(ValueError):
        operation(audio, sample_rate)
    assert vad._load_attempted is False


@pytest.mark.parametrize("max_window_s", [0.0, 1.999, float("inf"), float("nan")])
def test_invalid_window(max_window_s: float) -> None:
    """Окно должно быть конечным и не короче двух секунд."""
    with pytest.raises(ValueError):
        vad.segment(np.zeros(SAMPLE_RATE, dtype=np.float32), max_window_s=max_window_s)
    assert vad._load_attempted is False


def test_corrupt_samples_warn_once_without_loading_model(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Все виды испорченных отсчётов используют общий флаг предупреждения."""

    def forbidden_load() -> bool:
        pytest.fail("Испорченная запись не должна вызывать загрузчик модели")

    monkeypatch.setattr(vad, "_load_session", forbidden_load)
    with caplog.at_level(logging.WARNING, logger=vad.__name__):
        for value in (float("nan"), float("inf"), -float("inf"), 1e30, 10.1):
            for _ in range(2):
                audio = np.full(25 * SAMPLE_RATE, value, dtype=np.float32)
                assert vad.segment(audio) == [(0, len(audio))]
                trimmed = vad.trim_trailing_silence(audio)
                np.testing.assert_array_equal(trimmed, audio)
                _assert_independent_copy(trimmed, audio)
    assert len(_warnings(caplog)) == 1
    assert "некорректные значения" in _warnings(caplog)[0]
    assert vad._session is None
    assert vad._load_attempted is False


@pytest.mark.parametrize("value", [10.0, -10.0])
def test_amplitude_limit_is_inclusive(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, value: float
) -> None:
    """Допустимая амплитуда доходит до модели в обеих операциях."""
    audio = np.full(25 * SAMPLE_RATE, value, dtype=np.float32)
    session = FakeSession([0.0])
    _install_session(monkeypatch, session)
    frames = (len(audio) + FRAME - 1) // FRAME
    with caplog.at_level(logging.WARNING, logger=vad.__name__):
        segments = vad.segment(audio)
        _assert_coverage(segments, len(audio), 24 * SAMPLE_RATE)
        assert len(segments) > 1
        assert session.calls == frames
        trimmed = vad.trim_trailing_silence(audio)
        assert session.calls == 2 * frames
    assert _warnings(caplog) == []
    np.testing.assert_array_equal(trimmed, audio)
    _assert_independent_copy(trimmed, audio)


def test_missing_model_warns_only_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Без модели все вызовы сохраняют запись и выдают одно предупреждение."""
    (tmp_path / "vad").mkdir()
    monkeypatch.setattr(paths, "data_dir_static", lambda: tmp_path)
    audio = np.zeros(60 * SAMPLE_RATE, dtype=np.float32)
    with caplog.at_level(logging.WARNING, logger=vad.__name__):
        for _ in range(3):
            assert vad.segment(audio) == [(0, len(audio))]
            trimmed = vad.trim_trailing_silence(audio)
            np.testing.assert_array_equal(trimmed, audio)
            _assert_independent_copy(trimmed, audio)
            assert vad.vad_available() is False
        # Повторная попытка загрузки также не повторяет предупреждение.
        vad.unload()
        assert vad.segment(audio) == [(0, len(audio))]
        assert vad.vad_available() is False
    assert len(_warnings(caplog)) == 1
    assert "Не удалось включить определение речи" in _warnings(caplog)[0]
    assert vad._session is None


def test_inference_failure_preserves_session_and_recovers(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Ошибка записи не отключает VAD и предупреждает лишь один раз за процесс."""
    audio = np.zeros(40 * SAMPLE_RATE, dtype=np.float32)
    session = FakeSession([1.0], fail_every=5)
    _install_session(monkeypatch, session)
    with caplog.at_level(logging.WARNING, logger=vad.__name__):
        for attempt in range(3):
            assert vad.segment(audio) == [(0, len(audio))]
            assert session.calls == (attempt + 1) * 5
            assert vad._session is session
            assert vad._load_attempted is True
            assert vad.vad_available() is True
        trimmed = vad.trim_trailing_silence(audio)
        assert session.calls == 20
        np.testing.assert_array_equal(trimmed, audio)
        _assert_independent_copy(trimmed, audio)
        assert vad._session is session
        assert vad._load_attempted is True
        assert vad.vad_available() is True
        healthy = FakeSession(
            _pause_profile(
                len(audio),
                [(8 * SAMPLE_RATE, 11 * SAMPLE_RATE), (29 * SAMPLE_RATE, 32 * SAMPLE_RATE)],
            )
        )
        monkeypatch.setattr(vad, "_session", healthy)
        segments = vad.segment(audio)
        assert healthy.calls == (len(audio) + FRAME - 1) // FRAME
        assert len(segments) > 1
        _assert_coverage(segments, len(audio), 24 * SAMPLE_RATE)
        assert abs(segments[0][1] - int(9.5 * SAMPLE_RATE)) <= FRAME
        assert vad.vad_available() is True
    assert len(_warnings(caplog)) == 1
    assert "Не удалось разобрать запись по паузам" in _warnings(caplog)[0]
    assert "Не удалось включить определение речи" not in _warnings(caplog)[0]


@pytest.mark.parametrize("cancel_before", [True, False], ids=["before", "fifth-frame"])
def test_cancellation_stops_inference_without_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, cancel_before: bool
) -> None:
    """Отмена до запуска и внутри run возвращает запись целиком без предупреждений."""
    token = CancelToken()
    session = FakeSession([1.0], cancel=token)
    _install_session(monkeypatch, session)
    if cancel_before:
        token.cancel()
    audio = np.zeros(60 * SAMPLE_RATE, dtype=np.float32)
    with caplog.at_level(logging.WARNING):
        assert vad.segment(audio, cancel=token) == [(0, len(audio))]
    assert token.cancelled
    if cancel_before:
        assert session.calls == 0
    else:
        assert 5 <= session.calls <= 32
    assert vad._session is session
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_segment_prefers_longest_pause_over_last(monkeypatch: pytest.MonkeyPatch) -> None:
    """В одном окне длинная пауза идёт раньше короткой; выбирается длинная."""
    audio = np.zeros(60 * SAMPLE_RATE, dtype=np.float32)
    pauses = [
        (start * SAMPLE_RATE, end * SAMPLE_RATE)
        for start, end in [(8, 11), (19, 20), (29, 32), (40, 41), (50, 53)]
    ]
    probabilities = _pause_profile(len(audio), pauses)
    monkeypatch.setattr(vad, "_probabilities", lambda audio, cancel=None: probabilities)

    segments = vad.segment(audio)

    assert len(segments) > 1
    _assert_coverage(segments, len(audio), 24 * SAMPLE_RATE)
    assert abs(segments[0][1] - int(9.5 * SAMPLE_RATE)) <= FRAME
    assert not pauses[1][0] <= segments[0][1] <= pauses[1][1]
    for _, boundary in segments[:-1]:
        assert any(start <= boundary < end for start, end in pauses), boundary / SAMPLE_RATE


@pytest.mark.parametrize("max_window_s", [2.0, 24.0])
def test_continuous_speech_warns_once_per_call(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, max_window_s: float
) -> None:
    """Принудительные разрезы ограничены окном и предупреждают один раз за вызов."""
    audio = np.zeros(60 * SAMPLE_RATE, dtype=np.float32)
    probabilities = [1.0] * ((len(audio) + FRAME - 1) // FRAME)
    monkeypatch.setattr(vad, "_probabilities", lambda audio, cancel=None: probabilities)
    with caplog.at_level(logging.WARNING, logger=vad.__name__):
        for _ in range(2):
            caplog.clear()
            segments = vad.segment(audio, max_window_s=max_window_s)
            assert len(segments) > 2
            _assert_coverage(segments, len(audio), int(max_window_s * SAMPLE_RATE))
            assert len(_warnings(caplog)) == 1
            assert "посередине речи" in _warnings(caplog)[0]


def test_trim_keeps_speech_and_200_ms(monkeypatch: pytest.MonkeyPatch) -> None:
    """Обрезка сохраняет каждый отсчёт речи и ровно 200 мс после последнего кадра."""
    speech_frames = 100
    speech_end = speech_frames * FRAME
    audio = np.zeros(300 * FRAME, dtype=np.float32)
    audio[:speech_end] = np.linspace(-0.2, 0.2, speech_end, dtype=np.float32)
    probabilities = [1.0] * speech_frames + [0.0] * 200
    monkeypatch.setattr(vad, "_probabilities", lambda audio, cancel=None: probabilities)

    trimmed = vad.trim_trailing_silence(audio)

    assert len(trimmed) == speech_end + int(0.2 * SAMPLE_RATE)
    np.testing.assert_array_equal(trimmed, audio[: len(trimmed)])
    np.testing.assert_array_equal(trimmed[:speech_end], audio[:speech_end])
    _assert_independent_copy(trimmed, audio)


@pytest.mark.parametrize("probability", [0.0, 1.0], ids=["no-speech", "speech-to-end"])
def test_trim_without_removable_tail_returns_copy(
    monkeypatch: pytest.MonkeyPatch, probability: float
) -> None:
    """Без речи и без хвостовой тишины возвращается независимая полная копия."""
    audio = np.zeros(300 * FRAME, dtype=np.float32)
    monkeypatch.setattr(vad, "_probabilities", lambda audio, cancel=None: [probability] * 300)
    trimmed = vad.trim_trailing_silence(audio)
    np.testing.assert_array_equal(trimmed, audio)
    _assert_independent_copy(trimmed, audio)


def test_bundled_model_integrity_and_directory_contents() -> None:
    """Хеш соответствует sha256sum-манифесту, лишние файлы запрещены."""
    directory = Path(__file__).resolve().parents[2] / "data" / "vad"
    entries = list(directory.iterdir())
    assert {entry.name for entry in entries} == {"silero_vad.onnx", "SHA256SUMS", "README.md"}
    assert all(entry.is_file() for entry in entries)
    lines = (directory / "SHA256SUMS").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    match = re.fullmatch(r"([0-9a-fA-F]{64}) ([ *])(.+)", lines[0])
    assert match is not None, "Ожидается строка в формате sha256sum"
    expected_hash, _, filename = match.groups()
    assert filename == "silero_vad.onnx"
    assert sha256_file(directory / filename) == expected_hash.lower()
