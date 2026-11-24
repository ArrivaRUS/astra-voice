"""W4 call updates and their effects on W1 and W2."""

from __future__ import annotations

import logging
from dataclasses import replace

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
    assert rig.bridge.selectedSource == (selected or "room")
    for _ in range(2):
        rig.recording.tick()
    assert card_requests(rig) == [first, current]


@pytest.mark.parametrize(
    ("call_id", "app_class", "expected"),
    [(CALL, "dion", "call"), ("new-call", "dion", "room"), (CALL, "browser", "room")],
)
@pytest.mark.parametrize("explicit", [False, True])
def test_w4_refresh_keeps_call_selected_only_for_same_target(
    rig: Rig, call_id: str, app_class: str, expected: str, explicit: bool
) -> None:
    rig.bridge.requestRecording()
    rig.supervisor.event(rig.call_state())
    assert rig.bridge.selectedSource == "call"
    if explicit:
        rig.bridge.selectRecordingSource("call")
    deliver(rig, started(call_id, app_class=app_class))
    rig.recording.tick()
    rig.supervisor.event({**rig.call_state(app_class), "call_id": call_id})
    assert rig.recording.call_id == call_id and rig.recording.app_class == app_class
    assert rig.recording.variant1
    assert rig.bridge.selectedSource == expected
    if expected == "room":
        # The replacement is available and can be deliberately selected.
        assert [item["id"] for item in rig.bridge.startOptions] == ["call", "room", "all"]
        rig.bridge.selectRecordingSource("call")
    rig.bridge.on_start_clicked()
    assert rig.supervisor.messages[-1]["far"] == {"kind": "app_stream", "call_id": call_id}


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
@pytest.mark.parametrize("source", ["room", "call"])
def test_w4_fast_start_keeps_displayed_source_until_capture_starts(
    rig: Rig, phase: str, source: str
) -> None:
    rig.bridge.requestRecording()
    (first,) = card_requests(rig)
    with_call = source == "call"
    if with_call:
        rig.supervisor.event(rig.call_state(req=first))
        deliver(rig, started(CALL))
    rig.now += 0.05
    rig.recording.tick()
    requests = card_requests(rig)
    assert len(requests) == (2 if with_call else 1)
    assert rig.bridge.selectedSource == source
    assert rig.bridge.startCanStart
    rig.bridge.on_start_clicked()
    far = {"kind": "app_stream", "call_id": CALL} if with_call else {"kind": "none"}
    assert rig.supervisor.messages[-1]["far"] == far
    if phase == "opening":
        rig.reply("prepared")
    if with_call:
        inactive_card(rig, requests[-1])
    else:
        rig.supervisor.event(rig.call_state(req=first))
    deliver(rig, started(CALL))
    rig.recording.tick()
    assert card_requests(rig) == requests
    assert rig.bridge.startDialogOpen and rig.bridge.selectedSource == source
    assert rig.recording.call_id == (CALL if with_call else "")
    assert rig.recording.variant1 == with_call
    assert [row["id"] for row in rig.bridge.startOptions] == (
        ["call", "room", "all"] if with_call else ["room", "all"]
    )
    if phase == "preparing":
        rig.reply("prepared")
    rig.reply("started", tracks=["mic", "far"] if with_call else ["mic"])
    meeting = rig.recording.current_meeting
    assert meeting is not None and meeting.variant == ("app_stream" if with_call else "mic_only")
    assert not rig.bridge.startDialogOpen


@pytest.mark.parametrize("reason", ["evicted", "server_changed"])
def test_w4_matching_call_end_revokes_pending_card_and_refreshes(rig: Rig, reason: str) -> None:
    rig.bridge.requestRecording()
    rig.supervisor.event(rig.call_state())
    deliver(rig, started(CALL))
    rig.recording.tick()
    first, revoked = card_requests(rig)
    deliver(rig, ended(CALL, reason=reason))
    rig.supervisor.event(rig.call_state(req=revoked))
    assert (rig.bridge.selectedSource, rig.recording.call_id) == ("room", "")
    rig.recording.tick()
    requests = card_requests(rig)
    assert len(requests) == 3 and requests[:2] == [first, revoked]
    _, _, current = requests
    assert current != revoked
    rig.supervisor.event({**rig.call_state(req=current), "call_id": "new-call"})
    assert (rig.bridge.selectedSource, rig.recording.call_id) == ("room", "new-call")
    rig.recording.tick()
    assert card_requests(rig) == requests


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


@pytest.mark.parametrize("selected", [None, "room", "all"])
def test_w4_refresh_after_failed_preflight_renews_first_card_only(
    rig: Rig, selected: str | None
) -> None:
    rig.bridge.requestRecording()
    (first,) = card_requests(rig)
    inactive_card(rig, first)
    if selected is not None:
        rig.bridge.selectRecordingSource(selected)
    deliver(rig, started(CALL))
    rig.recording.tick()
    _, revoked = card_requests(rig)
    rig.storage = replace(rig.storage, free_mb=599)
    rig.bridge.on_start_clicked()
    rig.supervisor.event(rig.call_state(req=revoked))
    rig.recording.tick()
    assert rig.bridge.startState == "error-disk"
    rig.storage = replace(rig.storage, free_mb=1000)
    rig.bridge.retryStart()
    requests = card_requests(rig)
    assert len(requests) == 3 and requests[:2] == [first, revoked]
    current = requests[-1]
    assert current != revoked and rig.bridge.startCanStart
    rig.supervisor.event(rig.call_state("browser", req=revoked))
    assert rig.bridge.selectedSource == (selected or "room") and not rig.recording.call_id
    rig.supervisor.event(rig.call_state(req=current))
    assert rig.bridge.selectedSource == (selected or "call")
    rig.recording.tick()
    assert card_requests(rig) == requests


def test_w4_idle_after_unexpected_refusal_accepts_fresh_card(rig: Rig) -> None:
    rig.bridge.requestRecording()
    (first,) = card_requests(rig)
    rig.bridge.on_start_clicked()
    assert rig.supervisor.messages[-1]["far"] == {"kind": "none"}
    deliver(rig, capture_error(rig, "prepare", "target-mismatch"))
    assert rig.recording.controller.state == "idle"
    assert rig.bridge.startCanStart
    rig.recording.tick()
    _, current = card_requests(rig)
    assert current != first
    rig.supervisor.event(rig.call_state(req=current))
    assert rig.bridge.selectedSource == "call" and rig.recording.call_id == CALL


def automatic_pause(rig: Rig, reason: str, *, with_far: bool = True, revision: int = 2) -> None:
    rig.now += 4
    if reason == "indicators":
        rig.recording.controller.pause("indicators")
    else:
        # The role detects the missing keepalive without a GUI pause request.
        assert reason == "lease"
        rig.reply(
            "autopaused",
            state_rev=revision,
            at_recorded_ms=2000,
            pause_reasons=[reason],
            manual_resume_required=False,
            blocked_epoch=2,
            reason=reason,
        )
    rig.reply("closed", tracks=["mic", "far"] if with_far else ["mic"], state_rev=revision)
    rig.reply(
        "finalized",
        segments={"mic": 0, "far": 0},
        intervals=[],
        status="succeeded",
        state_rev=revision,
    )
    if reason == "indicators":
        rig.reply(
            "paused",
            state_rev=revision,
            at_recorded_ms=2000,
            pause_reasons=[reason],
            manual_resume_required=False,
            blocked_epoch=2,
            segments_closed=0,
        )
    assert rig.bridge.overlay["state"] == "paused"
    rig.now += 14


def pause_then_lose_far(rig: Rig, reason: str, *, with_far: bool = True) -> None:
    start(rig, with_far=with_far)
    automatic_pause(rig, reason, with_far=with_far)
    far_track(rig, "ended", "target-removed")
    assert rig.recording.controller.tracks["far"] == "ended"
    assert rig.bridge.overlay["state"] == "paused"
    assert rig.bridge.overlay["secondary"] == ""


def far_track(rig: Rig, state: str, kind: str, *, revision: int = 3) -> None:
    rig.reply("track", track="far", state=state, kind=kind, state_rev=revision, at_recorded_ms=2000)


def auto_resume(
    rig: Rig, reason: str, *, far: str = "ended", revision: int = 3, recorded_ms: int = 2000
) -> None:
    rig.reply(
        "alive",
        seq=revision,
        state="recording",
        state_rev=revision,
        recorded_ms=recorded_ms,
        tracks={"mic": "live", "far": far},
        free_mb=1000,
        cpu_pct=0,
        pause_reasons=[],
        manual_resume_required=False,
        blocked_epoch=2,
        recovery={"state_rev": revision, "reason": reason, "duration_ms": 14000},
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


def assert_full_far_warning(rig: Rig) -> None:
    shown_at = rig.now
    for elapsed in (0.0, 7.999):
        rig.now = shown_at + elapsed
        rig.recording.tick()
        assert rig.bridge.overlay["secondary"] == W2_TEXTS["farEnded"]
    rig.now = shown_at + 8
    rig.recording.tick()
    assert rig.bridge.overlay["state"] == "recording" and rig.bridge.overlay["secondary"] == ""


def test_w2_full_far_warning_after_extending_limit(rig: Rig) -> None:
    pause_then_lose_far(rig, "lease")
    auto_resume(rig, "lease", recorded_ms=6_910_000)
    rig.now += 8
    rig.recording.tick()
    assert rig.bridge.overlay["state"] == "limit-soon"
    rig.now += 3
    rig.recording.tick()
    assert rig.bridge.overlay["state"] == "limit-soon"
    rig.bridge.on_overlay_extend_clicked()
    assert rig.supervisor.messages[-1]["type"] == "meeting.capture.extend"
    assert_full_far_warning(rig)


def test_w2_full_far_warning_for_loss_during_recovery_text(rig: Rig) -> None:
    start(rig, with_far=True)
    automatic_pause(rig, "indicators")
    auto_resume(rig, "indicators", far="live")
    rig.now += 2
    far_track(rig, "ended", "target-removed")
    assert rig.bridge.overlay["state"] == "reason-indicator"
    rig.now += 6
    rig.recording.tick()
    assert_full_far_warning(rig)


def test_w2_auto_resume_does_not_repeat_already_shown_loss(rig: Rig) -> None:
    start(rig, with_far=True)
    far_track(rig, "ended", "target-removed", revision=2)
    assert_full_far_warning(rig)
    automatic_pause(rig, "lease", revision=3)
    rig.reply(
        "alive",
        seq=1,
        state="paused",
        state_rev=3,
        recorded_ms=2000,
        tracks={"mic": "paused", "far": "ended"},
        free_mb=1000,
        cpu_pct=0,
        pause_reasons=["lease"],
        manual_resume_required=False,
        blocked_epoch=2,
    )
    auto_resume(rig, "lease", revision=4)
    assert rig.bridge.overlay["state"] == "reason-hang"
    rig.now += 8
    rig.recording.tick()
    assert rig.bridge.overlay["state"] == "recording" and rig.bridge.overlay["secondary"] == ""
    far_track(rig, "live", "reopened", revision=4)
    far_track(rig, "ended", "target-removed", revision=4)
    assert_full_far_warning(rig)


def test_w2_stop_before_recovery_discards_loss_for_next_capture(rig: Rig) -> None:
    pause_then_lose_far(rig, "lease")
    assert rig.recording._overlay_far_pending
    rig.bridge.on_overlay_stop_clicked()
    assert not rig.recording._overlay_far_pending
    finish(rig)
    assert rig.recording.controller.saved
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


def test_w2_capture_id_alone_invalidates_previous_notice(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    start(rig)
    rig.recording.show_notify_fallback("Встреча сохранена.")
    assert rig.bridge.overlay["state"] == "notify-fallback"
    controller = rig.recording.controller
    meeting = controller.meeting
    assert meeting is not None and controller.capture_id == "capture1"
    # Isolate the capture key from the meeting identifier and the overlay deadline.
    with monkeypatch.context() as patch:
        patch.setitem(controller._identity, "capture_id", "capture2")
        rig.recording.tick()
        assert controller.meeting is meeting and controller.capture_id == "capture2"
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
    assert rig.recording.controller.saved
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


def test_w1_manual_start_refused_at_open_leaves_call_eligible(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="astra_voice.meetings")
    view = offer_ready(rig)
    rig.bridge.requestRecording()
    rig.supervisor.event(rig.call_state())
    rig.bridge.selectRecordingSource("room")
    rig.bridge.on_start_clicked()
    rig.reply("prepared")
    deliver(rig, capture_error(rig, "open", "audio-failed"))
    assert not rig.bridge.recording and rig.bridge.startState == "error-audio"
    assert not any("remember-accepted" in line for line in offer_events(caplog))
    rig.bridge.closeStartDialog()
    rig.now += 5
    rig.recording.tick()
    probe = next(
        message for message in rig.supervisor.messages if message["type"] == DETECTOR_START
    )
    deliver_detector(rig.supervisor, probe, "meeting.detector.started")
    show_offer(rig, call_id=CALL)
    assert view.calls == [("show", "call-app", 60)]
