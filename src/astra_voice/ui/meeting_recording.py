"""GUI adapter for M2's explicitly enabled capture harness.

Constructors do not start capture, a session subscription or audio inspection.
Filesystem and read-only audio preview work publish plain values to the GUI.
"""

from __future__ import annotations

import json
import logging
import math
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
from astra_voice.core.logging import log_meeting_offer
from astra_voice.core.meeting_jobs import JobRole, MeetingJobs
from astra_voice.core.meeting_session import (
    ERROR_TEXT,
    TARGET_REFUSALS,
    WARNING_TEXT,
    CaptureSupervisor,
    MeetingController,
    StorageStatus,
    probe_storage,
)
from astra_voice.core.meeting_session import (
    MeetingBridge as ClickBridge,
)
from astra_voice.meetings.events import (
    DETECTOR_CODE,
    START_FAILED,
    START_LATENCY,
    DetectorCode,
    DetectorPurpose,
    MeetingState,
    OfferEvent,
    OfferReason,
    StartFailureCode,
    StartFailureRequest,
    StartOrigin,
    StartOutcome,
)
from astra_voice.meetings.model import Meeting
from astra_voice.meetings.store import MeetingStore, MeetingStoreError, new_meeting_id
from astra_voice.platform.session import SessionKind
from astra_voice.platform.session_state import MonitorSnapshot, SessionMonitor
from astra_voice.ui.meeting_indicator_guard import MeetingIndicatorGuard
from astra_voice.ui.meeting_offer import OfferController, OfferView, Origin
from astra_voice.ui.meeting_texts import CALL_LOST_TEXT, W2_TEXTS, W4_TEXTS
from astra_voice.worker import ipc
from astra_voice.worker.supervisor import WorkerSupervisor

Message = dict[str, Any]
EXIT_QUEUE_WRITE_S = 1.0  # floor for the exit-time «transcribing» write (one meeting.json)
_START_FAILURES = {"disk-low", "storage-unavailable", "audio-unavailable"}
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
        detector_allowed: Callable[[], bool] = lambda: False,
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
        self._detector_allowed = detector_allowed
        self._automation_off = False
        self._observation_retry_at = 0.0
        self._observation_retry_delay = 2.0
        self._exit_stop_timeout = exit_stop_timeout
        self.session_kind, self.clock = session_kind, clock
        self.session_factory, self.owns_session = session_factory, owns_session
        self._monitor: Any = None
        self._offer_monitor: Any = None
        self._supervisor_factory = supervisor_factory
        self._role_serial = 0
        self._role_started = False
        self._query_req = ""
        self._offer_query_req = ""
        self._closed = False
        self._shutting_down = False
        self.dialog_open = False
        self.selected_source = "room"
        self._selection_touched = False
        self.call_id = ""
        self.app_class = ""
        self.variant1 = False
        self._variant1_lost = ""
        self.no_app_split = False
        self._preview_ready = False
        self._preview_serial = 0
        self._preview_error = ""
        self._storage = StorageStatus(0, False, False)
        self._audio = AudioHints()
        self._accepted_remote = self._accepted_network = False
        self._prepared_remote = self._prepared_network = False
        self._start_origin: StartOrigin | None = None
        self._start_pressed_at: int | None = None
        self._start_attempt = 0
        self._pending_call_lost = ""
        self._pending_offer_dialog = False
        self._start_remote = False
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
        self.release_files: Callable[[str, Callable[[], None]], None] = lambda mid, done: done()
        self.jobs: MeetingJobs | None = None
        self._jobs_saved = ""
        self._jobs_recover_pending = False  # initialize() asked, policy not yet allowed
        self.offer = OfferController(
            clock=clock,
            on_visibility_changed=self._offer_visibility_changed,
            on_start=self._offer_start_requested,
            on_open_meetings=self.openMeetingsRequested.emit,
            on_start_timeout=self._offer_start_timeout,
        )
        role = self._new_supervisor()
        self.controller = MeetingController(
            store,
            role,
            session=self._snapshot,
            session_kind=session_kind,
            clock=clock,
            storage_probe=storage_probe,
            enabled=enabled,
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

    def bind_offer(self, view: OfferView) -> None:
        if not self._closed:
            self._cancel_offer_start()
            self._offer_query_req = ""
            self.offer.bind(view)

    def press_offer(self, action: str, pressed_at_ms: int) -> bool:
        if self._closed or (not self.offer.starting_slot and not self._offer_eligible()):
            return False
        self._update_offer_session()
        return self.offer.press(action, pressed_at_ms)

    def dismiss_offer(self) -> None:
        if not self._closed:
            self._update_offer_session()
            self.offer.dismiss()

    def _offer_visibility_changed(self) -> None:
        if not self._closed and not self._shutting_down:
            self._sync_role_ownership()
            self._publish()

    def _offer_start_requested(self, call_id: str, app_class: str) -> None:
        self._start_pressed_at = self.offer.start_pressed_at_ms
        self._start_origin = StartOrigin.OFFER
        if not self._start_allowed():
            if not self.enabled() or not self.policy_allowed():
                code = "disabled"
            elif self.session_kind != SessionKind.FLY and not self._snapshot().allowed:
                code = "session-blocked"
            else:
                code = "busy"
            self._finish_start_error(code)
            return
        self._start_capture(
            {"kind": "app_stream", "call_id": call_id}, StartOrigin.OFFER, app_class
        )

    def _offer_start_timeout(self) -> None:
        if self._start_origin == StartOrigin.OFFER:
            self._log_start_outcome(StartOutcome.FAILED)
            self._clear_start()
            self.clicks.on_stop_clicked()

    def _new_supervisor(self) -> Role:
        self._role_serial += 1
        serial = self._role_serial
        self._offer_query_req = ""
        self.offer.new_role(serial)

        def event(message: Message) -> None:
            if serial == self._role_serial and not self._closed:
                self._event(message)
            elif serial != self._role_serial and not self._closed and self._is_offer_event(message):
                log_meeting_offer(OfferEvent.DROPPED, OfferReason.OLD_SERIAL)

        def exited(code: int) -> None:
            if serial == self._role_serial and not self._closed:
                self._exit(code)

        self._role_started = False
        self._standby_role = True
        self._retire_deadline: float | None = None
        self._retire_sent = False
        self._retire_killed = False
        self._query_req = ""
        self._reset_detector(None)
        return self._supervisor_factory(event, exited)

    def bind_jobs(self, jobs: MeetingJobs) -> None:
        """Attach the transcription queue (E13.M4); it resumes on initialize()."""
        if self._closed or self.jobs is not None:
            return
        self.jobs = jobs
        jobs.release_files = self.release_files
        jobs.admit_allowed = self._jobs_allowed  # one gate for admission (I3)
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
        """Resume unfinished jobs once when admitted; storage maintenance runs regardless."""
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
        if self.jobs is not None:
            self.jobs.start_retention()
        self.controller.recover()
        self._jobs_recover_pending = True
        self._recover_jobs()
        self._lease_timer.start()
        self._control_timer.start()
        self.tick()

    def _observation_allowed(self) -> bool:
        return (
            self.enabled()
            and self.policy_allowed()
            and self._detector_allowed()
            and not self._automation_off
        )

    def _ensure_session(self) -> None:
        if self._monitor is None:
            self._monitor = self.session_factory()
            if self._monitor is not self._offer_monitor:
                self.offer.reset_session()
                self._offer_monitor = self._monitor
            self._update_offer_session(can_query=False)
            self._monitor.changed.connect(self._session_changed)
            self._monitor.start()

    def _defer_observation(self) -> None:
        self._observation_retry_at = self.clock() + self._observation_retry_delay
        self._observation_retry_delay = min(60.0, self._observation_retry_delay * 2)

    def _activate(self, *, observation: bool = False) -> bool:
        if self._closed or not self.enabled() or not self.policy_allowed():
            return False
        if self._retire_deadline is not None:
            return False
        self._ensure_session()
        if self.controller.state == "stopped" and not (self._standby_role and self._role_started):
            if (
                not self.controller.supervisor.exited
                or not self.controller._exit_confirmed
                or self.controller._filesystem_pending
            ):
                return False
            role = self._new_supervisor()
            if not self.controller.attach_supervisor(role):
                return False
        if not self._role_started:
            role = cast(Role, self.controller.supervisor)
            try:
                role.start()
            except (OSError, RuntimeError):
                self._launch_failed(observation=observation)
                return False
            if role.exited and not role.ready:
                self._launch_failed(observation=observation)
                return False
            self._role_started = True
        self._lease_timer.start()
        self._control_timer.start()
        return True

    def _launch_failed(self, *, observation: bool) -> None:
        if self._observation_allowed():
            self._defer_observation()
        if not observation:
            self._preview_error = self.controller.error_code = "audio-unavailable"
        if not self.controller._exit_confirmed:
            self.controller.on_process_exit(3)

    def open_dialog(self) -> None:
        if self._closed or self.dialog_open or not self.enabled() or not self.policy_allowed():
            return
        if self.offer.state == "start-failed":
            self.offer.start_closed(reason=OfferReason.OPEN_DIALOG)
        self.dialog_open = True
        self._query_req = ""
        self.call_id = self.app_class = ""
        self.variant1 = False
        self._offer_query_req = ""
        self.offer.suspend(for_capture=True, reason=OfferReason.OPEN_DIALOG)
        if self.busy:
            self._preview_error = "busy"
            self._publish()
            return
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
        if self._variant1_lost:
            return "call-lost"
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
        if self._variant1_lost:
            result.append(CALL_LOST_TEXT[self._variant1_lost])
        if self._storage.network:
            result.append(WARNING_TEXT["storage-network"])
        if self._audio.remote:
            result.append(WARNING_TEXT["audio-remote"])
        return result

    @property
    def can_start(self) -> bool:
        return (
            self.dialog_open
            and self._preview_ready
            and not self.error
            and self._start_allowed()
            and (self.selected_source != "call" or self._call_available and not self.no_app_split)
        )

    def _start_allowed(self) -> bool:
        return (
            self.enabled()
            and self.policy_allowed()
            and not self.busy
            and self._retire_deadline is None
            and (
                self.controller.supervisor.ready
                or self.controller.state == "stopped"
                and self.controller.supervisor.exited
            )
            and not self.controller._filesystem_pending
            and (self.session_kind == SessionKind.FLY or self._snapshot().allowed)
        )

    @property
    def _call_available(self) -> bool:
        return (
            not self._variant1_lost
            and not self._automation_off
            and not self._prepared_remote
            and bool(self.call_id)
            and self.variant1
            and self.app_class in {"dion", "browser"}
        )

    @property
    def options(self) -> list[Message]:
        rows = []
        if self._call_available:
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
        far: Message = {"kind": "none"}
        if self.selected_source == "all":
            far = {"kind": "sink_monitor"}
        elif self.selected_source == "call" and self._call_available:
            far = {"kind": "app_stream", "call_id": self.call_id}
        self._start_pressed_at = self.offer.now_ms()
        app_class = self.app_class if far["kind"] == "app_stream" else ""
        self._start_capture(far, StartOrigin.CARD, app_class)

    def _start_capture(self, far: Message, origin: StartOrigin, app_class: str) -> None:
        self._start_origin = origin
        self._start_attempt = self.controller._attempt + 1
        if self.controller.state == "stopped" and not self._activate():
            self._finish_start_error(self.controller.error_code or "busy")
            self._publish()
            return
        if self._start_origin is None:
            return  # Session or policy changed while activating the role.
        self._accepted_remote = origin == StartOrigin.CARD and self._audio.remote
        self._accepted_network = origin == StartOrigin.CARD and self._storage.network
        self._notice_app = app_class
        accepted = self.clicks.on_start_clicked(far)
        if accepted:
            # A consumed role must exit before replacement; an already launched
            # fresh query-only role can accept its first explicit start click.
            self._standby_role = False
        elif self._start_origin is not None:
            self._finish_start_error(self.controller.error_code)
        self._publish()

    def close_dialog(self) -> None:
        self.dialog_open = False
        self._variant1_lost = ""
        self._prepared_remote = self._prepared_network = False
        if self._start_origin == StartOrigin.CARD:
            self._log_start_outcome(StartOutcome.ABORTED)
            self._clear_start()
        self._query_req = ""
        self._preview_serial += 1
        if self.controller.state in {"preparing", "opening"}:
            self.clicks.on_stop_clicked()
        self._sync_role_ownership()
        self._publish()

    def _release_session(self) -> None:
        monitor, self._monitor = self._monitor, None
        if monitor is not None:
            monitor.changed.disconnect(self._session_changed)
            if self.owns_session:
                self._offer_monitor = None
                monitor.close()

    def _sync_role_ownership(self) -> None:
        if self._closed:
            return
        observing = self._observation_allowed()
        if not observing:
            self._suspend_automation()
        elif self.dialog_open or self.busy:
            self._offer_query_req = ""
            self.offer.suspend(for_capture=True, reason=OfferReason.RECORDING)
        owned = observing or self.offer.visible or self.dialog_open or self.busy
        if owned:
            self._ensure_session()
        if (
            observing
            and not self.busy
            and not self._role_started
            and self.clock() >= self._observation_retry_at
        ):
            self._activate(observation=True)
        # Send detector.stop while the idle role can still accept commands.
        self.sync_detector()
        if not owned:
            self._release_session()
            if self._standby_role and self._role_started and self._retire_deadline is None:
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
            self._exit(0)
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
        elif action == "Продолжить" and self.enabled():
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
            controller._committed
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
            elif controller.far_partial and controller.tracks.get("far") == "live":
                state, secondary = "far-partial", W2_TEXTS["farPartial"]
        if self.clock() < self._overlay_error_deadline:
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
        if self._closed or not self.overlay["visible"]:
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
            if self.sender() is self._monitor and isinstance(snapshot, SessionSnapshot):
                self._update_offer_session(snapshot)
            else:
                self._update_offer_session()
            self.controller.on_session_changed()
            self._publish()

    def policy_changed(self) -> None:
        if not self.enabled() or not self.policy_allowed():
            self.sync_detector()
            self.close_dialog()
            if self.busy:
                self.clicks.on_stop_clicked()
            if self.jobs is not None:
                self.jobs.suspend()
        self._publish()

    def _send_query(self, kind: str) -> None:
        request = uuid4().hex
        if kind == "meeting.call.query":
            self._query_req = request
        self.controller.supervisor.send({"type": kind, "request_type": kind, "req": request})

    def _offer_eligible(self) -> bool:
        role = self.controller.supervisor
        return (
            self._observation_allowed()
            and not self.dialog_open
            and not self.busy
            and self._standby_role
            and self._role_started
            and self._retire_deadline is None
            and role.ready
            and not role.exited
            and self._detector_generation == role.generation
            and self._detector_running is True
            and self._detector_pending is None
        )

    def _update_offer_session(
        self, snapshot: SessionSnapshot | None = None, *, can_query: bool = True
    ) -> SessionSnapshot:
        if snapshot is None:
            snapshot = self._snapshot()
        if self._monitor is not None:
            if self.offer.session_changed(
                snapshot,
                can_query=can_query and self.offer.bound and self._offer_eligible(),
                revision=snapshot.revision if isinstance(snapshot, MonitorSnapshot) else None,
            ):
                self._offer_query_req = ""
                self._cancel_offer_start()
        return snapshot

    def _send_offer_query(self) -> None:
        request = uuid4().hex
        self._offer_query_req = request
        try:
            self.controller.supervisor.send(
                {"type": "meeting.call.query", "request_type": "meeting.call.query", "req": request}
            )
        except (RuntimeError, ValueError):
            if self._offer_query_req == request:
                self._offer_query_req = ""

    def _is_offer_event(self, event: Message) -> bool:
        kind = event.get("type")
        if kind in ("meeting.call.started", "meeting.call.ended"):
            return True
        # W1 and W4 use the same reply types. Exclude the known dialog request.
        return (
            event.get("request_type") == "meeting.call.query"
            and kind in ("meeting.call.state", "error")
            and (not self._query_req or event.get("req") != self._query_req)
        )

    def _receive_offer(
        self, raw: Message, origin: Origin, snapshot: SessionSnapshot, *, current: bool = True
    ) -> None:
        reason = OfferReason.CARD_ERROR if raw.get("type") == "error" else OfferReason(origin)
        log_meeting_offer(OfferEvent.RECEIVED, reason)
        self.offer.receive(
            call_id=raw.get("call_id", ""),
            app_class=raw.get("app_class", ""),
            measured_ms=raw.get("measured_mono_ms", 0),
            origin=origin,
            eligible=current and self._offer_eligible(),
            session=snapshot,
        )

    def _reset_detector(self, generation: int | None) -> None:
        self._offer_query_req = ""
        self._detector_generation = generation
        self._detector_running: bool | None = False
        self._detector_pending: tuple[str, str] | None = None
        self._detector_retry_at = 0.0
        self._detector_retry_delay = 1.0

    def sync_detector(self) -> None:
        allowed = self._observation_allowed()
        if not self._closed and not allowed:
            # The settings bridge signals this directly, before the next control tick.
            self._suspend_automation()
        controller = self.controller
        role = controller.supervisor
        # Detector request deadlines must not add another failure path during capture.
        if self._closed or self.busy or self._retire_deadline is not None or not role.ready:
            return
        if controller.state == "stopped" and not self._standby_role:
            return  # A consumed role still owes finalization and exit.
        if self._detector_generation != role.generation:
            self._reset_detector(role.generation)
        kind = "meeting.detector.start" if allowed else "meeting.detector.stop"
        if self.clock() < self._detector_retry_at:
            return
        if self._detector_pending is not None:
            if self._detector_pending[1] == kind:
                return
        elif self._detector_running == allowed:
            return

        request = uuid4().hex
        pending = (request, kind)
        message: Message = {"type": kind, "request_type": kind, "req": request}
        if allowed:
            message["apps_version"] = 1
        else:
            self._offer_query_req = ""
        # send() can deliver a rejection synchronously.
        self._detector_pending = pending
        try:
            role.send(message)
        except Exception:
            if self._detector_pending == pending:
                self._detector_failed(transient=True)

    def _detector_failed(self, *, transient: bool) -> None:
        self._detector_pending = None
        self._detector_running = None
        if transient:
            self._detector_retry_at = self.clock() + self._detector_retry_delay
            self._detector_retry_delay = min(30.0, self._detector_retry_delay * 2)
        else:
            self._detector_retry_at = math.inf
            self.offer.detector_unavailable()

    def _detector_reply(self, reply: Message, generation: int) -> None:
        if self._detector_generation != generation or self._detector_pending != (
            reply.get("req"),
            reply["request_type"],
        ):
            return
        kind = reply["type"]
        if kind in {"meeting.detector.started", "meeting.detector.stopped"}:
            self._detector_pending = None
            self._detector_running = kind == "meeting.detector.started"
            self._detector_retry_at = 0.0
            self._detector_retry_delay = 1.0
            if self._detector_running:
                self.offer.detector_started()
                self._observation_retry_at = 0.0
                self._observation_retry_delay = 2.0
        elif kind == "error":
            self._detector_failed(transient=reply["code"] in {"limit", "timeout"})

    def _suspend_automation(self) -> None:
        if (
            not self._automation_off
            or not self.enabled()
            or not self.policy_allowed()
            or not self._detector_allowed()
        ):
            self._cancel_offer_start()
        self.offer.suspend(clear_memory=True, reason=OfferReason.POLICY)

    def _log_start_outcome(self, outcome: StartOutcome) -> None:
        pressed_at, self._start_pressed_at = self._start_pressed_at, None
        if self._start_origin is not None and pressed_at is not None:
            log.info(START_LATENCY, self._start_origin, outcome, self.offer.now_ms() - pressed_at)
            if outcome is StartOutcome.FAILED:
                controller = self.controller
                code, request = (
                    controller.role_start_error
                    if self._start_attempt == controller._attempt
                    else ("", "")
                )
                logging.getLogger("astra_voice.meetings").info(
                    START_FAILED, StartFailureCode(code), StartFailureRequest(request)
                )

    def _clear_start(self) -> None:
        self._start_origin = None
        self._start_attempt = 0
        self._pending_call_lost = ""
        self._pending_offer_dialog = self._start_remote = False

    def _cancel_offer_start(self) -> None:
        if self._start_origin == StartOrigin.OFFER:
            self._log_start_outcome(StartOutcome.ABORTED)
            if self._pending_offer_dialog:
                self._prepared_remote = self._prepared_network = False
            self._clear_start()

    def _finish_start_error(self, code: str) -> None:
        origin = self._start_origin
        failed = code in _START_FAILURES
        self._log_start_outcome(StartOutcome.FAILED if failed else StartOutcome.ABORTED)
        self._clear_start()
        if origin == StartOrigin.OFFER:
            if failed:
                self.offer.start_failed(ERROR_TEXT[code])
            elif code == "disabled":
                self.offer.start_closed(reason=OfferReason.POLICY)
            else:
                self.offer.start_aborted()

    def _route_offer_dialog(self) -> None:
        self._pending_offer_dialog = True
        self.offer.start_resolved()
        self._log_start_outcome(StartOutcome.FAILED)

    def _resolve_start(self) -> None:
        origin = self._start_origin
        if origin is None:
            return
        if origin == StartOrigin.OFFER and not self.offer.starting_slot and not self._start_remote:
            self._cancel_offer_start()
            return
        controller = self.controller
        if controller._attempt != self._start_attempt:
            return
        reason, state = controller.far_reason, controller.state
        if origin == StartOrigin.CARD and self.busy:
            snapshot = self._snapshot()
            if (
                not controller._allowed(snapshot)
                or snapshot.blocked_epoch != controller._click_epoch
            ):
                self._finish_start_error("session-blocked")
                return
        if (
            not self._pending_call_lost
            and not self._pending_offer_dialog
            and self._notice_app in CALL_LOST_TEXT
            and reason in TARGET_REFUSALS
            and state in {"idle", "closing-unknown", "stopped"}
            and (not self._start_remote or reason == "target-mismatch")
        ):
            self._pending_call_lost = self._notice_app
            self._query_req = ""
            if origin == StartOrigin.OFFER:
                self.offer.start_resolved()
            self._log_start_outcome(StartOutcome.CALL_LOST)
        if (
            origin == StartOrigin.OFFER
            and self.offer.starting_slot
            and not self._pending_offer_dialog
            and reason == "monitor-stream-unsupported"
        ):
            self.no_app_split = True
            self._route_offer_dialog()
        if self._pending_offer_dialog or self._pending_call_lost:
            # Only the soft prepare refusal returns a reusable role to idle.
            if self.busy or (state != "idle" and not controller.supervisor.exited):
                return
            reason = OfferReason.CALL_LOST if self._pending_call_lost else OfferReason.NO_APP_SPLIT
            if self._pending_call_lost:
                self._variant1_lost = self._pending_call_lost
                self.selected_source = "room"
            self._clear_start()
            if origin == StartOrigin.OFFER:
                self.offer.start_closed(reason=reason)
                self.openMeetingsRequested.emit()
                self.open_dialog()
            return
        code = controller.error_code
        if code in {"session-blocked", "disabled", "storage-unavailable", "disk-low"}:
            self._finish_start_error(code)
        elif state == "stopped":
            self._finish_start_error(code or "audio-unavailable")

    def _event(self, event: Message) -> None:
        generation = self.controller.supervisor.generation
        if event.get("generation") != generation:
            if self._is_offer_event(event):
                log_meeting_offer(OfferEvent.DROPPED, OfferReason.GENERATION)
            return
        raw = {key: value for key, value in event.items() if key != "generation"}
        try:
            raw = ipc.decode(ipc.encode(raw, role="meeting-capture")[4:], role="meeting-capture")
        except (ipc.FrameError, ValueError, TypeError):
            if self._is_offer_event(event):
                log_meeting_offer(OfferEvent.DROPPED, OfferReason.DECODE)
            return
        if raw.get("request_type") in {"meeting.detector.start", "meeting.detector.stop"}:
            self._detector_reply(raw, generation)
            return
        if raw.get("request_type") == "meeting.call.query":
            if self._offer_query_req and raw["req"] == self._offer_query_req:
                snapshot = self._update_offer_session()
                if raw["type"] in {"meeting.call.state", "error"}:
                    current = self._offer_query_req == raw["req"]
                    self._offer_query_req = ""
                    self._receive_offer(raw, "card", snapshot, current=current)
                return
            if (
                raw["type"] != "meeting.call.state"
                or not self.dialog_open
                or self._variant1_lost
                or raw["req"] != self._query_req
            ):
                if self._is_offer_event(raw):
                    log_meeting_offer(OfferEvent.DROPPED, OfferReason.CARD_REQ)
                return
            self.call_id = raw.get("call_id", "")
            self.app_class = raw.get("app_class", "")
            self.variant1 = raw.get("variant1", False)
            if raw["active"]:
                self.offer.track_call(self.call_id, self.app_class)
            if self._call_available:
                if not self._selection_touched and not self.no_app_split:
                    self.selected_source = "call"
            elif self.selected_source == "call":
                self.selected_source = "room"
            self._publish()
            return
        if raw["type"] == "meeting.call.started":
            self._receive_offer(raw, "offer", self._update_offer_session())
            return
        if raw["type"] == "meeting.call.ended":
            if self.offer.call_ended(raw["call_id"], raw["reason"]):
                self._offer_query_req = ""
            return
        if raw["type"] == "meeting.detector.code":
            if raw["code"] == DetectorCode.REMOTE and not self._automation_off:
                self._start_remote = (
                    raw["purpose"] == DetectorPurpose.START
                    and self._start_origin == StartOrigin.OFFER
                )
                self._automation_off = True
                self._query_req = self._offer_query_req = ""
                self.offer.suspend(clear_memory=True, reason=OfferReason.REMOTE)
                if self.selected_source == "call":
                    self.selected_source = "room"
                self._publish()
            log.info(
                DETECTOR_CODE,
                DetectorPurpose(raw["purpose"]),
                DetectorCode(raw["code"]),
                raw["duration_ms"],
            )
            return
        controller = self.controller
        if self._standby_role and controller.state != "preparing":
            # Launch/hello and idle IPC must not touch a previous capture's state.
            self._publish()
            return
        if self.controller._exit_confirmed and raw["type"] == "error":
            # The supervisor fails pending requests after reporting process exit.
            return
        if self._start_origin == StartOrigin.OFFER:
            self._update_offer_session()
        if (
            raw["type"] == "meeting.capture.prepared"
            and controller.state == "preparing"
            and controller.meeting is not None
            and raw["meeting_id"] == controller.meeting.id
            and raw["meeting_generation"] == controller.meeting.meeting_generation
            and controller._requests.get(raw["req"]) == raw["request_type"]
        ):
            # Newly discovered warnings must be visible before a further click;
            # cancel this prepared role without ever granting open.
            network = bool(
                self.controller.meeting and self.controller.meeting.warnings.storage_network
            )
            remote = not raw["server_local"]
            remote_unapproved = remote and (
                not self._accepted_remote or controller._far["kind"] == "app_stream"
            )
            if remote_unapproved or (network and not self._accepted_network):
                self._audio = AudioHints(remote=remote, speakers=self._audio.speakers)
                self._storage = StorageStatus(
                    self._storage.free_mb, self._storage.writable, network
                )
                self._prepared_remote = remote
                if remote and self.selected_source == "call":
                    self.selected_source = "room"
                if self._start_origin == StartOrigin.OFFER and self.offer.starting_slot:
                    self._prepared_network = network
                    self._route_offer_dialog()
                self.clicks.on_stop_clicked()
            elif (
                not self.dialog_open
                and self._start_origin != StartOrigin.OFFER
                or not self.enabled()
                or not self.policy_allowed()
            ):
                self.clicks.on_stop_clicked()
        was_opening = controller.state == "opening"
        controller.handle_event(event)
        if self.controller.far_reason == "monitor-stream-unsupported":
            self.no_app_split = True
            self.controller.error_code = ""
        if self.controller.state in {"recording", "paused"} and self.controller._committed:
            self.dialog_open = False
            self._variant1_lost = ""
            self._prepared_remote = self._prepared_network = False
        self._publish(
            started=was_opening
            and raw["type"] == "meeting.capture.started"
            and controller.state in {"recording", "paused"}
        )

    def _exit(self, code: int) -> None:
        retiring = self._retire_deadline is not None
        state = self.controller.state
        starting = state in {"preparing", "opening"}
        capture_crashed = code != 0 and state in {"recording", "paused"}
        if (
            (self._standby_role or starting or capture_crashed or self._start_origin is not None)
            and not retiring
            and self._observation_allowed()
        ):
            self._defer_observation()
        if not self.controller._exit_confirmed:
            if starting:
                self.controller.error_code = "audio-unavailable"
            self.controller.on_process_exit(code)
        self._role_started = False
        self._retire_deadline = None
        self._offer_query_req = ""
        self.offer.role_ended(self._role_serial, reason=OfferReason.ROLE_ENDED)
        if retiring and self.dialog_open:
            # W4 may have been reopened while the old role was retiring.
            # Restore only its query/preview role, never implicit capture.
            self._activate()
        self._publish()

    def tick(self) -> None:
        if self._closed:
            return
        self.offer.begin_tick()
        while not self._results.empty():
            kind, value = self._results.get_nowait()
            if kind == "preview":
                serial, storage, audio, error = value
                if serial == self._preview_serial and self.dialog_open:
                    self._storage = StorageStatus(
                        storage.free_mb, storage.writable, storage.network or self._prepared_network
                    )
                    self._audio = AudioHints(
                        remote=audio.remote or self._prepared_remote, speakers=audio.speakers
                    )
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
        if self.controller.state == "idle":
            # Failed preflight never prepared the process; it is still reusable.
            self._standby_role = True
        if self._role_started and self.controller.supervisor.exited:
            self._exit(3)
        self._sync_role_ownership()
        snapshot = self._update_offer_session()
        self._resolve_start()
        if self.offer.tick(eligible=self._offer_eligible(), session=snapshot):
            self._send_offer_query()
        self._tick_jobs()
        if (
            self.dialog_open
            and not self._automation_off
            and not self._variant1_lost
            and not self._pending_call_lost
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
        if not self._jobs_allowed():
            self.jobs.suspend()  # a ban stops the running job; it resumes when lifted
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

    def _publish(self, *, started: bool = False) -> None:
        if started and self._start_origin is not None:
            origin = self._start_origin
            self._log_start_outcome(StartOutcome.STARTED)
            self._clear_start()
            if origin == StartOrigin.OFFER:
                self.offer.start_done()
        self._resolve_start()
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
                self.offer.state,
                self.offer.presentation,
                self.state,
                self.error,
                self.warnings,
                self.selected_source,
                self.call_id,
                self.app_class,
                self.variant1,
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
        """App class of the call this recording was started with."""
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
        if self._closed or self._shutting_down:
            return
        self._shutting_down = True
        self._log_start_outcome(StartOutcome.ABORTED)
        self._clear_start()
        self.offer.start_closed(reason=OfferReason.SHUTDOWN)
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
        self.offer.bind(None)
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
