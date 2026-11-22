"""Owner-approved meeting copy and the unchanged parts of the mockup."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest

from astra_voice.ui.meeting_texts import RESPONSIBILITY_ITEMS, TEXTS

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]
NOTICE_CAPTION = (
    "Копируется кнопкой на плашке записи и пунктом «Текст для участников…» в меню значка в трее"
)
RESPONSIBILITY_NOTICE = (
    "Готовый текст для чата встречи копируется кнопкой на плашке записи — вставьте его "
    "в чат и скажите вслух. Лучше предупреждать заранее — например, в приглашении на встречу."
)


def mockup_items() -> list[tuple[str, str, str, str]]:
    tree = ast.parse((ROOT / "design/meetings/mockups/_p1.py").read_text())
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "resp_dialog"
    )
    assignment = next(
        node
        for node in function.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "items" for target in node.targets)
    )
    value: Any = ast.literal_eval(assignment.value)
    assert isinstance(value, list)
    return value


@pytest.mark.parametrize("index", range(5))
@pytest.mark.parametrize("field,column", [("icon", 0), ("title", 1), ("text", 2)])
def test_responsibility_items_match_rendered_mockup(index: int, field: str, column: int) -> None:
    expected = (
        RESPONSIBILITY_NOTICE if (index, field) == (3, "text") else mockup_items()[index][column]
    )
    assert RESPONSIBILITY_ITEMS[index][field] == expected


def test_participant_notice_copy_matches_22_november_decision() -> None:
    assert TEXTS["noticeCaption"] == NOTICE_CAPTION
    assert NOTICE_CAPTION in (ROOT / "qml/sections/Meetings.qml").read_text()
    assert RESPONSIBILITY_NOTICE in (ROOT / "qml/components/ResponsibilityDialog.qml").read_text()


@pytest.mark.parametrize("key", ["offDescription", "empty", "noticeCaption", "diarizationCaption"])
def test_meeting_display_copy_uses_russian_guillemets(key: str) -> None:
    assert "„" not in TEXTS[key] and "“" not in TEXTS[key]
    assert "«" in TEXTS[key] and "»" in TEXTS[key]
