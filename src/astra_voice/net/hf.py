"""Недоверенный discovery HF: только refs экспорт-репозитория, без HEAD."""

from __future__ import annotations

import json
import random
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Literal

from astra_voice.net.update_cache import MAX_BODY_BYTES, UpdateCache

if TYPE_CHECKING:
    from astra_voice.net.gate import NetworkKind
    from astra_voice.net.http import HttpClient

HF_BASE_URL = "https://huggingface.co"
CACHE_KEY_PREFIX = "models:hf:"
MAX_REFS_BYTES = MAX_BODY_BYTES
_PART = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}", re.ASCII)
_COMMIT = re.compile(r"[0-9a-f]{40}", re.ASCII)
RevisionState = Literal[
    "available", "uptodate", "unknown", "unavailable", "rate-limited", "backoff"
]


def valid_repo(repo: object) -> bool:
    """Только owner/name, без неоднозначностей декодирования и переходов пути."""
    return (
        isinstance(repo, str)
        and ".." not in repo
        and len(repo.split("/")) == 2
        and all(_PART.fullmatch(part) is not None for part in repo.split("/"))
    )


def refs_url(repo: str, *, base: str = HF_BASE_URL) -> str:
    if not valid_repo(repo):
        raise ValueError("Неверный репозиторий модели")
    return f"{base.rstrip('/')}/api/models/{repo}/refs"


def repo_from_url_path(url_path: str) -> str | None:
    if (
        not url_path.startswith("/")
        or any(char in url_path for char in "\\%?#@")
        or any(ord(char) <= 32 or ord(char) == 127 for char in url_path)
    ):
        return None
    parts = url_path.split("/")
    if (
        len(parts) < 6
        or parts[3] != "resolve"
        or any(part in ("", ".", "..") for part in parts[1:])
    ):
        return None
    repo = "/".join(parts[1:3])
    return repo if valid_repo(repo) else None


def _unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Повтор ключа")
        result[key] = value
    return result


def _reject_constant(value: str) -> object:
    raise ValueError("Неверная константа JSON")


def parse_refs(body: bytes) -> dict[str, str] | None:
    if len(body) > MAX_REFS_BYTES:
        return None
    try:
        data = json.loads(
            body.decode("utf-8"), object_pairs_hook=_unique, parse_constant=_reject_constant
        )
    except (ValueError, UnicodeError, RecursionError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("branches"), list):
        return None
    result: dict[str, str] = {}
    seen: set[str] = set()
    for branch in data["branches"]:
        if not isinstance(branch, dict) or not isinstance(branch.get("name"), str):
            return None
        name = branch["name"]
        if name in seen:
            return None
        seen.add(name)
        commit = branch.get("targetCommit")
        if isinstance(commit, str) and _COMMIT.fullmatch(commit):
            result[name] = commit
    return result


@dataclass(frozen=True)
class RevisionCheck:
    state: RevisionState
    repo: str
    branch: str
    pinned: str
    remote: str | None = None
    http_status: int | None = None
    retry_at: float | None = None
    requests_made: int = 0


def evaluate(body: bytes, *, repo: str, branch: str, pinned: str) -> RevisionCheck:
    parsed = parse_refs(body)
    remote = parsed.get(branch) if parsed is not None else None
    if remote is None or _COMMIT.fullmatch(pinned) is None:
        return RevisionCheck("unknown", repo, branch, pinned)
    return RevisionCheck(
        "uptodate" if remote == pinned else "available", repo, branch, pinned, remote
    )


def check(
    client: HttpClient,
    *,
    repo: str,
    branch: str,
    pinned: str,
    cache: UpdateCache,
    cancel: threading.Event,
    kind: NetworkKind = "check_models",
    jitter_max_s: float = 0.0,
    ignore_backoff: bool = False,
    url: str | None = None,
    now: Callable[[], float] = time.time,
    rng: random.Random | None = None,
    wait: Callable[[float], bool] | None = None,
) -> RevisionCheck:
    default_url = refs_url(repo)
    result = client.get_check(
        default_url if url is None else url,
        cache_key=CACHE_KEY_PREFIX + repo,
        cache=cache,
        cancel=cancel,
        kind=kind,
        jitter_max_s=jitter_max_s,
        ignore_backoff=ignore_backoff,
        accept="application/json",
        max_body_bytes=MAX_REFS_BYTES,
        now=now,
        rng=rng,
        wait=wait,
    )
    verdict = RevisionCheck("unavailable", repo, branch, pinned)
    if result.state in ("fresh", "not-modified") and result.body is not None:
        verdict = evaluate(result.body, repo=repo, branch=branch, pinned=pinned)
    elif result.state in ("rate-limited", "backoff"):
        verdict = replace(verdict, state=result.state)
    return replace(
        verdict,
        http_status=result.http_status,
        retry_at=result.retry_at,
        requests_made=result.requests_made,
    )
