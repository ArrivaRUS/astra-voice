"""Версия приложения.

Источник правды при установке — сгенерированный сборкой ``astra_voice/_version.py``
(в git не хранится). В репозитории его нет, поэтому берётся первая строка
``packaging/debian/changelog``; если и её нет — ``0.0.0+dev``.
"""

from __future__ import annotations

import re
from pathlib import Path

FALLBACK_VERSION = "0.0.0+dev"

# src/astra_voice/core/version.py → core → astra_voice → src → корень репозитория
_REPO_ROOT = Path(__file__).resolve().parents[3]
_CHANGELOG = _REPO_ROOT / "packaging" / "debian" / "changelog"

_CHANGELOG_RE = re.compile(r"^\S+\s+\(([^)]+)\)")


def read_changelog_version(changelog: Path | None = None) -> str | None:
    """Версия из первой строки ``debian/changelog`` или ``None``."""
    path = _CHANGELOG if changelog is None else changelog
    try:
        with path.open(encoding="utf-8") as handle:
            first = handle.readline()
    except OSError:
        return None
    match = _CHANGELOG_RE.match(first.strip())
    return match.group(1) if match else None


def _detect() -> str:
    try:
        from astra_voice._version import __version__ as generated
    except Exception:  # noqa: BLE001 — файла нет в дереве разработки
        pass
    else:
        if generated:
            return str(generated)
    return read_changelog_version() or FALLBACK_VERSION


__version__ = _detect()
