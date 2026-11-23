"""Offscreen probes of the real W1, isolated from the pytest QApplication."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from ctypes import CDLL, CFUNCTYPE, POINTER, c_bool, c_int, c_uint64, c_ulong, c_void_p, cast
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import Mock

import pytest
from PyQt5 import sip
from PyQt5.QtCore import (
    QCoreApplication,
    QEvent,
    QEventLoop,
    QLibraryInfo,
    QMetaObject,
    QObject,
    QPoint,
    QPointF,
    QRect,
    Qt,
    QTimer,
    QTimerEvent,
    QUrl,
    pyqtProperty,
    pyqtSignal,
)
from PyQt5.QtGui import (
    QCloseEvent,
    QExposeEvent,
    QFontDatabase,
    QFontInfo,
    QKeyEvent,
    QMouseEvent,
    QRegion,
    QTouchDevice,
    QTouchEvent,
    QWheelEvent,
)
from PyQt5.QtQml import QQmlEngine, QQmlExpression
from PyQt5.QtQuick import QQuickItem, QQuickView
from PyQt5.QtTest import QSignalSpy, QTest

from astra_voice.core.meeting_session import ERROR_TEXT
from astra_voice.ui.meeting_offer_window import MeetingOffer, OfferQuickView
from astra_voice.ui.meeting_texts import W1_CALL_TEXTS, W1_TEXTS, W4_TEXTS
from helpers import meeting_offer as offer_points
from helpers.qt_app import get_qapplication

if TYPE_CHECKING:
    from test_meeting_recording_bridge import Rig

if __name__ != "__main__":
    from test_meeting_recording_bridge import rig as rig

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]
CONTROLS = {"start": "offerStart", "refuse": "offerRefuse", "close": "offerClose"}
FAILURE_CONTROLS = {"open_meetings": "offerOpenMeetings", "close": "offerClose"}
SHADOW_MARGIN = 36
CALL_MEASURED_MS = 100_000
CALL_GENERATION = 214_084
CALL_APP_CLASS = "browser"


class ThemeSource(QObject):
    changed = pyqtSignal()
    value = False
    dark = pyqtProperty(bool, lambda self: self.value, notify=changed)


class KeyCounter(QObject):
    presses: list[int]

    def __init__(self) -> None:
        super().__init__()
        self.presses = []

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if isinstance(event, QKeyEvent) and event.type() == QEvent.KeyPress:
            self.presses.append(event.key())
        return False


class DelayedView(OfferQuickView):
    exposed = False

    def isExposed(self) -> bool:
        return self.exposed


def qml(obj: QObject, expression: str) -> Any:
    runner = QQmlExpression(QQmlEngine.contextForObject(obj), obj, expression)
    value, _ = runner.evaluate()
    assert not runner.hasError(), f"{expression}: {runner.error().toString()}"
    return value


def child(window: MeetingOffer, name: str) -> QQuickItem:
    item = window._root.findChild(QQuickItem, name)
    assert item is not None, name
    return item


def button_position(button: QQuickItem) -> QPoint:
    return button.mapToScene(QPointF(button.width() / 2, button.height() / 2)).toPoint()


def mouse_click_at(view: QQuickView, button: QQuickItem, stamp: int) -> None:
    point = QPointF(button_position(button))
    for kind, buttons in (
        (QEvent.MouseButtonPress, Qt.LeftButton),
        (QEvent.MouseButtonRelease, Qt.NoButton),
    ):
        event = QMouseEvent(kind, point, Qt.LeftButton, buttons, Qt.NoModifier)
        event.setTimestamp(stamp)
        QCoreApplication.sendEvent(view, event)


def expose(view: QQuickView) -> None:
    blocked = view.blockSignals(True)
    try:
        QCoreApplication.sendEvent(view, QExposeEvent(QRegion(0, 0, view.width(), view.height())))
    finally:
        view.blockSignals(blocked)


def swap_frame(view: QQuickView) -> None:
    view.frameSwapped.emit()
    QCoreApplication.sendPostedEvents(None, QEvent.MetaCall)


def assert_contents(window: MeetingOffer, kind: str = "call-app") -> None:
    assert window._view.source().toLocalFile() == str(ROOT / "qml/MeetingOffer.qml")
    assert window._view.isVisible() and window._root.property("visible")
    assert window._root.property("kind") == kind
    assert child(window, "offerTitle").property("text") == W1_CALL_TEXTS[kind]["title"]


@contextmanager
def surface(recording: Any, **kwargs: Any) -> Iterator[MeetingOffer]:
    window = MeetingOffer(recording, **kwargs)
    try:
        yield window
    finally:
        window.shutdown()
        if isinstance(recording, QObject):
            recording.offer.bind(None)
        sip.delete(window)
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)


def geometry() -> None:
    """Q2, including the last card row, shadow only, active monitor and HiDPI."""
    for ratio in (1, 2):
        for width in (1600, 1024):
            _geometry_at_scale(width, ratio)


def _geometry_at_scale(width: int, ratio: int) -> None:
    patcher = pytest.MonkeyPatch()
    area = QRect(100, 68, width, 800)
    selected = SimpleNamespace(
        geometry=lambda: QRect(100, 20, width, 848),
        availableGeometry=lambda: area,
        devicePixelRatio=lambda: ratio,
    )
    primary = SimpleNamespace(availableGeometry=lambda: QRect(-900, 0, 1000, 900))
    screen_at = Mock(return_value=selected)
    screens = SimpleNamespace(primaryScreen=lambda: primary, screenAt=screen_at)
    active = SimpleNamespace(
        active_window=lambda: 90, window_geometry=lambda _id: (110, 50, 750, 400), d=None
    )
    card_center = QRect(area.x() + (width - 380) // 2, area.y() + 12, 380, 171)
    card_right = QRect(area.right() - 12 - 379, area.y() + 12, 380, 171)
    last_row = QPoint(card_center.right() - 1, card_center.bottom())
    if width == 1024:
        assert card_center.contains(last_row) and card_right.contains(last_row)
    below = last_row + QPoint(0, 1)
    cursor = Mock(return_value=below)

    class ScaledView(QQuickView):
        def devicePixelRatio(self) -> float:
            return float(ratio)

    now = Mock(return_value=0)
    with surface(
        Mock(offer=Mock(now_ms=now)), cursor_pos=cursor, view_factory=ScaledView
    ) as window:
        with patcher.context() as patch:
            patch.setattr(window, "_screen_application", lambda: screens)
            patch.setattr(window, "_x11", active)

            def actual_card() -> QRect:
                return QRect(
                    window._view.x() + SHADOW_MARGIN,
                    window._view.y() + SHADOW_MARGIN,
                    int(window._root.property("surfaceWidth")),
                    card_center.height(),
                )

            window.show("call-app", 60)
            assert_contents(window)
            screen_at.assert_called_with(QPoint(485, 250))
            assert actual_card() == card_center, "cursor in shadow must not move W1"
            cursor.assert_called_once_with()
            flags = window._view.flags()
            assert flags & Qt.WindowType_Mask == Qt.Tool
            for flag in (
                Qt.FramelessWindowHint,
                Qt.WindowStaysOnTopHint,
                Qt.WindowDoesNotAcceptFocus,
            ):
                assert flags & flag
            cursor.return_value = last_row
            assert card_center.contains(cursor.return_value)
            window.update_remaining(42)
            QCoreApplication.processEvents()
            window._resize()
            assert actual_card() == card_center
            cursor.assert_called_once_with()
            window.close()
            window.show("call-app", 60)
            assert_contents(window)
            assert actual_card() == card_right, "cursor on last card row must move W1"
            assert cursor.call_count == 2
            previous = window._view.geometry()
            cursor.return_value = below
            window.start_failed(ERROR_TEXT["disk-low"])
            QCoreApplication.processEvents()
            now.return_value += 500
            window._failure_input_timer.timeout.emit()
            current = window._view.geometry()
            assert window._at_right and cursor.call_count == 2
            assert current.topLeft() == previous.topLeft()
            assert current.width() == previous.width() and current.height() < previous.height()


def visuals(mode: str) -> None:
    """Q1/Q3/Q5, covering theme source, text, layout, hover and countdown."""
    theme = ThemeSource()
    warnings: list[str] = []

    def factory() -> QQuickView:
        view = QQuickView()
        view.engine().rootContext().setContextProperty("themeSource", theme)
        view.engine().warnings.connect(lambda rows: warnings.extend(row.toString() for row in rows))
        return view

    with surface(
        Mock(offer=Mock(now_ms=lambda: 0)), cursor_pos=QPoint, view_factory=factory
    ) as window:
        window._root.setProperty("freezeAnimations", True)
        if mode == "visual-colors":
            visual_colors(window, theme)
        elif mode == "visual-start-states":
            visual_start_states(window, theme)
        else:
            kind = "call-browser" if mode == "visual-browser" else "call-app"
            visual_layout(window, kind)
        assert not warnings, warnings


def visual_layout(window: MeetingOffer, kind: str) -> None:
    root = window._root
    height = 188.8 if kind == "call-browser" else 170.6
    with pytest.raises(ValueError, match="kind"):
        window.show("other", 60)
    assert not window._view.isVisible()
    window.show(kind, 60)
    assert_contents(window, kind)
    assert root.property("surfaceWidth") == 380
    assert root.property("surfaceHeight") == pytest.approx(height, abs=4)
    assert root.property("contentX") - root.property("borderWidth") == 14
    assert root.property("contentY") - root.property("borderWidth") == 12
    assert root.property("borderWidth") == 1
    assert child(window, "offerSurface").property("surfaceRadius") == 10
    title, body = (child(window, name) for name in ("offerTitle", "offerBody"))
    assert QFontInfo(title.property("font")).family() == "PT Root UI"
    assert body.property("text") == W1_CALL_TEXTS[kind]["body"]
    assert qml(root, "Accessible.role === Accessible.AlertMessage") is True
    assert qml(root, "Accessible.name") == title.property("text")
    assert child(window, "offerHeader").property("x") == 13 + 8
    assert child(window, "offerClose").width() == 18
    start, refuse, actions, counter = (
        child(window, name)
        for name in ("offerStart", "offerRefuse", "offerActions", "offerCounter")
    )
    for action, name in CONTROLS.items():
        button = child(window, name)
        role = "StaticText" if action == "start" else "Button"
        assert qml(button, f"Accessible.role === Accessible.{role}") is True
        assert qml(button, "Accessible.name") == W1_TEXTS[action]
    assert start.height() == refuse.height() == 28
    for block in (
        body.parentItem(),
        child(window, "offerHeader"),
        child(window, "offerHeadGlyph"),
        counter,
        actions,
        start.findChild(QQuickItem, "offerActionLabel").parentItem(),
        refuse.findChild(QQuickItem, "offerActionLabel").parentItem(),
    ):
        y = block.mapToScene(QPointF(0, 0)).y()
        assert y == round(y), (block.objectName(), y)
    for button in (start, refuse):
        label_box = button.findChild(QQuickItem, "offerActionLabel").parentItem()
        assert 2 * label_box.y() + label_box.height() == button.height()
    assert start.property("radius") == 6
    assert refuse.x() - start.width() == 7
    assert actions.y() == round(body.parentItem().y() + body.parentItem().height() + 10)
    assert counter.y() - actions.y() - actions.height() == pytest.approx(7)
    progress = child(window, "offerProgress")
    assert progress.height() == 2
    assert progress.y() + progress.height() + 1 == root.property("surfaceHeight")
    fill = child(window, "offerProgressFill")
    animation = root.findChild(QObject, "offerCountdown")
    assert animation is not None
    assert not animation.property("running")
    assert animation.property("duration") == 60_000
    assert (animation.property("from"), animation.property("to")) == (1, 0)
    source = Path(window._view.source().toLocalFile()).read_text()
    assert re.search(r'NumberAnimation on fraction\s*\{\s*objectName: "offerCountdown"', source)
    assert qml(progress, "Easing.Linear === 0") is True
    assert animation.property("easing").type() == 0
    size, surface_height = window._view.size(), root.property("surfaceHeight")
    remaining = child(window, "offerRemaining")
    progress.setProperty("fraction", 0.25)
    assert fill.width() == pytest.approx(progress.width() * 0.25)
    progress.setProperty("fraction", 0.7)
    for seconds in (60, 42, 21, 10, 9, 1):
        window.update_remaining(seconds)
        QMetaObject.invokeMethod(root, "forceLayout", Qt.DirectConnection)
        assert remaining.property("text") == f"Закроется через {seconds} с"
        assert root.property("surfaceHeight") == surface_height
        assert window._view.size() == size
        assert root.property("seconds") == seconds
        assert fill.width() == pytest.approx(progress.width() * 0.7)
        assert remaining.x() + remaining.implicitWidth() * remaining.scale() == pytest.approx(
            counter.width()
        )
    for item in root.findChildren(QQuickItem):
        if item.metaObject().indexOfProperty("textFormat") >= 0:
            assert qml(item, "textFormat === Text.PlainText") is True
    window.close()

    # Restore the initial value after exercising the frozen overrides.
    progress.setProperty("fraction", 1)
    root.setProperty("freezeAnimations", False)
    window.show("call-app", 60)
    assert_contents(window)
    assert animation.property("running")
    fill_fraction = progress.property("fraction")
    assert fill_fraction == 1 and fill.width() == 378
    window.update_remaining(42)
    assert progress.property("fraction") == fill_fraction
    assert QMetaObject.invokeMethod(animation, "complete", Qt.DirectConnection) is None
    assert progress.property("fraction") == 0 and fill.width() == 0
    window.close()
    assert not animation.property("running")


def visual_colors(window: MeetingOffer, theme: ThemeSource) -> None:
    root = window._root
    window.show("call-app", 60)
    assert_contents(window)
    QCoreApplication.processEvents()
    surface_item = child(window, "offerSurface")
    start = child(window, "offerStart")
    close = child(window, "offerClose")
    refuse = child(window, "offerRefuse")
    color_bindings = [
        (surface_item, "surfaceColor", "notifyBg"),
        (surface_item, "shadowSourceColor", "notifyBg"),
        (surface_item, "borderColor", "notifyBorder"),
        (child(window, "offerHeader"), "color", "notifyFgMuted"),
        (child(window, "offerHeadGlyph"), "color", "notifyFgMuted"),
        (child(window, "offerTitle"), "color", "notifyFg"),
        (child(window, "offerBody"), "color", "pillIconMuted"),
        (child(window, "offerRemaining"), "color", "notifyFgMuted"),
        (child(window, "offerProgressColor"), "color", "notifyBtnPrimaryBg"),
        (start, "color", "notifyBtnPrimaryBg"),
        (refuse, "color", "notifyBtnBg"),
        (close, "color", "notifyBtnBg"),
        (close.findChild(QQuickItem, "offerCloseGlyph"), "color", "pillCloseFg"),
        (start.findChild(QQuickItem, "offerActionLabel"), "color", "notifyBtnPrimaryFg"),
        (start.findChild(QQuickItem, "offerActionGlyph"), "color", "notifyBtnPrimaryFg"),
        (refuse.findChild(QQuickItem, "offerActionLabel"), "color", "notifyFg"),
    ]

    def all_item_colors() -> list[Any]:
        values = []
        for item in root.findChildren(QQuickItem):
            if item.metaObject().indexOfProperty("color") >= 0:
                values.append(item.property("color"))
            if item.metaObject().indexOfProperty("border") >= 0:
                values.append(qml(item, "border.color"))
        return values

    colors, backgrounds = [], []
    for dark in (False, True):
        theme.value = dark
        theme.changed.emit()
        assert qml(root, "Theme.dark") is dark
        backgrounds.append(qml(root, "Theme.bgApp"))
        current = [item.property(prop) for item, prop, _ in color_bindings]
        expected = [qml(root, f"PillTheme.{name}") for _, _, name in color_bindings]
        assert current == expected
        colors.append(all_item_colors())
    assert backgrounds[0] != backgrounds[1] and colors[0] == colors[1]
    for button in (start, refuse, close):
        before = button.property("color"), button.property("labelColor")
        QTest.mouseMove(window._view, button_position(button))
        assert button.property("hovered"), button.objectName()
        assert (button.property("color"), button.property("labelColor")) == before


def visual_start_states(window: MeetingOffer, theme: ThemeSource) -> None:
    """455-1: starting and failure in both themes, including literal error text."""
    window.show("call-app", 42)
    root = window._root
    height = root.property("surfaceHeight")
    title, body = (child(window, name) for name in ("offerTitle", "offerBody"))
    start, refuse, close = (child(window, name) for name in CONTROLS.values())
    counter, progress = (child(window, name) for name in ("offerCounter", "offerProgress"))
    open_meetings = child(window, "offerOpenMeetings")
    spinner = start.findChild(QQuickItem, "offerSpinner")
    rotation = spinner.findChild(QObject, "offerSpinnerRotation")
    start_label = start.findChild(QQuickItem, "offerActionLabel")
    start_width, refuse_x = start.width(), refuse.x()
    fixed_items = [child(window, "offerHead"), title, body, counter, progress]
    positions = [item.mapToScene(QPointF()) for item in fixed_items]

    window.starting()
    for dark in (False, True):
        theme.value = dark
        theme.changed.emit()
        QCoreApplication.processEvents()
        assert qml(root, "Theme.dark") is dark
        assert start_label.isVisible() and start_label.property("text") == W4_TEXTS["starting"]
        assert qml(start, "Accessible.name") == W4_TEXTS["starting"]
        assert spinner.isVisible() and spinner.width() == spinner.height() == 12
        assert spinner.property("ink") == start_label.property("color")
        assert not rotation.property("running")
        assert not start.findChild(QQuickItem, "offerActionGlyph").isVisible()
        assert start.width() < start_width and refuse.x() < refuse_x
        assert all(item.isVisible() for item in [*fixed_items, refuse, close])
        assert [item.mapToScene(QPointF()) for item in fixed_items] == positions
        assert root.property("surfaceHeight") == height
        assert not open_meetings.isVisible()

    error = ERROR_TEXT["disk-low"]
    window.start_failed(error)
    for dark in (False, True):
        theme.value = dark
        theme.changed.emit()
        QCoreApplication.processEvents()
        assert qml(root, "Theme.dark") is dark
        assert all(not item.isVisible() for item in (title, start, refuse, counter, progress))
        assert body.isVisible() and body.property("text") == error
        assert qml(root, "Accessible.name") == error
        assert qml(body, "wrapMode === Text.WordWrap")
        assert body.property("color") == qml(root, "PillTheme.notifyFg")
        assert body.property("size") == 12.5
        assert body.property("lineHeight") * body.scale() == pytest.approx(12.5 * 1.45)
        assert open_meetings.isVisible() and close.isVisible()
        assert qml(open_meetings, "Accessible.name") == W4_TEXTS["openMeetings"]
        assert open_meetings.property("color") == refuse.property("color")
        assert not open_meetings.property("hasActionGlyph")
        assert root.property("surfaceHeight") < height

    window.start_failed("<b>x</b>")
    QCoreApplication.processEvents()
    assert body.isVisible() and body.property("text") == "<b>x</b>"
    assert qml(body, "textFormat === Text.PlainText")
    literal_width = body.implicitWidth()
    window.start_failed("x")
    QMetaObject.invokeMethod(root, "forceLayout", Qt.DirectConnection)
    assert literal_width >= 4 * body.implicitWidth()
    window.close()
    assert not window._view.isVisible()


def _check_private_id(window: MeetingOffer) -> None:
    private_numbers = {CALL_MEASURED_MS, CALL_GENERATION}
    private_strings = ("call-PRIVATE-1", "disk-low", *(str(number) for number in private_numbers))

    def check(value: Any, field: str) -> None:
        if hasattr(value, "toVariant"):
            value = value.toVariant()
        if isinstance(value, QUrl):
            check(value.toString(), field)
            check(value.fragment(QUrl.FullyDecoded), field)
        elif isinstance(value, dict):
            for name, entry in value.items():
                check(name, field)
                check(entry, f"{field}.{name}")
        elif isinstance(value, (list, tuple)):
            for entry in value:
                check(entry, field)
        elif isinstance(value, (int, float)):
            assert value not in private_numbers, field
        else:
            assert all(private not in repr(value) for private in private_strings), field

    root = window._root
    kind = root.property("kind")
    starting = root.property("starting")
    window_name = W1_CALL_TEXTS[kind]["title"]
    if starting:
        window_name = W4_TEXTS["starting"]
    if root.property("startFailed"):
        window_name = ERROR_TEXT["disk-low"]
    accessible_names = {
        "meetingOffer": window_name,
        "offerClose": W1_TEXTS["close"],
        "offerStart": W4_TEXTS["starting"] if starting else W1_TEXTS["start"],
        "offerRefuse": W1_TEXTS["refuse"],
        "offerOpenMeetings": W4_TEXTS["openMeetings"],
    }
    for item in [root, *root.findChildren(QQuickItem)]:
        name = item.objectName()
        assert qml(item, "Accessible.name") == accessible_names.get(name, ""), (name, "name")
        assert qml(item, "Accessible.description") == "", (name, "description")
    for item in [root, *root.findChildren(QObject)]:
        meta = item.metaObject()
        for index in range(meta.propertyCount()):
            prop = meta.property(index)
            if prop.isReadable() and prop.typeName() in {
                "QString",
                "QStringList",
                "QByteArray",
                "QVariant",
                "QVariantMap",
                "QVariantList",
                "QVariantHash",
                "QJSValue",
                "QUrl",
                "int",
                "uint",
                "long",
                "ulong",
                "qlonglong",
                "qulonglong",
                "double",
                "float",
            }:
                check(prop.read(item), prop.name())
        for name in item.dynamicPropertyNames():
            key = bytes(name).decode()
            check(item.property(key), key)
    texts = root.property("texts").toVariant()
    assert set(texts) == W1_TEXTS.keys() | W1_CALL_TEXTS[kind].keys() | {"starting", "openMeetings"}


def _privacy_rig(directory: Path) -> Rig:
    from test_meeting_recording_bridge import Rig, Supervisor

    class PrivacySupervisor(Supervisor):
        generation = CALL_GENERATION

    return Rig(directory, role_type=PrivacySupervisor)


def starting_input(directory: Path) -> None:
    instance = _privacy_rig(directory)
    recording = instance.recording
    patcher = pytest.MonkeyPatch()
    try:
        with surface(recording) as window, patcher.context() as patch:
            press = Mock(return_value=False)
            patch.setattr(recording, "press_offer", press)
            start_observing_with_view(instance, window, app_class=CALL_APP_CLASS)
            assert recording._detector_generation == CALL_GENERATION
            instance.now = 100.6
            view, root = window._view, window._root
            start = child(window, "offerStart")
            QTest.mouseClick(view, Qt.LeftButton, pos=button_position(start))
            press.assert_called_once_with("start", 100_600)
            press.reset_mock()

            progress = child(window, "offerProgress")
            countdown = root.findChild(QObject, "offerCountdown")
            assert countdown.property("running") and not countdown.property("paused")
            window.update_remaining(42)
            remaining = child(window, "offerRemaining").property("text")
            height, geometry = root.property("surfaceHeight"), view.geometry()
            if progress.property("fraction") == 1:
                loop = QEventLoop()
                timer = QTimer()
                timer.setSingleShot(True)
                timer.timeout.connect(loop.quit)
                progress.fractionChanged.connect(loop.quit)
                timer.start(2000)
                loop.exec()
                progress.fractionChanged.disconnect(loop.quit)
            fraction = progress.property("fraction")
            assert 0 < fraction < 1
            window.starting()
            assert not (countdown.property("running") and not countdown.property("paused"))
            assert start.findChild(QObject, "offerSpinnerRotation").property("running")
            assert progress.property("fraction") == pytest.approx(fraction, abs=1e-6)
            QCoreApplication.processEvents()
            assert progress.property("fraction") == pytest.approx(fraction, abs=1e-6)
            assert root.property("surfaceHeight") == height and view.geometry() == geometry
            window.update_remaining(10)
            assert child(window, "offerRemaining").property("text") == remaining
            assert root.property("seconds") == 42
            _check_private_id(window)

            keys = [Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space]
            counter = KeyCounter()
            view.installEventFilter(counter)
            for name in CONTROLS.values():
                button = child(window, name)
                QTest.mouseClick(view, Qt.LeftButton, pos=button_position(button))
                qml(button, "Accessible.pressAction()")
            for key in keys:
                QTest.keyClick(view, key)
            assert counter.presses == keys
            press.assert_not_called()

            window.close()
            assert not view.isVisible()
            instance.now = 102.0
            window.show("call-app", 60)
            assert countdown.property("running") and not countdown.property("paused")
            assert progress.property("fraction") == 1
            assert start.property("label") == W1_TEXTS["start"]
            QCoreApplication.processEvents()
            if window._waiting_for_expose:
                expose(view)
            swap_frame(view)
            assert window._first_frame_at_ms == 102_000
            instance.now = 102.499
            QTest.mouseClick(view, Qt.LeftButton, pos=button_position(start))
            press.assert_not_called()
            instance.now = 102.5
            QTest.mouseClick(view, Qt.LeftButton, pos=button_position(start))
            press.assert_called_once_with("start", 102_500)
    finally:
        instance.bridge.shutdown()


def start_failed_input(directory: Path, mode: str) -> None:
    instance = _privacy_rig(directory)
    recording = instance.recording
    patcher = pytest.MonkeyPatch()
    try:
        with surface(recording) as window, patcher.context() as patch:
            press = Mock(return_value=False)
            patch.setattr(recording, "press_offer", press)
            start_observing_with_view(instance, window, app_class=CALL_APP_CLASS)
            assert recording._detector_generation == CALL_GENERATION
            view = window._view
            instance.now = 100.6
            window.starting()
            _check_private_id(window)
            old_points = [
                button_position(child(window, CONTROLS[action])) for action in ("start", "refuse")
            ]
            instance.now = 101.0
            window.start_failed(ERROR_TEXT["disk-low"])
            QCoreApplication.processEvents()
            _check_private_id(window)
            seconds = window._root.property("seconds")
            remaining = window._root.property("remainingText")
            window.update_remaining(10)
            assert window._root.property("seconds") == seconds
            assert window._root.property("remainingText") == remaining
            buttons = [child(window, name) for name in FAILURE_CONTROLS.values()]

            def press_buttons() -> None:
                for button in buttons:
                    if mode == "mouse":
                        QTest.mouseClick(view, Qt.LeftButton, pos=button_position(button))
                    else:
                        qml(button, "Accessible.pressAction()")

            for before_guard in (101.2, 101.499):
                instance.now = before_guard
                press_buttons()
                press.assert_not_called()
            instance.now = 101.5
            press_buttons()
            assert press.call_args_list == [
                (("open_meetings", 101_500), {}),
                (("close", 101_500), {}),
            ]
            assert view.isVisible(), "the window leaves closing to the controller"
            press.reset_mock()
            for action in ("start", "refuse"):
                qml(child(window, CONTROLS[action]), "Accessible.pressAction()")
            press.assert_not_called()
            for point in old_points:
                QTest.mouseClick(view, Qt.LeftButton, pos=point)
            assert all(call.args[0] == "open_meetings" for call in press.call_args_list)
    finally:
        instance.bridge.shutdown()


def show_after_failure(directory: Path) -> None:
    from test_meeting_recording_bridge import Rig

    instance = Rig(directory)
    recording = instance.recording
    patcher = pytest.MonkeyPatch()
    try:
        with surface(recording, view_factory=DelayedView) as window, patcher.context() as patch:
            press = Mock(return_value=False)
            patch.setattr(recording, "press_offer", press)
            view = window._view
            view.exposed = True
            start_observing_with_view(instance, window)
            window.start_failed(ERROR_TEXT["disk-low"])
            visibility: list[bool] = []
            view.visibleChanged.connect(visibility.append)

            instance.now = 101.0
            view.exposed = False
            window.show("call-app", 60)
            assert visibility == [False, True]
            assert window._waiting_for_expose and window._first_frame_at_ms is None
            for name in (*CONTROLS.values(), "offerCounter", "offerProgress"):
                assert child(window, name).isVisible(), name
            assert not child(window, "offerOpenMeetings").isVisible()
            assert child(window, "offerRemaining").property("text") == "Закроется через 60 с"
            assert_contents(window)
            start = child(window, "offerStart")
            instance.now = 102.0
            qml(start, "Accessible.pressAction()")
            press.assert_not_called()
            view.exposed = True
            expose(view)
            assert not window._waiting_for_expose and window._first_frame_at_ms is None
            swap_frame(view)
            assert window._first_frame_at_ms == 102_000

            for clock in (102.499, 102.5):
                instance.now = clock
                QTest.mouseClick(view, Qt.LeftButton, pos=button_position(start))
                qml(start, "Accessible.pressAction()")
                if clock == 102.499:
                    press.assert_not_called()
            press.assert_called_once_with("start", 102_500)
    finally:
        instance.bridge.shutdown()


def _platform_mouse_dispatch() -> Any:
    # QTest.mouseClick(QWindow) omits Qt's flagged press preceding a double click.
    library = CDLL(str(Path(QLibraryInfo.location(QLibraryInfo.LibrariesPath)) / "libQt5Gui.so.5"))
    dispatch = library[
        "_ZN22QWindowSystemInterface16handleMouseEventINS_19SynchronousDeliveryEEEb"
        "P7QWindowmRK7QPointFS6_6QFlagsIN2Qt11MouseButtonEES7_INS8_16KeyboardModifierEENS8_16MouseEventSourceE"
    ]
    dispatch.argtypes = [c_void_p, c_ulong, c_void_p, c_void_p, c_int, c_int, c_int]
    dispatch.restype = c_bool
    return dispatch


def _platform_click(dispatch: Any, view: QQuickView, point: QPointF, ms: int) -> None:
    global_point = QPointF(view.mapToGlobal(point.toPoint()))
    for stamp, buttons in ((ms, Qt.LeftButton), (ms + 10, Qt.NoButton)):
        dispatch(
            sip.unwrapinstance(view),
            stamp,
            sip.unwrapinstance(point),
            sip.unwrapinstance(global_point),
            int(buttons),
            0,
            0,
        )


def double_click() -> None:
    dispatch = _platform_mouse_dispatch()
    app = get_qapplication()
    now = Mock(return_value=100_000)
    recording = Mock(offer=Mock(now_ms=now))
    patcher = pytest.MonkeyPatch()
    with surface(recording) as window, patcher.context() as patch:
        view = window._view
        events: list[tuple[int, bool]] = []

        class MouseEvents(QObject):
            def eventFilter(self, watched: QObject, event: QEvent) -> bool:
                if isinstance(event, QMouseEvent):
                    events.append(
                        (event.type(), bool(event.flags() & Qt.MouseEventCreatedDoubleClick))
                    )
                return False

        observer = MouseEvents()
        view.installEventFilter(observer)
        window._root.setProperty("freezeAnimations", True)

        def click(point: QPointF, ms: int) -> None:
            now.return_value = ms
            _platform_click(dispatch, view, point, ms)

        def fail_after_start(action: str, _ms: int) -> None:
            if action == "start":
                window.starting()
                window.start_failed(ERROR_TEXT["disk-low"])

        def show_at(ms: int) -> None:
            now.return_value = ms
            window.show("call-app", 60)
            QCoreApplication.processEvents()
            swap_frame(view)
            assert window._first_frame_at_ms == ms

        actions = Mock(side_effect=fail_after_start)
        patch.setattr(recording, "press_offer", actions)
        previous_interval = app.doubleClickInterval()
        app.setDoubleClickInterval(800)
        try:
            assert app.doubleClickInterval() == 800
            point = QPointF(100, 144)
            show_at(100_000)
            start = child(window, "offerStart")
            assert start.contains(start.mapFromScene(point))
            click(point, 100_600)
            actions.assert_called_once_with("start", 100_600)
            meetings = child(window, "offerOpenMeetings")
            assert meetings.contains(meetings.mapFromScene(point))
            events.clear()
            click(point, 101_151)
            assert (QEvent.MouseButtonPress, True) in events
            assert any(kind == QEvent.MouseButtonDblClick for kind, _ in events)
            actions.assert_called_once_with("start", 100_600)

            actions.reset_mock()
            show_at(103_000)
            click(point, 103_600)
            actions.assert_called_once_with("start", 103_600)
            events.clear()
            click(QPointF(button_position(meetings)), 104_100)
            assert (QEvent.MouseButtonPress, False) in events
            assert actions.call_args_list == [
                (("start", 103_600), {}),
                (("open_meetings", 104_100), {}),
            ]
        finally:
            app.setDoubleClickInterval(previous_interval)


def focus_click_spacing() -> None:
    app = get_qapplication()
    dispatch = _platform_mouse_dispatch()
    now = Mock(return_value=100_000)
    recording = Mock(offer=Mock(now_ms=now))
    with surface(recording) as window, pytest.MonkeyPatch.context() as patch:
        view = window._view
        window._root.setProperty("freezeAnimations", True)
        window.show("call-app", 60)
        QCoreApplication.processEvents()
        swap_frame(view)
        assert window._first_frame_at_ms == 100_000
        first, second = offer_points.start_click_points(
            child(window, "offerStart"), app.styleHints()
        )
        patch.setattr(window, "_xcb", True)
        patch.setattr(window, "_frame_x_time", 100_000)
        previous_interval = app.doubleClickInterval()
        app.setDoubleClickInterval(800)
        try:
            now.return_value = 100_100
            _platform_click(dispatch, view, QPointF(first), 100_100)
            recording.press_offer.assert_not_called()

            now.return_value = 100_600
            patch.setattr(window, "_frame_x_time", 99_500)
            _platform_click(dispatch, view, QPointF(second), 100_160)
            recording.press_offer.assert_called_once_with("start", 100_600)
        finally:
            app.setDoubleClickInterval(previous_interval)


def failure_input_region() -> None:
    now = Mock(return_value=100_000)
    recording = Mock(offer=Mock(now_ms=now))
    with surface(recording) as window:
        window._root.setProperty("freezeAnimations", True)
        window.show("call-browser", 60)
        view = window._view
        QCoreApplication.processEvents()
        swap_frame(view)
        old = window._input_region
        geometry = view.geometry()
        point = button_position(child(window, "offerStart"))
        assert old.contains(point)
        now.return_value = 100_600
        window.starting()
        window.start_failed(ERROR_TEXT["disk-low"])
        QCoreApplication.sendPostedEvents(None, QEvent.MetaCall)
        width, height = window._surface_size()
        new = QRegion(SHADOW_MARGIN, SHADOW_MARGIN, width, height)
        assert not new.contains(point)
        assert window._input_region == old and view.geometry() == geometry
        timer = window._failure_input_timer
        assert timer.isActive() and timer.interval() == 500

        now.return_value = 101_099
        QCoreApplication.sendEvent(timer, QTimerEvent(timer.timerId()))
        assert timer.isActive() and timer.interval() == 1
        assert window._input_region.contains(point)
        QTest.mouseClick(view, Qt.LeftButton, pos=point)
        recording.press_offer.assert_not_called()
        now.return_value = 101_100
        QCoreApplication.sendEvent(timer, QTimerEvent(timer.timerId()))
        assert not timer.isActive()
        assert window._input_region == new and not window._input_region.contains(point)
        assert view.position() == geometry.topLeft() and view.height() < geometry.height()

        meetings = child(window, "offerOpenMeetings")
        QTest.mouseClick(view, Qt.LeftButton, pos=button_position(meetings))
        recording.press_offer.assert_called_once_with("open_meetings", 101_100)

        window.show("call-app", 60)
        window.start_failed("\n".join(["Строка ошибки."] * 10))
        QCoreApplication.sendPostedEvents(None, QEvent.MetaCall)
        assert window._input_region.boundingRect().height() == window._surface_size()[1] > height
        assert not timer.isActive()

        for finish in (window.close, window.shutdown):
            window.show("call-browser", 60)
            window.start_failed(ERROR_TEXT["disk-low"])
            assert timer.isActive()
            finish()
            assert not timer.isActive(), finish.__name__


def native_accessibility() -> None:
    library = CDLL(str(Path(QLibraryInfo.location(QLibraryInfo.LibrariesPath)) / "libQt5Gui.so.5"))
    query = library["_ZN11QAccessible24queryAccessibleInterfaceEP7QObject"]
    query.argtypes = [c_void_p]
    query.restype = c_void_p

    def vtable(interface: int) -> Any:
        assert interface
        return cast(interface, POINTER(POINTER(c_void_p))).contents

    def disabled(control: QObject) -> bool:
        interface = query(sip.unwrapinstance(control))
        # Linux Qt 5 C++ ABI: role() and state() follow rect() in the vtable.
        table = vtable(interface)
        assert CFUNCTYPE(c_int, c_void_p)(table[15])(interface) == 43  # QAccessible::Button
        state = CFUNCTYPE(c_uint64, c_void_p)(table[16])(interface)
        return bool(state & 1)  # QAccessible::State::disabled

    def accessible_buttons(interface: int) -> set[int]:
        table = vtable(interface)
        role = CFUNCTYPE(c_int, c_void_p)(table[15])(interface)
        buttons = {interface} if role == 43 else set()
        child_at = CFUNCTYPE(c_void_p, c_void_p, c_int)(table[9])
        count = CFUNCTYPE(c_int, c_void_p)(table[10])(interface)
        for index in range(count):
            descendant = child_at(interface, index)
            if descendant:
                buttons.update(accessible_buttons(descendant))
        return buttons

    with surface(Mock(offer=Mock(now_ms=lambda: 100_000))) as window:
        window.show("call-app", 60)
        controls = [child(window, name) for name in CONTROLS.values()]
        start = child(window, "offerStart")
        root = window._root
        interface = query(sip.unwrapinstance(root))

        def assert_offered() -> None:
            buttons = [c for c in controls if c is not start]
            assert all(c.isEnabled() for c in controls)
            assert not any(map(disabled, buttons))
            assert accessible_buttons(interface) == {query(sip.unwrapinstance(c)) for c in buttons}
            assert qml(start, "Accessible.name") == W1_TEXTS["start"]
            assert qml(root, "Accessible.name") == W1_CALL_TEXTS["call-app"]["title"]

        assert_offered()
        window.starting()
        assert all(not control.isEnabled() for control in controls)
        assert not accessible_buttons(interface)
        assert qml(root, "Accessible.name") == W4_TEXTS["starting"]
        window.close()
        window.show("call-app", 60)
        assert_offered()

        window.start_failed(ERROR_TEXT["disk-low"])
        visible = [child(window, name) for name in FAILURE_CONTROLS.values()]
        assert not any(map(disabled, visible))
        assert accessible_buttons(interface) == {query(sip.unwrapinstance(c)) for c in visible}
        assert qml(root, "Accessible.name") == ERROR_TEXT["disk-low"]
        window.close()
        window.show("call-app", 60)
        assert_offered()


def wm_close_states() -> None:
    recording = Mock(offer=Mock(now_ms=lambda: 100_000))
    with surface(recording) as window:
        for state in ("offered", "starting", "start-failed"):
            window.show("call-app", 60)
            if state == "starting":
                window.starting()
            elif state == "start-failed":
                window.start_failed(ERROR_TEXT["disk-low"])
            recording.dismiss_offer.reset_mock()
            event = QCloseEvent()
            assert QCoreApplication.sendEvent(window._view, event)
            assert not event.isAccepted() and window._view.isVisible(), state
            recording.dismiss_offer.assert_called_once_with()


def input_at(directory: Path, mode: str, elapsed: int) -> None:
    """Q4, including intent through the real MeetingRecording adapter."""
    from test_meeting_m8_offer import assert_no_capture, started_from_w1
    from test_meeting_recording_bridge import Rig

    patcher = pytest.MonkeyPatch()
    instance = Rig(directory)
    recording = instance.recording
    try:
        with surface(recording) as window, patcher.context() as patch:
            press = Mock(wraps=recording.press_offer)
            intent = Mock(wraps=recording.offer.startRequested)
            patch.setattr(recording, "press_offer", press)
            patch.setattr(recording.offer, "startRequested", intent)
            start_observing_with_view(instance, window)
            instance.now = (100_000 + elapsed) / 1000
            assert recording.offer.now_ms() == 100_000 + elapsed
            root_presses = QSignalSpy(window._root.pressed)
            actions = ("refuse", "close", "start") if elapsed == 100 else ("start",)
            for action in actions:
                button = child(window, CONTROLS[action])
                if mode == "accessible":
                    qml(button, "Accessible.pressAction()")
                    if action == "start":
                        assert all(entry[0] != "start" for entry in root_presses)
                else:
                    QTest.mousePress(window._view, Qt.LeftButton, pos=button_position(button))
                if elapsed < 500 or mode == "accessible":
                    press.assert_not_called()
                else:
                    press.assert_called_once_with(action, 100_000 + elapsed)
                press.reset_mock()
                if mode == "mouse":
                    # A delayed release cannot turn a rejected press into consent.
                    if elapsed == 100 and action == "start":
                        instance.now = 100.6
                    QTest.mouseRelease(window._view, Qt.LeftButton, pos=button_position(button))
                    press.assert_not_called()
                assert window._view.isVisible(), "apply the press on the next tick"
            recording.tick()
            if elapsed >= 500 and mode == "accessible":
                # A direct signal also cannot enter the Python start path.
                window._root.pressed.emit("start", False)
                press.assert_not_called()
                recording.tick()
            if elapsed < 500 or mode == "accessible":
                intent.assert_not_called()
                assert not instance.bridge.startDialogOpen and window._view.isVisible()
                assert_no_capture(instance)
            else:
                intent.assert_called_once_with("call-PRIVATE-1", "dion")
                assert window._view.isVisible() and window._root.property("starting")
                recording.tick()
                intent.assert_called_once_with("call-PRIVATE-1", "dion")
                started_from_w1(instance, "call-PRIVATE-1")
                assert not any(
                    msg["type"] == "meeting.capture.open" for msg in instance.supervisor.messages
                )
    finally:
        instance.bridge.shutdown()


def delayed_expose(directory: Path, mode: str) -> None:
    from test_meeting_m8_offer import assert_no_capture, started_from_w1
    from test_meeting_recording_bridge import Rig

    instance = Rig(directory)
    recording = instance.recording
    try:
        with (
            surface(recording, view_factory=DelayedView) as window,
            pytest.MonkeyPatch.context() as patch,
        ):
            press = Mock(wraps=recording.press_offer)
            intent = Mock(wraps=recording.offer.startRequested)
            patch.setattr(recording, "press_offer", press)
            patch.setattr(recording.offer, "startRequested", intent)
            patch.setattr(window, "_xcb", True)
            sample = Mock(side_effect=lambda: recording.offer.now_ms() - 50_000)
            patch.setattr(window, "_sample_x_time", sample)
            start_observing_with_view(instance, window, await_frame=False)
            view = window._view
            button = child(window, CONTROLS["start"])

            def click() -> None:
                if mode == "accessible":
                    qml(button, "Accessible.pressAction()")
                else:
                    mouse_click_at(view, button, recording.offer.now_ms() - 50_000)
                recording.tick()

            assert not view.isExposed()
            click()
            press.assert_not_called()
            view.exposed = True
            assert recording.offer.now_ms() == 100_000
            expose(view)
            instance.now = 100.65
            click()  # Exposed for 650 ms, but no first frame yet.
            press.assert_not_called()
            sample.assert_not_called()
            swap_frame(view)
            sample.assert_called_once_with()
            assert window._first_frame_at_ms == 100_650
            assert window._frame_x_time == 50_650
            for moment in (100.65, 101.149):
                instance.now = moment
                click()
                press.assert_not_called()
                intent.assert_not_called()
                assert_no_capture(instance)
            instance.now = 101.15
            click()
            if mode == "accessible":
                press.assert_not_called()
                intent.assert_not_called()
                assert_no_capture(instance)
                return
            press.assert_called_once_with("start", 101_150)
            intent.assert_called_once_with("call-PRIVATE-1", "dion")
            assert view.isVisible() and window._root.property("starting")
            started_from_w1(instance, "call-PRIVATE-1")
    finally:
        instance.bridge.shutdown()


def x_timestamp(directory: Path) -> None:
    """Real Qt/QML mouse dispatch with independently controlled GUI and X timestamps."""
    from test_meeting_m8_offer import assert_no_capture, started_from_w1
    from test_meeting_recording_bridge import Rig

    instance = Rig(directory)
    recording = instance.recording
    wrap = 1 << 32
    exposed = wrap - 250
    try:
        with surface(recording) as window, pytest.MonkeyPatch.context() as patch:
            press = Mock(wraps=recording.press_offer)
            patch.setattr(recording, "press_offer", press)
            patch.setattr(window, "_xcb", True)
            patch.setattr(window, "_sample_x_time", lambda: exposed)
            start_observing_with_view(instance, window)
            assert window._frame_x_time == exposed
            instance.now = 100.6
            button = child(window, CONTROLS["start"])

            def click(stamp: int) -> None:
                mouse_click_at(window._view, button, stamp)
                recording.tick()
                assert window._view.press_timestamp is None

            # Delayed from +100, before show, missing timestamp, and just below 500.
            for stamp in ((exposed + 100) % wrap, exposed - 10, 0, (exposed + 499) % wrap):
                click(stamp)
                press.assert_not_called()
                assert not instance.bridge.startDialogOpen
                assert_no_capture(instance)
            click((exposed + 500) % wrap)
            press.assert_called_once_with("start", 100_600)
            assert window._view.isVisible() and window._root.property("starting")
            started_from_w1(instance, "call-PRIVATE-1")
    finally:
        instance.bridge.shutdown()


def x_retry(directory: Path) -> None:
    from test_meeting_m8_offer import started_from_w1
    from test_meeting_recording_bridge import Rig

    instance = Rig(directory)
    recording = instance.recording
    try:
        with (
            surface(recording, view_factory=DelayedView) as window,
            pytest.MonkeyPatch.context() as patch,
        ):
            sample = Mock(side_effect=[None, 100_001])
            press = Mock(wraps=recording.press_offer)
            patch.setattr(window, "_xcb", True)
            patch.setattr(window, "_sample_x_time", sample)
            patch.setattr(recording, "press_offer", press)
            start_observing_with_view(instance, window, await_frame=False)
            view = window._view
            view.exposed = True
            expose(view)
            swap_frame(view)
            sample.assert_called_once_with()
            assert window._frame_x_time is None
            instance.now = 100.6
            button = child(window, CONTROLS["start"])
            mouse_click_at(view, button, 100_600)
            press.assert_not_called()
            qml(button, "Accessible.pressAction()")
            press.assert_not_called()
            swap_frame(view)
            assert sample.call_count == 2
            assert window._frame_x_time == 100_001
            assert window._first_frame_at_ms == 100_000
            swap_frame(view)
            assert sample.call_count == 2
            instance.now = 101.2
            mouse_click_at(view, button, 100_500)
            press.assert_not_called()
            mouse_click_at(view, button, 101_200)
            press.assert_called_once_with("start", 101_200)
            recording.tick()
            assert view.isVisible() and window._root.property("starting")
            started_from_w1(instance, "call-PRIVATE-1")
    finally:
        instance.bridge.shutdown()


@contextmanager
def touch_screen() -> Iterator[QTouchDevice]:
    # PyQt5 exposes QtTest's touch sequence but omits its device registration helper.
    library = CDLL(str(Path(QLibraryInfo.location(QLibraryInfo.LibrariesPath)) / "libQt5Gui.so.5"))
    register = library._ZN22QWindowSystemInterface19registerTouchDeviceEPK12QTouchDevice
    unregister = library._ZN22QWindowSystemInterface21unregisterTouchDeviceEPK12QTouchDevice
    for function in (register, unregister):
        function.argtypes = [c_void_p]
        function.restype = None
    device = QTouchDevice()
    device.setType(QTouchDevice.TouchScreen)
    device.setCapabilities(QTouchDevice.Position)
    address = sip.unwrapinstance(device)
    register(address)
    try:
        assert device in QTouchDevice.devices()
        yield device
    finally:
        unregister(address)


def x_touch() -> None:
    class TouchTimestamp(QObject):
        stamp = 0
        seen: list[int]

        def __init__(self) -> None:
            super().__init__()
            self.seen = []

        def eventFilter(self, watched: QObject, event: QEvent) -> bool:
            if isinstance(event, QTouchEvent) and event.type() == QEvent.TouchBegin:
                event.setTimestamp(self.stamp)
                self.seen.append(event.timestamp())
            return False

    now = 100_000
    recording = Mock(offer=Mock(now_ms=lambda: now))
    press = recording.press_offer
    with (
        touch_screen() as device,
        surface(recording, view_factory=DelayedView) as window,
        pytest.MonkeyPatch.context() as patch,
    ):
        patch.setattr(window, "_xcb", True)
        patch.setattr(window, "_sample_x_time", lambda: 100_000)
        window._root.setProperty("freezeAnimations", True)
        window.show("call-app", 60)
        assert_contents(window)
        QCoreApplication.processEvents()
        view = window._view
        view.exposed = True
        expose(view)
        point = button_position(child(window, CONTROLS["start"]))
        touch_clock = TouchTimestamp()
        view.installEventFilter(touch_clock)

        def touch(stamp: int, *, accepted: bool = False) -> None:
            touch_clock.stamp = stamp
            QTest.touchEvent(view, device).press(0, point, view).commit()
            if accepted:
                press.assert_called_once_with("start", now)
            else:
                press.assert_not_called()
            assert view.press_timestamp is None
            QTest.touchEvent(view, device).release(0, point, view).commit()

        now = 100_650
        view.blockSignals(True)
        try:
            touch(100_650)
        finally:
            view.blockSignals(False)
        assert window._first_frame_at_ms is None
        swap_frame(view)
        now = 101_650
        touch(100_100)
        touch(101_000, accepted=True)
        press.assert_called_once_with("start", 101_650)
        assert touch_clock.seen == [100_650, 100_100, 101_000]


def native_dispatch() -> None:
    properties: list[bool] = []

    class ObservedView(DelayedView):
        def show(self) -> None:
            properties.append(self.property("_q_showWithoutActivating"))
            super().show()

    restores = Mock()
    recording = Mock(offer=Mock(now_ms=lambda: 100_000))
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(MeetingOffer, "_restore_user_time", restores)
        with surface(recording, view_factory=ObservedView) as window:
            view = window._view
            for _ in range(2):
                # Verify the property at the exact show call on both presentations.
                view.setProperty("_q_showWithoutActivating", False)
                window.show("call-app", 60)
                assert_contents(window)
                assert properties[-1] is True
                view.exposed = True
                expose(view)
                view.frameSwapped.emit()  # Leave this presentation's notification queued.
                window.close()
                view.exposed = False
            assert properties == [True, True]
            window.show("call-app", 60)
            assert_contents(window)
            view.exposed = True
            expose(view)
            QCoreApplication.sendPostedEvents(None, QEvent.MetaCall)
            assert window._first_frame_at_ms is None
            swap_frame(view)
            assert window._first_frame_at_ms == 100_000
            point = button_position(child(window, CONTROLS["start"]))
            wheel = QWheelEvent(
                QPointF(point),
                QPointF(view.mapToGlobal(point)),
                QPoint(),
                QPoint(0, 120),
                Qt.NoButton,
                Qt.NoModifier,
                Qt.NoScrollPhase,
                False,
            )
            restores.reset_mock()
            QTest.mouseMove(view, point)
            restores.assert_not_called()
            QCoreApplication.sendEvent(view, wheel)
            restores.assert_called_once_with()
            assert view.press_timestamp is None


def keys_and_close(directory: Path) -> None:
    from test_meeting_m8_offer import ended, show_offer
    from test_meeting_recording_bridge import Rig

    patcher = pytest.MonkeyPatch()
    instance = Rig(directory)
    recording = instance.recording
    try:
        with surface(recording) as window, patcher.context() as patch:
            press = Mock(wraps=recording.press_offer)
            dismiss = Mock(wraps=recording.dismiss_offer)
            patch.setattr(recording, "press_offer", press)
            patch.setattr(recording, "dismiss_offer", dismiss)
            start_observing_with_view(instance, window)
            view = window._view
            _check_private_id(window)
            for name in CONTROLS.values():
                point = button_position(child(window, name))
                QTest.mouseClick(view, Qt.LeftButton, pos=point)
            press.assert_not_called()
            instance.now = 100.6
            with patcher.context() as input_patch:
                actions = Mock(return_value=False)
                input_patch.setattr(recording, "press_offer", actions)
                for action, name in CONTROLS.items():
                    button = child(window, name)
                    actions.reset_mock()
                    QTest.mouseClick(view, Qt.LeftButton, pos=button_position(button))
                    qml(button, "Accessible.pressAction()")
                    assert actions.call_args_list == [((action, 100_600), {})] * (
                        1 if action == "start" else 2
                    )
            QCoreApplication.processEvents()
            keys = [Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space]
            counter = KeyCounter()
            view.installEventFilter(counter)
            for name in CONTROLS.values():
                button = child(window, name)
                button.forceActiveFocus()
                assert button.hasActiveFocus() and view.activeFocusItem() is button
                for key in keys:
                    QTest.keyClick(view, key)
            assert counter.presses == keys * len(CONTROLS)
            press.assert_not_called()
            window.close()
            assert not view.isVisible()
            dismiss.assert_not_called()
            recording.offer.dismiss()
            instance.supervisor.event({**ended("call-PRIVATE-1"), "generation": 1})
            show_offer(instance, call_id="wm-close")
            assert_contents(window)
            assert QCoreApplication.sendEvent(view, QCloseEvent())
            dismiss.assert_called_once_with()
            assert not view.isVisible()
            instance.supervisor.event({**ended("wm-close"), "generation": 1})
            show_offer(instance, call_id="exit")
            window.shutdown()
            window.shutdown()
            window.show("invalid", 60)
            window.update_remaining(1)
            window.close()
            recording.shutdown()
            assert not view.isVisible()
            dismiss.assert_called_once_with()
            press.assert_not_called()
    finally:
        instance.bridge.shutdown()


def start_observing_with_view(
    instance: Rig, window: MeetingOffer, *, await_frame: bool = True, app_class: str = "dion"
) -> None:
    from test_meeting_m8_offer import show_offer, start_observing

    instance.recording.bind_offer(window)
    start_observing(instance)
    instance.recording._control_timer.stop()
    instance.recording._lease_timer.stop()
    show_offer(instance, call_id="call-PRIVATE-1", app_class=app_class)
    assert_contents(window, "call-browser" if app_class == "browser" else "call-app")
    QCoreApplication.processEvents()
    if not await_frame:
        return
    QCoreApplication.sendPostedEvents(None, QEvent.MetaCall)
    if window._first_frame_at_ms is None:
        loop = QEventLoop()
        view = window._view
        view.frameSwapped.connect(loop.quit, Qt.QueuedConnection)
        timer = QTimer()
        timer.setSingleShot(True)
        timer.timeout.connect(loop.quit)
        timer.start(2000)
        loop.exec()
        view.frameSwapped.disconnect(loop.quit)
        assert timer.isActive(), "W1 must render a frame"
    assert window._first_frame_at_ms == instance.recording.offer.now_ms()


def indicator(directory: Path, enabled: bool) -> None:
    from test_meeting_m8_offer import show_offer, start_observing
    from test_meeting_recording_bridge import Rig

    from astra_voice import app as app_module
    from astra_voice.core.settings import Settings
    from astra_voice.platform.session import SessionKind
    from astra_voice.ui import meeting_recording

    instance = Rig(
        directory,
        settings=Settings(meetings_enabled=True, pill_enabled=enabled),
        bind_recording=False,
    )
    try:
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(
                meeting_recording, "MeetingRecording", lambda *_a, **_kw: instance.recording
            )
            app_module._bind_meeting_recording(instance.bridge, Mock(device=""), SessionKind.KDE)
        window = instance.bridge._meeting_offer
        assert instance.bridge._stored.pill_enabled is enabled
        with pytest.MonkeyPatch.context() as patch:
            show = Mock(wraps=window.show)
            patch.setattr(window, "show", show)
            start_observing(instance)
            show_offer(instance)
        show.assert_called_once_with("call-app", 60)
        assert_contents(window)
    finally:
        instance.bridge.shutdown()


def _run_probe(mode: str, directory: Path) -> None:
    env = dict(
        os.environ, QT_QPA_PLATFORM="offscreen", QT_QUICK_BACKEND="software", QT_SCALE_FACTOR="1"
    )
    env["DBUS_SESSION_BUS_ADDRESS"] = "unix:path=/nonexistent"
    env["PYTHONPATH"] = os.pathsep.join(str(ROOT / path) for path in ("src", "tests", "tests/unit"))
    for key in (
        "DISPLAY",
        "WAYLAND_DISPLAY",
        "QT_ACCESSIBILITY",
        "AT_SPI_BUS_ADDRESS",
        "ASTRA_VOICE_RESOURCES",
    ):
        env.pop(key, None)
    result = subprocess.run(
        [sys.executable, __file__, mode, str(directory)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=27,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    expected = f"W1-PROBE-OK 1 {mode}"
    assert result.stdout.splitlines()[-1:] == [expected], result.stdout + result.stderr


@pytest.mark.parametrize(
    "mode",
    [
        "geometry",
        "visual-app",
        "visual-browser",
        "visual-colors",
        "visual-start-states",
        "starting-input",
        "start-failed-mouse",
        "start-failed-accessible",
        "show-after-failure",
        "double-click",
        "focus-click-spacing",
        "failure-input-region",
        "native-accessibility",
        "wm-close-states",
        "keys-close",
        "expose-mouse",
        "expose-accessible",
        "x-timestamp",
        "x-retry",
        "x-touch",
        "native-dispatch",
    ]
    + [f"{path}-{elapsed}" for path in ("mouse", "accessible") for elapsed in (100, 499, 500, 600)],
)
def test_w1_qml_behavior(mode: str, tmp_path: Path) -> None:
    _run_probe(mode, tmp_path)


def test_455_2_render_start_states(tmp_path: Path) -> None:
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "tools/render_meeting_offer.py"),
            "--out",
            str(tmp_path),
            "--only",
            "starting",
            "start-failed",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=27,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    rows = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))["pngs"]
    # The call-app card is 171 px. The failure height includes two lines of disk-low.
    heights = {"starting": 171, "start-failed": 124}
    assert {row["file"]: row["surface_rect"]["height"] for row in rows} == {
        f"{state}-{theme}.png": height
        for state, height in heights.items()
        for theme in ("light", "dark")
    }
    assert len(rows) == 4
    for row in rows:
        assert row["kind"] == "call-app" and row["surface_rect"]["width"] == 380
        assert (tmp_path / row["file"]).is_file()


@pytest.mark.parametrize("has_offer", [False, True])
def test_u17_offer_shutdown_precedes_capture_and_follows_w3(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, has_offer: bool
) -> None:
    order: list[str] = []
    recording = rig.recording
    with monkeypatch.context() as patch:
        for name in ("_meeting_notifications", "_meeting_notice", "_meeting_overlay"):
            patch.setattr(
                rig.bridge,
                name,
                SimpleNamespace(shutdown=lambda n=name: order.append(n)),
                raising=False,
            )
        if has_offer:
            patch.setattr(
                rig.bridge,
                "_meeting_offer",
                SimpleNamespace(shutdown=lambda: order.append("_meeting_offer")),
                raising=False,
            )
        real_shutdown = recording.shutdown

        def capture_shutdown() -> None:
            order.append("capture")
            real_shutdown()

        patch.setattr(recording, "shutdown", capture_shutdown)
        rig.bridge.shutdown()
    expected = ["_meeting_notifications", "_meeting_notice"]
    if has_offer:
        expected.append("_meeting_offer")
    assert order == [*expected, "capture", "_meeting_overlay"]


if __name__ == "__main__":
    app = get_qapplication()
    assert app.platformName() == "offscreen"
    fonts = sorted((ROOT / "tests/fixtures/fonts/pt-root-ui").glob("*.ttf"))
    assert fonts and all(QFontDatabase.addApplicationFont(str(path)) >= 0 for path in fonts)
    mode, directory = sys.argv[1:]
    if mode == "geometry":
        geometry()
    elif mode.startswith("visual-"):
        visuals(mode)
    elif mode in {"indicator-on", "indicator-off"}:
        indicator(Path(directory), enabled=mode == "indicator-on")
    elif mode == "keys-close":
        keys_and_close(Path(directory))
    elif mode == "starting-input":
        starting_input(Path(directory))
    elif mode.startswith("start-failed-"):
        start_failed_input(Path(directory), mode.removeprefix("start-failed-"))
    elif mode == "show-after-failure":
        show_after_failure(Path(directory))
    elif mode == "double-click":
        double_click()
    elif mode == "focus-click-spacing":
        focus_click_spacing()
    elif mode == "failure-input-region":
        failure_input_region()
    elif mode == "native-accessibility":
        native_accessibility()
    elif mode == "wm-close-states":
        wm_close_states()
    elif mode.startswith("expose-"):
        delayed_expose(Path(directory), mode.removeprefix("expose-"))
    elif mode == "x-timestamp":
        x_timestamp(Path(directory))
    elif mode == "x-retry":
        x_retry(Path(directory))
    elif mode == "x-touch":
        x_touch()
    elif mode == "native-dispatch":
        native_dispatch()
    else:
        input_mode, elapsed = mode.split("-")
        input_at(Path(directory), input_mode, int(elapsed))
    print(f"W1-PROBE-OK 1 {mode}")
