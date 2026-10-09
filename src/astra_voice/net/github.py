"""Последний выпуск программы на GitHub: `releases/latest` → SemVer (PRD F9.1).

Запрос идёт только через :meth:`HttpClient.get_check`: гейт, список хостов,
кэш с ETag и окна 403/429 здесь не обходятся. Всё, что пришло от сервера,
считается недоверенным: кривой JSON, тег или версия дают состояние
``unavailable`` и не поднимают исключений наружу. Сетевые отказы гейта и отмена
(:class:`NetworkError` с кодами ``no-network`` и ``cancelled``) пробрасываются —
их различает вызывающий код.
"""

from __future__ import annotations

import json
import logging
import random
import re
import threading
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from astra_voice.net.update_cache import MAX_BODY_BYTES, UpdateCache

if TYPE_CHECKING:
    # Разбор версий нужен и без requests (состояние проверок, CI engine).
    from astra_voice.net.gate import NetworkKind
    from astra_voice.net.http import CheckResult, HttpClient

log = logging.getLogger(__name__)

RELEASES_LATEST_URL = "https://api.github.com/repos/ArrivaRUS/astra-voice/releases/latest"
CACHE_KEY = "app:github"
ACCEPT = "application/vnd.github+json"
#: Предел тела ответа. Больше не сохранит и кэш (`update_cache.MAX_BODY_BYTES`).
MAX_RELEASE_BYTES = MAX_BODY_BYTES
#: Предел текста «Что нового»: длиннее обрезается, а не отвергается.
MAX_NOTES_CHARS = 20_000
_MAX_VERSION_CHARS = 64
_MAX_NUMBER_DIGITS = 9
#: Ссылка «Подробнее» — только на страницы выпусков нашего репозитория.
RELEASE_URL_PREFIX = "https://github.com/ArrivaRUS/astra-voice/releases/"

_IDENT = r"(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*)"
_SEMVER_RE = re.compile(
    r"(?P<major>0|[1-9][0-9]*)\.(?P<minor>0|[1-9][0-9]*)\.(?P<patch>0|[1-9][0-9]*)"
    rf"(?:-(?P<pre>{_IDENT}(?:\.{_IDENT})*))?"
    r"(?:\+(?P<build>[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?",
    re.ASCII,
)

ReleaseState = Literal["available", "uptodate", "unavailable", "rate-limited", "backoff"]


@dataclass(frozen=True)
class SemVer:
    """Версия SemVer 2.0.0; метаданные сборки в сравнении не участвуют."""

    major: int
    minor: int
    patch: int
    prerelease: tuple[str, ...] = ()
    text: str = ""

    def _key(self) -> tuple[int, int, int, int, tuple[tuple[int, int, str], ...]]:
        # Без пре-релиза версия старше любой своей пре-версии (SemVer §11).
        pre = tuple(
            (0, int(part), "") if part.isdigit() else (1, 0, part) for part in self.prerelease
        )
        return (self.major, self.minor, self.patch, 0 if self.prerelease else 1, pre)

    def __lt__(self, other: SemVer) -> bool:
        return self._key() < other._key()

    def __gt__(self, other: SemVer) -> bool:
        return self._key() > other._key()


def parse_semver(text: object) -> SemVer | None:
    """Строгий разбор SemVer; всё прочее, включая лишний префикс, — ``None``."""
    if not isinstance(text, str) or not 0 < len(text) <= _MAX_VERSION_CHARS:
        return None
    match = _SEMVER_RE.fullmatch(text)
    if match is None:
        return None
    numbers = (match.group("major"), match.group("minor"), match.group("patch"))
    pre = match.group("pre")
    parts = tuple(pre.split(".")) if pre else ()
    if any(len(number) > _MAX_NUMBER_DIGITS for number in numbers) or any(
        part.isdigit() and len(part) > _MAX_NUMBER_DIGITS for part in parts
    ):
        return None
    return SemVer(int(numbers[0]), int(numbers[1]), int(numbers[2]), parts, text)


def normalize_tag(tag: object) -> SemVer | None:
    """Снимает ровно один префикс ``v`` и разбирает SemVer: ``vv1.0.0`` не проходит."""
    if not isinstance(tag, str):
        return None
    return parse_semver(tag[1:] if tag.startswith("v") else tag)


def installed_version(version: str) -> SemVer | None:
    """Версия пакета: debian-тильда (``0.1.0~m1``) читается как пре-релиз SemVer."""
    return parse_semver(version.replace("~", "-"))


@dataclass(frozen=True)
class Release:
    """Поля discovery; доверие к выпуску устанавливает updates.release."""

    version: str
    notes: str = ""
    release_url: str | None = None
    raw_tag: str | None = None


@dataclass(frozen=True)
class ReleaseCheck:
    """Итог проверки выпуска; ``release`` задан для ``available`` и ``uptodate``."""

    state: ReleaseState
    release: Release | None = None
    http_status: int | None = None
    retry_at: float | None = None
    requests_made: int = 0


def _no_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("повтор ключа JSON")
        result[key] = value
    return result


def _release_url(value: object) -> str | None:
    if (
        not isinstance(value, str)
        or len(value) > 512
        or not value.startswith(RELEASE_URL_PREFIX)
        or not value.isascii()
        or any(char in value for char in "\\ \t\r\n?#%@")
        or any(part in (".", "..") for part in value[len(RELEASE_URL_PREFIX) :].split("/"))
    ):
        return None
    return value


def clean_notes(text: str) -> str:
    """Убирает управляющие (кроме перевода строки и табуляции), форматирующие
    символы, в том числе bidi U+202A–202E и U+2066–2069, и одиночные суррогаты:
    текст выпуска не должен переставлять или прятать строки в «Что нового»."""
    return "".join(
        char
        for char in text
        if char in "\n\t" or unicodedata.category(char) not in ("Cc", "Cf", "Cs")
    )


def parse_release(body: bytes) -> tuple[SemVer, Release] | None:
    """Разбирает ответ `releases/latest`; пре-релиз и черновик — ``None``."""
    if len(body) > MAX_RELEASE_BYTES:
        return None
    try:
        data: object = json.loads(body.decode("utf-8"), object_pairs_hook=_no_duplicates)
    except (UnicodeError, ValueError, RecursionError):
        return None
    if not isinstance(data, dict):
        return None
    if data.get("draft") is not False or data.get("prerelease") is not False:
        return None
    version = normalize_tag(data.get("tag_name"))
    if version is None or version.prerelease:
        return None
    notes = data.get("body")
    return version, Release(
        version=version.text,
        notes=clean_notes(notes[: MAX_NOTES_CHARS * 2])[:MAX_NOTES_CHARS]
        if isinstance(notes, str)
        else "",
        release_url=_release_url(data.get("html_url")),
        raw_tag=data["tag_name"],
    )


def evaluate(body: bytes, current_version: str) -> ReleaseCheck:
    """Сравнивает выпуск из тела ответа с установленной версией."""
    current = installed_version(current_version)
    if current is None:
        log.warning("Версия программы не в формате SemVer, сравнение невозможно")
        return ReleaseCheck("unavailable")
    parsed = parse_release(body)
    if parsed is None:
        log.warning("Ответ источника обновлений не разобран")
        return ReleaseCheck("unavailable")
    latest, release = parsed
    return ReleaseCheck("available" if latest > current else "uptodate", release)


def cached(
    cache: UpdateCache, current_version: str, *, url: str = RELEASES_LATEST_URL
) -> ReleaseCheck | None:
    """Результат по сохранённому ответу без сети; нет годного ответа — ``None``."""
    entry = cache.get(CACHE_KEY)
    if entry.url != url or entry.body is None:
        return None
    result = evaluate(entry.body.encode("utf-8"), current_version)
    return result if result.state != "unavailable" else None


def check(
    client: HttpClient,
    *,
    cache: UpdateCache,
    cancel: threading.Event,
    current_version: str,
    kind: NetworkKind = "check_app",
    jitter_max_s: float = 0.0,
    ignore_backoff: bool = False,
    url: str = RELEASES_LATEST_URL,
    now: Callable[[], float] = time.time,
    rng: random.Random | None = None,
    wait: Callable[[float], bool] | None = None,
) -> ReleaseCheck:
    """Одна проверка `releases/latest` через общий кэш и гейт."""
    result: CheckResult = client.get_check(
        url,
        cache_key=CACHE_KEY,
        cache=cache,
        cancel=cancel,
        kind=kind,
        jitter_max_s=jitter_max_s,
        ignore_backoff=ignore_backoff,
        accept=ACCEPT,
        max_body_bytes=MAX_RELEASE_BYTES,
        now=now,
        rng=rng,
        wait=wait,
    )
    if result.state in ("fresh", "not-modified") and result.body is not None:
        verdict = evaluate(result.body, current_version)
        return ReleaseCheck(
            verdict.state,
            verdict.release,
            http_status=result.http_status,
            requests_made=result.requests_made,
        )
    state: ReleaseState = (
        result.state if result.state in ("rate-limited", "backoff") else "unavailable"
    )
    return ReleaseCheck(
        state,
        http_status=result.http_status,
        retry_at=result.retry_at,
        requests_made=result.requests_made,
    )
