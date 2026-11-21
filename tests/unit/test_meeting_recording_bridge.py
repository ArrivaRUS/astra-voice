"""M2 click-only GUI integration with real controller and fake platform."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest
from PyQt5.QtCore import QObject, pyqtSignal

from astra_voice.core.command_mode import SessionSnapshot
from astra_voice.core.meeting_session import StorageStatus
from astra_voice.core.policy import Policy
from astra_voice.core.settings import Settings
from astra_voice.meetings.apps import AppClass, AppDefinition
from astra_voice.meetings.events import DETECTOR_CODE, MeetingState, log_arguments
from astra_voice.meetings.model import Meeting
from astra_voice.meetings.segments import CaptureJournal
from astra_voice.meetings.store import MeetingStore, Writer
from astra_voice.platform.session import SessionKind
from astra_voice.ui import meeting_recording
from astra_voice.ui.meeting_bridge import MeetingBridge
from astra_voice.ui.meeting_offer import OfferView
from astra_voice.ui.meeting_recording import AudioHints, MeetingRecording
from astra_voice.worker import ipc
from astra_voice.worker.supervisor import WorkerSupervisor

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("filesystem_euid")]
MID = "2026-11-01_120000-abcd"


class Session(QObject):
    changed = pyqtSignal(object)

    def __init__(self) -> None:
        super().__init__()
        self.value = SessionSnapshot(known=True, locked=False, blocked_epoch=2)
        self.starts = self.closes = 0

    def snapshot(self) -> SessionSnapshot:
        return self.value

    def start(self) -> None:
        self.starts += 1

    def close(self) -> None:
        self.closes += 1


class Supervisor:
    generation = 1

    def __init__(
        self, event: Callable[[dict[str, Any]], None], exited: Callable[[int], None]
    ) -> None:
        self.event, self.on_exit = event, exited
        self.ready = False
        self.exited = True
        self.messages: list[dict[str, Any]] = []
        self.starts = self.kills = 0

    def start(self) -> None:
        self.starts += 1
        self.ready, self.exited = True, False

    def send(self, message: dict[str, Any], *, timeout: float | None = None) -> dict[str, Any]:
        assert self.ready
        ipc.decode(ipc.encode(message, role="meeting-capture")[4:], role="meeting-capture")
        self.messages.append(message)
        return message

    def set_meeting_generation(self, mid: str, generation: int) -> None:
        pass

    def pump(self, timeout: float = 0.0) -> None:
        pass

    def kill(self) -> None:
        self.kills += 1
        self.ready, self.exited = False, True
        self.on_exit(3)


class Rig:
    def __init__(
        self,
        root: Path,
        *,
        kind: SessionKind = SessionKind.KDE,
        role_type: type[Supervisor] = Supervisor,
        settings: Settings | None = None,
        bind_recording: bool = True,
    ) -> None:
        self.now = 100.0
        self.session = Session()
        self.storage = StorageStatus(1000, True, False)
        self.audio = AudioHints()
        self.microphone = "test-mic"
        self.store = MeetingStore(root / "meetings")
        self.supervisors: list[Supervisor] = []
        self.bridge = MeetingBridge(
            settings if settings is not None else Settings(meetings_enabled=True),
            store=self.store,
            session=kind,
            save=lambda settings: None,
            known_apps=(AppDefinition("dion", "DION", ("dion",), (), AppClass.DION),),
        )

        def factory(
            event: Callable[[dict[str, Any]], None], exit: Callable[[int], None]
        ) -> Supervisor:
            supervisor = role_type(event, exit)
            self.supervisors.append(supervisor)
            return supervisor

        self.recording = MeetingRecording(
            self.store,
            enabled=lambda: self.bridge.enabled,
            policy_allowed=lambda: self.bridge.canEnable,
            detector_allowed=self.bridge.detector_allowed,
            session_kind=kind,
            session_factory=lambda: self.session,
            supervisor_factory=factory,
            clock=lambda: self.now,
            storage_probe=lambda path: self.storage,
            audio_probe=lambda: self.audio,
            microphone=lambda: self.microphone,
            submit=lambda job: job(),
            id_factory=lambda: MID,
            # One exit budget incl. the metadata drain (production 3 s). Fake roles
            # never exit by themselves: keep teardown of a live capture short; the
            # metadata-drain tests below set the 1 s budget they measure.
            exit_stop_timeout=0.2,
        )
        if bind_recording:
            self.bridge.bindRecording(self.recording)

    @property
    def supervisor(self) -> Supervisor:
        return self.supervisors[-1]

    def call_state(self, app_class: str = "dion", *, req: str | None = None) -> dict[str, Any]:
        if req is None:
            req = next(
                message["req"]
                for message in reversed(self.supervisor.messages)
                if message["type"] == "meeting.call.query"
            )
        return {
            "type": "meeting.call.state",
            "request_type": "meeting.call.query",
            "req": req,
            "active": True,
            "app_class": app_class,
            "call_id": "call1",
            "variant1": True,
            "measured_mono_ms": 100000,
            "generation": self.supervisor.generation,
        }

    def reply(self, action: str, **fields: Any) -> None:
        request = self.supervisor.messages[-1]
        event = {
            "type": "meeting.capture." + action,
            "meeting_id": MID,
            "meeting_generation": 0,
            "capture_id": "capture1",
            "state_rev": 0,
        }
        if action in {"prepared", "started", "paused"}:
            event.update(
                req=request["req"],
                request_type=request["type"],
                pause_reasons=[],
                manual_resume_required=False,
                blocked_epoch=1,
            )
        if action == "prepared":
            event.update(
                mic_label="", server_local=True, server_kind="pipewire", far_available=True
            )
        elif action == "started":
            event.update(t0_mono_ms=100000, tracks=["mic"], state_rev=1)
        event.update(fields)
        ipc.decode(ipc.encode(event, role="meeting-capture")[4:], role="meeting-capture")
        self.supervisor.event({**event, "generation": 1})


def capture_error(rig: Rig, action: str, code: str) -> dict[str, Any]:
    request_type = f"meeting.capture.{action}"
    request = next(m for m in reversed(rig.supervisor.messages) if m["type"] == request_type)
    return {
        "type": "error",
        "request_type": request_type,
        "req": request["req"],
        "meeting_id": MID,
        "meeting_generation": 0,
        "code": code,
        "message": "meeting-command-failed",
    }


@pytest.fixture
def rig(tmp_path: Path) -> Iterator[Rig]:
    from helpers.qt_app import get_qapplication

    get_qapplication()
    instance = Rig(tmp_path)
    yield instance
    instance.bridge.shutdown()


def test_bridge_w4_opens_without_prepare_and_only_start_click_records(rig: Rig) -> None:
    assert not rig.supervisor.messages and rig.supervisor.starts == 0
    rig.bridge.requestRecording()
    assert rig.bridge.startDialogOpen and rig.bridge.startState == "default"
    assert [row["id"] for row in rig.bridge.startOptions] == ["room", "all"]
    assert not any(
        message["type"].endswith(("prepare", "open")) for message in rig.supervisor.messages
    )
    rig.bridge.on_start_clicked()
    assert rig.supervisor.messages[-1]["type"] == "meeting.capture.prepare"
    assert rig.supervisor.messages[-1]["mic_device"] == "test-mic"
    rig.reply("prepared")
    assert rig.supervisor.messages[-1]["type"] == "meeting.capture.open"
    rig.reply("started")
    assert not rig.bridge.startDialogOpen and rig.bridge.recording
    assert rig.bridge.meetings[0]["badge"].startswith("● Запись")
    rig.now += 1
    rig.recording.tick()
    assert rig.supervisor.messages[-1]["type"] == "meeting.capture.keepalive"


@pytest.mark.parametrize("gate", ["setting", "policy"])
def test_setting_or_policy_off_keeps_programmatic_w4_and_start_inert(rig: Rig, gate: str) -> None:
    # The only switches are the setting (D1 accepted) and the administrator policy.
    from astra_voice.core.policy import Policy

    if gate == "setting":
        rig.recording.enabled = lambda: False
    else:
        rig.bridge.refreshPolicy(Policy(raw={"meeting_record": "off"}))
    rig.bridge.requestRecording()
    rig.bridge.on_start_clicked()
    assert rig.bridge.recordingAvailable and not rig.bridge.startDialogOpen
    assert not rig.supervisor.messages and rig.supervisor.starts == 0


def test_current_card_enables_call_and_starts_app_stream_on_click(rig: Rig) -> None:
    rig.bridge.requestRecording()
    rig.supervisor.event(rig.call_state())
    assert rig.recording.call_id == "call1" and rig.recording.variant1
    assert [row["id"] for row in rig.bridge.startOptions] == ["call", "room", "all"]
    assert rig.bridge.selectedSource == "call"
    rig.bridge.on_start_clicked()
    assert rig.supervisor.messages[-1]["far"] == {"kind": "app_stream", "call_id": "call1"}


@pytest.mark.parametrize("app_class", ["dion", "browser"])
def test_only_correlated_call_state_enables_app_stream(rig: Rig, app_class: str) -> None:
    rig.bridge.requestRecording()
    event = rig.call_state(app_class)
    rig.supervisor.event({**event, "req": "stale"})
    assert not rig.recording.call_id and rig.bridge.selectedSource == "room"
    rig.supervisor.event({**event, "generation": rig.supervisor.generation - 1})
    assert not rig.recording.call_id and rig.bridge.selectedSource == "room"
    rig.supervisor.event(event)
    assert rig.bridge.selectedSource == "call"
    rows = rig.bridge.startOptions
    assert [row["id"] for row in rows] == ["call", "room", "all"] and rows[0]["enabled"]
    assert rig.bridge.startState == ("browser" if app_class == "browser" else "call")
    rig.bridge.on_start_clicked()
    assert rig.supervisor.messages[-1]["far"] == {"kind": "app_stream", "call_id": "call1"}


@pytest.mark.parametrize("observing", [False, True])
def test_closed_w4_ignores_call_state_even_with_its_own_req(rig: Rig, observing: bool) -> None:
    view = Mock(spec=OfferView)
    rig.recording.bind_offer(view)
    if observing:
        rig.bridge.setSetting("meeting_offer_on_call", True)
        rig.recording.tick()
        deliver_detector(rig.supervisor, rig.supervisor.messages[-1], "meeting.detector.started")
    rig.bridge.requestRecording()
    event = rig.call_state()
    rig.recording.dialog_open = False
    rig.supervisor.event(event)
    rig.recording.tick()
    assert not rig.recording.call_id
    view.show.assert_not_called()
    if observing:
        rig.session.value = SessionSnapshot(known=True, locked=True)
        rig.recording.tick()
        rig.session.value = SessionSnapshot(known=True, locked=False)
        rig.recording.tick()
        rig.now = 105.0
        rig.recording.tick()
        req = rig.recording._offer_query_req
        assert req and req != event["req"]
        rig.supervisor.event({**rig.call_state(req=req), "measured_mono_ms": 105000})
        rig.recording.tick()
        view.show.assert_called_once_with("call-app", 60)
        assert not rig.recording.call_id


@pytest.mark.parametrize(
    "event",
    [
        {
            "type": "meeting.call.started",
            "call_id": "call1",
            "app_class": "dion",
            "variant1": True,
            "measured_mono_ms": 100000,
        },
        {"type": "meeting.call.ended", "call_id": "call1", "reason": "detector_off"},
        {
            "type": "meeting.detector.code",
            "purpose": "observe",
            "code": "identity_mismatch_counter",
            "duration_ms": 7,
        },
    ],
    ids=["call-started", "call-ended", "detector-code"],
)
def test_call_and_detector_events_are_accepted_only_from_current_generation(
    rig: Rig,
    event: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    rig.bridge.requestRecording()
    dispatch = Mock()
    delivered = Mock(wraps=rig.recording._event)
    role = WorkerSupervisor(delivered, role="meeting-capture", use_qt=False)
    role.state = "running"
    role.generation = 2
    frame = ipc.encode(event, role="meeting-capture")
    caplog.set_level(logging.INFO, logger=meeting_recording.__name__)
    with monkeypatch.context() as patch:
        patch.setattr(rig.recording.controller, "handle_event", dispatch)
        patch.setattr(rig.recording.controller, "supervisor", role)
        hello = ipc.encode(ipc.make_hello(role=role.role), role=role.role)
        role._receive(hello, role.generation)
        assert role.ready
        delivered.reset_mock()
        dispatch.reset_mock()
        role.dropped_late = 0
        role._receive(frame, role.generation - 1)
        delivered.assert_not_called()
        assert role.dropped_late == 1
        role._receive(frame, role.generation)
    delivered.assert_called_once_with({**event, "generation": role.generation})
    dispatch.assert_not_called()
    assert not rig.recording.call_id and rig.bridge.selectedSource == "room"
    records = [entry for entry in caplog.records if entry.name == meeting_recording.__name__]
    if event["type"] == "meeting.detector.code":
        (record,) = records
        assert record.msg == DETECTOR_CODE
        assert log_arguments(record.msg, record.args) == ("observe", "identity_mismatch_counter", 7)
    else:
        assert not records


@pytest.mark.parametrize("kind", [SessionKind.KDE, SessionKind.FLY])
def test_w4_preflight_warnings_are_visible_before_prepare(rig: Rig, kind: SessionKind) -> None:
    rig.recording.session_kind = kind
    rig.storage = StorageStatus(1000, True, True)
    rig.audio = AudioHints(remote=True, speakers=True)
    rig.bridge.requestRecording()
    assert len(rig.bridge.startWarnings) == 2 and rig.bridge.startSpeakers
    assert rig.bridge.startCanStart
    assert not any(m["type"].endswith("prepare") for m in rig.supervisor.messages)


def test_fly_unknown_lock_has_no_warning_and_allows_start(tmp_path: Path) -> None:
    from helpers.qt_app import get_qapplication

    get_qapplication()
    rig = Rig(tmp_path, kind=SessionKind.FLY)
    try:
        rig.session.value = SessionSnapshot(known=False, supported=False)
        rig.bridge.requestRecording()
        assert rig.bridge.startWarnings == []
        assert rig.bridge.startCanStart
        rig.bridge.on_start_clicked()
        assert rig.recording.controller.lock_signal_unavailable
        assert rig.recording.controller.warnings == []
        assert rig.supervisor.messages[-1]["type"] == "meeting.capture.prepare"
        rig.reply("prepared")
        assert rig.supervisor.messages[-1]["type"] == "meeting.capture.open"
    finally:
        rig.bridge.shutdown()


@pytest.mark.parametrize(
    "storage,state",
    [
        (StorageStatus(599, True, False), "error-disk"),
        (StorageStatus(1000, False, False), "error-folder"),
    ],
)
def test_preflight_errors_do_not_create_meeting(
    rig: Rig, storage: StorageStatus, state: str
) -> None:
    rig.storage = storage
    rig.bridge.requestRecording()
    assert rig.bridge.startState == state and not rig.bridge.startCanStart
    assert rig.store.list() == []


def test_session_monitor_pauses_and_unlock_does_not_resume(rig: Rig) -> None:
    rig.bridge.requestRecording()
    rig.bridge.on_start_clicked()
    rig.reply("prepared")
    rig.reply("started")
    rig.session.value = SessionSnapshot(known=True, locked=True, blocked_epoch=3)
    rig.session.changed.emit(rig.session.value)
    assert rig.supervisor.messages[-1]["type"] == "meeting.capture.pause"
    assert rig.supervisor.messages[-1]["reason"] == "lock"
    rig.session.value = SessionSnapshot(known=True, locked=False, blocked_epoch=3)
    rig.session.changed.emit(rig.session.value)
    assert not any(m["type"] == "meeting.capture.resume" for m in rig.supervisor.messages)


def test_recovery_dialog_never_starts_capture(rig: Rig) -> None:
    rig.store.create(
        Meeting(id=MID, started_at="2026-11-01T12:00:00Z", state=MeetingState.RECORDING)
    )
    rig.store.write(
        MID,
        "capture.json",
        CaptureJournal(
            MID,
            0,
            "old",
            boundaries=[
                {"kind": "open", "t_mono_ms": 100000, "recorded_ms": 0, "reason": None},
                {"kind": "close", "t_mono_ms": 105000, "recorded_ms": 5000, "reason": "stop"},
            ],
        ),
        writer=Writer.CAPTURE,
    )
    rig.recording.initialize()
    rig.recording.tick()
    assert rig.bridge.interruptedOpen
    assert rig.bridge.interruptedText == "Запись встречи прервалась. Сохранено 0:00:05"
    rig.bridge.openInterruptedMeetings()
    assert not rig.bridge.interruptedOpen
    assert rig.supervisor.starts == 0 and not rig.supervisor.messages


def test_shutdown_does_not_close_borrowed_session_monitor(rig: Rig) -> None:
    rig.recording.owns_session = False
    rig.bridge.requestRecording()
    rig.bridge.shutdown()
    assert rig.session.closes == 0


def test_timer_intervals_keep_control_responsive(rig: Rig) -> None:
    assert rig.recording._lease_timer.interval() == 1000
    assert rig.recording._control_timer.interval() <= 100


@pytest.mark.parametrize("payload", ["missing-id", "variant-disabled", "other-app"])
def test_incomplete_or_unavailable_call_never_enables_call(rig: Rig, payload: str) -> None:
    rig.bridge.requestRecording()
    event = rig.call_state()
    if payload == "missing-id":
        del event["call_id"]
    elif payload == "variant-disabled":
        event["variant1"] = False
    else:
        event["app_class"] = "other"
    rig.supervisor.event(event)
    rows = rig.bridge.startOptions
    assert [row["id"] for row in rows] == ["room", "all"]
    assert rig.bridge.selectedSource == "room"


def test_no_call_reply_preserves_explicit_whole_output_choice(rig: Rig) -> None:
    rig.bridge.requestRecording()
    rig.bridge.selectRecordingSource("all")
    query = rig.supervisor.messages[-1]
    rig.supervisor.event(
        {
            "type": "meeting.call.state",
            "request_type": query["type"],
            "req": query["req"],
            "active": False,
            "generation": 1,
        }
    )
    assert rig.bridge.selectedSource == "all"


@pytest.mark.parametrize("source", ["all", "room"])
def test_new_remote_warning_requires_new_click_after_confirmed_exit(
    rig: Rig, caplog: pytest.LogCaptureFixture, source: str
) -> None:
    caplog.set_level(logging.DEBUG, logger=meeting_recording.__name__)
    rig.bridge.setSetting("meeting_offer_on_call", True)
    rig.bridge.requestRecording()
    deliver_remote(rig.supervisor)
    rig.bridge.selectRecordingSource(source)
    rig.bridge.on_start_clicked()
    rig.reply("prepared", server_local=False)
    assert not any(m["type"] == "meeting.capture.open" for m in rig.supervisor.messages)
    assert rig.bridge.startState == "warn-remote-audio"
    rig.supervisor.ready, rig.supervisor.exited = False, True
    rig.supervisor.on_exit(0)
    rig.recording.tick()
    assert rig.bridge.startCanStart
    assert len(rig.supervisors) == 1
    assert rig.bridge.selectedSource == "room"
    rig.bridge.selectRecordingSource(source)
    rig.bridge.on_start_clicked()
    assert len(rig.supervisors) == 2
    assert rig.supervisor.messages[-1]["type"] == "meeting.capture.prepare"
    assert rig.supervisor.messages[-1]["far"] == {
        "kind": "sink_monitor" if source == "all" else "none"
    }
    rig.reply("prepared", server_local=False)
    assert rig.supervisor.messages[-1]["type"] == "meeting.capture.open"
    assert not background_commands(rig.supervisor)
    for marker in ("private-pulse-host.example", "private-pulse-path-marker", "PULSE_SERVER"):
        assert marker not in caplog.text


def test_cancel_during_prepare_blocks_late_open(rig: Rig) -> None:
    rig.bridge.requestRecording()
    rig.bridge.on_start_clicked()
    prepare = rig.supervisor.messages[-1]
    rig.bridge.closeStartDialog()
    rig.supervisor.event(
        {
            "type": "meeting.capture.prepared",
            "req": prepare["req"],
            "request_type": prepare["type"],
            "meeting_id": MID,
            "meeting_generation": 0,
            "capture_id": "late",
            "state_rev": 0,
            "blocked_epoch": 1,
            "manual_resume_required": False,
            "pause_reasons": [],
            "mic_label": "",
            "server_local": True,
            "server_kind": "pipewire",
            "far_available": True,
            "generation": 1,
        }
    )
    assert not any(m["type"] == "meeting.capture.open" for m in rig.supervisor.messages)


def test_app_binds_capture_and_borrows_runtime_monitor(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    from unittest.mock import Mock

    from astra_voice import app
    from astra_voice.core import meeting_jobs
    from astra_voice.ui import (
        meeting_notice,
        meeting_notifications,
        meeting_offer_window,
        meeting_overlay,
        meeting_recording,
    )

    bridge = Mock(_store=rig.store)
    runtime = Mock(_command_session=rig.session)
    factory = Mock()
    jobs = Mock()
    offer = Mock()
    with monkeypatch.context() as patch:
        patch.setattr(meeting_recording, "MeetingRecording", factory)
        patch.setattr(meeting_jobs, "MeetingJobs", jobs)
        for module, name, constructor in (
            (meeting_overlay, "MeetingOverlay", Mock()),
            (meeting_notice, "MeetingNotice", Mock()),
            (meeting_offer_window, "MeetingOffer", offer),
            (meeting_notifications, "MeetingNotifications", Mock()),
        ):
            patch.setattr(module, name, constructor)
        app._bind_meeting_recording(bridge, Mock(device="chosen-mic"), SessionKind.KDE, runtime)
    options = factory.call_args.kwargs
    assert options["owns_session"] is False
    assert options["session_factory"]() is rig.session
    runtime._ensure_session_monitor.assert_called_once_with()
    assert options["microphone"]() == "chosen-mic"
    assert options["detector_allowed"] is bridge.detector_allowed
    assert "dev_enabled" not in options
    bridge.bindRecording.assert_called_once_with(factory.return_value)
    # No dev gate: overlay, notices and the transcription queue are always bound;
    # whether anything starts is decided by the setting and the policy (enabled).
    factory.return_value.bind_jobs.assert_called_once_with(jobs.return_value)
    factory.return_value.bind_indicators.assert_called_once()
    factory.return_value.initialize.assert_called_once_with()
    offer.assert_called_once_with(factory.return_value, parent=bridge)
    factory.return_value.bind_offer.assert_called_once_with(offer.return_value)
    assert bridge._meeting_offer is offer.return_value


@pytest.mark.parametrize(
    "broken", ["MeetingOverlay", "MeetingNotice", "MeetingOffer", "MeetingJobs"]
)
def test_app_keeps_running_when_meeting_parts_fail_to_build_i2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, broken: str
) -> None:
    from unittest.mock import Mock

    from astra_voice import app
    from astra_voice.core import meeting_jobs
    from astra_voice.ui import (
        meeting_notice,
        meeting_notifications,
        meeting_offer_window,
        meeting_overlay,
        meeting_recording,
    )

    bridge = MeetingBridge(
        Settings(meetings_enabled=True), store=MeetingStore(tmp_path / "m"), session=SessionKind.KDE
    )
    factory = Mock()
    parts = {
        "MeetingOverlay": (meeting_overlay, Mock()),
        "MeetingNotice": (meeting_notice, Mock()),
        "MeetingOffer": (meeting_offer_window, Mock()),
        "MeetingNotifications": (meeting_notifications, Mock()),
        "MeetingJobs": (meeting_jobs, Mock()),
    }
    # E.g. a build where the overlay QML did not load (ui/overlay.py RuntimeError).
    parts[broken][1].side_effect = RuntimeError("PRIVATE /path/Overlay.qml failed")
    try:
        with monkeypatch.context() as patch, caplog.at_level(logging.WARNING):
            patch.setattr(meeting_recording, "MeetingRecording", factory)
            for name, (module, mock) in parts.items():
                patch.setattr(module, name, mock)
            app._bind_meeting_recording(bridge, Mock(device=""), SessionKind.KDE)
        assert "meeting-recording-unavailable" in caplog.text
        assert "PRIVATE" not in caplog.text
        # No half state: capture is not bound, recording stopped, nothing initialized.
        assert bridge._capture is None and not bridge.recordingAvailable
        factory.return_value.shutdown.assert_called_once_with()
        factory.return_value.initialize.assert_not_called()
        assert getattr(bridge, "_meeting_overlay", None) is None
        assert getattr(bridge, "_meeting_offer", None) is None
        for name, (_module, mock) in parts.items():
            if name != broken and mock.called:
                mock.return_value.deleteLater.assert_called_once_with()
        # The rest of the section still works.
        assert bridge.enabled and bridge.meetings == []
    finally:
        bridge.shutdown()


def test_native_supervisor_launch_failure_is_visible_and_retryable(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    from astra_voice.worker.supervisor import WorkerSupervisor

    def fail() -> None:
        raise OSError("private-marker")

    supervisor = WorkerSupervisor(rig.recording._event, role="meeting-capture", use_qt=False)
    monkeypatch.setattr(supervisor, "_launch", fail)
    rig.recording.controller.supervisor = supervisor
    rig.bridge.requestRecording()
    assert rig.bridge.startState == "error-audio"
    assert "private-marker" not in rig.bridge.startError
    assert not rig.bridge.startCanStart


def test_closed_before_finalized_row_is_stopped_not_live(rig: Rig) -> None:
    rig.bridge.requestRecording()
    rig.bridge.on_start_clicked()
    rig.reply("prepared")
    rig.reply("started")
    rig.bridge.requestMeetingAction(MID, "Остановить")
    rig.reply("closed", tracks=["mic"], state_rev=2)
    row = rig.bridge.meetings[0]
    assert row["badge"] == "Сохраняю запись…"
    assert not row["active"] and not row["actionEnabled"]
    assert not rig.recording.controller.saved


def test_preview_pending_never_grants_start(rig: Rig) -> None:
    jobs: list[Callable[[], None]] = []
    rig.recording._submit = jobs.append
    rig.bridge.requestRecording()
    assert not rig.bridge.startCanStart
    rig.bridge.on_start_clicked()
    assert not any(m["type"] == "meeting.capture.prepare" for m in rig.supervisor.messages)
    jobs.pop(0)()
    rig.recording.tick()
    assert rig.bridge.startCanStart


def test_policy_revoke_during_start_blocks_open(rig: Rig) -> None:
    from astra_voice.core.policy import Policy

    rig.bridge.requestRecording()
    rig.bridge.on_start_clicked()
    rig.bridge.refreshPolicy(Policy(raw={"meeting_record": "off"}))
    assert not rig.bridge.startDialogOpen
    assert not rig.bridge.startCanStart
    assert rig.supervisor.messages[-1]["type"] == "meeting.capture.shutdown"


def test_late_list_result_cannot_replace_live_paused_overlay(rig: Rig) -> None:
    from copy import deepcopy

    rig.bridge.requestRecording()
    rig.bridge.on_start_clicked()
    rig.reply("prepared")
    rig.reply("started")
    assert rig.recording.controller.meeting is not None
    stale = deepcopy(rig.recording.controller.meeting)
    rig.recording.clicks.on_pause_clicked()
    rig.recording.tick()
    assert rig.bridge.meetings[0]["badge"].startswith("‖ Пауза")
    rig.bridge._queue.put(([stale], ()))
    rig.bridge._poll()
    rig.recording.tick()
    assert rig.bridge.meetings[0]["badge"].startswith("‖ Пауза")


@pytest.mark.parametrize("selection", [None, "room", "all"])
def test_call_default_respects_explicit_choice(rig: Rig, selection: str | None) -> None:
    rig.bridge.requestRecording()
    if selection is not None:
        rig.bridge.selectRecordingSource(selection)
    rig.supervisor.event(rig.call_state())
    assert rig.bridge.selectedSource == (selection or "call")


def test_microphone_choice_frozen_before_async_prepare(rig: Rig) -> None:
    jobs: list[Callable[[], None]] = []
    rig.bridge.requestRecording()
    rig.recording.controller._filesystem_submit = jobs.append
    rig.bridge.on_start_clicked()
    rig.microphone = "replacement-mic"
    jobs.pop(0)()
    rig.recording.tick()
    assert rig.supervisor.messages[-1]["type"] == "meeting.capture.prepare"
    assert rig.supervisor.messages[-1]["mic_device"] == "test-mic"


def test_no_app_split_requires_explicit_new_whole_output_click(rig: Rig) -> None:
    rig.bridge.requestRecording()
    rig.supervisor.event(rig.call_state())
    rig.bridge.on_start_clicked()
    rig.reply("prepared", far_available=False, far_reason="monitor-stream-unsupported")
    assert rig.bridge.startState == "no-app-split"
    row = rig.bridge.startOptions[0]
    assert row["id"] == "call" and row["warning"] and not row["enabled"]
    assert not any(m["type"] == "meeting.capture.open" for m in rig.supervisor.messages)
    rig.reply("closed", tracks=[], state_rev=1)
    rig.reply(
        "finalized", segments={"mic": 0, "far": 0}, intervals=[], status="succeeded", state_rev=1
    )
    rig.supervisor.ready, rig.supervisor.exited = False, True
    rig.supervisor.on_exit(0)
    rig.recording.tick()
    assert rig.bridge.startCanStart and rig.bridge.selectedSource == "room"
    assert not any(m["type"] == "meeting.capture.open" for m in rig.supervisor.messages)
    rig.bridge.selectRecordingSource("all")
    assert rig.bridge.startCanStart and len(rig.supervisors) == 1
    rig.bridge.on_start_clicked()
    assert rig.supervisor.messages[-1]["far"] == {"kind": "sink_monitor"}


def test_audio_preview_reads_metadata_with_clean_host_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json
    import shutil
    import subprocess

    from astra_voice.ui import meeting_recording

    calls: list[tuple[list[str], dict[str, Any]]] = []
    monkeypatch.setenv("PULSE_CLIENTCONFIG", "/private/isolated-config")
    monkeypatch.setenv("LD_LIBRARY_PATH", "/bundle")
    monkeypatch.setenv("ASTRA_VOICE_ORIG_LD_LIBRARY_PATH", "/host")
    monkeypatch.setattr(shutil, "which", lambda *args, **kwargs: "/usr/bin/pactl")

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        calls.append((command, kwargs))
        payload: Any = (
            {"server_string": "tcp:private-host", "default_sink_name": "private-sink"}
            if command[-1] == "info"
            else [{"name": "private-sink", "active_port": "analog-output-speaker"}]
        )
        return subprocess.CompletedProcess(command, 0, json.dumps(payload).encode(), b"")

    monkeypatch.setattr(subprocess, "run", run)
    assert meeting_recording.probe_audio() == AudioHints(remote=True, speakers=True)
    assert calls[0][0][-4:] == ["/usr/bin/pactl", "-f", "json", "info"]
    assert calls[1][0][-2:] == ["list", "sinks"]
    assert calls[0][1]["env"]["LD_LIBRARY_PATH"] == "/host"
    assert calls[0][1]["env"]["PULSE_CLIENTCONFIG"] == "/private/isolated-config"
    assert calls[0][1]["timeout"] <= 1


def test_enabled_recording_never_launches_capture_before_a_click(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The former dev variable no longer exists: it neither enables nor blocks anything.
    monkeypatch.setenv("ASTRA_VOICE_MEETING_CAPTURE_DEV", "0")
    service = MeetingRecording(
        rig.store,
        enabled=lambda: True,
        policy_allowed=lambda: True,
        session_kind=SessionKind.KDE,
        supervisor_factory=lambda event, exit: Supervisor(event, exit),
        submit=lambda job: job(),
    )
    try:
        service.initialize()
        assert not hasattr(service, "dev_enabled")
        assert isinstance(service.controller.supervisor, Supervisor)
        assert service.controller.supervisor.starts == 0
        assert service.controller.supervisor.messages == []
    finally:
        service.shutdown()


@pytest.mark.parametrize("entry", ["retry", "reopen"])
def test_fresh_standby_role_after_exit_accepts_next_start_click(rig: Rig, entry: str) -> None:
    rig.bridge.requestRecording()
    rig.bridge.on_start_clicked()
    rig.reply("prepared", server_local=False)
    previous = rig.supervisor
    previous.ready, previous.exited = False, True
    previous.on_exit(0)
    rig.recording.tick()
    if entry == "retry":
        rig.bridge.retryStart()
    else:
        rig.bridge.closeStartDialog()
        rig.bridge.requestRecording()
    assert len(rig.supervisors) == 2 and rig.supervisor is not previous
    assert rig.recording.controller.state == "stopped"
    assert rig.supervisor.ready and rig.bridge.startCanStart
    assert not any(m["type"] == "meeting.capture.prepare" for m in rig.supervisor.messages)
    rig.bridge.on_start_clicked()
    assert len(rig.supervisors) == 2
    assert rig.supervisor.messages[-1]["type"] == "meeting.capture.prepare"


class DelayedSupervisor(Supervisor):
    """Hello and observed process exit are independent, as in the real supervisor."""

    def start(self) -> None:
        self.starts += 1
        self.ready, self.exited = False, False

    def kill(self) -> None:
        self.kills += 1
        self.ready = False


@pytest.fixture
def delayed_rig(tmp_path: Path) -> Iterator[Rig]:
    from helpers.qt_app import get_qapplication

    get_qapplication()
    instance = Rig(tmp_path, role_type=DelayedSupervisor)
    yield instance
    instance.bridge.shutdown()


DETECTOR_START = "meeting.detector.start"
DETECTOR_STOP = "meeting.detector.stop"


def detector_commands(role: Supervisor) -> list[dict[str, Any]]:
    return [msg for msg in role.messages if msg["type"] in {DETECTOR_START, DETECTOR_STOP}]


def background_commands(role: Supervisor) -> list[str]:
    return [
        message["type"]
        for message in role.messages
        if message["type"] in {DETECTOR_START, "meeting.call.query"}
    ]


def deliver_remote(role: Supervisor, *, purpose: str = "observe", stale: bool = False) -> None:
    role.event(
        {
            "type": "meeting.detector.code",
            "purpose": purpose,
            "code": "remote",
            "duration_ms": 0,
            "generation": role.generation - int(stale),
        }
    )


@pytest.mark.parametrize("purpose", ["observe", "offer", "card", "start"])
def test_remote_stops_start_and_query_for_current_generation(rig: Rig, purpose: str) -> None:
    rig.bridge.requestRecording()
    role = rig.supervisor
    pending = rig.call_state()
    assert background_commands(role) == ["meeting.call.query"]
    deliver_remote(role, purpose=purpose)
    role.event(pending)
    assert not rig.recording.call_id
    rig.bridge.setSetting("meeting_offer_on_call", True)
    rig.recording.tick()
    assert background_commands(role) == ["meeting.call.query"]


def test_remote_keeps_detector_stop_available(rig: Rig) -> None:
    rig.bridge.setSetting("meeting_offer_on_call", True)
    rig.bridge.requestRecording()
    role = rig.supervisor
    (start,) = detector_commands(role)
    deliver_detector(role, start, "meeting.detector.started")
    deliver_remote(role, purpose="card")
    rig.bridge.setSetting("meeting_offer_on_call", False)
    assert [message["type"] for message in detector_commands(role)] == [
        DETECTOR_START,
        DETECTOR_STOP,
    ]


@pytest.mark.parametrize("remote", ["current", "stale", "none", "ordinary"])
@pytest.mark.parametrize("replace", ["reopen", "exit-with-open-dialog", "after-capture"])
def test_remote_latch_survives_role_replacement(
    rig: Rig, remote: str, replace: str, caplog: pytest.LogCaptureFixture
) -> None:
    rig.bridge.setSetting("meeting_offer_on_call", True)
    rig.bridge.requestRecording()
    previous = rig.supervisor
    expected = [DETECTOR_START, "meeting.call.query"]
    assert background_commands(previous) == expected
    if remote == "ordinary":
        with caplog.at_level(logging.INFO, logger=meeting_recording.__name__):
            previous.event(
                {
                    "type": "meeting.detector.code",
                    "purpose": "observe",
                    "code": "identity_mismatch_counter",
                    "duration_ms": 7,
                    "generation": previous.generation,
                }
            )
        (record,) = [entry for entry in caplog.records if entry.msg == DETECTOR_CODE]
        assert log_arguments(record.msg, record.args) == ("observe", "identity_mismatch_counter", 7)
        assert not rig.recording._automation_off
    elif remote != "none":
        deliver_remote(previous, stale=remote == "stale")

    if replace == "after-capture":
        rig.bridge.on_start_clicked()
        rig.reply("prepared")
        rig.reply("started")
        assert rig.recording.controller.state == "recording"
        rig.recording.controller.stop()
        finish_capture_close(rig)
    else:
        rig.bridge.closeStartDialog()
        if replace == "exit-with-open-dialog":
            rig.bridge.requestRecording()
            assert rig.supervisor is previous and rig.bridge.startDialogOpen
    previous.ready, previous.exited = False, True
    previous.on_exit(0)
    if replace != "exit-with-open-dialog":
        rig.bridge.requestRecording()
    elif remote != "current":
        # Closing W4 left observation running. Its unexpected exit backs off.
        rig.recording.tick()
        assert rig.supervisor is previous
        rig.now += 2
        expected.append("meeting.call.query")
    rig.recording.tick()
    assert rig.supervisor is not previous and rig.supervisor.ready
    assert background_commands(previous) == expected
    assert background_commands(rig.supervisor) == (
        [] if remote == "current" else [DETECTOR_START, "meeting.call.query"]
    )


def detector_reply(request: dict[str, Any], kind: str, **fields: Any) -> dict[str, Any]:
    reply = {"type": kind, "req": request["req"], "request_type": request["type"], **fields}
    if kind == "meeting.detector.started":
        reply["apps_version"] = request["apps_version"]
    if kind == "error":
        reply["message"] = "meeting-command-failed"
    return reply


def deliver_detector(role: Supervisor, request: dict[str, Any], kind: str, **fields: Any) -> None:
    role.event(detector_reply(request, kind, generation=role.generation, **fields))


def start_recording(rig: Rig) -> None:
    rig.bridge.requestRecording()
    rig.bridge.on_start_clicked()
    rig.reply("prepared")
    rig.reply("started")
    assert rig.recording.controller.state == "recording"


def finish_capture_close(rig: Rig) -> None:
    rig.reply("closed", tracks=["mic"], state_rev=2)
    rig.reply(
        "finalized", segments={"mic": 0, "far": 0}, intervals=[], status="succeeded", state_rev=2
    )


@pytest.mark.parametrize("gate", ["off", "manual", "toggle-off"])
def test_detector_does_not_start_when_effectively_disabled(rig: Rig, gate: str) -> None:
    if gate != "toggle-off":
        rig.bridge.setSetting("meeting_offer_on_call", True)
        rig.bridge.refreshPolicy(Policy(raw={"meeting_record": gate}))
    else:
        assert rig.bridge.detectorEnabled  # The UI capability is not the opt-in.
    assert not rig.bridge.detector_allowed()
    rig.bridge.requestRecording()
    rig.recording.tick()
    assert not any(
        message["type"] == "meeting.detector.start"
        for role in rig.supervisors
        for message in role.messages
    )


@pytest.mark.parametrize("change", ["off", "manual", "toggle-off"])
def test_detector_starts_after_hello_once_and_stops_on_effective_change(
    delayed_rig: Rig, change: str
) -> None:
    rig = delayed_rig
    rig.bridge.setSetting("meeting_offer_on_call", True)
    assert rig.bridge.detector_allowed()
    rig.bridge.requestRecording()
    role = rig.supervisor
    assert not role.messages
    role.ready = True
    rig.recording.tick()
    rig.recording.tick()
    started = [message for message in role.messages if message["type"] == "meeting.detector.start"]
    assert [message["apps_version"] for message in started] == [1]
    deliver_detector(role, started[0], "meeting.detector.started")
    assert rig.recording._detector_running is True
    rig.bridge.changed.emit()
    rig.recording.tick()
    assert detector_commands(role) == started

    if change == "toggle-off":
        rig.bridge.setSetting("meeting_offer_on_call", False)
    else:
        rig.bridge.refreshPolicy(Policy(raw={"meeting_record": change}))
    assert not rig.bridge.detector_allowed()
    types = [message["type"] for message in role.messages]
    assert types.count("meeting.detector.stop") == 1
    if change == "off":
        assert types.index("meeting.detector.stop") < types.index("meeting.capture.shutdown")
    else:
        assert "meeting.capture.shutdown" not in types and rig.bridge.startDialogOpen
    rig.recording.tick()
    assert [message["type"] for message in role.messages] == types


@pytest.mark.parametrize("initially_running", [False, True], ids=["start", "stop"])
@pytest.mark.parametrize(
    "state", ["preparing", "opening", "recording", "paused", "stopping", "closing-unknown"]
)
def test_busy_window_suppresses_detector_commands(
    rig: Rig, state: str, initially_running: bool
) -> None:
    rig.bridge.setSetting("meeting_offer_on_call", initially_running)
    rig.bridge.requestRecording()
    role = rig.supervisor
    if initially_running:
        (first,) = detector_commands(role)
        deliver_detector(role, first, "meeting.detector.started")
    rig.bridge.on_start_clicked()
    if state != "preparing":
        rig.reply("prepared")
    if state not in {"preparing", "opening"}:
        rig.reply("started")
    if state == "paused":
        rig.recording.controller.pause()
        finish_capture_close(rig)
    if state in {"stopping", "closing-unknown"}:
        rig.recording.controller.stop()
    if state == "closing-unknown":
        rig.recording.controller.state = state
    assert rig.recording.controller.state == state and rig.recording.busy
    before = detector_commands(role)
    role.messages.clear()

    rig.bridge.setSetting("meeting_offer_on_call", not initially_running)
    rig.bridge.refreshPolicy(Policy(raw={"meeting_record": "manual"}))
    rig.bridge.refreshPolicy(Policy(raw={"meeting_record": "prompt"}))
    if state in {"opening", "recording", "paused"}:
        rig.now += 1.1
        rig.recording.tick()
        assert [
            message["seq"]
            for message in role.messages
            if message["type"] == "meeting.capture.keepalive"
        ]
    else:
        rig.recording.tick()
        assert not any(message["type"] == "meeting.capture.keepalive" for message in role.messages)
    rig.bridge.refreshPolicy(Policy(raw={"meeting_record": "off"}))
    assert len(before) == int(initially_running)
    assert detector_commands(role) == []


@pytest.mark.parametrize("allowed", [True, False])
def test_new_ready_role_syncs_detector_after_capture_ends(rig: Rig, allowed: bool) -> None:
    start_recording(rig)
    previous = rig.supervisor
    rig.bridge.setSetting("meeting_offer_on_call", allowed)
    rig.recording.controller.stop()
    finish_capture_close(rig)
    previous.ready, previous.exited = False, True
    previous.on_exit(0)
    rig.recording.tick()
    assert detector_commands(previous) == []

    rig.bridge.requestRecording()
    assert rig.supervisor is not previous and rig.supervisor.ready
    rig.recording.tick()
    rig.bridge.changed.emit()
    assert [msg["type"] for msg in detector_commands(rig.supervisor)] == (
        [DETECTOR_START] if allowed else []
    )


def test_retiring_role_receives_only_shutdown(delayed_rig: Rig) -> None:
    rig = delayed_rig
    rig.bridge.setSetting("meeting_offer_on_call", True)
    rig.bridge.requestRecording()
    role = rig.supervisor
    role.ready = True
    rig.recording.tick()
    (start,) = detector_commands(role)
    deliver_detector(role, start, "meeting.detector.started")
    rig.bridge.setSetting("meeting_offer_on_call", False)
    assert detector_commands(role)[-1]["type"] == DETECTOR_STOP
    role.ready = False
    rig.bridge.closeStartDialog()
    role.ready = True
    role.messages.clear()
    rig.bridge.setSetting("meeting_offer_on_call", True)
    rig.recording.tick()
    assert [msg["type"] for msg in role.messages] == ["meeting.capture.shutdown"]


def test_sync_limit_retries_detector_with_bounded_backoff(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig.bridge.requestRecording()
    role = rig.supervisor
    send = role.send

    def reject(message: dict[str, Any]) -> dict[str, Any]:
        sent = send(message)
        deliver_detector(role, message, "error", code="limit")
        return sent

    delays = [1, 2, 4, 8, 16, 30, 30]
    with monkeypatch.context() as patch:
        patch.setattr(role, "send", reject)
        rig.bridge.setSetting("meeting_offer_on_call", True)
        for attempt, delay in enumerate(delays, 1):
            assert len(detector_commands(role)) == attempt
            rig.now += delay - 0.25
            rig.recording.tick()
            assert len(detector_commands(role)) == attempt
            rig.now += 0.25
            if attempt < len(delays):
                rig.recording.tick()

    rig.recording.tick()
    commands = detector_commands(role)
    assert len(commands) == len(delays) + 1
    assert {command["type"] for command in commands} == {DETECTOR_START}
    assert len({command["req"] for command in commands}) == len(commands)
    deliver_detector(role, commands[-1], "meeting.detector.started")
    rig.now += 30
    rig.recording.tick()
    assert detector_commands(role) == commands

    with monkeypatch.context() as patch:
        patch.setattr(role, "send", reject)
        rig.bridge.setSetting("meeting_offer_on_call", False)
    (stop,) = detector_commands(role)[len(commands) :]
    assert stop["type"] == DETECTOR_STOP
    rig.now += 0.75
    rig.recording.tick()
    assert detector_commands(role) == [*commands, stop]
    rig.now += 0.25
    rig.recording.tick()
    (retry,) = detector_commands(role)[len(commands) + 1 :]
    assert retry["type"] == DETECTOR_STOP and retry["req"] != stop["req"]


def test_detector_stop_supersedes_start_and_ignores_its_late_reply(rig: Rig) -> None:
    rig.bridge.setSetting("meeting_offer_on_call", True)
    rig.bridge.requestRecording()
    role = rig.supervisor
    (start,) = detector_commands(role)
    rig.bridge.setSetting("meeting_offer_on_call", False)
    assert [msg["type"] for msg in detector_commands(role)] == [DETECTOR_START, DETECTOR_STOP]
    stop = detector_commands(role)[-1]
    deliver_detector(role, start, "meeting.detector.started")
    assert rig.recording._detector_pending == (stop["req"], DETECTOR_STOP)
    assert rig.recording._detector_running is False
    deliver_detector(role, stop, "meeting.detector.stopped")
    rig.recording.tick()
    assert rig.recording._detector_running is False
    assert detector_commands(role) == [start, stop]


def test_detector_timeout_after_ack_retries_stop_when_disallowed(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig.bridge.setSetting("meeting_offer_on_call", True)
    rig.bridge.requestRecording()
    role = rig.supervisor
    (start,) = detector_commands(role)
    dispatch = Mock()
    with monkeypatch.context() as patch:
        patch.setattr(rig.recording.controller, "handle_event", dispatch)
        deliver_detector(role, start, "meeting.detector.started")
        rig.bridge.setSetting("meeting_offer_on_call", False)
        stop = detector_commands(role)[-1]
        assert stop["type"] == DETECTOR_STOP
        deliver_detector(role, stop, "meeting.ack")
        assert rig.recording._detector_pending == (stop["req"], DETECTOR_STOP)
        assert rig.recording._detector_running is True
        deliver_detector(role, stop, "error", code="timeout")
    dispatch.assert_not_called()
    assert rig.recording._detector_running is None
    rig.now += 0.75
    rig.recording.tick()
    assert detector_commands(role) == [start, stop]
    rig.now += 0.25
    rig.recording.tick()
    assert [msg["type"] for msg in detector_commands(role)] == [
        DETECTOR_START,
        DETECTOR_STOP,
        DETECTOR_STOP,
    ]


def test_detector_permanent_error_blocks_this_generation(rig: Rig) -> None:
    rig.bridge.setSetting("meeting_offer_on_call", True)
    rig.bridge.requestRecording()
    role = rig.supervisor
    (start,) = detector_commands(role)
    deliver_detector(role, start, "error", code="not-prepared")
    assert rig.recording._detector_running is None
    for allowed in (True, False, True):
        rig.bridge.setSetting("meeting_offer_on_call", allowed)
        rig.now += 31
        rig.recording.tick()
    assert detector_commands(role) == [start]
    role.generation += 1
    rig.recording.tick()
    assert [msg["type"] for msg in detector_commands(role)] == [DETECTOR_START, DETECTOR_START]


def test_detector_send_exception_can_be_retried(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    rig.bridge.requestRecording()
    role = rig.supervisor
    disconnected = Mock(side_effect=OSError("test-transport"))
    with monkeypatch.context() as patch:
        patch.setattr(role, "send", disconnected)
        rig.bridge.setSetting("meeting_offer_on_call", True)
    disconnected.assert_called_once()
    assert rig.recording._detector_running is None
    assert not detector_commands(role)
    rig.now += 0.75
    rig.recording.tick()
    assert not detector_commands(role)
    rig.now += 0.25
    rig.recording.tick()
    (start,) = detector_commands(role)
    assert start["type"] == DETECTOR_START


def test_detector_ignores_old_generation_reply_and_starts_new_generation_once(rig: Rig) -> None:
    rig.bridge.setSetting("meeting_offer_on_call", True)
    rig.bridge.requestRecording()
    role = rig.supervisor
    (old_start,) = detector_commands(role)
    role.generation += 1
    rig.recording.tick()
    new_start = detector_commands(role)[-1]
    # Keep the current req to isolate the generation check from the req check.
    role.event(
        detector_reply(new_start, "meeting.detector.started", generation=role.generation - 1)
    )
    assert rig.recording._detector_running is False
    assert rig.recording._detector_pending == (new_start["req"], DETECTOR_START)
    deliver_detector(role, new_start, "meeting.detector.started")
    assert rig.recording._detector_running is True
    rig.bridge.changed.emit()
    rig.recording.tick()
    assert detector_commands(role) == [old_start, new_start]
    assert old_start["req"] != new_start["req"]


@pytest.mark.parametrize("gate", ["off", "manual", "toggle-off"])
def test_detector_retries_never_start_after_permission_is_withdrawn(rig: Rig, gate: str) -> None:
    rig.bridge.setSetting("meeting_offer_on_call", True)
    rig.bridge.requestRecording()
    role = rig.supervisor
    deliver_detector(role, detector_commands(role)[-1], "error", code="limit")
    rig.now += 1
    rig.recording.tick()
    deliver_detector(role, detector_commands(role)[-1], "error", code="timeout")
    if gate == "toggle-off":
        rig.bridge.setSetting("meeting_offer_on_call", False)
    else:
        rig.bridge.refreshPolicy(Policy(raw={"meeting_record": gate}))
    assert not rig.bridge.detector_allowed()
    before = detector_commands(role)
    assert [msg["type"] for msg in before] == [DETECTOR_START, DETECTOR_START]
    for _ in range(3):
        rig.now += 31
        rig.recording.tick()
        if gate != "off":
            stop = detector_commands(role)[-1]
            assert stop["type"] == DETECTOR_STOP
            deliver_detector(role, stop, "error", code="limit")
    assert [msg for msg in detector_commands(role) if msg["type"] == DETECTOR_START] == before


def test_detector_retries_after_real_supervisor_queue_limit(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig.bridge.requestRecording()
    on_event = Mock(wraps=rig.recording._event)
    role = WorkerSupervisor(on_event, role="meeting-capture", clock=lambda: rig.now, use_qt=False)
    role.state = "running"
    role._receive(ipc.encode(ipc.make_hello(role=role.role), role=role.role), role.generation)
    assert role.ready
    on_event.reset_mock()
    full_queue = 4 * ipc.MAX_FRAME_BYTES
    role._outgoing.extend(bytes(full_queue))
    with monkeypatch.context() as patch:
        patch.setattr(rig.recording.controller, "supervisor", role)
        rig.bridge.setSetting("meeting_offer_on_call", True)
        on_event.assert_called_once()
        rejected = on_event.call_args.args[0]
        assert {key: rejected[key] for key in ("type", "request_type", "code")} == {
            "type": "error",
            "request_type": DETECTOR_START,
            "code": "limit",
        }
        assert len(role._outgoing) == full_queue and not role._pending
        role._outgoing.clear()
        rig.now += 0.75
        rig.recording.sync_detector()
        assert not role._outgoing
        rig.now += 0.25
        rig.recording.sync_detector()
        assert role._outgoing
        sent = ipc.decode(bytes(role._outgoing[4:]), role=role.role)
        assert sent["type"] == DETECTOR_START and sent["req"] != rejected["req"]
        started = detector_reply(sent, "meeting.detector.started")
        role._receive(ipc.encode(started, role=role.role), role.generation)
        assert not role._pending
        role._outgoing.clear()
        rig.recording.sync_detector()
        assert not role._outgoing


@pytest.mark.parametrize("ready", [False, True], ids=["before-hello", "after-hello"])
@pytest.mark.parametrize(
    ("reason", "observing"),
    [("close", False), ("close", True), ("disabled", False), ("policy", False)],
    ids=["close", "close-with-observation", "disabled", "policy"],
)
def test_unused_role_retires_after_close_or_revocation(
    delayed_rig: Rig, ready: bool, reason: str, observing: bool
) -> None:
    from astra_voice.core.policy import Policy

    rig = delayed_rig
    rig.bridge.setSetting("meeting_offer_on_call", observing)
    rig.bridge.requestRecording()
    role = rig.supervisor
    role.ready = ready
    if reason == "close":
        rig.bridge.closeStartDialog()
    elif reason == "disabled":
        rig.recording.enabled = lambda: False
        rig.recording.policy_changed()
    else:
        rig.bridge.refreshPolicy(Policy(raw={"meeting_record": "off"}))
    role.ready = True
    rig.recording.tick()
    assert not rig.bridge.startDialogOpen
    if observing:
        assert [m["type"] for m in role.messages] == [DETECTOR_START]
        assert rig.session.closes == 0
        assert rig.session.receivers(rig.session.changed) == 1
        return
    # Late hello must finish retirement, never revive discovery/capture.
    assert [m["type"] for m in role.messages] == ["meeting.capture.shutdown"]
    assert rig.session.closes == 1
    assert rig.session.receivers(rig.session.changed) == 0
    rig.recording.tick()
    assert len(role.messages) == 1
    role.ready, role.exited = False, True  # poll-only exit: no on_exit callback
    rig.recording.tick()
    assert not rig.recording._role_started
    assert rig.recording.controller.state == "stopped"


@pytest.mark.parametrize("hello", [False, True], ids=["no-hello", "ignores-shutdown"])
@pytest.mark.parametrize("callback", [False, True], ids=["poll-exit", "callback-exit"])
def test_unused_role_deadline_and_confirmed_exit_gate_replacement(
    delayed_rig: Rig, hello: bool, callback: bool
) -> None:
    rig = delayed_rig
    rig.bridge.requestRecording()
    previous = rig.supervisor
    previous.ready = hello
    rig.bridge.closeStartDialog()
    rig.now += 0.75
    rig.bridge.closeStartDialog()  # repeated close must not extend the deadline
    rig.now += 0.26
    rig.recording.tick()
    assert previous.kills == 1 and not previous.exited
    rig.bridge.requestRecording()
    rig.bridge.on_start_clicked()
    rig.bridge.retryStart()
    rig.recording.tick()
    assert len(rig.supervisors) == 1 and previous.starts == 1
    assert not rig.bridge.startCanStart
    assert previous.kills == 1
    previous.exited = True  # no callback, like WorkerSupervisor.kill
    if callback:
        previous.on_exit(0)
    rig.recording.tick()
    assert len(rig.supervisors) == 2
    assert rig.supervisor.starts == 1 and not rig.supervisor.ready
    rig.supervisor.ready = True
    rig.recording.tick()
    assert rig.bridge.startCanStart  # reopened W4 needs no invisible/manual retry
    assert rig.supervisor.messages[-1]["type"] == "meeting.call.query"
    assert not any(m["type"].endswith(("prepare", "open")) for m in previous.messages)


def test_unused_role_releases_borrowed_subscription_without_closing_monitor(
    delayed_rig: Rig,
) -> None:
    rig = delayed_rig
    rig.recording.owns_session = False
    rig.bridge.requestRecording()
    rig.bridge.closeStartDialog()
    assert rig.session.receivers(rig.session.changed) == 0
    assert rig.session.closes == 0


def test_shutdown_drains_accepted_metadata_before_return(rig: Rig) -> None:
    entered, release, written = threading.Event(), threading.Event(), threading.Event()
    writer = rig.recording.controller._writer
    rig.recording._exit_stop_timeout = 1.0

    def write() -> None:
        entered.set()
        assert release.wait(3)
        written.set()

    writer.submit(write)
    releaser = threading.Timer(0.3, release.set)
    try:
        assert entered.wait(1)
        releaser.start()
        rig.recording.shutdown()
        assert written.is_set(), "shutdown returned before accepted metadata was written"
        with pytest.raises(RuntimeError, match="meeting-writer-closed"):
            writer.submit(lambda: None)
    finally:
        release.set()
        if releaser.ident is not None:
            releaser.join(1)
        assert writer.close(timeout=1)


def test_shutdown_metadata_deadline_warns_once_without_waiting_for_disk(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    entered, release, written = threading.Event(), threading.Event(), threading.Event()
    writer = rig.recording.controller._writer
    rig.recording._exit_stop_timeout = 1.0

    def write() -> None:
        entered.set()
        assert release.wait(3)
        written.set()

    caplog.set_level(logging.WARNING, logger="astra_voice.ui.meeting_recording")
    writer.submit(write)
    try:
        assert entered.wait(1)
        started = time.monotonic()
        rig.recording.shutdown()
        elapsed = time.monotonic() - started
        assert 0.9 <= elapsed < 1.3
        assert not written.is_set()
        rig.recording.shutdown()
        assert [
            (record.levelname, record.getMessage())
            for record in caplog.records
            if record.name == "astra_voice.ui.meeting_recording"
        ] == [("WARNING", "meeting-metadata-close-timeout")]
        with pytest.raises(RuntimeError, match="meeting-writer-closed"):
            writer.submit(lambda: None)
    finally:
        release.set()
        assert writer.close(timeout=1)


@pytest.mark.parametrize("failure", ["kill", "release"])
def test_shutdown_drains_metadata_even_when_other_cleanup_raises(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    entered, release, written = threading.Event(), threading.Event(), threading.Event()
    writer = rig.recording.controller._writer
    rig.recording._exit_stop_timeout = 1.0

    def write() -> None:
        entered.set()
        assert release.wait(3)
        written.set()

    def fail() -> None:
        raise RuntimeError("shutdown-cleanup-failed")

    rig.recording._role_started = True
    if failure == "kill":
        monkeypatch.setattr(rig.supervisor, "kill", fail)
    else:
        monkeypatch.setattr(rig.recording, "_release_session", fail)
    writer.submit(write)
    releaser = threading.Timer(0.05, release.set)
    try:
        assert entered.wait(1)
        releaser.start()
        with pytest.raises(RuntimeError, match="shutdown-cleanup-failed"):
            rig.recording.shutdown()
        assert written.is_set(), "cleanup exception skipped accepted metadata"
        with pytest.raises(RuntimeError, match="meeting-writer-closed"):
            writer.submit(lambda: None)
    finally:
        release.set()
        if releaser.ident is not None:
            releaser.join(1)
        assert writer.close(timeout=1)
