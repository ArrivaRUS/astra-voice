"""Meeting-local text names and irreversible aliases; no voice identity or logging."""

from __future__ import annotations

import unicodedata
from collections.abc import Iterable
from copy import deepcopy

from astra_voice.meetings.model import (
    DEFAULT_SELF_LABEL,
    Meeting,
    Speaker,
    SpeakerMap,
    Utterance,
)


def canonical_speaker(label: str, speakers: SpeakerMap | None = None) -> str:
    """Resolve an alias chain without renumbering; reject malformed in-memory maps."""
    if label in {"me", "others"}:
        return label
    seen: set[str] = set()
    while speakers is not None and label in speakers.labels:
        if label in seen:
            raise ValueError("Недопустимое объединение участников")
        seen.add(label)
        target = speakers.labels[label].merged_into
        if target is None:
            break
        if target not in speakers.labels:
            raise ValueError("Недопустимое объединение участников")
        label = target
    return label


def speaker_number(label: str, speakers: SpeakerMap | None = None) -> int | None:
    label = canonical_speaker(label, speakers)
    return (
        int(label[1:])
        if label.startswith("S") and label[1:].isascii() and label[1:].isdigit()
        else None
    )


def speaker_label(
    meeting: Meeting, label: str, speakers: SpeakerMap | None = None, *, file: bool = False
) -> str:
    """One literal label policy for UI and every named file template."""
    label = canonical_speaker(label, speakers)
    if label == "me":
        return meeting.self_label.strip() or DEFAULT_SELF_LABEL
    number = speaker_number(label)
    if number is None:
        return "Участники" if meeting.variant == "mic_only" else "Собеседники"
    name = speakers.labels[label].name if speakers and label in speakers.labels else None
    fallback = f"Спикер {number}"
    return name or fallback


def validate_name(value: str) -> str | None:
    # Reject rather than silently change control characters, including bidi controls.
    if any(unicodedata.category(c).startswith("C") or c in "\u2028\u2029" for c in value):
        raise ValueError("Имя не должно содержать управляющие символы или переводы строк")
    value = value.strip()
    if len(value) > 60:
        raise ValueError("Имя должно быть не длиннее 60 символов")
    return value or None


def validated_map(labels: Iterable[str], saved: SpeakerMap | None) -> SpeakerMap:
    """Ignore invalid records/chains, without I/O; only published S labels survive."""
    available = {label for label in labels if speaker_number(label) is not None}
    result = SpeakerMap(labels={label: Speaker() for label in available})
    if saved is None:
        return result
    for label, record in saved.labels.items():
        if label not in available:
            continue
        seen = {label}
        target = record.merged_into
        valid = True
        while target is not None:
            if target not in available or target in seen:
                valid = False
                break
            seen.add(target)
            next_record = saved.labels.get(target)
            target = next_record.merged_into if next_record else None
        try:
            name = validate_name(record.name) if record.name is not None else None
        except ValueError:
            valid = False
            name = None
        if valid:
            result.labels[label] = Speaker(name=name, merged_into=record.merged_into)
    return result


def speaker_map(utterances: Iterable[Utterance], saved: SpeakerMap | None) -> SpeakerMap:
    return validated_map((row.speaker for row in utterances), saved)


def rename(speakers: SpeakerMap, label: str, name: str) -> SpeakerMap:
    if label not in speakers.labels or speaker_number(label) is None:
        raise ValueError("Участник недоступен")
    if canonical_speaker(label, speakers) != label:
        raise ValueError("Участник уже объединён")
    result = deepcopy(speakers)
    result.labels[label].name = validate_name(name)
    return result


def merge(speakers: SpeakerMap, source: str, target: str) -> SpeakerMap:
    if (
        source == target
        or any(
            label not in speakers.labels or speaker_number(label) is None
            for label in (source, target)
        )
        or canonical_speaker(source, speakers) != source
        or canonical_speaker(target, speakers) != target
    ):
        raise ValueError("Нельзя объединить этих участников")
    result = deepcopy(speakers)
    result.labels[source] = Speaker(merged_into=target)
    return result
