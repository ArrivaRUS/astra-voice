"""Заглушка воркера распознавания. Настоящая реализация — milestone M2."""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    del argv
    sys.stderr.write("astra-voice worker: not implemented (M2)\n")
    return 2
