"""Meeting capture control, bounded audio queues, and independent durable writer.

The control loop never calls fsync, reads PCM, or waits for a native close. It
observes a fixed physical-close deadline and leaves unknown closure to process
supervision. A daemon storage worker cannot hold up microphone disconnection.
"""

from __future__ import annotations

import contextlib
import os
import queue
import threading
import time
import uuid
import wave
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, cast

from astra_voice.meetings.events import DetectorCode
from astra_voice.meetings.segments import CaptureJournal, ClockAnchor, SegmentSidecar
from astra_voice.meetings.store import MeetingStore, MeetingStoreBusy, MeetingWriter, Writer
from astra_voice.security.files import private_directory
from astra_voice.worker import ipc
from astra_voice.worker.meeting_audio_local import (
    LocalAudioServer,
    SocketFds,
    own_socket_fds,
    resolve_local_server,
    socket_family,
)
from astra_voice.worker.meeting_call_service import (
    CheckFactory,
    MeetingCallService,
    result_code,
    role_checker,
)
from astra_voice.worker.meeting_contract import COMMAND_FIELDS, RequestLedger
from astra_voice.worker.meeting_observer import (
    MeetingObserver,
    ObserverFactory,
    ObserverWait,
    observer_metadata,
)
from astra_voice.worker.meeting_roles import failure
from astra_voice.worker.meeting_session import MeetingSession, SessionAdmission

if TYPE_CHECKING:
    from astra_voice.worker.meeting_identity_check import CallAnchor, PinnedStream, PinnedTarget

Message = dict[str, Any]
MAX_PCM_BYTES = 128000  # total across the two tracks
MAX_TRACK_PCM_BYTES = 64000  # two seconds, including the block being written
ARCHIVE_WRITE_RETRY_SECONDS = 0.5
CLOSE_SECONDS = 1.0
TARGET_SECONDS = 3.0
FINAL_STATUS_RANK = {"succeeded": 0, "partial": 1, "failed": 2}
# Two 100ms fragment endpoints may differ by 200ms of jitter; keep 50ms
# margin. Add 200ppm of this block's duration, not accumulated epoch drift.
CLOCK_JITTER_MS = 250
CLOCK_DRIFT_PPM = 200


class Source(Protocol):
    def open(self, device: str | None = None) -> None: ...
    def read_chunk(self) -> bytes | None: ...
    def sample_clock(self) -> tuple[str, int, int]: ...
    def close(self) -> None: ...
    def shutdown(self) -> None: ...


class Backend(Protocol):
    def prepare(self, far: Message, *, target: Any = None) -> Message: ...
    def source(self, far: Message, *, target: Any = None) -> Source: ...
    def close(self) -> None: ...


@dataclass(repr=False)
class _PreparedControl:
    generation: int
    backend: Backend | None
    context: tuple[int, int] | None = None


def _backend() -> Backend:
    from astra_voice.worker.monitor_stream import CapturePulseBackend

    return CapturePulseBackend()


def _microphone() -> Source:
    from astra_voice.worker.monitor_stream import microphone_source

    return microphone_source()  # type: ignore[no-any-return]


def _observer_address() -> LocalAudioServer | None:
    return resolve_local_server(os.environ, os.getuid())


def _observer_wait(wake: threading.Event, seconds: float) -> bool:
    return wake.wait(seconds)


class _Archive:
    """Single disk owner. Queue bounds PCM and refuses backlog without blocking audio."""

    def __init__(
        self,
        directory: Path,
        identity: Message,
        index: int,
        fsync: Callable[[int], None],
        emit_event: Callable[[Message], None],
        *,
        retry_clock: Callable[[], float] = time.monotonic,
        retry_wait: Callable[[float], None] = time.sleep,
    ) -> None:
        self.directory, self.identity = directory, identity
        self.fsync, self.emit_event = fsync, emit_event
        self._retry_clock, self._retry_wait = retry_clock, retry_wait
        self.jobs: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=256)
        self.lock = threading.Lock()
        self.pending_bytes = 0
        self.pending_track_bytes = {"mic": 0, "far": 0}
        self.failed = threading.Event()
        self.stopping = threading.Event()
        self.counts = {"mic": index, "far": index}
        self.segments: dict[str, tuple[Any, Any, SegmentSidecar]] = {}
        self.next_sample = {"mic": 0, "far": 0}
        self.last_checkpoint = {"mic": 0, "far": 0}
        self.last_clock: dict[str, tuple[str, int, int]] = {}
        self.journal = CaptureJournal(
            identity["meeting_id"], identity["meeting_generation"], identity["capture_id"]
        )
        self.intervals: list[Message] = []
        self.thread = threading.Thread(target=self._run, name="meeting-storage", daemon=True)
        self.thread.start()

    def put(self, kind: str, payload: Any) -> bool:
        if kind == "quit":
            # Shutdown is independent of PCM/control queue credits. Wake an
            # idle reader; a full queue drains before observing the same flag.
            self.stopping.set()
            with contextlib.suppress(queue.Full):
                self.jobs.put_nowait((kind, payload))
            return True
        size = len(payload[1]) if kind == "pcm" else 0
        with self.lock:
            if (
                self.failed.is_set()
                or self.pending_bytes + size > MAX_PCM_BYTES
                or (
                    kind == "pcm"
                    and self.pending_track_bytes[payload[0]] + size > MAX_TRACK_PCM_BYTES
                )
            ):
                return False
            try:
                self.jobs.put_nowait((kind, payload))
            except queue.Full:
                return False
            self.pending_bytes += size
            if kind == "pcm":
                self.pending_track_bytes[payload[0]] += size
        return True

    def _write_with_retry(
        self, owner: MeetingWriter, filename: str, document: CaptureJournal | SegmentSidecar
    ) -> None:
        deadline = self._retry_clock() + ARCHIVE_WRITE_RETRY_SECONDS
        delay = 0.01
        while True:
            try:
                owner.write(filename, document)
                return
            except MeetingStoreBusy:
                remaining = deadline - self._retry_clock()
                if remaining <= 0:
                    raise
                # `stopping` may already be set while the final close is queued.
                self._retry_wait(min(delay, remaining))
                delay = min(delay * 2, 0.05)

    def _edge(self, payload: Message, owner: MeetingWriter) -> None:
        if payload["kind"] == "close" and len(self.intervals) % 2 == 0:
            return
        self.journal.boundaries.append(
            {
                key: payload[key]
                for key in (
                    "kind",
                    "t_mono_ms",
                    "recorded_ms",
                    "reason",
                    "pause_reasons",
                    "manual_resume_required",
                    "blocked_epoch",
                )
                if key in payload
            }
        )
        if payload["kind"] in {"open", "close"}:
            self.intervals.append({key: payload[key] for key in ("t_mono_ms", "recorded_ms")})
        if "state_rev" in payload:
            self.identity["state_rev"] = payload["state_rev"]
        self._write_with_retry(owner, "capture.json", self.journal)

    def _start(
        self,
        track: str,
        stamp: tuple[str, int, int],
        recorded: int,
        audio: int,
        owner: Any,
        *,
        new_epoch: bool = True,
    ) -> None:
        if self.counts[track] > 9999:
            raise ValueError("capture-segment-limit")
        if new_epoch:
            self.next_sample[track] = 0
        sample = self.next_sample[track]
        fd = os.open(
            f"{track}-{self.counts[track]:04d}.wav",
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=audio,
        )
        os.fchmod(fd, 0o600)
        file = os.fdopen(fd, "wb")
        try:
            wav = wave.open(file, "wb")
            wav.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
            epoch = uuid.uuid4().hex if new_epoch else self.last_epoch[track]
            sidecar = SegmentSidecar(
                **self.identity,
                track=track,
                index=self.counts[track],
                epoch_id=epoch,
                clock=stamp[0],
                clock_anchor=ClockAnchor(stamp[1], stamp[2]),
                opened_t_mono_ms=stamp[2],
                recorded_ms_at_open=recorded,
                sample_start=sample,
                sample_end=sample,
                timeline=[[sample, stamp[2]]],
                source_kind="mic_only" if track == "mic" else self.source_kind,
            )
            self.segments[track] = wav, file, sidecar
            self.last_epoch[track] = epoch
            self.last_clock[track] = stamp
            self.last_checkpoint[track] = stamp[2]
            wav.writeframesraw(b"")
        except BaseException:
            file.close()
            raise

    def _checkpoint(self, track: str, owner: MeetingWriter) -> None:
        wav, file, sidecar = self.segments[track]
        wav.writeframes(b"")  # update WAV header before fsync, including open segments
        file.flush()
        self.fsync(file.fileno())
        sidecar.frames_at_checkpoint = sidecar.frames
        # A durable sample range must have an exact mapped endpoint, even when
        # this checkpoint falls between the one-second timeline knots. Keep the
        # extra endpoint only in the snapshot (<=302 points for 300 seconds).
        timeline = sidecar.timeline
        mono = self.last_clock[track][2]
        if sidecar.sample_end > timeline[-1][0]:
            sidecar.timeline = [*timeline, [sidecar.sample_end, mono]]
        try:
            self._write_with_retry(owner, f"audio/{track}-{sidecar.index:04d}.json", sidecar)
        finally:
            sidecar.timeline = timeline

    def _finish(self, track: str, reason: str, owner: Any) -> None:
        if track not in self.segments:
            return
        wav, file, sidecar = self.segments[track]
        sidecar.state_rev = self.identity["state_rev"]
        sidecar.closed = True
        sidecar.close_reason = reason
        sidecar.pause_reasons = (
            [reason]
            if reason
            in {
                "user",
                "lock",
                "unknown",
                "lease",
                "indicators",
                "mic_change",
                "audio_failed",
                "disk",
            }
            else []
        )
        mono = self.last_clock[track][2]
        if sidecar.frames and mono > sidecar.timeline[-1][1]:
            sidecar.timeline.append([sidecar.sample_end, mono])
        try:
            self._checkpoint(track, owner)
            wav.close()
        finally:
            file.close()
        del self.segments[track]
        self.next_sample[track] = sidecar.sample_end
        self.counts[track] += 1
        self.emit_event({"type": "segment", "sidecar": sidecar.to_dict()})

    def _pcm(self, payload: Any, audio: int, owner: Any) -> None:
        track, data, stamp, recorded = payload
        if not data or len(data) % 2 or len(data) > 64000:
            raise ValueError("capture-pcm-invalid")
        previous = self.last_clock.get(track)
        duration_ms = len(data) / 32  # mono s16le, 16kHz
        tolerance_ms = CLOCK_JITTER_MS + duration_ms * CLOCK_DRIFT_PPM / 1000000
        changed = previous and (
            stamp[0] != previous[0]
            or any(
                abs(stamp[index] - previous[index] - duration_ms) > tolerance_ms for index in (1, 2)
            )
        )
        if previous and not changed:
            # Latency refreshes may move a 20ms endpoint backwards by a few ms.
            # Apply the same jitter budget as forward corrections, and retain a
            # strictly increasing sample axis without widening that budget.
            stamp = (stamp[0], stamp[1], max(stamp[2], previous[2] + 1))
        if changed:
            if track in self.segments:
                self.segments[track][2].discontinuity = True
            self._finish(track, "clock_change", owner)
        if track not in self.segments:
            # First sample precedes this end-of-buffer timestamp.
            # Exact rotations share one sample with the completed segment.
            # Reusing its clock preserves a single mapping in this epoch; the
            # next read's latency correction belongs to its new endpoint only.
            start = (
                previous
                if previous and not changed
                else (
                    stamp[0],
                    max(0, stamp[1] - len(data) // 32),
                    max(0, stamp[2] - len(data) // 32),
                )
            )
            self._start(
                track,
                start,
                max(0, recorded - len(data) // 32),
                audio,
                owner,
                new_epoch=not previous or bool(changed),
            )
        while data:
            wav, _, sidecar = self.segments[track]
            capacity = (4800000 - sidecar.frames) * 2
            block, data = data[:capacity], data[capacity:]
            wav.writeframesraw(block)
            sidecar.frames += len(block) // 2
            sidecar.sample_end += len(block) // 2
            # A buffer can straddle the 300s rotation boundary. Normalize each
            # delivered sub-block as well: backfilling the remaining duration
            # after a small latency correction must not reverse its endpoint.
            mono = max(stamp[2] - len(data) // 32, self.last_clock[track][2] + 1)
            self.last_clock[track] = (stamp[0], stamp[1] - len(data) // 32, mono)
            if mono - sidecar.timeline[-1][1] >= 1000:
                sidecar.timeline.append([sidecar.sample_end, mono])
            if sidecar.frames == 4800000:
                self._finish(track, "segment", owner)
                if data:
                    self._start(
                        track, self.last_clock[track], recorded, audio, owner, new_epoch=False
                    )
            # Two seconds of PCM queueing fit inside the ten-second crash-loss
            # budget. Bound frames as well as time: latency corrections must not
            # postpone durability when server-derived time progresses slowly.
            elif (
                sidecar.frames == len(block) // 2
                or mono - self.last_checkpoint[track] >= 8000
                or sidecar.frames - sidecar.frames_at_checkpoint >= 128000
            ):
                self._checkpoint(track, owner)
                if mono - self.last_checkpoint[track] >= 8000:
                    self.last_checkpoint[track] = mono

    def _run(self) -> None:
        self.last_epoch: dict[str, str] = {}
        self.source_kind = "app_stream"
        try:
            store = MeetingStore(self.directory.parent)
            with store.writer(self.directory.name, Writer.CAPTURE) as owner:
                with private_directory(self.directory / "audio") as audio:
                    while True:
                        if self.stopping.is_set() and self.jobs.empty():
                            return
                        kind, payload = self.jobs.get()
                        try:
                            if kind == "start":
                                self.source_kind = payload.pop("source_kind")
                                self._edge(payload, owner)
                            elif kind == "track_start":
                                track, _, _ = payload
                                # The journal owns open/close boundaries. A PCM
                                # epoch starts with actual data, not the timing
                                # fallback sampled before Pulse is ready.
                                self.last_clock.pop(track, None)
                                self.fsync(audio)
                            elif kind == "pcm":
                                self._pcm(payload, audio, owner)
                            elif kind == "track_close":
                                track, reason = payload
                                self._finish(track, reason, owner)
                            elif kind == "edge":
                                self._edge(payload, owner)
                            elif kind == "close":
                                self.identity["state_rev"] = payload["state_rev"]
                                for track in list(self.segments):
                                    sidecar = self.segments[track][2]
                                    sidecar.manual_resume_required = payload.get(
                                        "manual_resume_required", False
                                    )
                                    sidecar.blocked_epoch = payload.get("blocked_epoch", 0)
                                    self._finish(track, payload["reason"], owner)
                                self._edge(payload, owner)
                                self.fsync(audio)
                                self.emit_event(
                                    {
                                        "type": "finalized",
                                        "request": payload.get("request"),
                                        "segments": dict(self.counts),
                                        "intervals": deepcopy(self.intervals),
                                        "status": "succeeded",
                                    }
                                )
                            elif kind == "quit":
                                return
                        finally:
                            if kind == "pcm":
                                with self.lock:
                                    self.pending_bytes -= len(payload[1])
                                    self.pending_track_bytes[payload[0]] -= len(payload[1])
        except Exception:
            self.failed.set()
            self.emit_event({"type": "disk-failed"})
        finally:
            for wav, file, _ in self.segments.values():
                # Do not claim durability/closed on storage failure.
                try:
                    wav.close()
                except Exception:
                    pass
                file.close()


class MeetingCaptureState:
    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        mic_factory: Callable[[], Source] = _microphone,
        backend_factory: Callable[[], Backend] = _backend,
        free_mb: Callable[[], int] | None = None,
        fsync: Callable[[int], None] = os.fsync,
        session: SessionAdmission | None = None,
        observer_address: Callable[[], LocalAudioServer | None] = _observer_address,
        observer_factory: ObserverFactory = observer_metadata,
        observer_fds: Callable[[], SocketFds] = own_socket_fds,
        observer_family: Callable[[int, int], int | None] = socket_family,
        observer_wait: ObserverWait = _observer_wait,
        check_factory: CheckFactory = role_checker,
    ) -> None:
        self.session_monitor = MeetingSession.production() if session is None else None
        self.session = session if session is not None else self.session_monitor.state  # type: ignore[union-attr]
        self.manual_resume_required = False
        self.session_terminal = False
        self.auto_resume_pending = False
        self.lease_tracks: set[str] = set()
        self.indicators_visible = False
        self.recovery_admission: Message | None = None
        self.recovery: Message | None = None
        self.pause_at: float | None = None
        self.pause_epoch: int | None = None
        self.last_keepalive_seq = -1
        if self.session_monitor is not None:
            self.session_monitor.start()
        self.clock, self.mic_factory = clock, mic_factory
        self.backend_factory, self.fsync = backend_factory, fsync
        self.backend: Backend | None = None
        self._control_generation = 0
        self.free_mb = free_mb or self._free_mb
        self.directory: Path | None = None
        self.ledger = RequestLedger()
        self.identity: Message = {}
        self.far: Message = {"kind": "none"}
        self.prepared_target: PinnedTarget | None = None
        self.capture_target: PinnedTarget | None = None
        self._prepared_anchor: CallAnchor | None = None
        self._target_epoch: int | None = None
        self.mic_device: str | None = None
        self.archive: _Archive | None = None
        self.sources: dict[str, Source] = {}
        self.track_states = {"mic": "ended", "far": "ended"}
        self._far_incomplete = False
        self._far_partial_pending = False
        self._last_far_partial = float("-inf")
        self.messages: queue.SimpleQueue[Message] = queue.SimpleQueue()
        self.observer = MeetingObserver(
            address=observer_address(),
            factory=observer_factory,
            read_fds=observer_fds,
            family=observer_family,
            wait=observer_wait,
            clock=clock,
            emit=lambda message: self.messages.put(message),
        )
        self.calls = MeetingCallService(self, check_factory)
        self.audio_stop = threading.Event()
        self.stopped = False
        self.exit_code = 0
        self.state = "stopped"
        self.pause_reasons: set[str] = set()
        self.recorded_ms = 0
        self.active_at: float | None = None
        self.last_lease = self.last_alive = self.last_disk = self.clock()
        self.close_deadline: float | None = None
        self.close_wall: float | None = None
        self.closing_request: Message | None = None
        self.close_waiters: list[Message] = []
        self.pending: dict[str, list[Message]] = {}
        self.opening = False
        self.finalizing = False
        self.final_status = "succeeded"
        self.lost_estimate_ms = 0
        self.open_thread: threading.Thread | None = None
        self.readers: dict[str, threading.Thread] = {}
        self.shutdown_request: Message | None = None
        self.terminating = False
        self.used_meetings: set[str] = set()
        self.termination_deadline: float | None = None
        self.seq = 0
        self.preparing = False
        self.prepare_starts = 0
        self.prepare_request: Message | None = None
        self.prepare_deadline: float | None = None
        self.disk_sampling = False
        self.cached_free_mb = 0
        self.cpu_at, self.cpu_time = self.clock(), time.process_time()
        self.limit_ms = 7200000
        self.extends = 0

    def _free_mb(self) -> int:
        if self.directory is None:
            return 0
        stats = os.statvfs(self.directory)
        return stats.f_bavail * stats.f_frsize // (1024 * 1024)

    def _base(self) -> Message:
        return dict(self.identity)

    def _pause(self) -> Message:
        return {
            "pause_reasons": sorted(self.pause_reasons),
            "manual_resume_required": self.manual_resume_required,
            "blocked_epoch": self.session.snapshot().blocked_epoch,
            "lock_signal_unavailable": self.session.snapshot().lock_signal_unavailable,
        }

    def _admitted(self, request: Message) -> bool:
        return (
            not self.session_terminal
            and (self.pause_at is None or self.clock() - self.pause_at < 3600)
            and self.session.allowed(request["blocked_epoch"])
            and 0 <= self.clock() * 1000 - request["admission_mono_ms"] <= 1000
            and (
                not request.get("automatic")
                or (
                    self.indicators_visible
                    and self.pause_epoch == request["blocked_epoch"]
                    and not self.manual_resume_required
                )
            )
        )

    def _unadmitted_open_reason(self, request: Message) -> str:
        """An automatic reopen that lost its attestation returns to its own pause."""
        reason = self.session.snapshot().reason
        if reason is not None:
            return reason
        if (
            not request.get("automatic")
            or self.stopped
            or self.session_terminal
            or (self.pause_at is not None and self.clock() - self.pause_at >= 3600)
        ):
            return "stop"
        return str(request["recovery_reason"])

    def _expire_pause(self) -> None:
        """Terminal before commands/open completion, even if GUI was suspended."""
        if self.pause_at is None or self.clock() - self.pause_at < 3600:
            return
        self.session_terminal = True
        self.auto_resume_pending = False
        self._drop_target()
        if self.sources or self.opening:
            self._close(None, "stop")
        self.state = "stopped"

    def _begin_pause(self) -> None:
        if self.pause_at is None:
            self.pause_at = self.clock()
            self.pause_epoch = self.session.snapshot().blocked_epoch

    def _add_reason(self, reason: str) -> bool:
        if reason not in {
            "user",
            "lock",
            "unknown",
            "lease",
            "indicators",
            "mic_change",
            "audio_failed",
            "disk",
        }:
            return False
        added = reason not in self.pause_reasons
        self.pause_reasons.add(reason)
        if reason in {"lock", "unknown"} and self.close_deadline is not None:
            self.close_deadline = min(self.close_deadline, self.clock() + 0.2)
            self.close_wall = min(self.close_wall or float("inf"), time.monotonic() + 0.2)
        if reason not in {"lease", "indicators"}:
            self.manual_resume_required = True
            self.auto_resume_pending = False
        if added and self.archive is not None and self.state == "paused":
            self.archive.put(
                "edge",
                {
                    "kind": "pause_state",
                    "t_mono_ms": int(self.clock() * 1000),
                    "recorded_ms": self._recorded(),
                    "reason": reason,
                    **{
                        key: value
                        for key, value in self._pause().items()
                        if key != "lock_signal_unavailable"
                    },
                },
            )
        return added

    def _session_events(self, result: list[Message]) -> None:
        snapshots = self.session.drain()
        snapshots.append(self.session.snapshot())
        for snapshot in snapshots:
            if not self.identity or self.state in {"stopped", "closing-unknown"}:
                continue
            reason = snapshot.reason
            if reason in {"sleep", "session"}:
                self.session_terminal = True
                self.auto_resume_pending = False
                self._drop_target()
                if self.sources or self.opening:
                    self._close(None, reason)
                self.state = "stopped"
            elif reason in {"lock", "unknown"}:
                self._autopause(reason, reason, result)

    def _maybe_auto_resume(self) -> None:
        if (
            not self.auto_resume_pending
            or self.state != "paused"
            or self.opening
            or self.finalizing
            or self.close_deadline is not None
            or self.manual_resume_required
            or self.session_terminal
            or not self.pause_reasons
            or not self.pause_reasons <= {"lease", "indicators"}
            or self.final_status == "failed"
            or self.clock() - self.last_lease >= 5
            or not self.indicators_visible
            or self.recovery_admission is None
            or not self._can_reopen()
        ):
            return
        request = {
            **self.recovery_admission,
            "automatic": True,
            "recovery_reason": "indicators" if "indicators" in self.pause_reasons else "lease",
        }
        if not self._admitted(request):
            return
        # The GUI attestation was checked above; the 1 s open window is then
        # measured on capture's own clock, not shortened by the keepalive's age.
        # Visibility and epoch are re-checked through _admitted at native connect.
        request["admission_mono_ms"] = int(self.clock() * 1000)
        self.auto_resume_pending = False
        self.selected_tracks = [
            track
            for track in ("mic", "far")
            if track in self.lease_tracks and self.track_states[track] == "paused"
        ]
        if self.selected_tracks:
            self._open(request)

    def _recorded(self) -> int:
        return min(
            14400000,
            self.recorded_ms
            + (
                max(0, int((self.clock() - self.active_at) * 1000))
                if self.active_at is not None
                else 0
            ),
        )

    def _reply(self, kind: str, request: Message, **fields: Any) -> Message:
        return {
            "type": kind,
            **{
                key: request[key]
                for key in ("req", "request_type", "meeting_id", "meeting_generation")
                if key in request
            },
            **fields,
        }

    def _remember(self, request: Message, result: list[Message]) -> list[Message]:
        self.ledger.finish(request["req"], result)
        return deepcopy(result)

    def _outcome(self, request: Message | None, event: Message) -> Message:
        if request is None:
            return event
        event = {**event, "req": request["req"], "request_type": request["request_type"]}
        if request["req"] in self.ledger.requests:
            previous = self.pending.setdefault(request["req"], [])
            previous.append(event)
            self.ledger.finish(request["req"], previous)
        return event

    def _get_backend(self) -> Backend:
        if self.backend is None:
            self.backend = self.backend_factory()
        return self.backend

    @staticmethod
    def _retire_backend(backend: Backend | None) -> None:
        if backend is not None:
            threading.Thread(
                target=backend.close, name="meeting-control-close", daemon=True
            ).start()

    def _reset_control(self) -> None:
        self._control_generation += 1
        self.observer.registry.set("control", None)
        backend, self.backend = self.backend, None
        self._retire_backend(backend)

    def handle(self, message: Message) -> list[Message]:
        arrived_at = (
            self.clock()
            if message.get("type") in ("meeting.call.query", "meeting.capture.prepare")
            else 0.0
        )
        message = ipc.decode(
            ipc.encode(message, role="meeting-capture")[4:], role="meeting-capture"
        )
        if message["type"] not in COMMAND_FIELDS["meeting-capture"]:
            return [failure(message, "unknown-message")]
        status, cached = self.ledger.begin(message)
        if status == "same":
            return deepcopy(cached or [])
        if status in {"conflict", "limit", "exhausted"}:
            # Exhaustion refuses new work, never the ability to close. The
            # bounded ledger still admits keepalive and closing controls.
            return [failure(message, "req-conflict" if status == "conflict" else "limit")]
        action = message["type"]
        session_events: list[Message] = []
        self._session_events(session_events)
        self._expire_pause()
        for event in session_events:
            self.messages.put({"type": "session-event", "event": event})
        try:
            if self.stopped and action != "meeting.capture.shutdown":
                raise ValueError("not-prepared")
            if action == "ping":
                return self._remember(message, [self._reply("pong", message)])
            if action in {"meeting.detector.start", "meeting.detector.stop"}:
                self._detector(message)
                reply = self._reply("meeting.ack", message)
                if message["req"] in self.ledger.requests:
                    self.pending[message["req"]] = [reply]
                return self._remember(message, [reply])
            if action == "meeting.call.query":
                return self.calls.query(message, arrived_at)
            if action == "meeting.capture.shutdown":
                if not self.sources and not self.opening and not self.finalizing:
                    self.terminate()
                    self.stopped = True
                    if self.archive:
                        self.archive.put("quit", None)
                    return self._remember(message, [self._reply("meeting.capture.bye", message)])
                self.shutdown_request = message
                self.terminate()
                reply = self._reply("meeting.ack", message)
                if message["req"] in self.ledger.requests:
                    self.pending[message["req"]] = [reply]
                return self._remember(message, [reply])
            if action == "meeting.capture.prepare":
                if (
                    self.preparing
                    or self.opening
                    or self.finalizing
                    or self.sources
                    or self.close_deadline is not None
                    or self.stopped
                    or self.terminating
                    or message["meeting_id"] in self.used_meetings
                ):
                    raise ValueError("not-prepared")
                self._prepare(message, arrived_at)
                reply = self._reply("meeting.ack", message)
                if message["req"] in self.ledger.requests:
                    self.pending[message["req"]] = [reply]
                return self._remember(message, [reply])
            if self.stopped or self.terminating or self.preparing:
                raise ValueError("not-prepared")
            # Closing must survive an opened/track event racing the GUI's view
            # of state_rev. Capture identity/generation still match exactly.
            # shutdown is process-wide (no CAPTURE fields in M1) and handled above.
            closing = action in {"meeting.capture.stop", "meeting.capture.pause"}
            if not self.identity or any(
                message.get(key) != value
                for key, value in self.identity.items()
                if key != "state_rev" or not closing
            ):
                raise ValueError("not-prepared")
            if "admission_mono_ms" in message and not (
                0 <= self.clock() * 1000 - message["admission_mono_ms"] <= 1000
            ):
                raise ValueError("admission-stale")
            if action == "meeting.capture.keepalive":
                if message["seq"] <= self.last_keepalive_seq:
                    return self._remember(
                        message, [self._reply("meeting.ack", message, **self._base())]
                    )
                self.last_keepalive_seq = message["seq"]
                self.last_lease = self.clock()
                # The native adapter uses this same lock for admission at connect.
                # A stale/legacy keepalive cannot attest to a visible indicator.
                with self.session.lock:
                    self.indicators_visible = message.get("indicators_visible") is True
                self.recovery_admission = (
                    dict(message)
                    if {"blocked_epoch", "admission_mono_ms"} <= message.keys()
                    else None
                )
                self.auto_resume_pending = self.state == "paused" and self.indicators_visible
                if (
                    self.opening
                    and self.open_request.get("automatic")
                    and not self.indicators_visible
                ):
                    self._close(None, "indicators")
                self._maybe_auto_resume()
                return self._remember(
                    message, [self._reply("meeting.ack", message, **self._base())]
                )
            if action == "meeting.capture.extend":
                if self.extends >= 2 or message["max_ms"] != self.limit_ms + 3600000:
                    raise ValueError("limit")
                self.extends += 1
                self.limit_ms = message["max_ms"]
                return self._remember(
                    message,
                    [
                        self._reply(
                            "meeting.capture.started",
                            message,
                            **self._base(),
                            **self._pause(),
                            t0_mono_ms=int(self.clock() * 1000),
                            tracks=list(self.sources),
                        )
                    ],
                )
            if action in {"meeting.capture.open", "meeting.capture.resume"}:
                first = action == "meeting.capture.open"
                app_stream = self.far["kind"] == "app_stream"
                expected = "prepared" if first else "paused"
                if self.state != expected or self.opening or self.finalizing:
                    if (
                        first
                        and app_stream
                        and self.state == "paused"
                        and not self._admitted(message)
                    ):
                        raise ValueError("admission-stale")
                    raise ValueError("not-prepared")
                if self.final_status == "failed":
                    raise ValueError("io")
                if not self.far_available:
                    raise ValueError("limit")
                if not self._admitted(message):
                    raise ValueError("admission-stale")
                if first and app_stream:
                    if (
                        self._target_epoch is not None
                        and self._target_epoch != message["blocked_epoch"]
                    ):
                        self._drop_target()
                        raise ValueError("admission-stale")
                    self._require_prepared_target()
                if not first and not self._can_reopen():
                    raise ValueError("not-prepared")
                self.selected_tracks = (
                    message.get(
                        "tracks",
                        ["mic"]
                        + (
                            ["far"]
                            if self.far["kind"] != "none" and self.track_states["far"] != "ended"
                            else []
                        ),
                    )
                    if action == "meeting.capture.resume"
                    else (["mic", "far"] if self.far["kind"] != "none" else ["mic"])
                )
                if not first and app_stream and self.track_states["far"] == "ended":
                    self.selected_tracks = [
                        track for track in self.selected_tracks if track != "far"
                    ]
                if not self.selected_tracks or (
                    "far" in self.selected_tracks and self.far["kind"] == "none"
                ):
                    raise ValueError("not-prepared")
                if "mic_device" in message:
                    self.mic_device = message["mic_device"]
                self._open(message)
            elif action == "meeting.capture.track.resume":
                if (
                    self.state != "recording"
                    or self.track_states["mic"] != "paused"
                    or self.opening
                    or self.close_deadline is not None
                    or not self._admitted(message)
                ):
                    raise ValueError("not-prepared")
                self._resume_microphone(message)
            elif action in {"meeting.capture.stop", "meeting.capture.pause"}:
                if action == "meeting.capture.stop":
                    self._drop_target()
                if action == "meeting.capture.pause":
                    reason = message["reason"]
                    if (
                        reason in {"lock", "unknown"}
                        and not self.session.snapshot().lock_signal_unavailable
                    ):
                        if reason != "lock" or not self.session.snapshot().locked:
                            self.session.signal("Lock" if reason == "lock" else "unknown")
                    if (
                        reason in {"lock", "unknown"}
                        and self.session.snapshot().lock_signal_unavailable
                    ):
                        return self._remember(
                            message, [self._reply("meeting.ack", message, **self._base())]
                        )
                    self._add_reason(reason)
                if self.finalizing or self.close_deadline is not None:
                    if action == "meeting.capture.stop":
                        self.state = "stopped"
                        self.auto_resume_pending = False
                    replies = [self._reply("meeting.ack", message, **self._base())]
                    if self.close_deadline is None:
                        replies.append(
                            self._reply(
                                "meeting.capture.closed", message, **self._base(), tracks=[]
                            )
                        )
                    if message["req"] not in self.ledger.requests:
                        # Overflow control has one current waiter per action,
                        # not an unbounded second request journal.
                        self.close_waiters = [
                            waiter
                            for waiter in self.close_waiters
                            if waiter["req"] in self.ledger.requests or waiter["type"] != action
                        ]
                    self.close_waiters.append(message)
                    if message["req"] in self.ledger.requests:
                        self.pending[message["req"]] = list(replies)
                    return self._remember(message, replies)
                if self.state != "recording" and not self.opening:
                    return self._remember(message, self._already_closed(message))
                self._close(message, message.get("reason", "stop"))
            else:
                raise ValueError("not-prepared")
            reply = self._reply("meeting.ack", message, **self._base())
            if message["req"] in self.ledger.requests:
                self.pending[message["req"]] = [reply]
            return self._remember(message, [reply])
        except Exception as exc:
            code = (
                str(exc)
                if isinstance(exc, ValueError)
                and str(exc)
                in {
                    "not-prepared",
                    "admission-stale",
                    "dir-unwritable",
                    "target-mismatch",
                    "target-gone",
                    "limit",
                    "io",
                }
                else "audio-failed"
            )
            return self._remember(message, [failure(message, code)])

    def _detector(self, request: Message) -> None:
        starting = request["type"] == "meeting.detector.start"
        if starting:
            reply = self._reply(
                "meeting.detector.started", request, apps_version=request["apps_version"]
            )
        else:
            reply = self._reply("meeting.detector.stopped", request)
        self.messages.put({"type": "detector-result", "request": request, "reply": reply})
        if starting:
            self.observer.start()
        else:
            self.observer.stop()
            self.messages.put({"type": "detector-clear"})

    def _drop_target(self) -> None:
        self.prepared_target = None
        self.capture_target = None
        self._prepared_anchor = None
        self._target_epoch = None

    def _can_reopen(self) -> bool:
        return self.far["kind"] != "app_stream" or (
            self.capture_target is not None and self._prepared_anchor is None
        )

    def _require_prepared_target(self) -> None:
        target, anchor = self.prepared_target, self._prepared_anchor
        if (
            target is None
            or anchor is None
            or not self.clock() - target.measured_at < TARGET_SECONDS
            or self.calls.anchor_for(self.far["call_id"]) is not anchor
        ):
            self._drop_target()
            raise ValueError("target-gone")

    def _prepare(self, request: Message, arrived_at: float) -> None:
        self.prepare_starts += 1
        self.calls.cancel_offer_retries()
        self._drop_target()
        self.preparing = True
        self.prepare_request = request
        self.prepare_deadline = self.clock() + 5.0
        self.directory = Path(request["dir"])
        self.identity = {}
        self.state = "stopped"
        far = request["far"]
        app_stream = far["kind"] == "app_stream"
        blocked_epoch = self.session.snapshot().blocked_epoch
        anchor = self.calls.anchor_for(far["call_id"]) if app_stream else None
        checker = self.calls.start_checker() if anchor is not None else None
        # The prepare thread owns the backend until its result returns to poll().
        control = _PreparedControl(self._control_generation, self.backend)
        self.backend = None

        def work() -> None:
            event: Message = {"request": request, "control": control}
            target = None
            try:
                if app_stream:
                    if anchor is None:
                        event.update(start_code=DetectorCode.TARGET_GONE, start_duration=0)
                        raise ValueError("target-gone")
                    from astra_voice.worker.meeting_identity_check import (
                        CHECK_SECONDS,
                        CheckRequest,
                        Purpose,
                        TargetError,
                    )

                    check = self.calls.run_start(
                        checker,
                        CheckRequest(Purpose.START, None, anchor, arrived_at + CHECK_SECONDS),
                    )
                    event["start_duration"] = max(0, int((self.clock() - arrived_at) * 1000))
                    target = check.target
                    if not (
                        check.settled
                        and check.variant1
                        and target is not None
                        and 1 <= len(target.streams) <= 8
                    ):
                        error = check.target_error or TargetError.MISMATCH
                        diagnostic = result_code(check)
                        event["start_code"] = (
                            diagnostic
                            if diagnostic not in (None, DetectorCode.NO_CALL)
                            else DetectorCode(error)
                        )
                        raise ValueError(error)
                assert self.directory is not None
                if self.directory.name != request["meeting_id"]:
                    raise ValueError("dir-unwritable")
                with private_directory(self.directory) as directory:
                    try:
                        os.stat("capture.json", dir_fd=directory, follow_symlinks=False)
                    except FileNotFoundError:
                        pass
                    else:
                        raise ValueError("not-prepared")
                free = self.free_mb()
                if free < 600:
                    raise ValueError("limit")
                # Bind the result to the generation used for the control context.
                generation = self._control_generation
                if control.generation != generation:
                    if app_stream:
                        raise ValueError("target-gone")
                    self._retire_backend(control.backend)
                    control.backend = None
                    control.generation = generation
                backend = control.backend if control.backend is not None else self.backend_factory()
                control.backend = backend
                info = backend.prepare(far, target=target) if app_stream else backend.prepare(far)
                own_context = getattr(backend, "own_context", None)
                control.context = own_context() if own_context else None
                self.messages.put(
                    {
                        **event,
                        "type": "prepared",
                        "info": info,
                        "free": free,
                        "target": target,
                        "anchor": anchor,
                        "blocked_epoch": blocked_epoch,
                    }
                )
            except Exception as exc:
                code = (
                    str(exc)
                    if isinstance(exc, ValueError)
                    and str(exc)
                    in {"limit", "target-gone", "target-mismatch", "dir-unwritable", "not-prepared"}
                    else "audio-failed"
                )
                if app_stream and code in {"target-gone", "target-mismatch"}:
                    event.setdefault("start_code", DetectorCode(code))
                self.messages.put({**event, "type": "prepare-failed", "code": code})

        threading.Thread(target=work, name="meeting-prepare", daemon=True).start()

    def _already_closed(self, request: Message) -> list[Message]:
        if request["type"] == "meeting.capture.stop":
            self.state = "stopped"
        elif self.state != "stopped":
            self.state = "paused"
            self._begin_pause()
        counts = dict(self.archive.counts) if self.archive else {"mic": 0, "far": 0}
        intervals = deepcopy(self.archive.intervals) if self.archive else []
        replies = [
            self._reply("meeting.capture.closed", request, **self._base(), tracks=[]),
            self._reply(
                "meeting.capture.finalized",
                request,
                **self._base(),
                segments=counts,
                intervals=intervals,
                status="failed"
                if self.archive and self.archive.failed.is_set()
                else self.final_status,
            ),
        ]
        if request["type"] == "meeting.capture.pause":
            replies.append(
                self._reply(
                    "meeting.capture.paused",
                    request,
                    **self._base(),
                    **self._pause(),
                    at_recorded_ms=self.recorded_ms,
                    segments_closed=sum(counts.values()),
                )
            )
        else:
            replies.append(
                self._reply(
                    "meeting.capture.stopped",
                    request,
                    **self._base(),
                    recorded_ms=self.recorded_ms,
                    segments=counts,
                    lost_estimate_ms=self.lost_estimate_ms,
                )
            )
        return replies

    def _complete_waiters(self, result: list[Message]) -> None:
        """One physical close, independent correlated durable outcomes for all requests."""
        waiters, self.close_waiters = self.close_waiters, []
        for waiter in waiters:
            for reply in self._already_closed(waiter)[1:]:
                result.append(self._outcome(waiter, reply))

    def _sample_disk(self) -> None:
        if self.disk_sampling:
            return
        self.disk_sampling = True

        def sample() -> None:
            try:
                free = self.free_mb()
                self.messages.put({"type": "disk-space", "free": max(0, free)})
            except Exception:
                self.messages.put({"type": "disk-space", "free": 0})

        threading.Thread(target=sample, name="meeting-disk", daemon=True).start()

    def _guard_source(
        self, source: Source, request: Message, stop: threading.Event, *, opening: bool = False
    ) -> None:
        # Meeting-only adapters consult this again at native stream_new/connect;
        # context selection may have blocked after the outer admission check.
        guarded: Any = source
        guarded.admission_guard = lambda: self.session.lock
        guarded.admission_check = lambda: (
            not stop.is_set()
            and not self.session_terminal
            and (self.pause_at is None or self.clock() - self.pause_at < 3600)
            and self.session.allowed(request["blocked_epoch"])
            and (not opening or request.get("opening_complete", False) or self._admitted(request))
        )

    def _open(self, request: Message) -> None:
        assert self.directory is not None
        if self.archive is None:
            self.archive = _Archive(
                self.directory, self._base(), self.index, self.fsync, self.messages.put
            )
        self.audio_stop = threading.Event()
        self.opening = True
        self.auto_resume_pending = False
        pause_before = self.pause_reasons.copy(), self.manual_resume_required
        self.pause_reasons.clear()
        self.manual_resume_required = False
        self.open_request = request
        stop = self.audio_stop
        self.used_meetings.add(self.identity["meeting_id"])
        backend = self._get_backend()
        first = request["type"] == "meeting.capture.open"
        if first:
            target = self.capture_target = self.prepared_target
        else:
            target = self.capture_target
        far = self.far

        def start() -> None:
            far_source: Source | None = None
            far_ended = False
            lost_streams: tuple[PinnedStream, ...] = ()
            try:
                free = self.free_mb()
                self.messages.put({"type": "disk-space", "free": free})
                if free < 600:
                    raise ValueError("limit")
                self.sources = {}
                for track in self.selected_tracks:
                    if stop.is_set() or not self._admitted(request):
                        raise ValueError("admission-stale")
                    try:
                        if track == "mic":
                            source = self.mic_factory()
                        elif far["kind"] == "app_stream":
                            source = backend.source(far, target=target)
                            if target is not None and len(target.streams) > 1:
                                cast(Any, source).strict = first
                        else:
                            source = backend.source(far)
                        if track == "far":
                            far_source = source
                        self.sources[track] = source
                        self._guard_source(source, request, stop, opening=True)
                        if stop.is_set() or not self._admitted(request):
                            raise ValueError("admission-stale")
                        source.open(self.mic_device if track == "mic" else None)
                        if track == "far":
                            lost_streams = getattr(source, "lost_streams", ())
                    except ValueError as exc:
                        if (
                            first
                            or track != "far"
                            or far["kind"] != "app_stream"
                            or str(exc) not in {"target-gone", "target-mismatch"}
                        ):
                            raise
                        far_ended = True
                        failed_source = self.sources.get(track)
                        if failed_source is not None:
                            lost_streams = getattr(failed_source, "lost_streams", ())
                            failed_source.shutdown()
                            del self.sources[track]
                        continue
                    if stop.is_set() or not self._admitted(request):
                        source.shutdown()
                        raise ValueError("admission-stale")
                self.messages.put(
                    {
                        "type": "opened",
                        "request": request,
                        "far_ended": far_ended,
                        "lost_streams": lost_streams,
                        "pause_before": pause_before,
                        "stop": stop,
                    }
                )
                # Control activates the disk/reader edge after observing open completion.
            except Exception as exc:
                lost_streams = getattr(far_source, "lost_streams", lost_streams)
                self.messages.put(
                    {
                        "type": "open-failed",
                        "request": request,
                        "stop": stop,
                        "lost_streams": lost_streams,
                        "far_ended": far_ended
                        or bool(target is not None and len(lost_streams) == len(target.streams)),
                        "code": str(exc)
                        if str(exc) in {"admission-stale", "target-gone", "target-mismatch"}
                        else "audio-failed",
                    }
                )

        self.open_thread = threading.Thread(target=start, name="meeting-open", daemon=True)
        self.open_thread.start()

    def _resume_microphone(self, request: Message) -> None:
        self.opening = True
        stop = self.audio_stop

        def start() -> None:
            try:
                old = self.readers.get("mic")
                if old:
                    old.join()
                if stop.is_set() or not self._admitted(request):
                    raise ValueError("admission-stale")
                source = self.mic_factory()
                self.sources["mic"] = source
                self._guard_source(source, request, stop, opening=True)
                if stop.is_set() or not self._admitted(request):
                    raise ValueError("admission-stale")
                source.open(request["mic_device"])
                if stop.is_set() or not self._admitted(request):
                    source.shutdown()
                    raise ValueError("admission-stale")
                self.messages.put({"type": "mic-opened", "request": request})
            except Exception as exc:
                self.messages.put(
                    {
                        "type": "open-failed",
                        "request": request,
                        "code": "admission-stale"
                        if str(exc) == "admission-stale"
                        else "audio-failed",
                    }
                )

        self.open_thread = threading.Thread(target=start, name="meeting-mic-open", daemon=True)
        self.open_thread.start()

    def _start_reader(self, track: str, source: Source) -> None:
        thread = threading.Thread(
            target=self._read,
            args=(track, source, self.audio_stop),
            name=f"meeting-{track}",
            daemon=True,
        )
        self.readers[track] = thread
        thread.start()

    def _read(self, track: str, source: Source, stop: threading.Event) -> None:
        assert self.archive is not None
        archive = self.archive
        next_read = time.monotonic()
        # The opener already reported the initial losses in "opened".
        lost_count = len(getattr(source, "lost_streams", ())) if track == "far" else 0
        try:
            while not stop.is_set():
                try:
                    data = source.read_chunk()
                finally:
                    if track == "far":
                        lost = getattr(source, "lost_streams", ())
                        if len(lost) > lost_count:
                            self.messages.put(
                                {"type": "far-partial", "lost_streams": lost[lost_count:]}
                            )
                            lost_count = len(lost)
                far_ended = data is None and track == "far" and getattr(source, "ended", False)
                if stop.is_set() and not far_ended:
                    return
                change = getattr(source, "change", None)
                if change:
                    archive.put("track_close", (track, "target_changed"))
                    archive.put("track_start", (track, source.sample_clock(), self._recorded()))
                    self.messages.put({"type": "track-change", "track": track, "kind": change})
                if data is None:
                    reason = (
                        "far_ended"
                        if far_ended
                        else (
                            "mic_change"
                            if track == "mic" and getattr(source, "device_change", None)
                            else "audio_failed"
                        )
                    )
                    archive.put("track_close", (track, reason))
                    self.messages.put({"type": "track-ended", "track": track, "reason": reason})
                    if not stop.is_set():
                        source.shutdown()
                    return
                if data and not archive.put(
                    "pcm", (track, data, source.sample_clock(), self._recorded())
                ):
                    # This whole chunk was rejected. Preserve the known loss in
                    # the capture outcome even if the queued prefix later fsyncs.
                    self.messages.put(
                        {"type": "disk-failed", "lost_estimate_ms": (len(data) + 31) // 32}
                    )
                    return
                if getattr(source, "device_change", None) is not None:
                    # Drain the bounded pre-change tail and publish the already
                    # observed cutoff without adding another cadence interval.
                    continue
                # One native dispatch per 100ms batch, including empty input.
                # Wait outside source locks; stop wakes this wait immediately.
                # Sources drain up to 1s of queued PCM to catch up after stalls.
                next_read += 0.1
                now = time.monotonic()
                if next_read < now:
                    next_read = now
                stop.wait(min(0.1, max(0.0, next_read - now)))
        except Exception:
            self.messages.put({"type": "audio-failed"})

    def _close(self, request: Message | None, reason: str) -> None:
        self._far_partial_pending = False
        if reason in {"lease", "indicators"} and self.pause_at is None:
            self.lease_tracks = {
                track for track, state in self.track_states.items() if state == "live"
            }
        self._add_reason(reason)
        if self._prepared_anchor is not None:
            self._drop_target()
        if reason in {"stop", "limit", "sleep", "session"}:
            self.state = "stopped"
            self.auto_resume_pending = False
            self._drop_target()
        if self.close_deadline is not None:
            return
        self.recorded_ms = self._recorded()
        self.active_at = None
        self.audio_stop.set()
        self.identity["state_rev"] += 1
        self.state = "stopped" if reason in {"stop", "limit", "sleep", "session"} else "paused"
        if self.state == "paused":
            self._begin_pause()
        # Poll(.5s) + three bounded reads(.225s) + role tick(.05s)
        # reserve .2s for physical close on lock/unknown; timeout escalates.
        budget = 0.2 if reason in {"lock", "unknown"} else CLOSE_SECONDS
        self.close_deadline = self.clock() + budget
        if reason == "lease":
            self.close_deadline = min(self.close_deadline, self.last_lease + 5.0)
        self.close_wall = time.monotonic() + budget
        self.closing_request = request
        self.close_reason = reason
        self.close_mono_ms = int(self.clock() * 1000)
        opener = self.open_thread
        readers = list(self.readers.values())

        def close() -> None:
            if opener:
                opener.join()
            sources = dict(self.sources)
            results: queue.SimpleQueue[bool] = queue.SimpleQueue()

            def one(source: Source) -> None:
                try:
                    source.shutdown()
                    results.put(True)
                except Exception:
                    results.put(False)

            threads = [
                threading.Thread(target=one, args=(s,), daemon=True) for s in sources.values()
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            for reader in readers:
                reader.join()
            if all(results.get() for _ in threads):
                self.messages.put(
                    {
                        "type": "closed",
                        "request": request,
                        "tracks": list(sources),
                        "closed_wall": time.monotonic(),
                        "closed_mono": self.clock(),
                    }
                )

        threading.Thread(target=close, name="meeting-close", daemon=True).start()

    def _track_event(self, track: str, state: str, kind: str) -> Message:
        self.track_states[track] = state
        return {
            "type": "meeting.capture.track",
            **self._base(),
            "track": track,
            "state": state,
            "kind": kind,
            "at_recorded_ms": self._recorded(),
        }

    def _note_far_losses(self, lost: tuple[PinnedStream, ...]) -> bool:
        target = self.capture_target
        if target is None or not lost:
            return False
        streams = tuple(stream for stream in target.streams if stream not in lost)
        if not streams:
            # Keep the target for mic-only resume. The ended track excludes far.
            self._far_partial_pending = False
        elif streams != target.streams:
            self.capture_target = replace(target, streams=streams)
            self._far_incomplete = True
            if self.state == "recording" and self.track_states["far"] == "live":
                self._far_partial_pending = True
            return True
        return False

    def _finish_prepare(self, event: Message, result: list[Message]) -> None:
        control: _PreparedControl = event["control"]
        if not self.preparing or event["request"] != self.prepare_request:
            self._retire_backend(control.backend)
            return
        self.preparing = False
        self.prepare_deadline = None
        request = event["request"]
        code = event.get("code")
        if control.generation == self._control_generation:
            self.backend = control.backend
            if code is None:
                self.observer.registry.set("control", control.context)
        else:
            self._retire_backend(control.backend)
            if code is None and request["far"]["kind"] != "app_stream":
                code = "audio-failed"
        target, anchor = event.get("target"), event.get("anchor")
        start_code = event.get("start_code")
        if target is not None and self.calls.anchor_for(request["far"]["call_id"]) is not anchor:
            code = "target-gone"
            start_code = DetectorCode.TARGET_GONE
        if start_code is not None:
            self.calls.finish_start(start_code, event["start_duration"], result)
        if code is not None:
            self._drop_target()
            result.append(self._outcome(request, failure(request, code)))
            return
        self.prepared_target, self._prepared_anchor = target, anchor
        self._target_epoch = event["blocked_epoch"] if target is not None else None
        if self.archive:
            self.archive.put("quit", None)
            self.archive = None
        self.identity = {key: request[key] for key in ("meeting_id", "meeting_generation")}
        self.identity.update(capture_id=uuid.uuid4().hex, state_rev=0)
        self.cached_free_mb = event["free"]
        self.far, self.mic_device = request["far"], request.get("mic_device")
        self.limit_ms, self.extends = 7200000, 0
        self.final_status = "succeeded"
        self.lost_estimate_ms = 0
        self.track_states = {"mic": "ended", "far": "ended"}
        self._far_incomplete = False
        self._far_partial_pending = False
        self._last_far_partial = float("-inf")
        self.index = request["segment_index_from"]
        self.state, self.recorded_ms = "prepared", 0
        self.pause_reasons.clear()
        self.manual_resume_required = False
        self.session_terminal = False
        self.auto_resume_pending = False
        self.indicators_visible = False
        self.recovery_admission = self.recovery = None
        self.pause_at = None
        self.pause_epoch = None
        self.last_keepalive_seq = -1
        self.far_available = event["info"]["far_available"]
        result.append(
            self._outcome(
                request,
                self._reply(
                    "meeting.capture.prepared",
                    request,
                    **self._base(),
                    **self._pause(),
                    mic_label="",
                    **event["info"],
                ),
            )
        )

    def _queue_far_ended(self) -> None:
        self.messages.put({"type": "track-ended", "track": "far", "reason": "far_ended"})

    def _first_app_open(self, request: Message) -> bool:
        return bool(request["type"] == "meeting.capture.open" and self.far["kind"] == "app_stream")

    def _app_anchor_revoked(self) -> bool:
        return (
            self._prepared_anchor is None
            or self.calls.anchor_for(self.far["call_id"]) is not self._prepared_anchor
        )

    def _finish_open(self, event: Message, result: list[Message]) -> None:
        request = event["request"]
        first_app_open = self._first_app_open(request)
        stop = event.get("stop")
        if stop is not None and stop is not self.audio_stop:
            return
        new_loss = self._note_far_losses(event.get("lost_streams", ()))
        if event.get("far_ended"):
            self._queue_far_ended()
        if first_app_open and stop is not None and stop.is_set():
            # A physical close may already have completed earlier in this poll.
            self.opening = False
            return
        if "code" in event:
            code = event["code"]
            if (
                first_app_open
                and self.close_deadline is None
                and code != "admission-stale"
                and self._app_anchor_revoked()
            ):
                code = "target-gone"
            self._fail_open(request, code, result)
            return
        self.opening = False
        self._session_events(result)
        if self.close_deadline is not None or self.stopped or not self._admitted(request):
            if self.close_deadline is None:
                self._close(None, self._unadmitted_open_reason(request))
            return
        if first_app_open and self._app_anchor_revoked():
            self._fail_open(request, "target-gone", result)
            return
        if not self.sources:
            self.pause_reasons, self.manual_resume_required = event["pause_before"]
            if not request.get("automatic"):
                result.append(self._outcome(request, failure(request, "not-prepared")))
            return
        self.identity["state_rev"] += 1
        self.state = "recording"
        if request["type"] == "meeting.capture.open":
            self._prepared_anchor = None
            self._target_epoch = None
        # Subsequent native reconnects retain the local-session/stop guard.
        request["opening_complete"] = True
        if request.get("automatic"):
            self.recovery = {
                "state_rev": self.identity["state_rev"],
                "reason": request["recovery_reason"],
                "duration_ms": max(0, int((self.clock() - self.pause_at) * 1000))
                if self.pause_at is not None
                else 0,
            }
        self.pause_at = None
        self.pause_epoch = None
        self.active_at = self.clock()
        self.last_lease = self.last_disk = self.clock()
        assert self.archive is not None
        edge = {
            "kind": "open",
            "t_mono_ms": int(self.clock() * 1000),
            "recorded_ms": self.recorded_ms,
            "reason": None,
            "state_rev": self.identity["state_rev"],
            "source_kind": self.far["kind"] if self.far["kind"] != "none" else "mic_only",
        }
        self.archive.put("start", edge)
        for track, source in self.sources.items():
            self.track_states[track] = "live"
            self.archive.put("track_start", (track, source.sample_clock(), self.recorded_ms))
            self._start_reader(track, source)
        if self.track_states["far"] == "live" and (
            new_loss or (self._far_incomplete and self._last_far_partial == float("-inf"))
        ):
            self._far_partial_pending = True
        if request.get("automatic"):
            self.last_alive = 0
            return
        result.append(
            self._outcome(
                request,
                {
                    "type": "meeting.capture.started",
                    **self._base(),
                    **self._pause(),
                    "t0_mono_ms": edge["t_mono_ms"],
                    "tracks": list(self.sources),
                },
            )
        )

    def _fail_open(self, request: Message, code: str, result: list[Message]) -> None:
        self.opening = False
        if request["type"] == "meeting.capture.open":
            self._drop_target()
        if self.close_deadline is None:
            self._close(
                None,
                self._unadmitted_open_reason(request)
                if request.get("automatic") and code == "admission-stale"
                else "audio_failed",
            )
        if not request.get("automatic"):
            result.append(self._outcome(request, failure(request, code)))

    def poll(self) -> list[Message]:
        result: list[Message] = []
        checks: list[Message] = []
        server_events: list[Message] = []
        prepare_result: Message | None = None
        app_open: Message | None = None
        self.calls.sync_automation(result)
        self._session_events(result)
        self._expire_pause()
        while True:
            try:
                event = self.messages.get_nowait()
            except queue.Empty:
                break
            kind = event["type"]
            if kind == "session-event":
                result.append(event["event"])
            elif kind == "detector-result":
                result.append(self._outcome(event["request"], event["reply"]))
            elif kind == "detector-code":
                result.append(event["event"])
            elif kind == "detector-clear":
                self.calls.detector_stopped(result)
            elif kind == "card-remote":
                self.calls.remote_card(result)
            elif kind == "card-cancelled":
                self.calls.finish_card(event["request"], {"active": False}, result)
            elif kind == "check-result":
                checks.append(event)
                self.calls.note_result(event)
            elif kind == "detector-server-changed":
                server_events.append(event)
            elif kind in {"detector-candidate", "detector-call-ended"}:
                self.calls.observer_event(event, result)
            elif kind in {"prepared", "prepare-failed"}:
                prepare_result = event
            elif kind == "disk-space":
                self.disk_sampling = False
                self.cached_free_mb = event["free"]
                if self.cached_free_mb < 200:
                    self._autopause("disk", "disk-write", result)
            elif kind in {"opened", "open-failed"}:
                if self._first_app_open(event["request"]):
                    app_open = event
                else:
                    self._finish_open(event, result)
            elif kind == "mic-opened":
                self.opening = False
                self._session_events(result)
                if (
                    self.close_deadline is not None
                    or self.terminating
                    or not self._admitted(event["request"])
                ):
                    if self.close_deadline is None:
                        self._close(None, self.session.snapshot().reason or "unknown")
                    continue
                self.identity["state_rev"] += 1
                event["request"]["opening_complete"] = True
                assert self.archive is not None
                self.archive.put(
                    "track_start", ("mic", self.sources["mic"].sample_clock(), self._recorded())
                )
                self.archive.put(
                    "edge",
                    {
                        "kind": "mic_gap_end",
                        "t_mono_ms": int(self.clock() * 1000),
                        "recorded_ms": self._recorded(),
                        "reason": "mic_change",
                    },
                )
                self._start_reader("mic", self.sources["mic"])
                result.append(
                    self._outcome(event["request"], self._track_event("mic", "live", "reopened"))
                )
            elif kind == "closed":
                if (
                    self.close_wall is None
                    or event["closed_wall"] > self.close_wall
                    or event["closed_mono"] > (self.close_deadline or 0)
                ):
                    self.state = "closing-unknown"
                    self.stopped, self.exit_code = True, 3
                    continue
                self.close_deadline = self.close_wall = None
                self.sources.clear()
                request = event["request"]
                self.finalizing = True
                result.append(
                    self._outcome(
                        request,
                        {
                            "type": "meeting.capture.closed",
                            **self._base(),
                            "tracks": event["tracks"],
                        },
                    )
                )
                for waiter in self.close_waiters:
                    result.append(
                        self._outcome(
                            waiter,
                            {
                                "type": "meeting.capture.closed",
                                **self._base(),
                                "tracks": event["tracks"],
                            },
                        )
                    )
                for track in event["tracks"]:
                    if self.track_states[track] == "live":
                        self.track_states[track] = "paused" if self.state == "paused" else "ended"
                if self.archive is not None:
                    if not self.archive.put(
                        "close",
                        {
                            "kind": "close",
                            "reason": self.close_reason,
                            "state_rev": self.identity["state_rev"],
                            "t_mono_ms": self.close_mono_ms,
                            "recorded_ms": self.recorded_ms,
                            "request": request,
                            **{
                                key: value
                                for key, value in self._pause().items()
                                if key != "lock_signal_unavailable"
                            },
                        },
                    ):
                        self.finalizing = False
                        self.final_status = "failed"
                        result.append(
                            self._outcome(
                                request,
                                {
                                    "type": "meeting.capture.finalized",
                                    **self._base(),
                                    "segments": dict(self.archive.counts),
                                    "intervals": [],
                                    "status": "failed",
                                },
                            )
                        )
                        self._complete_waiters(result)
                else:
                    self.finalizing = False
            elif kind == "finalized":
                self.finalizing = False
                # Successful fsync of a prefix cannot undo rejected PCM or an
                # earlier disk failure in this capture session.
                self.final_status = max(
                    self.final_status, event["status"], key=FINAL_STATUS_RANK.__getitem__
                )
                request = event["request"]
                result.append(
                    self._outcome(
                        request,
                        {
                            "type": "meeting.capture.finalized",
                            **self._base(),
                            **{k: event[k] for k in ("segments", "intervals")},
                            "status": self.final_status,
                        },
                    )
                )
                if request:
                    response = (
                        {
                            "type": "meeting.capture.paused",
                            **self._base(),
                            **self._pause(),
                            "at_recorded_ms": self.recorded_ms,
                            "segments_closed": sum(event["segments"].values()),
                        }
                        if request["type"] == "meeting.capture.pause"
                        else {
                            "type": "meeting.capture.stopped",
                            **self._base(),
                            "recorded_ms": self.recorded_ms,
                            "segments": event["segments"],
                            "lost_estimate_ms": self.lost_estimate_ms,
                        }
                    )
                    result.append(self._outcome(request, response))
                self._complete_waiters(result)
            elif kind == "segment":
                segment = event["sidecar"]
                fields = (
                    "track",
                    "index",
                    "frames",
                    "closed",
                    "epoch_id",
                    "clock",
                    "clock_anchor",
                    "sample_start",
                    "sample_end",
                    "discontinuity",
                    "close_reason",
                    "pause_reasons",
                )
                result.append(
                    {
                        "type": "meeting.capture.segment",
                        **self._base(),
                        **{key: segment[key] for key in fields},
                    }
                )
            elif kind == "far-partial":
                self._note_far_losses(event["lost_streams"])
            elif kind == "track-ended":
                track, reason = event["track"], event["reason"]
                self.lease_tracks.discard(track)
                if reason == "audio_failed":
                    self._autopause("audio_failed", "audio-failed", result)
                else:
                    if track == "far":
                        self._far_partial_pending = False
                    result.append(
                        self._track_event(
                            track,
                            "ended" if track == "far" else "paused",
                            "target-removed" if track == "far" else "device-lost",
                        )
                    )
                    if self.archive:
                        edge_mono_ms = (
                            self.close_mono_ms
                            if track == "far" and self.close_deadline is not None
                            else int(self.clock() * 1000)
                        )
                        self.archive.put(
                            "edge",
                            {
                                "kind": "far_ended" if track == "far" else "mic_gap_start",
                                "t_mono_ms": edge_mono_ms,
                                "recorded_ms": self._recorded(),
                                "reason": reason,
                            },
                        )
            elif kind == "track-change":
                result.append(self._track_event(event["track"], "live", event["kind"]))
            elif kind in {"disk-failed", "audio-failed"}:
                if kind == "disk-failed":
                    self.final_status = "failed"
                    self.lost_estimate_ms += event.get("lost_estimate_ms", 0)
                if kind == "disk-failed" and self.finalizing:
                    self.finalizing = False
                    result.append(
                        self._outcome(
                            self.closing_request,
                            {
                                "type": "meeting.capture.finalized",
                                **self._base(),
                                "segments": dict(self.archive.counts)
                                if self.archive
                                else {"mic": 0, "far": 0},
                                "intervals": [],
                                "status": "failed",
                            },
                        )
                    )
                    self._complete_waiters(result)
                self._autopause(
                    "disk" if kind == "disk-failed" else "audio_failed",
                    "disk-write" if kind == "disk-failed" else "audio-failed",
                    result,
                )
        # Apply revocations from the entire batch before publishing app-stream audio.
        server_changed = self.calls.resolve_server(server_events, checks, result)
        if server_changed:
            self._reset_control()
        self.calls.sync_automation(result)
        if prepare_result is not None:
            self._finish_prepare(prepare_result, result)
        if app_open is not None:
            self._finish_open(app_open, result)
        if server_changed:
            self._far_partial_pending = False
        self._session_events(result)
        self._expire_pause()
        self._maybe_auto_resume()
        now = self.clock()
        if self.close_deadline is not None and (
            now >= self.close_deadline or time.monotonic() >= (self.close_wall or 0)
        ):
            self.state = "closing-unknown"
            self.stopped, self.exit_code = True, 3
            # No closed ACK. Supervisor escalates and confirms wait/poll before reuse.
        if self.prepare_deadline is not None and now >= self.prepare_deadline:
            self.preparing = False
            self._drop_target()
            self.stopped, self.exit_code = True, 3
            self.prepare_deadline = None
            assert self.prepare_request is not None
            result.append(
                self._outcome(self.prepare_request, failure(self.prepare_request, "timeout"))
            )
        if self.state == "recording":
            if now - self.last_disk >= 10:
                self.last_disk = now
                self._sample_disk()
            if self.state == "recording" and now - self.last_lease >= 3.95:
                self._autopause("lease", "lease", result)
            if self.state == "recording" and self._recorded() >= self.limit_ms:
                self._close(None, "limit")
        if (
            self._far_partial_pending
            and self.state == "recording"
            and self.track_states["far"] == "live"
            and now - self._last_far_partial >= 1
        ):
            self._far_partial_pending = False
            self._last_far_partial = now
            result.append(self._track_event("far", "live", "partial"))
        if self.identity and now - self.last_alive >= 1 and self.state != "closing-unknown":
            self.last_alive, self.seq = now, self.seq + 1
            elapsed = now - self.cpu_at
            cpu_time = time.process_time()
            cpu_pct = (
                min(100.0, max(0.0, (cpu_time - self.cpu_time) * 100 / elapsed))
                if elapsed > 0
                else 0.0
            )
            self.cpu_at, self.cpu_time = now, cpu_time
            result.append(
                {
                    "type": "meeting.capture.alive",
                    **self._base(),
                    **self._pause(),
                    "seq": self.seq,
                    "state": self.state,
                    "recorded_ms": self._recorded(),
                    "tracks": dict(self.track_states),
                    "free_mb": self.cached_free_mb,
                    "cpu_pct": cpu_pct,
                    **({"recovery": dict(self.recovery)} if self.recovery else {}),
                }
            )
        if (
            self.terminating
            and self.close_deadline is None
            and (not self.finalizing or time.monotonic() >= (self.termination_deadline or 0))
        ):
            if self.shutdown_request is not None:
                result.append(
                    self._outcome(
                        self.shutdown_request,
                        self._reply("meeting.capture.bye", self.shutdown_request),
                    )
                )
                self.shutdown_request = None
            self.stopped = True
            if self.archive:
                self.archive.put("quit", None)
        for event in checks:
            self.calls.finish(
                event["attempt"],
                event["result"],
                result,
                report_remote_offer=event.get("report_remote_offer", False),
                measured_server=event.get("measured_server"),
            )
        self.calls.retry_offers()
        return result

    def _autopause(self, reason: str, wire_reason: str, result: list[Message]) -> None:
        if self.state in {"stopped", "closing-unknown"}:
            return
        added = self._add_reason(reason)
        if self._prepared_anchor is not None:
            self._drop_target()
        if self.state == "recording" or self.opening:
            self._close(None, reason)
        elif self.state == "prepared":
            self.state = "paused"
            self._begin_pause()
        if not added:
            return
        result.append(
            {
                "type": "meeting.capture.autopaused",
                **self._base(),
                **self._pause(),
                "reason": wire_reason,
                "at_recorded_ms": self.recorded_ms,
            }
        )

    def terminate(self) -> None:
        if self.terminating:
            return
        self.terminating = True
        self._drop_target()
        self.termination_deadline = time.monotonic() + 1.0
        self.observer.stop(exiting=True)
        self.calls.terminate()
        self.preparing = False
        self.prepare_deadline = None
        if self.sources or self.opening:
            self._close(None, "stop")
        elif not self.finalizing:
            self.stopped = True
            if self.archive:
                self.archive.put("quit", None)
        self.calls.shutdown(self.termination_deadline)

    def cleanup(self) -> None:
        """Transport loss/signal: bounded physical cleanup, storage may be abandoned."""
        self.terminate()  # Start physical close before bounded helper reaping.
        if self.session_monitor is not None:
            self.session_monitor.close()
        assert self.termination_deadline is not None
        deadline = self.termination_deadline
        while not self.stopped and time.monotonic() < deadline:
            self.poll()
            time.sleep(0.005)
        self.observer.join(max(0.0, deadline - time.monotonic()))
        self.calls.cleanup(deadline)
        # An escalated close can already have set stopped while storage is
        # finishing. It still owns a worker that must receive its exit request.
        if self.archive is not None:
            self.archive.put("quit", None)
            self.archive.thread.join(max(0.0, deadline - time.monotonic()))
