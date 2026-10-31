"""E13 M1: human opt-in, fail-closed UI policy and asynchronous listing."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest
from PyQt5.QtCore import QCoreApplication
from PyQt5.QtTest import QSignalSpy

from astra_voice.core.policy import Policy, PolicyStatus
from astra_voice.core.settings import Settings
from astra_voice.meetings.apps import AppClass, AppDefinition
from astra_voice.meetings.events import MeetingState
from astra_voice.meetings.model import Audio, Mark, Meeting
from astra_voice.platform.session import SessionKind
from astra_voice.ui.meeting_bridge import MeetingBridge
from astra_voice.ui.meeting_texts import RESPONSIBILITY_ITEMS, TEXTS

pytestmark = pytest.mark.unit
STAMP = "2026-10-30T12:00:00Z"
APP = AppDefinition("dion", "DION", ("dion",), (), AppClass.DION)


@pytest.fixture(scope="module", autouse=True)
def application() -> QCoreApplication:
    from helpers.qt_app import get_qapplication

    return get_qapplication()


@pytest.fixture
def bridge_factory() -> Iterator[Callable[..., MeetingBridge]]:
    bridges: list[MeetingBridge] = []

    def create(**kwargs: object) -> MeetingBridge:
        options: dict[str, Any] = {
            "store": Mock(list=Mock(return_value=[])),
            "save": Mock(),
            "known_apps": (APP,),
        }
        options.update(kwargs)
        settings = options.pop("settings", Settings())
        assert isinstance(settings, Settings)
        bridge = MeetingBridge(settings, **options)
        bridges.append(bridge)
        return bridge

    yield create
    for bridge in bridges:
        bridge.shutdown()


def drain(bridge: MeetingBridge, tasks: list[Callable[[], None]]) -> None:
    tasks.pop(0)()
    bridge._poll()


@pytest.mark.parametrize("enabled,expected", [(False, "off"), (True, "empty")])
def test_default_states_and_no_constructor_store_io(
    bridge_factory: Callable[..., MeetingBridge], enabled: bool, expected: str
) -> None:
    store = Mock(list=Mock(return_value=[]))
    bridge = bridge_factory(settings=Settings(meetings_enabled=enabled), store=store)
    assert bridge.sectionState == expected
    store.list.assert_not_called()


@pytest.mark.parametrize("value", ["off", "garbage"])
def test_policy_off_never_opens_d1_or_enables(
    bridge_factory: Callable[..., MeetingBridge], value: str
) -> None:
    bridge = bridge_factory(policy=Policy(raw={"meeting_record": value}))
    assert bridge.sectionState == "policy-off" and not bridge.canEnable
    bridge.openResponsibility()
    bridge.acceptResponsibility(True)
    assert not bridge.responsibilityOpen and not bridge.enabled
    assert bool(bridge.policyInvalidText) == (value == "garbage")


@pytest.mark.parametrize("mode", [None, "prompt", "manual"])
def test_only_open_d1_acceptance_can_enable_and_each_enable_needs_d1(
    bridge_factory: Callable[..., MeetingBridge], mode: str | None
) -> None:
    saved = Mock()
    bridge = bridge_factory(
        save=saved, policy=Policy(raw={} if mode is None else {"meeting_record": mode})
    )
    bridge.setSetting("meetings_enabled", True)
    bridge.acceptResponsibility(True)
    assert not bridge.enabled and not saved.called
    bridge.openResponsibility()
    assert bridge.responsibilityOpen
    bridge.cancelResponsibility()
    assert not bridge.enabled
    bridge.openResponsibility()
    bridge.acceptResponsibility(True)
    assert bridge.enabled and not bridge.responsibilityOpen
    assert bridge.settings["meeting_offer_on_call"] == (mode != "manual")
    bridge.disableMeetings()
    assert not bridge.enabled
    bridge.acceptResponsibility(True)
    assert not bridge.enabled


def test_d1_default_detector_and_no_known_apps(
    bridge_factory: Callable[..., MeetingBridge],
) -> None:
    bridge = bridge_factory(known_apps=())
    assert not bridge.detectorVisible and not bridge.detectorEnabled
    bridge.openResponsibility()
    bridge.acceptResponsibility(True)
    assert not bridge.settings["meeting_offer_on_call"]
    manual = bridge_factory(policy=Policy(raw={"meeting_record": "manual"}))
    assert manual.detectorVisible and not manual.detectorEnabled
    assert manual.detectorCaption == TEXTS["d1ManualCaption"]


def test_policy_change_while_d1_open_revokes_acceptance(
    bridge_factory: Callable[..., MeetingBridge],
) -> None:
    bridge = bridge_factory()
    bridge.openResponsibility()
    bridge.refreshPolicy(Policy(status=PolicyStatus.INVALID))
    bridge.acceptResponsibility(False)
    assert not bridge.enabled and not bridge.responsibilityOpen


def test_policy_some_uses_effective_values_and_refuses_locked_edits(
    bridge_factory: Callable[..., MeetingBridge],
) -> None:
    saved = Mock()
    bridge = bridge_factory(
        settings=Settings(meetings_enabled=True, meeting_diarization=True),
        policy=Policy(raw={"meeting_diarization": "off", "meeting_audio_retention_days": "14"}),
        save=saved,
    )
    assert bridge.sectionState == "policy-some"
    assert bridge.policyNote == TEXTS["policyNote"]
    assert bridge.settings["meeting_diarization"] is False
    assert bridge.settings["meeting_audio_retention_days"] == 14
    assert bridge.locked["meeting_diarization"]
    bridge.setSetting("meeting_diarization", True)
    assert not saved.called and not bridge.settings["meeting_diarization"]


def test_settings_save_rollback_and_mirror_only_after_success(
    bridge_factory: Callable[..., MeetingBridge],
) -> None:
    stored, mirror = Settings(meetings_enabled=True), Settings(meetings_enabled=True)
    save = Mock(side_effect=OSError("private"))
    bridge = bridge_factory(settings=stored, mirror=mirror, save=save)
    bridge.setSetting("meeting_self_label", "Имя")
    assert stored.meeting_self_label == mirror.meeting_self_label == "Я"
    save.side_effect = None
    bridge.setSetting("meeting_self_label", "Имя")
    assert stored.meeting_self_label == mirror.meeting_self_label == "Имя"
    original_autostart = stored.autostart
    bridge.setSetting("autostart", not original_autostart)
    assert stored.autostart == original_autostart


def test_disable_preserves_saved_meetings_and_never_calls_store_mutation(
    bridge_factory: Callable[..., MeetingBridge],
) -> None:
    tasks: list[Callable[[], None]] = []
    meeting = Meeting("20261030T120000-abcdef", STAMP, title="Запись")
    store = Mock(list=Mock(return_value=[meeting]))
    bridge = bridge_factory(
        settings=Settings(meetings_enabled=True), store=store, submit=tasks.append
    )
    bridge.refresh()
    drain(bridge, tasks)
    before = bridge.meetings
    bridge.disableMeetings()
    assert bridge.sectionState == "off-saved"
    assert [(row["id"], row["title"]) for row in bridge.meetings] == [
        (row["id"], row["title"]) for row in before
    ]
    assert [call[0] for call in store.mock_calls] == ["list"]


def test_load_threshold_error_retry_and_shutdown_drop_late_reply(
    bridge_factory: Callable[..., MeetingBridge],
) -> None:
    tasks: list[Callable[[], None]] = []
    clock = Mock(return_value=10.0)
    store = Mock(list=Mock(side_effect=OSError("secret path")))
    bridge = bridge_factory(
        settings=Settings(meetings_enabled=True), store=store, submit=tasks.append, clock=clock
    )
    bridge.refresh()
    assert bridge.sectionState == "loading" and not bridge.loadingVisible
    clock.return_value = 10.3
    bridge._poll()
    assert not bridge.loadingVisible
    clock.return_value = 10.301
    bridge._poll()
    assert bridge.loadingVisible
    drain(bridge, tasks)
    assert bridge.sectionState == "error"
    store.list.side_effect = None
    store.list.return_value = []
    bridge.retry()
    drain(bridge, tasks)
    assert bridge.sectionState == "empty"
    bridge.refresh()
    bridge.shutdown()
    drain(bridge, tasks)
    assert bridge.meetings == [] and not bridge.loadingVisible


@pytest.mark.parametrize(
    "state,badge,action",
    [
        (MeetingState.RECORDING, "Запись", "Остановить"),
        (MeetingState.PAUSED, "Пауза", "Продолжить"),
        (MeetingState.RECORDED, "Записана", "Расшифровать"),
        (MeetingState.TRANSCRIBING, "Расшифровка", "Отменить"),
        (MeetingState.WAITING_MEMORY, "Ждёт памяти", ""),
        (MeetingState.NEEDS_MODEL, "Нужна модель", "Установить"),
        (MeetingState.DONE, "Готово", "Открыть"),
        (MeetingState.FAILED, "Ошибка расшифровки", "Повторить"),
        (MeetingState.INTERRUPTED, "Прервана", "Расшифровать"),
    ],
)
def test_meeting_rows_follow_state_table(
    bridge_factory: Callable[..., MeetingBridge], state: MeetingState, badge: str, action: str
) -> None:
    tasks: list[Callable[[], None]] = []
    row = Meeting(
        "20261030T120000-abcdef", STAMP, state=state, recorded_ms=3000, size_bytes=2 * 1024 * 1024
    )
    bridge = bridge_factory(
        settings=Settings(meetings_enabled=True),
        store=Mock(list=Mock(return_value=[row])),
        submit=tasks.append,
    )
    bridge.refresh()
    drain(bridge, tasks)
    assert badge in bridge.meetings[0]["badge"] and bridge.meetings[0]["action"] == action
    assert bridge.totalText == "Всего: 1 встреча · 2 МБ"


def test_policy_off_saved_rows_and_pause_lock_and_audio_deleted(
    bridge_factory: Callable[..., MeetingBridge],
) -> None:
    tasks: list[Callable[[], None]] = []
    done = Meeting(
        "20261030T120000-abcdef", STAMP, state=MeetingState.DONE, audio=Audio(present=False)
    )
    paused = Meeting(
        "20261030T120001-abcdef", STAMP, state=MeetingState.PAUSED, marks=[Mark(reason="lock")]
    )
    bridge = bridge_factory(
        policy=Policy(raw={"meeting_record": "off"}),
        store=Mock(list=Mock(return_value=[done, paused])),
        submit=tasks.append,
    )
    bridge.refresh()
    drain(bridge, tasks)
    rows = {row["id"]: row for row in bridge.meetings}
    assert rows[done.id]["action"] == "Открыть"
    assert "звук удалён" in rows[done.id]["subtitle"]
    assert rows[paused.id]["action"] == "" and "Расшифровать" not in str(rows)


def test_recording_flag_gates_disable_and_emits_only_stub(
    bridge_factory: Callable[..., MeetingBridge],
) -> None:
    bridge = bridge_factory(settings=Settings(meetings_enabled=True))
    spy = QSignalSpy(bridge.recordingRequested)
    bridge.requestRecording()
    assert len(spy) == 1
    assert bridge.setProperty("recording", True)
    bridge.requestRecording()
    bridge.disableMeetings()
    assert len(spy) == 1 and bridge.enabled and bridge.sectionState == "recording"


def test_d1_organization_and_fly_warning(
    bridge_factory: Callable[..., MeetingBridge],
) -> None:
    personal = bridge_factory()
    organization = bridge_factory(
        policy=Policy(raw={"meeting_diarization": "off"}), session=SessionKind.FLY
    )
    assert personal.responsibilityParagraph == TEXTS["d1Personal"]
    assert organization.responsibilityParagraph == TEXTS["d1Organization"]
    assert not personal.flyWarning
    assert organization.flyWarning == "Во время записи блокировка экрана не ставит запись на паузу"


def test_literal_strings_match_design_spec() -> None:
    spec = (Path(__file__).resolve().parents[2] / "design/meetings/spec.md").read_text()
    # The spec nests „…“ inside quoted UI text; the UI uses standalone «…».
    flat = re.sub(r"\s+", " ", spec).replace("„", "«").replace("“", "»")
    for key, text in TEXTS.items():
        if key == "flyWarning":
            continue  # Owner's 29.10 decision supersedes the earlier Fly table.
        assert re.sub(r"\s+", " ", text) in flat, key
    for item in RESPONSIBILITY_ITEMS:
        item_title, item_text = item["title"], item["text"]
        assert isinstance(item_title, str) and isinstance(item_text, str)
        assert item_title in flat and item_text in flat


def test_rows_sort_by_instant_then_live_priority(
    bridge_factory: Callable[..., MeetingBridge],
) -> None:
    tasks: list[Callable[[], None]] = []
    older = Meeting("20261030T120000-abcdef", "2026-10-30T12:00:00+03:00", title="Older")
    newer = Meeting("20261030T110000-abcdef", "2026-10-30T11:00:00Z", title="Newer")
    live = Meeting("20261029T120000-abcdef", "2026-10-29T12:00:00Z", state=MeetingState.RECORDING)
    bridge = bridge_factory(
        settings=Settings(meetings_enabled=True),
        store=Mock(list=Mock(return_value=[older, newer, live])),
        submit=tasks.append,
    )
    bridge.refresh()
    drain(bridge, tasks)
    assert [row["id"] for row in bridge.meetings] == [live.id, newer.id, older.id]


@pytest.mark.parametrize("source", ["mirror", "secure"])
def test_endpoint_caption_respects_effective_offline_and_secure(
    bridge_factory: Callable[..., MeetingBridge], source: str
) -> None:
    bridge = bridge_factory(
        settings=Settings(meetings_enabled=True, meeting_ai_endpoint="https://api.example.org/v1"),
        mirror=Settings(offline=source == "mirror"),
        policy=Policy(raw={"profile": "secure"} if source == "secure" else {}),
    )
    assert bridge.endpointCaption == "Внешний адрес недоступен в офлайн-режиме"


def test_retention_date_overflow_omits_date_for_schema_valid_meeting(
    bridge_factory: Callable[..., MeetingBridge],
) -> None:
    from astra_voice.meetings.model import Transcription

    tasks: list[Callable[[], None]] = []
    meeting = Meeting(
        "2026-10-30_120000-ab12",
        STAMP,
        state=MeetingState.DONE,
        audio=Audio(present=True, retain_days=365),
        transcription=Transcription(finished_at="9999-12-31T12:00:00Z"),
    )
    assert meeting.to_dict()["transcription"]["finished_at"] == "9999-12-31T12:00:00Z"
    bridge = bridge_factory(
        settings=Settings(meetings_enabled=True),
        store=Mock(list=Mock(return_value=[meeting])),
        submit=tasks.append,
    )
    bridge.refresh()
    drain(bridge, tasks)
    assert "звук до" not in bridge.meetings[0]["subtitle"]
    assert bridge.meetings[0]["badge"] == "✓ Готово"


@pytest.mark.parametrize("started_at", ["0001-01-01T00:00:00+01:00", "9999-12-31T23:59:59-01:00"])
def test_extreme_valid_start_dates_do_not_break_list_presentation(
    bridge_factory: Callable[..., MeetingBridge], started_at: str
) -> None:
    meeting = Meeting(id="2026-10-30_120000-ab12", started_at=started_at)
    meeting.to_dict()
    bridge = bridge_factory(settings=Settings(meetings_enabled=True))
    bridge._documents = [meeting]
    assert bridge.meetings[0]["title"] == meeting.title
    assert bridge.meetings[0]["subtitle"]
