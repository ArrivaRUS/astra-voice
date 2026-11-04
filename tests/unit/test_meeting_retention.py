"""T-407/433/500: durable retention uses success/recording dates, never directory names."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from test_meeting_jobs import MID, MID2, Rig

from astra_voice.core import meeting_jobs
from astra_voice.meetings.events import MeetingState, ProcessingStatus
from astra_voice.meetings.model import Audio, Meeting, Transcript, Transcription, Utterance
from astra_voice.meetings.store import MeetingStore, MeetingStoreError, Writer

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("filesystem_euid")]
NOW = datetime(2026, 11, 4, 12, tzinfo=UTC)


class RetentionRig(Rig):
    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.wall = NOW
        self.days = 0
        self.jobs.shutdown()
        self.jobs = meeting_jobs.MeetingJobs(
            self.store,
            self.factory,
            audio_retention_days=lambda: self.retention,
            meeting_retention_days=lambda: self.days,
            wall_clock=lambda: self.wall,
            clock=lambda: self.now,
            model_probe=lambda: self.admission,
            mem_available=lambda: self.memory,
            submit=lambda task: task(),
            notify=lambda kind, payload: self.notices.append(
                (kind, payload.id if isinstance(payload, Meeting) else payload)
            ),
            echo_gate=False,
        )

    def saved(self, mid: str = MID, *, age: int = 8, **fields: Any) -> bytes:
        self.create(
            mid,
            fields.pop("state", MeetingState.DONE),
            processing_status=fields.pop("processing_status", ProcessingStatus.SUCCEEDED),
            audio=Audio(retain_days=7),
            transcription=Transcription(finished_at=(self.wall - timedelta(days=age)).isoformat()),
            **fields,
        )
        self.store.write(
            mid,
            "transcript.json",
            Transcript(
                meeting_id=mid,
                utterances=[Utterance(id="u00001", seq=1, text="Сохранённый текст")],
            ),
            writer=Writer.TRANSCRIBE,
        )
        return (self.store.root / mid / "transcript.json").read_bytes()

    def age(self, days: int, mid: str = MID) -> None:
        meeting = self.meeting(mid)
        meeting.started_at = (self.wall - timedelta(days=days)).isoformat()
        self.store.write(mid, "meeting.json", meeting, writer=Writer.GUI)

    def summaries(self) -> list[Any]:
        return [payload for kind, payload in self.notices if kind == "retention"]


@pytest.fixture
def rig(tmp_path: Path) -> Iterator[RetentionRig]:
    instance = RetentionRig(tmp_path)
    yield instance
    instance.jobs.shutdown()


@pytest.mark.parametrize("age,removed", [(8, True), (7, True), (6, False), (-1, False)])
def test_audio_deadline_from_finished_at_t407(rig: RetentionRig, age: int, removed: bool) -> None:
    text = rig.saved(age=age)
    rig.jobs._retention_pass()
    meeting = rig.meeting()
    assert meeting.audio.present is not removed
    assert bool(meeting.audio.deleted_at) is removed
    assert not meeting.cleanup_pending
    assert rig.audio_present() is not removed
    assert (rig.store.root / MID / "transcript.json").read_bytes() == text
    assert rig.summaries() == []
    assert not rig.roles


@pytest.mark.parametrize(
    "state,status",
    [
        (MeetingState.DONE, status)
        for status in ProcessingStatus
        if status != ProcessingStatus.SUCCEEDED
    ]
    + [
        (state, ProcessingStatus.SUCCEEDED)
        for state in (
            MeetingState.INTERRUPTED,
            MeetingState.RECORDED,
            MeetingState.TRANSCRIBING,
            MeetingState.FAILED,
        )
    ],
)
def test_audio_requires_full_success_t407(
    rig: RetentionRig, state: MeetingState, status: ProcessingStatus
) -> None:
    rig.saved(age=100, state=state, processing_status=status)
    rig.jobs.admit_allowed = lambda: False
    rig.jobs._retention_pass()
    assert rig.audio_present() and rig.meeting().audio.present
    assert rig.summaries() == []


@pytest.mark.parametrize(
    "state",
    [
        MeetingState.RECORDED,
        MeetingState.DONE,
        MeetingState.PAUSED,
        MeetingState.RECORDING,
        MeetingState.INTERRUPTED,
        MeetingState.FAILED,
    ],
)
@pytest.mark.parametrize("age", [29, 30, 31])
def test_meeting_age_uses_started_at_t433(rig: RetentionRig, state: MeetingState, age: int) -> None:
    rig.create(state=state, cancelled=True)
    rig.age(age)
    rig.days = 30
    rig.jobs._retention_pass()
    removed = age >= 30 and state not in {MeetingState.RECORDING, MeetingState.PAUSED}
    assert (rig.store.root / MID).exists() is not removed
    assert rig.store.pending_deletions() == []
    assert not rig.jobs.tombstones
    assert rig.summaries() == ([1] if removed else [])


def test_never_delete_meetings_when_disabled(rig: RetentionRig) -> None:
    rig.create()
    rig.age(1000)
    rig.jobs._retention_pass()
    assert (rig.store.root / MID).exists() and not rig.summaries()


def test_startup_retention_precedes_asr_resume(rig: RetentionRig) -> None:
    rig.create(state=MeetingState.TRANSCRIBING)
    rig.age(31)
    rig.days = 30
    rig.jobs.start_retention()
    rig.jobs.recover()
    rig.jobs.tick()
    assert not rig.roles and not rig.jobs.queued_ids
    assert not (rig.store.root / MID).exists()


def test_daily_retention_cancels_active_and_waits_for_owners_t500(rig: RetentionRig) -> None:
    rig.create()
    rig.jobs._retention_pass()
    rig.jobs.on_recorded(MID)
    rig.running()
    rig.age(31)
    rig.days = 30
    released: list[Callable[[], None]] = []
    rig.jobs.release_files = lambda mid, done: released.append(done)
    rig.now += 86400
    rig.jobs.tick()
    assert rig.role.last("meeting.job.cancel")
    assert (rig.store.root / MID).exists() and not released
    rig.now += 6
    rig.jobs.tick()
    assert rig.role.kills and not released and not rig.summaries()
    rig.role.exited = True
    rig.jobs.tick()
    assert len(released) == 1 and (rig.store.root / MID).exists()
    released.pop()()
    assert not (rig.store.root / MID).exists()
    assert rig.summaries() == [1]


def test_cleanup_crash_recovery_preserves_text_without_asr_t433(
    rig: RetentionRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    text = rig.saved()
    real_remove = meeting_jobs.remove_audio
    monkeypatch.setattr(
        meeting_jobs, "remove_audio", lambda *args: (_ for _ in ()).throw(OSError())
    )
    rig.jobs._retention_pass()
    assert rig.meeting().cleanup_pending and rig.audio_present()
    monkeypatch.setattr(meeting_jobs, "remove_audio", real_remove)
    rig.jobs._retention_pass()
    rig.jobs.tick()
    assert not rig.meeting().audio.present and not rig.meeting().cleanup_pending
    assert (rig.store.root / MID / "transcript.json").read_bytes() == text
    assert not rig.roles


def test_schedule_waits_24_hours_and_audio_cleanup_is_silent(rig: RetentionRig) -> None:
    rig.saved(age=6)
    rig.saved(MID2, age=6)
    rig.jobs._retention_pass()
    rig.wall += timedelta(hours=23)
    rig.now += 86399
    rig.jobs.tick()
    assert rig.audio_present() and not rig.summaries()
    rig.now += 1
    rig.wall += timedelta(hours=1)
    rig.jobs.tick()
    assert not rig.audio_present() and not rig.audio_present(MID2)
    assert rig.summaries() == []
    rig.now += 86400
    rig.jobs.tick()
    assert rig.summaries() == []


def test_retention_ignores_processing_gate_and_current_audio_setting(rig: RetentionRig) -> None:
    rig.saved()
    rig.retention = 30
    rig.jobs.admit_allowed = lambda: False
    rig.jobs._retention_pass()
    assert not rig.audio_present() and rig.summaries() == []


def test_one_failure_does_not_stop_other_meetings(
    rig: RetentionRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig.saved()
    rig.saved(MID2)
    original = meeting_jobs.remove_audio

    def remove(store: MeetingStore, mid: str) -> None:
        if mid == MID:
            raise MeetingStoreError("fixed")
        original(store, mid)

    monkeypatch.setattr(meeting_jobs, "remove_audio", remove)
    rig.jobs._retention_pass()
    assert rig.audio_present() and not rig.audio_present(MID2)
    assert rig.summaries() == []


def test_scan_runs_on_worker_and_reads_snapshot_once(
    rig: RetentionRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig.saved()
    tasks: list[Callable[[], None]] = []
    calls: list[bool] = []
    original = rig.store.list

    def listing(*, include_deleting: bool = False) -> list[Meeting]:
        calls.append(True)
        return original(include_deleting=include_deleting)

    monkeypatch.setattr(rig.store, "list", listing)
    rig.jobs._submit = tasks.append
    rig.jobs._retention_pass()
    assert not calls and rig.audio_present()
    tasks.pop(0)()
    rig.jobs.tick()
    assert calls == [True] and not rig.audio_present()


def test_zero_days_cleanup_still_happens_on_success(rig: RetentionRig) -> None:
    rig.retention = 0
    rig.create()
    rig.jobs.on_recorded(MID)
    rig.running()
    rig.done()
    assert not rig.audio_present() and not rig.meeting().cleanup_pending
    assert rig.meeting().transcription.finished_at == rig.wall.isoformat()


def test_crash_after_unlink_before_metadata_is_idempotent_t433(
    rig: RetentionRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    text = rig.saved(cleanup_pending=True)
    real_update = rig.jobs._update
    monkeypatch.setattr(rig.jobs, "_update", lambda *a, **kw: None)
    rig.jobs._retention_pass()
    assert rig.meeting().cleanup_pending and not rig.audio_present()
    monkeypatch.setattr(rig.jobs, "_update", real_update)
    rig.jobs._retention_pass()
    assert not rig.meeting().cleanup_pending and not rig.meeting().audio.present
    assert (rig.store.root / MID / "transcript.json").read_bytes() == text
    assert not rig.roles
    rig.jobs._retention_pass()
    assert rig.summaries() == []


@pytest.mark.parametrize(
    "status", [ProcessingStatus.PARTIAL, ProcessingStatus.CANCELLED, ProcessingStatus.FAILED]
)
def test_pending_cleanup_never_overrides_unsuccessful_status(
    rig: RetentionRig, status: ProcessingStatus
) -> None:
    rig.saved(age=100, cleanup_pending=True, processing_status=status)
    rig.jobs._retention_pass()
    assert rig.audio_present() and rig.meeting().cleanup_pending
    assert not rig.summaries()


def test_mixed_pass_counts_only_deleted_meetings(rig: RetentionRig) -> None:
    rig.saved()
    rig.create(MID2)
    rig.age(31, MID2)
    rig.days = 30
    rig.jobs._retention_pass()
    assert not rig.audio_present() and not (rig.store.root / MID2).exists()
    assert rig.summaries() == [1]


def test_failed_purge_is_not_counted_and_restart_finishes_t500(
    rig: RetentionRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig.create()
    rig.age(31)
    rig.days = 30
    purge = rig.store.purge_deleted
    monkeypatch.setattr(
        rig.store, "purge_deleted", lambda mid: (_ for _ in ()).throw(MeetingStoreError("fixed"))
    )
    rig.jobs._retention_pass()
    assert not rig.summaries() and rig.store.pending_deletions() == [MID]
    monkeypatch.setattr(rig.store, "purge_deleted", purge)
    rig.jobs.recover_deletions()
    assert not (rig.store.root / MID).exists() and not rig.store.pending_deletions()
    assert not rig.roles


def test_wall_clock_backwards_does_not_expire_audio(rig: RetentionRig) -> None:
    rig.saved(age=6)
    rig.jobs._retention_pass()
    rig.wall -= timedelta(days=10)
    rig.now += 86400
    rig.jobs.tick()
    assert rig.audio_present() and not rig.summaries()


def test_waiting_owner_does_not_block_next_daily_pass(rig: RetentionRig) -> None:
    rig.create()
    rig.age(31)
    rig.days = 30
    releases: list[Callable[[], None]] = []
    rig.jobs.release_files = lambda mid, done: releases.append(done)
    rig.jobs._retention_pass()
    assert MID in rig.jobs.tombstones and len(releases) == 1
    rig.saved(MID2)
    rig.now += 86400
    rig.jobs.tick()
    assert not rig.audio_present(MID2) and rig.summaries() == []
    releases.pop()()
    assert rig.summaries() == [1]


@pytest.mark.parametrize("days", [1, 30, 365])
def test_arbitrary_admin_audio_period_is_frozen_at_success(rig: RetentionRig, days: int) -> None:
    rig.create()
    rig.retention = days
    rig.jobs.on_recorded(MID)
    rig.running()
    rig.done()
    assert rig.meeting().audio.retain_days == days
    rig.jobs._retention_pass()
    rig.retention = 0
    rig.wall += timedelta(days=days)
    rig.now += 86400
    rig.jobs.tick()
    assert not rig.audio_present() and rig.summaries() == []


def test_path_like_title_never_changes_retention_target_t433(rig: RetentionRig) -> None:
    sentinel = rig.store.root.parent / "x"
    sentinel.write_text("keep", encoding="utf-8")
    rig.create(title="../../x")
    rig.age(31)
    rig.days = 30
    rig.jobs._retention_pass()
    assert not (rig.store.root / MID).exists()
    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert rig.summaries() == [1]


def test_sleep_wall_clock_forward_triggers_retention_without_monotonic(rig: RetentionRig) -> None:
    rig.saved(age=6)
    rig.jobs._retention_pass()
    rig.wall += timedelta(hours=25)
    rig.jobs.tick()
    assert not rig.audio_present()
    assert not rig.summaries()


def test_wall_clock_backwards_does_not_trigger_pass(
    rig: RetentionRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig.saved(age=6)
    rig.jobs._retention_pass()
    monkeypatch.setattr(rig.store, "list", lambda **kw: pytest.fail("early retention pass"))
    rig.wall -= timedelta(days=10)
    rig.jobs.tick()
    assert rig.audio_present()


def test_pending_cleanup_and_expired_meeting_count_once_without_audio_cleanup(
    rig: RetentionRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig.saved(cleanup_pending=True)
    rig.age(31)
    rig.days = 30
    monkeypatch.setattr(meeting_jobs, "remove_audio", lambda *a: pytest.fail("unneeded cleanup"))
    rig.jobs._retention_pass()
    assert not (rig.store.root / MID).exists()
    assert rig.summaries() == [1]


def test_zero_days_crash_cleanup_is_silent(rig: RetentionRig) -> None:
    text = rig.saved(cleanup_pending=True)
    meeting = rig.meeting()
    meeting.audio.retain_days = 0
    rig.store.write(MID, "meeting.json", meeting, writer=Writer.GUI)
    rig.jobs._retention_pass()
    assert not rig.meeting().audio.present and not rig.meeting().cleanup_pending
    assert (rig.store.root / MID / "transcript.json").read_bytes() == text
    assert not rig.summaries()


def test_pending_deletions_error_does_not_block_retention_or_resume(
    rig: RetentionRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig.saved()
    rig.create(MID2, state=MeetingState.TRANSCRIBING)
    rig.jobs.admit_allowed = lambda: False
    monkeypatch.setattr(
        rig.store, "pending_deletions", lambda: (_ for _ in ()).throw(MeetingStoreError("fixed"))
    )
    rig.jobs.recover_deletions()
    rig.jobs.recover()
    rig.jobs.tick()
    assert not rig.audio_present()
    assert rig.jobs.queued_ids == [MID2]
    assert not rig.roles


def test_list_error_once_allows_next_resume_and_retention(
    rig: RetentionRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig.saved()
    rig.create(MID2, state=MeetingState.TRANSCRIBING)
    rig.jobs.admit_allowed = lambda: False
    listing = rig.store.list
    calls = 0

    def fail_once(*, include_deleting: bool = False) -> list[Meeting]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise MeetingStoreError("fixed")
        return listing(include_deleting=include_deleting)

    monkeypatch.setattr(rig.store, "list", fail_once)
    rig.jobs.recover()
    rig.jobs.recover()
    rig.jobs._retention_pass()
    assert rig.jobs.queued_ids == [MID2] and not rig.audio_present()
    assert not rig.jobs._retention_scanning and not rig.roles


def test_invalid_retention_setting_does_not_leave_scheduler_blocked(rig: RetentionRig) -> None:
    rig.jobs.meeting_retention_days = lambda: int("invalid")
    with pytest.raises(ValueError):
        rig.jobs._retention_pass()
    assert not rig.jobs._retention_scanning
    rig.jobs.meeting_retention_days = lambda: 0
    rig.create()
    rig.jobs.on_recorded(MID)
    rig.running()
    assert rig.jobs.active_id == MID


@pytest.mark.parametrize("stamp", ["2027-01-01T12:00:00+00:00", "invalid"])
def test_future_or_invalid_meeting_start_is_never_deleted(
    rig: RetentionRig, monkeypatch: pytest.MonkeyPatch, stamp: str
) -> None:
    rig.create()
    meeting = rig.meeting()
    meeting.started_at = stamp
    # A damaged timestamp cannot be persisted through the validated store. Inject
    # it at the snapshot boundary to exercise the defensive date parser itself.
    monkeypatch.setattr(rig.store, "list", lambda **kw: [meeting])
    rig.days = 30
    rig.jobs._retention_pass()
    assert (rig.store.root / MID).exists() and not rig.summaries()
    assert not rig.jobs._retention_scanning


def test_worker_order_keeps_startup_retention_ahead_of_resume(rig: RetentionRig) -> None:
    rig.create(state=MeetingState.TRANSCRIBING)
    rig.age(31)
    rig.days = 30
    tasks: list[Callable[[], None]] = []
    rig.jobs._submit = tasks.append
    rig.jobs.start_retention()
    rig.jobs.recover()
    # Run both scans before their GUI completions, like a fast serial writer.
    tasks.pop(0)()
    tasks.pop(0)()
    rig.jobs.tick()
    while tasks:
        tasks.pop(0)()
        rig.jobs.tick()
    assert not rig.roles and not rig.jobs.queued_ids
    assert not (rig.store.root / MID).exists()
