"""Sound switch user gestures, late real bridge, rollback and truthful status."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock

import pytest
from PyQt5 import sip
from PyQt5.QtCore import QPointF, Qt, QUrl, qInstallMessageHandler
from PyQt5.QtQml import QQmlApplicationEngine
from PyQt5.QtQuick import QQuickItem, QQuickWindow
from PyQt5.QtTest import QTest
from test_command_settings_layout import tree
from test_onboarding import ensure_control_in_viewport

from astra_voice.core.settings import Settings
from astra_voice.ui.bridges import SettingsBridge
from astra_voice.ui.icons import install_icon_provider
from helpers.qt_app import get_qapplication

pytestmark = pytest.mark.xvfb
ROOT = Path(__file__).resolve().parents[2]


@contextmanager
def sound_window(enabled: bool = False) -> Iterator[tuple[Any, ...]]:
    app = get_qapplication()
    messages: list[str] = []
    previous = qInstallMessageHandler(lambda _mode, _context, message: messages.append(message))
    saved = Mock()
    settings = Settings(sound_cues_enabled=enabled)
    bridge = SettingsBridge(settings, save=saved, device_provider=lambda: [])
    host = SimpleNamespace(
        sound_cues_status="", apply_sound_cues_enabled=Mock(), on_sound_cues_changed=None
    )
    bridge.bind_sound_cues_host(host)
    engine = QQmlApplicationEngine()
    install_icon_provider(engine)
    try:
        # The application loads QML first; the initial UI must not write defaults.
        engine.load(QUrl.fromLocalFile(str(ROOT / "qml/Main.qml")))
        assert engine.rootObjects(), messages
        window = cast(QQuickWindow, engine.rootObjects()[0])
        window.setWidth(900)
        window.setHeight(588)
        window.setProperty("freezeAnimations", True)
        window.show()
        QTest.qWait(30)
        row = next(
            item for item in tree(window.contentItem()) if item.objectName() == "soundCuesRow"
        )
        toggle = next(
            item for item in tree(window.contentItem()) if item.objectName() == "soundCuesToggle"
        )
        assert not toggle.property("checked") and not toggle.isEnabled()
        engine.rootContext().setContextProperty("settingsBridge", bridge)
        QTest.qWait(40)
        ensure_visible(window, toggle)
        yield window, row, toggle, bridge, settings, saved, host, messages
    finally:
        for window in engine.rootObjects():
            window.close()
        sip.delete(engine)
        sip.delete(bridge)
        app.processEvents()
        qInstallMessageHandler(previous)


def ensure_visible(window: QQuickWindow, control: QQuickItem) -> None:
    body = next(
        item
        for item in tree(window.contentItem())
        if item.metaObject().className() == "QQuickFlickable" and item.isVisible()
    )
    ensure_control_in_viewport(get_qapplication(), body, control)


def click(window: QQuickWindow, item: QQuickItem, x: float | None = None) -> None:
    point = item.mapToScene(QPointF(item.width() / 2 if x is None else x, item.height() / 2))
    QTest.mouseClick(window, Qt.LeftButton, pos=point.toPoint())
    QTest.qWait(30)


@pytest.mark.parametrize("enabled", [False, True])
def test_late_bridge_reads_sound_setting_without_saving(enabled: bool) -> None:
    with sound_window(enabled) as (_window, _row, toggle, bridge, settings, saved, host, messages):
        assert toggle.isEnabled() and toggle.property("checked") is enabled
        assert bridge.soundCuesEnabled is enabled and settings.sound_cues_enabled is enabled
        saved.assert_not_called()
        host.apply_sound_cues_enabled.assert_not_called()
        bridge.soundCuesEnabled = not enabled
        QTest.qWait(20)
        assert toggle.property("checked") is not enabled
        assert not messages, messages


@pytest.mark.parametrize("gesture", ["switch", "row", "space"])
def test_sound_user_activation_saves_and_failed_save_rolls_back(gesture: str) -> None:
    with sound_window() as (window, row, toggle, bridge, settings, saved, host, messages):

        def activate() -> None:
            ensure_visible(window, toggle)
            if gesture == "space":
                toggle.forceActiveFocus(Qt.TabFocusReason)
                QTest.keyClick(window, Qt.Key_Space)
                QTest.qWait(30)
            else:
                click(
                    window, toggle if gesture == "switch" else row, 18 if gesture == "row" else None
                )

        activate()
        assert settings.sound_cues_enabled and bridge.soundCuesEnabled
        assert toggle.property("checked")
        saved.assert_called_once_with(settings)
        host.apply_sound_cues_enabled.assert_called_once_with(True)
        saved.reset_mock()
        host.apply_sound_cues_enabled.reset_mock()
        saved.side_effect = OSError("private test failure")
        activate()
        assert settings.sound_cues_enabled and bridge.soundCuesEnabled
        assert toggle.property("checked")
        assert bridge.saveError == "Не удалось сохранить настройки"
        saved.assert_called_once_with(settings)
        host.apply_sound_cues_enabled.assert_not_called()
        # A later success still works: rollback must not detach the binding.
        saved.side_effect = None
        activate()
        assert not settings.sound_cues_enabled and not toggle.property("checked")
        assert bridge.saveError == ""
        assert not messages, messages


@pytest.mark.parametrize(
    "status",
    [
        "Звуковые сигналы недоступны: проигрыватель звука не найден",
        "Не удалось воспроизвести сигнал. Проверьте устройство вывода звука",
        "Звуковые сигналы приостановлены: сеанс заблокирован или его состояние неизвестно",
        # The string contract may carry a longer diagnostic; preserve all its text.
        "Звуковые сигналы приостановлены: сеанс заблокирован или его состояние неизвестно. "
        "Звуковые сигналы недоступны: проигрыватель звука не найден",
    ],
)
def test_sound_status_notify_is_wrapped_and_disappears_when_disabled(status: str) -> None:
    with sound_window(True) as (window, row, toggle, bridge, _settings, saved, host, messages):
        base_height = row.height()
        assert row.property("sub") == ""
        host.sound_cues_status = status
        host.on_sound_cues_changed()
        QTest.qWait(30)
        assert row.property("sub") == status
        assert row.height() > base_height
        text = next(item for item in tree(row) if item.property("text") == status)
        assert text.isVisible() and text.width() > 0
        if len(status) > 100:
            assert text.property("lineCount") >= 2
        assert text.mapToItem(row, QPointF(0, text.height())).y() <= row.height()
        assert toggle.isEnabled() and toggle.property("checked")
        saved.assert_not_called()
        ensure_visible(window, toggle)
        click(window, toggle)
        assert not bridge.soundCuesEnabled and row.property("sub") == ""
        assert row.height() == base_height
        assert not messages, messages
