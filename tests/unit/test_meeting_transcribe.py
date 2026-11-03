"""E13.M4 step 1: meeting-transcribe role on synthetic segments with a scripted engine.

T-403 (D10 crash between text and checkpoint), T-443 (credits/limits, control
independent of credits), T-452 (import gate), T-482 (model refusal), T-486
(damaged segment), T-487 (ASR deadline not extended by heartbeats), T-414
(checkpoint text/numbers only, removed after done).
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from astra_voice.meetings import store as store_module
from astra_voice.meetings.model import Checkpoint, Transcript
from astra_voice.worker import ipc
from astra_voice.worker.meeting_transcribe import (
    DAMAGED_TEXT,
    FRAME,
    MAX_UTTERANCE_MS,
    RATE,
    SPLIT_FROM_MS,
    UNRECOGNIZED_TEXT,
    sanitize_text,
    speech_regions,
)
from helpers.meeting_audio import (
    MID,
    MODEL,
    FakeEngine,
    Rig,
    bursts,
    make_disk,
    silence,
    tone,
    write_track,
)

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("filesystem_euid")]
ROLE = "meeting-transcribe"


@pytest.fixture
def rigs() -> Any:
    created: list[Rig] = []
    yield created
    for rig in created:
        rig.close()


def new_rig(rigs: list[Rig], disk: store_module.MeetingStore, **kwargs: Any) -> Rig:
    rig = Rig(disk, **kwargs)
    rigs.append(rig)
    return rig


def two_track_disk(tmp_path: Path) -> store_module.MeetingStore:
    disk = make_disk(tmp_path)
    write_track(disk, "mic", bursts([0.3, 0.5, 0.7]))
    write_track(disk, "far", bursts([0.4, 0.6], lead=2.0))
    return disk


def transcript(disk: store_module.MeetingStore) -> Transcript:
    value = disk.read(MID, "transcript.json")
    assert isinstance(value, Transcript)
    return value


def audio_digest(disk: store_module.MeetingStore) -> dict[str, str]:
    audio = disk.root / MID / "audio"
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(audio.iterdir())}


def tree(disk: store_module.MeetingStore) -> dict[str, bytes]:
    base = disk.root / MID
    return {
        str(p.relative_to(base)): p.read_bytes()
        for p in sorted(base.rglob("*"))
        if p.is_file() and not p.name.startswith(".")
    }


def test_pipeline_chronology_seq_speakers_and_final_files(tmp_path: Path, rigs: list[Rig]) -> None:
    disk = two_track_disk(tmp_path)
    rig = new_rig(rigs, disk)
    rig.load()
    loaded = next(e for e in rig.events if e["type"] == "model.loaded")
    assert loaded["engine_version"] == "fake-engine 1" and rig.engine.threads == 2  # ORT 2
    replies = rig.start()
    assert replies[0]["type"] == "meeting.ack"
    accepted = rig.wait(lambda e: e["type"] == "meeting.job.accepted")
    assert accepted["resume_from"] == {"mic_ms": 0, "far_ms": 0}
    done = rig.finish()
    assert done["type"] == "meeting.job.done", done
    items = rig.utterances()
    assert [u["seq"] for u in items] == list(range(5))
    assert [(u["track"], u["text"]) for u in items] == [
        ("mic", "речь 30"),
        ("far", "речь 40"),
        ("mic", "речь 50"),
        ("far", "речь 60"),
        ("mic", "речь 70"),
    ]
    assert [u["speaker"] for u in items] == ["me", "others", "me", "others", "me"]
    assert all(u["provisional"] is True for u in items)
    # Burst k of mic starts at 1 + 2k s, of far at 2 + 2k s; PAD 200 ms, frame 32 ms.
    for u, expected in zip(items, [1000, 2000, 3000, 4000, 5000], strict=True):
        assert expected - 250 <= u["start_ms"] <= expected - 150
        assert u["end_ms"] - u["start_ms"] == pytest.approx(1400, abs=80)
    assert done["utterances"] == 5 and done["speakers"] == 2
    assert done["diar_status"] == "skipped" and done["echo_dropped"] == 0
    final = transcript(disk)
    assert final.labels_final and final.transcript_revision == done["transcript_revision"]
    assert [u.text for u in final.utterances] == [u["text"] for u in items]
    assert all(u.final for u in final.utterances)
    assert not (disk.root / MID / "checkpoint.json").exists()
    stages = {e["stage"] for e in rig.events if e["type"] == "meeting.job.progress"}
    assert {"finalize"} <= stages
    for event in rig.events:
        assert len(ipc.encode(event, role=ROLE)) <= ipc.MAX_FRAME_BYTES + 4


def test_split_point_is_least_speech_frame_inside_18_24_seconds() -> None:
    frames = 50 * RATE // FRAME
    probs = [1.0] * frames
    dip = 20 * RATE // FRAME
    probs[dip] = 0.4  # still speech (>= SPEECH_OFF), the least speech-like frame
    regions = speech_regions(probs, frames * FRAME)
    assert all(b - a <= MAX_UTTERANCE_MS * RATE // 1000 for a, b in regions)
    assert regions[0] == (0, dip * FRAME)
    assert regions[1][0] == dip * FRAME
    lengths = [b - a for a, b in regions]
    assert sum(lengths) == frames * FRAME and len(regions) == 3
    # Without a dip the cut falls at the first frame at >= 18 s: deterministic.
    flat = speech_regions([1.0] * frames, frames * FRAME)
    first = -(-SPLIT_FROM_MS * RATE // 1000 // FRAME) * FRAME
    assert flat[0] == (0, first) and flat == speech_regions([1.0] * frames, frames * FRAME)


def test_long_speech_job_never_exceeds_24_seconds(tmp_path: Path, rigs: list[Rig]) -> None:
    disk = make_disk(tmp_path)
    write_track(disk, "mic", np.concatenate([silence(1), tone(50, 0.5), silence(1)]))
    rig = new_rig(rigs, disk)
    rig.load()
    rig.start(tracks=["mic"])
    assert rig.finish()["type"] == "meeting.job.done"
    items = rig.utterances()
    assert len(items) == 3
    assert all(u["end_ms"] - u["start_ms"] <= MAX_UTTERANCE_MS for u in items)
    assert all(a["end_ms"] == b["start_ms"] for a, b in zip(items, items[1:], strict=False))


def many_bursts_disk(tmp_path: Path, name: str = "a") -> store_module.MeetingStore:
    disk = make_disk(tmp_path / name)
    levels = [0.2 + (i % 50) / 100 for i in range(70)]
    write_track(disk, "mic", bursts(levels, on=0.5, off=0.6), segment_s=30)
    return disk


def run_to_end(rigs: list[Rig], disk: store_module.MeetingStore, **start: Any) -> Rig:
    rig = new_rig(rigs, disk)
    rig.load()
    rig.start(tracks=["mic"], **start)
    done = rig.finish()
    assert done["type"] == "meeting.job.done", done
    return rig


def crash_on(monkeypatch: pytest.MonkeyPatch, nth: int, name: str = "checkpoint.json") -> None:
    """Emulate kill -9 just before the nth write of `name` (earlier writes are durable)."""
    original = store_module.MeetingWriter.write
    count = {"writes": 0}

    def write(self: store_module.MeetingWriter, filename: str, document: Any) -> None:
        if filename == name:
            count["writes"] += 1
            if count["writes"] == nth:
                raise store_module.MeetingStoreError("crash")
        original(self, filename, document)

    monkeypatch.setattr(store_module.MeetingWriter, "write", write)


@pytest.mark.parametrize("crash_at", [1, 2])
def test_t403_crash_between_text_and_checkpoint_neither_loses_nor_duplicates(
    tmp_path: Path, rigs: list[Rig], monkeypatch: pytest.MonkeyPatch, crash_at: int
) -> None:
    baseline = run_to_end(rigs, many_bursts_disk(tmp_path, "baseline"))
    expected = [
        (u.seq, u.track, u.start_ms, u.end_ms, u.text) for u in transcript(baseline.disk).utterances
    ]
    assert len(expected) == 70
    disk = many_bursts_disk(tmp_path, "crash")
    with monkeypatch.context() as patch:
        crash_on(patch, crash_at)
        crashed = new_rig(rigs, disk)
        crashed.load()
        crashed.start(tracks=["mic"])
        failed = crashed.finish()
        assert failed["type"] == "meeting.job.failed" and failed["code"] == "io"
        crashed.close()
    durable = transcript(disk)
    assert len(durable.utterances) == 32 * crash_at
    checkpoint = disk.read(MID, "checkpoint.json") if crash_at == 2 else None
    if checkpoint is not None:
        assert isinstance(checkpoint, Checkpoint)
        # The cursor never passes durable text.
        assert checkpoint.transcript_revision == durable.transcript_revision - 1
        assert checkpoint.asr.mic.done_until_ms <= max(u.end_ms for u in durable.utterances)
    resumed = new_rig(rigs, disk)
    resumed.load()
    resumed.start(tracks=["mic"], resume=True)
    accepted = resumed.wait(lambda e: e["type"] == "meeting.job.accepted")
    done = resumed.finish()
    assert done["type"] == "meeting.job.done"
    seqs = [u["seq"] for u in resumed.utterances()]
    if crash_at == 2:
        # Durable revision is reused: re-emission starts right after it.
        assert seqs == list(range(64, 70))
        assert accepted["resume_from"]["mic_ms"] == durable.utterances[-1].end_ms
    else:
        # Text without any checkpoint cannot be verified: recomputed from zero.
        assert seqs == list(range(70))
    final = transcript(disk)
    assert [(u.seq, u.track, u.start_ms, u.end_ms, u.text) for u in final.utterances] == expected
    assert final.transcript_revision > durable.transcript_revision
    reused = 64 if crash_at == 2 else 0
    assert final.utterances[:reused] == [
        dataclasses.replace(u, final=True) for u in durable.utterances[:reused]
    ]


@pytest.mark.parametrize("change", ["config", "model", "manifest"])
def test_t403_incompatible_checkpoint_restarts_from_zero(
    tmp_path: Path, rigs: list[Rig], monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    disk = many_bursts_disk(tmp_path)
    with monkeypatch.context() as patch:
        crash_on(patch, 3, "transcript.json")  # durable rev 2 + checkpoint rev 2
        first = new_rig(rigs, disk)
        first.load()
        first.start(tracks=["mic"])
        assert first.finish()["code"] == "io"
        first.close()
    before = transcript(disk)
    rig = new_rig(rigs, disk)
    if change == "model":
        rig.send("model.load", **{**MODEL, "revision": "r2"})
        rig.wait(lambda e: e["type"] == "model.loaded")
    else:
        rig.load()
    if change == "manifest":
        with open(disk.root / MID / "audio" / "mic-0000.wav", "ab") as stream:
            stream.write(b"\0\0")
    rig.start(tracks=["mic"], resume=True, echo=change == "config")
    accepted = rig.wait(lambda e: e["type"] == "meeting.job.accepted")
    assert accepted["resume_from"] == {"mic_ms": 0, "far_ms": 0}
    assert rig.finish()["type"] == "meeting.job.done"
    assert [u["seq"] for u in rig.utterances()] == list(range(70))
    final = transcript(disk)
    # Rewritten, not appended: no duplicated seq/text and a newer revision.
    assert len(final.utterances) == 70 and final.transcript_revision > before.transcript_revision


def test_compatible_checkpoint_resumes_after_next_window(
    tmp_path: Path, rigs: list[Rig], monkeypatch: pytest.MonkeyPatch
) -> None:
    disk = many_bursts_disk(tmp_path)
    with monkeypatch.context() as patch:
        crash_on(patch, 3, "transcript.json")
        first = new_rig(rigs, disk)
        first.load()
        first.start(tracks=["mic"])
        assert first.finish()["code"] == "io"
        first.close()
    checkpoint = disk.read(MID, "checkpoint.json")
    assert isinstance(checkpoint, Checkpoint) and checkpoint.transcript_revision == 2
    rig = new_rig(rigs, disk)
    rig.load()
    rig.start(tracks=["mic"], resume=True)
    accepted = rig.wait(lambda e: e["type"] == "meeting.job.accepted")
    assert accepted["resume_from"]["mic_ms"] == checkpoint.asr.mic.done_until_ms
    assert rig.finish()["type"] == "meeting.job.done"
    assert [u["seq"] for u in rig.utterances()] == list(range(64, 70))
    assert rig.engine.calls == 6


def test_t487_asr_deadline_gives_unrecognized_and_ping_does_not_extend(
    tmp_path: Path, rigs: list[Rig]
) -> None:
    disk = two_track_disk(tmp_path)
    rig = new_rig(rigs, disk, engine=FakeEngine(hang_calls={1}), asr_timeout_s=0.3)
    rig.load()
    rig.start()
    deadline = time.monotonic() + 10
    while not rig.engine.hang_started and time.monotonic() < deadline:
        rig.pump()
        time.sleep(0.005)
    while not rig.engine.hang_ended and time.monotonic() < deadline:
        assert rig.send("ping")[0]["type"] == "pong"  # heartbeat during the window
        rig.pump()
        time.sleep(0.02)
    hung = rig.engine.hang_ended[0] - rig.engine.hang_started[0]
    assert 0.25 <= hung < 0.9
    assert rig.finish()["type"] == "meeting.job.done"
    texts = [u["text"] for u in rig.utterances()]
    assert texts[1] == UNRECOGNIZED_TEXT
    assert texts[0] == "речь 30" and texts[2:] == ["речь 50", "речь 60", "речь 70"]


@pytest.mark.parametrize("damage", ["sidecar", "wav", "truncated"])
def test_t486_damaged_segment_is_marked_not_silence(
    tmp_path: Path, rigs: list[Rig], damage: str
) -> None:
    disk = make_disk(tmp_path)
    # Three 6 s segments, one burst each at 2-3 s inside the segment.
    segment = np.concatenate([silence(2), tone(1, 0.5), silence(3)])
    write_track(disk, "mic", np.concatenate([segment] * 3), segment_s=6)
    audio = disk.root / MID / "audio"
    if damage == "sidecar":
        (audio / "mic-0001.json").write_bytes(b'{"schema": 1, "broken')
    elif damage == "wav":
        (audio / "mic-0001.wav").unlink()
    else:
        path = audio / "mic-0001.wav"
        os.truncate(path, path.stat().st_size - 2 * RATE * 2)  # last 2 s physically lost
    rig = new_rig(rigs, disk)
    rig.load()
    rig.start(tracks=["mic"])
    assert rig.finish()["type"] == "meeting.job.done"
    items = [(u["text"], u["start_ms"], u["end_ms"]) for u in rig.utterances()]
    recognized = [item for item in items if item[0] == "речь 50"]
    marks = [item for item in items if item[0] == DAMAGED_TEXT]
    assert len(marks) == 1
    if damage == "truncated":
        # The burst of segment 1 at 8-9 s survives; 10..12 s is reported damaged.
        assert len(recognized) == 3
        assert marks[0][1:] == (pytest.approx(10000, abs=40), pytest.approx(12000, abs=40))
    else:
        assert len(recognized) == 2
        assert marks[0][1:] == (pytest.approx(6000, abs=40), pytest.approx(12000, abs=40))
    assert transcript(disk).labels_final


def test_unclosed_segment_recovered_by_physical_length(tmp_path: Path, rigs: list[Rig]) -> None:
    disk = make_disk(tmp_path)
    pcm = np.concatenate([silence(2), tone(1, 0.3), silence(4), tone(1, 0.6), silence(1)])
    # Sidecar checkpointed at 6 s; the burst at 7-8 s lives in the unsynced tail.
    write_track(disk, "mic", pcm, segment_s=30, unclosed_tail_s=3)
    rig = new_rig(rigs, disk)
    rig.load()
    rig.start(tracks=["mic"])
    assert rig.finish()["type"] == "meeting.job.done"
    items = rig.utterances()
    assert [u["text"] for u in items] == ["речь 30", "речь 60"]
    assert items[1]["start_ms"] == pytest.approx(6800, abs=60)


@pytest.mark.parametrize("limit", ["max_ms", "utterances", "wall"])
def test_t443_limits_fail_with_limit_and_audio_intact(
    tmp_path: Path, rigs: list[Rig], limit: str
) -> None:
    disk = two_track_disk(tmp_path)
    before = audio_digest(disk)
    now = {"t": 1000.0}
    engine = FakeEngine()
    if limit == "wall":
        engine.on_call = lambda call: now.__setitem__("t", now["t"] + 10_000)
    rig = new_rig(rigs, disk, engine=engine, clock=lambda: now["t"])
    rig.load()
    limits = {"max_ms": 14_400_000, "max_utterances": 28800, "max_blocks": 12}
    if limit == "max_ms":
        limits["max_ms"] = 3000
    elif limit == "utterances":
        limits["max_utterances"] = 2
    rig.start(limits=limits)
    failed = rig.finish()
    assert failed["type"] == "meeting.job.failed" and failed["code"] == "limit"
    assert engine.calls == (1 if limit == "wall" else 0)
    assert audio_digest(disk) == before


def test_t443_no_credits_blocks_between_utterances_control_still_works(
    tmp_path: Path, rigs: list[Rig]
) -> None:
    disk = two_track_disk(tmp_path)
    rig = new_rig(rigs, disk)
    rig.load()
    rig.start(credits=0)
    rig.wait(lambda e: e["type"] == "meeting.job.accepted")
    time.sleep(0.3)
    rig.pump()
    assert rig.engine.calls == 0 and not rig.utterances()
    assert rig.send("ping")[0]["type"] == "pong"
    rig.credit(1)
    rig.wait(lambda e: e["type"] == "meeting.job.utterance")
    time.sleep(0.2)
    rig.pump()
    assert rig.engine.calls == 1 and len(rig.utterances()) == 1
    started = time.monotonic()
    replies = rig.send("meeting.job.cancel", **rig.identity())
    if replies[0]["type"] != "meeting.job.cancelled":
        rig.wait(lambda e: e["type"] == "meeting.job.cancelled", 1.0)
    assert time.monotonic() - started < 1.0


def test_cancel_within_one_second_and_no_writes_after(tmp_path: Path, rigs: list[Rig]) -> None:
    disk = many_bursts_disk(tmp_path)
    engine = FakeEngine(hang_calls={40})
    rig = new_rig(rigs, disk, engine=engine)
    rig.load()
    rig.start(tracks=["mic"])
    deadline = time.monotonic() + 10
    while not engine.hang_started and time.monotonic() < deadline:
        rig.pump()
        if rig.state.job is not None and rig.state.job.credits < 8:
            rig.credit(32)
        time.sleep(0.005)
    assert (disk.root / MID / "checkpoint.json").exists()  # a window was durable
    snapshot = tree(disk)
    count = len(rig.utterances())
    started = time.monotonic()
    replies = rig.send("meeting.job.cancel", **rig.identity())
    if replies[0]["type"] != "meeting.job.cancelled":
        rig.wait(lambda e: e["type"] == "meeting.job.cancelled", 1.0)
    assert time.monotonic() - started < 1.0
    job = rig.state.job
    while job is not None and not job.finished and time.monotonic() < deadline:
        time.sleep(0.01)
    time.sleep(0.1)
    rig.pump()
    assert len(rig.utterances()) == count
    assert not any(e["type"] in {"meeting.job.done", "meeting.job.failed"} for e in rig.events)
    assert tree(disk) == snapshot


def test_memory_below_threshold_waits_without_touching_files(
    tmp_path: Path, rigs: list[Rig]
) -> None:
    disk = two_track_disk(tmp_path)
    rig = new_rig(rigs, disk)
    rig.memory_mb = 850
    rig.send("model.load", **MODEL)
    refused = rig.wait(lambda e: e["type"] in {"model.loaded", "error"})
    assert refused["type"] == "error" and refused["code"] == "memory"
    rig.start()
    waiting = rig.wait(lambda e: e["type"] == "meeting.job.waiting")
    assert waiting["reason"] == "memory"
    assert (waiting["need_mb"], waiting["available_mb"]) == (900, 850)
    assert rig.engine.loads == 0 and not (disk.root / MID / "transcript.json").exists()
    deadline = time.monotonic() + 2
    while rig.state.job is not None and time.monotonic() < deadline:
        rig.pump()
        time.sleep(0.005)
    assert rig.state.job is None
    rig.memory_mb = 4000
    rig.start()
    assert rig.finish()["type"] == "meeting.job.done"
    assert rig.engine.loads == 1


@pytest.mark.parametrize("where", ["outside", "root", "symlink-out"])
def test_directory_outside_meetings_root_fails_io_before_opening(
    tmp_path: Path, rigs: list[Rig], where: str
) -> None:
    disk = two_track_disk(tmp_path)
    outside = tmp_path / "elsewhere" / MID
    outside.mkdir(parents=True)
    if where == "outside":
        directory = str(outside)
    elif where == "root":
        directory = str(disk.root)
    else:
        link = tmp_path / "link"
        link.symlink_to(outside)
        directory = str(link)
    rig = new_rig(rigs, disk)
    rig.load()
    loads = rig.engine.loads
    rig.start(directory=directory)
    failed = rig.finish()
    assert failed["type"] == "meeting.job.failed" and failed["code"] == "io"
    assert not any(e["type"] == "meeting.job.accepted" for e in rig.events)
    assert rig.engine.calls == 0 and rig.engine.loads == loads
    assert list(outside.iterdir()) == []


def test_t482_model_refusal_is_failed_model_and_checkpoint_does_not_bypass(
    tmp_path: Path, rigs: list[Rig], monkeypatch: pytest.MonkeyPatch
) -> None:
    disk = many_bursts_disk(tmp_path)
    with monkeypatch.context() as patch:
        crash_on(patch, 3, "transcript.json")
        first = new_rig(rigs, disk)
        first.load()
        first.start(tracks=["mic"])
        assert first.finish()["code"] == "io"
        first.close()
    snapshot = tree(disk)
    rig = new_rig(rigs, disk, engine=FakeEngine(load_raises=True))
    rig.send("model.load", **MODEL)
    refused = rig.wait(lambda e: e["type"] in {"model.loaded", "error"})
    assert refused["code"] == "model" and "PRIVATE" not in json.dumps(refused)
    rig.pump()
    reply = rig.start(tracks=["mic"], resume=True)
    assert reply[0]["type"] == "error" and reply[0]["code"] == "model"
    assert tree(disk) == snapshot


def test_model_load_unload_and_measure(tmp_path: Path, rigs: list[Rig]) -> None:
    rig = new_rig(rigs, two_track_disk(tmp_path))
    rig.load()
    rig.load()
    assert rig.engine.loads == 1  # same identity: loaded once per process
    rig.send("model.unload")
    rig.wait(lambda e: e["type"] == "model.unloaded")
    assert rig.engine.unloads == 1
    measured = rig.send("measure")[0]
    assert measured["type"] == "measured" and measured["vm_hwm_kb"] > 0
    assert rig.start()[0]["code"] == "model"  # unloaded: no silent reload


def walk_numeric_arrays(value: Any) -> list[int]:
    found: list[int] = []
    if isinstance(value, list):
        if value and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in value):
            found.append(len(value))
        for item in value:
            found += walk_numeric_arrays(item)
    elif isinstance(value, dict):
        for item in value.values():
            found += walk_numeric_arrays(item)
    return found


def test_t414_checkpoint_only_text_and_numbers_and_removed_after_done(
    tmp_path: Path, rigs: list[Rig], monkeypatch: pytest.MonkeyPatch
) -> None:
    written: list[Any] = []
    original = store_module.MeetingWriter._persist

    def persist(self: Any, filename: str, data: dict[str, Any]) -> None:
        if filename == "checkpoint.json":
            written.append(json.loads(json.dumps(data)))
        original(self, filename, data)

    monkeypatch.setattr(store_module.MeetingWriter, "_persist", persist)
    disk = many_bursts_disk(tmp_path)
    run_to_end(rigs, disk)
    assert len(written) == 2
    for document in written:
        assert all(length <= 16 for length in walk_numeric_arrays(document))
        assert set(document) == set(Checkpoint.SCHEMA["properties"])
        assert document["diar"]["turns"] == [] and document["diar"]["label_map"] == {}
        assert len(document["input_manifest_sha256"]) == 64
        assert len(document["processing_config_sha256"]) == 64
    assert not (disk.root / MID / "checkpoint.json").exists()


def test_frames_and_text_bounded(tmp_path: Path, rigs: list[Rig]) -> None:
    disk = make_disk(tmp_path)
    write_track(disk, "mic", bursts([0.5]))
    engine = FakeEngine()
    engine.text_override = "слово​\x07 " * 8000  # ~ 90 KiB with controls
    rig = new_rig(rigs, disk, engine=engine)
    rig.load()
    rig.start(tracks=["mic"])
    assert rig.finish()["type"] == "meeting.job.done"
    (item,) = rig.utterances()
    assert len(item["text"].encode()) <= ipc.MAX_TEXT_BYTES
    assert "​" not in item["text"] and "\x07" not in item["text"]
    assert len(ipc.encode(item, role=ROLE)) <= ipc.MAX_FRAME_BYTES + 4
    assert sanitize_text("а\nб\tв") == "а б в"


def test_t452_transcribe_import_gate() -> None:
    source = str(Path(__file__).resolve().parents[2] / "src")
    script = f"""
import sys
sys.path.insert(0,{source!r})
from astra_voice.worker.meeting_transcribe import MeetingTranscribeState
from astra_voice.meetings import echo_gate
from astra_voice.worker import meeting_capture
forbidden = ('PyQt5','PySide6','pulsectl','astra_voice.worker.pulse_stream',
             'astra_voice.worker.audio','astra_voice.worker.monitor_stream',
             'astra_voice.worker.state','requests','urllib3','httpx','http.client',
             'urllib.request','onnxruntime','onnx_asr')
for name in forbidden:
    assert name not in sys.modules, name
"""
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", script], capture_output=True, timeout=20, check=False
    )
    assert result.returncode == 0, result.stderr.decode()


def test_run_role_wires_transcribe_handler(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from astra_voice.worker import meeting_roles

    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    seen: dict[str, dict[str, Any]] = {}
    errors: list[BaseException] = []

    def peer() -> None:
        reader = ipc.FrameReader(role=ROLE)
        parent.settimeout(5)
        try:

            def receive(kind: str) -> None:
                while kind not in seen:
                    for message in reader.feed(parent.recv(65536)):
                        seen.setdefault(message["type"], message)

            receive("hello")
            ping = {"type": "ping", "req": "p1", "request_type": "ping"}
            parent.sendall(ipc.encode(ping, role=ROLE))
            receive("pong")
            cancel = {
                "type": "meeting.job.cancel",
                "req": "c1",
                "request_type": "meeting.job.cancel",
                "meeting_id": MID,
                "meeting_generation": 0,
            }
            parent.sendall(ipc.encode(cancel, role=ROLE))
            receive("meeting.job.cancelled")
        except BaseException as exc:  # reported in the main thread
            errors.append(exc)
        finally:
            parent.close()

    thread = threading.Thread(target=peer, daemon=True)
    thread.start()
    # signal.signal requires the main thread: run the role here, the GUI in a thread.
    code = meeting_roles.run_role(child, ROLE, parent_pid=os.getppid())
    thread.join(5)
    assert not errors and code == 0
    assert seen["hello"]["family"] == "astra-voice.meetings"
    assert seen["pong"]["req"] == "p1"
    assert seen["meeting.job.cancelled"]["req"] == "c1"


@pytest.mark.parametrize("enabled", [False, True])
def test_echo_gate_drops_mic_copy_of_far_only_when_enabled(
    tmp_path: Path, rigs: list[Rig], enabled: bool
) -> None:
    rng = np.random.default_rng(42)
    far = np.zeros(12 * RATE)
    for start in (2, 6):  # far speech at 2-4 s and 6-8 s
        burst = np.convolve(rng.standard_normal(2 * RATE + 7), np.ones(8) / 8, mode="valid")
        far[start * RATE : (start + 2) * RATE] = 0.3 * burst / np.sqrt(np.mean(burst**2))
    lag = 120 * RATE // 1000
    mic = np.zeros_like(far)
    mic[lag:] = 0.8 * far[:-lag]  # loudspeaker echo 120 ms later
    mic[10 * RATE : 11 * RATE] = 0.3 * np.sin(np.arange(RATE) * 0.2)  # own speech, no far
    disk = make_disk(tmp_path)
    write_track(disk, "far", np.round(far * 32767).astype(np.int16))
    write_track(disk, "mic", np.round(mic * 32767).astype(np.int16))
    rig = new_rig(rigs, disk)
    rig.load()
    rig.start(echo=enabled)
    done = rig.finish()
    assert done["type"] == "meeting.job.done"
    tracks = [u["track"] for u in rig.utterances()]
    if enabled:
        assert done["echo_dropped"] == 2 and tracks == ["far", "far", "mic"]
    else:
        assert done["echo_dropped"] == 0 and tracks.count("mic") == 3


def test_store_remove_checkpoint_only_by_transcribe_after_final_text(tmp_path: Path) -> None:
    disk = make_disk(tmp_path)
    draft = Transcript(meeting_id=MID, transcript_revision=1)
    with disk.writer(MID, store_module.Writer.TRANSCRIBE) as owner:
        owner.write("transcript.json", draft)
        owner.write("checkpoint.json", Checkpoint(meeting_id=MID, transcript_revision=1))
        with pytest.raises(store_module.MeetingStoreError):
            owner.remove("checkpoint.json")  # text is not final yet
        with pytest.raises(store_module.MeetingStoreError):
            owner.remove("transcript.json")
        owner.write(
            "transcript.json",
            Transcript(meeting_id=MID, transcript_revision=2, labels_final=True),
        )
        owner.remove("checkpoint.json")
        owner.remove("checkpoint.json")  # idempotent
    assert not (disk.root / MID / "checkpoint.json").exists()
    with disk.writer(MID, store_module.Writer.GUI) as gui:
        with pytest.raises(store_module.MeetingStoreError):
            gui.remove("checkpoint.json")
