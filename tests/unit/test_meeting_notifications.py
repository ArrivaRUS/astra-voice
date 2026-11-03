"""E13 recording notices: one notice per edge, actions mirror W2, undelivered → W2."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest
from PyQt5.QtCore import QObject, pyqtSignal

from astra_voice.meetings.model import Mark
from astra_voice.ui import notify
from astra_voice.ui.meeting_notifications import MeetingNotifications

pytestmark = pytest.mark.unit
WRAPPERS = [name for name in notify.__all__ if name.startswith("notify_meeting_")]


class Recording(QObject):
    changed = pyqtSignal()
    openMeetingsRequested = pyqtSignal()

    def __init__(self) -> None:
        super().__init__()
        self.overlay: dict[str, Any] = {
            "active": False,
            "state": "hidden",
            "limitSoon": False,
            "canExtend": True,
            "canResume": False,
        }
        self.controller = SimpleNamespace(
            meeting=None,
            _committed=False,
            state="idle",
            tracks={},
            pause_reasons=set(),
            saved=False,
        )
        self.actions: list[str] = []
        self.fallback: list[str] = []

    def overlay_action(self, action: str) -> None:
        self.actions.append(action)

    def show_notify_fallback(self, text: str) -> None:
        self.fallback.append(text)

    def begin(self, mid: str = "m1") -> None:
        self.controller.meeting = SimpleNamespace(id=mid, marks=[])
        self.controller._committed, self.controller.state = True, "recording"
        self.controller.tracks, self.controller.pause_reasons = {"mic": "live"}, set()
        self.controller.saved = False
        self.overlay.update(active=True, state="recording", limitSoon=False, canResume=False)
        self.changed.emit()

    def stop(self, reason: str) -> None:
        self.controller.meeting.marks.append(Mark(1000, "pause", reason))
        self.controller.state = "stopped"
        self.overlay.update(active=False, state="hidden")
        self.changed.emit()
        self.controller.saved = True
        self.changed.emit()


@pytest.fixture
def notices() -> Iterator[SimpleNamespace]:
    handlers: dict[str, Callable[[], None] | None] = {}
    delivery: list[Callable[[str, bool], None] | None] = [None]
    module = SimpleNamespace(
        **{name: getattr(notify, name) for name in dir(notify) if name.isupper()},
        **{name: Mock(name=name) for name in WRAPPERS},
        set_action_handler=lambda key, callback: handlers.__setitem__(key, callback),
        set_delivery_handler=lambda callback: delivery.__setitem__(0, callback),
        handlers=handlers,
        delivery=delivery,
    )
    yield module


def sent(module: SimpleNamespace) -> list[str]:
    return [name for name in WRAPPERS if getattr(module, name).called]


def test_wrappers_have_fixed_spec_texts() -> None:
    assert notify.MEETING_LIMIT_SOON == "Запись остановится через 5 минут"
    assert notify.MEETING_SAVED == "Встреча сохранена."
    assert len(WRAPPERS) == 16  # +6 transcription notices (E13.M4)


def test_each_edge_notifies_once_and_new_capture_starts_clean(notices: SimpleNamespace) -> None:
    recording = Recording()
    owner = MeetingNotifications(recording, notices=notices)  # type: ignore[arg-type]
    try:
        recording.begin()
        assert sent(notices) == []
        recording.overlay["limitSoon"] = True
        recording.changed.emit()
        recording.changed.emit()
        notices.notify_meeting_limit_soon.assert_called_once_with()
        recording.overlay["limitSoon"], recording.overlay["canExtend"] = False, False
        recording.changed.emit()
        recording.overlay["limitSoon"] = True
        recording.changed.emit()
        notices.notify_meeting_limit_final.assert_called_once_with()
        recording.controller.tracks["mic"] = "lost"
        recording.changed.emit()
        recording.changed.emit()
        notices.notify_meeting_mic_paused.assert_called_once_with()
        # BL-5: locked → nothing; unlock (resume possible) → one notice.
        recording.controller.state = "paused"
        recording.controller.pause_reasons = {"lock"}
        recording.overlay.update(state="paused-lock", canResume=False)
        recording.changed.emit()
        notices.notify_meeting_lock_paused.assert_not_called()
        recording.overlay["canResume"] = True
        recording.changed.emit()
        recording.changed.emit()
        notices.notify_meeting_lock_paused.assert_called_once_with()
        recording.controller.pause_reasons = {"indicators"}
        recording.changed.emit()
        notices.notify_meeting_indicators_paused.assert_called_once_with()
        recording.stop("stop")
        recording.changed.emit()
        notices.notify_meeting_saved.assert_called_once_with()
        before = sent(notices)
        recording.begin("m2")
        recording.changed.emit()
        assert sent(notices) == before
    finally:
        owner.shutdown()


@pytest.mark.parametrize(
    ("reason", "wrapper"),
    [
        ("stop", "notify_meeting_saved"),
        ("limit", "notify_meeting_limit_stopped"),
        ("disk", "notify_meeting_disk_stopped"),
        ("sleep", "notify_meeting_sleep_stopped"),
        ("session", "notify_meeting_session_stopped"),
    ],
)
def test_stop_reason_selects_the_spec_notice(
    notices: SimpleNamespace, reason: str, wrapper: str
) -> None:
    recording = Recording()
    owner = MeetingNotifications(recording, notices=notices)  # type: ignore[arg-type]
    try:
        recording.begin()
        recording.stop(reason)
        assert sent(notices) == [wrapper]
    finally:
        owner.shutdown()


def test_actions_mirror_w2_and_undelivered_notice_goes_to_w2(notices: SimpleNamespace) -> None:
    recording = Recording()
    opened = Mock()
    recording.openMeetingsRequested.connect(opened)
    owner = MeetingNotifications(recording, notices=notices)  # type: ignore[arg-type]
    try:
        # Each button acts only after its notice was sent for this meeting.
        recording.begin()
        recording.overlay["limitSoon"] = True
        recording.controller.tracks["mic"] = "lost"
        recording.changed.emit()
        recording.controller.state, recording.controller.pause_reasons = "paused", {"lock"}
        recording.overlay.update(state="paused-lock", canResume=True)
        recording.changed.emit()
        for key, action in [
            (notify.ACTION_MEETING_EXTEND, "extend"),
            (notify.ACTION_MEETING_RESUME, "resume"),
            (notify.ACTION_MEETING_STOP, "stop"),
        ]:
            callback = notices.handlers[key]
            assert callback is not None
            callback()
            assert recording.actions[-1] == action
        open_callback = notices.handlers[notify.ACTION_MEETING_OPEN]
        assert open_callback is not None
        open_callback()
        opened.assert_called_once_with()
        report = notices.delivery[0]
        assert report is not None
        report(notify.MEETING_LIMIT_SOON, True)
        report("Горячая клавиша перестала работать", False)
        assert recording.fallback == []
        report(notify.MEETING_LIMIT_SOON, False)
        assert recording.fallback == [notify.MEETING_LIMIT_SOON]
    finally:
        owner.shutdown()
    # Exit unregisters everything: no notice, action or fallback after shutdown.
    assert notices.delivery[0] is None
    assert all(callback is None for callback in notices.handlers.values())
    for name in WRAPPERS:
        getattr(notices, name).reset_mock()
    recording.begin("m2")
    recording.overlay["limitSoon"] = True
    recording.changed.emit()
    assert sent(notices) == []


@pytest.fixture
def clean_notify() -> Iterator[None]:
    notify.reset_state()
    yield
    notify.reset_state()


@pytest.mark.parametrize(
    ("notification_id", "error", "delivered"),
    [
        (7, None, True),
        (None, "org.freedesktop.DBus.Error.ServiceUnknown", False),
        (None, "org.freedesktop.DBus.Error.NoReply", False),
        (None, "invalid-reply", False),
    ],
)
def test_notify_reports_delivery_of_each_finished_send(
    clean_notify: None, notification_id: int | None, error: str | None, delivered: bool
) -> None:
    from time import monotonic

    reports: list[tuple[str, bool]] = []
    notify.set_delivery_handler(lambda summary, ok: reports.append((summary, ok)))
    notice = notify._Notice(notify.MEETING_SAVED, "", 1, (), monotonic(), False)
    notify._sent[1] = (notice, 0)
    notify._on_reply(1, notification_id, error)
    assert reports == [(notify.MEETING_SAVED, delivered)]
    notify.reset_state()
    notify._sent[2] = (notice, 0)
    notify._on_reply(2, None, error or "x")
    assert reports == [(notify.MEETING_SAVED, delivered)], "reset_state drops the listener"


def test_delivery_listener_failure_is_contained(clean_notify: None) -> None:
    from time import monotonic

    def broken(summary: str, ok: bool) -> None:
        raise RuntimeError("PRIVATE")

    notify.set_delivery_handler(broken)
    notice = notify._Notice(notify.MEETING_SAVED, "", 1, (), monotonic(), False)
    notify._sent[1] = (notice, 0)
    notify._on_reply(1, 5, None)
    assert notify.last_delivery_ok()


def test_stale_notice_buttons_never_override_a_later_intent(notices: SimpleNamespace) -> None:
    """Review 3/2: an old «Продолжить» from a lock notice must not end a user pause."""
    recording = Recording()
    owner = MeetingNotifications(recording, notices=notices)  # type: ignore[arg-type]
    resume = notices.handlers[notify.ACTION_MEETING_RESUME]
    extend = notices.handlers[notify.ACTION_MEETING_EXTEND]
    assert resume is not None and extend is not None
    try:
        recording.begin()
        recording.controller.state = "paused"
        recording.controller.pause_reasons = {"lock"}
        recording.overlay.update(state="paused-lock", canResume=True)
        recording.changed.emit()
        notices.notify_meeting_lock_paused.assert_called_once_with()
        # Normal path: the button right after unlock resumes.
        resume()
        assert recording.actions == ["resume"]
        # Resumed from W2, later paused by the person: the old button does nothing.
        recording.controller.state, recording.controller.pause_reasons = "recording", set()
        recording.overlay.update(state="recording", canResume=False)
        recording.changed.emit()
        recording.controller.state, recording.controller.pause_reasons = "paused", {"user"}
        recording.overlay.update(state="paused", canResume=True)
        recording.changed.emit()
        resume()
        assert recording.actions == ["resume"]
        # «Продлить» belongs to the meeting it was sent for.
        recording.overlay.update(state="recording", limitSoon=True)
        recording.controller.state, recording.controller.pause_reasons = "recording", set()
        recording.changed.emit()
        recording.begin("m2")
        extend()
        assert recording.actions == ["resume"]
        recording.overlay["limitSoon"] = True
        recording.changed.emit()
        extend()
        assert recording.actions == ["resume", "extend"]
    finally:
        owner.shutdown()
