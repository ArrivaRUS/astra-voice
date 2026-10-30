"""Frozen settings snapshots contain one fully selected navigation item.

The normal hover, keyboard focus and 120 ms crossfade remain available. The
existing Main.freezeAnimations stand flag must also settle sidebar selection.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from PyQt5.QtCore import QObject, QPointF
from PyQt5.QtTest import QTest
from test_meetings_section import grab
from test_meetings_section import rendered as rendered

pytestmark = pytest.mark.xvfb


def visual_items(item: Any) -> Iterator[Any]:
    yield item
    for child in item.childItems():
        yield from visual_items(child)


@pytest.mark.parametrize("dark", [False, True])
@pytest.mark.parametrize("title,index", [("Вывод", 2), ("Встречи", 3)])
def test_frozen_sidebar_captures_only_final_selection(
    rendered: Any, dark: bool, title: str, index: int
) -> None:
    window, _bridge, messages = rendered("off", dark)
    sidebar = window.findChild(QObject, "settingsSidebar")
    assert sidebar is not None
    if index != 3:
        assert sidebar.setProperty("currentIndex", index)
        QTest.qWait(1)
    general = next(item for item in visual_items(sidebar) if item.property("title") == "Общие")
    selected = next(item for item in visual_items(sidebar) if item.property("title") == title)
    assert not general.property("current") and not general.property("activeFocus")
    assert selected.property("current")
    assert not any(child.property("containsMouse") for child in general.childItems())
    background = next(
        item.property("color")
        for item in sidebar.childItems()
        if item.metaObject().className().startswith("QQuickRectangle")
    )
    primary = next(
        item.property("color")
        for item in selected.childItems()
        if item.metaObject().className().startswith("QQuickRectangle")
        and item.property("color").alpha() > 0
    )
    image = grab(window)
    # Check clear portions of the item, away from text, glyphs and round corners.
    old_point = general.mapToItem(window.contentItem(), QPointF(5, general.height() / 2))
    selected_point = selected.mapToItem(window.contentItem(), QPointF(5, selected.height() / 2))
    assert image.pixelColor(round(old_point.x()), round(old_point.y())) == background
    assert image.pixelColor(round(selected_point.x()), round(selected_point.y())) == primary
    assert not messages, messages


@pytest.mark.parametrize("dark", [False, True])
@pytest.mark.parametrize("title,index", [("Вывод", 2), ("Встречи", 3)])
def test_regular_sidebar_keeps_selection_crossfade(
    rendered: Any, dark: bool, title: str, index: int
) -> None:
    window, _bridge, messages = rendered("off", dark)
    sidebar = window.findChild(QObject, "settingsSidebar")
    assert sidebar is not None
    old_index, old_title = (2, "Вывод") if index == 3 else (3, "Встречи")
    assert sidebar.setProperty("currentIndex", old_index)
    previous = next(item for item in visual_items(sidebar) if item.property("title") == old_title)
    target = next(item for item in visual_items(sidebar) if item.property("title") == title)

    def fill(item: Any) -> Any:
        return next(
            child
            for child in item.childItems()
            if child.metaObject().className().startswith("QQuickRectangle")
            and child.property("color").alpha() > 0
        )

    previous_fill, target_fill = fill(previous), fill(target)
    assert window.setProperty("freezeAnimations", False)
    assert not sidebar.property("freezeAnimations")
    assert not previous.property("freezeAnimations")
    assert not target.property("freezeAnimations")
    assert sidebar.setProperty("currentIndex", index)
    # The normal behavior still starts at the old item and then settles, while
    # the frozen stand above is required to jump directly to the final frame.
    assert previous_fill.property("opacity") == 1
    assert target_fill.property("opacity") == 0
    QTest.qWait(250)
    assert previous_fill.property("opacity") == 0
    assert target_fill.property("opacity") == 1
    assert not messages, messages
