"""docs/PRIVACY.md говорит об источниках моделей то же, что встроенный каталог."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from astra_voice.models.downloader import _source_kind

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[2]
NO_MIRRORS = "Других источников моделей в этой версии нет."


def _download_section() -> str:
    text = (REPO / "docs/PRIVACY.md").read_text(encoding="utf-8")
    start = text.index("### 1. Скачивание модели")
    end = text.index("### 2. ", start)
    return " ".join(text[start:end].split())


def test_privacy_model_sources_match_builtin_catalog() -> None:
    catalog = json.loads((REPO / "data/catalog.json").read_text(encoding="utf-8"))
    hosts = {model["host"] for model in catalog["models"]}
    has_mirrors = any(model.get("mirrors") for model in catalog["models"])
    section = _download_section()
    assert {_source_kind(host) for host in hosts} == {"hf"}
    assert "Hugging Face" in section
    # Появилось зеркало (например, GitHub Releases) — раздел надо переписать.
    assert (NO_MIRRORS in section) is not has_mirrors
