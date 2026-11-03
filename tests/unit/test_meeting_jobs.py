"""E13.M4 GUI transcription queue: admission, FIFO, credits, cancel/delete, crash, resume."""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from astra_voice.core import meeting_jobs
from astra_voice.core.meeting_jobs import MeetingJobs, ModelAdmission
from astra_voice.meetings.events import MeetingState, ProcessingStatus
from astra_voice.meetings.model import Meeting
from astra_voice.meetings.store import MeetingStore
from astra_voice.worker import ipc

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("filesystem_euid")]
MID = "2026-11-01_120000-aaaa"
MID2 = "2026-11-01_130000-bbbb"
LOAD = {
    "id": "gigaam-v3-e2e-rnnt-int8",
    "revision": "322c3b29492673eb7d0b434bfa9dfb8653e34d02",
    "dir": "/models/gigaam",
    "layout": "onnx-asr-gigaam-v3",
    "variant": "gigaam-v3-e2e-rnnt",
    "threads": 2,
    "min_ram_mb": 419,
}


class Role:
    """Fake WorkerSupervisor(role="meeting-transcribe"): contract-checked commands."""

    def __init__(
        self,
        rig: Rig,
        on_event: Callable[[dict[str, Any]], None],
        on_exit: Callable[[int], None],
    ) -> None:
        self.rig, self.on_event, self.on_exit = rig, on_event, on_exit
        self.generation = 1
        self.state = "new"
        self.ready = False
        self.exited = False
        self.sent: list[dict[str, Any]] = []
        self.kills = self.stops = 0
        self.generations: dict[str, int] = {}
        # Synchronous answers inside send() (supervisor limit/req-conflict, ledger cache).
        self.on_send: dict[str, Callable[[dict[str, Any]], None]] = {}

    def start(self) -> None:
        self.state, self.ready = "running", True

    def send(self, message: dict[str, Any], *, timeout: float | None = None) -> dict[str, Any]:
        ipc.decode(ipc.encode(message, role="meeting-transcribe")[4:], role="meeting-transcribe")
        if not self.ready:
            raise RuntimeError("meeting-role-not-ready")
        if message["type"] == "meeting.job.cancel":
            self.rig.cancelled_at_send.append(self.rig.meeting(message["meeting_id"]).cancelled)
        self.sent.append(message)
        hook = self.on_send.pop(message["type"], None)
        if hook is not None:
            hook(message)
        return message

    def set_meeting_generation(self, meeting_id: str, generation: int) -> None:
        if generation < self.generations.get(meeting_id, 0):
            raise ValueError("meeting-generation-stale")
        self.generations[meeting_id] = generation

    def kill(self) -> None:
        self.kills += 1
        self.state, self.ready = "stopped", False

    def stop(self) -> None:
        self.stops += 1
        self.state, self.ready, self.exited = "stopped", False, True

    def emit(self, kind: str, **fields: Any) -> None:
        self.on_event({"type": kind, **fields, "generation": self.generation})

    def crash(self) -> None:
        """Supervisor auto-restart: confirmed exit, then a new process generation."""
        self.on_exit(-9)
        self.generation += 1

    def types(self) -> list[str]:
        return [message["type"] for message in self.sent]

    def last(self, kind: str) -> dict[str, Any]:
        return next(m for m in reversed(self.sent) if m["type"] == kind)


class Rig:
    def __init__(self, root: Path) -> None:
        self.now = 1000.0
        self.store = MeetingStore(root / "meetings")
        self.immediately = True
        self.retention = 30
        self.admission = ModelAdmission("ok", dict(LOAD))
        self.memory: int | None = 4000
        self.notices: list[tuple[str, str]] = []
        self.roles: list[Role] = []
        self.cancelled_at_send: list[bool] = []
        self.jobs = self.make()

    def make(self) -> MeetingJobs:
        return MeetingJobs(
            self.store,
            self.factory,
            transcribe_immediately=lambda: self.immediately,
            audio_retention_days=lambda: self.retention,
            model_probe=lambda: self.admission,
            mem_available=lambda: self.memory,
            clock=lambda: self.now,
            submit=lambda operation: operation(),
            notify=lambda kind, meeting: self.notices.append((kind, meeting.id)),
            echo_gate=False,
        )

    def factory(
        self, on_event: Callable[[dict[str, Any]], None], on_exit: Callable[[int], None]
    ) -> Role:
        role = Role(self, on_event, on_exit)
        self.roles.append(role)
        return role

    @property
    def role(self) -> Role:
        return self.roles[-1]

    def create(
        self, mid: str = MID, state: MeetingState = MeetingState.RECORDED, **fields: Any
    ) -> None:
        hour = mid[11:13]
        self.store.create(
            Meeting(
                id=mid,
                started_at=f"2026-11-01T{hour}:00:00+00:00",
                ended_at=f"2026-11-01T{hour}:30:00+00:00",
                state=state,
                recorded_ms=1_800_000,
                **fields,
            )
        )
        audio = self.store.root / mid / "audio"
        descriptor = os.open(audio / "mic-0000.wav", os.O_CREAT | os.O_WRONLY, 0o600)
        os.write(descriptor, b"RIFF")
        os.close(descriptor)

    def meeting(self, mid: str = MID) -> Meeting:
        value = self.store.read(mid, "meeting.json")
        assert isinstance(value, Meeting)
        return value

    def audio_present(self, mid: str = MID) -> bool:
        return (self.store.root / mid / "audio" / "mic-0000.wav").exists()

    def loaded(self) -> None:
        load = self.role.last("model.load")
        self.role.emit("model.loaded", req=load["req"], request_type="model.load", **LOAD_REPLY)

    def accepted(self, mid: str = MID) -> dict[str, Any]:
        start = self.role.last("meeting.job.start")
        self.role.emit(
            "meeting.job.accepted",
            req=start["req"],
            request_type="meeting.job.start",
            meeting_id=mid,
            meeting_generation=start["meeting_generation"],
            resume_from={"mic_ms": 0, "far_ms": 0},
        )
        return start

    def running(self, mid: str = MID) -> dict[str, Any]:
        """Queue -> admission -> model.load -> job.start -> accepted."""
        self.jobs.tick()
        if "model.load" in self.role.types() and "meeting.job.start" not in self.role.types():
            self.loaded()
        return self.accepted(mid)

    def job_event(
        self, kind: str, mid: str = MID, generation: int | None = None, **fields: Any
    ) -> None:
        start = self.role.last("meeting.job.start")
        self.role.emit(
            kind,
            meeting_id=mid,
            meeting_generation=start["meeting_generation"] if generation is None else generation,
            **fields,
        )

    def utterance(self, seq: int, text: str = "текст") -> None:
        self.job_event(
            "meeting.job.utterance",
            seq=seq,
            id=f"u{seq:05d}",
            track="mic",
            start_ms=seq * 1000,
            end_ms=seq * 1000 + 500,
            text=text,
            speaker="me",
            provisional=True,
        )

    def done(self, mid: str = MID, generation: int | None = None) -> None:
        self.job_event(
            "meeting.job.done",
            mid,
            generation,
            utterances=3,
            speakers=1,
            diar_status="skipped",
            wall_ms=1234,
            peak_rss_kb=456789,
            echo_dropped=0,
            transcript_revision=2,
        )


LOAD_REPLY = {
    "id": LOAD["id"],
    "revision": LOAD["revision"],
    "variant": LOAD["variant"],
    "load_ms": 12.0,
    "engine_version": "onnx-asr",
}


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    return Rig(tmp_path)


def test_stop_with_immediately_queues_a_new_generation_and_starts(rig: Rig) -> None:
    rig.create()
    assert rig.jobs.on_recorded(MID)
    queued = rig.meeting()
    assert queued.state == MeetingState.TRANSCRIBING and queued.meeting_generation == 1
    rig.jobs.tick()
    assert rig.role.types() == ["model.load"]
    assert {k: v for k, v in rig.role.sent[0].items() if k in LOAD} == LOAD
    rig.loaded()
    start = rig.role.last("meeting.job.start")
    assert rig.role.types() == ["model.load", "meeting.job.start", "meeting.job.credit"]
    assert start["meeting_id"] == MID and start["meeting_generation"] == 1
    assert start["dir"] == str(rig.store.root / MID)
    assert start["tracks"] == ["mic"] and start["variant"] == "mic_only"
    assert start["diarization"] is False and start["self_track"] == "mic"
    assert start["resume"] is False and start["echo_gate"] is False
    assert start["limits"] == {"max_ms": 14_400_000, "max_utterances": 28_800, "max_blocks": 12}
    assert rig.role.last("meeting.job.credit")["n"] == 64
    assert rig.meeting().processing_status == ProcessingStatus.RUNNING


def test_immediately_off_keeps_recorded_until_transcribe_click(rig: Rig) -> None:
    rig.create()
    rig.immediately = False
    assert not rig.jobs.on_recorded(MID)
    rig.jobs.tick()
    assert rig.meeting().state == MeetingState.RECORDED and rig.roles == []
    rig.jobs.transcribe(MID)
    assert rig.meeting().state == MeetingState.TRANSCRIBING
    rig.jobs.tick()
    assert rig.role.types() == ["model.load"]


def test_fifo_one_active_job_and_one_model_load_per_process(rig: Rig) -> None:
    rig.create(MID)
    rig.create(MID2)
    rig.jobs.on_recorded(MID)
    rig.jobs.on_recorded(MID2)
    rig.running(MID)
    rig.jobs.tick()
    assert rig.role.types().count("meeting.job.start") == 1
    assert rig.jobs.active_id == MID and rig.jobs.queued_ids == [MID2]
    rig.done(MID)
    rig.jobs.tick()
    starts = [m for m in rig.role.sent if m["type"] == "meeting.job.start"]
    assert [m["meeting_id"] for m in starts] == [MID, MID2]
    assert rig.role.types().count("model.load") == 1
    assert len(rig.roles) == 1


def test_restart_resumes_unfinished_but_not_cancelled_or_deleting_t488(rig: Rig) -> None:
    rig.create(MID, MeetingState.TRANSCRIBING, meeting_generation=3)
    rig.create(MID2, MeetingState.RECORDED, cancelled=True, meeting_generation=2)
    rig.create("2026-11-01_140000-cccc", MeetingState.DELETING)
    rig.jobs.recover()
    assert rig.jobs.queued_ids == [MID]
    rig.jobs.tick()
    rig.loaded()
    start = rig.role.last("meeting.job.start")
    assert start["resume"] is True and start["meeting_generation"] == 3
    assert rig.meeting(MID2).state == MeetingState.RECORDED and rig.meeting(MID2).cancelled


def test_recorded_with_checkpoint_is_resumed_without_new_generation(rig: Rig) -> None:
    rig.create(MID, MeetingState.RECORDED, meeting_generation=2)
    path = rig.store.root / MID / "checkpoint.json"
    descriptor = os.open(path, os.O_CREAT | os.O_WRONLY, 0o600)
    os.close(descriptor)
    rig.jobs.recover()
    rig.jobs.tick()
    rig.loaded()
    start = rig.role.last("meeting.job.start")
    assert start["resume"] is True and start["meeting_generation"] == 2


def test_needs_model_then_install_starts_by_itself_t481(rig: Rig) -> None:
    rig.create()
    rig.admission = ModelAdmission("missing")
    rig.jobs.on_recorded(MID)
    rig.jobs.tick()
    assert rig.meeting().state == MeetingState.NEEDS_MODEL and rig.roles == []
    assert rig.audio_present()
    assert rig.notices == [("needs_model", MID)]
    rig.jobs.tick()
    assert rig.roles == []
    rig.admission = ModelAdmission("ok", dict(LOAD))
    rig.jobs.models_changed()
    rig.jobs.tick()
    assert rig.meeting().state == MeetingState.TRANSCRIBING
    assert rig.role.types() == ["model.load"]
    assert rig.notices == [("needs_model", MID)]


def test_needs_model_rechecks_periodically(rig: Rig) -> None:
    rig.create()
    rig.admission = ModelAdmission("missing")
    rig.jobs.on_recorded(MID)
    rig.jobs.tick()
    rig.admission = ModelAdmission("ok", dict(LOAD))
    rig.now += 59
    rig.jobs.tick()
    assert rig.roles == []
    rig.now += 1
    rig.jobs.tick()
    assert rig.role.types() == ["model.load"]


def test_selfcheck_failed_or_revoked_fails_before_inference_even_on_resume_t482(
    rig: Rig,
) -> None:
    rig.create(MID, MeetingState.TRANSCRIBING, meeting_generation=1)
    descriptor = os.open(rig.store.root / MID / "checkpoint.json", os.O_CREAT | os.O_WRONLY, 0o600)
    os.close(descriptor)
    rig.admission = ModelAdmission("broken")
    rig.jobs.recover()
    rig.jobs.tick()
    meeting = rig.meeting()
    assert meeting.state == MeetingState.FAILED
    assert meeting.processing_status == ProcessingStatus.FAILED
    assert rig.roles == [] and rig.audio_present()
    assert rig.jobs.row(MID)["failure"] == "Распознавание на этом компьютере не работает"
    assert rig.notices == [("failed_model", MID)]


def test_role_model_error_fails_without_job_start_t482(rig: Rig) -> None:
    rig.create()
    rig.jobs.on_recorded(MID)
    rig.jobs.tick()
    load = rig.role.last("model.load")
    rig.role.emit("error", req=load["req"], request_type="model.load", code="model", message="x")
    assert "meeting.job.start" not in rig.role.types()
    assert rig.meeting().state == MeetingState.FAILED and rig.audio_present()


def test_low_memory_waits_and_retries_every_60_s_t483(rig: Rig) -> None:
    rig.create()
    rig.memory = 419 + 199
    rig.jobs.on_recorded(MID)
    rig.jobs.tick()
    assert rig.meeting().state == MeetingState.WAITING_MEMORY and rig.roles == []
    assert rig.notices == [("waiting_memory", MID)]
    rig.memory = 419 + 200
    rig.now += 59.9
    rig.jobs.tick()
    assert rig.roles == []
    rig.now += 0.1
    rig.jobs.tick()
    assert rig.meeting().state == MeetingState.TRANSCRIBING
    assert rig.role.types() == ["model.load"]
    assert rig.notices == [("waiting_memory", MID)]


@pytest.mark.parametrize("kind", ["waiting", "failed"])
def test_role_memory_ends_job_unloads_and_resumes_after_60_s_t483(rig: Rig, kind: str) -> None:
    rig.create()
    rig.jobs.on_recorded(MID)
    rig.running()
    if kind == "waiting":
        rig.job_event("meeting.job.waiting", reason="memory", need_mb=619, available_mb=100)
    else:
        rig.job_event("meeting.job.failed", code="memory")
    assert rig.meeting().state == MeetingState.WAITING_MEMORY and rig.audio_present()
    assert rig.role.types()[-1] == "model.unload"
    rig.now += 60
    rig.jobs.tick()
    assert rig.role.types()[-1] == "model.load"
    rig.loaded()
    assert rig.role.last("meeting.job.start")["resume"] is True


def test_credits_64_then_32_per_32_utterances_and_cancel_needs_none(rig: Rig) -> None:
    rig.create()
    rig.jobs.on_recorded(MID)
    rig.running()

    def credits() -> list[int]:
        return [m["n"] for m in rig.role.sent if m["type"] == "meeting.job.credit"]

    assert credits() == [64]
    for seq in range(31):
        rig.utterance(seq)
    assert credits() == [64]
    rig.utterance(31)
    assert credits() == [64, 32]
    for seq in range(32, 64):
        rig.utterance(seq)
    assert credits() == [64, 32, 32]
    # Exhausted credits never block control: cancel goes out at once.
    rig.jobs.cancel(MID)
    assert rig.role.types()[-1] == "meeting.job.cancel"


def test_progress_and_utterances_snapshot_hide_empty_text(rig: Rig) -> None:
    rig.create()
    rig.jobs.on_recorded(MID)
    rig.running()
    rig.job_event("meeting.job.progress", stage="asr", done_ms=720_000, total_ms=1_800_000)
    assert rig.jobs.percent == 40 and rig.jobs.row(MID)["percent"] == 40
    rig.utterance(0, "Привет")
    rig.utterance(1, "")
    rig.utterance(2, meeting_jobs.DAMAGED_TEXT)
    rig.utterance(0, "Привет")  # resumed role repeats a non-durable seq: no duplicate
    snapshot = rig.jobs.job_snapshot(MID)
    assert snapshot is not None and snapshot["percent"] == 40
    assert [(u["seq"], u["text"], u["mark"]) for u in snapshot["utterances"]] == [
        (0, "Привет", False),
        (2, meeting_jobs.DAMAGED_TEXT, True),
    ]
    assert rig.jobs.job_snapshot(MID2) is None


def test_mark_texts_match_the_role() -> None:
    source = (
        Path(__file__).resolve().parents[2] / "src/astra_voice/worker/meeting_transcribe.py"
    ).read_text()
    assert f'UNRECOGNIZED_TEXT = "{meeting_jobs.UNRECOGNIZED_TEXT}"' in source
    assert f'DAMAGED_TEXT = "{meeting_jobs.DAMAGED_TEXT}"' in source


def test_cancel_is_durable_before_job_cancel_and_acknowledged_t408(rig: Rig) -> None:
    rig.create()
    rig.jobs.on_recorded(MID)
    rig.running()
    rig.jobs.cancel(MID)
    assert rig.cancelled_at_send == [True]
    meeting = rig.meeting()
    assert meeting.cancelled and meeting.state == MeetingState.RECORDED
    assert meeting.processing_status == ProcessingStatus.CANCELLED
    cancel = rig.role.last("meeting.job.cancel")
    assert cancel["meeting_generation"] == 1
    rig.role.emit(
        "meeting.job.cancelled",
        req=cancel["req"],
        request_type="meeting.job.cancel",
        meeting_id=MID,
        meeting_generation=1,
    )
    assert rig.jobs.active_id == "" and rig.role.kills == 0
    assert rig.role.generations[MID] == 2  # late events of generation 1 are retired
    assert rig.audio_present()
    assert rig.jobs.row(MID)["cancelledText"] == (
        "Расшифровка отменена. Запись сохранена — можно начать снова."
    )
    # Late results of the cancelled job never publish.
    rig.done(MID, generation=1)
    assert rig.meeting().state == MeetingState.RECORDED


def test_cancel_without_ack_in_1_s_kills_and_waits_for_exit_t408(rig: Rig) -> None:
    rig.create()
    rig.create(MID2)
    rig.jobs.on_recorded(MID)
    rig.jobs.on_recorded(MID2)
    rig.running()
    rig.jobs.cancel(MID)
    rig.now += 0.99
    rig.jobs.tick()
    assert rig.role.kills == 0
    rig.now += 0.01
    rig.jobs.tick()
    assert rig.role.kills == 1 and rig.jobs.active_id == MID
    rig.jobs.tick()
    assert rig.jobs.active_id == MID  # SIGKILL is not an exit confirmation
    first = rig.role
    first.exited = True
    rig.jobs.tick()
    assert rig.jobs.active_id == MID2 and len(rig.roles) == 2
    assert first.types().count("meeting.job.start") == 1
    assert rig.meeting(MID).cancelled and rig.audio_present(MID)


def test_cancelled_meeting_is_not_resumed_after_restart_and_transcribe_clears(
    rig: Rig, tmp_path: Path
) -> None:
    rig.create()
    rig.jobs.on_recorded(MID)
    rig.running()
    rig.jobs.cancel(MID)
    rig.jobs.shutdown()
    again = Rig(tmp_path)
    again.jobs.recover()
    again.jobs.tick()
    assert again.jobs.queued_ids == [] and again.roles == []
    again.jobs.transcribe(MID)
    meeting = again.meeting()
    assert meeting.state == MeetingState.TRANSCRIBING and not meeting.cancelled
    assert meeting.meeting_generation == 2


def test_cancel_of_queued_or_waiting_job_never_starts_it(rig: Rig) -> None:
    rig.create()
    rig.memory = 10
    rig.jobs.on_recorded(MID)
    rig.jobs.tick()
    rig.jobs.cancel(MID)
    rig.memory = 4000
    rig.now += 120
    rig.jobs.tick()
    assert rig.roles == [] and rig.meeting().state == MeetingState.RECORDED
    assert rig.meeting().cancelled


def test_crash_resumes_three_times_then_fails_with_audio_kept_t484(rig: Rig) -> None:
    rig.create()
    rig.jobs.on_recorded(MID)
    rig.running()
    for attempt in range(3):
        rig.role.crash()
        rig.jobs.tick()
        rig.loaded()
        start = rig.role.last("meeting.job.start")
        assert start["resume"] is True and start["meeting_generation"] == 1
        assert rig.role.types().count("meeting.job.start") == attempt + 2
        rig.now += 10
    rig.role.crash()
    rig.jobs.tick()
    assert rig.meeting().state == MeetingState.FAILED and rig.audio_present()
    assert rig.jobs.active_id == ""
    assert rig.notices == [("failed", MID)]


def test_crashes_outside_the_window_are_forgotten_t484(rig: Rig) -> None:
    rig.create()
    rig.jobs.on_recorded(MID)
    rig.running()
    for _ in range(5):
        rig.role.crash()
        rig.jobs.tick()
        rig.loaded()
        rig.now += 301
    assert rig.meeting().state == MeetingState.TRANSCRIBING


def test_restart_limit_from_supervisor_fails_the_job(rig: Rig) -> None:
    rig.create()
    rig.jobs.on_recorded(MID)
    rig.running()
    rig.role.emit("error", code="restart-limit", message="x")
    assert rig.meeting().state == MeetingState.FAILED


def test_done_stale_generation_is_dropped(rig: Rig) -> None:
    rig.create()
    rig.jobs.on_recorded(MID)
    rig.running()
    rig.done(generation=0)
    assert rig.meeting().state == MeetingState.TRANSCRIBING and rig.jobs.active_id == MID


def test_done_records_stats_and_keeps_audio_with_retention(rig: Rig) -> None:
    rig.create()
    rig.jobs.on_recorded(MID)
    rig.running()
    rig.done()
    meeting = rig.meeting()
    assert meeting.state == MeetingState.DONE
    assert meeting.processing_status == ProcessingStatus.SUCCEEDED
    assert meeting.transcription.finished_at is not None
    assert (meeting.transcription.wall_ms, meeting.transcription.peak_rss_kb) == (1234, 456789)
    assert meeting.transcription.model_id == LOAD["id"]
    assert meeting.audio.present and meeting.audio.retain_days == 30
    assert not meeting.cleanup_pending and rig.audio_present()
    assert rig.notices == [("done", MID)]
    assert rig.role.types()[-1] == "model.unload"  # queue drained: memory back


def test_done_with_retention_zero_removes_audio_d6(rig: Rig) -> None:
    rig.create()
    rig.retention = 0
    rig.jobs.on_recorded(MID)
    rig.running()
    rig.done()
    meeting = rig.meeting()
    assert meeting.state == MeetingState.DONE and not meeting.cleanup_pending
    assert not meeting.audio.present and meeting.audio.deleted_at is not None
    assert not (rig.store.root / MID / "audio").exists()


def test_cleanup_pending_is_completed_after_restart_without_reprocessing_d6(rig: Rig) -> None:
    rig.create(MID, MeetingState.DONE, cleanup_pending=True)
    rig.jobs.recover()
    rig.jobs.tick()
    meeting = rig.meeting()
    assert meeting.state == MeetingState.DONE and not meeting.cleanup_pending
    assert not meeting.audio.present and not (rig.store.root / MID / "audio").exists()
    assert rig.roles == []


def test_delete_waits_for_confirmed_exit_and_late_result_is_dropped_t441(rig: Rig) -> None:
    rig.create()
    rig.jobs.on_recorded(MID)
    rig.running()
    released: list[str] = []
    rig.jobs.delete(MID, lambda: released.append(MID))
    assert rig.store.list() == []  # tombstone: DELETING is hidden
    cancel = rig.role.last("meeting.job.cancel")
    rig.role.emit(
        "meeting.job.cancelled",
        req=cancel["req"],
        request_type="meeting.job.cancel",
        meeting_id=MID,
        meeting_generation=1,
    )
    assert rig.role.kills == 1 and released == []
    rig.done()
    rig.jobs.tick()
    assert released == []
    rig.role.exited = True
    rig.jobs.tick()
    assert released == [MID]
    raw = (rig.store.root / MID / "meeting.json").read_text()
    assert '"state": "deleting"' in raw
    rig.jobs.transcribe(MID)
    assert rig.jobs.queued_ids == []


def test_delete_of_idle_meeting_releases_after_tombstone(rig: Rig) -> None:
    rig.create()
    released: list[str] = []
    rig.jobs.delete(MID, lambda: released.append(MID))
    assert released == [MID] and rig.roles == []


def test_exit_while_transcribing_stops_role_without_cancel_and_resumes_t478(
    rig: Rig, tmp_path: Path
) -> None:
    rig.create()
    rig.jobs.on_recorded(MID)
    rig.running()
    assert rig.jobs.busy
    assert rig.jobs.shutdown(timeout=1.0)
    assert rig.role.stops == 1 and "meeting.job.cancel" not in rig.role.types()
    assert rig.meeting().state == MeetingState.TRANSCRIBING and not rig.meeting().cancelled
    again = Rig(tmp_path)
    again.jobs.recover()
    again.jobs.tick()
    again.loaded()
    assert again.role.last("meeting.job.start")["resume"] is True


def test_other_meeting_events_never_publish(rig: Rig) -> None:
    rig.create()
    rig.jobs.on_recorded(MID)
    rig.running()
    rig.job_event("meeting.job.progress", MID2, stage="asr", done_ms=5, total_ms=10)
    assert rig.jobs.percent == 0


def test_probe_prefers_trusted_gigaam_v3_and_reports_broken() -> None:
    from types import SimpleNamespace

    def record(model_id: str, **fields: Any) -> SimpleNamespace:
        values = {
            "id": model_id,
            "revision": "r1",
            "dir": Path("/m") / model_id,
            "layout": "onnx-asr-gigaam-v3",
            "variant": "v",
            "state": "ok",
            "recheck": False,
            "metadata_ok": True,
        }
        values.update(fields)
        return SimpleNamespace(**values)

    def probe(*records: SimpleNamespace, revoked: bool = False) -> ModelAdmission:
        store = SimpleNamespace(records=lambda: records)
        return meeting_jobs.probe_gigaam_model(
            store=store,
            min_ram_mb=lambda model_id: 419,
            revoked=lambda model_id, revision: revoked,
        )

    assert probe().status == "missing"
    assert probe(record("whisper-small-int8", layout="onnx-community-whisper")).status == (
        "missing"
    )
    chosen = probe(record("gigaam-v3-ctc-int8"), record("gigaam-v3-e2e-rnnt-int8"))
    assert chosen.status == "ok" and chosen.load is not None
    assert chosen.load["id"] == "gigaam-v3-e2e-rnnt-int8" and chosen.min_ram_mb == 419
    assert probe(record("gigaam-v3-e2e-rnnt-int8", state="broken")).status == "broken"
    assert probe(record("gigaam-v3-e2e-rnnt-int8", recheck=True)).status == "broken"
    assert probe(record("gigaam-v3-e2e-rnnt-int8"), revoked=True).status == "broken"


# ---- review of step 2: answers outside the expected codes (I1) ----------------------


def _load_error(rig: Rig, code: str) -> None:
    load = rig.role.last("model.load")
    rig.role.emit("error", req=load["req"], request_type="model.load", code=code, message="x")


@pytest.mark.parametrize("code", ["timeout", "limit", "req-conflict"])
def test_unexpected_model_load_error_retries_then_fails_and_queue_moves_i1(
    rig: Rig, code: str
) -> None:
    rig.create(MID)
    rig.create(MID2)
    rig.jobs.on_recorded(MID)
    rig.jobs.on_recorded(MID2)
    rig.jobs.tick()
    for attempt in range(3):
        _load_error(rig, code)
        rig.jobs.tick()
        assert rig.role.types().count("model.load") == attempt + 1  # 1 s back-off
        rig.now += 1
        rig.jobs.tick()
        assert rig.role.types().count("model.load") == attempt + 2
        assert rig.jobs.active_id == MID
    _load_error(rig, code)  # the fourth unexpected answer in the window fails the job
    assert rig.meeting(MID).state == MeetingState.FAILED and rig.audio_present(MID)
    assert rig.notices == [("failed", MID)]
    rig.jobs.tick()
    assert rig.jobs.active_id == MID2
    assert rig.role.types().count("model.load") == 5


def test_not_prepared_waits_for_the_previous_load_without_failing_i1(rig: Rig) -> None:
    rig.create()
    rig.jobs.on_recorded(MID)
    rig.jobs.tick()
    for _ in range(60):  # the role finishes a cancelled load/unload (<= 60 s)
        _load_error(rig, "not-prepared")
        rig.now += 1
        rig.jobs.tick()
    assert rig.meeting().state == MeetingState.TRANSCRIBING
    rig.loaded()
    assert rig.role.last("meeting.job.start")["meeting_id"] == MID
    assert rig.jobs.active_id == MID


def test_not_prepared_forever_ends_in_failure_not_a_stuck_queue_i1(rig: Rig) -> None:
    rig.create()
    rig.jobs.on_recorded(MID)
    rig.jobs.tick()
    for _ in range(200):
        if rig.jobs.active_id != MID:
            break
        _load_error(rig, "not-prepared")
        rig.now += 1
        rig.jobs.tick()
    assert rig.jobs.active_id == "" and rig.meeting().state == MeetingState.FAILED


@pytest.mark.parametrize("code", ["timeout", "limit", "model"])
def test_start_error_kills_the_role_and_fails_only_after_confirmed_exit_i1(
    rig: Rig, code: str
) -> None:
    rig.create(MID)
    rig.create(MID2)
    rig.jobs.on_recorded(MID)
    rig.jobs.on_recorded(MID2)
    rig.jobs.tick()
    rig.loaded()
    start = rig.role.last("meeting.job.start")
    rig.role.emit(
        "error", req=start["req"], request_type="meeting.job.start", code=code, message="x"
    )
    first = rig.role
    assert first.kills == 1
    assert rig.meeting(MID).state == MeetingState.TRANSCRIBING  # not before the exit
    assert rig.jobs.job_snapshot(MID) is None
    rig.jobs.tick()
    assert rig.jobs.active_id == MID and len(rig.roles) == 1
    first.exited = True
    rig.jobs.tick()
    assert rig.meeting(MID).state == MeetingState.FAILED and rig.audio_present(MID)
    assert rig.notices == [("failed_model" if code == "model" else "failed", MID)]
    assert rig.jobs.active_id == MID2 and len(rig.roles) == 2
    assert rig.role.types() == ["model.load"]  # a fresh process, no `limit` from a ghost job


def test_cancel_while_a_failed_start_is_killed_keeps_cancelled_not_failed_i1(rig: Rig) -> None:
    rig.create()
    rig.jobs.on_recorded(MID)
    rig.jobs.tick()
    rig.loaded()
    start = rig.role.last("meeting.job.start")
    rig.role.emit(
        "error", req=start["req"], request_type="meeting.job.start", code="timeout", message="x"
    )
    rig.jobs.cancel(MID)
    rig.role.exited = True
    rig.jobs.tick()
    meeting = rig.meeting()
    assert meeting.cancelled and meeting.state == MeetingState.RECORDED
    assert rig.notices == [] and rig.jobs.active_id == ""


def test_delete_while_a_failed_start_is_killed_releases_after_exit_i1(rig: Rig) -> None:
    rig.create()
    rig.jobs.on_recorded(MID)
    rig.jobs.tick()
    rig.loaded()
    start = rig.role.last("meeting.job.start")
    rig.role.emit(
        "error", req=start["req"], request_type="meeting.job.start", code="timeout", message="x"
    )
    released: list[str] = []
    rig.jobs.delete(MID, lambda: released.append(MID))
    assert released == [] and "meeting.job.cancel" not in rig.role.types()
    rig.role.exited = True
    rig.jobs.tick()
    assert released == [MID] and rig.notices == []


def _sync_error(rig: Rig, kind: str, code: str) -> None:
    def answer(message: dict[str, Any]) -> None:
        rig.role.emit("error", req=message["req"], request_type=kind, code=code, message="x")

    rig.role.on_send[kind] = answer


def test_synchronous_error_inside_model_load_send_is_not_lost_i1(rig: Rig) -> None:
    rig.create()
    rig.jobs.on_recorded(MID)
    original = rig.factory

    def factory(on_event: Any, on_exit: Any) -> Role:
        role = original(on_event, on_exit)
        _sync_error(rig, "model.load", "limit")
        return role

    rig.jobs._role_factory = factory
    rig.jobs.tick()
    assert rig.role.types() == ["model.load"]
    assert rig.jobs._active is not None and rig.jobs._active.phase == "crashed"
    rig.now += 1
    rig.jobs.tick()
    assert rig.role.types() == ["model.load", "model.load"]


@pytest.mark.parametrize("code", ["limit", "req-conflict"])
def test_synchronous_error_inside_job_start_send_is_not_lost_i1(rig: Rig, code: str) -> None:
    rig.create()
    rig.jobs.on_recorded(MID)
    rig.jobs.tick()
    _sync_error(rig, "meeting.job.start", code)
    rig.loaded()
    assert rig.role.kills == 1
    assert "meeting.job.credit" not in rig.role.types()
    rig.role.exited = True
    rig.jobs.tick()
    assert rig.meeting().state == MeetingState.FAILED and rig.jobs.active_id == ""


def test_synchronous_accept_inside_job_start_send_still_sends_credits_i1(rig: Rig) -> None:
    rig.create()
    rig.jobs.on_recorded(MID)
    rig.jobs.tick()

    def accept(message: dict[str, Any]) -> None:
        rig.role.emit(
            "meeting.job.accepted",
            req=message["req"],
            request_type="meeting.job.start",
            meeting_id=MID,
            meeting_generation=message["meeting_generation"],
            resume_from={"mic_ms": 0, "far_ms": 0},
        )

    rig.role.on_send["meeting.job.start"] = accept
    rig.loaded()
    assert rig.jobs.job_snapshot(MID)["phase"] == "running"  # type: ignore[index]
    assert rig.role.last("meeting.job.credit")["n"] == 64


# ---- review of step 2: durable cancel only (#2), no lost completions (#3) -----------


class BrokenWrites:
    """meeting.json writes of the jobs queue fail while ``on`` (disk/lease trouble)."""

    def __init__(self, rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
        from astra_voice.meetings.store import MeetingStoreError

        self.on = False
        original = rig.store.write

        def write(*args: Any, **kwargs: Any) -> Any:
            if self.on:
                raise MeetingStoreError("meeting-store-write")
            return original(*args, **kwargs)

        monkeypatch.setattr(rig.store, "write", write)
        monkeypatch.setattr(time, "sleep", lambda seconds: None)


def test_failed_cancel_write_sends_no_cancel_and_retries_d12(
    rig: Rig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig.create()
    rig.jobs.on_recorded(MID)
    rig.running()
    broken = BrokenWrites(rig, monkeypatch)
    broken.on = True
    rig.jobs.cancel(MID)
    assert "meeting.job.cancel" not in rig.role.types() and rig.role.kills == 0
    assert rig.jobs.active_id == MID and not rig.meeting().cancelled
    rig.now += 1
    rig.jobs.tick()  # second attempt fails too: still nothing sent
    assert "meeting.job.cancel" not in rig.role.types()
    broken.on = False
    rig.jobs.tick()
    assert "meeting.job.cancel" not in rig.role.types()  # waits for its retry time
    rig.now += 1
    rig.jobs.tick()
    assert rig.cancelled_at_send == [True]  # durable before meeting.job.cancel
    rig.jobs.shutdown()
    again = Rig(tmp_path)
    again.jobs.recover()
    again.jobs.tick()
    assert again.jobs.queued_ids == [] and again.roles == []


def test_cancel_that_cannot_be_written_ends_in_visible_error_not_silent_cancel_d12(
    rig: Rig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig.create()
    rig.jobs.on_recorded(MID)
    rig.running()
    broken = BrokenWrites(rig, monkeypatch)
    broken.on = True
    rig.jobs.cancel(MID)
    for _ in range(meeting_jobs.CANCEL_WRITE_ATTEMPTS - 1):
        rig.now += 1
        rig.jobs.tick()
    assert "meeting.job.cancel" not in rig.role.types()
    assert rig.role.kills == 1 and rig.jobs.failures[MID] == "io"
    broken.on = False
    rig.role.exited = True
    rig.jobs.tick()
    meeting = rig.meeting()
    assert meeting.state == MeetingState.FAILED and not meeting.cancelled
    assert rig.notices == [("failed", MID)] and rig.audio_present()
    rig.jobs.shutdown()
    again = Rig(tmp_path)
    again.jobs.recover()
    again.jobs.tick()
    assert again.jobs.queued_ids == [] and again.roles == []


def test_queued_cancel_that_cannot_be_written_is_reported_d12(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig.create()
    rig.memory = 10
    rig.jobs.on_recorded(MID)
    rig.jobs.tick()
    broken = BrokenWrites(rig, monkeypatch)
    broken.on = True
    rig.jobs.cancel(MID)
    for _ in range(meeting_jobs.CANCEL_WRITE_ATTEMPTS):
        rig.now += 1
        rig.jobs.tick()
    assert rig.jobs.failures[MID] == "io"
    assert rig.notices == [("waiting_memory", MID), ("failed", MID)]
    assert rig.roles == []


def test_admission_that_raises_releases_the_queue_and_retries_3(rig: Rig) -> None:
    rig.create()
    calls: list[int] = []

    def probe() -> ModelAdmission:
        calls.append(1)
        if len(calls) == 1:
            raise OSError("model-store")
        return ModelAdmission("ok", dict(LOAD))

    rig.jobs.model_probe = probe
    rig.jobs.on_recorded(MID)
    rig.jobs.tick()
    assert rig.jobs._admitting == "" and rig.jobs.queued_ids == [MID] and rig.roles == []
    rig.now += meeting_jobs.ADMIT_RETRY_S
    rig.jobs.tick()
    assert rig.role.types() == ["model.load"] and rig.jobs.active_id == MID


def test_admission_refused_by_a_full_writer_releases_the_queue_3(rig: Rig) -> None:
    import queue

    rig.create()
    rig.jobs.on_recorded(MID)
    run = rig.jobs._submit

    def full(operation: Callable[[], None]) -> None:
        rig.jobs._submit = run
        raise queue.Full

    rig.jobs._submit = full
    rig.jobs.tick()
    assert rig.jobs._admitting == "" and not rig.jobs.filesystem_pending
    rig.now += meeting_jobs.ADMIT_RETRY_S
    rig.jobs.tick()
    assert rig.role.types() == ["model.load"]


def test_cancel_refused_by_a_full_writer_is_retried_not_stuck_3(rig: Rig) -> None:
    import queue

    rig.create()
    rig.jobs.on_recorded(MID)
    rig.running()
    run = rig.jobs._submit

    def full(operation: Callable[[], None]) -> None:
        rig.jobs._submit = run
        raise queue.Full

    rig.jobs._submit = full
    rig.jobs.cancel(MID)
    assert "meeting.job.cancel" not in rig.role.types()
    rig.now += 1
    rig.jobs.tick()
    assert rig.cancelled_at_send == [True]


def test_exit_without_role_or_jobs_is_immediate(tmp_path: Path) -> None:
    """CI PR #65 guard: an idle queue (role never started, nothing queued, meetings off)
    with the real writer thread ends at once and starts no role."""
    rig = Rig(tmp_path)
    jobs = MeetingJobs(
        rig.store,
        rig.factory,
        transcribe_immediately=lambda: False,
        audio_retention_days=lambda: 30,
        model_probe=lambda: rig.admission,
        admit_allowed=lambda: False,
    )
    jobs.recover()
    deadline = time.monotonic() + 2.0
    while jobs.filesystem_pending and time.monotonic() < deadline:
        jobs.tick()
        time.sleep(0.01)
    started = time.monotonic()
    assert jobs.shutdown(timeout=1.0)
    assert time.monotonic() - started < 2.0
    assert rig.roles == []
