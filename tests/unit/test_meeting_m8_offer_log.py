"""W1 log evidence collected through the application's meeting log filter."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import replace
from unittest.mock import Mock

import pytest
from PyQt5.QtCore import QEvent, QRect
from test_meeting_m8_offer import (
    CALL,
    View,
    deliver,
    ended,
    offer_ready,
    replace_role,
    show_offer,
    start_observing,
    started,
    tick_at,
)
from test_meeting_m8_offer_card import QUERY, query_after_unlock, reply, sleep_or_lock
from test_meeting_m8_offer_start import lock, press_start
from test_meeting_m8_offer_view_qml import DelayedView, expose, surface, swap_frame
from test_meeting_m8_role_ownership import leave
from test_meeting_recording_bridge import Rig, capture_error, deliver_detector, deliver_remote
from test_meeting_recording_bridge import rig as rig

from astra_voice.core.logging import RedactTextFilter
from astra_voice.meetings.events import INVALID_LOG, OFFER_LIFECYCLE, OfferEvent, OfferReason
from astra_voice.ui.meeting_offer import Origin

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("filesystem_euid")]
LOGGER = "astra_voice.meetings"


class ManualFramesView(DelayedView):
    def event(self, event: QEvent) -> bool:
        if event.type() == QEvent.Expose:
            # Deliver Expose to W1's event filter without starting the renderer.
            return True
        return super().event(event)


@pytest.fixture
def trace(caplog: pytest.LogCaptureFixture) -> Iterator[list[str]]:
    lines: list[str] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            lines.append(record.getMessage())

    logger = logging.getLogger(LOGGER)
    handler = Capture()
    handler.addFilter(RedactTextFilter())
    with caplog.at_level(logging.INFO, logger=LOGGER):
        logger.addHandler(handler)
        try:
            yield lines
        finally:
            logger.removeHandler(handler)
            handler.close()


def line(event: str, reason: str, shown_ms: int = 0) -> str:
    return f"meeting-offer event={event} reason={reason} shown_ms={shown_ms}"


def receive(
    rig: Rig,
    *,
    call_id: str = CALL,
    origin: Origin = "offer",
    eligible: bool = True,
    app_class: str = "dion",
) -> None:
    offer = rig.recording.offer
    offer.receive(
        call_id=call_id,
        app_class=app_class,
        measured_ms=offer.now_ms(),
        origin=origin,
        eligible=eligible,
        session=rig.session.value,
    )


def refuse(rig: Rig) -> None:
    now = rig.recording.offer.now_ms() + 500
    tick_at(rig, now)
    assert rig.recording.press_offer("refuse", now)
    rig.recording.tick()


def receipt_case(rig: Rig, case: str, trace: list[str]) -> None:
    if case == "no-role":
        offer_ready(rig)
        leave(rig.supervisor)
        trace.clear()
        receive(rig)
        return
    if case == "unbound":
        start_observing(rig)
    else:
        offer_ready(rig)
    if case == "session-blocked":
        rig.session.value = replace(rig.session.value, locked=True)
    elif case == "pending":
        deliver(rig, started())
    elif case in {"visible", "starting"}:
        show_offer(rig)
        if case == "starting":
            rig.storage = replace(rig.storage, free_mb=599)
            press_start(rig)
            assert rig.recording.offer.state == "start-failed"
    elif case == "call-ended":
        deliver(rig, ended())

    trace.clear()
    if case == "unknown-class":
        receive(rig, app_class="private-unknown-class")
    elif case == "not-eligible":
        receive(rig, eligible=False)
    else:
        measured = rig.recording.offer.now_ms() - (1001 if case == "stale" else 0)
        deliver(rig, started(measured=measured))


def pending_case(rig: Rig, case: str, trace: list[str], patch: pytest.MonkeyPatch) -> None:
    view = offer_ready(rig)
    offer = rig.recording.offer
    if case == "deferred-suppressed":
        show_offer(rig)
        refuse(rig)
        replace_role(rig)
        rig.now += 15
        deliver(rig, started("new-role-call", measured=offer.now_ms() - 500))
        # A forward monotonic clock expires the entry before the recheck.
        # A backwards test clock makes the defensive recheck observable.
        rig.now -= 0.25
    else:
        deliver(rig, started())
    snapshot = rig.session.value
    if case == "session-blocked":
        snapshot = replace(snapshot, locked=True)
    elif case == "epoch-changed":
        snapshot = replace(snapshot, sleep_epoch=snapshot.sleep_epoch + 1)
    elif case == "stale":
        rig.now += 1.1
    elif case == "show-failed":
        patch.setattr(view, "show", Mock(side_effect=RuntimeError("private-view-error")))
    trace.clear()
    offer.begin_tick()
    offer.tick(eligible=case != "not-eligible", session=snapshot)
    assert not offer.visible


def ipc_case(rig: Rig, case: str, trace: list[str]) -> None:
    offer_ready(rig)
    if case == "old-serial":
        old = rig.supervisor
        replace_role(rig)
        trace.clear()
        role_events = [started(), ended(), reply(rig, "old-card-req")]
        for event in role_events:
            old.event({**event, "generation": old.generation})
        assert trace == [line("dropped", "old-serial")] * 2
        trace.clear()
        old.event({"type": "meeting.capture.alive", "generation": old.generation})
        assert trace == []
        old.event({**started(), "generation": old.generation})
    elif case == "generation":
        req = query_after_unlock(rig)
        trace.clear()
        for event in (ended(), reply(rig, req), reply(rig, "other-req")):
            rig.supervisor.event({**event, "generation": rig.supervisor.generation - 1})
        assert trace == [line("dropped", "generation")] * 2
        trace.clear()
        rig.supervisor.event({**started(), "generation": rig.supervisor.generation - 1})
    elif case == "decode":
        req = query_after_unlock(rig)
        trace.clear()
        deliver(rig, {"type": "error", "request_type": QUERY, "req": ""})
        assert trace == []
        deliver(rig, {"type": "error", "request_type": QUERY, "req": req})
        assert trace == [line("dropped", "decode")]
        trace.clear()
        deliver(rig, {"type": "meeting.call.started"})
    elif case == "unknown-call":
        deliver(rig, ended("unknown-call"))


def suppressed_case(rig: Rig, case: str, trace: list[str]) -> None:
    view = offer_ready(rig)
    if case == "card-origin":
        deliver(rig, reply(rig, query_after_unlock(rig)))
        rig.recording.tick()
    else:
        show_offer(rig)
    if case in {"same-call", "lock-closed"}:
        rig.session.value = sleep_or_lock(rig, "locked")
    else:
        refuse(rig)
    if case == "inherited":
        replace_role(rig)
    req = query_after_unlock(rig) if case == "card-memory" else ""
    before = list(view.calls)
    trace.clear()
    if req:
        deliver(rig, reply(rig, req))
    else:
        call_id = CALL if case == "same-call" else "second-call"
        deliver(rig, started(call_id, measured=rig.recording.offer.now_ms()))
    rig.recording.tick()
    assert view.calls == before


def cancel_case(
    rig: Rig, case: str, trace: list[str], patch: pytest.MonkeyPatch, *, pending: bool
) -> None:
    view = offer_ready(rig)
    if pending:
        deliver(rig, started())
        assert rig.recording.offer.state == "pending"
    else:
        show_offer(rig)
        tick_at(rig, 101_240)
    recording = rig.recording
    trace.clear()

    if case in {"server-changed", "detector-off", "mic-released", "app-gone", "evicted"}:
        deliver(rig, ended(reason=case.replace("-", "_")))
    elif case == "bind":
        recording.bind_offer(View())
    elif case == "unbind":
        recording.shutdown()
    elif case == "role-ended":
        leave(rig.supervisor)
    elif case == "role-replaced":
        recording.offer.new_role(42)
    elif case == "open-dialog":
        rig.bridge.requestRecording()
    elif case == "recording":
        # Simulate a dialog state already present when role ownership is reevaluated.
        patch.setattr(recording, "dialog_open", True)
        recording.tick()
    elif case == "policy":
        rig.bridge.setSetting("meeting_offer_on_call", False)
    elif case == "remote":
        deliver_remote(rig.supervisor)
    elif case == "session-blocked":
        lock(rig)
    elif case == "epoch-changed":
        rig.session.value = replace(rig.session.value, sleep_epoch=1)
        recording.tick()
    elif case == "timeout":
        tick_at(rig, 160_043)
    elif case in {"refused", "close-button"}:
        assert recording.press_offer("refuse" if case == "refused" else "close", 101_240)
        recording.tick()
    elif case == "other":
        recording.offer.call_ended(CALL, "unknown-end-reason")
    elif case == "wm-close":
        recording.dismiss_offer()
    if pending:
        assert view.calls == [] and rig.recording.offer.state == "idle"
    else:
        assert view.calls[-1] == ("close",)


def slot_case(rig: Rig, case: str, trace: list[str]) -> None:
    view = offer_ready(rig)
    show_offer(rig)
    if case in {"open-dialog", "wm-close", "open-meetings", "start-failed"}:
        rig.storage = replace(rig.storage, free_mb=599)
    press_start(rig, at=100_500)
    assert line("start", "accepted", 500) in trace
    assert not any("event=closed" in item for item in trace)
    rig.now = 101.24
    trace.clear()

    if case == "started":
        rig.reply("prepared")
        rig.reply("started", tracks=["mic", "far"])
    elif case == "start-aborted":
        lock(rig)
    elif case == "call-lost":
        deliver(rig, capture_error(rig, "prepare", "target-gone"))
    elif case == "no-app-split":
        rig.reply("prepared", far_available=False, far_reason="monitor-stream-unsupported")
        leave(rig.supervisor)
    elif case == "shutdown":
        rig.recording.shutdown()
    elif case == "bind":
        rig.recording.bind_offer(View())
    elif case == "remote":
        deliver_remote(rig.supervisor, purpose="start")
    else:
        assert rig.recording.offer.state == "start-failed"
        assert not any("event=closed" in item for item in trace)
        if case == "open-dialog":
            rig.bridge.requestRecording()
        elif case == "wm-close":
            rig.recording.dismiss_offer()
        else:
            action = "open_meetings" if case == "open-meetings" else "close"
            assert rig.recording.press_offer(action, 101_240)
    assert view.calls[-1] == ("close",)


def reject_start_case(rig: Rig, case: str, trace: list[str], patch: pytest.MonkeyPatch) -> None:
    view = offer_ready(rig)
    show_offer(rig)
    recording = rig.recording

    def starting() -> None:
        View.starting(view)
        if case == "policy":
            patch.setattr(recording, "enabled", lambda: False)
        else:
            patch.setattr(recording.controller, "_filesystem_pending", 1)

    patch.setattr(view, "starting", starting)
    trace.clear()
    press_start(rig, at=101_240)
    assert line("start", "accepted", 1200) in trace
    assert view.calls[-2:] == [("starting",), ("close",)]


@pytest.mark.parametrize(
    ("phase", "event", "reason"),
    [
        *[
            ("receipt", "dropped", reason)
            for reason in (
                "unknown-class",
                "no-role",
                "unbound",
                "not-eligible",
                "session-blocked",
                "pending",
                "visible",
                "starting",
                "call-ended",
                "stale",
            )
        ],
        *[
            ("tick", "dropped", reason)
            for reason in (
                "not-eligible",
                "session-blocked",
                "epoch-changed",
                "stale",
                "deferred-suppressed",
                "show-failed",
            )
        ],
        *[
            ("ipc", "dropped", reason)
            for reason in ("old-serial", "generation", "decode", "unknown-call")
        ],
        *[
            ("memory", "suppressed", reason)
            for reason in ("card-memory", "inherited", "same-call", "lock-closed", "card-origin")
        ],
        *[
            ("visible", "closed", reason)
            for reason in (
                "bind",
                "unbind",
                "role-ended",
                "role-replaced",
                "server-changed",
                "detector-off",
                "open-dialog",
                "recording",
                "policy",
                "remote",
                "mic-released",
                "app-gone",
                "evicted",
                "session-blocked",
                "epoch-changed",
                "timeout",
                "refused",
                "close-button",
                "wm-close",
                "other",
            )
        ],
        *[
            ("cancel-pending", "dropped", reason)
            for reason in (
                "bind",
                "unbind",
                "role-ended",
                "role-replaced",
                "server-changed",
                "detector-off",
                "open-dialog",
                "recording",
                "policy",
                "remote",
                "mic-released",
                "app-gone",
                "evicted",
                "session-blocked",
            )
        ],
        *[
            ("slot", "closed", reason)
            for reason in (
                "started",
                "start-failed",
                "start-aborted",
                "open-dialog",
                "open-meetings",
                "call-lost",
                "no-app-split",
                "wm-close",
                "shutdown",
                "bind",
                "remote",
            )
        ],
        ("reject-start", "closed", "policy"),
        ("reject-start", "closed", "start-aborted"),
    ],
)
def test_offer_log_reasons(
    rig: Rig, trace: list[str], monkeypatch: pytest.MonkeyPatch, phase: str, event: str, reason: str
) -> None:
    with monkeypatch.context() as patch:
        if phase == "receipt":
            receipt_case(rig, reason, trace)
        elif phase == "tick":
            pending_case(rig, reason, trace, patch)
        elif phase == "ipc":
            ipc_case(rig, reason, trace)
        elif phase == "memory":
            suppressed_case(rig, reason, trace)
        elif phase == "slot":
            slot_case(rig, reason, trace)
        elif phase == "reject-start":
            reject_start_case(rig, reason, trace, patch)
        else:
            cancel_case(rig, reason, trace, patch, pending=phase == "cancel-pending")
    duration = 60_000 if reason == "timeout" else 1200 if event == "closed" else 0
    assert [item for item in trace if f"event={event} " in item] == [line(event, reason, duration)]
    assert INVALID_LOG not in trace


@pytest.mark.parametrize("result", ["card", "card-error"])
def test_received_card_reply(rig: Rig, trace: list[str], result: str) -> None:
    view = offer_ready(rig)
    req = query_after_unlock(rig)
    if result == "card":
        deliver(rig, reply(rig, req))
    else:
        deliver(
            rig,
            {
                "type": "error",
                "request_type": QUERY,
                "req": req,
                "code": "timeout",
                "message": "meeting-command-failed",
            },
        )
    rig.recording.tick()
    assert trace == [
        line("received", result),
        line("memory", "remember-shown") if result == "card" else line("dropped", "unknown-class"),
    ]
    assert bool(view.calls) == (result == "card")


def test_non_offer_messages_remain_silent(rig: Rig, trace: list[str]) -> None:
    offer_ready(rig)
    rig.bridge.requestRecording()
    w4_reply = rig.call_state()
    rig.supervisor.event(w4_reply)
    rig.supervisor.event({**w4_reply, "generation": rig.supervisor.generation - 1})
    deliver(rig, {"type": "meeting.ack", "req": "unrelated", "request_type": QUERY})
    deliver(rig, {"type": "stats"})
    rig.supervisor.event({"type": "meeting.capture.alive", "generation": -1})
    assert trace == []


def test_closed_w4_reply_is_absent_from_offer_log(rig: Rig, trace: list[str]) -> None:
    offer_ready(rig)
    rig.bridge.requestRecording()
    w4_reply = rig.call_state()
    rig.bridge.closeStartDialog()
    trace.clear()
    rig.supervisor.event(w4_reply)
    assert trace == []
    rig.supervisor.event({**w4_reply, "generation": rig.supervisor.generation - 1})
    assert trace == []


@pytest.mark.parametrize(("placed", "dismiss"), [(True, False), (True, True), (False, False)])
def test_offer_log_window(
    rig: Rig, trace: list[str], monkeypatch: pytest.MonkeyPatch, placed: bool, dismiss: bool
) -> None:
    with (
        surface(rig.recording, view_factory=ManualFramesView) as window,
        monkeypatch.context() as patch,
    ):
        rig.recording.bind_offer(window)
        start_observing(rig)
        patch.setattr(
            window, "_overlay_workarea", lambda: QRect(0, 0, 1024, 800) if placed else None
        )
        # Exercise the retry path where another frame reaches the same handler.
        patch.setattr(window, "_xcb", True)
        sample = Mock(side_effect=[None, 100_500])
        patch.setattr(window, "_sample_x_time", sample)
        check_duplicate_offers = placed and not dismiss
        deliver(rig, started())
        expected = [line("received", "offer")]
        assert trace == expected
        if check_duplicate_offers:
            deliver(rig, started("while-pending"))
            expected += [line("received", "offer"), line("dropped", "pending")]
            assert trace == expected

        rig.recording.tick()
        expected += [
            line("memory", "remember-shown"),
            line("shown", "placed" if placed else "no-workarea"),
        ]
        assert trace == expected
        if check_duplicate_offers:
            deliver(rig, started("while-visible"))
            expected += [line("received", "offer"), line("dropped", "visible")]
            assert trace == expected

        view = window._view
        assert isinstance(view, ManualFramesView)
        if placed:
            tick_at(rig, 100_243)
            view.exposed = True
            expose(view)
            swap_frame(view)
            tick_at(rig, 100_619)
            swap_frame(view)
            assert sample.call_count == 2
            expected.append(line("first-frame", "frame-swapped", 200))
            assert trace == expected
        if dismiss:
            assert rig.recording.press_offer("refuse", 100_619)
            rig.recording.tick()
        else:
            tick_at(rig, 160_000)
        expected += [
            line("memory", "remember-refused"),
            line("closed", "refused", 600) if dismiss else line("closed", "timeout", 60_000),
        ]
        assert trace == expected
        if dismiss:
            deliver(rig, ended())
            assert trace == [*expected, line("memory", "clear")]
        assert not rig.recording.offer.visible


@pytest.mark.parametrize("value", ["free text marker", "/private/not-for-log", 5678])
def test_offer_log_filter_accepts_only_enum_members(trace: list[str], value: object) -> None:
    logger = logging.getLogger(LOGGER)
    event = OfferEvent(value)  # type: ignore[arg-type]
    reason = OfferReason(value)  # type: ignore[arg-type]
    logger.info(OFFER_LIFECYCLE, event, reason, 0)
    logger.info(OFFER_LIFECYCLE, OfferEvent.CLOSED, value, 0)
    logger.info(OFFER_LIFECYCLE, value, OfferReason.STALE, 0)
    logger.info(OFFER_LIFECYCLE, OfferEvent.CLOSED, OfferReason.TIMEOUT, True)
    logger.info(OFFER_LIFECYCLE, OfferEvent.CLOSED, OfferReason.TIMEOUT, -100)
    logger.info(OFFER_LIFECYCLE, OfferEvent.CLOSED, OfferReason.TIMEOUT, 0.5)
    assert trace == [
        line("other", "other"),
        INVALID_LOG,
        INVALID_LOG,
        INVALID_LOG,
        INVALID_LOG,
        INVALID_LOG,
    ]


def test_offer_memory_inheritance_transitions(rig: Rig, trace: list[str]) -> None:
    offer_ready(rig)
    show_offer(rig)
    trace.clear()
    replace_role(rig)
    assert trace == [
        line("memory", "reset"),
        line("closed", "role-ended"),
        line("memory", "inherit"),
    ]
    trace.clear()
    deliver(rig, started("inherited-call", measured=rig.recording.offer.now_ms()))
    assert trace == [
        line("received", "offer"),
        line("memory", "bind"),
        line("suppressed", "inherited"),
    ]
    replace_role(rig)
    trace.clear()
    tick_at(rig, rig.recording.offer.now_ms() + 15_000)
    assert trace == [line("memory", "expire")]

    show_offer(rig, call_id="fresh-call")
    replace_role(rig, start=False)
    trace.clear()
    role = rig.supervisor
    deliver_detector(role, role.messages[-1], "error", code="protocol-mismatch")
    assert trace == [line("memory", "drop")]


def test_offer_memory_end_transitions(rig: Rig, trace: list[str]) -> None:
    offer_ready(rig)
    show_offer(rig)
    lock(rig)
    assert trace[-2:] == [line("memory", "remember-lock-closed"), line("closed", "session-blocked")]
    trace.clear()
    deliver(rig, ended(reason="server_changed"))
    assert trace == [line("memory", "server-changed")]

    rig.session.value = replace(rig.session.value, locked=False)
    show_offer(rig, call_id="next-call")
    trace.clear()
    deliver(rig, ended("next-call", reason="evicted"))
    assert trace == [
        line("memory", "remember-shown"),
        line("closed", "evicted"),
        line("memory", "evicted"),
    ]
    trace.clear()
    deliver_remote(rig.supervisor)
    assert trace == [line("memory", "clear")]


@pytest.mark.parametrize("during_show", [True, False])
def test_offer_close_with_no_positive_elapsed_time(
    rig: Rig, trace: list[str], monkeypatch: pytest.MonkeyPatch, during_show: bool
) -> None:
    view = offer_ready(rig)
    if during_show:
        with monkeypatch.context() as patch:
            patch.setattr(view, "show", lambda *_args: rig.recording.dismiss_offer())
            deliver(rig, started())
            rig.recording.tick()
    else:
        show_offer(rig)
        rig.now -= 1
        rig.recording.dismiss_offer()
    assert trace[-1] == line("closed", "wm-close", 0)
    assert view.calls[-1] == ("close",)
