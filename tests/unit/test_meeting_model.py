"""E13 M1: disk contract, including every applicable row of the R5 table."""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from typing import Any

import pytest

from astra_voice.meetings import events, model, segments, timeline

pytestmark = pytest.mark.unit
MEETING_ID = "2026-10-29_120000-ab12"
STAMP = "2026-10-29T12:00:00+00:00"
HASH = "a" * 64


def document(kind: str) -> model.JsonModel:
    factories: dict[str, Callable[[], model.JsonModel]] = {
        "meeting": lambda: model.Meeting(id=MEETING_ID, started_at=STAMP),
        "transcript": lambda: model.Transcript(meeting_id=MEETING_ID),
        "checkpoint": lambda: model.Checkpoint(meeting_id=MEETING_ID),
        "segment": lambda: segments.SegmentSidecar(meeting_id=MEETING_ID, epoch_id="epoch-1"),
        "speakers": model.SpeakerMap,
    }
    return factories[kind]()


# R5 row name, disk document, dotted field, non-default value.
M1_DISK_ROWS = [
    ("meeting_generation", "meeting", "meeting_generation", 9),
    ("meeting_generation", "transcript", "meeting_generation", 9),
    ("meeting_generation", "checkpoint", "meeting_generation", 9),
    ("meeting_generation", "segment", "meeting_generation", 9),
    ("capture_id/state_rev", "segment", "capture_id", "capture-2"),
    ("capture_id/state_rev", "segment", "state_rev", 9),
    ("manual_resume_required/blocked_epoch", "segment", "manual_resume_required", True),
    ("manual_resume_required/blocked_epoch", "segment", "blocked_epoch", 9),
    ("pause_reasons", "segment", "pause_reasons", ["lease", "lock"]),
    (
        "seq",
        "transcript",
        "utterances",
        [
            {
                "id": "u1",
                "seq": 8,
                "track": "far",
                "start_ms": 10,
                "end_ms": 20,
                "text": "Реплика",
                "speaker": "S1",
                "final": True,
                "echo": False,
            }
        ],
    ),
    ("transcript_revision", "transcript", "transcript_revision", 7),
    ("transcript_revision", "checkpoint", "transcript_revision", 7),
    ("input_manifest_sha256", "checkpoint", "input_manifest_sha256", HASH),
    ("processing_config_sha256", "checkpoint", "processing_config_sha256", HASH),
    ("diar.turns", "checkpoint", "diar.turns", [{"label": "S1", "start_ms": 10, "end_ms": 20}]),
    ("diar.label_map", "checkpoint", "diar.label_map", {"S2": "S1"}),
    ("diar.status/reconcile.status", "checkpoint", "diar.status", "running"),
    ("diar.status/reconcile.status", "checkpoint", "reconcile.status", "running"),
    ("reconcile.*", "checkpoint", "reconcile.done_ids", ["u1", "u2"]),
    ("reconcile.*", "checkpoint", "reconcile.used_ms", 100),
    ("reconcile.*", "checkpoint", "reconcile.budget_ms", 150),
    ("epoch_id", "segment", "epoch_id", "epoch-2"),
    ("clock/clock_anchor", "segment", "clock", "pipewire"),
    ("clock/clock_anchor", "segment", "clock_anchor", {"server_ms": 20, "t_mono_ms": 30}),
    ("sample_start/sample_end", "segment", "sample_start", 16000),
    ("sample_start/sample_end", "segment", "sample_end", 16000),
    ("discontinuity/close_reason", "segment", "discontinuity", True),
    ("discontinuity/close_reason", "segment", "close_reason", "lease"),
    ("processing_status/cancelled", "meeting", "processing_status", "partial"),
    ("processing_status/cancelled", "meeting", "cancelled", True),
    ("cleanup_pending", "meeting", "cleanup_pending", True),
    ("audio/transcription timestamps", "meeting", "audio.present", False),
    ("audio/transcription timestamps", "meeting", "audio.deleted_at", STAMP),
    ("audio/transcription timestamps", "meeting", "transcription.finished_at", STAMP),
]


def set_field(data: dict[str, Any], path: str, value: object) -> None:
    parts = path.split(".")
    for name in parts[:-1]:
        data = data[name]
    data[parts[-1]] = value


@pytest.mark.parametrize(
    "row,kind,path,value", M1_DISK_ROWS, ids=[f"{r[0]}:{r[1]}:{r[2]}" for r in M1_DISK_ROWS]
)
def test_m1_disk_row_roundtrip(row: str, kind: str, path: str, value: object) -> None:
    instance = document(kind)
    data = instance.to_dict()
    set_field(data, path, value)
    if kind == "checkpoint" and path.startswith("reconcile."):
        data["reconcile"]["speech_ms"] = 10000
        if path != "reconcile.budget_ms":
            data["reconcile"]["budget_ms"] = 1500
    if kind == "segment" and path in {"sample_start", "sample_end"}:
        data.update(sample_start=16000, sample_end=16000)
    restored = type(instance).from_dict(json.loads(json.dumps(data)))
    assert restored.to_dict() == data, row


@pytest.mark.parametrize("kind", ["meeting", "transcript", "checkpoint", "segment", "speakers"])
def test_all_fields_roundtrip_and_unknown_fields_rejected(kind: str) -> None:
    instance = document(kind)
    raw = instance.to_dict()
    assert type(instance).from_dict(raw) == instance
    raw["unexpected"] = "PRIVATE_MARKER"
    with pytest.raises(model.MeetingValidationError) as caught:
        type(instance).from_dict(raw)
    assert "PRIVATE_MARKER" not in str(caught.value)


@pytest.mark.parametrize(
    "kind,path,limit",
    [
        ("meeting", "title", 120),
        ("meeting", "self_label", 40),
        ("speakers", "labels.S1.name", 60),
    ],
)
def test_text_limits(kind: str, path: str, limit: int) -> None:
    instance = document(kind)
    raw = instance.to_dict()
    if kind == "speakers":
        raw["labels"] = {"S1": {"name": None, "merged_into": None}}
    set_field(raw, path, "я" * limit)
    assert type(instance).from_dict(raw).to_dict() == raw
    set_field(raw, path, "я" * (limit + 1))
    with pytest.raises(model.MeetingValidationError):
        type(instance).from_dict(raw)


def test_nfc_normalizes_before_counting() -> None:
    meeting = model.Meeting(id=MEETING_ID, started_at=STAMP, title="е\u0308" * 120)
    assert meeting.to_dict()["title"] == "ё" * 120
    assert model.Meeting.from_dict(meeting.to_dict()).title == "ё" * 120


@pytest.mark.parametrize("char", ["\x00", "\x1b", "\x7f", "\u200b", "\u202e", "\ufeff", "\n", "\t"])
@pytest.mark.parametrize(
    "kind,path", [("meeting", "title"), ("meeting", "self_label"), ("speakers", "labels.S1.name")]
)
def test_controls_rejected_in_names(char: str, kind: str, path: str) -> None:
    instance = document(kind)
    data = instance.to_dict()
    if kind == "speakers":
        data["labels"] = {"S1": {"name": None}}
    set_field(data, path, f"A{char}B")
    with pytest.raises(model.MeetingValidationError):
        type(instance).from_dict(data)


def test_utterance_text_allows_lf_tab_only_and_no_acoustic_features() -> None:
    utterance = model.Utterance(id="u1", text="Первая\n\tвторая", end_ms=10)
    assert utterance.to_dict()["text"] == "Первая\n\tвторая"
    for char in ("\r", "\x00", "\u202e"):
        with pytest.raises(model.MeetingValidationError):
            model.Utterance(id="u1", text=char).to_dict()
    for kind in ("checkpoint", "transcript"):
        instance = document(kind)
        for key in ("embedding", "pcm", "features", "vector"):
            raw = instance.to_dict()
            raw[key] = [0.1, 0.2]
            with pytest.raises(model.MeetingValidationError):
                type(instance).from_dict(raw)
    raw = document("checkpoint").to_dict()
    raw["diar"]["embedding"] = [0.1]
    with pytest.raises(model.MeetingValidationError):
        model.Checkpoint.from_dict(raw)


@pytest.mark.parametrize(
    "kind,path,value",
    [
        ("meeting", "meeting_generation", -1),
        ("meeting", "meeting_generation", True),
        ("meeting", "meeting_generation", 2**64),
        ("meeting", "recorded_ms", 14400001),
        ("meeting", "limit_ms", 7200001),
        ("meeting", "app_class", "raw-process-name"),
        ("meeting", "processing_status", "unknown"),
        ("meeting", "state", "unknown"),
        ("meeting", "started_at", "yesterday"),
        ("meeting", "started_at", "2026-02-31T12:00:00Z"),
        ("meeting", "ended_at", "yesterday"),
        ("meeting", "id", "../escape"),
        ("checkpoint", "input_manifest_sha256", "x" * 64),
        ("checkpoint", "processing_config_sha256", "a" * 63),
        ("checkpoint", "diar.threshold", float("nan")),
        ("checkpoint", "diar.tau", float("inf")),
        ("checkpoint", "diar.status", "failed"),
        ("segment", "pause_reasons", ["lease", "lease"]),
        ("segment", "clock", "buffer_arrival"),
        ("segment", "sample_end", -1),
        ("segment", "frames_at_checkpoint", 1),
        ("segment", "meeting_generation", True),
        ("segment", "meeting_generation", -1),
        ("segment", "capture_id", "../escape"),
        ("segment", "state_rev", -1),
        ("segment", "state_rev", 1.0),
        ("segment", "state_rev", 2**64),
        ("segment", "capture_id", ""),
        ("segment", "capture_id", "x" * 65),
        ("segment", "manual_resume_required", 1),
        ("segment", "blocked_epoch", True),
        ("checkpoint", "diar.turns", [{"label": "S1", "start_ms": 20, "end_ms": 10}]),
        ("checkpoint", "reconcile.done_ids", ["u1", "u1"]),
        ("segment", "timeline", [[0, 0], [0, 1]]),
    ],
)
def test_invalid_disk_fields(kind: str, path: str, value: object) -> None:
    instance = document(kind)
    raw = instance.to_dict()
    set_field(raw, path, value)
    with pytest.raises(model.MeetingValidationError):
        type(instance).from_dict(raw)


def test_cross_field_ranges_and_reconcile_budget() -> None:
    with pytest.raises(model.MeetingValidationError):
        model.Utterance(id="u1", start_ms=20, end_ms=10).to_dict()
    raw = document("checkpoint").to_dict()
    raw["reconcile"].update(speech_ms=1000, budget_ms=151)
    with pytest.raises(model.MeetingValidationError):
        model.Checkpoint.from_dict(raw)
    raw["reconcile"].update(budget_ms=150, used_ms=151)
    with pytest.raises(model.MeetingValidationError):
        model.Checkpoint.from_dict(raw)


def test_checkpoint_compatibility_requires_revision_identity_hashes_and_model() -> None:
    checkpoint = model.Checkpoint(meeting_id=MEETING_ID)
    transcript = model.Transcript(meeting_id=MEETING_ID)
    assert checkpoint.compatible_with(
        transcript,
        input_manifest_sha256="0" * 64,
        processing_config_sha256="0" * 64,
        model=checkpoint.model,
    )
    for field, value in (
        ("transcript_revision", 1),
        ("meeting_generation", 1),
        ("meeting_id", "2026-10-29_120001-ab12"),
    ):
        raw = transcript.to_dict()
        raw[field] = value
        assert not checkpoint.compatible_with(
            model.Transcript.from_dict(raw),
            input_manifest_sha256="0" * 64,
            processing_config_sha256="0" * 64,
            model=checkpoint.model,
        )
    for key in ("input_manifest_sha256", "processing_config_sha256"):
        kwargs = {"input_manifest_sha256": "0" * 64, "processing_config_sha256": "0" * 64}
        kwargs[key] = HASH
        assert not checkpoint.compatible_with(transcript, model=checkpoint.model, **kwargs)
    changed = copy.deepcopy(checkpoint.model)
    changed.revision = "new"
    assert not checkpoint.compatible_with(
        transcript, input_manifest_sha256="0" * 64, processing_config_sha256="0" * 64, model=changed
    )


def test_lifecycle_and_tombstone_are_terminal() -> None:
    assert events.transition(events.MeetingState.RECORDING, events.MeetingState.PAUSED)
    assert events.transition(events.MeetingState.PAUSED, events.MeetingState.RECORDED)
    assert events.transition(events.MeetingState.RECORDED, events.MeetingState.TRANSCRIBING)
    for target in events.MeetingState:
        with pytest.raises(model.MeetingValidationError):
            events.transition(events.MeetingState.DELETING, target)
    with pytest.raises(model.MeetingValidationError):
        events.transition(events.MeetingState.DONE, events.MeetingState.RECORDING)


def test_timeline_data_does_not_interpolate_through_discontinuity() -> None:
    sidecar = segments.SegmentSidecar(
        meeting_id=MEETING_ID,
        epoch_id="e1",
        sample_end=16000,
        frames=16000,
        timeline=[[0, 100], [16000, 1100]],
    )
    assert timeline.sample_to_monotonic(sidecar, 8000) == 600
    sidecar.discontinuity = True
    with pytest.raises(model.MeetingValidationError):
        timeline.sample_to_monotonic(sidecar, 8000)
    sidecar.discontinuity = False
    with pytest.raises(model.MeetingValidationError):
        timeline.sample_to_monotonic(sidecar, 16001)


def test_model_and_store_share_document_byte_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    from astra_voice.meetings import store

    assert model.MAX_DOCUMENT_BYTES == store.MAX_DOCUMENT_BYTES
    monkeypatch.setattr(model, "MAX_DOCUMENT_BYTES", 1024)
    transcript = model.Transcript(
        meeting_id=MEETING_ID,
        utterances=[model.Utterance(id=f"u{i}", seq=i, text="я" * 300) for i in range(3)],
    )
    with pytest.raises(model.MeetingValidationError):
        transcript.to_dict()


def test_nested_intervals_and_marks_run_cross_field_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    def interval_check(cls: type[model.Interval], data: dict[str, Any]) -> None:
        seen.append("interval")

    def mark_check(cls: type[model.Mark], data: dict[str, Any]) -> None:
        seen.append("mark")

    monkeypatch.setattr(model.Interval, "_check", classmethod(interval_check))
    monkeypatch.setattr(model.Mark, "_check", classmethod(mark_check))
    model.Meeting(
        id=MEETING_ID, started_at=STAMP, intervals=[model.Interval()], marks=[model.Mark()]
    ).to_dict()
    assert seen == ["interval", "mark"]


def test_ai_attempt_default_timestamp_is_valid() -> None:
    attempt = model.AiAttempt(host="example.org")
    assert attempt.to_dict()["at"]


@pytest.mark.parametrize(
    "current,target",
    [("recording", events.MeetingState.PAUSED), (events.MeetingState.RECORDING, "paused")],
)
def test_transition_requires_enum_instances(current: Any, target: Any) -> None:
    with pytest.raises(model.MeetingValidationError):
        events.transition(current, target)


def test_timeline_snapshot_validates_once(monkeypatch: pytest.MonkeyPatch) -> None:
    segment = segments.SegmentSidecar(
        meeting_id=MEETING_ID,
        epoch_id="e1",
        sample_end=16000,
        frames=16000,
        timeline=[[0, 100], [16000, 1100]],
    )
    calls: list[int] = []
    original = segment.to_dict

    def validate_once() -> dict[str, Any]:
        calls.append(1)
        return original()

    monkeypatch.setattr(segment, "to_dict", validate_once)
    view = timeline.SegmentTimeline.from_segment(segment)
    assert view.sample_to_monotonic(8000) == 600
    assert view.sample_to_monotonic(16000) == 1100
    segment.timeline[0][1] = 9000
    assert view.sample_to_monotonic(8000) == 600
    assert calls == [1]
