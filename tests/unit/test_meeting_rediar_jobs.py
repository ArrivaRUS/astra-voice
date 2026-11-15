"""5в-4: queue redo lifecycle, durable fault injection and synthetic supervisor."""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from test_meeting_jobs import MID, MID2, Rig

from astra_voice.core import meeting_jobs
from astra_voice.core.meeting_jobs import ModelAdmission
from astra_voice.meetings import model, store
from astra_voice.meetings.events import MeetingState, PhaseStatus, ProcessingStatus

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("filesystem_euid")]
PAIR = {"segmentation_dir": "/models/seg", "embedder_dir": "/models/emb"}
WORK = "rediar-transcript.json"


class RedoRig(Rig):
    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.pair: dict[str, str] | None = dict(PAIR)
        self.allowed = True
        self.wall = datetime(2026, 11, 12, tzinfo=UTC)
        self.jobs.diar_models = lambda: self.pair
        self.jobs.admit_allowed = lambda: self.allowed
        self.jobs.wall_clock = lambda: self.wall
        self.create(
            state=MeetingState.DONE,
            meeting_generation=1,
            processing_status=ProcessingStatus.SUCCEEDED,
        )
        self.store.write(MID, "transcript.json", self.text(1), writer=store.Writer.TRANSCRIBE)
        self.store.write(
            MID,
            "speakers.json",
            model.SpeakerMap(labels={"S1": model.Speaker(name="Анна")}),
            writer=store.Writer.GUI,
        )
        current = self.meeting()
        current.transcription.diar_status = "done"
        self.store.write(MID, "meeting.json", current, writer=store.Writer.GUI)
        self.before = self.published()

    @staticmethod
    def text(generation: int, *, final: bool = True, partial: bool = False) -> model.Transcript:
        return model.Transcript(
            MID,
            meeting_generation=generation,
            transcript_revision=generation + 10,
            labels_final=final,
            utterances=[
                model.Utterance("u1", seq=0, speaker="S1", text="Прежний текст", final=final)
            ],
            reconcile=model.ReconcileProgress(
                status=PhaseStatus.DONE if final else PhaseStatus.PENDING, failed=partial
            ),
        )

    def published(self) -> tuple[bytes, bytes]:
        return tuple(
            (self.store.root / MID / name).read_bytes()
            for name in ("transcript.json", "speakers.json")
        )  # type: ignore[return-value]

    def start(self) -> None:
        self.jobs.rediarize(MID)
        self.jobs.tick()
        self.loaded()
        self.store.write(MID, WORK, self.text(2, final=False), writer=store.Writer.TRANSCRIBE)
        self.accepted()
        assert self.jobs.job_snapshot(MID)["utterances"][0]["id"] == "u1"  # type: ignore[index]

    def finish_text(self, *, partial: bool = False) -> None:
        text = self.text(2, partial=partial)
        text.transcript_revision += 1
        self.store.write(MID, WORK, text, writer=store.Writer.TRANSCRIBE)

    def redo_done(self, status: str = "done") -> None:
        self.job_event("meeting.job.done", diar_status=status, wall_ms=100, peak_rss_kb=55)

    def confirm_exit(self) -> None:
        self.role.exited = True
        self.jobs.tick()

    def unload(self) -> None:
        request = self.role.last("model.unload")
        self.role.emit("model.unloaded", req=request["req"])


@pytest.fixture
def rig(tmp_path: Path) -> RedoRig:
    return RedoRig(tmp_path)


def test_enqueue_only_metadata_and_idempotent_request(rig: RedoRig) -> None:
    before = rig.meeting().to_dict()
    rig.jobs.rediarize(MID)
    rig.jobs.rediarize(MID)
    after = rig.meeting().to_dict()
    assert set(k for k in before if before[k] != after[k]) == {
        "rediar",
        "transcript_generation",
        "meeting_generation",
    }
    assert after["meeting_generation"] == 2
    assert rig.published() == rig.before
    assert not (rig.store.root / MID / WORK).exists()
    assert rig.jobs.names_busy(MID)
    rig.jobs.transcribe(MID)
    assert rig.meeting().state == MeetingState.DONE
    assert rig.meeting().meeting_generation == 2


@pytest.mark.parametrize(
    "change", ["no-audio", "cleanup", "wrong-state", "text-generation", "unfinal"]
)
def test_enqueue_checks_disk_without_changes(rig: RedoRig, change: str) -> None:
    meeting = rig.meeting()
    if change == "no-audio":
        meeting.audio.present = False
    elif change == "cleanup":
        meeting.cleanup_pending = True
    elif change == "wrong-state":
        meeting.state = MeetingState.RECORDED
    elif change == "text-generation":
        meeting.meeting_generation = 5
    else:
        text = rig.text(1, final=False)
        text.transcript_revision += 1
        rig.store.write(MID, "transcript.json", text, writer=store.Writer.TRANSCRIBE)
    (rig.store.root / MID / "meeting.json").write_text(json.dumps(meeting.to_dict()))
    before = rig.meeting().to_dict()
    rig.jobs.rediarize(MID)
    assert rig.meeting().to_dict() == before
    assert not rig.jobs.busy


@pytest.mark.parametrize("wait_memory", [False, True])
def test_cancel_queued_and_memory_independent_of_gates(rig: RedoRig, wait_memory: bool) -> None:
    rig.jobs.rediarize(MID)
    if wait_memory:
        rig.memory = 0
        before = (rig.store.root / MID / "meeting.json").read_bytes()
        rig.jobs.tick()
        assert rig.jobs.redo_snapshot(MID)["status"] == "waiting_memory"
        assert (rig.store.root / MID / "meeting.json").read_bytes() == before
    rig.allowed = False
    rig.pair = None
    rig.admission = ModelAdmission("missing")
    rig.jobs.cancel(MID)
    assert rig.meeting().rediar is None
    assert rig.meeting().state == MeetingState.DONE
    assert not rig.meeting().cancelled
    assert rig.jobs.redo_results[MID] == "cancelled"
    assert rig.published() == rig.before
    assert not rig.jobs.busy
    assert not rig.roles and not rig.notices


@pytest.mark.parametrize("stage", ["diar", "between", "reconcile"])
def test_cancel_at_stages_is_durable_before_command_and_drops_done(
    rig: RedoRig, stage: str
) -> None:
    rig.start()
    rig.job_event("meeting.job.progress", stage=stage, done_ms=5, total_ms=10)
    assert rig.jobs.job_snapshot(MID)["stage"] == stage  # type: ignore[index]
    assert rig.jobs.percent is None
    original_send = rig.role.send

    def send(message: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        if message["type"] == "meeting.job.cancel":
            assert rig.meeting().rediar is None
        return original_send(message, **kwargs)

    rig.role.send = send  # type: ignore[method-assign]
    rig.jobs.cancel(MID)
    rig.redo_done()
    rig.job_event("meeting.job.labels", items=[["u1", "S2"]], transcript_revision=99)
    assert rig.meeting().rediar is None
    assert rig.published() == rig.before
    assert (rig.store.root / MID / WORK).exists()
    req = rig.role.last("meeting.job.cancel")["req"]
    rig.role.emit("meeting.job.cancelled", req=req)
    assert rig.role.kills == 1
    assert (rig.store.root / MID / WORK).exists()  # ACK alone never proves process exit
    rig.confirm_exit()
    assert not (rig.store.root / MID / WORK).exists()
    assert rig.jobs.redo_results[MID] == "cancelled"
    rig.redo_done()
    assert rig.published() == rig.before
    assert not rig.notices


@pytest.mark.parametrize("original_failed", [False, True])
@pytest.mark.parametrize("failure", ["diar", "io-before-accepted", "io-after-accepted"])
def test_failure_keeps_published_names(rig: RedoRig, original_failed: bool, failure: str) -> None:
    if original_failed:
        meeting = rig.meeting()
        meeting.transcription.diar_status = "failed"
        meeting.processing_status = ProcessingStatus.PARTIAL
        rig.store.write(MID, "meeting.json", meeting, writer=store.Writer.GUI)
    if failure == "io-before-accepted":
        rig.jobs.rediarize(MID)
        rig.jobs.tick()
        rig.loaded()
    else:
        rig.start()
    if failure == "diar":
        rig.redo_done("failed")
    else:
        rig.job_event("meeting.job.failed", code="io")
    rig.confirm_exit()
    assert rig.meeting().rediar is None
    assert rig.meeting().processing_status == (
        ProcessingStatus.PARTIAL if original_failed else ProcessingStatus.SUCCEEDED
    )
    assert rig.jobs.redo_results[MID] == ("failed-again" if original_failed else "redo-failed")
    assert rig.published() == rig.before
    assert rig.audio_present() and not rig.notices


def test_fourth_crash_aborts_but_first_three_resume(rig: RedoRig) -> None:
    rig.start()
    for _ in range(3):
        rig.role.crash()
        rig.jobs.tick()
        assert rig.meeting().rediar is not None
        rig.loaded()
        assert rig.role.last("meeting.job.start")["resume"]
        assert rig.role.last("meeting.job.start")["operation"] == "rediarize"
        rig.accepted()
    rig.role.crash()
    rig.jobs.tick()
    assert rig.jobs.redo_results[MID] == "redo-failed"
    assert rig.meeting().rediar is None
    assert rig.published() == rig.before


@pytest.mark.parametrize("gate", ["pair", "disabled", "asr"])
def test_admission_revoked_since_click_never_starts_role(rig: RedoRig, gate: str) -> None:
    rig.jobs.rediarize(MID)
    if gate in {"pair", "disabled"}:
        rig.pair = None
    else:
        rig.admission = ModelAdmission("missing")
    rig.jobs.tick()
    assert not rig.roles
    assert rig.meeting().rediar is None
    assert rig.jobs.redo_results[MID] == "redo-failed"
    assert rig.published() == rig.before


@pytest.mark.parametrize("memory", [False, True])
def test_expired_audio_is_held_while_rediar(rig: RedoRig, memory: bool) -> None:
    rig.jobs.rediarize(MID)
    if memory:
        rig.memory = 0
        rig.jobs.tick()
    meeting = rig.meeting()
    meeting.transcription.finished_at = "2026-10-01T00:00:00+00:00"
    meeting.audio.retain_days = 1
    rig.store.write(MID, "meeting.json", meeting, writer=store.Writer.GUI)
    rig.jobs.start_retention()
    assert rig.audio_present()
    assert not rig.meeting().cleanup_pending
    assert rig.meeting().rediar is not None


def test_delete_during_redo_confirms_exit_before_purge(rig: RedoRig) -> None:
    rig.start()
    rig.jobs.delete_and_purge(MID)
    assert rig.meeting().state == MeetingState.DELETING
    rig.redo_done()
    assert (rig.store.root / MID).exists()
    rig.now += 2
    rig.jobs.tick()
    assert rig.role.kills
    rig.confirm_exit()
    assert not (rig.store.root / MID).exists()
    rig.redo_done()
    assert not (rig.store.root / MID).exists()


@pytest.mark.parametrize("partial", [False, True])
def test_success_publishes_and_finalizes_once(rig: RedoRig, partial: bool) -> None:
    rig.start()
    assert rig.published() == rig.before
    rig.finish_text(partial=partial)
    rig.redo_done()
    assert rig.meeting().processing_status == (
        ProcessingStatus.PARTIAL if partial else ProcessingStatus.SUCCEEDED
    )
    assert rig.meeting().rediar is None and rig.meeting().transcript_generation is None
    assert rig.store.read(MID, "speakers.json").labels == {}
    assert rig.store.read(MID, "speakers.json").transcript_generation == 2
    assert rig.meeting().transcription.finished_at == rig.wall.isoformat()
    before = (rig.store.root / MID / "meeting.json").read_bytes()
    rig.wall += timedelta(days=5)
    rig.redo_done()
    rig.jobs.cancel(MID)  # already delivered done, no active redo
    rig.jobs.tick()
    assert (rig.store.root / MID / "meeting.json").read_bytes() == before
    assert rig.jobs.redo_results[MID] == "done"
    assert not rig.notices


@pytest.mark.parametrize("restart", [False, True])
def test_divide_without_models_then_installed_without_speaker_map(
    rig: RedoRig, restart: bool
) -> None:
    rig.retention = 7
    (rig.store.root / MID / "speakers.json").unlink()
    meeting = rig.meeting()
    meeting.transcription.diarization = False
    meeting.transcription.diar_status = "skipped"
    rig.store.write(MID, "meeting.json", meeting, writer=store.Writer.GUI)
    rig.pair = None
    rig.jobs.rediarize(MID)
    assert rig.meeting().rediar is None
    rig.pair = dict(PAIR)
    rig.start()
    rig.finish_text()
    if restart:
        # A restart classifies the durable final work via _probe_redo.
        rig.jobs.shutdown()
        rig.jobs = meeting_jobs.MeetingJobs(
            rig.store,
            rig.factory,
            audio_retention_days=lambda: 7,
            wall_clock=lambda: rig.wall,
            submit=lambda task: task(),
        )
        rig.jobs.start_retention()
        rig.jobs.tick()
    else:
        rig.redo_done()
    assert rig.jobs.redo_results[MID] == "done"
    assert not rig.jobs.recovery_errors and not rig.jobs.names_busy(MID)
    assert rig.meeting().rediar is None
    assert rig.store.read(MID, "transcript.json").meeting_generation == 2
    assert rig.store.read(MID, "speakers.json") == model.SpeakerMap(transcript_generation=2)
    assert rig.meeting().transcription.finished_at == rig.wall.isoformat()
    assert rig.meeting().audio.retain_days == 7 and rig.meeting().audio.present
    rig.wall += timedelta(days=7)
    rig.jobs._retention_pass()
    assert not rig.meeting().audio.present


@pytest.mark.parametrize("lease", [store.Writer.DIAR, store.Writer.TRANSCRIBE, store.Writer.GUI])
def test_publish_busy_retries_with_backoff_and_other_jobs_run(
    rig: RedoRig, lease: store.Writer, monkeypatch: Any
) -> None:
    rig.start()
    rig.finish_text()
    calls = []
    original = store.MeetingWriter.rediar_publish

    def publish(writer: store.MeetingWriter, generation: int) -> store.RediarPublishResult:
        calls.append(generation)
        return original(writer, generation)

    monkeypatch.setattr(store.MeetingWriter, "rediar_publish", publish)
    with rig.store.writer(MID, lease):
        rig.redo_done()
        assert rig.jobs.names_busy(MID)
        assert rig.jobs.busy
        assert rig.jobs.redo_snapshot(MID)["status"] == "committing"
        assert rig.jobs.redo_snapshot(MID)["percent"] == 100
        for _ in range(50):
            rig.jobs.tick()
        assert len(calls) == (0 if lease == store.Writer.GUI else 1)
        assert rig.meeting().rediar is not None
        rig.jobs.cancel(MID)
        assert rig.meeting().rediar is not None
    rig.unload()
    rig.create(MID2)
    rig.jobs.transcribe(MID2)
    rig.jobs.tick()
    assert rig.jobs.active_id == MID2
    rig.now += meeting_jobs.ADMIT_RETRY_S
    rig.jobs.tick()
    assert rig.meeting().rediar is None
    assert rig.jobs.redo_results[MID] == "done"


@pytest.mark.parametrize("kind", ["missing", "unfinished", "invalid"])
def test_done_without_final_file_is_failure_not_endless_wait(rig: RedoRig, kind: str) -> None:
    rig.start()
    path = rig.store.root / MID / WORK
    if kind == "missing":
        path.unlink()
    elif kind == "invalid":
        path.write_bytes(b"{}")
    rig.redo_done()
    assert rig.meeting().rediar is None
    assert rig.jobs.redo_results[MID] == "redo-failed"
    assert not rig.jobs.names_busy(MID)
    assert rig.published() == rig.before


def test_role_memory_wait_keeps_metadata_unchanged_and_cancel_works(rig: RedoRig) -> None:
    rig.start()
    before = (rig.store.root / MID / "meeting.json").read_bytes()
    rig.job_event("meeting.job.failed", code="memory")
    assert rig.jobs.redo_snapshot(MID)["status"] == "waiting_memory"
    assert (rig.store.root / MID / "meeting.json").read_bytes() == before
    rig.allowed = False
    rig.jobs.cancel(MID)
    assert rig.jobs.redo_results[MID] == "cancelled"
    assert rig.published() == rig.before


def test_suspend_keeps_redo_for_resume(rig: RedoRig) -> None:
    rig.start()
    before = (rig.store.root / MID / "meeting.json").read_bytes()
    assert rig.jobs.suspend()
    assert rig.jobs.queued_ids == [MID]
    assert (rig.store.root / MID / "meeting.json").read_bytes() == before
    rig.jobs.tick()
    rig.loaded()
    start = rig.role.last("meeting.job.start")
    assert start["resume"] and start["operation"] == "rediarize"


def fail_metadata_directory_fsync(monkeypatch: Any, *, repeat: bool = False) -> list[int]:
    """Actual write_private replace succeeded; inject only its directory fsync."""
    real_replace, real_sync = os.replace, os.fsync
    armed = [False]
    faults: list[int] = []

    def replace(src: Any, dst: Any, **kwargs: Any) -> None:
        real_replace(src, dst, **kwargs)
        if str(dst) == "meeting.json":
            armed[0] = True

    def fsync(fd: int) -> None:
        if armed[0] and stat.S_ISDIR(os.fstat(fd).st_mode) and (repeat or not faults):
            faults.append(fd)
            raise OSError("synthetic-directory-fsync")
        real_sync(fd)

    monkeypatch.setattr(os, "replace", replace)
    monkeypatch.setattr(os, "fsync", fsync)
    return faults


@pytest.mark.parametrize("restart", [False, True])
def test_enqueue_after_replace_fsync_never_increments_twice(
    rig: RedoRig, monkeypatch: Any, restart: bool
) -> None:
    with monkeypatch.context() as patch:
        faults = fail_metadata_directory_fsync(patch, repeat=True)
        rig.jobs.rediarize(MID)
        assert faults
        assert rig.meeting().meeting_generation == 2
        assert rig.meeting().rediar is not None
    if restart:
        rig.jobs = rig.make()
        rig.jobs.diar_models = lambda: rig.pair
    rig.jobs.rediarize(MID)
    assert rig.jobs.queued_ids == [MID]
    assert rig.meeting().meeting_generation == 2
    assert rig.published() == rig.before


def test_enqueue_own_generation_is_checked(rig: RedoRig) -> None:
    assert rig.store.rediar_enqueue(MID, 1) is not None
    assert rig.store.rediar_enqueue(MID, 1) is not None
    assert rig.store.rediar_enqueue(MID, 2) is None
    assert rig.meeting().meeting_generation == 2


@pytest.mark.parametrize("active", [False, True])
def test_cancel_after_replace_fsync_is_confirmed_from_disk(
    rig: RedoRig, monkeypatch: Any, active: bool
) -> None:
    if active:
        rig.start()
    else:
        rig.jobs.rediarize(MID)
    with monkeypatch.context() as patch:
        faults = fail_metadata_directory_fsync(patch)
        rig.jobs.cancel(MID)
        assert faults
    if active:
        assert "meeting.job.cancel" in rig.role.types()
        rig.now += 2
        rig.jobs.tick()
        rig.confirm_exit()
    assert rig.meeting().rediar is None
    assert rig.published() == rig.before
    other = rig.make()
    other.recover()
    other.tick()
    assert not other.busy


@pytest.mark.parametrize("persistent", [False, True])
def test_k6_after_replace_fsync_uses_disk_and_keeps_finished_at(
    rig: RedoRig, monkeypatch: Any, persistent: bool
) -> None:
    rig.start()
    rig.finish_text()
    with monkeypatch.context() as patch:
        faults = fail_metadata_directory_fsync(patch, repeat=persistent)
        rig.redo_done()
        assert faults
        assert rig.meeting().rediar is None
        assert rig.jobs.names_busy(MID) == persistent
    finished = rig.meeting().transcription.finished_at
    rig.wall += timedelta(days=5)
    rig.now += meeting_jobs.ADMIT_RETRY_S
    rig.jobs.tick()
    assert rig.meeting().transcription.finished_at == finished
    assert rig.jobs.redo_results[MID] == "done"
    assert not rig.jobs.names_busy(MID)


def test_late_old_generation_never_reaches_next_redo(rig: RedoRig) -> None:
    rig.start()
    rig.jobs.cancel(MID)
    rig.now += 2
    rig.jobs.tick()
    rig.confirm_exit()
    rig.jobs.rediarize(MID)
    rig.jobs.tick()
    rig.loaded()
    rig.store.write(MID, WORK, rig.text(3, final=False), writer=store.Writer.TRANSCRIBE)
    rig.accepted()
    rig.job_event("meeting.job.done", generation=2, diar_status="done")
    assert rig.meeting().rediar.generation == 3  # type: ignore[union-attr]
    assert rig.published() == rig.before


@pytest.mark.parametrize("reply", ["io", "timeout"])
def test_old_role_refuses_before_accepted(rig: RedoRig, reply: str) -> None:
    rig.jobs.rediarize(MID)
    rig.jobs.tick()
    rig.loaded()
    start = rig.role.last("meeting.job.start")
    assert start["operation"] == "rediarize"
    if reply == "timeout":
        rig.role.emit("error", req=start["req"], request_type="meeting.job.start", code="timeout")
    else:
        rig.job_event("meeting.job.failed", code="io")
    assert rig.jobs.names_busy(MID)
    rig.confirm_exit()
    assert rig.jobs.redo_results[MID] == "redo-failed"
    assert rig.published() == rig.before


@pytest.mark.parametrize(
    "result", [store.RediarPublishResult.IO_ERROR, store.RediarPublishResult.REJECTED]
)
def test_publish_result_contract(
    rig: RedoRig, result: store.RediarPublishResult, monkeypatch: Any
) -> None:
    rig.start()
    rig.finish_text()
    calls = []
    original = store.MeetingWriter.rediar_publish

    def publish(writer: store.MeetingWriter, generation: int) -> store.RediarPublishResult:
        calls.append(generation)
        return result

    monkeypatch.setattr(store.MeetingWriter, "rediar_publish", publish)
    rig.redo_done()
    for _ in range(25):
        rig.jobs.tick()
    assert calls == [2]
    assert rig.published() == rig.before
    assert rig.meeting().rediar is not None
    if result == store.RediarPublishResult.REJECTED:
        assert not rig.jobs.names_busy(MID)
        assert MID not in rig.jobs.redo_results
    else:
        assert rig.jobs.names_busy(MID)
        monkeypatch.setattr(store.MeetingWriter, "rediar_publish", original)
        rig.now += meeting_jobs.ADMIT_RETRY_S
        rig.jobs.tick()
        assert rig.jobs.redo_results[MID] == "done"


@pytest.mark.parametrize("active", [False, True])
def test_repeated_fsync_failure_during_cancel_does_not_resurrect(
    rig: RedoRig, monkeypatch: Any, active: bool
) -> None:
    if active:
        rig.start()
    else:
        rig.jobs.rediarize(MID)
    with monkeypatch.context() as patch:
        fail_metadata_directory_fsync(patch, repeat=True)
        rig.jobs.cancel(MID)
        assert rig.meeting().rediar is None
        if active:
            assert "meeting.job.cancel" not in rig.role.types()
            rig.redo_done()
            assert rig.published() == rig.before
    rig.now += meeting_jobs.CANCEL_RETRY_S
    rig.jobs.tick()
    if active:
        rig.now += 2
        rig.jobs.tick()
        rig.confirm_exit()
    assert rig.jobs.redo_results[MID] == "cancelled"
    fresh = rig.make()
    fresh.recover()
    fresh._settle_redo(MID)
    fresh.tick()
    assert not fresh.busy
    assert rig.meeting().rediar is None
    assert rig.published() == rig.before


@pytest.mark.parametrize("partial", [False, True])
def test_fresh_queue_after_uncertain_k6_keeps_logical_time(
    rig: RedoRig, monkeypatch: Any, partial: bool
) -> None:
    rig.retention = 0
    rig.start()
    rig.finish_text(partial=partial)
    with monkeypatch.context() as patch:
        fail_metadata_directory_fsync(patch, repeat=True)
        rig.redo_done()
    before = rig.meeting().transcription.finished_at
    rig.wall += timedelta(days=3)
    fresh = rig.make()
    fresh.wall_clock = lambda: rig.wall
    fresh._settle_redo(MID)
    fresh.tick()
    assert not fresh.busy
    assert rig.meeting().transcription.finished_at == before
    assert fresh.redo_results[MID] == "done"
    assert not rig.audio_present()  # 5в-5: partial from Д5 follows the same retention term
    assert rig.meeting().processing_status == (
        ProcessingStatus.PARTIAL if partial else ProcessingStatus.SUCCEEDED
    )


def test_restore_and_replace_uses_working_rows_always(rig: RedoRig) -> None:
    rig.start()
    active = rig.jobs._active
    assert active is not None
    active.resume = False
    rig.jobs._restore_live(active)
    rig.job_event(
        "meeting.job.replace",
        seq=0,
        replaces_id="u1",
        expected_revision=12,
        items=[
            {
                "id": "r1",
                "track": "mic",
                "start_ms": 0,
                "end_ms": 500,
                "text": "Уточнение",
                "speaker": "S2",
                "provisional": True,
                "multiple_speakers": False,
            }
        ],
    )
    snapshot = rig.jobs.job_snapshot(MID)
    assert snapshot is not None and snapshot["utterances"][0]["text"] == "Уточнение"
    assert rig.role.last("meeting.job.applied")["revision"] == 13
    assert rig.published() == rig.before


def test_store_enqueue_owns_one_non_nested_gate(rig: RedoRig, monkeypatch: Any) -> None:
    original = store._lock
    calls = []

    def lock(directory: int, name: str) -> Any:
        calls.append(name)
        return original(directory, name)

    monkeypatch.setattr(store, "_lock", lock)
    assert rig.store.rediar_enqueue(MID, 1) is not None
    assert calls == [".writer-gui.lock", ".store.lock"]


def test_cancel_while_enqueue_completion_waits(rig: RedoRig) -> None:
    work: list[Callable[[], None]] = []
    rig.jobs._submit = work.append
    rig.jobs.rediarize(MID)
    rig.jobs.cancel(MID)
    assert not rig.jobs.busy or rig.jobs.redo_snapshot(MID)["status"] == "queued"
    # P4 and its GUI completion are delayed; cancel still wins and cannot launch.
    while work:
        work.pop(0)()
        rig.jobs._drain()
    assert rig.meeting().rediar is None
    assert rig.jobs.redo_results[MID] == "cancelled"
    assert not rig.roles


def test_cancel_queued_is_busy_until_durable_completion(rig: RedoRig) -> None:
    rig.jobs.rediarize(MID)
    work: list[Callable[[], None]] = []
    rig.jobs._submit = work.append
    rig.jobs.cancel(MID)
    assert rig.jobs.names_busy(MID) and rig.jobs.busy
    assert rig.jobs.redo_snapshot(MID)["status"] == "cancelling"
    rig.jobs.rediarize(MID)
    assert len(work) == 1
    while work:
        work.pop(0)()
        rig.jobs._drain()
    assert not rig.jobs.names_busy(MID)
    assert rig.jobs.redo_results[MID] == "cancelled"


@pytest.mark.parametrize("during_cancel", [False, True])
def test_supervisor_error_never_discards_before_exit(rig: RedoRig, during_cancel: bool) -> None:
    rig.start()
    if during_cancel:
        rig.jobs.cancel(MID)
    rig.role.emit("error", code="restart-limit")
    assert rig.jobs.names_busy(MID)
    assert (rig.store.root / MID / WORK).exists()
    rig.confirm_exit()
    assert rig.jobs.redo_results[MID] == ("cancelled" if during_cancel else "redo-failed")
    assert rig.published() == rig.before


@pytest.mark.parametrize("gate", ["policy", "pair", "missing", "broken"])
def test_enqueue_preconditions_do_not_write_metadata(rig: RedoRig, gate: str) -> None:
    if gate == "policy":
        rig.allowed = False
    elif gate == "pair":
        rig.pair = None
    else:
        rig.admission = ModelAdmission(gate)
    before = (rig.store.root / MID / "meeting.json").read_bytes()
    rig.jobs.rediarize(MID)
    assert (rig.store.root / MID / "meeting.json").read_bytes() == before
    assert not rig.jobs.busy


def test_k6_writes_metadata_exactly_once_on_reentry(rig: RedoRig, monkeypatch: Any) -> None:
    rig.start()
    rig.finish_text(partial=True)
    writes = []
    original = rig.store.write

    def write(mid: str, name: str, *args: Any, **kwargs: Any) -> None:
        writes.append(name)
        original(mid, name, *args, **kwargs)

    monkeypatch.setattr(rig.store, "write", write)
    rig.redo_done()
    stamp = rig.meeting().transcription.finished_at
    rig.wall += timedelta(days=1)
    rig.jobs._settle_redo(MID)
    assert writes == ["meeting.json"]
    assert rig.meeting().transcription.finished_at == stamp
    assert rig.meeting().processing_status == ProcessingStatus.PARTIAL


def test_k6_rechecks_descriptor_after_publication(rig: RedoRig, monkeypatch: Any) -> None:
    rig.start()
    rig.finish_text()
    original = store.MeetingWriter.rediar_publish
    descriptor = rig.meeting().rediar

    # The GUI lease is held by the publish wrapper: use the real persistence
    # seam for this synthetic external descriptor change, not a nested writer.
    def changed(writer: store.MeetingWriter, generation: int) -> store.RediarPublishResult:
        result = original(writer, generation)
        meeting = rig.meeting()
        meeting.rediar = model.Rediar(generation, "f" * 64)
        (rig.store.root / MID / "meeting.json").write_text(json.dumps(meeting.to_dict()))
        return result

    monkeypatch.setattr(store.MeetingWriter, "rediar_publish", changed)
    rig.redo_done()
    assert rig.jobs.names_busy(MID)
    assert rig.meeting().rediar is not None
    assert rig.store.read(MID, "transcript.json").meeting_generation == 2
    rig.jobs.cancel(MID)
    assert rig.meeting().rediar is not None
    meeting = rig.meeting()
    meeting.rediar = descriptor
    rig.store.write(MID, "meeting.json", meeting, writer=store.Writer.GUI)
    monkeypatch.setattr(store.MeetingWriter, "rediar_publish", original)
    rig.now += meeting_jobs.ADMIT_RETRY_S
    rig.jobs.tick()
    assert rig.jobs.redo_results[MID] == "done"
    assert rig.role.types().count("meeting.job.start") == 1


def test_io_after_k3_only_rolls_commit_forward(rig: RedoRig, monkeypatch: Any) -> None:
    rig.start()
    rig.finish_text()
    real_sync, real_replace = os.fsync, os.replace
    armed = [False]

    def replace(src: Any, dst: Any, **kwargs: Any) -> None:
        real_replace(src, dst, **kwargs)
        if str(dst) == "transcript.json":
            armed[0] = True

    def fsync(fd: int) -> None:
        if armed[0] and stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError("synthetic-after-k3")
        real_sync(fd)

    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", replace)
        patch.setattr(os, "fsync", fsync)
        rig.redo_done()
        assert rig.store.read(MID, "transcript.json").meeting_generation == 2
        assert rig.jobs.names_busy(MID)
        rig.jobs.cancel(MID)
        for _ in range(10):
            rig.jobs.tick()
        assert rig.meeting().rediar is not None
    rig.now += meeting_jobs.ADMIT_RETRY_S
    rig.jobs.tick()
    assert rig.jobs.redo_results[MID] == "done"
    assert rig.store.read(MID, "speakers.json").labels == {}
    assert rig.role.types().count("meeting.job.start") == 1


def test_role_exit_during_cancel_write_retry_retires_timer(rig: RedoRig, monkeypatch: Any) -> None:
    rig.start()
    original = rig.jobs._redo_update
    calls = []

    def update(*args: Any, **kwargs: Any) -> model.Meeting | None:
        calls.append(1)
        if len(calls) == 1:
            raise store.MeetingStoreError("synthetic-before-cancel-replace")
        return original(*args, **kwargs)

    monkeypatch.setattr(rig.jobs, "_redo_update", update)
    rig.jobs.cancel(MID)
    assert MID in rig.jobs._cancel_writes
    rig.role.crash()  # confirmed exit before the retry timer fires
    rig.jobs.tick()
    assert rig.jobs.redo_results[MID] == "cancelled"
    assert MID not in rig.jobs._cancel_writes
    rig.now += meeting_jobs.CANCEL_RETRY_S
    rig.jobs.tick()
    assert rig.meeting().rediar is None
    assert not rig.meeting().cancelled
    assert rig.meeting().processing_status == ProcessingStatus.SUCCEEDED
    assert rig.published() == rig.before


def test_failure_of_first_diar_for_skipped_meeting_is_not_redo_failed(rig: RedoRig) -> None:
    meeting = rig.meeting()
    meeting.transcription.diar_status = "skipped"
    rig.store.write(MID, "meeting.json", meeting, writer=store.Writer.GUI)
    rig.start()
    rig.redo_done("failed")
    rig.confirm_exit()
    assert rig.jobs.redo_results[MID] == "failed-again"
    assert rig.published() == rig.before


def test_ordinary_transcribe_after_cancelled_redo_clears_published_override(rig: RedoRig) -> None:
    rig.jobs.rediarize(MID)
    rig.jobs.cancel(MID)
    assert rig.meeting().transcript_generation == 1
    rig.jobs.transcribe(MID)
    assert rig.meeting().meeting_generation == 3
    assert rig.meeting().transcript_generation is None
    assert rig.meeting().rediar is None
    assert rig.meeting().state == MeetingState.TRANSCRIBING
    assert MID not in rig.jobs.redo_results
    rig.jobs.cancel(MID)
    assert rig.meeting().cancelled  # the retired redo context cannot own this action


@pytest.mark.parametrize("suspended", [False, True])
@pytest.mark.parametrize("cancel", [False, True])
def test_processing_ban_preserves_redo_until_resume_or_cancel(
    rig: RedoRig, suspended: bool, cancel: bool
) -> None:
    if suspended:
        rig.start()
        rig.allowed = False
        assert rig.jobs.suspend()
    else:
        rig.jobs.rediarize(MID)
        rig.allowed = False
    directory = rig.store.root / MID
    before = {p.name: p.read_bytes() for p in directory.iterdir() if p.is_file()}
    for _ in range(8):
        rig.now += meeting_jobs.MEMORY_RETRY_S
        rig.jobs.tick()
    assert rig.jobs.queued_ids == [MID]
    assert rig.jobs.names_busy(MID) and rig.jobs.busy
    assert {p.name: p.read_bytes() for p in directory.iterdir() if p.is_file()} == before
    assert rig.audio_present()
    assert MID not in rig.jobs.redo_results
    assert len(rig.roles) == int(suspended)
    if cancel:
        rig.pair = None
        rig.admission = ModelAdmission("missing")
        rig.jobs.cancel(MID)
        assert rig.meeting().rediar is None
        assert rig.jobs.redo_results[MID] == "cancelled"
        return
    rig.allowed = True
    rig.jobs.tick()
    rig.loaded()
    start = rig.role.last("meeting.job.start")
    assert start["resume"] and start["operation"] == "rediarize"
    if not suspended:
        rig.store.write(MID, WORK, rig.text(2, final=False), writer=store.Writer.TRANSCRIBE)
    rig.accepted()
    rig.finish_text()
    rig.redo_done()
    assert rig.meeting().rediar is None
    assert rig.jobs.redo_results[MID] == "done"


@pytest.mark.parametrize("ban_at", ["probe", "completion"])
def test_processing_ban_during_redo_admission_waits(rig: RedoRig, ban_at: str) -> None:
    rig.jobs.rediarize(MID)
    before = (rig.store.root / MID / "meeting.json").read_bytes()
    admission = rig.admission

    def probe() -> ModelAdmission:
        rig.allowed = False
        return admission

    if ban_at == "probe":
        rig.jobs.model_probe = probe
        rig.jobs.tick()
    else:
        work: list[Callable[[], None]] = []
        rig.jobs._submit = work.append
        rig.jobs.tick()
        work.pop()()  # disk result exists, GUI completion has not run
        rig.allowed = False
        rig.jobs._drain()
    assert rig.jobs.queued_ids == [MID]
    assert rig.meeting().rediar is not None
    assert (rig.store.root / MID / "meeting.json").read_bytes() == before
    assert not rig.roles and MID not in rig.jobs.redo_results


def deferred_redo(rig: RedoRig, monkeypatch: Any, kind: str) -> meeting_jobs._Active:
    if kind == "abort":
        rig.jobs.rediarize(MID)
        rig.store.write(MID, WORK, rig.text(2, final=False), writer=store.Writer.TRANSCRIBE)

        def discard(writer: store.MeetingWriter) -> None:
            raise store.MeetingStoreError("synthetic-cleanup-busy")

        monkeypatch.setattr(store.MeetingWriter, "rediar_discard", discard)
        rig.jobs.cancel(MID)
        active = rig.jobs._redo_aborts[MID][0]
    else:
        rig.start()
        rig.finish_text()
        result = (
            store.RediarPublishResult.BUSY if kind == "busy" else store.RediarPublishResult.IO_ERROR
        )
        monkeypatch.setattr(
            store.MeetingWriter, "rediar_publish", lambda writer, generation: result
        )
        rig.redo_done()
        active = rig.jobs._redo_commits[MID]
    assert rig.jobs.names_busy(MID) and rig.jobs.busy
    return active


def assert_no_redo_context(rig: RedoRig) -> None:
    for rows in (
        rig.jobs._redo_commits,
        rig.jobs._redo_aborts,
        rig.jobs._redo_retry,
        rig.jobs._redo_generations,
        rig.jobs._redo_io,
        rig.jobs._redo_preparing,
        rig.jobs._cancel_writes,
        rig.jobs.redo_results,
    ):
        assert MID not in rows
    assert not rig.jobs.busy and not rig.jobs.names_busy(MID)


@pytest.mark.parametrize("kind", ["busy", "io", "abort"])
@pytest.mark.parametrize("deletion", ["api", "absent", "tombstone"])
def test_delete_during_deferred_redo_stops_all_retries(
    rig: RedoRig, monkeypatch: Any, kind: str, deletion: str
) -> None:
    active = deferred_redo(rig, monkeypatch, kind)
    later = []
    monkeypatch.setattr(rig.jobs, "_redo_later", lambda mid: later.append(mid))
    if deletion == "api":
        rig.jobs.delete_and_purge(MID)
        assert not (rig.store.root / MID).exists()
        assert_no_redo_context(rig)
    else:
        meeting = rig.meeting()
        meeting.state = MeetingState.DELETING
        meeting.meeting_generation += 1
        rig.store.write(MID, "meeting.json", meeting, writer=store.Writer.GUI)
        if deletion == "absent":
            rig.store.begin_delete(MID)
            rig.store.purge_deleted(MID)
    for _ in range(8):
        rig.now += meeting_jobs.ADMIT_RETRY_S
        rig.jobs.tick()
    assert_no_redo_context(rig)
    assert not later
    rig.jobs._settle_redo(MID)
    rig.jobs._redo_abort(active, "redo-failed")
    assert_no_redo_context(rig)
    if deletion == "tombstone":
        assert rig.published() == rig.before
    else:
        assert not (rig.store.root / MID).exists()


@pytest.mark.parametrize("kind", ["busy", "abort"])
@pytest.mark.parametrize("delivery", ["work", "completed", "failed"])
def test_late_redo_work_and_callbacks_after_purge_are_inert(
    rig: RedoRig, monkeypatch: Any, kind: str, delivery: str
) -> None:
    deferred_redo(rig, monkeypatch, kind)
    work: list[Callable[[], None]] = []
    rig.jobs._submit = work.append
    rig.now += meeting_jobs.ADMIT_RETRY_S
    rig.jobs.tick()
    run = work.pop()
    late = run
    if delivery != "work":
        with monkeypatch.context() as patch:
            if delivery == "failed":
                patch.setattr(
                    rig.jobs,
                    "_redo_read",
                    lambda mid: (_ for _ in ()).throw(store.MeetingStoreError("synthetic-io")),
                )
            run()
        late = rig.jobs._results.get_nowait()
        rig.jobs._pending -= 1
    rig.jobs._submit = lambda operation: operation()
    rig.jobs.delete_and_purge(MID)
    assert_no_redo_context(rig)
    later = []
    monkeypatch.setattr(rig.jobs, "_redo_later", lambda mid: later.append(mid))
    late()
    rig.jobs._drain()
    for _ in range(4):
        rig.now += meeting_jobs.ADMIT_RETRY_S
        rig.jobs.tick()
    assert_no_redo_context(rig)
    assert not later
    assert not (rig.store.root / MID).exists()
