"""Раскладки каталога моделей: состав файлов, подкаталоги и имена onnx-asr."""

from __future__ import annotations

from pathlib import Path

import pytest

from astra_voice.worker.engine import (
    LAYOUTS,
    ExtraFileError,
    ModelMissingError,
    VariantSpec,
    check_layout,
    layout_files,
    make_engine,
)

pytestmark = pytest.mark.unit

ALL_VARIANTS = [
    pytest.param(layout, variant, spec, id=f"{layout}/{variant}")
    for layout, variants in LAYOUTS.items()
    for variant, spec in variants.items()
]
NESTED = ("onnx-asr-vosk", "vosk-am-onnx")


def write_layout(model_dir: Path, names: tuple[str, ...]) -> Path:
    """Создаёт каталог варианта, включая подкаталоги раскладки Vosk и Whisper."""
    model_dir.mkdir(parents=True, exist_ok=True)
    for name in names:
        target = model_dir / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"")
    return model_dir


@pytest.mark.parametrize(("layout", "variant", "spec"), ALL_VARIANTS)
@pytest.mark.parametrize("with_optional", [False, True], ids=["без-необязательных", "полный"])
def test_layout_accepts_its_own_files(
    tmp_path: Path, layout: str, variant: str, spec: VariantSpec, with_optional: bool
) -> None:
    names = spec.required + (spec.optional if with_optional else ())
    directory = write_layout(tmp_path / "model", names)
    check_layout(directory, layout, variant)
    assert set(layout_files(directory, layout, variant)) == set(names)
    assert make_engine(layout) is not None


@pytest.mark.parametrize(("layout", "variant", "spec"), ALL_VARIANTS)
def test_layout_rejects_extra_file(
    tmp_path: Path, layout: str, variant: str, spec: VariantSpec
) -> None:
    directory = write_layout(tmp_path / "model", spec.required)
    (directory / "лишний.bin").write_bytes(b"")
    with pytest.raises(ExtraFileError) as failure:
        check_layout(directory, layout, variant)
    assert failure.value.code == "extra-file"
    assert str(tmp_path) not in str(failure.value)


@pytest.mark.parametrize(("layout", "variant", "spec"), ALL_VARIANTS)
def test_layout_reports_missing_file(
    tmp_path: Path, layout: str, variant: str, spec: VariantSpec
) -> None:
    directory = write_layout(tmp_path / "model", spec.required[1:])
    with pytest.raises(ModelMissingError) as failure:
        check_layout(directory, layout, variant)
    assert failure.value.code == "model-missing"
    assert str(tmp_path) not in str(failure.value)


def test_nested_layout_rejects_extra_file_in_subdirectory(tmp_path: Path) -> None:
    layout, variant = NESTED
    directory = write_layout(tmp_path / "model", LAYOUTS[layout][variant].required)
    (directory / "am-onnx" / "encoder.onnx").write_bytes(b"")
    with pytest.raises(ExtraFileError):
        check_layout(directory, layout, variant)


def test_nested_layout_rejects_unexpected_subdirectory(tmp_path: Path) -> None:
    layout, variant = NESTED
    directory = write_layout(tmp_path / "model", LAYOUTS[layout][variant].required)
    (directory / "lm").mkdir()
    with pytest.raises(ExtraFileError):
        check_layout(directory, layout, variant)


def test_nested_layout_rejects_symlink_in_subdirectory(tmp_path: Path) -> None:
    layout, variant = NESTED
    directory = write_layout(tmp_path / "model", LAYOUTS[layout][variant].required)
    target = directory / "am-onnx" / "encoder.int8.onnx"
    target.unlink()
    outside = tmp_path / "encoder.int8.onnx"
    outside.write_bytes(b"")
    target.symlink_to(outside)
    with pytest.raises(ExtraFileError):
        check_layout(directory, layout, variant)


def test_nested_layout_reports_missing_file_in_subdirectory(tmp_path: Path) -> None:
    layout, variant = NESTED
    directory = write_layout(tmp_path / "model", LAYOUTS[layout][variant].required)
    (directory / "lang" / "tokens.txt").unlink()
    with pytest.raises(ModelMissingError):
        check_layout(directory, layout, variant)


def test_variant_names_are_unique_across_layouts() -> None:
    """Вариант принадлежит ровно одной раскладке: иначе каталог не разобрать."""
    owners: dict[str, str] = {}
    for layout, variants in LAYOUTS.items():
        for variant in variants:
            assert variant not in owners, (variant, owners.get(variant), layout)
            owners[variant] = layout


def test_only_t_one_runs_without_quantization() -> None:
    """int8-весов нет только у T-one; остальным раскладкам нужен int8."""
    without = {
        (layout, variant)
        for layout, variants in LAYOUTS.items()
        for variant, spec in variants.items()
        if spec.quantization is None
    }
    assert without == {("onnx-asr-t-one", "t-one-ctc")}
