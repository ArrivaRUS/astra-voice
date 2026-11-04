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
from astra_voice.meetings.model import Meeting, Utterance
from astra_voice.meetings.templates import (
    DAMAGED_LABEL,
    DAMAGED_TEXT,
    OTHERS_LABEL,
    PLACEHOLDERS,
    MarkInfo,
    clock,
    from_placeholder,
    meeting_marks,
    speaker_label,
)

ROLES = ("kind", "time", "who", "whoKind", "num", "text", "icon", "accessibleName")
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
TEXTS = {
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
    "cancel": "Отменить",
    "tail": "Дальше — по мере расшифровки",
    "policyOff": (
        "Запись встреч отключена администратором. Встречу можно прочитать, сохранить как файл "
        "и удалить."
    ),
    "readError": "Не удалось прочитать папку встреч. Проверьте, что диск подключён и доступен.",
    "retry": "Повторить",
    # D4 «Сохранить как…» (spec §4.7; M4: only «Стенограмма», .md/.txt).
    "saveTitle": "Сохранить как — ",
    "templateTitle": "Стенограмма",
    "templateCaption": "Реплики по порядку, с именами и временем",
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
FAILED_MODEL_TEXT = "Распознавание на этом компьютере не работает"
FAILED_MEMORY_TEXT = "Расшифровка подождёт: сейчас мало свободной памяти"
FAILED_TITLE = "Ошибка расшифровки"
SAVED_TEXT = "Запись сохранена и не удалится, пока вы её не удалите."


class TranscriptRows(QAbstractListModel):
    """Append-only rows of the card table (role names: ``ROLES``)."""

    countChanged = pyqtSignal()

    def __init__(self, parent: Any = None) -> None:
        super().__init__(parent)
        self._rows: list[dict[str, str]] = []

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: B008, N802
        return 0 if parent.isValid() else len(self._rows)

    def roleNames(self) -> dict[int, QByteArray]:  # noqa: N802
        return {Qt.UserRole + 1 + i: QByteArray(name.encode()) for i, name in enumerate(ROLES)}

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole) -> Any:
        if not index.isValid() or not 0 <= index.row() < len(self._rows):
            return None
        offset = role - Qt.UserRole - 1
        if 0 <= offset < len(ROLES):
            return self._rows[index.row()].get(ROLES[offset], "")
        return None

    @pyqtProperty(int, notify=countChanged)
    def count(self) -> int:
        return len(self._rows)

    @pyqtSlot(int, result="QVariantMap")
    def get(self, row: int) -> dict[str, str]:
        return dict(self._rows[row]) if 0 <= row < len(self._rows) else {}

    def rows(self) -> list[dict[str, str]]:
        return list(self._rows)

    def reset(self, rows: Iterable[dict[str, str]] = ()) -> None:
        self.beginResetModel()
        self._rows = list(rows)
        self.endResetModel()
        self.countChanged.emit()

    def append(self, rows: list[dict[str, str]]) -> None:
        if not rows:
            return
        first = len(self._rows)
        self.beginInsertRows(QModelIndex(), first, first + len(rows) - 1)
        self._rows.extend(rows)
        self.endInsertRows()
        self.countChanged.emit()


def mark_row(mark: MarkInfo) -> dict[str, str]:
    at = clock(mark.at_ms or 0)
    return {
        "kind": "mark",
        "time": at,
        "who": "",
        "whoKind": "",
        "num": "",
        "text": mark.line,
        "icon": "mic" if mark.kind == "mic" else mark.icon,
        "accessibleName": f"{at}, {mark.line}",
    }


def utterance_row(meeting: Meeting, item: Utterance) -> dict[str, str]:
    at = clock(item.start_ms)
    if item.text in PLACEHOLDERS:
        text = item.text
        if item.text == DAMAGED_TEXT:
            text = f"{DAMAGED_LABEL} {at}–{clock(item.end_ms)}"
        return {
            "kind": "mark",
            "time": at,
            "who": "",
            "whoKind": "",
            "num": "",
            "text": text,
            "icon": "alert",
            "accessibleName": f"{at}, {text}",
        }
    who = speaker_label(meeting, item.speaker)
    return {
        "kind": "utterance",
        "time": f"[{at}]",
        "who": who,
        "whoKind": "me" if item.speaker == "me" else "others",
        "num": "",
        "text": item.text,
        "icon": "",
        "accessibleName": f"{at}, {who}: {item.text}",
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


def _same(a: Utterance, b: Utterance) -> bool:
    return (a.seq, a.start_ms, a.end_ms, a.speaker, a.text) == (
        b.seq,
        b.start_ms,
        b.end_ms,
        b.speaker,
        b.text,
    )


class CardRows:
    """Chronological merge of replies and marks, appended as replies arrive."""

    def __init__(self, meeting: Meeting, model: TranscriptRows) -> None:
        self.meeting = meeting
        self.model = model
        self.last_seq = -1
        self._reset_stats()
        self._marks = table_marks(meeting)

    def _reset_stats(self) -> None:
        # Kept incrementally: a running job adds one reply at a time (≤ 9000), and the
        # card's header must not rescan every reply on each of them.
        self.utterances: list[Utterance] = []
        self.placeholders: list[Utterance] = []
        self.speech = 0
        self.speaker_ms: dict[str, int] = {}

    def load(self, utterances: list[Utterance], *, complete: bool) -> None:
        self._marks = table_marks(self.meeting, utterances)
        self._reset_stats()
        self.last_seq = -1
        self.model.reset(self._rows(utterances, complete=complete))

    def minutes(self) -> dict[str, int]:
        """Whole minutes each speaker talked (≥ 1 when they said anything)."""
        return {
            key: max(1, round(value / 60_000)) if value else 0
            for key, value in self.speaker_ms.items()
        }

    def extend(self, utterances: list[Utterance]) -> None:
        fresh = [item for item in utterances if item.seq > self.last_seq]
        self.model.append(self._rows(fresh, complete=False))

    def continues(self, utterances: Sequence[Utterance]) -> bool:
        """The shown replies are the start of ``utterances`` (the finished run's text)."""
        shown = self.utterances
        return len(shown) <= len(utterances) and all(
            _same(a, b) for a, b in zip(shown, utterances, strict=False)
        )

    def finish(self, utterances: Sequence[Utterance]) -> None:
        """Append the replies after the shown ones and the remaining marks (no reset)."""
        self.model.append(self._rows(list(utterances[len(self.utterances) :]), complete=True))

    def _rows(self, utterances: list[Utterance], *, complete: bool) -> list[dict[str, str]]:
        rows: list[dict[str, str]] = []
        for item in utterances:
            while self._marks and (self._marks[0].at_ms or 0) <= item.start_ms:
                rows.append(mark_row(self._marks.pop(0)))
            rows.append(utterance_row(self.meeting, item))
            self.utterances.append(item)
            if item.text in PLACEHOLDERS:
                self.placeholders.append(item)
            else:
                self.speech += 1
                length = max(0, item.end_ms - item.start_ms)
                self.speaker_ms[item.speaker] = self.speaker_ms.get(item.speaker, 0) + length
            self.last_seq = max(self.last_seq, item.seq)
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


def subtitle(meeting: Meeting, view: str) -> dict[str, str]:
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


def badge(meeting: Meeting, percent: int) -> dict[str, str]:
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
    meeting: Meeting, minutes: Mapping[str, int], *, live: bool
) -> list[dict[str, str]]:
    """Panel/strip entries of M4: «Я» and «Собеседники» (no names before M7, BL-3)."""
    rows = [
        {
            "kind": "me",
            "label": meeting.self_label,
            # 05 §3: «Я» — дорожка микрофона, минуты известны и во время расшифровки.
            "minutes": f"{minutes.get('me', 0)} мин",
        }
    ]
    if meeting.variant != "mic_only":
        others = sum(value for key, value in minutes.items() if key != "me")
        rows.append(
            {
                "kind": "others",
                "label": OTHERS_LABEL,
                "minutes": "—" if live else f"{others} мин",
            }
        )
    return rows


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
            if table
            else []
        ),
        "showTable": table,
        "showEdit": table,
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
        "participants": participants(meeting, rows.minutes() if rows else {}, live=live),
        "live": live,
        "policyOff": policy_off and table,
        "panel": panel(meeting, view, can_transcribe=can_transcribe, failure=failure),
    }
