"""M8 step 2 metadata lifecycle, with no real desktop server or record API."""

from __future__ import annotations

import ctypes as C
import errno
import json
import logging
import shutil
import socket
import subprocess
import threading
import time
from collections.abc import Iterator
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock

import pytest
from test_meeting_m8_repr import repr_without_object_addresses

from astra_voice.meetings import apps
from astra_voice.meetings import events as meeting_events
from astra_voice.meetings.apps import load_apps
from astra_voice.meetings.detector_rule import CallEnded, Candidate, MeetingDetectorRule
from astra_voice.security.files import read_private
from astra_voice.worker import meeting_detector as d
from astra_voice.worker import meeting_metadata as m
from astra_voice.worker.monitor_stream import MonitorStreamSource
from astra_voice.worker.pulse_stream import PA_CONTEXT_FAILED, PA_CONTEXT_READY, _PulseAsync
from helpers.pulse_fakes import FakeLibrary

pytestmark = pytest.mark.unit
MARKER = "PRIVATE_MEDIA_HOME_USER_MAC_aabbccddeeff_LABEL"
PID = 123456789
CALL_PROGRAM = d.ProgramKey("dion", "dion")
CALL_MIC = (d.Direction.MICROPHONE, 8)


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def properties(**extra: object) -> dict[str, object]:
    return {"application.process.id": PID, "application.process.binary": "dion", **extra}


def stream(
    index: int = 7, client: int = 10, direction: d.Direction = d.Direction.OUTPUT
) -> d.RawStream:
    return d.RawStream(index, client, direction, {"object.serial": 77}, False, False)


class Backend:
    def __init__(self, own_index: int = 42) -> None:
        self.own_index = own_index
        self.clients = {10: d.RawClient(10, properties())}
        self.streams = {(d.Direction.OUTPUT, 7): stream()}
        self.notices: list[d.Notice] = []
        self.overflow = False
        self.closed = False
        self.fail = False
        self.calls: list[tuple[object, ...]] = []

    def snapshot(
        self, *, remaining: d.TimeRemaining | None = None
    ) -> tuple[tuple[d.RawClient, ...], tuple[d.RawStream, ...]]:
        self.calls.append(("snapshot",))
        return tuple(self.clients.values()), tuple(self.streams.values())

    def client(self, index: int, *, remaining: d.TimeRemaining | None = None) -> d.RawClient | None:
        self.calls.append(("client", index))
        return self.clients.get(index)

    def stream(
        self, direction: d.Direction, index: int, *, remaining: d.TimeRemaining | None = None
    ) -> d.RawStream | None:
        self.calls.append(("stream", direction, index))
        return self.streams.get((direction, index))

    def poll(self) -> tuple[tuple[d.Notice, ...], bool]:
        if self.fail:
            raise d.MetadataDisconnected()
        notices, self.notices = tuple(self.notices), []
        return notices, self.take_overflow()

    def take_overflow(self) -> bool:
        overflow, self.overflow = self.overflow, False
        return overflow

    def has_pending_notices(self) -> bool:
        return bool(self.notices)

    def close(self) -> None:
        self.closed = True
        self.notices.clear()

    def notify(
        self,
        facility: d.Facility = d.Facility.OUTPUT,
        change: d.Change = d.Change.CHANGE,
        index: int = 7,
    ) -> None:
        self.notices.append(d.Notice(facility, change, index))


def make(backend: d.MetadataBackend, clock: Clock) -> d.MeetingDetectorSource:
    return d.MeetingDetectorSource(factory=lambda: backend, clock=clock)


def upserts(events: tuple[d.NormalizedEvent, ...]) -> list[d.NormalizedEvent]:
    return [event for event in events if event.kind is d.EventKind.UPSERT]


def assert_revoked(events: tuple[d.NormalizedEvent, ...], source: d.MeetingDetectorSource) -> None:
    assert [event.kind for event in events] == [d.EventKind.RESET, d.EventKind.UNAVAILABLE]
    assert all(event.key is None and event.program is None for event in events)
    assert source.diagnostic_snapshot() == ()


@pytest.fixture
def call_source(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[Backend, Clock, d.MeetingDetectorSource, MeetingDetectorRule]]:
    dion = apps.AppDefinition("dion", "DION", ("dion",), (), apps.AppClass.DION)
    monkeypatch.setattr(d, "load_apps", lambda: (dion,))
    backend, clock = Backend(), Clock()
    backend.streams[CALL_MIC] = stream(CALL_MIC[1], direction=CALL_MIC[0])
    source = make(backend, clock)
    rule = MeetingDetectorRule(clock=clock)
    assert rule.update(source.start(), timing=source.picture_timing) == ()
    try:
        yield backend, clock, source, rule
    finally:
        source.stop()


def test_rule_applies_deferred_microphone_removal(
    call_source: tuple[Backend, Clock, d.MeetingDetectorSource, MeetingDetectorRule],
) -> None:
    backend, clock, source, rule = call_source
    backend.notify()
    clock.now = 3.5
    assert rule.update(source.poll(), timing=source.picture_timing) == ()
    del backend.streams[CALL_MIC]
    backend.notify(d.Facility.MICROPHONE, d.Change.REMOVE, CALL_MIC[1])
    for at in (3.8, 3.9):
        clock.now = at
        events = source.poll()
        assert events == ()
        assert rule.update(events, timing=source.picture_timing) == ()
        assert source.picture_timing == d.PictureTiming(at, 3.8, False)
    clock.now = 4.5
    (removed,) = source.poll()
    assert removed.kind is d.EventKind.REMOVE
    assert rule.update((removed,), timing=source.picture_timing) == ()
    clock.now = 6.0
    assert rule.update(source.poll(), timing=source.picture_timing) == ()
    assert rule.candidate(CALL_PROGRAM) is None


def test_rule_preserves_offer_when_deferred_return_crosses_off_deadline(
    call_source: tuple[Backend, Clock, d.MeetingDetectorSource, MeetingDetectorRule],
) -> None:
    backend, clock, source, rule = call_source
    clock.now = 6.0
    assert rule.update(source.poll(), timing=source.picture_timing) == (
        Candidate(CALL_PROGRAM, False),
    )
    assert rule.mark_offered(CALL_PROGRAM)
    clock.now = 6.0
    mic = backend.streams.pop(CALL_MIC)
    backend.notify(d.Facility.MICROPHONE, d.Change.REMOVE, CALL_MIC[1])
    assert rule.update(source.poll(), timing=source.picture_timing) == ()

    backend.notify()
    clock.now = 14.75
    assert rule.update(source.poll(), timing=source.picture_timing) == ()
    backend.streams[CALL_MIC] = mic
    backend.notify(d.Facility.MICROPHONE, d.Change.NEW, CALL_MIC[1])
    for at in (15.5, 15.749):
        clock.now = at
        events = source.poll()
        assert events == ()
        assert rule.update(events, timing=source.picture_timing) == ()
    clock.now = 16.5
    (returned,) = source.poll()
    assert returned.kind is d.EventKind.UPSERT
    assert source.picture_timing == d.PictureTiming(16.5, 15.5, True)
    assert rule.update((returned,), timing=source.picture_timing) == ()
    clock.now = 22.5
    assert rule.update(source.poll(), timing=source.picture_timing) == (
        Candidate(CALL_PROGRAM, True),
    )


def test_rule_keeps_offer_when_failed_lookup_crosses_off_deadline(
    call_source: tuple[Backend, Clock, d.MeetingDetectorSource, MeetingDetectorRule],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend, clock, source, rule = call_source
    clock.now = 6.0
    assert rule.update(source.poll(), timing=source.picture_timing) == (
        Candidate(CALL_PROGRAM, False),
    )
    assert rule.mark_offered(CALL_PROGRAM)
    clock.now = 6.0
    mic = backend.streams.pop(CALL_MIC)
    backend.notify(d.Facility.MICROPHONE, d.Change.REMOVE, CALL_MIC[1])
    assert rule.update(source.poll(), timing=source.picture_timing) == ()

    def missing(
        direction: d.Direction, index: int, *, remaining: d.TimeRemaining | None = None
    ) -> None:
        assert (direction, index) == CALL_MIC
        assert CALL_MIC not in backend.streams
        clock.now = 16.1

    monkeypatch.setattr(backend, "stream", missing)
    clock.now = 15.9
    backend.notify(d.Facility.MICROPHONE, d.Change.NEW, CALL_MIC[1])
    events = source.poll()
    assert_revoked(events, source)
    assert source.picture_timing == d.PictureTiming(16.1, 15.9, False)
    assert rule.update(events, timing=source.picture_timing) == ()

    clock.now = 17.2
    backend.streams[CALL_MIC] = mic
    assert rule.update(source.poll(), timing=source.picture_timing) == ()
    clock.now = 23.2
    assert rule.update(source.poll(), timing=source.picture_timing) == (
        Candidate(CALL_PROGRAM, True),
    )


def test_rule_times_empty_snapshot_from_completion(
    call_source: tuple[Backend, Clock, d.MeetingDetectorSource, MeetingDetectorRule],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend, clock, source, rule = call_source
    clock.now = 6.0
    assert rule.update(source.poll(), timing=source.picture_timing) == (
        Candidate(CALL_PROGRAM, False),
    )
    assert rule.mark_offered(CALL_PROGRAM)
    original_snapshot = backend.snapshot

    def slow_snapshot(
        *,
        remaining: d.TimeRemaining | None = None,
    ) -> tuple[tuple[d.RawClient, ...], tuple[d.RawStream, ...]]:
        clock.now = 6.8
        backend.streams.clear()
        return original_snapshot(remaining=remaining)

    monkeypatch.setattr(backend, "snapshot", slow_snapshot)
    clock.now = 6.0
    backend.notify(d.Facility.SERVER)
    events = source.poll()
    assert events == (d.NormalizedEvent(d.EventKind.RESET, 6.0),)
    assert source.picture_timing == d.PictureTiming(6.8, 6.0, True)
    assert rule.update(events, timing=source.picture_timing) == ()

    clock.now = 15.799
    assert rule.update(source.poll(), timing=source.picture_timing) == ()
    clock.now = 17.8
    assert rule.update(source.poll(), timing=source.picture_timing) == (CallEnded(CALL_PROGRAM),)


def test_rule_restarts_off_deadline_after_snapshot_confirms_absence(
    call_source: tuple[Backend, Clock, d.MeetingDetectorSource, MeetingDetectorRule],
) -> None:
    backend, clock, source, rule = call_source
    clock.now = 6.0
    assert rule.update(source.poll(), timing=source.picture_timing) == (
        Candidate(CALL_PROGRAM, False),
    )
    assert rule.mark_offered(CALL_PROGRAM)
    mic = backend.streams.pop(CALL_MIC)
    backend.notify(d.Facility.MICROPHONE, d.Change.REMOVE, CALL_MIC[1])
    assert rule.update(source.poll(), timing=source.picture_timing) == ()

    clock.now = 14.0
    backend.notify(d.Facility.SERVER)
    events = source.poll()
    assert [event.kind for event in events] == [d.EventKind.RESET, d.EventKind.UPSERT]
    assert source.picture_timing.confirmed
    assert rule.update(events, timing=source.picture_timing) == ()
    assert all(
        event.key is None or event.key.direction is not d.Direction.MICROPHONE for event in events
    )

    for at in (16.0, 17.0, 22.999):
        clock.now = at
        assert rule.update(source.poll(), timing=source.picture_timing) == ()
    clock.now = 25.0
    assert rule.update(source.poll(), timing=source.picture_timing) == (CallEnded(CALL_PROGRAM),)

    clock.now = 25.25
    backend.streams[CALL_MIC] = mic
    backend.notify(d.Facility.MICROPHONE, d.Change.NEW, CALL_MIC[1])
    assert rule.update(source.poll(), timing=source.picture_timing) == ()
    clock.now = 31.25
    assert rule.update(source.poll(), timing=source.picture_timing) == (
        Candidate(CALL_PROGRAM, False),
    )


def noisy_player(backend: Backend, monkeypatch: pytest.MonkeyPatch) -> None:
    player_index = 33
    backend.clients[20] = d.RawClient(20, properties(**{"application.process.binary": "player"}))
    backend.streams[(d.Direction.OUTPUT, player_index)] = stream(player_index, client=20)
    backend.notify(index=player_index, change=d.Change.NEW)
    original = backend.stream

    def notify_during_read(
        direction: d.Direction, index: int, *, remaining: d.TimeRemaining | None = None
    ) -> d.RawStream | None:
        row = original(direction, index, remaining=remaining)
        if direction is d.Direction.OUTPUT and index == player_index:
            backend.notify(index=index)
        return row

    monkeypatch.setattr(backend, "stream", notify_during_read)


def test_rule_candidate_is_available_through_unrelated_notices(
    call_source: tuple[Backend, Clock, d.MeetingDetectorSource, MeetingDetectorRule],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend, clock, source, rule = call_source
    noisy_player(backend, monkeypatch)
    decisions: list[Candidate | CallEnded] = []
    for quarter in range(16, 78):
        clock.now = quarter / 4
        events = source.poll()
        assert not source.picture_timing.confirmed
        update = rule.update(events, timing=source.picture_timing)
        if update:
            assert 4.0 <= clock.now <= 5.25
        decisions.extend(update)
        if decisions:
            assert rule.candidate(CALL_PROGRAM) == Candidate(CALL_PROGRAM, False)
    assert backend.calls.count(("stream", d.Direction.OUTPUT, 33)) >= 15
    assert decisions == [Candidate(CALL_PROGRAM, False)]
    assert rule.mark_offered(CALL_PROGRAM)
    assert rule.candidate(CALL_PROGRAM) == Candidate(CALL_PROGRAM, True)


def test_ready_candidate_uses_last_applied_picture_while_removal_is_pending(
    call_source: tuple[Backend, Clock, d.MeetingDetectorSource, MeetingDetectorRule],
) -> None:
    backend, clock, source, rule = call_source
    clock.now = 6.0
    assert rule.update(source.poll(), timing=source.picture_timing) == (
        Candidate(CALL_PROGRAM, False),
    )
    clock.now = 6.5
    backend.notify()
    assert rule.update(source.poll(), timing=source.picture_timing) == ()
    assert rule.candidate(CALL_PROGRAM) == Candidate(CALL_PROGRAM, False)

    clock.now = 6.75
    del backend.streams[CALL_MIC]
    backend.notify(d.Facility.MICROPHONE, d.Change.REMOVE, CALL_MIC[1])
    events = source.poll()
    assert events == () and not source.picture_timing.confirmed
    assert rule.update(events, timing=source.picture_timing) == ()
    candidate = rule.candidate(CALL_PROGRAM)
    marked = rule.mark_offered(CALL_PROGRAM)
    assert (candidate, marked) == (Candidate(CALL_PROGRAM, False), True)

    clock.now = 7.5
    (removed,) = source.poll()
    assert removed.kind is d.EventKind.REMOVE
    assert rule.update((removed,), timing=source.picture_timing) == ()
    assert rule.candidate(CALL_PROGRAM) is None


@pytest.mark.parametrize("returns", [False, True], ids=["still-absent", "microphone-returns"])
def test_rule_short_gap_and_second_removal_across_snapshot(
    call_source: tuple[Backend, Clock, d.MeetingDetectorSource, MeetingDetectorRule],
    returns: bool,
) -> None:
    backend, clock, source, rule = call_source
    clock.now = 6.0
    assert rule.update(source.poll(), timing=source.picture_timing) == (
        Candidate(CALL_PROGRAM, False),
    )
    assert rule.mark_offered(CALL_PROGRAM)
    mic = backend.streams.pop(CALL_MIC)
    backend.notify(d.Facility.MICROPHONE, d.Change.REMOVE, CALL_MIC[1])
    assert rule.update(source.poll(), timing=source.picture_timing) == ()

    clock.now = 13.05
    backend.streams[CALL_MIC] = mic
    backend.notify(d.Facility.MICROPHONE, d.Change.NEW, CALL_MIC[1])
    assert rule.update(source.poll(), timing=source.picture_timing) == ()
    clock.now = 14.5
    del backend.streams[CALL_MIC]
    backend.notify(d.Facility.MICROPHONE, d.Change.REMOVE, CALL_MIC[1])
    clock.now = 14.6
    backend.notify(d.Facility.SERVER)
    events = source.poll()
    assert events[0].kind is d.EventKind.RESET and source.picture_timing.confirmed
    assert rule.update(events, timing=source.picture_timing) == ()
    clock.now = 16.0
    assert rule.update(source.poll(), timing=source.picture_timing) == ()

    if returns:
        clock.now = 16.2
        backend.streams[CALL_MIC] = mic
        backend.notify(d.Facility.MICROPHONE, d.Change.NEW, CALL_MIC[1])
        assert rule.update(source.poll(), timing=source.picture_timing) == ()
        clock.now = 22.2
        assert rule.update(source.poll(), timing=source.picture_timing) == (
            Candidate(CALL_PROGRAM, True),
        )
        assert not rule.mark_offered(CALL_PROGRAM)
    else:
        clock.now = 23.599
        assert rule.update(source.poll(), timing=source.picture_timing) == ()
        clock.now = 25.6
        assert rule.update(source.poll(), timing=source.picture_timing) == (
            CallEnded(CALL_PROGRAM),
        )


def test_rule_restarts_after_eight_seconds_of_absence_and_unavailable(
    call_source: tuple[Backend, Clock, d.MeetingDetectorSource, MeetingDetectorRule],
) -> None:
    backend, clock, source, rule = call_source
    clock.now = 6.0
    assert rule.update(source.poll(), timing=source.picture_timing) == (
        Candidate(CALL_PROGRAM, False),
    )
    assert rule.mark_offered(CALL_PROGRAM)
    del backend.streams[CALL_MIC]
    backend.notify(d.Facility.MICROPHONE, d.Change.REMOVE, CALL_MIC[1])
    assert rule.update(source.poll(), timing=source.picture_timing) == ()
    clock.now = 14.0
    backend.overflow = True
    events = source.poll()
    assert_revoked(events, source)
    assert rule.update(events, timing=source.picture_timing) == ()
    clock.now = 14.2
    events = source.poll()
    assert events[0].kind is d.EventKind.RESET and source.picture_timing.confirmed
    assert rule.update(events, timing=source.picture_timing) == ()
    for at in (16.0, 23.199):
        clock.now = at
        assert rule.update(source.poll(), timing=source.picture_timing) == ()
    clock.now = 25.2
    assert rule.update(source.poll(), timing=source.picture_timing) == (CallEnded(CALL_PROGRAM),)


@pytest.mark.parametrize("returns", [False, True], ids=["still-absent", "microphone-returns"])
def test_rule_off_waits_for_confirmed_absence_under_unrelated_notices(
    call_source: tuple[Backend, Clock, d.MeetingDetectorSource, MeetingDetectorRule],
    monkeypatch: pytest.MonkeyPatch,
    returns: bool,
) -> None:
    backend, clock, source, rule = call_source
    clock.now = 6.0
    assert rule.update(source.poll(), timing=source.picture_timing) == (
        Candidate(CALL_PROGRAM, False),
    )
    assert rule.mark_offered(CALL_PROGRAM)
    mic = backend.streams.pop(CALL_MIC)
    backend.notify(d.Facility.MICROPHONE, d.Change.REMOVE, CALL_MIC[1])
    assert rule.update(source.poll(), timing=source.picture_timing) == ()

    with monkeypatch.context() as patch:
        noisy_player(backend, patch)
        for quarter in range(56, 80):
            clock.now = quarter / 4
            events = source.poll()
            assert not source.picture_timing.confirmed
            assert rule.update(events, timing=source.picture_timing) == ()

    if returns:
        backend.streams[CALL_MIC] = mic
        backend.notify(d.Facility.MICROPHONE, d.Change.NEW, CALL_MIC[1])
    clock.now = 20.0
    events = source.poll()
    assert source.picture_timing.confirmed
    if not returns:
        assert rule.update(events, timing=source.picture_timing) == (CallEnded(CALL_PROGRAM),)
        return

    assert rule.update(events, timing=source.picture_timing) == ()
    clock.now = 26.0
    assert rule.update(source.poll(), timing=source.picture_timing) == (
        Candidate(CALL_PROGRAM, True),
    )


def test_snapshot_changes_and_one_second_coalescing() -> None:
    backend, clock = Backend(), Clock()
    source = make(backend, clock)
    events = source.start()
    assert [event.kind for event in events] == [d.EventKind.RESET, d.EventKind.UPSERT]
    first = events[-1]
    assert first.program == d.ProgramKey("dion", "dion") and first.key is not None
    assert first.key.serial == 77 and first.key.direction is d.Direction.OUTPUT
    assert (first.corked, first.muted, first.running) == (False, False, None)
    for now in (0.1, 0.5, 0.999):
        clock.now = now
        backend.notify()
        backend.streams[(d.Direction.OUTPUT, 7)] = replace(stream(), muted=True)
        assert source.poll() == ()
    assert backend.calls == [("snapshot",)]
    clock.now = 1
    changed = source.poll()
    assert len(changed) == 1 and changed[0].muted is True and changed[0].at == 1
    assert changed[0].key == first.key
    assert backend.calls == [("snapshot",), ("stream", d.Direction.OUTPUT, 7)]
    backend.notify()
    assert source.poll() == ()
    clock.now = 2
    assert source.poll() == ()  # no normalized change, no heartbeat/free text
    assert len(backend.calls) == 3
    assert source.stop()[0].kind is d.EventKind.RESET and backend.closed
    assert source.stop() == () and source.poll() == ()


@pytest.mark.parametrize("serial", [None, 77, 78])
def test_remove_new_same_index_is_new_key_even_without_serial(serial: int | None) -> None:
    backend, clock = Backend(), Clock()
    props = {} if serial is None else {"object.serial": serial}
    backend.streams[(d.Direction.OUTPUT, 7)] = replace(stream(), properties=props)
    source = make(backend, clock)
    first = upserts(source.start())[0]
    backend.notify(change=d.Change.REMOVE)
    backend.notify(change=d.Change.NEW)
    clock.now = 1
    events = source.poll()
    assert [event.kind for event in events] == [d.EventKind.REMOVE, d.EventKind.UPSERT]
    assert events[0].key == first.key and events[1].key != first.key
    source.stop()


def test_microphone_and_output_index_spaces_do_not_collide() -> None:
    backend, clock = Backend(), Clock()
    backend.streams[(d.Direction.MICROPHONE, 7)] = stream(direction=d.Direction.MICROPHONE)
    source = make(backend, clock)
    events = upserts(source.start())
    assert len(events) == 2 and events[0].key != events[1].key
    source.stop()


@pytest.mark.parametrize("native_overflow", [False, True])
def test_overflow_forces_full_resync_not_silent_loss(native_overflow: bool) -> None:
    backend, clock = Backend(), Clock()
    source = make(backend, clock)
    old_key = upserts(source.start())[0].key
    if native_overflow:
        backend.overflow = True
    else:
        for index in range(d.MAX_PENDING):
            backend.notify(index=index)
        assert source.poll() == ()
        assert len(source.diagnostic_snapshot()) == 1
        backend.notify(index=d.MAX_PENDING)
    backend.streams.clear()  # removal can be hidden by the overflow
    events = source.poll()
    assert_revoked(events, source)
    assert source.diagnostic_snapshot() == ()
    backend.streams[(d.Direction.OUTPUT, 7)] = stream()
    backend.notify(change=d.Change.NEW)
    clock.now = 1
    assert upserts(source.poll())[0].key != old_key
    assert backend.calls.count(("snapshot",)) == 2
    source.stop()


def test_client_events_refresh_program_by_index() -> None:
    backend, clock = Backend(), Clock()
    source = make(backend, clock)
    first = upserts(source.start())[0]
    backend.clients[10] = d.RawClient(10, properties(**{"application.process.binary": "firefox"}))
    backend.notify(d.Facility.CLIENT, d.Change.CHANGE, 10)
    clock.now = 1
    event = upserts(source.poll())[0]
    assert event.key == first.key
    assert event.program == d.ProgramKey("browser", "firefox")
    assert backend.calls == [("snapshot",), ("client", 10)]
    source.stop()


@pytest.mark.parametrize("binary", ["dion", "firefox-esr", "unknown-player"])
def test_program_is_canonical_catalog_entry(binary: str, monkeypatch: pytest.MonkeyPatch) -> None:
    class Declaration(str):
        pass

    apps = load_apps()
    monkeypatch.setattr(d, "load_apps", lambda: apps)
    backend = Backend()
    backend.clients[10] = d.RawClient(
        10, properties(**{"application.process.binary": Declaration(binary)})
    )
    source = make(backend, Clock())
    event = upserts(source.start())[0]
    match = next(
        ((app.id, name) for app in apps for name in app.binary if name == binary),
        None,
    )
    if binary == "unknown-player":
        assert match is None and event.program is None
    else:
        assert event.program is not None and match is not None
        assert event.program.app_id is match[0] and event.program.binary is match[1]
        assert source.diagnostic_snapshot()[0]["program"] == {
            "app_id": match[0],
            "binary": match[1],
        }
    source.stop()


@pytest.mark.parametrize(
    "defect", ["missing-pid", "missing-binary", "invalid", "conflict", "absent"]
)
def test_incomplete_foreign_client_only_loses_its_own_program(defect: str) -> None:
    backend, clock = Backend(), Clock()
    props = properties()
    if defect == "missing-pid":
        del props["application.process.id"]
    elif defect == "missing-binary":
        del props["application.process.binary"]
    if defect != "absent":
        backend.clients[11] = d.RawClient(11, props, valid=defect != "invalid")
    foreign = stream(8, 11)
    if defect == "conflict":
        foreign = replace(foreign, properties={"application.process.binary": "firefox"})
    backend.streams[(foreign.direction, foreign.index)] = foreign
    source = make(backend, clock)
    initial = upserts(source.start())
    assert [event.program for event in initial] == [d.ProgramKey("dion", "dion"), None]
    backend.notify(change=d.Change.CHANGE, index=8)
    clock.now = 1
    assert source.poll() == ()
    assert len(source.diagnostic_snapshot()) == 2
    source.stop()


def test_client_remove_removes_its_stream() -> None:
    backend, clock = Backend(), Clock()
    source = make(backend, clock)
    first = upserts(source.start())[0]
    backend.notify(d.Facility.CLIENT, d.Change.REMOVE, 10)
    clock.now = 1
    removed = source.poll()
    assert (
        len(removed) == 1 and removed[0].kind is d.EventKind.REMOVE and removed[0].key == first.key
    )
    source.stop()


def test_reconnect_resets_generation_snapshot_and_own_index() -> None:
    first, second, clock = Backend(42), Backend(43), Clock()
    for backend in (first, second):
        backend.clients[42] = d.RawClient(42, properties(**{"application.process.id": 222}))
        backend.clients[43] = d.RawClient(43, properties(**{"application.process.id": 333}))
        backend.streams[(d.Direction.OUTPUT, 42)] = stream(42, 42)
        backend.streams[(d.Direction.OUTPUT, 43)] = stream(43, 43)
    backends = iter([first, second])
    generations: list[int] = []

    def own(generation: int) -> frozenset[int]:
        generations.append(generation)
        return frozenset()

    source = d.MeetingDetectorSource(factory=lambda: next(backends), clock=clock, own_contexts=own)
    events = upserts(source.start())
    assert {event.key.index for event in events if event.key} == {7, 43}
    old = next(event.key for event in events if event.key and event.key.index == 7)
    first.fail = True
    assert [event.kind for event in source.poll()] == [d.EventKind.RESET, d.EventKind.UNAVAILABLE]
    assert first.closed and source.diagnostic_snapshot() == ()
    assert source.poll() == ()  # bounded retry, no busy reconnect
    clock.now = 1
    events = upserts(source.poll())
    assert {event.key.index for event in events if event.key} == {7, 42}
    assert next(event.key for event in events if event.key and event.key.index == 7) != old
    assert set(generations) == {1, 2}
    source.stop()


@pytest.mark.parametrize("delay", [0.0, 1.1], ids=["in-time", "expired"])
def test_own_context_change_republishes_from_cache_in_same_poll(
    delay: float, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend, clock = Backend(), Clock()
    backend.clients[11] = d.RawClient(11, properties())
    backend.streams[(d.Direction.OUTPUT, 8)] = stream(8, 11)
    own = [frozenset({11})]
    source = d.MeetingDetectorSource(
        factory=lambda: backend, clock=clock, own_contexts=lambda generation: own[0]
    )
    events = upserts(source.start())
    assert [event.program for event in events] == [d.ProgramKey("dion", "dion")]
    own[0] = frozenset({10})
    poll = backend.poll

    def delayed_poll() -> tuple[tuple[d.Notice, ...], bool]:
        result = poll()
        clock.now += delay
        return result

    monkeypatch.setattr(backend, "poll", delayed_poll)
    clock.now = 0.2
    changed = source.poll()
    if delay:
        assert_revoked(changed, source)
    else:
        assert [event.kind for event in changed] == [d.EventKind.UPSERT, d.EventKind.REMOVE]
        assert changed[1].key == events[0].key
        assert {item["index"] for item in source.diagnostic_snapshot()} == {8}
    assert backend.calls == [("snapshot",)]
    source.stop()


@pytest.mark.parametrize("known_value", [MARKER, "dion\n", "dion\u202e", "x" * 10240])
def test_private_properties_never_escape_event_diagnostic_or_logs(
    known_value: str,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    caplog.set_level(logging.DEBUG)
    backend, clock = Backend(), Clock()
    backend.clients[10] = d.RawClient(
        10,
        properties(
            **{
                "application.process.exe": "/private/" + MARKER,
                "application.name": MARKER + "\n\u202e",
                "media.name": MARKER * 1000,
                "application.process.binary": known_value,
                "pipewire.sec.pid": PID + 1,
                "pipewire.sec.label": MARKER,
                "client.api": "pipewire",
            }
        ),
    )
    private_stream = replace(stream(), properties=backend.clients[10].properties)
    backend.streams[(private_stream.direction, private_stream.index)] = private_stream
    source = make(backend, clock)
    events = source.start()
    public = json.dumps([asdict(event) for event in events]) + json.dumps(
        source.diagnostic_snapshot()
    )
    payload = (
        public
        + repr_without_object_addresses(events)
        + repr_without_object_addresses(backend.clients[10])
        + repr_without_object_addresses(private_stream)
        + repr_without_object_addresses(source)
        + caplog.text
    )
    for private in (MARKER, str(PID), str(PID + 1), "application.", "media.name", "verdict"):
        assert private not in payload
    assert not any(char in public for char in ("\n", "\u202e"))
    assert capsys.readouterr() == ("", "")
    assert [event.program for event in upserts(events)] == [None]
    source.stop()


def test_server_event_full_resync_and_bounded_cache_failure() -> None:
    backend, clock = Backend(), Clock()
    source = make(backend, clock)
    source.start()
    backend.notify(d.Facility.SERVER, index=0)
    clock.now = 1
    assert source.poll()[0].kind is d.EventKind.RESET
    backend.clients = {
        index: d.RawClient(index, properties()) for index in range(d.MAX_ENTRIES + 1)
    }
    backend.overflow = True
    assert source.poll()[0].kind is d.EventKind.RESET
    clock.now = 2
    assert source.poll()[-1].kind is d.EventKind.UNAVAILABLE
    assert not source._clients and not source._streams
    source.stop()


class PulseFake(_PulseAsync):
    """Real thin _PulseAsync wrappers over fake C calls; all stream calls trap."""

    def __init__(self) -> None:
        self.fake_lib = FakeLibrary(self.call)
        self.lib = cast(Any, self.fake_lib)
        self.mainloop = None
        self.calls: list[str] = []
        self.callback: Any = None
        self.clients = [d.RawClient(10, properties())]
        self.streams = [stream(), stream(8, direction=d.Direction.MICROPHONE)]
        self.notices: list[tuple[int, int]] = []
        self.props: dict[int, dict[str, object]] = {}
        self.buffers: list[Any] = []
        self.operations: set[int] = set()
        self.operation = 100
        self.own_index = 42
        self.state = 4
        self.mask = 0
        self.hold = False
        self.property_reads: list[str] = []
        self._declare()

    def call(self, name: str, *args: Any) -> Any:
        self.calls.append(name)
        if name.startswith(("pa_stream_", "pa_simple_")):
            pytest.fail("record/audio API called by metadata lifecycle: " + name)
        if name == "pa_mainloop_new":
            return 2
        if name == "pa_mainloop_get_api":
            return 3
        if name == "pa_context_new":
            return 1
        if name == "pa_context_get_state":
            return self.state
        if name == "pa_context_get_index":
            return self.own_index
        if name == "pa_context_set_subscribe_callback":
            self.callback = args[1]
        elif name == "pa_context_subscribe":
            self.mask = args[1]
            if args[2] is not None:
                args[2](1, 1, None)
            return self.new_op()
        elif name == "pa_mainloop_dispatch":
            self.deliver_notices()
        elif name.startswith("pa_context_get_") and "_info" in name:
            return self.query(name, args)
        elif name == "pa_proplist_get":
            _, key, pointer, size = args
            key = key.decode()
            self.property_reads.append(key)
            props = self.props.get(args[0], {})
            if key not in props:
                return -1
            value = props[key]
            data = value if isinstance(value, bytes) else str(value).encode() + b"\0"
            buffer = C.create_string_buffer(data, len(data))
            self.buffers.append(buffer)
            C.cast(pointer, C.POINTER(C.c_void_p))[0] = C.addressof(buffer)
            C.cast(size, C.POINTER(C.c_size_t))[0] = len(data)
        elif name == "pa_operation_unref":
            self.operations.discard(args[0])
        return 0

    def deliver_notices(self) -> None:
        notices, self.notices = self.notices, []
        if self.callback:
            for event, index in notices:
                if self.mask & (1 << (event & 0x0F)):
                    self.callback(1, event, index, None)

    def new_op(self) -> int:
        self.operation += 1
        self.operations.add(self.operation)
        return self.operation

    def query(self, name: str, args: tuple[Any, ...]) -> int:
        op = self.new_op()
        if not self.hold:
            self.reply(name, args)
        return op

    def reply(self, name: str, args: tuple[Any, ...]) -> None:
        listed = name.endswith("_list")
        callback = args[1] if listed else args[2]
        rows: list[d.RawClient | d.RawStream]
        structure: Any
        if "client_info" in name:
            rows = list(self.clients)
            structure = m._ClientInfo
        else:
            direction = d.Direction.OUTPUT if "sink_input" in name else d.Direction.MICROPHONE
            rows = [row for row in self.streams if row.direction is direction]
            structure = m._SinkInput if direction is d.Direction.OUTPUT else m._SourceOutput
        if not listed:
            rows = [row for row in rows if row.index == args[1]]
        for row in rows:
            native = structure()
            native.index = row.index
            native.name = MARKER.encode()
            prop_id = len(self.props) + 1
            self.props[prop_id] = dict(row.properties)
            native.proplist = prop_id
            if isinstance(row, d.RawStream):
                native.client = row.client
                native.mute = int(bool(row.muted))
                native.corked = int(bool(row.corked))
            callback(1, C.pointer(native), 0, None)
        callback(1, None, 1, None)


def test_native_adapter_whole_lifecycle_snapshot_events_stop_no_record_calls() -> None:
    pulse, clock = PulseFake(), Clock()
    before = set(threading.enumerate())
    adapter = m.PulseMetadata(pulse=pulse)
    source = d.MeetingDetectorSource(factory=lambda: adapter, clock=clock)
    events = upserts(source.start())
    assert len(events) == 2 and pulse.mask == 0xAC
    assert pulse.calls.count("pa_mainloop_new") == 1
    assert pulse.calls.count("pa_context_get_sink_input_info_list") == 1
    assert pulse.calls.count("pa_context_get_source_output_info_list") == 1
    assert pulse.calls.count("pa_context_get_client_info_list") == 1
    pulse.notices.extend([(2 | 16, 7)] * 100)
    pulse.streams[0] = replace(stream(), corked=True)
    assert source.poll() == ()
    clock.now = 1
    assert upserts(source.poll())[0].corked is True
    assert pulse.calls.count("pa_context_get_sink_input_info") == 1
    assert pulse.calls.count("pa_context_get_sink_input_info_list") == 1
    assert set(pulse.property_reads) <= set(d.PROPERTY_KEYS)
    for second in range(2, 53):
        clock.now = float(second)
        assert source.poll() == ()  # 50+ virtual seconds, still no record API
    assert pulse.calls.count("pa_context_get_sink_input_info_list") == 1
    held = adapter._callback
    source.stop()
    held(1, 2 | 16, 7, None)  # late callback cannot repopulate a stopped source
    assert pulse.callback is None and pulse.mainloop is None
    assert adapter._callback is None and not adapter._callbacks and not adapter._notices
    assert not pulse.operations and set(threading.enumerate()) == before
    assert not any(name.startswith(("pa_stream_", "pa_simple_")) for name in pulse.calls)


def test_stopping_observer_leaves_active_capture_readable() -> None:
    capture_pulse = Mock()
    capture_pulse.default_monitor.return_value = "capture.monitor"
    capture_pulse.poll_events.return_value = []
    capture_pulse.read_chunk.return_value = b"pcm"
    capture = MonitorStreamSource(capture_pulse, "run")
    capture.open()
    try:
        pulse = PulseFake()
        source = make(m.PulseMetadata(pulse=pulse), Clock())
        assert upserts(source.start())
        source.stop()
        assert pulse.callback is None and pulse.mainloop is None
        assert capture.opened and capture.read_chunk() == b"pcm"
        capture_pulse.close_record.assert_not_called()
    finally:
        capture.close()


def test_native_queue_overflow_visible_and_memory_bounded() -> None:
    pulse = PulseFake()
    adapter = m.PulseMetadata(pulse=pulse)
    pulse.notices = [(2 | 16, index) for index in range(d.MAX_PENDING + 20)]
    notices, overflow = adapter.poll()
    assert overflow and len(notices) <= d.MAX_PENDING
    assert adapter.poll() == ((), False)
    adapter.close()


def test_native_query_timeout_cancels_before_callback_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pulse = PulseFake()
    adapter = m.PulseMetadata(pulse=pulse)
    pulse.hold = True
    monkeypatch.setattr(m, "OPERATION_SECONDS", 0.01)
    with pytest.raises(d.MetadataRequestTimeout):
        adapter.snapshot()
    assert not adapter._callbacks and not pulse.operations
    assert pulse.calls[-3:-1] == ["pa_operation_cancel", "pa_operation_unref"]
    adapter.close()


def test_native_property_limits_checked_before_copy_and_no_private_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pulse = PulseFake()
    pulse.clients = [
        d.RawClient(
            10,
            properties(
                **{
                    "application.process.binary": "x" * 10240,
                    "media.name": MARKER,
                    "application.name": MARKER,
                    "pipewire.sec.pid": PID,
                }
            ),
        )
    ]
    adapter = m.PulseMetadata(pulse=pulse)
    copy = C.string_at

    def checked_copy(pointer: Any, size: int) -> bytes:
        assert 0 < size <= d.MAX_PROPERTY_BYTES + 1
        return copy(pointer, size)

    monkeypatch.setattr(C, "string_at", checked_copy)
    clients, streams = adapter.snapshot()
    assert not clients[0].valid
    assert "application.process.binary" not in clients[0].properties
    assert set(clients[0].properties) == {"application.process.id"}
    assert set(pulse.property_reads) <= set(d.PROPERTY_KEYS)
    assert (
        "media.name" not in pulse.property_reads and "application.name" not in pulse.property_reads
    )
    adapter.close()


def test_private_pipewire_metadata_subscription_without_recording(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only disposable, hardware-free daemons and explicitly addressed clients."""
    if any(shutil.which(binary) is None for binary in ("pipewire", "pipewire-pulse")):
        pytest.skip("isolated PipeWire executables unavailable")
    guard = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        try:
            guard.bind(str(tmp_path / "bind-check"))
        except OSError as error:
            if error.errno in {errno.EPERM, errno.EACCES, errno.EAFNOSUPPORT}:
                pytest.skip("sandbox forbids private AF_UNIX server bind")
            raise
    finally:
        guard.close()
    from tools.meeting_stand import private_environment

    env = private_environment(tmp_path / "stand")
    configs = tmp_path / "configs"
    configs.mkdir()
    env["PIPEWIRE_CONFIG_DIR"] = str(configs)
    modules = ["protocol-native", "metadata", "spa-node-factory", "client-node", "access"]
    (configs / "pipewire.conf").write_text(
        "context.properties = { core.daemon = true core.name = pipewire-0 support.dbus = false }\n"
        "context.spa-libs = { support.* = support/libspa-support }\ncontext.modules = [\n"
        + "\n".join("{ name = libpipewire-module-" + name + " }" for name in modules)
        + "\n]\n"
    )
    (configs / "pipewire-pulse.conf").write_text(
        "context.properties = { support.dbus = false }\n"
        "context.spa-libs = { support.* = support/libspa-support }\ncontext.modules = [\n"
        + "\n".join(
            "{ name = libpipewire-module-" + name + " }"
            for name in ["protocol-native", "client-node", "metadata", "protocol-pulse"]
        )
        + '\n]\npulse.properties = { server.address = [ "unix:native" ] }\n'
    )
    children: list[subprocess.Popen[bytes]] = []
    adapters: list[m.PulseMetadata] = []
    record_calls: list[str] = []

    def forbidden(*args: object) -> None:
        record_calls.append("record")
        raise AssertionError("metadata attempted a record API")

    def adapter() -> m.PulseMetadata:
        pulse = _PulseAsync()
        for name in (
            "pa_stream_new",
            "pa_stream_new_with_proplist",
            "pa_stream_connect_record",
            "pa_stream_peek",
            "pa_stream_drop",
        ):
            monkeypatch.setattr(pulse.lib, name, forbidden)
        result = m.PulseMetadata(pulse=pulse, server=env["PULSE_SERVER"].encode())
        adapters.append(result)
        return result

    try:
        for binary in ("pipewire", "pipewire-pulse"):
            children.append(
                subprocess.Popen(
                    [f"/usr/bin/{binary}"],
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
            )
        deadline = time.monotonic() + 4
        socket_path = Path(env["PULSE_SERVER"].removeprefix("unix:"))
        while not socket_path.is_socket() and time.monotonic() < deadline:
            if any(child.poll() is not None for child in children):
                pytest.fail("private metadata server failed to start")
            time.sleep(0.01)
        assert socket_path.is_socket(), "private pulse socket not ready"
        first = adapter()
        clients, streams = first.snapshot()
        assert any(row.index == first.own_index for row in clients) and not streams
        other = adapter()
        assert other.own_index != first.own_index
        deadline = time.monotonic() + 3
        notices: tuple[d.Notice, ...] = ()
        while time.monotonic() < deadline:
            notices = first.poll()[0]
            if any(notice.facility is d.Facility.CLIENT for notice in notices):
                break
            time.sleep(0.01)
        assert any(notice.facility is d.Facility.CLIENT for notice in notices)
        assert not first.snapshot()[1]  # neither observer created source/sink streams
    finally:
        for item in reversed(adapters):
            item.close()
        for child in children:
            if child.poll() is None:
                child.terminate()
        for child in children:
            try:
                child.wait(timeout=2)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=2)
    assert not record_calls


def test_native_source_overflow_and_reconnect_do_not_create_record_streams() -> None:
    clock = Clock()
    first, second = PulseFake(), PulseFake()
    second.own_index = 43
    pulses = iter([first, second])
    adapters: list[m.PulseMetadata] = []

    def factory() -> m.PulseMetadata:
        adapter = m.PulseMetadata(pulse=next(pulses))
        adapters.append(adapter)
        return adapter

    source = d.MeetingDetectorSource(factory=factory, clock=clock)
    old = upserts(source.start())[0].key
    first.notices = [(2 | 16, index) for index in range(d.MAX_PENDING + 10)]
    first.streams.clear()
    assert_revoked(source.poll(), source)
    clock.now = 1
    assert source.poll()[0].kind is d.EventKind.RESET
    assert first.calls.count("pa_context_get_sink_input_info_list") == 2
    first.state = 5
    assert source.poll()[-1].kind is d.EventKind.UNAVAILABLE
    assert first.callback is None and first.mainloop is None
    clock.now = 2
    events = upserts(source.poll())
    assert events and events[0].key != old
    source.stop()
    assert second.callback is None and second.mainloop is None
    for pulse in (first, second):
        assert not pulse.operations
        assert not any(name.startswith(("pa_stream_", "pa_simple_")) for name in pulse.calls)
    assert all(adapter._callback is None and not adapter._callbacks for adapter in adapters)


def test_stream_disappeared_during_targeted_query_removes_previous_key() -> None:
    backend, clock = Backend(), Clock()
    source = make(backend, clock)
    key = upserts(source.start())[0].key
    backend.streams.clear()
    backend.notify()
    clock.now = 1
    events = source.poll()
    assert key is not None
    assert_revoked(events, source)
    source.stop()


def test_serial_change_and_client_replacement_invalidate_stream_key() -> None:
    backend, clock = Backend(), Clock()
    source = make(backend, clock)
    first = upserts(source.start())[0].key
    backend.streams[(d.Direction.OUTPUT, 7)] = replace(stream(), properties={"object.serial": 78})
    backend.notify()
    clock.now = 1
    second = upserts(source.poll())[0].key
    assert first != second
    backend.notify(d.Facility.CLIENT, d.Change.REMOVE, 10)
    backend.notify(d.Facility.CLIENT, d.Change.NEW, 10)
    clock.now = 2
    third = upserts(source.poll())[0].key
    assert third != second
    source.stop()


def test_backend_query_failure_invalidates_all_public_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend, clock = Backend(), Clock()
    source = make(backend, clock)
    source.start()

    def denied(
        direction: d.Direction, index: int, *, remaining: d.TimeRemaining | None = None
    ) -> d.RawStream | None:
        raise PermissionError(MARKER)

    monkeypatch.setattr(backend, "stream", denied)
    backend.notify()
    clock.now = 1
    assert [event.kind for event in source.poll()] == [d.EventKind.RESET, d.EventKind.UNAVAILABLE]
    assert not source.diagnostic_snapshot() and not backend.closed
    source.stop()


@pytest.mark.parametrize("mode", ["connect", "subscribe"])
def test_native_constructor_failure_releases_loop_context_and_callbacks(
    mode: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pulse = PulseFake()
    if mode == "connect":
        pulse.state = 5
    else:

        def reject(context: int, mask: int, callback: Any, unused: Any) -> int:
            callback(context, 0, None)
            return pulse.new_op()

        monkeypatch.setattr(pulse.lib.pa_context_subscribe, "callback", reject)
    with pytest.raises(d.MetadataUnavailable):
        m.PulseMetadata(pulse=pulse)
    assert pulse.callback is None and pulse.mainloop is None and not pulse.operations
    assert "pa_context_disconnect" in pulse.calls and "pa_context_unref" in pulse.calls
    assert "pa_mainloop_free" in pulse.calls


def test_source_constructor_and_stopped_poll_are_inert() -> None:
    calls: list[int] = []

    def unavailable() -> d.MetadataBackend:
        calls.append(1)
        raise OSError(MARKER)

    clock = Clock()
    source = d.MeetingDetectorSource(factory=unavailable, clock=clock)
    assert source.poll() == () and not calls
    assert source.start()[-1].kind is d.EventKind.UNAVAILABLE
    assert calls == [1] and source.poll() == ()
    clock.now = 1
    assert source.poll()[-1].kind is d.EventKind.UNAVAILABLE
    assert calls == [1, 1]
    source.stop()
    assert source.poll() == () and calls == [1, 1]


def test_coalescing_interval_starts_after_slow_introspection_finishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend, clock = Backend(), Clock()
    source = make(backend, clock)
    source.start()
    original = backend.stream

    def delayed(
        direction: d.Direction, index: int, *, remaining: d.TimeRemaining | None = None
    ) -> d.RawStream | None:
        clock.now += 0.5
        return original(direction, index, remaining=remaining)

    monkeypatch.setattr(backend, "stream", delayed)
    backend.streams[(d.Direction.OUTPUT, 7)] = replace(stream(), muted=True)
    backend.notify()
    clock.now = 1
    first = upserts(source.poll())[0]
    assert first.at == 1.5
    backend.streams[(d.Direction.OUTPUT, 7)] = stream()
    backend.notify()
    clock.now = 2
    assert source.poll() == ()
    clock.now = 2.5
    second = upserts(source.poll())[0]
    assert second.at - first.at >= 1
    source.stop()


def test_missing_client_lookup_is_deduplicated_per_batch() -> None:
    backend, clock = Backend(), Clock()
    source = make(backend, clock)
    source.start()
    backend.calls.clear()
    for index in range(8, 12):
        backend.streams[(d.Direction.OUTPUT, index)] = stream(index, 99)
        backend.notify(index=index)
    clock.now = 1
    events = source.poll()
    assert [event.program for event in upserts(events)] == [None] * 4
    assert len(source.diagnostic_snapshot()) == 5
    assert backend.calls.count(("client", 99)) == 1
    source.stop()


def test_slow_batch_budget_cannot_leave_partially_updated_public_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend, clock = Backend(), Clock()
    source = make(backend, clock)
    source.start()
    original = backend.stream

    def slow(
        direction: d.Direction, index: int, *, remaining: d.TimeRemaining | None = None
    ) -> d.RawStream | None:
        clock.now += d.BATCH_SECONDS
        return original(direction, index, remaining=remaining)

    monkeypatch.setattr(backend, "stream", slow)
    backend.streams[(d.Direction.OUTPUT, 7)] = replace(stream(), muted=True)
    backend.notify()
    clock.now = 1
    events = source.poll()
    assert events[-1].kind is d.EventKind.UNAVAILABLE and not backend.closed
    assert source.diagnostic_snapshot() == ()
    source.stop()


def test_repeated_overflow_invalidates_immediately_but_resyncs_at_most_once_per_second() -> None:
    backend, clock = Backend(), Clock()
    source = make(backend, clock)
    old = upserts(source.start())[0]
    for now in (0.1, 0.2, 0.5, 0.999):
        clock.now = now
        backend.overflow = True
        events = source.poll()
        assert not upserts(events) and source.diagnostic_snapshot() == ()
    assert backend.calls == [("snapshot",)]
    clock.now = 1
    fresh = upserts(source.poll())[0]
    assert fresh.at - old.at >= 1 and fresh.key != old.key
    assert backend.calls == [("snapshot",), ("snapshot",)]
    source.stop()


def test_server_internal_stream_without_client_does_not_disable_detection() -> None:
    backend, clock = Backend(), Clock()
    backend.streams[(d.Direction.MICROPHONE, 9)] = replace(
        stream(9, d.INVALID_INDEX, d.Direction.MICROPHONE),
        properties=properties(),
    )
    source = make(backend, clock)
    events = upserts(source.start())
    assert len(events) == 2
    dion = next(event for event in events if event.key and event.key.index == 7)
    internal = next(event for event in events if event.key and event.key.index == 9)
    assert dion.program == d.ProgramKey("dion", "dion")
    assert internal.program is None
    backend.notify(d.Facility.MICROPHONE, index=9)
    clock.now = 1
    source.poll()
    assert ("client", d.INVALID_INDEX) not in backend.calls
    source.stop()


def test_native_ready_packet_drain_is_bounded_without_introspection_or_record_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pulse = PulseFake()
    adapter = m.PulseMetadata(pulse=pulse)
    dispatched: list[int] = []

    def ready(loop: int) -> int:
        dispatched.append(1)
        return 1

    monkeypatch.setattr(pulse.lib.pa_mainloop_dispatch, "callback", ready)
    calls = len(pulse.calls)
    assert adapter.poll() == ((), False)
    assert 0 < len(dispatched) <= m.PUMP_ITERATIONS
    assert not any("_info" in name or name.startswith("pa_stream_") for name in pulse.calls[calls:])
    adapter.close()


def test_native_noentity_overrides_partial_by_index_row(monkeypatch: pytest.MonkeyPatch) -> None:
    pulse = PulseFake()
    adapter = m.PulseMetadata(pulse=pulse)
    original = pulse.query
    monkeypatch.setattr(pulse.lib.pa_context_errno, "callback", lambda context: 5)

    def missing_after_row(name: str, args: tuple[Any, ...]) -> int:
        context, index, callback, unused = args

        def receive(ctx: int, info: Any, eol: int, userdata: Any) -> None:
            callback(ctx, info, -1 if eol else 0, userdata)

        return original(name, (context, index, receive, unused))

    monkeypatch.setattr(pulse, "query", missing_after_row)
    assert adapter.stream(d.Direction.OUTPUT, 7) is None
    assert not adapter._callbacks and not pulse.operations
    adapter.close()


def test_native_snapshot_notices_are_processed_at_next_tick(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pulse, clock = PulseFake(), Clock()
    adapter = m.PulseMetadata(pulse=pulse)
    source = make(adapter, clock)
    original = pulse.query

    def raced(name: str, args: tuple[Any, ...]) -> int:
        op = original(name, args)
        if name == "pa_context_get_source_output_info_list":
            pulse.streams[0] = replace(stream(), muted=True)
            pulse.callback(1, 2 | 16, 7, None)
        return op

    monkeypatch.setattr(pulse, "query", raced)
    original_event = next(
        event for event in upserts(source.start()) if event.key and event.key.index == 7
    )
    assert original_event.muted is False
    assert source.picture_timing == d.PictureTiming(0.0, None, False)
    clock.now = 0.2
    assert source.poll() == ()
    assert source.picture_timing == d.PictureTiming(0.2, 0.0, False)
    clock.now = 1
    (changed,) = source.poll()
    assert source.picture_timing == d.PictureTiming(1.0, 0.0, True)
    assert changed.kind is d.EventKind.UPSERT and changed.key == original_event.key
    assert changed.muted is True
    assert pulse.calls.count("pa_context_get_sink_input_info") == 1
    assert pulse.calls.count("pa_context_get_sink_input_info_list") == 1
    source.stop()


def test_notices_during_point_query_leave_current_publication_intact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend, clock = Backend(), Clock()
    source = make(backend, clock)
    source.start()
    original = backend.stream

    def raced(
        direction: d.Direction, index: int, *, remaining: d.TimeRemaining | None = None
    ) -> d.RawStream | None:
        row = original(direction, index, remaining=remaining)
        backend.streams[(direction, index)] = stream()
        backend.notify()
        return row

    monkeypatch.setattr(backend, "stream", raced)
    backend.streams[(d.Direction.OUTPUT, 7)] = replace(stream(), muted=True)
    backend.notify()
    clock.now = 1
    (changed,) = source.poll()
    assert changed.kind is d.EventKind.UPSERT and changed.muted is True
    clock.now = 1.5
    assert source.poll() == ()
    clock.now = 2
    (changed,) = source.poll()
    assert changed.kind is d.EventKind.UPSERT and changed.muted is False
    assert backend.calls == [("snapshot",), ("stream", d.Direction.OUTPUT, 7)] + [
        ("stream", d.Direction.OUTPUT, 7)
    ]
    source.stop()


@pytest.mark.parametrize("total", [1024, 1025])
@pytest.mark.parametrize("mixed", [False, True], ids=["clients", "mixed"])
def test_same_inclusive_picture_limit_at_both_boundaries(total: int, mixed: bool) -> None:
    native, backend = PulseFake(), Backend()
    streams = [stream(), stream(8, direction=d.Direction.MICROPHONE)] if mixed else []
    clients = [
        d.RawClient(index, properties() if index == 10 or index == total - 1 else {})
        for index in range(total - len(streams))
    ]
    native.clients, native.streams = clients, streams
    backend.clients = {row.index: row for row in clients}
    backend.streams = {(row.direction, row.index): row for row in streams}
    adapter = m.PulseMetadata(pulse=native)
    if total == 1024:
        raw_clients, raw_streams = adapter.snapshot()
        assert len(raw_clients) + len(raw_streams) == total
    else:
        with pytest.raises(d.MetadataCapacity):
            adapter.snapshot()
        # The final client's properties (or the final stream's serial) were never copied.
        assert len(native.buffers) == len(properties()) + int(mixed)
    for candidate in (backend, adapter):
        source = make(candidate, Clock())
        events = source.start()
        if total == 1024:
            assert [event.kind for event in events] == [d.EventKind.RESET] + [
                d.EventKind.UPSERT
            ] * len(streams)
        else:
            assert_revoked(events, source)
        if candidate is adapter:
            assert native.callback is not None and native.mainloop is not None
        else:
            assert not backend.closed
        source.stop()


def test_cache_growth_above_1024_rebuilds_without_reconnect() -> None:
    backend, clock = Backend(), Clock()
    backend.clients.update({index: d.RawClient(index) for index in range(100, 1122)})
    source = make(backend, clock)
    old = upserts(source.start())[0]
    backend.streams[(d.Direction.OUTPUT, 8)] = stream(8)
    backend.notify(change=d.Change.NEW, index=8)
    clock.now = 1
    assert_revoked(source.poll(), source)
    assert not backend.closed
    del backend.clients[100]
    clock.now = 1.999
    assert source.poll() == ()
    clock.now = 2
    events = upserts(source.poll())
    assert len(events) == 2 and events[0].key != old.key and not backend.closed
    source.stop()


def test_capacity_backoff_doubles_to_thirty_and_resets_after_recovery() -> None:
    backend, clock = Backend(), Clock()
    occupied = {index: d.RawClient(index) for index in range(d.MAX_ENTRIES)}
    backend.clients = occupied.copy()
    source = make(backend, clock)
    assert_revoked(source.start(), source)
    when = 0.0
    for calls, delay in enumerate((1, 2, 4, 8, 16, 30, 30), start=1):
        clock.now = when + delay - 0.001
        assert source.poll() == () and backend.calls.count(("snapshot",)) == calls
        when += delay
        clock.now = when
        assert_revoked(source.poll(), source)
        assert not backend.closed
    when += 30
    clock.now = when
    del backend.clients[0]  # 1023 clients + 1 stream.
    assert upserts(source.poll())
    backend.clients = occupied
    backend.notify(d.Facility.SERVER, index=0)
    clock.now = when + 1
    assert_revoked(source.poll(), source)
    clock.now = when + 2
    assert_revoked(source.poll(), source)
    assert not backend.closed
    source.stop()


def test_native_point_request_timeout_revokes_without_reconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pulse, clock = PulseFake(), Clock()
    adapter = m.PulseMetadata(pulse=pulse)
    source = make(adapter, clock)
    assert upserts(source.start())
    pulse.hold = True
    pulse.notices.append((5 | 16, 10))
    monkeypatch.setattr(m, "OPERATION_SECONDS", 0.01)
    clock.now = 1
    assert_revoked(source.poll(), source)
    assert pulse.callback is not None and pulse.mainloop is not None
    assert not pulse.operations and not adapter._callbacks
    pulse.hold = False
    clock.now = 2
    assert upserts(source.poll())
    assert pulse.calls.count("pa_context_new") == 1
    source.stop()


@pytest.mark.parametrize(
    "name,event,index",
    [
        ("pa_context_get_client_info_list", 7 | 16, d.INVALID_INDEX),
        ("pa_context_get_sink_input_info_list", 7 | 16, d.INVALID_INDEX),
        ("pa_context_get_source_output_info_list", 7 | 16, d.INVALID_INDEX),
        ("pa_context_get_client_info", 5 | 16, 10),
        ("pa_context_get_sink_input_info", 2 | 16, 7),
        ("pa_context_get_source_output_info", 3 | 16, 8),
    ],
)
def test_native_disconnect_during_request_closes_in_same_call(
    name: str,
    event: int,
    index: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first, second, clock = PulseFake(), PulseFake(), Clock()
    pulses = iter((first, second))
    source = d.MeetingDetectorSource(
        factory=lambda: m.PulseMetadata(pulse=next(pulses)), clock=clock
    )
    old = upserts(source.start())
    original = first.query

    def disconnected(request: str, args: tuple[Any, ...]) -> int:
        op = original(request, args)
        if request == name:
            # The last reply arrived in the same dispatch as the loss.
            first.state = PA_CONTEXT_FAILED
        return op

    monkeypatch.setattr(first, "query", disconnected)
    first.notices.append((event, index))
    clock.now = 1
    assert_revoked(source.poll(), source)
    assert first.callback is None and first.mainloop is None and not first.operations
    assert second.callback is None
    assert source.poll() == ()
    clock.now = 2
    fresh = upserts(source.poll())
    assert len(fresh) == len(old) == 2
    assert {item.key for item in fresh}.isdisjoint(item.key for item in old)
    source.stop()
    assert second.callback is None and second.mainloop is None


@pytest.mark.parametrize("disconnect", [False, True], ids=["request-only", "disconnected"])
def test_native_failed_request_creation_checks_connection(
    disconnect: bool,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    pulse, clock = PulseFake(), Clock()
    adapter = m.PulseMetadata(pulse=pulse)
    source = make(adapter, clock)
    assert upserts(source.start())

    def failed(*args: Any) -> int:
        if disconnect:
            pulse.state = PA_CONTEXT_FAILED
        raise OSError(MARKER)

    monkeypatch.setattr(pulse.lib.pa_context_get_sink_input_info, "callback", failed)
    pulse.notices.append((2 | 16, 7))
    clock.now = 1
    assert_revoked(source.poll(), source)
    assert (pulse.callback is None) is disconnect
    assert (pulse.mainloop is None) is disconnect
    assert MARKER not in caplog.text
    if not disconnect:
        clock.now = 2
        assert upserts(source.poll())
        assert pulse.calls.count("pa_context_new") == 1
    source.stop()


def test_native_loss_during_wait_closes_context_and_cancels_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pulse, clock = PulseFake(), Clock()
    source = make(m.PulseMetadata(pulse=pulse), clock)
    assert upserts(source.start())
    original = pulse.fake_lib.pa_mainloop_dispatch.callback

    def disconnect_on_dispatch(loop: int) -> int:
        if pulse.operations:
            pulse.state = PA_CONTEXT_FAILED
        return cast(int, original(loop))

    monkeypatch.setattr(pulse.lib.pa_mainloop_dispatch, "callback", disconnect_on_dispatch)
    pulse.hold = True
    pulse.notices.append((2 | 16, 7))
    clock.now = 1
    assert_revoked(source.poll(), source)
    assert pulse.callback is None and pulse.mainloop is None and not pulse.operations


CLOSE_STEPS = (
    "pa_context_set_subscribe_callback",
    "pa_context_disconnect",
    "pa_context_unref",
    "pa_mainloop_free",
)


@pytest.mark.parametrize("fault", CLOSE_STEPS)
def test_native_close_retries_only_unfinished_stages(
    fault: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pulse = PulseFake()
    adapter = m.PulseMetadata(pulse=pulse)
    original_callback = pulse.callback
    calls: list[str] = []

    def wrap(name: str) -> Any:
        original = getattr(pulse.lib, name).callback

        def run(*args: Any) -> Any:
            already_attempted = name in calls
            calls.append(name)
            if name == fault and not already_attempted:
                raise OSError(MARKER)
            return original(*args)

        return run

    for name in CLOSE_STEPS:
        monkeypatch.setattr(getattr(pulse.lib, name), "callback", wrap(name))
    with pytest.raises(OSError, match=MARKER):
        adapter.close()
    stage = CLOSE_STEPS.index(fault)
    assert calls == list(CLOSE_STEPS[: stage + 1])
    assert pulse.callback is (original_callback if stage == 0 else None)
    assert pulse.mainloop is not None
    with pytest.raises(d.MetadataDisconnected):
        adapter.poll()
    adapter.close()
    assert calls == [*CLOSE_STEPS[:stage], fault, *CLOSE_STEPS[stage:]]
    assert pulse.callback is None and pulse.mainloop is None and not pulse.operations
    adapter.close()
    assert calls == [*CLOSE_STEPS[:stage], fault, *CLOSE_STEPS[stage:]]


@pytest.mark.parametrize("stage", ["disconnect", "opening"])
def test_source_retries_detach_before_creating_another_subscription(
    stage: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first, second, clock = PulseFake(), PulseFake(), Clock()
    opened: list[PulseFake] = []
    blocked = True
    detach_attempts = 0
    original = first.fake_lib.pa_context_set_subscribe_callback.callback

    def set_callback(context: int, callback: Any, userdata: Any) -> None:
        nonlocal detach_attempts
        if callback is None:
            detach_attempts += 1
            if blocked:
                raise OSError(MARKER)
        original(context, callback, userdata)

    monkeypatch.setattr(first.lib.pa_context_set_subscribe_callback, "callback", set_callback)
    if stage == "opening":
        subscribe = first.fake_lib.pa_context_subscribe.callback

        def reject(*args: Any) -> int:
            op = subscribe(*args)
            args[2](1, 0, None)
            return cast(int, op)

        monkeypatch.setattr(first.lib.pa_context_subscribe, "callback", reject)

    def factory() -> m.PulseMetadata:
        pulse = first if not opened else second
        opened.append(pulse)
        if pulse is second:
            assert first.callback is None and first.mainloop is None
        return m.PulseMetadata(pulse=pulse)

    source = d.MeetingDetectorSource(factory=factory, clock=clock)
    if stage == "opening":
        assert_revoked(source.start(), source)
    else:
        assert upserts(source.start())
        first.state = PA_CONTEXT_FAILED
        assert_revoked(source.poll(), source)
    assert detach_attempts == 1 and first.callback is not None
    assert opened == [first] and second.callback is None
    clock.now = 1
    assert_revoked(source.poll(), source)
    assert detach_attempts == 2 and first.callback is not None
    assert opened == [first] and second.callback is None
    blocked = False
    clock.now = 2
    assert upserts(source.poll())
    assert detach_attempts == 3 and first.callback is None and first.mainloop is None
    assert opened == [first, second] and second.callback is not None
    source.stop()


def test_stop_retries_partial_close_even_when_already_stopped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pulse, clock = PulseFake(), Clock()
    source = make(m.PulseMetadata(pulse=pulse), clock)
    assert upserts(source.start())
    with monkeypatch.context() as patch:

        def fail(*args: Any) -> None:
            raise OSError(MARKER)

        patch.setattr(pulse.lib.pa_mainloop_free, "callback", fail)
        assert [event.kind for event in source.stop()] == [d.EventKind.RESET]
        assert pulse.callback is None and pulse.mainloop is not None
        assert source.diagnostic_snapshot() == () and source.poll() == ()
    assert source.stop() == () and pulse.mainloop is None
    assert source.stop() == ()


def test_overflow_during_last_snapshot_request_revokes_before_publication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pulse, clock = PulseFake(), Clock()
    adapter = m.PulseMetadata(pulse=pulse)
    source = make(adapter, clock)
    original = pulse.query
    lost = False

    def raced(name: str, args: tuple[Any, ...]) -> int:
        nonlocal lost
        op = original(name, args)
        if name == "pa_context_get_source_output_info_list" and not lost:
            lost = True
            pulse.streams.clear()
            pulse.notices.extend((2 | 32, index) for index in range(257))
            pulse.deliver_notices()
        return op

    monkeypatch.setattr(pulse, "query", raced)
    try:
        assert_revoked(source.start(), source)
        assert lost and "pa_mainloop_dispatch" not in pulse.calls
        assert pulse.callback is not None
        clock.now = 0.1
        assert source.poll() == ()
        clock.now = 1
        assert [event.kind for event in source.poll()] == [d.EventKind.RESET]
        assert not source.diagnostic_snapshot()
    finally:
        source.stop()


@pytest.mark.parametrize("reply_at", [0.699, 0.711], ids=["within-limit", "after-limit"])
def test_last_dispatch_reply_respects_request_deadline(
    reply_at: float,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pulse, clock = PulseFake(), Clock()
    monkeypatch.setattr(m, "time", SimpleNamespace(monotonic=clock))
    source = make(m.PulseMetadata(pulse=pulse), clock)
    original = pulse.query
    pending: list[tuple[str, tuple[Any, ...]]] = []
    dispatch_starts: list[float] = []

    def queue(name: str, args: tuple[Any, ...]) -> int:
        if name != "pa_context_get_client_info_list":
            return original(name, args)
        pending.append((name, args))
        return pulse.new_op()

    def dispatch(loop: int) -> int:
        dispatch_starts.append(clock.now)
        if clock.now < 0.690:
            clock.now = 0.690
            return 0
        clock.now = reply_at
        name, args = pending.pop()
        pulse.reply(name, args)
        return 1

    monkeypatch.setattr(pulse, "query", queue)
    monkeypatch.setattr(pulse.fake_lib.pa_mainloop_dispatch, "callback", dispatch)
    try:
        events = source.start()
        assert dispatch_starts == [0, 0.690]
        if reply_at > 0.7:
            assert_revoked(events, source)
            assert "pa_context_get_sink_input_info_list" not in pulse.calls
        else:
            assert len(upserts(events)) == 2
        assert pulse.callback is not None and not pulse.operations
    finally:
        source.stop()


@pytest.mark.parametrize("mode", ["snapshot", "by-index", "two-clients"])
def test_queries_share_one_batch_budget(
    mode: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pulse, clock = PulseFake(), Clock()
    monkeypatch.setattr(m, "time", SimpleNamespace(monotonic=clock))
    source = make(m.PulseMetadata(pulse=pulse), clock)
    if mode != "snapshot":
        assert len(upserts(source.start())) == 2
        clock.now = 1.0
    if mode == "by-index":
        pulse.notices.extend(((5 | 16, 10), (2 | 16, 7), (3 | 16, 8)))
    elif mode == "two-clients":
        pulse.clients.append(d.RawClient(11, properties()))
        pulse.notices.extend(((5 | 16, 10), (5 | 16, 11)))

    batch_started = clock.now
    first_delay = 0.55123 if mode == "two-clients" else 0.55
    requests: list[tuple[str, float]] = []
    pending: list[tuple[str, tuple[Any, ...], float]] = []
    wait_us = 0

    def queue(name: str, args: tuple[Any, ...]) -> int:
        delay = 0.55 if requests else first_delay
        requests.append((name, clock.now))
        pending.append((name, args, round(clock.now + delay, 9)))
        return pulse.new_op()

    def prepare(loop: int, timeout_us: int) -> int:
        nonlocal wait_us
        wait_us = timeout_us
        return 0

    def dispatch(loop: int) -> int:
        if not pending:
            pulse.deliver_notices()
            return 0
        name, args, ready_at = pending[0]
        clock.now = min(ready_at, round(clock.now + wait_us / 1_000_000, 9))
        if clock.now < ready_at:
            return 0
        pending.pop()
        pulse.reply(name, args)
        return 1

    monkeypatch.setattr(pulse, "query", queue)
    monkeypatch.setattr(pulse.fake_lib.pa_mainloop_prepare, "callback", prepare)
    monkeypatch.setattr(pulse.fake_lib.pa_mainloop_dispatch, "callback", dispatch)
    try:
        events = source.start() if mode == "snapshot" else source.poll()
        assert_revoked(events, source)
        suffix = "_list" if mode == "snapshot" else ""
        second = (
            "pa_context_get_client_info"
            if mode == "two-clients"
            else "pa_context_get_sink_input_info"
        )
        assert [name for name, _ in requests] == [
            "pa_context_get_client_info" + suffix,
            second + suffix,
        ]
        assert [at for _, at in requests] == pytest.approx(
            [batch_started, batch_started + first_delay]
        )
        if mode == "two-clients":
            assert wait_us == pytest.approx(3770, abs=1)
        assert clock.now == pytest.approx(batch_started + 1.0, abs=2e-6)
        assert pulse.callback is not None and not pulse.operations
    finally:
        source.stop()


def test_expired_batch_does_not_launch_native_query() -> None:
    pulse = PulseFake()
    adapter = m.PulseMetadata(pulse=pulse)
    try:
        with pytest.raises(d.MetadataRequestTimeout):
            adapter.snapshot(remaining=lambda: 0.0)
        assert "pa_context_get_client_info_list" not in pulse.calls
        assert not pulse.operations
    finally:
        adapter.close()


@pytest.mark.parametrize(
    "elapsed, timed_out",
    [
        pytest.param(0.0, False, id="immediate"),
        pytest.param(0.4, False, id="slow-success"),
        pytest.param(0.7, True, id="timeout"),
    ],
)
def test_fast_restart_defers_next_full_snapshot(
    elapsed: float, timed_out: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second, clock = Backend(), Backend(), Clock()
    opened: list[tuple[float, Backend]] = []
    snapshot = first.snapshot

    def first_snapshot(
        *, remaining: d.TimeRemaining | None = None
    ) -> tuple[tuple[d.RawClient, ...], tuple[d.RawStream, ...]]:
        rows = snapshot(remaining=remaining)
        clock.now += elapsed
        if timed_out:
            raise d.MetadataRequestTimeout()
        return rows

    def factory() -> Backend:
        backend = first if not opened else second
        opened.append((clock.now, backend))
        return backend

    monkeypatch.setattr(first, "snapshot", first_snapshot)
    source = d.MeetingDetectorSource(factory=factory, clock=clock)
    initial = source.start()
    if timed_out:
        assert_revoked(initial, source)
        old = None
    else:
        old = upserts(initial)[0].key
    assert first.calls == [("snapshot",)]
    assert source.stop()[0].kind is d.EventKind.RESET
    clock.now = elapsed + 0.1
    assert source.start() == () and first.closed
    assert opened == [(0, first)] and not second.calls
    clock.now = elapsed + 0.999
    assert source.poll() == () and opened == [(0, first)]
    clock.now = elapsed + 1
    fresh = upserts(source.poll())[0]
    assert fresh.key != old and fresh.at == elapsed + 1
    assert second.calls == [("snapshot",)] and opened == [(0, first), (elapsed + 1, second)]
    source.stop()


@pytest.mark.parametrize("problem", ["malformed", "io", "missing"])
def test_catalog_read_failure_recovers_with_full_snapshot(
    problem: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    backend, clock = Backend(), Clock()
    source = make(backend, clock)
    reads: list[float] = []

    def read(path: Path, limit: int) -> bytes:
        reads.append(clock.now)
        if clock.now >= 2:
            return read_private(path, limit)
        if problem == "io":
            raise PermissionError(str(path) + MARKER)
        if problem == "missing":
            raise FileNotFoundError(str(path) + MARKER)
        return b'{broken-json "' + MARKER.encode() + b'"'

    caplog.set_level(logging.DEBUG, logger=d.__name__)
    monkeypatch.setattr(apps, "read_private", read)
    try:
        initial = upserts(source.start())
        assert len(initial) == 1 and initial[0].program is None
        assert source.diagnostic_snapshot()[0]["program"] is None
        for now in (0.1, 0.999, 1, 1.9):
            clock.now = now
            assert source.poll() == ()
        assert reads == [0, 1]
        snapshot = backend.snapshot

        def fresh_snapshot(
            *,
            remaining: d.TimeRemaining | None = None,
        ) -> tuple[tuple[d.RawClient, ...], tuple[d.RawStream, ...]]:
            assert source.diagnostic_snapshot() == ()
            return snapshot(remaining=remaining)

        monkeypatch.setattr(backend, "snapshot", fresh_snapshot)
        clock.now = 2
        events = source.poll()
        assert [event.kind for event in events] == [d.EventKind.RESET, d.EventKind.UPSERT]
        changed = events[1]
        assert changed.key != initial[0].key
        assert changed.program == d.ProgramKey("dion", "dion")
        assert source.diagnostic_snapshot()[0]["program"] == {"app_id": "dion", "binary": "dion"}
        clock.now = 30
        assert source.poll() == ()
        assert reads == [0, 1, 2] and backend.calls == [("snapshot",), ("snapshot",)]
        messages = [record.getMessage() for record in caplog.records if record.name == d.__name__]
        assert messages == ([] if problem == "missing" else [meeting_events.CATALOG_LOAD_FAILED])
        assert MARKER not in caplog.text and "meeting-apps.json" not in caplog.text
    finally:
        source.stop()


def test_native_subscription_delivers_all_four_required_facilities() -> None:
    pulse = PulseFake()
    adapter = m.PulseMetadata(pulse=pulse)
    try:
        pulse.notices.extend(((0x12, 7), (0x23, 8), (0x05, 10), (0x17, 0xFFFFFFFF)))
        notices, overflow = adapter.poll()
        assert notices == (
            d.Notice(d.Facility.OUTPUT, d.Change.CHANGE, 7),
            d.Notice(d.Facility.MICROPHONE, d.Change.REMOVE, 8),
            d.Notice(d.Facility.CLIENT, d.Change.NEW, 10),
            d.Notice(d.Facility.SERVER, d.Change.CHANGE, 0),
        )
        assert pulse.mask == 0xAC and not overflow
    finally:
        adapter.close()


def test_default_factory_runs_metadata_lifecycle_at_pulse_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pulse, clock = PulseFake(), Clock()
    audio_calls: list[str] = []

    def call(name: str, *args: Any) -> Any:
        if name.startswith(("pa_stream_", "pa_simple_")):
            audio_calls.append(name)
            return 3 if name == "pa_simple_new" else 0
        return pulse.call(name, *args)

    libraries = {
        "libpulse.so.0": FakeLibrary(call),
        "libpulse-simple.so.0": FakeLibrary(call),
    }

    def load(name: str, **kwargs: Any) -> FakeLibrary:
        return libraries[name]

    monkeypatch.setattr(C, "CDLL", load)
    source = d.MeetingDetectorSource()
    monkeypatch.setattr(source, "clock", clock)
    try:
        old = upserts(source.start())
        assert len(old) == 2 and pulse.callback is not None
        pulse.state = PA_CONTEXT_FAILED
        assert_revoked(source.poll(), source)
        assert pulse.callback is None and not pulse.operations
        pulse.state = PA_CONTEXT_READY
        clock.now = 1
        fresh = upserts(source.poll())
        assert len(fresh) == 2 and fresh[0].key != old[0].key
        assert pulse.callback is not None and not pulse.operations
    finally:
        source.stop()
    assert pulse.callback is None and pulse.calls.count("pa_mainloop_free") == 2
    assert audio_calls == []


@pytest.mark.parametrize(
    "data,expected_binary,expected_program",
    [
        pytest.param(b"dionX", None, None, id="unterminated"),
        pytest.param(b"dion\0", "dion", d.ProgramKey("dion", "dion"), id="terminated"),
        pytest.param(b"x" * 4096 + b"\0", "x" * 4096, None, id="maximum"),
    ],
)
def test_native_proplist_uses_reported_bytes_and_requires_terminator(
    data: bytes,
    expected_binary: str | None,
    expected_program: d.ProgramKey | None,
) -> None:
    pulse = PulseFake()
    key = "application.process.binary"
    pulse.clients = [d.RawClient(10, properties(**{key: data}))]
    adapter = m.PulseMetadata(pulse=pulse)
    source = make(adapter, Clock())
    try:
        client = adapter.client(10)
        assert client is not None and client.valid is (expected_binary is not None)
        assert client.properties.get(key) == expected_binary
        assert [event.program for event in upserts(source.start())] == [expected_program] * 2
    finally:
        source.stop()


def test_catalog_recovery_checks_budget_before_publishing_full_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend, clock = Backend(), Clock()
    source = make(backend, clock)

    def read(path: Path, limit: int) -> bytes:
        return b"{broken-json" if clock.now == 0 else read_private(path, limit)

    monkeypatch.setattr(apps, "read_private", read)
    try:
        assert [event.program for event in upserts(source.start())] == [None]
        program = source._program

        def delayed(row: d.RawStream) -> d.ProgramKey | None:
            result = program(row)
            clock.now += 1.1
            return result

        monkeypatch.setattr(source, "_program", delayed)
        clock.now = 1
        assert_revoked(source.poll(), source)
        assert clock.now == pytest.approx(2.1)
        assert backend.calls == [("snapshot",), ("snapshot",)] and not backend.closed
    finally:
        source.stop()


def test_fast_restart_reads_large_current_catalog_only_when_snapshot_is_due(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    extra_apps = [
        {
            "id": f"app-{index}",
            "title": "T" * 220,
            "binary": [f"app-{index}-" + "b" * 128],
            "exe": [],
            "class": "other",
        }
        for index in range(127)
    ]
    document = {
        "schema": 1,
        "apps": [
            {"id": "old-dion", "title": "DION", "binary": ["dion"], "exe": [], "class": "dion"}
        ]
        + extra_apps,
    }
    content = json.dumps(document, separators=(",", ":")).encode().ljust(59_239, b" ")
    assert len(content) == 59_239
    clock = Clock()
    lifecycle: list[tuple[str, float]] = []

    def read(path: Path, limit: int) -> bytes:
        lifecycle.append(("catalog", clock.now))
        return content

    def factory() -> Backend:
        lifecycle.append(("connect", clock.now))
        return Backend()

    monkeypatch.setattr(apps, "read_private", read)
    source = d.MeetingDetectorSource(factory=factory, clock=clock)
    try:
        assert upserts(source.start())[0].program == d.ProgramKey("old-dion", "dion")
        source.stop()
        content = content.replace(b"old-dion", b"new-dion")
        clock.now = 0.1
        assert source.start() == ()
        assert lifecycle == [("catalog", 0), ("connect", 0)]
        clock.now = 0.99
        assert source.poll() == () and lifecycle == [("catalog", 0), ("connect", 0)]
        clock.now = 1
        assert upserts(source.poll())[0].program == d.ProgramKey("new-dion", "dion")
        assert lifecycle == [("catalog", 0), ("connect", 0), ("catalog", 1), ("connect", 1)]
    finally:
        source.stop()


@pytest.mark.parametrize("reply_at", [0.699, 0.701], ids=["within-limit", "after-limit"])
def test_snapshot_capacity_backoff_survives_late_dispatch(
    reply_at: float,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pulse, clock = PulseFake(), Clock()
    pulse.clients = [d.RawClient(index) for index in range(1025)]
    monkeypatch.setattr(m, "time", SimpleNamespace(monotonic=clock))
    source = make(m.PulseMetadata(pulse=pulse), clock)
    pending: list[tuple[str, tuple[Any, ...]]] = []
    starts: list[float] = []

    def queue(name: str, args: tuple[Any, ...]) -> int:
        assert name == "pa_context_get_client_info_list"
        pending.append((name, args))
        starts.append(clock.now)
        return pulse.new_op()

    def dispatch(loop: int) -> int:
        if not pending:
            return 0
        name, args = pending.pop()
        pulse.reply(name, args)
        clock.now = round(clock.now + reply_at, 9)
        return 1

    monkeypatch.setattr(pulse, "query", queue)
    monkeypatch.setattr(pulse.fake_lib.pa_mainloop_dispatch, "callback", dispatch)
    try:
        assert_revoked(source.start(), source)
        assert starts == [0]
        for attempts, delay in enumerate((1, 2, 4, 8), start=1):
            retry_at = clock.now + delay
            clock.now = retry_at - 0.01
            assert source.poll() == ()
            assert len(starts) == attempts
            clock.now = retry_at
            assert_revoked(source.poll(), source)
            assert len(starts) == attempts + 1 and starts[-1] == retry_at
        assert pulse.callback is not None and pulse.calls.count("pa_context_new") == 1
        assert not pulse.operations
    finally:
        source.stop()


def test_catalog_warns_once_until_it_recovers(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = Clock()
    source = d.MeetingDetectorSource(factory=Backend, clock=clock)
    broken = True
    reads: list[float] = []

    def read(path: Path, limit: int) -> bytes:
        reads.append(clock.now)
        return b"{broken-json" if broken else read_private(path, limit)

    def warnings() -> list[str]:
        return [record.getMessage() for record in caplog.records if record.name == d.__name__]

    message = meeting_events.CATALOG_LOAD_FAILED
    monkeypatch.setattr(apps, "read_private", read)
    try:
        assert [event.program for event in upserts(source.start())] == [None]
        for second in range(1, 61):
            clock.now = float(second)
            assert source.poll() == ()
        assert reads == list(range(61))
        assert warnings() == [message]
        broken = False
        clock.now = 61
        assert upserts(source.poll())[0].program == d.ProgramKey("dion", "dion")
        source.stop()
        broken = True
        clock.now = 62
        assert [event.program for event in upserts(source.start())] == [None]
        assert warnings() == [message, message]
    finally:
        source.stop()


def test_slow_catalog_recovery_with_notice_cannot_restart_poll_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend, clock = Backend(), Clock()
    backend.clients[11] = d.RawClient(11)
    source = make(backend, clock)

    def read(path: Path, limit: int) -> bytes:
        if clock.now == 0:
            return b"{broken-json"
        clock.now += 1.1
        return read_private(path, limit)

    monkeypatch.setattr(apps, "read_private", read)
    try:
        initial = upserts(source.start())
        assert len(initial) == 1 and initial[0].program is None
        del backend.clients[11]
        backend.notify(d.Facility.CLIENT, d.Change.REMOVE, 11)
        clock.now = 1
        assert_revoked(source.poll(), source)
        assert clock.now == pytest.approx(2.1)
        assert backend.calls == [("snapshot",)] and not backend.closed
        clock.now = 3.099
        assert source.poll() == () and source.diagnostic_snapshot() == ()
        clock.now = 3.1
        reset, fresh = source.poll()
        assert reset.kind is d.EventKind.RESET
        assert fresh.kind is d.EventKind.UPSERT and fresh.key != initial[0].key
        assert fresh.program == d.ProgramKey("dion", "dion")
        assert source.diagnostic_snapshot()[0]["program"] == {"app_id": "dion", "binary": "dion"}
        assert backend.calls == [("snapshot",), ("snapshot",)]
    finally:
        source.stop()


def test_successful_empty_catalog_rearms_warning(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend, clock = Backend(), Clock()
    source = make(backend, clock)
    catalogs = iter((b"{broken-json", b'{"schema":1,"apps":[]}', b"{broken-json"))
    reads: list[float] = []

    def read(path: Path, limit: int) -> bytes:
        reads.append(clock.now)
        return next(catalogs)

    monkeypatch.setattr(apps, "read_private", read)
    try:
        assert [event.program for event in upserts(source.start())] == [None]
        for second in (1, 2):
            clock.now = second
            assert not any(event.program for event in source.poll())
        assert reads == [0, 1, 2]
        assert [record.getMessage() for record in caplog.records if record.name == d.__name__] == [
            meeting_events.CATALOG_LOAD_FAILED,
        ] * 2
    finally:
        source.stop()
