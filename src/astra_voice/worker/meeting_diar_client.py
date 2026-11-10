"""Parent-side transport for the isolated diar role; no acoustic feature imports.

All waits are on the transcriber's executor thread. Cancellation is checked
at most every 50 ms, independently of inference, credits and the GUI channel.
close() never returns while the owned child is alive (including after SIGKILL).
"""

from __future__ import annotations

import array
import contextlib
import fcntl
import os
import select
import signal
import socket
import stat
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from astra_voice.meetings.diar_link import DIAR_PROFILES
from astra_voice.worker import ipc
from astra_voice.worker.meeting_contract import operation_timeout
from astra_voice.worker.meeting_diar import (
    F_ADD_SEALS,
    MODEL_FILES,
    MODEL_SNAPSHOT_LIMITS,
    REQUIRED_SEALS,
    BlockAssembly,
)
from astra_voice.worker.supervisor import worker_command

ROLE = "meeting-diar"
Message = dict[str, Any]


class DiarCancelled(Exception):
    """Parent job cancellation, distinct from a diarization failure."""


def model_fingerprint(models: Message) -> list[dict[str, Any]]:
    """Bounded content hashes, not voice features; missing models fail in the role.

    Do not follow a file symlink or read a FIFO/device. The role independently
    pins/validates the entire directory path and snapshots the selected ONNX.
    """
    import hashlib

    result = []
    for key, names, maximum in zip(
        ("segmentation_dir", "embedder_dir"), MODEL_FILES, MODEL_SNAPSHOT_LIMITS, strict=True
    ):
        directory = models.get(key, "")
        item: Message = {"directory": directory, "files": {}}
        for name in names:
            try:
                fd = os.open(
                    Path(directory) / name,
                    os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
                )
            except OSError:
                continue
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= maximum:
                    item["files"][name] = "invalid"
                    continue
                digest = hashlib.sha256()
                left = info.st_size
                while left:
                    chunk = os.read(fd, min(left, 1024 * 1024))
                    if not chunk:
                        break
                    digest.update(chunk)
                    left -= len(chunk)
                item["files"][name] = digest.hexdigest() if not left else "truncated"
            finally:
                os.close(fd)
        result.append(item)
    return result


def sealed_pcm(audio: Any) -> int:
    fd = os.memfd_create("astra-voice-diar-pcm", os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
    try:
        view = memoryview(audio).cast("B")
        while view:
            count = os.write(fd, view)
            if count <= 0:
                raise OSError("diar-pcm-write")
            view = view[count:]
        fcntl.fcntl(fd, F_ADD_SEALS, REQUIRED_SEALS)
        return fd
    except BaseException:
        os.close(fd)
        raise


class DiarProcess:
    def __init__(
        self, identity: Message, cancelled: Callable[[], bool], *, lease_fd: int | None = None
    ) -> None:
        self.identity, self.cancelled = dict(identity), cancelled
        self.serial = 0
        self.reader = ipc.FrameReader(role=ROLE)
        self.pending: list[Message] = []
        self.peak_rss_kb = 0
        self.overall_deadline = float("inf")
        parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        environment = dict(os.environ)
        environment.pop("HOME", None)
        environment.update(
            TMPDIR="/nonexistent/astra-voice-diar",
            OPENBLAS_NUM_THREADS="1",
            OMP_NUM_THREADS="2",
            PYTHONDONTWRITEBYTECODE="1",
        )
        command = worker_command(child.fileno(), role=ROLE)
        command.insert(1, "-B")
        try:
            self.process = subprocess.Popen(
                command,
                # The same open file description keeps flock alive even if the
                # parent is killed while the child is stopped by SIGSTOP.
                pass_fds=(child.fileno(),) + (() if lease_fd is None else (lease_fd,)),
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except BaseException:
            parent.close()
            raise
        finally:
            child.close()
        self.connection = parent
        self.connection.setblocking(False)
        try:
            self.wait("hello", timeout=3)
        except BaseException:
            self.close()
            raise

    def _check(self, deadline: float) -> None:
        if self.cancelled():
            raise DiarCancelled
        if time.monotonic() >= min(deadline, self.overall_deadline):
            raise TimeoutError("diar-deadline")

    def sample_rss(self) -> None:
        try:
            with open(f"/proc/{self.process.pid}/status", encoding="ascii") as stream:
                for line in stream:
                    if line.startswith("VmHWM:"):
                        self.peak_rss_kb = max(self.peak_rss_kb, int(line.split()[1]))
        except OSError:
            pass

    def _send(self, message: Message, fd: int | None = None, *, control: bool = False) -> None:
        wire = memoryview(ipc.encode(message, role=ROLE))
        deadline = time.monotonic() + 1
        while wire:
            if not control:
                self._check(deadline)
            elif time.monotonic() >= deadline:
                raise TimeoutError("diar-send")
            _, writable, _ = select.select([], [self.connection], [], 0.05)
            if not writable:
                continue
            try:
                if fd is not None:
                    count = self.connection.sendmsg(
                        [wire[:1]], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", [fd]))]
                    )
                    fd = None
                else:
                    count = self.connection.send(wire)
            except BlockingIOError:
                continue
            if count <= 0:
                raise OSError("diar-send")
            wire = wire[count:]

    def send(self, kind: str, *, descriptor: int | None = None, **fields: Any) -> Message:
        self.serial += 1
        message = {
            "type": kind,
            "req": f"diar{self.serial}",
            "request_type": kind,
            **self.identity,
            **fields,
        }
        self._send(message, descriptor)
        return message

    def receive(self, deadline: float) -> Message:
        while True:
            self._check(deadline)
            self.sample_rss()
            if self.pending:
                event = self.pending.pop(0)
                if event["type"] == "error":
                    raise ValueError("diar-role-failed")
                if event["type"] != "hello" and any(
                    event.get(k) != v for k, v in self.identity.items()
                ):
                    raise ValueError("diar-identity")
                return event
            readable, _, _ = select.select([self.connection], [], [], 0.05)
            if not readable:
                continue
            data = self.connection.recv(65536)
            if not data:
                raise OSError("diar-exited")
            self.pending.extend(self.reader.feed(data))

    def wait(self, kind: str, *, timeout: float, request: Message | None = None) -> Message:
        deadline = time.monotonic() + timeout
        while True:
            event = self.receive(deadline)
            if event["type"] == "meeting.ack":
                if request is None or event.get("req") != request["req"]:
                    raise ValueError("diar-ack")
                continue
            if event["type"] != kind or (
                request is not None and event.get("req") != request["req"]
            ):
                raise ValueError("diar-order")
            return event

    def load(self, models: Message) -> None:
        profile = DIAR_PROFILES["3dspeaker-campplus"]
        request = self.send(
            "diar.load",
            **models,
            threshold=profile.seed_threshold,
            tau=profile.merge_ceiling,
            threads=2,
        )
        self.wait("diar.loaded", timeout=60, request=request)

    def block(self, audio: Any, index: int, offset_ms: int) -> list[list[Any]]:
        fd = sealed_pcm(audio)
        try:
            request = self.send(
                "diar.block",
                descriptor=fd,
                pcm_fd=fd,
                block_index=index,
                offset_ms=offset_ms,
                block_s=len(audio) / 16000,
                expected_rtf=1.0,
            )
        finally:
            os.close(fd)
        deadline = time.monotonic() + operation_timeout(request)
        assembly = BlockAssembly(self.identity, index)
        while True:
            event = self.receive(deadline)
            if event["type"] == "meeting.ack" and event.get("req") == request["req"]:
                continue
            if event["type"] == "diar.block.done" and event.get("req") != request["req"]:
                raise ValueError("diar-block-reply")
            if assembly.accept(event):
                return assembly.segments

    def finish(self) -> Message:
        request = self.send("diar.finish")
        return self.wait("diar.finished", timeout=operation_timeout(request), request=request)

    def close(self, *, completed: bool = False) -> bool:
        """Cooperative one second, then kill the owned group and reap. Idempotent."""
        self.sample_rss()
        killed = False
        grace = time.monotonic() + 1
        try:
            if self.process.poll() is None and not completed:
                self.serial += 1
                with contextlib.suppress(OSError, ValueError, TimeoutError):
                    self._send(
                        {
                            "type": "diar.cancel",
                            "req": f"diar{self.serial}",
                            "request_type": "diar.cancel",
                            **self.identity,
                        },
                        control=True,
                    )
            try:
                self.process.wait(timeout=max(0, grace - time.monotonic()))
            except subprocess.TimeoutExpired:
                killed = True
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(self.process.pid, signal.SIGKILL)
                # No timeout here: a sent signal is NOT evidence of an exit.
                self.process.wait()
            return self.process.returncode == 0 and not killed
        finally:
            self.connection.close()
