"""E13 section presentation and human opt-in; no recorder or detector.

Only D1 acceptance enables the feature. Reads run in a daemon that owns no Qt
objects; a parented timer consumes plain results and shutdown ignores late work.
"""

from __future__ import annotations

import os
import queue
import threading
import time
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

from PyQt5.QtCore import QObject, Qt, QTimer, pyqtProperty, pyqtSignal, pyqtSlot

from astra_voice.core.diagnostics import endpoint_class
from astra_voice.core.meeting_session import MetadataWriter
from astra_voice.core.policy import Policy
from astra_voice.core.settings import Settings
from astra_voice.core.settings import save as settings_save
from astra_voice.meetings.apps import AppClass, AppDataError, AppDefinition, load_apps
from astra_voice.meetings.events import MeetingState, PolicyCode
from astra_voice.meetings.model import Meeting, Transcript, normalize_text
from astra_voice.meetings.policy import RecordMode, parse_policy, validate_endpoint
from astra_voice.meetings.settings_ext import (
    MEETING_DEFAULTS,
    EffectiveMeetingSettings,
    effective_settings,
)
from astra_voice.meetings.store import MeetingStore
from astra_voice.meetings.templates import (
    TEMPLATES,
    ExportExists,
    render,
    sanitize_filename,
    visible_utterances,
    with_extension,
    write_export,
)
from astra_voice.platform.session import SessionKind
from astra_voice.ui import meeting_card
from astra_voice.ui.meeting_card import CARD_STATES, MODEL_ACTIONS, CardRows, TranscriptRows
from astra_voice.ui.meeting_recording import MeetingRecording
from astra_voice.ui.meeting_texts import (
    DELETE_TEXTS,
    EXIT_TEXTS,
    RENAME_TEXTS,
    RESPONSIBILITY_ITEMS,
    TEXTS,
    W2_TEXTS,
    W4_TEXTS,
)

RENAME_ACTION = "Переименовать"

SAVE_ERROR = "Не удалось сохранить настройки. Попробуйте ещё раз."


def _edited_setting(name: str, value: object) -> bool | int | str:
    """Reject bad edits without the persisted-settings loader's recovery defaults.

    An explicitly emptied text field is a reset; an invalid nonempty value is
    never a reset. Endpoint validation shares the policy parser's URL rules.
    """
    default = MEETING_DEFAULTS[name]
    if type(value) is not type(default):
        raise ValueError("Недопустимый тип настройки встречи")
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        valid = (
            0 <= value <= 365
            if name == "meeting_audio_retention_days"
            else value in (0, 30, 90, 365)
        )
        if not valid:
            raise ValueError("Недопустимый срок хранения встречи")
        return value
    if not isinstance(value, str) or not isinstance(default, str):
        raise ValueError("Недопустимый тип настройки встречи")
    if value == "":
        return default
    limits = {
        "meeting_notice_text": 500,
        "meeting_invite_text": 500,
        "meeting_self_label": 40,
        "meeting_ai_endpoint": 2048,
        "meeting_ai_model": 128,
    }
    normalized = normalize_text(value, limits[name])
    return validate_endpoint(normalized) if name == "meeting_ai_endpoint" else normalized


class MeetingListing(Protocol):
    def list(self) -> list[Meeting]: ...


LoadResult = tuple[list[Meeting] | None, tuple[AppDefinition, ...]]


def _submit(work: Callable[[], None]) -> None:
    threading.Thread(target=work, name="meeting-list", daemon=True).start()


def _pick_save_path(suggested: str, fmt: str) -> str | None:
    """System «Save» dialog (KDE portal / Qt in Fly) for D4.

    It does not ask about overwriting: the file is created with O_EXCL and an
    existing one is replaced only after the card's own question (MN-3/15).
    """
    from PyQt5.QtWidgets import QFileDialog

    dialog = QFileDialog(None, "Сохранить как", suggested)
    dialog.setAcceptMode(QFileDialog.AcceptSave)
    dialog.setOption(QFileDialog.DontConfirmOverwrite, True)
    dialog.setFileMode(QFileDialog.AnyFile)
    dialog.setDefaultSuffix(fmt)
    dialog.setNameFilters(["Markdown (*.md)"] if fmt == "md" else ["Текст (*.txt)"])
    dialog.selectFile(suggested)
    if not dialog.exec_():
        return None
    chosen = dialog.selectedFiles()
    return chosen[0] if chosen else None


def _documents_dir() -> Path:
    from PyQt5.QtCore import QStandardPaths

    location = QStandardPaths.writableLocation(QStandardPaths.DocumentsLocation)
    return Path(location) if location else Path.home()


def _open_folder(path: str) -> bool:
    from PyQt5.QtCore import QUrl

    from astra_voice.platform.external import open_external

    return open_external(QUrl.fromLocalFile(path).toString())


class MeetingReader(Protocol):
    def read(self, meeting_id: str, filename: str) -> Any: ...


def _duration(milliseconds: int) -> str:
    seconds = milliseconds // 1000
    return f"{seconds // 3600}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"


def _megabytes(size: int) -> str:
    return f"{size / (1024 * 1024):.1f}".rstrip("0").rstrip(".").replace(".", ",")


def _count(count: int) -> str:
    if 11 <= count % 100 <= 14:
        return "встреч"
    return "встреча" if count % 10 == 1 else "встречи" if 2 <= count % 10 <= 4 else "встреч"


# design/meetings/tokens.md §2.4: badge glyphs (rec/pause keep their «●»/«‖» text marks).
_BADGE_ICONS = {
    MeetingState.TRANSCRIBING: "clock",
    MeetingState.WAITING_MEMORY: "clock",
    MeetingState.NEEDS_MODEL: "alert",
    MeetingState.DONE: "check",
    MeetingState.FAILED: "alert",
    MeetingState.INTERRUPTED: "alert",
}


@dataclass(frozen=True)
class _ExportResult:
    serial: int
    target: Path
    fmt: str
    status: str
    template: str


class MeetingBridge(QObject):
    changed = pyqtSignal()
    enabledChanged = pyqtSignal(bool)
    recordingRequested = pyqtSignal()
    meetingActionRequested = pyqtSignal(str, str)
    openMeetingsRequested = pyqtSignal()
    participantsRequested = pyqtSignal()
    renameRequested = pyqtSignal(str, str)
    renameFinished = pyqtSignal(str)
    deleteRequested = pyqtSignal(str, str)
    cardSaveRequested = pyqtSignal()
    cardSaveFinished = pyqtSignal(str)
    saveTemplateChanged = pyqtSignal()
    savePreviewChanged = pyqtSignal()
    _exportCompleted = pyqtSignal(object)
    # QML bindings run before ordinary slots of the same signal: «card» is notified by
    # its own signal, emitted after the cache is dropped (a stale cache never reaches QML).
    cardChanged = pyqtSignal()
    overlayKeyboardRequested = pyqtSignal()
    noticeKeyboardRequested = pyqtSignal()
    exitConfirmed = pyqtSignal()

    def __init__(
        self,
        stored: Settings,
        *,
        policy: Policy | None = None,
        mirror: Settings | None = None,
        session: SessionKind = SessionKind.OTHER,
        save: Callable[[Settings], None] = settings_save,
        store: MeetingListing | None = None,
        known_apps: tuple[AppDefinition, ...] | None = None,
        submit: Callable[[Callable[[], None]], None] = _submit,
        clock: Callable[[], float] = time.monotonic,
        pick_save_path: Callable[[str, str], str | None] = _pick_save_path,
        documents_dir: Callable[[], Path] = _documents_dir,
        open_folder: Callable[[str], bool] = _open_folder,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._pick_save_path, self._documents_dir = pick_save_path, documents_dir
        self._open_folder = open_folder
        self._stored, self._mirror, self._save = stored, mirror, save
        self._policy = policy or Policy()
        self._parsed = parse_policy(self._policy)
        self._store: MeetingListing = MeetingStore() if store is None else store
        self._apps = known_apps
        self._submit, self._clock = submit, clock
        self._documents: list[Meeting] = []
        self._rename_pending = ""
        self._queue: queue.Queue[LoadResult] = queue.Queue(maxsize=1)
        self._loading = self._loading_visible = self._read_error = False
        self._save_error = ""
        self._started = 0.0
        self._responsibility_open = self._recording = self._closed = False
        self._capture: MeetingRecording | None = None
        self._exit_confirm = False
        self._timer = QTimer(self)
        self._timer.setInterval(20)
        self._timer.timeout.connect(self._poll)
        # Card A (E13.M4): one open meeting, its rows and the transcript reader.
        self._card_id = ""
        self._card_key: tuple[str, int] | None = None
        self._card_rows = TranscriptRows(self)
        self._card_builder: CardRows | None = None
        self._card_loaded = self._card_error = False
        self._card_transcript: Transcript | None = None
        self._card_serial = 0
        self._card_results: queue.SimpleQueue[tuple[int, Transcript | None, bool]] = (
            queue.SimpleQueue()
        )
        self._card_timer = QTimer(self)
        self._card_timer.setInterval(20)
        self._card_timer.timeout.connect(self._card_poll)
        self._card_save_error = ""
        self._save_template = TEMPLATES[0]
        self._export_busy = False
        self._export_id = ""
        self._delete_pending: dict[str, Meeting] = {}
        self._delete_waiting: set[str] = set()
        self._release_waiting: dict[str, Callable[[], None]] = {}
        self._delete_observed: dict[str, str | None] = {}
        self._delete_timer = QTimer(self)
        self._delete_timer.setInterval(100)
        self._delete_timer.timeout.connect(self._poll_deletions)
        self.cardSaveFinished.connect(self._resume_deletes)
        self._export_writer = MetadataWriter()
        self._exportCompleted.connect(self._export_completed, Qt.QueuedConnection)
        # (path, fmt, template) awaiting «Заменить».
        self._replace_target: tuple[Path, str, str] | None = None
        # «Сохранить как…» from the list waits until the opened card has loaded (D4 needs «ready»).
        self._card_save_pending = False
        # TRANSCRIBING → DONE of one generation: the live rows stay, the final text adds its tail.
        self._card_tail = False
        self._saved_folder = ""
        self._toast: dict[str, Any] = {"serial": 0, "folder": "", "file": ""}
        # The card is read by several QML bindings per change: build it once.
        self._card_cache: dict[str, Any] | None = None
        self._card_progress: dict[str, Any] | None = None
        self.changed.connect(self._drop_card_cache)

    def _effective(self) -> EffectiveMeetingSettings:
        return effective_settings(self._stored, self._parsed)

    @pyqtProperty(str, notify=changed)
    def sectionState(self) -> str:
        if not self._parsed.recording_allowed:
            return "policy-off"
        if self._recording:
            return "recording"
        if self._loading:
            return "loading"
        if self._read_error:
            return "error"
        if not self.enabled:
            return "off-saved" if self._documents else "off"
        if self.policyNote:
            return "policy-some"
        return "success" if self._documents else "empty"

    @pyqtProperty(bool, notify=changed)
    def loadingVisible(self) -> bool:
        return self._loading_visible

    @pyqtProperty(bool, notify=changed)
    def loading(self) -> bool:
        return self._loading

    @pyqtProperty(bool, notify=changed)
    def readError(self) -> bool:
        return self._read_error

    @pyqtProperty(str, notify=changed)
    def saveError(self) -> str:
        return self._save_error

    @pyqtProperty(bool, notify=changed)
    def enabled(self) -> bool:
        return bool(self._effective().recording_allowed)

    @pyqtProperty(bool, notify=changed)
    def canEnable(self) -> bool:
        return self._parsed.recording_allowed and not self._closed

    @pyqtProperty(bool, notify=changed)
    def recording(self) -> bool:
        return self._recording

    @recording.setter  # type: ignore[no-redef]
    def recording(self, value: bool) -> None:
        if self._recording != value:
            self._recording = value
            self.changed.emit()

    @pyqtProperty("QVariantMap", notify=changed)
    def settings(self) -> dict[str, Any]:
        effective = self._effective().settings
        return {key: getattr(effective, key) for key in MEETING_DEFAULTS}

    @pyqtProperty("QVariantMap", notify=changed)
    def locked(self) -> dict[str, bool]:
        return {key: decision.locked for key, decision in self._effective().controls.items()}

    def _organization(self) -> bool:
        return any(
            key.startswith("meeting_") for key in self._policy.raw.keys() | self._policy.locked_keys
        )

    @pyqtProperty(str, notify=changed)
    def policyNote(self) -> str:
        return TEXTS["policyNote"] if self._organization() else ""

    @pyqtProperty(str, notify=changed)
    def policyInvalidText(self) -> str:
        code = self._parsed.meeting_record.code
        invalid = code in {
            PolicyCode.RECORD_INVALID,
            PolicyCode.INVALID,
            PolicyCode.UNSAFE,
            PolicyCode.UNKNOWN_KEY,
        }
        return TEXTS["policyInvalid"] if invalid else ""

    @pyqtProperty("QVariantMap", constant=True)
    def texts(self) -> dict[str, str]:
        return dict(TEXTS)

    @pyqtProperty("QVariantList", constant=True)
    def responsibilityItems(self) -> list[dict[str, Any]]:
        return [dict(item) for item in RESPONSIBILITY_ITEMS]

    @pyqtProperty(str, notify=changed)
    def responsibilityParagraph(self) -> str:
        return TEXTS["d1Organization" if self._organization() else "d1Personal"]

    @pyqtProperty(bool, notify=changed)
    def responsibilityOpen(self) -> bool:
        return self._responsibility_open

    @pyqtProperty(bool, notify=changed)
    def detectorVisible(self) -> bool:
        return bool(self._apps)

    @pyqtProperty(bool, notify=changed)
    def detectorEnabled(self) -> bool:
        return self.detectorVisible and self._parsed.detector_allowed

    @pyqtProperty(str, notify=changed)
    def detectorCaption(self) -> str:
        return TEXTS[
            "d1ManualCaption"
            if self._parsed.meeting_record.value == RecordMode.MANUAL
            else "d1OfferCaption"
        ]

    @pyqtProperty(str, notify=changed)
    def knownAppsText(self) -> str:
        classes = {app.app_class for app in self._apps or ()}
        return " · ".join(
            text
            for kind, text in (
                (AppClass.DION, "DION"),
                (AppClass.BROWSER, "звонок в браузере"),
                (AppClass.OTHER, "Другая программа"),
            )
            if kind in classes
        )

    @pyqtProperty(bool, notify=changed)
    def aiDisabled(self) -> bool:
        return self._parsed.meeting_ai_endpoint.value == "off"

    @pyqtProperty(bool, notify=changed)
    def aiAdminAddress(self) -> bool:
        return self._parsed.meeting_ai_endpoint.locked and not self.aiDisabled

    @pyqtProperty(str, notify=changed)
    def endpointKind(self) -> str:
        endpoint = self.settings["meeting_ai_endpoint"]
        if not endpoint:
            return "none"
        category = endpoint_class(endpoint)
        host = urlsplit(endpoint).hostname
        if endpoint.startswith("http://") and self._parsed.meeting_ai_allow_plaintext.value:
            return "plaintext"
        if host in {"localhost", "127.0.0.1", "::1"}:
            return "loopback"
        if category == "internal":
            return "internal"
        if category == "external":
            secure = any(
                isinstance(value, str) and value.strip().lower() == "secure"
                for value in (self._policy.raw.get("profile"), self._policy.values.get("profile"))
            )
            offline = self._mirror.offline if self._mirror is not None else self._stored.offline
            if offline or secure:
                return "offline"
            if self._parsed.external_without_confirmation:
                return "external-admin"
            return "external"
        return "none"

    @pyqtProperty(str, notify=changed)
    def endpointCaption(self) -> str:
        return {
            "none": "",
            "plaintext": "Без шифрования, разрешено администратором · адрес внутри вашей сети",
            "loopback": "Сервис на этом компьютере",
            "internal": "Адрес внутри вашей сети",
            "offline": "Внешний адрес недоступен в офлайн-режиме",
            "external-admin": (
                "Внешний адрес согласован администратором — предупреждения перед отправкой не будет"
            ),
            "external": "Внешний адрес — перед каждой отправкой программа предупредит",
        }[self.endpointKind]

    @pyqtProperty("QVariantList", notify=changed)
    def meetings(self) -> list[dict[str, Any]]:
        ordered = sorted(
            self._documents,
            key=lambda row: (
                row.state in {MeetingState.RECORDING, MeetingState.PAUSED},
                datetime.fromisoformat(row.started_at.replace("Z", "+00:00")),
            ),
            reverse=True,
        )
        return [self._row(meeting) for meeting in ordered]

    @pyqtProperty(str, notify=changed)
    def totalText(self) -> str:
        count = len(self._documents)
        size = _megabytes(sum(row.size_bytes for row in self._documents))
        return f"Всего: {count} {_count(count)} · {size} МБ"

    def _row(self, meeting: Meeting) -> dict[str, Any]:
        state = meeting.state
        duration = _duration(meeting.recorded_ms)
        start = datetime.fromisoformat(meeting.started_at.replace("Z", "+00:00"))
        try:
            start = start.astimezone()
        except OverflowError:
            # Valid RFC3339 extrema may fall outside datetime in the local
            # zone. Keep the source wall time instead of breaking a Qt property.
            pass
        date = (
            f"{start.day:02d}.{start.month:02d}.{start.year:04d} · "
            f"{start.hour:02d}:{start.minute:02d}"
        )
        subtitle = f"{date} · {duration} · {_megabytes(meeting.size_bytes)} МБ"
        badge, kind, action, actions = {
            MeetingState.RECORDING: (f"● Запись · {duration}", "primary", "Остановить", []),
            MeetingState.PAUSED: (f"‖ Пауза · {duration}", "neutral", "Продолжить", ["Остановить"]),
            MeetingState.RECORDED: (
                "Записана",
                "neutral",
                "Расшифровать",
                [RENAME_ACTION, "Удалить"],
            ),
            MeetingState.TRANSCRIBING: (
                "Расшифровка · 0 %",
                "warning",
                "Отменить",
                [RENAME_ACTION, "Удалить"],
            ),
            MeetingState.WAITING_MEMORY: ("Ждёт памяти", "warning", "", ["Отменить", "Удалить"]),
            MeetingState.NEEDS_MODEL: ("Нужна модель", "warning", "Установить", ["Удалить"]),
            MeetingState.DONE: (
                "Готово",
                "success",
                "Открыть",
                ["Сохранить как…", "Имена участников", RENAME_ACTION, "Удалить"],
            ),
            MeetingState.FAILED: ("Ошибка расшифровки", "danger", "Повторить", ["Удалить"]),
            MeetingState.INTERRUPTED: ("Прервана", "warning", "Расшифровать", ["Удалить"]),
            MeetingState.DELETING: ("", "neutral", "", []),
        }[state]
        jobs = self._capture.jobs if self._capture is not None else None
        hints = jobs.row(meeting.id, meeting) if jobs is not None else None
        progress = 0
        if state == MeetingState.TRANSCRIBING and hints is not None:
            progress = int(hints["percent"])
            badge = f"Расшифровка · {progress} %"
        if state == MeetingState.RECORDING:
            subtitle = date
        elif state == MeetingState.RECORDED and meeting.cancelled and hints is not None:
            subtitle = str(hints["cancelledText"])
        elif state == MeetingState.FAILED and hints is not None:
            subtitle = str(hints["failure"])
        elif state == MeetingState.PAUSED:
            subtitle = (
                "Экран был заблокирован — продолжите вручную"
                if meeting.marks and meeting.marks[-1].reason == "lock"
                else date
            )
        elif state == MeetingState.WAITING_MEMORY:
            subtitle = "Расшифровка подождёт: сейчас мало свободной памяти"
        elif state == MeetingState.NEEDS_MODEL:
            subtitle = "Для расшифровки встреч нужна модель GigaAM v3"
        elif state == MeetingState.INTERRUPTED:
            subtitle = f"Сохранено {duration} — программа закрылась во время записи"
        elif state == MeetingState.DONE:
            if not meeting.audio.present:
                subtitle += " · звук удалён"
            elif meeting.transcription.finished_at and meeting.audio.retain_days:
                try:
                    until = datetime.fromisoformat(
                        meeting.transcription.finished_at.replace("Z", "+00:00")
                    ) + timedelta(days=meeting.audio.retain_days)
                except OverflowError:
                    pass  # A valid year-9999 timestamp may have no representable expiry.
                else:
                    subtitle += f" · звук до {until:%d.%m}"
        if not self.enabled:
            action = "Открыть" if state == MeetingState.DONE else ""
            actions = ["Сохранить как…", "Удалить"] if state == MeetingState.DONE else ["Удалить"]
        saving = bool(
            self._capture is not None
            and self._capture.controller.meeting is not None
            and self._capture.controller.meeting.id == meeting.id
            and self._capture.controller.state == "stopped"
            and state in {MeetingState.RECORDING, MeetingState.PAUSED}
        )
        if meeting.id in self._delete_pending or (
            jobs is not None and meeting.id in jobs.tombstones
        ):
            actions = [item for item in actions if item != RENAME_ACTION]
        if saving:
            badge, kind, action, actions = "Сохраняю запись…", "neutral", "", []
        return {
            "id": meeting.id,
            "title": meeting.title,
            "subtitle": subtitle,
            "badge": badge,
            "badgeKind": kind,
            "badgeIcon": "" if saving else _BADGE_ICONS.get(state, ""),
            "openable": not saving and state in CARD_STATES,
            "action": action,
            "actionEnabled": bool(
                action
                and (
                    state not in {MeetingState.RECORDED, MeetingState.INTERRUPTED}
                    or jobs is not None
                )
                and (
                    state not in {MeetingState.RECORDING, MeetingState.PAUSED}
                    or self._capture is not None
                    and self._capture.current_meeting is not None
                    and self._capture.current_meeting.id == meeting.id
                    and self._capture.busy
                )
            ),
            "actions": actions,
            "actionKeys": ["rename" if action == RENAME_ACTION else "" for action in actions],
            "progress": progress,
            "active": not saving and state in {MeetingState.RECORDING, MeetingState.PAUSED},
            "transcribing": state == MeetingState.TRANSCRIBING,
        }

    @pyqtSlot()
    def refresh(self) -> None:
        if self._closed or self._loading:
            return
        self._loading, self._loading_visible, self._read_error = True, False, False
        self._started = self._clock()
        output = self._queue
        store, apps = self._store, self._apps

        def work() -> None:
            known = apps
            if known is None:
                try:
                    known = load_apps()
                except (OSError, AppDataError):
                    known = ()
            try:
                documents = store.list()
            except Exception:
                documents = None  # Fixed UI error only; no exception text/logging.
            output.put((documents, known))

        self._submit(work)
        self._timer.start()
        self.changed.emit()

    @pyqtSlot()
    def retry(self) -> None:
        self.refresh()

    def _poll(self) -> None:
        if self._closed:
            return
        try:
            documents, self._apps = self._queue.get_nowait()
        except queue.Empty:
            if (
                self._loading
                and not self._loading_visible
                and self._clock() - self._started > 0.3 + 1e-9
            ):
                self._loading_visible = True
                self.changed.emit()
            return
        self._loading = self._loading_visible = False
        self._read_error = documents is None
        if documents is not None:
            self._documents = documents
            self._overlay_capture()
            self._sync_card()
        self._timer.stop()
        self.changed.emit()

    def shutdown(self) -> None:
        # No notices about our own exit; W3 first, then the ordinary stop with
        # finalization (W2 still shows «Сохраняю запись…»), then W2.
        for name in ("_meeting_notifications", "_meeting_notice"):
            owner = getattr(self, name, None)
            if owner is not None:
                owner.shutdown()
        if self._capture is not None:
            self._capture.shutdown()
        overlay = getattr(self, "_meeting_overlay", None)
        if overlay is not None:
            overlay.shutdown()
        self._closed = True
        self._delete_timer.stop()
        self._export_writer.close(timeout=2.0)
        self._timer.stop()
        self._card_timer.stop()
        self._loading = self._loading_visible = False
        self._responsibility_open = False

    def refreshPolicy(self, policy: Policy) -> None:
        old_enabled = self.enabled
        self._policy, self._parsed = policy, parse_policy(policy)
        if self._capture is not None:
            self._capture.policy_changed()
        if not self.canEnable:
            self._responsibility_open = False
        self.changed.emit()
        if old_enabled != self.enabled:
            self.enabledChanged.emit(self.enabled)

    @pyqtSlot()
    def openResponsibility(self) -> None:
        if self.canEnable and not self.enabled and not self._recording:
            self._responsibility_open = True
            self.changed.emit()

    @pyqtSlot()
    def cancelResponsibility(self) -> None:
        self._responsibility_open = False
        self.changed.emit()

    @pyqtSlot(bool)
    def acceptResponsibility(self, offer: bool) -> None:
        if not self._responsibility_open or not self.canEnable or self.enabled:
            return
        if self._persist(
            {
                "meetings_enabled": True,
                "meeting_offer_on_call": bool(offer and self.detectorEnabled),
            }
        ):
            self._responsibility_open = False
            self.changed.emit()

    @pyqtSlot()
    def disableMeetings(self) -> None:
        if not self._recording and not self._closed:
            self._persist({"meetings_enabled": False})

    def _persist(self, updates: dict[str, Any]) -> bool:
        candidate = replace(self._stored, extra=dict(self._stored.extra))
        for key, value in updates.items():
            setattr(candidate, key, value)
        try:
            self._save(candidate)
        except (OSError, ValueError):
            self._save_error = SAVE_ERROR
            self.changed.emit()
            return False
        old_enabled = self.enabled
        for key, value in updates.items():
            setattr(self._stored, key, value)
            if self._mirror is not None:
                setattr(self._mirror, key, value)
        self._save_error = ""
        self.changed.emit()
        if old_enabled != self.enabled:
            self.enabledChanged.emit(self.enabled)
        return True

    @pyqtSlot(str, "QVariant")
    def setSetting(self, key: str, value: Any) -> None:
        if (
            self._closed
            or key == "meetings_enabled"
            or key not in MEETING_DEFAULTS
            or self.locked[key]
        ):
            return
        if key == "meeting_offer_on_call" and not self.detectorEnabled:
            self.changed.emit()
            return
        try:
            normalized = _edited_setting(key, value)
        except (ValueError, UnicodeError):
            self.changed.emit()
            return
        self._persist({key: normalized})

    @pyqtSlot()
    def requestRecording(self) -> None:
        if self.enabled and not self._recording and not self._closed:
            if self._capture is not None:
                self._capture.open_dialog()
            self.recordingRequested.emit()

    @pyqtSlot(str, str)
    def requestMeetingAction(self, meeting_id: str, action: str) -> None:
        for row in self.meetings:
            if row["id"] == meeting_id and action and action in [row["action"], *row["actions"]]:
                if action == row["action"] and not row["actionEnabled"]:
                    return
                if action == RENAME_ACTION:
                    meeting = next(
                        (item for item in self._documents if item.id == meeting_id), None
                    )
                    if meeting is not None:
                        self.renameRequested.emit(meeting_id, meeting.title)
                    return
                if action == "Удалить":
                    meeting = next(
                        (item for item in self._documents if item.id == meeting_id), None
                    )
                    if meeting is not None:
                        self.deleteRequested.emit(meeting_id, meeting.title)
                    return
                if action in {"Открыть", "Сохранить как…"}:
                    self.openMeeting(meeting_id)
                    if action == "Сохранить как…" and self._card_id == meeting_id:
                        self._card_save_pending = True
                        self._flush_card_save()
                    return
                if self._capture is not None and action in {"Остановить", "Продолжить"}:
                    self._capture.action(meeting_id, action)
                    return
                if self._capture is not None and self._capture.jobs_action(meeting_id, action):
                    return
                self.meetingActionRequested.emit(meeting_id, action)
                return

    def bindRecording(self, capture: MeetingRecording) -> None:
        """Bind the existing section to the inert M2 adapter; no launch here."""
        self._capture = capture
        capture.release_files = self._release_files
        if capture.jobs is not None:
            capture.jobs.release_files = self._release_files
        capture.changed.connect(self._capture_changed)
        capture.openMeetingsRequested.connect(self.openMeetingsRequested)
        capture.participantsRequested.connect(self.participantsRequested)
        self._capture_changed()

    def _capture_changed(self) -> None:
        self._overlay_capture()
        self._sync_card()
        if self._exit_confirm and not self._exit_needs_confirmation():
            # The recording ended by itself: the question no longer applies.
            self._exit_confirm = False
        if not self._closed:
            self.changed.emit()

    def _overlay_capture(self) -> None:
        if self._closed or self._capture is None:
            return
        self._recording = self._capture.busy
        updates = dict(self._capture.recovered)
        current = self._capture.current_meeting
        if current is not None:
            updates[current.id] = current
        jobs = self._capture.jobs
        if jobs is not None:
            # The queue publishes the newest durable state (generation +1 after stop).
            for meeting_id in jobs.tombstones:
                updates.pop(meeting_id, None)
            updates.update(jobs.meetings)
        if updates:
            self._documents = [row for row in self._documents if row.id not in updates]
            self._documents.extend(updates.values())
        if jobs is not None:
            hidden = jobs.tombstones | {
                mid
                for mid, status in jobs.deletions.items()
                if status in {"deleted", "purge_error"}
            }
            self._documents = [row for row in self._documents if row.id not in hidden]

    @pyqtProperty(int, notify=changed)
    def transcriptionPercent(self) -> int:
        """Running transcription for the tray hint (spec §3.9), -1 when none."""
        percent = self._capture.transcription_percent if self._capture is not None else None
        return -1 if percent is None else percent

    @pyqtProperty("QVariantMap", notify=changed)
    def overlay(self) -> dict[str, Any]:
        return self._capture.overlay if self._capture else {"visible": False, "state": "hidden"}

    @pyqtProperty("QVariantMap", constant=True)
    def overlayTexts(self) -> dict[str, str]:
        return dict(W2_TEXTS)

    @pyqtSlot()
    def on_overlay_pause_clicked(self) -> None:
        if self._capture:
            self._capture.overlay_action("pause")

    @pyqtSlot()
    def on_overlay_resume_clicked(self) -> None:
        if self._capture:
            self._capture.overlay_action("resume")

    @pyqtSlot()
    def on_overlay_stop_clicked(self) -> None:
        if self._capture:
            self._capture.overlay_action("stop")

    @pyqtSlot()
    def on_overlay_participants_clicked(self) -> None:
        if self._capture:
            self._capture.overlay_action("participants")

    def _exit_needs_confirmation(self) -> bool:
        overlay = self.overlay
        return bool(overlay.get("active")) and overlay.get("state") not in {
            "stopping",
            "error",
            "hidden",
        }

    def request_quit(self, quit_app: Callable[[], None]) -> None:
        """Tray «Выход» (T-478): ask only while a recording runs; otherwise quit.

        SIGTERM/SIGINT and programmatic ``app.quit()`` never come here: they stop
        and save without a question (MeetingRecording._stop_for_exit).
        """
        if self._closed or not self._exit_needs_confirmation():
            quit_app()
            return
        # Asked again (e.g. its window was hidden): always bring the question back.
        self._exit_confirm = True
        self.changed.emit()

    @pyqtProperty(bool, notify=changed)
    def exitConfirmOpen(self) -> bool:
        return self._exit_confirm

    @pyqtProperty("QVariantMap", constant=True)
    def exitTexts(self) -> dict[str, str]:
        return dict(EXIT_TEXTS)

    @pyqtSlot()
    def confirmExit(self) -> None:
        if not self._exit_confirm:
            return
        self._exit_confirm = False
        self.changed.emit()
        self.exitConfirmed.emit()

    @pyqtSlot()
    def cancelExit(self) -> None:
        if self._exit_confirm:
            self._exit_confirm = False
            self.changed.emit()

    @pyqtSlot()
    def openNoticeWithKeyboard(self) -> None:
        """Tray «Текст для участников…»: W3 with keyboard focus (spec §7)."""
        if self._capture is not None and self.overlay.get("active"):
            self._capture.overlay_action("participants")
            self.noticeKeyboardRequested.emit()

    def notice(self) -> tuple[str, bool]:
        """Effective W3 text and whether the administrator set it (spec §2.7)."""
        admin = self._parsed.meeting_notice_text.code == PolicyCode.NOTICE_ADMIN
        return str(self.settings["meeting_notice_text"]), admin

    @pyqtSlot()
    def on_overlay_extend_clicked(self) -> None:
        if self._capture:
            self._capture.overlay_action("extend")

    @pyqtSlot()
    def activateOverlayKeyboard(self) -> None:
        if self.overlay.get("visible"):
            self.overlayKeyboardRequested.emit()

    @pyqtProperty(bool, notify=changed)
    def recordingAvailable(self) -> bool:
        """Capture is bound; whether it may start is ``enabled`` (setting + policy)."""
        return self._capture is not None

    @pyqtProperty(bool, notify=changed)
    def startDialogOpen(self) -> bool:
        return self._capture is not None and self._capture.dialog_open

    @pyqtProperty(str, notify=changed)
    def startState(self) -> str:
        return self._capture.state if self._capture else "default"

    @pyqtProperty("QVariantMap", constant=True)
    def startTexts(self) -> dict[str, str]:
        return dict(W4_TEXTS)

    @pyqtProperty("QVariantList", notify=changed)
    def startOptions(self) -> list[dict[str, Any]]:
        return self._capture.options if self._capture else []

    @pyqtProperty(str, notify=changed)
    def selectedSource(self) -> str:
        return self._capture.selected_source if self._capture else "room"

    @pyqtProperty(str, notify=changed)
    def startError(self) -> str:
        return self._capture.error if self._capture else ""

    @pyqtProperty("QStringList", notify=changed)
    def startWarnings(self) -> list[str]:
        return self._capture.warnings if self._capture else []

    @pyqtProperty(bool, notify=changed)
    def startCanStart(self) -> bool:
        return self._capture is not None and self._capture.can_start

    @pyqtProperty(bool, notify=changed)
    def startSpeakers(self) -> bool:
        return self._capture is not None and self._capture._audio.speakers

    @pyqtProperty(bool, notify=changed)
    def interruptedOpen(self) -> bool:
        return bool(self._capture and self._capture.interrupted_text)

    @pyqtProperty(str, notify=changed)
    def interruptedText(self) -> str:
        return self._capture.interrupted_text if self._capture else ""

    @pyqtSlot()
    def on_start_clicked(self) -> None:
        if self._capture is not None:
            self._capture.begin_clicked()

    @pyqtSlot()
    def closeStartDialog(self) -> None:
        if self._capture is not None:
            self._capture.close_dialog()

    @pyqtSlot(str)
    def selectRecordingSource(self, source: str) -> None:
        if self._capture is not None:
            self._capture.select(source)

    @pyqtSlot()
    def retryStart(self) -> None:
        if self._capture is not None:
            self._capture.retry()

    @pyqtSlot()
    def closeInterrupted(self) -> None:
        if self._capture is not None:
            self._capture.close_interrupted()

    @pyqtSlot()
    def openInterruptedMeetings(self) -> None:
        if self._capture is not None:
            self._capture.close_interrupted(open_meetings=True)

    @pyqtSlot(result=bool)
    def copyInviteText(self) -> bool:
        from PyQt5.QtWidgets import QApplication

        clipboard = QApplication.clipboard()
        if clipboard is None:
            return False
        clipboard.setText(self.settings["meeting_invite_text"])
        return True

    # ---- card A «Документ» (E13.M4, spec §3.7/§4.7) -----------------------

    def _card_meeting(self) -> Meeting | None:
        if not self._card_id:
            return None
        return next((row for row in self._documents if row.id == self._card_id), None)

    @pyqtSlot(str)
    def openMeeting(self, meeting_id: str) -> None:
        meeting = next((row for row in self._documents if row.id == meeting_id), None)
        if self._closed or meeting is None or meeting.state not in CARD_STATES:
            return
        self._save_template = TEMPLATES[0]
        self._card_id = meeting_id
        self._card_key = None
        self._card_save_error = ""
        self._card_save_pending = False
        self._replace_target = None  # a question of another card never applies here
        self._sync_card()
        self.saveTemplateChanged.emit()
        self.savePreviewChanged.emit()
        self.changed.emit()

    def _flush_card_save(self) -> None:
        """Emit a pending list «Сохранить как…» once the card is no longer loading."""
        if not self._card_save_pending:
            return
        view = self.card.get("view")
        if view == "loading":
            return
        self._card_save_pending = False
        if view == "ready":
            self.cardSaveRequested.emit()

    @pyqtSlot()
    def closeMeeting(self) -> None:
        if not self._card_id:
            return
        self._card_id = ""
        self._card_key = None
        self._card_builder = None
        self._card_transcript = None
        self._card_save_pending = False
        self._replace_target = None
        self._card_serial += 1  # late reads of the closed card are dropped
        self._card_rows.reset()
        self._card_timer.stop()
        self.savePreviewChanged.emit()
        self.changed.emit()

    @pyqtProperty(bool, notify=changed)
    def cardOpen(self) -> bool:
        return bool(self._card_id) and self._card_meeting() is not None

    @pyqtProperty(QObject, constant=True)
    def cardRows(self) -> QObject:
        return self._card_rows

    @pyqtProperty("QVariantMap", constant=True)
    def cardTexts(self) -> dict[str, str]:
        return dict(meeting_card.TEXTS)

    @pyqtProperty("QVariantList", constant=True)
    def saveTemplates(self) -> list[dict[str, str]]:
        return [
            {"id": key, "icon": icon, "title": title, "caption": caption}
            for key, (icon, title, caption) in zip(
                TEMPLATES, meeting_card.TEMPLATE_TEXTS, strict=True
            )
        ]

    @pyqtProperty(str, notify=saveTemplateChanged)
    def saveTemplate(self) -> str:
        return self._save_template

    @saveTemplate.setter  # type: ignore[no-redef]
    def saveTemplate(self, template: str) -> None:
        if template in TEMPLATES and template != self._save_template:
            self._save_template = template
            self.saveTemplateChanged.emit()
            self.savePreviewChanged.emit()

    @pyqtSlot(result="QVariantMap")
    def savePreview(self) -> dict[str, Any]:
        meeting = self._savable_meeting("txt")
        if meeting is None:
            return {}
        return meeting_card.save_preview(self._save_template, meeting, self._card_transcript)

    @pyqtProperty(str, notify=changed)
    def cardSaveError(self) -> str:
        return self._card_save_error

    @pyqtProperty("QVariantMap", notify=changed)
    def toast(self) -> dict[str, Any]:
        return dict(self._toast)

    def _jobs(self) -> Any:
        return self._capture.jobs if self._capture is not None else None

    def _snapshot(self, meeting: Meeting) -> dict[str, Any] | None:
        jobs = self._jobs()
        if jobs is None or meeting.state != MeetingState.TRANSCRIBING:
            return None
        snapshot = jobs.job_snapshot(meeting.id)
        return snapshot if isinstance(snapshot, dict) else None

    def _progress(self, meeting: Meeting) -> dict[str, Any] | None:
        snapshot = self._snapshot(meeting)
        if snapshot is None:
            return None
        return {key: snapshot.get(key, 0) for key in ("percent", "done_ms", "total_ms")}

    def _drop_card_cache(self) -> None:
        self._card_cache = None
        self.cardChanged.emit()

    @pyqtProperty("QVariantMap", notify=cardChanged)
    def card(self) -> dict[str, Any]:
        if self._card_cache is None:
            self._card_cache = self._build_card()
        return self._card_cache

    def _build_card(self) -> dict[str, Any]:
        meeting = self._card_meeting()
        if meeting is None:
            return {"open": False}
        jobs = self._jobs()
        hints = jobs.row(meeting.id, meeting) if jobs is not None else None
        failure = (
            str(hints.get("failureCode", "")) if hints else (meeting.transcription.failure or "")
        )
        builder = self._card_builder
        return meeting_card.presentation(
            meeting,
            loaded=self._card_loaded,
            read_error=self._card_error,
            rows=builder,
            can_transcribe=self.enabled and jobs is not None,
            policy_off=self.sectionState == "policy-off",
            failure=failure,
            snapshot=self._card_progress if meeting.state == MeetingState.TRANSCRIBING else None,
            rename_allowed=RENAME_ACTION in self._row(meeting)["actions"],
            deleting=meeting.id in self._delete_pending
            or (jobs is not None and meeting.id in jobs.tombstones),
        )

    def _sync_card(self) -> None:
        """Rows follow the open meeting: reload on a new state/generation, append live text."""
        if not self._card_id or self._closed:
            return
        meeting = self._card_meeting()
        if meeting is None:
            self._card_id = ""  # deleted elsewhere: back to the list
            self._card_builder = None
            self._card_rows.reset()
            self.savePreviewChanged.emit()
            return
        key = (str(meeting.state), meeting.meeting_generation)
        if key != self._card_key:
            previous, self._card_key = self._card_key, key
            live = (str(MeetingState.TRANSCRIBING), meeting.meeting_generation)
            if (
                previous == live
                and meeting.state == MeetingState.DONE
                and self._card_builder is not None
                and self._card_loaded
                and not self._card_error
            ):
                # The finished run keeps its live rows: no model reset, only the tail.
                self._card_builder.meeting = meeting
                self._card_progress = None
                self._card_tail = True
                self._load_transcript(meeting)
                self.savePreviewChanged.emit()
                return
            self._card_tail = False
            self._card_builder = CardRows(meeting, self._card_rows)
            self._card_rows.reset()
            self._card_loaded = self._card_error = False
            self._card_transcript = None
            self._card_progress = self._progress(meeting)
            if meeting.state in {MeetingState.DONE, MeetingState.TRANSCRIBING}:
                self._load_transcript(meeting)
            self.savePreviewChanged.emit()
            return
        if self._card_builder is not None:
            self._card_builder.meeting = meeting
        if meeting.state == MeetingState.TRANSCRIBING:
            if self._card_loaded:
                self._append_live(meeting)
            else:
                self._card_progress = self._progress(meeting)

    def _append_live(self, meeting: Meeting) -> None:
        builder = self._card_builder
        snapshot = self._snapshot(meeting)
        if snapshot is not None:
            self._card_progress = {
                key: snapshot.get(key, 0) for key in ("percent", "done_ms", "total_ms")
            }
        if builder is None:
            return
        live = meeting_card.snapshot_utterances(snapshot)
        if live and live[-1].seq < builder.last_seq:
            # The job started over (not a resume): show what it has now.
            builder.load(live, complete=False)
            return
        builder.extend(live)

    def _load_transcript(self, meeting: Meeting) -> None:
        self._card_serial += 1
        serial, output = self._card_serial, self._card_results
        store, meeting_id = self._store, meeting.id
        generation = meeting.meeting_generation
        root = getattr(store, "root", None)

        def work() -> None:
            reader = getattr(store, "read", None)
            transcript: Transcript | None = None
            failed = False
            if reader is None:
                failed = True
            else:
                try:
                    value = reader(meeting_id, "transcript.json")
                    if isinstance(value, Transcript):
                        transcript = value
                except Exception:
                    missing = root is not None and not os.path.lexists(
                        Path(root) / meeting_id / "transcript.json"
                    )
                    failed = not missing  # no transcript yet is not an error
            if transcript is not None:
                if transcript.meeting_id != meeting_id:
                    transcript, failed = None, True
                elif transcript.meeting_generation != generation:
                    transcript = None  # a previous run is not a read error
            output.put((serial, transcript, failed))

        self._submit(work)
        self._card_timer.start()

    def _card_poll(self) -> None:
        if self._closed:
            self._card_timer.stop()
            return
        try:
            serial, transcript, failed = self._card_results.get_nowait()
        except queue.Empty:
            return
        if serial != self._card_serial:
            return
        self._card_timer.stop()
        meeting = self._card_meeting()
        if meeting is None or self._card_builder is None:
            return
        self._card_transcript = transcript
        self._card_loaded, self._card_error = True, failed
        utterances = visible_utterances(transcript)
        done = meeting.state == MeetingState.DONE
        tail, self._card_tail = self._card_tail, False
        if tail and done and not failed and self._card_builder.continues(utterances):
            self._card_builder.finish(utterances)
        else:
            self._card_builder.load(utterances, complete=done)
        if meeting.state == MeetingState.TRANSCRIBING:
            self._append_live(meeting)
        self.savePreviewChanged.emit()
        self.changed.emit()
        self._flush_card_save()

    @pyqtSlot()
    def retryCard(self) -> None:
        meeting = self._card_meeting()
        if meeting is not None:
            self._card_key = None
            self._sync_card()
            self.changed.emit()

    @pyqtSlot(str)
    def cardAction(self, action: str) -> None:
        """Panel and progress buttons: queue actions go through the row whitelist."""
        meeting = self._card_meeting()
        if meeting is None or self._closed:
            return
        if action in MODEL_ACTIONS:
            if self.enabled or action == "Подробнее":
                self.meetingActionRequested.emit(meeting.id, action)
            return
        if action == "rename":
            action = RENAME_ACTION
        if action in {"delete", "Удалить встречу…", "Удалить…"}:
            action = "Удалить"
        self.requestMeetingAction(meeting.id, action)

    @pyqtProperty("QVariantMap", constant=True)
    def renameTexts(self) -> dict[str, str]:
        return dict(RENAME_TEXTS)

    @pyqtSlot(str, str, result=str)
    def renameMeeting(self, meeting_id: str, title: str) -> str:
        meeting = next((row for row in self._documents if row.id == meeting_id), None)
        jobs = self._jobs()
        if (
            self._closed
            or self._rename_pending
            or jobs is None
            or meeting is None
            or RENAME_ACTION not in self._row(meeting)["actions"]
        ):
            return "error"
        try:
            # Validate controls before collapsing whitespace (tabs/newlines must not disappear).
            title = normalize_text(
                " ".join(normalize_text(title, 1_000_000).split()), 120, minimum=1
            )
        except ValueError:
            return "error"
        self._rename_pending = meeting_id

        def completed(ok: bool) -> None:
            self._rename_pending = ""
            if self._closed:
                return
            if ok:
                self._overlay_capture()
                self.savePreviewChanged.emit()
                self.changed.emit()
            self.renameFinished.emit("saved" if ok else "error")

        # Even injected synchronous submitters must publish after the slot returns pending.
        jobs.rename(meeting_id, title, lambda ok: QTimer.singleShot(0, lambda: completed(ok)))
        return "pending"

    @pyqtProperty("QVariantMap", constant=True)
    def deleteTexts(self) -> dict[str, str]:
        return dict(DELETE_TEXTS)

    @pyqtSlot(str)
    def confirmDelete(self, meeting_id: str) -> None:
        meeting = next((item for item in self._documents if item.id == meeting_id), None)
        jobs = self._jobs()
        if (
            self._closed
            or jobs is None
            or meeting is None
            or meeting_id in self._delete_pending
            or meeting.state in {MeetingState.RECORDING, MeetingState.PAUSED}
        ):
            return
        self._save_error = ""
        self._delete_pending[meeting_id] = deepcopy(meeting)
        self._delete_waiting.add(meeting_id)
        self._delete_timer.start()
        self._resume_deletes()

    def _resume_deletes(self, _result: str = "") -> None:
        jobs = self._jobs()
        if self._closed or jobs is None:
            return
        for meeting_id, done in tuple(self._release_waiting.items()):
            if self._export_busy and self._export_id == meeting_id:
                continue
            self._release_waiting.pop(meeting_id)
            done()
        for meeting_id in tuple(self._delete_waiting):
            if self._export_busy and self._export_id == meeting_id:
                continue
            self._delete_waiting.remove(meeting_id)
            if meeting_id not in jobs.tombstones and jobs.deletions.get(meeting_id) not in {
                "pending",
                "deleting",
                "deleted",
                "purge_error",
            }:
                jobs.delete_and_purge(meeting_id)
        self._poll_deletions()

    def _release_files(self, meeting_id: str, done: Callable[[], None]) -> None:
        """Both human and age-based deletion wait for the existing export owner."""
        if self._export_busy and self._export_id == meeting_id:
            self._release_waiting[meeting_id] = done
        else:
            done()

    def _poll_deletions(self) -> None:
        jobs = self._jobs()
        if self._closed or jobs is None:
            return
        changed = False
        for meeting_id, original in tuple(self._delete_pending.items()):
            status = (
                "waiting_export"
                if meeting_id in self._delete_waiting
                else jobs.deletions.get(meeting_id)
            )
            if meeting_id in self._delete_observed and self._delete_observed[meeting_id] == status:
                continue
            self._delete_observed[meeting_id] = status
            changed = True
            if status == "waiting_export":
                continue
            if status == "error":
                self._documents = [row for row in self._documents if row.id != meeting_id]
                self._documents.append(original)
                self._save_error = DELETE_TEXTS["error"]
            elif status in {"deleting", "deleted", "purge_error"}:
                self._documents = [row for row in self._documents if row.id != meeting_id]
                if self._card_id == meeting_id:
                    self.closeMeeting()
                    self.openMeetingsRequested.emit()
                if status == "purge_error":
                    self._save_error = DELETE_TEXTS["error"]
            if status in {"error", "deleted", "purge_error"}:
                self._delete_pending.pop(meeting_id)
                self._delete_observed.pop(meeting_id, None)
        if not self._delete_pending:
            self._delete_timer.stop()
        if changed:
            self._overlay_capture()
            self.changed.emit()

    @pyqtSlot(str, result=str)
    def saveAs(self, fmt: str) -> str:
        """D4 «Выбрать папку и сохранить…»: system dialog, then a 0600 file (MN-3/15).

        Returns ``pending`` while writing; cardSaveFinished delivers the result.
        An existing file is replaced only by ``replaceSaveTarget``.
        """
        if self._export_busy:
            return "pending"
        self._replace_target = None
        meeting = self._savable_meeting(fmt)
        if meeting is None:
            return "error"
        self._export_id = meeting.id
        self._export_busy = True  # also guard the system dialog's nested event loop
        serial, template = self._card_serial, self._save_template
        name = sanitize_filename(meeting.title, fmt)
        try:
            chosen = self._pick_save_path(str(self._documents_dir() / name), fmt)
        except Exception:
            self._export_busy = False
            self.cardSaveFinished.emit("error")
            raise
        if not chosen:
            self._export_busy = False
            self.cardSaveFinished.emit("cancelled")
            return "cancelled"
        # The nested dialog may have closed/switched the card. Snapshot only now.
        meeting = self._savable_meeting(fmt)
        if meeting is None or serial != self._card_serial:
            self._export_busy = False
            self.cardSaveFinished.emit("error")
            return "error"
        # Never derived from «exists after the dialog»: another process may have
        # created the file meanwhile. O_EXCL first, replacing needs the person's «Заменить».
        return self._write_card(
            meeting, with_extension(Path(chosen), fmt), fmt, template, overwrite=False
        )

    @pyqtProperty(str, notify=changed)
    def cardReplaceFile(self) -> str:
        return self._replace_target[0].name if self._replace_target is not None else ""

    @pyqtSlot(result=str)
    def replaceSaveTarget(self) -> str:
        """«Заменить» in the question after ``exists``: replace that regular file."""
        if self._export_busy:
            return "pending"
        pending, self._replace_target = self._replace_target, None
        if pending is None:
            return "error"
        target, fmt, template = pending
        meeting = self._savable_meeting(fmt)
        if meeting is None:
            self.changed.emit()
            return "error"
        self._export_id = meeting.id
        self._export_busy = True
        return self._write_card(meeting, target, fmt, template, overwrite=True)

    @pyqtSlot()
    def cancelReplace(self) -> None:
        if self._replace_target is not None:
            self._replace_target = None
            self.changed.emit()

    def _savable_meeting(self, fmt: str) -> Meeting | None:
        meeting = self._card_meeting()
        jobs = self._jobs()
        if (
            meeting is None
            or self._closed
            or fmt not in ("md", "txt")
            or meeting.state != MeetingState.DONE
            or not self._card_loaded
            or self._card_error
            or self._card_tail  # the final text is still being read
            or meeting.id in self._delete_pending
            or (
                jobs is not None
                and (
                    meeting.id in jobs.tombstones
                    or jobs.deletions.get(meeting.id)
                    in {"pending", "deleting", "deleted", "purge_error"}
                )
            )
        ):
            return None
        return meeting

    def _write_card(
        self, meeting: Meeting, target: Path, fmt: str, template: str, *, overwrite: bool
    ) -> str:
        # Plain detached values only: the worker never reads card/bridge state.
        snapshot, transcript = deepcopy(meeting), deepcopy(self._card_transcript)
        serial, emit = self._card_serial, self._exportCompleted.emit

        def work() -> None:
            status = "saved"
            try:
                text = render(template, snapshot, transcript, fmt)
                write_export(target, text, overwrite=overwrite)
            except ExportExists:
                status = "exists"
            except Exception:
                status = "error"
            finally:
                try:
                    emit(_ExportResult(serial, target, fmt, status, template))
                except RuntimeError:
                    pass  # QObject already destroyed during application shutdown

        try:
            self._export_writer.submit(work)
        except (RuntimeError, queue.Full):
            self._export_completed(_ExportResult(serial, target, fmt, "error", template))
        return "pending"

    @pyqtSlot(object)
    def _export_completed(self, result: object) -> None:
        assert isinstance(result, _ExportResult)
        self._export_busy = False
        if self._closed:
            return
        if result.serial != self._card_serial:
            # Release deletion waiters without opening D4 for a different card.
            self.cardSaveFinished.emit("stale")
            return
        target = result.target
        if result.status == "exists":
            self._replace_target = (target, result.fmt, result.template)
        elif result.status == "error":
            self._card_save_error = meeting_card.TEXTS["saveError"]
        else:
            self._card_save_error = ""
            self._saved_folder = str(target.parent)
            self._toast = {
                "serial": int(self._toast["serial"]) + 1,
                "folder": target.parent.name or str(target.parent),
                "file": target.name,
            }
        self.changed.emit()
        self.cardSaveFinished.emit(result.status)

    @pyqtSlot()
    def clearCardSaveError(self) -> None:
        if self._card_save_error:
            self._card_save_error = ""
            self.changed.emit()

    @pyqtSlot()
    def openSavedFolder(self) -> None:
        if self._saved_folder:
            self._open_folder(self._saved_folder)
