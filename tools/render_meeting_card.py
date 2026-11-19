#!/usr/bin/env python3
"""Render card A «Документ» states (E13.M4) without X11, audio or real bridges.

Example: python tools/render_meeting_card.py --out /tmp/e13m4-render-s3
Each PNG has scale 1. Full 05/08 windows use the reference viewport; 06 state
fragments are recorded separately from the actual product viewport. Reference
crops use QImage.copy on the unmodified committed originals (no resampling).
The manifest records source hashes, rectangles, comparison scope, card geometry,
focus and the presentation shown. Fixtures use _p3/_p4/_p6 mockup data; D4 uses
its own literal d4_echo sample without changing the application's export logic.
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
# Crops measured from committed PNG boundaries, without resampling.
REFERENCE_BOUNDS.update(
    {
        "names-edit": ("05-meeting-A", 374, 1634, 716, 520, 22, 12),
        "merge": ("05-meeting-A", 312, 3700, 840, 552, 22, 12),
        "merged": ("05-meeting-A", 312, 4336, 840, 552, 22, 12),
        "room": ("06-meeting-states", 115, 1663, 502, 241, 18, 14),
        "no-diarization": ("06-meeting-states", 663, 1663, 502, 471, 18, 14),
        "policy-off": ("06-meeting-states", 663, 1212, 502, 382, 18, 14),
        "models-missing": ("09-diar-install", 312, 4605, 840, 416, 22, 12),
        "marks-1024": ("08-marks", 312, 311, 840, 778, 22, 12),
        "marks-900": ("08-marks", 374, 2271, 716, 765, 22, 12),
        "save-echo": ("08-marks", 220, 5109, 840, 552, 22, 12),
        "save-echo-checked": ("08-marks", 220, 5640, 840, 552, 22, 12),
    }
)
# 09: full-sheet coordinates, 1280 × 8086. Content fragments exclude shadows.
# §2 crops share card border y=39 despite the fractional HTML frame origins.
REFERENCE_BOUNDS.update(
    {
        "diar-missing": ("09-diar-install", 100, 5198, 840, 130, 22, 14),
        "diar-downloading": ("09-diar-install", 100, 5375, 840, 131, 22, 14),
        "diar-installed": ("09-diar-install", 100, 5932, 840, 122, 22, 14),
        "diar-failed-admin-900": ("09-diar-install", 100, 6719, 716, 161, 22, 14),
        "section-group-missing": ("09-diar-install", 312, 407, 840, 287, 22, 0),
        "section-missing": ("09-diar-install", 100, 891, 840, 155, 22, 4),
        "section-downloading": ("09-diar-install", 100, 1117, 840, 164, 22, 4),
        "section-installed": ("09-diar-install", 100, 1847, 840, 155, 22, 4),
        "section-failed-admin": ("09-diar-install", 100, 2501, 840, 155, 22, 4),
        "section-offline": ("09-diar-install", 100, 3809, 840, 155, 22, 4),
    }
)
# 10: measured against both complete 1280 × 10087 sheets. Rejected B/B′
# proposals are deliberately excluded. Fragments retain their literal origin.
REFERENCE_BOUNDS.update(
    {
        "redo-failed-1024": ("10-rediarize", 312, 329, 840, 552, 22, 12),
        "redo-failed-900": ("10-rediarize", 374, 1087, 716, 520, 22, 12),
        "redo-failed": ("10-rediarize", 100, 1728, 840, 149, 22, 14),
        "redo-room": ("10-rediarize", 100, 1941, 840, 149, 22, 14),
        "redo-queued": ("10-rediarize", 100, 2154, 840, 138, 22, 14),
        "redo-wait-memory": ("10-rediarize", 100, 2356, 840, 139, 22, 14),
        "redo-running": ("10-rediarize", 100, 2559, 840, 139, 22, 14),
        "redo-reconcile": ("10-rediarize", 100, 2762, 840, 139, 22, 14),
        "redo-cancelled": ("10-rediarize", 100, 2965, 840, 138, 22, 14),
        "redo-failed-again": ("10-rediarize", 100, 3167, 840, 149, 22, 14),
        "redo-no-audio": ("10-rediarize", 100, 3380, 840, 130, 22, 14),
        "redo-done": ("10-rediarize", 100, 3574, 840, 130, 22, 14),
        "redo-running-900": ("10-rediarize", 100, 3768, 716, 145, 22, 14),
        "redo-failed-again-900": ("10-rediarize", 100, 3933, 716, 169, 22, 14),
        "redo-running-1024": ("10-rediarize", 312, 4197, 840, 552, 22, 12),
        "redo-menu": ("10-rediarize", 284, 4976, 840, 552, 22, 12),
        "redo-dialog": ("10-rediarize", 430, 5586, 420, 307, 0, 0),
        "redo-redo-failed": ("10-rediarize", 100, 5993, 840, 130, 22, 14),
        "redo-offer": ("10-rediarize", 100, 7794, 840, 209, 22, 4),
        "redo-offer-installed": ("10-rediarize", 100, 8042, 840, 138, 22, 14),
        "redo-first": ("10-rediarize", 100, 8219, 840, 139, 22, 14),
        "redo-before-keep": ("10-rediarize", 100, 8517, 840, 219, 22, 4),
        "redo-kept-deleted": ("10-rediarize", 100, 8775, 840, 199, 22, 4),
        "redo-kept-7": ("10-rediarize", 100, 9038, 840, 209, 22, 4),
        "redo-kept-30": ("10-rediarize", 100, 9311, 840, 209, 22, 4),
    }
)
# Match product content only, excluding the HTML stand's rounded frame/shadow.
# Rendering retains the original viewport so control widths and wrapping cannot
# be changed by a crop. Only the final image and reference rectangle are inset.
REDO_RENDER_BOUNDS = {
    name: bounds for name, bounds in REFERENCE_BOUNDS.items() if name.startswith("redo-")
}
REDO_FULL_VIEWS = {
    "redo-failed-1024",
    "redo-failed-900",
    "redo-running-1024",
    "redo-menu",
    "redo-dialog",
}
REDO_HEADER_FRAGMENTS = {
    "redo-offer",
    "redo-before-keep",
    "redo-kept-deleted",
    "redo-kept-7",
    "redo-kept-30",
}
REDO_CROP_INSETS = {
    name: (8, 2 if name in REDO_HEADER_FRAGMENTS else 8, 8, 8)
    for name in REDO_RENDER_BOUNDS
    if name not in REDO_FULL_VIEWS
}
for _name, (_left, _top, _right, _bottom) in REDO_CROP_INSETS.items():
    _sheet, _x, _y, _w, _h, _cx, _cy = REFERENCE_BOUNDS[_name]
    REFERENCE_BOUNDS[_name] = (
        _sheet,
        _x + _left,
        _y + _top,
        _w - _left - _right,
        _h - _top - _bottom,
        _cx - _left,
        _cy - _top,
    )

# No corresponding frame exists in the committed reference. Dimensions describe
# the requested product viewport; the manifest explicitly records the missing crop.
MISSING_REFERENCES = {
    "diarization-failed": (840, 552, "06 has no failed-separation frame"),
    "save-statistics": (840, 620, "08 contains no statistics preview frame"),
}

# 06 shows state fragments; render the product at its real content width,
# and compare only the common header/banner/table fragment, not whole frames.
RENDER_VIEWPORTS = {
    "redo-dialog": (840, 552),
    "room": (716, 520),
    "no-diarization": (840, 552),
    "policy-off": (716, 520),
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
    from astra_voice.meetings.events import MeetingState
    from astra_voice.meetings.model import Audio, Interval, Mark, Meeting

    meeting = Meeting(
        id=MEETING_ID,
        started_at="2026-10-09T07:00:00Z",
        ended_at="2026-10-09T08:12:40Z",
        title=TITLE,
        state=MeetingState(state),
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
    from astra_voice.meetings.model import SpeakerMap, Transcript
    from astra_voice.meetings.templates import TEMPLATES
    from astra_voice.ui import meeting_card
    from astra_voice.ui.meeting_card import CardRows, TranscriptRows

    class FakeBridge(QObject):
        changed = pyqtSignal()
        saveTemplateChanged = pyqtSignal()  # noqa: N815
        savePreviewChanged = pyqtSignal()  # noqa: N815
        renameRequested = pyqtSignal(str, str)  # noqa: N815
        renameFinished = pyqtSignal(str)  # noqa: N815
        speakerOperationFinished = pyqtSignal(str)  # noqa: N815
        speakerOperationChanged = pyqtSignal()  # noqa: N815
        savePreviewOpenChanged = pyqtSignal()  # noqa: N815
        saveEchoChanged = pyqtSignal()  # noqa: N815
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
            self._exclude_echo = False
            self.utterances: list[Any] = []
            self.preview_calls = 0
            self.save_error = ""
            self.save_result = "cancelled"
            self.replace_file = ""
            self.replace_result = "saved"
            self._speakers: SpeakerMap | None = None
            self._preview_open = False
            self._exclude_echo = False
            self._speaker_operation: dict[str, Any] = {}
            self.speaker_calls: list[tuple[str, str, str]] = []
            self.reject_names = False
            self.models_missing = False
            self.preview_fixture: dict[str, Any] | None = None

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

        @pyqtProperty("QVariantMap", notify=changed)
        def diarizationInstall(self) -> dict[str, Any]:  # noqa: N802
            from astra_voice.ui.meeting_texts import diar_install_presentation

            code = getattr(self, "install_code", "missing")
            percent = 100 if code in {"verifying", "installing", "installed"} else 42
            result = diar_install_presentation(code, percent, "hf")
            return {**result, "visible": True, "accessiblePercent": 100 if percent == 100 else 40}

        @pyqtProperty("QVariantMap", constant=True)
        def diarizationRedoTexts(self) -> dict[str, str]:  # noqa: N802
            from astra_voice.ui.meeting_texts import REDO_TEXTS

            return dict(REDO_TEXTS)

        @pyqtSlot(str, int, bool, result=bool)
        def rediarizeMeeting(self, mid: str, generation: int, confirmed: bool) -> bool:  # noqa: N802
            self.actions.append(f"redo:{mid}:{generation}:{confirmed}")
            return True

        @pyqtSlot(str, int, result=bool)
        def divideMeeting(self, mid: str, generation: int) -> bool:  # noqa: N802
            self.actions.append(f"divide:{mid}:{generation}")
            return True

        @pyqtSlot(str, int, result=bool)
        def cancelDiarizationRedo(self, mid: str, generation: int) -> bool:  # noqa: N802
            self.actions.append(f"cancel-redo:{mid}:{generation}")
            return True

        @pyqtSlot(str, int, result=bool)
        def keepDiarizationAsIs(self, mid: str, generation: int) -> bool:  # noqa: N802
            self.actions.append(f"keep-redo:{mid}:{generation}")
            return True

        card = pyqtProperty("QVariantMap", _get_card, notify=changed)
        cardRows = pyqtProperty(QObject, _get_rows, constant=True)  # noqa: N815
        cardTexts = pyqtProperty("QVariantMap", _get_texts, constant=True)  # noqa: N815
        cardSaveError = pyqtProperty(str, _get_error, notify=changed)  # noqa: N815
        toast = pyqtProperty("QVariantMap", _get_toast, notify=changed)
        cardReplaceFile = pyqtProperty(str, _get_replace, notify=changed)  # noqa: N815

        @pyqtProperty("QVariantMap", constant=True)
        def speakerTexts(self) -> dict[str, str]:  # noqa: N802
            from astra_voice.ui.meeting_bridge import MeetingBridge

            return dict(MeetingBridge.speakerTexts.fget(self))

        @pyqtProperty("QVariantMap", notify=speakerOperationChanged)
        def speakerOperation(self) -> dict[str, Any]:  # noqa: N802
            return dict(self._speaker_operation)

        @pyqtProperty(bool, notify=savePreviewOpenChanged)
        def savePreviewOpen(self) -> bool:  # noqa: N802
            return self._preview_open

        @savePreviewOpen.setter  # type: ignore[no-redef]
        def savePreviewOpen(self, value: bool) -> None:  # noqa: N802
            self._preview_open = value
            if value:
                self.setSaveExcludeEcho(False)
            self.savePreviewOpenChanged.emit()

        @pyqtProperty(bool, notify=saveEchoChanged)
        def saveExcludeEcho(self) -> bool:  # noqa: N802
            return self._exclude_echo

        @pyqtSlot(bool)
        def setSaveExcludeEcho(self, value: bool) -> None:  # noqa: N802
            self._exclude_echo = value
            self.saveEchoChanged.emit()
            self.savePreviewChanged.emit()

        @pyqtProperty(int, notify=savePreviewChanged)
        def saveEchoCount(self) -> int:  # noqa: N802
            return sum(bool(u.echo) for u in self.utterances)

        @pyqtProperty(bool, notify=savePreviewChanged)
        def saveEchoVisible(self) -> bool:  # noqa: N802
            return bool(self.saveTemplate != "stats" and self.saveEchoCount > 0)

        @pyqtProperty(str, notify=savePreviewChanged)
        def saveEchoAccessibleName(self) -> str:  # noqa: N802
            return f"{meeting_card.TEXTS['excludeEcho']}, {self.saveEchoCount}"

        @pyqtProperty(str, constant=True)
        def saveEchoDescription(self) -> str:  # noqa: N802
            return meeting_card.TEXTS["excludeEchoDescription"]

        @pyqtSlot()
        def requestDiarizationInstall(self) -> None:  # noqa: N802
            self.actions.append("install-diarization")

        @pyqtSlot(str, str, result=bool)
        def renameSpeaker(self, label: str, name: str) -> bool:  # noqa: N802
            from astra_voice.meetings import speakers

            self.speaker_calls.append(("rename", label, name))
            if self.reject_names:
                self._speaker_operation = {
                    "ok": False,
                    "code": "save-error",
                    "text": "Не удалось сохранить имена участников",
                }
                self.speakerOperationChanged.emit()
                self.speakerOperationFinished.emit("save-error")
                return False
            assert self._speakers is not None
            self._speakers = speakers.rename(self._speakers, label, name)
            assert self.builder is not None
            self.builder.rename(self._speakers, label)
            self.refresh()
            self._speaker_operation = {"ok": True, "code": "renamed", "text": ""}
            self.speakerOperationChanged.emit()
            self.speakerOperationFinished.emit("renamed")
            return True

        @pyqtSlot(str, str, result=bool)
        def mergeSpeakers(self, source: str, target: str) -> bool:  # noqa: N802
            from astra_voice.meetings import speakers

            self.speaker_calls.append(("merge", source, target))
            old = speakers.speaker_label(self.meeting, source, self._speakers)
            assert self._speakers is not None
            self._speakers = speakers.merge(self._speakers, source, target)
            assert self.builder is not None
            self.builder.speakers = self._speakers
            self.builder.load(self.utterances, complete=True)
            self.refresh()
            label = speakers.speaker_label(self.meeting, target, self._speakers)
            text = f"{old} объединён с «{label}» · статистика пересчитана"
            self._speaker_operation = {"ok": True, "code": "merged", "text": text}
            self.speakerOperationChanged.emit()
            self.speakerOperationFinished.emit("merged")
            return True

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

        @saveTemplate.setter  # type: ignore[no-redef]
        def saveTemplate(self, value: str) -> None:  # noqa: N802
            if value in TEMPLATES and value != self._save_template:
                self._save_template = value
                self.saveTemplateChanged.emit()
                self.savePreviewChanged.emit()

        @pyqtSlot(result="QVariantMap")
        def savePreview(self) -> dict[str, Any]:  # noqa: N802
            self.preview_calls += 1
            if self.preview_fixture is not None:
                return self.preview_fixture
            return meeting_card.save_preview(
                self._save_template,
                self.meeting,
                Transcript(meeting_id=self.meeting.id, utterances=self.utterances),
                self._speakers,
                exclude_echo=self._exclude_echo,
            )

        def show(
            self,
            meeting: Any,
            utterances: list[Any] | None = None,
            *,
            snapshot: dict[str, Any] | None = None,
            failure: str = "",
            policy_off: bool = False,
            speakers: Any = None,
            models_missing: bool = False,
        ) -> None:
            self._save_template = TEMPLATES[0]
            self._exclude_echo = False
            self.preview_fixture = None
            self.utterances = list(utterances or [])
            self._speakers = speakers
            self.models_missing = models_missing
            self.builder = CardRows(meeting, self._rows)
            self.builder.speakers = speakers
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
                names_editable=bool(self._speakers)
                and not self.policy_off
                and self.meeting.state == "done",
                models_missing=self.models_missing,
            )
            if self.models_missing:
                state = self.diarizationInstall
                self._card.update(
                    diarizationInstallVisible=True,
                    diarizationModelsMissing=state["code"] != "installed",
                    diarizationHint=state["cardText"],
                    diarizationAction=state["action"],
                )
            self.changed.emit()

        @pyqtSlot()
        def cancelDiarizationInstall(self) -> None:  # noqa: N802
            self.actions.append("cancel-install")

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


def visual_states() -> dict[str, dict[str, Any]]:
    """M7 presentations; mockup text is imported read-only, never regenerated."""
    from dataclasses import replace

    from astra_voice.meetings.model import Speaker, SpeakerMap

    source = REPO / "design/meetings/mockups"
    sys.path.insert(0, str(source))
    from _p3 import SPK  # type: ignore[import-not-found]
    from _p6 import LINES as MARK_LINES  # type: ignore[import-not-found]

    fixtures = states()
    names = SpeakerMap(
        labels={
            "S1": Speaker(name="Ирина Соколова"),
            "S2": Speaker(name="Олег Ли"),
            "S3": Speaker(),
        }
    )

    def named(marked: bool = False, room: bool = False) -> dict[str, Any]:
        meeting = fixture_meeting(variant="mic_only" if room else "app_stream")
        meeting.transcription.diarization = True
        meeting.transcription.diar_status = "done"
        lines = [(t, w, x) for t, w, x, _ in MARK_LINES] if marked else LINES
        replies = fixture_utterances(lines)
        flags = [f for *_, f in MARK_LINES] if marked else [""] * len(replies)
        replies = [
            replace(
                u,
                speaker="S1" if room and w == "me" else w.upper() if w != "me" else "me",
                track="mic" if room or w == "me" else "far",
                echo=flag == "echo",
                multiple_speakers=flag == "multi",
            )
            for u, (_, w, _), flag in zip(replies, lines, flags, strict=True)
        ]
        if marked:
            # 08-marks contains only the pause badge, no microphone/lock gaps.
            meeting.marks = [m for m in meeting.marks if m.kind == "pause" and m.reason == "user"]

        return {"meeting": meeting, "utterances": replies, "speakers": names, "panel_fixture": SPK}

    for key in ("ready-1024", "ready-900", "names-edit", "merge", "merged"):
        fixtures[key] = named()
    fixtures["names-edit"]["action"] = "names-edit"
    fixtures["merge"]["action"] = "merge"
    fixtures["merged"]["action"] = "merged"
    fixtures["transcribing-1024"] = named()
    fixtures["transcribing-1024"]["meeting"].state = "transcribing"
    fixtures["transcribing-1024"]["snapshot"] = {
        "percent": 40,
        "done_ms": 1_744_000,
        "total_ms": 4_360_000,
    }
    fixtures["transcribing-1024"]["speakers"] = None
    import ast

    tree06 = ast.parse((source / "_p4.py").read_text())
    room_call = next(
        n
        for n in ast.walk(tree06)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "table"
        and n.args
        and isinstance(n.args[0], ast.List)
    )
    room_lines = ast.literal_eval(room_call.args[0])
    room = named(room=True)
    room["meeting"].app_class = None
    room["meeting"].started_at = "2026-10-10T11:00:00Z"
    room["meeting"].ended_at = "2026-10-10T11:41:12Z"
    room["meeting"].recorded_ms = 2_472_000
    room["meeting"].marks = []
    room["meeting"].intervals = []
    room["utterances"] = [
        replace(u, speaker=w.upper(), track="mic")
        for u, (_, w, _) in zip(fixture_utterances(room_lines), room_lines, strict=True)
    ]
    room["speakers"] = SpeakerMap(labels={"S1": Speaker(), "S2": Speaker(), "S3": Speaker()})
    room.pop("panel_fixture", None)
    room["fragment"] = True
    fixtures["room"] = room
    fixtures["no-diarization"] = {
        "meeting": fixture_meeting(marks=[], intervals=[]),
        "utterances": fixture_utterances(LINES[:5]),
        "fragment": True,
    }
    # 09 §3: no pauses, no participants panel, first seven non-mark replies.
    plain = [(t, w, text) for t, w, text in LINES if w != "mark"]
    fixtures["models-missing"] = {
        "meeting": fixture_meeting(marks=[], intervals=[]),
        "utterances": fixture_utterances(plain[:7]),
        "models_missing": True,
    }
    for name, code in (
        ("diar-missing", "missing"),
        ("diar-downloading", "downloading"),
        ("diar-installed", "installed"),
        ("diar-failed-admin-900", "failed:admin"),
    ):
        fixtures[name] = {
            "meeting": fixture_meeting(marks=[], intervals=[]),
            "utterances": fixture_utterances(plain[:2]),
            "models_missing": True,
            "install_code": code,
            "install_fragment": True,
        }
    fixtures["diarization-failed"] = states()["ready-1024"]
    fixtures["diarization-failed"]["meeting"].transcription.diar_status = "failed"
    policy = named()
    policy["meeting"].marks = []
    policy["meeting"].intervals = []
    policy["utterances"] = [
        replace(u, speaker=w.upper() if w != "me" else "me")
        for u, (_, w, _) in zip(fixture_utterances(LINES[:3]), LINES[:3], strict=True)
    ]
    policy["speakers"] = SpeakerMap(labels={"S1": Speaker(), "S2": Speaker()})
    policy.pop("panel_fixture", None)
    fixtures["policy-off"] = {**policy, "policy_off": True, "fragment": True}
    for key in ("marks-1024", "marks-900", "save-echo", "save-echo-checked", "save-statistics"):
        fixtures[key] = named(marked=True)
        if key.startswith("save-"):
            fixtures[key]["action"] = key
    fixtures["marks-900"]["table_until_ms"] = 80_000
    # Statistics is a different worked example in _p6 (214 counted replies,
    # 61 echoes / 16 minutes). Build those durations instead of forging a label.
    import ast

    tree = ast.parse((source / "_p6.py").read_text())
    function = next(
        n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "stats_file"
    )
    assignment = next(
        n
        for n in function.body
        if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "data" for t in n.targets)
    )
    statistics_data = ast.literal_eval(assignment.value)
    seed_rows = fixtures["save-statistics"]["utterances"]
    generated: list[Any] = []
    for label, minutes, _share, count in statistics_data:
        speaker = {"Ирина Соколова": "S1", "Олег Ли": "S2", "Я": "me", "Спикер 3": "S3"}[label]
        seed = next(u for u in seed_rows if u.speaker == speaker and not u.echo)
        total_ms = minutes * 60_000
        for i in range(count):
            start = total_ms * i // count
            end = total_ms * (i + 1) // count
            generated.append(
                replace(
                    seed,
                    id=f"u{len(generated):05d}",
                    seq=len(generated),
                    start_ms=start,
                    end_ms=end,
                    multiple_speakers=False,
                )
            )
    seed = next(u for u in seed_rows if u.echo)
    for i in range(61):
        generated.append(
            replace(
                seed,
                id=f"u{len(generated):05d}",
                seq=len(generated),
                start_ms=960_000 * i // 61,
                end_ms=960_000 * (i + 1) // 61,
            )
        )
    fixtures["save-statistics"]["utterances"] = generated
    fixtures.update(redo_states())
    return fixtures


def redo_states() -> dict[str, dict[str, Any]]:
    """All accepted visual sections of reference 10; B/B′ are rejected."""
    from dataclasses import replace

    from astra_voice.meetings.model import Speaker, SpeakerMap
    from astra_voice.ui.meeting_texts import REDO_TEXTS, redo_presentation

    result = {}
    for name in REFERENCE_BOUNDS:
        if not name.startswith("redo-"):
            continue
        code = name.removeprefix("redo-")
        for suffix in ("-1024", "-900"):
            code = code.removesuffix(suffix)
        code = {
            "room": "failed",
            "before-keep": "failed",
            "first": "running",
            "menu": "",
            "dialog": "",
            "offer-installed": "offer",
        }.get(code, code)
        busy = code in {"queued", "wait-memory", "running", "reconcile"}
        keep = code in {"failed", "failed-again", "cancelled"}
        divide = code == "offer" or code in {"kept-7", "kept-30"}
        action = "cancel" if busy else "again" if keep else "divide" if divide else ""
        percent = 92 if code == "reconcile" else 35
        date = "12.12" if code == "kept-30" else "19.11"
        redo = redo_presentation(
            code,
            percent,
            room=name == "redo-room",
            first=name == "redo-first",
            date=date,
            installed=name == "redo-offer-installed",
        )
        redo.update(
            meetingId=MEETING_ID,
            expectedGeneration=1,
            busy=busy,
            canStart=not busy,
            canCancel=busy,
            canKeep=keep,
            action=REDO_TEXTS.get(action, ""),
            actionName=REDO_TEXTS.get(action + "Name", ""),
            actionDescription=redo["text"],
            keepAction=REDO_TEXTS["keep"] if keep else "",
            keepName=REDO_TEXTS["keepName"],
            keepDescription=REDO_TEXTS["keepDeleteDescription"],
            menuVisible=name in {"redo-menu", "redo-dialog"},
            menuEnabled=True,
            menuText="Разделить заново…",
            needsWarning=True,
            warningTitle=REDO_TEXTS["warningTitle"].format(title=TITLE),
            warningText=REDO_TEXTS["warningText"]
            + REDO_TEXTS["warningNames"].format(names="Ирина Соколова, Олег Ли"),
            warningCaption=REDO_TEXTS["warningCaption"],
        )
        full = name in {
            "redo-failed-1024",
            "redo-failed-900",
            "redo-running-1024",
            "redo-menu",
            "redo-dialog",
        }
        header = full or name in {
            "redo-offer",
            "redo-before-keep",
            "redo-kept-deleted",
            "redo-kept-7",
            "redo-kept-30",
        }
        meeting = fixture_meeting()
        meeting.marks = []
        meeting.intervals = []
        result[name] = {
            "meeting": meeting,
            "utterances": fixture_utterances() if full else fixture_utterances(LINES[:2]),
            "redo_focus": "meetingRedoKeep"
            if name == "redo-before-keep"
            else "meetingRedoAction"
            if name
            in {
                "redo-failed-1024",
                "redo-running",
                "redo-failed-again",
                "redo-running-1024",
                "redo-offer",
                "redo-first",
                "redo-kept-7",
            }
            else "",
            "redo": redo,
            "redo_fragment": not header,
            "redo_header": header,
            "redo_action": name if name in {"redo-menu", "redo-dialog"} else "",
        }
    result["redo-room"]["meeting"].variant = "mic_only"
    for row in result["redo-room"]["utterances"]:
        row.speaker = "others"
    for name in ("redo-menu", "redo-dialog", "redo-redo-failed", "redo-done"):
        fixture = result[name]
        fixture["meeting"].transcription.diarization = True
        fixture["meeting"].transcription.diar_status = "done"
        fixture["utterances"] = [
            replace(u, speaker="S1" if u.speaker != "me" else "me") for u in fixture["utterances"]
        ]
        fixture["speakers"] = SpeakerMap(
            labels={
                "S1": Speaker(name="" if name == "redo-done" else "Ирина Соколова"),
                "S2": Speaker(name="Олег Ли"),
            }
        )
    for name in ("redo-menu", "redo-dialog"):
        fixture = result[name]
        fixture["meeting"] = fixture_meeting()
        fixture["meeting"].transcription.diarization = True
        fixture["meeting"].transcription.diar_status = "done"
        fixture["utterances"] = [
            replace(u, speaker=w.upper() if w != "me" else "me")
            for u, (_t, w, _text) in zip(fixture_utterances(LINES[:7]), LINES[:7], strict=True)
        ]
        fixture["speakers"] = SpeakerMap(
            labels={
                "S1": Speaker(name="Ирина Соколова"),
                "S2": Speaker(name="Олег Ли"),
                "S3": Speaker(),
            }
        )
        sys.path.insert(0, str(REPO / "design/meetings/mockups"))
        from _p3 import SPK

        fixture["panel_fixture"] = SPK
    return result


def apply_redo_fixture(bridge: Any, fixture: dict[str, Any]) -> None:
    from astra_voice.ui.meeting_texts import REDO_TEXTS

    redo = fixture["redo"]
    card = bridge._card
    old_participants = card.get("participants", [])
    old_marks = card.get("marks", [])
    card.update(
        diarizationRedo=redo,
        diarizationInstallVisible=False,
        marks=[],
        namesEditable=False,
        namesHint=REDO_TEXTS["namesLater"] if redo["busy"] else "",
    )
    card["subtitle"]["suffix"] = " · Звонок в DION · " + (
        "звук хранится до конца разделения"
        if redo["busy"]
        else "звук удалён"
        if redo["code"] in {"kept-deleted", "done", "no-audio"}
        else "звук хранится до " + ("12.12" if redo["code"] == "kept-30" else "19.11")
        if redo["code"] == "offer" or redo["code"].startswith("kept-")
        else "звук хранится до разделения"
    )
    if redo["busy"]:
        card["badge"] = {
            "kind": "warning",
            "icon": "clock",
            "text": REDO_TEXTS["badge"].format(percent=redo["percent"]),
        }
    if (
        fixture.get("redo_fragment")
        or fixture.get("redo_header")
        and not fixture.get("redo_action")
        and len(fixture["utterances"]) == 2
    ):
        # Fragments in §2/5/6 have no participants panel or strip.
        card["participants"] = []
    else:
        card["participants"] = [
            {"kind": "me", "id": "me", "label": "Я", "minutes": "18 мин"},
            {"kind": "others", "id": "others", "label": "Собеседники", "minutes": "54 мин"},
        ]
    if fixture.get("redo_action"):
        card["participants"] = old_participants
        card["marks"] = old_marks
        card["namesEditable"] = True
        card["subtitle"]["suffix"] = " · Звонок в DION · звук хранится до 19.10"
    bridge.changed.emit()


def d4_preview_fixture(meeting: Any, speakers: Any, *, checked: bool) -> dict[str, Any]:
    """Literal bounded fragment in _p6.d4_echo, including its hand-shortened text.

    This is screenshot-only data. Production echo filtering/merging is unchanged.
    """
    import ast
    from dataclasses import replace

    from astra_voice.meetings.model import Transcript
    from astra_voice.ui import meeting_card

    tree = ast.parse((REPO / "design/meetings/mockups/_p6.py").read_text())
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "d4_echo")
    assign = next(
        n
        for n in fn.body
        if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "rows" for t in n.targets)
    )
    rows = ast.literal_eval(assign.value)
    if checked:
        branch = next(
            n
            for n in fn.body
            if isinstance(n, ast.If) and isinstance(n.test, ast.Name) and n.test.id == "checked"
        )
        update = branch.body[0]
        assert isinstance(update, ast.Assign) and isinstance(update.value, ast.BinOp)
        rows = [r for r in rows if r[3] != "echo"] + ast.literal_eval(update.value.right)
    plain = [(t, w, text) for t, w, text, _flag in rows]
    utterances = [
        replace(u, speaker=w.upper() if w != "me" else "me", echo=flag == "echo")
        for u, (_t, w, _text, flag) in zip(fixture_utterances(plain), rows, strict=True)
    ]
    result = meeting_card.save_preview(
        "stenogram", meeting, Transcript(meeting_id=meeting.id, utterances=utterances), speakers
    )
    result["rows"] = [r for r in result["rows"] if r["kind"] == "utterance"]
    result["moreText"] = "…"
    return result


def apply_panel_fixture(bridge: Any, fixture: dict[str, Any]) -> None:
    """Reapply the mockup's full-meeting totals after any name/merge operation."""
    if not fixture.get("panel_fixture"):
        return
    panel = {
        "me" if k == "me" else k.upper(): (minutes, quote)
        for k, _label, minutes, quote, _name in fixture["panel_fixture"]
    }
    for person in bridge._card["participants"]:
        if person["id"] in panel:
            person["minutes"], person["firstText"] = panel[person["id"]]
            if bridge._card["live"] and person["kind"] == "speaker":
                person["minutes"] = "—"
    bridge.changed.emit()


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
                    "comparison_scope": "state fragment"
                    if state in RENDER_VIEWPORTS
                    else "full viewport",
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


def render(out: Path, *, redo_only: bool = False, canon_only: bool = False) -> dict[str, Any]:
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
    from PyQt5.QtCore import Q_ARG, QObject, QUrl, pyqtProperty, pyqtSignal
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
    property real clipFrom: 0
    readonly property real focusInset: Theme.focusOffset + Theme.focusWidth
    readonly property color focusColor: Theme.stateFocusRing
    width: 840; height: 552
    visible: true
    Rectangle {
        anchors.fill: parent
        color: Theme.bgApp
        clip: true
        Item {
            y: window.clipFrom
            width: parent.width
            height: parent.height - y
            clip: true
        MeetingCard {
            id: card
            objectName: "renderCard"
            x: window.padX; y: window.padY - window.clipFrom
            width: window.width - window.padX * 2
            bridge: cardBridge
        }
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

    def visual_children(node: Any) -> Any:
        yield node
        for child in node.childItems():
            yield from visual_children(child)

    def reset_focus() -> None:
        from PyQt5.QtCore import Qt

        for node in reversed(list(visual_children(window.contentItem()))):
            if node.property("focus") or node.property("visualFocus"):
                node.forceActiveFocus(Qt.OtherFocusReason)
                node.setProperty("focus", False)
        window.contentItem().forceActiveFocus(Qt.OtherFocusReason)

    for theme_name in ("light", "dark"):
        theme.value = theme_name == "dark"
        theme.changed.emit()
        fixtures = redo_states() if redo_only else visual_states()
        if canon_only:
            fixtures = {name: f for name, f in fixtures.items() if not name.startswith("redo-")}
        for name, fixture in fixtures.items():
            if name in REFERENCE_BOUNDS:
                _sheet, _x, _y, width, height, pad_x, pad_y = REDO_RENDER_BOUNDS.get(
                    name, REFERENCE_BOUNDS[name]
                )
            else:
                width, height, _reason = MISSING_REFERENCES[name]
                pad_x, pad_y = 22, 12
            if name in RENDER_VIEWPORTS:
                width, height = RENDER_VIEWPORTS[name]
                pad_x, pad_y = 22, 12
            window.setProperty(
                "clipFrom",
                max(0, pad_y - window.property("focusInset"))
                if fixture.get("redo_fragment")
                else 0,
            )
            window.setProperty("padX", pad_x)
            window.setProperty("padY", pad_y)
            window.setWidth(width)
            window.setHeight(height)
            for popup_name in (
                "meetingCardMenu",
                "meetingRediarizeDialog",
                "meetingSaveDialog",
                "meetingMergePopover",
                "meetingParticipantsDrawer",
            ):
                popup = window.findChild(QObject, popup_name)
                if popup is not None:
                    from PyQt5.QtCore import QMetaObject

                    QMetaObject.invokeMethod(popup, "close")
            toast = window.findChild(QObject, "meetingSpeakerToast")
            toast.setProperty("visible", False)
            QTest.qWait(200)
            from PyQt5.QtCore import Qt

            reset_focus()
            bridge.install_code = fixture.get("install_code", "missing")
            bridge.show(
                fixture["meeting"],
                fixture.get("utterances"),
                snapshot=fixture.get("snapshot"),
                failure=fixture.get("failure", ""),
                policy_off=fixture.get("policy_off", False),
                speakers=fixture.get("speakers"),
                models_missing=fixture.get("models_missing", False),
            )
            if fixture.get("fragment"):
                bridge._card["showActions"] = False
                bridge.changed.emit()
            if "table_until_ms" in fixture:
                bridge._rows.reset(
                    [
                        r
                        for r in bridge._rows.rows()
                        if _ms(r["time"].strip("[]")) <= fixture["table_until_ms"]
                    ]
                )
            apply_panel_fixture(bridge, fixture)
            if "redo" in fixture:
                apply_redo_fixture(bridge, fixture)
            QTest.qWait(80)
            if fixture.get("redo_fragment"):
                row = window.findChild(QObject, "meetingDiarizationRedo")
                from PyQt5.QtCore import QPointF

                card.setY(
                    pad_y - window.property("clipFrom") - row.mapToItem(card, QPointF(0, 0)).y()
                )
            elif fixture.get("install_fragment"):
                row = window.findChild(QObject, "meetingDiarizationMissing")
                from PyQt5.QtCore import QPointF

                card.setY(pad_y - row.mapToItem(card, QPointF(0, 0)).y())
            else:
                card.setY(pad_y)
            from PyQt5.QtCore import QMetaObject

            redo_action = fixture.get("redo_action")
            if redo_action == "redo-menu":
                menu = window.findChild(QObject, "meetingCardMenu")
                QMetaObject.invokeMethod(menu, "open")
                menu.setProperty("currentIndex", 2)
            elif redo_action == "redo-dialog":
                QMetaObject.invokeMethod(
                    card, "requestRedo", Qt.DirectConnection, Q_ARG("QVariant", None)
                )
            action = fixture.get("action")
            if action == "names-edit":
                window.requestActivate()
                QMetaObject.invokeMethod(
                    card, "openNames", Qt.DirectConnection, Q_ARG("QVariant", None)
                )
                QTest.qWait(200)

                for field in visual_children(window.contentItem()):
                    if (
                        field.objectName() == "meetingSpeakerName"
                        and field.isVisible()
                        and field.property("speakerId") == "S2"
                    ):
                        field.forceActiveFocus(Qt.TabFocusReason)
                        field.setProperty("cursorPosition", len(field.property("text")))
                        break
            elif action == "merge":
                panel = window.findChild(QObject, "meetingCardParticipants")
                QMetaObject.invokeMethod(
                    panel, "showMerge", Qt.DirectConnection, Q_ARG("QVariant", "S3")
                )
            elif action == "merged":
                bridge.mergeSpeakers("S3", "S1")
                apply_panel_fixture(bridge, fixture)
                window.contentItem().forceActiveFocus(Qt.OtherFocusReason)
            elif action and action.startswith("save-"):
                QMetaObject.invokeMethod(card, "openSaveDialog")
                QTest.qWait(40)
                if action == "save-echo-checked":
                    bridge.setSaveExcludeEcho(True)
                elif action == "save-statistics":
                    bridge.saveTemplate = "stats"
                if action in {"save-echo", "save-echo-checked"}:
                    bridge.preview_fixture = d4_preview_fixture(
                        bridge.meeting, bridge._speakers, checked=action == "save-echo-checked"
                    )
                    bridge.savePreviewChanged.emit()
            QTest.qWait(200)
            if action == "names-edit":
                for field in visual_children(window.contentItem()):
                    if (
                        field.objectName() == "meetingSpeakerName"
                        and field.isVisible()
                        and field.property("speakerId") == "S2"
                    ):
                        field.forceActiveFocus(Qt.TabFocusReason)
                        field.setProperty("cursorPosition", len(field.property("text")))
                        if not field.hasActiveFocus():
                            raise RuntimeError("names-edit failed to focus S2")
                        break
                else:
                    raise RuntimeError("names-edit has no visible S2 field")
            if action == "save-statistics":
                preview_scroll = window.findChild(QObject, "meetingSavePreviewFlickable")
                preview_scroll.setProperty(
                    "contentY",
                    max(
                        0,
                        preview_scroll.property("contentHeight")
                        - preview_scroll.property("height"),
                    ),
                )
                QTest.qWait(40)
            if not redo_action and action not in {
                "names-edit",
                "merge",
                "save-echo",
                "save-echo-checked",
                "save-statistics",
            }:
                reset_focus()
                QTest.qWait(20)
                leaked = [
                    n.objectName()
                    for n in visual_children(window.contentItem())
                    if n.isVisible() and n.property("visualFocus")
                ]
                if leaked:
                    raise RuntimeError(f"Focus leaked into {name}/{theme_name}: {leaked}")
            if fixture.get("redo_focus"):
                target = window.findChild(QObject, fixture["redo_focus"])
                window.requestActivate()
                target.forceActiveFocus(Qt.TabFocusReason)
                QTest.qWait(20)
            # Reassert keyboard focus after theme/state changes, in both palettes.
            if action == "merge":
                window.requestActivate()
                cancel = window.findChild(QObject, "meetingMergeCancel")
                cancel.setProperty("focus", False)
                cancel.forceActiveFocus(Qt.TabFocusReason)
                QTest.qWait(20)
                if not cancel.property("visualFocus"):
                    raise RuntimeError(f"Merge cancel lost keyboard focus: {theme_name}")
            if name == "diar-downloading":
                window.requestActivate()
                button = window.findChild(QObject, "meetingDiarizationInstall")
                button.setProperty("focus", False)
                button.forceActiveFocus(Qt.TabFocusReason)
                QTest.qWait(20)
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
            if redo_action == "redo-dialog":
                dialog = window.findChild(QObject, "meetingRediarizeDialog")
                image = image.copy(
                    round(dialog.property("x")),
                    round(dialog.property("y")),
                    round(dialog.property("width")),
                    round(dialog.property("height")),
                )
                width, height = image.width(), image.height()
            crop_rect = None
            if name in REDO_CROP_INSETS:
                left, top, right, bottom = REDO_CROP_INSETS[name]
                crop_rect = {
                    "x": left,
                    "y": top,
                    "width": width - left - right,
                    "height": height - top - bottom,
                }
                background = image.pixelColor(0, 0)
                image = image.copy(left, top, crop_rect["width"], crop_rect["height"])
                if any(image.pixelColor(x, 0) != background for x in range(image.width())):
                    raise RuntimeError(f"Header leaked into content crop: {name}/{theme_name}")
                if name in {"redo-running", "redo-first"}:
                    focus_color = window.property("focusColor")
                    right_strip = range(max(0, image.width() - 124), image.width())
                    for band in ((2, 3), (37, 38)):
                        if not any(
                            image.pixelColor(x, y) == focus_color for y in band for x in right_strip
                        ):
                            raise RuntimeError(f"Focus ring clipped: {name}/{theme_name}/{band}")
                width, height = image.width(), image.height()
                pad_x -= left
                pad_y -= top
            filename = f"{name}-{theme_name}.png"
            if not image.save(str(out / filename), "PNG"):
                raise RuntimeError(f"Cannot write {filename}")
            print(f"{filename}: {width}x{height}", flush=True)
            rows.append(
                {
                    "file": filename,
                    "kind": "render",
                    "crop_rect": crop_rect,
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
                    "visual_focus": [
                        node.objectName()
                        for node in visual_children(window.contentItem())
                        if node.isVisible() and node.property("visualFocus")
                    ],
                    "comparison_scope": "state fragment: header/banner/table/install row only"
                    if name in RENDER_VIEWPORTS
                    else "full viewport",
                    "preview_data_source": "_p6.d4_echo"
                    if action in {"save-echo", "save-echo-checked"}
                    else None,
                    "merge_geometry": {
                        key: window.findChild(QObject, "meetingMergePopover").property(key)
                        for key in ("x", "y", "width", "height")
                    }
                    if action == "merge"
                    else None,
                    "save_dialog_geometry": {
                        key: window.findChild(QObject, "meetingSaveDialog").property(key)
                        for key in ("x", "y", "width", "height")
                    }
                    if action in {"save-echo", "save-echo-checked"}
                    else None,
                    "rows": bridge.cardRows.rowCount(),
                    "preview": bridge.savePreview()
                    if action and action.startswith("save-")
                    else None,
                    "excluded_echo_geometry": {
                        key: window.findChild(QObject, "meetingSaveExcludedEcho").property(key)
                        for key in ("x", "y", "width", "height", "visible", "text")
                    }
                    if action == "save-statistics"
                    else None,
                }
            )
    if not redo_only:
        rows.extend(render_sections(out, app, theme))
    rows.extend(reference_crops(out))
    from PyQt5.QtGui import QImage, QPainter

    for row in list(rows):
        if row["kind"] != "render" or not row["file"].startswith("redo-"):
            continue
        name, palette = row["file"].removesuffix(".png").rsplit("-", 1)
        actual = QImage(str(out / row["file"]))
        ref = QImage(str(out / f"{name}-ref-{palette}.png"))
        pair = QImage(
            actual.width() + ref.width(), max(actual.height(), ref.height()), QImage.Format_ARGB32
        )
        pair.fill(0xFF888888)
        painter = QPainter(pair)
        painter.drawImage(0, 0, actual)
        painter.drawImage(actual.width(), 0, ref)
        painter.end()
        pair.save(str(out / f"{name}-{palette}-pair.png"))
    window.hide()
    sip.delete(engine)
    if warnings:
        raise RuntimeError("\n".join(warnings))
    qml = [
        REPO / "qml/sections/MeetingCard.qml",
        REPO / "qml/sections/Meetings.qml",
        REPO / "qml/components/SettingRow.qml",
        REPO / "qml/components/DiarizationProgress.qml",
        REPO / "qml/Theme.qml",
        REPO / "qml/components/AvSelect.qml",
        REPO / "qml/components/OverlaySurface.qml",
        *sorted((REPO / "qml/components").glob("Meeting*.qml")),
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
            "M7 QML: named participants, operations, marks, D4; "
            "short snippets and panel minutes/quotes come from _p3/_p6. "
            "Reference gaps are explicit."
        ),
        "missing_references": {
            name: reason for name, (_w, _h, reason) in MISSING_REFERENCES.items()
        },
        "warnings": warnings,
        "pngs": rows,
    }
    (out / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def render_sections(out: Path, app: Any, theme: Any) -> list[dict[str, Any]]:
    """Actual Meetings.qml, showing the same two-row group fragments as 09 §2."""
    import tempfile

    from PyQt5 import sip
    from PyQt5.QtCore import QObject, Qt, QUrl
    from PyQt5.QtQml import QQmlApplicationEngine, QQmlEngine, QQmlExpression
    from PyQt5.QtQuick import QQuickItem
    from PyQt5.QtTest import QTest

    from astra_voice.core.settings import Settings
    from astra_voice.meetings.store import MeetingStore
    from astra_voice.ui.icons import install_icon_provider
    from astra_voice.ui.meeting_bridge import MeetingBridge

    rows: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="s7b2-section-") as folder:
        bridge = MeetingBridge(
            Settings(meetings_enabled=True, meeting_diarization=True),
            store=MeetingStore(Path(folder)),
            save=lambda _: None,
            diar_models_provider=lambda: None,
        )
        engine = QQmlApplicationEngine()
        warnings: list[str] = []
        engine.warnings.connect(lambda messages: warnings.extend(m.toString() for m in messages))
        engine.rootContext().setContextProperty("themeSource", theme)
        engine.rootContext().setContextProperty("meetingBridge", bridge)
        install_icon_provider(engine)
        engine.loadData(
            b"""import QtQuick 2.15
import QtQuick.Window 2.15
import "qml"
import "qml/sections"
Window { width:840; height:164; visible:true
 Rectangle { anchors.fill:parent; color:Theme.bgApp; clip:true
  Meetings { objectName:"renderSection"; x:22; y:4; width:796 }
 }
}""",
            QUrl.fromLocalFile(str(REPO / "_render-section.qml")),
        )
        if not engine.rootObjects():
            raise RuntimeError("\n".join(warnings))
        window = engine.rootObjects()[0]
        section = window.findChild(QObject, "renderSection")
        group_box = window.findChild(QObject, "meetingSettingsGroups")
        install = window.findChild(QObject, "meetingDiarizationModels")
        toggle = window.findChild(QObject, "meetingDiarization")
        toggle_row = toggle.parentItem().parentItem().parentItem()
        row_column = install.parentItem()
        group = row_column.parentItem().parentItem()
        # Hide out-of-fragment material, retaining production geometry and controls.
        for child in section.childItems():
            if child != group_box:
                child.setVisible(False)
        for child in group_box.childItems():
            if child != group:
                child.setVisible(False)
        full_rows = list(row_column.childItems())
        # 09 §1 assumes the independent ASR model is installed; screenshot-only fixture.
        asr_row = full_rows[0]
        # Freeze its canonical geometry before replacing the out-of-scope ASR fixture.
        freeze = QQmlExpression(
            QQmlEngine.contextForObject(asr_row), asr_row, "implicitHeight = Theme.cardRowHWithSub"
        )
        freeze.evaluate()
        if freeze.hasError():
            raise RuntimeError(freeze.error().toString())
        asr_row.setProperty("sub", "GigaAM v3 RNN-T · установлена")
        for child in asr_row.findChildren(QObject):
            if child.property("small") is True:
                child.setProperty("visible", False)
        for child in full_rows:
            if child not in (toggle_row, install):
                child.setVisible(False)
        toggle_row.setProperty("divider", False)
        # Reparent the actual group out of the full section; its canonical rows remain.
        holder = QQuickItem(window.contentItem())
        holder.setX(22)
        holder.setY(17)
        holder.setWidth(796)
        group.setParentItem(holder)
        group.setX(0)
        group.setY(0)
        section.setVisible(False)
        QTest.qWait(100)

        def visual_children(item: Any) -> Any:
            yield item
            for child in item.childItems():
                yield from visual_children(child)

        def reset_focus() -> None:
            for item in reversed(list(visual_children(window.contentItem()))):
                if item.property("focus") or item.property("visualFocus"):
                    item.forceActiveFocus(Qt.OtherFocusReason)
                    item.setProperty("focus", False)
            window.contentItem().forceActiveFocus(Qt.OtherFocusReason)

        for palette in ("light", "dark"):
            theme.value = palette == "dark"
            theme.changed.emit()
            for suffix, code in (
                ("group-missing", "missing"),
                ("missing", "missing"),
                ("downloading", "downloading"),
                ("installed", "installed"),
                ("failed-admin", "failed:admin"),
                ("offline", "offline-user"),
            ):
                name = "section-" + suffix
                full_group = suffix == "group-missing"
                for child in full_rows:
                    child.setVisible(full_group or child in (toggle_row, install))
                toggle_row.setProperty("divider", full_group)
                holder.setY(0 if full_group else 17)
                _, _x, _y, width, height, _, _ = REFERENCE_BOUNDS[name]
                window.setHeight(height)
                bridge._diar_code = code
                bridge._diar_percent = 100 if code == "installed" else 42
                bridge._diar_accessible_percent = 100 if code == "installed" else 40
                bridge._publish_diar_install()
                button = window.findChild(QObject, "meetingDiarizationModelsAction")
                reset_focus()
                if suffix == "downloading":
                    window.requestActivate()
                    button.forceActiveFocus(Qt.TabFocusReason)
                QTest.qWait(100)
                focused = [
                    item.objectName()
                    for item in visual_children(window.contentItem())
                    if item.isVisible() and item.property("visualFocus")
                ]
                expected_focus = [button.objectName()] if suffix == "downloading" else []
                if focused != expected_focus:
                    raise RuntimeError(f"Section focus leaked into {name}/{palette}: {focused}")
                picture = window.grabWindow()
                if picture.isNull():
                    grab = window.contentItem().grabToImage()
                    QTest.qWait(50)
                    picture = grab.image()
                filename = f"{name}-{palette}.png"
                if picture.isNull() or not picture.save(str(out / filename), "PNG"):
                    raise RuntimeError(filename)
                print(f"{filename}: {width}x{height}", flush=True)
                rows.append(
                    {
                        "file": filename,
                        "kind": "render",
                        "scale": 1,
                        "width": width,
                        "height": height,
                        "presentation": _jsonable(bridge.diarizationInstall),
                        "visual_focus": focused,
                        "comparison_scope": "09 section group fragment",
                    }
                )
        bridge.shutdown()
        window.hide()
        sip.delete(engine)
        if warnings:
            raise RuntimeError("\n".join(warnings))
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True, help="Destination for PNGs and manifest")
    parser.add_argument(
        "--redo-only", action="store_true", help="Only accepted reference 10 states"
    )
    parser.add_argument("--canon-only", action="store_true", help="Only pre-10 canonical frames")
    options = parser.parse_args()
    manifest = render(
        options.out.resolve(), redo_only=options.redo_only, canon_only=options.canon_only
    )
    for row in manifest["pngs"]:
        if row["kind"] != "render":
            print(f"{row['file']}: {row['width']}x{row['height']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
