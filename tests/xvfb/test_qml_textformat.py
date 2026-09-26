"""ИБ-12: внешние строки рисуются буквально и не загружают HTML-изображения."""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from PyQt5 import sip
from PyQt5.QtCore import QIODevice, QMetaObject, QObject, QTimer, QUrl
from PyQt5.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest
from PyQt5.QtQml import QQmlComponent, QQmlNetworkAccessManagerFactory
from PyQt5.QtQuick import QQuickItem, QQuickView
from PyQt5.QtTest import QTest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from helpers.qt_app import get_qapplication  # noqa: E402

pytestmark = pytest.mark.xvfb

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QUICK_BACKEND", "software")
os.environ["QT_QUICK_CONTROLS_STYLE"] = "Default"

QML_DIR = Path(__file__).resolve().parents[2] / "qml"
IMAGE_URL = "http://127.0.0.1:1/x.png"
ATTACK_TEXT = f'<img src="{IMAGE_URL}">'


class EmptyReply(QNetworkReply):
    """Завершённый пустой ответ: ни сокетов, ни вызова сетевого backend Qt."""

    def __init__(
        self, operation: Any, request: QNetworkRequest, parent: QNetworkAccessManager
    ) -> None:
        super().__init__(parent)
        self.setOperation(operation)
        self.setRequest(request)
        self.setUrl(request.url())
        self.open(QIODevice.ReadOnly | QIODevice.Unbuffered)
        QTimer.singleShot(0, self.finish)

    def finish(self) -> None:
        if not self.isFinished():
            self.setFinished(True)
            self.finished.emit()

    def abort(self) -> None:
        self.finish()

    def readData(self, maxlen: int) -> bytes:
        return b""

    def bytesAvailable(self) -> int:
        return 0

    def isSequential(self) -> bool:
        return True


class RecordingNetworkAccessManager(QNetworkAccessManager):
    def __init__(self, urls: list[str], parent: QObject) -> None:
        super().__init__(parent)
        self.urls = urls

    def createRequest(
        self, operation: Any, request: QNetworkRequest, outgoingData: QIODevice | None = None
    ) -> QNetworkReply:
        self.urls.append(request.url().toString())
        return EmptyReply(operation, request, self)


class RecordingNetworkFactory(QQmlNetworkAccessManagerFactory):
    def __init__(self) -> None:
        super().__init__()
        self.urls: list[str] = []
        self.managers: list[RecordingNetworkAccessManager] = []

    def create(self, parent: QObject) -> QNetworkAccessManager:
        manager = RecordingNetworkAccessManager(self.urls, parent)
        self.managers.append(manager)
        return manager


@pytest.fixture(scope="module")
def textformat_app() -> Any:
    return get_qapplication()


@pytest.fixture
def textformat_view(textformat_app: Any) -> Iterator[tuple[QQuickView, RecordingNetworkFactory]]:
    view = QQuickView()
    from astra_voice.ui.icons import install_icon_provider

    install_icon_provider(view.engine())
    factory = RecordingNetworkFactory()
    # Фабрика устанавливается ДО загрузки любого QML и живёт дольше движка.
    view.engine().setNetworkAccessManagerFactory(factory)
    view.rootContext().setContextProperty("attackText", ATTACK_TEXT)
    try:
        yield view, factory
    finally:
        sip.delete(view)
        textformat_app.processEvents()


def render_component(view: QQuickView, body: str) -> QQuickItem:
    url = QUrl.fromLocalFile(str(QML_DIR / "__ib12_textformat_test__.qml"))
    component = QQmlComponent(view.engine())
    component.setData(
        (
            'import QtQuick 2.15\nimport "components"\n'
            "Item { width: 800; height: 160; "
            "readonly property int plainTextFormat: Text.PlainText\n" + body + "\n}"
        ).encode(),
        url,
    )
    root = component.create()
    assert root is not None, [error.toString() for error in component.errors()]
    view.setContent(url, component, root)
    view.show()
    QTest.qWait(100)
    assert view.status() == QQuickView.Ready, [error.toString() for error in view.errors()]
    assert view.isExposed()
    return root


def visual_tree(root: QQuickItem) -> Iterator[QQuickItem]:
    yield root
    for child in root.childItems():
        yield from visual_tree(child)


def assert_literal_text(view: QQuickView, root: QQuickItem, expected_count: int) -> None:
    texts = [
        item
        for item in visual_tree(view.contentItem())
        if item.property("text") == ATTACK_TEXT
        and item.metaObject().indexOfProperty("textFormat") >= 0
    ]
    assert len(texts) == expected_count, "не найдены все Text с внешней строкой"
    for item in texts:
        assert item.property("textFormat") == root.property("plainTextFormat")
        assert item.isVisible()
        assert item.property("contentWidth") > 0
        # Проверяем реальную отрисовку глифов тега; PNG на диск не записываем.
        grab = item.grabToImage()
        assert grab is not None
        for _ in range(20):
            QTest.qWait(25)
            frame = grab.image()
            if not frame.isNull():
                break
        assert not frame.isNull(), "offscreen-кадр текста не отрисован"
        assert any(
            frame.pixel(x, y) != frame.pixel(0, 0)
            for y in range(frame.height())
            for x in range(frame.width())
        ), "вместо буквального тега получен пустой кадр"


@pytest.mark.parametrize("property_name", ["sub", "label", "lockedText"])
def test_setting_row_external_text_never_requests_images(
    textformat_view: tuple[QQuickView, RecordingNetworkFactory], property_name: str
) -> None:
    view, factory = textformat_view
    root = render_component(
        view,
        "SettingRow { width: 780; locked: true; " + property_name + ": attackText }",
    )
    assert_literal_text(view, root, expected_count=1)
    assert factory.urls == [], f"HTML обошёл NetworkGate: {factory.urls}"


def test_avselect_display_and_delegate_never_request_images(
    textformat_view: tuple[QQuickView, RecordingNetworkFactory],
) -> None:
    view, factory = textformat_view
    root = render_component(
        view,
        'AvSelect { objectName: "selector"; width: 780; model: [attackText]\n'
        "readonly property bool popupVisible: popup.visible\n"
        "function openPopup() { popup.open() }\n}",
    )
    assert_literal_text(view, root, expected_count=1)
    assert factory.urls == []
    selector = root.findChild(QObject, "selector")
    assert selector is not None
    QMetaObject.invokeMethod(selector, "openPopup")
    QTest.qWait(100)
    assert selector.property("popupVisible")
    assert_literal_text(view, root, expected_count=2)
    assert factory.urls == [], f"HTML обошёл NetworkGate: {factory.urls}"


def test_network_recorder_detects_autotext_image_request(
    textformat_view: tuple[QQuickView, RecordingNetworkFactory],
) -> None:
    """Контроль PoC: тот же тег при AutoText обязан попасть в заглушку."""
    view, factory = textformat_view
    root = render_component(view, "Text { text: attackText; textFormat: Text.AutoText }")
    assert root is view.rootObject()
    for _ in range(20):
        if factory.urls:
            break
        QTest.qWait(25)
    assert factory.urls == [IMAGE_URL], "заглушка не перехватила контрольный запрос Qt"
