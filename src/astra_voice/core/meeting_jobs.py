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
import stat
import time
from collections import deque
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import uuid4

from astra_voice.core.meeting_session import MetadataWriter
from astra_voice.meetings import MeetingValidationError
from astra_voice.meetings.events import MeetingState, ProcessingStatus, transition
from astra_voice.meetings.model import Meeting
from astra_voice.meetings.store import MeetingStore, MeetingStoreError, Writer
from astra_voice.security.files import private_directory

Message = dict[str, Any]
ROLE = "meeting-transcribe"
INITIAL_CREDITS = 64
CREDIT_STEP = 32
MEMORY_MARGIN_MB = 200  # no diarization in M4 (+900 MB arrives with M7)
MEMORY_RETRY_S = 60.0
MODEL_RETRY_S = 60.0
CANCEL_TIMEOUT_S = 1.0  # worker/meeting_contract.CANCEL_TIMEOUT_S
MAX_CRASHES = 3  # restarts per window; the fourth crash fails the job
CRASH_WINDOW_S = 600.0
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
Notify = Callable[[str, Meeting], None]


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
    """Remove ``<meeting>/audio`` through directory descriptors, never by path walk."""
    with private_directory(store.root / meeting_id, private_from=store.root) as directory:
        try:
            audio = os.open(
                "audio",
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=directory,
            )
        except FileNotFoundError:
            return
        try:
            for name in os.listdir(audio):
                info = os.stat(name, dir_fd=audio, follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    raise OSError("meeting-audio-unexpected-directory")
                os.unlink(name, dir_fd=audio)
            os.fsync(audio)
        finally:
            os.close(audio)
        os.rmdir("audio", dir_fd=directory)
        os.fsync(directory)


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class _Entry:
    meeting_id: str
    order: int
    status: str = "queued"  # queued | waiting_memory | needs_model
    retry_at: float = 0.0
    resume: bool = False
    notified: set[str] = field(default_factory=set)


@dataclass
class _Active:
    meeting: Meeting
    admission: ModelAdmission
    resume: bool
    phase: str = "launching"
    # launching -> loading -> starting -> running; cancelling -> exiting; crashed
    purpose: str = ""  # "" | cancel | delete
    load_req: str = ""
    start_req: str = ""
    cancel_req: str = ""
    job_sent: bool = False
    deadline: float | None = None
    received: int = 0
    done_ms: int = 0
    total_ms: int = 0
    percent: int = 0
    utterances: dict[int, Message] = field(default_factory=dict)
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
        model_probe: Callable[[], ModelAdmission] = probe_gigaam_model,
        mem_available: Callable[[], int | None] = mem_available_mb,
        clock: Callable[[], float] = time.monotonic,
        submit: Callable[[Callable[[], None]], None] | None = None,
        notify: Notify = lambda kind, meeting: None,
        echo_gate: bool | None = None,
    ) -> None:
        self.store = store
        self._role_factory = role_factory
        self.transcribe_immediately = transcribe_immediately
        self.audio_retention_days = audio_retention_days
        self.model_probe = model_probe
        self.mem_available = mem_available
        self.clock = clock
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
        self._crashes: deque[float] = deque()
        self._exits: list[int] = []
        self.failures: dict[str, str] = {}
        self.meetings: dict[str, Meeting] = {}
        self.tombstones: set[str] = set()
        self.cancelled: set[str] = set()
        self.revision = 0
        self._closed = False

    # ---- public API -----------------------------------------------------

    @property
    def active_id(self) -> str:
        return self._active.meeting_id if self._active is not None else ""

    @property
    def queued_ids(self) -> list[str]:
        return [entry.meeting_id for entry in self._queue]

    @property
    def busy(self) -> bool:
        """A job is running or waits in the queue (exit stays unconfirmed: T-478)."""
        return self._active is not None or bool(self._queue)

    @property
    def percent(self) -> int | None:
        """Running job's progress for the tray hint, else None."""
        active = self._active
        if active is None or active.purpose:
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
            "percent": active.percent,
            "done_ms": active.done_ms,
            "total_ms": active.total_ms,
            "utterances": utterances,
        }

    def row(self, meeting_id: str) -> Message:
        """Presentation hints for a list row: percent, failure text, cancelled text."""
        active = self._active
        percent = (
            active.percent
            if active is not None and active.meeting_id == meeting_id and not active.purpose
            else 0
        )
        code = self.failures.get(meeting_id, "")
        return {
            "percent": percent,
            "failure": FAILED_TEXT.get(code, FAILED_DEFAULT_TEXT),
            "cancelledText": CANCELLED_TEXT,
        }

    def recover(self) -> None:
        """Start of program: resume unfinished jobs (not cancelled/deleting) and cleanups."""

        def scan() -> Callable[[], None]:
            try:
                meetings = self.store.list()
            except MeetingStoreError:
                meetings = []
            resume: list[Meeting] = []
            cleanup: list[str] = []
            for meeting in meetings:
                if meeting.state == MeetingState.DELETING or meeting.cancelled:
                    continue
                if meeting.state == MeetingState.DONE and meeting.cleanup_pending:
                    cleanup.append(meeting.id)
                elif meeting.state in RESUMABLE or (
                    meeting.state == MeetingState.RECORDED and self._has_checkpoint(meeting.id)
                ):
                    resume.append(meeting)
            resume.sort(key=lambda row: (row.ended_at or row.started_at, row.id))

            def completed() -> None:
                for meeting_id in cleanup:
                    self._cleanup(meeting_id)
                for meeting in resume:
                    if meeting.state == MeetingState.RECORDED:
                        self._enqueue(meeting.id, auto=False, resume=True)
                    else:
                        self._add(meeting.id, resume=True)
                        self.meetings[meeting.id] = meeting

            return completed

        self._run(scan)

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
        if meeting_id == self.active_id or meeting_id in self.queued_ids:
            return
        self.cancelled.discard(meeting_id)
        self._enqueue(meeting_id, auto=False)

    def models_changed(self) -> None:
        """A model was installed/checked: needs_model jobs re-check now (T-481)."""
        for entry in self._queue:
            if entry.status == "needs_model":
                entry.retry_at = 0.0

    def cancel(self, meeting_id: str) -> None:
        """D12: durable cancelled=true first, then meeting.job.cancel (<= 1 s, else kill)."""
        if self._closed or meeting_id in self.tombstones:
            return
        self.cancelled.add(meeting_id)
        entry = self._take(meeting_id)
        active = self._active
        if active is not None and active.meeting_id == meeting_id:
            if active.purpose:
                return
            active.purpose = "cancel"
            active.phase = "cancelling"
            self._changed()
            self._persist_cancel(meeting_id, then=lambda: self._send_cancel(meeting_id))
            return
        if entry is not None or meeting_id == self._admitting:
            self._persist_cancel(meeting_id, then=None)

    def delete(self, meeting_id: str, released: Callable[[], None]) -> None:
        """Tombstone -> cancel the job -> confirmed exit of the role -> ``released()``.

        M5 owns the rename/rmtree performed by ``released``. A SIGKILL without a
        confirmed exit (poll) never releases the files.
        """
        if self._closed or meeting_id in self.tombstones:
            return
        self.tombstones.add(meeting_id)
        self._take(meeting_id)

        def tombstone() -> Callable[[], None]:
            ok = self._update(meeting_id, lambda meeting: self._to(meeting, MeetingState.DELETING))

            def completed() -> None:
                if ok is None:
                    self.tombstones.discard(meeting_id)
                    return
                self.meetings.pop(meeting_id, None)
                active = self._active
                if active is not None and active.meeting_id == meeting_id:
                    active.released = released
                    if active.purpose == "delete":
                        return
                    if active.purpose == "cancel" and active.phase == "exiting":
                        active.purpose = "delete"
                        return
                    active.purpose = "delete"
                    active.phase = "cancelling"
                    self._send_cancel(meeting_id)
                else:
                    released()
                self._changed()

            return completed

        self._run(tombstone)

    def tick(self) -> None:
        self._drain()
        if self._closed:
            return
        self._handle_exits()
        active = self._active
        if active is not None:
            self._advance(active)
        if self._active is None:
            self._schedule()
        self._drain()

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

    def _add(self, meeting_id: str, *, resume: bool) -> None:
        if meeting_id in self.tombstones or meeting_id in self.queued_ids:
            return
        self._order += 1
        self._queue.append(_Entry(meeting_id, self._order, resume=resume))
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
            if auto and (meeting.state != MeetingState.RECORDED or meeting.cancelled):
                return False
            if not auto and meeting.state not in TRANSCRIBABLE and not resume:
                return False
            if not resume:
                meeting.meeting_generation += 1
                meeting.cancelled = False
                meeting.processing_status = ProcessingStatus.PENDING
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
                self._add(meeting_id, resume=resume)

            return completed

        self._run(work)

    def _schedule(self) -> None:
        if self._admitting or not self._queue:
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
        meeting_id = entry.meeting_id
        self._admitting = meeting_id

        def work() -> Callable[[], None]:
            admission = self.model_probe()
            available = self.mem_available() if admission.status == "ok" else None
            if admission.status == "missing":
                outcome = "needs_model"
                target = MeetingState.NEEDS_MODEL
            elif admission.status != "ok":
                outcome, target = "failed", MeetingState.FAILED
            elif available is not None and available < admission.min_ram_mb + MEMORY_MARGIN_MB:
                outcome, target = "waiting_memory", MeetingState.WAITING_MEMORY
            else:
                outcome, target = "start", MeetingState.TRANSCRIBING

            def mutate(meeting: Meeting) -> bool:
                if meeting.cancelled or meeting.state not in RESUMABLE:
                    return False
                self._to(meeting, target)
                if outcome == "start":
                    meeting.processing_status = ProcessingStatus.RUNNING
                elif outcome == "failed":
                    meeting.processing_status = ProcessingStatus.FAILED
                return True

            updated = self._update(meeting_id, mutate, require_change=True)

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
                else:
                    self._take(meeting_id)
                    self._active = _Active(updated, admission, resume=current.resume)
                    self._advance(self._active)
                self._changed()

            return completed

        self._run(work)

    def _notify_once(self, entry: _Entry, kind: str, meeting: Meeting) -> None:
        if kind not in entry.notified:
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

    def _send(self, kind: str, **fields: Any) -> str:
        role = self._role
        if role is None:
            return ""
        request = uuid4().hex
        try:
            role.send({"type": kind, "req": request, "request_type": kind, **fields})
        except (RuntimeError, ValueError):
            return ""
        return request

    def _identity(self, active: _Active) -> Message:
        return {"meeting_id": active.meeting_id, "meeting_generation": active.generation}

    def _advance(self, active: _Active) -> None:
        if active.phase in {"launching", "crashed"} and not active.purpose:
            role = self._ensure_role()
            if role is None or not role.ready:
                return
            spec = active.admission.load or {}
            if self._loaded == (role.generation, spec):
                self._start_job(active)
                return
            active.load_req = self._send("model.load", **spec)
            if active.load_req:
                active.phase = "loading"
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
        active.start_req = self._send(
            "meeting.job.start",
            **self._identity(active),
            dir=str(self.store.root / meeting.id),
            tracks=tracks,
            variant=meeting.variant,
            diarization=False,
            self_track="mic",
            resume=active.resume,
            echo_gate=bool(echo and "far" in tracks),
            limits=dict(LIMITS),
        )
        if not active.start_req:
            return
        active.job_sent = True
        active.phase = "starting"
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
        role = self._role
        if role is not None and getattr(role, "state", "running") != "stopped":
            try:
                role.set_meeting_generation(active.meeting_id, active.generation + 1)
            except (RuntimeError, ValueError):
                pass
        self._active = None
        if active.released is not None:
            active.released()
        self._changed()

    def _handle_exits(self) -> None:
        exits, self._exits = self._exits, []
        if not exits:
            return
        self._loaded = None  # a new process loads the model again
        active = self._active
        if active is None:
            return
        if active.purpose:
            # A confirmed exit ends cancel/delete, also on the supervisor's own kill.
            self._finish_stop(active)
            return
        now = self.clock()
        while self._crashes and now - self._crashes[0] >= CRASH_WINDOW_S:
            self._crashes.popleft()
        self._crashes.append(now)
        if len(self._crashes) > MAX_CRASHES:
            self._fail(active, "io")
            return
        active.phase = "crashed"
        active.resume = True
        active.job_sent = False
        self._changed()

    # ---- events -----------------------------------------------------------

    def _event(self, event: Message) -> None:
        role = self._role
        if role is None or event.get("generation") != role.generation:
            return
        kind = event.get("type")
        active = self._active
        if kind == "error" and "req" not in event:
            if event.get("code") in {"restart-limit", "worker-start"} and active is not None:
                if active.purpose:
                    self._finish_stop(active)
                else:
                    self._fail(active, "io")
            return
        if active is None:
            return
        req = event.get("req")
        if req is not None and req == active.cancel_req:
            if kind == "meeting.job.cancelled" and active.phase == "cancelling":
                if active.purpose == "delete":
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
                code = event.get("code")
                if code == "memory":
                    self._wait_memory(active)
                elif code == "model":
                    self._fail(active, "model")
            return
        if req is not None and req == active.start_req:
            if kind == "meeting.job.accepted":
                active.phase = "running"
                self._changed()
            elif kind == "error":
                self._fail(active, "model" if event.get("code") == "model" else "io")
            return
        if req is not None or (
            event.get("meeting_id") != active.meeting_id
            or event.get("meeting_generation") != active.generation
        ):
            return  # MJ-14: other meetings and stale generations never publish
        if kind == "meeting.job.progress":
            total = int(event.get("total_ms", 0))
            done = int(event.get("done_ms", 0))
            percent = min(100, done * 100 // total) if total > 0 else 0
            active.done_ms, active.total_ms = done, total
            if active.phase == "starting":
                active.phase = "running"
            if percent != active.percent:
                active.percent = percent
                self._changed()
        elif kind == "meeting.job.utterance":
            seq = int(event["seq"])
            active.utterances[seq] = {
                key: event[key]
                for key in ("seq", "id", "track", "start_ms", "end_ms", "text", "speaker")
                if key in event
            }
            active.received += 1
            if active.received % CREDIT_STEP == 0:
                self._send("meeting.job.credit", **self._identity(active), n=CREDIT_STEP)
            self._changed()
        elif kind == "meeting.job.waiting":
            self._wait_memory(active)
        elif kind == "meeting.job.failed":
            code = str(event.get("code", "io"))
            if code == "memory":
                self._wait_memory(active)
            else:
                self._fail(active, code)
        elif kind == "meeting.job.done":
            self._done(active, event)

    def _unload(self) -> None:
        if self._loaded is not None:
            self._loaded = None
            self._send("model.unload")

    def _wait_memory(self, active: _Active) -> None:
        """Role ended the job for memory: unload the model, retry in 60 s (T-483)."""
        self._active = None
        self._unload()
        meeting_id = active.meeting_id
        self._order += 1
        entry = _Entry(
            meeting_id,
            order=-self._order,  # it keeps the head of the FIFO
            status="waiting_memory",
            retry_at=self.clock() + MEMORY_RETRY_S,
            resume=True,
        )
        self._queue.append(entry)

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
        """Audio stays; «Ошибка расшифровки» with «Повторить» (T-482/T-484)."""
        self._active = None
        if code == "model":
            self._loaded = None
        meeting_id = active.meeting_id
        self.failures[meeting_id] = code

        def mutate(meeting: Meeting) -> bool:
            if meeting.cancelled or meeting.meeting_generation != active.generation:
                return False
            self._to(meeting, MeetingState.FAILED)
            meeting.processing_status = ProcessingStatus.FAILED
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
        self._active = None
        meeting_id = active.meeting_id
        retention = int(self.audio_retention_days())
        load = active.admission.load or {}

        def mutate(meeting: Meeting) -> bool:
            if meeting.cancelled or meeting.meeting_generation != active.generation:
                return False
            self._to(meeting, MeetingState.DONE)
            meeting.processing_status = ProcessingStatus.SUCCEEDED
            transcription = meeting.transcription
            transcription.model_id = str(load.get("id", transcription.model_id))
            transcription.revision = str(load.get("revision", transcription.revision))
            transcription.diarization = False
            transcription.finished_at = _now()
            transcription.wall_ms = int(event.get("wall_ms", 0))
            transcription.peak_rss_kb = int(event.get("peak_rss_kb", 0))
            meeting.audio.retain_days = max(0, min(365, retention))
            meeting.cleanup_pending = retention == 0 and meeting.audio.present
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

    def _cleanup(self, meeting_id: str) -> None:
        """D6: a durable cleanup_pending is completed after restart, no reprocessing."""

        def work() -> Callable[[], None]:
            updated = self._remove_audio(meeting_id)

            def completed() -> None:
                if updated is not None:
                    self.meetings[meeting_id] = updated
                    self._changed()

            return completed

        self._run(work)

    def _remove_audio(self, meeting_id: str) -> Meeting | None:
        """Worker thread: delete audio/ then publish audio.present=false, deleted_at."""
        try:
            current = self.store.read(meeting_id, "meeting.json")
            if (
                not isinstance(current, Meeting)
                or not current.cleanup_pending
                or current.state != MeetingState.DONE
            ):
                return None
            remove_audio(self.store, meeting_id)
        except (MeetingStoreError, OSError, ValueError):
            return None

        def mutate(meeting: Meeting) -> bool:
            if not meeting.cleanup_pending:
                return False
            meeting.cleanup_pending = False
            meeting.audio.present = False
            meeting.audio.deleted_at = _now()
            return True

        return self._update(meeting_id, mutate, require_change=True)

    # ---- durable metadata ---------------------------------------------------

    def _persist_cancel(self, meeting_id: str, *, then: Callable[[], None] | None) -> None:
        def mutate(meeting: Meeting) -> bool:
            meeting.cancelled = True
            meeting.processing_status = ProcessingStatus.CANCELLED
            if meeting.state in RESUMABLE:
                self._to(meeting, MeetingState.RECORDED)
            return True

        def work() -> Callable[[], None]:
            updated = self._update(meeting_id, mutate)

            def completed() -> None:
                if updated is not None and meeting_id not in self.tombstones:
                    self.meetings[meeting_id] = updated
                # A failed write still stops the role: the user asked to stop and
                # this session keeps the cancel in memory (not resumed until restart).
                if then is not None:
                    then()
                self._changed()

            return completed

        self._run(work)

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

    def _run(self, operation: Callable[[], Callable[[], None]]) -> None:
        self._pending += 1

        def run() -> None:
            try:
                completed = operation()
            except Exception:  # noqa: BLE001 — a failed write leaves the state as is

                def completed() -> None:
                    pass

            self._results.put(completed)

        try:
            self._submit(run)
        except (queue.Full, RuntimeError):
            self._pending -= 1
            return
        self._drain()

    def _drain(self) -> None:
        while not self._results.empty():
            completed = self._results.get_nowait()
            self._pending -= 1
            completed()

    @property
    def filesystem_pending(self) -> bool:
        return self._pending > 0
