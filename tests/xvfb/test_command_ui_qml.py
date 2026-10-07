"""Actual QML command status and preview, offscreen on a private session bus."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest
from PyQt5.QtCore import QMetaObject, QObject, Qt, QUrl
from PyQt5.QtQml import QQmlApplicationEngine, QQmlComponent

from astra_voice.core.settings import Settings
from astra_voice.ui.bridges import SettingsBridge
from astra_voice.ui.icons import install_icon_provider
from helpers.qt_app import get_qapplication

pytestmark = pytest.mark.xvfb
ROOT = Path(__file__).resolve().parents[2]


def test_command_dialog_cancel_clears_private_message() -> None:
    app = get_qapplication()
    host = SimpleNamespace(
        command_installed=True,
        command_available=True,
        command_status="Astra Cowork найден · доступен",
        command_feedback=None,
        command_preview_shown=Mock(),
        reject_command=Mock(),
        confirm_command=Mock(),
        check_command_hotkey=Mock(return_value="ok"),
        reload_command_hotkey=Mock(),
        refresh_command_status=Mock(),
        _command_snapshot=Mock(return_value=SimpleNamespace(allowed=True)),
    )
    settings = Settings(command_preview=True)
    bridge = SettingsBridge(settings, save=Mock(), device_provider=lambda: [])
    bridge.bind_command_host(host)
    engine = QQmlApplicationEngine()
    install_icon_provider(engine)
    engine.rootContext().setContextProperty("settingsBridge", bridge)
    engine.load(QUrl.fromLocalFile(str(ROOT / "qml/Main.qml")))
    assert engine.rootObjects()
    root = engine.rootObjects()[0]
    root.show()
    bridge.update_command_preview("PRIVATE COMMAND MARKER")
    app.processEvents()
    host.command_preview_shown.assert_called_once_with()
    bridge.rejectCommand()
    app.processEvents()
    assert bridge.commandPreviewText == ""
    host.reject_command.assert_called_once_with()
    host.confirm_command.assert_not_called()
    bridge.update_command_preview("SECOND PRIVATE COMMAND")
    app.processEvents()
    root.hide()
    app.processEvents()
    assert bridge.commandPreviewText == ""
    assert host.reject_command.call_count == 2
    engine.deleteLater()
    app.processEvents()


@pytest.mark.parametrize("state", ["command-failed", "command-unknown"])
def test_command_pill_has_room_for_fixed_text_and_recovery_buttons(state: str) -> None:
    get_qapplication()
    engine = QQmlApplicationEngine()
    install_icon_provider(engine)
    component = QQmlComponent(engine, QUrl.fromLocalFile(str(ROOT / "qml/Pill.qml")))
    root: Any = component.create()
    assert root is not None, [error.toString() for error in component.errors()]
    root.setProperty("avState", state)
    QMetaObject.invokeMethod(root, "forceLayout", Qt.DirectConnection)
    caption = root.findChild(QObject, "commandPillCaption")
    content = root.findChild(QObject, "commandPillContent")
    assert caption is not None and content is not None
    assert caption.property("width") <= 420
    assert content.property("width") + root.property("paddingX") * 2 <= root.property("pillWidth")
    assert root.property("pillWidth") <= 520
    assert root.property("pillHeight") >= 36
    assert root.property("error")
    assert root.property("commandResult")
    assert root.property("defaultLabel")
    root.deleteLater()
    engine.deleteLater()
