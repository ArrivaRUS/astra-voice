"""`updates/checker.py`: расписание, кэш, гейт, даты попытки и успеха (У41, T-36)."""

from __future__ import annotations

import pytest

pytest.importorskip("requests")

# Импорт зависимости должен предшествовать импорту проверяемого модуля.
# ruff: noqa: E402
import hashlib
import json
import os
import random
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path

from astra_voice.core import paths, stats
from astra_voice.core.policy import Policy, PolicyStatus, effective
from astra_voice.core.settings import Settings
from astra_voice.net import github, http
from astra_voice.net.gate import NetworkGate
from astra_voice.net.http import HttpClient, NetworkError
from astra_voice.net.update_cache import CacheEntry, UpdateCache, shared_cache
from astra_voice.security.verify import Verifier, VerifyResult
from astra_voice.ui.updates_bridge import UpdatesBridge
from astra_voice.updates import checker as checker_module
from astra_voice.updates.checker import (
    CHECK_INTERVAL_S,
    REMIND_LATER_S,
    UpdateChecker,
    UpdateStatus,
)
from astra_voice.updates.release import (
    CheckKind,
    ReleaseMetadata,
    ReleaseMetadataError,
    Track,
    VerifiedArtifact,
    VerifiedRelease,
)
from astra_voice.updates.state import SourceState, StateStore
from helpers.http_fault_server import FaultServer
from helpers.net_isolation import fault_server, local_only

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "github"
PATH = "/repos/ArrivaRUS/astra-voice/releases/latest"
T0 = 1_800_000_000.0


def fixture(name: str) -> bytes:
    return (FIXTURES / f"release-{name}.json").read_bytes()


def release(tag: str) -> bytes:
    data = json.loads(fixture("plain"))
    data["tag_name"] = tag
    return json.dumps(data).encode("utf-8")


class MaxRng(random.Random):
    """Случайная задержка всегда максимальна: запрос не успеет уйти сам."""

    def uniform(self, a: float, b: float) -> float:
        return b


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> float:
        return self.now


class MetadataStub(ReleaseMetadata):
    """Scheduling tests inject trust; crypto boundaries have separate tests."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Track, CheckKind, threading.Event | None]] = []
        self.local_calls: list[str] = []
        self.error: str | None = None
        self.after_fetch: Callable[[], None] | None = None

    def result(self, raw_tag: str, track: Track) -> VerifiedRelease:
        version = raw_tag.removeprefix("v")
        name = (
            f"Astra_Voice-{version}-x86_64.AppImage"
            if track == "appimage"
            else f"astra-voice_{version}_amd64.deb"
        )
        url = f"https://github.com/ArrivaRUS/astra-voice/releases/tag/{raw_tag}"
        return VerifiedRelease(
            version,
            raw_tag,
            "2026-10-15T09:00:00Z",
            "1.8",
            url,
            VerifiedArtifact(track, name, "a" * 64, 1024, url + "/" + name),
            b"signed sums",
            b"signature",
            (raw_tag + ":" + track).encode(),
        )

    def fetch(
        self,
        raw_tag: str,
        current_version: str,
        track: Track,
        *,
        kind: CheckKind = "check_app",
        cancel: threading.Event | None = None,
    ) -> VerifiedRelease:
        self.calls.append((raw_tag, track, kind, cancel))
        if self.after_fetch is not None:
            self.after_fetch()
        if self.error is not None:
            raise ReleaseMetadataError(self.error)
        return self.result(raw_tag, track)

    def validate_local(
        self,
        directory: Path,
        raw_tag: str,
        current_version: str,
        track: Track,
        *,
        cancel: threading.Event | None = None,
    ) -> VerifiedRelease:
        self.local_calls.append(raw_tag)
        expected = self.result(raw_tag, track)
        for name, raw in (
            ("SHA256SUMS", expected.sums),
            ("SHA256SUMS.asc", expected.signature),
            ("latest.json", expected.latest),
        ):
            if (directory / name).read_bytes() != raw:
                raise ReleaseMetadataError("hash-mismatch")
        return expected


@dataclass
class Rig:
    server: FaultServer
    tmp: Path
    clock: Clock = field(default_factory=Clock)
    settings: Settings = field(default_factory=lambda: Settings(check_app_updates=True))
    policy: Policy = field(default_factory=Policy)
    statuses: list[UpdateStatus] = field(default_factory=list)
    events: list[tuple[str, dict[str, str]]] = field(default_factory=list)
    checkers: list[UpdateChecker] = field(default_factory=list)
    changed: threading.Condition = field(default_factory=threading.Condition)
    cursor: int = 0
    gate_settings: Settings | None = None
    metadata: MetadataStub = field(default_factory=MetadataStub)

    @property
    def url(self) -> str:
        return self.server.url + PATH

    @property
    def cache(self) -> UpdateCache:
        return UpdateCache(self.tmp / "update-cache.json")

    @property
    def store(self) -> StateStore:
        return StateStore(self.tmp / "update-state.json")

    def requests(self) -> int:
        return sum(path == PATH for path, _ in self.server.requests)

    def make(self, *, version: str = "0.2.0", jitter_max_s: float = 0.0) -> UpdateChecker:
        # Как в app.py: гейт читает живой объект настроек с наложенной политикой.
        self.gate_settings = effective(self.settings, self.policy)
        gate = NetworkGate(self.gate_settings, self.policy)
        checker = UpdateChecker(
            gate,
            HttpClient(gate, user_agent="astra-voice/test"),
            cache=self.cache,
            store=self.store,
            current_version=version,
            metadata=self.metadata,
            metadata_cache_dir=self.tmp / "metadata",
            url=self.url,
            on_status=self._on_status,
            on_event=self._on_event,
            clock=self.clock,
            rng=MaxRng(),
            jitter_max_s=jitter_max_s,
        )
        self.checkers.append(checker)
        return checker

    def start(self, *, version: str = "0.2.0", jitter_max_s: float = 0.0) -> UpdateChecker:
        checker = self.make(version=version, jitter_max_s=jitter_max_s)
        checker.start(delay_s=0.0)
        return checker

    def _on_status(self, status: UpdateStatus) -> None:
        assert threading.current_thread().name == "update-checker"
        with self.changed:
            self.statuses.append(status)
            self.changed.notify_all()

    def _on_event(self, kind: str, fields: Mapping[str, str]) -> None:
        with self.changed:
            self.events.append((kind, dict(fields)))
            self.changed.notify_all()

    def wait(self, predicate: Callable[[UpdateStatus], bool], timeout: float = 5.0) -> UpdateStatus:
        """Ждёт состояние среди опубликованных после предыдущего найденного."""
        deadline = time.monotonic() + timeout
        with self.changed:
            while True:
                for index in range(self.cursor, len(self.statuses)):
                    if predicate(self.statuses[index]):
                        self.cursor = index + 1
                        return self.statuses[index]
                remaining = deadline - time.monotonic()
                assert remaining > 0, f"нет нужного состояния: {self.statuses[self.cursor :]}"
                self.changed.wait(remaining)

    def settle(self, checker: UpdateChecker) -> UpdateStatus:
        """Ждёт конца текущего круга потока: действия выполняются в начале круга."""
        for _ in range(2):
            marker = threading.Event()
            checker._request(action=marker.set)
            assert marker.wait(5)
        return checker.status


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Rig]:
    local_only(monkeypatch)
    with fault_server(monkeypatch) as server:
        rig = Rig(server, tmp_path)
        server.bodies[PATH] = fixture("plain")
        try:
            yield rig
        finally:
            for checker in rig.checkers:
                checker.stop()


def seed_metadata(rig: Rig, raw_tag: str, track: Track = "deb") -> None:
    verified = rig.metadata.result(raw_tag, track)
    directory = rig.tmp / "metadata" / track / raw_tag
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    for name, raw in (
        ("SHA256SUMS", verified.sums),
        ("SHA256SUMS.asc", verified.signature),
        ("latest.json", verified.latest),
    ):
        (directory / name).write_bytes(raw)
        (directory / name).chmod(0o600)


def seed_cache(rig: Rig, body: bytes) -> None:
    tag = json.loads(body)["tag_name"]
    seed_metadata(rig, tag)
    rig.cache.set(github.CACHE_KEY, CacheEntry(body=body.decode(), stored_at=T0, url=rig.url))


# -- кэш и расписание --------------------------------------------------------


def test_cache_first_then_no_check_within_24h(rig: Rig) -> None:
    """PRD F9.1 / S8-A6: при старте — кэш; проверка была < 24 ч назад → запроса нет."""
    seed_cache(rig, fixture("plain"))
    rig.store.set("app", SourceState(last_attempt_at=T0 - 3600, last_success_at=T0 - 3600))
    checker = rig.start()
    status = rig.wait(lambda s: s.state == "available")
    assert status.version == "0.2.1" and status.notes.startswith("## Что нового")
    assert status.last_success_at == T0 - 3600
    rig.settle(checker)
    assert rig.requests() == 0


def test_first_check_then_once_per_24h(rig: Rig) -> None:
    checker = rig.start()
    status = rig.wait(lambda s: s.state == "available" and s.last_success_at is not None)
    assert status.last_attempt_at == status.last_success_at == T0
    assert rig.requests() == 1
    for offset in (3600.0, CHECK_INTERVAL_S - 1):
        rig.clock.now = T0 + offset
        checker.refresh()
        rig.settle(checker)
        assert rig.requests() == 1
    rig.clock.now = T0 + CHECK_INTERVAL_S + 1
    checker.refresh()
    rig.wait(lambda s: s.last_attempt_at == T0 + CHECK_INTERVAL_S + 1)
    assert rig.requests() == 2
    # Новый запуск в те же сутки не проверяет снова.
    checker.stop()
    rig.start()
    rig.wait(lambda s: s.state == "available")
    rig.settle(rig.checkers[-1])
    assert rig.requests() == 2


def test_startup_delay_before_first_check(rig: Rig) -> None:
    checker = rig.make()
    checker.start(delay_s=60.0)
    rig.wait(lambda s: s.state == "uptodate" and s.version is None)
    rig.settle(checker)
    assert rig.requests() == 0
    rig.clock.now = T0 + 61
    checker.refresh()
    rig.wait(lambda s: s.state == "available")
    assert rig.requests() == 1


def test_jitter_is_interrupted_by_manual_check(rig: Rig) -> None:
    """Плановая проверка ждёт задержку в get_check; «Проверить сейчас» её прерывает."""
    checker = rig.start(jitter_max_s=600.0)
    rig.wait(lambda s: s.state == "uptodate")
    time.sleep(0.1)
    assert rig.requests() == 0
    checker.check_now()
    status = rig.wait(lambda s: s.state == "available" and s.manual)
    assert status.last_attempt_at == T0
    assert rig.requests() == 1


def test_stop_interrupts_jitter(rig: Rig) -> None:
    checker = rig.start(jitter_max_s=600.0)
    rig.wait(lambda s: s.state == "uptodate")
    started = time.monotonic()
    checker.stop()
    assert time.monotonic() - started < 2
    assert rig.requests() == 0


# -- У41 / T-36: попытка и успех раздельно -----------------------------------


def test_network_error_moves_attempt_only(rig: Rig) -> None:
    checker = rig.start(version="0.2.1")
    first = rig.wait(lambda s: s.last_success_at == T0)
    assert first.state == "uptodate" and not first.manual
    rig.server.statuses[PATH] = 503
    rig.clock.now = T0 + 7200
    checker.check_now()
    status = rig.wait(lambda s: s.last_attempt_at == T0 + 7200 and s.state != "checking")
    assert status.state == "unavailable"
    assert status.last_success_at == T0
    assert rig.store.get("app") == SourceState(last_attempt_at=T0 + 7200, last_success_at=T0)
    # Перезапуск: неудача видна без сети и не прячется за «последняя версия».
    checker.stop()
    rig.start(version="0.2.1")
    again = rig.wait(lambda s: s.state == "unavailable")
    assert again.last_attempt_at == T0 + 7200 and again.last_success_at == T0


def test_failure_keeps_known_available_version(rig: Rig) -> None:
    checker = rig.start()
    rig.wait(lambda s: s.state == "available" and s.last_success_at == T0)
    rig.server.statuses[PATH] = 503
    rig.clock.now = T0 + CHECK_INTERVAL_S + 5
    checker.refresh()
    status = rig.wait(lambda s: s.last_attempt_at == T0 + CHECK_INTERVAL_S + 5)
    assert status.state == "available" and status.version == "0.2.1"
    assert status.last_success_at == T0


def test_garbage_response_is_attempt_not_success(rig: Rig) -> None:
    rig.server.bodies[PATH] = fixture("vv-prefix")
    rig.start()
    status = rig.wait(lambda s: s.last_attempt_at == T0)
    assert status.state == "unavailable" and status.last_success_at is None


# -- «Проверить сейчас» ------------------------------------------------------


def test_manual_check_without_toggle(rig: Rig) -> None:
    """PRD S9-A5: тумблер выключен → «отключена», но ручная проверка работает."""
    rig.settings = Settings(check_app_updates=False)
    checker = rig.start(version="0.2.1")
    rig.wait(lambda s: s.state == "disabled")
    rig.settle(checker)
    assert rig.requests() == 0
    checker.check_now()
    rig.wait(lambda s: s.state == "checking")
    status = rig.wait(lambda s: s.state == "uptodate" and s.manual)
    assert status.version == "0.2.1"
    for _ in range(2):  # без ограничения частоты
        checker.check_now()
        rig.wait(lambda s: s.state == "checking")
        rig.wait(lambda s: s.state == "uptodate" and s.manual)
    assert rig.requests() == 3
    checker.refresh()
    rig.wait(lambda s: s.state == "disabled")


def test_manual_check_respects_rate_limit_window(rig: Rig) -> None:
    until = T0 + 900
    rig.cache.set(
        github.CACHE_KEY,
        CacheEntry(body=fixture("plain").decode(), url=rig.url, rate_limited_until=until),
    )
    rig.store.set("app", SourceState(last_attempt_at=T0 - 60, last_success_at=T0 - 7200))
    checker = rig.start(version="0.2.1")
    rig.wait(lambda s: s.state == "unavailable")
    checker.check_now()
    status = rig.wait(lambda s: s.manual and s.state == "unavailable")
    assert status.retry_at == until
    assert rig.requests() == 0
    assert ("update_check", {"source": "app", "result": "ratelimit"}) in rig.events


def test_rate_limit_response_sets_window(rig: Rig) -> None:
    rig.server.statuses[PATH] = 429
    checker = rig.start()
    status = rig.wait(lambda s: s.last_attempt_at == T0)
    assert status.state == "unavailable"
    checker.check_now()
    manual = rig.wait(lambda s: s.manual and s.state == "unavailable")
    assert manual.retry_at is not None and manual.retry_at > T0
    assert rig.requests() == 1


# -- гейт ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("values", "status", "expected"),
    [
        ({"offline": True}, PolicyStatus.OK, "policy-locked"),
        ({"profile": "secure"}, PolicyStatus.OK, "policy-locked"),
        ({}, PolicyStatus.INVALID, "policy-locked"),
        ({"check_app_updates": False}, PolicyStatus.OK, "policy-locked"),
    ],
)
def test_policy_blocks_scheduled_and_manual(
    rig: Rig, values: dict[str, object], status: PolicyStatus, expected: str
) -> None:
    rig.policy = Policy(values=values, locked_keys=frozenset(values), status=status)
    checker = rig.start()
    rig.wait(lambda s: s.state == expected)
    checker.check_now()
    assert rig.settle(checker).state == expected
    assert rig.requests() == 0 and rig.events == []


def test_hf_hub_offline_disables(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    checker = rig.start()
    rig.wait(lambda s: s.state == "disabled")
    checker.check_now()
    assert rig.settle(checker).state == "disabled"
    assert rig.requests() == 0


def test_toggle_change_applies_on_refresh(rig: Rig) -> None:
    rig.settings = Settings(check_app_updates=False)
    checker = rig.start()
    rig.wait(lambda s: s.state == "disabled")
    assert rig.gate_settings is not None
    rig.gate_settings.check_app_updates = True  # тумблер включили в настройках
    checker.refresh()
    rig.wait(lambda s: s.state == "available")
    assert rig.requests() == 1


# -- «Пропустить» и «Напомнить позже» (US-7.6, PRD S9-A6) -------------------


def test_skip_version_until_next_release(rig: Rig) -> None:
    checker = rig.start()
    rig.wait(lambda s: s.state == "available")
    checker.skip_version("0.2.1")
    rig.wait(lambda s: s.state == "skipped" and s.version == "0.2.1")
    checker.stop()
    # Перезапуск: 0.2.1 по-прежнему пропущена.
    restarted = rig.start()
    rig.wait(lambda s: s.state == "skipped")
    rig.server.bodies[PATH] = release("v0.2.2")
    restarted.check_now()
    status = rig.wait(lambda s: s.state == "available" and s.version == "0.2.2")
    assert status.manual
    restarted.skip_version("0.2.2")
    rig.wait(lambda s: s.state == "skipped" and s.version == "0.2.2")
    restarted.clear_skip()
    rig.wait(lambda s: s.state == "available" and s.version == "0.2.2")
    with pytest.raises(ValueError):
        restarted.skip_version("../../etc")


def test_remind_later(rig: Rig) -> None:
    checker = rig.start()
    rig.wait(lambda s: s.state == "available")
    checker.remind_later()
    status = rig.wait(lambda s: s.reminder_until is not None)
    assert status.state == "available" and status.reminder_until == T0 + REMIND_LATER_S
    rig.clock.now = T0 + REMIND_LATER_S + 1
    checker.refresh()
    rig.wait(lambda s: s.state == "available" and s.reminder_until is None)


# -- статистика и потоки ----------------------------------------------------


def test_stats_events_are_closed_codes(rig: Rig) -> None:
    checker = rig.start()
    rig.wait(lambda s: s.state == "available")
    rig.server.bodies[PATH] = release("0.2.0")
    checker.check_now()
    rig.wait(lambda s: s.state == "uptodate" and s.manual)
    rig.server.statuses[PATH] = 503
    checker.check_now()
    rig.wait(lambda s: s.state == "unavailable" and s.manual)
    assert [fields["result"] for _, fields in rig.events] == ["ok", "none", "unavailable"]
    for kind, fields in rig.events:
        assert kind == "update_check" and set(fields) == {"source", "result"}
        stats._event(kind, fields)  # схема PRD §10 принимает событие как есть
        assert "http" not in json.dumps(fields)


def test_no_qt_in_checker() -> None:
    """Урок 025: проверка живёт в threading.Thread и не тянет Qt."""
    code = (
        "import sys\n"
        "import astra_voice.updates.checker\n"
        "assert not [m for m in sys.modules if m.startswith('PyQt')], 'Qt загружен'\n"
    )
    subprocess.run(
        [sys.executable, "-c", code],
        check=True,
        capture_output=True,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src")},
    )


def test_one_shared_cache_per_process() -> None:
    assert shared_cache() is shared_cache()
    checker = checker_module.create_app_checker(Settings(), Policy())
    assert checker._cache is shared_cache()
    assert checker.status.state == "disabled"


def test_manual_result_does_not_override_offline(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    rig.settings = Settings(check_app_updates=False)
    checker = rig.start()
    rig.wait(lambda s: s.state == "disabled")
    checker.check_now()
    rig.wait(lambda s: s.state == "available" and s.manual)
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    checker.remind_later()  # любой пересчёт без refresh()
    rig.wait(lambda s: s.state == "disabled")


def test_manual_check_postpones_scheduled(rig: Rig) -> None:
    """Ручная проверка с запросом переносит плановую на сутки от неё."""
    seed_cache(rig, fixture("plain"))
    rig.store.set("app", SourceState(last_attempt_at=T0 - 3600, last_success_at=T0 - 3600))
    checker = rig.start()
    rig.wait(lambda s: s.state == "available")
    rig.clock.now = T0 + 7200
    checker.check_now()
    rig.wait(lambda s: s.manual and s.last_attempt_at == T0 + 7200)
    assert rig.requests() == 1
    # Прежний срок (T0 − 1 ч + 24 ч) прошёл, но сутки от ручной — ещё нет.
    rig.clock.now = T0 + CHECK_INTERVAL_S
    checker.refresh()
    rig.settle(checker)
    assert rig.requests() == 1
    rig.clock.now = T0 + 7200 + CHECK_INTERVAL_S + 1
    checker.refresh()
    rig.wait(lambda s: s.last_attempt_at == T0 + 7200 + CHECK_INTERVAL_S + 1)
    assert rig.requests() == 2


def test_blocked_manual_check_keeps_schedule(rig: Rig) -> None:
    """Ручная проверка без запроса (окно 429) плановый срок не сдвигает."""
    seed_metadata(rig, "0.2.1")
    rig.cache.set(
        github.CACHE_KEY,
        CacheEntry(body=fixture("plain").decode(), url=rig.url, rate_limited_until=T0 + 60),
    )
    rig.store.set("app", SourceState(last_attempt_at=T0 - 3600, last_success_at=T0 - 3600))
    checker = rig.start()
    rig.wait(lambda s: s.state == "available")
    checker.check_now()
    rig.wait(lambda s: s.manual and s.retry_at == T0 + 60)
    assert rig.requests() == 0
    rig.clock.now = T0 - 3600 + CHECK_INTERVAL_S + 1
    checker.refresh()
    rig.wait(lambda s: s.last_attempt_at == rig.clock.now)
    assert rig.requests() == 1


# -- связка с мостом строки (ревью 29.09, P1) --------------------------------


def _bridge_for(rig: Rig, checker: UpdateChecker) -> UpdatesBridge:
    assert rig.gate_settings is not None
    return UpdatesBridge(checker, refusal=NetworkGate(rig.gate_settings, rig.policy).refusal)


def test_manual_uptodate_survives_next_round_in_bridge(rig: Rig) -> None:
    """После check_now следующий круг не превращает итог в фоновый (строка держит 3000 мс)."""
    checker = rig.start(version="0.2.1")
    rig.settle(checker)
    bridge = _bridge_for(rig, checker)
    checker.check_now()
    rig.wait(lambda s: s.state == "uptodate" and s.manual)
    time.sleep(0.2)
    rig.settle(checker)
    assert rig.statuses[-1].manual, [(s.state, s.manual) for s in rig.statuses]
    bridge.set_status(checker.status)  # в приложении — очередью GUI-потока
    assert (bridge.state, bridge.manual, bridge.restState) == ("uptodate", True, "idle")
    checker.refresh()
    rig.settle(checker)
    bridge.set_status(checker.status)
    assert bridge.state == "idle"


def test_manual_check_with_toggle_off_returns_to_disabled(rig: Rig) -> None:
    """Тумблер выключен: итог ручной проверки виден, затем строка — «отключена»."""
    rig.settings = Settings(check_app_updates=False)
    checker = rig.start(version="0.2.1")
    rig.wait(lambda s: s.state == "disabled")
    bridge = _bridge_for(rig, checker)
    assert bridge.restState == "disabled"
    checker.check_now()
    rig.wait(lambda s: s.state == "uptodate" and s.manual)
    rig.settle(checker)
    bridge.set_status(checker.status)
    assert (bridge.state, bridge.manual, bridge.restState) == ("uptodate", True, "disabled")
    checker.refresh()
    rig.settle(checker)
    bridge.set_status(checker.status)
    assert bridge.state == "disabled"


def test_remind_later_after_manual_check_removes_accent(rig: Rig) -> None:
    checker = rig.start()
    rig.settle(checker)
    checker.check_now()
    rig.wait(lambda s: s.state == "available" and s.manual)
    checker.remind_later()
    status = rig.wait(lambda s: s.state == "available" and s.reminder_until is not None)
    assert not status.manual


def test_scheduled_result_replaces_manual_view(rig: Rig) -> None:
    """Повторное ревью P2: плановая проверка через сутки вытесняет итог ручной."""
    checker = rig.start(version="0.2.1")
    rig.settle(checker)
    checker.check_now()
    rig.wait(lambda s: s.state == "uptodate" and s.manual)
    rig.settle(checker)
    count = len(rig.statuses)
    requests = rig.requests()
    rig.clock.now += CHECK_INTERVAL_S + 10
    rig.settle(checker)
    time.sleep(0.3)
    rig.settle(checker)
    assert rig.requests() > requests, "плановая проверка не состоялась"
    after = [(s.state, s.manual) for s in rig.statuses[count:]]
    assert ("uptodate", True) not in after, after
    assert rig.statuses[-1].manual is False


# -- Trust integration: discovery is never a verified release -----------------


@pytest.mark.parametrize("tag", ["0.2.1", "v0.2.1"])
def test_metadata_kind_and_raw_tag_preserved(rig: Rig, tag: str) -> None:
    rig.server.bodies[PATH] = release(tag)
    checker = rig.start()
    status = rig.wait(lambda s: s.state == "available")
    assert status.raw_tag == tag and status.verified_release is not None
    assert rig.metadata.calls == [(tag, "deb", "check_app", checker._stop)]
    checker.check_now()
    rig.wait(lambda s: s.state == "available" and s.manual)
    assert rig.metadata.calls[-1] == (tag, "deb", "check_app_manual", checker._stop)


def test_manual_metadata_works_with_auto_toggle_off(rig: Rig) -> None:
    rig.settings = Settings(check_app_updates=False)
    checker = rig.start()
    rig.wait(lambda s: s.state == "disabled")
    checker.check_now()
    status = rig.wait(lambda s: s.state == "available" and s.manual)
    assert status.verified_release is not None
    assert rig.metadata.calls[0][2] == "check_app_manual"


@pytest.mark.parametrize("error", ["hash-mismatch", "signature-invalid", "metadata-invalid"])
def test_metadata_failure_never_publishes_available_or_success(rig: Rig, error: str) -> None:
    rig.metadata.error = error
    checker = rig.start()
    status = rig.wait(lambda s: s.last_attempt_at == T0)
    assert status.state == "unavailable" and status.last_success_at is None
    assert status.verified_release is None
    assert not any(s.state in ("available", "skipped") for s in rig.statuses)
    assert rig.events[-1][1]["result"] == "unavailable"
    checker.refresh()
    rig.settle(checker)
    assert len(rig.metadata.calls) == 1  # no hidden HTTP or immediate retry


def test_old_trusted_tag_cannot_resurrect_after_new_metadata_failure(rig: Rig) -> None:
    checker = rig.start()
    rig.wait(lambda s: s.state == "available")
    rig.metadata.error = "metadata-invalid"  # T-178: metadata layer rejects mismatched set
    rig.server.bodies[PATH] = release("v0.2.2")
    rig.clock.now += 60
    checker.check_now()
    status = rig.wait(lambda s: s.last_attempt_at == rig.clock.now and s.state != "checking")
    assert status.state == "unavailable" and status.version is None
    assert status.last_success_at == T0
    checker.refresh()
    assert rig.settle(checker).state == "unavailable"
    checker.stop()
    restarted = rig.start()
    assert rig.wait(lambda s: s.state == "unavailable").verified_release is None
    rig.settle(restarted)
    assert rig.requests() == 2


@pytest.mark.parametrize("corrupt", [False, True])
def test_discovery_without_valid_metadata_cache_is_unavailable_without_http(
    rig: Rig,
    corrupt: bool,
) -> None:
    seed_cache(rig, fixture("plain"))
    latest = rig.tmp / "metadata" / "deb" / "0.2.1" / "latest.json"
    if corrupt:
        latest.write_bytes(b"tampered")
    else:
        latest.unlink()
    rig.store.set("app", SourceState(last_attempt_at=T0 - 60, last_success_at=T0 - 60))
    checker = rig.start()
    rig.wait(lambda s: s.state == "unavailable")
    checker.refresh()
    rig.settle(checker)
    assert rig.requests() == 0 and rig.metadata.calls == []
    assert rig.metadata.local_calls == ["0.2.1"]
    checker.check_now()
    rig.wait(lambda s: s.state == "available" and s.manual)


def test_valid_metadata_cache_reverified_once_without_http(rig: Rig) -> None:
    seed_cache(rig, fixture("v-prefix"))
    rig.store.set("app", SourceState(last_attempt_at=T0 - 60, last_success_at=T0 - 60))
    checker = rig.start()
    status = rig.wait(lambda s: s.state == "available")
    assert status.raw_tag == "v0.2.1" and status.verified_release is not None
    checker.remind_later()
    checker.skip_version("0.2.1")
    assert rig.wait(lambda s: s.state == "skipped").verified_release == status.verified_release
    rig.settle(checker)
    assert rig.requests() == 0 and rig.metadata.calls == []
    assert rig.metadata.local_calls == ["v0.2.1"]


def test_matching_tag_requires_matching_track(rig: Rig) -> None:
    seed_cache(rig, fixture("plain"))
    checker = rig.make()
    checker._verified = rig.metadata.result("0.2.1", "appimage")
    assert checker._compose().state == "unavailable"
    checker._verified = replace(rig.metadata.result("0.2.1", "deb"), raw_tag="v0.2.1")
    assert checker._compose().state == "unavailable"


def test_stop_during_metadata_does_not_publish_verified_status(rig: Rig) -> None:
    checker = rig.make()
    rig.metadata.after_fetch = lambda: checker.stop(timeout_s=0)
    checker.start(delay_s=0)
    assert checker._thread is not None
    checker._thread.join(5)
    assert not checker._thread.is_alive()
    assert not any(s.verified_release is not None for s in rig.statuses)
    assert rig.store.get("app").last_success_at is None


def test_policy_rechecked_after_metadata(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    checker = rig.make()
    rig.metadata.after_fetch = lambda: monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    checker.start(delay_s=0)
    rig.wait(lambda s: s.state == "disabled")
    assert not any(s.verified_release is not None for s in rig.statuses)
    assert rig.store.get("app").last_success_at is None


def test_private_cache_preserves_exact_bytes_and_modes(rig: Rig) -> None:
    rig.start()
    status = rig.wait(lambda s: s.state == "available")
    assert status.verified_release is not None
    verified = status.verified_release
    directory = rig.tmp / "metadata" / "deb" / "0.2.1"
    assert all(
        part.stat().st_mode & 0o777 == 0o700
        for part in (directory, directory.parent, directory.parent.parent)
    )
    for name, raw in (
        ("SHA256SUMS", verified.sums),
        ("SHA256SUMS.asc", verified.signature),
        ("latest.json", verified.latest),
    ):
        path = directory / name
        assert path.read_bytes() == raw
        assert path.stat().st_mode & 0o777 == 0o600


def test_metadata_cache_symlink_component_is_refused(rig: Rig) -> None:
    target = rig.tmp / "external"
    target.mkdir()
    (rig.tmp / "metadata").symlink_to(target, target_is_directory=True)
    rig.start()
    status = rig.wait(lambda s: s.last_attempt_at == T0)
    assert status.state == "unavailable" and status.last_success_at is None
    assert list(target.iterdir()) == []


def test_metadata_network_error_is_unavailable(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*args: object, **kwargs: object) -> VerifiedRelease:
        raise NetworkError("http-status", "unavailable")

    monkeypatch.setattr(rig.metadata, "fetch", fail)
    rig.start()
    status = rig.wait(lambda s: s.last_attempt_at == T0)
    assert status.state == "unavailable" and status.last_success_at is None


class AcceptSignature(Verifier):
    """Only detached GPG verification is fake; signed checksum checks are real."""

    def __init__(self) -> None:
        super().__init__("release", Path("/unused-test-keyring"))

    def verify_detached(
        self, data: Path, sig: Path, *, cancel: Callable[[], bool] | None = None
    ) -> VerifyResult:
        return VerifyResult(True, purpose="release")


class MetadataResponse:
    def __init__(self, body: bytes) -> None:
        self.status = 200
        self.headers: dict[str, str] = {}
        self.body = body

    def __enter__(self) -> MetadataResponse:
        return self

    def __exit__(self, *args: object) -> None:
        pass

    def iter_chunks(self, *, limit: int) -> Iterator[bytes]:
        yield self.body[:limit]


def signed_metadata(
    *, sums_version: str = "0.2.1", unsigned_latest: bool = False
) -> dict[str, bytes]:
    latest = json.dumps(
        {
            "schema": 2,
            "version": "0.2.1",
            "min_astra": "1.8",
            "published_at": "2026-10-15T09:00:00Z",
            "release_url": "https://github.com/ArrivaRUS/astra-voice/releases/tag/v0.2.1",
            "artifacts": {
                "deb": {
                    "name": "astra-voice_0.2.1_amd64.deb",
                    "size": 123,
                    "sha256": "a" * 64,
                }
            },
        }
    ).encode()
    digest = hashlib.sha256(b"old latest" if unsigned_latest else latest).hexdigest()
    sums = f"{digest}  latest.json\n{'a' * 64}  astra-voice_{sums_version}_amd64.deb\n".encode()
    return {"latest.json": latest, "SHA256SUMS": sums, "SHA256SUMS.asc": b"test signature"}


def attach_real_metadata(
    checker: UpdateChecker,
    rig: Rig,
    monkeypatch: pytest.MonkeyPatch,
    files: dict[str, bytes],
    *,
    verifier: Verifier | None = None,
    statuses: dict[str, int] | None = None,
    headers: dict[str, str] | None = None,
) -> list[str]:
    calls: list[str] = []

    def stream(url: str, **kwargs: object) -> MetadataResponse:
        calls.append(url)
        assert kwargs["kind"] in ("check_app", "check_app_manual")
        assert kwargs["cancel"] is checker._stop
        name = url.rsplit("/", 1)[-1]
        response = MetadataResponse(files[name])
        response.status = (statuses or {}).get(name, 200)
        response.headers = headers or {}
        return response

    metadata_client = HttpClient(checker._gate, user_agent="astra-voice/metadata-test")
    monkeypatch.setattr(metadata_client, "get_stream", stream)
    checker._metadata = ReleaseMetadata(
        metadata_client,
        verifier if verifier is not None else AcceptSignature(),
        staging_dir=rig.tmp / "staging",
    )
    checker._metadata._clock = rig.clock
    return calls


@pytest.mark.parametrize("case", ["old-signed-assets", "unsigned-latest"])
def test_t178_actual_metadata_rejection_cannot_enable_update(
    rig: Rig,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    rig.server.bodies[PATH] = release("v0.2.1")
    checker = rig.make()
    calls = attach_real_metadata(
        checker,
        rig,
        monkeypatch,
        signed_metadata(
            sums_version="0.2.0" if case == "old-signed-assets" else "0.2.1",
            unsigned_latest=case == "unsigned-latest",
        ),
    )
    checker.start(delay_s=0)
    status = rig.wait(lambda s: s.last_attempt_at == T0)
    assert status.state == "unavailable" and status.last_success_at is None
    assert status.verified_release is None
    assert len(calls) == 3
    assert not any(s.state in ("available", "skipped") for s in rig.statuses)


def test_actual_metadata_cache_reverified_without_http(
    rig: Rig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig.server.bodies[PATH] = release("v0.2.1")
    checker = rig.make()
    attach_real_metadata(checker, rig, monkeypatch, signed_metadata())
    checker.start(delay_s=0)
    trusted = rig.wait(lambda s: s.state == "available").verified_release
    assert trusted is not None
    checker.stop()
    restarted = rig.make()
    calls = attach_real_metadata(restarted, rig, monkeypatch, signed_metadata())
    restarted.start(delay_s=0)
    status = rig.wait(lambda s: s.state == "available")
    assert status.verified_release == trusted
    rig.settle(restarted)
    assert calls == [] and rig.requests() == 1


def test_verifier_unavailable_never_enables_update(
    rig: Rig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checker = rig.make()
    attach_real_metadata(
        checker,
        rig,
        monkeypatch,
        signed_metadata(),
        verifier=Verifier("release", rig.tmp / "missing-keyring.gpg"),
    )
    checker.start(delay_s=0)
    status = rig.wait(lambda s: s.last_attempt_at == T0)
    assert status.state == "unavailable" and status.last_success_at is None


@pytest.mark.parametrize(
    "kind,track",
    [
        (paths.InstallKind.SOURCE, "deb"),
        (paths.InstallKind.DEB, "deb"),
        (paths.InstallKind.APPIMAGE_INSTALLED, "appimage"),
        (paths.InstallKind.APPIMAGE_PORTABLE, "appimage"),
    ],
)
def test_factory_uses_release_keyring_and_install_track(
    monkeypatch: pytest.MonkeyPatch,
    kind: paths.InstallKind,
    track: Track,
) -> None:
    monkeypatch.setattr(paths, "install_kind", lambda: kind)
    checker = checker_module.create_app_checker(Settings(), Policy())
    assert checker._track == track
    assert checker._metadata._verifier.purpose == "release"
    assert checker._metadata._verifier.keyring == paths.data_dir_static() / "keys" / "release.gpg"


def test_default_cache_rejects_xdg_symlink_before_creation_or_chmod(
    rig: Rig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = rig.tmp / "external-cache"
    target.mkdir(mode=0o755)
    link = rig.tmp / "xdg-link"
    link.symlink_to(target, target_is_directory=True)
    monkeypatch.setenv("XDG_CACHE_HOME", str(link))
    checker = rig.make()
    checker._metadata_cache_dir = None
    with pytest.raises(ReleaseMetadataError, match="path-unsafe"):
        checker._metadata_directory("0.2.1", create=True)
    assert list(target.iterdir()) == []
    assert target.stat().st_mode & 0o777 == 0o755


@pytest.mark.parametrize(
    "code,status",
    [("timeout", None), ("bad-status", 503), ("host-unreachable", None), ("short-read", None)],
)
def test_metadata_transport_failure_preserves_same_trusted_release_and_disk_cache(
    rig: Rig,
    monkeypatch: pytest.MonkeyPatch,
    code: str,
    status: int | None,
) -> None:
    checker = rig.start()
    first = rig.wait(lambda s: s.state == "available")
    directory = rig.tmp / "metadata" / "deb" / "0.2.1"
    before = {p.name: p.read_bytes() for p in directory.iterdir()}

    def fail(*args: object, **kwargs: object) -> VerifiedRelease:
        raise NetworkError(code, "temporary transport failure", status=status)

    monkeypatch.setattr(rig.metadata, "fetch", fail)
    rig.clock.now += 60
    checker.check_now()
    failed = rig.wait(lambda s: s.last_attempt_at == rig.clock.now and s.state != "checking")
    assert failed.state == "available" and failed.verified_release == first.verified_release
    assert failed.last_success_at == T0
    assert {p.name: p.read_bytes() for p in directory.iterdir()} == before
    checker.refresh()
    assert rig.settle(checker).state == "available"
    checker.stop()
    restarted = rig.start()
    again = rig.wait(lambda s: s.state == "available")
    assert again.last_success_at == T0 and again.verified_release == first.verified_release
    rig.settle(restarted)
    assert rig.requests() == 2


def test_metadata_transport_failure_for_new_tag_cannot_reuse_old_trust(
    rig: Rig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checker = rig.start()
    rig.wait(lambda s: s.state == "available")
    rig.server.bodies[PATH] = release("v0.2.2")

    def fail(*args: object, **kwargs: object) -> VerifiedRelease:
        raise NetworkError("timeout", "temporary transport failure")

    monkeypatch.setattr(rig.metadata, "fetch", fail)
    rig.clock.now += 60
    checker.check_now()
    status = rig.wait(lambda s: s.last_attempt_at == rig.clock.now and s.state != "checking")
    assert status.state == "unavailable" and status.verified_release is None
    assert status.last_success_at == T0


@pytest.mark.parametrize("code", ["signature-invalid", "metadata-invalid", "path-unsafe"])
def test_trust_failure_invalidates_same_tag_trust_and_disk_cache(rig: Rig, code: str) -> None:
    checker = rig.start()
    rig.wait(lambda s: s.state == "available")
    rig.metadata.error = code
    rig.clock.now += 60
    checker.check_now()
    status = rig.wait(lambda s: s.last_attempt_at == rig.clock.now and s.state != "checking")
    assert status.state == "unavailable" and status.verified_release is None
    assert status.last_success_at == T0
    assert not (rig.tmp / "metadata" / "deb" / "0.2.1" / "latest.json").exists()
    checker.stop()
    rig.start()
    assert rig.wait(lambda s: s.state == "unavailable").verified_release is None


@pytest.mark.parametrize(
    "status,headers",
    [
        (429, {"Retry-After": "900"}),
        (403, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(int(T0 + 900))}),
    ],
)
def test_metadata_rate_limit_blocks_manual_http_and_survives_restart(
    rig: Rig,
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    headers: dict[str, str],
) -> None:
    rig.server.bodies[PATH] = release("v0.2.1")
    checker = rig.make()
    statuses = {"SHA256SUMS": status}
    calls = attach_real_metadata(
        checker, rig, monkeypatch, signed_metadata(), statuses=statuses, headers=headers
    )
    checker.start(delay_s=60)
    rig.wait(lambda s: s.state == "uptodate")
    checker.check_now()
    limited = rig.wait(lambda s: s.last_attempt_at == T0 and s.state != "checking")
    assert limited.state == "unavailable" and limited.retry_at == T0 + 900
    assert limited.last_success_at is None
    assert rig.cache.get(github.CACHE_KEY).rate_limited_until == T0 + 900
    assert rig.requests() == 1 and len(calls) == 1
    checker.check_now()
    rig.settle(checker)
    assert rig.requests() == 1 and len(calls) == 1
    checker.stop()
    restarted = rig.make()
    after = attach_real_metadata(
        restarted, rig, monkeypatch, signed_metadata(), statuses=statuses, headers=headers
    )
    restarted.start(delay_s=0)
    rig.wait(lambda s: s.state == "unavailable")
    restarted.check_now()
    rig.settle(restarted)
    assert rig.requests() == 1 and after == []
    statuses.clear()
    rig.clock.now = T0 + 901
    restarted.check_now()
    verified = rig.wait(lambda s: s.state == "available" and s.last_success_at == T0 + 901)
    assert verified.retry_at is None and verified.verified_release is not None
    assert rig.requests() == 2 and len(after) == 3


def test_metadata_rate_limit_preserves_already_verified_same_release(
    rig: Rig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig.server.bodies[PATH] = release("v0.2.1")
    checker = rig.make()
    statuses: dict[str, int] = {}
    calls = attach_real_metadata(
        checker,
        rig,
        monkeypatch,
        signed_metadata(),
        statuses=statuses,
        headers={"Retry-After": "900"},
    )
    checker.start(delay_s=0)
    first = rig.wait(lambda s: s.state == "available")
    directory = rig.tmp / "metadata" / "deb" / "v0.2.1"
    before = {p.name: p.read_bytes() for p in directory.iterdir()}
    statuses["SHA256SUMS"] = 429
    rig.clock.now += 60
    checker.check_now()
    limited = rig.wait(lambda s: s.last_attempt_at == rig.clock.now and s.state != "checking")
    assert limited.state == "available" and limited.verified_release == first.verified_release
    assert limited.last_success_at == T0 and limited.retry_at == rig.clock.now + 900
    assert {p.name: p.read_bytes() for p in directory.iterdir()} == before
    count = len(calls)
    checker.check_now()
    rig.settle(checker)
    assert rig.requests() == 2 and len(calls) == count


def test_metadata_403_without_rate_headers_uses_backoff_but_manual_can_retry(
    rig: Rig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig.server.bodies[PATH] = release("v0.2.1")
    checker = rig.make()
    calls = attach_real_metadata(
        checker,
        rig,
        monkeypatch,
        signed_metadata(),
        statuses={"SHA256SUMS": 403},
    )
    checker.start(delay_s=60)
    rig.wait(lambda s: s.state == "uptodate")
    checker.check_now()
    first = rig.wait(lambda s: s.last_attempt_at == T0 and s.state != "checking")
    assert first.state == "unavailable" and first.retry_at == T0 + 300
    assert rig.cache.get(github.CACHE_KEY).rate_limited_until is None
    assert rig.cache.get(github.CACHE_KEY).backoff_until == T0 + 300
    checker.check_now()
    rig.settle(checker)
    assert rig.requests() == 2 and len(calls) == 2
    assert rig.cache.get(github.CACHE_KEY).backoff_until == T0 + 600
    assert rig.cache.get(github.CACHE_KEY).failures == 2


@pytest.mark.parametrize(
    "code,status",
    [
        ("http-status", 404),
        ("not-allowed", None),
        ("bad-status", None),
        ("unknown-error", None),
    ],
)
def test_non_temporary_metadata_refusal_invalidates_same_tag_trust(
    rig: Rig,
    monkeypatch: pytest.MonkeyPatch,
    code: str,
    status: int | None,
) -> None:
    checker = rig.start()
    rig.wait(lambda s: s.state == "available")

    def fail(*args: object, **kwargs: object) -> VerifiedRelease:
        raise NetworkError(code, "refusal", status=status)

    monkeypatch.setattr(rig.metadata, "fetch", fail)
    rig.clock.now += 60
    checker.check_now()
    status_now = rig.wait(lambda s: s.last_attempt_at == rig.clock.now and s.state != "checking")
    assert status_now.state == "unavailable" and status_now.verified_release is None
    assert status_now.last_success_at == T0
    assert not (rig.tmp / "metadata" / "deb" / "0.2.1" / "latest.json").exists()


def test_tls_refusal_keeps_only_old_verified_snapshot_and_old_success_date(
    rig: Rig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import requests

    error = http._request_error(requests.exceptions.SSLError("test-only refusal"), "github.com")
    assert error.code == "host-unreachable"
    checker = rig.start()
    first = rig.wait(lambda s: s.state == "available")

    def fail(*args: object, **kwargs: object) -> VerifiedRelease:
        raise error

    monkeypatch.setattr(rig.metadata, "fetch", fail)
    rig.clock.now += 60
    checker.check_now()
    preserved = rig.wait(lambda s: s.last_attempt_at == rig.clock.now and s.state != "checking")
    assert preserved.state == "available" and preserved.verified_release == first.verified_release
    assert preserved.last_success_at == T0
