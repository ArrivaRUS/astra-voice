"""Подписи и полоски карточек: мост считает их по настоящему каталогу."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("jsonschema")

# ruff: noqa: E402
from astra_voice.models.catalog import Catalog, load_builtin
from astra_voice.security.verify import Verifier
from astra_voice.ui.formatting import format_rtfx, format_wer
from astra_voice.ui.model_downloads import catalog_best, entry_metrics, entry_tags

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
        "MIT · Сбер",
        "отечественная",
    ]


def test_tags_skip_what_the_model_does_not_have(catalog: Catalog) -> None:
    vosk = catalog.entry("vosk-small-ru-int8")
    assert vosk is not None
    tags = entry_tags(vosk)
    assert "с пунктуацией" not in tags
    assert "отечественная" not in tags
    assert tags == ["Только русский", "Apache-2.0 · Alpha Cephei"]


def test_metrics_fill_is_relative_to_the_best_in_catalog(catalog: Catalog) -> None:
    best_wer, best_rtfx = catalog_best(catalog.entries)
    assert best_wer == 4.39  # GigaAM v3 RNN-T без пунктуации
    assert best_rtfx == 83.5  # Vosk small ru
    entry = catalog.entries[0]
    quality, speed = entry_metrics(entry, best_wer, best_rtfx)
    assert quality == {
        "label": "Качество",
        "text": format_wer(7.6),
        "fill": pytest.approx(best_wer / 7.6),
        "hasData": True,
        "measured": False,
    }
    assert speed == {
        "label": "Скорость",
        "text": format_rtfx(42.5),
        "fill": pytest.approx(42.5 / best_rtfx),
        "hasData": True,
        "measured": False,
    }


def test_best_entries_fill_the_bar_completely(catalog: Catalog) -> None:
    best_wer, best_rtfx = catalog_best(catalog.entries)
    leader = catalog.entry("gigaam-v3-rnnt-int8")
    fastest = catalog.entry("vosk-small-ru-int8")
    assert leader is not None and fastest is not None
    assert entry_metrics(leader, best_wer, best_rtfx)[0]["fill"] == 1.0
    assert entry_metrics(fastest, best_wer, best_rtfx)[1]["fill"] == 1.0


def test_missing_numbers_are_not_invented(catalog: Catalog) -> None:
    """Whisper small не замерен по протоколу — полоски пустые, а не выдуманные."""
    best_wer, best_rtfx = catalog_best(catalog.entries)
    entry = catalog.entry("whisper-small-int8")
    assert entry is not None
    for row in entry_metrics(entry, best_wer, best_rtfx):
        assert row["hasData"] is False
        assert row["text"] == ""
        assert row["fill"] == 0.0


def test_speed_alone_is_enough_for_one_bar(catalog: Catalog) -> None:
    """У многоязычных есть WER, но нет int8-замера скорости: одна полоска из двух."""
    best_wer, best_rtfx = catalog_best(catalog.entries)
    entry = catalog.entry("gigaam-multilingual-ctc-int8")
    assert entry is not None
    quality, speed = entry_metrics(entry, best_wer, best_rtfx)
    assert quality["hasData"] is True
    assert speed["hasData"] is False


def test_every_card_has_two_bars_and_a_vendor(catalog: Catalog) -> None:
    best_wer, best_rtfx = catalog_best(catalog.entries)
    for entry in catalog.entries:
        rows = entry_metrics(entry, best_wer, best_rtfx)
        assert [row["label"] for row in rows] == ["Качество", "Скорость"]
        assert all(0.0 <= row["fill"] <= 1.0 for row in rows)
        assert all(row["measured"] is False for row in rows)
        assert entry.vendor
        assert entry_tags(entry)


def test_empty_catalog_has_no_best(catalog: Catalog) -> None:
    assert catalog_best(()) == (0.0, 0.0)


def test_formatters_use_russian_decimal_comma() -> None:
    assert format_wer(7.6) == "WER 7,60 %"
    assert format_rtfx(42.5) == "42,5× быстрее речи"
