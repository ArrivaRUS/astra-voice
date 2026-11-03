"""Local «Стенограмма» template and the safe «Сохранить как…» write (E13.M4).

design/meetings/spec.md §4.7, arch/e13-plan-claude.md §7.1, T1 MN-3/MN-12/MN-15.
No network and no format strings over user text (MN-12): the layout is fixed
and built by concatenation. Speech text stays literal; a line break inside a
reply becomes a space, so one reply never fakes another «[ч:мм:сс] …» line.
Marks (pauses, microphone gaps, damage) are shared with card A.
"""

from __future__ import annotations

import os
import stat
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, tzinfo
from pathlib import Path
from uuid import uuid4

from astra_voice.meetings.model import Meeting, Transcript, Utterance

FORMATS = ("md", "txt")
MERGE_GAP_MS = 2000  # adjacent replies of one speaker closer than 2 s are joined
FILENAME_LIMIT = 120
NAME_MAX_BYTES = 255  # a file name in bytes (Linux NAME_MAX)
DEFAULT_NAME = "Встреча"
OTHERS_LABEL = "Собеседники"
# Constant placeholder texts of the role (worker/meeting_transcribe.py, core/meeting_jobs.py).
UNRECOGNIZED_TEXT = "[не распознано]"
DAMAGED_TEXT = "[кусок записи повреждён]"
PLACEHOLDERS = frozenset({UNRECOGNIZED_TEXT, DAMAGED_TEXT})
DAMAGED_LABEL = "Кусок записи повреждён"
# Badge order of spec §4.7: pause, microphone, lock, indicator, damage, app, whole sound.
_ORDER = ("pause", "mic", "lock", "indicators", "damaged", "far", "whole", "network", "remote")


class ExportError(Exception):
    """The file could not be written (folder unavailable, not a regular file…)."""


class ExportExists(ExportError):
    """The file exists and overwriting was not confirmed by the person."""


def clock(milliseconds: int) -> str:
    """«ч:мм:сс» — the format of timecodes and durations (spec §4.7)."""
    seconds = max(0, int(milliseconds)) // 1000
    return f"{seconds // 3600}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"


def duration_text(milliseconds: int) -> str:
    """Real length of a pause: «52 с», «2 мин 52 с», «12 мин», «1 ч 5 мин» (decision 03.11)."""
    seconds = max(0, int(milliseconds)) // 1000
    if seconds < 60:
        return f"{seconds} с"
    if seconds < 3600:
        minutes, rest = divmod(seconds, 60)
        return f"{minutes} мин {rest} с" if rest else f"{minutes} мин"
    hours, minutes = seconds // 3600, seconds // 60 % 60
    return f"{hours} ч {minutes} мин" if minutes else f"{hours} ч"


PAUSE_KINDS = frozenset({"pause", "lock", "indicators"})


@dataclass(frozen=True)
class MarkInfo:
    """One meeting mark: a header badge and, when timed, a table/file line.

    Pauses are a point on the recorded axis (it excludes paused time) plus their
    real length: «Пауза 0:31:10 · 2 мин 52 с»; other marks keep recorded ranges.
    """

    kind: str  # pause | mic | lock | indicators | damaged | far | whole | network | remote
    label: str
    at_ms: int | None = None
    end_ms: int | None = None
    duration_ms: int | None = None  # pauses only: wall-clock length

    @property
    def range_text(self) -> str:
        if self.at_ms is None:
            return ""
        if self.kind == "far":
            return "в " + clock(self.at_ms)
        if self.kind in PAUSE_KINDS:
            if self.duration_ms is None or self.duration_ms <= 0:
                return clock(self.at_ms)
            return f"{clock(self.at_ms)} · {duration_text(self.duration_ms)}"
        if self.end_ms is None or self.end_ms <= self.at_ms:
            return clock(self.at_ms)
        return f"{clock(self.at_ms)}–{clock(self.end_ms)}"

    @property
    def badge_label(self) -> str:
        """Badge words; the times after them are PT Mono (``badge_mono``)."""
        return self.label + " в" if self.kind == "far" and self.at_ms is not None else self.label

    @property
    def badge_mono(self) -> str:
        if self.kind == "far" and self.at_ms is not None:
            return clock(self.at_ms)
        return self.range_text

    @property
    def badge(self) -> str:
        """«Пауза 0:31:10 · 2 мин 52 с», «DION закрылся в 1:05:20» (spec §4.7)."""
        return f"{self.label} {self.range_text}" if self.range_text else self.label

    @property
    def line(self) -> str:
        """Table row and file line: «Ваш микрофон не записывался: 0:12:03–0:12:40»."""
        if self.kind != "mic":
            return self.badge
        return self.label + ": " + self.range_text

    @property
    def tone(self) -> str:
        return "warning" if self.kind in {"mic", "damaged", "network", "remote"} else "neutral"

    @property
    def icon(self) -> str:
        return {
            "pause": "pause",
            "indicators": "pause",
            "lock": "lock",
            "mic": "mic",
            "damaged": "alert",
            "network": "alert",
            "remote": "alert",
            "far": "info",
            "whole": "speaker",
        }[self.kind]


def _pause_length(meeting: Meeting, at_ms: int) -> int | None:
    """Wall-clock length of the pause that closed the recording at ``at_ms``.

    Intervals are [open, close] pairs of (monotonic, recorded) points; the gap
    between a close at ``at_ms`` and the next open is the pause length.
    """
    points = meeting.intervals
    for index in range(1, len(points) - 1, 2):
        if points[index].recorded_ms == at_ms:
            gap = points[index + 1].t_mono_ms - points[index].t_mono_ms
            return gap if gap > 0 else None
    return None


def meeting_marks(meeting: Meeting, utterances: Iterable[Utterance] = ()) -> list[MarkInfo]:
    """Marks of the meeting in spec §4.7 order (badges, table rows, file header)."""
    result: list[MarkInfo] = []
    seen: set[tuple[str, int | None]] = set()

    def add(mark: MarkInfo) -> None:
        if (mark.label, mark.at_ms) not in seen:
            seen.add((mark.label, mark.at_ms))
            result.append(mark)

    gap_start: int | None = None
    damaged_ranges: list[tuple[int, int]] = [
        (u.start_ms, u.end_ms) for u in utterances if u.text == DAMAGED_TEXT
    ]
    for mark in meeting.marks:
        if mark.kind == "pause" and mark.reason != "stop":
            label = {
                "lock": "Пауза: экран был заблокирован",
                "indicators": "Пауза: индикатор записи не отображался",
            }.get(mark.reason or "", "Пауза")
            kind = {"lock": "lock", "indicators": "indicators"}.get(mark.reason or "", "pause")
            length = _pause_length(meeting, mark.at_ms)
            if length is None and mark.at_ms >= meeting.recorded_ms:
                continue  # the recording ended paused: nothing was missed after it
            add(MarkInfo(kind, label, mark.at_ms, duration_ms=length))
        elif mark.kind == "indicators_lost":
            label = "Пауза: индикатор записи не отображался"
            length = _pause_length(meeting, mark.at_ms)
            add(MarkInfo("indicators", label, mark.at_ms, duration_ms=length))
        elif mark.kind == "mic_gap_start":
            gap_start = mark.at_ms
        elif mark.kind == "mic_gap_end" and gap_start is not None:
            add(MarkInfo("mic", "Ваш микрофон не записывался", gap_start, mark.at_ms))
            gap_start = None
        elif mark.kind == "far_ended":
            label = (
                "DION закрылся" if meeting.app_class == "dion" else "Программа видеосвязи закрылась"
            )
            add(MarkInfo("far", label, mark.at_ms))
        elif mark.kind == "damaged" and not any(a <= mark.at_ms <= b for a, b in damaged_ranges):
            add(MarkInfo("damaged", DAMAGED_LABEL, mark.at_ms))
    if gap_start is not None:
        add(MarkInfo("mic", "Ваш микрофон не записывался", gap_start, meeting.recorded_ms))
    for start, end in damaged_ranges:
        add(MarkInfo("damaged", DAMAGED_LABEL, start, end))
    if meeting.warnings.whole_output:
        add(MarkInfo("whole", "Записан весь звук компьютера"))
    if meeting.warnings.storage_network:
        add(MarkInfo("network", "Хранится на сетевом диске"))
    if meeting.warnings.audio_server_remote:
        add(MarkInfo("remote", "Звук шёл через другой компьютер"))
    return sorted(
        result,
        key=lambda item: (_ORDER.index(item.kind), item.at_ms if item.at_ms is not None else 0),
    )


def from_placeholder(mark: MarkInfo) -> bool:
    """A damage range taken from a placeholder reply (the reply itself shows it)."""
    return mark.kind == "damaged" and mark.end_ms is not None


def visible_utterances(transcript: Transcript | None) -> list[Utterance]:
    """Chronological replies without echo and without empty text (spec §3.7)."""
    if transcript is None:
        return []
    rows = [u for u in transcript.utterances if not u.echo and u.text.strip()]
    return sorted(rows, key=lambda u: (u.start_ms, u.seq))


def speaker_label(meeting: Meeting, speaker: str) -> str:
    """«Я» (the meeting's own label) / «Собеседники»; M7 adds «Спикер N»."""
    if speaker == "me":
        return meeting.self_label
    if speaker.startswith("S") and speaker[1:].isdigit():
        return f"Спикер {speaker[1:]}"
    return OTHERS_LABEL


def speaker_minutes(utterances: Iterable[Utterance]) -> dict[str, int]:
    """Whole minutes each speaker talked (≥ 1 when they said anything)."""
    total: dict[str, int] = {}
    for item in utterances:
        if item.text in PLACEHOLDERS:
            continue
        total[item.speaker] = total.get(item.speaker, 0) + max(0, item.end_ms - item.start_ms)
    return {key: max(1, round(value / 60_000)) if value else 0 for key, value in total.items()}


def _local(value: str | None, tz: tzinfo | None) -> datetime | None:
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return moment.astimezone(tz)
    except (ValueError, OverflowError):
        return None


def _one_line(text: str) -> str:
    return " ".join(text.split())


def _md_escape(text: str) -> str:
    """Literal text in Markdown: no HTML, emphasis, code spans, links, images or headings.

    User text never starts a line (it follows «# », «[ч:мм:сс] Кто: » or a label),
    so line-start markers (-, +, 1.) need no escaping.
    """
    return "".join("\\" + c if c in "\\<>&*_`[]()!#" else c for c in text)


def _merged(meeting: Meeting, utterances: Sequence[Utterance]) -> list[tuple[int, str, str]]:
    """(start_ms, speaker label, text) with adjacent replies of one speaker joined."""
    rows: list[tuple[int, str, str, int, bool]] = []
    for item in utterances:
        text = _one_line(item.text)
        placeholder = item.text in PLACEHOLDERS
        if (
            rows
            and not placeholder
            and not rows[-1][4]
            and rows[-1][1] == item.speaker
            and item.start_ms - rows[-1][3] <= MERGE_GAP_MS
        ):
            start, speaker, previous, _end, _ = rows[-1]
            rows[-1] = (start, speaker, previous + " " + text, item.end_ms, False)
        else:
            rows.append((item.start_ms, item.speaker, text, item.end_ms, placeholder))
    return [(start, speaker_label(meeting, speaker), text) for start, speaker, text, _, _ in rows]


def stenogram(
    meeting: Meeting,
    transcript: Transcript | None,
    fmt: str = "md",
    *,
    tz: tzinfo | None = None,
) -> str:
    """«Стенограмма»: header and replies «[ч:мм:сс] Я: …» in chronological order."""
    if fmt not in FORMATS:
        raise ValueError("Неизвестный формат")
    md = fmt == "md"

    def esc(text: str) -> str:
        return _md_escape(text) if md else text

    utterances = visible_utterances(transcript)
    marks = meeting_marks(meeting, utterances)
    start = _local(meeting.started_at, tz)
    lines: list[str] = ["# " + esc(meeting.title) if md else meeting.title, ""]
    item = "- " if md else ""
    if start is not None:
        lines.append(f"{item}Дата: {start:%d.%m.%Y}")
        lines.append(f"{item}Начало: {start:%H:%M}")
    lines.append(f"{item}Длительность: {clock(meeting.recorded_ms)}")
    speakers = sorted({u.speaker for u in utterances}, key=lambda s: (s != "me", s))
    if speakers:
        names = ", ".join(speaker_label(meeting, s) for s in speakers)
        lines.append(f"{item}Участники: {esc(names)}")
    if marks:
        lines.append(f"{item}Отметки:")
        lines.extend(("  - " if md else "  ") + mark.badge for mark in marks)
    lines.append("")
    timed = [(mark.at_ms, 0, mark) for mark in marks if mark.at_ms is not None]
    body: list[tuple[int, int, str]] = [
        (at, 0, "— " + mark.line) for at, _, mark in timed if not from_placeholder(mark)
    ]
    body.extend(
        (at, 1, f"[{clock(at)}] {esc(who)}: {esc(text)}")
        for at, who, text in _merged(meeting, utterances)
    )
    body.sort(key=lambda row: (row[0], row[1]))
    separator = "\n\n" if md else "\n"
    return "\n".join(lines) + "\n" + separator.join(text for _, _, text in body) + "\n"


def sanitize_filename(title: str, fmt: str) -> str:
    """Default file name from the meeting title (MN-3): no «/», controls, ≤ 120."""
    if fmt not in FORMATS:
        raise ValueError("Неизвестный формат")
    normalized = unicodedata.normalize("NFC", title)
    cleaned = "".join(
        " " if c in "/\\" else c
        for c in normalized
        if unicodedata.category(c) not in {"Cc", "Cf", "Cs", "Co", "Cn"}
    )
    stem = " ".join(cleaned.split()).lstrip(". ")
    # NAME_MAX is 255 bytes, not characters: cut the UTF-8 stem on a character boundary.
    limited = stem[:FILENAME_LIMIT].encode("utf-8")[: NAME_MAX_BYTES - 1 - len(fmt)]
    stem = limited.decode("utf-8", "ignore").rstrip(" .")
    if not stem:
        stem = DEFAULT_NAME
    return f"{stem}.{fmt}"


def with_extension(path: Path, fmt: str) -> Path:
    """The chosen path with the format's extension (a system dialog may drop it)."""
    if fmt not in FORMATS:
        raise ValueError("Неизвестный формат")
    return path if path.suffix.lower() == "." + fmt else path.with_name(path.name + "." + fmt)


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        view = view[written:]
    os.fchmod(fd, 0o600)
    os.fsync(fd)


def write_export(path: Path, text: str, *, overwrite: bool) -> None:
    """Write a 0600 regular file; replace an existing one only when confirmed.

    The new file is created with O_EXCL|O_NOFOLLOW. A confirmed replacement goes
    through a private temporary file and an atomic rename; a symlink, a folder
    or another special file is never replaced (MN-3/MN-15).
    """
    name = path.name
    if not name or name in {".", ".."} or "/" in name or "\0" in name:
        raise ExportError("Недопустимое имя файла")
    data = text.encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
    temporary = ""
    try:
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    except OSError:
        raise ExportError("Папка недоступна") from None
    try:
        try:
            fd = os.open(name, flags, 0o600, dir_fd=directory)
        except FileExistsError:
            if not overwrite:
                raise ExportExists("Файл уже есть") from None
            current = os.stat(name, dir_fd=directory, follow_symlinks=False)
            if not stat.S_ISREG(current.st_mode):
                raise ExportError("Это не обычный файл") from None
            temporary = f".astra-voice-{uuid4().hex}.tmp"  # short: name may be 255 bytes
            fd = os.open(temporary, flags, 0o600, dir_fd=directory)
            try:
                _write_all(fd, data)
            finally:
                os.close(fd)
            os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
            temporary = ""
        else:
            temporary = name  # a half-written new file is removed on failure
            try:
                _write_all(fd, data)
            finally:
                os.close(fd)
            temporary = ""
        os.fsync(directory)
    except ExportError:
        raise
    except OSError:
        raise ExportError("Не удалось записать файл") from None
    finally:
        if temporary:
            try:
                os.unlink(temporary, dir_fd=directory)
            except OSError:
                pass
        os.close(directory)
