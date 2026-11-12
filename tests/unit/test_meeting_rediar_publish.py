"""5в-2: real disk recovery, syscall fault injection and private rename boundary."""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
from typing import Any

import pytest

from astra_voice.meetings import model, store
from astra_voice.meetings.events import MeetingState, PhaseStatus

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("filesystem_euid")]
ID = "2026-10-29_120000-ab12"
STAMP = "2026-10-29T12:00:00+00:00"
RESULT = store.RediarPublishResult
WORK = "rediar-transcript.json"
CHECK = "rediar-checkpoint.json"


def raw_put(disk: store.MeetingStore, name: str, data: Any) -> Path:
    path = disk.root / ID / name
    path.write_bytes(json.dumps(data).encode() if not isinstance(data, bytes) else data)
    path.chmod(0o600)
    return path


def text(generation: int) -> model.Transcript:
    return model.Transcript(
        meeting_id=ID,
        meeting_generation=generation,
        transcript_revision=10 + generation,
        labels_final=True,
        utterances=[model.Utterance(id="u1", seq=0, text=f"PRIVATE_TEXT_{generation}", final=True)],
        reconcile=model.ReconcileProgress(status=PhaseStatus.DONE),
    )


@pytest.fixture
def disk(tmp_path: Path) -> store.MeetingStore:
    disk = store.MeetingStore(tmp_path / "meetings")
    disk.create(
        model.Meeting(id=ID, started_at=STAMP, state=MeetingState.DONE, meeting_generation=1)
    )
    disk.write(ID, "transcript.json", text(1), writer=store.Writer.TRANSCRIBE)
    disk.write(
        ID,
        "speakers.json",
        model.SpeakerMap(labels={"me": model.Speaker(name="Прежнее имя")}),
        writer=store.Writer.GUI,
    )
    meeting = disk.read(ID, "meeting.json")
    meeting.transcript_generation = 1
    meeting.meeting_generation = 2
    meeting.rediar = model.Rediar(2, disk.transcript_state(ID).digest)
    disk.write(ID, "meeting.json", meeting, writer=store.Writer.GUI)
    disk.write(ID, WORK, text(2), writer=store.Writer.TRANSCRIBE)
    disk.write(
        ID,
        CHECK,
        model.Checkpoint(meeting_id=ID, meeting_generation=2, transcript_revision=12),
        writer=store.Writer.TRANSCRIBE,
    )
    raw_put(disk, "checkpoint.json", b"old checkpoint")
    # Create lifetime lock files before injection, to count only commit syscalls.
    with disk.writer(ID, store.Writer.DIAR):
        pass
    return disk


def publish(disk: store.MeetingStore, generation: int = 2) -> store.RediarPublishResult:
    # Each call uses a fresh store and writer: no cached state survives restart.
    with store.MeetingStore(disk.root).writer(ID, store.Writer.GUI) as writer:
        return writer.rediar_publish(generation)


def pair(disk: store.MeetingStore) -> tuple[bytes, bytes]:
    directory = disk.root / ID
    return (directory / "transcript.json").read_bytes(), (directory / "speakers.json").read_bytes()


def assert_published(disk: store.MeetingStore) -> None:
    assert disk.read(ID, "transcript.json") == text(2)
    assert disk.read(ID, "speakers.json") == model.SpeakerMap(transcript_generation=2)
    assert disk.read(ID, "meeting.json").rediar is not None  # K6 belongs to queue.
    for name in (WORK, CHECK, "checkpoint.json"):
        assert not (disk.root / ID / name).exists()


@pytest.mark.parametrize("stage", ["ready", "k2", "k3", "k4", "k5"])
def test_recovery_table_and_idempotence(
    disk: store.MeetingStore, stage: str, monkeypatch: Any
) -> None:
    if stage != "ready":
        names = disk.read(ID, "speakers.json")
        names.transcript_generation = 1
        raw_put(disk, "speakers.json", names.to_dict())
    if stage in {"k3", "k4", "k5"}:
        os.replace(disk.root / ID / WORK, disk.root / ID / "transcript.json")
    if stage in {"k4", "k5"}:
        raw_put(disk, "speakers.json", model.SpeakerMap(transcript_generation=2).to_dict())
    if stage == "k5":
        (disk.root / ID / CHECK).unlink()
        (disk.root / ID / "checkpoint.json").unlink()
    assert publish(disk) == RESULT.PUBLISHED
    assert_published(disk)
    first = pair(disk)
    inode = (disk.root / ID / "speakers.json").stat().st_ino

    def no_replace(*args: Any, **kwargs: Any) -> None:
        pytest.fail("second call must not rename transcript or names")

    monkeypatch.setattr(os, "replace", no_replace)
    assert publish(disk) == RESULT.PUBLISHED
    assert pair(disk) == first
    assert (disk.root / ID / "speakers.json").stat().st_ino == inode


@pytest.mark.parametrize("state", ["unseeded", "diar", "reconcile"])
def test_unfinished_recovery_rows(disk: store.MeetingStore, state: str) -> None:
    if state == "unseeded":
        (disk.root / ID / WORK).unlink()
        (disk.root / ID / CHECK).unlink()
    else:
        candidate = text(2)
        candidate.labels_final = False
        candidate.reconcile.status = PhaseStatus.PENDING if state == "diar" else PhaseStatus.RUNNING
        raw_put(disk, WORK, candidate.to_dict())
    before = pair(disk)
    assert publish(disk) == RESULT.WORK_PENDING
    assert pair(disk) == before


@pytest.mark.parametrize("utterances_final", [False, True])
@pytest.mark.parametrize("reconcile_failed", [False, True])
@pytest.mark.parametrize("checkpoint_written", [False, True])
def test_unfinalized_reconcile_done_survives_restart_and_publishes(
    disk: store.MeetingStore,
    utterances_final: bool,
    reconcile_failed: bool,
    checkpoint_written: bool,
) -> None:
    # _reconcile -> _persist(final=False): text first, then checkpoint.
    candidate = text(2)
    candidate.transcript_revision += 1
    candidate.labels_final = False
    candidate.utterances[0].final = utterances_final
    candidate.reconcile.failed = reconcile_failed
    checkpoint = model.Checkpoint(
        meeting_id=ID,
        meeting_generation=2,
        transcript_revision=candidate.transcript_revision,
        reconcile=candidate.reconcile,
    )
    checkpoint.asr.status = PhaseStatus.DONE
    checkpoint.diar.status = PhaseStatus.DONE
    with disk.writer(ID, store.Writer.TRANSCRIBE) as writer:
        writer.write(WORK, candidate)
        if checkpoint_written:
            writer.write(CHECK, checkpoint)
    before = pair(disk)
    work_before = tuple((disk.root / ID / name).read_bytes() for name in (WORK, CHECK))
    # publish() creates a new store and GUI writer, with no surviving cache.
    assert publish(disk) == RESULT.WORK_PENDING
    assert pair(disk) == before
    assert tuple((disk.root / ID / name).read_bytes() for name in (WORK, CHECK)) == work_before
    # _post_asr -> _persist(final=True), using a fresh role lease after restart.
    candidate.transcript_revision += 1
    candidate.labels_final = True
    for utterance in candidate.utterances:
        utterance.final = True
    checkpoint.transcript_revision = candidate.transcript_revision
    with store.MeetingStore(disk.root).writer(ID, store.Writer.TRANSCRIBE) as writer:
        writer.write(WORK, candidate)
        writer.write(CHECK, checkpoint)
    assert publish(disk) == RESULT.PUBLISHED
    assert disk.read(ID, "transcript.json") == candidate
    assert disk.read(ID, "speakers.json") == model.SpeakerMap(transcript_generation=2)
    for name in (WORK, CHECK, "checkpoint.json"):
        assert not (disk.root / ID / name).exists()


@pytest.mark.parametrize("state", ["reservation", "receipt", "receipt-cleared"])
def test_nonfinal_reconcile_snapshots_remain_pending(disk: store.MeetingStore, state: str) -> None:
    candidate = text(2)
    candidate.labels_final = False
    candidate.utterances[0].final = False
    candidate.reconcile.status = PhaseStatus.RUNNING
    if state == "reservation":
        candidate.reconcile.inflight_id = "u1"
        candidate.reconcile.speech_ms = 1000
        candidate.reconcile.budget_ms = 150
        candidate.reconcile.used_ms = 100
    else:
        candidate.reconcile.done_ids = ["original"]
        if state == "receipt":
            candidate.reconcile.pending = model.ReconcileReplacement(
                seq=candidate.transcript_revision,
                replaces_id="original",
                expected_revision=candidate.transcript_revision - 1,
                items=candidate.utterances,
            )
    raw_put(disk, WORK, candidate.to_dict())
    before = pair(disk)
    work_before = tuple((disk.root / ID / name).read_bytes() for name in (WORK, CHECK))
    assert publish(disk) == RESULT.WORK_PENDING
    assert pair(disk) == before
    assert tuple((disk.root / ID / name).read_bytes() for name in (WORK, CHECK)) == work_before


@pytest.mark.parametrize(
    "field",
    ["utterance", "status", "pending", "inflight", "seq", "id", "generation", "base"],
)
def test_invalid_final_candidate(disk: store.MeetingStore, field: str) -> None:
    data = text(2).to_dict()
    if field == "utterance":
        data["utterances"][0]["final"] = False
    elif field == "status":
        data["reconcile"]["status"] = "running"
    elif field == "pending":
        data["reconcile"]["done_ids"] = ["original"]
        data["reconcile"]["pending"] = {
            "seq": 12,
            "expected_revision": 11,
            "replaces_id": "original",
            "items": data["utterances"],
        }
        model.Transcript.from_dict(data)  # A valid typed receipt, still not final.
    elif field == "inflight":
        data["reconcile"]["inflight_id"] = "u1"
    elif field == "seq":
        data["utterances"][0]["seq"] = 1
    elif field == "id":
        data["meeting_id"] = "2026-10-29_130000-ab12"
    elif field == "generation":
        data["meeting_generation"] = 1
    else:
        meeting = disk.read(ID, "meeting.json")
        meeting.rediar.base_sha256 = "0" * 64
        raw_put(disk, "meeting.json", meeting.to_dict())
    raw_put(disk, WORK, data)
    before = pair(disk)
    assert publish(disk) == RESULT.INVALID
    assert pair(disk) == before
    assert (disk.root / ID / WORK).exists()
    assert (disk.root / ID / CHECK).exists()


@pytest.mark.parametrize(
    "field", ["utterance", "labels", "status", "pending", "inflight", "seq", "id", "json"]
)
def test_k0_uses_full_validator_and_never_resumes_role(
    disk: store.MeetingStore, field: str
) -> None:
    os.replace(disk.root / ID / WORK, disk.root / ID / "transcript.json")
    data = text(2).to_dict()
    if field == "utterance":
        data["utterances"][0]["final"] = False
    elif field == "labels":
        data["labels_final"] = False
    elif field == "status":
        data["reconcile"]["status"] = "pending"
    elif field == "pending":
        data["reconcile"]["done_ids"] = ["original"]
        data["reconcile"]["pending"] = {
            "seq": 12,
            "expected_revision": 11,
            "replaces_id": "original",
            "items": data["utterances"],
        }
        model.Transcript.from_dict(data)  # A valid typed receipt, still not final.
    elif field == "inflight":
        data["reconcile"]["inflight_id"] = "u1"
    elif field == "seq":
        data["utterances"][0]["seq"] = 2
    elif field == "id":
        data["meeting_id"] = "2026-10-29_130000-ab12"
    raw_put(disk, "transcript.json", b"PRIVATE_BROKEN" if field == "json" else data)
    before = pair(disk)
    assert publish(disk) == RESULT.IO_ERROR
    assert pair(disk) == before
    with disk.writer(ID, store.Writer.GUI) as writer, pytest.raises(store.MeetingStoreError):
        writer.rediar_discard()


@pytest.mark.parametrize("field", ["no-rediar", "generation", "state", "cancelled", "deleting"])
def test_rejected_metadata(disk: store.MeetingStore, field: str) -> None:
    meeting = disk.read(ID, "meeting.json")
    if field == "no-rediar":
        meeting.rediar = None
    elif field == "state":
        meeting.state = MeetingState.RECORDED
    elif field == "deleting":
        meeting.state = MeetingState.DELETING
    elif field == "cancelled":
        meeting.cancelled = True
    # Open before tombstone: writer() itself refuses DELETING.
    with disk.writer(ID, store.Writer.GUI) as writer:
        raw_put(disk, "meeting.json", meeting.to_dict())
        before = pair(disk)
        assert writer.rediar_publish(3 if field == "generation" else 2) == RESULT.REJECTED
        assert pair(disk) == before


@pytest.mark.parametrize("role", [store.Writer.DIAR, store.Writer.TRANSCRIBE, ".store.lock"])
def test_busy_leases_and_gate(disk: store.MeetingStore, role: Any) -> None:
    directory = disk.root / ID
    name = role if role == ".store.lock" else f".writer-{role.value}.lock"
    before = {p.name: p.read_bytes() for p in directory.iterdir() if p.is_file()}
    with (directory / name).open("rb") as lease:
        fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert publish(disk) == RESULT.BUSY
        with disk.writer(ID, store.Writer.GUI) as writer, pytest.raises(store.MeetingStoreError):
            writer.rediar_discard()
    assert {p.name: p.read_bytes() for p in directory.iterdir() if p.is_file()} == before


# All K2/K4 write_private internals are counted: 7 fsyncs, 3 replaces, 2 writes.
FAULTS = (
    [("fsync", n) for n in range(1, 8)]
    + [("replace", n) for n in range(1, 4)]
    + [("write_private", n) for n in range(1, 3)]
)


RECOVERY_FAULTS = (
    [("k3", "fsync", n) for n in range(1, 5)]
    + [("k3", "replace", 1), ("k3", "write_private", 1)]
    + [("k4", "fsync", n) for n in range(1, 3)]
)


@pytest.mark.parametrize(
    "stage,operation,nth", [("ready", op, n) for op, n in FAULTS] + RECOVERY_FAULTS
)
@pytest.mark.parametrize("side", ["before", "after"])
@pytest.mark.parametrize("crash", [False, True], ids=["io-error", "process-crash"])
def test_every_commit_fault_recovers(
    disk: store.MeetingStore,
    monkeypatch: Any,
    stage: str,
    operation: str,
    nth: int,
    side: str,
    crash: bool,
) -> None:
    if stage != "ready":
        os.replace(disk.root / ID / WORK, disk.root / ID / "transcript.json")
        names = disk.read(ID, "speakers.json")
        names.transcript_generation = 1
        raw_put(disk, "speakers.json", names.to_dict())
    if stage == "k4":
        raw_put(disk, "speakers.json", model.SpeakerMap(transcript_generation=2).to_dict())

    class Crash(BaseException):
        pass

    module = store if operation == "write_private" else os
    original = getattr(module, operation)
    count = 0
    hit = False
    old = pair(disk)

    def injected(*args: Any, **kwargs: Any) -> Any:
        nonlocal count, hit
        count += 1
        target = count == nth

        def fail() -> None:
            nonlocal hit
            hit = True
            if crash:
                raise Crash()
            raise OSError(errno.EIO, "injected")

        if target and side == "before":
            fail()
        result = original(*args, **kwargs)
        if target and side == "after":
            fail()
        return result

    with monkeypatch.context() as scoped:
        scoped.setattr(module, operation, injected)
        if crash:
            with pytest.raises(Crash):
                publish(disk)
        else:
            result = publish(disk)
            assert result in {RESULT.BUSY, RESULT.IO_ERROR}
            # These are precisely the uncertain replacement windows.
            if (
                (
                    operation == "fsync"
                    and (
                        (stage == "ready" and nth in {2, 3, 4, 6})
                        or (stage == "k3" and nth in {1, 3})
                        or (stage == "k4" and nth == 1)
                    )
                )
                or (operation == "replace" and side == "after")
                or (operation == "write_private" and side == "after")
            ):
                assert result == RESULT.BUSY
    assert hit, (operation, nth, side)
    if disk.transcript_state(ID).generation == 1:
        assert (disk.root / ID / WORK).exists()
        assert (disk.root / ID / CHECK).exists()
        names = disk.read(ID, "speakers.json")
        assert names.labels == {"me": model.Speaker(name="Прежнее имя")}
        assert pair(disk)[0] == old[0]
    else:
        assert disk.transcript_state(ID).generation == 2
    assert publish(disk) == RESULT.PUBLISHED
    assert_published(disk)
    assert publish(disk) == RESULT.PUBLISHED


@pytest.mark.parametrize("damage", ["symlink", "hardlink", "fifo", "directory", "mode", "owner"])
def test_source_substitution_between_k1_k3(
    disk: store.MeetingStore, monkeypatch: Any, tmp_path: Path, damage: str
) -> None:
    if damage == "owner" and os.geteuid() != 0:
        pytest.skip("chown to foreign uid requires root")
    external = tmp_path / "external"
    external.write_bytes(b"EXTERNAL_PRIVATE")
    external.chmod(0o600)
    target = disk.root / ID / WORK
    original = store.MeetingWriter._rediar_names

    def swapped(writer: store.MeetingWriter, names: model.SpeakerMap) -> None:
        original(writer, names)
        target.unlink()
        if damage == "symlink":
            target.symlink_to(external)
        elif damage == "hardlink":
            os.link(external, target)
        elif damage == "fifo":
            os.mkfifo(target, 0o600)
        elif damage == "directory":
            target.mkdir(mode=0o700)
        else:
            target.write_bytes(b"SUBSTITUTED")
            target.chmod(0o644 if damage == "mode" else 0o600)
            if damage == "owner":
                os.chown(target, 65534, -1)

    monkeypatch.setattr(store.MeetingWriter, "_rediar_names", swapped)
    # Monitor actual opens: the rename source check must never read payloads.
    real_read = os.read

    def checked_read(fd: int, size: int) -> bytes:
        assert os.fstat(fd).st_ino != external.stat().st_ino
        return real_read(fd, size)

    monkeypatch.setattr(os, "read", checked_read)
    assert publish(disk) == RESULT.IO_ERROR
    assert disk.read(ID, "transcript.json") == text(1)
    assert external.read_bytes() == b"EXTERNAL_PRIVATE"
    assert (disk.root / ID / CHECK).exists()


def test_discard_exact_templates_and_symlink_targets(
    disk: store.MeetingStore, tmp_path: Path
) -> None:
    with disk.writer(ID, store.Writer.GUI) as writer, pytest.raises(store.MeetingStoreError):
        writer.rediar_discard()
    meeting = disk.read(ID, "meeting.json")
    meeting.rediar = None
    disk.write(ID, "meeting.json", meeting, writer=store.Writer.GUI)
    directory = disk.root / ID
    external = tmp_path / "target"
    external.write_bytes(b"KEEP")
    owned = [f".rediar-{stem}-{'a' * 32}.tmp" for stem in ("transcript", "checkpoint")]
    other = [
        ".rediar-transcript-abc.tmp",
        f".rediar-transcript-{'g' * 32}.tmp",
        f".rediar-transcript-{'a' * 32}.tmp.extra",
        f".transcript-{'a' * 32}.tmp",
        "rediar-other.json",
    ]
    for name in owned + other:
        (directory / name).symlink_to(external)
    (directory / WORK).unlink()
    (directory / WORK).symlink_to(external)
    before = pair(disk)
    for _ in range(2):
        with store.MeetingStore(disk.root).writer(ID, store.Writer.GUI) as writer:
            writer.rediar_discard()
    for name in [WORK, CHECK, *owned]:
        assert not (directory / name).is_symlink()
        assert not (directory / name).exists()
    assert all((directory / name).is_symlink() for name in other)
    assert external.read_bytes() == b"KEEP"
    assert pair(disk) == before
    assert (directory / "checkpoint.json").exists()


def test_published_cleans_own_temps(disk: store.MeetingStore) -> None:
    paths = [disk.root / ID / f".rediar-{s}-{'b' * 32}.tmp" for s in ("transcript", "checkpoint")]
    for path in paths:
        path.write_bytes(b"private remnants")
    assert publish(disk) == RESULT.PUBLISHED
    assert all(not p.exists() for p in paths)


def test_transcript_state_exact_bytes_and_errors(disk: store.MeetingStore) -> None:
    raw = b" \n" + (disk.root / ID / "transcript.json").read_bytes() + b" "
    raw_put(disk, "transcript.json", raw)
    state = disk.transcript_state(ID)
    assert (
        state.meeting_id,
        state.generation,
        state.revision,
        state.labels_final,
        state.digest,
    ) == (ID, 1, 11, True, hashlib.sha256(raw).hexdigest())
    raw_put(disk, "transcript.json", b"PRIVATE_MARKER broken")
    with pytest.raises(store.MeetingStoreError) as caught:
        disk.transcript_state(ID)
    assert "PRIVATE_MARKER" not in str(caught.value)
    assert str(disk.root) not in str(caught.value)
    assert caught.value.__cause__ is None


@pytest.mark.parametrize("role", [store.Writer.TRANSCRIBE, store.Writer.DIAR, store.Writer.CAPTURE])
def test_gui_only_and_closed_writer(disk: store.MeetingStore, role: store.Writer) -> None:
    with disk.writer(ID, role) as writer:
        assert writer.rediar_publish(2) == RESULT.REJECTED
        with pytest.raises(store.MeetingStoreError):
            writer.rediar_discard()
    assert writer.rediar_publish(2) == RESULT.REJECTED
    with pytest.raises(store.MeetingStoreError):
        writer.rediar_discard()


@pytest.mark.parametrize("name", [CHECK, "checkpoint.json"])
@pytest.mark.parametrize("side", ["before", "after"])
def test_k5_interrupted_unlink(
    disk: store.MeetingStore, monkeypatch: Any, name: str, side: str
) -> None:
    real_unlink = os.unlink

    def interrupted(path: Any, *args: Any, **kwargs: Any) -> None:
        if path == name and side == "before":
            raise OSError(errno.EIO, "unlink")
        real_unlink(path, *args, **kwargs)
        if path == name and side == "after":
            raise OSError(errno.EIO, "unlink")

    with monkeypatch.context() as scoped:
        scoped.setattr(os, "unlink", interrupted)
        assert publish(disk) == RESULT.IO_ERROR
    assert disk.transcript_state(ID).generation == 2
    assert publish(disk) == RESULT.PUBLISHED
    assert_published(disk)


@pytest.mark.parametrize("code", [errno.EIO, errno.ENOSPC, errno.EACCES])
def test_precommit_io_error_is_distinct_and_preserves_work(
    disk: store.MeetingStore, monkeypatch: Any, code: int
) -> None:
    before = pair(disk)

    def refused(*args: Any, **kwargs: Any) -> None:
        raise OSError(code, "private write")

    with monkeypatch.context() as scoped:
        scoped.setattr(store, "write_private", refused)
        assert publish(disk) == RESULT.IO_ERROR
    assert pair(disk) == before
    assert (disk.root / ID / WORK).exists()
    assert (disk.root / ID / CHECK).exists()
    assert publish(disk) == RESULT.PUBLISHED


@pytest.mark.parametrize("damage", ["symlink", "hardlink", "mode", "id", "duplicate"])
def test_transcript_state_private_typed_read(
    disk: store.MeetingStore, tmp_path: Path, damage: str
) -> None:
    external = tmp_path / "external"
    external.write_bytes((disk.root / ID / "transcript.json").read_bytes())
    external.chmod(0o600)
    target = disk.root / ID / "transcript.json"
    if damage in {"symlink", "hardlink"}:
        target.unlink()
        if damage == "symlink":
            target.symlink_to(external)
        else:
            os.link(external, target)
    elif damage == "mode":
        target.chmod(0o644)
    elif damage == "id":
        data = text(1).to_dict()
        data["meeting_id"] = "2026-10-29_130000-ab12"
        raw_put(disk, "transcript.json", data)
    else:
        raw_put(disk, "transcript.json", target.read_bytes().rstrip()[:-1] + b', "schema":1}')
    with pytest.raises(store.MeetingStoreError) as caught:
        disk.transcript_state(ID)
    assert "PRIVATE_TEXT" not in str(caught.value)
    assert str(disk.root) not in str(caught.value)


def test_parent_path_substitution_keeps_pinned_directory(
    disk: store.MeetingStore, tmp_path: Path, monkeypatch: Any
) -> None:
    original_directory = disk.root / ID
    moved = tmp_path / "moved-meeting"
    original_names = store.MeetingWriter._rediar_names
    replaced = False

    def swap_parent(writer: store.MeetingWriter, names: model.SpeakerMap) -> None:
        nonlocal replaced
        original_names(writer, names)
        if not replaced:
            original_directory.rename(moved)
            original_directory.mkdir(mode=0o700)
            for name in (WORK, "transcript.json", "speakers.json"):
                (original_directory / name).write_bytes(b"UNTOUCHED_DECOY")
            replaced = True

    monkeypatch.setattr(store.MeetingWriter, "_rediar_names", swap_parent)
    # writer pins the directory before its path is replaced in K2.
    with disk.writer(ID, store.Writer.GUI) as writer:
        assert writer.rediar_publish(2) == RESULT.PUBLISHED
    assert model.Transcript.from_dict(json.loads((moved / "transcript.json").read_bytes())) == text(
        2
    )
    assert model.SpeakerMap.from_dict(
        json.loads((moved / "speakers.json").read_bytes())
    ) == model.SpeakerMap(transcript_generation=2)
    assert not (moved / CHECK).exists()
    assert all(
        (original_directory / n).read_bytes() == b"UNTOUCHED_DECOY"
        for n in (WORK, "transcript.json", "speakers.json")
    )


def test_reconcile_failed_is_still_a_finished_result(disk: store.MeetingStore) -> None:
    candidate = text(2)
    candidate.reconcile.failed = True
    raw_put(disk, WORK, candidate.to_dict())
    assert publish(disk) == RESULT.PUBLISHED
    assert disk.read(ID, "transcript.json") == candidate
    assert disk.read(ID, "speakers.json") == model.SpeakerMap(transcript_generation=2)
