"""M7 transcriber: D4/D10 boundaries, restart, labels, cancellation and failures.

Tests named subprocess_* use a real child/socketpair; run outside the restricted
Codex sandbox. They deliberately have no permission-based skips.
"""

from __future__ import annotations

import copy
import fcntl
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from astra_voice.meetings import diar_link
from astra_voice.meetings.events import MeetingState, PhaseStatus
from astra_voice.meetings.model import Checkpoint, DiarTurn, Meeting, Transcript, Utterance
from astra_voice.meetings.store import MeetingStoreError, MeetingWriter, Writer
from astra_voice.worker import meeting_diar_client, meeting_transcribe
from astra_voice.worker.meeting_diar_client import DiarCancelled, DiarProcess, sealed_pcm
from astra_voice.worker.meeting_transcribe import assign_speakers, diar_blocks
from helpers.meeting_audio import MID, Rig, bursts, make_disk, write_track

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("filesystem_euid")]
MODELS = {"segmentation_dir": "/absent/segmentation", "embedder_dir": "/absent/embedder"}


class ScriptedDiar:
    def __init__(self, identity: dict[str, Any], cancelled: Any) -> None:
        self.identity, self.cancelled = identity, cancelled
        self.blocks: list[tuple[int, int]] = []
        self.alive = True
        self.peak_rss_kb = 100
        self.on_block: Any = None
        self.on_close: Any = None
        self.overall_deadline = float("inf")

    def load(self, models: dict[str, str]) -> None:
        assert models == MODELS

    def block(self, audio: Any, index: int, offset: int) -> list[list[Any]]:
        self.blocks.append((index, offset))
        if self.on_block:
            self.on_block(index)
        length = len(audio) // 16
        return [[offset, offset + length, f"S{index + 1}"]]

    def finish(self) -> dict[str, Any]:
        return {
            "label_map": {f"S{i + 1}": "S1" for i, _ in self.blocks},
            "speakers_total": 1 if self.blocks else 0,
        }

    def close(self, *, completed: bool = False) -> bool:
        if self.on_close:
            self.on_close(completed)
        self.alive = False
        return completed


def start(rig: Rig, *, resume: bool = False, tracks: list[str] | None = None) -> None:
    rig.send(
        "meeting.job.start",
        **rig.identity(),
        dir=str(rig.disk.root / MID),
        tracks=tracks or ["mic", "far"],
        variant="app_stream",
        diarization=True,
        diar_models=MODELS,
        self_track="mic",
        resume=resume,
        echo_gate=False,
        limits={"max_ms": 14_400_000, "max_utterances": 9000, "max_blocks": 12},
    )
    rig.credit(64)


@pytest.fixture
def rig(tmp_path: Path) -> Any:
    disk = make_disk(tmp_path)
    write_track(disk, "far", bursts([0.5] * 5))
    write_track(disk, "mic", bursts([0.6]))
    value = Rig(disk)
    value.load()
    yield value
    value.close()


def install(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> list[ScriptedDiar]:
    roles: list[ScriptedDiar] = []

    def factory(identity: Any, cancelled: Any, **kwargs: Any) -> ScriptedDiar:
        assert rig.engine.unloads > 0 and rig.state.engine is None
        checkpoint = rig.disk.read(MID, "checkpoint.json")
        assert isinstance(checkpoint, Checkpoint)
        assert checkpoint.asr.status == PhaseStatus.DONE
        assert checkpoint.diar.status == PhaseStatus.RUNNING
        role = ScriptedDiar(identity, cancelled)
        roles.append(role)
        return role

    rig.state.diar_factory = factory
    monkeypatch.setattr(meeting_transcribe, "DIAR_BLOCK_MS", 3000)
    monkeypatch.setattr(meeting_transcribe, "DIAR_PAUSE_SEARCH_MS", 0)
    return roles


def test_d4_d10_phase_boundaries_and_canonical_publication(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    roles = install(rig, monkeypatch)
    writes: list[tuple[str, dict[str, Any], bool]] = []
    original = MeetingWriter.write

    def capture(owner: Any, filename: str, document: Any) -> None:
        original(owner, filename, document)
        writes.append((filename, copy.deepcopy(document.data), bool(roles and roles[0].alive)))

    monkeypatch.setattr(MeetingWriter, "write", capture)
    start(rig)
    done = rig.finish()
    assert done["type"] == "meeting.job.done" and done["diar_status"] == "done"
    assert done["speakers"] == 2  # S1 and the independent microphone speaker
    final = rig.disk.read(MID, "transcript.json")
    assert isinstance(final, Transcript) and final.labels_final
    assert {u.speaker for u in final.utterances if u.track == "far"} == {"S1"}
    assert {u.speaker for u in final.utterances if u.track == "mic"} == {"me"}
    checkpoints = [(d, alive) for name, d, alive in writes if name == "checkpoint.json"]
    assert any(
        d["asr"]["status"] == "done" and d["diar"]["status"] == "pending" for d, _ in checkpoints
    )
    before_exit = [d for d, alive in checkpoints if alive and d["diar"]["label_map"]]
    assert before_exit and all(d["diar"]["status"] == "running" for d in before_exit)
    after_exit = [d for d, alive in checkpoints if d["diar"]["status"] == "done"]
    assert after_exit and all(not alive for d, alive in checkpoints if d in after_exit)
    assert after_exit[0]["reconcile"]["status"] == "pending"
    assert after_exit[-1]["reconcile"]["status"] == "done"
    for i, (name, document, _) in enumerate(writes):
        if name == "checkpoint.json":
            assert writes[i - 1][0] == "transcript.json"
            assert writes[i - 1][1]["transcript_revision"] == document["transcript_revision"]
            assert document["diar"]["algo"] == diar_link.DIAR_ALGORITHM
            assert_plain(document)
    labels = [e for e in rig.events if e["type"] == "meeting.job.labels"]
    assert any(not e["final"] for e in labels) and labels[-1]["final"]
    assert all(label == "S1" for _, label in labels[-1]["items"] if label != "me")
    assert not (rig.disk.root / MID / "checkpoint.json").exists()
    for event in rig.events:
        assert_plain(event)


def assert_plain(value: Any) -> None:
    if isinstance(value, dict):
        assert not {"features", "embeddings", "centroids"} & value.keys()
        for child in value.values():
            assert_plain(child)
    elif isinstance(value, list):
        assert not (len(value) > 16 and all(isinstance(x, (int, float)) for x in value))
        for child in value:
            assert_plain(child)


@pytest.mark.parametrize("failure", ["crash", "memory", "deadline", "load", "map", "exit"])
def test_failure_preserves_text_and_audio_then_restarts_all_blocks(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    roles = install(rig, monkeypatch)
    factory = rig.state.diar_factory

    def fail_factory(identity: Any, cancelled: Any, **kwargs: Any) -> ScriptedDiar:
        role: ScriptedDiar = factory(identity, cancelled)

        def block(index: int) -> None:
            if index == 1:
                raise {"memory": MemoryError, "deadline": TimeoutError}.get(failure, OSError)()

        if failure in {"crash", "memory", "deadline"}:
            role.on_block = block
        elif failure == "load":
            role.load = lambda models: (_ for _ in ()).throw(ValueError("model"))  # type: ignore[method-assign]
        elif failure == "map":
            role.finish = lambda: {"label_map": {}, "speakers_total": 0}  # type: ignore[method-assign]
        elif failure == "exit":

            def close(*, completed: bool = False) -> bool:
                role.alive = False
                return False

            role.close = close  # type: ignore[method-assign]
        return role

    rig.state.diar_factory = fail_factory
    start(rig)
    first = rig.finish()
    assert first["type"] == "meeting.job.done" and first["diar_status"] == "failed"
    assert not roles[0].alive
    text = rig.disk.read(MID, "transcript.json")
    assert isinstance(text, Transcript) and text.labels_final
    before = [(u.id, u.text) for u in text.utterances]
    assert all(u.speaker == "others" for u in text.utterances if u.track == "far")
    cp = rig.disk.read(MID, "checkpoint.json")
    assert isinstance(cp, Checkpoint) and cp.asr.status == PhaseStatus.DONE
    assert cp.diar.status != PhaseStatus.DONE
    calls = rig.engine.calls
    rig.state.diar_factory = factory
    start(rig, resume=True)
    assert rig.finish()["diar_status"] == "done"
    assert rig.engine.calls == calls
    assert roles[1].blocks[0] == (0, 0) and len(roles[1].blocks) == 4
    result = rig.disk.read(MID, "transcript.json")
    assert isinstance(result, Transcript)
    assert [(u.id, u.text) for u in result.utterances] == before
    assert list((rig.disk.root / MID / "audio").glob("*.wav"))


@pytest.mark.parametrize("deleting", [False, True])
def test_cancel_waits_for_exit_and_lease_but_ping_is_independent(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, deleting: bool
) -> None:
    roles = install(rig, monkeypatch)
    factory = rig.state.diar_factory
    entered, closing, release = (threading.Event() for _ in range(3))

    def factory_block(identity: Any, cancelled: Any, **kwargs: Any) -> ScriptedDiar:
        role: ScriptedDiar = factory(identity, cancelled)

        def block(index: int) -> None:
            entered.set()
            while not cancelled():
                time.sleep(0.005)
            raise DiarCancelled

        def close(completed: bool) -> None:
            closing.set()
            assert release.wait(5)

        role.on_block, role.on_close = block, close
        return role

    rig.state.diar_factory = factory_block
    start(rig)
    assert entered.wait(5)
    job = rig.state.job
    if deleting:
        meeting = rig.disk.read(MID, "meeting.json")
        assert isinstance(meeting, Meeting)
        meeting.state = MeetingState.DELETING
        rig.disk.write(MID, "meeting.json", meeting, writer=Writer.GUI)
        rig.disk.begin_delete(MID)
        with pytest.raises(MeetingStoreError):
            rig.disk.purge_deleted(MID)
    response = rig.send("meeting.job.cancel", **rig.identity())
    assert response[0]["type"] == "meeting.ack"
    assert closing.wait(2)
    assert rig.send("ping")[0]["type"] == "pong"
    assert all(e["type"] != "meeting.job.cancelled" for e in rig.pump())
    with pytest.raises(MeetingStoreError), rig.disk.writer(MID, Writer.DIAR):
        pass
    release.set()
    rig.wait(lambda e: e["type"] == "meeting.job.cancelled")
    assert not roles[0].alive
    rig.settle(job)
    if deleting:
        rig.disk.purge_deleted(MID)
        assert not (rig.disk.root / f".deleting-{MID}").exists()
    else:
        with rig.disk.writer(MID, Writer.DIAR):
            pass
    assert not any(e["type"] == "meeting.job.done" for e in rig.events)


def test_memory_retry_and_exhaustion_preserve_text(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(rig, monkeypatch)
    original = rig.state._unload_ack

    def unload() -> dict[str, Any]:
        ack = original()
        rig.memory_mb = 0
        return ack

    rig.state._unload_ack = unload  # type: ignore[method-assign]
    monkeypatch.setattr(meeting_transcribe, "DIAR_MEMORY_WAIT_S", 0.05)
    monkeypatch.setattr(meeting_transcribe, "DIAR_MEMORY_RETRY_S", 0.01)
    start(rig)
    waiting = rig.wait(lambda e: e["type"] == "meeting.job.waiting")
    assert waiting["need_mb"] == 1100
    assert rig.finish()["diar_status"] == "failed"


@pytest.mark.parametrize("change", ["algorithm", "profile", "model"])
def test_incompatible_checkpoint_rejects_old_diar(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, change: str, tmp_path: Path
) -> None:
    install(rig, monkeypatch)
    rig.state.diar_factory = lambda *a, **kw: (_ for _ in ()).throw(OSError())
    start(rig)
    assert rig.finish()["diar_status"] == "failed"
    cp = rig.disk.read(MID, "checkpoint.json")
    assert isinstance(cp, Checkpoint)
    calls = rig.engine.calls
    if change == "algorithm":
        monkeypatch.setattr(diar_link, "DIAR_ALGORITHM", "test-new-algorithm")
    elif change == "profile":
        monkeypatch.setitem(
            diar_link.DIAR_PROFILES, "3dspeaker-campplus", diar_link.Profile(0.7, 0.4)
        )
    else:
        monkeypatch.setattr(
            meeting_transcribe, "model_fingerprint", lambda models: [{"sha": "new"}]
        )
    install(rig, monkeypatch)
    start(rig, resume=True)
    assert rig.finish()["diar_status"] == "done"
    assert rig.engine.calls > calls


@pytest.mark.parametrize("boundary", ["asr", "unloaded", "turns", "exited", "reconcile", "final"])
def test_resume_crash_at_each_durable_boundary(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    class Crash(BaseException):
        pass

    roles = install(rig, monkeypatch)
    original = MeetingWriter.write
    fired = False

    def crash(owner: Any, filename: str, document: Any) -> None:
        nonlocal fired
        original(owner, filename, document)
        d = document.data
        if filename != "checkpoint.json" or fired:
            return
        diar, rec = d["diar"], d["reconcile"]
        matches = {
            "asr": d["asr"]["status"] == "done" and diar["status"] == "pending",
            "unloaded": diar["status"] == "running" and not diar["turns"],
            "turns": diar["status"] == "running" and bool(diar["label_map"]),
            "exited": diar["status"] == "done" and rec["status"] == "pending",
            "reconcile": diar["status"] == "done" and rec["status"] == "done",
            "final": bool(rig.disk.read(MID, "transcript.json").labels_final),
        }
        if matches[boundary]:
            fired = True
            raise Crash

    with monkeypatch.context() as patch:
        patch.setattr(MeetingWriter, "write", crash)
        start(rig)
        assert rig.finish()["type"] == "meeting.job.failed"
    assert fired
    calls, count = rig.engine.calls, len(roles)
    start(rig, resume=True)
    assert rig.finish()["diar_status"] == "done"
    assert rig.engine.calls == calls
    assert len(roles) == count + (boundary in {"asr", "unloaded", "turns"})


def test_pause_boundaries_and_hard_caps() -> None:
    rows = [
        Utterance("a", start_ms=0, end_ms=1_180_000),
        Utterance("b", start_ms=1_181_000, end_ms=2_400_000),
    ]
    assert diar_blocks(rows, "mic", 2_400_000)[0] == (0, 1_181_000)
    continuous = [Utterance("a", start_ms=0, end_ms=14_400_000)]
    assert len(diar_blocks(continuous, "mic", 14_400_000)) == 12
    assert all(b - a <= 1_380_000 for a, b in diar_blocks(rows, "mic", 2_400_000))


def test_label_union_threshold_nearest_and_self_track() -> None:
    rows = [
        Utterance("a", track="far", start_ms=100, end_ms=1100),
        Utterance("b", track="far", start_ms=5000, end_ms=6000),
        Utterance("c", track="mic", start_ms=100, end_ms=900),
    ]
    turns = [
        DiarTurn("S2", 100, 250),
        DiarTurn("S2", 350, 500),
        DiarTurn("S1", 900, 1100),
        DiarTurn("S3", 6200, 6500),
    ]
    assign_speakers(rows, turns, "far")
    assert [u.speaker for u in rows] == ["S2", "S3", "me"]
    assign_speakers(rows, [], "mic")
    assert rows[2].speaker == "others"


@pytest.mark.parametrize("mode", ["missing", "duplicate", "gap", "trailing"])
def test_d13_full_assembly_required(monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    role = object.__new__(DiarProcess)
    role.identity = {"meeting_id": MID, "meeting_generation": 0}
    role.send = lambda *a, **kw: {"req": "b", "type": "diar.block", "block_s": 1, "expected_rtf": 1}  # type: ignore[method-assign]
    base = {**role.identity, "block_index": 0}
    part = {
        "type": "diar.block.part",
        **base,
        "seq": 0,
        "last": True,
        "segments": [[0, 1000, "S1"]],
    }
    done = {
        "type": "diar.block.done",
        **base,
        "req": "b",
        "request_type": "diar.block",
        "segments": [],
    }
    frames = {
        "missing": [done],
        "duplicate": [part, part, done],
        "gap": [{**part, "seq": 1}, done],
        "trailing": [{**part, "last": False}, done],
    }[mode]
    role.receive = lambda deadline: frames.pop(0)  # type: ignore[method-assign]
    with pytest.raises(ValueError, match="diar-parts"):
        role.block(np.zeros(16000, dtype="<f4"), 0, 0)


def test_sealed_pcm_and_block_deadline_formula() -> None:
    from astra_voice.worker.meeting_contract import operation_timeout

    fd = sealed_pcm(np.zeros(16000, dtype="<f4"))
    try:
        assert fcntl.fcntl(fd, 1034) & 0xF == 0xF
        with pytest.raises(OSError):
            os.write(fd, b"x")
    finally:
        os.close(fd)
    for seconds, rtf, expected in [(1, 1, 120), (1200, 0.1, 360), (1200, 1, 900)]:
        assert (
            operation_timeout({"type": "diar.block", "block_s": seconds, "expected_rtf": rtf})
            == expected
        )


def test_parent_deadline_is_absolute_and_cancellation_immediate() -> None:
    role = object.__new__(DiarProcess)
    role.cancelled = lambda: False
    role.overall_deadline = time.monotonic() - 1
    with pytest.raises(TimeoutError):
        role._check(time.monotonic() + 100)
    role.cancelled = lambda: True
    with pytest.raises(DiarCancelled):
        role._check(time.monotonic() + 100)


def test_subprocess_uncooperative_cancel_kills_group_and_reaps() -> None:
    # Actual process death evidence, not a mock claiming wait() happened.
    parent, child = socket.socketpair()
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        start_new_session=True,
        pass_fds=(child.fileno(),),
    )
    child.close()
    role = object.__new__(DiarProcess)
    role.process, role.connection = process, parent
    role.identity = {"meeting_id": MID, "meeting_generation": 0}
    role.serial, role.peak_rss_kb = 0, 0
    role.cancelled = lambda: True
    role.overall_deadline = float("inf")
    started = time.monotonic()
    try:
        assert not role.close()
        assert process.poll() == -signal.SIGKILL
        with pytest.raises(ChildProcessError):
            os.waitpid(process.pid, os.WNOHANG)
        assert time.monotonic() - started < 2
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def test_model_schema_accepts_new_statuses_and_legacy_asr() -> None:
    cp = Checkpoint(meeting_id=MID).to_dict()
    del cp["asr"]["status"]
    assert Checkpoint.from_dict(cp).asr.status == PhaseStatus.PENDING
    assert_plain(cp)
    transcript = Transcript(meeting_id=MID)
    assert json.loads(json.dumps(transcript.to_dict()))["labels_final"] is False


def peer_command(monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    peer = Path(__file__).resolve().parents[1] / "helpers/meeting_diar_peer.py"
    monkeypatch.setattr(
        meeting_diar_client,
        "worker_command",
        lambda fd, **kw: [sys.executable, str(peer), str(fd), mode],
    )


def test_subprocess_kill9_second_of_four_blocks_and_resume(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(rig, monkeypatch)
    rig.state.diar_factory = DiarProcess
    peer_command(monkeypatch, "kill-second")
    start(rig)
    first = rig.finish()
    assert first["diar_status"] == "failed"
    cp = rig.disk.read(MID, "checkpoint.json")
    assert isinstance(cp, Checkpoint) and cp.diar.status != PhaseStatus.DONE
    final = rig.disk.read(MID, "transcript.json")
    assert isinstance(final, Transcript)
    before = [(u.id, u.text) for u in final.utterances]
    calls = rig.engine.calls
    peer_command(monkeypatch, "normal")
    start(rig, resume=True)
    assert rig.finish()["diar_status"] == "done"
    assert rig.engine.calls == calls
    transcript = rig.disk.read(MID, "transcript.json")
    assert isinstance(transcript, Transcript)
    assert [(u.id, u.text) for u in transcript.utterances] == before
    progress = [e for e in rig.events if e["type"] == "meeting.job.progress" and "block" in e]
    assert progress[-1]["blocks"] == 4


def test_subprocess_block_deadline_and_confirmed_exit(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(rig, monkeypatch)
    peer_command(monkeypatch, "hang")
    rig.state.diar_factory = DiarProcess
    monkeypatch.setattr(meeting_diar_client, "operation_timeout", lambda request: 0.1)
    start(rig)
    assert rig.finish()["diar_status"] == "failed"
    assert rig.state.job is None or rig.state.job.diar_exited.is_set()
    with rig.disk.writer(MID, Writer.DIAR):
        pass


def test_subprocess_cooperative_cancel_during_block_and_delete_barrier(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(rig, monkeypatch)
    peer_command(monkeypatch, "hang")
    created = []
    entered = threading.Event()

    class TrackedDiar(DiarProcess):
        def receive(self, deadline: float) -> dict[str, Any]:
            event = super().receive(deadline)
            if event["type"] == "meeting.ack" and event.get("request_type") == "diar.block":
                entered.set()
            return event

    def factory(identity: Any, cancelled: Any, **kwargs: Any) -> DiarProcess:
        role = TrackedDiar(identity, cancelled, **kwargs)
        created.append(role)
        return role

    rig.state.diar_factory = factory
    start(rig)
    rig.wait(lambda e: e["type"] == "meeting.job.progress" and e["stage"] == "diar")
    assert entered.wait(5) and created
    assert rig.send("ping")[0]["type"] == "pong"
    assert rig.send("meeting.job.cancel", **rig.identity())[0]["type"] == "meeting.ack"
    rig.wait(lambda e: e["type"] == "meeting.job.cancelled")
    assert created[0].process.poll() == 0
    with rig.disk.writer(MID, Writer.DIAR):
        pass


def test_room_uses_mic_when_far_was_requested_but_is_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    disk = make_disk(tmp_path)
    write_track(disk, "mic", bursts([0.5, 0.6]))
    rig = Rig(disk)
    try:
        rig.load()
        install(rig, monkeypatch)
        start(rig)
        assert rig.finish()["diar_status"] == "done"
        final = disk.read(MID, "transcript.json")
        assert isinstance(final, Transcript) and all(u.speaker == "S1" for u in final.utterances)
    finally:
        rig.close()


def test_unconfirmed_unload_cannot_start_second_engine(rig: Rig) -> None:
    rig.engine._model = object()
    created = []
    rig.state.diar_factory = lambda *a, **kw: created.append(a)
    start(rig)
    assert rig.finish()["diar_status"] == "failed"
    assert not created and rig.state.engine is rig.engine
    delattr(rig.engine, "_model")


def test_waiting_memory_retries_in_same_job_then_completes(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    roles = install(rig, monkeypatch)
    original = rig.state._unload_ack

    def unload() -> dict[str, Any]:
        ack = original()
        rig.memory_mb = 0
        return ack

    rig.state._unload_ack = unload  # type: ignore[method-assign]
    monkeypatch.setattr(meeting_transcribe, "DIAR_MEMORY_RETRY_S", 0.01)
    start(rig)
    rig.wait(lambda e: e["type"] == "meeting.job.waiting")
    assert not roles
    rig.memory_mb = 100_000
    assert rig.finish()["diar_status"] == "done" and len(roles) == 1


def test_model_fingerprint_is_sensitive_to_bytes_not_just_directory(tmp_path: Path) -> None:
    from astra_voice.worker.meeting_diar_client import model_fingerprint

    models = {"segmentation_dir": str(tmp_path), "embedder_dir": str(tmp_path)}
    (tmp_path / "model.onnx").write_bytes(b"model-v1")
    (tmp_path / "3dspeaker_speech_campplus_sv_zh_en_16k-common_advanced.onnx").write_bytes(
        b"embedder-v1"
    )
    before = model_fingerprint(models)
    (tmp_path / "model.onnx").write_bytes(b"model-v2")
    assert model_fingerprint(models) != before
    assert_plain(before)
