"""Repr checks that are independent of process memory layout."""

import re

import pytest

pytestmark = pytest.mark.unit

_OBJECT_ADDRESS = re.compile(r"(<[\w.]+ object at )0x[0-9a-fA-F]+(?=>)")


def repr_without_object_addresses(value: object) -> str:
    """Omit only the pointer in Python's default object representation."""
    return _OBJECT_ADDRESS.sub(r"\1<address>", repr(value))


@pytest.mark.parametrize("leak", ["", " sink_input=701", " sink_input=0x701"])
def test_only_object_addresses_hide_numeric_markers(leak: str) -> None:
    class FixedAddress:
        def __repr__(self) -> str:
            return (
                "<astra_voice.worker.meeting_identity_check.PinnedTarget object at 0x7019446e2f90>"
                + leak
            )

    assert ("701" in repr_without_object_addresses(FixedAddress())) is bool(leak)
