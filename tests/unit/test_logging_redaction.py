"""Recursive secret-key filtering for legacy logger arguments and extras."""

from __future__ import annotations

import logging

import pytest

from astra_voice.core.logging import REDACTED, SECRET_FIELDS, RedactTextFilter

pytestmark = pytest.mark.unit
MARKER = "NESTED_SECRET_MARKER"


@pytest.mark.parametrize("field", sorted(SECRET_FIELDS))
@pytest.mark.parametrize("where", ["mapping", "positional", "extra"])
def test_nested_secret_keys_are_redacted_for_repr(field: str, where: str) -> None:
    original = {field: MARKER}
    payload = [{"safe": (original,), "count": 12}]
    record = logging.LogRecord("dictation", logging.INFO, __file__, 1, "%(payload)r", (), None)
    if where == "mapping":
        record.args = {"payload": payload}
    elif where == "positional":
        record.msg, record.args = "%r", (payload,)
    else:
        record.msg = "legacy"
        record.__dict__["payload"] = payload
    RedactTextFilter().filter(record)
    output = logging.Formatter("%(message)s %(payload)r" if where == "extra" else "%(message)s")
    assert MARKER not in output.format(record)
    assert REDACTED in output.format(record)
    assert original[field] == MARKER  # never mutate caller data


@pytest.mark.parametrize("where", ["mapping", "extra"])
def test_recursive_secret_filter_handles_cycles(where: str) -> None:
    payload: dict[str, object] = {"api_key": MARKER}
    payload["cycle"] = [payload]
    record = logging.LogRecord("dictation", logging.INFO, __file__, 1, "%r", (), None)
    if where == "mapping":
        record.args = (payload,)
    else:
        record.__dict__["payload"] = payload
    RedactTextFilter().filter(record)
    assert MARKER not in repr(record.args if where == "mapping" else record.__dict__["payload"])


def test_recursive_cleaning_does_not_call_arbitrary_repr() -> None:
    class Hostile:
        def __repr__(self) -> str:
            raise AssertionError("repr must not be called while cleaning")

    record = logging.LogRecord("dictation", logging.INFO, __file__, 1, "legacy", (), None)
    record.__dict__["payload"] = {"api_key": Hostile(), "other": Hostile()}
    RedactTextFilter().filter(record)
    assert record.__dict__["payload"]["api_key"] == REDACTED
