"""Memory-only meeting-diar role: one sherpa pass, adaptive centroids, bounded IPC.

Control runs independently of native inference. Cancellation revokes all output
immediately; cancellation exits the process even during native work. No
Python onnxruntime/onnx_asr imports, custom operators, caches or writable paths.
The caller has already verified catalog signatures, hashes and revocations.
"""

from __future__ import annotations

import contextlib
import ctypes
import fcntl
import math
import os
import queue
import stat
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Any

from astra_voice.meetings.diar_link import (
    DIAR_PROFILES,
    MAX_BLOCKS,
    MAX_LOCAL_SPEAKERS,
    MAX_SEGMENTS,
    Centroid,
    LimitError,
    SpeakerLinker,
)
from astra_voice.meetings.model import MAX_MEETING_MS
from astra_voice.worker import ipc
from astra_voice.worker.meeting_contract import COMMAND_FIELDS, RequestLedger, operation_timeout
from astra_voice.worker.meeting_roles import failure
from astra_voice.worker.onnx_scan import collect_external_locations

ROLE = "meeting-diar"
RATE = 16000
MAX_PCM_BYTES = 1380 * RATE * 4
MAX_FRAME_BYTES = 65536
QUEUE_FRAMES = 2
# Linux UAPI; some CPython builds omit these fcntl names despite memfd support.
F_GET_SEALS = 1034
F_ADD_SEALS = 1033
REQUIRED_SEALS = 0x000F
MODEL_SNAPSHOT_LIMITS = (16 * 1024**2, 64 * 1024**2)
MODEL_COPY_BYTES = 1024 * 1024
MODEL_FILES = (
    ("segmentation.onnx",),
    ("embedder.onnx", "3dspeaker_speech_campplus_sv_zh_en_16k-common_advanced.onnx"),
)
Message = dict[str, Any]


class Cancelled(Exception):
    """Private control flow; never serialized."""


def check_cancel(cancel: threading.Event) -> None:
    if cancel.is_set():
        raise Cancelled


def _model_snapshot(source: int, maximum: int) -> int:
    """Read the source once; seal the bounded private image before any checks.

    Size/mtime/ctime checks reject changes during copying. Subsequent mutation
    of the source inode is irrelevant: validation and native loading use ONLY
    the sealed image, which cannot be overwritten, resized or unsealed.
    """
    before = os.fstat(source)
    if not stat.S_ISREG(before.st_mode) or before.st_size <= 0:
        raise ValueError("diar-model-type")
    if before.st_size > maximum:
        raise LimitError("limit")
    snapshot = os.memfd_create("astra-voice-diar-model", os.MFD_ALLOW_SEALING | os.MFD_CLOEXEC)
    try:
        remaining = before.st_size
        while remaining:
            chunk = os.read(source, min(remaining, MODEL_COPY_BYTES))
            if not chunk:
                raise ValueError("diar-model-short")
            remaining -= len(chunk)
            view = memoryview(chunk)
            while view:
                count = os.write(snapshot, view)
                if count <= 0:
                    raise OSError("diar-model-snapshot")
                view = view[count:]
            view.release()
            del view, chunk
        after = os.fstat(source)
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise ValueError("diar-model-changed")
        fcntl.fcntl(snapshot, F_ADD_SEALS, REQUIRED_SEALS)
        if fcntl.fcntl(snapshot, F_GET_SEALS) & REQUIRED_SEALS != REQUIRED_SEALS:
            raise ValueError("diar-model-unsealed")
        return snapshot
    except BaseException:
        os.close(snapshot)
        raise


@contextlib.contextmanager
def checked_models(directories: Sequence[str]) -> Iterator[list[str]]:
    """No symlink source paths; validate immutable snapshots before sessions.

    collect_external_locations is the protobuf scanner used by scan_model_dir;
    it also rejects malformed/unsupported graphs. Unlike that directory API,
    it can scan a /proc/self/fd image without relying on a .onnx suffix. ANY
    external tensor is refused. Both sealed images outlive session creation.
    """
    with contextlib.ExitStack() as stack:
        paths = []
        for directory, names, maximum in zip(
            directories, MODEL_FILES, MODEL_SNAPSHOT_LIMITS, strict=True
        ):
            path = Path(directory)
            if not path.is_absolute() or ".." in path.parts:
                raise ValueError("diar-model-path")
            descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
            stack.callback(os.close, descriptor)
            for part in path.parts[1:]:
                descriptor = os.open(
                    part,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=descriptor,
                )
                stack.callback(os.close, descriptor)
            pinned = Path(f"/proc/self/fd/{descriptor}")
            selected = [name for name in names if (pinned / name).exists()]
            if len(selected) != 1:
                raise ValueError("diar-model-layout")
            source = os.open(
                selected[0],
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                dir_fd=descriptor,
            )
            try:
                snapshot = _model_snapshot(source, maximum)
            finally:
                os.close(source)
            stack.callback(os.close, snapshot)
            model_path = Path(f"/proc/self/fd/{snapshot}")
            if collect_external_locations(model_path):
                raise ValueError("diar-model-external")
            paths.append(str(model_path))
        yield paths


def read_pcm(descriptor: int, seconds: float, cancel: threading.Event) -> Any:
    """Consume and close an SCM_RIGHTS memfd; require immutable exact-size f32.

    All seals are required, so the sender cannot resize or mutate while it is
    read. A regular disk file, pipe, socket or a merely named anonymous file is
    refused before reading; F_GET_SEALS plus /proc identity identifies memfd.
    """
    try:
        info = os.fstat(descriptor)
        size = round(seconds * RATE) * 4
        seals = REQUIRED_SEALS
        if (
            not stat.S_ISREG(info.st_mode)
            or not 0 < info.st_size == size <= MAX_PCM_BYTES
            or info.st_size % 4
            or not os.readlink(f"/proc/self/fd/{descriptor}").startswith("/memfd:")
            or fcntl.fcntl(descriptor, F_GET_SEALS) & seals != seals
        ):
            raise ValueError("diar-pcm-invalid")
        import numpy as np

        audio = np.empty(size // 4, dtype="<f4")
        view = memoryview(audio.data).cast("B")
        position = 0
        while position < size:
            check_cancel(cancel)
            chunk = os.pread(descriptor, min(1024 * 1024, size - position), position)
            if not chunk:
                raise ValueError("diar-pcm-short")
            view[position : position + len(chunk)] = chunk
            position += len(chunk)
        if not np.isfinite(audio).all() or np.max(np.abs(audio)) > 1:
            raise ValueError("diar-pcm-invalid")
        return audio
    finally:
        os.close(descriptor)


class _EmbeddingConfig(ctypes.Structure):
    _fields_ = [
        ("model", ctypes.c_char_p),
        ("threads", ctypes.c_int32),
        ("debug", ctypes.c_int32),
        ("provider", ctypes.c_char_p),
    ]


class _PyannoteConfig(ctypes.Structure):
    _fields_ = [("model", ctypes.c_char_p), ("window_shift_ratio", ctypes.c_float)]


class _SegmentationConfig(ctypes.Structure):
    _fields_ = [
        ("pyannote", _PyannoteConfig),
        ("threads", ctypes.c_int32),
        ("debug", ctypes.c_int32),
        ("provider", ctypes.c_char_p),
    ]


class _ClusteringConfig(ctypes.Structure):
    _fields_ = [
        ("clusters", ctypes.c_int32),
        ("threshold", ctypes.c_float),
        ("confidence", ctypes.c_int32),
    ]


class _DiarConfig(ctypes.Structure):
    _fields_ = [
        ("segmentation", _SegmentationConfig),
        ("embedding", _EmbeddingConfig),
        ("clustering", _ClusteringConfig),
        ("min_on", ctypes.c_float),
        ("min_off", ctypes.c_float),
    ]


class _NativeTurn(ctypes.Structure):
    _fields_ = [
        ("start", ctypes.c_float),
        ("end", ctypes.c_float),
        ("speaker", ctypes.c_int32),
        ("confidence", ctypes.c_float),
    ]


class _NativeSherpa:
    """The pinned 1.13.8 C ABI, with CDLL releasing the GIL for EVERY native call.

    The wheel's Python process binding retains the GIL and copies PCM through
    std::vector<float>. Neither is acceptable for bounded control latency/RSS.
    The C API's progress callback cannot abort inference. Cancellation therefore
    revokes output and exits the owning role process, reclaiming native work.
    ABI definitions: sherpa-onnx v1.13.8 c-api/c-api.h (embedding + diarization).
    """

    def __init__(self, paths: list[str], threads: int, threshold: float) -> None:
        import importlib.util

        self.diarizer: Any = None
        self.extractor: Any = None
        self.functions: dict[str, Any] = {}
        spec = importlib.util.find_spec("sherpa_onnx")
        if spec is None or spec.origin is None:
            raise ValueError("diar-runtime-unavailable")
        library = Path(spec.origin).parent / "lib" / "libsherpa-onnx-c-api.so"
        self.lib = ctypes.CDLL(str(library))
        pointer = ctypes.c_void_p
        floats = ctypes.POINTER(ctypes.c_float)

        def bind(name: str, result: Any, *arguments: Any) -> Any:
            function = getattr(self.lib, "SherpaOnnx" + name)
            function.restype = result
            function.argtypes = list(arguments)
            self.functions[name] = function
            return function

        version = bind("GetVersionStr", ctypes.c_char_p)().decode("ascii")
        if version not in {"1.13.8", "v1.13.8"}:
            raise ValueError("diar-runtime-abi")
        bind("CreateOfflineSpeakerDiarization", pointer, ctypes.POINTER(_DiarConfig))
        bind("DestroyOfflineSpeakerDiarization", None, pointer)
        bind("OfflineSpeakerDiarizationProcess", pointer, pointer, floats, ctypes.c_int32)
        bind("OfflineSpeakerDiarizationResultGetNumSegments", ctypes.c_int32, pointer)
        bind("OfflineSpeakerDiarizationResultSortByStartTime", ctypes.POINTER(_NativeTurn), pointer)
        bind("OfflineSpeakerDiarizationDestroySegment", None, ctypes.POINTER(_NativeTurn))
        bind("OfflineSpeakerDiarizationDestroyResult", None, pointer)
        bind("CreateSpeakerEmbeddingExtractor", pointer, ctypes.POINTER(_EmbeddingConfig))
        bind("DestroySpeakerEmbeddingExtractor", None, pointer)
        bind("SpeakerEmbeddingExtractorDim", ctypes.c_int32, pointer)
        bind("SpeakerEmbeddingExtractorCreateStream", pointer, pointer)
        bind("DestroyOnlineStream", None, pointer)
        bind("OnlineStreamAcceptWaveform", None, pointer, ctypes.c_int32, floats, ctypes.c_int32)
        bind("OnlineStreamInputFinished", None, pointer)
        bind("SpeakerEmbeddingExtractorIsReady", ctypes.c_int32, pointer, pointer)
        bind("SpeakerEmbeddingExtractorComputeEmbedding", floats, pointer, pointer)
        bind("SpeakerEmbeddingExtractorDestroyEmbedding", None, floats)
        embedding = _EmbeddingConfig(os.fsencode(paths[1]), threads, 0, b"cpu")
        config = _DiarConfig(
            _SegmentationConfig(_PyannoteConfig(os.fsencode(paths[0]), 0.1), threads, 0, b"cpu"),
            embedding,
            _ClusteringConfig(-1, threshold, 0),
            0.3,
            0.5,
        )
        try:
            self.diarizer = self.functions["CreateOfflineSpeakerDiarization"](ctypes.byref(config))
            self.extractor = self.functions["CreateSpeakerEmbeddingExtractor"](
                ctypes.byref(embedding)
            )
            if not self.diarizer or not self.extractor:
                raise ValueError("diar-model-config")
            self.dimension = self.functions["SpeakerEmbeddingExtractorDim"](self.extractor)
            if not 1 <= self.dimension <= 4096:
                raise ValueError("diar-embedding-dimension")
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        if self.diarizer:
            self.functions["DestroyOfflineSpeakerDiarization"](self.diarizer)
            self.diarizer = None
        if self.extractor:
            self.functions["DestroySpeakerEmbeddingExtractor"](self.extractor)
            self.extractor = None

    def __del__(self) -> None:
        self.close()

    def turns(self, audio: Any, cancel: threading.Event) -> Iterator[tuple[float, float, int]]:
        pointer = audio.ctypes.data_as(ctypes.POINTER(ctypes.c_float))
        result = self.functions["OfflineSpeakerDiarizationProcess"](
            self.diarizer, pointer, len(audio)
        )
        if not result:
            raise ValueError("diar-inference")
        try:
            check_cancel(cancel)
            count = self.functions["OfflineSpeakerDiarizationResultGetNumSegments"](result)
            if not 0 <= count <= MAX_SEGMENTS:
                raise LimitError("limit")
            segments = self.functions["OfflineSpeakerDiarizationResultSortByStartTime"](result)
            if not segments and count:
                raise ValueError("diar-inference")
            try:
                for index in range(count):
                    item = segments[index]
                    yield float(item.start), float(item.end), int(item.speaker)
            finally:
                self.functions["OfflineSpeakerDiarizationDestroySegment"](segments)
        finally:
            self.functions["OfflineSpeakerDiarizationDestroyResult"](result)

    def embedding(self, audio: Any, cancel: threading.Event) -> Any:
        import numpy as np

        stream = self.functions["SpeakerEmbeddingExtractorCreateStream"](self.extractor)
        if not stream:
            raise ValueError("diar-embedding-stream")
        try:
            self.functions["OnlineStreamAcceptWaveform"](
                stream, RATE, audio.ctypes.data_as(ctypes.POINTER(ctypes.c_float)), len(audio)
            )
            self.functions["OnlineStreamInputFinished"](stream)
            check_cancel(cancel)
            if not self.functions["SpeakerEmbeddingExtractorIsReady"](self.extractor, stream):
                raise ValueError("diar-embedding-short")
            vector = self.functions["SpeakerEmbeddingExtractorComputeEmbedding"](
                self.extractor, stream
            )
            if not vector:
                raise ValueError("diar-embedding-invalid")
            try:
                check_cancel(cancel)
                return np.ctypeslib.as_array(vector, shape=(self.dimension,)).copy()
            finally:
                self.functions["SpeakerEmbeddingExtractorDestroyEmbedding"](vector)
        finally:
            self.functions["DestroyOnlineStream"](stream)


class SherpaEngine:
    """CPU-only OfflineSpeakerDiarization + SpeakerEmbeddingExtractor, one pass."""

    def __init__(self, message: Message) -> None:
        self.profile = DIAR_PROFILES["3dspeaker-campplus"]
        with checked_models([message["segmentation_dir"], message["embedder_dir"]]) as paths:
            self.native = _NativeSherpa(paths, message["threads"], self.profile.seed_threshold)

    def process(
        self, audio: Any, cancel: threading.Event
    ) -> tuple[list[tuple[int, int, int]], dict[int, Centroid]]:
        import numpy as np

        check_cancel(cancel)
        turns: list[tuple[int, int, int]] = []
        spans: dict[int, list[tuple[int, int]]] = {}
        for a, b, speaker in self.native.turns(audio, cancel):
            check_cancel(cancel)
            if not math.isfinite(a) or not math.isfinite(b) or b <= a:
                raise ValueError("diar-engine-turn")
            start, end = max(0, round(a * RATE)), min(len(audio), round(b * RATE))
            if start >= end:
                continue
            if speaker not in spans and len(spans) >= MAX_LOCAL_SPEAKERS:
                raise LimitError("limit")
            spans.setdefault(speaker, []).append((start, end))
            turns.append((round(start / 16), round(end / 16), speaker))
        centroids = {}
        for speaker, regions in spans.items():
            check_cancel(cancel)
            longest = sorted(regions, key=lambda ab: (-(ab[1] - ab[0]), ab[0]))
            pieces, count = [], 0
            for start, end in longest:
                end = min(end, start + 60 * RATE - count)
                pieces.append(audio[start:end])
                count += end - start
                if count >= 60 * RATE:
                    break
            centroids[speaker] = Centroid(
                self.native.embedding(np.concatenate(pieces), cancel),
                sum(b - a for a, b in regions) / RATE,
            )
        return turns, centroids


def block_parts(identity: Message, index: int, turns: list[list[Any]]) -> Iterator[Message]:
    """At most 64 KiB INCLUDING the four-byte frame header, no empty tail part."""
    # 512 triples fit even at maximum millisecond/label widths, then verify the
    # actual wire size with the same strict encoder used by the transport.
    for seq, first in enumerate(range(0, max(1, len(turns)), 512)):
        event = {
            "type": "diar.block.part",
            **identity,
            "block_index": index,
            "seq": seq,
            "last": first + 512 >= len(turns),
            "segments": turns[first : first + 512],
        }
        if len(ipc.encode(event, role=ROLE)) > MAX_FRAME_BYTES:
            raise LimitError("limit")
        yield event


class BlockAssembly:
    """Receiver-side D13 guard usable by the later transcriber; fail closed.

    A duplicate/gap, missing last, wrong block/identity or nonempty legacy done
    poisons this assembly. A caller must restart diarization after such loss.
    """

    def __init__(self, identity: Message, index: int) -> None:
        self.identity = dict(identity)
        self.index = index
        self.seq = 0
        self.last = False
        self.done = False
        self.failed = False
        self.segments: list[list[Any]] = []

    def accept(self, message: Message) -> bool:
        try:
            if self.failed or self.done:
                raise ValueError("diar-parts-order")
            message = ipc.decode(ipc.encode(message, role=ROLE)[4:], role=ROLE)
            if any(message.get(k) != v for k, v in self.identity.items()):
                raise ValueError("diar-parts-identity")
            if message.get("block_index") != self.index:
                raise ValueError("diar-parts-block")
            if message["type"] == "diar.block.done":
                if not self.last or message["segments"]:
                    raise ValueError("diar-parts-incomplete")
                self.done = True
                return True
            if (
                message["type"] != "diar.block.part"
                or self.last
                or message["seq"] != self.seq
                or (not message["segments"] and not (message["last"] and self.seq == 0))
                or len(ipc.encode(message, role=ROLE)) > MAX_FRAME_BYTES
                or len(self.segments) + len(message["segments"]) > MAX_SEGMENTS
            ):
                raise ValueError("diar-parts-order")
            self.segments.extend(message["segments"])
            self.seq += 1
            self.last = message["last"]
            return False
        except (ValueError, ipc.FrameError):
            self.failed = True
            self.segments.clear()
            raise ValueError("diar-parts-invalid") from None


class MeetingDiarState:
    """Single meeting/generation; one background operation, two queued frames.

    threshold/tau in the M1 wire contract are retained for compatibility only;
    the embedder profile chooses the adaptive thresholds, never a UI setting.
    Long operations ACK immediately. Repeated block req replays ALL its parts,
    never an isolated done; a pending retry only repeats the original receipt.
    """

    def __init__(self, *, engine_factory: Callable[[Message], Any] = SherpaEngine) -> None:
        self.engine_factory = engine_factory
        self.engine: Any = None
        self.linker = SpeakerLinker(DIAR_PROFILES["3dspeaker-campplus"])
        self.identity: Message | None = None
        self.ledger = RequestLedger()
        self.cancel = threading.Event()
        self.events: queue.Queue[Message] = queue.Queue(QUEUE_FRAMES)
        self.output_lock = threading.Lock()
        self.thread: threading.Thread | None = None
        self.active: Message | None = None
        self.deadline = math.inf
        self.stopped = False
        self.exit_code = 0
        self.finished = False
        self.failed = False
        self.next_block = 0
        self.end_ms = 0

    def _put(self, message: Message) -> None:
        while not self.cancel.is_set():
            # Never wait for queue capacity while holding the output fence.
            # Cancellation drains under the same fence: no blocked put can
            # wake after that drain and resurrect a post-cancel event.
            with self.output_lock:
                check_cancel(self.cancel)
                try:
                    self.events.put_nowait(message)
                    return
                except queue.Full:
                    pass
            self.cancel.wait(0.01)
        raise Cancelled

    def handle(self, message: Message, descriptors: Sequence[int] = ()) -> list[Message]:
        # Ownership transfers at entry, including every error/duplicate path.
        owned = list(descriptors)
        try:
            message = ipc.decode(ipc.encode(message, role=ROLE)[4:], role=ROLE)
            if message["type"] not in COMMAND_FIELDS[ROLE]:
                return [failure(message, "unknown-message")]
            if message["type"] == "diar.block":
                if len(owned) != 1:
                    return [failure(message, "bad-field")]
            elif owned:
                return [failure(message, "bad-field")]
            if message["type"] == "diar.cancel":
                if self.identity is not None and any(
                    message.get(k) != v for k, v in self.identity.items()
                ):
                    return [failure(message, "not-prepared")]
                self.cleanup()
                self.stopped = True
                return [self._reply(message, "diar.cancelled")]
            status, cached = self.ledger.begin(message)
            if status == "same":
                if self.active is not None and self.active["req"] == message["req"]:
                    return cached[:1] if cached else [self._reply(message, "meeting.ack")]
                if cached and len(cached) > 1:
                    if self.active is not None:
                        return [failure(message, "not-prepared")]
                    self._start(message, lambda: self._replay(cached[1:]))
                    return cached[:1]
                return cached or [self._reply(message, "meeting.ack")]
            if status != "new":
                return [failure(message, "req-conflict" if status == "conflict" else "limit")]
            kind = message["type"]
            if kind == "ping":
                result = [self._reply(message, "pong")]
            elif self.stopped or self.failed or self.finished:
                result = [failure(message, "not-prepared")]
            elif kind == "diar.load" and self.identity is None and self.active is None:
                self.identity = {k: message[k] for k in ("meeting_id", "meeting_generation")}
                self._start(message, lambda: self._load(message))
                result = [self._reply(message, "meeting.ack")]
            elif self.identity is None or any(
                message.get(k) != v for k, v in self.identity.items()
            ):
                result = [failure(message, "not-prepared")]
            elif self.active is not None or not self.events.empty():
                result = [failure(message, "not-prepared")]
            elif kind == "diar.block" and self.engine is not None:
                if (
                    self.next_block >= MAX_BLOCKS
                    or message["offset_ms"] + round(message["block_s"] * 1000) > MAX_MEETING_MS
                ):
                    result = [failure(message, "limit")]
                elif (
                    message["block_index"] != self.next_block or message["offset_ms"] < self.end_ms
                ):
                    result = [failure(message, "bad-field")]
                else:
                    descriptor = owned.pop()
                    try:
                        self._start(message, lambda: self._block(message, descriptor))
                    except (RuntimeError, MemoryError):
                        os.close(descriptor)
                        raise
                    result = [self._reply(message, "meeting.ack")]
            elif kind == "diar.finish" and self.engine is not None:
                self._start(message, lambda: self._finish(message))
                result = [self._reply(message, "meeting.ack")]
            else:
                result = [failure(message, "not-prepared")]
            self.ledger.finish(message["req"], result)
            return result
        except (RuntimeError, MemoryError):
            self.failed = True
            result = [failure(message, "memory")]
            if message.get("req") in self.ledger.requests:
                self.ledger.finish(message["req"], result)
            return result
        finally:
            for descriptor in owned:
                os.close(descriptor)

    @staticmethod
    def _reply(message: Message, kind: str, **fields: Any) -> Message:
        return {
            "type": kind,
            **{
                k: message[k]
                for k in ("req", "request_type", "meeting_id", "meeting_generation")
                if k in message
            },
            **fields,
        }

    def _start(self, message: Message, work: Callable[[], None]) -> None:
        self.active = message
        self.deadline = time.monotonic() + operation_timeout(message)

        def run() -> None:
            try:
                work()
            except Cancelled:
                pass
            except Exception as error:
                code = (
                    "limit"
                    if isinstance(error, LimitError)
                    else "memory"
                    if isinstance(error, MemoryError)
                    else "model"
                    if message["type"] == "diar.load"
                    else "bad-field"
                )
                self.failed = True
                with contextlib.suppress(Cancelled):
                    self._put(failure(message, code))
            finally:
                if self.cancel.is_set():
                    self.engine = None
                    self.linker.globals.clear()

        self.thread = threading.Thread(target=run, name="meeting-diar", daemon=True)
        try:
            self.thread.start()
        except (RuntimeError, MemoryError):
            self.thread = None
            self.active = None
            raise

    def _replay(self, events: list[Message]) -> None:
        for event in events:
            self._put(event)

    def _load(self, message: Message) -> None:
        engine = self.engine_factory(message)
        check_cancel(self.cancel)
        self.engine = engine
        self._put(self._reply(message, "diar.loaded"))

    def _block(self, message: Message, descriptor: int) -> None:
        audio = read_pcm(descriptor, message["block_s"], self.cancel)
        turns, centroids = self.engine.process(audio, self.cancel)
        check_cancel(self.cancel)
        segments = self.linker.link(turns, centroids, message["offset_ms"])
        check_cancel(self.cancel)
        self.end_ms = message["offset_ms"] + round(message["block_s"] * 1000)
        self.next_block += 1
        assert self.identity is not None
        for part in block_parts(self.identity, message["block_index"], segments):
            self._put(part)
        self._put(
            self._reply(message, "diar.block.done", block_index=message["block_index"], segments=[])
        )

    def _finish(self, message: Message) -> None:
        labels, count = self.linker.finish()
        check_cancel(self.cancel)
        self.finished = True
        self.engine = None
        self.linker.globals.clear()
        self._put(self._reply(message, "diar.finished", speakers_total=count, label_map=labels))

    def poll(self) -> list[Message]:
        if self.cancel.is_set():
            return []
        if self.active is not None and time.monotonic() > self.deadline:
            message = self.active
            self.cleanup()
            self.failed = True
            self.stopped = True
            return [failure(message, "limit")]
        try:
            event = self.events.get_nowait()
        except queue.Empty:
            event = None
        if event is not None and self.active is not None:
            # Retain bounded text/labels only, for complete retry replay.
            req = self.active["req"]
            _, cached = self.ledger.requests[req]
            if cached is not None and event not in cached[1:]:
                self.ledger.finish(req, [*cached, event])
        terminal = event is not None and event["type"] in {
            "diar.loaded",
            "diar.block.done",
            "diar.finished",
            "error",
        }
        if terminal or (
            self.thread is not None and not self.thread.is_alive() and self.events.empty()
        ):
            # The terminal event is the operation's commit point. All mutable
            # work precedes it; do not race Thread.is_alive() after announcing
            # done and refuse the peer's immediately following valid command.
            self.active = None
            self.thread = None
        if event is not None and event["type"] == "diar.finished":
            self.stopped = True
        return [event] if event is not None else []

    def cleanup(self) -> None:
        already_cancelled = self.cancel.is_set()
        self.cancel.set()
        with self.output_lock:
            while True:
                try:
                    self.events.get_nowait()
                except queue.Empty:
                    break
        if self.thread is not None and not already_cancelled:
            self.thread.join(timeout=0.1)
        self.engine = None
        self.linker.globals.clear()


class DiarReceiver:
    """Bound ancillary ownership to exactly one framed command on SOCK_STREAM.

    Read the first byte separately (SCM_RIGHTS must accompany it), then the
    remaining header and payload without crossing the next frame boundary.
    Truncated/extra ancillary data is terminal; every received fd is closed.
    """

    def __init__(self) -> None:
        self.data = bytearray()
        self.descriptors: list[int] = []

    def receive(self, connection: Any) -> tuple[bytes, list[int]] | None:
        import array
        import socket
        import struct

        length = (
            1
            if not self.data
            else 4 - len(self.data)
            if len(self.data) < 4
            else 4 + struct.unpack("!I", self.data[:4])[0] - len(self.data)
        )
        data, ancillary, flags, _ = connection.recvmsg(
            length, socket.CMSG_SPACE(4 * 4), socket.MSG_CMSG_CLOEXEC
        )
        received: list[int] = []
        invalid = False
        for level, kind, raw in ancillary:
            if level != socket.SOL_SOCKET or kind != socket.SCM_RIGHTS or len(raw) % 4:
                invalid = True
                continue
            descriptors = array.array("i")
            descriptors.frombytes(raw)
            received.extend(descriptors)
        invalid |= bool(flags & socket.MSG_CTRUNC) or len(received) > 1
        invalid |= bool(received and self.data)
        self.descriptors.extend(received)
        if invalid or not data:
            self.close()
            raise OSError("diar-transport-invalid")
        self.data.extend(data)
        if len(self.data) >= 4:
            size = struct.unpack("!I", self.data[:4])[0]
            if not 0 < size <= ipc.MAX_FRAME_BYTES:
                self.close()
                raise OSError("diar-transport-limit")
            if len(self.data) == size + 4:
                result = bytes(self.data[4:]), self.descriptors
                self.data = bytearray()
                self.descriptors = []
                return result
        return None

    def close(self) -> None:
        for descriptor in self.descriptors:
            os.close(descriptor)
        self.descriptors.clear()
