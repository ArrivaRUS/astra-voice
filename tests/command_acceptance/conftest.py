"""Expose the canonical helpers package during standalone acceptance runs."""

from __future__ import annotations

import sys
from pathlib import Path

TESTS = str(Path(__file__).resolve().parents[1])
if TESTS not in sys.path:
    sys.path.insert(0, TESTS)
