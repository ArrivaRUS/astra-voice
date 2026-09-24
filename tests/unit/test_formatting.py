"""Проверки подписей загрузки и расчёта средней скорости."""

from __future__ import annotations

import math
from typing import cast

import pytest

from astra_voice.ui.formatting import (
    SpeedTracker,
    clean_display_name,
    format_accuracy,
    format_eta,
    format_size,
    format_space,
    format_speed,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("wer", "expected"),
    [(0, "100,0 %"), (100, "0,0 %"), (5.55, "94,5 %"), (5.45, "94,6 %")],
)
def test_format_accuracy(wer: float, expected: str) -> None:
    assert format_accuracy(wer) == expected


@pytest.mark.parametrize("for_menu", [False, True])
def test_clean_display_name_controls_and_mnemonics(for_menu: bool) -> None:
    controls = "".join(chr(code) for code in (*range(32), *range(127, 160)))
    bidi = "\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069\u200e\u200f"
    text = f"  A&B{controls}{bidi}\r\n  модель\u00a0   один  "
    assert clean_display_name(text, for_menu=for_menu) == (
        "A&&B модель один" if for_menu else "A&B модель один"
    )


@pytest.mark.parametrize(
    ("text", "limit", "expected"),
    [
        ("abcdef", 4, "abc…"),
        ("abcd", 4, "abcd"),
        ("abc", 1, "…"),
        ("abc", 0, ""),
        ("abc", -1, ""),
        ("", 80, ""),
        ("x" * 500, 80, "x" * 79 + "…"),
    ],
)
def test_clean_display_name_limit(text: str, limit: int, expected: str) -> None:
    assert clean_display_name(text, limit=limit) == expected


def test_clean_display_name_escapes_complete_mnemonics_after_limit() -> None:
    assert clean_display_name("&&&&", limit=3, for_menu=True) == "&&&&…"


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (0, "осталось меньше минуты"),
        (59, "осталось меньше минуты"),
        (60, "осталось ~1 мин"),
        (61, "осталось ~1 мин"),
        (89, "осталось ~1 мин"),
        (90, "осталось ~2 мин"),
        (3599, "осталось ~60 мин"),
        (3600, "осталось ~1 ч"),
        (3601, "осталось ~1 ч"),
        (4200, "осталось ~1 ч 10 мин"),
        (7169, "осталось ~1 ч 59 мин"),
        (7170, "осталось ~2 ч"),
        (7199, "осталось ~2 ч"),
        (float("inf"), "считаю…"),
        (float("nan"), "считаю…"),
        (-1, "считаю…"),
    ],
)
def test_format_eta(seconds: float, expected: str) -> None:
    """Время округляется до минут с переносом в следующий час."""
    assert format_eta(seconds, elapsed_s=10, stable=True) == expected


@pytest.mark.parametrize("seconds", [0, 60, 4200])
@pytest.mark.parametrize(("elapsed_s", "stable"), [(4.9, True), (10, False)])
def test_format_eta_waits(seconds: float, elapsed_s: float, stable: bool) -> None:
    """До готовности оценки показывается ожидание."""
    assert format_eta(seconds, elapsed_s, stable) == "считаю…"


def test_format_eta_elapsed_boundary() -> None:
    """Ровно через пять секунд оценку уже можно показать."""
    assert format_eta(60, elapsed_s=5.0, stable=True) == "осталось ~1 мин"


@pytest.mark.parametrize("value", [None, "нет", True, False, complex(1, 2)])
def test_formatters_reject_non_numbers(value: object) -> None:
    """Нечисловые значения не попадают в подписи."""
    number = cast(float, value)
    assert format_eta(number, elapsed_s=10, stable=True) == "считаю…"
    assert format_eta(60, elapsed_s=number, stable=True) == "считаю…"
    assert format_speed(number) == ""
    assert format_size(number) == ""
    assert format_space(number) == ""


@pytest.mark.parametrize("elapsed_s", [float("inf"), float("nan"), -1.0])
def test_format_eta_invalid_elapsed(elapsed_s: float) -> None:
    """Неверное время начала не позволяет показать оценку."""
    assert format_eta(60, elapsed_s, stable=True) == "считаю…"


@pytest.mark.parametrize(
    ("speed", "expected"),
    [
        (0, ""),
        (float("nan"), ""),
        (float("inf"), ""),
        (-5, ""),
        (1, "0 КБ/с"),
        (2500, "2 КБ/с"),
        (860_000, "860 КБ/с"),
        (999_499, "999 КБ/с"),
        (999_500, "1,0 МБ/с"),
        (999_999, "1,0 МБ/с"),
        (1_000_000, "1,0 МБ/с"),
        (5_200_000, "5,2 МБ/с"),
    ],
)
def test_format_speed(speed: float, expected: str) -> None:
    """Скорость меняет единицу с учётом округления."""
    assert format_speed(speed) == expected


@pytest.mark.parametrize(
    ("size", "round_up", "expected"),
    [
        (226_000_000, False, "226 МБ"),
        (0, False, "0 МБ"),
        (1_500_000, True, "2 МБ"),
        (1_100_000, True, "2 МБ"),
        (1_100_000, False, "1 МБ"),
        (2_500_000, False, "2 МБ"),
        (-1, False, ""),
        (float("nan"), False, ""),
        (float("inf"), True, ""),
    ],
)
def test_format_size(size: float, round_up: bool, expected: str) -> None:
    """Размер округляется по выбранному правилу."""
    assert format_size(size, round_up=round_up) == expected


@pytest.mark.parametrize(
    ("size", "expected"),
    [
        (0, "0 МБ"),
        (512_000_000, "512 МБ"),
        (999_999_999, "1000 МБ"),
        (1_000_000_000, "1,0 ГБ"),
        (42_100_000_000, "42,1 ГБ"),
        (-1, ""),
        (float("nan"), ""),
        (float("inf"), ""),
        (float("-inf"), ""),
    ],
)
def test_format_space(size: float, expected: str) -> None:
    assert format_space(size) == expected


def test_tracker_uniform_download() -> None:
    """Равномерная загрузка даёт ожидаемую скорость и оставшееся время."""
    tracker = SpeedTracker()
    tracker.reset(now=100.0)
    for elapsed in range(5):
        tracker.update(elapsed * 1000, now=100.0 + elapsed)
        assert not tracker.stable
    tracker.update(4900, now=104.9)
    assert not tracker.stable
    tracker.update(5000, now=105.0)
    assert tracker.stable
    tracker.update(6000, now=106.0)
    assert tracker.elapsed_s == pytest.approx(6.0)
    assert tracker.speed_bps == pytest.approx(1000.0)
    assert tracker.stable
    assert tracker.eta_s(10_000) == pytest.approx(4.0)
    assert tracker.eta_s(6000) == 0.0
    assert tracker.eta_s(5000) == 0.0


def test_tracker_zero_speed_and_short_interval() -> None:
    """Без роста объёма за достаточное время скорость равна нулю."""
    tracker = SpeedTracker()
    assert tracker.elapsed_s == 0.0
    assert tracker.speed_bps == 0.0
    assert not tracker.stable
    assert math.isinf(tracker.eta_s(10_000))
    tracker.update(0, now=0.0)
    assert tracker.speed_bps == 0.0
    assert math.isinf(tracker.eta_s(10_000))
    tracker.update(500, now=0.5)
    assert tracker.speed_bps == 0.0
    tracker.update(1000, now=1.0)
    assert tracker.speed_bps == pytest.approx(1000.0)
    tracker.update(0, now=6.0)
    assert tracker.speed_bps == 0.0
    assert not tracker.stable
    assert math.isinf(tracker.eta_s(10_000))
    tracker.update(-1, now=7.0)
    assert tracker.speed_bps == 0.0


def test_tracker_discards_old_samples() -> None:
    """Старая высокая скорость не влияет на среднее за текущее окно."""
    tracker = SpeedTracker(window_s=3.0, min_elapsed_s=2.0)
    tracker.update(0, now=0.0)
    tracker.update(10_000, now=1.0)
    tracker.update(10_100, now=2.0)
    assert tracker.stable
    tracker.update(10_300, now=4.0)
    assert tracker.speed_bps == pytest.approx(100.0)
    tracker.update(10_900, now=5.0)
    assert tracker.speed_bps == pytest.approx(800 / 3)


def test_tracker_keeps_one_previous_sample() -> None:
    """После долгой паузы остаётся предыдущее измерение для расчёта."""
    tracker = SpeedTracker(window_s=2.0)
    tracker.update(0, now=0.0)
    tracker.update(1000, now=1.0)
    tracker.update(10_000, now=10.0)
    assert tracker.speed_bps == pytest.approx(1000.0)


def test_tracker_reset() -> None:
    """Сброс удаляет прежние измерения и меняет начало отсчёта."""
    tracker = SpeedTracker()
    tracker.update(0, now=0.0)
    tracker.update(6000, now=6.0)
    tracker.reset(now=20.0)
    assert tracker.elapsed_s == 0.0
    assert tracker.speed_bps == 0.0
    assert not tracker.stable
    assert math.isinf(tracker.eta_s(10_000))
    tracker.update(1000, now=21.0)
    assert tracker.elapsed_s == pytest.approx(1.0)
    assert tracker.speed_bps == 0.0
    tracker.reset()
    tracker.update(2000, now=2.0)
    assert tracker.elapsed_s == pytest.approx(2.0)


@pytest.mark.parametrize(
    "value", [None, "нет", True, False, complex(1, 2), float("inf"), float("nan")]
)
def test_tracker_ignores_invalid_samples(value: object) -> None:
    """Неверные измерения не меняют ни скорость, ни прошедшее время."""
    tracker = SpeedTracker()
    tracker.update(0, now=0.0)
    tracker.update(6000, now=6.0)
    tracker.update(cast(int, value), now=100.0)
    tracker.update(100_000, now=cast(float, value))
    assert tracker.elapsed_s == pytest.approx(6.0)
    assert tracker.speed_bps == pytest.approx(1000.0)
    assert tracker.stable
    assert tracker.eta_s(10_000) == pytest.approx(4.0)
