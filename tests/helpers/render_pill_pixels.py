"""Measure legacy pill pixels with an explicit QML source directory.

Compare a baseline copy of Pill.qml and the working tree in the same environment:
    python tests/helpers/render_pill_pixels.py --qml-dir /tmp/old-qml --out /tmp/old.json
    python tests/helpers/render_pill_pixels.py --qml-dir qml --out /tmp/new.json
The output contains geometry and SHA256 of raw pixels, including shadow margins.
This helper does not update any reference images or open an X11 connection.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from math import ceil
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]


def render(qml_dir: Path) -> dict[str, dict[str, int | str]]:
    for key in ("DISPLAY", "WAYLAND_DISPLAY", "QT_ACCESSIBILITY", "AT_SPI_BUS_ADDRESS"):
        os.environ.pop(key, None)
    os.environ.update(
        QT_QPA_PLATFORM="offscreen",
        QT_QUICK_BACKEND="software",
        QT_SCALE_FACTOR="1",
        DBUS_SESSION_BUS_ADDRESS="unix:path=/nonexistent",
    )
    from PyQt5.QtCore import QMetaObject, Qt, QUrl
    from PyQt5.QtGui import QFontDatabase, QGuiApplication
    from PyQt5.QtQuick import QQuickView, QQuickWindow
    from PyQt5.QtTest import QTest

    sys.path.insert(0, str(REPO / "src"))
    from astra_voice.ui.icons import install_icon_provider
    from astra_voice.ui.pill import PillState

    QQuickWindow.setSceneGraphBackend("software")
    app = QGuiApplication.instance() or QGuiApplication([])
    assert app.platformName() == "offscreen"
    for font in (REPO / "tests/fixtures/fonts").rglob("*.ttf"):
        assert QFontDatabase.addApplicationFont(str(font)) >= 0, font
    result: dict[str, dict[str, int | str]] = {}
    held: list[Any] = []
    warnings: list[str] = []
    for state in PillState:
        if state in (PillState.HIDDEN, PillState.DISABLED):
            continue
        view = QQuickView()
        install_icon_provider(view.engine())
        view.engine().warnings.connect(lambda rows: warnings.extend(row.toString() for row in rows))
        view.setColor(Qt.transparent)
        view.setResizeMode(QQuickView.SizeRootObjectToView)
        view.setSource(QUrl.fromLocalFile(str(qml_dir / "Pill.qml")))
        root = view.rootObject()
        assert root is not None, warnings
        root.setProperty("freezeAnimations", True)
        root.setProperty("avState", state.value)
        QMetaObject.invokeMethod(root, "forceLayout", Qt.DirectConnection)
        QTest.qWait(20)
        view.resize(ceil(root.property("pillWidth")) + 36, ceil(root.property("pillHeight")) + 42)
        root.setProperty("x", 18)
        root.setProperty("y", 18)
        view.show()
        QTest.qWait(200)
        image = view.grabWindow()
        if image.isNull():
            grab = view.contentItem().grabToImage()
            QTest.qWait(100)
            image = grab.image()
        assert not image.isNull(), state
        raw = image.bits()
        raw.setsize(image.byteCount())
        result[state.value] = {
            "width": image.width(),
            "height": image.height(),
            "hash": hashlib.sha256(bytes(raw)).hexdigest(),
        }
        view.hide()
        held.append(view)
    assert not warnings, warnings
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qml-dir", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    result = render(args.qml_dir.resolve())
    args.out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(f"{len(result)} pill states; warnings=0; {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
