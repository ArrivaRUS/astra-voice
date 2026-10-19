"""Б-1: общий каталог/отзывы после выбора источника и после перезапуска."""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import logging
import stat
import tempfile
import threading
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

from astra_voice.core import paths
from astra_voice.core.policy import Policy
from astra_voice.core.settings import Settings
from astra_voice.models import catalog_effective, catalog_state
from astra_voice.models.catalog import Catalog, CatalogEntry, CatalogError
from astra_voice.models.catalog_effective import load_effective, state_of, verify_catalog
from astra_voice.models.installer import Installer, SmokeResult
from astra_voice.models.store import ModelStore, StoreError
from astra_voice.net import update_cache
from astra_voice.net.update_cache import write_private
from astra_voice.security.verify import Verifier, VerifyResult
from astra_voice.updates import catalog_updates

pytestmark = pytest.mark.unit
DATA_ROOT = Path(__file__).resolve().parents[2] / "data"
MODEL_ID = "gigaam-v3-e2e-ctc-int8"
NEW_REVISION = "99052dc2665168ae285e5ec3e697ea56137b069f"
CONTENTS = {
    "v3_e2e_ctc.int8.onnx": b"\x08\x03\x12\x04test",
    "v3_e2e_ctc_vocab.txt": "а\nб\n".encode(),
    "config.json": b'{"model_type":"gigaam"}',
}


def signature(raw: bytes) -> bytes:
    """Тестовый подписант связывает результат проверки с точными байтами."""
    return b"test-signature:" + hashlib.sha256(raw).digest()


class BoundVerifier(Verifier):
    """Без gpg/ключей; повреждённая пара действительно получает отказ."""

    def __init__(self) -> None:
        super().__init__("catalog", keyring=DATA_ROOT / "keys" / "release.gpg")
        self.calls = 0

    def verify_detached(
        self, data: Path, sig: Path, *, cancel: Callable[[], bool] | None = None
    ) -> VerifyResult:
        assert data.is_absolute() and sig.is_absolute()
        self.calls += 1
        return VerifyResult(ok=sig.read_bytes() == signature(data.read_bytes()))


@dataclass
class Scenario:
    root: Path
    snapshots: Path
    state: Path
    document: dict[str, Any]
    verifier: BoundVerifier

    def builtin(self, document: dict[str, Any] | None = None) -> bytes:
        raw = json.dumps(self.document if document is None else document).encode()
        (self.root / "catalog.json").write_bytes(raw)
        (self.root / "catalog.json.sig").write_bytes(signature(raw))
        return raw

    def snapshot(self, document: dict[str, Any], *, valid_signature: bool = True) -> bytes:
        raw = json.dumps(document).encode()
        self.snapshot_bytes(raw, signature(raw) if valid_signature else b"bad signature")
        return raw

    def snapshot_bytes(self, raw: bytes, sig: bytes) -> None:
        write_private(
            self.snapshots / "catalog-snapshot.json",
            json.dumps(
                {
                    "raw": base64.b64encode(raw).decode("ascii"),
                    "signature": base64.b64encode(sig).decode("ascii"),
                }
            ).encode(),
        )

    def load(self) -> Catalog:
        return load_effective(
            self.verifier,
            builtin_root=self.root,
            snapshot_dir=self.snapshots,
            state_path=self.state,
            require_schema=True,
        )

    def remote(self, *, serial: int = 5, epoch: int = 1) -> dict[str, Any]:
        document = copy.deepcopy(self.document)
        document["serial"], document["trust_epoch"] = serial, epoch
        return document


@pytest.fixture(autouse=True)
def reset_snapshot_caches(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(catalog_effective, "_snapshot_cache", None)
    monkeypatch.setattr(catalog_effective, "_snapshot_rejection_cache", None)


@pytest.fixture
def scenario(tmp_path: Path) -> Scenario:
    root = tmp_path / "builtin"
    root.mkdir()
    (root / "catalog.schema.json").write_bytes((DATA_ROOT / "catalog.schema.json").read_bytes())
    document = json.loads((DATA_ROOT / "catalog.json").read_bytes())
    entry = next(item for item in document["models"] if item["id"] == MODEL_ID)
    entry["recommended"] = True
    entry["size_bytes"] = sum(map(len, CONTENTS.values()))
    entry["min_ram_mb"] = 1
    entry["files"] = [
        {
            "path": name,
            "size": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "url_path": f"/istupakov/gigaam-v3-onnx/resolve/{entry['revision']}/{name}",
        }
        for name, raw in CONTENTS.items()
    ]
    document["models"], document["revoked"] = [entry], []
    document["trust_epoch"], document["serial"] = 1, 4
    case = Scenario(
        root,
        tmp_path / "state" / "model-catalog-updates",
        tmp_path / "state" / "catalog-state.json",
        document,
        BoundVerifier(),
    )
    case.builtin()
    return case


@pytest.fixture
def source(scenario: Scenario) -> catalog_updates.CatalogUpdateSource:
    from astra_voice.net.gate import NetworkGate
    from astra_voice.net.http import HttpClient

    return catalog_updates.CatalogUpdateSource(
        HttpClient(
            NetworkGate(Settings(check_model_updates=True), Policy()),
            user_agent="catalog-recovery-test",
        ),
        baseline=scenario.load,
        catalog_url="https://models.example.ru/catalog.json",
        verifier=scenario.verifier,
        schema_root=scenario.root,
        state_dir=scenario.snapshots,
        global_state_path=scenario.state,
    )


def test_newer_remote_is_applied_and_reverified_after_restart(scenario: Scenario) -> None:
    remote = scenario.remote()
    remote["revoked"] = [
        {"model_id": MODEL_ID, "revision": remote["models"][0]["revision"], "reason": "отзыв"}
    ]
    raw = scenario.snapshot(remote)
    accepted = scenario.load()
    assert accepted.serial == 5
    assert catalog_state.read_state(scenario.state) == state_of(accepted, raw)
    assert stat.S_IMODE(scenario.state.stat().st_mode) == 0o600
    assert stat.S_IMODE((scenario.snapshots / "catalog-snapshot.json").stat().st_mode) == 0o600
    # Новый экземпляр проверяющего не имеет принятого каталога в памяти.
    scenario.verifier = BoundVerifier()
    assert scenario.load() == accepted
    assert scenario.verifier.calls == 2
    assert not (scenario.snapshots / "catalog-state.json").exists()


@pytest.mark.parametrize("epoch,serial", [(1, 6), (2, 1)])
def test_newer_builtin_wins_and_updates_global_state(
    scenario: Scenario, epoch: int, serial: int
) -> None:
    scenario.snapshot(scenario.remote())
    assert scenario.load().serial == 5
    newer = scenario.remote(epoch=epoch, serial=serial)
    raw = scenario.builtin(newer)
    selected = scenario.load()
    assert (selected.trust_epoch, selected.serial) == (epoch, serial)
    assert catalog_state.read_state(scenario.state) == state_of(selected, raw)


def test_equal_pair_different_bytes_uses_builtin_and_warns(
    scenario: Scenario, caplog: pytest.LogCaptureFixture
) -> None:
    remote = scenario.remote(serial=4)
    remote["models"][0]["description"] = "Другие подписанные байты"
    scenario.snapshot(remote)
    with caplog.at_level(logging.WARNING, logger=catalog_effective.__name__):
        selected = scenario.load()
    assert selected.entries[0].description == scenario.document["models"][0]["description"]
    assert catalog_state.read_state(scenario.state) == state_of(
        selected, (scenario.root / "catalog.json").read_bytes()
    )
    assert "другой SHA256" in caplog.text


def test_equal_pair_same_bytes_is_allowed(scenario: Scenario) -> None:
    scenario.snapshot(scenario.document)
    first = scenario.load()
    before = scenario.state.read_bytes()
    assert scenario.load() == first
    assert scenario.state.read_bytes() == before


def test_bad_signature_uses_builtin_with_compatible_watermark(
    scenario: Scenario, caplog: pytest.LogCaptureFixture
) -> None:
    builtin = scenario.load()
    before = scenario.state.read_bytes()
    scenario.snapshot(scenario.remote(), valid_signature=False)
    with caplog.at_level(logging.WARNING, logger=catalog_effective.__name__):
        assert scenario.load() == builtin
    assert scenario.state.read_bytes() == before
    assert "не прошёл проверку" in caplog.text


@pytest.mark.parametrize("payload", [b"{", b"[]", b'{"raw":"!","signature":"!"}'])
def test_corrupt_snapshot_warns_and_keeps_builtin(
    scenario: Scenario, payload: bytes, caplog: pytest.LogCaptureFixture
) -> None:
    write_private(scenario.snapshots / "catalog-snapshot.json", payload)
    with caplog.at_level(logging.WARNING, logger=catalog_effective.__name__):
        assert scenario.load().serial == 4
    assert "снимок каталога" in caplog.text


def test_both_catalogs_below_watermark_are_stale(scenario: Scenario) -> None:
    scenario.snapshot(scenario.remote(serial=6))
    assert scenario.load().serial == 6
    before = scenario.state.read_bytes()
    scenario.snapshot(scenario.remote(serial=5))
    with pytest.raises(CatalogError, match="устарел") as refused:
        scenario.load()
    assert refused.value.code == "stale-catalog"
    assert scenario.state.read_bytes() == before


@pytest.mark.parametrize("bad_signature", [False, True])
@pytest.mark.parametrize("serial", [4, 5, 6])
def test_missing_or_bad_snapshot_cannot_erase_remote_watermark(
    scenario: Scenario,
    source: catalog_updates.CatalogUpdateSource,
    monkeypatch: pytest.MonkeyPatch,
    bad_signature: bool,
    serial: int,
) -> None:
    scenario.snapshot(scenario.remote())
    scenario.load()
    before = scenario.state.read_bytes()
    if bad_signature:
        scenario.snapshot(scenario.remote(), valid_signature=False)
    else:
        (scenario.snapshots / "catalog-snapshot.json").unlink()
    with pytest.raises(CatalogError) as refused:
        scenario.load()
    assert refused.value.code == "stale-catalog"
    assert scenario.state.read_bytes() == before
    raw = json.dumps(scenario.remote(serial=serial)).encode()
    fetch = Mock(return_value=(raw, signature(raw)))
    monkeypatch.setattr(catalog_updates, "fetch", fetch)
    if serial < 5:
        with pytest.raises(CatalogError) as refused:
            source.refresh(threading.Event())
        assert refused.value.code == "stale-catalog"
        assert scenario.state.read_bytes() == before
    else:
        source.refresh(threading.Event())
        assert scenario.load().serial == serial
        assert source.current().serial == serial
    fetch.assert_called_once()


@pytest.mark.parametrize("payload", [b"{}", b"{", b"[]"])
def test_invalid_marker_is_never_repaired_locally(scenario: Scenario, payload: bytes) -> None:
    scenario.load()
    scenario.snapshot(scenario.remote())
    scenario.state.write_bytes(payload)
    with pytest.raises(CatalogError) as refused:
        scenario.load()
    assert refused.value.code == "state-invalid"
    assert scenario.state.read_bytes() == payload


@pytest.mark.parametrize("keep_snapshot", [False, True])
@pytest.mark.parametrize("serial", [3, 4, 5, 6])
def test_invalid_marker_recovery_preserves_all_known_bounds(
    scenario: Scenario,
    source: catalog_updates.CatalogUpdateSource,
    monkeypatch: pytest.MonkeyPatch,
    keep_snapshot: bool,
    serial: int,
) -> None:
    remote = scenario.remote()
    entry = remote["models"][0]
    remote["revoked"] = [
        {"model_id": entry["id"], "revision": entry["revision"], "reason": "отзыв"}
    ]
    scenario.snapshot(remote)
    assert source.current().serial == 5
    scenario.state.write_bytes(b"{}")
    if not keep_snapshot:
        (scenario.snapshots / "catalog-snapshot.json").unlink()
    with pytest.raises(CatalogError) as refused:
        source.current()
    assert refused.value.code == "state-invalid"
    assert scenario.state.read_bytes() == b"{}"
    remote["serial"] = serial
    raw = json.dumps(remote).encode()
    monkeypatch.setattr(catalog_updates, "fetch", Mock(return_value=(raw, signature(raw))))
    if serial < 5:
        with pytest.raises(CatalogError) as refused:
            source.refresh(threading.Event())
        assert refused.value.code == "stale-catalog"
        assert scenario.state.read_bytes() == b"{}"
    else:
        source.refresh(threading.Event())
        assert source.current().serial == serial
        assert source.current().is_revoked(entry["id"], entry["revision"])


def test_remote_snapshot_requires_schema_when_builtin_allows_missing_module(
    scenario: Scenario,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from astra_voice.models import schema

    remote = scenario.remote()
    remote["manifest_version"] = 999
    scenario.snapshot(remote)
    monkeypatch.setattr(schema, "validate", Mock(side_effect=schema.SchemaUnavailable))
    selected = load_effective(
        scenario.verifier,
        builtin_root=scenario.root,
        snapshot_dir=scenario.snapshots,
        state_path=scenario.state,
        require_schema=False,
    )
    assert selected.serial == 4


def test_snapshot_write_failure_does_not_block_current(
    scenario: Scenario,
    source: catalog_updates.CatalogUpdateSource,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    accepted = source.current()
    before = scenario.state.read_bytes()
    raw = json.dumps(scenario.remote()).encode()
    monkeypatch.setattr(catalog_updates, "fetch", Mock(return_value=(raw, signature(raw))))
    monkeypatch.setattr(
        update_cache,
        "write_private",
        Mock(side_effect=OSError("disk full")),
    )
    with pytest.raises(CatalogError) as refused:
        source.refresh(threading.Event())
    assert refused.value.code == "state-write"
    assert scenario.state.read_bytes() == before
    assert source.current() == accepted


def test_snapshot_failure_after_marker_repair_clears_previous_block(
    scenario: Scenario,
    source: catalog_updates.CatalogUpdateSource,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source._baseline = source.current()
    raw = json.dumps(scenario.remote()).encode()
    monkeypatch.setattr(catalog_updates, "fetch", Mock(return_value=(raw, signature(raw))))
    # T-233: снимок новее отметки теперь остаётся только при двойном сбое.
    real_unlink = Path.unlink

    def fail_snapshot_removal(path: Path, missing_ok: bool = False) -> None:
        if path == source._snapshot:
            raise OSError("сбой удаления снимка при откате")
        real_unlink(path, missing_ok=missing_ok)

    with monkeypatch.context() as failed_marker:
        failed_marker.setattr(catalog_state, "write_state", Mock(side_effect=OSError("disk full")))
        failed_marker.setattr(Path, "unlink", fail_snapshot_removal)
        with pytest.raises(CatalogError) as refused:
            source.refresh(threading.Event())
        assert refused.value.code == "state-write"
    assert source._blocked
    raw = json.dumps(scenario.remote(serial=6)).encode()
    monkeypatch.setattr(catalog_updates, "fetch", Mock(return_value=(raw, signature(raw))))
    monkeypatch.setattr(update_cache, "write_private", Mock(side_effect=OSError("disk full")))
    with pytest.raises(CatalogError) as refused:
        source.refresh(threading.Event())
    assert refused.value.code == "state-write"
    assert source.current().serial == 5
    marker = catalog_state.read_state(scenario.state)
    assert marker is not None and marker.serial == 5
    assert not source._blocked


@pytest.mark.parametrize("invalid_marker", [False, True])
@pytest.mark.parametrize("failure", ["offline", "network", "signature", "same-pair-conflict"])
def test_recovery_failure_keeps_marker_bytes(
    scenario: Scenario,
    source: catalog_updates.CatalogUpdateSource,
    monkeypatch: pytest.MonkeyPatch,
    invalid_marker: bool,
    failure: str,
) -> None:
    from astra_voice.net.http import NetworkError

    scenario.snapshot(scenario.remote())
    source.current()
    (scenario.snapshots / "catalog-snapshot.json").unlink()
    if invalid_marker:
        scenario.state.write_bytes(b"{}")
    before = scenario.state.read_bytes()
    remote = scenario.remote()
    if failure == "same-pair-conflict":
        remote["models"][0]["description"] = "Другой SHA"
    raw = json.dumps(remote).encode()
    fetch = Mock(return_value=(raw, b"bad" if failure == "signature" else signature(raw)))
    monkeypatch.setattr(catalog_updates, "fetch", fetch)
    if failure == "offline":
        monkeypatch.setattr(source._client._gate, "refusal", lambda *_args: "offline")
    if failure == "network":
        fetch.side_effect = NetworkError("no-network", "Недоступно")
    with pytest.raises((CatalogError, NetworkError)):
        source.refresh(threading.Event())
    assert scenario.state.read_bytes() == before
    assert not (scenario.snapshots / "catalog-snapshot.json").exists()
    assert fetch.call_count == (0 if failure == "offline" else 1)


@pytest.mark.parametrize("keep_snapshot", [False, True])
@pytest.mark.parametrize("serial", [3, 4, 5])
def test_invalid_marker_after_restart_uses_surviving_verified_bounds(
    scenario: Scenario,
    source: catalog_updates.CatalogUpdateSource,
    monkeypatch: pytest.MonkeyPatch,
    keep_snapshot: bool,
    serial: int,
) -> None:
    scenario.snapshot(scenario.remote())
    scenario.load()
    scenario.state.write_bytes(b"{}")
    if not keep_snapshot:
        (scenario.snapshots / "catalog-snapshot.json").unlink()
    raw = json.dumps(scenario.remote(serial=serial)).encode()
    monkeypatch.setattr(catalog_updates, "fetch", Mock(return_value=(raw, signature(raw))))
    if serial < (5 if keep_snapshot else 4):
        with pytest.raises(CatalogError) as refused:
            source.refresh(threading.Event())
        assert refused.value.code == "stale-catalog"
        assert scenario.state.read_bytes() == b"{}"
    else:
        source.refresh(threading.Event())
        assert source.current().serial == serial


def test_recovery_rechecks_marker_advanced_during_fetch(
    scenario: Scenario,
    source: catalog_updates.CatalogUpdateSource,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario.snapshot(scenario.remote())
    scenario.load()
    (scenario.snapshots / "catalog-snapshot.json").unlink()

    def fetch(*_args: Any, **_kwargs: Any) -> tuple[bytes, bytes]:
        scenario.snapshot(scenario.remote(serial=7))
        scenario.load()
        raw = json.dumps(scenario.remote(serial=6)).encode()
        return raw, signature(raw)

    monkeypatch.setattr(catalog_updates, "fetch", fetch)
    with pytest.raises(CatalogError) as refused:
        source.refresh(threading.Event())
    assert refused.value.code == "stale-catalog"
    assert scenario.load().serial == 7


def test_equal_pair_conflict_does_not_override_accepted_sha(
    scenario: Scenario, caplog: pytest.LogCaptureFixture
) -> None:
    remote = scenario.remote()
    scenario.snapshot(remote)
    scenario.load()
    before = scenario.state.read_bytes()
    builtin = scenario.remote()
    builtin["models"][0]["description"] = "Новый встроенный с занятой парой"
    scenario.builtin(builtin)
    with caplog.at_level(logging.WARNING, logger=catalog_effective.__name__):
        selected = scenario.load()
    assert selected.entries[0].description == remote["models"][0]["description"]
    assert scenario.state.read_bytes() == before
    assert "другой SHA256" in caplog.text


def test_equal_pair_conflict_keeps_accepted_builtin(
    scenario: Scenario, caplog: pytest.LogCaptureFixture
) -> None:
    builtin = scenario.load()
    before = scenario.state.read_bytes()
    remote = scenario.remote(serial=builtin.serial)
    remote["models"][0]["description"] = "Иной SHA при принятом встроенном"
    scenario.snapshot(remote)
    with caplog.at_level(logging.WARNING, logger=catalog_effective.__name__):
        assert scenario.load() == builtin
    assert scenario.state.read_bytes() == before
    assert "другой SHA256" in caplog.text


def test_equal_pair_conflict_with_neither_sha_accepted_is_closed(scenario: Scenario) -> None:
    scenario.load()
    before = scenario.state.read_bytes()
    builtin = scenario.remote(serial=4)
    builtin["models"][0]["description"] = "Второй SHA"
    scenario.builtin(builtin)
    remote = scenario.remote(serial=4)
    remote["models"][0]["description"] = "Третий SHA"
    scenario.snapshot(remote)
    with pytest.raises(CatalogError) as refused:
        scenario.load()
    assert refused.value.code == "stale-catalog"
    assert scenario.state.read_bytes() == before


@pytest.mark.parametrize("builtin_serial", [4, 5, 6], ids=["older", "equal", "newer"])
def test_schema_upgrade_preserves_watermark_and_revocations(
    scenario: Scenario, builtin_serial: int
) -> None:
    remote = scenario.remote(serial=5)
    remote["revoked"] = [
        {"model_id": MODEL_ID, "revision": remote["models"][0]["revision"], "reason": "отзыв"}
    ]
    scenario.snapshot(remote)
    scenario.load()
    before = scenario.state.read_bytes()
    old_marker = catalog_state.read_state(scenario.state)
    assert old_marker is not None
    schema_path = scenario.root / "catalog.schema.json"
    new_schema = schema_path.read_bytes() + b"\n"
    schema_path.write_bytes(new_schema)
    builtin = scenario.remote(serial=builtin_serial)
    builtin["schema_sha256"] = hashlib.sha256(new_schema).hexdigest()
    scenario.builtin(builtin)
    if builtin_serial <= 5:
        # Старый snapshot не проходит новую схему; смена S не даёт принять
        # каталог без отзыва со старой/равной парой и новыми байтами.
        with pytest.raises(CatalogError) as refused:
            scenario.load()
        assert refused.value.code == "stale-catalog"
        assert scenario.state.read_bytes() == before
        assert catalog_state.read_state(scenario.state) == old_marker
    else:
        accepted = scenario.load()
        marker = catalog_state.read_state(scenario.state)
        assert marker is not None
        assert (marker.trust_epoch, marker.serial) == (1, 6)
        assert marker.schema_id == hashlib.sha256(new_schema).hexdigest()[:16]
        assert marker.schema_id != old_marker.schema_id
        assert not accepted.revoked and marker.revoked == ()
        # Снятие отзыва разрешено только старшей паре; обратный ход закрыт.
        scenario.builtin(scenario.remote(serial=4))
        with pytest.raises(CatalogError):
            scenario.load()
        assert catalog_state.read_state(scenario.state) == marker


@pytest.mark.parametrize("builtin_serial", [5, 6, 7], ids=["older", "equal", "newer"])
def test_schema_id_change_does_not_affect_candidate_order(
    scenario: Scenario, builtin_serial: int
) -> None:
    remote = scenario.remote(serial=5)
    scenario.snapshot(remote)
    scenario.load()
    old_marker = catalog_state.read_state(scenario.state)
    assert old_marker is not None
    schema_path = scenario.root / "catalog.schema.json"
    new_schema = schema_path.read_bytes() + b"\n"
    schema_path.write_bytes(new_schema)
    # Оба кандидата подписаны заново и совместимы с новой схемой. Watermark
    # прошлого клиента (1, 5, SHA) не сбрасывается и не делится по schema_id.
    remote = scenario.remote(serial=6)
    remote["schema_sha256"] = hashlib.sha256(new_schema).hexdigest()
    raw = scenario.snapshot(remote)
    builtin = copy.deepcopy(remote)
    builtin["serial"] = builtin_serial
    builtin_raw = scenario.builtin(builtin)
    selected = scenario.load()
    assert selected.serial == max(6, builtin_serial)
    marker = catalog_state.read_state(scenario.state)
    assert marker is not None
    assert marker.schema_id == hashlib.sha256(new_schema).hexdigest()[:16]
    assert marker.schema_id != old_marker.schema_id
    assert marker.sha256 == hashlib.sha256(raw if builtin_serial < 6 else builtin_raw).hexdigest()


def test_snapshot_is_validated_against_current_builtin_schema(scenario: Scenario) -> None:
    remote = scenario.remote()
    scenario.snapshot(remote)
    scenario.load()
    before = scenario.state.read_bytes()
    schema_path = scenario.root / "catalog.schema.json"
    schema = schema_path.read_bytes() + b"\n"
    schema_path.write_bytes(schema)
    builtin = scenario.remote(serial=4)
    builtin["schema_sha256"] = hashlib.sha256(schema).hexdigest()
    scenario.builtin(builtin)
    with pytest.raises(CatalogError) as refused:
        scenario.load()
    assert refused.value.code == "stale-catalog"
    assert scenario.state.read_bytes() == before


def test_write_failure_never_returns_catalog(
    scenario: Scenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario.load()
    before = scenario.state.read_bytes()
    scenario.snapshot(scenario.remote())
    monkeypatch.setattr(catalog_state, "write_state", Mock(side_effect=OSError("disk full")))
    with pytest.raises(CatalogError) as refused:
        scenario.load()
    assert refused.value.code == "io"
    assert scenario.state.read_bytes() == before


def test_verify_helper_never_applies_state(
    scenario: Scenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = json.dumps(scenario.remote()).encode()
    apply = Mock(side_effect=AssertionError("verification must not accept a catalog"))
    monkeypatch.setattr(catalog_state, "apply_state", apply)
    selected = verify_catalog(raw, signature(raw), scenario.verifier, schema_root=scenario.root)
    assert selected.serial == 5
    apply.assert_not_called()
    assert not scenario.state.exists()


def install_fixture(store: ModelStore, entry: CatalogEntry, *, activate: bool = True) -> None:
    staging = store.staging_dir(entry.id, entry.revision)
    for name, raw in CONTENTS.items():
        (staging / name).write_bytes(raw)
    result = Installer(store, lambda _directory, _entry: SmokeResult(True)).install_from_staging(
        entry, activate=activate
    )
    assert result.state == "ok", result.reason


@pytest.mark.parametrize("restart", [False, True], ids=["live", "restart"])
@pytest.mark.parametrize("operation", ["install", "activate", "revert"])
def test_remote_revocation_reaches_real_consumers(
    scenario: Scenario, monkeypatch: pytest.MonkeyPatch, restart: bool, operation: str
) -> None:
    from astra_voice.app import _revoked_check
    from astra_voice.net.gate import NetworkGate
    from astra_voice.net.http import HttpClient
    from astra_voice.ui import model_downloads
    from astra_voice.updates import catalog_updates
    from helpers.qt_app import get_qapplication

    get_qapplication()
    store = ModelStore(scenario.state.parent / "models")
    monkeypatch.setattr(paths, "data_dir_static", lambda: scenario.root)
    monkeypatch.setattr(paths, "state_dir", lambda: scenario.state.parent)
    monkeypatch.setattr(model_downloads, "Verifier", lambda *_args, **_kwargs: BoundVerifier())
    monkeypatch.setattr(catalog_effective, "Verifier", lambda *_args, **_kwargs: BoundVerifier())
    monkeypatch.setattr(model_downloads, "ModelStore", lambda: store)
    service = model_downloads.ModelService(Settings(), Policy())
    entry = service.catalog.entries[0]
    install_fixture(store, entry)
    if operation == "activate":
        # Другая активная модель исключает ложный PASS из-за no-op выбора текущей.
        other = replace(entry, id="other-model")
        install_fixture(store, other)
    elif operation == "revert":
        install_fixture(store, replace(entry, revision=NEW_REVISION))
        store.set_current(entry.id, NEW_REVISION, previous_revision=entry.revision)
    remote = scenario.remote()
    remote["revoked"] = [{"model_id": entry.id, "revision": entry.revision, "reason": "отзыв"}]
    raw = json.dumps(remote).encode()
    monkeypatch.setattr(catalog_updates, "fetch", lambda *_args, **_kwargs: (raw, signature(raw)))
    client = HttpClient(
        NetworkGate(Settings(check_model_updates=True), Policy()),
        user_agent="catalog-effective-test",
    )
    source = catalog_updates.CatalogUpdateSource(
        client,
        baseline=service.catalog,
        catalog_url="https://models.example.ru/catalog.json",
        verifier=scenario.verifier,
        schema_root=scenario.root,
        state_dir=scenario.snapshots,
        global_state_path=scenario.state,
    )
    source.refresh(threading.Event())
    accepted = source.current()
    if restart:
        # Новый ModelService обязан сам найти snapshot, без accept_catalog/provider.
        service = model_downloads.ModelService(Settings(), Policy())
    else:
        service.accept_catalog(accepted)
    assert service.catalog == accepted
    assert service.revoked_revision(entry.id, entry.revision)
    assert _revoked_check(None)(entry.id, entry.revision)
    before = (store.root / "current.json").read_bytes()
    if operation == "install":
        assert service.install_from_staging(entry).reason_code == "revoked"
        assert service.install_from_path(scenario.root, entry).reason_code == "revoked"
    elif operation == "activate":
        with pytest.raises(StoreError) as refused:
            service.set_current(entry.id, entry.revision)
        assert refused.value.code == "revoked"
        downloads = model_downloads.ModelDownloads(service)
        try:
            downloads.makeModelCurrent(entry.id)
            assert downloads._card_states[entry.id] == "failed"
            assert "Издатель больше не рекомендует" in downloads._card_messages[entry.id]
        finally:
            downloads.shutdown()
    else:
        result = service.revision_updater(on_state=lambda *_args: None).revert_previous(entry.id)
        assert result.state == "refused" and result.reason_code == "revoked"
    assert (store.root / "current.json").read_bytes() == before


@pytest.mark.parametrize("operation", ["update", "revert", "set", "restore", "install"])
@pytest.mark.parametrize("marker_has_revocation", [False, True])
def test_source_revocation_blocks_activation_before_gui_accepts_catalog(
    scenario: Scenario,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    marker_has_revocation: bool,
) -> None:
    """T-223: source уже принял отзыв, очередь GUI ещё не обработана."""
    from astra_voice.ui import model_downloads

    store = ModelStore(scenario.state.parent / "models")
    monkeypatch.setattr(paths, "data_dir_static", lambda: scenario.root)
    monkeypatch.setattr(paths, "state_dir", lambda: scenario.state.parent)
    monkeypatch.setattr(model_downloads, "Verifier", lambda *_args, **_kwargs: BoundVerifier())
    monkeypatch.setattr(model_downloads, "ModelStore", lambda: store)
    monkeypatch.setattr(catalog_effective, "Verifier", lambda *_args, **_kwargs: BoundVerifier())
    service = model_downloads.ModelService(Settings(check_model_updates=True), Policy())
    entry = service.catalog.entries[0]
    original_catalog = service.catalog
    install_fixture(store, entry)
    safe = replace(entry, revision=NEW_REVISION)
    install_fixture(store, safe)
    store.set_current(entry.id, safe.revision, previous_revision=entry.revision)
    remote = scenario.remote()
    remote["revoked"] = [{"model_id": entry.id, "revision": entry.revision, "reason": "отзыв"}]
    raw = json.dumps(remote).encode()
    monkeypatch.setattr(catalog_updates, "fetch", lambda *_args, **_kwargs: (raw, signature(raw)))
    source = catalog_updates.CatalogUpdateSource(
        service._downloader._http,
        baseline=original_catalog,
        catalog_url="https://models.example.ru/catalog.json",
        verifier=scenario.verifier,
        schema_root=scenario.root,
        state_dir=scenario.snapshots,
        global_state_path=scenario.state,
    )
    # Крошечные fixture-файлы не являются ASR-моделью. Самопроверка здесь
    # успешна, чтобы тест дошёл до настоящего финального барьера активации.
    monkeypatch.setattr(service._installer, "_smoke", lambda *_args: SmokeResult(True))
    previous_marker = scenario.state.read_bytes()
    source.refresh(threading.Event())
    assert source.current().is_revoked(entry.id, entry.revision)
    assert service.catalog is original_catalog
    assert not service.catalog.is_revoked(entry.id, entry.revision)
    if not marker_has_revocation:
        # Окно публикации: новый подписанный snapshot уже записан, а JSON-
        # отметка пока остаётся от прежнего каталога (с прежним SHA и serial).
        write_private(scenario.state, previous_marker)
    before = (store.root / "current.json").read_bytes()

    if operation in {"update", "revert"}:
        updater = service.revision_updater(on_state=lambda *_args: None)

        # Локальная staging-загрузка; сетевой транспорт и GUI-очередь здесь не нужны.
        def download(model: CatalogEntry, **_kwargs: Any) -> Path:
            staging = store.staging_dir(model.id, model.revision)
            for name, payload in CONTENTS.items():
                (staging / name).write_bytes(payload)
            return staging

        monkeypatch.setattr(service._downloader, "download", download)
        result = (
            updater.apply(entry.id, cancel=threading.Event())
            if operation == "update"
            else updater.revert_previous(entry.id)
        )
        assert result.state in {"refused", "update-failed"}
        assert result.reason_code == "revoked"
    elif operation == "install":
        staging = store.staging_dir(entry.id, entry.revision)
        for name, payload in CONTENTS.items():
            (staging / name).write_bytes(payload)
        install_result = service.install_from_staging(entry)
        assert install_result.state == "error" and install_result.reason_code == "revoked"
    else:
        with pytest.raises(StoreError) as refused:
            if operation == "set":
                service.set_current(entry.id, entry.revision)
            else:
                service.restore_current(entry.id, entry.revision, previous_revision=safe.revision)
        assert refused.value.code == "revoked"
    assert (store.root / "current.json").read_bytes() == before


def test_source_publishes_snapshot_and_watermark_after_activation_finishes(
    scenario: Scenario,
    source: catalog_updates.CatalogUpdateSource,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Принятие source нельзя вклинить между проверкой отзыва и записью current."""
    from astra_voice.ui.model_downloads import ModelService

    monkeypatch.setattr(paths, "state_dir", lambda: scenario.state.parent)
    monkeypatch.setattr(paths, "data_dir_static", lambda: scenario.root)
    monkeypatch.setattr(catalog_effective, "Verifier", lambda *_args, **_kwargs: BoundVerifier())
    service = ModelService.__new__(ModelService)
    service._catalog = source.current()
    service._catalog_error = None
    service._store = ModelStore(scenario.state.parent / "models")
    entry = service.catalog.entries[0]
    install_fixture(service._store, entry)
    install_fixture(service._store, replace(entry, revision=NEW_REVISION))
    marker_before = scenario.state.read_bytes()
    remote = scenario.remote()
    remote["revoked"] = [{"model_id": entry.id, "revision": entry.revision, "reason": "отзыв"}]
    raw = json.dumps(remote).encode()
    fetching, downloaded = threading.Event(), threading.Event()
    writing, finish_write = threading.Event(), threading.Event()
    verified, published = threading.Event(), threading.Event()
    errors: list[Exception] = []
    original_write = service._store.set_current
    original_verify = catalog_effective.verify_catalog

    def fetch(*_args: Any, **_kwargs: Any) -> tuple[bytes, bytes]:
        fetching.set()
        assert downloaded.wait(5), "Тест не разрешил завершить получение каталога"
        return raw, signature(raw)

    def verify(*args: Any, **kwargs: Any) -> Catalog:
        catalog = original_verify(*args, **kwargs)
        verified.set()
        return catalog

    def write(model_id: str, revision: str, *, previous_revision: str | None = None) -> None:
        writing.set()
        assert finish_write.wait(5), "Тест не разрешил записать current"
        original_write(model_id, revision, previous_revision=previous_revision)

    def activate() -> None:
        try:
            service.set_current(entry.id, entry.revision)
        except Exception as error:
            errors.append(error)

    def publish() -> None:
        try:
            source.refresh(threading.Event())
            published.set()
        except Exception as error:
            errors.append(error)

    monkeypatch.setattr(catalog_updates, "fetch", fetch)
    monkeypatch.setattr(catalog_updates, "verify_catalog", verify)
    monkeypatch.setattr(service._store, "set_current", write)
    publication = threading.Thread(target=publish)
    activation = threading.Thread(target=activate)
    publication.start()
    try:
        assert fetching.wait(5), "Source не начал получение каталога"
        activation.start()
        assert writing.wait(5), "Активация не дошла до записи current"
        downloaded.set()
        assert verified.wait(5), "Source не проверил полученный каталог"
        assert not published.wait(0.2), "Source опубликовал отзыв посреди активации"
        assert scenario.state.read_bytes() == marker_before
        assert not (scenario.snapshots / "catalog-snapshot.json").exists()
    finally:
        downloaded.set()
        finish_write.set()
        publication.join(5)
        if activation.ident is not None:
            activation.join(5)
    assert not publication.is_alive() and not activation.is_alive()
    assert errors == []
    assert published.is_set()
    assert source.current().is_revoked(entry.id, entry.revision)
    assert scenario.state.read_bytes() != marker_before
    assert (scenario.snapshots / "catalog-snapshot.json").is_file()
    before = (service._store.root / "current.json").read_bytes()
    with pytest.raises(StoreError) as refused:
        service.set_current(entry.id, entry.revision)
    assert refused.value.code == "revoked"
    assert (service._store.root / "current.json").read_bytes() == before


@pytest.fixture
def revocation_service(scenario: Scenario, monkeypatch: pytest.MonkeyPatch) -> Any:
    from astra_voice.ui.model_downloads import ModelService

    monkeypatch.setattr(paths, "state_dir", lambda: scenario.state.parent)
    monkeypatch.setattr(paths, "data_dir_static", lambda: scenario.root)
    monkeypatch.setattr(catalog_effective, "Verifier", lambda *_args, **_kwargs: scenario.verifier)
    monkeypatch.setattr(catalog_effective, "_snapshot_cache", None)
    service = ModelService.__new__(ModelService)
    service._catalog = scenario.load()
    service._catalog_error = None
    service._store = ModelStore(scenario.state.parent / "models")
    return service


def test_gui_fifty_revocation_reads_never_verify_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Настоящий Verifier обёрнут счётчиком; gpgv/результат не подменяются."""
    from astra_voice.ui.model_downloads import ModelService

    state = tmp_path / "state"
    monkeypatch.setattr(paths, "state_dir", lambda: state)
    monkeypatch.setattr(paths, "data_dir_static", lambda: DATA_ROOT)
    raw = (DATA_ROOT / "catalog.json").read_bytes()
    sig = (DATA_ROOT / "catalog.json.sig").read_bytes()
    calls = []
    real_verify = Verifier.verify_detached

    def verify(self: Verifier, *args: Any, **kwargs: Any) -> VerifyResult:
        calls.append(args)
        return real_verify(self, *args, **kwargs)

    monkeypatch.setattr(Verifier, "verify_detached", verify)
    catalog = verify_catalog(
        raw,
        sig,
        Verifier("catalog", keyring=DATA_ROOT / "keys" / "release.gpg"),
        schema_root=DATA_ROOT,
    )
    assert len(calls) == 1  # Обёртка действительно вызывает проверку подписи.
    write_private(
        state / "model-catalog-updates" / "catalog-snapshot.json",
        json.dumps(
            {"raw": base64.b64encode(raw).decode(), "signature": base64.b64encode(sig).decode()}
        ).encode(),
    )
    service = ModelService.__new__(ModelService)
    service._catalog, service._catalog_error = catalog, None
    service._store = ModelStore(state / "models")
    entry = catalog.entries[0]
    staging = service._store.staging_dir(entry.id, entry.revision)
    (staging / "state.json").write_text(
        json.dumps({"layout": entry.layout, "variant": entry.variant, "size_bytes": 1})
    )
    service._store.commit(entry.id, entry.revision)
    for _ in range(50):
        assert not service.revoked_revision(entry.id, entry.revision)
        assert service.installed_ok()
    assert len(calls) == 1


def test_new_snapshot_signature_is_verified_once_per_exact_pair(
    scenario: Scenario, revocation_service: Any
) -> None:
    entry = revocation_service.catalog.entries[0]
    remote = scenario.remote()
    remote["revoked"] = [{"model_id": entry.id, "revision": entry.revision, "reason": "отзыв"}]
    scenario.snapshot(remote)
    before = scenario.verifier.calls
    for _ in range(3):
        with pytest.raises(StoreError, match="отозвана") as refused:
            catalog_effective.check_revoked(entry.id, entry.revision, None, include_snapshot=True)
        assert refused.value.code == "revoked"
    assert scenario.verifier.calls == before + 1
    # Те же raw, другая подпись: нужна новая проверка, прежние отзывы кэша остаются.
    # T-233: отказ проверяем на безопасной ревизии, сохраняя известный отзыв X.
    scenario.snapshot(remote, valid_signature=False)
    for expected_calls in (before + 2, before + 2):  # Отказ проверки тоже кэшируется.
        with pytest.raises(StoreError) as unknown:
            catalog_effective.check_revoked(entry.id, NEW_REVISION, None, include_snapshot=True)
        assert unknown.value.code == "catalog-unavailable"
        assert scenario.verifier.calls == expected_calls
        with pytest.raises(StoreError) as revoked:
            catalog_effective.check_revoked(entry.id, entry.revision, None, include_snapshot=True)
        assert revoked.value.code == "revoked"
        assert scenario.verifier.calls == expected_calls


@pytest.mark.parametrize("kind", ["json", "signature", "schema"])
@pytest.mark.parametrize("known", ["memory", "marker"])
def test_bad_snapshot_cannot_hide_known_revocation(
    scenario: Scenario,
    revocation_service: Any,
    caplog: pytest.LogCaptureFixture,
    kind: str,
    known: str,
) -> None:
    service = revocation_service
    entry = service.catalog.entries[0]
    remote = scenario.remote()
    remote["revoked"] = [{"model_id": entry.id, "revision": entry.revision, "reason": "отзыв"}]
    raw = json.dumps(remote).encode()
    accepted = verify_catalog(raw, signature(raw), scenario.verifier, schema_root=scenario.root)
    if known == "memory":
        service.accept_catalog(accepted)
        write_private(scenario.state, b"{}")
    else:
        catalog_state.write_state(scenario.state, state_of(accepted, raw))
    bad = scenario.remote(serial=6)
    if kind == "json":
        write_private(scenario.snapshots / "catalog-snapshot.json", b"{}")
    elif kind == "signature":
        scenario.snapshot(bad, valid_signature=False)
    else:
        bad["schema_sha256"] = "0" * 64
        scenario.snapshot(bad)
    with pytest.raises(StoreError) as refused:
        service.set_current(entry.id, entry.revision)
    assert refused.value.code == "revoked"
    assert service.revoked_revision(entry.id, entry.revision)
    from astra_voice.app import _revoked_check
    from astra_voice.core.model_source import ModelRevoked, resolve_model_request
    from astra_voice.core.settings import from_dict

    with pytest.raises(ModelRevoked):
        resolve_model_request(
            from_dict({"model_id": entry.id, "model_revision": entry.revision}),
            store_dir=service._store.root,
            revoked=_revoked_check(service),
        )
    # Unknown marker остаётся fail-closed; восстанавливаем полноценный marker.
    catalog_state.write_state(scenario.state, state_of(accepted, raw))
    if kind == "json":
        # Пару не прочитать, отметка есть: снимок пропускается с WARNING.
        safe = replace(entry, revision=NEW_REVISION)
        install_fixture(service._store, safe)
        with caplog.at_level(logging.WARNING):
            service.set_current(safe.id, safe.revision)
        assert service._store.current().revision == safe.revision
        assert "снимок каталога" in caplog.text and "пропущен" in caplog.text
        return
    # Снимок новее отметки не проверен: отзывы неизвестны, поэтому
    # активация и неотозванной ревизии отказывает до store.
    with pytest.raises(StoreError) as unknown:
        service.set_current(entry.id, NEW_REVISION)
    assert unknown.value.code == "catalog-unavailable"


@pytest.mark.parametrize("pair", [(1, 3), (1, 4)])
def test_stale_or_equal_pair_snapshot_never_adds_revocations(
    scenario: Scenario, revocation_service: Any, pair: tuple[int, int]
) -> None:
    entry = revocation_service.catalog.entries[0]
    remote = scenario.remote(epoch=pair[0], serial=pair[1])
    remote["revoked"] = [{"model_id": entry.id, "revision": entry.revision, "reason": "отзыв"}]
    scenario.snapshot(remote)
    before = scenario.verifier.calls
    catalog_effective.check_revoked(entry.id, entry.revision, None, include_snapshot=True)
    assert scenario.verifier.calls == before


def test_snapshot_matching_marker_sha_skips_signature(
    scenario: Scenario, revocation_service: Any
) -> None:
    entry = revocation_service.catalog.entries[0]
    scenario.snapshot(scenario.document, valid_signature=False)
    before = scenario.verifier.calls
    catalog_effective.check_revoked(entry.id, entry.revision, None, include_snapshot=True)
    assert scenario.verifier.calls == before


def test_cached_snapshot_is_rechecked_after_schema_changes(
    scenario: Scenario, revocation_service: Any, caplog: pytest.LogCaptureFixture
) -> None:
    entry = revocation_service.catalog.entries[0]
    scenario.snapshot(scenario.remote())
    catalog_effective.check_revoked(entry.id, entry.revision, None, include_snapshot=True)
    before = scenario.verifier.calls
    schema = scenario.root / "catalog.schema.json"
    schema.write_bytes(schema.read_bytes() + b"\n")
    with caplog.at_level(logging.WARNING), pytest.raises(StoreError) as unknown:
        catalog_effective.check_revoked(entry.id, entry.revision, None, include_snapshot=True)
    assert unknown.value.code == "catalog-unavailable"
    assert scenario.verifier.calls == before + 1
    assert "отзывы неизвестны" in caplog.text


def test_unavailable_provider_preserves_positive_marker(
    scenario: Scenario, revocation_service: Any
) -> None:
    service = revocation_service
    entry = service.catalog.entries[0]
    marker = catalog_state.read_state(scenario.state)
    assert marker is not None
    catalog_state.write_state(
        scenario.state, replace(marker, revoked=((entry.id, entry.revision),))
    )
    service._catalog_error = CatalogError("state-invalid", "Каталог недоступен")
    assert service.revoked_revision(entry.id, entry.revision)
    with pytest.raises(StoreError) as refused:
        service.set_current(entry.id, entry.revision)
    assert refused.value.code == "revoked"
    with pytest.raises(StoreError) as unknown:
        service.set_current(entry.id, NEW_REVISION)
    assert unknown.value.code == "catalog-unavailable"


def test_slow_provider_retries_changed_catalog_without_holding_catalog_lock(
    scenario: Scenario, revocation_service: Any
) -> None:
    """accept_catalog получает lock во время provider; устаревший объект не активируется."""
    service = revocation_service
    entry = service.catalog.entries[0]
    install_fixture(service._store, entry)
    remote = scenario.remote()
    remote["revoked"] = [{"model_id": entry.id, "revision": entry.revision, "reason": "отзыв"}]
    raw = json.dumps(remote).encode()
    newer = verify_catalog(raw, signature(raw), scenario.verifier, schema_root=scenario.root)
    entered, finish = threading.Event(), threading.Event()
    current_before = (service._store.root / "current.json").read_bytes()
    provider_lock = threading.Lock()
    provider_lock.acquire()
    calls = []
    failures = []

    def provider() -> Catalog:
        current: Catalog = service.catalog
        calls.append(current)
        entered.set()
        with provider_lock:
            assert finish.wait(5), "Не разрешено завершение provider"
        return current

    def activate() -> None:
        try:
            catalog_effective.activate_revision(
                service._store,
                provider,
                entry.id,
                entry.revision,
                catalog_peek=lambda: service._catalog,
            )
        except Exception as error:
            failures.append(error)

    thread = threading.Thread(target=activate)
    thread.start()
    try:
        assert entered.wait(5)
        # Поток держит provider_lock и может публиковать каталог: обратный
        # порядок locks в активации привёл бы к deadlock.
        acquired = catalog_effective.CATALOG_LOCK.acquire(blocking=False)
        assert acquired, "Provider выполняется под CATALOG_LOCK"
        try:
            service.accept_catalog(newer)
        finally:
            catalog_effective.CATALOG_LOCK.release()
    finally:
        finish.set()
        provider_lock.release()
        thread.join(5)
    assert not thread.is_alive()
    assert len(calls) == 2
    assert calls[0] is not newer and calls[1] is newer
    assert len(failures) == 1 and isinstance(failures[0], StoreError)
    assert failures[0].code == "revoked"
    assert (service._store.root / "current.json").read_bytes() == current_before


def test_newer_builtin_activation_ignores_revoked_old_snapshot(
    scenario: Scenario, revocation_service: Any
) -> None:
    service = revocation_service
    entry = service.catalog.entries[0]
    old = scenario.remote(serial=5)
    old["revoked"] = [{"model_id": entry.id, "revision": entry.revision, "reason": "отзыв"}]
    scenario.snapshot(old)
    scenario.builtin(scenario.remote(serial=6))
    service.accept_catalog(scenario.load())
    install_fixture(service._store, entry)
    before = scenario.verifier.calls
    service.set_current(entry.id, entry.revision)
    assert service._store.current().revision == entry.revision
    assert scenario.verifier.calls == before


@pytest.mark.parametrize("newer", [True, False], ids=["newer-than-marker", "not-newer"])
def test_t230_bad_snapshot_without_known_revocation(
    scenario: Scenario, revocation_service: Any, newer: bool
) -> None:
    """T-230, отрицательная ветка: снимок битый, отзыва нет ни в памяти, ни в отметке.

    Решение Юрки 15.10, отступление от формулировки T2 для быстрого пути:
    рантайм снимок не читает, поэтому GUI/рантайм отвечают «не отозвано», а не
    RevocationUnknown. Полный путь (активация): снимок новее отметки —
    catalog-unavailable, не новее — пропуск без проверки подписи.
    """
    from astra_voice.app import _revoked_check
    from astra_voice.core.model_source import _not_revoked

    service = revocation_service
    entry = service.catalog.entries[0]
    install_fixture(service._store, entry)
    bad = scenario.remote(serial=5 if newer else 3)
    bad["schema_sha256"] = "0" * 64
    scenario.snapshot(bad)
    marker = catalog_state.read_state(scenario.state)
    assert marker is not None and marker.serial == 4 and marker.revoked == ()
    calls = scenario.verifier.calls

    assert _revoked_check(service)(entry.id, entry.revision) is False
    request = {"id": entry.id, "revision": entry.revision}
    assert _not_revoked(request, _revoked_check(service)) is request
    assert scenario.verifier.calls == calls

    current = service._store.root / "current.json"
    before = current.read_bytes()
    if newer:
        with pytest.raises(StoreError) as unknown:
            service.set_current(entry.id, entry.revision)
        assert unknown.value.code == "catalog-unavailable"
        assert current.read_bytes() == before
    else:
        service.set_current(entry.id, entry.revision)
        assert service._store.current().revision == entry.revision
        assert scenario.verifier.calls == calls


@pytest.mark.parametrize("operation", ["install", "switch", "update"])
@pytest.mark.parametrize("revoked", [False, True], ids=["safe", "revoked"])
def test_t231_schema_upgrade_allows_safe_revision_and_blocks_marker_revocation(
    scenario: Scenario, revocation_service: Any, operation: str, revoked: bool
) -> None:
    """Старый snapshot, новая схема и принятый встроенный каталог старшей пары."""
    from astra_voice.updates.model_updates import ModelRevisionUpdater

    service = revocation_service
    entry = service.catalog.entries[0]
    if operation == "switch":
        install_fixture(service._store, entry)
    old = scenario.remote()
    if revoked:
        old["revoked"] = [{"model_id": entry.id, "revision": entry.revision, "reason": "отзыв"}]
    scenario.snapshot(old)
    scenario.load()
    schema = scenario.root / "catalog.schema.json"
    schema.write_bytes(schema.read_bytes() + b"\n")
    newer = copy.deepcopy(old)
    newer["serial"] = 6
    newer["schema_sha256"] = hashlib.sha256(schema.read_bytes()).hexdigest()
    scenario.builtin(newer)
    service.accept_catalog(scenario.load())
    marker = catalog_state.read_state(scenario.state)
    assert marker is not None and marker.serial == 6 and marker.revoked is not None
    assert ((entry.id, entry.revision) in marker.revoked) is revoked
    assert not service.catalog.is_revoked(entry.id, NEW_REVISION)
    # Отказ на целевую ревизию должен опираться на отметку, даже до GUI-доставки.
    service._catalog = replace(service.catalog, revoked=())
    install_fixture(service._store, replace(entry, revision=NEW_REVISION))
    service._store.set_current(entry.id, NEW_REVISION)
    before = (service._store.root / "current.json").read_bytes()

    class StagingDownloader:
        def download(self, entry: CatalogEntry, **_kwargs: Any) -> Path:
            staging = service._store.staging_dir(entry.id, entry.revision)
            for name, payload in CONTENTS.items():
                (staging / name).write_bytes(payload)
            return Path(staging)

    downloader = StagingDownloader()
    installer = Installer(
        service._store,
        lambda *_args: SmokeResult(True),
        catalog=lambda: service.catalog,
        activate=service.activate,
    )
    if operation == "install":
        downloader.download(entry)
        result = installer.install_from_staging(entry)
        assert result.state == ("error" if revoked else "ok")
        assert result.reason_code == ("revoked" if revoked else "")
    elif operation == "switch":
        if revoked:
            with pytest.raises(StoreError) as refused:
                service.set_current(entry.id, entry.revision)
            assert refused.value.code == "revoked"
        else:
            service.set_current(entry.id, entry.revision)
    else:
        updater = ModelRevisionUpdater(
            service._store,
            downloader,
            installer,
            lambda: service.catalog,
            activate=service.activate,
        )
        update_result = updater.apply(entry.id, cancel=threading.Event())
        assert update_result.state == ("refused" if revoked else "updated")
        assert update_result.reason_code == ("revoked" if revoked else "")
    if revoked:
        assert (service._store.root / "current.json").read_bytes() == before
    else:
        assert service._store.current().revision == entry.revision


@pytest.mark.parametrize("marker_present", [False, True], ids=["no-marker", "newer-than-marker"])
def test_t231_unverifiable_newer_snapshot_refuses_activation(
    scenario: Scenario, revocation_service: Any, marker_present: bool
) -> None:
    """Повторный T2 требует отказа, если непроверенный snapshot нельзя считать старым."""
    service = revocation_service
    entry = service.catalog.entries[0]
    install_fixture(service._store, entry)
    if not marker_present:
        scenario.state.unlink()
    bad = scenario.remote()
    bad["schema_sha256"] = "0" * 64
    scenario.snapshot(bad)
    before = (service._store.root / "current.json").read_bytes()

    with pytest.raises(StoreError) as refused:
        service.set_current(entry.id, entry.revision)

    assert refused.value.code == "catalog-unavailable"
    assert (service._store.root / "current.json").read_bytes() == before


def prepare_revoking_refresh(
    scenario: Scenario,
    source: catalog_updates.CatalogUpdateSource,
    monkeypatch: pytest.MonkeyPatch,
) -> bytes:
    """Готовит настоящий подписанный каталог с отзывом поверх принятой пары (1,4)."""
    source._baseline = source.current()
    entry = source._baseline.entries[0]
    remote = scenario.remote()
    remote["revoked"] = [{"model_id": entry.id, "revision": entry.revision, "reason": "отзыв"}]
    raw = json.dumps(remote).encode()
    monkeypatch.setattr(catalog_updates, "fetch", Mock(return_value=(raw, signature(raw))))
    return raw


@pytest.mark.parametrize("previous_snapshot", [False, True])
@pytest.mark.parametrize("failure", ["marker", "snapshot-before-replace", "snapshot-after-replace"])
def test_t233_refresh_rolls_back_snapshot_and_retries(
    scenario: Scenario,
    source: catalog_updates.CatalogUpdateSource,
    revocation_service: Any,
    monkeypatch: pytest.MonkeyPatch,
    previous_snapshot: bool,
    failure: str,
) -> None:
    from astra_voice.app import _revoked_check
    from astra_voice.core.model_source import _not_revoked

    service = revocation_service
    entry = service.catalog.entries[0]
    install_fixture(service._store, entry)
    if previous_snapshot:
        scenario.snapshot(scenario.document)
    snapshot_path = scenario.snapshots / catalog_effective.SNAPSHOT_FILE_NAME
    before = snapshot_path.read_bytes() if previous_snapshot else None
    prepare_revoking_refresh(scenario, source, monkeypatch)
    accepted, accepted_state = source._catalog, source._accepted
    real_write = update_cache.write_private
    calls = 0

    def fail_snapshot(path: Path, raw: bytes) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            if failure == "snapshot-after-replace":
                real_write(path, raw)
            raise OSError("сбой записи снимка")
        real_write(path, raw)

    with monkeypatch.context() as fault:
        if failure == "marker":
            fault.setattr(catalog_state, "write_state", Mock(side_effect=OSError("диск заполнен")))
        else:
            fault.setattr(update_cache, "write_private", fail_snapshot)
        with pytest.raises(CatalogError) as refused:
            source.refresh(threading.Event())
    assert refused.value.code == "state-write"
    assert not source._blocked
    assert source._catalog is accepted and source._accepted == accepted_state
    assert (snapshot_path.read_bytes() if snapshot_path.exists() else None) == before
    assert source.current() is accepted
    service.set_current(entry.id, entry.revision)
    checker = _revoked_check(service)
    request = {"id": entry.id, "revision": entry.revision}
    assert checker(entry.id, entry.revision) is False
    assert _not_revoked(request, checker) is request
    restarted = scenario.load()
    assert restarted.serial == 4 and not restarted.is_revoked(entry.id, entry.revision)
    source.refresh(threading.Event())
    assert source.current().serial == 5
    marker = catalog_state.read_state(scenario.state)
    assert marker is not None and (marker.trust_epoch, marker.serial) == (1, 5)


@pytest.mark.parametrize("previous_snapshot", ["oversized", "symlink", "directory", "eio"])
@pytest.mark.parametrize("marker_failure", [False, True])
def test_t233_unreadable_previous_snapshot_does_not_block_refresh(
    scenario: Scenario,
    source: catalog_updates.CatalogUpdateSource,
    monkeypatch: pytest.MonkeyPatch,
    previous_snapshot: str,
    marker_failure: bool,
) -> None:
    raw = prepare_revoking_refresh(scenario, source, monkeypatch)
    accepted, accepted_state = source._catalog, source._accepted
    marker_before = scenario.state.read_bytes()
    snapshot_path = scenario.snapshots / catalog_effective.SNAPSHOT_FILE_NAME
    target = scenario.snapshots / "snapshot-target.json"
    target.write_bytes("не изменять".encode())
    if previous_snapshot == "oversized":
        with snapshot_path.open("wb") as stream:
            stream.truncate(catalog_effective.SNAPSHOT_MAX_BYTES + 1)
    elif previous_snapshot == "symlink":
        snapshot_path.symlink_to(target)
    elif previous_snapshot == "directory":
        snapshot_path.mkdir()
    else:
        snapshot_path.write_bytes("нечитаемый прежний снимок".encode())
    read_private = update_cache.read_private

    def read_with_eio(path: Path, limit: int) -> bytes:
        if path == snapshot_path:
            raise OSError(5, "сбой чтения снимка")
        return read_private(path, limit)

    with monkeypatch.context() as fault:
        if previous_snapshot == "eio":
            fault.setattr(catalog_updates, "read_private", read_with_eio)
        if marker_failure:
            fault.setattr(catalog_state, "write_state", Mock(side_effect=OSError("диск заполнен")))
            with pytest.raises(CatalogError) as refused:
                source.refresh(threading.Event())
            assert refused.value.code == "state-write"
        else:
            source.refresh(threading.Event())
    assert not source._blocked
    assert target.read_bytes() == "не изменять".encode()
    if marker_failure:
        assert not snapshot_path.exists() and not snapshot_path.is_symlink()
        assert source._catalog is accepted and source._accepted == accepted_state
        assert scenario.state.read_bytes() == marker_before
        assert source.current() is accepted
    else:
        assert catalog_effective.read_snapshot(scenario.snapshots) == (raw, signature(raw))
        marker = catalog_state.read_state(scenario.state)
        assert marker is not None and (marker.trust_epoch, marker.serial) == (1, 5)
        assert source.current().serial == 5


def test_t233_marker_failure_after_replace_keeps_accepted_snapshot(
    scenario: Scenario,
    source: catalog_updates.CatalogUpdateSource,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = prepare_revoking_refresh(scenario, source, monkeypatch)
    real_write = catalog_state.write_state

    def write_then_fail(path: Path, state: catalog_state.CatalogState) -> None:
        real_write(path, state)
        raise OSError("сбой fsync после замены отметки")

    with monkeypatch.context() as fault:
        fault.setattr(catalog_state, "write_state", write_then_fail)
        with pytest.raises(CatalogError) as refused:
            source.refresh(threading.Event())
    assert refused.value.code == "state-write"
    assert catalog_effective.read_snapshot(scenario.snapshots) == (raw, signature(raw))
    assert source._catalog is not None and source._catalog.serial == 5
    assert not source._blocked
    assert source.current().serial == 5
    assert catalog_state.read_state(scenario.state) == source._accepted


@pytest.mark.parametrize("damage", ["eio", "json", "missing", "catalog-json"])
def test_t233_cached_revocation_survives_double_write_failure(
    scenario: Scenario,
    source: catalog_updates.CatalogUpdateSource,
    revocation_service: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    damage: str,
) -> None:
    from astra_voice.app import _revoked_check
    from astra_voice.core.model_source import ModelRevoked, _not_revoked

    service = revocation_service
    entry = service.catalog.entries[0]
    install_fixture(service._store, entry, activate=False)
    current_path = service._store.root / "current.json"
    assert not current_path.exists()
    scenario.snapshot(scenario.document)
    raw = prepare_revoking_refresh(scenario, source, monkeypatch)
    real_write = update_cache.write_private
    calls = 0

    def fail_rollback(path: Path, payload: bytes) -> None:
        nonlocal calls
        calls += 1
        if calls > 1:
            raise OSError("сбой отката")
        real_write(path, payload)

    with monkeypatch.context() as fault, caplog.at_level(logging.WARNING):
        fault.setattr(catalog_state, "write_state", Mock(side_effect=OSError("диск заполнен")))
        fault.setattr(update_cache, "write_private", fail_rollback)
        with pytest.raises(CatalogError) as refused:
            source.refresh(threading.Event())
    assert refused.value.code == "state-write"
    assert source._blocked
    assert "Не удалось откатить снимок" in caplog.text
    assert str(scenario.snapshots) not in caplog.text
    assert catalog_effective.read_snapshot(scenario.snapshots) == (raw, signature(raw))
    # Быстрый путь обязан знать отзыв до первой активации после двойного сбоя.
    for model in (service, None):
        assert _revoked_check(model)(entry.id, entry.revision) is True
    cached = catalog_effective._snapshot_cache
    assert cached is not None and cached[1] is not None
    assert cached[1].is_revoked(entry.id, entry.revision)
    assert cached[0] == (
        hashlib.sha256(raw).hexdigest(),
        hashlib.sha256(signature(raw)).hexdigest(),
        scenario.root,
        hashlib.sha256((scenario.root / "catalog.schema.json").read_bytes()).hexdigest(),
    )
    verify_calls = scenario.verifier.calls
    snapshot_path = scenario.snapshots / catalog_effective.SNAPSHOT_FILE_NAME
    if damage == "eio":
        monkeypatch.setattr(catalog_effective, "read_snapshot", Mock(side_effect=OSError(5, "EIO")))
    elif damage == "json":
        snapshot_path.write_bytes(b"{")
    elif damage == "missing":
        snapshot_path.unlink()
    else:
        scenario.snapshot_bytes(b"{", b"bad signature")
    # Повреждение/ошибка чтения случились до полной активации: она не заполняла кэш.
    for model in (service, None):
        assert _revoked_check(model)(entry.id, entry.revision) is True
    with pytest.raises(StoreError) as second:
        service.set_current(entry.id, entry.revision)
    assert second.value.code == "revoked"
    assert not current_path.exists()
    snapshot_read = Mock(side_effect=AssertionError("Быстрый путь читает снимок"))
    monkeypatch.setattr(catalog_effective, "read_snapshot", snapshot_read)
    for model in (service, None):
        checker = _revoked_check(model)
        assert checker(entry.id, entry.revision) is True
        with pytest.raises(ModelRevoked):
            _not_revoked({"id": entry.id, "revision": entry.revision}, checker)
    assert scenario.verifier.calls == verify_calls
    snapshot_read.assert_not_called()


@pytest.mark.parametrize("marker_serial", [5, 6])
def test_t233_cached_revocation_not_newer_than_marker_is_ignored(
    scenario: Scenario, revocation_service: Any, marker_serial: int
) -> None:
    service = revocation_service
    entry = service.catalog.entries[0]
    install_fixture(service._store, entry)
    remote = scenario.remote()
    remote["revoked"] = [{"model_id": entry.id, "revision": entry.revision, "reason": "отзыв"}]
    scenario.snapshot(remote)
    with pytest.raises(StoreError, match="отозвана"):
        service.set_current(entry.id, entry.revision)
    accepted = scenario.remote(serial=marker_serial)
    raw = json.dumps(accepted).encode()
    catalog = verify_catalog(raw, signature(raw), scenario.verifier, schema_root=scenario.root)
    catalog_state.write_state(scenario.state, state_of(catalog, raw))
    service.set_current(entry.id, entry.revision)
    assert service.revoked_revision(entry.id, entry.revision) is False
    assert service._store.current().revision == entry.revision


@pytest.mark.parametrize("failure", ["io", "clock-behind", "timeout", "oserror"])
def test_t235_transient_snapshot_failure_is_retried(
    scenario: Scenario,
    revocation_service: Any,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    service = revocation_service
    entry = service.catalog.entries[0]
    install_fixture(service._store, entry)
    scenario.snapshot(scenario.remote())
    real_verify = scenario.verifier.verify_detached
    verify_calls = 0
    failing = True

    def verify(data: Path, sig: Path, *, cancel: Callable[[], bool] | None = None) -> VerifyResult:
        nonlocal verify_calls
        verify_calls += 1
        result = real_verify(data, sig, cancel=cancel)
        if failing:
            if failure == "io":
                raise CatalogError("io", "Временный сбой проверки")
            if failure == "oserror":
                raise OSError("Временный сбой временной папки gpgv")
            if failure == "clock-behind":
                return replace(result, ok=False, code="clock-behind")
            return replace(result, ok=False, code="", reason="gpgv не ответил за отведённое время")
        return result

    monkeypatch.setattr(scenario.verifier, "verify_detached", verify)
    with pytest.raises(StoreError) as first:
        service.set_current(entry.id, entry.revision)
    assert first.value.code == "catalog-unavailable"
    assert catalog_effective._snapshot_cache is None
    failing = False
    service.set_current(entry.id, entry.revision)
    assert verify_calls == 2
    assert service._store.current().revision == entry.revision


@pytest.mark.parametrize("failure", ["signature", "schema", "json"])
def test_t235_deterministic_snapshot_failure_is_cached(
    scenario: Scenario, revocation_service: Any, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    service = revocation_service
    entry = service.catalog.entries[0]
    install_fixture(service._store, entry)
    remote = scenario.remote()
    if failure == "schema":
        remote["schema_sha256"] = "0" * 64
    if failure == "json":
        raw = (json.dumps(remote)[:-1] + ', "serial": 5}').encode()
        scenario.snapshot_bytes(raw, signature(raw))
    else:
        scenario.snapshot(remote, valid_signature=failure != "signature")
    verify = Mock(wraps=scenario.verifier.verify_detached)
    monkeypatch.setattr(scenario.verifier, "verify_detached", verify)
    for _ in range(2):
        with pytest.raises(StoreError) as refused:
            service.set_current(entry.id, entry.revision)
        assert refused.value.code == "catalog-unavailable"
    assert verify.call_count == 1
    assert service.revoked_revision(entry.id, entry.revision) is False


def test_t235_temporary_directory_failure_is_retried(
    scenario: Scenario, revocation_service: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = revocation_service
    entry = service.catalog.entries[0]
    install_fixture(service._store, entry)
    scenario.snapshot(scenario.remote())
    verify = Mock(wraps=catalog_effective.verify_catalog)
    monkeypatch.setattr(catalog_effective, "verify_catalog", verify)
    with monkeypatch.context() as fault:
        fault.setattr(
            tempfile,
            "TemporaryDirectory",
            Mock(side_effect=OSError("Временная папка недоступна")),
        )
        with pytest.raises(StoreError) as refused:
            service.set_current(entry.id, entry.revision)
    assert refused.value.code == "catalog-unavailable"
    assert catalog_effective._snapshot_cache is None
    service.set_current(entry.id, entry.revision)
    assert verify.call_count == 2
    assert service._store.current().revision == entry.revision


@pytest.mark.parametrize("revoked", [False, True])
def test_t233_cached_snapshot_without_marker_keeps_unreadable_failure_closed(
    scenario: Scenario, revocation_service: Any, revoked: bool
) -> None:
    service = revocation_service
    entry = service.catalog.entries[0]
    install_fixture(service._store, entry)
    remote = scenario.remote()
    if revoked:
        remote["revoked"] = [{"model_id": entry.id, "revision": entry.revision, "reason": "отзыв"}]
    scenario.snapshot(remote)
    if revoked:
        with pytest.raises(StoreError, match="отозвана"):
            service.set_current(entry.id, entry.revision)
    else:
        service.set_current(entry.id, entry.revision)
    before = (service._store.root / "current.json").read_bytes()
    scenario.state.unlink()
    (scenario.snapshots / catalog_effective.SNAPSHOT_FILE_NAME).write_bytes(b"{")
    with pytest.raises(StoreError) as refused:
        service.set_current(entry.id, entry.revision)
    assert refused.value.code == ("revoked" if revoked else "catalog-unavailable")
    assert (service._store.root / "current.json").read_bytes() == before


@pytest.mark.parametrize("marker_damage", ["nonhex", "missing-revoked"])
@pytest.mark.parametrize("known_revocation", [False, True])
def test_t237_invalid_marker_does_not_hide_cached_revocation(
    scenario: Scenario,
    revocation_service: Any,
    marker_damage: str,
    known_revocation: bool,
) -> None:
    from astra_voice.app import _revoked_check
    from astra_voice.core.model_source import ModelRevoked, RevocationUnknown, _not_revoked

    entry = revocation_service.catalog.entries[0]
    if known_revocation:
        remote = scenario.remote()
        remote["revoked"] = [{"model_id": entry.id, "revision": entry.revision, "reason": "отзыв"}]
        scenario.snapshot(remote)
        with pytest.raises(StoreError, match="отозвана"):
            catalog_effective.check_revoked(entry.id, entry.revision, None, include_snapshot=True)
    document = json.loads(scenario.state.read_bytes())
    document.update(serial=6, sha256="a" * 64, revoked=[])
    if marker_damage == "nonhex":
        document["sha256"] = "z" * 64
    else:
        document.pop("revoked")
    scenario.state.write_text(json.dumps(document))
    checker = _revoked_check(None)
    if known_revocation:
        assert checker(entry.id, entry.revision) is True
    else:
        with pytest.raises(RevocationUnknown):
            checker(entry.id, entry.revision)
    request = {"id": entry.id, "revision": entry.revision}
    if known_revocation:
        with pytest.raises(ModelRevoked):
            _not_revoked(request, checker)
    else:
        unknown = Mock()
        assert _not_revoked(request, checker, unknown) is request
        unknown.assert_called_once_with()
    for include_snapshot in (False, True):
        with pytest.raises(StoreError) as refused:
            catalog_effective.check_revoked(
                entry.id, entry.revision, None, include_snapshot=include_snapshot
            )
        assert refused.value.code == ("revoked" if known_revocation else "catalog-unavailable")

    # Валидная новая отметка без X подавляет старый положительный кэш.
    document.update(sha256="a" * 64, revoked=[])
    scenario.state.write_text(json.dumps(document))
    assert checker(entry.id, entry.revision) is False
    request = {"id": entry.id, "revision": entry.revision}
    assert _not_revoked(request, checker) is request
    catalog_effective.check_revoked(entry.id, entry.revision, None, include_snapshot=True)


@pytest.mark.parametrize(
    "cached_pair,incoming_pair", [((1, 5), (1, 4)), ((1, 5), (1, 5)), ((2, 1), (1, 999))]
)
@pytest.mark.parametrize("remember_directly", [False, True])
def test_t237_older_verified_snapshot_preserves_positive_cache(
    scenario: Scenario,
    revocation_service: Any,
    cached_pair: tuple[int, int],
    incoming_pair: tuple[int, int],
    remember_directly: bool,
) -> None:
    from astra_voice.app import _revoked_check

    entry = revocation_service.catalog.entries[0]
    remote = scenario.remote(epoch=cached_pair[0], serial=cached_pair[1])
    remote["revoked"] = [{"model_id": entry.id, "revision": entry.revision, "reason": "отзыв"}]
    scenario.snapshot(remote)
    with catalog_effective.CATALOG_LOCK:
        cached, unknown = catalog_effective._pending_snapshot(None)
        assert cached is not None and not unknown
        positive = catalog_effective._snapshot_cache
        incoming = scenario.remote(epoch=incoming_pair[0], serial=incoming_pair[1])
        raw = scenario.snapshot(incoming)
        if remember_directly:
            verified = verify_catalog(
                raw, signature(raw), scenario.verifier, schema_root=scenario.root
            )
            catalog_effective.remember_verified_snapshot(raw, signature(raw), verified)
        else:
            verified, unknown = catalog_effective._pending_snapshot(None)
            assert verified is not None and not unknown
        assert (verified.trust_epoch, verified.serial) == incoming_pair
        assert not verified.is_revoked(entry.id, entry.revision)
        assert catalog_effective._snapshot_cache is positive
        assert catalog_effective.cached_pending_snapshot(None) is cached
        assert cached.is_revoked(entry.id, entry.revision)
        assert _revoked_check(None)(entry.id, entry.revision) is True


@pytest.mark.parametrize("error_type", [MemoryError, KeyboardInterrupt, ValueError])
@pytest.mark.parametrize("previous_snapshot", [False, True])
@pytest.mark.parametrize("rollback_fails", [False, True])
def test_t238_any_publication_failure_rolls_back_or_remembers_revocation(
    scenario: Scenario,
    source: catalog_updates.CatalogUpdateSource,
    revocation_service: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    error_type: type[BaseException],
    previous_snapshot: bool,
    rollback_fails: bool,
) -> None:
    from astra_voice.app import _revoked_check

    entry = revocation_service.catalog.entries[0]
    if previous_snapshot:
        scenario.snapshot(scenario.document)
    raw = prepare_revoking_refresh(scenario, source, monkeypatch)
    snapshot_path = scenario.snapshots / catalog_effective.SNAPSHOT_FILE_NAME
    snapshot_before = snapshot_path.read_bytes() if previous_snapshot else None
    marker_before = scenario.state.read_bytes()
    accepted, accepted_state = source._catalog, source._accepted
    real_write, real_unlink = update_cache.write_private, Path.unlink
    failure = error_type("сбой публикации")
    calls = 0

    def write_snapshot(path: Path, payload: bytes) -> None:
        nonlocal calls
        calls += 1
        if rollback_fails and calls > 1:
            raise OSError("сбой отката")
        real_write(path, payload)

    def unlink_snapshot(path: Path, missing_ok: bool = False) -> None:
        if rollback_fails and path == snapshot_path:
            raise OSError("сбой отката")
        real_unlink(path, missing_ok=missing_ok)

    with monkeypatch.context() as fault, caplog.at_level(logging.WARNING):
        fault.setattr(catalog_state, "apply_state", Mock(side_effect=failure))
        fault.setattr(update_cache, "write_private", write_snapshot)
        fault.setattr(Path, "unlink", unlink_snapshot)
        with pytest.raises(CatalogError if error_type is ValueError else error_type) as refused:
            source.refresh(threading.Event())
    if error_type is ValueError:
        assert isinstance(refused.value, CatalogError) and refused.value.code == "stale-catalog"
    else:
        assert refused.value is failure
    assert scenario.state.read_bytes() == marker_before
    assert source._catalog is accepted and source._accepted == accepted_state
    assert source._blocked is rollback_fails
    if rollback_fails:
        assert "Не удалось откатить снимок" in caplog.text
        assert catalog_effective.read_snapshot(scenario.snapshots) == (raw, signature(raw))
        snapshot_path.write_bytes(b"{")
        assert _revoked_check(None)(entry.id, entry.revision) is True
        with pytest.raises(StoreError) as revoked:
            catalog_effective.check_revoked(entry.id, entry.revision, None, include_snapshot=True)
        assert revoked.value.code == "revoked"
    else:
        assert (snapshot_path.read_bytes() if snapshot_path.exists() else None) == snapshot_before
        assert catalog_effective.cached_pending_snapshot(None) is None
        assert source.current() is accepted


@pytest.mark.parametrize("error_type", [MemoryError, KeyboardInterrupt, ValueError])
def test_t238_failure_after_marker_replace_preserves_accepted_pair(
    scenario: Scenario,
    source: catalog_updates.CatalogUpdateSource,
    monkeypatch: pytest.MonkeyPatch,
    error_type: type[BaseException],
) -> None:
    raw = prepare_revoking_refresh(scenario, source, monkeypatch)
    apply_state = catalog_state.apply_state
    failure = error_type("сбой после замены отметки")

    def apply_then_fail(path: Path, state: catalog_state.CatalogState) -> None:
        apply_state(path, state)
        raise failure

    with monkeypatch.context() as fault:
        fault.setattr(catalog_state, "apply_state", apply_then_fail)
        with pytest.raises(CatalogError if error_type is ValueError else error_type) as refused:
            source.refresh(threading.Event())
    if error_type is ValueError:
        assert isinstance(refused.value, CatalogError) and refused.value.code == "stale-catalog"
    else:
        assert refused.value is failure
    assert not source._blocked
    assert source.current().serial == 5
    assert catalog_state.read_state(scenario.state) == source._accepted
    assert catalog_effective.read_snapshot(scenario.snapshots) == (raw, signature(raw))


@pytest.mark.parametrize(
    "cache_error", [OSError, ValueError, RuntimeError, MemoryError, KeyboardInterrupt]
)
@pytest.mark.parametrize("publication_error", [OSError, MemoryError, KeyboardInterrupt, ValueError])
def test_t238_remember_failure_does_not_mask_publication_failure(
    scenario: Scenario,
    source: catalog_updates.CatalogUpdateSource,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    cache_error: type[BaseException],
    publication_error: type[BaseException],
) -> None:
    prepare_revoking_refresh(scenario, source, monkeypatch)
    snapshot_path = scenario.snapshots / catalog_effective.SNAPSHOT_FILE_NAME
    original_unlink = Path.unlink
    failure = publication_error("исходный сбой")

    def fail_unlink(path: Path, missing_ok: bool = False) -> None:
        if path == snapshot_path:
            raise OSError("сбой отката")
        original_unlink(path, missing_ok=missing_ok)

    with monkeypatch.context() as fault, caplog.at_level(logging.WARNING):
        fault.setattr(catalog_state, "apply_state", Mock(side_effect=failure))
        fault.setattr(Path, "unlink", fail_unlink)
        fault.setattr(catalog_updates, "remember_verified_snapshot", Mock(side_effect=cache_error))
        with pytest.raises(
            CatalogError if publication_error in (OSError, ValueError) else publication_error
        ) as refused:
            source.refresh(threading.Event())
    if publication_error in (OSError, ValueError):
        expected_code = "state-write" if publication_error is OSError else "stale-catalog"
        assert isinstance(refused.value, CatalogError) and refused.value.code == expected_code
    else:
        assert refused.value is failure
    assert source._blocked
    assert "Не удалось сохранить проверенные отзывы" in caplog.text
