#!/usr/bin/env python3
"""Render W3 «Предупредите участников» states without X11, audio or real bridges.

Example: python tools/render_meeting_notice.py --out /tmp/e13m3-render-s3
PNGs have scale 1 and the full 36/36/36/50 px shadow margins of shadow.notify;
the manifest records the card rectangle without shadow. Reference crops use
QImage.copy on the unmodified committed originals (no resampling).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from hashlib import sha256
from math import ceil
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
MARGIN = 36
OFFSET_Y = 14

# design/meetings/refs/04-recording-{theme}.png, section «4 · Карточка
# «Предупредите участников» W3». Card body with its 1 px border, without shadow;
# measured from the notifyBorder pixels of the committed originals.
REFERENCE_BOUNDS = {
    "default": (196, 6906, 340, 265),
    "copied": (744, 6906, 340, 265),
    "admin": (196, 7272, 340, 266),
    "copy-failed": (744, 7272, 340, 283),
}
DEFAULT_TEXT = "Я веду запись и расшифровку этой встречи для протокола."
# The administrator text shown by the reference mockup.
ADMIN_TEXT = "Встреча записывается для протокола."


def presentations() -> dict[str, dict[str, Any]]:
    sys.path.insert(0, str(REPO / "src"))
    from astra_voice.ui.meeting_texts import notice_mute_text

    base = {
        "visible": True,
        "text": DEFAULT_TEXT,
        "admin": False,
        "copyState": "default",
        "muteText": notice_mute_text("dion"),
    }
    return {
        "default": base,
        "copied": {**base, "copyState": "copied"},
        "admin": {**base, "text": ADMIN_TEXT, "admin": True},
        "copy-failed": {**base, "copyState": "failed"},
    }


def reference_crops(out: Path) -> list[dict[str, Any]]:
    from PyQt5.QtGui import QImage

    rows: list[dict[str, Any]] = []
    for theme in ("light", "dark"):
        source = REPO / f"design/meetings/refs/04-recording-{theme}.png"
        source_bytes = source.read_bytes()
        digest = sha256(source_bytes).hexdigest()
        original = QImage.fromData(source_bytes, "PNG")
        if original.isNull():
            raise RuntimeError(f"Cannot load reference {source}")
        for state, (x, y, width, height) in REFERENCE_BOUNDS.items():
            if x < 0 or y < 0 or x + width > original.width() or y + height > original.height():
                raise RuntimeError(f"Reference crop out of bounds: {state}")
            filename = f"{state}-ref-{theme}.png"
            if not original.copy(x, y, width, height).save(str(out / filename), "PNG"):
                raise RuntimeError(f"Cannot write reference crop {filename}")
            rows.append(
                {
                    "file": filename,
                    "kind": "reference-crop",
                    "scale": 1,
                    "width": width,
                    "height": height,
                    "source": str(source.relative_to(REPO)),
                    "source_sha256": digest,
                    "source_rect": {"x": x, "y": y, "width": width, "height": height},
                    "surface_rect": {"x": 0, "y": 0, "width": width, "height": height},
                    "compare_with": f"{state}-{theme}.png",
                }
            )
        if sha256(source.read_bytes()).hexdigest() != digest:
            raise RuntimeError(f"Original reference changed during crop: {source}")
    return rows


def render(out: Path) -> dict[str, Any]:
    for key in ("DISPLAY", "WAYLAND_DISPLAY", "QT_ACCESSIBILITY", "AT_SPI_BUS_ADDRESS"):
        os.environ.pop(key, None)
    os.environ.update(
        QT_QPA_PLATFORM="offscreen",
        QT_QUICK_BACKEND="software",
        QT_SCALE_FACTOR="1",
        QT_AUTO_SCREEN_SCALE_FACTOR="0",
        DBUS_SESSION_BUS_ADDRESS="unix:path=/nonexistent",
    )
    from PyQt5 import sip
    from PyQt5.QtCore import QMetaObject, QObject, Qt, QUrl, pyqtProperty, pyqtSignal
    from PyQt5.QtGui import QFontDatabase, QGuiApplication
    from PyQt5.QtQml import QQmlApplicationEngine
    from PyQt5.QtQuick import QQuickWindow
    from PyQt5.QtTest import QTest

    sys.path.insert(0, str(REPO / "src"))
    from astra_voice.ui.icons import install_icon_provider
    from astra_voice.ui.meeting_texts import W3_TEXTS

    QQuickWindow.setSceneGraphBackend("software")
    app = QGuiApplication.instance() or QGuiApplication([])
    assert app.platformName() == "offscreen"
    font_paths = sorted((REPO / "tests/fixtures/fonts/pt-root-ui").glob("*.ttf"))
    if not font_paths:
        raise RuntimeError("PT Root UI fixtures are missing")
    for path in font_paths:
        if QFontDatabase.addApplicationFont(str(path)) < 0:
            raise RuntimeError(f"Cannot register font: {path}")

    class ThemeSource(QObject):
        changed = pyqtSignal()
        value = False
        dark = pyqtProperty(bool, lambda self: self.value, notify=changed)

    theme = ThemeSource()
    engine = QQmlApplicationEngine()
    warnings: list[str] = []
    engine.warnings.connect(lambda rows: warnings.extend(row.toString() for row in rows))
    engine.rootContext().setContextProperty("themeSource", theme)
    install_icon_provider(engine)
    engine.loadData(
        f"""import QtQuick 2.15
import QtQuick.Window 2.15
import "qml"
Window {{
    width: Math.ceil(notice.surfaceWidth) + {MARGIN * 2}
    height: Math.ceil(notice.surfaceHeight) + {MARGIN * 2 + OFFSET_Y}
    color: "transparent"
    visible: true
    MeetingNotice {{ id: notice; x: {MARGIN}; y: {MARGIN} }}
}}""".encode(),
        QUrl.fromLocalFile(str(REPO / "_render-w3.qml")),
    )
    if not engine.rootObjects():
        raise RuntimeError("\n".join(warnings))
    window = engine.rootObjects()[0]
    root = window.findChild(QObject, "meetingNotice")
    root.setProperty("texts", dict(W3_TEXTS))
    out.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    for theme_name in ("light", "dark"):
        theme.value = theme_name == "dark"
        theme.changed.emit()
        for name, presentation in presentations().items():
            root.setProperty("presentation", presentation)
            QMetaObject.invokeMethod(root, "forceLayout", Qt.DirectConnection)
            QTest.qWait(80)
            width = ceil(root.property("surfaceWidth")) + MARGIN * 2
            height = ceil(root.property("surfaceHeight")) + MARGIN * 2 + OFFSET_Y
            image = window.grabWindow()
            grab = None
            for _attempt in range(20):
                if not image.isNull() and (image.width(), image.height()) == (width, height):
                    break
                if grab is None:
                    grab = window.contentItem().grabToImage()
                QTest.qWait(25)
                image = grab.image()
            else:
                raise RuntimeError(f"Invalid image {name}/{theme_name}")
            if warnings:
                raise RuntimeError("\n".join(warnings))
            filename = f"{name}-{theme_name}.png"
            if not image.save(str(out / filename), "PNG"):
                raise RuntimeError(f"Cannot write {filename}")
            rows.append(
                {
                    "file": filename,
                    "kind": "render",
                    "scale": 1,
                    "width": width,
                    "height": height,
                    "surface_rect": {
                        "x": MARGIN,
                        "y": MARGIN,
                        "width": ceil(root.property("surfaceWidth")),
                        "height": ceil(root.property("surfaceHeight")),
                    },
                    "logical_surface_size": {
                        "width": root.property("surfaceWidth"),
                        "height": root.property("surfaceHeight"),
                    },
                    "presentation": presentation,
                }
            )
    rows.extend(reference_crops(out))
    window.hide()
    sip.delete(engine)
    if warnings:
        raise RuntimeError("\n".join(warnings))
    manifest: dict[str, Any] = {
        "scale": 1,
        "qml_sha256": {
            str(path.relative_to(REPO)): sha256(path.read_bytes()).hexdigest()
            for path in [REPO / "qml/MeetingNotice.qml", REPO / "qml/components/OverlaySurface.qml"]
        },
        "ui_font": "PT Root UI (tests/fixtures/fonts/pt-root-ui)",
        "warnings": warnings,
        "pngs": rows,
    }
    (out / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True, help="Destination for PNGs and manifest")
    options = parser.parse_args()
    manifest = render(options.out.resolve())
    for row in manifest["pngs"]:
        rect = row["surface_rect"]
        print(f"{row['file']}: {rect['width']}x{rect['height']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
