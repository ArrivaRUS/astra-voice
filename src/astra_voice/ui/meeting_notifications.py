"""System notices for E13 recording events (spec §4.6, S17-A4).

Edges of published capture state become fixed-text notices. Every notice action
is also a W2 button and a tray item; a notice the service did not take shows its
text on W2 for 8 s (``notify-fallback``). Exit never announces itself: the owner
shuts this down before stopping capture.
"""

from __future__ import annotations

from collections.abc import Callable
from types import ModuleType
from typing import Any

from PyQt5.QtCore import QObject

from astra_voice.meetings.model import Meeting
from astra_voice.ui import notify as notify_module
from astra_voice.ui.meeting_recording import MeetingRecording


def job_notifier(notices: ModuleType = notify_module) -> Callable[[str, Meeting | int], None]:
    """MeetingJobs events -> fixed-text notices (spec §2.5, §4.6, §4.7)."""

    def deliver(kind: str, payload: Meeting | int) -> None:
        if kind == "done":
            notices.notify_meeting_transcribed()
        elif kind == "waiting_memory":
            notices.notify_meeting_memory_wait()
        elif kind == "needs_model":
            notices.notify_meeting_needs_model()
        elif kind == "failed_model":
            notices.notify_meeting_selfcheck_failed()
        elif kind == "failed":
            notices.notify_meeting_transcription_failed()
        elif kind == "retention" and isinstance(payload, int) and payload > 0:
            notices.notify_meeting_retention(payload)

    return deliver


class MeetingNotifications(QObject):
    def __init__(
        self,
        recording: MeetingRecording,
        *,
        notices: ModuleType = notify_module,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.recording = recording
        self._notices = notices
        self._closed = False
        self._meeting = ""
        self._edges: dict[str, bool] = {}
        self._summaries = frozenset(
            (
                notices.MEETING_LIMIT_SOON,
                notices.MEETING_MIC_PAUSED,
                notices.MEETING_LOCK_PAUSED,
                notices.MEETING_INDICATORS_PAUSED,
                notices.MEETING_SAVED,
                notices.MEETING_LIMIT_STOPPED,
                notices.MEETING_DISK_STOPPED,
                notices.MEETING_SLEEP_STOPPED,
                notices.MEETING_SESSION_STOPPED,
            )
        )
        # A notice button acts only for the meeting (and pause reason) it was sent
        # for: an old «Продолжить» in the notification history must never end a
        # pause the person chose later.
        self._issued: dict[str, tuple[str, str]] = {}
        self._actions: dict[str, Callable[[], None]] = {
            notices.ACTION_MEETING_EXTEND: lambda: self._act(
                notices.ACTION_MEETING_EXTEND, "extend"
            ),
            notices.ACTION_MEETING_RESUME: lambda: self._act(
                notices.ACTION_MEETING_RESUME, "resume"
            ),
            notices.ACTION_MEETING_STOP: lambda: self._act(notices.ACTION_MEETING_STOP, "stop"),
            notices.ACTION_MEETING_OPEN: recording.openMeetingsRequested.emit,
        }
        for key, callback in self._actions.items():
            notices.set_action_handler(key, callback)
        notices.set_delivery_handler(self._delivered)
        recording.changed.connect(self.refresh)
        self.refresh()

    def _state(self) -> dict[str, Any]:
        controller = self.recording.controller
        overlay = self.recording.overlay
        live = bool(overlay["active"]) and overlay["state"] not in {"stopping", "error"}
        reasons = controller.pause_reasons
        return {
            "limit": live and bool(overlay["limitSoon"]),
            "mic": live
            and controller.state == "recording"
            and controller.tracks.get("mic") in {"lost", "paused"},
            # BL-5: the notice follows unlock, when «Продолжить» is possible.
            "lock": live and "lock" in reasons and bool(overlay["canResume"]),
            "indicators": live and "indicators" in reasons,
            "saved": bool(controller.saved),
        }

    def refresh(self) -> None:
        if self._closed:
            return
        controller = self.recording.controller
        meeting = controller.meeting
        meeting_id = meeting.id if meeting is not None and controller._committed else ""
        state = self._state()
        if meeting_id != self._meeting:
            # A new capture starts from its own edges; an old one never re-announces.
            self._meeting, self._edges = meeting_id, state
            return
        if not meeting_id:
            return
        rising = {key for key, value in state.items() if value and not self._edges.get(key)}
        self._edges = state
        notices = self._notices
        if "limit" in rising:
            if self.recording.overlay["canExtend"]:
                self._issued[notices.ACTION_MEETING_EXTEND] = (meeting_id, "")
                notices.notify_meeting_limit_soon()
            else:
                notices.notify_meeting_limit_final()
        if "mic" in rising:
            self._issued[notices.ACTION_MEETING_STOP] = (meeting_id, "")
            notices.notify_meeting_mic_paused()
        if "lock" in rising:
            self._issued[notices.ACTION_MEETING_RESUME] = (meeting_id, "lock")
            notices.notify_meeting_lock_paused()
        if "indicators" in rising:
            notices.notify_meeting_indicators_paused()
        if "saved" in rising and meeting is not None:
            # MeetingController.stop(reason) records the cause as the last pause mark.
            reason = next(
                (mark.reason for mark in reversed(meeting.marks) if mark.kind == "pause"), ""
            )
            if reason == "limit":
                notices.notify_meeting_limit_stopped()
            elif reason == "disk":
                notices.notify_meeting_disk_stopped()
            elif reason == "sleep":
                notices.notify_meeting_sleep_stopped()
            elif reason == "session":
                notices.notify_meeting_session_stopped()
            elif self._transcribes_now():
                notices.notify_meeting_saved_transcribing()
            else:
                notices.notify_meeting_saved()

    def _transcribes_now(self) -> bool:
        jobs = getattr(self.recording, "jobs", None)
        return bool(jobs is not None and jobs.transcribe_immediately())

    def _act(self, key: str, action: str) -> None:
        issued = self._issued.get(key)
        controller = self.recording.controller
        meeting = controller.meeting
        if self._closed or issued is None or meeting is None or meeting.id != issued[0]:
            return
        if issued[1] and issued[1] not in controller.pause_reasons:
            return
        self.recording.overlay_action(action)

    def _delivered(self, summary: str, delivered: bool) -> None:
        if not delivered and not self._closed and summary in self._summaries:
            self.recording.show_notify_fallback(summary)

    def shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.recording.changed.disconnect(self.refresh)
        self._notices.set_delivery_handler(None)
        for key in self._actions:
            self._notices.set_action_handler(key, None)
