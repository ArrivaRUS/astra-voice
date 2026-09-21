"""Установщик О2: проверка байтов, пробное распознавание и сохранение текущей модели."""

from __future__ import annotations

import errno
import hashlib
import json
import shutil
from dataclasses import replace
from functools import partial
from pathlib import Path
from unittest.mock import Mock

import pytest

from astra_voice.core.model_request import build_model_load
from astra_voice.models import installer as inst
from astra_voice.models.catalog import Catalog, CatalogEntry, FileSpec, RevokedEntry
from astra_voice.models.installer import Installer, SmokeResult, verify_installed
from astra_voice.models.store import ModelRecord, ModelStore, StoreError
from astra_voice.worker import ipc

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
    assert result.reason_code == ""
    assert result.record is not None
    return result.record


def _flip_byte(file: Path) -> None:
    data = bytearray(file.read_bytes())
    data[0] ^= 1
    file.write_bytes(data)


def _pointer(store: ModelStore) -> bytes | None:
    path = store.root / "current.json"
    return path.read_bytes() if path.exists() else None


def test_verify_installed_accepts_complete_model(
    tmp_path: Path, entry: CatalogEntry, contents: dict[str, bytes]
) -> None:
    directory = _write_files(tmp_path / "installed", contents)

    assert verify_installed(directory, entry) == (True, "")


@pytest.mark.parametrize(
    ("damage", "reason"),
    [
        ("extra", "В папке есть лишние файлы. Оставьте только файлы выбранной модели."),
        ("part", "В папке есть лишние файлы. Оставьте только файлы выбранной модели."),
        ("missing", "В папке не хватает файлов модели. Получите их заново."),
        (
            "checksum",
            "Файлы модели повреждены: контрольная сумма не совпала. Получите их заново.",
        ),
        ("size", "Файлы модели повреждены: контрольная сумма не совпала. Получите их заново."),
        ("layout", "Файлы в папке не подходят для выбранной модели."),
        ("variant", "Файлы в папке не подходят для выбранной модели."),
        ("missing-required", "В папке не хватает файлов модели. Получите их заново."),
        ("extra-listed", "Файлы в папке не подходят для выбранной модели."),
    ],
)
def test_verify_installed_rejects_invalid_model(
    tmp_path: Path, entry: CatalogEntry, contents: dict[str, bytes], damage: str, reason: str
) -> None:
    directory = _write_files(tmp_path / "installed", contents)
    first = directory / entry.files[0].path
    if damage in {"extra", "part", "extra-listed"}:
        name = "unexpected.part" if damage == "part" else "unexpected.bin"
        (directory / name).write_bytes(b"extra")
        if damage == "extra-listed":
            extra = FileSpec(name, hashlib.sha256(b"extra").hexdigest(), 5, "/extra")
            entry = replace(entry, files=(*entry.files, extra))
    elif damage == "missing":
        first.unlink()
    elif damage == "checksum":
        _flip_byte(first)
    elif damage == "size":
        # Хеш совпадает: отказ должен дать именно размер из каталога.
        spec = replace(entry.files[0], size=entry.files[0].size + 1)
        entry = replace(entry, files=(spec, *entry.files[1:]))
    elif damage == "layout":
        entry = replace(entry, layout="unknown-layout")
    elif damage == "variant":
        entry = replace(entry, variant="unknown-variant")
    else:
        # Состав и суммы совпадают с каталогом, но раскладка требует config.json.
        (directory / entry.files[-1].path).unlink()
        entry = replace(entry, files=entry.files[:-1])

    assert verify_installed(directory, entry) == (False, reason)


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
    assert result.reason_code == ""
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


def test_installed_record_builds_valid_smoke_ipc_request(
    store: ModelStore, entry: CatalogEntry, contents: dict[str, bytes]
) -> None:
    staging = _stage(store, entry, contents)
    checked_records: list[ModelRecord] = []
    frames: list[bytes] = []

    def check_smoke(directory: Path, model: CatalogEntry) -> SmokeResult:
        (record,) = store.records()
        assert record.dir == directory
        assert (record.id, record.revision) == (model.id, model.revision)
        assert directory.is_dir()
        assert not staging.exists()
        assert store.current() is None
        # Передаём настоящий Path из хранилища без нормализации на стороне теста.
        request = build_model_load(
            {
                "model_id": record.id,
                "model_revision": record.revision,
                "model_dir": record.dir,
                "model_layout": record.layout,
                "model_variant": record.variant,
                "model_min_ram_mb": model.min_ram_mb,
            },
            store_dir=store.root,
        )
        frame = ipc.encode(request)
        assert ipc.decode(frame[4:]) == request
        checked_records.append(record)
        frames.append(frame)
        return SmokeResult(True, text="Контракт IPC проверен")

    result = Installer(store, check_smoke).install_from_staging(entry)

    assert result.state == "ok"
    assert result.reason_code == ""
    assert result.reason == ""
    assert result.record is not None
    assert checked_records == [result.record]
    assert store.current() == result.record
    assert store.records() == (result.record,)
    assert len(frames) == 1
    assert ipc.decode(frames[0][4:]) == {
        "type": "model.load",
        "id": entry.id,
        "revision": entry.revision,
        "dir": str(result.record.dir),
        "layout": entry.layout,
        "variant": entry.variant,
        "threads": 2,
        "min_ram_mb": entry.min_ram_mb,
    }


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
    assert result.reason_code == "checksum"
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
    assert result.reason_code == "selfcheck"
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
    state = json.loads(installed.with_name(f"{entry.revision}.json").read_text())
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
    assert result.reason_code == "layout"
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
    assert result.reason_code == ""
    assert result.record is not None
    assert {file.name for file in result.record.dir.iterdir()} == set(contents)


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
    assert result.reason_code == "layout"
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
    assert result.reason_code == "layout"
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
    assert result.reason_code == ""
    assert result.record == store.current()
    assert result.record is not None
    assert result.record.layout == entry.layout
    assert not (store.root / entry.id / f"{entry.revision}.partial").exists()
    for name, data in contents.items():
        assert (source / name).read_bytes() == data
        assert (result.record.dir / name).read_bytes() == data
    smoke.assert_called_once_with(result.record.dir, entry)


def test_cancel_during_copy_clears_partial_file_and_keeps_current(
    tmp_path: Path,
    store: ModelStore,
    entry: CatalogEntry,
    contents: dict[str, bytes],
    smoke: Mock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    previous = _previous(store, entry, contents)
    before = _pointer(store)
    source = _write_files(tmp_path / "source", contents)
    staging = store.root / entry.id / f"{entry.revision}.partial"
    monkeypatch.setattr(inst, "_COPY_CHUNK", 2)
    # Начальная проверка и два блока первого файла; отмена после второго блока.
    cancel = Mock(side_effect=[False, False, True])
    rmtree = shutil.rmtree

    def clear_partial(directory: Path) -> None:
        assert directory == staging
        first = entry.files[0].path
        assert (directory / first).read_bytes() == contents[first][:4]
        assert not (directory / entry.files[1].path).exists()
        rmtree(directory)

    cleanup = Mock(side_effect=clear_partial)
    monkeypatch.setattr(shutil, "rmtree", cleanup)

    result = Installer(store, smoke).install_from_path(source, entry, cancel=cancel)

    assert result.state == "error"
    assert result.reason_code == "cancelled"
    assert result.reason == "Установка отменена."
    assert result.record is None
    assert cancel.call_count == 3
    cleanup.assert_called_once_with(staging)
    assert not staging.exists()
    assert not (store.root / entry.id / entry.revision).exists()
    assert store.current() == previous
    assert store.records() == (previous,)
    assert _pointer(store) == before
    for name, data in contents.items():
        assert (source / name).read_bytes() == data
    smoke.assert_not_called()


@pytest.mark.parametrize("from_path", [False, True])
def test_cancel_during_sha256_preserves_download_staging_and_keeps_current(
    tmp_path: Path,
    store: ModelStore,
    entry: CatalogEntry,
    contents: dict[str, bytes],
    smoke: Mock,
    monkeypatch: pytest.MonkeyPatch,
    from_path: bool,
) -> None:
    previous = _previous(store, entry, contents)
    before = _pointer(store)
    staging = store.root / entry.id / f"{entry.revision}.partial"
    source = (
        _write_files(tmp_path / "source", contents) if from_path else _stage(store, entry, contents)
    )
    monkeypatch.setattr(inst, "_COPY_CHUNK", 2)
    hashing = Mock(wraps=partial(inst.sha256_file, chunk=2))
    monkeypatch.setattr(inst, "sha256_file", hashing)
    hash_cancel = Mock(side_effect=[False, False, True])

    def cancel() -> bool:
        # При установке из папки пропускаем копирование и отменяем перечитывание.
        return bool(hash_cancel()) if hashing.called else False

    installer = Installer(store, smoke)
    result = (
        installer.install_from_path(source, entry, cancel=cancel)
        if from_path
        else installer.install_from_staging(entry, cancel=cancel)
    )

    assert result.state == "error"
    assert result.reason_code == "cancelled"
    assert result.reason == "Установка отменена."
    assert result.record is None
    assert hash_cancel.call_count == 3
    hashing.assert_called_once_with(staging / entry.files[0].path, cancel=cancel)
    if from_path:
        assert not staging.exists()
    else:
        assert staging.is_dir()
        for name, data in contents.items():
            assert (staging / name).read_bytes() == data
    assert not (store.root / entry.id / entry.revision).exists()
    assert store.current() == previous
    assert store.records() == (previous,)
    assert _pointer(store) == before
    smoke.assert_not_called()


@pytest.mark.parametrize("from_path", [False, True])
@pytest.mark.parametrize("with_callback", [False, True])
def test_install_without_cancellation(
    tmp_path: Path,
    store: ModelStore,
    entry: CatalogEntry,
    contents: dict[str, bytes],
    smoke: Mock,
    monkeypatch: pytest.MonkeyPatch,
    from_path: bool,
    with_callback: bool,
) -> None:
    source = (
        _write_files(tmp_path / "source", contents) if from_path else _stage(store, entry, contents)
    )
    monkeypatch.setattr(inst, "_COPY_CHUNK", 2)
    cancel = Mock(return_value=False) if with_callback else None
    installer = Installer(store, smoke)

    result = (
        installer.install_from_path(source, entry, cancel=cancel)
        if from_path
        else installer.install_from_staging(entry, cancel=cancel)
    )

    assert result.state == "ok"
    assert result.reason_code == ""
    assert result.reason == ""
    assert result.record is not None
    assert store.current() == result.record
    assert store.records() == (result.record,)
    assert not (store.root / entry.id / f"{entry.revision}.partial").exists()
    for name, data in contents.items():
        assert (result.record.dir / name).read_bytes() == data
    if cancel is not None:
        assert cancel.call_count > len(entry.files)
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
    assert result.reason_code == "layout"
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
    assert result.reason_code == ("" if source_kind == "unknown-model" else "layout")
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
    assert result.reason_code == "checksum"
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
    assert result.reason_code == "disk"
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
    assert result.reason_code == ""
    assert result.record is not None
    assert {file.name for file in result.record.dir.iterdir()} == set(contents)
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
    assert result.reason_code == ""
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
    assert result.reason_code == "layout"
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
    assert result.reason_code == ("disk" if fault == "copy" else "")
    assert result.reason
    assert str(source) not in result.reason
    assert "state.json" not in result.reason
    assert not (store.root / entry.id / f"{entry.revision}.partial").exists()
    assert store.current() == previous
    assert _pointer(store) == before
    if fault != "activate":
        smoke.assert_not_called()
        assert not (store.root / entry.id / entry.revision).exists()


@pytest.mark.parametrize("from_path", [False, True])
def test_revoked_revision_is_rejected_before_copy_commit_and_smoke(
    tmp_path: Path,
    store: ModelStore,
    entry: CatalogEntry,
    contents: dict[str, bytes],
    smoke: Mock,
    monkeypatch: pytest.MonkeyPatch,
    from_path: bool,
) -> None:
    previous = _previous(store, entry, contents)
    before = _pointer(store)
    staging = _stage(store, entry, contents)
    source = _write_files(tmp_path / "source", contents) if from_path else staging
    catalog = Catalog(1, 1, (RevokedEntry(entry.id, entry.revision, "Ошибка модели"),), (entry,))
    copy = Mock(wraps=inst._copy_file)
    commit = Mock(wraps=store.commit)
    create_staging = Mock(wraps=store.staging_dir)
    monkeypatch.setattr(inst, "_copy_file", copy)
    monkeypatch.setattr(store, "commit", commit)
    monkeypatch.setattr(store, "staging_dir", create_staging)
    installer = Installer(store, smoke, catalog=catalog)

    result = (
        installer.install_from_path(source, entry)
        if from_path
        else installer.install_from_staging(entry)
    )

    assert result.state == "error"
    assert result.reason_code == "revoked"
    assert result.reason == "Эта версия модели отозвана. Выберите другую версию или модель."
    assert result.record is None
    assert {file.name: file.read_bytes() for file in staging.iterdir()} == contents
    assert {file.name: file.read_bytes() for file in source.iterdir()} == contents
    assert not (store.root / entry.id / entry.revision).exists()
    assert store.current() == previous
    assert store.records() == (previous,)
    assert _pointer(store) == before
    copy.assert_not_called()
    commit.assert_not_called()
    create_staging.assert_not_called()
    smoke.assert_not_called()


@pytest.mark.parametrize("from_path", [False, True])
@pytest.mark.parametrize("revocation", ["none", "other-model", "other-revision"])
def test_catalog_allows_revision_without_matching_revocation(
    tmp_path: Path,
    store: ModelStore,
    entry: CatalogEntry,
    contents: dict[str, bytes],
    smoke: Mock,
    from_path: bool,
    revocation: str,
) -> None:
    source = (
        _write_files(tmp_path / "source", contents) if from_path else _stage(store, entry, contents)
    )
    revoked = (
        ()
        if revocation == "none"
        else (
            RevokedEntry(
                "other-model" if revocation == "other-model" else entry.id,
                "other-revision" if revocation == "other-revision" else entry.revision,
                "Ошибка модели",
            ),
        )
    )
    installer = Installer(store, smoke, Catalog(1, 1, revoked, (entry,)))

    result = (
        installer.install_from_path(source, entry)
        if from_path
        else installer.install_from_staging(entry)
    )

    assert result.state == "ok"
    assert result.reason_code == ""
    assert result.record == store.current()
    smoke.assert_called_once_with(store.root / entry.id / entry.revision, entry)


@pytest.mark.parametrize("size_delta", [-1, 1])
def test_staged_size_mismatch_with_matching_hash_is_checksum(
    store: ModelStore,
    entry: CatalogEntry,
    contents: dict[str, bytes],
    smoke: Mock,
    size_delta: int,
) -> None:
    spec = entry.files[0]
    entry = replace(entry, files=(replace(spec, size=spec.size + size_delta), *entry.files[1:]))
    staging = _stage(store, entry, contents)

    result = Installer(store, smoke).install_from_staging(entry)

    assert result.state == "error"
    assert result.reason_code == "checksum"
    assert staging.is_dir()
    assert store.records() == ()
    smoke.assert_not_called()


@pytest.mark.parametrize(
    ("error", "reason_code"),
    [
        (OSError(errno.ENOSPC, "Нет места"), "disk"),
        (OSError(errno.EDQUOT, "Исчерпана квота"), "disk"),
        (StoreError("disk-full"), "disk"),
        (StoreError("bad-id"), "layout"),
        (OSError(errno.EACCES, "Отказ доступа"), ""),
        (StoreError("broken-store"), ""),
    ],
)
def test_commit_error_reason_codes(
    store: ModelStore,
    entry: CatalogEntry,
    contents: dict[str, bytes],
    smoke: Mock,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    reason_code: str,
) -> None:
    staging = _stage(store, entry, contents)
    monkeypatch.setattr(store, "commit", Mock(side_effect=error))

    result = Installer(store, smoke).install_from_staging(entry)

    assert result.state == "error"
    assert result.reason_code == reason_code
    assert result.reason
    assert staging.is_dir()
    assert not (staging / "state.json").exists()
    assert store.records() == ()
    smoke.assert_not_called()
