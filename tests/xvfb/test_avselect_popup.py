"""AvSelect: раскрытый список шире поля под длинные названия (микрофоны, 22.09).

Спецификация §4.4 задаёт 236 как минимальную ширину поповера: при длинных пунктах
список расширяется до popupMaxWidth и раскрывается влево, правый край на месте.
Без popupMaxWidth поведение прежнее — список не шире поля.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from PyQt5 import sip
from PyQt5.QtCore import QMetaObject, QObject, QUrl
from PyQt5.QtQml import QQmlComponent
from PyQt5.QtQuick import QQuickItem, QQuickView
from PyQt5.QtTest import QTest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from astra_voice.ui.icons import install_icon_provider
from helpers.qt_app import get_qapplication  # noqa: E402

pytestmark = pytest.mark.xvfb

QML_DIR = Path(__file__).resolve().parents[2] / "qml"
LONG_NAMES = [
    "Системный по умолчанию",
    "Встроенный звук Аналоговый стерео — Цифровой микрофон",
    "Встроенный звук Аналоговый стерео — Микрофон гарнитуры",
]


@pytest.fixture(scope="module")
def popup_app() -> Any:
    return get_qapplication()


@pytest.fixture
def popup_view(popup_app: Any) -> Iterator[QQuickView]:
    view = QQuickView()
    install_icon_provider(view.engine())
    try:
        yield view
    finally:
        sip.delete(view)
        popup_app.processEvents()


def render(view: QQuickView, body: str) -> QQuickItem:
    url = QUrl.fromLocalFile(str(QML_DIR / "__avselect_popup_test__.qml"))
    component = QQmlComponent(view.engine())
    component.setData(
        (
            'import QtQuick 2.15\nimport "components"\nItem { width: 800; height: 400\n'
            + body
            + "\n}"
        ).encode(),
        url,
    )
    root = component.create()
    assert root is not None, [error.toString() for error in component.errors()]
    view.setContent(url, component, root)
    view.show()
    QTest.qWait(100)
    assert view.status() == QQuickView.Ready, [error.toString() for error in view.errors()]
    return root


SELECT = (
    'AvSelect { objectName: "selector"; x: 500; width: 236; model: names\n'
    "    popupMaxWidth: maxWidth\n"
    "    readonly property real popupWidth: popup.width\n"
    "    readonly property real popupX: popup.x\n"
    "    function openPopup() { popup.open() }\n}"
)


def open_selector(view: QQuickView, max_width: float) -> tuple[QQuickItem, QObject]:
    view.rootContext().setContextProperty("names", LONG_NAMES)
    view.rootContext().setContextProperty("maxWidth", max_width)
    root = render(view, SELECT)
    selector = root.findChild(QObject, "selector")
    assert selector is not None
    QMetaObject.invokeMethod(selector, "openPopup")
    QTest.qWait(100)
    # Корень возвращаем вместе с элементом: без ссылки Python удалит дерево QML.
    return root, selector


def test_popup_widens_to_longest_item_and_opens_leftwards(popup_view: QQuickView) -> None:
    _root, selector = open_selector(popup_view, 700)
    width = selector.property("popupWidth")
    assert 236 < width <= 700
    # Правый край списка совпадает с правым краем поля.
    assert selector.property("popupX") == pytest.approx(236 - width)


def test_popup_width_is_capped_by_popup_max_width(popup_view: QQuickView) -> None:
    _root, selector = open_selector(popup_view, 300)
    assert selector.property("popupWidth") == 300


def test_popup_keeps_field_width_without_limit(popup_view: QQuickView) -> None:
    _root, selector = open_selector(popup_view, 0)
    assert selector.property("popupWidth") == 236
    assert selector.property("popupX") == 0
