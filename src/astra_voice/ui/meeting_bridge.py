"""E13 section presentation and human opt-in; no recorder or detector.

Only D1 acceptance enables the feature. Reads run in a daemon that owns no Qt
objects; a parented timer consumes plain results and shutdown ignores late work.
"""

from __future__ import annotations

import queue
import threading
import time
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any, Protocol
from urllib.parse import urlsplit

from PyQt5.QtCore import QObject, QTimer, pyqtProperty, pyqtSignal, pyqtSlot

from astra_voice.core.diagnostics import endpoint_class
from astra_voice.core.policy import Policy
from astra_voice.core.settings import Settings
from astra_voice.core.settings import save as settings_save
from astra_voice.meetings.apps import AppClass, AppDataError, AppDefinition, load_apps
from astra_voice.meetings.events import MeetingState, PolicyCode
from astra_voice.meetings.model import Meeting, normalize_text
from astra_voice.meetings.policy import RecordMode, parse_policy, validate_endpoint
from astra_voice.meetings.settings_ext import (
    MEETING_DEFAULTS,
    EffectiveMeetingSettings,
    effective_settings,
)
from astra_voice.meetings.store import MeetingStore
from astra_voice.platform.session import SessionKind
from astra_voice.ui.meeting_texts import RESPONSIBILITY_ITEMS, TEXTS

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


def _duration(milliseconds: int) -> str:
    seconds = milliseconds // 1000
    return f"{seconds // 3600}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"


def _megabytes(size: int) -> str:
    return f"{size / (1024 * 1024):.1f}".rstrip("0").rstrip(".").replace(".", ",")


def _count(count: int) -> str:
    if 11 <= count % 100 <= 14:
        return "встреч"
    return "встреча" if count % 10 == 1 else "встречи" if 2 <= count % 10 <= 4 else "встреч"


class MeetingBridge(QObject):
    changed = pyqtSignal()
    enabledChanged = pyqtSignal(bool)
    recordingRequested = pyqtSignal()
    meetingActionRequested = pyqtSignal(str, str)

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
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._stored, self._mirror, self._save = stored, mirror, save
        self._policy = policy or Policy()
        self._parsed = parse_policy(self._policy)
        self._session = session
        self._store: MeetingListing = MeetingStore() if store is None else store
        self._apps = known_apps
        self._submit, self._clock = submit, clock
        self._documents: list[Meeting] = []
        self._queue: queue.Queue[LoadResult] = queue.Queue(maxsize=1)
        self._loading = self._loading_visible = self._read_error = False
        self._save_error = ""
        self._started = 0.0
        self._responsibility_open = self._recording = self._closed = False
        self._timer = QTimer(self)
        self._timer.setInterval(20)
        self._timer.timeout.connect(self._poll)

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

    @pyqtProperty(str, notify=changed)
    def flyWarning(self) -> str:
        return TEXTS["flyWarning"] if self._session == SessionKind.FLY else ""

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
                ["Переименовать", "Удалить"],
            ),
            MeetingState.TRANSCRIBING: (
                "Расшифровка · 0 %",
                "warning",
                "Отменить",
                ["Переименовать", "Удалить"],
            ),
            MeetingState.WAITING_MEMORY: ("Ждёт памяти", "warning", "", ["Отменить", "Удалить"]),
            MeetingState.NEEDS_MODEL: ("Нужна модель", "warning", "Установить", ["Удалить"]),
            MeetingState.DONE: (
                "✓ Готово",
                "success",
                "Открыть",
                ["Сохранить как…", "Имена участников", "Переименовать", "Удалить"],
            ),
            MeetingState.FAILED: ("! Ошибка расшифровки", "danger", "Повторить", ["Удалить"]),
            MeetingState.INTERRUPTED: ("Прервана", "warning", "Расшифровать", ["Удалить"]),
            MeetingState.DELETING: ("", "neutral", "", []),
        }[state]
        if state == MeetingState.RECORDING:
            subtitle = date
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
        return {
            "id": meeting.id,
            "title": meeting.title,
            "subtitle": subtitle,
            "badge": badge,
            "badgeKind": kind,
            "action": action,
            "actions": actions,
            "progress": 0,
            "active": state in {MeetingState.RECORDING, MeetingState.PAUSED},
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
        self._timer.stop()
        self.changed.emit()

    def shutdown(self) -> None:
        self._closed = True
        self._timer.stop()
        self._loading = self._loading_visible = False
        self._responsibility_open = False

    def refreshPolicy(self, policy: Policy) -> None:
        old_enabled = self.enabled
        self._policy, self._parsed = policy, parse_policy(policy)
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
            self.recordingRequested.emit()

    @pyqtSlot(str, str)
    def requestMeetingAction(self, meeting_id: str, action: str) -> None:
        for row in self.meetings:
            if row["id"] == meeting_id and action and action in [row["action"], *row["actions"]]:
                self.meetingActionRequested.emit(meeting_id, action)
                return

    @pyqtSlot(result=bool)
    def copyInviteText(self) -> bool:
        from PyQt5.QtWidgets import QApplication

        clipboard = QApplication.clipboard()
        if clipboard is None:
            return False
        clipboard.setText(self.settings["meeting_invite_text"])
        return True
