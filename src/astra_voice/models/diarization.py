"""Signed meeting models: isolated generations and one atomic pair pointer.

No ASR current.json is touched. A generation is invisible to dictation's
ModelStore, including after its entries disappear from the catalog. The only
ready state is current.json published after BOTH installed sets pass checks.
Abandoned generations never constitute an installed pair.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import stat
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import uuid4

from astra_voice.core import paths
from astra_voice.models.catalog import (
    DIARIZATION_MODEL_IDS,
    Catalog,
    CatalogEntry,
    CatalogError,
    load_builtin,
)
from astra_voice.models.catalog_effective import CATALOG_LOCK, check_revoked, load_effective
from astra_voice.models.downloader import Downloader, DownloadError, Progress
from astra_voice.models.installer import (
    Installer,
    InstallResult,
    ReasonCode,
    SmokeResult,
    verify_installed,
)
from astra_voice.models.store import ModelStore, StoreError, _fsync_dir
from astra_voice.net.http import HttpClient, NetworkError
from astra_voice.net.update_cache import read_private, write_private
from astra_voice.security.verify import HashCancelledError, Verifier

MODEL_IDS = DIARIZATION_MODEL_IDS
SEGMENTATION_ID, EMBEDDER_ID = MODEL_IDS
PAIR_DIRECTORY = ".diarization"
CatalogProvider = Callable[[], Catalog]
EventSink = Callable[[str, tuple[Any, ...]], None]
_PAIR_LOCK = threading.RLock()
_BUSY_MESSAGE = "Уже выполняется установка или удаление моделей разделения. Попробуйте позже."
_LOCK_FILE = "mutation.lock"


def _ignore_event(kind: str, args: tuple[Any, ...]) -> None:
    pass


def _effective_catalog() -> Catalog:
    return load_effective(
        Verifier("catalog", keyring=paths.data_dir_static() / "keys/release.gpg"),
        require_schema=True,
    )


def _read_only_effective_catalog() -> Catalog:
    return load_effective(
        Verifier("catalog", keyring=paths.data_dir_static() / "keys/release.gpg"),
        require_schema=True,
        persist=False,
    )


def _entries(catalog: Catalog, *, read_only: bool = False) -> tuple[CatalogEntry, CatalogEntry]:
    if not catalog.verified:
        raise StoreError("catalog-unavailable")
    segmentation, embedder = (catalog.entry(model_id) for model_id in MODEL_IDS)
    if (
        segmentation is None
        or embedder is None
        or segmentation.kind != "diarization"
        or embedder.kind != "diarization"
        or segmentation.layout != "sherpa-onnx-pyannote-segmentation"
        or embedder.layout != "sherpa-onnx-campplus"
    ):
        raise StoreError("catalog-unavailable")
    for entry in (segmentation, embedder):
        check_revoked(entry.id, entry.revision, catalog, include_snapshot=True, read_only=read_only)
    return segmentation, embedder


def _pair_root(store: ModelStore) -> Path:
    root = store.root / PAIR_DIRECTORY
    if root.is_symlink() or not root.resolve().is_relative_to(store.root.resolve()):
        raise StoreError("bad-id")
    return root


@contextmanager
def _mutation_lock(store: ModelStore) -> Iterator[Path]:
    """One nonblocking writer across threads/processes; never unlink the inode."""
    root = _pair_root(store)
    paths._ensure_private_dir(store.root)
    paths._ensure_private_dir(root)
    descriptor = os.open(
        root / _LOCK_FILE,
        os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
        0o600,
    )
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid():
            raise StoreError("bad-id")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise StoreError("busy", _BUSY_MESSAGE) from None
        yield root
    finally:
        os.close(descriptor)


def _valid_generation(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{32}", value) is not None


def _published_generations(root: Path) -> tuple[str | None, str | None]:
    try:
        pointer = json.loads(read_private(root / "current.json", 4096))
    except (FileNotFoundError, ValueError):
        return None, None
    if not isinstance(pointer, dict):
        return None, None
    current, previous = pointer.get("generation"), pointer.get("previous_generation")
    return (
        current if _valid_generation(current) else None,
        previous if _valid_generation(previous) else None,
    )


def _discard_generation(path: Path) -> None:
    if path.is_symlink() or not path.is_dir():
        path.unlink()
    else:
        shutil.rmtree(path)


def _cleanup_generations(root: Path) -> None:
    """Writer lock required. Recover partials and retain current + one previous."""
    for child in root.iterdir():
        if re.fullmatch(r"[0-9a-f]{32}\.partial", child.name):
            _discard_generation(child)
    current, previous = _published_generations(root)
    keep = {token for token in (current, previous) if token is not None}
    completed = [child for child in root.iterdir() if _valid_generation(child.name)]
    # Legacy pointers have no previous field. Keep the newest remaining image
    # once, until the next publication records the precise predecessor.
    for child in sorted(completed, key=lambda path: path.lstat().st_mtime_ns, reverse=True):
        if len(keep) >= 2:
            break
        keep.add(child.name)
    for child in completed:
        if child.name not in keep:
            _discard_generation(child)
    _fsync_dir(root)


def _install_error(error: Exception, cancel: threading.Event) -> tuple[InstallResult, str]:
    code = getattr(error, "code", "")
    state = (
        "cancelled"
        if cancel.is_set() or code == "cancelled"
        else "no-space"
        if code == "disk-full"
        else f"failed:{code}"
        if code in {"admin", "offline", "settings"}
        else "broken"
    )
    reason_code: ReasonCode = (
        "revoked"
        if code == "revoked"
        else "catalog-unavailable"
        if isinstance(error, CatalogError) or code == "catalog-unavailable"
        else "cancelled"
        if state == "cancelled"
        else ""
    )
    return InstallResult(
        "error",
        _BUSY_MESSAGE if code == "busy" else "Не удалось установить модели разделения.",
        reason_code=reason_code,
    ), state


def catalog_diarization_hashes() -> dict[str, str]:
    """Signed builtin hashes for explicit calibration inputs; fail closed."""
    catalog = load_builtin(
        Verifier("catalog", keyring=paths.data_dir_static() / "keys/release.gpg"),
        require_schema=True,
    )
    with CATALOG_LOCK:
        segmentation, embedder = _entries(catalog)
        return {
            "segmentation_sha256": segmentation.files[0].sha256,
            "embedder_sha256": embedder.files[0].sha256,
        }


def trusted_diarization_models(
    store: ModelStore | None = None,
    *,
    catalog_provider: CatalogProvider = _read_only_effective_catalog,
) -> dict[str, str] | None:
    """Return directories + signed hashes, or None on ANY missing/untrusted set.

    The default provider verifies the effective signed catalog. An application's
    provider must return the Catalog produced by that same verified loader.
    Hashes are checked here and again by the role on its sealed snapshots.
    """
    try:
        store = store if store is not None else ModelStore(paths.model_store_dir_path())
        catalog = catalog_provider()
        with _PAIR_LOCK, CATALOG_LOCK:
            entries = _entries(catalog, read_only=True)
            root = _pair_root(store)
            pointer = json.loads(read_private(root / "current.json", 4096))
            if not isinstance(pointer, dict) or set(pointer) not in (
                {"generation", "models"},
                {"generation", "models", "previous_generation"},
            ):
                return None
            generation = pointer["generation"]
            if not isinstance(generation, str) or re.fullmatch(r"[0-9a-f]{32}", generation) is None:
                return None
            directory = root / generation
            if directory.is_symlink() or not directory.is_dir():
                return None
            expected = [[entry.id, entry.revision] for entry in entries]
            if pointer["models"] != expected:
                return None
            pair_store = ModelStore(directory)
            result = {}
            for prefix, entry in zip(("segmentation", "embedder"), entries, strict=True):
                directory = pair_store._installed(entry.id, entry.revision)
                record = pair_store._record(
                    entry.id, entry.revision, directory, migrate_legacy=False
                )
                if (
                    record is None
                    or record.state != "ok"
                    or record.recheck
                    or not record.metadata_ok
                ):
                    return None
                ok, _reason = verify_installed(record.dir, entry)
                if not ok:
                    return None
                result[f"{prefix}_dir"] = str(record.dir.resolve())
                result[f"{prefix}_sha256"] = entry.files[0].sha256
            return result
    except (CatalogError, StoreError, OSError, ValueError, RuntimeError, TypeError):
        return None


class DiarizationService:
    """Blocking pair operation; run in the model-job thread, never the GUI thread.

    Events match model_downloads._ModelJob: stage(str), source(str),
    progress(float), finished(state, reason). Return is the shared InstallResult.
    Download policy/host/TLS/size/hash limits are enforced by the supplied
    HttpClient and the existing Downloader, including policy changes mid-load.
    """

    def __init__(
        self,
        store: ModelStore,
        http: HttpClient,
        *,
        catalog_provider: CatalogProvider = _effective_catalog,
        corp_base: str | None = None,
    ) -> None:
        self._store = store
        self._http = http
        self._catalog_provider = catalog_provider
        self._corp_base = corp_base

    def trusted_models(self) -> dict[str, str] | None:
        provider = (
            _read_only_effective_catalog
            if self._catalog_provider is _effective_catalog
            else self._catalog_provider
        )
        return trusted_diarization_models(self._store, catalog_provider=provider)

    def install(self, *, cancel: threading.Event, sink: EventSink = _ignore_event) -> InstallResult:
        """Download both into an unpublished generation, verify, publish once."""
        try:
            with _mutation_lock(self._store) as root:
                with _PAIR_LOCK:
                    result, state = self._install(cancel, sink, root)
        except (StoreError, OSError, paths.PathError, ValueError) as error:
            result, state = _install_error(error, cancel)
        sink("finished", (state, result.reason))
        return result

    def _install(
        self, cancel: threading.Event, sink: EventSink, root: Path
    ) -> tuple[InstallResult, str]:
        generation: Path | None = None
        publication_attempted = False
        try:
            _cleanup_generations(root)
            if cancel.is_set():
                raise DownloadError("cancelled")
            catalog = self._catalog_provider()
            with CATALOG_LOCK:
                entries = _entries(catalog)
            if (
                trusted_diarization_models(self._store, catalog_provider=lambda: catalog)
                is not None
            ):
                return InstallResult("ok"), "installed"
            refusal = self._http.gate.refusal("download")
            if refusal:
                raise DownloadError(refusal)
            previous, _ = _published_generations(root)
            token = uuid4().hex
            generation = root / (token + ".partial")
            paths._ensure_private_dir(generation)
            pair_store = ModelStore(generation)
            downloader = Downloader(self._http, pair_store, corp_base=self._corp_base)
            # No native ASR selfcheck: ONNX is scanned before commit by Installer
            # and the role validates immutable snapshots before native inference.
            installer = Installer(pair_store, lambda *_: SmokeResult(True), catalog)
            total = sum(entry.size_bytes for entry in entries)
            if not pair_store.disk_ok(total):
                raise DownloadError("disk-full")
            completed = 0
            for entry in entries:
                if cancel.is_set():
                    raise DownloadError("cancelled")
                sink("stage", ("downloading",))

                def progress(value: Progress, base: int = completed) -> None:
                    fraction = (base + value.bytes_done) / total
                    sink("progress", (max(0.0, min(1.0, fraction)),))

                downloader.download(
                    entry,
                    progress=progress,
                    cancel=cancel,
                    source=lambda kind: sink("source", (kind,)),
                )
                sink("stage", ("verifying",))
                # Unlike the ASR resume path, a pair's fresh staging permits NO
                # extra files (including .part left by an injected downloader).
                staging = generation / entry.id / (entry.revision + ".partial")
                if not verify_installed(staging, entry, cancel=cancel.is_set)[0]:
                    return InstallResult(
                        "error", "Модель не прошла проверку.", reason_code="checksum"
                    ), "broken"
                sink("stage", ("installing",))
                result = installer.install_from_staging(entry, cancel=cancel.is_set, activate=False)
                if result.state != "ok":
                    state = (
                        "cancelled"
                        if result.reason_code == "cancelled"
                        else "no-space"
                        if result.reason_code == "disk"
                        else "broken"
                    )
                    return replace(result, record=None), state
                completed += entry.size_bytes
            # Resolve provider outside the catalog barrier (it can perform I/O).
            latest = self._catalog_provider()
            with CATALOG_LOCK:
                if _entries(latest) != entries:
                    raise StoreError("catalog-unavailable")
                for entry in entries:
                    directory = pair_store._installed(entry.id, entry.revision)
                    record = pair_store._record(
                        entry.id, entry.revision, directory, migrate_legacy=False
                    )
                    if (
                        record.state != "ok"
                        or not record.metadata_ok
                        or record.recheck
                        or not verify_installed(directory, entry, cancel=cancel.is_set)[0]
                    ):
                        raise StoreError("broken-store")
                if cancel.is_set():
                    raise DownloadError("cancelled")
                final = root / token
                generation.rename(final)
                generation = final
                _fsync_dir(root)
                pointer: dict[str, Any] = {
                    "generation": token,
                    "models": [[entry.id, entry.revision] for entry in entries],
                }
                if previous is not None:
                    pointer["previous_generation"] = previous
                # write_private can replace the pointer successfully and THEN
                # raise on directory fsync. From this point the new complete
                # image may be referenced; never remove it on an ambiguous error.
                publication_attempted = True
                write_private(root / "current.json", json.dumps(pointer).encode())
                _cleanup_generations(root)
            return InstallResult("ok"), "installed"
        except (
            DownloadError,
            NetworkError,
            StoreError,
            CatalogError,
            HashCancelledError,
            paths.PathError,
            OSError,
            ValueError,
        ) as error:
            return _install_error(error, cancel)
        finally:
            if generation is not None and not publication_attempted:
                shutil.rmtree(generation, ignore_errors=True)

    def remove(self) -> None:
        """Withdraw the entire pair first; then delete all pair generations.

        An interrupted deletion remains not-installed. No dictation revision or
        pointer is ever removed. Symlink generations are unlinked, not followed.
        """
        if not _pair_root(self._store).exists():
            return
        with _mutation_lock(self._store) as root, _PAIR_LOCK:
            (root / "current.json").unlink(missing_ok=True)
            _fsync_dir(root)
            for child in root.iterdir():
                if re.fullmatch(r"[0-9a-f]{32}(?:\.partial)?", child.name):
                    _discard_generation(child)
            _fsync_dir(root)
