"""W3 «Предупредите участников о записи» under W2 (spec §2.3/§2.4/§3.6).

Opens once when a recording starts and again on «Текст для участников» (W2, tray).
It never takes focus on its own; the tray path and an explicit text selection
switch it to keyboard mode. Copy publishes only ``text/plain``. The program never
claims that participants were warned (US-13.6).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from PyQt5.QtCore import QEvent, QMetaObject, QObject, QPoint, Qt, QTimer, QUrl
from PyQt5.QtGui import QRegion

from astra_voice.core.paths import qml_dir
from astra_voice.platform.x11 import X11Display
from astra_voice.ui.meeting_overlay import MeetingOverlay, SurfaceWindow
from astra_voice.ui.meeting_recording import MeetingRecording
from astra_voice.ui.meeting_texts import W3_TEXTS, notice_mute_text
from astra_voice.ui.overlay import WINDOW_FLAGS

log = logging.getLogger(__name__)

# Theme.durationPillClipboard: «✓ Скопировано», then back to the primary button.
COPIED_MS = 1200
# shadow.notify «0 14px 36px»: the transparent margin must hold the whole blur.
NOTICE_SHADOW_MARGIN = 36
NOTICE_SHADOW_OFFSET_Y = 14
# tokens.md §5.2: W3 sits 8 px under the W2 body, centred on it.
NOTICE_GAP = 8
# Fallback when W2 has no mapped surface: its default top offset and one-row height.
W2_SCREEN_OFFSET = 12
W2_ROW_HEIGHT = 36
INACTIVE_STATES = frozenset(("hidden", "stopping", "error"))


def copy_plain_text(text: str) -> bool:
    """Publish ``text/plain`` only; True means the clipboard now serves this text."""
    try:
        from PyQt5.QtCore import QMimeData
        from PyQt5.QtGui import QClipboard, QGuiApplication

        clipboard = QGuiApplication.clipboard()
        if clipboard is None:
            return False
        mime = QMimeData()
        mime.setText(text)
        clipboard.setMimeData(mime, QClipboard.Clipboard)
        if QGuiApplication.platformName() == "xcb":
            return bool(clipboard.ownsClipboard())
        published = clipboard.mimeData(QClipboard.Clipboard)
        return published is not None and published.text() == text
    except Exception:
        # Never log the notice text.
        log.warning("meeting-notice-copy-unavailable")
        return False


class MeetingNotice(SurfaceWindow):
    _width_property = "surfaceWidth"
    _height_property = "surfaceHeight"
    _shadow_margin = NOTICE_SHADOW_MARGIN
    _shadow_offset_y = NOTICE_SHADOW_OFFSET_Y

    def __init__(
        self,
        recording: MeetingRecording,
        overlay: MeetingOverlay | None,
        *,
        notice: Callable[[], tuple[str, bool]],
        clipboard: Callable[[str], bool] = copy_plain_text,
        qml_url: QUrl | None = None,
        view_factory: Callable[[], Any] | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.recording = recording
        self._overlay = overlay
        self._notice = notice
        self._clipboard = clipboard
        self._closed = False
        self._open = False
        self._copy_state = "default"
        self._announced = ""
        self._input_region = QRegion()
        self._input_native_window_id: int | None = None
        self._x11 = X11Display()
        self.destroyed.connect(self._x11.close)
        self._copied = QTimer(self)
        self._copied.setSingleShot(True)
        self._copied.setInterval(COPIED_MS)
        self._copied.timeout.connect(self._copy_feedback_done)
        self._load_overlay(
            qml_url or QUrl.fromLocalFile(str(qml_dir() / "MeetingNotice.qml")),
            view_factory=view_factory,
        )
        self._root.setProperty("x", NOTICE_SHADOW_MARGIN)
        self._root.setProperty("y", NOTICE_SHADOW_MARGIN)
        self._root.setProperty("texts", dict(W3_TEXTS))
        self._root.copyClicked.connect(self.copy)
        self._root.closeClicked.connect(self.close)
        self._root.keyboardRequested.connect(self._selection_needs_keyboard)
        self._root.copySelectionRequested.connect(self.copy_selection)
        self._root.surfaceHeightChanged.connect(self._resize, Qt.QueuedConnection)
        recording.changed.connect(self.refresh)
        recording.participantsRequested.connect(self.open)
        if overlay is not None:
            overlay.geometryChanged.connect(self._place_if_open)
        self.refresh()

    @property
    def is_open(self) -> bool:
        return self._open and not self._closed

    @property
    def copy_state(self) -> str:
        return self._copy_state

    def _active(self) -> bool:
        overlay = self.recording.overlay
        return bool(overlay["active"]) and overlay["state"] not in INACTIVE_STATES

    def open(self) -> None:
        if self._closed or not self._active():
            return
        self._open = True
        self._copy_state = "default"
        self._copied.stop()
        self.refresh()

    def close(self) -> None:
        if not self._open:
            return
        self._open = False
        self._copied.stop()
        self.refresh()

    def copy(self) -> None:
        if self._closed or not self._open:
            return
        text, _admin = self._notice()
        if self._clipboard(text):
            self._copy_state = "copied"
            self._copied.start()
        else:
            # The textbox stays selectable for a manual copy; no retry loop.
            self._copy_state = "failed"
            self._copied.stop()
        self.refresh()

    def copy_selection(self, text: str) -> None:
        """Manual Ctrl+C in the textbox: the same text/plain-only publication."""
        if self._closed or not self._open or not text:
            return
        # Rich-text layout separators back to plain line breaks.
        self._clipboard(text.replace("\u2028", "\n").replace("\u2029", "\n"))

    def _copy_feedback_done(self) -> None:
        if self._copy_state == "copied":
            self._copy_state = "default"
            self.refresh()

    def refresh(self) -> None:
        if self._closed:
            return
        meeting_id = self.recording.active_meeting_id
        if meeting_id and meeting_id != self._announced and self._active():
            # Spec §2.3: W3 appears with W2 at every start of a recording.
            self._announced = meeting_id
            self._open, self._copy_state = True, "default"
            self._copied.stop()
        if self._open and not self._active():
            self._open = False
            self._copied.stop()
        text, admin = self._notice()
        self._root.setProperty(
            "presentation",
            {
                "visible": self._open,
                "text": text,
                "admin": admin,
                "copyState": self._copy_state,
                "muteText": notice_mute_text(self.recording.notice_app_class),
            },
        )
        if self._open:
            if not self._view.isVisible():
                self._prepare_window()
            self._resize()
            if not self._view.isVisible():
                self._view.show()
                self._prepare_window()
                self._place()
        else:
            self.release_keyboard()
            self._view.hide()

    def _place_if_open(self) -> None:
        if self._open and not self._closed:
            self._place()

    def _place(self) -> None:
        width, height = self._surface_size()
        margin = self._shadow_margin
        body = self._overlay.surface_rect() if self._overlay is not None else None
        if body is not None:
            area = self._overlay_workarea(self._screen_application().screenAt(body.center()))
            point = QPoint(
                body.x() + (body.width() - width) // 2 - margin,
                body.y() + body.height() + NOTICE_GAP - margin,
            )
        else:
            area = self._overlay_workarea()
            if area is None:
                return
            point = QPoint(
                area.x() + (area.width() - width) // 2 - margin,
                area.y() + W2_SCREEN_OFFSET + W2_ROW_HEIGHT + NOTICE_GAP - margin,
            )
        if area is None:
            return
        self._view.setPosition(self._clamped(point, area))

    def _selection_needs_keyboard(self) -> None:
        self.activate_keyboard(focus_text=True)

    def activate_keyboard(self, *, focus_text: bool = False) -> None:
        if self._closed or not self._open:
            return
        self._view.setFlags(WINDOW_FLAGS & ~Qt.WindowDoesNotAcceptFocus)
        self._root.setProperty("keyboardMode", True)
        self._prepare_window()
        self._resize()
        self._view.show()
        self._view.requestActivate()
        QMetaObject.invokeMethod(
            self._root, "focusText" if focus_text else "focusFirstControl", Qt.DirectConnection
        )

    def release_keyboard(self) -> None:
        if not self._root.property("keyboardMode"):
            return
        # Drop focus first: Qt refuses activeFocusOnTab=false on the focused item.
        self._root.setProperty("focus", False)
        self._root.setProperty("keyboardMode", False)
        self._view.setFlags(WINDOW_FLAGS)
        if not self._closed and self._open:
            self._prepare_window()
            self._resize()
            self._view.show()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self._view and event.type() == QEvent.Close and not self._closed:
            # A window-manager close is the same as «×».
            event.ignore()
            self.close()
            return True
        if watched is self._view and event.type() == QEvent.FocusOut:
            self.release_keyboard()
        return False

    def shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._open = False
        self._copied.stop()
        self.recording.changed.disconnect(self.refresh)
        self.recording.participantsRequested.disconnect(self.open)
        if self._overlay is not None:
            self._overlay.geometryChanged.disconnect(self._place_if_open)
        self._view.hide()
        self._x11.close()
