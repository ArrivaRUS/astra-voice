"""Meetings v1 role skeletons. No audio, inference, threads or media file I/O.

A not-prepared outcome never claims physical close, durable finalization or
publication. M2 implements capture; M4/M7 implement transcribe/diar and fd rights.
"""

from __future__ import annotations

import os
import select
import signal
import socket
import time
from collections.abc import Callable
from copy import deepcopy
from typing import Any

from astra_voice.worker import ipc
from astra_voice.worker.meeting_contract import COMMAND_FIELDS, ROLES, RequestLedger

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


def run_role(connection: socket.socket, role: str, *, parent_pid: int) -> int:
    """Single bounded control loop, including shutdown -> bye -> exit 0.

    SIGTERM/PDEATHSIG only sets a flag; there are no media resources in M1.
    The loop checks parent identity every 50 ms, never logs peer payloads.
    """
    handler = MeetingRole(role)
    reader = ipc.FrameReader(role=role)
    outgoing = bytearray(ipc.encode(ipc.make_hello(role=role), role=role))
    terminated = False

    def terminate(signum: int, frame: Any) -> None:
        nonlocal terminated
        terminated = True

    previous: Any = None
    try:
        previous = signal.signal(signal.SIGTERM, terminate)
        connection.setblocking(False)
        while not terminated and os.getppid() == parent_pid:
            if handler.stopped and not outgoing:
                return handler.exit_code
            readable, writable, _ = select.select(
                [] if handler.stopped else [connection], [connection] if outgoing else [], [], 0.05
            )
            if writable and outgoing:
                # Snapshot prevents an injected transport retaining a mutable view.
                count = connection.send(bytes(outgoing))
                if count == 0:
                    return 2
                del outgoing[:count]
            if readable and not handler.stopped:
                try:
                    data = connection.recv(ipc.MAX_FRAME_BYTES)
                except BlockingIOError:
                    continue
                if not data:
                    return 0
                try:
                    frames = reader.feed_frames(data)
                except ipc.FrameError:
                    return 2  # Dangerous length: bounded terminal refusal.
                for frame in frames:
                    try:
                        message = ipc.decode(frame, role=role)
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
                    for reply in replies:
                        outgoing.extend(ipc.encode(reply, role=role))
                    if len(outgoing) > MAX_SEND_BUFFER:
                        return 2
                    if handler.stopped:
                        break
    except (OSError, ValueError):
        return 2
    finally:
        if previous is not None:
            signal.signal(signal.SIGTERM, previous)
        connection.close()
    return 0
