"""Каталог и раскладки движка описывают одни и те же файлы (M6, блок 2).

Модели здесь не загружаются: весов двенадцати моделей на машине нет, а сверять
надо именно контракт «запись каталога ↔ VariantSpec», который иначе проверил бы
только пользователь после часовой загрузки.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from astra_voice.models.catalog import DIARIZATION_MODEL_IDS, CatalogEntry, _model, load_builtin
from astra_voice.security.verify import Verifier
from astra_voice.worker.engine import LAYOUTS, VariantSpec, make_engine

pytestmark = pytest.mark.engine

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_ROOT = REPO_ROOT / "data"


@pytest.fixture(scope="module")
def entries() -> tuple[CatalogEntry, ...]:
    catalog = load_builtin(
        Verifier("catalog", keyring=DATA_ROOT / "keys/release.gpg"), root=DATA_ROOT
    )
    return catalog.entries


def test_catalog_has_twelve_asr_and_two_diarization_models(
    entries: tuple[CatalogEntry, ...],
) -> None:
    assert Counter(entry.kind for entry in entries) == {"dictation": 12, "diarization": 2}
    assert {entry.id for entry in entries if entry.kind == "diarization"} == set(
        DIARIZATION_MODEL_IDS
    )


def test_every_entry_matches_its_layout(entries: tuple[CatalogEntry, ...]) -> None:
    raw_entries = {
        entry["id"]: entry
        for entry in json.loads((DATA_ROOT / "catalog.json").read_bytes())["models"]
    }
    for entry in entries:
        if entry.kind == "diarization":
            # The product's step 7a validator checks the exact layout/variant/files,
            # license and recommendation flag; these are not ASR engines.
            assert _model(raw_entries[entry.id]) == entry
            continue
        layout = entry.layout
        variant = entry.variant
        assert layout in LAYOUTS, (entry, layout)
        assert variant in LAYOUTS[layout], (entry, variant)
        spec = LAYOUTS[layout][variant]
        paths = {file.path for file in entry.files}
        assert paths == set(spec.required) | set(spec.optional), entry.id
        assert set(spec.required) <= paths, entry.id
        assert make_engine(layout) is not None


def test_every_layout_is_used_by_the_catalog(entries: tuple[CatalogEntry, ...]) -> None:
    """Неиспользуемая раскладка — мёртвый код, который никто не проверяет."""
    used = {(entry.layout, entry.variant) for entry in entries}
    declared = {(layout, variant) for layout, variants in LAYOUTS.items() for variant in variants}
    assert declared - used == set()


ALL_VARIANTS = [
    pytest.param(layout, variant, spec, id=f"{layout}/{variant}")
    for layout, variants in LAYOUTS.items()
    for variant, spec in variants.items()
]


def write_layout(model_dir: Path, names: tuple[str, ...]) -> Path:
    """Создаёт пустой каталог варианта, включая подкаталоги раскладок Vosk и Whisper."""
    model_dir.mkdir(parents=True, exist_ok=True)
    for name in names:
        target = model_dir / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"")
    return model_dir


@pytest.mark.parametrize(("layout", "variant", "spec"), ALL_VARIANTS)
def test_onnx_asr_finds_exactly_our_files(
    tmp_path: Path, layout: str, variant: str, spec: VariantSpec
) -> None:
    """onnx-asr должен найти в нашей раскладке ровно те файлы, что мы объявили.

    Модель не загружается: `resolve_model` только ищет файлы по шаблонам
    рантайма, сессии ORT не создаются и веса не нужны.
    """
    from onnx_asr.loader import create_asr_resolver  # type: ignore[import-not-found]

    directory = write_layout(tmp_path / "model", spec.required + spec.optional)
    resolver = create_asr_resolver(spec.onnx_asr_name, directory, offline=True)
    found = resolver.resolve_model(quantization=spec.quantization)
    names = {path.relative_to(directory).as_posix() for path in found.values()}
    assert names <= set(spec.required + spec.optional)
    assert names >= set(spec.required) - set(spec.optional)
