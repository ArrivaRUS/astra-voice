"""T2: trust renewal at D4, fresh checkpoint identities and redo isolation."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from test_meeting_diar_install import PAIR
from test_meeting_jobs import Rig as JobsRig
from test_meeting_rediar_role import prepare, published
from test_meeting_transcribe_diar import ScriptedDiar

from astra_voice.meetings import model, store
from astra_voice.meetings.events import PhaseStatus
from astra_voice.models import diarization
from astra_voice.worker import meeting_transcribe as mt
from helpers.meeting_audio import MID, Rig, bursts, make_disk, write_track

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("filesystem_euid")]
NEW_PAIR = {**PAIR, "embedder_sha256": "c" * 64}


class Crash(BaseException):
    """Interruption keeps the unfinished checkpoint for a new worker."""


def request(disk: store.MeetingStore, *, redo: bool, resume: bool) -> dict[str, Any]:
    return {
        "type": "meeting.job.start",
        "request_type": "meeting.job.start",
        "req": "t2",
        "meeting_id": MID,
        "meeting_generation": 1 if redo else 0,
        "dir": str(disk.root / MID),
        "tracks": ["mic", "far"],
        "variant": "app_stream",
        "diarization": True,
        "diar_models": dict(PAIR),
        "self_track": "mic",
        "resume": resume,
        "echo_gate": False,
        "limits": {"max_ms": 14_400_000, "max_utterances": 9000, "max_blocks": 12},
        **({"operation": "rediarize"} if redo else {}),
    }


def disk_for(root: Path, redo: bool) -> store.MeetingStore:
    if redo:
        disk = prepare(root)
        meeting = disk.read(MID, "meeting.json")
        meeting.transcription.diarization = True
        meeting.transcription.diar_status = "done"
        disk.write(MID, "meeting.json", meeting, writer=store.Writer.GUI)
        return disk
    disk = make_disk(root)
    write_track(disk, "mic", bursts([0.6]))
    write_track(disk, "far", bursts([0.5]))
    return disk


def run(
    rig: Rig,
    monkeypatch: pytest.MonkeyPatch,
    req: dict[str, Any],
    *,
    interrupt: bool = False,
) -> tuple[dict[str, Any], list[dict[str, str]], list[model.Checkpoint], list[int]]:
    state = rig.state
    events: list[dict[str, Any]] = []
    loaded: list[dict[str, str]] = []
    checkpoints: list[model.Checkpoint] = []
    blocks: list[int] = []
    job = mt._Job(deepcopy(req))
    job.credits = 64
    job.delivered.set()
    monkeypatch.setattr(state, "_emit", lambda job, event: events.append(deepcopy(event)))

    class CaptureDiar(ScriptedDiar):
        def load(self, models: dict[str, str]) -> None:
            loaded.append(dict(models))
            cp = rig.disk.read(MID, job.checkpoint_name)
            assert cp.diar.status == PhaseStatus.RUNNING
            assert not cp.diar.turns and not cp.diar.label_map
            checkpoints.append(cp)
            expected = mt._Job({**req, "diar_models": dict(models)})
            assert cp.processing_config_sha256 == state._config(expected, cp.model)
            if models != req["diar_models"]:
                assert cp.processing_config_sha256 != state._config(mt._Job(req), cp.model)

        def block(self, audio: Any, index: int, offset: int) -> list[list[Any]]:
            blocks.append(index)
            if interrupt:
                raise Crash
            return super().block(audio, index, offset)

    state.diar_factory = lambda identity, cancelled, **kw: CaptureDiar(identity, cancelled)
    state._run_job(job)
    terminal = [e for e in events if e["type"] in {"meeting.job.done", "meeting.job.failed"}]
    assert terminal, f"worker did not finish: {events!r}"
    return terminal[-1], loaded, checkpoints, blocks


@pytest.mark.parametrize("redo", [False, True])
@pytest.mark.parametrize("replacement", [None, NEW_PAIR])
@pytest.mark.parametrize("boundary", ["unload", "memory"])
def test_reconfirm_pair_at_load_and_checkpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    redo: bool,
    replacement: dict[str, str] | None,
    boundary: str,
) -> None:
    disk = disk_for(tmp_path / "disk", redo)
    before = published(disk) if redo else None
    selected: list[dict[str, str] | None] = [dict(PAIR)]
    monkeypatch.setattr(diarization, "trusted_diarization_models", lambda *a, **k: selected[0])
    rig = Rig(disk)
    rig.load()
    state = rig.state
    if boundary == "unload":
        unload = state._unload_ack

        def changed_unload() -> dict[str, Any]:
            ack = unload()
            selected[0] = replacement
            return ack

        monkeypatch.setattr(state, "_unload_ack", changed_unload)
    else:
        memory = state._diar_memory

        def changed_memory(job: mt._Job, deadline: float) -> None:
            memory(job, deadline)
            selected[0] = replacement

        monkeypatch.setattr(state, "_diar_memory", changed_memory)
    try:
        result, loaded, checkpoints, blocks = run(
            rig, monkeypatch, request(disk, redo=redo, resume=redo)
        )
        assert result["type"] == "meeting.job.done"
        assert result["diar_status"] == (
            "done" if replacement is not None else "failed" if redo else "skipped"
        )
        assert result["reconcile_failed"] is False
        assert loaded == ([] if replacement is None else [replacement])
        if replacement is not None:
            assert checkpoints and blocks[0] == 0
        if redo:
            assert published(disk) == before
            assert disk.read(MID, "transcript.json").meeting_generation == 0
            if replacement is None:
                # Feed the actual failed redo outcome to the GUI coordinator on
                # this same disk: abort only the work pair after confirmed exit.
                jobs = JobsRig(tmp_path / "gui")
                jobs.jobs.store = disk
                jobs.store = disk
                jobs.jobs.diar_models = lambda: dict(PAIR)
                jobs.jobs._recover_requested = True
                jobs.jobs._accept_redo_recovery(
                    disk.read(MID, "meeting.json"), store.RediarPublishResult.WORK_PENDING
                )
                try:
                    jobs.running(MID)
                    fields = {
                        k: v
                        for k, v in result.items()
                        if k not in {"type", "meeting_id", "meeting_generation"}
                    }
                    jobs.job_event("meeting.job.done", mid=MID, **fields)
                    jobs.role.exited = True
                    jobs.jobs.tick()
                    assert jobs.jobs.redo_results[MID] == "redo-failed"
                    assert disk.read(MID, "meeting.json").rediar is None
                    assert not (disk.root / MID / "rediar-transcript.json").exists()
                    assert not (disk.root / MID / "rediar-checkpoint.json").exists()
                    assert published(disk) == before
                finally:
                    jobs.jobs.shutdown()
        else:
            final = disk.read(MID, "transcript.json")
            assert final.labels_final
            if replacement is None:
                assert {u.speaker for u in final.utterances if u.track == "far"} == {"others"}
    finally:
        rig.close()


@pytest.mark.parametrize("redo", [False, True])
def test_new_trusted_pair_reseeds_work_after_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    redo: bool,
) -> None:
    disk = disk_for(tmp_path, redo)
    selected = [dict(PAIR)]
    monkeypatch.setattr(diarization, "trusted_diarization_models", lambda *a, **k: selected[0])
    req = request(disk, redo=redo, resume=redo)
    first = Rig(disk)
    first.load()
    try:
        with pytest.raises(Crash):
            run(first, monkeypatch, req, interrupt=True)
    finally:
        first.close()
    name = "rediar-checkpoint.json" if redo else "checkpoint.json"
    old = disk.read(MID, name)
    selected[0] = dict(NEW_PAIR)
    second = Rig(disk)
    second.load()
    try:
        req["resume"] = True
        result, loaded, checkpoints, blocks = run(second, monkeypatch, req)
        assert result["diar_status"] == "done" and loaded == [NEW_PAIR]
        assert blocks[0] == 0
        assert checkpoints[0].processing_config_sha256 != old.processing_config_sha256
    finally:
        second.close()


def test_late_untrusted_pair_has_same_metadata_as_no_pair_at_admission(tmp_path: Path) -> None:
    from astra_voice.meetings.events import ProcessingStatus

    jobs = JobsRig(tmp_path)
    jobs.jobs.diar_models = lambda: dict(PAIR)
    jobs.create(MID)
    try:
        jobs.jobs.transcribe(MID)
        jobs.running(MID)
        jobs.job_event("meeting.job.done", mid=MID, diar_status="skipped")
        meeting = jobs.meeting(MID)
        assert meeting.processing_status == ProcessingStatus.SUCCEEDED
        assert meeting.transcription.diar_status == "skipped"
        assert meeting.transcription.diarization is False
    finally:
        jobs.jobs.shutdown()
