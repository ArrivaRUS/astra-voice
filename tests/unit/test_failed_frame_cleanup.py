"""Падение теста не должно оставлять локальные объекты в traceback pytest."""

from __future__ import annotations

import sys
import weakref
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = pytest.mark.unit
CONFTEST = Path(__file__).resolve().parents[1] / "conftest.py"


def test_failed_test_releases_local_references(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    references: list[weakref.ReferenceType[object]] = []
    witness = ModuleType("frame_cleanup_witness")
    witness.__dict__["references"] = references
    monkeypatch.setitem(sys.modules, witness.__name__, witness)
    pytester.makeconftest(
        f"""
import importlib.util
spec = importlib.util.spec_from_file_location("frame_cleanup_hook", {str(CONFTEST)!r})
assert spec is not None and spec.loader is not None
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
pytest_runtest_call = module.pytest_runtest_call
"""
    )
    pytester.makepyfile(
        """
import weakref
import frame_cleanup_witness

class Held:
    pass

def test_failure():
    held = Held()
    frame_cleanup_witness.references.append(weakref.ref(held))
    actual = 1
    expected = 2
    assert actual == expected
"""
    )
    result = pytester.runpytest_inprocess("-q")
    result.assert_outcomes(failed=1)
    report = result.stdout.str()
    assert "assert actual == expected" in report
    assert "assert 1 == 2" in report
    assert len(references) == 1
    assert references[0]() is None
