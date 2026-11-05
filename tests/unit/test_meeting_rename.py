"""D3: serialized private metadata writes and bridge presentation."""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from PyQt5.QtCore import QThread
from PyQt5.QtTest import QSignalSpy, QTest
from test_meeting_card_bridge import ROWS, SaveRig
from test_meeting_card_bridge import rig as rig
from test_meeting_jobs_ui import MID

from astra_voice.meetings.events import MeetingState
from astra_voice.meetings.model import Meeting
from astra_voice.meetings.templates import sanitize_filename

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("filesystem_euid")]


def finish(rig: SaveRig, spy: QSignalSpy) -> None:
    for _ in range(200):
        rig.jobs._drain()
        if spy:
            return
        QTest.qWait(10)
    pytest.fail("rename did not finish")


@pytest.mark.parametrize("source", ["card", "list"])
def test_rename_from_card_and_list_uses_worker_and_refreshes_export(
    rig: SaveRig, monkeypatch: pytest.MonkeyPatch, source: str
) -> None:
    rig.create(state=MeetingState.DONE, title="Старое")
    rig.transcript(ROWS[:1])
    rig.open_card()
    gui = QThread.currentThread()
    writes = []
    original = rig.store.write

    def write(*args: Any, **kwargs: Any) -> Any:
        if args[1] == "meeting.json":
            assert QThread.currentThread() != gui
            writes.append(args[2].title)
        return original(*args, **kwargs)

    monkeypatch.setattr(rig.store, "write", write)
    rig.jobs._submit = rig.jobs._writer.submit
    requested = QSignalSpy(rig.bridge.renameRequested)
    if source == "card":
        rig.bridge.cardAction("rename")
    else:
        rig.bridge.requestMeetingAction(MID, "Переименовать")
    assert list(requested) == [[MID, "Старое"]]
    completed_on = []
    rig.bridge.renameFinished.connect(lambda result: completed_on.append(QThread.currentThread()))
    spy = QSignalSpy(rig.bridge.renameFinished)
    assert rig.bridge.renameMeeting(MID, "  Новое   е\u0308  ") == "pending"
    finish(rig, spy)
    assert list(spy) == [["saved"]] and completed_on == [gui]
    assert writes == ["Новое ё"]
    meeting = rig.meeting()
    assert meeting.title == rig.bridge.meetings[0]["title"] == rig.bridge.card["title"] == "Новое ё"
    assert rig.bridge.cardTexts["saveTitle"] + meeting.title == "Сохранить как — Новое ё"
    rig.bridge.saveAs("txt")
    assert Path(rig.picked[-1][0]).name == sanitize_filename(meeting.title, "txt")
    rig.bridge._capture_changed()
    assert rig.bridge.card["title"] == "Новое ё"  # no stale capture/jobs overlay


@pytest.mark.parametrize(
    "title,ok",
    [
        ("x", True),
        ("я" * 120, True),
        ("я" * 121, False),
        (" " * 5, False),
        ("a\x00b", False),
        ("a\nb", False),
        ("a\tb", False),
        ("a\u202eb", False),
        ("  " + "я" * 120 + "  ", True),
    ],
)
def test_rename_normalized_boundaries_and_controls(rig: SaveRig, title: str, ok: bool) -> None:
    rig.create(title="Исходное")
    rig.refresh()
    before = (rig.store.root / MID / "meeting.json").read_bytes()
    spy = QSignalSpy(rig.bridge.renameFinished)
    result = rig.bridge.renameMeeting(MID, title)
    if ok:
        assert result == "pending"
        finish(rig, spy)
        assert list(spy) == [["saved"]]
        assert rig.meeting().title == title.strip()
    else:
        assert result == "error"
        assert (rig.store.root / MID / "meeting.json").read_bytes() == before


@pytest.mark.parametrize("state", list(MeetingState))
def test_rename_actions_follow_state_table(rig: SaveRig, state: MeetingState) -> None:
    rig.create(state=state)
    rig.refresh()
    allowed = state in {MeetingState.RECORDED, MeetingState.TRANSCRIBING, MeetingState.DONE}
    assert any("Переименовать" in row["actions"] for row in rig.bridge.meetings) == allowed
    requested = QSignalSpy(rig.bridge.renameRequested)
    rig.bridge.requestMeetingAction(MID, "Переименовать")
    assert bool(requested) == allowed
    if state not in {MeetingState.RECORDING, MeetingState.PAUSED, MeetingState.DELETING}:
        rig.bridge.openMeeting(MID)
        assert rig.bridge.card["renameEnabled"] == allowed
        assert rig.bridge.card["showEdit"] == allowed


@pytest.mark.parametrize(
    "block", ["tombstone", "pending-delete", "deleting", "recording", "paused"]
)
def test_rename_refuses_deleting_and_capture_without_write(rig: SaveRig, block: str) -> None:
    state = {
        "deleting": MeetingState.DELETING,
        "recording": MeetingState.RECORDING,
        "paused": MeetingState.PAUSED,
    }.get(block, MeetingState.RECORDED)
    rig.create(state=state, title="Исходное")
    rig.refresh()
    if block == "tombstone":
        rig.jobs.tombstones.add(MID)
    if block == "pending-delete":
        rig.bridge._delete_pending[MID] = rig.meeting()
    before = (rig.store.root / MID / "meeting.json").read_bytes()
    assert rig.bridge.renameMeeting(MID, "Новое") == "error"
    results: list[bool] = []
    if block != "pending-delete":
        rig.jobs.rename(MID, "Новое", results.append)
        assert results == [False]
    assert (rig.store.root / MID / "meeting.json").read_bytes() == before
    assert all("Переименовать" not in row["actions"] for row in rig.bridge.meetings)


@pytest.mark.parametrize("title", ["", " ", "x" * 121, "a\x00", "a\n", "a\u202e"])
def test_jobs_rename_rejects_invalid_title_without_write(rig: SaveRig, title: str) -> None:
    rig.create(title="Исходное")
    before = (rig.store.root / MID / "meeting.json").read_bytes()
    done: list[bool] = []
    rig.jobs.rename(MID, title, done.append)
    assert done == [False]
    assert (rig.store.root / MID / "meeting.json").read_bytes() == before


@pytest.mark.parametrize("rename_first", [True, False])
def test_rename_and_status_share_writer_preserving_both_edits(
    rig: SaveRig, rename_first: bool
) -> None:
    rig.create(title="Исходное")
    rig.refresh()
    before = deepcopy(rig.meeting().to_dict())
    rig.jobs._submit = rig.jobs._writer.submit
    done = QSignalSpy(rig.bridge.renameFinished)

    def status(meeting: Meeting) -> bool:
        meeting.state = MeetingState.TRANSCRIBING
        return True

    def update_status() -> Any:
        assert rig.jobs._update(MID, status) is not None
        return lambda: None

    if not rename_first:
        rig.jobs._run(update_status)
    assert rig.bridge.renameMeeting(MID, "Новое") == "pending"
    if rename_first:
        rig.jobs._run(update_status)
    finish(rig, done)
    for _ in range(200):
        rig.jobs._drain()
        if not rig.jobs.filesystem_pending:
            break
        QTest.qWait(10)
    after = rig.meeting().to_dict()
    assert after == {**before, "title": "Новое", "state": "transcribing"}


def test_rename_path_like_title_keeps_id_directory_and_private_file_t433(rig: SaveRig) -> None:
    rig.create(title="Исходное")
    rig.refresh()
    done = QSignalSpy(rig.bridge.renameFinished)
    assert rig.bridge.renameMeeting(MID, "../../x") == "pending"
    finish(rig, done)
    assert rig.meeting().title == "../../x" and rig.meeting().id == MID
    assert sorted(p.name for p in rig.store.root.iterdir() if p.is_dir()) == [MID]
    assert (rig.store.root / MID / "meeting.json").stat().st_mode & 0o777 == 0o600


def test_rename_write_failure_reports_error_and_keeps_title(
    rig: SaveRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig.create(title="Исходное")
    rig.refresh()
    monkeypatch.setattr(rig.jobs, "_update", lambda *args, **kwargs: None)
    done = QSignalSpy(rig.bridge.renameFinished)
    assert rig.bridge.renameMeeting(MID, "Новое") == "pending"
    finish(rig, done)
    assert list(done) == [["error"]]
    assert rig.meeting().title == rig.bridge.meetings[0]["title"] == "Исходное"


def test_rename_queued_before_tombstone_cannot_write(rig: SaveRig) -> None:
    rig.create(title="Исходное")
    rig.refresh()
    before = (rig.store.root / MID / "meeting.json").read_bytes()
    queued: list[Callable[[], None]] = []
    rig.jobs._submit = queued.append
    done = QSignalSpy(rig.bridge.renameFinished)
    assert rig.bridge.renameMeeting(MID, "Новое") == "pending"
    assert rig.bridge.renameMeeting(MID, "Второе") == "error"
    rig.jobs.tombstones.add(MID)
    queued.pop()()
    finish(rig, done)
    assert list(done) == [["error"]]
    assert (rig.store.root / MID / "meeting.json").read_bytes() == before


def test_rename_respects_policy_off_row_whitelist(rig: SaveRig) -> None:
    from astra_voice.core.policy import Policy

    rig.create(state=MeetingState.DONE, title="Исходное")
    rig.open_card()
    rig.bridge.refreshPolicy(Policy(raw={"meeting_record": "off"}))
    assert "Переименовать" not in rig.bridge.meetings[0]["actions"]
    assert not rig.bridge.card["renameEnabled"] and not rig.bridge.card["showEdit"]
    assert rig.bridge.renameMeeting(MID, "Новое") == "error"
    assert rig.meeting().title == "Исходное"


@pytest.mark.parametrize("raw,normalized", [("\ufb2a", "\u05e9\u05c1"), ("\u0958", "\u0915\u093c")])
def test_rename_accepts_nfc_expansion(rig: SaveRig, raw: str, normalized: str) -> None:
    rig.create(title="Исходное")
    rig.refresh()
    done = QSignalSpy(rig.bridge.renameFinished)
    assert rig.bridge.renameMeeting(MID, raw) == "pending"
    finish(rig, done)
    assert list(done) == [["saved"]]
    assert rig.meeting().title == normalized
