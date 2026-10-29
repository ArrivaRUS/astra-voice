"""E13 M1: private disk storage; no Qt, real audio or D-Bus."""

from __future__ import annotations

import fcntl
import os
import stat
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

from astra_voice.meetings import events, model, segments, store

pytestmark = pytest.mark.unit
ID = "2026-10-29_120000-ab12"
STAMP = "2026-10-29T12:00:00+00:00"


@pytest.fixture
def disk(tmp_path: Path) -> store.MeetingStore:
    result = store.MeetingStore(tmp_path / "meetings")
    result.create(model.Meeting(id=ID, started_at=STAMP))
    return result


def test_layout_permissions_and_no_overwrite(disk: store.MeetingStore) -> None:
    meeting_dir = disk.root / ID
    for directory in (
        disk.root,
        meeting_dir,
        *(meeting_dir / n for n in ("audio", "exports", "ai")),
    ):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE((meeting_dir / "meeting.json").stat().st_mode) == 0o600
    assert disk.read(ID, "meeting.json").id == ID
    with pytest.raises(store.MeetingStoreError):
        disk.create(model.Meeting(id=ID, started_at=STAMP, title="overwrite"))
    assert disk.read(ID, "meeting.json").title != "overwrite"


def test_default_root_respects_xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    disk = store.MeetingStore()
    assert disk.root == tmp_path / "astra-voice" / "meetings"
    disk.create(model.Meeting(id=ID, started_at=STAMP))
    assert stat.S_IMODE(disk.root.parent.stat().st_mode) == 0o700


@pytest.mark.parametrize("location", ["root", "ancestor", "meeting", "audio", "file"])
@pytest.mark.parametrize("operation", ["read", "write"])
def test_symlink_refused_without_touching_target(
    tmp_path: Path, location: str, operation: str
) -> None:
    root = tmp_path / "meetings"
    disk = store.MeetingStore(root)
    disk.create(model.Meeting(id=ID, started_at=STAMP))
    target = tmp_path / "PRIVATE_TARGET"
    target_disk = store.MeetingStore(target)
    target_disk.create(model.Meeting(id=ID, started_at=STAMP, title="PRIVATE_MARKER"))
    sidecar = segments.SegmentSidecar(meeting_id=ID, epoch_id="e1")
    for current in (disk, target_disk):
        current.write(ID, "audio/mic-0000.json", sidecar, writer=store.Writer.CAPTURE)
        assert current.read(ID, "audio/mic-0000.json") == sidecar
    assert target_disk.read(ID, "meeting.json").title == "PRIVATE_MARKER"
    payload = target / ID / "meeting.json"
    original = payload.read_bytes()
    target_path = {
        "root": target,
        "meeting": target / ID,
        "audio": target / ID / "audio",
        "file": payload,
    }.get(location, target)
    victim = {
        "root": root,
        "ancestor": tmp_path / "alias",
        "meeting": root / ID,
        "audio": root / ID / "audio",
        "file": root / ID / "meeting.json",
    }[location]
    if location == "ancestor":
        victim.symlink_to(tmp_path, target_is_directory=True)
        disk = store.MeetingStore(victim / "meetings")
    else:
        victim.rename(victim.with_name(victim.name + "-original"))
        victim.symlink_to(target_path, target_is_directory=location != "file")
    with pytest.raises(store.MeetingStoreError) as caught:
        if operation == "read":
            disk.read(ID, "audio/mic-0000.json" if location == "audio" else "meeting.json")
        elif location == "audio":
            disk.write(
                ID,
                "audio/mic-0000.json",
                segments.SegmentSidecar(meeting_id=ID, epoch_id="e1"),
                writer=store.Writer.CAPTURE,
            )
        else:
            disk.write(
                ID, "meeting.json", model.Meeting(id=ID, started_at=STAMP), writer=store.Writer.GUI
            )
    assert payload.read_bytes() == original
    assert str(tmp_path) not in str(caught.value)
    assert "PRIVATE_MARKER" not in str(caught.value)


@pytest.mark.parametrize("stage", ["replace", "fsync"])
def test_atomic_failure_keeps_old_file_and_removes_temporary(
    disk: store.MeetingStore, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    disk.write(ID, "meeting.json", model.Meeting(id=ID, started_at=STAMP), writer=store.Writer.GUI)
    before = (disk.root / ID / "meeting.json").read_bytes()
    monkeypatch.setattr(os, stage, Mock(side_effect=OSError("PRIVATE_MARKER /private/path")))
    with pytest.raises(store.MeetingStoreError) as caught:
        disk.write(
            ID,
            "meeting.json",
            model.Meeting(id=ID, started_at=STAMP, title="new"),
            writer=store.Writer.GUI,
        )
    assert "PRIVATE_MARKER" not in str(caught.value)
    assert (disk.root / ID / "meeting.json").read_bytes() == before
    assert not list((disk.root / ID).glob("*.tmp"))
    assert not list((disk.root / ID).glob(".*.tmp"))


def test_creation_flags_and_fsync_order(
    disk: store.MeetingStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    operations = Mock()
    for name in ("open", "fsync", "replace"):
        spy = Mock(wraps=getattr(os, name))
        operations.attach_mock(spy, name)
        monkeypatch.setattr(os, name, spy)
    disk.write(ID, "meeting.json", model.Meeting(id=ID, started_at=STAMP), writer=store.Writer.GUI)
    creates = [c for c in operations.open.call_args_list if c.args[1] & os.O_CREAT]
    assert creates
    assert all(c.args[1] & os.O_EXCL and c.args[1] & os.O_NOFOLLOW for c in creates)
    calls = operations.mock_calls
    replace_index = next(i for i, c in enumerate(calls) if c[0] == "replace")
    assert any(c[0] == "fsync" for c in calls[:replace_index])
    assert calls[replace_index + 1][0] == "fsync"


@pytest.mark.parametrize(
    "writer,filename,kind",
    [
        ("gui", "transcript.json", "transcript"),
        ("gui", "checkpoint.json", "checkpoint"),
        ("capture", "meeting.json", "meeting"),
        ("transcribe", "meeting.json", "meeting"),
        ("transcribe", "audio/mic-0000.json", "segment"),
        ("capture", "speakers.json", "speakers"),
    ],
)
def test_single_writer_by_file_type(
    disk: store.MeetingStore, writer: str, filename: str, kind: str
) -> None:
    values = {
        "meeting": model.Meeting(id=ID, started_at=STAMP),
        "transcript": model.Transcript(meeting_id=ID),
        "checkpoint": model.Checkpoint(meeting_id=ID),
        "segment": segments.SegmentSidecar(meeting_id=ID, epoch_id="e1"),
        "speakers": model.SpeakerMap(),
    }
    with pytest.raises(store.MeetingStoreError):
        disk.write(ID, filename, values[kind], writer=store.Writer(writer))


def test_writer_lease_excludes_second_store_and_allows_other_role(disk: store.MeetingStore) -> None:
    other = store.MeetingStore(disk.root)
    with disk.writer(ID, store.Writer.TRANSCRIBE) as owner:
        with pytest.raises(store.MeetingStoreError):
            other.write(
                ID,
                "transcript.json",
                model.Transcript(meeting_id=ID),
                writer=store.Writer.TRANSCRIBE,
            )
        owner.write("transcript.json", model.Transcript(meeting_id=ID))
        other.write(
            ID,
            "audio/mic-0000.json",
            segments.SegmentSidecar(meeting_id=ID, epoch_id="e1"),
            writer=store.Writer.CAPTURE,
        )
    other.write(
        ID, "transcript.json", model.Transcript(meeting_id=ID), writer=store.Writer.TRANSCRIBE
    )


def test_checkpoint_cannot_precede_durable_text(disk: store.MeetingStore) -> None:
    checkpoint = model.Checkpoint(meeting_id=ID, transcript_revision=1)
    with pytest.raises(store.MeetingStoreError):
        disk.write(ID, "checkpoint.json", checkpoint, writer=store.Writer.TRANSCRIBE)
    disk.write(
        ID,
        "transcript.json",
        model.Transcript(meeting_id=ID, transcript_revision=1),
        writer=store.Writer.TRANSCRIBE,
    )
    disk.write(ID, "checkpoint.json", checkpoint, writer=store.Writer.TRANSCRIBE)
    assert disk.read(ID, "checkpoint.json") == checkpoint
    checkpoint.transcript_revision = 2
    with pytest.raises(store.MeetingStoreError):
        disk.write(ID, "checkpoint.json", checkpoint, writer=store.Writer.TRANSCRIBE)
    assert disk.read(ID, "checkpoint.json").transcript_revision == 1


def test_speakers_require_publication_and_exited_diar(disk: store.MeetingStore) -> None:
    with pytest.raises(store.MeetingStoreError):
        disk.write(ID, "speakers.json", model.SpeakerMap(), writer=store.Writer.GUI)
    meeting = model.Meeting(id=ID, started_at=STAMP, state=events.MeetingState.TRANSCRIBING)
    disk.write(ID, "meeting.json", meeting, writer=store.Writer.GUI)
    meeting.state = events.MeetingState.DONE
    disk.write(ID, "meeting.json", meeting, writer=store.Writer.GUI)
    with disk.writer(ID, store.Writer.DIAR):
        with pytest.raises(store.MeetingStoreError):
            disk.write(ID, "speakers.json", model.SpeakerMap(), writer=store.Writer.GUI)
    with disk.writer(ID, store.Writer.TRANSCRIBE):
        with pytest.raises(store.MeetingStoreError):
            disk.write(ID, "speakers.json", model.SpeakerMap(), writer=store.Writer.GUI)
    disk.write(ID, "speakers.json", model.SpeakerMap(), writer=store.Writer.GUI)


@pytest.mark.parametrize(
    "filename", ["../escape.json", "/tmp/escape.json", "audio/../meeting.json", "bad.json"]
)
def test_paths_are_closed(disk: store.MeetingStore, filename: str) -> None:
    with pytest.raises(store.MeetingStoreError):
        disk.write(ID, filename, model.Meeting(id=ID, started_at=STAMP), writer=store.Writer.GUI)


def test_tombstone_cancel_fields_roundtrip_without_recreation(disk: store.MeetingStore) -> None:
    meeting = model.Meeting(
        id=ID,
        started_at=STAMP,
        state=events.MeetingState.DELETING,
        meeting_generation=3,
        cancelled=True,
        processing_status=events.ProcessingStatus.CANCELLED,
        cleanup_pending=True,
    )
    disk.write(ID, "meeting.json", meeting, writer=store.Writer.GUI)
    assert disk.read(ID, "meeting.json") == meeting
    with pytest.raises(store.MeetingStoreError):
        disk.write(
            ID, "transcript.json", model.Transcript(meeting_id=ID), writer=store.Writer.TRANSCRIBE
        )
    (disk.root / ID).rename(disk.root / f".deleting-{ID}")
    with pytest.raises(store.MeetingStoreError):
        disk.write(ID, "meeting.json", meeting, writer=store.Writer.GUI)
    assert not (disk.root / ID).exists()


def test_transcript_revision_cannot_be_reused_for_different_text(disk: store.MeetingStore) -> None:
    transcript = model.Transcript(meeting_id=ID, transcript_revision=1)
    disk.write(ID, "transcript.json", transcript, writer=store.Writer.TRANSCRIBE)
    disk.write(
        ID,
        "checkpoint.json",
        model.Checkpoint(meeting_id=ID, transcript_revision=1),
        writer=store.Writer.TRANSCRIBE,
    )
    # Exact replay is harmless; a revision must identify one durable text.
    disk.write(ID, "transcript.json", transcript, writer=store.Writer.TRANSCRIBE)
    transcript.utterances.append(model.Utterance(id="u1", text="Different", end_ms=1))
    with pytest.raises(store.MeetingStoreError):
        disk.write(ID, "transcript.json", transcript, writer=store.Writer.TRANSCRIBE)
    with pytest.raises(store.MeetingStoreError):
        disk.write(
            ID,
            "transcript.json",
            model.Transcript(meeting_id=ID, transcript_revision=0),
            writer=store.Writer.TRANSCRIBE,
        )


def test_stale_generation_cannot_publish(disk: store.MeetingStore) -> None:
    meeting = model.Meeting(id=ID, started_at=STAMP, meeting_generation=1)
    disk.write(ID, "meeting.json", meeting, writer=store.Writer.GUI)
    with pytest.raises(store.MeetingStoreError):
        disk.write(
            ID,
            "audio/mic-0000.json",
            segments.SegmentSidecar(meeting_id=ID, epoch_id="e1", meeting_generation=0),
            writer=store.Writer.CAPTURE,
        )


@pytest.mark.parametrize("kind", ["transcript", "checkpoint", "segment"])
def test_durable_cancel_blocks_current_generation(disk: store.MeetingStore, kind: str) -> None:
    disk.write(
        ID,
        "meeting.json",
        model.Meeting(id=ID, started_at=STAMP, meeting_generation=1),
        writer=store.Writer.GUI,
    )
    disk.write(
        ID,
        "transcript.json",
        model.Transcript(meeting_id=ID, meeting_generation=1),
        writer=store.Writer.TRANSCRIBE,
    )
    disk.write(
        ID,
        "meeting.json",
        model.Meeting(id=ID, started_at=STAMP, meeting_generation=1, cancelled=True),
        writer=store.Writer.GUI,
    )
    values = {
        "transcript": (
            "transcript.json",
            model.Transcript(meeting_id=ID, meeting_generation=1),
            store.Writer.TRANSCRIBE,
        ),
        "checkpoint": (
            "checkpoint.json",
            model.Checkpoint(meeting_id=ID, meeting_generation=1),
            store.Writer.TRANSCRIBE,
        ),
        "segment": (
            "audio/mic-0000.json",
            segments.SegmentSidecar(meeting_id=ID, epoch_id="e1", meeting_generation=1),
            store.Writer.CAPTURE,
        ),
    }
    filename, value, role = values[kind]
    path = disk.root / ID / filename
    before = path.read_bytes() if path.exists() else None
    with pytest.raises(store.MeetingStoreError):
        disk.write(ID, filename, value, writer=role)
    assert (path.read_bytes() if path.exists() else None) == before


@pytest.mark.parametrize("kind", ["mode", "hardlink", "fifo", "malformed", "duplicate", "unknown"])
def test_unsafe_or_invalid_file_read_is_closed(disk: store.MeetingStore, kind: str) -> None:
    path = disk.root / ID / "meeting.json"
    original = path.read_bytes()
    if kind == "mode":
        path.chmod(0o644)
    elif kind == "hardlink":
        os.link(path, path.with_name("alias.json"))
    elif kind == "fifo":
        path.unlink()
        os.mkfifo(path, mode=0o600)
    elif kind == "malformed":
        path.write_bytes(b"PRIVATE_MARKER")
    elif kind == "duplicate":
        path.write_bytes(original.rstrip()[:-1] + b', "schema": 1}')
    else:
        path.write_bytes(original.rstrip()[:-1] + b', "secret": "PRIVATE_MARKER"}')
    with pytest.raises(store.MeetingStoreError) as caught:
        disk.read(ID, "meeting.json")
    assert "PRIVATE_MARKER" not in str(caught.value) and str(path) not in str(caught.value)


def test_writer_lease_excludes_other_process(disk: store.MeetingStore) -> None:
    script = """
import sys
from pathlib import Path
from astra_voice.meetings.store import MeetingStore, MeetingStoreError, Writer
try:
    with MeetingStore(Path(sys.argv[1])).writer(sys.argv[2], Writer.TRANSCRIBE):
        raise SystemExit(2)
except MeetingStoreError:
    pass
"""
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src")}
    with disk.writer(ID, store.Writer.TRANSCRIBE):
        result = subprocess.run(
            [sys.executable, "-c", script, str(disk.root), ID],
            env=env,
            capture_output=True,
            timeout=10,
            check=False,
        )
    assert result.returncode == 0, result.stderr.decode()


def test_every_disk_document_is_private_and_roundtrips(disk: store.MeetingStore) -> None:
    transcript = model.Transcript(
        meeting_id=ID,
        transcript_revision=1,
        utterances=[model.Utterance(id="u1", seq=2, text="Текст")],
    )
    checkpoint = model.Checkpoint(
        meeting_id=ID,
        transcript_revision=1,
        input_manifest_sha256="a" * 64,
        processing_config_sha256="b" * 64,
    )
    sidecar = segments.SegmentSidecar(
        meeting_id=ID,
        epoch_id="e1",
        capture_id="c1",
        state_rev=2,
        pause_reasons=["lease"],
        close_reason="lease",
        clock="pipewire",
        clock_anchor=segments.ClockAnchor(200, 500),
    )
    for name, value, writer in (
        ("transcript.json", transcript, store.Writer.TRANSCRIBE),
        ("checkpoint.json", checkpoint, store.Writer.TRANSCRIBE),
        ("audio/mic-0000.json", sidecar, store.Writer.CAPTURE),
    ):
        disk.write(ID, name, value, writer=writer)
        assert disk.read(ID, name) == value
        assert stat.S_IMODE((disk.root / ID / name).stat().st_mode) == 0o600
    disk.write(
        ID,
        "meeting.json",
        model.Meeting(id=ID, started_at=STAMP, state=events.MeetingState.TRANSCRIBING),
        writer=store.Writer.GUI,
    )
    disk.write(
        ID,
        "meeting.json",
        model.Meeting(id=ID, started_at=STAMP, state=events.MeetingState.DONE),
        writer=store.Writer.GUI,
    )
    speakers = model.SpeakerMap(labels={"S1": model.Speaker(name="Имя")})
    disk.write(ID, "speakers.json", speakers, writer=store.Writer.GUI)
    assert disk.read(ID, "speakers.json") == speakers
    assert stat.S_IMODE((disk.root / ID / "speakers.json").stat().st_mode) == 0o600


@pytest.mark.parametrize("location,mode", [("root", 0o755), ("meeting", 0o770), ("audio", 0o755)])
def test_existing_unsafe_directory_permissions_refused(
    disk: store.MeetingStore, location: str, mode: int
) -> None:
    sidecar = segments.SegmentSidecar(meeting_id=ID, epoch_id="e1")
    disk.write(ID, "audio/mic-0000.json", sidecar, writer=store.Writer.CAPTURE)
    assert disk.read(ID, "audio/mic-0000.json") == sidecar
    path = {"root": disk.root, "meeting": disk.root / ID, "audio": disk.root / ID / "audio"}[
        location
    ]
    path.chmod(mode)
    with pytest.raises(store.MeetingStoreError):
        disk.read(ID, "audio/mic-0000.json" if location == "audio" else "meeting.json")
    assert stat.S_IMODE(path.stat().st_mode) == mode


def test_directory_fsync_failure_after_replace_leaves_complete_new_document(
    disk: store.MeetingStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    disk.write(ID, "meeting.json", model.Meeting(id=ID, started_at=STAMP), writer=store.Writer.GUI)
    original_fsync = os.fsync

    def fail_directory(descriptor: int) -> None:
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError("PRIVATE_MARKER")
        original_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", fail_directory)
    with pytest.raises(store.MeetingStoreError):
        disk.write(
            ID,
            "meeting.json",
            model.Meeting(id=ID, started_at=STAMP, title="new"),
            writer=store.Writer.GUI,
        )
    assert disk.read(ID, "meeting.json").title == "new"
    assert not list((disk.root / ID).glob(".*.tmp"))


@pytest.mark.parametrize("kind", ["directory", "writer"])
@pytest.mark.parametrize("error_type", [ValueError, OSError, TypeError, RecursionError])
def test_context_body_exception_is_not_rewritten(
    disk: store.MeetingStore, kind: str, error_type: type[Exception]
) -> None:
    error = error_type("caller-owned exception")
    manager = disk._directory(ID) if kind == "directory" else disk.writer(ID, store.Writer.GUI)
    with pytest.raises(error_type) as caught, manager:
        raise error
    assert caught.value is error


def test_create_obeys_root_namespace_lock(disk: store.MeetingStore) -> None:
    descriptor = os.open(disk.root / ".store.lock", os.O_RDWR | os.O_CREAT, 0o600)
    new_id = "2026-10-29_120001-ab12"
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(store.MeetingStoreError):
            disk.create(model.Meeting(id=new_id, started_at=STAMP))
        assert not (disk.root / new_id).exists()
    finally:
        os.close(descriptor)
    disk.create(model.Meeting(id=new_id, started_at=STAMP))


def test_create_refuses_existing_tombstone(disk: store.MeetingStore) -> None:
    new_id = "2026-10-29_120001-ab12"
    tombstone = disk.root / f".deleting-{new_id}"
    tombstone.mkdir(mode=0o700)
    with pytest.raises(store.MeetingStoreError):
        disk.create(model.Meeting(id=new_id, started_at=STAMP))
    assert tombstone.is_dir() and not (disk.root / new_id).exists()


def test_default_store_hardens_owned_data_directory_0755(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    data = tmp_path / "astra-voice"
    data.mkdir(mode=0o755)
    disk = store.MeetingStore()
    disk.create(model.Meeting(id=ID, started_at=STAMP))
    # Свой каталог данных не отвергается, а ужесточается до 0700, как в core/paths.py.
    assert stat.S_IMODE(data.stat().st_mode) == 0o700
    assert stat.S_IMODE(disk.root.stat().st_mode) == 0o700


def test_symlink_home_is_explicitly_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "real-home"
    home.mkdir()
    alias = tmp_path / "home"
    alias.symlink_to(home, target_is_directory=True)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setenv("HOME", str(alias))
    disk = store.MeetingStore()
    with pytest.raises(store.MeetingStoreError):
        disk.create(model.Meeting(id=ID, started_at=STAMP))
    assert not list(home.iterdir())


def test_non_reentrant_writer_fails_fast_and_original_remains_usable(
    disk: store.MeetingStore,
) -> None:
    with disk.writer(ID, store.Writer.TRANSCRIBE) as writer:
        with pytest.raises(store.MeetingStoreError):
            disk.write(
                ID,
                "transcript.json",
                model.Transcript(meeting_id=ID),
                writer=store.Writer.TRANSCRIBE,
            )
        writer.write("transcript.json", model.Transcript(meeting_id=ID))


def test_speaker_leases_precede_transaction_lock(
    disk: store.MeetingStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    disk.write(
        ID,
        "meeting.json",
        model.Meeting(id=ID, started_at=STAMP, state=events.MeetingState.TRANSCRIBING),
        writer=store.Writer.GUI,
    )
    disk.write(
        ID,
        "meeting.json",
        model.Meeting(id=ID, started_at=STAMP, state=events.MeetingState.DONE),
        writer=store.Writer.GUI,
    )
    original = store._lock
    with monkeypatch.context() as patch:
        spy = Mock(wraps=original)
        patch.setattr(store, "_lock", spy)
        disk.write(ID, "speakers.json", model.SpeakerMap(), writer=store.Writer.GUI)
    names = [call.args[1] for call in spy.call_args_list]
    assert names.index(".writer-diar.lock") < names.index(".store.lock")
    assert names.index(".writer-transcribe.lock") < names.index(".store.lock")


@pytest.mark.parametrize("stage", ["mkdir", "write"])
def test_partial_creation_is_rolled_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    disk = store.MeetingStore(tmp_path / "meetings")
    original = os.mkdir

    def fail_exports(path: str | Path, mode: int = 0o777, *, dir_fd: int | None = None) -> None:
        if path == "exports":
            raise OSError("PRIVATE_MARKER")
        original(path, mode, dir_fd=dir_fd)

    with monkeypatch.context() as patch:
        if stage == "mkdir":
            patch.setattr(os, "mkdir", fail_exports)
        else:
            patch.setattr(store, "write_private", Mock(side_effect=OSError("PRIVATE_MARKER")))
        with pytest.raises(store.MeetingStoreError):
            disk.create(model.Meeting(id=ID, started_at=STAMP))
    assert not (disk.root / ID).exists()
    disk.create(model.Meeting(id=ID, started_at=STAMP))


def test_metadata_rejects_state_regression_and_same_generation_uncancel(
    disk: store.MeetingStore,
) -> None:
    cancelled = model.Meeting(id=ID, started_at=STAMP, cancelled=True)
    disk.write(ID, "meeting.json", cancelled, writer=store.Writer.GUI)
    with pytest.raises(store.MeetingStoreError):
        disk.write(
            ID, "meeting.json", model.Meeting(id=ID, started_at=STAMP), writer=store.Writer.GUI
        )
    newer = model.Meeting(
        id=ID, started_at=STAMP, meeting_generation=1, state=events.MeetingState.TRANSCRIBING
    )
    disk.write(ID, "meeting.json", newer, writer=store.Writer.GUI)
    newer.state = events.MeetingState.DONE
    disk.write(ID, "meeting.json", newer, writer=store.Writer.GUI)
    newer.state = events.MeetingState.RECORDING
    with pytest.raises(store.MeetingStoreError):
        disk.write(ID, "meeting.json", newer, writer=store.Writer.GUI)


def test_new_lock_entry_syncs_parent(
    disk: store.MeetingStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    with disk._directory(ID) as descriptor:
        spy = Mock(wraps=os.fsync)
        monkeypatch.setattr(os, "fsync", spy)
        with store._lock(descriptor, ".new-test.lock"):
            pass
        spy.assert_called_with(descriptor)


def test_create_restores_private_mode_under_restrictive_umask(tmp_path: Path) -> None:
    disk = store.MeetingStore(tmp_path / "meetings")
    previous = os.umask(0o277)
    try:
        disk.create(model.Meeting(id=ID, started_at=STAMP))
    finally:
        os.umask(previous)
    assert stat.S_IMODE((disk.root / ID).stat().st_mode) == 0o700
    assert disk.read(ID, "meeting.json").id == ID
