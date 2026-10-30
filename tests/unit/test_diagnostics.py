"""Diagnostic archive backend: private output and exclusive publication."""

from __future__ import annotations

import stat
import zipfile
from pathlib import Path

import pytest

from astra_voice.core.diagnostics import collect_diagnostics
from astra_voice.core.settings import Settings

pytestmark = pytest.mark.unit


def test_archive_is_private_readable_and_exclusive(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir(mode=0o700)
    output = tmp_path / "diagnostics.zip"
    assert collect_diagnostics(output, settings=Settings(), data_dir=data) == output
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    before = output.read_bytes()
    assert zipfile.is_zipfile(output)
    with pytest.raises(FileExistsError):
        collect_diagnostics(output, settings=Settings(), data_dir=data)
    assert output.read_bytes() == before
