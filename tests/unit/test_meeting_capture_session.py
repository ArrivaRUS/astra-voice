"""D3/D9: capture owns session admission, independently of GUI and storage."""

from __future__ import annotations

import importlib
import itertools
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from test_meeting_capture import Rig, Source, wait_until

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("filesystem_euid")]


def session_module() -> Any:
    return importlib.import_module("astra_voice.worker.meeting_session")


def ready_session(clock: Any, kind: str = "KDE") -> Any:
    session = session_module().SessionAdmission(kind=kind, clock=clock)
    session.update(known=True, active=True, locked=False, sleeping=False)
    return session


@pytest.fixture
def rig(tmp_path: Path) -> Any:
    instance = Rig(tmp_path)
    yield instance
    instance.cleanup()


def admit(rig: Rig, action: str = "resume", **fields: Any) -> list[dict[str, Any]]:
    return rig.send(
        action,
        blocked_epoch=rig.state.session.snapshot().blocked_epoch,
        admission_mono_ms=int(rig.clock() * 1000),
        **fields,
    )


def block(rig: Rig, reason: str = "lock") -> None:
    rig.state.session.update(
        known=reason != "unknown", active=True, locked=reason == "lock", sleeping=False
    )


def unlock(rig: Rig) -> None:
    rig.state.session.update(known=True, active=True, locked=False, sleeping=False)


@pytest.mark.parametrize("reason", ["lock", "unknown"])
@pytest.mark.parametrize("gui_alive", [False, True])
def test_local_block_closes_without_gui_and_retains_manual_latch(
    rig: Rig, reason: str, gui_alive: bool
) -> None:
    rig.prepare()
    rig.open()
    start = rig.clock()
    block(rig, reason)
    if gui_alive:
        assert rig.send("pause", reason=reason)[0]["type"] != "error"
    closed = rig.until("meeting.capture.closed")
    assert rig.clock() - start <= 1
    assert not any(s.opened for s in [*rig.mics, *rig.backend.sources])
    assert closed["capture_id"] == rig.identity["capture_id"]
    rig.until("meeting.capture.finalized")
    unlock(rig)
    rig.poll()
    assert reason in rig.state.pause_reasons
    assert rig.state.manual_resume_required
    before = len(rig.mics)
    rig.send("keepalive", seq=1)
    rig.poll()
    assert len(rig.mics) == before
    assert admit(rig)[0]["type"] == "meeting.ack"
    rig.until("meeting.capture.started")
    assert not rig.state.pause_reasons
    assert not rig.state.manual_resume_required


def test_lock_closes_even_when_fsync_hangs(tmp_path: Path) -> None:
    entered, release = threading.Event(), threading.Event()

    def fsync(fd: int) -> None:
        entered.set()
        release.wait(5)

    rig = Rig(tmp_path, fsync=fsync)
    try:
        rig.prepare()
        rig.open()
        assert entered.wait(1)
        block(rig)
        rig.until("meeting.capture.closed")
        assert rig.state.finalizing
        assert not any(s.opened for s in [*rig.mics, *rig.backend.sources])
        unlock(rig)
        rig.poll()
        assert admit(rig)[0]["type"] == "error"
    finally:
        release.set()
        rig.cleanup()


def test_fresh_resume_with_stale_epoch_and_locked_resume_are_refused(rig: Rig) -> None:
    rig.prepare()
    rig.open()
    block(rig)
    rig.until("meeting.capture.finalized")
    assert admit(rig)[0]["type"] == "error"
    unlock(rig)
    rig.poll()
    reply = rig.send("resume", blocked_epoch=0, admission_mono_ms=int(rig.clock() * 1000))
    assert reply[0]["code"] == "admission-stale"
    assert len(rig.mics) == 1


@pytest.mark.parametrize("reason", ["lock", "unknown"])
@pytest.mark.parametrize("edge", ["before_factory", "after_factory", "before_far"])
def test_lock_between_open_command_and_each_stream_open(rig: Rig, edge: str, reason: str) -> None:
    rig.prepare()
    entered, release = threading.Event(), threading.Event()
    old_factory = rig.state.mic_factory

    def mic() -> Any:
        if edge == "after_factory":
            block(rig, reason)
        source = old_factory()
        if edge == "before_far":
            original = source.open

            def open_then_lock(device: str | None = None) -> None:
                original(device)
                block(rig, reason)

            source.open = open_then_lock  # type: ignore[method-assign]
        return source

    rig.state.mic_factory = mic
    if edge == "before_factory":

        def free() -> int:
            entered.set()
            release.wait(3)
            return 1000

        rig.state.free_mb = free
    try:
        assert admit(rig, "open")[0]["type"] == "meeting.ack"
        if edge == "before_factory":
            assert entered.wait(1)
            block(rig, reason)
            release.set()
        rig.until("meeting.capture.closed")
        assert not any(s.opened for s in [*rig.mics, *rig.backend.sources])
        if edge == "before_factory":
            assert not rig.mics and not rig.backend.sources
        assert not rig.backend.sources
        assert rig.state.manual_resume_required
    finally:
        release.set()


def test_track_resume_checks_session_after_factory(rig: Rig) -> None:
    rig.prepare()
    rig.open()
    rig.mics[0].device_change = object()
    rig.mics[0].chunks.append(None)
    rig.until("meeting.capture.track")
    wait_until(lambda: not rig.mics[0].opened)
    original = rig.state.mic_factory

    def factory() -> Any:
        source = original()
        block(rig)
        return source

    rig.state.mic_factory = factory
    assert admit(rig, "track.resume", track="mic", mic_device="test")[0]["type"] == "meeting.ack"
    rig.until("meeting.capture.closed")
    assert not rig.mics[-1].opened
    assert not rig.backend.sources[0].opened


@pytest.mark.parametrize("order", list(itertools.permutations(["lease", "indicators", "lock"])))
def test_pause_reason_union_never_auto_resumes_over_lock(rig: Rig, order: tuple[str, ...]) -> None:
    rig.prepare()
    rig.open()
    for reason in order:
        if reason == "lock":
            block(rig)
            rig.poll()
        else:
            assert rig.send("pause", reason=reason)[0]["type"] != "error"
        rig.poll()
    rig.until("meeting.capture.finalized")
    assert set(rig.state.pause_reasons) == set(order)
    unlock(rig)
    rig.poll()
    rig.send("keepalive", seq=1)
    rig.poll()
    assert len(rig.mics) == 1
    assert rig.state.manual_resume_required
    assert admit(rig)[0]["type"] == "meeting.ack"
    rig.until("meeting.capture.started")


def test_lease_keepalive_resumes_only_same_capture_without_other_reasons(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    shutdown = Source.shutdown

    def delayed_shutdown(source: Source) -> None:
        time.sleep(0.025)
        shutdown(source)

    monkeypatch.setattr(Source, "shutdown", delayed_shutdown)
    rig.prepare()
    rig.open()
    capture = rig.identity["capture_id"]
    last_lease = rig.state.last_lease
    # Let the control loop observe lease expiry before its absolute close
    # deadline; jumping directly to +5 incorrectly skips the whole budget.
    rig.clock.now += 4
    rig.poll()
    rig.until("meeting.capture.closed")
    assert rig.state.close_mono_ms <= (last_lease + 5) * 1000
    rig.until("meeting.capture.finalized")
    rig.clock.now += 1
    assert set(rig.state.pause_reasons) == {"lease"}
    assert not rig.state.manual_resume_required
    assert rig.send("keepalive", seq=1, capture_id="foreign")[0]["type"] == "error"
    assert len(rig.mics) == 1
    assert rig.send("keepalive", seq=2)[0]["type"] == "meeting.ack"
    wait_until(lambda: (rig.poll(), rig.state.state == "recording")[1])
    assert len(rig.mics) == 2
    assert rig.identity["capture_id"] == capture


@pytest.mark.parametrize("reason", ["user", "audio_failed", "disk", "mic_change"])
def test_keepalive_does_not_clear_nonautomatic_pause(rig: Rig, reason: str) -> None:
    rig.prepare()
    rig.open()
    rig.send("pause", reason=reason)
    rig.until("meeting.capture.finalized")
    rig.send("keepalive", seq=1)
    rig.poll()
    assert len(rig.mics) == 1
    assert reason in rig.state.pause_reasons


@pytest.mark.parametrize("stage", ["prepared", "recording", "paused"])
@pytest.mark.parametrize("reason", ["sleep", "inactive"])
def test_sleep_and_inactive_are_terminal(rig: Rig, stage: str, reason: str) -> None:
    rig.prepare()
    if stage != "prepared":
        rig.open()
    if stage == "paused":
        rig.send("pause", reason="user")
        rig.until("meeting.capture.finalized")
    rig.state.session.update(
        known=True, active=reason != "inactive", locked=False, sleeping=reason == "sleep"
    )
    rig.poll()
    if stage == "recording":
        rig.until("meeting.capture.closed")
        rig.until("meeting.capture.finalized")
    assert rig.state.state == "stopped"
    unlock(rig)
    rig.poll()
    assert admit(rig, "open" if stage == "prepared" else "resume")[0]["type"] == "error"


def test_fly_unknown_starts_and_advertises_missing_lock_signal(tmp_path: Path) -> None:
    session = session_module().SessionAdmission(kind="FLY", clock=lambda: 100.0)
    rig = Rig(tmp_path, session=session)
    try:
        rig.prepare()
        prepared = next(e for e in rig.events if e["type"] == "meeting.capture.prepared")
        assert prepared["lock_signal_unavailable"] is True
        rig.open()
        session.update(known=False, active=False, locked=True, sleeping=False)
        rig.poll()
        assert rig.state.state == "recording"
        assert not rig.state.manual_resume_required
        session.update(known=True, active=True, locked=True, sleeping=True)
        rig.until("meeting.capture.closed")
        assert rig.state.state == "stopped"
    finally:
        rig.cleanup()


def test_double_gui_and_local_lock_pause_is_noop(rig: Rig) -> None:
    rig.prepare()
    rig.open()
    stale = dict(rig.identity)
    rig.send("pause", reason="lock")
    block(rig)
    rig.poll()
    assert rig.send("pause", **stale, reason="lock")[0]["type"] != "error"
    rig.until("meeting.capture.finalized")
    assert len({e["state_rev"] for e in rig.events if e["type"] == "meeting.capture.closed"}) == 1
    assert set(rig.state.pause_reasons) == {"lock"}


def test_session_lock_request_survives_false_hint_and_stale_reply() -> None:
    session = ready_session(lambda: 100.0)
    generation = session.generation
    session.signal("Lock")
    epoch = session.snapshot().blocked_epoch
    session.update(known=True, active=True, locked=False, sleeping=False, generation=generation)
    assert not session.allowed(epoch)
    session.update(known=True, active=True, locked=False, sleeping=False)
    assert session.snapshot().locked
    session.signal("Unlock")
    session.update(known=True, active=True, locked=False, sleeping=False)
    assert session.allowed(epoch)


def test_session_snapshot_staleness_is_unknown_and_increments_epoch() -> None:
    now = [100.0]
    session = ready_session(lambda: now[0])
    assert session.allowed(0)
    now[0] += 1
    assert not session.allowed(0)
    assert session.snapshot().reason == "unknown"
    assert session.snapshot().blocked_epoch > 0


def test_login1_transport_polls_current_session_with_bounded_private_commands() -> None:
    from types import SimpleNamespace

    calls: list[tuple[list[str], dict[str, Any]]] = []
    outputs = [
        'o "/org/freedesktop/login1/session/c1"\n',
        "b false\n",
        'b false\nb true\n(uo) 1000 "/org/freedesktop/login1/user/_1000"\n'
        'b false\ns "x11"\ns "user"\n(so) "seat0" "/org/freedesktop/login1/seat/seat0"\n',
    ]

    def run(argv: list[str], **kwargs: Any) -> Any:
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout=outputs.pop(0))

    source = session_module().Login1Source(run=run, uid=1000)
    snapshot = source.read()
    assert snapshot == {"known": True, "active": True, "locked": False, "sleeping": False}
    assert len(calls) == 3
    assert all(c[0][0] == "/usr/bin/busctl" and "--system" in c[0] for c in calls)
    assert all(c[1]["timeout"] <= 0.08 and c[1].get("shell", False) is False for c in calls)
    assert "auto" in calls[0][0]


def test_login1_transport_wrong_uid_and_failure_are_unknown() -> None:
    from types import SimpleNamespace

    outputs = iter(
        [
            'o "/org/freedesktop/login1/session/c1"\n',
            "b false\n",
            'b false\nb true\n(uo) 999 "/org/freedesktop/login1/user/_999"\n'
            'b false\ns "x11"\ns "user"\n(so) "seat0" "/org/freedesktop/login1/seat/seat0"\n',
        ]
    )

    def run(argv: list[str], **kwargs: Any) -> Any:
        return SimpleNamespace(returncode=0, stdout=next(outputs))

    source = session_module().Login1Source(run=run, uid=1000)
    with pytest.raises(ValueError, match="session-unavailable"):
        source.read()


def test_login1_poll_does_not_overwrite_signal_received_during_read() -> None:
    state = ready_session(lambda: 100.0)

    class Source:
        def read(self) -> dict[str, bool]:
            state.signal("Lock")
            return {"known": True, "active": True, "locked": False, "sleeping": False}

    monitor = session_module().MeetingSession(state, source=Source())
    monitor.tick()
    assert state.snapshot().locked
    assert not state.allowed(state.snapshot().blocked_epoch)


@pytest.mark.parametrize("lease", [False, True])
def test_keepalive_is_not_evidence_of_indicator_recovery(rig: Rig, lease: bool) -> None:
    rig.prepare()
    rig.open()
    rig.send("keepalive", seq=1)
    rig.send("pause", reason="indicators")
    if lease:
        rig.send("pause", reason="lease")
    rig.until("meeting.capture.finalized")
    rig.send("keepalive", seq=2)
    for _ in range(5):
        rig.poll()
    assert rig.state.state == "paused"
    assert len(rig.mics) == 1
    assert "indicators" in rig.state.pause_reasons


def test_late_lock_during_finalization_persists_pause_set(tmp_path: Path) -> None:
    import json

    entered, release = threading.Event(), threading.Event()

    def fsync(fd: int) -> None:
        entered.set()
        release.wait(5)

    rig = Rig(tmp_path, fsync=fsync)
    try:
        rig.prepare()
        rig.open()
        assert entered.wait(1)
        rig.send("pause", reason="lease")
        rig.until("meeting.capture.closed")
        block(rig)
        rig.poll()
        assert rig.state.finalizing
        release.set()
        rig.until("meeting.capture.finalized")

        def persisted() -> bool:
            data = json.loads((rig.directory / "capture.json").read_text())
            return any(
                edge.get("pause_reasons") == ["lease", "lock"]
                and edge.get("manual_resume_required") is True
                and edge.get("blocked_epoch", 0) > 0
                for edge in data["boundaries"]
            )

        wait_until(persisted)
    finally:
        release.set()
        rig.cleanup()


def test_lease_close_budget_includes_physical_shutdown(rig: Rig) -> None:
    rig.prepare()
    rig.open()
    entered, release = threading.Event(), threading.Event()
    source = rig.mics[0]

    def shutdown() -> None:
        entered.set()
        release.wait(3)
        source.opened = False

    source.shutdown = shutdown  # type: ignore[method-assign]
    try:
        rig.clock.now += 4.05
        rig.poll()
        assert entered.wait(0.2), "lease did not reserve physical close time"
        assert rig.state.close_deadline is not None and rig.state.close_deadline <= 105.0
        rig.clock.now = 105.0
        release.set()
        rig.until("meeting.capture.closed")
        assert not source.opened
    finally:
        release.set()


def test_duplicate_pause_during_local_close_gets_final_correlated_outcome(rig: Rig) -> None:
    rig.prepare()
    rig.open()
    block(rig)
    rig.poll()
    replies = rig.send("pause", reason="lock")
    assert replies[0]["type"] == "meeting.ack"
    command = rig.commands[-1]
    rig.until("meeting.capture.finalized")
    replay = rig.state.handle(command)
    assert any(e["type"] == "meeting.capture.closed" for e in replay)
    assert any(e["type"] == "meeting.capture.finalized" for e in replay)
    assert any(e["type"] == "meeting.capture.paused" for e in replay)


@pytest.mark.parametrize("sleeping", [False, True])
def test_terminal_session_during_gui_pause_finalization_keeps_wire_valid(
    tmp_path: Path, sleeping: bool
) -> None:
    entered, release = threading.Event(), threading.Event()

    def fsync(fd: int) -> None:
        entered.set()
        release.wait(5)

    rig = Rig(tmp_path, fsync=fsync)
    try:
        rig.prepare()
        rig.open()
        assert entered.wait(1)
        rig.send("pause", reason="user")
        rig.until("meeting.capture.closed")
        rig.state.session.update(known=True, active=sleeping, locked=False, sleeping=sleeping)
        rig.poll()
        release.set()
        rig.until("meeting.capture.finalized")
        assert rig.state.state == "stopped"
    finally:
        release.set()
        rig.cleanup()


@pytest.mark.parametrize("foreign_unlock", [False, True])
def test_login1_lock_before_first_snapshot_cannot_be_lost(
    monkeypatch: pytest.MonkeyPatch, foreign_unlock: bool
) -> None:
    import io
    from types import SimpleNamespace

    module = session_module()
    state = module.SessionAdmission(kind="KDE", clock=lambda: 100.0)

    class Source:
        path = None

        def read(self) -> dict[str, bool]:
            return {"known": True, "active": True, "locked": False, "sleeping": False}

    process = SimpleNamespace(
        stdout=io.StringIO(
            "signal time=1 sender=:1.1 -> destination=(null destination) serial=1 "
            "path=/org/freedesktop/login1/session/c1; "
            "interface=org.freedesktop.login1.Session; member=Lock\n"
            + (
                "signal time=2 sender=:1.1 -> destination=(null destination) serial=2 "
                "path=/org/freedesktop/login1/session/c2; "
                "interface=org.freedesktop.login1.Session; member=Unlock\n"
                if foreign_unlock
                else ""
            )
        ),
        terminate=lambda: None,
        wait=lambda **kwargs: None,
    )
    monkeypatch.setattr(module.subprocess, "Popen", lambda *args, **kwargs: process)
    monitor = module.MeetingSession(state, source=Source())
    monitor._signals()
    monitor.tick()
    assert not state.allowed(state.snapshot().blocked_epoch)
    assert state.lock_requested


def test_lock_shortens_existing_close_without_second_shutdown(rig: Rig) -> None:
    rig.prepare()
    rig.open()
    entered, release = threading.Event(), threading.Event()
    source = rig.mics[0]
    calls = [0]

    def shutdown() -> None:
        calls[0] += 1
        entered.set()
        release.wait(3)
        source.opened = False

    source.shutdown = shutdown  # type: ignore[method-assign]
    try:
        rig.send("pause", reason="user")
        assert entered.wait(1)
        rig.clock.now += 0.05
        block(rig)
        rig.poll()
        assert rig.state.close_deadline is not None
        assert rig.state.close_deadline <= rig.clock() + 0.2
        release.set()
        rig.until("meeting.capture.closed")
        assert calls[0] == 1
    finally:
        release.set()


def test_duplicate_pause_gets_failed_finalization_on_disk_error(tmp_path: Path) -> None:
    entered, release = threading.Event(), threading.Event()

    def fsync(fd: int) -> None:
        entered.set()
        release.wait(5)
        raise OSError("fake-io")

    rig = Rig(tmp_path, fsync=fsync)
    try:
        rig.prepare()
        rig.open()
        assert entered.wait(1)
        block(rig)
        rig.until("meeting.capture.closed")
        rig.send("pause", reason="lock")
        command = rig.commands[-1]
        release.set()
        rig.until("meeting.capture.finalized")
        replay = rig.state.handle(command)
        finalized = [e for e in replay if e["type"] == "meeting.capture.finalized"]
        assert finalized and finalized[0]["status"] == "failed"
        assert any(e["type"] == "meeting.capture.paused" for e in replay)
        assert not rig.state.close_waiters
    finally:
        release.set()
        rig.cleanup()


def test_stalled_signal_startup_still_polls_fly_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    module = session_module()
    state = module.SessionAdmission(kind="FLY", clock=lambda: 100.0)
    called = threading.Event()

    class Source:
        def read(self) -> dict[str, bool]:
            called.set()
            return {"known": True, "active": True, "locked": False, "sleeping": True}

    monitor = module.MeetingSession(state, source=Source())
    monkeypatch.setattr(monitor, "_signals", lambda: monitor.stop.wait(2))
    try:
        monitor.start()
        assert called.wait(0.5), "signal startup prevented independent session polling"
        wait_until(lambda: state.snapshot().reason == "sleep")
    finally:
        monitor.close()


def test_track_resume_installs_guard_inside_native_open(rig: Rig) -> None:
    rig.prepare()
    rig.open()
    rig.mics[0].device_change = object()
    rig.mics[0].chunks.append(None)
    rig.until("meeting.capture.track")
    wait_until(lambda: not rig.mics[0].opened)
    attempted: list[bool] = []
    original = rig.state.mic_factory

    def factory() -> Any:
        source: Any = original()
        source.admission_check = lambda: True

        def open(device: str | None = None) -> None:
            # Model blocking native context setup before stream_new/connect.
            block(rig)
            attempted.append(source.admission_check())
            source.opened = attempted[-1]

        source.open = open
        return source

    rig.state.mic_factory = factory
    assert admit(rig, "track.resume", track="mic", mic_device="test")[0]["type"] == "meeting.ack"
    rig.until("meeting.capture.closed")
    assert attempted == [False]


def test_session_close_joins_inflight_poll_and_signal_reader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = session_module()
    entered, release, closed = threading.Event(), threading.Event(), threading.Event()

    class Source:
        def read(self) -> dict[str, bool]:
            entered.set()
            assert release.wait(2)
            return dict(known=True, active=True, locked=False, sleeping=False)

    monitor = module.MeetingSession(ready_session(time.monotonic), source=Source())
    signal_workers: list[threading.Thread] = []

    def signals() -> None:
        signal_workers.append(threading.current_thread())
        monitor.signals_ready.set()
        monitor.stop.wait(2)

    monkeypatch.setattr(monitor, "_signals", signals)
    outcome: list[bool] = []

    def close() -> None:
        outcome.append(monitor.close())
        closed.set()

    closer = threading.Thread(target=close)
    try:
        monitor.start()
        assert entered.wait(1)
        closer.start()
        assert not closed.wait(0.05), "close returned before its in-flight login1 read"
        release.set()
        closer.join(2)
        assert outcome == [True]
        assert not monitor.thread.is_alive()
        assert signal_workers and not signal_workers[0].is_alive()
        assert monitor.close() is True
    finally:
        release.set()
        monitor.close()
        if closer.ident is not None:
            closer.join(2)
        if monitor.thread is not None:
            monitor.thread.join(2)
        for thread in signal_workers:
            thread.join(2)


def test_session_close_reports_live_reader_and_can_be_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = session_module()
    entered, release = threading.Event(), threading.Event()

    class Source:
        def read(self) -> dict[str, bool]:
            entered.set()
            release.wait(2)
            return dict(known=True, active=True, locked=False, sleeping=False)

    monitor = module.MeetingSession(ready_session(time.monotonic), source=Source())
    monkeypatch.setattr(monitor, "_signals", monitor.signals_ready.set)
    try:
        monitor.start()
        assert entered.wait(1)
        started = time.monotonic()
        assert monitor.close() is False
        assert time.monotonic() - started < 1
    finally:
        release.set()
        assert monitor.close() is True
    monitor.start()
    assert not monitor.thread.is_alive()
