"""One module reference keeps allocator stubs scoped without import hooks."""

from __future__ import annotations

import ctypes
import gc
import os
import subprocess
import sys
from pathlib import Path

import pytest

from astra_voice.worker import engine

pytestmark = pytest.mark.unit


def test_heap_trims_are_recorded_without_gc_or_libc(
    monkeypatch: pytest.MonkeyPatch, recorded_heap_trim: list[None]
) -> None:
    def forbidden(*args: object) -> None:
        raise AssertionError("ordinary unit test reached the native allocator or GC")

    monkeypatch.setattr(gc, "collect", forbidden)
    monkeypatch.setattr(ctypes, "CDLL", forbidden)
    engine._trim_heap()
    engine._trim_malloc()
    assert recorded_heap_trim == [None, None]


def run_copied_fixture(
    tmp_path: Path, engine_source: str | None, tests: str, *, expected: int = 0
) -> str:
    conftest = Path(__file__).resolve().parents[1] / "conftest.py"
    (tmp_path / "conftest.py").write_text(conftest.read_text())
    (tmp_path / "pytest.ini").write_text("[pytest]\npythonpath = src\n")
    package = tmp_path / "src/astra_voice/worker"
    package.mkdir(parents=True)
    (package.parent / "__init__.py").write_text("")
    (package / "__init__.py").write_text("")
    if engine_source is not None:
        (package / "engine.py").write_text(engine_source)
    (package / "meeting_transcribe.py").write_text(
        "from . import engine\ndef trim():\n    engine._trim_heap()\n    engine._trim_malloc()\n"
    )
    (tmp_path / "test_probe.py").write_text(tests)
    env = dict(os.environ)
    for key in ("PYTHONPATH", "PYTEST_ADDOPTS", "ASTRA_VOICE_REQUIRE_QT"):
        env.pop(key, None)
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )
    output = result.stdout + result.stderr
    assert result.returncode == expected, output
    return output


def test_late_module_reference_and_real_trim_switch(tmp_path: Path) -> None:
    output = run_copied_fixture(
        tmp_path,
        "calls = []\ndef _trim_heap():\n    calls.append('heap')\n"
        "def _trim_malloc():\n    calls.append('malloc')\n",
        """
import importlib
import sys
import pytest
FINDERS = tuple(sys.meta_path)

def test_01_late_import(recorded_heap_trim, monkeypatch):
    assert tuple(sys.meta_path) == FINDERS
    assert 'astra_voice.worker.meeting_transcribe' not in sys.modules
    engine = sys.modules['astra_voice.worker.engine']
    engine._trim_heap()
    engine._trim_malloc()
    mt = importlib.import_module('astra_voice.worker.meeting_transcribe')
    assert mt.engine is engine
    mt.trim()
    assert recorded_heap_trim == [None] * 4
    assert engine.calls == []
    monkeypatch.setattr(engine, '_trim_heap', lambda: None)
    monkeypatch.setattr(engine, '_trim_malloc', lambda: None)

@pytest.mark.usefixtures('real_heap_trim')
def test_02_real():
    from astra_voice.worker import engine, meeting_transcribe as mt
    assert mt.engine is engine
    assert engine._trim_heap.__module__ == engine.__name__
    assert engine._trim_malloc.__module__ == engine.__name__
    mt.trim()
    assert engine.calls == ['heap', 'malloc']

def test_03_stub_again(recorded_heap_trim, monkeypatch):
    from astra_voice.worker import engine, meeting_transcribe as mt
    engine._trim_heap()
    engine._trim_malloc()
    mt.trim()
    assert recorded_heap_trim == [None] * 4
    assert engine.calls == ['heap', 'malloc']
    monkeypatch.setattr(engine, '_trim_heap', lambda: None)
    monkeypatch.setattr(engine, '_trim_malloc', lambda: None)

@pytest.mark.usefixtures('real_heap_trim')
def test_04_real_again():
    from astra_voice.worker import engine, meeting_transcribe as mt
    assert mt.engine is engine
    assert engine._trim_heap.__module__ == engine.__name__
    assert engine._trim_malloc.__module__ == engine.__name__
""",
    )
    assert "4 passed" in output


def test_copied_conftest_without_engine_is_harmless(tmp_path: Path) -> None:
    output = run_copied_fixture(tmp_path, None, "def test_plain():\n    assert True\n")
    assert "1 passed" in output


@pytest.mark.parametrize(
    "broken", ["import missing_heap_trim_dependency", "raise ImportError('broken worker')"]
)
def test_real_worker_import_errors_are_not_hidden(tmp_path: Path, broken: str) -> None:
    output = run_copied_fixture(
        tmp_path, broken + "\n", "def test_plain():\n    assert True\n", expected=1
    )
    assert "ERROR at setup" in output
    assert "missing_heap_trim_dependency" in output or "broken worker" in output
