"""О4 с настоящими Tray и IndicatorGuard, без настоящих окон, X11 и D-Bus."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast
from unittest.mock import Mock

import pytest
from test_indicators import FakePill, FakeTimer
from test_tray import OWN_ITEM, SERVICE, Harness
from test_tray import harness as harness
from test_tray import qapp as qapp

from astra_voice.platform.session import SessionKind
from astra_voice.ui import indicators, notify
from astra_voice.ui.indicators import IndicatorGuard
from astra_voice.ui.tray_icons import TrayState

if TYPE_CHECKING:
    from astra_voice.ui.pill import Pill

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def forbid_real_display(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    try:
        from Xlib import display
    except ImportError:
        yield
        return
    forbidden = Mock(side_effect=AssertionError("Real Xlib Display forbidden"))
    monkeypatch.setattr(display, "Display", forbidden)
    yield
    forbidden.assert_not_called()


@dataclass
class GuardHarness:
    guard: IndicatorGuard
    pill: FakePill
    timer: FakeTimer
    stopped: Mock
    lost: Mock
    pending: list[Callable[[], None]]


@pytest.fixture
def guard_harness(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> Iterator[GuardHarness]:
    timers: list[FakeTimer] = []
    pending: list[Callable[[], None]] = []

    def timer_factory(parent: object) -> FakeTimer:
        timer = FakeTimer(parent)
        timers.append(timer)
        return timer

    def single_shot(delay: int, callback: Callable[[], None]) -> None:
        assert delay == 0
        pending.append(callback)

    monkeypatch.setattr(
        indicators, "QTimer", Mock(side_effect=timer_factory, singleShot=single_shot)
    )
    monkeypatch.setattr(indicators, "monotonic", lambda: harness.clock.now / 1000)
    lost = Mock()
    monkeypatch.setattr(notify, "notify_indicators_lost", lost)
    pill = FakePill(enabled=False)
    guard = IndicatorGuard(cast("Pill", pill), harness.tray)
    stopped = Mock()
    guard.on_stop_recording = stopped
    assert len(timers) == 1
    yield GuardHarness(guard, pill, timers[0], stopped, lost, pending)
    guard.set_recording(False)


@pytest.mark.parametrize("loss", ["item", "watcher", "host"])
def test_kde_sni_loss_forces_disabled_pill_on_next_guard_tick(
    harness: Harness, guard_harness: GuardHarness, loss: str
) -> None:
    """FT-2/О4: живой владелец XEmbed не скрывает потерю индикации KDE."""
    h, g = harness, guard_harness
    h.detect.return_value = SessionKind.KDE
    h.probe.owner.return_value = 0xF00
    h.bus.host_registered = True
    h.bus.registered_items = [OWN_ITEM]
    h.tray.start()
    h.tray.set_state(TrayState.LISTENING)
    assert h.tray.registered
    g.guard.set_recording(True)
    assert not g.pill.visible
    assert not g.pill.forced
    h.clock.advance(1500)

    h.icon.show.reset_mock()
    if loss == "item":
        h.bus.item_unregistered(OWN_ITEM)
    else:
        h.bus.host_registered = False
        if loss == "watcher":
            h.watcher.serviceUnregistered.emit(SERVICE)
        else:
            h.tray._host_changed()
    assert not h.tray.registered
    assert not g.pill.visible
    if loss != "item":
        h.icon.show.assert_not_called()

    g.timer.fire()
    assert g.pill.forced
    assert g.pill.visible
    assert not g.pill.enabled
    assert g.guard.ok
    assert g.guard.recording
    assert g.timer.active
    h.clock.advance(1500)
    g.timer.fire()
    g.stopped.assert_not_called()
    g.lost.assert_not_called()
    assert g.pending == []
    if loss != "item":
        h.icon.show.assert_not_called()

    h.bus.host_registered = True
    h.bus.registered_items = [OWN_ITEM]
    if loss == "item":
        h.bus.item_registered(OWN_ITEM)
    elif loss == "watcher":
        h.watcher.serviceRegistered.emit(SERVICE)
    else:
        h.tray._host_changed()
    assert h.tray.registered
    g.timer.fire()
    assert not g.pill.forced
    assert not g.pill.visible
    assert g.guard.ok
    assert g.guard.recording
    g.stopped.assert_not_called()


@pytest.mark.parametrize("xembed_owner", [None, 0xF00], ids=["no-tray", "fly-xembed"])
def test_unconfirmed_fly_tray_forces_disabled_pill_without_stopping_recording(
    harness: Harness, guard_harness: GuardHarness, xembed_owner: int | None
) -> None:
    h, g = harness, guard_harness
    h.detect.return_value = SessionKind.FLY
    h.probe.owner.return_value = xembed_owner
    h.bus.host_registered = False
    h.bus.registered_items = []
    h.tray.start()
    if xembed_owner is not None:
        h.icon.show.assert_called()
        assert h.icon.isVisible()
    else:
        h.icon.show.assert_not_called()
    assert not h.tray.registered

    h.tray.set_state(TrayState.LISTENING)
    g.guard.set_recording(True)
    assert g.pill.forced
    assert g.pill.visible
    assert not g.pill.enabled
    h.clock.advance(1500)
    g.timer.fire()
    assert not h.tray.registered
    assert g.guard.ok
    assert g.guard.recording
    g.stopped.assert_not_called()
    g.lost.assert_not_called()
    assert g.pending == []
