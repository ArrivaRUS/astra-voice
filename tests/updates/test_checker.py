"""`updates/checker.py`: расписание, кэш, гейт, даты попытки и успеха (У41, T-36)."""

from __future__ import annotations

import pytest

pytest.importorskip("requests")

# Импорт зависимости должен предшествовать импорту проверяемого модуля.
# ruff: noqa: E402
import json
import os
import random
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from astra_voice.core import stats
from astra_voice.core.policy import Policy, PolicyStatus, effective
from astra_voice.core.settings import Settings
from astra_voice.net import github
from astra_voice.net.gate import NetworkGate
from astra_voice.net.http import HttpClient
from astra_voice.net.update_cache import CacheEntry, UpdateCache, shared_cache
from astra_voice.updates import checker as checker_module
from astra_voice.updates.checker import (
    CHECK_INTERVAL_S,
    REMIND_LATER_S,
    UpdateChecker,
    UpdateStatus,
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


def seed_cache(rig: Rig, body: bytes) -> None:
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
