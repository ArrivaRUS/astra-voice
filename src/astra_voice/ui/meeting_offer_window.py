"""W1 surface. The offer controller owns timing and admission."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from PyQt5.QtCore import QEvent, QMetaObject, QObject, QPoint, QRect, Qt, QTimer, QUrl, pyqtSignal
from PyQt5.QtGui import QCursor, QGuiApplication, QMouseEvent, QRegion, QTouchEvent, QWheelEvent
from PyQt5.QtQuick import QQuickView

from astra_voice.core.logging import log_meeting_offer
from astra_voice.core.paths import qml_dir
from astra_voice.meetings.events import OfferEvent, OfferReason
from astra_voice.platform.x11 import TIMESTAMP_ATOM, X11Display
from astra_voice.ui.meeting_notice import NOTICE_SHADOW_MARGIN, NOTICE_SHADOW_OFFSET_Y
from astra_voice.ui.meeting_offer import OFFER_GUARD_MS
from astra_voice.ui.meeting_overlay import SurfaceWindow
from astra_voice.ui.meeting_texts import W1_CALL_TEXTS, W1_REMAINING, W1_TEXTS, W4_TEXTS

if TYPE_CHECKING:
    from astra_voice.ui.meeting_recording import MeetingRecording


class OfferQuickView(QQuickView):
    """Expose the X timestamp only during the pointer press that raised the QML signal."""

    pointerDispatched = pyqtSignal()
    press_timestamp: int | None = None

    def event(self, event: QEvent) -> bool:
        if isinstance(event, QMouseEvent) and (
            event.type() == QEvent.MouseButtonDblClick
            or (
                event.type() == QEvent.MouseButtonPress
                and event.flags() & Qt.MouseEventCreatedDoubleClick
            )
        ):
            # Qt also sends a regular-looking press before the double-click event.
            event.accept()
            self.pointerDispatched.emit()
            return True
        if isinstance(event, (QMouseEvent, QTouchEvent)) and event.type() in (
            QEvent.MouseButtonPress,
            QEvent.TouchBegin,
        ):
            timestamp = int(event.timestamp())
        elif isinstance(event, QWheelEvent):
            timestamp = None
        else:
            return bool(super().event(event))
        previous, self.press_timestamp = self.press_timestamp, timestamp
        try:
            return bool(super().event(event))
        finally:
            self.press_timestamp = previous
            self.pointerDispatched.emit()


class MeetingOffer(SurfaceWindow):
    _width_property = "surfaceWidth"
    _height_property = "surfaceHeight"
    _shadow_margin = NOTICE_SHADOW_MARGIN
    _shadow_offset_y = NOTICE_SHADOW_OFFSET_Y

    def __init__(
        self,
        recording: MeetingRecording,
        *,
        cursor_pos: Callable[[], QPoint] = QCursor.pos,
        view_factory: Callable[[], Any] | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.recording = recording
        self._cursor_pos = cursor_pos
        self._closed = False
        self._open = False
        self._starting = False
        self._start_failed_at_ms: int | None = None
        self._failure_input_timer = QTimer(self)
        self._failure_input_timer.setSingleShot(True)
        self._failure_input_timer.setTimerType(Qt.PreciseTimer)
        self._failure_input_timer.timeout.connect(self._release_failure_input)
        self._xcb = QGuiApplication.platformName() == "xcb"
        self._waiting_for_expose = False
        self._frame_handler: Callable[[], None] | None = None
        self._first_frame_at_ms: int | None = None
        self._show_logged_at_ms: int | None = None
        self._frame_x_time: int | None = None
        self._restoring_user_time = False
        self._workarea: QRect | None = None
        self._at_right = False
        self._input_region = QRegion()
        self._input_native_window_id = None
        self._x11 = X11Display()
        self.destroyed.connect(self._x11.close)
        try:
            self._load_overlay(
                QUrl.fromLocalFile(str(qml_dir() / "MeetingOffer.qml")),
                view_factory=view_factory or OfferQuickView,
            )
            self._root.setProperty("x", self._shadow_margin)
            self._root.setProperty("y", self._shadow_margin)
            self._root.pressed.connect(self._press, Qt.DirectConnection)
            self._root.surfaceHeightChanged.connect(self._resize, Qt.QueuedConnection)
            if isinstance(self._view, OfferQuickView):
                self._view.pointerDispatched.connect(self._restore_user_time, Qt.DirectConnection)
        except Exception:
            self.deleteLater()
            raise

    def show(self, kind: str, seconds: int) -> None:
        if self._closed:
            return
        if kind not in W1_CALL_TEXTS:
            raise ValueError("Unknown meeting offer kind")
        if self._open:
            # Remap for a new Expose and first frame, including on X11.
            self.close()
        self._open = True
        self._starting = False
        self._start_failed_at_ms = None
        self._root.setProperty("starting", False)
        self._root.setProperty("startFailed", False)
        self._root.setProperty("errorText", "")
        self._root.setProperty("kind", kind)
        self._root.setProperty(
            "texts",
            W1_TEXTS
            | W1_CALL_TEXTS[kind]
            | {"starting": W4_TEXTS["starting"], "openMeetings": W4_TEXTS["openMeetings"]},
        )
        self.update_remaining(seconds)
        self._resize()
        if not self._open:
            return
        self._workarea = self._overlay_workarea()
        if self._workarea is not None:
            width, height = self._surface_size()
            inset = QPoint(self._shadow_margin, self._shadow_margin)
            origin = self._window_position(self._workarea, at_right=False) + inset
            card = QRect(origin.x(), origin.y(), width, height)
            self._at_right = card.contains(self._cursor_pos())
            self._place()
        self._prepare_window()
        self._root.setProperty("presented", True)
        self._waiting_for_expose = True
        self._view.setProperty("_q_showWithoutActivating", True)
        self._view.show()
        if self._open:
            self._show_logged_at_ms = self.recording.offer.now_ms()
            reason = OfferReason.NO_WORKAREA if self._workarea is None else OfferReason.PLACED
            log_meeting_offer(OfferEvent.SHOWN, reason)

    def starting(self) -> None:
        if self._open and self._start_failed_at_ms is None:
            self._starting = True
            self._root.setProperty("starting", True)

    def start_failed(self, text: str) -> None:
        if self._open:
            failed_at = self.recording.offer.now_ms()
            self._start_failed_at_ms = failed_at
            self._starting = False
            self._root.setProperty("errorText", text)
            self._root.setProperty("startFailed", True)
            self._root.setProperty("starting", False)
            QMetaObject.invokeMethod(self._root, "forceLayout", Qt.DirectConnection)
            if self._surface_size()[1] < self._input_region.boundingRect().height():
                elapsed = self.recording.offer.now_ms() - failed_at
                self._failure_input_timer.start(max(0, OFFER_GUARD_MS - elapsed))
            else:
                self._failure_input_timer.stop()

    def _resize(self) -> None:
        if (
            self._failure_input_timer.isActive()
            and self._surface_size()[1] < self._input_region.boundingRect().height()
        ):
            # QML draws the shorter card immediately. Keep the old native
            # bounds and input shape until the click guard expires.
            return
        super()._resize()

    def _release_failure_input(self) -> None:
        if not self._open or self._start_failed_at_ms is None:
            return
        remaining = OFFER_GUARD_MS - (self.recording.offer.now_ms() - self._start_failed_at_ms)
        if remaining > 0:
            self._failure_input_timer.start(remaining)
            return
        self._failure_input_timer.stop()
        self._resize()

    def _window_position(self, area: QRect, *, at_right: bool) -> QPoint:
        width, _ = self._surface_size()
        offset = int(self._root.property("screenOffset"))
        x = (
            area.x() + area.width() - width - offset
            if at_right
            else area.x() + (area.width() - width) // 2
        )
        inset = QPoint(self._shadow_margin, self._shadow_margin)
        return self._clamped(QPoint(x, area.y() + offset) - inset, area)

    def _place(self) -> None:
        # Failure changes the height while keeping the original top-left corner.
        if self._workarea is not None and self._start_failed_at_ms is None:
            self._view.setPosition(self._window_position(self._workarea, at_right=self._at_right))

    def update_remaining(self, seconds: int) -> None:
        if not self._closed and not self._starting and self._start_failed_at_ms is None:
            self._root.setProperty("seconds", seconds)
            self._root.setProperty("remainingText", W1_REMAINING.format(seconds=seconds))

    def _track_frames(self) -> None:
        self._waiting_for_expose = False

        def swapped() -> None:
            # A queued signal from a previous presentation can outlive disconnect().
            if self._frame_handler is swapped:
                self._frame_swapped()

        self._frame_handler = swapped
        # The render loop can emit from a thread other than the GUI thread.
        self._view.frameSwapped.connect(swapped, Qt.QueuedConnection)
        if self._xcb:
            self._restore_user_time()

    def _stop_tracking_frames(self) -> None:
        handler, self._frame_handler = self._frame_handler, None
        if handler is not None:
            self._view.frameSwapped.disconnect(handler)

    def _frame_swapped(self) -> None:
        if not self._view.isVisible() or not self._view.isExposed():
            return
        if self._first_frame_at_ms is None:
            self._first_frame_at_ms = self.recording.offer.now_ms()
            shown_at = self._show_logged_at_ms
            shown_ms = self._first_frame_at_ms - shown_at if shown_at is not None else 0
            log_meeting_offer(OfferEvent.FIRST_FRAME, OfferReason.FRAME_SWAPPED, shown_ms)
        if self._xcb:
            self._frame_x_time = self._sample_x_time()
            if self._frame_x_time is None:
                # A subsequent frame can retry without waiting for another X event here.
                return
        self._stop_tracking_frames()

    def _sample_x_time(self) -> int | None:
        """An ICCCM property event on our Xlib connection timestamps the exposed offer."""
        connection = self._x11.d
        if connection is None:
            return None
        try:
            from Xlib import X, Xatom

            window = connection.create_resource_object("window", int(self._view.winId()))
            attributes = window.get_attributes()
            if attributes.map_state != X.IsViewable:
                return None
            mask = attributes.your_event_mask
            atom = connection.intern_atom(TIMESTAMP_ATOM)
            # This connection belongs to W1. Clear any older event before this probe.
            for _ in range(connection.pending_events()):
                connection.next_event()
            before = self._x11._error_count
            window.change_attributes(event_mask=mask | X.PropertyChangeMask)
            try:
                window.change_property(atom, Xatom.STRING, 8, b"")
                connection.sync()
                if self._x11._error_count == before:
                    for _ in range(connection.pending_events()):
                        event = connection.next_event()
                        if (
                            event.type == X.PropertyNotify
                            and not event.send_event
                            and event.window.id == window.id
                            and event.atom == atom
                            and event.state == X.PropertyNewValue
                        ):
                            return int(event.time)
            finally:
                window.change_attributes(event_mask=mask)
                connection.flush()
        except Exception:
            # Without proof of the X epoch, native pointer input stays closed.
            return None
        return None

    def _restore_user_time(self) -> None:
        connection = self._x11.d
        if not self._xcb or self._closed or connection is None or self._restoring_user_time:
            return
        self._restoring_user_time = True
        try:
            # Qt can write user time on its separate XCB connection during map
            # and button dispatch. Order the correction after those requests.
            QGuiApplication.sync()
            if self._closed or self._x11.d is not connection:
                return
            from Xlib import Xatom

            native = connection.create_resource_object("window", int(self._view.winId()))
            link = connection.intern_atom("_NET_WM_USER_TIME_WINDOW")
            user_time = connection.intern_atom("_NET_WM_USER_TIME")
            target = native.get_full_property(link, Xatom.WINDOW)
            native.change_property(user_time, Xatom.CARDINAL, 32, [0])
            if target is not None and target.format == 32 and len(target.value) == 1:
                wid = int(target.value[0])
                if wid and wid != native.id:
                    aux = connection.create_resource_object("window", wid)
                    aux.change_property(user_time, Xatom.CARDINAL, 32, [0])
            connection.sync()
        except Exception:
            # A lost X window must never propagate through a Qt event handler.
            return
        finally:
            self._restoring_user_time = False

    def _press(self, action: str, mouse: bool) -> None:
        if self._starting or (action == "start" and not mouse):
            return
        if not self._open or not self._view.isVisible() or not self._view.isExposed():
            return
        now = self.recording.offer.now_ms()
        if self._start_failed_at_ms is not None:
            if action not in {"open_meetings", "close"}:
                return
            if now - self._start_failed_at_ms < OFFER_GUARD_MS:
                return
        elif action not in {"start", "refuse", "close"}:
            return
        if self._first_frame_at_ms is None or now - self._first_frame_at_ms < OFFER_GUARD_MS:
            return
        if mouse and self._xcb:
            stamp = getattr(self._view, "press_timestamp", None)
            if not stamp or self._frame_x_time is None:
                return
            # X timestamps wrap at 2**32. The upper half denotes an older event.
            elapsed = (stamp - self._frame_x_time) & 0xFFFFFFFF
            if not OFFER_GUARD_MS <= elapsed < 0x80000000:
                return
        self.recording.press_offer(action, now)

    def close(self) -> None:
        if self._closed:
            return
        self._open = False
        self._failure_input_timer.stop()
        self._waiting_for_expose = False
        self._stop_tracking_frames()
        self._first_frame_at_ms = self._frame_x_time = None
        self._show_logged_at_ms = None
        self._workarea = None
        self._view.hide()
        self._root.setProperty("presented", False)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is not getattr(self, "_view", None) or self._closed:
            return False
        if event.type() == QEvent.Expose and self._waiting_for_expose and self._view.isExposed():
            self._track_frames()
        if event.type() == QEvent.Close:
            event.ignore()
            self.recording.dismiss_offer()
            return True
        return False

    def shutdown(self) -> None:
        if self._closed:
            return
        self.close()
        self._closed = True
        self._x11.close()
