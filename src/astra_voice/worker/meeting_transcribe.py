"""Role meeting-transcribe (E13.M4 step 1): VAD -> echo gate -> ASR -> durable text.

One job at a time runs on a single executor thread. The control path (ping,
cancel, credits) never waits for inference, fsync or credits. Durability is D10:
transcript.json (atomic + fsync, revision + 1) strictly before checkpoint.json
referencing that revision; ASR cursors never pass durable text. Every run that
starts its text from zero over existing files first publishes an empty text
and its own checkpoint, so an older checkpoint never vouches for foreign text.
The final text also gets a checkpoint; it is dropped only after done was handed
to the transport, so a crash in between resumes straight to done. After a
cancel is acknowledged no write to the meeting directory can start (write
fence); serialization and validation run outside the fence.
Speech text, user paths and file names never reach logs or error messages.
Diarization (stage 4) is not part of this step: done carries diar_status=skipped.
"""

from __future__ import annotations

import bisect
import contextlib
import dataclasses
import hashlib
import json
import math
import os
import queue
import re
import struct
import threading
import time
import unicodedata
from collections.abc import Callable, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt

from astra_voice.meetings import MeetingValidationError, echo_gate
from astra_voice.meetings.model import (
    ID_PATTERN,
    MAX_MEETING_MS,
    AsrCursor,
    AsrProgress,
    Checkpoint,
    Meeting,
    ModelIdentity,
    ProcessingLimits,
    Transcript,
    Utterance,
)
from astra_voice.meetings.segments import SegmentSidecar
from astra_voice.meetings.store import MeetingStore, MeetingStoreError, MeetingWriter, Writer
from astra_voice.meetings.timeline import EpochTimeline, SegmentTimeline
from astra_voice.security.files import check_private_file, private_directory
from astra_voice.worker import ipc
from astra_voice.worker.engine import CancelToken, Engine, EngineError, make_engine
from astra_voice.worker.meeting_contract import ASR_WINDOW_TIMEOUT_S, COMMAND_FIELDS, RequestLedger
from astra_voice.worker.meeting_roles import failure

Message = dict[str, Any]
Audio = npt.NDArray[np.float32]
Probabilities = Callable[[Audio, CancelToken], Sequence[float] | None]
VadFactory = Callable[[], tuple[str, Probabilities]]

ROLE = "meeting-transcribe"
RATE = 16000
FRAME = 512  # Silero frame, 32 ms
SPEECH_ON = 0.5
SPEECH_OFF = 0.35  # worker/vad.py pause threshold
MIN_PAUSE_MS = 500
MIN_SPEECH_MS = 200
PAD_MS = 200
MAX_UTTERANCE_MS = 24_000  # GigaAM window; hard limit
SPLIT_FROM_MS = 18_000  # long speech is cut at the least speech-like frame in [18; 24] s
VAD_WINDOW_FRAMES = 60_000 * RATE // 1000 // FRAME  # 60 s
VAD_OVERLAP_FRAMES = 32  # 1.024 s of state warm-up, discarded
VAD_STEP_FRAMES = VAD_WINDOW_FRAMES - VAD_OVERLAP_FRAMES
CHECKPOINT_UTTERANCES = 32
CHECKPOINT_AUDIO_MS = 30_000
WALL_SLACK_MS = 600_000  # ceiling: 2 x duration + 10 min
PROGRESS_INTERVAL_S = 0.5  # <= 2 progress events per second
MEMORY_MARGIN_MB = 200  # no diarization in this step
MAX_THREADS = 2
MAX_SEGMENT_FRAMES = 4_800_000
TRANSCRIPT_CAPACITY = 9000  # Transcript schema maximum
TEXT_LIMIT_BYTES = ipc.MAX_TEXT_BYTES
MAX_CREDITS = 4096
DONE_DELIVERY_S = 5.0  # final checkpoint is dropped only after done left the role
UNRECOGNIZED_TEXT = "[не распознано]"
DAMAGED_TEXT = "[кусок записи повреждён]"
_SEGMENT_FILE = re.compile(r"(mic|far)-([0-9]{4})\.(wav|json)\Z")
_TRACK_ORDER = {"mic": 0, "far": 1}


class _Cancelled(Exception):
    """Job cancelled; nothing more is written or emitted."""


class _Failed(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _mem_available_mb() -> int:
    try:
        with open("/proc/meminfo", encoding="ascii") as stream:
            for line in stream:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        pass
    return 0


def _status_kb(field_name: str, path: str = "/proc/self/status") -> int:
    try:
        with open(path, encoding="ascii") as stream:
            for line in stream:
                name, _, value = line.partition(":")
                if name == field_name:
                    return int(value.split()[0])
    except (OSError, ValueError, IndexError):
        pass
    return 0


def energy_probabilities(audio: Audio, cancel: CancelToken) -> list[float] | None:
    """Fallback when Silero is unavailable: deterministic frame RMS gate."""
    if cancel.cancelled:
        return None
    count = (len(audio) + FRAME - 1) // FRAME
    padded = np.zeros(count * FRAME, dtype=np.float32)
    padded[: len(audio)] = audio
    rms = np.sqrt((padded.reshape(count, FRAME).astype(np.float64) ** 2).mean(axis=1))
    return [1.0 if value >= 0.01 else 0.0 for value in rms]


def _default_vad() -> tuple[str, Probabilities]:
    from astra_voice.worker import vad

    if vad.vad_available():
        return "silero-v5", vad.speech_probabilities
    return "energy-rms", energy_probabilities


def sanitize_text(text: str) -> str:
    """NFC, no Cc/Cf/Cs (whitespace controls become spaces), <= 32 KiB UTF-8."""
    normalized = unicodedata.normalize("NFC", text)
    cleaned = "".join(
        " " if char in "\t\n\r\v\f" else char
        for char in normalized
        if unicodedata.category(char) not in {"Cc", "Cf", "Cs"} or char in "\t\n\r\v\f"
    ).strip()
    encoded = cleaned.encode("utf-8")
    if len(encoded) > TEXT_LIMIT_BYTES:
        cleaned = encoded[:TEXT_LIMIT_BYTES].decode("utf-8", errors="ignore")
    return cleaned


def speech_regions(
    probabilities: Sequence[float] | npt.NDArray[np.float32], samples: int
) -> list[tuple[int, int]]:
    """Deterministic utterance sample ranges [a, b), each <= MAX_UTTERANCE_MS.

    Hysteresis SPEECH_ON/SPEECH_OFF, pauses >= MIN_PAUSE_MS split, PAD_MS of
    context on both sides. Longer speech is cut at the frame with the lowest
    speech probability inside [SPLIT_FROM_MS; MAX_UTTERANCE_MS] from its start.
    """
    probs = np.asarray(probabilities, dtype=np.float32)
    pause = math.ceil(MIN_PAUSE_MS * RATE / 1000 / FRAME)
    min_speech = math.ceil(MIN_SPEECH_MS * RATE / 1000 / FRAME)
    pad = PAD_MS * RATE // 1000
    limit = MAX_UTTERANCE_MS * RATE // 1000
    split_from = SPLIT_FROM_MS * RATE // 1000
    raw: list[tuple[int, int, int]] = []
    start: int | None = None
    last = strong = 0
    for index, value in enumerate(probs.tolist()):
        if start is None:
            if value >= SPEECH_ON:
                start, last, strong = index, index, 1
        elif value >= SPEECH_OFF:
            last = index
            strong += value >= SPEECH_ON
        elif index - last > pause:
            raw.append((start, last + 1, strong))
            start = None
    if start is not None:
        raw.append((start, last + 1, strong))
    result: list[tuple[int, int]] = []
    previous = 0
    for first, end, count in raw:
        if count < min_speech:
            continue
        a = max(previous, first * FRAME - pad, 0)
        b = min(samples, end * FRAME + pad)
        while b - a > limit:
            low = -(-(a + split_from) // FRAME)
            high = (a + limit) // FRAME
            if low <= high and low < len(probs):
                window = probs[low : min(high, len(probs) - 1) + 1]
                cut = (low + int(np.argmin(window))) * FRAME
            else:
                cut = a + limit
            cut = max(a + 1, min(cut, a + limit))
            result.append((a, cut))
            a = cut
        if b > a:
            result.append((a, b))
            previous = b
    return result


def _wav_data_offset(header: bytes) -> int | None:
    """RIFF/WAVE PCM s16le mono 16 kHz; return data chunk offset, else None."""
    if len(header) < 12 or header[:4] != b"RIFF" or header[8:12] != b"WAVE":
        return None
    position, valid = 12, False
    while position + 8 <= len(header):
        chunk = header[position : position + 4]
        size = int.from_bytes(header[position + 4 : position + 8], "little")
        body = position + 8
        if chunk == b"fmt ":
            if size < 16 or body + 16 > len(header):
                return None
            kind, channels, rate, _, _, bits = struct.unpack_from("<HHIIHH", header, body)
            valid = (kind, channels, rate, bits) == (1, 1, RATE, 16)
        elif chunk == b"data":
            return body if valid else None
        position = body + size + (size & 1)
    return None


@dataclass(frozen=True)
class _Wav:
    offset: int
    frames: int
    size: int


@dataclass(frozen=True)
class _Mapping:
    """Sample -> recorded ms for one epoch (or one segment), never across gaps.

    Inside timeline knots the shared epoch estimate is used; beyond the last
    knot (an unclosed segment's recovered tail) the nominal rate continues.
    """

    clock: EpochTimeline | SegmentTimeline
    anchor_ms: int
    origin_mono: int

    def mono(self, sample: int) -> int:
        points = self.clock.points
        if sample > points[-1][0]:
            return points[-1][1] + (sample - points[-1][0]) * 1000 // RATE
        if sample < points[0][0]:
            return points[0][1] - (points[0][0] - sample) * 1000 // RATE
        return self.clock.sample_to_monotonic(sample)

    def recorded(self, sample: int) -> int:
        return min(MAX_MEETING_MS, max(0, self.anchor_ms + self.mono(sample) - self.origin_mono))


@dataclass(frozen=True)
class _Piece:
    name: str
    first: int
    end: int
    offset: int


@dataclass
class _Span:
    track: str
    mapping: _Mapping
    pieces: list[_Piece] = field(default_factory=list)

    @property
    def first(self) -> int:
        return self.pieces[0].first

    @property
    def end(self) -> int:
        return self.pieces[-1].end

    def recorded(self, sample: int) -> int:
        return self.mapping.recorded(sample)

    def sample_at(self, ms: int) -> int:
        """Smallest span sample whose recorded time is >= ms (clamped)."""
        low, high = self.first, self.end
        while low < high:
            middle = (low + high) // 2
            if self.recorded(middle) < ms:
                low = middle + 1
            else:
                high = middle
        return low


@dataclass(frozen=True)
class _Candidate:
    start_ms: int
    end_ms: int
    track: str
    span: int = -1
    a: int = 0
    b: int = 0
    damaged: bool = False

    @property
    def key(self) -> tuple[int, int, int]:
        return self.start_ms, _TRACK_ORDER[self.track], self.end_ms


@dataclass
class _Entry:
    track: str
    index: int
    sidecar: SegmentSidecar | None
    wav: _Wav | None


@dataclass
class _Resume:
    kept: list[Utterance]
    cursors: dict[str, int]
    revision: int
    fresh: bool  # the text starts from zero
    on_disk: bool  # transcript.json or checkpoint.json already exists
    complete: Transcript | None = None  # final text of this very configuration


@dataclass
class _Durable:
    """What every checkpoint of a run carries; revision = last durable text."""

    manifest: str
    config: str
    identity: ModelIdentity
    capacity: int
    revision: int


class _Reader:
    """Read-only PCM access through the pinned audio directory, no link following."""

    def __init__(self, directory: int) -> None:
        self.directory = directory
        self.files: dict[str, int] = {}

    def probe(self, name: str) -> _Wav | None:
        try:
            descriptor = os.open(
                name,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                dir_fd=self.directory,
            )
        except OSError:
            return None
        try:
            info = os.fstat(descriptor)
            check_private_file(info)
            offset = _wav_data_offset(os.pread(descriptor, 4096, 0))
        except (OSError, ValueError):
            os.close(descriptor)
            return None
        if offset is None or info.st_size < offset:
            os.close(descriptor)
            return None
        self.files[name] = descriptor
        return _Wav(offset, (info.st_size - offset) // 2, info.st_size)

    def read(self, span: _Span, a: int, b: int) -> Audio:
        """Samples [a, b) of a span as float32; outside audio stays zero."""
        out = np.zeros(max(0, b - a), dtype=np.float32)
        for piece in span.pieces:
            low, high = max(a, piece.first), min(b, piece.end)
            if low >= high:
                continue
            raw = os.pread(
                self.files[piece.name], (high - low) * 2, piece.offset + (low - piece.first) * 2
            )
            values = np.frombuffer(raw[: len(raw) // 2 * 2], dtype="<i2")
            out[low - a : low - a + len(values)] = values.astype(np.float32) / 32768.0
        return out

    def close(self) -> None:
        for descriptor in self.files.values():
            os.close(descriptor)
        self.files.clear()


class _Job:
    def __init__(self, request: Message) -> None:
        self.request = request
        self.identity: Message = {
            "meeting_id": request["meeting_id"],
            "meeting_generation": request["meeting_generation"],
        }
        self.cancelled = False
        self.finished = False
        self.credits = 0
        self.condition = threading.Condition()
        self.token = CancelToken()  # VAD/whole-job token
        self.current: CancelToken | None = None  # one ASR window
        self.progress_at = -math.inf
        self.delivered = threading.Event()  # done handed to the transport

    def matches(self, message: Message) -> bool:
        return all(message.get(key) == value for key, value in self.identity.items())

    def add_credits(self, count: int) -> None:
        with self.condition:
            self.credits = min(MAX_CREDITS, self.credits + count)
            self.condition.notify_all()

    def cancel(self) -> None:
        with self.condition:
            self.cancelled = True
            self.condition.notify_all()
        self.token.cancel()
        current = self.current
        if current is not None:
            current.cancel()


class MeetingTranscribeState:
    """Handler for run_role: handle() for commands, poll() for events/outcomes."""

    def __init__(
        self,
        *,
        store: MeetingStore | None = None,
        engine_factory: Callable[[str], Engine] = make_engine,
        vad_factory: VadFactory = _default_vad,
        mem_available_mb: Callable[[], int] = _mem_available_mb,
        clock: Callable[[], float] = time.monotonic,
        asr_timeout_s: float = ASR_WINDOW_TIMEOUT_S,
        status_path: str = "/proc/self/status",
    ) -> None:
        self.store = store if store is not None else MeetingStore()
        self.engine_factory, self.vad_factory = engine_factory, vad_factory
        self.mem_available_mb, self.clock = mem_available_mb, clock
        self.asr_timeout_s, self.status_path = asr_timeout_s, status_path
        self.ledger = RequestLedger()
        self.pending: dict[str, list[Message]] = {}
        self.stopped = False
        self.exit_code = 0
        self.outbox: queue.SimpleQueue[tuple[str, Any, Message]] = queue.SimpleQueue()
        self.tasks: queue.SimpleQueue[tuple[str, Any] | None] = queue.SimpleQueue()
        self.fence = threading.Lock()
        self.job: _Job | None = None
        self.cancels: list[tuple[Message, _Job]] = []
        self.model_requested = False
        self.busy_loading = False
        # Executor-owned below.
        self.engine: Engine | None = None
        self.spec: Message | None = None
        self.loaded: Message | None = None
        self.engine_version = "unspecified"
        self.thread = threading.Thread(
            target=self._executor, name="meeting-transcribe", daemon=True
        )
        self.thread.start()

    # ---- control thread -------------------------------------------------

    @staticmethod
    def _reply(kind: str, request: Message, **fields: Any) -> Message:
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

    def _receipt(self, request: Message) -> list[Message]:
        ack = self._reply("meeting.ack", request)
        self.pending[request["req"]] = [ack]
        return self._remember(request, [ack])

    def _outcome(self, request: Message, message: Message) -> Message:
        message = {**message, "req": request["req"], "request_type": request["request_type"]}
        if request["req"] in self.ledger.requests:
            previous = self.pending.setdefault(request["req"], [])
            previous.append(message)
            self.ledger.finish(request["req"], previous)
        return message

    def _job_active(self) -> bool:
        job = self.job
        if job is None or job.cancelled or job.finished:
            return False
        return not job.delivered.is_set()  # after done only cleanup remains

    def handle(self, message: Message) -> list[Message]:
        message = ipc.decode(ipc.encode(message, role=ROLE)[4:], role=ROLE)
        if message["type"] not in COMMAND_FIELDS[ROLE]:
            return [failure(message, "unknown-message")]
        status, cached = self.ledger.begin(message)
        if status == "exhausted":
            self.stopped = True
            self.exit_code = 2
            return [failure(message, "limit")]
        if status == "same":
            return deepcopy(cached or [])
        if status in {"conflict", "limit"}:
            return [failure(message, "req-conflict" if status == "conflict" else "limit")]
        kind = message["type"]
        if kind == "ping":
            return self._remember(message, [self._reply("pong", message)])
        if kind == "measure":
            hwm = _status_kb("VmHWM", self.status_path)
            if not hwm:
                return self._remember(message, [failure(message, "io")])
            return self._remember(
                message,
                [self._reply("measured", message, vm_hwm_kb=hwm, pss_kb=self._pss())],
            )
        if kind in {"model.load", "model.unload"}:
            if self._job_active() or self.busy_loading:
                return self._remember(message, [failure(message, "not-prepared")])
            self.busy_loading = True
            if kind == "model.load":
                self.model_requested = True
            self.tasks.put(("load" if kind == "model.load" else "unload", message))
            return self._receipt(message)
        if kind == "meeting.job.start":
            if self._job_active():
                return self._remember(message, [failure(message, "limit")])
            if not self.model_requested:
                return self._remember(message, [failure(message, "model")])
            job = _Job(message)
            self.job = job
            self.tasks.put(("job", job))
            return self._receipt(message)
        if kind == "meeting.job.credit":
            if self.job is None or not self.job.matches(message) or not self._job_active():
                return self._remember(message, [failure(message, "not-prepared")])
            self.job.add_credits(int(message["n"]))
            return self._remember(message, [self._reply("meeting.ack", message)])
        if kind == "meeting.job.cancel":
            active = self.job
            if active is None or active.finished or not active.matches(message):
                return self._remember(message, [self._reply("meeting.job.cancelled", message)])
            active.cancel()
            if self._fenced():
                return self._remember(message, [self._reply("meeting.job.cancelled", message)])
            self.cancels.append((message, active))
            return self._receipt(message)
        # meeting.job.applied: GUI application receipt only (never fsync evidence).
        return self._remember(message, [self._reply("meeting.ack", message)])

    def _fenced(self) -> bool:
        """True when no meeting write is in flight; later writes see cancelled."""
        if not self.fence.acquire(blocking=False):
            return False
        self.fence.release()
        return True

    def _pss(self) -> int | None:
        value = _status_kb("Pss", "/proc/self/smaps_rollup")
        return value or None

    def poll(self, limit: int = 2) -> list[Message]:
        result: list[Message] = []
        for request, _job in list(self.cancels):
            if self._fenced():
                self.cancels.remove((request, _job))
                result.append(self._outcome(request, self._reply("meeting.job.cancelled", request)))
        while len(result) < limit:
            try:
                kind, owner, message = self.outbox.get_nowait()
            except queue.Empty:
                break
            if kind == "finished":
                if owner is self.job:
                    self.job = None
            elif kind == "loaded":
                self.busy_loading = False
                if message["type"] == "model.unloaded" or message.get("code") == "model":
                    self.model_requested = False
                result.append(self._outcome(owner, message))
            elif kind == "reply":
                if isinstance(owner[1], _Job) and owner[1].cancelled:
                    continue
                result.append(self._outcome(owner[0], message))
            elif not (isinstance(owner, _Job) and owner.cancelled):
                result.append(message)
                if message["type"] == "meeting.job.done" and isinstance(owner, _Job):
                    owner.delivered.set()
        return result

    def cleanup(self) -> None:
        if self.job is not None:
            self.job.cancel()
        self.tasks.put(None)
        self.thread.join(1.0)
        if not self.thread.is_alive() and self.engine is not None:
            try:
                self.engine.unload()
            except Exception:
                pass
            self.engine = None

    # ---- executor thread ------------------------------------------------

    def _executor(self) -> None:
        while True:
            task = self.tasks.get()
            if task is None:
                return
            kind, payload = task
            try:
                if kind == "load":
                    self.outbox.put(("loaded", payload, self._load(payload)))
                elif kind == "unload":
                    self._unload()
                    self.spec = None
                    self.outbox.put(("loaded", payload, {"type": "model.unloaded"}))
                else:
                    self._run_job(payload)
            except BaseException:  # the executor must outlive any single task
                if kind == "job":
                    self._emit(payload, self._failed(payload, "io"))
                else:
                    self.outbox.put(("loaded", payload, failure(payload, "model")))
            finally:
                if kind == "job":
                    payload.finished = True
                    self.outbox.put(("finished", payload, {}))

    def _unload(self) -> None:
        if self.engine is not None:
            self.engine.unload()
        self.engine = None
        self.loaded = None
        from astra_voice.worker import vad

        vad.unload()

    def _load(self, request: Message) -> Message:
        spec = {
            key: request[key]
            for key in ("id", "revision", "dir", "layout", "variant", "threads", "min_ram_mb")
        }
        if self.engine is not None and self.spec == spec and self.loaded is not None:
            return dict(self.loaded)
        if self.engine is not None:
            self._unload()
        self.spec = spec
        need = int(spec["min_ram_mb"]) + MEMORY_MARGIN_MB
        if self.mem_available_mb() < need:
            # Deferred: a job re-checks memory before loading and reports waiting.
            return failure(request, "memory")
        try:
            return self._load_engine(spec)
        except MemoryError:
            return failure(request, "memory")
        except Exception:
            self.spec = None
            return failure(request, "model")

    def _load_engine(self, spec: Message) -> Message:
        engine = self.engine_factory(spec["layout"])
        result = engine.load(
            Path(spec["dir"]),
            spec["layout"],
            spec["variant"],
            max(1, min(int(spec["threads"]), MAX_THREADS)),
        )
        self.engine = engine
        self.engine_version = sanitize_text(result.engine_version)[:128] or "unspecified"
        self.loaded = {
            "type": "model.loaded",
            "id": spec["id"],
            "revision": spec["revision"],
            "variant": spec["variant"],
            "load_ms": float(result.load_ms),
            "engine_version": self.engine_version,
        }
        return dict(self.loaded)

    def _emit(self, job: _Job, message: Message) -> None:
        if not job.cancelled:
            self.outbox.put(("event", job, message))

    def _failed(self, job: _Job, code: str) -> Message:
        return {"type": "meeting.job.failed", **job.identity, "code": code}

    def _write(self, job: _Job, owner: MeetingWriter, filename: str, document: Any) -> None:
        if job.cancelled:
            raise _Cancelled
        # O(text) validation/serialization stays outside the fence: a cancel
        # waits at most for one atomic replace + fsync, not for the encoder.
        prepared = owner.prepare(filename, document)
        with self.fence:
            if job.cancelled:
                raise _Cancelled
            owner.write(filename, prepared)

    def _remove_checkpoint(self, job: _Job, owner: MeetingWriter) -> None:
        with self.fence:
            if job.cancelled:
                raise _Cancelled
            owner.remove("checkpoint.json")

    def _run_job(self, job: _Job) -> None:
        try:
            self._job(job)
        except _Cancelled:
            pass
        except _Failed as failed:
            self._emit(job, self._failed(job, failed.code))
        except MemoryError:
            self._emit(job, self._failed(job, "memory"))
        except EngineError:
            self._emit(job, self._failed(job, "model"))
        except (MeetingStoreError, MeetingValidationError, OSError, ValueError):
            self._emit(job, self._failed(job, "io"))

    def _check_dir(self, job: _Job) -> None:
        """realpath(dir) must be <meetings root>/<meeting_id>; nothing is opened."""
        root = Path(os.path.realpath(self.store.root))
        target = Path(os.path.realpath(job.request["dir"]))
        if (
            not target.is_relative_to(root)
            or target.parent != root
            or target.name != job.identity["meeting_id"]
        ):
            raise _Failed("io")

    def _ensure_model(self, job: _Job) -> bool:
        if job.cancelled:
            raise _Cancelled  # never load a model for a cancelled job
        if self.engine is not None:
            return True
        if self.spec is None:
            raise _Failed("model")
        need = int(self.spec["min_ram_mb"]) + MEMORY_MARGIN_MB
        available = self.mem_available_mb()
        if available < need:
            self._emit(
                job,
                {
                    "type": "meeting.job.waiting",
                    **job.identity,
                    "reason": "memory",
                    "need_mb": need,
                    "available_mb": max(0, available),
                },
            )
            return False
        try:
            self._load_engine(self.spec)
        except MemoryError:
            raise
        except Exception:
            self.spec = None
            raise _Failed("model") from None
        return True

    def _job(self, job: _Job) -> None:
        started = self.clock()
        self._check_dir(job)
        if not self._ensure_model(job):
            return
        if job.cancelled:
            raise _Cancelled
        meeting_id = job.identity["meeting_id"]
        meeting = self.store.read(meeting_id, "meeting.json")
        if (
            not isinstance(meeting, Meeting)
            or meeting.meeting_generation != job.identity["meeting_generation"]
            or meeting.cancelled
        ):
            raise _Failed("io")
        with contextlib.ExitStack() as lease:
            # Taking the lease creates its lock file and fsyncs the directory:
            # behind the fence, so nothing changes on disk after a cancel ack.
            with self.fence:
                if job.cancelled:
                    raise _Cancelled
                owner = lease.enter_context(self.store.writer(meeting_id, Writer.TRANSCRIBE))
            audio_path = self.store.root / meeting_id / "audio"
            with private_directory(audio_path, private_from=self.store.root) as audio:
                reader = _Reader(audio)
                try:
                    self._pipeline(job, owner, reader, started)
                finally:
                    reader.close()

    # ---- stage 0: inventory ---------------------------------------------

    def _inventory(
        self, job: _Job, reader: _Reader
    ) -> tuple[list[_Span], list[_Candidate], str, int]:
        meeting_id = job.identity["meeting_id"]
        tracks = set(job.request["tracks"])
        found: dict[tuple[str, int], set[str]] = {}
        for name in os.listdir(reader.directory):
            match = _SEGMENT_FILE.fullmatch(name)
            if match and match[1] in tracks:
                found.setdefault((match[1], int(match[2])), set()).add(match[3])
        entries: list[_Entry] = []
        manifest: list[list[Any]] = []
        for track, index in sorted(found):
            stem = f"{track}-{index:04d}"
            sidecar: SegmentSidecar | None = None
            if "json" in found[track, index]:
                try:
                    value = self.store.read(meeting_id, f"audio/{stem}.json")
                except MeetingStoreError:
                    value = None
                if (
                    isinstance(value, SegmentSidecar)
                    and value.meeting_id == meeting_id
                    and (value.track, value.index) == (track, index)
                ):
                    sidecar = value
            wav = reader.probe(f"{stem}.wav") if "wav" in found[track, index] else None
            digest = (
                hashlib.sha256(
                    json.dumps(sidecar.to_dict(), sort_keys=True, ensure_ascii=False).encode()
                ).hexdigest()
                if sidecar is not None
                else None
            )
            manifest.append([stem, wav.size if wav else -1, digest])
            entries.append(_Entry(track, index, sidecar, wav))
        manifest_hash = hashlib.sha256(
            json.dumps(manifest, separators=(",", ":")).encode()
        ).hexdigest()
        spans: list[_Span] = []
        damaged: list[_Candidate] = []
        bounds: dict[tuple[str, int], tuple[int, int]] = {}
        for track in sorted(tracks):
            valid = [e for e in entries if e.track == track and e.sidecar is not None]
            for group in self._groups(valid):
                self._map_group(group, spans, damaged, bounds)
        for entry in entries:
            if (entry.track, entry.index) in bounds:
                continue
            damaged.append(self._unknown_range(entry, entries, bounds))
        duration = max(
            [span.recorded(span.end) for span in spans] + [c.end_ms for c in damaged] + [0]
        )
        return spans, damaged, manifest_hash, duration

    @staticmethod
    def _groups(entries: list[_Entry]) -> list[list[_Entry]]:
        groups: list[list[_Entry]] = []
        for entry in entries:
            sidecar = entry.sidecar
            assert sidecar is not None
            if groups:
                previous = groups[-1][-1].sidecar
                assert previous is not None
                if (
                    previous.epoch_id == sidecar.epoch_id
                    and previous.capture_id == sidecar.capture_id
                    and previous.index + 1 == sidecar.index
                    and previous.sample_end == sidecar.sample_start
                    and not previous.discontinuity
                    and previous.closed
                ):
                    groups[-1].append(entry)
                    continue
            groups.append([entry])
        return groups

    def _map_group(
        self,
        group: list[_Entry],
        spans: list[_Span],
        damaged: list[_Candidate],
        bounds: dict[tuple[str, int], tuple[int, int]],
    ) -> None:
        sidecars = [entry.sidecar for entry in group if entry.sidecar is not None]
        try:
            clock: EpochTimeline | SegmentTimeline = (
                EpochTimeline(sidecars)
                if len(sidecars) > 1
                else SegmentTimeline.from_segment(sidecars[0])
            )
            if not clock.points:
                raise MeetingValidationError("Нет опорных точек часов")
        except MeetingValidationError:
            if len(group) > 1:
                for entry in group:
                    self._map_group([entry], spans, damaged, bounds)
            return
        first = sidecars[0]
        mapping = _Mapping(clock, first.recorded_ms_at_open, 0)
        mapping = dataclasses.replace(mapping, origin_mono=mapping.mono(first.sample_start))
        span: _Span | None = None
        for position, entry in enumerate(group):
            sidecar = entry.sidecar
            assert sidecar is not None
            last = position == len(group) - 1
            wav = entry.wav
            usable = 0
            if wav is not None:
                if sidecar.closed or not last:
                    usable = min(wav.frames, sidecar.frames)
                else:
                    usable = min(wav.frames, MAX_SEGMENT_FRAMES)
            missing_from = sidecar.sample_start + usable
            missing_to = max(missing_from, sidecar.sample_end)
            end = sidecar.sample_start + max(usable, sidecar.frames)
            bounds[entry.track, entry.index] = (
                mapping.recorded(sidecar.sample_start),
                mapping.recorded(end),
            )
            if usable:
                piece = _Piece(
                    f"{entry.track}-{entry.index:04d}.wav",
                    sidecar.sample_start,
                    sidecar.sample_start + usable,
                    wav.offset if wav else 0,
                )
                if span is None or span.end != piece.first:
                    span = _Span(entry.track, mapping)
                    spans.append(span)
                span.pieces.append(piece)
            if missing_to > missing_from:
                span = None
                damaged.append(
                    _Candidate(
                        mapping.recorded(missing_from),
                        mapping.recorded(missing_to),
                        entry.track,
                        damaged=True,
                    )
                )

    @staticmethod
    def _unknown_range(
        entry: _Entry, entries: list[_Entry], bounds: dict[tuple[str, int], tuple[int, int]]
    ) -> _Candidate:
        """Unreadable sidecar: bound the damage by its readable neighbours."""
        before = [
            bounds[e.track, e.index][1]
            for e in entries
            if e.track == entry.track and e.index < entry.index and (e.track, e.index) in bounds
        ]
        after = [
            bounds[e.track, e.index][0]
            for e in entries
            if e.track == entry.track and e.index > entry.index and (e.track, e.index) in bounds
        ]
        length = entry.wav.frames * 1000 // RATE if entry.wav is not None else 0
        start = max(before) if before else max(0, (min(after) if after else 0) - length)
        end = min(after) if after else start + length
        start = min(start, MAX_MEETING_MS)
        return _Candidate(start, min(MAX_MEETING_MS, max(start, end)), entry.track, damaged=True)

    # ---- resume -----------------------------------------------------------

    def _read_optional(self, meeting_id: str, name: str) -> Any:
        if not os.path.lexists(self.store.root / meeting_id / name):
            return None
        try:
            return self.store.read(meeting_id, name)
        except MeetingStoreError:
            return None  # an unreadable checkpoint is never trusted

    def _resume(
        self,
        job: _Job,
        owner: MeetingWriter,
        manifest: str,
        config: str,
        identity: ModelIdentity,
    ) -> _Resume:
        meeting_id = job.identity["meeting_id"]
        generation = job.identity["meeting_generation"]
        try:
            # Through the lease: one read + validation also serves the writer's
            # revision cache, so the next publication does not re-read the text.
            transcript = owner.read_transcript()
        except MeetingStoreError:
            raise _Failed("io") from None  # cannot be superseded safely
        checkpoint = self._read_optional(meeting_id, "checkpoint.json")
        on_disk = transcript is not None or os.path.lexists(
            self.store.root / meeting_id / "checkpoint.json"
        )
        same = (
            isinstance(transcript, Transcript)
            and transcript.meeting_id == meeting_id
            and transcript.meeting_generation == generation
        )
        base = transcript.transcript_revision if same and transcript is not None else 0
        fresh = _Resume([], {"mic": 0, "far": 0}, base, True, on_disk)
        if (
            job.request["resume"]
            and same
            and isinstance(transcript, Transcript)
            and transcript.labels_final
            and checkpoint is None
            and not os.path.lexists(self.store.root / meeting_id / "checkpoint.json")
            and all(u.seq == i for i, u in enumerate(transcript.utterances))
        ):
            # Checkpoint already dropped after done: within one generation the final
            # text is this job's (a model/config change comes with a new generation
            # and resume=false), so it is done again without VAD/ASR or a rewrite.
            cursors = {"mic": 0, "far": 0}
            for utterance in transcript.utterances:
                cursors[utterance.track] = max(cursors[utterance.track], utterance.end_ms)
            return _Resume(
                list(transcript.utterances),
                cursors,
                transcript.transcript_revision,
                False,
                on_disk,
                transcript,
            )
        if not (job.request["resume"] and same and isinstance(checkpoint, Checkpoint)):
            return fresh
        assert isinstance(transcript, Transcript)
        if checkpoint.transcript_revision > transcript.transcript_revision:
            return fresh  # never trust a checkpoint ahead of durable text
        # Identity fields only: the text itself was validated when it was read.
        view = dataclasses.replace(
            transcript, transcript_revision=checkpoint.transcript_revision, utterances=[]
        )
        try:
            compatible = checkpoint.compatible_with(
                view,
                input_manifest_sha256=manifest,
                processing_config_sha256=config,
                model=identity,
            )
        except MeetingValidationError:
            compatible = False
        kept = list(transcript.utterances)
        if not compatible or any(u.seq != i for i, u in enumerate(kept)):
            return fresh
        cursors = {"mic": 0, "far": 0}
        for utterance in kept:
            cursors[utterance.track] = max(cursors[utterance.track], utterance.end_ms)
        if checkpoint.transcript_revision == transcript.transcript_revision:
            cursors["mic"] = max(cursors["mic"], checkpoint.asr.mic.done_until_ms)
            cursors["far"] = max(cursors["far"], checkpoint.asr.far.done_until_ms)
        # Every run that writes text from zero first publishes its own checkpoint,
        # so the latest compatible checkpoint vouches for the final text as well.
        complete = transcript if transcript.labels_final else None
        return _Resume(kept, cursors, transcript.transcript_revision, False, on_disk, complete)

    # ---- pipeline ---------------------------------------------------------

    def _config(self, job: _Job, identity: ModelIdentity) -> str:
        request = job.request
        document = {
            "schema": 1,
            "model": identity.to_dict(),
            "vad": {
                "on": SPEECH_ON,
                "off": SPEECH_OFF,
                "min_pause_ms": MIN_PAUSE_MS,
                "min_speech_ms": MIN_SPEECH_MS,
                "pad_ms": PAD_MS,
                "window_frames": VAD_WINDOW_FRAMES,
                "overlap_frames": VAD_OVERLAP_FRAMES,
            },
            "split": {"max_ms": MAX_UTTERANCE_MS, "from_ms": SPLIT_FROM_MS},
            "echo_gate": bool(request["echo_gate"]),
            "echo": echo_gate.config(),
            "tracks": sorted(request["tracks"]),
            "self_track": request["self_track"],
            "variant": request["variant"],
            "diarization": False,
            "limits": request["limits"],
            "window": {"utterances": CHECKPOINT_UTTERANCES, "audio_ms": CHECKPOINT_AUDIO_MS},
        }
        return hashlib.sha256(json.dumps(document, sort_keys=True).encode()).hexdigest()

    def _identity(self, vad_name: str) -> ModelIdentity:
        assert self.spec is not None
        variant = str(self.spec["variant"])
        if re.fullmatch(ID_PATTERN, variant) is None:
            variant = "v-" + hashlib.sha256(variant.encode()).hexdigest()[:16]
        return ModelIdentity(
            id=str(self.spec["id"]),
            revision=str(self.spec["revision"]),
            variant=variant,
            vad=vad_name,
            engine=self.engine_version,
        )

    def _progress(
        self, job: _Job, stage: str, done: int, total: int, *, force: bool = False
    ) -> None:
        now = self.clock()
        if not force and now - job.progress_at < PROGRESS_INTERVAL_S:
            return
        job.progress_at = now
        total = min(MAX_MEETING_MS, max(0, total))
        self._emit(
            job,
            {
                "type": "meeting.job.progress",
                **job.identity,
                "stage": stage,
                "done_ms": min(total, max(0, done)),
                "total_ms": total,
            },
        )

    def _check_wall(self, job: _Job, deadline: float) -> None:
        if job.cancelled:
            raise _Cancelled
        if self.clock() > deadline:
            raise _Failed("limit")

    def _pipeline(self, job: _Job, owner: MeetingWriter, reader: _Reader, started: float) -> None:
        request = job.request
        limits = request["limits"]
        spans, damaged, manifest, duration = self._inventory(job, reader)
        vad_name, probabilities = self.vad_factory()
        identity = self._identity(vad_name)
        config = self._config(job, identity)
        resume = self._resume(job, owner, manifest, config, identity)
        kept, cursors = resume.kept, resume.cursors
        capacity = min(int(limits["max_utterances"]), TRANSCRIPT_CAPACITY)
        durable = _Durable(manifest, config, identity, capacity, resume.revision)
        if resume.fresh and resume.on_disk and duration <= int(limits["max_ms"]):
            # From zero over existing files: our empty text and checkpoint go first,
            # so no older checkpoint can vouch for the text this run writes (D10).
            self._persist(job, owner, durable, [], cursors)
        reply = self._reply(
            "meeting.job.accepted",
            {**job.identity, **{k: request[k] for k in ("req", "request_type")}},
            resume_from={"mic_ms": cursors["mic"], "far_ms": cursors["far"]},
        )
        if job.cancelled:
            raise _Cancelled
        self.outbox.put(("reply", (request, job), reply))
        if resume.complete is not None:
            # The final text of this configuration is durable: no VAD/ASR, no rewrite.
            # echo_dropped of the original run is not durable; it is reported as 0.
            self._finish(job, owner, resume.complete, 0, started)
            return
        if duration > int(limits["max_ms"]):
            raise _Failed("limit")
        deadline = started + (2 * duration + WALL_SLACK_MS) / 1000
        # Stage 1: VAD per span (deterministic, so resume yields the same list).
        candidates = list(damaged)
        for index, span in enumerate(spans):
            probs = self._span_probabilities(job, span, reader, probabilities, duration, deadline)
            for a, b in speech_regions(probs, span.end - span.first):
                candidates.append(
                    _Candidate(
                        span.recorded(span.first + a),
                        span.recorded(span.first + b),
                        span.track,
                        index,
                        span.first + a,
                        span.first + b,
                    )
                )
                if len(candidates) > 4 * capacity:
                    raise _Failed("limit")
        candidates.sort(key=lambda c: c.key)
        # Stage 2: echo gate (mic only, only when far is part of the job).
        echoes = self._echo(job, candidates, spans, reader)
        transcribable = [c for c, echo in zip(candidates, echoes, strict=True) if not echo]
        if len(transcribable) > capacity:
            raise _Failed("limit")
        if any(
            (u.track, u.start_ms, u.end_ms) != (c.track, c.start_ms, c.end_ms)
            for u, c in zip(kept, transcribable, strict=False)
        ) or len(kept) > len(transcribable):
            kept, cursors = [], {"mic": 0, "far": 0}  # durable text is not this input
            self._persist(job, owner, durable, [], cursors)
        # Stage 3: ASR in chronological order of both tracks.
        self._asr(
            job,
            owner,
            reader,
            spans,
            candidates,
            echoes,
            kept,
            cursors,
            durable,
            (duration, deadline, started),
        )

    def _span_probabilities(
        self,
        job: _Job,
        span: _Span,
        reader: _Reader,
        probabilities: Probabilities,
        duration: int,
        deadline: float,
    ) -> npt.NDArray[np.float32]:
        samples = span.end - span.first
        frames = (samples + FRAME - 1) // FRAME
        out = np.zeros(frames, dtype=np.float32)
        position = 0
        while position < frames:
            self._check_wall(job, deadline)
            warm = min(VAD_OVERLAP_FRAMES, position)
            end = min(frames, position + VAD_STEP_FRAMES)
            audio = reader.read(
                span,
                span.first + (position - warm) * FRAME,
                min(span.end, span.first + end * FRAME),
            )
            values = probabilities(audio, job.token)
            if values is None:
                if job.cancelled:
                    raise _Cancelled
                raise _Failed("model")
            chunk = np.asarray(values, dtype=np.float32)[warm : warm + end - position]
            if len(chunk) != end - position or not np.isfinite(chunk).all():
                raise _Failed("model")
            out[position:end] = chunk
            position = end
            self._progress(job, "vad", span.recorded(span.first + end * FRAME), duration)
        return out

    def _echo(
        self, job: _Job, candidates: list[_Candidate], spans: list[_Span], reader: _Reader
    ) -> list[bool]:
        enabled = bool(job.request["echo_gate"]) and "far" in job.request["tracks"]
        result = [False] * len(candidates)
        if not enabled:
            return result
        # Far utterances never overlap each other: ends are sorted with starts.
        far = [c for c in candidates if c.track == "far" and not c.damaged]
        far_ends = [c.end_ms for c in far]
        far_spans = [span for span in spans if span.track == "far"]
        for position, candidate in enumerate(candidates):
            if job.cancelled:
                raise _Cancelled
            if candidate.track != "mic" or candidate.damaged:
                continue
            length = candidate.end_ms - candidate.start_ms
            covered = 0
            for other in far[bisect.bisect_right(far_ends, candidate.start_ms) :]:
                if other.start_ms >= candidate.end_ms:
                    break
                covered += min(candidate.end_ms, other.end_ms) - max(
                    candidate.start_ms, other.start_ms
                )
            overlap = covered / length if length > 0 else 0.0
            if overlap < echo_gate.FAR_OVERLAP_MIN:
                continue
            mic_audio = reader.read(spans[candidate.span], candidate.a, candidate.b)
            far_audio = np.zeros(len(mic_audio) + echo_gate.LAG_SAMPLES, dtype=np.float32)
            for span in far_spans:
                if span.recorded(span.first) <= candidate.start_ms <= span.recorded(span.end):
                    first = span.sample_at(candidate.start_ms) - echo_gate.LAG_SAMPLES
                    far_audio = reader.read(span, first, first + len(far_audio))
                    break
            decision = echo_gate.assess(mic_audio, far_audio, overlap, enabled=True)
            result[position] = decision.echo
        return result

    def _wait_credit(self, job: _Job, deadline: float) -> None:
        with job.condition:
            while job.credits <= 0 and not job.cancelled:
                if self.clock() > deadline:
                    raise _Failed("limit")
                job.condition.wait(0.05)
            if job.cancelled:
                raise _Cancelled
            job.credits -= 1

    def _recognize(self, job: _Job, audio: Audio) -> str:
        """One window with an absolute deadline; nothing (ping, credits) extends it."""
        assert self.engine is not None
        token = CancelToken()
        job.current = token
        if job.cancelled:
            token.cancel()
        timer = threading.Timer(self.asr_timeout_s, token.cancel)
        timer.daemon = True
        timer.start()
        try:
            result = self.engine.transcribe(audio, token)
        except ValueError:
            raise _Failed("model") from None
        finally:
            timer.cancel()
            job.current = None
        if job.cancelled:
            raise _Cancelled
        if result.cancelled:
            return UNRECOGNIZED_TEXT
        return sanitize_text(result.text)

    def _persist(
        self,
        job: _Job,
        owner: MeetingWriter,
        durable: _Durable,
        utterances: list[Utterance],
        cursors: dict[str, int],
        *,
        final: bool = False,
    ) -> Transcript:
        """Text (atomic + fsync) strictly before the checkpoint referencing it (D10)."""
        durable.revision += 1
        identity_fields = job.identity
        transcript = Transcript(
            meeting_id=identity_fields["meeting_id"],
            meeting_generation=identity_fields["meeting_generation"],
            transcript_revision=durable.revision,
            utterances=[dataclasses.replace(u, final=True) for u in utterances]
            if final
            else list(utterances),
            labels_final=final,
        )
        self._write(job, owner, "transcript.json", transcript)
        checkpoint = Checkpoint(
            meeting_id=identity_fields["meeting_id"],
            meeting_generation=identity_fields["meeting_generation"],
            transcript_revision=durable.revision,
            input_manifest_sha256=durable.manifest,
            processing_config_sha256=durable.config,
            model=durable.identity,
            asr=AsrProgress(AsrCursor(cursors["mic"]), AsrCursor(cursors["far"])),
            limits=ProcessingLimits(
                utterances=durable.capacity, blocks=job.request["limits"]["max_blocks"]
            ),
        )
        self._write(job, owner, "checkpoint.json", checkpoint)
        return transcript

    def _finish(
        self,
        job: _Job,
        owner: MeetingWriter,
        transcript: Transcript,
        echo_dropped: int,
        started: float,
    ) -> None:
        self._emit(
            job,
            {
                "type": "meeting.job.done",
                **job.identity,
                "utterances": len(transcript.utterances),
                "speakers": len({u.speaker for u in transcript.utterances}),
                "diar_status": "skipped",
                "wall_ms": max(0, round((self.clock() - started) * 1000)),
                "peak_rss_kb": _status_kb("VmHWM", self.status_path),
                "echo_dropped": echo_dropped,
                "transcript_revision": transcript.transcript_revision,
            },
        )
        # The final checkpoint outlives done until the transport took it: a crash
        # in between resumes straight to done instead of recomputing the text.
        deadline = time.monotonic() + DONE_DELIVERY_S
        while not job.delivered.wait(0.05):
            if job.cancelled or time.monotonic() > deadline:
                return  # a leftover final checkpoint only short-cuts a later resume
        with contextlib.suppress(MeetingStoreError):
            self._remove_checkpoint(job, owner)

    def _asr(
        self,
        job: _Job,
        owner: MeetingWriter,
        reader: _Reader,
        spans: list[_Span],
        candidates: list[_Candidate],
        echoes: list[bool],
        kept: list[Utterance],
        cursors: dict[str, int],
        durable: _Durable,
        context: tuple[int, float, float],
    ) -> None:
        duration, deadline, started = context
        identity_fields = job.identity
        utterances = list(kept)
        self_track = job.request["self_track"]
        window_utterances = window_ms = 0

        seen = 0
        for candidate, echo in zip(candidates, echoes, strict=True):
            length = candidate.end_ms - candidate.start_ms
            if echo:
                if candidate.end_ms > cursors[candidate.track]:
                    cursors[candidate.track] = candidate.end_ms
                    window_ms += length
                continue
            seen += 1
            if seen <= len(kept):
                continue
            self._wait_credit(job, deadline)
            self._check_wall(job, deadline)
            if candidate.damaged:
                text = DAMAGED_TEXT
            else:
                audio = reader.read(spans[candidate.span], candidate.a, candidate.b)
                text = self._recognize(job, audio)
                self._check_wall(job, deadline)
            seq = len(utterances)
            utterance = Utterance(
                id=f"u{seq:05d}",
                seq=seq,
                track=candidate.track,
                start_ms=candidate.start_ms,
                end_ms=candidate.end_ms,
                text=text,
                speaker="me" if candidate.track == "mic" and self_track == "mic" else "others",
            )
            utterances.append(utterance)
            self._emit(
                job,
                {
                    "type": "meeting.job.utterance",
                    **identity_fields,
                    "seq": seq,
                    "id": utterance.id,
                    "track": utterance.track,
                    "start_ms": utterance.start_ms,
                    "end_ms": utterance.end_ms,
                    "text": utterance.text,
                    "speaker": utterance.speaker,
                    "provisional": True,
                },
            )
            cursors[candidate.track] = max(cursors[candidate.track], candidate.end_ms)
            window_utterances += 1
            window_ms += length
            if window_utterances >= CHECKPOINT_UTTERANCES or window_ms >= CHECKPOINT_AUDIO_MS:
                self._persist(job, owner, durable, utterances, cursors)
                window_utterances = window_ms = 0
            self._progress(job, "asr", candidate.end_ms, duration)
        # Stage 5: final text (labels_final without diarization) + its checkpoint,
        # done, then the checkpoint is dropped (T-414).
        self._progress(job, "finalize", duration, duration, force=True)
        final = self._persist(job, owner, durable, utterances, cursors, final=True)
        self._finish(job, owner, final, sum(echoes), started)
