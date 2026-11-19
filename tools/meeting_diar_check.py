#!/usr/bin/env python3
"""Measure the real hardened meeting-diar subprocess, never an in-process engine.

Read-only WAV/RTTM/model inputs; PCM goes through sealed memfd + SCM_RIGHTS.
The launcher/isolated Python and private role environment match WorkerSupervisor.
RTTM scoring follows spikes/e13/der.py: 10 ms centres, NIST errors including
speech overlap, 0.25 s reference boundary collar, exact Hungarian assignment.
Only time intervals/labels and scalar metrics cross the process boundary.
"""

from __future__ import annotations

import argparse
import array
import fcntl
import json
import math
import os
import select
import socket
import subprocess
import sys
import time
import wave
from collections import Counter
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

if __name__ == "__main__" and (Path(__file__).resolve().parents[1] / "src/astra_voice").is_dir():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from astra_voice.meetings.diar_link import (
    DIAR_PROFILES,
    MAX_BLOCKS,
    MAX_SEGMENTS,
    maximize_assignment,
)
from astra_voice.worker import ipc
from astra_voice.worker.meeting_diar import F_ADD_SEALS, REQUIRED_SEALS, ROLE, BlockAssembly
from astra_voice.worker.supervisor import worker_command

RATE = 16000
DIAR_TRACE_CALLS = (
    "open,openat,openat2,creat,memfd_create,close,dup,dup2,dup3,fcntl,"
    "fork,vfork,clone,clone3,write,writev,pwrite64,pwritev,pwritev2,sendfile,"
    "copy_file_range,splice,tee,ftruncate,fallocate,mmap"
)
IDENTITY = {"meeting_id": "2026-11-10_120000-d1a2", "meeting_generation": 1}
Turn = tuple[float, float, str]


def model_paths(models: Path) -> tuple[Path, Path]:
    """Accept corpus aliases, but pass resolved canonical filenames to the role."""
    result = []
    for name, legacy in (
        ("model.onnx", "segmentation.onnx"),
        ("3dspeaker_speech_campplus_sv_zh_en_16k-common_advanced.onnx", "embedder.onnx"),
    ):
        source = models / name
        if not source.exists():
            source = models / legacy
        result.append(source.resolve(strict=True))
    return result[0], result[1]


def read_rttm(path: Path, start: float, duration: float) -> list[Turn]:
    rows = []
    with path.open(encoding="utf-8") as source:
        for line in source:
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            fields = line.split()
            if len(fields) != 10 or fields[0] != "SPEAKER" or fields[2] != "1":
                raise ValueError("rttm-invalid")
            a, length = float(fields[3]), float(fields[4])
            if not math.isfinite(a + length) or min(a, length) < 0:
                raise ValueError("rttm-invalid")
            b = min(a + length, start + duration) - start
            a = max(a - start, 0.0)
            if b > a:
                rows.append((a, b, fields[7]))
            if len(rows) > MAX_SEGMENTS:
                raise ValueError("rttm-limit")
    return rows


def der(reference: Sequence[Turn], hypothesis: Sequence[Turn], collar: float = 0.25) -> float:
    """10 ms NIST DER percent; exact mapping, overlap included, reference collar."""
    import numpy as np

    end = max((b for _, b, _ in [*reference, *hypothesis]), default=0)
    length = max(0, math.ceil(end / 0.01 - 0.5 - 1e-9))
    excluded = np.zeros(length, dtype=np.bool_)

    def span(a: float, b: float) -> tuple[int, int]:
        return max(0, math.ceil(a / 0.01 - 0.5 - 1e-9)), max(0, math.ceil(b / 0.01 - 0.5 - 1e-9))

    def frames(turns: Sequence[Turn]) -> tuple[Any, list[str]]:
        labels = sorted({k for _, _, k in turns})
        if len(labels) > 64:
            raise ValueError("der-speakers-limit")
        indices = {k: i for i, k in enumerate(labels)}
        result = np.zeros(length, dtype=np.uint64)
        for a, b, k in turns:
            first, last = span(a, b)
            result[first:last] |= np.uint64(1 << indices[k])
        return result, labels

    ref, refs = frames(reference)
    hyp, hyps = frames(hypothesis)
    for a, b, _ in reference:
        for edge in (a, b):
            first, last = span(edge - collar, edge + collar)
            excluded[first : min(last, length)] = True
    states = Counter(zip(ref[~excluded].tolist(), hyp[~excluded].tolist(), strict=True))
    weights = [[0.0] * len(refs) for _ in hyps]
    for (r, h), count in states.items():
        for i in range(len(hyps)):
            if h & (1 << i):
                for j in range(len(refs)):
                    if r & (1 << j):
                        weights[i][j] += count
    mapping = dict(maximize_assignment(weights))
    total = errors = 0
    for (r, h), count in states.items():
        correct = sum(bool(h & (1 << i) and r & (1 << j)) for i, j in mapping.items())
        total += r.bit_count() * count
        errors += (max(r.bit_count(), h.bit_count()) - correct) * count
    if not total:
        return 0.0 if not errors else math.inf
    return 100 * errors / total


def sealed_pcm(audio: Any) -> int:
    descriptor = os.memfd_create("astra-voice-diar-pcm", os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
    try:
        view = memoryview(audio).cast("B")
        while view:
            count = os.write(descriptor, view)
            view = view[count:]
        fcntl.fcntl(
            descriptor,
            F_ADD_SEALS,
            REQUIRED_SEALS,
        )
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


class RoleProcess:
    """Small synchronous CLI driver; the application supervisor owns Qt policy.

    Uses the same worker_command and environment contract, no alternate engine
    path. Optional strace wraps ONLY the role, never the PCM-producing parent.
    """

    def __init__(self, *, trace: Path | None = None) -> None:
        parent, child = socket.socketpair()
        environment = dict(os.environ)
        environment.pop("HOME", None)
        environment["TMPDIR"] = "/nonexistent/astra-voice-diar"
        environment["OPENBLAS_NUM_THREADS"] = "1"
        environment["OMP_NUM_THREADS"] = "2"
        command = worker_command(child.fileno(), role=ROLE)
        command.insert(1, "-B")  # no bytecode writes, including imports before the role
        if trace is not None:
            command = [
                "strace",
                "-f",
                "-qq",
                "-yy",
                "-s",
                "256",
                "-o",
                str(trace),
                "-e",
                f"trace={DIAR_TRACE_CALLS}",
                *command,
            ]
        try:
            self.process = subprocess.Popen(
                command,
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
        self.connection = parent
        self.reader = ipc.FrameReader(role=ROLE)
        self.pending: list[dict[str, Any]] = []
        self.serial = 0
        self.peak_rss_kb = 0
        try:
            self.wait("hello", timeout=3)
        except BaseException:
            self.close()
            raise

    def sample_rss(self) -> None:
        try:
            with open(f"/proc/{self.process.pid}/status", encoding="ascii") as source:
                for line in source:
                    if line.startswith("VmHWM:"):
                        self.peak_rss_kb = max(self.peak_rss_kb, int(line.split()[1]))
        except OSError:
            pass

    def send(self, kind: str, *, descriptor: int | None = None, **fields: Any) -> str:
        self.serial += 1
        req = f"d{self.serial}"
        message = {"type": kind, "request_type": kind, "req": req, **IDENTITY, **fields}
        if kind == "ping":
            message = {k: v for k, v in message.items() if k not in IDENTITY}
        wire = ipc.encode(message, role=ROLE)
        if descriptor is None:
            self.connection.sendall(wire)
        else:
            sent = self.connection.sendmsg(
                [wire[:1]], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", [descriptor]))]
            )
            if sent != 1:
                raise RuntimeError("diar-send-failed")
            self.connection.sendall(wire[1:])
        return req

    def receive(self, timeout: float) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while not self.pending:
            self.sample_rss()
            if time.monotonic() >= deadline:
                raise TimeoutError("diar-response-timeout")
            readable, _, _ = select.select(
                [self.connection], [], [], max(0, min(0.05, deadline - time.monotonic()))
            )
            if not readable:
                continue
            data = self.connection.recv(ipc.MAX_FRAME_BYTES)
            if not data:
                raise RuntimeError(f"diar-exited-{self.process.poll()}")
            self.pending.extend(self.reader.feed(data))
        return self.pending.pop(0)

    def wait(self, kind: str, timeout: float = 900) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while True:
            event = self.receive(max(0, deadline - time.monotonic()))
            if event["type"] == "error":
                raise RuntimeError(f"diar-error-{event['code']}")
            if event["type"] == kind:
                return event

    def load(
        self,
        models: Path,
        threads: int = 2,
        *,
        segmentation_sha256: str | None = None,
        embedder_sha256: str | None = None,
    ) -> None:
        # Development corpus models are symlinks. Resolve them in the driver;
        # the role still rejects every symlink in its OWN supplied directories.
        segmentation, embedder = model_paths(models)
        if segmentation_sha256 is None or embedder_sha256 is None:
            from astra_voice.models.diarization import catalog_diarization_hashes

            hashes = catalog_diarization_hashes()
            if segmentation_sha256 is None:
                segmentation_sha256 = hashes["segmentation_sha256"]
            if embedder_sha256 is None:
                embedder_sha256 = hashes["embedder_sha256"]
        profile = DIAR_PROFILES["3dspeaker-campplus"]
        self.send(
            "diar.load",
            segmentation_dir=str(segmentation.parent),
            segmentation_sha256=segmentation_sha256,
            embedder_sha256=embedder_sha256,
            embedder_dir=str(embedder.parent),
            threshold=profile.seed_threshold,
            tau=profile.merge_ceiling,
            threads=threads,
        )
        self.wait("diar.loaded", 60)

    def block(self, audio: Any, index: int, offset_ms: int) -> list[list[Any]]:
        descriptor = sealed_pcm(audio)
        try:
            self.send(
                "diar.block",
                descriptor=descriptor,
                block_index=index,
                offset_ms=offset_ms,
                pcm_fd=descriptor,
                block_s=len(audio) / RATE,
                expected_rtf=1.0,
            )
        finally:
            os.close(descriptor)
        assembly = BlockAssembly(IDENTITY, index)
        deadline = time.monotonic() + 900
        while True:
            event = self.receive(max(0, deadline - time.monotonic()))
            if event["type"] == "error":
                raise RuntimeError(f"diar-error-{event['code']}")
            if event["type"] in {"diar.block.part", "diar.block.done"} and assembly.accept(event):
                return assembly.segments

    def close(self) -> None:
        self.sample_rss()
        self.connection.close()
        if self.process.poll() is None:
            self.process.terminate()
        try:
            self.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=2)

    def __enter__(self) -> RoleProcess:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


def wav_blocks(path: Path, start: float, duration: float | None, block_s: float) -> Iterator[Any]:
    import numpy as np

    with wave.open(str(path), "rb") as source:
        if (source.getnchannels(), source.getframerate(), source.getsampwidth()) != (1, RATE, 2):
            raise ValueError("wav-must-be-s16le-16khz-mono")
        first = round(start * RATE)
        count = max(0, source.getnframes() - first)
        if duration is not None:
            count = min(count, round(duration * RATE))
        if count <= 0 or math.ceil(count / (block_s * RATE)) > MAX_BLOCKS:
            raise ValueError("wav-duration-limit")
        source.setpos(first)
        while count:
            frames = min(count, round(block_s * RATE))
            data = source.readframes(frames)
            if len(data) != frames * 2:
                raise ValueError("wav-truncated")
            yield np.frombuffer(data, dtype="<i2").astype("<f4") / 32768
            count -= frames


def speaker_breakdown(
    blocks: Sequence[Sequence[list[Any]]], label_map: dict[str, str]
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """Final-label interval sums and provisional births, without voice features.

    speech_s is the sum of interval durations, not elapsed meeting time.
    Birth counts use the labels BEFORE final consolidation, so a later merge
    cannot conceal the block in which an extra provisional identity appeared.
    """
    speakers: dict[str, dict[str, Any]] = {}
    births: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, turns in enumerate(blocks):
        labels = {label for _, _, label in turns}
        new = sorted(labels - seen, key=lambda label: int(label[1:]))
        seen.update(labels)
        births.append(
            {
                "new_global_speakers": len(new),
                "new_labels": new,
                "provisional_speakers_total": len(seen),
            }
        )
        for start, end, label in turns:
            canonical = label_map[label]
            stats = speakers.setdefault(
                canonical, {"speech_ms": 0, "intervals": 0, "first_block": index}
            )
            stats["speech_ms"] += end - start
            stats["intervals"] += 1
    for stats in speakers.values():
        stats["speech_s"] = stats.pop("speech_ms") / 1000
    return dict(sorted(speakers.items(), key=lambda item: int(item[0][1:]))), births


def measure(
    wav: Path,
    models: Path,
    *,
    reference: Path | None = None,
    block_s: float = 1200,
    start: float = 0,
    duration: float | None = None,
    trace: Path | None = None,
    segmentation_sha256: str | None = None,
    embedder_sha256: str | None = None,
) -> dict[str, Any]:
    if not math.isfinite(block_s) or not 0 < block_s <= 1200:
        raise ValueError("block-duration-limit-1200s")
    if (
        not math.isfinite(start)
        or start < 0
        or (duration is not None and (not math.isfinite(duration) or duration <= 0))
    ):
        raise ValueError("wav-interval-invalid")
    started = time.monotonic()
    turns: list[list[Any]] = []
    turn_blocks: list[list[list[Any]]] = []
    blocks = []
    samples = 0
    with RoleProcess(trace=trace) as role:
        role.load(models, segmentation_sha256=segmentation_sha256, embedder_sha256=embedder_sha256)
        for index, audio in enumerate(wav_blocks(wav, start, duration, block_s)):
            block_started = time.monotonic()
            block_turns = role.block(audio, index, round(samples * 1000 / RATE))
            turn_blocks.append(block_turns)
            turns.extend(block_turns)
            if len(turns) > MAX_SEGMENTS:
                raise ValueError("diar-segments-limit")
            samples += len(audio)
            blocks.append(
                {"audio_s": len(audio) / RATE, "wall_s": time.monotonic() - block_started}
            )
        role.send("diar.finish")
        final = role.wait("diar.finished")
        role.sample_rss()
        peak = role.peak_rss_kb
    label_map = final["label_map"]
    labels_seen = {label for _, _, label in turns}
    if (
        set(label_map) != labels_seen
        or not set(label_map.values()) <= labels_seen
        or len(set(label_map.values())) != final["speakers_total"]
        or any(label_map.get(canonical) != canonical for canonical in label_map.values())
        or any(int(canonical[1:]) > int(old[1:]) for old, canonical in label_map.items())
    ):
        raise ValueError("diar-final-map-incomplete")
    hypothesis = [(a / 1000, b / 1000, label_map[k]) for a, b, k in turns]
    speakers, births = speaker_breakdown(turn_blocks, label_map)
    blocks = [{**block, **birth} for block, birth in zip(blocks, births, strict=True)]
    result = {
        "speakers_total": final["speakers_total"],
        "speaker_stats": speakers,
        "segments": len(turns),
        "audio_s": samples / RATE,
        "wall_s": time.monotonic() - started,
        "peak_rss_kb": peak,
        "blocks": blocks,
        "der_percent": None,
    }
    if reference is not None:
        result["der_percent"] = der(read_rttm(reference, start, samples / RATE), hypothesis)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wav", type=Path)
    parser.add_argument("--models", required=True, type=Path)
    parser.add_argument("--reference", type=Path)
    parser.add_argument("--segmentation-sha256")
    parser.add_argument("--embedder-sha256")
    parser.add_argument("--block-s", type=float, default=1200)
    parser.add_argument("--start", type=float, default=0)
    parser.add_argument("--duration", type=float)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--trace", type=Path)
    args = parser.parse_args(argv)
    if args.start < 0 or (args.duration is not None and args.duration <= 0):
        parser.error("invalid audio interval")
    try:
        result = measure(
            args.wav,
            args.models,
            reference=args.reference,
            block_s=args.block_s,
            start=args.start,
            duration=args.duration,
            trace=args.trace,
            segmentation_sha256=args.segmentation_sha256,
            embedder_sha256=args.embedder_sha256,
        )
    except (OSError, ValueError, RuntimeError, TimeoutError) as error:
        print(str(error), file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result, allow_nan=False, sort_keys=True))
    else:
        print(
            f"Speakers: {result['speakers_total']}; DER: {result['der_percent']}; "
            f"wall: {result['wall_s']:.2f} s; peak RSS: {result['peak_rss_kb']} KiB"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
