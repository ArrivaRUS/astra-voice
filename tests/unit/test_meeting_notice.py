"""W3 data path, notification fallback and exit/SIGTERM stop with the real controller."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from PyQt5.QtTest import QSignalSpy
from test_meeting_overlay import finish, start
from test_meeting_recording_bridge import MID, Rig, Supervisor
from test_meeting_recording_bridge import rig as rig

from astra_voice.core import policy as core
from astra_voice.core.settings import Settings
from astra_voice.meetings.events import MeetingState
from astra_voice.meetings.model import Meeting
from astra_voice.meetings.settings_ext import NOTICE_TEXT
from astra_voice.ui.meeting_bridge import MeetingBridge
from astra_voice.ui.meeting_notice import copy_plain_text
from astra_voice.ui.meeting_texts import notice_mute_text

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("filesystem_euid")]


def test_copy_publishes_text_plain_only() -> None:
    from PyQt5.QtGui import QClipboard

    from helpers.qt_app import get_qapplication

    app = get_qapplication()
    text = "Встреча записывается для протокола."
    assert copy_plain_text(text) is True
    published = app.clipboard().mimeData(QClipboard.Clipboard)
    assert published.formats() == ["text/plain"]
    assert published.text() == text


def test_copy_reports_unavailable_clipboard(monkeypatch: pytest.MonkeyPatch) -> None:
    from PyQt5.QtGui import QGuiApplication

    monkeypatch.setattr(QGuiApplication, "clipboard", staticmethod(lambda: None))
    assert copy_plain_text("x") is False


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ({}, (NOTICE_TEXT, False)),
        ({"meeting_notice_text": "Администратор пишет"}, ("Администратор пишет", True)),
        # Spec §2.7: an invalid value shows the default text, not «задан администратором».
        ({"meeting_notice_text": "a\x00b"}, (NOTICE_TEXT, False)),
    ],
)
def test_notice_text_follows_policy(raw: dict[str, str], expected: tuple[str, bool]) -> None:
    policy = core.Policy(raw=raw, status=core.PolicyStatus.OK)
    bridge = MeetingBridge(Settings(meetings_enabled=True), policy=policy, store=_Empty())
    try:
        assert bridge.notice() == expected
    finally:
        bridge.shutdown()


def test_user_notice_text_is_not_marked_as_administrator() -> None:
    bridge = MeetingBridge(
        Settings(meetings_enabled=True, meeting_notice_text="Мой текст"), store=_Empty()
    )
    try:
        assert bridge.notice() == ("Мой текст", False)
    finally:
        bridge.shutdown()


class _Empty:
    def list(self) -> list[Meeting]:
        return []


def test_mute_line_names_dion_only_for_a_dion_call() -> None:
    assert "выключенный микрофон в DION на запись не влияет" in notice_mute_text("dion")
    other = notice_mute_text("browser")
    assert "в программе видеосвязи" in other and "DION" not in other
    assert other.endswith("Чтобы не записывать, нажмите «Пауза».")


@pytest.mark.parametrize("app_class", ["dion", "browser"])
def test_recording_remembers_the_call_app_for_w3(rig: Rig, app_class: str) -> None:
    rig.bridge.requestRecording()
    rig.recording.tick()
    query = rig.supervisor.messages[-1]
    rig.supervisor.event(
        {
            "type": "meeting.call.state",
            "req": query["req"],
            "request_type": "meeting.call.query",
            "active": True,
            "app_class": app_class,
            "call_id": "call1",
            "target": {
                "sink_input": 7,
                "object_serial": 9,
                "verified": "declared-verified",
            },
            "generation": 1,
        }
    )
    assert rig.bridge.selectedSource == "call"
    rig.bridge.on_start_clicked()
    assert rig.recording.notice_app_class == app_class


def test_room_recording_has_no_app_for_w3(rig: Rig) -> None:
    start(rig)
    assert rig.recording.notice_app_class == ""
    assert rig.recording.active_meeting_id == MID


def test_participants_from_tray_requests_keyboard_only_while_recording(rig: Rig) -> None:
    opened = QSignalSpy(rig.recording.participantsRequested)
    keyboard = QSignalSpy(rig.bridge.noticeKeyboardRequested)
    rig.bridge.openNoticeWithKeyboard()
    assert len(opened) == 0 and len(keyboard) == 0
    start(rig)
    rig.bridge.openNoticeWithKeyboard()
    assert len(opened) == 1 and len(keyboard) == 1


def test_undelivered_notice_is_shown_on_w2_for_eight_seconds(rig: Rig) -> None:
    rig.recording.show_notify_fallback("ignored before recording")
    start(rig)
    assert rig.recording.active_meeting_id == MID
    rig.recording.show_notify_fallback("Запись остановится через 5 минут")
    view = rig.bridge.overlay
    assert view["state"] == "notify-fallback"
    assert view["secondary"] == "Запись остановится через 5 минут"
    assert view["secondaryAction"] == ""
    rig.now += 7.9
    rig.recording.tick()
    assert rig.bridge.overlay["state"] == "notify-fallback"
    rig.now += 0.2
    rig.recording.tick()
    assert rig.bridge.overlay["state"] == "recording"
    assert rig.bridge.overlay["secondary"] == ""


def test_w2_reason_rows_take_priority_over_fallback(rig: Rig) -> None:
    start(rig)
    rig.now += 6900
    rig.recording.tick()
    assert rig.bridge.overlay["state"] == "limit-soon"
    rig.recording.show_notify_fallback("Запись остановится через 5 минут")
    assert rig.bridge.overlay["state"] == "limit-soon"
    assert rig.bridge.overlay["secondaryAction"] == "extend"


def test_fallback_does_not_cross_into_the_next_meeting(rig: Rig) -> None:
    next_id = "2026-11-01_120100-abce"
    ids = iter([MID, next_id])
    rig.recording.controller.id_factory = lambda: next(ids)
    start(rig)
    rig.recording.show_notify_fallback("Встреча сохранена.")
    finish(rig)
    start(rig, mid=next_id)
    assert rig.bridge.overlay["state"] == "recording"


class _GracefulRole(Supervisor):
    """Capture role that answers stop with closed/finalized and exits on shutdown."""

    rig: Rig | None = None

    def __init__(self, *args: Any) -> None:
        super().__init__(*args)
        self.pending: list[tuple[str, dict[str, Any]]] = []

    def send(self, message: dict[str, Any], *, timeout: float | None = None) -> dict[str, Any]:
        sent = super().send(message, timeout=timeout)
        if message["type"] == "meeting.capture.stop":
            self.pending += [
                ("closed", {"tracks": ["mic"], "state_rev": 2}),
                (
                    "finalized",
                    {
                        "segments": {"mic": 0, "far": 0},
                        "intervals": [],
                        "status": "succeeded",
                        "state_rev": 2,
                    },
                ),
            ]
        elif message["type"] == "meeting.capture.shutdown":
            self.pending.append(("exit", {}))
        return sent

    def pump(self, timeout: float = 0.0) -> None:
        if not self.pending or self.rig is None:
            return
        action, fields = self.pending.pop(0)
        if action == "exit":
            self.ready, self.exited = False, True
            self.on_exit(0)
        else:
            self.rig.reply(action, **fields)


@pytest.mark.parametrize("state", ["recording", "paused"])
def test_exit_during_recording_stops_and_finalizes_without_kill(tmp_path: Path, state: str) -> None:
    from helpers.qt_app import get_qapplication

    get_qapplication()
    rig = Rig(tmp_path, role_type=_GracefulRole)
    rig.recording._exit_stop_timeout = 3.0
    role = rig.supervisor
    assert isinstance(role, _GracefulRole)
    role.rig = rig
    start(rig)
    if state == "paused":
        rig.bridge.on_overlay_pause_clicked()
        assert rig.bridge.overlay["state"] == "paused"
    rig.bridge.shutdown()
    kinds = [message["type"] for message in role.messages]
    assert kinds.index("meeting.capture.stop") < kinds.index("meeting.capture.shutdown")
    # The role exited by itself after finalization; kill() only released it afterwards.
    assert role.exited and rig.recording.controller._capture_finalized
    saved = rig.store.read(MID, "meeting.json")
    assert isinstance(saved, Meeting) and saved.state == MeetingState.RECORDED
    assert saved.ended_at
    assert rig.recording.controller.close(timeout=0)


def test_exit_without_capture_answer_kills_within_budget(tmp_path: Path) -> None:
    import time

    from helpers.qt_app import get_qapplication

    get_qapplication()
    rig = Rig(tmp_path)
    rig.recording._exit_stop_timeout = 0.3
    start(rig)
    began = time.monotonic()
    rig.bridge.shutdown()
    assert time.monotonic() - began < 2.0
    assert rig.supervisor.messages[-1]["type"] == "meeting.capture.stop"
    assert rig.supervisor.kills == 1


def test_exit_between_closed_and_finalized_waits_for_finalization(tmp_path: Path) -> None:
    """Review 3/1: «Стоп» (or stop("session")), then exit before `finalized`."""
    from helpers.qt_app import get_qapplication

    get_qapplication()
    rig = Rig(tmp_path, role_type=_GracefulRole)
    rig.recording._exit_stop_timeout = 3.0
    role = rig.supervisor
    assert isinstance(role, _GracefulRole)
    role.rig = rig
    start(rig)
    rig.bridge.on_overlay_stop_clicked()
    role.pending.clear()
    rig.reply("closed", tracks=["mic"], state_rev=2)
    assert rig.recording.controller.state == "stopped" and not role.exited
    role.pending.append(
        (
            "finalized",
            {
                "segments": {"mic": 0, "far": 0},
                "intervals": [],
                "status": "succeeded",
                "state_rev": 2,
            },
        )
    )
    rig.bridge.shutdown()
    assert rig.recording.controller._capture_finalized
    saved = rig.store.read(MID, "meeting.json")
    assert isinstance(saved, Meeting) and saved.state == MeetingState.RECORDED


def test_exit_budget_is_one_deadline_including_metadata_close(tmp_path: Path) -> None:
    import threading
    import time

    from helpers.qt_app import get_qapplication

    get_qapplication()
    rig = Rig(tmp_path)
    rig.recording._exit_stop_timeout = 0.6
    start(rig)
    release = threading.Event()

    def block() -> None:
        release.wait(5)

    rig.recording.controller._writer.submit(block)
    try:
        began = time.monotonic()
        rig.bridge.shutdown()
        assert time.monotonic() - began < 0.6 + 0.3
    finally:
        release.set()
        rig.recording.controller._writer.close(timeout=1)
