"""Fresh Pulse identity and private target selection for meeting-capture."""

from __future__ import annotations

import math
import os
import socket
import threading
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from contextlib import nullcontext
from dataclasses import dataclass, field, replace
from enum import StrEnum

from astra_voice.meetings.apps import AppClass, AppDataError, AppDefinition, load_apps
from astra_voice.meetings.identity import (
    SOCKET_UNCERTAINTY,
    Client,
    ConnectionProbe,
    IdentityChecker,
    ProcessFacts,
    ProcessGone,
    ProcessKey,
    ProcessReused,
    ProcReader,
    ProcUnavailable,
    Reason,
)
from astra_voice.meetings.identity_sockets import SsRunner
from astra_voice.meetings.identity_ss import IdentitySocketProbe, SocketMeasurement, SsStatus
from astra_voice.worker.meeting_audio_local import local_server_address as local_server_address
from astra_voice.worker.meeting_detector import (
    INVALID_INDEX,
    Direction,
    MetadataOpenFailed,
    ProgramKey,
    RawClient,
    RawStream,
    check_picture_size,
    project_properties,
)
from astra_voice.worker.meeting_metadata import PulseMetadata
from astra_voice.worker.monitor_stream import MAX_FAR_STREAMS

_PID = "application.process.id"
_BINARY = "application.process.binary"
_SERIAL = "object.serial"
CONTEXT_NAME = b"astra-voice-meeting-identity"
CHECK_SECONDS = 0.5
RETRY_MIN_SECONDS = 0.25
SS_CAPACITY = 6
SS_START_RESERVE = 2
SS_REFILL_SECONDS = 5.0
BINARY_LIMIT = 255


def _no_checkpoint() -> None:
    pass


class Purpose(StrEnum):
    OFFER = "offer"
    CARD = "card"
    START = "start"


class Unsettled(StrEnum):
    PICTURE_CHANGED = "picture_changed"
    PULSE_FAILED = "pulse_failed"
    BUDGET = "budget"
    BUSY = "busy"
    RATE = "rate"
    REMOTE = "remote"


class TargetError(StrEnum):
    GONE = "target-gone"
    MISMATCH = "target-mismatch"


class VerificationCode(StrEnum):
    FAILED = "verification=failed"
    UNAVAILABLE = "verification=unavailable"


JournalCode = Reason | VerificationCode | Unsettled


@dataclass(frozen=True, repr=False)
class _Member:
    direction: Direction
    index: int
    serial: int | None
    client: int
    client_serial: int | None
    process: ProcessKey

    def pin(self) -> PinnedStream | None:
        if self.direction is Direction.OUTPUT and self.serial is not None:
            return PinnedStream(
                self.index, self.serial, self.client, self.client_serial, self.process
            )
        return None


@dataclass(frozen=True, repr=False)
class CallAnchor:
    program: ProgramKey
    server_cookie: int
    processes: frozenset[ProcessKey]
    members: frozenset[_Member] = field(repr=False)
    ambiguous: bool = field(repr=False)


@dataclass(frozen=True)
class CheckRequest:
    purpose: Purpose
    program: ProgramKey | None = field(repr=False)
    anchor: CallAnchor | None = field(repr=False)
    deadline: float


@dataclass(frozen=True, repr=False)
class PinnedStream:
    sink_input: int
    stream_serial: int
    client: int
    client_serial: int | None
    process: ProcessKey


@dataclass(frozen=True, repr=False)
class PinnedTarget:
    server_cookie: int
    streams: tuple[PinnedStream, ...]
    measured_at: float


@dataclass(frozen=True)
class CheckResult:
    settled: bool
    unsettled: Unsettled | None
    app_class: AppClass
    offer: bool
    variant1: bool
    journal: tuple[JournalCode, ...]
    measured_mono_ms: int
    attempts: int
    anchor: CallAnchor | None = field(repr=False)
    target: PinnedTarget | None = field(repr=False)
    target_error: TargetError | None = None


@dataclass(frozen=True, repr=False)
class OwnContexts:
    """One consistent view from the role. The version changes on every update."""

    version: int
    indexes: frozenset[int] = frozenset()
    server_cookie: int | None = None

    def on_server(self, cookie: int) -> frozenset[int]:
        return self.indexes if self.server_cookie == cookie else frozenset()


@dataclass(frozen=True, repr=False)
class _Properties:
    pid: int | None
    binary: str | None
    serial: int | None
    valid: bool

    @classmethod
    def read(cls, values: Mapping[str, object], valid: bool) -> _Properties:
        name = values.get(_BINARY)
        if isinstance(name, str):
            try:
                short = len(name) <= BINARY_LIMIT and len(name.encode()) <= BINARY_LIMIT
            except UnicodeError:
                short = False
            if not short:
                values = {_PID: values.get(_PID), _SERIAL: values.get(_SERIAL)}
                valid = False
        props, normalized = project_properties(values)
        pid, binary, serial = props.get(_PID), props.get(_BINARY), props.get(_SERIAL)
        return cls(
            pid if type(pid) is int else None,
            binary if isinstance(binary, str) else None,
            serial if type(serial) is int else None,
            valid and normalized,
        )

    def declarations(self) -> dict[str, object]:
        return {_PID: self.pid, _BINARY: self.binary}


@dataclass(frozen=True, repr=False)
class _ClientRow:
    index: int
    properties: _Properties


@dataclass(frozen=True, repr=False)
class _StreamRow:
    direction: Direction
    index: int
    client: int
    properties: _Properties


@dataclass(frozen=True, repr=False)
class _Projection:
    clients: tuple[_ClientRow, ...]
    streams: tuple[_StreamRow, ...]
    valid: bool

    def pids(self) -> frozenset[int]:
        rows: tuple[_ClientRow | _StreamRow, ...] = (*self.clients, *self.streams)
        return frozenset(row.properties.pid for row in rows if row.properties.pid is not None)


Picture = tuple[tuple[RawClient, ...], tuple[RawStream, ...]]


def _project(
    picture: Picture,
    own: frozenset[int],
    checkpoint: Callable[[], None] = _no_checkpoint,
) -> _Projection:
    """Pi uses only the white-listed fields and is independent of list order."""
    clients, streams = picture
    check_picture_size(len(clients) + len(streams))
    client_rows: list[_ClientRow] = []
    for client in sorted(clients, key=lambda row: row.index):
        checkpoint()
        if client.index not in own:
            client_rows.append(
                _ClientRow(client.index, _Properties.read(client.properties, client.valid))
            )
    stream_rows: list[_StreamRow] = []
    for stream in sorted(streams, key=lambda row: (row.direction, row.index)):
        checkpoint()
        if stream.client not in own and stream.client != INVALID_INDEX:
            stream_rows.append(
                _StreamRow(
                    stream.direction,
                    stream.index,
                    stream.client,
                    _Properties.read(stream.properties, stream.valid),
                )
            )
    indexes = {row.index for row in client_rows}
    valid = (
        len(indexes) == len(client_rows)
        and all(0 <= index < INVALID_INDEX for index in indexes)
        and len({(row.direction, row.index) for row in stream_rows}) == len(stream_rows)
        and all(row.client in indexes and 0 <= row.index < INVALID_INDEX for row in stream_rows)
    )
    return _Projection(tuple(client_rows), tuple(stream_rows), valid)


_Reading = ProcessFacts | type[ProcUnavailable]


@dataclass(frozen=True, repr=False)
class _FrozenProc:
    before: Mapping[int, _Reading]
    after: Mapping[int, _Reading]

    def snapshot(self, pid: int) -> ProcessFacts:
        first = self.before.get(pid, ProcUnavailable)
        second = self.after.get(pid, ProcUnavailable)
        if not isinstance(first, ProcessFacts):
            raise first()
        if not isinstance(second, ProcessFacts):
            raise second()
        if first != second:
            raise ProcessReused()
        return first


@dataclass(frozen=True, repr=False)
class _Participant:
    client: _ClientRow
    stream: _StreamRow
    member: _Member


@dataclass(repr=False)
class _Call:
    program: ProgramKey
    app_class: AppClass
    participants: set[_Participant] = field(default_factory=set)
    outputs: list[PinnedStream] = field(default_factory=list)
    declared_pids: set[int | None] = field(default_factory=set)
    declared_outputs: int = 0

    @property
    def members(self) -> frozenset[_Member]:
        return frozenset(participant.member for participant in self.participants)

    @property
    def active(self) -> bool:
        return _has_call(self.members)

    def unambiguous(self, limit: int) -> bool:
        # Each PID has exactly one frozen ProcessKey in this measurement.
        # Unconfirmed declarations by other stream holders also prevent a split.
        members = self.members
        pids = self.declared_pids | {member.process.pid for member in members}
        outputs = _outputs(members)
        return (
            len(pids) == 1
            and 0 < self.declared_outputs == len(outputs) <= limit
            and all(member.serial is not None for member in outputs)
        )

    def pinnable(self, limit: int) -> bool:
        return self.unambiguous(limit) and len(self.outputs) == self.declared_outputs


def _outputs(members: frozenset[_Member]) -> frozenset[_Member]:
    return frozenset(member for member in members if member.direction is Direction.OUTPUT)


def _has_call(members: frozenset[_Member]) -> bool:
    return {member.direction for member in members} == {Direction.OUTPUT, Direction.MICROPHONE}


def _resolve(
    projection: _Projection,
    apps: Sequence[AppDefinition],
    proc: _FrozenProc,
    uid: int,
    sample: ConnectionProbe,
    checkpoint: Callable[[], None] = _no_checkpoint,
) -> tuple[dict[ProgramKey, _Call], dict[ProgramKey, set[JournalCode]]]:
    # Supply every claimant, including unattached clients and both declarations
    # of a conflicting stream. IdentityChecker counts each client once per PID.
    clients = {row.index: row for row in projection.clients}
    rows = [Client(row.index, row.properties.declarations()) for row in projection.clients]
    attached = [stream for stream in projection.streams if stream.client in clients]
    attached_indexes = {stream.client for stream in attached}
    pids: dict[int, set[int]] = {row.index: set() for row in projection.clients}
    binaries: dict[int, set[str]] = {row.index: set() for row in projection.clients}
    for stream in attached:
        checkpoint()
        rows.append(
            Client(
                stream.client,
                clients[stream.client].properties.declarations(),
                stream.properties.declarations(),
            )
        )
    catalog = {binary: ProgramKey(app.id, binary) for app in apps for binary in app.binary}
    classes = {app.id: app.app_class for app in apps}
    checker = IdentityChecker(apps, proc, uid=uid, connections=sample)
    identities = checker.check(rows, checkpoint=checkpoint)
    calls: dict[ProgramKey, _Call] = {}
    codes: dict[ProgramKey, set[JournalCode]] = {}
    client_count = len(projection.clients)
    for index, (row, identity) in enumerate(zip(rows, identities, strict=True)):
        checkpoint()
        row_pids: set[int] = set()
        programs: set[ProgramKey] = set()
        for properties in (row.properties, row.stream_properties):
            pid, binary = properties.get(_PID), properties.get(_BINARY)
            if type(pid) is int:
                row_pids.add(pid)
            if isinstance(binary, str):
                binaries[row.index].add(binary)
                if binary in catalog:
                    programs.add(catalog[binary])
        pids[row.index].update(row_pids)
        if index >= client_count:
            stream = attached[index - client_count]
            for program in programs:
                call = calls.setdefault(program, _Call(program, classes[program.app_id]))
                call.declared_pids.update(row_pids or {None})
                call.declared_outputs += stream.direction is Direction.OUTPUT
        elif row.index in attached_indexes:
            # Attached clients contribute evidence to the counter above. The
            # effective identity for the journal comes from their merged rows.
            continue
        for program in programs:
            codes.setdefault(program, set()).add(identity.reason)
            if identity.reason is Reason.SS_UNAVAILABLE:
                codes[program].add(VerificationCode.UNAVAILABLE)
            elif identity.reason in SOCKET_UNCERTAINTY:
                codes[program].add(VerificationCode.FAILED)

    client_indexes = Counter(row.index for row in projection.clients)
    stream_indexes = Counter((row.direction, row.index) for row in projection.streams)
    for stream, identity in zip(attached, identities[client_count:], strict=True):
        checkpoint()
        client = clients[stream.client]
        left, right = client.properties, stream.properties
        binary = left.binary if left.binary is not None else right.binary
        pid = left.pid if left.pid is not None else right.pid
        if (
            client_indexes[stream.client] != 1
            or stream_indexes[stream.direction, stream.index] != 1
            or not 0 <= stream.index < INVALID_INDEX
            or not left.valid
            or not right.valid
            or not identity.offer_allowed
            or binary is None
            or pid is None
            or binary not in catalog
        ):
            continue
        call = calls[catalog[binary]]
        member = _Member(
            stream.direction,
            stream.index,
            right.serial,
            client.index,
            left.serial,
            proc.snapshot(pid).key,
        )
        call.participants.add(_Participant(client, stream, member))
        has_conflict = len(pids[client.index]) > 1 or len(binaries[client.index]) > 1
        if identity.variant1_allowed and not has_conflict and (pinned := member.pin()) is not None:
            call.outputs.append(pinned)
    return calls, codes


def _instance_observed(anchor: CallAnchor, picture: _Projection) -> bool:
    # Inspect the raw projection before rights filtering. A readable Pulse
    # object can survive a failed /proc read while a different call qualifies.
    observed = {
        (stream.direction, stream.index, stream.properties.serial, stream.client)
        for stream in picture.streams
    }
    surviving = frozenset(
        member
        for member in anchor.members
        if (member.direction, member.index, member.serial, member.client) in observed
    )
    if not surviving:
        return False
    if any(member.direction is Direction.MICROPHONE for member in surviving):
        return True
    pids = {key.pid for key in anchor.processes}
    clients = {row.index: row.properties for row in picture.clients}
    return any(
        stream.direction is Direction.MICROPHONE
        and (
            stream.properties.pid in pids
            or (stream.client in clients and clients[stream.client].pid in pids)
        )
        for stream in picture.streams
    )


def _check_anchor(
    anchor: CallAnchor,
    call: _Call | None,
    picture: _Projection,
    limit: int,
) -> TargetError | None:
    if anchor.ambiguous:
        return TargetError.MISMATCH
    if not _instance_observed(anchor, picture):
        return TargetError.GONE
    if call is None or not call.pinnable(limit):
        return TargetError.MISMATCH
    if _outputs(call.members) != _outputs(anchor.members):
        return TargetError.MISMATCH
    if not any(
        member.direction is Direction.MICROPHONE and member.process in anchor.processes
        for member in call.members
    ):
        return TargetError.MISMATCH
    return None


def _failure(code: Unsettled, attempts: int = 0) -> CheckResult:
    return CheckResult(False, code, AppClass.OTHER, False, False, (code,), 0, attempts, None, None)


@dataclass(repr=False)
class _Decision:
    calls: dict[ProgramKey, _Call]
    codes: dict[ProgramKey, set[JournalCode]]
    picture: _Projection
    changed: bool
    cookie: int
    measured_at: float
    attempts: int
    output_limit: int


def _analyse(
    apps: Sequence[AppDefinition],
    before: _Projection,
    after: _Projection,
    proc: _FrozenProc,
    uid: int,
    sample: ConnectionProbe,
    *,
    version_changed: bool,
    cookie: int,
    measured_at: float,
    attempts: int,
    output_limit: int,
    checkpoint: Callable[[], None] = _no_checkpoint,
) -> _Decision:
    changed = before != after or version_changed or not before.valid or not after.valid
    calls, codes = _resolve(before, apps, proc, uid, sample, checkpoint)
    if changed:
        latest, _ = _resolve(after, apps, proc, uid, sample, checkpoint)
        calls = {
            program: call
            for program, call in calls.items()
            if program in latest and call.participants == latest[program].participants
        }
    checkpoint()
    return _Decision(calls, codes, after, changed, cookie, measured_at, attempts, output_limit)


def _decide(request: CheckRequest, decision: _Decision) -> CheckResult:
    anchor = request.anchor if request.purpose is Purpose.START else None
    wanted = anchor.program if anchor is not None else request.program
    active = [
        call
        for program, call in decision.calls.items()
        if (wanted is None or program == wanted) and call.active
    ]
    call = next(
        (call for call in active if call.pinnable(decision.output_limit)),
        active[0] if active else None,
    )
    target_error = None
    if request.purpose is Purpose.START:
        if anchor is None or decision.cookie != anchor.server_cookie:
            target_error = TargetError.GONE
        elif decision.changed or (
            request.program is not None and request.program != anchor.program
        ):
            target_error = TargetError.MISMATCH
        else:
            target_error = _check_anchor(anchor, call, decision.picture, decision.output_limit)
    offer = call is not None and target_error is None
    out_anchor = None
    target = None
    if offer and call is not None:
        members = call.members
        out_anchor = CallAnchor(
            call.program,
            decision.cookie,
            frozenset(member.process for member in members),
            members,
            decision.changed or not call.unambiguous(decision.output_limit),
        )
        if not decision.changed and call.pinnable(decision.output_limit):
            target = PinnedTarget(decision.cookie, tuple(call.outputs), decision.measured_at)
    if decision.changed:
        codes: set[JournalCode] = {Unsettled.PICTURE_CHANGED}
    elif wanted is not None:
        codes = decision.codes.get(wanted, set())
    else:
        codes = set().union(*decision.codes.values())
    return CheckResult(
        not decision.changed,
        Unsettled.PICTURE_CHANGED if decision.changed else None,
        call.app_class if call is not None else AppClass.OTHER,
        offer,
        target is not None,
        tuple(sorted(codes)),
        int(decision.measured_at * 1000),
        decision.attempts,
        out_anchor,
        target,
        target_error,
    )


def _dup_control_socket(fd: int, inode: int) -> socket.socket:
    duplicate = os.dup(fd)
    try:
        if os.fstat(duplicate).st_ino != inode:
            raise OSError()
        return socket.socket(fileno=duplicate)
    except BaseException:
        os.close(duplicate)
        raise


def _control_socket(
    before: dict[int, int] | None,
    after: dict[int, int] | None,
) -> socket.socket | None:
    if before is None or after is None:
        return None
    added = [(fd, inode) for fd, inode in after.items() if before.get(fd) != inode]
    if len(added) == 1:
        try:
            return _dup_control_socket(*added[0])
        except OSError:
            return None
    return None


@dataclass(repr=False)
class _Job:
    request: CheckRequest
    result: _Decision | CheckResult | None = None


class IdentityCheck:
    """One role-owned checker. Admitted callers perform their work synchronously.

    At most one check runs at a time. Compatible callers can join a pending
    OFFER or CARD. They share that check within their own arrival deadlines.
    """

    def __init__(
        self,
        server: bytes | None,
        *,
        apps: Sequence[AppDefinition] | None = None,
        own_contexts: Callable[[], OwnContexts] = lambda: OwnContexts(0),
        output_limit: int = 1,
        clock: Callable[[], float] = time.monotonic,
        proc: ProcReader | None = None,
        metadata: Callable[..., PulseMetadata] = PulseMetadata,
    ) -> None:
        if not 1 <= output_limit <= MAX_FAR_STREAMS:
            raise ValueError("invalid output limit")
        if apps is None:
            try:
                apps = load_apps()
            except AppDataError:
                apps = ()
        self._apps = tuple(apps)
        self._server = local_server_address(server)
        self._own_contexts = own_contexts
        self._output_limit = output_limit
        self._clock = clock
        self._proc = proc if proc is not None else ProcReader()
        self._probe = IdentitySocketProbe(
            self._proc, runner=SsRunner(on_launch=self._charge_launch)
        )
        self._metadata = metadata
        self._closing: PulseMetadata | None = None
        self._condition = threading.Condition()
        self._active = False
        self._stopped = False
        self._pending: dict[Purpose, _Job] = {}
        self._tokens = SS_CAPACITY
        self._refilled_at = clock()

    def _refill(self) -> None:
        now = self._clock()
        added = int((now - self._refilled_at) // SS_REFILL_SECONDS)
        self._tokens = min(SS_CAPACITY, self._tokens + added)
        self._refilled_at = (
            now if self._tokens == SS_CAPACITY else self._refilled_at + added * SS_REFILL_SECONDS
        )

    def _can_launch(self, purpose: Purpose) -> bool:
        self._refill()
        floor = 0 if purpose is Purpose.START else SS_START_RESERVE
        return self._tokens > floor

    def _charge_launch(self) -> None:
        # Called with this checker active, just before Popen, including Popen failures.
        self._refill()
        self._tokens -= 1

    def _require_time(self, deadline: float) -> None:
        if self._clock() >= deadline:
            raise TimeoutError()

    def _own_fds(self, deadline: float) -> dict[int, int] | None:
        self._require_time(deadline)
        try:
            sample = self._proc.socket_fds(os.getpid(), deadline)
        except (ProcUnavailable, OSError) as error:
            if isinstance(error, TimeoutError):
                raise
            return None
        return (
            {fd: inode for fd, inode in sample.descriptors.items() if inode}
            if sample.complete
            else None
        )

    def _read_processes(self, pids: frozenset[int], deadline: float) -> dict[int, _Reading]:
        facts: dict[int, _Reading] = {}
        for pid in sorted(pids):
            self._require_time(deadline)
            try:
                facts[pid] = self._proc.snapshot(pid)
            except (ProcessGone, ProcessReused) as error:
                facts[pid] = type(error)
            except TimeoutError:
                raise
            except (ProcUnavailable, OSError):
                facts[pid] = ProcUnavailable
        return facts

    def _finish_close(self) -> bool:
        if self._closing is not None:
            try:
                self._closing.close()
            except Exception:
                return False
            self._closing = None
        return True

    def _measure(self, request: CheckRequest, attempts: int) -> _Decision | CheckResult:
        deadline = request.deadline

        def checkpoint() -> None:
            self._require_time(deadline)

        own_before = self._own_contexts()
        old_fds = self._own_fds(deadline)
        self._require_time(deadline)
        try:
            pulse = self._metadata(
                server=self._server,
                subscribe=False,
                deadline=deadline,
                context_name=CONTEXT_NAME,
                binary_limit=BINARY_LIMIT,
            )
        except MetadataOpenFailed as error:
            # Keep the backend for a later close retry, never open a second one.
            if isinstance(error.backend, PulseMetadata):
                self._closing = error.backend
            raise
        self._closing = pulse
        remote = False
        try:
            control = _control_socket(old_fds, self._own_fds(deadline))
            with control if control is not None else nullcontext():
                remote = control is not None and control.family != socket.AF_UNIX
                if remote:
                    return _failure(Unsettled.REMOTE, attempts)
                cookie = pulse.server_cookie()
                before = _project(
                    pulse.snapshot(), own_before.on_server(cookie) | {pulse.own_index}, checkpoint
                )
                pids = before.pids()
                r1 = self._read_processes(pids, deadline)
                self._require_time(deadline)
                sample = (
                    self._probe.measure(pids, control.fileno(), deadline)
                    if control is not None
                    else SocketMeasurement(SsStatus.FAILED, Reason.SS_INSUFFICIENT)
                )
            self._require_time(deadline)
            r2 = self._read_processes(pids, deadline)
            raw_after = pulse.snapshot()
            measured_at = self._clock()
        finally:
            if not self._finish_close() and not remote:
                # The outer boundary publishes only a fixed code.
                raise RuntimeError("metadata close failed") from None
        self._require_time(deadline)
        own_after = self._own_contexts()
        after = _project(raw_after, own_after.on_server(cookie) | {pulse.own_index}, checkpoint)
        return _analyse(
            self._apps,
            before,
            after,
            _FrozenProc(r1, r2),
            self._proc.uid,
            sample,
            version_changed=own_before != own_after,
            cookie=cookie,
            measured_at=measured_at,
            attempts=attempts,
            output_limit=self._output_limit,
            checkpoint=checkpoint,
        )

    def _attempt(self, request: CheckRequest, attempts: int) -> _Decision | CheckResult:
        try:
            result = self._measure(request, attempts)
        except Exception:
            result = _failure(Unsettled.PULSE_FAILED, attempts)
        if self._clock() >= request.deadline:
            return _failure(Unsettled.BUDGET, attempts)
        return result

    def _execute(self, request: CheckRequest) -> _Decision | CheckResult:
        if not self._finish_close():
            return _failure(Unsettled.PULSE_FAILED)
        if self._clock() >= request.deadline:
            return _failure(Unsettled.BUDGET)
        if not self._can_launch(request.purpose):
            return _failure(Unsettled.RATE)
        result = self._attempt(request, 1)
        if (
            isinstance(result, _Decision)
            and result.changed
            and self._clock() <= request.deadline - RETRY_MIN_SECONDS
            and self._can_launch(request.purpose)
        ):
            result = self._attempt(request, 2)
        return result

    def _prune_pending(self, now: float) -> None:
        for purpose, job in tuple(self._pending.items()):
            if self._stopped or job.request.deadline <= now:
                job.result = _failure(Unsettled.BUSY)
                del self._pending[purpose]
                self._condition.notify_all()

    def _admit(self, request: CheckRequest) -> _Job | _Decision | CheckResult:
        """Called with the condition lock. Return a job only to its executor."""
        now = self._clock()
        self._prune_pending(now)
        if not math.isfinite(request.deadline) or now >= request.deadline:
            return _failure(Unsettled.BUDGET)
        if self._stopped:
            return _failure(Unsettled.BUSY)
        if not self._active and not self._pending:
            self._active = True
            return _Job(request)

        purpose = request.purpose
        job = self._pending.get(purpose)
        leader = job is None
        if job is None:
            job = self._pending[purpose] = _Job(request)
        elif purpose is Purpose.CARD:
            if request.deadline > job.request.deadline:
                job.request = replace(job.request, deadline=request.deadline)
        elif purpose is Purpose.START or job.request.program != request.program:
            return _failure(Unsettled.BUSY)

        while True:
            now = self._clock()
            self._prune_pending(now)
            if now >= request.deadline:
                return _failure(Unsettled.BUSY)
            if job.result is not None:
                return job.result
            first = self._pending.get(Purpose.START) or next(iter(self._pending.values()), None)
            if (leader or purpose is Purpose.CARD) and not self._active and first is job:
                del self._pending[purpose]
                self._active = True
                return job
            self._condition.wait(timeout=request.deadline - now)

    def _run_job(self, job: _Job) -> _Decision | CheckResult:
        result: _Decision | CheckResult = _failure(Unsettled.PULSE_FAILED)
        try:
            result = self._execute(job.request)
        finally:
            with self._condition:
                if self._stopped:
                    result = _failure(Unsettled.BUSY, result.attempts)
                job.result = result
                self._active = False
                self._condition.notify_all()
        return result

    def run(self, request: CheckRequest) -> CheckResult:
        if self._server is None:
            outcome: _Decision | CheckResult = _failure(Unsettled.REMOTE)
        else:
            with self._condition:
                admission = self._admit(request)
            outcome = self._run_job(admission) if isinstance(admission, _Job) else admission

        if isinstance(outcome, _Decision):
            result = _decide(request, outcome)
            if self._clock() >= request.deadline:
                result = _failure(Unsettled.BUDGET, result.attempts)
        else:
            result = outcome
        if request.purpose is Purpose.START and not result.variant1 and result.target_error is None:
            return replace(result, target_error=TargetError.MISMATCH)
        return result

    def shutdown(self, deadline: float) -> bool:
        """Stop admission and reap owned resources. Retry if a check is still active."""
        with self._condition:
            self._stopped = True
            self._prune_pending(self._clock())
            if self._active:
                return False
            self._active = True
        try:
            closed = self._finish_close()
            return self._probe.shutdown(deadline) and closed
        finally:
            with self._condition:
                self._active = False
