#!/usr/bin/env python3
"""Render W2's complete visual state matrix without X11, audio or real bridges.

Example: python tools/render_meeting_overlay.py --out /tmp/e13m3-render-s1b
State PNGs have scale 1 and full 18/18/18/24 px shadow margins. The manifest
records body rectangles separately. Reference crops use QImage.copy on the
unmodified committed originals, preserving scale 1 without any resampling;
source hashes, bounds and state correspondence are recorded in the manifest.
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

# Original design/meetings/refs/04-recording-{theme}.png, section
# «3 · Состояния плашки W2». Rectangles contain the body, without its shadow.
# These are measurements of committed references; never regenerate the originals.
REFERENCE_BOUNDS = {
    "recording": (399, 4404, 482, 36),
    "paused": (408, 4541, 464, 36),
    "compact": (487, 4677, 142, 36),
    "compact-paused": (652, 4677, 142, 36),
    "limit-soon": (399, 4814, 482, 71),
    "mic-paused": (399, 4985, 482, 96),
    "mic-long-name": (399, 5181, 482, 120),
    "app-closed": (399, 5596, 482, 63),
    "stopping": (559, 5760, 162, 36),
    "error": (400, 5896, 480, 36),
    "paused-lock": (408, 6033, 464, 64),
    "reason-indicator": (399, 6197, 482, 83),
    "reason-hang": (399, 6380, 482, 83),
    "notify-fallback": (399, 6563, 482, 64),
}


def reference_crops(out: Path) -> list[dict[str, Any]]:
    """Copy measured rectangles from the read-only original PNGs using Qt only."""
    from PyQt5.QtGui import QImage

    rows: list[dict[str, Any]] = []
    for theme in ("light", "dark"):
        source = REPO / f"design/meetings/refs/04-recording-{theme}.png"
        source_bytes = source.read_bytes()
        digest = sha256(source_bytes).hexdigest()
        original = QImage.fromData(source_bytes, "PNG")
        if original.isNull():
            raise RuntimeError(f"Cannot load reference {source}")
        for state, bounds in REFERENCE_BOUNDS.items():
            x, y, width, height = bounds
            if x < 0 or y < 0 or x + width > original.width() or y + height > original.height():
                raise RuntimeError(f"Reference crop out of bounds: {state} {bounds}")
            filename = f"{state}-ref-{theme}.png"
            if not original.copy(*bounds).save(str(out / filename), "PNG"):
                raise RuntimeError(f"Cannot write reference crop {filename}")
            actual = {
                "compact": "w2-recording-compact",
                "compact-paused": "w2-paused-compact",
                "mic-paused": "w2-mic-paused-full",
            }.get(state, f"w2-{state}-full")
            rows.append(
                {
                    "file": filename,
                    "width": width,
                    "height": height,
                    "kind": "reference-crop",
                    "scale": 1,
                    "source": str(source.relative_to(REPO)),
                    "source_sha256": digest,
                    "source_dimensions": {"width": original.width(), "height": original.height()},
                    "source_rect": {"x": x, "y": y, "width": width, "height": height},
                    "surface_rect": {"x": 0, "y": 0, "width": width, "height": height},
                    "compare_with": f"{actual}-{theme}.png",
                }
            )
        if sha256(source.read_bytes()).hexdigest() != digest:
            raise RuntimeError(f"Original reference changed during crop: {source}")
    return rows


def presentations() -> dict[str, dict[str, Any]]:
    base: dict[str, Any] = {
        "visible": True,
        "state": "recording",
        "elapsed": "0:42:10",
        "secondary": "",
        "secondaryAction": "",
        "pausedMinutes": 3,
        "canResume": True,
        "canExtend": False,
        "microphoneName": "",
        "timerTooltip": "На паузе 3 мин. Через 60 мин паузы встреча сохранится",
    }
    cases: dict[str, dict[str, Any]] = {
        "recording": {},
        "paused": {"state": "paused"},
        "paused-lock": {
            "state": "paused-lock",
            "secondary": "Экран был заблокирован — продолжите вручную",
        },
        "paused-indicator": {
            "state": "paused-indicator",
            "secondary": "Индикатор записи не отображается",
            "canResume": False,
        },
        "paused-hang": {
            "state": "paused-hang",
            "secondary": "Программа не отвечала",
        },
        "mic-paused": {
            "state": "mic-paused",
            "elapsed": "0:12:40",
            "secondary": "Ваш голос не записывается: микрофон сменился",
            "secondaryAction": "resume-microphone",
            "microphoneName": "Встроенный микрофон",
        },
        "mic-long-name": {
            "state": "mic-paused",
            "elapsed": "0:12:40",
            "secondary": (
                "Ваш голос не записывается: микрофон сменился на «Гарнитура Jabra Evolve2 65 "
                "USB Headset Microphone»"
            ),
            "secondaryAction": "resume-microphone",
            "microphoneName": "Гарнитура Jabra Evolve2 65 USB Headset Microphone",
        },
        "limit-soon": {
            "state": "limit-soon",
            "elapsed": "1:55:12",
            "secondary": "Остановится через 4:48",
            "secondaryAction": "extend",
            "canExtend": True,
        },
        "limit-ceiling": {
            "state": "limit-soon",
            "elapsed": "3:55:12",
            "secondary": "Остановится через 4:48",
        },
        "app-closed": {
            "state": "app-closed",
            "elapsed": "1:05:21",
            "secondary": "DION закрылся — пишется только микрофон",
        },
        "reason-indicator": {
            "elapsed": "0:51:16",
            "secondary": (
                "Запись стояла на паузе 0:00:14: индикатор записи не отображался. "
                "Отметка — в расшифровке"
            ),
        },
        "reason-hang": {
            "elapsed": "0:51:16",
            "secondary": (
                "Запись стояла на паузе 0:00:14: программа не отвечала. Отметка — в расшифровке"
            ),
        },
        "notify-fallback": {
            "state": "notify-fallback",
            "elapsed": "0:51:02",
            "secondary": "Встреча сохранена. Делаю расшифровку…",
        },
        "stopping": {"state": "stopping"},
        "error": {
            "state": "error",
            "secondary": ("Запись остановлена: звуковая служба не отвечает. Записанное сохранено"),
        },
    }
    return {name: {**base, **value} for name, value in cases.items()}


def render(out: Path) -> dict[str, Any]:
    # This utility must remain harmless even when launched from a desktop shell.
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
    from PyQt5.QtGui import QFontDatabase, QFontInfo, QGuiApplication, QImage, QPainter
    from PyQt5.QtQml import QQmlApplicationEngine
    from PyQt5.QtQuick import QQuickWindow
    from PyQt5.QtTest import QTest

    sys.path.insert(0, str(REPO / "src"))
    from astra_voice.ui.icons import install_icon_provider
    from astra_voice.ui.meeting_texts import W2_TEXTS

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
        b"""import QtQuick 2.15
import QtQuick.Window 2.15
import "qml"
Window {
    width: Math.ceil(overlay.pillWidth) + 36
    height: Math.ceil(overlay.pillHeight) + 42
    color: "transparent"
    visible: true
    MeetingOverlay { id: overlay; x: 18; y: 18; freezeAnimations: true }
}""",
        QUrl.fromLocalFile(str(REPO / "_render-w2.qml")),
    )
    if not engine.rootObjects():
        raise RuntimeError("\n".join(warnings))
    window = engine.rootObjects()[0]
    root = window.findChild(QObject, "meetingOverlay")
    root.setProperty("texts", dict(W2_TEXTS))
    out.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    images: dict[str, list[tuple[str, Any]]] = {"light": [], "dark": []}
    for theme_name in images:
        theme.value = theme_name == "dark"
        theme.changed.emit()
        for name, presentation in presentations().items():
            for compact in (False, True):
                root.setProperty("presentation", presentation)
                root.setProperty("compact", compact)
                QMetaObject.invokeMethod(root, "forceLayout", Qt.DirectConnection)
                QTest.qWait(80)
                width = ceil(root.property("pillWidth")) + 36
                height = ceil(root.property("pillHeight")) + 42
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
                    raise RuntimeError(
                        f"Invalid image {name}/{theme_name}: "
                        f"{image.width()}x{image.height()}, expected {width}x{height}"
                    )
                if warnings:
                    raise RuntimeError("\n".join(warnings))
                variant = "compact" if compact else "full"
                filename = f"w2-{name}-{variant}-{theme_name}.png"
                if not image.save(str(out / filename), "PNG"):
                    raise RuntimeError(f"Cannot write {filename}")
                images[theme_name].append((f"{name} · {variant}", image))
                rows.append(
                    {
                        "file": filename,
                        "width": width,
                        "height": height,
                        "surface_rect": {
                            "x": 18,
                            "y": 18,
                            "width": ceil(root.property("pillWidth")),
                            "height": ceil(root.property("pillHeight")),
                        },
                        "logical_surface_size": {
                            "width": root.property("pillWidth"),
                            "height": root.property("pillHeight"),
                        },
                        "presentation": presentation,
                    }
                )
    font = QFontInfo(root.findChild(QObject, "meetingTimer").property("font"))
    manifest: dict[str, Any] = {
        "scale": 1,
        "source_revision": "working tree; see captured qml_sha256",
        "qml_sha256": {
            str(path.relative_to(REPO)): sha256(path.read_bytes()).hexdigest()
            for path in [
                REPO / "qml/MeetingOverlay.qml",
                REPO / "qml/components/MeetingOverlayButton.qml",
                REPO / "qml/components/OverlaySurface.qml",
            ]
        },
        "ui_font": "PT Root UI (tests/fixtures/fonts/pt-root-ui)",
        "timer_font": font.family(),
        "timer_fixed_pitch": font.fixedPitch(),
        "warnings": warnings,
        "pngs": rows,
    }
    for name, frames in images.items():
        board = QImage(1200, len(presentations()) * 175 + 20, QImage.Format_RGB32)
        board.fill(Qt.black if name == "dark" else Qt.lightGray)
        painter = QPainter(board)
        painter.setPen(Qt.white if name == "dark" else Qt.black)
        for index, (label, image) in enumerate(frames):
            x, y = 15 + (index % 2) * 600, 20 + (index // 2) * 175
            painter.drawText(x, y, label)
            painter.drawImage(x, y + 5, image)
        painter.end()
        filename = f"w2-contact-{name}.png"
        if not board.save(str(out / filename), "PNG"):
            raise RuntimeError(f"Cannot write {filename}")
        rows.append(
            {
                "file": filename,
                "width": board.width(),
                "height": board.height(),
                "kind": "contact-sheet",
                "surface_rects": [
                    {
                        "file": rows[index + (0 if name == "light" else len(frames))]["file"],
                        "x": 33 + (index % 2) * 600,
                        "y": 43 + (index // 2) * 175,
                        "width": rows[index + (0 if name == "light" else len(frames))][
                            "surface_rect"
                        ]["width"],
                        "height": rows[index + (0 if name == "light" else len(frames))][
                            "surface_rect"
                        ]["height"],
                    }
                    for index in range(len(frames))
                ],
            }
        )
    rows.extend(reference_crops(out))
    window.hide()
    sip.delete(engine)
    if warnings:
        raise RuntimeError("\n".join(warnings))
    (out / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True, help="Destination for PNGs and manifest")
    options = parser.parse_args()
    manifest = render(options.out.resolve())
    print(
        f"{len(manifest['pngs'])} PNG; scale=1; timer={manifest['timer_font']}; "
        f"fixed_pitch={manifest['timer_fixed_pitch']}; warnings={len(manifest['warnings'])}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
