"""Single-threaded exec launcher: a session helper cannot outlive capture.

Invoked as an isolated script, never as a Python preexec_fn in the threaded
capture process. Only stdlib is loaded before installing the parent-death guard.
"""

from __future__ import annotations

import ctypes
import os
import signal
import sys


def main(arguments: list[str]) -> int:
    if len(arguments) < 2:
        return 3
    try:
        expected_parent = int(arguments[0])
        if expected_parent <= 0 or not os.path.isabs(arguments[1]):
            return 3
        libc = ctypes.CDLL(None, use_errno=True)
        prctl = libc.prctl
        prctl.argtypes = [ctypes.c_int, *([ctypes.c_ulong] * 4)]
        prctl.restype = ctypes.c_int
        if prctl(1, signal.SIGKILL, 0, 0, 0) != 0:  # PR_SET_PDEATHSIG
            return 3
        # Parent may have died before prctl. Never exec in that stale generation.
        if os.getppid() != expected_parent:
            return 3
        os.execv(arguments[1], arguments[1:])
    except (OSError, ValueError):
        return 3
    return 3


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
