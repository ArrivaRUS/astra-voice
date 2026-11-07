"""MJ-14/Д12: descriptor cleanup, owner release, crash recovery (T-408/433/441/489)."""

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from test_meeting_jobs import MID, MID2, Rig

from astra_voice.meetings.events import MeetingState
from astra_voice.meetings.store import MeetingStoreError, Writer

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("filesystem_euid")]


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    return Rig(tmp_path)


def test_store_tombstone_and_recursive_purge_t433(rig: Rig) -> None:
    rig.create(state=MeetingState.DELETING)
    meeting = rig.meeting()
    rig.store.begin_delete(MID)
    assert rig.store.list() == []
    assert rig.store.pending_deletions() == [MID]
    with pytest.raises(MeetingStoreError, match="Встреча удаляется"):
        rig.store.create(meeting)
    with pytest.raises(MeetingStoreError):
        rig.store.read(MID, "meeting.json")
    nested = rig.store.root / f".deleting-{MID}" / "audio" / "nested"
    nested.mkdir(mode=0o700)
    # Unlink needs parent authority, not private-file read access: 0644 is removable.
    (nested / "public-mode").write_text("private contents")
    rig.store.purge_deleted(MID)
    assert rig.store.root.is_dir()
    assert rig.store.pending_deletions() == []


@pytest.mark.parametrize("entry", ["audio", "meeting"])
def test_purge_never_follows_home_symlink_t433(rig: Rig, tmp_path: Path, entry: str) -> None:
    home = tmp_path / "home-like"
    home.mkdir()
    secret = home / "keep"
    secret.write_text("keep")
    rig.create(state=MeetingState.DELETING)
    rig.store.begin_delete(MID)
    target = rig.store.root / f".deleting-{MID}"
    if entry == "audio":
        target /= "audio"
        (target / "mic-0000.wav").unlink()
        target.rmdir()
    else:
        rig.store.purge_deleted(MID)
    target.symlink_to(home, target_is_directory=True)
    rig.store.purge_deleted(MID)
    assert secret.read_text() == "keep"
    assert not target.is_symlink()


def test_begin_requires_durable_state(rig: Rig) -> None:
    rig.create()
    with pytest.raises(MeetingStoreError):
        rig.store.begin_delete(MID)
    assert rig.meeting().state == MeetingState.RECORDED


@pytest.mark.parametrize("role", [Writer.CAPTURE, Writer.TRANSCRIBE])
def test_purge_refuses_live_writer_without_waiting(rig: Rig, role: Writer) -> None:
    rig.create()
    with rig.store.writer(MID, role):
        rig.jobs.delete(MID, lambda: rig.store.begin_delete(MID))
        with pytest.raises(MeetingStoreError):
            rig.store.purge_deleted(MID)
        assert rig.store.pending_deletions() == [MID]
    rig.store.purge_deleted(MID)


@pytest.mark.parametrize("renamed", [False, True])
def test_recover_completes_both_crash_boundaries_without_asr_t441(rig: Rig, renamed: bool) -> None:
    rig.create(state=MeetingState.DELETING)
    if renamed:
        rig.store.begin_delete(MID)
    rig.jobs.recover_deletions()
    rig.jobs.tick()
    assert not (rig.store.root / MID).exists()
    assert rig.store.pending_deletions() == []
    assert rig.roles == [] and rig.jobs.queued_ids == []


def test_delete_active_waits_for_poll_and_drops_late_events_t408_t489(rig: Rig) -> None:
    rig.create()
    rig.jobs.on_recorded(MID)
    rig.running()
    releases = []

    def released() -> None:
        releases.append(MID)
        rig.jobs.purge_deleted(MID)

    rig.jobs.delete(MID, released)
    rig.done()
    rig.jobs.tick()
    assert releases == []
    assert (rig.store.root / MID).exists()
    rig.now += 6
    rig.jobs.tick()
    assert rig.role.kills and releases == []
    rig.role.exited = True
    rig.jobs.tick()
    assert releases == [MID]
    assert not (rig.store.root / MID).exists()
    rig.done()
    rig.job_event(
        "meeting.job.utterance",
        seq=999,
        id="u00999",
        track="mic",
        start_ms=0,
        end_ms=1,
        text="late",
        speaker="me",
    )
    rig.job_event("meeting.job.failed", code="io", message="late")
    rig.jobs.tick()
    assert rig.store.list() == []
    assert MID not in rig.jobs.meetings and MID not in rig.jobs.tombstones


@pytest.mark.parametrize(
    "state", [MeetingState.RECORDED, MeetingState.WAITING_MEMORY, MeetingState.NEEDS_MODEL]
)
def test_delete_idle_or_queued_is_immediate_t441(rig: Rig, state: MeetingState) -> None:
    rig.create(state=state)
    if state != MeetingState.RECORDED:
        rig.jobs.recover()
    rig.jobs.delete(MID, lambda: rig.jobs.purge_deleted(MID))
    assert rig.jobs.queued_ids == []
    assert not (rig.store.root / MID).exists()
    assert rig.store.pending_deletions() == []


def test_done_during_async_tombstone_cannot_release_owner_early_t489(rig: Rig) -> None:
    rig.create()
    rig.jobs.on_recorded(MID)
    rig.running()
    tasks: list[Callable[[], None]] = []
    rig.jobs._submit = tasks.append
    released = []
    rig.jobs.delete(MID, lambda: released.append(True))
    rig.done()  # arrives before meeting.json has reached the disk
    assert rig.jobs.active_id == MID and released == []
    tasks.pop(0)()
    rig.jobs.tick()
    assert rig.jobs.active_id == MID and released == []
    assert rig.role.last("meeting.job.cancel")
    rig.now += 6
    rig.jobs.tick()
    assert released == []
    rig.role.exited = True
    rig.jobs.tick()
    assert released == [True]


def test_failed_async_tombstone_replays_terminal_event(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig.create()
    rig.jobs.on_recorded(MID)
    rig.running()
    tasks: list[Callable[[], None]] = []
    rig.jobs._submit = tasks.append
    original = rig.store.write
    monkeypatch.setattr(
        rig.store, "write", lambda *a, **kw: (_ for _ in ()).throw(MeetingStoreError("fixed"))
    )
    rig.jobs.delete(MID, lambda: pytest.fail("owner released after failed tombstone"))
    rig.done()
    tasks.pop(0)()
    monkeypatch.setattr(rig.store, "write", original)
    rig.jobs.tick()
    while tasks:
        tasks.pop(0)()
        rig.jobs.tick()
    assert rig.jobs.deletions[MID] == "error"
    assert rig.meeting().state == MeetingState.DONE


def test_empty_tombstone_is_recovered(rig: Rig) -> None:
    rig.store.root.mkdir(mode=0o700)
    (rig.store.root / f".deleting-{MID}").mkdir(mode=0o700)
    rig.jobs.recover_deletions()
    assert rig.store.pending_deletions() == [] and rig.roles == []


def test_non_directory_tombstone_is_refused(rig: Rig) -> None:
    rig.store.root.mkdir(mode=0o700)
    remnant = rig.store.root / f".deleting-{MID}"
    remnant.write_text("keep")
    with pytest.raises(MeetingStoreError, match="Недопустимый каталог"):
        rig.store.purge_deleted(MID)
    assert remnant.read_text() == "keep"


@pytest.mark.parametrize("entry", ["root", "nested"])
def test_foreign_owned_remnant_is_refused(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, entry: str
) -> None:
    import os

    rig.create(state=MeetingState.DELETING)
    rig.store.begin_delete(MID)
    target = f".deleting-{MID}" if entry == "root" else "foreign"
    foreign = rig.store.root / f".deleting-{MID}" / "audio" / "foreign"
    foreign.write_text("keep")
    original = os.stat

    def other_owner(
        path: str | bytes | int | os.PathLike[str] | os.PathLike[bytes],
        *,
        dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> os.stat_result:
        info = original(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)
        if path == target:
            values = list(info)
            values[4] = os.geteuid() + 1
            return os.stat_result(values)
        return info

    monkeypatch.setattr(os, "stat", other_owner)
    with pytest.raises(MeetingStoreError, match="Чужой владелец"):
        rig.store.purge_deleted(MID)
    assert foreign.read_text() == "keep"


def test_purge_failure_keeps_tombstone_for_restart(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = rig.store.purge_deleted
    rig.create()

    def fail(_meeting_id: str) -> None:
        raise MeetingStoreError("fixed")

    monkeypatch.setattr(rig.store, "purge_deleted", fail)
    rig.jobs.delete(MID, lambda: rig.jobs.purge_deleted(MID))
    assert rig.jobs.deletions[MID] == "purge_error" and MID not in rig.jobs.tombstones
    assert rig.store.pending_deletions() == [MID]
    monkeypatch.setattr(rig.store, "purge_deleted", original)
    rig.jobs.recover_deletions()
    assert rig.store.pending_deletions() == []


def test_purge_runs_on_jobs_worker_and_leaves_gui_free(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading
    import time

    from astra_voice.core.meeting_jobs import MeetingJobs

    entered, finish = threading.Event(), threading.Event()
    caller = threading.get_ident()
    threads = []
    original = rig.store.purge_deleted

    def paused(mid: str) -> None:
        threads.append(threading.get_ident())
        entered.set()
        assert finish.wait(2)
        original(mid)

    monkeypatch.setattr(rig.store, "purge_deleted", paused)
    jobs = MeetingJobs(rig.store, rig.factory)
    rig.create()
    try:
        jobs.delete(MID, lambda: jobs.purge_deleted(MID))
        deadline = time.monotonic() + 2
        while not entered.is_set() and time.monotonic() < deadline:
            jobs.tick()
            time.sleep(0.005)
        assert entered.is_set()
        assert threads == [threads[0]] and threads[0] != caller
        assert jobs.filesystem_pending
        # Returning tick while rmtree is blocked proves it isn't running in the caller.
        jobs.tick()
        finish.set()
        while jobs.filesystem_pending and time.monotonic() < deadline:
            jobs.tick()
            time.sleep(0.005)
        assert jobs.deletions[MID] == "deleted"
    finally:
        finish.set()
        jobs.shutdown(timeout=2)


def test_delete_queued_meeting_does_not_cancel_other_active_owner(rig: Rig) -> None:
    rig.create(MID)
    rig.create(MID2)
    rig.jobs.on_recorded(MID)
    rig.jobs.on_recorded(MID2)
    rig.running(MID)
    rig.jobs.delete(MID2, lambda: rig.jobs.purge_deleted(MID2))
    assert rig.jobs.active_id == MID and rig.jobs.queued_ids == []
    assert "meeting.job.cancel" not in rig.role.types()
    assert not (rig.store.root / MID2).exists() and rig.audio_present(MID)


def test_r2_failed_tombstone_replays_all_role_events(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig.create()
    rig.jobs.on_recorded(MID)
    rig.running()
    tasks: list[Callable[[], None]] = []
    rig.jobs._submit = tasks.append
    monkeypatch.setattr(
        rig.store, "write", lambda *a, **kw: (_ for _ in ()).throw(MeetingStoreError("fixed"))
    )
    rig.jobs.delete(MID, lambda: pytest.fail("failed tombstone released owner"))
    events: list[dict[str, Any]] = [
        {"type": "pong", "req": "ping"},
        {"type": "model.unloaded", "req": "unload"},
        {"type": "meeting.job.labels", "seq": 11},
        {"type": "meeting.job.applied", "seq": 12},
        {"type": "meeting.job.replace", "seq": 13},
    ]
    for event in events:
        rig.role.emit(event["type"], **{k: v for k, v in event.items() if k != "type"})
    # Progress may carry seq too: the newest progress replaces the older one.
    rig.job_event("meeting.job.progress", seq=1, stage="asr", done_ms=1, total_ms=10)
    rig.job_event("meeting.job.progress", seq=2, stage="asr", done_ms=2, total_ms=10)
    original_event = rig.jobs._event
    replayed: list[dict[str, Any]] = []

    def replay(event: dict[str, Any]) -> None:
        replayed.append(event)
        original_event(event)

    monkeypatch.setattr(rig.jobs, "_event", replay)
    tasks.pop(0)()
    rig.jobs.tick()
    assert [event["type"] for event in replayed] == [
        *(event["type"] for event in events),
        "meeting.job.progress",
    ]
    assert replayed[-1]["done_ms"] == 2
    assert all(event["generation"] == rig.role.generation for event in replayed)


def test_r2_purge_retries_after_live_capture_lease_is_released(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    import contextlib
    import time

    rig.create()
    sleeps = []
    with contextlib.ExitStack() as owners:
        owners.enter_context(rig.store.writer(MID, Writer.CAPTURE))

        def release_after_delay(delay: float) -> None:
            sleeps.append(delay)
            owners.close()

        monkeypatch.setattr(time, "sleep", release_after_delay)
        rig.jobs.delete(MID, lambda: rig.jobs.purge_deleted(MID))
    assert rig.jobs.deletions[MID] == "deleted"
    assert sleeps == [0.5]
    assert not (rig.store.root / MID).exists() and not rig.store.pending_deletions()


def test_r2_erase_snapshots_scandir_before_unlink(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    import contextlib
    import os
    from collections.abc import Iterator

    rig.create(state=MeetingState.DELETING)
    rig.store.begin_delete(MID)
    original_scan, original_unlink = os.scandir, os.unlink
    scanning: set[int] = set()

    @contextlib.contextmanager
    def scan(descriptor: int) -> Iterator[Iterator[os.DirEntry[str]]]:
        scanning.add(descriptor)
        try:
            with original_scan(descriptor) as entries:
                yield entries
        finally:
            scanning.remove(descriptor)

    def unlink(name: str, *, dir_fd: int | None = None) -> None:
        assert not scanning, "unlink while a directory stream is still open"
        original_unlink(name, dir_fd=dir_fd)

    monkeypatch.setattr(os, "scandir", scan)
    monkeypatch.setattr(os, "unlink", unlink)
    rig.store.purge_deleted(MID)
    assert not rig.store.pending_deletions()


@pytest.mark.parametrize("probe", ["stat", "fstat"])
@pytest.mark.parametrize("entry", ["root", "nested"])
def test_r2_erase_refuses_another_device(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, probe: str, entry: str
) -> None:
    import os

    rig.create(state=MeetingState.DELETING)
    rig.store.begin_delete(MID)
    tombstone = rig.store.root / f".deleting-{MID}"
    target = tombstone if entry == "root" else tombstone / "audio"
    target_inode = target.stat().st_ino
    original_stat, original_fstat = os.stat, os.fstat

    def another_device(info: os.stat_result) -> os.stat_result:
        if info.st_ino != target_inode:
            return info
        values = list(info)
        values[2] = info.st_dev + 1
        return os.stat_result(values)

    def stat(
        path: str | bytes | int | os.PathLike[str] | os.PathLike[bytes],
        *,
        dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> os.stat_result:
        return another_device(original_stat(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks))

    def fstat(descriptor: int) -> os.stat_result:
        return another_device(original_fstat(descriptor))

    monkeypatch.setattr(os, probe, stat if probe == "stat" else fstat)
    with pytest.raises(MeetingStoreError, match="Другая файловая система"):
        rig.store.purge_deleted(MID)
    assert (target / "mic-0000.wav" if entry == "nested" else target / "meeting.json").exists()


def test_r2_sigkill_inside_erase_is_finished_at_startup(rig: Rig) -> None:
    import os
    import signal
    import subprocess
    import sys

    rig.create(state=MeetingState.DELETING)
    rig.store.begin_delete(MID)
    tombstone = rig.store.root / f".deleting-{MID}"
    nested = tombstone / "audio" / "nested"
    nested.mkdir(mode=0o700)
    (nested / "remove-before-crash").write_bytes(b"remove")
    (nested / "keep-after-crash").write_bytes(b"remaining")
    script = """
import os, signal, sys
from pathlib import Path
from astra_voice.meetings.store import MeetingStore
nested = (Path(sys.argv[1]) / f".deleting-{sys.argv[2]}" / "audio" / "nested").stat()
original = os.unlink
def kill_after_unlink(name, **kwargs):
    parent = os.fstat(kwargs["dir_fd"])
    original(name, **kwargs)
    # Stop after the first removed file in nested, regardless of scandir order.
    if (parent.st_dev, parent.st_ino) == (nested.st_dev, nested.st_ino):
        os.kill(os.getpid(), signal.SIGKILL)
os.unlink = kill_after_unlink
MeetingStore(Path(sys.argv[1])).purge_deleted(sys.argv[2])
raise AssertionError("crash boundary not reached")
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(rig.store.root), MID],
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src")},
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == -signal.SIGKILL, result.stderr.decode()
    remnants = [path for path in nested.iterdir() if path.is_file()]
    assert len(remnants) == 1
    assert remnants[0].name in {"remove-before-crash", "keep-after-crash"}
    assert tombstone.is_dir()
    rig.jobs.recover_deletions()
    rig.jobs.tick()
    assert not tombstone.exists() and not remnants[0].exists()
    assert not rig.store.pending_deletions() and not rig.roles
