"""Fly panel preference: persistence, bridge transactions and real offscreen QML."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast
from unittest.mock import Mock

import pytest
from PyQt5 import sip
from PyQt5.QtCore import QObject, QPointF, Qt, QUrl, pyqtSignal, qInstallMessageHandler
from PyQt5.QtQml import QQmlComponent, QQmlEngine
from PyQt5.QtQuick import QQuickItem, QQuickWindow
from PyQt5.QtTest import QSignalSpy, QTest

from astra_voice.core import settings as settings_mod
from astra_voice.core.settings import Settings
from astra_voice.ui.bridges import SettingsBridge
from astra_voice.ui.icons import install_icon_provider
from helpers.qt_app import get_qapplication

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]
LABEL = "Показывать значок на панели Fly"
TOOLTIP = (
    "Добавляет Astra Voice в список приоритетных значков панели Fly и один раз "
    "перезапускает панель. Отключение убирает только добавленную запись."
)


class FakeFlyTray(QObject):
    changed = pyqtSignal()

    def __init__(self, *, visible: bool = True, available: bool = True, reason: str = "") -> None:
        super().__init__()
        self.visible = visible
        self.available = available
        self.reason = reason
        self.set_enabled = Mock()


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"schema_version": 1},
        {"fly_tray_icon": None},
        {"fly_tray_icon": "false"},
        {"fly_tray_icon": 0},
    ],
)
def test_fly_preference_defaults_for_old_and_invalid_settings(data: dict[str, Any]) -> None:
    assert Settings().fly_tray_icon is True
    assert settings_mod.from_dict(data).fly_tray_icon is True


def test_fly_preference_missing_corrupt_and_false_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    assert settings_mod.load(path, appimage=False).fly_tray_icon is True
    path.write_text("broken", encoding="utf-8")
    assert settings_mod.load(path, appimage=False).fly_tray_icon is True
    settings_mod.save(Settings(fly_tray_icon=False), path)
    assert json.loads(path.read_text(encoding="utf-8"))["fly_tray_icon"] is False
    assert settings_mod.load(path, appimage=False).fly_tray_icon is False


def test_bridge_without_controller_is_hidden_and_has_no_actions() -> None:
    save = Mock()
    bridge = SettingsBridge(Settings(), save=save)
    assert bridge.flyTrayVisible is False
    assert bridge.flyTrayAvailable is False
    assert bridge.flyTrayReason == ""
    bridge.setProperty("flyTrayIcon", False)
    assert bridge.flyTrayIcon is True
    save.assert_not_called()


@pytest.mark.parametrize(
    "name,kind",
    [("flyTrayVisible", "bool"), ("flyTrayAvailable", "bool"), ("flyTrayReason", "QString")],
)
def test_bridge_publishes_readonly_status_with_live_notify(name: str, kind: str) -> None:
    control = FakeFlyTray()
    bridge = SettingsBridge(Settings(), fly_tray=control, save=Mock())
    prop = bridge.metaObject().property(bridge.metaObject().indexOfProperty(name))
    assert prop.typeName() == kind and not prop.isWritable() and prop.hasNotifySignal()
    spy = QSignalSpy(bridge.flyTrayStatusChanged)
    control.visible = False
    control.available = False
    control.reason = "Значок скрыт настройками Fly"
    control.changed.emit()
    assert bridge.flyTrayVisible is False
    assert bridge.flyTrayAvailable is False
    assert bridge.flyTrayReason == control.reason
    assert len(spy) == 1


def test_bridge_saves_mirrors_and_only_then_delegates_changed_values() -> None:
    settings = Settings()
    mirror = Settings()
    control = FakeFlyTray()
    observed: list[bool] = []

    def saving(current: Settings) -> None:
        # Applying before persistence would restart the panel even after a failed save.
        assert control.set_enabled.call_count == len(observed)
        observed.append(current.fly_tray_icon)

    save = Mock(side_effect=saving)
    bridge = SettingsBridge(settings, mirror=mirror, fly_tray=control, save=save)
    spy = QSignalSpy(bridge.flyTrayIconChanged)
    for value in (False, True):
        bridge.setProperty("flyTrayIcon", value)
        assert bridge.flyTrayIcon is settings.fly_tray_icon is mirror.fly_tray_icon is value
        control.set_enabled.assert_called_with(value, explicit=True)
        assert observed[-1] is value
        bridge.setProperty("flyTrayIcon", value)
    assert save.call_count == control.set_enabled.call_count == len(spy) == 2


def test_bridge_save_failure_rolls_back_and_does_not_delegate() -> None:
    settings = Settings()
    mirror = Settings()
    control = FakeFlyTray()
    save = Mock(side_effect=OSError("private test path"))
    bridge = SettingsBridge(settings, mirror=mirror, fly_tray=control, save=save)
    bridge.setProperty("flyTrayIcon", False)
    assert bridge.flyTrayIcon is settings.fly_tray_icon is mirror.fly_tray_icon is True
    assert bridge.saveError == "Не удалось сохранить настройки"
    control.set_enabled.assert_not_called()
    save.side_effect = None
    bridge.setProperty("flyTrayIcon", False)
    assert bridge.flyTrayIcon is False and bridge.saveError == ""
    control.set_enabled.assert_called_once_with(False, explicit=True)


@pytest.mark.parametrize("blocked", ["policy", "hidden", "unavailable"])
def test_bridge_blocked_preference_does_not_save_or_delegate(blocked: str) -> None:
    settings = Settings(fly_tray_icon=False)
    mirror = Settings(fly_tray_icon=True)
    control = FakeFlyTray(visible=blocked != "hidden", available=blocked != "unavailable")
    save = Mock()
    bridge = SettingsBridge(
        settings,
        mirror=mirror,
        fly_tray=control,
        save=save,
        locked=["fly_tray_icon"] if blocked == "policy" else [],
    )
    bridge.setProperty("flyTrayIcon", False)
    assert bridge.flyTrayIcon is mirror.fly_tray_icon is True
    assert settings.fly_tray_icon is False
    save.assert_not_called()
    control.set_enabled.assert_not_called()


def tree(item: QQuickItem) -> Iterator[QQuickItem]:
    yield item
    for child in item.childItems():
        yield from tree(child)


@contextmanager
def general_qml(control: FakeFlyTray | None, *, locked: bool = False) -> Iterator[tuple[Any, ...]]:
    app = get_qapplication()
    QQuickWindow.setSceneGraphBackend("software")
    messages: list[str] = []

    def handler(_mode: Any, _context: Any, message: str) -> None:
        messages.append(message)

    previous = qInstallMessageHandler(handler)
    save = Mock()
    bridge = SettingsBridge(
        Settings(),
        fly_tray=control,
        save=save,
        device_provider=lambda: [],
        locked=["fly_tray_icon"] if locked else [],
    )
    engine = QQmlEngine()
    install_icon_provider(engine)
    component = QQmlComponent(engine, QUrl.fromLocalFile(str(ROOT / "qml/sections/General.qml")))
    window = QQuickWindow()
    window.setWidth(700)
    window.setHeight(900)
    root: QQuickItem | None = None
    try:
        root = cast(QQuickItem, component.create())
        assert root is not None, [error.toString() for error in component.errors()]
        root.setParentItem(window.contentItem())
        root.setWidth(700)
        window.show()
        QTest.qWait(30)
        app.processEvents()
        row = next(item for item in tree(root) if item.objectName() == "flyTrayRow")
        toggle = next(item for item in tree(root) if item.objectName() == "flyTrayToggle")
        assert not row.isVisible() and row.height() == 0
        engine.rootContext().setContextProperty("settingsBridge", bridge)
        QTest.qWait(30)
        yield root, row, toggle, bridge, save, messages
    finally:
        window.close()
        if root is not None:
            sip.delete(root)
        sip.delete(component)
        sip.delete(engine)
        sip.delete(bridge)
        sip.delete(window)
        app.processEvents()
        qInstallMessageHandler(previous)


@pytest.mark.parametrize("visible", [False, True])
def test_general_fly_row_visibility_tooltip_and_height_collapse(visible: bool) -> None:
    control = FakeFlyTray(visible=visible)
    with general_qml(control) as (root, row, toggle, _bridge, save, messages):
        assert row.isVisible() is visible and (row.height() > 0) is visible
        assert row.property("label") == LABEL
        assert row.property("tooltipText") == TOOLTIP
        base_height = root.height()
        control.visible = not visible
        control.changed.emit()
        QTest.qWait(30)
        assert row.isVisible() is not visible
        assert (row.height() == 0) is visible
        assert (root.height() < base_height) is visible
        save.assert_not_called()
        control.set_enabled.assert_not_called()
        assert not messages, messages


@pytest.mark.parametrize("locked", [False, True])
def test_general_unavailable_reason_and_policy_disable_toggle(locked: bool) -> None:
    reason = "Значок скрыт настройками Fly"
    control = FakeFlyTray(available=False, reason=reason)
    with general_qml(control, locked=locked) as (_root, row, toggle, _bridge, save, messages):
        assert row.isVisible() and not toggle.isEnabled()
        assert row.property("sub") == reason
        control.available = True
        control.reason = ""
        control.changed.emit()
        QTest.qWait(30)
        assert toggle.isEnabled() is not locked
        assert row.property("sub") == ""
        assert row.property("locked") is locked
        save.assert_not_called()
        assert not messages, messages


def test_general_toggle_delegates_and_preserves_binding_after_save_failure() -> None:
    control = FakeFlyTray()
    with general_qml(control) as (_root, _row, toggle, bridge, save, messages):
        assert toggle.isEnabled() and toggle.property("checked") is True
        assert toggle.setProperty("checked", False)
        assert bridge.flyTrayIcon is False
        control.set_enabled.assert_called_once_with(False, explicit=True)
        save.side_effect = OSError("private path")
        assert toggle.setProperty("checked", True)
        assert toggle.property("checked") is bridge.flyTrayIcon is False
        assert control.set_enabled.call_count == 1
        save.side_effect = None
        assert toggle.setProperty("checked", True)
        assert toggle.property("checked") is bridge.flyTrayIcon is True
        control.set_enabled.assert_called_with(True, explicit=True)
        assert not messages, messages


@pytest.mark.parametrize("gesture", ["row", "switch"])
def test_general_user_mouse_gesture_saves_and_delegates(gesture: str) -> None:
    control = FakeFlyTray()
    with general_qml(control) as (root, row, toggle, bridge, save, messages):
        window = root.window()
        window.setHeight(int(root.height()) + 100)
        QTest.qWait(30)
        item = row if gesture == "row" else toggle
        point = item.mapToScene(
            QPointF(18 if gesture == "row" else item.width() / 2, item.height() / 2)
        )
        for enabled in (False, True):
            QTest.mouseClick(window, Qt.LeftButton, pos=point.toPoint())
            QTest.qWait(30)
            assert bridge.flyTrayIcon is toggle.property("checked") is enabled
            control.set_enabled.assert_called_with(enabled, explicit=True)
        assert save.call_count == control.set_enabled.call_count == 2
        assert not messages, messages
