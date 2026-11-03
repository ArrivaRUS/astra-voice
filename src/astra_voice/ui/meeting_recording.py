"""GUI adapter for M2's explicitly enabled capture harness.

Constructors do not start capture, a session subscription or audio inspection.
Filesystem and read-only audio preview work publish plain values to the GUI.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, cast
from uuid import uuid4

from PyQt5.QtCore import QObject, Qt, QTimer, pyqtSignal

from astra_voice.core.childenv import clean_env as host_environment
from astra_voice.core.command_mode import SessionSnapshot
from astra_voice.core.meeting_jobs import JobRole, MeetingJobs
from astra_voice.core.meeting_session import (
    ERROR_TEXT,
    WARNING_TEXT,
    CaptureSupervisor,
    MeetingController,
    StorageStatus,
    probe_storage,
)
from astra_voice.core.meeting_session import (
    MeetingBridge as ClickBridge,
)
from astra_voice.meetings.events import MeetingState
from astra_voice.meetings.model import Meeting
from astra_voice.meetings.store import MeetingStore, MeetingStoreError, new_meeting_id
from astra_voice.platform.session import SessionKind
from astra_voice.platform.session_state import SessionMonitor
from astra_voice.ui.meeting_indicator_guard import MeetingIndicatorGuard
from astra_voice.ui.meeting_texts import W2_TEXTS, W4_TEXTS
from astra_voice.worker import ipc
from astra_voice.worker.supervisor import WorkerSupervisor

Message = dict[str, Any]
DEV_ENV = "ASTRA_VOICE_MEETING_CAPTURE_DEV"
EXIT_QUEUE_WRITE_S = 1.0  # floor for the exit-time «transcribing» write (one meeting.json)
log = logging.getLogger(__name__)


class Role(CaptureSupervisor, Protocol):
    def start(self) -> None: ...


@dataclass(frozen=True)
class AudioHints:
    remote: bool = False
    speakers: bool = False


def _submit(work: Callable[[], None]) -> None:
    threading.Thread(target=work, name="meeting-preview", daemon=True).start()


def probe_audio() -> AudioHints:
    """Read-only pactl metadata; no PCM stream, graph change or raw diagnostics."""
    environment = host_environment(keep_pulse_config=True)
    executable = shutil.which("pactl", path=environment.get("PATH"))
    if executable is None:
        raise OSError("meeting-preview-unavailable")
    command = [
        sys.executable,
        "-I",
        "-S",
        "-B",
        str(Path(__file__).resolve().parents[1] / "worker" / "meeting_session_helper.py"),
        str(os.getpid()),
        str(Path(executable).absolute()),
    ]

    def read(arguments: list[str]) -> Any:
        result = subprocess.run(
            [*command, "-f", "json", *arguments],
            env=environment,
            capture_output=True,
            timeout=0.75,
            check=True,
        )
        if len(result.stdout) > 1048576:
            raise ValueError("meeting-preview-invalid")
        return json.loads(result.stdout)

    info = read(["info"])
    if not isinstance(info, dict):
        raise ValueError("meeting-preview-invalid")
    server = info.get("server_string", "")
    remote = isinstance(server, str) and bool(server) and not server.startswith(("unix:", "/"))
    sinks = read(["list", "sinks"])
    speakers = False
    if isinstance(sinks, list):
        for sink in sinks:
            if isinstance(sink, dict) and sink.get("name") == info.get("default_sink_name"):
                port = sink.get("active_port", "")
                if isinstance(port, dict):
                    port = port.get("name", "")
                speakers = isinstance(port, str) and "speaker" in port
    return AudioHints(remote=remote, speakers=speakers)


def _supervisor(event: Callable[[Message], None], exited: Callable[[int], None]) -> Role:
    return WorkerSupervisor(event, role="meeting-capture", auto_restart=False, on_exit=exited)


def transcribe_supervisor(
    event: Callable[[Message], None], exited: Callable[[int], None]
) -> JobRole:
    """Role meeting-transcribe: supervisor restarts it (<= 3 / 10 min), no replay."""
    return WorkerSupervisor(event, role="meeting-transcribe", on_exit=exited)


class MeetingRecording(QObject):
    changed = pyqtSignal()
    openMeetingsRequested = pyqtSignal()
    participantsRequested = pyqtSignal()

    def __init__(
        self,
        store: MeetingStore,
        *,
        enabled: Callable[[], bool],
        policy_allowed: Callable[[], bool],
        session_kind: SessionKind,
        dev_enabled: bool | None = None,
        session_factory: Callable[[], Any] = SessionMonitor,
        owns_session: bool = True,
        supervisor_factory: Callable[
            [Callable[[Message], None], Callable[[int], None]], Role
        ] = _supervisor,
        clock: Callable[[], float] = time.monotonic,
        storage_probe: Callable[[Path], StorageStatus] = probe_storage,
        audio_probe: Callable[[], AudioHints] = probe_audio,
        microphone: Callable[[], str | None] = lambda: None,
        submit: Callable[[Callable[[], None]], None] | None = None,
        id_factory: Callable[[], str] = lambda: new_meeting_id(datetime.now(UTC)),
        exit_stop_timeout: float = 3.0,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.store, self.enabled, self.policy_allowed = store, enabled, policy_allowed
        self._exit_stop_timeout = exit_stop_timeout
        self.session_kind, self.clock = session_kind, clock
        self.dev_enabled = os.environ.get(DEV_ENV) == "1" if dev_enabled is None else dev_enabled
        self.session_factory, self.owns_session = session_factory, owns_session
        self._monitor: Any = None
        self._supervisor_factory = supervisor_factory
        self._role_serial = 0
        self._role_started = False
        self._query_req = ""
        self._closed = False
        self.dialog_open = False
        self.selected_source = "room"
        self._selection_touched = False
        self.target: Message | None = None
        self.app_class = ""
        self._call_id = ""
        self.no_app_split = False
        self._preview_ready = False
        self._preview_serial = 0
        self._preview_error = ""
        self._storage = StorageStatus(0, False, False)
        self._audio = AudioHints()
        self._accepted_remote = self._accepted_network = False
        self._audio_probe, self._storage_probe = audio_probe, storage_probe
        self._submit = submit or _submit
        self._results: queue.SimpleQueue[tuple[str, Any]] = queue.SimpleQueue()
        self._interrupted: list[Meeting] = []
        self._recovery_pending: set[str] = set()
        self._recovery_seen: set[str] = set()
        self.recovered: dict[str, Meeting] = {}
        self._signature = ""
        self._overlay_identity = ""
        self._overlay_far_state = ""
        self._overlay_far_deadline = 0.0
        self._overlay_error_deadline = 0.0
        self._overlay_error_seen = False
        self._overlay_recovery_key: tuple[str, int] | None = None
        self._overlay_recovery_deadline = 0.0
        self._overlay_recovery_text = ""
        self._overlay_recovery_reason = ""
        self._overlay_fallback_text = ""
        self._overlay_fallback_deadline = 0.0
        self._notice_app = ""
        self._indicator_guard: MeetingIndicatorGuard | None = None
        self.jobs: MeetingJobs | None = None
        self._jobs_saved = ""
        self._jobs_recover_pending = False  # initialize() asked, policy not yet allowed
        role = self._new_supervisor()
        self.controller = MeetingController(
            store,
            role,
            session=self._snapshot,
            session_kind=session_kind,
            clock=clock,
            storage_probe=storage_probe,
            enabled=lambda: self.dev_enabled and enabled(),
            policy_allowed=policy_allowed,
            id_factory=id_factory,
            metadata_submit=submit,
            filesystem_submit=submit,
            mic_device=microphone,
        )
        self.clicks = ClickBridge(self.controller)
        self._lease_timer = QTimer(self)
        self._lease_timer.setInterval(1000)
        self._lease_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._lease_timer.timeout.connect(self.tick)
        self._control_timer = QTimer(self)
        self._control_timer.setInterval(50)
        self._control_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._control_timer.timeout.connect(self.tick)

    def bind_indicators(
        self, *, overlay_visible: Callable[[], bool], tray_visible: Callable[[], bool]
    ) -> None:
        if self._closed:
            return
        if self._indicator_guard is not None:
            self._indicator_guard.shutdown()
            self._indicator_guard.deleteLater()
        self._indicator_guard = MeetingIndicatorGuard(
            active=lambda: bool(self.overlay["active"]),
            overlay_visible=overlay_visible,
            tray_visible=tray_visible,
            set_visible=self.controller.set_indicators_visible,
            pause=lambda: self.controller.pause("indicators"),
            capturing=lambda: self.controller.capturing_without_indicator_pause,
            identity=lambda: str((self.controller._identity or {}).get("capture_id", "")),
            clock=self.clock,
            parent=self,
        )

    def unbind_indicators(self) -> None:
        if self._indicator_guard is not None:
            self._indicator_guard.shutdown()
            self._indicator_guard = None

    def _new_supervisor(self) -> Role:
        self._role_serial += 1
        serial = self._role_serial

        def event(message: Message) -> None:
            if serial == self._role_serial and not self._closed:
                self._event(message)

        def exited(code: int) -> None:
            if serial == self._role_serial and not self._closed:
                self._exit(code)

        self._role_started = False
        self._standby_role = True
        self._retire_deadline: float | None = None
        self._retire_sent = False
        self._retire_killed = False
        self._query_req = ""
        return self._supervisor_factory(event, exited)

    def bind_jobs(self, jobs: MeetingJobs) -> None:
        """Attach the transcription queue (E13.M4); it resumes on initialize()."""
        if self._closed or self.jobs is not None:
            return
        self.jobs = jobs
        meeting = self.controller.meeting
        # A meeting saved before binding belongs to the previous program run.
        self._jobs_saved = meeting.id if meeting is not None and self.controller.saved else ""

    def jobs_action(self, meeting_id: str, action: str) -> bool:
        """List actions of the transcription queue; False when not ours."""
        jobs = self.jobs
        if jobs is None or self._closed:
            return False
        if action in {"Расшифровать", "Повторить"}:
            if not self._jobs_allowed():
                return True
            jobs.transcribe(meeting_id)
        elif action == "Отменить":
            jobs.cancel(meeting_id)
        else:
            return False
        self._publish()
        return True

    def _jobs_allowed(self) -> bool:
        """One gate for every new or resumed job (I3): manual, «сразу» and restart."""
        return bool(self.enabled() and self.policy_allowed())

    def _recover_jobs(self) -> None:
        """Resume unfinished jobs once after initialize(), and only while meetings are
        allowed: under a policy ban the meetings stay as they are (nothing is resumed
        or removed); a later tick resumes them when the ban is lifted."""
        if self.jobs is None or not self._jobs_recover_pending or not self._jobs_allowed():
            return
        self._jobs_recover_pending = False
        self.jobs.recover()

    @property
    def transcription_percent(self) -> int | None:
        return self.jobs.percent if self.jobs is not None else None

    def _snapshot(self) -> SessionSnapshot:
        return self._monitor.snapshot() if self._monitor is not None else SessionSnapshot()

    def initialize(self) -> None:
        if self._closed:
            return
        self.controller.recover()
        self._jobs_recover_pending = True
        self._recover_jobs()
        self._lease_timer.start()
        self._control_timer.start()
        self.tick()

    def _activate(self) -> bool:
        if self._closed or not self.dev_enabled or not self.enabled() or not self.policy_allowed():
            return False
        if self._retire_deadline is not None:
            return False
        if self._monitor is None:
            self._monitor = self.session_factory()
            self._monitor.changed.connect(self._session_changed)
            self._monitor.start()
        if self.controller.state == "stopped" and not (self._standby_role and self._role_started):
            if not self.controller.supervisor.exited or self.controller._filesystem_pending:
                return False
            role = self._new_supervisor()
            if not self.controller.attach_supervisor(role):
                return False
        if not self._role_started:
            try:
                cast(Role, self.controller.supervisor).start()
                if self.controller.supervisor.exited and not self.controller.supervisor.ready:
                    self._preview_error = "audio-unavailable"
                    self.controller.error_code = "audio-unavailable"
                    self.controller.on_process_exit(3)
                    return False
                self._role_started = True
            except (OSError, RuntimeError):
                self._preview_error = "audio-unavailable"
                self.controller.error_code = "audio-unavailable"
                self.controller.on_process_exit(3)
                return False
        self._lease_timer.start()
        self._control_timer.start()
        return True

    def open_dialog(self) -> None:
        if self._closed or not self.dev_enabled or not self.enabled() or not self.policy_allowed():
            return
        if self.busy:
            self._preview_error = "busy"
            self.dialog_open = True
            self._publish()
            return
        self.dialog_open = True
        self.selected_source = "room"
        self._selection_touched = False
        self._preview_error = ""
        self.controller.error_code = ""
        self._preview_ready = False
        self._activate()
        self._preview()
        self.tick()

    def _preview(self) -> None:
        self._preview_serial += 1
        serial = self._preview_serial
        self._preview_ready = False

        def work() -> None:
            error = ""
            storage, audio = StorageStatus(0, False, False), AudioHints()
            try:
                storage = self._storage_probe(self.store.root)
                if storage.free_mb < 600:
                    error = "disk-low"
                elif not storage.writable:
                    error = "storage-unavailable"
            except (OSError, ValueError):
                error = "storage-unavailable"
            if not error:
                try:
                    audio = self._audio_probe()
                except (OSError, ValueError, subprocess.SubprocessError):
                    error = "audio-unavailable"
            self._results.put(("preview", (serial, storage, audio, error)))

        self._submit(work)

    @property
    def busy(self) -> bool:
        return self.controller.state in {
            "preparing",
            "opening",
            "recording",
            "paused",
            "stopping",
            "closing-unknown",
        }

    @property
    def state(self) -> str:
        if self.controller.state in {"preparing", "opening"}:
            return "starting"
        code = self._preview_error or self.controller.error_code
        if code in {"disk-low", "storage-unavailable", "audio-unavailable", "busy"}:
            return {
                "disk-low": "error-disk",
                "storage-unavailable": "error-folder",
                "audio-unavailable": "error-audio",
                "busy": "error-busy",
            }[code]
        if self.no_app_split:
            return "no-app-split"
        if self._storage.network:
            return "warn-nfs"
        if self._audio.remote:
            return "warn-remote-audio"
        if self.selected_source == "call":
            return "browser" if self.app_class == "browser" else "call"
        return "speakers" if self._audio.speakers else "default"

    @property
    def error(self) -> str:
        code = self._preview_error or self.controller.error_code
        return ERROR_TEXT.get(code, "")

    @property
    def warnings(self) -> list[str]:
        result = []
        if self._storage.network:
            result.append(WARNING_TEXT["storage-network"])
        if self._audio.remote:
            result.append(WARNING_TEXT["audio-remote"])
        return result

    @property
    def can_start(self) -> bool:
        return (
            self.dialog_open
            and self.dev_enabled
            and self.enabled()
            and self.policy_allowed()
            and self._preview_ready
            and not self.error
            and not self.busy
            and self._retire_deadline is None
            and (
                self.controller.supervisor.ready
                or self.controller.state == "stopped"
                and self.controller.supervisor.exited
                and not self.controller._filesystem_pending
            )
            and (self.session_kind == SessionKind.FLY or self._snapshot().allowed)
            and (
                self.selected_source != "call" or self.target is not None and not self.no_app_split
            )
        )

    @property
    def options(self) -> list[Message]:
        rows = []
        if self.target is not None:
            browser = self.app_class == "browser"
            rows.append(
                {
                    "id": "call",
                    "title": W4_TEXTS["browserTitle" if browser else "callTitle"],
                    "caption": W4_TEXTS["browserCaption" if browser else "callCaption"],
                    "enabled": not self.no_app_split,
                    "warning": W4_TEXTS["noAppSplit"] if self.no_app_split else "",
                }
            )
        rows.extend(
            [
                {
                    "id": "room",
                    "title": W4_TEXTS["roomTitle"],
                    "caption": W4_TEXTS["roomCaption"],
                    "enabled": True,
                    "warning": "",
                },
                {
                    "id": "all",
                    "title": W4_TEXTS["allTitle"],
                    "caption": W4_TEXTS["allCaption"],
                    "enabled": True,
                    "warning": "",
                },
            ]
        )
        return rows

    def select(self, source: str) -> None:
        if not self.busy and source in {row["id"] for row in self.options if row["enabled"]}:
            self.selected_source = source
            self._selection_touched = True
            self._publish()

    def begin_clicked(self) -> None:
        if not self.can_start:
            return
        if self.controller.state == "stopped" and not self._activate():
            return
        self._accepted_remote, self._accepted_network = self._audio.remote, self._storage.network
        far: Message = {"kind": "none"}
        if self.selected_source == "all":
            far = {"kind": "sink_monitor"}
        elif self.selected_source == "call" and self.target is not None:
            far = {
                "kind": "app_stream",
                "sink_input": self.target["sink_input"],
                "object_serial": self.target["object_serial"],
                "call_id": self._call_id,
            }
        self._notice_app = self.app_class if far["kind"] == "app_stream" else ""
        if self.clicks.on_start_clicked(far):
            # A consumed role must exit before replacement; an already launched
            # fresh query-only role can accept its first explicit start click.
            self._standby_role = False
        self._publish()

    def close_dialog(self) -> None:
        self.dialog_open = False
        self._query_req = ""
        self._preview_serial += 1
        if self.controller.state in {"preparing", "opening"}:
            self.clicks.on_stop_clicked()
        elif not self.busy:
            self._retire_unused_role()
        self._publish()

    def _release_session(self) -> None:
        monitor, self._monitor = self._monitor, None
        if monitor is not None:
            monitor.changed.disconnect(self._session_changed)
            if self.owns_session:
                monitor.close()

    def _retire_unused_role(self) -> None:
        self._release_session()
        if self._role_started and self._retire_deadline is None:
            # One deadline covers both delayed hello and shutdown. Repeated
            # close/policy notifications must never prolong resource ownership.
            self._retire_deadline = self.clock() + 1
        self._poll_retirement()

    def _poll_retirement(self) -> None:
        if self._retire_deadline is None:
            return
        role = self.controller.supervisor
        if role.exited:
            # kill() deliberately has no exit callback: poll is authoritative.
            if not self.controller._exit_confirmed or self.controller.state != "stopped":
                self.controller.on_process_exit(0)
            self._role_started = False
            self._retire_deadline = None
            if self.dialog_open:
                self._activate()
            return
        if self._retire_killed:
            return
        if self.clock() >= self._retire_deadline:
            self._retire_killed = True
            role.kill()
        elif role.ready and not self._retire_sent:
            self._retire_sent = True
            self._send_query("meeting.capture.shutdown")

    def retry(self) -> None:
        if self.busy:
            return
        self._preview_error = self.controller.error_code = ""
        self._activate()
        self._preview()
        self.tick()

    def action(self, meeting_id: str, action: str) -> None:
        current = self.controller.meeting
        if current is None or current.id != meeting_id:
            return
        if action == "Остановить":
            self.clicks.on_stop_clicked()
        elif action == "Продолжить" and self.dev_enabled and self.enabled():
            self.clicks.on_resume_clicked()
        self._publish()

    @staticmethod
    def _duration(milliseconds: int) -> str:
        seconds = max(0, milliseconds // 1000)
        return f"{seconds // 3600}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"

    @property
    def overlay(self) -> Message:
        """Presentation of controller-published values, never a capture authority."""
        controller = self.controller
        meeting = controller.meeting
        state = controller.state
        visible = bool(
            self.dev_enabled
            and controller._committed
            and meeting is not None
            and (
                state in {"recording", "paused", "stopping", "closing-unknown"}
                or state == "opening"
                and bool(controller.pause_reasons)
            )
        )
        secondary = ""
        action = ""
        remaining_ms = max(0, meeting.limit_ms - controller.recorded_ms) if meeting else 0
        can_extend = bool(
            meeting and meeting.limit_ms < 14400000 and state in {"recording", "paused"}
        )
        can_resume = bool(
            state == "paused"
            and not controller._finalizing
            and controller._close_deadline is None
            and not controller._epoch_pending
            and controller._allowed()
            and self.enabled()
            and self.policy_allowed()
        )
        active = visible
        if state == "opening" and controller.pause_reasons:
            state = "paused"
        if state in {"stopping", "closing-unknown"}:
            state = "stopping"
        elif state == "paused":
            if "lock" in controller.pause_reasons:
                state, secondary = "paused-lock", W2_TEXTS["pausedLock"]
            elif "unknown" in controller.pause_reasons:
                secondary = W2_TEXTS["pausedUnknown"]
            # spec §3.5: indicators/lease pauses have no own W2 row; plain «Пауза».
        elif state == "recording":
            if controller.tracks.get("mic") in {"lost", "paused"}:
                state, secondary = "mic-paused", W2_TEXTS["micPaused"]
            elif controller.limit_warning:
                seconds = remaining_ms // 1000
                state = "limit-soon"
                secondary = W2_TEXTS["limitSoon"].format(
                    remaining=f"{seconds // 60}:{seconds % 60:02d}"
                )
                action = "extend" if can_extend else ""
            elif self.clock() < self._overlay_recovery_deadline:
                state = self._overlay_recovery_reason
                secondary = self._overlay_recovery_text
            elif self.clock() < self._overlay_far_deadline:
                state, secondary = "app-closed", W2_TEXTS["farEnded"]
            elif self.clock() < self._overlay_fallback_deadline:
                # S17-A4: the notification service did not take the notice.
                state, secondary = "notify-fallback", self._overlay_fallback_text
        if self.dev_enabled and self.clock() < self._overlay_error_deadline:
            visible, state, secondary = True, "error", W2_TEXTS["audioError"]
        pause_minutes = (
            max(0, int((self.clock() - controller._pause_at) // 60))
            if controller._pause_at is not None
            else 0
        )
        return {
            "visible": visible,
            "active": active,
            "state": state if visible else "hidden",
            "elapsed": self._duration(controller.recorded_ms),
            "elapsedSeconds": controller.recorded_ms // 1000,
            "secondary": secondary,
            "secondaryAction": action,
            "canExtend": can_extend,
            "canResume": can_resume,
            "microphoneName": "",  # M2 publishes no replacement candidate or acceptance command.
            "pausedMinutes": pause_minutes,
            "timerTooltip": W2_TEXTS["pausedTimer"].format(minutes=pause_minutes),
            "limitSoon": controller.limit_warning,
            "limitRemainingSeconds": remaining_ms // 1000,
            "paused": controller.state == "paused"
            or controller.state == "opening"
            and bool(controller.pause_reasons),
        }

    def overlay_action(self, action: str) -> None:
        if self._closed or not self.dev_enabled or not self.overlay["visible"]:
            return
        if action == "stop":
            self.clicks.on_stop_clicked()
        elif action == "participants":
            self.participantsRequested.emit()
        elif self.enabled() and self.policy_allowed():
            if action == "pause":
                self.clicks.on_pause_clicked()
            elif action == "resume" and self.overlay["canResume"]:
                self.clicks.on_resume_clicked()
            elif action == "extend":
                self.clicks.on_extend_clicked()
        self._publish()

    def _session_changed(self, snapshot: object = None) -> None:
        if not self._closed:
            self.controller.on_session_changed()
            self._publish()

    def policy_changed(self) -> None:
        if not self.enabled() or not self.policy_allowed():
            self.close_dialog()
            if self.busy:
                self.clicks.on_stop_clicked()
        self._publish()

    def _send_query(self, kind: str) -> None:
        request = uuid4().hex
        if kind == "meeting.call.query":
            self._query_req = request
        self.controller.supervisor.send({"type": kind, "request_type": kind, "req": request})

    def _event(self, event: Message) -> None:
        if event.get("generation") != self.controller.supervisor.generation:
            return
        raw = {key: value for key, value in event.items() if key != "generation"}
        try:
            ipc.decode(ipc.encode(raw, role="meeting-capture")[4:], role="meeting-capture")
        except (ipc.FrameError, ValueError, TypeError):
            return
        if raw["type"] == "meeting.call.state":
            if (
                not self.dialog_open
                or raw.get("req") != self._query_req
                or raw.get("request_type") != "meeting.call.query"
            ):
                return
            self.target = None
            if (
                raw["active"]
                and raw.get("app_class") in {"dion", "browser"}
                and "target" in raw
                and "call_id" in raw
                and raw["target"]["verified"] in {"declared-verified", "declared"}
            ):
                self.target = raw["target"]
                self.app_class = raw["app_class"]
                self._call_id = raw["call_id"]
                if not self._selection_touched:
                    self.selected_source = "call"
            if self.target is None and self.selected_source == "call":
                self.selected_source = "room"
            self._publish()
            return
        if raw["type"] == "meeting.capture.prepared":
            # Newly discovered warnings must be visible before a further click;
            # cancel this prepared role without ever granting open.
            network = bool(
                self.controller.meeting and self.controller.meeting.warnings.storage_network
            )
            remote = not raw["server_local"]
            if (remote and not self._accepted_remote) or (network and not self._accepted_network):
                self._audio = AudioHints(remote=remote, speakers=self._audio.speakers)
                self._storage = StorageStatus(
                    self._storage.free_mb, self._storage.writable, network
                )
                self.clicks.on_stop_clicked()
            elif not self.dialog_open or not self.enabled() or not self.policy_allowed():
                self.clicks.on_stop_clicked()
        self.controller.handle_event(event)
        if self.controller.far_reason == "monitor-stream-unsupported":
            self.no_app_split = True
            self.controller.error_code = ""
        if self.controller.state in {"recording", "paused"} and self.controller._committed:
            self.dialog_open = False
        self._publish()

    def _exit(self, code: int) -> None:
        retiring = self._retire_deadline is not None
        self.controller.on_process_exit(code)
        self._role_started = False
        self._retire_deadline = None
        if not self.dialog_open:
            self._release_session()
        elif retiring:
            # W4 may have been reopened while the old role was retiring.
            # Restore only its query/preview role, never implicit capture.
            self._activate()
        self._publish()

    def tick(self) -> None:
        if self._closed:
            return
        while not self._results.empty():
            kind, value = self._results.get_nowait()
            if kind == "preview":
                serial, storage, audio, error = value
                if serial == self._preview_serial and self.dialog_open:
                    self._storage, self._audio = storage, audio
                    self._preview_error, self._preview_ready = error, True
            elif kind == "interrupted":
                mid, meeting = value
                self._recovery_pending.discard(mid)
                if meeting is not None:
                    self._recovery_seen.add(mid)
                    self.recovered[mid] = meeting
                    self._interrupted.append(meeting)
        if not self.enabled() or not self.policy_allowed():
            self.policy_changed()
        if self._indicator_guard is not None:
            self._indicator_guard.check()
        self.controller.tick()
        self._poll_retirement()
        self._tick_jobs()
        if (
            self.dialog_open
            and self._retire_deadline is None
            and self.controller.supervisor.ready
            and not self._query_req
        ):
            self._send_query("meeting.call.query")
        for mid in self.controller.interrupted_dialog_ids:
            if mid not in self._recovery_seen | self._recovery_pending:
                self._recovery_pending.add(mid)

                def read(mid: str = mid) -> None:
                    try:
                        meeting = self.store.read(mid, "meeting.json")
                    except MeetingStoreError:
                        meeting = None
                    if (
                        not isinstance(meeting, Meeting)
                        or meeting.state != MeetingState.INTERRUPTED
                    ):
                        meeting = None
                    self._results.put(("interrupted", (mid, meeting)))

                self._submit(read)
        self._publish()

    def _queue_saved(self) -> bool:
        """Queue the just saved meeting once; True when a job was requested."""
        jobs = self.jobs
        meeting = self.controller.meeting
        if (
            jobs is not None
            and meeting is not None
            and self.controller.saved
            and self.controller._committed
            and meeting.id != self._jobs_saved
        ):
            # The final RECORDED write is durable: no GUI-lease race with capture.
            self._jobs_saved = meeting.id
            if self._jobs_allowed():  # a policy ban leaves the meeting «Записана»
                return jobs.on_recorded(meeting.id)
        return False

    def _tick_jobs(self) -> None:
        if self.jobs is None:
            return
        self._recover_jobs()
        self._queue_saved()
        self.jobs.tick()

    @property
    def current_meeting(self) -> Meeting | None:
        if not self.controller._committed or self.controller.meeting is None:
            return None
        meeting = deepcopy(self.controller.meeting)
        meeting.recorded_ms = self.controller.recorded_ms
        return meeting

    @property
    def interrupted_text(self) -> str:
        if not self._interrupted:
            return ""
        seconds = self._interrupted[0].recorded_ms // 1000
        duration = f"{seconds // 3600}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"
        return f"Запись встречи прервалась. Сохранено {duration}"

    def close_interrupted(self, *, open_meetings: bool = False) -> None:
        if self._interrupted:
            self._interrupted.pop(0)
        if open_meetings:
            self.close_dialog()
            self.openMeetingsRequested.emit()
        self._publish()

    def _publish(self) -> None:
        identity = self.controller.meeting.id if self.controller.meeting else ""
        if identity != self._overlay_identity:
            self._overlay_identity = identity
            self._overlay_far_deadline = self._overlay_error_deadline = 0.0
            self._overlay_far_state = ""
            self._overlay_error_seen = False
            self._overlay_recovery_key = None
            self._overlay_recovery_deadline = 0.0
            self._overlay_recovery_text = self._overlay_recovery_reason = ""
            self._overlay_fallback_text, self._overlay_fallback_deadline = "", 0.0
        recovery = self.controller.indicator_recovery
        if recovery is not None:
            key = (recovery["capture_id"], recovery["state_rev"])
            if key != self._overlay_recovery_key:
                self._overlay_recovery_key = key
                reason = recovery["reason"]
                self._overlay_recovery_reason = (
                    "reason-indicator" if reason == "indicators" else "reason-hang"
                )
                self._overlay_recovery_text = W2_TEXTS[
                    "restoredIndicator" if reason == "indicators" else "restoredHang"
                ].format(duration=self._duration(recovery["duration_ms"]))
                self._overlay_recovery_deadline = self.clock() + 8
        far_state = self.controller.tracks.get("far", "")
        if (
            far_state == "ended"
            and self._overlay_far_state == "live"
            and self.controller.state == "recording"
        ):
            self._overlay_far_deadline = self.clock() + 8
        self._overlay_far_state = far_state
        audio_error = bool(
            self.controller._committed
            and self.controller.state == "stopped"
            and (self.controller._physically_closed or self.controller._exit_confirmed)
            and self.controller.error_code == "audio-unavailable"
        )
        if audio_error and not self._overlay_error_seen:
            self._overlay_error_deadline = self.clock() + 3
        self._overlay_error_seen = audio_error
        signature = repr(
            (
                self.overlay,
                self.controller.state,
                self.controller.recorded_ms // 1000,
                self.controller.saved,
                self.dialog_open,
                self.state,
                self.error,
                self.warnings,
                self.selected_source,
                self.target,
                self.can_start,
                self.interrupted_text,
                tuple(self.recovered),
                self.controller.manual_resume_required,
                tuple(sorted(self.controller.pause_reasons)),
                self.jobs.revision if self.jobs is not None else -1,
            )
        )
        if signature != self._signature:
            self._signature = signature
            self.changed.emit()

    @property
    def active_meeting_id(self) -> str:
        """Committed meeting shown by W2 as an active recording, else ""."""
        meeting = self.controller.meeting
        if meeting is None or not self.overlay["active"]:
            return ""
        return meeting.id

    @property
    def notice_app_class(self) -> str:
        """App class of the verified call target this recording was started with."""
        return self._notice_app

    def show_notify_fallback(self, text: str) -> None:
        """W2 second row for 8 s when a recording notice was not delivered (S17-A4)."""
        if self._closed or not self.overlay["active"]:
            return
        self._overlay_fallback_text = text
        self._overlay_fallback_deadline = self.clock() + 8
        self._publish()

    def _stop_for_exit(self, deadline: float) -> None:
        """Exit/SIGTERM during capture: the ordinary stop and M2 finalization.

        Capture closes within the controller's 1 s close deadline (or is killed);
        the final metadata write is then drained before the role is released.
        A consumed role already `stopped` after `closed` is still finalizing:
        exit waits for it too, within the same deadline.
        """
        controller = self.controller
        if not self._role_started:
            return
        finalizing = (
            controller.state == "stopped"
            and not self._standby_role
            and not controller.supervisor.exited
        )
        if not finalizing and controller.state not in {
            "preparing",
            "opening",
            "recording",
            "paused",
            "stopping",
        }:
            return
        if controller.state not in {"stopping", "stopped"}:
            self.clicks.on_stop_clicked()
        while time.monotonic() < deadline:
            controller.tick()
            if controller.state in {"stopped", "closing-unknown"} and controller.supervisor.exited:
                break
            controller.supervisor.pump(0.02)
        controller.tick()

    def shutdown(self) -> None:
        if self._closed:
            return
        self._lease_timer.stop()
        self._control_timer.stop()
        self.unbind_indicators()
        # One exit budget for the stop, finalization and the metadata drain.
        deadline = time.monotonic() + self._exit_stop_timeout
        try:
            self._stop_for_exit(deadline)
        except Exception:
            log.warning("meeting-exit-stop-failed")
        queued = False
        if self.jobs is not None:
            # Exit during a recording or right after «Стоп» (I2): the final RECORDED
            # write may still be on the writer thread or not drained into `saved`
            # (that happens on the next 50 ms tick, which never comes). Finish the
            # metadata writes, take their results, then queue the meeting durably
            # (transcribing): it is transcribed at the next start (spec §4.6).
            try:
                if not self.controller.close(timeout=max(0.0, deadline - time.monotonic())):
                    log.warning("meeting-metadata-close-timeout")
                self.controller._drain_writes()
            except Exception:
                log.warning("meeting-exit-drain-failed")
            queued = self._queue_saved()
        self._closed = True
        if self.jobs is not None:
            remaining = max(0.0, deadline - time.monotonic())
            if queued:
                # The transcribing write must land before the process ends.
                remaining = max(remaining, EXIT_QUEUE_WRITE_S)
            try:
                # T-478: no confirmation and no job.cancel; the checkpoint resumes.
                if not self.jobs.shutdown(timeout=remaining):
                    log.warning("meeting-jobs-exit-timeout")
            except Exception:
                log.warning("meeting-jobs-exit-failed")
        try:
            if self._role_started:
                # Terminal for the role; after a graceful exit it only releases IPC.
                self.controller.supervisor.kill()
            self._release_session()
        finally:
            if not self.controller.close(timeout=max(0.0, deadline - time.monotonic())):
                log.warning("meeting-metadata-close-timeout")
