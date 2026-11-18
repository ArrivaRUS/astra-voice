"""Bounded asynchronous socket-evidence seam for E13.M8 step 1.

There is deliberately NO subprocess runner/parser here. The default backend is
unavailable; step 2 can supply an audited adapter. A runner must use the fixed
request (no shell), drain at most max_output bytes, kill/reap on timeout, parse
ss as data, and prove mutually linked ESTAB accepted pulse/native peers. It
must bracket collection with /proc starttime and report counts for that exact
generation (explicit zero only if socket visibility is known complete). It
must not include our own contexts/connections. Caller requests only on detector
or target decisions, never on stream events. No periodic work is created.
"""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Protocol

from astra_voice.core.childenv import clean_env
from astra_voice.meetings.identity import MAX_CLIENTS, ProcessKey, Reason, SocketEvidence

SS_TIMEOUT = 2.0
SS_TTL = 5.0
SS_LIMIT = 4 * 1024 * 1024
SS_ARGV = ("/usr/bin/ss", "-xp")


@dataclass(frozen=True)
class SsRequest:
    argv: tuple[str, ...] = SS_ARGV
    env: Mapping[str, str] = field(
        default_factory=lambda: MappingProxyType(
            clean_env({"LC_ALL": "C", "PATH": "/usr/bin:/bin"})
        )
    )
    shell: bool = False
    timeout: float = SS_TIMEOUT
    max_output: int = SS_LIMIT


@dataclass(frozen=True)
class SocketCount:
    process: ProcessKey
    connections: int


@dataclass(frozen=True)
class SocketSample:
    counts: tuple[SocketCount, ...] = ()
    output_bytes: int = 0


class SsRunner(Protocol):
    def __call__(self, request: SsRequest) -> SocketSample: ...


def _unavailable(request: SsRequest) -> SocketSample:
    raise FileNotFoundError()


class AsyncConnectionProbe:
    """On-demand nonblocking cache. One worker maximum, even if runner stalls.

    A wedged injected backend cannot be killed by Python: timeout discards its
    result and prevents accumulating threads. The real runner must enforce the
    child deadline itself. No stale/pending sample can produce verified/zero.
    evidence() never starts IO; request() is explicit and rate-limited, including
    failed attempts. A cache is scoped to ONE server generation.
    """

    def __init__(
        self,
        runner: SsRunner = _unavailable,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._runner = runner
        self._clock = clock
        self._lock = threading.Lock()
        self._started: float | None = None
        self._pending = False
        self._counts: dict[ProcessKey, int] = {}
        self._reason = Reason.SS_UNAVAILABLE

    def request(self) -> None:
        now = self._clock()
        if not math.isfinite(now):
            return
        with self._lock:
            if self._pending or (self._started is not None and now - self._started < SS_TTL):
                return
            self._started = now
            self._pending = True
            self._counts = {}
            self._reason = Reason.SS_PENDING
            thread = threading.Thread(target=self._refresh, daemon=True, name="meeting-identity-ss")
            try:
                thread.start()
            except RuntimeError:
                self._pending = False
                self._reason = Reason.SS_UNAVAILABLE

    def _refresh(self) -> None:
        counts: dict[ProcessKey, int] = {}
        reason = Reason.SS_UNAVAILABLE
        with self._lock:
            # Rate-limit at actual launch as well as at request time.
            started = self._clock()
            self._started = started
        try:
            sample = self._runner(SsRequest())
            if type(sample.output_bytes) is not int or not 0 <= sample.output_bytes <= SS_LIMIT:
                reason = Reason.SS_LIMIT
            elif len(sample.counts) > MAX_CLIENTS:
                reason = Reason.SS_LIMIT
            else:
                for row in sample.counts:
                    if (
                        type(row.connections) is not int
                        or not 0 <= row.connections <= MAX_CLIENTS
                        or row.process in counts
                        or type(row.process.pid) is not int
                        or not 0 < row.process.pid < 2**31
                        or type(row.process.starttime) is not int
                        or not 0 <= row.process.starttime < 2**64
                    ):
                        raise ValueError()
                    counts[row.process] = row.connections
                reason = Reason.CONNECTION
        except TimeoutError:
            reason = Reason.SS_TIMEOUT
        except Exception:
            # Never log stderr, paths, runner exceptions or socket details.
            reason = Reason.SS_UNAVAILABLE
        with self._lock:
            elapsed = self._clock() - started
            if not math.isfinite(elapsed) or elapsed < 0 or elapsed >= SS_TIMEOUT:
                reason = Reason.SS_TIMEOUT
            self._counts = counts if reason is Reason.CONNECTION else {}
            self._reason = reason
            self._pending = False

    def evidence(self, process: ProcessKey) -> SocketEvidence:
        with self._lock:
            if self._started is None:
                return SocketEvidence()
            age = self._clock() - self._started
            if not math.isfinite(age) or age < 0 or age >= SS_TTL:
                return SocketEvidence(reason=Reason.SS_STALE)
            if self._pending:
                return SocketEvidence(
                    reason=Reason.SS_TIMEOUT if age >= SS_TIMEOUT else Reason.SS_PENDING
                )
            if self._reason is not Reason.CONNECTION:
                return SocketEvidence(reason=self._reason)
            if process not in self._counts:
                return SocketEvidence(reason=Reason.SS_GENERATION)
            return SocketEvidence(self._counts[process], Reason.CONNECTION)
