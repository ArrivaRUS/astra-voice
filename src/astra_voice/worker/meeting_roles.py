"""Meetings v1 control transport and role dispatch.

Role implementations are imported only after entry-point hardening. Each
process owns one runtime; not-prepared never claims durable publication.
"""

from __future__ import annotations

import os
import select
import signal
import socket
import time
from collections import deque
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from astra_voice.worker import ipc
from astra_voice.worker.meeting_contract import (
    CANCEL_TIMEOUT_S,
    COMMAND_FIELDS,
    ROLES,
    RequestLedger,
)

Message = dict[str, Any]
MAX_SEND_BUFFER = 4 * ipc.MAX_FRAME_BYTES


def silence_native_stderr() -> bool:
    """Suppress fd2 before native imports; failure refuses role startup."""
    try:
        descriptor = os.open(os.devnull, os.O_WRONLY | os.O_CLOEXEC)
        try:
            os.dup2(descriptor, 2)
        finally:
            if descriptor != 2:
                os.close(descriptor)
    except OSError:
        return False
    return True


def failure(message: Message, code: str) -> Message:
    return {
        "type": "error",
        "code": code,
        "message": "meeting-command-failed",
        **{
            key: message[key]
            for key in ("req", "request_type", "meeting_id", "meeting_generation")
            if key in message
        },
    }


class MeetingRole:
    """One role/process generation; duplicate requests retain their first outcome."""

    def __init__(self, role: str, *, clock: Callable[[], float] = time.monotonic) -> None:
        if role not in ROLES:
            raise ValueError("meeting-role-invalid")
        self.role = role
        self.clock = clock
        self.stopped = False
        self.exit_code = 0
        self.ledger = RequestLedger()

    def handle(self, message: Message) -> list[Message]:
        message = ipc.decode(ipc.encode(message, role=self.role)[4:], role=self.role)
        if message["type"] not in COMMAND_FIELDS[self.role]:
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
        reply: Message = {
            key: message[key]
            for key in ("req", "request_type", "meeting_id", "meeting_generation")
            if key in message
        }
        if kind == "ping":
            reply["type"] = "pong"
        elif kind == "meeting.capture.shutdown":
            reply["type"] = "meeting.capture.bye"
            self.stopped = True
        elif kind == "meeting.detector.stop":
            reply["type"] = "meeting.detector.stopped"
        elif kind == "meeting.call.query":
            reply.update(type="meeting.call.state", active=False)
        elif kind == "model.unload":
            reply["type"] = "model.unloaded"
        elif kind == "diar.cancel":
            reply["type"] = "diar.cancelled"
        else:
            stale = (
                "admission_mono_ms" in message
                and not 0 <= self.clock() * 1000 - message["admission_mono_ms"] <= 1000
            )
            reply = failure(message, "admission-stale" if stale else "not-prepared")
        replies = [ipc.decode(ipc.encode(reply, role=self.role)[4:], role=self.role)]
        self.ledger.finish(str(message["req"]), replies)
        return deepcopy(replies)


_DIAR_RESULTS = frozenset({"diar.loaded", "diar.block.part", "diar.block.done", "diar.finished"})


@dataclass
class _OutputFrame:
    data: bytes
    kind: str
    offset: int = 0


class _DiarOutput:
    """Frame-aware output for diar only; legacy roles keep their byte buffer.

    Cancellation removes every unstarted result. A partially sent frame keeps
    its remaining bytes ahead of the cancellation reply, preserving framing.
    Once cancelled, no further result may enter this transport generation.
    """

    def __init__(self) -> None:
        self.frames: deque[_OutputFrame] = deque()
        self.pending = 0
        self.cancelled = False

    def __bool__(self) -> bool:
        return bool(self.frames)

    def __len__(self) -> int:
        return self.pending

    def append(self, data: bytes, kind: str) -> None:
        if self.cancelled and kind in _DIAR_RESULTS:
            return
        self.frames.append(_OutputFrame(data, kind))
        self.pending += len(data)

    def discard_results(self) -> None:
        self.cancelled = True
        retained = deque(
            frame for frame in self.frames if frame.offset or frame.kind not in _DIAR_RESULTS
        )
        self.frames = retained
        self.pending = sum(len(frame.data) - frame.offset for frame in retained)

    def send(self, connection: socket.socket) -> int:
        frame = self.frames[0]
        count = connection.send(frame.data[frame.offset :])
        frame.offset += count
        self.pending -= count
        if frame.offset == len(frame.data):
            self.frames.popleft()
        return count


def run_role(connection: socket.socket, role: str, *, parent_pid: int) -> int:
    """Single bounded control loop, including shutdown -> bye -> exit 0.

    SIGTERM/PDEATHSIG sets a flag; capture cleanup physically revokes streams
    within a bounded wait independent of storage. Parent identity is checked
    every 50 ms. Peer payloads are never logged.
    """
    if role == "meeting-capture":
        from astra_voice.worker.meeting_capture import MeetingCaptureState

        handler: Any = MeetingCaptureState()
    elif role == "meeting-transcribe":
        from astra_voice.worker.meeting_transcribe import MeetingTranscribeState

        handler = MeetingTranscribeState()
    elif role == "meeting-diar":
        # NumPy only does small centroid operations. An inherited BLAS thread
        # count can consume the role's entire 1.5 GiB virtual-address allowance.
        os.environ["OPENBLAS_NUM_THREADS"] = "1"
        from astra_voice.worker.meeting_diar import DiarReceiver, MeetingDiarState

        handler = MeetingDiarState()
    else:
        handler = MeetingRole(role)
    diar_receiver = DiarReceiver() if role == "meeting-diar" else None
    polling = role in {"meeting-capture", "meeting-transcribe", "meeting-diar"}
    reader = ipc.FrameReader(role=role)
    outgoing: bytearray | _DiarOutput = _DiarOutput() if diar_receiver is not None else bytearray()

    def enqueue(message: Message) -> None:
        data = ipc.encode(message, role=role)
        if isinstance(outgoing, _DiarOutput):
            outgoing.append(data, message["type"])
        else:
            outgoing.extend(data)

    enqueue(ipc.make_hello(role=role))
    drain_deadline: float | None = None
    terminated = False

    def terminate(signum: int, frame: Any) -> None:
        nonlocal terminated
        terminated = True

    previous: Any = None
    try:
        previous = signal.signal(signal.SIGTERM, terminate)
        connection.setblocking(False)
        while not terminated and os.getppid() == parent_pid:
            # Transcribe events wait in the role (bounded by credits) while the
            # peer is not reading; capture keeps its own overflow policy.
            if polling and (role == "meeting-capture" or len(outgoing) < ipc.MAX_FRAME_BYTES):
                for event in handler.poll():
                    enqueue(event)
                if len(outgoing) > MAX_SEND_BUFFER:
                    return 2
                if handler.exit_code:
                    return int(handler.exit_code)
            if handler.stopped and not outgoing:
                return int(handler.exit_code)
            wait_s = 0.05
            if isinstance(outgoing, _DiarOutput) and handler.stopped:
                if drain_deadline is None:
                    drain_deadline = time.monotonic() + CANCEL_TIMEOUT_S
                remaining = drain_deadline - time.monotonic()
                if remaining <= 0:
                    return 2  # Nonreading peer: close rather than wait indefinitely.
                wait_s = min(wait_s, remaining)
            readable, writable, _ = select.select(
                [] if handler.stopped else [connection],
                [connection] if outgoing else [],
                [],
                wait_s,
            )
            if writable and outgoing:
                # Snapshot prevents an injected transport retaining a mutable view.
                if isinstance(outgoing, _DiarOutput):
                    count = outgoing.send(connection)
                else:
                    count = connection.send(bytes(outgoing))
                    del outgoing[:count]
                if count == 0:
                    return 2
            if readable and not handler.stopped:
                try:
                    descriptors: list[int] = []
                    if diar_receiver is not None:
                        incoming = diar_receiver.receive(connection)
                        if incoming is None:
                            continue
                        payload, descriptors = incoming
                        frames = [payload]
                        data = b"received"
                    else:
                        data = connection.recv(ipc.MAX_FRAME_BYTES)
                except BlockingIOError:
                    continue
                if not data:
                    return 0
                try:
                    if diar_receiver is None:
                        frames = reader.feed_frames(data)
                except ipc.FrameError:
                    return 2  # Dangerous length: bounded terminal refusal.
                for frame in frames:
                    cancel_requested_at: float | None = None
                    try:
                        message = ipc.decode(frame, role=role)
                        if role == "meeting-capture":
                            for event in handler.poll():
                                enqueue(event)
                        if diar_receiver is not None:
                            if message["type"] == "diar.cancel":
                                cancel_requested_at = time.monotonic()
                            owned, descriptors = descriptors, []
                            replies = handler.handle(message, owned)
                        else:
                            replies = handler.handle(message)
                    except ipc.FrameError as exc:
                        if exc.code == ipc.PROTOCOL_MISMATCH:
                            return 2
                        # No unvalidated req/type/identity is reflected to the peer.
                        replies = [
                            {
                                "type": "error",
                                "code": exc.code,
                                "message": "meeting-protocol-error",
                            }
                        ]
                    finally:
                        for descriptor in descriptors:
                            os.close(descriptor)
                        descriptors = []
                    if (
                        isinstance(outgoing, _DiarOutput)
                        and cancel_requested_at is not None
                        and any(reply["type"] == "diar.cancelled" for reply in replies)
                    ):
                        outgoing.discard_results()
                        drain_deadline = cancel_requested_at + CANCEL_TIMEOUT_S
                    for reply in replies:
                        enqueue(reply)
                    if len(outgoing) > MAX_SEND_BUFFER:
                        return 2
                    if handler.stopped:
                        break
    except (OSError, ValueError):
        return 2
    finally:
        if polling:
            handler.cleanup()
        if previous is not None:
            signal.signal(signal.SIGTERM, previous)
        if diar_receiver is not None:
            diar_receiver.close()
        connection.close()
    return 0
