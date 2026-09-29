"""`net/github.py`: записанные ответы `releases/latest` и сравнение SemVer."""

from __future__ import annotations

import pytest

pytest.importorskip("requests")

# Импорт зависимости должен предшествовать импорту проверяемого модуля.
# ruff: noqa: E402
import json
import os
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from urllib.parse import urlsplit

import requests

from astra_voice.core.policy import Policy
from astra_voice.core.settings import Settings
from astra_voice.net import github, http
from astra_voice.net.gate import NetworkGate
from astra_voice.net.hosts import ALLOWED_HOSTS
from astra_voice.net.http import HttpClient, NetworkError
from astra_voice.net.update_cache import UpdateCache
from helpers.http_fault_server import FaultServer

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "github"
PATH = "/repos/ArrivaRUS/astra-voice/releases/latest"


def fixture(name: str) -> bytes:
    return (FIXTURES / f"release-{name}.json").read_bytes()


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Запрещает сетевой транспорт вне адреса локального сервера."""
    for name in tuple(os.environ):
        if name.lower().endswith("_proxy"):
            monkeypatch.delenv(name)
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    original: Callable[..., requests.Response] = requests.adapters.HTTPAdapter.send

    def local_send(
        self: requests.adapters.HTTPAdapter, request: requests.PreparedRequest, **kwargs: object
    ) -> requests.Response:
        assert urlsplit(request.url).hostname == "127.0.0.1"
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


def client(*, enabled: bool = True) -> HttpClient:
    return HttpClient(
        NetworkGate(Settings(check_app_updates=enabled), Policy()), user_agent="astra-voice/test"
    )


# -- разбор записанных ответов ---------------------------------------------


@pytest.mark.parametrize("name", ["plain", "v-prefix"])
@pytest.mark.parametrize(
    ("installed", "state"),
    [
        ("0.2.0", "available"),
        ("0.1.0~m1", "available"),
        ("0.2.1-rc.1", "available"),
        ("0.2.1", "uptodate"),
        ("0.2.1+build.7", "uptodate"),
        ("0.3.0", "uptodate"),
    ],
)
def test_recorded_release(name: str, installed: str, state: str) -> None:
    result = github.evaluate(fixture(name), installed)
    assert result.state == state
    assert result.release is not None
    assert result.release.version == "0.2.1"
    assert result.release.notes.startswith("## Что нового")
    assert result.release.release_url is not None
    assert result.release.release_url.startswith("https://github.com/ArrivaRUS/astra-voice/")


@pytest.mark.parametrize("name", ["vv-prefix", "prerelease", "draft", "not-semver"])
def test_recorded_rejected(name: str) -> None:
    # Пре-релиз и черновик — не «доступно», даже если версия больше установленной.
    result = github.evaluate(fixture(name), "0.0.1")
    assert result.state == "unavailable"
    assert result.release is None


def release(**fields: object) -> bytes:
    data = json.loads(fixture("plain"))
    data.update(fields)
    return json.dumps(data).encode("utf-8")


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"not json",
        b"[]",
        b"null",
        b'"0.2.1"',
        b"\xff\xfe{}",
        b"[" * 100_000 + b"]" * 100_000,
        b'{"tag_name": "0.2.1", "tag_name": "9.9.9", "draft": false, "prerelease": false}',
        release(tag_name=5),
        release(tag_name=None),
        release(tag_name=""),
        release(tag_name="v"),
        release(tag_name="V0.2.1"),
        release(tag_name=" 0.2.1"),
        release(tag_name="0.2.1\n"),
        release(tag_name="0.02.1"),
        release(tag_name="0.2.1-"),
        release(tag_name="0.2.1-01"),
        release(tag_name="١.٢.٣"),
        release(tag_name="1" * 10 + ".0.0"),
        release(tag_name="0.3.0-rc.1"),
        release(draft=None),
        release(prerelease="false"),
    ],
    ids=lambda value: repr(value[:30]),
)
def test_garbage_is_unavailable(body: bytes) -> None:
    result = github.evaluate(body, "0.0.1")
    assert result.state == "unavailable" and result.release is None


def test_missing_flags_rejected() -> None:
    data = json.loads(fixture("plain"))
    del data["draft"]
    assert github.evaluate(json.dumps(data).encode(), "0.0.1").state == "unavailable"


def test_bad_installed_version_is_unavailable() -> None:
    assert github.evaluate(fixture("plain"), "garbage").state == "unavailable"
    assert github.evaluate(fixture("plain"), "0.2").state == "unavailable"


def test_release_url_and_notes_are_limited() -> None:
    body = release(
        html_url="https://evil.example/ArrivaRUS/astra-voice",
        body="я" * (github.MAX_NOTES_CHARS + 5),
    )
    result = github.evaluate(body, "0.0.1")
    assert result.release is not None
    assert result.release.release_url is None
    assert len(result.release.notes) == github.MAX_NOTES_CHARS
    for url in (
        "http://github.com/ArrivaRUS/astra-voice",
        "https://user@github.com/ArrivaRUS/astra-voice",
        "https://github.com:8443/ArrivaRUS/astra-voice",
        "https://github.com.evil.example/x",
        "https://github.com/a b",
        "javascript:alert(1)",
    ):
        parsed = github.evaluate(release(html_url=url), "0.0.1").release
        assert parsed is not None and parsed.release_url is None, url
    assert github.evaluate(release(body=7), "0.0.1").release == github.Release(
        "0.2.1", "", "https://github.com/ArrivaRUS/astra-voice/releases/tag/0.2.1"
    )


# -- SemVer ------------------------------------------------------------------


def test_semver_precedence() -> None:
    ordered = [
        "1.0.0-alpha",
        "1.0.0-alpha.1",
        "1.0.0-alpha.beta",
        "1.0.0-beta",
        "1.0.0-beta.2",
        "1.0.0-beta.11",
        "1.0.0-rc.1",
        "1.0.0",
        "1.0.1",
        "1.1.0",
        "2.0.0",
        "10.0.0",
    ]
    versions = [github.parse_semver(text) for text in ordered]
    assert all(version is not None for version in versions)
    for lower, higher in zip(versions, versions[1:], strict=False):
        assert lower is not None and higher is not None
        assert lower < higher and higher > lower
        assert not lower > higher


def test_build_metadata_ignored() -> None:
    left = github.parse_semver("1.0.0+a")
    right = github.parse_semver("1.0.0+b")
    assert left is not None and right is not None
    assert not left < right and not left > right


@pytest.mark.parametrize(
    ("tag", "expected"),
    [("0.2.1", "0.2.1"), ("v0.2.1", "0.2.1"), ("vv0.2.1", None), ("v", None), ("", None)],
)
def test_normalize_tag_strips_one_prefix(tag: str, expected: str | None) -> None:
    version = github.normalize_tag(tag)
    assert (version.text if version is not None else None) == expected


# -- через HttpClient и локальный сервер ----------------------------------


def test_check_through_client(server: FaultServer, tmp_path: Path) -> None:
    server.bodies[PATH] = fixture("v-prefix")
    cache = UpdateCache(tmp_path / "cache.json")
    url = server.url + PATH
    result = github.check(
        client(), cache=cache, cancel=threading.Event(), current_version="0.2.0", url=url
    )
    assert result.state == "available" and result.requests_made == 1
    assert result.release is not None and result.release.version == "0.2.1"
    assert server.requests[0][1]["Accept"] == github.ACCEPT
    # Сначала кэш: повторный показ без запросов, другой адрес кэш не использует.
    cached = github.cached(cache, "0.2.0", url=url)
    assert cached is not None and cached.state == "available" and cached.requests_made == 0
    assert github.cached(cache, "0.2.1", url=url) is not None
    assert github.cached(cache, "0.2.0", url=server.url + "/other") is None
    assert len(server.requests) == 1


def test_large_body_is_unavailable(server: FaultServer, tmp_path: Path) -> None:
    server.bodies[PATH] = b" " * (github.MAX_RELEASE_BYTES + 1)
    cache = UpdateCache(tmp_path / "cache.json")
    result = github.check(
        client(),
        cache=cache,
        cancel=threading.Event(),
        current_version="0.2.0",
        url=server.url + PATH,
    )
    assert result.state == "unavailable" and result.release is None
    assert cache.get(github.CACHE_KEY).body is None
    assert github.cached(cache, "0.2.0", url=server.url + PATH) is None


def test_garbage_response_does_not_raise(server: FaultServer, tmp_path: Path) -> None:
    server.bodies[PATH] = b"<html>rate limit page</html>"
    result = github.check(
        client(),
        cache=UpdateCache(tmp_path / "cache.json"),
        cancel=threading.Event(),
        current_version="0.2.0",
        url=server.url + PATH,
    )
    assert result.state == "unavailable" and result.http_status == 200


def test_rate_limit_passed_through(server: FaultServer, tmp_path: Path) -> None:
    result = github.check(
        client(),
        cache=UpdateCache(tmp_path / "cache.json"),
        cancel=threading.Event(),
        current_version="0.2.0",
        url=server.url + "/retry-seconds",
    )
    assert result.state == "rate-limited" and result.retry_at is not None


def test_gate_is_not_bypassed(server: FaultServer, tmp_path: Path) -> None:
    server.bodies[PATH] = fixture("plain")
    cache = UpdateCache(tmp_path / "cache.json")
    with pytest.raises(NetworkError) as refused:
        github.check(
            client(enabled=False),
            cache=cache,
            cancel=threading.Event(),
            current_version="0.2.0",
            url=server.url + PATH,
        )
    assert refused.value.code == "no-network"
    assert server.requests == []
    # «Проверить сейчас» — явное действие: тумблер не нужен.
    manual = github.check(
        client(enabled=False),
        cache=cache,
        cancel=threading.Event(),
        current_version="0.2.0",
        kind="check_app_manual",
        url=server.url + PATH,
    )
    assert manual.state == "available"


def test_default_url_is_allowlisted() -> None:
    parts = urlsplit(github.RELEASES_LATEST_URL)
    assert parts.scheme == "https" and parts.hostname in ALLOWED_HOSTS
