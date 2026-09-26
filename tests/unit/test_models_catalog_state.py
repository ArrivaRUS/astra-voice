"""Анти-откат каталога: применённое состояние и отказ более старому списку."""

from __future__ import annotations

import hashlib
import json
import logging
import stat
from pathlib import Path

import pytest

from astra_voice.models import catalog_state
from astra_voice.models.catalog import CatalogError, RevokedEntry, _apply_state, load_builtin
from astra_voice.models.catalog_state import CatalogState

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_ROOT = REPO_ROOT / "data"
KEYRING = DATA_ROOT / "keys/release.gpg"
SHA_A = "a" * 64
SHA_B = "b" * 64


@pytest.fixture
def state_file(tmp_path: Path) -> Path:
    return catalog_state.state_path(tmp_path)


def read_raw(path: Path) -> dict[str, object]:
    document: dict[str, object] = json.loads(path.read_text(encoding="utf-8"))
    return document


def test_first_catalog_is_recorded(state_file: Path) -> None:
    assert catalog_state.apply_state(state_file, CatalogState(1, 2, SHA_A)) is True
    assert read_raw(state_file) == {"trust_epoch": 1, "serial": 2, "sha256": SHA_A}
    assert stat.S_IMODE(state_file.stat().st_mode) == 0o600
    assert catalog_state.read_state(state_file) == CatalogState(1, 2, SHA_A)


def test_same_catalog_is_not_rewritten(state_file: Path) -> None:
    catalog_state.apply_state(state_file, CatalogState(1, 2, SHA_A))
    before = state_file.stat().st_mtime_ns
    assert catalog_state.apply_state(state_file, CatalogState(1, 2, SHA_A)) is False
    assert state_file.stat().st_mtime_ns == before


def test_same_catalog_migrates_missing_snapshot(state_file: Path) -> None:
    catalog_state.write_state(state_file, CatalogState(1, 2, SHA_A))
    candidate = CatalogState(1, 2, SHA_A, (("model", "r1"),))
    assert catalog_state.apply_state(state_file, candidate) is True
    assert catalog_state.read_state(state_file) == candidate
    assert read_raw(state_file)["revoked"] == [{"model_id": "model", "revision": "r1"}]
    assert catalog_state.apply_state(state_file, candidate) is False


def test_same_catalog_replaces_different_snapshot(state_file: Path) -> None:
    old = CatalogState(1, 2, SHA_A, (("old", "r1"),))
    candidate = CatalogState(1, 2, SHA_A, (("new", "r2"),))
    catalog_state.write_state(state_file, old)
    assert catalog_state.apply_state(state_file, candidate) is True
    assert catalog_state.read_state(state_file) == candidate
    assert catalog_state.apply_state(state_file, candidate) is False


def test_same_catalog_repairs_bad_snapshot(state_file: Path) -> None:
    state_file.write_text(
        json.dumps({"trust_epoch": 1, "serial": 2, "sha256": SHA_A, "revoked": "broken"}),
        encoding="utf-8",
    )
    candidate = CatalogState(1, 2, SHA_A, (("model", "r1"),))
    assert catalog_state.apply_state(state_file, candidate) is True
    assert catalog_state.read_state(state_file) == candidate


def test_newer_serial_is_applied(state_file: Path) -> None:
    catalog_state.apply_state(state_file, CatalogState(1, 2, SHA_A))
    assert catalog_state.apply_state(state_file, CatalogState(1, 3, SHA_B)) is True
    assert catalog_state.read_state(state_file) == CatalogState(1, 3, SHA_B)


def test_newer_trust_epoch_wins_over_smaller_serial(state_file: Path) -> None:
    catalog_state.apply_state(state_file, CatalogState(1, 9, SHA_A))
    assert catalog_state.apply_state(state_file, CatalogState(2, 1, SHA_B)) is True
    assert catalog_state.read_state(state_file) == CatalogState(2, 1, SHA_B)


@pytest.mark.parametrize(
    "candidate",
    [
        CatalogState(1, 1, SHA_B),
        CatalogState(1, 2, SHA_B),
        pytest.param(CatalogState(1, 2, SHA_A), marks=pytest.mark.skip("это тот же каталог")),
    ],
    ids=["меньший-серийный", "тот-же-серийный-другие-байты", "тот-же"],
)
def test_older_catalog_is_rejected(state_file: Path, candidate: CatalogState) -> None:
    catalog_state.apply_state(state_file, CatalogState(1, 2, SHA_A))
    with pytest.raises(ValueError, match="устарел"):
        catalog_state.apply_state(state_file, candidate)
    assert catalog_state.read_state(state_file) == CatalogState(1, 2, SHA_A)


def test_older_trust_epoch_is_rejected(state_file: Path) -> None:
    catalog_state.apply_state(state_file, CatalogState(3, 1, SHA_A))
    with pytest.raises(ValueError, match="устарел"):
        catalog_state.apply_state(state_file, CatalogState(2, 99, SHA_B))


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        "не json".encode(),
        b"[]",
        json.dumps({"trust_epoch": 1, "serial": 1}).encode("utf-8"),
        json.dumps({"trust_epoch": 0, "serial": 1, "sha256": SHA_A}).encode("utf-8"),
        json.dumps({"trust_epoch": True, "serial": 1, "sha256": SHA_A}).encode("utf-8"),
        json.dumps({"trust_epoch": 1, "serial": 1, "sha256": "коротко"}).encode("utf-8"),
    ],
    ids=["пусто", "мусор", "список", "без-суммы", "нулевая-эпоха", "логическое", "короткая-сумма"],
)
def test_broken_state_is_ignored(state_file: Path, payload: bytes) -> None:
    state_file.write_bytes(payload)
    assert catalog_state.read_state(state_file) is None
    assert catalog_state.apply_state(state_file, CatalogState(1, 1, SHA_A)) is True


def test_oversized_state_is_ignored(state_file: Path) -> None:
    state_file.write_bytes(b" " * (catalog_state.STATE_MAX_BYTES + 1))
    assert catalog_state.read_state(state_file) is None


@pytest.mark.parametrize(
    "revoked",
    [
        None,
        {},
        [{"model_id": "model"}],
        [{"model_id": "model", "revision": "r1", "reason": "extra"}],
        [{"model_id": 1, "revision": "r1"}],
        [{"model_id": "model", "revision": "не-id"}],
        [{"model_id": "a" * 65, "revision": "r1"}],
        [{"model_id": "m", "revision": "r"}] * 1025,
    ],
)
def test_bad_snapshot_preserves_rollback_state(
    state_file: Path, revoked: object, caplog: pytest.LogCaptureFixture
) -> None:
    state_file.write_text(
        json.dumps({"trust_epoch": 2, "serial": 3, "sha256": SHA_A, "revoked": revoked}),
        encoding="utf-8",
    )
    with caplog.at_level(logging.WARNING, logger=catalog_state.__name__):
        assert catalog_state.read_state(state_file) == CatalogState(2, 3, SHA_A)
    assert sum("Снимок отозванных версий" in row.message for row in caplog.records) == 1
    with pytest.raises(ValueError, match="устарел"):
        catalog_state.apply_state(state_file, CatalogState(2, 2, SHA_B))


def test_maximum_snapshot_fits_state_limit(state_file: Path) -> None:
    pairs = (("a" * 64, "b" * 64),) * 1024
    catalog_state.write_state(state_file, CatalogState(1, 1, SHA_A, pairs))
    assert state_file.stat().st_size <= catalog_state.STATE_MAX_BYTES
    assert catalog_state.read_state(state_file) == CatalogState(1, 1, SHA_A, pairs)


def test_catalog_application_records_revoked_pairs_without_reason(state_file: Path) -> None:
    raw = b"signed catalog bytes"
    _apply_state(state_file, raw, 3, 2, (RevokedEntry("model", "r1", "private reason"),))
    assert catalog_state.read_state(state_file) == CatalogState(
        2, 3, hashlib.sha256(raw).hexdigest(), (("model", "r1"),)
    )
    assert "private reason" not in state_file.read_text(encoding="utf-8")


def test_state_directory_instead_of_file_is_ignored(state_file: Path) -> None:
    state_file.mkdir()
    assert catalog_state.read_state(state_file) is None


def test_load_builtin_records_and_then_rejects_rollback(tmp_path: Path) -> None:
    """Первый разбор запоминает каталог, откат на меньший серийный — отказ."""
    from astra_voice.security.verify import Verifier

    state_file = catalog_state.state_path(tmp_path)
    verifier = Verifier("catalog", keyring=KEYRING)
    catalog = load_builtin(verifier, root=DATA_ROOT, state_path=state_file)
    raw = (DATA_ROOT / "catalog.json").read_bytes()
    assert catalog_state.read_state(state_file) == CatalogState(
        catalog.trust_epoch,
        catalog.serial,
        hashlib.sha256(raw).hexdigest(),
        tuple((item.model_id, item.revision) for item in catalog.revoked),
    )
    # Тот же каталог принимается повторно: состояние не меняется.
    assert load_builtin(verifier, root=DATA_ROOT, state_path=state_file).serial == catalog.serial

    catalog_state.write_state(
        state_file, CatalogState(catalog.trust_epoch, catalog.serial + 1, SHA_B)
    )
    with pytest.raises(CatalogError) as failure:
        load_builtin(verifier, root=DATA_ROOT, state_path=state_file)
    assert failure.value.code == "stale-catalog"
    assert "устарел" in failure.value.message


def test_load_builtin_survives_unwritable_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Не записать состояние — не повод оставить человека без списка моделей."""
    from astra_voice.security.verify import Verifier

    def refuse(path: Path, state: CatalogState) -> None:
        raise OSError("только чтение")

    monkeypatch.setattr(catalog_state, "write_state", refuse)
    catalog = load_builtin(
        Verifier("catalog", keyring=KEYRING),
        root=DATA_ROOT,
        state_path=catalog_state.state_path(tmp_path),
    )
    assert catalog.entries
