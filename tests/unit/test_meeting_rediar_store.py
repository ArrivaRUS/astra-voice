"""5в-1: real private files, independent revision streams and transaction guards."""

from __future__ import annotations

import fcntl
import json
import os
import stat
from pathlib import Path

import pytest

from astra_voice.meetings import model, store
from astra_voice.meetings.events import MeetingState

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("filesystem_euid")]
ID = "2026-10-29_120000-ab12"
STAMP = "2026-10-29T12:00:00+00:00"
WORK = ("rediar-transcript.json", "rediar-checkpoint.json")


def document(
    name: str, *, generation: int = 2, revision: int = 10
) -> model.Transcript | model.Checkpoint:
    cls = model.Transcript if name.endswith("transcript.json") else model.Checkpoint
    return cls(meeting_id=ID, meeting_generation=generation, transcript_revision=revision)


def begin_rediar(disk: store.MeetingStore) -> None:
    meeting = disk.read(ID, "meeting.json")
    meeting.transcript_generation = 1
    meeting.meeting_generation = 2
    meeting.rediar = model.Rediar(2, "a" * 64)
    disk.write(ID, "meeting.json", meeting, writer=store.Writer.GUI)


@pytest.fixture
def disk(tmp_path: Path) -> store.MeetingStore:
    disk = store.MeetingStore(tmp_path / "meetings")
    disk.create(
        model.Meeting(id=ID, started_at=STAMP, state=MeetingState.DONE, meeting_generation=1)
    )
    disk.write(
        ID,
        "transcript.json",
        model.Transcript(
            meeting_id=ID, meeting_generation=1, transcript_revision=100, labels_final=True
        ),
        writer=store.Writer.TRANSCRIBE,
    )
    disk.write(ID, "speakers.json", model.SpeakerMap(), writer=store.Writer.GUI)
    return disk


@pytest.fixture
def redo(disk: store.MeetingStore) -> store.MeetingStore:
    begin_rediar(disk)
    return disk


def published_bytes(disk: store.MeetingStore) -> tuple[bytes, bytes]:
    directory = disk.root / ID
    return (directory / "transcript.json").read_bytes(), (directory / "speakers.json").read_bytes()


def put_raw(disk: store.MeetingStore, name: str, raw: bytes) -> Path:
    target = disk.root / ID / name
    target.write_bytes(raw)
    target.chmod(0o600)
    return target


def test_legacy_and_new_metadata_on_disk(disk: store.MeetingStore) -> None:
    meeting = disk.read(ID, "meeting.json").to_dict()
    meeting.pop("rediar")
    meeting.pop("transcript_generation")
    put_raw(disk, "meeting.json", json.dumps(meeting).encode())
    put_raw(disk, "speakers.json", b'{"schema":1,"labels":{},"renumbered":false}')
    assert disk.read(ID, "meeting.json").rediar is None
    assert disk.read(ID, "speakers.json").transcript_generation is None
    begin_rediar(disk)
    restored = disk.read(ID, "meeting.json")
    assert restored.rediar == model.Rediar(2, "a" * 64)
    assert restored.transcript_generation == 1


@pytest.mark.parametrize("name", ["transcript.json", "checkpoint.json"])
@pytest.mark.parametrize("role", list(store.Writer))
def test_i1_blocks_every_role(redo: store.MeetingStore, name: str, role: store.Writer) -> None:
    before = published_bytes(redo)
    with pytest.raises(store.MeetingStoreError):
        redo.write(ID, name, document(name), writer=role)
    assert published_bytes(redo) == before
    assert not (redo.root / ID / "checkpoint.json").exists()


@pytest.mark.parametrize("name", ["transcript.json", "checkpoint.json"])
def test_i1_rechecks_prepared_document_after_enqueue(disk: store.MeetingStore, name: str) -> None:
    with disk.writer(ID, store.Writer.TRANSCRIBE) as writer:
        prepared = writer.prepare(name, document(name, generation=1, revision=100))
        begin_rediar(disk)
        before = published_bytes(disk)
        with pytest.raises(store.MeetingStoreError):
            writer.write(name, prepared)
    assert published_bytes(disk) == before


@pytest.mark.parametrize("name", WORK)
@pytest.mark.parametrize("role", [store.Writer.GUI, store.Writer.CAPTURE, store.Writer.DIAR])
def test_i2_only_transcribe_writes_work(
    redo: store.MeetingStore, name: str, role: store.Writer
) -> None:
    with pytest.raises(store.MeetingStoreError):
        redo.write(ID, name, document(name), writer=role)
    assert not (redo.root / ID / name).exists()


@pytest.mark.parametrize("name", WORK)
@pytest.mark.parametrize("violation", ["state", "descriptor", "counter", "generation", "id"])
def test_i2_requires_current_descriptor_and_done_state(
    redo: store.MeetingStore, name: str, violation: str
) -> None:
    meeting = redo.read(ID, "meeting.json")
    if violation == "state":
        # Prepare the fixture directly: DONE -> RECORDED is deliberately illegal.
        meeting.state = MeetingState.RECORDED
    elif violation == "descriptor":
        meeting.rediar = None
    elif violation == "counter":
        meeting.meeting_generation = 3
    put_raw(redo, "meeting.json", json.dumps(meeting.to_dict()).encode())
    payload = document(name, generation=3 if violation == "generation" else 2)
    if violation == "id":
        payload.meeting_id = "2026-10-29_130000-ab12"
    before = published_bytes(redo)
    with pytest.raises(store.MeetingStoreError):
        redo.write(ID, name, payload, writer=store.Writer.TRANSCRIBE)
    assert published_bytes(redo) == before
    assert not (redo.root / ID / name).exists()


@pytest.mark.parametrize("name", WORK)
def test_i2_rechecks_prepared_document_after_cancellation(
    redo: store.MeetingStore, name: str
) -> None:
    with redo.writer(ID, store.Writer.TRANSCRIBE) as writer:
        if name.endswith("checkpoint.json"):
            writer.write(WORK[0], document(WORK[0]))
        prepared = writer.prepare(name, document(name))
        meeting = redo.read(ID, "meeting.json")
        meeting.rediar = None
        redo.write(ID, "meeting.json", meeting, writer=store.Writer.GUI)
        with pytest.raises(store.MeetingStoreError):
            writer.write(name, prepared)
    assert not (redo.root / ID / name).exists()


def test_work_checkpoint_uses_work_text_and_separate_revision_cache(
    redo: store.MeetingStore,
) -> None:
    before = published_bytes(redo)
    with redo.writer(ID, store.Writer.TRANSCRIBE) as writer:
        published = writer.read_transcript()
        assert published is not None and published.transcript_revision == 100
        assert writer.read_transcript(WORK[0]) is None
        with pytest.raises(store.MeetingStoreError):
            writer.write(WORK[1], document(WORK[1], revision=100))
        writer.write(WORK[0], document(WORK[0], revision=101))
        writer.write(WORK[1], document(WORK[1], revision=101))
        # Reading the published stream must not replace the work stream cache.
        published = writer.read_transcript()
        assert published is not None and published.transcript_revision == 100
        with pytest.raises(store.MeetingStoreError):
            writer.write(WORK[1], document(WORK[1], revision=100))
        working = writer.read_transcript(WORK[0])
        assert working is not None and working.transcript_revision == 101
        # A reseed in the same generation continues the previous revision.
        with pytest.raises(store.MeetingStoreError):
            writer.write(WORK[0], document(WORK[0], revision=1))
        writer.write(WORK[0], document(WORK[0], revision=102))
        writer.write(WORK[1], document(WORK[1], revision=102))
    # A new lease also reads the work file, rather than using the published revision.
    with redo.writer(ID, store.Writer.TRANSCRIBE) as writer:
        with pytest.raises(store.MeetingStoreError):
            writer.write(WORK[0], document(WORK[0], revision=101))
        writer.write(WORK[0], document(WORK[0], revision=103))
    assert redo.read(ID, WORK[1]).transcript_revision == 102
    assert published_bytes(redo) == before
    for name in WORK:
        assert stat.S_IMODE((redo.root / ID / name).stat().st_mode) == 0o600
    assert stat.S_IMODE((redo.root / ID).stat().st_mode) == 0o700


def test_work_and_published_revisions_do_not_conflict(redo: store.MeetingStore) -> None:
    # Independent streams: published revision 100 does not forbid work revision 1.
    redo.write(ID, WORK[0], document(WORK[0], revision=1), writer=store.Writer.TRANSCRIBE)
    redo.write(ID, WORK[1], document(WORK[1], revision=1), writer=store.Writer.TRANSCRIBE)
    assert redo.read(ID, "transcript.json").transcript_revision == 100


@pytest.mark.parametrize("bound_generation", [None, 0, 1, 2])
def test_i3_no_name_writes_during_rediar(
    redo: store.MeetingStore, bound_generation: int | None
) -> None:
    before = published_bytes(redo)
    with pytest.raises(store.MeetingStoreError):
        redo.write(
            ID,
            "speakers.json",
            model.SpeakerMap(transcript_generation=bound_generation),
            writer=store.Writer.GUI,
        )
    assert published_bytes(redo) == before


def test_i3_rechecks_prepared_names(disk: store.MeetingStore) -> None:
    with disk.writer(ID, store.Writer.GUI) as writer:
        prepared = writer.prepare("speakers.json", model.SpeakerMap())
        meeting = disk.read(ID, "meeting.json")
        meeting.transcript_generation = 1
        meeting.meeting_generation = 2
        meeting.rediar = model.Rediar(2, "a" * 64)
        writer.write("meeting.json", meeting)
        with pytest.raises(store.MeetingStoreError):
            writer.write("speakers.json", prepared)


@pytest.mark.parametrize("generation", [None, 0, 1, 2])
def test_i3_names_bind_to_published_generation(
    disk: store.MeetingStore, generation: int | None
) -> None:
    meeting = disk.read(ID, "meeting.json")
    meeting.meeting_generation = 2
    meeting.transcript_generation = 1  # A cancelled redo left the old publication.
    disk.write(ID, "meeting.json", meeting, writer=store.Writer.GUI)
    names = model.SpeakerMap(transcript_generation=generation)
    if generation in (None, 1):
        disk.write(ID, "speakers.json", names, writer=store.Writer.GUI)
        assert disk.read(ID, "speakers.json") == names
    else:
        with pytest.raises(store.MeetingStoreError):
            disk.write(ID, "speakers.json", names, writer=store.Writer.GUI)


@pytest.mark.parametrize("name", WORK)
def test_prepared_work_publication_requires_store_lock(redo: store.MeetingStore, name: str) -> None:
    with redo.writer(ID, store.Writer.TRANSCRIBE) as writer:
        writer.write(WORK[0], document(WORK[0], revision=9))
        prepared = writer.prepare(name, document(name, revision=9))
        with (redo.root / ID / ".store.lock").open("rb") as gate:
            fcntl.flock(gate, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with pytest.raises(store.MeetingStoreError):
                writer.write(name, prepared)
        writer.write(name, prepared)


def test_prepared_snapshot_not_mutated_model_is_checked(redo: store.MeetingStore) -> None:
    with redo.writer(ID, store.Writer.TRANSCRIBE) as writer:
        payload = document(WORK[0], generation=1)
        prepared = writer.prepare(WORK[0], payload)
        payload.meeting_generation = 2
        with pytest.raises(store.MeetingStoreError):
            writer.write(WORK[0], prepared)
    assert not (redo.root / ID / WORK[0]).exists()


@pytest.mark.parametrize("name", WORK)
@pytest.mark.parametrize(
    "damage",
    [
        "not-json",
        "duplicate",
        "nested-duplicate",
        "unknown",
        "nested-unknown",
        "embeddings",
        "centroids",
        "nested-embeddings",
        "nested-centroids",
        "oversize",
        "symlink",
        "hardlink",
        "permissions",
        "generation",
        "id",
    ],
)
def test_work_reads_refuse_unsafe_documents_without_content(
    redo: store.MeetingStore, name: str, damage: str, tmp_path: Path
) -> None:
    raw = document(name).to_dict()
    marker = "PRIVATE_MARKER"
    encoded = json.dumps(raw).encode()
    nested = "reconcile"
    if damage == "not-json":
        encoded = marker.encode()
    elif damage == "duplicate":
        encoded = encoded[:-1] + b', "schema": 1}'
    elif damage == "nested-duplicate":
        encoded = encoded.replace(b'"used_ms": 0', b'"used_ms": 0, "used_ms": 0')
    elif damage in {"unknown", "embeddings", "centroids"}:
        raw[damage] = marker
        encoded = json.dumps(raw).encode()
    elif damage.startswith("nested-"):
        raw[nested][damage[7:]] = marker
        encoded = json.dumps(raw).encode()
    elif damage == "generation":
        raw["meeting_generation"] = 1
        encoded = json.dumps(raw).encode()
    elif damage == "id":
        raw["meeting_id"] = "2026-10-29_130000-ab12"
        encoded = json.dumps(raw).encode()
    target = put_raw(redo, name, encoded)
    external = tmp_path / "private-target"
    external.write_bytes(encoded)
    external.chmod(0o600)
    if damage == "oversize":
        with target.open("r+b") as stream:
            # Otherwise valid JSON: rejection must enforce the on-disk byte
            # limit rather than merely fail parsing a sparse file's NULs.
            stream.seek(0, os.SEEK_END)
            stream.write(b" " * (model.MAX_DOCUMENT_BYTES + 1 - len(encoded)))
        assert target.stat().st_size == model.MAX_DOCUMENT_BYTES + 1
    elif damage == "symlink":
        target.unlink()
        target.symlink_to(external)
    elif damage == "hardlink":
        target.unlink()
        os.link(external, target)
        assert target.stat().st_nlink == 2
    elif damage == "permissions":
        target.chmod(0o644)
    with pytest.raises(store.MeetingStoreError) as caught:
        redo.read(ID, name)
    assert marker not in str(caught.value)
    assert str(tmp_path) not in str(caught.value)
    assert caught.value.__cause__ is None
    if name == WORK[0]:
        with redo.writer(ID, store.Writer.TRANSCRIBE) as writer:
            with pytest.raises(store.MeetingStoreError) as caught:
                writer.read_transcript(name)
            assert marker not in str(caught.value)
            assert str(tmp_path) not in str(caught.value)
    assert external.read_bytes() == encoded


@pytest.mark.parametrize("name", WORK)
@pytest.mark.parametrize("damage", ["symlink", "hardlink", "permissions"])
def test_work_write_refuses_unsafe_destination(
    redo: store.MeetingStore, name: str, damage: str, tmp_path: Path
) -> None:
    external = tmp_path / "private-target"
    external.write_bytes(b"PRIVATE_MARKER")
    external.chmod(0o600)
    target = redo.root / ID / name
    if name == WORK[1]:
        redo.write(ID, WORK[0], document(WORK[0]), writer=store.Writer.TRANSCRIBE)
    if damage == "symlink":
        target.symlink_to(external)
    elif damage == "hardlink":
        os.link(external, target)
    else:
        target.write_bytes(b"PRIVATE_MARKER")
        target.chmod(0o644)
    with pytest.raises(store.MeetingStoreError) as caught:
        redo.write(ID, name, document(name), writer=store.Writer.TRANSCRIBE)
    assert "PRIVATE_MARKER" not in str(caught.value)
    assert external.read_bytes() == b"PRIVATE_MARKER"


def test_remove_work_checkpoint_requires_its_own_final_text(redo: store.MeetingStore) -> None:
    before = published_bytes(redo)
    with redo.writer(ID, store.Writer.TRANSCRIBE) as writer:
        writer.write(WORK[0], document(WORK[0]))
        writer.write(WORK[1], document(WORK[1]))
        # Published text is final; work text is not.
        with pytest.raises(store.MeetingStoreError):
            writer.remove(WORK[1])
        final = document(WORK[0], revision=11)
        assert isinstance(final, model.Transcript)
        final.labels_final = True
        writer.write(WORK[0], final)
        writer.remove(WORK[1])
        writer.remove(WORK[1])
        with pytest.raises(store.MeetingStoreError):
            writer.remove("checkpoint.json")
    assert not (redo.root / ID / WORK[1]).exists()
    assert published_bytes(redo) == before


@pytest.mark.parametrize("role", [store.Writer.GUI, store.Writer.CAPTURE, store.Writer.DIAR])
def test_remove_work_checkpoint_only_by_transcribe(
    redo: store.MeetingStore, role: store.Writer
) -> None:
    with redo.writer(ID, role) as writer, pytest.raises(store.MeetingStoreError):
        writer.remove(WORK[1])


def test_read_transcript_name_is_fixed_and_writer_lifetime_checked(
    redo: store.MeetingStore,
) -> None:
    with redo.writer(ID, store.Writer.TRANSCRIBE) as writer:
        with pytest.raises(store.MeetingStoreError):
            writer.read_transcript("../transcript.json")
    with pytest.raises(store.MeetingStoreError):
        writer.read_transcript(WORK[0])


def test_work_revision_is_idempotent_but_cannot_change_content(redo: store.MeetingStore) -> None:
    before = published_bytes(redo)
    text = model.Transcript(
        meeting_id=ID,
        meeting_generation=2,
        transcript_revision=10,
        utterances=[model.Utterance(id="u1", text="Прежний текст")],
    )
    with redo.writer(ID, store.Writer.TRANSCRIBE) as writer:
        writer.write(WORK[0], text)
        writer.write(WORK[0], text)
        text.utterances[0].text = "Другой текст"
        with pytest.raises(store.MeetingStoreError):
            writer.write(WORK[0], text)
    assert redo.read(ID, WORK[0]).utterances[0].text == "Прежний текст"
    assert published_bytes(redo) == before


@pytest.mark.parametrize(
    "name,path",
    [
        (WORK[0], "utterances"),
        (WORK[0], "reconcile"),
        (WORK[1], "model"),
        (WORK[1], "asr"),
        (WORK[1], "asr.mic"),
        (WORK[1], "diar"),
        (WORK[1], "diar.turns"),
        (WORK[1], "limits"),
    ],
)
def test_work_schema_rejects_unknown_fields_at_nested_levels(
    redo: store.MeetingStore, name: str, path: str
) -> None:
    raw = document(name).to_dict()
    if path == "utterances":
        raw["utterances"] = [model.Utterance(id="u1").to_dict()]
    if path == "diar.turns":
        raw["diar"]["turns"] = [model.DiarTurn().to_dict()]
    nested = raw
    for key in path.split("."):
        nested = nested[key]
    if isinstance(nested, list):
        nested = nested[0]
    nested["embeddings"] = "PRIVATE_MARKER"
    put_raw(redo, name, json.dumps(raw).encode())
    with pytest.raises(store.MeetingStoreError) as caught:
        redo.read(ID, name)
    assert "PRIVATE_MARKER" not in str(caught.value)


def test_work_private_reader_rejects_unfinished_final_text(redo: store.MeetingStore) -> None:
    raw = model.Transcript(
        meeting_id=ID, meeting_generation=2, utterances=[model.Utterance(id="u1")]
    ).to_dict()
    raw["labels_final"] = True
    put_raw(redo, WORK[0], json.dumps(raw).encode())
    with pytest.raises(store.MeetingStoreError):
        redo.read(ID, WORK[0])
    with redo.writer(ID, store.Writer.TRANSCRIBE) as writer:
        with pytest.raises(store.MeetingStoreError):
            writer.read_transcript(WORK[0])


@pytest.mark.parametrize("violation", ["descriptor", "state", "counter", "cancelled"])
def test_remove_work_checkpoint_rechecks_metadata(redo: store.MeetingStore, violation: str) -> None:
    with redo.writer(ID, store.Writer.TRANSCRIBE) as writer:
        text = model.Transcript(meeting_id=ID, meeting_generation=2, labels_final=True)
        writer.write(WORK[0], text)
        writer.write(WORK[1], document(WORK[1], revision=0))
        meeting = redo.read(ID, "meeting.json")
        if violation == "descriptor":
            meeting.rediar = None
        elif violation == "state":
            meeting.state = MeetingState.RECORDED
        elif violation == "counter":
            meeting.meeting_generation = 3
        else:
            meeting.cancelled = True
        put_raw(redo, "meeting.json", json.dumps(meeting.to_dict()).encode())
        with pytest.raises(store.MeetingStoreError):
            writer.remove(WORK[1])
    assert (redo.root / ID / WORK[1]).exists()
