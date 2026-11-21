"""W1 GUI life cycle with a fake view and the recording bridge's clock."""

from __future__ import annotations

import ast
import logging
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest
from test_meeting_logging import violations
from test_meeting_m8_role_ownership import command_types, leave, observe
from test_meeting_recording_bridge import (
    DETECTOR_START,
    Rig,
    deliver_detector,
    deliver_remote,
)
from test_meeting_recording_bridge import rig as rig

from astra_voice.core.settings import Settings
from astra_voice.ui import meeting_offer, meeting_recording
from astra_voice.worker import ipc
from astra_voice.worker.supervisor import WorkerSupervisor
from helpers.qt_app import get_qapplication

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("filesystem_euid")]
CALL = "w1-private-id"
SHOW = ("show", "call-app", 60)


class View:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    def show(self, kind: str, seconds: int) -> None:
        self.calls.append(("show", kind, seconds))

    def update_remaining(self, seconds: int) -> None:
        self.calls.append(("remaining", seconds))

    def close(self) -> None:
        self.calls.append(("close",))


def tick_at(rig: Rig, ms: int) -> None:
    rig.now = ms / 1000
    assert rig.recording.offer.now_ms() == ms
    rig.recording.tick()


def started(
    call_id: str = CALL,
    *,
    measured: int = 100_000,
    app_class: str = "dion",
    variant1: bool = True,
) -> dict[str, Any]:
    return {
        "type": "meeting.call.started",
        "call_id": call_id,
        "app_class": app_class,
        "variant1": variant1,
        "measured_mono_ms": measured,
    }


def ended(call_id: str = CALL, *, reason: str = "mic_released") -> dict[str, str]:
    return {"type": "meeting.call.ended", "call_id": call_id, "reason": reason}


def deliver(rig: Rig, event: dict[str, Any]) -> None:
    rig.supervisor.event({**event, "generation": rig.supervisor.generation})


def offer_ready(rig: Rig, *, allowed: bool = True) -> View:
    view = View()
    rig.recording.bind_offer(view)
    start = observe(rig)
    deliver_detector(rig.supervisor, start, "meeting.detector.started")
    assert rig.recording._offer_eligible()
    assert not rig.bridge.startDialogOpen and rig.recording._snapshot().allowed is allowed
    return view


def show_offer(rig: Rig, *, call_id: str = CALL, app_class: str = "dion") -> None:
    deliver(
        rig,
        started(call_id, measured=rig.recording.offer.now_ms(), app_class=app_class),
    )
    rig.recording.tick()
    assert rig.recording.offer.state == "shown_guard"


def memory(rig: Rig, app_class: str = "dion") -> str | None:
    entry = rig.recording.offer._memory.get(app_class)
    return entry.state if entry is not None else None


def replace_role(rig: Rig, *, start: bool = True) -> None:
    old = rig.supervisor
    leave(old)
    rig.now += 5
    rig.recording.tick()
    assert rig.supervisor is not old
    (command,) = rig.supervisor.messages
    assert command["type"] == DETECTOR_START
    if start:
        deliver_detector(rig.supervisor, command, "meeting.detector.started")


def real_supervisor(rig: Rig) -> WorkerSupervisor:
    role = WorkerSupervisor(
        rig.recording._event, role="meeting-capture", clock=lambda: rig.now, use_qt=False
    )
    role.state = "running"
    role.generation = rig.supervisor.generation
    hello = ipc.encode(ipc.make_hello(role=role.role), role=role.role)
    role._receive(hello, role.generation)
    return role


@pytest.mark.parametrize("measured", [98_999, 0, 100_001, 100])
def test_u1_rejects_invalid_age_at_receipt(rig: Rig, measured: int) -> None:
    view = offer_ready(rig)
    if measured == 0:
        # At this clock zero would pass the age test without the sentinel check.
        tick_at(rig, 500)
    deliver(rig, started(measured=measured))
    assert rig.recording.offer.state == "idle"
    rig.recording.tick()
    assert view.calls == []
    now = rig.recording.offer.now_ms()
    deliver(rig, started(measured=max(1, now - 1_000)))
    assert rig.recording.offer.state == "pending"
    rig.recording.tick()
    assert view.calls == [SHOW]


@pytest.mark.parametrize("age_at_show", [1001, -1])
def test_u1_rechecks_freshness_at_display(rig: Rig, age_at_show: int) -> None:
    view = offer_ready(rig)
    deliver(rig, started(measured=99_999))
    assert rig.recording.offer.state == "pending"
    # -1 exercises a future stamp at display with a deliberately reversed test clock.
    tick_at(rig, 99_999 + age_at_show)
    assert view.calls == []
    tick_at(rig, 100_200)
    deliver(rig, started(measured=100_000))
    tick_at(rig, 101_000)
    assert view.calls == [SHOW]


@pytest.mark.parametrize(("now", "measured"), [(500, 0), (100_000, 100)])
def test_u1_pending_stamp_is_checked_for_zero_and_wrong_units(
    rig: Rig, now: int, measured: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    view = offer_ready(rig)
    tick_at(rig, now)
    deliver(rig, started(measured=now))
    pending = rig.recording.offer._pending
    assert pending is not None
    # Only a test seam can inject these values after the receipt check.
    with monkeypatch.context() as patch:
        patch.setattr(pending, "measured_ms", measured)
        rig.recording.tick()
    assert view.calls == []
    show_offer(rig)
    assert view.calls == [SHOW]


@pytest.mark.parametrize("epoch", ["blocked_epoch", "sleep_epoch"])
def test_pending_offer_checks_each_session_epoch(rig: Rig, epoch: str) -> None:
    view = offer_ready(rig)
    deliver(rig, started())
    assert rig.recording.offer.state == "pending"
    allowed = rig.session.value
    rig.session.value = replace(
        allowed,
        blocked_epoch=3 if epoch == "blocked_epoch" else allowed.blocked_epoch,
        sleep_epoch=3 if epoch == "sleep_epoch" else allowed.sleep_epoch,
    )
    assert rig.session.value.allowed
    rig.recording.tick()
    assert view.calls == []
    show_offer(rig)
    assert view.calls == [SHOW]


@pytest.mark.parametrize(
    "gate",
    ["enabled", "policy_allowed", "_detector_allowed", "_standby_role", "_role_started"],
)
def test_u2_each_observation_gate_blocks_then_allows(
    rig: Rig, gate: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    view = offer_ready(rig)
    value = getattr(rig.recording, gate)
    with monkeypatch.context() as patch:
        patch.setattr(rig.recording, gate, (lambda: False) if callable(value) else False)
        deliver(rig, started())
        assert rig.recording.offer.state == "idle"
    rig.recording.tick()
    assert view.calls == []
    show_offer(rig)
    assert view.calls == [SHOW]


@pytest.mark.parametrize(
    "fields",
    [
        {"locked": True},
        {"known": False},
        {"active": False},
        {"lock_requested": True},
        {"preparing_for_sleep": True},
    ],
)
def test_u2_each_kde_session_gate_blocks_then_allows(rig: Rig, fields: dict[str, bool]) -> None:
    view = offer_ready(rig)
    allowed = rig.session.value
    rig.session.value = replace(allowed, **fields)
    deliver(rig, started())
    rig.recording.tick()
    assert view.calls == []
    rig.session.value = allowed
    rig.recording.tick()
    show_offer(rig)
    assert view.calls == [SHOW]


def test_u2_waits_for_detector_started_and_rejects_old_generation(rig: Rig) -> None:
    view = View()
    rig.recording.bind_offer(view)
    request = observe(rig)
    deliver(rig, started())
    rig.recording.tick()
    assert view.calls == []
    deliver_detector(rig.supervisor, request, "meeting.detector.started")
    rig.supervisor.event({**started(), "generation": rig.supervisor.generation - 1})
    rig.recording.tick()
    assert view.calls == []
    show_offer(rig)
    assert view.calls == [SHOW]


@pytest.mark.parametrize(
    "attribute", ["ready", "exited", "_retire_deadline", "_detector_generation"]
)
def test_u2_checks_live_role_at_receipt_and_at_display(
    rig: Rig, attribute: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    view = offer_ready(rig)
    obj = rig.recording if attribute.startswith("_") else rig.supervisor
    invalid: object = {"ready": False, "exited": True}.get(attribute, 99)
    with monkeypatch.context() as patch:
        patch.setattr(obj, attribute, invalid)
        deliver(rig, started())
        assert rig.recording.offer.state == "idle"
    deliver(rig, started())
    assert rig.recording.offer.state == "pending"
    # ready=False leaves the role available for the positive control afterwards.
    with monkeypatch.context() as patch:
        patch.setattr(rig.supervisor, "ready", False)
        rig.recording.tick()
    assert view.calls == []
    show_offer(rig)
    assert view.calls == [SHOW]


@pytest.mark.parametrize("conflict", ["w4", "recording"])
def test_u2_u5_w4_or_capture_suppresses_or_closes_w1(
    rig: Rig, conflict: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    view = offer_ready(rig)

    def occupy() -> None:
        if conflict == "w4":
            rig.bridge.requestRecording()
        else:
            # Exercise the defensive path independently of W4, which normally closes W1 first.
            rig.recording._sync_role_ownership()

    with monkeypatch.context() as patch:
        if conflict == "recording":
            patch.setattr(rig.recording.controller, "state", "recording")
        occupy()
        deliver(rig, started())
        assert rig.recording.offer.state == "idle"
        assert view.calls == [] and memory(rig) is None
    if conflict == "w4":
        rig.bridge.closeStartDialog()
    show_offer(rig)
    with monkeypatch.context() as patch:
        if conflict == "recording":
            patch.setattr(rig.recording.controller, "state", "recording")
        occupy()
        assert view.calls == [SHOW, ("close",)]
        assert memory(rig) == "shown"


def test_d17_open_w4_consumes_only_the_pending_offer_class(rig: Rig) -> None:
    view = offer_ready(rig)
    deliver(rig, started())
    assert rig.recording.offer.state == "pending"
    rig.bridge.requestRecording()
    assert view.calls == [] and memory(rig) == "shown"
    assert memory(rig, "browser") is None
    rig.bridge.closeStartDialog()
    deliver(rig, started())
    rig.recording.tick()
    assert view.calls == []
    show_offer(rig, call_id="browser-call", app_class="browser")
    assert view.calls == [("show", "call-browser", 60)]


def test_unbound_offer_has_no_phantom_window_or_memory(rig: Rig) -> None:
    request = observe(rig)
    deliver_detector(rig.supervisor, request, "meeting.detector.started")
    deliver(rig, started())
    rig.recording.tick()
    assert rig.recording.offer.state == "idle" and not rig.recording.offer._memory
    view = View()
    rig.recording.bind_offer(view)
    show_offer(rig)
    assert view.calls == [SHOW]


@pytest.mark.parametrize("variant1", [False, True])
@pytest.mark.parametrize(("app_class", "kind"), [("dion", "call-app"), ("browser", "call-browser")])
def test_u12_view_receives_only_whitelisted_values(
    rig: Rig,
    app_class: str,
    kind: str,
    variant1: bool,
    caplog: pytest.LogCaptureFixture,
) -> None:
    view = offer_ready(rig)
    with caplog.at_level(logging.DEBUG, logger="astra_voice.ui"):
        deliver(rig, started(app_class="other"))
        rig.recording.tick()
        assert view.calls == []
        deliver(rig, started(app_class=app_class, variant1=variant1))
        rig.recording.tick()
        tick_at(rig, 101_000)
        rig.recording.dismiss_offer()
    assert view.calls == [("show", kind, 60), ("remaining", 59), ("close",)]
    assert CALL not in repr(rig.recording.offer._memory)
    assert all(CALL not in entry.getMessage() for entry in caplog.records)


@pytest.mark.parametrize("second_before_show", [False, True])
def test_one_offer_at_a_time_across_classes(rig: Rig, second_before_show: bool) -> None:
    view = offer_ready(rig)
    deliver(rig, started())
    if not second_before_show:
        rig.recording.tick()
    deliver(rig, started("browser-call", app_class="browser"))
    rig.recording.tick()
    assert view.calls == [SHOW] and memory(rig, "browser") is None
    deliver(rig, ended())
    show_offer(rig, call_id="browser-call", app_class="browser")
    assert view.calls == [SHOW, ("close",), ("show", "call-browser", 60)]


@pytest.mark.parametrize("terminated_in_batch", [False, True])
def test_real_receive_defers_offer_after_a_whole_ipc_batch(
    rig: Rig, terminated_in_batch: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    view = offer_ready(rig)
    role = real_supervisor(rig)
    assert role.ready

    def encode(event: dict[str, Any]) -> bytes:
        return ipc.encode(event, role=role.role)

    frames = encode(started())
    if terminated_in_batch:
        frames += encode(ended())

    with monkeypatch.context() as patch:
        patch.setattr(role, "process", Mock(poll=Mock(return_value=None)))
        patch.setattr(rig.recording.controller, "supervisor", role)
        original_pump = role.pump
        pump = Mock(side_effect=lambda: role._receive(frames, role.generation))
        patch.setattr(role, "pump", pump)
        rig.recording.tick()
        pump.assert_called_once()
        assert view.calls == []
        patch.setattr(role, "pump", original_pump)
        rig.recording.tick()
        assert view.calls == ([] if terminated_in_batch else [SHOW])
        if terminated_in_batch:
            role._receive(encode(started("next-call")), role.generation)
            rig.recording.tick()
            assert view.calls == [SHOW]


@pytest.mark.parametrize(("action", "at"), [("refuse", 500), ("close", 600), ("wm", 100)])
def test_u5_explicit_refusals_suppress_same_call_until_it_ends(
    rig: Rig, action: str, at: int
) -> None:
    view = offer_ready(rig)
    show_offer(rig)
    tick_at(rig, 100_000 + at)
    if action == "wm":
        rig.recording.dismiss_offer()
    else:
        assert rig.recording.press_offer(action, 100_000 + at)
        rig.recording.tick()
    assert view.calls == [SHOW, ("close",)] and memory(rig) == "refused"
    deliver(rig, started())
    rig.recording.tick()
    assert not rig.recording.offer.visible
    deliver(rig, ended())
    assert memory(rig) is None
    show_offer(rig, call_id="next-call")


def test_u6_countdown_rounds_up_and_expires_sixty_seconds_after_display(rig: Rig) -> None:
    view = offer_ready(rig)
    deliver(rig, started())
    tick_at(rig, 100_300)
    assert view.calls == [SHOW]
    for now, seconds in [(100_301, 60), (117_800, 43), (118_300, 42), (160_299, 1)]:
        tick_at(rig, now)
        assert rig.recording.offer.presentation == ("call-app", seconds)
    assert view.calls == [SHOW, ("remaining", 43), ("remaining", 42), ("remaining", 1)]
    assert rig.recording.offer.visible
    tick_at(rig, 160_300)
    assert view.calls[-1] == ("close",) and memory(rig) == "refused"
    assert command_types(rig.supervisor) == [DETECTOR_START]
    deliver(rig, started(measured=160_300))
    rig.recording.tick()
    assert not rig.recording.offer.visible
    deliver(rig, ended())
    show_offer(rig, call_id="next-call")


def test_show_return_defines_the_clock_origin_for_guard_and_countdown(rig: Rig) -> None:
    class SlowView(View):
        def show(self, kind: str, seconds: int) -> None:
            rig.now = 100.4
            super().show(kind, seconds)

    offer_ready(rig)
    view = SlowView()
    rig.recording.bind_offer(view)
    show_offer(rig)
    tick_at(rig, 100_899)
    assert not rig.recording.press_offer("refuse", 100_899)
    tick_at(rig, 160_399)
    assert view.calls == [SHOW, ("remaining", 1)]
    tick_at(rig, 160_400)
    assert view.calls[-1] == ("close",)


@pytest.mark.parametrize("action", ["start", "refuse", "close"])
@pytest.mark.parametrize(("pressed", "released"), [(100, 100), (100, 600), (300, 600), (499, 499)])
def test_u7_early_press_is_ignored_and_not_deferred(
    rig: Rig, action: str, pressed: int, released: int
) -> None:
    view = offer_ready(rig)
    intent = Mock()
    rig.recording.openMeetingsRequested.connect(intent)
    deliver(rig, started())
    tick_at(rig, 100_300)
    tick_at(rig, 100_300 + released)
    assert not rig.recording.press_offer(action, 100_300 + pressed)
    tick_at(rig, 100_950)
    intent.assert_not_called()
    assert view.calls == [SHOW]
    assert not rig.bridge.startDialogOpen and memory(rig) == "shown"
    assert rig.recording.press_offer(action, 100_950)
    rig.recording.tick()
    assert view.calls[-1] == ("close",) and not rig.recording.offer.visible


@pytest.mark.parametrize("at", [500, 600])
def test_u7_start_at_guard_boundary_is_accepted(rig: Rig, at: int) -> None:
    view = offer_ready(rig)
    deliver(rig, started())
    tick_at(rig, 100_300)
    tick_at(rig, 100_300 + at)
    assert rig.recording.offer.state == "shown"
    assert rig.recording.press_offer("start", 100_300 + at)
    rig.recording.tick()
    assert view.calls == [SHOW, ("close",)]
    assert memory(rig) == "accepted" and rig.bridge.startDialogOpen


@pytest.mark.parametrize("app_stream_gate", [False, True])
def test_u9_w1_intent_opens_w4_without_capture(
    rig: Rig, app_stream_gate: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    view = offer_ready(rig)
    section = Mock()
    rig.bridge.openMeetingsRequested.connect(section)
    w4_at_emit: list[bool] = []
    rig.recording.openMeetingsRequested.connect(
        lambda: w4_at_emit.append(rig.bridge.startDialogOpen)
    )
    show_offer(rig, call_id="first-call")
    tick_at(rig, 150_000)
    tick_at(rig, 159_000)
    assert rig.recording.offer.visible and command_types(rig.supervisor) == [DETECTOR_START]
    deliver(rig, ended("first-call"))
    assert view.calls[-1] == ("close",)
    show_offer(rig)
    tick_at(rig, 159_600)
    with monkeypatch.context() as patch:
        patch.setattr(meeting_recording, "APP_STREAM_ENABLED", app_stream_gate)
        assert rig.recording.press_offer("start", 159_600)
        rig.recording.tick()
        assert rig.bridge.startDialogOpen
        assert [row["id"] for row in rig.bridge.startOptions] == ["room", "all"]
    section.assert_called_once_with()
    assert w4_at_emit == [False]
    assert command_types(rig.supervisor) == [DETECTOR_START, "meeting.call.query"]
    assert memory(rig) == "accepted"
    rig.bridge.closeStartDialog()
    deliver(rig, started(measured=159_600))
    rig.recording.tick()
    assert not rig.recording.offer.visible
    deliver(rig, ended())
    show_offer(rig, call_id="next-call")


@pytest.mark.parametrize("reason", ["mic_released", "app_gone"])
def test_u5_end_clears_whole_class_even_when_its_id_was_suppressed(rig: Rig, reason: str) -> None:
    view = offer_ready(rig)
    show_offer(rig)
    deliver(rig, started("same-class-other-id"))
    assert memory(rig) == "shown"
    deliver(rig, ended("same-class-other-id", reason=reason))
    assert view.calls == [SHOW, ("close",)]
    assert memory(rig) is None
    show_offer(rig, call_id="next-call")


@pytest.mark.parametrize("reason", ["server_changed", "evicted", "detector_off"])
def test_end_reasons_close_without_clearing_memory(rig: Rig, reason: str) -> None:
    view = offer_ready(rig)
    show_offer(rig)
    deliver(rig, ended("unknown-id", reason=reason))
    assert view.calls == [SHOW] and memory(rig) == "shown"
    if reason == "evicted":
        deliver(rig, started("other-id"))
        deliver(rig, ended("other-id", reason=reason))
        assert view.calls == [SHOW]
    deliver(rig, ended(reason=reason))
    assert view.calls == [SHOW, ("close",)] and memory(rig) == "shown"
    if reason == "server_changed":
        assert rig.recording.offer._memory["dion"].call_ids == set()
    show_offer(rig, call_id="fresh-browser", app_class="browser")


@pytest.mark.parametrize("reason", ["locked", "unknown", "sleep", "epoch"])
def test_session_invalidation_closes_visible_w1_without_refusing(rig: Rig, reason: str) -> None:
    view = offer_ready(rig)
    show_offer(rig)
    allowed = rig.session.value
    rig.session.value = replace(
        allowed,
        locked=reason == "locked",
        known=reason != "unknown",
        preparing_for_sleep=reason == "sleep",
        sleep_epoch=allowed.sleep_epoch + int(reason == "sleep"),
        blocked_epoch=allowed.blocked_epoch + int(reason in {"locked", "epoch"}),
    )
    rig.recording.tick()
    assert view.calls == [SHOW, ("close",)] and memory(rig) == "lock_closed"
    rig.session.value = replace(
        rig.session.value, locked=False, known=True, preparing_for_sleep=False
    )
    rig.recording.tick()
    deliver(rig, started("new-id-same-class"))
    rig.recording.tick()
    assert view.calls == [SHOW, ("close",)]
    show_offer(rig, call_id="fresh-browser", app_class="browser")


@pytest.mark.parametrize("gate", ["enabled", "policy_allowed", "_detector_allowed"])
def test_d18_revoke_closes_once_and_clears_memory(
    rig: Rig, gate: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    view = offer_ready(rig)
    show_offer(rig)
    with monkeypatch.context() as patch:
        patch.setattr(rig.recording, gate, lambda: False)
        rig.recording.tick()
        assert view.calls == [SHOW, ("close",)] and not rig.recording.offer._memory
        signature = rig.recording._signature
        for ms in range(100_050, 100_501, 50):
            tick_at(rig, ms)
        assert view.calls == [SHOW, ("close",)] and rig.recording._signature == signature
        leave(rig.supervisor)
    tick_at(rig, 101_000)
    deliver_detector(rig.supervisor, rig.supervisor.messages[-1], "meeting.detector.started")
    show_offer(rig)
    assert view.calls == [SHOW, ("close",), SHOW]


@pytest.mark.parametrize("old_end_first", [False, True])
def test_u11_inherits_across_role_exit_and_ignores_old_end_in_both_orders(
    rig: Rig, old_end_first: bool
) -> None:
    view = offer_ready(rig)
    show_offer(rig)
    old = rig.supervisor
    replace_role(rig)
    assert view.calls == [SHOW, ("close",)] and memory(rig) == "shown"
    fresh = started("new-role-id", measured=119_999)
    rig.now = 119.999
    if old_end_first:
        old.event({**ended(), "generation": old.generation})
    deliver(rig, fresh)
    if not old_end_first:
        old.event({**ended(), "generation": old.generation})
    rig.recording.tick()
    assert view.calls == [SHOW, ("close",)]
    assert rig.recording.offer._memory["dion"].call_ids == {"new-role-id"}
    deliver(rig, ended("new-role-id"))
    show_offer(rig, call_id="next-call")


@pytest.mark.parametrize("age", [14_999, 15_000])
def test_u11_inherits_from_new_detector_start_until_deadline(rig: Rig, age: int) -> None:
    view = offer_ready(rig)
    show_offer(rig)
    tick_at(rig, 100_500)
    assert rig.recording.press_offer("refuse", 100_500)
    rig.recording.tick()
    deliver(rig, ended(reason="detector_off"))
    assert memory(rig) == "refused"
    replace_role(rig, start=False)
    tick_at(rig, 110_000)
    deliver_detector(rig.supervisor, rig.supervisor.messages[-1], "meeting.detector.started")
    tick_at(rig, 110_000 + age)
    deliver(rig, started("replacement-id", measured=110_000 + age))
    rig.recording.tick()
    if age == 14_999:
        assert view.calls == [SHOW, ("close",)] and memory(rig) == "refused"
        deliver(rig, ended("replacement-id"))
        show_offer(rig, call_id="next-call")
    assert view.calls == [SHOW, ("close",), SHOW]


@pytest.mark.parametrize("failure", ["exit", "detector-error"])
def test_u11_replacement_without_detector_discards_inherited_memory(rig: Rig, failure: str) -> None:
    view = offer_ready(rig)
    show_offer(rig)
    replace_role(rig, start=False)
    assert memory(rig) == "shown"
    role = rig.supervisor
    if failure == "exit":
        leave(role)
    else:
        deliver_detector(role, role.messages[-1], "error", code="protocol-mismatch")
    assert memory(rig) is None
    if failure != "exit":
        leave(role)
    tick_at(rig, 120_000)
    deliver_detector(rig.supervisor, rig.supervisor.messages[-1], "meeting.detector.started")
    show_offer(rig)
    assert view.calls == [SHOW, ("close",), SHOW]


def test_d20_direct_role_replacement_closes_view_and_keeps_memory(rig: Rig) -> None:
    view = offer_ready(rig)
    show_offer(rig)
    old = rig.supervisor
    # Exercise replacement directly as well as the normal _exit-driven replacement.
    new = rig.recording._new_supervisor()
    assert new is not old
    assert view.calls == [SHOW, ("close",)] and memory(rig) == "shown"


def test_d21_remote_closes_w1_and_latch_survives_manual_role_replacement(rig: Rig) -> None:
    view = offer_ready(rig)
    show_offer(rig)
    old = rig.supervisor
    deliver_remote(old, stale=True)
    assert view.calls == [SHOW]
    deliver_remote(old)
    assert view.calls == [SHOW, ("close",)]
    leave(old)
    rig.bridge.requestRecording()
    assert rig.supervisor is not old and rig.bridge.startCanStart
    rig.bridge.closeStartDialog()
    deliver(rig, started(measured=rig.recording.offer.now_ms()))
    rig.recording.tick()
    assert view.calls == [SHOW, ("close",)] and rig.recording._automation_off


def test_show_and_close_resynchronize_role_ownership(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    view = offer_ready(rig)
    states: list[bool] = []
    original = rig.recording._sync_role_ownership

    def sync() -> None:
        states.append(rig.recording.offer.visible)
        original()

    with monkeypatch.context() as patch:
        patch.setattr(rig.recording, "_sync_role_ownership", sync)
        show_offer(rig)
        assert states == [False, True]
        states.clear()
        deliver(rig, ended())
        assert states == [False]
    assert view.calls == [SHOW, ("close",)]


def test_a7_offer_is_independent_of_capture_indicators(tmp_path: Path) -> None:
    get_qapplication()
    instance = Rig(tmp_path, settings=Settings(meetings_enabled=True, pill_enabled=False))
    try:
        instance.recording.bind_indicators(
            overlay_visible=lambda: False, tray_visible=lambda: False
        )
        view = offer_ready(instance)
        show_offer(instance)
        assert view.calls == [SHOW]
    finally:
        instance.bridge.shutdown()


def test_a8_only_the_guarded_press_signals_intent_and_offer_is_passive() -> None:
    assert meeting_offer.__file__ is not None
    source = Path(meeting_offer.__file__).read_text(encoding="utf-8")
    assert not violations(source)
    tree = ast.parse(source)
    controller = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "OfferController"
    )
    senders = [
        method.name
        for method in controller.body
        if isinstance(method, ast.FunctionDef)
        and any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "startRequested"
            for node in ast.walk(method)
        )
    ]
    assert senders == ["_apply_press"]
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            assert not any(
                part in ast.unparse(node)
                for part in ("PyQt", "threading", "time", "socket", "cowork", "dbus")
            )
        if isinstance(node, ast.Call):
            assert not any(
                part in ast.unparse(node.func)
                for part in ("QTimer", "monotonic", "time.time", "register", "prepare")
            )


def test_offer_received_while_locked_cannot_show_after_unlock_in_same_epoch(rig: Rig) -> None:
    view = offer_ready(rig)
    allowed = rig.session.value
    rig.session.value = replace(allowed, locked=True)
    deliver(rig, started())
    assert rig.recording.offer.state == "idle"
    rig.session.value = allowed
    rig.recording.tick()
    assert view.calls == []
    show_offer(rig)
    assert view.calls == [SHOW]


def test_press_checks_monitor_before_queued_lock_notification(rig: Rig) -> None:
    view = offer_ready(rig)
    show_offer(rig)
    tick_at(rig, 100_600)
    allowed = rig.session.value
    rig.session.value = replace(allowed, locked=True, blocked_epoch=allowed.blocked_epoch + 1)
    assert not rig.recording.press_offer("start", 100_600)
    assert view.calls == [SHOW, ("close",)] and memory(rig) == "lock_closed"
    rig.recording.tick()
    assert not rig.bridge.startDialogOpen

    rig.session.value = replace(rig.session.value, locked=False)
    show_offer(rig, app_class="browser", call_id="next-browser")
    tick_at(rig, 101_200)
    assert rig.recording.press_offer("start", 101_200)
    rig.recording.tick()
    assert rig.bridge.startDialogOpen


def test_a8_session_signal_cannot_emit_w1_intent(rig: Rig) -> None:
    view = offer_ready(rig)
    intent = Mock()
    rig.recording.openMeetingsRequested.connect(intent)
    show_offer(rig)
    tick_at(rig, 100_600)
    rig.session.changed.emit(rig.session.value)
    rig.recording.tick()
    intent.assert_not_called()
    assert view.calls == [SHOW] and not rig.bridge.startDialogOpen

    assert rig.recording.press_offer("start", 100_600)
    rig.recording.tick()
    intent.assert_called_once_with()
    assert rig.bridge.startDialogOpen


def test_evicted_candidate_is_discarded_before_display(rig: Rig) -> None:
    view = offer_ready(rig)
    deliver(rig, started())
    assert rig.recording.offer.state == "pending"
    deliver(rig, ended(reason="evicted"))
    rig.recording.tick()
    assert view.calls == []
    show_offer(rig, call_id="live-call")
    assert view.calls == [SHOW]


def test_independent_browser_offer_is_allowed_after_refusing_another_offer(rig: Rig) -> None:
    view = offer_ready(rig)
    show_offer(rig, app_class="browser", call_id="first-browser")
    tick_at(rig, 100_500)
    assert rig.recording.press_offer("refuse", 100_500)
    rig.recording.tick()
    assert memory(rig, "browser") == "refused"
    browser_show = ("show", "call-browser", 60)
    deliver(rig, started("first-browser", app_class="browser", measured=100_500))
    rig.recording.tick()
    assert view.calls == [browser_show, ("close",)]

    show_offer(rig, app_class="browser", call_id="second-browser")
    assert view.calls == [browser_show, ("close",), browser_show]


def test_receipt_at_inheritance_deadline_leaves_memory_until_tick(rig: Rig) -> None:
    view = offer_ready(rig)
    show_offer(rig)
    tick_at(rig, 100_500)
    assert rig.recording.press_offer("refuse", 100_500)
    rig.recording.tick()
    replace_role(rig)
    deadline = rig.recording.offer.now_ms() + 15_000
    tick_at(rig, deadline - 1)
    entry = rig.recording.offer._memory["dion"]
    assert entry.state == "refused" and entry.deadline == deadline

    rig.now = deadline / 1000
    deliver(rig, started("replacement-id", measured=deadline))
    assert rig.recording.offer.state == "idle"
    assert rig.recording.offer._memory["dion"] is entry and entry.deadline == deadline
    assert view.calls == [SHOW, ("close",)]
    rig.recording.tick()
    assert view.calls == [SHOW, ("close",), SHOW]
    assert memory(rig) == "shown"


@pytest.mark.parametrize("pressed_ms", [499, 500])
def test_press_at_guard_edge_is_decided_on_next_tick(rig: Rig, pressed_ms: int) -> None:
    view = offer_ready(rig)
    intent = Mock()
    rig.recording.openMeetingsRequested.connect(intent)
    show_offer(rig)
    rig.now = 100.500
    assert rig.recording.offer.state == "shown_guard"
    admitted = pressed_ms == 500
    assert rig.recording.press_offer("start", 100_000 + pressed_ms) is admitted
    assert rig.recording.offer.state == "shown_guard"
    assert view.calls == [SHOW] and not rig.bridge.startDialogOpen
    intent.assert_not_called()

    tick_at(rig, 100_550)
    assert rig.bridge.startDialogOpen is admitted
    assert intent.call_count == int(admitted)
    assert view.calls == ([SHOW, ("close",)] if admitted else [SHOW])
    assert memory(rig) == ("accepted" if admitted else "shown")


def test_queued_press_is_discarded_if_role_is_unready_on_next_tick(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    view = offer_ready(rig)
    show_offer(rig)
    rig.now = 100.500
    assert rig.recording.press_offer("start", 100_500)
    with monkeypatch.context() as patch:
        patch.setattr(rig.supervisor, "ready", False)
        tick_at(rig, 100_550)
    tick_at(rig, 100_600)
    assert view.calls == [SHOW] and not rig.bridge.startDialogOpen
    assert rig.recording.press_offer("start", 100_600)
    rig.recording.tick()
    assert rig.bridge.startDialogOpen
