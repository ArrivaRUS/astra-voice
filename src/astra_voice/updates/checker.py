"""Проверка обновлений программы (M7-ядро v0.2; PRD F9.1–F9.3, У41, T-36).

Только источник «программа» (GitHub `releases/latest`); обновления моделей — v1.0.

Порядок: при запуске строка сразу получает результат из кэша, свежая проверка
идёт в фоне не чаще раза в 24 ч, после паузы старта и случайной задержки
0–10 мин (задержку выдерживает :meth:`HttpClient.get_check`). «Проверить сейчас»
не ограничена по частоте и не требует тумблера, но офлайн, запрет
администратора и окно 403/429 действуют и на неё. Разрешения — только через
:class:`NetworkGate`.

Работа идёт в одном ``threading.Thread`` без Qt-объектов (урок 025). Колбэки
``on_status`` и ``on_event`` вызываются **из рабочего потока**: владелец сам
передаёт их в GUI-поток. В события статистики попадают только закрытые коды,
без адресов и текста.
"""

from __future__ import annotations

import logging
import math
import random
import re
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from astra_voice.core import paths
from astra_voice.core.version import __version__
from astra_voice.net import github
from astra_voice.net.gate import NetworkGate
from astra_voice.net.http import (
    APP_CHECK_JITTER_MAX_S,
    RATE_LIMIT_MAX_S,
    RATE_LIMIT_MIN_S,
    HttpClient,
    NetworkError,
    backoff_seconds,
)
from astra_voice.net.update_cache import UpdateCache, shared_cache, write_private
from astra_voice.security.verify import Verifier
from astra_voice.updates.release import (
    ReleaseMetadata,
    ReleaseMetadataError,
    Track,
    VerifiedRelease,
)
from astra_voice.updates.state import SourceState, StateStore

if TYPE_CHECKING:
    from astra_voice.core.policy import Policy
    from astra_voice.core.settings import Settings

log = logging.getLogger(__name__)

UpdateState = Literal[
    "disabled",
    "policy-locked",
    "checking",
    "uptodate",
    "available",
    "unavailable",
    "error-net",
    "skipped",
]
SOURCE = "app"
CHECK_INTERVAL_S = 24 * 3600.0
REMIND_LATER_S = 24 * 3600.0
STARTUP_DELAY_S = 30.0
#: Как часто поток перечитывает гейт и срок «напомнить позже» без внешних событий.
IDLE_WAKE_S = 3600.0
_RAW_TAG = re.compile(r"v?(?:0|[1-9][0-9]{0,8})\.(?:0|[1-9][0-9]{0,8})\.(?:0|[1-9][0-9]{0,8})")
USER_AGENT = f"astra-voice/{__version__} (+https://github.com/ArrivaRUS/astra-voice)"
_STATS_RESULT = {
    "available": "ok",
    "uptodate": "none",
    "unavailable": "unavailable",
    "backoff": "unavailable",
    "rate-limited": "ratelimit",
}


@dataclass(frozen=True)
class UpdateStatus:
    """Снимок для строки состояния (подмножество v0.2 спеки §2.2).

    ``uptodate`` с ``manual=False`` — обычный вид строки без текста: надпись
    «Установлена последняя версия» показывается только после ручной проверки.
    ``reminder_until`` — «Напомнить позже»: версия доступна, но без акцента до
    этого времени. ``error-net`` зарезервировано за действием «Скачать пакет».
    """

    state: UpdateState
    version: str | None = None
    notes: str = ""
    release_url: str | None = None
    manual: bool = False
    reminder_until: float | None = None
    retry_at: float | None = None
    last_attempt_at: float | None = None
    last_success_at: float | None = None
    raw_tag: str | None = None
    verified_release: VerifiedRelease | None = None


class UpdateChecker:
    """Фоновая проверка обновлений программы с расписанием и кэшем."""

    def __init__(
        self,
        gate: NetworkGate,
        client: HttpClient,
        *,
        cache: UpdateCache | None = None,
        store: StateStore | None = None,
        current_version: str = __version__,
        metadata: ReleaseMetadata | None = None,
        track: Track = "deb",
        metadata_cache_dir: Path | None = None,
        url: str = github.RELEASES_LATEST_URL,
        on_status: Callable[[UpdateStatus], None] | None = None,
        on_event: Callable[[str, Mapping[str, str]], None] | None = None,
        clock: Callable[[], float] = time.time,
        rng: random.Random | None = None,
        jitter_max_s: float = APP_CHECK_JITTER_MAX_S,
    ) -> None:
        self._gate = gate
        self._client = client
        self._cache = cache if cache is not None else shared_cache()
        self._store = store if store is not None else StateStore()
        if track not in ("deb", "appimage"):
            raise ValueError("Неизвестный трек обновления")
        self._metadata = (
            metadata
            if metadata is not None
            else ReleaseMetadata(
                client,
                Verifier("release", keyring=paths.data_dir_static() / "keys" / "release.gpg"),
                clock=clock,
            )
        )
        self._track = track
        self._metadata_cache_dir = metadata_cache_dir
        self._verified: VerifiedRelease | None = None
        self._version = current_version
        self._url = url
        self.on_status = on_status
        self.on_event = on_event
        self._clock = clock
        self._rng = rng
        self._jitter_max_s = jitter_max_s
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._status = UpdateStatus("disabled")
        self._published = False
        self._manual_requested = False
        self._actions: list[Callable[[], None]] = []
        # Результат ручной проверки показывается и при выключенном тумблере —
        # до следующего refresh() (смены настроек).
        self._manual_view = False
        self._next_due = 0.0
        self._start_delay_s = 0.0

    # -- публичное API (любой поток) -------------------------------------

    @property
    def status(self) -> UpdateStatus:
        with self._lock:
            return self._status

    def start(self, *, delay_s: float = STARTUP_DELAY_S) -> None:
        """Запускает поток; плановая проверка — не раньше чем через ``delay_s``."""
        with self._lock:
            if self._thread is not None or self._stop.is_set():
                return
            self._start_delay_s = max(0.0, delay_s)
            self._thread = threading.Thread(target=self._run, name="update-checker", daemon=True)
            self._thread.start()

    def check_now(self) -> None:
        """«Проверить сейчас»: без лимита частоты, окно 429 уважается."""
        self._request(manual=True)

    def refresh(self) -> None:
        """Настройки или политика изменились: пересчитать строку без сети."""
        self._request(action=self._reset_manual_view)

    def skip_version(self, version: str) -> None:
        """«Пропустить эту версию»: строка молчит до выхода следующей."""
        if github.parse_semver(version) is None:
            raise ValueError("Версия не в формате SemVer")
        self._request(
            action=partial(
                self._change_state,
                lambda state: replace(state, skipped_version=version, remind_until=None),
            )
        )

    def clear_skip(self) -> None:
        """«Показать» у пропущенной версии."""
        self._request(
            action=partial(self._change_state, lambda state: replace(state, skipped_version=None))
        )

    def remind_later(self) -> None:
        """«Напомнить позже»: без акцента на ``REMIND_LATER_S``."""
        self._request(
            action=partial(
                self._change_state,
                lambda state: replace(state, remind_until=self._clock() + REMIND_LATER_S),
            )
        )
        # Итог ручной проверки игнорирует срок напоминания — снимаем его вид.
        self._request(action=self._reset_manual_view)

    def stop(self, timeout_s: float = 5.0) -> None:
        """Запрашивает отмену транспорта/GPG и ждёт поток до timeout_s."""
        with self._lock:
            self._stop.set()
            self._wake.set()
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout_s)
            if thread.is_alive():
                log.warning("Проверка обновлений не остановилась вовремя")

    # -- рабочий поток ------------------------------------------------------

    def _request(self, *, manual: bool = False, action: Callable[[], None] | None = None) -> None:
        with self._lock:
            if manual:
                self._manual_requested = True
            if action is not None:
                self._actions.append(action)
            self._wake.set()

    def _reset_manual_view(self) -> None:
        self._manual_view = False

    def _change_state(self, change: Callable[[SourceState], SourceState]) -> None:
        self._store.set(SOURCE, change(self._store.get(SOURCE)))

    def _run(self) -> None:
        now = self._clock()
        last = self._store.get(SOURCE).last_attempt_at
        # Часы ушли назад (попытка «в будущем») — такую дату не считаем.
        due = last + CHECK_INTERVAL_S if last is not None and last <= now else now
        self._next_due = max(due, now + self._start_delay_s)
        self._load_metadata()
        self._publish(self._compose())
        while not self._stop.is_set():
            try:
                self._round()
            except Exception:  # noqa: BLE001 — ошибка проверки не должна ронять приложение
                log.warning("Сбой проверки обновлений, повтор позже", exc_info=True)
                self._next_due = max(self._next_due, self._clock() + IDLE_WAKE_S)
                self._wake.wait(IDLE_WAKE_S)

    def _round(self) -> None:
        with self._lock:
            manual = self._manual_requested
            self._manual_requested = False
            actions, self._actions = self._actions, []
            self._wake.clear()
        for action in actions:
            try:
                action()
            except Exception:  # noqa: BLE001 — один сбой не останавливает поток
                log.warning("Действие со строкой обновлений не выполнено", exc_info=True)
        if manual:
            self._check(manual=True)
            return  # срок плановой проверки оценивается следующим кругом
        if self._clock() >= self._next_due and self._gate.refusal("check_app") == "":
            self._check(manual=False)
        else:
            # Итог ручной проверки держится до refresh(): иначе следующий круг
            # публиковал бы тот же результат как фоновый, и строка теряла бы
            # «Установлена последняя версия» раньше 3000 мс.
            self._publish(self._compose(manual=self._manual_view))
        self._wake.wait(self._sleep_s())

    def _sleep_s(self) -> float:
        # Срок проверки прошёл, а гейт закрыт: ждём событий или часового пересчёта.
        now = self._clock()
        wake = [IDLE_WAKE_S]
        if self._next_due > now:
            wake.append(self._next_due - now)
        remind = self._store.get(SOURCE).remind_until
        if remind is not None and remind > now:
            wake.append(remind - now)
        return max(0.05, min(wake))

    def _check(self, *, manual: bool) -> None:
        kind: Literal["check_app", "check_app_manual"] = (
            "check_app_manual" if manual else "check_app"
        )
        if self._gate.refusal(kind):
            self._publish(self._compose())
            return
        if manual:
            self._manual_view = True
            self._publish(replace(self._compose(), state="checking", manual=True))
        previous_entry = self._cache.get(github.CACHE_KEY)
        failures_before = previous_entry.failures if previous_entry.url == self._url else 0
        try:
            result = github.check(
                self._client,
                cache=self._cache,
                cancel=self._stop,
                current_version=self._version,
                kind=kind,
                jitter_max_s=0.0 if manual else self._jitter_max_s,
                ignore_backoff=manual,
                url=self._url,
                now=self._clock,
                rng=self._rng,
                # Плановую задержку прерывают ручная проверка, настройки и остановка;
                # у ручной проверки задержки нет, её прерывает только остановка.
                wait=None if manual else self._wake.wait,
            )
        except NetworkError as error:
            if error.code != "cancelled" and error.code != "no-network":
                log.warning("Проверка обновлений не выполнена: %s", error.code)
            if error.code == "no-network" or error.code == "cancelled":
                self._publish(self._compose())
                return
            result = github.ReleaseCheck("unavailable")
        if self._stop.is_set():
            return
        if result.state == "available":
            release = result.release
            # A previously verified snapshot survives temporary transport
            # failure only when the new discovery has exactly the same identity.
            previous = self._verified
            self._verified = None
            try:
                if release is None or release.raw_tag is None:
                    raise ReleaseMetadataError("invalid-tag")
                verified = self._metadata.fetch(
                    release.raw_tag, self._version, self._track, kind=kind, cancel=self._stop
                )
                if self._stop.is_set():
                    return
                if self._gate.refusal(kind):
                    self._publish(self._compose())
                    return
                if not self._matches(release, verified):
                    raise ReleaseMetadataError("metadata-invalid")
                self._save_metadata(verified)
                self._verified = verified
            except (ReleaseMetadataError, NetworkError, OSError, paths.PathError) as error:
                if self._stop.is_set():
                    return
                code = getattr(error, "code", "metadata-io")
                transport = self._temporary_metadata_failure(error)
                if transport and release is not None and self._matches(release, previous):
                    self._verified = previous
                if code in ("cancelled", "no-network") or self._gate.refusal(kind):
                    self._publish(self._compose())
                    return
                log.warning("Метаданные выпуска не проверены: %s", code)
                if self._verified is None and release is not None and release.raw_tag is not None:
                    self._invalidate_metadata(release.raw_tag)
                retry_at = None
                if isinstance(error, NetworkError) or (
                    isinstance(error, ReleaseMetadataError) and transport
                ):
                    retry_at = self._remember_metadata_failure(error, failures_before)
                result = replace(
                    result,
                    state="rate-limited" if code == "rate-limited" else "unavailable",
                    retry_at=retry_at,
                )
        if self._stop.is_set():
            return
        if self._gate.refusal(kind):
            self._publish(self._compose())
            return
        now = self._clock()
        attempted = result.requests_made > 0
        if not manual:
            self._next_due = max(now + CHECK_INTERVAL_S, result.retry_at or 0.0)
            if attempted:
                # Плановый результат вытесняет ручной: иначе через сутки строка
                # снова показала бы «Установлена последняя версия» и панель.
                self._manual_view = False
        elif attempted:
            # Ручная проверка ушла в сеть — плановая в эти сутки уже не нужна.
            self._next_due = max(self._next_due, now + CHECK_INTERVAL_S)
        if attempted or result.state in ("available", "uptodate"):
            succeeded = result.state in ("available", "uptodate")
            state = self._store.get(SOURCE)
            self._store.set(
                SOURCE,
                replace(
                    state,
                    last_attempt_at=now,
                    last_success_at=now if succeeded else state.last_success_at,
                ),
            )
        if attempted or result.state == "rate-limited":
            self._emit("update_check", {"source": SOURCE, "result": _STATS_RESULT[result.state]})
        log.info("Проверка обновлений: %s", result.state)
        self._publish(
            self._compose(
                manual=manual,
                retry_at=result.retry_at,
                failed_now=manual and result.state not in ("available", "uptodate"),
            )
        )

    @staticmethod
    def _temporary_metadata_failure(error: Exception) -> bool:
        if isinstance(error, ReleaseMetadataError):
            return error.code in ("rate-limited", "backoff")
        if isinstance(error, NetworkError):
            # host-unreachable includes DNS, proxy and TLS refusal in HttpClient.
            # None authorizes fresh metadata; the already signed snapshot alone
            # remains usable with its old success date for the same identity.
            return error.code in ("timeout", "short-read", "host-unreachable") or (
                error.code in ("http-status", "bad-status") and error.status in (429, 502, 503, 504)
            )
        return False

    def _remember_metadata_failure(
        self, error: ReleaseMetadataError | NetworkError, failures_before: int
    ) -> float:
        """Share the app discovery embargo so manual checks/restarts cannot bypass it."""
        now = self._clock()
        failures = failures_before + 1
        delay = backoff_seconds(failures)
        retry_at = getattr(error, "retry_at", None)
        if retry_at is not None and math.isfinite(retry_at):
            delay = max(delay, min(max(0.0, retry_at - now), RATE_LIMIT_MAX_S))
        limited = error.code == "rate-limited"
        if limited:
            delay = max(delay, RATE_LIMIT_MIN_S)
        until = now + min(delay, RATE_LIMIT_MAX_S)
        entry = self._cache.get(github.CACHE_KEY)
        self._cache.set(
            github.CACHE_KEY,
            replace(
                entry,
                url=self._url,
                failures=failures,
                rate_limited_until=until if limited else None,
                backoff_until=None if limited else until,
            ),
        )
        return until

    def _matches(self, release: github.Release, verified: VerifiedRelease | None) -> bool:
        return (
            verified is not None
            and release.raw_tag == verified.raw_tag
            and release.version == verified.version
            and verified.artifact.track == self._track
        )

    def _metadata_directory(self, raw_tag: str, *, create: bool = False) -> Path:
        if _RAW_TAG.fullmatch(raw_tag) is None:
            raise ReleaseMetadataError("invalid-tag")
        root = self._metadata_cache_dir
        if root is None:
            root = paths.cache_dir_path() / "updates" / "metadata"
        directory = root / self._track / raw_tag
        if not directory.is_absolute() or ".." in directory.parts:
            raise ReleaseMetadataError("path-unsafe")
        if any(part.is_symlink() for part in (directory, *directory.parents)):
            raise ReleaseMetadataError("path-unsafe")
        if create:
            if self._metadata_cache_dir is None:
                paths.ensure_private_dir(root.parent.parent)
                paths.ensure_private_dir(root.parent)
            for part in (root, root / self._track, directory):
                paths.ensure_private_dir(part)
        return directory

    def _load_metadata(self) -> None:
        """Only worker startup revalidates persisted bytes; never HTTP."""
        verdict = github.cached(self._cache, self._version, url=self._url)
        if verdict is None or verdict.state != "available" or verdict.release is None:
            return
        release = verdict.release
        if release.raw_tag is None:
            return
        try:
            verified = self._metadata.validate_local(
                self._metadata_directory(release.raw_tag),
                release.raw_tag,
                self._version,
                self._track,
                cancel=self._stop,
            )
            if not self._stop.is_set() and self._matches(release, verified):
                self._verified = verified
        except (ReleaseMetadataError, OSError, paths.PathError):
            log.info("Нет проверенного локального набора метаданных выпуска")

    def _save_metadata(self, verified: VerifiedRelease) -> None:
        directory = self._metadata_directory(verified.raw_tag, create=True)
        for name, raw in (
            ("SHA256SUMS", verified.sums),
            ("SHA256SUMS.asc", verified.signature),
            ("latest.json", verified.latest),
        ):
            write_private(directory / name, raw)
        # Incomplete writes are rejected by validate_local at the next start.

    def _invalidate_metadata(self, raw_tag: str) -> None:
        try:
            (self._metadata_directory(raw_tag) / "latest.json").unlink(missing_ok=True)
        except (ReleaseMetadataError, OSError, paths.PathError):
            log.warning("Не удалось очистить кэш метаданных выпуска")

    def _compose(
        self, *, manual: bool = False, retry_at: float | None = None, failed_now: bool = False
    ) -> UpdateStatus:
        """Строка по гейту, кэшу и сохранённому состоянию — без сети."""
        state = self._store.get(SOURCE)
        if retry_at is None:
            entry = self._cache.get(github.CACHE_KEY)
            if entry.url == self._url:
                now = self._clock()
                active = [
                    min(until, now + RATE_LIMIT_MAX_S)
                    for until in (entry.rate_limited_until, entry.backoff_until)
                    if until is not None and until > now
                ]
                retry_at = max(active) if active else None
        base = UpdateStatus(
            "uptodate",
            retry_at=retry_at,
            last_attempt_at=state.last_attempt_at,
            last_success_at=state.last_success_at,
        )
        # Результат ручной проверки перекрывает только выключенный тумблер,
        # но не офлайн и не запрет администратора.
        refusal = self._gate.refusal("check_app")
        if refusal in ("admin", "policy"):
            return replace(base, state="policy-locked")
        if refusal and (refusal != "settings" or not self._manual_view):
            return replace(base, state="disabled")
        failed = failed_now or (
            state.last_attempt_at is not None
            and (state.last_success_at is None or state.last_attempt_at > state.last_success_at)
        )
        verdict = github.cached(self._cache, self._version, url=self._url)
        release = verdict.release if verdict is not None else None
        if verdict is None or release is None or (failed and verdict.state != "available"):
            # Неудачу не прячем за старым «последняя версия» (У41).
            if failed:
                return replace(base, state="unavailable", manual=manual)
            return replace(base, manual=manual)
        found = replace(
            base,
            manual=manual,
            version=release.version,
            notes=release.notes,
            release_url=release.release_url,
            raw_tag=release.raw_tag,
        )
        if verdict.state == "uptodate":
            return found
        if not self._matches(release, self._verified):
            return replace(base, state="unavailable", manual=manual)
        assert self._verified is not None
        found = replace(
            found, release_url=self._verified.release_url, verified_release=self._verified
        )
        if state.skipped_version == release.version:
            return replace(found, state="skipped")
        remind = state.remind_until
        snoozed = not manual and remind is not None and remind > self._clock()
        return replace(found, state="available", reminder_until=remind if snoozed else None)

    def _publish(self, status: UpdateStatus) -> None:
        with self._lock:
            if self._stop.is_set():
                return
            if self._published and status == self._status:
                return
            self._status = status
            self._published = True
            if self._stop.is_set():
                return
        callback = self.on_status
        if callback is not None:
            try:
                callback(status)
            except Exception:  # noqa: BLE001 — сбой получателя не ломает проверку
                log.warning("Не удалось передать состояние проверки обновлений", exc_info=True)

    def _emit(self, kind: str, fields: Mapping[str, str]) -> None:
        callback = self.on_event
        if callback is None or self._stop.is_set():
            return
        try:
            callback(kind, dict(fields))
        except Exception:  # noqa: BLE001
            log.warning("Не удалось записать событие проверки обновлений", exc_info=True)


def create_app_checker(settings: Settings, policy: Policy) -> UpdateChecker:
    """Проверка программы для приложения: общий кэш процесса и CA из политики."""
    gate = NetworkGate(settings, policy)
    ca_bundle = policy.values.get("ca_bundle")
    client = HttpClient(
        gate,
        ca_bundle=Path(ca_bundle) if isinstance(ca_bundle, str) and ca_bundle else None,
        user_agent=USER_AGENT,
    )
    metadata = ReleaseMetadata(
        client, Verifier("release", keyring=paths.data_dir_static() / "keys" / "release.gpg")
    )
    track: Track = "appimage" if paths.install_kind().is_appimage else "deb"
    return UpdateChecker(gate, client, metadata=metadata, track=track)
