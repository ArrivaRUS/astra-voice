"""E13.M4 GUI hooks of the transcription queue: list rows, actions, tray, notices, exit."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest
from test_tray import Harness
from test_tray import forbid_real_x as forbid_real_x
from test_tray import harness as harness
from test_tray import qapp as qapp

from astra_voice import app
from astra_voice.core.meeting_jobs import MeetingJobs, ModelAdmission
from astra_voice.core.meeting_session import StorageStatus
from astra_voice.core.settings import Settings
from astra_voice.meetings.events import MeetingState
from astra_voice.meetings.model import Meeting
from astra_voice.meetings.store import MeetingStore
from astra_voice.platform.session import SessionKind
from astra_voice.ui import notify
from astra_voice.ui.meeting_bridge import MeetingBridge
from astra_voice.ui.meeting_notifications import MeetingNotifications, job_notifier
from astra_voice.ui.meeting_recording import AudioHints, MeetingRecording
from astra_voice.ui.tray_icons import TrayIconProvider
from astra_voice.worker import ipc

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("filesystem_euid")]
MID = "2026-11-01_120000-aaaa"
LOAD = {
    "id": "gigaam-v3-e2e-rnnt-int8",
    "revision": "r1",
    "dir": "/models/gigaam",
    "layout": "onnx-asr-gigaam-v3",
    "variant": "gigaam-v3-e2e-rnnt",
    "threads": 2,
    "min_ram_mb": 419,
}


class Capture:
    """Capture role fake: never started by these tests."""

    generation = 1

    def __init__(
        self, event: Callable[[dict[str, Any]], None], exited: Callable[[int], None]
    ) -> None:
        self.ready, self.exited = False, True

    def start(self) -> None:
        self.ready, self.exited = True, False

    def send(self, message: dict[str, Any], *, timeout: float | None = None) -> dict[str, Any]:
        return message

    def set_meeting_generation(self, mid: str, generation: int) -> None:
        pass

    def pump(self, timeout: float = 0.0) -> None:
        pass

    def kill(self) -> None:
        self.ready = False


class Role:
    def __init__(
        self, event: Callable[[dict[str, Any]], None], exited: Callable[[int], None]
    ) -> None:
        self.event = event
        self.generation = 1
        self.state, self.ready, self.exited = "new", False, False
        self.sent: list[dict[str, Any]] = []
        self.stops = 0

    def start(self) -> None:
        self.state, self.ready = "running", True

    def send(self, message: dict[str, Any], *, timeout: float | None = None) -> dict[str, Any]:
        ipc.decode(ipc.encode(message, role="meeting-transcribe")[4:], role="meeting-transcribe")
        self.sent.append(message)
        return message

    def set_meeting_generation(self, mid: str, generation: int) -> None:
        pass

    def kill(self) -> None:
        self.state, self.ready = "stopped", False

    def stop(self) -> None:
        self.stops += 1
        self.state, self.ready, self.exited = "stopped", False, True

    def emit(self, kind: str, **fields: Any) -> None:
        self.event({"type": kind, **fields, "generation": self.generation})

    def types(self) -> list[str]:
        return [message["type"] for message in self.sent]


class Rig:
    def __init__(self, root: Path) -> None:
        self.store = MeetingStore(root / "meetings")
        self.settings = Settings(meetings_enabled=True)
        self.tasks: list[Callable[[], None]] = []
        self.bridge = MeetingBridge(self.settings, store=self.store, submit=self.tasks.append)
        self.recording = MeetingRecording(
            self.store,
            enabled=lambda: self.bridge.enabled,
            policy_allowed=lambda: self.bridge.canEnable,
            session_kind=SessionKind.KDE,
            dev_enabled=True,
            session_factory=Mock,
            supervisor_factory=lambda event, exited: Capture(event, exited),
            storage_probe=lambda path: StorageStatus(1000, True, False),
            audio_probe=AudioHints,
            submit=lambda job: job(),
            exit_stop_timeout=0.2,
        )
        self.bridge.bindRecording(self.recording)
        self.immediately = True
        self.admission = ModelAdmission("ok", dict(LOAD))
        self.memory: int | None = 4000
        self.notices: list[tuple[str, str]] = []
        self.roles: list[Role] = []

        def factory(event: Callable[[dict[str, Any]], None], exited: Callable[[int], None]) -> Role:
            role = Role(event, exited)
            self.roles.append(role)
            return role

        self.jobs = MeetingJobs(
            self.store,
            factory,
            transcribe_immediately=lambda: self.immediately,
            audio_retention_days=lambda: 7,
            model_probe=lambda: self.admission,
            mem_available=lambda: self.memory,
            submit=lambda operation: operation(),
            notify=lambda kind, meeting: self.notices.append((kind, meeting.id)),
            echo_gate=False,
        )
        self.recording.bind_jobs(self.jobs)

    @property
    def role(self) -> Role:
        return self.roles[-1]

    def create(
        self, mid: str = MID, state: MeetingState = MeetingState.RECORDED, **kw: Any
    ) -> None:
        hour = mid[11:13]
        self.store.create(
            Meeting(
                id=mid,
                started_at=f"2026-11-01T{hour}:00:00+00:00",
                ended_at=f"2026-11-01T{hour}:30:00+00:00",
                state=state,
                recorded_ms=1_800_000,
                **kw,
            )
        )

    def refresh(self) -> dict[str, dict[str, Any]]:
        self.bridge.refresh()
        self.tasks.pop(0)()
        self.bridge._poll()
        return {row["id"]: row for row in self.bridge.meetings}

    def meeting(self, mid: str = MID) -> Meeting:
        value = self.store.read(mid, "meeting.json")
        assert isinstance(value, Meeting)
        return value

    def start_running(self) -> None:
        self.recording.tick()
        load = next(m for m in self.role.sent if m["type"] == "model.load")
        self.role.emit(
            "model.loaded",
            req=load["req"],
            request_type="model.load",
            id=LOAD["id"],
            revision=LOAD["revision"],
            variant=LOAD["variant"],
            load_ms=1.0,
            engine_version="x",
        )
        start = next(m for m in self.role.sent if m["type"] == "meeting.job.start")
        self.role.emit(
            "meeting.job.accepted",
            req=start["req"],
            request_type="meeting.job.start",
            meeting_id=MID,
            meeting_generation=start["meeting_generation"],
            resume_from={"mic_ms": 0, "far_ms": 0},
        )
        self.role.emit(
            "meeting.job.progress",
            meeting_id=MID,
            meeting_generation=start["meeting_generation"],
            stage="asr",
            done_ms=720_000,
            total_ms=1_800_000,
        )
        self.recording.tick()

    def save(self, mid: str = MID) -> None:
        """Capture finalized the meeting: the controller's final write is durable."""
        controller = self.recording.controller
        controller.meeting, controller._committed, controller.saved = self.meeting(mid), True, True


@pytest.fixture
def rig(tmp_path: Path) -> Iterator[Rig]:
    from helpers.qt_app import get_qapplication

    get_qapplication()
    instance = Rig(tmp_path)
    yield instance
    instance.bridge.shutdown()


def test_stop_queues_once_and_row_shows_percent_with_progress_bar(rig: Rig) -> None:
    rig.create()
    rig.save()
    rig.start_running()
    rig.recording.tick()
    assert rig.role.types().count("meeting.job.start") == 1  # the edge queues only once
    row = rig.refresh()[MID]
    assert row["badge"] == "Расшифровка · 40 %" and row["progress"] == 40
    assert row["action"] == "Отменить" and row["actionEnabled"] and row["transcribing"]
    assert rig.bridge.transcriptionPercent == 40


def test_immediately_off_keeps_recorded_and_transcribe_click_starts(rig: Rig) -> None:
    rig.create()
    rig.immediately = False
    rig.save()
    rig.recording.tick()
    row = rig.refresh()[MID]
    assert row["badge"] == "Записана" and row["action"] == "Расшифровать"
    assert row["actionEnabled"] and rig.roles == []
    rig.bridge.requestMeetingAction(MID, "Расшифровать")
    assert rig.meeting().state == MeetingState.TRANSCRIBING
    rig.recording.tick()
    assert rig.role.types() == ["model.load"]


def test_rows_for_waiting_needs_model_failed_cancelled_and_done(rig: Rig) -> None:
    rig.create("2026-11-01_100000-wait", MeetingState.WAITING_MEMORY)
    rig.create("2026-11-01_110000-need", MeetingState.NEEDS_MODEL)
    rig.create("2026-11-01_120000-fail", MeetingState.FAILED)
    rig.create("2026-11-01_130000-canc", MeetingState.RECORDED, cancelled=True)
    rig.create("2026-11-01_140000-done", MeetingState.DONE)
    rig.jobs.failures["2026-11-01_120000-fail"] = "model"
    rows = rig.refresh()
    wait = rows["2026-11-01_100000-wait"]
    assert (wait["badge"], wait["action"]) == ("Ждёт памяти", "")
    assert wait["subtitle"] == "Расшифровка подождёт: сейчас мало свободной памяти"
    assert wait["actions"] == ["Отменить", "Удалить"]
    need = rows["2026-11-01_110000-need"]
    assert (need["badge"], need["action"]) == ("Нужна модель", "Установить")
    assert need["subtitle"] == "Для расшифровки встреч нужна модель GigaAM v3"
    fail = rows["2026-11-01_120000-fail"]
    assert (fail["badge"], fail["action"]) == ("! Ошибка расшифровки", "Повторить")
    assert fail["subtitle"] == "Распознавание на этом компьютере не работает"
    cancelled = rows["2026-11-01_130000-canc"]
    assert (cancelled["badge"], cancelled["action"]) == ("Записана", "Расшифровать")
    assert cancelled["subtitle"] == "Расшифровка отменена. Запись сохранена — можно начать снова."
    done = rows["2026-11-01_140000-done"]
    assert (done["badge"], done["action"]) == ("✓ Готово", "Открыть")


def test_cancel_from_the_list_records_cancelled_before_job_cancel(rig: Rig) -> None:
    rig.create()
    rig.save()
    rig.start_running()
    rig.bridge.requestMeetingAction(MID, "Отменить")
    assert rig.meeting().cancelled and rig.meeting().state == MeetingState.RECORDED
    assert rig.role.types()[-1] == "meeting.job.cancel"
    row = rig.refresh()[MID]
    assert row["subtitle"] == "Расшифровка отменена. Запись сохранена — можно начать снова."


def test_exit_while_transcribing_needs_no_confirmation_and_sends_no_cancel_t478(
    rig: Rig,
) -> None:
    rig.create()
    rig.save()
    rig.start_running()
    assert rig.jobs.busy and not rig.bridge._exit_needs_confirmation()
    quit_app = Mock()
    rig.bridge.request_quit(quit_app)
    quit_app.assert_called_once_with()
    assert not rig.bridge.exitConfirmOpen
    rig.bridge.shutdown()
    assert rig.role.stops == 1 and "meeting.job.cancel" not in rig.role.types()
    assert rig.meeting().state == MeetingState.TRANSCRIBING


def test_exit_right_after_a_saved_recording_queues_it_durably(rig: Rig) -> None:
    rig.create()
    rig.save()
    rig.bridge.shutdown()  # no tick in between: exit during finalization
    assert rig.meeting().state == MeetingState.TRANSCRIBING and rig.roles == []


def test_tray_hint_has_a_second_transcription_line(harness: Harness) -> None:
    harness.provider.tooltip.side_effect = TrayIconProvider(SessionKind.OTHER).tooltip
    harness.tray.set_meeting_transcription(40)
    first, second = harness.icon.setToolTip.call_args.args[0].split("\n")
    assert first.startswith("Astra Voice — готов · ")
    assert second == "Расшифровка встречи: 40 %"
    harness.tray.set_meeting_transcription(None)
    assert harness.icon.setToolTip.call_args.args[0] == first


def test_tray_wiring_publishes_transcription_percent() -> None:
    tray = Mock()
    bridge = Mock(enabled=True, recordingDevEnabled=True, transcriptionPercent=40)
    bridge.overlay = {"active": False}
    disconnect = app._wire_tray_meetings(tray, bridge, Mock())
    try:
        tray.set_meeting_transcription.assert_called_with(40)
        bridge.transcriptionPercent = -1
        bridge.changed.connect.call_args.args[0]()
        tray.set_meeting_transcription.assert_called_with(None)
    finally:
        disconnect()


def test_job_notices_use_fixed_spec_texts() -> None:
    notices = SimpleNamespace(
        **{
            name: Mock(name=name)
            for name in (
                "notify_meeting_transcribed",
                "notify_meeting_memory_wait",
                "notify_meeting_needs_model",
                "notify_meeting_selfcheck_failed",
                "notify_meeting_transcription_failed",
            )
        }
    )
    deliver = job_notifier(notices)  # type: ignore[arg-type]
    meeting = Meeting(MID, "2026-11-01T12:00:00+00:00", title="Встреча 24.09.2026 14:00")
    deliver("done", meeting)
    deliver("waiting_memory", meeting)
    deliver("needs_model", meeting)
    deliver("failed_model", meeting)
    deliver("failed", meeting)
    notices.notify_meeting_transcribed.assert_called_once_with()
    notices.notify_meeting_memory_wait.assert_called_once_with()
    notices.notify_meeting_needs_model.assert_called_once_with()
    notices.notify_meeting_selfcheck_failed.assert_called_once_with()
    notices.notify_meeting_transcription_failed.assert_called_once_with()
    assert notify.MEETING_SAVED_TRANSCRIBING == "Встреча сохранена. Делаю расшифровку…"
    assert notify.MEETING_MEMORY_WAIT == "Расшифровка подождёт: сейчас мало свободной памяти"
    assert notify.MEETING_NEEDS_MODEL == "Для расшифровки встреч нужна модель GigaAM v3"
    assert notify.MEETING_SELFCHECK_FAILED == "Распознавание на этом компьютере не работает"


@pytest.mark.parametrize("immediately", [True, False])
def test_stop_notice_says_transcribing_only_when_queued(immediately: bool) -> None:
    from test_meeting_notifications import WRAPPERS, Recording

    recording = Recording()
    recording.jobs = SimpleNamespace(transcribe_immediately=lambda: immediately)
    notices = SimpleNamespace(
        **{name: getattr(notify, name) for name in dir(notify) if name.isupper()},
        **{name: Mock(name=name) for name in WRAPPERS},
        set_action_handler=lambda key, callback: None,
        set_delivery_handler=lambda callback: None,
    )
    owner = MeetingNotifications(recording, notices=notices)  # type: ignore[arg-type]
    recording.begin()
    recording.stop("stop")
    assert notices.notify_meeting_saved_transcribing.called is immediately
    assert notices.notify_meeting_saved.called is not immediately
    owner.shutdown()


def test_recover_on_initialize_resumes_and_deleting_rows_hide(rig: Rig) -> None:
    rig.create(MID, MeetingState.TRANSCRIBING, meeting_generation=2)
    rig.recording.initialize()
    assert rig.role.types() == ["model.load"]
    released: list[str] = []
    rig.jobs.delete(MID, lambda: released.append(MID))
    rig.recording.tick()
    assert released == [MID]
    assert MID not in rig.refresh()
