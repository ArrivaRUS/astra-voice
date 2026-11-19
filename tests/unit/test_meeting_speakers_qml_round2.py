"""QML round 2 (a): cached accessibility strings follow optimistic rename and rollback."""

from __future__ import annotations

import pytest
from test_meeting_card_bridge import SaveRig
from test_meeting_speakers import rig as rig
from test_tray import qapp as qapp

from astra_voice.meetings import speakers

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("filesystem_euid")]


def test_cached_speaker_strings_follow_rename_and_rollback_without_reopening(rig: SaveRig) -> None:
    bridge = rig.bridge
    anna = speakers.rename(bridge._speakers, "S1", "Анна")
    bridge._apply_speakers(anna)
    assert bridge.card["participants"][1]["label"] == "Анна"  # warm the point-update cache
    maria = speakers.rename(anna, "S1", "Мария")
    bridge._apply_speakers(maria)
    participant = bridge.card["participants"][1]
    assert participant["name"] == participant["label"] == "Мария"
    assert participant["inputAccessibleName"] == "Как подписать: Мария"
    assert participant["mergeTitle"] == "Мария — это…"
    bridge._apply_speakers(anna)  # same application path used by failed-write rollback
    participant = bridge.card["participants"][1]
    assert participant["inputAccessibleName"] == "Как подписать: Анна"
    assert participant["mergeTitle"] == "Анна — это…"


def test_panel_label_stays_anonymous_and_preview_numbers_do_not_depend_on_names() -> None:
    from test_meeting_speakers import data

    from astra_voice.ui import meeting_card

    meeting, transcript, names = data()
    names = speakers.rename(names, "S1", "Одинаково")
    names = speakers.rename(names, "S2", "Одинаково")
    panel = meeting_card.participants(
        meeting,
        {"me": 1, "S1": 2, "S2": 1},
        live=False,
        speakers=names,
        utterances=transcript.utterances,
        editable=True,
    )
    assert [(p["num"], p["panelLabel"]) for p in panel if p["kind"] == "speaker"] == [
        ("1", "Спикер 1"),
        ("2", "Спикер 2"),
    ]
    preview = meeting_card.save_preview("stenogram", meeting, transcript, names)
    by_text = {row["text"]: row for row in preview["rows"] if row["kind"] == "utterance"}
    assert by_text["слово1"]["who"] == by_text["слово3"]["who"] == "Одинаково"
    assert by_text["слово1"]["num"] == "1"
    assert by_text["слово2"]["num"] == "1"  # canonical target after S3 → S1
    assert by_text["слово3"]["num"] == "2"
    assert all(
        not row["num"]
        for row in meeting_card.save_preview("me_and_others", meeting, transcript, names)["rows"]
    )
