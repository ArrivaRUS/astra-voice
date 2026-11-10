"""Native ABI ownership, CPU-only configuration and cancellation without models."""

from __future__ import annotations

import ctypes
import importlib.util
import os
import threading
import time
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest
from test_meeting_diar import command, drain, loaded
from tools.meeting_diar_check import sealed_pcm

from astra_voice.meetings.diar_link import Centroid, LimitError, distance
from astra_voice.worker import meeting_diar as diar
from astra_voice.worker.onnx_scan import collect_external_locations

pytestmark = pytest.mark.unit


class Function:
    def __init__(self, action: Any) -> None:
        self.action = action
        self.restype: Any = None
        self.argtypes: Any = None

    def __call__(self, *args: Any) -> Any:
        return self.action(*args)


class Library:
    def __init__(self, dimension: int = 192) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.dimension = dimension
        self.functions: dict[str, Any] = {}
        self.vector = (ctypes.c_float * 192)(*([0.5] * 192))
        self.segments = (diar._NativeTurn * 1)(diar._NativeTurn(0, 1, 7, -2))

    def __getattr__(self, symbol: str) -> Any:
        name = symbol.removeprefix("SherpaOnnx")
        if name not in self.functions:

            def action(*args: Any) -> Any:
                self.calls.append((name, args))
                if name == "GetVersionStr":
                    return b"1.13.8"
                if name == "SpeakerEmbeddingExtractorDim":
                    return self.dimension
                if name == "OfflineSpeakerDiarizationResultGetNumSegments":
                    return 1
                if name == "OfflineSpeakerDiarizationResultSortByStartTime":
                    return self.segments
                if name == "SpeakerEmbeddingExtractorComputeEmbedding":
                    return self.vector
                return 1

            self.functions[name] = Function(action)
        return self.functions[name]


@pytest.fixture
def library(monkeypatch: pytest.MonkeyPatch) -> Library:
    lib = Library()
    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda name: SimpleNamespace(origin="/wheel/sherpa_onnx/__init__.py"),
    )

    def load(path: str) -> Library:
        assert path == "/wheel/sherpa_onnx/lib/libsherpa-onnx-c-api.so"
        return lib

    monkeypatch.setattr(ctypes, "CDLL", load)
    return lib


def test_cpu_provider_threads_abi_and_owned_allocations(library: Library) -> None:
    native = diar._NativeSherpa(["/proc/self/fd/31", "/proc/self/fd/32"], 3, 0.5)
    config = next(
        args[0]._obj for name, args in library.calls if name == "CreateOfflineSpeakerDiarization"
    )
    assert config.segmentation.threads == config.embedding.threads == 3
    assert config.segmentation.provider == config.embedding.provider == b"cpu"
    assert config.clustering.clusters == -1 and config.clustering.threshold == 0.5
    assert config.segmentation.pyannote.model == b"/proc/self/fd/31"
    assert config.embedding.model == b"/proc/self/fd/32"
    audio = np.zeros(16000, dtype=np.float32)
    cancel = threading.Event()
    assert list(native.turns(audio, cancel)) == [(0.0, 1.0, 7)]
    vector = native.embedding(audio, cancel)
    assert vector.shape == (192,) and np.all(vector == 0.5)
    native.close()
    native.close()
    names = [name for name, _ in library.calls]
    for name in [
        "DestroyOfflineSpeakerDiarization",
        "DestroySpeakerEmbeddingExtractor",
        "OfflineSpeakerDiarizationDestroyResult",
        "OfflineSpeakerDiarizationDestroySegment",
        "SpeakerEmbeddingExtractorDestroyEmbedding",
        "DestroyOnlineStream",
    ]:
        assert names.count(name) == 1, name
    assert not any("Custom" in name or "Register" in name for name in names)


def test_bad_dimension_frees_both_models(library: Library) -> None:
    library.dimension = 4097
    with pytest.raises(ValueError, match="dimension"):
        diar._NativeSherpa(["/a", "/b"], 2, 0.5)
    names = [name for name, _ in library.calls]
    assert names.count("DestroyOfflineSpeakerDiarization") == 1
    assert names.count("DestroySpeakerEmbeddingExtractor") == 1


def test_cancel_during_native_call_does_not_wait_for_inference() -> None:
    entered, release = threading.Event(), threading.Event()

    class Blocked:
        def __init__(self, message: Any) -> None:
            pass

        def process(self, audio: Any, cancel: threading.Event) -> Any:
            entered.set()
            assert release.wait(5)
            diar.check_cancel(cancel)

    state = loaded(Blocked)
    fd = sealed_pcm(np.zeros(16000, dtype="<f4"))
    state.handle(
        command("diar.block", block_index=0, offset_ms=0, pcm_fd=77, block_s=1, expected_rtf=0.1),
        [fd],
    )
    assert entered.wait(3)
    wrong = command("diar.cancel", "old")
    wrong["meeting_generation"] = 5
    assert state.handle(wrong)[0]["code"] == "not-prepared"
    assert not state.cancel.is_set()
    # Receipt remains available even after ordinary ledger capacity is spent.
    state.ledger.exhausted = True
    started = time.monotonic()
    assert state.handle(command("diar.cancel"))[0]["type"] == "diar.cancelled"
    assert time.monotonic() - started < 1
    release.set()
    assert state.thread is not None
    state.thread.join(3)
    assert not state.thread.is_alive() and state.poll() == []
    assert state.engine is None and not state.linker.globals


def test_duration_weighted_mean_is_associative_and_zero_mean_is_not_identity() -> None:
    a, b, c = Centroid([1, 0], 3), Centroid([0, 1], 5), Centroid([1, 1], 2)
    left, right = a.merged(b).merged(c), a.merged(b.merged(c))
    assert np.allclose(left.vector, right.vector) and left.duration == right.duration == 10
    zero = Centroid([1, 0], 1).merged(Centroid([-1, 0], 1))
    assert distance(zero, a) == 2


def test_malformed_d13_or_over_capacity_never_finishes() -> None:
    assembly = diar.BlockAssembly(
        {"meeting_id": "2026-11-10_120000-d1a2", "meeting_generation": 1}, 0
    )
    part = {
        "type": "diar.block.part",
        **assembly.identity,
        "block_index": 0,
        "seq": 0,
        "last": False,
        "segments": [[0, 1, "S1"]] * 512,
    }
    for seq in range(56):
        assembly.accept(dict(part, seq=seq))
    with pytest.raises(ValueError):
        assembly.accept(dict(part, seq=56, last=True))
    assert assembly.failed and not assembly.done and assembly.segments == []


def test_symlink_parent_dir_rejected_before_native(tmp_path: Path) -> None:
    actual = tmp_path / "actual"
    actual.mkdir()
    (tmp_path / "alias").symlink_to(actual, target_is_directory=True)
    with pytest.raises(OSError):
        with diar.checked_models([str(tmp_path / "alias"), str(actual)]):
            pytest.fail("symlink parent accepted")


def test_external_tensor_real_protobuf_refused_before_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from test_engine_scan import _field, _tensor

    # Model.graph.initializer TensorProto; scanner sees the reference regardless
    # of the external file's existence. Also refuse a legitimate in-dir file.
    for location in ("../escape", "weights.bin"):
        (tmp_path / "segmentation.onnx").write_bytes(_field(7, _field(5, _tensor(location))))
        (tmp_path / "embedder.onnx").write_bytes(b"\x08\x01")
        (tmp_path / "weights.bin").write_bytes(b"unused")

        def refuse(*args: Any, **kwargs: Any) -> Any:
            pytest.fail("native runtime reached before external-data refusal")

        monkeypatch.setattr(ctypes, "CDLL", refuse)
        state = diar.MeetingDiarState()
        try:
            state.handle(
                command(
                    "diar.load",
                    segmentation_dir=str(tmp_path),
                    embedder_dir=str(tmp_path),
                    threshold=0.5,
                    tau=0.45,
                    threads=2,
                )
            )
            assert drain(state)[-1]["code"] == "model"
        finally:
            state.cleanup()


def test_fd_limit_checked_without_reading_large_audio() -> None:
    import fcntl

    fd = os.memfd_create("too-large", os.MFD_ALLOW_SEALING)
    os.ftruncate(fd, diar.MAX_PCM_BYTES + 4)
    fcntl.fcntl(fd, diar.F_ADD_SEALS, diar.REQUIRED_SEALS)
    with pytest.raises(ValueError):
        diar.read_pcm(fd, 1380, threading.Event())
    with pytest.raises(OSError):
        os.fstat(fd)


def test_ancillary_truncation_closes_every_received_fd() -> None:
    import array
    import socket

    fd = sealed_pcm(np.zeros(1, dtype="<f4"))

    class Transport:
        def recvmsg(self, *args: Any) -> Any:
            return (
                b"\0",
                [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", [fd]).tobytes())],
                socket.MSG_CTRUNC,
                None,
            )

    receiver = diar.DiarReceiver()
    with pytest.raises(OSError):
        receiver.receive(Transport())
    with pytest.raises(OSError):
        os.fstat(fd)
    assert not receiver.descriptors


def test_peer_cannot_submit_role_events_as_commands() -> None:
    state = diar.MeetingDiarState()
    event = {
        "type": "diar.block.part",
        "meeting_id": "2026-11-10_120000-d1a2",
        "meeting_generation": 1,
        "block_index": 0,
        "seq": 0,
        "last": True,
        "segments": [],
    }
    result = state.handle(event)
    assert result[0]["code"] == "unknown-message"
    assert not state.ledger.requests and state.active is None


def test_wall_limit_aborts_pending_operation_without_done() -> None:
    state = loaded()
    try:
        state.active = command(
            "diar.block", block_index=0, offset_ms=0, pcm_fd=77, block_s=1, expected_rtf=0.1
        )
        state.deadline = time.monotonic() - 1
        result = state.poll()
        assert result[0]["type"] == "error" and result[0]["code"] == "limit"
        assert state.stopped and state.failed and state.poll() == []
    finally:
        state.cleanup()


def test_thirteenth_block_and_end_of_meeting_refused_with_limit() -> None:
    state = loaded()
    try:
        for index, offset, req in [(0, 14_400_000, "beyond"), (11, 0, "thirteenth")]:
            if index == 11:
                state.next_block = 12
            fd = sealed_pcm(np.zeros(16000, dtype="<f4"))
            result = state.handle(
                command(
                    "diar.block",
                    req,
                    block_index=index,
                    offset_ms=offset,
                    pcm_fd=70,
                    block_s=1,
                    expected_rtf=0.1,
                ),
                [fd],
            )
            assert result[0]["code"] == "limit"
            with pytest.raises(OSError):
                os.fstat(fd)
        assert not state.linker.globals
    finally:
        state.cleanup()


def test_finish_reuses_local_scatter_instead_of_refitting_only_global_means() -> None:
    from test_meeting_diar import centroid

    from astra_voice.meetings.diar_link import Profile, SpeakerLinker

    linker = SpeakerLinker(Profile(0.5, 0.45))
    # An already learned meeting scale separates these close global means.
    linker.globals = {1: centroid(0.0), 2: centroid(0.6)}
    linker.cut = 0.1
    linker.cut_weight = 100
    assert linker.finish() == ({"S1": "S1", "S2": "S2"}, 2)


@pytest.mark.parametrize(
    "body,ok",
    [
        ('write(4<UNIX-STREAM:[123->456]>, "data", 4) = 4', True),
        ('write(5</memfd:pcm (deleted)>, "data", 4) = 4', True),
        ('openat(AT_FDCWD, "/model.onnx", O_RDONLY|O_CLOEXEC) = 7</model.onnx>', True),
        ('openat(AT_FDCWD, "/model.onnx", O_RDONLY|O_CLOEXEC <unfinished ...>', True),
        ('write(8</tmp/deleted (deleted)>, "data", 4) = 4', False),
        ('write(9, "data", 4) = 4', False),
        ('openat(AT_FDCWD, "/tmp/features", O_RDWR|O_CREAT, 0600) = 5', False),
        ("mmap(NULL, 4096, PROT_WRITE, MAP_SHARED, 4</tmp/features>, 0) = 0x2000", False),
        ('write(9</tmp/features>, "data", 4 <unfinished ...>', False),
    ],
)
def test_strace_audit_including_deleted_files_and_unfinished_calls(body: str, ok: bool) -> None:
    assert (_audit_trace("123 " + body) == []) is ok


def _audit_trace(text: str) -> list[str]:
    import runpy

    namespace = runpy.run_path(
        str(Path(__file__).resolve().parents[1] / "integration/test_meeting_diar_sherpa.py")
    )
    checker: Callable[[str], list[str]] = namespace["trace_violations"]
    return checker(text)


def test_thread_capacity_failure_closes_transferred_descriptor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = loaded()
    fd = sealed_pcm(np.zeros(16000, dtype="<f4"))

    def refused(thread: threading.Thread) -> None:
        raise RuntimeError("no thread capacity")

    monkeypatch.setattr(threading.Thread, "start", refused)
    result = state.handle(
        command("diar.block", block_index=0, offset_ms=0, pcm_fd=77, block_s=1, expected_rtf=0.1),
        [fd],
    )
    assert result[0]["code"] == "memory" and state.active is None
    with pytest.raises(OSError):
        os.fstat(fd)
    state.cleanup()


def test_terminal_block_receipt_allows_finish_before_thread_epilogue() -> None:
    state = loaded()
    put = state._put
    terminal, release = threading.Event(), threading.Event()

    def delayed(message: dict[str, Any]) -> None:
        put(message)
        if message["type"] == "diar.block.done":
            terminal.set()
            assert release.wait(5)

    state._put = delayed  # type: ignore[method-assign]
    fd = sealed_pcm(np.zeros(16000, dtype="<f4"))
    state.handle(
        command("diar.block", block_index=0, offset_ms=0, pcm_fd=77, block_s=1, expected_rtf=0.1),
        [fd],
    )
    assert terminal.wait(3)
    old_thread = state.thread
    assert old_thread is not None and old_thread.is_alive()
    try:
        events = drain(state)
        assert events[-1]["type"] == "diar.block.done"
        assert state.handle(command("diar.finish"))[0]["type"] == "meeting.ack"
        assert drain(state)[-1]["type"] == "diar.finished"
    finally:
        release.set()
        old_thread.join(3)
        state.cleanup()


# Round 2: regression tests for the three independently reproduced findings.
def test_model_snapshot_survives_source_inode_rewrite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import fcntl

    from test_engine_scan import _field, _tensor

    original = b"\x08\x01"
    for name in ("segmentation.onnx", "embedder.onnx"):
        (tmp_path / name).write_bytes(original)
    parser = collect_external_locations
    checked: list[Path] = []

    def checked_then_overwritten(path: Path) -> list[str]:
        locations = parser(path)
        checked.append(path)
        # Keep the same inode; a pathname/descriptor pin alone does not help.
        with (tmp_path / "segmentation.onnx").open("r+b") as source:
            source.write(_field(7, _field(5, _tensor("../forbidden"))))
            source.truncate()
        return locations

    monkeypatch.setattr(diar, "collect_external_locations", checked_then_overwritten)
    with diar.checked_models([str(tmp_path), str(tmp_path)]) as paths:
        assert len(checked) == 2
        assert Path(paths[0]).read_bytes() == original
        for path in paths:
            descriptor = int(Path(path).name)
            assert (
                fcntl.fcntl(descriptor, diar.F_GET_SEALS) & diar.REQUIRED_SEALS
                == diar.REQUIRED_SEALS
            )
            with pytest.raises(OSError):
                os.write(descriptor, b"changed")
    for path in paths:
        assert not Path(path).exists()


def test_active_request_retry_after_first_part_returns_receipt() -> None:
    from test_meeting_diar import FakeEngine, centroid

    class Many(FakeEngine):
        def process(self, audio: Any, cancel: threading.Event) -> Any:
            self.calls += 1
            return [(i, i + 1, 0) for i in range(5000)], {0: centroid(0)}

    state = loaded(Many)
    message = command(
        "diar.block",
        "retry-active",
        block_index=0,
        offset_ms=0,
        pcm_fd=77,
        block_s=1,
        expected_rtf=0.1,
    )
    fd = sealed_pcm(np.zeros(16000, dtype="<f4"))
    receipt = state.handle(message, [fd])
    events: list[dict[str, Any]] = []
    try:
        deadline = time.monotonic() + 3
        while not events:
            events.extend(state.poll())
            assert time.monotonic() < deadline
            time.sleep(0.001)
        assert events[0]["type"] == "diar.block.part" and not events[0]["last"]
        assert state.active is not None
        retry_fd = sealed_pcm(np.zeros(16000, dtype="<f4"))
        assert state.handle(message, [retry_fd]) == receipt
        with pytest.raises(OSError):
            os.fstat(retry_fd)
        conflict_fd = sealed_pcm(np.zeros(16000, dtype="<f4"))
        assert (
            state.handle(dict(message, offset_ms=1000), [conflict_fd])[0]["code"] == "req-conflict"
        )
        events.extend(drain(state))
        assert state.engine.calls == 1
        replay_fd = sealed_pcm(np.zeros(16000, dtype="<f4"))
        assert state.handle(message, [replay_fd]) == receipt
        assert drain(state) == events
        assert state.engine.calls == 1
    finally:
        state.cleanup()


def _run_buffered_cancel(
    monkeypatch: pytest.MonkeyPatch,
    role: str,
    *,
    partial: bool,
    blocked: bool = False,
) -> tuple[int, bytearray, float, bool]:
    """Run the real transport against deterministic send/recv/select, no OS socket."""
    from test_meeting_diar import IDENTITY
    from test_meeting_ipc import command as legacy_command

    from astra_voice.worker import ipc, meeting_roles

    is_diar = role == "meeting-diar"
    if is_diar:
        request = command("diar.cancel")
        events = [
            {
                "type": "diar.block.part",
                **IDENTITY,
                "block_index": 0,
                "seq": i,
                "last": bool(i),
                "segments": [[i, i + 1, "S1"]],
            }
            for i in range(2)
        ]
        events.append(
            {**command("diar.block"), "type": "diar.block.done", "block_index": 0, "segments": []}
        )
        final = {**request, "type": "diar.cancelled"}
    else:
        kind = "meeting.capture.shutdown" if role == "meeting-capture" else "meeting.job.cancel"
        request = legacy_command(role, kind, "cancel-old")
        events = [{"type": "pong", "req": "earlier", "request_type": "ping"}]
        final = {
            **request,
            "type": "meeting.capture.bye" if role == "meeting-capture" else "meeting.job.cancelled",
        }
    hello_size = len(ipc.encode(ipc.make_hello(role=role), role=role))

    class Handler:
        stopped = False
        exit_code = 0
        emitted = False
        cancelled_at: float | None = None

        def poll(self) -> list[dict[str, Any]]:
            if self.emitted:
                return []
            self.emitted = True
            return events

        def handle(self, message: Any, *args: Any) -> list[dict[str, Any]]:
            assert message == request
            self.cancelled_at = clock[0]
            self.stopped = True
            return [final]

        def cleanup(self) -> None:
            pass

    handler = Handler()
    module = __import__("astra_voice.worker." + role.replace("-", "_"), fromlist=["x"])
    cls = {
        "meeting-diar": "MeetingDiarState",
        "meeting-capture": "MeetingCaptureState",
        "meeting-transcribe": "MeetingTranscribeState",
    }[role]
    monkeypatch.setattr(module, cls, lambda: handler)
    clock = [0.0]
    monkeypatch.setattr(meeting_roles, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    incoming = bytearray(ipc.encode(request, role=role))
    sent = bytearray()

    class Connection:
        calls = 0
        closed = False

        def setblocking(self, flag: bool) -> None:
            assert flag is False

        def send(self, data: bytes) -> int:
            self.calls += 1
            count = min(
                len(data),
                hello_size if self.calls == 1 else 10 if partial and self.calls == 2 else len(data),
            )
            sent.extend(data[:count])
            return count

        def recv(self, size: int) -> bytes:
            result = bytes(incoming[:size])
            del incoming[:size]
            return result

        def recvmsg(self, size: int, *args: Any) -> Any:
            return self.recv(size), [], 0, None

        def close(self) -> None:
            self.closed = True

    connection = Connection()

    def ready(read: Any, write: Any, exceptional: Any, timeout: float) -> Any:
        clock[0] += timeout if blocked and handler.stopped else 0.01
        if connection.calls < (2 if partial else 1):
            return [], [connection], []
        if incoming:
            return [connection], [], []
        if blocked:
            # Bound an old unbounded loop so this regression fails promptly.
            if handler.cancelled_at is not None and clock[0] - handler.cancelled_at > 1.2:
                raise OSError("old transport did not stop on its own deadline")
            return [], [], []
        if not is_diar:
            clock[0] += 2.0  # No new diar drain timeout may affect legacy roles.
        return [], [connection], []

    monkeypatch.setattr(meeting_roles, "select", SimpleNamespace(select=ready))
    code = meeting_roles.run_role(connection, role, parent_pid=os.getppid())  # type: ignore[arg-type]
    assert handler.cancelled_at is not None
    return code, sent, clock[0] - handler.cancelled_at, connection.closed


@pytest.mark.parametrize("partial", [False, True])
def test_transport_cancel_drops_unstarted_diar_frames(
    monkeypatch: pytest.MonkeyPatch,
    partial: bool,
) -> None:
    from astra_voice.worker import ipc

    code, sent, _, closed = _run_buffered_cancel(monkeypatch, "meeting-diar", partial=partial)
    messages = ipc.FrameReader(role="meeting-diar").feed(bytes(sent))
    assert code == 0 and closed
    assert [m["type"] for m in messages] == (
        ["hello", "diar.block.part", "diar.cancelled"] if partial else ["hello", "diar.cancelled"]
    )
    assert not any(m["type"] == "diar.block.done" for m in messages)


def test_transport_cancel_exit_deadline_for_nonreading_peer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    code, _, elapsed, closed = _run_buffered_cancel(
        monkeypatch,
        "meeting-diar",
        partial=True,
        blocked=True,
    )
    assert code == 2 and closed and elapsed <= 1.0


@pytest.mark.parametrize("role", ["meeting-capture", "meeting-transcribe"])
def test_transport_cancel_keeps_legacy_buffer_and_shutdown_policy(
    monkeypatch: pytest.MonkeyPatch,
    role: str,
) -> None:
    from astra_voice.worker import ipc

    code, sent, elapsed, closed = _run_buffered_cancel(monkeypatch, role, partial=True)
    messages = ipc.FrameReader(role=role).feed(bytes(sent))
    assert code == 0 and closed and elapsed > 1.0
    assert [m["type"] for m in messages] == [
        "hello",
        "pong",
        "meeting.capture.bye" if role == "meeting-capture" else "meeting.job.cancelled",
    ]


def test_model_snapshot_limit_precedes_read_and_allocation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "huge.onnx"
    with path.open("wb") as stream:
        stream.truncate(diar.MODEL_SNAPSHOT_LIMITS[0] + 1)
    source = os.open(path, os.O_RDONLY)

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("oversize model was read/allocated")

    try:
        # Restore before pytest's /proc-reading teardown hooks run.
        with monkeypatch.context() as patches:
            patches.setattr(os, "read", forbidden)
            patches.setattr(os, "memfd_create", forbidden)
            with pytest.raises(LimitError):
                diar._model_snapshot(source, diar.MODEL_SNAPSHOT_LIMITS[0])
    finally:
        os.close(source)


def test_cancel_cleanup_joins_only_once_for_transport_deadline() -> None:
    from unittest.mock import Mock

    state = diar.MeetingDiarState()
    thread = Mock(spec=threading.Thread)
    state.thread = thread
    state.cleanup()
    state.cleanup()
    thread.join.assert_called_once_with(timeout=0.1)


# Round 3: nondumpable strace lines supplied by the reviewer, without fd labels.
_R3_CREATE = "191996 memfd_create(0x791fb71068d0, MFD_CLOEXEC|MFD_ALLOW_SEALING) = 12"
_R3_WRITE = "191996 write(12, 0x791fb67bb030, 1048576) = 1048576"


def test_trace_nondumpable_model_snapshots_from_real_sample() -> None:
    lines = [
        _R3_CREATE,
        *[_R3_WRITE] * 5,
        "191996 write(12, 0x791fb0000df0, 750033) = 750033",
        "191996 memfd_create(0x791fb7104e50, MFD_CLOEXEC|MFD_ALLOW_SEALING) = 20",
        *["191996 write(20, 0x791fb0001e30, 1048576) = 1048576"] * 26,
        "191996 write(20, 0x791fb0001e30, 1018188) = 1018188",
    ]
    assert _audit_trace("\n".join(lines)) == []


@pytest.mark.parametrize(
    "body",
    [
        "write(12, 0x791fb67bb030, 1048576)",
        "pwrite64(12, 0x791fb67bb030, 1048576, 0)",
        "writev(12, [{iov_base=0x791fb67bb030, iov_len=1048576}], 1)",
        "pwritev(12, 0x791fb67bb030, 1, 0)",
        "pwritev2(12, 0x791fb67bb030, 1, 0, RWF_DSYNC)",
        "sendfile(12, 9, NULL, 1048576)",
        "copy_file_range(9, NULL, 12, NULL, 1048576, 0)",
        "splice(9, NULL, 12, NULL, 1048576, 0)",
        "tee(9, 12, 1048576, 0)",
        "ftruncate(12, 1048576)",
        "fallocate(12, 0, 0, 1048576)",
        "mmap(NULL, 4096, PROT_READ|PROT_WRITE, MAP_SHARED, 12, 0)",
    ],
)
@pytest.mark.parametrize("owned", [False, True])
def test_trace_output_calls_require_proven_memory_target(body: str, owned: bool) -> None:
    prefix = _R3_CREATE + "\n" if owned else ""
    assert (_audit_trace(prefix + "191996 " + body + " = 0") == []) is owned


@pytest.mark.parametrize(
    "copy",
    [
        "dup(12) = 21",
        "dup2(12, 21) = 21",
        "dup3(12, 21, O_CLOEXEC) = 21",
        "fcntl(12, F_DUPFD, 21) = 21",
        "fcntl(12, F_DUPFD_CLOEXEC, 21) = 21",
    ],
)
def test_trace_memfd_duplicates_outlive_source_close(copy: str) -> None:
    assert (
        _audit_trace(
            "\n".join(
                [
                    _R3_CREATE,
                    "191996 " + copy,
                    "191996 close(12) = 0",
                    "191996 write(21, 0x791fb67bb030, 1048576) = 1048576",
                ]
            )
        )
        == []
    )


@pytest.mark.parametrize(
    "replace",
    [
        "dup2(9, 12) = 12",
        "dup3(9, 12, O_CLOEXEC) = 12",
        "fcntl(9, F_DUPFD, 12) = 12",
        "fcntl(9, F_DUPFD_CLOEXEC, 12) = 12",
        'openat(AT_FDCWD, "/tmp/ordinary", O_RDWR|O_CLOEXEC) = 12',
        'open("/tmp/ordinary", O_RDWR) = 12',
        'openat2(AT_FDCWD, "/tmp/ordinary", {flags=O_RDWR}, 24) = 12',
    ],
)
def test_trace_replaced_descriptor_loses_memfd_status(replace: str) -> None:
    assert "file-write" in _audit_trace(
        "\n".join(
            [
                _R3_CREATE,
                "191996 close(12) = 0",
                "191996 " + replace,
                _R3_WRITE,
            ]
        )
    )


@pytest.mark.parametrize("replace", ["dup2(9, 12) = 12", "dup3(9, 12, 0) = 12"])
def test_trace_dup_overwrites_open_memfd_target(replace: str) -> None:
    assert _audit_trace("\n".join([_R3_CREATE, "191996 " + replace, _R3_WRITE])) == ["file-write"]


def test_trace_failed_dup_does_not_replace_target() -> None:
    assert (
        _audit_trace(
            "\n".join(
                [
                    _R3_CREATE,
                    "191996 dup2(9, 12) = -1 EBADF (Bad file descriptor)",
                    _R3_WRITE,
                ]
            )
        )
        == []
    )


@pytest.mark.parametrize("created", [False, True])
def test_trace_memfd_numbers_are_not_global_across_pids(created: bool) -> None:
    lines = [_R3_CREATE, _R3_WRITE.replace("191996", "191992")]
    if created:
        lines = [
            _R3_CREATE,
            _R3_CREATE.replace("191996", "191992"),
            "191992 close(12) = 0",
            _R3_WRITE,
        ]
    assert (_audit_trace("\n".join(lines)) == []) is created


def test_trace_failed_memfd_create_does_not_authorize_write() -> None:
    assert _audit_trace(
        "\n".join(
            [
                "191996 memfd_create(0x791fb71068d0, MFD_CLOEXEC) = -1 ENOMEM "
                "(Cannot allocate memory)",
                _R3_WRITE,
            ]
        )
    ) == ["file-write"]


@pytest.mark.parametrize(
    "body",
    [
        "sendfile(9, 12, NULL, 1048576)",
        "copy_file_range(12, NULL, 9, NULL, 1048576, 0)",
        "splice(12, NULL, 9, NULL, 1048576, 0)",
        "tee(12, 9, 1048576, 0)",
    ],
)
def test_trace_memory_input_never_authorizes_file_output(body: str) -> None:
    assert _audit_trace(_R3_CREATE + "\n191996 " + body + " = 1048576") == ["file-write"]


@pytest.mark.parametrize(
    "body",
    [
        "sendfile(9<UNIX-STREAM:[123->456]>, 12, NULL, 1048576)",
        "copy_file_range(12, NULL, 9<UNIX-STREAM:[123->456]>, NULL, 1048576, 0)",
        "splice(12, NULL, 9<UNIX-STREAM:[123->456]>, NULL, 1048576, 0)",
        "tee(12, 9<UNIX-STREAM:[123->456]>, 1048576, 0)",
    ],
)
def test_trace_explicit_socket_transfer_target_is_allowed(body: str) -> None:
    assert _audit_trace("191996 " + body + " = 1048576") == []


@pytest.mark.parametrize(
    "clone",
    [
        "fork() = 200",
        "vfork() = 200",
        "clone(child_stack=NULL, flags=CLONE_VM|SIGCHLD) = 200",
        "clone3({flags=CLONE_VM, exit_signal=SIGCHLD}, 88) = 200",
    ],
)
def test_trace_fork_copies_inherited_memfd_set(clone: str) -> None:
    assert (
        _audit_trace(
            "\n".join(
                [
                    _R3_CREATE,
                    "191996 " + clone,
                    "191996 close(12) = 0",
                    _R3_WRITE.replace("191996", "200"),
                ]
            )
        )
        == []
    )


def test_trace_clone_files_shares_close_and_replacement() -> None:
    assert _audit_trace(
        "\n".join(
            [
                _R3_CREATE,
                "191996 clone(child_stack=NULL, flags=CLONE_FILES|CLONE_VM) = 200",
                "200 close(12) = 0",
                _R3_WRITE,
            ]
        )
    ) == ["file-write"]


def test_trace_hidden_clone_flags_do_not_guess_inheritance() -> None:
    assert (
        _audit_trace(
            "\n".join(
                [
                    _R3_CREATE,
                    "191996 clone3(0x791fb71068d0, 88) = 200",
                    _R3_WRITE.replace("191996", "200"),
                    _R3_WRITE,
                    _R3_CREATE.replace("191996", "200"),
                    "191996 close(12) = 0",
                    _R3_WRITE.replace("191996", "200"),
                ]
            )
        )
        == ["file-write"] * 3
    )


def test_trace_child_own_memfd_before_resumed_clone_is_preserved() -> None:
    assert (
        _audit_trace(
            "\n".join(
                [
                    "100 clone3(0x791fb71068d0, 88 <unfinished ...>",
                    _R3_CREATE,
                    "100 <... clone3 resumed>) = 191996",
                    _R3_WRITE,
                ]
            )
        )
        == []
    )


def test_trace_child_close_before_resumed_shared_clone_revokes_parent() -> None:
    assert _audit_trace(
        "\n".join(
            [
                _R3_CREATE,
                "191996 clone(child_stack=NULL, flags=CLONE_FILES <unfinished ...>",
                "200 close(12) = 0",
                "191996 <... clone resumed>) = 200",
                _R3_WRITE,
            ]
        )
    ) == ["file-write"]


def test_trace_unfinished_memory_write_and_pid_exit() -> None:
    assert _audit_trace(
        "\n".join(
            [
                _R3_CREATE,
                "191996 write(12, 0x791fb67bb030, 1048576 <unfinished ...>",
                "191996 +++ killed by SIGKILL +++",
                _R3_WRITE,
            ]
        )
    ) == ["file-write"]


def test_trace_resumed_memfd_create_and_write() -> None:
    assert (
        _audit_trace(
            "\n".join(
                [
                    "191996 memfd_create(0x791fb71068d0, MFD_ALLOW_SEALING <unfinished ...>",
                    "191996 <... memfd_create resumed>) = 12",
                    "191996 write(12, 0x791fb67bb030, 1048576 <unfinished ...>",
                    "191996 <... write resumed>) = 1048576",
                ]
            )
        )
        == []
    )


def test_trace_syscall_list_covers_lifecycle_and_output_calls() -> None:
    from tools.meeting_diar_check import DIAR_TRACE_CALLS

    assert {
        "memfd_create",
        "close",
        "dup",
        "dup2",
        "dup3",
        "fcntl",
        "fork",
        "clone",
        "clone3",
        "sendfile",
        "copy_file_range",
        "splice",
        "tee",
        "pwritev",
        "pwritev2",
        "ftruncate",
        "fallocate",
        "mmap",
        "write",
        "pwrite64",
        "writev",
    } <= set(DIAR_TRACE_CALLS.split(","))


@pytest.mark.parametrize(
    "body",
    [
        "sendfile(12</memfd:model>(deleted), 9, NULL, 4096)",
        "copy_file_range(9, NULL, 12</memfd:model>(deleted), NULL, 4096, 0)",
        "splice(9, NULL, 12</memfd:model>(deleted), NULL, 4096, 0)",
        "tee(9, 12</memfd:model>(deleted), 4096, 0)",
        "pwritev(12</memfd:model>(deleted), 0x791fb67bb030, 1, 0)",
        "pwritev2(12</memfd:model>(deleted), 0x791fb67bb030, 1, 0, 0)",
        "ftruncate(12</memfd:model>(deleted), 4096)",
        "fallocate(12</memfd:model>(deleted), 0, 0, 4096)",
    ],
)
def test_trace_new_outputs_require_own_memfd_even_with_name(body: str) -> None:
    assert _audit_trace("191996 " + body + " = 0") == ["file-write"]
    assert _audit_trace(_R3_CREATE + "\n191996 " + body + " = 0") == []


def test_r2_b_pcm_grid_ceiling_passes_validation_before_allocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import fcntl

    class Validated(Exception):
        pass

    def allocate(count: int, **kwargs: Any) -> Any:
        assert count == 1380 * diar.RATE
        raise Validated

    fd = os.memfd_create("grid-ceiling", os.MFD_ALLOW_SEALING)
    os.ftruncate(fd, 1380 * diar.RATE * 4)
    fcntl.fcntl(fd, diar.F_ADD_SEALS, diar.REQUIRED_SEALS)
    monkeypatch.setattr(np, "empty", allocate)
    with pytest.raises(Validated):
        diar.read_pcm(fd, 1380, threading.Event())
    with pytest.raises(OSError):
        os.fstat(fd)
