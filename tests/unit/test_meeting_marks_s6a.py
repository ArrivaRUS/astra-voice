"""D7/§Д: logical marks contracts; QML geometry is checked in step 6b."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from typing import Any

import pytest
from PyQt5.QtTest import QSignalSpy
from test_meeting_templates import fixture_meeting

from astra_voice.meetings import templates as t
from astra_voice.meetings.events import MeetingState
from astra_voice.meetings.model import Meeting, Transcript, Utterance
from astra_voice.ui import meeting_card as c
from astra_voice.ui.meeting_texts import MARK_TEXTS, reply_count

pytestmark = pytest.mark.unit
TEXT_TEMPLATES = t.TEMPLATES[:3]


def reply(
    seq: int = 0,
    *,
    echo: bool = False,
    multi: bool = False,
    speaker: str = "me",
    start: int = 0,
    end: int = 1000,
    text: str = "Речь",
) -> Utterance:
    return Utterance(
        id=f"u{seq:05d}",
        seq=seq,
        track="mic" if speaker == "me" else "far",
        start_ms=start,
        end_ms=end,
        text=text,
        speaker=speaker,
        echo=echo,
        multiple_speakers=multi,
        final=True,
    )


def transcript(*rows: Utterance) -> Transcript:
    return Transcript(meeting_id=fixture_meeting().id, utterances=list(rows))


def meeting() -> Meeting:
    result = fixture_meeting()
    result.marks = []
    result.warnings.storage_network = False
    result.transcription.diarization = True
    result.transcription.diar_status = "done"
    return result


def present(m: Meeting, builder: c.CardRows, *, policy_off: bool = False) -> dict[str, Any]:
    return c.presentation(
        m,
        loaded=True,
        read_error=False,
        rows=builder,
        can_transcribe=True,
        policy_off=policy_off,
        failure="",
        snapshot=None,
    )


def test_legacy_without_flags_and_empty_echo_t519() -> None:
    raw = transcript(reply()).to_dict()
    for row in raw["utterances"]:
        row.pop("echo", None)
        row.pop("multiple_speakers", None)
    old = Transcript.from_dict(raw)
    assert not old.utterances[0].echo and not old.utterances[0].multiple_speakers
    old.utterances.append(reply(1, echo=True, text=""))
    assert len(t.visible_utterances(old)) == 1
    for template in t.TEMPLATES:
        for fmt in t.FORMATS:
            assert "Возможное эхо" not in t.render(template, meeting(), old, fmt)
    assert not c.utterance_row(meeting(), old.utterances[0])["flag"]


def test_live_late_echo_updates_in_place_and_d5_waits_for_publication_t528() -> None:
    m = meeting()
    m.state = MeetingState.TRANSCRIBING
    model = c.TranscriptRows()
    builder = c.CardRows(m, model)
    rows = [reply(), reply(1, multi=True, speaker="S1", start=2000, end=3000)]
    builder.load(rows, complete=False)
    assert model.get(1)["flag"] == ""
    resets = QSignalSpy(model.modelReset)
    changed = QSignalSpy(model.dataChanged)
    rows[0] = replace(rows[0], echo=True)
    builder.sync(rows)
    assert not resets and changed and model.get(0)["flag"] == "echo"
    assert present(m, builder)["marks"][-1]["text"] == "Возможное эхо"
    m.state = MeetingState.DONE
    builder.finish(rows)
    assert model.get(1)["flag"] == "multi"
    assert present(m, builder)["marks"][-1]["text"] == "Возможное эхо: 1 реплика"


def test_marks_survive_relabel_merge_and_readonly_t529() -> None:
    from astra_voice.meetings.model import Speaker, SpeakerMap

    m = meeting()
    rows = [reply(echo=True), reply(1, multi=True, speaker="S2")]
    model = c.TranscriptRows()
    builder = c.CardRows(m, model)
    builder.load(rows, complete=True)
    speakers = SpeakerMap(
        labels={"S1": Speaker(name="Анна"), "S2": Speaker(merged_into="S1")},
    )
    builder.rename(speakers, "S1")
    assert model.get(1)["flag"] == "multi" and model.get(1)["who"] == "Анна"
    assert model.get(0)["flag"] == "echo"
    assert present(m, builder, policy_off=True)["marks"][-1]["text"] == "Возможное эхо: 1 реплика"


def test_track_modes_and_malformed_dual_flag_echo_wins_t530() -> None:
    m = meeting()
    m.transcription.diarization = False
    assert c.utterance_row(m, reply(echo=True, multi=True))["flag"] == "echo"
    assert c.utterance_row(m, reply(multi=True, speaker="S1"))["flag"] == ""
    m.variant = "mic_only"
    m.transcription.diarization = True
    assert c.utterance_row(m, reply(echo=True))["flag"] == ""
    assert c.utterance_row(m, reply(multi=True, speaker="S1"))["flag"] == "multi"
    assert t.utterance_flag(reply(echo=True, multi=True)) == "echo"


@pytest.mark.parametrize("text", sorted(t.PLACEHOLDERS))
def test_placeholder_has_no_reply_mark_t531(text: str) -> None:
    row = c.utterance_row(meeting(), reply(echo=True, text=text))
    assert row["kind"] == "mark" and not row.get("flag") and row["text"]


@pytest.mark.parametrize(
    "state", [MeetingState.DONE, MeetingState.FAILED, MeetingState.NEEDS_MODEL]
)
def test_empty_or_error_has_no_marks_t532(state: MeetingState) -> None:
    m = meeting()
    m.state = state
    builder = c.CardRows(m, c.TranscriptRows())
    builder.load([], complete=True)
    assert not present(m, builder)["showTable"] and not present(m, builder)["marks"]


@pytest.mark.parametrize(
    "n,noun",
    [
        (1, "реплика"),
        (2, "реплики"),
        (4, "реплики"),
        (5, "реплик"),
        (11, "реплик"),
        (12, "реплик"),
        (14, "реплик"),
        (21, "реплика"),
        (22, "реплики"),
    ],
)
def test_last_badge_counts_before_merge_t533(n: int, noun: str) -> None:
    m = meeting()
    m.warnings.audio_server_remote = True
    builder = c.CardRows(m, c.TranscriptRows())
    rows = [reply(i, echo=True, start=i * 1000, end=(i + 1) * 1000) for i in range(n)]
    builder.load(rows, complete=True)
    marks = present(m, builder)["marks"]
    assert marks[-1]["text"] == f"Возможное эхо: {n} {noun}"
    assert marks[-1]["accessibleName"] == marks[-1]["text"]
    assert marks[-1]["tone"] == "neutral" and marks[-1]["icon"] == "speaker"
    assert len(t.template_rows("stenogram", m, rows, [])) == 1


def test_no_echo_badge_for_multiple_speakers_t534() -> None:
    builder = c.CardRows(meeting(), c.TranscriptRows())
    builder.load([reply(multi=True, speaker="S1")], complete=True)
    assert not present(builder.meeting, builder)["marks"]


def test_exact_accessible_names_and_roles_t535() -> None:
    m = meeting()
    row = c.utterance_row(
        m,
        reply(
            echo=True, start=13000, end=14000, text="Тогда начнём с бюджета на четвёртый квартал."
        ),
    )
    assert (
        row["accessibleName"]
        == "0:00:13, Я, возможное эхо: Тогда начнём с бюджета на четвёртый квартал."
    )
    assert row["textMuted"] is True and row["flagIcon"] == "speaker"
    row = c.utterance_row(
        m, reply(multi=True, speaker="S3", start=62000, end=63000, text="Предлагаю перенести…")
    )
    assert row["accessibleName"] == "0:01:02, Спикер 3, несколько говорящих: Предлагаю перенести…"
    assert row["textMuted"] is False and row["flagIcon"] == "people"
    model = c.TranscriptRows()
    model.reset([row])
    assert model.get(0)["accessibleName"] == row["accessibleName"]
    assert c.echo_badge(4)[0]["accessibleName"] == "Возможное эхо: 4 реплики"
    assert c.echo_badge(4, live=True)[0]["accessibleName"] == "Возможное эхо"


@pytest.mark.parametrize("template", TEXT_TEMPLATES)
@pytest.mark.parametrize("fmt", t.FORMATS)
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("gap", [0, 2000])
def test_ordinary_never_merges_with_echo_t541(
    template: str, fmt: str, reverse: bool, gap: int
) -> None:
    rows = [
        reply(echo=reverse, text="Первое"),
        reply(1, echo=not reverse, start=1000 + gap, end=2000 + gap, text="Второе"),
    ]
    data = transcript(*rows)
    assert len(t.template_rows(template, meeting(), rows, [])) == 2
    output = t.render(template, meeting(), data, fmt)
    assert output.count("(возможное эхо)") == 1


@pytest.mark.parametrize("template", TEXT_TEMPLATES)
@pytest.mark.parametrize("fmt", t.FORMATS)
@pytest.mark.parametrize("gap,expected", [(2000, 1), (2001, 2)])
def test_echo_merge_boundary_and_count_t542(
    template: str, fmt: str, gap: int, expected: int
) -> None:
    data = transcript(
        reply(echo=True, text="Первое"),
        reply(1, echo=True, start=1000 + gap, end=2000 + gap, text="Второе"),
    )
    output = t.render(template, meeting(), data, fmt)
    assert output.count("(возможное эхо)") == expected
    assert "Возможное эхо: 2 реплики" in output


@pytest.mark.parametrize("template", TEXT_TEMPLATES)
@pytest.mark.parametrize("fmt", t.FORMATS)
@pytest.mark.parametrize("multi", [False, True])
@pytest.mark.parametrize("gap", [0, 2000])
def test_multi_never_merges_t543(template: str, fmt: str, multi: bool, gap: int) -> None:
    rows = [
        reply(multi=True, speaker="S1"),
        reply(1, multi=multi, speaker="S1", start=1000 + gap, end=2000 + gap),
    ]
    assert len(t.template_rows(template, meeting(), rows, [])) == 2
    output = t.render(template, meeting(), transcript(*rows), fmt)
    assert ("(несколько говорящих)" in output) == (template == "stenogram")


@pytest.mark.parametrize("template", t.TEMPLATES)
@pytest.mark.parametrize("fmt", t.FORMATS)
def test_header_echo_last_and_before_merge_t544(template: str, fmt: str) -> None:
    m = fixture_meeting()
    output = t.render(template, m, transcript(*(reply(i, echo=True) for i in range(4))), fmt)
    header = output.split("\n\n", 2)[1]
    assert header.endswith("Возможное эхо: 4 реплики")
    assert "Хранится на сетевом диске" in header


@pytest.mark.parametrize("template", TEXT_TEMPLATES)
@pytest.mark.parametrize("fmt", t.FORMATS)
@pytest.mark.parametrize("gap,merged", [(2000, True), (2001, False)])
def test_exclude_then_merge_t549(template: str, fmt: str, gap: int, merged: bool) -> None:
    data = transcript(
        reply(text="А"),
        reply(1, echo=True, start=1000, end=1500, text="ЭХО"),
        reply(2, start=1000 + gap, end=2000 + gap, text="Б"),
    )
    output = t.render(template, meeting(), data, fmt, exclude_echo=True)
    assert "ЭХО" not in output
    assert ("А Б" in output) == merged


@pytest.mark.parametrize("template", TEXT_TEMPLATES)
@pytest.mark.parametrize("fmt", t.FORMATS)
def test_excluded_header_and_actual_participants_t550(template: str, fmt: str) -> None:
    data = transcript(*(reply(i, echo=True, text="ЭХО") for i in range(4)), reply(4, speaker="S1"))
    output = t.render(template, meeting(), data, fmt, exclude_echo=True)
    assert "Возможное эхо не включено в файл: 4 реплики" in output
    header = output.split("\n\n", 2)[1]
    assert "`Я`" not in header and "Участники: Я" not in header
    assert "ЭХО" not in output
    normal = t.render(template, meeting(), data, fmt)
    assert "Возможное эхо: 4 реплики" in normal and "ЭХО" in normal
    assert ("Участники: `Я`" if fmt == "md" else "Участники: Я") in normal


@pytest.mark.parametrize("fmt", t.FORMATS)
def test_stats_ignores_checkbox_t551(fmt: str) -> None:
    data = transcript(reply(echo=True), reply(1, speaker="S1"))
    assert t.render("stats", meeting(), data, fmt, exclude_echo=True) == t.render(
        "stats", meeting(), data, fmt
    )


@pytest.mark.parametrize("template", t.TEMPLATES)
@pytest.mark.parametrize("fmt", t.FORMATS)
def test_no_echo_no_header_or_exclusion_t552(template: str, fmt: str) -> None:
    data = transcript(reply(multi=True, speaker="S1"))
    assert "Возможное эхо" not in t.render(template, meeting(), data, fmt, exclude_echo=True)


def test_statistics_excludes_echo_rounds_shares_t556() -> None:
    rows = [
        reply(end=60000),
        reply(1, speaker="S1", end=180000),
        reply(2, echo=True, end=60000),
        reply(3, echo=True, end=60000),
    ]
    stats = t.statistics(meeting(), rows)
    assert stats.replies == 2
    assert [(r.label, r.minutes, r.share, r.replies) for r in stats.speakers] == [
        ("Спикер 1", 3, 75, 1),
        ("Я", 1, 25, 1),
    ]
    stats = t.statistics(meeting(), [reply(i, speaker=f"S{i + 1}") for i in range(3)])
    assert [s.share for s in stats.speakers] == [34, 33, 33]


@pytest.mark.parametrize("n", [1, 2, 5, 11, 14, 61])
@pytest.mark.parametrize("fmt", t.FORMATS)
def test_excluded_statistics_line_t557(n: int, fmt: str) -> None:
    duration = 960000 if n == 61 else 1
    rows = [
        reply(i, echo=True, start=duration * i // n, end=duration * (i + 1) // n) for i in range(n)
    ]
    expected = (
        f"Не учтено в статистике: возможное эхо — {16 if n == 61 else 1} мин · {reply_count(n)}"
    )
    output = t.render("stats", meeting(), transcript(*rows), fmt)
    assert output.rstrip().endswith(expected)
    assert c.save_preview("stats", meeting(), transcript(*rows))["excludedEcho"] == expected


def test_stats_no_echo_has_no_exclusion_t558() -> None:
    data = transcript(reply())
    for fmt in t.FORMATS:
        output = t.render("stats", meeting(), data, fmt)
        assert "Не учтено" not in output and "100 %" in output
    assert not c.save_preview("stats", meeting(), data)["excludedEcho"]


def test_echo_only_participant_not_in_stats_t559() -> None:
    data = transcript(reply(echo=True), reply(1, speaker="S1", end=60000))
    for fmt in t.FORMATS:
        output = t.render("stats", meeting(), data, fmt)
        assert "`Я` |" not in output and "Я —" not in output
    stats = t.statistics(meeting(), data.utterances)
    assert len(stats.speakers) == 1 and stats.speakers[0].share == 100 and stats.replies == 1


def test_multi_entire_duration_to_assigned_speaker_t560() -> None:
    data = transcript(reply(end=60000), reply(1, multi=True, speaker="S3", end=60000))
    stats = t.statistics(meeting(), data.utterances)
    assert [(s.minutes, s.share, s.replies) for s in stats.speakers] == [(1, 50, 1), (1, 50, 1)]
    for fmt in t.FORMATS:
        assert "несколько говорящих" not in t.render("stats", meeting(), data, fmt)


def test_participant_panel_minutes_same_arithmetic_t561() -> None:
    rows = [reply(end=60000), reply(1, speaker="S1", end=180000), reply(2, echo=True, end=120000)]
    builder = c.CardRows(meeting(), c.TranscriptRows())
    builder.load(rows, complete=True)
    assert builder.minutes() == t.speaker_minutes(rows) == {"me": 1, "S1": 3}
    assert [p["minutes"] for p in present(builder.meeting, builder)["participants"]] == [
        "1 мин",
        "3 мин",
    ]


def test_new_constant_texts_lexicon_t562() -> None:
    from test_meeting_lexicon import ROOT, violations

    assert not violations(ROOT / "src/astra_voice/ui/meeting_texts.py")
    forbidden = ("NCC", "гейт", "корреляция", "диаризация", "дубль")
    assert not any(
        word.lower() in text.lower() for text in MARK_TEXTS.values() for word in forbidden
    )


@pytest.mark.parametrize("text", ["возможное эхо", "несколько говорящих", "<b>возможное эхо</b>"])
def test_text_data_never_becomes_a_mark_t563(text: str) -> None:
    row = reply(text=text)
    assert not c.utterance_row(meeting(), row)["flag"]
    for template in t.TEMPLATES:
        output = t.render(template, meeting(), transcript(row), "md")
        assert "Возможное эхо:" not in output
    assert t.echo_count([row]) == 0


@pytest.mark.parametrize("name", ["возможное эхо", "несколько говорящих", "Я (возможное эхо)"])
@pytest.mark.parametrize("template", t.TEMPLATES)
@pytest.mark.parametrize("fmt", t.FORMATS)
def test_names_are_literal_data_and_filter_by_flag_t564(name: str, template: str, fmt: str) -> None:
    m = meeting()
    m.self_label = name
    data = transcript(
        reply(text="Настоящая речь"), reply(1, echo=True, start=2000, end=3000, text="ЭХО")
    )
    before = deepcopy(data.to_dict())
    output = t.render(template, m, data, fmt, exclude_echo=True)
    assert "Настоящая речь" in output if template != "stats" else "реплик 1" in output
    assert "ЭХО" not in output
    if template not in ("stats", "text_only"):
        assert (f"`{name}`:" if fmt == "md" else f"{name}:") in output
    assert data.to_dict() == before


@pytest.mark.parametrize("name", ["возможное эхо", "несколько говорящих", "Я (возможное эхо)"])
@pytest.mark.parametrize("template", t.TEMPLATES)
@pytest.mark.parametrize("fmt", t.FORMATS)
def test_participant_names_and_real_marks_are_separate_t564(
    name: str, template: str, fmt: str
) -> None:
    from astra_voice.meetings.model import Speaker, SpeakerMap

    m = meeting()
    names = SpeakerMap(labels={"S1": Speaker(name=name)})
    data = transcript(
        reply(speaker="S1", text="Первая"),
        reply(1, speaker="S1", multi=True, start=1000, end=2000, text="Вторая"),
        reply(2, echo=True, start=3000, end=4000, text="ЭХО"),
    )
    output = t.render(template, m, data, fmt, speakers=names, exclude_echo=True)
    assert "ЭХО" not in output
    if template == "stenogram":
        label = t._md_escape(name) if fmt == "md" else name
        assert f"{label} (несколько говорящих):" in output
        assert f"{label}:" in output
    assert len(t.statistics(m, data.utterances, speakers=names).speakers) == 1


def test_merge_keeps_panel_and_statistics_minutes_without_echo_t561() -> None:
    from astra_voice.meetings.model import Speaker, SpeakerMap

    m = meeting()
    rows = [
        reply(end=60000),
        reply(1, speaker="S1", end=120000),
        reply(2, speaker="S2", end=60000),
        reply(3, echo=True, end=120000),
    ]
    model = c.TranscriptRows()
    builder = c.CardRows(m, model)
    builder.load(rows, complete=True)
    names = SpeakerMap(labels={"S1": Speaker(name="Ира"), "S2": Speaker(merged_into="S1")})
    builder.rename(names, "S1")
    assert builder.minutes() == t.speaker_minutes(rows, names) == {"me": 1, "S1": 3}
    assert [(s.label, s.minutes) for s in t.statistics(m, rows, speakers=names).speakers] == [
        ("Ира", 3),
        ("Я", 1),
    ]
    assert [p["minutes"] for p in present(m, builder)["participants"]] == ["1 мин", "3 мин"]
    badge = present(m, builder)["marks"][-1]
    assert badge["label"] + " " + badge["mono"] + badge["suffix"] == badge["text"]


@pytest.mark.parametrize("fmt", t.FORMATS)
@pytest.mark.parametrize("surface", ["file", "preview"])
def test_stats_header_excludes_echo_only_participant_round2(fmt: str, surface: str) -> None:
    m = meeting()
    data = transcript(reply(echo=True, end=60000), reply(1, speaker="S1", end=60000))
    exclusion = "Не учтено в статистике: возможное эхо — 1 мин · 1 реплика"
    if surface == "file":
        output = t.render("stats", m, data, fmt)
        participants = next(line for line in output.splitlines() if "Участники:" in line)
        expected = "- Участники: `Спикер 1`" if fmt == "md" else "Участники: Спикер 1"
        assert participants == expected
        assert "Возможное эхо: 1 реплика" in output and exclusion in output
        assert t.render("stats", m, data, fmt, exclude_echo=True) == output
    else:
        preview = c.save_preview("stats", m, data)
        participants = next(line for line in preview["header"] if "Участники:" in line)
        assert participants == "Участники: Спикер 1"
        assert any("Возможное эхо: 1 реплика" in line for line in preview["header"])
        assert preview["excludedEcho"] == exclusion
        assert c.save_preview("stats", m, data, exclude_echo=True) == preview
