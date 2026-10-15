"""`net/hf.py` (v1.0, M7-модели): ревизия модели по `GET /api/models/{repo}/refs`.

Тесты написаны ДО кода (T-200…T-206, `docs/test-plan.md`; US-6.1, PRD S8-A1/A3/A7).
Модуля ещё нет: каждый тест падает на импорте внутри фикстуры ``hf``.

Предлагаемый интерфейс ``astra_voice.net.hf`` (по образцу ``net/github.py``):

    HF_BASE_URL = "https://huggingface.co"   # константа; HF_ENDPOINT не читается
    CACHE_KEY_PREFIX = "models:hf:"          # ключ кэша = префикс + repo
    MAX_REFS_BYTES: int                      # ≤ update_cache.MAX_BODY_BYTES

    RevisionState = Literal["available", "uptodate", "unknown", "unavailable",
                            "rate-limited", "backoff"]

    def valid_repo(repo: object) -> bool
        # ровно "owner/name"; части [A-Za-z0-9][A-Za-z0-9._-]{0,95}; без "..", "%", "@"
    def refs_url(repo: str, *, base: str = HF_BASE_URL) -> str   # ValueError на плохой repo
    def repo_from_url_path(url_path: str) -> str | None
        # "/owner/name/resolve/<rev>/<file>" → "owner/name"; иначе None
    def parse_refs(body: bytes) -> dict[str, str] | None
        # имя ветки → targetCommit (40 hex, нижний регистр); мусор/дубли ключей/лимит → None;
        # ветки с негодным коммитом пропускаются
    @dataclass(frozen=True)
    class RevisionCheck:
        state: RevisionState
        repo: str
        branch: str
        pinned: str
        remote: str | None = None            # targetCommit нужной ветки, если определён
        http_status: int | None = None
        retry_at: float | None = None
        requests_made: int = 0
    def evaluate(body: bytes, *, repo: str, branch: str, pinned: str) -> RevisionCheck
        # нет ветки / нет targetCommit («без x-repo-commit») / pinned не коммит → "unknown"
    def check(client: HttpClient, *, repo: str, branch: str, pinned: str,
              cache: UpdateCache, cancel: threading.Event,
              kind: NetworkKind = "check_models", jitter_max_s: float = 0.0,
              ignore_backoff: bool = False, url: str | None = None,
              now=time.time, rng=None, wait=None) -> RevisionCheck
        # только через HttpClient.get_check; NetworkError no-network/cancelled — наружу;
        # сбой сети → "unavailable"; 403/429 → "rate-limited"/"backoff" с retry_at.

Отдельно (гейт, T-205): ``NetworkGate.refusal("check_models")`` закрыт при выключенном
тумблере, офлайне пользователя, ``HF_HUB_OFFLINE``, ``offline``/``profile=secure``/
невалидной политике, а также (новое) при ``check_model_updates=false`` из политики и
``updates=offline`` из политики — тогда к HF ноль запросов.
"""

from __future__ import annotations

import pytest

pytest.importorskip("requests")

# ruff: noqa: E402
import importlib
import json
import os
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from types import ModuleType
from typing import Any
from urllib.parse import urlsplit

import requests

from astra_voice.core.policy import Policy, PolicyStatus
from astra_voice.core.settings import Settings
from astra_voice.net import http
from astra_voice.net.gate import NetworkGate
from astra_voice.net.hosts import ALLOWED_HOSTS
from astra_voice.net.http import CheckResult, HttpClient, NetworkError
from astra_voice.net.update_cache import MAX_BODY_BYTES, UpdateCache
from helpers.http_fault_server import FaultServer

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "hf"
EXPORT_REPO = "istupakov/gigaam-v3-onnx"
ORIGINAL_REPO = "ai-sage/GigaAM-v3"
PINNED = "322c3b29492673eb7d0b434bfa9dfb8653e34d02"  # каталог 0.1.6
EXPORT_MAIN = "99052dc2665168ae285e5ec3e697ea56137b069f"
ORIGINAL_MAIN = "34e9772e98c26630621834e07542326b682546c1"
ORIGINAL_E2E_RNNT = "96f450ce0f826b3aadb2e3bda9bbff959449bc54"
REFS_PATH = f"/api/models/{EXPORT_REPO}/refs"


@pytest.fixture
def hf() -> ModuleType:
    """Модуль под тестом; пока его нет — ModuleNotFoundError (ожидаемый red)."""
    return importlib.import_module("astra_voice.net.hf")


def fixture(name: str) -> bytes:
    return (FIXTURES / f"refs-{name}.json").read_bytes()


def refs(**branches: object) -> bytes:
    return json.dumps(
        {
            "branches": [
                {"name": name, "ref": f"refs/heads/{name}", "targetCommit": commit}
                for name, commit in branches.items()
            ],
            "converts": [],
            "tags": [],
        }
    ).encode("utf-8")


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Транспорт пускается только на локальный сервер теста."""
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


def client(settings: Settings | None = None, policy: Policy | None = None) -> HttpClient:
    return HttpClient(
        NetworkGate(
            settings if settings is not None else Settings(check_model_updates=True),
            policy if policy is not None else Policy(),
        ),
        user_agent="astra-voice/test",
    )


def run_check(hf: ModuleType, server: FaultServer, tmp_path: Path, **kwargs: Any) -> Any:
    params: dict[str, Any] = {
        "repo": EXPORT_REPO,
        "branch": "main",
        "pinned": PINNED,
        "cache": UpdateCache(tmp_path / "cache.json"),
        "cancel": threading.Event(),
        "url": server.url + REFS_PATH,
    }
    params.update(kwargs)
    http_client = params.pop("client", None) or client()
    return hf.check(http_client, **params)


# -- T-200: разбор записанных ответов и сравнение с pinned_revision -----------


def test_export_repo_new_commit_is_available(hf: ModuleType) -> None:
    """S8-A1 по экспорт-репо (Д3): targetCommit ветки ≠ установленной ревизии."""
    result = hf.evaluate(fixture("gigaam-v3-onnx"), repo=EXPORT_REPO, branch="main", pinned=PINNED)
    assert result.state == "available"
    assert result.remote == EXPORT_MAIN
    assert (result.repo, result.branch, result.pinned) == (EXPORT_REPO, "main", PINNED)


def test_same_commit_is_uptodate(hf: ModuleType) -> None:
    result = hf.evaluate(
        fixture("gigaam-v3-onnx"), repo=EXPORT_REPO, branch="main", pinned=EXPORT_MAIN
    )
    assert result.state == "uptodate"
    assert result.remote == EXPORT_MAIN


def test_only_main_changed_is_not_an_update(hf: ModuleType) -> None:
    """S8-A3: следим за веткой-вариантом; сдвиг только `main` обновлением не считается."""
    result = hf.evaluate(
        fixture("ai-sage-gigaam-v3"),
        repo=ORIGINAL_REPO,
        branch="e2e_rnnt",
        pinned=ORIGINAL_E2E_RNNT,
    )
    assert result.state == "uptodate"
    assert result.remote == ORIGINAL_E2E_RNNT != ORIGINAL_MAIN


def test_tracked_variant_branch_moved_is_available(hf: ModuleType) -> None:
    result = hf.evaluate(
        fixture("ai-sage-gigaam-v3"), repo=ORIGINAL_REPO, branch="e2e_rnnt", pinned=PINNED
    )
    assert result.state == "available"
    assert result.remote == ORIGINAL_E2E_RNNT


def test_parse_refs_maps_branches_only(hf: ModuleType) -> None:
    parsed = hf.parse_refs(fixture("ai-sage-gigaam-v3"))
    assert parsed is not None
    assert parsed["main"] == ORIGINAL_MAIN
    assert parsed["e2e_rnnt"] == ORIGINAL_E2E_RNNT
    # Теги — не ветки: ревизию по тегу не сравниваем.
    assert "v3.0" not in parsed


# -- T-201: «неизвестно» вместо ложного «обновление доступно» -----------------


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(refs(other=EXPORT_MAIN), id="нет-нужной-ветки"),
        pytest.param(
            json.dumps({"branches": [{"name": "main", "ref": "refs/heads/main"}]}).encode(),
            id="нет-targetCommit",
        ),
        pytest.param(refs(main=None), id="targetCommit-null"),
        pytest.param(refs(main=""), id="targetCommit-пустой"),
        pytest.param(refs(main="99052dc"), id="короткий-sha"),
        pytest.param(refs(main=EXPORT_MAIN.upper()), id="верхний-регистр"),
        pytest.param(refs(main=EXPORT_MAIN + "0"), id="41-символ"),
        pytest.param(refs(main="z" * 40), id="не-hex"),
        pytest.param(refs(main=123), id="число"),
        pytest.param(json.dumps({"branches": []}).encode(), id="веток-нет"),
    ],
)
def test_commit_not_determined_is_unknown(hf: ModuleType, body: bytes) -> None:
    """Без коммита ветки (в т. ч. «без x-repo-commit») — «неизвестно», бейджа нет."""
    result = hf.evaluate(body, repo=EXPORT_REPO, branch="main", pinned=PINNED)
    assert result.state == "unknown"
    assert result.remote is None


@pytest.mark.parametrize("pinned", ["", "rev-new", "322c3b2", PINNED.upper(), "../" + PINNED])
def test_pinned_not_a_commit_is_unknown(hf: ModuleType, pinned: str) -> None:
    """Сравнивать не с чем — молчим, а не предлагаем «обновление»."""
    result = hf.evaluate(fixture("gigaam-v3-onnx"), repo=EXPORT_REPO, branch="main", pinned=pinned)
    assert result.state == "unknown"


# -- T-202: мусорный ответ не роняет проверку --------------------------------


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"not json",
        b"<html>rate limit page</html>",
        b"[]",
        b"null",
        b'"main"',
        b'{"branches": {}}',
        b'{"branches": [1, "x", null]}',
        b'{"branches": [{"name": 1, "targetCommit": "' + EXPORT_MAIN.encode() + b'"}]}',
        b"\xff\xfe",
        b"[" * 100_000,
        b'{"branches": [], "branches": []}',
        b'{"branches": [{"name": "main", "name": "evil", "targetCommit": "'
        + EXPORT_MAIN.encode()
        + b'"}]}',
    ],
    ids=lambda raw: repr(raw[:24]),
)
def test_garbage_is_unknown_without_exception(hf: ModuleType, body: bytes) -> None:
    assert hf.parse_refs(body) is None
    result = hf.evaluate(body, repo=EXPORT_REPO, branch="main", pinned=PINNED)
    assert result.state == "unknown"
    assert result.remote is None


def test_oversized_body_is_rejected(hf: ModuleType) -> None:
    assert 0 < hf.MAX_REFS_BYTES <= MAX_BODY_BYTES
    padding = b" " * (hf.MAX_REFS_BYTES + 1 - len(fixture("gigaam-v3-onnx")))
    body = fixture("gigaam-v3-onnx") + padding
    assert len(body) == hf.MAX_REFS_BYTES + 1
    assert hf.parse_refs(body) is None
    assert hf.evaluate(body, repo=EXPORT_REPO, branch="main", pinned=PINNED).state == "unknown"


# -- T-203: адрес только huggingface.co, HF_ENDPOINT не учитывается -----------


def test_refs_url_is_fixed_and_allowlisted(hf: ModuleType) -> None:
    url = hf.refs_url(EXPORT_REPO)
    assert url == "https://huggingface.co/api/models/istupakov/gigaam-v3-onnx/refs"
    parts = urlsplit(url)
    assert parts.scheme == "https" and parts.hostname in ALLOWED_HOSTS
    assert hf.HF_BASE_URL == "https://huggingface.co"


def test_hf_endpoint_is_ignored(hf: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HF_ENDPOINT", "https://evil.example")
    monkeypatch.setenv("HUGGINGFACE_HUB_ENDPOINT", "https://evil.example")
    importlib.reload(hf)
    assert hf.refs_url(EXPORT_REPO).startswith("https://huggingface.co/api/models/")
    assert hf.HF_BASE_URL == "https://huggingface.co"


def test_check_default_url_ignores_hf_endpoint(
    hf: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Без явного url проверка идёт на huggingface.co и только через get_check."""
    monkeypatch.setenv("HF_ENDPOINT", "https://evil.example")
    calls: list[tuple[str, dict[str, Any]]] = []

    def spy(self: HttpClient, url: str, **kwargs: Any) -> CheckResult:
        calls.append((url, kwargs))
        return CheckResult("unavailable")

    monkeypatch.setattr(HttpClient, "get_check", spy)
    result = hf.check(
        client(),
        repo=EXPORT_REPO,
        branch="main",
        pinned=PINNED,
        cache=UpdateCache(tmp_path / "cache.json"),
        cancel=threading.Event(),
    )
    assert result.state == "unavailable" and result.remote is None
    assert [url for url, _ in calls] == [
        "https://huggingface.co/api/models/istupakov/gigaam-v3-onnx/refs"
    ]
    kwargs = calls[0][1]
    assert kwargs["kind"] == "check_models"
    assert kwargs["cache_key"] == hf.CACHE_KEY_PREFIX + EXPORT_REPO
    assert kwargs["max_body_bytes"] == hf.MAX_REFS_BYTES


@pytest.mark.parametrize(
    "repo",
    [
        "",
        "istupakov",
        "istupakov/",
        "/gigaam-v3-onnx",
        "istupakov/gigaam-v3-onnx/extra",
        "../gigaam-v3-onnx",
        "istupakov/..",
        "istupakov/gigaam%2Fonnx",
        "user:pass@istupakov/gigaam",
        "istupakov/gigaam?x=1",
        "istupakov/gigaam#x",
        "istupakov/gigaam onnx",
        "istupakov/гигаам",
        "istupakov/-gigaam",
        "istupakov/" + "a" * 200,
        "istupakov\\gigaam",
    ],
)
def test_bad_repo_rejected_before_request(hf: ModuleType, repo: str) -> None:
    assert hf.valid_repo(repo) is False
    with pytest.raises(ValueError):
        hf.refs_url(repo)


def test_bad_repo_makes_no_request(hf: ModuleType, server: FaultServer, tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        run_check(hf, server, tmp_path, repo="../evil", url=None)
    assert server.requests == []


@pytest.mark.parametrize(
    ("url_path", "repo"),
    [
        (f"/istupakov/gigaam-v3-onnx/resolve/{PINNED}/config.json", EXPORT_REPO),
        (f"/t-tech/T-one/resolve/{PINNED}/model.onnx", "t-tech/T-one"),
        (f"/alphacep/vosk-model-ru/resolve/{PINNED}/lang/tokens.txt", "alphacep/vosk-model-ru"),
        (f"/istupakov/gigaam-v3-onnx/blob/{PINNED}/config.json", None),
        (f"/istupakov/resolve/{PINNED}/config.json", None),
        (f"istupakov/gigaam-v3-onnx/resolve/{PINNED}/config.json", None),
        (f"/../gigaam-v3-onnx/resolve/{PINNED}/config.json", None),
        ("/models-2026.10/gigaam.onnx", None),
        ("", None),
    ],
)
def test_repo_from_url_path(hf: ModuleType, url_path: str, repo: str | None) -> None:
    assert hf.repo_from_url_path(url_path) == repo


# -- T-204/T-206: через HttpClient и локальный сервер ------------------------


def test_check_through_client(hf: ModuleType, server: FaultServer, tmp_path: Path) -> None:
    server.bodies[REFS_PATH] = fixture("gigaam-v3-onnx")
    result = run_check(hf, server, tmp_path)
    assert result.state == "available" and result.remote == EXPORT_MAIN
    assert result.requests_made == 1 and result.http_status == 200
    assert [path for path, _ in server.requests] == [REFS_PATH]
    headers = server.requests[0][1]
    assert "Authorization" not in headers and "Cookie" not in headers


def test_garbage_response_through_client(
    hf: ModuleType, server: FaultServer, tmp_path: Path
) -> None:
    server.bodies[REFS_PATH] = b"<html>Hugging Face is down</html>"
    result = run_check(hf, server, tmp_path)
    assert result.state == "unknown" and result.remote is None
    assert result.http_status == 200


def test_redirect_to_foreign_host_is_not_followed(
    hf: ModuleType, server: FaultServer, tmp_path: Path
) -> None:
    """T-204: 302 на чужой хост — без запроса туда, без бейджа, без исключения."""
    result = run_check(hf, server, tmp_path, url=server.url + "/evil")
    assert result.state == "unavailable"
    assert result.remote is None
    assert [path for path, _ in server.requests] == ["/evil"]


def test_rate_limit_is_silent(hf: ModuleType, server: FaultServer, tmp_path: Path) -> None:
    """T-206 / S8-A7: 429 → пропуск с окном повтора, без «неизвестно» и без бейджа."""
    result = run_check(hf, server, tmp_path, url=server.url + "/retry-seconds")
    assert result.state == "rate-limited"
    assert result.retry_at is not None and result.remote is None


def test_rate_limit_window_blocks_next_request(
    hf: ModuleType, server: FaultServer, tmp_path: Path
) -> None:
    cache = UpdateCache(tmp_path / "cache.json")
    url = server.url + "/retry-seconds"
    first = run_check(hf, server, tmp_path, cache=cache, url=url)
    second = run_check(hf, server, tmp_path, cache=cache, url=url)
    assert first.state == second.state == "rate-limited"
    assert second.requests_made == 0
    assert len(server.requests) == 1


# -- T-205: гейт закрыт → ноль запросов к HF ---------------------------------

LOCKED_OFF = Policy(
    values={"check_model_updates": False},
    locked_keys=frozenset({"check_model_updates"}),
    status=PolicyStatus.OK,
)

CLOSED_GATES = [
    pytest.param(Settings(check_model_updates=False), Policy(), None, id="тумблер-выключен"),
    pytest.param(
        Settings(check_model_updates=True, offline=True), Policy(), None, id="офлайн-пользователя"
    ),
    pytest.param(
        Settings(check_model_updates=True),
        Policy(
            values={"offline": True}, locked_keys=frozenset({"offline"}), status=PolicyStatus.OK
        ),
        None,
        id="policy-offline",
    ),
    pytest.param(
        Settings(check_model_updates=True),
        Policy(values={"profile": "secure"}, status=PolicyStatus.OK),
        None,
        id="policy-profile-secure",
    ),
    pytest.param(
        Settings(check_model_updates=True),
        Policy(status=PolicyStatus.INVALID),
        None,
        id="policy-невалидна",
    ),
    pytest.param(Settings(check_model_updates=True), Policy(), "1", id="HF_HUB_OFFLINE"),
    pytest.param(
        Settings(check_model_updates=True), LOCKED_OFF, None, id="policy-check_model_updates-false"
    ),
    pytest.param(
        Settings(check_model_updates=True),
        Policy(values={"updates": "offline"}, status=PolicyStatus.OK),
        None,
        id="policy-updates-offline",
    ),
]


@pytest.mark.parametrize(("settings", "policy", "hf_offline"), CLOSED_GATES)
def test_closed_gate_makes_zero_requests(
    hf: ModuleType,
    server: FaultServer,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    policy: Policy,
    hf_offline: str | None,
) -> None:
    if hf_offline is not None:
        monkeypatch.setenv("HF_HUB_OFFLINE", hf_offline)
    server.bodies[REFS_PATH] = fixture("gigaam-v3-onnx")
    with pytest.raises(NetworkError) as refused:
        run_check(hf, server, tmp_path, client=client(settings, policy))
    assert refused.value.code == "no-network"
    assert server.requests == []


@pytest.mark.parametrize(("settings", "policy", "hf_offline"), CLOSED_GATES)
def test_gate_refuses_model_checks(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    policy: Policy,
    hf_offline: str | None,
) -> None:
    """Тот же набор на уровне гейта: работает и без модуля hf (регресс + новые ключи)."""
    if hf_offline is not None:
        monkeypatch.setenv("HF_HUB_OFFLINE", hf_offline)
    assert NetworkGate(settings, policy).refusal("check_models") != ""


def test_gate_open_for_enabled_toggle() -> None:
    assert NetworkGate(Settings(check_model_updates=True), Policy()).refusal("check_models") == ""


def test_cancel_propagates(hf: ModuleType, server: FaultServer, tmp_path: Path) -> None:
    cancel = threading.Event()
    cancel.set()
    server.bodies[REFS_PATH] = fixture("gigaam-v3-onnx")
    with pytest.raises(NetworkError) as cancelled:
        run_check(hf, server, tmp_path, cancel=cancel)
    assert cancelled.value.code == "cancelled"
    assert server.requests == []
