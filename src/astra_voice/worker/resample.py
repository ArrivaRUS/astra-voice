"""Понижение частоты 16→8 кГц для моделей, которым нужен телефонный поток.

Захват у программы всегда 16 кГц. Часть моделей (T-one) работает на 8 кГц, и
`onnx-asr` для таких моделей поднимает семь сессий onnxruntime ради ресемплера —
мы их не создаём (S3-R4), поэтому понижение частоты делаем сами на numpy: scipy
в зависимостях нет и не будет.

Схема — децимация 2:1 с фильтром низких частот перед прореживанием, иначе всё
выше 4 кГц вернулось бы в полосу как помеха. Ядро — оконный sinc с частотой
среза ровно на четверти частоты дискретизации (полуполосный, длина 4k+3);
считается один раз на процесс и переиспользуется, на кадр ничего не строится.

:class:`Decimator2` хранит хвост входа между вызовами, поэтому поток можно
подавать кусками любой длины: стык не даёт щелчка, а фаза прореживания
считается по сквозному номеру отсчёта, а не по началу куска.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import numpy as np
    import numpy.typing as npt

SOURCE_RATE = 16_000
TARGET_RATE = 8_000
# Длина ядра 4k+3 — полуполосный фильтр с целой групповой задержкой (TAPS − 1)/2.
# 95 отсчётов при 16 кГц — задержка 2,9 мс: для распознавания незаметно.
TAPS = 95
# Задержку компенсируем на выходе, чтобы длина результата была ровно половиной.
_DELAY_OUT = (TAPS // 2 + 1) // 2

_kernel_cache: Any = None


def _kernel() -> npt.NDArray[np.float32]:
    """Строит ядро один раз на процесс: numpy импортируется только здесь."""
    global _kernel_cache
    if _kernel_cache is None:
        import numpy as np

        index = np.arange(TAPS, dtype=np.float64) - (TAPS - 1) / 2
        # Идеальный ФНЧ с срезом на четверти частоты: sinc(n/2)/2.
        ideal = np.sinc(index / 2) / 2
        # Окно Хэмминга: боковые лепестки −53 дБ, этого хватает для 6 кГц.
        window = 0.54 - 0.46 * np.cos(2 * np.pi * np.arange(TAPS) / (TAPS - 1))
        taps = ideal * window
        # Единичное усиление на постоянном токе: громкость не меняется.
        _kernel_cache = (taps / taps.sum()).astype(np.float32)
    return _kernel_cache  # type: ignore[no-any-return]


class Decimator2:
    """Децимация 2:1 с ФНЧ; хвост входа сохраняется между кусками."""

    def __init__(self) -> None:
        import numpy as np

        self._kernel = _kernel()
        # История ровно на длину ядра минус один: столько нужно свёртке.
        self._tail: npt.NDArray[np.float32] = np.zeros(TAPS - 1, dtype=np.float32)
        self._index = 0

    def process(self, block: npt.NDArray[np.float32]) -> npt.NDArray[np.float32]:
        """Фильтрует кусок и берёт каждый второй отсчёт по сквозному номеру."""
        import numpy as np

        samples = np.asarray(block, dtype=np.float32).reshape(-1)
        if samples.size == 0:
            return np.zeros(0, dtype=np.float32)
        joined = np.concatenate((self._tail, samples))
        # «valid» даёт ровно len(samples) значений: по одному на входной отсчёт.
        filtered = np.convolve(joined, self._kernel, mode="valid").astype(np.float32)
        self._tail = joined[-(TAPS - 1) :]
        # Фаза прореживания — от начала потока, а не от начала куска.
        start = (-self._index) % 2
        self._index += samples.size
        return filtered[start::2]

    def flush(self) -> npt.NDArray[np.float32]:
        """Досчитывает хвост нулями: последние отсчёты не теряются."""
        import numpy as np

        return self.process(np.zeros(TAPS // 2, dtype=np.float32))


def resample_16k_to_8k(waveform: npt.NDArray[np.float32]) -> npt.NDArray[np.float32]:
    """Понижает один моно-сигнал 16→8 кГц; длина результата — ровно половина."""
    import numpy as np

    samples = np.asarray(waveform, dtype=np.float32).reshape(-1)
    target = (samples.size + 1) // 2
    if target == 0:
        return np.zeros(0, dtype=np.float32)
    decimator = Decimator2()
    # Нули в хвосте покрывают групповую задержку: конец фразы не срезается.
    filtered = np.concatenate((decimator.process(samples), decimator.flush()))
    result = filtered[_DELAY_OUT : _DELAY_OUT + target]
    if result.size < target:
        result = np.concatenate((result, np.zeros(target - result.size, dtype=np.float32)))
    return result


def resample_batch_16k_to_8k(
    waveforms: npt.NDArray[np.float32], waveforms_lens: npt.NDArray[np.int64]
) -> tuple[npt.NDArray[np.float32], npt.NDArray[np.int64]]:
    """Понижает частоту у пачки сигналов, считая длину каждого отдельно."""
    import numpy as np

    batch = np.asarray(waveforms, dtype=np.float32)
    if batch.ndim != 2:
        raise ValueError("Ожидается пачка сигналов формой (batch, samples)")
    lengths = np.asarray(waveforms_lens, dtype=np.int64).reshape(-1)
    if lengths.size != batch.shape[0]:
        raise ValueError("Число длин не совпадает с числом сигналов в пачке")
    target_lens = (lengths + 1) // 2
    width = int(target_lens.max()) if target_lens.size else 0
    result = np.zeros((batch.shape[0], width), dtype=np.float32)
    for row in range(batch.shape[0]):
        length = int(lengths[row])
        if length > 0:
            # Каждая строка считается по своей длине: дополнение не размазывается.
            result[row, : int(target_lens[row])] = resample_16k_to_8k(batch[row, :length])
    return result, target_lens.astype(np.int64)
