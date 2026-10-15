"""Получение подписанного каталога и локальные отметки новых моделей (M7)."""

from __future__ import annotations

import base64
import json
import logging
import math
import re
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import replace
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from astra_voice.models import catalog_state
from astra_voice.models.catalog import CATALOG_MAX_BYTES, Catalog, CatalogError
from astra_voice.models.catalog_effective import (
    CATALOG_LOCK,
    SIGNATURE_MAX_BYTES,
    SNAPSHOT_FILE_NAME,
    load_effective,
    state_of,
    verify_catalog,
)
from astra_voice.models.store import ModelRecord
from astra_voice.net import http, update_cache
from astra_voice.net.gate import NetworkKind
from astra_voice.net.http import (
    CHECK_CONNECT_TIMEOUT_S,
    CHECK_TOTAL_TIMEOUT_S,
    RATE_LIMIT_MAX_S,
    RATE_LIMIT_MIN_S,
    HttpClient,
    NetworkError,
    parse_rate_limit,
)
from astra_voice.net.update_cache import CacheEntry, UpdateCache, read_private
from astra_voice.security.verify import Verifier

log = logging.getLogger(__name__)
NEW_BADGE_WINDOW_S = 30 * 24 * 3600.0
_TRACKER_MAX_BYTES = 1024 * 1024


def _urls(catalog_url: str, schemes: tuple[str, ...]) -> tuple[str, str]:
    if (
        not catalog_url
        or not catalog_url.isascii()
        or any(ord(char) <= 32 or ord(char) == 127 or char in "\\%?#" for char in catalog_url)
    ):
        raise ValueError("Неверный адрес каталога")
    parsed = urlsplit(catalog_url)
    if (
        parsed.scheme not in schemes
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or re.fullmatch(r"[A-Za-z0-9.-]+", parsed.hostname) is None
        or any(part in (".", "..") for part in parsed.path.split("/"))
        or parsed.port is not None
        and not 0 < parsed.port <= 65535
    ):
        raise ValueError("Неверный адрес каталога")
    return catalog_url, catalog_url + ".sig"


def catalog_urls(catalog_url: str) -> tuple[str, str]:
    """Production URL: HTTPS, без credentials, параметров и обхода пути."""
    return _urls(catalog_url, ("https",))


def fetch(
    client: HttpClient,
    *,
    catalog_url: str,
    cancel: threading.Event,
    kind: NetworkKind = "check_models",
    scope: Literal["public", "corp"] = "public",
) -> tuple[bytes, bytes]:
    """Два ограниченных GET; большие тела не попадают в UpdateCache.

    Допустимые схемы транспорта также позволяют локальный HTTP test harness.
    В рабочем транспорте разрешена только HTTPS.
    """
    urls = _urls(catalog_url, http.ALLOWED_SCHEMES)
    bodies: list[bytes] = []
    for url, limit in zip(urls, (CATALOG_MAX_BYTES, SIGNATURE_MAX_BYTES), strict=True):
        with client.get_stream(
            url,
            deadline_s=CHECK_TOTAL_TIMEOUT_S,
            connect_timeout_s=CHECK_CONNECT_TIMEOUT_S,
            cancel=cancel,
            kind=kind,
            scope=scope,
            _bounded_connect=True,
            raise_for_status=False,
        ) as response:
            if response.status != 200:
                raise NetworkError(
                    "bad-status",
                    "Источник каталога отклонил запрос.",
                    status=response.status,
                    retry_after_s=parse_rate_limit(response.headers, time.time()),
                )
            length = response.headers.get("Content-Length")
            if length is not None:
                if re.fullmatch(r"[0-9]+", length) is None:
                    raise CatalogError("bad-response", "Некорректная длина ответа.")
                significant = length.lstrip("0")
                if len(significant) > len(str(limit)) or int(significant or "0") > limit:
                    raise CatalogError("too-large", "Каталог или подпись слишком велики.")
            body = b"".join(response.iter_chunks(limit=limit + 1))
            if len(body) > limit:
                raise CatalogError("too-large", "Каталог или подпись слишком велики.")
            bodies.append(body)
    return bodies[0], bodies[1]


def apply_remote(
    raw: bytes,
    signature: bytes,
    verifier: Verifier,
    *,
    schema_root: Path,
    state_path: Path,
    require_schema: bool = True,
) -> Catalog:
    """Проверяет тем же загрузчиком, что встроенный каталог; затем анти-откат.

    Файлы проверки приватны и удаляются при любом исходе. Состояние меняется
    только после успешной подписи, схемы и строгого разбора всех полей.
    """
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        catalog = verify_catalog(
            raw, signature, verifier, schema_root=schema_root, require_schema=require_schema
        )
        try:
            catalog_state.apply_state(state_path, state_of(catalog, raw))
        except ValueError:
            raise CatalogError("stale-catalog", "Каталог старее текущего.") from None
    except OSError:
        raise CatalogError("io", "Не удалось сохранить принятый каталог.") from None
    return catalog


class CatalogUpdateSource:
    """Подписанный удалённый каталог с единым глобальным состоянием.

    Snapshot содержит одну пару raw/signature и публикуется атомарно прежде
    глобального watermark. После сбоя между записями load_effective заново
    проверяет snapshot и восстанавливает watermark. Другого барьера нет.
    """

    def __init__(
        self,
        client: HttpClient,
        *,
        baseline: Catalog | Callable[[], Catalog],
        catalog_url: str | Callable[[], str],
        verifier: Verifier,
        schema_root: Path,
        state_dir: Path,
        cache: UpdateCache | None = None,
        global_state_path: Path | None = None,
        scope: Literal["public", "corp"] = "public",
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._client = client
        self._baseline = baseline
        self._url = catalog_url
        self._verifier = verifier
        self._schema_root = schema_root
        self._directory = state_dir
        self._snapshot = state_dir / SNAPSHOT_FILE_NAME
        self._watermark = (
            global_state_path
            if global_state_path is not None
            else catalog_state.state_path(state_dir.parent)
        )
        self._cache = cache if cache is not None else UpdateCache(state_dir / "http-state.json")
        self._scope = scope
        self._clock = clock
        self._lock = threading.RLock()
        self._catalog: Catalog | None = None
        self._accepted: catalog_state.CatalogState | None = None
        self._baseline_seen: Catalog | None = None
        self._tracker = NewModelTracker(state_dir / "new-models.json")
        self._initialized = False
        self._blocked = False

    def _baseline_catalog(self) -> Catalog:
        return self._baseline() if callable(self._baseline) else self._baseline

    def _catalog_url(self) -> str:
        return self._url() if callable(self._url) else self._url

    @staticmethod
    def _read_watermark(path: Path) -> catalog_state.CatalogState | None:
        try:
            path.lstat()
        except FileNotFoundError:
            return None
        except OSError:
            raise CatalogError("state-unavailable", "Состояние каталога недоступно.") from None
        try:
            result = catalog_state.read_state(path)
        except (OSError, ValueError, RecursionError):
            raise CatalogError("state-invalid", "Состояние каталога повреждено.") from None
        if result is None or re.fullmatch(r"[0-9a-f]{64}", result.sha256) is None:
            raise CatalogError("state-invalid", "Состояние каталога повреждено.")
        return result

    def _prepare(self) -> None:
        baseline = self._baseline_catalog()
        marker = self._read_watermark(self._watermark)
        if marker is None and self._initialized:
            raise CatalogError("state-invalid", "Состояние каталога отсутствует.")
        if (
            self._initialized
            and self._baseline_seen == baseline
            and self._accepted is not None
            and marker is not None
            and (marker.trust_epoch, marker.serial, marker.sha256)
            == (self._accepted.trust_epoch, self._accepted.serial, self._accepted.sha256)
        ):
            return
        self._directory.mkdir(parents=True, exist_ok=True)
        # Единственное место выбора между встроенным и сохранённым удалённым
        # каталогом; один и тот же путь используется ModelService при запуске.
        catalog = load_effective(
            self._verifier,
            builtin_root=self._schema_root,
            snapshot_dir=self._directory,
            state_path=self._watermark,
            require_schema=True,
        )
        accepted = self._read_watermark(self._watermark)
        if accepted is None:
            raise CatalogError("state-invalid", "Состояние каталога отсутствует.")
        self._catalog = catalog
        self._accepted = accepted
        self._baseline_seen = baseline
        self._initialized = True
        self._tracker.observe(catalog, self._clock())

    def current(self) -> Catalog:
        with self._lock, CATALOG_LOCK:
            try:
                self._prepare()
                marker = self._read_watermark(self._watermark)
            except (OSError, ValueError):
                raise CatalogError("state-unavailable", "Состояние каталога недоступно.") from None
            if (
                self._blocked
                or self._catalog is None
                or self._accepted is None
                or marker is None
                or (marker.trust_epoch, marker.serial, marker.sha256)
                != (self._accepted.trust_epoch, self._accepted.serial, self._accepted.sha256)
            ):
                raise CatalogError("stale-catalog", "Актуальный каталог недоступен.")
            return self._catalog

    def refresh(self, cancel: threading.Event) -> None:
        with self._lock:
            if self._client._gate.refusal("check_models"):
                raise NetworkError("no-network", "Проверка каталога запрещена.")
            if cancel.is_set():
                raise NetworkError("cancelled", "Проверка отменена.")
            url = self._catalog_url()
            if not url:
                return
            catalog_urls(url)
            try:
                with CATALOG_LOCK:
                    self._prepare()
                    marker = self._read_watermark(self._watermark)
                if marker is None:
                    raise CatalogError("state-invalid", "Состояние каталога отсутствует.")
                key = "models:catalog:rate-limit"
                cached = self._cache.get(key)
                if cached.rate_limited_until is not None:
                    limited_until = min(cached.rate_limited_until, self._clock() + RATE_LIMIT_MAX_S)
                    if limited_until != cached.rate_limited_until:
                        cached = replace(cached, rate_limited_until=limited_until)
                        self._cache.set(key, cached)
                if (
                    cached.rate_limited_until is not None
                    and cached.rate_limited_until > self._clock()
                ):
                    raise NetworkError("rate-limited", "Проверка отложена.", status=429)
                try:
                    raw, signature = fetch(
                        self._client, catalog_url=url, cancel=cancel, scope=self._scope
                    )
                except NetworkError as error:
                    if error.status in (403, 429):
                        delay = min(
                            RATE_LIMIT_MAX_S,
                            max(RATE_LIMIT_MIN_S, error.retry_after_s or RATE_LIMIT_MIN_S),
                        )
                        self._cache.set(key, CacheEntry(rate_limited_until=self._clock() + delay))
                    raise
                # Проверка без записи состояния: глобальный watermark
                # продвигается только вместе с восстанавливаемым snapshot.
                catalog = verify_catalog(
                    raw,
                    signature,
                    self._verifier,
                    schema_root=self._schema_root,
                    require_schema=True,
                )
                candidate = state_of(catalog, raw)
                if not catalog_state.is_newer(candidate, marker):
                    raise CatalogError("stale-catalog", "Каталог старее текущего.")
                if cancel.is_set():
                    raise NetworkError("cancelled", "Проверка отменена.")
                if self._client._gate.refusal("check_models"):
                    raise NetworkError("no-network", "Проверка каталога запрещена.")
                with CATALOG_LOCK:
                    # Источник базового каталога мог измениться за время запроса.
                    self._prepare()
                    latest_marker = self._read_watermark(self._watermark)
                    if latest_marker is None or not catalog_state.is_newer(
                        candidate, latest_marker
                    ):
                        raise CatalogError("stale-catalog", "Каталог старее текущего.")
                    snapshot = json.dumps(
                        {
                            "raw": base64.b64encode(raw).decode("ascii"),
                            "signature": base64.b64encode(signature).decode("ascii"),
                        },
                        separators=(",", ":"),
                    ).encode()
                    self._blocked = True
                    update_cache.write_private(self._snapshot, snapshot)
                    catalog_state.apply_state(self._watermark, candidate)
                    self._catalog = catalog
                    self._accepted = candidate
                    self._blocked = False
                self._tracker.observe(catalog, self._clock())
                self._cache.set(key, CacheEntry())
            except OSError:
                raise CatalogError("state-write", "Не удалось сохранить каталог.") from None
            except ValueError:
                raise CatalogError("stale-catalog", "Каталог старее текущего.") from None


class NewModelTracker:
    """Первое наблюдение — база; последующие новые id отмечены на 30 дней."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()

    def _read(self) -> dict[str, float | None] | None:
        try:
            data = json.loads(read_private(self._path, _TRACKER_MAX_BYTES).decode("utf-8"))
            if not isinstance(data, dict) or data.get("schema_version") != 1:
                return None
            seen = data.get("seen")
            if not isinstance(seen, dict):
                return None
            for key, stamp in seen.items():
                if (
                    not isinstance(key, str)
                    or stamp is not None
                    and (
                        isinstance(stamp, bool)
                        or not isinstance(stamp, (float, int))
                        or not math.isfinite(stamp)
                        or stamp < 0
                    )
                ):
                    return None
            return dict(seen)
        except (OSError, ValueError, UnicodeError, RecursionError, OverflowError):
            return None

    def observe(self, catalog: Catalog, now: float) -> None:
        with self._lock:
            seen = self._read()
            if seen is None:
                seen = {entry.id: None for entry in catalog.entries}
            else:
                for entry in catalog.entries:
                    seen.setdefault(entry.id, now)
            raw = json.dumps({"schema_version": 1, "seen": seen}, separators=(",", ":")).encode()
            if len(raw) > _TRACKER_MAX_BYTES:
                log.warning("catalog state=unavailable reason=tracker-too-large")
                return
            try:
                update_cache.write_private(self._path, raw)
            except OSError:
                log.warning("catalog state=unavailable reason=tracker-write")

    def is_new(self, model_id: str, now: float) -> bool:
        with self._lock:
            stamp = (self._read() or {}).get(model_id)
            return stamp is not None and 0 <= now - stamp <= NEW_BADGE_WINDOW_S

    def new_ids(self, catalog: Catalog, now: float) -> frozenset[str]:
        with self._lock:
            seen = self._read() or {}
            return frozenset(
                entry.id
                for entry in catalog.entries
                if (stamp := seen.get(entry.id)) is not None
                and 0 <= now - stamp <= NEW_BADGE_WINDOW_S
            )


def removed_installed(catalog: Catalog, records: Iterable[ModelRecord]) -> frozenset[str]:
    present = {entry.id for entry in catalog.entries if not entry.removed_from_catalog}
    return frozenset(
        record.id
        for record in records
        if record.state == "ok"
        and record.metadata_ok
        and not record.recheck
        and record.id not in present
    )
