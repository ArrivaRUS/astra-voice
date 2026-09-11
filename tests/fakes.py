"""Общие фейки звука и движка: без numpy, ORT, устройства и дисплея."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Any, Protocol


class Cancellation(Protocol):
    """Доступная движку часть токена отмены."""

    @property
    def cancelled(self) -> bool:
        """Возвращает признак отмены."""
        ...


class FakeCancelToken:
    """Потокобезопасный токен без зависимости от реализации движка."""

    def __init__(self) -> None:
        self._event = Event()

    def cancel(self) -> None:
        """Помечает работу отменённой."""
        self._event.set()

    @property
    def cancelled(self) -> bool:
        """Проверяет отмену."""
        return self._event.is_set()


@dataclass(frozen=True)
class FakeLoadResult:
    """Результат загрузки по контракту движка."""

    load_ms: float = 1.5
    engine_version: str = "fake-1"
    sessions: int = 3


@dataclass(frozen=True)
class FakeTranscribeResult:
    """Результат инференса по контракту движка."""

    text: str
    infer_ms: float
    cancelled: bool


class FakeEngine:
    """Управляемый движок; gate удерживает вызов до разрешения тестом.

    ``unsafe_text`` даёт строку, похожую на команды, ``huge_text`` — 50 МиБ.
    Исключение намеренно содержит text: проверяем отсутствие утечки через ошибки.
    """

    def __init__(
        self,
        text: str = "проверка",
        *,
        delay: float = 0.0,
        respect_cancel: bool = True,
        raises: bool = False,
        load_raises: bool = False,
        unsafe_text: bool = False,
        huge_text: bool = False,
        cancelled_result: bool = False,
        gate: Event | None = None,
    ) -> None:
        self.text = (
            "x" * (50 * 1024 * 1024) if huge_text else ("ls\n rm -rf ~" if unsafe_text else text)
        )
        self.delay = delay
        self.respect_cancel = respect_cancel
        self.raises = raises
        self.load_raises = load_raises
        self.cancelled_result = cancelled_result
        self.gate = gate
        self.started = Event()
        self.finished = Event()
        self.load_calls = 0
        self.transcribe_calls = 0
        self.unload_calls = 0
        self.load_args: tuple[Path, str, str, int] | None = None
        self.audios: list[Any] = []
        self.tokens: list[Cancellation] = []

    def load(self, model_dir: Path, layout: str, variant: str, threads: int) -> FakeLoadResult:
        """Запоминает параметры и возвращает известные метрики."""
        self.load_calls += 1
        self.load_args = model_dir, layout, variant, threads
        if self.load_raises:
            raise RuntimeError(self.text)
        return FakeLoadResult()

    def transcribe(self, audio: Any, cancel: Cancellation) -> FakeTranscribeResult:
        """Имитирует задержку, отмену или исключение."""
        self.transcribe_calls += 1
        self.audios.append(audio)
        self.tokens.append(cancel)
        self.started.set()
        started = time.monotonic()
        try:
            while time.monotonic() - started < self.delay or (
                self.gate is not None and not self.gate.is_set()
            ):
                if self.respect_cancel and cancel.cancelled:
                    return FakeTranscribeResult("", 0.0, True)
                time.sleep(0.002)
            if self.raises:
                raise RuntimeError(self.text)
            cancelled = self.cancelled_result or (self.respect_cancel and cancel.cancelled)
            return FakeTranscribeResult(self.text, 0.0, cancelled)
        finally:
            self.finished.set()

    def unload(self) -> None:
        """Учитывает освобождение модели."""
        self.unload_calls += 1


class FakeAudioSource:
    """Отдаёт заранее заданные нормализованные кадры PCM по запросу теста."""

    def __init__(self, frames: Iterable[Iterable[float]] = ()) -> None:
        self.frames = [list(frame) for frame in frames]
        self.close_calls = 0

    def feed(self, utterance_id: str, sink: Callable[[str, Iterable[float]], None]) -> None:
        """Передаёт кадры методу feed_audio автомата."""
        for frame in self.frames:
            sink(utterance_id, frame)

    def close(self) -> None:
        """Имитирует закрытие устройства."""
        self.close_calls += 1
