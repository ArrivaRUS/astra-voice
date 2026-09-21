"""Установка настоящей GigaAM с пробным распознаванием установленной ревизии."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from astra_voice.core.model_source import SmokeRunner
from astra_voice.models.catalog import CatalogEntry, FileSpec
from astra_voice.models.installer import Installer
from astra_voice.models.store import ModelStore
from astra_voice.security.verify import sha256_file
from astra_voice.worker.engine import LAYOUTS, check_layout

pytestmark = pytest.mark.engine

LAYOUT = "onnx-asr-gigaam-v3"
VARIANT = "gigaam-v3-e2e-rnnt"
REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module", autouse=True)
def runtime() -> None:
    pytest.importorskip("onnxruntime", reason="Для engine-теста нужен onnxruntime")
    pytest.importorskip("onnx_asr", reason="Для engine-теста нужен onnx-asr")
    pytest.importorskip("numpy", reason="Для пробного распознавания нужен numpy")


@pytest.fixture(scope="module")
def model_dir() -> Path:
    directory = Path(
        os.environ.get(
            "ASTRA_VOICE_TEST_MODEL_DIR", "~/.cache/astra-voice-spike/gigaam-v3/e2e_rnnt"
        )
    ).expanduser()
    if not directory.is_dir():
        pytest.skip(f"Каталог модели GigaAM отсутствует: {directory}")
    for name in LAYOUTS[LAYOUT][VARIANT].required:
        if not (directory / name).is_file():
            pytest.skip(f"Отсутствует обязательный файл модели GigaAM: {directory / name}")
    return directory


def test_install_from_staging_with_real_worker(tmp_path: Path, model_dir: Path) -> None:
    from astra_voice.ui.model_downloads import make_smoke_check

    wav_path = REPO_ROOT / "data/smoke/smoke-ru.wav"
    if not wav_path.is_file():
        pytest.skip(f"Запись для пробного распознавания отсутствует: {wav_path}")
    revision = "real-model"
    spec = LAYOUTS[LAYOUT][VARIANT]
    files = tuple(
        FileSpec(
            path=name,
            sha256=sha256_file(model_dir / name),
            size=(model_dir / name).stat().st_size,
            url_path=f"/{revision}/{name}",
        )
        for name in spec.required + spec.optional
        if (model_dir / name).is_file()
    )
    entry = CatalogEntry(
        id=VARIANT,
        revision=revision,
        name="GigaAM v3 e2e RNN-T",
        description="Настоящие файлы модели для интеграционной проверки установки.",
        size_bytes=sum(file.size for file in files),
        min_ram_mb=1,
        layout=LAYOUT,
        variant=VARIANT,
        recommended=True,
        host="huggingface.co",
        files=files,
    )
    store = ModelStore(tmp_path / "models")
    staging = store.staging_dir(entry.id, entry.revision)
    for file in entry.files:
        shutil.copyfile(model_dir / file.path, staging / file.path)

    result = Installer(
        store, make_smoke_check(SmokeRunner(wav_path=wav_path))
    ).install_from_staging(entry)

    assert result.state == "ok", result.reason
    record = store.current()
    assert record is not None
    assert record.state == "ok"
    assert record == result.record
    assert not (record.dir / "state.json").exists()
    assert record.dir.with_name(f"{entry.revision}.json").is_file()
    check_layout(record.dir, LAYOUT, VARIANT)
