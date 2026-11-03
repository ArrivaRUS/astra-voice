"""E13.M4 card A in the bridge: rows, live text, panels, failure after restart, «Сохранить как…»."""

from __future__ import annotations

import os
import stat
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest
from test_meeting_jobs_ui import MID, Rig
from test_tray import qapp as qapp

from astra_voice import app
from astra_voice.core.meeting_jobs import ModelAdmission
from astra_voice.meetings.events import MeetingState
from astra_voice.meetings.model import Mark, Transcript, Utterance
from astra_voice.meetings.store import Writer
from astra_voice.meetings.templates import DAMAGED_TEXT

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("filesystem_euid")]


class SaveRig(Rig):
    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.documents = root / "Документы"
        self.documents.mkdir()
        self.picked: list[tuple[str, str]] = []
        self.answer: str | None = None
        self.opened: list[str] = []
        # The rig's bridge is already bound to the recording; inject the dialog seams.
        self.bridge._pick_save_path = self.pick
        self.bridge._documents_dir = lambda: self.documents
        self.bridge._open_folder = self.open_folder

    def create(
        self, mid: str = MID, state: MeetingState = MeetingState.RECORDED, **kw: Any
    ) -> None:
        super().create(mid, state, **{"variant": "app_stream", "app_class": "dion", **kw})

    def open_folder(self, path: str) -> bool:
        self.opened.append(path)
        return True

    def pick(self, suggested: str, fmt: str) -> str | None:
        self.picked.append((suggested, fmt))
        return self.answer

    def transcript(self, rows: list[tuple[int, int, str, str]], *, echo: int = -1) -> None:
        generation = self.meeting().meeting_generation
        self.store.write(
            MID,
            "transcript.json",
            Transcript(
                meeting_id=MID,
                meeting_generation=generation,
                utterances=[
                    Utterance(
                        id=f"u{i:05d}",
                        seq=i,
                        track="mic" if who == "me" else "far",
                        start_ms=start,
                        end_ms=end,
                        text=text,
                        speaker=who,
                        final=True,
                        echo=i == echo,
                    )
                    for i, (start, end, who, text) in enumerate(rows)
                ],
            ),
            writer=Writer.TRANSCRIBE,
        )

    def open_card(self, mid: str = MID) -> dict[str, Any]:
        self.refresh()
        self.bridge.openMeeting(mid)
        while self.tasks:
            self.tasks.pop(0)()
        self.bridge._card_poll()
        return dict(self.bridge.card)


@pytest.fixture
def rig(tmp_path: Path) -> Iterator[SaveRig]:
    from helpers.qt_app import get_qapplication

    get_qapplication()
    instance = SaveRig(tmp_path)
    yield instance
    instance.bridge.shutdown()


ROWS = [
    (4_000, 9_000, "me", "Коллеги, добрый день."),
    (12_000, 20_000, "others", "<b>Добрый</b> & день"),
    (21_000, 22_000, "others", "   "),
    (23_000, 25_000, "me", "эхо"),
    (2_400_000, 2_410_000, "others", DAMAGED_TEXT),
]


def test_done_card_rows_hide_echo_and_empty_and_turn_placeholders_into_marks(
    rig: SaveRig,
) -> None:
    rig.create(
        state=MeetingState.DONE, marks=[Mark(10_000, "mic_gap_start"), Mark(11_000, "mic_gap_end")]
    )
    rig.transcript(ROWS, echo=3)
    card = rig.open_card()
    assert card["view"] == "ready" and card["showActions"] and not card["live"]
    rows = rig.bridge.cardRows.rows()
    assert [(row["kind"], row["time"], row["who"], row["text"]) for row in rows] == [
        ("utterance", "[0:00:04]", "Я", "Коллеги, добрый день."),
        ("mark", "0:00:10", "", "Ваш микрофон не записывался: 0:00:10–0:00:11"),
        ("utterance", "[0:00:12]", "Собеседники", "<b>Добрый</b> & день"),
        ("mark", "0:40:00", "", "Кусок записи повреждён 0:40:00–0:40:10"),
    ]
    assert rows[2]["accessibleName"] == "0:00:12, Собеседники: <b>Добрый</b> & день"
    assert [mark["text"] for mark in card["marks"]] == [
        "Ваш микрофон не записывался 0:00:10–0:00:11",
        "Кусок записи повреждён 0:40:00–0:40:10",
    ]
    assert card["badge"] == {"text": "Готово", "kind": "success", "icon": "check"}
    assert card["subtitle"]["mono"] == "0:30:00"
    assert card["participants"] == [
        {"kind": "me", "label": "Я", "minutes": "1 мин"},
        {"kind": "others", "label": "Собеседники", "minutes": "1 мин"},
    ]
    rig.bridge.closeMeeting()
    assert not rig.bridge.cardOpen and rig.bridge.cardRows.rowCount() == 0


def test_done_without_speech_is_the_empty_panel(rig: SaveRig) -> None:
    rig.create(state=MeetingState.DONE)
    rig.transcript([(0, 1000, "others", "")])
    card = rig.open_card()
    assert card["view"] == "empty" and not card["showTable"]
    assert card["panel"]["title"] == "В записи не нашлось речи"


def test_open_from_the_list_action_and_save_request(rig: SaveRig) -> None:
    rig.create(state=MeetingState.DONE)
    rig.transcript(ROWS[:1])
    rig.refresh()
    rig.bridge.requestMeetingAction(MID, "Открыть")
    assert rig.bridge.cardOpen
    saves = Mock()
    rig.bridge.cardSaveRequested.connect(saves)
    rig.bridge.requestMeetingAction(MID, "Сохранить как…")
    saves.assert_called_once_with()
    assert rig.bridge.meetings[0]["openable"]


def test_live_rows_are_appended_without_reset(rig: SaveRig) -> None:
    rig.create(marks=[Mark(30_000, "mic_gap_start"), Mark(31_000, "mic_gap_end")])
    rig.save()
    rig.start_running()
    card = rig.open_card()
    assert card["view"] == "transcribing" and card["live"]
    assert card["badge"]["text"] == "Расшифровка · 40 %"
    assert (card["progressDone"], card["progressTotal"]) == ("0:12:00", "0:30:00")
    assert card["subtitle"]["suffix"].endswith("звук хранится до конца расшифровки")
    assert [p["minutes"] for p in card["participants"]] == ["0 мин", "—"]
    model = rig.bridge.cardRows
    resets, inserts = Mock(), []
    model.modelReset.connect(resets)
    model.rowsInserted.connect(lambda parent, first, last: inserts.append((first, last)))
    start = next(m for m in rig.role.sent if m["type"] == "meeting.job.start")
    snapshots: list[str] = []
    original = rig.jobs.job_snapshot

    def counted(meeting_id: str) -> dict[str, Any] | None:
        snapshots.append(meeting_id)
        return original(meeting_id)

    rig.jobs.job_snapshot = counted  # type: ignore[method-assign]
    for seq in range(200):
        rig.role.emit(
            "meeting.job.utterance",
            meeting_id=MID,
            meeting_generation=start["meeting_generation"],
            seq=seq,
            id=f"u{seq:05d}",
            track="far",
            start_ms=seq * 1000,
            end_ms=seq * 1000 + 500,
            text=f"реплика {seq}",
            speaker="others",
        )
        rig.recording.tick()
        for _binding in range(3):  # several QML bindings read the card per change
            assert rig.bridge.card["live"]
    # One copy of the running job per change, not one per binding (≤ 9000 replies).
    assert len(snapshots) <= 200
    resets.assert_not_called()
    assert model.rowCount() == 201  # 200 replies + the microphone mark at 0:00:30
    assert inserts[0] == (0, 0) and len(inserts) == 200
    assert model.rows()[30]["kind"] == "mark" and model.rows()[31]["text"] == "реплика 30"


def test_failure_reason_survives_a_restart(rig: SaveRig, tmp_path: Path) -> None:
    rig.create()
    rig.save()
    rig.admission = ModelAdmission("broken", None)
    rig.recording.tick()
    assert rig.meeting().state == MeetingState.FAILED
    assert rig.meeting().transcription.failure == "model"
    # A new process: the in-memory failure map is empty, the disk explains the error.
    rig.jobs.failures.clear()
    row = rig.refresh()[MID]
    assert row["subtitle"] == "Распознавание на этом компьютере не работает"
    card = rig.open_card()
    assert card["view"] == "error"
    assert card["panel"]["title"] == "Распознавание на этом компьютере не работает"
    assert [b["label"] for b in card["panel"]["buttons"]] == ["Повторить", "Подробнее"]
    # «Повторить» starts over and clears the stored reason.
    rig.admission = ModelAdmission("ok", rig.admission.load)
    rig.bridge.cardAction("Повторить")
    assert rig.meeting().transcription.failure is None


def test_other_failures_show_the_general_text(rig: SaveRig) -> None:
    rig.create(state=MeetingState.FAILED)
    meeting = rig.meeting()
    meeting.transcription.failure = "io"
    card_meeting = meeting  # stored by the test as a previous process would have
    rig.store.write(MID, "meeting.json", card_meeting, writer=Writer.GUI)
    card = rig.open_card()
    assert card["panel"]["title"] == "Ошибка расшифровки"
    assert [b["label"] for b in card["panel"]["buttons"]] == ["Повторить"]


@pytest.mark.parametrize(
    ("state", "kw", "view", "title", "buttons"),
    [
        (
            MeetingState.RECORDED,
            {"cancelled": True},
            "cancelled",
            "Расшифровка отменена",
            ["Расшифровать", "Удалить встречу…"],
        ),
        (
            MeetingState.NEEDS_MODEL,
            {},
            "no-model",
            "Для расшифровки встреч нужна модель GigaAM v3",
            ["Установить", "Установить из файла…"],
        ),
        (
            MeetingState.WAITING_MEMORY,
            {},
            "wait-memory",
            "Расшифровка подождёт: сейчас мало свободной памяти",
            ["Отменить"],
        ),
        (
            MeetingState.INTERRUPTED,
            {},
            "interrupted",
            "Запись встречи прервалась. Сохранено ",
            ["Расшифровать", "Удалить…"],
        ),
    ],
)
def test_state_panels(
    rig: SaveRig,
    state: MeetingState,
    kw: dict[str, Any],
    view: str,
    title: str,
    buttons: list[str],
) -> None:
    rig.create(state=state, **kw)
    card = rig.open_card()
    assert card["view"] == view and not card["showTable"] and card["marks"] == []
    assert card["panel"]["title"] == title
    assert [b["label"] for b in card["panel"]["buttons"]] == buttons
    assert [b["enabled"] for b in card["panel"]["buttons"]][-1] is (
        view != "cancelled" and view != "interrupted"
    )


def test_model_buttons_go_to_the_models_section(rig: SaveRig) -> None:
    rig.create(state=MeetingState.NEEDS_MODEL)
    rig.open_card()
    actions = Mock()
    rig.bridge.meetingActionRequested.connect(actions)
    for action in ("Установить", "Установить из файла…", "Подробнее"):
        rig.bridge.cardAction(action)
    assert [call.args for call in actions.call_args_list] == [
        (MID, "Установить"),
        (MID, "Установить из файла…"),
        (MID, "Подробнее"),
    ]
    focuser, info, settings = Mock(), Mock(), Mock()
    handle = app._meeting_model_action(focuser, info, settings)
    handle(MID, "Установить")
    handle(MID, "Открыть")
    handle(MID, "Установить из файла…")
    assert [c.args for c in info.show_section.call_args_list] == [("models",), ("models",)]
    settings.pickInstallPath.assert_called_once_with()


def test_save_as_writes_a_private_file_and_shows_the_toast(rig: SaveRig) -> None:
    rig.create(state=MeetingState.DONE, title="Планёрка / бюджет")
    rig.transcript(ROWS[:2])
    rig.open_card()
    rig.answer = None
    assert rig.bridge.saveAs("md") == "cancelled"
    assert rig.picked == [(str(rig.documents / "Планёрка бюджет.md"), "md")]
    target = rig.documents / "итог"
    rig.answer = str(target)
    old = os.umask(0)
    try:
        assert rig.bridge.saveAs("txt") == "saved"
    finally:
        os.umask(old)
    written = rig.documents / "итог.txt"
    assert stat.S_IMODE(written.stat().st_mode) == 0o600
    text = written.read_text(encoding="utf-8")
    assert "[0:00:12] Собеседники: <b>Добрый</b> & день" in text
    assert rig.bridge.toast["folder"] == "Документы" and rig.bridge.toast["file"] == "итог.txt"
    serial = rig.bridge.toast["serial"]
    rig.bridge.openSavedFolder()
    assert rig.opened == [str(rig.documents)]
    # The dialog confirmed this existing name: replaced, still 0600.
    rig.answer = str(written)
    assert rig.bridge.saveAs("txt") == "saved" and rig.bridge.toast["serial"] == serial + 1
    # A name that only exists after the extension was added was never confirmed.
    (rig.documents / "новое.md").write_text("старое", encoding="utf-8")
    rig.answer = str(rig.documents / "новое")
    assert rig.bridge.saveAs("md") == "error"
    assert rig.bridge.cardSaveError == "Не удалось сохранить файл: папка недоступна для записи"
    assert (rig.documents / "новое.md").read_text(encoding="utf-8") == "старое"
    rig.answer = str(rig.documents / "нет" / "файл.md")
    assert rig.bridge.saveAs("md") == "error"


def test_save_needs_a_loaded_done_meeting(rig: SaveRig) -> None:
    rig.create(state=MeetingState.NEEDS_MODEL)
    rig.open_card()
    rig.answer = str(rig.documents / "x.md")
    assert rig.bridge.saveAs("md") == "error" and rig.picked == []
    assert rig.bridge.saveAs("html") == "error"
