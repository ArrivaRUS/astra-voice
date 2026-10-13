"""Onboarding scheduling contracts with a recorded discovery boundary; no sockets."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from astra_voice.core.policy import Policy
from astra_voice.core.settings import Settings
from astra_voice.net import github
from astra_voice.net.gate import NetworkGate
from astra_voice.net.http import HttpClient
from astra_voice.net.update_cache import UpdateCache
from astra_voice.updates.checker import CHECK_INTERVAL_S, UpdateChecker, UpdateStatus
from astra_voice.updates.state import SourceState, StateStore

pytestmark = pytest.mark.unit
T0 = 1_800_000_000.0


class ScheduleRig:
    def __init__(self, root: Path) -> None:
        self.now = T0
        self.settings = Settings(check_app_updates=True, check_model_updates=True)  # AppImage
        self.policy = Policy()
        self.calls: list[str] = []
        self.statuses: list[UpdateStatus] = []
        self.store = StateStore(root / "state.json")
        self.cache = UpdateCache(root / "cache.json")
        self.checker: UpdateChecker | None = None
        self.entered = threading.Event()
        self.release = threading.Event()
        self.release.set()

    def discovery(self, *args: Any, **kwargs: Any) -> github.ReleaseCheck:
        self.calls.append(kwargs["kind"])
        self.entered.set()
        assert self.release.wait(5), "test discovery boundary was not released"
        return github.ReleaseCheck("uptodate", requests_made=1)

    def start(self, *, scheduled: bool = False, delay_s: float = 0.0) -> UpdateChecker:
        gate = NetworkGate(self.settings, self.policy)
        self.checker = UpdateChecker(
            gate,
            HttpClient(gate, user_agent="astra-voice/test"),
            cache=self.cache,
            store=self.store,
            current_version="0.2.0",
            clock=lambda: self.now,
            jitter_max_s=0.0,
            on_status=self.statuses.append,
        )
        self.checker.start(scheduled=scheduled, delay_s=delay_s)
        self.settle()
        return self.checker

    def settle(self) -> None:
        assert self.checker is not None
        for _ in range(2):
            finished = threading.Event()
            self.checker._request(action=finished.set)
            assert finished.wait(5), "worker did not process queued actions"


@pytest.fixture
def schedule(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[ScheduleRig]:
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    rig = ScheduleRig(tmp_path)
    monkeypatch.setattr(github, "check", rig.discovery)
    try:
        yield rig
    finally:
        rig.release.set()
        if rig.checker is not None:
            rig.checker.stop()


def test_onboarding_blocks_scheduled_checks_until_step1_is_accepted(schedule: ScheduleRig) -> None:
    checker = schedule.start()
    for offset in (1.0, CHECK_INTERVAL_S, CHECK_INTERVAL_S * 2):
        schedule.now = T0 + offset
        checker.refresh()
        schedule.settle()
        assert schedule.calls == []
    checker.enable_scheduled_checks()
    schedule.settle()
    assert schedule.calls == ["check_app"]
    assert schedule.store.get("app").last_success_at == schedule.now
    checker.enable_scheduled_checks()
    schedule.settle()
    assert schedule.calls == ["check_app"]


def test_manual_check_bypasses_onboarding_without_opening_schedule(schedule: ScheduleRig) -> None:
    checker = schedule.start()
    checker.check_now()
    schedule.settle()
    assert schedule.calls == ["check_app_manual"]
    assert checker.status.manual
    schedule.now += CHECK_INTERVAL_S + 1
    checker.refresh()
    schedule.settle()
    assert schedule.calls == ["check_app_manual"]
    checker.enable_scheduled_checks()
    schedule.settle()
    assert schedule.calls == ["check_app_manual", "check_app"]


def test_opening_gate_preserves_delay_and_duplicate_notifications_do_not_postpone(
    schedule: ScheduleRig,
) -> None:
    checker = schedule.start(delay_s=10)
    schedule.now += 100
    checker.enable_scheduled_checks()
    schedule.settle()
    assert schedule.calls == []
    schedule.now += 9
    checker.enable_scheduled_checks()
    schedule.settle()
    assert schedule.calls == []
    schedule.now += 1
    checker.refresh()
    schedule.settle()
    assert schedule.calls == ["check_app"]


def test_onboarding_gate_preserves_recent_attempt_24_hour_interval(schedule: ScheduleRig) -> None:
    schedule.store.set("app", SourceState(last_attempt_at=T0 - 60, last_success_at=T0 - 60))
    checker = schedule.start()
    checker.enable_scheduled_checks()
    schedule.settle()
    assert schedule.calls == []
    schedule.now = T0 - 60 + CHECK_INTERVAL_S
    checker.refresh()
    schedule.settle()
    assert schedule.calls == ["check_app"]


def test_toggle_disabled_on_step1_keeps_manual_check_available(schedule: ScheduleRig) -> None:
    checker = schedule.start()
    schedule.settings.check_app_updates = False
    checker.enable_scheduled_checks()
    schedule.settle()
    assert schedule.calls == []
    checker.check_now()
    schedule.settle()
    assert schedule.calls == ["check_app_manual"]


@pytest.mark.parametrize("reason", ["offline", "policy", "secure"])
def test_opening_onboarding_gate_cannot_override_network_refusal(
    schedule: ScheduleRig, reason: str
) -> None:
    if reason == "offline":
        schedule.settings.offline = True
    elif reason == "policy":
        schedule.settings.check_app_updates = False
        schedule.policy = Policy(locked_keys=frozenset({"check_app_updates"}))
    else:
        schedule.policy = Policy(values={"profile": "secure"})
    checker = schedule.start()
    checker.enable_scheduled_checks()
    checker.check_now()
    schedule.settle()
    assert schedule.calls == []
    assert checker.status.state in {"disabled", "policy-locked"}


def test_existing_installation_starts_scheduled_checks_normally(schedule: ScheduleRig) -> None:
    schedule.start(scheduled=True)
    assert schedule.calls == ["check_app"]
    assert any(status.state == "checking" and not status.manual for status in schedule.statuses)
