"""Meeting capture control, bounded audio queues, and independent durable writer.

The control loop never calls fsync, reads PCM, or waits for a native close. It
observes a fixed physical-close deadline and leaves unknown closure to process
supervision. A daemon storage worker cannot hold up microphone disconnection.
"""

from __future__ import annotations

import os
import queue
import threading
import time
import uuid
import wave
from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
from typing import Any, Protocol

from astra_voice.meetings.segments import CaptureJournal, ClockAnchor, SegmentSidecar
from astra_voice.meetings.store import MeetingStore, Writer
from astra_voice.security.files import private_directory
from astra_voice.worker import ipc
from astra_voice.worker.meeting_contract import COMMAND_FIELDS, RequestLedger
from astra_voice.worker.meeting_roles import failure
from astra_voice.worker.meeting_session import MeetingSession, SessionAdmission

Message = dict[str, Any]
MAX_PCM_BYTES = 128000  # two tracks, at most two seconds each
CLOSE_SECONDS = 1.0
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
    def prepare(self, far: Message) -> Message: ...
    def source(self, far: Message) -> Source: ...
    def detector_start(self) -> int: ...
    def detector_stop(self) -> None: ...
    def close(self) -> None: ...


def _backend() -> Backend:
    from astra_voice.worker.monitor_stream import CapturePulseBackend

    return CapturePulseBackend()


def _microphone() -> Source:
    from astra_voice.worker.monitor_stream import microphone_source

    return microphone_source()  # type: ignore[no-any-return]


class _Archive:
    """Single disk owner. Queue bounds PCM and refuses backlog without blocking audio."""

    def __init__(
        self,
        directory: Path,
        identity: Message,
        index: int,
        fsync: Callable[[int], None],
        emit_event: Callable[[Message], None],
    ) -> None:
        self.directory, self.identity = directory, identity
        self.fsync, self.emit_event = fsync, emit_event
        self.jobs: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=256)
        self.lock = threading.Lock()
        self.pending_bytes = 0
        self.failed = threading.Event()
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
        size = len(payload[1]) if kind == "pcm" else 0
        with self.lock:
            if self.failed.is_set() or self.pending_bytes + size > MAX_PCM_BYTES:
                return False
            try:
                self.jobs.put_nowait((kind, payload))
            except queue.Full:
                return False
            self.pending_bytes += size
        return True

    def _edge(self, payload: Message, owner: Any) -> None:
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
        owner.write("capture.json", self.journal)

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
            self._checkpoint(track, owner)
        except BaseException:
            file.close()
            raise

    def _checkpoint(self, track: str, owner: Any) -> None:
        wav, file, sidecar = self.segments[track]
        wav.writeframes(b"")  # update WAV header before fsync, including open segments
        file.flush()
        self.fsync(file.fileno())
        sidecar.frames_at_checkpoint = sidecar.frames
        owner.write(f"audio/{track}-{sidecar.index:04d}.json", sidecar)

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
            or stamp[1] < previous[1]
            or stamp[2] < previous[2]
            or any(
                abs(stamp[index] - previous[index] - duration_ms) > tolerance_ms for index in (1, 2)
            )
        )
        if changed:
            if track in self.segments:
                self.segments[track][2].discontinuity = True
            self._finish(track, "clock_change", owner)
        if track not in self.segments:
            # First sample precedes this end-of-buffer timestamp.
            start = (
                stamp[0],
                max(0, stamp[1] - len(data) // 32),
                max(0, stamp[2] - len(data) // 32),
            )
            self._start(
                track, start, recorded, audio, owner, new_epoch=not previous or bool(changed)
            )
        while data:
            wav, _, sidecar = self.segments[track]
            capacity = (4800000 - sidecar.frames) * 2
            block, data = data[:capacity], data[capacity:]
            wav.writeframesraw(block)
            sidecar.frames += len(block) // 2
            sidecar.sample_end += len(block) // 2
            mono = stamp[2] - len(data) // 32
            self.last_clock[track] = (stamp[0], stamp[1] - len(data) // 32, mono)
            if mono - sidecar.timeline[-1][1] >= 1000:
                sidecar.timeline.append([sidecar.sample_end, mono])
            if sidecar.frames == 4800000:
                self._finish(track, "segment", owner)
                if data:
                    self._start(
                        track, self.last_clock[track], recorded, audio, owner, new_epoch=False
                    )
            # Two seconds of PCM queueing fit inside the ten-second crash-loss budget.
            elif mono - self.last_checkpoint[track] >= 8000:
                self._checkpoint(track, owner)
                self.last_checkpoint[track] = mono

    def _run(self) -> None:
        self.last_epoch: dict[str, str] = {}
        self.source_kind = "app_stream"
        try:
            store = MeetingStore(self.directory.parent)
            with store.writer(self.directory.name, Writer.CAPTURE) as owner:
                with private_directory(self.directory / "audio") as audio:
                    while True:
                        kind, payload = self.jobs.get()
                        try:
                            if kind == "start":
                                self.source_kind = payload.pop("source_kind")
                                self._edge(payload, owner)
                            elif kind == "track_start":
                                track, stamp, recorded = payload
                                self._start(track, stamp, recorded, audio, owner)
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
    ) -> None:
        self.session_monitor = MeetingSession.production() if session is None else None
        self.session = session if session is not None else self.session_monitor.state  # type: ignore[union-attr]
        self.manual_resume_required = False
        self.session_terminal = False
        self.auto_resume_pending = False
        self.lease_tracks: set[str] = set()
        if self.session_monitor is not None:
            self.session_monitor.start()
        self.clock, self.mic_factory = clock, mic_factory
        self.backend_factory, self.fsync = backend_factory, fsync
        self.backend: Backend | None = None
        self.free_mb = free_mb or self._free_mb
        self.directory: Path | None = None
        self.ledger = RequestLedger()
        self.identity: Message = {}
        self.far: Message = {"kind": "none"}
        self.mic_device: str | None = None
        self.archive: _Archive | None = None
        self.sources: dict[str, Source] = {}
        self.track_states = {"mic": "ended", "far": "ended"}
        self.messages: queue.SimpleQueue[Message] = queue.SimpleQueue()
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
        self.control_busy = False
        self.open_thread: threading.Thread | None = None
        self.readers: dict[str, threading.Thread] = {}
        self.shutdown_request: Message | None = None
        self.terminating = False
        self.used_meetings: set[str] = set()
        self.termination_deadline: float | None = None
        self.seq = 0
        self.preparing = False
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
            and self.session.allowed(request["blocked_epoch"])
            and 0 <= self.clock() * 1000 - request["admission_mono_ms"] <= 1000
        )

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
            or self.pause_reasons != {"lease"}
            or self.final_status == "failed"
            or self.clock() - self.last_lease >= 5
        ):
            return
        snapshot = self.session.snapshot()
        if not snapshot.admissible:
            return
        self.auto_resume_pending = False
        self.selected_tracks = [
            track
            for track in ("mic", "far")
            if track in self.lease_tracks and self.track_states[track] == "paused"
        ]
        if self.selected_tracks:
            self._open(
                {
                    "automatic": True,
                    "blocked_epoch": snapshot.blocked_epoch,
                    "admission_mono_ms": int(self.clock() * 1000),
                }
            )

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
        previous = self.pending.setdefault(request["req"], [])
        previous.append(event)
        self.ledger.finish(request["req"], previous)
        return event

    def _get_backend(self) -> Backend:
        if self.backend is None:
            self.backend = self.backend_factory()
        return self.backend

    def handle(self, message: Message) -> list[Message]:
        message = ipc.decode(
            ipc.encode(message, role="meeting-capture")[4:], role="meeting-capture"
        )
        if message["type"] not in COMMAND_FIELDS["meeting-capture"]:
            return [failure(message, "unknown-message")]
        status, cached = self.ledger.begin(message)
        if status == "same":
            return deepcopy(cached or [])
        if status in {"conflict", "limit", "exhausted"}:
            if status == "exhausted":
                self.terminate()
            return [failure(message, "req-conflict" if status == "conflict" else "limit")]
        action = message["type"]
        session_events: list[Message] = []
        self._session_events(session_events)
        for event in session_events:
            self.messages.put({"type": "session-event", "event": event})
        try:
            if self.stopped and action != "meeting.capture.shutdown":
                raise ValueError("not-prepared")
            if action == "ping":
                return self._remember(message, [self._reply("pong", message)])
            if action in {"meeting.detector.start", "meeting.detector.stop"}:
                if self.preparing or self.control_busy:
                    raise ValueError("not-prepared")
                self._detector(message)
                reply = self._reply("meeting.ack", message)
                self.pending[message["req"]] = [reply]
                return self._remember(message, [reply])
            if action == "meeting.call.query":
                return self._remember(
                    message, [self._reply("meeting.call.state", message, active=False)]
                )
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
                self.pending[message["req"]] = [reply]
                return self._remember(message, [reply])
            if action == "meeting.capture.prepare":
                if (
                    self.preparing
                    or self.control_busy
                    or self.opening
                    or self.finalizing
                    or self.sources
                    or self.close_deadline is not None
                    or self.stopped
                    or self.terminating
                    or message["meeting_id"] in self.used_meetings
                ):
                    raise ValueError("not-prepared")
                self._prepare(message)
                reply = self._reply("meeting.ack", message)
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
                self.last_lease = self.clock()
                self.auto_resume_pending = self.state == "paused" and self.pause_reasons == {
                    "lease"
                }
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
                expected = "prepared" if action == "meeting.capture.open" else "paused"
                if self.state != expected or self.opening or self.finalizing:
                    raise ValueError("not-prepared")
                if self.final_status == "failed":
                    raise ValueError("io")
                if not self.far_available:
                    raise ValueError("limit")
                if not self._admitted(message):
                    raise ValueError("admission-stale")
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
                    self.close_waiters.append(message)
                    self.pending[message["req"]] = list(replies)
                    return self._remember(message, replies)
                if self.state != "recording" and not self.opening:
                    return self._remember(message, self._already_closed(message))
                self._close(message, message.get("reason", "stop"))
            else:
                raise ValueError("not-prepared")
            reply = self._reply("meeting.ack", message, **self._base())
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
        self.control_busy = True

        def work() -> None:
            try:
                if request["type"] == "meeting.detector.start":
                    index = self._get_backend().detector_start()
                    reply = self._reply("meeting.detector.started", request, own_client_index=index)
                else:
                    if self.backend:
                        self.backend.detector_stop()
                    reply = self._reply("meeting.detector.stopped", request)
            except Exception:
                reply = failure(request, "audio-failed")
            self.messages.put({"type": "detector-result", "request": request, "reply": reply})

        threading.Thread(target=work, name="meeting-detector-control", daemon=True).start()

    def _prepare(self, request: Message) -> None:
        self.preparing = True
        self.prepare_request = request
        self.prepare_deadline = self.clock() + 5.0
        self.directory = Path(request["dir"])
        self.identity = {}
        self.state = "stopped"

        def work() -> None:
            try:
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
                far = request["far"]
                if (
                    far["kind"] == "app_stream"
                    and not {"sink_input", "object_serial"} <= far.keys()
                ):
                    raise ValueError("target-mismatch")
                info = self._get_backend().prepare(far)
                self.messages.put(
                    {"type": "prepared", "request": request, "info": info, "free": free}
                )
            except Exception as exc:
                code = (
                    str(exc)
                    if isinstance(exc, ValueError)
                    and str(exc) in {"limit", "target-mismatch", "dir-unwritable", "not-prepared"}
                    else "audio-failed"
                )
                self.messages.put({"type": "prepare-failed", "request": request, "code": code})

        threading.Thread(target=work, name="meeting-prepare", daemon=True).start()

    def _already_closed(self, request: Message) -> list[Message]:
        if request["type"] == "meeting.capture.stop":
            self.state = "stopped"
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

    def _guard_source(self, source: Source, request: Message, stop: threading.Event) -> None:
        # Meeting-only adapters consult this again at native stream_new/connect;
        # context selection may have blocked after the outer admission check.
        guarded: Any = source
        guarded.admission_guard = lambda: self.session.lock
        guarded.admission_check = lambda: (
            not stop.is_set()
            and not self.session_terminal
            and self.session.allowed(request["blocked_epoch"])
        )

    def _open(self, request: Message) -> None:
        assert self.directory is not None
        if self.archive is None:
            self.archive = _Archive(
                self.directory, self._base(), self.index, self.fsync, self.messages.put
            )
        self.audio_stop = threading.Event()
        self.opening = True
        self.pause_reasons.clear()
        self.manual_resume_required = False
        self.open_request = request
        stop = self.audio_stop
        self.used_meetings.add(self.identity["meeting_id"])
        backend = self._get_backend()

        def start() -> None:
            try:
                free = self.free_mb()
                self.messages.put({"type": "disk-space", "free": free})
                if free < 600:
                    raise ValueError("limit")
                self.sources = {}
                for track in self.selected_tracks:
                    if stop.is_set() or not self._admitted(request):
                        raise ValueError("admission-stale")
                    source = self.mic_factory() if track == "mic" else backend.source(self.far)
                    self.sources[track] = source
                    self._guard_source(source, request, stop)
                    if stop.is_set() or not self._admitted(request):
                        raise ValueError("admission-stale")
                    source.open(self.mic_device if track == "mic" else None)
                    if stop.is_set() or not self._admitted(request):
                        source.shutdown()
                        raise ValueError("admission-stale")
                self.messages.put({"type": "opened", "request": request})
                # Control activates the disk/reader edge after observing open completion.
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
                self._guard_source(source, request, stop)
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
        try:
            while not stop.is_set():
                data = source.read_chunk()
                if stop.is_set():
                    return
                change = getattr(source, "change", None)
                if change:
                    archive.put("track_close", (track, "target_changed"))
                    archive.put("track_start", (track, source.sample_clock(), self._recorded()))
                    self.messages.put({"type": "track-change", "track": track, "kind": change})
                if data is None:
                    reason = (
                        "far_ended"
                        if track == "far" and getattr(source, "ended", False)
                        else (
                            "mic_change"
                            if track == "mic" and getattr(source, "device_change", None)
                            else "audio_failed"
                        )
                    )
                    archive.put("track_close", (track, reason))
                    self.messages.put({"type": "track-ended", "track": track, "reason": reason})
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
                if not data:
                    stop.wait(0.01)
        except Exception:
            self.messages.put({"type": "audio-failed"})

    def _close(self, request: Message | None, reason: str) -> None:
        if reason == "lease" and self.close_deadline is None:
            self.lease_tracks = {
                track for track, state in self.track_states.items() if state == "live"
            }
        self._add_reason(reason)
        if reason in {"stop", "limit", "sleep", "session"}:
            self.state = "stopped"
            self.auto_resume_pending = False
        if self.close_deadline is not None:
            return
        self.recorded_ms = self._recorded()
        self.active_at = None
        self.audio_stop.set()
        self.identity["state_rev"] += 1
        self.state = "stopped" if reason in {"stop", "limit", "sleep", "session"} else "paused"
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

    def poll(self) -> list[Message]:
        result: list[Message] = []
        self._session_events(result)
        while True:
            try:
                event = self.messages.get_nowait()
            except queue.Empty:
                break
            kind = event["type"]
            if kind == "session-event":
                result.append(event["event"])
            elif kind == "detector-result":
                self.control_busy = False
                result.append(self._outcome(event["request"], event["reply"]))
            elif kind in {"prepared", "prepare-failed"}:
                if not self.preparing or event["request"] != self.prepare_request:
                    continue
                self.preparing = False
                self.prepare_deadline = None
                request = event["request"]
                if kind == "prepare-failed":
                    result.append(self._outcome(request, failure(request, event["code"])))
                    continue
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
                self.index = request["segment_index_from"]
                self.state, self.recorded_ms = "prepared", 0
                self.pause_reasons.clear()
                self.manual_resume_required = False
                self.session_terminal = False
                self.auto_resume_pending = False
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
            elif kind == "disk-space":
                self.disk_sampling = False
                self.cached_free_mb = event["free"]
                if self.cached_free_mb < 200:
                    self._autopause("disk", "disk-write", result)
            elif kind == "opened":
                self.opening = False
                self._session_events(result)
                if (
                    self.close_deadline is not None
                    or self.stopped
                    or not self._admitted(event["request"])
                ):
                    if self.close_deadline is None:
                        self._close(None, self.session.snapshot().reason or "stop")
                    continue
                self.identity["state_rev"] += 1
                self.state = "recording"
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
                    self.archive.put(
                        "track_start", (track, source.sample_clock(), self.recorded_ms)
                    )
                    self._start_reader(track, source)
                if event["request"].get("automatic"):
                    self.last_alive = 0
                    continue
                result.append(
                    self._outcome(
                        event["request"],
                        {
                            "type": "meeting.capture.started",
                            **self._base(),
                            **self._pause(),
                            "t0_mono_ms": edge["t_mono_ms"],
                            "tracks": list(self.sources),
                        },
                    )
                )
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
            elif kind == "open-failed":
                self.opening = False
                if self.close_deadline is None:
                    self._close(None, "audio_failed")
                if not event["request"].get("automatic"):
                    result.append(
                        self._outcome(
                            event["request"],
                            failure(event["request"], event.get("code", "audio-failed")),
                        )
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
            elif kind == "track-ended":
                track, reason = event["track"], event["reason"]
                self.lease_tracks.discard(track)
                if reason == "audio_failed":
                    self._autopause("audio_failed", "audio-failed", result)
                else:
                    result.append(
                        self._track_event(
                            track,
                            "ended" if track == "far" else "paused",
                            "target-removed" if track == "far" else "device-lost",
                        )
                    )
                    if self.archive:
                        self.archive.put(
                            "edge",
                            {
                                "kind": "far_ended" if track == "far" else "mic_gap_start",
                                "t_mono_ms": int(self.clock() * 1000),
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
        self._session_events(result)
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
        return result

    def _autopause(self, reason: str, wire_reason: str, result: list[Message]) -> None:
        if self.state in {"stopped", "closing-unknown"}:
            return
        added = self._add_reason(reason)
        if self.state == "recording" or self.opening:
            self._close(None, reason)
        elif self.state == "prepared":
            self.state = "paused"
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
        self.preparing = False
        self.prepare_deadline = None
        self.termination_deadline = time.monotonic() + 1.0
        if self.sources or self.opening:
            self._close(None, "stop")
        elif not self.finalizing:
            self.stopped = True
            if self.archive:
                self.archive.put("quit", None)

    def cleanup(self) -> None:
        """Transport loss/signal: bounded physical cleanup, storage may be abandoned."""
        self.terminate()  # Start physical close before bounded helper reaping.
        if self.session_monitor is not None:
            self.session_monitor.close()
        deadline = time.monotonic() + 1.0
        while not self.stopped and time.monotonic() < deadline:
            self.poll()
            time.sleep(0.005)
