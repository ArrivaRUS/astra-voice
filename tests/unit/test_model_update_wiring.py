"""M7: проверки моделей проходят через общий worker и барьер онбординга, без сети."""

from __future__ import annotations

import hashlib
import json
import stat
import threading
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest

from astra_voice.core import paths
from astra_voice.core.policy import Policy
from astra_voice.core.settings import Settings
from astra_voice.models import catalog_state
from astra_voice.models.catalog import Catalog, CatalogEntry, CatalogError, FileSpec, load_builtin
from astra_voice.models.installer import Installer, SmokeResult
from astra_voice.models.store import ModelStore
from astra_voice.net import github, update_cache
from astra_voice.net.gate import NetworkGate
from astra_voice.net.http import MODEL_CHECK_JITTER_MAX_S, CheckResult, HttpClient, NetworkError
from astra_voice.net.update_cache import UpdateCache
from astra_voice.security.verify import Verifier, VerifyResult
from astra_voice.updates import catalog_updates as catalog_module
from astra_voice.updates import checker as checker_module
from astra_voice.updates import model_updates as model_module
from astra_voice.updates.checker import CHECK_INTERVAL_S, UpdateChecker
from astra_voice.updates.model_updates import ModelCheckSummary, ModelUpdateChecker
from astra_voice.updates.state import StateStore

pytestmark = pytest.mark.unit
T0 = 1_800_000_000.0
DATA_ROOT = Path(__file__).resolve().parents[2] / "data"
CATALOG_URL = "https://models.example.ru/catalog.json"


class RecordingModels:
    """Записанная граница backend: transport, jitter и хранилище проверяются отдельно."""

    def __init__(self, calls: list[bool]) -> None:
        self.calls = calls

    def run_once(self, cancel: threading.Event, *, manual: bool = False) -> ModelCheckSummary:
        assert not cancel.is_set()
        self.calls.append(manual)
        return ModelCheckSummary("checked")


class WiringRig:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.now = T0
        self.settings = Settings(check_app_updates=True, check_model_updates=True)
        self.policy = Policy()
        self.calls: list[bool] = []
        self.app_calls: list[str] = []
        self.checker: UpdateChecker | None = None

    def discovery(self, *args: Any, **kwargs: Any) -> github.ReleaseCheck:
        self.app_calls.append(kwargs["kind"])
        return github.ReleaseCheck("uptodate", requests_made=1)

    def start(self) -> UpdateChecker:
        gate = NetworkGate(self.settings, self.policy)
        self.checker = UpdateChecker(
            gate,
            HttpClient(gate, user_agent="astra-voice/test"),
            cache=UpdateCache(self.root / "cache.json"),
            store=StateStore(self.root / "state.json"),
            current_version="0.2.0",
            clock=lambda: self.now,
            jitter_max_s=0.0,
            model_checker=cast(ModelUpdateChecker, RecordingModels(self.calls)),
        )
        self.checker.start(scheduled=False, delay_s=0.0)
        self.settle()
        return self.checker

    def settle(self) -> None:
        assert self.checker is not None
        for _ in range(2):
            finished = threading.Event()
            self.checker._request(action=finished.set)
            assert finished.wait(5), "планировщик не обработал очередь"


@pytest.fixture
def wiring(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[WiringRig]:
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    rig = WiringRig(tmp_path)
    monkeypatch.setattr(github, "check", rig.discovery)
    try:
        yield rig
    finally:
        if rig.checker is not None:
            rig.checker.stop()


def test_model_schedule_waits_for_onboarding(wiring: WiringRig) -> None:
    checker = wiring.start()
    for offset in (1.0, CHECK_INTERVAL_S, CHECK_INTERVAL_S * 2):
        wiring.now = T0 + offset
        checker.refresh()
        wiring.settle()
        assert wiring.calls == []

    checker.enable_scheduled_checks()
    wiring.settle()
    assert wiring.calls == [False]


def test_manual_check_runs_models_without_opening_schedule(wiring: WiringRig) -> None:
    checker = wiring.start()
    checker.check_now()
    wiring.settle()
    assert wiring.calls == [True]

    wiring.now += CHECK_INTERVAL_S + 1
    checker.refresh()
    wiring.settle()
    assert wiring.calls == [True]


def test_manual_check_with_models_disabled_does_not_run_models(wiring: WiringRig) -> None:
    wiring.settings.check_model_updates = False
    checker = wiring.start()
    checker.check_now()
    wiring.settle()
    assert wiring.calls == []
    assert wiring.app_calls == ["check_app_manual"]


def test_model_schedule_is_independent_of_app_toggle(wiring: WiringRig) -> None:
    wiring.settings.check_app_updates = False
    checker = wiring.start()
    checker.enable_scheduled_checks()
    wiring.settle()
    assert wiring.calls == [False]
    assert wiring.app_calls == []


@pytest.mark.parametrize("reason", ["offline", "policy-offline", "policy-models-disabled"])
def test_manual_model_checks_respect_closed_network_gate(wiring: WiringRig, reason: str) -> None:
    if reason == "offline":
        wiring.settings.offline = True
    elif reason == "policy-offline":
        wiring.policy = Policy(values={"updates": "offline"})
    else:
        wiring.policy = Policy(values={"check_model_updates": False})
    checker = wiring.start()
    checker.check_now()
    wiring.settle()
    assert wiring.calls == []


def test_app_factory_passes_model_store_catalog_provider_and_shared_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model_store = ModelStore(tmp_path / "models")
    cache = UpdateCache(tmp_path / "cache.json")
    catalog = Catalog(serial=5, trust_epoch=1, entries=(), revoked=())
    arguments: dict[str, Any] = {}

    def provider() -> Catalog:
        return catalog

    def make_models(client: HttpClient, **kwargs: Any) -> RecordingModels:
        arguments.update(kwargs)
        assert isinstance(client, HttpClient)
        return RecordingModels([])

    monkeypatch.setattr(checker_module, "shared_cache", lambda: cache)
    monkeypatch.setattr(model_module, "ModelUpdateChecker", make_models)
    checker = checker_module.create_app_checker(
        Settings(), Policy(), model_store=model_store, catalog=provider
    )
    assert isinstance(checker, UpdateChecker)
    assert arguments["store"] is model_store
    assert arguments["catalog"] is provider
    assert arguments["cache"] is cache


def tiny_installed_model(tmp_path: Path) -> tuple[ModelStore, CatalogEntry]:
    revision = "a" * 40
    repo = "istupakov/gigaam-v3-onnx"
    contents = {
        "v3_e2e_ctc.int8.onnx": b"\x08\x03\x12\x04test",
        "v3_e2e_ctc_vocab.txt": "а\nб\n".encode(),
        "config.json": b'{"model_type":"gigaam"}',
    }
    entry = CatalogEntry(
        id="gigaam-v3-e2e-ctc-int8",
        revision=revision,
        name="GigaAM v3 CTC",
        description="Тестовая модель",
        size_bytes=sum(map(len, contents.values())),
        min_ram_mb=1,
        layout="onnx-asr-gigaam-v3",
        variant="gigaam-v3-e2e-ctc",
        recommended=True,
        host="huggingface.co",
        files=tuple(
            FileSpec(
                name,
                hashlib.sha256(data).hexdigest(),
                len(data),
                f"/{repo}/resolve/{revision}/{name}",
            )
            for name, data in contents.items()
        ),
    )
    store = ModelStore(tmp_path / "models")
    staging = store.staging_dir(entry.id, revision)
    for name, data in contents.items():
        (staging / name).write_bytes(data)
    result = Installer(store, lambda directory, model: SmokeResult(True)).install_from_staging(
        entry
    )
    assert result.state == "ok", result.reason
    return store, entry


def test_manual_check_interrupts_scheduled_model_jitter_from_app_factory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Фабрика должна передать backend ожидание, прерываемое общим check_now()."""
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    store, entry = tiny_installed_model(tmp_path)
    revision = entry.revision
    catalog = Catalog(serial=5, trust_epoch=1, entries=(entry,), revoked=())
    waiting = threading.Event()
    checked_manually = threading.Event()
    jitters: list[float] = []

    def discovery(self: HttpClient, url: str, **kwargs: Any) -> CheckResult:
        assert kwargs["kind"] == "check_models"
        jitter = kwargs["jitter_max_s"]
        jitters.append(jitter)
        if jitter:
            waiting.set()
            wait = kwargs.get("wait")
            assert wait is not None, "фабрика потеряла прерываемое ожидание"
            assert wait(5.0), "check_now не прервал плановую задержку моделей"
            raise NetworkError("cancelled", "Плановая задержка прервана")
        checked_manually.set()
        body = json.dumps({"branches": [{"name": "main", "targetCommit": revision}]}).encode()
        return CheckResult("fresh", body=body, http_status=200, requests_made=1)

    monkeypatch.setattr(HttpClient, "get_check", discovery)
    monkeypatch.setattr(
        github, "check", lambda *args, **kwargs: github.ReleaseCheck("uptodate", requests_made=1)
    )
    checker = checker_module.create_app_checker(
        Settings(check_app_updates=False, check_model_updates=True),
        Policy(),
        model_store=store,
        catalog=lambda: catalog,
    )
    try:
        checker.start(scheduled=False, delay_s=0.0)
        checker.enable_scheduled_checks()
        assert waiting.wait(5), "плановая проверка моделей не началась"
        checker.check_now()
        assert checked_manually.wait(5), "ручная проверка моделей не выполнилась"
        assert jitters == [MODEL_CHECK_JITTER_MAX_S, 0.0]
    finally:
        checker.stop()


@pytest.mark.parametrize(
    "configured_by", ["settings", "policy", "policy-over-settings", "cleared-with-persisted-source"]
)
def test_factory_connects_configured_catalog_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, configured_by: str
) -> None:
    model_store = ModelStore(tmp_path / "models")
    cache = UpdateCache(tmp_path / "cache.json")
    baseline = Catalog(serial=4, trust_epoch=1, entries=(), revoked=())
    updated = Catalog(serial=5, trust_epoch=1, entries=(), revoked=())
    source_args: dict[str, Any] = {}
    model_args: dict[str, Any] = {}
    refreshes: list[threading.Event] = []

    class RecordedSource:
        def __init__(self, client: HttpClient, **kwargs: Any) -> None:
            assert isinstance(client, HttpClient)
            source_args.update(kwargs)

        def current(self) -> Catalog:
            return updated

        def refresh(self, cancel: threading.Event) -> None:
            refreshes.append(cancel)

    def make_models(client: HttpClient, **kwargs: Any) -> RecordingModels:
        model_args.update(kwargs)
        return RecordingModels([])

    settings = Settings(check_model_updates=True)
    policy = Policy()
    expected_url = CATALOG_URL
    state_root = tmp_path / "state"
    if configured_by == "settings":
        settings.extra["catalog_url"] = CATALOG_URL
    elif configured_by == "cleared-with-persisted-source":
        (state_root / "model-catalog-updates").mkdir(parents=True)
        expected_url = ""
    else:
        policy = Policy(values={"catalog_url": CATALOG_URL})
        if configured_by == "policy-over-settings":
            settings.extra["catalog_url"] = "https://other.example.ru/catalog.json"
    monkeypatch.setattr(checker_module, "shared_cache", lambda: cache)
    monkeypatch.setattr(paths, "state_dir", lambda: state_root)
    monkeypatch.setattr(catalog_module, "CatalogUpdateSource", RecordedSource)
    monkeypatch.setattr(model_module, "ModelUpdateChecker", make_models)

    checker_module.create_app_checker(
        settings, policy, model_store=model_store, catalog=lambda: baseline
    )

    configured_url = source_args["catalog_url"]
    assert (configured_url() if callable(configured_url) else configured_url) == expected_url
    assert source_args["cache"] is cache
    assert model_args["catalog"]() is updated
    cancel = threading.Event()
    model_args["refresh_catalog"](cancel)
    assert refreshes == [cancel]


def test_catalog_refresh_precedes_hf_comparison(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    store, entry = tiny_installed_model(tmp_path)
    remote_revision = "b" * 40
    remote = replace(
        entry,
        revision=remote_revision,
        files=tuple(
            replace(file, url_path=file.url_path.replace(entry.revision, remote_revision))
            for file in entry.files
        ),
    )
    current = [Catalog(serial=4, trust_epoch=1, entries=(entry,), revoked=())]
    calls: list[str] = []
    notices: list[model_module.ModelUpdateNotice] = []

    def refresh(cancel: threading.Event) -> None:
        assert not cancel.is_set()
        calls.append("catalog")
        current[0] = Catalog(serial=5, trust_epoch=1, entries=(remote,), revoked=())

    def discovery(self: HttpClient, url: str, **kwargs: Any) -> CheckResult:
        calls.append("refs")
        assert current[0].entry(entry.id) == remote
        body = json.dumps(
            {"branches": [{"name": "main", "targetCommit": remote_revision}]}
        ).encode()
        return CheckResult("fresh", body=body, http_status=200, requests_made=1)

    monkeypatch.setattr(HttpClient, "get_check", discovery)
    gate = NetworkGate(Settings(check_model_updates=True), Policy())
    checker = ModelUpdateChecker(
        HttpClient(gate, user_agent="astra-voice/test"),
        store=store,
        catalog=lambda: current[0],
        cache=UpdateCache(tmp_path / "cache.json"),
        state_path=tmp_path / "model-state.json",
        refresh_catalog=refresh,
        notify=notices.append,
    )
    summary = checker.run_once(threading.Event(), manual=True)
    assert calls == ["catalog", "refs"]
    assert len(summary.notices) == 1 and notices == list(summary.notices)
    assert summary.notices[0].remote_revision == remote_revision
    active = store.current()
    assert active is not None and active.revision == entry.revision


class CatalogVerifier(Verifier):
    """Криптографическая граница, как StubVerifier в исходных каталоговых тестах."""

    def __init__(self) -> None:
        super().__init__("catalog", keyring=DATA_ROOT / "keys/release.gpg")
        self.calls = 0

    def verify_detached(self, data: Path, sig: Path, **kwargs: Any) -> VerifyResult:
        assert data.is_absolute() and sig.is_absolute()
        self.calls += 1
        return VerifyResult(ok=True)


class CatalogSourceRig:
    def __init__(self, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.verifier = CatalogVerifier()
        self.global_path = root / "baseline" / "catalog-state.json"
        self.global_path.parent.mkdir(parents=True)
        self.baseline = load_builtin(
            self.verifier, root=DATA_ROOT, require_schema=True, state_path=self.global_path
        )
        self.global_before = self.global_path.read_bytes()
        self.state_dir = root / "remote"
        self.cache = UpdateCache(root / "update-cache.json")
        self.client = HttpClient(
            NetworkGate(Settings(check_model_updates=True), Policy()), user_agent="astra-voice/test"
        )
        self.raw = (DATA_ROOT / "catalog.json").read_bytes()
        self.remote_raw = self.with_serial(self.baseline.serial + 1)
        self.fetch_calls = 0

        def recorded_fetch(client: HttpClient, **kwargs: Any) -> tuple[bytes, bytes]:
            assert kwargs["catalog_url"] == CATALOG_URL
            assert not kwargs["cancel"].is_set()
            self.fetch_calls += 1
            return self.remote_raw, b"recorded catalog signature"

        monkeypatch.setattr(catalog_module, "fetch", recorded_fetch)

    def with_serial(self, serial: int) -> bytes:
        document = json.loads(self.raw)
        document["serial"] = serial
        return json.dumps(document, ensure_ascii=False).encode()

    def source(self) -> Any:
        return catalog_module.CatalogUpdateSource(
            self.client,
            baseline=lambda: self.baseline,
            catalog_url=CATALOG_URL,
            verifier=self.verifier,
            schema_root=DATA_ROOT,
            state_dir=self.state_dir,
            global_state_path=self.global_path,
            cache=self.cache,
            clock=lambda: T0,
        )


@pytest.fixture
def catalog_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> CatalogSourceRig:
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    return CatalogSourceRig(tmp_path, monkeypatch)


def test_catalog_source_restores_verified_snapshot_without_changing_global_state(
    catalog_source: CatalogSourceRig,
) -> None:
    source = catalog_source.source()
    source.refresh(threading.Event())
    accepted = source.current()
    assert accepted.serial == catalog_source.baseline.serial + 1
    assert catalog_source.global_path.read_bytes() == catalog_source.global_before
    for name in ("catalog-snapshot.json", "catalog-state.json"):
        assert stat.S_IMODE((catalog_source.state_dir / name).stat().st_mode) == 0o600

    verifications = catalog_source.verifier.calls
    restored = catalog_source.source().current()
    assert restored == accepted
    assert catalog_source.verifier.calls > verifications, "снимок должен повторно проверить подпись"
    assert catalog_source.global_path.read_bytes() == catalog_source.global_before


def test_corrupt_catalog_highwater_cannot_be_reset_to_baseline(
    catalog_source: CatalogSourceRig,
) -> None:
    source = catalog_source.source()
    source.refresh(threading.Event())
    watermark = catalog_source.state_dir / "catalog-state.json"
    watermark.write_bytes(b"{")

    with pytest.raises(CatalogError):
        catalog_source.source().current()

    assert watermark.read_bytes() == b"{", "повреждённый барьер нельзя молча пересоздать"
    assert catalog_source.global_path.read_bytes() == catalog_source.global_before


def test_catalog_source_rejects_rollback_after_restart(catalog_source: CatalogSourceRig) -> None:
    source = catalog_source.source()
    source.refresh(threading.Event())
    accepted = source.current()
    catalog_source.remote_raw = catalog_source.raw
    restarted = catalog_source.source()
    with pytest.raises(CatalogError):
        restarted.refresh(threading.Event())
    assert restarted.current() == accepted
    assert catalog_source.global_path.read_bytes() == catalog_source.global_before


def test_catalog_source_rebuilds_corrupt_new_model_tracker_from_accepted_snapshot(
    catalog_source: CatalogSourceRig,
) -> None:
    new_id = "gigaam-v3-e2e-rnnt-int8-next"
    document = json.loads(catalog_source.remote_raw)
    additional = dict(document["models"][0], id=new_id, recommended=False)
    document["models"].append(additional)
    catalog_source.remote_raw = json.dumps(document, ensure_ascii=False).encode()
    source = catalog_source.source()
    source.refresh(threading.Event())
    tracker_path = catalog_source.state_dir / "new-models.json"
    tracker = catalog_module.NewModelTracker(tracker_path)
    assert tracker.is_new(new_id, T0)

    tracker_path.write_bytes(b"{")
    restored = catalog_source.source().current()

    assert restored.entry(new_id) is not None
    assert not tracker.is_new(new_id, T0)
    assert tracker.new_ids(restored, T0) == frozenset()


def test_catalog_source_observes_updated_provider_and_canonical_marker_without_fetch(
    catalog_source: CatalogSourceRig,
) -> None:
    source = catalog_source.source()
    source.refresh(threading.Event())
    previous = source.current()
    newer_raw = catalog_source.with_serial(previous.serial + 1)
    catalog_source.baseline = catalog_module.apply_remote(
        newer_raw,
        b"validated newer catalog signature",
        catalog_source.verifier,
        schema_root=DATA_ROOT,
        state_path=catalog_source.global_path,
    )

    assert source.current() == catalog_source.baseline
    assert catalog_source.baseline.serial == previous.serial + 1
    assert catalog_source.fetch_calls == 1


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_newer_catalog_highwater_requires_valid_snapshot(
    catalog_source: CatalogSourceRig, damage: str
) -> None:
    source = catalog_source.source()
    source.refresh(threading.Event())
    snapshot = catalog_source.state_dir / "catalog-snapshot.json"
    if damage == "missing":
        snapshot.unlink()
    else:
        snapshot.write_bytes(b"{}")

    with pytest.raises(CatalogError):
        catalog_source.source().current()


@pytest.mark.parametrize("failed_write", ["snapshot", "watermark"])
def test_catalog_write_failure_blocks_publication_and_preserves_recoverable_state(
    catalog_source: CatalogSourceRig, monkeypatch: pytest.MonkeyPatch, failed_write: str
) -> None:
    source = catalog_source.source()
    baseline = source.current()
    marker = catalog_source.state_dir / "catalog-state.json"
    before = marker.read_bytes()
    snapshot = catalog_source.state_dir / "catalog-snapshot.json"
    original_snapshot_write = update_cache.write_private
    original_state_write = catalog_state.write_state

    def write_snapshot(path: Path, payload: bytes) -> None:
        if failed_write == "snapshot" and path == snapshot:
            raise OSError("recorded snapshot write failure")
        original_snapshot_write(path, payload)

    def write_marker(path: Path, state: catalog_state.CatalogState) -> None:
        if failed_write == "watermark" and path == marker:
            raise OSError("recorded highwater write failure")
        original_state_write(path, state)

    with monkeypatch.context() as failed:
        failed.setattr(update_cache, "write_private", write_snapshot)
        failed.setattr(catalog_state, "write_state", write_marker)
        with pytest.raises(CatalogError):
            source.refresh(threading.Event())
        with pytest.raises(CatalogError):
            source.current()

    assert marker.read_bytes() == before
    assert catalog_source.global_path.read_bytes() == catalog_source.global_before
    restarted = catalog_source.source().current()
    if failed_write == "snapshot":
        assert restarted == baseline
    else:
        # Успешно сохранённая подписанная пара восстанавливает отставший маркер.
        assert restarted.serial == baseline.serial + 1
