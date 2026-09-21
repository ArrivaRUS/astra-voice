"""Хранилище моделей: раскладка, атомарность, права, восстановление и защита путей."""

from __future__ import annotations

import errno
import json
import logging
import os
import shutil
import stat
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from astra_voice.core import paths
from astra_voice.models import store as st
from astra_voice.models.store import ModelRecord, ModelStore, StoreError

pytestmark = pytest.mark.unit


@pytest.fixture
def store(tmp_path: Path) -> ModelStore:
    return ModelStore(tmp_path / "models")


def install(store: ModelStore, model_id: str = "model", revision: str = "rev") -> Path:
    staging = store.staging_dir(model_id, revision)
    (staging / "weights.onnx").write_bytes(b"weights")
    return store.commit(model_id, revision)


def test_default_root_is_computed_in_constructor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = Mock(return_value=tmp_path / "data")
    monkeypatch.setattr(paths, "data_dir", data_dir)
    ModelStore(tmp_path / "explicit")
    data_dir.assert_not_called()
    store = ModelStore()
    data_dir.assert_called_once_with()
    assert store.staging_dir("model", "rev") == tmp_path / "data/models/model/rev.partial"


def test_staging_layout_and_permissions(store: ModelStore, tmp_path: Path) -> None:
    directory = store.staging_dir("model", "rev")
    assert directory == tmp_path / "models/model/rev.partial"
    assert directory.is_dir()
    for path in (directory, directory.parent, directory.parent.parent):
        assert stat.S_IMODE(path.stat().st_mode) == 0o700
    assert store.staging_dir("model", "rev") == directory


def test_missing_parents_are_private(tmp_path: Path) -> None:
    directory = ModelStore(tmp_path / "a/b/models").staging_dir("model", "rev")
    while directory != tmp_path:
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
        directory = directory.parent


def test_existing_directory_permissions_are_repaired(store: ModelStore) -> None:
    directory = store.root / "model/rev.partial"
    directory.mkdir(parents=True)
    for path in (store.root, directory.parent, directory):
        path.chmod(0o755)
    assert store.staging_dir("model", "rev") == directory
    for path in (store.root, directory.parent, directory):
        assert stat.S_IMODE(path.stat().st_mode) == 0o700


def test_commit_moves_once_and_writes_state(
    store: ModelStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    staging = store.staging_dir("model", "rev")
    (staging / "weights.onnx").write_bytes(b"weights")
    original_inode = staging.stat().st_ino
    replace = Mock(wraps=os.replace)
    monkeypatch.setattr(os, "replace", replace)
    directory = store.commit("model", "rev")
    assert directory == store.root / "model/rev"
    assert directory.stat().st_ino == original_inode
    assert (staging, directory) in [call.args for call in replace.call_args_list]
    assert sum(call.args == (staging, directory) for call in replace.call_args_list) == 1
    assert not staging.exists()
    assert (directory / "weights.onnx").read_bytes() == b"weights"
    assert not (directory / "state.json").exists()
    state_path = directory.with_name("rev.json")
    assert json.loads(state_path.read_text()) == {
        "state": "ok",
        "reason": "",
        "recheck": False,
        "layout": "",
        "variant": "",
        "size_bytes": 7,
    }
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(state_path.stat().st_mode) == 0o600


def test_commit_preserves_staged_metadata(store: ModelStore) -> None:
    staging = store.staging_dir("model", "rev")
    (staging / "state.json").write_text(
        json.dumps(
            {
                "layout": "onnx-asr-gigaam-v3",
                "variant": "gigaam-v3-e2e-rnnt",
                "size_bytes": 123,
                "state": "broken",
                "reason": "Предыдущая попытка",
            }
        )
    )
    directory = store.commit("model", "rev")
    assert not (directory / "state.json").exists()
    assert stat.S_IMODE(directory.with_name("rev.json").stat().st_mode) == 0o600
    assert store.records() == (
        ModelRecord("model", "rev", directory, "onnx-asr-gigaam-v3", "gigaam-v3-e2e-rnnt", 123),
    )


def test_commit_counts_nested_payload(store: ModelStore) -> None:
    staging = store.staging_dir("model", "rev")
    (staging / "nested").mkdir()
    (staging / "nested/weights").write_bytes(b"12345")
    (staging / "config").write_bytes(b"12")
    store.commit("model", "rev")
    assert store.records()[0].size_bytes == 7


@pytest.mark.parametrize("operation", ["commit", "set_current", "mark_broken", "mark_ok", "remove"])
def test_missing_revision(store: ModelStore, operation: str) -> None:
    with pytest.raises(StoreError) as error:
        if operation == "mark_broken":
            store.mark_broken("model", "rev", "Ошибка")
        else:
            getattr(store, operation)("model", "rev")
    assert error.value.code == "not-found"
    assert error.value.message
    assert not store.root.exists()


def test_commit_replaces_existing_revision(
    store: ModelStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    installed = install(store)
    store.set_current("model", "rev")
    staging = store.staging_dir("model", "rev")
    (staging / "new.onnx").write_bytes(b"new")
    replace = Mock(wraps=os.replace)
    monkeypatch.setattr(os, "replace", replace)
    assert store.commit("model", "rev") == installed
    first, second = replace.call_args_list[:2]
    assert first.args[0] == installed
    assert first.args[1].name.startswith("rev.old-")
    assert second.args == (staging, installed)
    assert set(installed.iterdir()) == {installed / "new.onnx"}
    assert set(installed.parent.iterdir()) == {installed, installed.with_name("rev.json")}
    assert store.current() == store.records()[0]


def test_failed_rename_restores_old_revision(
    store: ModelStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    installed = install(store)
    store.set_current("model", "rev")
    previous = store.current()
    state_before = installed.with_name("rev.json").read_bytes()
    staging = store.staging_dir("model", "rev")
    (staging / "new").write_bytes(b"new")
    original_replace = os.replace

    def fail_install(source: Path, target: Path) -> None:
        if source == staging:
            raise OSError(errno.ENOSPC, "Недостаточно места")
        original_replace(source, target)

    monkeypatch.setattr(os, "replace", fail_install)
    with pytest.raises(StoreError) as error:
        store.commit("model", "rev")
    assert error.value.code == "disk-full"
    assert (installed / "weights.onnx").read_bytes() == b"weights"
    assert (staging / "new").read_bytes() == b"new"
    assert not list(installed.parent.glob("*.old-*"))
    assert installed.with_name("rev.json").read_bytes() == state_before
    assert store.current() == previous


def test_current_roundtrip_and_permissions(store: ModelStore) -> None:
    assert store.current() is None
    directory = install(store)
    assert store.current() is None
    store.set_current("model", "rev")
    assert store.current() == ModelRecord("model", "rev", directory, "", "", 7)
    pointer = store.root / "current.json"
    assert json.loads(pointer.read_text()) == {"id": "model", "revision": "rev"}
    assert stat.S_IMODE(pointer.stat().st_mode) == 0o600


def test_current_rejects_ok_revision_pending_recheck(store: ModelStore) -> None:
    directory = install(store)
    store.set_current("model", "rev")
    state_path = directory.with_name("rev.json")
    data = json.loads(state_path.read_text())
    data["recheck"] = True
    state_path.write_text(json.dumps(data))
    before = state_path.read_bytes()
    pointer = (store.root / "current.json").read_bytes()

    assert store.records()[0].state == "ok"
    assert store.current() is None
    assert state_path.read_bytes() == before
    assert (store.root / "current.json").read_bytes() == pointer


def test_unread_metadata_blocks_current_and_mark_ok(
    store: ModelStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = install(store)
    store.set_current("model", "rev")
    record = ModelRecord("model", "rev", directory, "layout", "variant", 7, metadata_ok=False)
    monkeypatch.setattr(store, "_record", Mock(return_value=record))
    write = Mock(wraps=st._write_json)
    monkeypatch.setattr(st, "_write_json", write)

    assert store.current() is None
    with pytest.raises(StoreError) as error:
        store.mark_ok("model", "rev")
    assert error.value.code == "invalid-metadata"
    write.assert_not_called()


def test_atomic_json_flushes_file_before_replace_and_directory_after(
    store: ModelStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = install(store)
    original_replace, original_fsync = os.replace, os.fsync
    events: list[str] = []

    def synced(fd: int) -> None:
        events.append("каталог" if stat.S_ISDIR(os.fstat(fd).st_mode) else "файл")
        original_fsync(fd)

    def replaced(source: Path, target: Path) -> None:
        assert source.parent == target.parent
        assert stat.S_IMODE(source.stat().st_mode) == 0o600
        json.loads(source.read_text())  # flush уже сделал данные доступными другому читателю.
        events.append(target.name)
        original_replace(source, target)

    monkeypatch.setattr(os, "fsync", synced)
    monkeypatch.setattr(os, "replace", replaced)
    store.set_current("model", "rev")
    store.mark_broken("model", "rev", "Не загружается")
    assert events == ["файл", "current.json", "каталог", "файл", "rev.json", "каталог"]
    assert not list(store.root.rglob("*.tmp"))
    assert not (directory / "state.json").exists()
    assert stat.S_IMODE(directory.with_name("rev.json").stat().st_mode) == 0o600


def test_failed_json_write_keeps_current_and_removes_temp(
    store: ModelStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(store)
    install(store, revision="new")
    store.set_current("model", "rev")
    before = (store.root / "current.json").read_bytes()
    monkeypatch.setattr(os, "fsync", Mock(side_effect=OSError(errno.ENOSPC, "Нет места")))
    with pytest.raises(StoreError) as error:
        store.set_current("model", "new")
    assert error.value.code == "disk-full"
    assert (store.root / "current.json").read_bytes() == before
    assert not list(store.root.rglob("*.tmp"))


@pytest.mark.parametrize(
    "contents",
    [b"{", b"[]", b"null", b"{}", b"\xff", b'{"id":"../evil","revision":"rev"}'],
)
def test_broken_current_is_quarantined(
    store: ModelStore, contents: bytes, caplog: pytest.LogCaptureFixture
) -> None:
    store.root.mkdir()
    pointer = store.root / "current.json"
    pointer.write_bytes(contents)
    assert store.current() is None
    assert not pointer.exists()
    backups = list(store.root.glob("current.json.bak-*"))
    assert len(backups) == 1
    assert backups[0].name.split(".bak-")[1].isdigit()
    assert backups[0].read_bytes() == contents
    assert "испорчен" in caplog.text


def test_quarantine_preserves_previous_backup(
    store: ModelStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.root.mkdir()
    monkeypatch.setattr(time, "time", lambda: 100)
    pointer = store.root / "current.json"
    pointer.write_bytes(b"first")
    assert store.current() is None
    pointer.write_bytes(b"second")
    assert store.current() is None
    assert (store.root / "current.json.bak-100").read_bytes() == b"first"
    assert (store.root / "current.json.bak-101").read_bytes() == b"second"


def test_current_missing_revision_preserves_pointer(store: ModelStore) -> None:
    directory = install(store)
    store.set_current("model", "rev")
    shutil.rmtree(directory)
    assert store.current() is None
    assert (store.root / "current.json").exists()
    assert not list(store.root.glob("*.bak-*"))


def test_mark_broken_preserves_pointer_and_metadata(store: ModelStore) -> None:
    directory = install(store)
    store.set_current("model", "rev")
    pointer = store.root / "current.json"
    before = pointer.read_bytes()
    store.mark_broken("model", "rev", "Ошибка проверки модели")
    assert store.records() == (
        ModelRecord("model", "rev", directory, "", "", 7, "broken", "Ошибка проверки модели"),
    )
    assert store.current() is None
    assert pointer.read_bytes() == before


def test_empty_records_and_ignored_staging(store: ModelStore) -> None:
    assert store.records() == ()
    store.staging_dir("model", "rev")
    (store.root / "model/rev.old-123").mkdir()
    assert store.records() == ()


@pytest.mark.parametrize("state", ["ok", "broken"])
@pytest.mark.parametrize("neighbor_exists", [False, True])
@pytest.mark.parametrize("reason", ["", "Произвольная причина отказа"])
def test_legacy_migration_marks_only_migrated_broken_state_for_recheck(
    store: ModelStore, state: str, neighbor_exists: bool, reason: str
) -> None:
    directory = install(store)
    store.set_current("model", "rev")
    state_path = directory.with_name("rev.json")
    data = json.loads(state_path.read_text())
    data.update(state=state, reason=reason)
    del data["recheck"]
    contents = json.dumps(data).encode()
    legacy = directory / "state.json"
    legacy.write_bytes(contents)
    legacy.chmod(0o644)
    if neighbor_exists:
        state_path.write_bytes(contents)
    else:
        state_path.unlink()

    (record,) = store.records()
    expected_recheck = state == "broken" and not neighbor_exists
    assert record.state == state
    assert record.reason == reason
    assert record.recheck is expected_recheck
    assert (record.layout, record.variant, record.size_bytes) == ("", "", 7)
    assert not legacy.exists()
    assert stat.S_IMODE(state_path.stat().st_mode) == 0o600
    if expected_recheck:
        assert json.loads(state_path.read_text()) == {**data, "recheck": True}
    else:
        assert state_path.read_bytes() == contents
    assert store.records() == (record,)
    assert store.current() == (record if state == "ok" else None)


def test_external_broken_state_without_recheck_is_not_marked(store: ModelStore) -> None:
    directory = install(store)
    state_path = directory.with_name("rev.json")
    data = json.loads(state_path.read_text())
    data.update(state="broken", reason="Ошибка проверки модели")
    del data["recheck"]
    state_path.write_text(json.dumps(data))
    before = state_path.read_bytes()
    (record,) = store.records()
    assert record.state == "broken"
    assert record.reason == data["reason"]
    assert record.recheck is False
    assert not (directory / "state.json").exists()
    assert state_path.read_bytes() == before


@pytest.mark.parametrize("operation", ["mark_ok", "mark_broken"])
def test_recheck_verdict_clears_flag_and_preserves_metadata(
    store: ModelStore, operation: str
) -> None:
    directory = install(store)
    state_path = directory.with_name("rev.json")
    data = json.loads(state_path.read_text())
    data.update(
        state="broken",
        reason="Старая причина",
        recheck=True,
        layout="onnx-asr-gigaam-v3",
        variant="gigaam-v3-e2e-rnnt",
    )
    state_path.write_text(json.dumps(data))
    store.set_current("model", "rev")
    pointer = store.root / "current.json"
    before = pointer.read_bytes()
    assert store.records()[0].recheck is True
    assert store.current() is None

    if operation == "mark_ok":
        store.mark_ok("model", "rev")
        state, reason = "ok", ""
    else:
        store.mark_broken("model", "rev", "Новая причина")
        state, reason = "broken", "Новая причина"
    (record,) = store.records()
    assert record.state == state
    assert record.reason == reason
    assert record.recheck is False
    assert record.metadata_ok is True
    assert json.loads(state_path.read_text()) == {
        **data,
        "state": state,
        "reason": reason,
        "recheck": False,
    }
    assert pointer.read_bytes() == before
    assert stat.S_IMODE(state_path.stat().st_mode) == 0o600
    store.set_current("model", "rev")
    assert store.current() == (record if state == "ok" else None)


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize(
    "failure", ["missing", "corrupt", "unreadable", "empty-layout", "empty-variant"]
)
def test_mark_ok_rejects_invalid_metadata_without_writing(
    store: ModelStore, monkeypatch: pytest.MonkeyPatch, legacy: bool, failure: str
) -> None:
    directory = install(store)
    store.set_current("model", "rev")
    state_path = directory.with_name("rev.json")
    data = json.loads(state_path.read_text())
    data.update(state="broken", recheck=True, layout="layout", variant="variant")
    if legacy:
        state_path.unlink()
        state_path = directory / "state.json"
    state_path.write_text(json.dumps(data))
    if failure == "missing":
        state_path.unlink()
    elif failure == "corrupt":
        state_path.write_bytes(b'{"metadata_ok": true}')
    elif failure == "unreadable":
        read_json = st._read_json

        def read(path: Path) -> dict[str, object]:
            if path == state_path:
                raise PermissionError("PRIVATE /private/model")
            return read_json(path)

        monkeypatch.setattr(st, "_read_json", read)
    else:
        data[failure.removeprefix("empty-")] = ""
        state_path.write_text(json.dumps(data))
    before = {path: path.read_bytes() for path in store.root.rglob("*") if path.is_file()}
    write = Mock(wraps=st._write_json)
    monkeypatch.setattr(st, "_write_json", write)

    with pytest.raises(StoreError) as error:
        store.mark_ok("model", "rev")

    assert error.value.code == "invalid-metadata"
    assert error.value.message == (
        "Метаданные модели недоступны или неполны. Модель нужно переустановить."
    )
    write.assert_not_called()
    assert {path: path.read_bytes() for path in store.root.rglob("*") if path.is_file()} == before


@pytest.mark.parametrize("contents", [None, b"{"])
def test_mark_broken_still_works_without_metadata(
    store: ModelStore, contents: bytes | None
) -> None:
    directory = install(store)
    state_path = directory.with_name("rev.json")
    if contents is None:
        state_path.unlink()
    else:
        state_path.write_bytes(contents)
    (record,) = store.records()
    assert record.metadata_ok is False

    store.mark_broken("model", "rev", "Ошибка проверки")

    assert json.loads(state_path.read_text()) == {
        "state": "broken",
        "reason": "Ошибка проверки",
        "recheck": False,
        "layout": "",
        "variant": "",
        "size_bytes": 0,
    }
    with pytest.raises(StoreError) as error:
        store.mark_ok("model", "rev")
    assert error.value.code == "invalid-metadata"


@pytest.mark.parametrize("metadata_ok", [False, True])
def test_metadata_ok_is_neither_read_nor_written(store: ModelStore, metadata_ok: bool) -> None:
    directory = install(store)
    state_path = directory.with_name("rev.json")
    data = json.loads(state_path.read_text())
    data["metadata_ok"] = metadata_ok
    state_path.write_text(json.dumps(data))

    (record,) = store.records()

    assert record.metadata_ok is True
    store.mark_broken("model", "rev", "Ошибка проверки")
    assert "metadata_ok" not in json.loads(state_path.read_text())


@pytest.mark.parametrize("failure_stage", ["replace", "fsync_dir", "unlink"])
def test_failed_broken_migration_preserves_legacy_until_recheck_is_written(
    store: ModelStore, monkeypatch: pytest.MonkeyPatch, failure_stage: str
) -> None:
    directory = install(store)
    state_path = directory.with_name("rev.json")
    data = json.loads(state_path.read_text())
    data.update(state="broken", reason="Старая причина")
    del data["recheck"]
    legacy = directory / "state.json"
    contents = json.dumps(data).encode()
    legacy.write_bytes(contents)
    state_path.unlink()
    original_unlink = Path.unlink

    def fail_legacy_unlink(path: Path, missing_ok: bool = False) -> None:
        if path == legacy:
            assert json.loads(state_path.read_text())["recheck"] is True
            raise PermissionError("Нет доступа")
        original_unlink(path, missing_ok=missing_ok)

    with monkeypatch.context() as patch:
        if failure_stage == "replace":
            patch.setattr(os, "replace", Mock(side_effect=OSError(errno.ENOSPC, "Нет места")))
        elif failure_stage == "fsync_dir":
            patch.setattr(st, "_fsync_dir", Mock(side_effect=OSError(errno.EIO, "Ошибка записи")))
        else:
            patch.setattr(Path, "unlink", fail_legacy_unlink)
        (record,) = store.records()
        assert record.state == "broken"
        assert record.reason == "Файл состояния модели (state.json) повреждён или недоступен."
        assert legacy.read_bytes() == contents
        if failure_stage == "replace":
            assert not state_path.exists()
        else:
            assert json.loads(state_path.read_text())["recheck"] is True
        assert not list(directory.parent.glob("*.tmp"))

    (record,) = store.records()
    assert record.state == "broken"
    assert record.recheck is True
    assert record.reason == data["reason"]
    assert not legacy.exists()


@pytest.mark.parametrize("operation", ["records", "current"])
@pytest.mark.parametrize("neighbor_exists", [False, True])
def test_legacy_state_is_migrated_once(
    store: ModelStore,
    caplog: pytest.LogCaptureFixture,
    operation: str,
    neighbor_exists: bool,
) -> None:
    directory = install(store)
    store.set_current("model", "rev")
    expected = store.current()
    state_path = directory.with_name("rev.json")
    contents = state_path.read_bytes()
    legacy = directory / "state.json"
    legacy.write_bytes(b"outdated" if neighbor_exists else contents)
    if not neighbor_exists:
        state_path.unlink()
    with caplog.at_level(logging.INFO, logger=st.__name__):
        result = store.records() if operation == "records" else store.current()
        assert result == ((expected,) if operation == "records" else expected)
        assert store.records() == (expected,)
        assert store.current() == expected
    assert not legacy.exists()
    assert state_path.read_bytes() == contents
    assert stat.S_IMODE(state_path.stat().st_mode) == 0o600
    assert len(caplog.records) == 1
    assert caplog.records[0].levelno == logging.INFO
    assert "перенесено" in caplog.records[0].message


@pytest.mark.parametrize("operation", ["records", "current"])
@pytest.mark.parametrize("neighbor_exists", [False, True])
def test_failed_legacy_state_migration_is_broken(
    store: ModelStore,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    operation: str,
    neighbor_exists: bool,
) -> None:
    directory = install(store)
    store.set_current("model", "rev")
    state_path = directory.with_name("rev.json")
    legacy = directory / "state.json"
    legacy.write_bytes(state_path.read_bytes())
    if not neighbor_exists:
        state_path.unlink()
    failure = Mock(side_effect=PermissionError("Нет доступа"))
    if neighbor_exists:
        monkeypatch.setattr(Path, "unlink", failure)
    else:
        monkeypatch.setattr(os, "replace", failure)
    if operation == "records":
        (record,) = store.records()
        assert record.state == "broken"
        assert record.reason == "Файл состояния модели (state.json) повреждён или недоступен."
    else:
        assert store.current() is None
    assert legacy.is_file()
    failure.assert_called_once()
    assert len(caplog.records) == 1
    assert caplog.records[0].levelno == logging.WARNING
    assert "Не удалось перенести" in caplog.records[0].message


def test_legacy_state_symlink_is_not_migrated(store: ModelStore) -> None:
    directory = install(store)
    store.set_current("model", "rev")
    state_path = directory.with_name("rev.json")
    target = directory.parent / "legacy.json"
    state_path.rename(target)
    contents = target.read_bytes()
    legacy = directory / "state.json"
    legacy.symlink_to(target)
    (record,) = store.records()
    assert record.state == "broken"
    assert store.current() is None
    assert legacy.is_symlink()
    assert not state_path.exists()
    assert target.read_bytes() == contents


@pytest.mark.parametrize("revision", ["rev", "r" * 64])
def test_records_ignore_neighbor_state_files(store: ModelStore, revision: str) -> None:
    directory = install(store, revision=revision)
    (directory.parent / "orphan.json").write_text("{}")
    (directory.parent / "linked.json").symlink_to(store.root.parent)
    assert store.records() == (ModelRecord("model", revision, directory, "", "", 7),)
    assert store.recover_incomplete() == ()
    assert directory.with_name(f"{revision}.json").is_file()


@pytest.mark.parametrize("name", ("bad revision", ".hidden", "ревизия", "r" * 65, "bad\n"))
def test_records_ignore_invalid_revision_directory(store: ModelStore, name: str) -> None:
    installed = install(store)
    expected = store.records()
    (installed.parent / name).mkdir()
    assert store.records() == expected
    assert len(expected) == 1


def test_recover_accepts_service_suffixes_on_long_revision(store: ModelStore) -> None:
    revision = "r" * 64
    staging = store.staging_dir("model", revision)
    old = staging.with_name(revision + ".old-123")
    old.mkdir()
    assert store.records() == ()
    assert store.recover_incomplete() == (f"model/{revision}",)
    assert not staging.exists()
    assert not old.exists()


@pytest.mark.parametrize(
    "contents",
    [
        None,
        b"{",
        b"[]",
        b"\xff",
        b"{}",
        b'{"state":"unknown"}',
        b'{"state":"ok","reason":"","layout":"","variant":"","size_bytes":true}',
    ],
)
def test_missing_or_broken_state_is_listed_as_broken(
    store: ModelStore, contents: bytes | None
) -> None:
    directory = install(store)
    store.set_current("model", "rev")
    state = directory.with_name("rev.json")
    if contents is None:
        state.unlink()
    else:
        state.write_bytes(contents)
    (record,) = store.records()
    assert record.id == "model"
    assert record.revision == "rev"
    assert record.state == "broken"
    assert "состояния модели" in record.reason
    assert record.metadata_ok is False
    assert store.current() is None


def test_state_without_reason_is_ok(store: ModelStore) -> None:
    directory = install(store)
    state = directory.with_name("rev.json")
    data = json.loads(state.read_text())
    del data["reason"]
    state.write_text(json.dumps(data))
    (record,) = store.records()
    assert record.state == "ok"
    assert record.reason == ""


@pytest.mark.parametrize("reason", [123, None])
def test_state_with_non_string_reason_is_broken(store: ModelStore, reason: object) -> None:
    directory = install(store)
    state = directory.with_name("rev.json")
    data = json.loads(state.read_text())
    data["reason"] = reason
    state.write_text(json.dumps(data))
    (record,) = store.records()
    assert record.state == "broken"
    assert record.reason == "Файл состояния модели (state.json) повреждён или недоступен."


@pytest.mark.parametrize("recheck", ["yes", 0, 1, None, [], {}])
def test_state_with_non_bool_recheck_is_broken(store: ModelStore, recheck: object) -> None:
    directory = install(store)
    state_path = directory.with_name("rev.json")
    data = json.loads(state_path.read_text())
    data["recheck"] = recheck
    state_path.write_text(json.dumps(data))
    (record,) = store.records()
    assert record.state == "broken"
    assert record.recheck is False
    assert record.reason == "Файл состояния модели (state.json) повреждён или недоступен."


def test_records_enumerate_models_and_revisions(store: ModelStore) -> None:
    for model_id, revision in (("b", "2"), ("a", "2"), ("a", "1")):
        install(store, model_id, revision)
    assert [(record.id, record.revision) for record in store.records()] == [
        ("a", "1"),
        ("a", "2"),
        ("b", "2"),
    ]


@pytest.mark.parametrize("value", ["../evil", "a/b", "", "a" * 65, ".", "..", "a\x00b"])
@pytest.mark.parametrize("field", ["model_id", "revision"])
@pytest.mark.parametrize(
    "operation", ["staging_dir", "commit", "set_current", "mark_broken", "mark_ok", "remove"]
)
def test_bad_identifiers_have_no_side_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str, field: str, operation: str
) -> None:
    store = ModelStore(tmp_path)
    before = set(tmp_path.parent.iterdir())
    forbidden = Mock(side_effect=AssertionError("Запись до проверки идентификаторов"))
    monkeypatch.setattr(os, "mkdir", forbidden)
    monkeypatch.setattr(os, "open", forbidden)
    model_id, revision = (value, "rev") if field == "model_id" else ("model", value)
    with pytest.raises(StoreError) as error:
        if operation == "mark_broken":
            store.mark_broken(model_id, revision, "Ошибка")
        else:
            getattr(store, operation)(model_id, revision)
    assert error.value.code == "bad-id"
    forbidden.assert_not_called()
    assert not list(tmp_path.iterdir())
    assert set(tmp_path.parent.iterdir()) == before


@pytest.mark.parametrize("linked_component", ["model", "revision", "staging"])
def test_symlink_escape_is_rejected(tmp_path: Path, linked_component: str) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    store = ModelStore(tmp_path / "models")
    (store.root / "model").mkdir(parents=True)
    if linked_component == "model":
        (store.root / "model").rmdir()
        (store.root / "model").symlink_to(outside, target_is_directory=True)
    else:
        name = "rev.partial" if linked_component == "staging" else "rev"
        (store.root / "model" / name).symlink_to(outside, target_is_directory=True)
    with pytest.raises(StoreError) as error:
        if linked_component == "revision":
            store.set_current("model", "rev")
        else:
            store.staging_dir("model", "rev")
    assert error.value.code == "bad-id"
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize(
    "size,free,expected",
    [
        (100, 120, True),
        (100, 119, False),
        (1, 1, False),
        (0, 0, True),
        (10**20, 12 * 10**19 - 1, False),
    ],
)
def test_disk_boundary_and_missing_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, size: int, free: int, expected: bool
) -> None:
    store = ModelStore(tmp_path / "missing/models")
    usage = Mock(return_value=SimpleNamespace(free=free))
    monkeypatch.setattr(shutil, "disk_usage", usage)
    assert store.disk_ok(size) is expected
    usage.assert_called_once_with(tmp_path)


def test_disk_usage_uses_existing_root(store: ModelStore, monkeypatch: pytest.MonkeyPatch) -> None:
    store.root.mkdir()
    usage = Mock(return_value=SimpleNamespace(free=120))
    monkeypatch.setattr(shutil, "disk_usage", usage)
    assert store.disk_ok(100)
    usage.assert_called_once_with(store.root)


@pytest.mark.parametrize(
    "total,minimum,expected", [(2048, 1, True), (2048, 2, True), (2048, 3, False), (2047, 2, False)]
)
def test_ram_memtotal(
    tmp_path: Path,
    store: ModelStore,
    monkeypatch: pytest.MonkeyPatch,
    total: int,
    minimum: int,
    expected: bool,
) -> None:
    meminfo = tmp_path / "meminfo"
    meminfo.write_text(f"MemFree: 1 kB\nMemTotal:       {total} kB\nMemAvailable: 1 kB\n")
    monkeypatch.setattr(st, "MEMINFO_PATH", meminfo)
    assert store.ram_ok(minimum) is expected


@pytest.mark.parametrize(
    "contents",
    [None, "мусор", "MemTotal: нет kB", "MemTotal:", "MemTotal: -1 kB", "MemTotal: 100 MB"],
)
def test_unknown_ram_does_not_block(
    tmp_path: Path,
    store: ModelStore,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    contents: str | None,
) -> None:
    meminfo = tmp_path / "meminfo"
    if contents is not None:
        meminfo.write_text(contents)
    monkeypatch.setattr(st, "MEMINFO_PATH", meminfo)
    assert store.ram_ok(100000) is True
    assert caplog.records


def test_recover_cleans_partial_and_old_only(store: ModelStore) -> None:
    assert store.recover_incomplete() == ()
    installed = install(store)
    store.set_current("model", "rev")
    original = (installed / "weights.onnx").read_bytes()
    for model_id, revision in (("model", "next"), ("other", "rev")):
        staging = store.staging_dir(model_id, revision)
        (staging / "weights.part").write_bytes(b"partial")
    old = installed.with_name("rev.old-123")
    old.mkdir()
    (old / "old").write_bytes(b"old")
    assert store.recover_incomplete() == ("model/next", "other/rev")
    assert not list(store.root.rglob("*.partial"))
    assert not old.exists()
    assert (installed / "weights.onnx").read_bytes() == original
    assert store.current() == store.records()[0]
    assert store.recover_incomplete() == ()


def test_recover_continues_after_deletion_failure(
    store: ModelStore, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    failed = store.staging_dir("a", "rev")
    removed = store.staging_dir("b", "rev")
    rmtree = shutil.rmtree

    def fail_one(path: Path) -> None:
        if path == failed:
            raise PermissionError("Нет доступа")
        rmtree(path)

    monkeypatch.setattr(shutil, "rmtree", fail_one)
    assert store.recover_incomplete() == ("b/rev",)
    assert failed.exists()
    assert not removed.exists()
    assert "Не удалось удалить" in caplog.text


@pytest.mark.parametrize("broken", [False, True])
def test_remove_current(store: ModelStore, broken: bool) -> None:
    directory = install(store)
    store.set_current("model", "rev")
    if broken:
        store.mark_broken("model", "rev", "Ошибка")
    store.remove("model", "rev")
    assert not directory.exists()
    assert not directory.with_name("rev.json").exists()
    assert not (store.root / "current.json").exists()
    assert store.current() is None
    assert store.records() == ()


def test_remove_other_revision_preserves_current(store: ModelStore) -> None:
    install(store)
    other = install(store, revision="other")
    store.set_current("model", "rev")
    current = store.current()
    store.remove("model", "other")
    assert not other.exists()
    assert not other.with_name("other.json").exists()
    assert store.current() == current


def test_remove_flushes_after_neighbor_state_is_deleted(
    store: ModelStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = install(store)
    state_path = directory.with_name("rev.json")
    original_fsync = st._fsync_dir
    synced: list[Path] = []

    def fsync_after_removal(parent: Path) -> None:
        assert not directory.exists()
        assert not state_path.exists()
        original_fsync(parent)
        synced.append(parent)

    monkeypatch.setattr(st, "_fsync_dir", fsync_after_removal)
    store.remove("model", "rev")
    assert synced == [directory.parent]


def test_filesystem_errors_have_public_codes(store: ModelStore) -> None:
    store.root.write_text("Это файл")
    with pytest.raises(StoreError) as error:
        store.staging_dir("model", "rev")
    assert error.value.code == "broken-store"
