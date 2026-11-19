"""GUI-side transcription queue (E13.M4): one job at a time, durable lifecycle.

No Qt here. Filesystem work (meeting.json read-modify-write, model probe,
MemAvailable, audio removal) runs on a submitted worker and publishes a plain
completion that tick() applies on the GUI thread. The role contract lives in
worker/meeting_transcribe.py: ack -> model.loaded / job.accepted come
asynchronously, ``waiting{memory}`` ends the job (the 60 s retry is ours),
damaged/unrecognized marks are utterances with constant text, an empty text is
hidden here.

Durability (D12): an explicit cancel is written (``cancelled=true``) before
``meeting.job.cancel`` is sent and is never resumed after a restart; a delete
writes the tombstone, cancels and waits for a confirmed process exit before it
reports the files released. A program exit never cancels: the role is stopped,
its checkpoint stays durable and the job resumes on the next start (T-478).
"""

from __future__ import annotations

import os
import queue
import time
from collections import deque
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from uuid import uuid4

from astra_voice.core.meeting_session import MetadataWriter
from astra_voice.meetings import MeetingValidationError
from astra_voice.meetings.events import MeetingState, ProcessingStatus, transition
from astra_voice.meetings.model import FAILURE_CODES, Meeting, normalize_text, utterance_order
from astra_voice.meetings.store import (
    MeetingStore,
    MeetingStoreBusy,
    MeetingStoreError,
    RediarPublishResult,
    Writer,
    audio_succeeded,
)
from astra_voice.security.files import private_directory

Message = dict[str, Any]
ROLE = "meeting-transcribe"
INITIAL_CREDITS = 64
CREDIT_STEP = 32
MEMORY_MARGIN_MB = 200
DIAR_MEMORY_MB = 900
MEMORY_RETRY_S = 60.0
MODEL_RETRY_S = 60.0
CANCEL_TIMEOUT_S = 1.0  # worker/meeting_contract.CANCEL_TIMEOUT_S
DELETE_PURGE_ATTEMPTS = 3
DELETE_PURGE_RETRY_LATER_S = 300.0
DELETE_PURGE_RETRY_S = 0.5
RETENTION_INTERVAL_S = 86400
AUDIO_CLEANUP_RETRY_S = 5.0
MAX_CRASHES = 3  # restarts per window; the fourth crash fails the job
CRASH_WINDOW_S = 600.0
# model.load answered by an unexpected error (timeout, limit, ...): crash path, retry later.
LOAD_RETRY_S = 1.0
# ``not-prepared``: the role still loads/unloads a model (a cancelled load, model.unload
# after done). It is bounded by the supervisor's 60 s model.load deadline, so the job
# waits that long before such answers start counting as crashes.
NOT_PREPARED_BUDGET_S = 90.0
# D12: cancelled=true could not be written. meeting.job.cancel waits for the durable
# write; it is retried by tick and after the last attempt the user sees «Ошибка».
CANCEL_WRITE_ATTEMPTS = 3
CANCEL_RETRY_S = 1.0
ADMIT_RETRY_S = 5.0  # admission work raised/was not accepted by the writer: retry later
WRITE_ATTEMPTS = 3  # GUI-lease contention with the capture controller is short
LIMITS = {"max_ms": 14_400_000, "max_utterances": 28_800, "max_blocks": 12}
GIGAAM_LAYOUT = "onnx-asr-gigaam-v3"
GIGAAM_PREFERENCE = (
    "gigaam-v3-e2e-rnnt-int8",
    "gigaam-v3-rnnt-int8",
    "gigaam-v3-e2e-ctc-int8",
    "gigaam-v3-ctc-int8",
)
DEFAULT_MIN_RAM_MB = 768  # core/model_request.build_model_load default
# Constant mark texts of the role (worker/meeting_transcribe.py); kept, not hidden.
UNRECOGNIZED_TEXT = "[не распознано]"
DAMAGED_TEXT = "[кусок записи повреждён]"
RESUMABLE = frozenset(
    {MeetingState.TRANSCRIBING, MeetingState.WAITING_MEMORY, MeetingState.NEEDS_MODEL}
)
TRANSCRIBABLE = frozenset(
    {MeetingState.RECORDED, MeetingState.INTERRUPTED, MeetingState.FAILED, MeetingState.DONE}
)
FAILED_TEXT = {
    "model": "Распознавание на этом компьютере не работает",
    "memory": "Расшифровка подождёт: сейчас мало свободной памяти",
}
FAILED_DEFAULT_TEXT = "Запись сохранена и не удалится, пока вы её не удалите."
CANCELLED_TEXT = "Расшифровка отменена. Запись сохранена — можно начать снова."


class JobRole(Protocol):
    """WorkerSupervisor surface used here (role ``meeting-transcribe``)."""

    generation: int

    @property
    def ready(self) -> bool: ...
    @property
    def exited(self) -> bool: ...
    def start(self) -> None: ...
    def send(self, message: Message, *, timeout: float | None = None) -> Message: ...
    def set_meeting_generation(self, meeting_id: str, generation: int) -> None: ...
    def kill(self) -> None: ...
    def stop(self) -> None: ...


RoleFactory = Callable[[Callable[[Message], None], Callable[[int], None]], JobRole]
# Lifecycle events carry a Meeting; the aggregate ``retention`` event carries a count.
Notify = Callable[[str, Meeting | int], None]


@dataclass(frozen=True)
class ModelAdmission:
    """``ok`` (trusted GigaAM v3 with a model.load request), ``missing`` or ``broken``."""

    status: str
    load: Message | None = None

    @property
    def min_ram_mb(self) -> int:
        return int((self.load or {}).get("min_ram_mb", DEFAULT_MIN_RAM_MB))


def probe_gigaam_model(
    *,
    store: Any = None,
    min_ram_mb: Callable[[str], int | None] = lambda model_id: None,
    revoked: Callable[[str, str], bool] | None = None,
) -> ModelAdmission:
    """Installed and trusted GigaAM v3 (F4.9 self-check passed, not revoked).

    A revision that failed the self-check or was revoked is ``broken``: the
    job fails with ``model`` before any inference, the audio stays (T-482).
    """
    from astra_voice.models.store import ModelStore, StoreError

    models = store if store is not None else ModelStore()
    if revoked is None:
        from astra_voice.models.catalog_effective import check_revoked

        def revoked(model_id: str, revision: str) -> bool:
            try:
                check_revoked(model_id, revision, None)
            except StoreError as error:
                return error.code == "revoked"
            return False

    try:
        records = [record for record in models.records() if record.layout == GIGAAM_LAYOUT]
    except (OSError, StoreError, ValueError):
        return ModelAdmission("missing")
    if not records:
        return ModelAdmission("missing")

    def rank(record: Any) -> tuple[int, str]:
        order = (
            GIGAAM_PREFERENCE.index(record.id)
            if record.id in GIGAAM_PREFERENCE
            else len(GIGAAM_PREFERENCE)
        )
        return order, record.id

    for record in sorted(records, key=rank):
        trusted = record.state == "ok" and not record.recheck and record.metadata_ok
        if not trusted or revoked(record.id, record.revision):
            continue
        return ModelAdmission(
            "ok",
            {
                "id": record.id,
                "revision": record.revision,
                "dir": str(record.dir),
                "layout": record.layout,
                "variant": record.variant,
                "threads": 2,
                "min_ram_mb": int(min_ram_mb(record.id) or DEFAULT_MIN_RAM_MB),
            },
        )
    return ModelAdmission("broken")


def mem_available_mb() -> int | None:
    try:
        with open("/proc/meminfo", encoding="ascii") as stream:
            for line in stream:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


def remove_audio(store: MeetingStore, meeting_id: str) -> None:
    """Compatibility seam; the store owns every lock, check and unlink."""
    store.remove_audio(meeting_id)


@dataclass
class _Entry:
    meeting_id: str
    order: int
    status: str = "queued"  # queued | waiting_memory | needs_model
    retry_at: float = 0.0
    resume: bool = False
    redo: bool = False
    notified: set[str] = field(default_factory=set)


@dataclass
class _Active:
    meeting: Meeting
    admission: ModelAdmission
    resume: bool
    diar_models: dict[str, str] | None = None
    redo: bool = False
    stage: str = ""
    phase: str = "launching"
    # launching -> loading -> starting -> running; cancelling -> exiting; crashed
    purpose: str = ""  # "" | cancel | delete | fail (role killed, then «Ошибка»)
    failure: str = ""  # code of purpose "fail"
    load_req: str = ""
    start_req: str = ""
    cancel_req: str = ""
    job_sent: bool = False
    deadline: float | None = None
    retry_at: float = 0.0  # next model.load after an unexpected answer
    busy_since: float | None = None  # first ``not-prepared`` of this job
    received: int = 0
    done_ms: int = 0
    total_ms: int = 0
    percent: int = 0
    utterances: dict[int, Message] = field(default_factory=dict)
    transcript_revision: int = 0
    replacements: dict[int, Message] = field(default_factory=dict)
    restoring: bool = False
    restore_request: str | None = None
    restore_events: list[Message] = field(default_factory=list)
    released: Callable[[], None] | None = None

    @property
    def meeting_id(self) -> str:
        return self.meeting.id

    @property
    def generation(self) -> int:
        return self.meeting.meeting_generation


class MeetingJobs:
    """FIFO of meeting transcription jobs; the GUI drives ``tick()`` (50 ms)."""

    def __init__(
        self,
        store: MeetingStore,
        role_factory: RoleFactory,
        *,
        transcribe_immediately: Callable[[], bool] = lambda: True,
        audio_retention_days: Callable[[], int] = lambda: 0,
        meeting_retention_days: Callable[[], int] = lambda: 0,
        model_probe: Callable[[], ModelAdmission] = probe_gigaam_model,
        mem_available: Callable[[], int | None] = mem_available_mb,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        submit: Callable[[Callable[[], None]], None] | None = None,
        notify: Notify = lambda kind, meeting: None,
        echo_gate: bool | None = None,
        admit_allowed: Callable[[], bool] = lambda: True,
        diar_models: Callable[[], dict[str, str] | None] = lambda: None,
    ) -> None:
        self.store = store
        # Setting + administrator policy (I3): nothing is admitted while it is False.
        self.admit_allowed = admit_allowed
        # One seam for the next policy/catalog step. Resolve once per admission;
        # memory admission and the actual job must use the same choice.
        self.diar_models = diar_models
        self._role_factory = role_factory
        self.transcribe_immediately = transcribe_immediately
        self.audio_retention_days = audio_retention_days
        self.meeting_retention_days = meeting_retention_days
        self.model_probe = model_probe
        self.mem_available = mem_available
        self.clock = clock
        self.wall_clock = wall_clock
        # The GUI installs its export-owner barrier here; transcribe exits first.
        self.release_files: Callable[[str, Callable[[], None]], None] = lambda mid, done: done()
        self._retention_at: float | None = None
        self._retention_wall: datetime | None = None
        self._retention_scanning = False
        self._writer = MetadataWriter()
        self._submit = submit or self._writer.submit
        self._notify = notify
        self._echo_gate = echo_gate
        self._results: queue.SimpleQueue[Callable[[], None]] = queue.SimpleQueue()
        self._pending = 0
        self._queue: list[_Entry] = []
        self._order = 0
        self._admitting = ""
        self._active: _Active | None = None
        self._role: JobRole | None = None
        self._role_serial = 0
        self._loaded: tuple[int, Message] | None = None  # (process generation, spec)
        self._diar_unload_pending = False
        self._diar_unload_req = ""
        self._crashes: deque[float] = deque()
        self._exits: list[int] = []
        self.failures: dict[str, str] = {}
        self.meetings: dict[str, Meeting] = {}
        self.tombstones: set[str] = set()
        self.deletions: dict[str, str] = {}
        self._purge_retry_at: dict[str, float] = {}
        # Workers report cleanup outcomes; only GUI completions own retry timers.
        self._audio_cleanup_results: queue.SimpleQueue[tuple[str, bool]] = queue.SimpleQueue()
        self._audio_retry_at: dict[str, float] = {}
        self._audio_cleanup_io: set[str] = set()
        self._retention_deletions: set[str] = set()
        self._delete_events: dict[str, dict[tuple[str, str], Message]] = {}
        self.cancelled: set[str] = set()
        # meeting_id -> (failed cancel writes, retry time or None while a write is queued)
        self._cancel_writes: dict[str, tuple[int, float | None]] = {}
        # Redo metadata work, terminal cleanup and commit remain busy even when
        # no role is active. Only GUI completions mutate these registries.
        self._redo_deleted: set[str] = set()
        self._redo_generations: dict[str, int] = {}
        self._redo_preparing: set[str] = set()
        self._redo_commits: dict[str, _Active] = {}
        self._redo_aborts: dict[str, tuple[_Active, str]] = {}
        self._redo_retry: dict[str, float] = {}
        self._redo_io: set[str] = set()
        self._redo_recovery: dict[str, float] = {}
        self._redo_resumable: set[str] = set()
        self._recover_requested = False
        self.recovery_errors: dict[str, str] = {}
        self.redo_results: dict[str, str] = {}
        self.revision = 0
        self._closed = False

    # ---- public API -----------------------------------------------------

    @property
    def active_id(self) -> str:
        return self._active.meeting_id if self._active is not None else ""

    def names_busy(self, meeting_id: str) -> bool:
        """No edits while this meeting is admitted, queued or active."""
        return (
            self.active_id == meeting_id
            or meeting_id in self.queued_ids
            or self._admitting == meeting_id
            or meeting_id in self._redo_preparing
            or meeting_id in self._redo_commits
            or meeting_id in self._redo_aborts
            or meeting_id in self._redo_recovery
            or (meeting_id in self._cancel_writes and meeting_id in self._redo_generations)
        )

    @property
    def queued_ids(self) -> list[str]:
        return [entry.meeting_id for entry in self._queue]

    @property
    def busy(self) -> bool:
        """A job is running or waits in the queue (exit stays unconfirmed: T-478)."""
        return (
            self._active is not None
            or bool(self._queue)
            or bool(self._redo_preparing)
            or bool(self._redo_commits)
            or bool(self._redo_aborts)
            or bool(self._redo_recovery)
            or bool(self._cancel_writes.keys() & self._redo_generations.keys())
        )

    @property
    def percent(self) -> int | None:
        """Running job's progress for the tray hint, else None."""
        active = self._active
        if active is None or active.purpose or active.redo:
            return None
        return active.percent

    def job_snapshot(self, meeting_id: str) -> Message | None:
        """Progress and visible utterances of the running job (card A reads this)."""
        active = self._active
        if active is None or active.meeting_id != meeting_id or active.purpose:
            return None
        utterances = [
            {**item, "mark": item["text"] in {UNRECOGNIZED_TEXT, DAMAGED_TEXT}}
            for _, item in sorted(active.utterances.items())
            if item["text"]
        ]
        return {
            "meeting_id": meeting_id,
            "phase": active.phase,
            "redo": active.redo,
            "stage": active.stage,
            "percent": active.percent,
            "done_ms": active.done_ms,
            "total_ms": active.total_ms,
            "utterances": utterances,
            "transcript_revision": active.transcript_revision,
        }

    def redo_snapshot(self, meeting_id: str) -> Message:
        """5в-6 bridge: busy/status/stage and ephemeral terminal outcome.

        The bridge may pop redo_results[id] when the card closes. revision
        changes on every visible transition; no terminal hint is persisted.
        """
        active = self._active
        entry = next((e for e in self._queue if e.meeting_id == meeting_id and e.redo), None)
        status, stage, percent = "", "", 0
        if meeting_id in self._redo_preparing:
            status = "queued"
        if meeting_id in self._redo_recovery:
            status = "recovery_error" if meeting_id in self.recovery_errors else "recovering"
        if entry is not None:
            status = entry.status
        if active is not None and active.meeting_id == meeting_id and active.redo:
            status, stage, percent = active.phase, active.stage, active.percent
        if meeting_id in self._redo_commits:
            status, stage, percent = "committing", self._redo_commits[meeting_id].stage, 100
            if meeting_id in self.recovery_errors:
                status = "recovery_error"
        if meeting_id in self._redo_aborts or (
            meeting_id in self._cancel_writes and meeting_id in self._redo_generations
        ):
            status = "cancelling"
        return {
            "busy": bool(status),
            "status": status,
            "stage": stage,
            "percent": percent,
            "outcome": self.redo_results.get(meeting_id, ""),
            "recoveryError": self.recovery_errors.get(meeting_id, ""),
        }

    def row(self, meeting_id: str, meeting: Meeting | None = None) -> Message:
        """Presentation hints for a list row: percent, failure text, cancelled text.

        The failure code of this process wins; after a restart the durable
        ``transcription.failure`` of ``meeting`` explains the error.
        """
        active = self._active
        percent = (
            active.percent
            if active is not None and active.meeting_id == meeting_id and not active.purpose
            else 0
        )
        code = self.failures.get(meeting_id) or (
            meeting.transcription.failure if meeting is not None else None
        )
        return {
            "percent": percent,
            "failure": FAILED_TEXT.get(code or "", FAILED_DEFAULT_TEXT),
            "failureCode": code or "",
            "cancelledText": CANCELLED_TEXT,
        }

    def start_retention(self) -> None:
        """Start storage maintenance once, independently of the processing gate."""
        if self._retention_at is None:
            self.recover_deletions()

    def recover_deletions(self) -> None:
        """Compatibility entry point: one maintenance path owns deletion recovery."""
        self._retention_pass()

    def recover(self) -> None:
        """Resume jobs only; the caller admits processing after start_retention()."""
        self._recover_requested = True

        def scan() -> Callable[[], None]:
            try:
                meetings = self.store.list()
            except MeetingStoreError:
                meetings = []
            resume = [
                meeting
                for meeting in meetings
                if not meeting.cancelled
                and meeting.id not in self.tombstones
                and meeting.state != MeetingState.DELETING
                and (meeting.rediar is not None or self._ordinary_resumable(meeting))
            ]
            resume.sort(key=lambda row: (row.ended_at or row.started_at, row.id))

            def completed() -> None:
                if self._closed:
                    return
                for meeting in resume:
                    # A preceding retention scan may have published tombstones
                    # after this worker snapshot, before either GUI completion.
                    if meeting.id in self.tombstones or meeting.id in self.cancelled:
                        continue
                    if meeting.rediar is not None:
                        if meeting.id in self._redo_resumable:
                            self._redo_generations[meeting.id] = meeting.meeting_generation
                            self._add(meeting.id, resume=True, redo=True)
                            self.meetings[meeting.id] = meeting
                        elif not self.names_busy(meeting.id):
                            self._settle_redo_disk(meeting.id)
                    elif meeting.state == MeetingState.RECORDED:
                        self._enqueue(meeting.id, auto=False, resume=True)
                    else:
                        self._add(meeting.id, resume=True)
                        self.meetings[meeting.id] = meeting

            return completed

        self._run(scan)

    def _ordinary_resumable(self, meeting: Meeting) -> bool:
        """Worker-only eligibility shared by initial and repaired recovery."""
        return not meeting.cancelled and (
            meeting.state in RESUMABLE
            or (meeting.state == MeetingState.RECORDED and self._has_checkpoint(meeting.id))
        )

    def _recover_ordinary_after_redo(self, meeting_id: str) -> None:
        """A delayed descriptor repair must not lose the ordinary resume job."""

        def work() -> Callable[[], None]:
            current = self._redo_read(meeting_id)
            eligible = (
                current is not None and current.rediar is None and self._ordinary_resumable(current)
            )

            def completed() -> None:
                if (
                    not eligible
                    or current is None
                    or self._closed
                    or meeting_id in self.tombstones
                    or meeting_id in self.cancelled
                    or self.names_busy(meeting_id)
                ):
                    return
                if current.state == MeetingState.RECORDED:
                    self._enqueue(meeting_id, auto=False, resume=True)
                else:
                    self.meetings[meeting_id] = current
                    self._add(meeting_id, resume=True)

            return completed

        self._run(work)

    @staticmethod
    def _expired(stamp: str | None, days: int, now: datetime) -> bool:
        if stamp is None or days <= 0:
            return False
        try:
            start = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            return now >= start + timedelta(days=days)
        except (ValueError, TypeError, OverflowError):
            return False

    @staticmethod
    def _audio_succeeded(meeting: Meeting) -> bool:
        return audio_succeeded(meeting)

    def _deletion_owned(self, meeting_id: str) -> bool:
        return meeting_id in self.tombstones or self.deletions.get(meeting_id) in {
            "pending",
            "deleting",
        }

    def _retention_pass(self) -> None:
        if self._closed or self._retention_scanning:
            return
        days = int(self.meeting_retention_days())
        wall = self.wall_clock()
        deadline = self.clock() + RETENTION_INTERVAL_S
        self._retention_scanning = True
        self._retention_at = deadline
        self._retention_wall = wall
        # Snapshot GUI ownership before submitting the serial filesystem scan.
        owned = (
            {mid for mid in self.meetings if self.names_busy(mid)}
            | set(self.queued_ids)
            | self._redo_preparing
            | self._redo_commits.keys()
            | self._redo_aborts.keys()
            | self._redo_recovery.keys()
        )
        if self._admitting:
            owned.add(self._admitting)
        if self.active_id:
            owned.add(self.active_id)
        # Back off even if the scan itself fails (for example on an unreadable root).
        retrying = tuple(self._purge_retry_at)
        for meeting_id in retrying:
            self._purge_retry_at[meeting_id] = self.clock() + DELETE_PURGE_RETRY_LATER_S

        def scan() -> Callable[[], None]:
            now = self.wall_clock()
            recovered: list[str] = []
            retry: list[str] = []
            for meeting_id in retrying:
                if self._deletion_owned(meeting_id):
                    continue
                # purge may remove everything and then fail fsync(root). Probe
                # both names directly; a failed catalogue is not proof of absence.
                try:
                    with private_directory(self.store.root) as root:
                        for name in (meeting_id, f".deleting-{meeting_id}"):
                            try:
                                os.stat(name, dir_fd=root, follow_symlinks=False)
                            except FileNotFoundError:
                                continue
                            break
                        else:
                            recovered.append(meeting_id)
                except FileNotFoundError:
                    recovered.append(meeting_id)  # the entire storage root is gone
                except (OSError, ValueError):
                    pass  # uncertainty preserves the retry and its backoff
            try:
                pending = self.store.pending_deletions()
            except MeetingStoreError:
                pending = []
            for meeting_id in pending:
                if self._deletion_owned(meeting_id):
                    continue  # active role/export still owns the release barrier
                try:
                    self.store.purge_deleted(meeting_id)
                    recovered.append(meeting_id)
                except MeetingStoreError:
                    retry.append(meeting_id)
            try:
                meetings = self.store.list(include_deleting=True)
            except MeetingStoreError:
                meetings = []
            expired: list[str] = []
            updates: list[Meeting] = []
            redos: list[tuple[Meeting, RediarPublishResult]] = []
            for meeting in meetings:
                try:
                    if self._deletion_owned(meeting.id) or meeting.id in pending:
                        continue
                    # Capture checkpoints and controller metadata fail fast on
                    # our store gate/GUI lease. Never contend with live capture,
                    # including an inconsistent leftover redo descriptor.
                    if meeting.state in {MeetingState.RECORDING, MeetingState.PAUSED}:
                        continue
                    if self.store.capture_active(meeting.id):
                        continue  # capture still owns its final durable edges
                    if meeting.id not in owned:
                        # Classification precedes both retention and role admission.
                        if meeting.rediar is not None:
                            meeting, result = self._probe_redo(meeting)
                            redos.append((meeting, result))
                        elif meeting.state != MeetingState.DELETING:
                            try:
                                meeting = self.store.rediar_cleanup(meeting.id)
                            except MeetingStoreBusy:
                                redos.append((meeting, RediarPublishResult.BUSY))
                            except MeetingStoreError:
                                redos.append((meeting, RediarPublishResult.IO_ERROR))
                    if meeting.state == MeetingState.DELETING:
                        try:
                            self.store.begin_delete(meeting.id)
                            self.store.purge_deleted(meeting.id)
                            recovered.append(meeting.id)
                        except MeetingStoreError:
                            retry.append(meeting.id)
                        continue
                    if meeting.state not in {MeetingState.RECORDING, MeetingState.PAUSED} and (
                        self._expired(meeting.started_at, days, now)
                    ):
                        expired.append(meeting.id)
                        continue  # delete the meeting once, without a separate audio cleanup
                    if meeting.cleanup_pending and self._audio_succeeded(meeting):
                        updated = self._remove_audio(meeting.id)
                        meeting = updated or meeting
                        # Publish the confirmed pending marker even on refusal.
                        updates.append(meeting)
                    if (
                        self._audio_succeeded(meeting)
                        and meeting.audio.present
                        and not meeting.cleanup_pending
                        and self._expired(
                            meeting.transcription.finished_at, meeting.audio.retain_days, now
                        )
                    ):
                        marked = self.store.mark_audio_cleanup(meeting.id, now)
                        if marked is not None:
                            updated = self._remove_audio(meeting.id)
                            updates.append(updated or marked)
                except (MeetingStoreError, OSError, ValueError):
                    continue  # one private/unreadable meeting cannot stop the pass

            def completed() -> None:
                self._retention_scanning = False
                if self._closed:
                    return
                for meeting, result in redos:
                    if meeting.id not in self.tombstones:
                        self._accept_redo_recovery(meeting, result)
                for meeting_id in retry:
                    self._purge_retry_at[meeting_id] = self.clock() + DELETE_PURGE_RETRY_LATER_S
                retained_count = 0
                for meeting_id in recovered:
                    self.meetings.pop(meeting_id, None)
                    self._purge_retry_at.pop(meeting_id, None)
                    self.deletions[meeting_id] = "deleted"
                    self._forget_redo(meeting_id)
                    if meeting_id in self._retention_deletions:
                        self._retention_deletions.discard(meeting_id)
                        retained_count += 1
                    self._changed()
                if retained_count:
                    self._notify("retention", retained_count)
                for updated in updates:
                    if updated.id not in self.tombstones:
                        self.meetings[updated.id] = updated
                        self._changed()
                remaining = len(expired)
                removed = 0

                def finished(ok: bool) -> None:
                    nonlocal remaining, removed
                    remaining -= 1
                    removed += int(ok)
                    if remaining == 0 and removed and not self._closed:
                        self._notify("retention", removed)

                for meeting_id in expired:
                    self._retention_deletions.add(meeting_id)

                    def deleted(ok: bool, mid: str = meeting_id) -> None:
                        if ok or self.deletions.get(mid) == "error":
                            self._retention_deletions.discard(mid)
                        finished(ok)

                    self.delete_and_purge(meeting_id, finished=deleted)

            return completed

        def failed() -> None:
            self._retention_scanning = False

        self._run(scan, failed=failed)

    def on_recorded(self, meeting_id: str) -> bool:
        """Capture finalized a meeting: queue it if «Расшифровывать сразу» is on."""
        if self._closed or not self.transcribe_immediately():
            return False
        self._enqueue(meeting_id, auto=True)
        return True

    def transcribe(self, meeting_id: str) -> None:
        """«Расшифровать»/«Повторить»: a new job (new generation, clears cancelled)."""
        if self._closed or meeting_id in self.tombstones:
            return
        if self.names_busy(meeting_id):
            return
        self.cancelled.discard(meeting_id)
        self._cancel_writes.pop(meeting_id, None)  # a new job: the old cancel is moot
        self._enqueue(meeting_id, auto=False)

    def rediarize(self, meeting_id: str) -> None:
        """Explicit redo; bridge flushes accepted speaker writes before calling."""
        if (
            self._closed
            or meeting_id in self.tombstones
            or meeting_id in self._redo_deleted
            or self.names_busy(meeting_id)
        ):
            return
        self._redo_preparing.add(meeting_id)
        self.cancelled.discard(meeting_id)
        self.redo_results.pop(meeting_id, None)
        self._changed()

        def work() -> Callable[[], None]:
            updated = None
            if self.admit_allowed() and self.diar_models() and self.model_probe().status == "ok":
                current = self.store.read(meeting_id, "meeting.json")
                expected = (
                    current.rediar.generation - 1
                    if current.rediar is not None
                    else current.meeting_generation
                )
                # The fixed expected generation survives every ambiguous retry.
                for attempt in range(WRITE_ATTEMPTS):
                    try:
                        updated = self.store.rediar_enqueue(meeting_id, expected)
                        break
                    except MeetingStoreError:
                        if attempt + 1 == WRITE_ATTEMPTS:
                            raise

            def completed() -> None:
                self._redo_preparing.discard(meeting_id)
                if (
                    updated is not None
                    and meeting_id not in self.tombstones
                    and meeting_id not in self._redo_deleted
                ):
                    self.meetings[meeting_id] = updated
                    self._redo_generations[meeting_id] = updated.meeting_generation
                    if meeting_id in self.cancelled:
                        self._persist_cancel(meeting_id)
                    else:
                        self._add(meeting_id, resume=True, redo=True)
                self._changed()

            return completed

        def failed() -> None:
            if meeting_id in self._redo_deleted:
                return
            self._redo_preparing.discard(meeting_id)
            self.failures[meeting_id] = "io"
            self._changed()

        self._run(work, failed=failed)

    def keep_as_is(
        self, meeting_id: str, expected_generation: int, done: Callable[[bool], None] | None = None
    ) -> None:
        """5в-6 action, available independently of policy/model admission.

        The generation comes from the displayed snapshot; the backend rechecks
        every condition under the P4 gate. A successful duplicate does no write.
        """
        if self._closed or meeting_id in self.tombstones or self.names_busy(meeting_id):
            if done is not None:
                done(False)
            return
        stamp = self.wall_clock()
        retention = int(self.audio_retention_days())

        def work() -> Callable[[], None]:
            self.store.keep_as_is(meeting_id, expected_generation, retain_days=retention, now=stamp)
            current = self.store.read(meeting_id, "meeting.json")
            ok = (
                current.meeting_generation == expected_generation
                and current.rediar is None
                and current.processing_status == ProcessingStatus.SUCCEEDED
                and current.transcription.diar_status == "skipped"
            )
            if ok and current.cleanup_pending:
                current = self._remove_audio(meeting_id) or current

            def completed() -> None:
                if meeting_id not in self.tombstones:
                    self.meetings[meeting_id] = current
                    self._changed()
                if done is not None:
                    done(ok)

            return completed

        self._run(work, failed=(lambda: done(False)) if done is not None else None)

    def models_changed(self) -> None:
        """A model was installed/checked: needs_model jobs re-check now (T-481)."""
        if not self.admit_allowed():
            return
        for entry in self._queue:
            if entry.status == "needs_model":
                entry.retry_at = 0.0

    def cancel(self, meeting_id: str) -> None:
        """D12: durable cancelled=true first, then meeting.job.cancel (<= 1 s, else kill).

        Without a confirmed write nothing is sent: the job keeps running while the
        write is retried, and a cancel that cannot be made durable ends in «Ошибка».
        """
        if self._closed or meeting_id in self.tombstones:
            return
        if meeting_id in self._redo_commits or meeting_id in self._redo_aborts:
            return  # done was delivered first: only commit recovery is legal
        if meeting_id in self._redo_generations and not self.names_busy(meeting_id):
            return
        self.cancelled.add(meeting_id)
        if meeting_id in self._redo_preparing or meeting_id in self._redo_recovery:
            # Classification owns the disk until it knows which side of K3
            # survived. WORK_PENDING consumes this intent before queue admission;
            # PUBLISHED must only roll forward, never discard the public result.
            self._changed()
            return
        entry = self._take(meeting_id)
        active = self._active
        if active is not None and active.meeting_id == meeting_id:
            if active.purpose == "fail":
                # The role is already being killed: record the cancel, no «Ошибка».
                active.purpose = "cancel"
                self._changed()
                self._persist_cancel(meeting_id)
                return
            if active.purpose:
                return
            active.purpose = "cancel"
            active.phase = "cancelling"
            self._changed()
            self._persist_cancel(meeting_id)
            return
        if entry is not None or meeting_id == self._admitting:
            self._persist_cancel(meeting_id)

    def rename(self, meeting_id: str, title: str, done: Callable[[bool], None]) -> None:
        """D3: use the same serialized RMW writer as transcription lifecycle edges.

        Only the title changes; completion is drained by tick() on the GUI thread.
        Capture owns a separate metadata copy, so recording/paused cannot be edited.
        """
        if self._closed or meeting_id in self.tombstones:
            done(False)
            return

        def mutate(meeting: Meeting) -> bool:
            if meeting.state in {MeetingState.RECORDING, MeetingState.PAUSED}:
                return False
            meeting.title = normalize_text(title, 120, minimum=1)
            return bool(title.strip())

        def work() -> Callable[[], None]:
            updated = self._update(meeting_id, mutate, require_change=True)

            def completed() -> None:
                ok = updated is not None and meeting_id not in self.tombstones
                if updated is not None and ok:
                    self.meetings[meeting_id] = updated
                    self._changed()
                done(ok)

            return completed

        self._run(work, failed=lambda: done(False))

    def delete_and_purge(
        self, meeting_id: str, *, finished: Callable[[bool], None] | None = None
    ) -> None:
        """Shared GUI/retention path: tombstone, transcribe exit, export release, purge."""
        self.delete(
            meeting_id,
            lambda: self.release_files(
                meeting_id, lambda: self.purge_deleted(meeting_id, finished=finished)
            ),
            failed=(lambda: finished(False)) if finished is not None else None,
        )

    def delete(
        self,
        meeting_id: str,
        released: Callable[[], None],
        *,
        failed: Callable[[], None] | None = None,
    ) -> None:
        """Tombstone -> cancel the job -> confirmed exit of the role -> ``released()``.

        M5 owns the rename/rmtree performed by ``released``. A SIGKILL without a
        confirmed exit (poll) never releases the files.
        """
        if self._closed or meeting_id in self.tombstones:
            if failed is not None:
                failed()
            return
        self.tombstones.add(meeting_id)
        self.deletions[meeting_id] = "pending"
        entry = self._take(meeting_id)

        def tombstone_failed() -> None:
            self.tombstones.discard(meeting_id)
            self.deletions[meeting_id] = "error"
            if entry is not None:
                self._queue.append(entry)
                self._queue.sort(key=lambda item: item.order)
            self._changed()
            for event in self._delete_events.pop(meeting_id, {}).values():
                self._event(event)
            if failed is not None:
                failed()

        def tombstone() -> Callable[[], None]:
            def mark(meeting: Meeting) -> bool:
                meeting.state = MeetingState.DELETING
                meeting.meeting_generation += 1
                return True

            ok = self._update(meeting_id, mark)

            def completed() -> None:
                if ok is None:
                    tombstone_failed()
                    return
                self.deletions[meeting_id] = "deleting"
                self._forget_redo(meeting_id)
                self._delete_events.pop(meeting_id, None)
                self._cancel_writes.pop(meeting_id, None)
                self.meetings.pop(meeting_id, None)
                active = self._active
                if active is not None and active.meeting_id == meeting_id:
                    active.released = released
                    if active.purpose == "delete":
                        return
                    if active.phase == "exiting":  # cancel/fail already killed the role
                        active.purpose = "delete"
                        return
                    active.purpose = "delete"
                    active.phase = "cancelling"
                    self._send_cancel(meeting_id)
                else:
                    released()
                self._changed()

            return completed

        # The tombstone was not written: nothing is deleted, the meeting stays usable.
        self._run(tombstone, failed=tombstone_failed)

    def purge_deleted(
        self, meeting_id: str, *, finished: Callable[[bool], None] | None = None
    ) -> None:
        """GUI release callback schedules filesystem work after every owner has exited.

        The bridge first releases export; future DIAR/AI owners must also acknowledge
        release before this callback. No filesystem work runs in the GUI thread.
        """

        def work() -> Callable[[], None]:
            meeting = self.store.read(meeting_id, "meeting.json")
            for attempt in range(DELETE_PURGE_ATTEMPTS):
                if meeting_id not in self.tombstones or self._closed:
                    return lambda: None
                try:
                    # begin_delete is idempotent: a previous attempt may have renamed.
                    self.store.begin_delete(meeting_id)
                    self.store.purge_deleted(meeting_id)
                    break
                except MeetingStoreError:
                    if attempt + 1 == DELETE_PURGE_ATTEMPTS:
                        raise
                    time.sleep(DELETE_PURGE_RETRY_S)

            def completed() -> None:
                self.meetings.pop(meeting_id, None)
                self.tombstones.discard(meeting_id)
                self.deletions[meeting_id] = "deleted"
                self._forget_redo(meeting_id)
                self._purge_retry_at.pop(meeting_id, None)
                self._notify("deleted", meeting)
                self._changed()
                if finished is not None:
                    finished(True)

            return completed

        def failed() -> None:
            self.deletions[meeting_id] = "purge_error"
            self.tombstones.discard(meeting_id)  # all owners have already released
            self._purge_retry_at[meeting_id] = self.clock() + DELETE_PURGE_RETRY_LATER_S
            self._changed()  # on disk DELETING/.deleting-* remains recoverable
            if finished is not None:
                finished(False)

        self._run(work, failed=failed)

    def tick(self) -> None:
        self._drain()
        if self._closed:
            return
        if any(self.clock() >= at for at in self._purge_retry_at.values()) or (
            self._retention_at is not None
            and (
                self.clock() >= self._retention_at
                or (
                    self._retention_wall is not None
                    and self.wall_clock() - self._retention_wall
                    >= timedelta(seconds=RETENTION_INTERVAL_S)
                )
            )
        ):
            self._retention_pass()
        self._handle_exits()
        if self._diar_unload_pending:
            self._unload_diar()
        self._retry_cancel_writes()
        self._retry_redo()
        self._retry_audio_cleanup()
        active = self._active
        if active is not None:
            self._advance(active)
        if self._active is None:
            self._schedule()
        self._drain()

    def suspend(self) -> bool:
        """Recording is banned/disabled: stop the running job like a program exit.

        No job.cancel: the role is stopped, its checkpoint stays durable and the
        meeting stays «transcribing» on disk. The job waits at the head of the queue
        and resumes (``job.start{resume: true}``) once admission is allowed again.
        Cancel/delete/fail in progress are left to finish. True when stopped.
        """
        active = self._active
        if self._closed or active is None or active.purpose:
            return False
        role, self._role = self._role, None
        self._role_serial += 1  # late events/exits of the stopped process are ignored
        self._loaded = None
        self._active = None
        if role is not None:
            try:
                role.stop()
            except Exception:  # noqa: BLE001 — the job resumes from its checkpoint
                pass
        if active.meeting_id not in self.tombstones:
            first = min((entry.order for entry in self._queue), default=self._order + 1)
            self._queue.append(_Entry(active.meeting_id, first - 1, resume=True, redo=active.redo))
        self._changed()
        return True

    def shutdown(self, *, timeout: float = 0.0) -> bool:
        """Program exit: no job.cancel; SIGTERM keeps the checkpoint for resume (T-478)."""
        if self._closed:
            return True
        self._closed = True
        role, self._role = self._role, None
        self._role_serial += 1
        if role is not None:
            try:
                role.stop()
            except Exception:  # noqa: BLE001 — exit continues; the next start resumes
                pass
        return self._writer.close(timeout=timeout)

    # ---- queue ------------------------------------------------------------

    def _changed(self) -> None:
        self.revision += 1

    def _add(self, meeting_id: str, *, resume: bool, redo: bool = False) -> None:
        if meeting_id in self.tombstones or meeting_id in self.queued_ids:
            return
        self._order += 1
        self._queue.append(_Entry(meeting_id, self._order, resume=resume, redo=redo))
        self._changed()

    def _take(self, meeting_id: str) -> _Entry | None:
        for entry in self._queue:
            if entry.meeting_id == meeting_id:
                self._queue.remove(entry)
                self._changed()
                return entry
        return None

    def _enqueue(self, meeting_id: str, *, auto: bool, resume: bool = False) -> None:
        def mutate(meeting: Meeting) -> bool:
            if meeting.rediar is not None:
                return False
            if auto and (meeting.state != MeetingState.RECORDED or meeting.cancelled):
                return False
            if not auto and meeting.state not in TRANSCRIBABLE and not resume:
                return False
            if not resume:
                meeting.meeting_generation += 1
                meeting.transcript_generation = None
                meeting.cancelled = False
                meeting.processing_status = ProcessingStatus.PENDING
                meeting.transcription.failure = None
            self._to(meeting, MeetingState.TRANSCRIBING)
            return True

        def work() -> Callable[[], None]:
            updated = self._update(meeting_id, mutate, require_change=True)

            def completed() -> None:
                if updated is None or meeting_id in self.tombstones:
                    return
                if meeting_id in self.cancelled and resume:
                    return
                self.meetings[meeting_id] = updated
                self.failures.pop(meeting_id, None)
                self._redo_generations.pop(meeting_id, None)
                self.redo_results.pop(meeting_id, None)
                self._add(meeting_id, resume=resume)

            return completed

        self._run(work)

    def _schedule(self) -> None:
        if (
            self._diar_unload_pending
            or self._retention_scanning
            or self._admitting
            or not self._queue
            or not self.admit_allowed()
        ):
            return
        now = self.clock()
        for entry in sorted(self._queue, key=lambda item: item.order):
            if entry.status == "needs_model" and entry.retry_at > now:
                continue  # missing model blocks nobody: the next one finds the same
            if entry.retry_at > now:
                return  # waiting_memory keeps FIFO: memory is short for everyone
            self._admit(entry)
            return

    def _admit(self, entry: _Entry) -> None:
        if entry.redo:
            self._admit_redo(entry)
            return
        meeting_id = entry.meeting_id
        self._admitting = meeting_id

        def work() -> Callable[[], None]:
            admission = self.model_probe()
            diar_models = self.diar_models()
            available = self.mem_available() if admission.status == "ok" else None
            if admission.status == "missing":
                outcome = "needs_model"
                target = MeetingState.NEEDS_MODEL
            elif admission.status != "ok":
                outcome, target = "failed", MeetingState.FAILED
            elif available is not None and available < (
                admission.min_ram_mb + MEMORY_MARGIN_MB + (DIAR_MEMORY_MB if diar_models else 0)
            ):
                outcome, target = "waiting_memory", MeetingState.WAITING_MEMORY
            else:
                outcome, target = "start", MeetingState.TRANSCRIBING

            def mutate(meeting: Meeting) -> bool:
                if meeting.cancelled or meeting.state not in RESUMABLE:
                    return False
                self._to(meeting, target)
                if outcome == "start":
                    meeting.processing_status = ProcessingStatus.RUNNING
                    meeting.transcription.diarization = diar_models is not None
                    meeting.transcription.diar_status = "pending" if diar_models else "skipped"
                elif outcome == "failed":
                    meeting.processing_status = ProcessingStatus.FAILED
                    meeting.transcription.failure = "model"
                return True

            updated = self._update(meeting_id, mutate, require_change=True)
            if updated is not None and outcome == "start" and diar_models is not None:
                # Only the GUI role owns speakers.json. Reset before the role can
                # start publishing a new set of labels (also after crash/cancel).
                with self.store.writer(meeting_id, Writer.GUI) as writer:
                    writer.reset_speakers(updated.meeting_generation)

            def completed() -> None:
                self._admitting = ""
                current = next((e for e in self._queue if e.meeting_id == meeting_id), None)
                if current is None or updated is None or meeting_id in self.tombstones:
                    if current is not None:
                        self._take(meeting_id)
                    return
                self.meetings[meeting_id] = updated
                now = self.clock()
                if outcome == "needs_model":
                    current.status, current.retry_at = "needs_model", now + MODEL_RETRY_S
                    self._notify_once(current, "needs_model", updated)
                elif outcome == "waiting_memory":
                    current.status, current.retry_at = "waiting_memory", now + MEMORY_RETRY_S
                    self._notify_once(current, "waiting_memory", updated)
                elif outcome == "failed":
                    self._take(meeting_id)
                    self.failures[meeting_id] = "model"
                    self._notify("failed_model", updated)
                elif not self.admit_allowed():
                    # Banned while the probe ran: stays queued («transcribing» on disk).
                    current.retry_at = 0.0
                else:
                    self._take(meeting_id)
                    self._active = _Active(
                        updated, admission, resume=current.resume, diar_models=diar_models
                    )
                    self._advance(self._active)
                self._changed()

            return completed

        def failed() -> None:
            # The probe/write raised or the writer refused the work: the queue must
            # not wait for a completion that never comes (``_admitting`` cleared).
            self._admitting = ""
            current = next((e for e in self._queue if e.meeting_id == meeting_id), None)
            if current is not None:
                current.retry_at = self.clock() + ADMIT_RETRY_S
            self._changed()

        self._run(work, failed=failed)

    def _notify_once(self, entry: _Entry, kind: str, meeting: Meeting) -> None:
        if not entry.redo and kind not in entry.notified:
            entry.notified.add(kind)
            self._notify(kind, meeting)

    # ---- role -------------------------------------------------------------

    def _ensure_role(self) -> JobRole | None:
        role = self._role
        if role is not None and getattr(role, "state", "running") == "stopped":
            if not role.exited:
                return None
            self._role = role = None
            self._role_serial += 1
        if role is None:
            self._role_serial += 1
            serial = self._role_serial

            def event(message: Message) -> None:
                if serial == self._role_serial and not self._closed:
                    self._event(message)

            def exited(code: int) -> None:
                if serial == self._role_serial and not self._closed:
                    self._exits.append(code)

            role = self._role_factory(event, exited)
            self._role = role
            self._loaded = None
            try:
                role.start()
            except (OSError, RuntimeError):
                return None
        return role

    def _send(self, kind: str, *, req: str = "", **fields: Any) -> str:
        """``req`` given by the caller is stored before the send: the supervisor and the
        role may answer synchronously inside ``send`` (limit, req-conflict, cache)."""
        role = self._role
        if role is None:
            return ""
        request = req or uuid4().hex
        try:
            role.send({"type": kind, "req": request, "request_type": kind, **fields})
        except (RuntimeError, ValueError):
            return ""
        return request

    def _identity(self, active: _Active) -> Message:
        return {"meeting_id": active.meeting_id, "meeting_generation": active.generation}

    def _advance(self, active: _Active) -> None:
        if self.deletions.get(active.meeting_id) == "pending":
            return  # pin the owner until the durable tombstone has completed
        if active.phase in {"launching", "crashed"} and not active.purpose:
            if active.retry_at > self.clock():
                return
            role = self._ensure_role()
            if role is None or not role.ready:
                return
            spec = active.admission.load or {}
            if self._loaded == (role.generation, spec):
                self._start_job(active)
                return
            phase, request = active.phase, uuid4().hex
            active.load_req, active.phase = request, "loading"
            sent = self._send("model.load", req=request, **spec)
            if not sent and self._active is active and active.load_req == request:
                active.load_req, active.phase = "", phase  # not sent: retried next tick
            return
        if active.phase == "cancelling" and active.deadline is not None:
            if self.clock() >= active.deadline:
                self._kill(active)
            return
        if active.phase == "exiting":
            role = self._role
            if role is None or role.exited:
                self._finish_stop(active)

    def _start_job(self, active: _Active) -> None:
        meeting = active.meeting
        echo = self._echo_gate
        if echo is None:
            from astra_voice.meetings.echo_gate import ECHO_GATE_DEFAULT

            echo = ECHO_GATE_DEFAULT
        tracks = ["mic"] if meeting.variant == "mic_only" else ["mic", "far"]
        if not active.resume:
            active.utterances.clear()
        active.received = 0
        request = uuid4().hex
        active.start_req, active.job_sent, active.phase = request, True, "starting"
        sent = self._send(
            "meeting.job.start",
            req=request,
            **self._identity(active),
            dir=str(self.store.root / meeting.id),
            tracks=tracks,
            variant=meeting.variant,
            diarization=active.diar_models is not None,
            **({"diar_models": dict(active.diar_models)} if active.diar_models is not None else {}),
            self_track="mic",
            resume=active.resume,
            **({"operation": "rediarize"} if active.redo else {}),
            echo_gate=bool(echo and "far" in tracks),
            limits=dict(LIMITS),
        )
        if self._active is not active or active.start_req != request or active.purpose:
            return  # a synchronous answer inside send() already decided the job
        if not sent:
            active.start_req, active.job_sent = "", False
            self._retry_load(active, "send")
            return
        if not active.resume:
            self._send("meeting.job.credit", **self._identity(active), n=INITIAL_CREDITS)

    def _send_cancel(self, meeting_id: str) -> None:
        active = self._active
        if (
            active is None
            or active.meeting_id != meeting_id
            or active.phase == "exiting"
            or active.cancel_req
        ):
            return
        if not active.job_sent or self._role is None:
            # No job in the role: nothing owns the meeting files.
            self._finish_stop(active)
            return
        active.cancel_req = self._send("meeting.job.cancel", **self._identity(active))
        if not active.cancel_req:
            self._kill(active)
            return
        active.phase = "cancelling"
        active.deadline = self.clock() + CANCEL_TIMEOUT_S

    def _kill(self, active: _Active) -> None:
        active.phase = "exiting"
        active.deadline = None
        role = self._role
        if role is not None:
            role.kill()
        self._advance(active)

    def _finish_stop(self, active: _Active) -> None:
        """Cancel/delete completed: late events are retired by a newer generation."""
        if self._active is not active:
            return
        if active.purpose == "fail":
            self._fail(active, active.failure)  # the role is gone: the next job is free
            return
        role = self._role
        if role is not None and getattr(role, "state", "running") != "stopped":
            try:
                role.set_meeting_generation(active.meeting_id, active.generation + 1)
            except (RuntimeError, ValueError):
                pass
        self._active = None
        if active.diar_models is not None:
            self._unload_diar()
        if active.redo and active.purpose == "cancel":
            self._redo_abort(active, "cancelled")
        if active.released is not None:
            active.released()
        self._changed()

    def _handle_exits(self) -> None:
        if self._active is not None and self.deletions.get(self.active_id) == "pending":
            return
        exits, self._exits = self._exits, []
        if not exits:
            return
        self._loaded = None  # a new process loads the model again
        self._diar_unload_pending, self._diar_unload_req = False, ""
        active = self._active
        if active is None:
            return
        if active.purpose:
            # A confirmed exit ends cancel/delete, also on the supervisor's own kill.
            self._finish_stop(active)
            return
        if self._count_crash():
            self._fail(active, "io")
            return
        active.phase = "crashed"
        active.resume = True
        active.job_sent = False
        self._changed()

    def _count_crash(self) -> bool:
        """One more crash in the window; True when the job has to fail (T-484)."""
        now = self.clock()
        while self._crashes and now - self._crashes[0] >= CRASH_WINDOW_S:
            self._crashes.popleft()
        self._crashes.append(now)
        return len(self._crashes) > MAX_CRASHES

    def _retry_load(self, active: _Active, code: str) -> None:
        """model.load answered with an unexpected code (timeout, limit, req-conflict,
        not-prepared) or meeting.job.start was not sent: crash path, never a stuck
        ``loading`` (I1). ``not-prepared`` only waits while the role finishes a
        previous load/unload; afterwards it counts like any other crash."""
        active.load_req = ""
        now = self.clock()
        if code == "not-prepared":
            if active.busy_since is None:
                active.busy_since = now
            waiting = now - active.busy_since < NOT_PREPARED_BUDGET_S
        else:
            waiting = False
        if not waiting and self._count_crash():
            self._fail(active, "io")
            return
        active.phase, active.retry_at = "crashed", now + LOAD_RETRY_S
        self._changed()

    def _fail_after_exit(self, active: _Active, code: str) -> None:
        """meeting.job.start failed (timeout before accepted, limit, ...): the role may
        still run the job and write the transcript. Kill it; «Ошибка» is published only
        after a confirmed exit, so the next job gets a free role (I1)."""
        active.purpose, active.failure = "fail", code
        self._changed()
        self._kill(active)

    # ---- events -----------------------------------------------------------

    def _event(self, event: Message) -> None:
        role = self._role
        if role is None or event.get("generation") != role.generation:
            return
        kind = event.get("type")
        if event.get("req") == self._diar_unload_req and self._diar_unload_req:
            if kind == "model.unloaded":
                self._loaded = None  # only the correlated unload ACK proves this
                self._diar_unload_pending, self._diar_unload_req = False, ""
            elif kind == "error":
                self._diar_unload_req = ""  # transient busy/send failure: retry from tick
            return
        active = self._active
        if active is not None and self.deletions.get(active.meeting_id) == "pending":
            # Preserve every role event for replay if the durable write fails.
            # Outstanding credits bound utterances; progress keeps only its latest value.
            key = (
                str(kind),
                ""
                if kind == "meeting.job.progress"
                else str(event.get("req", event.get("seq", ""))),
            )
            self._delete_events.setdefault(active.meeting_id, {})[key] = event
            return
        if kind == "error" and "req" not in event:
            if event.get("code") in {"restart-limit", "worker-start"} and active is not None:
                if active.purpose == "delete" or (active.redo and active.purpose):
                    self._kill(active)  # even a supervisor error must await confirmed poll
                elif active.purpose:
                    self._finish_stop(active)
                elif active.redo:
                    self._fail_after_exit(active, "io")
                else:
                    self._fail(active, "io")
            return
        if active is None:
            return
        req = event.get("req")
        if req is not None and req == active.cancel_req:
            if kind == "meeting.job.cancelled" and active.phase == "cancelling":
                if active.purpose == "delete" or active.redo:
                    self._kill(active)  # files are released only by a confirmed exit
                else:
                    self._finish_stop(active)
            return
        if active.purpose:
            return  # cancelled/deleted: late progress, text and results are dropped
        if req is not None and req == active.load_req:
            if kind == "model.loaded":
                self._loaded = (role.generation, active.admission.load or {})
                self._start_job(active)
            elif kind == "error":
                code = str(event.get("code", ""))
                if code == "memory":
                    self._wait_memory(active)
                elif code == "model":
                    self._fail(active, "model")
                else:
                    self._retry_load(active, code)
            return
        if req is not None and req == active.start_req:
            if kind == "meeting.job.accepted":
                if (active.resume or active.redo) and active.restore_request == active.start_req:
                    return  # replayed accepted must not grant readiness credits twice
                active.phase = "running"
                active.transcript_revision = int(event.get("transcript_revision", 0))
                if active.resume or active.redo:
                    self._restore_live(active)
                self._changed()
            elif kind == "error":
                self._fail_after_exit(active, "model" if event.get("code") == "model" else "io")
            return
        if req is not None or (
            event.get("meeting_id") != active.meeting_id
            or event.get("meeting_generation") != active.generation
        ):
            return  # MJ-14: other meetings and stale generations never publish
        if active.restoring and kind in {
            "meeting.job.utterance",
            "meeting.job.labels",
            "meeting.job.replace",
            "meeting.job.done",
        }:
            active.restore_events.append(event)
            return
        if kind == "meeting.job.progress":
            total = int(event.get("total_ms", 0))
            done = int(event.get("done_ms", 0))
            percent = min(100, done * 100 // total) if total > 0 else 0
            active.done_ms, active.total_ms = done, total
            stage = str(event.get("stage", active.stage))
            if stage != active.stage:
                active.stage = stage
                self._changed()
            if active.phase == "starting":
                active.phase = "running"
            if percent != active.percent:
                active.percent = percent
                self._changed()
        elif kind == "meeting.job.utterance":
            seq = int(event["seq"])
            active.utterances[seq] = {
                key: event[key]
                for key in (
                    "seq",
                    "id",
                    "track",
                    "start_ms",
                    "end_ms",
                    "text",
                    "speaker",
                    "multiple_speakers",
                    "echo",
                )
                if key in event
            }
            active.received += 1
            if active.received % CREDIT_STEP == 0:
                self._send("meeting.job.credit", **self._identity(active), n=CREDIT_STEP)
            self._changed()
        elif kind == "meeting.job.labels":
            labels = dict(event["items"])
            for item in active.utterances.values():
                if item["id"] in labels:
                    item["speaker"] = labels[item["id"]]
            active.transcript_revision = max(
                active.transcript_revision, int(event.get("transcript_revision", 0))
            )
            self._changed()
        elif kind == "meeting.job.replace":
            self._replace(active, event)
        elif kind == "meeting.job.waiting":
            # Before accepted, memory ends admission as in M4. During the
            # selected diar pipeline the executor owns a bounded retry and
            # retains its lease; do not launch a competing replacement job.
            if active.diar_models is None or active.phase != "running":
                self._wait_memory(active)
        elif kind == "meeting.job.failed":
            code = str(event.get("code", "io"))
            if code == "memory":
                self._wait_memory(active)
            elif active.redo:
                self._fail_after_exit(active, code)
            else:
                self._fail(active, code)
        elif kind == "meeting.job.done":
            self._done(active, event)

    @staticmethod
    def _replacement_payload(event: Message) -> Message:
        return deepcopy({k: event[k] for k in ("seq", "replaces_id", "expected_revision", "items")})

    def _restore_live(self, active: _Active) -> None:
        """Prepare a validated snapshot off GUI, then grant the readiness credits."""
        from astra_voice.meetings.model import Transcript

        active.restoring = True
        active.restore_request = active.start_req
        active.restore_events = []
        request, generation = active.start_req, active.generation
        accepted_revision = active.transcript_revision

        def current() -> bool:
            return (
                not self._closed
                and self._active is active
                and not active.purpose
                and active.start_req == request
                and active.phase == "running"
            )

        def work() -> Callable[[], None]:
            try:
                filename = "rediar-transcript.json" if active.redo else "transcript.json"
                transcript = self.store.read(active.meeting_id, filename)
            except MeetingStoreError:
                if active.redo or os.path.lexists(
                    self.store.root / active.meeting_id / "transcript.json"
                ):
                    raise
                transcript = None  # a resumed ASR may not have written any text yet
            rows: dict[int, Message] = {}
            receipts: dict[int, Message] = {}
            revision = accepted_revision
            if isinstance(transcript, Transcript) and transcript.meeting_generation == generation:
                # store.read already validated the entire document. Per-row
                # to_dict() would repeat expensive validation 9000 times.
                rows = {
                    u.seq: {
                        "seq": u.seq,
                        "id": u.id,
                        "track": u.track,
                        "start_ms": u.start_ms,
                        "end_ms": u.end_ms,
                        "text": u.text,
                        "speaker": u.speaker,
                        "multiple_speakers": u.multiple_speakers,
                        "echo": u.echo,
                    }
                    for u in transcript.utterances
                }
                revision = transcript.transcript_revision
                pending = transcript.reconcile.pending
                if pending is not None:
                    receipts[pending.seq] = {
                        "seq": pending.seq,
                        "replaces_id": pending.replaces_id,
                        "expected_revision": pending.expected_revision,
                        "items": [
                            {
                                "id": u.id,
                                "track": u.track,
                                "start_ms": u.start_ms,
                                "end_ms": u.end_ms,
                                "text": u.text,
                                "speaker": u.speaker,
                                "provisional": True,
                                "multiple_speakers": u.multiple_speakers,
                            }
                            for u in pending.items
                        ],
                    }

            def completed() -> None:
                if not current():
                    return
                active.utterances = rows
                active.transcript_revision = revision
                active.replacements = receipts
                active.restoring = False
                events, active.restore_events = active.restore_events, []
                for event in events:
                    self._event(event)
                # A buffered done/cancel/delete may have retired this job.
                if current():
                    self._send("meeting.job.credit", **self._identity(active), n=INITIAL_CREDITS)
                    self._changed()

            return completed

        def failed() -> None:
            if current():
                active.restoring = False
                active.restore_events = []
                self._fail_after_exit(active, "io")

        self._run(work, failed=failed)

    def _replace(self, active: _Active, event: Message) -> bool:
        """Atomic in-memory edit. No GUI transcript writes, including on retry."""
        payload = self._replacement_payload(event)
        seq = int(event["seq"])
        previous = active.replacements.get(seq)
        if previous is not None:
            if previous != payload:
                return False
        else:
            if active.transcript_revision != event["expected_revision"]:
                return False
            rows = [item for _, item in sorted(active.utterances.items())]
            positions = [i for i, item in enumerate(rows) if item["id"] == event["replaces_id"]]
            if len(positions) != 1 or not event["items"]:
                return False
            index = positions[0]
            inserted = [
                {k: v for k, v in item.items() if k != "provisional"} for item in event["items"]
            ]
            rows[index : index + 1] = inserted
            rows.sort(
                key=lambda item: utterance_order(item["start_ms"], item["track"], item["end_ms"])
            )
            if len({item["id"] for item in rows}) != len(rows):
                return False
            updated = {i: {**item, "seq": i} for i, item in enumerate(rows)}
            active.utterances = updated
            active.transcript_revision += 1
            active.replacements[seq] = payload
            self._changed()
        self._send(
            "meeting.job.applied",
            **self._identity(active),
            seq=seq,
            revision=int(event["expected_revision"]) + 1,
        )
        return True

    def _unload(self) -> None:
        if self._diar_unload_pending:
            return  # a tracked terminal unload already owns this transition
        if self._loaded is not None:
            self._loaded = None
            self._send("model.unload")

    def _unload_diar(self) -> None:
        """Every diar terminal path unloads, including a resumed completed phase.

        Keep resident accounting and block admission until the correlated ACK.
        A receipt/busy/error does not prove unloading; the GUI tick retries.
        """
        role = self._role
        if role is None or role.exited:
            self._loaded = None  # confirmed process exit also releases the model
            self._diar_unload_pending, self._diar_unload_req = False, ""
            return
        self._diar_unload_pending = True
        if self._diar_unload_req:
            return
        self._diar_unload_req = request = uuid4().hex
        if not self._send("model.unload", req=request):
            self._diar_unload_req = ""

    def _wait_memory(self, active: _Active) -> None:
        """Role ended the job for memory: unload the model, retry in 60 s (T-483)."""
        self._active = None
        if active.redo:
            self._unload_diar()
        else:
            self._unload()
        meeting_id = active.meeting_id
        self._order += 1
        entry = _Entry(
            meeting_id,
            order=-self._order,  # it keeps the head of the FIFO
            status="waiting_memory",
            retry_at=self.clock() + MEMORY_RETRY_S,
            resume=True,
            redo=active.redo,
        )
        self._queue.append(entry)
        if active.redo:
            self._changed()
            return

        def mutate(meeting: Meeting) -> bool:
            if meeting.cancelled:
                return False
            self._to(meeting, MeetingState.WAITING_MEMORY)
            return True

        def work() -> Callable[[], None]:
            updated = self._update(meeting_id, mutate, require_change=True)

            def completed() -> None:
                if updated is not None:
                    self.meetings[meeting_id] = updated
                    self._notify_once(entry, "waiting_memory", updated)
                self._changed()

            return completed

        self._run(work)
        self._changed()

    def _fail(self, active: _Active, code: str) -> None:
        """Audio stays; ordinary failure is durable, redo failure is ephemeral."""
        if active.redo:
            self._active = None
            self._unload_diar()
            self._redo_abort(active, self._redo_failure(active.meeting))
            return
        self._active = None
        if active.diar_models is not None:
            self._unload_diar()
        if code == "model" and not self._diar_unload_pending:
            self._loaded = None
        meeting_id = active.meeting_id
        self.failures[meeting_id] = code

        def mutate(meeting: Meeting) -> bool:
            if meeting.cancelled or meeting.meeting_generation != active.generation:
                return False
            self._to(meeting, MeetingState.FAILED)
            meeting.processing_status = ProcessingStatus.FAILED
            meeting.transcription.failure = code if code in FAILURE_CODES else "io"
            return True

        def work() -> Callable[[], None]:
            updated = self._update(meeting_id, mutate, require_change=True)

            def completed() -> None:
                if updated is not None:
                    self.meetings[meeting_id] = updated
                    self._notify("failed_model" if code == "model" else "failed", updated)
                self._changed()

            return completed

        self._run(work)
        self._changed()

    def _done(self, active: _Active, event: Message) -> None:
        if active.redo:
            if event.get("diar_status") != "done":
                self._fail_after_exit(active, "io")
                return
            self._active = None
            active.phase = "committing"
            # Keep timing/metrics from this logical done, never from a retry.
            active.meeting.transcription.wall_ms = int(event.get("wall_ms", 0))
            active.meeting.transcription.peak_rss_kb = int(event.get("peak_rss_kb", 0))
            active.meeting.transcription.finished_at = self.wall_clock().isoformat()
            self._redo_commits[active.meeting_id] = active
            self._unload_diar()
            self._settle_redo(active.meeting_id)
            self._changed()
            return
        self._active = None
        meeting_id = active.meeting_id
        retention = int(self.audio_retention_days())
        load = active.admission.load or {}
        diar_failed = event.get("diar_status") == "failed" or bool(event.get("reconcile_failed"))
        if active.diar_models is not None:
            # A resumed completed diar skips its usual ASR unload. Request an
            # idempotent real unload on every diar completion, even with a queue.
            self._unload_diar()

        def mutate(meeting: Meeting) -> bool:
            if meeting.cancelled or meeting.meeting_generation != active.generation:
                return False
            self._to(meeting, MeetingState.DONE)
            meeting.processing_status = (
                ProcessingStatus.PARTIAL if diar_failed else ProcessingStatus.SUCCEEDED
            )
            transcription = meeting.transcription
            transcription.model_id = str(load.get("id", transcription.model_id))
            transcription.revision = str(load.get("revision", transcription.revision))
            transcription.diar_status = str(event.get("diar_status", "skipped"))
            transcription.diarization = (
                active.diar_models is not None and transcription.diar_status != "skipped"
            )
            transcription.finished_at = self.wall_clock().isoformat()
            transcription.wall_ms = int(event.get("wall_ms", 0))
            transcription.peak_rss_kb = int(event.get("peak_rss_kb", 0))
            meeting.audio.retain_days = max(0, min(365, retention))
            meeting.cleanup_pending = (
                self._audio_succeeded(meeting) and retention == 0 and meeting.audio.present
            )
            return True

        def work() -> Callable[[], None]:
            updated = self._update(meeting_id, mutate, require_change=True)
            if updated is not None and updated.cleanup_pending:
                updated = self._remove_audio(meeting_id) or updated

            def completed() -> None:
                if updated is not None and meeting_id not in self.tombstones:
                    self.meetings[meeting_id] = updated
                    self._notify("done", updated)
                self._changed()

            return completed

        self._run(work)
        if not self._queue:
            self._unload()
        self._changed()

    def _remove_audio(self, meeting_id: str) -> Meeting | None:
        """Worker: store transaction proves writers released before deleting."""
        if meeting_id in self.tombstones:
            return None
        try:
            remove_audio(self.store, meeting_id)
            current = self.store.read(meeting_id, "meeting.json")
            self._audio_cleanup_results.put(
                (meeting_id, current.cleanup_pending and self._audio_succeeded(current))
            )
            return current if not current.audio.present else None
        except (MeetingStoreError, OSError, ValueError):
            self._audio_cleanup_results.put((meeting_id, True))
            return None

    def _retry_audio_cleanup(self) -> None:
        """Short, rate-limited retries; neither admission nor models are needed."""
        for mid, at in list(self._audio_retry_at.items()):
            if mid in self.tombstones or mid in self._redo_deleted:
                self._audio_retry_at.pop(mid, None)
                continue
            if at > self.clock() or mid in self._audio_cleanup_io:
                continue
            self._audio_retry_at[mid] = self.clock() + AUDIO_CLEANUP_RETRY_S
            self._audio_cleanup_io.add(mid)
            self._retry_audio_cleanup_one(mid)

    def _retry_audio_cleanup_one(self, meeting_id: str) -> None:
        def work() -> Callable[[], None]:
            current = self.store.read(meeting_id, "meeting.json")
            pending = current.cleanup_pending and self._audio_succeeded(current)
            updated = self._remove_audio(meeting_id) if pending else None
            if not pending:
                self._audio_cleanup_results.put((meeting_id, False))

            def completed() -> None:
                self._audio_cleanup_io.discard(meeting_id)
                if updated is not None and meeting_id not in self.tombstones:
                    self.meetings[meeting_id] = updated
                    self._changed()

            return completed

        self._run(work, failed=lambda: self._audio_cleanup_io.discard(meeting_id))

    # ---- redo lifecycle (published meeting remains DONE throughout) --------

    @staticmethod
    def _redo_failure(meeting: Meeting) -> str:
        return "redo-failed" if meeting.transcription.diar_status == "done" else "failed-again"

    def _redo_update(
        self, meeting_id: str, generation: int, mutate: Callable[[Meeting], bool]
    ) -> Meeting | None:
        """Idempotent redo metadata action, including post-replace fsync errors.

        A false mutator means the same action is already visible on disk.
        Re-sync it rather than treating the previous exception as a rollback.
        The mutator's logical timestamp must be captured outside this function.
        """
        for _ in range(WRITE_ATTEMPTS):
            try:
                current = self.store.read(meeting_id, "meeting.json")
                if (
                    not isinstance(current, Meeting)
                    or current.state != MeetingState.DONE
                    or meeting_id in self.tombstones
                    or current.meeting_generation != generation
                ):
                    return None
                if current.rediar is not None and current.rediar.generation != generation:
                    return None
                updated = deepcopy(current)
                if mutate(updated):
                    self.store.write(meeting_id, "meeting.json", updated, writer=Writer.GUI)
                with private_directory(
                    self.store.root / meeting_id, private_from=self.store.root
                ) as directory:
                    os.fsync(directory)
                return updated
            except (MeetingStoreError, MeetingValidationError, OSError, ValueError):
                continue
        raise MeetingStoreError("Не удалось завершить повторное разделение")

    def _admit_redo(self, entry: _Entry) -> None:
        meeting_id = entry.meeting_id
        self._admitting = meeting_id

        def work() -> Callable[[], None]:
            admission = self.model_probe()
            pair = self.diar_models()
            allowed = self.admit_allowed()
            meeting = self.store.read(meeting_id, "meeting.json")
            available = self.mem_available()
            outcome = "start"
            if not allowed:
                outcome = "paused"
            elif not pair or admission.status != "ok":
                outcome = "failed"
            elif available is not None and available < (
                admission.min_ram_mb + MEMORY_MARGIN_MB + DIAR_MEMORY_MB
            ):
                outcome = "waiting_memory"

            def completed() -> None:
                if meeting_id in self._redo_deleted:
                    return
                self._admitting = ""
                if entry not in self._queue or meeting_id in self.tombstones:
                    return
                if (
                    meeting.state != MeetingState.DONE
                    or meeting.rediar is None
                    or meeting.rediar.generation != self._redo_generations.get(meeting_id)
                ):
                    self._take(meeting_id)
                    return
                self.meetings[meeting_id] = meeting
                # A processing ban pauses both first admission and resume. Model
                # trust/diarization refusal is evaluated only once processing is allowed.
                if not self.admit_allowed() or outcome == "paused":
                    entry.retry_at = 0.0
                    self._changed()
                    return
                if outcome == "failed" or self.diar_models() != pair:
                    self._take(meeting_id)
                    active = _Active(meeting, admission, resume=True, redo=True)
                    self._redo_abort(active, self._redo_failure(meeting))
                elif outcome == "waiting_memory":
                    entry.status = "waiting_memory"
                    entry.retry_at = self.clock() + MEMORY_RETRY_S
                else:
                    self._take(meeting_id)
                    self._active = _Active(
                        meeting, admission, resume=True, diar_models=pair, redo=True
                    )
                    self._advance(self._active)
                self._changed()

            return completed

        def failed() -> None:
            if meeting_id in self._redo_deleted:
                return
            self._admitting = ""
            if entry in self._queue:
                entry.retry_at = self.clock() + ADMIT_RETRY_S
            self._changed()

        self._run(work, failed=failed)

    def _redo_release(self, meeting_id: str) -> None:
        # Exit/abort may finish while a cancel write awaits retry. Retire its
        # timer with the redo context, never route it into ordinary cancellation.
        self._cancel_writes.pop(meeting_id, None)
        self._redo_commits.pop(meeting_id, None)
        self._redo_aborts.pop(meeting_id, None)
        self._redo_retry.pop(meeting_id, None)
        self._redo_generations.pop(meeting_id, None)
        self._redo_io.discard(meeting_id)
        self._redo_preparing.discard(meeting_id)
        self._redo_recovery.pop(meeting_id, None)
        self._redo_resumable.discard(meeting_id)
        self.recovery_errors.pop(meeting_id, None)
        self._changed()

    def _forget_redo(self, meeting_id: str) -> None:
        """Confirmed deletion retires contexts and every delayed redo callback."""
        self._redo_deleted.add(meeting_id)
        self._audio_retry_at.pop(meeting_id, None)
        self._redo_release(meeting_id)
        self._take(meeting_id)
        if self._admitting == meeting_id:
            self._admitting = ""
        self.redo_results.pop(meeting_id, None)
        self.failures.pop(meeting_id, None)
        self.cancelled.discard(meeting_id)

    def _redo_read(self, meeting_id: str) -> Meeting | None:
        """Worker: distinguish deletion/absent directory from damaged private I/O."""
        try:
            meeting = self.store.read(meeting_id, "meeting.json")
        except MeetingStoreError:
            try:
                os.stat(self.store.root / meeting_id, follow_symlinks=False)
            except FileNotFoundError:
                return None
            raise
        if not isinstance(meeting, Meeting):
            raise MeetingStoreError("Не удалось прочитать встречу")
        return None if meeting.state == MeetingState.DELETING else meeting

    def _run_redo(self, active: _Active, work: Callable[[], Callable[[], None]]) -> None:
        """Late work/completions cannot revive a retired commit or discard."""
        mid = active.meeting_id

        def current() -> bool:
            abort = self._redo_aborts.get(mid)
            return (
                mid not in self._redo_deleted
                and self._redo_generations.get(mid) == active.generation
                and (
                    self._redo_commits.get(mid) is active
                    or (abort is not None and abort[0] is active)
                )
            )

        def forgotten() -> None:
            self._forget_redo(mid)

        def guarded() -> Callable[[], None]:
            if not current():
                return lambda: None
            try:
                completed = work()
            except MeetingStoreBusy:
                return lambda: self._redo_later(mid)
            except (MeetingStoreError, OSError):
                if self._redo_read(mid) is not None:
                    raise
                completed = forgotten
            return lambda: completed() if current() else None

        def failed() -> None:
            if current():
                self.recovery_errors[mid] = "io"
                self._redo_later(mid)

        self._run(guarded, failed=failed)

    def _redo_later(self, meeting_id: str) -> None:
        if meeting_id in self._redo_deleted or meeting_id not in self._redo_generations:
            return
        self._redo_io.discard(meeting_id)
        self._redo_retry[meeting_id] = self.clock() + ADMIT_RETRY_S
        self._changed()

    def _retry_redo(self) -> None:
        for mid, at in list(self._redo_recovery.items()):
            if at <= self.clock():
                self._settle_redo_disk(mid)
        for mid in list(self._redo_commits):
            if mid not in self._redo_io and self._redo_retry.get(mid, 0) <= self.clock():
                self._settle_redo(mid)
        for mid, (active, outcome) in list(self._redo_aborts.items()):
            if mid not in self._redo_io and self._redo_retry.get(mid, 0) <= self.clock():
                self._redo_abort(active, outcome)

    def _settle_redo(self, meeting_id: str) -> None:
        """K0–K7; only commit recovery is legal after a delivered successful done."""
        if meeting_id in self._redo_deleted:
            return
        active = self._redo_commits.get(meeting_id)
        if active is None:
            self._settle_redo_disk(meeting_id)
            return
        if meeting_id in self._redo_io:
            return
        self._redo_io.add(meeting_id)
        retention = max(0, min(365, int(self.audio_retention_days())))

        def work() -> Callable[[], None]:
            current = self._redo_read(meeting_id)
            if current is None:
                return lambda: self._forget_redo(meeting_id)
            updated = None
            result = RediarPublishResult.REJECTED
            if (
                current.state == MeetingState.DONE
                and current.meeting_generation == active.generation
                and meeting_id not in self.tombstones
            ):
                if current.rediar is None:
                    # K6 may already have replaced meeting.json before fsync failed.
                    if (
                        current.transcript_generation is None
                        and current.transcription.diar_status == "done"
                    ):
                        state = self.store.transcript_state(meeting_id)
                        if state.generation == active.generation and state.labels_final:
                            result = RediarPublishResult.PUBLISHED
                else:
                    with self.store.writer(meeting_id, Writer.GUI) as writer:
                        result = writer.rediar_publish(active.generation)
                        if result == RediarPublishResult.BUSY and writer.rediar_error:
                            result = RediarPublishResult.IO_ERROR
                if result == RediarPublishResult.PUBLISHED:
                    text = self.store.read(meeting_id, "transcript.json")
                    if text.meeting_generation != active.generation or not text.labels_final:
                        raise MeetingStoreError("Не совпадает поколение результата")

                    def finalize(meeting: Meeting) -> bool:
                        if meeting.rediar is None:
                            if (
                                meeting.transcript_generation is not None
                                or meeting.transcription.diar_status != "done"
                            ):
                                raise MeetingStoreError("Нет завершённого повтора")
                            return False
                        if meeting.rediar != active.meeting.rediar:
                            raise MeetingStoreError("Не совпадает дескриптор повтора")
                        meeting.rediar = None
                        meeting.transcript_generation = None
                        meeting.processing_status = (
                            ProcessingStatus.PARTIAL
                            if text.reconcile.failed
                            else ProcessingStatus.SUCCEEDED
                        )
                        transcription = meeting.transcription
                        transcription.diarization = True
                        transcription.diar_status = "done"
                        transcription.finished_at = active.meeting.transcription.finished_at
                        transcription.wall_ms = active.meeting.transcription.wall_ms
                        transcription.peak_rss_kb = active.meeting.transcription.peak_rss_kb
                        meeting.audio.retain_days = retention
                        meeting.cleanup_pending = (
                            self._audio_succeeded(meeting)
                            and retention == 0
                            and meeting.audio.present
                        )
                        return True

                    updated = self._redo_update(meeting_id, active.generation, finalize)
                    if updated is not None:
                        with self.store.writer(meeting_id, Writer.GUI) as writer:
                            writer.rediar_discard()
                        if updated.cleanup_pending:
                            cleaned = self._remove_audio(meeting_id)
                            if cleaned is None:
                                raise MeetingStoreError("Не удалось завершить хранение звука")
                            updated = cleaned

            def completed() -> None:
                self._redo_io.discard(meeting_id)
                if meeting_id in self.tombstones:
                    self._redo_release(meeting_id)
                elif result in {RediarPublishResult.BUSY, RediarPublishResult.IO_ERROR}:
                    if result == RediarPublishResult.IO_ERROR:
                        self.recovery_errors[meeting_id] = "io"
                    self._redo_later(meeting_id)
                elif result in {RediarPublishResult.WORK_PENDING, RediarPublishResult.INVALID}:
                    self._redo_commits.pop(meeting_id, None)
                    self._redo_abort(active, self._redo_failure(active.meeting))
                else:
                    if updated is not None:
                        self.meetings[meeting_id] = updated
                        self.redo_results[meeting_id] = "done"
                    self._redo_release(meeting_id)

            return completed

        self._run_redo(active, work)

    def _probe_redo(self, meeting: Meeting) -> tuple[Meeting, RediarPublishResult]:
        """Worker-only disk classification; no models, admission or role calls."""
        try:
            if meeting.rediar is not None and meeting.state == MeetingState.DONE:
                with self.store.writer(meeting.id, Writer.GUI) as writer:
                    result = writer.rediar_publish(meeting.meeting_generation)
                    if result == RediarPublishResult.BUSY and writer.rediar_error:
                        result = RediarPublishResult.IO_ERROR
                    return meeting, result
            current = self.store.rediar_cleanup(meeting.id)
            if current.rediar is None and current.state == MeetingState.DONE:
                text = self.store.transcript_state(current.id)
                if (
                    current.transcript_generation is None
                    and current.transcription.diar_status == "done"
                    and text.generation == current.meeting_generation
                    and text.labels_final
                ):
                    return current, RediarPublishResult.PUBLISHED
            return current, RediarPublishResult.REJECTED
        except MeetingStoreBusy:
            return meeting, RediarPublishResult.BUSY
        except (MeetingStoreError, OSError, ValueError):
            return meeting, RediarPublishResult.IO_ERROR

    def _accept_redo_recovery(self, meeting: Meeting, result: RediarPublishResult) -> None:
        mid = meeting.id
        if self._closed or mid in self.tombstones or mid in self._redo_deleted:
            return
        self._redo_recovery.pop(mid, None)
        self.recovery_errors.pop(mid, None)
        self.meetings[mid] = meeting
        if result == RediarPublishResult.WORK_PENDING:
            self._redo_resumable.add(mid)
            if mid in self.cancelled or self._recover_requested:
                self._redo_generations[mid] = meeting.meeting_generation
                if mid in self.cancelled:
                    self._persist_cancel(mid)
                else:
                    self._add(mid, resume=True, redo=True)
        elif result == RediarPublishResult.PUBLISHED:
            self.cancelled.discard(mid)  # publication precedes cancellation
            self._redo_resumable.discard(mid)
            current = deepcopy(meeting)
            if current.rediar is not None:
                current.transcription.finished_at = self.wall_clock().isoformat()
            active = _Active(
                current, ModelAdmission("missing"), resume=True, redo=True, phase="committing"
            )
            self._redo_generations[mid] = current.meeting_generation
            self._redo_commits[mid] = active
            self._settle_redo(mid)
        elif result == RediarPublishResult.REJECTED:
            if (
                self._recover_requested
                and meeting.rediar is None
                and meeting.state != MeetingState.DONE
            ):
                self._recover_ordinary_after_redo(mid)
        else:
            # INVALID at startup is repairable damage, never permission to run
            # a role or discard a potentially committed publication.
            self._redo_resumable.discard(mid)
            self._redo_recovery[mid] = self.clock() + ADMIT_RETRY_S
            if result != RediarPublishResult.BUSY:
                self.recovery_errors[mid] = "io"
        self._changed()

    def _settle_redo_disk(self, meeting_id: str) -> None:
        """Classify/retry startup work and roll forward K3/K6 without a role."""
        if (
            self._closed
            or meeting_id in self.tombstones
            or meeting_id in self._redo_deleted
            or meeting_id in self._redo_preparing
            or meeting_id in self._redo_commits
            or meeting_id in self._redo_aborts
            or meeting_id in self.queued_ids
            or self.active_id == meeting_id
        ):
            return
        self._redo_preparing.add(meeting_id)
        # Before classification the disk may already be past K3. Present the
        # same non-cancellable recovery state as an ambiguous publication retry.
        self._redo_recovery[meeting_id] = self.clock()
        self._changed()

        def work() -> Callable[[], None]:
            current = self._redo_read(meeting_id)
            if current is None:
                return lambda: self._forget_redo(meeting_id)
            meeting, result = self._probe_redo(current)

            def completed() -> None:
                self._redo_preparing.discard(meeting_id)
                self._accept_redo_recovery(meeting, result)

            return completed

        def failed() -> None:
            self._redo_preparing.discard(meeting_id)
            if meeting_id not in self._redo_deleted:
                self._redo_recovery[meeting_id] = self.clock() + ADMIT_RETRY_S
                self.recovery_errors[meeting_id] = "io"
                self._changed()

        self._run(work, failed=failed)

    def _redo_abort(self, active: _Active, outcome: str) -> None:
        """Clear descriptor durably, then discard only after leases are released."""
        mid = active.meeting_id
        if mid in self._redo_deleted or self._redo_generations.get(mid) != active.generation:
            return
        self._redo_aborts[mid] = (active, outcome)
        if mid in self._redo_io:
            return
        self._redo_io.add(mid)

        def clear(meeting: Meeting) -> bool:
            if meeting.rediar is None:
                return False
            meeting.rediar = None
            return True

        def work() -> Callable[[], None]:
            if self._redo_read(mid) is None:
                return lambda: self._forget_redo(mid)
            updated = self._redo_update(mid, active.generation, clear)
            if updated is not None:
                with self.store.writer(mid, Writer.GUI) as writer:
                    writer.rediar_discard()

            def completed() -> None:
                self._redo_io.discard(mid)
                if updated is not None and mid not in self.tombstones:
                    self.meetings[mid] = updated
                    self.redo_results[mid] = outcome
                self._redo_release(mid)

            return completed

        self._run_redo(active, work)

    def _persist_redo_cancel(self, meeting_id: str) -> None:
        generation = self._redo_generations[meeting_id]
        attempts = self._cancel_writes.get(meeting_id, (0, None))[0]
        self._cancel_writes[meeting_id] = (attempts, None)

        def clear(meeting: Meeting) -> bool:
            if meeting.rediar is None:
                return False
            meeting.rediar = None
            return True

        def work() -> Callable[[], None]:
            updated = self._redo_update(meeting_id, generation, clear)
            if updated is None:
                return failed

            def completed() -> None:
                self._cancel_writes.pop(meeting_id, None)
                if (
                    meeting_id in self.tombstones
                    or self._redo_generations.get(meeting_id) != generation
                ):
                    return
                self.meetings[meeting_id] = updated
                active = self._active
                if active is not None and active.meeting_id == meeting_id:
                    self._send_cancel(meeting_id)
                else:
                    self._redo_abort(
                        _Active(updated, ModelAdmission("missing"), resume=True, redo=True),
                        "cancelled",
                    )
                self._changed()

            return completed

        def failed() -> None:
            if (
                meeting_id in self.tombstones
                or self._redo_generations.get(meeting_id) != generation
            ):
                self._cancel_writes.pop(meeting_id, None)
                return
            count = self._cancel_writes.get(meeting_id, (0, None))[0] + 1
            if count < CANCEL_WRITE_ATTEMPTS:
                self._cancel_writes[meeting_id] = (count, self.clock() + CANCEL_RETRY_S)
            else:
                self._cancel_writes.pop(meeting_id, None)
                self._cancel_failed(meeting_id)

        self._run(work, failed=failed)

    # ---- durable metadata ---------------------------------------------------

    def _persist_cancel(self, meeting_id: str) -> None:
        """D12: meeting.job.cancel only after ``cancelled=true`` is on disk."""
        if meeting_id in self._redo_generations:
            self._persist_redo_cancel(meeting_id)
            return
        attempts = self._cancel_writes.get(meeting_id, (0, None))[0]
        self._cancel_writes[meeting_id] = (attempts, None)

        def mutate(meeting: Meeting) -> bool:
            meeting.cancelled = True
            meeting.processing_status = ProcessingStatus.CANCELLED
            if meeting.state in RESUMABLE:
                self._to(meeting, MeetingState.RECORDED)
            return True

        def work() -> Callable[[], None]:
            updated = self._update(meeting_id, mutate)
            if updated is None:
                return failed

            def completed() -> None:
                if meeting_id not in self.cancelled:
                    self._cancel_writes.pop(meeting_id, None)
                    return  # «Расшифровать» again: a new job owns the meeting
                self._cancel_writes.pop(meeting_id, None)
                if meeting_id not in self.tombstones:
                    self.meetings[meeting_id] = updated
                self._send_cancel(meeting_id)
                self._changed()

            return completed

        def failed() -> None:
            if meeting_id in self.tombstones or meeting_id not in self.cancelled:
                self._cancel_writes.pop(meeting_id, None)
                return  # delete or a new job owns the meeting now
            attempts = self._cancel_writes.get(meeting_id, (0, None))[0] + 1
            if attempts < CANCEL_WRITE_ATTEMPTS:
                self._cancel_writes[meeting_id] = (attempts, self.clock() + CANCEL_RETRY_S)
                return
            self._cancel_writes.pop(meeting_id, None)
            self._cancel_failed(meeting_id)

        self._run(work, failed=failed)

    def _retry_cancel_writes(self) -> None:
        now = self.clock()
        for meeting_id, (_, retry_at) in list(self._cancel_writes.items()):
            if retry_at is not None and retry_at <= now:
                self._persist_cancel(meeting_id)

    def _cancel_failed(self, meeting_id: str) -> None:
        """The cancel cannot be made durable, so it is never sent (D12): a silent cancel
        would come back after a restart. The user sees «Ошибка расшифровки» instead."""
        self.failures[meeting_id] = "io"
        active = self._active
        if meeting_id in self._redo_generations:
            if active is not None and active.meeting_id == meeting_id:
                active.purpose = "cancel-write-failed"
                self._kill(active)
            self._changed()
            return
        if active is not None and active.meeting_id == meeting_id and active.purpose == "cancel":
            if active.phase == "exiting":  # the role is already being killed
                active.purpose, active.failure = "fail", "io"
            else:
                self._fail_after_exit(active, "io")
            return
        meeting = self.meetings.get(meeting_id)
        if meeting is not None:
            self._notify("failed", meeting)
        self._changed()

    @staticmethod
    def _to(meeting: Meeting, target: MeetingState) -> bool:
        meeting.state = target
        return True

    def _has_checkpoint(self, meeting_id: str) -> bool:
        """Worker thread: an unfinished job left checkpoint.json behind."""
        try:
            with private_directory(
                self.store.root / meeting_id, private_from=self.store.root
            ) as directory:
                os.stat("checkpoint.json", dir_fd=directory, follow_symlinks=False)
        except (OSError, ValueError):
            return False
        return True

    def _update(
        self,
        meeting_id: str,
        mutate: Callable[[Meeting], bool],
        *,
        require_change: bool = False,
    ) -> Meeting | None:
        """Worker thread: read-modify-write meeting.json; tombstones are never touched.

        A lifecycle edge that the state machine does not allow directly is taken
        through ``transcribing`` (e.g. needs_model -> transcribing -> failed).
        """
        for attempt in range(WRITE_ATTEMPTS):
            try:
                current = self.store.read(meeting_id, "meeting.json")
                if not isinstance(current, Meeting) or current.state == MeetingState.DELETING:
                    return None
                updated = deepcopy(current)
                if not mutate(updated):
                    return None if require_change else current
                if meeting_id in self.tombstones and updated.state != MeetingState.DELETING:
                    return None  # with a tombstone only the tombstone itself is written
                self._write_route(current, updated)
                return updated
            except (MeetingStoreError, MeetingValidationError):
                if attempt + 1 < WRITE_ATTEMPTS:
                    time.sleep(0.02)
        return None

    def _write_route(self, current: Meeting, updated: Meeting) -> None:
        target = updated.state
        if target != current.state:
            try:
                transition(current.state, target)
            except MeetingValidationError:
                transition(current.state, MeetingState.TRANSCRIBING)
                transition(MeetingState.TRANSCRIBING, target)
                step = deepcopy(updated)
                step.state = MeetingState.TRANSCRIBING
                self.store.write(updated.id, "meeting.json", step, writer=Writer.GUI)
        self.store.write(updated.id, "meeting.json", updated, writer=Writer.GUI)

    # ---- filesystem plumbing ------------------------------------------------

    def _run(
        self,
        operation: Callable[[], Callable[[], None]],
        *,
        failed: Callable[[], None] | None = None,
    ) -> None:
        """Worker ``operation`` returns a GUI completion. ``failed`` runs on the GUI
        thread instead whenever the work raised or the writer refused it, so no caller
        waits forever for a completion that never comes."""
        fallback = failed if failed is not None else lambda: None
        self._pending += 1

        def run() -> None:
            completed = fallback
            try:
                completed = operation()
            except Exception:  # noqa: BLE001, S110 — the GUI side runs ``failed``
                pass
            finally:
                self._results.put(completed)

        try:
            self._submit(run)
        except (queue.Full, RuntimeError):
            self._pending -= 1
            fallback()
            return
        self._drain()

    def _drain(self) -> None:
        while not self._results.empty():
            completed = self._results.get_nowait()
            self._pending -= 1
            completed()
        while not self._audio_cleanup_results.empty():
            mid, retry = self._audio_cleanup_results.get_nowait()
            if (
                retry
                and not self._closed
                and mid not in self.tombstones
                and mid not in self._redo_deleted
            ):
                self._audio_retry_at.setdefault(mid, self.clock() + AUDIO_CLEANUP_RETRY_S)
            else:
                self._audio_retry_at.pop(mid, None)

    @property
    def filesystem_pending(self) -> bool:
        return self._pending > 0
