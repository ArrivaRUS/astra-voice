"""Shared preloaded transparent overlay window foundation."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from PyQt5.QtCore import QObject, QPoint, QRect, Qt, QUrl
from PyQt5.QtGui import QGuiApplication, QScreen
from PyQt5.QtQuick import QQuickView

from astra_voice.ui.icons import install_icon_provider

log = logging.getLogger(__name__)

SHADOW_MARGIN = 18
SHADOW_OFFSET_Y = 6
WINDOW_FLAGS = (
    Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.WindowDoesNotAcceptFocus
)


class OverlayWindow(QObject):
    """Own a native view and its QML surface without activating another window."""

    def _load_overlay(
        self,
        qml_url: QUrl,
        *,
        view_factory: Callable[[], Any] | None = None,
        icon_provider: Callable[[Any], None] = install_icon_provider,
        window_flags: Any = WINDOW_FLAGS,
    ) -> None:
        factory = view_factory if view_factory is not None else QQuickView
        self._view = factory()
        icon_provider(self._view.engine())
        self._view.installEventFilter(self)
        self.destroyed.connect(self._view.deleteLater)
        self._view.setFlags(window_flags)
        self._view.setColor(Qt.transparent)
        self._view.setResizeMode(QQuickView.SizeRootObjectToView)
        self._view.setSource(qml_url)
        self._root = self._view.rootObject()
        if self._root is None:
            raise RuntimeError("Overlay QML failed to load")

    def _screen_application(self) -> Any:
        return QGuiApplication

    def _active_screen(self) -> QScreen | None:
        application = self._screen_application()
        try:
            active = self._x11.active_window()
            geometry = self._x11.window_geometry(active) if active else None
            if geometry is not None:
                x, y, width, height = geometry
                screen = application.screenAt(QPoint(x + width // 2, y + height // 2))
                if screen is not None:
                    return screen
        except Exception as exc:
            log.debug("Пилюля: экран активного окна недоступен: %s", exc)
        return application.primaryScreen()

    def _net_workarea(self) -> QRect | None:
        # TODO: перейти на platform/x11, когда соседняя зона добавит чтение _NET_WORKAREA
        if self._x11.d is None:
            return None
        try:
            from Xlib import Xatom

            conn = self._x11.d
            atom = conn.intern_atom("_NET_WORKAREA", only_if_exists=True)
            prop = self._x11.root.get_full_property(atom, Xatom.CARDINAL) if atom else None
            if prop is None or prop.format != 32 or len(prop.value) < 4:
                return None
            desktop_atom = conn.intern_atom("_NET_CURRENT_DESKTOP", only_if_exists=True)
            desktop = (
                self._x11.root.get_full_property(desktop_atom, Xatom.CARDINAL)
                if desktop_atom
                else None
            )
            index = 0
            if desktop is not None and desktop.format == 32 and len(desktop.value):
                index = int(desktop.value[0]) * 4
            if index < 0 or index + 4 > len(prop.value):
                return None
            x, y, width, height = (int(v) for v in prop.value[index : index + 4])
            return QRect(x, y, width, height) if width > 0 and height > 0 else None
        except Exception as exc:
            log.debug("Пилюля: _NET_WORKAREA недоступна: %s", exc)
            return None

    def _overlay_workarea(self, screen: QScreen | None = None) -> QRect | None:
        screen = self._active_screen() if screen is None else screen
        if screen is None:
            return None
        area = screen.availableGeometry()
        workarea = self._net_workarea()
        if workarea is not None:
            workarea = workarea.intersected(screen.geometry())
            if not workarea.isEmpty():
                area = workarea
        return area
