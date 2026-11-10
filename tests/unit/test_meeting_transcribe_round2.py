"""Round 2 regressions: lifetime lease, resident RAM, canonical coverage, grid."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

from astra_voice.meetings.events import PhaseStatus
from astra_voice.meetings.model import DiarProgress, DiarTurn, ModelIdentity, Utterance
from astra_voice.meetings.store import MeetingStoreError, Writer
from astra_voice.worker import meeting_transcribe as transcribe
from astra_voice.worker.meeting_transcribe import _Durable, _Job, diar_blocks
from helpers.meeting_audio import MID, Rig, make_disk

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("filesystem_euid")]


def request(*, diarization: bool = True) -> dict[str, Any]:
    return {
        "meeting_id": MID,
        "meeting_generation": 0,
        "diarization": diarization,
        "echo_gate": False,
        "tracks": ["far"],
        "self_track": "mic",
        "variant": "app_stream",
        "limits": {"max_ms": 14_400_000, "max_utterances": 9000, "max_blocks": 12},
    }


def test_r2_a1_duplicate_of_diar_lease_outlives_parent_owner(tmp_path: Path) -> None:
    disk = make_disk(tmp_path)
    with disk.writer(MID, Writer.DIAR) as owner:
        inherited = os.dup(owner.diar_lease_fd())
    try:
        with pytest.raises(MeetingStoreError), disk.writer(MID, Writer.DIAR):
            pass
    finally:
        os.close(inherited)
    with disk.writer(MID, Writer.DIAR):
        pass
    with pytest.raises(MeetingStoreError):
        owner.diar_lease_fd()
    with disk.writer(MID, Writer.TRANSCRIBE) as wrong_role:
        with pytest.raises(MeetingStoreError):
            wrong_role.diar_lease_fd()


@pytest.mark.parametrize("diarization", [False, True])
def test_r2_a2_resident_asr_is_not_charged_again(tmp_path: Path, diarization: bool) -> None:
    rig = Rig(make_disk(tmp_path), memory_mb=1300)
    try:
        rig.load()
        rig.memory_mb = 600  # model.load consumed its 700 MB out of the original 1300
        assert rig.state._ensure_model(_Job(request(diarization=diarization)))
        assert rig.engine.loads == 1
    finally:
        rig.close()


def test_r2_a2_diar_checks_only_extra_budget_after_asr_unload(tmp_path: Path) -> None:
    rig = Rig(make_disk(tmp_path))
    try:
        rig.load()
        rig.state._unload_ack()
        rig.memory_mb = 1100
        rig.state._diar_memory(_Job(request()), time.monotonic() + 0.03)
        assert rig.state.engine is None
    finally:
        rig.close()


def test_r2_a3_canonical_intervals_are_combined_before_speaker_choice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    disk = make_disk(tmp_path)
    rig = Rig(disk)
    try:
        job = _Job(request())
        durable = _Durable("0" * 64, "0" * 64, ModelIdentity(), 9000, 0)
        durable.asr_status = PhaseStatus.DONE
        durable.diar = DiarProgress(
            status=PhaseStatus.DONE,
            turns=[DiarTurn("S1", 0, 300), DiarTurn("S2", 300, 600), DiarTurn("S3", 600, 1000)],
            label_map={"S1": "S1", "S2": "S1", "S3": "S3"},
        )
        monkeypatch.setattr(rig.state, "_finish", lambda *args: None)
        row = Utterance("u0", track="far", end_ms=1000, text="Текст")
        reader: Any = None  # resume of completed diar never reads PCM
        with disk.writer(MID, Writer.TRANSCRIBE) as owner:
            rig.state._post_asr(
                job,
                owner,
                reader,
                [],
                [row],
                {"mic": 0, "far": 1000},
                durable,
                1000,
                time.monotonic(),
                0,
            )
        assert row.speaker == "S1"  # canonical 60%, not provisional S3's 40%
        # Original durable evidence retains the provisional intervals and map.
        assert [t.label for t in durable.diar.turns] == ["S1", "S2", "S3"]
    finally:
        rig.close()


def speech_except(pauses: list[tuple[int, int]], duration: int) -> list[Utterance]:
    regions: list[Utterance] = []
    first = 0
    for a, b in [*pauses, (duration, duration)]:
        if a > first:
            regions.append(Utterance(f"u{len(regions)}", start_ms=first, end_ms=a))
        first = b
    return regions


def test_r2_b_grid_shifts_forward_without_accumulating_drift() -> None:
    duration = 14_400_000
    pauses = [(k * 1_200_000 + 89_000, k * 1_200_000 + 90_000) for k in range(1, 12)]
    blocks = diar_blocks(speech_except(pauses, duration), "mic", duration)
    assert len(blocks) == 12
    assert blocks[0][1] == 1_289_000
    assert [b for _, b in blocks[:-1]] == [a for a, _ in pauses]
    assert all(0 < b - a <= 1_380_000 for a, b in blocks)
    assert blocks[-1][1] == duration


def test_r2_b_alternating_edge_pauses_and_no_pause_grid() -> None:
    duration = 14_400_000
    pauses = []
    for k in range(1, 12):
        grid = k * 1_200_000
        pauses.append((grid + 89_500, grid + 90_000) if k % 2 else (grid - 90_000, grid - 89_500))
    blocks = diar_blocks(speech_except(pauses, duration), "mic", duration)
    assert len(blocks) == 12
    assert max(b - a for a, b in blocks) <= 1_380_000
    assert blocks == [
        (a, b)
        for a, b in zip([0, *[b for _, b in blocks[:-1]]], [b for _, b in blocks], strict=True)
    ]
    continuous = [Utterance("u0", end_ms=duration)]
    assert diar_blocks(continuous, "mic", duration) == [
        (k * 1_200_000, (k + 1) * 1_200_000) for k in range(12)
    ]


def test_r2_b_new_boundary_semantics_and_search_window_affect_fingerprint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = Rig(make_disk(tmp_path))
    try:
        job = _Job(request())
        identity = ModelIdentity()
        before = rig.state._config(job, identity)
        monkeypatch.setattr(transcribe, "DIAR_PAUSE_SEARCH_MS", 89_000)
        assert rig.state._config(job, identity) != before
        # Reconstruct the legacy fingerprint from the current configuration:
        # removing the new fixed-grid/1380-s facts must make it incompatible.
        original_dumps = json.dumps

        def legacy_dumps(document: Any, **kwargs: Any) -> str:
            if isinstance(document, dict) and "diar" in document:
                document = dict(document)
                document["diar"] = dict(document["diar"])
                document["diar"].pop("grid", None)
                document["diar"].pop("max_block_ms", None)
            return original_dumps(document, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(json, "dumps", legacy_dumps)
            legacy = rig.state._config(job, identity)
        assert legacy != rig.state._config(job, identity)
    finally:
        rig.close()


def test_r2_a1_transport_inherits_exact_lease_open_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace
    from unittest.mock import Mock

    from astra_voice.worker import meeting_diar_client as client

    disk = make_disk(tmp_path)
    parent, child = Mock(), Mock()
    child.fileno.return_value = 42
    process = SimpleNamespace(pid=2147483647, returncode=0, poll=lambda: 0, wait=lambda **kw: 0)
    duplicates = []

    def spawn(*args: Any, **kwargs: Any) -> Any:
        assert kwargs["pass_fds"][0] == 42
        duplicates.append(os.dup(kwargs["pass_fds"][1]))
        return process

    monkeypatch.setattr(socket, "socketpair", lambda *args: (parent, child))
    monkeypatch.setattr(subprocess, "Popen", spawn)
    monkeypatch.setattr(client.DiarProcess, "wait", lambda *args, **kwargs: {})
    monkeypatch.setattr(client.DiarProcess, "sample_rss", lambda *args: None)
    with disk.writer(MID, Writer.DIAR) as owner:
        role = client.DiarProcess(request(), lambda: False, lease_fd=owner.diar_lease_fd())
    try:
        with pytest.raises(MeetingStoreError), disk.writer(MID, Writer.DIAR):
            pass
    finally:
        role.close(completed=True)
        for fd in duplicates:
            os.close(fd)
    with disk.writer(MID, Writer.DIAR):
        pass


def test_subprocess_r2_a1_stopped_real_diar_holds_lease_after_parent_sigkill(
    tmp_path: Path,
) -> None:
    import ctypes
    import select
    import signal
    import subprocess
    import sys

    from astra_voice.meetings.events import MeetingState
    from astra_voice.meetings.model import Meeting
    from helpers.meeting_audio import bursts, write_track

    disk = make_disk(tmp_path)
    write_track(disk, "mic", bursts([0.5]))
    helper = Path(__file__).resolve().parents[1] / "helpers/meeting_transcribe_parent_peer.py"
    # Adopt/reap exactly our own orphan after killing its parent. Restore the
    # previous setting so other tests do not inherit a changed runner policy.
    libc = ctypes.CDLL(None, use_errno=True)
    previous = ctypes.c_int()
    assert libc.prctl(37, ctypes.byref(previous), 0, 0, 0) == 0  # GET_CHILD_SUBREAPER
    assert libc.prctl(36, 1, 0, 0, 0) == 0  # SET_CHILD_SUBREAPER
    parent = None
    child_pid = None
    try:
        parent = subprocess.Popen(
            [sys.executable, "-B", str(helper), str(disk.root)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert parent.stdout is not None
        assert select.select([parent.stdout], [], [], 15)[0], (
            "transcriber did not publish child pid"
        )
        child_pid = int(parent.stdout.readline())
        os.kill(child_pid, signal.SIGSTOP)
        until = time.monotonic() + 5
        while time.monotonic() < until:
            status = Path(f"/proc/{child_pid}/status").read_text()
            if "State:\tT" in status:
                break
            time.sleep(0.01)
        else:
            pytest.fail("child did not stop")
        parent.kill()
        assert parent.wait(timeout=5) == -signal.SIGKILL
        os.kill(child_pid, 0)  # child still alive and its SIGTERM is pending
        # Another diar role cannot acquire this meeting's lifetime lease.
        with pytest.raises(MeetingStoreError), disk.writer(MID, Writer.DIAR):
            pass
        meeting = disk.read(MID, "meeting.json")
        assert isinstance(meeting, Meeting)
        meeting.state = MeetingState.DELETING
        disk.write(MID, "meeting.json", meeting, writer=Writer.GUI)
        disk.begin_delete(MID)
        with pytest.raises(MeetingStoreError):
            disk.purge_deleted(MID)
        os.kill(child_pid, signal.SIGKILL)
        assert os.waitpid(child_pid, 0)[0] == child_pid
        child_pid = None
        disk.purge_deleted(MID)
        assert not (disk.root / f".deleting-{MID}").exists()
    finally:
        if parent is not None:
            if parent.poll() is None:
                parent.kill()
            parent.wait(timeout=5)
            for stream in (parent.stdin, parent.stdout, parent.stderr):
                if stream is not None:
                    stream.close()
        if child_pid is not None:
            try:
                os.kill(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            os.waitpid(child_pid, 0)
        assert libc.prctl(36, previous.value, 0, 0, 0) == 0
