"""E13.M8 wiring of the observer, rule, queue and the independent capture control."""

from __future__ import annotations

import queue
import threading
import time
from collections.abc import Callable
from contextlib import nullcontext
from pathlib import Path
from typing import Any

import pytest
from test_meeting_capture import Clock, Rig, wait_until
from test_meeting_m8_detector_source import CALL_PROGRAM, stream
from test_meeting_m8_locality import (
    ObserverPulse,
    ObserverPulses,
    address,
    detector,
    make_socket,
)

from astra_voice.meetings.apps import AppClass
from astra_voice.worker import meeting_observer
from astra_voice.worker.meeting_audio_local import CONNECT_LOCK, LocalAudioServer
from astra_voice.worker.meeting_detector import (
    RETRY_SECONDS,
    Direction,
    MeetingDetectorSource,
    NormalizedEvent,
    PictureTiming,
)
from astra_voice.worker.meeting_identity_check import CheckRequest, CheckResult, Purpose
from astra_voice.worker.meeting_metadata import PulseMetadata
from astra_voice.worker.meeting_observer import (
    ObserverBackend,
    ObserverConnect,
    ObserverServer,
    observer_metadata,
)
from astra_voice.worker.pulse_stream import PA_CONTEXT_FAILED

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("filesystem_euid")]


class NoCallCheck:
    def __init__(self) -> None:
        self.requests: list[CheckRequest] = []

    def run(self, request: CheckRequest) -> CheckResult:
        self.requests.append(request)
        return CheckResult(True, None, AppClass.OTHER, False, False, (), 0, 1, None, None)

    def shutdown(self, deadline: float) -> bool:
        return True


def local_rig(root: Path, patch: pytest.MonkeyPatch, **kwargs: Any) -> Rig:
    patch.delenv("ASTRA_VOICE_RESOURCES", raising=False)
    kwargs.setdefault("observer_factory", observer_metadata)
    kwargs.setdefault("observer_fds", lambda: {})
    native = make_socket(root / "native")
    return Rig(
        root,
        observer_address=lambda: LocalAudioServer(address(native), "pulse_server"),
        **kwargs,
    )


def assert_no_pcm(rig: Rig, pulses: ObserverPulses) -> None:
    pulses.assert_no_pcm()
    assert rig.mics == [] and rig.backend.sources == []


def assert_control_untouched(rig: Rig) -> None:
    assert rig.backend.close_calls == 0
    rig.backend.control.close.assert_not_called()


class ObserverSteps:
    """The test changes fake Pulse data only between two completed polls."""

    def __init__(self) -> None:
        self.clock: Clock
        self.arrived = threading.Event()
        self.advance = threading.Event()
        self.delays: list[float] = []
        self.tenths = 1000

    def wait(self, wake: threading.Event, seconds: float) -> bool:
        self.delays.append(seconds)
        self.arrived.set()
        if not self.advance.wait(2):
            raise TimeoutError()
        self.advance.clear()
        if wake.is_set():
            return True
        self.tenths += round(seconds * 10)
        self.clock.now = self.tenths / 10
        return False

    def ready(self) -> None:
        assert self.arrived.wait(2), "observer did not reach next wait"
        self.arrived.clear()

    def step(self, count: int = 1) -> None:
        for _ in range(count):
            self.advance.set()
            self.ready()


class ObservedConnectLock:
    def __init__(
        self,
        *,
        entering: Callable[[], None] = lambda: None,
        released: Callable[[], None] = lambda: None,
    ) -> None:
        self.lock = threading.Lock()
        self.entering = entering
        self.released = released

    def __enter__(self) -> None:
        self.entering()
        self.lock.acquire()

    def __exit__(self, *exc: object) -> None:
        self.lock.release()
        self.released()


def observer_idle(rig: Rig, patch: pytest.MonkeyPatch) -> threading.Event:
    idle = threading.Event()
    condition = rig.state.observer._condition
    wait_for = condition.wait_for

    def before_idle(predicate: Callable[[], bool], timeout: float | None = None) -> bool:
        if not predicate():
            idle.set()
        return wait_for(predicate, timeout)

    patch.setattr(condition, "wait_for", before_idle)
    return idle


class ObservedQueue(queue.SimpleQueue[dict[str, Any]]):
    def __init__(self) -> None:
        super().__init__()
        self.observations: list[dict[str, Any]] = []

    def put(
        self, item: dict[str, Any], /, block: bool = True, timeout: float | None = None
    ) -> None:
        if item["type"] in {"detector-candidate", "detector-call-ended", "detector-server-changed"}:
            self.observations.append(item)
        super().put(item, block, timeout)


def test_without_start_local_role_never_subscribes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with monkeypatch.context() as patch:
        pulses = ObserverPulses(patch)
        checker = NoCallCheck()
        rig = local_rig(tmp_path, patch, check_factory=lambda *_: checker)
        try:
            rig.prepare("none")
            kind = "meeting.call.query"
            replies = rig.state.handle({"type": kind, "request_type": kind, "req": "no-start"})
            assert [event["type"] for event in replies] == ["meeting.ack"]
            assert rig.until("meeting.call.state")["active"] is False
            assert [r.purpose for r in checker.requests] == [Purpose.CARD]
            assert rig.send("keepalive", seq=1)[0]["type"] == "meeting.ack"
            assert rig.state.observer._thread is None
            assert pulses.count("pa_context_connect") == pulses.count("pa_context_new") == 0
        finally:
            rig.cleanup()
        assert_no_pcm(rig, pulses)


@pytest.mark.parametrize("finish", ["stop", "cleanup"])
def test_stop_closes_only_observer_and_preserves_capture_control(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, finish: str
) -> None:
    with monkeypatch.context() as patch:
        pulses = ObserverPulses(patch)
        rig = local_rig(tmp_path, patch)
        try:
            registry = rig.state.observer.registry
            blank = registry.snapshot()
            assert blank.version == 0 and blank.server_cookie is None and not blank.indexes
            rig.prepare()
            control = registry.snapshot()
            assert (control.version, control.indexes, control.server_cookie) == (
                1,
                frozenset({51}),
                1,
            )
            assert not registry.for_observer(91)
            detector(rig, "start", "start")
            rig.until("meeting.detector.started")
            wait_until(lambda: registry.for_observer(17) == {42, 51})
            connected = registry.snapshot()
            assert (connected.version, connected.indexes, connected.server_cookie) == (
                2,
                frozenset({42, 51}),
                1,
            )
            assert pulses.pulses[0].owner.name == "meeting-detector"
            assert pulses.pulses[0].owner.daemon
            if finish == "stop":
                detector(rig, "stop", "stop")
                rig.until("meeting.detector.stopped")
                wait_until(lambda: not registry.for_observer(17))
            else:
                start = time.monotonic()
                rig.cleanup()
                assert time.monotonic() - start < 1
                assert not pulses.pulses[0].owner.is_alive()
            remaining = registry.snapshot()
            assert (remaining.version, remaining.indexes, remaining.server_cookie) == (
                3,
                frozenset({51}),
                1,
            )
            assert not registry.for_observer(99)
            assert rig.state.observer.epoch == 1
            assert pulses.count("pa_context_disconnect") == 1
            assert_control_untouched(rig)
            assert_no_pcm(rig, pulses)
            if finish == "stop":
                rig.open()
                assert len(rig.backend.sources) == 1 and rig.backend.sources[0].opened
        finally:
            rig.cleanup()
        assert_control_untouched(rig)


@pytest.mark.parametrize("stopping", ["stop", "stop-start", "cleanup"])
def test_role_remains_responsive_during_blocked_observer_poll(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stopping: str
) -> None:
    entered, release, returned = threading.Event(), threading.Event(), threading.Event()

    def block_poll() -> None:
        entered.set()
        try:
            if not release.wait(2):
                raise TimeoutError()
        finally:
            returned.set()
        raise OSError()  # libpulse lost its connection while poll was in flight

    def setup(pulse: ObserverPulse) -> None:
        if not entered.is_set():
            pulse.before_dispatch = block_poll

    with monkeypatch.context() as patch:
        pulses = ObserverPulses(patch, setup=setup)
        rig = local_rig(tmp_path, patch)
        try:
            detector(rig, "start", "start")
            assert entered.wait(2)
            owner = pulses.pulses[0].owner
            assert owner is not threading.current_thread()
            assert rig.state.backend is None
            assert rig.state.poll()[0]["type"] == "meeting.detector.started"
            detector(rig, "start", "same-running")
            assert rig.state.poll()[0]["req"] == "same-running"
            assert not returned.is_set() and not release.is_set()

            if stopping != "cleanup":
                detector(rig, "stop", "stop")
                assert rig.state.poll()[0]["type"] == "meeting.detector.stopped"
                if stopping == "stop-start":
                    detector(rig, "start", "restart")
                    assert rig.state.poll()[0]["req"] == "restart"
            rig.prepare("sink_monitor")  # handled while the observer is still in poll
            assert rig.send("keepalive", seq=1)[0]["type"] == "meeting.ack"
            assert rig.state.poll() == []
            assert pulses.count("pa_context_new") == 1
            assert [t for t in threading.enumerate() if t.name == "meeting-detector"] == [owner]
            assert rig.backend.prepare_calls == [{"kind": "sink_monitor"}]
            assert not returned.is_set()

            if stopping == "cleanup":
                start = time.monotonic()
                rig.cleanup()
                assert time.monotonic() - start < 1.25
                assert owner.is_alive() and not returned.is_set()
                assert pulses.count("pa_context_disconnect") == 0
                release.set()
                owner.join(2)
                assert not owner.is_alive()
            else:
                if stopping == "stop-start":
                    rig.clock.now += 2 * RETRY_SECONDS
                release.set()
                wait_until(lambda: pulses.count("pa_context_disconnect") >= 1)
                if stopping == "stop-start":
                    wait_until(lambda: rig.state.observer.registry.for_observer(17) == {43, 51})
                    assert pulses.count("pa_context_new") == 2
                    assert pulses.pulses[1].owner is owner
                    detector(rig, "stop", "second-stop")
                    rig.until("meeting.detector.stopped")
                wait_until(lambda: not rig.state.observer.registry.for_observer(1))
                assert not rig.state.stopped and rig.state.exit_code == 0
            assert returned.is_set()
            assert_control_untouched(rig)
        finally:
            release.set()
            rig.cleanup()
        assert pulses.count("pa_context_new") == (2 if stopping == "stop-start" else 1)
        assert pulses.count("pa_context_disconnect") == pulses.count("pa_context_new")
        assert_no_pcm(rig, pulses)


def test_prepare_can_run_while_observer_connect_waits_for_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered, release = threading.Event(), threading.Event()

    def setup(pulse: ObserverPulse) -> None:
        pulse.state = 1  # PA_CONTEXT_CONNECTING

        def ready() -> None:
            entered.set()
            if not release.wait(2):
                raise TimeoutError()
            pulse.state = 4

        pulse.before_dispatch = ready

    with monkeypatch.context() as patch:
        pulses = ObserverPulses(patch, setup=setup)
        rig = local_rig(tmp_path, patch)
        original_prepare = rig.backend.prepare

        def prepare(far: dict[str, Any]) -> dict[str, Any]:
            with CONNECT_LOCK:
                return original_prepare(far)

        patch.setattr(rig.backend, "prepare", prepare)
        try:
            detector(rig, "start", "start")
            assert entered.wait(2)
            assert rig.state.observer.registry.for_observer(0) == frozenset()
            rig.prepare()
            assert not release.is_set()
            assert rig.state.observer.registry.snapshot().indexes == {51}
            release.set()
            wait_until(lambda: rig.state.observer.registry.for_observer(0) == {42, 51})
        finally:
            release.set()
            rig.cleanup()
        assert pulses.count("pa_context_disconnect") == 1
        assert_no_pcm(rig, pulses)


@pytest.mark.parametrize("blocked_at", ["ready", "cookie"])
def test_stop_during_observer_confirmation_leaves_no_server_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, blocked_at: str
) -> None:
    entered, release = threading.Event(), threading.Event()
    steps = ObserverSteps()
    cookie = 286331153

    def pause() -> None:
        entered.set()
        if not release.wait(2):
            raise TimeoutError()

    def setup(pulse: ObserverPulse) -> None:
        if pulse.own_index == 42 and blocked_at == "ready":
            pulse.state = 1  # PA_CONTEXT_CONNECTING

            def ready() -> None:
                pause()
                pulse.state = 4  # PA_CONTEXT_READY

            pulse.before_dispatch = ready

    original_cookie = PulseMetadata.server_cookie

    def read_cookie(backend: PulseMetadata) -> int:
        if backend.own_index == 42:
            pause()
        return original_cookie(backend)

    with monkeypatch.context() as patch:
        if blocked_at == "cookie":
            patch.setattr(PulseMetadata, "server_cookie", read_cookie)
        pulses = ObserverPulses(patch, cookies=(cookie,), setup=setup)
        rig = local_rig(tmp_path, patch, observer_wait=steps.wait)
        steps.clock = rig.clock
        inbox = ObservedQueue()
        rig.state.messages = inbox
        idle = observer_idle(rig, patch)
        registry = rig.state.observer.registry
        try:
            detector(rig, "start", "pending")
            assert entered.wait(2)
            detector(rig, "stop", "cancel")
            assert [m["type"] for m in rig.poll()] == [
                "meeting.detector.started",
                "meeting.detector.stopped",
            ]
            release.set()
            assert idle.wait(2)
            assert inbox.observations == []
            assert registry.snapshot().version == 0
            assert not registry.for_observer(0)
            assert pulses.count("pa_context_disconnect") == 1
            assert pulses.count("pa_context_get_server_info") == int(blocked_at == "cookie")

            detector(rig, "start", "confirmed")
            steps.ready()
            (event,) = inbox.observations
            assert event == {
                "type": "detector-server-changed",
                "epoch": 1,
                "server": ObserverServer(cookie),
            }
            assert registry.for_observer(1) == {43}
        finally:
            rig.state.terminate()
            release.set()
            steps.advance.set()
            rig.cleanup()
        assert pulses.count("pa_context_disconnect") == 2
        assert_no_pcm(rig, pulses)


def test_observer_server_event_repr_hides_cookie(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    steps = ObserverSteps()
    cookie = 286331153
    other_cookie = 572662306
    with monkeypatch.context() as patch:
        pulses = ObserverPulses(patch, cookies=(cookie, other_cookie))
        rig = local_rig(tmp_path, patch, observer_wait=steps.wait)
        steps.clock = rig.clock
        inbox = ObservedQueue()
        rig.state.messages = inbox
        try:
            detector(rig, "start", "confirm-cookie")
            steps.ready()
            (event,) = inbox.observations
            assert event["type"] == "detector-server-changed"
            assert event["server"].cookie == cookie
            assert str(cookie) not in repr(event)
            pulses.pulses[0].state = PA_CONTEXT_FAILED
            steps.step(12)
            first, second = inbox.observations
            assert first is event
            assert second["type"] == event["type"] and second["epoch"] == event["epoch"]
            assert second["server"].cookie == other_cookie
            assert repr(first) == repr(second)
        finally:
            rig.state.terminate()
            steps.advance.set()
            rig.cleanup()


def test_reconnect_reports_server_change_and_fences_old_cookie(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    steps = ObserverSteps()
    with monkeypatch.context() as patch:
        pulses = ObserverPulses(patch, cookies=(1, 2))
        rig = local_rig(tmp_path, patch, observer_wait=steps.wait)
        steps.clock = rig.clock
        inbox = ObservedQueue()
        rig.state.messages = inbox
        try:
            rig.prepare()
            detector(rig, "start", "start")
            steps.ready()
            registry = rig.state.observer.registry
            assert registry.for_observer(10) == {42, 51}
            assert registry.snapshot().version == 2
            pulses.pulses[0].state = PA_CONTEXT_FAILED
            steps.step()
            assert registry.for_observer(10) == frozenset()
            assert registry.snapshot().version == 3
            steps.step(11)
            view = registry.snapshot()
            assert (view.version, view.indexes, view.server_cookie) == (4, frozenset({43}), 2)
            assert registry.for_observer(12) == {43}
            first, second = inbox.observations
            assert first == {
                "type": "detector-server-changed",
                "epoch": 0,
                "server": ObserverServer(1),
            }
            assert second == {
                "type": "detector-server-changed",
                "epoch": 0,
                "server": ObserverServer(2),
            }
            assert [m["type"] for m in rig.poll()] == [
                "meeting.detector.started",
                "meeting.capture.alive",
            ]
            assert not rig.state.stopped and rig.state.exit_code == 0
            view = registry.snapshot()
            assert (view.version, view.indexes, view.server_cookie) == (5, frozenset({43}), 2)
            detector(rig, "stop", "stop")
            steps.advance.set()
            wait_until(lambda: not registry.for_observer(12))
            view = registry.snapshot()
            assert (view.version, view.indexes, view.server_cookie) == (6, frozenset(), None)
        finally:
            rig.state.terminate()
            steps.advance.set()
            rig.cleanup()
        assert [p.calls.count("pa_context_get_server_info") for p in pulses.pulses] == [1, 1]
        assert pulses.count("pa_context_disconnect") == 2
        assert_no_pcm(rig, pulses)


def test_call_start_and_end_reach_only_the_role_queue_with_virtual_clock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    steps = ObserverSteps()
    with monkeypatch.context() as patch:
        pulses = ObserverPulses(patch, setup=lambda pulse: pulse.streams.clear())
        checker = NoCallCheck()
        rig = local_rig(tmp_path, patch, observer_wait=steps.wait, check_factory=lambda *_: checker)
        steps.clock = rig.clock
        inbox = ObservedQueue()
        rig.state.messages = inbox
        try:
            detector(rig, "start", "start")
            steps.ready()  # Empty first snapshot. Streams arrive before the next poll.
            assert inbox.observations.pop() == {
                "type": "detector-server-changed",
                "epoch": 0,
                "server": ObserverServer(1),
            }
            assert [m["type"] for m in rig.poll()] == ["meeting.detector.started"]
            pulse = pulses.pulses[0]
            pair_at = rig.clock.now
            pulse.streams = [stream(), stream(8, direction=Direction.MICROPHONE)]
            pulse.notices = [(2, 7), (3, 8)]
            steps.step(40)
            assert rig.clock.now == pair_at + 5 - 1
            assert inbox.observations == []
            steps.step(21)
            assert rig.clock.now == pair_at + 6.1
            assert inbox.observations == [
                {
                    "type": "detector-candidate",
                    "program": CALL_PROGRAM,
                    "offered": False,
                    "epoch": 0,
                }
            ]
            assert all(m["type"] == "meeting.detector.code" for m in rig.poll())
            assert rig.until("meeting.detector.code") == {
                "type": "meeting.detector.code",
                "purpose": "offer",
                "code": "no_call",
                "duration_ms": 0,
            }
            assert [r.purpose for r in checker.requests] == [Purpose.OFFER]

            mic_left_at = rig.clock.now
            pulse.streams.pop()
            pulse.notices = [(3 | 32, 8)]
            steps.step(90)
            assert rig.clock.now == mic_left_at + 10 - 1
            assert len(inbox.observations) == 1
            steps.step(21)
            assert round(rig.clock.now - mic_left_at, 1) == 11.1
            assert inbox.observations[1:] == [
                {"type": "detector-call-ended", "program": CALL_PROGRAM, "epoch": 0}
            ]
            assert rig.state.poll() == []
        finally:
            rig.state.terminate()
            steps.advance.set()
            rig.cleanup()
        assert pulses.count("pa_context_disconnect") == 1
        assert_no_pcm(rig, pulses)


@pytest.mark.parametrize("blocked_at", ["factory", "lock", "snapshot"])
def test_stop_cancels_pending_connect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, blocked_at: str
) -> None:
    steps = ObserverSteps()
    entered, proceed = threading.Event(), threading.Event()
    reads: list[bool] = []
    lock_entries = 0

    def pause() -> None:
        entered.set()
        if not proceed.wait(2):
            raise TimeoutError()

    def entering_lock() -> None:
        nonlocal lock_entries
        lock_entries += 1
        if lock_entries == 2:  # The old context was created. _connect is entering the lock.
            pause()

    def factory(server: bytes, connect: ObserverConnect) -> ObserverBackend:
        if blocked_at == "factory":
            pause()
        return observer_metadata(server, connect)

    def read_fds() -> dict[int, int]:
        reads.append(True)
        if blocked_at == "snapshot" and len(reads) == 1:
            pause()
        return {}

    with monkeypatch.context() as patch:
        pulses = ObserverPulses(patch)
        lock = ObservedConnectLock(entering=entering_lock)
        if blocked_at == "lock":
            patch.setattr(meeting_observer, "CONNECT_LOCK", lock)
        rig = local_rig(
            tmp_path,
            patch,
            observer_factory=factory,
            observer_fds=read_fds,
            observer_wait=steps.wait,
        )
        steps.clock = rig.clock
        idle = observer_idle(rig, patch)
        try:
            detector(rig, "start", "start")
            assert entered.wait(2)
            with lock.lock if blocked_at == "lock" else nullcontext():
                detector(rig, "stop", "stop")
                assert [m["type"] for m in rig.poll()] == [
                    "meeting.detector.started",
                    "meeting.detector.stopped",
                ]
                assert pulses.count("pa_context_connect") == 0
                if blocked_at == "lock":
                    detector(rig, "start", "restart")
                    assert rig.state.observer.epoch == 1
                proceed.set()
            if blocked_at == "lock":
                steps.ready()
                assert pulses.pulses[0].connections == []
                assert len(pulses.pulses) == 2
                assert len(pulses.pulses[1].connections) == 1
                assert len(reads) == 2  # Only the new epoch's snapshots.
            else:
                assert idle.wait(2)
                assert pulses.count("pa_context_connect") == 0
        finally:
            rig.state.terminate()
            proceed.set()
            steps.advance.set()
            rig.cleanup()
        owner = rig.state.observer._thread
        assert owner is not None and not owner.is_alive()
        assert pulses.count("pa_context_new") == (2 if blocked_at == "lock" else 1)
        assert pulses.count("pa_context_unref") == pulses.count("pa_context_new")
        assert pulses.count("pa_mainloop_free") == pulses.count("pa_mainloop_new")
        assert all(pulse.mainloop is None for pulse in pulses.pulses)
        assert pulses.count("pa_context_connect") == int(blocked_at == "lock")
        assert len(reads) == {"factory": 0, "lock": 2, "snapshot": 1}[blocked_at]
        assert rig.state.observer.registry.for_observer(0) == frozenset()
        rig.poll()
        assert not any(message["type"] == "meeting.detector.code" for message in rig.events)


@pytest.mark.parametrize("next_action", ["cleanup", "start"])
def test_failed_thread_start_allows_cleanup_or_new_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, next_action: str
) -> None:
    steps = ObserverSteps()
    attempts: list[threading.Thread] = []
    real_start = threading.Thread.start

    def start_once(thread: threading.Thread) -> None:
        if thread.name == "meeting-detector":
            attempts.append(thread)
            if len(attempts) == 1:
                raise RuntimeError("simulated thread creation failure")
        real_start(thread)

    with monkeypatch.context() as patch:
        pulses = ObserverPulses(patch)
        rig = local_rig(tmp_path, patch, observer_wait=steps.wait)
        steps.clock = rig.clock
        try:
            with monkeypatch.context() as start_patch:
                start_patch.setattr(threading.Thread, "start", start_once)
                rig.state.handle(
                    {
                        "type": "meeting.detector.start",
                        "request_type": "meeting.detector.start",
                        "req": "start-failed",
                        "apps_version": 1,
                    }
                )
                assert len(attempts) == 1
                assert pulses.count("pa_context_connect") == 0
                if next_action == "cleanup":
                    rig.cleanup()
                    assert rig.state.observer._thread is None
                else:
                    detector(rig, "start", "retry")
                    steps.ready()
                    assert len(attempts) == 2
                    assert pulses.pulses[0].owner is attempts[1] is rig.state.observer._thread
                    assert [t for t in threading.enumerate() if t.name == "meeting-detector"] == [
                        attempts[1]
                    ]
                    assert pulses.count("pa_context_connect") == 1
        finally:
            rig.state.terminate()
            steps.advance.set()
            rig.cleanup()


@pytest.mark.parametrize("operation", ["reset", "source-stop"])
def test_start_resumes_after_failed_final_reset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    steps = ObserverSteps()
    fail_reset, raised = threading.Event(), threading.Event()
    with monkeypatch.context() as patch:
        pulses = ObserverPulses(patch)
        rig = local_rig(tmp_path, patch, observer_wait=steps.wait)
        steps.clock = rig.clock
        observer = rig.state.observer
        update = observer._update
        stop = MeetingDetectorSource.stop

        def fail_once() -> None:
            if fail_reset.is_set() and not raised.is_set():
                raised.set()
                raise RuntimeError("simulated exception during the stop reset")

        def update_once(
            events: tuple[NormalizedEvent, ...], timing: PictureTiming, epoch: int
        ) -> None:
            fail_once()
            update(events, timing, epoch)

        def stop_once(source: MeetingDetectorSource) -> tuple[NormalizedEvent, ...]:
            events = stop(source)
            fail_once()
            return events

        if operation == "source-stop":
            patch.setattr(MeetingDetectorSource, "stop", stop_once)
        else:
            patch.setattr(observer, "_update", update_once)
        try:
            detector(rig, "start", "start")
            steps.ready()
            fail_reset.set()
            detector(rig, "stop", "stop")
            steps.advance.set()
            assert raised.wait(2)
            assert pulses.count("pa_context_disconnect") == 1
            steps.tenths += round(2 * RETRY_SECONDS * 10)
            rig.clock.now = steps.tenths / 10
            detector(rig, "start", "restart")
            steps.ready()
            assert pulses.count("pa_context_connect") == 2
            assert pulses.pulses[0].owner is pulses.pulses[1].owner
            assert pulses.pulses[1].owner.is_alive()
        finally:
            rig.state.terminate()
            steps.advance.set()
            rig.cleanup()


@pytest.mark.parametrize(
    ("operation", "interrupt"),
    [("start", False), ("poll", False), ("start", True)],
    ids=["start", "poll", "restart-during-backoff"],
)
def test_observer_recovers_after_source_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str, interrupt: bool
) -> None:
    steps = ObserverSteps()
    attempts: list[float] = []
    original = getattr(MeetingDetectorSource, operation)

    def fail_once(source: MeetingDetectorSource) -> tuple[NormalizedEvent, ...]:
        attempts.append(steps.clock())
        if len(attempts) == 1:
            raise OSError("simulated one-off source exception")
        return original(source)  # type: ignore[no-any-return]

    with monkeypatch.context() as patch:
        pulses = ObserverPulses(patch)
        rig = local_rig(tmp_path, patch, observer_wait=steps.wait)
        steps.clock = rig.clock
        patch.setattr(MeetingDetectorSource, operation, fail_once)
        try:
            detector(rig, "start", "start")
            steps.ready()
            if operation == "poll":
                steps.step()  # Fail the first poll after the initial connection.
            first_attempt = steps.clock()
            assert attempts == [first_attempt]
            assert steps.delays[-1] == RETRY_SECONDS
            assert not rig.state.observer.registry.for_observer(0)
            if interrupt:
                detector(rig, "stop", "stop")
                detector(rig, "start", "restart")
            steps.step()
            assert rig.clock.now == first_attempt + (0 if interrupt else RETRY_SECONDS)
            assert rig.state.observer.registry.for_observer(0)
            expected = 1 if operation == "start" else 2
            assert pulses.count("pa_context_connect") == expected
            types = [item["type"] for item in rig.state.poll()]
            assert types == (
                ["meeting.detector.started", "meeting.detector.stopped", "meeting.detector.started"]
                if interrupt
                else ["meeting.detector.started"]
            )
        finally:
            rig.state.terminate()
            steps.advance.set()
            rig.cleanup()


@pytest.mark.parametrize("operation", ["new", "connect"])
def test_observer_locks_native_context_calls_and_fd_snapshots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    steps = ObserverSteps()
    observations: list[tuple[str, bool]] = []
    function = f"pa_context_{operation}"

    def setup(pulse: ObserverPulse) -> None:
        original = pulse.fake_lib.call

        def call(name: str, *args: Any) -> Any:
            if name == function:
                observations.append((name, CONNECT_LOCK.locked()))
            return original(name, *args)

        pulse.fake_lib.call = call

    def read_fds() -> dict[int, int]:
        if operation == "connect":
            observations.append(("fd", CONNECT_LOCK.locked()))
        return {}

    with monkeypatch.context() as patch:
        pulses = ObserverPulses(patch, setup=setup)
        rig = local_rig(tmp_path, patch, observer_fds=read_fds, observer_wait=steps.wait)
        steps.clock = rig.clock
        try:
            detector(rig, "start", "start")
            steps.ready()
            assert pulses.count("pa_context_connect") == 1
            if operation == "connect":
                assert observations == [("fd", True), (function, True), ("fd", True)]
            else:
                assert observations == [(function, True)]
        finally:
            rig.state.terminate()
            steps.advance.set()
            rig.cleanup()


def test_observer_keeps_connection_lock_until_second_fd_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    steps = ObserverSteps()
    first_snapshot = threading.Event()
    contender_entering, contender_acquired = threading.Event(), threading.Event()
    order: list[str] = []

    def enter() -> None:
        if threading.current_thread() is contender:
            contender_entering.set()

    def release() -> None:
        # Give a waiting contender the first turn if the observer releases the lock.
        if threading.current_thread() is not contender and first_snapshot.is_set():
            if not contender_acquired.wait(2):
                raise TimeoutError()

    def compete() -> None:
        if first_snapshot.wait(2):
            with lock:
                order.append("contender")
                contender_acquired.set()

    def read_fds() -> dict[int, int]:
        if not first_snapshot.is_set():
            order.append("first snapshot")
            first_snapshot.set()
            if not contender_entering.wait(2):
                raise TimeoutError()
        else:
            order.append("second snapshot")
        return {}

    def setup(pulse: ObserverPulse) -> None:
        original = pulse.fake_lib.call

        def call(name: str, *args: Any) -> Any:
            if name == "pa_context_connect":
                order.append("connect")
            return original(name, *args)

        pulse.fake_lib.call = call

    lock = ObservedConnectLock(entering=enter, released=release)
    contender = threading.Thread(target=compete)
    with monkeypatch.context() as patch:
        patch.setattr(meeting_observer, "CONNECT_LOCK", lock)
        pulses = ObserverPulses(patch, setup=setup)
        rig = local_rig(tmp_path, patch, observer_fds=read_fds, observer_wait=steps.wait)
        steps.clock = rig.clock
        contender.start()
        try:
            detector(rig, "start", "start")
            steps.ready()
            assert pulses.count("pa_context_connect") == 1
            assert order == ["first snapshot", "connect", "second snapshot", "contender"]
        finally:
            rig.state.terminate()
            steps.advance.set()
            rig.cleanup()
            first_snapshot.set()
            contender.join(2)
        assert not contender.is_alive()


def test_pending_microphone_return_does_not_end_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    steps = ObserverSteps()
    with monkeypatch.context() as patch:
        pulses = ObserverPulses(patch)
        rig = local_rig(tmp_path, patch, observer_wait=steps.wait)
        steps.clock = rig.clock
        inbox = ObservedQueue()
        rig.state.messages = inbox
        try:
            detector(rig, "start", "start")
            steps.ready()
            assert inbox.observations.pop() == {
                "type": "detector-server-changed",
                "epoch": 0,
                "server": ObserverServer(1),
            }
            steps.step(51)
            assert [item["type"] for item in inbox.observations] == ["detector-candidate"]
            pulse = pulses.pulses[0]

            pulse.streams.pop()
            pulse.notices = [(3 | 32, 8)]
            steps.step()
            off_deadline = round(rig.clock.now + 10, 1)
            steps.step(93)
            pulse.notices = [(2 | 16, 7)]
            steps.step()
            assert round(off_deadline - rig.clock.now, 1) == 0.6

            mic = stream(9, direction=Direction.MICROPHONE)
            pulse.streams.append(mic)
            pulse.notices = [(3, 9)]
            steps.step(6)
            assert rig.clock.now == off_deadline
            assert [item["type"] for item in inbox.observations] == ["detector-candidate"]
            steps.step(5)
            assert round(rig.clock.now - off_deadline, 1) == 0.5
            assert [item["type"] for item in inbox.observations] == ["detector-candidate"]
        finally:
            rig.state.terminate()
            steps.advance.set()
            rig.cleanup()


@pytest.mark.parametrize("after_stop", ["restart", "cleanup"])
def test_stop_resets_rule_before_restart_or_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, after_stop: str
) -> None:
    steps = ObserverSteps()
    with monkeypatch.context() as patch:
        pulses = ObserverPulses(patch)
        rig = local_rig(tmp_path, patch, observer_wait=steps.wait)
        steps.clock = rig.clock
        inbox = ObservedQueue()
        rig.state.messages = inbox
        observer = rig.state.observer
        idle = observer_idle(rig, patch)
        try:
            detector(rig, "start", "start")
            steps.ready()
            assert inbox.observations.pop() == {
                "type": "detector-server-changed",
                "epoch": 0,
                "server": ObserverServer(1),
            }
            steps.step(51)
            assert len(inbox.observations) == 1 and inbox.observations[0]["epoch"] == 0
            with observer.rule_lock:
                assert observer.rule.candidate(CALL_PROGRAM) is not None

            detector(rig, "stop", "stop")
            steps.advance.set()
            assert idle.wait(2)
            with observer.rule_lock:
                assert observer.rule.candidate(CALL_PROGRAM) is None

            if after_stop == "cleanup":
                owner = pulses.pulses[0].owner
                rig.cleanup()
                assert not owner.is_alive()
                return

            detector(rig, "start", "restart")
            steps.ready()
            assert inbox.observations.pop() == {
                "type": "detector-server-changed",
                "epoch": 1,
                "server": ObserverServer(1),
            }
            steps.step(51)
            assert pulses.count("pa_context_connect") == 2
            assert len(inbox.observations) == 2
            assert inbox.observations[-1]["type"] == "detector-candidate"
            assert inbox.observations[-1]["epoch"] == observer.epoch == 1
        finally:
            rig.state.terminate()
            with observer._condition:
                observer._condition.notify_all()
            steps.advance.set()
            rig.cleanup()
