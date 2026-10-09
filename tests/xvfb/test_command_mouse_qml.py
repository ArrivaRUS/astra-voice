"""Cowork relocation and safe UI fallback before the mouse runtime port exists."""

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
from PyQt5.QtCore import QMetaObject, QPoint, QPointF, Qt, QUrl, qInstallMessageHandler
from PyQt5.QtGui import QCursor, QGuiApplication
from PyQt5.QtQml import QQmlApplicationEngine, QQmlEngine, QQmlExpression
from PyQt5.QtQuick import QQuickItem, QQuickWindow
from PyQt5.QtTest import QTest
from test_command_settings_layout import render, tree
from test_onboarding import ensure_control_in_viewport

from astra_voice.core.settings import Settings
from astra_voice.ui.bridges import SettingsBridge
from astra_voice.ui.icons import install_icon_provider
from helpers.qt_app import get_qapplication

pytestmark = pytest.mark.xvfb
ROOT = Path(__file__).resolve().parents[2]


@contextmanager
def mouse_window(width: int, button: int = 2, runtime: Any = None) -> Iterator[tuple[Any, ...]]:
    """Use the real bridge's separate notify groups; only external host resources are fake."""
    app = get_qapplication()
    messages: list[str] = []
    previous = qInstallMessageHandler(lambda _mode, _context, message: messages.append(message))
    active: list[object] = []

    def begin() -> object:
        token = object()
        active[:] = [token]
        return token

    def end(token: object) -> None:
        if active and active[0] is token:
            active.clear()

    host = SimpleNamespace(
        command_installed=True,
        command_available=True,
        command_status="Astra Cowork найден · доступен",
        command_feedback=None,
        check_command_hotkey=Mock(return_value="ok"),
        reload_command_hotkey=Mock(),
        refresh_command_status=Mock(),
        _command_snapshot=Mock(return_value=SimpleNamespace(allowed=True)),
        command_mouse_can_edit=True,
        command_mouse_status="disabled",
        command_mouse_status_message="",
        begin_command_mouse_capture=begin,
        end_command_mouse_capture=end,
        command_mouse_capture_valid=lambda token: bool(active and active[0] is token),
        reload_command_mouse=Mock(),
        on_command_mouse_changed=None,
    )
    if runtime is not None:
        host = runtime
    settings = runtime.settings if runtime is not None else Settings(command_mouse_button=button)
    saved = Mock()
    bridge = SettingsBridge(settings, save=saved, device_provider=lambda: [])
    bridge.bind_command_host(host)
    engine = QQmlApplicationEngine()
    install_icon_provider(engine)
    try:
        engine.rootContext().setContextProperty("settingsBridge", bridge)
        engine.load(QUrl.fromLocalFile(str(ROOT / "qml/Main.qml")))
        assert engine.rootObjects(), messages
        window = cast(QQuickWindow, engine.rootObjects()[0])
        window.setProperty("freezeAnimations", True)
        window.setWidth(width)
        window.setHeight(588)
        sidebar = next(
            item
            for item in tree(window.contentItem())
            if item.metaObject().indexOfProperty("sections") >= 0
        )
        sidebar.setProperty("currentIndex", 4)
        window.show()
        QTest.qWait(80)
        bridge.attach_window(window)
        yield window, bridge, settings, saved, messages
    finally:
        if runtime is not None:
            runtime.shutdown()  # keep bound Qt signal receivers alive through shutdown
        for window in engine.rootObjects():
            window.close()
        sip.delete(engine)
        sip.delete(bridge)
        app.processEvents()
        qInstallMessageHandler(previous)


def assert_actual_selection(window: QQuickWindow, button: int) -> None:
    selector = next(
        item for item in tree(window.contentItem()) if item.objectName() == "commandMouseSelect"
    )
    expected = ["Средняя кнопка", "Дополнительная кнопка 1", "Дополнительная кнопка 2"]
    if button == 31:
        expected.append("Кнопка мыши 31")
    model = selector.property("model")
    assert (model.toVariant() if hasattr(model, "toVariant") else model) == expected
    index = {2: 0, 8: 1, 9: 2, 31: 3}[button]
    assert selector.property("currentIndex") == index
    assert selector.property("currentText") == expected[index]

    def evaluate(source: str) -> Any:
        expression = QQmlExpression(QQmlEngine.contextForObject(selector), selector, source)
        value, _undefined = expression.evaluate()
        assert not expression.hasError(), expression.error().toString()
        return value

    # QTest.mouseMove sends a Qt event but leaves X11's physical cursor where
    # it was. Popup opening then sees that cursor and legitimately highlights
    # the hovered row. Position it outside our popup only on an owned server.
    away = window.mapToGlobal(QPoint(5, 5))
    if QGuiApplication.platformName() == "xcb":
        assert os.environ.get("ASTRA_VOICE_TEST_X11_ISOLATED") == "1", (
            "native cursor positioning requires an explicitly isolated X11 server"
        )
        QCursor.setPos(away)
        QGuiApplication.sync()
        QTest.qWait(10)
        assert QCursor.pos() == away, "native cursor must be outside the popup before opening"
    QTest.mouseMove(window, QPoint(5, 5))
    QTest.qWait(10)
    evaluate("popup.open()")
    QTest.qWait(30)
    assert selector.property("highlightedIndex") == index
    assert evaluate("popup.contentItem.currentIndex") == index
    assert evaluate("popup.contentItem.currentItem !== null") is True
    assert evaluate("popup.contentItem.currentItem.index") == index
    assert evaluate("popup.contentItem.currentItem.modelData") == expected[index]
    evaluate("popup.close()")
    QTest.qWait(10)


def test_saved_custom_button_initial_selection_uses_actual_model_and_popup() -> None:
    with mouse_window(900, 31) as (window, _bridge, _settings, saved, messages):
        assert_actual_selection(window, 31)
        saved.assert_not_called()
        assert not messages, messages


def test_saved_button_transitions_keep_model_selection_and_popup_synchronised() -> None:
    with mouse_window(1035) as (window, bridge, settings, _saved, messages):
        assert_actual_selection(window, 2)
        for button in (31, 9, 31):
            assert bridge.selectCommandMouseButton(button)
            assert bridge.applyCommandMouseButton()
            QTest.qWait(30)
            assert settings.command_mouse_button == button and not settings.command_mouse_enabled
            assert_actual_selection(window, button)
        assert not messages, messages


@pytest.mark.parametrize("width", [900, 1035])
def test_tab_to_preview_reveals_entire_row_explanation_and_focus_ring(width: int) -> None:
    with mouse_window(width) as (window, bridge, _settings, saved, messages):
        items = list(tree(window.contentItem()))
        row = next(
            item for item in items if item.property("label") == "Показывать команду перед отправкой"
        )
        toggle = cast(QQuickItem, row.property("toggle"))
        first = next(item for item in items if item.property("text") == "Левая Win")
        first.forceActiveFocus(Qt.TabFocusReason)
        for _ in range(20):
            QTest.keyClick(window, Qt.Key_Tab)
            QTest.qWait(20)
            if toggle.property("activeFocus"):
                break
        assert toggle.property("activeFocus"), "preview must be reachable with normal Tab traversal"
        body = next(item for item in items if item.metaObject().indexOfProperty("contentY") >= 0)
        top = row.mapToItem(body, QPointF())
        bottom = row.mapToItem(body, QPointF(row.width(), row.height()))
        assert top.y() >= 0 and bottom.y() <= body.height(), (
            "show the entire row, not only its toggle"
        )
        explanation = next(
            item for item in tree(row) if item.property("text") == row.property("sub")
        )
        end = explanation.mapToItem(body, QPointF(explanation.width(), explanation.height()))
        assert explanation.isVisible() and end.y() <= body.height()
        focus_start = toggle.mapToItem(body, QPointF(-4, -4))
        focus_end = toggle.mapToItem(body, QPointF(toggle.width() + 4, toggle.height() + 4))
        assert focus_start.y() >= 0 and focus_end.y() <= body.height()
        assert focus_start.x() >= 0 and focus_end.x() <= body.width()
        assert not bridge.commandPreview
        saved.assert_not_called()
        assert not messages, messages


@pytest.mark.parametrize("width", [900, 1035])
def test_unwired_mouse_controls_are_visible_reachable_and_fail_closed(width: int) -> None:
    with render(width, "installed", False) as (editor, bridge, host, _capture, messages):
        window = editor.window()
        assert window is not None
        items = list(tree(window.contentItem()))
        assert any(item.property("text") == "Продвинутые настройки" for item in items)
        assert any(item.property("text") == "Голосовые команды Astra Cowork" for item in items)
        body = next(item for item in items if item.metaObject().indexOfProperty("contentY") >= 0)
        controls = {
            item.objectName(): item
            for item in items
            if item.objectName().startswith("commandMouse")
        }
        for name in ("commandMouseToggle", "commandMouseSelect", "commandMouseChoose"):
            control = controls[name]
            assert control.isVisible() and not control.isEnabled()
            ensure_control_in_viewport(get_qapplication(), body, control)
        assert not bridge._settings.command_mouse_enabled
        host.reload_command_hotkey.assert_not_called()
        assert not messages, messages


def test_keyboard_mode_link_returns_to_general_without_changing_preferences() -> None:
    with render(900, "installed", False) as (editor, bridge, _host, _capture, messages):
        window = editor.window()
        assert window is not None
        before = bridge._settings.to_dict()
        text = next(
            item
            for item in tree(editor)
            if str(item.property("text")).startswith("Режим клавиатуры:")
        )
        QMetaObject.invokeMethod(text.parentItem(), "clicked", Qt.DirectConnection)
        QTest.qWait(50)
        labels = {str(item.property("label")) for item in tree(window.contentItem())}
        assert not labels.intersection(
            {
                "Клавиша команды",
                "Голосовые команды",
                "Команда кнопкой мыши",
                "Показывать команду перед отправкой",
            }
        )
        assert window.activeFocusItem() is not None
        assert window.activeFocusItem().property("options") is not None
        assert bridge._settings.to_dict() == before
        assert not messages, messages
