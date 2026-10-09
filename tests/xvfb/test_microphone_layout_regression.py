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
from PyQt5.QtCore import QPointF, QRectF, QSizeF, Qt, QUrl, qInstallMessageHandler
from PyQt5.QtQml import QQmlApplicationEngine, QQmlEngine, QQmlExpression
from PyQt5.QtQuick import QQuickItem, QQuickWindow
from PyQt5.QtTest import QTest
from test_onboarding import FakeTheme

from astra_voice.core.settings import Settings
from astra_voice.platform.sound import MicrophoneState
from astra_voice.ui.bridges import SettingsApply, SettingsBridge
from astra_voice.ui.icons import install_icon_provider
from astra_voice.worker.audio import AudioDevice
from helpers.qt_app import get_qapplication

pytestmark = pytest.mark.xvfb
QML = Path(__file__).resolve().parents[2] / "qml"
os.environ["QT_QUICK_CONTROLS_STYLE"] = "Default"

STATES = {
    "unknown": MicrophoneState(),
    "muted": MicrophoneState(known=True, muted=True, percent=60),
    "normal": MicrophoneState(known=True, muted=False, percent=60),
    "low_restore": MicrophoneState(known=True, muted=False, percent=20),
    "muted_restore": MicrophoneState(known=True, muted=True, percent=60),
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
    reads: list[MicrophoneState]
    engine: QQmlApplicationEngine
    saved_changes: list[dict[str, Any]]
    device_changes: list[str | None]


@contextmanager
def render(
    width: int,
    state: str,
    *,
    can_raise: bool = True,
    late_bridge: bool = False,
    microphone: MicrophoneState | None = None,
    can_sound: bool = True,
    dark: bool = False,
    device_name: str = "",
    saved_device: str = "",
) -> Iterator[Rendered]:
    app = get_qapplication()
    messages: list[str] = []

    def handler(_mode: Any, _context: Any, message: str) -> None:
        messages.append(message)

    previous = qInstallMessageHandler(handler)
    # Это только контракт SettingsApply: ни SoundControl, ни системных команд.
    mic_state = [microphone if microphone is not None else STATES[state]]
    reads: list[MicrophoneState] = []
    saved_changes: list[dict[str, Any]] = []
    device_changes: list[str | None] = []

    def microphone_state() -> MicrophoneState:
        reads.append(mic_state[0])
        return mic_state[0]

    apply = SimpleNamespace(
        has_volume_control=can_raise,
        has_sound_settings=can_sound,
        has_sound_service=False,
        can_restore_microphone_volume=state.endswith("_restore"),
        microphone_state=microphone_state,
        device=device_changes.append,
    )
    bridge = SettingsBridge(
        Settings(extra={"device": saved_device} if saved_device else {}),
        apply=cast(SettingsApply, apply),
        locked=["device", "mic_volume_on_start"] if state.startswith("locked") else [],
        device_provider=lambda: (
            [AudioDevice(1, "long-device", device_name, False)] if device_name else []
        ),
        save=lambda settings: saved_changes.append(settings.to_dict()),
    )
    engine = QQmlApplicationEngine()
    install_icon_provider(engine)
    theme = FakeTheme(dark)
    engine.rootContext().setContextProperty("themeSource", theme)
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
        yield Rendered(
            window,
            rows[0],
            bridge,
            messages,
            apply,
            mic_state,
            reads,
            engine,
            saved_changes,
            device_changes,
        )
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
    # All primary controls, including ? and both optional actions, share one axis.
    axis_names = [
        name
        for name in boxes
        if name != "policy"
        and not (name == "status" and not str(items[name].property("text")).endswith(" %"))
    ]
    centers = [boxes[name].center().y() for name in axis_names]
    assert max(centers) - min(centers) <= 1, details
    ordered = sorted((boxes[name] for name in axis_names), key=lambda box: box.left())
    for left, right in zip(ordered, ordered[1:], strict=False):
        assert right.left() - left.right() >= 7.9, details
    assert boxes["selector"].width() >= 120, details
    if "slider" in boxes:
        assert boxes["slider"].width() >= 80, details
    if "status" in boxes and "status" not in axis_names:
        assert boxes["status"].top() >= boxes["selector"].bottom() + 3.9, details
    if "policy" in boxes:
        assert boxes["policy"].top() >= boxes["selector"].bottom() + 3.9, details
    if "Настройки звука…" in boxes:
        assert boxes["Настройки звука…"].right() == pytest.approx(row.width() - 14, abs=0.1)
    for name in ("Поднять", "Вернуть", "Настройки звука…"):
        if name in items:
            assert boxes[name].width() >= float(items[name].property("implicitWidth")), details
    if state.endswith("_restore"):
        assert "Поднять" in items and "Вернуть" in items, details
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
        assert "slider" in items and f"{rendered.bridge.microphoneVolume} %" in texts
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


@pytest.mark.parametrize("width", [900, 1035], ids=["minimum", "screenshot"])
@pytest.mark.parametrize("late_bridge", [False, True], ids=["early", "late"])
def test_first_show_reads_current_microphone_without_prepopulation(
    width: int, late_bridge: bool
) -> None:
    known = MicrophoneState(known=True, muted=False, percent=100)
    with render(width, "normal", late_bridge=late_bridge, microphone=known) as rendered:
        assert rendered.reads == [known], (
            "First show must read current microphone state exactly once"
        )
        assert rendered.bridge.microphoneVolume == 100
        assert not rendered.bridge.microphoneMuted
        assert_layout(rendered, "normal")
        assert foreground(rendered.row)["slider"].property("value") == 100
        rendered.engine.rootContext().setContextProperty("settingsBridge", rendered.bridge)
        QTest.qWait(80)
        get_qapplication().processEvents()
        assert rendered.reads == [known], "Assigning the same bridge must not refresh it again"
        assert_layout(rendered, "normal")


@pytest.mark.parametrize("width", [900, 1035], ids=["minimum", "screenshot"])
def test_replaced_settings_bridge_reads_its_microphone_state(width: int) -> None:
    reads: list[MicrophoneState] = []
    known = MicrophoneState(known=True, muted=False, percent=100)

    def microphone_state() -> MicrophoneState:
        reads.append(known)
        return known

    replacement_apply = SimpleNamespace(
        has_volume_control=True,
        has_sound_settings=True,
        has_sound_service=False,
        can_restore_microphone_volume=False,
        microphone_state=microphone_state,
    )
    replacement = SettingsBridge(
        Settings(),
        apply=cast(SettingsApply, replacement_apply),
        device_provider=lambda: [],
        save=lambda settings: None,
    )
    try:
        with render(width, "unknown") as rendered:
            assert rendered.bridge.microphoneVolume == -1
            rendered.engine.rootContext().setContextProperty("settingsBridge", replacement)
            QTest.qWait(80)
            get_qapplication().processEvents()
            assert reads == [known], (
                "A replacement bridge must read its current microphone state once"
            )
            assert replacement.microphoneVolume == 100
            assert not replacement.microphoneMuted
            rendered.bridge = replacement
            assert_layout(rendered, "normal")
            assert foreground(rendered.row)["slider"].property("value") == 100
    finally:
        sip.delete(replacement)


@pytest.mark.parametrize("width", [900, 1035])
@pytest.mark.parametrize("can_sound", [True, False])
def test_unavailable_volume_keeps_independent_sound_action(width: int, can_sound: bool) -> None:
    with render(width, "unknown", can_raise=False, can_sound=can_sound) as rendered:
        items = foreground(rendered.row)
        assert ("Настройки звука…" in items) is can_sound
        assert "slider" not in items and "status" not in items
        centers = [rect(item, rendered.row).center().y() for item in items.values()]
        assert max(centers) - min(centers) <= 1
        assert not rendered.messages, rendered.messages


@pytest.mark.parametrize("width", [900, 1035])
def test_percentage_reserve_keeps_sound_action_still(width: int) -> None:
    with render(width, "normal") as rendered:
        rendered.apply.can_restore_microphone_volume = True
        positions = []
        for percent in (0, 100):
            rendered.state[0] = MicrophoneState(known=True, muted=False, percent=percent)
            rendered.bridge.refreshMicrophone()
            rendered.bridge.microphoneChanged.emit()
            QTest.qWait(20)
            items = foreground(rendered.row)
            positions.append(rect(items["Настройки звука…"], rendered.row))
            status = items["status"]
            assert status.width() >= float(status.property("implicitWidth"))
        assert positions[0] == positions[1]
        assert not rendered.messages, rendered.messages


@pytest.mark.parametrize("width", [900, 1035])
def test_long_device_popup_stays_inside_microphone_card(width: int) -> None:
    name = "Внешний микрофон конференции с очень длинным названием устройства и канала записи"
    with render(width, "low_restore", device_name=name) as rendered:
        assert_layout(rendered, "low_restore")
        selector = foreground(rendered.row)["selector"]
        # Explicit keyboard selection; no initial settings prepopulation.
        selector.forceActiveFocus(Qt.TabFocusReason)
        QTest.keyClick(rendered.window, Qt.Key_Down)
        QTest.qWait(20)

        def popup_value(source: str) -> Any:
            expression = QQmlExpression(QQmlEngine.contextForObject(selector), selector, source)
            value, _undefined = expression.evaluate()
            assert not expression.hasError(), expression.error().toString()
            return value

        popup_value("popup.open()")
        QTest.qWait(40)
        left = rect(selector, rendered.row).left() + float(popup_value("popup.x"))
        right = left + float(popup_value("popup.width"))
        assert left >= 13.9 and right <= rendered.row.width() - 13.9
        assert selector.property("displayText") == AudioDevice(1, "long-device", name, False).label
        popup_value("popup.close()")
        assert not rendered.messages, rendered.messages


@pytest.mark.parametrize("width", [900, 1035])
@pytest.mark.parametrize("late_bridge", [False, True])
def test_saved_device_first_show_and_programmatic_index_do_not_write(
    width: int, late_bridge: bool
) -> None:
    with render(
        width,
        "normal",
        device_name="Внешний микрофон",
        saved_device="long-device",
        late_bridge=late_bridge,
    ) as rendered:
        selector = foreground(rendered.row)["selector"]
        assert rendered.bridge.device == "long-device"
        assert selector.property("currentIndex") == 1
        assert rendered.saved_changes == []
        assert rendered.device_changes == []
        selector.setProperty("currentIndex", 0)
        selector.setProperty("currentIndex", 1)
        QTest.qWait(20)
        assert rendered.bridge.device == "long-device"
        assert rendered.saved_changes == []
        assert rendered.device_changes == []
        selector.forceActiveFocus(Qt.TabFocusReason)
        QTest.keyClick(rendered.window, Qt.Key_Up)
        QTest.qWait(20)
        assert rendered.bridge.device == ""
        assert rendered.device_changes == [None]
        assert len(rendered.saved_changes) == 1 and rendered.saved_changes[0]["device"] is None
        assert not rendered.messages, rendered.messages
