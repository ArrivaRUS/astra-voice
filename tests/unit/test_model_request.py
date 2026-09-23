"""Построение model.load: приоритет источников, значения по умолчанию и пути."""

from __future__ import annotations

from pathlib import Path
from types import MappingProxyType
from typing import Any
from unittest.mock import Mock

import pytest

from astra_voice.core.model_request import ModelNotConfigured, build_model_load, model_threads
from astra_voice.core.model_source import resolve_model_request
from astra_voice.core.settings import from_dict
from astra_voice.models.store import ModelRecord, ModelStore

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("value", [None, "bad", "4", 0, -2, True, False])
def test_invalid_model_threads_use_default(value: object) -> None:
    assert model_threads({"model_threads": value}) == 2
    assert type(model_threads({"model_threads": value})) is int


def test_numeric_model_threads_are_int() -> None:
    assert model_threads({"model_threads": 4}) == 4
    assert type(model_threads({"model_threads": 4})) is int


@pytest.mark.parametrize("failure", ["recheck", "metadata"])
@pytest.mark.parametrize("configured", [False, True])
def test_resolver_rejects_unverified_store_record_even_if_current_returns_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str, configured: bool
) -> None:
    store = ModelStore(tmp_path / "models")
    record = ModelRecord(
        "stored-model",
        "r1",
        store.root / "stored-model/r1",
        "stored-layout",
        "stored-variant",
        123,
        recheck=failure == "recheck",
        metadata_ok=failure != "metadata",
    )
    current = Mock(return_value=record)
    monkeypatch.setattr(store, "current", current)
    settings = from_dict(
        {"model_id": "fallback-model", "model_revision": "r2"} if configured else {}
    )

    request = resolve_model_request(settings, store, store_dir=store.root)

    current.assert_called_once_with()
    if configured:
        assert request == build_model_load(settings.to_dict(), store_dir=store.root)
        assert request["id"] == "fallback-model"
    else:
        assert request is None


@pytest.mark.parametrize("prefix", ["model_", ""])
def test_arguments_override_settings(prefix: str) -> None:
    request = build_model_load(
        {
            f"{prefix}id": "saved-model",
            f"{prefix}revision": "saved-revision",
            f"{prefix}dir": "/saved/model",
            f"{prefix}variant": "saved-variant",
            f"{prefix}threads": 8,
        },
        model_dir="/explicit/model/r2",
        variant="explicit-variant",
        threads=4,
        store_dir=Path("/store"),
    )

    assert request["id"] == "model"
    assert request["revision"] == "r2"
    assert request["dir"] == "/explicit/model/r2"
    assert request["variant"] == "explicit-variant"
    assert request["threads"] == 4


@pytest.mark.parametrize(
    ("name", "preferred", "fallback"),
    [
        ("id", "preferred-model", "fallback-model"),
        ("revision", "r2", "r1"),
        ("dir", "/preferred/model", "/fallback/model"),
        ("layout", "preferred-layout", "fallback-layout"),
        ("variant", "preferred-variant", "fallback-variant"),
        ("threads", 4, 8),
        ("min_ram_mb", 1024, 2048),
    ],
)
@pytest.mark.parametrize("source", ["prefixed", "plain", "none"])
def test_settings_priority(name: str, preferred: Any, fallback: Any, source: str) -> None:
    settings = {"id": "model", "revision": "r1", name: fallback}
    if source != "plain":
        settings[f"model_{name}"] = preferred if source == "prefixed" else None

    request = build_model_load(MappingProxyType(settings), store_dir=Path("/store"))

    assert request[name] == (preferred if source == "prefixed" else fallback)


@pytest.mark.parametrize("unset", [{}, {"model_dir": None, "dir": None}, {"dir": ""}])
def test_defaults_and_store_directory(unset: dict[str, Any]) -> None:
    request = build_model_load(
        {"id": "model", "revision": "r1", **unset}, store_dir=Path("/custom/store")
    )

    assert request == {
        "type": "model.load",
        "id": "model",
        "revision": "r1",
        "dir": "/custom/store/model/r1",
        "layout": "onnx-asr-gigaam-v3",
        "variant": "gigaam-v3-e2e-rnnt",
        "threads": 2,
        "min_ram_mb": 768,
    }


@pytest.mark.parametrize(
    "settings",
    [
        {},
        {"id": "model"},
        {"revision": "r1"},
        {"id": "", "revision": "r1"},
        {"id": "model", "revision": ""},
        {"model_id": "", "id": "fallback", "revision": "r1"},
        {"dir": "/saved/model/r1"},
    ],
)
def test_model_not_configured(settings: dict[str, Any]) -> None:
    with pytest.raises(ModelNotConfigured) as caught:
        build_model_load(settings, store_dir=Path("/store"))

    assert isinstance(caught.value, ValueError)
    assert str(caught.value) == (
        "Модель не настроена. Укажите --model-dir или модель и ревизию в настройках."
    )


@pytest.mark.parametrize(
    ("directory", "model_id", "revision"),
    [
        ("/cache/model/r2", "model", "r2"),
        ("~/cache/model/r2", "model", "r2"),
        ("model", "local-model", "model"),
        ("/", "local-model", "local-revision"),
        (".", "local-model", "local-revision"),
        ("", "local-model", "local-revision"),
    ],
)
def test_explicit_directory_supplies_identity(directory: str, model_id: str, revision: str) -> None:
    request = build_model_load({}, model_dir=directory, store_dir=Path("/store"))

    assert request["id"] == model_id
    assert request["revision"] == revision
    assert request["dir"] == str(
        Path(directory or "/store/local-model/local-revision").expanduser()
    )


@pytest.mark.parametrize("source", ["argument", "model_dir", "dir"])
def test_directory_expands_home(monkeypatch: pytest.MonkeyPatch, source: str) -> None:
    monkeypatch.setenv("HOME", "/home/model-user")
    settings = {"id": "model", "revision": "r1"}
    directory = "~/models/../model/r1"
    if source != "argument":
        settings[source] = directory

    request = build_model_load(
        settings,
        model_dir=directory if source == "argument" else None,
        store_dir=Path("/store"),
    )

    assert request["dir"] == "/home/model-user/models/../model/r1"


def test_only_none_is_skipped() -> None:
    request = build_model_load(
        {
            "id": "model",
            "revision": "r1",
            "model_layout": None,
            "layout": None,
            "model_variant": "saved-variant",
            "model_threads": 8,
            "model_min_ram_mb": 0,
            "min_ram_mb": 1024,
        },
        variant="",
        threads=0,
        store_dir=Path("/store"),
    )

    assert request["layout"] == "onnx-asr-gigaam-v3"
    assert request["variant"] == ""
    assert request["threads"] == 0
    assert request["min_ram_mb"] == 0
