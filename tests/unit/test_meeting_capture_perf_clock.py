"""Capture regressions from paced synthetic S4 artifacts; no audio service."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from test_meeting_capture import Rig, Source, wait_until

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("filesystem_euid")]


def test_first_pcm_owns_epoch_without_empty_startup_segment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Source, "sample_clock", lambda self: ("monotonic", 100000, 100000))
    rig = Rig(tmp_path)
    try:
        rig.prepare("none")
        rig.open()
        archive = rig.state.archive
        assert archive is not None
        # Native timing is unavailable at open; first actual PCM supplies Pulse time.
        assert archive.put("pcm", ("mic", b"\0" * 3200, ("pulse", 100, 100100), 100))
        wait_until(lambda: archive.pending_bytes == 0)
        rig.stop()
        paths = sorted((rig.directory / "audio").glob("mic-*.json"))
        assert len(paths) == 1
        sidecar = json.loads(paths[0].read_text())
        assert sidecar["frames"] == 1600 and not sidecar["discontinuity"]
        assert sidecar["timeline"] == [[0, 100000], [1600, 100100]]
    finally:
        rig.cleanup()


def test_open_close_without_pcm_keeps_journal_not_empty_wav(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    try:
        rig.prepare("none")
        rig.open()
        rig.stop()
        assert list((rig.directory / "audio").iterdir()) == []
        journal = json.loads((rig.directory / "capture.json").read_text())
        assert [edge["kind"] for edge in journal["boundaries"]] == ["open", "close"]
    finally:
        rig.cleanup()


def test_replay_96_second_boundary_small_backward_latency_update(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    try:
        rig.prepare("none")
        rig.open()
        archive = rig.state.archive
        assert archive is not None
        origin = 1623124357
        # Reproduce last endpoint of mic-0001 (96,240 ms of PCM) and next
        # 20 ms chunk in mic-0002: start 1623220574 + 20 = 1623220594,
        # three milliseconds behind the preceding endpoint 1623220597.
        for end in range(1000, 96001, 1000):
            assert archive.put("pcm", ("mic", b"\0" * 32000, ("pulse", end, origin + end), 0))
            wait_until(lambda: archive.pending_bytes == 0)
        assert archive.put("pcm", ("mic", b"\0" * 7680, ("pulse", 96240, 1623220597), 0))
        wait_until(lambda: archive.pending_bytes == 0)
        assert archive.put("pcm", ("mic", b"\0" * 640, ("pulse", 96239, 1623220594), 0))
        wait_until(lambda: archive.pending_bytes == 0)
        rig.stop()
        sidecars = [json.loads(p.read_text()) for p in (rig.directory / "audio").glob("mic-*.json")]
        assert len(sidecars) == 1
        assert not sidecars[0]["discontinuity"]
        assert sidecars[0]["frames"] == 1540160
        assert sidecars[0]["timeline"][-1][0] == 1540160
    finally:
        rig.cleanup()


def test_checkpoint_endpoint_maps_all_durable_samples_without_per_pcm_json(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    try:
        rig.prepare("none")
        rig.open()
        archive = rig.state.archive
        assert archive is not None
        for end in range(100, 8101, 100):
            jitter = -80 if end == 7000 else (40 if end >= 8000 else 0)
            assert archive.put(
                "pcm", ("mic", b"\0" * 3200, ("pulse", end, 100000 + end + jitter), 0)
            )
            wait_until(lambda: archive.pending_bytes == 0)
        sidecar: dict[str, Any] = json.loads((rig.directory / "audio/mic-0000.json").read_text())
        assert sidecar["frames_at_checkpoint"] == 128000
        assert sidecar["timeline"][-1] == [128000, 108040]
        assert not sidecar["closed"]
        rig.stop()
    finally:
        rig.cleanup()


def test_keepalive_reuses_checked_schema_but_validates_every_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jsonschema import Draft202012Validator

    from astra_voice.worker import ipc

    rig = Rig(tmp_path)
    try:
        rig.prepare("none")
        rig.open()
        rig.send("keepalive", seq=1)
        checks = 0
        original = Draft202012Validator.check_schema

        def checked(*args: Any, **kwargs: Any) -> None:
            nonlocal checks
            checks += 1
            original(*args, **kwargs)

        monkeypatch.setattr(Draft202012Validator, "check_schema", checked)
        for seq in range(2, 12):
            rig.send("keepalive", seq=seq)
        with pytest.raises(ipc.FrameError):
            rig.send("keepalive", seq="invalid")
        assert checks == 0, "constant schemas must not be meta-validated for each keepalive"
        rig.stop()
    finally:
        rig.cleanup()


def test_schema_cache_observes_nested_mutation_and_rejects_bad_schemas() -> None:
    from astra_voice.models.schema import SchemaInvalid, validate

    schema: dict[str, Any] = {
        "type": "object",
        "properties": {"n": {"type": "integer"}},
        "additionalProperties": False,
    }
    validate({"n": 1}, schema)
    schema["properties"]["n"]["type"] = "string"
    with pytest.raises(SchemaInvalid):
        validate({"n": 1}, schema)
    validate({"n": "text"}, schema)
    with pytest.raises(SchemaInvalid):
        validate({"unexpected": 1}, schema)
    schema["properties"]["n"]["type"] = "not-a-json-type"
    with pytest.raises(SchemaInvalid):
        validate({}, schema)


def test_schema_cache_is_bounded_and_resolvers_are_not_shared() -> None:
    from concurrent.futures import ThreadPoolExecutor

    from astra_voice.models.schema import _check_schema, validate

    def check(index: int) -> None:
        validate(index, {"type": "integer", "const": index})

    with ThreadPoolExecutor(max_workers=3) as executor:
        list(executor.map(check, range(140)))
    assert _check_schema.cache_info().currsize <= 128


def test_pcm_writes_are_bounded_by_checkpoints_not_fragment_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from astra_voice.meetings.model import JsonModel
    from astra_voice.meetings.store import MeetingWriter

    writes: list[str] = []
    original = MeetingWriter.write

    def write(owner: MeetingWriter, filename: str, document: JsonModel) -> None:
        writes.append(filename)
        original(owner, filename, document)

    rig = Rig(tmp_path)
    monkeypatch.setattr(MeetingWriter, "write", write)
    try:
        rig.prepare("none")
        rig.open()
        archive = rig.state.archive
        assert archive is not None
        for end in range(100, 2501, 100):
            assert archive.put("pcm", ("mic", b"\0" * 3200, ("pulse", end, 100000 + end), end))
            wait_until(lambda: archive.pending_bytes == 0)
        assert writes.count("audio/mic-0000.json") == 1
        rig.stop()
        assert writes.count("audio/mic-0000.json") == 2
        assert writes.count("capture.json") == 2
        sidecar = json.loads((rig.directory / "audio/mic-0000.json").read_text())
        assert sidecar["recorded_ms_at_open"] == 0
    finally:
        rig.cleanup()


def test_backward_jitter_at_rotation_keeps_both_endpoints_monotonic(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    try:
        rig.prepare("none")
        rig.open()
        archive = rig.state.archive
        assert archive is not None
        for end in range(1000, 299001, 1000):
            assert archive.put("pcm", ("mic", b"\0" * 32000, ("pulse", end, 100000 + end), 0))
            wait_until(lambda: archive.pending_bytes == 0)
        assert archive.put("pcm", ("mic", b"\0" * 31680, ("pulse", 299990, 399990), 0))
        wait_until(lambda: archive.pending_bytes == 0)
        archive.segments["mic"][2].timeline.append([4799840, 399990])
        assert archive.put("pcm", ("mic", b"\0" * 640, ("pulse", 300010, 399987), 0))
        wait_until(lambda: archive.pending_bytes == 0)
        assert not archive.failed.is_set()
        rig.stop()
        paths = sorted((rig.directory / "audio").glob("mic-*.json"))
        assert len(paths) == 2
        first, second = [json.loads(p.read_text()) for p in paths]
        assert first["frames"] == 4800000 and second["frames"] == 160
        assert first["epoch_id"] == second["epoch_id"]
        for segment in (first, second):
            assert segment["timeline"][-1][0] == segment["sample_end"]
            assert all(
                a[1] < b[1]
                for a, b in zip(segment["timeline"], segment["timeline"][1:], strict=False)
            )
    finally:
        rig.cleanup()


def test_schema_cache_does_not_normalize_invalid_python_schema() -> None:
    from astra_voice.models.schema import SchemaInvalid, validate

    validate({"n": 1}, {"type": "object", "required": ["n"]})
    # JSON encoding alone would normalize this invalid tuple into the cached list.
    with pytest.raises(SchemaInvalid):
        validate({"n": 1}, {"type": "object", "required": ("n",)})


def test_small_clock_corrections_cannot_defer_pcm_durability_budget(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    try:
        rig.prepare("none")
        rig.open()
        archive = rig.state.archive
        assert archive is not None
        assert archive.put("pcm", ("mic", b"\0" * 3200, ("pulse", 100, 100100), 100))
        wait_until(lambda: archive.pending_bytes == 0)
        # Repeated small corrections stay inside the fragment jitter threshold;
        # checkpointing must still bound the uncommitted PCM, independently of
        # the corrected clock's progress.
        for index in range(120):
            jitter = index % 2
            stamp = ("pulse", 100 - jitter, 100100 - jitter)
            assert archive.put("pcm", ("mic", b"\0" * 3200, stamp, (index + 2) * 100))
            wait_until(lambda: archive.pending_bytes == 0)
        sidecar = archive.segments["mic"][2]
        assert sidecar.frames - sidecar.frames_at_checkpoint <= 128000
        assert not archive.failed.is_set()
        rig.stop()
    finally:
        rig.cleanup()


@pytest.mark.parametrize("far_ended", [False, True], ids=["mic-only", "far-ended"])
def test_each_active_track_keeps_two_second_queue_credit_during_fsync(
    tmp_path: Path, far_ended: bool
) -> None:
    import os
    import threading
    import time

    blocked, release = threading.Event(), threading.Event()
    syncs = 0

    def fsync(fd: int) -> None:
        nonlocal syncs
        if os.readlink(f"/proc/self/fd/{fd}").endswith("mic-0000.wav"):
            syncs += 1
            if syncs == 2:
                blocked.set()
                assert release.wait(10)
        os.fsync(fd)

    rig = Rig(tmp_path, fsync=fsync)
    try:
        rig.prepare("app_stream" if far_ended else "none")
        rig.open()
        archive = rig.state.archive
        assert archive is not None
        if far_ended:
            source = rig.backend.sources[0]
            source.ended = True
            source.chunks.append(None)
            wait_until(lambda: not source.opened)
            rig.poll()
            assert rig.state.track_states["far"] == "ended"
        # Same slow-clock input as the independent probe: durable initial20ms,
        # then exactly8s of PCM stuck in the second WAV fsync.
        for index in range(401):
            stamp = ("pulse", 100 + index, 100100 + index)
            assert archive.put("pcm", ("mic", b"\0" * 640, stamp, index * 20))
            if index < 400:
                wait_until(lambda: archive.pending_bytes == 0)
        assert blocked.wait(2)
        accepted = 0
        while archive.put("pcm", ("mic", b"\0" * 640, ("pulse", 500, 100500), 9000)):
            accepted += 1
        sidecar = archive.segments["mic"][2]
        written_unsafe = (sidecar.frames - sidecar.frames_at_checkpoint) / 16000
        queued_unwritten = (archive.pending_bytes - 640) / 32000
        assert written_unsafe == 8
        assert archive.pending_bytes == 64000 and accepted == 99
        assert written_unsafe + queued_unwritten <= 10
        # Exhausting PCM credit must not prevent physical close or its control job.
        started = time.monotonic()
        rig.send("pause", reason="user")
        rig.until("meeting.capture.closed")
        assert time.monotonic() - started < 1
        assert not rig.mics[0].opened
        release.set()
        rig.until("meeting.capture.finalized")
        assert archive.pending_bytes == 0
        assert archive.pending_track_bytes == {"mic": 0, "far": 0}
    finally:
        release.set()
        rig.cleanup()


def test_two_track_queue_credits_are_independent_and_close_bypasses_pcm_budget(
    tmp_path: Path,
) -> None:
    import threading

    blocked, release = threading.Event(), threading.Event()

    def fsync(fd: int) -> None:
        blocked.set()
        assert release.wait(10)

    rig = Rig(tmp_path, fsync=fsync)
    try:
        rig.prepare()
        rig.open()
        assert blocked.wait(2)
        archive = rig.state.archive
        assert archive is not None
        for track in ("mic", "far"):
            assert archive.put("pcm", (track, b"\0" * 64000, ("pulse", 2000, 102000), 2000))
            assert not archive.put("pcm", (track, b"\0" * 640, ("pulse", 2020, 102020), 2020))
        assert archive.pending_bytes == 128000
        assert archive.pending_track_bytes == {"mic": 64000, "far": 64000}
        rig.send("pause", reason="user")
        rig.until("meeting.capture.closed")
        assert not rig.mics[0].opened and not rig.backend.sources[0].opened
        release.set()
        rig.until("meeting.capture.finalized")
        assert archive.pending_bytes == 0
        assert archive.pending_track_bytes == {"mic": 0, "far": 0}
    finally:
        release.set()
        rig.cleanup()


@pytest.mark.parametrize("correction_ms", [-14, 11])
def test_exact_rotation_preserves_shared_sample_clock(tmp_path: Path, correction_ms: int) -> None:
    """T-453: a segment boundary is one sample, not a new clock observation.

    Replay corrections seen at mic-0000/0001 and mic-0007/0008 in the
    60-minute stand. The next read may adjust its endpoint, but must not
    retroactively move the previous segment's final sample in the same epoch.
    """
    rig = Rig(tmp_path)
    try:
        rig.prepare("none")
        rig.open()
        archive = rig.state.archive
        assert archive is not None
        for end in range(1000, 300001, 1000):
            assert archive.put("pcm", ("mic", b"\0" * 32000, ("pulse", end, 100000 + end), 0))
            wait_until(lambda: archive.pending_bytes == 0)
        assert archive.put(
            "pcm", ("mic", b"\0" * 640, ("pulse", 300020, 400020 + correction_ms), 0)
        )
        wait_until(lambda: archive.pending_bytes == 0)
        rig.stop()
        first, second = [
            json.loads(path.read_text())
            for path in sorted((rig.directory / "audio").glob("mic-*.json"))
        ]
        assert first["epoch_id"] == second["epoch_id"]
        assert first["sample_end"] == second["sample_start"] == 4800000
        assert first["timeline"][-1] == second["timeline"][0]
        assert second["clock_anchor"] == {"server_ms": 300000, "t_mono_ms": 400000}
        assert second["timeline"][-1] == [4800320, 400020 + correction_ms]
    finally:
        rig.cleanup()
