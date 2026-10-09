"""Independent Cowork presence checks through actual QML and SettingsBridge.

The host has explicit installation state; no RuntimeRig or discovery fixture can
turn absence into presence. Every desktop/hardware port is fake.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock

import pytest
from PyQt5 import sip
from PyQt5.QtCore import QPointF, Qt, QUrl
from PyQt5.QtQml import QQmlComponent, QQmlEngine, QQmlExpression
from PyQt5.QtQuick import QQuickItem, QQuickWindow
from PyQt5.QtTest import QTest
from test_command_mouse_qml import mouse_window
from test_command_settings_layout import tree
from test_onboarding import ensure_control_in_viewport

from astra_voice.core.settings import Settings
from astra_voice.ui.hotkey_capture import CaptureHost
from helpers.qt_app import get_qapplication

pytestmark = pytest.mark.xvfb


@contextmanager
def settings_window(
    width: int, *, installed: bool, saved_on: bool = True, available: bool = True
) -> Iterator[tuple[Any, ...]]:
    active: list[object] = []
    host: Any = SimpleNamespace(
        settings=Settings(
            hotkey="Ctrl+Space",
            command_hotkey="Super_R",
            command_enabled=saved_on,
            command_mouse_enabled=saved_on,
            command_mouse_button=9,
            command_preview=saved_on,
        ),
        command_installed=installed,
        command_available=available,
        command_status="Astra Cowork найден · не запущен"
        if installed
        else "Astra Cowork не установлен",
        command_feedback=None,
        check_command_hotkey=Mock(return_value="ok"),
        reload_command_hotkey=Mock(),
        refresh_command_status=Mock(),
        _command_snapshot=Mock(return_value=SimpleNamespace(allowed=True)),
        command_mouse_can_edit=True,
        command_mouse_status="unavailable",  # absence must outrank port errors
        command_mouse_status_message="Кнопка мыши недоступна",
        end_command_mouse_capture=Mock(side_effect=lambda token: active.clear()),
        command_mouse_capture_valid=lambda token: bool(
            host.command_installed and active and active[0] is token
        ),
        reload_command_mouse=Mock(),
        shutdown=Mock(),
    )

    def begin() -> object:
        token = object()
        active[:] = [token]
        return token

    host.begin_command_mouse_capture = Mock(side_effect=begin)
    with mouse_window(width, runtime=host) as result:
        # Advanced performs one discovery read when mounted. Actions below are
        # measured after that explicit initial read, not against a zero baseline.
        host.refresh_command_status.assert_called_once_with()
        host.refresh_command_status.reset_mock()
        yield (host, *result)


def row(window: QQuickWindow, label: str) -> QQuickItem:
    return next(item for item in tree(window.contentItem()) if item.property("label") == label)


def editor(window: QQuickWindow) -> QQuickItem:
    return next(
        item
        for item in tree(window.contentItem())
        if item.metaObject().indexOfProperty("wizard") >= 0
    )


def controls(window: QQuickWindow) -> dict[str, QQuickItem]:
    keyboard = editor(window)
    items = list(tree(window.contentItem()))
    result = {
        text: next(item for item in tree(keyboard) if item.property("text") == text)
        for text in ("Выбрать другую", "Левая Win", "Правая Win", "Проверить снова")
    }
    result["keyboard mode"] = next(
        item.parentItem()
        for item in tree(keyboard)
        if str(item.property("text")).startswith("Режим клавиатуры:")
    )
    for name in ("commandMouseToggle", "commandMouseSelect", "commandMouseChoose"):
        result[name] = next(item for item in items if item.objectName() == name)
    for label in ("Голосовые команды", "Показывать команду перед отправкой"):
        result[label] = cast(QQuickItem, row(window, label).property("toggle"))
    return result


def theme(item: QQuickItem, token: str) -> Any:
    expression = QQmlExpression(QQmlEngine.contextForObject(item), item, "Theme." + token)
    value, undefined = expression.evaluate()
    assert not undefined and not expression.hasError(), expression.error().toString()
    return value


def click(window: QQuickWindow, item: QQuickItem) -> None:
    body = next(
        child
        for child in tree(window.contentItem())
        if child.metaObject().indexOfProperty("contentY") >= 0
    )
    ensure_control_in_viewport(get_qapplication(), body, item)
    position = item.mapToItem(
        window.contentItem(), QPointF(item.width() / 2, item.height() / 2)
    ).toPoint()
    QTest.mouseClick(window, Qt.LeftButton, pos=position)
    QTest.qWait(20)


@pytest.mark.parametrize("width", [900, 1035])
@pytest.mark.parametrize("saved_on", [False, True])
def test_absent_controls_are_disabled_gray_and_preserve_preferences(
    width: int, saved_on: bool
) -> None:
    with settings_window(width, installed=False, saved_on=saved_on) as result:
        host, window, bridge, settings, saved, messages = result
        before = settings.to_dict()
        assert not bridge.commandInstalled
        # Deliberately contradictory availability verifies the installation gate.
        assert bridge.commandAvailable
        found = controls(window)
        for name, item in found.items():
            assert item.isVisible(), name
            assert item.isEnabled() is (name == "Проверить снова"), name
        assert row(window, "Клавиша команды").property("sub") == "Astra Cowork не установлен"
        for label in (
            "Голосовые команды",
            "Показывать команду перед отправкой",
            "Команда кнопкой мыши",
            "Кнопка мыши",
        ):
            setting_row = row(window, label)
            assert not setting_row.property("rowEnabled")
            assert setting_row.property("labelColor") == theme(setting_row, "fgDisabled")
            assert setting_row.property("subColor") == theme(setting_row, "fgDisabled")
        for name in (
            "Голосовые команды",
            "Показывать команду перед отправкой",
            "commandMouseToggle",
        ):
            toggle = found[name]
            assert toggle.property("checked") is saved_on
            indicator = cast(QQuickItem, toggle.property("indicator"))
            assert indicator.property("color") == theme(toggle, "toggleOffBg")
            assert indicator.property("opacity") == 1
        chip = next(
            item
            for item in tree(editor(window))
            if item.property("text") == "Правая Win"
            and item.metaObject().indexOfProperty("radius") >= 0
        )
        assert not chip.isEnabled()
        chip_text = next(
            item
            for item in tree(chip)
            if item is not chip and item.property("text") == "Правая Win"
        )
        assert chip_text.property("color") == theme(chip, "fgDisabled")
        visible_text = [
            item
            for item in tree(window.contentItem())
            if item.isVisible() and item.property("text")
        ]
        assert any(
            item.property("text") == "Голосовые команды доступны после установки Astra Cowork"
            for item in visible_text
        )
        assert not any(
            item.property("text") == "Кнопка мыши недоступна — выберите другую"
            for item in visible_text
        )
        for name, item in found.items():
            if name != "Проверить снова":
                click(window, item)
        assert settings.to_dict() == before
        saved.assert_not_called()
        host.begin_command_mouse_capture.assert_not_called()
        host.reload_command_hotkey.assert_not_called()
        assert bridge.commandMouseCaptureState == "idle"
        click(window, found["Проверить снова"])
        host.refresh_command_status.assert_called_once_with()
        assert settings.to_dict() == before
        saved.assert_not_called()
        assert not messages, messages


@pytest.mark.parametrize("width", [900, 1035])
def test_installed_but_not_running_is_editable(width: int) -> None:
    with settings_window(width, installed=True, available=False) as result:
        _host, window, bridge, _settings, saved, messages = result
        assert bridge.commandInstalled and not bridge.commandAvailable
        assert bridge.commandStatus == "Astra Cowork найден · не запущен"
        for name, item in controls(window).items():
            assert item.isVisible() and item.isEnabled(), name
        for label in (
            "Голосовые команды",
            "Показывать команду перед отправкой",
            "Команда кнопкой мыши",
        ):
            toggle = cast(QQuickItem, row(window, label).property("toggle"))
            assert cast(QQuickItem, toggle.property("indicator")).property("color") == theme(
                toggle, "toggleOnBg"
            )
        saved.assert_not_called()
        assert not messages, messages


@pytest.mark.parametrize("width", [900, 1035])
def test_refresh_restores_controls_without_saving_or_moving_focus(width: int) -> None:
    with settings_window(width, installed=True) as result:
        host, window, bridge, settings, saved, messages = result
        before = settings.to_dict()
        found = controls(window)
        refresh = found["Проверить снова"]
        refresh.forceActiveFocus(Qt.TabFocusReason)
        refresh_states = iter((False, False, True))

        def update() -> None:
            host.command_installed = next(refresh_states)
            host.command_available = host.command_installed
            host.command_status = (
                "Astra Cowork найден · не запущен"
                if host.command_installed
                else "Astra Cowork не установлен"
            )
            host.on_command_changed()

        host.refresh_command_status.side_effect = update
        for installed in (False, False, True):
            bridge.refreshCommandStatus()
            QTest.qWait(20)
            assert bridge.commandInstalled is installed
            for name, item in found.items():
                assert item.isEnabled() is (installed or name == "Проверить снова"), name
            assert window.activeFocusItem() is refresh
            assert settings.to_dict() == before
            saved.assert_not_called()
        assert host.refresh_command_status.call_count == 3
        assert not messages, messages


def test_absence_discards_staged_mouse_assignment_without_saving() -> None:
    with settings_window(900, installed=True) as result:
        host, window, bridge, settings, saved, messages = result
        before = settings.to_dict()
        assert bridge.selectCommandMouseButton(8)
        assert bridge.commandMouseCaptureState == "ready"
        host.command_installed = False
        host.on_command_changed()
        QTest.qWait(20)
        assert bridge.commandMouseCaptureState == "idle"
        assert bridge.commandMousePendingButton == 0
        assert not bridge.applyCommandMouseButton()
        assert not controls(window)["commandMouseChoose"].isEnabled()
        assert settings.to_dict() == before
        saved.assert_not_called()
        assert host.end_command_mouse_capture.call_count == 1
        assert not messages, messages


@pytest.mark.parametrize("width", [900, 1035])
def test_absence_leaves_general_dictation_controls_available(width: int) -> None:
    with settings_window(width, installed=False) as result:
        _host, window, bridge, settings, saved, messages = result
        before = settings.to_dict()
        sidebar = next(
            item
            for item in tree(window.contentItem())
            if item.metaObject().indexOfProperty("sections") >= 0
        )
        sidebar.setProperty("currentIndex", 0)
        QTest.qWait(30)
        general_row = row(window, "Горячая клавиша")
        change = next(item for item in tree(general_row) if item.property("text") == "Изменить")
        assert change.isVisible() and change.isEnabled()
        chip = next(
            item
            for item in tree(general_row)
            if item.property("text") == "Ctrl+Space"
            and item.metaObject().indexOfProperty("radius") >= 0
        )
        assert chip.isEnabled()
        chip_text = next(
            item
            for item in tree(chip)
            if item is not chip and item.property("text") == "Ctrl+Space"
        )
        assert chip_text.property("color") == theme(chip, "hotkeyChipFg")
        mode = next(
            item
            for item in tree(window.contentItem())
            if item.metaObject().indexOfProperty("options") >= 0
        )
        assert mode.isVisible() and mode.isEnabled()
        assert settings.to_dict() == before and bridge.hotkey == "Ctrl+Space"
        saved.assert_not_called()
        assert not messages, messages


@pytest.mark.parametrize("locked", [False, True])
def test_shared_switch_keeps_default_checked_appearance(locked: bool) -> None:
    get_qapplication()
    engine = QQmlEngine()
    path = Path(__file__).resolve().parents[2] / "qml/components/AvToggle.qml"
    component = QQmlComponent(engine, QUrl.fromLocalFile(str(path)))
    toggle = cast(QQuickItem, component.create())
    assert toggle is not None, [error.toString() for error in component.errors()]
    try:
        toggle.setProperty("checked", True)
        toggle.setProperty("locked", locked)
        toggle.setProperty("enabled", False)
        QTest.qWait(140)
        indicator = cast(QQuickItem, toggle.property("indicator"))
        color_name = "toggleLockedOnBg" if locked else "toggleOnBg"
        assert toggle.property("muted") is False
        assert indicator.property("color") == theme(toggle, color_name)
        knob = next(item for item in indicator.childItems() if item.property("x") > 0)
        saved_x = knob.property("x")
        toggle.setProperty("muted", True)
        assert toggle.property("checked") is True
        assert indicator.property("color") == theme(toggle, "toggleOffBg")
        assert knob.property("x") == saved_x
    finally:
        sip.delete(toggle)
        sip.delete(component)
        sip.delete(engine)
        get_qapplication().processEvents()


def test_late_bridge_is_checking_and_cannot_edit_or_refresh() -> None:
    get_qapplication()
    engine = QQmlEngine()
    path = Path(__file__).resolve().parents[2] / "qml/components/CommandHotkeySettings.qml"
    component = QQmlComponent(engine, QUrl.fromLocalFile(str(path)))
    keyboard = cast(QQuickItem, component.create())
    assert keyboard is not None, [error.toString() for error in component.errors()]
    try:
        assert keyboard.property("bridge") is None
        setting_row = next(item for item in tree(keyboard) if item.property("label"))
        assert setting_row.property("sub") == "Проверяю Astra Cowork…"
        for text in ("Выбрать другую", "Левая Win", "Правая Win", "Проверить снова"):
            button = next(item for item in tree(keyboard) if item.property("text") == text)
            assert button.isVisible() and not button.isEnabled(), text
    finally:
        sip.delete(keyboard)
        sip.delete(component)
        sip.delete(engine)
        get_qapplication().processEvents()


KEYBOARD_RESULTS = ("capturing", "conflict", "duplicate", "not-grabbed", "success")


def keyboard_host(bridge: Any) -> Mock:
    host = Mock(spec=CaptureHost)
    host.begin_capture.return_value = True
    host.probe.return_value = "ok"
    host.free_candidates.return_value = []
    bridge._capture.host = host
    return host


def stage_command_capture(bridge: Any, state: str) -> Mock:
    host = keyboard_host(bridge)
    host.probe.return_value = {
        "conflict": "busy",
        "duplicate": "duplicate",
        "not-grabbed": "not-grabbed",
    }.get(state, "ok")
    bridge.beginCommandCapture()
    assert bridge.captureState == "capturing" and bridge.captureRole == "command"
    if state != "capturing":
        bridge.endCapture("Ctrl+F9")
    QTest.qWait(20)
    assert bridge.captureState == state
    return host


@pytest.mark.parametrize("state", KEYBOARD_RESULTS)
def test_installation_loss_closes_command_keyboard_capture_or_result(state: str) -> None:
    with settings_window(900, installed=True) as result:
        host, window, bridge, settings, saved, messages = result
        capture = stage_command_capture(bridge, state)
        field = next(
            item
            for item in tree(editor(window))
            if item.metaObject().indexOfProperty("state7") >= 0
        )
        assert field.isVisible()
        before = settings.to_dict()
        saved.reset_mock()
        host.command_installed = False
        host.on_command_changed()
        QTest.qWait(20)
        assert bridge.captureState == "idle"
        assert bridge.pendingCombo == ""
        assert not field.isVisible()
        capture.end_capture.assert_called()
        assert settings.to_dict() == before
        saved.assert_not_called()
        assert not messages, messages


@pytest.mark.parametrize("state", KEYBOARD_RESULTS)
@pytest.mark.parametrize("action", ["keep", "end", "apply"])
def test_absence_rejects_stale_keyboard_keep_end_and_save(state: str, action: str) -> None:
    with settings_window(900, installed=True) as result:
        host, window, bridge, settings, saved, messages = result
        capture = stage_command_capture(bridge, state)
        before = settings.to_dict()
        saved.reset_mock()
        host.command_installed = False
        host.on_command_changed()
        QTest.qWait(20)
        capture.probe.return_value = "ok"
        if action == "keep":
            bridge.keepCombo()
        elif action == "end":
            bridge.endCapture("Ctrl+F10")
        else:
            bridge.save_command_capture_combo("Ctrl+F10", True)
        QTest.qWait(20)
        assert settings.to_dict() == before
        saved.assert_not_called()
        assert not controls(window)["Выбрать другую"].isEnabled()
        assert not messages, messages


def test_installation_loss_during_captured_signal_prevents_save() -> None:
    with settings_window(900, installed=True) as result:
        host, _window, bridge, settings, saved, messages = result
        keyboard_host(bridge)
        before = settings.to_dict()
        observed: list[str] = []

        def changed() -> None:
            observed.append(bridge.captureState)
            if bridge.captureState == "captured":
                host.command_installed = False
                host.on_command_changed()

        bridge.captureStateChanged.connect(changed)
        try:
            bridge.beginCommandCapture()
            bridge.endCapture("Ctrl+F9")
            QTest.qWait(20)
            assert "captured" in observed
            assert not bridge.commandInstalled
            assert settings.to_dict() == before
            saved.assert_not_called()
            assert bridge.captureState == "idle"
        finally:
            bridge.captureStateChanged.disconnect(changed)
        assert not messages, messages


def test_cowork_loss_does_not_cancel_or_block_text_keyboard_capture() -> None:
    with settings_window(900, installed=True) as result:
        host, _window, bridge, settings, saved, messages = result
        keyboard_host(bridge)
        apply = Mock()
        apply.hotkey.return_value = "ok"
        bridge._apply = apply
        command_key = settings.command_hotkey
        bridge.beginCapture()
        assert bridge.captureRole == "text" and bridge.captureState == "capturing"
        host.command_installed = False
        host.on_command_changed()
        QTest.qWait(20)
        assert bridge.captureRole == "text" and bridge.captureState == "capturing"
        bridge.endCapture("Ctrl+F8")
        assert bridge.captureState == "success"
        assert settings.hotkey == "Ctrl+F8" and settings.command_hotkey == command_key
        saved.assert_called_once_with(settings)
        assert not messages, messages
