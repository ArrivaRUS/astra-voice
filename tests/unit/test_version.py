"""Версия: _version.py → debian/changelog → запасное значение."""

from __future__ import annotations

from pathlib import Path

import pytest

from astra_voice.core import version

pytestmark = pytest.mark.unit


def test_changelog_first_line_parsed(tmp_path: Path) -> None:
    changelog = tmp_path / "changelog"
    changelog.write_text(
        "astra-voice (0.1.0~m1) unstable; urgency=medium\n\n  * Первый выпуск.\n",
        encoding="utf-8",
    )
    assert version.read_changelog_version(changelog) == "0.1.0~m1"


def test_missing_changelog_returns_none(tmp_path: Path) -> None:
    assert version.read_changelog_version(tmp_path / "нет-такого") is None


def test_garbage_changelog_returns_none(tmp_path: Path) -> None:
    changelog = tmp_path / "changelog"
    changelog.write_text("мусор без скобок\n", encoding="utf-8")
    assert version.read_changelog_version(changelog) is None


def test_version_is_non_empty_string() -> None:
    assert isinstance(version.__version__, str)
    assert version.__version__
