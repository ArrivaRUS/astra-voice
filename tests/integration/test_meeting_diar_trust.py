"""T-431/T1 T-221 real IPC/AS containment. Run outside the socket sandbox.

Synthetic digests are explicit test parameters, never claimed to be signed.
The service's signature/rollback/revocation path is tested separately. No
external network, microphone, user settings, ptrace or key generation here.
"""

from __future__ import annotations

import hashlib
import importlib.util
import os
import re
import socket
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from tools.meeting_diar_check import RoleProcess

from astra_voice.worker import ipc
from astra_voice.worker.meeting_diar import MODEL_FILES
from astra_voice.worker.supervisor import worker_command

pytestmark = pytest.mark.engine
RAW = b"\x08\x01"


def _varint(number: int) -> bytes:
    result = bytearray()
    while number >= 128:
        result.append((number & 127) | 128)
        number >>= 7
    result.append(number)
    return bytes(result)


def _field(number: int, value: bytes | int) -> bytes:
    if isinstance(value, int):
        return _varint(number << 3) + _varint(value)
    return _varint((number << 3) | 2) + _varint(len(value)) + value


def _external_model() -> bytes:
    item = _field(1, b"location") + _field(2, b"../x")
    tensor = _field(1, 1) + _field(2, 1) + _field(13, item) + _field(14, 1)
    return _field(1, 8) + _field(7, _field(5, tensor))


def _huge_model() -> bytes:
    # A tiny graph whose ConstantOfShape output requests 8 GiB of floats.
    # Constant folding/session construction can allocate it; role RLIMIT_AS
    # must contain this even with an explicit matching test digest.
    shape = _field(1, 1) + _field(2, 7) + _field(7, 2**31) + _field(8, b"shape")
    node = _field(1, b"shape") + _field(2, b"huge") + _field(4, b"ConstantOfShape")
    dimension = _field(1, 2**31)
    tensor_type = _field(1, 1) + _field(2, _field(1, dimension))
    output = _field(1, b"huge") + _field(2, _field(1, tensor_type))
    graph = _field(1, node) + _field(2, b"oversize") + _field(5, shape) + _field(12, output)
    return _field(1, 8) + _field(7, graph) + _field(8, _field(2, 13))


def _models(tmp_path: Path, segmentation: bytes = RAW) -> dict[str, Any]:
    result = {}
    for prefix, names, data in zip(
        ("segmentation", "embedder"), MODEL_FILES, (segmentation, RAW), strict=True
    ):
        directory = tmp_path / prefix
        directory.mkdir()
        (directory / names[0]).write_bytes(data)
        result[prefix + "_dir"] = str(directory.resolve())
        result[prefix + "_sha256"] = hashlib.sha256(data).hexdigest()
    return result


def _role_error(role: RoleProcess, timeout: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while True:
        reply = role.receive(max(0, deadline - time.monotonic()))
        assert reply["type"] != "diar.loaded"
        if reply["type"] == "error":
            return reply


@pytest.mark.parametrize("case", ["missing", "mismatch", "external", "extra"])
def test_real_role_refuses_untrusted_before_native(tmp_path: Path, case: str) -> None:
    models = _models(tmp_path, _external_model() if case == "external" else RAW)
    if case == "missing":
        del models["segmentation_sha256"]
    elif case == "mismatch":
        models["embedder_sha256"] = "0" * 64
    elif case == "extra":
        (Path(models["embedder_dir"]) / "extra").write_bytes(b"x")
    with RoleProcess() as role:
        role.send("diar.load", **models, threshold=0.3, tau=0.45, threads=2)
        reply = _role_error(role, 10)
        assert reply["code"] == ("external-data" if case == "external" else "model-untrusted")
        assert not any(event["type"] == "diar.loaded" for event in role.pending)


@contextmanager
def _dictation_worker() -> Iterator[tuple[Any, socket.socket, ipc.FrameReader]]:
    parent, child = socket.socketpair()
    environment = dict(os.environ)
    environment["OPENBLAS_NUM_THREADS"] = "1"
    environment["OMP_NUM_THREADS"] = "1"
    try:
        process = subprocess.Popen(
            worker_command(child.fileno(), role="worker"),
            pass_fds=(child.fileno(),),
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
    try:
        parent.settimeout(10)
        reader = ipc.FrameReader()
        _wait(parent, reader, "hello")
        yield process, parent, reader
    finally:
        parent.close()
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)


def _wait(connection: socket.socket, reader: ipc.FrameReader, kind: str) -> None:
    while True:
        data = connection.recv(65536)
        assert data, "dictation worker exited"
        if any(message["type"] == kind for message in reader.feed(data)):
            return


def test_huge_tensor_limited_to_role_and_dictation_stays_responsive(tmp_path: Path) -> None:
    assert importlib.util.find_spec("sherpa_onnx") is not None, "Требуется sherpa-onnx"
    models = _models(tmp_path, _huge_model())
    with _dictation_worker() as (asr, connection, reader), RoleProcess() as role:
        limits = Path(f"/proc/{role.process.pid}/limits").read_text()
        match = re.search(r"Max address space\s+(\d+)\s+", limits)
        assert match is not None and 0 < int(match[1]) <= 1536 * 1024**2
        connection.sendall(ipc.encode({"type": "ping"}))
        _wait(connection, reader, "pong")
        role.send("diar.load", **models, threshold=0.3, tau=0.45, threads=2)
        # A native allocator can fail by error or terminate only this child.
        try:
            reply = _role_error(role, 60)
            assert reply["code"] in {"model", "memory"}
        except RuntimeError:
            assert role.process.wait(timeout=5) != 0
        assert not any(event["type"] == "diar.loaded" for event in role.pending)
        assert asr.poll() is None
        connection.sendall(ipc.encode({"type": "ping"}))
        _wait(connection, reader, "pong")
        assert asr.poll() is None
        # The test never requests audio capture or recognizer model loading.
