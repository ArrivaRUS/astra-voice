"""E13.M8 step 7: browser metadata to W1, capture, overlay and saved audio."""

from __future__ import annotations

import logging
import os
import queue
import threading
import wave
from collections import deque
from collections.abc import Callable, Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest
from test_meeting_capture import Source
from test_meeting_m8_call_service import ALLOWED, ANCHOR, TARGET, CheckDouble
from test_meeting_m8_detector_source import Backend as MetadataBackend
from test_meeting_m8_far_mix import ScriptedPulse, tone
from test_meeting_m8_identity_check import KEY
from test_meeting_m8_locality import make_socket
from test_meeting_m8_offer import View
from test_meeting_m8_offer_log import line
from test_meeting_m8_offer_start import BROWSER_LOST, assert_call_lost
from test_meeting_m8_start import resolved_target
from test_meeting_recording_bridge import MID, Rig
from test_meeting_supervisor import receive

from astra_voice.core.meeting_session import WARNING_TEXT
from astra_voice.core.settings import Settings
from astra_voice.meetings.apps import AppClass
from astra_voice.meetings.events import MeetingState
from astra_voice.worker import ipc
from astra_voice.worker.meeting_capture import MeetingCaptureState
from astra_voice.worker.meeting_detector import (
    Change,
    Direction,
    Facility,
    ProgramKey,
    RawClient,
    RawStream,
)
from astra_voice.worker.meeting_identity_check import (
    CheckResult,
    PinnedStream,
    PinnedTarget,
    Purpose,
)
from astra_voice.worker.meeting_observer import ObserverConnect
from astra_voice.worker.meeting_session import SessionAdmission
from astra_voice.worker.monitor_stream import MixedFarSource, MonitorStreamSource
from astra_voice.worker.supervisor import WorkerSupervisor
from helpers.qt_app import get_qapplication

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("filesystem_euid")]

BROWSER = ProgramKey("browser", "firefox")
CLIENT, MIC, FIRST_OUTPUT = 238, 870, 910
SECOND_MID = "2026-11-01_120001-bcde"
PRIVATE = "S7_PRIVATE_MEDIA_\u202e\n"
PREPARE, OPEN = "meeting.capture.prepare", "meeting.capture.open"
QUERY = "meeting.call.query"
SHOW = ("show", "call-browser", 60)


class ObserverGate:
    """Pause at the end of a poll until the test has changed the picture or clock."""

    def __init__(self) -> None:
        self.arrived = threading.Event()
        self.advance = threading.Event()
        self.tearing_down = False

    def wait(self, wake: threading.Event, _seconds: float) -> bool:
        self.arrived.set()
        assert self.advance.wait(3), "observer was not released"
        if not self.tearing_down:
            self.advance.clear()
        return wake.is_set()

    def ready(self) -> None:
        assert self.arrived.wait(2), "observer did not complete a poll"
        self.arrived.clear()

    def step(self) -> None:
        self.advance.set()
        self.ready()


class Metadata(MetadataBackend):
    def __init__(self, cookie: int, streams: dict[tuple[Direction, int], RawStream]) -> None:
        super().__init__()
        self.cookie = cookie
        self.clients = {
            CLIENT: RawClient(
                CLIENT,
                {
                    "application.process.id": KEY.pid,
                    "application.process.binary": "firefox",
                    "application.name": PRIVATE,
                    "application.process.user": PRIVATE,
                },
            )
        }
        self.streams = streams

    def server_cookie(self) -> int:
        return self.cookie


class Browser:
    def __init__(self) -> None:
        self.cookie = ANCHOR.server_cookie
        self.streams: dict[tuple[Direction, int], RawStream] = {}
        self.connections: list[Metadata] = []
        self.addresses: list[bytes] = []

    def connect(self, address: bytes, connect: ObserverConnect) -> Metadata:
        self.addresses.append(address)
        assert address.startswith(b"unix:")
        assert connect(lambda: 0) == 0
        backend = Metadata(self.cookie, self.streams)
        self.connections.append(backend)
        return backend

    def set_stream(self, facility: Facility, index: int, *, present: bool) -> None:
        direction = Direction(facility.value)
        key = (direction, index)
        if present:
            self.streams[key] = RawStream(
                index,
                CLIENT,
                direction,
                {"object.serial": index + 100, "media.name": PRIVATE},
                False,
                False,
            )
        else:
            del self.streams[key]
        if self.connections and not self.connections[-1].closed:
            change = Change.NEW if present else Change.REMOVE
            self.connections[-1].notify(facility, change, index)

    def result(self, now: float) -> CheckResult:
        mic = replace(next(iter(ANCHOR.members)), index=MIC, serial=MIC + 100, client=CLIENT)
        anchor = replace(
            ANCHOR,
            program=BROWSER,
            server_cookie=self.cookie,
            members=frozenset({mic}),
        )
        streams = tuple(
            PinnedStream(index, index + 100, CLIENT, TARGET.streams[0].client_serial, KEY)
            for direction, index in self.streams
            if direction == Direction.OUTPUT
        )
        return replace(
            ALLOWED,
            app_class=AppClass.BROWSER,
            measured_mono_ms=round(now * 1000),
            anchor=anchor,
            target=PinnedTarget(self.cookie, streams, now),
        )


class CaptureAudio:
    """Fake control transport and native record endpoints; the far mixer is real."""

    def __init__(self, browser: Browser, clock: Callable[[], float]) -> None:
        self.browser, self.clock = browser, clock
        self.pulses: list[ScriptedPulse] = []
        self.mics: list[Source] = []
        self.context: tuple[int, int] | None = None
        self.server_local = True
        self.control_opens = 0
        self.control_closed = threading.Event()

    def control_open(self) -> CaptureAudio:
        self.control_opens += 1
        self.context = (51, self.browser.cookie)
        return self

    def prepare(self, far: dict[str, Any], *, target: PinnedTarget | None = None) -> dict[str, Any]:
        return {
            "server_local": self.server_local,
            "server_kind": "pulseaudio",
            "far_available": True,
        }

    def own_context(self) -> tuple[int, int] | None:
        return self.context

    def mic(self) -> Source:
        clock = self.clock

        class Microphone(Source):
            def open(self, device: str | None = None) -> None:
                self.started_at = round(clock() * 1000)
                super().open(device)

            def sample_clock(self) -> tuple[str, int, int]:
                elapsed = self.samples // 16
                return "pulse", elapsed, self.started_at + elapsed

        mic = Microphone()
        self.mics.append(mic)
        return mic

    def close(self) -> None:
        self.context = None
        self.control_closed.set()

    def source(
        self, far: dict[str, Any], *, target: PinnedTarget | None = None
    ) -> MonitorStreamSource | MixedFarSource:
        assert far["kind"] == "app_stream" and target is not None
        pending = iter(target.streams)

        def make_pulse(**_admission: Any) -> ScriptedPulse:
            stream = next(pending)
            pulse = ScriptedPulse(resolved_target(replace(target, streams=(stream,))))
            self.pulses.append(pulse)
            return pulse

        if len(target.streams) == 1:
            return MonitorStreamSource(make_pulse(), "acceptance", target=target)
        return MixedFarSource(
            target, "acceptance", factory=make_pulse, now_ms=lambda: round(self.clock() * 1000)
        )


class CaptureEvents(queue.SimpleQueue[dict[str, Any]]):
    def __init__(self, *kinds: str) -> None:
        super().__init__()
        self.signals = {kind: threading.Event() for kind in kinds}

    def put(
        self, item: dict[str, Any], /, block: bool = True, timeout: float | None = None
    ) -> None:
        super().put(item, block, timeout)
        ready = self.signals.get(item["type"])
        if ready is not None:
            ready.set()

    def wait(self, kind: str) -> None:
        assert self.signals[kind].wait(2), f"no worker event {kind}"
        self.signals[kind].clear()


class LoopbackRole(WorkerSupervisor):
    """Replace the subprocess transport only, keeping the IPC codecs and supervisor."""

    def __init__(
        self,
        event: Callable[[dict[str, Any]], None],
        exited: Callable[[int], None],
        *,
        clock: Callable[[], float],
        browser: Browser,
    ) -> None:
        super().__init__(event, clock=clock, on_exit=exited, role="meeting-capture", use_qt=False)
        self.messages: list[dict[str, Any]] = []
        self.replies: list[dict[str, Any]] = []
        self.pending: deque[dict[str, Any]] = deque()
        self.requests = ipc.FrameReader(role=self.role)
        self.gate = ObserverGate()
        self.audio = CaptureAudio(browser, clock)
        self.checks = CheckDouble()
        session = SessionAdmission(kind="KDE", clock=clock, max_age=float("inf"))
        session.update(known=True, active=True, locked=False, sleeping=False)
        self.archive_ready = threading.Event()
        self.state_machine = MeetingCaptureState(
            clock=clock,
            session=session,
            observer_factory=browser.connect,
            observer_fds=lambda: {},
            observer_wait=self.gate.wait,
            check_factory=self.checks.factory,
            backend_factory=self.audio.control_open,
            mic_factory=self.audio.mic,
            free_mb=lambda: 1000,
            fsync=self.sync_archive,
        )
        self.events = CaptureEvents(
            "prepared",
            "prepare-failed",
            "opened",
            "closed",
            "finalized",
            "far-partial",
            "track-ended",
        )
        self.state_machine.messages = self.events
        self.process_double = Mock()
        self.process_double.poll.return_value = None

    def sync_archive(self, descriptor: int) -> None:
        os.fsync(descriptor)
        self.archive_ready.set()

    def _launch(self) -> None:
        self._hello_deadline = self._clock() + 3
        self.process = self.process_double
        self.state = "running"
        receive(self, ipc.make_hello(role=self.role))

    def _flush(self) -> None:
        frame = bytes(self._outgoing)
        self._outgoing.clear()
        for command in self.requests.feed(frame):
            self.messages.append(command)
            self.pending.extend(self.state_machine.handle(command))
        if self.state_machine.terminating:
            self.gate.advance.set()

    def pump(self, timeout: float = 0.0) -> None:
        if self.process is None:
            return
        self.pending.extend(self.state_machine.poll())
        while self.pending:
            event = self.pending.popleft()
            self.replies.append(event)
            receive(self, event)
        if self.state_machine.stopped:
            self.process_double.poll.return_value = self.state_machine.exit_code
            if self._meeting_exit_deadline is not None:
                self._expire_meeting()
            else:
                self._restart()
        else:
            self._expire()

    def finish(self, kind: str) -> None:
        self.events.wait(kind)
        self.pump()

    def dispose(self) -> None:
        self.checks.release_all()
        self.gate.tearing_down = True
        self.gate.advance.set()
        self.state_machine.cleanup()

    def simulate_exit(self) -> None:
        self.dispose()
        self.process_double.poll.return_value = 3
        self._restart()


class Acceptance:
    def __init__(self, root: Path, browser: Browser) -> None:
        self.browser = browser
        self.roles: list[LoopbackRole] = []

        def factory(
            event: Callable[[dict[str, Any]], None],
            exited: Callable[[int], None],
            clock: Callable[[], float],
        ) -> LoopbackRole:
            role = LoopbackRole(event, exited, clock=clock, browser=browser)
            self.roles.append(role)
            return role

        self.gui = Rig(
            root,
            role_factory=factory,
            settings=Settings(meetings_enabled=True, meeting_diarization=False),
        )
        self.gui.recording.controller.id_factory = iter((MID, SECOND_MID)).__next__
        self.view = View()
        self.gui.recording.bind_offer(self.view)

    @property
    def role(self) -> LoopbackRole:
        return self.roles[-1]

    @property
    def now(self) -> float:
        return self.gui.now

    def tick(self, seconds: float = 0) -> None:
        self.gui.now = round(self.gui.now + seconds, 3)
        self.gui.recording.tick()

    def poll_observer(self, seconds: float = 0) -> None:
        self.gui.now = round(self.gui.now + seconds, 3)
        self.role.gate.step()
        self.tick()

    def enable(self, *, local: bool = True) -> None:
        self.gui.bridge.setSetting("meeting_offer_on_call", True)
        self.tick()
        if local:
            self.role.gate.ready()
        self.tick()

    def begin_call(self, outputs: int = 1, *, first_output: int = FIRST_OUTPUT) -> None:
        for index in range(first_output, first_output + outputs):
            self.browser.set_stream(Facility.OUTPUT, index, present=True)
        self.browser.set_stream(Facility.MICROPHONE, MIC, present=True)
        self.poll_observer()

    def qualify(self) -> str:
        self.poll_observer(1)  # Apply any queued point updates before counting five seconds.
        run = self.role.checks.plan(self.browser.result(self.now + 6))
        self.poll_observer(6)
        request = run.started()
        assert request.purpose == Purpose.OFFER and request.program == BROWSER
        run.complete()
        self.role.pump()
        self.tick()
        call_id = self.call_frames("started")[-1]["call_id"]
        assert isinstance(call_id, str)
        return call_id

    def call_frames(self, action: str) -> list[dict[str, Any]]:
        return [e for e in self.role.replies if e["type"] == f"meeting.call.{action}"]

    def press(self, action: str = "start", *, accepted: bool = True) -> None:
        assert self.gui.recording.press_offer(action, self.gui.recording.offer.now_ms()) is accepted
        self.tick()

    def start(self) -> None:
        role = self.role
        self.tick(0.1)
        self.press(accepted=False)
        assert not {PREPARE, OPEN}.intersection(m["type"] for m in role.messages)
        assert not role.audio.mics and not role.audio.pulses
        self.tick(0.5)
        run = role.checks.plan(self.browser.result(self.now))
        self.press()
        fresh = run.started()
        assert fresh.purpose == Purpose.START
        assert fresh.anchor is not None and fresh.anchor.program == BROWSER
        # Keep the real START in flight while pressing W1 again.
        self.press(accepted=False)
        commands = [command["type"] for command in role.messages]
        assert commands.count(PREPARE) == 1 and OPEN not in commands
        run.complete()
        self.finish_open()
        assert sum(command["type"] == OPEN for command in role.messages) == 1
        assert self.view.calls[-1] == ("close",)
        assert self.gui.recording.overlay["state"] == "recording"
        assert not self.gui.bridge.startDialogOpen

    def finish_open(self) -> None:
        self.role.finish("prepared")
        self.role.events.wait("opened")
        # The archive's opening journal write shares a lock with GUI metadata.
        # Wait for the actual archive fsync before feeding 'started' to the GUI.
        opened = self.role.state_machine.poll()
        assert self.role.archive_ready.wait(2), "capture archive was not opened"
        self.role.pending.extend(opened)
        self.tick()

    def command_count(self, kind: str) -> int:
        return sum(command["type"] == kind for role in self.roles for command in role.messages)

    def assert_no_audio(self) -> None:
        for role in self.roles:
            assert not role.audio.mics and not role.audio.pulses
            assert not role.state_machine.sources and role.state_machine.archive is None
        assert not list(self.gui.store.root.rglob("*.wav"))
        assert not list(self.gui.store.root.rglob("capture.json"))

    def assert_quiet(self) -> None:
        assert self.command_count(PREPARE) == self.command_count(OPEN) == 0
        assert all(role.audio.control_opens == 0 for role in self.roles)
        self.assert_no_audio()
        assert self.gui.store.list() == [] and not list(self.gui.store.root.rglob("*"))

    def pcm(self, patch: pytest.MonkeyPatch, mic: int, far: list[int] | None) -> None:
        role = self.role
        archive = role.state_machine.archive
        assert archive is not None
        put = archive.put
        wanted = {"mic"} if far is None else {"mic", "far"}
        written = {track: threading.Event() for track in wanted}
        expected_far = None if far is None else tone(sum(far), 100)

        def put_pcm(kind: str, payload: Any) -> bool:
            accepted = put(kind, payload)
            if kind == "pcm" and accepted:
                track, data, *_ = payload
                expected = tone(mic, 100) if track == "mic" else expected_far
                if track in written and data == expected:
                    written[track].set()
            return accepted

        self.tick(0.1)
        with patch.context() as scoped:
            scoped.setattr(archive, "put", put_pcm)
            if far is not None:
                source = role.state_machine.sources["far"]
                assert isinstance(source, (MixedFarSource, MonitorStreamSource))
                with source._lock if isinstance(source, MixedFarSource) else source.lock:
                    pulses = [pulse for pulse in role.audio.pulses if pulse.recording]
                    assert len(pulses) == len(far)
                    for pulse, value in zip(pulses, far, strict=True):
                        pulse.give(tone(value, 100), round(self.now * 1000))
            role.audio.mics[0].chunks.append(tone(mic, 100))
            for track, event in written.items():
                assert event.wait(2), f"{track} PCM did not reach archive"

    def stop(self) -> None:
        role = self.role
        current = self.gui.recording.controller.meeting
        assert current is not None
        self.gui.recording.clicks.on_stop_clicked()
        role.finish("closed")
        role.finish("finalized")
        self.tick()
        assert self.gui.recording.controller.saved
        assert not self.gui.recording.overlay["active"]
        saved = {meeting.id: meeting.state for meeting in self.gui.store.list()}
        assert saved[current.id] == MeetingState.RECORDED
        assert not any(source.opened for source in role.audio.mics)
        assert all(not pulse.recording for pulse in role.audio.pulses)

    def wave_bytes(self, track: str, meeting_id: str = MID) -> bytes:
        (path,) = (self.gui.store.root / meeting_id / "audio").glob(f"{track}-*.wav")
        with wave.open(str(path), "rb") as wav:
            assert (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) == (1, 2, 16000)
            return wav.readframes(wav.getnframes())


@pytest.fixture
def browser_call(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Acceptance]:
    get_qapplication()
    # A filesystem socket inode only. No socket is bound, listened on or connected.
    with monkeypatch.context() as patch:
        patch.setenv("PULSE_SERVER", str(make_socket(tmp_path / "pulse")))
        patch.delenv("ASTRA_VOICE_RESOURCES", raising=False)
        scenario = Acceptance(tmp_path, Browser())
        try:
            yield scenario
        finally:
            for role in scenario.roles:
                role.checks.release_all()
                role.gate.advance.set()
            try:
                scenario.gui.bridge.shutdown()
            finally:
                for role in scenario.roles:
                    role.dispose()


def test_browser_offer_is_quiet_for_50_seconds_then_logs_timeout(
    browser_call: Acceptance, caplog: pytest.LogCaptureFixture
) -> None:
    call = browser_call
    with caplog.at_level(logging.INFO, logger="astra_voice.meetings"):
        call.enable()
        call.browser.set_stream(Facility.OUTPUT, FIRST_OUTPUT, present=True)
        call.poll_observer(1)
        call.poll_observer(6)
        assert call.view.calls == []
        assert not call.role.checks.requests
        call.browser.set_stream(Facility.MICROPHONE, MIC, present=True)
        call.poll_observer()
        call.qualify()
        assert call.view.calls == [SHOW]
        call.assert_quiet()
        for _ in range(5):
            call.poll_observer(10)
            call.assert_quiet()
        assert call.view.calls[-1] == ("remaining", 10)
        call.poll_observer(9.9)
        assert ("close",) not in call.view.calls
        call.poll_observer(0.1)
        assert call.view.calls[-1:] == [("close",)]
        assert caplog.messages.count(line("closed", "timeout", 60_000)) == 1
        call.poll_observer(6)
    assert call.view.calls.count(SHOW) == 1 and not call.call_frames("ended")
    assert call.view.calls.count(("close",)) == 1
    call.assert_quiet()


def test_browser_start_records_both_tracks_and_saves(
    browser_call: Acceptance, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    call = browser_call
    with caplog.at_level(logging.DEBUG, logger="astra_voice.meetings"):
        call.enable()
        call.begin_call()
        call_id = call.qualify()
        assert call.view.calls == [SHOW]
        call.start()
        role = call.role
        assert next(m["far"] for m in role.messages if m["type"] == PREPARE) == {
            "kind": "app_stream",
            "call_id": call_id,
        }
        (pulse,) = role.audio.pulses
        assert pulse.target is not None
        resolve = ("resolve", FIRST_OUTPUT)
        assert pulse.calls[:3] == [
            resolve,
            (pulse.target.monitor, FIRST_OUTPUT, "acceptance"),
            resolve,
        ]
        call.pcm(monkeypatch, mic=23, far=[42])
        call.stop()
    assert call.wave_bytes("mic") == tone(23, 100)
    assert call.wave_bytes("far") == tone(42, 100)
    trace = str(role.replies + role.messages) + caplog.text
    assert "S7_PRIVATE_MEDIA" not in trace


def test_continuing_call_is_suppressed_after_capture_role_replacement(
    browser_call: Acceptance, monkeypatch: pytest.MonkeyPatch
) -> None:
    call = browser_call
    call.enable()
    call.begin_call()
    original_id = call.qualify()
    call.start()
    call.pcm(monkeypatch, mic=22, far=[44])
    old = call.role
    call.stop()
    call.tick()
    assert old.exited and call.role is not old
    call.role.gate.ready()
    call.tick()
    continued_id = call.qualify()
    assert continued_id != original_id and call.view.calls.count(SHOW) == 1

    call.browser.set_stream(Facility.MICROPHONE, MIC, present=False)
    call.browser.set_stream(Facility.OUTPUT, FIRST_OUTPUT, present=False)
    call.poll_observer()
    call.poll_observer(1)
    call.poll_observer(11)
    assert call.call_frames("ended")[-1]["call_id"] == continued_id
    assert call.call_frames("ended")[-1]["reason"] == "mic_released"

    call.begin_call(first_output=FIRST_OUTPUT + 10)
    assert call.qualify() not in {original_id, continued_id}
    assert call.view.calls.count(SHOW) == 2
    assert call.command_count(PREPARE) == call.command_count(OPEN) == 1
    call.start()
    call.pcm(monkeypatch, mic=24, far=[46])
    call.stop()
    assert call.command_count(PREPARE) == call.command_count(OPEN) == 2
    assert call.wave_bytes("mic") == tone(22, 100)
    assert call.wave_bytes("far") == tone(44, 100)
    assert call.wave_bytes("mic", SECOND_MID) == tone(24, 100)
    assert call.wave_bytes("far", SECOND_MID) == tone(46, 100)


def restart_server(call: Acceptance) -> None:
    old = call.browser.connections[-1]
    call.browser.cookie += 1
    old.fail = True
    call.poll_observer()
    assert old.closed
    call.poll_observer(1.1)
    assert call.browser.connections[-1].cookie == call.browser.cookie


def test_server_reconnect_closes_live_offer_and_fences_the_old_start(
    browser_call: Acceptance, caplog: pytest.LogCaptureFixture
) -> None:
    call = browser_call
    with caplog.at_level(logging.INFO, logger="astra_voice.meetings"):
        call.enable()
        call.begin_call()
        stale = call.qualify()
        assert call.view.calls == [SHOW]
        restart_server(call)
        assert call.call_frames("ended") == [
            {"type": "meeting.call.ended", "call_id": stale, "reason": "server_changed"}
        ]
        assert call.view.calls[-1] == ("close",)
        assert line("closed", "server-changed", 1100) in caplog.messages
        call.press(accepted=False)
        call.poll_observer(6)
        assert call.view.calls.count(SHOW) == len(call.role.checks.requests) == 1
    call.assert_quiet()


def test_server_reconnect_during_recording_fences_far_and_preserves_mic(
    browser_call: Acceptance, monkeypatch: pytest.MonkeyPatch
) -> None:
    call = browser_call
    call.enable()
    call.begin_call()
    previous_id = call.qualify()
    call.start()
    role = call.role
    call.pcm(monkeypatch, mic=21, far=[41])
    (pulse,) = role.audio.pulses
    restart_server(call)
    assert call.call_frames("ended")[-1]["call_id"] == previous_id
    assert call.call_frames("ended")[-1]["reason"] == "server_changed"
    assert role.state_machine.backend is None
    assert role.audio.control_closed.wait(2)
    # Reusing the same sink-input number on the new server cannot authorize it.
    assert pulse.target is not None
    pulse.target = replace(pulse.target, cookie=call.browser.cookie)
    pulse.events.append(("server", 0))
    role.finish("track-ended")
    assert call.gui.recording.controller.state == "recording"
    assert call.gui.recording.overlay["active"] and not pulse.recording
    call.pcm(monkeypatch, mic=22, far=None)
    call.stop()
    assert call.wave_bytes("far") == tone(41, 100)
    assert call.wave_bytes("mic") == tone(21, 100) + tone(22, 100)
    assert role.audio.control_opens == 1


def test_replacing_role_while_w1_is_open_fences_late_offer(
    browser_call: Acceptance, caplog: pytest.LogCaptureFixture
) -> None:
    call = browser_call
    with caplog.at_level(logging.INFO, logger="astra_voice.meetings"):
        call.enable()
        call.begin_call()
        call.qualify()
        old = call.role
        (stale,) = call.call_frames("started")
        old.simulate_exit()
        assert call.view.calls[-1] == ("close",)
        call.tick(5)
        assert call.role is not old
        call.role.gate.ready()
        call.tick()
        # Exercise both a late wire frame and a callback already decoded by the old role.
        receive(old, stale)
        old._emit(stale, generation=old.generation)
        call.tick()
        assert line("dropped", "old-serial") in caplog.messages
        assert call.view.calls.count(SHOW) == 1
    call.assert_quiet()


@pytest.mark.parametrize("outputs", range(2, 9))
def test_losing_one_output_keeps_mic_and_surviving_far_audio(
    browser_call: Acceptance, monkeypatch: pytest.MonkeyPatch, outputs: int
) -> None:
    call = browser_call
    call.enable()
    call.begin_call(outputs)
    call.qualify()
    call.start()
    pulses = call.role.audio.pulses
    assert len(pulses) == outputs
    values = list(range(10, 10 + outputs))
    call.pcm(monkeypatch, mic=31, far=values)

    lost = pulses[1]
    assert lost.target is not None
    lost.events.append(("removed", lost.target.index))
    call.browser.set_stream(Facility.OUTPUT, lost.target.index, present=False)
    call.role.finish("far-partial")
    assert lost.destroyed_event.wait(2) and not lost.recording
    assert call.gui.recording.controller.state == "recording"
    assert call.gui.recording.overlay["state"] == "far-partial"
    assert call.gui.recording.overlay["secondary"] == "Часть звука не записывается"
    survivors = [value + 100 for i, value in enumerate(values) if i != 1]
    call.pcm(monkeypatch, mic=32, far=survivors)
    assert len(pulses) == outputs and call.command_count(OPEN) == 1
    call.stop()
    assert call.wave_bytes("mic") == tone(31, 100) + tone(32, 100)
    assert call.wave_bytes("far") == tone(sum(values), 100) + tone(sum(survivors), 100)


def test_remote_server_cannot_offer_or_open_program_audio(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    get_qapplication()
    with monkeypatch.context() as patch:
        host = "tcp:s7-no-connection.invalid:4713"
        patch.setenv("PULSE_SERVER", host)
        client_config = tmp_path / "client.conf"
        client_config.write_text(f"default-server = {host}\nautospawn = no\n", encoding="ascii")
        patch.setenv("PULSE_CLIENTCONFIG", str(client_config))
        call = Acceptance(tmp_path, Browser())
        try:
            call.gui.audio = replace(call.gui.audio, remote=True)
            call.browser.set_stream(Facility.OUTPUT, FIRST_OUTPUT, present=True)
            call.browser.set_stream(Facility.MICROPHONE, MIC, present=True)
            call.enable(local=False)
            call.tick(6)
            assert call.view.calls == []
            assert any(reply.get("code") == "remote" for reply in call.role.replies)
            call.gui.bridge.requestRecording()
            call.tick()
            assert call.gui.bridge.startWarnings == [WARNING_TEXT["audio-remote"]]
            assert [option["id"] for option in call.gui.bridge.startOptions] == ["room", "all"]
            call.gui.bridge.selectRecordingSource("call")
            assert call.gui.bridge.selectedSource == "room"
            assert not call.browser.addresses and not call.role.checks.requests
            call.assert_quiet()

            role = call.role
            role.audio.server_local = False
            call.gui.bridge.on_start_clicked()
            call.finish_open()
            assert next(m["far"] for m in role.messages if m["type"] == PREPARE) == {"kind": "none"}
            assert role.audio.mics[0].opened and not role.audio.pulses
            call.pcm(monkeypatch, mic=24, far=None)
            call.stop()
            assert call.wave_bytes("mic") == tone(24, 100)
            assert not call.browser.addresses and not role.checks.requests
        finally:
            call.gui.bridge.shutdown()
            for role in call.roles:
                role.dispose()


def test_unlock_reoffers_only_after_a_fresh_card(
    browser_call: Acceptance, caplog: pytest.LogCaptureFixture
) -> None:
    call = browser_call
    with caplog.at_level(logging.INFO, logger="astra_voice.meetings"):
        call.enable()
        call.begin_call()
        call_id = call.qualify()
        session = call.gui.session
        session.value = replace(session.value, locked=True, blocked_epoch=3)
        session.changed.emit(session.value)
        assert call.view.calls == [SHOW, ("close",)]
        assert line("closed", "session-blocked") in caplog.messages
        assert line("memory", "remember-lock-closed") in caplog.messages
        call.press(accepted=False)
        call.poll_observer(1)
        assert call.command_count(QUERY) == 0

        session.value = replace(session.value, locked=False)
        session.changed.emit(session.value)
        call.poll_observer(4.9)
        assert call.command_count(QUERY) == 0 and call.view.calls.count(SHOW) == 1
        card = call.role.checks.plan(call.browser.result(call.now + 0.1))
        call.poll_observer(0.1)
        assert card.started().purpose == Purpose.CARD
        assert call.command_count(QUERY) == 1 and call.view.calls.count(SHOW) == 1
        card.complete()
        call.role.pump()
        call.tick()
        (result,) = call.call_frames("state")
        assert result["call_id"] == call_id and result["variant1"]
        assert call.view.calls.count(SHOW) == 2
    call.assert_quiet()


def test_fresh_start_refusal_shows_w4_and_waits_for_second_click(
    browser_call: Acceptance, monkeypatch: pytest.MonkeyPatch
) -> None:
    call = browser_call
    call.enable()
    call.begin_call()
    call.qualify()
    call.tick(0.6)
    refused = replace(call.browser.result(call.now), variant1=False, target=None)
    run = call.role.checks.plan(refused)
    call.press()
    assert run.started().purpose == Purpose.START
    run.complete()
    call.role.finish("prepare-failed")
    call.tick()
    assert call.view.calls[-1] == ("close",)
    assert_call_lost(call.gui, BROWSER_LOST)
    assert call.command_count(PREPARE) == 1 and call.command_count(OPEN) == 0
    call.assert_no_audio()
    assert call.gui.store.list() == []

    call.gui.bridge.on_start_clicked()
    call.finish_open()
    assert [m["far"] for m in call.role.messages if m["type"] == PREPARE][-1] == {"kind": "none"}
    assert call.command_count(OPEN) == 1 and not call.role.audio.pulses
    call.pcm(monkeypatch, mic=24, far=None)
    call.stop()
    assert call.wave_bytes("mic", SECOND_MID) == tone(24, 100)
    assert not list((call.gui.store.root / SECOND_MID / "audio").glob("far-*.wav"))
