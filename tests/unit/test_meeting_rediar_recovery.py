"""5в-5: startup roll-forward, leases, acceptance and retention crash points."""

from __future__ import annotations

import errno
import json
import os
import stat
import threading
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from test_meeting_jobs import MID, MID2
from test_meeting_rediar_jobs import WORK, RedoRig, fail_metadata_directory_fsync

from astra_voice.core import meeting_jobs
from astra_voice.core.meeting_jobs import MeetingJobs, ModelAdmission
from astra_voice.meetings import model, store
from astra_voice.meetings.events import MeetingState, ProcessingStatus
from astra_voice.security import files

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("filesystem_euid")]


@pytest.fixture
def rig(tmp_path: Path) -> Iterator[RedoRig]:
    instance = RedoRig(tmp_path)
    yield instance
    instance.jobs.shutdown()


def restart(rig: RedoRig, *, resume: bool = True) -> MeetingJobs:
    rig.jobs.shutdown()
    rig.jobs = rig.make()
    rig.jobs.wall_clock = lambda: rig.wall
    rig.jobs.admit_allowed = lambda: False
    rig.jobs.model_probe = lambda: pytest.fail("recovery must not probe a model")
    rig.jobs.diar_models = lambda: pytest.fail("recovery must not resolve models")
    rig.jobs.start_retention()
    if resume:
        rig.jobs.recover()
    rig.jobs.tick()
    return rig.jobs


def queued(rig: RedoRig, *, final: bool | None = None, partial: bool = False) -> None:
    rig.jobs.rediarize(MID)
    if final is not None:
        rig.store.write(
            MID, WORK, rig.text(2, final=final, partial=partial), writer=store.Writer.TRANSCRIBE
        )


def failed_first(rig: RedoRig) -> None:
    meeting = rig.meeting()
    meeting.processing_status = ProcessingStatus.PARTIAL
    meeting.transcription.diarization = True
    meeting.transcription.diar_status = "failed"
    rig.store.write(MID, "meeting.json", meeting, writer=store.Writer.GUI)


def expire(rig: RedoRig) -> None:
    meeting = rig.meeting()
    meeting.audio.retain_days = 7
    meeting.transcription.finished_at = (rig.wall - timedelta(days=10)).isoformat()
    rig.store.write(MID, "meeting.json", meeting, writer=store.Writer.GUI)


@pytest.mark.parametrize("work", [None, False])
def test_restart_resumes_unfinished_redo_and_holds_expired_audio(
    rig: RedoRig, work: bool | None
) -> None:
    expire(rig)
    queued(rig, final=work)
    jobs = restart(rig)
    assert jobs.queued_ids == [MID] and jobs._queue[0].resume and jobs._queue[0].redo
    assert rig.audio_present() and rig.meeting().rediar is not None
    assert rig.published() == rig.before
    jobs.cancel(MID)  # Available despite both model seams and admission being banned.
    jobs._retention_pass()
    assert rig.meeting().rediar is None and not rig.audio_present()


@pytest.mark.parametrize("partial", [False, True])
@pytest.mark.parametrize("retention", [0, 7, 30])
def test_final_work_published_at_start_without_role_or_models(
    rig: RedoRig, partial: bool, retention: int
) -> None:
    rig.retention = retention
    queued(rig, final=True, partial=partial)
    jobs = restart(rig)
    assert not rig.roles and not jobs.busy
    assert rig.store.read(MID, "transcript.json").meeting_generation == 2
    assert rig.store.read(MID, "speakers.json") == model.SpeakerMap(transcript_generation=2)
    assert rig.meeting().rediar is None
    assert rig.meeting().processing_status == (
        ProcessingStatus.PARTIAL if partial else ProcessingStatus.SUCCEEDED
    )
    assert rig.meeting().transcription.finished_at == rig.wall.isoformat()
    assert rig.audio_present() == bool(retention)
    if retention:
        rig.wall += timedelta(days=retention)
        jobs._retention_pass()
        assert not rig.audio_present()


class Crash(BaseException):
    """Process termination: ordinary error handlers must not turn it into failure."""


@pytest.mark.parametrize("boundary", ["rename", "names", "checkpoint", "metadata"])
def test_startup_rolls_forward_k3_k4_k5_k6(rig: RedoRig, boundary: str) -> None:
    rig.retention = 0
    queued(rig, final=True)
    directory = rig.store.root / MID
    if boundary == "rename":
        # K2 must be durable before the sole publication rename.
        names = rig.store.read(MID, "speakers.json")
        names.transcript_generation = 1
        (directory / "speakers.json").write_text(json.dumps(names.to_dict()))
        os.replace(directory / WORK, directory / "transcript.json")
        assert rig.store.read(MID, "speakers.json").transcript_generation != 2
    else:
        with rig.store.writer(MID, store.Writer.GUI) as writer:
            assert writer.rediar_publish(2) == store.RediarPublishResult.PUBLISHED
        if boundary == "names":
            # K4 done, an old checkpoint survived K5.
            (directory / "checkpoint.json").write_bytes(b"old checkpoint")
        elif boundary == "metadata":
            meeting = rig.meeting()
            meeting.rediar = None
            meeting.transcript_generation = None
            meeting.transcription.diar_status = "done"
            meeting.transcription.finished_at = rig.wall.isoformat()
            meeting.cleanup_pending = True
            rig.store.write(MID, "meeting.json", meeting, writer=store.Writer.GUI)
    jobs = restart(rig)
    assert not jobs.busy and not rig.roles
    assert rig.meeting().rediar is None and not rig.audio_present()
    assert rig.store.read(MID, "speakers.json") == model.SpeakerMap(transcript_generation=2)
    assert not (directory / "checkpoint.json").exists()


@pytest.mark.parametrize("lease", [store.Writer.DIAR, store.Writer.TRANSCRIBE])
def test_post_k3_lease_held_never_restarts_or_discards(rig: RedoRig, lease: store.Writer) -> None:
    rig.retention = 7
    queued(rig, final=True)
    with rig.store.writer(MID, store.Writer.GUI) as writer:
        assert writer.rediar_publish(2) == store.RediarPublishResult.PUBLISHED
    with rig.store.writer(MID, lease):
        jobs = restart(rig)
        assert rig.meeting().rediar is not None and not rig.roles
        assert jobs.redo_snapshot(MID)["status"] == "recovering"
        jobs.cancel(MID)
        for _ in range(3):
            jobs.tick()
        assert rig.meeting().rediar is not None
    rig.now += meeting_jobs.ADMIT_RETRY_S
    jobs.tick()
    assert rig.meeting().rediar is None and not jobs.busy and not rig.roles


@pytest.mark.parametrize("damage", ["eio", "permissions", "candidate", "post-k3"])
def test_recovery_error_backs_off_serves_others_and_recovers(
    rig: RedoRig, monkeypatch: pytest.MonkeyPatch, damage: str
) -> None:
    rig.retention = 7
    queued(rig, final=True)
    rig.create(MID2, state=MeetingState.TRANSCRIBING)
    path = rig.store.root / MID / WORK
    saved = path.read_bytes()
    publish = store.MeetingWriter.rediar_publish
    calls: list[str] = []

    def counted(writer: store.MeetingWriter, generation: int) -> store.RediarPublishResult:
        calls.append("publish")
        return publish(writer, generation)

    monkeypatch.setattr(store.MeetingWriter, "rediar_publish", counted)
    with monkeypatch.context() as patch:
        if damage == "eio":
            original = files.read_private

            def broken(path: Path, *args: Any, **kwargs: Any) -> bytes:
                if path.name == WORK:
                    raise OSError(errno.EIO, "fixed")
                return original(path, *args, **kwargs)

            patch.setattr(store, "read_private", broken)
        elif damage == "permissions":
            path.chmod(0o644)
        elif damage == "candidate":
            path.write_bytes(b"{broken")
        else:
            with rig.store.writer(MID, store.Writer.GUI) as writer:
                assert writer.rediar_publish(2) == store.RediarPublishResult.PUBLISHED
            path = rig.store.root / MID / "transcript.json"
            saved = path.read_bytes()
            broken_text = json.loads(saved)
            broken_text["labels_final"] = False
            path.write_text(json.dumps(broken_text))
        jobs = restart(rig)
        before = len(calls)
        assert jobs.queued_ids == [MID2]
        assert jobs.redo_snapshot(MID)["status"] == "recovery_error"
        assert jobs.redo_snapshot(MID)["recoveryError"] == "io"
        for _ in range(4):
            jobs.tick()
        assert len(calls) == before and rig.meeting().rediar is not None
        assert rig.audio_present() and not rig.roles
    path.chmod(0o600)
    path.write_bytes(saved)
    rig.now += meeting_jobs.ADMIT_RETRY_S
    jobs.tick()
    assert rig.meeting().rediar is None and not jobs.recovery_errors
    assert jobs.queued_ids == [MID2] and not rig.roles


def test_done_without_final_file_aborts_once_without_tick_loop(rig: RedoRig) -> None:
    rig.start()
    rig.redo_done()  # unfinished work with a delivered successful done is a role failure
    assert rig.meeting().rediar is None and rig.published() == rig.before
    assert not rig.jobs.busy
    for _ in range(3):
        rig.jobs.tick()
    assert rig.jobs.redo_results[MID] == "redo-failed"


@pytest.mark.parametrize("state", [MeetingState.RECORDED, MeetingState.FAILED])
def test_inconsistent_descriptor_is_cleared_and_work_removed(
    rig: RedoRig, state: MeetingState
) -> None:
    queued(rig, final=False)
    path = rig.store.root / MID / "meeting.json"
    data = json.loads(path.read_bytes())
    data["state"] = state
    path.write_text(json.dumps(data))
    jobs = restart(rig, resume=False)
    assert rig.meeting().rediar is None and rig.meeting().state == state
    assert not (path.parent / WORK).exists() and not jobs.busy and not rig.roles


@pytest.mark.parametrize("days", [0, 7, 30, 123])
def test_keep_as_is_terms_and_duplicate_do_not_extend(rig: RedoRig, days: int) -> None:
    failed_first(rig)
    rig.retention = days
    rig.allowed = False
    rig.pair = None
    rig.admission = ModelAdmission("missing")
    before = rig.published()
    result: list[bool] = []
    rig.jobs.keep_as_is(MID, 1, result.append)
    meeting = rig.meeting()
    assert result == [True]
    assert meeting.processing_status == ProcessingStatus.SUCCEEDED
    assert not meeting.transcription.diarization and meeting.transcription.diar_status == "skipped"
    assert meeting.transcription.finished_at == rig.wall.isoformat()
    assert meeting.audio.retain_days == days
    assert rig.audio_present() == bool(days)
    persisted = meeting.to_dict()
    rig.wall += timedelta(days=1)
    rig.jobs.keep_as_is(MID, 1)
    assert rig.meeting().to_dict() == persisted and rig.published() == before
    assert not rig.roles


@pytest.mark.parametrize("phase", ["queued", "running", "cancelling", "commit"])
def test_stale_keep_after_redo_does_nothing(rig: RedoRig, phase: str) -> None:
    failed_first(rig)
    if phase == "queued":
        queued(rig)
    else:
        rig.start()
        if phase == "cancelling":
            rig.jobs.cancel(MID)
        elif phase == "commit":
            rig.finish_text()
            with rig.store.writer(MID, store.Writer.DIAR):
                rig.redo_done()
    before = rig.meeting().to_dict()
    audio = rig.audio_present()
    assert rig.store.keep_as_is(MID, 1, retain_days=0, now=rig.wall) is None
    rig.jobs.keep_as_is(MID, 1)
    assert rig.meeting().to_dict() == before and rig.audio_present() == audio


@pytest.mark.parametrize("days", [0, 7])
def test_keep_before_redo_is_serialized_by_backend(rig: RedoRig, days: int) -> None:
    failed_first(rig)
    assert rig.store.keep_as_is(MID, 0, retain_days=days, now=rig.wall) is None
    assert rig.store.keep_as_is(MID, 1, retain_days=days, now=rig.wall) is not None
    updated = rig.store.rediar_enqueue(MID, 1)
    if days == 0:
        assert updated is None and rig.meeting().cleanup_pending
    else:
        assert updated is not None
        before = rig.meeting().to_dict()
        assert rig.store.keep_as_is(MID, 1, retain_days=0, now=rig.wall) is None
        assert rig.meeting().to_dict() == before
    assert rig.audio_present()


@pytest.mark.parametrize("repeat", [False, True])
def test_keep_uncertain_fsync_has_one_timestamp(
    rig: RedoRig, monkeypatch: pytest.MonkeyPatch, repeat: bool
) -> None:
    failed_first(rig)
    stamp = rig.wall.isoformat()
    with monkeypatch.context() as patch:
        fail_metadata_directory_fsync(patch, repeat=repeat)
        if repeat:
            with pytest.raises(store.MeetingStoreError):
                rig.store.keep_as_is(MID, 1, retain_days=7, now=rig.wall)
        else:
            assert rig.store.keep_as_is(MID, 1, retain_days=7, now=rig.wall) is not None
    assert rig.meeting().transcription.finished_at == stamp
    rig.wall += timedelta(days=3)
    assert rig.store.keep_as_is(MID, 1, retain_days=30, now=rig.wall) is None
    assert rig.meeting().transcription.finished_at == stamp
    assert rig.meeting().audio.retain_days == 7
    restart(rig)
    assert rig.meeting().transcription.finished_at == stamp


@pytest.mark.parametrize("repeat", [False, True])
@pytest.mark.parametrize("partial", [False, True])
def test_k6_uncertain_sync_restarts_and_completes_k7_once(
    rig: RedoRig, monkeypatch: pytest.MonkeyPatch, repeat: bool, partial: bool
) -> None:
    rig.retention = 0
    rig.start()
    rig.finish_text(partial=partial)
    with monkeypatch.context() as patch:
        fail_metadata_directory_fsync(patch, repeat=repeat)
        rig.redo_done()
    stamp = rig.meeting().transcription.finished_at
    rig.wall += timedelta(days=2)
    jobs = restart(rig)
    assert not rig.audio_present() and not jobs.busy
    assert rig.meeting().transcription.finished_at == stamp


@pytest.mark.parametrize("days", [0, 7, 30])
@pytest.mark.parametrize("diar", ["done", "failed"])
def test_first_partial_audio_retention_rule(rig: RedoRig, days: int, diar: str) -> None:
    meeting = rig.meeting()
    meeting.processing_status = ProcessingStatus.PARTIAL
    meeting.transcription.diar_status = diar
    meeting.transcription.finished_at = rig.wall.isoformat()
    meeting.audio.retain_days = days
    meeting.cleanup_pending = days == 0
    rig.store.write(MID, "meeting.json", meeting, writer=store.Writer.GUI)
    if days:
        rig.jobs._retention_pass()
        assert rig.audio_present()
        rig.wall += timedelta(days=days)
    rig.jobs._retention_pass()
    assert rig.audio_present() == (diar == "failed")


@pytest.mark.parametrize("phase", ["queued", "memory", "running"])
def test_expiry_waits_for_redo_then_resets_or_cancels(rig: RedoRig, phase: str) -> None:
    expire(rig)
    if phase == "running":
        rig.start()
    else:
        queued(rig)
        if phase == "memory":
            rig.memory = 0
            rig.jobs.tick()
            assert rig.jobs.redo_snapshot(MID)["status"] == "waiting_memory"
    rig.jobs._retention_pass()
    assert rig.audio_present()
    if phase == "running":
        rig.retention = 7
        rig.finish_text()
        rig.redo_done()
        assert rig.meeting().transcription.finished_at == rig.wall.isoformat()
        rig.jobs._retention_pass()
        assert rig.audio_present()
    else:
        rig.jobs.cancel(MID)
        rig.jobs._retention_pass()
        assert not rig.audio_present()


@pytest.mark.parametrize("lease", [store.Writer.DIAR, store.Writer.TRANSCRIBE])
def test_cancel_expired_audio_waits_for_role_lease(rig: RedoRig, lease: store.Writer) -> None:
    expire(rig)
    rig.start()
    with rig.store.writer(MID, lease):
        rig.jobs.cancel(MID)
        assert rig.meeting().rediar is None
        rig.jobs._retention_pass()
        assert rig.audio_present() and rig.meeting().audio.present
        rig.confirm_exit()  # Even an exit notification cannot defeat the lease.
        assert rig.audio_present()
    rig.now += meeting_jobs.ADMIT_RETRY_S
    rig.jobs.tick()
    rig.jobs._retention_pass()
    assert not rig.audio_present()


def test_audio_unlink_barrier_blocks_parallel_redo(
    rig: RedoRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    expire(rig)
    reached = threading.Event()
    attempted = threading.Event()
    outcomes: list[Any] = []
    errors: list[BaseException] = []
    unlink = os.unlink

    def barrier(name: Any, *args: Any, **kwargs: Any) -> None:
        if name == "mic-0000.wav":
            reached.set()
            assert attempted.wait(3)
        unlink(name, *args, **kwargs)

    def enqueue() -> None:
        try:
            assert reached.wait(3)
            try:
                outcomes.append(rig.store.rediar_enqueue(MID, 1))
            except store.MeetingStoreError:
                outcomes.append("busy")
        except BaseException as exc:
            errors.append(exc)
        finally:
            attempted.set()

    monkeypatch.setattr(os, "unlink", barrier)
    thread = threading.Thread(target=enqueue)
    thread.start()
    rig.store.remove_audio(MID, now=rig.wall)
    thread.join(3)
    assert not thread.is_alive() and not errors and outcomes == ["busy"]
    assert rig.meeting().rediar is None and not rig.audio_present()


def test_accepted_redo_before_unlink_retains_audio(rig: RedoRig) -> None:
    expire(rig)
    assert rig.store.rediar_enqueue(MID, 1) is not None
    assert rig.store.remove_audio(MID, now=rig.wall) is None
    assert rig.audio_present() and rig.meeting().rediar is not None


@pytest.mark.parametrize(
    "boundary", ["unlink", "audio-sync", "rmdir", "directory-sync", "metadata"]
)
def test_audio_cleanup_crashes_are_completed_next_start(
    rig: RedoRig, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    expire(rig)
    root = rig.store.root / MID
    audio_info, root_info = (root / "audio").stat(), root.stat()
    unlink, fsync, rmdir, write = os.unlink, os.fsync, os.rmdir, files.write_private
    fired = False

    def fail() -> None:
        nonlocal fired
        fired = True
        raise Crash

    def interrupted_unlink(name: Any, *args: Any, **kwargs: Any) -> None:
        unlink(name, *args, **kwargs)
        if name == "mic-0000.wav":
            fail()

    def interrupted_sync(fd: int) -> None:
        fsync(fd)
        info = os.fstat(fd)
        target = audio_info if boundary == "audio-sync" else root_info
        if (info.st_dev, info.st_ino) == (target.st_dev, target.st_ino) and not rig.audio_present():
            fail()

    def interrupted_rmdir(name: Any, *args: Any, **kwargs: Any) -> None:
        rmdir(name, *args, **kwargs)
        if name == "audio":
            fail()

    def interrupted_write(path: Path, payload: bytes, **kwargs: Any) -> None:
        if path.name == "meeting.json" and not json.loads(payload)["audio"]["present"]:
            fail()
        write(path, payload, **kwargs)

    with monkeypatch.context() as patch:
        if boundary == "unlink":
            patch.setattr(os, "unlink", interrupted_unlink)
        elif boundary in {"audio-sync", "directory-sync"}:
            patch.setattr(os, "fsync", interrupted_sync)
        elif boundary == "rmdir":
            patch.setattr(os, "rmdir", interrupted_rmdir)
        else:
            patch.setattr(store, "write_private", interrupted_write)
        with pytest.raises(Crash):
            rig.store.remove_audio(MID, now=rig.wall)
    assert fired and rig.meeting().cleanup_pending
    restart(rig)
    assert not rig.meeting().audio.present and not rig.meeting().cleanup_pending


@pytest.mark.parametrize("where", ["startup", "discard", "publish"])
def test_exact_temp_cleanup_waits_for_leases(rig: RedoRig, where: str) -> None:
    if where == "publish":
        queued(rig, final=True)
    directory = rig.store.root / MID
    owned = directory / (".rediar-transcript-" + "a" * 32 + ".tmp")
    checkpoint = directory / (".rediar-checkpoint-" + "b" * 32 + ".tmp")
    unrelated = [directory / name for name in [".rediar-transcript-short.tmp", ".other.tmp"]]
    outside = rig.store.root / (".rediar-transcript-" + "c" * 32 + ".tmp")
    for path in [owned, checkpoint, *unrelated, outside]:
        path.write_bytes(b"temporary")
    with rig.store.writer(MID, store.Writer.TRANSCRIBE):
        if where == "startup":
            restart(rig)
            assert rig.jobs.redo_snapshot(MID)["status"] == "recovering"
        else:
            with rig.store.writer(MID, store.Writer.GUI) as writer:
                if where == "discard":
                    with pytest.raises(store.MeetingStoreError):
                        writer.rediar_discard()
                else:
                    assert writer.rediar_publish(2) == store.RediarPublishResult.BUSY
        assert owned.exists() and checkpoint.exists()
    if where == "startup":
        rig.now += meeting_jobs.ADMIT_RETRY_S
        rig.jobs.tick()
    else:
        with rig.store.writer(MID, store.Writer.GUI) as writer:
            if where == "discard":
                writer.rediar_discard()
            else:
                assert writer.rediar_publish(2) == store.RediarPublishResult.PUBLISHED
    assert not owned.exists() and not checkpoint.exists()
    assert all(path.exists() for path in [*unrelated, outside])


@pytest.mark.parametrize("name", [WORK, "rediar-checkpoint.json"])
@pytest.mark.parametrize(
    "stage",
    [
        "create",
        "file-before",
        "file-after",
        "rename-before",
        "rename-after",
        "dir-before",
        "dir-after",
    ],
)
@pytest.mark.parametrize("crash", [False, True], ids=["enospc", "crash"])
def test_work_write_crash_and_enospc_sweep_exact_temporaries(
    rig: RedoRig, monkeypatch: pytest.MonkeyPatch, name: str, stage: str, crash: bool
) -> None:
    queued(rig, final=False)
    directory = rig.store.root / MID
    original_text = (directory / "transcript.json").read_bytes()
    original_names = (directory / "speakers.json").read_bytes()
    real_open, real_sync, real_replace, real_unlink = os.open, os.fsync, os.replace, os.unlink
    temp_fds: set[int] = set()
    armed = False
    fired = False

    def fault() -> None:
        nonlocal fired
        fired = True
        if crash:
            raise Crash
        raise OSError(errno.ENOSPC, "fixed")

    def opening(path: Any, *args: Any, **kwargs: Any) -> int:
        nonlocal armed
        fd = real_open(path, *args, **kwargs)
        if str(path).startswith(".rediar-") and str(path).endswith(".tmp"):
            armed = True
            temp_fds.add(fd)
            if stage == "create":
                os.close(fd)  # Process termination would close this descriptor.
                fault()
        return fd

    def syncing(fd: int) -> None:
        regular = stat.S_ISREG(os.fstat(fd).st_mode)
        matching = (regular and fd in temp_fds) or (not regular and armed)
        prefix = "file" if regular else "dir"
        if matching and stage == prefix + "-before":
            fault()
        real_sync(fd)
        if matching and stage == prefix + "-after":
            fault()

    def replacing(src: Any, dst: Any, **kwargs: Any) -> None:
        if str(dst) == name and stage == "rename-before":
            fault()
        real_replace(src, dst, **kwargs)
        if str(dst) == name and stage == "rename-after":
            fault()

    def unlinking(path: Any, *args: Any, **kwargs: Any) -> None:
        # SIGKILL does not execute write_private's finally cleanup.
        if crash and fired and str(path).startswith(".rediar-"):
            raise Crash
        real_unlink(path, *args, **kwargs)

    document: model.Transcript | model.Checkpoint
    if name == WORK:
        document = rig.text(2)
        document.transcript_revision += 1
    else:
        document = model.Checkpoint(MID, meeting_generation=2, transcript_revision=12)
    with monkeypatch.context() as patch:
        patch.setattr(os, "open", opening)
        patch.setattr(os, "fsync", syncing)
        patch.setattr(os, "replace", replacing)
        patch.setattr(os, "unlink", unlinking)
        with pytest.raises(Crash if crash else store.MeetingStoreError):
            rig.store.write(MID, name, document, writer=store.Writer.TRANSCRIBE)
    assert fired
    assert (directory / "transcript.json").read_bytes() == original_text
    assert (directory / "speakers.json").read_bytes() == original_names
    assert rig.audio_present() and rig.meeting().rediar is not None
    restart(rig)
    assert not [p for p in directory.iterdir() if p.name.startswith(".rediar-")]
    # If the final working text reached disk, startup publishes it. Otherwise
    # only unfinished work is queued; neither case loads a model.
    assert not rig.roles
    assert rig.meeting().rediar is None or rig.jobs.queued_ids == [MID]


@pytest.mark.parametrize("days", [0, 7, 30])
def test_first_transcription_done_reconcile_failure_starts_retention(
    rig: RedoRig, days: int
) -> None:
    rig.retention = days
    rig.jobs.transcribe(MID)
    rig.jobs.tick()
    rig.loaded()
    rig.accepted()
    rig.job_event(
        "meeting.job.done", diar_status="done", reconcile_failed=True, wall_ms=1, peak_rss_kb=1
    )
    assert rig.meeting().processing_status == ProcessingStatus.PARTIAL
    assert rig.audio_present() == bool(days)
    if days:
        rig.wall += timedelta(days=days)
        rig.jobs._retention_pass()
        assert not rig.audio_present()


@pytest.mark.parametrize("lease", [store.Writer.DIAR, store.Writer.TRANSCRIBE])
def test_keep_waits_for_live_writer_even_after_descriptor_removed(
    rig: RedoRig, lease: store.Writer
) -> None:
    failed_first(rig)
    rig.start()
    with rig.store.writer(MID, lease):
        rig.jobs.cancel(MID)
        before = rig.meeting().to_dict()
        # Even a direct backend caller with the *new* generation cannot race
        # a cancelling process whose descriptor was already durably removed.
        with pytest.raises(store.MeetingStoreError):
            rig.store.keep_as_is(MID, 2, retain_days=0, now=rig.wall)
        assert rig.meeting().to_dict() == before and rig.audio_present()


def test_persistent_commit_directory_eio_is_visible_and_repairable(
    rig: RedoRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig.retention = 7
    queued(rig, final=True)
    with rig.store.writer(MID, store.Writer.GUI) as writer:
        assert writer.rediar_publish(2) == store.RediarPublishResult.PUBLISHED
    directory_info = (rig.store.root / MID).stat()
    sync = os.fsync
    calls: list[int] = []

    def broken(fd: int) -> None:
        info = os.fstat(fd)
        if (info.st_dev, info.st_ino) == (directory_info.st_dev, directory_info.st_ino):
            calls.append(fd)
            raise OSError(errno.EIO, "fixed")
        sync(fd)

    with monkeypatch.context() as patch:
        patch.setattr(os, "fsync", broken)
        jobs = restart(rig)
        assert jobs.redo_snapshot(MID)["status"] == "recovery_error"
        assert rig.meeting().rediar is not None
        count = len(calls)
        for _ in range(3):
            jobs.tick()
        assert len(calls) == count
    rig.now += meeting_jobs.ADMIT_RETRY_S
    jobs.tick()
    assert rig.meeting().rediar is None and not jobs.busy and not rig.roles


@pytest.mark.parametrize("repeat", [False, True])
def test_k7_metadata_uncertain_sync_keeps_finished_and_deleted_times(
    rig: RedoRig, monkeypatch: pytest.MonkeyPatch, repeat: bool
) -> None:
    rig.retention = 0
    rig.start()
    rig.finish_text()
    replace, sync = os.replace, os.fsync
    armed = False
    calls: list[int] = []

    def replacing(src: Any, dst: Any, **kwargs: Any) -> None:
        nonlocal armed
        replace(src, dst, **kwargs)
        if str(dst) == "meeting.json" and not rig.meeting().audio.present:
            armed = True

    def syncing(fd: int) -> None:
        if armed and stat.S_ISDIR(os.fstat(fd).st_mode) and (repeat or not calls):
            calls.append(fd)
            raise OSError(errno.EIO, "fixed")
        sync(fd)

    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", replacing)
        patch.setattr(os, "fsync", syncing)
        rig.redo_done()
    assert calls and not rig.meeting().audio.present
    finished = rig.meeting().transcription.finished_at
    deleted = rig.meeting().audio.deleted_at
    rig.wall += timedelta(days=3)
    restart(rig)
    assert rig.meeting().transcription.finished_at == finished
    assert rig.meeting().audio.deleted_at == deleted
    assert not rig.meeting().cleanup_pending and not rig.audio_present()


def test_zero_retention_done_retries_after_last_checkpoint_lease_release(
    rig: RedoRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig.retention = 0
    rig.jobs.transcribe(MID)
    rig.jobs.tick()
    rig.loaded()
    rig.accepted()
    attempts: list[float] = []
    remove = rig.store.remove_audio

    def counted(mid: str, **kwargs: Any) -> model.Meeting | None:
        attempts.append(rig.now)
        return remove(mid, **kwargs)

    monkeypatch.setattr(rig.store, "remove_audio", counted)
    checkpoint = rig.store.root / MID / "checkpoint.json"
    with rig.store.writer(MID, store.Writer.TRANSCRIBE):
        # _finish sends done before unlinking its final checkpoint and releasing
        # this lease. Neither receipt of done nor ticks prove the writer exited.
        checkpoint.write_bytes(b"last checkpoint")
        rig.done()
        assert rig.meeting().state == MeetingState.DONE
        assert rig.meeting().cleanup_pending and rig.audio_present()
        assert checkpoint.exists() and len(attempts) == 1
        rig.jobs.admit_allowed = lambda: False
        rig.jobs.model_probe = lambda: pytest.fail("cleanup must not probe models")
        for _ in range(5):
            rig.jobs.tick()
        assert len(attempts) == 1
        rig.now += meeting_jobs.AUDIO_CLEANUP_RETRY_S
        for _ in range(5):
            rig.jobs.tick()
        assert len(attempts) == 2 and rig.audio_present()
        checkpoint.unlink()
    rig.now += meeting_jobs.AUDIO_CLEANUP_RETRY_S - 0.5
    rig.jobs.tick()
    assert len(attempts) == 2 and rig.audio_present()
    rig.now += 0.5
    rig.jobs.tick()
    assert len(attempts) == 3 and not rig.audio_present()
    assert not rig.meeting().audio.present and not rig.meeting().cleanup_pending
    assert not rig.jobs.meetings[MID].audio.present
    rig.now += meeting_jobs.AUDIO_CLEANUP_RETRY_S
    rig.jobs.tick()
    assert len(attempts) == 3


def test_short_audio_cleanup_retry_has_only_one_worker_in_flight(rig: RedoRig) -> None:
    meeting = rig.meeting()
    meeting.cleanup_pending = True
    rig.store.write(MID, "meeting.json", meeting, writer=store.Writer.GUI)
    with rig.store.writer(MID, store.Writer.TRANSCRIBE):
        assert rig.jobs._remove_audio(MID) is None
        rig.jobs._drain()
        assert rig.audio_present() and MID in rig.jobs._audio_retry_at
    tasks: list[Any] = []
    rig.jobs._submit = tasks.append
    rig.now += meeting_jobs.AUDIO_CLEANUP_RETRY_S
    for _ in range(5):
        rig.jobs.tick()
    assert len(tasks) == 1 and rig.jobs.filesystem_pending
    assert rig.audio_present()
    tasks.pop()()
    rig.jobs.tick()
    assert not rig.audio_present() and MID not in rig.jobs._audio_retry_at
    assert not rig.jobs._audio_cleanup_io and not rig.jobs.filesystem_pending


def test_crash_after_invalid_descriptor_clear_sweeps_non_done_orphans_on_restart(
    rig: RedoRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    queued(rig, final=False)
    directory = rig.store.root / MID
    metadata = directory / "meeting.json"
    data = json.loads(metadata.read_bytes())
    data["state"] = MeetingState.FAILED
    metadata.write_text(json.dumps(data))
    owned = [
        directory / WORK,
        directory / "rediar-checkpoint.json",
        directory / (".rediar-transcript-" + "a" * 32 + ".tmp"),
        directory / (".rediar-checkpoint-" + "b" * 32 + ".tmp"),
    ]
    for path in owned[1:]:
        path.write_bytes(b"orphan")
    checkpoint = directory / "checkpoint.json"
    checkpoint.write_bytes(b"ordinary checkpoint")
    unrelated = directory / ".rediar-transcript-not-32-hex.tmp"
    unrelated.write_bytes(b"unrelated")
    persist = rig.store._persist_metadata

    def crash_after_metadata(fd: int, meeting: model.Meeting) -> None:
        persist(fd, meeting)
        raise Crash

    with monkeypatch.context() as patch:
        patch.setattr(rig.store, "_persist_metadata", crash_after_metadata)
        with pytest.raises(Crash):
            rig.store.rediar_cleanup(MID)
    assert rig.meeting().state == MeetingState.FAILED and rig.meeting().rediar is None
    assert all(path.exists() for path in owned)
    restart(rig, resume=False)
    assert all(not path.exists() for path in owned)
    assert checkpoint.read_bytes() == b"ordinary checkpoint"
    assert unrelated.read_bytes() == b"unrelated" and not rig.roles


@pytest.mark.parametrize("state", [s for s in MeetingState if s != MeetingState.DELETING])
def test_retention_sweeps_rediar_orphans_without_descriptor_in_every_state(
    rig: RedoRig, state: MeetingState
) -> None:
    directory = rig.store.root / MID
    metadata = directory / "meeting.json"
    data = json.loads(metadata.read_bytes())
    data["state"] = state
    metadata.write_text(json.dumps(data))
    owned = [directory / WORK, directory / (".rediar-checkpoint-" + "d" * 32 + ".tmp")]
    for path in owned:
        path.write_bytes(b"orphan")
    before = rig.published()
    rig.jobs._retention_pass()
    assert all(not path.exists() for path in owned)
    assert rig.meeting().state == state and rig.meeting().rediar is None
    assert rig.published() == before and not rig.roles


@pytest.mark.parametrize(
    "state,cancelled,checkpoint,expected",
    [
        (MeetingState.TRANSCRIBING, False, False, True),
        (MeetingState.WAITING_MEMORY, False, False, True),
        (MeetingState.NEEDS_MODEL, False, False, True),
        (MeetingState.RECORDED, False, True, True),
        (MeetingState.RECORDED, False, False, False),
        (MeetingState.TRANSCRIBING, True, False, False),
        (MeetingState.FAILED, False, False, False),
    ],
)
def test_delayed_descriptor_repair_applies_ordinary_recovery_conditions(
    rig: RedoRig, state: MeetingState, cancelled: bool, checkpoint: bool, expected: bool
) -> None:
    queued(rig, final=False)
    directory = rig.store.root / MID
    data = json.loads((directory / "meeting.json").read_bytes())
    data.update(state=state, cancelled=cancelled)
    (directory / "meeting.json").write_text(json.dumps(data))
    if checkpoint:
        (directory / "checkpoint.json").write_bytes(b"ordinary checkpoint")
    with rig.store.writer(MID, store.Writer.TRANSCRIBE):
        jobs = restart(rig)
        assert rig.meeting().rediar is not None and not jobs.queued_ids
        assert MID in jobs._redo_recovery and not rig.roles
        for _ in range(3):
            jobs.tick()
        assert rig.meeting().rediar is not None
    rig.now += meeting_jobs.ADMIT_RETRY_S
    jobs.tick()
    assert rig.meeting().rediar is None and not (directory / WORK).exists()
    assert jobs.queued_ids == ([MID] if expected else [])
    if expected:
        entry = jobs._queue[0]
        assert entry.resume and not entry.redo
        jobs.recover()
        assert jobs.queued_ids == [MID]  # Recovery stays idempotent after repair.
    assert MID not in jobs._redo_recovery and not rig.roles


@pytest.mark.parametrize("already_pending", [False, True])
def test_retention_publishes_disk_marker_when_unlink_fails_and_retries_in_five_seconds(
    rig: RedoRig, already_pending: bool
) -> None:
    expire(rig)
    meeting = rig.meeting()
    meeting.transcription.diarization = False
    meeting.transcription.diar_status = "skipped"
    meeting.cleanup_pending = already_pending
    rig.store.write(MID, "meeting.json", meeting, writer=store.Writer.GUI)
    rig.jobs.meetings[MID] = meeting
    obstacle = rig.store.root / MID / "audio" / "directory-blocking-unlink"
    obstacle.mkdir(mode=0o700)
    before = rig.jobs.revision
    rig.jobs.start_retention()
    rig.jobs.tick()
    assert obstacle.is_dir()
    assert rig.meeting().cleanup_pending and rig.meeting().audio.present
    assert rig.jobs.meetings[MID].cleanup_pending
    assert rig.jobs.meetings[MID].audio.present
    assert rig.jobs.revision > before
    assert rig.jobs._audio_retry_at[MID] == rig.now + 5
    obstacle.rmdir()
    rig.now += 4.9
    for _ in range(3):
        rig.jobs.tick()
    assert rig.meeting().audio.present and rig.meeting().cleanup_pending
    rig.now += 0.1
    rig.jobs.tick()
    assert not rig.meeting().audio.present and not rig.meeting().cleanup_pending
    assert not rig.jobs.meetings[MID].audio.present
    assert not rig.jobs.meetings[MID].cleanup_pending
    assert MID not in rig.jobs._audio_retry_at
    deleted = rig.meeting().audio.deleted_at
    rig.now += 5
    rig.jobs.tick()
    assert rig.meeting().audio.deleted_at == deleted
