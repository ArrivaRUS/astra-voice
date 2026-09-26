"""Отчёт о падении сохраняет диагностику и освобождает кадры xvfb-теста."""

from __future__ import annotations

import sys
import weakref
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = pytest.mark.unit
CONFTEST = Path(__file__).resolve().parents[1] / "conftest.py"


def _install_hook(pytester: pytest.Pytester) -> None:
    pytester.makeini(
        """[pytest]
markers =
    xvfb: тест с настоящим QML
    unit: модульный тест
"""
    )
    pytester.makeconftest(
        f"""
import importlib.util
spec = importlib.util.spec_from_file_location("frame_cleanup_hook", {str(CONFTEST)!r})
assert spec is not None and spec.loader is not None
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
pytest_runtest_makereport = module.pytest_runtest_makereport
"""
    )


def test_xvfb_failure_releases_local_references_after_report(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    references: list[weakref.ReferenceType[object]] = []
    witness = ModuleType("frame_cleanup_witness")
    witness.__dict__["references"] = references
    monkeypatch.setitem(sys.modules, witness.__name__, witness)
    _install_hook(pytester)
    pytester.makepyfile(
        """
import weakref
import pytest
import frame_cleanup_witness

class Held:
    pass

@pytest.mark.xvfb
def test_failure():
    held = Held()
    frame_cleanup_witness.references.append(weakref.ref(held))
    actual = 1
    expected = 2
    assert actual == expected
"""
    )
    result = pytester.runpytest_inprocess("-q", "-l")
    result.assert_outcomes(failed=1)
    report = result.stdout.str()
    assert "assert actual == expected" in report
    assert "assert 1 == 2" in report
    assert "actual = 1" in report
    assert "expected = 2" in report
    assert len(references) == 1
    assert references[0]() is None


def test_non_xvfb_failure_shows_locals(pytester: pytest.Pytester) -> None:
    _install_hook(pytester)
    pytester.makepyfile(
        """
import pytest

@pytest.mark.unit
def test_failure():
    actual = 1
    expected = 2
    assert actual == expected
"""
    )
    result = pytester.runpytest_inprocess("-q", "-l")
    result.assert_outcomes(failed=1)
    report = result.stdout.str()
    assert "actual = 1" in report
    assert "expected = 2" in report


def test_xvfb_failure_respects_tracebackhide(pytester: pytest.Pytester) -> None:
    _install_hook(pytester)
    pytester.makepyfile(
        """
import pytest

def hidden_helper():
    __tracebackhide__ = True
    pytest.fail("HIDDEN_HELPER_SENTINEL")

@pytest.mark.xvfb
def test_failure():
    hidden_helper()
"""
    )
    result = pytester.runpytest_inprocess("-q")
    result.assert_outcomes(failed=1)
    report = result.stdout.str()
    assert "HIDDEN_HELPER_SENTINEL" in report
    assert 'pytest.fail("HIDDEN_HELPER_SENTINEL")' not in report
    assert "in hidden_helper" not in report
