"""Bounded owner of one child process; no GUI/X11 imports."""

from __future__ import annotations

import json
import os
import selectors
import subprocess
import time
from collections.abc import Callable, Sequence


def supervise(
    argv: Sequence[str],
    *,
    duration: float = 15.0,
    heartbeat_timeout: float = 0.6,
    kill_grace: float = 0.2,
    emit: Callable[[dict[str, object]], None] | None = None,
) -> int:
    """Terminate only our own child on missed heartbeat or absolute deadline."""
    if not 0 < duration <= 60 or not 0 < heartbeat_timeout <= 0.8 or not 0 < kill_grace <= 0.2:
        raise ValueError("invalid supervisor limits")
    report = emit or (lambda event: print(json.dumps(event), flush=True))
    child = subprocess.Popen(
        list(argv),
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        close_fds=True,
    )
    started = last_heartbeat = time.monotonic()
    selector = selectors.DefaultSelector()
    assert child.stdout is not None
    selector.register(child.stdout, selectors.EVENT_READ)
    buffered = b""
    report({"event": "supervisor_started", "child_pid": child.pid, "limit_s": duration})
    reason = "child_exit"
    try:
        while child.poll() is None:
            now = time.monotonic()
            if now - started >= duration:
                reason = "deadline"
                break
            if now - last_heartbeat >= heartbeat_timeout:
                reason = "heartbeat_timeout"
                break
            for key, _ in selector.select(0.025):
                chunk = os.read(key.fileobj.fileno(), 4096)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                buffered += chunk
                if len(buffered) > 16384:
                    reason = "invalid_output"
                    break
                while b"\n" in buffered:
                    line, buffered = buffered.split(b"\n", 1)
                    if line == b"HB":
                        last_heartbeat = time.monotonic()
                    else:
                        # Child emits only predefined stage codes/counters, never input.
                        try:
                            event = json.loads(line)
                        except (ValueError, UnicodeError):
                            reason = "invalid_output"
                            continue
                        if isinstance(event, dict):
                            report(event)
            if reason == "invalid_output":
                break
    except KeyboardInterrupt:
        reason = "operator_stop"
    finally:
        selector.close()
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=kill_grace)
            except subprocess.TimeoutExpired:
                report({"event": "kill_child"})
                child.kill()
        child.wait()
        child.stdout.close()
        report(
            {
                "event": "supervisor_closed",
                "reason": reason,
                "child_rc": child.returncode,
                "elapsed_ms": round((time.monotonic() - started) * 1000),
            }
        )
    return 0 if child.returncode == 0 and reason == "child_exit" else 2
