"""Проверка и обновление ревизии модели (v1.0, M7-модели; US-6.1, US-6.2, PRD S8-A1…A3/A6/A7).

Тесты написаны ДО кода (T-205…T-215, `docs/test-plan.md`). Модуля ещё нет: тесты
падают на импорте внутри фикстуры ``mu`` (ожидаемый red).

Предлагаемый интерфейс ``astra_voice.updates.model_updates``:

    SOURCE = "models"
    CHECK_INTERVAL_S = 24 * 3600.0
    FAILED_MESSAGE = "Новая ревизия не прошла проверку, оставлена текущая"

    @dataclass(frozen=True)
    class WatchTarget:
        repo: str
        branch: str = "main"
    def watch_target(entry: CatalogEntry) -> WatchTarget | None
        # Д3: экспорт-репо из files[].url_path (все файлы — один repo), host huggingface.co|hf.co;
        # снятая с каталога / чужой хост / разные repo / битый путь → None (не проверяем)

    @dataclass(frozen=True)
    class ModelUpdateNotice:
        model_id: str
        name: str
        installed_revision: str
        remote_revision: str
    def status_line(notices: Sequence[ModelUpdateNotice]) -> str | None
        # строка `model-update` спеки §2.2: "Обновление модели: <name>"; пусто → None

    @dataclass(frozen=True)
    class ModelCheckSummary:
        state: Literal["disabled", "not-due", "checked", "rate-limited", "unavailable"]
        notices: tuple[ModelUpdateNotice, ...] = ()
        requests_made: int = 0
        retry_at: float | None = None

    class ModelUpdateChecker:
        def __init__(self, client: HttpClient, *, store: ModelStore,
                     catalog: Catalog | Callable[[], Catalog],
                     cache: UpdateCache, state_path: Path, api_base: str = hf.HF_BASE_URL,
                     clock: Callable[[], float] = time.time, rng: random.Random | None = None,
                     jitter_max_s: float = MODEL_CHECK_JITTER_MAX_S,
                     wait: Callable[[float], bool] | None = None,
                     notify: Callable[[ModelUpdateNotice], None] | None = None) -> None
        def run_once(self, cancel: threading.Event, *, manual: bool = False) -> ModelCheckSummary
        # ≤ 1 раза в 24 ч (last_attempt_at в state_path, 0600, переживает перезапуск);
        # manual=True — без суточного ограничения и случайной задержки;
        # гейт закрыт → "disabled", 0 запросов, попытка не засчитывается;
        # проверяется только активная исправная модель store.current() из каталога;
        # notify — один раз на (model_id, remote_revision), только если remote есть в каталоге;
        # иначе awaiting-catalog только в журнале; ничего не скачивает (S8-A1).

    class ModelRevisionUpdater:
        def __init__(self, store: ModelStore, downloader: DownloaderLike, installer: Installer,
                     catalog: Catalog, *,
                     on_state: Callable[[str, str], None] | None = None) -> None
            # DownloaderLike: download(entry, *, progress, cancel, source=None) -> Path
            # on_state(model_id, card_state): "updating" → "update-failed" (спека §5, сост. 25)
        def apply(self, model_id: str, *, cancel: threading.Event) -> RevisionUpdateResult
            # Д3: ставится только ревизия из ПОДПИСАННОГО каталога; staging → sha256 → смоук →
            # current.json (Installer.install_from_staging); провал → FAILED_MESSAGE, текущая живёт
        def revert_previous(self, model_id: str) -> RevisionUpdateResult   # P2, «если дёшево»

    @dataclass(frozen=True)
    class RevisionUpdateResult:
        state: Literal["updated", "update-failed", "refused"]
        model_id: str
        active_revision: str | None       # ревизия в current.json после операции
        message: str = ""
        reason_code: str = ""             # no-update · revoked · not-installed · no-previous ·
                                          # checksum · selfcheck · download · cancelled
"""

from __future__ import annotations

import pytest

pytest.importorskip("requests")

# ruff: noqa: E402
import hashlib
import importlib
import inspect
import json
import os
import random
import stat
import threading
from collections.abc import Callable, Iterator
from dataclasses import replace
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import Mock, call
from urllib.parse import urlsplit

import requests

from astra_voice.core.policy import Policy, PolicyStatus
from astra_voice.core.settings import Settings
from astra_voice.models.catalog import Catalog, CatalogEntry, FileSpec, RevokedEntry
from astra_voice.models.downloader import DownloadError
from astra_voice.models.installer import Installer, SmokeResult
from astra_voice.models.store import ModelStore, StoreError
from astra_voice.net import http
from astra_voice.net.gate import NetworkGate
from astra_voice.net.http import MODEL_CHECK_JITTER_MAX_S, CheckResult, HttpClient
from astra_voice.net.update_cache import UpdateCache
from helpers.http_fault_server import FaultServer

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "hf"
REPO = "istupakov/gigaam-v3-onnx"
REFS_PATH = f"/api/models/{REPO}/refs"
OLD = "322c3b29492673eb7d0b434bfa9dfb8653e34d02"
NEW = "99052dc2665168ae285e5ec3e697ea56137b069f"  # = main в refs-gigaam-v3-onnx.json
NEWER = "1d47386a9777a927aac611d869a7baed63609654"
DAY = 24 * 3600.0
T0 = 1_790_000_000.0


@pytest.fixture
def mu() -> ModuleType:
    """Модуль под тестом; пока его нет — ModuleNotFoundError (ожидаемый red)."""
    return importlib.import_module("astra_voice.updates.model_updates")


# -- данные ------------------------------------------------------------------


@pytest.fixture
def contents() -> dict[str, bytes]:
    # Настоящая раскладка e2e CTC (как в test_models_installer.py): проходит check_layout.
    return {
        "v3_e2e_ctc.int8.onnx": b"\x08\x03\x12\x04test",
        "v3_e2e_ctc_vocab.txt": "а\nб\n".encode(),
        "config.json": b'{"model_type":"gigaam"}',
    }


def make_entry(
    contents: dict[str, bytes],
    revision: str,
    *,
    model_id: str = "gigaam-v3-e2e-ctc-int8",
    name: str = "GigaAM v3 CTC",
    repo: str = REPO,
    host: str = "huggingface.co",
) -> CatalogEntry:
    return CatalogEntry(
        id=model_id,
        revision=revision,
        name=name,
        description="Крошечные файлы настоящей раскладки CTC.",
        size_bytes=sum(len(data) for data in contents.values()),
        min_ram_mb=1,
        layout="onnx-asr-gigaam-v3",
        variant="gigaam-v3-e2e-ctc",
        recommended=True,
        host=host,
        files=tuple(
            FileSpec(
                file_name,
                hashlib.sha256(data).hexdigest(),
                len(data),
                f"/{repo}/resolve/{revision}/{file_name}",
            )
            for file_name, data in contents.items()
        ),
    )


def make_catalog(*entries: CatalogEntry, revoked: tuple[RevokedEntry, ...] = ()) -> Catalog:
    return Catalog(serial=5, trust_epoch=1, revoked=revoked, entries=entries)


def install(store: ModelStore, entry: CatalogEntry, contents: dict[str, bytes]) -> None:
    staging = store.staging_dir(entry.id, entry.revision)
    for file_name, data in contents.items():
        (staging / file_name).write_bytes(data)
    result = Installer(store, lambda directory, model: SmokeResult(True)).install_from_staging(
        entry
    )
    assert result.state == "ok", result.reason


@pytest.fixture
def store(tmp_path: Path) -> ModelStore:
    result = ModelStore(tmp_path / "models")
    return result


@pytest.fixture
def old_entry(contents: dict[str, bytes]) -> CatalogEntry:
    return make_entry(contents, OLD)


@pytest.fixture
def new_entry(contents: dict[str, bytes]) -> CatalogEntry:
    return make_entry(contents, NEW)


@pytest.fixture
def installed(store: ModelStore, old_entry: CatalogEntry, contents: dict[str, bytes]) -> ModelStore:
    install(store, old_entry, contents)
    current = store.current()
    assert current is not None and current.revision == OLD
    return store


def tree_snapshot(root: Path) -> dict[str, tuple[int, str]]:
    """Все файлы хранилища: размер и sha256 — «ничего не скачано» проверяется побайтно."""
    result: dict[str, tuple[int, str]] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            data = path.read_bytes()
            result[str(path.relative_to(root))] = (len(data), hashlib.sha256(data).hexdigest())
    return result


# -- сеть: только локальный сервер --------------------------------------------


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in tuple(os.environ):
        if name.lower().endswith("_proxy"):
            monkeypatch.delenv(name)
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    monkeypatch.delenv("HF_ENDPOINT", raising=False)
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


def refs_body(main: str) -> bytes:
    return json.dumps(
        {
            "branches": [{"name": "main", "ref": "refs/heads/main", "targetCommit": main}],
            "converts": [],
            "tags": [],
        }
    ).encode("utf-8")


class Clock:
    def __init__(self, value: float = T0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


class Harness:
    """Собирает проверяльщик с локальным сервером, часами и журналом уведомлений."""

    def __init__(
        self,
        mu: ModuleType,
        server: FaultServer,
        tmp_path: Path,
        store: ModelStore,
        catalog: Catalog,
        *,
        settings: Settings | None = None,
        policy: Policy | None = None,
    ) -> None:
        self.mu = mu
        self.server = server
        self.tmp_path = tmp_path
        self.store = store
        self.catalog = catalog
        self.settings = settings if settings is not None else Settings(check_model_updates=True)
        self.policy = policy if policy is not None else Policy()
        self.clock = Clock()
        self.delays: list[float] = []
        self.notices: list[Any] = []
        self.state_path = tmp_path / "state" / "model-update-state.json"

    def checker(self) -> Any:
        client = HttpClient(NetworkGate(self.settings, self.policy), user_agent="astra-voice/test")

        def wait(delay: float) -> bool:
            self.delays.append(delay)
            return False

        return self.mu.ModelUpdateChecker(
            client,
            store=self.store,
            catalog=self.catalog,
            cache=UpdateCache(self.tmp_path / "update-cache.json"),
            state_path=self.state_path,
            api_base=self.server.url,
            clock=self.clock,
            rng=random.Random(7),
            wait=wait,
            notify=self.notices.append,
        )

    def run(self) -> Any:
        return self.checker().run_once(threading.Event())


# -- watch_target (Д3) -------------------------------------------------------


def test_watch_target_is_export_repo_main(mu: ModuleType, old_entry: CatalogEntry) -> None:
    target = mu.watch_target(old_entry)
    assert target == mu.WatchTarget(REPO, "main")


def test_watch_target_none_for_unwatched(
    mu: ModuleType, contents: dict[str, bytes], old_entry: CatalogEntry
) -> None:
    assert mu.watch_target(replace(old_entry, removed_from_catalog=True)) is None
    assert mu.watch_target(make_entry(contents, OLD, host="github.com")) is None
    mixed = replace(
        old_entry,
        files=(
            old_entry.files[0],
            replace(old_entry.files[1], url_path=f"/other/repo/resolve/{OLD}/x"),
            old_entry.files[2],
        ),
    )
    assert mu.watch_target(mixed) is None
    broken = replace(
        old_entry, files=tuple(replace(f, url_path="/models-2026.10/x") for f in old_entry.files)
    )
    assert mu.watch_target(broken) is None


# -- T-208/T-209: S8-A1 — бейдж, строка, одно уведомление, ничего не скачано ---


def test_new_revision_notifies_once_and_downloads_nothing(
    mu: ModuleType,
    server: FaultServer,
    tmp_path: Path,
    installed: ModelStore,
    old_entry: CatalogEntry,
    new_entry: CatalogEntry,
) -> None:
    server.bodies[REFS_PATH] = (FIXTURES / "refs-gigaam-v3-onnx.json").read_bytes()
    harness = Harness(mu, server, tmp_path, installed, make_catalog(new_entry))
    before = tree_snapshot(installed.root)

    summary = harness.run()

    assert summary.state == "checked"
    assert summary.requests_made == 1
    assert [path for path, _ in server.requests] == [REFS_PATH]
    assert len(summary.notices) == 1
    notice = summary.notices[0]
    assert (notice.model_id, notice.installed_revision, notice.remote_revision) == (
        old_entry.id,
        OLD,
        NEW,
    )
    assert mu.status_line(summary.notices) == "Обновление модели: GigaAM v3 CTC"
    assert harness.notices == [notice]
    # S8-A1: ничего не скачано — хранилище побайтно то же, рабочая ревизия прежняя.
    assert tree_snapshot(installed.root) == before
    current = installed.current()
    assert current is not None and current.revision == OLD

    # Через сутки та же ревизия на HF: запрос есть, второго уведомления нет.
    harness.clock.value += DAY + 1
    again = harness.run()
    assert again.state == "checked" and again.requests_made == 1
    assert [n.remote_revision for n in again.notices] == [NEW]
    assert len(harness.notices) == 1


def test_next_new_revision_notifies_again(
    mu: ModuleType,
    server: FaultServer,
    tmp_path: Path,
    installed: ModelStore,
    new_entry: CatalogEntry,
    contents: dict[str, bytes],
) -> None:
    server.bodies[REFS_PATH] = refs_body(NEW)
    harness = Harness(mu, server, tmp_path, installed, make_catalog(new_entry))
    # Один checker должен читать актуальный каталог на каждом run_once.
    checker = mu.ModelUpdateChecker(
        HttpClient(NetworkGate(harness.settings, harness.policy), user_agent="astra-voice/test"),
        store=installed,
        catalog=lambda: harness.catalog,
        cache=UpdateCache(tmp_path / "update-cache.json"),
        state_path=harness.state_path,
        api_base=server.url,
        clock=harness.clock,
        wait=lambda delay: False,
        notify=harness.notices.append,
    )
    checker.run_once(threading.Event())
    server.bodies[REFS_PATH] = refs_body(NEWER)
    harness.catalog = make_catalog(make_entry(contents, NEWER))
    harness.clock.value += DAY + 1
    checker.run_once(threading.Event())
    assert [n.remote_revision for n in harness.notices] == [NEW, NEWER]


def test_same_revision_no_notice(
    mu: ModuleType,
    server: FaultServer,
    tmp_path: Path,
    installed: ModelStore,
    old_entry: CatalogEntry,
) -> None:
    server.bodies[REFS_PATH] = refs_body(OLD)
    harness = Harness(mu, server, tmp_path, installed, make_catalog(old_entry))
    summary = harness.run()
    assert summary.state == "checked" and summary.notices == ()
    assert harness.notices == []
    assert mu.status_line(summary.notices) is None


def test_remote_revision_awaits_signed_catalog_without_notice(
    mu: ModuleType,
    server: FaultServer,
    tmp_path: Path,
    installed: ModelStore,
    old_entry: CatalogEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Сдвиг HF без той же ревизии в подписанном каталоге — только запись в журнале."""
    server.bodies[REFS_PATH] = refs_body(NEW)
    harness = Harness(mu, server, tmp_path, installed, make_catalog(old_entry))
    before = tree_snapshot(installed.root)

    with caplog.at_level("INFO", logger="astra_voice.updates.model_updates"):
        summary = harness.run()

    assert summary.state == "checked" and summary.requests_made == 1
    assert summary.notices == () and harness.notices == []
    assert mu.status_line(summary.notices) is None
    assert tree_snapshot(installed.root) == before
    assert "awaiting-catalog" in caplog.text and REPO in caplog.text
    assert OLD not in caplog.text and NEW not in caplog.text
    assert str(installed.root) not in caplog.text


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        json.dumps({"branches": [{"name": "main", "ref": "refs/heads/main"}]}).encode(),
        refs_body("short"),
    ],
    ids=["мусор", "без-targetCommit", "не-sha"],
)
def test_unknown_revision_is_silent(
    mu: ModuleType,
    server: FaultServer,
    tmp_path: Path,
    installed: ModelStore,
    old_entry: CatalogEntry,
    body: bytes,
) -> None:
    server.bodies[REFS_PATH] = body
    harness = Harness(mu, server, tmp_path, installed, make_catalog(old_entry))
    summary = harness.run()
    assert summary.notices == ()
    assert harness.notices == []


def test_one_request_per_repository(
    mu: ModuleType,
    server: FaultServer,
    tmp_path: Path,
    store: ModelStore,
    contents: dict[str, bytes],
) -> None:
    """В одном экспорт-репо проверяется только активный вариант, одним запросом."""
    first = make_entry(contents, OLD)
    second = make_entry(contents, OLD, model_id="gigaam-v3-e2e-ctc-int8-b", name="Вторая")
    install(store, second, contents)
    install(store, first, contents)
    server.bodies[REFS_PATH] = refs_body(NEW)
    harness = Harness(
        mu,
        server,
        tmp_path,
        store,
        make_catalog(
            make_entry(contents, NEW),
            make_entry(contents, NEW, model_id=second.id, name=second.name),
        ),
    )
    summary = harness.run()
    assert summary.requests_made == 1
    assert len(server.requests) == 1
    assert [n.model_id for n in summary.notices] == [first.id]


def test_removed_and_foreign_models_are_not_checked(
    mu: ModuleType,
    server: FaultServer,
    tmp_path: Path,
    store: ModelStore,
    contents: dict[str, bytes],
) -> None:
    """US-6.3: «снята с каталога — обновлений не будет»; чужой хост тоже не проверяем."""
    removed = make_entry(contents, OLD, model_id="removed-model")
    foreign = make_entry(contents, OLD, model_id="github-model", host="github.com")
    install(store, removed, contents)
    install(store, foreign, contents)
    server.bodies[REFS_PATH] = refs_body(NEW)
    # removed отсутствует в каталоге, foreign — не с HF.
    harness = Harness(mu, server, tmp_path, store, make_catalog(foreign))
    summary = harness.run()
    assert summary.notices == ()
    assert server.requests == []


def test_broken_revision_is_not_checked(
    mu: ModuleType,
    server: FaultServer,
    tmp_path: Path,
    installed: ModelStore,
    old_entry: CatalogEntry,
) -> None:
    installed.mark_broken(old_entry.id, OLD, "проверка")
    server.bodies[REFS_PATH] = refs_body(NEW)
    harness = Harness(mu, server, tmp_path, installed, make_catalog(old_entry))
    assert harness.run().notices == ()
    assert server.requests == []


# -- T-207: S8-A6 — не чаще раза в сутки, переживает перезапуск; задержка ≤ 30 мин


def test_not_due_within_24h_even_after_restart(
    mu: ModuleType,
    server: FaultServer,
    tmp_path: Path,
    installed: ModelStore,
    old_entry: CatalogEntry,
) -> None:
    server.bodies[REFS_PATH] = refs_body(OLD)
    harness = Harness(mu, server, tmp_path, installed, make_catalog(old_entry))
    assert harness.run().state == "checked"
    assert len(server.requests) == 1

    harness.clock.value += DAY - 60
    # Новый экземпляр = повторный запуск программы с тем же файлом состояния.
    summary = harness.run()
    assert summary.state == "not-due"
    assert summary.requests_made == 0
    assert len(server.requests) == 1

    harness.clock.value += 120
    assert harness.run().state == "checked"
    assert len(server.requests) == 2


def test_state_file_is_private(
    mu: ModuleType,
    server: FaultServer,
    tmp_path: Path,
    installed: ModelStore,
    old_entry: CatalogEntry,
) -> None:
    server.bodies[REFS_PATH] = refs_body(OLD)
    harness = Harness(mu, server, tmp_path, installed, make_catalog(old_entry))
    harness.run()
    assert harness.state_path.is_file()
    assert stat.S_IMODE(harness.state_path.stat().st_mode) == 0o600


@pytest.mark.parametrize(
    "payload",
    [b"", b"{", b"[]", '{"last_attempt_at": "вчера"}'.encode(), b'{"last_attempt_at": 1e309}'],
)
def test_broken_state_file_does_not_block_checks(
    mu: ModuleType,
    server: FaultServer,
    tmp_path: Path,
    installed: ModelStore,
    old_entry: CatalogEntry,
    payload: bytes,
) -> None:
    server.bodies[REFS_PATH] = refs_body(OLD)
    harness = Harness(mu, server, tmp_path, installed, make_catalog(old_entry))
    harness.state_path.parent.mkdir(parents=True, exist_ok=True)
    harness.state_path.write_bytes(payload)
    assert harness.run().state == "checked"


def test_future_attempt_time_does_not_freeze_checks(
    mu: ModuleType,
    server: FaultServer,
    tmp_path: Path,
    installed: ModelStore,
    old_entry: CatalogEntry,
) -> None:
    """Часы ушли назад (или файл подложен): «попытка в будущем» не глушит проверки навсегда."""
    server.bodies[REFS_PATH] = refs_body(OLD)
    harness = Harness(mu, server, tmp_path, installed, make_catalog(old_entry))
    harness.clock.value = T0 + 365 * DAY
    harness.run()
    harness.clock.value = T0
    harness.clock.value += DAY + 1
    assert harness.run().state == "checked"


def test_jitter_is_within_30_minutes(
    mu: ModuleType,
    server: FaultServer,
    tmp_path: Path,
    installed: ModelStore,
    old_entry: CatalogEntry,
) -> None:
    server.bodies[REFS_PATH] = refs_body(OLD)
    harness = Harness(mu, server, tmp_path, installed, make_catalog(old_entry))
    harness.run()
    assert harness.delays, "проверка по расписанию обязана выдержать случайную задержку"
    assert all(0.0 <= delay <= 1800.0 for delay in harness.delays)
    default = inspect.signature(mu.ModelUpdateChecker).parameters["jitter_max_s"].default
    assert default == MODEL_CHECK_JITTER_MAX_S == 1800.0


def test_manual_check_skips_jitter(
    mu: ModuleType,
    server: FaultServer,
    tmp_path: Path,
    installed: ModelStore,
    old_entry: CatalogEntry,
) -> None:
    server.bodies[REFS_PATH] = refs_body(OLD)
    harness = Harness(mu, server, tmp_path, installed, make_catalog(old_entry))
    summary = harness.checker().run_once(threading.Event(), manual=True)
    assert summary.state == "checked" and summary.requests_made == 1
    assert harness.delays == []


# -- T-205: гейт закрыт → ноль запросов к HF, попытка не засчитана --------------

CLOSED = [
    pytest.param(Settings(check_model_updates=False), Policy(), id="тумблер-выключен"),
    pytest.param(Settings(check_model_updates=True, offline=True), Policy(), id="офлайн"),
    pytest.param(
        Settings(check_model_updates=True),
        Policy(
            values={"offline": True}, locked_keys=frozenset({"offline"}), status=PolicyStatus.OK
        ),
        id="policy-offline",
    ),
    pytest.param(
        Settings(check_model_updates=True),
        Policy(
            values={"check_model_updates": False},
            locked_keys=frozenset({"check_model_updates"}),
            status=PolicyStatus.OK,
        ),
        id="policy-check_model_updates-false",
    ),
    pytest.param(
        Settings(check_model_updates=True),
        Policy(values={"updates": "offline"}, status=PolicyStatus.OK),
        id="policy-updates-offline",
    ),
    pytest.param(
        Settings(check_model_updates=True),
        Policy(status=PolicyStatus.INVALID),
        id="policy-невалидна",
    ),
]


@pytest.mark.parametrize(("settings", "policy"), CLOSED)
def test_closed_gate_zero_requests(
    mu: ModuleType,
    server: FaultServer,
    tmp_path: Path,
    installed: ModelStore,
    old_entry: CatalogEntry,
    settings: Settings,
    policy: Policy,
) -> None:
    server.bodies[REFS_PATH] = refs_body(NEW)
    harness = Harness(
        mu, server, tmp_path, installed, make_catalog(old_entry), settings=settings, policy=policy
    )
    summary = harness.run()
    assert summary.state == "disabled"
    assert summary.requests_made == 0 and summary.notices == ()
    assert server.requests == []
    assert harness.notices == []
    # Отказ гейта — не попытка: включили тумблер — проверка сразу по расписанию.
    harness.settings = Settings(check_model_updates=True)
    harness.policy = Policy()
    assert harness.run().state == "checked"
    assert len(server.requests) == 1


def test_hf_hub_offline_zero_requests(
    mu: ModuleType,
    server: FaultServer,
    tmp_path: Path,
    installed: ModelStore,
    old_entry: CatalogEntry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    server.bodies[REFS_PATH] = refs_body(NEW)
    harness = Harness(mu, server, tmp_path, installed, make_catalog(old_entry))
    assert harness.run().state == "disabled"
    assert server.requests == []


def test_default_api_base_ignores_hf_endpoint(
    mu: ModuleType,
    tmp_path: Path,
    installed: ModelStore,
    old_entry: CatalogEntry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HF_ENDPOINT", "https://evil.example")
    urls: list[str] = []

    def spy(self: HttpClient, url: str, **kwargs: Any) -> CheckResult:
        urls.append(url)
        assert kwargs["kind"] == "check_models"
        return CheckResult("unavailable")

    monkeypatch.setattr(HttpClient, "get_check", spy)
    checker = mu.ModelUpdateChecker(
        HttpClient(NetworkGate(Settings(check_model_updates=True), Policy()), user_agent="t"),
        store=installed,
        catalog=make_catalog(old_entry),
        cache=UpdateCache(tmp_path / "update-cache.json"),
        state_path=tmp_path / "model-update-state.json",
        wait=lambda delay: False,
    )
    checker.run_once(threading.Event())
    assert urls == [f"https://huggingface.co/api/models/{REPO}/refs"]


# -- T-206: S8-A7 — 429 → тишина ---------------------------------------------


def test_rate_limited_is_silent(
    mu: ModuleType,
    server: FaultServer,
    tmp_path: Path,
    installed: ModelStore,
    old_entry: CatalogEntry,
) -> None:
    server.statuses[REFS_PATH] = 429
    harness = Harness(mu, server, tmp_path, installed, make_catalog(old_entry))
    summary = harness.run()
    assert summary.state == "rate-limited"
    assert summary.retry_at is not None and summary.retry_at > harness.clock.value
    assert summary.notices == () and harness.notices == []
    assert mu.status_line(summary.notices) is None


# -- T-210…T-215: «Обновить» — staging → sha256 → смоук → current.json; откат ---


class FakeDownloader:
    """Кладёт файлы новой ревизии в staging так же, как настоящий загрузчик."""

    def __init__(
        self,
        store: ModelStore,
        contents: dict[str, bytes],
        *,
        corrupt: str | None = None,
        error: Exception | None = None,
    ) -> None:
        self.store = store
        self.contents = contents
        self.corrupt = corrupt
        self.error = error
        self.calls: list[str] = []

    def download(
        self,
        entry: CatalogEntry,
        *,
        progress: Callable[..., None],
        cancel: threading.Event,
        source: Callable[[str], None] | None = None,
    ) -> Path:
        self.calls.append(entry.revision)
        if self.error is not None:
            raise self.error
        staging = self.store.staging_dir(entry.id, entry.revision)
        for file_name, data in self.contents.items():
            if file_name == self.corrupt:
                data = bytes([data[0] ^ 1]) + data[1:]
            (staging / file_name).write_bytes(data)
        return staging


def smoke_for(*, fail_revision: str | None = None) -> Mock:
    def check(directory: Path, entry: CatalogEntry) -> SmokeResult:
        if entry.revision == fail_revision:
            return SmokeResult(False, reason="Модель не прошла пробное распознавание.")
        return SmokeResult(True, text="проверка связи")

    return Mock(side_effect=check)


def updater(
    mu: ModuleType,
    store: ModelStore,
    downloader: FakeDownloader,
    catalog: Catalog | Callable[[], Catalog],
    *,
    smoke: Mock | None = None,
    states: list[tuple[str, str]] | None = None,
) -> Any:
    installer = Installer(store, smoke if smoke is not None else smoke_for(), catalog)
    return mu.ModelRevisionUpdater(
        store,
        downloader,
        installer,
        catalog,
        on_state=(lambda model_id, state: states.append((model_id, state)))
        if states is not None
        else None,
    )


def test_apply_installs_signed_revision(
    mu: ModuleType,
    installed: ModelStore,
    new_entry: CatalogEntry,
    contents: dict[str, bytes],
) -> None:
    """T-210 / S8-A2 (успех): активна новая ревизия, старая остаётся для отката."""
    downloader = FakeDownloader(installed, contents)
    states: list[tuple[str, str]] = []
    smoke = smoke_for()
    result = updater(
        mu, installed, downloader, make_catalog(new_entry), smoke=smoke, states=states
    ).apply(new_entry.id, cancel=threading.Event())
    assert result.state == "updated"
    assert result.active_revision == NEW
    current = installed.current()
    assert current is not None and current.revision == NEW
    assert downloader.calls == [NEW]
    assert smoke.call_count == 1
    assert {record.revision for record in installed.records()} == {OLD, NEW}
    assert states[0] == (new_entry.id, "updating")


def test_checksum_mismatch_keeps_current(
    mu: ModuleType,
    installed: ModelStore,
    new_entry: CatalogEntry,
    contents: dict[str, bytes],
) -> None:
    """T-211: sha256 не совпал → «оставлена текущая», смоук не запускался."""
    downloader = FakeDownloader(installed, contents, corrupt="v3_e2e_ctc.int8.onnx")
    smoke = smoke_for()
    states: list[tuple[str, str]] = []
    result = updater(
        mu, installed, downloader, make_catalog(new_entry), smoke=smoke, states=states
    ).apply(new_entry.id, cancel=threading.Event())
    assert result.state == "update-failed"
    assert result.message == mu.FAILED_MESSAGE
    assert result.message == "Новая версия модели не прошла проверку, оставлена текущая"
    assert result.reason_code == "checksum"
    assert result.active_revision == OLD
    current = installed.current()
    assert current is not None and current.revision == OLD
    smoke.assert_not_called()
    assert states[-1] == (new_entry.id, "update-failed")


def test_smoke_failure_rolls_back(
    mu: ModuleType,
    installed: ModelStore,
    new_entry: CatalogEntry,
    contents: dict[str, bytes],
) -> None:
    """T-212 / S8-A2 (провал) / N-13: смоук упал → текущая работает, новая помечена broken."""
    downloader = FakeDownloader(installed, contents)
    states: list[tuple[str, str]] = []
    result = updater(
        mu,
        installed,
        downloader,
        make_catalog(new_entry),
        smoke=smoke_for(fail_revision=NEW),
        states=states,
    ).apply(new_entry.id, cancel=threading.Event())
    assert result.state == "update-failed"
    assert result.message == mu.FAILED_MESSAGE
    assert result.reason_code == "selfcheck"
    assert result.active_revision == OLD
    current = installed.current()
    assert current is not None and current.revision == OLD
    broken = {r.revision: r.state for r in installed.records()}
    assert broken[NEW] == "broken" and broken[OLD] == "ok"
    assert states[-1] == (new_entry.id, "update-failed")


@pytest.mark.parametrize(
    ("error", "reason_code"),
    [
        (DownloadError("too-large"), "download"),
        (DownloadError("bad-checksum"), "checksum"),
        (DownloadError("host-unreachable"), "download"),
        (DownloadError("disk-full"), "download"),
    ],
    ids=["обрыв-на-size+1", "sha256-у-загрузчика", "сеть", "нет-места"],
)
def test_download_failure_keeps_current(
    mu: ModuleType,
    installed: ModelStore,
    new_entry: CatalogEntry,
    contents: dict[str, bytes],
    error: DownloadError,
    reason_code: str,
) -> None:
    """T-213: загрузка новой ревизии прервана (в т. ч. на size+1) → текущая не тронута."""
    smoke = smoke_for()
    result = updater(
        mu,
        installed,
        FakeDownloader(installed, contents, error=error),
        make_catalog(new_entry),
        smoke=smoke,
    ).apply(new_entry.id, cancel=threading.Event())
    assert result.state == "update-failed"
    assert result.reason_code == reason_code
    assert result.active_revision == OLD
    current = installed.current()
    assert current is not None and current.revision == OLD
    assert NEW not in {record.revision for record in installed.records()}
    smoke.assert_not_called()


def test_cancel_keeps_current(
    mu: ModuleType,
    installed: ModelStore,
    new_entry: CatalogEntry,
    contents: dict[str, bytes],
) -> None:
    cancel = threading.Event()
    cancel.set()
    downloader = FakeDownloader(installed, contents, error=DownloadError("cancelled"))
    result = updater(mu, installed, downloader, make_catalog(new_entry)).apply(
        new_entry.id, cancel=cancel
    )
    assert result.state in ("update-failed", "refused")
    assert result.reason_code == "cancelled"
    current = installed.current()
    assert current is not None and current.revision == OLD


def test_no_signed_new_revision_is_refused(
    mu: ModuleType,
    installed: ModelStore,
    old_entry: CatalogEntry,
    contents: dict[str, bytes],
) -> None:
    """T-214 / Д3: HF сдвинулся, но в подписанном каталоге та же ревизия — ставить нечего."""
    downloader = FakeDownloader(installed, contents)
    result = updater(mu, installed, downloader, make_catalog(old_entry)).apply(
        old_entry.id, cancel=threading.Event()
    )
    assert result.state == "refused" and result.reason_code == "no-update"
    assert downloader.calls == []
    current = installed.current()
    assert current is not None and current.revision == OLD


def test_revoked_target_is_refused_before_download(
    mu: ModuleType,
    installed: ModelStore,
    new_entry: CatalogEntry,
    contents: dict[str, bytes],
) -> None:
    downloader = FakeDownloader(installed, contents)
    catalog = make_catalog(new_entry, revoked=(RevokedEntry(new_entry.id, NEW, "уязвимость"),))
    result = updater(mu, installed, downloader, catalog).apply(
        new_entry.id, cancel=threading.Event()
    )
    assert result.state == "refused" and result.reason_code == "revoked"
    assert downloader.calls == []
    current = installed.current()
    assert current is not None and current.revision == OLD


def test_not_installed_model_is_refused(
    mu: ModuleType, store: ModelStore, new_entry: CatalogEntry, contents: dict[str, bytes]
) -> None:
    downloader = FakeDownloader(store, contents)
    result = updater(mu, store, downloader, make_catalog(new_entry)).apply(
        new_entry.id, cancel=threading.Event()
    )
    assert result.state == "refused" and result.reason_code == "not-installed"
    assert downloader.calls == []


def test_unknown_model_is_refused(
    mu: ModuleType, installed: ModelStore, new_entry: CatalogEntry, contents: dict[str, bytes]
) -> None:
    downloader = FakeDownloader(installed, contents)
    result = updater(mu, installed, downloader, make_catalog(new_entry)).apply(
        "../evil", cancel=threading.Event()
    )
    assert result.state == "refused"
    assert downloader.calls == []


# -- T-215: «Вернуть предыдущую» (P2) и revoked[] ------------------------------


def updated_store(
    mu: ModuleType, installed: ModelStore, new_entry: CatalogEntry, contents: dict[str, bytes]
) -> ModelStore:
    result = updater(
        mu, installed, FakeDownloader(installed, contents), make_catalog(new_entry)
    ).apply(new_entry.id, cancel=threading.Event())
    assert result.state == "updated"
    return installed


def test_revert_previous(
    mu: ModuleType, installed: ModelStore, new_entry: CatalogEntry, contents: dict[str, bytes]
) -> None:
    store = updated_store(mu, installed, new_entry, contents)
    result = updater(
        mu, store, FakeDownloader(store, contents), make_catalog(new_entry)
    ).revert_previous(new_entry.id)
    assert result.state == "updated" and result.active_revision == OLD
    current = store.current()
    assert current is not None and current.revision == OLD


def test_revert_to_revoked_revision_is_refused(
    mu: ModuleType, installed: ModelStore, new_entry: CatalogEntry, contents: dict[str, bytes]
) -> None:
    """US-6.6: на отозванную ревизию нельзя вернуться «Вернуть предыдущую»."""
    store = updated_store(mu, installed, new_entry, contents)
    catalog = make_catalog(new_entry, revoked=(RevokedEntry(new_entry.id, OLD, "уязвимость"),))
    result = updater(mu, store, FakeDownloader(store, contents), catalog).revert_previous(
        new_entry.id
    )
    assert result.state == "refused" and result.reason_code == "revoked"
    current = store.current()
    assert current is not None and current.revision == NEW


def test_revert_without_previous_is_refused(
    mu: ModuleType, installed: ModelStore, old_entry: CatalogEntry, contents: dict[str, bytes]
) -> None:
    result = updater(
        mu, installed, FakeDownloader(installed, contents), make_catalog(old_entry)
    ).revert_previous(old_entry.id)
    assert result.state == "refused" and result.reason_code == "no-previous"
    current = installed.current()
    assert current is not None and current.revision == OLD


def test_revert_skips_broken_previous(
    mu: ModuleType, installed: ModelStore, new_entry: CatalogEntry, contents: dict[str, bytes]
) -> None:
    store = updated_store(mu, installed, new_entry, contents)
    store.mark_broken(new_entry.id, OLD, "проверка")
    result = updater(
        mu, store, FakeDownloader(store, contents), make_catalog(new_entry)
    ).revert_previous(new_entry.id)
    assert result.state == "refused" and result.reason_code == "no-previous"
    current = store.current()
    assert current is not None and current.revision == NEW


# -- M7, шаг 3: явный указатель предыдущей ревизии в current.json -------------


def test_apply_saves_previous_revision(
    mu: ModuleType, installed: ModelStore, new_entry: CatalogEntry, contents: dict[str, bytes]
) -> None:
    store = updated_store(mu, installed, new_entry, contents)
    pointer = json.loads((store.root / "current.json").read_text())
    assert pointer == {"id": new_entry.id, "revision": NEW, "previous_revision": OLD}
    assert store.previous_revision() == OLD


def test_revert_uses_explicit_previous_among_three_revisions(
    mu: ModuleType, installed: ModelStore, new_entry: CatalogEntry, contents: dict[str, bytes]
) -> None:
    install(installed, new_entry, contents)
    install(installed, make_entry(contents, NEWER), contents)
    installed.set_current(new_entry.id, NEWER, previous_revision=OLD)
    assert {record.revision for record in installed.records() if record.state == "ok"} == {
        OLD,
        NEW,
        NEWER,
    }
    assert max(OLD, NEW) == NEW
    result = updater(
        mu, installed, FakeDownloader(installed, contents), make_catalog(new_entry)
    ).revert_previous(new_entry.id)
    assert result.state == "updated" and result.active_revision == OLD
    current = installed.current()
    assert current is not None and current.revision == OLD


def test_revert_legacy_pointer_does_not_infer_previous(
    mu: ModuleType, installed: ModelStore, new_entry: CatalogEntry, contents: dict[str, bytes]
) -> None:
    install(installed, new_entry, contents)
    pointer = installed.root / "current.json"
    assert json.loads(pointer.read_text()) == {"id": new_entry.id, "revision": NEW}
    assert installed.previous_revision() is None
    before = pointer.read_bytes()
    result = updater(
        mu, installed, FakeDownloader(installed, contents), make_catalog(new_entry)
    ).revert_previous(new_entry.id)
    assert result.state == "refused" and result.reason_code == "no-previous"
    assert result.active_revision == NEW
    assert pointer.read_bytes() == before


@pytest.mark.parametrize("previous", ["../x", 42, "not-installed"])
def test_invalid_previous_keeps_current_and_refuses_revert(
    mu: ModuleType,
    installed: ModelStore,
    new_entry: CatalogEntry,
    contents: dict[str, bytes],
    previous: object,
) -> None:
    store = updated_store(mu, installed, new_entry, contents)
    pointer = store.root / "current.json"
    data = json.loads(pointer.read_text())
    data["previous_revision"] = previous
    pointer.write_text(json.dumps(data))
    before = pointer.read_bytes()
    assert store.previous_revision() is None
    current = store.current()
    assert current is not None and current.revision == NEW
    result = updater(
        mu, store, FakeDownloader(store, contents), make_catalog(new_entry)
    ).revert_previous(new_entry.id)
    assert result.state == "refused" and result.reason_code == "no-previous"
    assert result.active_revision == NEW
    assert pointer.read_bytes() == before
    assert not list(store.root.glob("current.json.bak-*"))


def test_revert_removes_previous_revision_pointer(
    mu: ModuleType, installed: ModelStore, new_entry: CatalogEntry, contents: dict[str, bytes]
) -> None:
    store = updated_store(mu, installed, new_entry, contents)
    revision_updater = updater(mu, store, FakeDownloader(store, contents), make_catalog(new_entry))
    assert store.previous_revision() == OLD
    result = revision_updater.revert_previous(new_entry.id)
    assert result.state == "updated" and result.active_revision == OLD
    assert json.loads((store.root / "current.json").read_text()) == {
        "id": new_entry.id,
        "revision": OLD,
    }
    assert store.previous_revision() is None
    again = revision_updater.revert_previous(new_entry.id)
    assert again.state == "refused" and again.reason_code == "no-previous"
    assert again.active_revision == OLD


def test_ordinary_set_current_clears_previous_revision(
    mu: ModuleType, installed: ModelStore, new_entry: CatalogEntry, contents: dict[str, bytes]
) -> None:
    store = updated_store(mu, installed, new_entry, contents)
    assert store.previous_revision() == OLD
    store.set_current(new_entry.id, NEW)
    assert store.previous_revision() is None
    assert json.loads((store.root / "current.json").read_text()) == {
        "id": new_entry.id,
        "revision": NEW,
    }
    current = store.current()
    assert current is not None and current.revision == NEW


def test_revert_set_current_failure_reports_revert_message(
    mu: ModuleType,
    installed: ModelStore,
    new_entry: CatalogEntry,
    contents: dict[str, bytes],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = updated_store(mu, installed, new_entry, contents)
    before = (store.root / "current.json").read_bytes()
    set_current = Mock(side_effect=StoreError("broken-store"))
    monkeypatch.setattr(store, "set_current", set_current)
    states: list[tuple[str, str]] = []
    result = updater(
        mu, store, FakeDownloader(store, contents), make_catalog(new_entry), states=states
    ).revert_previous(new_entry.id)
    assert result.state == "update-failed" and result.reason_code == "revert"
    assert result.message == mu.REVERT_FAILED_MESSAGE
    assert result.message == "Не удалось вернуть предыдущую версию модели"
    assert result.message != mu.FAILED_MESSAGE
    assert result.active_revision == NEW
    assert set_current.call_args_list == [
        call(new_entry.id, OLD),
        call(new_entry.id, NEW, previous_revision=OLD),
    ]
    assert states == [(new_entry.id, "activating"), (new_entry.id, "update-failed")]
    assert (store.root / "current.json").read_bytes() == before
    current = store.current()
    assert current is not None and current.revision == NEW


@pytest.mark.parametrize("write_before_error", [False, True])
def test_apply_pointer_failure_restores_current_and_previous(
    mu: ModuleType,
    installed: ModelStore,
    new_entry: CatalogEntry,
    contents: dict[str, bytes],
    monkeypatch: pytest.MonkeyPatch,
    write_before_error: bool,
) -> None:
    install(installed, make_entry(contents, NEWER), contents)
    installed.set_current(new_entry.id, OLD, previous_revision=NEWER)
    before = (installed.root / "current.json").read_bytes()
    original_set_current = installed.set_current

    def fail_activation(
        model_id: str, revision: str, *, previous_revision: str | None = None
    ) -> None:
        if revision == NEW:
            if write_before_error:
                original_set_current(model_id, revision, previous_revision=previous_revision)
            raise StoreError("disk-full")
        original_set_current(model_id, revision, previous_revision=previous_revision)

    set_current = Mock(side_effect=fail_activation)
    monkeypatch.setattr(installed, "set_current", set_current)
    states: list[tuple[str, str]] = []
    result = updater(
        mu, installed, FakeDownloader(installed, contents), make_catalog(new_entry), states=states
    ).apply(new_entry.id, cancel=threading.Event())
    assert result.state == "update-failed" and result.active_revision == OLD
    assert result.reason_code == "activation"
    assert result.message == mu.FAILED_MESSAGE
    assert (installed.root / "current.json").read_bytes() == before
    assert installed.previous_revision() == NEWER
    assert set_current.call_args_list == [
        call(new_entry.id, NEW, previous_revision=OLD),
        call(new_entry.id, OLD, previous_revision=NEWER),
    ]
    assert states == [
        (new_entry.id, "updating"),
        (new_entry.id, "activating"),
        (new_entry.id, "update-failed"),
    ]


@pytest.mark.parametrize("problem", ["recheck", "missing-metadata", "layout", "variant"])
def test_revert_refuses_unusable_previous_record(
    mu: ModuleType,
    installed: ModelStore,
    new_entry: CatalogEntry,
    contents: dict[str, bytes],
    problem: str,
) -> None:
    store = updated_store(mu, installed, new_entry, contents)
    state_path = store.root / new_entry.id / f"{OLD}.json"
    if problem == "missing-metadata":
        state_path.unlink()
    else:
        data = json.loads(state_path.read_text())
        data[problem] = True if problem == "recheck" else ""
        state_path.write_text(json.dumps(data))
    assert store.previous_revision() == OLD
    before = (store.root / "current.json").read_bytes()
    result = updater(
        mu, store, FakeDownloader(store, contents), make_catalog(new_entry)
    ).revert_previous(new_entry.id)
    assert result.state == "refused" and result.reason_code == "no-previous"
    assert result.active_revision == NEW
    assert (store.root / "current.json").read_bytes() == before


@pytest.mark.parametrize("installer_provider", [False, True])
def test_apply_revocation_during_delayed_download_keeps_current(
    mu: ModuleType,
    installed: ModelStore,
    new_entry: CatalogEntry,
    contents: dict[str, bytes],
    installer_provider: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = threading.Event()
    resume = threading.Event()
    downloader = FakeDownloader(installed, contents)
    download = downloader.download
    catalog = make_catalog(new_entry)
    original_catalog = catalog
    before = (installed.root / "current.json").read_bytes()

    def delayed_download(*args: Any, **kwargs: Any) -> Path:
        started.set()
        assert resume.wait(5), "Тест не разрешил продолжить загрузку"
        return download(*args, **kwargs)

    monkeypatch.setattr(downloader, "download", Mock(side_effect=delayed_download))
    provider = lambda: catalog  # noqa: E731
    installer = Installer(
        installed, smoke_for(), provider if installer_provider else original_catalog
    )
    revision_updater = mu.ModelRevisionUpdater(installed, downloader, installer, provider)
    results: list[Any] = []
    worker = threading.Thread(
        target=lambda: results.append(
            revision_updater.apply(new_entry.id, cancel=threading.Event())
        )
    )
    worker.start()
    try:
        assert started.wait(5), "Загрузка не началась"
        catalog = make_catalog(new_entry, revoked=(RevokedEntry(new_entry.id, NEW, "отзыв"),))
    finally:
        resume.set()
        worker.join(5)
    assert not worker.is_alive()
    assert len(results) == 1
    assert results[0].state == "update-failed"
    assert results[0].reason_code == "revoked"
    assert results[0].active_revision == OLD
    assert (installed.root / "current.json").read_bytes() == before


@pytest.mark.parametrize("cancel_after_installer", [False, True])
def test_apply_cancel_after_smoke_keeps_current(
    mu: ModuleType,
    installed: ModelStore,
    new_entry: CatalogEntry,
    contents: dict[str, bytes],
    monkeypatch: pytest.MonkeyPatch,
    cancel_after_installer: bool,
) -> None:
    cancel = threading.Event()
    before = (installed.root / "current.json").read_bytes()

    def smoke(directory: Path, entry: CatalogEntry) -> SmokeResult:
        if not cancel_after_installer:
            cancel.set()
        return SmokeResult(True)

    installer = Installer(installed, smoke)
    install_staged = installer.install_from_staging

    def install_then_cancel(*args: Any, **kwargs: Any) -> Any:
        result = install_staged(*args, **kwargs)
        cancel.set()
        return result

    monkeypatch.setattr(installer, "install_from_staging", install_then_cancel)
    states: list[tuple[str, str]] = []
    result = mu.ModelRevisionUpdater(
        installed,
        FakeDownloader(installed, contents),
        installer,
        make_catalog(new_entry),
        on_state=lambda model_id, state: states.append((model_id, state)),
    ).apply(new_entry.id, cancel=cancel)
    assert result.state == "update-failed" and result.reason_code == "cancelled"
    assert result.active_revision == OLD
    assert (installed.root / "current.json").read_bytes() == before
    assert states == [(new_entry.id, "updating"), (new_entry.id, "update-failed")]


def test_apply_activates_once_with_previous_revision(
    mu: ModuleType,
    installed: ModelStore,
    new_entry: CatalogEntry,
    contents: dict[str, bytes],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    states: list[tuple[str, str]] = []
    cancel = threading.Event()
    original_set_current = installed.set_current

    def save(model_id: str, revision: str, *, previous_revision: str | None = None) -> None:
        assert states[-1] == (model_id, "activating")
        current = installed.current()
        assert current is not None and current.revision == OLD
        original_set_current(model_id, revision, previous_revision=previous_revision)

    set_current = Mock(side_effect=save)
    monkeypatch.setattr(installed, "set_current", set_current)

    def state_changed(model_id: str, state: str) -> None:
        states.append((model_id, state))
        if state == "activating":
            cancel.set()  # После границы активации отмена уже не принимается.

    result = mu.ModelRevisionUpdater(
        installed,
        FakeDownloader(installed, contents),
        Installer(installed, smoke_for()),
        make_catalog(new_entry),
        on_state=state_changed,
    ).apply(new_entry.id, cancel=cancel)
    assert result.state == "updated"
    set_current.assert_called_once_with(new_entry.id, NEW, previous_revision=OLD)
    assert installed.previous_revision() == OLD


@pytest.mark.parametrize("revert", [False, True])
def test_latest_revocation_is_checked_after_activating_notification(
    mu: ModuleType,
    installed: ModelStore,
    new_entry: CatalogEntry,
    contents: dict[str, bytes],
    revert: bool,
) -> None:
    if revert:
        updated_store(mu, installed, new_entry, contents)
    before = (installed.root / "current.json").read_bytes()
    catalog = make_catalog(new_entry)
    target = OLD if revert else NEW

    def state_changed(model_id: str, state: str) -> None:
        nonlocal catalog
        if state == "activating":
            catalog = make_catalog(new_entry, revoked=(RevokedEntry(model_id, target, "отзыв"),))

    revision_updater = mu.ModelRevisionUpdater(
        installed,
        FakeDownloader(installed, contents),
        Installer(installed, smoke_for()),
        lambda: catalog,
        on_state=state_changed,
    )
    result = (
        revision_updater.revert_previous(new_entry.id)
        if revert
        else revision_updater.apply(new_entry.id, cancel=threading.Event())
    )
    assert result.reason_code == "revoked"
    assert result.state == ("refused" if revert else "update-failed")
    assert (installed.root / "current.json").read_bytes() == before


def test_revert_write_then_raise_restores_previous_pointer(
    mu: ModuleType,
    installed: ModelStore,
    new_entry: CatalogEntry,
    contents: dict[str, bytes],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    updated_store(mu, installed, new_entry, contents)
    before = (installed.root / "current.json").read_bytes()
    original_set_current = installed.set_current

    def save(model_id: str, revision: str, *, previous_revision: str | None = None) -> None:
        original_set_current(model_id, revision, previous_revision=previous_revision)
        if revision == OLD:
            raise StoreError("broken-store")

    monkeypatch.setattr(installed, "set_current", save)
    result = updater(
        mu, installed, FakeDownloader(installed, contents), make_catalog(new_entry)
    ).revert_previous(new_entry.id)
    assert result.state == "update-failed" and result.reason_code == "revert"
    assert result.active_revision == NEW
    assert (installed.root / "current.json").read_bytes() == before


def test_live_service_accepts_revocation_during_revision_download(
    installed: ModelStore,
    new_entry: CatalogEntry,
    contents: dict[str, bytes],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from PyQt5.QtCore import QCoreApplication, QEvent

    from astra_voice.ui.model_downloads import ModelDownloads, ModelService
    from helpers.qt_app import get_qapplication
    from helpers.scripted_model_port import pump_until

    app = get_qapplication()
    assert app is not None
    started = threading.Event()
    resume = threading.Event()
    service = ModelService.__new__(ModelService)
    service._catalog = make_catalog(new_entry)
    service._catalog_error = None
    service._store = installed
    service._gate = NetworkGate(Settings(), Policy())
    service._cancel = threading.Event()
    downloader = FakeDownloader(installed, contents)
    download = downloader.download

    def delayed_download(*args: Any, **kwargs: Any) -> Path:
        started.set()
        assert resume.wait(5), "Тест не разрешил продолжить загрузку"
        return download(*args, **kwargs)

    monkeypatch.setattr(downloader, "download", Mock(side_effect=delayed_download))
    service._downloader = downloader  # type: ignore[assignment]
    service._installer = Installer(installed, smoke_for(), lambda: service.catalog)
    downloads = ModelDownloads(service, store=installed)
    before = (installed.root / "current.json").read_bytes()
    try:
        downloads.updateModel(new_entry.id)
        assert started.wait(5), "Обновление не начало загрузку"
        revoked = make_catalog(new_entry, revoked=(RevokedEntry(new_entry.id, NEW, "отзыв"),))
        downloads.accept_catalog(revoked, frozenset())
        assert service.catalog is revoked
        assert downloads._pending_catalog is not None
        resume.set()
        pump_until(lambda: downloads._model_thread is None, "завершение отозванного обновления")
        assert (installed.root / "current.json").read_bytes() == before
        current = installed.current()
        assert current is not None and current.revision == OLD
        assert downloads._pending_catalog is None
    finally:
        resume.set()
        downloads.shutdown()
        QCoreApplication.sendPostedEvents(None, QEvent.MetaCall)
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
