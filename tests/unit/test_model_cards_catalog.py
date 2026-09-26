"""Подписи и полоски карточек: мост считает их по настоящему каталогу."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

pytest.importorskip("jsonschema")

# ruff: noqa: E402
from astra_voice.models.catalog import Catalog, Metric, Metrics, load_builtin, merge_measurement
from astra_voice.security.verify import Verifier
from astra_voice.ui.formatting import format_accuracy, format_rtfx
from astra_voice.ui.model_downloads import _vendor_short, catalog_best, entry_metrics, entry_tags

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_ROOT = REPO_ROOT / "data"


@pytest.fixture(scope="module")
def catalog() -> Catalog:
    return load_builtin(Verifier("catalog", keyring=DATA_ROOT / "keys/release.gpg"), root=DATA_ROOT)


def test_recommended_card_tags(catalog: Catalog) -> None:
    entry = catalog.entries[0]
    assert entry.recommended is True
    assert entry_tags(entry) == [
        "Только русский",
        "с пунктуацией",
        "MIT",
        "отечественная",
    ]


def test_tags_skip_what_the_model_does_not_have(catalog: Catalog) -> None:
    vosk = catalog.entry("vosk-small-ru-int8")
    assert vosk is not None
    tags = entry_tags(vosk)
    assert "с пунктуацией" not in tags
    assert "отечественная" not in tags
    assert tags == ["Только русский", "Apache-2.0"]


def test_metrics_fill_and_levels(catalog: Catalog) -> None:
    best_rtfx = catalog_best(catalog.entries)
    assert best_rtfx == 83.5  # Vosk small ru
    entry = catalog.entries[0]
    quality, speed = entry_metrics(entry, best_rtfx)
    assert quality == {
        "kind": "quality",
        "label": "Точность",
        "text": format_accuracy(7.6),
        "fill": pytest.approx(0.924),
        "hasData": True,
        "level": "good",
        "measured": False,
    }
    assert speed == {
        "kind": "speed",
        "label": "Скорость",
        "text": format_rtfx(42.5),
        "fill": pytest.approx(42.5 / best_rtfx),
        "hasData": True,
        "level": "good",
        "measured": False,
    }


def test_accuracy_uses_absolute_scale_and_fastest_speed_fills_bar(catalog: Catalog) -> None:
    best_rtfx = catalog_best(catalog.entries)
    leader = catalog.entry("gigaam-v3-rnnt-int8")
    fastest = catalog.entry("vosk-small-ru-int8")
    assert leader is not None and fastest is not None
    assert entry_metrics(leader, best_rtfx)[0]["fill"] == pytest.approx(0.9561)
    assert entry_metrics(fastest, best_rtfx)[1]["fill"] == 1.0


def test_missing_numbers_are_not_invented(catalog: Catalog) -> None:
    """Whisper small не замерен по протоколу — полоски пустые, а не выдуманные."""
    best_rtfx = catalog_best(catalog.entries)
    entry = catalog.entry("whisper-small-int8")
    assert entry is not None
    for row in entry_metrics(entry, best_rtfx):
        assert row["hasData"] is False
        assert row["text"] == ""
        assert row["fill"] == 0.0
        assert row["level"] == ""


def test_speed_alone_is_enough_for_one_bar(catalog: Catalog) -> None:
    """У многоязычных есть WER, но нет int8-замера скорости: одна полоска из двух."""
    best_rtfx = catalog_best(catalog.entries)
    entry = catalog.entry("gigaam-multilingual-ctc-int8")
    assert entry is not None
    quality, speed = entry_metrics(entry, best_rtfx)
    assert quality["hasData"] is True
    assert speed["hasData"] is False


def test_every_card_has_two_bars_and_a_vendor(catalog: Catalog) -> None:
    best_rtfx = catalog_best(catalog.entries)
    for entry in catalog.entries:
        rows = entry_metrics(entry, best_rtfx)
        assert [row["label"] for row in rows] == ["Точность", "Скорость"]
        assert all(0.0 <= row["fill"] <= 1.0 for row in rows)
        assert all(row["measured"] is False for row in rows)
        assert entry.vendor
        assert entry.vendor_short
        assert entry_tags(entry)


def test_vendor_short_names_come_from_catalog(catalog: Catalog) -> None:
    assert {entry.vendor_short for entry in catalog.entries} == {
        "Сбер",
        "Т-Банк",
        "Alpha Cephei",
        "OpenAI",
        "NVIDIA",
    }
    assert _vendor_short(replace(catalog.entries[0], vendor_short="")) == "Сбер"


@pytest.mark.parametrize(
    ("accuracy", "level"),
    [(92, "good"), (91.9, "fair"), (88, "fair"), (87.9, "weak")],
)
def test_accuracy_levels(catalog: Catalog, accuracy: float, level: str) -> None:
    entry = replace(catalog.entries[0], metrics=Metrics(wer_ru=Metric(100 - accuracy, "source")))
    assert entry_metrics(entry, 0)[0]["level"] == level


@pytest.mark.parametrize(
    ("rtfx", "level"),
    [(20, "good"), (19.9, "fair"), (5, "fair"), (4.9, "weak")],
)
def test_speed_levels(catalog: Catalog, rtfx: float, level: str) -> None:
    entry = replace(catalog.entries[0], metrics=Metrics(rtfx=Metric(rtfx, "source")))
    assert entry_metrics(entry, 20)[1]["level"] == level


def test_empty_catalog_has_no_best(catalog: Catalog) -> None:
    assert catalog_best(()) == 0.0


def test_formatters_use_russian_decimal_comma() -> None:
    assert format_accuracy(7.6) == "92,4 %"
    assert format_rtfx(42.5) == "42,5× быстрее речи"
    assert format_rtfx(19.95) == "20,0× быстрее речи"


def test_merge_measurements_and_thread_matching(catalog: Catalog) -> None:
    entry = catalog.entries[0]
    fallback = merge_measurement(entry, {})
    assert fallback == {"ramMb": entry.min_ram_mb, "ramMeasured": False, "measuredRtfx": None}
    key = f"{entry.id}@{entry.revision}"
    local = merge_measurement(entry, {key: {"ram_mb": 600, "rtfx": 60, "threads": 2}}, 2)
    assert local == {"ramMb": 600, "ramMeasured": True, "measuredRtfx": 60.0}
    no_ram = merge_measurement(entry, {key: {"ram_mb": None, "rtfx": 60, "threads": 2}}, 2)
    assert no_ram == {
        "ramMb": entry.min_ram_mb,
        "ramMeasured": False,
        "measuredRtfx": 60.0,
    }
    assert merge_measurement(entry, {key: {"rtfx": 0, "threads": 2}}) == fallback
    assert merge_measurement(entry, {key: {"ram_mb": 600, "rtfx": 60, "threads": 4}}, 2) == fallback
    assert merge_measurement(entry, {key: {"ram_mb": 600, "threads": True}}, 1) == fallback
    assert merge_measurement(entry, {f"{entry.id}@old": {"ram_mb": 600}}) == fallback


def test_merge_missing_measurement(catalog: Catalog) -> None:
    missing = catalog.entry("whisper-small-int8")
    assert missing is not None
    assert merge_measurement(missing, {}) == {
        "ramMb": missing.min_ram_mb,
        "ramMeasured": False,
        "measuredRtfx": None,
    }


@pytest.mark.parametrize(("wer", "level"), [(8.04, "good"), (8.06, "fair")])
def test_accuracy_level_uses_displayed_value(catalog: Catalog, wer: float, level: str) -> None:
    entry = replace(catalog.entries[0], metrics=Metrics(wer_ru=Metric(wer, "source")))
    row = entry_metrics(entry, 0)[0]
    assert row["level"] == level
    assert row["text"] == ("92,0 %" if wer == 8.04 else "91,9 %")


@pytest.mark.parametrize(("rtfx", "level"), [(19.96, "good"), (19.94, "fair")])
def test_speed_level_uses_displayed_value(catalog: Catalog, rtfx: float, level: str) -> None:
    entry = replace(catalog.entries[0], metrics=Metrics(rtfx=Metric(rtfx, "source")))
    row = entry_metrics(entry, 20)[1]
    assert row["level"] == level
    assert row["text"] == ("20,0× быстрее речи" if rtfx == 19.96 else "19,9× быстрее речи")
