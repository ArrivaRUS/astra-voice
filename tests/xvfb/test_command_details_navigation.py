"""The existing details action navigates to Cowork and opens its original dialog."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from PyQt5.QtCore import QObject
from PyQt5.QtGui import QGuiApplication
from PyQt5.QtTest import QTest
from test_command_settings_layout import render, tree

pytestmark = pytest.mark.xvfb


@pytest.mark.parametrize("width", [900, 1035])
def test_command_details_signal_shows_advanced_and_original_dialog(width: int) -> None:
    with render(width, "installed", False) as (editor, bridge, host, _capture, messages):
        window = editor.window()
        assert window is not None
        sidebar = next(
            item
            for item in tree(window.contentItem())
            if item.metaObject().indexOfProperty("sections") >= 0
        )
        dialog = next(
            item
            for item in window.findChildren(QObject)
            if item.property("heading") == "Команда помощнику"
            and item.property("confirmText") == "Понятно"
        )
        detail = "Не удалось отправить команду. Проверьте доступность Astra Cowork."
        host.command_feedback = SimpleNamespace(detail=detail, text="Команда не отправлена")
        bridge.commandStateChanged.emit()
        sidebar.setProperty("currentIndex", 0)
        window.hide()
        QTest.qWait(30)
        assert not dialog.property("visible")

        # Exercise the application's real bridge slot and QML signal connection.
        bridge.showCommandDetails()
        QTest.qWait(80)

        assert window.isVisible()
        assert sidebar.property("currentIndex") == 4
        assert dialog.property("visible")
        assert dialog.property("message") == detail
        assert any(
            item.isVisible() and item.property("text") == detail
            for item in tree(window.contentItem())
        ), "The original dialog must render the bridge's detailed recovery message"
        # Offscreen cannot raise native windows; keep the actual raise() call
        # and reject every other diagnostic, including all QML warnings.
        unexpected = [
            message
            for message in messages
            if not (
                QGuiApplication.platformName() == "offscreen"
                and message == "This plugin does not support raise()"
            )
        ]
        assert not unexpected, unexpected
