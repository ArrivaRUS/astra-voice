"""W2 view of published capture state; independent from dictation preferences."""

from __future__ import annotations

import logging
from collections.abc import Callable
from math import ceil, floor
from typing import Any

from PyQt5.QtCore import QEvent, QMetaObject, QObject, QPoint, Qt, QUrl
from PyQt5.QtGui import QGuiApplication, QRegion

from astra_voice.core.paths import qml_dir
from astra_voice.platform.session import SessionKind
from astra_voice.platform.x11 import X11Display
from astra_voice.ui.meeting_recording import MeetingRecording
from astra_voice.ui.meeting_texts import W2_TEXTS
from astra_voice.ui.overlay import SHADOW_MARGIN, SHADOW_OFFSET_Y, WINDOW_FLAGS, OverlayWindow

log = logging.getLogger(__name__)


class MeetingOverlay(OverlayWindow):
    def __init__(
        self,
        recording: MeetingRecording,
        *,
        session: SessionKind = SessionKind.KDE,
        qml_url: QUrl | None = None,
        view_factory: Callable[[], Any] | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.recording = recording
        self._session = session
        self._closed = False
        self._input_region = QRegion()
        self._input_native_window_id: int | None = None
        self._dragged_position: QPoint | None = None
        self._x11 = X11Display()
        self.destroyed.connect(self._x11.close)
        self._load_overlay(
            qml_url or QUrl.fromLocalFile(str(qml_dir() / "MeetingOverlay.qml")),
            view_factory=view_factory,
        )
        self._root.setProperty("x", SHADOW_MARGIN)
        self._root.setProperty("y", SHADOW_MARGIN)
        self._root.setProperty("texts", dict(W2_TEXTS))
        self._root.pauseClicked.connect(self.pause)
        self._root.resumeClicked.connect(self.resume)
        self._root.stopClicked.connect(self.stop)
        self._root.participantsClicked.connect(self.participants)
        self._root.extendClicked.connect(self.extend)
        self._root.releaseKeyboardRequested.connect(self.release_keyboard)
        self._root.dragRequested.connect(self._drag)
        self._root.pillWidthChanged.connect(self._resize, Qt.QueuedConnection)
        self._root.pillHeightChanged.connect(self._resize, Qt.QueuedConnection)
        self._root.pillWidthChanged.connect(self._update_mask)
        self._root.pillHeightChanged.connect(self._update_mask)
        self._root.compactChanged.connect(self._resize, Qt.QueuedConnection)
        recording.changed.connect(self.refresh)
        self.refresh()

    def pause(self) -> None:
        self.recording.overlay_action("pause")

    @property
    def indicator_visible(self) -> bool:
        """Mapped W2 surface on a screen; overlap with other windows is irrelevant."""
        if (
            self._closed
            or not self._view.isVisible()
            or self._view.windowState() & Qt.WindowMinimized
        ):
            return False
        surface = self._input_region.boundingRect().translated(self._view.position())
        if surface.isEmpty() or not any(
            surface.intersects(screen.geometry()) for screen in QGuiApplication.screens()
        ):
            return False
        if QGuiApplication.platformName() != "xcb":
            return bool(self._view.isExposed())
        try:
            if self._x11.d is None and not self._x11.open():
                return False
            from Xlib import X

            window = self._x11.d.create_resource_object("window", int(self._view.winId()))
            return bool(window.get_attributes().map_state == X.IsViewable)
        except Exception:
            log.warning("meeting-overlay-visibility-unavailable")
            self._x11.close()
            return False

    def resume(self) -> None:
        self.recording.overlay_action("resume")

    def stop(self) -> None:
        self.recording.overlay_action("stop")

    def participants(self) -> None:
        self.recording.overlay_action("participants")

    def extend(self) -> None:
        self.recording.overlay_action("extend")

    def refresh(self) -> None:
        if self._closed:
            return
        presentation = self.recording.overlay
        self._root.setProperty("presentation", presentation)
        if presentation["visible"] and not self._view.isVisible():
            self._prepare_window()
        self._resize()
        if presentation["visible"]:
            if not self._view.isVisible():
                self._prepare_window()
                self._view.show()
                self._prepare_window()
        else:
            self.release_keyboard()
            self._view.hide()

    def _prepare_window(self) -> None:
        try:
            self._configure_native_window()
        except Exception:
            # Window metadata must not turn a capture update into a UI exception.
            log.warning("meeting-overlay-window-unavailable")
            self._x11.close()

    def _configure_native_window(self) -> None:
        if QGuiApplication.platformName() != "xcb":
            return
        if self._x11.d is None and not self._x11.open():
            return
        from Xlib import Xatom

        connection = self._x11.d
        window_type = (
            "_NET_WM_WINDOW_TYPE_UTILITY"
            if self._root.property("keyboardMode")
            else "_NET_WM_WINDOW_TYPE_NOTIFICATION"
        )
        window = connection.create_resource_object("window", int(self._view.winId()))
        for name, values, kind in (
            (
                "_NET_WM_WINDOW_TYPE",
                [connection.intern_atom(window_type)],
                Xatom.ATOM,
            ),
            (
                "_NET_WM_STATE",
                [
                    connection.intern_atom("_NET_WM_STATE_ABOVE"),
                    connection.intern_atom("_NET_WM_STATE_SKIP_TASKBAR"),
                    connection.intern_atom("_NET_WM_STATE_SKIP_PAGER"),
                ],
                Xatom.ATOM,
            ),
            ("_NET_WM_DESKTOP", [0xFFFFFFFF], Xatom.CARDINAL),
            ("_NET_WM_USER_TIME", [0], Xatom.CARDINAL),
        ):
            window.change_property(connection.intern_atom(name), kind, 32, values)
        connection.flush()

    def _resize(self) -> None:
        if self._closed:
            return
        QMetaObject.invokeMethod(self._root, "forceLayout", Qt.DirectConnection)
        self._view.resize(
            ceil(float(self._root.property("pillWidth"))) + SHADOW_MARGIN * 2,
            ceil(float(self._root.property("pillHeight"))) + SHADOW_MARGIN * 2 + SHADOW_OFFSET_Y,
        )
        self._update_mask()
        self._place()

    def _update_mask(self) -> None:
        if self._closed:
            return
        width = ceil(float(self._root.property("pillWidth")))
        height = ceil(float(self._root.property("pillHeight")))
        self._input_region = QRegion(SHADOW_MARGIN, SHADOW_MARGIN, width, height)
        if self._set_x11_input_mask(width, height):
            if not self._view.mask().isEmpty():
                # A confirmed Input region survives clearing fallback Bounding.
                self._view.setMask(QRegion())
        else:
            self._set_mask_fallback()

    def _set_mask_fallback(self) -> None:
        self._view.setMask(self._input_region)
        if QGuiApplication.platformName() != "xcb":
            return
        # Bounding must constrain input before Qt resets any previous Input shape.
        flags = self._view.flags()
        try:
            self._view.setFlags(flags | Qt.WindowTransparentForInput)
        finally:
            self._view.setFlags(flags)
        self._prepare_window()

    def _set_x11_input_mask(self, width: int, height: int) -> bool:
        if QGuiApplication.platformName() != "xcb":
            return False
        try:
            if self._x11.d is None and not self._x11.open():
                return False
            from Xlib import X
            from Xlib.ext import shape

            connection = self._x11.d
            if not connection.has_extension("SHAPE") or not connection.has_extension("XFIXES"):
                return False
            window_id = int(self._view.winId())
            if self._input_native_window_id != window_id:
                # Flush Qt's native creation before using the separate Xlib connection.
                self._input_native_window_id = window_id
                QGuiApplication.sync()
                # Qt synchronization may have completed another QML layout.
                width = self._input_region.boundingRect().width()
                height = self._input_region.boundingRect().height()
            window = connection.create_resource_object("window", window_id)
            ratio = float(self._view.devicePixelRatio())
            left = top = floor(SHADOW_MARGIN * ratio)
            right = ceil((SHADOW_MARGIN + width) * ratio)
            bottom = ceil((SHADOW_MARGIN + height) * ratio)
            before = self._x11._error_count
            window.shape_rectangles(
                shape.SO.Set,
                shape.SK.Input,
                X.Unsorted,
                0,
                0,
                [(left, top, right - left, bottom - top)],
            )
            connection.sync()
            if self._x11._error_count != before:
                raise RuntimeError("X11 input mask rejected")
            return True
        except Exception:
            log.warning("meeting-overlay-input-mask-unavailable")
            self._x11.close()
            return False

    def _place(self) -> None:
        screen = (
            QGuiApplication.screenAt(self._dragged_position + QPoint(SHADOW_MARGIN, SHADOW_MARGIN))
            if self._dragged_position is not None
            else None
        )
        area = self._overlay_workarea(screen)
        if area is None:
            return
        if self._dragged_position is None:
            point = QPoint(
                area.x() + (area.width() - self._view.width()) // 2,
                area.y() + 12 - SHADOW_MARGIN,
            )
        else:
            point = self._dragged_position
        # Clamp the visible surface, allowing its transparent shadow past edges.
        x = min(
            max(point.x(), area.left() - SHADOW_MARGIN),
            max(area.left() - SHADOW_MARGIN, area.right() + 1 - self._view.width() + SHADOW_MARGIN),
        )
        y = min(
            max(point.y(), area.top() - SHADOW_MARGIN),
            max(
                area.top() - SHADOW_MARGIN,
                area.bottom() + 1 - self._view.height() + SHADOW_MARGIN + SHADOW_OFFSET_Y,
            ),
        )
        self._view.setPosition(x, y)

    def _drag(self, dx: float, dy: float) -> None:
        self._dragged_position = self._view.position() + QPoint(round(dx), round(dy))
        self._place()
        self._dragged_position = self._view.position()

    def activate_keyboard(self) -> None:
        if self._closed or not self.recording.overlay["visible"]:
            return
        self._view.setFlags(WINDOW_FLAGS & ~Qt.WindowDoesNotAcceptFocus)
        self._root.setProperty("keyboardMode", True)
        self._prepare_window()
        self._resize()
        self._view.show()
        self._view.requestActivate()
        QMetaObject.invokeMethod(self._root, "focusFirstControl", Qt.DirectConnection)

    def release_keyboard(self) -> None:
        if not self._root.property("keyboardMode"):
            return
        self._root.setProperty("keyboardMode", False)
        self._root.setProperty("focus", False)
        self._view.setFlags(WINDOW_FLAGS)
        if not self._closed and self.recording.overlay["visible"]:
            self._prepare_window()
            self._resize()
            self._view.show()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if (
            watched is self._view
            and event.type() == QEvent.Close
            and not self._closed
            and self.recording.overlay["visible"]
        ):
            event.ignore()
            return True
        if watched is self._view and event.type() == QEvent.FocusOut:
            self.release_keyboard()
        return False

    def shutdown(self) -> None:
        if self._closed:
            return
        self.recording.unbind_indicators()
        self._closed = True
        self.recording.changed.disconnect(self.refresh)
        self._view.hide()
        self._x11.close()
