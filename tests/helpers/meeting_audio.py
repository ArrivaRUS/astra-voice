"""Synthetic meeting directories (M2 layout) and a scripted transcribe rig (M4)."""

from __future__ import annotations

import os
import threading
import time
import wave
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt

from astra_voice.meetings.model import Meeting
from astra_voice.meetings.segments import ClockAnchor, SegmentSidecar
from astra_voice.meetings.store import MeetingStore, Writer
from astra_voice.worker import ipc
from astra_voice.worker.engine import CancelToken, TranscribeResult
from astra_voice.worker.meeting_transcribe import MeetingTranscribeState

MID = "2026-11-03_120000-ab12"
STAMP = "2026-11-03T12:00:00+03:00"
RATE = 16000
MONO0 = 5_000_000
Pcm = npt.NDArray[np.int16]
MODEL = {
    "id": "gigaam-v3",
    "revision": "r1",
    "dir": "/not-opened",
    "layout": "onnx-asr-gigaam-v3",
    "variant": "gigaam-v3-e2e-rnnt",
    "threads": 4,
    "min_ram_mb": 700,
}


def silence(seconds: float) -> Pcm:
    return np.zeros(round(seconds * RATE), dtype=np.int16)


def tone(seconds: float, level: float, hz: float = 440.0) -> Pcm:
    """A burst whose peak encodes `level` (0..1): FakeEngine reads it back as text."""
    t = np.arange(round(seconds * RATE)) / RATE
    result: Pcm = np.round(np.sin(2 * np.pi * hz * t) * level * 32767).astype(np.int16)
    return result


def bursts(levels: list[float], *, on: float = 1.0, off: float = 1.0, lead: float = 1.0) -> Pcm:
    parts = [silence(lead)]
    for level in levels:
        parts += [tone(on, level), silence(off)]
    return np.concatenate(parts)


def make_disk(root: Path, *, generation: int = 0) -> MeetingStore:
    disk = MeetingStore(root / "meetings")
    disk.create(Meeting(id=MID, started_at=STAMP, meeting_generation=generation))
    return disk


def write_track(
    disk: MeetingStore,
    track: str,
    pcm: Pcm,
    *,
    segment_s: float = 20.0,
    recorded0: int = 0,
    mono0: int = MONO0,
    ppm: float = 0.0,
    unclosed_tail_s: float | None = None,
    generation: int = 0,
) -> list[str]:
    """Segments + sidecars like meeting_capture: 1 s knots, shared epoch clock.

    ``unclosed_tail_s`` leaves the last segment closed=false with its sidecar
    checkpointed that many seconds before the physical end (recovered tail).
    """
    audio = disk.root / MID / "audio"
    size = round(segment_s * RATE)
    names: list[str] = []
    sample = 0
    chunks = [pcm[i : i + size] for i in range(0, len(pcm), size)]
    for index, chunk in enumerate(chunks):
        last = index == len(chunks) - 1
        stem = f"{track}-{index:04d}"
        path = audio / f"{stem}.wav"
        with wave.open(str(path), "wb") as wav:
            wav.setparams((1, 2, RATE, 0, "NONE", "not compressed"))
            wav.writeframes(chunk.tobytes())
        os.chmod(path, 0o600)
        frames = len(chunk)
        closed = True
        if last and unclosed_tail_s is not None:
            frames = len(chunk) - round(unclosed_tail_s * RATE)
            closed = False

        def mono(s: int) -> int:
            return mono0 + round(s * 1000 / RATE * (1 + ppm / 1e6))

        knots = [[s, mono(s)] for s in range(sample, sample + frames, RATE)]
        knots.append([sample + frames, mono(sample + frames)])
        if len(knots) > 1 and knots[-1][0] == knots[-2][0]:
            knots.pop()
        sidecar = SegmentSidecar(
            meeting_id=MID,
            epoch_id=f"epoch-{track}",
            meeting_generation=generation,
            capture_id="capture1",
            track=track,
            index=index,
            opened_t_mono_ms=mono(sample),
            recorded_ms_at_open=recorded0 + sample * 1000 // RATE,
            closed=closed,
            frames=frames,
            frames_at_checkpoint=frames,
            timeline=knots,
            source_kind="mic_only" if track == "mic" else "app_stream",
            clock="monotonic",
            clock_anchor=ClockAnchor(mono(sample), mono(sample)),
            sample_start=sample,
            sample_end=sample + frames,
            close_reason=("stop" if last else "segment") if closed else None,
        )
        with disk.writer(MID, Writer.CAPTURE) as owner:
            owner.write(f"audio/{stem}.json", sidecar)
        names.append(stem)
        sample += frames
    return names


def fake_vad() -> tuple[str, Callable[[Any, CancelToken], list[float] | None]]:
    def probabilities(audio: Any, cancel: CancelToken) -> list[float] | None:
        if cancel.cancelled:
            return None
        count = (len(audio) + 511) // 512
        padded = np.zeros(count * 512, dtype=np.float64)
        padded[: len(audio)] = audio
        rms = np.sqrt((padded.reshape(count, 512) ** 2).mean(axis=1))
        return [float(v) for v in np.clip(rms / 0.2, 0.0, 1.0)]

    return "test-energy", probabilities


@dataclass(frozen=True)
class Loaded:
    load_ms: float = 1.0
    engine_version: str = "fake-engine 1"
    sessions: int = 3


class FakeEngine:
    """Text = 'речь <peak*100>'; scripted hangs block until the token cancels."""

    def __init__(self, *, hang_calls: set[int] | None = None, load_raises: bool = False) -> None:
        self.hang_calls = hang_calls or set()
        self.load_raises = load_raises
        self.calls = 0
        self.loads = 0
        self.unloads = 0
        self.started = threading.Event()
        self.hang_started: list[float] = []
        self.hang_ended: list[float] = []
        self.on_call: Callable[[int], None] | None = None
        self.text_override: str | None = None

    def load(self, model_dir: Path, layout: str, variant: str, threads: int) -> Loaded:
        self.loads += 1
        self.threads = threads
        if self.load_raises:
            raise RuntimeError("PRIVATE broken model")
        return Loaded()

    def transcribe(self, audio: Any, cancel: CancelToken) -> TranscribeResult:
        call = self.calls
        self.calls += 1
        self.started.set()
        if self.on_call is not None:
            self.on_call(call)
        if call in self.hang_calls:
            self.hang_started.append(time.monotonic())
            cancel.wait(30)
            self.hang_ended.append(time.monotonic())
            return TranscribeResult("", 0.0, True)
        if cancel.cancelled:
            return TranscribeResult("", 0.0, True)
        if self.text_override is not None:
            return TranscribeResult(self.text_override, 1.0, False)
        peak = float(np.abs(audio).max(initial=0.0))
        return TranscribeResult(f"речь {round(peak * 100)}", 1.0, False)

    def unload(self) -> None:
        self.unloads += 1


class Rig:
    """Drive MeetingTranscribeState like run_role does (handle + poll)."""

    def __init__(
        self,
        disk: MeetingStore,
        *,
        engine: Any = None,
        memory_mb: int = 100_000,
        asr_timeout_s: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
        vad: Any = fake_vad,
    ) -> None:
        self.disk = disk
        self.engine: Any = engine or FakeEngine()
        self.memory_mb = memory_mb
        self.state = MeetingTranscribeState(
            store=disk,
            engine_factory=lambda layout: self.engine,
            vad_factory=vad,
            mem_available_mb=lambda: self.memory_mb,
            clock=clock,
            asr_timeout_s=asr_timeout_s,
        )
        self.events: list[dict[str, Any]] = []
        self.counter = 0

    def send(self, kind: str, **fields: Any) -> list[dict[str, Any]]:
        self.counter += 1
        message = {"type": kind, "req": f"r{self.counter}", "request_type": kind, **fields}
        replies = self.state.handle(message)
        for reply in replies:
            ipc.encode(reply, role="meeting-transcribe")
        self.events.extend(replies)
        return replies

    def identity(self, generation: int = 0) -> dict[str, Any]:
        return {"meeting_id": MID, "meeting_generation": generation}

    def load(self) -> None:
        self.send("model.load", **MODEL)
        self.wait(lambda e: e["type"] in {"model.loaded", "error"})

    def start(
        self,
        *,
        tracks: list[str] | None = None,
        resume: bool = False,
        echo: bool = False,
        credits: int = 64,
        limits: dict[str, int] | None = None,
        directory: str | None = None,
        generation: int = 0,
    ) -> list[dict[str, Any]]:
        replies = self.send(
            "meeting.job.start",
            **self.identity(generation),
            dir=directory or str(self.disk.root / MID),
            tracks=tracks or ["mic", "far"],
            variant="app_stream",
            diarization=False,
            self_track="mic",
            resume=resume,
            echo_gate=echo,
            limits=limits or {"max_ms": 14_400_000, "max_utterances": 28800, "max_blocks": 12},
        )
        if credits:
            self.credit(credits, generation)
        return replies

    def credit(self, n: int, generation: int = 0) -> list[dict[str, Any]]:
        return self.send("meeting.job.credit", **self.identity(generation), n=n)

    def pump(self) -> list[dict[str, Any]]:
        fresh = self.state.poll(limit=1_000_000)
        for event in fresh:
            # Everything the role emits must pass the wire schema (run_role encodes it).
            assert len(ipc.encode(event, role="meeting-transcribe")) <= ipc.MAX_FRAME_BYTES + 4
        self.events.extend(fresh)
        return fresh

    def wait(
        self,
        predicate: Callable[[dict[str, Any]], bool],
        timeout: float = 10.0,
        *,
        auto_credit: bool = False,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        start = len(self.events)
        while time.monotonic() < deadline:
            self.pump()
            for event in self.events[start:]:
                if predicate(event):
                    return event
            if auto_credit and self.state.job is not None and self.state.job.credits < 8:
                self.credit(32)
            time.sleep(0.005)
        raise AssertionError(f"timeout; last events: {[e['type'] for e in self.events[-8:]]}")

    def finish(self, timeout: float = 20.0) -> dict[str, Any]:
        job = self.state.job
        outcome = self.wait(
            lambda e: e["type"] in {"meeting.job.done", "meeting.job.failed"},
            timeout,
            auto_credit=True,
        )
        self.settle(job)
        return outcome

    def settle(self, job: Any, timeout: float = 10.0) -> None:
        """Wait for the executor to leave the job (post-done cleanup included)."""
        deadline = time.monotonic() + timeout
        while job is not None and not job.finished:
            if time.monotonic() > deadline:
                raise AssertionError("job did not finish")
            self.pump()
            time.sleep(0.005)

    def utterances(self) -> list[dict[str, Any]]:
        return [e for e in self.events if e["type"] == "meeting.job.utterance"]

    def close(self) -> None:
        self.state.cleanup()
