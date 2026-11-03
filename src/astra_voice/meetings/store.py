"""Private meeting layout, typed single-writer leases and atomic JSON persistence.

No capture, cleanup, retention scheduler, IPC or recursive deletion lives here.
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
def _lease(directory: int, role: Writer) -> Iterator[None]:
    with _lock(directory, f".writer-{role.value}.lock"):
        yield


@contextlib.contextmanager
def _lock(directory: int, name: str) -> Iterator[None]:
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
        yield
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


@dataclass(frozen=True)
class _TranscriptState:
    """What this lease last saw durable in transcript.json (no utterances kept)."""

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


class MeetingWriter:
    """Valid only inside MeetingStore.writer; all writes recheck tombstone state.

    prepare() validates and serializes without any store lock, so a caller can
    keep its own publication critical section short; write() publishes under
    the per-meeting .store.lock. transcript.json has exactly one writer role and
    the lifetime lease excludes any other holder, so the last durable revision
    is cached for the lease instead of re-reading and re-validating the whole
    text on every publication. Any failed transcript publication drops the
    cache: the next use re-reads the disk.
    """

    def __init__(self, directory: int, meeting_id: str, role: Writer) -> None:
        self._directory = directory
        self._meeting_id = meeting_id
        self._role = role
        self._active = True
        self._transcript: _TranscriptState | None = None
        self._transcript_known = False

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
                self._known_transcript()  # warm the lease cache outside the store lock
            return PreparedDocument(
                filename, document, data, raw, hashlib.sha256(raw).hexdigest(), self
            )
        except (OSError, ValueError, TypeError, RecursionError):
            raise MeetingStoreError("Не удалось сохранить данные встречи") from None

    def write(self, filename: str, document: JsonModel | PreparedDocument) -> None:
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
                    self._publish(prepared)
                except BaseException:
                    if isinstance(prepared.document, Transcript):
                        self._transcript_known = False  # disk state unknown: re-read
                    raise
        except (OSError, ValueError, TypeError, RecursionError):
            raise MeetingStoreError("Не удалось сохранить данные встречи") from None

    def remove(self, filename: str) -> None:
        """Transcribe drops checkpoint.json only after the final text is durable (T-414)."""
        try:
            if not self._active:
                raise MeetingStoreError("Писатель встречи закрыт")
            if filename != "checkpoint.json" or self._role != Writer.TRANSCRIBE:
                raise MeetingStoreError("Неверный писатель файла встречи")
            with _lock(self._directory, ".store.lock"):
                meeting = _metadata(self._directory, self._meeting_id)
                if meeting.state == MeetingState.DELETING:
                    raise MeetingStoreError("Встреча удаляется")
                if meeting.cancelled:
                    raise MeetingStoreError("Обработка встречи отменена")
                transcript = self._known_transcript()
                if transcript is None or not transcript.labels_final:
                    raise MeetingStoreError("Текст встречи ещё не окончательный")
                try:
                    os.unlink(filename, dir_fd=self._directory)
                except FileNotFoundError:
                    return
                os.fsync(self._directory)
        except (OSError, ValueError, TypeError, RecursionError):
            raise MeetingStoreError("Не удалось сохранить данные встречи") from None

    def read_transcript(self) -> Transcript | None:
        """Durable transcript.json (None if absent); also primes the lease cache."""
        try:
            if not self._active:
                raise MeetingStoreError("Писатель встречи закрыт")
            if self._role != Writer.TRANSCRIBE:
                raise MeetingStoreError("Неверный писатель файла встречи")
            return self._load_transcript()
        except (OSError, ValueError, TypeError, RecursionError):
            raise MeetingStoreError("Не удалось прочитать данные встречи") from None

    def _load_transcript(self) -> Transcript | None:
        try:
            raw = read_private(
                Path("transcript.json"),
                MAX_DOCUMENT_BYTES,
                directory_fd=self._directory,
                private=True,
            )
        except FileNotFoundError:
            parsed = None
        else:
            parsed = Transcript.from_dict(json.loads(raw, object_pairs_hook=_pairs))
        self._transcript = None if parsed is None else _transcript_state(parsed, raw)
        self._transcript_known = True
        return parsed

    def _known_transcript(self) -> _TranscriptState | None:
        """Durable transcript.json as of this lease; read and validated once."""
        if not self._transcript_known:
            self._load_transcript()
        return self._transcript

    def _publish(self, prepared: PreparedDocument) -> None:
        filename, document, data = prepared.filename, prepared.document, prepared.data
        meeting = _metadata(self._directory, self._meeting_id)
        if meeting.state == MeetingState.DELETING:
            raise MeetingStoreError("Встреча удаляется")
        if meeting.cancelled and filename != "meeting.json":
            raise MeetingStoreError("Обработка встречи отменена")
        if isinstance(document, Meeting):
            if (
                document.id != self._meeting_id
                or document.meeting_generation < meeting.meeting_generation
            ):
                raise MeetingStoreError("Не совпадает идентичность встречи")
            target = MeetingState(data["state"])
            if target != meeting.state:
                transition(meeting.state, target)
            # Cancellation is monotonic within one generation. Only an explicit
            # new job (new generation) may clear it; never a stale GUI update.
            if (
                meeting.cancelled
                and not document.cancelled
                and meeting.meeting_generation == document.meeting_generation
            ):
                raise MeetingStoreError("Отмена встречи уже сохранена")
        if isinstance(document, (Transcript, Checkpoint, SegmentSidecar, CaptureJournal)):
            if (
                document.meeting_id != self._meeting_id
                or document.meeting_generation != meeting.meeting_generation
            ):
                raise MeetingStoreError("Не совпадает поколение встречи")
        if isinstance(document, Transcript):
            previous = self._known_transcript()
            if previous is not None and (
                previous.generation == document.meeting_generation
                and (
                    previous.revision > document.transcript_revision
                    or (
                        previous.revision == document.transcript_revision
                        and previous.digest != prepared.digest
                        # Bytes differ (e.g. another encoder): compare as before.
                        and _read(self._directory, "transcript.json").to_dict() != data
                    )
                )
            ):
                raise MeetingStoreError("Ревизия текста уже опубликована")
        if isinstance(document, Checkpoint):
            current = self._known_transcript()
            if current is None or (
                current.generation != document.meeting_generation
                or current.revision != document.transcript_revision
                or current.meeting_id != document.meeting_id
            ):
                raise MeetingStoreError("Чекпойнт не ссылается на устойчивый текст")
        if isinstance(document, SpeakerMap):
            if meeting.state != MeetingState.DONE:
                raise MeetingStoreError("Имена доступны после публикации")
        self._persist(filename, prepared.raw)
        if isinstance(document, Transcript):
            self._transcript = _transcript_state(document, prepared.raw)
            self._transcript_known = True

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

    def read(self, meeting_id: str, filename: str) -> Any:
        try:
            with self._directory(meeting_id) as directory:
                _metadata(directory, meeting_id)
                return _read(directory, filename)
        except (OSError, ValueError, TypeError, RecursionError):
            raise MeetingStoreError("Не удалось прочитать данные встречи") from None

    def list(self) -> list[Meeting]:
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
                    if meeting.state != MeetingState.DELETING:
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
            resources.enter_context(_lease(directory, role))
            meeting = _metadata(directory, meeting_id)
            if meeting.state == MeetingState.DELETING:
                raise MeetingStoreError("Встреча удаляется")
            writer = MeetingWriter(directory, meeting_id, role)
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

    def write(self, meeting_id: str, filename: str, document: JsonModel, *, writer: Writer) -> None:
        with self.writer(meeting_id, writer) as owner:
            owner.write(filename, document)
