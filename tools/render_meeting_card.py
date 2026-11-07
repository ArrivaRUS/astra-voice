#!/usr/bin/env python3
"""Render card A «Документ» states (E13.M4) without X11, audio or real bridges.

Example: python tools/render_meeting_card.py --out /tmp/e13m4-render-s3
Each PNG has scale 1 and the size of its reference crop: the settings-window
content area (840×552 at 1024, 716×520 at 900; card at 22/12) for
refs/05-meeting-A, the state frame (502 wide, card at 18/14) for
refs/06-meeting-states. Reference crops use QImage.copy on the unmodified
committed originals (no resampling); the manifest records source hashes,
rectangles, the card's own content height and the presentation that was shown.
Data is the mockup's (design/meetings/mockups/_p3.py LINES); M4 has no speaker
names, so «Ирина Соколова»/«Олег Ли»/«Спикер 3» are «Собеседники».
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from hashlib import sha256
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
MONO_SUBSTITUTE = "DejaVu Sans Mono"
TZ = "Europe/Moscow"  # the mockup's 10:00 is local time; fixed for reproducible PNGs

# (reference sheet, x, y, width, height, card x, card y). Measured on the committed
# originals: window content area (sidebar border +1 … status bar border −1) for 05,
# the frame's bgApp rectangle for 06 (light and dark share the geometry).
REFERENCE_BOUNDS: dict[str, tuple[str, int, int, int, int, int, int]] = {
    "ready-1024": ("05-meeting-A", 312, 292, 840, 552, 22, 12),
    "ready-900": ("05-meeting-A", 284, 1030, 716, 520, 22, 12),
    "transcribing-1024": ("05-meeting-A", 312, 2359, 840, 552, 22, 12),
    "cancelled": ("06-meeting-states", 663, 227, 502, 236, 18, 14),
    "no-model": ("06-meeting-states", 115, 565, 502, 255, 18, 14),
    "wait-memory": ("06-meeting-states", 663, 565, 502, 255, 18, 14),
    "error": ("06-meeting-states", 115, 888, 502, 255, 18, 14),
    "interrupted": ("06-meeting-states", 663, 888, 502, 255, 18, 14),
    "empty": ("06-meeting-states", 115, 1212, 502, 255, 18, 14),
}
MEETING_ID = "2026-10-09_100000-ab12"
TITLE = "Планёрка по бюджету"
# design/meetings/mockups/_p3.py LINES (time, who, text); «mark» is the microphone gap.
LINES = [
    (
        "0:00:04",
        "me",
        "Коллеги, добрый день. Я веду запись и расшифровку этой встречи для протокола.",
    ),
    ("0:00:12", "s1", "Добрый день. Тогда начнём с бюджета на четвёртый квартал."),
    (
        "0:00:21",
        "s2",
        "По подрядчикам у нас перерасход около двенадцати процентов, "
        "в основном из-за сдвига сроков.",
    ),
    ("0:00:35", "me", "Олег, это уже с учётом договора на поддержку?"),
    ("0:00:39", "s2", "Да, договор включён. Без него было бы около семи."),
    ("0:01:02", "s3", "Предлагаю перенести закупку серверов на январь — тогда укладываемся."),
    ("0:01:15", "s1", "Согласна. Олег, подготовишь пересчёт к пятнице?"),
    ("0:01:20", "s2", "Подготовлю, и сразу пришлю Марии."),
    ("0:12:41", "me", "Я вернулся, гарнитура отключилась. На чём остановились?"),
]


def _ms(clock: str) -> int:
    hours, minutes, seconds = (int(part) for part in clock.split(":"))
    return ((hours * 60 + minutes) * 60 + seconds) * 1000


def set_timezone() -> None:
    os.environ["TZ"] = TZ
    time.tzset()


def fixture_meeting(state: str = "done", **changes: Any) -> Any:
    sys.path.insert(0, str(REPO / "src"))
    from astra_voice.meetings.model import Audio, Interval, Mark, Meeting

    meeting = Meeting(
        id=MEETING_ID,
        started_at="2026-10-09T07:00:00Z",
        ended_at="2026-10-09T08:12:40Z",
        title=TITLE,
        state=state,
        variant="app_stream",
        app_class="dion",
        recorded_ms=4_360_000,
        intervals=[
            Interval(0, 0),
            Interval(1_870_000, 1_870_000),  # «Пауза 0:31:10 · 2 мин 52 с»
            Interval(2_042_000, 1_870_000),
            Interval(3_172_000, 3_000_000),  # «Пауза: экран был заблокирован 0:50:00 · 12 мин»
            Interval(3_892_000, 3_000_000),
            Interval(5_252_000, 4_360_000),
        ],
        marks=[
            Mark(723_000, "mic_gap_start"),
            Mark(760_000, "mic_gap_end"),
            Mark(1_870_000, "pause", "user"),
            Mark(3_000_000, "pause", "lock"),
            Mark(4_360_000, "pause", "stop"),
        ],
        audio=Audio(present=state != "done"),
    )
    for key, value in changes.items():
        setattr(meeting, key, value)
    return meeting


def fixture_utterances(lines: list[tuple[str, str, str]] | None = None) -> list[Any]:
    sys.path.insert(0, str(REPO / "src"))
    from astra_voice.meetings.model import Utterance

    rows = []
    for seq, (clock, who, text) in enumerate(LINES if lines is None else lines):
        start = _ms(clock)
        rows.append(
            Utterance(
                id=f"u{seq:05d}",
                seq=seq,
                track="mic" if who == "me" else "far",
                start_ms=start,
                end_ms=start + 3_000,
                text=text,
                speaker="me" if who == "me" else "others",
                final=True,
            )
        )
    return rows


def states() -> dict[str, dict[str, Any]]:
    """Name → meeting, visible replies and job snapshot for the presentation."""
    snapshot = {"percent": 40, "done_ms": 1_744_000, "total_ms": 4_360_000}
    return {
        "ready-1024": {"meeting": fixture_meeting(), "utterances": fixture_utterances()},
        "ready-900": {"meeting": fixture_meeting(), "utterances": fixture_utterances()},
        "transcribing-1024": {
            "meeting": fixture_meeting("transcribing"),
            "utterances": fixture_utterances(LINES[:7]),
            "snapshot": snapshot,
        },
        "cancelled": {"meeting": fixture_meeting("recorded", cancelled=True)},
        "no-model": {"meeting": fixture_meeting("needs_model")},
        "wait-memory": {"meeting": fixture_meeting("waiting_memory")},
        "error": {"meeting": fixture_meeting("failed"), "failure": "model"},
        "interrupted": {
            "meeting": fixture_meeting(
                "interrupted",
                started_at="2026-10-07T06:00:00Z",
                ended_at=None,
                recorded_ms=2_222_000,
                marks=[],
                intervals=[],
            )
        },
        "empty": {"meeting": fixture_meeting(), "utterances": []},
    }


def make_bridge() -> Any:
    """A QML stand-in for MeetingBridge's card API (same presentation and rows)."""
    from PyQt5.QtCore import QObject, pyqtProperty, pyqtSignal, pyqtSlot

    sys.path.insert(0, str(REPO / "src"))
    from astra_voice.meetings.model import Transcript
    from astra_voice.meetings.templates import TEMPLATES
    from astra_voice.ui import meeting_card
    from astra_voice.ui.meeting_card import CardRows, TranscriptRows

    class FakeBridge(QObject):
        changed = pyqtSignal()
        saveTemplateChanged = pyqtSignal()  # noqa: N815
        savePreviewChanged = pyqtSignal()  # noqa: N815
        renameRequested = pyqtSignal(str, str)  # noqa: N815
        renameFinished = pyqtSignal(str)  # noqa: N815
        cardSaveRequested = pyqtSignal()  # noqa: N815 — QML API

        def __init__(self) -> None:
            super().__init__()
            self._card: dict[str, Any] = {"open": False}
            self._rows = TranscriptRows(self)
            self.builder: CardRows | None = None
            self.actions: list[str] = []
            self.saves: list[str] = []
            self.renames: list[tuple[str, str]] = []
            self.rename_result = "pending"
            self.saved_templates: list[str] = []
            self._save_template = TEMPLATES[0]
            self.utterances: list[Any] = []
            self.preview_calls = 0
            self.save_error = ""
            self.save_result = "cancelled"
            self.replace_file = ""
            self.replace_result = "saved"

        def _get_card(self) -> dict[str, Any]:
            return self._card

        def _get_rows(self) -> QObject:
            return self._rows

        def _get_texts(self) -> dict[str, str]:
            return dict(meeting_card.TEXTS)

        def _get_error(self) -> str:
            return self.save_error

        def _get_toast(self) -> dict[str, Any]:
            return {"serial": 0}

        def _get_replace(self) -> str:
            return self.replace_file

        card = pyqtProperty("QVariantMap", _get_card, notify=changed)
        cardRows = pyqtProperty(QObject, _get_rows, constant=True)  # noqa: N815
        cardTexts = pyqtProperty("QVariantMap", _get_texts, constant=True)  # noqa: N815
        cardSaveError = pyqtProperty(str, _get_error, notify=changed)  # noqa: N815
        toast = pyqtProperty("QVariantMap", _get_toast, notify=changed)
        cardReplaceFile = pyqtProperty(str, _get_replace, notify=changed)  # noqa: N815

        @pyqtProperty("QVariantMap", constant=True)
        def renameTexts(self) -> dict[str, str]:  # noqa: N802
            from astra_voice.ui.meeting_texts import RENAME_TEXTS

            return dict(RENAME_TEXTS)

        @pyqtSlot(str, str, result=str)
        def renameMeeting(self, meeting_id: str, title: str) -> str:  # noqa: N802
            self.renames.append((meeting_id, title))
            return self.rename_result

        @pyqtProperty("QVariantList", constant=True)
        def saveTemplates(self) -> list[dict[str, str]]:  # noqa: N802
            return [
                {"id": key, "icon": icon, "title": title, "caption": caption}
                for key, (icon, title, caption) in zip(
                    TEMPLATES, meeting_card.TEMPLATE_TEXTS, strict=True
                )
            ]

        @pyqtProperty(str, notify=saveTemplateChanged)
        def saveTemplate(self) -> str:  # noqa: N802
            return self._save_template

        @saveTemplate.setter
        def saveTemplate(self, value: str) -> None:  # noqa: N802
            if value in TEMPLATES and value != self._save_template:
                self._save_template = value
                self.saveTemplateChanged.emit()
                self.savePreviewChanged.emit()

        @pyqtSlot(result="QVariantMap")
        def savePreview(self) -> dict[str, Any]:  # noqa: N802
            self.preview_calls += 1
            return meeting_card.save_preview(
                self._save_template,
                self.meeting,
                Transcript(meeting_id=self.meeting.id, utterances=self.utterances),
            )

        def show(
            self,
            meeting: Any,
            utterances: list[Any] | None = None,
            *,
            snapshot: dict[str, Any] | None = None,
            failure: str = "",
            policy_off: bool = False,
        ) -> None:
            self._save_template = TEMPLATES[0]
            self.utterances = list(utterances or [])
            self.builder = CardRows(meeting, self._rows)
            loaded = utterances is not None
            self.builder.load(list(utterances or []), complete=meeting.state == "done")
            self.meeting, self.snapshot = meeting, snapshot
            self.failure, self.policy_off, self.loaded = failure, policy_off, loaded
            self.refresh()
            self.saveTemplateChanged.emit()
            self.savePreviewChanged.emit()

        def refresh(self) -> None:
            builder = self.builder
            assert builder is not None
            self._card = meeting_card.presentation(
                self.meeting,
                loaded=self.loaded,
                read_error=False,
                rows=builder,
                can_transcribe=True,
                policy_off=self.policy_off,
                rename_allowed=not self.policy_off,
                failure=self.failure,
                snapshot=self.snapshot,
            )
            self.changed.emit()

        @pyqtSlot(str)
        def cardAction(self, action: str) -> None:  # noqa: N802
            self.actions.append(action)
            if action == "rename" and self._card.get("renameEnabled"):
                self.renameRequested.emit(self.meeting.id, self.meeting.title)

        @pyqtSlot(str, result=str)
        def saveAs(self, fmt: str) -> str:  # noqa: N802
            self.saves.append(fmt)
            self.saved_templates.append(self._save_template)
            self.changed.emit()  # like MeetingBridge: cardSaveError is published first
            return self.save_result

        @pyqtSlot(result=str)
        def replaceSaveTarget(self) -> str:  # noqa: N802
            self.actions.append("replace")
            self.replace_file = ""
            self.changed.emit()
            return self.replace_result

        @pyqtSlot()
        def cancelReplace(self) -> None:  # noqa: N802
            self.actions.append("keep")
            self.replace_file = ""
            self.changed.emit()

        @pyqtSlot()
        def closeMeeting(self) -> None:  # noqa: N802
            self.actions.append("close")

        @pyqtSlot()
        def retryCard(self) -> None:  # noqa: N802
            self.actions.append("retry")

        @pyqtSlot()
        def clearCardSaveError(self) -> None:  # noqa: N802
            self.save_error = ""

        @pyqtSlot()
        def openSavedFolder(self) -> None:  # noqa: N802
            self.actions.append("open-folder")

    return FakeBridge()


def reference_crops(out: Path) -> list[dict[str, Any]]:
    from PyQt5.QtGui import QImage

    rows: list[dict[str, Any]] = []
    for theme in ("light", "dark"):
        originals: dict[str, tuple[QImage, str, Path]] = {}
        for state, (sheet, x, y, width, height, _cx, _cy) in REFERENCE_BOUNDS.items():
            if sheet not in originals:
                source = REPO / f"design/meetings/refs/{sheet}-{theme}.png"
                data = source.read_bytes()
                image = QImage.fromData(data, "PNG")
                if image.isNull():
                    raise RuntimeError(f"Cannot load reference {source}")
                originals[sheet] = (image, sha256(data).hexdigest(), source)
            original, digest, source = originals[sheet]
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
                    "compare_with": f"{state}-{theme}.png",
                }
            )
        for _image, digest, source in originals.values():
            if sha256(source.read_bytes()).hexdigest() != digest:
                raise RuntimeError(f"Original reference changed during crop: {source}")
    return rows


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(item) for item in value]
    return value if isinstance(value, str | int | float | bool) or value is None else str(value)


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
    set_timezone()
    from PyQt5 import sip
    from PyQt5.QtCore import QObject, QUrl, pyqtProperty, pyqtSignal
    from PyQt5.QtGui import QFont, QFontDatabase, QGuiApplication
    from PyQt5.QtQml import QQmlApplicationEngine
    from PyQt5.QtQuick import QQuickWindow
    from PyQt5.QtTest import QTest

    sys.path.insert(0, str(REPO / "src"))
    from astra_voice.ui.icons import install_icon_provider

    QQuickWindow.setSceneGraphBackend("software")
    app = QGuiApplication.instance() or QGuiApplication([])
    assert app.platformName() == "offscreen"
    font_paths = sorted((REPO / "tests/fixtures/fonts/pt-root-ui").glob("*.ttf"))
    if not font_paths:
        raise RuntimeError("PT Root UI fixtures are missing")
    for path in font_paths:
        if QFontDatabase.addApplicationFont(str(path)) < 0:
            raise RuntimeError(f"Cannot register font: {path}")
    # PT Mono is not on the VPS; the references were measured with DejaVu Sans Mono
    # (design/meetings/tokens.md «Замер»), so the render uses the same substitute.
    QFont.insertSubstitution("PT Mono", MONO_SUBSTITUTE)

    class ThemeSource(QObject):
        changed = pyqtSignal()
        value = False
        dark = pyqtProperty(bool, lambda self: self.value, notify=changed)

    theme = ThemeSource()
    bridge = make_bridge()
    engine = QQmlApplicationEngine()
    warnings: list[str] = []
    engine.warnings.connect(lambda rows: warnings.extend(row.toString() for row in rows))
    engine.rootContext().setContextProperty("themeSource", theme)
    engine.rootContext().setContextProperty("cardBridge", bridge)
    install_icon_provider(engine)
    engine.loadData(
        b"""import QtQuick 2.15
import QtQuick.Window 2.15
import "qml"
import "qml/sections"
Window {
    id: window
    property real padX: 22
    property real padY: 12
    width: 840; height: 552
    visible: true
    Rectangle {
        anchors.fill: parent
        color: Theme.bgApp
        clip: true
        MeetingCard {
            id: card
            objectName: "renderCard"
            x: window.padX; y: window.padY
            width: window.width - window.padX * 2
            bridge: cardBridge
        }
    }
}""",
        QUrl.fromLocalFile(str(REPO / "_render-card.qml")),
    )
    if not engine.rootObjects():
        raise RuntimeError("\n".join(warnings))
    window = engine.rootObjects()[0]
    card = window.findChild(QObject, "renderCard")
    out.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    for theme_name in ("light", "dark"):
        theme.value = theme_name == "dark"
        theme.changed.emit()
        for name, fixture in states().items():
            _sheet, _x, _y, width, height, pad_x, pad_y = REFERENCE_BOUNDS[name]
            window.setProperty("padX", pad_x)
            window.setProperty("padY", pad_y)
            window.setWidth(width)
            window.setHeight(height)
            bridge.show(
                fixture["meeting"],
                fixture.get("utterances"),
                snapshot=fixture.get("snapshot"),
                failure=fixture.get("failure", ""),
            )
            QTest.qWait(80)
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
                    "card_rect": {
                        "x": pad_x,
                        "y": pad_y,
                        "width": width - pad_x * 2,
                        "content_height": card.property("implicitHeight"),
                    },
                    "presentation": _jsonable(bridge.property("card")),
                    "rows": bridge.cardRows.rowCount(),
                }
            )
    rows.extend(reference_crops(out))
    window.hide()
    sip.delete(engine)
    if warnings:
        raise RuntimeError("\n".join(warnings))
    qml = [
        REPO / "qml/sections/MeetingCard.qml",
        REPO / "qml/components/MeetingBadge.qml",
        REPO / "qml/components/MeetingTranscriptRow.qml",
        REPO / "qml/components/MeetingSaveDialog.qml",
    ]
    manifest: dict[str, Any] = {
        "scale": 1,
        "timezone": TZ,
        "qml_sha256": {
            str(path.relative_to(REPO)): sha256(path.read_bytes()).hexdigest() for path in qml
        },
        "ui_font": "PT Root UI (tests/fixtures/fonts/pt-root-ui)",
        "mono_font": f"PT Mono → {MONO_SUBSTITUTE} (as in the reference measurements)",
        "note": (
            "M4: no speaker names/numbers (M7), no rename/delete (M5); "
            "minutes are computed from the fixture replies"
        ),
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
        print(f"{row['file']}: {row['width']}x{row['height']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
