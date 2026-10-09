"""Real QtDBus client exercised only under tests/helpers/private_bus.py."""

from __future__ import annotations

import faulthandler
import json
import logging
import os
import resource
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from threading import Event
from typing import Any

from PyQt5.QtCore import QCoreApplication

from astra_voice.platform import cowork


class Records(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


def pump_until(predicate: Callable[[], bool], timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "private command peer timed out"
        QCoreApplication.processEvents()
        time.sleep(0.002)


def pump_for(seconds: float) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        QCoreApplication.processEvents()
        time.sleep(0.002)


def main() -> None:
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    faulthandler.enable()
    assert os.environ["QT_QPA_PLATFORM"] == "offscreen"
    assert "DISPLAY" not in os.environ
    scenario = sys.argv[1]
    trace_path = Path(sys.argv[2])
    app = QCoreApplication([])
    assert app is QCoreApplication.instance()
    release_worker = Event()
    entered_worker = Event()
    server: subprocess.Popen[str] | None = None
    client = cowork.CoworkClient()
    owners: list[bool] = []
    client.owner_changed.connect(owners.append)
    records = Records()
    root = logging.getLogger()
    root.addHandler(records)
    root.setLevel(logging.DEBUG)
    try:
        if scenario != "no_owner":
            server = subprocess.Popen(
                [
                    sys.executable,
                    str(Path(__file__).with_name("fake_server.py")),
                    str(trace_path),
                    "accepted"
                    if scenario
                    in ("long_uptime", "queued_deadline", "guard", "held_timeout", "held_sleep")
                    else scenario,
                ],
                text=True,
            )
            pump_until(
                lambda: trace_path.with_suffix(".ready").exists() or server.poll() is not None
            )
            assert server.poll() is None
        client.start()
        pump_until(lambda: bool(owners))
        assert owners[-1] == (scenario != "no_owner")
        if scenario == "long_uptime":
            # Same monotonic epoch on both threads; force explicit x beyond uint32.
            cowork.monotonic_ms = lambda: (1 << 33) + 123
        results: list[cowork.DeliveryResult] = []
        started = time.monotonic()
        if scenario == "queued_deadline":
            # Time spent queued in the worker is part of the 300 ms budget.
            old = cowork.monotonic_ms
            calls = 0

            def advancing_clock() -> int:
                nonlocal calls
                calls += 1
                return old() + (301 if calls > 1 else 0)

            cowork.monotonic_ms = advancing_clock
        elif scenario == "guard":
            client.set_admission_guard(lambda: False)
        if scenario in ("held_timeout", "held_sleep"):

            def hold_admission() -> bool:
                entered_worker.set()
                assert release_worker.wait(2), "test must release worker before teardown"
                return True

            client.set_admission_guard(hold_admission)
        client.submit("SECRET_SENTENCE_FOR_COMMAND_ACCEPTANCE", results.append)
        if scenario in ("held_timeout", "held_sleep"):
            pump_until(entered_worker.is_set)
            if scenario == "held_sleep":
                client.cancel()

        if scenario == "sleep":
            pump_until(lambda: trace_path.exists())
            client.cancel()
        pump_until(lambda: bool(results))
        elapsed = time.monotonic() - started
        release_worker.set()
        expected = {
            "accepted": ("delivered", "none"),
            "long_uptime": ("delivered", "none"),
            "busy": ("undelivered", "busy"),
            "bad_echo": ("unknown", "bad_reply"),
            "malformed": ("unknown", "bad_reply"),
            "Failed": ("unknown", "bad_reply"),
            "foreign_error": ("unknown", "bad_reply"),
            "late": ("unknown", "timeout"),
            "owner_loss": ("unknown", "timeout"),
            "sleep": ("unknown", "suspended"),
            "no_owner": ("undelivered", "not_running"),
            "queued_deadline": ("undelivered", "timeout"),
            "guard": ("undelivered", "locked"),
            "held_timeout": ("undelivered", "timeout"),
            "held_sleep": ("undelivered", "suspended"),
        }[scenario]
        assert (results[0].outcome, results[0].reason) == expected, results
        if scenario in ("late", "held_timeout"):
            assert 0.15 < elapsed < 0.5, elapsed
        pump_for(0.75 if scenario in ("late", "sleep") else 0.1)
        assert len(results) == 1, results
        events: list[dict[str, Any]] = (
            [json.loads(line) for line in trace_path.read_text().splitlines()]
            if trace_path.exists()
            else []
        )
        no_submit = scenario in (
            "no_owner",
            "queued_deadline",
            "guard",
            "held_timeout",
            "held_sleep",
        )
        assert len(events) == (0 if no_submit else 1), events
        assert all(event["method"] == "Submit" for event in events), events
        for event in events:
            assert event["deadline_mono_ms"] - event["sent_mono_ms"] == 300
            assert event["source"] == "astra-voice" and event["contract"] == 1
            assert len(event["request_id"]) == 32
            assert event["sent_boot_ms"] >= 0
            if scenario == "long_uptime":
                assert event["sent_mono_ms"] == (1 << 33) + 123
        assert "SECRET_SENTENCE_FOR_COMMAND_ACCEPTANCE" not in repr(records.messages)
        assert "PRIVATE_REMOTE_SENTENCE" not in repr(records.messages)
        print("COMMAND_ACCEPTANCE_OK", scenario, flush=True)
    finally:
        release_worker.set()
        client.close()
        if server is not None:
            server.terminate()
            try:
                server.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                server.kill()
                server.communicate()
        root.removeHandler(records)


if __name__ == "__main__":
    main()
