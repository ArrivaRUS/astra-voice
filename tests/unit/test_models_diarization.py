"""T-431 / T1 T-221: signed pair lifecycle and immutable role admission."""

from __future__ import annotations

import hashlib
import json
import runpy
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest
from test_meeting_diar import command, drain
from test_models_catalog import StubVerifier

from astra_voice.core.policy import Policy
from astra_voice.core.settings import Settings
from astra_voice.models.catalog import Catalog, CatalogError, RevokedEntry, load_builtin
from astra_voice.models.diarization import MODEL_IDS, DiarizationService, trusted_diarization_models
from astra_voice.models.downloader import Downloader
from astra_voice.models.store import ModelStore, StoreError
from astra_voice.net.gate import NetworkGate
from astra_voice.net.http import HttpClient, NetworkError
from astra_voice.updates.catalog_updates import NewModelTracker, removed_installed
from astra_voice.updates.model_updates import watch_target
from astra_voice.worker import ipc
from astra_voice.worker import meeting_diar as diar

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]
RAW = b"\x08\x01"
SHA = hashlib.sha256(RAW).hexdigest()


@pytest.fixture
def catalog(tmp_path: Path) -> Catalog:
    directory = tmp_path / "catalog"
    directory.mkdir()
    doc = json.loads((ROOT / "data/catalog.json").read_bytes())
    for entry in doc["models"]:
        if entry.get("kind") == "diarization":
            entry["size_bytes"] = len(RAW)
            entry["files"][0].update(size=len(RAW), sha256=SHA)
    (directory / "catalog.json").write_text(json.dumps(doc))
    (directory / "catalog.json.sig").write_bytes(b"synthetic parser test only")
    (directory / "catalog.schema.json").write_bytes(
        (ROOT / "data/catalog.schema.json").read_bytes()
    )
    return load_builtin(StubVerifier(), root=directory, require_schema=True)


@pytest.fixture
def pair(tmp_path: Path, catalog: Catalog, monkeypatch: pytest.MonkeyPatch) -> Any:
    settings = Settings()
    http = HttpClient(NetworkGate(settings, Policy()), user_agent="test")
    calls: list[str] = []

    class Response:
        status = 200
        headers: dict[str, str] = {}

        def iter_chunks(self, *args: Any, **kwargs: Any) -> Iterator[bytes]:
            yield RAW

        def close(self) -> None:
            pass

    @contextmanager
    def transport(url: str, **kwargs: Any) -> Iterator[Response]:
        assert url.startswith("https://huggingface.co/")
        calls.append(url)
        yield Response()

    monkeypatch.setattr(http, "get_stream", transport)
    store = ModelStore(tmp_path / "store")
    holder = [catalog]
    service = DiarizationService(store, http, catalog_provider=lambda: holder[0])
    return service, store, settings, holder, calls


def install(pair: Any) -> None:
    assert pair[0].install(cancel=threading.Event()).state == "ok"


def test_schema_and_reproducible_build() -> None:
    from astra_voice.models.schema import SchemaInvalid, validate

    schema = json.loads((ROOT / "data/catalog.schema.json").read_bytes())
    document = json.loads((ROOT / "data/catalog.json").read_bytes())
    validate(document, schema)
    module = runpy.run_path(str(ROOT / "scripts/build_manifest.py"))
    built = module["build"](
        json.loads((ROOT / "data/catalog-source.json").read_bytes()),
        json.loads((ROOT / "data/catalog-hf-cache.json").read_bytes()),
        serial=document["serial"],
        generated_at=document["generated_at"],
    )
    assert module["_dump"](built) == (ROOT / "data/catalog.json").read_bytes()
    assert built["serial"] > 4
    assert len(built["models"]) == 14
    for change in ("license", "layout", "kind", "variant", "path", "recommended"):
        bad = deepcopy(document)
        entry = bad["models"][-1]
        if change == "license":
            del entry[change]
        elif change == "path":
            entry["files"][0]["path"] = "other.onnx"
        else:
            entry[change] = True if change == "recommended" else "unknown"
        with pytest.raises(SchemaInvalid):
            validate(bad, schema)


def test_pair_success_events_and_removal(pair: Any) -> None:
    service, store, _, _, calls = pair
    events: list[tuple[str, tuple[Any, ...]]] = []

    def event(kind: str, args: tuple[Any, ...]) -> None:
        events.append((kind, args))
        if kind != "finished":
            assert service.trusted_models() is None

    assert service.install(cancel=threading.Event(), sink=event).state == "ok"
    assert len(calls) == 2
    assert events[-1] == ("finished", ("installed", ""))
    fractions = [args[0] for kind, args in events if kind == "progress"]
    assert fractions == sorted(fractions) and fractions[-1] == 1
    result = service.trusted_models()
    assert result is not None
    assert result["segmentation_sha256"] == result["embedder_sha256"] == SHA
    assert store.records() == ()
    assert store.current() is None
    assert not (store.root / "current.json").exists()
    service.remove()
    assert service.trusted_models() is None
    service.remove()


@pytest.mark.parametrize("damage", ["second", "sha", "size", "extra", "part", "external"])
def test_pair_failure_never_publishes_half(
    pair: Any, damage: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, store, _, holder, _ = pair
    original = Downloader.download
    count = 0
    if damage == "external":
        from test_engine_scan import _field, _tensor

        raw = _field(7, _field(5, _tensor("../x")))
        entry = holder[0].entry(MODEL_IDS[1])
        assert entry is not None
        damaged = replace(
            entry,
            size_bytes=len(raw),
            files=(replace(entry.files[0], size=len(raw), sha256=hashlib.sha256(raw).hexdigest()),),
        )
        holder[0] = replace(
            holder[0],
            entries=tuple(damaged if item.id == damaged.id else item for item in holder[0].entries),
        )

    def download(self: Downloader, entry: Any, **kwargs: Any) -> Path:
        nonlocal count
        count += 1
        if count == 2 and damage == "second":
            raise NetworkError("no-network", "test")
        if count == 2 and damage == "external":
            directory = self._store.staging_dir(entry.id, entry.revision)
            (directory / entry.files[0].path).write_bytes(raw)
        else:
            directory = original(self, entry, **kwargs)
        if count == 2:
            if damage == "sha":
                (directory / entry.files[0].path).write_bytes(b"\x08\x02")
            elif damage == "size":
                (directory / entry.files[0].path).write_bytes(RAW + b"more")
            elif damage in {"extra", "part"}:
                (directory / ("other.part" if damage == "part" else "extra.txt")).write_bytes(b"x")
        return directory

    monkeypatch.setattr(Downloader, "download", download)
    assert service.install(cancel=threading.Event()).state == "error"
    assert service.trusted_models() is None
    assert store.records() == ()
    assert {path.name for path in (store.root / ".diarization").iterdir()} == {"mutation.lock"}


@pytest.mark.parametrize("blocked", ["offline", "admin"])
def test_policy_and_offline_no_transport(pair: Any, blocked: str) -> None:
    service, _, settings, _, calls = pair
    if blocked == "offline":
        settings.offline = True
    else:
        service._http.gate._policy = Policy(values={"offline": "true"})
    events: list[Any] = []
    assert (
        service.install(cancel=threading.Event(), sink=lambda *e: events.append(e)).state == "error"
    )
    assert events[-1][1][0] == f"failed:{blocked}"
    assert calls == []


@pytest.mark.parametrize("case", ["none", "one", "both", "revoked", "tampered", "unsigned"])
def test_trusted_provider_table(pair: Any, case: str) -> None:
    service, store, _, holder, _ = pair
    if case != "none":
        install(pair)
        result = service.trusted_models()
        assert result is not None
        if case == "one":
            import shutil

            shutil.rmtree(result["embedder_dir"])
        elif case == "tampered":
            (Path(result["segmentation_dir"]) / "model.onnx").write_bytes(b"\x08\x02")
        elif case == "revoked":
            entry = holder[0].entry(MODEL_IDS[1])
            assert entry is not None
            holder[0] = replace(
                holder[0], revoked=(RevokedEntry(entry.id, entry.revision, "test"),)
            )
        elif case == "unsigned":
            holder[0] = replace(holder[0], verified=False)
    result = trusted_diarization_models(store, catalog_provider=lambda: holder[0])
    assert (result is not None) == (case == "both")


def test_revocation_before_publication_and_failed_reinstall_keeps_old_pair(
    pair: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(pair)
    service, store, _, holder, _ = pair
    before = (store.root / ".diarization/current.json").read_bytes()
    holder[0] = replace(
        holder[0],
        entries=tuple(
            replace(item, revision="retry-revision") if item.kind == "diarization" else item
            for item in holder[0].entries
        ),
    )
    original = Downloader.download

    def revoked(self: Downloader, entry: Any, **kwargs: Any) -> Path:
        directory = original(self, entry, **kwargs)
        if entry.id == MODEL_IDS[1]:
            holder[0] = replace(
                holder[0], revoked=(RevokedEntry(entry.id, entry.revision, "test"),)
            )
        return directory

    monkeypatch.setattr(Downloader, "download", revoked)
    assert service.install(cancel=threading.Event()).reason_code == "revoked"
    assert (store.root / ".diarization/current.json").read_bytes() == before
    assert service.trusted_models() is None


def test_unverified_catalog_refused_before_downloader(pair: Any) -> None:
    service, _, _, holder, calls = pair
    holder[0] = replace(holder[0], verified=False)
    assert service.install(cancel=threading.Event()).reason_code == "catalog-unavailable"
    assert calls == []


def test_dictation_consumers(catalog: Catalog, pair: Any, tmp_path: Path) -> None:
    from astra_voice.ui.model_downloads import ModelService

    install(pair)
    _, store, _, _, _ = pair
    service = ModelService.__new__(ModelService)
    service._catalog = catalog
    service._store = store
    assert len(service.entries()) == len(catalog.dictation_entries) == 12
    assert all(entry.kind == "dictation" for entry in service.entries())
    assert service.recommended() in catalog.dictation_entries
    assert service.installed_ids() == ()
    assert not service.installed_ok() and not service.broken()
    assert service.current_ids() is None
    assert service.recheck_entries() == ()
    assert all(service.catalog_rtfx(mid) is None for mid in MODEL_IDS)
    tracker = NewModelTracker(tmp_path / "new.json")
    tracker.observe(replace(catalog, entries=catalog.dictation_entries), 1)
    tracker.observe(catalog, 2)
    assert tracker.new_ids(catalog, 2) == frozenset()
    assert all(watch_target(catalog.entry(mid)) is None for mid in MODEL_IDS)  # type: ignore[arg-type]
    assert removed_installed(catalog, store.records()) == frozenset()
    # An orphaned pair never becomes a 'removed from catalog' dictation card.
    service._catalog = replace(catalog, entries=catalog.dictation_entries)
    assert len(service.entries()) == 12
    # CLI list/diagnostics operate on the same default store view.
    assert store.records() == ()
    benchmark = runpy.run_path(str(ROOT / "tools/benchmark"))["catalog_entries"]
    benchmark.__globals__["load_builtin"] = Mock(return_value=catalog)
    entries, error = benchmark()
    assert not error and len(entries) == 12
    assert not any(mid in {key[0] for key in entries} for mid in MODEL_IDS)


@pytest.mark.parametrize("expected", [SHA, "0" * 64, None])
def test_role_checks_catalog_digest_before_native(
    tmp_path: Path, expected: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    for names in diar.MODEL_FILES:
        (tmp_path / names[0]).write_bytes(RAW)
    native = Mock()
    monkeypatch.setattr(diar, "_NativeSherpa", native)
    message = command(
        "diar.load",
        segmentation_dir=str(tmp_path),
        embedder_dir=str(tmp_path),
        threshold=0.3,
        tau=0.45,
        threads=2,
        embedder_sha256=SHA,
    )
    if expected is not None:
        message["segmentation_sha256"] = expected
    # Missing hash must pass IPC to a stable model-untrusted role refusal.
    ipc.decode(ipc.encode(message, role=diar.ROLE)[4:], role=diar.ROLE)
    state = diar.MeetingDiarState()
    try:
        state.handle(message)
        reply = drain(state)[-1]
        assert reply["type"] == ("diar.loaded" if expected == SHA else "error")
        if expected != SHA:
            assert reply["code"] == "model-untrusted"
            native.assert_not_called()
        else:
            native.assert_called_once()
    finally:
        state.cleanup()


def test_unsigned_catalog_loader_fails_closed(tmp_path: Path) -> None:
    from astra_voice.security.verify import Verifier

    # The repository signature is intentionally stale until the owner's signing.
    # Use a synthetic invalid signature so this regression survives that signing.
    for name in ("catalog.json", "catalog.schema.json"):
        (tmp_path / name).write_bytes((ROOT / "data" / name).read_bytes())
    (tmp_path / "catalog.json.sig").write_bytes(b"not a signature")
    with pytest.raises(CatalogError, match="подпись"):
        load_builtin(Verifier("catalog", keyring=ROOT / "data/keys/release.gpg"), root=tmp_path)


def test_pair_cannot_be_selected_for_dictation(pair: Any) -> None:
    install(pair)
    service, _, _, _, _ = pair
    result = service.trusted_models()
    assert result is not None
    # Even direct low-level misuse cannot make a diarization revision ASR current.
    generation = Path(result["segmentation_dir"]).parents[1]
    store = ModelStore(generation)
    record = store.records(include_diarization=True)[0]
    with pytest.raises(StoreError):
        store.set_current(record.id, record.revision)
    assert store.current() is None and store.records() == ()


def test_cancel_does_not_publish_and_failed_retry_keeps_previous(
    pair: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(pair)
    service, store, _, holder, _ = pair
    previous_catalog = holder[0]
    holder[0] = replace(
        holder[0],
        entries=tuple(
            replace(item, revision="retry-revision") if item.kind == "diarization" else item
            for item in holder[0].entries
        ),
    )
    pointer = store.root / ".diarization/current.json"
    before = pointer.read_bytes()
    original = Downloader.download
    cancel = threading.Event()

    def download(self: Downloader, entry: Any, **kwargs: Any) -> Path:
        result = original(self, entry, **kwargs)
        cancel.set()
        return result

    monkeypatch.setattr(Downloader, "download", download)
    assert service.install(cancel=cancel).reason_code == "cancelled"
    assert pointer.read_bytes() == before
    holder[0] = previous_catalog
    assert service.trusted_models() is not None


def test_role_extra_file_refused_before_native(tmp_path: Path) -> None:
    for names in diar.MODEL_FILES:
        (tmp_path / names[0]).write_bytes(RAW)
    (tmp_path / "extra.txt").write_text("x")
    with pytest.raises(diar.ModelUntrusted):
        with diar.checked_models([str(tmp_path)] * 2, [SHA] * 2):
            pytest.fail("extra file accepted")


def test_dictation_installer_refuses_pair_entries(pair: Any) -> None:
    from astra_voice.models.installer import Installer, SmokeResult

    _, store, _, holder, _ = pair
    entry = holder[0].entry(MODEL_IDS[0])
    assert entry is not None
    smoke = Mock(return_value=SmokeResult(True))
    installer = Installer(store, smoke, holder[0])
    assert installer.install_from_staging(entry).reason_code == "layout"
    assert installer.install_from_path(Path("/unused"), entry).reason_code == "layout"
    smoke.assert_not_called()
    assert not store.root.exists()


def test_diar_hash_contract_roundtrip() -> None:
    from astra_voice.models.schema import validate
    from astra_voice.worker.meeting_contract import OPTIONAL

    models = {
        "segmentation_dir": "/a",
        "embedder_dir": "/b",
        "segmentation_sha256": SHA,
        "embedder_sha256": SHA,
    }
    validate(models, OPTIONAL["meeting.job.start"]["diar_models"])
    wire = ipc.encode(
        command("diar.load", **models, threshold=0.3, tau=0.45, threads=2), role=diar.ROLE
    )
    decoded = ipc.decode(wire[4:], role=diar.ROLE)
    assert {key: decoded[key] for key in models} == models


def test_cli_explicit_hashes_are_forwarded_without_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tools.meeting_diar_check import RoleProcess

    for names in diar.MODEL_FILES:
        (tmp_path / names[0]).write_bytes(RAW)
    role = RoleProcess.__new__(RoleProcess)
    messages: list[Any] = []
    monkeypatch.setattr(role, "send", lambda *args, **kwargs: messages.append((args, kwargs)))
    monkeypatch.setattr(role, "wait", lambda *args: {})
    role.load(tmp_path, segmentation_sha256=SHA, embedder_sha256=SHA)
    assert messages[0][1]["segmentation_sha256"] == SHA
    assert messages[0][1]["embedder_sha256"] == SHA


def test_cli_legacy_corpus_aliases_resolve_to_catalog_names(tmp_path: Path) -> None:
    from tools.meeting_diar_check import model_paths

    canonical = tmp_path / "weights"
    canonical.mkdir()
    for names, alias in zip(diar.MODEL_FILES, ("segmentation.onnx", "embedder.onnx"), strict=True):
        path = canonical / names[0]
        path.write_bytes(RAW)
        (tmp_path / alias).symlink_to(path)
    assert model_paths(tmp_path) == tuple(
        (canonical / names[0]).resolve() for names in diar.MODEL_FILES
    )


def test_first_run_cards_summary_selection_and_memory_ignore_pair(pair: Any) -> None:
    from astra_voice.ui.model_downloads import ModelDownloads, ModelService, catalog_best

    install(pair)
    pair_service, store, _, holder, _ = pair
    service = ModelService.__new__(ModelService)
    service._catalog = holder[0]
    service._store = store
    service._gate = pair_service._http.gate
    service._cancel = threading.Event()
    downloads = ModelDownloads(service)
    try:
        assert len(downloads.models) == 12
        assert not any(row["id"] in MODEL_IDS for row in downloads.models)
        assert downloads.installedCount == 0
        assert downloads.installedSummary.startswith("Установлено 0 из 12")
        for model_id in MODEL_IDS:
            downloads.makeModelCurrent(model_id)
        assert store.current() is None
        recommended = service.recommended()
        assert recommended is not None and recommended.kind == "dictation"
        # A future diarization metric must not change the dictation speed scale.
        entry = holder[0].entries[-1]
        from astra_voice.models.catalog import Metric, Metrics

        entry = replace(entry, metrics=Metrics(rtfx=Metric(1000, "https://example.org")))
        assert catalog_best((*holder[0].dictation_entries, entry)) == catalog_best(
            holder[0].dictation_entries
        )
    finally:
        downloads.shutdown()


def test_provider_unavailable_store_path_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    from astra_voice.core import paths

    monkeypatch.setattr(paths, "model_store_dir_path", Mock(side_effect=paths.PathError("test")))
    assert trusted_diarization_models() is None


def test_cancelled_installer_reports_cancelled_event(
    pair: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from astra_voice.models.installer import Installer, InstallResult

    service = pair[0]
    monkeypatch.setattr(
        Installer,
        "install_from_staging",
        Mock(return_value=InstallResult("error", reason_code="cancelled")),
    )
    events: list[Any] = []
    assert (
        service.install(cancel=threading.Event(), sink=lambda *e: events.append(e)).state == "error"
    )
    assert events[-1] == ("finished", ("cancelled", ""))
    assert service.trusted_models() is None


def test_transcriber_diar_client_forwards_catalog_hashes() -> None:
    from astra_voice.worker.meeting_diar_client import DiarProcess

    models = {
        "segmentation_dir": "/seg",
        "embedder_dir": "/emb",
        "segmentation_sha256": SHA,
        "embedder_sha256": SHA,
    }
    role = DiarProcess.__new__(DiarProcess)
    send = Mock(return_value="request")
    role.send = send  # type: ignore[method-assign]
    role.wait = Mock()  # type: ignore[method-assign]
    role.load(models)
    assert all(send.call_args.kwargs[key] == value for key, value in models.items())


def test_calibration_hashes_require_verified_catalog(
    catalog: Catalog, monkeypatch: pytest.MonkeyPatch
) -> None:
    from astra_voice.models import diarization

    monkeypatch.setattr(diarization, "load_builtin", Mock(return_value=catalog))
    assert diarization.catalog_diarization_hashes() == {
        "segmentation_sha256": SHA,
        "embedder_sha256": SHA,
    }
    monkeypatch.setattr(
        diarization, "load_builtin", Mock(return_value=replace(catalog, verified=False))
    )
    with pytest.raises(StoreError):
        diarization.catalog_diarization_hashes()
    monkeypatch.setattr(
        diarization, "load_builtin", Mock(side_effect=CatalogError("bad-signature", "test"))
    )
    with pytest.raises(CatalogError):
        diarization.catalog_diarization_hashes()
