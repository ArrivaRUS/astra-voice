"""Offscreen probes of the real W1, isolated from the pytest QApplication."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from ctypes import CDLL, c_void_p
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
from PyQt5.QtTest import QTest

from astra_voice.ui.meeting_offer_window import MeetingOffer, OfferQuickView
from astra_voice.ui.meeting_texts import W1_CALL_TEXTS, W1_TEXTS
from helpers.qt_app import get_qapplication

if TYPE_CHECKING:
    from test_meeting_recording_bridge import Rig

if __name__ != "__main__":
    from test_meeting_recording_bridge import rig as rig

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]
CONTROLS = {"start": "offerStart", "refuse": "offerRefuse", "close": "offerClose"}
SHADOW_MARGIN = 36


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

    with surface(Mock(), cursor_pos=cursor, view_factory=ScaledView) as window:
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


def visuals(mode: str) -> None:
    """Q1/Q3/Q5, covering theme source, text, layout, hover and countdown."""
    theme = ThemeSource()
    warnings: list[str] = []

    def factory() -> QQuickView:
        view = QQuickView()
        view.engine().rootContext().setContextProperty("themeSource", theme)
        view.engine().warnings.connect(lambda rows: warnings.extend(row.toString() for row in rows))
        return view

    with surface(Mock(), cursor_pos=QPoint, view_factory=factory) as window:
        window._root.setProperty("freezeAnimations", True)
        if mode == "visual-colors":
            visual_colors(window, theme)
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
        assert qml(button, "Accessible.role === Accessible.Button") is True
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


def _check_private_id(window: MeetingOffer) -> None:
    call_id = "call-PRIVATE-1"
    for item in (window._root, *(child(window, name) for name in CONTROLS.values())):
        for field in ("name", "description"):
            assert call_id not in repr(qml(item, f"Accessible.{field}")), (
                item.objectName(),
                field,
            )
    for item in [window._root, *window._root.findChildren(QQuickItem)]:
        meta = item.metaObject()
        for index in range(meta.propertyCount()):
            prop = meta.property(index)
            if prop.isReadable() and prop.typeName() in {
                "QString",
                "QVariant",
                "QVariantMap",
                "QVariantList",
                "QVariantHash",
                "QJSValue",
            }:
                value = prop.read(item)
                if hasattr(value, "toVariant"):
                    value = value.toVariant()
                assert call_id not in repr(value), prop.name()
        for name in item.dynamicPropertyNames():
            assert call_id not in repr(item.property(bytes(name).decode()))


def input_at(directory: Path, mode: str, elapsed: int) -> None:
    """Q4, including intent through the real MeetingRecording adapter."""
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
            actions = ("refuse", "close", "start") if elapsed == 100 else ("start",)
            for action in actions:
                button = child(window, CONTROLS[action])
                if mode == "accessible":
                    qml(button, "Accessible.pressAction()")
                else:
                    QTest.mousePress(window._view, Qt.LeftButton, pos=button_position(button))
                if elapsed < 500:
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
            if elapsed < 500:
                intent.assert_not_called()
                assert not instance.bridge.startDialogOpen and window._view.isVisible()
            else:
                intent.assert_called_once_with()
                assert instance.bridge.startDialogOpen and not window._view.isVisible()
                recording.tick()
                intent.assert_called_once_with()
            assert not any(
                msg["type"] in {"meeting.capture.prepare", "meeting.capture.open"}
                for msg in instance.supervisor.messages
            )
    finally:
        instance.bridge.shutdown()


def delayed_expose(directory: Path, mode: str) -> None:
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
            instance.now = 101.15
            click()
            press.assert_called_once_with("start", 101_150)
            intent.assert_called_once_with()
            assert instance.bridge.startDialogOpen
    finally:
        instance.bridge.shutdown()


def x_timestamp(directory: Path) -> None:
    """Real Qt/QML mouse dispatch with independently controlled GUI and X timestamps."""
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
            click((exposed + 500) % wrap)
            press.assert_called_once_with("start", 100_600)
            assert instance.bridge.startDialogOpen
    finally:
        instance.bridge.shutdown()


def x_retry(directory: Path) -> None:
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
            with patch.context() as access_patch:
                accessible = Mock(return_value=False)
                access_patch.setattr(recording, "press_offer", accessible)
                qml(button, "Accessible.pressAction()")
                accessible.assert_called_once_with("start", 100_600)
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
            assert instance.bridge.startDialogOpen
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
                    QTest.mouseClick(view, Qt.LeftButton, pos=button_position(button))
                    qml(button, "Accessible.pressAction()")
                    assert actions.call_args_list[-2:] == [
                        ((action, 100_600), {}),
                        ((action, 100_600), {}),
                    ]
                assert actions.call_count == 6
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
    instance: Rig, window: MeetingOffer, *, await_frame: bool = True
) -> None:
    from test_meeting_m8_offer import show_offer, start_observing

    instance.recording.bind_offer(window)
    start_observing(instance)
    instance.recording._control_timer.stop()
    instance.recording._lease_timer.stop()
    show_offer(instance, call_id="call-PRIVATE-1")
    assert_contents(window)
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
