#!/usr/bin/env python3
"""Run a complete meeting job with diarization, from a meeting directory or WAVs.

Uses the production transcriber and real hardened child role. --fake-asr replaces
only speech recognition with fixed text, useful for isolated diar corpus checks.
No transcript text is printed. Output folders are private; defaults live beneath
.artifacts/e13-m7/tmp. This does not install models or alter user settings.
"""

from __future__ import annotations

import argparse
import json
import tempfile
import time
import wave
from pathlib import Path
from typing import Any

if __name__ == "__main__":
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from astra_voice.meetings.model import Meeting, Transcript
from astra_voice.meetings.segments import ClockAnchor, SegmentSidecar
from astra_voice.meetings.store import MeetingStore, Writer
from astra_voice.worker.engine import CancelToken, LoadResult, TranscribeResult
from astra_voice.worker.meeting_transcribe import MeetingTranscribeState, energy_probabilities

RATE = 16000
MID = "2026-11-10_120000-d1a2"


class CheckEngine:
    """Deterministic ASR substitute; no dependency on a speech model/runtime."""

    def load(self, *args: Any) -> LoadResult:
        return LoadResult(0.0, "diar-check-fake-asr", 0)

    def unload(self) -> None:
        pass

    def transcribe(self, audio: Any, cancel: CancelToken) -> TranscribeResult:
        return TranscribeResult("[проверка разделения]", 0.0, cancel.cancelled)


def prepare_meeting(
    root: Path, wavs: dict[str, Path], *, duration_s: float | None = None
) -> tuple[MeetingStore, str]:
    """Copy s16le mono 16 kHz WAV tracks into the production M2 segmented layout."""
    store = MeetingStore(root / "meetings")
    frames_by_track = {}
    for track, path in wavs.items():
        with wave.open(str(path), "rb") as source:
            if (source.getnchannels(), source.getsampwidth(), source.getframerate()) != (
                1,
                2,
                RATE,
            ):
                raise ValueError("wav-must-be-mono-s16le-16khz")
            frames = source.getnframes()
            if duration_s is not None:
                frames = min(frames, round(duration_s * RATE))
            if not 0 < frames <= 14_400 * RATE:
                raise ValueError("wav-duration")
            frames_by_track[track] = frames
    duration = max(frames_by_track.values()) * 1000 // RATE
    meeting = Meeting(
        id=MID,
        started_at="2026-11-10T12:00:00+00:00",
        variant="app_stream" if "far" in wavs else "mic_only",
        recorded_ms=duration,
        limit_ms=next(v for v in (7_200_000, 10_800_000, 14_400_000) if duration <= v),
    )
    store.create(meeting)
    for track, path in wavs.items():
        sample = index = 0
        frames = frames_by_track[track]
        with wave.open(str(path), "rb") as source:
            while sample < frames:
                count = min(300 * RATE, frames - sample)
                raw = source.readframes(count)
                if len(raw) != count * 2:
                    raise ValueError("wav-truncated")
                stem = f"{track}-{index:04d}"
                target = store.root / MID / "audio" / f"{stem}.wav"
                with wave.open(str(target), "wb") as dest:
                    dest.setparams((1, 2, RATE, 0, "NONE", "not compressed"))
                    dest.writeframes(raw)
                target.chmod(0o600)

                def mono(s: int) -> int:
                    return 1_000_000 + s // 16

                knots = [[s, mono(s)] for s in range(sample, sample + count, RATE)]
                knots.append([sample + count, mono(sample + count)])
                sidecar = SegmentSidecar(
                    meeting_id=MID,
                    epoch_id=f"check-{track}",
                    capture_id="diar-check",
                    track=track,
                    index=index,
                    opened_t_mono_ms=mono(sample),
                    recorded_ms_at_open=sample // 16,
                    frames=count,
                    frames_at_checkpoint=count,
                    closed=True,
                    timeline=knots,
                    source_kind="app_stream" if track == "far" else "mic_only",
                    clock="monotonic",
                    clock_anchor=ClockAnchor(mono(sample), mono(sample)),
                    sample_start=sample,
                    sample_end=sample + count,
                    close_reason="stop" if sample + count == frames else "segment",
                )
                with store.writer(MID, Writer.CAPTURE) as owner:
                    owner.write(f"audio/{stem}.json", sidecar)
                sample += count
                index += 1
    return store, MID


def run_job(
    store: MeetingStore,
    meeting_id: str,
    models: dict[str, str],
    *,
    asr_model: Path | None = None,
    fake_asr: bool = False,
    resume: bool = False,
) -> dict[str, Any]:
    from astra_voice.worker.engine import make_engine

    models = dict(models)
    fields = ("segmentation_sha256", "embedder_sha256")
    if any(field not in models for field in fields):
        from astra_voice.models.diarization import catalog_diarization_hashes

        for field, value in catalog_diarization_hashes().items():
            models.setdefault(field, value)
    meeting = store.read(meeting_id, "meeting.json")
    assert isinstance(meeting, Meeting)
    tracks = [
        track
        for track in ("mic", "far")
        if any((store.root / meeting_id / "audio").glob(f"{track}-*.wav"))
    ]
    if not tracks:
        raise ValueError("meeting-has-no-audio")
    options: dict[str, Any] = {}
    if fake_asr:
        options["vad_factory"] = lambda: ("energy-rms", energy_probabilities)
    state = MeetingTranscribeState(
        store=store,
        engine_factory=(lambda layout: CheckEngine()) if fake_asr else make_engine,
        **options,
    )
    serial = 0
    identity = {"meeting_id": meeting_id, "meeting_generation": meeting.meeting_generation}
    phases: dict[str, float] = {}
    phase, phase_started = "load", time.monotonic()
    blocks = 0

    def send(kind: str, **fields: Any) -> list[dict[str, Any]]:
        nonlocal serial
        serial += 1
        return state.handle({"type": kind, "req": f"check{serial}", "request_type": kind, **fields})

    started = time.monotonic()
    try:
        send(
            "model.load",
            id="gigaam-v3",
            revision="check",
            dir=str(asr_model or "/fake-asr"),
            layout="onnx-asr-gigaam-v3",
            variant="gigaam-v3-e2e-rnnt",
            threads=2,
            min_ram_mb=0 if fake_asr else 700,
        )
        load_until = time.monotonic() + 90
        loaded = False
        while not loaded:
            if time.monotonic() >= load_until:
                raise TimeoutError("asr-load")
            for event in state.poll(128):
                if event["type"] == "error":
                    raise ValueError("asr-load-" + str(event.get("code")))
                loaded |= event["type"] == "model.loaded"
            time.sleep(0.01)
        send(
            "meeting.job.start",
            **identity,
            dir=str(store.root / meeting_id),
            tracks=tracks,
            variant=meeting.variant,
            diarization=True,
            diar_models=models,
            self_track="mic" if "far" in tracks else "none",
            resume=resume,
            echo_gate=False,
            limits={"max_ms": 14_400_000, "max_utterances": 9000, "max_blocks": 12},
        )
        send("meeting.job.credit", **identity, n=64)
        until = time.monotonic() + 2 * max(meeting.recorded_ms / 1000, 60) + 900
        done = None
        accepted = False
        while done is None:
            if time.monotonic() > until:
                raise TimeoutError("job")
            for event in state.poll(128):
                kind = event["type"]
                if kind == "meeting.job.accepted":
                    accepted = True
                elif kind == "meeting.job.waiting" and not accepted:
                    raise ValueError("waiting-memory")
                elif kind == "meeting.job.progress":
                    now = time.monotonic()
                    current = str(event["stage"])
                    if current != phase:
                        phases[phase] = phases.get(phase, 0) + now - phase_started
                        phase, phase_started = current, now
                    blocks = max(blocks, int(event.get("blocks", 0)))
                elif kind == "meeting.job.utterance":
                    send("meeting.job.credit", **identity, n=1)
                elif kind == "meeting.job.failed":
                    raise ValueError("job-" + str(event.get("code")))
                elif kind == "meeting.job.done":
                    done = event
            time.sleep(0.01)
        phases[phase] = phases.get(phase, 0) + time.monotonic() - phase_started
        transcript = store.read(meeting_id, "transcript.json")
        assert isinstance(transcript, Transcript)
        return {
            "utterances": len(transcript.utterances),
            "speakers": len(
                {u.speaker for u in transcript.utterances if u.speaker.startswith("S")}
            ),
            "diar_status": done["diar_status"],
            "labels_final": transcript.labels_final,
            "blocks": blocks,
            "wall_s": time.monotonic() - started,
            "phase_s": {k: round(v, 4) for k, v in phases.items()},
            "transcriber_peak_rss_kb": done["peak_rss_kb"],
            "diar_peak_rss_kb": state.diar_peak_rss_kb,
            "fake_asr": fake_asr,
        }
    finally:
        state.cleanup()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--meeting", type=Path)
    parser.add_argument("--far", type=Path)
    parser.add_argument("--mic", type=Path)
    parser.add_argument("--segmentation-dir", type=Path, required=True)
    parser.add_argument("--embedder-dir", type=Path, required=True)
    parser.add_argument("--segmentation-sha256")
    parser.add_argument("--embedder-sha256")
    parser.add_argument("--asr-model-dir", type=Path)
    parser.add_argument("--fake-asr", action="store_true")
    parser.add_argument("--duration-s", type=float)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    if bool(args.meeting) == bool(args.far or args.mic):
        parser.error("provide --meeting OR --far/--mic")
    if not args.fake_asr and args.asr_model_dir is None:
        parser.error("provide --asr-model-dir or --fake-asr")
    if args.duration_s is not None and not 0 < args.duration_s <= 14400:
        parser.error("invalid --duration-s")
    if args.meeting:
        store, mid = MeetingStore(args.meeting.absolute().parent), args.meeting.name
    else:
        root = args.out
        if root is None:
            base = Path(__file__).resolve().parents[1] / ".artifacts/e13-m7/tmp"
            base.mkdir(mode=0o700, parents=True, exist_ok=True)
            root = Path(tempfile.mkdtemp(prefix="transcribe-diar-", dir=base))
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        store, mid = prepare_meeting(
            root,
            {k: v for k, v in {"mic": args.mic, "far": args.far}.items() if v},
            duration_s=args.duration_s,
        )
    models = {
        "segmentation_dir": str(args.segmentation_dir.absolute()),
        "embedder_dir": str(args.embedder_dir.absolute()),
    }
    for field in ("segmentation_sha256", "embedder_sha256"):
        if (value := getattr(args, field)) is not None:
            models[field] = value
    result = run_job(
        store, mid, models, asr_model=args.asr_model_dir, fake_asr=args.fake_asr, resume=args.resume
    )
    print(
        json.dumps(result, sort_keys=True)
        if args.json
        else f"Реплик: {result['utterances']}; дикторов: {result['speakers']}; "
        f"diar_status={result['diar_status']}; время: {result['wall_s']:.2f} с; "
        f"RSS transcribe/diar: {result['transcriber_peak_rss_kb']}/"
        f"{result['diar_peak_rss_kb']} КиБ; фазы: {result['phase_s']}"
    )
    return 0 if result["diar_status"] == "done" else 1


if __name__ == "__main__":
    raise SystemExit(main())
