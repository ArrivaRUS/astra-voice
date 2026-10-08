"""Геометрия строки микрофона: реальный Main.qml, offscreen, без звука.

Состояния приходят через настоящий SettingsBridge. Подставляется только
SettingsApply: он возвращает детерминированный MicrophoneState и не вызывает ОС.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from PyQt5 import sip
from PyQt5.QtCore import QPointF, QRectF, QSizeF, QUrl, qInstallMessageHandler
from PyQt5.QtQml import QQmlApplicationEngine
from PyQt5.QtQuick import QQuickItem, QQuickWindow
from PyQt5.QtTest import QTest

from astra_voice.core.settings import Settings
from astra_voice.platform.sound import MicrophoneState
from astra_voice.ui.bridges import SettingsApply, SettingsBridge
from astra_voice.ui.icons import install_icon_provider
from helpers.qt_app import get_qapplication

pytestmark = pytest.mark.xvfb
QML = Path(__file__).resolve().parents[2] / "qml"
os.environ["QT_QUICK_CONTROLS_STYLE"] = "Default"

STATES = {
    "unknown": MicrophoneState(),
    "muted": MicrophoneState(known=True, muted=True, percent=60),
    "normal": MicrophoneState(known=True, muted=False, percent=60),
    "locked": MicrophoneState(known=True, muted=False, percent=60),
    "locked_unknown": MicrophoneState(),
    "locked_muted": MicrophoneState(known=True, muted=True, percent=60),
}


def visual_tree(root: QQuickItem) -> Iterator[QQuickItem]:
    yield root
    for child in root.childItems():
        yield from visual_tree(child)


@dataclass
class Rendered:
    window: QQuickWindow
    row: QQuickItem
    bridge: SettingsBridge
    messages: list[str]
    apply: Any
    state: list[MicrophoneState]


@contextmanager
def render(
    width: int, state: str, *, can_raise: bool = True, late_bridge: bool = False
) -> Iterator[Rendered]:
    app = get_qapplication()
    messages: list[str] = []

    def handler(_mode: Any, _context: Any, message: str) -> None:
        messages.append(message)

    previous = qInstallMessageHandler(handler)
    # Это только контракт SettingsApply: ни SoundControl, ни системных команд.
    mic_state = [STATES[state]]
    apply = SimpleNamespace(
        has_volume_control=can_raise,
        has_sound_settings=True,
        has_sound_service=False,
        can_restore_microphone_volume=False,
        microphone_state=lambda: mic_state[0],
    )
    bridge = SettingsBridge(
        Settings(),
        apply=cast(SettingsApply, apply),
        locked=["device", "mic_volume_on_start"] if state.startswith("locked") else [],
        device_provider=lambda: [],
        save=lambda settings: None,
    )
    bridge.refreshDevices()
    bridge.refreshMicrophone()
    engine = QQmlApplicationEngine()
    install_icon_provider(engine)
    if not late_bridge:
        engine.rootContext().setContextProperty("settingsBridge", bridge)
    try:
        engine.load(QUrl.fromLocalFile(str(QML / "Main.qml")))
        assert engine.rootObjects(), messages
        window = cast(QQuickWindow, engine.rootObjects()[0])
        window.setWidth(width)
        window.setHeight(588)
        window.setProperty("freezeAnimations", True)
        if late_bridge:
            assert not window.isVisible()
            QTest.qWait(80)
            engine.rootContext().setContextProperty("settingsBridge", bridge)
            QTest.qWait(80)
        window.show()  # Только QT_QPA_PLATFORM=offscreen, без DISPLAY/Wayland.
        QTest.qWait(120)
        app.processEvents()
        rows = [
            item
            for item in visual_tree(window.contentItem())
            if item.property("label") == "Микрофон"
        ]
        assert len(rows) == 1, messages
        assert bridge.canRaiseMicrophone == can_raise
        yield Rendered(window, rows[0], bridge, messages, apply, mic_state)
    finally:
        for root in engine.rootObjects():
            if isinstance(root, QQuickWindow):
                root.close()
        sip.delete(engine)
        sip.delete(bridge)
        app.processEvents()
        qInstallMessageHandler(previous)


def rect(item: QQuickItem, row: QQuickItem) -> QRectF:
    return QRectF(item.mapToItem(row, QPointF()), QSizeF(item.width(), item.height()))


def foreground(row: QQuickItem) -> dict[str, QQuickItem]:
    visible = [item for item in visual_tree(row) if item.isVisible()]
    label = next(item for item in visible if item.property("text") == "Микрофон")
    hint = next(item for item in visible if item.property("text") == "?").parentItem()
    assert hint is not None
    selector = next(
        item
        for item in visible
        if item.metaObject().indexOfProperty("popupMaxWidth") >= 0
        and item.metaObject().indexOfProperty("currentIndex") >= 0
    )
    result = {"label": label, "help": hint, "selector": selector}
    for item in visible:
        text = item.property("text")
        if item.metaObject().indexOfProperty("stepSize") >= 0:
            result["slider"] = item
        elif isinstance(text, str) and (
            text.startswith("Не удалось узнать громкость")
            or text.startswith("Звук микрофона выключен")
            or text.endswith(" %")
        ):
            result["status"] = item
        elif text in ("Поднять", "Вернуть", "Настройки звука…") and (
            item.metaObject().indexOfProperty("small") >= 0
        ):
            result[str(text)] = item
        elif text == "Задано администратором":
            result["policy"] = item
    return result


def assert_layout(rendered: Rendered, state: str) -> None:
    row = rendered.row
    items = foreground(row)
    boxes = {name: rect(item, row) for name, item in items.items()}
    details = {name: (box.x(), box.y(), box.width(), box.height()) for name, box in boxes.items()}
    assert row.width() > 0 and row.height() > 0, details
    for name, box in boxes.items():
        assert box.width() > 0 and box.height() > 0, (name, details)
        assert box.left() >= -1 and box.top() >= -1, (name, details)
        assert box.right() <= row.width() + 1, (name, details)
        assert box.bottom() <= row.height() + 1, (name, details)
    pairs = list(boxes.items())
    for index, (left_name, left) in enumerate(pairs):
        for right_name, right in pairs[index + 1 :]:
            overlap = left.intersected(right)
            assert overlap.width() <= 1 or overlap.height() <= 1, (
                left_name,
                right_name,
                details,
            )
    # Название настройки остаётся на основной строке выбора устройства.
    assert boxes["label"].center().y() <= boxes["selector"].bottom() + 1, details
    assert boxes["label"].width() >= float(items["label"].property("implicitWidth")) - 1, details
    assert items["selector"].isEnabled() == (not state.startswith("locked"))
    assert "Настройки звука…" in items
    texts = [item.property("text") for item in visual_tree(row) if item.isVisible()]
    if STATES[state].known is False:
        assert rendered.bridge.microphoneVolume == -1 and not rendered.bridge.microphoneMuted
        assert "Не удалось узнать громкость микрофона" in texts
        assert "slider" not in items and "Поднять" not in items
    elif STATES[state].muted:
        assert "Звук микрофона выключен в системе — вас не слышно" in texts
        assert "slider" not in items and "Поднять" in items
    else:
        assert "slider" in items and "60 %" in texts
    if state.startswith("locked"):
        assert "Задано администратором" in texts
    assert not rendered.messages, rendered.messages


@pytest.mark.parametrize("width", [900, 1035], ids=["minimum", "screenshot"])
@pytest.mark.parametrize("state", list(STATES))
def test_microphone_fields_fit_without_overlap(width: int, state: str) -> None:
    with render(width, state) as rendered:
        assert_layout(rendered, state)


@pytest.mark.parametrize("width", [900, 1035], ids=["minimum", "screenshot"])
@pytest.mark.parametrize("locked", [False, True], ids=["editable", "locked"])
def test_microphone_capability_and_state_transitions_have_stable_layout(
    width: int, locked: bool
) -> None:
    prefix = "locked_" if locked else ""
    with render(width, prefix + "unknown", can_raise=False) as rendered:
        # Внешняя capability приходит позже загрузки QML — startup из реального журнала.
        for state in ("unknown", "normal", "muted", "unknown"):
            key = "locked" if locked and state == "normal" else prefix + state
            rendered.state[0] = STATES[key]
            rendered.apply.has_volume_control = True
            rendered.bridge.refreshMicrophone()
            rendered.bridge.microphoneChanged.emit()
            QTest.qWait(30)
            get_qapplication().processEvents()
            assert_layout(rendered, key)
        rendered.window.hide()
        rendered.window.setWidth(1035 if width == 900 else 900)
        rendered.window.show()
        QTest.qWait(30)
        assert_layout(rendered, prefix + "unknown")


@pytest.mark.parametrize("width", [900, 1035], ids=["minimum", "screenshot"])
@pytest.mark.parametrize("state", list(STATES))
def test_late_settings_bridge_first_show_preserves_microphone_row(width: int, state: str) -> None:
    # Настоящий app.py грузит QML раньше bridge. Проверяем первое show,
    # не давая последующим state changes случайно восстановить сломанную строку.
    with render(width, state, late_bridge=True) as rendered:
        assert_layout(rendered, state)
