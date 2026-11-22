"""Role-owned metadata observation and the registry of its Pulse contexts."""

from __future__ import annotations

import socket
import threading
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Literal, Protocol

from astra_voice.meetings.detector_rule import Candidate, MeetingDetectorRule
from astra_voice.worker.meeting_audio_local import CONNECT_LOCK, LocalAudioServer, SocketFds
from astra_voice.worker.meeting_detector import (
    INVALID_INDEX,
    RETRY_SECONDS,
    MeetingDetectorSource,
    MetadataBackend,
    MetadataDisconnected,
    MetadataOpenFailed,
    NormalizedEvent,
    PictureConnection,
    PictureTiming,
    ProgramKey,
)

if TYPE_CHECKING:
    from astra_voice.worker.meeting_identity_check import OwnContexts

OBSERVER_POLL_SECONDS = 0.1
Context = tuple[int, int]
Connect = Callable[[], int]
ObserverConnect = Callable[[Connect], int]
ObserverWait = Callable[[threading.Event, float], bool]


class ObserverBackend(MetadataBackend, Protocol):
    def server_cookie(self) -> int: ...


ObserverFactory = Callable[[bytes, ObserverConnect], ObserverBackend]


@dataclass(frozen=True)
class ObserverServer:
    cookie: int = field(repr=False)


def observer_metadata(server: bytes, connect: ObserverConnect) -> ObserverBackend:
    from astra_voice.worker.meeting_metadata import PulseMetadata
    from astra_voice.worker.pulse_stream import _PulseAsync

    class ObserverPulse(_PulseAsync):
        def context_new(self, api: int, name: bytes) -> int | None:
            with CONNECT_LOCK:
                return super().context_new(api, name)

        def context_connect(self, ctx: int, server: bytes | None, flags: int, spawn: None) -> int:
            native_connect = super().context_connect
            return connect(lambda: native_connect(ctx, server, flags, spawn))

    return PulseMetadata(server=server, pulse=ObserverPulse())


class OwnContextRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._version = 0
        self._entries: dict[Literal["observer", "control"], Context] = {}

    def set(self, owner: Literal["observer", "control"], context: Context | None) -> None:
        if context is not None and not 0 <= context[0] < INVALID_INDEX:
            context = None
        with self._lock:
            if self._entries.get(owner) == context:
                return
            if context is None:
                del self._entries[owner]
            else:
                self._entries[owner] = context
            self._version += 1

    def _indexes(self, context: Context | None) -> frozenset[int]:
        if context is None:
            return frozenset()
        return frozenset(index for index, cookie in self._entries.values() if cookie == context[1])

    def for_observer(self, _generation: int) -> frozenset[int]:
        with self._lock:
            return self._indexes(self._entries.get("observer"))

    def snapshot(self) -> OwnContexts:
        from astra_voice.worker.meeting_identity_check import OwnContexts

        with self._lock:
            context = self._entries.get("observer") or self._entries.get("control")
            return OwnContexts(
                self._version, self._indexes(context), context[1] if context is not None else None
            )


class _RegisteredBackend:
    def __init__(
        self,
        backend: ObserverBackend,
        index: int,
        cookie: int,
        epoch: int,
        registry: OwnContextRegistry,
    ) -> None:
        self._backend = backend
        self._registry = registry
        self.own_index = index
        self.cookie = cookie
        self.epoch = epoch
        self.snapshot = backend.snapshot
        self.client = backend.client
        self.stream = backend.stream
        self.poll = backend.poll
        self.take_overflow = backend.take_overflow
        self.has_pending_notices = backend.has_pending_notices

    def close(self) -> None:
        self._backend.close()
        self._registry.set("observer", None)


def remote_observer_code() -> dict[str, Any]:
    return {
        "type": "meeting.detector.code",
        "purpose": "observe",
        "code": "remote",
        "duration_ms": 0,
    }


class MeetingObserver:
    """One serial owner of the source, active only while observation is requested."""

    def __init__(
        self,
        *,
        address: LocalAudioServer | None,
        factory: ObserverFactory,
        read_fds: Callable[[], SocketFds],
        family: Callable[[int, int], int | None],
        wait: ObserverWait,
        clock: Callable[[], float],
        emit: Callable[[dict[str, Any]], None],
    ) -> None:
        self.address = address
        self.automation_off = threading.Event()
        if address is None:
            self.automation_off.set()
        self.registry = OwnContextRegistry()
        self.rule_lock = threading.Lock()
        self.rule = MeetingDetectorRule(clock=clock)
        self._end_revisions: dict[ProgramKey, int] = {}
        self._factory, self._read_fds, self._family = factory, read_fds, family
        self._clock, self._wait, self._emit = clock, wait, emit
        self._condition = threading.Condition()
        self._wake = threading.Event()
        self._requested = self._exiting = False
        self._epoch = 0
        self._thread: threading.Thread | None = None

    @property
    def epoch(self) -> int:
        with self._condition:
            return self._epoch

    def start(self) -> None:
        with self._condition:
            if self._exiting:
                return
            if self.automation_off.is_set():
                self._report_remote(self._epoch)
                return
            self._requested = True
            if self._thread is None:
                thread = threading.Thread(target=self._run, name="meeting-detector", daemon=True)
                thread.start()
                self._thread = thread
            self._condition.notify_all()

    def stop(self, *, exiting: bool = False) -> None:
        with self.rule_lock, self._condition:
            self.rule.invalidate_outputs()
            self._epoch += 1
            self._requested = False
            self._exiting |= exiting
            self._wake.set()
            self._condition.notify_all()

    def join(self, timeout: float) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    def _report_remote(self, epoch: int) -> None:
        self._emit({"type": "detector-code", "epoch": epoch, "event": remote_observer_code()})

    def _connect(self, connect: Connect, epoch: int) -> int:
        with CONNECT_LOCK:
            # CONNECT_LOCK is innermost, so read the request state without the condition.
            if self._epoch != epoch or not self._requested or self.automation_off.is_set():
                raise MetadataDisconnected()
            before = self._read_fds()
            if self._epoch != epoch or not self._requested or self.automation_off.is_set():
                raise MetadataDisconnected()
            result = connect()
            after = self._read_fds()
        if result >= 0 and before is not None and after is not None:
            added = [(fd, inode) for fd, inode in after.items() if before.get(fd) != inode]
            if len(added) == 1:
                family = self._family(*added[0])
                if family is not None and family != socket.AF_UNIX:
                    self.automation_off.set()
                    raise MetadataDisconnected()
        return result

    def _active(self, epoch: int) -> bool:
        # Called under the condition. The worker closes the old source before a new epoch.
        return self._requested and self._epoch == epoch and not self.automation_off.is_set()

    def _wait_active(self, epoch: int, delay: float) -> bool:
        with self._condition:
            if not self._active(epoch):
                return False
            self._wake.clear()
        self._wait(self._wake, delay)
        with self._condition:
            return self._active(epoch)

    def _open(self, epoch: int) -> MetadataBackend:
        if self.automation_off.is_set():
            raise MetadataDisconnected()
        with self._condition:
            if not self._active(epoch):
                raise MetadataDisconnected()
        assert self.address is not None
        try:
            backend = self._factory(
                self.address.address, lambda connect: self._connect(connect, epoch)
            )
        except Exception:
            if self.automation_off.is_set():
                self._report_remote(epoch)
            raise
        try:
            with self._condition:
                if not self._active(epoch):
                    raise MetadataDisconnected()
            index, cookie = backend.own_index, backend.server_cookie()
            with self._condition:
                if not self._active(epoch):
                    raise MetadataDisconnected()
                self.registry.set("observer", (index, cookie))
                self._emit(
                    {
                        "type": "detector-server-changed",
                        "epoch": epoch,
                        "server": ObserverServer(cookie),
                    }
                )
        except Exception:
            try:
                backend.close()
            except Exception:
                raise MetadataOpenFailed(backend) from None
            raise
        return _RegisteredBackend(backend, index, cookie, epoch, self.registry)

    def end_revisions(self) -> dict[ProgramKey, int]:
        """Snapshot the call endings with rule_lock held."""
        return dict(self._end_revisions)

    def end_revision(self, program: ProgramKey) -> int:
        """Read the revision of one program with rule_lock held."""
        return self._end_revisions.get(program, 0)

    def _call_ended(self, program: ProgramKey, epoch: int) -> None:
        # The caller holds rule_lock. Keep the epoch check and emission atomic with stop().
        with self._condition:
            if epoch == self._epoch:
                self._end_revisions[program] = self.end_revision(program) + 1
                self._emit({"type": "detector-call-ended", "program": program, "epoch": epoch})

    def _update(
        self, events: tuple[NormalizedEvent, ...], timing: PictureTiming, epoch: int
    ) -> None:
        with self.rule_lock:
            with self._condition:
                if not self._active(epoch):
                    timing = replace(timing, connection=None)
            for decision in self.rule.update(events, timing=timing):
                message = {"program": decision.program, "epoch": epoch}
                if isinstance(decision, Candidate):
                    self._emit(
                        {"type": "detector-candidate", "offered": decision.offered, **message}
                    )
                else:
                    self._call_ended(decision.program, epoch)

    def _read_picture(
        self,
        source: MeetingDetectorSource,
        read: Callable[[], tuple[NormalizedEvent, ...]],
        epoch: int,
    ) -> None:
        # I/O may rebuild or close the source before the next publication.
        with self.rule_lock:
            self.rule.invalidate_outputs()
        events = read()
        timing = source.picture_timing
        current = source.picture_backend
        if timing.confirmed and current is not None:
            backend, generation = current
            if isinstance(backend, _RegisteredBackend) and backend.epoch == epoch:
                timing = replace(timing, connection=PictureConnection(backend.cookie, generation))
        self._update(events, timing, epoch)

    def _run(self) -> None:
        epoch = 0
        source = MeetingDetectorSource(
            factory=lambda: self._open(epoch),
            own_contexts=self.registry.for_observer,
            clock=self._clock,
        )
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._requested or self._exiting)
                if self._exiting or self.automation_off.is_set():
                    return
                epoch = self._epoch
            retry = False
            try:
                self._read_picture(source, source.start, epoch)
                while self._wait_active(epoch, OBSERVER_POLL_SECONDS):
                    self._read_picture(source, source.poll, epoch)
            except Exception:
                # Recover failures that escaped the source's own retry logic.
                retry = True
            finally:
                with suppress(Exception):
                    self._read_picture(source, source.stop, epoch)
            if self.automation_off.is_set():
                return
            if retry:
                self._wait_active(epoch, RETRY_SECONDS)
