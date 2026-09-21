"""Понятные подписи времени, скорости и размера загрузки."""

from __future__ import annotations

import math
from collections import deque


def clean_display_name(text: str, *, limit: int = 80, for_menu: bool = False) -> str:
    """Ограничить видимое имя; для QAction экранировать мнемоники после обрезки."""
    text = text.replace("\r", " ").replace("\n", " ")
    text = "".join(
        char
        for char in text
        if not (
            ord(char) < 32
            or 127 <= ord(char) <= 159
            or "\u202a" <= char <= "\u202e"
            or "\u2066" <= char <= "\u2069"
            or char in "\u200e\u200f"
        )
    )
    text = " ".join(text.split())
    if limit <= 0:
        return ""
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text.replace("&", "&&") if for_menu else text


def _is_finite_number(value: object) -> bool:
    """Проверить, что значение — конечное число, а не логический признак."""
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def format_eta(seconds: float, elapsed_s: float, stable: bool) -> str:
    """Описать оставшееся время с точностью до минут."""
    if not _is_finite_number(elapsed_s) or elapsed_s < 5.0:
        return "считаю…"
    if not stable:
        return "считаю…"
    if not _is_finite_number(seconds) or seconds < 0:
        return "считаю…"
    if seconds < 60:
        return "осталось меньше минуты"
    if seconds < 3600:
        minutes = max(1, math.floor(seconds / 60 + 0.5))
        return f"осталось ~{minutes} мин"
    hours = int(seconds // 3600)
    minutes = math.floor((seconds - hours * 3600) / 60 + 0.5)
    if minutes == 60:
        hours += 1
        minutes = 0
    if minutes == 0:
        return f"осталось ~{hours} ч"
    return f"осталось ~{hours} ч {minutes} мин"


def format_speed(bytes_per_s: float) -> str:
    """Описать скорость загрузки в килобайтах или мегабайтах за секунду."""
    if not _is_finite_number(bytes_per_s) or bytes_per_s <= 0:
        return ""
    kilobytes = round(bytes_per_s / 1000)
    if bytes_per_s < 1_000_000 and kilobytes < 1000:
        return f"{kilobytes} КБ/с"
    return f"{bytes_per_s / 1_000_000:.1f} МБ/с".replace(".", ",")


def format_size(size_bytes: float, *, round_up: bool = False) -> str:
    """Описать размер в целых мегабайтах, при необходимости округляя вверх."""
    if not _is_finite_number(size_bytes) or size_bytes < 0:
        return ""
    value = size_bytes / 1_000_000
    return f"{math.ceil(value) if round_up else value:.0f} МБ"


def format_space(size_bytes: float) -> str:
    """Описать свободное место в десятичных гигабайтах или целых мегабайтах."""
    if not _is_finite_number(size_bytes) or size_bytes < 0:
        return ""
    if size_bytes >= 1_000_000_000:
        return f"{size_bytes / 1_000_000_000:.1f} ГБ".replace(".", ",")
    return format_size(size_bytes)


class SpeedTracker:
    """Средняя скорость загрузки за последние несколько секунд."""

    def __init__(self, *, window_s: float = 8.0, min_elapsed_s: float = 5.0) -> None:
        """Задать длину окна и время ожидания надёжной оценки."""
        self._window_s = window_s
        self._min_elapsed_s = min_elapsed_s
        self._samples: deque[tuple[int, float]] = deque()
        self.reset()

    def reset(self, now: float = 0.0) -> None:
        """Очистить измерения и запомнить время начала загрузки."""
        self._samples.clear()
        self._started_at = now

    def update(self, bytes_done: int, now: float) -> None:
        """Добавить измерение, сохраняя хотя бы одно предыдущее."""
        if not _is_finite_number(bytes_done) or not _is_finite_number(now):
            return
        self._samples.append((bytes_done, now))
        cutoff = now - self._window_s
        while len(self._samples) > 2 and self._samples[0][1] < cutoff:
            self._samples.popleft()

    @property
    def elapsed_s(self) -> float:
        """Вернуть время от начала загрузки до последнего измерения."""
        if not self._samples:
            return 0.0
        return self._samples[-1][1] - self._started_at

    @property
    def speed_bps(self) -> float:
        """Вернуть среднюю скорость в байтах за секунду."""
        if len(self._samples) < 2:
            return 0.0
        first_bytes, first_time = self._samples[0]
        last_bytes, last_time = self._samples[-1]
        interval = last_time - first_time
        bytes_delta = last_bytes - first_bytes
        if interval < 1.0 or bytes_delta <= 0:
            return 0.0
        return bytes_delta / interval

    @property
    def stable(self) -> bool:
        """Проверить, достаточно ли времени прошло для оценки скорости."""
        return self.elapsed_s >= self._min_elapsed_s and self.speed_bps > 0

    def eta_s(self, bytes_total: int) -> float:
        """Оценить оставшееся время, вернув бесконечность при нулевой скорости."""
        speed = self.speed_bps
        if speed == 0:
            return float("inf")
        return max(0.0, (bytes_total - self._samples[-1][0]) / speed)
