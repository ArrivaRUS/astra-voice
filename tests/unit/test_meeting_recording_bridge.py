"""M2 click-only GUI integration with real controller and fake platform."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from PyQt5.QtCore import QObject, pyqtSignal

from astra_voice.core.command_mode import SessionSnapshot
from astra_voice.core.meeting_session import StorageStatus
from astra_voice.core.settings import Settings
from astra_voice.meetings.events import MeetingState
from astra_voice.meetings.model import Meeting
from astra_voice.meetings.segments import CaptureJournal
from astra_voice.meetings.store import MeetingStore, Writer
from astra_voice.platform.session import SessionKind
from astra_voice.ui.meeting_bridge import MeetingBridge
from astra_voice.ui.meeting_recording import AudioHints, MeetingRecording
from astra_voice.worker import ipc

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
    ) -> None:
        self.now = 100.0
        self.session = Session()
        self.storage = StorageStatus(1000, True, False)
        self.audio = AudioHints()
        self.microphone = "test-mic"
        self.store = MeetingStore(root / "meetings")
        self.supervisors: list[Supervisor] = []
        self.bridge = MeetingBridge(Settings(meetings_enabled=True), store=self.store, session=kind)

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
        self.bridge.bindRecording(self.recording)

    @property
    def supervisor(self) -> Supervisor:
        return self.supervisors[-1]

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


@pytest.mark.parametrize("app_class", ["dion", "browser"])
def test_only_correlated_validated_target_enables_call(rig: Rig, app_class: str) -> None:
    rig.bridge.requestRecording()
    rig.recording.tick()
    query = rig.supervisor.messages[-1]
    assert query["type"] == "meeting.call.query"
    event = {
        "type": "meeting.call.state",
        "request_type": query["type"],
        "req": query["req"],
        "active": True,
        "app_class": app_class,
        "call_id": "call1",
        "target": {"sink_input": 7, "object_serial": 19, "verified": "declared-verified"},
        "generation": 1,
    }
    rig.supervisor.event({**event, "req": "stale"})
    assert len(rig.bridge.startOptions) == 2
    rig.supervisor.event(event)
    assert len(rig.bridge.startOptions) == 3
    rig.bridge.selectRecordingSource("call")
    assert rig.bridge.startState == ("browser" if app_class == "browser" else "call")
    rig.bridge.on_start_clicked()
    assert rig.supervisor.messages[-1]["far"]["sink_input"] == 7


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


@pytest.mark.parametrize("payload", ["missing-id", "mismatch", "unverified"])
def test_incomplete_or_unverified_target_never_enables_call(rig: Rig, payload: str) -> None:
    rig.bridge.requestRecording()
    query = rig.supervisor.messages[-1]
    event = {
        "type": "meeting.call.state",
        "request_type": query["type"],
        "req": query["req"],
        "active": True,
        "app_class": "dion",
        "target": {"sink_input": 7, "object_serial": 19, "verified": "declared-verified"},
        "generation": 1,
    }
    if payload != "missing-id":
        event["call_id"] = "call1"
        event["target"]["verified"] = payload
    rig.supervisor.event(event)
    assert len(rig.bridge.startOptions) == 2


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


def test_new_remote_warning_requires_new_click_after_confirmed_exit(rig: Rig) -> None:
    rig.bridge.requestRecording()
    rig.bridge.on_start_clicked()
    rig.reply("prepared", server_local=False)
    assert not any(m["type"] == "meeting.capture.open" for m in rig.supervisor.messages)
    assert rig.bridge.startState == "warn-remote-audio"
    rig.supervisor.ready, rig.supervisor.exited = False, True
    rig.supervisor.on_exit(0)
    rig.recording.tick()
    assert rig.bridge.startCanStart
    assert len(rig.supervisors) == 1
    rig.bridge.on_start_clicked()
    assert len(rig.supervisors) == 2
    assert rig.supervisor.messages[-1]["type"] == "meeting.capture.prepare"


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
        meeting_overlay,
        meeting_recording,
    )

    bridge = Mock(_store=rig.store)
    runtime = Mock(_command_session=rig.session)
    factory = Mock()
    monkeypatch.setattr(meeting_recording, "MeetingRecording", factory)
    jobs = Mock()
    monkeypatch.setattr(meeting_jobs, "MeetingJobs", jobs)
    for module, name in (
        (meeting_overlay, "MeetingOverlay"),
        (meeting_notice, "MeetingNotice"),
        (meeting_notifications, "MeetingNotifications"),
    ):
        monkeypatch.setattr(module, name, Mock())
    app._bind_meeting_recording(bridge, Mock(device="chosen-mic"), SessionKind.KDE, runtime)
    options = factory.call_args.kwargs
    assert options["owns_session"] is False
    assert options["session_factory"]() is rig.session
    runtime._ensure_session_monitor.assert_called_once_with()
    assert options["microphone"]() == "chosen-mic"
    assert "dev_enabled" not in options
    bridge.bindRecording.assert_called_once_with(factory.return_value)
    # No dev gate: overlay, notices and the transcription queue are always bound;
    # whether anything starts is decided by the setting and the policy (enabled).
    factory.return_value.bind_jobs.assert_called_once_with(jobs.return_value)
    factory.return_value.bind_indicators.assert_called_once()
    factory.return_value.initialize.assert_called_once_with()


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
    query = rig.supervisor.messages[-1]
    rig.supervisor.event(
        {
            "type": "meeting.call.state",
            "request_type": query["type"],
            "req": query["req"],
            "active": True,
            "app_class": "dion",
            "call_id": "call1",
            "target": {"sink_input": 7, "object_serial": 19, "verified": "declared-verified"},
            "generation": 1,
        }
    )
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
    query = rig.supervisor.messages[-1]
    rig.supervisor.event(
        {
            "type": "meeting.call.state",
            "request_type": query["type"],
            "req": query["req"],
            "active": True,
            "app_class": "dion",
            "call_id": "call1",
            "target": {"sink_input": 7, "object_serial": 19, "verified": "declared-verified"},
            "generation": 1,
        }
    )
    rig.bridge.on_start_clicked()
    rig.reply("prepared", far_available=False, far_reason="monitor-stream-unsupported")
    assert rig.bridge.startState == "no-app-split"
    assert rig.bridge.startOptions[0]["warning"] and not rig.bridge.startOptions[0]["enabled"]
    assert not any(m["type"] == "meeting.capture.open" for m in rig.supervisor.messages)
    rig.reply("closed", tracks=[], state_rev=1)
    rig.reply(
        "finalized", segments={"mic": 0, "far": 0}, intervals=[], status="succeeded", state_rev=1
    )
    rig.supervisor.ready, rig.supervisor.exited = False, True
    rig.supervisor.on_exit(0)
    rig.recording.tick()
    assert not rig.bridge.startCanStart
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


@pytest.mark.parametrize("ready", [False, True], ids=["before-hello", "after-hello"])
@pytest.mark.parametrize("reason", ["close", "disabled", "policy"])
def test_unused_role_retires_after_close_or_revocation(
    delayed_rig: Rig, ready: bool, reason: str
) -> None:
    from astra_voice.core.policy import Policy

    rig = delayed_rig
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
    # Late hello must finish retirement, never revive discovery/capture.
    role.ready = True
    rig.recording.tick()
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
