"""T1 §4.0/MN-9 identity, independent of audio, subscriptions and call policy.

Inputs are normalized client/stream properties from ONE server generation.
Only fixed enums leave this module; application.name/media.name are never used.
Call forget() on client removal and reset() on server reconnect. Own context
indexes must cover all our pa_context instances in this server generation.
Socket evidence is optional and injected (see identity_sockets); unavailable
checks are not negative evidence. No process or sound service is started here.
"""

from __future__ import annotations

import errno
import os
import re
import unicodedata
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from astra_voice.meetings.apps import AppClass, AppDefinition

PROC_LIMIT = 64 * 1024
MAX_CLIENTS = 4096
MAX_PROPERTIES = 128
MAX_PROPERTY_BYTES = 4096
MAX_PID = 2**31 - 1
MAX_STARTTIME = 2**64 - 1


class Verdict(StrEnum):
    VERIFIED = "verified"
    DECLARED = "declared"
    MISMATCH = "mismatch"
    OWN = "own"


class Reason(StrEnum):
    NATIVE = "native_credentials"
    CONNECTION = "pulse_connection"
    OWN = "own_context"
    OWN_NATIVE = "own_native_process"
    UNKNOWN = "unknown_program"
    DECLARED_ONLY = "catalog_declared_only"
    UNAVAILABLE = "proc_unavailable"
    LIMIT = "identity_limit"
    PROPERTIES = "properties_unavailable"
    PID = "invalid_pid"
    DEAD = "process_gone"
    UID = "foreign_uid"
    EXE = "executable_mismatch"
    BINARY = "basename_mismatch"
    REUSED = "pid_reused"
    CONFLICT = "property_conflict"
    SS_UNAVAILABLE = "ss_unavailable"
    SS_PENDING = "ss_pending"
    SS_TIMEOUT = "ss_timeout"
    SS_LIMIT = "ss_output_limit"
    SS_STALE = "ss_stale"
    SS_GENERATION = "ss_process_generation"
    NO_CONNECTION = "pulse_connection_absent"
    COUNTER = "identity_mismatch_counter"


@dataclass(frozen=True)
class Identity:
    verdict: Verdict
    app_class: AppClass = AppClass.OTHER
    reason: Reason = Reason.UNKNOWN

    @property
    def offer_allowed(self) -> bool:
        """Counter ambiguity still permits an offer (owner decision 18.11)."""
        return self.app_class is not AppClass.OTHER and (
            self.verdict in {Verdict.VERIFIED, Verdict.DECLARED} or self.reason is Reason.COUNTER
        )

    @property
    def variant1_allowed(self) -> bool:
        return self.offer_allowed and self.verdict in {Verdict.VERIFIED, Verdict.DECLARED}


@dataclass(frozen=True)
class ProcessKey:
    pid: int = field(repr=False)
    starttime: int = field(repr=False)


@dataclass(frozen=True)
class ProcessFacts:
    key: ProcessKey
    uids: tuple[int, int, int, int]
    exe: str = field(repr=False)


class ProcUnavailable(Exception):
    """No readable evidence; the message contains no external data."""


class ProcessGone(ProcUnavailable):
    pass


class ProcessReused(ProcUnavailable):
    pass


class IdentityLimit(ProcUnavailable):
    pass


class ProcAccess(Protocol):
    def snapshot(self, pid: int) -> ProcessFacts: ...


class ProcReader:
    """Bounded /proc reader with injectable root, never opening the exe target.

    Pin the PID directory and bracket status/readlink with stat.starttime.
    O_NOFOLLOW applies to root, PID directory, status and stat; only exe is
    readlink'ed. ENOENT/ESRCH inside a pinned PID directory is negative evidence
    of disappearance. Permission/MAC errors remain unavailable.
    """

    def __init__(self, root: Path = Path("/proc"), *, uid: int | None = None) -> None:
        self.root = root
        self.uid = os.getuid() if uid is None else uid

    @staticmethod
    def _read(directory: int, name: str) -> bytes:
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(descriptor, "rb") as stream:
            data = stream.read(PROC_LIMIT + 1)
        if len(data) > PROC_LIMIT:
            raise IdentityLimit()
        return data

    @classmethod
    def _starttime(cls, directory: int, pid: int) -> int:
        data = cls._read(directory, "stat")
        # comm can itself contain spaces and ')' (and newlines).
        prefix, separator, suffix = data.rpartition(b") ")
        fields = suffix.split()
        if not separator or not prefix.startswith(str(pid).encode() + b" (") or len(fields) < 20:
            raise ProcUnavailable()
        number = fields[19]
        if not re.fullmatch(rb"[0-9]{1,20}", number) or int(number) > MAX_STARTTIME:
            raise ProcUnavailable()
        return int(number)

    def snapshot(self, pid: int) -> ProcessFacts:
        if type(pid) is not int or not 0 < pid <= MAX_PID:
            raise ProcUnavailable()
        try:
            root = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                try:
                    directory = os.open(
                        str(pid), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root
                    )
                except FileNotFoundError:
                    raise ProcessGone() from None
            finally:
                os.close(root)
            try:
                before = self._starttime(directory, pid)
                data = self._read(directory, "status")
                rows = re.findall(
                    rb"^Uid:[ \t]+([0-9]{1,10})[ \t]+([0-9]{1,10})[ \t]+"
                    rb"([0-9]{1,10})[ \t]+([0-9]{1,10})[ \t]*$",
                    data,
                    re.M,
                )
                if len(rows) != 1:
                    raise ProcUnavailable()
                uids = tuple(int(value) for value in rows[0])
                # Foreign UID is already negative evidence; never inspect its exe.
                exe = (
                    os.readlink("exe", dir_fd=directory) if all(u == self.uid for u in uids) else ""
                )
                if len(exe.encode("utf-8")) > PROC_LIMIT:
                    raise IdentityLimit()
                after = self._starttime(directory, pid)
                if before != after:
                    raise ProcessReused()
                return ProcessFacts(
                    ProcessKey(pid, before), (uids[0], uids[1], uids[2], uids[3]), exe
                )
            except OSError as exc:
                if exc.errno in {errno.ENOENT, errno.ESRCH}:
                    raise ProcessGone() from None
                raise
            finally:
                os.close(directory)
        except OSError:
            raise ProcUnavailable() from None


@dataclass(frozen=True, repr=False)
class Client:
    """Server client index, client properties and one stream's properties.

    Repeat an index for several streams: it is still ONE counter claimant.
    Server credentials/client.api must come from the client, never the stream.
    The dataclass deliberately has no repr containing properties.
    """

    index: int
    properties: Mapping[str, object] = field(repr=False)
    stream_properties: Mapping[str, object] = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class SocketEvidence:
    """Trusted adapter output, sampled for an exact PID generation.

    None = unavailable, zero = checked, no accepted pulse/native connection.
    Own contexts/connections are excluded by the provider before counting.
    """

    connections: int | None = None
    reason: Reason = Reason.SS_UNAVAILABLE


class ConnectionProbe(Protocol):
    def evidence(self, process: ProcessKey) -> SocketEvidence: ...


class UnavailableConnections:
    def evidence(self, process: ProcessKey) -> SocketEvidence:
        return SocketEvidence()


def _pid(value: object) -> int | None:
    if type(value) is int:
        return value if 0 < value <= MAX_PID else None
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,10}", value):
        number = int(value)
        return number if 0 < number <= MAX_PID else None
    return None


def _bounded(properties: Mapping[str, object]) -> bool:
    if len(properties) > MAX_PROPERTIES:
        return False
    for key, value in properties.items():
        if not isinstance(key, str) or len(key) > 120:
            return False
        if isinstance(value, str):
            if len(value) > MAX_PROPERTY_BYTES or len(value.encode("utf-8")) > MAX_PROPERTY_BYTES:
                return False
            if any(unicodedata.category(c) in {"Cc", "Cf", "Cs"} for c in value):
                return False
        elif type(value) not in {int, bool, type(None)}:
            return False
    return True


def _property(client: Client, name: str) -> object:
    left = client.properties.get(name)
    right = client.stream_properties.get(name)
    if left is not None and right is not None and left != right:
        raise ValueError()
    return left if left is not None else right


class IdentityChecker:
    """Synchronous pure decision over bounded injected facts; no ss launch.

    Run proc reads in the metadata worker, not the GUI thread. Limits yield a
    fixed reason, never raw exceptions. The bindings prevent an existing client
    index from silently following a reused PID, including across decisions.
    """

    def __init__(
        self,
        apps: Sequence[AppDefinition],
        proc: ProcAccess,
        *,
        uid: int,
        connections: ConnectionProbe | None = None,
    ) -> None:
        self.apps = tuple(apps)
        self.proc = proc
        self.uid = uid
        self.connections = connections or UnavailableConnections()
        self._bindings: dict[int, ProcessKey] = {}

    def forget(self, client_index: int) -> None:
        self._bindings.pop(client_index, None)

    def reset(self) -> None:
        self._bindings.clear()

    @staticmethod
    def _native(client: Client) -> bool:
        # Missing api (e.g. real PulseAudio) is never authority to trust sec.pid.
        api = client.properties.get("client.api")
        return isinstance(api, str) and bool(api) and api != "pipewire-pulse"

    def check(
        self,
        clients: Sequence[Client],
        *,
        own_client_indexes: frozenset[int] = frozenset(),
        own_native_processes: frozenset[ProcessKey] = frozenset(),
    ) -> tuple[Identity, ...]:
        if len(clients) > MAX_CLIENTS:
            return tuple(Identity(Verdict.DECLARED, reason=Reason.LIMIT) for _ in clients)
        # Count server CLIENTS, not streams, before inspecting identity. Even a
        # failed claimant must not make another stream with that PID verified.
        claims: dict[int, set[int]] = {}
        for client in clients:
            if type(client.index) is not int or not 0 <= client.index < 2**32 - 1:
                return tuple(Identity(Verdict.DECLARED, reason=Reason.PROPERTIES) for _ in clients)
            if client.index in own_client_indexes or self._native(client):
                continue
            for properties in (client.properties, client.stream_properties):
                pid = _pid(properties.get("application.process.id"))
                if pid is not None:
                    claims.setdefault(pid, set()).add(client.index)
        counts = Counter({pid: len(indexes) for pid, indexes in claims.items()})
        return tuple(
            self._check_safe(c, counts, own_client_indexes, own_native_processes) for c in clients
        )

    def _check_safe(
        self,
        client: Client,
        counts: Mapping[int, int],
        own: frozenset[int],
        own_native: frozenset[ProcessKey],
    ) -> Identity:
        if client.index in own:
            return Identity(Verdict.OWN, reason=Reason.OWN)
        try:
            return self._check(client, counts, own_native)
        except IdentityLimit:
            return Identity(Verdict.DECLARED, reason=Reason.LIMIT)
        except Exception:
            # Optional evidence adapters may fail, including malformed /proc.
            # Never format their exceptions or log any input.
            return Identity(Verdict.DECLARED, reason=Reason.UNAVAILABLE)

    def _check(
        self,
        client: Client,
        counts: Mapping[int, int],
        own_native: frozenset[ProcessKey],
    ) -> Identity:
        if type(client.index) is not int or not 0 <= client.index < 2**32 - 1:
            return Identity(Verdict.DECLARED, reason=Reason.PROPERTIES)
        if not _bounded(client.properties) or not _bounded(client.stream_properties):
            return Identity(Verdict.DECLARED, reason=Reason.PROPERTIES)
        native = self._native(client)
        try:
            binary = (
                client.properties.get("application.process.binary")
                if native
                else _property(client, "application.process.binary")
            )
            pid = _pid(
                client.properties.get("pipewire.sec.pid")
                if native
                else _property(client, "application.process.id")
            )
        except ValueError:
            return Identity(Verdict.MISMATCH, reason=Reason.CONFLICT)
        app = next((a for a in self.apps if binary in a.binary), None)
        kind = app.app_class if app else AppClass.OTHER
        if pid is None:
            return Identity(Verdict.MISMATCH, kind, Reason.PID)
        try:
            facts = self.proc.snapshot(pid)
            if (
                facts.key.pid != pid
                or type(facts.key.starttime) is not int
                or not 0 <= facts.key.starttime <= MAX_STARTTIME
                or len(facts.uids) != 4
                or any(type(u) is not int or u < 0 for u in facts.uids)
            ):
                raise ProcUnavailable()
        except ProcessGone:
            return Identity(Verdict.MISMATCH, kind, Reason.DEAD)
        except ProcessReused:
            return Identity(Verdict.MISMATCH, kind, Reason.REUSED)
        except IdentityLimit:
            raise
        except Exception:
            return Identity(Verdict.DECLARED, kind, Reason.UNAVAILABLE)
        if any(uid != self.uid for uid in facts.uids):
            return Identity(Verdict.MISMATCH, kind, Reason.UID)
        previous = self._bindings.get(client.index)
        if previous is not None and previous != facts.key:
            return Identity(Verdict.MISMATCH, kind, Reason.REUSED)
        if previous is None and len(self._bindings) >= MAX_CLIENTS:
            raise IdentityLimit()
        self._bindings[client.index] = facts.key
        if native and facts.key in own_native:
            return Identity(Verdict.OWN, reason=Reason.OWN_NATIVE)
        if native:
            app = next((a for a in self.apps if facts.exe in a.exe), None)
            if app is None:
                basename = facts.exe.rsplit("/", 1)[-1]
                app = next((a for a in self.apps if not a.exe and basename in a.binary), None)
                if app is not None:
                    return Identity(Verdict.DECLARED, app.app_class, Reason.DECLARED_ONLY)
            return (
                Identity(Verdict.VERIFIED, app.app_class, Reason.NATIVE)
                if app
                else Identity(Verdict.MISMATCH, reason=Reason.EXE)
            )
        basename = facts.exe.rsplit("/", 1)[-1]
        if basename != binary:
            return Identity(Verdict.MISMATCH, kind, Reason.BINARY)
        if app is None:
            return Identity(Verdict.MISMATCH, reason=Reason.EXE)
        declared_only = not app.exe
        if not declared_only and facts.exe not in app.exe:
            return Identity(Verdict.MISMATCH, kind, Reason.EXE)
        try:
            evidence = self.connections.evidence(facts.key)
        except ProcessGone:
            return Identity(Verdict.MISMATCH, kind, Reason.DEAD)
        except ProcessReused:
            return Identity(Verdict.MISMATCH, kind, Reason.REUSED)
        except IdentityLimit:
            raise
        except (ProcUnavailable, OSError, ValueError):
            return Identity(Verdict.DECLARED, kind, Reason.SS_UNAVAILABLE)
        # Missing catalog paths prevent promotion, never negative socket checks.
        if evidence.connections is None:
            reason = evidence.reason if type(evidence.reason) is Reason else Reason.SS_UNAVAILABLE
            result = Identity(
                Verdict.DECLARED, kind, Reason.DECLARED_ONLY if declared_only else reason
            )
        elif type(evidence.connections) is not int or not 0 <= evidence.connections <= MAX_CLIENTS:
            result = Identity(Verdict.DECLARED, kind, Reason.LIMIT)
        elif evidence.connections == 0:
            result = Identity(Verdict.MISMATCH, kind, Reason.NO_CONNECTION)
        elif counts.get(pid, 0) > evidence.connections:
            result = Identity(Verdict.MISMATCH, kind, Reason.COUNTER)
        elif declared_only:
            result = Identity(Verdict.DECLARED, kind, Reason.DECLARED_ONLY)
        else:
            result = Identity(Verdict.VERIFIED, kind, Reason.CONNECTION)
        try:
            # Socket collection can race with exit/exec/PID reuse as well.
            fresh = self.proc.snapshot(pid)
            if fresh.key != facts.key:
                return Identity(Verdict.MISMATCH, kind, Reason.REUSED)
            if fresh.uids != facts.uids or fresh.exe != facts.exe:
                return Identity(Verdict.MISMATCH, kind, Reason.EXE)
        except ProcessGone:
            return Identity(Verdict.MISMATCH, kind, Reason.DEAD)
        except ProcessReused:
            return Identity(Verdict.MISMATCH, kind, Reason.REUSED)
        except IdentityLimit:
            return (
                result
                if result.verdict is Verdict.MISMATCH
                else Identity(Verdict.DECLARED, kind, Reason.LIMIT)
            )
        except Exception:
            # A failed recheck cannot erase a contradiction already established.
            return (
                result
                if result.verdict is Verdict.MISMATCH
                else Identity(Verdict.DECLARED, kind, Reason.UNAVAILABLE)
            )
        return result
