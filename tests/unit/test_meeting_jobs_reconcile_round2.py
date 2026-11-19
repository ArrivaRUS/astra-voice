"""R2: resumed 9000-reply snapshots stay off GUI; credits gate D5 readiness."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import pytest
from test_meeting_jobs import MID, Rig

from astra_voice.meetings.events import PhaseStatus
from astra_voice.meetings.model import (
    ReconcileProgress,
    ReconcileReplacement,
    Transcript,
    Utterance,
)
from astra_voice.meetings.store import Writer
from astra_voice.worker.meeting_transcribe import MeetingTranscribeState, _Job

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("filesystem_euid")]


def test_round2_9000_replies_slow_restore_does_not_block_gui_or_expire_ack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = Rig(tmp_path)
    rig.create()
    rig.jobs.on_recorded(MID)
    rig.jobs.tick()
    active = rig.jobs._active
    assert active is not None
    active.resume = True
    rows = [
        Utterance(f"u{i}", seq=i, start_ms=i * 1000, end_ms=i * 1000 + 500, text="слово " * 40)
        for i in range(9000)
    ]
    pending = ReconcileReplacement(11, "source", 10, rows[:2])
    transcript = Transcript(
        MID,
        meeting_generation=active.generation,
        transcript_revision=11,
        utterances=rows,
        reconcile=ReconcileProgress(
            status=PhaseStatus.RUNNING, done_ids=["source"], pending=pending
        ),
    )
    rig.store.write(MID, "transcript.json", transcript, writer=Writer.TRANSCRIBE)
    assert (rig.store.root / MID / "transcript.json").stat().st_size >= 5_000_000
    worker = MeetingTranscribeState(store=rig.store)
    job = _Job({"meeting_id": MID, "meeting_generation": active.generation})
    worker.job = job
    errors: list[BaseException] = []
    finished = threading.Event()
    entered, release = threading.Event(), threading.Event()
    gui_thread = threading.get_ident()
    read = rig.store.read

    def slow_read(mid: str, name: str) -> Any:
        if name == "transcript.json":
            assert threading.get_ident() != gui_thread, "transcript validation ran on GUI"
            entered.set()
            assert release.wait(30), "restore was not released"
        return read(mid, name)

    monkeypatch.setattr(rig.store, "read", slow_read)
    # The production submission path, not the rig's synchronous test executor.
    rig.jobs._submit = rig.jobs._writer.submit
    rig.loaded()
    role_send = rig.role.send

    def send(message: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        result = role_send(message, **kwargs)
        if message["type"] in {"meeting.job.credit", "meeting.job.applied"}:
            replies = worker.handle(message)
            assert all(reply["type"] != "error" for reply in replies), replies
        return result

    monkeypatch.setattr(rig.role, "send", send)

    def apply() -> None:
        try:
            worker._apply_replacement(job, pending, time.monotonic() + 60)
        except BaseException as exc:
            errors.append(exc)
        finally:
            finished.set()

    task = threading.Thread(target=apply)
    try:
        before = time.monotonic()
        rig.accepted()
        elapsed = time.monotonic() - before
        # Two 50ms GUI ticks: generous for scheduling an O(1) background operation.
        assert elapsed < 0.1, elapsed
        assert entered.wait(2)
        rig.accepted()  # accepted retry while the snapshot is still pending
        assert rig.jobs._pending == 1
        assert not [m for m in rig.role.sent if m["type"] == "meeting.job.credit"]
        task.start()
        # Slower than the former five-second credit/ACK deadline.
        slow_until = time.monotonic() + 5.2
        while time.monotonic() < slow_until:
            rig.jobs.tick()
            assert not worker.poll(128)
            assert not finished.is_set(), errors
            time.sleep(0.01)
        release.set()
        until = time.monotonic() + 40
        while not finished.is_set():
            assert time.monotonic() < until, "D5 did not finish after readiness"
            rig.jobs.tick()
            for event in worker.poll(128):
                rig.role.emit(event.pop("type"), **event)
            time.sleep(0.005)
        task.join(2)
        assert not errors
        assert len(active.utterances) == 9000
        rig.accepted()  # accepted retry after readiness must not reset/re-credit the job
        assert rig.jobs._pending == 0
        assert len([m for m in rig.role.sent if m["type"] == "meeting.job.credit"]) == 1
        assert job.credits == 64
        receipts = [m for m in rig.role.sent if m["type"] == "meeting.job.applied"]
        assert len(receipts) == 1 and receipts[0]["revision"] == 11
        frame = worker._replacement_message(job, pending)
        rig.role.emit(frame.pop("type"), **frame)
        assert job.credits == 64  # repeated delivery returns no second credit
        (tmp_path / "restore-timing.txt").write_text(
            f"utterances=9000 bytes={(rig.store.root / MID / 'transcript.json').stat().st_size} "
            f"accepted_s={elapsed:.6f} restore_and_delivery_s={time.monotonic() - before:.3f}\n"
        )
    finally:
        release.set()
        job.cancel()
        if task.ident is not None:
            task.join(3)
        worker.cleanup()
        rig.jobs.shutdown(timeout=10)


def test_round2_readiness_wait_is_separate_from_applied_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from astra_voice.worker import meeting_transcribe as mt

    monkeypatch.setattr(mt, "DONE_DELIVERY_S", 0.05)
    worker = MeetingTranscribeState()
    job = _Job({"meeting_id": MID, "meeting_generation": 1})
    worker.job = job
    pending = ReconcileReplacement(11, "source", 10, [Utterance("new", text="ответ")])
    errors: list[BaseException] = []
    finished = threading.Event()

    def apply() -> None:
        try:
            worker._apply_replacement(job, pending, time.monotonic() + 2)
        except BaseException as exc:
            errors.append(exc)
        finally:
            finished.set()

    task = threading.Thread(target=apply)
    task.start()
    try:
        assert not finished.wait(0.1), errors  # twice the applied timeout, still no readiness
        assert not worker.poll(128)
        job.add_credits(1)
        until = time.monotonic() + 1
        event = None
        while event is None:
            assert time.monotonic() < until
            events = worker.poll(128)
            if events:
                event = events[0]
            else:
                time.sleep(0.001)
        assert event["type"] == "meeting.job.replace"
        reply = worker.handle(
            {
                "type": "meeting.job.applied",
                "req": "applied-r2",
                "request_type": "meeting.job.applied",
                **job.identity,
                "seq": 11,
                "revision": 11,
            }
        )
        assert reply[0]["type"] == "meeting.ack"
        assert finished.wait(1) and not errors
        assert job.credits == 1
    finally:
        job.cancel()
        task.join(3)
        worker.cleanup()
