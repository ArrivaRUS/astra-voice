"""Capture deadlines and exit callbacks through a real subprocess and Unix socket."""

from __future__ import annotations

import json
import os
import select
import signal
import socket
import subprocess
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from test_meeting_ipc import CAPTURE, command
from test_meeting_recording_bridge import MID, Session
from test_meeting_supervisor import Clock

from astra_voice.core.meeting_session import ERROR_TEXT, StorageStatus
from astra_voice.core.settings import Settings
from astra_voice.meetings.apps import AppClass, AppDefinition
from astra_voice.meetings.events import MeetingState
from astra_voice.meetings.model import Meeting
from astra_voice.meetings.segments import CaptureJournal
from astra_voice.meetings.store import MeetingStore, Writer
from astra_voice.platform.session import SessionKind
from astra_voice.ui.meeting_bridge import MeetingBridge
from astra_voice.ui.meeting_recording import AudioHints, MeetingRecording
from astra_voice.worker import ipc
from astra_voice.worker.supervisor import Message, WorkerSupervisor

pytestmark = pytest.mark.unit
ROLE = "meeting-capture"
PREPARE = "meeting.capture.prepare"
OPEN = "meeting.capture.open"
KEEPALIVE = "meeting.capture.keepalive"
STOP = "meeting.capture.stop"
SHUTDOWN = "meeting.capture.shutdown"
DETECTOR_START = "meeting.detector.start"

PEER = """
import json
import os
import struct
import sys

fd = int(sys.argv[1])
mode = sys.argv[2]
with os.fdopen(fd, 'rb') as reader, os.fdopen(fd, 'wb', closefd=False) as writer:
    def frame(message):
        body = json.dumps(message).encode()
        return struct.pack('>I', len(body)) + body

    def send(*messages):
        writer.write(b''.join(frame(message) for message in messages))
        writer.flush()

    def reply(request, kind, **fields):
        identity = {key: request[key] for key in ('req', 'request_type', 'meeting_id',
                    'meeting_generation', 'capture_id', 'state_rev') if key in request}
        return {**identity, 'type': kind, **fields}

    def reject(request):
        correlation = {key: request[key] for key in ('req', 'request_type', 'meeting_id',
                     'meeting_generation')}
        send({**correlation, 'type': 'error', 'code': 'audio-failed',
              'message': 'meeting-command-failed'})

    if mode != 'silent-hello':
        send(json.loads(sys.argv[3]))
    close_request = None
    open_to_reject = None
    while header := reader.read(4):
        request = json.loads(reader.read(struct.unpack('>I', header)[0]))
        kind = request['type']
        if kind == 'meeting.capture.keepalive' and mode == 'reply':
            send(reply(request, 'meeting.ack'))
        elif kind == 'meeting.capture.stop' and mode != 'silent':
            close_request = request
            send(reply(request, 'meeting.capture.closed', tracks=['mic']))
        elif kind == 'ping':
            if open_to_reject is not None:
                reject(open_to_reject)
                open_to_reject = None
                continue
            pong = reply(request, 'pong')
            if close_request is not None and mode == 'finalize':
                send(reply(close_request, 'meeting.capture.finalized',
                           segments={'mic': 1, 'far': 0}, intervals=[], status='succeeded'),
                     pong)
            else:
                send(pong)
        elif kind == 'meeting.capture.shutdown' and mode != 'silent':
            if mode == 'burst-bye':
                # Put bye beyond the first recv() behind an unrelated valid frame.
                stale = {'type': 'pong', 'request_type': 'ping', 'req': 'unmatched'}
                body = json.dumps(stale).encode().ljust(64 * 1024)
                writer.write(struct.pack('>I', len(body)) + body)
            send(reply(request, 'meeting.capture.bye'))
            if mode != 'linger':
                sys.exit(0)
        elif kind == 'meeting.detector.start':
            send(reply(request, 'meeting.detector.started', apps_version=1))
        elif kind == 'meeting.call.query':
            send(reply(request, 'meeting.call.state', active=False))
        elif kind == 'meeting.capture.prepare':
            if mode == 'crash-prepare':
                sys.exit(3)
            if mode == 'reject-prepare':
                reject(request)
            else:
                send(reply(request, 'meeting.capture.prepared', capture_id='capture1',
                           state_rev=0, pause_reasons=[], manual_resume_required=False,
                           blocked_epoch=2, mic_label='', server_local=True,
                           server_kind='pipewire', far_available=True))
        elif kind == 'meeting.capture.open':
            if mode == 'pending-open':
                continue
            if mode == 'reject-open':
                open_to_reject = request
                continue
            send(reply(request, 'meeting.capture.started', state_rev=1, pause_reasons=[],
                       manual_resume_required=False, blocked_epoch=2,
                       t0_mono_ms=100000, tracks=['mic']))
"""


def make_role(
    mode: str,
    clock: Clock,
    event: Callable[[Message], None],
    exited: Callable[[int], None],
) -> WorkerSupervisor:
    hello = json.dumps(ipc.make_hello(role=ROLE))
    return WorkerSupervisor(
        event,
        role=ROLE,
        clock=clock,
        use_qt=False,
        command_factory=lambda fd: [sys.executable, "-c", PEER, str(fd), mode, hello],
        on_exit=exited,
    )


def queued(role: WorkerSupervisor, *kinds: str) -> None:
    """Wait for socket readiness and verify complete replies without consuming them."""
    connection = role._socket
    assert connection is not None
    assert select.select([connection], [], [], 3)[0], kinds
    reader = ipc.FrameReader(role=ROLE)
    assert [
        message["type"]
        for message in reader.feed(connection.recv(2 * ipc.MAX_FRAME_BYTES, socket.MSG_PEEK))
    ] == list(kinds)


def active_process(role: WorkerSupervisor) -> subprocess.Popen[bytes]:
    process = role.process
    assert process is not None and process.poll() is None
    return process


@contextmanager
def running(mode: str) -> Iterator[tuple[WorkerSupervisor, Clock, list[Message], list[int]]]:
    clock = Clock()
    events: list[Message] = []
    exits: list[int] = []
    role = make_role(mode, clock, events.append, exits.append)
    role.start()
    try:
        yield role, clock, events, exits
    finally:
        role.stop()


@contextmanager
def wired(
    clock: Clock, event: Callable[[Message], None]
) -> Iterator[tuple[WorkerSupervisor, socket.socket]]:
    parent, peer = socket.socketpair()
    with parent, peer:
        parent.setblocking(False)
        peer.setblocking(False)
        role = WorkerSupervisor(event, clock=clock, role=ROLE, use_qt=False)
        role._socket = parent
        role.state = "running"
        try:
            write_wire(peer, ipc.encode(ipc.make_hello(role=ROLE), role=ROLE))
            role.pump()
            assert role.ready
            yield role, peer
        finally:
            role.stop()


def write_wire(peer: socket.socket, data: bytes) -> None:
    assert os.write(peer.fileno(), data) == len(data)


def test_partial_ack_survives_eagain_between_pumps() -> None:
    clock = Clock()
    events: list[Message] = []
    with wired(clock, events.append) as (role, peer):
        role.send(command(ROLE, KEEPALIVE))
        ack = {"type": "meeting.ack", "request_type": KEEPALIVE, "req": "req1", **CAPTURE}
        frame = ipc.encode(ack, role=ROLE)
        events.clear()
        write_wire(peer, frame[:7])
        role.pump()
        assert not events and role._pending
        write_wire(peer, frame[7:])
        role.pump()
        role.pump()
        assert events == [{**ack, "generation": role.generation}] and not role._pending


def test_select_wait_is_clamped_to_the_closest_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = Clock()
    events: list[Message] = []
    with wired(clock, events.append) as (role, _):
        role.send(command(ROLE, STOP))
        waits: list[float] = []

        def select_timeout(
            read: list[socket.socket],
            write: list[socket.socket],
            exc: list[socket.socket],
            timeout: float,
        ) -> tuple[list[socket.socket], list[socket.socket], list[socket.socket]]:
            waits.append(timeout)
            return [], [], []

        clock.now += 0.999
        with monkeypatch.context() as patch:
            patch.setattr(select, "select", select_timeout)
            role.pump(0.050)
        assert role.ready
        clock.now = 11.0
        with monkeypatch.context() as patch:
            patch.setattr(select, "select", select_timeout)
            role.pump(0.050)
        assert waits == pytest.approx([0.001, 0.0])
        assert not role.ready and events[-1]["code"] == "timeout"


@pytest.mark.parametrize("reply", [True, False], ids=["queued", "missing"])
@pytest.mark.parametrize("kind", [KEEPALIVE, STOP], ids=["ack", "physical-close"])
def test_request_reply_precedes_expiry_after_gui_stall(reply: bool, kind: str) -> None:
    with running("reply" if reply else "silent") as (role, clock, events, exits):
        queued(role, "hello")
        role.pump()
        process = active_process(role)
        events.clear()
        role.send(command(ROLE, kind))
        expected = "meeting.ack" if kind == KEEPALIVE else "meeting.capture.closed"
        if reply:
            queued(role, expected)

        clock.now += 1.5
        role.pump(timeout=0.05)

        assert role.generation == 1 and not role._restarts
        if reply:
            assert role.ready and role.process is process and process.poll() is None and not exits
            assert [event["type"] for event in events] == [expected]
            if kind == STOP:
                assert role.close_state == "closed"
        else:
            assert process.wait(timeout=3) == -signal.SIGKILL
            role.pump()
            assert role.exited and role.state == "stopped" and exits == [-signal.SIGKILL]
            assert {event["code"] for event in events} == {"timeout"}


@pytest.mark.parametrize("reply", [True, False], ids=["queued", "missing"])
def test_hello_precedes_its_deadline_after_gui_stall(reply: bool) -> None:
    with running("reply" if reply else "silent-hello") as (role, clock, events, exits):
        process = active_process(role)
        if reply:
            queued(role, "hello")
        clock.now += 3.5
        role.pump()

        assert role.generation == 1 and not role._restarts
        if reply:
            assert role.ready and process.poll() is None and not exits
            assert [event["type"] for event in events] == ["hello"]
        else:
            assert process.wait(timeout=3) == -signal.SIGKILL
            role.pump()
            assert role.exited and role.state == "stopped" and exits == [-signal.SIGKILL]
            assert [event["code"] for event in events] == ["hello-timeout"]


@pytest.mark.parametrize("reply", [True, False], ids=["queued", "missing"])
def test_finalized_precedes_end_deadline_after_close(reply: bool) -> None:
    with running("finalize" if reply else "reply") as (role, clock, events, exits):
        queued(role, "hello")
        role.pump()
        process = active_process(role)
        role.send(command(ROLE, STOP))
        queued(role, "meeting.capture.closed")
        role.pump()
        assert role.close_state == "closed"
        events.clear()
        # Let the live role send its remaining results before stalling the GUI.
        role.send(command(ROLE, "ping", req="next"))
        kinds = ("meeting.capture.finalized", "pong") if reply else ("pong",)
        queued(role, *kinds)

        clock.now += 3.5
        role.pump()

        assert role.ready and process.poll() is None and not exits and not role._restarts
        assert not role._pending
        if reply:
            assert role.close_state == "finalized"
            assert [event["type"] for event in events] == list(kinds)
        else:
            assert role.close_state == "closed"
            assert [event["type"] for event in events] == ["pong", "error"]
            assert events[-1]["code"] == "timeout" and events[-1]["request_type"] == STOP


@pytest.mark.parametrize("exits_after_bye", [True, False], ids=["exited", "still-live"])
def test_bye_requires_exit_within_shutdown_ceiling(exits_after_bye: bool) -> None:
    with running("burst-bye" if exits_after_bye else "linger") as (role, clock, events, exits):
        queued(role, "hello")
        role.pump()
        process = active_process(role)
        events.clear()
        role.send(command(ROLE, SHUTDOWN))
        if exits_after_bye:
            assert process.wait(timeout=3) == 0
            queued(role, "pong", "meeting.capture.bye")
        else:
            queued(role, "meeting.capture.bye")

        clock.now += 2.5 if exits_after_bye else 1.5
        role.pump()

        assert [event["type"] for event in events] == ["meeting.capture.bye"]
        if exits_after_bye:
            assert exits == [0]
        else:
            assert process.poll() is None and not exits
            assert not role.ready and role.close_state == "closing-unknown"
            clock.now += 0.5
            role.pump()
            assert process.wait(timeout=3) == -signal.SIGKILL
            role.pump()
            assert exits == [-signal.SIGKILL] and events[-1]["code"] == "timeout"
        assert role.exited and role.state == "stopped" and role.close_state == "closed"
        assert role.generation == 1 and not role._restarts


class RecordingRig:
    def __init__(self, root: Path, mode: str, replacement: str) -> None:
        self.clock = Clock()
        self.clock.now = 100.0
        self.store = MeetingStore(root / "meetings")
        self.session = Session()
        self.roles: list[WorkerSupervisor] = []
        self.callbacks: list[tuple[str, Message | int]] = []
        self.bridge = MeetingBridge(
            Settings(meetings_enabled=True),
            store=self.store,
            session=SessionKind.KDE,
            save=lambda settings: None,
            known_apps=(AppDefinition("dion", "DION", ("dion",), (), AppClass.DION),),
        )

        def factory(
            event: Callable[[Message], None], exited: Callable[[int], None]
        ) -> WorkerSupervisor:
            def on_event(message: Message) -> None:
                self.callbacks.append(("event", message))
                event(message)

            def on_exit(code: int) -> None:
                self.callbacks.append(("exit", code))
                exited(code)

            role = make_role(mode if not self.roles else replacement, self.clock, on_event, on_exit)
            self.roles.append(role)
            return role

        self.recording = MeetingRecording(
            self.store,
            enabled=lambda: self.bridge.enabled,
            policy_allowed=lambda: self.bridge.canEnable,
            detector_allowed=self.bridge.detector_allowed,
            session_kind=SessionKind.KDE,
            session_factory=lambda: self.session,
            supervisor_factory=factory,
            clock=self.clock,
            storage_probe=lambda path: StorageStatus(1000, True, False),
            audio_probe=AudioHints,
            submit=lambda job: job(),
            id_factory=lambda: MID,
            exit_stop_timeout=0.2,
        )
        self.bridge.bindRecording(self.recording)

    def begin(self, observing: bool = False) -> WorkerSupervisor:
        (role,) = self.roles
        if observing:
            self.bridge.setSetting("meeting_offer_on_call", True)
            self.recording.tick()
            queued(role, "hello")
            self.recording.tick()
            queued(role, "meeting.detector.started")
            self.recording.tick()
        self.open_dialog(role)
        return role

    def open_dialog(self, role: WorkerSupervisor | None = None) -> None:
        self.bridge.requestRecording()
        role = self.roles[-1] if role is None else role
        if not role.ready:
            queued(role, "hello")
            self.recording.tick()
        queued(role, "meeting.call.state")
        self.recording.tick()
        assert self.bridge.startCanStart
        self.bridge.on_start_clicked()
        assert self.recording.controller.state == "preparing"

    def advance_to_recording(self, role: WorkerSupervisor) -> None:
        queued(role, "meeting.capture.prepared")
        self.recording.tick()
        if self.recording.controller.state == "opening":
            queued(role, "meeting.capture.started")
            self.recording.tick()
        assert self.recording.controller.state == "recording"

    def replace_after_prepare_crash(self) -> WorkerSupervisor:
        old_role = self.begin()
        old_serial = self.recording._role_serial
        process = old_role.process
        assert process is not None and process.wait(timeout=3) == 3
        self.recording.tick()
        assert self.recording.controller._exit_confirmed
        self.bridge.closeStartDialog()

        self.open_dialog()
        new_role = self.roles[-1]
        assert new_role is not old_role and new_role.generation == old_role.generation
        assert self.recording._role_serial == old_serial + 1
        assert not self.recording.controller._exit_confirmed and new_role.ready
        return new_role

    def assert_ownership_after_crash(self, role: WorkerSupervisor, observing: bool) -> None:
        serial = self.recording._role_serial
        self.bridge.closeStartDialog()
        self.recording.tick()
        self.clock.now += 1.5
        self.recording.tick()
        assert self.roles == [role]
        self.clock.now += 0.5
        self.recording.tick()
        if observing:
            _, new_role = self.roles
            assert role.exited and new_role is not role
            assert self.recording._role_serial == serial + 1
            queued(new_role, "hello")
            self.recording.tick()
            queued(new_role, "meeting.detector.started")
            self.recording.tick()
            assert new_role.ready and self.recording._detector_running is True
            assert self.session.starts == 1 and self.session.closes == 0
        else:
            assert self.roles == [role] and self.session.closes == 1
            assert self.session.receivers(self.session.changed) == 0


@contextmanager
def recording_rig(root: Path, mode: str, replacement: str = "reply") -> Iterator[RecordingRig]:
    from helpers.qt_app import get_qapplication

    get_qapplication()
    rig = RecordingRig(root, mode, replacement)
    try:
        yield rig
    finally:
        rig.bridge.shutdown()
        for role in rig.roles:
            role.stop()


@pytest.mark.usefixtures("filesystem_euid")
@pytest.mark.parametrize("observing", [True, False], ids=["observe", "manual"])
def test_prepare_error_after_exit_cannot_reopen_closing(tmp_path: Path, observing: bool) -> None:
    with recording_rig(tmp_path, "crash-prepare") as rig:
        role = rig.begin(observing)
        process = role.process
        assert process is not None and process.wait(timeout=3) == 3
        serial = rig.recording._role_serial
        rig.callbacks.clear()
        rig.recording.tick()

        assert rig.callbacks[0] == ("exit", 3)
        kind, error = rig.callbacks[1]
        assert kind == "event" and isinstance(error, dict)
        assert error["code"] == "worker-crashed" and error["request_type"] == PREPARE
        assert error["generation"] == role.generation and rig.recording._role_serial == serial
        assert rig.recording.controller._exit_confirmed
        assert rig.recording.controller.state == "stopped"
        assert rig.bridge.sectionState == "empty" and not rig.bridge.recording
        assert rig.bridge.startState == "error-audio"
        assert rig.bridge.startError == ERROR_TEXT["audio-unavailable"]
        assert rig.session.starts == 1 and rig.session.closes == 0  # Open dialog owns the monitor.

        rig.assert_ownership_after_crash(role, observing)


@pytest.mark.usefixtures("filesystem_euid")
@pytest.mark.parametrize("observing", [True, False], ids=["observe", "manual"])
@pytest.mark.parametrize("action", ["open", "keepalive"])
def test_capture_error_after_exit_cannot_reopen_closing(
    tmp_path: Path, observing: bool, action: str
) -> None:
    with recording_rig(tmp_path, f"pending-{action}") as rig:
        role = rig.begin(observing)
        process = active_process(role)
        rig.callbacks.clear()
        if action == "open":
            queued(role, "meeting.capture.prepared")
        else:
            rig.advance_to_recording(role)
            rig.clock.now += 1
        rig.recording.tick()
        request_type = f"meeting.capture.{action}"
        assert [request["type"] for request in role._meeting_requests.values()] == [request_type]
        process.kill()
        assert process.wait(timeout=3) == -signal.SIGKILL
        rig.recording.tick()
        rig.recording.tick()

        exit_index = rig.callbacks.index(("exit", -signal.SIGKILL))
        kind, error = rig.callbacks[exit_index + 1]
        assert kind == "event" and isinstance(error, dict)
        assert error["code"] == "worker-crashed"
        assert error["request_type"] == request_type
        assert error["generation"] == role.generation
        assert rig.recording.controller._exit_confirmed
        assert rig.recording.controller.state == "stopped"
        assert rig.bridge.sectionState != "recording" and not rig.bridge.recording
        if action == "open":
            assert rig.bridge.startState == "error-audio"
            assert rig.bridge.startError == ERROR_TEXT["audio-unavailable"]
        rig.assert_ownership_after_crash(role, observing)


@pytest.mark.usefixtures("filesystem_euid")
def test_live_prepare_error_reaches_dialog_after_replacing_role(tmp_path: Path) -> None:
    with recording_rig(tmp_path, "crash-prepare", "reject-prepare") as rig:
        new_role = rig.replace_after_prepare_crash()
        queued(new_role, "error")
        active_process(new_role)
        rig.recording.tick()
        assert rig.bridge.startState == "error-audio"
        assert rig.bridge.startError == ERROR_TEXT["audio-unavailable"]


@pytest.mark.usefixtures("filesystem_euid")
def test_live_open_error_reaches_dialog_after_replacing_role(tmp_path: Path) -> None:
    with recording_rig(tmp_path, "crash-prepare", "reject-open") as rig:
        new_role = rig.replace_after_prepare_crash()
        queued(new_role, "meeting.capture.prepared")
        rig.recording.tick()
        assert rig.recording.controller.state == "opening" and rig.bridge.startState == "starting"
        assert [request["type"] for request in new_role._meeting_requests.values()] == [OPEN]

        # The peer rejects the pending open only after this separate IPC command.
        new_role.send(command(ROLE, "ping"))
        queued(new_role, "error")
        active_process(new_role)
        rig.callbacks.clear()
        rig.recording.tick()
        kind, error = rig.callbacks[0]
        assert kind == "event" and isinstance(error, dict)
        assert error["request_type"] == OPEN and error["generation"] == new_role.generation
        assert rig.bridge.startState == "error-audio"
        assert rig.bridge.startError == ERROR_TEXT["audio-unavailable"]


@pytest.mark.usefixtures("filesystem_euid")
def test_crash_during_recording_keeps_the_existing_interrupted_outcome(tmp_path: Path) -> None:
    with recording_rig(tmp_path, "reply") as rig:
        role = rig.begin()
        rig.advance_to_recording(role)
        assert rig.bridge.recording and rig.bridge.sectionState == "recording"
        rig.store.write(
            MID,
            "capture.json",
            CaptureJournal(
                MID,
                0,
                "capture1",
                boundaries=[
                    {"kind": "open", "t_mono_ms": 100000, "recorded_ms": 0, "reason": None},
                    {"kind": "close", "t_mono_ms": 105000, "recorded_ms": 5000, "reason": "stop"},
                ],
            ),
            writer=Writer.CAPTURE,
        )
        process = active_process(role)
        process.kill()
        assert process.wait(timeout=3) == -signal.SIGKILL
        rig.recording.tick()
        rig.recording.tick()  # Publish the interrupted meeting read on the previous tick.

        assert rig.recording.controller.state == "stopped" and not rig.bridge.recording
        assert rig.bridge.sectionState == "success" and not rig.bridge.overlay["visible"]
        assert rig.bridge.interruptedOpen and not rig.bridge.startError
        assert rig.bridge.interruptedText == "Запись встречи прервалась. Сохранено 0:00:05"
        meeting = rig.store.read(MID, "meeting.json")
        assert isinstance(meeting, Meeting)
        assert meeting.state == MeetingState.INTERRUPTED and meeting.recorded_ms == 5000
        assert rig.roles == [role] and rig.session.closes == 1
