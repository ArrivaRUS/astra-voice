"""Card A «Документ» presentation (E13.M4): header, marks, rows and state panels.

design/meetings/spec.md §3.7/§4.7/§5.2, tokens.md §3. Replies are an
append-only list model: a running transcription adds rows without rebuilding
the table (≤ 9000 rows). Text values are plain strings; QML shows them with
``Text.PlainText`` (MN-1). Names and speaker numbers arrive with M7: in M4 the
table says «Я» / «Собеседники».
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timedelta
from itertools import chain
from typing import Any

from PyQt5.QtCore import (
    QAbstractListModel,
    QByteArray,
    QModelIndex,
    Qt,
    pyqtProperty,
    pyqtSignal,
    pyqtSlot,
)

from astra_voice.meetings.events import MeetingState
from astra_voice.meetings.model import Meeting, SpeakerMap, Transcript, Utterance
from astra_voice.meetings.speakers import (
    canonical_speaker,
    speaker_map,
    speaker_number,
    validated_map,
)
from astra_voice.meetings.templates import (
    DAMAGED_LABEL,
    DAMAGED_TEXT,
    PLACEHOLDERS,
    MarkInfo,
    clock,
    echo_count,
    from_placeholder,
    meeting_marks,
    minutes_from_totals,
    selected_utterances,
    speaker_label,
    statistics,
    template_header,
    template_rows,
    utterance_flag,
    visible_utterances,
)
from astra_voice.ui.meeting_texts import MARK_TEXTS, echo_label

ROLES = (
    "kind",
    "time",
    "who",
    "whoKind",
    "num",
    "text",
    "icon",
    "accessibleName",
    "flag",
    "flagText",
    "flagIcon",
    "textMuted",
)
MODEL_ACTIONS = ("Установить", "Установить из файла…", "Подробнее")
CARD_STATES = frozenset(
    {
        MeetingState.RECORDED,
        MeetingState.TRANSCRIBING,
        MeetingState.WAITING_MEMORY,
        MeetingState.NEEDS_MODEL,
        MeetingState.DONE,
        MeetingState.FAILED,
        MeetingState.INTERRUPTED,
    }
)
RENAME_STATES = frozenset({MeetingState.RECORDED, MeetingState.TRANSCRIBING, MeetingState.DONE})
TEXTS = {
    **MARK_TEXTS,
    "back": "Все встречи",
    "save": "Сохранить как…",
    "rename": "Переименовать…",
    "names": "Имена участников",
    "delete": "Удалить встречу…",
    "participants": "Участники",
    "participantsStrip": "Участники:",
    "meCaption": "Ваш микрофон — подпись из настроек",
    "namesLater": (
        "Имена можно вписать после окончания расшифровки — "
        "номера спикеров до конца могут измениться."
    ),
    "progressCaption": (
        "Готовые реплики появляются сразу. Можно закрыть окно или выйти из программы — "
        "расшифровка продолжится."
    ),
    "progressDone": "Расшифровано ",
    "progressOf": " из ",
    "progressNames": " · имена спикеров уточняются до конца",
    "cancel": "Отменить",
    "tail": "Дальше — по мере расшифровки",
    "policyOff": (
        "Запись встреч отключена администратором. Встречу можно прочитать, сохранить как файл "
        "и удалить."
    ),
    "readError": "Не удалось прочитать папку встреч. Проверьте, что диск подключён и доступен.",
    "retry": "Повторить",
    # D4 «Сохранить как…» (spec §4.7; four local templates, .md/.txt).
    "saveTitle": "Сохранить как — ",
    "previewDuration": "Длительность ",
    "previewReplies": " · реплик ",
    "previewShares": " · сумма долей ",
    "previewMinutes": " мин",
    "previewMorePrefix": "…и ещё ",
    "previewEllipsis": "…",
    "saveNote": (
        "За файлами, сохранёнными вне программы, программа не следит — удалите их сами, если "
        "нужно. Файл будет доступен только вашей учётной записи."
    ),
    "saveCancel": "Отмена",
    "saveConfirm": "Выбрать папку и сохранить…",
    "saveError": "Не удалось сохранить файл: папка недоступна для записи",
    # The file appeared at the chosen path: replace only after an explicit answer.
    "replacePrefix": "Файл «",
    "replaceSuffix": "» уже существует. Заменить?",
    "replaceCancel": "Отмена",
    "replaceConfirm": "Заменить",
    "savedPrefix": "Сохранено в «",
    "savedMiddle": "»: ",
    "openFolder": "Открыть папку",
}
# Positional presentation corresponds to templates.TEMPLATES; identifiers live there only.
TEMPLATE_TEXTS = (
    ("doc", "Стенограмма", "Реплики по порядку, с именами и временем"),
    ("meeting", "Я и собеседники", "Только две подписи — без разделения собеседников"),
    ("file", "Только текст", "Абзацами, без имён и времени"),
    ("chart", "Статистика встречи", "Кто сколько говорил"),
)


def _accessible_name(at: str, text: str, who: str | None = None, flag: str = "") -> str:
    suffix = f", {MARK_TEXTS[flag]}" if flag else ""
    return f"{at}, {who}{suffix}: {text}" if who is not None else f"{at}, {text}"


def flag_roles(flag: str) -> dict[str, Any]:
    return {
        "flag": flag,
        "flagText": MARK_TEXTS[flag] if flag else "",
        "flagIcon": {"echo": "speaker", "multi": "people"}.get(flag, ""),
        "textMuted": flag == "echo",
    }


def save_preview(
    template: str,
    meeting: Meeting,
    transcript: Transcript | None,
    speakers: SpeakerMap | None = None,
    *,
    exclude_echo: bool = False,
) -> dict[str, Any]:
    """Bounded, format-independent view of the same structures used by render."""
    utterances = selected_utterances(transcript, exclude_echo=exclude_echo and template != "stats")
    marks = meeting_marks(meeting, utterances)
    count = echo_count(visible_utterances(transcript))
    if count:
        marks.append(
            MarkInfo("echo", echo_label(count, excluded=exclude_echo and template != "stats"))
        )
    header = template_header(template, meeting, utterances, marks, False, None, speakers)
    if template == "stats":
        data = statistics(meeting, utterances, limit=8, speakers=speakers)
        summary = [
            {"text": TEXTS["previewDuration"], "mono": False},
            {"text": data.duration, "mono": True},
            {"text": TEXTS["previewReplies"], "mono": False},
            {"text": str(data.replies), "mono": True},
        ]
        if data.has_shares:
            summary += [
                {"text": TEXTS["previewShares"], "mono": False},
                {"text": "100 %", "mono": True},
            ]
        return {
            "kind": "stats",
            "header": header,
            "excludedEcho": data.excluded_echo,
            "summary": summary,
            "hasShares": data.has_shares,
            "rows": [
                {
                    "who": row.label,
                    "minutes": str(row.minutes) + TEXTS["previewMinutes"],
                    "share": row.share,
                    "percent": f"{row.share} %" if row.share is not None else "",
                }
                for row in data.speakers
            ],
            "moreText": TEXTS["previewMorePrefix"] + str(data.remaining) if data.remaining else "",
        }
    paragraphs = template == "text_only"
    limit = 3 if paragraphs else 6
    # One lookahead determines the final ellipsis, without rendering the remaining meeting.
    source = template_rows(
        template,
        meeting,
        utterances,
        meeting_marks(meeting, utterances),
        speakers=speakers,
        limit=limit + 1,
        text_limit=600 if paragraphs else 240,
    )
    more = len(source) > limit
    rows = []
    for index, row in enumerate(source[:limit]):
        text = row.text
        if row.kind == "utterance" and (
            row.truncated or (paragraphs and more and index == limit - 1)
        ):
            text = text.rstrip() + TEXTS["previewEllipsis"]
        at = clock(row.at_ms)
        rows.append(
            {
                "kind": row.kind,
                **flag_roles(row.flag if row.flag == "echo" or template == "stenogram" else ""),
                "fileMark": (
                    f"({MARK_TEXTS[row.flag]}) " if paragraphs else f" ({MARK_TEXTS[row.flag]})"
                )
                if row.flag == "echo" or (row.flag == "multi" and template == "stenogram")
                else "",
                "markPosition": "start" if paragraphs else "after-name",
                "time": at if row.kind == "mark" else f"[{at}]",
                "who": row.who,
                "num": row.num,
                "whoKind": row.who_kind,
                "text": text,
                "icon": row.icon,
                "accessibleName": _accessible_name(
                    at,
                    text,
                    row.who if row.kind == "utterance" else None,
                    row.flag if row.flag == "echo" or template == "stenogram" else "",
                ),
            }
        )
    return {
        "kind": "text_only" if paragraphs else "transcript",
        "header": header,
        "rows": rows,
        "moreText": TEXTS["previewEllipsis"]
        if paragraphs and more and rows[-1]["kind"] == "mark"
        else "",
    }


FAILED_MODEL_TEXT = "Распознавание на этом компьютере не работает"
FAILED_MEMORY_TEXT = "Расшифровка подождёт: сейчас мало свободной памяти"
FAILED_TITLE = "Ошибка расшифровки"
SAVED_TEXT = "Запись сохранена и не удалится, пока вы её не удалите."


class TranscriptRows(QAbstractListModel):
    """Append-only rows of the card table (role names: ``ROLES``)."""

    countChanged = pyqtSignal()

    def __init__(self, parent: Any = None) -> None:
        super().__init__(parent)
        self._rows: list[dict[str, Any]] = []

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: B008, N802
        return 0 if parent.isValid() else len(self._rows)

    def roleNames(self) -> dict[int, QByteArray]:  # noqa: N802
        return {Qt.UserRole + 1 + i: QByteArray(name.encode()) for i, name in enumerate(ROLES)}

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole) -> Any:
        if not index.isValid() or not 0 <= index.row() < len(self._rows):
            return None
        offset = role - Qt.UserRole - 1
        if 0 <= offset < len(ROLES):
            key = ROLES[offset]
            row = self._rows[index.row()]
            if key == "accessibleName" and row.get("kind") == "utterance":
                return _accessible_name(
                    row["time"].strip("[]"), row["text"], row["who"], row.get("flag", "")
                )
            return row.get(key, "")
        return None

    @pyqtProperty(int, notify=countChanged)
    def count(self) -> int:
        return len(self._rows)

    @pyqtSlot(int, result="QVariantMap")
    def get(self, row: int) -> dict[str, Any]:
        if not 0 <= row < len(self._rows):
            return {}
        result = dict(self._rows[row])
        if result.get("kind") == "utterance":
            result["accessibleName"] = _accessible_name(
                result["time"].strip("[]"), result["text"], result["who"], result.get("flag", "")
            )
        return result

    def rows(self) -> list[dict[str, Any]]:
        return [self.get(index) for index in range(len(self._rows))]

    def reset(self, rows: Iterable[dict[str, Any]] = ()) -> None:
        self.beginResetModel()
        self._rows = list(rows)
        self.endResetModel()
        self.countChanged.emit()

    def relabel(self, indices: list[int], who: str, number: str) -> None:
        """Only label roles change; keep reply identity, order and text untouched."""
        roles = [Qt.UserRole + 1 + ROLES.index(key) for key in ("who", "num", "accessibleName")]
        start = previous = -1
        for index in indices:
            row = self._rows[index]
            row["who"], row["num"] = who, number
            if start >= 0 and index != previous + 1:
                self.dataChanged.emit(self.index(start), self.index(previous), roles)
                start = -1
            if start < 0:
                start = index
            previous = index
        if start >= 0:
            self.dataChanged.emit(self.index(start), self.index(previous), roles)

    def update(self, rows: list[dict[str, Any]]) -> None:
        """Refresh roles in place when reply identities/order are unchanged."""
        assert len(rows) == len(self._rows)
        for index, row in enumerate(rows):
            previous = self.get(index)
            roles = [
                Qt.UserRole + 1 + offset
                for offset, name in enumerate(ROLES)
                if row.get(name, "") != previous.get(name, "")
            ]
            if roles:
                self._rows[index] = row
                self.dataChanged.emit(self.index(index), self.index(index), roles)

    def append(self, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        first = len(self._rows)
        self.beginInsertRows(QModelIndex(), first, first + len(rows) - 1)
        self._rows.extend(rows)
        self.endInsertRows()
        self.countChanged.emit()


def mark_row(mark: MarkInfo) -> dict[str, Any]:
    at = clock(mark.at_ms or 0)
    return {
        **flag_roles(""),
        "kind": "mark",
        "time": at,
        "who": "",
        "whoKind": "",
        "num": "",
        "text": mark.line,
        "icon": "mic" if mark.kind == "mic" else mark.icon,
        "accessibleName": _accessible_name(at, mark.line),
    }


def utterance_row(
    meeting: Meeting, item: Utterance, speakers: SpeakerMap | None = None
) -> dict[str, Any]:
    at = clock(item.start_ms)
    if item.text in PLACEHOLDERS:
        text = item.text
        if item.text == DAMAGED_TEXT:
            text = f"{DAMAGED_LABEL} {at}–{clock(item.end_ms)}"
        return {
            **flag_roles(""),
            "kind": "mark",
            "time": at,
            "who": "",
            "whoKind": "",
            "num": "",
            "text": text,
            "icon": "alert",
            "accessibleName": _accessible_name(at, text),
        }
    who = speaker_label(meeting, item.speaker, speakers)
    flag = utterance_flag(item)
    if flag == "echo" and meeting.variant == "mic_only":
        flag = ""
    if flag == "multi" and (
        meeting.state == MeetingState.TRANSCRIBING
        or not meeting.transcription.diarization
        or meeting.transcription.diar_status == "failed"
    ):
        flag = ""
    return {
        **flag_roles(flag),
        "kind": "utterance",
        "time": f"[{at}]",
        "who": who,
        "whoKind": "me" if item.speaker == "me" else "others",
        "num": str(speaker_number(item.speaker, speakers) or ""),
        "text": item.text,
        "icon": "",
        "accessibleName": _accessible_name(at, item.text, who, flag),
    }


def table_marks(meeting: Meeting, utterances: Iterable[Utterance] = ()) -> list[MarkInfo]:
    """Timed marks that become table rows (placeholder replies show their own damage)."""
    return sorted(
        (
            mark
            for mark in meeting_marks(meeting, utterances)
            if mark.at_ms is not None and not from_placeholder(mark)
        ),
        key=lambda mark: mark.at_ms or 0,
    )


def _same(a: Utterance, b: Utterance, *, content_only: bool = False) -> bool:
    return (
        a.id,
        a.seq,
        a.track,
        a.start_ms,
        a.end_ms,
        a.text,
    ) == (
        b.id,
        b.seq,
        b.track,
        b.start_ms,
        b.end_ms,
        b.text,
    ) and (
        content_only
        or (a.speaker, a.multiple_speakers, a.echo) == (b.speaker, b.multiple_speakers, b.echo)
    )


class CardRows:
    """Chronological merge of replies and marks, appended as replies arrive."""

    def __init__(self, meeting: Meeting, model: TranscriptRows) -> None:
        self.meeting = meeting
        self.speakers: SpeakerMap | None = None
        self.model = model
        self.last_seq = -1
        self._reset_stats()
        self._marks = table_marks(meeting)

    def _reset_stats(self) -> None:
        # Kept incrementally: a running job adds one reply at a time (≤ 9000), and the
        # card's header must not rescan every reply on each of them.
        self.utterances: list[Utterance] = []
        self.utterance_rows: dict[str, int] = {}
        self.placeholders: list[Utterance] = []
        self.speech = 0
        self.echoes = 0
        self.speaker_ms: dict[str, int] = {}
        self.speaker_rows: dict[str, list[int]] = {}
        self.first_utterances: dict[str, Utterance] = {}

    def load(self, utterances: list[Utterance], *, complete: bool) -> None:
        if self.speakers is not None:
            self.speakers = speaker_map(utterances, self.speakers)
        self._marks = table_marks(self.meeting, utterances)
        self._reset_stats()
        self.last_seq = -1
        self.model.reset(self._rows(utterances, complete=complete))

    def rename(self, speakers: SpeakerMap, label: str) -> None:
        self.speakers = speakers
        groups = [
            rows
            for source, rows in self.speaker_rows.items()
            if canonical_speaker(source, speakers) == label
        ]
        indices = groups[0] if len(groups) == 1 else sorted(chain.from_iterable(groups))
        self.model.relabel(
            indices,
            speaker_label(self.meeting, label, speakers),
            str(speaker_number(label, speakers) or ""),
        )

    def minutes(self) -> dict[str, int]:
        """Whole minutes each speaker talked (≥ 1 when they said anything)."""
        return minutes_from_totals(self.speaker_ms, self.speakers)

    def sync(self, utterances: list[Utterance]) -> None:
        """Apply a validated complete live snapshot, including atomic D5 edits."""
        if not self.continues(utterances):
            self.load(utterances, complete=False)
            return
        if any(not _same(a, b) for a, b in zip(self.utterances, utterances, strict=False)):
            self._refresh(utterances[: len(self.utterances)])
        self.extend(utterances)

    def extend(self, utterances: list[Utterance]) -> None:
        fresh = [item for item in utterances if item.seq > self.last_seq]
        self.model.append(self._rows(fresh, complete=False))

    def continues(self, utterances: Sequence[Utterance]) -> bool:
        """Stable reply identities/content; marks and speaker labels may arrive later."""
        shown = self.utterances
        return len(shown) <= len(utterances) and all(
            _same(a, b, content_only=True) for a, b in zip(shown, utterances, strict=False)
        )

    def _refresh(self, utterances: Sequence[Utterance]) -> None:
        """Update existing rows by id, retaining timed marks and the pending mark queue."""
        updated = self.model.rows()
        indices = self.utterance_rows
        self._reset_stats()
        self.last_seq = -1
        for item in utterances:
            index = indices[item.id]
            updated[index] = utterance_row(self.meeting, item, self.speakers)
            self._remember(item, index)
        self.model.update(updated)

    def finish(self, utterances: Sequence[Utterance]) -> None:
        """Publish late marks/labels in place, then append the final tail and marks."""
        self._refresh(utterances[: len(self.utterances)])
        self.model.append(self._rows(list(utterances[len(self.utterances) :]), complete=True))

    def _remember(self, item: Utterance, index: int) -> None:
        self.utterance_rows[item.id] = index
        if item.text not in PLACEHOLDERS:
            self.speaker_rows.setdefault(item.speaker, []).append(index)
            self.first_utterances.setdefault(item.speaker, item)
        self.utterances.append(item)
        self.echoes += utterance_flag(item) == "echo"
        if item.text in PLACEHOLDERS:
            self.placeholders.append(item)
        else:
            self.speech += 1
            if not item.echo:
                length = max(0, item.end_ms - item.start_ms)
                self.speaker_ms[item.speaker] = self.speaker_ms.get(item.speaker, 0) + length
        self.last_seq = max(self.last_seq, item.seq)

    def _rows(self, utterances: list[Utterance], *, complete: bool) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        offset = self.model.rowCount()
        # load() resets the model only after constructing its new rows.
        if not self.utterances:
            offset = 0
        for item in utterances:
            while self._marks and (self._marks[0].at_ms or 0) <= item.start_ms:
                rows.append(mark_row(self._marks.pop(0)))
            self._remember(item, offset + len(rows))
            rows.append(utterance_row(self.meeting, item, self.speakers))
        if complete:
            rows.extend(mark_row(mark) for mark in self._marks)
            self._marks = []
        return rows


def snapshot_utterances(snapshot: Mapping[str, Any] | None) -> list[Utterance]:
    """Running-job replies (job_snapshot) as model objects; bad items are skipped."""
    result: list[Utterance] = []
    for item in (snapshot or {}).get("utterances", []):
        try:
            result.append(
                Utterance(
                    id=str(item.get("id", f"u{int(item['seq']):05d}")),
                    seq=int(item["seq"]),
                    track="far" if item.get("track") == "far" else "mic",
                    start_ms=int(item.get("start_ms", 0)),
                    end_ms=int(item.get("end_ms", 0)),
                    text=str(item.get("text", "")),
                    speaker=str(item.get("speaker", "others")),
                    multiple_speakers=bool(item.get("multiple_speakers", False)),
                    echo=bool(item.get("echo", False)),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return sorted((u for u in result if u.text.strip()), key=lambda u: u.seq)


def _local(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    try:
        return moment.astimezone()
    except OverflowError:
        return moment


def place(meeting: Meeting) -> str:
    if meeting.app_class == "dion":
        return "Звонок в DION"
    if meeting.app_class == "browser":
        return "Звонок в браузере"
    if meeting.variant == "mic_only":
        return "Встреча в комнате"
    if meeting.variant == "sink_monitor":
        return "Весь звук компьютера"
    return "Звонок"


def subtitle(meeting: Meeting, view: str) -> dict[str, Any]:
    """«09.10.2026 · 10:00–11:12 · <mono>1:12:40</mono> · Звонок в DION · звук удалён»."""
    start = _local(meeting.started_at)
    end = _local(meeting.ended_at)
    date = f"{start:%d.%m.%Y} · {start:%H:%M}" if start is not None else ""
    duration = clock(meeting.recorded_ms)
    if view == "interrupted":
        return {"prefix": f"{date} · сохранено ", "mono": duration, "suffix": ""}
    span = f"{date}–{end:%H:%M}" if end is not None and date else date
    suffix = " · " + place(meeting)
    if view == "transcribing":
        suffix += " · звук хранится до конца расшифровки"
    elif meeting.state == MeetingState.DONE:
        if not meeting.audio.present:
            suffix += " · звук удалён"
        elif meeting.transcription.finished_at and meeting.audio.retain_days:
            finished = _local(meeting.transcription.finished_at)
            if finished is not None:
                try:
                    until = finished + timedelta(days=meeting.audio.retain_days)
                except OverflowError:
                    until = None
                if until is not None:
                    suffix += f" · звук хранится до {until:%d.%m}"
    return {"prefix": f"{span} · " if span else "", "mono": duration, "suffix": suffix}


def badge(meeting: Meeting, percent: int) -> dict[str, Any]:
    state = meeting.state
    if state == MeetingState.TRANSCRIBING:
        return {"text": f"Расшифровка · {percent} %", "kind": "warning", "icon": "clock"}
    return {
        MeetingState.RECORDED: {"text": "Записана", "kind": "neutral", "icon": ""},
        MeetingState.WAITING_MEMORY: {"text": "Ждёт памяти", "kind": "warning", "icon": "clock"},
        MeetingState.NEEDS_MODEL: {"text": "Нужна модель", "kind": "warning", "icon": "alert"},
        MeetingState.DONE: {"text": "Готово", "kind": "success", "icon": "check"},
        MeetingState.FAILED: {"text": FAILED_TITLE, "kind": "danger", "icon": "alert"},
        MeetingState.INTERRUPTED: {"text": "Прервана", "kind": "warning", "icon": "alert"},
    }.get(state, {"text": "", "kind": "neutral", "icon": ""})


def _button(label: str, *, primary: bool = False, enabled: bool = True) -> dict[str, Any]:
    return {"label": label, "primary": primary, "enabled": enabled}


def panel(meeting: Meeting, view: str, *, can_transcribe: bool, failure: str) -> dict[str, Any]:
    """Message panel instead of the table (06-meeting-states, spec §4.7)."""
    delete = _button("Удалить встречу…")
    transcribe = [_button("Расшифровать", primary=True)] if can_transcribe else []
    if view == "cancelled":
        title, text = "Расшифровка отменена", "Запись сохранена — можно начать снова."
        return _panel("info", "muted", title, text, [*transcribe, delete])
    if view == "recorded":
        title, text = "Встреча записана", "Запись сохранена — можно начать расшифровку."
        return _panel("info", "muted", title, text, [*transcribe, delete])
    if view == "no-model":
        buttons = (
            [_button("Установить", primary=True), _button("Установить из файла…")]
            if can_transcribe
            else []
        )
        return _panel(
            "chip",
            "warning",
            "Для расшифровки встреч нужна модель GigaAM v3",
            "Запись сохранена. После установки расшифровка начнётся сама.",
            buttons,
        )
    if view == "wait-memory":
        return _panel(
            "clock",
            "warning",
            FAILED_MEMORY_TEXT,
            "Начнётся сама, когда памяти станет достаточно. Можно закрыть другие программы.",
            [_button("Отменить")],
        )
    if view == "error":
        retry = [_button("Повторить", primary=True)] if can_transcribe else []
        if failure == "model":
            return _panel(
                "alert",
                "danger",
                FAILED_MODEL_TEXT,
                "Запись сохранена и не удалится, пока вы её не удалите. "
                "Подробности — в разделе «Модели».",
                [*retry, _button("Подробнее")],
            )
        title = FAILED_MEMORY_TEXT if failure == "memory" else FAILED_TITLE
        return _panel("alert", "danger", title, SAVED_TEXT, retry)
    if view == "interrupted":
        return _panel(
            "alert",
            "warning",
            "Запись встречи прервалась. Сохранено ",
            "Программа закрылась во время записи. Последние секунды могли потеряться.",
            [*transcribe, _button("Удалить…")],
            mono=clock(meeting.recorded_ms),
        )
    if view == "empty":
        return _panel(
            "mic",
            "muted",
            "В записи не нашлось речи",
            "Проверьте, что выбран нужный микрофон, и что звук программы видеосвязи был слышен.",
            [delete],
        )
    return {}


def _panel(
    icon: str,
    tone: str,
    title: str,
    text: str,
    buttons: list[dict[str, Any]],
    *,
    mono: str = "",
) -> dict[str, Any]:
    return {
        "icon": icon,
        "tone": tone,
        "title": title,
        "titleMono": mono,
        "text": text,
        "buttons": buttons,
    }


def view_of(meeting: Meeting, *, loaded: bool, rows: int) -> str:
    state = meeting.state
    if state == MeetingState.TRANSCRIBING:
        return "transcribing"
    if state == MeetingState.DONE:
        if not loaded:
            return "loading"
        return "ready" if rows else "empty"
    return {
        MeetingState.RECORDED: "cancelled" if meeting.cancelled else "recorded",
        MeetingState.WAITING_MEMORY: "wait-memory",
        MeetingState.NEEDS_MODEL: "no-model",
        MeetingState.FAILED: "error",
        MeetingState.INTERRUPTED: "interrupted",
    }.get(state, "loading")


def participants(
    meeting: Meeting,
    minutes: Mapping[str, int],
    *,
    live: bool,
    speakers: SpeakerMap | None = None,
    utterances: Sequence[Utterance] = (),
    editable: bool = False,
    first_utterances: Mapping[str, Utterance] | None = None,
) -> list[dict[str, Any]]:
    """One panel/strip contract, with stable canonical numbers and first reply cues."""
    known = (
        first_utterances.keys()
        if first_utterances is not None
        else (u.speaker for u in utterances)
        if utterances
        else minutes.keys()
    )
    speakers = validated_map(known, speakers) if speakers is not None else None
    diar = (
        meeting.transcription.diarization and meeting.transcription.diar_status != "failed"
    ) or any(speaker_number(key) for key in minutes)
    rows: list[dict[str, Any]] = []
    if not (diar and meeting.variant == "mic_only"):
        rows.append(
            {
                "kind": "me",
                "id": "me",
                "num": "",
                "label": speaker_label(meeting, "me"),
                "name": "",
                "minutes": f"{minutes.get('me', 0)} мин",
                "canEdit": False,
                "canMerge": False,
                "mergeCandidates": [],
                "firstText": "",
                "firstStartMs": None,
            }
        )
    if not diar:
        if meeting.variant == "mic_only":
            rows = []
        others = sum(value for key, value in minutes.items() if key != "me")
        rows.append(
            {
                "kind": "others",
                "id": "others",
                "num": "",
                "label": speaker_label(meeting, "others"),
                "name": "",
                "minutes": "—"
                if live
                else f"{sum(minutes.values()) if meeting.variant == 'mic_only' else others} мин",
                "canEdit": False,
                "canMerge": False,
                "mergeCandidates": [],
                "firstText": "",
                "firstStartMs": None,
            }
        )
        return rows
    labels = set(minutes) | (set(speakers.labels) if speakers else set())
    labels = {canonical_speaker(label, speakers) for label in labels if speaker_number(label)}
    ordered = sorted(labels, key=lambda label: speaker_number(label) or 0)
    firsts: dict[str, Utterance] = {}
    first_source = (
        sorted(first_utterances.values(), key=lambda u: (u.start_ms, u.seq))
        if first_utterances is not None
        else utterances
    )
    for row in first_source:
        if row.text not in PLACEHOLDERS and row.text.strip():
            firsts.setdefault(canonical_speaker(row.speaker, speakers), row)
    for label in ordered:
        first = firsts.get(label)
        rows.append(
            {
                "kind": "speaker",
                "id": label,
                "num": str(speaker_number(label)),
                "label": speaker_label(meeting, label, speakers),
                "panelLabel": speaker_label(meeting, label),
                "inputAccessibleName": f"Как подписать: {speaker_label(meeting, label, speakers)}",
                "mergeTitle": f"{speaker_label(meeting, label, speakers)} — это…",
                "name": (speakers.labels[label].name or "")
                if speakers and label in speakers.labels
                else "",
                "minutes": "—" if live else f"{minutes.get(label, 0)} мин",
                "firstText": first.text[:160] if first else "",
                "firstStartMs": first.start_ms if first else None,
                "canEdit": editable,
                "canMerge": editable and len(ordered) > 1,
                "mergeCandidates": [
                    {
                        "id": target,
                        "num": str(speaker_number(target)),
                        "label": speaker_label(meeting, target, speakers),
                    }
                    for target in ordered
                    if target != label
                ]
                if editable
                else [],
            }
        )
    return rows


def echo_badge(count: int, *, live: bool = False) -> list[dict[str, Any]]:
    if not count:
        return []
    text = echo_label(count, live=live)
    return [
        {
            "text": text,
            "label": MARK_TEXTS["badge"] if live else MARK_TEXTS["badge"] + ":",
            "mono": "" if live else str(count),
            "suffix": "" if live else " " + text.split(" ", 3)[-1],
            "tone": "neutral",
            "icon": "speaker",
            "accessibleName": text,
        }
    ]


def presentation(
    meeting: Meeting,
    *,
    loaded: bool,
    read_error: bool,
    rows: CardRows | None,
    can_transcribe: bool,
    policy_off: bool,
    failure: str,
    snapshot: Mapping[str, Any] | None,
    deleting: bool = False,
    rename_allowed: bool = True,
    names_editable: bool = False,
    models_missing: bool = False,
) -> dict[str, Any]:
    # Marks alone are not speech: a done meeting without replies is «empty» (06).
    speech = rows.speech if rows is not None else 0
    placeholders = rows.placeholders if rows is not None else []
    view = view_of(meeting, loaded=loaded, rows=speech)
    if read_error and view in {"ready", "empty", "loading"}:
        view = "read-error"
    snap = snapshot or {}
    percent = int(snap.get("percent", 0)) if view == "transcribing" else 0
    total = int(snap.get("total_ms", 0)) or meeting.recorded_ms
    done = min(int(snap.get("done_ms", 0)), total)
    live = view == "transcribing"
    table = view in {"ready", "transcribing"}
    return {
        "open": True,
        "id": meeting.id,
        "view": view,
        "title": meeting.title,
        "badge": badge(meeting, percent),
        "subtitle": subtitle(meeting, view),
        "marks": (
            [
                {
                    "text": mark.badge,
                    "label": mark.badge_label,
                    "mono": mark.badge_mono,
                    "tone": mark.tone,
                    "icon": mark.icon,
                }
                for mark in meeting_marks(meeting, placeholders)
            ]
            + echo_badge(
                rows.echoes if rows and meeting.variant != "mic_only" else 0,
                live=live,
            )
            if table
            else []
        ),
        "showTable": table,
        "showEdit": rename_allowed and meeting.state in RENAME_STATES and not deleting,
        "renameEnabled": rename_allowed and meeting.state in RENAME_STATES and not deleting,
        "showActions": view == "ready",
        "deleteEnabled": not deleting
        and meeting.state
        not in {
            MeetingState.RECORDING,
            MeetingState.PAUSED,
            MeetingState.DELETING,
        },
        "percent": percent,
        "progressDone": clock(done),
        "progressTotal": clock(total),
        "participants": participants(
            meeting,
            rows.minutes() if rows else {},
            live=live,
            speakers=rows.speakers if rows else None,
            utterances=rows.utterances if rows else (),
            editable=names_editable,
            first_utterances=rows.first_utterances if rows else None,
        ),
        "namesEditable": names_editable,
        "namesHint": TEXTS["namesLater"] if live else "Имена действуют только в этой встрече.",
        "speakerState": (
            "transcribing"
            if live
            else "policy-off"
            if policy_off
            else "no-diarization"
            if not meeting.transcription.diarization
            or meeting.transcription.diar_status == "failed"
            else "room"
            if meeting.variant == "mic_only"
            else "ready"
        ),
        "diarizationFailed": meeting.transcription.diar_status == "failed",
        "diarizationModelsMissing": models_missing and not meeting.transcription.diarization,
        "diarizationHint": "Разделение по голосам: модели не установлены"
        if models_missing and not meeting.transcription.diarization
        else "",
        "diarizationAction": "Установить"
        if models_missing and not meeting.transcription.diarization
        else "",
        "live": live,
        "policyOff": policy_off and table,
        "panel": panel(meeting, view, can_transcribe=can_transcribe, failure=failure),
    }
