"""Hard deadline for Linux FIFO attack fixtures; no sleeps or worker leaks."""

from __future__ import annotations

import signal
from collections.abc import Iterator
from contextlib import contextmanager
from types import FrameType

import pytest


@contextmanager
def fifo_deadline() -> Iterator[None]:
    def expired(signum: int, frame: FrameType | None) -> None:
        pytest.fail("themerc read exceeded the 2 second FIFO watchdog")

    previous_handler = signal.signal(signal.SIGALRM, expired)
    previous_timer = signal.setitimer(signal.ITIMER_REAL, 2.0)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        signal.setitimer(signal.ITIMER_REAL, *previous_timer)
