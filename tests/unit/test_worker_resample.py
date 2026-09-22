"""Децимация 16→8 кГц для моделей с телефонной частотой (T-one)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    import numpy as np
    import numpy.typing as npt

pytestmark = pytest.mark.unit

np = pytest.importorskip("numpy", reason="Для проверки децимации нужен numpy")

from astra_voice.worker.engine import (  # noqa: E402 — после importorskip numpy
    MODEL_RATE_MESSAGE,
    check_model_rate,
    resample_for_model,
)
from astra_voice.worker.resample import (  # noqa: E402
    SOURCE_RATE,
    TAPS,
    TARGET_RATE,
    Decimator2,
    resample_16k_to_8k,
    resample_batch_16k_to_8k,
)


def tone(hz: float, samples: int, rate: int = SOURCE_RATE) -> npt.NDArray[np.float32]:
    """Синус единичной амплитуды — ровно то, чем меряют фильтр."""
    signal: npt.NDArray[np.float32] = np.sin(2 * np.pi * hz * np.arange(samples) / rate).astype(
        np.float32
    )
    return signal


def rms(signal: npt.NDArray[np.float32]) -> float:
    """Среднеквадратичное без краёв: края занимает разгон фильтра."""
    core = signal[TAPS:-TAPS] if signal.size > 3 * TAPS else signal
    return float(np.sqrt(np.mean(np.square(core.astype(np.float64)))))


# ── ядро фильтра ───────────────────────────────────────────────────────────


def test_kernel_is_halfband_with_unit_gain() -> None:
    """Полуполосный фильтр: нули на чётном смещении, центр 1/2, сумма 1."""
    from astra_voice.worker.resample import _kernel

    kernel = _kernel()
    assert kernel.dtype == np.float32
    assert kernel.size == TAPS and TAPS % 4 == 3
    center = TAPS // 2
    offsets = np.arange(TAPS) - center
    assert np.allclose(kernel[(offsets % 2 == 0) & (offsets != 0)], 0.0, atol=1e-9)
    assert kernel[center] == pytest.approx(0.5, abs=1e-3)
    assert float(kernel.sum()) == pytest.approx(1.0, abs=1e-6)
    # Симметрия — линейная фаза: задержка одинакова для всех частот.
    assert np.allclose(kernel, kernel[::-1], atol=1e-9)


def test_kernel_is_built_once_per_process() -> None:
    """На кадр ядро не пересобирается: держим один и тот же массив."""
    from astra_voice.worker.resample import _kernel

    assert _kernel() is _kernel()
    assert Decimator2()._kernel is _kernel()


# ── частотная характеристика ───────────────────────────────────────────────


@pytest.mark.parametrize("hz", [200, 1000, 2000, 3000, 3400])
def test_speech_band_passes_without_loss(hz: int) -> None:
    """Телефонная полоса 300–3400 Гц проходит с прежней громкостью."""
    result = resample_16k_to_8k(tone(hz, SOURCE_RATE))

    assert rms(result) == pytest.approx(2**-0.5, rel=0.02)


@pytest.mark.parametrize("hz", [5000, 6000, 7000, 7900])
def test_above_half_band_is_suppressed(hz: int) -> None:
    """Всё выше 4 кГц давится, иначе после прореживания вернулось бы помехой."""
    result = resample_16k_to_8k(tone(hz, SOURCE_RATE))

    # −40 дБ — запас: у окна Хэмминга на этих частотах около −58 дБ.
    assert rms(result) < 2**-0.5 / 100


def test_decimation_without_filter_would_alias() -> None:
    """Контрольный опыт: голое прореживание превратило бы 6 кГц в 2 кГц."""
    naive = tone(6000, SOURCE_RATE)[::2]

    assert rms(naive) == pytest.approx(2**-0.5, rel=0.02)
    assert rms(resample_16k_to_8k(tone(6000, SOURCE_RATE))) < rms(naive) / 100


def test_tone_keeps_its_frequency_after_decimation() -> None:
    """1 кГц остаётся 1 кГц: считаем по максимуму спектра на 8 кГц."""
    result = resample_16k_to_8k(tone(1000, SOURCE_RATE))
    spectrum = np.abs(np.fft.rfft(result.astype(np.float64)))
    peak = int(np.argmax(spectrum))

    assert peak * TARGET_RATE / result.size == pytest.approx(1000, abs=2)


# ── длина, выравнивание, пустой вход ───────────────────────────────────────


@pytest.mark.parametrize("samples", [0, 1, 2, 3, 160, 1599, 1600, 16000])
def test_output_length_is_exactly_half(samples: int) -> None:
    result = resample_16k_to_8k(tone(1000, samples))

    assert result.size == (samples + 1) // 2
    assert result.dtype == np.float32


def test_silence_stays_silence() -> None:
    result = resample_16k_to_8k(np.zeros(4000, dtype=np.float32))

    assert result.size == 2000
    assert not np.any(result)


def test_group_delay_is_compensated() -> None:
    """Импульс не уезжает: фильтр линейнофазный, задержка снята на выходе."""
    impulse = np.zeros(4000, dtype=np.float32)
    impulse[2000] = 1.0

    result = resample_16k_to_8k(impulse)

    # 2000-й отсчёт входа — 1000-й выхода; допуск в один отсчёт на полфазы.
    assert abs(int(np.argmax(np.abs(result))) - 1000) <= 1


# ── состояние между кусками ────────────────────────────────────────────────


@pytest.mark.parametrize("splits", [(997, 2001), (1, 2), (2000,), (3999,)])
def test_chunked_stream_equals_single_pass(splits: tuple[int, ...]) -> None:
    """Куски любой длины, в том числе нечётной, дают тот же поток целиком."""
    signal = tone(1000, 4000)
    decimator = Decimator2()
    bounds = (0, *splits, signal.size)
    pairs = zip(bounds[:-1], bounds[1:], strict=True)
    chunked = np.concatenate([decimator.process(signal[start:stop]) for start, stop in pairs])

    whole = Decimator2().process(signal)

    assert chunked.size == whole.size
    assert np.allclose(chunked, whole, atol=1e-6)


def test_chunk_seam_has_no_click() -> None:
    """На стыке кусков нет скачка: соседние отсчёты меняются как внутри куска."""
    signal = tone(1000, 4000)
    decimator = Decimator2()
    first = decimator.process(signal[:1001])
    second = decimator.process(signal[1001:])
    seam = np.concatenate((first[-4:], second[:4]))
    reference = np.diff(Decimator2().process(signal))

    assert np.max(np.abs(np.diff(seam))) <= float(np.max(np.abs(reference))) + 1e-6


def test_state_is_not_shared_between_decimators() -> None:
    """Новый экземпляр начинает с тишины, а не с хвоста прошлой фразы."""
    first = Decimator2()
    first.process(tone(1000, 4000))

    assert np.allclose(
        Decimator2().process(tone(1000, 400)), first.__class__().process(tone(1000, 400))
    )


def test_empty_block_keeps_phase() -> None:
    decimator = Decimator2()
    signal = tone(1000, 400)
    head = decimator.process(signal[:101])
    assert decimator.process(np.zeros(0, dtype=np.float32)).size == 0
    tail = decimator.process(signal[101:])

    assert np.allclose(np.concatenate((head, tail)), Decimator2().process(signal), atol=1e-6)


def test_flush_returns_filter_tail() -> None:
    decimator = Decimator2()
    decimator.process(tone(1000, 400))

    assert decimator.flush().size == TAPS // 2 // 2 + 1


# ── пачка сигналов, как её отдаёт onnx-asr ─────────────────────────────────


def test_batch_uses_each_length_separately() -> None:
    long_signal, short_signal = tone(1000, 4000), tone(1000, 800)
    waveforms = np.zeros((2, 4000), dtype=np.float32)
    waveforms[0] = long_signal
    waveforms[1, :800] = short_signal
    lengths = np.array([4000, 800], dtype=np.int64)

    result, result_lens = resample_batch_16k_to_8k(waveforms, lengths)

    assert result.shape == (2, 2000)
    assert result_lens.tolist() == [2000, 400]
    assert result.dtype == np.float32 and result_lens.dtype == np.int64
    assert np.allclose(result[0], resample_16k_to_8k(long_signal), atol=1e-6)
    assert np.allclose(result[1, :400], resample_16k_to_8k(short_signal), atol=1e-6)
    # Дополнение короткой строки остаётся нулевым, а не размазанным хвостом.
    assert not np.any(result[1, 400:])


@pytest.mark.parametrize(
    ("waveforms", "lengths"),
    [
        (np.zeros(10, dtype=np.float32), np.array([10], dtype=np.int64)),
        (np.zeros((2, 10), dtype=np.float32), np.array([10], dtype=np.int64)),
    ],
    ids=["одномерная", "длин меньше строк"],
)
def test_batch_rejects_wrong_shapes(
    waveforms: npt.NDArray[np.float32], lengths: npt.NDArray[np.int64]
) -> None:
    with pytest.raises(ValueError):
        resample_batch_16k_to_8k(waveforms, lengths)


# ── выбор частоты в движке ─────────────────────────────────────────────────


def test_model_on_16k_gets_capture_untouched() -> None:
    waveforms = tone(1000, 1600).reshape(1, -1)
    lengths = np.array([1600], dtype=np.int64)

    result, result_lens = resample_for_model(SOURCE_RATE, waveforms, lengths, SOURCE_RATE)

    assert result is waveforms and result_lens is lengths


def test_model_on_8k_gets_decimated_capture() -> None:
    waveforms = tone(1000, 1600).reshape(1, -1)
    lengths = np.array([1600], dtype=np.int64)

    result, result_lens = resample_for_model(TARGET_RATE, waveforms, lengths, SOURCE_RATE)

    assert result.shape == (1, 800)
    assert result_lens.tolist() == [800]


@pytest.mark.parametrize("rate", [8000, 22050, 44100, 48000, 0, -16000])
def test_other_capture_rates_are_rejected(rate: int) -> None:
    """Захват всегда 16 кГц; другое на вход движка попасть не должно."""
    waveforms = np.zeros((1, 16), dtype=np.float32)
    lengths = np.array([16], dtype=np.int64)

    with pytest.raises(ValueError, match="Ожидается аудио 16000 Гц"):
        resample_for_model(SOURCE_RATE, waveforms, lengths, rate)


@pytest.mark.parametrize("rate", [22050, 24000, 44100, 48000, 0])
def test_other_model_rates_keep_the_old_message(rate: int) -> None:
    waveforms = np.zeros((1, 16), dtype=np.float32)
    lengths = np.array([16], dtype=np.int64)

    with pytest.raises(ValueError, match=MODEL_RATE_MESSAGE):
        resample_for_model(rate, waveforms, lengths, SOURCE_RATE)
    with pytest.raises(ValueError, match=MODEL_RATE_MESSAGE):
        check_model_rate(rate)


@pytest.mark.parametrize("rate", [TARGET_RATE, SOURCE_RATE])
def test_supported_model_rates_pass_the_check(rate: int) -> None:
    check_model_rate(rate)  # не поднимает исключение — других обещаний нет


def test_message_mentions_no_paths_or_hosts() -> None:
    """Текст остаётся прежним и простым: ни путей, ни имён устройств."""
    assert MODEL_RATE_MESSAGE == "Движок поддерживает только модели с частотой 16000 Гц"
