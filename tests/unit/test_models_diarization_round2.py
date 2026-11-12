"""Round 2: mutation exclusion, publication failure and bounded generations."""

from __future__ import annotations

import errno
import json
import os
import shutil
import stat
import subprocess
import sys
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from test_models_diarization import ROOT, install
from test_models_diarization import catalog as catalog
from test_models_diarization import pair as pair

from astra_voice.models import diarization as dm
from astra_voice.models.installer import Installer
from astra_voice.models.store import StoreError

pytestmark = pytest.mark.unit


def advance(pair: Any, revision: str) -> None:
    holder = pair[3]
    entries = []
    for entry in holder[0].entries:
        if entry.kind == "diarization":
            files = tuple(
                replace(file, url_path=file.url_path.replace(entry.revision, revision))
                for file in entry.files
            )
            entry = replace(entry, revision=revision, files=files)
        entries.append(entry)
    holder[0] = replace(holder[0], serial=holder[0].serial + 1, entries=tuple(entries))


def generations(pair: Any) -> set[str]:
    root = pair[1].root / dm.PAIR_DIRECTORY
    return {path.name for path in root.iterdir() if len(path.name) == 32 and path.is_dir()}


def test_r2_remove_between_model_installs_is_refused(
    pair: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, store, _, holder, _ = pair
    other = dm.DiarizationService(store, service._http, catalog_provider=lambda: holder[0])
    original = Installer.install_from_staging
    attempts: list[str] = []

    def commit(self: Installer, entry: Any, **kwargs: Any) -> Any:
        result = original(self, entry, **kwargs)
        if not attempts:
            attempts.append("removed")
            try:
                other.remove()
            except StoreError as error:
                attempts[0] = error.code
        return result

    monkeypatch.setattr(Installer, "install_from_staging", commit)
    assert service.install(cancel=threading.Event()).state == "ok"
    assert service.trusted_models() is not None, "install returned ok for half a pair"
    assert attempts == ["busy"]


def test_r2_second_install_is_refused_without_waiting(
    pair: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, store, _, holder, calls = pair
    other = dm.DiarizationService(store, service._http, catalog_provider=lambda: holder[0])
    original = Installer.install_from_staging
    results: list[Any] = []

    def commit(self: Installer, entry: Any, **kwargs: Any) -> Any:
        result = original(self, entry, **kwargs)
        if not results:
            results.append(None)
            results[0] = other.install(cancel=threading.Event())
        return result

    monkeypatch.setattr(Installer, "install_from_staging", commit)
    install(pair)
    assert results[0].state == "error"
    assert "уже выполняется" in results[0].reason.lower()
    assert len(calls) == 2


_CHILD = r"""
import json, sys, threading
from unittest.mock import Mock
sys.path.insert(0, sys.argv[3])
from astra_voice.models.catalog import Catalog
from astra_voice.models.diarization import DiarizationService
from astra_voice.models.store import ModelStore, StoreError
http = Mock()
http.gate.refusal.return_value = ""
service = DiarizationService(ModelStore(__import__("pathlib").Path(sys.argv[1])), http,
                            catalog_provider=lambda: Catalog(1, 1, (), ()))
if sys.argv[2] == "install":
    result = service.install(cancel=threading.Event())
    print(json.dumps({"state": result.state, "reason": result.reason}))
else:
    try:
        service.remove()
        print(json.dumps({"code": "removed"}))
    except StoreError as error:
        print(json.dumps({"code": error.code, "reason": error.message}))
"""


@pytest.mark.parametrize("operation", ["remove", "install"])
def test_r2_other_process_is_excluded(
    pair: Any, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    service, store, _, _, _ = pair
    original = Installer.install_from_staging
    attempts: list[dict[str, Any]] = []

    def commit(self: Installer, entry: Any, **kwargs: Any) -> Any:
        result = original(self, entry, **kwargs)
        if not attempts:
            process = subprocess.run(
                [sys.executable, "-c", _CHILD, str(store.root), operation, str(ROOT / "src")],
                capture_output=True,
                text=True,
                timeout=10,
                check=True,
            )
            attempts.append(json.loads(process.stdout))
        return result

    monkeypatch.setattr(Installer, "install_from_staging", commit)
    install(pair)
    assert service.trusted_models() is not None
    assert "уже выполняется" in attempts[0].get("reason", "").lower()
    if operation == "remove":
        assert attempts[0]["code"] == "busy"
    else:
        assert attempts[0]["state"] == "error"


def test_r2_both_installed_sets_are_rechecked_before_publication(
    pair: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _, _, _, _ = pair
    original = Installer.install_from_staging
    first: list[Path] = []

    def commit(self: Installer, entry: Any, **kwargs: Any) -> Any:
        result = original(self, entry, **kwargs)
        assert result.record is not None
        if not first:
            first.append(result.record.dir)
        else:
            shutil.rmtree(first[0])
        return result

    monkeypatch.setattr(Installer, "install_from_staging", commit)
    assert service.install(cancel=threading.Event()).state == "error"
    assert service.trusted_models() is None
    assert not (pair[1].root / dm.PAIR_DIRECTORY / "current.json").exists()


def test_r2_eio_after_pointer_replacement_preserves_ready_pair(
    pair: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(pair)
    service, store, _, _, calls = pair
    root = store.root / dm.PAIR_DIRECTORY
    pointer = root / "current.json"
    before = pointer.read_bytes()
    advance(pair, "revision-2")
    original = os.fsync
    injected: list[bool] = []

    def fsync(descriptor: int) -> None:
        if (
            not injected
            and stat.S_ISDIR(os.fstat(descriptor).st_mode)
            and os.readlink(f"/proc/self/fd/{descriptor}") == str(root)
            and pointer.read_bytes() != before
        ):
            injected.append(True)
            raise OSError(errno.EIO, "injected directory sync failure after replace")
        original(descriptor)

    monkeypatch.setattr(os, "fsync", fsync)
    service.install(cancel=threading.Event())
    assert injected == [True]
    assert service.trusted_models() is not None, "pointer references a deleted generation"
    before_retry = len(calls)
    install(pair)
    assert len(calls) == before_retry, "availability required another full download"


def test_r2_three_identical_installs_download_once(pair: Any) -> None:
    for _ in range(3):
        install(pair)
    assert len(pair[4]) == 2, "same ready pair was downloaded again"
    assert len(generations(pair)) == 1
    assert pair[0].trusted_models() is not None


def test_r2_three_revisions_keep_current_and_previous(pair: Any) -> None:
    seen = []
    for revision in ("revision-1", "revision-2", "revision-3"):
        advance(pair, revision)
        install(pair)
        ready = pair[0].trusted_models()
        assert ready is not None
        seen.append(Path(ready["segmentation_dir"]).parents[1])
    assert len(generations(pair)) == 2
    assert not seen[0].exists()
    assert seen[1].is_dir() and seen[2].is_dir()
    assert len(pair[4]) == 6


def test_r2_partial_recovered_by_install_not_trusted_models(pair: Any) -> None:
    install(pair)
    root = pair[1].root / dm.PAIR_DIRECTORY
    abandoned = root / ("a" * 32 + ".partial")
    abandoned.mkdir()
    (abandoned / "incomplete").write_bytes(b"x")
    before = {
        path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()
    }
    assert pair[0].trusted_models() is not None
    after = {
        path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()
    }
    assert after == before and abandoned.is_dir()
    install(pair)
    assert not abandoned.exists()
    assert len(pair[4]) == 2 and len(generations(pair)) == 1


def test_r2_idempotent_install_needs_no_network_permission(pair: Any) -> None:
    install(pair)
    pair[2].offline = True
    install(pair)
    assert len(pair[4]) == 2


def test_r2_trusted_models_does_not_migrate_legacy_metadata(pair: Any) -> None:
    install(pair)
    ready = pair[0].trusted_models()
    assert ready is not None
    directory = Path(ready["segmentation_dir"])
    metadata = directory.with_suffix(".json")
    legacy = directory / "state.json"
    metadata.rename(legacy)
    raw = legacy.read_bytes()
    assert pair[0].trusted_models() is None  # legacy state is an extra file
    assert legacy.read_bytes() == raw and not metadata.exists()


def test_r2_readonly_default_catalog_keeps_watermark_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from test_models_catalog import StubVerifier

    from astra_voice.core import paths
    from astra_voice.models import catalog_effective
    from astra_voice.models.catalog import CatalogError

    state_root = tmp_path / "read-only-state"
    monkeypatch.setattr(paths, "state_dir_path", lambda: state_root)

    def forbidden() -> None:
        pytest.fail("read-only catalog tried to create state directory")

    monkeypatch.setattr(paths, "state_dir", forbidden)
    verifier = StubVerifier()
    loaded = catalog_effective.load_effective(
        verifier, builtin_root=ROOT / "data", require_schema=True, persist=False
    )
    assert loaded.verified and not state_root.exists()
    # Read-only mode still enforces an already accepted higher watermark.
    from astra_voice.models import catalog_state

    marker = catalog_state.state_path(state_root)
    marker.parent.mkdir(parents=True)
    catalog_state.write_state(
        marker,
        catalog_state.CatalogState(
            loaded.trust_epoch + 1,
            1,
            "0" * 64,
            (),
            "3f78990acce3cc32",
        ),
    )
    before = marker.read_bytes()
    with pytest.raises(CatalogError) as refused:
        catalog_effective.load_effective(
            verifier, builtin_root=ROOT / "data", require_schema=True, persist=False
        )
    assert refused.value.code == "stale-catalog"
    assert marker.read_bytes() == before


def test_r2_readonly_revocations_do_not_create_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, catalog: Any
) -> None:
    from astra_voice.core import paths

    state_root = tmp_path / "absent-state"
    monkeypatch.setattr(paths, "state_dir_path", lambda: state_root)
    monkeypatch.setattr(paths, "state_dir", lambda: pytest.fail("state directory created"))
    dm._entries(catalog, read_only=True)
    assert not state_root.exists()


@pytest.mark.parametrize("failure", ["before-pointer", "generation-sync", "after-pointer"])
def test_r2_single_publication_failure_keeps_old_or_new_ready(
    pair: Any, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    install(pair)
    service, store, _, holder, _ = pair
    old_catalog = holder[0]
    root = store.root / dm.PAIR_DIRECTORY
    old_token = json.loads((root / "current.json").read_bytes())["generation"]
    advance(pair, "revision-2")
    from astra_voice.models.store import _fsync_dir as original_sync
    from astra_voice.net.update_cache import write_private as original_write

    injected: list[bool] = []

    def write(path: Path, raw: bytes) -> None:
        if failure == "before-pointer":
            injected.append(True)
            raise OSError(errno.EIO, "injected before pointer replace")
        original_write(path, raw)
        if failure == "after-pointer":
            injected.append(True)
            raise OSError(errno.EIO, "injected after pointer replace")

    def sync(path: Path) -> None:
        if (
            failure == "generation-sync"
            and path == root
            and not injected
            and any(token != old_token for token in generations(pair))
        ):
            injected.append(True)
            raise OSError(errno.EIO, "injected before pointer publication")
        original_sync(path)

    monkeypatch.setattr(dm, "write_private", write)
    monkeypatch.setattr(dm, "_fsync_dir", sync)
    assert service.install(cancel=threading.Event()).state == "error"
    assert injected == [True]
    assert (
        service.trusted_models() is not None
        or dm.trusted_diarization_models(store, catalog_provider=lambda: old_catalog) is not None
    )
    assert (root / old_token).is_dir()


def test_r2_prune_failure_can_be_retried_without_another_download(
    pair: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(pair)
    first = next(iter(generations(pair)))
    advance(pair, "revision-2")
    install(pair)
    advance(pair, "revision-3")
    original = shutil.rmtree
    injected: list[bool] = []

    def discard(path: Any, *args: Any, **kwargs: Any) -> None:
        if Path(path).name == first and not injected:
            injected.append(True)
            raise OSError(errno.EIO, "injected old-generation cleanup failure")
        original(path, *args, **kwargs)

    monkeypatch.setattr(shutil, "rmtree", discard)
    assert pair[0].install(cancel=threading.Event()).state == "error"
    assert injected and pair[0].trusted_models() is not None
    count = len(pair[4])
    install(pair)
    assert len(pair[4]) == count and len(generations(pair)) == 2


def test_r2_crashed_process_releases_lock_and_partial_is_not_installed(pair: Any) -> None:
    script = r"""
import os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[2])
from astra_voice.models.diarization import _mutation_lock
from astra_voice.models.store import ModelStore
with _mutation_lock(ModelStore(Path(sys.argv[1]))) as root:
    partial = root / ("c" * 32 + ".partial")
    partial.mkdir()
    (partial / "unfinished").write_bytes(b"unfinished model download")
    os._exit(17)
"""
    process = subprocess.run(
        [sys.executable, "-c", script, str(pair[1].root), str(ROOT / "src")],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert process.returncode == 17, process.stderr
    partial = pair[1].root / dm.PAIR_DIRECTORY / ("c" * 32 + ".partial")
    assert partial.is_dir() and pair[0].trusted_models() is None
    assert partial.is_dir(), "read-only provider performed recovery"
    install(pair)
    assert not partial.exists() and pair[0].trusted_models() is not None
    assert len(pair[4]) == 2
