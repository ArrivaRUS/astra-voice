"""F18.11 matrix: real file loader, parser, effective settings and invariants."""

from pathlib import Path
from typing import Any

import pytest
from tools.meeting_policy_check import check_case, check_invariants, load_case, matrix_cases

from astra_voice.core.policy import PolicyStatus
from astra_voice.meetings.policy import parse_policy

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("case", matrix_cases(), ids=lambda case: case.id)
def test_policy_matrix(case: Any, tmp_path: Path) -> None:
    source = load_case(case, tmp_path)
    assert source.status == (
        PolicyStatus.ABSENT
        if case.absent
        else PolicyStatus.INVALID
        if case.broken
        else PolicyStatus.OK
    )
    assert bool(source.permissions_warning) == (case.mode == 0o666 or case.uid != 0)
    parsed = parse_policy(source)
    assert not check_case(case, parsed)
    assert not check_invariants(parsed)
