"""Private meeting layout, typed single-writer leases and atomic JSON persistence.

No capture, retention scheduler or IPC lives here. Deletion uses pinned dirfds.
Persistent empty lease files keep flock identity stable across store instances.
Leases coordinate trusted roles; they are not isolation from the same Unix user.
One writer context per role/meeting: leases are non-reentrant and fail fast,
including across threads. Inside a context use owner.write(), not store.write().
All meeting-directory creation and future tombstone rename/removal must hold
the root .store.lock. Per-meeting transactions use a different .store.lock.
Symlink ancestors (including HOME) are refused; use a physical XDG_DATA_HOME.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
import string
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from astra_voice.core.paths import data_dir_path
from astra_voice.meetings import MeetingValidationError
from astra_voice.meetings.events import MeetingState, transition
from astra_voice.meetings.model import (
    MAX_DOCUMENT_BYTES as MAX_DOCUMENT_BYTES,
)
from astra_voice.meetings.model import (
    MEETING_ID_PATTERN,
    Checkpoint,
    JsonModel,
    Meeting,
    SpeakerMap,
    Transcript,
    published_generation,
)
from astra_voice.meetings.segments import CaptureJournal, SegmentSidecar
from astra_voice.security.files import (
    check_private_file,
    private_directory,
    read_private,
    write_private,
)

_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_SEGMENT_NAME = re.compile(r"audio/(mic|far)-([0-9]{4})\.json\Z")
_DOCUMENTS: dict[str, type[JsonModel]] = {
    "meeting.json": Meeting,
    "transcript.json": Transcript,
    "checkpoint.json": Checkpoint,
    "rediar-transcript.json": Transcript,
    "rediar-checkpoint.json": Checkpoint,
    "speakers.json": SpeakerMap,
    "capture.json": CaptureJournal,
}


class MeetingStoreError(Exception):
    """Storage failure with a constant, path/content-free message."""


class Writer(StrEnum):
    GUI = "gui"
    CAPTURE = "capture"
    TRANSCRIBE = "transcribe"
    DIAR = "diar"  # lifetime lease only; diar never writes durable data


class RediarPublishResult(StrEnum):
    """Queue contract: only PUBLISHED permits K6; BUSY/IO_ERROR need backoff.

    WORK_PENDING permits resuming the role. INVALID means failure before K3;
    REJECTED means the request no longer matches metadata. Neither should be
    inferred from an exception while acquiring the GUI writer itself.
    """

    PUBLISHED = "published"
    WORK_PENDING = "work_pending"
    BUSY = "busy"
    REJECTED = "rejected"
    INVALID = "invalid"
    IO_ERROR = "io_error"


class _RediarCommitUncertain(Exception):
    """A replacement may be visible but is not confirmed durable: retry."""


def new_meeting_id(start: datetime) -> str:
    if start.tzinfo is None:
        raise MeetingValidationError("Нужна временная зона встречи")
    suffix = "".join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(4))
    return f"{start:%Y-%m-%d_%H%M%S}-{suffix}"


def _id(value: str) -> None:
    if not isinstance(value, str) or re.fullmatch(MEETING_ID_PATTERN, value) is None:
        raise MeetingStoreError("Недопустимая идентичность встречи")


@contextlib.contextmanager
def _child(
    parent: int, name: str, *, create: bool = False, reserved: bool = False
) -> Iterator[int]:
    if create:
        os.mkdir(name, mode=0o700, dir_fd=parent)
        os.fsync(parent)
    descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent)
    try:
        if create or reserved:
            os.fchmod(descriptor, 0o700)
        info = os.fstat(descriptor)
        if info.st_uid != os.geteuid() or info.st_mode & 0o777 != 0o700:
            raise MeetingStoreError("Небезопасный каталог встречи")
        yield descriptor
    finally:
        os.close(descriptor)


@contextlib.contextmanager
def _lease(directory: int, role: Writer) -> Iterator[int]:
    with _lock(directory, f".writer-{role.value}.lock") as descriptor:
        yield descriptor


@contextlib.contextmanager
def _lock(directory: int, name: str) -> Iterator[int]:
    flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    created = False
    try:
        descriptor = os.open(name, flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=directory)
        created = True
    except FileExistsError:
        descriptor = os.open(name, flags, dir_fd=directory)
    try:
        if created:
            os.fchmod(descriptor, 0o600)
            os.fsync(directory)
        check_private_file(os.fstat(descriptor))
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield descriptor
    finally:
        os.close(descriptor)


def _document_type(filename: str) -> type[JsonModel]:
    if filename in _DOCUMENTS:
        return _DOCUMENTS[filename]
    if _SEGMENT_NAME.fullmatch(filename):
        return SegmentSidecar
    raise MeetingStoreError("Недопустимый тип файла встречи")


def _pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in items:
        if key in result:
            raise MeetingValidationError("Повтор поля документа")
        result[key] = value
    return result


def _read(directory: int, filename: str) -> JsonModel:
    cls = _document_type(filename)
    if filename.startswith("audio/"):
        with _child(directory, "audio") as audio:
            payload = read_private(
                Path(filename[6:]), MAX_DOCUMENT_BYTES, directory_fd=audio, private=True
            )
    else:
        payload = read_private(
            Path(filename), MAX_DOCUMENT_BYTES, directory_fd=directory, private=True
        )
    parsed = json.loads(payload, object_pairs_hook=_pairs)
    return cls.from_dict(parsed)


def _metadata(directory: int, meeting_id: str) -> Meeting:
    value = _read(directory, "meeting.json")
    if not isinstance(value, Meeting) or value.id != meeting_id:
        raise MeetingStoreError("Не совпадает идентичность встречи")
    return value


def _transcript_name(filename: str) -> str:
    return "rediar-transcript.json" if filename.startswith("rediar-") else "transcript.json"


def _check_rediar(meeting: Meeting, generation: int) -> None:
    if (
        meeting.state != MeetingState.DONE
        or meeting.rediar is None
        or generation != meeting.rediar.generation
        or generation != meeting.meeting_generation
    ):
        raise MeetingStoreError("Рабочий документ не соответствует повторному разделению")


@dataclass(frozen=True)
class _TranscriptState:
    """What this lease last saw durable in a transcript file (no utterances kept)."""

    meeting_id: str
    generation: int
    revision: int
    labels_final: bool
    digest: str


@dataclass(frozen=True, eq=False)
class PreparedDocument:
    """Validated and serialized by MeetingWriter.prepare; publish via owner.write().

    Bound to the writer that prepared it. Publication still rechecks every
    piece of disk state (tombstone, cancellation, generation, revision).
    """

    filename: str
    document: JsonModel
    data: dict[str, Any]
    raw: bytes
    digest: str
    owner: object


def _encode(data: dict[str, Any]) -> bytes:
    raw = (json.dumps(data, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
    if len(raw) > MAX_DOCUMENT_BYTES:
        raise MeetingStoreError("Документ встречи слишком велик")
    return raw


def _transcript_state(transcript: Transcript, raw: bytes) -> _TranscriptState:
    return _TranscriptState(
        transcript.meeting_id,
        transcript.meeting_generation,
        transcript.transcript_revision,
        transcript.labels_final,
        hashlib.sha256(raw).hexdigest(),
    )


def _rediar_final(transcript: Transcript, meeting_id: str, generation: int) -> bool:
    """One final-result validator for both sides of the K3 rename."""
    return (
        transcript.meeting_id == meeting_id
        and transcript.meeting_generation == generation
        and transcript.labels_final
        and all(u.final and u.seq == seq for seq, u in enumerate(transcript.utterances))
        and transcript.reconcile.status == "done"
        and transcript.reconcile.pending is None
        and transcript.reconcile.inflight_id is None
    )


def _rediar_unlink(directory: int, names: tuple[str, ...]) -> None:
    """Unlink fixed entries and only write_private's exact working temp names.

    Never opens entries or follows links; directories are refused by unlink.
    Writers must have released both lifetime leases before calling this.
    """
    temporary = re.compile(r"\.rediar-(?:transcript|checkpoint)-[0-9a-f]{32}\.tmp\Z")
    for name in (*names, *(n for n in os.listdir(directory) if temporary.fullmatch(n))):
        with contextlib.suppress(FileNotFoundError):
            os.unlink(name, dir_fd=directory)
    os.fsync(directory)


class MeetingWriter:
    """Valid only inside MeetingStore.writer; all writes recheck tombstone state.

    prepare() validates and serializes without any store lock, so a caller can
    keep its own publication critical section short; write() publishes under
    the per-meeting .store.lock. Each transcript has exactly one writer role and
    the lifetime lease excludes any other holder, so durable revisions are
    cached by filename instead of re-reading and re-validating the whole text
    on every publication. A failed publication drops that file's cache entry:
    the next use re-reads the disk.
    """

    def __init__(
        self, directory: int, meeting_id: str, role: Writer, *, lease_fd: int | None = None
    ) -> None:
        self._directory = directory
        self._meeting_id = meeting_id
        self._role = role
        self._active = True
        self._lease_fd = lease_fd
        self._transcripts: dict[str, _TranscriptState | None] = {}

    def diar_lease_fd(self) -> int:
        """Borrow the DIAR flock's open file description for child pass_fds.

        Never LOCK_UN it: closing the parent's descriptor must leave the lock
        held until the inherited child descriptor also closes. The child only
        keeps this fd open and never uses it for file I/O.
        """
        if not self._active or self._role != Writer.DIAR or self._lease_fd is None:
            raise MeetingStoreError("Дескриптор аренды разделения недоступен")
        return self._lease_fd

    def prepare(self, filename: str, document: JsonModel) -> PreparedDocument:
        try:
            if not self._active:
                raise MeetingStoreError("Писатель встречи закрыт")
            cls = _document_type(filename)
            allowed = (
                Writer.GUI
                if filename in {"meeting.json", "speakers.json"}
                else Writer.CAPTURE
                if cls in {SegmentSidecar, CaptureJournal}
                else Writer.TRANSCRIBE
            )
            if self._role != allowed or type(document) is not cls:
                raise MeetingStoreError("Неверный писатель файла встречи")
            if isinstance(document, SegmentSidecar):
                match = _SEGMENT_NAME.fullmatch(filename)
                if match is None or (document.track, document.index) != (match[1], int(match[2])):
                    raise MeetingStoreError("Не совпадает идентичность сегмента")
            data = document.to_dict()
            raw = _encode(data)
            if isinstance(document, (Transcript, Checkpoint)):
                # Warm this file's lease cache outside the store lock.
                self._known_transcript(_transcript_name(filename))
            return PreparedDocument(
                filename, document, data, raw, hashlib.sha256(raw).hexdigest(), self
            )
        except (OSError, ValueError, TypeError, RecursionError):
            raise MeetingStoreError("Не удалось сохранить данные встречи") from None

    def write(
        self,
        filename: str,
        document: JsonModel | PreparedDocument,
        *,
        expected_generation: int | None = None,
    ) -> None:
        try:
            if not self._active:
                raise MeetingStoreError("Писатель встречи закрыт")
            prepared = (
                document
                if isinstance(document, PreparedDocument)
                else self.prepare(filename, document)
            )
            if prepared.owner is not self or prepared.filename != filename:
                raise MeetingStoreError("Неверный писатель файла встречи")
            # A short transaction gate serializes state check + publication with
            # GUI cancellation/deletion; lifetime leases remain per role.
            # Publication leases precede the transaction gate in every role.
            # All acquisitions are nonblocking: contention is an expected error.
            with contextlib.ExitStack() as resources:
                if filename == "speakers.json" and isinstance(prepared.document, SpeakerMap):
                    resources.enter_context(_lease(self._directory, Writer.DIAR))
                    resources.enter_context(_lease(self._directory, Writer.TRANSCRIBE))
                resources.enter_context(_lock(self._directory, ".store.lock"))
                try:
                    self._publish(prepared, expected_generation=expected_generation)
                except BaseException:
                    if isinstance(prepared.document, Transcript):
                        self._transcripts.pop(filename, None)  # disk state unknown: re-read
                    raise
        except (OSError, ValueError, TypeError, RecursionError):
            raise MeetingStoreError("Не удалось сохранить данные встречи") from None

    def reset_speakers(self, generation: int) -> None:
        """GUI-only reset before admitting a new diar job; never races a live writer."""
        try:
            if not self._active or self._role != Writer.GUI:
                raise MeetingStoreError("Неверный писатель файла встречи")
            with contextlib.ExitStack() as resources:
                resources.enter_context(_lease(self._directory, Writer.DIAR))
                resources.enter_context(_lease(self._directory, Writer.TRANSCRIBE))
                resources.enter_context(_lock(self._directory, ".store.lock"))
                meeting = _metadata(self._directory, self._meeting_id)
                if (
                    meeting.state != MeetingState.TRANSCRIBING
                    or meeting.cancelled
                    or meeting.rediar is not None
                    or meeting.meeting_generation != generation
                ):
                    raise MeetingStoreError("Встреча недоступна для обработки")
                self._persist("speakers.json", _encode(SpeakerMap().to_dict()))
        except (OSError, ValueError, TypeError, RecursionError):
            raise MeetingStoreError("Не удалось сохранить данные встречи") from None

    def rediar_publish(self, generation: int) -> RediarPublishResult:
        """GUI K0–K5 transaction; caller performs K6 only after PUBLISHED.

        All leases are nonblocking, in DIAR/TRANSCRIBE/store order. After K3
        only commit recovery is allowed, even if the published file is damaged
        (IO_ERROR, never WORK_PENDING/INVALID). No metadata is changed here.
        A same-UID process can still swap the source between validation and
        rename: accepted P26 boundary, not isolation against that process.
        """
        if not self._active or self._role != Writer.GUI:
            return RediarPublishResult.REJECTED
        try:
            with contextlib.ExitStack() as resources:
                resources.enter_context(_lease(self._directory, Writer.DIAR))
                resources.enter_context(_lease(self._directory, Writer.TRANSCRIBE))
                resources.enter_context(_lock(self._directory, ".store.lock"))
                meeting = _metadata(self._directory, self._meeting_id)
                if (
                    meeting.state != MeetingState.DONE
                    or meeting.cancelled
                    or meeting.rediar is None
                    or generation != meeting.rediar.generation
                    or generation != meeting.meeting_generation
                ):
                    return RediarPublishResult.REJECTED
                raw = read_private(
                    Path("transcript.json"),
                    MAX_DOCUMENT_BYTES,
                    directory_fd=self._directory,
                    private=True,
                )
                published = Transcript.from_dict(json.loads(raw, object_pairs_hook=_pairs))
                if published.meeting_generation == generation:
                    # K0: generation establishes that K3 occurred. A damaged
                    # commit must never be mistaken for resumable role work.
                    if not _rediar_final(published, self._meeting_id, generation):
                        return RediarPublishResult.IO_ERROR
                    self._rediar_sync()
                else:
                    if (
                        published.meeting_id != self._meeting_id
                        or published.meeting_generation != published_generation(meeting)
                        or hashlib.sha256(raw).hexdigest() != meeting.rediar.base_sha256
                    ):
                        return RediarPublishResult.INVALID
                    try:
                        work_raw = read_private(
                            Path("rediar-transcript.json"),
                            MAX_DOCUMENT_BYTES,
                            directory_fd=self._directory,
                            private=True,
                        )
                    except FileNotFoundError:
                        return RediarPublishResult.WORK_PENDING
                    try:
                        work = Transcript.from_dict(json.loads(work_raw, object_pairs_hook=_pairs))
                    except (ValueError, TypeError, RecursionError):
                        return RediarPublishResult.INVALID
                    if not _rediar_final(work, self._meeting_id, generation):
                        # The role persists reconcile=done before its separate
                        # labels_final write. Any typed snapshot of this job
                        # without labels_final can still resume that work.
                        if (
                            work.meeting_id == self._meeting_id
                            and work.meeting_generation == generation
                            and not work.labels_final
                        ):
                            return RediarPublishResult.WORK_PENDING
                        return RediarPublishResult.INVALID
                    # K2 is the sole internal exception to I3, like reset_speakers.
                    names = _read(self._directory, "speakers.json")
                    assert isinstance(names, SpeakerMap)
                    if names.transcript_generation is None:
                        names.transcript_generation = published.meeting_generation
                        self._rediar_names(names)
                    elif names.transcript_generation != published.meeting_generation:
                        return RediarPublishResult.IO_ERROR
                    # Confirm K2 also after an uncertain prior write_private.
                    self._rediar_sync()
                    # K3: recheck the source without reading it or following
                    # symlinks/FIFOs, then rename fixed names under pinned fd.
                    source = os.open(
                        "rediar-transcript.json",
                        os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                        dir_fd=self._directory,
                    )
                    try:
                        check_private_file(os.fstat(source))
                        try:
                            os.replace(
                                "rediar-transcript.json",
                                "transcript.json",
                                src_dir_fd=self._directory,
                                dst_dir_fd=self._directory,
                            )
                        except OSError:
                            # A wrapper/syscall may report failure after rename.
                            if (
                                read_private(
                                    Path("transcript.json"),
                                    MAX_DOCUMENT_BYTES,
                                    directory_fd=self._directory,
                                    private=True,
                                )
                                == work_raw
                            ):
                                raise _RediarCommitUncertain from None
                            raise
                    finally:
                        os.close(source)
                    self._rediar_sync()
                # K4: avoid a second name replacement on an idempotent call.
                empty = SpeakerMap(transcript_generation=generation)
                if _read(self._directory, "speakers.json") != empty:
                    self._rediar_names(empty)
                # K5 also confirms a prior uncertain K4 and interrupted unlink.
                _rediar_unlink(self._directory, ("rediar-checkpoint.json", "checkpoint.json"))
                return RediarPublishResult.PUBLISHED
        except (BlockingIOError, _RediarCommitUncertain):
            return RediarPublishResult.BUSY
        except (OSError, ValueError, TypeError, RecursionError, MeetingStoreError):
            return RediarPublishResult.IO_ERROR
        finally:
            self._transcripts.clear()  # K3 can have committed even on an exception.

    def _rediar_sync(self) -> None:
        try:
            os.fsync(self._directory)
        except OSError:
            raise _RediarCommitUncertain from None

    def _rediar_names(self, names: SpeakerMap) -> None:
        raw = _encode(names.to_dict())
        try:
            self._persist("speakers.json", raw)
        except OSError:
            # write_private may have replaced the file before directory-fsync
            # failed. Do not roll back or discard; recognize K2/K4 next time.
            if (
                read_private(
                    Path("speakers.json"),
                    MAX_DOCUMENT_BYTES,
                    directory_fd=self._directory,
                    private=True,
                )
                == raw
            ):
                raise _RediarCommitUncertain from None
            raise

    def rediar_discard(self) -> None:
        """GUI cleanup after cancellation/K6; refuses any active rediar commit.

        Unlinks only the working pair and exact write_private temp templates,
        without following links. Errors have no document/path information.
        """
        try:
            if not self._active or self._role != Writer.GUI:
                raise MeetingStoreError("Неверный писатель файла встречи")
            with contextlib.ExitStack() as resources:
                resources.enter_context(_lease(self._directory, Writer.DIAR))
                resources.enter_context(_lease(self._directory, Writer.TRANSCRIBE))
                resources.enter_context(_lock(self._directory, ".store.lock"))
                meeting = _metadata(self._directory, self._meeting_id)
                if meeting.rediar is not None or meeting.state == MeetingState.DELETING:
                    raise MeetingStoreError("Рабочий результат ещё занят")
                _rediar_unlink(
                    self._directory, ("rediar-transcript.json", "rediar-checkpoint.json")
                )
        except (OSError, ValueError, TypeError, RecursionError):
            raise MeetingStoreError("Не удалось удалить рабочий результат") from None

    def remove(self, filename: str) -> None:
        """Drop a checkpoint only after its final text is durable (T-414)."""
        try:
            if not self._active:
                raise MeetingStoreError("Писатель встречи закрыт")
            if filename not in {"checkpoint.json", "rediar-checkpoint.json"} or (
                self._role != Writer.TRANSCRIBE
            ):
                raise MeetingStoreError("Неверный писатель файла встречи")
            with _lock(self._directory, ".store.lock"):
                meeting = _metadata(self._directory, self._meeting_id)
                if meeting.state == MeetingState.DELETING:
                    raise MeetingStoreError("Встреча удаляется")
                if meeting.cancelled:
                    raise MeetingStoreError("Обработка встречи отменена")
                if filename == "rediar-checkpoint.json":
                    _check_rediar(meeting, meeting.meeting_generation)
                elif meeting.rediar is not None:
                    raise MeetingStoreError("Опубликованный текст занят повторным разделением")
                transcript = self._known_transcript(_transcript_name(filename))
                if (
                    transcript is None
                    or not transcript.labels_final
                    or transcript.meeting_id != self._meeting_id
                    or transcript.generation != meeting.meeting_generation
                ):
                    raise MeetingStoreError("Текст встречи ещё не окончательный")
                try:
                    os.unlink(filename, dir_fd=self._directory)
                except FileNotFoundError:
                    return
                os.fsync(self._directory)
        except (OSError, ValueError, TypeError, RecursionError):
            raise MeetingStoreError("Не удалось сохранить данные встречи") from None

    def read_transcript(self, name: str = "transcript.json") -> Transcript | None:
        """Read either durable transcript (None if absent); prime its lease cache."""
        try:
            if not self._active:
                raise MeetingStoreError("Писатель встречи закрыт")
            if self._role != Writer.TRANSCRIBE:
                raise MeetingStoreError("Неверный писатель файла встречи")
            if name not in {"transcript.json", "rediar-transcript.json"}:
                raise MeetingStoreError("Недопустимый тип файла встречи")
            return self._load_transcript(name)
        except (OSError, ValueError, TypeError, RecursionError):
            raise MeetingStoreError("Не удалось прочитать данные встречи") from None

    def _load_transcript(self, name: str) -> Transcript | None:
        try:
            raw = read_private(
                Path(name),
                MAX_DOCUMENT_BYTES,
                directory_fd=self._directory,
                private=True,
            )
        except FileNotFoundError:
            parsed = None
        else:
            parsed = Transcript.from_dict(json.loads(raw, object_pairs_hook=_pairs))
            if parsed.meeting_id != self._meeting_id:
                raise MeetingStoreError("meeting-document-id")
            if name == "rediar-transcript.json":
                _check_rediar(
                    _metadata(self._directory, self._meeting_id), parsed.meeting_generation
                )
        self._transcripts[name] = None if parsed is None else _transcript_state(parsed, raw)
        return parsed

    def _known_transcript(self, name: str) -> _TranscriptState | None:
        """Durable state per filename as of this lease; read and validated once."""
        if name not in self._transcripts:
            self._load_transcript(name)
        return self._transcripts[name]

    def _publish(
        self, prepared: PreparedDocument, *, expected_generation: int | None = None
    ) -> None:
        filename, document, data = prepared.filename, prepared.document, prepared.data
        # Check the serialized snapshot, even if the caller mutated its model
        # after prepare(). The same snapshot is what _persist will publish.
        meeting = _metadata(self._directory, self._meeting_id)
        if expected_generation is not None and meeting.meeting_generation != expected_generation:
            raise MeetingStoreError("Не совпадает поколение встречи")
        if meeting.state == MeetingState.DELETING:
            raise MeetingStoreError("Встреча удаляется")
        if meeting.cancelled and filename != "meeting.json":
            raise MeetingStoreError("Обработка встречи отменена")
        if filename in {"transcript.json", "checkpoint.json"} and meeting.rediar is not None:
            raise MeetingStoreError("Опубликованный текст занят повторным разделением")
        if filename in {"rediar-transcript.json", "rediar-checkpoint.json"}:
            if self._role != Writer.TRANSCRIBE:
                raise MeetingStoreError("Неверный писатель файла встречи")
            _check_rediar(meeting, data["meeting_generation"])
        if isinstance(document, Meeting):
            if (
                data["id"] != self._meeting_id
                or data["meeting_generation"] < meeting.meeting_generation
            ):
                raise MeetingStoreError("Не совпадает идентичность встречи")
            target = MeetingState(data["state"])
            if target != meeting.state:
                transition(meeting.state, target)
            # Cancellation is monotonic within one generation. Only an explicit
            # new job (new generation) may clear it; never a stale GUI update.
            if (
                meeting.cancelled
                and not data["cancelled"]
                and meeting.meeting_generation == data["meeting_generation"]
            ):
                raise MeetingStoreError("Отмена встречи уже сохранена")
        if isinstance(document, (Transcript, Checkpoint, SegmentSidecar, CaptureJournal)):
            if (
                data["meeting_id"] != self._meeting_id
                or data["meeting_generation"] != meeting.meeting_generation
            ):
                raise MeetingStoreError("Не совпадает поколение встречи")
        if isinstance(document, Transcript):
            previous = self._known_transcript(filename)
            if previous is not None and (
                previous.generation == data["meeting_generation"]
                and (
                    previous.revision > data["transcript_revision"]
                    or (
                        previous.revision == data["transcript_revision"]
                        and previous.digest != prepared.digest
                        # Bytes differ (e.g. another encoder): compare as before.
                        and _read(self._directory, filename).to_dict() != data
                    )
                )
            ):
                raise MeetingStoreError("Ревизия текста уже опубликована")
        if isinstance(document, Checkpoint):
            current = self._known_transcript(_transcript_name(filename))
            if current is None or (
                current.generation != data["meeting_generation"]
                or current.revision != data["transcript_revision"]
                or current.meeting_id != data["meeting_id"]
            ):
                raise MeetingStoreError("Чекпойнт не ссылается на устойчивый текст")
        if isinstance(document, SpeakerMap):
            if meeting.state != MeetingState.DONE or meeting.rediar is not None:
                raise MeetingStoreError("Имена доступны после публикации")
            generation = data["transcript_generation"]
            if generation is not None and generation != published_generation(meeting):
                raise MeetingStoreError("Не совпадает поколение имён встречи")
        self._persist(filename, prepared.raw)
        if isinstance(document, Transcript):
            self._transcripts[filename] = _TranscriptState(
                data["meeting_id"],
                data["meeting_generation"],
                data["transcript_revision"],
                data["labels_final"],
                prepared.digest,
            )

    def _persist(self, filename: str, raw: bytes) -> None:
        if filename.startswith("audio/"):
            with _child(self._directory, "audio") as audio:
                write_private(Path(filename[6:]), raw, directory_fd=audio, private=True)
        else:
            write_private(Path(filename), raw, directory_fd=self._directory, private=True)


class MeetingStore:
    def __init__(self, root: Path | None = None) -> None:
        self.root = data_dir_path() / "meetings" if root is None else root.absolute()
        # data_dir_path() returns a path only; shared data may already be 0755.
        # Our private boundary starts at meetings/, never at the whole data dir.

    @contextlib.contextmanager
    def _directory(self, meeting_id: str, *, create: bool = False) -> Iterator[int]:
        _id(meeting_id)
        resources = contextlib.ExitStack()
        try:
            root = resources.enter_context(private_directory(self.root, create=create))
            directory = resources.enter_context(_child(root, meeting_id, create=create))
        except (OSError, ValueError, TypeError, RecursionError):
            resources.close()
            raise MeetingStoreError("Каталог встречи недоступен") from None
        except BaseException:
            resources.close()
            raise
        with resources:
            yield directory

    def reserve(self, meeting_id: str) -> None:
        """Prepare-only directory: no meeting metadata and no capture writer yet."""
        try:
            _id(meeting_id)
            with private_directory(self.root, create=True) as root, _lock(root, ".store.lock"):
                if f".deleting-{meeting_id}" in os.listdir(root):
                    raise MeetingStoreError("Встреча удаляется")
                os.mkdir(meeting_id, mode=0o700, dir_fd=root)
                try:
                    os.chmod(meeting_id, 0o700, dir_fd=root, follow_symlinks=False)
                    os.fsync(root)
                    with _child(root, meeting_id) as directory:
                        write_private(
                            Path(".preparing"),
                            b"1\n",
                            directory_fd=directory,
                            create_only=True,
                            private=True,
                        )
                except BaseException:
                    # mkdir succeeded exclusively: remove only our marker/empty dir.
                    with _child(root, meeting_id) as directory:
                        with contextlib.suppress(FileNotFoundError):
                            os.unlink(".preparing", dir_fd=directory)
                    os.rmdir(meeting_id, dir_fd=root)
                    raise
        except (OSError, ValueError, TypeError):
            raise MeetingStoreError("Не удалось подготовить встречу") from None

    @staticmethod
    def _reservation(directory: int) -> bool:
        try:
            marker = read_private(Path(".preparing"), 2, directory_fd=directory, private=True)
        except FileNotFoundError:
            return False
        if marker != b"1\n":
            raise MeetingStoreError("Недопустимая подготовка встречи")
        return True

    @staticmethod
    def _reservation_children(directory: int) -> tuple[set[str], set[str]]:
        """Validate an interrupted pre-publication commit, without removing it."""
        entries = set(os.listdir(directory)) - {".preparing"}
        temporaries = {
            name for name in entries if re.fullmatch(r"\.meeting-[0-9a-f]{32}\.tmp", name)
        }
        children = entries - temporaries
        if len(temporaries) > 1:
            raise MeetingStoreError("Подготовка встречи непуста")
        if not children <= {"audio", "exports", "ai"}:
            raise MeetingStoreError("Подготовка встречи непуста")
        for name in children:
            with _child(directory, name) as child:
                if os.listdir(child):
                    raise MeetingStoreError("Подготовка встречи непуста")
        for name in temporaries:
            descriptor = os.open(
                name,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                dir_fd=directory,
            )
            try:
                info = os.fstat(descriptor)
                check_private_file(info)
                if info.st_size > MAX_DOCUMENT_BYTES:
                    raise MeetingStoreError("Подготовка встречи непуста")
            finally:
                os.close(descriptor)
        return children, temporaries

    def commit_reserved(self, meeting: Meeting) -> None:
        """Publish metadata before granting open; reservations never overwrite data."""
        try:
            raw = (json.dumps(meeting.to_dict(), ensure_ascii=False) + "\n").encode()
            with private_directory(self.root) as root, _lock(root, ".store.lock"):
                with _child(root, meeting.id) as directory:
                    if not self._reservation(directory) or set(os.listdir(directory)) != {
                        ".preparing"
                    }:
                        raise MeetingStoreError("Подготовка встречи недоступна")
                    try:
                        for name in ("audio", "exports", "ai"):
                            with _child(directory, name, create=True):
                                pass
                        write_private(
                            Path("meeting.json"),
                            raw,
                            directory_fd=directory,
                            create_only=True,
                            private=True,
                        )
                    except BaseException:
                        # Only empty, known directories created by this transaction.
                        # Published metadata is retained if directory fsync failed.
                        if "meeting.json" not in os.listdir(directory):
                            for name in ("audio", "exports", "ai"):
                                with contextlib.suppress(FileNotFoundError):
                                    os.rmdir(name, dir_fd=directory)
                        raise
                    os.unlink(".preparing", dir_fd=directory)
                    os.fsync(directory)
        except (OSError, ValueError, TypeError):
            raise MeetingStoreError("Не удалось создать встречу") from None

    def discard_reserved(self, meeting_id: str) -> None:
        """Caller confirms capture exit first. Refuse any data, links or active metadata."""
        try:
            _id(meeting_id)
            with private_directory(self.root) as root, _lock(root, ".store.lock"):
                with _child(root, meeting_id) as directory:
                    if not self._reservation(directory):
                        raise MeetingStoreError("Подготовка встречи непуста")
                    children, temporaries = self._reservation_children(directory)
                    for name in children:
                        os.rmdir(name, dir_fd=directory)
                    for name in temporaries:
                        os.unlink(name, dir_fd=directory)
                    os.unlink(".preparing", dir_fd=directory)
                os.rmdir(meeting_id, dir_fd=root)
                os.fsync(root)
        except (OSError, ValueError, TypeError):
            raise MeetingStoreError("Не удалось отменить подготовку") from None

    def create(self, meeting: Meeting) -> None:
        """Reserve a new ID exclusively; never overwrite or resurrect a tombstone."""
        try:
            raw = (
                json.dumps(meeting.to_dict(), ensure_ascii=False, allow_nan=False) + "\n"
            ).encode()
            with private_directory(self.root, create=True) as root, _lock(root, ".store.lock"):
                _id(meeting.id)
                try:
                    os.stat(f".deleting-{meeting.id}", dir_fd=root, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    raise MeetingStoreError("Встреча удаляется")
                os.mkdir(meeting.id, mode=0o700, dir_fd=root)
                try:
                    os.fsync(root)
                    with _child(root, meeting.id, reserved=True) as directory:
                        for name in ("audio", "exports", "ai"):
                            with _child(directory, name, create=True):
                                pass
                        write_private(
                            Path("meeting.json"),
                            raw,
                            directory_fd=directory,
                            create_only=True,
                            private=True,
                        )
                except BaseException:
                    self._rollback_create(root, meeting.id)
                    raise
        except (OSError, ValueError, TypeError, RecursionError):
            raise MeetingStoreError("Не удалось создать встречу") from None

    @staticmethod
    def _rollback_create(root: int, meeting_id: str) -> None:
        """Best effort for this newly reserved ID only; never recursively follow links."""
        try:
            with _child(root, meeting_id) as directory:
                for name in ("audio", "exports", "ai"):
                    with contextlib.suppress(OSError):
                        os.rmdir(name, dir_fd=directory)
                with contextlib.suppress(FileNotFoundError):
                    os.unlink("meeting.json", dir_fd=directory)
            os.rmdir(meeting_id, dir_fd=root)
            os.fsync(root)
        except (OSError, MeetingStoreError):
            pass

    def begin_delete(self, meeting_id: str) -> None:
        """Rename only a durably marked meeting; repeatable after a crash."""
        _id(meeting_id)
        try:
            with private_directory(self.root) as root, _lock(root, ".store.lock"):
                name = f".deleting-{meeting_id}"
                if name in os.listdir(root):
                    if meeting_id in os.listdir(root):
                        raise MeetingStoreError("Конфликт удаления встречи")
                    return
                with _child(root, meeting_id) as directory, _lock(directory, ".store.lock"):
                    if _metadata(directory, meeting_id).state != MeetingState.DELETING:
                        raise MeetingStoreError("Встреча не помечена для удаления")
                    os.rename(meeting_id, name, src_dir_fd=root, dst_dir_fd=root)
                    os.fsync(root)
        except (OSError, ValueError, TypeError):
            raise MeetingStoreError("Не удалось начать удаление встречи") from None

    def pending_deletions(self) -> list[str]:
        """Include empty crash remnants and links, without opening their targets."""
        try:
            with private_directory(self.root) as root:
                return sorted(
                    name[len(".deleting-") :]
                    for name in os.listdir(root)
                    if name.startswith(".deleting-")
                    and re.fullmatch(MEETING_ID_PATTERN, name[len(".deleting-") :])
                )
        except FileNotFoundError:
            return []
        except (OSError, ValueError):
            raise MeetingStoreError("Не удалось прочитать удаления встреч") from None

    @staticmethod
    def _erase(
        parent: int, name: str, *, device: int, expected: os.stat_result | None = None
    ) -> None:
        info = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if info.st_dev != device:
            raise MeetingStoreError("Другая файловая система встречи")
        if expected is not None and (info.st_dev, info.st_ino) != (
            expected.st_dev,
            expected.st_ino,
        ):
            raise MeetingStoreError("Каталог встречи изменился")
        if info.st_uid != os.geteuid():
            raise MeetingStoreError("Чужой владелец файлов встречи")
        if stat.S_ISDIR(info.st_mode):
            descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent)
            try:
                opened = os.fstat(descriptor)
                if opened.st_dev != device:
                    raise MeetingStoreError("Другая файловая система встречи")
                if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                    raise MeetingStoreError("Каталог встречи изменился")
                with os.scandir(descriptor) as iterator:
                    entries = list(iterator)
                for entry in entries:
                    MeetingStore._erase(descriptor, entry.name, device=device)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            current = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
                raise MeetingStoreError("Каталог встречи изменился")
            os.rmdir(name, dir_fd=parent)
        else:
            # No file is read: 0644 remnants and symlinks are unlinked, never followed.
            os.unlink(name, dir_fd=parent)

    def purge_deleted(self, meeting_id: str) -> None:
        """Fail fast on a live lease; unlink only beneath the pinned private root."""
        _id(meeting_id)
        try:
            with private_directory(self.root) as root, _lock(root, ".store.lock"):
                name = f".deleting-{meeting_id}"
                try:
                    info = os.stat(name, dir_fd=root, follow_symlinks=False)
                except FileNotFoundError:
                    return
                device = os.fstat(root).st_dev
                if info.st_dev != device:
                    raise MeetingStoreError("Другая файловая система встречи")
                if info.st_uid != os.geteuid():
                    raise MeetingStoreError("Чужой владелец файлов встречи")
                if stat.S_ISLNK(info.st_mode):
                    os.unlink(name, dir_fd=root)
                elif stat.S_ISDIR(info.st_mode):
                    with _child(root, name) as directory, contextlib.ExitStack() as leases:
                        if os.fstat(directory).st_dev != device:
                            raise MeetingStoreError("Другая файловая система встречи")
                        # Include GUI/DIAR leases too; future AI owners must join this barrier.
                        for role in Writer:
                            leases.enter_context(_lease(directory, role))
                        self._erase(root, name, device=device, expected=os.fstat(directory))
                else:
                    raise MeetingStoreError("Недопустимый каталог удаления встречи")
                os.fsync(root)
        except (OSError, ValueError, TypeError, RecursionError):
            raise MeetingStoreError("Не удалось удалить файлы встречи") from None

    def read(self, meeting_id: str, filename: str) -> Any:
        try:
            with self._directory(meeting_id) as directory:
                meeting = _metadata(directory, meeting_id)
                document = _read(directory, filename)
                if isinstance(document, (Transcript, Checkpoint, SegmentSidecar, CaptureJournal)):
                    if document.meeting_id != meeting_id:
                        raise MeetingStoreError("meeting-document-id") from None
                    if filename in {"rediar-transcript.json", "rediar-checkpoint.json"}:
                        _check_rediar(meeting, document.meeting_generation)
                return document
        except (OSError, ValueError, TypeError, RecursionError):
            raise MeetingStoreError("Не удалось прочитать данные встречи") from None

    def transcript_state(self, meeting_id: str) -> _TranscriptState:
        """Typed private snapshot of published bytes, including their SHA256.

        No utterance text escapes in the return value or an error. The digest
        is of the exact bytes validated, not of a re-encoded model.
        """
        try:
            with self._directory(meeting_id) as directory:
                raw = read_private(
                    Path("transcript.json"),
                    MAX_DOCUMENT_BYTES,
                    directory_fd=directory,
                    private=True,
                )
                text = Transcript.from_dict(json.loads(raw, object_pairs_hook=_pairs))
                if text.meeting_id != meeting_id:
                    raise MeetingStoreError("Не совпадает идентичность встречи")
                return _transcript_state(text, raw)
        except (OSError, ValueError, TypeError, RecursionError):
            raise MeetingStoreError("Не удалось прочитать состояние текста встречи") from None

    def list(self, *, include_deleting: bool = False) -> list[Meeting]:
        """Read metadata only, newest first, without creating a data directory.

        Service entries and deletion tombstones are omitted. A damaged or unsafe
        live entry fails the catalogue as a whole, so the UI can offer retry
        instead of silently hiding a saved meeting. Directory handles pin every
        read; no paths or document contents enter the error message.
        """
        resources = contextlib.ExitStack()
        try:
            try:
                root = resources.enter_context(private_directory(self.root))
            except FileNotFoundError:
                return []
            with resources:
                result: list[Meeting] = []
                for name in os.listdir(root):
                    if re.fullmatch(MEETING_ID_PATTERN, name) is None:
                        continue
                    try:
                        os.stat(f".deleting-{name}", dir_fd=root, follow_symlinks=False)
                    except FileNotFoundError:
                        pass
                    else:
                        continue
                    with _child(root, name) as directory:
                        if self._reservation(directory) and "meeting.json" not in os.listdir(
                            directory
                        ):
                            self._reservation_children(directory)
                            continue
                        meeting = _metadata(directory, name)
                    if include_deleting or meeting.state != MeetingState.DELETING:
                        result.append(meeting)
                return sorted(
                    result,
                    key=lambda row: (datetime.fromisoformat(row.started_at), row.id),
                    reverse=True,
                )
        except (OSError, ValueError, TypeError, RecursionError, MeetingStoreError):
            raise MeetingStoreError("Не удалось прочитать список встреч") from None
        finally:
            resources.close()

    @contextlib.contextmanager
    def writer(self, meeting_id: str, role: Writer) -> Iterator[MeetingWriter]:
        """Single non-reentrant lifetime lease; contention fails without waiting."""
        resources = contextlib.ExitStack()
        try:
            if not isinstance(role, Writer):
                raise MeetingStoreError("Неизвестный писатель встречи")
            directory = resources.enter_context(self._directory(meeting_id))
            lease_fd = resources.enter_context(_lease(directory, role))
            meeting = _metadata(directory, meeting_id)
            if meeting.state == MeetingState.DELETING:
                raise MeetingStoreError("Встреча удаляется")
            writer = MeetingWriter(directory, meeting_id, role, lease_fd=lease_fd)
        except (OSError, ValueError, TypeError, RecursionError):
            resources.close()
            raise MeetingStoreError("Писатель встречи недоступен") from None
        except BaseException:
            resources.close()
            raise
        with resources:
            try:
                yield writer
            finally:
                writer._active = False

    def write(
        self,
        meeting_id: str,
        filename: str,
        document: JsonModel,
        *,
        writer: Writer,
        expected_generation: int | None = None,
    ) -> None:
        with self.writer(meeting_id, writer) as owner:
            owner.write(filename, document, expected_generation=expected_generation)
