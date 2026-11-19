"""One synchronous (proc/fd, ss, proc/fd) sample for the on-demand checker."""

from __future__ import annotations

import errno
import io
import math
import os
import re
import socket
import stat
import time
from collections import Counter
from collections.abc import Mapping, Set
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType

from astra_voice.meetings.identity import (
    MAX_CLIENTS,
    MAX_STARTTIME,
    ProcessKey,
    ProcReader,
    ProcUnavailable,
    Reason,
    SocketEvidence,
    SocketFds,
)
from astra_voice.meetings.identity_sockets import SsFailure, SsRunner

# Only an unambiguous row may contribute a socket edge. Names containing spaces
# can obscure numeric columns, so their rows can only invalidate evidence.
_MAX_ROW_BYTES = 1024
_CONTROL_BYTE = re.compile(rb"[\x00-\x1f\x7f]")
_ROW_PREFIX = (
    rb"(u_str|u_dgr|u_seq|u_raw|u_rdm) +"
    rb"(ESTAB|UNCONN|LISTEN|CLOSE|SYN-SENT|SYN-RECV|FIN-WAIT-1|FIN-WAIT-2|"
    rb"TIME-WAIT|CLOSE-WAIT|LAST-ACK|CLOSING|NEW-SYN-RECV|UNKNOWN) +"
    rb"[0-9]{1,20} +[0-9]{1,20} +"
)
_ROW_START = re.compile(_ROW_PREFIX)
_INODE_TOKEN = re.compile(rb"[0-9]{1,20}")
_ROW = re.compile(
    _ROW_PREFIX + rb"([^\x00-\x20\x7f]+) +([0-9]{1,20}) +([^\x00-\x20\x7f]+) +([0-9]{1,20}) *\n"
)


class SsStatus(StrEnum):
    OK = "ok"
    ABSENT = "absent"
    FAILED = "failed"


class SocketInsufficiency(StrEnum):
    FD_UNREADABLE = "fd_unreadable"
    FD_CHANGED = "fd_changed"
    SHARED_CONNECTION = "shared_connection_inode"
    INCOMPLETE_PEER = "incomplete_peer"
    OTHER = "other"


@dataclass(frozen=True)
class SocketMeasurement:
    """Single-use facts implementing ConnectionProbe. No PID or path in repr."""

    status: SsStatus
    reason: Reason
    _counts: Mapping[ProcessKey, int] = field(
        default_factory=lambda: MappingProxyType({}), repr=False
    )
    control_pair: bool | None = None
    server_path_matches: bool | None = None
    _insufficient_by_pid: Mapping[int, SocketInsufficiency] = field(
        default_factory=lambda: MappingProxyType({}), repr=False
    )

    def evidence(self, process: ProcessKey) -> SocketEvidence:
        if self.status is SsStatus.ABSENT:
            return SocketEvidence(reason=Reason.SS_UNAVAILABLE)
        if self.status is SsStatus.FAILED:
            reason = (
                self.reason
                if self.reason in {Reason.SS_TIMEOUT, Reason.SS_LIMIT}
                else (Reason.SS_INSUFFICIENT)
            )
            return SocketEvidence(reason=reason)
        count = self._counts.get(process)
        return SocketEvidence(count, Reason.CONNECTION) if count is not None else SocketEvidence()

    def insufficient_reason(self, process: ProcessKey) -> SocketInsufficiency | None:
        """Diagnostic only. The PID-level reason cannot grant socket evidence."""
        if self.status is not SsStatus.OK:
            return SocketInsufficiency.OTHER
        if process in self._counts:
            return None
        return self._insufficient_by_pid.get(process.pid, SocketInsufficiency.OTHER)


@dataclass(frozen=True)
class _Endpoint:
    kind: bytes
    established: bool
    name: bytes = field(repr=False)
    peer: int = field(repr=False)
    peer_name: bytes = field(repr=False)


def _check_time(deadline: float) -> None:
    if not math.isfinite(deadline) or time.monotonic() >= deadline:
        raise TimeoutError(errno.ETIMEDOUT)


def _control(fd: int) -> tuple[int, bytes]:
    # Duplicate without taking ownership of libpulse's descriptor. Python
    # discovers the actual address family when constructing from this fd.
    duplicate = os.dup(fd)
    try:
        handle = socket.socket(fileno=duplicate)
    except BaseException:
        os.close(duplicate)
        raise
    with handle:
        info = os.fstat(handle.fileno())
        if (
            not stat.S_ISSOCK(info.st_mode)
            or handle.family != socket.AF_UNIX
            or handle.type != socket.SOCK_STREAM
        ):
            raise ValueError()
        peer = os.fsencode(handle.getpeername())
        if info.st_ino <= 0 or not peer.startswith(b"/"):
            raise ValueError()
        return info.st_ino, peer


def _table(
    output: bytearray, server: bytes, deadline: float
) -> tuple[dict[int, _Endpoint], set[int]]:
    rows: dict[int, _Endpoint] = {}
    doubtful: set[int] = set()
    for line in io.BytesIO(output):
        _check_time(deadline)
        if (
            len(line) > _MAX_ROW_BYTES
            or not line.endswith(b"\n")
            or _CONTROL_BYTE.search(line, 0, len(line) - 1)
        ):
            raise ValueError()
        match = _ROW.fullmatch(line)
        if match is None:
            if server in line:
                raise ValueError()
            tokens = line.split()
            # Both names must have at least one token. A local inode must be
            # possible between those names, and a peer inode must end the row.
            if (
                len(tokens) < 8
                or not _ROW_START.match(line)
                or not _INODE_TOKEN.fullmatch(tokens[-1])
                or int(tokens[-1]) > MAX_STARTTIME
                or not any(
                    _INODE_TOKEN.fullmatch(token) and 0 < int(token) <= MAX_STARTTIME
                    for token in tokens[5:-2]
                )
            ):
                raise ValueError()
            doubtful.update(int(token) for token in tokens if token.isdigit())
            continue
        kind, state, name, inode_text, peer_name, peer_text = match.groups()
        inode, peer = int(inode_text), int(peer_text)
        if not 0 < inode <= MAX_STARTTIME or peer > MAX_STARTTIME or inode in rows:
            raise ValueError()
        rows[inode] = _Endpoint(kind, state == b"ESTAB", name, peer, peer_name)
    if not rows:
        raise ValueError()
    return rows, doubtful


def _mutual(table: Mapping[int, _Endpoint], inode: int, row: _Endpoint) -> _Endpoint | None:
    peer = table.get(row.peer)
    if (
        row.established
        and peer is not None
        and peer.established
        and peer.kind == row.kind
        and peer.peer == inode
        and row.peer != inode
    ):
        return peer
    return None


def _graph(
    table: Mapping[int, _Endpoint], control: int, server: bytes, deadline: float
) -> tuple[set[int], set[int]]:
    """Return proven clients of SP and unresolved edges that could reach SP."""
    proven: set[int] = set()
    incomplete: set[int] = set()
    for inode, row in table.items():
        _check_time(deadline)
        if row.kind != b"u_str":
            continue
        if row.name == server:
            if not row.established and row.peer == 0:
                continue
            if row.peer == 0:
                # An accepted server socket whose client is unknown cannot be
                # assigned to any particular candidate.
                raise ValueError()
            if _mutual(table, inode, row) is None:
                incomplete.add(row.peer)
            else:
                proven.add(row.peer)
            continue
        target = table.get(row.peer)
        if target is not None and target.name == server:
            if target.peer != inode:
                incomplete.add(inode)
        elif row.peer_name == server or (
            row.established and target is None and row.peer_name == b"*"
        ):
            # Without a peer row, an unnamed peer cannot be ruled out as SP.
            # An explicitly different peer path is unrelated to the Pulse graph.
            incomplete.add(inode)
    if control not in proven or control in incomplete:
        raise ValueError()
    proven.remove(control)
    return proven, incomplete


def _walk(proc: ProcReader, pids: Set[int], deadline: float) -> dict[int, SocketFds]:
    snapshots: dict[int, SocketFds] = {}
    for pid in pids:
        _check_time(deadline)
        try:
            snapshots[pid] = proc.socket_fds(pid, deadline)
        except ProcUnavailable:
            continue
    return snapshots


def _counts(
    pids: Set[int],
    before: Mapping[int, SocketFds],
    after: Mapping[int, SocketFds],
    proven: set[int],
    incomplete: set[int],
    deadline: float,
) -> tuple[dict[ProcessKey, int], dict[int, SocketInsufficiency]]:
    # Track every observed holder, including partial or changed fd lists.
    # Duplicate descriptors in one PID count as one holder and one connection.
    owners: Counter[int] = Counter()
    for pid in before.keys() | after.keys():
        _check_time(deadline)
        seen: set[int] = set()
        for snapshots in (before, after):
            if (snapshot := snapshots.get(pid)) is not None:
                seen.update(snapshot.descriptors.values())
        seen.discard(0)
        owners.update(seen)
    counts: dict[ProcessKey, int] = {}
    insufficient: dict[int, SocketInsufficiency] = {}
    for pid in pids:
        _check_time(deadline)
        first, second = before.get(pid), after.get(pid)
        if first is None or second is None or not first.complete or not second.complete:
            insufficient[pid] = SocketInsufficiency.FD_UNREADABLE
            continue
        if first != second:
            insufficient[pid] = SocketInsufficiency.FD_CHANGED
            continue
        inodes = set(first.descriptors.values())
        inodes.discard(0)
        connections = inodes & proven
        if inodes & incomplete:
            insufficient[pid] = SocketInsufficiency.INCOMPLETE_PEER
        elif any(owners[inode] > 1 for inode in connections):
            insufficient[pid] = SocketInsufficiency.SHARED_CONNECTION
        else:
            counts[first.key] = len(connections)
    return counts, insufficient


class IdentitySocketProbe:
    """Reusable child owner; each measure call returns independent evidence.

    Pass the full set of candidate PIDs, the live AF_UNIX fd of the short
    introspection context, and an absolute time.monotonic() deadline. Call
    from the role's check worker. Keep the same instance until shutdown so a
    child that could not be reaped by the deadline blocks further launches.
    """

    def __init__(self, proc: ProcReader | None = None, *, runner: SsRunner | None = None) -> None:
        self._proc = proc if proc is not None else ProcReader()
        self._runner = runner if runner is not None else SsRunner()

    def measure(self, pids: Set[int], control_fd: int, deadline: float) -> SocketMeasurement:
        control_pair = None
        path_matches = None
        try:
            _check_time(deadline)
            if len(pids) > MAX_CLIENTS:
                raise ValueError()
            control = _control(control_fd)
            before = _walk(self._proc, pids, deadline)
            output = self._runner.run(deadline)
            after = _walk(self._proc, pids, deadline)
            if _control(control_fd) != control:
                raise ValueError()
            inode, server = control
            table, doubtful = _table(output, server, deadline)
            endpoint = table.get(inode)
            if inode in doubtful or (endpoint is not None and endpoint.peer in doubtful):
                raise ValueError()
            peer = _mutual(table, inode, endpoint) if endpoint is not None else None
            control_pair = peer is not None
            path_matches = peer.name == server if peer is not None else None
            if not path_matches:
                raise ValueError()
            proven, incomplete = _graph(table, inode, server, deadline)
            incomplete.update(doubtful)
            counts, insufficient = _counts(pids, before, after, proven, incomplete, deadline)
            _check_time(deadline)
            return SocketMeasurement(
                SsStatus.OK,
                Reason.CONNECTION,
                MappingProxyType(counts),
                control_pair,
                path_matches,
                _insufficient_by_pid=MappingProxyType(insufficient),
            )
        except SsFailure as exc:
            if exc.reason is Reason.SS_UNAVAILABLE:
                return SocketMeasurement(SsStatus.ABSENT, exc.reason)
            return SocketMeasurement(SsStatus.FAILED, exc.reason)
        except TimeoutError:
            return SocketMeasurement(
                SsStatus.FAILED,
                Reason.SS_TIMEOUT,
                control_pair=control_pair,
                server_path_matches=path_matches,
            )
        except (OSError, ValueError, MemoryError, OverflowError):
            return SocketMeasurement(
                SsStatus.FAILED,
                Reason.SS_INSUFFICIENT,
                control_pair=control_pair,
                server_path_matches=path_matches,
            )

    def shutdown(self, deadline: float) -> bool:
        return self._runner.shutdown(deadline)
