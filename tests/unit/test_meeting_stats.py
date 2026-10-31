"""T-425/T-439: strings outside a finite meeting-event vocabulary disappear."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from astra_voice.core import stats as st

pytestmark = pytest.mark.unit
MARKER = "PRIVATE_STATS_MARKER"


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setattr(st, "_pending", {})


@pytest.mark.parametrize(
    "kind",
    [
        "meeting_capture",
        "meeting_transcribe",
        "meeting_diar",
        "meeting_ai",
        "meeting_detector",
        "meeting_policy",
        "meeting_lifecycle",
    ],
)
def test_meeting_stats_roundtrip_numeric_and_enumerated_fields(kind: str) -> None:
    stats = st.Stats()
    stats.append(kind, duration_ms=1200, bytes=42, app_class="dion", result="ok", code="timeout")
    stats.flush()
    assert st.Stats().events()[0] == stats.events()[0]
    assert stats.events()[0]["app_class"] == "dion" and stats.events()[0]["bytes"] == 42


@pytest.mark.parametrize(
    "field",
    [
        "meeting_title",
        "speaker_name",
        "notice_text",
        "api_key",
        "authorization",
        "prompt",
        "completion",
        "segment_text",
        "media.name",
        "application.name",
        "binary",
        "pid",
        "endpoint",
        "exception",
        "code",
        "result",
        "app_class",
        "duration_ms",
    ],
)
def test_raw_meeting_fields_are_dropped_not_raised(field: str) -> None:
    stats = st.Stats()
    stats.append("meeting_capture", **{field: MARKER})
    stats.flush()
    assert MARKER not in json.dumps(stats.events())
    assert MARKER not in stats._path.read_text()


@pytest.mark.parametrize("value", [-1, float("nan"), float("inf"), True, "42", 10**400])
def test_invalid_numbers_drop_without_exception(value: object) -> None:
    stats = st.Stats()
    stats.append("meeting_capture", duration_ms=value)
    assert "duration_ms" not in stats.events()[0]


def test_loaded_raw_fields_are_not_resaved_and_unknown_type_is_canonical() -> None:
    stats = st.Stats()
    stats._path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "events": [{"type": "meeting_" + MARKER, "ts": 1, "title": MARKER, "bytes": 42}],
            }
        )
    )
    stats = st.Stats()
    assert stats.events()[0]["type"] == "meeting_unknown"
    listener: list[str] = []
    stats.on_append = listener.append
    stats.append("meeting_" + MARKER, media_name=MARKER)
    stats.flush()
    assert MARKER not in stats._path.read_text() and MARKER not in repr(listener)


def test_raw_detector_identifiers_are_not_allowed_even_when_numeric() -> None:
    stats = st.Stats()
    stats.append(
        "meeting_detector", pid=12345, sink_input=99, object_serial=123, app_class="other", count=1
    )
    assert set(stats.events()[0]) == {"type", "ts", "app_class", "count"}


@pytest.mark.parametrize(
    "field,value", [("app_class", "dion"), ("result", "ok"), ("code", "timeout")]
)
def test_string_subclass_cannot_export_custom_str(field: str, value: str) -> None:
    class Hostile(str):
        def __str__(self) -> str:
            return MARKER

    stats = st.Stats()
    stats.append("meeting_capture", **{field: Hostile(value)})
    stats.flush()
    assert field not in stats.events()[0]
    assert MARKER not in stats._path.read_text()


@pytest.mark.parametrize("field", ["state", "reason", "exception", "code"])
def test_trusted_enum_values_are_canonical_strings(field: str) -> None:
    from astra_voice.meetings import events

    enum_type = {
        "state": events.MeetingState,
        "reason": events.PauseReason,
        "exception": events.ExceptionClass,
        "code": events.PolicyCode,
    }[field]
    # Select a real member without relying on names shared by different enums.
    member = next(iter(enum_type))
    stats = st.Stats()
    stats.append("meeting_capture", **{field: member})
    assert type(stats.events()[0][field]) is str
    assert stats.events()[0][field] == member.value


@pytest.mark.parametrize("where", ["type", "key"])
def test_subclass_cannot_spoof_stats_type_or_field_membership(where: str) -> None:
    from astra_voice.meetings.events import stats_event

    class Spoof(str):
        def __hash__(self) -> int:
            return hash("meeting_capture" if where == "type" else "bytes")

        def __eq__(self, other: object) -> bool:
            return True

    if where == "type":
        event = stats_event(Spoof("meeting_" + MARKER), {})
        assert event == {"type": "meeting_unknown"}
    else:
        event = stats_event("meeting_capture", {Spoof(MARKER): 42})
        assert event == {"type": "meeting_capture"}
    assert MARKER not in json.dumps(event)
