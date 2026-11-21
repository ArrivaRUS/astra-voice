"""E13 section presentation and human opt-in; no recorder or detector.

Only D1 acceptance enables the feature. Reads run in a daemon that owns no Qt
objects; a parented timer consumes plain results and shutdown ignores late work.
"""

from __future__ import annotations

import os
import queue
import threading
import time
from collections import deque
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

from PyQt5.QtCore import QEvent, QObject, Qt, QTimer, pyqtProperty, pyqtSignal, pyqtSlot

from astra_voice.core.diagnostics import endpoint_class
from astra_voice.core.meeting_session import MetadataWriter
from astra_voice.core.policy import Policy
from astra_voice.core.settings import Settings
from astra_voice.core.settings import save as settings_save
from astra_voice.meetings import speakers as speaker_ops
from astra_voice.meetings.apps import AppClass, AppDataError, AppDefinition, load_apps
from astra_voice.meetings.diar_models import installed_diar_models, nonblocking_diar_models
from astra_voice.meetings.events import MeetingState, PhaseStatus, PolicyCode
from astra_voice.meetings.model import (
    Meeting,
    SpeakerMap,
    Transcript,
    normalize_self_label,
    normalize_text,
    published_generation,
)
from astra_voice.meetings.policy import RecordMode, parse_policy, validate_endpoint
from astra_voice.meetings.settings_ext import (
    MEETING_DEFAULTS,
    EffectiveMeetingSettings,
    effective_settings,
)
from astra_voice.meetings.store import MeetingStore, Writer
from astra_voice.meetings.templates import (
    TEMPLATES,
    ExportExists,
    render,
    sanitize_filename,
    visible_utterances,
    with_extension,
    write_export,
)
from astra_voice.net.gate import NetworkGate
from astra_voice.platform.session import SessionKind
from astra_voice.ui import meeting_card
from astra_voice.ui.meeting_card import CARD_STATES, MODEL_ACTIONS, CardRows, TranscriptRows
from astra_voice.ui.meeting_recording import MeetingRecording
from astra_voice.ui.meeting_texts import (
    DELETE_TEXTS,
    DIAR_INSTALL_TEXTS,
    EXIT_TEXTS,
    REDO_TEXTS,
    RENAME_TEXTS,
    RESPONSIBILITY_ITEMS,
    TEXTS,
    W2_TEXTS,
    W4_TEXTS,
    diar_install_presentation,
    redo_presentation,
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
    if name == "meeting_self_label":
        return normalize_self_label(value)
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
    exclude_echo: bool = False


@dataclass
class _SpeakerGroup:
    failed: threading.Event = field(default_factory=threading.Event)


@dataclass(frozen=True)
class _SpeakerWrite:
    meeting_id: str
    generation: int
    speakers: SpeakerMap
    group: _SpeakerGroup
    code: str
    text: str


@dataclass(frozen=True)
class _CardRead:
    serial: int
    transcript: Transcript | None
    speakers: SpeakerMap | None = None
    failed: bool = False
    speaker_revision: int = -1


def _legacy_base_matches(store: Any, meeting: Meeting, transcript: Transcript) -> bool:
    """I-3: check the exact private bytes used by the enqueue descriptor."""
    if meeting.rediar is None:
        return transcript.meeting_generation == published_generation(meeting)
    if transcript.meeting_generation != published_generation(meeting):
        return False
    try:
        state = store.transcript_state(meeting.id)
        return bool(
            state.digest == meeting.rediar.base_sha256
            and state.generation == transcript.meeting_generation
            and state.revision == transcript.transcript_revision
        )
    except Exception:
        return False


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
    saveEchoChanged = pyqtSignal()
    _exportCompleted = pyqtSignal(object)
    # QML bindings run before ordinary slots of the same signal: «card» is notified by
    # its own signal, emitted after the cache is dropped (a stale cache never reaches QML).
    cardChanged = pyqtSignal()
    speakerOperationChanged = pyqtSignal()
    savePreviewOpenChanged = pyqtSignal()
    speakerOperationFinished = pyqtSignal(str)
    diarizationInstallRequested = pyqtSignal()
    diarizationInstallChanged = pyqtSignal()
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
        diar_models_provider: Callable[[], dict[str, str] | None] = installed_diar_models,
        diarization_service: Any | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._diar_models_provider = diar_models_provider
        self._speakers = SpeakerMap()
        self._saved_speakers = SpeakerMap()
        self._speaker_group = _SpeakerGroup()
        self._speaker_commands: deque[tuple[str, int, str, str, str]] = deque()
        self._speaker_busy = False
        self._speaker_closing = False
        self._speaker_pending = 0
        self._speaker_revision = 0
        self._speaker_writes: deque[_SpeakerWrite] = deque()
        self._speaker_writer = MetadataWriter()
        self._speaker_results: queue.SimpleQueue[tuple[_SpeakerWrite, bool]] = queue.SimpleQueue()
        self._speaker_timer = QTimer(self)
        self._speaker_timer.setInterval(10)
        self._speaker_timer.timeout.connect(self._speaker_poll)
        self._save_preview_open = False
        self._save_exclude_echo = False
        self._speakers_error = False
        self._speaker_operation: dict[str, Any] = {"ok": False, "code": "", "text": ""}
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
        self._card_results: queue.SimpleQueue[_CardRead | tuple[int, Transcript | None, bool]] = (
            queue.SimpleQueue()
        )
        self._redo_local: dict[str, str] = {}
        self._redo_requests: dict[str, tuple[int, str]] = {}
        self._redo_progress: dict[str, tuple[int, int]] = {}
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
        # (path, fmt, template, exclude_echo) awaiting «Заменить».
        self._replace_target: tuple[Path, str, str, bool] | None = None
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
        self._diar_downloads: Any = None
        self._diar_revoked: Callable[[], bool] = lambda: False
        self._diar_catalog_token: Callable[[], object] = lambda: None
        self._diar_revocation_token: object = None
        self._diar_code, self._diar_reason, self._diar_source = "missing", "", "hf"
        self._diar_busy = False
        self._diar_cancel_left = False
        self._diar_effective_enabled = bool(self._effective().settings.meeting_diarization)
        self._diar_percent = self._diar_accessible_percent = 0
        self._diar_last_progress = self._diar_last_accessible = float("-inf")
        self._diar_pending_percent: int | None = None
        self._diar_card_installed = ""
        self._section_window: QObject | None = None
        self._diar_install: dict[str, Any] = {}
        self._diar_progress_timer = QTimer(self)
        self._diar_progress_timer.setInterval(100)
        self._diar_progress_timer.timeout.connect(self._diar_progress_flush)
        self.changed.connect(self._refresh_diar_install_settings)
        if diarization_service is not None:
            self.bindDiarizationService(diarization_service)
        else:
            self.refreshDiarizationModels()

    def _effective(self) -> EffectiveMeetingSettings:
        return effective_settings(self._stored, self._parsed)

    def detector_allowed(self) -> bool:
        return self._effective().detector_allowed

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
        return (
            TEXTS["policyNote"]
            if any(decision.locked for decision in self._effective().controls.values())
            else ""
        )

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
            storage = meeting_card.audio_storage(meeting, short=True)
            if storage:
                subtitle += " · " + storage
        if not self.enabled:
            action = "Открыть" if state == MeetingState.DONE else ""
            actions = ["Сохранить как…", "Удалить"] if state == MeetingState.DONE else ["Удалить"]
        redo = self._redo_snapshot(meeting)
        if redo["busy"]:
            progress = int(redo["percent"])
            badge, kind = REDO_TEXTS["badge"].format(percent=progress), "warning"
            actions = [a for a in actions if a != "Имена участников"]
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
            "badgeIcon": "" if saving else "clock" if redo["busy"] else _BADGE_ICONS.get(state, ""),
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
            "diarizationRedo": redo,
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
        if self._diar_downloads is not None:
            self._diar_downloads.shutdown()
        self._diar_progress_timer.stop()
        self._speaker_closing = True
        if not self.flushSpeakerWrites():
            self._speaker_closing = False
            raise RuntimeError("meeting-speaker-save-timeout")
        self._speaker_writer.close(timeout=0.0)
        self._speaker_timer.stop()
        # Close W3 and W1 before our own exit, then the ordinary stop with
        # finalization (W2 still shows «Сохраняю запись…»), then W2.
        for name in ("_meeting_notifications", "_meeting_notice", "_meeting_offer"):
            owner = getattr(self, name, None)
            if owner is not None:
                owner.shutdown()
        if self._capture is not None:
            self._capture.shutdown()
        overlay = getattr(self, "_meeting_overlay", None)
        if overlay is not None:
            overlay.shutdown()
        self._closed = True
        self._section_window = None
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
        self.changed.connect(capture.sync_detector)
        capture.openMeetingsRequested.connect(self.openMeetingsRequested)
        capture.participantsRequested.connect(self.participantsRequested)
        self._capture_changed()

    def _capture_changed(self) -> None:
        self._overlay_capture()
        self._sync_card()
        self._observe_redo_results()
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
        if self._card_id and not self.flushSpeakerWrites():
            return
        self._save_template = TEMPLATES[0]
        self.setSaveExcludeEcho(False)
        self._diar_card_installed = ""
        self.refreshDiarizationModels()
        self._clear_redo_outcome()
        self._card_builder = None
        self._card_loaded = False
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
        self._speaker_closing = True
        if not self.flushSpeakerWrites():
            self._speaker_closing = False
            return
        if self._save_preview_open:
            self._save_preview_open = False
            self.savePreviewOpenChanged.emit()
        self.setSaveExcludeEcho(False)
        self._diar_card_installed = ""
        self._clear_redo_outcome()
        self._card_id = ""
        self._card_key = None
        self._card_builder = None
        self._card_transcript = None
        self._card_save_pending = False
        self._replace_target = None
        self._card_serial += 1  # late reads of the closed card are dropped
        self._card_rows.reset()
        self._card_timer.stop()
        self._speaker_closing = False
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
            self.saveEchoChanged.emit()
            self.savePreviewChanged.emit()

    @pyqtSlot(result="QVariantMap")
    def savePreview(self) -> dict[str, Any]:
        meeting = self._savable_meeting("txt")
        if meeting is None:
            return {}
        return meeting_card.save_preview(
            self._save_template,
            meeting,
            self._card_transcript,
            self._display_speakers(),
            exclude_echo=self._save_exclude_echo,
        )

    @pyqtProperty(bool, notify=saveEchoChanged)
    def saveExcludeEcho(self) -> bool:
        return self._save_exclude_echo

    @pyqtSlot(bool)
    def setSaveExcludeEcho(self, checked: bool) -> None:
        if self._save_exclude_echo != checked:
            self._save_exclude_echo = checked
            self.saveEchoChanged.emit()
            self.savePreviewChanged.emit()

    @pyqtProperty(int, notify=savePreviewChanged)
    def saveEchoCount(self) -> int:
        from astra_voice.meetings.templates import echo_count

        return echo_count(visible_utterances(self._card_transcript))

    @pyqtProperty(bool, notify=savePreviewChanged)
    def saveEchoVisible(self) -> bool:
        return self._save_template != "stats" and self.saveEchoCount > 0

    @pyqtProperty(str, notify=savePreviewChanged)
    def saveEchoAccessibleName(self) -> str:
        return f"{meeting_card.TEXTS['excludeEcho']}, {self.saveEchoCount}"

    @pyqtProperty(str, constant=True)
    def saveEchoDescription(self) -> str:
        return meeting_card.TEXTS["excludeEchoDescription"]

    @pyqtSlot(str, result=str)
    def savePreviewText(self, fmt: str) -> str:
        """Full literal content, identical to the selected export; bounded UI rows stay separate."""
        meeting = self._savable_meeting(fmt)
        if meeting is None:
            return ""
        return render(
            self._save_template,
            meeting,
            self._card_transcript,
            fmt,
            speakers=self._display_speakers(),
            exclude_echo=self._save_exclude_echo,
        )

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
        result = meeting_card.presentation(
            meeting,
            loaded=self._card_loaded,
            read_error=self._card_error,
            rows=builder,
            can_transcribe=self.enabled and jobs is not None,
            policy_off=self.sectionState == "policy-off",
            failure=failure,
            snapshot=self._card_progress if meeting.state == MeetingState.TRANSCRIBING else None,
            names_editable=self._names_editable(),
            models_missing=self._effective().settings.meeting_diarization
            and self._diar_code != "installed",
            rename_allowed=RENAME_ACTION in self._row(meeting)["actions"],
            deleting=meeting.id in self._delete_pending
            or (jobs is not None and meeting.id in jobs.tombstones),
        )
        show = bool(
            self._diar_install.get("visible")
            and not meeting.transcription.diarization
            and (self._diar_code != "installed" or self._diar_card_installed == meeting.id)
        )
        result["diarizationModelsMissing"] = bool(
            self._diar_install.get("visible")
            and not meeting.transcription.diarization
            and self._diar_code != "installed"
        )
        result["diarizationInstallVisible"] = show
        result["diarizationHint"] = self._diar_install["cardText"] if show else ""
        result["diarizationAction"] = self._diar_install["action"] if show else ""
        redo = self._redo_snapshot(meeting)
        result["diarizationRedo"] = redo
        result["expectedGeneration"] = redo["expectedGeneration"]
        if redo["busy"]:
            result["badge"] = {
                "text": REDO_TEXTS["badge"].format(percent=redo["percent"]),
                "kind": "warning",
                "icon": "clock",
            }
            result["namesHint"] = REDO_TEXTS["namesLater"]
        if redo["visible"]:
            result["diarizationInstallVisible"] = False
        return result

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
        generation = published_generation(meeting)
        if (
            meeting.rediar is not None
            and self._card_transcript is not None
            and self._card_transcript.meeting_generation == meeting.meeting_generation
        ):
            generation = meeting.meeting_generation  # K3 observed before K6
        key = (str(meeting.state), generation)
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
            if (
                meeting.state == MeetingState.DONE
                and self._card_loaded
                and self._card_builder is not None
                and not self._card_error
            ):
                # I-4: keep the entire previous pair while the new pair is being read.
                self._load_transcript(meeting)
                return
            self._card_tail = False
            self._card_builder = CardRows(meeting, self._card_rows)
            self._card_rows.reset()
            self._card_loaded = self._card_error = False
            self._card_transcript = None
            self._speakers = SpeakerMap()
            self._speakers_error = False
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
        builder.sync(live)

    def _load_transcript(self, meeting: Meeting) -> None:
        self._card_serial += 1
        serial, output = self._card_serial, self._card_results
        speaker_revision = self._speaker_revision
        store, meeting_id = self._store, meeting.id
        root = getattr(store, "root", None)

        def work() -> None:
            reader = getattr(store, "read", None)
            result = _CardRead(serial, None, failed=True)
            if reader is not None:
                for attempt in range(3):
                    try:
                        current = reader(meeting_id, "meeting.json")
                        if not isinstance(current, Meeting) or current.id != meeting_id:
                            break
                        transcript = reader(meeting_id, "transcript.json")
                        if not isinstance(transcript, Transcript):
                            break
                        try:
                            saved = reader(meeting_id, "speakers.json")
                        except Exception:
                            saved = None  # Optional names are never repaired on read.
                        after = reader(meeting_id, "meeting.json")
                        verify = reader(meeting_id, "transcript.json")
                        if not isinstance(after, Meeting) or after.id != meeting_id:
                            continue
                        if current != after or transcript != verify:
                            continue
                        accepted = transcript.meeting_id == meeting_id and (
                            transcript.meeting_generation == published_generation(after)
                            or (
                                after.state == MeetingState.DONE
                                and transcript.labels_final
                                and transcript.meeting_generation == after.meeting_generation
                            )
                        )
                        if not accepted:
                            if after.state != MeetingState.DONE:
                                result = _CardRead(serial, None)
                            continue
                        bound = isinstance(saved, SpeakerMap) and (
                            saved.transcript_generation == transcript.meeting_generation
                            or (
                                saved.transcript_generation is None
                                and _legacy_base_matches(store, after, transcript)
                            )
                        )
                        if isinstance(saved, SpeakerMap) and not bound:
                            if attempt < 2:
                                continue
                            # K3 before K4: only the NEW final text may lose stale names.
                            if not (
                                transcript.labels_final
                                and transcript.meeting_generation == after.meeting_generation
                                and (
                                    after.rediar is None
                                    or transcript.meeting_generation != published_generation(after)
                                )
                                and (
                                    saved.transcript_generation is None
                                    or saved.transcript_generation < transcript.meeting_generation
                                )
                            ):
                                continue
                        names = speaker_ops.speaker_map(
                            transcript.utterances, saved if bound else None
                        )
                        names.transcript_generation = transcript.meeting_generation
                        result = _CardRead(serial, transcript, names)
                        break
                    except Exception:
                        missing = root is not None and not os.path.lexists(
                            Path(root) / meeting_id / "transcript.json"
                        )
                        if missing and meeting.state != MeetingState.DONE:
                            result = _CardRead(serial, None)
            output.put(replace(result, speaker_revision=speaker_revision))

        self._submit(work)
        self._card_timer.start()

    def _card_poll(self) -> None:
        if self._closed:
            self._card_timer.stop()
            return
        if self._speaker_pending:
            return  # A read may predate an accepted name that is still being written.
        while True:
            try:
                result = self._card_results.get_nowait()
                if isinstance(result, tuple):  # Existing live-publication seam.
                    result = _CardRead(result[0], result[1], failed=result[2])
                serial, transcript, failed = result.serial, result.transcript, result.failed
            except queue.Empty:
                return
            if serial == self._card_serial:
                break  # Retire stale reads before adopting the current completed pair.
            if not self._card_loaded:
                return  # Preserve the deferred initial «Save as» delivery contract.
        self._card_timer.stop()
        meeting = self._card_meeting()
        if meeting is None or self._card_builder is None:
            return
        if result.speaker_revision >= 0 and result.speaker_revision != self._speaker_revision:
            # Never combine a stale map with newer optimistic or committed names.
            # Re-read the whole pair, retaining the completed snapshot until it arrives.
            self._load_transcript(meeting)
            return
        if failed and self._card_loaded and self._card_transcript is not None:
            # Keep the completed pair; retry is explicit, never a partial names update.
            self._card_save_error = REDO_TEXTS["readError"]
            self.changed.emit()
            return
        self._card_save_error = ""
        self._card_transcript = transcript
        self._card_loaded, self._card_error = True, failed
        self._speakers = result.speakers or SpeakerMap()
        self._saved_speakers = deepcopy(self._speakers)
        self._speaker_group = _SpeakerGroup()
        self._speakers_error = False
        if transcript is not None:
            self._card_key = (str(meeting.state), transcript.meeting_generation)
        utterances = visible_utterances(transcript)
        done = meeting.state == MeetingState.DONE
        tail, self._card_tail = self._card_tail, False
        self._card_builder.speakers = self._display_speakers()
        if tail and done and not failed and self._card_builder.continues(utterances):
            self._card_builder.finish(utterances)
        else:
            self._card_builder.load(utterances, complete=done)
        if meeting.state == MeetingState.TRANSCRIBING:
            self._append_live(meeting)
        self.savePreviewChanged.emit()
        self._observe_redo_results()
        self.changed.emit()
        self._flush_card_save()

    def _published_names(self) -> bool:
        meeting, transcript = self._card_meeting(), self._card_transcript
        return bool(
            meeting is not None
            and meeting.state == MeetingState.DONE
            and meeting.transcription.diarization
            and meeting.transcription.diar_status == "done"
            and self._card_loaded
            and not self._card_error
            and not self._card_tail
            and transcript is not None
            and transcript.labels_final
            and self._speakers.transcript_generation == transcript.meeting_generation
            and transcript.reconcile.status == PhaseStatus.DONE
            and transcript.reconcile.pending is None
            and transcript.reconcile.inflight_id is None
        )

    def _display_speakers(self) -> SpeakerMap | None:
        return self._speakers if self._published_names() and not self._speakers_error else None

    def _names_editable(self) -> bool:
        meeting = self._card_meeting()
        return bool(
            not self._closed
            and not self._speaker_closing
            and self._published_names()
            and not self._speakers_error
            and self._parsed.recording_allowed
            and meeting is not None
            and not meeting.cancelled
            and meeting.rediar is None
            and (self._jobs() is None or not self._jobs().names_busy(meeting.id))
            and meeting.id not in self._delete_pending
            and (self._jobs() is None or meeting.id not in self._jobs().tombstones)
        )

    @pyqtProperty("QVariantMap", notify=speakerOperationChanged)
    def speakerOperation(self) -> dict[str, Any]:
        return dict(self._speaker_operation)

    @pyqtProperty("QVariantMap", constant=True)
    def speakerTexts(self) -> dict[str, str]:
        return {
            "placeholder": "Как подписать",
            "closeNames": "Закрыть имена участников",
            "mergeTarget": "Это один человек",
            "merge": "Это один человек…",
            "mergeSuffix": " — это…",
            "mergeCaption": (
                "Выберите, кто это на самом деле. Реплики получат одну подпись, "
                "статистика пересчитается."
            ),
            "irreversible": "Разделить обратно будет нельзя",
            "cancel": "Отмена",
            "confirm": "Объединить",
            "namesNote": "Имена действуют только в этой встрече.",
            "namesLater": meeting_card.TEXTS["namesLater"],
        }

    @pyqtProperty(bool, notify=savePreviewOpenChanged)
    def savePreviewOpen(self) -> bool:
        return self._save_preview_open

    @savePreviewOpen.setter  # type: ignore[no-redef]
    def savePreviewOpen(self, opened: bool) -> None:
        if self._save_preview_open != opened:
            self._save_preview_open = opened
            self.savePreviewOpenChanged.emit()
            if opened:
                self.setSaveExcludeEcho(False)
                self.savePreviewChanged.emit()

    def bind_window(self, window: QObject | None) -> None:
        """Drain accepted names before a settings window closes or hides to tray."""
        if isinstance(window, QObject):
            window.installEventFilter(self)
            self._section_window = window
            section_changed = getattr(window, "currentSectionChanged", None)
            if section_changed is not None:
                section_changed.connect(self._window_section_changed)

    def _window_section_changed(self) -> None:
        window = self._section_window
        if window is None:
            return
        section = window.property("currentSection")
        if hasattr(section, "toVariant"):
            section = section.toVariant()
        if isinstance(section, dict) and section.get("key") != "meetings":
            self.leaveMeetingsSection()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802
        if event.type() == QEvent.Close and not self.flushSpeakerWrites():
            event.ignore()
            return True
        if event.type() in (QEvent.Close, QEvent.Hide):
            self.leaveMeetingsSection()
        return bool(super().eventFilter(watched, event))

    def _speaker_result(self, ok: bool, code: str, text: str = "") -> bool:
        self._speaker_operation = {"ok": ok, "code": code, "text": text}
        self.speakerOperationChanged.emit()
        if code != "pending":
            self.speakerOperationFinished.emit(code)
        return ok

    def _apply_speakers(self, updated: SpeakerMap) -> None:
        """Optimistic view/rollback; normal renames never reset the reply model."""
        self._speaker_revision += 1
        previous = self._speakers
        self._speakers = updated
        builder = self._card_builder
        if builder is None:
            return
        merged = any(
            previous.labels.get(label) is None
            or previous.labels[label].merged_into != record.merged_into
            for label, record in updated.labels.items()
        )
        if merged:
            builder.speakers = updated
            builder.load(list(builder.utterances), complete=True)
            self._card_cache = None
        else:
            changed = {
                label
                for label, record in updated.labels.items()
                if previous.labels.get(label) != record
            }
            for label in changed:
                builder.rename(updated, label)
            builder.speakers = updated
            if self._card_cache is not None:
                participants = []
                for row in self._card_cache["participants"]:
                    patch = dict(row) if row["id"] in changed else row
                    if row["id"] in changed:
                        patch["label"] = speaker_ops.speaker_label(
                            builder.meeting, row["id"], updated
                        )
                        patch["name"] = updated.labels[row["id"]].name or ""
                        patch["inputAccessibleName"] = f"Как подписать: {patch['label']}"
                        patch["mergeTitle"] = f"{patch['label']} — это…"
                    if any(c["id"] in changed for c in row["mergeCandidates"]):
                        patch = dict(patch)
                        patch["mergeCandidates"] = [
                            {
                                **c,
                                "label": speaker_ops.speaker_label(
                                    builder.meeting, c["id"], updated
                                ),
                            }
                            if c["id"] in changed
                            else c
                            for c in row["mergeCandidates"]
                        ]
                    participants.append(patch)
                self._card_cache = {**self._card_cache, "participants": participants}
        self.cardChanged.emit()
        if self._save_preview_open:
            self.savePreviewChanged.emit()

    def _save_speakers(self, updated: SpeakerMap, code: str, text: str) -> bool:
        meeting = self._card_meeting()
        assert meeting is not None
        write = _SpeakerWrite(
            meeting.id, meeting.meeting_generation, updated, self._speaker_group, code, text
        )
        if self._speaker_pending >= 64:
            return self._speaker_result(
                False, "save-error", "Не удалось сохранить имена участников"
            )
        self._speaker_writes.append(write)
        self._speaker_pending += 1
        self._apply_speakers(updated)
        self._speaker_result(True, "pending")
        self._speaker_timer.start()
        return True

    def _submit_speaker_writes(self) -> None:
        # Start filesystem work at the next GUI turn, outside the typing slot.
        store, output = self._store, self._speaker_results
        while self._speaker_writes:
            write = self._speaker_writes.popleft()

            def work(write: _SpeakerWrite = write) -> None:
                ok = False
                if not write.group.failed.is_set():
                    try:
                        jobs = self._jobs()
                        if jobs is not None and jobs.names_busy(write.meeting_id):
                            raise RuntimeError("meeting-names-busy")
                        reader = getattr(store, "read", None)
                        if reader is None:
                            raise RuntimeError("meeting-reader-unavailable")
                        current = reader(write.meeting_id, "meeting.json")
                        if current.rediar is not None:
                            raise RuntimeError("meeting-names-busy")
                        write_file = getattr(store, "write", None)
                        if write_file is None:
                            raise RuntimeError("meeting-writer-unavailable")
                        write_file(
                            write.meeting_id,
                            "speakers.json",
                            write.speakers,
                            writer=Writer.GUI,
                            expected_generation=write.generation,
                        )
                        ok = True
                    except Exception:
                        write.group.failed.set()
                output.put((write, ok))

            try:
                self._speaker_writer.submit(work)
            except (RuntimeError, queue.Full):
                write.group.failed.set()
                output.put((write, False))

    def _execute_speaker(self, command: tuple[str, int, str, str, str]) -> bool:
        meeting_id, generation, kind, source, value = command
        meeting = self._card_meeting()
        if (
            not self._names_editable()
            or meeting is None
            or meeting.id != meeting_id
            or meeting.meeting_generation != generation
        ):
            return self._speaker_result(False, "unavailable", "Имена сейчас недоступны")
        try:
            updated = (
                speaker_ops.rename(self._speakers, source, value)
                if kind == "rename"
                else speaker_ops.merge(self._speakers, source, value)
            )
        except ValueError:
            return self._speaker_result(
                False,
                "invalid-name" if kind == "rename" else "invalid-merge",
                "Проверьте имя участника: до 60 символов, без переводов строк"
                if kind == "rename"
                else "Нельзя объединить этих участников",
            )
        text = (
            (
                f"Спикер {speaker_ops.speaker_number(source)} объединён с «"
                + speaker_ops.speaker_label(meeting, value, updated)
                + "» · статистика пересчитана"
            )
            if kind == "merge"
            else ""
        )
        return self._save_speakers(updated, "renamed" if kind == "rename" else "merged", text)

    def _drain_speaker_commands(self) -> bool:
        self._speaker_busy = True
        results = []
        try:
            while self._speaker_commands:
                results.append(self._execute_speaker(self._speaker_commands.popleft()))
        finally:
            self._speaker_busy = False
        return results[0] if results else True

    def _speaker_command(self, kind: str, source: str, value: str) -> bool:
        meeting = self._card_meeting()
        self._speaker_commands.append(
            (self._card_id, meeting.meeting_generation if meeting else -1, kind, source, value)
        )
        if self._speaker_busy:
            return True  # Queued, not executed recursively; terminal result comes later.
        return self._drain_speaker_commands()

    @pyqtSlot(str, str, result=bool)
    def renameSpeaker(self, label: str, name: str) -> bool:
        return self._speaker_command("rename", label, name)

    @pyqtSlot(str, str, result=bool)
    def mergeSpeakers(self, source: str, target: str) -> bool:
        return self._speaker_command("merge", source, target)

    def _speaker_poll(self) -> None:
        if self._speaker_busy:
            return
        self._submit_speaker_writes()
        self._speaker_busy = True
        try:
            while True:
                try:
                    write, ok = self._speaker_results.get_nowait()
                except queue.Empty:
                    break
                self._speaker_pending -= 1
                self._speaker_revision += 1  # Also retire reads made during the write.
                meeting = self._card_meeting()
                current = (
                    meeting is not None
                    and meeting.id == write.meeting_id
                    and self._card_transcript is not None
                    and self._card_transcript.meeting_generation
                    == write.speakers.transcript_generation
                )
                if current and ok:
                    self._saved_speakers = write.speakers
                elif current and write.group is self._speaker_group:
                    self._speaker_group = _SpeakerGroup()
                    self._apply_speakers(self._saved_speakers)
                self._speaker_result(
                    ok,
                    write.code if ok else "save-error",
                    write.text if ok else "Не удалось сохранить имена участников",
                )
        finally:
            self._speaker_busy = False
        if self._speaker_commands:
            self._drain_speaker_commands()
        if not self._speaker_pending:
            self._speaker_timer.stop()

    @pyqtSlot(result=bool)
    def flushSpeakerWrites(self) -> bool:
        """Bounded close barrier; failure keeps the card/window open."""
        if self._speaker_busy:
            return False
        deadline = time.monotonic() + 2.0
        while True:
            self._submit_speaker_writes()
            if not self._speaker_writer.close(timeout=max(0, deadline - time.monotonic())):
                self._speaker_result(False, "save-error", "Не удалось сохранить имена участников")
                return False
            self._speaker_writer = MetadataWriter()
            self._speaker_poll()
            if not self._speaker_pending and not self._speaker_commands:
                return True
            if time.monotonic() >= deadline:
                return False

    def bindDiarizationService(
        self,
        service: Any,
        *,
        revoked: Callable[[], bool] = lambda: False,
        catalog_token: Callable[[], object] = lambda: None,
    ) -> None:
        """Bind once, before QML can request installation; no network here."""
        from astra_voice.ui.model_downloads import DiarizationDownloads

        if self._diar_downloads is not None:
            raise RuntimeError("meeting-model-service-already-bound")
        self._diar_downloads = DiarizationDownloads(service, self)
        downloads = self._diar_downloads

        def trusted_models() -> dict[str, str] | None:
            if downloads._thread is not None and self._diar_code in {
                "downloading",
                "verifying",
                "installing",
            }:
                return None
            return nonblocking_diar_models(service.trusted_models)

        self._diar_models_provider = trusted_models
        self._diar_revoked, self._diar_catalog_token = revoked, catalog_token
        self._diar_downloads.jobEvent.connect(self._diar_event)
        self.diarizationInstallRequested.connect(self._start_diar_install)
        self.refreshDiarizationModels()

    @pyqtProperty("QVariantMap", notify=diarizationInstallChanged)
    def diarizationInstall(self) -> dict[str, Any]:
        return dict(self._diar_install)

    def _publish_diar_install(self) -> None:
        state = diar_install_presentation(
            self._diar_code,
            self._diar_percent,
            self._diar_source,
            reason_code=self._diar_reason,
            busy=self._diar_busy,
        )
        state["visible"] = bool(self._effective().settings.meeting_diarization)
        state["accessiblePercent"] = self._diar_accessible_percent
        if state != self._diar_install:
            self._diar_install = state
            self._card_cache = None
            self.diarizationInstallChanged.emit()
            self.cardChanged.emit()

    def _refresh_diar_install_settings(self) -> None:
        # General meeting changes also carry recording progress. Do not hash the
        # pair (34 MB) on every UI notification: jobs and explicit model refreshes
        # validate trust; settings only update row visibility and the network gate.
        enabled = bool(self._effective().settings.meeting_diarization)
        was_enabled, self._diar_effective_enabled = self._diar_effective_enabled, enabled
        if enabled and not was_enabled:
            self.refreshDiarizationModels()
            return
        if self._diar_code in {"missing", "offline-user", "policy"}:
            refusal = NetworkGate(self._stored, self._policy).refusal("download")
            self._diar_code = (
                "policy" if refusal == "admin" else "offline-user" if refusal else "missing"
            )
        self._publish_diar_install()

    @pyqtSlot()
    def refreshDiarizationModels(self) -> None:
        """Refresh trust/policy after catalog, settings or installation changes."""
        if self._closed:
            return
        self._diar_effective_enabled = bool(self._effective().settings.meeting_diarization)
        if not self._diar_effective_enabled:
            self._publish_diar_install()
            return
        if self._diar_downloads is None or self._diar_downloads._thread is None:
            if self._diar_revoked():
                self._diar_code, self._diar_reason = "broken", "revoked"
                self._diar_revocation_token = self._diar_catalog_token()
            elif self._diar_models_provider() is not None:
                self._diar_code, self._diar_reason = "installed", ""
            elif self._diar_code in {"missing", "installed", "offline-user", "policy"} or (
                self._diar_reason == "revoked"
                and self._diar_catalog_token() != self._diar_revocation_token
            ):
                refusal = NetworkGate(self._stored, self._policy).refusal("download")
                self._diar_code = (
                    "policy" if refusal == "admin" else "offline-user" if refusal else "missing"
                )
                self._diar_reason = ""
        self._publish_diar_install()

    @pyqtSlot()
    def leaveMeetingsSection(self) -> None:
        if self._diar_downloads is not None and self._diar_downloads.cancel.is_set():
            self._diar_cancel_left = True
        if self._diar_code == "cancelled":
            self._diar_code = "missing"
            self.refreshDiarizationModels()

    @pyqtSlot()
    def requestDiarizationInstall(self) -> None:
        if self._closed:
            return
        self.refreshDiarizationModels()
        if self._diar_install.get("visible") and self._diar_install.get("action") in {
            DIAR_INSTALL_TEXTS["install"],
            DIAR_INSTALL_TEXTS["retry"],
            DIAR_INSTALL_TEXTS["reinstall"],
        }:
            # A settings/policy change after a failure must still close the network.
            if NetworkGate(self._stored, self._policy).refusal("download"):
                return
            self.diarizationInstallRequested.emit()

    def _start_diar_install(self) -> None:
        downloads = self._diar_downloads
        if (
            self._closed
            or downloads is None
            or downloads._thread is not None
            or not self._effective().settings.meeting_diarization
            or NetworkGate(self._stored, self._policy).refusal("download")
        ):
            return
        self._diar_cancel_left = False
        self._diar_code, self._diar_reason, self._diar_source = "downloading", "", "hf"
        self._diar_percent = self._diar_accessible_percent = 0
        self._diar_last_progress = self._diar_last_accessible = self._clock()
        self._diar_busy = False
        self._publish_diar_install()
        downloads.start()

    @pyqtSlot()
    def cancelDiarizationInstall(self) -> None:
        if self._diar_downloads is not None:
            if not self._diar_downloads.cancel.is_set():
                self._diar_cancel_left = False
            self._diar_downloads.cancel.set()

    def _diar_progress_flush(self) -> None:
        now = self._clock()
        if now - self._diar_last_progress < 0.1 - 1e-9:
            return
        updated = False
        if self._diar_pending_percent is not None:
            self._diar_percent, self._diar_pending_percent = self._diar_pending_percent, None
            updated = True
        if now - self._diar_last_accessible >= 1.0 - 1e-9:
            if self._diar_accessible_percent != self._diar_percent:
                self._diar_accessible_percent = self._diar_percent
                updated = True
            self._diar_last_accessible = now
        if updated:
            self._diar_last_progress = now
            self._publish_diar_install()

    def _diar_event(self, kind: str, args: Any) -> None:
        if self._closed:
            return
        if kind == "progress":
            self._diar_pending_percent = max(0, min(100, int(float(args[0]) * 100)))
            self._diar_progress_flush()
            return
        if kind == "source":
            self._diar_source = str(args[0])
        elif kind == "stage":
            if args[0] not in {"downloading", "verifying", "installing"}:
                return
            self._diar_code = str(args[0])
            if self._diar_code in {"verifying", "installing"}:
                self._diar_percent = 100
                self._diar_pending_percent = None
            self._diar_progress_timer.start()
        elif kind == "finished":
            code, reason, reason_code = args
            self._diar_code = (
                code
                if code
                in {
                    "installed",
                    "cancelled",
                    "no-space",
                    "broken",
                    "failed:offline",
                    "failed:settings",
                    "failed:admin",
                }
                else "broken"
            )
            self._diar_reason = reason_code
            if reason_code == "revoked":
                self._diar_revocation_token = self._diar_catalog_token()
            self._diar_busy = reason == DIAR_INSTALL_TEXTS["busy"]
            self._diar_pending_percent = None
            self._diar_progress_timer.stop()
            if self._diar_code == "cancelled" and self._diar_cancel_left:
                self._diar_code = "missing"
                self._refresh_diar_install_settings()
                return
            if self._diar_code == "installed":
                self._diar_percent = 100
                meeting = self._card_meeting()
                if meeting is not None and not meeting.transcription.diarization:
                    self._diar_card_installed = meeting.id
        self._publish_diar_install()

    def _clear_redo_outcome(self) -> None:
        jobs = self._jobs()
        if jobs is not None:
            jobs.redo_results.pop(self._card_id, None)
        self._redo_local.pop(self._card_id, None)

    def _observe_redo_results(self) -> None:
        """Turn a failed enqueue into the same card-local outcome as a failed run.

        Requests outlive D4 and card navigation while the queue still owns them;
        terminal outcomes use the existing close-card lifetime, independently of exports.
        """
        if not self._redo_requests or self._closed:
            return
        jobs = self._jobs()
        if jobs is None:
            return
        meetings = {meeting.id: meeting for meeting in self._documents}
        meetings.update(jobs.meetings)
        for meeting_id, (generation, outcome) in list(self._redo_requests.items()):
            meeting = meetings.get(meeting_id)
            if (
                meeting is None
                or meeting_id in jobs.tombstones
                or published_generation(meeting) != generation
                or meeting.rediar is not None
            ):
                self._redo_requests.pop(meeting_id, None)
                continue
            snapshot = jobs.redo_snapshot(meeting_id)
            if snapshot.get("outcome"):
                self._redo_requests.pop(meeting_id, None)
            elif not snapshot.get("busy") and jobs.failures.get(meeting_id) == "io":
                self._redo_local[meeting_id] = outcome
                self._redo_requests.pop(meeting_id, None)

    def _redo_snapshot(self, meeting: Meeting) -> dict[str, Any]:
        jobs = self._jobs()
        snapshot = jobs.redo_snapshot(meeting.id) if jobs is not None else {}
        busy = bool(snapshot.get("busy") or meeting.rediar is not None)
        status = snapshot.get("status", "")
        if not status and meeting.rediar is not None:
            status = "recovering"
        can_cancel = busy and status not in {
            "recovery_error",
            "recovering",
            "committing",
            "cancelling",
        }
        separated = (
            meeting.transcription.diarization and meeting.transcription.diar_status == "done"
        )
        failed = meeting.transcription.diar_status == "failed"
        available = bool(
            self.enabled
            and self._effective().settings.meeting_diarization
            and self._diar_code == "installed"
            and meeting.audio.present
            and not meeting.cleanup_pending
            and not meeting.cancelled
            and meeting.state == MeetingState.DONE
            and meeting.id not in self._delete_pending
            and (jobs is None or meeting.id not in jobs.tombstones)
        )
        outcome = self._redo_local.get(meeting.id, "") or snapshot.get("outcome", "")
        percent = max(0, min(100, int(snapshot.get("percent", 0))))
        if busy:
            old_generation, old_percent = self._redo_progress.get(meeting.id, (-1, 0))
            if old_generation == meeting.meeting_generation:
                percent = max(percent, old_percent)
            self._redo_progress[meeting.id] = (meeting.meeting_generation, percent)
            code = (
                "recovery_error"
                if status == "recovery_error"
                else "queued"
                if status == "queued"
                else "wait-memory"
                if status == "waiting_memory"
                else "reconcile"
                if snapshot.get("stage") == "reconcile" or status == "committing"
                else "running"
            )
        elif outcome:
            code = str(outcome)
            if code == "cancelled" and not failed and not separated:
                code = "offer" if available else ""
            if code == "kept":
                code = (
                    "kept-deleted"
                    if not meeting.audio.present
                    else "kept-pending"
                    if meeting.cleanup_pending
                    else f"kept-{meeting.audio.retain_days}"
                    if meeting.audio.retain_days in {7, 30}
                    else "kept-retained"
                )
        elif meeting.state == MeetingState.DONE and failed:
            code = "failed" if meeting.audio.present else "no-audio"
        elif (
            available
            and not separated
            and not meeting.transcription.diarization
            and meeting.processing_status.value == "succeeded"
        ):
            code = "offer"
        else:
            code = ""
        if not busy and meeting.cleanup_pending:
            if outcome == "kept" and not failed and meeting.audio.present:
                # Keep the current acceptance receipt while deletion is unconfirmed.
                code = "kept-pending"
            else:
                code = "no-audio" if failed else ""
        elif not busy and not meeting.audio.present:
            if failed:
                code = "no-audio"
            elif not separated:
                if code in {"failed-again", "cancelled", "offer"}:
                    code = ""
                elif code.startswith("kept-"):
                    # §Р.2б keeps the immediate zero-day receipt; an expired offer
                    # under §Р.2а disappears, even while its transient outcome lives.
                    code = "kept-deleted" if meeting.audio.retain_days == 0 else ""
        date = meeting_card.audio_expiry(meeting)
        state: dict[str, Any] = dict(
            redo_presentation(
                code,
                percent,
                room=meeting.variant == "mic_only",
                first=not separated and not failed,
                date=date,
                installed=self._diar_card_installed == meeting.id,
                separated=separated,
            )
        )
        if code == "no-audio" and meeting.cleanup_pending and meeting.audio.present:
            state["text"] = REDO_TEXTS["prefix"] + meeting_card.TEXTS["audioDeleting"]
        generation = (
            self._card_transcript.meeting_generation
            if meeting.id == self._card_id and self._card_transcript is not None
            else published_generation(meeting)
        )
        names = self._display_speakers() if meeting.id == self._card_id else None
        warning = bool(names and any(r.name or r.merged_into for r in names.labels.values()))
        listed_names = ", ".join(r.name for r in names.labels.values() if r.name) if names else ""
        can_start = available and not busy and jobs is not None
        can_keep = bool(
            not busy
            and meeting.state == MeetingState.DONE
            and meeting.audio.present
            and not meeting.cleanup_pending
            and (failed or code == "failed-again")
            and jobs is not None
        )
        action = (
            "cancel"
            if can_cancel
            else "again"
            if can_start and code in {"failed", "failed-again", "cancelled"}
            else "divide"
            if can_start and (code == "offer" or code.startswith("kept-"))
            else ""
        )
        retain = self._effective().settings.meeting_audio_retention_days
        try:
            now = jobs.wall_clock() if jobs is not None else datetime.now().astimezone()
            keep_date = f"{now.astimezone() + timedelta(days=retain):%d.%m}"
        except OverflowError:
            keep_date = ""
        state.update(
            {
                "meetingId": meeting.id,
                "expectedGeneration": generation,
                "busy": busy,
                "stage": str(snapshot.get("stage", "")),
                "canStart": can_start,
                "canCancel": can_cancel,
                "canKeep": can_keep,
                "menuVisible": available and separated,
                "menuEnabled": can_start and separated,
                "menuText": REDO_TEXTS["again"] + ("…" if warning else ""),
                "needsWarning": warning,
                "warningTitle": REDO_TEXTS["warningTitle"].format(title=meeting.title),
                "warningText": REDO_TEXTS["warningText"]
                + (REDO_TEXTS["warningNames"].format(names=listed_names) if listed_names else ""),
                "warningCaption": REDO_TEXTS["warningCaption"],
                "action": REDO_TEXTS[action] if action else "",
                "actionName": REDO_TEXTS[action + "Name"] if action else "",
                "actionDescription": (
                    REDO_TEXTS["divideDescription"].format(date=date)
                    if action == "divide"
                    else state["text"]
                ),
                "keepAction": REDO_TEXTS["keep"] if can_keep else "",
                "keepName": REDO_TEXTS["keepName"],
                "keepDescription": (
                    REDO_TEXTS["divideDescription"].format(date=date)
                    if not failed
                    else REDO_TEXTS["keepRetainDescription"].format(date=keep_date)
                    if retain
                    else REDO_TEXTS["keepDeleteDescription"]
                ),
                "audioPresent": meeting.audio.present,
                "cleanupPending": meeting.cleanup_pending,
                "audioUntil": date,
                "storageText": meeting_card.audio_storage(meeting),
                "namesEnabled": self._names_editable() if meeting.id == self._card_id else False,
                "saveEnabled": meeting.state == MeetingState.DONE,
                "renameEnabled": meeting.state in meeting_card.RENAME_STATES,
                "deleteEnabled": meeting.state == MeetingState.DONE,
            }
        )
        return state

    @pyqtProperty("QVariantMap", notify=cardChanged)
    def diarizationRedo(self) -> dict[str, Any]:
        meeting = self._card_meeting()
        return self._redo_snapshot(meeting) if meeting is not None else {}

    @pyqtProperty("QVariantMap", constant=True)
    def diarizationRedoTexts(self) -> dict[str, str]:
        return dict(REDO_TEXTS)

    def _redo_target(self, meeting_id: str, expected_generation: int) -> Meeting | None:
        if (
            self._closed
            or self._card_id != meeting_id
            or self._card_transcript is None
            or self._card_transcript.meeting_generation != expected_generation
            or self._jobs() is None
            or meeting_id in self._delete_pending
            or meeting_id in self._jobs().tombstones
        ):
            return None
        try:
            meeting = self._store.read(meeting_id, "meeting.json")  # type: ignore[attr-defined]
        except Exception:
            return None
        return (
            meeting
            if (
                isinstance(meeting, Meeting)
                and meeting.id == meeting_id
                and meeting.state == MeetingState.DONE
                and not meeting.cancelled
                and published_generation(meeting) == expected_generation
            )
            else None
        )

    def _start_redo(self, meeting_id: str, expected_generation: int, *, confirmed: bool) -> bool:
        self.refreshDiarizationModels()
        meeting = self._redo_target(meeting_id, expected_generation)
        if meeting is None or not self.flushSpeakerWrites():
            return False
        # Re-read after the names barrier: policy, disk and the open card may have changed.
        meeting = self._redo_target(meeting_id, expected_generation)
        if meeting is None:
            return False
        state = self._redo_snapshot(meeting)
        if (
            not state["canStart"]
            or self._jobs().names_busy(meeting_id)
            or (state["needsWarning"] and not confirmed)
            or self._diar_models_provider() is None
        ):
            return False
        self._redo_local.pop(meeting_id, None)
        self._redo_progress.pop(meeting_id, None)
        separated = (
            meeting.transcription.diarization and meeting.transcription.diar_status == "done"
        )
        self._redo_requests[meeting_id] = (
            expected_generation,
            "redo-failed" if separated else "failed-again",
        )
        self._jobs().failures.pop(meeting_id, None)  # Only errors of this new attempt count.
        self._jobs().rediarize(meeting_id)
        self._observe_redo_results()
        self.changed.emit()
        return True

    @pyqtSlot(str, int, bool, result=bool)
    def rediarizeMeeting(self, meeting_id: str, expected_generation: int, confirmed: bool) -> bool:
        return self._start_redo(meeting_id, expected_generation, confirmed=confirmed)

    @pyqtSlot(str, int, result=bool)
    def divideMeeting(self, meeting_id: str, expected_generation: int) -> bool:
        meeting = self._redo_target(meeting_id, expected_generation)
        if meeting is None or meeting.transcription.diarization:
            return False
        return self._start_redo(meeting_id, expected_generation, confirmed=False)

    @pyqtSlot(str, int, result=bool)
    def cancelDiarizationRedo(self, meeting_id: str, expected_generation: int) -> bool:
        meeting = self._redo_target(meeting_id, expected_generation)
        if meeting is None or not self._redo_snapshot(meeting)["canCancel"]:
            return False
        self._jobs().cancel(meeting_id)
        self.changed.emit()
        return True

    @pyqtSlot(str, int, result=bool)
    def keepDiarizationAsIs(self, meeting_id: str, expected_generation: int) -> bool:
        meeting = self._redo_target(meeting_id, expected_generation)
        if meeting is None or not self._redo_snapshot(meeting)["canKeep"]:
            return False
        if meeting.rediar is not None or self._jobs().names_busy(meeting_id):
            return False
        if meeting.transcription.diar_status != "failed":
            # §Р.2а: close only the transient error, without touching its retention clock.
            self._redo_local.pop(meeting_id, None)
            self._jobs().redo_results.pop(meeting_id, None)
            self.changed.emit()
            return True
        serial = self._card_serial

        def completed(ok: bool) -> None:
            if (
                ok
                and not self._closed
                and serial == self._card_serial
                and self._card_id == meeting_id
            ):
                self._redo_local[meeting_id] = "kept"
                self._overlay_capture()
                self.changed.emit()

        self._jobs().keep_as_is(meeting_id, meeting.meeting_generation, completed)
        return True

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
        exclude_echo = self._save_exclude_echo
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
            meeting,
            with_extension(Path(chosen), fmt),
            fmt,
            template,
            overwrite=False,
            exclude_echo=exclude_echo,
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
        target, fmt, template, exclude_echo = pending
        meeting = self._savable_meeting(fmt)
        if meeting is None:
            self.changed.emit()
            return "error"
        self._export_id = meeting.id
        self._export_busy = True
        return self._write_card(
            meeting, target, fmt, template, overwrite=True, exclude_echo=exclude_echo
        )

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
        self,
        meeting: Meeting,
        target: Path,
        fmt: str,
        template: str,
        *,
        overwrite: bool,
        exclude_echo: bool = False,
    ) -> str:
        # Plain detached values only: the worker never reads card/bridge state.
        snapshot, transcript = deepcopy(meeting), deepcopy(self._card_transcript)
        speakers = deepcopy(self._display_speakers())
        serial, emit = self._card_serial, self._exportCompleted.emit

        def work() -> None:
            status = "saved"
            try:
                text = render(
                    template,
                    snapshot,
                    transcript,
                    fmt,
                    speakers=speakers,
                    exclude_echo=exclude_echo,
                )
                write_export(target, text, overwrite=overwrite)
            except ExportExists:
                status = "exists"
            except Exception:
                status = "error"
            finally:
                try:
                    emit(_ExportResult(serial, target, fmt, status, template, exclude_echo))
                except RuntimeError:
                    pass  # QObject already destroyed during application shutdown

        try:
            self._export_writer.submit(work)
        except (RuntimeError, queue.Full):
            self._export_completed(
                _ExportResult(serial, target, fmt, "error", template, exclude_echo)
            )
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
            self._replace_target = (target, result.fmt, result.template, result.exclude_echo)
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
