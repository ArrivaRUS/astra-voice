"""T-405 GUI replace ledger, resumed receipts and no transcript writes."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
from test_meeting_jobs import MID, Rig

from astra_voice.meetings.events import PhaseStatus, ProcessingStatus
from astra_voice.meetings.model import (
    ReconcileProgress,
    ReconcileReplacement,
    Transcript,
    Utterance,
)
from astra_voice.meetings.store import Writer

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("filesystem_euid")]


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    r = Rig(tmp_path)
    r.create()
    assert r.jobs.on_recorded(MID)
    r.running()
    r.utterance(0, "первый второй")
    r.utterance(1, "следующий")
    r.job_event(
        "meeting.job.labels",
        seq=0,
        items=[["u00000", "S1"]],
        final=False,
        speakers_total=1,
        transcript_revision=10,
    )
    return r


def payload() -> dict[str, Any]:
    return {
        "seq": 11,
        "replaces_id": "u00000",
        "expected_revision": 10,
        "items": [
            {
                "id": f"r{i}",
                "track": "mic",
                "start_ms": i * 1000,
                "end_ms": (i + 1) * 1000,
                "text": text,
                "speaker": f"S{i + 1}",
                "provisional": True,
                "multiple_speakers": False,
            }
            for i, text in enumerate(["первый", "второй"])
        ],
    }


def test_replace_retry_same_answer_without_disk_writes(rig: Rig) -> None:
    event = payload()
    rig.job_event("meeting.job.replace", **event)
    once = rig.jobs.job_snapshot(MID)
    assert once is not None
    assert [u["text"] for u in once["utterances"]] == ["первый", "следующий", "второй"]
    assert once["transcript_revision"] == 11
    ack1 = rig.role.last("meeting.job.applied")
    rig.job_event("meeting.job.replace", **event)
    assert rig.jobs.job_snapshot(MID) == once
    ack2 = rig.role.last("meeting.job.applied")
    assert ack1["seq"] == ack2["seq"] == 11
    assert ack1["revision"] == ack2["revision"] == 11
    assert not (rig.store.root / MID / "transcript.json").exists()


@pytest.mark.parametrize("conflict", ["revision", "unknown", "duplicate-id", "empty", "same-seq"])
def test_refuse_without_changing_feed(rig: Rig, conflict: str) -> None:
    event = payload()
    if conflict == "same-seq":
        rig.job_event("meeting.job.replace", **event)
        event["items"][0]["text"] = "подмена"
    elif conflict == "revision":
        event["expected_revision"] = 9
    elif conflict == "unknown":
        event["replaces_id"] = "missing"
    elif conflict == "duplicate-id":
        event["items"][0]["id"] = "u00001"
    else:
        event["items"] = []
    before = copy.deepcopy(rig.jobs.job_snapshot(MID))
    sent = len(rig.role.sent)
    rig.job_event("meeting.job.replace", **event)
    assert rig.jobs.job_snapshot(MID) == before
    assert len(rig.role.sent) == sent


def test_resume_from_committed_pending_does_not_replace_again(rig: Rig) -> None:
    active = rig.jobs._active
    assert active is not None
    items = [Utterance("r0", text="первый"), Utterance("r1", seq=1, text="второй")]
    transcript = Transcript(
        MID,
        meeting_generation=active.generation,
        transcript_revision=11,
        utterances=items,
        reconcile=ReconcileProgress(
            status=PhaseStatus.RUNNING,
            done_ids=["u00000"],
            pending=ReconcileReplacement(11, "u00000", 10, items),
        ),
    )
    rig.store.write(MID, "transcript.json", transcript, writer=Writer.TRANSCRIBE)
    rig.jobs._restore_live(active)
    frame = payload()
    frame["items"] = [
        {
            "id": u.id,
            "track": u.track,
            "start_ms": u.start_ms,
            "end_ms": u.end_ms,
            "text": u.text,
            "speaker": u.speaker,
            "provisional": True,
            "multiple_speakers": False,
        }
        for u in items
    ]
    before = rig.jobs.job_snapshot(MID)
    rig.job_event("meeting.job.replace", **frame)
    assert rig.jobs.job_snapshot(MID) == before
    assert rig.role.last("meeting.job.applied")["revision"] == 11
    assert rig.store.read(MID, "transcript.json") == transcript


def test_reconcile_failure_starts_audio_retention(rig: Rig) -> None:
    rig.retention = 0
    rig.job_event(
        "meeting.job.done",
        utterances=2,
        speakers=2,
        diar_status="done",
        wall_ms=10,
        peak_rss_kb=1,
        echo_dropped=0,
        transcript_revision=11,
        reconcile_failed=True,
    )
    rig.jobs.tick()
    meeting = rig.meeting()
    assert meeting.processing_status == ProcessingStatus.PARTIAL
    assert meeting.transcription.diar_status == "done"
    assert not meeting.audio.present and not meeting.cleanup_pending
