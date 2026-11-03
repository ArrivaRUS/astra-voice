"""D9/T417: real controller and capture with fake native PCM endpoints only."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from test_meeting_capture import Rig as CaptureRig
from test_meeting_capture import wait_until

from astra_voice.core.command_mode import SessionSnapshot
from astra_voice.core.meeting_session import MeetingBridge, MeetingController, StorageStatus
from astra_voice.meetings.store import MeetingStore
from astra_voice.platform.session import SessionKind

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("filesystem_euid")]
MID = "2026-11-01_120000-cdef"


class Loopback:
    """Preserve queued wire boundaries; never call back inside send()."""

    ready = True
    exited = False
    generation = 1

    def __init__(self, capture: CaptureRig) -> None:
        self.capture = capture
        self.requests: list[dict[str, Any]] = []
        self.pending: list[dict[str, Any]] = []
        self.delayed: list[dict[str, Any]] = []
        self.controller: MeetingController
        self.kills = 0
        self.events: list[dict[str, Any]] = []

    def send(self, message: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        self.requests.append(message)
        self.pending.append(message)
        return message

    def pump(self, timeout: float = 0.0) -> None:
        pending, self.pending = self.pending, []
        events, self.delayed = self.delayed, []
        for request in pending:
            events.extend(self.capture.state.handle(request))
        events.extend(self.capture.state.poll())
        for event in events:
            self.events.append(event)
            self.controller.handle_event({**event, "generation": self.generation})

    def kill(self) -> None:
        self.kills += 1
        self.exited = True

    def set_meeting_generation(self, meeting_id: str, generation: int) -> None:
        pass


class Integrated:
    def __init__(self, root: Path) -> None:
        self.capture = CaptureRig(root / "capture-fixture")
        self.role = Loopback(self.capture)
        self.metadata_jobs: list[Any] = []
        self.snapshot = SessionSnapshot(known=True, locked=False, blocked_epoch=0)
        self.controller = MeetingController(
            MeetingStore(root / "gui-meetings"),
            self.role,
            session=lambda: self.snapshot,
            session_kind=SessionKind.KDE,
            clock=self.capture.clock,
            storage_probe=lambda _: StorageStatus(1000, True, False),
            enabled=lambda: True,
            policy_allowed=lambda: True,
            id_factory=lambda: MID,
            metadata_submit=self.metadata_jobs.append,
            filesystem_submit=lambda work: work(),
        )
        self.role.controller = self.controller
        self.bridge = MeetingBridge(self.controller)

    @property
    def now(self) -> float:
        return self.capture.clock.now

    @now.setter
    def now(self, value: float) -> None:
        self.capture.clock.now = value

    def tick(self) -> None:
        self.controller.tick()

    def until(self, predicate: Any) -> None:
        def ready() -> bool:
            self.tick()
            return bool(predicate())

        try:
            wait_until(ready)
        except AssertionError:
            raise AssertionError(
                f"controller={self.controller.state}, error={self.controller.error_code}, "
                f"capture={self.capture.state.state}, requests={self.role.requests[-3:]}, "
                f"events={self.role.events[-8:]}"
            ) from None

    def start(self) -> None:
        self.controller.set_indicators_visible(True)
        assert self.bridge.on_start_clicked({"kind": "sink_monitor"})
        self.until(lambda: self.controller.state == "recording")
        self.controller.set_indicators_visible(True)
        self.role.pump()
        assert len(self.live_sources()) == 2

    def drain_metadata(self) -> None:
        # The store concurrency contract is tested separately. Publish deferred
        # GUI snapshots only once capture has acknowledged its final fsync.
        jobs = list(self.metadata_jobs)
        self.metadata_jobs.clear()
        for job in jobs:
            job()
        self.controller._drain_writes()
        assert self.controller.error_code != "storage-unavailable"

    def recovered(self) -> None:
        self.until(lambda: self.capture.state.state == "recording")
        # The authoritative recovery is carried by the next 1 Hz alive packet.
        self.now += 1.01
        self.until(lambda: self.controller.state == "recording")

    def live_sources(self) -> list[Any]:
        return [s for s in [*self.capture.mics, *self.capture.backend.sources] if s.opened]

    def finalized_pause(self) -> None:
        def ready() -> bool:
            self.role.pump()
            return (
                self.controller.state == "paused"
                and not self.controller._finalizing
                and self.controller._close_deadline is None
            )

        wait_until(ready)
        assert not self.live_sources()

    def close(self) -> None:
        self.capture.cleanup()
        assert self.controller.close(timeout=1)


@pytest.fixture
def integrated(tmp_path: Path) -> Iterator[Integrated]:
    rig = Integrated(tmp_path)
    try:
        yield rig
    finally:
        rig.close()


def test_indicator_recovery_reopens_same_capture_and_marks_pause(integrated: Integrated) -> None:
    r = integrated
    r.start()
    identity = dict(r.capture.state.identity)
    r.now += 2
    r.controller.set_indicators_visible(False)
    r.controller.pause("indicators")
    r.finalized_pause()
    assert r.controller.pause_reasons == {"indicators"}
    assert not r.controller.manual_resume_required
    r.now += 14
    r.controller.set_indicators_visible(True)
    r.recovered()
    assert r.capture.state.identity["capture_id"] == identity["capture_id"]
    assert r.capture.state.identity["meeting_generation"] == identity["meeting_generation"]
    assert len(r.live_sources()) == 2
    assert r.role.kills == 0
    assert r.controller.indicator_recovery is not None
    assert r.controller.indicator_recovery["reason"] == "indicators"
    assert r.controller.indicator_recovery["duration_ms"] >= 14000
    assert r.controller.meeting is not None
    assert any(m.kind == "pause" and m.reason == "indicators" for m in r.controller.meeting.marks)
    r.controller.stop()
    r.until(lambda: r.controller.state == "stopped" and r.controller._capture_finalized)
    r.drain_metadata()
    assert r.controller.saved
    assert r.controller.store.list()[0].recorded_ms == r.controller.meeting.recorded_ms
    intervals = r.controller.meeting.intervals
    assert any(
        b.t_mono_ms - a.t_mono_ms >= 14000 and a.recorded_ms == b.recorded_ms
        for a, b in zip(intervals, intervals[1:], strict=False)
    )


def test_no_gui_ticks_lease_physically_closes_by_five_seconds(integrated: Integrated) -> None:
    r = integrated
    r.start()
    identity = r.capture.state.identity["capture_id"]
    r.now += 4.05

    def capture_only() -> bool:
        r.role.delayed.extend(r.capture.state.poll())
        return not r.live_sources()

    wait_until(capture_only)
    assert r.now <= 105
    r.role.pump()
    r.finalized_pause()
    assert r.controller.pause_reasons == {"lease"}
    r.now += 3
    r.recovered()
    assert r.capture.state.identity["capture_id"] == identity
    assert r.controller.indicator_recovery is not None
    assert r.controller.indicator_recovery["reason"] == "lease"


def test_lock_plus_indicator_loss_never_resumes_after_unlock(integrated: Integrated) -> None:
    r = integrated
    r.start()
    r.controller.set_indicators_visible(False)
    r.controller.pause("indicators")
    r.finalized_pause()
    local = r.capture.state.session
    local.signal("Lock")
    local.update(known=True, active=True, locked=True, sleeping=False)
    r.snapshot = SessionSnapshot(known=True, locked=True, blocked_epoch=1)
    r.tick()
    r.finalized_pause()
    local.signal("Unlock")
    local.update(known=True, active=True, locked=False, sleeping=False)
    r.snapshot = SessionSnapshot(known=True, locked=False, blocked_epoch=1)
    r.controller.set_indicators_visible(True)
    r.role.pump()
    r.now += 2
    for _ in range(8):
        r.tick()
    assert r.controller.state == "paused"
    assert r.controller.manual_resume_required
    assert "lock" in r.controller.pause_reasons
    assert not r.live_sources()
    assert len(r.capture.mics) == 1


def test_duplicate_indicator_pause_is_noop_and_sixty_minutes_stops(integrated: Integrated) -> None:
    r = integrated
    r.start()
    r.controller.set_indicators_visible(False)
    r.controller.pause("indicators")
    r.controller.pause("indicators")
    r.finalized_pause()
    pauses = [m for m in r.role.requests if m["type"] == "meeting.capture.pause"]
    assert len(pauses) == 1
    r.now += 3600.001
    r.tick()
    r.until(lambda: r.controller.state == "stopped" and r.controller._capture_finalized)
    r.drain_metadata()
    assert r.controller.saved
    assert not r.live_sources()
    assert r.controller.store.list()[0].state.value == "recorded"


@pytest.mark.parametrize(
    "invalid", ["foreign-capture", "old-revision", "stale-admission", "manual", "user", "unknown"]
)
def test_recovery_requires_current_identity_admission_and_only_automatic_reasons(
    tmp_path: Path, invalid: str
) -> None:
    r = CaptureRig(tmp_path)
    try:
        r.prepare()
        r.open()
        r.send("pause", reason="indicators")
        r.until("meeting.capture.finalized")
        fields = {
            "seq": 1,
            "indicators_visible": True,
            "blocked_epoch": r.state.session.snapshot().blocked_epoch,
            "admission_mono_ms": int(r.clock() * 1000),
        }
        if invalid == "foreign-capture":
            fields["capture_id"] = "foreign"
        elif invalid == "old-revision":
            fields["state_rev"] = 0
        elif invalid == "stale-admission":
            fields["admission_mono_ms"] -= 1001
        elif invalid == "manual":
            r.state.manual_resume_required = True
        else:
            r.send("pause", reason=invalid)
            r.poll()
        r.send("keepalive", **fields)
        for _ in range(5):
            r.poll()
        assert r.state.state == "paused"
        assert not any(s.opened for s in [*r.mics, *r.backend.sources])
        assert len(r.mics) == 1
    finally:
        r.cleanup()


@pytest.mark.parametrize("crossing", ["deadline", "indicator-loss"])
def test_automatic_open_rechecks_deadline_and_visibility_inside_native_connect(
    tmp_path: Path, crossing: str
) -> None:
    r = CaptureRig(tmp_path)
    try:
        r.clock.now = 0.0
        r.prepare()
        r.open()
        r.send("pause", reason="indicators")
        r.until("meeting.capture.finalized")
        assert r.state.pause_at == 0.0
        r.clock.now = 3599.99
        checks: list[bool] = []
        original = r.state.mic_factory

        def factory() -> Any:
            source: Any = original()

            def open(device: str | None = None) -> None:
                if crossing == "deadline":
                    r.clock.now = 3600.01
                else:
                    r.state.indicators_visible = False
                checks.append(source.admission_check())
                source.opened = checks[-1]

            source.open = open
            return source

        r.state.mic_factory = factory
        r.send(
            "keepalive",
            seq=1,
            indicators_visible=True,
            blocked_epoch=r.state.session.snapshot().blocked_epoch,
            admission_mono_ms=int(r.clock() * 1000),
        )
        wait_until(lambda: (r.poll(), bool(checks))[1])
        assert checks == [False]
        assert not any(s.opened for s in [*r.mics, *r.backend.sources])
    finally:
        r.cleanup()


def test_guard_loss_grace_physically_closes_both_tracks(tmp_path: Path) -> None:
    from astra_voice.ui.meeting_indicator_guard import MeetingIndicatorGuard

    r = Integrated(tmp_path)
    shown = [True, False]
    guard = MeetingIndicatorGuard(
        active=lambda: r.controller.state in {"recording", "paused", "opening"},
        overlay_visible=lambda: shown[0],
        tray_visible=lambda: shown[1],
        set_visible=r.controller.set_indicators_visible,
        pause=lambda: r.controller.pause("indicators"),
        capturing=lambda: r.controller.capturing_without_indicator_pause,
        clock=r.capture.clock,
    )
    try:
        r.start()
        guard.check()
        shown[0] = False
        guard.check()
        r.role.pump()
        assert r.controller.indicators_visible is False
        r.now += 1
        guard.check()
        assert r.controller.state == "recording" and len(r.live_sources()) == 2
        r.now += 0.001
        guard.check()
        r.finalized_pause()
        assert not r.live_sources()
        shown[1] = True
        guard.check()
        r.recovered()
        assert len(r.live_sources()) == 2
    finally:
        guard.shutdown()
        r.close()


def test_manual_resume_without_visible_indicators_pauses_again(tmp_path: Path) -> None:
    from astra_voice.ui.meeting_indicator_guard import MeetingIndicatorGuard

    r = Integrated(tmp_path)
    shown = [True, False]
    guard = MeetingIndicatorGuard(
        active=lambda: r.controller.state in {"recording", "paused", "opening"},
        overlay_visible=lambda: shown[0],
        tray_visible=lambda: shown[1],
        set_visible=r.controller.set_indicators_visible,
        pause=lambda: r.controller.pause("indicators"),
        capturing=lambda: r.controller.capturing_without_indicator_pause,
        identity=lambda: r.controller.capture_id,
        clock=r.capture.clock,
    )
    try:
        r.start()
        guard.check()
        shown[0] = False
        guard.check()
        r.role.pump()
        r.now += 1.001
        guard.check()
        r.finalized_pause()
        assert r.controller.pause_reasons == {"indicators"}
        # "Продолжить" in the meetings window while both indicators stay hidden.
        assert r.controller.resume()
        r.until(lambda: r.controller.state == "recording")
        assert len(r.live_sources()) == 2
        guard.check()  # re-armed: a fresh >1 s grace starts now
        r.role.pump()  # deliver in-flight keepalives before the clock moves
        r.now += 1.0
        guard.check()
        r.role.pump()
        assert r.controller.state == "recording"
        r.now += 0.001
        guard.check()
        r.finalized_pause()
        assert r.controller.pause_reasons == {"indicators"}
        assert not r.live_sources()
    finally:
        guard.shutdown()
        r.close()


def test_replayed_lease_sequence_cannot_renew_or_attest_visibility(tmp_path: Path) -> None:
    r = CaptureRig(tmp_path)
    try:
        r.prepare()
        r.open()
        r.send(
            "keepalive", seq=5, indicators_visible=False, blocked_epoch=0, admission_mono_ms=100000
        )
        lease = r.state.last_lease
        r.clock.now += 3
        r.send(
            "keepalive",
            seq=5,
            indicators_visible=True,
            blocked_epoch=0,
            admission_mono_ms=int(r.clock() * 1000),
        )
        assert r.state.last_lease == lease
        assert r.state.indicators_visible is False
        r.clock.now += 1.05
        r.until("meeting.capture.closed")
        assert not any(s.opened for s in [*r.mics, *r.backend.sources])
    finally:
        r.cleanup()


def test_union_of_lease_and_indicators_is_the_only_automatic_pause_set(tmp_path: Path) -> None:
    r = CaptureRig(tmp_path)
    try:
        r.prepare()
        r.open()
        r.send("pause", reason="indicators")
        r.send("pause", reason="lease")
        r.until("meeting.capture.finalized")
        assert r.state.pause_reasons == {"lease", "indicators"}
        r.send(
            "keepalive",
            seq=1,
            indicators_visible=True,
            blocked_epoch=0,
            admission_mono_ms=int(r.clock() * 1000),
        )
        wait_until(lambda: (r.poll(), r.state.state == "recording")[1])
        assert not r.state.manual_resume_required
        assert len(r.mics) == 2
    finally:
        r.cleanup()


@pytest.mark.parametrize("age,connect,late", [(0.9, 0.15, 0.0), (0.9, 0.09, 0.02)])
def test_aged_keepalive_admission_does_not_shorten_automatic_open(
    tmp_path: Path, age: float, connect: float, late: float
) -> None:
    r = CaptureRig(tmp_path)
    try:
        r.prepare()
        r.open()
        r.send("pause", reason="indicators")
        r.until("meeting.capture.finalized")
        # The keepalive lands while finalization is in flight: no open yet.
        r.state.finalizing = True
        r.send(
            "keepalive",
            seq=1,
            indicators_visible=True,
            blocked_epoch=r.state.session.snapshot().blocked_epoch,
            admission_mono_ms=int(r.clock() * 1000),
        )
        assert not r.state.opening
        original = r.state.mic_factory

        def factory() -> Any:
            source: Any = original()

            def slow_open(device: Any = None) -> None:
                r.clock.now += connect  # native connect time
                source.opened = True

            source.open = slow_open
            return source

        r.state.mic_factory = factory
        r.clock.now += age
        r.state.finalizing = False
        r.poll()  # the capture tick reopens with a 0.9 s old GUI admission
        assert r.state.opening
        assert r.state.open_thread is not None
        wait_until(lambda: not r.state.open_thread.is_alive())
        r.clock.now += late
        for _ in range(5):
            r.poll()
        assert r.state.state == "recording"
        assert not r.state.manual_resume_required
        assert not r.state.session_terminal
    finally:
        r.cleanup()


@pytest.mark.parametrize("stage", ["stale-after-connect", "native-failure"])
def test_automatic_open_losing_admission_returns_to_automatic_pause(
    tmp_path: Path, stage: str
) -> None:
    r = CaptureRig(tmp_path)
    try:
        r.prepare()
        r.open()
        r.send("pause", reason="indicators")
        r.until("meeting.capture.finalized")
        original = r.state.mic_factory

        def factory() -> Any:
            source: Any = original()

            def slow_open(device: Any = None) -> None:
                # Connect overruns the 1 s attestation window.
                r.clock.now += 1.2
                if stage == "native-failure":
                    raise RuntimeError("native-connect")
                source.opened = True

            source.open = slow_open
            return source

        r.state.mic_factory = factory
        r.send(
            "keepalive",
            seq=1,
            indicators_visible=True,
            blocked_epoch=r.state.session.snapshot().blocked_epoch,
            admission_mono_ms=int(r.clock() * 1000),
        )
        assert r.state.opening
        assert r.state.open_thread is not None
        wait_until(lambda: not r.state.open_thread.is_alive())
        for _ in range(5):
            r.poll()
        assert r.state.state == "paused"
        assert not r.state.session_terminal
        assert not any(s.opened for s in [*r.mics, *r.backend.sources])
        if stage == "stale-after-connect":
            # A lapsed attestation is the same automatic pause, never audio_failed.
            assert r.state.pause_reasons == {"indicators"}
            assert not r.state.manual_resume_required
        else:
            # A real native failure keeps the existing manual audio_failed latch.
            assert r.state.pause_reasons == {"audio_failed"}
            assert r.state.manual_resume_required
    finally:
        r.cleanup()
