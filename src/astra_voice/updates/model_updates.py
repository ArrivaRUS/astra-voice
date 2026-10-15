"""Проверка активной модели: HF discovery плюс актуальный подписанный каталог.

Модуль не загружает и не применяет модели. Колбэк notify вызывается из потока
проверки; вызывающая сторона отвечает за передачу уведомления в GUI.
"""

from __future__ import annotations

import json
import logging
import math
import random
import re
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from astra_voice.models.catalog import Catalog, CatalogEntry, CatalogError
from astra_voice.models.store import ModelStore
from astra_voice.net import hf
from astra_voice.net.http import MODEL_CHECK_JITTER_MAX_S, HttpClient, NetworkError
from astra_voice.net.update_cache import UpdateCache, read_private, write_private

log = logging.getLogger(__name__)
SOURCE = "models"
CHECK_INTERVAL_S = 24 * 3600.0
FAILED_MESSAGE = "Новая ревизия не прошла проверку, оставлена текущая"
_MAX_STATE_BYTES = 256 * 1024


@dataclass(frozen=True)
class WatchTarget:
    repo: str
    branch: str = "main"


def watch_target(entry: CatalogEntry) -> WatchTarget | None:
    if entry.removed_from_catalog or entry.host not in ("huggingface.co", "hf.co"):
        return None
    repos = {hf.repo_from_url_path(file.url_path) for file in entry.files}
    if len(repos) != 1 or None in repos:
        return None
    repo = next(iter(repos))
    return WatchTarget(repo) if repo is not None else None


@dataclass(frozen=True)
class ModelUpdateNotice:
    model_id: str
    name: str
    installed_revision: str
    remote_revision: str


def status_line(notices: Sequence[ModelUpdateNotice]) -> str | None:
    return "Обновление модели: " + ", ".join(notice.name for notice in notices) if notices else None


@dataclass(frozen=True)
class ModelCheckSummary:
    state: Literal["disabled", "not-due", "checked", "rate-limited", "unavailable"]
    notices: tuple[ModelUpdateNotice, ...] = ()
    requests_made: int = 0
    retry_at: float | None = None


class ModelUpdateChecker:
    """Суточная проверка только current(); ручная проверка без джиттера.

    catalog — проверенный Catalog либо провайдер актуального проверенного
    снимка. Обновление каталога выполняется владельцем перед run_once.
    """

    def __init__(
        self,
        client: HttpClient,
        *,
        store: ModelStore,
        catalog: Catalog | Callable[[], Catalog],
        cache: UpdateCache,
        state_path: Path,
        api_base: str = hf.HF_BASE_URL,
        clock: Callable[[], float] = time.time,
        rng: random.Random | None = None,
        jitter_max_s: float = MODEL_CHECK_JITTER_MAX_S,
        wait: Callable[[float], bool] | None = None,
        notify: Callable[[ModelUpdateNotice], None] | None = None,
        refresh_catalog: Callable[[threading.Event], None] | None = None,
    ) -> None:
        if not math.isfinite(jitter_max_s) or jitter_max_s < 0:
            raise ValueError("Неверная задержка проверки")
        self._client = client
        self._store = store
        self._catalog = catalog
        self._cache = cache
        self._state_path = state_path
        self._api_base = api_base
        self._clock = clock
        self._rng = rng
        self._jitter_max_s = jitter_max_s
        self._wait = wait
        self._notify = notify
        self._refresh_catalog = refresh_catalog
        self._lock = threading.Lock()

    def _read(self) -> tuple[float | None, set[tuple[str, str]]]:
        try:
            data = json.loads(read_private(self._state_path, _MAX_STATE_BYTES).decode("utf-8"))
            if not isinstance(data, dict) or data.get("schema_version") != 1:
                return None, set()
            stamp = data.get("last_attempt_at")
            if (
                isinstance(stamp, bool)
                or not isinstance(stamp, (float, int))
                or (not math.isfinite(stamp) or stamp < 0)
            ):
                return None, set()
            pairs = data.get("notified")
            if not isinstance(pairs, list) or any(
                not isinstance(pair, list)
                or len(pair) != 2
                or not all(isinstance(item, str) for item in pair)
                or re.fullmatch(r"[0-9a-f]{40}", pair[1]) is None
                for pair in pairs
            ):
                return None, set()
            return float(stamp), {(pair[0], pair[1]) for pair in pairs}
        except (OSError, ValueError, UnicodeError, RecursionError, OverflowError):
            return None, set()

    def _save(self, stamp: float, notified: set[tuple[str, str]]) -> None:
        raw = json.dumps(
            {"schema_version": 1, "last_attempt_at": stamp, "notified": sorted(notified)},
            separators=(",", ":"),
        ).encode()
        if len(raw) > _MAX_STATE_BYTES:
            log.warning("models state=unavailable reason=state-too-large")
            return
        try:
            write_private(self._state_path, raw)
        except OSError:
            log.warning("models state=unavailable reason=state-write")

    def _current_catalog(self) -> Catalog:
        return self._catalog() if callable(self._catalog) else self._catalog

    def run_once(self, cancel: threading.Event, *, manual: bool = False) -> ModelCheckSummary:
        with self._lock:
            return self._run_once(cancel, manual=manual)

    def _run_once(self, cancel: threading.Event, *, manual: bool) -> ModelCheckSummary:
        if self._client._gate.refusal("check_models"):
            return ModelCheckSummary("disabled")
        if cancel.is_set():
            raise NetworkError("cancelled", "Проверка отменена.")
        now = self._clock()
        last_attempt, notified = self._read()
        if not manual and last_attempt is not None and 0 <= now - last_attempt < CHECK_INTERVAL_S:
            return ModelCheckSummary("not-due")
        if self._refresh_catalog is not None:
            if not manual:
                delay = (self._rng or random.Random()).uniform(0, self._jitter_max_s)
                if (self._wait or cancel.wait)(delay) or cancel.is_set():
                    raise NetworkError("cancelled", "Проверка отменена.")
            if self._client._gate.refusal("check_models"):
                return ModelCheckSummary("disabled")
            try:
                self._refresh_catalog(cancel)
            except NetworkError as error:
                if error.code == "no-network":
                    return ModelCheckSummary("disabled")
                if error.code == "cancelled":
                    raise
                log.info("catalog state=unavailable reason=%s", error.code)
                if error.status in (403, 429):
                    self._save(self._clock(), notified)
                    return ModelCheckSummary("rate-limited")
            except CatalogError as error:
                log.info("catalog state=unavailable reason=%s", error.code)
            except Exception:  # noqa: BLE001 — ошибка источника не останавливает планировщик
                log.warning("catalog state=unavailable reason=refresh-failed")
        if cancel.is_set():
            raise NetworkError("cancelled", "Проверка отменена.")
        if self._client._gate.refusal("check_models"):
            return ModelCheckSummary("disabled")
        try:
            catalog = self._current_catalog()
        except (CatalogError, OSError, ValueError):
            log.info("catalog state=unavailable reason=current-unavailable")
            self._save(self._clock(), notified)
            return ModelCheckSummary("unavailable")
        current = self._store.current()
        entry = catalog.entry(current.id) if current is not None else None
        target = watch_target(entry) if entry is not None else None
        if (
            current is None
            or entry is None
            or target is None
            or catalog.is_revoked(current.id, current.revision)
        ):
            if self._refresh_catalog is not None:
                self._save(self._clock(), notified)
            return ModelCheckSummary("checked")
        try:
            result = hf.check(
                self._client,
                repo=target.repo,
                branch=target.branch,
                pinned=current.revision,
                cache=self._cache,
                cancel=cancel,
                jitter_max_s=0.0
                if manual or self._refresh_catalog is not None
                else self._jitter_max_s,
                ignore_backoff=manual,
                url=hf.refs_url(target.repo, base=self._api_base),
                now=self._clock,
                rng=self._rng,
                wait=None if manual or self._refresh_catalog is not None else self._wait,
            )
        except NetworkError as error:
            if error.code == "no-network":
                return ModelCheckSummary("disabled")
            if error.code == "cancelled":
                raise
            self._save(self._clock(), notified)
            log.info("repo=%s state=unavailable reason=%s", target.repo, error.code)
            return ModelCheckSummary("unavailable")
        if cancel.is_set():
            raise NetworkError("cancelled", "Проверка отменена.")
        if self._client._gate.refusal("check_models"):
            return ModelCheckSummary("disabled", requests_made=result.requests_made)
        notices: tuple[ModelUpdateNotice, ...] = ()
        state: str = result.state
        if state == "available" and result.remote is not None:
            # За время сети пользователь мог сменить модель, каталог — обновиться.
            try:
                latest_catalog = self._current_catalog()
            except (CatalogError, OSError, ValueError):
                self._save(self._clock(), notified)
                return ModelCheckSummary("unavailable", requests_made=result.requests_made)
            latest_entry = latest_catalog.entry(current.id)
            active = self._store.current()
            if (
                latest_entry is not None
                and latest_entry.revision == result.remote
                and watch_target(latest_entry) == target
                and not latest_catalog.is_revoked(current.id, result.remote)
                and not latest_catalog.is_revoked(current.id, current.revision)
                and active is not None
                and (active.id, active.revision) == (current.id, current.revision)
            ):
                notices = (
                    ModelUpdateNotice(
                        current.id, latest_entry.name, current.revision, result.remote
                    ),
                )
            else:
                state = "awaiting-catalog"
        log.info("repo=%s state=%s http_status=%s", target.repo, state, result.http_status)
        unseen = tuple(
            notice
            for notice in notices
            if (notice.model_id, notice.remote_revision) not in notified
        )
        if self._notify is not None:
            notified.update((notice.model_id, notice.remote_revision) for notice in unseen)
        self._save(self._clock(), notified)
        for notice in unseen:
            if self._notify is not None:
                self._notify(notice)
        if result.state == "rate-limited":
            return ModelCheckSummary(
                "rate-limited", requests_made=result.requests_made, retry_at=result.retry_at
            )
        if result.state in ("unavailable", "backoff"):
            return ModelCheckSummary(
                "unavailable", requests_made=result.requests_made, retry_at=result.retry_at
            )
        return ModelCheckSummary("checked", notices, result.requests_made)
