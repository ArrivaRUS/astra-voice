"""Atomic card snapshot replacement through the same live seam as MeetingJobs."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest
from test_tray import qapp as qapp

from astra_voice.meetings.model import Meeting, Utterance
from astra_voice.ui.meeting_card import CardRows, TranscriptRows, snapshot_utterances

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("filesystem_euid")]


def test_card_replaces_old_row_and_repeated_snapshot_is_idempotent(qapp: object) -> None:
    meeting = Meeting("2026-11-10_120000-aaaa", started_at="2026-11-10T12:00:00Z")
    model = TranscriptRows()
    builder = CardRows(meeting, model)
    old = [Utterance("old", text="первый второй", start_ms=0, end_ms=2000)]
    builder.load(old, complete=False)
    replacement = [
        Utterance("a", text="первый", start_ms=0, end_ms=1000),
        Utterance("b", seq=1, text="второй", start_ms=1000, end_ms=2000),
    ]
    builder.sync(replacement)
    assert model.count == 2 and [r["text"] for r in model.rows()] == ["первый", "второй"]
    rows = model.rows()
    builder.sync(replacement)
    assert model.rows() == rows and len(builder.utterances) == 2
    assert builder.speech == 2
    # Invisible marker survives the live model without changing presentation.
    marked = [dataclasses.replace(u, multiple_speakers=True) for u in replacement]
    builder.sync(marked)
    assert all(u.multiple_speakers for u in builder.utterances)
    assert model.rows() == rows
    assert snapshot_utterances({"utterances": [u.to_dict() for u in marked]}) == marked


def test_bridge_live_replacement_retry_and_refusal(tmp_path: Path, qapp: object) -> None:
    from test_meeting_card_bridge import SaveRig
    from test_meeting_jobs_ui import MID

    rig = SaveRig(tmp_path)
    try:
        rig.create()
        rig.save()
        rig.start_running()
        rig.open_card()
        start = next(m for m in rig.role.sent if m["type"] == "meeting.job.start")
        identity = {"meeting_id": MID, "meeting_generation": start["meeting_generation"]}
        rig.role.emit(
            "meeting.job.utterance",
            **identity,
            seq=0,
            id="source",
            track="far",
            start_ms=0,
            end_ms=2000,
            text="первый второй",
            speaker="S1",
            provisional=True,
        )
        rig.role.emit(
            "meeting.job.labels",
            **identity,
            seq=0,
            items=[["source", "S1"]],
            final=False,
            speakers_total=1,
            transcript_revision=10,
        )
        rig.recording.tick()
        assert [r["text"] for r in rig.bridge.cardRows.rows()] == ["первый второй"]
        items = [
            {
                "id": f"r{i}",
                "track": "far",
                "start_ms": i * 1000,
                "end_ms": (i + 1) * 1000,
                "text": text,
                "speaker": f"S{i + 1}",
                "provisional": True,
            }
            for i, text in enumerate(["первый", "второй"])
        ]
        frame = {
            **identity,
            "seq": 11,
            "replaces_id": "source",
            "expected_revision": 10,
            "items": items,
        }
        rig.role.emit("meeting.job.replace", **frame)
        rig.recording.tick()
        expected = rig.bridge.cardRows.rows()
        assert [r["text"] for r in expected] == ["первый", "второй"]
        rig.role.emit("meeting.job.replace", **frame)
        rig.recording.tick()
        assert rig.bridge.cardRows.rows() == expected
        rig.role.emit(
            "meeting.job.replace",
            **{**frame, "seq": 12, "replaces_id": "missing", "expected_revision": 11},
        )
        rig.recording.tick()
        assert rig.bridge.cardRows.rows() == expected
        assert len([m for m in rig.role.sent if m["type"] == "meeting.job.applied"]) == 2
        assert not (rig.store.root / MID / "transcript.json").exists()
    finally:
        rig.bridge.shutdown()
