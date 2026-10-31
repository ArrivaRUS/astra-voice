"""GUI-owned meeting lifecycle. Pump/timer and session callbacks are wired by the UI.

Only the click bridge grants opening commands. Capture owns PCM and durable
edges; this controller owns meeting.json and never infers closure from a signal.
"""

from __future__ import annotations

import ctypes
import os
import queue
import re
import threading
import time
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from astra_voice.core.command_mode import SessionSnapshot
from astra_voice.meetings.events import MeetingState
from astra_voice.meetings.model import Interval, Mark, Meeting
from astra_voice.meetings.segments import CaptureJournal, SegmentSidecar
from astra_voice.meetings.store import MeetingStore, MeetingStoreError, Writer, new_meeting_id
from astra_voice.platform.session import SessionKind
from astra_voice.security.files import private_directory
from astra_voice.worker import ipc

MEETING_HARD_LIMIT_MS = 14_400_000
LOCK_SIGNAL_BY_SESSION = {SessionKind.KDE: "login1", SessionKind.FLY: "none"}
ERROR_TEXT = {
    "disk-low": (
        "Мало места на диске: для записи встречи нужно около 600 МБ. Освободите "
        "место или удалите старые встречи."
    ),
    "audio-unavailable": (
        "Не удалось начать запись: звуковая служба не отвечает. Попробуйте ещё "
        "раз или обратитесь к администратору."
    ),
    "storage-unavailable": (
        "Не удалось начать запись: папка встреч недоступна для записи. Обратитесь к администратору."
    ),
    "busy": "Уже идёт запись встречи",
    "far-unavailable": "",  # Step 3b presents the alternative source, using W4 copy.
    "session-blocked": (
        "Запись недоступна: в этой сессии нельзя узнать, заблокирован ли экран, — "
        "а без этого программа не может поставить запись на паузу при блокировке."
    ),
    "disabled": "Запись встреч отключена.",
}
WARNING_TEXT = {
    "storage-network": (
        "Домашняя папка находится на сетевом диске — запись встречи будет "
        "храниться не только на этом компьютере. Записывайте, только если это "
        "разрешено в вашей организации."
    ),
    "audio-remote": (
        "Звуковая служба работает не на этом компьютере — звук встречи пройдёт "
        "через другой компьютер. Записывайте, только если это разрешено в вашей "
        "организации."
    ),
    "lock-unavailable": (
        "В этой сессии запись продолжится при блокировке экрана. Остановите "
        "запись вручную перед блокировкой."
    ),
}
Message = dict[str, Any]


class CaptureSupervisor(Protocol):
    generation: int

    @property
    def ready(self) -> bool: ...
    @property
    def exited(self) -> bool: ...
    def send(self, message: Message, *, timeout: float | None = None) -> Message: ...
    def set_meeting_generation(self, meeting_id: str, generation: int) -> None: ...
    def kill(self) -> None: ...
    def pump(self, timeout: float = 0.0) -> None: ...


@dataclass(frozen=True)
class StorageStatus:
    free_mb: float
    writable: bool
    network: bool


class MetadataWriter:
    """GUI-process writer thread; bounded control queue never waits for fsync."""

    def __init__(self) -> None:
        self._jobs: queue.Queue[Callable[[], None]] = queue.Queue(64)
        self._thread: threading.Thread | None = None

    def submit(self, operation: Callable[[], None]) -> None:
        self._jobs.put_nowait(operation)
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="meeting-metadata", daemon=True)
            self._thread.start()

    def _run(self) -> None:
        while True:
            self._jobs.get()()


def filesystem_type(descriptor: int) -> int:
    """Linux fstatfs starts with a native signed long; oversized aligned buffer."""
    buffer = (ctypes.c_long * 32)()
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.fstatfs(ctypes.c_int(descriptor), ctypes.byref(buffer)) != 0:
        raise OSError("meeting-storage-probe")
    return int(buffer[0]) & 0xFFFFFFFF


def probe_storage(path: Path) -> StorageStatus:
    """Probe the private target and Linux mount type without exporting paths."""
    with private_directory(path, create=True) as directory:
        stats = os.fstatvfs(directory)
        fs_type = filesystem_type(directory)
        name = ".probe-" + uuid4().hex
        fd = os.open(
            name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory
        )
        os.close(fd)
        os.unlink(name, dir_fd=directory)
    network = fs_type in {0x6969, 0xFF534D42, 0xFE534D42, 0x01021997}
    best = -1
    for row in Path("/proc/self/mountinfo").read_text().splitlines():
        left, _, right = row.partition(" - ")
        fields = left.split()
        if len(fields) < 5 or not right:
            continue
        mount = Path(fields[4].replace("\\040", " ").replace("\\134", "\\"))
        if path.is_relative_to(mount) and len(str(mount)) > best:
            best = len(str(mount))
            kind = right.split()[0]
            network = (
                network
                or kind in {"nfs", "nfs4", "cifs", "smb3", "9p"}
                or kind.startswith(("fuse.sshfs", "fuse.rclone", "fuse.s3", "fuse.davfs"))
            )
    return StorageStatus(stats.f_bavail * stats.f_frsize / 1048576, True, network)


class MeetingController:
    def __init__(
        self,
        store: MeetingStore,
        supervisor: CaptureSupervisor,
        *,
        session: Callable[[], SessionSnapshot],
        session_kind: SessionKind,
        clock: Callable[[], float] = time.monotonic,
        storage_probe: Callable[[Path], StorageStatus] = probe_storage,
        enabled: Callable[[], bool] = lambda: False,
        policy_allowed: Callable[[], bool] = lambda: False,
        id_factory: Callable[[], str] = lambda: new_meeting_id(datetime.now(UTC)),
        metadata_submit: Callable[[Callable[[], None]], None] | None = None,
        filesystem_submit: Callable[[Callable[[], None]], None] | None = None,
    ) -> None:
        self.store, self.supervisor = store, supervisor
        self.session, self.session_kind, self.clock = session, session_kind, clock
        self.storage_probe, self.enabled, self.policy_allowed = (
            storage_probe,
            enabled,
            policy_allowed,
        )
        self.id_factory = id_factory
        writer = MetadataWriter()
        self._metadata_submit = metadata_submit or writer.submit
        self._filesystem_submit = filesystem_submit or writer.submit
        self._filesystem_results: queue.SimpleQueue[Callable[[], None]] = queue.SimpleQueue()
        self._filesystem_pending = 0
        self._disk_pending = False
        self._attempt = 0
        self._capture_succeeded = False
        self._write_results: queue.SimpleQueue[tuple[str, bool, bool]] = queue.SimpleQueue()
        self._exit_confirmed = False
        self.state = "idle"
        self.meeting: Meeting | None = None
        self.pause_reasons: set[str] = set()
        self.manual_resume_required = False
        self.lock_signal_unavailable = session_kind == SessionKind.FLY
        self.far_available = False
        self.far_reason = ""
        self.mic_only_available = False
        self.saved = False
        self.error_code = ""
        self.warnings: list[str] = []
        self.interrupted_dialog_ids: list[str] = []
        self.tracks: dict[str, str] = {}
        self.limit_warning = False
        self._identity: Message = {}
        self._requests: dict[str, str] = {}
        self._reserved = False
        self._committed = False
        self._click_epoch = 0
        self._capture_epoch = 0
        self._epoch_pending = False
        self._prepare_deadline: float | None = None
        self._close_deadline: float | None = None
        self._terminal = False
        self._finalizing = False
        self._capture_finalized = False
        self._physically_closed = True
        self._finalized_snapshot: Message | None = None
        self._process_generation = supervisor.generation
        self._active_at: float | None = None
        self._recorded_ms = 0
        self._pause_at: float | None = None
        self._last_keepalive = self._last_disk = self.clock()
        self._sequence = 0
        self._far: Message = {"kind": "none"}

    @property
    def error_text(self) -> str:
        return ERROR_TEXT.get(
            self.error_code, ERROR_TEXT["audio-unavailable"] if self.error_code else ""
        )

    @property
    def recorded_ms(self) -> int:
        extra = (
            0 if self._active_at is None else max(0, int((self.clock() - self._active_at) * 1000))
        )
        return min(MEETING_HARD_LIMIT_MS, self._recorded_ms + extra)

    def _allowed(self, snapshot: SessionSnapshot | None = None) -> bool:
        snapshot = self.session() if snapshot is None else snapshot
        if self.session_kind == SessionKind.FLY:
            return not snapshot.preparing_for_sleep and (snapshot.active or not snapshot.known)
        return snapshot.allowed

    def _error(self, code: str) -> bool:
        self.error_code = code
        return False

    def _send(self, action: str, **fields: Any) -> None:
        request = uuid4().hex
        message = {
            "type": "meeting.capture." + action,
            "req": request,
            "request_type": "meeting.capture." + action,
            **fields,
        }
        self._requests[request] = message["type"]
        self.supervisor.send(message)

    def _admission(self) -> Message:
        return {
            **self._identity,
            "blocked_epoch": self._capture_epoch,
            "admission_mono_ms": int(self.clock() * 1000),
        }

    def start(self, far: Message | None = None) -> bool:
        """Click-only API; construction, recovery and polling never invoke it."""
        if not self.enabled() or not self.policy_allowed():
            return self._error("disabled")
        if (
            self._filesystem_pending
            or self.state not in {"idle", "stopped"}
            or (self.state == "stopped" and not self._exit_confirmed)
        ):
            return self._error("busy")
        click_snapshot = self.session()
        if not self._allowed(click_snapshot):
            return self._error("session-blocked")
        self._click_epoch = click_snapshot.blocked_epoch
        self.error_code = ""
        self._far = far or {"kind": "none"}
        self.far_available = self.mic_only_available = False
        self.far_reason = ""
        self.meeting = Meeting(
            id=self.id_factory(),
            started_at=datetime.now(UTC).isoformat(),
            state=MeetingState.RECORDING,
            variant="mic_only" if self._far["kind"] == "none" else self._far["kind"],
        )
        self.meeting.warnings.whole_output = self._far["kind"] == "sink_monitor"
        self._reserved = self._committed = False
        self.saved = self._terminal = self._exit_confirmed = self._capture_finalized = False
        self._capture_succeeded = False
        self._attempt += 1
        attempt = self._attempt
        self._process_generation = self.supervisor.generation
        self._last_keepalive = self._last_disk = self.clock()
        self._identity = {}
        self._recorded_ms = 0
        self._active_at = self._pause_at = None
        self.pause_reasons.clear()
        self.manual_resume_required = self.limit_warning = False
        self.state = "preparing"
        self._prepare_deadline = self.clock() + 5
        self._prepare_sent = False
        self.supervisor.set_meeting_generation(self.meeting.id, self.meeting.meeting_generation)
        meeting_id = self.meeting.id

        def preflight() -> Callable[[], None]:
            code = ""
            storage: StorageStatus | None = None
            reserved = False
            try:
                storage = self.storage_probe(self.store.root)
                if storage.free_mb < 600:
                    code = "disk-low"
                elif not storage.writable:
                    code = "storage-unavailable"
                else:
                    self.store.reserve(meeting_id)
                    reserved = True
            except (MeetingStoreError, OSError, ValueError):
                code = "storage-unavailable"

            def completed() -> None:
                if attempt != self._attempt:
                    return
                self._reserved = reserved
                if code:
                    self._error(code)
                    if self.state == "preparing":
                        self.state = "idle"
                        self._prepare_deadline = None
                        self.meeting = None
                    return
                if self.state != "preparing" or self._exit_confirmed:
                    return
                assert storage is not None and self.meeting is not None
                self.meeting.warnings.storage_network = storage.network
                self.warnings = [WARNING_TEXT["storage-network"]] if storage.network else []
                if self.lock_signal_unavailable:
                    self.warnings.append(WARNING_TEXT["lock-unavailable"])
                self._prepare_if_ready()

            return completed

        self._submit_filesystem(preflight)
        return not bool(self.error_code)

    def _prepare_if_ready(self) -> None:
        if (
            self._prepare_sent
            or not self.supervisor.ready
            or self.meeting is None
            or not self._reserved
            or self.state != "preparing"
            or self._exit_confirmed
        ):
            return
        if self._prepare_deadline is not None and self.clock() >= self._prepare_deadline:
            self._error("audio-unavailable")
            self._escalate()
            return
        snapshot = self.session()
        if not self._allowed(snapshot) or snapshot.blocked_epoch != self._click_epoch:
            self._error("session-blocked")
            self.stop("lock")
            return
        self._prepare_sent = True
        self._send(
            "prepare",
            meeting_id=self.meeting.id,
            meeting_generation=self.meeting.meeting_generation,
            dir=str(self.store.root / self.meeting.id),
            far=self._far,
            segment_s=300,
            fsync_s=10,
            segment_index_from=0,
        )

    def _freeze(self) -> None:
        self._recorded_ms = self.recorded_ms
        self._active_at = None

    def pause(self, reason: str = "user") -> None:
        if self.state not in {"recording", "opening", "paused"} or reason in self.pause_reasons:
            return
        self._freeze()
        self.pause_reasons.add(reason)
        if reason in {"lock", "unknown"}:
            self._epoch_pending = True
        if reason not in {"lease", "indicators"}:
            self.manual_resume_required = True
        self._pause_at = self._pause_at or self.clock()
        self.state = "paused"
        self._close_deadline = self._close_deadline or self.clock() + 1
        self._finalizing = True
        self._send("pause", **self._identity, reason=reason)
        self._persist(MeetingState.PAUSED)

    def resume(self) -> bool:
        if (
            self.state == "paused"
            and self._pause_at is not None
            and self.clock() - self._pause_at > 3600
        ):
            self.stop()
            return False
        if (
            self.state != "paused"
            or self._close_deadline is not None
            or self._finalizing
            or self._epoch_pending
            or not self._allowed()
        ):
            return self._error("session-blocked")
        self.state = "opening"
        self._physically_closed = False
        self._finalized_snapshot = None
        self._send("resume", **self._admission())
        return True

    def extend(self) -> bool:
        if (
            self.meeting is None
            or self.state not in {"recording", "paused"}
            or not self._allowed()
            or self.meeting.limit_ms >= MEETING_HARD_LIMIT_MS
        ):
            return False
        self.meeting.limit_ms = min(MEETING_HARD_LIMIT_MS, self.meeting.limit_ms + 3600000)
        self._send("extend", **self._admission(), max_ms=self.meeting.limit_ms)
        self.meeting.marks.append(Mark(self.recorded_ms, "limit_extended"))
        self.limit_warning = False
        self._persist()
        return True

    def stop(self, reason: str = "stop") -> None:
        if self.state not in {"preparing", "opening", "recording", "paused"}:
            return
        self._freeze()
        self._terminal = True
        self.state = "stopping"
        self._close_deadline = self._close_deadline or self.clock() + 1
        self._prepare_deadline = None
        if self.meeting is not None and self._committed:
            self.meeting.marks.append(Mark(self.recorded_ms, "pause", reason))
        if not self.supervisor.ready:
            # A role awaiting hello cannot receive shutdown. Retire it and
            # retain its reservation until confirmed exit and queued I/O finish.
            self._escalate()
            return
        if self._identity:
            self._send("stop", **self._identity)
        else:
            self._send("shutdown")

    def on_session_changed(self) -> None:
        if self.state not in {"preparing", "opening", "recording", "paused"}:
            return
        snapshot = self.session()
        if snapshot.preparing_for_sleep or (snapshot.known and not snapshot.active):
            self.stop("sleep" if snapshot.preparing_for_sleep else "session")
        elif self.session_kind != SessionKind.FLY and not self._allowed():
            if self.state == "preparing":
                self.stop("lock")
            else:
                self.pause("unknown" if not snapshot.known else "lock")

    def tick(self) -> None:
        self.supervisor.pump()
        self._drain_filesystem()
        self._drain_writes()
        now = self.clock()
        if (
            self.state in {"closing-unknown", "stopped", "stopping"}
            and not self._exit_confirmed
            and self.supervisor.exited
        ):
            self.on_process_exit(3)
        self.on_session_changed()
        if self.state == "preparing":
            if self._prepare_deadline is not None and now >= self._prepare_deadline:
                self._error("audio-unavailable")
                self._escalate()
            else:
                self._prepare_if_ready()
        if self._close_deadline is not None and now >= self._close_deadline:
            self._escalate()
        if self.state not in {"recording", "paused", "opening"}:
            return
        if now - self._last_keepalive >= 1 and self._close_deadline is None:
            self._last_keepalive = now
            self._sequence += 1
            self._send("keepalive", **self._identity, seq=self._sequence)
        if self.meeting is not None:
            self.limit_warning = self.recorded_ms >= self.meeting.limit_ms - 300000
            if self.recorded_ms >= self.meeting.limit_ms:
                self.stop("limit")
                return
        if self._pause_at is not None and now - self._pause_at > 3600:
            self.stop("stop")
            return
        if now - self._last_disk >= 10 and not self._disk_pending:
            self._last_disk = now
            self._disk_pending = True
            attempt = self._attempt

            def probe() -> Callable[[], None]:
                try:
                    low = self.storage_probe(self.store.root).free_mb < 200
                except (OSError, ValueError):
                    low = True

                def completed() -> None:
                    self._disk_pending = False
                    if attempt == self._attempt and low:
                        self.stop("disk")

                return completed

            self._submit_filesystem(probe)

    def _submit_filesystem(self, operation: Callable[[], Callable[[], None]]) -> None:
        """Run filesystem work off the control thread; publish only completed values."""
        self._filesystem_pending += 1

        def run() -> None:
            try:
                completed = operation()
            except (MeetingStoreError, OSError, ValueError):

                def completed() -> None:
                    self._error("storage-unavailable")
                    self.stop("disk")

            self._filesystem_results.put(completed)

        try:
            self._filesystem_submit(run)
        except queue.Full:
            self._filesystem_pending -= 1
            self._error("storage-unavailable")
            self.stop("disk")
        self._drain_filesystem()

    def _drain_filesystem(self) -> None:
        while not self._filesystem_results.empty():
            completed = self._filesystem_results.get_nowait()
            self._filesystem_pending -= 1
            completed()

    def _escalate(self) -> None:
        self._freeze()
        self._terminal = True
        self.state = "closing-unknown"
        self._close_deadline = self._prepare_deadline = None
        self.supervisor.kill()

    def attach_supervisor(self, supervisor: CaptureSupervisor) -> bool:
        """Bind a replacement role only after confirmed exit; does not launch or record."""
        if not self._exit_confirmed or self.state != "stopped":
            return False
        self.supervisor = supervisor
        self._process_generation = supervisor.generation
        return True

    def on_process_exit(self, code: int) -> None:
        self._exit_confirmed = True
        self._freeze()
        self._prepare_deadline = self._close_deadline = None
        self.state = "stopped"
        meeting = self.meeting
        attempt = self._attempt
        if meeting is None:
            return

        def cleanup() -> Callable[[], None]:
            # Serialized behind preflight/commit, including a completion that was
            # still blocked when exit was confirmed. Never delete published data.
            published: Meeting | None = None
            error = False
            try:
                candidate = self.store.read(meeting.id, "meeting.json")
                if isinstance(candidate, Meeting):
                    published = candidate
            except MeetingStoreError:
                try:
                    self.store.discard_reserved(meeting.id)
                except MeetingStoreError:
                    error = True

            def completed() -> None:
                if attempt != self._attempt:
                    return
                self._reserved = False
                if error:
                    self._error("storage-unavailable")
                if published is not None:
                    self._committed = True
                    if not self._capture_succeeded:
                        self.meeting = published
                        self._recover_one(published)

            return completed

        self._submit_filesystem(cleanup)

    def handle_event(self, event: Message) -> None:
        if self.meeting is None or self.state in {"idle", "closing-unknown"}:
            return
        if event.get("generation", self._process_generation) != self._process_generation:
            return
        event = {key: value for key, value in event.items() if key != "generation"}
        try:
            ipc.decode(ipc.encode(event, role="meeting-capture")[4:], role="meeting-capture")
        except (ipc.FrameError, ValueError, TypeError):
            return
        if (
            event.get("meeting_id") != self.meeting.id
            or event.get("meeting_generation") != self.meeting.meeting_generation
        ):
            return
        if "req" in event and self._requests.get(event["req"]) != event.get("request_type"):
            return
        if event.get("request_type") == "meeting.capture.keepalive" and "req" in event:
            self._requests.pop(event["req"], None)
        kind = event["type"].removeprefix("meeting.capture.")
        if kind == "prepared":
            if self.state != "preparing":
                return
            self._prepare_deadline = None
            self._identity = {
                key: event[key]
                for key in ("meeting_id", "meeting_generation", "capture_id", "state_rev")
            }
            self._capture_epoch = event["blocked_epoch"]
            if not self._allowed() or self.session().blocked_epoch != self._click_epoch:
                self._error("session-blocked")
                self.stop("lock")
                return
            self.far_available = event["far_available"]
            reported_reason = event.get("far_reason", "")
            self.far_reason = (
                reported_reason
                if reported_reason
                in {"target-gone", "target-mismatch", "monitor-stream-unsupported"}
                else ""
                if self.far_available
                else "unavailable"
            )
            self.mic_only_available = True
            if self._far["kind"] != "none" and not self.far_available:
                self._error("far-unavailable")
                self.stop()
                return
            self.meeting.warnings.audio_server_remote = not event["server_local"]
            if not event["server_local"]:
                self.warnings.append(WARNING_TEXT["audio-remote"])
            snapshot = deepcopy(self.meeting)
            attempt = self._attempt

            def commit() -> Callable[[], None]:
                try:
                    self.store.commit_reserved(snapshot)
                    succeeded = True
                except MeetingStoreError:
                    succeeded = False

                def completed() -> None:
                    if attempt != self._attempt:
                        return
                    if succeeded:
                        self._reserved, self._committed = False, True
                    if self.state != "preparing" or self._exit_confirmed:
                        return
                    if not succeeded:
                        self._error("storage-unavailable")
                        self.stop()
                        return
                    current = self.session()
                    if not self._allowed(current) or current.blocked_epoch != self._click_epoch:
                        self._error("session-blocked")
                        self.stop("lock")
                        return
                    self.state = "opening"
                    self._physically_closed = False
                    self._finalized_snapshot = None
                    self._send("open", **self._admission())

                return completed

            self._submit_filesystem(commit)
            return
        if kind == "error":
            if (
                event.get("request_type") == "meeting.capture.keepalive"
                and event.get("code") == "not-prepared"
            ):
                # A transition ACK can cross an already-sent heartbeat. The
                # next authoritative state updates revision; keep close deadline.
                return
            if event.get("request_type") == "meeting.capture.resume" and event.get("code") in {
                "admission-stale",
                "not-prepared",
            }:
                self.state = "paused"
                self._epoch_pending = True
                self._error("session-blocked")
                return
            self._error("audio-unavailable")
            self._escalate()
            return
        if not self._identity or event.get("capture_id") != self._identity["capture_id"]:
            return
        if event.get("state_rev", -1) < self._identity["state_rev"]:
            return
        self._identity["state_rev"] = event["state_rev"]
        self._capture_epoch = event.get("blocked_epoch", self._capture_epoch)
        if "blocked_epoch" in event:
            self._epoch_pending = False
        if kind == "started" and self.state in {"opening", "recording", "paused"}:
            if event.get("request_type", "").endswith("extend"):
                return
            if self.state != "opening":
                return
            self.state = "recording"
            self._active_at = self.clock()
            self._pause_at = None
            self.pause_reasons.clear()
            self.manual_resume_required = False
            self.tracks = {track: "live" for track in event["tracks"]}
            self.on_session_changed()
            if self.state == "recording":
                self._persist(MeetingState.RECORDING)
        elif kind in {"paused", "autopaused"}:
            if kind == "autopaused":
                self._finalizing = True
            self._freeze()
            self._recorded_ms = max(self._recorded_ms, event["at_recorded_ms"])
            self.pause_reasons.update(event["pause_reasons"])
            self.manual_resume_required |= event["manual_resume_required"]
            self._pause_at = self._pause_at or self.clock()
            if not self._terminal:
                self.state = "paused"
                self._persist(MeetingState.PAUSED)
        elif kind == "track":
            self.tracks[event["track"]] = event["state"]
        elif kind == "alive":
            if self._terminal:
                return
            self._recorded_ms = event["recorded_ms"]
            self.tracks = dict(event["tracks"])
            self._capture_epoch = event["blocked_epoch"]
            if event["state"] == "recording" and not event["manual_resume_required"]:
                self.state = "recording"
                self._physically_closed = False
                self._finalized_snapshot = None
                self._active_at = self.clock()
                self._pause_at = None
                self.pause_reasons = set(event["pause_reasons"])
                self._persist(MeetingState.RECORDING)
            elif event["state"] == "paused":
                self._active_at = None
                self.state = "paused"
                self._pause_at = self._pause_at or self.clock()
                self.pause_reasons.update(event["pause_reasons"])
                self.manual_resume_required |= event["manual_resume_required"]
                self._persist(MeetingState.PAUSED)
            elif event["state"] == "stopped":
                self._active_at = None
                self._terminal = True
                self.state = "stopped" if self._physically_closed else "stopping"
                if not self._physically_closed:
                    self._close_deadline = self._close_deadline or self.clock() + 1
                self._finish_finalized()
        elif kind == "closed":
            self._freeze()
            self._physically_closed = True
            self._close_deadline = None
            self.tracks = {track: "paused" for track in self.tracks}
            if self._terminal:
                self.state = "stopped"
        elif kind == "finalized":
            self._finalized_snapshot = event
            self._finalizing = False
            if event["status"] == "succeeded" or event["intervals"]:
                self.meeting.intervals = [Interval.from_dict(item) for item in event["intervals"]]
            self.meeting.tracks.mic.segments = event["segments"]["mic"]
            self.meeting.tracks.far.segments = event["segments"]["far"]
            if self._terminal:
                self._finish_finalized()
            else:
                self._persist()

    def _finish_finalized(self) -> None:
        if self._finalized_snapshot is None or self._capture_finalized:
            return
        self._capture_finalized = True
        success = self._finalized_snapshot["status"] == "succeeded"
        self._capture_succeeded = success
        self._send("shutdown")
        self._persist(MeetingState.RECORDED if success else MeetingState.INTERRUPTED, final=success)

    def _persist(self, state: MeetingState | None = None, *, final: bool = False) -> None:
        if self.meeting is None or not self._committed:
            return
        if state is not None:
            self.meeting.state = state
        self.meeting.recorded_ms = self.recorded_ms
        if self.meeting.state in {MeetingState.RECORDED, MeetingState.INTERRUPTED}:
            self.meeting.ended_at = datetime.now(UTC).isoformat()
        self._enqueue_metadata(self.meeting, final=final)

    def _enqueue_metadata(self, meeting: Meeting, *, final: bool = False) -> None:
        snapshot = deepcopy(meeting)

        def write() -> None:
            try:
                self.store.write(snapshot.id, "meeting.json", snapshot, writer=Writer.GUI)
            except (MeetingStoreError, OSError):
                self._write_results.put((snapshot.id, False, final))
            else:
                self._write_results.put((snapshot.id, True, final))

        try:
            self._metadata_submit(write)
        except queue.Full:
            self._error("storage-unavailable")
            self.stop("disk")
        self._drain_writes()

    def _drain_writes(self) -> None:
        while not self._write_results.empty():
            meeting_id, succeeded, final = self._write_results.get_nowait()
            if self.meeting is None or self.meeting.id != meeting_id:
                continue
            if not succeeded:
                self._error("storage-unavailable")
                self.saved = False
                self.stop("disk")
            elif final:
                self.saved = True

    def recover(self, live_capture: Callable[[Meeting], bool] = lambda meeting: False) -> None:
        def scan() -> Callable[[], None]:
            try:
                meetings = self.store.list()
            except MeetingStoreError:
                meetings = []

            def completed() -> None:
                for meeting in meetings:
                    if meeting.state in {
                        MeetingState.RECORDING,
                        MeetingState.PAUSED,
                        MeetingState.INTERRUPTED,
                    } and not live_capture(meeting):
                        self._recover_one(meeting)

            return completed

        self._submit_filesystem(scan)

    def _recover_one(self, meeting: Meeting | None) -> None:
        if meeting is None:
            return
        snapshot = deepcopy(meeting)

        def recover() -> Callable[[], None]:
            self._recover_durable(snapshot)

            def completed() -> None:
                if self.meeting is not None and self.meeting.id == snapshot.id:
                    self.meeting = snapshot
                    self._recorded_ms = snapshot.recorded_ms
                    self._active_at = None
                self._enqueue_metadata(snapshot)
                if snapshot.id not in self.interrupted_dialog_ids:
                    self.interrupted_dialog_ids.append(snapshot.id)

            return completed

        self._submit_filesystem(recover)

    def _recover_durable(self, meeting: Meeting) -> None:
        intervals: list[Interval] = []
        marks: list[Mark] = []
        opened: Message | None = None
        try:
            journal = self.store.read(meeting.id, "capture.json")
        except (MeetingStoreError, FileNotFoundError):
            journal = None
        if (
            isinstance(journal, CaptureJournal)
            and journal.meeting_generation == meeting.meeting_generation
        ):
            for edge in journal.boundaries:
                if edge["kind"] == "open":
                    opened = edge
                elif edge["kind"] == "close" and opened is not None:
                    intervals.extend(
                        [
                            Interval(opened["t_mono_ms"], opened["recorded_ms"]),
                            Interval(edge["t_mono_ms"], edge["recorded_ms"]),
                        ]
                    )
                    opened = None
                if edge["kind"] == "close":
                    reason = edge["reason"]
                    allowed = Mark.SCHEMA["properties"]["reason"]["anyOf"][0]["enum"]
                    marks.append(
                        Mark(
                            edge["recorded_ms"],
                            "pause",
                            reason if reason in allowed else "audio_failed",
                        )
                    )
                elif edge["kind"] in {"far_ended", "mic_gap_start", "mic_gap_end"}:
                    marks.append(Mark(edge["recorded_ms"], edge["kind"]))
                elif edge["kind"] == "pause_state":
                    for reason in edge.get("pause_reasons", []):
                        mark = Mark(edge["recorded_ms"], "pause", reason)
                        if mark not in marks:
                            marks.append(mark)
        # Use each durable epoch range independently, never one max-tail span.
        ranges: list[tuple[int, int, int, int]] = []
        try:
            with private_directory(self.store.root / meeting.id / "audio") as audio:
                names = sorted(
                    name
                    for name in os.listdir(audio)
                    if re.fullmatch(r"(?:mic|far)-[0-9]{4}\.json", name)
                )
        except (OSError, ValueError):
            names = []
        for name in names:
            try:
                side = self.store.read(meeting.id, "audio/" + name)
            except (MeetingStoreError, FileNotFoundError):
                marks.append(Mark(0, "damaged"))
                continue
            if (
                not isinstance(side, SegmentSidecar)
                or side.meeting_generation != meeting.meeting_generation
                or (isinstance(journal, CaptureJournal) and side.capture_id != journal.capture_id)
            ):
                marks.append(Mark(0, "damaged"))
                continue
            durable_end = side.sample_start + side.frames_at_checkpoint
            points = [(sample, mono) for sample, mono in side.timeline if sample <= durable_end]
            if len(points) < 2:
                continue
            start, end = points[0][1], points[-1][1]
            rec = side.recorded_ms_at_open
            ranges.append(
                (start, end, rec, rec + (points[-1][0] - points[0][0]) * 1000 // side.rate)
            )
            if side.discontinuity:
                marks.append(Mark(rec, "damaged"))
        if ranges:
            intervals = []
            for start, end, rec, rec_end in sorted(ranges):
                if intervals and start < intervals[-1].t_mono_ms:
                    if end <= intervals[-1].t_mono_ms:
                        continue
                    rec += intervals[-1].t_mono_ms - start
                    start = intervals[-1].t_mono_ms
                if intervals:
                    rec = max(rec, intervals[-1].recorded_ms)
                rec_end = min(MEETING_HARD_LIMIT_MS, rec + end - start)
                intervals.extend([Interval(start, rec), Interval(end, rec_end)])
        tail = max((item.recorded_ms for item in intervals), default=0)
        meeting.intervals = intervals
        meeting.marks.extend(mark for mark in marks if mark not in meeting.marks)
        meeting.recorded_ms = tail
        meeting.state = MeetingState.INTERRUPTED
        meeting.ended_at = datetime.now(UTC).isoformat()


class MeetingBridge:
    """Button entry points for the step 3b Qt/QML adapter; no automatic starts."""

    def __init__(self, controller: MeetingController) -> None:
        self.controller = controller

    def on_start_clicked(self, far: Message | None = None) -> bool:
        return self.controller.start(far)

    def on_resume_clicked(self) -> bool:
        return self.controller.resume()

    def on_extend_clicked(self) -> bool:
        return self.controller.extend()

    def on_pause_clicked(self) -> None:
        self.controller.pause()

    def on_stop_clicked(self) -> None:
        self.controller.stop()
