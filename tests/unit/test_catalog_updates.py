"""Манифест с `catalog_url`, «Новое» ≤ 30 дней, «снята с каталога» (v1.0; US-6.3/6.6, S8-A4).

Тесты написаны ДО кода (T-216…T-220, `docs/test-plan.md`). Модуля ещё нет: тесты
падают на импорте внутри фикстуры ``cu`` (ожидаемый red).

Предлагаемый интерфейс ``astra_voice.updates.catalog_updates``:

    NEW_BADGE_WINDOW_S = 30 * 24 * 3600.0

    def catalog_urls(catalog_url: str) -> tuple[str, str]
        # (адрес catalog.json, адрес подписи = тот же + ".sig"); только https, ASCII-хост,
        # без userinfo, query, fragment, "..", управляющих символов → иначе ValueError
    def fetch(client: HttpClient, *, catalog_url: str, cancel: threading.Event,
              kind: NetworkKind = "check_models") -> tuple[bytes, bytes]
        # два GET через HttpClient (гейт, хосты, редиректы — там); тело > CATALOG_MAX_BYTES →
        # CatalogError("too-large") без чтения сверх лимита; гейт закрыт → NetworkError
    def apply_remote(raw: bytes, signature: bytes, verifier: Verifier, *, schema_root: Path,
                     state_path: Path, require_schema: bool = True) -> Catalog
        # тот же порядок, что load_builtin: лимит → подпись → JSON → схема → поля →
        # анти-откат (catalog_state.apply_state); любой отказ — CatalogError, состояние не меняется
    class NewModelTracker:
        def __init__(self, path: Path) -> None          # файл 0600
        def observe(self, catalog: Catalog, now: float) -> None
            # первый каталог (и после порчи файла) — база: его модели «Новым» не считаются;
            # модель, впервые встреченная позже, «Новая» с момента первой встречи
        def is_new(self, model_id: str, now: float) -> bool   # now - first_seen ≤ 30 дней
        def new_ids(self, catalog: Catalog, now: float) -> frozenset[str]
    def removed_installed(catalog: Catalog, records: Iterable[ModelRecord]) -> frozenset[str]
        # установленные исправные модели, которых нет в каталоге → «Снята с каталога»
"""

from __future__ import annotations

import pytest

pytest.importorskip("jsonschema")
pytest.importorskip("requests")

# ruff: noqa: E402
import copy
import hashlib
import importlib
import json
import os
import stat
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from types import ModuleType
from typing import Any, cast
from urllib.parse import urlsplit

import requests

from astra_voice.core.policy import Policy
from astra_voice.core.settings import Settings
from astra_voice.models import catalog_state
from astra_voice.models.catalog import CATALOG_MAX_BYTES, Catalog, CatalogError, load_builtin
from astra_voice.models.store import ModelRecord
from astra_voice.net import http
from astra_voice.net.gate import NetworkGate
from astra_voice.net.http import HttpClient, NetworkError
from astra_voice.security.verify import Verifier, VerifyResult
from helpers.http_fault_server import FaultServer

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_ROOT = REPO_ROOT / "data"
KEYRING = DATA_ROOT / "keys/release.gpg"
DAY = 24 * 3600.0
T0 = 1_790_000_000.0
CATALOG_PATH = "/astra-voice/catalog.json"


@pytest.fixture
def cu() -> ModuleType:
    """Модуль под тестом; пока его нет — ModuleNotFoundError (ожидаемый red)."""
    return importlib.import_module("astra_voice.updates.catalog_updates")


class StubVerifier(Verifier):
    """Разбор без закрытого ключа: результат подписи задаёт тест (как в test_models_catalog)."""

    def __init__(self, *, ok: bool = True) -> None:
        super().__init__("catalog", keyring=KEYRING)
        self.result = VerifyResult(ok=True) if ok else VerifyResult(ok=False, reason="отказ теста")
        self.calls = 0

    def verify_detached(
        self, data: Path, sig: Path, *, cancel: Callable[[], bool] | None = None
    ) -> VerifyResult:
        assert data.is_absolute() and sig.is_absolute()
        self.calls += 1
        return self.result


@pytest.fixture
def document() -> dict[str, Any]:
    return cast(dict[str, Any], json.loads((DATA_ROOT / "catalog.json").read_bytes()))


def encode(document: dict[str, Any]) -> bytes:
    return json.dumps(document, ensure_ascii=False).encode("utf-8")


def with_serial(document: dict[str, Any], serial: int, trust_epoch: int = 1) -> dict[str, Any]:
    result = copy.deepcopy(document)
    result["serial"] = serial
    result["trust_epoch"] = trust_epoch
    return result


@pytest.fixture
def state_file(tmp_path: Path) -> Path:
    """Состояние после встроенного каталога пакета: (trust_epoch, serial) = (1, 4)."""
    path = catalog_state.state_path(tmp_path / "state")
    path.parent.mkdir(parents=True)
    builtin = load_builtin(StubVerifier(), root=DATA_ROOT, state_path=path)
    assert (builtin.trust_epoch, builtin.serial) == (1, 4)
    return path


def apply(cu: ModuleType, raw: bytes, state_file: Path, verifier: Verifier | None = None) -> Any:
    return cu.apply_remote(
        raw,
        b"stub signature",
        verifier if verifier is not None else StubVerifier(),
        schema_root=DATA_ROOT,
        state_path=state_file,
    )


def assert_refused(
    cu: ModuleType, raw: bytes, state_file: Path, code: str, verifier: Verifier | None = None
) -> None:
    before = state_file.read_bytes()
    with pytest.raises(CatalogError) as refused:
        apply(cu, raw, state_file, verifier)
    assert refused.value.code == code
    assert state_file.read_bytes() == before, "отказ не должен трогать принятое состояние"


# -- T-216: адрес манифеста ---------------------------------------------------


def test_catalog_urls(cu: ModuleType) -> None:
    url = "https://models.example.ru/astra-voice/catalog.json"
    assert cu.catalog_urls(url) == (url, url + ".sig")


@pytest.mark.parametrize(
    "url",
    [
        "",
        "http://models.example.ru/astra-voice/catalog.json",
        "ftp://models.example.ru/catalog.json",
        "file:///etc/astra-voice/catalog.json",
        "https://user:pass@models.example.ru/catalog.json",
        "https://models.example.ru@evil.example/catalog.json",
        "https://модели.рф/catalog.json",
        "https://models.example.ru/catalog.json?serial=1",
        "https://models.example.ru/catalog.json#x",
        "https:///catalog.json",
        "https://models.example.ru/a/../catalog.json",
        "https://models.example.ru/catalog.json\n",
        "https://models.example.ru/cat\x00alog.json",
        " https://models.example.ru/catalog.json",
    ],
)
def test_catalog_url_rejected(cu: ModuleType, url: str) -> None:
    with pytest.raises(ValueError):
        cu.catalog_urls(url)


# -- T-216: получение через HttpClient ---------------------------------------


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in tuple(os.environ):
        if name.lower().endswith("_proxy"):
            monkeypatch.delenv(name)
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    original: Callable[..., requests.Response] = requests.adapters.HTTPAdapter.send

    def local_send(
        self: requests.adapters.HTTPAdapter, request: requests.PreparedRequest, **kwargs: object
    ) -> requests.Response:
        assert urlsplit(request.url).hostname == "127.0.0.1", "запрос мимо локального сервера"
        return original(self, request, **kwargs)

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", local_send)


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> Iterator[FaultServer]:
    with FaultServer() as local:
        monkeypatch.setattr(http, "ALLOWED_HOSTS", ("127.0.0.1",))
        monkeypatch.setattr(http, "ALLOWED_SCHEMES", ("http",))
        monkeypatch.setattr(
            http, "ALLOWED_PORTS", (None, local.server.server_port if local.server else 0)
        )
        yield local


def client(enabled: bool = True, *, offline: bool = False) -> HttpClient:
    return HttpClient(
        NetworkGate(Settings(check_model_updates=enabled, offline=offline), Policy()),
        user_agent="astra-voice/test",
    )


def test_fetch_catalog_and_signature(
    cu: ModuleType, server: FaultServer, document: dict[str, Any]
) -> None:
    raw = encode(document)
    server.bodies[CATALOG_PATH] = raw
    server.bodies[CATALOG_PATH + ".sig"] = b"signature"
    got = cu.fetch(client(), catalog_url=server.url + CATALOG_PATH, cancel=threading.Event())
    assert got == (raw, b"signature")
    assert sorted(path for path, _ in server.requests) == [CATALOG_PATH, CATALOG_PATH + ".sig"]


def test_fetch_oversized_manifest(cu: ModuleType, server: FaultServer) -> None:
    """T-23 (хвост «манифест 1 МиБ + 1 байт — теста не найдено»): отказ «слишком большой»."""
    server.bodies[CATALOG_PATH] = b" " * (CATALOG_MAX_BYTES + 1)
    server.bodies[CATALOG_PATH + ".sig"] = b"signature"
    with pytest.raises(CatalogError) as refused:
        cu.fetch(client(), catalog_url=server.url + CATALOG_PATH, cancel=threading.Event())
    assert refused.value.code == "too-large"


@pytest.mark.parametrize(
    ("enabled", "offline"), [(False, False), (True, True)], ids=["тумблер-выключен", "офлайн"]
)
def test_fetch_closed_gate_zero_requests(
    cu: ModuleType, server: FaultServer, enabled: bool, offline: bool
) -> None:
    server.bodies[CATALOG_PATH] = b"{}"
    with pytest.raises(NetworkError) as refused:
        cu.fetch(
            client(enabled, offline=offline),
            catalog_url=server.url + CATALOG_PATH,
            cancel=threading.Event(),
        )
    assert refused.value.code == "no-network"
    assert server.requests == []


def test_fetch_redirect_to_foreign_host(cu: ModuleType, server: FaultServer) -> None:
    with pytest.raises((NetworkError, CatalogError)):
        cu.fetch(client(), catalog_url=server.url + "/evil", cancel=threading.Event())
    assert [path for path, _ in server.requests] == ["/evil"]


# -- T-217: анти-откат манифеста и подпись ------------------------------------


def test_newer_serial_applied(cu: ModuleType, state_file: Path, document: dict[str, Any]) -> None:
    catalog = apply(cu, encode(with_serial(document, 5)), state_file)
    assert isinstance(catalog, Catalog)
    assert (catalog.trust_epoch, catalog.serial) == (1, 5)
    state = catalog_state.read_state(state_file)
    assert state is not None and (state.trust_epoch, state.serial) == (1, 5)


def test_newer_trust_epoch_wins_over_serial(
    cu: ModuleType, state_file: Path, document: dict[str, Any]
) -> None:
    catalog = apply(cu, encode(with_serial(document, 1, trust_epoch=2)), state_file)
    assert (catalog.trust_epoch, catalog.serial) == (2, 1)


def test_older_serial_rejected(cu: ModuleType, state_file: Path, document: dict[str, Any]) -> None:
    """US-6.6: (1, 3) после (1, 4) — «каталог старее текущего», предыдущий остаётся."""
    assert_refused(cu, encode(with_serial(document, 3)), state_file, "stale-catalog")


def test_same_serial_other_bytes_rejected(
    cu: ModuleType, state_file: Path, document: dict[str, Any]
) -> None:
    changed = with_serial(document, 4)
    changed["models"][0]["description"] = "Подменённое описание"
    assert_refused(cu, encode(changed), state_file, "stale-catalog")


def test_bad_signature_rejected(cu: ModuleType, state_file: Path, document: dict[str, Any]) -> None:
    assert_refused(
        cu, encode(with_serial(document, 5)), state_file, "bad-signature", StubVerifier(ok=False)
    )


def test_oversized_rejected_before_signature(
    cu: ModuleType, state_file: Path, document: dict[str, Any]
) -> None:
    verifier = StubVerifier()
    raw = encode(with_serial(document, 5))
    raw += b" " * (CATALOG_MAX_BYTES + 1 - len(raw))
    assert_refused(cu, raw, state_file, "too-large", verifier)
    assert verifier.calls == 0


def test_duplicate_keys_rejected(
    cu: ModuleType, state_file: Path, document: dict[str, Any]
) -> None:
    raw = encode(with_serial(document, 5))
    raw = raw.replace(b'"serial": 5', b'"serial": 5, "serial": 6', 1)
    assert b'"serial": 6' in raw
    assert_refused(cu, raw, state_file, "bad-schema")


def test_path_traversal_rejected(
    cu: ModuleType, state_file: Path, document: dict[str, Any]
) -> None:
    changed = with_serial(document, 5)
    changed["models"][0]["files"][0]["path"] = "../../evil.onnx"
    assert_refused(cu, encode(changed), state_file, "bad-path")


def test_revoked_active_revision_recorded(
    cu: ModuleType, state_file: Path, document: dict[str, Any]
) -> None:
    """T-218: revoked[] нового манифеста — в каталоге и в снимке catalog-state (У84)."""
    model = document["models"][0]
    changed = with_serial(document, 5)
    changed["revoked"] = [
        {"model_id": model["id"], "revision": model["revision"], "reason": "уязвимость"}
    ]
    catalog = apply(cu, encode(changed), state_file)
    assert catalog.is_revoked(model["id"], model["revision"])
    state = catalog_state.read_state(state_file)
    assert state is not None and state.revoked == ((model["id"], model["revision"]),)


# -- T-219: «Новое» ≤ 30 дней -------------------------------------------------


def catalog_from(document: dict[str, Any], tmp_path: Path, name: str) -> Catalog:
    root = tmp_path / name
    root.mkdir()
    (root / "catalog.json").write_bytes(encode(document))
    (root / "catalog.json.sig").write_bytes(b"stub")
    (root / "catalog.schema.json").write_bytes((DATA_ROOT / "catalog.schema.json").read_bytes())
    return load_builtin(StubVerifier(), root=root)


@pytest.fixture
def catalogs(tmp_path: Path, document: dict[str, Any]) -> tuple[Catalog, Catalog]:
    """Встроенный каталог и следующий — с одной новой моделью."""
    builtin = catalog_from(document, tmp_path, "builtin")
    extended = with_serial(document, 5)
    newcomer = copy.deepcopy(extended["models"][1])
    newcomer["id"] = "gigaam-v3-e2e-ctc-int8-next"
    newcomer["name"] = "GigaAM v3 CTC (новая)"
    extended["models"].append(newcomer)
    return builtin, catalog_from(extended, tmp_path, "remote")


def test_first_catalog_is_baseline(
    cu: ModuleType, tmp_path: Path, catalogs: tuple[Catalog, Catalog]
) -> None:
    builtin, _ = catalogs
    tracker = cu.NewModelTracker(tmp_path / "new-models.json")
    tracker.observe(builtin, T0)
    assert tracker.new_ids(builtin, T0) == frozenset()
    assert not tracker.is_new(builtin.entries[0].id, T0)


def test_new_model_badge_for_30_days(
    cu: ModuleType, tmp_path: Path, catalogs: tuple[Catalog, Catalog]
) -> None:
    """S8-A4: новая модель манифеста — бейдж «Новое» ровно 30 дней от первой встречи."""
    builtin, remote = catalogs
    path = tmp_path / "new-models.json"
    tracker = cu.NewModelTracker(path)
    tracker.observe(builtin, T0)
    seen_at = T0 + DAY
    tracker.observe(remote, seen_at)
    assert tracker.new_ids(remote, seen_at) == frozenset({"gigaam-v3-e2e-ctc-int8-next"})
    # Повторное наблюдение не продлевает срок.
    tracker.observe(remote, seen_at + 10 * DAY)
    reopened = cu.NewModelTracker(path)
    assert reopened.is_new("gigaam-v3-e2e-ctc-int8-next", seen_at + 30 * DAY)
    assert not reopened.is_new("gigaam-v3-e2e-ctc-int8-next", seen_at + 30 * DAY + 1)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize("payload", [b"", b"{", b"[]", b'{"x": "y"}', b"\xff"])
def test_corrupt_tracker_is_baseline_not_flood(
    cu: ModuleType, tmp_path: Path, catalogs: tuple[Catalog, Catalog], payload: bytes
) -> None:
    """Испорченный файл не превращает все 13 карточек в «Новое»."""
    _, remote = catalogs
    path = tmp_path / "new-models.json"
    path.write_bytes(payload)
    tracker = cu.NewModelTracker(path)
    tracker.observe(remote, T0)
    assert tracker.new_ids(remote, T0) == frozenset()


# -- T-220: «Снята с каталога» -------------------------------------------------


def record(model_id: str, revision: str, state: str = "ok") -> ModelRecord:
    return ModelRecord(
        model_id,
        revision,
        Path("/nonexistent") / model_id / revision,
        "onnx-asr-gigaam-v3",
        "gigaam-v3-e2e-ctc",
        1,
        state=cast(Any, state),
    )


def test_removed_installed(cu: ModuleType, catalogs: tuple[Catalog, Catalog]) -> None:
    builtin, _ = catalogs
    present = builtin.entries[0]
    other_revision = hashlib.sha1(b"other").hexdigest()
    removed = cu.removed_installed(
        builtin,
        [
            record(present.id, present.revision),
            record(present.id, other_revision),  # та же модель, другая ревизия — не «снята»
            record("old-model-gone", other_revision),
        ],
    )
    assert removed == frozenset({"old-model-gone"})


def test_removed_installed_ignores_catalog_only(
    cu: ModuleType, catalogs: tuple[Catalog, Catalog]
) -> None:
    builtin, _ = catalogs
    assert cu.removed_installed(builtin, []) == frozenset()
