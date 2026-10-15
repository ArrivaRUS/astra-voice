"""Б-1: общий каталог/отзывы после выбора источника и после перезапуска."""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import logging
import stat
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
    with monkeypatch.context() as failed_marker:
        failed_marker.setattr(catalog_state, "write_state", Mock(side_effect=OSError("disk full")))
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


def install_fixture(store: ModelStore, entry: CatalogEntry) -> None:
    staging = store.staging_dir(entry.id, entry.revision)
    for name, raw in CONTENTS.items():
        (staging / name).write_bytes(raw)
    result = Installer(store, lambda _directory, _entry: SmokeResult(True)).install_from_staging(
        entry
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
