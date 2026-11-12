"""Screenshot source fragments and round 3 text/geometry invariants."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit
REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))


def test_snapshot_fragments_and_reference_coordinates() -> None:
    from tools import render_meeting_card as rc

    fixtures = rc.visual_states()
    room = fixtures["room"]
    assert room["meeting"].started_at == "2026-10-10T11:00:00Z"
    assert room["meeting"].recorded_ms == 2472000
    assert room["meeting"].app_class is None
    from astra_voice.ui.meeting_card import place

    assert place(room["meeting"]) == "Встреча в комнате"
    assert [u.speaker for u in room["utterances"]] == ["S1", "S2", "S3"]
    assert [u.text for u in room["utterances"]] == [
        "Начнём? Сегодня три вопроса.",
        "Да, у меня первый — по отпускам.",
        "Я тоже по графику.",
    ]
    assert len(fixtures["no-diarization"]["utterances"]) == 5
    assert fixtures["marks-900"]["table_until_ms"] == 80000
    assert rc.RENDER_VIEWPORTS["room"][0] - 44 == 672
    assert rc.RENDER_VIEWPORTS["no-diarization"][0] - 44 == 796
    assert rc.REFERENCE_BOUNDS["names-edit"][1:5] == (374, 1634, 716, 520)
    assert rc.REFERENCE_BOUNDS["save-echo"][2] == 5109
    assert rc.REFERENCE_BOUNDS["save-echo-checked"][2] == 5640
    assert rc.REFERENCE_BOUNDS["policy-off"][1:4] == (663, 1212, 502)
    assert rc.REFERENCE_BOUNDS["policy-off"][4] >= 370


def test_d4_literal_mockup_fragment_and_final_ellipsis() -> None:
    from tools import render_meeting_card as rc

    f = rc.visual_states()["save-echo"]
    for checked, times in [
        (False, ["[0:00:12]", "[0:00:13]", "[0:00:21]", "[0:00:22]", "[0:00:35]"]),
        (True, ["[0:00:12]", "[0:00:21]", "[0:00:35]", "[0:00:39]"]),
    ]:
        preview = rc.d4_preview_fixture(f["meeting"], f["speakers"], checked=checked)
        assert [r["time"] for r in preview["rows"]] == times
        assert preview["moreText"] == "…"
        assert sum(r["flag"] == "echo" for r in preview["rows"]) == (0 if checked else 2)
        assert next(r["text"] for r in preview["rows"] if r["time"] == "[0:00:21]").endswith(
            "в основном…"
        )


def test_new_ui_strings_use_standalone_guillemets() -> None:
    from astra_voice.ui.meeting_texts import DELETE_TEXTS, MARK_TEXTS, W2_TEXTS

    assert MARK_TEXTS["excludeEcho"] == "Не включать реплики с пометкой «возможное эхо»"
    assert all(
        "„" not in value and "“" not in value
        for source in (DELETE_TEXTS, MARK_TEXTS, W2_TEXTS)
        for value in source.values()
    )


def test_section_reference_card_tops_share_integer_origin() -> None:
    from tools.render_meeting_card import REFERENCE_BOUNDS

    gui = pytest.importorskip("PyQt5.QtGui")
    for theme, border in (("light", "#d7deea"), ("dark", "#2c3445")):
        sheet = gui.QImage(str(REPO / f"design/meetings/refs/09-diar-install-{theme}.png"))
        assert not sheet.isNull()
        for state, (_sheet, x, y, width, height, _px, _py) in REFERENCE_BOUNDS.items():
            if not state.startswith("section-") or state == "section-group-missing":
                continue
            assert all(isinstance(value, int) for value in (x, y, width, height))
            top = next(
                row for row in range(65) if sheet.pixelColor(x + 40, y + row).name() == border
            )
            assert top == 39, (theme, state, top)
