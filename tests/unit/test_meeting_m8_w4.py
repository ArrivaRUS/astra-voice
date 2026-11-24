"""W4 call updates and their effects on W1 and W2."""

from __future__ import annotations

import logging

import pytest
from test_meeting_m8_offer import deliver, ended, offer_ready, show_offer, started
from test_meeting_overlay import finish, start
from test_meeting_recording_bridge import DETECTOR_START, Rig, capture_error, deliver_detector
from test_meeting_recording_bridge import rig as rig

from astra_voice.ui.meeting_texts import W2_TEXTS

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("filesystem_euid")]

QUERY = "meeting.call.query"
CALL = "call1"


def card_requests(rig: Rig) -> list[str]:
    return [message["req"] for message in rig.supervisor.messages if message["type"] == QUERY]


def inactive_card(rig: Rig, req: str) -> None:
    deliver(rig, {"type": "meeting.call.state", "request_type": QUERY, "req": req, "active": False})


@pytest.mark.parametrize(
    ("call_id", "selected", "expected"),
    [
        (CALL, "call", "room"),
        (CALL, "room", "room"),
        (CALL, "all", "all"),
        ("other-call", "call", "call"),
    ],
)
def test_w4_call_end_only_removes_matching_option(
    rig: Rig, call_id: str, selected: str, expected: str
) -> None:
    rig.bridge.requestRecording()
    rig.supervisor.event(rig.call_state())
    if selected != "call":
        rig.recording.select(selected)
    deliver(rig, ended(call_id, reason="app_gone"))
    assert rig.bridge.selectedSource == expected
    if call_id == CALL:
        assert [row["id"] for row in rig.bridge.startOptions] == ["room", "all"]
        assert rig.recording.call_id == "" and not rig.recording.variant1
    else:
        assert [row["id"] for row in rig.bridge.startOptions] == ["call", "room", "all"]
        assert rig.recording.call_id == CALL and rig.recording.variant1
    assert rig.bridge.startCanStart
    rig.bridge.on_start_clicked()
    far = {
        "room": {"kind": "none"},
        "all": {"kind": "sink_monitor"},
        "call": {"kind": "app_stream", "call_id": CALL},
    }
    assert rig.supervisor.messages[-1]["far"] == far[expected]


@pytest.mark.parametrize("selected", [None, "all", "room"])
def test_w4_call_start_refreshes_card_once_and_respects_explicit_choice(
    rig: Rig, selected: str | None
) -> None:
    rig.bridge.requestRecording()
    (first,) = card_requests(rig)
    inactive_card(rig, first)
    if selected is not None:
        rig.recording.select(selected)
    deliver(rig, started(CALL))
    assert card_requests(rig) == [first]
    assert rig.bridge.startCanStart
    rig.recording.tick()
    _, current = card_requests(rig)
    assert current != first
    assert rig.bridge.startCanStart
    rig.supervisor.event(rig.call_state(req=current))
    assert rig.recording.call_id == CALL and rig.recording.variant1
    assert rig.bridge.selectedSource == (selected or "call")
    for _ in range(2):
        rig.recording.tick()
    assert card_requests(rig) == [first, current]


def test_w4_ignores_old_card_even_before_tick_sends_refresh(rig: Rig) -> None:
    rig.bridge.requestRecording()
    (first,) = card_requests(rig)
    deliver(rig, started(CALL))
    rig.supervisor.event(rig.call_state("browser", req=first))
    assert rig.bridge.selectedSource == "room"
    assert not rig.recording.call_id and not rig.recording.variant1
    rig.recording.tick()
    _, current = card_requests(rig)
    rig.supervisor.event(rig.call_state(req=current))
    inactive_card(rig, first)
    assert rig.bridge.selectedSource == "call"
    assert rig.recording.call_id == CALL and rig.recording.variant1
    assert rig.recording.app_class == "dion"


@pytest.mark.parametrize("phase", ["preparing", "opening"])
def test_w4_fast_start_keeps_displayed_source_until_capture_starts(rig: Rig, phase: str) -> None:
    rig.bridge.requestRecording()
    (first,) = card_requests(rig)
    rig.now += 0.05
    rig.recording.tick()
    assert rig.bridge.startCanStart
    rig.bridge.on_start_clicked()
    assert rig.supervisor.messages[-1]["far"] == {"kind": "none"}
    if phase == "opening":
        rig.reply("prepared")
    rig.supervisor.event(rig.call_state(req=first))
    deliver(rig, started(CALL))
    rig.recording.tick()
    assert card_requests(rig) == [first]
    assert rig.bridge.startDialogOpen and rig.bridge.selectedSource == "room"
    assert not rig.recording.call_id and not rig.recording.variant1
    assert [row["id"] for row in rig.bridge.startOptions] == ["room", "all"]
    if phase == "preparing":
        rig.reply("prepared")
    rig.reply("started")
    meeting = rig.recording.current_meeting
    assert meeting is not None and meeting.variant == "mic_only"
    assert not rig.bridge.startDialogOpen


def test_w4_call_end_during_prepare_leaves_that_attempt_unchanged(rig: Rig) -> None:
    rig.bridge.requestRecording()
    rig.supervisor.event(rig.call_state())
    rig.bridge.on_start_clicked()
    deliver(rig, ended(CALL, reason="app_gone"))
    assert rig.bridge.startDialogOpen and rig.bridge.selectedSource == "call"
    assert rig.recording.call_id == CALL and rig.recording.variant1
    assert rig.supervisor.messages[-1]["far"] == {"kind": "app_stream", "call_id": CALL}


def test_w4_reopen_resets_call_and_explicit_choice(rig: Rig) -> None:
    offer_ready(rig)
    rig.bridge.requestRecording()
    rig.supervisor.event(rig.call_state())
    rig.recording.select("all")
    (first,) = card_requests(rig)
    rig.recording.close_dialog()
    rig.bridge.requestRecording()
    _, current = card_requests(rig)
    assert current != first
    rig.supervisor.event(rig.call_state(req=first))
    assert rig.bridge.selectedSource == "room"
    assert not rig.recording.call_id and not rig.recording.variant1
    rig.supervisor.event(rig.call_state(req=current))
    assert rig.bridge.selectedSource == "call"


def pause_then_lose_far(rig: Rig, reason: str, *, with_far: bool = True) -> None:
    start(rig, with_far=with_far)
    rig.now += 4
    if reason == "indicators":
        rig.recording.controller.pause("indicators")
    else:
        # The role detects the missing keepalive without a GUI pause request.
        assert reason == "lease"
        rig.reply(
            "autopaused",
            state_rev=2,
            at_recorded_ms=2000,
            pause_reasons=[reason],
            manual_resume_required=False,
            blocked_epoch=2,
            reason=reason,
        )
    rig.reply("closed", tracks=["mic", "far"] if with_far else ["mic"], state_rev=2)
    rig.reply(
        "finalized", segments={"mic": 0, "far": 0}, intervals=[], status="succeeded", state_rev=2
    )
    if reason == "indicators":
        rig.reply(
            "paused",
            state_rev=2,
            at_recorded_ms=2000,
            pause_reasons=[reason],
            manual_resume_required=False,
            blocked_epoch=2,
            segments_closed=0,
        )
    assert rig.bridge.overlay["state"] == "paused"
    rig.now += 14
    far_track(rig, "ended", "target-removed")
    assert rig.recording.controller.tracks["far"] == "ended"
    assert rig.bridge.overlay["state"] == "paused"
    assert rig.bridge.overlay["secondary"] == ""


def far_track(rig: Rig, state: str, kind: str, *, revision: int = 3) -> None:
    rig.reply("track", track="far", state=state, kind=kind, state_rev=revision, at_recorded_ms=2000)


def auto_resume(rig: Rig, reason: str, *, far: str = "ended", mic: str = "live") -> None:
    rig.reply(
        "alive",
        seq=1,
        state="recording",
        state_rev=3,
        recorded_ms=2000,
        tracks={"mic": mic, "far": far},
        free_mb=1000,
        cpu_pct=0,
        pause_reasons=[],
        manual_resume_required=False,
        blocked_epoch=2,
        recovery={"state_rev": 3, "reason": reason, "duration_ms": 14000},
    )


@pytest.mark.parametrize(
    ("reason", "restored", "state"),
    [
        ("indicators", "restoredIndicator", "reason-indicator"),
        ("lease", "restoredHang", "reason-hang"),
    ],
)
@pytest.mark.parametrize("with_far", [True, False], ids=["call", "mic-only"])
def test_w2_auto_resume_shows_recovery_then_full_far_warning(
    rig: Rig, reason: str, restored: str, state: str, with_far: bool
) -> None:
    pause_then_lose_far(rig, reason, with_far=with_far)
    # The role can deliver the far loss before the next alive publication.
    resumed_at = rig.now + 1
    rig.now = resumed_at
    auto_resume(rig, reason)
    recovery = W2_TEXTS[restored].format(duration="0:00:14")
    for elapsed in (0.0, 7.999):
        rig.now = resumed_at + elapsed
        rig.recording.tick()
        assert (rig.bridge.overlay["state"], rig.bridge.overlay["secondary"]) == (state, recovery)
    expected = ("app-closed", W2_TEXTS["farEnded"]) if with_far else ("recording", "")
    for elapsed in (8.0, 15.999):
        rig.now = resumed_at + elapsed
        rig.recording.tick()
        assert (rig.bridge.overlay["state"], rig.bridge.overlay["secondary"]) == expected
    rig.now = resumed_at + 16
    rig.recording.tick()
    assert rig.bridge.overlay["state"] == "recording" and rig.bridge.overlay["secondary"] == ""


@pytest.mark.parametrize("phase", ["pending", "shown"])
def test_w2_return_of_far_clears_warning(rig: Rig, phase: str) -> None:
    pause_then_lose_far(rig, "indicators")
    auto_resume(rig, "indicators")
    if phase == "shown":
        rig.now += 8
        rig.recording.tick()
        assert rig.bridge.overlay["state"] == "app-closed"
    far_track(rig, "live", "reopened")
    rig.now = 127
    rig.recording.tick()
    assert rig.bridge.overlay["state"] == "recording" and rig.bridge.overlay["secondary"] == ""


@pytest.mark.parametrize("blocked_for", [3, 8], ids=["shows-later", "expires-hidden"])
def test_w2_far_warning_yields_to_microphone_pause(rig: Rig, blocked_for: int) -> None:
    pause_then_lose_far(rig, "lease")
    auto_resume(rig, "lease")
    restored_at = rig.now
    rig.now = restored_at + 8
    rig.reply(
        "track", track="mic", state="paused", kind="device-lost", state_rev=3, at_recorded_ms=2000
    )
    assert rig.bridge.overlay["state"] == "mic-paused"
    rig.now += blocked_for
    rig.recording.tick()
    rig.reply("track", track="mic", state="live", kind="reopened", state_rev=3, at_recorded_ms=2000)
    if blocked_for < 8:
        assert rig.bridge.overlay["secondary"] == W2_TEXTS["farEnded"]
        rig.now += 7.999
        rig.recording.tick()
        assert rig.bridge.overlay["secondary"] == W2_TEXTS["farEnded"]
        rig.now += 0.002
        rig.recording.tick()
    assert rig.bridge.overlay["state"] == "recording" and rig.bridge.overlay["secondary"] == ""


def test_w2_stop_before_recovery_discards_loss_for_next_capture(rig: Rig) -> None:
    pause_then_lose_far(rig, "lease")
    assert rig.recording._overlay_far_pending
    rig.bridge.on_overlay_stop_clicked()
    assert not rig.recording._overlay_far_pending
    finish(rig)
    next_id = "2026-11-01_120100-abce"
    rig.recording.controller.id_factory = lambda: next_id
    rig.bridge.requestRecording()
    rig.supervisor.event(rig.call_state())
    rig.bridge.on_start_clicked()
    for event in ("prepared", "started"):
        rig.reply(event, meeting_id=next_id, capture_id="capture2")
    assert rig.recording.controller.capture_id == "capture2"
    assert rig.bridge.overlay["state"] == "recording"
    assert rig.bridge.overlay["secondary"] == ""


def offer_events(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [line for line in caplog.messages if line.startswith("meeting-offer ")]


def complete_w4_start(rig: Rig, *, tracks: list[str] | None = None) -> None:
    rig.bridge.on_start_clicked()
    rig.reply("prepared")
    rig.reply("started", tracks=tracks or ["mic"])
    assert rig.bridge.recording


@pytest.mark.parametrize(
    ("source", "variant1", "app_class", "reset"),
    [
        ("room", True, "dion", "app_gone"),
        ("call", True, "dion", "mic_released"),
        ("all", True, "dion", "clear_memory"),
        ("room", False, "browser", "app_gone"),
    ],
)
def test_w1_manual_start_suppresses_call_until_existing_memory_reset(
    rig: Rig,
    caplog: pytest.LogCaptureFixture,
    source: str,
    variant1: bool,
    app_class: str,
    reset: str,
) -> None:
    caplog.set_level(logging.INFO, logger="astra_voice.meetings")
    view = offer_ready(rig)
    rig.bridge.requestRecording()
    rig.supervisor.event({**rig.call_state(app_class), "variant1": variant1})
    rig.recording.select(source)
    complete_w4_start(rig, tracks=["mic"] if source == "room" else ["mic", "far"])
    finish(rig)
    detector = next(
        message for message in rig.supervisor.messages if message["type"] == DETECTOR_START
    )
    deliver_detector(rig.supervisor, detector, "meeting.detector.started")
    deliver(rig, started(CALL, measured=rig.recording.offer.now_ms(), app_class=app_class))
    rig.recording.tick()
    events = offer_events(caplog)
    accepted = "meeting-offer event=memory reason=remember-accepted shown_ms=0"
    suppressed = "meeting-offer event=suppressed reason=inherited shown_ms=0"
    assert accepted in events and suppressed in events
    assert events.index(accepted) < events.index(suppressed)
    assert not view.calls
    if reset == "clear_memory":
        rig.recording.offer.suspend(clear_memory=True)
    else:
        deliver(rig, ended(CALL, reason=reset))
    show_offer(rig, call_id="new-call", app_class=app_class)
    kind = "call-browser" if app_class == "browser" else "call-app"
    assert view.calls == [("show", kind, 60)]


@pytest.mark.parametrize("case", ["inactive-card", "failed-start", "call-ended-during-start"])
def test_w1_manual_start_only_remembers_active_call_after_success(
    rig: Rig, caplog: pytest.LogCaptureFixture, case: str
) -> None:
    caplog.set_level(logging.INFO, logger="astra_voice.meetings")
    offer_ready(rig)
    rig.bridge.requestRecording()
    (req,) = card_requests(rig)
    if case == "inactive-card":
        inactive_card(rig, req)
        complete_w4_start(rig)
    else:
        rig.supervisor.event(rig.call_state(req=req))
        if case == "failed-start":
            rig.bridge.on_start_clicked()
            deliver(rig, capture_error(rig, "prepare", "target-gone"))
            assert not rig.bridge.recording
        else:
            rig.recording.select("room")
            rig.bridge.on_start_clicked()
            deliver(rig, ended(CALL, reason="app_gone"))
            rig.reply("prepared")
            rig.reply("started")
            assert rig.bridge.recording
    assert not any("remember-accepted" in line for line in offer_events(caplog))
