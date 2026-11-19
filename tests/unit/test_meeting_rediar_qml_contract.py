"""Reference 10 crops are checked against the complete committed sheets."""

import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit
REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))


def test_reference_10_bounds_against_full_sheets() -> None:
    from PyQt5.QtGui import QImage
    from tools.render_meeting_card import REFERENCE_BOUNDS, redo_states

    tops = {
        "failed": 1791,
        "room": 2004,
        "queued": 2206,
        "wait-memory": 2409,
        "running": 2612,
        "reconcile": 2815,
        "cancelled": 3017,
        "failed-again": 3230,
        "no-audio": 3424,
        "done": 3618,
        "running-900": 3821,
        "failed-again-900": 3996,
        "redo-failed": 6036,
        "offer": 7917,
        "offer-installed": 8094,
        "first": 8272,
        "before-keep": 8650,
        "kept-deleted": 8888,
        "kept-7": 9161,
        "kept-30": 9434,
    }
    full_tops = {
        "redo-failed-1024": 470,
        "redo-failed-900": 1302,
        "redo-running-1024": 4327,
        "redo-menu": 5123,
        "redo-dialog": 5586,
    }
    fixtures = redo_states()
    assert len(fixtures) == 25
    assert "redo-failed-900" in fixtures
    assert not any("rejected" in name for name in fixtures)
    for palette, border, _bg in [("light", "#d7deea", "#f7f9fc"), ("dark", "#2c3445", "#161b22")]:
        sheet = QImage(str(REPO / f"design/meetings/refs/10-rediarize-{palette}.png"))
        assert (sheet.width(), sheet.height()) == (1280, 10087)
        for name in fixtures:
            source, x, y, w, h, cx, cy = REFERENCE_BOUNDS[name]
            assert source == "10-rediarize"
            assert x >= 0 and y >= 0 and x + w <= sheet.width() and y + h <= sheet.height()
            assert all(isinstance(v, int) for v in (x, y, w, h, cx, cy))
            if name in full_tops:
                # The dialog's translucent border is composited on the sheet canvas.
                expected = "#232a36" if palette == "dark" and name == "redo-dialog" else border
                assert sheet.pixelColor(x + 60, full_tops[name]).name() == expected, (palette, name)
            if name.removeprefix("redo-") in tops:
                top = tops[name.removeprefix("redo-")]
                # Border at a fixed interior x, read from the *full* sheet;
                # the row above is app background, the next is table surface.
                assert sheet.pixelColor(x + 40, top).name() == border, (palette, name, top)
                assert sheet.pixelColor(x + 40, top - 1).name() != border, (palette, name)
                assert top > y + cy and top < y + h


def test_render_fake_redo_methods_match_real_bridge() -> None:
    from tools.render_meeting_card import make_bridge

    from astra_voice.ui.meeting_bridge import MeetingBridge

    fake = make_bridge()
    methods = [
        "rediarizeMeeting(QString,int,bool)",
        "divideMeeting(QString,int)",
        "cancelDiarizationRedo(QString,int)",
        "keepDiarizationAsIs(QString,int)",
    ]
    for signature in methods:
        assert fake.metaObject().indexOfMethod(signature.encode()) >= 0
        assert MeetingBridge.staticMetaObject.indexOfMethod(signature.encode()) >= 0
    assert fake.diarizationRedoTexts["warningHeading"] == "Разделить заново"


def test_warning_title_uses_guillemets() -> None:
    from astra_voice.ui.meeting_texts import REDO_TEXTS

    assert REDO_TEXTS["warningTitle"].format(title="Планёрка") == (
        "Разделить встречу «Планёрка» по голосам заново?"
    )


def test_reference_fragments_exclude_stand_edges() -> None:
    from PyQt5.QtGui import QImage
    from tools.render_meeting_card import (
        REDO_CROP_INSETS,
        REDO_RENDER_BOUNDS,
        REFERENCE_BOUNDS,
    )

    for theme, bg in [("light", "#f7f9fc"), ("dark", "#0b1220")]:
        sheet = QImage(str(REPO / f"design/meetings/refs/10-rediarize-{theme}.png"))
        for name, (left, top, right, bottom) in REDO_CROP_INSETS.items():
            _, x, y, w, h, cx, cy = REFERENCE_BOUNDS[name]
            _, ox, oy, ow, oh, ocx, ocy = REDO_RENDER_BOUNDS[name]
            assert (x, y, w, h) == (ox + left, oy + top, ow - left - right, oh - top - bottom)
            assert (cx, cy) == (ocx - left, ocy - top)
            assert {sheet.pixelColor(px, y).name() for px in range(x, x + w)} == {bg}, name


@pytest.mark.parametrize(
    "name",
    [
        "sections/MeetingCard",
        "sections/Meetings",
        "components/MeetingRediarizeDialog",
        "components/MeetingParticipants",
        "components/MeetingBadge",
    ],
)
def test_step_qml_has_no_literal_colors(name: str) -> None:
    import re

    source = (REPO / "qml" / (name + ".qml")).read_text()
    assert not re.search(r"#[0-9a-fA-F]{3,8}|\bQt\.rgba\s*\(", source)


@pytest.mark.parametrize("state", ["redo-running", "redo-first"])
@pytest.mark.parametrize("theme,focus", [("light", "#0a776a"), ("dark", "#2fd9c4")])
def test_reference_bounds_include_complete_cancel_focus(state: str, theme: str, focus: str) -> None:
    from PyQt5.QtGui import QImage
    from tools.render_meeting_card import REFERENCE_BOUNDS

    sheet = QImage(str(REPO / f"design/meetings/refs/10-rediarize-{theme}.png"))
    _, x, y, w, h, _, _ = REFERENCE_BOUNDS[state]
    assert w == 824 and h > 38
    for band in ((2, 3), (37, 38)):
        assert any(
            sheet.pixelColor(x + col, y + row).name() == focus
            for row in band
            for col in range(w - 124, w)
        ), (state, theme, band)
