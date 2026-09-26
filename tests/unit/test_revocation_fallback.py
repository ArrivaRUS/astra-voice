"""Проверка отзыва сохраняется при отказе каталога."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock

import pytest

from astra_voice import app
from astra_voice.core import model_source, paths
from astra_voice.core.settings import Settings, from_dict
from astra_voice.models import catalog_state
from astra_voice.models.catalog import CatalogError, load_builtin
from astra_voice.runtime import DictationRuntime
from astra_voice.security.verify import Verifier
from astra_voice.ui.bridges import SettingsBridge
from astra_voice.ui.model_downloads import ModelDownloads

pytestmark = pytest.mark.unit


def _settings() -> Settings:
    return from_dict({"model_id": "model", "model_revision": "r1"})


def _newer_epoch() -> int:
    catalog = Path(__file__).resolve().parents[2] / "data/catalog.json"
    return int(json.loads(catalog.read_text(encoding="utf-8"))["trust_epoch"]) + 1


def test_broken_catalog_blocks_snapshot_revocation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(paths, "state_dir", lambda: tmp_path)
    catalog_state.write_state(
        catalog_state.state_path(tmp_path),
        catalog_state.CatalogState(_newer_epoch(), 3, "a" * 64, (("model", "r1"),)),
    )
    data_root = Path(__file__).resolve().parents[2] / "data"
    with pytest.raises(CatalogError, match="устарел"):
        load_builtin(
            Verifier("catalog", keyring=data_root / "keys/release.gpg"),
            root=data_root,
            state_path=catalog_state.state_path(tmp_path),
        )
    checker = app._revoked_check(None)
    with pytest.raises(model_source.ModelRevoked):
        model_source.resolve_model_request(_settings(), store_dir=tmp_path, revoked=checker)


def test_no_snapshot_loads_and_warns_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(paths, "state_dir", lambda: tmp_path)
    callback = Mock()
    runtime = cast(
        DictationRuntime,
        SimpleNamespace(revocation_unknown=False, on_revocation_unknown=callback),
    )
    checker = app._revoked_check(None)
    with caplog.at_level(logging.WARNING, logger="astra_voice"):
        for _ in range(2):
            request = model_source.resolve_model_request(
                _settings(),
                store_dir=tmp_path,
                revoked=checker,
                on_revocation_unknown=lambda: DictationRuntime._mark_revocation_unknown(runtime),
            )
            assert request is not None
    assert runtime.revocation_unknown is True
    callback.assert_called_once_with()
    messages = [
        row.message
        for row in caplog.records
        if row.levelno == logging.WARNING and "отозванных версий модели" in row.message
    ]
    assert messages == ["Не удалось проверить список отозванных версий модели"]


def test_catalog_checker_exception_uses_snapshot(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(paths, "state_dir", lambda: tmp_path)
    catalog_state.write_state(
        catalog_state.state_path(tmp_path),
        catalog_state.CatalogState(_newer_epoch(), 3, "a" * 64, (("model", "r1"),)),
    )
    model = Mock()
    model.revoked_revision.side_effect = CatalogError("bad-schema", "broken")
    checker = app._revoked_check(model)
    with pytest.raises(model_source.ModelRevoked):
        model_source.resolve_model_request(_settings(), store_dir=tmp_path, revoked=checker)
    model.revoked_revision.assert_called_once_with("model", "r1")


@pytest.mark.parametrize("has_snapshot", [False, True])
def test_unavailable_catalog_exposes_unknown_to_models_bridge(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, has_snapshot: bool
) -> None:
    monkeypatch.setattr(paths, "state_dir", lambda: tmp_path)
    if has_snapshot:
        catalog_state.write_state(
            catalog_state.state_path(tmp_path),
            catalog_state.CatalogState(_newer_epoch(), 3, "a" * 64, ()),
        )
    runtime = cast(
        DictationRuntime,
        SimpleNamespace(revocation_unknown=False, on_revocation_unknown=None),
    )
    downloads = ModelDownloads(None, revocation_unknown=lambda: runtime.revocation_unknown)
    runtime.on_revocation_unknown = downloads.revocation_unknown_changed
    bridge = SettingsBridge(Settings(), downloads=downloads, save=Mock())
    changed = Mock()
    bridge.revocationUnknownChanged.connect(changed)
    try:
        request = model_source.resolve_model_request(
            _settings(),
            store_dir=tmp_path,
            revoked=app._revoked_check(None),
            on_revocation_unknown=lambda: DictationRuntime._mark_revocation_unknown(runtime),
        )
        assert request is not None
        assert bridge.models == []
        assert bridge.revocationUnknown is not has_snapshot
        assert changed.call_count == (0 if has_snapshot else 1)
    finally:
        downloads.shutdown()
