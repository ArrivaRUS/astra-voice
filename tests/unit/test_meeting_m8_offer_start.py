"""E13.M8 6a-3: admission, capture and failure routing from W1 and W4."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest
from PyQt5 import sip
from PyQt5.QtCore import QCoreApplication, QEvent, QObject, Qt, QUrl
from PyQt5.QtGui import QKeyEvent
from PyQt5.QtQml import QQmlApplicationEngine
from test_meeting_capture import Rig as RoleRig
from test_meeting_m8_call_service import ALLOWED, CheckDouble, checked_card
from test_meeting_m8_call_service import service as service
from test_meeting_m8_offer import (
    CALL,
    SHOW,
    View,
    assert_no_capture,
    deliver,
    ended,
    memory,
    offer_ready,
    real_supervisor,
    show_offer,
    started,
    started_from_w1,
    tick_at,
)
from test_meeting_m8_role_ownership import command_types
from test_meeting_m8_start import CaptureEvents, pinned_outputs
from test_meeting_recording_bridge import (
    MID,
    Rig,
    capture_error,
    deliver_detector,
    deliver_remote,
)
from test_meeting_recording_bridge import delayed_rig as delayed_rig
from test_meeting_recording_bridge import rig as rig
from test_meeting_supervisor import receive

from astra_voice.core.command_mode import SessionSnapshot
from astra_voice.core.meeting_session import ERROR_TEXT, WARNING_TEXT, StorageStatus
from astra_voice.meetings import events
from astra_voice.meetings.store import MeetingStoreError
from astra_voice.ui.icons import install_icon_provider
from astra_voice.ui.meeting_notice import MeetingNotice
from astra_voice.ui.meeting_offer import OfferController
from astra_voice.ui.meeting_recording import AudioHints
from astra_voice.worker import ipc
from helpers.qt_app import get_qapplication

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("filesystem_euid")]

PREPARE = "meeting.capture.prepare"
OPEN = "meeting.capture.open"
QUERY = "meeting.call.query"
STOP = "meeting.capture.stop"
START_AT = 100_600
DION_LOST = (
    "Не удалось однозначно отделить звук DION — запишется только микрофон или весь звук компьютера"
)
BROWSER_LOST = (
    "Не удалось однозначно отделить звук браузера — "
    "запишется только микрофон или весь звук компьютера"
)
ROOT = Path(__file__).resolve().parents[2]


def press_start(rig: Rig, at: int = START_AT) -> None:
    rig.now = at / 1000
    assert rig.recording.press_offer("start", at)
    rig.recording.tick()


def start_offer(rig: Rig, *, browser: bool = False) -> View:
    view = offer_ready(rig)
    show_offer(rig, app_class="browser" if browser else "dion")
    press_start(rig)
    started_from_w1(rig, CALL)
    assert view.calls == [("show", "call-browser" if browser else "call-app", 60), ("starting",)]
    return view


def delayed_start_offer(rig: Rig) -> View:
    rig.bridge.setSetting("meeting_offer_on_call", True)
    rig.recording.tick()
    assert not rig.supervisor.ready
    rig.supervisor.ready = True
    rig.recording.tick()
    deliver_detector(rig.supervisor, rig.supervisor.messages[-1], "meeting.detector.started")
    view = View()
    rig.recording.bind_offer(view)
    show_offer(rig)
    press_start(rig)
    started_from_w1(rig, CALL)
    return view


def lock(rig: Rig, *, sleep: bool = False) -> None:
    before = rig.session.value
    rig.session.value = replace(
        before,
        locked=not sleep,
        preparing_for_sleep=sleep,
        blocked_epoch=before.blocked_epoch + 1,
        sleep_epoch=before.sleep_epoch + int(sleep),
    )
    rig.session.changed.emit(rig.session.value)


def assert_call_lost(rig: Rig, text: str = DION_LOST) -> None:
    assert rig.bridge.startDialogOpen and rig.bridge.startState == "call-lost"
    assert rig.bridge.startWarnings == [text]
    assert [item["id"] for item in rig.bridge.startOptions] == ["room", "all"]
    assert rig.bridge.selectedSource == "room"


def run_qt_probe(mode: str, directory: Path) -> None:
    # Qt Quick chooses its render backend on first use in each process.
    env = {
        **os.environ,
        "DBUS_SESSION_BUS_ADDRESS": "unix:path=/nonexistent",
        "QT_QPA_PLATFORM": "offscreen",
        "QT_QUICK_BACKEND": "software",
        "PYTHONPATH": os.pathsep.join(str(ROOT / part) for part in ("src", "tests", "tests/unit")),
    }
    for key in ("DISPLAY", "WAYLAND_DISPLAY", "ASTRA_VOICE_RESOURCES"):
        env.pop(key, None)
    result = subprocess.run(
        [sys.executable, __file__, mode, str(directory)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=27,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("key", ["return", "keypad-enter", "space"])
def test_w4_held_key_cannot_accept_fallback(tmp_path: Path, key: str) -> None:
    run_qt_probe(key, tmp_path)


def w4_held_key(rig: Rig, key: Qt.Key) -> None:
    engine = QQmlApplicationEngine()
    engine.rootContext().setContextProperty("meetingBridge", rig.bridge)
    install_icon_provider(engine)
    engine.load(QUrl.fromLocalFile(str(Path("qml/StartDialog.qml").resolve())))
    (window,) = engine.rootObjects()
    try:
        rig.bridge.requestRecording()
        deliver(rig, rig.call_state())
        assert rig.bridge.selectedSource == "call"
        QCoreApplication.processEvents()
        button = window.findChild(QObject, "meetingStartConfirm")
        assert window.isVisible() and button is not None
        button.forceActiveFocus(Qt.TabFocusReason)
        assert button.hasActiveFocus()

        def send(kind: QEvent.Type, *, repeat: bool = False) -> None:
            event = QKeyEvent(kind, key, Qt.NoModifier, "", repeat, 1)
            QCoreApplication.sendEvent(window, event)

        send(QEvent.KeyPress)
        if key == Qt.Key_Space:
            send(QEvent.KeyRelease)
        assert command_types(rig.supervisor).count(PREPARE) == 1
        assert rig.supervisor.messages[-1]["far"] == {"kind": "app_stream", "call_id": "call1"}
        deliver(rig, capture_error(rig, "prepare", "target-mismatch"))
        assert_call_lost(rig)
        assert button.hasActiveFocus() and rig.bridge.startCanStart
        for _ in range(2):
            send(QEvent.KeyPress, repeat=True)
            send(QEvent.KeyRelease, repeat=True)
        assert command_types(rig.supervisor).count(PREPARE) == 1
        send(QEvent.KeyRelease)
        assert command_types(rig.supervisor).count(PREPARE) == 1
        send(QEvent.KeyPress)
        send(QEvent.KeyRelease)
        assert command_types(rig.supervisor).count(PREPARE) == 2
        assert rig.supervisor.messages[-1]["far"] == {"kind": "none"}
    finally:
        window.hide()
        sip.delete(engine)


def test_app_stream_prepared_remote_requires_fresh_manual_click(rig: Rig) -> None:
    rig.audio = AudioHints(remote=True)
    rig.bridge.requestRecording()
    deliver(rig, rig.call_state())
    assert rig.bridge.startWarnings == [WARNING_TEXT["audio-remote"]]
    assert [row["id"] for row in rig.bridge.startOptions] == ["call", "room", "all"]
    assert rig.bridge.selectedSource == "call"
    assert_no_capture(rig)
    rig.bridge.on_start_clicked()
    role = rig.supervisor
    assert role.messages[-1]["far"] == {"kind": "app_stream", "call_id": "call1"}
    assert rig.recording._accepted_remote
    rig.reply("prepared", server_local=False)
    assert OPEN not in command_types(role) and command_types(role).count(PREPARE) == 1
    assert rig.recording.controller.state == "stopping"
    assert rig.bridge.startState == "warn-remote-audio"
    assert rig.bridge.startWarnings == [WARNING_TEXT["audio-remote"]]
    assert [row["id"] for row in rig.bridge.startOptions] == ["room", "all"]
    assert rig.bridge.selectedSource == "room"
    role.ready, role.exited = False, True
    rig.recording.tick()
    assert sum(command_types(item).count(PREPARE) for item in rig.supervisors) == 1
    rig.bridge.selectRecordingSource("all")
    rig.bridge.on_start_clicked()
    assert rig.supervisor.messages[-1]["far"] == {"kind": "sink_monitor"}
    rig.reply("prepared", server_local=False)
    assert rig.supervisor.messages[-1]["type"] == OPEN


def test_request_recording_closes_failed_offer_before_opening_w4(rig: Rig) -> None:
    view = offer_ready(rig)
    show_offer(rig)
    rig.storage = StorageStatus(599, True, False)
    press_start(rig)
    assert rig.recording.offer.state == "start-failed" and not rig.bridge.startDialogOpen
    w4_frames: list[tuple[object, ...]] = []

    def observe_w4() -> None:
        if rig.bridge.startDialogOpen:
            w4_frames.append(view.calls[-1])

    rig.recording.changed.connect(observe_w4)
    rig.storage = StorageStatus(1000, True, False)
    rig.bridge.requestRecording()
    assert w4_frames and all(frame == ("close",) for frame in w4_frames)
    assert rig.bridge.startDialogOpen and rig.bridge.startCanStart
    assert not rig.recording.offer.starting_slot and memory(rig) == "accepted"
    assert_no_capture(rig)


def test_w4_stale_card_after_role_exit_requires_second_click(rig: Rig) -> None:
    rig.bridge.requestRecording()
    deliver(rig, rig.call_state())
    assert rig.bridge.selectedSource == "call"
    old_role = rig.supervisor
    old_role.kill()
    assert rig.bridge.startDialogOpen and rig.bridge.selectedSource == "call"
    assert_no_capture(rig)
    rig.bridge.on_start_clicked()
    role = rig.supervisor
    assert role is not old_role
    assert role.messages[-1]["far"] == {"kind": "app_stream", "call_id": "call1"}
    deliver(rig, capture_error(rig, "prepare", "target-gone"))
    assert_call_lost(rig)
    assert command_types(role).count(PREPARE) == 1 and OPEN not in command_types(role)
    rig.recording.tick()
    assert command_types(role).count(PREPARE) == 1 and OPEN not in command_types(role)
    rig.bridge.on_start_clicked()
    assert rig.supervisor is role
    assert command_types(role).count(PREPARE) == 2
    assert role.messages[-1]["far"] == {"kind": "none"}
    rig.reply("prepared")
    assert command_types(role).count(OPEN) == 1


def test_410_4_offer_alone_never_starts_capture(rig: Rig) -> None:
    view = offer_ready(rig)
    show_offer(rig)
    tick_at(rig, 150_000)
    assert_no_capture(rig)
    assert view.calls == [SHOW, ("remaining", 10)]


@pytest.mark.parametrize("at", [100, 499, 500, 600])
def test_434_1_start_guard(rig: Rig, at: int) -> None:
    view = offer_ready(rig)
    show_offer(rig)
    rig.now = (100_000 + at) / 1000
    admitted = at >= 500
    assert rig.recording.press_offer("start", 100_000 + at) is admitted
    assert_no_capture(rig)
    rig.recording.tick()
    if admitted:
        started_from_w1(rig, CALL)
        assert view.calls == [SHOW, ("starting",)]
    else:
        assert_no_capture(rig)
        assert view.calls == [SHOW]


def test_434_2_started_closes_w1_before_w2_and_w3(rig: Rig) -> None:
    recording = rig.recording
    rig.recording.call_id = "other-card-call"
    rig.recording.app_class = "browser"
    from_recording, from_bridge = Mock(), Mock()
    recording.openMeetingsRequested.connect(from_recording)
    rig.bridge.openMeetingsRequested.connect(from_bridge)
    notice = MeetingNotice(recording, None, notice=rig.bridge.notice)
    try:
        view = start_offer(rig)
        frames: list[tuple[object, ...]] = []
        recording.changed.connect(
            lambda: frames.append(view.calls[-1]) if recording.overlay["visible"] else None
        )
        rig.reply("prepared")
        assert recording.controller.state == "opening"
        assert not notice._view.isVisible() and frames == []
        rig.reply("started", tracks=["mic", "far"])
        assert frames and all(frame == ("close",) for frame in frames)
        assert recording.overlay["visible"] and notice._view.isVisible()
        assert recording.notice_app_class == "dion"
        assert memory(rig) == "accepted" and not rig.bridge.startDialogOpen
        from_recording.assert_not_called()
        from_bridge.assert_not_called()
    finally:
        notice.shutdown()


@pytest.mark.parametrize(("code", "browser"), [("target-mismatch", False), ("target-gone", True)])
def test_434_3_prepare_refusal_keeps_role_and_waits_for_second_click(
    rig: Rig, code: str, browser: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    mr_signal, bridge_signal = Mock(), Mock()
    rig.recording.openMeetingsRequested.connect(mr_signal)
    rig.bridge.openMeetingsRequested.connect(bridge_signal)
    view = start_offer(rig, browser=browser)
    role = rig.supervisor
    assert (rig.store.root / MID).is_dir()
    discard = Mock(wraps=rig.store.discard_reserved)
    with monkeypatch.context() as patch:
        patch.setattr(rig.store, "discard_reserved", discard)
        deliver(rig, capture_error(rig, "prepare", code))
        rig.recording.tick()
    discard.assert_called_once_with(MID)
    assert not (rig.store.root / MID).exists() and rig.store.list() == []
    assert role.kills == 0 and role.ready and rig.recording.controller.state == "idle"
    assert view.calls[-1] == ("close",)
    assert memory(rig, "browser" if browser else "dion") == "accepted"
    assert_call_lost(rig, BROWSER_LOST if browser else DION_LOST)
    mr_signal.assert_called_once_with()
    bridge_signal.assert_called_once_with()
    assert command_types(role).count(PREPARE) == 1 and OPEN not in command_types(role)
    assert QUERY not in command_types(role) and rig.recording._query_req == ""
    deliver(rig, rig.call_state(req="old-card"))
    rig.recording.tick()
    assert_call_lost(rig, BROWSER_LOST if browser else DION_LOST)
    assert rig.bridge.startCanStart
    rig.bridge.on_start_clicked()
    assert rig.supervisor is role and role.starts == 1
    assert command_types(role).count(PREPARE) == 2
    assert role.messages[-1]["far"] == {"kind": "none"}
    assert QUERY not in command_types(role)


@pytest.mark.parametrize("brackets", [False, True], ids=["no-anchor", "bracket-refusal"])
def test_434_4_first_open_refusal_routes_after_confirmed_exit(
    delayed_rig: Rig, brackets: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = delayed_rig
    view = delayed_start_offer(rig)
    role = rig.supervisor
    rig.reply("prepared")
    assert role.messages[-1]["type"] == OPEN
    done = Mock(wraps=rig.recording.offer.start_done)
    with monkeypatch.context() as patch:
        patch.setattr(rig.recording.offer, "start_done", done)
        code = "target-mismatch" if brackets else "target-gone"
        deliver(rig, capture_error(rig, "open", code))
        if brackets:
            rig.reply("closed", tracks=["mic", "far"], state_rev=1)
            rig.reply(
                "finalized",
                segments={"mic": 0, "far": 0},
                intervals=[],
                status="failed",
                state_rev=1,
            )
        tick_at(rig, START_AT + 6_000)  # A known route has disarmed the W1 watchdog.
        assert view.calls == [SHOW, ("starting",)]
        assert role.kills == 1 and not role.exited
        assert rig.recording.controller.error_code == ""
        assert not rig.bridge.startDialogOpen and rig.bridge.startState != "error-busy"
        assert command_types(role).count(PREPARE) == 1
        role.exited = True
        rig.recording.tick()
    done.assert_not_called()
    assert view.calls == [SHOW, ("starting",), ("close",)]
    assert_call_lost(rig)
    assert rig.recording.controller.error_code == ""
    assert "звуковая служба не отвечает" not in rig.bridge.startError
    assert rig.recording.overlay["state"] != "error"
    assert sum(command_types(item).count(PREPARE) for item in rig.supervisors) == 1
    rig.recording.tick()
    # Долг 21.11: после commit_reserved пустая прерванная встреча пока публикуется.
    assert rig.recording.interrupted_text == "Запись встречи прервалась. Сохранено 0:00:00"
    rig.supervisor.ready = True
    rig.recording.tick()
    assert rig.bridge.startCanStart
    with monkeypatch.context() as patch:
        patch.setattr(rig.recording.controller, "id_factory", lambda: "2026-11-01_120000-efgh")
        rig.bridge.on_start_clicked()
        rig.recording.tick()
    assert rig.supervisor is not role and PREPARE in command_types(rig.supervisor)


@pytest.mark.parametrize("phase", ["preparing", "opening", "sleep"])
def test_434_5_lock_closes_before_late_role_reply(rig: Rig, phase: str) -> None:
    view = start_offer(rig)
    if phase == "opening":
        rig.reply("prepared")
        late = capture_error(rig, "open", "admission-stale")
    else:
        late = capture_error(rig, "prepare", "target-mismatch")
    lock(rig, sleep=phase == "sleep")
    assert view.calls == [SHOW, ("starting",), ("close",)]
    assert memory(rig) == "lock_closed"
    if phase != "opening":
        assert OPEN not in command_types(rig.supervisor)
    deliver(rig, late)
    rig.recording.tick()
    assert view.calls == [SHOW, ("starting",), ("close",)]
    assert not rig.bridge.startDialogOpen


def test_offer_session_revokes_slot_without_bridge_even_after_role_exit() -> None:
    now = [100.0]
    view, fired = View(), Mock()
    offer = OfferController(
        clock=lambda: now[0], on_visibility_changed=lambda: None, on_start=fired
    )
    allowed = SessionSnapshot(known=True, locked=False)
    offer.bind(view)
    offer.new_role(19)
    offer.detector_started()
    offer.receive(
        call_id=CALL,
        app_class="dion",
        measured_ms=100_000,
        origin="offer",
        eligible=True,
        session=allowed,
    )
    offer.begin_tick()
    offer.tick(eligible=True, session=allowed)
    now[0] = 100.6
    assert offer.press("start", 100_600)
    offer.tick(eligible=True, session=allowed)
    fired.assert_called_once_with(CALL, "dion")
    offer.role_ended(19)
    assert view.calls == [SHOW, ("starting",)]
    offer.session_changed(replace(allowed, locked=True), can_query=False)
    assert view.calls == [SHOW, ("starting",), ("close",)]
    assert offer._memory["dion"].state == "lock_closed"
    assert offer._memory["dion"].role_serial == 19
    offer.start_failed("late")
    offer.start_aborted()
    offer.start_done()
    offer.start_closed()
    assert view.calls[-1] == ("close",) and len(view.calls) == 3


def test_448_10_offer_without_variant1_still_uses_start(rig: Rig) -> None:
    offer_ready(rig)
    deliver(rig, started(variant1=False))
    rig.recording.tick()
    assert rig.recording.offer.visible
    press_start(rig)
    started_from_w1(rig, CALL)


@pytest.mark.parametrize(
    "code", ["disk-low", "storage-unavailable", "busy", "session-blocked", "disabled"]
)
def test_synchronous_rejection_never_leaves_w1_in_starting(
    rig: Rig, code: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    view = offer_ready(rig)
    show_offer(rig)

    def reject(_far: object) -> bool:
        rig.recording.controller.error_code = code
        return False

    with monkeypatch.context() as patch:
        patch.setattr(rig.recording.controller, "start", reject)
        press_start(rig)
    if code in {"disk-low", "storage-unavailable"}:
        assert view.calls == [SHOW, ("starting",), ("start-failed", ERROR_TEXT[code])]
    else:
        assert view.calls == [SHOW, ("starting",), ("close",)]
    assert memory(rig) == ("lock_closed" if code in {"busy", "session-blocked"} else "accepted")
    assert_no_capture(rig)


@pytest.mark.parametrize("reason", ["busy", "retiring", "unready", "blocked", "disabled", "policy"])
def test_rejected_bridge_gate_resolves_on_press(
    rig: Rig, reason: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    view = offer_ready(rig)
    show_offer(rig)
    with monkeypatch.context() as patch:

        def starting() -> None:
            View.starting(view)
            if reason == "busy":
                patch.setattr(rig.recording.controller, "_filesystem_pending", 1)
            elif reason == "retiring":
                patch.setattr(rig.recording, "_retire_deadline", rig.now + 1)
            elif reason == "unready":
                patch.setattr(rig.supervisor, "ready", False)
            elif reason == "blocked":
                rig.session.value = replace(rig.session.value, locked=True)
            else:
                gate = "enabled" if reason == "disabled" else "policy_allowed"
                patch.setattr(rig.recording, gate, lambda: False)

        patch.setattr(view, "starting", starting)
        press_start(rig)
    assert view.calls == [SHOW, ("starting",), ("close",)]
    assert_no_capture(rig)
    if reason not in {"disabled", "policy"}:
        assert memory(rig) == "lock_closed"


@pytest.mark.parametrize("code", ["disk-low", "storage-unavailable"])
def test_filesystem_start_failure_from_completed_job(
    rig: Rig, code: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    view = offer_ready(rig)
    show_offer(rig)
    jobs: list[Callable[[], None]] = []
    if code == "disk-low":
        rig.storage = StorageStatus(599, True, False)

    def fail_commit(_meeting: object) -> None:
        raise MeetingStoreError("test commit")

    with monkeypatch.context() as patch:
        patch.setattr(rig.recording.controller, "_filesystem_submit", jobs.append)
        press_start(rig)
        assert view.calls == [SHOW, ("starting",)]
        if code == "storage-unavailable":
            jobs.pop(0)()
            rig.recording.tick()
            started_from_w1(rig, CALL)
            patch.setattr(rig.store, "commit_reserved", fail_commit)
            rig.reply("prepared")
            assert rig.recording.offer.state == "starting"
        jobs.pop(0)()
        rig.recording.tick()
        assert view.calls == [SHOW, ("starting",), ("start-failed", ERROR_TEXT[code])]
    assert OPEN not in command_types(rig.supervisor)
    assert not rig.bridge.startDialogOpen


def test_failed_slot_blocks_new_offer_and_old_error_does_not_block_next_start(rig: Rig) -> None:
    view = offer_ready(rig)
    show_offer(rig)
    rig.storage = StorageStatus(599, True, False)
    press_start(rig)
    assert view.calls == [SHOW, ("starting",), ("start-failed", ERROR_TEXT["disk-low"])]
    assert rig.recording._offer_eligible()
    deliver(rig, started("next-browser", app_class="browser", measured=START_AT))
    rig.recording.tick()
    assert not any(call[0] == "show" and call[1] == "call-browser" for call in view.calls)
    assert rig.recording.press_offer("close", START_AT)
    assert view.calls[-1] == ("close",)
    assert rig.recording.controller.error_code == "disk-low"
    rig.storage = StorageStatus(1000, True, False)
    show_offer(rig, call_id="next-browser", app_class="browser")
    press_start(rig, START_AT + 600)
    started_from_w1(rig, "next-browser")


def test_offer_can_start_after_start_failure_and_role_replacement(rig: Rig) -> None:
    view = start_offer(rig)
    failed_role = rig.supervisor
    failed_role.kill()
    assert view.calls[-1] == ("start-failed", ERROR_TEXT["audio-unavailable"])
    assert rig.recording.press_offer("close", START_AT)
    tick_at(rig, START_AT + 2_000)
    assert rig.supervisor is not failed_role and rig.supervisor.ready
    deliver_detector(rig.supervisor, rig.supervisor.messages[-1], "meeting.detector.started")
    assert rig.recording.controller.error_code == "audio-unavailable"
    show_offer(rig, call_id="new-browser", app_class="browser")
    press_start(rig, START_AT + 2_600)
    started_from_w1(rig, "new-browser")


@pytest.mark.parametrize(
    ("phase", "action"),
    [
        ("exit", "close"),
        ("exit", "open_meetings"),
        ("exit", "wm"),
        ("prepare-timeout", "close"),
        ("open-audio-failed", "close"),
    ],
)
def test_failed_slot_actions_work_with_dead_role(rig: Rig, phase: str, action: str) -> None:
    opened, bridge_opened = Mock(), Mock()
    rig.recording.openMeetingsRequested.connect(opened)
    rig.bridge.openMeetingsRequested.connect(bridge_opened)
    view = start_offer(rig)
    if phase == "exit":
        rig.supervisor.kill()
    elif phase == "prepare-timeout":
        tick_at(rig, START_AT + 4_999)
        assert view.calls[-1] == ("starting",)
        tick_at(rig, START_AT + 5_000)
    else:
        rig.reply("prepared")
        deliver(rig, capture_error(rig, "open", "audio-failed"))
    assert rig.supervisor.exited and not rig.recording._offer_eligible()
    assert view.calls[-1] == ("start-failed", ERROR_TEXT["audio-unavailable"])
    if action == "wm":
        rig.recording.dismiss_offer()
    else:
        assert rig.recording.press_offer(action, rig.recording.offer.now_ms())
    assert view.calls[-1:] == [("close",)]
    assert memory(rig) == "accepted" and not rig.bridge.startDialogOpen
    expected = int(action == "open_meetings")
    assert opened.call_count == bridge_opened.call_count == expected


@pytest.mark.parametrize("delayed_kill", [False, True])
def test_offer_watchdog_at_six_seconds(delayed_rig: Rig, delayed_kill: bool) -> None:
    rig = delayed_rig
    view = delayed_start_offer(rig)
    open_request = None
    if delayed_kill:
        tick_at(rig, START_AT + 5_000)
        assert rig.supervisor.kills == 1 and not rig.supervisor.exited
    else:
        rig.reply("prepared")
        assert rig.recording.controller.state == "opening"
        open_request = rig.supervisor.messages[-1]
        assert open_request["type"] == OPEN
    tick_at(rig, START_AT + 5_999)
    assert view.calls == [SHOW, ("starting",)]
    tick_at(rig, START_AT + 6_000)
    assert view.calls[-1] == ("start-failed", ERROR_TEXT["audio-unavailable"])
    assert rig.recording.press_offer("close", START_AT + 6_000)
    assert view.calls[-1:] == [("close",)] and not rig.bridge.startDialogOpen
    if open_request is not None:
        rig.reply("started", req=open_request["req"], request_type=OPEN, tracks=["mic", "far"])
        assert rig.recording.controller.state == "stopping"
        assert command_types(rig.supervisor).count(STOP) == 1


def test_late_prepare_refusal_after_stop_uses_only_exit_cleanup(
    delayed_rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = delayed_rig
    delayed_start_offer(rig)
    refusal = capture_error(rig, "prepare", "target-mismatch")
    discard = Mock(wraps=rig.store.discard_reserved)
    with monkeypatch.context() as patch:
        patch.setattr(rig.store, "discard_reserved", discard)
        lock(rig)
        assert rig.recording.controller.state == "stopping"
        deliver(rig, refusal)
        assert rig.recording.controller.state == "closing-unknown"
        discard.assert_not_called()
        rig.supervisor.exited = True
        rig.recording.tick()
    discard.assert_called_once_with(MID)
    assert rig.recording.controller.state == "stopped"
    assert not rig.bridge.startDialogOpen


@pytest.mark.parametrize("kind", ["warn-nfs", "warn-remote-audio", "no-app-split"])
def test_prepared_fallback_waits_for_exit_without_open(delayed_rig: Rig, kind: str) -> None:
    rig = delayed_rig
    if kind == "warn-nfs":
        rig.storage = StorageStatus(1000, True, True)
    view = delayed_start_offer(rig)
    if kind == "no-app-split":
        rig.reply("prepared", far_available=False, far_reason="monitor-stream-unsupported")
    else:
        rig.reply("prepared", server_local=kind != "warn-remote-audio")
    role = rig.supervisor
    assert OPEN not in command_types(role) and command_types(role).count(PREPARE) == 1
    assert not rig.bridge.startDialogOpen and rig.bridge.startState != "error-busy"
    assert view.calls == [SHOW, ("starting",)]
    rig.storage = StorageStatus(1000, True, False)
    role.ready, role.exited = False, True
    rig.recording.tick()
    assert view.calls == [SHOW, ("starting",), ("close",)]
    assert rig.bridge.startDialogOpen and rig.bridge.startState == kind
    warnings = {"warn-nfs": "storage-network", "warn-remote-audio": "audio-remote"}
    assert rig.bridge.startWarnings == ([WARNING_TEXT[warnings[kind]]] if kind in warnings else [])
    assert all(OPEN not in command_types(item) for item in rig.supervisors)
    if kind in {"warn-remote-audio", "no-app-split"}:
        rig.supervisor.ready = True
        rig.recording.tick()
        deliver(rig, rig.call_state())
        assert [item["id"] for item in rig.bridge.startOptions if item["enabled"]] == [
            "room",
            "all",
        ]
        assert rig.bridge.selectedSource == "room"
        assert rig.bridge.startCanStart
    if kind == "warn-remote-audio":
        assert [item["id"] for item in rig.bridge.startOptions] == ["room", "all"]
        assert sum(command_types(item).count(PREPARE) for item in rig.supervisors) == 1
        rig.bridge.on_start_clicked()
        assert rig.supervisor.messages[-1]["far"] == {"kind": "none"}
        rig.reply("prepared", server_local=False)
        assert rig.supervisor.messages[-1]["type"] == OPEN


def test_remote_start_closes_w1_before_late_target_mismatch(rig: Rig) -> None:
    view = start_offer(rig)
    deliver_remote(rig.supervisor, purpose="start")
    assert view.calls == [SHOW, ("starting",), ("close",)]
    assert memory(rig) is None and not rig.bridge.startDialogOpen
    deliver(rig, capture_error(rig, "prepare", "target-mismatch"))
    assert_call_lost(rig)
    assert view.calls == [SHOW, ("starting",), ("close",)]
    assert not rig.supervisor.kills


@pytest.mark.parametrize("cancel", ["bind", "policy"])
def test_closed_slot_by_bind_or_policy_never_reopens_ui(rig: Rig, cancel: str) -> None:
    view = start_offer(rig)
    late = capture_error(rig, "prepare", "target-mismatch")
    replacement = View()
    if cancel == "bind":
        rig.recording.bind_offer(replacement)
    else:
        rig.bridge.setSetting("meeting_offer_on_call", False)
    assert view.calls == [SHOW, ("starting",), ("close",)]
    deliver(rig, late)
    rig.recording.tick()
    assert not rig.bridge.startDialogOpen
    assert view.calls == [SHOW, ("starting",), ("close",)] and replacement.calls == []


def test_starting_slot_ignores_end_replacement_suspend_dismiss_and_presses(rig: Rig) -> None:
    view = start_offer(rig)
    offer = rig.recording.offer
    deliver(rig, ended())
    offer.new_role(42)
    offer.suspend(for_capture=True)
    rig.recording.dismiss_offer()
    for action in ("close", "open_meetings", "start", "refuse"):
        assert not rig.recording.press_offer(action, START_AT)
    assert offer.state == "starting" and view.calls == [SHOW, ("starting",)]


@pytest.mark.parametrize("origin", ["offer", "card"])
def test_shutdown_aborts_start_before_waiting_on_role(
    rig: Rig, origin: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="astra_voice.ui.meeting_recording")
    view = None
    if origin == "offer":
        view = start_offer(rig)
    else:
        rig.bridge.requestRecording()
        rig.bridge.on_start_clicked()
    late = capture_error(rig, "prepare", "target-mismatch")
    rig.now += 0.25

    def wait_on_role(_deadline: float) -> None:
        if view is not None:
            assert view.calls == [SHOW, ("starting",), ("close",)]
        deliver(rig, late)
        assert rig.bridge.startDialogOpen is (origin == "card")

    with monkeypatch.context() as patch:
        patch.setattr(rig.recording, "_stop_for_exit", wait_on_role)
        rig.recording.shutdown()
        rig.recording.shutdown()
    if view is not None:
        assert view.calls == [SHOW, ("starting",), ("close",)]
    (outcome,) = [record.args for record in caplog.records if record.msg is events.START_LATENCY]
    assert outcome == (origin, "aborted", 250)
    assert rig.recording._start_origin is None and rig.recording._start_pressed_at is None
    assert rig.recording._start_attempt == 0 and not rig.recording.offer.starting_slot


def test_w4_keeps_variant1_hidden_until_dialog_closes(rig: Rig) -> None:
    offer_ready(rig)
    rig.bridge.requestRecording()
    card = rig.call_state()
    deliver(rig, card)
    assert rig.bridge.selectedSource == "call"
    rig.bridge.on_start_clicked()
    deliver(rig, capture_error(rig, "prepare", "target-mismatch"))
    assert_call_lost(rig)
    deliver(rig, card)
    rig.recording.tick()
    assert_call_lost(rig)
    assert command_types(rig.supervisor).count(QUERY) == 1
    rig.bridge.closeStartDialog()
    rig.bridge.requestRecording()
    assert (rig.recording.call_id, rig.recording.app_class, rig.recording.variant1) == (
        "",
        "",
        False,
    )
    assert [item["id"] for item in rig.bridge.startOptions] == ["room", "all"]
    deliver(rig, rig.call_state())
    assert [item["id"] for item in rig.bridge.startOptions] == ["call", "room", "all"]
    assert rig.bridge.selectedSource == "call"


@pytest.mark.parametrize("origin", ["offer", "card"])
@pytest.mark.parametrize("outcome", ["started", "call-lost", "start-failed", "aborted"])
def test_start_latency_is_once_and_contains_only_fixed_fields(
    rig: Rig, origin: str, outcome: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="astra_voice.ui.meeting_recording")
    if origin == "offer":
        pressed_at = START_AT
        offer_ready(rig)
        show_offer(rig)
        rig.now = pressed_at / 1000
        assert rig.recording.press_offer("start", pressed_at)
        tick_at(rig, pressed_at + 50)
        started_from_w1(rig, CALL)
    else:
        rig.bridge.requestRecording()
        deliver(rig, rig.call_state())
        rig.bridge.on_start_clicked()
        pressed_at = 100_000
    rig.now = (pressed_at + 250) / 1000
    if outcome == "started":
        rig.reply("prepared")
        rig.reply("started", tracks=["mic", "far"])
    elif outcome == "call-lost":
        deliver(rig, capture_error(rig, "prepare", "target-mismatch"))
    elif outcome == "start-failed":
        deliver(rig, capture_error(rig, "prepare", "audio-failed"))
    else:
        lock(rig)
    rig.recording.tick()
    rig.recording.tick()
    (record,) = [record for record in caplog.records if record.msg is events.START_LATENCY]
    assert record.args == (origin, outcome, 250)
    assert events.log_arguments(record.msg, record.args) == record.args
    line = record.getMessage()
    assert all(value not in line for value in (CALL, MID, "capture1", "target-", "call1"))


@pytest.mark.parametrize(
    "outcome,outputs", [("started", 1), ("started", 3), ("target-mismatch", 1)]
)
def test_w1_round_trip_through_worker_supervisor_and_capture_role(
    rig: Rig,
    service: tuple[RoleRig, CheckDouble],
    outcome: str,
    outputs: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker, checks = service
    worker.clock.now = rig.now
    rig.session.value = replace(
        rig.session.value, blocked_epoch=worker.state.session.snapshot().blocked_epoch
    )
    card, _ = checked_card(rig=worker, checks=checks, req="anchor")
    assert card["active"] and card["variant1"]
    call_id = card["call_id"]
    view = offer_ready(rig)
    supervisor = real_supervisor(rig)
    live_process = Mock()
    live_process.poll.return_value = None
    reader = ipc.FrameReader(role=supervisor.role)
    commands: list[dict[str, object]] = []
    states: list[dict[str, object]] = []
    replies: list[dict[str, object]] = []
    capture_events = CaptureEvents(worker, "prepared", "prepare-failed", "opened", "far-partial")
    archive_started = threading.Event()
    fsync = worker.state.fsync

    def signal_archive_start(descriptor: int) -> None:
        fsync(descriptor)
        archive_started.set()

    def feed(replies: list[dict[str, object]]) -> None:
        for reply in replies:
            states.append(reply)
            receive(supervisor, reply)

    def flush() -> None:
        frame = bytes(supervisor._outgoing)
        supervisor._outgoing.clear()
        for command in reader.feed(frame):
            commands.append(command)
            replies.extend(worker.state.handle(command))

    def deliver_queued() -> None:
        while replies:
            batch = replies[:]
            replies.clear()
            feed(batch)

    with monkeypatch.context() as patch:
        stop = Mock(wraps=rig.recording.controller.stop)
        patch.setattr(rig.recording.controller, "stop", stop)
        patch.setattr(supervisor, "_flush", flush)
        patch.setattr(supervisor, "process", live_process)
        patch.setattr(worker.state, "messages", capture_events)
        patch.setattr(worker.state, "fsync", signal_archive_start)
        patch.setattr(rig.recording.controller, "supervisor", supervisor)
        receive(supervisor, started(call_id))
        rig.recording.tick()
        assert view.calls == [SHOW] and commands == []

        worker.clock.now = START_AT / 1000
        target = pinned_outputs(outputs, worker.clock() + 0.55)
        result = replace(ALLOWED, variant1=outcome == "started", target=target)
        run = checks.plan(result)
        press_start(rig)
        deliver_queued()
        (request,) = commands
        assert request["type"] == PREPARE
        assert request["far"] == {"kind": "app_stream", "call_id": call_id}
        assert view.calls == [SHOW, ("starting",)]
        assert not worker.mics and not worker.backend.sources
        run.started()
        worker.clock.now += 0.55
        rig.now = worker.clock()
        run.complete()
        if outcome == "started":
            capture_events.wait("prepared")
            feed(worker.poll())
            deliver_queued()
            capture_events.wait("opened")
            opened = worker.poll()
            # The first track fsync follows publication of the open boundary.
            # Serialize that archive write with the GUI's metadata write in this IPC test.
            assert archive_started.wait(3)
            feed(opened)
            deliver_queued()
            assert rig.recording.overlay["state"] == "recording", (
                rig.recording.controller.error_code,
                stop.call_args_list,
                [(state["type"], state.get("code"), state.get("reason")) for state in states],
            )
            assert worker.backend.source_targets == [target]
            assert worker.backend.source_targets[0] is target
            if outputs == 3:
                worker.backend.sources[0].losses.put(target.streams[1:2])
                capture_events.wait("far-partial")
                feed(worker.poll())  # WorkerSupervisor reads and decodes the actual IPC frame.
                assert rig.recording.overlay["state"] == "far-partial"
                assert rig.recording.overlay["secondary"] == "Часть звука не записывается"
            assert not rig.bridge.startDialogOpen
            assert [command["type"] for command in commands] == [PREPARE, OPEN]
        else:
            feed(worker.poll())
            assert any(reply.get("code") == "target-mismatch" for reply in states)
            assert_call_lost(rig)
            assert [command["type"] for command in commands] == [PREPARE]
            assert not worker.mics and not worker.backend.sources
        assert view.calls == [SHOW, ("starting",), ("close",)]
        assert supervisor.ready and not supervisor.exited
        live_process.kill.assert_not_called()
        assert not supervisor._pending
        assert all(reply.get("code") != "timeout" for reply in states)


if __name__ == "__main__":
    mode, directory = sys.argv[1:]
    keys = {"return": Qt.Key_Return, "keypad-enter": Qt.Key_Enter, "space": Qt.Key_Space}
    get_qapplication()
    probe_rig = Rig(Path(directory))
    try:
        w4_held_key(probe_rig, keys[mode])
    finally:
        probe_rig.bridge.shutdown()
