"""T-404/405/454: durable D5, ACK credits, crash recovery; real files, fake ASR."""

from __future__ import annotations

import copy
import math
import threading
from pathlib import Path
from typing import Any

import pytest

from astra_voice.meetings import reconcile
from astra_voice.meetings.events import PhaseStatus
from astra_voice.meetings.model import (
    Checkpoint,
    DiarProgress,
    DiarTurn,
    ModelIdentity,
    Transcript,
    Utterance,
)
from astra_voice.meetings.store import MeetingStore, MeetingStoreError, MeetingWriter, Writer
from astra_voice.worker import ipc
from astra_voice.worker import meeting_transcribe as mt
from helpers.meeting_audio import MID, Rig, make_disk, tone, write_track

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("filesystem_euid")]


class Harness:
    def __init__(self, tmp_path: Path, *, existing: bool = False) -> None:
        self.disk = MeetingStore(tmp_path / "meetings") if existing else make_disk(tmp_path)
        if not existing:
            write_track(self.disk, "far", tone(50, 0.4))
        self.rig = Rig(self.disk)
        self.rig.load()
        self.state = self.rig.state
        self.job = mt._Job(
            {
                **self.rig.identity(),
                "limits": {"max_blocks": 12},
                "diarization": True,
                "resume": True,
                "tracks": ["far"],
            }
        )
        self.state.job = self.job
        self.job.credits = 64
        self.rows = [
            Utterance("source", track="far", start_ms=1000, end_ms=3000, text="первый второй"),
            Utterance("filler", seq=1, track="far", start_ms=5000, end_ms=25000, text="фон"),
            Utterance("filler2", seq=2, track="far", start_ms=26000, end_ms=46000, text="фон"),
        ]
        self.durable = mt._Durable("0" * 64, "1" * 64, ModelIdentity(), 9000, 0)
        self.durable.asr_status = PhaseStatus.DONE
        self.durable.diar = DiarProgress(
            status=PhaseStatus.DONE,
            turns=[DiarTurn("S1", 0, 2000), DiarTurn("S2", 2000, 50000)],
            label_map={"S1": "S1", "S2": "S2"},
        )
        self.cursors = {"mic": 0, "far": 50000}
        self.events: list[dict[str, Any]] = []
        self.calls: list[int] = []
        self.ack = True
        self.serial = 0
        self.state._recognize = self.recognize  # type: ignore[method-assign]
        self.state._emit = self.emit  # type: ignore[method-assign, assignment]

    def recognize(self, job: Any, audio: Any) -> str:
        self.calls.append(len(audio) // 16)
        return ["чужой первый чужой", "первый", "чужой второй чужой", "второй"][
            (len(self.calls) - 1) % 4
        ]

    def emit(self, job: Any, event: dict[str, Any]) -> None:
        ipc.encode(event, role=mt.ROLE)
        self.events.append(copy.deepcopy(event))
        if event["type"] == "meeting.job.replace" and self.ack:
            self.receipt(event)

    def receipt(self, event: dict[str, Any], **override: Any) -> list[dict[str, Any]]:
        self.serial += 1
        return self.state.handle(
            {
                "type": "meeting.job.applied",
                "request_type": "meeting.job.applied",
                "req": f"ack{self.serial}",
                **self.rig.identity(),
                "seq": event["seq"],
                "revision": event["expected_revision"] + 1,
                **override,
            }
        )

    def run(self, *, resume: bool = False, publish: bool = False) -> None:
        with self.disk.writer(MID, Writer.TRANSCRIBE) as owner:
            if resume:
                r = self.state._resume(self.job, owner, "0" * 64, "1" * 64, ModelIdentity())
                assert not r.fresh and r.checkpoint is not None
                self.rows = r.kept
                self.durable.revision = r.revision
                self.durable.reconcile = copy.deepcopy(r.checkpoint.reconcile)
                self.durable.diar = copy.deepcopy(r.checkpoint.diar)
            else:
                self.state._persist(self.job, owner, self.durable, self.rows, self.cursors)
            from astra_voice.security.files import private_directory

            with private_directory(
                self.disk.root / MID / "audio", private_from=self.disk.root
            ) as fd:
                reader = mt._Reader(fd)
                try:
                    spans, _, _, _ = self.state._inventory(self.job, reader)
                    if publish:
                        self.state._post_asr(
                            self.job,
                            owner,
                            reader,
                            spans,
                            self.rows,
                            self.cursors,
                            self.durable,
                            50000,
                            self.state.clock(),
                            0,
                        )
                    else:
                        self.state._reconcile(
                            self.job,
                            owner,
                            self.rows,
                            self.cursors,
                            self.durable,
                            reader,
                            spans,
                            "far",
                            math.inf,
                        )
                finally:
                    reader.close()

    def close(self) -> None:
        self.rig.close()


@pytest.fixture
def harness(tmp_path: Path) -> Any:
    h = Harness(tmp_path)
    yield h
    h.close()


def test_split_context_budget_and_durable_order(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = harness
    writes = []
    original = MeetingWriter.write

    def capture(owner: Any, name: str, data: Any) -> None:
        original(owner, name, data)
        writes.append((name, copy.deepcopy(data.data)))

    monkeypatch.setattr(MeetingWriter, "write", capture)
    h.run()
    assert [u.text for u in h.rows] == ["первый", "второй", "фон", "фон"]
    assert [u.speaker for u in h.rows[:2]] == ["S1", "S2"]
    assert [u.start_ms for u in h.rows[:2]] == [1000, 2000]
    assert h.calls == [1600, 1000, 1600, 1000]
    p = h.durable.reconcile
    assert p.used_ms == 5200 and p.budget_ms == 6300
    assert p.done_ids == p.replaced_ids == ["source"]
    assert h.job.credits == 64
    assert len({u.id for u in h.rows}) == 4
    assert [u.seq for u in h.rows] == list(range(4))
    from test_meeting_transcribe_diar import assert_plain

    for event in h.events:
        assert_plain(event)
    for i, (name, data) in enumerate(writes):
        assert_plain(data)
        if name == "checkpoint.json":
            assert writes[i - 1][0] == "transcript.json"
            assert writes[i - 1][1]["transcript_revision"] == data["transcript_revision"]
            assert writes[i - 1][1]["reconcile"] == data["reconcile"]


@pytest.mark.parametrize("budget", [5199, 5200, 5201])
def test_exact_budget_includes_both_context_and_core(harness: Harness, budget: int) -> None:
    h = harness
    p = h.durable.reconcile
    p.status = PhaseStatus.RUNNING
    p.speech_ms, p.budget_ms = 42000, budget
    h.run()
    assert p.used_ms == (5200 if budget >= 5200 else 0)
    assert h.rows[0].multiple_speakers is (budget < 5200)
    assert p.used_ms <= p.budget_ms


def test_lost_ack_same_seq_one_credit_and_conflicts(harness: Harness) -> None:
    h = harness
    h.ack = False
    result: list[BaseException] = []

    def run() -> None:
        try:
            h.run()
        except BaseException as exc:
            result.append(exc)

    task = threading.Thread(target=run)
    task.start()
    import time

    until = time.monotonic() + 5
    while len([e for e in h.events if e["type"] == "meeting.job.replace"]) < 2:
        assert time.monotonic() < until
        time.sleep(0.01)
    replacements = [e for e in h.events if e["type"] == "meeting.job.replace"]
    assert replacements[0] == replacements[1]
    event = replacements[0]
    assert h.job.credits == 63
    assert h.receipt(event, revision=event["expected_revision"] + 2)[0]["code"] == "req-conflict"
    assert h.receipt(event, seq=event["seq"] + 1)[0]["code"] == "req-conflict"
    assert h.job.credits == 63
    h.receipt(event)
    h.receipt(event)
    task.join(5)
    assert not task.is_alive() and not result
    assert h.job.credits == 64
    assert len(h.rows) == 4


@pytest.mark.parametrize(
    "boundary",
    [
        "before-reserve",
        "after-reserve-text",
        "before-text",
        "after-text",
        "after-checkpoint",
        "after-replace",
    ],
)
def test_crash_boundaries_resume_without_diar_or_budget_reset(
    harness: Harness, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    h = harness
    original_write = h.state._write
    original_emit = h.state._emit

    def write(job: Any, owner: Any, name: str, data: Any) -> None:
        p = data.reconcile
        reserved = p.inflight_id == "source"
        committed = "source" in p.done_ids
        if (
            (boundary == "before-reserve" and name == "transcript.json" and reserved)
            or (boundary == "after-reserve-text" and name == "checkpoint.json" and reserved)
            or (boundary == "before-text" and name == "transcript.json" and committed)
            or (boundary == "after-text" and name == "checkpoint.json" and committed)
        ):
            raise MeetingStoreError("crash")
        original_write(job, owner, name, data)
        if boundary == "after-checkpoint" and name == "checkpoint.json" and committed:
            raise MeetingStoreError("crash")

    def emit(job: Any, event: Any) -> None:
        if boundary == "after-replace" and event["type"] == "meeting.job.replace":
            original_emit(job, event)
            raise MeetingStoreError("crash")
        original_emit(job, event)

    with monkeypatch.context() as patch:
        patch.setattr(h.state, "_write", write)
        patch.setattr(h.state, "_emit", emit)
        with pytest.raises(MeetingStoreError):
            h.run()
    before = h.disk.read(MID, "transcript.json")
    assert isinstance(before, Transcript)
    used = before.reconcile.used_ms
    calls = len(h.calls)
    h.job.replacements.clear()

    def no_diar(*args: Any) -> None:
        raise AssertionError("diar restarted after D5 crash")

    monkeypatch.setattr(h.state, "_diarize", no_diar)
    monkeypatch.setattr(h.state, "_finish", lambda *args: None)
    h.run(resume=True, publish=True)
    final = h.disk.read(MID, "transcript.json")
    cp = h.disk.read(MID, "checkpoint.json")
    assert isinstance(final, Transcript) and isinstance(cp, Checkpoint)
    assert final.reconcile.budget_ms == cp.reconcile.budget_ms == 6300
    assert final.reconcile.used_ms >= used
    assert final.reconcile.done_ids == ["source"]
    assert final.reconcile.used_ms == 5200
    assert len({u.id for u in final.utterances}) == len(final.utterances)
    assert h.durable.diar.status == PhaseStatus.DONE
    if boundary != "before-reserve":
        assert len(h.calls) == calls  # charged work is never repeated
    if boundary in {"after-reserve-text", "before-text"}:
        assert final.utterances[0].text == "первый второй"
        assert final.utterances[0].multiple_speakers
    else:
        assert [u.text for u in final.utterances[:2]] == ["первый", "второй"]


@pytest.mark.parametrize("failure", ["empty", "join", "different", "memory", "deadline"])
def test_fallback_preserves_original(
    harness: Harness, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    h = harness
    if failure == "memory":
        monkeypatch.setattr(
            h.state, "_ensure_model", lambda *a, **kw: (_ for _ in ()).throw(MemoryError())
        )
    else:

        def recognize(job: Any, audio: Any) -> str:
            if failure == "deadline":
                raise mt._Failed("limit")
            if failure == "empty":
                return ""
            return "первый" if failure == "join" else "совершенно другое"

        monkeypatch.setattr(h.state, "_recognize", recognize)
    h.run()
    assert h.rows[0].id == "source" and h.rows[0].text == "первый второй"
    assert h.rows[0].multiple_speakers
    assert h.durable.reconcile.failed
    assert h.durable.diar.status == PhaseStatus.DONE


def test_d4_reload_only_after_diar_exit_and_memory_budget(harness: Harness) -> None:
    h = harness
    h.state._unload_ack()
    assert h.state.engine is None and h.job.diar_exited.is_set()
    h.rig.memory_mb = 900  # ASR 700 + margin 200; no extra diar reservation
    h.run()
    assert h.rig.engine.loads == 2
    assert len(h.rows) == 4


def test_frame_overflow_falls_back_without_fragmenting(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = harness
    monkeypatch.setattr(h.state, "_recognize", lambda *args: "я" * 16300)
    monkeypatch.setattr(reconcile, "trusted", lambda *args: True)
    h.run()
    assert len(h.rows) == 3 and h.rows[0].multiple_speakers
    frames = [e for e in h.events if e["type"] == "meeting.job.replace"]
    assert len(frames) == 1 and len(frames[0]["items"]) == 1
    assert len(ipc.encode(frames[0], role=mt.ROLE)) <= 65536 + 4


def test_cancel_during_d5_has_no_later_write(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = harness
    snapshot: list[bytes] = []

    def cancel(job: Any, audio: Any) -> str:
        job.cancel()
        snapshot.append((h.disk.root / MID / "transcript.json").read_bytes())
        raise mt._Cancelled

    monkeypatch.setattr(h.state, "_recognize", cancel)
    with pytest.raises(mt._Cancelled):
        h.run()
    assert (h.disk.root / MID / "transcript.json").read_bytes() == snapshot[0]
    assert not [e for e in h.events if e["type"] == "meeting.job.replace"]
    assert h.state._fenced(h.job)


def _kill_child(root: Path, boundary: str, notify_fd: int) -> None:
    """Real SIGKILL harness; invoked only by the subprocess tests below."""
    import os
    import signal

    h = Harness(root)
    original_write = h.state._write
    original_emit = h.state._emit

    def stop() -> None:
        os.write(notify_fd, b"ready")
        signal.pause()

    def write(job: Any, owner: Any, name: str, data: Any) -> None:
        reserved = data.reconcile.inflight_id == "source"
        committed = "source" in data.reconcile.done_ids
        if (boundary == "before-text" and name == "transcript.json" and committed) or (
            boundary == "after-text" and name == "checkpoint.json" and committed
        ):
            stop()
        original_write(job, owner, name, data)
        if (boundary == "reserved" and name == "checkpoint.json" and reserved) or (
            boundary == "checkpoint" and name == "checkpoint.json" and committed
        ):
            stop()

    def emit(job: Any, event: Any) -> None:
        if boundary == "replace" and event["type"] == "meeting.job.replace":
            h.ack = False
            original_emit(job, event)
            stop()
        original_emit(job, event)

    h.state._write = write  # type: ignore[method-assign, assignment]
    h.state._emit = emit  # type: ignore[method-assign, assignment]
    h.run()


@pytest.mark.parametrize(
    "boundary", ["reserved", "before-text", "after-text", "checkpoint", "replace"]
)
def test_subprocess_sigkill_d5_resume(tmp_path: Path, boundary: str) -> None:
    """No EPERM skip: run outside the Codex sandbox; kill/reap only our PID."""
    import os
    import select
    import signal
    import subprocess
    import sys

    read_fd, write_fd = os.pipe()
    repo = Path(__file__).resolve().parents[2]
    paths = [str(repo / "src"), str(repo / "tests"), str(repo / "tests/unit")]
    code = (
        "import sys; from pathlib import Path; "
        f"sys.path[:0] = {paths!r}; "
        "from test_meeting_transcribe_reconcile import _kill_child; "
        f"_kill_child(Path({str(tmp_path)!r}), {boundary!r}, {write_fd})"
    )
    process = subprocess.Popen([sys.executable, "-c", code], pass_fds=(write_fd,))
    os.close(write_fd)
    try:
        assert select.select([read_fd], [], [], 20)[0], "child did not reach D5 boundary"
        assert os.read(read_fd, 5) == b"ready"
        os.kill(process.pid, signal.SIGKILL)
        assert process.wait(timeout=5) == -signal.SIGKILL
        h = Harness(tmp_path, existing=True)
        try:
            h.run(resume=True)
            assert h.durable.reconcile.used_ms == 5200
            assert h.durable.reconcile.budget_ms == 6300
            assert h.durable.reconcile.done_ids == ["source"]
            assert not h.calls
            assert len({u.id for u in h.rows}) == len(h.rows)
            assert h.durable.diar.status == PhaseStatus.DONE
        finally:
            h.close()
    finally:
        os.close(read_fd)
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def test_full_pipeline_reload_replace_and_publish(tmp_path: Path) -> None:
    """D4/D5 complete job, production VAD/windows/store/credits and fake engines."""
    from astra_voice.worker.engine import TranscribeResult
    from helpers.meeting_audio import FakeEngine, bursts

    class Engine(FakeEngine):
        d5_calls = 0

        def transcribe(self, audio: Any, cancel: Any) -> TranscribeResult:
            self.calls += 1
            if not self.unloads:
                return TranscribeResult("первый второй", 0, False)
            text = ["контекст первый контекст", "первый", "контекст второй контекст", "второй"][
                self.d5_calls % 4
            ]
            self.d5_calls += 1
            return TranscribeResult(text, 0, False)

    class Diar:
        peak_rss_kb = 1
        overall_deadline = math.inf

        def load(self, models: Any) -> None:
            assert rig.state.engine is None and engine.unloads == 1

        def block(self, audio: Any, index: int, offset: int) -> list[list[Any]]:
            return [[0, 2000, "S1"], [2000, len(audio) // 16, "S2"]]

        def finish(self) -> dict[str, Any]:
            return {"label_map": {"S1": "S1", "S2": "S2"}, "speakers_total": 2}

        def close(self, *, completed: bool = False) -> bool:
            return completed

    disk = make_disk(tmp_path)
    write_track(disk, "far", bursts([0.4] * 20, on=2, off=0.6))
    engine = Engine()
    rig = Rig(disk, engine=engine)
    rig.state.diar_factory = lambda *args, **kw: Diar()
    original_emit = rig.state._emit

    def emit(job: Any, event: Any) -> None:
        if event["type"] == "meeting.job.replace":
            cp = disk.read(MID, "checkpoint.json")
            text = disk.read(MID, "transcript.json")
            assert isinstance(cp, Checkpoint) and isinstance(text, Transcript)
            assert (
                cp.transcript_revision == text.transcript_revision == event["expected_revision"] + 1
            )
            assert cp.diar.status == PhaseStatus.DONE
            rig.send(
                "meeting.job.applied",
                **rig.identity(),
                seq=event["seq"],
                revision=text.transcript_revision,
            )
        original_emit(job, event)

    rig.state._emit = emit  # type: ignore[method-assign, assignment]
    try:
        from test_meeting_transcribe_diar import start

        rig.load()
        start(rig, tracks=["far"])
        done = rig.finish()
        assert done["type"] == "meeting.job.done" and done["diar_status"] == "done"
        assert not done["reconcile_failed"]
        final = disk.read(MID, "transcript.json")
        assert isinstance(final, Transcript) and final.labels_final
        assert len(final.utterances) == len(rig.utterances()) + 1
        assert [u.text for u in final.utterances[:2]] == ["первый", "второй"]
        assert final.reconcile.used_ms <= final.reconcile.budget_ms
        assert engine.loads == 2 and engine.d5_calls == 4
        assert not (disk.root / MID / "checkpoint.json").exists()
    finally:
        rig.close()


@pytest.mark.parametrize("mic_start", [1500, 2000])
@pytest.mark.parametrize("crash", [False, True])
def test_round2_two_track_order_and_pending_resume(
    harness: Harness, monkeypatch: pytest.MonkeyPatch, mic_start: int, crash: bool
) -> None:
    from astra_voice.core.meeting_jobs import MeetingJobs, ModelAdmission, _Active
    from astra_voice.meetings.model import Meeting

    h = harness
    h.rows[0].start_ms, h.rows[0].end_ms = 0, 4000
    h.rows.insert(
        1, Utterance("mic", track="mic", start_ms=mic_start, end_ms=2500, text="микрофон")
    )
    for seq, item in enumerate(h.rows):
        item.seq = seq
    # Fix a generous already-initialized budget to isolate interleaving from D5 admission.
    h.durable.reconcile.status = PhaseStatus.RUNNING
    h.durable.reconcile.speech_ms = 100000
    h.durable.reconcile.budget_ms = 15000
    meeting = h.disk.read(MID, "meeting.json")
    assert isinstance(meeting, Meeting)
    jobs = MeetingJobs(h.disk, lambda *args: None, submit=lambda operation: operation())  # type: ignore[arg-type]
    active = _Active(meeting, ModelAdmission("ok"), resume=True, phase="running")
    jobs._active = active
    active.utterances = {u.seq: u.to_dict() for u in h.rows}
    original_emit = h.state._emit

    def emit(job: Any, event: Any) -> None:
        if event["type"] == "meeting.job.labels":
            active.transcript_revision = event.get(
                "transcript_revision", active.transcript_revision
            )
        elif event["type"] == "meeting.job.replace":
            assert jobs._replace(active, event)
        original_emit(job, event)

    monkeypatch.setattr(h.state, "_emit", emit)
    if crash:
        write = h.state._write

        def interrupt(job: Any, owner: Any, name: str, document: Any) -> None:
            if name == "checkpoint.json" and document.reconcile.pending is not None:
                raise MeetingStoreError("crash between text and checkpoint")
            write(job, owner, name, document)

        with monkeypatch.context() as patch:
            patch.setattr(h.state, "_write", interrupt)
            with pytest.raises(MeetingStoreError):
                h.run()
        committed = h.disk.read(MID, "transcript.json")
        assert isinstance(committed, Transcript)
        # Pending parts may have a microphone reply between them on disk.
        assert [u.start_ms for u in committed.utterances[:3]] == [0, mic_start, 2000]
        calls = len(h.calls)
        jobs._restore_live(active)
        h.run(resume=True)
        assert len(h.calls) == calls
    else:
        h.run()
    final = h.disk.read(MID, "transcript.json")
    assert isinstance(final, Transcript)
    snapshot = jobs.job_snapshot(MID)
    assert snapshot is not None
    expected = [(0, "far"), (mic_start, "mic"), (2000, "far"), (5000, "far"), (26000, "far")]
    assert [(u.start_ms, u.track) for u in final.utterances] == expected
    assert [(u["start_ms"], u["track"]) for u in snapshot["utterances"]] == expected
    assert [u.seq for u in final.utterances] == list(range(5))
    assert [u["seq"] for u in snapshot["utterances"]] == list(range(5))
    assert [u.id for u in final.utterances] == [u["id"] for u in snapshot["utterances"]]
