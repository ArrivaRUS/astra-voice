#!/usr/bin/env python3
"""Render W1 PNGs and a card-geometry manifest with no X11 or recording.

Example: python tools/render_meeting_offer.py --out /tmp/e13m8-render-s5v
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

REPO = Path(__file__).resolve().parents[1]
STATES = (
    ("call-app", "call-app", 42),
    ("call-browser", "call-browser", 51),
    ("call-app-60", "call-app", 60),
    ("hover-start", "call-app", 42),
    ("starting", "call-app", 37),
    ("start-failed", "call-app", 42),
)


def render(out: Path, only: list[str] | None = None) -> list[dict[str, Any]]:
    for key in (
        "DISPLAY",
        "WAYLAND_DISPLAY",
        "QT_ACCESSIBILITY",
        "AT_SPI_BUS_ADDRESS",
        "ASTRA_VOICE_RESOURCES",
    ):
        os.environ.pop(key, None)
    os.environ.update(
        QT_QPA_PLATFORM="offscreen",
        QT_QUICK_BACKEND="software",
        QT_SCALE_FACTOR="1",
        QT_AUTO_SCREEN_SCALE_FACTOR="0",
        DBUS_SESSION_BUS_ADDRESS="unix:path=/nonexistent",
    )
    from PyQt5 import sip
    from PyQt5.QtCore import (
        QCoreApplication,
        QEvent,
        QEventLoop,
        QObject,
        QPoint,
        QPointF,
        QTimer,
        pyqtProperty,
        pyqtSignal,
    )
    from PyQt5.QtGui import QFontDatabase, QGuiApplication
    from PyQt5.QtQml import QQmlEngine, QQmlExpression
    from PyQt5.QtQuick import QQuickItem, QQuickView, QQuickWindow
    from PyQt5.QtTest import QTest

    sys.path.insert(0, str(REPO / "src"))
    from astra_voice.core.meeting_session import ERROR_TEXT
    from astra_voice.ui.meeting_offer import OFFER_GUARD_MS
    from astra_voice.ui.meeting_offer_window import MeetingOffer

    QQuickWindow.setSceneGraphBackend("software")
    app = QGuiApplication.instance() or QGuiApplication([])
    if app.platformName() != "offscreen":
        raise RuntimeError("W1 rendering requires offscreen Qt")
    fonts = sorted((REPO / "tests/fixtures/fonts/pt-root-ui").glob("*.ttf"))
    if not fonts or any(QFontDatabase.addApplicationFont(str(path)) < 0 for path in fonts):
        raise RuntimeError("Cannot register the PT Root UI fixture fonts")

    class ThemeSource(QObject):
        changed = pyqtSignal()
        value = False
        dark = pyqtProperty(bool, lambda self: self.value, notify=changed)

    theme = ThemeSource()
    warnings: list[str] = []

    def create_view() -> QQuickView:
        view = QQuickView()
        view.engine().rootContext().setContextProperty("themeSource", theme)
        view.engine().warnings.connect(lambda rows: warnings.extend(row.toString() for row in rows))
        return view

    now_ms = 0
    recording = SimpleNamespace(offer=SimpleNamespace(now_ms=lambda: now_ms))
    window = MeetingOffer(recording, cursor_pos=QPoint, view_factory=create_view)
    view, root = window._view, window._root
    root.setProperty("freezeAnimations", True)
    out.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    try:
        for state, kind, seconds in STATES:
            if only is not None and state not in only:
                continue
            window.show(kind, 60)
            window.update_remaining(seconds)
            progress = root.findChild(QQuickItem, "offerProgress")
            progress.setProperty("fraction", seconds / 60)
            if state == "starting":
                window.starting()
            elif state == "start-failed":
                window.start_failed(ERROR_TEXT["disk-low"])
                # Capture the settled window. Its card is already at the final height.
                now_ms += OFFER_GUARD_MS
                window._release_failure_input()
            for theme_name in ("light", "dark"):
                theme.value = theme_name == "dark"
                theme.changed.emit()
                app.processEvents()
                dark = QQmlExpression(QQmlEngine.contextForObject(root), root, "Theme.dark")
                if dark.evaluate()[0] != theme.value or dark.hasError():
                    raise RuntimeError("The view did not pick up the selected theme")
                start = root.findChild(QQuickItem, "offerStart")
                hover = state == "hover-start"
                cursor_position = (
                    start.mapToScene(QPointF(start.width() / 2, start.height() / 2)).toPoint()
                    if hover
                    else QPoint(1, 1)
                )
                QTest.mouseMove(view, cursor_position)
                if start.property("hovered") != hover:
                    raise RuntimeError(f"Unexpected hover state in {state}/{theme_name}")
                image = view.grabWindow()
                if image.isNull() or image.size() != view.size():
                    # Offscreen plugins can lack a native backing image.
                    grab = view.contentItem().grabToImage()
                    if grab is not None:
                        loop = QEventLoop()
                        grab.ready.connect(loop.quit)
                        QTimer.singleShot(1000, loop.quit)
                        loop.exec_()
                        image = grab.image()
                if image.isNull() or image.size() != view.size():
                    raise RuntimeError(f"Cannot grab the view in {state}/{theme_name}")
                name = f"{state}-{theme_name}.png"
                if not image.save(str(out / name), "PNG"):
                    raise RuntimeError(f"Cannot write {out / name}")
                card_width, card_height = window._surface_size()
                rows.append(
                    {
                        "file": name,
                        "kind": kind,
                        "seconds": seconds,
                        "hover": hover,
                        "scale": 1,
                        "width": image.width(),
                        "height": image.height(),
                        "surface_rect": {
                            "x": window._shadow_margin,
                            "y": window._shadow_margin,
                            "width": card_width,
                            "height": card_height,
                        },
                    }
                )
            window.close()
        if warnings:
            raise RuntimeError("\n".join(warnings))
    finally:
        window.shutdown()
        sip.delete(window)
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)

    (out / "manifest.json").write_text(
        json.dumps({"ui_font": "PT Root UI", "pngs": rows}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True, help="Destination for PNGs and manifest")
    parser.add_argument("--only", nargs="+", choices=[state for state, _, _ in STATES])
    args = parser.parse_args()
    for row in render(args.out.resolve(), args.only):
        print(f"{row['file']}: {row['width']}x{row['height']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
