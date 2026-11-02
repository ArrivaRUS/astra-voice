"""GUI evidence for capture's indicator admission; never resumes capture itself."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

from PyQt5.QtCore import QObject

log = logging.getLogger(__name__)


class MeetingIndicatorGuard(QObject):
    """Checked by MeetingRecording's owned GUI timer before each controller tick."""

    def __init__(
        self,
        *,
        active: Callable[[], bool],
        overlay_visible: Callable[[], bool],
        tray_visible: Callable[[], bool],
        set_visible: Callable[[bool], None],
        pause: Callable[[], None],
        capturing: Callable[[], bool],
        identity: Callable[[], str] | None = None,
        clock: Callable[[], float] = time.monotonic,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._active = active
        self._overlay_visible, self._tray_visible = overlay_visible, tray_visible
        self._set_visible, self._pause, self._clock = set_visible, pause, clock
        # True while capture records/opens without an `indicators` pause reason.
        self._capturing = capturing
        self._identity = identity
        self._last_identity: str | None = None
        self._last_visible: bool | None = None
        self._lost_at: float | None = None
        self._pause_sent = False
        self._closed = False

    @staticmethod
    def _visible(probe: Callable[[], bool]) -> bool:
        try:
            return bool(probe())
        except Exception:
            # A destroyed window or unavailable native backend is no evidence.
            log.warning("meeting-indicator-probe-unavailable")
            return False

    def check(self) -> None:
        if self._closed:
            return
        identity = self._identity() if self._identity is not None else None
        if identity != self._last_identity:
            self._last_identity = identity
            self._last_visible = self._lost_at = None
            self._pause_sent = False
        if not self._active():
            self._last_visible = self._lost_at = None
            self._pause_sent = False
            return
        visible = self._visible(self._overlay_visible) or self._visible(self._tray_visible)
        if visible != self._last_visible:
            # Admission is revoked immediately; the pause itself has a 1 s grace.
            self._set_visible(visible)
            self._last_visible = visible
        if visible:
            self._lost_at = None
            self._pause_sent = False
        elif self._lost_at is None:
            self._lost_at = self._clock()
        elif self._pause_sent and self._capturing():
            # Capture left our pause (e.g. a manual resume) while both indicators
            # are still missing: re-arm, so the next >1 s of absence pauses again.
            self._pause_sent = False
            self._lost_at = self._clock()
        elif not self._pause_sent and self._clock() - self._lost_at > 1.0:
            self._pause()
            self._pause_sent = True

    def shutdown(self) -> None:
        self._closed = True
        # QObject remains parent-owned; release references before native UI teardown.
        self._active = self._overlay_visible = self._tray_visible = lambda: False
        self._capturing = lambda: False
        self._set_visible = lambda value: None
        self._pause = lambda: None
        self._identity = None
