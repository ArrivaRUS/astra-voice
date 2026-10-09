"""Command editor padding and actions; real QML/SettingsBridge, no desktop calls."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock

import pytest
from PyQt5 import sip
from PyQt5.QtCore import QMetaObject, QPointF, QRectF, QSizeF, Qt, QUrl, qInstallMessageHandler
from PyQt5.QtQml import QQmlApplicationEngine, QQmlComponent
from PyQt5.QtQuick import QQuickItem, QQuickWindow
from PyQt5.QtTest import QTest

from astra_voice.core.settings import Settings
from astra_voice.ui.bridges import SettingsBridge
from astra_voice.ui.hotkey_capture import CaptureHost
from astra_voice.ui.icons import install_icon_provider
from helpers.qt_app import get_qapplication

pytestmark = pytest.mark.xvfb
ROOT = Path(__file__).resolve().parents[2]
os.environ["QT_QUICK_CONTROLS_STYLE"] = "Default"


def tree(item: QQuickItem) -> Iterator[QQuickItem]:
    yield item
    for child in item.childItems():
        yield from tree(child)


def box(item: QQuickItem, relative: QQuickItem) -> QRectF:
    return QRectF(item.mapToItem(relative, QPointF()), QSizeF(item.width(), item.height()))


def buttons(editor: QQuickItem) -> dict[str, QQuickItem]:
    return {
        str(item.property("text")): item
        for item in tree(editor)
        if item.isVisible() and item.metaObject().indexOfProperty("small") >= 0
    }


@contextmanager
def render(width: int, state: str, wizard: bool) -> Iterator[tuple[Any, ...]]:
    app = get_qapplication()
    messages: list[str] = []
    previous = qInstallMessageHandler(lambda _mode, _context, message: messages.append(message))
    host = SimpleNamespace(
        command_installed=state != "absent",
        command_available=True,
        command_status="Astra Cowork найден · доступен"
        if state != "absent"
        else "Astra Cowork не установлен",
        command_feedback=None,
        check_command_hotkey=Mock(return_value="ok"),
        reload_command_hotkey=Mock(),
        refresh_command_status=Mock(),
        _command_snapshot=Mock(return_value=SimpleNamespace(allowed=True)),
    )
    capture_host = SimpleNamespace(
        begin_capture=Mock(return_value=True),
        end_capture=Mock(),
        set_capture_callback=Mock(),
        probe=Mock(return_value="ok"),
        free_candidates=Mock(return_value=[]),
    )
    bridge = SettingsBridge(
        Settings(),
        save=Mock(),
        device_provider=lambda: [],
        capture_host=cast(CaptureHost, capture_host),
    )
    bridge.bind_command_host(host)
    engine = QQmlApplicationEngine()
    install_icon_provider(engine)
    component = None
    editor = None
    wizard_page = None
    try:
        # Mirror the application: load hidden QML, then provide the real bridge.
        engine.load(QUrl.fromLocalFile(str(ROOT / "qml/Main.qml")))
        assert engine.rootObjects(), messages
        window = cast(QQuickWindow, engine.rootObjects()[0])
        window.setWidth(width)
        window.setHeight(588)
        window.setProperty("freezeAnimations", True)
        QTest.qWait(30)
        engine.rootContext().setContextProperty("settingsBridge", bridge)
        if wizard:
            component = QQmlComponent(
                engine, QUrl.fromLocalFile(str(ROOT / "qml/onboarding/Step3Hotkey.qml"))
            )
            wizard_page = cast(QQuickItem, component.create())
            assert wizard_page is not None, [error.toString() for error in component.errors()]
            wizard_page.setParentItem(window.contentItem())
            engine.rootContext().setContextProperty("onboarding", bridge)
            editor = next(
                item
                for item in tree(wizard_page)
                if item.metaObject().indexOfProperty("wizard") >= 0
            )
        # Adding late context properties re-evaluates the QML initial width binding.
        window.setWidth(width)
        window.show()
        QTest.qWait(80)
        assert window.width() == width
        if not wizard:
            sidebar = next(
                item
                for item in tree(window.contentItem())
                if item.metaObject().indexOfProperty("sections") >= 0
            )
            sidebar.setProperty("currentIndex", 4)  # existing Advanced navigation entry
            QTest.qWait(40)
            editor = next(
                item
                for item in tree(window.contentItem())
                if item.metaObject().indexOfProperty("wizard") >= 0
            )
        assert editor is not None
        if state == "capture":
            QMetaObject.invokeMethod(
                buttons(editor)["Выбрать другую"], "clicked", Qt.DirectConnection
            )
            QTest.qWait(40)
            assert bridge.captureRole == "command" and bridge.captureState == "capturing"
        app.processEvents()
        yield editor, bridge, host, capture_host, messages
    finally:
        for window in engine.rootObjects():
            window.close()
        if wizard_page is not None:
            sip.delete(wizard_page)
        if component is not None:
            sip.delete(component)
        sip.delete(engine)
        sip.delete(bridge)
        app.processEvents()
        qInstallMessageHandler(previous)


@pytest.mark.parametrize("width", [900, 1035], ids=["minimum", "screenshot"])
@pytest.mark.parametrize("wizard", [False, True], ids=["settings", "wizard"])
@pytest.mark.parametrize("state", ["installed", "absent", "capture"])
def test_command_editor_content_padding_and_neighbor_rows(
    width: int, wizard: bool, state: str
) -> None:
    with render(width, state, wizard) as (editor, _bridge, _host, _capture, messages):
        assert editor.width() == pytest.approx(580 if wizard else min(796, width - 184 - 44) - 2)
        if wizard and state == "absent":
            assert not editor.isVisible() and editor.height() == 0
            assert not buttons(editor)
            assert not messages, messages
            return
        content = [item for item in tree(editor) if item.isVisible()]
        row_label = "Команда помощнику" if wizard else "Клавиша команды"
        row = next(item for item in content if item.property("label") == row_label)
        label = next(item for item in content if item.property("text") == row_label)
        assert box(label, editor).left() == pytest.approx(14, abs=1), (
            "Header must not get double padding"
        )
        padded: list[QQuickItem] = []
        if state != "absent":
            intro = next(
                item
                for item in content
                if str(item.property("text")).startswith("Win используется для записи")
            )
            padded.append(intro)
            assert box(intro, editor).left() == pytest.approx(14, abs=1)
            assert editor.width() - box(intro, editor).right() == pytest.approx(14, abs=1)
            assert box(intro, editor).top() - box(row, editor).bottom() == pytest.approx(8, abs=1)
            assert intro.property("lineHeight") == 18
            assert "После выхода меню снова доступно" not in intro.property("text")
        action_buttons = buttons(editor)
        lower_buttons = [
            item for item in action_buttons.values() if box(item, editor).top() >= row.height()
        ]
        padded.extend(lower_buttons)
        if state != "absent":
            win_buttons = [
                action_buttons[text] for text in ("Левая Win", "Правая Win", "Проверить снова")
            ]
            assert box(win_buttons[0], editor).top() - box(
                intro.parentItem(), editor
            ).bottom() == pytest.approx(8, abs=1)
            for left, right in zip(win_buttons, win_buttons[1:], strict=False):
                assert box(right, editor).left() - box(left, editor).right() == pytest.approx(
                    8, abs=1
                )
        if lower_buttons:
            assert min(box(item, editor).left() for item in lower_buttons) == pytest.approx(
                14, abs=1
            )
        if state == "capture":
            field = next(
                item for item in content if item.metaObject().indexOfProperty("state7") >= 0
            )
            padded.append(field)
            assert box(field, editor).top() - max(
                box(item, editor).bottom() for item in win_buttons
            ) == pytest.approx(8, abs=1)
            assert box(field, editor).left() == pytest.approx(14, abs=1)
            assert editor.width() - box(field, editor).right() == pytest.approx(14, abs=1)
        for item in padded:
            rect = box(item, editor)
            assert rect.left() >= 13 and rect.right() <= editor.width() - 13
            assert rect.bottom() <= editor.height() - 6
        if padded:
            assert editor.height() - max(
                box(item, editor).bottom() for item in padded
            ) == pytest.approx(7, abs=1)
        assert action_buttons["Выбрать другую"].isEnabled() == (state != "absent")
        assert ("Проверить снова" in action_buttons) == (state != "absent" or not wizard)
        if not wizard:
            column = editor.parentItem()
            assert column is not None
            neighbors = [
                item
                for item in column.childItems()
                if item.property("label")
                in ("Голосовые команды", "Показывать команду перед отправкой")
            ]
            assert len(neighbors) == 2
            assert box(neighbors[0], column).top() >= box(editor, column).bottom() - 1
            assert box(neighbors[1], column).top() >= box(neighbors[0], column).bottom() - 1
            card = column.parentItem()
            assert card is not None
            assert box(neighbors[1], card).bottom() <= card.height() - 1 + 1
        assert not messages, messages


@pytest.mark.parametrize("wizard", [False, True], ids=["settings", "wizard"])
def test_command_editor_buttons_keep_real_bridge_contract(wizard: bool) -> None:
    with render(1035, "installed", wizard) as (editor, bridge, host, capture, messages):
        controls = buttons(editor)
        QMetaObject.invokeMethod(controls["Правая Win"], "clicked", Qt.DirectConnection)
        assert bridge.commandHotkey == "Super_R"
        QMetaObject.invokeMethod(controls["Левая Win"], "clicked", Qt.DirectConnection)
        assert bridge.commandHotkey == "Super_L"
        assert host.reload_command_hotkey.call_count == 2
        # First show has already refreshed status through the late settings bridge.
        refreshes_before_click = host.refresh_command_status.call_count
        QMetaObject.invokeMethod(controls["Проверить снова"], "clicked", Qt.DirectConnection)
        assert host.refresh_command_status.call_count == refreshes_before_click + 1
        host.refresh_command_status.assert_called_with()
        QMetaObject.invokeMethod(controls["Выбрать другую"], "clicked", Qt.DirectConnection)
        QTest.qWait(30)
        assert bridge.captureRole == "command" and bridge.captureState == "capturing"
        capture.begin_capture.assert_called_once_with()
        field = next(item for item in tree(editor) if item.property("state7") == "capturing")
        ends_before_cancel = capture.end_capture.call_count
        QMetaObject.invokeMethod(field, "cancelRequested", Qt.DirectConnection)
        assert bridge.captureState == "idle"
        assert capture.end_capture.call_count == ends_before_cancel + 1
        capture.end_capture.assert_called_with()
        assert not messages, messages
