"""Параметры отладочной расшифровки без модели, дисплея и запуска воркера."""

from __future__ import annotations

from pathlib import Path

import pytest

from astra_voice.app import _debug_model_request, _parse_args

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("prefix", ["model_", ""])
def test_cli_overrides_settings(prefix: str) -> None:
    """Явные аргументы перекрывают оба варианта имён настроек."""
    settings = {
        f"{prefix}id": "old-model",
        f"{prefix}revision": "old-revision",
        f"{prefix}dir": "/old/model",
        f"{prefix}variant": "old-variant",
        f"{prefix}threads": 8,
    }
    args = _parse_args(
        [
            "--debug-transcribe",
            "test.wav",
            "--model-dir",
            "/cache/gigaam-v3/e2e_rnnt",
            "--variant",
            "gigaam-v3-e2e-rnnt",
            "--threads",
            "1",
        ]
    )

    request = _debug_model_request(settings, args, Path("/store"))

    assert args.debug_transcribe == "test.wav"
    assert request == {
        "type": "model.load",
        "id": "gigaam-v3",
        "revision": "e2e_rnnt",
        "dir": "/cache/gigaam-v3/e2e_rnnt",
        "layout": "onnx-asr-gigaam-v3",
        "variant": "gigaam-v3-e2e-rnnt",
        "threads": 1,
        "min_ram_mb": 768,
    }


@pytest.mark.parametrize("prefix", ["model_", ""])
def test_settings_override_defaults(prefix: str) -> None:
    """Без аргументов используются сохранённые параметры и каталог хранилища."""
    request = _debug_model_request(
        {
            f"{prefix}id": "gigaam",
            f"{prefix}revision": "r1",
            f"{prefix}variant": "saved-variant",
            f"{prefix}threads": 4,
        },
        _parse_args([]),
        Path("/store"),
    )

    assert request["dir"] == "/store/gigaam/r1"
    assert request["variant"] == "saved-variant"
    assert request["threads"] == 4


@pytest.mark.parametrize(
    ("directory", "model_id", "revision"),
    [
        ("/store/gigaam/r1", "gigaam", "r1"),
        ("~/.cache/astra-voice-spike/gigaam-v3/e2e_rnnt", "gigaam-v3", "e2e_rnnt"),
        ("model", "local-model", "model"),
        ("/", "local-model", "local-revision"),
        (".", "local-model", "local-revision"),
    ],
)
def test_model_dir_without_configured_model(directory: str, model_id: str, revision: str) -> None:
    """Каталог достаточен для запроса даже с пустыми настройками модели."""
    request = _debug_model_request(
        {"model_id": None}, _parse_args(["--model-dir", directory]), Path("/store")
    )

    assert request["id"] == model_id
    assert request["revision"] == revision
    assert request["dir"] == directory
    assert request["variant"] == "gigaam-v3-e2e-rnnt"
    assert request["threads"] == 2


def test_missing_model_still_rejected() -> None:
    with pytest.raises(ValueError, match="Модель не настроена"):
        _debug_model_request({"model_id": None}, _parse_args([]), Path("/store"))


@pytest.mark.parametrize("value", ["0", "-1", "1.5", "не-число"])
def test_invalid_threads(value: str, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as caught:
        _parse_args(["--threads", value])

    assert caught.value.code == 2
    assert "число потоков должно быть целым и не меньше 1" in capsys.readouterr().err


def test_help_describes_model_options(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as caught:
        _parse_args(["--help"])

    assert caught.value.code == 0
    help_text = capsys.readouterr().out
    for option in ("--model-dir PATH", "--variant NAME", "--threads N"):
        assert option in help_text
