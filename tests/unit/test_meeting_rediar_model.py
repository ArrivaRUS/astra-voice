"""5в-1: backwards-compatible metadata and generation/finality contracts."""

from __future__ import annotations

import json

import pytest

from astra_voice.meetings import model, speakers

pytestmark = pytest.mark.unit
ID = "2026-10-29_120000-ab12"
STAMP = "2026-10-29T12:00:00+00:00"


def test_legacy_metadata_and_explicit_null_are_equivalent() -> None:
    meeting = model.Meeting(id=ID, started_at=STAMP)
    raw = meeting.to_dict()
    assert raw.pop("rediar") is None
    assert raw.pop("transcript_generation") is None
    assert model.Meeting.from_dict(raw) == meeting
    raw["rediar"] = None
    assert model.Meeting.from_dict(raw) == meeting
    names = model.SpeakerMap().to_dict()
    assert names.pop("transcript_generation") is None
    assert model.SpeakerMap.from_dict(names) == model.SpeakerMap()


@pytest.mark.parametrize("rediar", [None, model.Rediar(generation=4, base_sha256="a" * 64)])
def test_new_metadata_roundtrip(rediar: model.Rediar | None) -> None:
    meeting = model.Meeting(
        id=ID, started_at=STAMP, meeting_generation=5, transcript_generation=3, rediar=rediar
    )
    assert model.Meeting.from_dict(json.loads(json.dumps(meeting.to_dict()))) == meeting
    names = model.SpeakerMap(labels={"S1": model.Speaker(name="Имя")}, transcript_generation=3)
    assert model.SpeakerMap.from_dict(json.loads(json.dumps(names.to_dict()))) == names


@pytest.mark.parametrize("cls", [model.Meeting, model.SpeakerMap])
def test_legacy_required_fields_remain_required(cls: type[model.JsonModel]) -> None:
    instance = model.Meeting(id=ID, started_at=STAMP) if cls is model.Meeting else cls()
    legacy = instance.to_dict()
    legacy.pop("transcript_generation")
    legacy.pop("rediar", None)
    assert set(cls.SCHEMA["required"]) == set(legacy)
    for field in legacy:
        missing = dict(legacy)
        del missing[field]
        with pytest.raises(model.MeetingValidationError):
            cls.from_dict(missing)


@pytest.mark.parametrize(
    "generation,published,rediar",
    [(3, 4, None), (3, 1, 4), (3, None, 3), (3, 3, 3), (4, 3, 2)],
)
def test_meeting_cross_field_checks(
    generation: int, published: int | None, rediar: int | None
) -> None:
    meeting = model.Meeting(
        id=ID,
        started_at=STAMP,
        meeting_generation=generation,
        transcript_generation=published,
        rediar=None if rediar is None else model.Rediar(rediar, "a" * 64),
    )
    with pytest.raises(model.MeetingValidationError):
        meeting.to_dict()


@pytest.mark.parametrize("generation", [None, 0, 3, 5])
def test_published_generation(generation: int | None) -> None:
    meeting = model.Meeting(
        id=ID, started_at=STAMP, meeting_generation=5, transcript_generation=generation
    )
    before = meeting.to_dict()
    assert model.published_generation(meeting) == (5 if generation is None else generation)
    assert meeting.to_dict() == before


@pytest.mark.parametrize("field", ["generation", "base_sha256"])
@pytest.mark.parametrize("value", [None, True, -1, 1.0, "PRIVATE_MARKER"])
def test_rediar_strict_fields(field: str, value: object) -> None:
    raw = model.Rediar(1, "a" * 64).to_dict()
    raw[field] = value
    with pytest.raises(model.MeetingValidationError) as caught:
        model.Rediar.from_dict(raw)
    assert "PRIVATE_MARKER" not in str(caught.value)


@pytest.mark.parametrize("field", ["generation", "base_sha256"])
def test_rediar_requires_both_fields(field: str) -> None:
    raw = model.Rediar(1, "a" * 64).to_dict()
    del raw[field]
    with pytest.raises(model.MeetingValidationError):
        model.Rediar.from_dict(raw)


@pytest.mark.parametrize("kind", ["meeting", "rediar", "speakers"])
def test_new_metadata_rejects_unknown_fields(kind: str) -> None:
    instance = {
        "meeting": model.Meeting(id=ID, started_at=STAMP),
        "rediar": model.Rediar(1, "a" * 64),
        "speakers": model.SpeakerMap(),
    }[kind]
    raw = instance.to_dict()
    raw["embeddings"] = [0.1]
    with pytest.raises(model.MeetingValidationError):
        type(instance).from_dict(raw)


@pytest.mark.parametrize("unfinished", [0, 1])
def test_final_labels_require_every_utterance_final(unfinished: int) -> None:
    transcript = model.Transcript(
        meeting_id=ID,
        utterances=[model.Utterance(id=f"u{i}", seq=i, final=True) for i in range(2)],
        labels_final=True,
    )
    raw = transcript.to_dict()
    raw["utterances"][unfinished]["final"] = False
    with pytest.raises(model.MeetingValidationError):
        model.Transcript.from_dict(raw)
    transcript.utterances[unfinished].final = False
    with pytest.raises(model.MeetingValidationError):
        transcript.to_dict()


@pytest.mark.parametrize("generation", [None, 0, 4])
@pytest.mark.parametrize("method", ["speaker_map", "validated_map"])
def test_name_processing_preserves_generation(generation: int | None, method: str) -> None:
    saved = model.SpeakerMap(
        labels={"S1": model.Speaker(name="Имя"), "S2": model.Speaker(merged_into="S1")},
        transcript_generation=generation,
    )
    if method == "speaker_map":
        result = speakers.speaker_map([model.Utterance(id="u1", speaker="S1")], saved)
    else:
        result = speakers.validated_map(["S1", "S2"], saved)
    assert result.transcript_generation == generation
    assert result.labels["S1"].name == "Имя"
    assert result.to_dict()["transcript_generation"] == generation


def test_name_processing_without_saved_map() -> None:
    assert speakers.validated_map(["S1"], None).transcript_generation is None
