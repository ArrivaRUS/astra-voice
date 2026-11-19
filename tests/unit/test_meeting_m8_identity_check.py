"""E13.M8 step 2b: a fresh measurement and a private, anchored target."""

from __future__ import annotations

import ctypes as C
import errno
import os
import socket
import subprocess
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from test_meeting_m8_detector_source import PulseFake
from test_meeting_m8_repr import repr_without_object_addresses
from test_meeting_m8_ss import CONTROL, CONTROL_ROWS, SERVER, Child, Rig, pair, process, row

from astra_voice.meetings import identity, identity_sockets, identity_ss
from astra_voice.meetings.apps import AppClass, AppDefinition
from astra_voice.meetings.identity import (
    ProcessFacts,
    ProcessGone,
    ProcessKey,
    ProcessReused,
    ProcUnavailable,
    Reason,
    SocketFds,
)
from astra_voice.meetings.identity_ss import SocketMeasurement, SsStatus
from astra_voice.worker import meeting_identity_check as check
from astra_voice.worker import meeting_metadata
from astra_voice.worker.meeting_detector import (
    INVALID_INDEX,
    Direction,
    ProgramKey,
    RawClient,
    RawStream,
    project_properties,
)
from astra_voice.worker.monitor_stream import _ServerInfo

pytestmark = pytest.mark.unit

UID = 1000
PID = 72123
CLIENT = 601
OUT = 701
MIC = 801
COOKIE = 901
EXE = "/opt/local-dion/dion"
APP = AppDefinition("dion", "DION", ("dion",), (EXE,), AppClass.DION)
PROGRAM = ProgramKey("dion", "dion")
KEY = ProcessKey(PID, 61234)
FACTS = ProcessFacts(KEY, (UID,) * 4, EXE)


def props(pid: int = PID, binary: str = "dion", serial: int = 101) -> dict[str, object]:
    return {
        "application.process.id": pid,
        "application.process.binary": binary,
        "object.serial": serial,
    }


def picture(
    pid: int = PID,
    client: int = CLIENT,
    output_serial: int = 201,
    mic_serial: int = 301,
) -> check.Picture:
    return (
        (RawClient(client, props(pid)),),
        (
            RawStream(OUT, client, Direction.OUTPUT, props(pid, serial=output_serial)),
            RawStream(MIC, client, Direction.MICROPHONE, props(pid, serial=mic_serial)),
        ),
    )


def test_projection_canonical_fields_and_own_index() -> None:
    raw = picture()
    baseline = check._project(raw, frozenset())
    other = RawClient(12, props(pid=12))
    output = replace(raw[1][0], corked=True, muted=True)
    microphone = replace(raw[1][1], properties={**raw[1][1].properties, "media.name": "private"})
    assert (
        check._project(
            ((other, *raw[0]), (microphone, output, replace(output, client=12))),
            frozenset({12}),
        )
        == baseline
    )
    assert (
        check._project((raw[0], (replace(output, index=702), microphone)), frozenset()) != baseline
    )
    assert not check._project(((), raw[1]), frozenset()).valid


@pytest.mark.parametrize(
    "sample,facts,app,changed,codes,offer,variant1",
    [
        (
            SocketMeasurement(SsStatus.OK, Reason.CONNECTION, {KEY: 1}),
            FACTS,
            APP,
            False,
            Reason.CONNECTION,
            True,
            True,
        ),
        (
            SocketMeasurement(SsStatus.OK, Reason.CONNECTION, {KEY: 0}),
            FACTS,
            APP,
            False,
            Reason.NO_CONNECTION,
            False,
            False,
        ),
        (
            SocketMeasurement(SsStatus.ABSENT, Reason.SS_UNAVAILABLE),
            ProcUnavailable,
            APP,
            False,
            Reason.UNAVAILABLE,
            False,
            False,
        ),
        (
            SocketMeasurement(SsStatus.ABSENT, Reason.SS_UNAVAILABLE),
            FACTS,
            APP,
            False,
            check.VerificationCode.UNAVAILABLE,
            True,
            True,
        ),
        (
            SocketMeasurement(SsStatus.FAILED, Reason.SS_TIMEOUT),
            FACTS,
            APP,
            False,
            check.VerificationCode.FAILED,
            True,
            False,
        ),
        (
            SocketMeasurement(SsStatus.OK, Reason.CONNECTION, {KEY: 0}),
            FACTS,
            replace(APP, exe=()),
            False,
            Reason.DECLARED_ONLY,
            True,
            False,
        ),
        (
            SocketMeasurement(SsStatus.OK, Reason.CONNECTION, {KEY: 1}),
            FACTS,
            APP,
            True,
            check.Unsettled.PICTURE_CHANGED,
            True,
            False,
        ),
    ],
)
def test_pure_rights(
    sample: SocketMeasurement,
    facts: check._Reading,
    app: AppDefinition,
    changed: bool,
    codes: check.JournalCode,
    offer: bool,
    variant1: bool,
) -> None:
    projection = check._project(picture(), frozenset())
    request = check.CheckRequest(check.Purpose.OFFER, PROGRAM, None, 1.5)
    decision = check._analyse(
        (app,),
        projection,
        projection,
        check._FrozenProc({PID: facts}, {PID: facts}),
        UID,
        sample,
        version_changed=changed,
        cookie=COOKIE,
        measured_at=1.018,
        attempts=1,
        output_limit=1,
    )
    result = check._decide(request, decision)
    assert result.settled is not changed
    assert (result.offer, result.variant1, result.target is not None) == (offer, variant1, variant1)
    assert codes in result.journal


def test_pure_counter_includes_client_without_streams() -> None:
    raw = picture()
    projected = check._project(((*raw[0], RawClient(CLIENT + 1, props())), raw[1]), frozenset())
    calls, journal = check._resolve(
        projected,
        (APP,),
        check._FrozenProc({PID: FACTS}, {PID: FACTS}),
        UID,
        SocketMeasurement(SsStatus.OK, Reason.CONNECTION, {KEY: 1}),
    )
    assert calls[PROGRAM].active
    assert calls[PROGRAM].outputs == []
    assert Reason.COUNTER in journal[PROGRAM]


def test_pure_frozen_process_errors() -> None:
    with pytest.raises(ProcessReused):
        check._FrozenProc({PID: FACTS}, {PID: replace(FACTS, exe="/other/dion")}).snapshot(PID)
    with pytest.raises(ProcessGone):
        check._FrozenProc({PID: FACTS}, {PID: ProcessGone}).snapshot(PID)


class Clock:
    def __init__(self) -> None:
        self.now = 10.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class ImmediateSelector:
    """Read a fake child's output without entering the system poller."""

    def __enter__(self) -> ImmediateSelector:
        return self

    def __exit__(self, *args: object) -> None:
        pass

    def register(self, descriptor: int, events: int) -> None:
        pass

    def select(self, timeout: float) -> list[object]:
        return [object()]


class Pulse(PulseFake):
    def __init__(self, rig: Harness, pictures: tuple[check.Picture, check.Picture]) -> None:
        super().__init__()
        self.rig = rig
        self.pictures = iter(pictures)
        self.own_index = rig.own_index
        self.cookie = rig.cookie
        self.connected_to: tuple[Any, ...] | None = None
        self.name: bytes | None = None

    def call(self, name: str, *args: Any) -> Any:
        if name == "pa_context_new":
            self.name = args[1]
        elif name == "pa_context_connect":
            self.connected_to = args[1:]
            self.rig.clock.advance(0.0004)
            self.rig.connected = True
            if self.rig.pulse_failure == "connect":
                return -1
        elif name == "pa_context_get_server_info":
            self.calls.append(name)
            self.rig.clock.advance(0.0004)
            if self.rig.pulse_failure == "cookie":
                args[1](1, None, None)
            else:
                row = _ServerInfo(cookie=self.cookie)
                args[1](1, C.pointer(row), None)
            return self.new_op()
        elif name.endswith("_info_list"):
            self.rig.clock.advance(0.0004)
            if self.rig.pulse_failure == "query":
                return None
            if name == "pa_context_get_client_info_list":
                clients, streams = next(self.pictures)
                self.clients, self.streams = list(clients), list(streams)
                self.hold = self.rig.pulse_failure == "timeout"
        elif name == "pa_context_disconnect":
            if self.rig.close_fails:
                raise OSError(self.rig.secret)
            self.rig.clock.advance(self.rig.close_time)
            self.rig.connected = False
        elif name == "pa_mainloop_prepare":
            self.rig.waits.append(args[1])
            self.rig.clock.advance(max(1, args[1]) / 1_000_000)
        elif name == "pa_mainloop_poll":
            return 0
        return super().call(name, *args)


class FakeSocket:
    def __init__(self, rig: Harness, descriptor: int) -> None:
        self.rig = rig
        self.descriptor = descriptor
        self.family = rig.family

    def fileno(self) -> int:
        return self.descriptor

    def __enter__(self) -> FakeSocket:
        return self

    def __exit__(self, *args: object) -> None:
        self.rig.control_closed += 1


class Harness:
    secret = "private-meeting-info-損"

    def __init__(self, tmp_path: Path, patch: pytest.MonkeyPatch) -> None:
        self.patch = patch
        self.clock = Clock()
        with patch.context() as setup:
            self.ss = Rig(tmp_path, setup)
        self.proc = self.ss.proc
        self.control_fd = self.ss.control_fd
        self.cookie = COOKIE
        self.own_index = 902
        self.own = check.OwnContexts(0)
        self.connected = False
        self.added_fds = {self.control_fd: CONTROL}
        self.family = socket.AF_UNIX
        self.control_closed = 0
        self.pulse_failure: str | None = None
        self.close_fails = False
        self.close_time = 0.0
        self.pulses: list[Pulse] = []
        self.waits: list[int] = []
        self.scans: list[tuple[int, float]] = []
        self.measurements: list[tuple[float, bool, bytes | None]] = []
        self.images: list[tuple[check.Picture, check.Picture]] = []
        process(self.proc, PID, (30001,), exe=EXE)
        self.ss.output += pair(30001, 40001)
        self.ss.on_launch = lambda: self.clock.advance(0.015)
        real_socket_fds = self.proc.socket_fds

        def socket_fds(pid: int, deadline: float) -> SocketFds:
            self.scans.append((pid, deadline))
            if pid != os.getpid():
                return real_socket_fds(pid, deadline)
            descriptors = {11: 18, 12: 0}
            if self.connected:
                descriptors.update(self.added_fds)
            return SocketFds(ProcessKey(pid, 6), descriptors, True)

        def control(fd: int, inode: int) -> FakeSocket:
            assert fd == self.control_fd and inode == CONTROL
            return FakeSocket(self, fd)

        self.selector: type[ImmediateSelector] = ImmediateSelector
        patch.setattr(self.proc, "socket_fds", socket_fds)
        patch.setattr(check, "_dup_control_socket", control)
        self.checker = self.make_checker()

    def metadata(
        self,
        *,
        server: bytes | None,
        subscribe: bool,
        deadline: float,
        context_name: bytes,
        binary_limit: int,
    ) -> meeting_metadata.PulseMetadata:
        images = self.images.pop(0) if self.images else (picture(), picture())
        pulse = Pulse(self, images)
        self.pulses.append(pulse)
        self.measurements.append((deadline, subscribe, server))
        return meeting_metadata.PulseMetadata(
            pulse=pulse,
            server=server,
            subscribe=subscribe,
            deadline=deadline,
            context_name=context_name,
            binary_limit=binary_limit,
        )

    def make_checker(
        self,
        server: bytes | None = SERVER,
        *,
        apps: tuple[AppDefinition, ...] = (APP,),
        output_limit: int | None = None,
    ) -> check.IdentityCheck:
        rig = self

        class Checker(check.IdentityCheck):
            def _measure(
                self,
                request: check.CheckRequest,
                attempts: int,
            ) -> check._Decision | check.CheckResult:
                with rig.patch.context() as system:
                    virtual_time = SimpleNamespace(monotonic=rig.clock)
                    for module in (identity, identity_ss, identity_sockets, meeting_metadata):
                        system.setattr(module, "time", virtual_time)
                    system.setattr(identity_ss, "_control", rig.ss.control)
                    system.setattr(
                        identity_sockets,
                        "selectors",
                        SimpleNamespace(
                            DefaultSelector=rig.selector,
                            EVENT_READ=1,
                        ),
                    )
                    system.setattr(os, "read", rig.ss.read)
                    system.setattr(subprocess, "Popen", rig.ss.popen)
                    return super()._measure(request, attempts)

        options = {} if output_limit is None else {"output_limit": output_limit}
        return Checker(
            server,
            apps=apps,
            clock=self.clock,
            own_contexts=lambda: self.own,
            proc=self.proc,
            metadata=self.metadata,
            **options,
        )

    def run(
        self,
        purpose: check.Purpose = check.Purpose.OFFER,
        *,
        anchor: check.CallAnchor | None = None,
        program: ProgramKey | None = PROGRAM,
        remaining: float = check.CHECK_SECONDS,
    ) -> check.CheckResult:
        return self.checker.run(
            check.CheckRequest(purpose, program, anchor, self.clock() + remaining)
        )

    def queue_pictures(self, first: check.Picture, second: check.Picture | None = None) -> None:
        self.images.append((first, first if second is None else second))


@pytest.fixture
def h(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Harness]:
    rig = Harness(tmp_path, monkeypatch)
    try:
        yield rig
    finally:
        rig.close_fails = False
        for child in rig.ss.children:
            child.stubborn = False
            child.stdout.close()
        assert rig.checker.shutdown(rig.clock() + check.CHECK_SECONDS)


def assert_offer(result: check.CheckResult, *, variant1: bool) -> None:
    assert result.settled and result.unsettled is None
    assert result.app_class is AppClass.DION
    assert result.offer and result.variant1 is variant1
    assert (result.target is not None) is variant1
    assert result.anchor is not None


def test_run_fresh_verified_sample_and_bracket_order(h: Harness) -> None:
    operations: list[str] = []
    snapshot = h.proc.snapshot
    sockets = h.proc.socket_fds
    on_launch = h.ss.on_launch
    pulse_snapshot = meeting_metadata.PulseMetadata.snapshot

    def read_proc(pid: int) -> ProcessFacts:
        operations.append("R")
        return snapshot(pid)

    def read_fds(pid: int, deadline: float) -> SocketFds:
        operations.append("self" if pid == os.getpid() else "F")
        return sockets(pid, deadline)

    def launch() -> None:
        operations.append("S")
        on_launch()

    def get_picture(adapter: meeting_metadata.PulseMetadata, **kwargs: Any) -> check.Picture:
        operations.append("picture")
        return pulse_snapshot(adapter, **kwargs)

    h.patch.setattr(h.proc, "snapshot", read_proc)
    h.patch.setattr(h.proc, "socket_fds", read_fds)
    h.ss.on_launch = launch
    h.patch.setattr(meeting_metadata.PulseMetadata, "snapshot", get_picture)
    result = h.run()
    assert_offer(result, variant1=True)
    assert operations == ["self", "self", "picture", "R", "F", "S", "F", "R", "picture"]
    assert result.journal == (Reason.CONNECTION,)
    assert result.measured_mono_ms == pytest.approx(10018, abs=1)
    assert result.target == check.PinnedTarget(
        COOKIE,
        (check.PinnedStream(OUT, 201, CLIENT, 101, ProcessKey(PID, 91)),),
        result.target.measured_at if result.target else 0.0,
    )
    pulse = h.pulses[0]
    assert pulse.name == b"astra-voice-meeting-identity"
    assert pulse.connected_to == (b"unix:" + SERVER, 1, None)
    assert set(pulse.property_reads) <= {
        "application.process.id",
        "application.process.binary",
        "object.serial",
    }
    assert not pulse.operations and not h.connected and h.control_closed == 1
    assert not any("subscribe" in call for call in pulse.calls)
    argv, kwargs = h.ss.calls[0]
    assert argv == ("/usr/bin/ss", "-x", "-H")
    assert kwargs["close_fds"] and not kwargs["shell"]
    assert kwargs["stdin"] == kwargs["stderr"] == subprocess.DEVNULL


@pytest.mark.parametrize("remaining", [0.2, 0.5])
def test_ss_timeout_uses_smaller_deadline(h: Harness, remaining: float) -> None:
    waits: list[float] = []

    class TimeoutSelector(ImmediateSelector):
        def select(self, timeout: float) -> list[object]:
            waits.append(timeout)
            h.clock.advance(timeout)
            return []

    h.selector = TimeoutSelector
    h.ss.on_launch = lambda: None
    result = h.run(remaining=remaining)
    assert waits == [pytest.approx(min(0.3, remaining - 0.002))]
    if remaining == 0.2:
        assert result.unsettled is check.Unsettled.BUDGET and not result.offer
    else:
        assert_offer(result, variant1=False)
        assert Reason.SS_TIMEOUT in result.journal
    assert h.ss.children[0].events[0][0] == "kill"


@pytest.mark.parametrize(
    "server",
    [
        None,
        b"",
        b"tcp:127.0.0.1:4713",
        b"unix:relative",
        b"{id}unix:/tmp/pulse",
        b"unix:/local tcp:other",
        b"/local\ntcp:other",
        b"unix:/local\0tcp:other",
    ],
)
def test_nonlocal_or_missing_server_never_calls_connect(h: Harness, server: bytes | None) -> None:
    h.checker = h.make_checker(server)
    for purpose in check.Purpose:
        result = h.run(purpose)
        assert result.unsettled is check.Unsettled.REMOTE and result.attempts == 0
        assert not (result.offer or result.variant1 or result.target)
    assert not h.pulses and not h.scans and not h.ss.calls


def test_actual_nonunix_control_socket_closes_context(h: Harness) -> None:
    h.family = socket.AF_INET
    result = h.run(check.Purpose.CARD)
    assert result.unsettled is check.Unsettled.REMOTE and not result.offer
    assert h.pulses[0].calls.count("pa_context_disconnect") == 1
    assert not h.ss.calls and not h.connected


@pytest.mark.parametrize("count", [0, 2])
def test_ambiguous_control_fd_cannot_grant_target(h: Harness, count: int) -> None:
    h.added_fds = dict(enumerate(range(999, 999 + count), start=20))
    result = h.run()
    assert_offer(result, variant1=False)
    assert check.VerificationCode.FAILED in result.journal
    assert not h.ss.calls


@pytest.mark.parametrize("failure", ["connect", "cookie", "query", "capacity", "timeout"])
def test_pulse_failed_oversize_and_absolute_budget(h: Harness, failure: str) -> None:
    h.pulse_failure = failure
    if failure == "capacity":
        base = picture()
        strangers = tuple(RawClient(i) for i in range(1000, 2024))
        h.queue_pictures(((*base[0], *strangers), base[1]))
    result = h.run()
    assert result.unsettled is (
        check.Unsettled.BUDGET if failure == "timeout" else check.Unsettled.PULSE_FAILED
    )
    assert not (result.offer or result.variant1 or result.target)
    assert not h.ss.calls and not h.connected
    if failure == "timeout":
        assert h.waits and max(h.waits) == 5000 and h.clock() == pytest.approx(10.5, abs=0.000002)


@pytest.mark.parametrize("read_at", [1, 2])
@pytest.mark.parametrize("field", ["exe", "status"])
def test_eacces_on_mandatory_read_overrides_absent_ss(
    h: Harness,
    read_at: int,
    field: str,
) -> None:
    reads = 0
    readlink = os.readlink
    snapshot = h.proc.snapshot
    open_status = identity.ProcReader._read

    def proc_readlink(path: Any, *args: Any, **kwargs: Any) -> Any:
        if path == "exe" and field == "exe" and reads == read_at:
            raise PermissionError(errno.EACCES, h.secret)
        return readlink(path, *args, **kwargs)

    def proc_read(directory: int, name: str) -> bytes:
        if name == "status" and field == "status" and reads == read_at:
            raise PermissionError(errno.EACCES, h.secret)
        return open_status(directory, name)

    def attributed(pid: int) -> ProcessFacts:
        nonlocal reads
        reads += 1
        return snapshot(pid)

    def missing() -> Child:
        raise FileNotFoundError(errno.ENOENT, h.secret)

    h.patch.setattr(h.proc, "snapshot", attributed)
    h.patch.setattr(identity.ProcReader, "_read", staticmethod(proc_read))
    h.patch.setattr(identity_sockets, "_ss_absent", lambda: True)
    h.ss.spawn = missing
    with h.patch.context() as system:
        system.setattr(os, "readlink", proc_readlink)
        result = h.run()
    assert result.settled and not (result.offer or result.variant1 or result.target)
    assert reads == 2 and result.journal == (Reason.UNAVAILABLE,)


@pytest.mark.parametrize(
    "code", [errno.ENOENT, errno.EACCES, errno.ENOEXEC, errno.EAGAIN, errno.ENOMEM]
)
def test_ss_launch_errno_needs_proven_absence(h: Harness, code: int) -> None:
    def broken() -> Child:
        raise OSError(code, h.secret)

    h.ss.spawn = broken
    h.patch.setattr(identity_sockets, "_ss_absent", lambda: False)
    result = h.run()
    assert_offer(result, variant1=False)
    assert Reason.SS_INSUFFICIENT in result.journal
    assert check.VerificationCode.FAILED in result.journal


def test_proven_missing_ss_preserves_only_documented_exception(h: Harness) -> None:
    def missing() -> Child:
        raise FileNotFoundError(errno.ENOENT, h.secret)

    h.ss.spawn = missing
    h.patch.setattr(identity_sockets, "_ss_absent", lambda: True)
    result = h.run()
    assert_offer(result, variant1=True)
    assert result.journal == (Reason.SS_UNAVAILABLE, check.VerificationCode.UNAVAILABLE)


@pytest.mark.parametrize(
    "failure,reason",
    [
        ("garbage", Reason.SS_INSUFFICIENT),
        ("control", Reason.SS_INSUFFICIENT),
        ("exit", Reason.SS_INSUFFICIENT),
        ("limit", Reason.SS_LIMIT),
        ("stubborn", Reason.SS_TIMEOUT),
    ],
)
def test_failed_ss_never_promotes_and_child_is_reaped(
    h: Harness,
    failure: str,
    reason: Reason,
) -> None:
    if failure == "garbage":
        h.ss.output += b"an invalid row\n"
    elif failure == "control":
        h.ss.output = row(CONTROL, 601) + pair(30001, 40001)
    elif failure == "exit":
        h.ss.spawn = lambda: Child(status=1)
    elif failure == "limit":
        h.ss.output = b"x" * (10 * 1024 * 1024)
    elif failure == "stubborn":
        h.ss.spawn = lambda: Child(stubborn=True)

    result = h.run()
    assert_offer(result, variant1=False)
    assert reason in result.journal and check.VerificationCode.FAILED in result.journal
    if failure == "stubborn":
        child = h.ss.children[0]
        blocked = h.run(check.Purpose.CARD)
        assert_offer(blocked, variant1=False)
        assert Reason.SS_INSUFFICIENT in blocked.journal and len(h.ss.calls) == 1
        assert not h.checker.shutdown(h.clock() + 0.1)
        child.stubborn = False
        assert h.checker.shutdown(h.clock() + 0.1) and child.returncode is not None


@pytest.mark.parametrize(
    "kind,expected",
    [
        ("zero", Reason.NO_CONNECTION),
        ("partial", Reason.SS_INSUFFICIENT),
        ("duplicates", Reason.CONNECTION),
        ("shared", Reason.SS_INSUFFICIENT),
        ("zero-catalog-only", Reason.DECLARED_ONLY),
    ],
)
def test_socket_certainty_and_catalog_without_exe(h: Harness, kind: str, expected: Reason) -> None:
    fd = h.proc.root / str(PID) / "fd"
    if kind in {"zero", "zero-catalog-only"}:
        h.ss.output = CONTROL_ROWS
    elif kind == "partial":
        h.ss.output = CONTROL_ROWS + row(40001, 30001, name=SERVER)
    elif kind == "duplicates":
        (fd / "123").symlink_to("socket:[30001]")
    elif kind == "shared":
        process(h.proc, PID + 1, (30001,), exe="/opt/other/unknown")
        raw = picture()
        h.queue_pictures(((*raw[0], RawClient(10, props(PID + 1, "unknown"))), raw[1]))
    if kind == "zero-catalog-only":
        h.checker = h.make_checker(apps=(replace(APP, exe=()),))
    result = h.run()
    assert result.settled and expected in result.journal
    assert (result.offer, result.variant1) == (kind != "zero", kind == "duplicates")


def extra_claimant(raw: check.Picture, *, own: int = CLIENT + 10) -> check.Picture:
    return ((*raw[0], RawClient(own, props())), raw[1])


@pytest.mark.parametrize("change", ["arrives", "leaves", "only-in-b", "orphan", "stream-serial"])
def test_changed_picture_is_fresh_and_fallback_requires_same_participants(
    h: Harness,
    change: str,
) -> None:
    baseline = picture()
    before, after = baseline, baseline
    if change == "arrives":
        after = extra_claimant(baseline)
    elif change == "leaves":
        before = extra_claimant(baseline)
    elif change == "only-in-b":
        before = ((), ())
    elif change == "orphan":
        stream = replace(baseline[1][0], client=1002, index=1003)
        after = (baseline[0], (*baseline[1], stream))
    else:
        after = picture(output_serial=202)
    h.queue_pictures(before, after)
    h.close_time = 0.24
    result = h.run()
    assert result.unsettled is check.Unsettled.PICTURE_CHANGED and not result.settled
    assert result.offer is (change in {"arrives", "leaves", "orphan"})
    assert result.attempts == 1 and not result.variant1 and result.target is None
    assert (result.anchor is not None) is result.offer
    if change == "only-in-b":
        assert [pid for pid, _ in h.scans if pid != os.getpid()] == []


@pytest.mark.parametrize("offset,expected_attempts", [(-0.001, 2), (0.0, 2), (0.001, 1)])
def test_retry_picture_changed_respects_quarter_second_boundary(
    h: Harness,
    monkeypatch: pytest.MonkeyPatch,
    offset: float,
    expected_attempts: int,
) -> None:
    baseline = picture()
    h.queue_pictures(baseline, extra_claimant(baseline))
    analyse = check._analyse

    def costs_time(*args: Any, **kwargs: Any) -> check._Decision:
        if kwargs["attempts"] == 1:
            h.clock.now = 10.25 + offset
        return analyse(*args, **kwargs)

    monkeypatch.setattr(check, "_analyse", costs_time)
    result = h.run()
    assert result.attempts == expected_attempts
    assert len(h.ss.calls) == len(h.pulses) == expected_attempts
    assert result.variant1 is (expected_attempts == 2)
    assert result.unsettled is (None if expected_attempts == 2 else check.Unsettled.PICTURE_CHANGED)
    assert {deadline for deadline, _, _ in h.measurements} == {10.5}
    for pulse in h.pulses:
        assert "pa_context_disconnect" in pulse.calls
        assert not any("subscribe" in name for name in pulse.calls)


def test_only_one_retry_and_no_pulse_subscription(h: Harness) -> None:
    for _ in range(2):
        h.queue_pictures(picture(), extra_claimant(picture()))
    result = h.run()
    assert result.unsettled is check.Unsettled.PICTURE_CHANGED and result.attempts == 2
    assert result.offer and not result.variant1 and result.target is None
    for pulse in h.pulses:
        assert pulse.calls.count("pa_context_disconnect") == 1
        assert not any("subscribe" in call for call in pulse.calls)


def test_version_fence_detects_unchanged_projection_with_new_context_set(h: Harness) -> None:
    on_launch = h.ss.on_launch

    def change_version() -> None:
        on_launch()
        h.own = check.OwnContexts(h.own.version + 1)

    h.ss.on_launch = change_version
    result = h.run()
    assert result.attempts == 2 and result.unsettled is check.Unsettled.PICTURE_CHANGED
    assert result.offer and not result.variant1 and result.target is None


def test_both_snapshots_exclude_role_contexts_and_own_index(h: Harness) -> None:
    raw = picture()
    role_index = h.own_index + 1
    h.own = check.OwnContexts(2, frozenset({role_index}), COOKIE)
    before = extra_claimant(raw, own=h.own_index)
    after = extra_claimant(raw, own=role_index)
    h.queue_pictures(before, after)
    result = h.run()
    assert_offer(result, variant1=True)
    assert result.attempts == 1


def test_role_index_from_old_server_does_not_exclude_fresh_claimant(h: Harness) -> None:
    idle_client = CLIENT + 10
    h.own = check.OwnContexts(3, frozenset({idle_client}), COOKIE - 1)
    h.queue_pictures(extra_claimant(picture(), own=idle_client))
    result = h.run()
    assert_offer(result, variant1=False)
    assert Reason.COUNTER in result.journal


def test_own_streams_cannot_offer_and_out_of_catalog_process_ignored(h: Harness) -> None:
    h.queue_pictures(picture(client=h.own_index))
    assert not h.run().offer
    process(h.proc, PID + 1, (30002,), exe="/usr/bin/astra-voice")
    h.ss.output += pair(30002, 40002)
    raw = picture(pid=PID + 1)
    h.queue_pictures(raw)
    result = h.run(check.Purpose.CARD, program=None)
    assert result.app_class is AppClass.OTHER and not result.offer and not result.variant1


def test_idle_claimant_is_counted_on_run(h: Harness) -> None:
    h.queue_pictures(extra_claimant(picture()))
    result = h.run()
    assert_offer(result, variant1=False)
    assert Reason.COUNTER in result.journal


def test_conflicting_stream_counts_in_both_pid_groups_and_cannot_be_target(h: Harness) -> None:
    other_pid = PID + 1
    process(h.proc, other_pid, (30002,), exe=EXE)
    h.ss.output += pair(30002, 40002)
    first, second = picture(), picture(pid=other_pid, client=CLIENT + 1)
    second_output = replace(second[1][0], index=OUT + 1)
    conflict = RawClient(CLIENT + 2, props())
    bad_output = RawStream(OUT + 2, conflict.index, Direction.OUTPUT, props(other_pid))
    h.queue_pictures(((*first[0], *second[0], conflict), (*first[1], second_output, bad_output)))
    result = h.run()
    assert_offer(result, variant1=False)
    assert {Reason.CONFLICT, Reason.COUNTER} <= set(result.journal)


@pytest.mark.parametrize("failure", ["reuse", "exit", "changed-exe", "foreign-uid", "basename"])
def test_proc_facts_are_frozen_per_pid_across_all_streams(h: Harness, failure: str) -> None:
    original = h.proc.snapshot
    snapshots: list[int] = []

    def read(pid: int) -> ProcessFacts:
        snapshots.append(pid)
        facts = original(pid)
        if len(snapshots) == 1 and failure not in {"basename", "foreign-uid"}:
            return facts
        if failure == "reuse":
            return replace(facts, key=ProcessKey(pid, 98))
        if failure == "exit":
            raise ProcessGone(h.secret)
        if failure == "changed-exe":
            return replace(facts, exe="/other/dion")
        if failure == "foreign-uid":
            return replace(facts, uids=(UID + 1,) * 4)
        return replace(facts, exe="/other/binary")

    expected = {
        "reuse": Reason.REUSED,
        "exit": Reason.DEAD,
        "changed-exe": Reason.REUSED,
        "foreign-uid": Reason.UID,
        "basename": Reason.BINARY,
    }
    h.patch.setattr(h.proc, "snapshot", read)
    result = h.run()
    assert snapshots == [PID, PID]
    assert result.settled and not result.offer and not result.variant1
    assert result.journal == (expected[failure],)


def test_restart_discards_verdict_socket_evidence_and_old_anchor(h: Harness) -> None:
    shown = h.run()
    assert_offer(shown, variant1=True)
    h.cookie += 1
    h.ss.output = CONTROL_ROWS
    second = h.run(check.Purpose.CARD, program=None)
    assert second.settled and not second.offer and second.journal == (Reason.NO_CONNECTION,)
    assert not second.variant1 and second.target is None and second.anchor is None
    assert len(h.ss.calls) == len(h.pulses) == 2

    # Even independent new evidence of the same streams cannot reuse the anchor.
    h.ss.output += pair(30001, 40001)
    new = h.run(check.Purpose.START, anchor=shown.anchor)
    assert new.target_error is check.TargetError.GONE and not new.variant1


@pytest.mark.parametrize("change", ["same", "gone", "new-call", "foreign-output", "new-serial"])
def test_start_requires_pinned_members_of_shown_call(h: Harness, change: str) -> None:
    shown = h.run()
    assert_offer(shown, variant1=True)
    base = picture()
    if change == "gone":
        h.queue_pictures((base[0], (base[1][0],)))
    elif change == "new-call":
        h.queue_pictures(picture(output_serial=211, mic_serial=311))
    elif change == "foreign-output":
        h.queue_pictures(
            (
                base[0],
                (
                    *base[1],
                    replace(base[1][0], index=OUT + 1, properties=props(serial=202)),
                ),
            )
        )
    elif change == "new-serial":
        h.queue_pictures(picture(output_serial=211))
    result = h.run(check.Purpose.START, anchor=shown.anchor)
    if change == "same":
        assert_offer(result, variant1=True)
        assert result.target is not None and len(result.target.streams) == 1
    else:
        expected = (
            check.TargetError.GONE if change in {"gone", "new-call"} else check.TargetError.MISMATCH
        )
        assert result.target_error is expected and not result.variant1 and result.target is None


@pytest.mark.parametrize("field", ["client", "client_serial", "stream_serial", "sink_input"])
def test_start_rejects_change_to_one_field_of_the_shown_output(h: Harness, field: str) -> None:
    # The microphone stays on a separate client of the same process. Changing
    # the output's client therefore leaves the microphone and the call intact.
    fd = h.proc.root / str(PID) / "fd"
    (fd / "101").symlink_to("socket:[30002]")
    h.ss.output += pair(30002, 40002)
    (client,), (output, mic) = picture()
    mic_client = RawClient(CLIENT + 1, props(serial=102))
    mic = replace(mic, client=mic_client.index)
    h.queue_pictures(((client, mic_client), (output, mic)))
    shown = h.run()
    assert_offer(shown, variant1=True)
    assert shown.target is not None and shown.attempts == 1
    assert shown.target.streams == (check.PinnedStream(OUT, 201, CLIENT, 101, ProcessKey(PID, 91)),)

    if field == "client":
        client = replace(client, index=CLIENT + 2)
        output = replace(output, client=client.index)
    elif field == "client_serial":
        client = replace(client, properties={**client.properties, "object.serial": 103})
    elif field == "stream_serial":
        output = replace(output, properties={**output.properties, "object.serial": 202})
    else:
        output = replace(output, index=OUT + 1)
    h.queue_pictures(((client, mic_client), (output, mic)))

    result = h.run(check.Purpose.START, anchor=shown.anchor)
    assert result.settled and result.attempts == 1 and result.journal == (Reason.CONNECTION,)
    assert result.target_error is check.TargetError.MISMATCH
    assert not result.variant1 and result.target is None


def test_start_detects_foreign_output_even_with_retained_mic_and_larger_limit(h: Harness) -> None:
    h.checker = h.make_checker(output_limit=8)
    shown = h.run()
    assert shown.anchor is not None and shown.variant1
    other_pid = PID + 1
    process(h.proc, other_pid, (30002,), exe=EXE)
    h.ss.output += pair(30002, 40002)
    first = picture()
    second = picture(pid=other_pid, client=CLIENT + 1)
    h.queue_pictures(((*first[0], *second[0]), (*first[1], replace(second[1][0], index=OUT + 1))))
    result = h.run(check.Purpose.START, anchor=shown.anchor)
    assert result.target_error is check.TargetError.MISMATCH
    assert not result.variant1 and result.target is None


def test_offer_looks_for_microphone_and_output_in_fresh_snapshot(h: Harness) -> None:
    clients, (output, microphone) = picture()
    for item in (output, microphone):
        h.queue_pictures((clients, (item,)))
        result = h.run()
        assert result.settled and not (result.offer or result.variant1 or result.anchor)
    other_pid = PID + 1
    process(h.proc, other_pid, (30002,), exe=EXE)
    h.ss.output += pair(30002, 40002)
    other = RawClient(CLIENT + 1, props(other_pid))
    microphone = replace(microphone, client=other.index, properties=props(other_pid, serial=301))
    h.queue_pictures(((*clients, other), (output, microphone)))
    assert_offer(h.run(), variant1=False)


def test_default_output_limit_keeps_offer_without_target(h: Harness) -> None:
    clients, streams = picture()
    more = (*streams, replace(streams[0], index=OUT + 1, properties=props(serial=202)))
    h.queue_pictures((clients, more))
    assert_offer(h.run(), variant1=False)
    h.checker = h.make_checker(output_limit=2)
    h.queue_pictures((clients, more))
    allowed = h.run()
    assert_offer(allowed, variant1=True)
    assert allowed.target is not None and len(allowed.target.streams) == 2


def test_close_failure_has_no_audio_and_retries_cleanup_before_new_context(h: Harness) -> None:
    h.close_fails = True
    result = h.run()
    assert result.unsettled is check.Unsettled.PULSE_FAILED and not result.offer
    assert not h.checker.shutdown(h.clock() + 0.1)
    assert h.connected and len(h.pulses) == 1
    h.close_fails = False
    assert h.checker.shutdown(h.clock() + 0.1) and not h.connected
    assert not any("subscribe" in call for call in h.pulses[0].calls)


def test_close_failure_with_subsequent_run_finishes_cleanup_before_open(h: Harness) -> None:
    h.close_fails = True
    assert h.run().unsettled is check.Unsettled.PULSE_FAILED
    assert h.run().unsettled is check.Unsettled.PULSE_FAILED
    assert len(h.pulses) == 1
    h.close_fails = False
    assert_offer(h.run(), variant1=True)
    assert len(h.pulses) == 2 and not h.connected


def test_last_decision_can_exceed_deadline_no_late_target(h: Harness) -> None:
    decide = check._decide

    def slow(*args: Any, **kwargs: Any) -> check.CheckResult:
        result = decide(*args, **kwargs)
        assert result.variant1 and result.target is not None
        h.clock.now = 10.5
        return result

    h.patch.setattr(check, "_decide", slow)
    result = h.run()
    assert result.unsettled is check.Unsettled.BUDGET and not result.settled
    assert not (result.offer or result.variant1 or result.target or result.anchor)
    assert result.attempts == 1 and result.measured_mono_ms == 0


def test_rate_exhaustion_reserves_two_actual_launches_for_start(h: Harness) -> None:
    shown = h.run()
    assert_offer(shown, variant1=True)
    for _ in range(3):
        assert_offer(h.run(check.Purpose.CARD), variant1=True)

    blocked = h.run()
    assert blocked.unsettled is check.Unsettled.RATE and not blocked.settled
    assert not (blocked.offer or blocked.variant1 or blocked.target)
    assert len(h.ss.calls) == len(h.pulses) == 4
    for _ in range(2):
        assert_offer(h.run(check.Purpose.START, anchor=shown.anchor), variant1=True)
    assert h.run(check.Purpose.START, anchor=shown.anchor).unsettled is check.Unsettled.RATE
    assert len(h.ss.calls) == len(h.pulses) == 6

    h.clock.now = 15.001
    assert h.run(check.Purpose.START, anchor=shown.anchor).unsettled is check.Unsettled.RATE
    h.clock.now = 15.003
    assert_offer(h.run(check.Purpose.START, anchor=shown.anchor), variant1=True)
    assert h.run(check.Purpose.START, anchor=shown.anchor).unsettled is check.Unsettled.RATE


def test_rate_counts_failed_launch_and_retry_but_not_missing_control_fd(h: Harness) -> None:
    fds = h.added_fds
    h.added_fds = {}
    for _ in range(5):
        assert_offer(h.run(), variant1=False)
    assert not h.ss.calls
    h.added_fds = fds

    def broken() -> Child:
        raise OSError(errno.EAGAIN, h.secret)

    h.ss.spawn = broken
    assert_offer(h.run(), variant1=False)
    h.queue_pictures(picture(), extra_claimant(picture()))
    repeated = h.run()
    assert repeated.attempts == 2 and repeated.settled
    h.queue_pictures(picture(), extra_claimant(picture()))
    no_token = h.run()
    assert no_token.attempts == 1 and no_token.unsettled is check.Unsettled.PICTURE_CHANGED
    assert no_token.offer and not no_token.variant1
    assert h.run().unsettled is check.Unsettled.RATE and len(h.ss.calls) == 4


def test_queue_time_is_part_of_original_deadline(h: Harness) -> None:
    h.checker._active = True

    def waited(timeout: float | None = None) -> bool:
        assert timeout == pytest.approx(0.5)
        h.clock.advance(0.4)
        h.checker._active = False
        return True

    h.patch.setattr(h.checker._condition, "wait", waited)
    h.close_time = 0.09  # ~18 ms measurement plus close exceeds the remaining 100 ms
    result = h.run(check.Purpose.CARD)
    assert h.measurements == [(10.5, False, b"unix:" + SERVER)]
    assert result.unsettled is check.Unsettled.BUDGET and result.attempts == 1
    assert not (result.variant1 or result.target)


def test_queue_expiration_is_busy_without_connecting(h: Harness) -> None:
    h.checker._active = True

    def waited(timeout: float | None = None) -> bool:
        assert timeout is not None
        h.clock.advance(timeout)
        h.checker._active = False
        return False

    h.patch.setattr(h.checker._condition, "wait", waited)
    result = h.run(check.Purpose.CARD)
    assert result.unsettled is check.Unsettled.BUSY and not result.offer
    assert not h.pulses


def test_public_payload_and_repr_only_expose_fixed_codes(
    h: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    raw = picture()
    private = h.secret
    client_props = {
        **raw[0][0].properties,
        "application.name": private,
        "media.name": private,
        "pipewire.sec.pid": PID + 1,
        "client.api": "pipewire",
    }
    h.queue_pictures(((replace(raw[0][0], properties=client_props),), raw[1]))
    result = h.run()
    assert result.anchor is not None and result.target is not None
    request = check.CheckRequest(check.Purpose.START, PROGRAM, result.anchor, 11.0)
    for internal in (result.anchor, result.target, result.target.streams[0], request, result):
        representation = repr_without_object_addresses(internal)
        for secret in (private, EXE, str(PID), str(CLIENT), str(MIC), str(OUT), str(COOKIE)):
            assert secret not in representation
    h.pulse_failure = "query"
    h.close_fails = True
    failure = h.run()
    assert failure.journal == (check.Unsettled.PULSE_FAILED,)
    assert private not in caplog.text + repr(failure)
    assert "PinnedTarget" not in repr(result) and "CallAnchor" not in repr(result)


def test_queued_start_goes_before_card_without_restarting_deadlines(h: Harness) -> None:
    shown = h.run()
    assert shown.anchor is not None
    h.checker._active = True
    deadline = h.clock() + 0.5
    waiting = 0
    start: check.CheckResult | None = None

    def waited(timeout: float | None = None) -> bool:
        nonlocal waiting, start
        waiting += 1
        assert waiting == 1 and timeout == pytest.approx(0.5)
        assert check.Purpose.CARD in h.checker._pending
        h.clock.advance(0.02)
        h.checker._active = False
        start = h.checker.run(
            check.CheckRequest(check.Purpose.START, PROGRAM, shown.anchor, deadline)
        )
        return True

    h.patch.setattr(h.checker._condition, "wait", waited)
    card = h.checker.run(check.CheckRequest(check.Purpose.CARD, None, None, deadline))
    assert start is not None
    assert_offer(start, variant1=True)
    assert_offer(card, variant1=True)
    assert start.measured_mono_ms < card.measured_mono_ms
    assert len(h.ss.calls) == 3
    assert h.measurements[1:] == [(deadline, False, b"unix:" + SERVER)] * 2


def test_pending_same_program_coalesces_and_unrelated_request_is_busy(h: Harness) -> None:
    h.checker._active = True
    purpose = check.Purpose.OFFER
    request = check.CheckRequest(purpose, PROGRAM, None, h.clock() + 0.5)
    other = replace(request, program=ProgramKey("other", "other"))
    waits = 0
    joined: check.CheckResult | None = None

    def waited(timeout: float | None = None) -> bool:
        nonlocal waits, joined
        waits += 1
        assert timeout == pytest.approx(0.5)
        job = h.checker._pending[purpose]
        if waits == 1:
            excess = h.checker.run(other)
            assert excess.unsettled is check.Unsettled.BUSY and not excess.offer
            joined = h.checker.run(replace(request))
        elif waits == 2:
            assert h.checker._pending == {purpose: job} and job.request is request
            # Emulate completion by the admitted caller, without sleeping or
            # depending on how quickly the OS schedules another Python thread.
            job.result = h.checker._execute(job.request)
            del h.checker._pending[purpose]
            h.checker._active = False
        else:
            pytest.fail("unexpected wait")
        return True

    h.patch.setattr(h.checker._condition, "wait", waited)
    result = h.checker.run(request)
    assert_offer(result, variant1=True)
    assert joined == result and waits == 2 and len(h.ss.calls) == 1


def test_queued_follower_cannot_inherit_longer_deadline(h: Harness) -> None:
    h.checker._active = True
    request = check.CheckRequest(check.Purpose.OFFER, PROGRAM, None, h.clock() + 0.5)
    follower = replace(request, deadline=h.clock() + 0.1)
    joined: check.CheckResult | None = None
    waits = 0

    def waited(timeout: float | None = None) -> bool:
        nonlocal waits, joined
        waits += 1
        if waits == 1:
            joined = h.checker.run(follower)
            h.checker._active = False
        elif waits == 2:
            assert timeout == pytest.approx(0.1)
            h.clock.now = follower.deadline
        else:
            pytest.fail("unexpected wait")
        return True

    h.patch.setattr(h.checker._condition, "wait", waited)
    assert_offer(h.checker.run(request), variant1=True)
    assert joined is not None and joined.unsettled is check.Unsettled.BUSY
    assert not joined.variant1 and len(h.ss.calls) == 1


def test_missing_output_serial_blocks_target_with_verified_neighbor(h: Harness) -> None:
    clients, streams = picture()
    extra = replace(streams[0], index=OUT + 1, properties=props())
    extra = replace(
        extra,
        properties={
            key: value for key, value in extra.properties.items() if key != "object.serial"
        },
    )
    h.checker = h.make_checker(output_limit=2)
    h.queue_pictures((clients, (*streams, extra)))
    shown = h.run()
    assert_offer(shown, variant1=False)
    h.queue_pictures((clients, (*streams, extra)))
    result = h.run(check.Purpose.START, anchor=shown.anchor)
    assert result.target_error is check.TargetError.MISMATCH and not result.variant1


def test_start_cannot_link_multiple_preexisting_output_clients_to_one_call(h: Harness) -> None:
    h.checker = h.make_checker(output_limit=2)
    other_pid = PID + 1
    process(h.proc, other_pid, (30002,), exe=EXE)
    h.ss.output += pair(30002, 40002)
    first, second = picture(), picture(pid=other_pid, client=CLIENT + 1)
    mixed = ((*first[0], *second[0]), (*first[1], replace(second[1][0], index=OUT + 1)))
    h.queue_pictures(mixed)
    shown = h.run()
    assert shown.anchor is not None and shown.offer
    h.queue_pictures(mixed)
    result = h.run(check.Purpose.START, anchor=shown.anchor)
    assert result.target_error is check.TargetError.MISMATCH and result.target is None


def test_control_descriptor_reuse_is_checked_against_observed_inode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actual_dup = os.dup
    duplicates: list[int] = []

    def track_dup(descriptor: int) -> int:
        duplicate = actual_dup(descriptor)
        duplicates.append(duplicate)
        return duplicate

    first, second = socket.socketpair()
    with first, second:
        fd = first.fileno()
        inode = os.fstat(fd).st_ino
        assert os.fstat(second.fileno()).st_ino != inode

        def wrap_socket(*, fileno: int) -> socket.socket:
            # The sandbox blocks family autodetection with getsockopt.
            return socket.socket(socket.AF_UNIX, socket.SOCK_STREAM, 0, fileno=fileno)

        with monkeypatch.context() as system:
            system.setattr(os, "dup", track_dup)
            system.setattr(check, "socket", SimpleNamespace(socket=wrap_socket))
            with pytest.raises(OSError):
                check._dup_control_socket(fd, os.fstat(second.fileno()).st_ino).close()
            assert len(duplicates) == 1
            with pytest.raises(OSError) as closed:
                os.fstat(duplicates[0])
            assert closed.value.errno == errno.EBADF
            with check._dup_control_socket(fd, inode) as control:
                assert (
                    control.family == socket.AF_UNIX and os.fstat(control.fileno()).st_ino == inode
                )
        assert os.fstat(fd).st_ino == inode


def test_start_rechecks_rights_even_for_same_shown_stream_objects(h: Harness) -> None:
    shown = h.run()
    assert shown.anchor is not None and shown.variant1
    h.ss.output = CONTROL_ROWS
    result = h.run(check.Purpose.START, anchor=shown.anchor)
    assert result.settled and result.target_error is check.TargetError.MISMATCH
    assert not result.variant1 and result.target is None and Reason.NO_CONNECTION in result.journal


@pytest.mark.parametrize("limit", [1, 2])
def test_losing_one_of_the_shown_outputs_never_authorizes_the_remainder(
    h: Harness,
    limit: int,
) -> None:
    h.checker = h.make_checker(output_limit=limit)
    clients, (output, mic) = picture()
    neighbor = replace(output, index=OUT + 1, properties=props(serial=202))
    h.queue_pictures((clients, (output, mic, neighbor)))
    shown = h.run()
    assert_offer(shown, variant1=limit == 2)
    h.queue_pictures((clients, (mic, neighbor)))
    start = h.run(check.Purpose.START, anchor=shown.anchor)
    assert start.target_error is check.TargetError.MISMATCH and start.target is None


@pytest.mark.parametrize("purpose", [check.Purpose.OFFER, check.Purpose.CARD])
def test_different_processes_cannot_form_an_authorized_instance(
    h: Harness,
    purpose: check.Purpose,
) -> None:
    other_pid = PID + 1
    process(h.proc, other_pid, (30002,), exe=EXE)
    h.ss.output += pair(30002, 40002)
    clients, (output, _) = picture()
    other_clients, (_, mic) = picture(pid=other_pid, client=CLIENT + 1)
    h.queue_pictures((clients + other_clients, (output, mic)))
    shown = h.run(purpose)
    assert_offer(shown, variant1=False)
    assert (
        h.run(check.Purpose.START, anchor=shown.anchor).target_error is check.TargetError.MISMATCH
    )


def test_same_process_can_use_separate_clients_and_replace_microphone(h: Harness) -> None:
    fd = h.proc.root / str(PID) / "fd"
    (fd / "101").symlink_to("socket:[30003]")
    h.ss.output += pair(30003, 40003)
    clients, (output, mic) = picture()
    other = RawClient(CLIENT + 1, props(serial=102))
    mic = replace(mic, client=other.index)
    h.queue_pictures(((*clients, other), (output, mic)))
    shown = h.run()
    assert_offer(shown, variant1=True)
    h.queue_pictures(((*clients, other), (output, replace(mic, index=MIC + 1))))
    assert_offer(h.run(check.Purpose.START, anchor=shown.anchor), variant1=True)


def test_unconfirmed_other_process_with_stream_blocks_variant1(h: Harness) -> None:
    clients, streams = picture()
    other_pid = PID + 1
    other = RawClient(CLIENT + 1, props(other_pid))
    other_output = replace(
        streams[0], client=other.index, index=OUT + 1, properties=props(other_pid)
    )
    h.queue_pictures(((*clients, other), (*streams, other_output)))
    result = h.run()
    assert_offer(result, variant1=False)
    assert Reason.DEAD in result.journal


def test_start_loss_of_proc_confirmation_with_neighbor_is_mismatch(h: Harness) -> None:
    shown = h.run()
    assert_offer(shown, variant1=True)
    other_pid = PID + 1
    process(h.proc, other_pid, (30002,), exe=EXE)
    h.ss.output += pair(30002, 40002)
    clients, streams = picture()
    other_clients, other_streams = picture(pid=other_pid, client=CLIENT + 1)
    h.queue_pictures(
        (
            clients + other_clients,
            (
                *streams,
                *(replace(stream, index=stream.index + 1) for stream in other_streams),
            ),
        )
    )
    read = h.proc.snapshot

    def failed_read(pid: int) -> ProcessFacts:
        if pid == PID:
            raise ProcUnavailable()
        return read(pid)

    h.patch.setattr(h.proc, "snapshot", failed_read)
    start = h.run(check.Purpose.START, anchor=shown.anchor)
    assert start.settled and Reason.UNAVAILABLE in start.journal
    assert start.target_error is check.TargetError.MISMATCH and start.target is None


@pytest.mark.parametrize("loss", ["ss-timeout", "insufficient", "counter", "pulse"])
def test_start_refusals_keep_a_target_code(h: Harness, loss: str) -> None:
    shown = h.run()
    assert_offer(shown, variant1=True)
    if loss == "ss-timeout":
        h.ss.spawn = lambda: Child(stubborn=True)
    elif loss == "insufficient":
        h.ss.output = b"truncated"
    elif loss == "counter":
        h.queue_pictures(extra_claimant(picture()))
    else:
        h.pulse_failure = "query"
    start = h.run(check.Purpose.START, anchor=shown.anchor)
    assert start.target_error is check.TargetError.MISMATCH
    assert not start.variant1 and start.target is None


@pytest.mark.parametrize("change", [False, True])
def test_internal_stream_with_no_client_does_not_enter_projection(h: Harness, change: bool) -> None:
    clients, streams = picture()
    internal = RawStream(OUT + 1, INVALID_INDEX, Direction.OUTPUT, props(PID + 1, serial=501))
    h.queue_pictures(
        (clients, (*streams, internal)), (clients, (*streams, replace(internal, index=OUT + 2)))
    )
    result = h.run()
    assert_offer(result, variant1=True)
    assert result.attempts == 1
    assert {pid for pid, _ in h.scans if pid != os.getpid()} == {PID}


def test_journal_uses_combined_declarations_when_only_streams_have_pid(h: Harness) -> None:
    clients, streams = picture()
    properties = {
        key: val for key, val in clients[0].properties.items() if key != "application.process.id"
    }
    h.queue_pictures(((replace(clients[0], properties=properties),), streams))
    result = h.run()
    assert_offer(result, variant1=True)
    assert result.journal == (Reason.CONNECTION,)


def test_consistent_output_of_client_with_conflicting_sibling_is_excluded(h: Harness) -> None:
    fd = h.proc.root / str(PID) / "fd"
    (fd / "101").symlink_to("socket:[30003]")
    h.ss.output += pair(30003, 40003)
    h.checker = h.make_checker(output_limit=2)
    clients, streams = picture()
    other = RawClient(CLIENT + 1, props(serial=102))
    other_output = replace(
        streams[0], index=OUT + 1, client=other.index, properties=props(serial=202)
    )
    h.queue_pictures(((*clients, other), (*streams, other_output)))
    assert_offer(h.run(), variant1=True)
    bad_sibling = replace(
        streams[1],
        index=MIC + 1,
        client=other.index,
        properties=props(binary="unknown", serial=302),
    )
    h.queue_pictures(((*clients, other), (*streams, other_output, bad_sibling)))
    result = h.run()
    assert_offer(result, variant1=False)
    assert Reason.CONFLICT in result.journal and Reason.CONNECTION in result.journal


@pytest.mark.parametrize(
    ("kind", "field"),
    [
        *[("client", key) for key in ("index", "serial", "pid", "binary", "valid")],
        *[
            ("stream", key)
            for key in ("direction", "index", "client", "serial", "pid", "binary", "valid")
        ],
    ],
)
def test_single_projection_field_change_is_detected_on_run(
    h: Harness, kind: str, field: str
) -> None:
    clients, streams = picture()
    spare = RawClient(CLIENT + 1)
    client = clients[0]
    output, mic = streams
    property_field = {
        "serial": ("object.serial", 209),
        "pid": ("application.process.id", PID + 1),
        "binary": ("application.process.binary", "unmatched"),
        "valid": ("application.process.id", b"\xff\0"),
    }
    a = (clients + (spare,), streams)
    if kind == "client":
        if field == "index":
            b = (clients + (replace(spare, index=CLIENT + 2),), streams)
        else:
            key, value = property_field[field]
            raw = dict(client.properties)
            if field == "valid":
                # A and B normalize to the same declarations. Only validity changes.
                del raw[key]
                a = ((replace(client, properties=raw), spare), streams)
            b = ((replace(client, properties={**raw, key: value}), spare), streams)
    elif field == "direction":
        b = (a[0], (replace(output, direction=Direction.MICROPHONE), mic))
    elif field == "index":
        b = (a[0], (replace(output, index=OUT + 1), mic))
    elif field == "client":
        b = (a[0], (replace(output, client=spare.index), mic))
    else:
        key, value = property_field[field]
        raw = dict(output.properties)
        if field == "valid":
            del raw[key]
            a = (a[0], (replace(output, properties=raw), mic))
        b = (a[0], (replace(output, properties={**raw, key: value}), mic))
    h.queue_pictures(a, b)
    h.close_time = 0.25
    result = h.run()
    assert result.unsettled is check.Unsettled.PICTURE_CHANGED
    assert result.attempts == 1 and not result.variant1 and result.target is None


@pytest.mark.parametrize("first_expires", [False, True])
def test_card_filters_share_one_measurement_with_independent_deadlines(
    h: Harness,
    first_expires: bool,
) -> None:
    h.checker._active = True
    other = ProgramKey("other", "other")
    first = check.CheckRequest(check.Purpose.CARD, other, None, h.clock() + 0.3)
    second = check.CheckRequest(check.Purpose.CARD, None, None, h.clock() + 0.5)
    waiting = 0
    joined: check.CheckResult | None = None

    def waited(timeout: float | None = None) -> bool:
        nonlocal waiting, joined
        waiting += 1
        if waiting == 1:
            assert timeout == pytest.approx(0.3)
            joined = h.checker.run(second)
        else:
            assert waiting == 2 and timeout == pytest.approx(0.5)
            if first_expires:
                h.clock.advance(0.35)
            h.checker._active = False
        return True

    h.patch.setattr(h.checker._condition, "wait", waited)
    result = h.checker.run(first)
    assert not result.offer
    assert joined is not None
    assert_offer(joined, variant1=True)
    if first_expires:
        assert result.unsettled is check.Unsettled.BUSY and result.measured_mono_ms == 0
    else:
        assert result.settled and result.measured_mono_ms == joined.measured_mono_ms
    assert h.measurements == [(second.deadline, False, b"unix:" + SERVER)]
    assert len(h.ss.calls) == 1


def test_coalesced_cards_select_the_requested_active_program(h: Harness) -> None:
    chromium = AppDefinition(
        "browser", "Chromium", ("chromium",), ("/usr/lib/chromium/chromium",), AppClass.BROWSER
    )
    chromium_program = ProgramKey(chromium.id, "chromium")
    missing_program = ProgramKey("missing", "missing")
    chromium_pid = PID + 1
    chromium_client = CLIENT + 1
    process(h.proc, chromium_pid, (30002,), exe=chromium.exe[0])
    h.ss.output += pair(30002, 40002)
    h.checker = h.make_checker(apps=(APP, chromium))
    clients, streams = picture()
    chromium_streams = (
        RawStream(OUT + 1, chromium_client, Direction.OUTPUT, props(chromium_pid, "chromium", 202)),
        RawStream(
            MIC + 1, chromium_client, Direction.MICROPHONE, props(chromium_pid, "chromium", 302)
        ),
    )
    h.queue_pictures(
        (
            (*clients, RawClient(chromium_client, props(chromium_pid, "chromium", 102))),
            (*streams, *chromium_streams),
        )
    )

    h.checker._active = True
    waiting = iter((chromium_program, missing_program, None))
    joined: dict[ProgramKey, check.CheckResult] = {}

    def waited(timeout: float | None = None) -> bool:
        assert timeout == pytest.approx(check.CHECK_SECONDS)
        program = next(waiting)
        if program is None:
            h.checker._active = False
        else:
            joined[program] = h.run(check.Purpose.CARD, program=program)
        return True

    h.patch.setattr(h.checker._condition, "wait", waited)
    dion_result = h.run(check.Purpose.CARD)
    chromium_result = joined[chromium_program]
    for result, program, app_class, target in (
        (
            dion_result,
            PROGRAM,
            AppClass.DION,
            check.PinnedStream(OUT, 201, CLIENT, 101, ProcessKey(PID, 91)),
        ),
        (
            chromium_result,
            chromium_program,
            AppClass.BROWSER,
            check.PinnedStream(OUT + 1, 202, chromium_client, 102, ProcessKey(chromium_pid, 91)),
        ),
    ):
        assert result.settled and result.offer and result.variant1 and result.attempts == 1
        assert result.anchor is not None and result.anchor.program == program
        assert result.app_class is app_class
        assert result.target is not None and result.target.streams == (target,)
    missing = joined[missing_program]
    assert missing.settled and missing.app_class is AppClass.OTHER
    assert not missing.offer and not missing.variant1
    assert missing.target is None and missing.anchor is None
    assert (
        dion_result.measured_mono_ms == chromium_result.measured_mono_ms == missing.measured_mono_ms
    )
    assert len(h.ss.calls) == len(h.pulses) == 1


def test_completed_measurement_notifies_a_real_waiting_thread(h: Harness) -> None:
    launched = threading.Barrier(2, timeout=6)
    queued = threading.Barrier(2, timeout=6)
    proceed = threading.Event()
    wait = h.checker._condition.wait

    def first_launch() -> None:
        h.clock.advance(0.015)
        if len(h.ss.calls) == 1:
            launched.wait()
            assert proceed.wait(12)

    def waiting(timeout: float | None = None) -> bool:
        assert timeout is not None and timeout > 6
        queued.wait()
        return wait(timeout)

    h.ss.on_launch = first_launch
    h.patch.setattr(h.checker._condition, "wait", waiting)
    with ThreadPoolExecutor(max_workers=2) as pool:
        try:
            active = pool.submit(h.run, remaining=12)
            launched.wait()
            follower = pool.submit(h.run, check.Purpose.CARD, remaining=12)
            queued.wait()
            proceed.set()
            assert_offer(active.result(timeout=6), variant1=True)
            assert_offer(follower.result(timeout=6), variant1=True)
        finally:
            proceed.set()
            with h.checker._condition:
                h.checker._condition.notify_all()
    assert len(h.ss.calls) == 2


def test_dense_projection_yields_slot_to_queued_start_at_deadline(h: Harness) -> None:
    shown = h.run()
    assert_offer(shown, variant1=True)
    first, streams = picture()
    dense = tuple(RawClient(index, props(binary="x" * 4095)) for index in range(1000, 2000))
    h.queue_pictures(((*first, *dense), streams))
    read = check._Properties.read
    waiting = threading.Barrier(2, timeout=6)
    halfway = threading.Barrier(2, timeout=6)
    proceed = threading.Event()
    wait = h.checker._condition.wait
    processed = 0

    def counted(
        cls: type[check._Properties], values: dict[str, object], valid: bool
    ) -> check._Properties:
        nonlocal processed
        if not valid:
            assert "application.process.binary" not in values  # rejected by libpulse adapter
            processed += 1
            h.clock.advance(0.0006)
            if processed == 500:
                halfway.wait()
                assert proceed.wait(12)
        return read(values, valid)

    def at_wait(timeout: float | None = None) -> bool:
        assert timeout == pytest.approx(0.5)
        waiting.wait()
        return wait(12)  # Scheduling watchdog only. Request expiry uses the virtual clock.

    h.patch.setattr(check._Properties, "read", classmethod(counted))
    h.patch.setattr(h.checker._condition, "wait", at_wait)
    with ThreadPoolExecutor(max_workers=2) as pool:
        try:
            dense_result = pool.submit(h.run)
            halfway.wait()
            start = pool.submit(h.run, check.Purpose.START, anchor=shown.anchor)
            waiting.wait()
            proceed.set()
            result = dense_result.result(timeout=6)
            assert result.unsettled is check.Unsettled.BUDGET and result.target is None
            assert_offer(start.result(timeout=6), variant1=True)
        finally:
            proceed.set()
            with h.checker._condition:
                h.checker._condition.notify_all()
    assert 500 < processed < 1000
    assert len(h.ss.calls) == 2  # the dense measurement did not reach ss


@pytest.mark.parametrize(
    ("binary", "valid"),
    [
        ("x" * 255, True),
        ("x" * 256, False),
        ("я" * 127 + "x", True),
        ("я" * 128, False),
    ],
)
def test_binary_limit_uses_utf8_bytes_before_normalization(
    binary: str,
    valid: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def normalize_short(values: dict[str, object]) -> tuple[dict[str, object], bool]:
        assert ("application.process.binary" in values) is valid
        return project_properties(values)

    monkeypatch.setattr(check, "project_properties", normalize_short)
    props_result = check._Properties.read(props(binary=binary), True)
    assert props_result.valid is valid and props_result.pid == PID
    assert props_result.binary == (binary if valid else None)


def test_rights_calculation_checks_deadline_before_consuming_dense_picture(h: Harness) -> None:
    clients, streams = picture()
    dense = tuple(RawClient(index, props(binary="unknown")) for index in range(1000, 2000))
    h.queue_pictures(((*clients, *dense), streams))
    original = identity.IdentityChecker._check_safe
    examined = 0

    def counted(
        checker: identity.IdentityChecker,
        client: identity.Client,
        *args: Any,
    ) -> identity.Identity:
        nonlocal examined
        if client.index >= 1000:
            examined += 1
            h.clock.advance(0.0006)
        return original(checker, client, *args)

    h.patch.setattr(identity.IdentityChecker, "_check_safe", counted)
    result = h.run()
    assert result.unsettled is check.Unsettled.BUDGET and result.target is None
    assert 500 < examined < 1000 and len(h.ss.calls) == 1
