"""О4 с простыми фейками индикаторов и таймера, без окон и сессионной шины."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast
from unittest.mock import Mock

import pytest

from astra_voice.ui import indicators as module
from astra_voice.ui import notify
from astra_voice.ui.indicators import IndicatorGuard, indicator_visible

if TYPE_CHECKING:
    from astra_voice.ui.pill import Pill
    from astra_voice.ui.tray import Tray

pytestmark = pytest.mark.unit


@dataclass
class FakePill:
    enabled: bool = True
    available: bool = True
    forced: bool = False
    forced_calls: list[bool] = field(default_factory=list)
    on_visible: Callable[[], None] | None = None
    on_forced: Callable[[], None] | None = None

    @property
    def visible(self) -> bool:
        if self.on_visible is not None:
            self.on_visible()
        return self.available and (self.enabled or self.forced)

    def set_forced(self, value: bool) -> None:
        self.forced_calls.append(value)
        self.forced = value
        if self.on_forced is not None:
            self.on_forced()


@dataclass
class FakeTray:
    _registered: bool = True
    on_registered: Callable[[], None] | None = None

    @property
    def registered(self) -> bool:
        if self.on_registered is not None:
            self.on_registered()
        return self._registered

    @registered.setter
    def registered(self, value: bool) -> None:
        self._registered = value


class Signal:
    def __init__(self) -> None:
        self.callback: Callable[[], bool] | None = None

    def connect(self, callback: Callable[[], bool]) -> None:
        self.callback = callback

    def emit(self) -> None:
        assert self.callback is not None
        self.callback()


class FakeTimer:
    def __init__(self, parent: object) -> None:
        self.parent = parent
        self.timeout = Signal()
        self.interval = 0
        self.active = False

    def setInterval(self, value: int) -> None:
        self.interval = value

    def isActive(self) -> bool:
        return self.active

    def start(self) -> None:
        self.active = True

    def stop(self) -> None:
        self.active = False

    def fire(self) -> None:
        if self.active:
            self.timeout.emit()


@dataclass
class FakeClock:
    now_ms: int = 100_000

    def monotonic(self) -> float:
        return self.now_ms / 1000

    def advance(self, milliseconds: int) -> None:
        self.now_ms += milliseconds


@dataclass
class Harness:
    guard: IndicatorGuard
    pill: FakePill
    tray: FakeTray
    timer: FakeTimer
    events: list[str]
    clock: FakeClock
    pending_notifications: list[Callable[[], None]]

    def flush_notifications(self) -> None:
        while self.pending_notifications:
            self.pending_notifications.pop(0)()


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> Harness:
    timers: list[FakeTimer] = []
    pending: list[Callable[[], None]] = []

    def timer_factory(parent: object) -> FakeTimer:
        timer = FakeTimer(parent)
        timers.append(timer)
        return timer

    def single_shot(delay_ms: int, callback: Callable[[], None]) -> None:
        assert delay_ms == 0
        pending.append(callback)

    events: list[str] = []
    clock = FakeClock()
    monkeypatch.setattr(module, "monotonic", clock.monotonic)
    monkeypatch.setattr(module, "QTimer", Mock(side_effect=timer_factory, singleShot=single_shot))
    monkeypatch.setattr(notify, "notify_indicators_lost", lambda: events.append("notify"))
    pill = FakePill()
    tray = FakeTray()
    # Cast меняет только статический тип: реальные компоненты не создаются.
    guard = IndicatorGuard(cast("Pill", pill), cast("Tray", tray))
    guard.on_stop_recording = lambda: events.append("stop")
    assert len(timers) == 1
    return Harness(guard, pill, tray, timers[0], events, clock, pending)


@pytest.mark.parametrize(
    ("pill_visible", "tray_registered", "expected"),
    [(False, False, False), (False, True, True), (True, False, True), (True, True, True)],
)
def test_indicator_visible(pill_visible: bool, tray_registered: bool, expected: bool) -> None:
    assert indicator_visible(pill_visible=pill_visible, tray_registered=tray_registered) is expected


def test_lost_tray_forces_disabled_pill_and_return_releases_it(harness: Harness) -> None:
    harness.pill.enabled = False
    harness.guard.set_recording(True)
    assert not harness.pill.visible
    harness.tray.registered = False
    assert not harness.guard.ok
    harness.timer.fire()
    assert harness.pill.forced_calls[-1] is True
    assert harness.pill.visible
    assert harness.guard.ok
    harness.tray.registered = True
    harness.timer.fire()
    assert harness.pill.forced_calls[-1] is False
    assert not harness.pill.visible
    assert harness.guard.ok
    harness.flush_notifications()
    assert harness.events == []


def test_missing_indicators_are_forced_but_not_reported_during_grace(harness: Harness) -> None:
    harness.pill.available = False
    harness.tray.registered = False
    # Грейс отсчитывается от начала записи, а не от создания стража.
    harness.clock.advance(5000)
    harness.guard.set_recording(True)
    assert harness.timer.active
    assert harness.pill.forced_calls == [True]
    assert not harness.guard.ok
    harness.flush_notifications()
    assert harness.events == []
    harness.clock.advance(999)
    harness.timer.fire()
    assert harness.pill.forced_calls == [True, True]
    assert not harness.guard.check()
    assert harness.guard.recording
    harness.flush_notifications()
    assert harness.events == []


@pytest.mark.parametrize("restored", ["pill", "tray"])
def test_indicator_appearing_during_grace_keeps_recording(harness: Harness, restored: str) -> None:
    harness.pill.available = False
    harness.tray.registered = False
    harness.guard.set_recording(True)
    harness.clock.advance(500)
    if restored == "pill":
        harness.pill.available = True
    else:
        harness.tray.registered = True
    harness.timer.fire()
    assert harness.guard.ok
    for _ in range(3):
        harness.clock.advance(1000)
        harness.timer.fire()
    assert harness.guard.recording
    assert harness.timer.active
    harness.flush_notifications()
    assert harness.events == []


def test_loss_after_grace_is_reported_immediately(harness: Harness) -> None:
    harness.tray.registered = False
    harness.guard.set_recording(True)
    harness.clock.advance(5000)
    harness.timer.fire()
    harness.flush_notifications()
    assert harness.events == []
    harness.pill.available = False
    assert not harness.guard.check()
    harness.flush_notifications()
    assert harness.events == ["stop", "notify"]


@pytest.mark.parametrize("appears", [False, True])
@pytest.mark.parametrize("grace_ms", [0, 250, 1000])
def test_forced_show_gets_new_grace_after_recording_grace(
    harness: Harness, appears: bool, grace_ms: int
) -> None:
    guard = IndicatorGuard(
        cast("Pill", harness.pill), cast("Tray", harness.tray), grace_ms=grace_ms
    )
    guard.on_stop_recording = lambda: harness.events.append("stop")
    harness.pill.enabled = False
    harness.pill.available = False
    guard.set_recording(True)
    harness.clock.advance(1300)
    harness.tray.registered = False
    assert not guard.check()
    assert harness.pill.forced
    assert guard.recording
    assert harness.events == []
    assert not harness.pending_notifications
    if grace_ms:
        harness.clock.advance(grace_ms - 1)
        for _ in range(3):
            assert not guard.check()
            guard.set_recording(True)
        assert harness.events == []
        assert not harness.pending_notifications
    harness.pill.available = appears
    harness.clock.advance(1 if grace_ms else 0)
    assert guard.check() is appears
    assert harness.events == ([] if appears else ["stop"])
    assert len(harness.pending_notifications) == (0 if appears else 1)
    harness.flush_notifications()
    expected = [] if appears else ["stop", "notify"]
    assert harness.events == expected
    for _ in range(3):
        harness.clock.advance(1000)
        assert guard.check() is appears
        harness.flush_notifications()
    assert harness.events == expected


def test_forced_show_grace_starts_before_reentrant_check(harness: Harness) -> None:
    harness.guard.set_recording(True)
    harness.clock.advance(1300)
    harness.pill.available = False
    harness.tray.registered = False

    def check_during_force() -> None:
        harness.pill.on_forced = None
        assert not harness.guard.check()

    harness.pill.on_forced = check_during_force
    assert not harness.guard.check()
    assert harness.events == []
    assert not harness.pending_notifications
    harness.clock.advance(999)
    assert not harness.guard.check()
    assert harness.events == []
    harness.clock.advance(1)
    assert not harness.guard.check()
    harness.flush_notifications()
    assert harness.events == ["stop", "notify"]


def test_repeated_recording_true_does_not_extend_grace(harness: Harness) -> None:
    harness.pill.available = False
    harness.tray.registered = False
    harness.guard.set_recording(True)
    harness.clock.advance(999)
    harness.guard.set_recording(True)
    harness.flush_notifications()
    assert harness.events == []
    harness.clock.advance(1)
    harness.timer.fire()
    harness.flush_notifications()
    assert harness.events == ["stop", "notify"]


@pytest.mark.parametrize("grace_ms", [0, 250])
def test_custom_grace_period(harness: Harness, grace_ms: int) -> None:
    harness.pill.available = False
    harness.tray.registered = False
    guard = IndicatorGuard(
        cast("Pill", harness.pill), cast("Tray", harness.tray), grace_ms=grace_ms
    )
    guard.on_stop_recording = lambda: harness.events.append("stop")
    guard.set_recording(True)
    harness.flush_notifications()
    assert harness.events == []  # Даже нулевой грейс пропускает тик включения форса.
    if grace_ms:
        harness.flush_notifications()
        assert harness.events == []
        harness.clock.advance(grace_ms - 1)
        assert not guard.check()
        harness.flush_notifications()
        assert harness.events == []
        harness.clock.advance(1)
    assert not guard.check()
    harness.flush_notifications()
    assert harness.events == ["stop", "notify"]


def test_loss_requests_stop_then_notifies_once_per_recording(harness: Harness) -> None:
    harness.pill.available = False
    harness.tray.registered = False
    harness.guard.set_recording(True)
    harness.clock.advance(1000)
    harness.timer.fire()
    harness.flush_notifications()
    assert harness.events == ["stop", "notify"]
    assert harness.guard.recording  # Страж сам запись не останавливает.
    for _ in range(3):
        assert not harness.guard.check()
        harness.timer.fire()
    harness.guard.set_recording(True)
    harness.flush_notifications()
    assert harness.events == ["stop", "notify"]
    harness.guard.set_recording(False)
    assert not harness.pill.forced
    harness.guard.set_recording(True)
    harness.flush_notifications()
    assert harness.events == ["stop", "notify"]
    harness.clock.advance(999)
    harness.timer.fire()
    harness.flush_notifications()
    assert harness.events == ["stop", "notify"]
    harness.clock.advance(1)
    harness.timer.fire()
    harness.flush_notifications()
    assert harness.events == ["stop", "notify", "stop", "notify"]


@pytest.mark.parametrize("restored", ["pill", "tray"])
def test_any_recovered_indicator_resets_episode(harness: Harness, restored: str) -> None:
    harness.pill.available = False
    harness.tray.registered = False
    harness.guard.set_recording(True)
    harness.clock.advance(1000)
    harness.timer.fire()
    if restored == "pill":
        harness.pill.available = True
    else:
        harness.tray.registered = True
    assert harness.guard.check()
    harness.flush_notifications()
    assert harness.events == ["stop", "notify"]
    harness.pill.available = False
    harness.tray.registered = False
    assert not harness.guard.check()
    if restored == "tray":
        harness.clock.advance(1000)
        assert not harness.guard.check()
    harness.flush_notifications()
    assert harness.events == ["stop", "notify", "stop", "notify"]


def test_idle_does_not_force_notify_or_start_timer(harness: Harness) -> None:
    harness.pill.available = False
    harness.tray.registered = False
    assert not harness.guard.recording
    harness.guard.start()
    assert not harness.guard.check()
    harness.timer.fire()
    assert not harness.timer.active
    assert harness.pill.forced_calls == []
    harness.flush_notifications()
    assert harness.events == []
    harness.guard.set_recording(False)
    assert harness.pill.forced_calls == [False]
    harness.guard.check()
    assert harness.pill.forced_calls == [False]
    harness.flush_notifications()
    assert harness.events == []


def test_missing_stop_callback_is_safe(harness: Harness) -> None:
    harness.guard.on_stop_recording = None
    harness.pill.available = False
    harness.tray.registered = False
    harness.guard.set_recording(True)
    harness.clock.advance(1000)
    assert not harness.guard.check()
    harness.flush_notifications()
    assert harness.events == ["notify"]


def test_timer_lifecycle(harness: Harness) -> None:
    assert harness.timer.interval == 500
    assert harness.timer.parent is harness.guard
    assert not harness.timer.active
    harness.guard.start()
    assert not harness.timer.active
    harness.guard.set_recording(True)
    assert harness.timer.active
    harness.guard.stop()
    assert not harness.timer.active
    assert harness.guard.recording
    harness.pill.available = False
    harness.tray.registered = False
    harness.timer.fire()
    harness.flush_notifications()
    assert harness.events == []
    harness.guard.start()
    assert harness.timer.active
    harness.timer.fire()  # Начало грейса принудительного показа.
    harness.clock.advance(1000)
    harness.timer.fire()
    harness.flush_notifications()
    assert harness.events == ["stop", "notify"]
    harness.guard.set_recording(False)
    assert not harness.timer.active
    assert not harness.guard.recording
    assert not harness.pill.forced
    harness.guard.start()
    assert not harness.timer.active


def test_custom_poll_interval(harness: Harness) -> None:
    guard = IndicatorGuard(cast("Pill", harness.pill), cast("Tray", harness.tray), poll_ms=123)
    timer = cast(FakeTimer, guard._timer)
    assert timer.interval == 123
    assert not timer.active


def test_callback_can_synchronously_end_recording(harness: Harness) -> None:
    def stop_recording() -> None:
        harness.events.append("stop")
        harness.guard.set_recording(False)

    harness.guard.on_stop_recording = stop_recording
    harness.pill.available = False
    harness.tray.registered = False
    harness.guard.set_recording(True)
    harness.clock.advance(1000)
    harness.timer.fire()
    harness.flush_notifications()
    assert harness.events == ["stop", "notify"]
    assert not harness.guard.recording
    assert not harness.timer.active
    assert not harness.pill.forced


def test_reentrant_check_does_not_duplicate_episode(harness: Harness) -> None:
    def stop_recording() -> None:
        harness.events.append("stop")
        assert not harness.guard.check()

    harness.guard.on_stop_recording = stop_recording
    harness.pill.available = False
    harness.tray.registered = False
    harness.guard.set_recording(True)
    harness.clock.advance(1000)
    harness.timer.fire()
    harness.flush_notifications()
    assert harness.events == ["stop", "notify"]


def test_tick_stops_before_queuing_notification_without_calling_transport(harness: Harness) -> None:
    harness.guard.set_recording(True)
    harness.clock.advance(1000)
    harness.pill.available = False
    harness.tray.registered = False

    def stop_recording() -> None:
        assert not harness.pending_notifications
        harness.events.append("stop")
        harness.guard.set_recording(False)

    harness.guard.on_stop_recording = stop_recording
    harness.timer.fire()
    assert harness.events == []
    assert not harness.pending_notifications
    harness.clock.advance(1000)
    harness.timer.fire()
    assert harness.events == ["stop"]
    assert len(harness.pending_notifications) == 1
    assert not harness.guard.recording
    harness.flush_notifications()
    assert harness.events == ["stop", "notify"]
    harness.timer.fire()
    harness.guard.check()
    harness.flush_notifications()
    assert harness.events == ["stop", "notify"]


@pytest.mark.parametrize("grace_ms", [0, 1000])
@pytest.mark.parametrize("probe", ["visible", "registered", "forced"])
@pytest.mark.parametrize("restart", [False, True])
def test_nested_recording_change_invalidates_check(
    harness: Harness, grace_ms: int, probe: str, restart: bool
) -> None:
    guard = IndicatorGuard(
        cast("Pill", harness.pill), cast("Tray", harness.tray), grace_ms=grace_ms
    )
    guard.on_stop_recording = lambda: harness.events.append("stop")
    guard.set_recording(True)
    harness.clock.advance(grace_ms)
    harness.pill.available = False
    harness.tray.registered = False

    def finish_during_check() -> None:
        harness.pill.on_visible = harness.pill.on_forced = None
        harness.tray.on_registered = None
        guard.set_recording(False)
        harness.pill.available = False  # Вложенное hide() из заключения У54.
        if restart:
            harness.pill.available = True
            guard.set_recording(True)
            harness.pill.available = False

    if probe == "visible":
        harness.pill.on_visible = finish_during_check
    elif probe == "registered":
        harness.tray.on_registered = finish_during_check
    else:
        harness.pill.on_forced = finish_during_check
    assert not guard.check()
    assert guard.recording is restart
    harness.flush_notifications()
    assert harness.events == []
    if not restart:
        assert not harness.pill.forced
    else:
        # Следующий самостоятельный тик вправе остановить новый эпизод.
        harness.clock.advance(grace_ms)
        assert not guard.check()
        harness.flush_notifications()
        assert harness.events == ["stop", "notify"]
