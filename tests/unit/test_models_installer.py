"""Установщик О2: проверка байтов, пробное распознавание и сохранение текущей модели."""

from __future__ import annotations

import errno
import hashlib
import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest

from astra_voice.models import installer as inst
from astra_voice.models.catalog import CatalogEntry, FileSpec
from astra_voice.models.installer import Installer, SmokeResult
from astra_voice.models.store import ModelRecord, ModelStore, StoreError

pytestmark = pytest.mark.unit


@pytest.fixture
def contents() -> dict[str, bytes]:
    return {
        "v3_e2e_ctc.int8.onnx": b"\x08\x03\x12\x04test",
        "v3_e2e_ctc_vocab.txt": "а\nб\n".encode(),
        "config.json": b'{"model_type":"gigaam"}',
    }


@pytest.fixture
def entry(contents: dict[str, bytes]) -> CatalogEntry:
    return CatalogEntry(
        id="gigaam-v3-e2e-ctc-int8",
        revision="rev-new",
        name="Модель для проверки",
        description="Крошечные файлы настоящей раскладки CTC.",
        size_bytes=sum(len(data) for data in contents.values()),
        min_ram_mb=1,
        layout="onnx-asr-gigaam-v3",
        variant="gigaam-v3-e2e-ctc",
        recommended=True,
        host="huggingface.co",
        files=tuple(
            FileSpec(name, hashlib.sha256(data).hexdigest(), len(data), f"/rev-new/{name}")
            for name, data in contents.items()
        ),
    )


@pytest.fixture
def store(tmp_path: Path) -> ModelStore:
    return ModelStore(tmp_path)


@pytest.fixture
def smoke() -> Mock:
    return Mock(return_value=SmokeResult(True, text="Проверка прошла"))


def _write_files(directory: Path, contents: dict[str, bytes]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    for name, data in contents.items():
        (directory / name).write_bytes(data)
    return directory


def _stage(store: ModelStore, entry: CatalogEntry, contents: dict[str, bytes]) -> Path:
    return _write_files(store.staging_dir(entry.id, entry.revision), contents)


def _previous(store: ModelStore, entry: CatalogEntry, contents: dict[str, bytes]) -> ModelRecord:
    old_entry = replace(entry, revision="rev-old")
    _stage(store, old_entry, contents)
    result = Installer(store, lambda directory, model: SmokeResult(True)).install_from_staging(
        old_entry
    )
    assert result.state == "ok"
    assert result.record is not None
    return result.record


def _flip_byte(file: Path) -> None:
    data = bytearray(file.read_bytes())
    data[0] ^= 1
    file.write_bytes(data)


def _pointer(store: ModelStore) -> bytes | None:
    path = store.root / "current.json"
    return path.read_bytes() if path.exists() else None


def test_success_obeys_o2_order(
    store: ModelStore,
    entry: CatalogEntry,
    contents: dict[str, bytes],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    staging = _stage(store, entry, contents)
    installed = store.root / entry.id / entry.revision
    inode = staging.stat().st_ino

    def check_smoke(directory: Path, model: CatalogEntry) -> SmokeResult:
        assert directory == installed
        assert model == entry
        assert directory.is_dir()
        assert not staging.exists()
        assert store.current() is None
        return SmokeResult(True, text="Пробная фраза")

    smoke = Mock(side_effect=check_smoke)
    calls = Mock()
    for owner, name, label in (
        (inst, "sha256_file", "sha256"),
        (inst, "check_layout", "layout"),
        (store, "commit", "commit"),
        (store, "set_current", "current"),
    ):
        wrapped = Mock(wraps=getattr(owner, name))
        calls.attach_mock(wrapped, label)
        monkeypatch.setattr(owner, name, wrapped)
    calls.attach_mock(smoke, "smoke")

    result = Installer(store, smoke).install_from_staging(entry)

    assert result.state == "ok"
    assert result.reason == ""
    assert result.record == ModelRecord(
        entry.id, entry.revision, installed, entry.layout, entry.variant, entry.size_bytes
    )
    assert store.current() == result.record
    assert store.records() == (result.record,)
    assert installed.stat().st_ino == inode
    assert not staging.exists()
    for name, data in contents.items():
        assert (installed / name).read_bytes() == data
    smoke.assert_called_once_with(installed, entry)
    assert [call[0] for call in calls.mock_calls] == [
        "sha256",
        "sha256",
        "sha256",
        "layout",
        "commit",
        "smoke",
        "current",
    ]
    assert [call.args[0] for call in calls.sha256.call_args_list] == [
        staging / file.path for file in entry.files
    ]


@pytest.mark.parametrize("file_index", [0, 1, 2])
def test_changed_byte_keeps_staging_and_current(
    store: ModelStore,
    entry: CatalogEntry,
    contents: dict[str, bytes],
    smoke: Mock,
    monkeypatch: pytest.MonkeyPatch,
    file_index: int,
) -> None:
    previous = _previous(store, entry, contents)
    before = _pointer(store)
    staging = _stage(store, entry, contents)
    damaged = staging / entry.files[file_index].path
    _flip_byte(damaged)
    altered = damaged.read_bytes()
    commit = Mock(wraps=store.commit)
    current = Mock(wraps=store.set_current)
    monkeypatch.setattr(store, "commit", commit)
    monkeypatch.setattr(store, "set_current", current)

    result = Installer(store, smoke).install_from_staging(entry)

    assert result.state == "error"
    assert "контрольная сумма" in result.reason
    assert result.record is None
    assert staging.is_dir()
    assert damaged.read_bytes() == altered
    assert not (store.root / entry.id / entry.revision).exists()
    assert _pointer(store) == before
    assert store.current() == previous
    assert store.records() == (previous,)
    commit.assert_not_called()
    current.assert_not_called()
    smoke.assert_not_called()


@pytest.mark.parametrize("had_current", [False, True])
@pytest.mark.parametrize("smoke_failure", ["result", "exception", "empty-reason"])
def test_smoke_failure_marks_broken_and_preserves_current(
    store: ModelStore,
    entry: CatalogEntry,
    contents: dict[str, bytes],
    monkeypatch: pytest.MonkeyPatch,
    had_current: bool,
    smoke_failure: str,
) -> None:
    previous = _previous(store, entry, contents) if had_current else None
    before = _pointer(store)
    staging = _stage(store, entry, contents)
    reason = "Модель не распознала пробную фразу."
    if smoke_failure == "exception":
        smoke = Mock(side_effect=RuntimeError(f"Ошибка ONNX в {staging}/internal.onnx"))
    else:
        smoke = Mock(
            return_value=SmokeResult(False, reason=reason if smoke_failure == "result" else "")
        )
    current = Mock(wraps=store.set_current)
    monkeypatch.setattr(store, "set_current", current)

    result = Installer(store, smoke).install_from_staging(entry)

    installed = store.root / entry.id / entry.revision
    assert result.state == "broken"
    assert result.reason
    assert result.record == ModelRecord(
        entry.id,
        entry.revision,
        installed,
        entry.layout,
        entry.variant,
        entry.size_bytes,
        "broken",
        result.reason,
    )
    if smoke_failure == "result":
        assert result.reason == reason
    assert str(staging) not in result.reason
    assert "ONNX" not in result.reason
    assert "internal.onnx" not in result.reason
    assert installed.is_dir()
    assert not staging.exists()
    state = json.loads((installed / "state.json").read_text())
    assert state["state"] == "broken"
    assert state["reason"] == result.reason
    assert result.record in store.records()
    assert store.current() == previous
    assert _pointer(store) == before
    current.assert_not_called()
    smoke.assert_called_once_with(installed, entry)


@pytest.mark.parametrize("extra_name", ["лишний.txt", "v3_e2e_ctc.yaml", "state.json"])
def test_extra_staged_file_is_rejected(
    store: ModelStore,
    entry: CatalogEntry,
    contents: dict[str, bytes],
    smoke: Mock,
    extra_name: str,
) -> None:
    staging = _stage(store, entry, contents)
    (staging / extra_name).write_bytes(b"extra")

    result = Installer(store, smoke).install_from_staging(entry)

    assert result.state == "error"
    assert "лишние" in result.reason
    assert extra_name not in result.reason
    assert staging.exists()
    assert store.records() == ()
    assert _pointer(store) is None
    smoke.assert_not_called()


def test_temporary_parts_are_removed_before_layout_check(
    store: ModelStore, entry: CatalogEntry, contents: dict[str, bytes], smoke: Mock
) -> None:
    staging = _stage(store, entry, contents)
    (staging / (entry.files[0].path + ".part")).write_bytes(b"partial")

    result = Installer(store, smoke).install_from_staging(entry)

    assert result.state == "ok"
    assert result.record is not None
    assert {file.name for file in result.record.dir.iterdir()} == {*contents, "state.json"}


@pytest.mark.parametrize("missing_staging", [False, True])
def test_missing_staging_or_required_file(
    store: ModelStore,
    entry: CatalogEntry,
    contents: dict[str, bytes],
    smoke: Mock,
    missing_staging: bool,
) -> None:
    if not missing_staging:
        staging = _stage(store, entry, contents)
        (staging / entry.files[-1].path).unlink()

    result = Installer(store, smoke).install_from_staging(entry)

    assert result.state == "error"
    assert result.reason
    assert store.records() == ()
    if missing_staging:
        assert not (store.root / entry.id).exists()
    smoke.assert_not_called()


@pytest.mark.parametrize("mismatch", ["layout", "variant", "missing-required", "extra-listed"])
def test_real_layout_check_rejects_inconsistent_catalog(
    store: ModelStore,
    entry: CatalogEntry,
    contents: dict[str, bytes],
    smoke: Mock,
    mismatch: str,
) -> None:
    payload = dict(contents)
    if mismatch == "layout":
        entry = replace(entry, layout="unknown-layout")
    elif mismatch == "variant":
        entry = replace(entry, variant="unknown-variant")
    elif mismatch == "missing-required":
        payload.pop(entry.files[-1].path)
        entry = replace(entry, files=entry.files[:-1])
    else:
        payload["extra.bin"] = b"extra"
        extra = FileSpec("extra.bin", hashlib.sha256(b"extra").hexdigest(), 5, "/rev-new/extra.bin")
        entry = replace(entry, files=(*entry.files, extra))
    staging = _stage(store, entry, payload)

    result = Installer(store, smoke).install_from_staging(entry)

    assert result.state == "error"
    assert result.reason
    assert "unknown" not in result.reason
    assert "config.json" not in result.reason
    assert staging.exists()
    assert store.records() == ()
    smoke.assert_not_called()


def test_install_from_folder(
    tmp_path: Path,
    store: ModelStore,
    entry: CatalogEntry,
    contents: dict[str, bytes],
    smoke: Mock,
) -> None:
    source = _write_files(tmp_path / "source", contents)

    result = Installer(store, smoke).install_from_path(source, entry)

    assert result.state == "ok"
    assert result.record == store.current()
    assert result.record is not None
    assert result.record.layout == entry.layout
    assert not (store.root / entry.id / f"{entry.revision}.partial").exists()
    for name, data in contents.items():
        assert (source / name).read_bytes() == data
        assert (result.record.dir / name).read_bytes() == data
    smoke.assert_called_once_with(result.record.dir, entry)


@pytest.mark.parametrize("extra_name", ["лишний.txt", "weights.part"])
def test_local_extra_file_is_rejected_before_copying(
    tmp_path: Path,
    store: ModelStore,
    entry: CatalogEntry,
    contents: dict[str, bytes],
    smoke: Mock,
    extra_name: str,
) -> None:
    source = _write_files(tmp_path / "source", contents)
    (source / extra_name).write_bytes(b"extra")

    result = Installer(store, smoke).install_from_path(source, entry)

    assert result.state == "error"
    assert "лишние" in result.reason
    assert not (store.root / entry.id).exists()
    smoke.assert_not_called()


@pytest.mark.parametrize("source_kind", ["file", "missing", "unknown-model", "missing-file"])
def test_invalid_local_source(
    tmp_path: Path,
    store: ModelStore,
    entry: CatalogEntry,
    contents: dict[str, bytes],
    smoke: Mock,
    source_kind: str,
) -> None:
    source = tmp_path / "source"
    if source_kind == "file":
        source.write_bytes(b"archive")
    elif source_kind == "missing-file":
        _write_files(source, contents)
        (source / entry.files[-1].path).unlink()

    result = Installer(store, smoke).install_from_path(
        source, None if source_kind == "unknown-model" else entry
    )

    assert result.state == "error"
    assert result.reason
    if source_kind == "file":
        assert result.reason == "Выберите папку с файлами модели."
    elif source_kind == "unknown-model":
        assert result.reason == "Не удалось определить, какая это модель. Выберите её в списке."
    assert not (store.root / entry.id).exists()
    smoke.assert_not_called()


@pytest.mark.parametrize("damage", ["changed-byte", "too-large", "too-small"])
def test_bad_local_bytes_clear_staging_and_keep_current(
    tmp_path: Path,
    store: ModelStore,
    entry: CatalogEntry,
    contents: dict[str, bytes],
    smoke: Mock,
    damage: str,
) -> None:
    previous = _previous(store, entry, contents)
    before = _pointer(store)
    source = _write_files(tmp_path / "source", contents)
    # Последний файл: очистка должна убрать и уже скопированные целые файлы.
    damaged = source / entry.files[-1].path
    if damage == "changed-byte":
        _flip_byte(damaged)
    elif damage == "too-large":
        damaged.write_bytes(damaged.read_bytes() + b"x")
    else:
        damaged.write_bytes(damaged.read_bytes()[:-1])
    altered = damaged.read_bytes()

    result = Installer(store, smoke).install_from_path(source, entry)

    assert result.state == "error"
    assert "контрольная сумма" in result.reason
    assert not (store.root / entry.id / f"{entry.revision}.partial").exists()
    assert not (store.root / entry.id / entry.revision).exists()
    assert damaged.read_bytes() == altered
    assert store.current() == previous
    assert _pointer(store) == before
    smoke.assert_not_called()


def test_disk_full_does_not_copy_or_create_staging(
    tmp_path: Path,
    store: ModelStore,
    entry: CatalogEntry,
    contents: dict[str, bytes],
    smoke: Mock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _write_files(tmp_path / "source", contents)
    disk_ok = Mock(return_value=False)
    copy = Mock(wraps=inst._copy_file)
    staging = Mock(wraps=store.staging_dir)
    monkeypatch.setattr(store, "disk_ok", disk_ok)
    monkeypatch.setattr(store, "staging_dir", staging)
    monkeypatch.setattr(inst, "_copy_file", copy)

    result = Installer(store, smoke).install_from_path(source, entry)

    assert result.state == "error"
    assert "мест" in result.reason and "диск" in result.reason
    disk_ok.assert_called_once_with(entry.size_bytes)
    copy.assert_not_called()
    staging.assert_not_called()
    smoke.assert_not_called()
    assert not (store.root / entry.id).exists()


def test_recovery_then_fresh_install_has_no_garbage(
    store: ModelStore, entry: CatalogEntry, contents: dict[str, bytes], smoke: Mock
) -> None:
    staging = store.staging_dir(entry.id, entry.revision)
    _write_files(staging / "garbage", {"junk.part": b"interrupted"})

    assert store.recover_incomplete() == (f"{entry.id}/{entry.revision}",)
    assert not staging.exists()
    _stage(store, entry, contents)
    result = Installer(store, smoke).install_from_staging(entry)

    assert result.state == "ok"
    assert result.record is not None
    assert {file.name for file in result.record.dir.iterdir()} == {*contents, "state.json"}
    assert not staging.exists()


def test_upgrade_preserves_old_revision_and_switches_after_smoke(
    store: ModelStore, entry: CatalogEntry, contents: dict[str, bytes]
) -> None:
    previous = _previous(store, entry, contents)
    before = _pointer(store)
    old_files = {file.name: file.read_bytes() for file in previous.dir.iterdir()}
    staging = _stage(store, entry, contents)

    def check_smoke(directory: Path, model: CatalogEntry) -> SmokeResult:
        assert directory == store.root / entry.id / entry.revision
        assert model == entry
        assert not staging.exists()
        assert directory.is_dir()
        assert store.current() == previous
        assert _pointer(store) == before
        return SmokeResult(True)

    smoke = Mock(side_effect=check_smoke)
    result = Installer(store, smoke).install_from_staging(entry)

    assert result.state == "ok"
    assert result.record == store.current()
    assert result.record is not None
    assert {record.revision for record in store.records()} == {previous.revision, entry.revision}
    assert {file.name: file.read_bytes() for file in previous.dir.iterdir()} == old_files
    assert _pointer(store) != before
    smoke.assert_called_once_with(result.record.dir, entry)


@pytest.mark.parametrize("attack", ["source-link", "destination-link", "traversal", "absolute"])
def test_unsafe_paths_never_overwrite_outside_files(
    tmp_path: Path,
    store: ModelStore,
    entry: CatalogEntry,
    contents: dict[str, bytes],
    smoke: Mock,
    attack: str,
) -> None:
    source = _write_files(tmp_path / "source", contents)
    outside = tmp_path / "outside"
    outside.write_bytes(b"keep")
    spec = entry.files[0]
    if attack == "source-link":
        (source / spec.path).unlink()
        (source / spec.path).symlink_to(outside)
    elif attack == "destination-link":
        staging = store.staging_dir(entry.id, entry.revision)
        (staging / spec.path).symlink_to(outside)
    else:
        unsafe = "../outside" if attack == "traversal" else str(outside)
        entry = replace(entry, files=(replace(spec, path=unsafe), *entry.files[1:]))

    result = Installer(store, smoke).install_from_path(source, entry)

    assert result.state == "error"
    assert outside.read_bytes() == b"keep"
    assert not (store.root / entry.id / entry.revision).exists()
    smoke.assert_not_called()


@pytest.mark.parametrize("fault", ["commit", "copy", "activate"])
def test_io_errors_are_reported_without_internal_details(
    tmp_path: Path,
    store: ModelStore,
    entry: CatalogEntry,
    contents: dict[str, bytes],
    smoke: Mock,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    previous = _previous(store, entry, contents)
    before = _pointer(store)
    source = _write_files(tmp_path / "source", contents)
    if fault == "copy":
        monkeypatch.setattr(
            inst, "_copy_file", Mock(side_effect=OSError(errno.ENOSPC, f"Запись в {source}"))
        )
    else:
        monkeypatch.setattr(
            store,
            "commit" if fault == "commit" else "set_current",
            Mock(side_effect=StoreError("broken-store", f"state.json: {source}")),
        )

    result = Installer(store, smoke).install_from_path(source, entry)

    assert result.state == "error"
    assert result.reason
    assert str(source) not in result.reason
    assert "state.json" not in result.reason
    assert not (store.root / entry.id / f"{entry.revision}.partial").exists()
    assert store.current() == previous
    assert _pointer(store) == before
    if fault != "activate":
        smoke.assert_not_called()
        assert not (store.root / entry.id / entry.revision).exists()
