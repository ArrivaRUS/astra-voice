"""«Стенограмма» golden files and the «Сохранить как…» write (T-498 part, MN-3/MN-15)."""

from __future__ import annotations

import importlib
import os
import re
import stat
from datetime import timedelta, timezone
from pathlib import Path

import pytest

from astra_voice.core import meeting_jobs
from astra_voice.meetings import templates
from astra_voice.meetings.events import MeetingState
from astra_voice.meetings.model import (
    Interval,
    Mark,
    Meeting,
    Transcript,
    Utterance,
    Warnings,
)
from astra_voice.meetings.templates import (
    FORMATS,
    ExportError,
    ExportExists,
    meeting_marks,
    sanitize_filename,
    stenogram,
    with_extension,
    write_export,
)

pytestmark = pytest.mark.unit
REPO = Path(__file__).resolve().parents[2]
GOLDEN = REPO / "tests/fixtures/meeting_templates"
MSK = timezone(timedelta(hours=3))
MEETING_ID = "2026-10-09_100000-ab12"


def fixture_meeting() -> Meeting:
    return Meeting(
        id=MEETING_ID,
        started_at="2026-10-09T07:00:00Z",
        ended_at="2026-10-09T08:12:40Z",
        title="Планёрка по бюджету",
        state=MeetingState.DONE,
        variant="app_stream",
        app_class="dion",
        recorded_ms=4_360_000,
        intervals=[
            Interval(0, 0),
            Interval(1_870_000, 1_870_000),
            Interval(2_042_000, 1_870_000),
            Interval(4_532_000, 4_360_000),
        ],
        marks=[
            Mark(723_000, "mic_gap_start"),
            Mark(760_000, "mic_gap_end"),
            Mark(1_870_000, "pause", "user"),
            Mark(4_360_000, "pause", "stop"),
        ],
        warnings=Warnings(storage_network=True),
    )


def fixture_transcript() -> Transcript:
    rows = [
        ("mic", 4_000, 9_000, "Коллеги, добрый день.", "me", False),
        ("far", 12_000, 20_000, "Добрый день.", "others", False),
        ("far", 21_000, 30_000, "Тогда начнём\nс бюджета.", "others", False),
        ("far", 31_000, 33_000, "", "others", False),
        ("mic", 35_000, 38_000, "Это эхо собеседника", "me", True),
        ("mic", 39_000, 44_000, "Олег, <b>это</b> & *всё*?", "me", False),
        ("far", 2_400_000, 2_410_000, templates.DAMAGED_TEXT, "others", False),
        ("mic", 2_500_000, 2_503_000, "Я вернулся.", "me", False),
    ]
    return Transcript(
        meeting_id=MEETING_ID,
        utterances=[
            Utterance(
                id=f"u{index:05d}",
                seq=index,
                track=track,
                start_ms=start,
                end_ms=end,
                text=text,
                speaker=speaker,
                final=True,
                echo=echo,
            )
            for index, (track, start, end, text, speaker, echo) in enumerate(rows)
        ],
    )


@pytest.mark.parametrize("fmt", ["md", "txt"])
def test_stenogram_golden(fmt: str) -> None:
    text = stenogram(fixture_meeting(), fixture_transcript(), fmt, tz=MSK)
    assert text == (GOLDEN / f"stenogram.{fmt}").read_text(encoding="utf-8")


def test_stenogram_rules() -> None:
    text = stenogram(fixture_meeting(), fixture_transcript(), "txt", tz=MSK)
    # Echo and empty replies are hidden; adjacent replies (gap ≤ 2 s) are joined.
    assert "эхо собеседника" not in text
    assert "[0:00:12] Собеседники: Добрый день. Тогда начнём с бюджета." in text
    # Literal text, one line per reply: an embedded LF cannot fake a timecode line.
    assert "Олег, <b>это</b> & *всё*?" in text
    md = stenogram(fixture_meeting(), fixture_transcript(), "md", tz=MSK)
    assert "`Олег, <b>это</b> & *всё*?`" in md
    # Pause = recorded point + wall-clock gap of the intervals (decision 03.11: the
    # recorded axis excludes paused time, so no range); stop is not a pause.
    assert "Пауза 0:31:10 · 2 мин 52 с" in text and "0:34:02" not in text
    assert "1:12:40" not in text.split("Длительность: 1:12:40", 1)[1]
    assert text.count("Пауза") == 2
    assert text.index("— Ваш микрофон не записывался: 0:12:03–0:12:40") < text.index(
        "[0:41:40] Я: Я вернулся."
    )


def test_markdown_links_images_and_headings_stay_literal() -> None:
    meeting = fixture_meeting()
    meeting.title = "# заголовок [клик](https://example.com)"
    transcript = fixture_transcript()
    transcript.utterances[0].text = "[клик](https://example.com) ![x](file:///etc/passwd)"
    md = stenogram(meeting, transcript, "md", tz=MSK)
    assert md.startswith("# `# заголовок [клик](https://example.com)`\n")
    assert "[0:00:04] `Я`: `[клик](https://example.com) ![x](file:///etc/passwd)`\n" in md
    txt = stenogram(meeting, transcript, "txt", tz=MSK)
    assert txt.startswith("# заголовок [клик](https://example.com)\n")
    assert "[0:00:04] Я: [клик](https://example.com) ![x](file:///etc/passwd)\n" in txt


def test_stenogram_unknown_format_is_rejected() -> None:
    with pytest.raises(ValueError):
        stenogram(fixture_meeting(), fixture_transcript(), "html")


HOSTILE_FIELDS = [
    "https://x.ru",
    "http://x.ru",
    "www.x.ru",
    "a@b.ru",
    "example.ru",
    "<img src=x>",
    "![i](u)",
    "[t](u)",
    "`c`",
    "`start",
    "end`",
    "a``b```c",
    "```",
    " padded ",
    "\\<>&*_[]()!#@",
]


def untrusted_export(field: str, literal: str) -> tuple[str, list[str]]:
    meeting, transcript = fixture_meeting(), fixture_transcript()
    transcript.utterances = transcript.utterances[:1]
    if field == "title":
        meeting.title = literal
    elif field == "label":
        meeting.self_label = literal
    else:
        transcript.utterances[0].text = literal
    md = stenogram(meeting, transcript, "md", tz=MSK)
    reply = next(line for line in md.splitlines() if line.startswith("[0:00:04] "))
    if field == "title":
        fragments = [md.splitlines()[0].removeprefix("# ")]
    elif field == "label":
        names = next(
            line.removeprefix("- Участники: ")
            for line in md.splitlines()
            if line.startswith("- Участники: ")
        )
        fragments = [names, reply.removeprefix("[0:00:04] ").rsplit(": ", 1)[0]]
    else:
        fragments = [reply.split(": ", 1)[1]]
    return md, fragments


@pytest.mark.parametrize("field", ["title", "label", "reply"])
@pytest.mark.parametrize("literal", HOSTILE_FIELDS)
def test_markdown_untrusted_fields_are_whole_code_spans(field: str, literal: str) -> None:
    _md, fragments = untrusted_export(field, literal)
    visible = " ".join(literal.split()) if field == "reply" else literal
    longest = max((len(run) for run in re.findall(r"`+", visible)), default=0)
    for fragment in fragments:
        span = re.fullmatch(r"(`+)(.*?)\1", fragment)
        assert span is not None
        assert len(span[1]) > longest
        assert span[2] in (visible, " " + visible + " ")
        if visible.startswith("`") or visible.endswith("`"):
            assert span[2] == " " + visible + " "


@pytest.mark.parametrize("renderer", ["markdown_it", "cmarkgfm"])
@pytest.mark.parametrize("field", ["title", "label", "reply"])
@pytest.mark.parametrize("literal", HOSTILE_FIELDS)
def test_markdown_untrusted_fields_render_without_links_or_images(
    renderer: str,
    field: str,
    literal: str,
) -> None:
    from html import unescape

    try:
        module = importlib.import_module(renderer)
        if renderer == "markdown_it":
            importlib.import_module("linkify_it")
    except ImportError:
        pytest.skip("Markdown renderer unavailable")
    md, _fragments = untrusted_export(field, literal)
    if renderer == "markdown_it":
        html = (
            module.MarkdownIt("commonmark", {"linkify": True, "html": False})
            .enable("linkify")
            .render(md)
        )
    else:
        html = module.github_flavored_markdown_to_html(md)
    assert "<a" not in html and "<img" not in html
    visible = " ".join(literal.split()) if field == "reply" else literal
    assert visible in unescape("".join(re.findall(r"<code>(.*?)</code>", html)))


def test_placeholders_match_the_role_constants() -> None:
    assert templates.DAMAGED_TEXT == meeting_jobs.DAMAGED_TEXT
    assert templates.UNRECOGNIZED_TEXT == meeting_jobs.UNRECOGNIZED_TEXT
    worker = (REPO / "src/astra_voice/worker/meeting_transcribe.py").read_text(encoding="utf-8")
    assert f'DAMAGED_TEXT = "{templates.DAMAGED_TEXT}"' in worker
    assert f'UNRECOGNIZED_TEXT = "{templates.UNRECOGNIZED_TEXT}"' in worker


def test_marks_order_texts_and_tones() -> None:
    meeting = fixture_meeting()
    meeting.marks += [
        Mark(3_000_000, "pause", "lock"),
        Mark(3_060_000, "pause", "indicators"),
        Mark(3_900_000, "far_ended"),
    ]
    meeting.warnings.whole_output = True
    marks = meeting_marks(meeting, fixture_transcript().utterances)
    assert [mark.badge for mark in marks] == [
        "Пауза 0:31:10 · 2 мин 52 с",
        "Ваш микрофон не записывался 0:12:03–0:12:40",
        "Пауза: экран был заблокирован 0:50:00",
        "Пауза: индикатор записи не отображался 0:51:00",
        "Кусок записи повреждён 0:40:00–0:40:10",
        "DION закрылся в 1:05:00",
        "Записан весь звук компьютера",
        "Хранится на сетевом диске",
    ]
    assert [mark.tone for mark in marks][:2] == ["neutral", "warning"]
    assert marks[1].line == "Ваш микрофон не записывался: 0:12:03–0:12:40"
    assert marks[2].line == "Пауза: экран был заблокирован 0:50:00"


@pytest.mark.parametrize(
    ("title", "fmt", "expected"),
    [
        ("Планёрка по бюджету", "md", "Планёрка по бюджету.md"),
        ("../../etc/passwd", "txt", "etc passwd.txt"),
        ("..", "md", "Встреча.md"),
        ("a‮b​c\x00d\x1f", "md", "abcd.md"),
        ("  ...скрытый  ", "md", "скрытый.md"),
        ("я" * 300, "txt", "я" * 120 + ".txt"),
        ("é", "md", "é.md"),
    ],
)
def test_sanitize_filename(title: str, fmt: str, expected: str) -> None:
    assert sanitize_filename(title, fmt) == expected


def test_extension_follows_format() -> None:
    assert with_extension(Path("/x/a.md"), "md") == Path("/x/a.md")
    assert with_extension(Path("/x/a"), "txt") == Path("/x/a.txt")
    assert with_extension(Path("/x/a.md"), "txt") == Path("/x/a.md.txt")


def test_export_is_private_and_never_overwrites_without_confirmation(tmp_path: Path) -> None:
    old = os.umask(0)
    try:
        target = tmp_path / "Планёрка.md"
        write_export(target, "первый\n", overwrite=False)
        assert stat.S_IMODE(target.stat().st_mode) == 0o600
        assert target.read_text(encoding="utf-8") == "первый\n"
        with pytest.raises(ExportExists):
            write_export(target, "второй\n", overwrite=False)
        assert target.read_text(encoding="utf-8") == "первый\n"
        target.chmod(0o644)
        write_export(target, "второй\n", overwrite=True)
        assert target.read_text(encoding="utf-8") == "второй\n"
        assert stat.S_IMODE(target.stat().st_mode) == 0o600
        assert sorted(p.name for p in tmp_path.iterdir()) == ["Планёрка.md"]
    finally:
        os.umask(old)


def test_export_refuses_symlinks_and_special_files(tmp_path: Path) -> None:
    victim = tmp_path / "victim"
    victim.write_text("не трогать", encoding="utf-8")
    link = tmp_path / "link.md"
    link.symlink_to(victim)
    for overwrite in (False, True):
        with pytest.raises(ExportError):
            write_export(link, "x", overwrite=overwrite)
    assert victim.read_text(encoding="utf-8") == "не трогать"
    folder = tmp_path / "folder.md"
    folder.mkdir()
    with pytest.raises(ExportError):
        write_export(folder, "x", overwrite=True)
    assert folder.is_dir()


@pytest.mark.parametrize("title", ["Ж" * 120, "🎉" * 70, "é" * 200])
def test_long_multibyte_names_are_written_and_overwritten(tmp_path: Path, title: str) -> None:
    # NAME_MAX is 255 bytes: the name fits in bytes, the temporary name stays short.
    for fmt in FORMATS:
        name = sanitize_filename(title, fmt)
        assert len(name.encode("utf-8")) <= 255
        assert name.endswith("." + fmt) and name[0] == title[0]
        target = tmp_path / name
        write_export(target, "1\n", overwrite=False)
        write_export(target, "2\n", overwrite=True)
        assert target.read_text(encoding="utf-8") == "2\n"
    assert len(list(tmp_path.iterdir())) == len(FORMATS)


def test_export_to_unwritable_folder_fails_cleanly(tmp_path: Path) -> None:
    with pytest.raises(ExportError):
        write_export(tmp_path / "missing" / "a.md", "x", overwrite=False)
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        if os.access(locked, os.W_OK):
            pytest.skip("root ignores directory permissions")
        with pytest.raises(ExportError) as error:
            write_export(locked / "a.md", "x", overwrite=False)
        assert not isinstance(error.value, ExportExists)
    finally:
        locked.chmod(0o700)


@pytest.mark.parametrize(
    ("milliseconds", "expected"),
    [
        (0, "0 с"),
        (52_999, "52 с"),
        (60_000, "1 мин"),
        (172_000, "2 мин 52 с"),
        (720_000, "12 мин"),
        (3_599_999, "59 мин 59 с"),
        (3_600_000, "1 ч"),
        (3_900_000, "1 ч 5 мин"),
        (7_259_000, "2 ч"),
    ],
)
def test_pause_duration_text(milliseconds: int, expected: str) -> None:
    assert templates.duration_text(milliseconds) == expected


def test_pause_marks_show_point_and_real_length_ranges_stay() -> None:
    meeting = fixture_meeting()
    meeting.intervals = [
        Interval(0, 0),
        Interval(1_870_000, 1_870_000),
        Interval(2_042_000, 1_870_000),
        Interval(3_172_000, 3_000_000),  # lock: 12 min of real time, a point on the axis
        Interval(3_892_000, 3_000_000),
        Interval(5_252_000, 4_360_000),
    ]
    meeting.marks += [Mark(3_000_000, "pause", "lock")]
    marks = {mark.kind: mark for mark in meeting_marks(meeting, fixture_transcript().utterances)}
    assert marks["pause"].badge == "Пауза 0:31:10 · 2 мин 52 с"
    assert (marks["pause"].badge_label, marks["pause"].badge_mono) == (
        "Пауза",
        "0:31:10 · 2 мин 52 с",
    )
    assert marks["lock"].line == "Пауза: экран был заблокирован 0:50:00 · 12 мин"
    # Microphone gaps and damage are on the recorded axis: ranges stay.
    assert marks["mic"].line == "Ваш микрофон не записывался: 0:12:03–0:12:40"
    assert marks["damaged"].badge == "Кусок записи повреждён 0:40:00–0:40:10"
