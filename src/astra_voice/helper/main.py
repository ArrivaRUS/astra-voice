"""Заглушка привилегированного помощника обновления. Реализация — milestone M8."""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    del argv
    sys.stderr.write("astra-voice helper: not implemented (M8)\n")
    return 2
