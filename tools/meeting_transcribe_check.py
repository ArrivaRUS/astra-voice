"""``tools/validate meetings --transcribe``: the real transcribe role over the SP4 corpus.

The corpus (``mic.wav``, ``far.wav``, ``transcript.json``) is only read. A
meeting directory in the M2 layout (300 s segments + sidecars) is built under
``--out`` (a temporary directory by default, removed afterwards); the role
``meeting-transcribe`` runs as in production: launcher + ``WorkerSupervisor``,
private environment, model.load → meeting.job.start → credits 64/32.

Only numbers are reported (WER per track, timecode deltas, wall/RTF, peak RSS);
recognized and reference texts never reach stdout/stderr.

Gate (DoD E13.M4): timecodes p95 ≤ 1 s and max ≤ 2 s. WER (no dictation WER
on this TTS corpus is known) and speed (the laptop is the reference, not the
VPS) are reported as guides.
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import sys
import tempfile
import time
import uuid
import wave
from bisect import bisect_left
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

RATE = 16000
SEGMENT_S = 300  # capture rotates segments every 5 min (sidecar: <= 302 knots)
MEETING_ID = "2026-11-03_120000-e13v"
STARTED_AT = "2026-11-03T12:00:00+03:00"
MONO0 = 5_000_000
TRACK_FILES = {"mic": "mic.wav", "far": "far.wav"}
CREDITS_INITIAL = 64  # core/meeting_jobs.INITIAL_CREDITS
CREDITS_STEP = 32  # core/meeting_jobs.CREDIT_STEP
TIMECODE_P95_MS = 1000
TIMECODE_MAX_MS = 2000
HOUR_WALL_MS = 240_000  # 1 h of sound <= 4 min wall (laptop; guide on the VPS)
REFERENCE_TAIL_MS = 1000  # a reference reply needs >= 1 s of sound before the cut
SEPARABLE_GAP_MS = 1000  # silence on the track before a reply that VAD (pause 0.5 s) must split
PLACEHOLDERS = frozenset({"[не распознано]", "[кусок записи повреждён]"})
DEFAULT_MODEL_DIR = "~/.cache/astra-voice-spike/gigaam-v3/e2e_rnnt"
MODEL = {
    "id": "gigaam-v3",
    "revision": "validate",
    "layout": "onnx-asr-gigaam-v3",
    "variant": "gigaam-v3-e2e-rnnt",
    "min_ram_mb": 768,
}
_WORD = re.compile(r"\w+")


class Unavailable(RuntimeError):
    """The run cannot happen (corpus, model, role start); fixed code only."""


# ---- metrics (pure) ---------------------------------------------------------


def normalize_words(text: str) -> list[str]:
    """Lower case, ё→е, punctuation dropped; words are runs of letters/digits."""
    return _WORD.findall(text.casefold().replace("ё", "е"))


def word_errors(reference: Sequence[str], hypothesis: Sequence[str]) -> int:
    """Levenshtein distance over words (substitutions + deletions + insertions).

    Row by row with numpy: insertions are a running minimum of ``row - j``.
    """
    if not reference or not hypothesis:
        return max(len(reference), len(hypothesis))
    vocabulary: dict[str, int] = {}
    ref = [vocabulary.setdefault(word, len(vocabulary)) for word in reference]
    hyp = np.array([vocabulary.setdefault(word, len(vocabulary)) for word in hypothesis])
    steps = np.arange(len(hyp) + 1, dtype=np.int64)
    row = steps.copy()
    candidate = np.empty_like(row)
    for index, token in enumerate(ref, 1):
        candidate[0] = index
        np.minimum(row[1:] + 1, row[:-1] + (hyp != token), out=candidate[1:])
        row = np.minimum.accumulate(candidate - steps) + steps
    return int(row[-1])


def wer(reference: Sequence[str], hypothesis: Sequence[str]) -> float | None:
    """Word error rate in percent; ``None`` without reference words."""
    if not reference:
        return None
    return round(100.0 * word_errors(reference, hypothesis) / len(reference), 2)


def percentile(values: Sequence[float], q: float) -> float | None:
    """Nearest-rank percentile (no interpolation): p95 is an observed value."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(q / 100 * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]


def nearest_deltas(reference: Sequence[int], ours: Sequence[int]) -> list[int]:
    """|Δ| from every reference start to the nearest start of ours (same track)."""
    if not ours:
        return []
    starts = sorted(ours)
    result: list[int] = []
    for value in reference:
        index = bisect_left(starts, value)
        near = [starts[i] for i in (index - 1, index) if 0 <= i < len(starts)]
        result.append(min(abs(value - other) for other in near))
    return result


def summary(deltas: Sequence[int]) -> dict[str, Any]:
    return {
        "n": len(deltas),
        "p50": percentile(deltas, 50),
        "p95": percentile(deltas, 95),
        "max": max(deltas) if deltas else None,
    }


@dataclass
class Reference:
    """Reference of one cut of the corpus, per track (mic = owner*, far = far*)."""

    words: dict[str, list[str]] = field(default_factory=lambda: {"mic": [], "far": []})
    starts: dict[str, list[int]] = field(default_factory=lambda: {"mic": [], "far": []})
    onsets: dict[str, list[int]] = field(default_factory=lambda: {"mic": [], "far": []})
    separable: dict[str, list[int]] = field(default_factory=lambda: {"mic": [], "far": []})


def track_of(speaker: str) -> str:
    return "mic" if speaker.startswith("owner") else "far"


def load_reference(document: dict[str, Any], audio_ms: int) -> Reference:
    """Words whose middle is before the cut; replies with >= 1 s of sound before it.

    ``onsets`` (reply and sentence starts) serve the diagnostic reverse check;
    ``separable`` are reply starts after >= 1 s of silence on the same track
    (turns closer than the VAD pause merge into one reply of ours).
    """
    reference = Reference()
    words = sorted(document["words"], key=lambda w: (w["start"], w["end"]))
    for word in words:
        if (float(word["start"]) + float(word["end"])) * 500 < audio_ms:
            reference.words[track_of(str(word["speaker"]))].extend(
                normalize_words(str(word["word"]))
            )
    ends = {"mic": -SEPARABLE_GAP_MS, "far": -SEPARABLE_GAP_MS}
    for utterance in sorted(document["utterances"], key=lambda u: (u["start"], u["end"])):
        track = track_of(str(utterance["speaker"]))
        start = round(float(utterance["start"]) * 1000)
        if start + REFERENCE_TAIL_MS <= audio_ms:
            reference.starts[track].append(start)
            if start - ends[track] >= SEPARABLE_GAP_MS:
                reference.separable[track].append(start)
        ends[track] = max(ends[track], round(float(utterance["end"]) * 1000))
        for sentence in utterance.get("sentences", []):
            onset = round(float(sentence["start"]) * 1000)
            if onset < audio_ms:
                reference.onsets[track].append(onset)
        if start < audio_ms:
            reference.onsets[track].append(start)
    for track in reference.starts:
        reference.starts[track].sort()
        reference.onsets[track] = sorted(set(reference.onsets[track]))
    return reference


def evaluate(
    reference: Reference,
    utterances: Iterable[dict[str, Any]],
    *,
    audio_ms: int,
    wall_ms: int,
    done: dict[str, Any] | None,
) -> dict[str, Any]:
    """Metrics and verdicts from the role's utterances (text is consumed here only)."""
    rows = sorted(utterances, key=lambda u: (int(u["start_ms"]), int(u.get("seq", 0))))
    spoken = [u for u in rows if str(u.get("text", "")).strip() not in PLACEHOLDERS]
    report: dict[str, Any] = {"wer": {}, "timecodes_by_track": {}, "reverse_by_track": {}}
    deltas: list[int] = []
    reverse: list[int] = []
    separable: list[int] = []
    missing = 0
    for track in ("mic", "far"):
        mine = [u for u in spoken if u["track"] == track]
        hypothesis: list[str] = []
        for item in mine:
            hypothesis.extend(normalize_words(str(item["text"])))
        words = reference.words[track]
        report["wer"][track] = {
            "percent": wer(words, hypothesis),
            "reference_words": len(words),
            "hypothesis_words": len(hypothesis),
        }
        starts = [int(u["start_ms"]) for u in mine]
        found = nearest_deltas(reference.starts[track], starts)
        if reference.starts[track] and not found:
            missing += len(reference.starts[track])
        deltas.extend(found)
        report["timecodes_by_track"][track] = summary(found)
        separable.extend(nearest_deltas(reference.separable[track], starts))
        back = nearest_deltas(starts, reference.onsets[track])
        reverse.extend(back)
        report["reverse_by_track"][track] = summary(back)
    report["timecodes"] = summary(deltas)
    report["timecodes"]["missing"] = missing
    # Diagnostic: our start → nearest reference reply/sentence onset (merges of
    # turns separated by < 0.5 s show up in "timecodes", not here).
    report["timecodes_reverse"] = summary(reverse)
    report["timecodes_separable"] = summary(separable)
    rtf = wall_ms / audio_ms if audio_ms > 0 else None
    report.update(
        {
            "audio_ms": audio_ms,
            "wall_ms": wall_ms,
            "role_wall_ms": int(done["wall_ms"]) if done else None,
            "rtf": round(rtf, 4) if rtf is not None else None,
            "hour_wall_ms": round(rtf * 3_600_000) if rtf is not None else None,
            "peak_rss_kb": int(done["peak_rss_kb"]) if done else None,
            "utterances": len(rows),
            "unrecognized": sum(1 for u in rows if u.get("text") == "[не распознано]"),
            "damaged": sum(1 for u in rows if u.get("text") == "[кусок записи повреждён]"),
            "echo_dropped": int(done["echo_dropped"]) if done else None,
        }
    )
    stats = report["timecodes"]
    timecodes_ok = (
        bool(deltas)
        and not missing
        and stats["p95"] is not None
        and stats["p95"] <= TIMECODE_P95_MS
        and stats["max"] <= TIMECODE_MAX_MS
    )
    speed_ok = rtf is not None and rtf * 3_600_000 <= HOUR_WALL_MS
    report["verdicts"] = {
        "job": "pass" if done is not None else "fail",
        "timecodes": "pass" if timecodes_ok else "fail",
        "speed": ("pass" if speed_ok else "fail") + " (ориентир: эталон — ноутбук)",
        "wer": "ориентир: WER диктовки на этом TTS-корпусе неизвестен",
    }
    passed = done is not None and timecodes_ok
    report["verdict"] = "pass" if passed else "fail"
    report["exit_code"] = 0 if passed else 1
    return report


# ---- meeting directory ------------------------------------------------------


def private_environment(root: Path) -> dict[str, str]:
    """Explicit role environment: every XDG location lives under ``root``."""
    locations = {
        "HOME": "home",
        "XDG_RUNTIME_DIR": "runtime",
        "XDG_CONFIG_HOME": "config",
        "XDG_CACHE_HOME": "cache",
        "XDG_STATE_HOME": "state",
        "XDG_DATA_HOME": "data",
        "TMPDIR": "tmp",
    }
    env = {key: str(root / name) for key, name in locations.items()}
    for value in env.values():
        Path(value).mkdir(mode=0o700, exist_ok=True)
    env.update(
        {
            "PATH": "/usr/bin:/bin",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "QT_QPA_PLATFORM": "offscreen",
            "PYTHONDONTWRITEBYTECODE": "1",
            "DBUS_SESSION_BUS_ADDRESS": f"unix:path={root / 'runtime'}/absent-session-bus",
        }
    )
    return env


def _check_wav(path: Path) -> int:
    try:
        with wave.open(str(path), "rb") as wav:
            if (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) != (RATE, 1, 2):
                raise Unavailable("corpus-format")
            return wav.getnframes()
    except (OSError, EOFError, wave.Error) as exc:
        raise Unavailable("corpus-unreadable") from exc


def build_meeting(corpus: Path, data_home: Path, minutes: float | None) -> int:
    """Meeting directory of the M2 layout from the corpus; returns recorded ms."""
    from astra_voice.core.paths import APP_NAME
    from astra_voice.meetings.model import Meeting
    from astra_voice.meetings.segments import ClockAnchor, SegmentSidecar
    from astra_voice.meetings.store import MeetingStore, Writer

    frames = min(_check_wav(corpus / name) for name in TRACK_FILES.values())
    if minutes is not None:
        frames = min(frames, round(minutes * 60 * RATE))
    if frames <= 0:
        raise Unavailable("corpus-empty")
    audio_ms = frames * 1000 // RATE
    limit = next((v for v in (7_200_000, 10_800_000, 14_400_000) if audio_ms <= v), None)
    if limit is None:
        raise Unavailable("corpus-too-long")
    (data_home / APP_NAME).mkdir(mode=0o700, exist_ok=True)
    store = MeetingStore(data_home / APP_NAME / "meetings")
    store.create(
        Meeting(
            id=MEETING_ID,
            started_at=STARTED_AT,
            variant="app_stream",
            recorded_ms=audio_ms,
            limit_ms=limit,
        )
    )
    audio = store.root / MEETING_ID / "audio"
    size = SEGMENT_S * RATE
    for track, name in TRACK_FILES.items():
        with wave.open(str(corpus / name), "rb") as source:
            sample = index = 0
            while sample < frames:
                count = min(size, frames - sample)
                chunk = source.readframes(count)
                stem = f"{track}-{index:04d}"
                target = audio / f"{stem}.wav"
                with wave.open(str(target), "wb") as wav:
                    wav.setparams((1, 2, RATE, 0, "NONE", "not compressed"))
                    wav.writeframes(chunk)
                os.chmod(target, 0o600)
                last = sample + count >= frames

                def mono(s: int) -> int:
                    return MONO0 + s * 1000 // RATE

                knots = [[s, mono(s)] for s in range(sample, sample + count, RATE)]
                if knots[-1][0] != sample + count:
                    knots.append([sample + count, mono(sample + count)])
                sidecar = SegmentSidecar(
                    meeting_id=MEETING_ID,
                    epoch_id=f"epoch-{track}",
                    capture_id="validate",
                    track=track,
                    index=index,
                    opened_t_mono_ms=mono(sample),
                    recorded_ms_at_open=sample * 1000 // RATE,
                    closed=True,
                    frames=count,
                    frames_at_checkpoint=count,
                    timeline=knots,
                    source_kind="mic_only" if track == "mic" else "app_stream",
                    clock="monotonic",
                    clock_anchor=ClockAnchor(mono(sample), mono(sample)),
                    sample_start=sample,
                    sample_end=sample + count,
                    close_reason="stop" if last else "segment",
                )
                with store.writer(MEETING_ID, Writer.CAPTURE) as owner:
                    owner.write(f"audio/{stem}.json", sidecar)
                sample += count
                index += 1
    return audio_ms


# ---- role -------------------------------------------------------------------


@dataclass
class RoleRun:
    utterances: list[dict[str, Any]] = field(default_factory=list)
    done: dict[str, Any] | None = None
    failure: str | None = None
    load_ms: float | None = None
    wall_ms: int = 0
    progress_events: int = 0
    credits: int = 0


def run_role(
    env: dict[str, str],
    directory: Path,
    load: dict[str, Any],
    *,
    echo_gate: bool,
    audio_ms: int,
    progress: Callable[[str, int, int], None] = lambda stage, done, total: None,
) -> RoleRun:
    """model.load → meeting.job.start → credits, like core/meeting_jobs does."""
    from astra_voice.worker.supervisor import WorkerSupervisor, worker_command

    events: list[dict[str, Any]] = []
    supervisor = WorkerSupervisor(
        events.append,
        role="meeting-transcribe",
        use_qt=False,
        auto_restart=False,
        command_factory=lambda fd: [
            "/usr/bin/env",
            "-i",
            *(f"{key}={value}" for key, value in sorted(env.items())),
            *worker_command(fd, role="meeting-transcribe"),
        ],
    )
    identity = {"meeting_id": MEETING_ID, "meeting_generation": 0}
    run = RoleRun()

    def send(kind: str, **fields: Any) -> str:
        req = uuid.uuid4().hex
        supervisor.send({"type": kind, "req": req, "request_type": kind, **fields})
        return req

    def wait(predicate: Callable[[dict[str, Any]], bool], timeout: float) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        seen = 0
        while time.monotonic() < deadline:
            supervisor.pump(0.05)
            for event in events[seen:]:
                if predicate(event):
                    return event
            seen = len(events)
            if supervisor.state == "stopped" or (
                supervisor.process is not None and supervisor.process.poll() is not None
            ):
                raise Unavailable("role-exited")
        raise Unavailable("role-timeout")

    try:
        supervisor.set_meeting_generation(MEETING_ID, 0)
        supervisor.start()
        deadline = time.monotonic() + 10
        while not supervisor.ready and time.monotonic() < deadline:
            supervisor.pump(0.05)
        if not supervisor.ready:
            raise Unavailable("role-start")
        req = send("model.load", **load)
        loaded = wait(lambda e: e.get("req") == req and e["type"] in {"model.loaded", "error"}, 90)
        if loaded["type"] != "model.loaded":
            raise Unavailable("model-load-" + str(loaded.get("code", "error")))
        run.load_ms = float(loaded.get("load_ms", 0.0))
        started = time.monotonic()
        req = send(
            "meeting.job.start",
            **identity,
            dir=str(directory),
            tracks=["mic", "far"],
            variant="app_stream",
            diarization=False,
            self_track="mic",
            resume=False,
            echo_gate=echo_gate,
            limits={"max_ms": 14_400_000, "max_utterances": 28_800, "max_blocks": 12},
        )
        send("meeting.job.credit", **identity, n=CREDITS_INITIAL)
        run.credits = CREDITS_INITIAL
        deadline = started + 2 * audio_ms / 1000 + 900
        seen = 0
        while run.done is None and run.failure is None:
            if time.monotonic() > deadline:
                run.failure = "timeout"
                break
            supervisor.pump(0.05)
            if supervisor.process is not None and supervisor.process.poll() is not None:
                run.failure = "role-exited"
                break
            for event in events[seen:]:
                kind = event.get("type")
                if event.get("req") == req and kind == "error":
                    run.failure = "start-" + str(event.get("code", "error"))
                elif kind == "meeting.job.progress":
                    run.progress_events += 1
                    progress(
                        str(event.get("stage", "")),
                        int(event.get("done_ms", 0)),
                        int(event.get("total_ms", 0)),
                    )
                elif kind == "meeting.job.utterance":
                    run.utterances.append(
                        {k: event[k] for k in ("seq", "track", "start_ms", "end_ms", "text")}
                    )
                    if len(run.utterances) % CREDITS_STEP == 0:
                        send("meeting.job.credit", **identity, n=CREDITS_STEP)
                        run.credits += CREDITS_STEP
                elif kind == "meeting.job.done":
                    run.done = dict(event)
                elif kind == "meeting.job.failed":
                    run.failure = "job-" + str(event.get("code", "io"))
                elif kind == "meeting.job.waiting":
                    run.failure = "waiting-memory"
                elif kind == "error" and "req" not in event:
                    run.failure = "role-" + str(event.get("code", "error"))
            seen = len(events)
        run.wall_ms = round((time.monotonic() - started) * 1000)
    finally:
        supervisor.stop()
    return run


# ---- command ----------------------------------------------------------------


def _prepare_out(out: Path | None) -> tuple[Path, bool]:
    if out is None:
        return Path(tempfile.mkdtemp(prefix="astra-voice-meetings-")), True
    out = out.absolute()
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise Unavailable("out-not-empty")
    out.mkdir(mode=0o700, parents=True, exist_ok=True)
    out.chmod(0o700)
    return out, False


def run_transcribe(
    corpus: Path,
    *,
    minutes: float | None = None,
    model_dir: Path | None = None,
    threads: int = 2,
    echo_gate: bool = False,
    out: Path | None = None,
    progress: Callable[[str, int, int], None] = lambda stage, done, total: None,
) -> tuple[int, dict[str, Any]]:
    """Exit code 0 pass / 1 fail / 2 unavailable and a text-free report."""
    report: dict[str, Any] = {
        "corpus": str(corpus),
        "minutes": minutes,
        "threads": threads,
        "echo_gate": echo_gate,
        "model": {"layout": MODEL["layout"], "variant": MODEL["variant"]},
        "thresholds": {
            "timecodes_p95_ms": TIMECODE_P95_MS,
            "timecodes_max_ms": TIMECODE_MAX_MS,
            "hour_wall_ms_guide": HOUR_WALL_MS,
        },
        "verdict": "unavailable",
        "exit_code": 2,
    }
    root: Path | None = None
    temporary = False
    from astra_voice.meetings import MeetingValidationError
    from astra_voice.meetings.store import MeetingStoreError

    try:
        from astra_voice.worker.engine import LAYOUTS

        directory = Path(
            model_dir or os.environ.get("ASTRA_VOICE_TEST_MODEL_DIR") or DEFAULT_MODEL_DIR
        ).expanduser()
        required = LAYOUTS[str(MODEL["layout"])][str(MODEL["variant"])].required
        if not all((directory / name).is_file() for name in required):
            raise Unavailable("model-missing")
        try:
            document = json.loads((corpus / "transcript.json").read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise Unavailable("corpus-transcript") from exc
        if not isinstance(document, dict) or not all(
            isinstance(document.get(key), list) for key in ("words", "utterances")
        ):
            raise Unavailable("corpus-transcript")
        root, temporary = _prepare_out(out)
        report["out"] = None if temporary else str(root)
        env = private_environment(root)
        data_home = Path(env["XDG_DATA_HOME"])
        prepared = time.monotonic()
        audio_ms = build_meeting(corpus, data_home, minutes)
        report["prepare_ms"] = round((time.monotonic() - prepared) * 1000)
        from astra_voice.core.paths import APP_NAME

        meeting_dir = data_home / APP_NAME / "meetings" / MEETING_ID
        load = {**MODEL, "dir": str(directory.resolve()), "threads": threads}
        run = run_role(
            env, meeting_dir, load, echo_gate=echo_gate, audio_ms=audio_ms, progress=progress
        )
        report.update(
            {
                "load_ms": run.load_ms,
                "progress_events": run.progress_events,
                "credits_sent": run.credits,
                "failure": run.failure,
            }
        )
        metrics = evaluate(
            load_reference(document, audio_ms),
            run.utterances,
            audio_ms=audio_ms,
            wall_ms=run.wall_ms,
            done=run.done,
        )
        report.update(metrics)
    except Unavailable as exc:
        report["failure"] = str(exc)
    except (OSError, MeetingStoreError, MeetingValidationError) as exc:
        report["failure"] = "prepare-" + type(exc).__name__
    finally:
        if root is not None and temporary:
            shutil.rmtree(root, ignore_errors=True)
    return int(report["exit_code"]), report


def text_report(report: dict[str, Any]) -> list[str]:
    """Human summary: numbers only."""
    lines = ["meetings --transcribe: " + str(report.get("verdict", "unavailable"))]
    if report.get("failure"):
        lines.append("причина: " + str(report["failure"]))
    if "wer" in report:
        for track, item in report["wer"].items():
            lines.append(
                f"WER {track}: {item['percent']} % "
                f"(эталон {item['reference_words']} сл., распознано {item['hypothesis_words']})"
            )
        tc = report["timecodes"]
        lines.append(
            f"таймкоды |Δstart|, мс: p50 {tc['p50']} · p95 {tc['p95']} · max {tc['max']} "
            f"(n {tc['n']}, пропущено {tc['missing']})"
        )
        for key, name in (
            ("timecodes_separable", "реплики после паузы ≥ 1 с"),
            ("timecodes_reverse", "наша реплика → ближайшее начало эталона"),
        ):
            item = report[key]
            lines.append(
                f"{name}, мс: p50 {item['p50']} · p95 {item['p95']} · max {item['max']}"
                f" (n {item['n']})"
            )
        lines.append(
            f"звук {report['audio_ms']} мс · wall {report['wall_ms']} мс · RTF {report['rtf']}"
            f" · 1 ч ≈ {report['hour_wall_ms']} мс · пик RSS {report['peak_rss_kb']} КиБ"
        )
        lines.append(
            f"реплик {report['utterances']} · эхо отброшено {report['echo_dropped']}"
            f" · вердикты {json.dumps(report['verdicts'], ensure_ascii=False)}"
        )
    return lines


def progress_printer(stream: Any = None) -> Callable[[str, int, int], None]:
    last: list[Any] = ["", -1]

    def show(stage: str, done: int, total: int) -> None:
        decile = (done * 100 // total if total > 0 else 0) // 10
        if [stage, decile] != last:
            last[:] = [stage, decile]
            print(f"прогресс {stage}: {decile * 10} %", file=stream or sys.stderr, flush=True)

    return show
