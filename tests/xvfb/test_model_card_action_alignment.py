"""Compact model actions at both supported card widths; actual reusable QML."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

import pytest
from PyQt5 import sip
from PyQt5.QtCore import QPoint, QPointF, QRectF, QSizeF, Qt, QUrl, qInstallMessageHandler
from PyQt5.QtQml import QQmlComponent
from PyQt5.QtQuick import QQuickItem, QQuickView
from PyQt5.QtTest import QSignalSpy, QTest

from astra_voice.ui.icons import install_icon_provider
from helpers.qt_app import get_qapplication

pytestmark = pytest.mark.xvfb
QML = Path(__file__).resolve().parents[2] / "qml"
FACTS = {
    "vosk": ("Vosk small ru", "27 МБ", "50 МБ", ["Только русский", "Apache-2.0"]),
    "whisper": (
        "Whisper large-v3-turbo",
        "1086 МБ",
        "2007 МБ",
        ["Русский и ещё много языков", "с пунктуацией", "MIT"],
    ),
    "gigaam": (
        "GigaAM v3 RNN-T",
        "226 МБ",
        "419 МБ",
        ["Только русский", "с пунктуацией", "MIT", "отечественная"],
    ),
}


def visual_tree(item: QQuickItem) -> Iterator[QQuickItem]:
    yield item
    for child in item.childItems():
        yield from visual_tree(child)


def rect(item: QQuickItem, card: QQuickItem) -> QRectF:
    return QRectF(item.mapToItem(card, QPointF()), QSizeF(item.width(), item.height()))


@contextmanager
def render(width: int, facts: str, properties: dict[str, Any]) -> Iterator[Any]:
    app = get_qapplication()
    messages: list[str] = []

    def handler(_mode: Any, _context: Any, message: str) -> None:
        messages.append(message)

    previous = qInstallMessageHandler(handler)
    view = QQuickView()
    install_icon_provider(view.engine())
    component = QQmlComponent(view.engine())
    component.setData(
        f"""import QtQuick 2.15
import "."
import "components"
Rectangle {{
    width: {width}; height: card.implicitHeight + 16; color: Theme.bgApp
    OnboardingModelCard {{ id: card; objectName: "alignmentCard"; y: 8 }}
}}""".encode(),
        QUrl.fromLocalFile(str(QML / "alignment-harness.qml")),
    )
    root = component.create()
    assert root is not None, [error.toString() for error in component.errors()]
    view.setContent(QUrl(), component, root)
    card = cast(QQuickItem, root.findChild(QQuickItem, "alignmentCard"))
    title, size, ram, tags = FACTS[facts]
    for key, value in {
        "modelTitle": title,
        "sizeText": size,
        "ramText": ram,
        "tags": tags,
        "purpose": "Русская диктовка — тест раскладки",
        **properties,
    }.items():
        assert card.setProperty(key, value), key
    try:
        view.show()
        QTest.qWait(100)
        app.processEvents()
        yield view, card, messages
    finally:
        view.close()
        sip.delete(view)
        app.processEvents()
        qInstallMessageHandler(previous)


def save_evidence(view: QQuickView, card: QQuickItem, name: str) -> None:
    directory = os.environ.get("ASTRA_VOICE_MODEL_CARD_REPORT_DIR")
    if not directory:
        return
    destination = Path(directory)
    destination.mkdir(parents=True, exist_ok=True)
    geometry = []
    for item in visual_tree(card):
        if item.isVisible() and (item.objectName() or item.property("small") is True):
            bounds = rect(item, card)
            geometry.append(
                {
                    "name": item.objectName(),
                    "text": item.property("text"),
                    "x": bounds.x(),
                    "y": bounds.y(),
                    "w": bounds.width(),
                    "h": bounds.height(),
                    "implicitWidth": item.implicitWidth(),
                }
            )
    (destination / f"{name}.json").write_text(json.dumps(geometry, ensure_ascii=False, indent=2))
    frame = view.grabWindow()
    if frame.isNull():
        capture = view.contentItem().grabToImage()
        assert capture is not None
        QTest.qWait(60)
        frame = capture.image()
    assert not frame.isNull()
    assert frame.save(str(destination / f"{name}.png"))


CASES: dict[str, dict[str, Any]] = {
    "installed": {"manageEnabled": True, "badge": "installed", "cardState": "installed"},
    "active": {"manageEnabled": True, "badge": "active", "cardState": "installed"},
    "update": {
        "manageEnabled": True,
        "badge": "installed",
        "cardState": "installed",
        "updateAvailable": True,
    },
    "corrupted": {"manageEnabled": True, "badge": "installed", "cardState": "corrupted"},
    "broken": {"manageEnabled": True, "badge": "installed", "cardState": "broken"},
    "message": {
        "manageEnabled": True,
        "badge": "installed",
        "cardState": "installed",
        "message": "Не удалось переключить модель. Попробуйте снова после завершения операции.",
    },
    "hint": {
        "manageEnabled": True,
        "badge": "installed",
        "cardState": "installed",
        "hint": "Для этой модели может не хватить оперативной памяти",
        "hintKind": "warning",
    },
    "switching": {"manageEnabled": True, "badge": "installed", "cardState": "switching"},
    "queued": {"cardState": "queued"},
    "downloading": {"cardState": "downloading"},
    "verifying": {"cardState": "verifying"},
    "failed": {"cardState": "failed"},
    "paused": {"cardState": "paused-no-space"},
    "wizard_installed": {"badge": "installed", "cardState": "installed"},
}


@pytest.mark.parametrize("width", [796, 672])
@pytest.mark.parametrize("facts", list(FACTS))
@pytest.mark.parametrize("state", list(CASES))
def test_compact_action_geometry(width: int, facts: str, state: str) -> None:
    with render(width, facts, CASES[state]) as (view, card, messages):
        save_evidence(view, card, f"{state}-{facts}-{width}")
        footer = card.findChild(QQuickItem, "cardFooter")
        actions = card.findChild(QQuickItem, "footerActions")
        facts_item = card.findChild(QQuickItem, "footerFacts")
        assert footer is not None and actions is not None and facts_item is not None
        buttons = [
            item
            for item in visual_tree(card)
            if item.isVisible() and item.property("small") is True
        ]
        bounds = [rect(item, card) for item in buttons]
        for left, right in zip(bounds, bounds[1:], strict=False):
            assert right.left() - left.right() == pytest.approx(8, abs=1), (
                state,
                facts,
                width,
                [(b.x(), b.width()) for b in bounds],
            )
            assert right.top() == pytest.approx(left.top(), abs=1)
        if bounds:
            right_margin = 1 if CASES[state].get("manageEnabled") else 0
            assert bounds[-1].right() == pytest.approx(
                rect(footer, card).right() - right_margin, abs=1
            )
        for button, bounds_item in zip(buttons, bounds, strict=True):
            assert button.width() >= button.implicitWidth() - 1
            assert bounds_item.right() <= width - 12
            assert bounds_item.bottom() <= card.height() - 8
        if actions.isVisible():
            assert rect(facts_item, card).right() + 12 <= rect(actions, card).left() + 1
            assert actions.width() <= footer.width() * 0.65 + 1
        for item in visual_tree(card):
            if item.isVisible() and item.property("text") and item.property("elide") is not None:
                assert item.property("elide") == 3  # Text.ElideNone: text must remain complete.
        assert not [m for m in messages if any(s in m for s in ("Error", "binding loop", "Cannot"))]
        if state == "wizard_installed":
            assert not buttons
        if state in {"active", "switching"}:
            assert len(buttons) == 1 and not buttons[0].isEnabled()


@pytest.mark.parametrize(
    "text,signal", [("Сделать рабочей", "activateRequested"), ("Удалить", "removeRequested")]
)
def test_installed_action_signals(text: str, signal: str) -> None:
    with render(796, "vosk", CASES["installed"]) as (view, card, _messages):
        button = next(
            item for item in visual_tree(card) if item.isVisible() and item.property("text") == text
        )
        spy = QSignalSpy(getattr(card, signal))
        point = button.mapToItem(
            view.contentItem(), QPointF(button.width() / 2, button.height() / 2)
        )
        QTest.mouseClick(view, Qt.LeftButton, pos=QPoint(round(point.x()), round(point.y())))
        assert len(spy) == 1
