"""E13.M8 step 5a: role ownership without opening the recording dialog."""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest
from test_meeting_m8_locality import ObserverPulse, ObserverPulses
from test_meeting_m8_role_observer import assert_no_pcm, local_rig
from test_meeting_recording_bridge import (
    DETECTOR_START,
    DETECTOR_STOP,
    Rig,
    Supervisor,
    deliver_detector,
    deliver_remote,
    detector_commands,
    detector_reply,
    finish_capture_close,
    start_recording,
)
from test_meeting_recording_bridge import rig as rig

from astra_voice.core.meeting_session import StorageStatus
from astra_voice.ui.meeting_offer import OfferView
from astra_voice.worker import ipc
from astra_voice.worker.supervisor import WorkerSupervisor

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("filesystem_euid")]
SHUTDOWN = "meeting.capture.shutdown"


def observe(rig: Rig) -> dict[str, Any]:
    rig.bridge.setSetting("meeting_offer_on_call", True)
    rig.recording.tick()
    (command,) = rig.supervisor.messages
    assert command["type"] == DETECTOR_START
    assert rig.supervisor.starts == 1 and not rig.bridge.startDialogOpen
    return command


def command_types(role: Supervisor) -> list[str]:
    return [message["type"] for message in role.messages]


def leave(role: Supervisor) -> None:
    role.ready, role.exited = False, True
    role.on_exit(0)


def test_observation_starts_one_role_without_dialog_or_pcm(
    rig: Rig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert rig.supervisor.starts == 0
    rig.bridge.setSetting("meeting_offer_on_call", True)
    rig.recording.initialize()
    for _ in range(3):
        rig.now += 0.05
        rig.recording.tick()
    assert len(rig.supervisors) == 1 and rig.supervisor.starts == 1
    (command,) = rig.supervisor.messages
    assert command["type"] == DETECTOR_START
    assert not rig.bridge.startDialogOpen and not rig.bridge.recording
    assert rig.session.starts == 1 and rig.recording._snapshot().allowed

    dispatched = threading.Event()

    def setup(pulse: ObserverPulse) -> None:
        pulse.before_dispatch = dispatched.set

    with monkeypatch.context() as patch:
        pulses = ObserverPulses(patch, setup=setup)
        worker = local_rig(tmp_path, patch)
        try:
            assert [reply["type"] for reply in worker.state.handle(command)] == ["meeting.ack"]
            assert dispatched.wait(2)
            (started,) = worker.poll()
            assert started == detector_reply(command, "meeting.detector.started")
            assert worker.state.prepare_starts == 0 and worker.state.backend is None
        finally:
            worker.cleanup()
        assert pulses.count("pa_context_connect") == 1
        assert_no_pcm(worker, pulses)


@pytest.mark.parametrize("gate", ["setting", "policy", "toggle", "remote"])
@pytest.mark.parametrize("show_w1", [False, True])
def test_revoke_one_observation_gate_retires_role_without_dialog(
    rig: Rig, gate: str, show_w1: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    view = Mock(spec=OfferView)
    rig.recording.bind_offer(view)
    start = observe(rig)
    role = rig.supervisor
    deliver_detector(role, start, "meeting.detector.started")
    if show_w1:
        role.event(
            {
                "type": "meeting.call.started",
                "generation": role.generation,
                "call_id": "current-call",
                "app_class": "dion",
                "variant1": True,
                "measured_mono_ms": 100_000,
            }
        )
        rig.recording.tick()
        view.show.assert_called_once_with("call-app", 60)
    assert rig.session.closes == 0 and rig.session.receivers(rig.session.changed) == 1

    with monkeypatch.context() as patch:
        # Separate the predicates even though the real effective toggle also checks policy.
        if gate == "setting":
            patch.setattr(rig.recording, "enabled", lambda: False)
        elif gate == "policy":
            patch.setattr(rig.recording, "policy_allowed", lambda: False)
        elif gate == "toggle":
            rig.bridge.setSetting("meeting_offer_on_call", False)
        else:
            deliver_remote(role)
        rig.recording.tick()
        assert command_types(role) == [DETECTOR_START, DETECTOR_STOP, SHUTDOWN]
        if show_w1:
            view.close.assert_called_once_with()
        else:
            view.close.assert_not_called()
        assert not rig.bridge.startDialogOpen
        assert rig.session.closes == 1 and rig.session.receivers(rig.session.changed) == 0
        leave(role)
        rig.now += 61
        rig.recording.tick()
        assert rig.supervisors == [role] and role.starts == 1


def test_remote_latch_blocks_observation_after_manual_role_replacement(rig: Rig) -> None:
    observe(rig)
    observed = rig.supervisor
    deliver_remote(observed, stale=True)
    rig.recording.tick()
    assert command_types(observed) == [DETECTOR_START]
    deliver_remote(observed)
    rig.recording.tick()
    assert command_types(observed) == [DETECTOR_START, DETECTOR_STOP, SHUTDOWN]
    leave(observed)

    rig.bridge.requestRecording()
    manual = rig.supervisor
    assert manual is not observed and manual.starts == 1
    assert rig.bridge.startDialogOpen and rig.bridge.startCanStart
    assert manual.messages == []
    rig.bridge.closeStartDialog()
    assert command_types(manual) == [SHUTDOWN]
    leave(manual)
    rig.bridge.setSetting("meeting_offer_on_call", False)
    rig.bridge.setSetting("meeting_offer_on_call", True)
    rig.now += 61
    rig.recording.tick()
    assert rig.supervisors == [observed, manual]


def test_recording_dialog_reuses_observation_role_for_explicit_start(rig: Rig) -> None:
    start = observe(rig)
    role = rig.supervisor
    deliver_detector(role, start, "meeting.detector.started")
    rig.bridge.requestRecording()
    rig.no_call()
    assert rig.supervisors == [role] and role.starts == 1
    assert rig.bridge.startDialogOpen and rig.bridge.startCanStart
    assert command_types(role) == [DETECTOR_START, "meeting.call.query"]
    rig.bridge.on_start_clicked()
    assert command_types(role)[-1] == "meeting.capture.prepare"
    rig.reply("prepared")
    assert command_types(role)[-1] == "meeting.capture.open"
    rig.reply("started")
    assert rig.bridge.recording and not rig.bridge.startDialogOpen
    assert rig.supervisors == [role] and role.starts == 1


@pytest.mark.parametrize("observing", [True, False], ids=["observe", "manual"])
def test_preflight_failure_leaves_a_role_owned_only_by_dialog_or_observation(
    rig: Rig, observing: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    if observing:
        observe(rig)
    rig.bridge.requestRecording()
    rig.no_call()
    role = rig.supervisor
    assert rig.bridge.startCanStart
    rig.storage = StorageStatus(100, True, False)
    jobs: list[Callable[[], None]] = []
    controller = rig.recording.controller
    with monkeypatch.context() as patch:
        patch.setattr(controller, "_filesystem_submit", jobs.append)
        rig.bridge.on_start_clicked()
    assert controller.state == "preparing" and not rig.recording._standby_role
    (preflight,) = jobs
    preflight()
    assert controller.state == "preparing"
    rig.recording.tick()
    assert rig.bridge.startState == "error-disk"
    before = command_types(role)
    assert "meeting.capture.prepare" not in before
    rig.bridge.closeStartDialog()
    assert command_types(role) == (before if observing else [*before, SHUTDOWN])
    if not observing:
        role.ready, role.exited = False, True
        rig.recording.tick()
        assert controller.state == "stopped" and not rig.recording._role_started


def test_prepare_timeout_replaces_role_only_after_poll_confirms_exit(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    start = observe(rig)
    previous = rig.supervisor
    deliver_detector(previous, start, "meeting.detector.started")
    rig.bridge.requestRecording()
    rig.no_call()
    rig.bridge.on_start_clicked()
    assert command_types(previous)[-1] == "meeting.capture.prepare"
    assert rig.recording.controller.state == "preparing"

    def kill() -> None:
        previous.kills += 1
        previous.ready = False

    # WorkerSupervisor.kill() does not call on_exit and poll can confirm exit later.
    with monkeypatch.context() as patch:
        patch.setattr(previous, "kill", kill)
        rig.now += 5
        rig.recording.tick()
    assert previous.kills == 1 and not previous.exited
    rig.bridge.closeStartDialog()
    rig.recording.tick()
    assert rig.supervisors == [previous] and not rig.bridge.startDialogOpen

    previous.exited = True
    rig.recording.tick()
    assert len(rig.supervisors) == 2 and rig.supervisor.starts == 1
    assert command_types(rig.supervisor) == [DETECTOR_START]
    assert not rig.bridge.startDialogOpen and rig.session.closes == 0


@pytest.mark.parametrize("observing", [True, False], ids=["observe", "manual"])
def test_consumed_role_replacement_waits_for_exit_and_files_without_backoff(
    rig: Rig, observing: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    if observing:
        start = observe(rig)
        deliver_detector(rig.supervisor, start, "meeting.detector.started")
    start_recording(rig)
    previous = rig.supervisor
    rig.now += 1
    rig.recording.tick()
    assert rig.session.starts == 1 and rig.recording._snapshot().allowed
    rig.recording.controller.stop()
    finish_capture_close(rig)
    rig.recording.tick()
    assert rig.recording.controller.state == "stopped" and not previous.exited
    assert not rig.bridge.startDialogOpen and rig.supervisors == [previous]
    assert rig.session.closes == int(not observing)

    filesystem: list[Callable[[], None]] = []
    with monkeypatch.context() as patch:
        patch.setattr(rig.recording.controller, "_filesystem_submit", filesystem.append)
        leave(previous)
        rig.recording.tick()
        assert rig.supervisors == [previous]
        (cleanup,) = filesystem
        cleanup()
        rig.recording.tick()
    assert not rig.bridge.startDialogOpen
    if observing:
        assert len(rig.supervisors) == 2 and rig.supervisor.starts == 1
        assert command_types(rig.supervisor) == [DETECTOR_START]
        assert rig.session.starts == 1 and rig.session.closes == 0
        assert rig.recording._snapshot().allowed
    else:
        assert rig.supervisors == [previous]
        assert rig.session.receivers(rig.session.changed) == 0


@pytest.mark.parametrize("raises", [True, False], ids=["exception", "exited-immediately"])
def test_observer_launch_failures_back_off_at_50_ms_ticks(
    rig: Rig, raises: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts: list[float] = []
    initial = rig.now

    def fail(role: Supervisor) -> None:
        attempts.append(rig.now - initial)
        if raises:
            raise OSError()

    rig.bridge.setSetting("meeting_offer_on_call", True)
    with monkeypatch.context() as patch:
        patch.setattr(Supervisor, "start", fail)
        for tick in range(242 * 20):
            rig.now = initial + tick / 20
            rig.recording.tick()
    assert attempts == [0, 2, 6, 14, 30, 62, 122, 182]
    assert rig.session.starts == 1 and rig.session.closes == 0

    rig.now = initial + 242
    rig.recording.tick()
    assert rig.supervisor.starts == 1
    assert command_types(rig.supervisor) == [DETECTOR_START]


def test_started_through_supervisor_receive_resets_observer_launch_delay(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    initial = rig.now
    rig.bridge.setSetting("meeting_offer_on_call", True)
    fail = Mock(side_effect=OSError())
    with monkeypatch.context() as patch:
        patch.setattr(Supervisor, "start", fail)
        rig.recording.tick()
        rig.now = initial + 2
        rig.recording.tick()
    assert fail.call_count == 2
    rig.now = initial + 6
    rig.recording.tick()
    previous = rig.supervisor
    (command,) = detector_commands(previous)
    role = WorkerSupervisor(previous.event, role="meeting-capture", use_qt=False)
    role.state = "running"
    role._receive(ipc.encode(ipc.make_hello(role=role.role), role=role.role), role.generation)
    assert role.ready
    reply = ipc.encode(detector_reply(command, "meeting.detector.started"), role=role.role)
    with monkeypatch.context() as patch:
        patch.setattr(rig.recording.controller, "supervisor", role)
        role.send(command)
        role._receive(reply, role.generation - 1)
        assert rig.recording._observation_retry_delay == 8
        role._receive(reply, role.generation)
        assert not role._pending and rig.recording._detector_running is True
    leave(previous)
    rig.now += 1.95
    rig.recording.tick()
    assert rig.supervisor is previous
    rig.now = initial + 8
    rig.recording.tick()
    assert rig.supervisor is not previous
    assert command_types(rig.supervisor) == [DETECTOR_START]


@pytest.mark.parametrize("past_recording", [False, True], ids=["fresh", "after-recording"])
def test_background_launch_failure_preserves_user_visible_recording_state(
    rig: Rig, past_recording: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    if past_recording:
        start_recording(rig)
        rig.recording.controller.stop()
        finish_capture_close(rig)
        leave(rig.supervisor)
        rig.recording.tick()
        rig.recording._preview_error = "disk-low"
        rig.recording.controller.error_code = "far-unavailable"
        rig.recording.controller.far_reason = "monitor-stream-unsupported"
    before = (
        rig.recording.controller.error_code,
        rig.recording._preview_error,
        rig.bridge.overlay,
        rig.bridge.sectionState,
        rig.bridge.meetings,
        rig.bridge.startState,
    )
    assert not rig.bridge.startDialogOpen and not rig.bridge.overlay["visible"]
    fail = Mock(side_effect=RuntimeError())
    rig.bridge.setSetting("meeting_offer_on_call", True)
    with monkeypatch.context() as patch:
        if past_recording:
            fail.side_effect = OSError()

            def factory(
                event: Callable[[dict[str, Any]], None], exited: Callable[[int], None]
            ) -> WorkerSupervisor:
                role = WorkerSupervisor(event, role="meeting-capture", use_qt=False, on_exit=exited)
                patch.setattr(role, "_launch", fail)
                return role

            patch.setattr(rig.recording, "_supervisor_factory", factory)
        else:
            patch.setattr(Supervisor, "start", fail)
        rig.recording.tick()
    fail.assert_called_once()
    assert not rig.bridge.startDialogOpen
    assert (
        rig.recording.controller.error_code,
        rig.recording._preview_error,
        rig.bridge.overlay,
        rig.bridge.sectionState,
        rig.bridge.meetings,
        rig.bridge.startState,
    ) == before
