"""MIC-20261014-01: actual microphone model and selector, isolated offscreen Qt."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

import pytest
from PyQt5 import sip
from PyQt5.QtCore import QPointF, Qt, QUrl, qInstallMessageHandler
from PyQt5.QtQml import QQmlApplicationEngine, QQmlEngine, QQmlExpression
from PyQt5.QtQuick import QQuickItem, QQuickWindow
from PyQt5.QtTest import QTest
from test_microphone_layout_regression import render, visual_tree
from test_onboarding import FakeTheme

from astra_voice.core.settings import Settings
from astra_voice.ui.bridges import OnboardingController, SettingsBridge
from astra_voice.ui.icons import install_icon_provider
from astra_voice.worker.audio import AudioDevice
from helpers.qt_app import get_qapplication

pytestmark = pytest.mark.xvfb
QML = Path(__file__).resolve().parents[2] / "qml"


def sources() -> list[AudioDevice]:
    return [
        AudioDevice(1, "monitor", "Колонки", True, available="yes"),
        AudioDevice(2, "unplugged", "Гарнитура", False, available="no"),
        AudioDevice(3, "builtin", "Встроенный микрофон", False, available="yes"),
        AudioDevice(4, "usb", "USB-микрофон", False, available="unknown"),
        AudioDevice(5, "monitor-hdmi", "HDMI", True, available="unknown"),
    ]


def evaluate(item: QQuickItem, expression: str) -> Any:
    query = QQmlExpression(QQmlEngine.contextForObject(item), item, expression)
    result, _undefined = query.evaluate()
    assert not query.hasError(), query.error().toString()
    return result


def selector_in(root: QQuickItem) -> QQuickItem:
    return next(item for item in visual_tree(root) if item.objectName() == "microphoneSelector")


@pytest.mark.parametrize("dark", [False, True])
def test_microphone_popup_groups_gray_items_and_keyboard_selection(dark: bool) -> None:
    with render(1035, "normal", dark=dark) as rendered:
        devices = sources()
        rendered.bridge._device_provider = lambda: devices
        rendered.bridge._default_device_provider = lambda devices: devices[2]
        rendered.bridge.refreshDevices()
        QTest.qWait(30)
        selector = selector_in(rendered.row)
        assert [item["id"] for item in rendered.bridge.devices] == [
            "",
            "builtin",
            "usb",
            "unplugged",
            "monitor",
            "monitor-hdmi",
        ]
        explanation = rendered.row.findChild(QQuickItem, "microphoneExplanation")
        assert explanation is not None and explanation.isVisible()
        assert explanation.property("text") == "Сейчас: Встроенный микрофон"
        assert selector.property("currentText") == "Системный по умолчанию"
        evaluate(selector, "popup.open()")
        QTest.qWait(35)
        content = cast(QQuickItem, evaluate(selector, "popup.contentItem"))
        labels = {
            item.property("text"): item
            for item in visual_tree(content)
            if item.objectName() == "selectItemLabel"
        }
        assert "Гарнитура — нет подключения" in labels
        subtitles = [
            item
            for item in visual_tree(content)
            if item.objectName() == "selectItemSubtitle" and item.isVisible()
        ]
        assert len(subtitles) == 1
        assert subtitles[0].property("text") == "Сейчас: Встроенный микрофон"
        assert subtitles[0].property("color") == evaluate(selector, "Theme.fgMuted")
        gray = labels["Гарнитура — нет подключения"]
        assert gray.property("color") == evaluate(selector, "Theme.fgMuted")
        assert gray.parentItem().parentItem().property("enabled") is True
        headers = [
            item
            for item in visual_tree(content)
            if item.objectName() == "selectGroupHeader" and item.isVisible()
        ]
        assert len(headers) == 1 and headers[0].property("text") == "ЗВУК СИСТЕМЫ"
        header = headers[0]
        assert header.property("color") == evaluate(selector, "Theme.fgMuted")
        assert evaluate(header, "font.family") == evaluate(selector, "Theme.fontUi")
        assert evaluate(header, "font.pixelSize") == evaluate(selector, "Theme.fontGroupCapsSize")
        assert evaluate(header, "font.weight") == evaluate(header, "Font.Medium")
        assert evaluate(header, "font.capitalization") == evaluate(header, "Font.AllUppercase")
        # QFont stores spacing in 1/64-pixel increments.
        assert evaluate(header, "font.letterSpacing") == pytest.approx(
            evaluate(selector, "Theme.fontGroupCapsTracking * Theme.fontGroupCapsSize"), abs=1 / 64
        )
        assert evaluate(header, "Accessible.role") == evaluate(header, "Accessible.StaticText")
        assert evaluate(header, "Accessible.focusable") is False
        assert not header.hasActiveFocus()
        divider = next(
            item
            for item in visual_tree(content)
            if item.objectName() == "selectGroupDivider" and item.isVisible()
        )
        assert divider.height() == 1
        assert divider.y() == 0
        assert divider.property("color") == evaluate(selector, "Theme.border")
        assert "Колонки" in labels
        # ListView may not instantiate the last off-viewport monitor.
        assert evaluate(selector, "popupLabel(5)") == "HDMI"
        assert not any("(звук системы)" in label for label in labels)
        assert selector.property("count") == len(rendered.bridge.devices)
        before = selector.property("currentIndex")
        position = header.mapToScene(QPointF(header.width() / 2, header.height() / 2))
        QTest.mouseClick(rendered.window, Qt.LeftButton, pos=position.toPoint())
        QTest.qWait(20)
        assert selector.property("currentIndex") == before
        assert rendered.bridge.device == ""
        assert evaluate(selector, "popup.visible") is True
        assert rendered.device_changes == []
        assert not header.hasActiveFocus()
        assert evaluate(selector, "accessibleLabel(3)") == "Гарнитура — нет подключения"
        assert evaluate(selector, "accessibleLabel(4)") == "Звук системы: Колонки"
        assert evaluate(selector, "accessibleLabel(5)") == "Звук системы: HDMI"
        evaluate(selector, "popup.contentItem.positionViewAtIndex(4, ListView.Beginning)")
        QTest.qWait(20)
        monitor = labels["Колонки"].parentItem().parentItem()
        position = monitor.mapToScene(QPointF(monitor.width() / 2, monitor.height() / 2))
        QTest.mouseClick(rendered.window, Qt.LeftButton, pos=position.toPoint())
        QTest.qWait(20)
        assert selector.property("currentIndex") == 4
        assert rendered.bridge.device == "monitor"
        assert rendered.bridge.devices[4]["id"] == "monitor"
        assert selector.property("currentText") == "Колонки (звук системы)"
        assert evaluate(selector, "Accessible.name") == "Колонки (звук системы)"
        assert evaluate(selector, "popup.visible") is False
        rendered.bridge.setProperty("device", "")
        QTest.qWait(20)
        selector.forceActiveFocus(Qt.TabFocusReason)
        for _ in range(3):
            QTest.keyClick(rendered.window, Qt.Key_Down)
        QTest.qWait(30)
        assert rendered.bridge.device == "unplugged"
        assert selector.property("currentText") == "Гарнитура — нет подключения"
        assert evaluate(selector, "contentItem.color") == evaluate(selector, "Theme.fgMuted")
        assert explanation.property("text") == "Подключите его или выберите другой микрофон"
        assert not rendered.messages, rendered.messages


@pytest.mark.parametrize("opened", [False, True], ids=["closed", "open"])
def test_microphone_arrows_cross_group_without_an_extra_selection(opened: bool) -> None:
    with render(1035, "normal") as rendered:
        devices = sources()
        rendered.bridge._device_provider = lambda: devices
        rendered.bridge.refreshDevices()
        rendered.bridge.setProperty("device", "unplugged")
        QTest.qWait(30)
        selector = selector_in(rendered.row)
        selector.forceActiveFocus(Qt.TabFocusReason)
        if opened:
            evaluate(selector, "popup.open()")
            QTest.qWait(30)
        QTest.keyClick(rendered.window, Qt.Key_Down)
        QTest.qWait(20)
        if opened:
            assert selector.property("highlightedIndex") == 4
            QTest.keyClick(rendered.window, Qt.Key_Return)
        assert selector.property("currentIndex") == 4
        assert rendered.bridge.device == "monitor"
        assert selector.property("currentText") == "Колонки (звук системы)"
        assert rendered.bridge.devices[selector.property("currentIndex")]["id"] == "monitor"
        if opened:
            evaluate(selector, "popup.open()")
            QTest.qWait(20)
        QTest.keyClick(rendered.window, Qt.Key_Up)
        QTest.qWait(20)
        if opened:
            assert selector.property("highlightedIndex") == 3
            QTest.keyClick(rendered.window, Qt.Key_Return)
        assert selector.property("currentIndex") == 3
        assert rendered.bridge.device == "unplugged"
        assert not rendered.messages, rendered.messages


@pytest.mark.parametrize("opened", [False, True], ids=["closed", "open"])
@pytest.mark.parametrize("key", [Qt.Key_Home, Qt.Key_End, Qt.Key_PageUp, Qt.Key_PageDown])
def test_microphone_navigation_keys_only_address_devices(opened: bool, key: int) -> None:
    with render(1035, "normal") as rendered:
        devices = sources()
        rendered.bridge._device_provider = lambda: devices
        rendered.bridge.refreshDevices()
        rendered.bridge.setProperty("device", "monitor")
        QTest.qWait(30)
        selector = selector_in(rendered.row)
        selector.forceActiveFocus(Qt.TabFocusReason)
        if opened:
            evaluate(selector, "popup.open()")
            QTest.qWait(30)
        QTest.keyClick(rendered.window, key)
        QTest.qWait(20)
        if opened:
            assert 0 <= selector.property("highlightedIndex") < len(rendered.bridge.devices)
            QTest.keyClick(rendered.window, Qt.Key_Return)
        index = selector.property("currentIndex")
        assert 0 <= index < len(rendered.bridge.devices)
        if key == Qt.Key_Home:
            assert index == 0
        elif key == Qt.Key_End:
            assert index == len(rendered.bridge.devices) - 1
        assert rendered.bridge.device == rendered.bridge.devices[index]["id"]
        assert selector.property("currentText") == rendered.bridge.devices[index]["label"]
        assert not rendered.messages, rendered.messages


@pytest.mark.parametrize("locked", [False, True])
def test_missing_saved_microphone_stays_selected_and_respects_policy(locked: bool) -> None:
    with render(1035, "locked" if locked else "normal", saved_device="missing") as rendered:
        rendered.bridge._settings.device_label = "USB-гарнитура"
        rendered.bridge.refreshDevices()
        QTest.qWait(30)
        selector = selector_in(rendered.row)
        assert rendered.bridge.device == "missing"
        assert selector.property("currentIndex") == 1
        assert selector.property("currentText") == "USB-гарнитура — нет подключения"
        assert selector.property("enabled") is (not locked)
        explanation = rendered.row.findChild(QQuickItem, "microphoneExplanation")
        assert explanation is not None and explanation.isVisible()
        assert explanation.property("text") == "Подключите его или выберите другой микрофон"
        assert rendered.device_changes == []
        assert rendered.saved_changes == []
        # A forged activation cannot bypass policy; keyboard naturally skips disabled control.
        evaluate(selector, "currentIndex = 0; activated(0)")
        QTest.qWait(30)
        assert rendered.bridge.device == ("missing" if locked else "")
        assert rendered.device_changes == ([] if locked else [None])
        assert not rendered.messages, rendered.messages


@contextmanager
def wizard(
    locked: bool,
    *,
    source_no: bool = False,
) -> Iterator[tuple[QQuickWindow, OnboardingController, list[str]]]:
    app = get_qapplication()
    devices = [AudioDevice(1, "unplugged", "Гарнитура", False, available="no")] if source_no else []
    settings = Settings(
        device_label="USB-гарнитура",
        extra={
            "device": "unplugged" if source_no else "missing",
            "onboarding_language_set": True,
        },
    )
    bridge = SettingsBridge(
        settings,
        save=lambda _settings: None,
        device_provider=lambda: devices,
        locked=["device"] if locked else [],
    )
    controller = OnboardingController(bridge, settings=settings, device_provider=lambda: devices)
    theme = FakeTheme(False)
    messages: list[str] = []

    def handler(_mode: Any, _context: Any, message: str) -> None:
        messages.append(message)

    previous = qInstallMessageHandler(handler)
    engine = QQmlApplicationEngine()
    install_icon_provider(engine)
    engine.rootContext().setContextProperty("onboarding", controller)
    engine.rootContext().setContextProperty("themeSource", theme)
    engine.loadData(
        b"""import QtQuick 2.15
import QtQuick.Window 2.15
import "onboarding" as Onb
Window { width: 660; height: 700; visible: true
    Onb.Step4Mic { x: 20; y: 20 }
}""",
        QUrl.fromLocalFile(str(QML / "MicrophoneTest.qml")),
    )
    try:
        assert engine.rootObjects(), messages
        window = cast(QQuickWindow, engine.rootObjects()[0])
        QTest.qWait(70)
        yield window, controller, messages
    finally:
        controller.shutdown()
        for root in engine.rootObjects():
            if isinstance(root, QQuickWindow):
                root.close()
        sip.delete(engine)
        sip.delete(controller)
        sip.delete(bridge)
        app.processEvents()
        qInstallMessageHandler(previous)


@pytest.mark.parametrize("locked", [False, True])
def test_wizard_missing_microphone_explains_actual_recording_contract(locked: bool) -> None:
    with wizard(locked) as (window, controller, messages):
        selector = selector_in(window.contentItem())
        assert selector.property("currentText") == "USB-гарнитура — нет подключения"
        assert selector.property("currentIndex") == 1
        assert controller.device == "missing"
        assert selector.property("enabled") is (not locked)
        explanation = next(
            item
            for item in visual_tree(window.contentItem())
            if item.objectName() == "microphoneExplanation"
        )
        assert explanation.isVisible()
        assert explanation.property("text") == (
            "Подключите его или выберите другой микрофон. Пока у него нет подключения, "
            "диктовка не начнётся: «Микрофон отключился»."
        )
        assert not messages, messages


def test_wizard_no_port_does_not_promise_recording_is_blocked() -> None:
    with wizard(False, source_no=True) as (window, controller, messages):
        selector = selector_in(window.contentItem())
        assert selector.property("currentText") == "Гарнитура — нет подключения"
        assert controller.device == "unplugged"
        explanation = next(
            item
            for item in visual_tree(window.contentItem())
            if item.objectName() == "microphoneExplanation"
        )
        assert explanation.isVisible()
        assert explanation.property("text") == "Подключите его или выберите другой микрофон"
        assert not any(
            "диктовка не начнётся" in str(item.property("text") or "")
            for item in visual_tree(window.contentItem())
        )
        assert not messages, messages
