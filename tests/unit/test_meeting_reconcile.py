"""T-405: D5 pure segmentation, canonical speakers and conservative text joins."""

from __future__ import annotations

import pytest

from astra_voice.meetings.model import DiarTurn, Transcript, Utterance
from astra_voice.meetings.reconcile import Part, candidates, context_agrees, parts, trusted

pytestmark = pytest.mark.unit


def u(start: int = 0, end: int = 4000, uid: str = "u1") -> Utterance:
    return Utterance(uid, track="far", start_ms=start, end_ms=end)


def turns(*rows: tuple[str, int, int]) -> list[DiarTurn]:
    return [DiarTurn(*row) for row in rows]


@pytest.mark.parametrize(
    "rows,expected",
    [
        ([("S1", 0, 2000), ("S2", 2000, 4000)], [Part(0, 2000, "S1"), Part(2000, 4000, "S2")]),
        ([("S1", 0, 3000), ("S2", 2000, 4000)], None),
        ([("S1", 0, 2000), ("S1", 1000, 4000)], None),
        ([("S1", 0, 500), ("S2", 500, 4000)], []),
        (
            [("S1", 0, 1000), ("S2", 1000, 2000), ("S3", 2000, 4000)],
            [Part(0, 1000, "S1"), Part(1000, 2000, "S2"), Part(2000, 4000, "S3")],
        ),
        # Short middle joins the longer right neighbour.
        (
            [("S1", 0, 1200), ("S2", 1200, 1700), ("S3", 1700, 4000)],
            [Part(0, 1200, "S1"), Part(1200, 4000, "S3")],
        ),
        # Equal neighbours: short middle joins left.
        (
            [("S1", 0, 1750), ("S2", 1750, 2250), ("S3", 2250, 4000)],
            [Part(0, 2250, "S1"), Part(2250, 4000, "S3")],
        ),
        # Gap midpoint is stable.
        ([("S1", 0, 1600), ("S2", 2400, 4000)], [Part(0, 2000, "S1"), Part(2000, 4000, "S2")]),
    ],
)
def test_cut_rules(rows: list[tuple[str, int, int]], expected: list[Part] | None) -> None:
    assert parts(u(), turns(*rows)) == expected


def test_candidates_order_and_track() -> None:
    rows = [u(uid="z"), u(uid="a"), u(5000, 9000, "b"), Utterance("mic")]
    intervals = turns(("S1", 0, 2000), ("S2", 2000, 7000), ("S3", 7000, 9000))
    assert [item.id for item, _ in candidates(rows, intervals, "far")] == ["a", "z", "b"]


@pytest.mark.parametrize(
    "original,texts,expected",
    [
        ("Первый ответ. Второй ответ.", ["первый ответ", "второй ответ"], True),
        ("один два три четыре", ["один два", "три пять"], True),  # exactly 25%
        ("один два три четыре", ["один семь", "три пять"], False),
        ("первый ответ второй", ["первый ответ", "ответ второй"], False),
        (
            "один два три четыре пять шесть семь восемь",
            ["один два три четыре", "три четыре пять шесть семь восемь"],
            False,
        ),
        ("один два", ["один", ""], False),
        ("Ёлка зелёная", ["елка", "зеленая"], True),
    ],
)
def test_text_gate(original: str, texts: list[str], expected: bool) -> None:
    assert trusted(original, texts) is expected


def test_context_never_becomes_core() -> None:
    assert context_agrees("чужой текст первый ответ чужой текст", "первый ответ")
    assert not context_agrees("чужой текст", "первый ответ")


def test_old_transcript_defaults_marker_and_progress() -> None:
    document = Transcript("2026-11-10_120000-aaaa", utterances=[u()]).to_dict()
    document.pop("reconcile")
    document["utterances"][0].pop("multiple_speakers")
    loaded = Transcript.from_dict(document)
    assert not loaded.utterances[0].multiple_speakers
    assert loaded.reconcile.used_ms == 0
