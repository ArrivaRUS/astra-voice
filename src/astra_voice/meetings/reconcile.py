"""D5 deterministic cuts and conservative text checks; no acoustic dependencies."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher

from astra_voice.meetings.model import DiarTurn, Utterance

MIN_PART_MS = 1000
CONTEXT_MS = 300
MAX_TEXT_DISTANCE = 0.25


@dataclass(frozen=True)
class Part:
    start_ms: int
    end_ms: int
    speaker: str


def parts(utterance: Utterance, turns: Sequence[DiarTurn]) -> list[Part] | None:
    """None: one speaker/overlap; []: mixed but cannot safely cut.

    Gaps between consecutive speakers are divided at their midpoint. Adjacent
    intervals with the same canonical label coalesce. Short pieces join the
    longer neighbour (tie: left); the merged label uses greatest overlap.
    """
    relevant = sorted(
        (t for t in turns if t.start_ms < utterance.end_ms and t.end_ms > utterance.start_ms),
        key=lambda t: (t.start_ms, t.end_ms, t.label),
    )
    merged: list[Part] = []
    for t in relevant:
        a, b = max(t.start_ms, utterance.start_ms), min(t.end_ms, utterance.end_ms)
        if merged and a < merged[-1].end_ms and t.label != merged[-1].speaker:
            return None  # simultaneous speakers are never resolved here
        if merged and t.label == merged[-1].speaker:
            old = merged.pop()
            merged.append(Part(old.start_ms, max(old.end_ms, b), t.label))
        else:
            merged.append(Part(a, b, t.label))
    if len(merged) < 2:
        return None
    cuts = [utterance.start_ms]
    cuts += [(a.end_ms + b.start_ms) // 2 for a, b in zip(merged, merged[1:], strict=False)]
    cuts += [utterance.end_ms]
    ranges = list(zip(cuts, cuts[1:], strict=False))
    while len(ranges) > 1:
        short = next((i for i, (a, b) in enumerate(ranges) if b - a < MIN_PART_MS), None)
        if short is None:
            break
        i = short
        left = ranges[i - 1][1] - ranges[i - 1][0] if i else -1
        right = ranges[i + 1][1] - ranges[i + 1][0] if i + 1 < len(ranges) else -1
        if left >= right:
            ranges[i - 1 : i + 1] = [(ranges[i - 1][0], ranges[i][1])]
        else:
            ranges[i : i + 2] = [(ranges[i][0], ranges[i + 1][1])]
    if len(ranges) < 2 or any(b - a < MIN_PART_MS for a, b in ranges):
        return []
    result = []
    for a, b in ranges:
        coverage: dict[str, int] = {}
        for region in merged:
            coverage[region.speaker] = coverage.get(region.speaker, 0) + max(
                0, min(b, region.end_ms) - max(a, region.start_ms)
            )
        speaker = min(coverage, key=lambda key: (-coverage[key], int(key[1:])))
        result.append(Part(a, b, speaker))
    # A swallowed short speaker can leave adjacent pieces of the same speaker.
    compact: list[Part] = []
    for part in result:
        if compact and compact[-1].speaker == part.speaker:
            before = compact.pop()
            part = Part(before.start_ms, part.end_ms, part.speaker)
        compact.append(part)
    return compact if len(compact) > 1 else []


def candidates(
    utterances: Sequence[Utterance], turns: Sequence[DiarTurn], target: str
) -> list[tuple[Utterance, list[Part]]]:
    result = []
    for u in sorted(utterances, key=lambda u: (u.start_ms, u.id)):
        if u.track == target:
            split = parts(u, turns)
            if split is not None:
                result.append((u, split))
    return result


def words(text: str) -> list[str]:
    return re.findall(r"\w+", text.casefold().replace("ё", "е"))


def trusted(original: str, texts: Sequence[str]) -> bool:
    """Reject any repeated word at a join, or >25% word edit distance.

    Case/punctuation/ё are ignored. A quarter permits local ASR corrections but
    rejects a changed sentence; it is a conservative gate, not measured WER.
    """
    chunks = [words(t) for t in texts]
    if not all(chunks):
        return False
    if any(
        a[-count:] == b[:count]
        for a, b in zip(chunks, chunks[1:], strict=False)
        for count in range(1, min(len(a), len(b)) + 1)
    ):
        return False
    before, after = words(original), [w for chunk in chunks for w in chunk]
    if not before:
        return False
    # Bounded by 24s utterances, but cap pathological model output as well.
    if max(len(before), len(after)) > 512:
        return False
    row = list(range(len(after) + 1))
    for i, a in enumerate(before, 1):
        new = [i]
        for j, b in enumerate(after, 1):
            new.append(min(new[-1] + 1, row[j] + 1, row[j - 1] + (a != b)))
        row = new
    return row[-1] <= MAX_TEXT_DISTANCE * max(len(before), len(after))


def context_agrees(padded: str, core: str) -> bool:
    """Core must occur intact in the contextual pass; never emit context words."""
    a, b = words(padded), words(core)
    if not b or max(len(a), len(b)) > 512:
        return False
    match = SequenceMatcher(None, a, b, autojunk=False).find_longest_match()
    return match.size == len(b)
