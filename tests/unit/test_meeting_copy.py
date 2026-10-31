"""Owner-approved meeting copy follows the rendered mockup, not quote nesting."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest

from astra_voice.ui.meeting_texts import RESPONSIBILITY_ITEMS, TEXTS

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


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
    assert RESPONSIBILITY_ITEMS[index][field] == mockup_items()[index][column]


@pytest.mark.parametrize("key", ["offDescription", "empty", "noticeCaption", "diarizationCaption"])
def test_meeting_display_copy_uses_russian_guillemets(key: str) -> None:
    assert "„" not in TEXTS[key] and "“" not in TEXTS[key]
    assert "«" in TEXTS[key] and "»" in TEXTS[key]
