"""packaging/debian/control: зависимости пакета (ревью P3 — xdg-utils)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

CONTROL = Path(__file__).resolve().parents[2] / "packaging" / "debian" / "control"


def _field(name: str) -> set[str]:
    text = CONTROL.read_text(encoding="utf-8")
    match = re.search(rf"^{name}:(.*?)(?=^\S)", text, re.MULTILINE | re.DOTALL)
    assert match is not None, name
    lines = [line for line in match.group(1).splitlines() if not line.lstrip().startswith("#")]
    return {
        re.split(r"[\s(]", item.strip(), maxsplit=1)[0]
        for item in ",".join(lines).split(",")
        if item.strip()
    }


def test_xdg_utils_recommended_not_required() -> None:
    """xdg-open нужен ссылкам «Открыть…», но без него программа работает."""
    assert "xdg-utils" in _field("Recommends")
    assert "xdg-utils" not in _field("Depends")
