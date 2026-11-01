"""Offline measurement evidence: no audio servers, real tiny PCM files."""

from __future__ import annotations

import array
import json
import math
import os
import struct
import sys
import wave
from pathlib import Path
from typing import Any

import pytest
from tools import meeting_stand_metrics as module

pytestmark = pytest.mark.unit


def stat_line(ticks: int = 10, start: int = 100) -> str:
    fields = ["0"] * 22
    fields[0], fields[1] = "S", "1"
    fields[11], fields[12], fields[19] = str(ticks), "0", str(start)
    return "42 (capture (worker)) " + " ".join(fields)


def process(root: Path, *, ticks: int = 10, start: int = 100, rss: int = 1024) -> None:
    directory = root / "42"
    directory.mkdir(exist_ok=True)
    (directory / "stat").write_text(stat_line(ticks, start))
    (directory / "status").write_text(f"VmRSS:\t{rss} kB\n")
    (directory / "limits").write_text(
        "Max core file size        0              0             bytes\n"
        "Max address space         536870912      unlimited     bytes\n"
    )
    (directory / "oom_score_adj").write_text("500\n")


def recording(
    root: Path,
    track: str,
    *,
    shift_ms: int = 0,
    beeps: tuple[int, ...] = (100, 1100),
    discontinuity: bool = False,
    closed: bool = True,
    checkpoint: int = 32000,
    epoch: str = "epoch1",
    sample_start: int = 16000,
) -> None:
    root.mkdir(exist_ok=True)
    samples = array.array("h", [0] * 32000)
    for onset in beeps:
        for offset in range(1600):
            samples[onset * 16 + offset] = round(16000 * math.sin(2 * math.pi * offset / 16))
    if sys.byteorder != "little":
        samples.byteswap()
    path = root / f"{track}-0000.wav"
    with wave.open(str(path), "wb") as stream:
        stream.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        stream.writeframes(samples.tobytes())
    side: dict[str, Any] = {
        "meeting_id": "2026-10-31_120000-ab12",
        "schema": 1,
        "track": track,
        "rate": 16000,
        "channels": 1,
        "width": 2,
        "index": 0,
        "frames": 32000,
        "frames_at_checkpoint": checkpoint,
        "sample_start": sample_start,
        "sample_end": sample_start + 32000,
        "epoch_id": epoch,
        "clock": "pulse",
        "closed": closed,
        "opened_t_mono_ms": 10000 + shift_ms,
        "clock_anchor": {"server_ms": 3000, "t_mono_ms": 10000 + shift_ms},
        "timeline": [[sample_start, 10000 + shift_ms], [sample_start + 32000, 12000 + shift_ms]],
        "discontinuity": discontinuity,
    }
    path.with_suffix(".json").write_text(json.dumps(side))


def statuses(report: dict[str, Any]) -> dict[str, str]:
    return {row["name"]: row["status"] for row in report["criteria"]}


def second_epoch(
    root: Path, track: str, *, shift_ms: int = 0, beeps: tuple[int, ...] = (100, 1100)
) -> None:
    temporary = root / f"{track}-fixture"
    recording(
        temporary,
        track,
        shift_ms=2000 + shift_ms,
        beeps=beeps,
        epoch=f"{track}-second",
        sample_start=0,
    )
    source = temporary / f"{track}-0000.wav"
    side = json.loads(source.with_suffix(".json").read_text())
    side["index"] = 1
    source.replace(root / f"{track}-0001.wav")
    (root / f"{track}-0001.json").write_text(json.dumps(side))


def test_beep_drift_is_within_epoch_pairs_and_reports_boundary_jump(tmp_path: Path) -> None:
    for track in ("mic", "far"):
        recording(tmp_path, track)
        second_epoch(tmp_path, track, shift_ms=200 if track == "mic" else 0)
    report = module.analyze_recording(
        tmp_path,
        expected_beeps_ms=[10100, 11100, 12100, 13100],
        intervals=[
            {"t_mono_ms": 10000, "recorded_ms": 0},
            {"t_mono_ms": 15000, "recorded_ms": 5000},
        ],
    )
    assert report["beep_drift_ms"] == pytest.approx(0, abs=0.1)
    assert report["epoch_jumps"][0]["offset_change_ms"] == pytest.approx(200, abs=0.1)
    assert statuses(report)["timecodes"] == "pass"


def test_unmapped_epoch_is_unavailable_with_specific_detail(tmp_path: Path) -> None:
    for track in ("mic", "far"):
        recording(tmp_path, track)
        second_epoch(tmp_path, track)
    path = tmp_path / "mic-0001.json"
    side = json.loads(path.read_text())
    side["timeline"] = [side["timeline"][0]]
    path.write_text(json.dumps(side))
    report = module.analyze_recording(tmp_path, expected_beeps_ms=[10100, 11100, 12100, 13100])
    detail = next(c["detail"] for c in report["criteria"] if c["name"] == "beep_drift")
    assert statuses(report)["beep_drift"] == "unavailable"
    assert "unmapped-epoch" in detail


def test_known_150ms_beep_with_43ms_hole_is_not_two_markers(tmp_path: Path) -> None:
    for track in ("mic", "far"):
        recording(tmp_path, track)
        path = tmp_path / f"{track}-0000.wav"
        with wave.open(str(path), "rb") as stream:
            raw = bytearray(stream.readframes(32000))
        for offset in range(2400):
            value = (
                0 if 1280 <= offset < 1968 else round(16000 * math.sin(2 * math.pi * offset / 16))
            )
            struct.pack_into("<h", raw, (1600 + offset) * 2, value)
        with wave.open(str(path), "wb") as stream:
            stream.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
            stream.writeframes(raw)
    report = module.analyze_recording(tmp_path, expected_beeps_ms=[10100, 11100])
    assert all(len(values) == 2 for values in report["beeps_mono_ms"].values())
    assert statuses(report)["beep_drift"] == "pass"


def test_one_beep_in_each_epoch_cannot_prove_drift(tmp_path: Path) -> None:
    for track in ("mic", "far"):
        recording(tmp_path, track, beeps=(100,))
        second_epoch(tmp_path, track, beeps=(100,))
    report = module.analyze_recording(tmp_path, expected_beeps_ms=[10100, 12100])
    assert statuses(report)["beep_drift"] == "unavailable"
    detail = next(c["detail"] for c in report["criteria"] if c["name"] == "beep_drift")
    assert "insufficient-within-epoch-beeps" in detail


@pytest.mark.parametrize("boundary_gap", [0, 1000])
def test_marker_split_at_epoch_counter_reset_requires_adjacent_clock_edges(
    tmp_path: Path, boundary_gap: int
) -> None:
    for track in ("mic", "far"):
        recording(tmp_path, track, beeps=(100, 1900))
        second_epoch(tmp_path, track, shift_ms=boundary_gap, beeps=(0, 1100))
        path = tmp_path / f"{track}-0001.wav"
        with wave.open(str(path), "rb") as stream:
            raw = bytearray(stream.readframes(32000))
        raw[800 * 2 : 1600 * 2] = bytes(800 * 2)  # Remaining 50ms of one 150ms pulse.
        with wave.open(str(path), "wb") as stream:
            stream.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
            stream.writeframes(raw)
    report = module.analyze_recording(tmp_path, expected_beeps_ms=[10100, 11900, 13100])
    if boundary_gap:
        assert statuses(report)["beep_drift"] == "unavailable"
        assert all(len(values) == 4 for values in report["beeps_mono_ms"].values())
    else:
        assert statuses(report)["beep_drift"] == "pass"
        assert all(len(values) == 3 for values in report["beeps_mono_ms"].values())


@pytest.mark.usefixtures("filesystem_euid")
def test_disk_excludes_only_private_capture_atomic_temporaries(tmp_path: Path) -> None:
    (tmp_path / "mic-0000.wav").write_bytes(b"x" * 100)
    temporary = tmp_path / (".mic-0000-" + "a" * 32 + ".tmp")
    temporary.write_bytes(b"t" * 500)
    temporary.chmod(0o600)
    (tmp_path / "other.tmp").write_bytes(b"u" * 7)
    assert module._disk_bytes(tmp_path) == 107


def test_disk_atomic_temporary_disappearing_during_walk_is_not_missing_sample(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "mic-0000.wav").write_bytes(b"x" * 100)
    monkeypatch.setattr(
        os,
        "walk",
        lambda *a, **k: iter(
            [(str(tmp_path), [], ["mic-0000.wav", ".mic-0000-" + "a" * 32 + ".tmp"])]
        ),
    )
    assert module._disk_bytes(tmp_path) == 100


def test_disk_unavailable_explains_missing_samples(tmp_path: Path) -> None:
    process(tmp_path)
    sampler = module.ProcSampler(tmp_path, hz=100)
    rows = [sampler.sample({"capture": [42]}, now_s=t, disk_root=tmp_path) for t in (0.0, 3600.0)]
    rows[1]["disk_bytes"] = None
    report = module.summarize_resources(rows)
    disk = next(c for c in report["criteria"] if c["name"] == "disk")
    assert disk["status"] == "unavailable"
    assert "missing-disk-samples" in disk["detail"]


def test_proc_parser_handles_parentheses_and_rejects_short_stat() -> None:
    parsed = module.parse_proc_stat(stat_line())
    assert (parsed.pid, parsed.ticks, parsed.start_ticks) == (42, 10, 100)
    with pytest.raises(ValueError):
        module.parse_proc_stat("42 (bad) S")


def test_sampler_time_weighted_one_core_cpu_rss_and_disk(tmp_path: Path) -> None:
    proc, disk = tmp_path / "proc", tmp_path / "disk"
    proc.mkdir()
    disk.mkdir()
    process(proc)
    sampler = module.ProcSampler(proc, hz=100)
    rows = [sampler.sample({"capture": [42]}, now_s=0.0, disk_root=disk)]
    process(proc, ticks=12, rss=2048)
    (disk / "pcm").write_bytes(b"x" * 1024)
    rows.append(sampler.sample({"capture": [42]}, now_s=1.0, disk_root=disk))
    process(proc, ticks=42, rss=3072)
    (disk / "pcm").write_bytes(b"x" * 4096)
    rows.append(sampler.sample({"capture": [42]}, now_s=4.0, disk_root=disk))
    report = module.summarize_resources(rows)
    assert report["capture_cpu_mean_pct"] == pytest.approx(8.0)
    assert report["capture_cpu_peak_pct"] == pytest.approx(10.0)
    assert report["rss_peak_mib"] == 3.0
    assert report["growth_mib_per_hour"] == pytest.approx(1800.0)
    assert report["disk_mib_per_hour"] == pytest.approx(4096 / 1024**2 * 900)
    assert statuses(report)["capture_cpu"] == "fail"
    assert report["projected"] is True
    assert report["soak_criterion"]["status"] == "unavailable"


def test_pid_reuse_missing_rss_and_single_sample_cannot_pass(tmp_path: Path) -> None:
    process(tmp_path)
    sampler = module.ProcSampler(tmp_path, hz=100)
    first = sampler.sample({"capture": [42]}, now_s=0.0, disk_root=tmp_path)
    process(tmp_path, start=200, ticks=9999)
    (tmp_path / "42/status").write_text("Threads: 1\n")
    second = sampler.sample({"capture": [42]}, now_s=1.0, disk_root=tmp_path)
    assert module.summarize_resources([first])["capture_cpu_mean_pct"] is None
    report = module.summarize_resources([first, second])
    assert report["capture_cpu_mean_pct"] is None
    assert statuses(report)["capture_rss"] == "unavailable"
    assert statuses(report)["capture_rss_growth"] == "unavailable"


def test_missing_proc_is_unavailable_not_zero(tmp_path: Path) -> None:
    sampler = module.ProcSampler(tmp_path, hz=100)
    rows = [sampler.sample({"capture": [42]}, now_s=t, disk_root=tmp_path) for t in (0.0, 2.0)]
    report = module.summarize_resources(rows)
    assert report["capture_cpu_mean_pct"] is None
    assert statuses(report)["capture_role_limits"] == "unavailable"


def test_clock_mapping_uses_absolute_samples_and_rejects_gaps(tmp_path: Path) -> None:
    recording(tmp_path, "mic")
    side = json.loads((tmp_path / "mic-0000.json").read_text())
    assert module.sample_to_monotonic(side, 17600) == pytest.approx(10100)
    for sample in (15999, 48001):
        with pytest.raises(ValueError):
            module.sample_to_monotonic(side, sample)
    side["discontinuity"] = True
    assert module.sample_to_monotonic(side, 17600) == pytest.approx(10100)
    with pytest.raises(ValueError):
        module.sample_to_monotonic(side, 48001)


def test_beep_drift_uses_timeline_epochs_and_constant_offset(tmp_path: Path) -> None:
    recording(tmp_path, "mic", shift_ms=25, epoch="mic-epoch", sample_start=0)
    recording(tmp_path, "far", epoch="far-epoch", sample_start=900000)
    report = module.analyze_recording(
        tmp_path,
        expected_beeps_ms=[10100.0, 11100.0],
        intervals=[
            {"t_mono_ms": 10000, "recorded_ms": 0},
            {"t_mono_ms": 13000, "recorded_ms": 3000},
        ],
    )
    assert report["beep_drift_ms"] == pytest.approx(0.0, abs=0.1)
    assert report["beep_initial_offset_ms"] == pytest.approx(25.0, abs=0.1)
    assert statuses(report)["beep_drift"] == "pass"
    assert statuses(report)["timecodes"] == "pass"
    assert report["latest_far_mono_ms"] == 12000


def test_beep_drift_over_50ms_fails(tmp_path: Path) -> None:
    recording(tmp_path, "mic", beeps=(100, 1160))
    recording(tmp_path, "far")
    report = module.analyze_recording(tmp_path, expected_beeps_ms=[10100.0, 11100.0])
    assert report["beep_drift_ms"] == pytest.approx(60.0, abs=0.1)
    assert statuses(report)["beep_drift"] == "fail"


def test_missing_beep_is_unavailable(tmp_path: Path) -> None:
    recording(tmp_path, "mic", beeps=(100,))
    recording(tmp_path, "far")
    report = module.analyze_recording(tmp_path, expected_beeps_ms=[10100.0, 11100.0])
    assert statuses(report)["beep_drift"] == "unavailable"
    assert statuses(report)["timecodes"] == "unavailable"
    assert report["beep_diagnostics"]["status"] == "unavailable"


def test_no_files_and_no_crash_time_never_pass(tmp_path: Path) -> None:
    report = module.analyze_recording(tmp_path)
    assert module.verdict(report["criteria"]) == 2
    assert "crash_loss" not in statuses(report)


def test_crash_loss_uses_checkpoint_not_physical_tail(tmp_path: Path) -> None:
    recording(tmp_path, "mic", closed=False, checkpoint=16000)
    recording(tmp_path, "far", closed=False, checkpoint=16000)
    report = module.analyze_recording(tmp_path, crash_mono_ms=22000.0)
    assert report["crash_loss_ms"] == pytest.approx(11000.0)
    assert statuses(report)["crash_loss"] == "fail"
    assert statuses(report)["segments"] == "pass"


def test_truncated_wav_is_failure_not_pass(tmp_path: Path) -> None:
    recording(tmp_path, "mic")
    recording(tmp_path, "far")
    path = tmp_path / "mic-0000.wav"
    path.write_bytes(path.read_bytes()[:-12])
    assert statuses(module.analyze_recording(tmp_path))["segments"] == "fail"


@pytest.mark.parametrize("empty_pcm", [True, False])
def test_crash_requires_positive_durable_pcm_on_both_tracks(
    tmp_path: Path, empty_pcm: bool
) -> None:
    for track in ("mic", "far"):
        recording(tmp_path, track, closed=False, checkpoint=0)
        if empty_pcm:
            path = tmp_path / f"{track}-0000.wav"
            with wave.open(str(path), "wb") as stream:
                stream.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
            side = json.loads(path.with_suffix(".json").read_text())
            side.update(
                frames=0,
                sample_start=0,
                sample_end=0,
                timeline=[[0, 10000]],
            )
            path.with_suffix(".json").write_text(json.dumps(side))
    report = module.analyze_recording(tmp_path, crash_mono_ms=10001.0)
    assert report["crash_loss_ms"] is None
    assert module.verdict(report["criteria"]) != 0


@pytest.mark.parametrize("invalid", ["tail", "origin", "pause-continuity"])
def test_timecodes_validate_all_boundaries_and_recorded_origin(
    tmp_path: Path, invalid: str
) -> None:
    recording(tmp_path, "mic")
    recording(tmp_path, "far")
    edges: list[dict[str, Any]] = [
        {"t_mono_ms": 10000, "recorded_ms": 0},
        {"t_mono_ms": 13000, "recorded_ms": 3000},
    ]
    if invalid == "tail":
        edges += [{"t_mono_ms": "bad", "recorded_ms": None}, {"t_mono_ms": 0, "recorded_ms": 0}]
    elif invalid == "origin":
        for edge in edges:
            edge["recorded_ms"] += 50000
    else:
        edges += [
            {"t_mono_ms": 14000, "recorded_ms": 4000},
            {"t_mono_ms": 15000, "recorded_ms": 5000},
        ]
    report = module.analyze_recording(
        tmp_path,
        expected_beeps_ms=[10100.0, 11100.0],
        intervals=edges,
    )
    assert statuses(report)["timecodes"] != "pass"
    assert module.verdict(report["criteria"]) != 0


def test_criterion_and_verdict_do_not_inflate_missing_evidence() -> None:
    passed = module.criterion("x", 1.0, 2.0, "ms")
    failed = module.criterion("x", 3.0, 2.0, "ms")
    missing = module.criterion("x", None, 2.0, "ms")
    assert module.verdict([passed]) == 0
    assert module.verdict([passed, missing]) == 2
    assert module.verdict([failed, missing]) == 1
    assert module.verdict([]) == 2
    assert module.criterion("nan", float("nan"), 2.0, "ms")["status"] == "unavailable"


def startup_placeholder(root: Path, track: str, *, closed: bool = True) -> None:
    """Source-derived lazy-clock transition seen in the 2026-11-01 sidecars."""
    path = root / f"{track}-0000.wav"
    path.rename(root / f"{track}-0001.wav")
    side_path = path.with_suffix(".json")
    side = json.loads(side_path.read_text())
    side["index"] = 1
    # Pulse stream time zero is a valid anchor, not missing timing evidence.
    side["clock_anchor"]["server_ms"] = 0
    (root / f"{track}-0001.json").write_text(json.dumps(side))
    placeholder = dict(side)
    opened = side["opened_t_mono_ms"] + 50
    placeholder.update(
        index=0,
        frames=0,
        frames_at_checkpoint=0,
        sample_start=0,
        sample_end=0,
        opened_t_mono_ms=opened,
        clock="monotonic",
        clock_anchor={"server_ms": opened, "t_mono_ms": opened},
        timeline=[[0, opened]],
        discontinuity=True,
        close_reason="clock_change",
        closed=closed,
    )
    side_path.write_text(json.dumps(placeholder))
    with wave.open(str(path), "wb") as stream:
        stream.setparams((1, 2, 16000, 0, "NONE", "not compressed"))


def test_startup_placeholders_do_not_hide_clocked_pcm(tmp_path: Path) -> None:
    for track in ("mic", "far"):
        recording(tmp_path, track)
        startup_placeholder(tmp_path, track)
    report = module.analyze_recording(
        tmp_path,
        expected_beeps_ms=[10100.0, 11100.0],
        # Real independently truncated monotonic/recorded clocks differ by 1ms.
        intervals=[
            {"t_mono_ms": 10000, "recorded_ms": 0},
            {"t_mono_ms": 13001, "recorded_ms": 3000},
        ],
    )
    assert module.verdict(report["criteria"]) == 0
    assert report["beep_drift_ms"] == pytest.approx(0.0, abs=0.1)
    assert report["timecode_error_ms"] == pytest.approx(0.0625, abs=0.1)
    assert all(
        item["clock_status"] == "empty-startup-placeholder"
        for item in report["segments"]
        if item["frames"] == 0
    )


@pytest.mark.parametrize("problem", ["nonempty-discontinuity", "unclosed", "wrong-reason"])
def test_placeholder_skip_is_restricted_to_safe_startup_headers(
    tmp_path: Path, problem: str
) -> None:
    for track in ("mic", "far"):
        recording(tmp_path, track, discontinuity=problem == "nonempty-discontinuity")
        startup_placeholder(tmp_path, track, closed=problem != "unclosed")
        if problem == "wrong-reason":
            path = tmp_path / f"{track}-0000.json"
            side = json.loads(path.read_text())
            side["close_reason"] = "lease"
            path.write_text(json.dumps(side))
    report = module.analyze_recording(
        tmp_path, expected_beeps_ms=[10100.0, 11100.0], crash_mono_ms=13000
    )
    assert module.verdict(report["criteria"]) != 0
    if problem == "nonempty-discontinuity":
        assert report["crash_loss_ms"] == 1000.0
        assert all(s.get("mapping_method") for s in report["segments"] if s["frames"])
    else:
        assert report["crash_loss_ms"] is None


def test_integer_clock_tolerance_does_not_admit_larger_boundary_error(tmp_path: Path) -> None:
    recording(tmp_path, "mic")
    recording(tmp_path, "far")
    report = module.analyze_recording(
        tmp_path,
        expected_beeps_ms=[10100.0, 11100.0],
        intervals=[
            {"t_mono_ms": 10000, "recorded_ms": 0},
            {"t_mono_ms": 13002, "recorded_ms": 3000},
        ],
    )
    assert statuses(report)["timecodes"] == "unavailable"


def test_crash_uses_last_mapped_durable_knot_without_extrapolation(tmp_path: Path) -> None:
    for track in ("mic", "far"):
        recording(tmp_path, track, closed=False)
        startup_placeholder(tmp_path, track)
    path = tmp_path / "mic-0001.json"
    side = json.loads(path.read_text())
    side["timeline"][-1] = [side["sample_start"] + 16000, 11000]
    path.write_text(json.dumps(side))
    report = module.analyze_recording(tmp_path, crash_mono_ms=13000.5)
    assert statuses(report)["crash_loss"] == "pass"
    assert report["crash_loss_ms"] == 2000.5
    assert report["crash_mono_ms"] == 13000.5
    assert report["durable_mono_ms"] == {"mic": 11000.0, "far": 12000.0}
    assert report["crash_loss_basis"] == "last-mapped-durable-knot"


@pytest.mark.parametrize("hours", [0.0, 1.0, 20.0, 20.01, -2.0])
@pytest.mark.parametrize("duration", [120, 3599, 3600])
def test_growth_short_projection_is_provisional_and_long_threshold_is_retained(
    tmp_path: Path, duration: int, hours: float
) -> None:
    process(tmp_path, rss=40000)
    sampler = module.ProcSampler(tmp_path, hz=100)
    before = sampler.sample({"capture": [42]}, now_s=0.0, disk_root=tmp_path)
    after = sampler.sample({"capture": [42]}, now_s=duration, disk_root=tmp_path)
    after["processes"][0]["rss_bytes"] += hours * module.MIB * duration / 3600
    report = module.summarize_resources([before, after])
    assert report["growth_mib_per_hour"] == pytest.approx(hours)
    if duration < 3600:
        assert "capture_rss_growth" not in statuses(report)
        assert report["growth_criterion"]["status"] == "provisional"
        assert report["growth_criterion"]["limit"] == 20.0
        # An intentionally inapplicable short growth projection does not
        # produce rc=2 when every applicable resource criterion is available.
        assert module.verdict(report["criteria"]) == 0
    else:
        expected_status = "fail" if hours > 20 else "pass"
        assert statuses(report)["capture_rss_growth"] == expected_status
        assert report["growth_criterion"]["status"] == expected_status


def test_real_short_startup_release_rate_has_no_pass_or_fail(tmp_path: Path) -> None:
    # Source-derived duration and endpoint delta of the saved 2-minute report;
    # raw /proc samples were not persisted, so this is a minimal reproduction.
    process(tmp_path, rss=40000)
    sampler = module.ProcSampler(tmp_path, hz=100)
    before = sampler.sample({"capture": [42]}, now_s=0.0, disk_root=tmp_path)
    process(tmp_path, rss=30920)
    after = sampler.sample({"capture": [42]}, now_s=120.00441616983153, disk_root=tmp_path)
    report = module.summarize_resources([before, after])
    assert report["growth_mib_per_hour"] == pytest.approx(-266.00583560878147)
    assert report["growth_criterion"]["status"] == "provisional"
    assert "capture_rss_growth" not in statuses(report)
    assert module.verdict(report["criteria"]) == 0


def exclusivity_recording(
    root: Path,
    *,
    foreign: bool = False,
    shift_ms: int = 0,
    beeps: tuple[int, ...] = (1000, 6000),
    discontinuity: bool = False,
) -> list[float]:
    """Nine seconds of known 1kHz markers, optionally mixed with a 1.7kHz sentinel."""
    recording(root, "far", discontinuity=discontinuity, shift_ms=shift_ms)
    frames = 9 * 16000
    samples = array.array("h")
    for index in range(frames):
        value = round(16000 * math.sin(2 * math.pi * 1700 * index / 16000)) if foreign else 0
        if any(onset * 16 <= index < (onset + 150) * 16 for onset in beeps):
            value += round(12000 * math.sin(2 * math.pi * 1000 * index / 16000))
        samples.append(value)
    if sys.byteorder != "little":
        samples.byteswap()
    path = root / "far-0000.wav"
    with wave.open(str(path), "wb") as stream:
        stream.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        stream.writeframes(samples.tobytes())
    side = json.loads(path.with_suffix(".json").read_text())
    side.update(
        frames=frames,
        frames_at_checkpoint=frames,
        sample_end=16000 + frames,
        timeline=[
            [16000 + second * 16000, 10000 + shift_ms + second * 1000] for second in range(10)
        ],
    )
    path.with_suffix(".json").write_text(json.dumps(side))
    return [10000.0 + onset for onset in beeps]


def test_exclusivity_real_far_beeps_have_confirmed_quiet_pcm(tmp_path: Path) -> None:
    expected = exclusivity_recording(tmp_path)
    report = module.analyze_exclusivity(tmp_path, expected_beeps_ms=expected)
    assert statuses(report)["farforeign_rms_dbfs"] == "pass"
    assert report["farforeign_rms_dbfs"] == -120.0
    assert report["quiet_duration_s"] == pytest.approx(2.35, abs=0.001)
    json.dumps(report, allow_nan=False)


def test_exclusivity_safe_startup_header_does_not_hide_quiet_pcm(tmp_path: Path) -> None:
    expected = exclusivity_recording(tmp_path)
    startup_placeholder(tmp_path, "far")
    report = module.analyze_exclusivity(tmp_path, expected_beeps_ms=expected)
    assert report["farforeign_rms_dbfs"] == -120.0
    assert statuses(report)["farforeign_rms_dbfs"] == "pass"


def test_exclusivity_continuous_foreign_tone_fails_in_inter_beep_gaps(tmp_path: Path) -> None:
    expected = exclusivity_recording(tmp_path, foreign=True)
    report = module.analyze_exclusivity(tmp_path, expected_beeps_ms=expected)
    assert statuses(report)["farforeign_rms_dbfs"] == "fail"
    assert report["farforeign_rms_dbfs"] > -10.0
    assert module.verdict(report["criteria"]) == 1


@pytest.mark.parametrize(
    "problem",
    [
        "clock-offset",
        "discontinuity",
        "no-gap",
        "missing-pcm",
        "subsecond-quiet",
        "truncated",
        "bad-anchor",
    ],
)
def test_exclusivity_requires_mapped_quiet_pcm(tmp_path: Path, problem: str) -> None:
    expected = exclusivity_recording(
        tmp_path,
        shift_ms=10000 if problem == "clock-offset" else 0,
        discontinuity=problem == "discontinuity",
        beeps=(
            (1000, 2000)
            if problem == "no-gap"
            else (1000, 4600)
            if problem == "subsecond-quiet"
            else (1000, 6000)
        ),
    )
    if problem == "missing-pcm":
        (tmp_path / "far-0000.wav").unlink()
    if problem == "truncated":
        path = tmp_path / "far-0000.wav"
        path.write_bytes(path.read_bytes()[:-8])
    if problem == "bad-anchor":
        path = tmp_path / "far-0000.json"
        side = json.loads(path.read_text())
        side["clock_anchor"]["t_mono_ms"] += 1
        path.write_text(json.dumps(side))
    report = module.analyze_exclusivity(tmp_path, expected_beeps_ms=expected)
    if problem == "discontinuity":
        assert statuses(report)["farforeign_rms_dbfs"] == "pass"
        assert report["farforeign_rms_dbfs"] <= -60
        return
    assert statuses(report)["farforeign_rms_dbfs"] == "unavailable"
    assert report["farforeign_rms_dbfs"] is None
    assert module.verdict(report["criteria"]) == 2


def test_beep_diagnostics_separates_known_hourly_drift_from_residual() -> None:
    expected = [float(i * 60000) for i in range(61)]
    mapped = {"mic": [t + 120 + i for i, t in enumerate(expected)], "far": expected}
    report = module.beep_diagnostics(expected, mapped)
    fit = report["fits"]["mic_minus_far"]
    assert fit["slope_ms_per_hour"] == pytest.approx(60)
    assert fit["span_change_ms"] == pytest.approx(60)
    assert fit["residual_sigma_ms"] == pytest.approx(0, abs=1e-9)
    assert report["raw_max_change_ms"] == pytest.approx(60)
    assert report["hourly_projection"] is False
    assert report["status"] == "diagnostic"
    assert "criteria" not in report  # a fitted slope must never replace the raw gate


def test_beep_diagnostics_symmetric_25ms_noise_has_no_trend() -> None:
    expected = [float(i * 60000) for i in range(61)]
    noise = [25.0 if i % 2 else -25.0 for i in range(61)]
    mapped = {"mic": [t + n for t, n in zip(expected, noise, strict=True)], "far": expected}
    report = module.beep_diagnostics(expected, mapped)
    fit = report["fits"]["mic_minus_far"]
    assert fit["slope_ms_per_hour"] == pytest.approx(0, abs=1e-9)
    assert fit["residual_sigma_ms"] == pytest.approx(25, abs=0.01)
    assert report["raw_max_change_ms"] == 50
    assert report["status"] == "diagnostic"


@pytest.mark.parametrize("case", ["early-step", "first-outlier", "last-outlier"])
def test_beep_diagnostics_never_hides_steps_or_endpoint_outliers(case: str) -> None:
    expected = [float(i * 60000) for i in range(61)]
    offsets = [0.0] * 61
    if case == "early-step":
        offsets[1:] = [70.0] * 60
    else:
        offsets[0 if case == "first-outlier" else -1] = 70.0
    report = module.beep_diagnostics(
        expected,
        {"mic": [t + n for t, n in zip(expected, offsets, strict=True)], "far": expected},
    )
    assert report["raw_max_change_ms"] == 70
    assert report["fits"]["mic_minus_far"]["residual_max_abs_ms"] > 50
    worst = report["worst_change"]
    assert worst["trend_contribution_ms"] + worst["residual_change_ms"] == pytest.approx(
        worst["offset_ms"] - worst["initial_offset_ms"]
    )


def test_beep_diagnostics_short_span_labels_hourly_projection() -> None:
    expected = [10000.0 + i * 5000 for i in range(21)]
    report = module.beep_diagnostics(
        expected, {"mic": [t + i / 12 for i, t in enumerate(expected)], "far": expected}
    )
    assert report["hourly_projection"] is True
    assert report["observed_span_s"] == 100
    assert report["fits"]["mic_minus_far"]["slope_ms_per_hour"] == pytest.approx(60)
    assert report["status"] == "diagnostic"


@pytest.mark.parametrize("problem", ["few", "missing", "unordered", "nonfinite", "ambiguous"])
def test_beep_diagnostics_missing_or_ambiguous_evidence_is_unavailable(problem: str) -> None:
    expected = [10000.0, 15000.0, 20000.0]
    mapped = {"mic": expected.copy(), "far": expected.copy()}
    if problem == "few":
        expected = expected[:2]
        mapped = {t: v[:2] for t, v in mapped.items()}
    elif problem == "missing":
        mapped["mic"].pop()
    elif problem == "unordered":
        mapped["mic"].reverse()
    elif problem == "nonfinite":
        mapped["mic"][0] = float("nan")
    else:
        mapped["mic"] = [t + 2500 for t in expected]  # half spacing: identity ambiguous
    report = module.beep_diagnostics(expected, mapped)
    assert report["status"] == "unavailable"
    assert not report["fits"]
    json.dumps(report, allow_nan=False)


@pytest.mark.parametrize("phase", range(16))
def test_pcm_detector_sample_resolution_is_not_20ms_window_jitter(
    tmp_path: Path, phase: int
) -> None:
    recording(tmp_path, "mic")
    onsets = [1600 + phase, 17600 + phase]
    samples = array.array("h", [0] * 32000)
    for onset in onsets:
        for i in range(1600):
            samples[onset + i] = round(12000 * math.sin(2 * math.pi * (i + phase) / 16))
    if sys.byteorder != "little":
        samples.byteswap()
    path = tmp_path / "mic-0000.wav"
    with wave.open(str(path), "wb") as wav:
        wav.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        wav.writeframes(samples.tobytes())
    _, detected = module._read_segment(path, crash=False)
    assert len(detected) == 2
    assert all(0 <= d - onset <= 1 for d, onset in zip(detected, onsets, strict=True))
    assert detected[1] - detected[0] == 16000


def test_beep_diagnostics_minute_windows_preserve_nonlinear_error() -> None:
    expected = [float(i * 5000) for i in range(37)]
    offsets = [0.0] * 12 + [75.0] * 12 + [0.0] * 13
    report = module.beep_diagnostics(
        expected,
        {"mic": [t + n for t, n in zip(expected, offsets, strict=True)], "far": expected},
    )
    windows = report["minute_windows"]
    assert windows["full_window_count"] == 3
    assert [row["beep_count"] for row in windows["items"]] == [12, 12, 12, 1]
    assert [row["complete"] for row in windows["items"]] == [True, True, True, False]
    assert windows["max_change_from_first_ms"] == 75
    assert windows["range_ms"] == 75
    assert windows["items"][0]["median_offset_ms"] == 0


def test_beep_diagnostics_partial_minute_is_not_full_window_evidence() -> None:
    expected = [float(i * 5000) for i in range(21)]
    offsets = [0.0] * 12 + [80.0] * 9
    report = module.beep_diagnostics(
        expected,
        {"mic": [t + n for t, n in zip(expected, offsets, strict=True)], "far": expected},
    )
    windows = report["minute_windows"]
    assert windows["full_window_count"] == 1
    assert windows["max_change_from_first_ms"] is None
    assert windows["range_ms"] is None
    assert windows["items"][-1]["median_offset_ms"] == 80
    assert report["raw_max_change_ms"] == 80


def test_recording_adds_diagnostics_without_replacing_raw_failure(tmp_path: Path) -> None:
    recording(tmp_path, "mic", beeps=(100, 670, 1100))
    recording(tmp_path, "far", beeps=(100, 600, 1100))
    report = module.analyze_recording(tmp_path, expected_beeps_ms=[10100.0, 10600.0, 11100.0])
    assert report["beep_drift_ms"] == pytest.approx(70)
    assert statuses(report)["beep_drift"] == "fail"
    fit = report["beep_diagnostics"]["fits"]["mic_minus_far"]
    assert fit["slope_ms_per_hour"] == pytest.approx(0, abs=1e-7)
    assert report["beep_diagnostics"]["hourly_projection"] is True
    assert all(len(s["beep_local_samples"]) == 3 for s in report["segments"])


@pytest.mark.parametrize("ordered", [False, True])
def test_recording_diagnostics_validates_epoch_order_before_legacy_sort(
    tmp_path: Path, ordered: bool
) -> None:
    starts = [10000, 15000, 20000] if ordered else [20000, 10000, 15000]
    for track in ("mic", "far"):
        # Descending file creation preserves the helper's fixed 0000 filename.
        for index in reversed(range(3)):
            recording(
                tmp_path,
                track,
                shift_ms=starts[index] - 10000,
                beeps=(0,),
                epoch=f"{track}-epoch-{index}",
                sample_start=0,
            )
            old = tmp_path / f"{track}-0000.wav"
            side = json.loads(old.with_suffix(".json").read_text())
            side["index"] = index
            old.with_suffix(".json").write_text(json.dumps(side))
            if index:
                new = tmp_path / f"{track}-{index:04d}.wav"
                old.rename(new)
                old.with_suffix(".json").rename(new.with_suffix(".json"))
    report = module.analyze_recording(tmp_path, expected_beeps_ms=[10000.0, 15000.0, 20000.0])
    # Each epoch has only one marker: there is no within-epoch drift evidence.
    assert statuses(report)["beep_drift"] == "unavailable"
    assert report["beep_drift_ms"] is None
    assert report["beep_diagnostics"]["status"] == ("diagnostic" if ordered else "unavailable")


def test_beep_gate_uses_shared_product_epoch_clock_and_keeps_raw_failure(tmp_path: Path) -> None:
    from astra_voice.meetings.segments import ClockAnchor, SegmentSidecar
    from astra_voice.meetings.timeline import EpochTimeline

    expected = [105000.0, 135000.0, 165000.0, 195000.0]
    epochs: dict[str, EpochTimeline] = {}
    for track, sign in (("mic", 1), ("far", -1)):
        sides = []
        for index in range(2):
            samples = array.array("h", [0]) * 960000
            for second in (5, 35):
                samples[second * 16000] = 16000
            if sys.byteorder != "little":
                samples.byteswap()
            path = tmp_path / f"{track}-{index:04d}.wav"
            with wave.open(str(path), "wb") as stream:
                stream.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
                stream.writeframes(samples.tobytes())
            points = [
                [t * 16000, 100000 + t * 1000 + sign * (40 if t % 4 < 2 else -40)]
                for t in range(index * 60, (index + 1) * 60 + 1)
            ]
            side = SegmentSidecar(
                meeting_id="2026-10-31_120000-ab12",
                epoch_id=f"{track}-epoch",
                track=track,
                index=index,
                frames=960000,
                frames_at_checkpoint=960000,
                sample_start=index * 960000,
                sample_end=(index + 1) * 960000,
                timeline=points,
                closed=True,
                opened_t_mono_ms=points[0][1],
                clock_anchor=ClockAnchor(0, points[0][1]),
            )
            sides.append(side)
            path.with_suffix(".json").write_text(json.dumps(side.to_dict()))
        epochs[track] = EpochTimeline.from_segments(sides)
    report = module.analyze_recording(tmp_path, expected_beeps_ms=expected)
    assert report["raw_beep_drift_ms"] == 160
    assert report["raw_beep_drift_criterion"]["status"] == "fail"
    assert statuses(report)["beep_drift"] == "pass"
    for track in ("mic", "far"):
        assert report["beeps_mono_ms"][track] == [
            epochs[track].sample_to_monotonic(int((v - 100000) * 16)) for v in expected
        ]
    assert all(row["mapping_method"] == "robust-linear" for row in report["segments"])


def test_rejected_epoch_keeps_historical_raw_diagnostics(tmp_path: Path) -> None:
    for track in ("mic", "far"):
        for index in reversed(range(2)):
            recording(
                tmp_path,
                track,
                shift_ms=index * 2000 + (80 if track == "mic" and index else 0),
                sample_start=index * 32000,
                beeps=(100,),
            )
            path = tmp_path / f"{track}-0000.wav"
            side = json.loads(path.with_suffix(".json").read_text())
            side["index"] = index
            path.with_suffix(".json").write_text(json.dumps(side))
            if index:
                destination = tmp_path / f"{track}-{index:04d}.wav"
                path.rename(destination)
                path.with_suffix(".json").rename(destination.with_suffix(".json"))
    report = module.analyze_recording(tmp_path, expected_beeps_ms=[10100, 12100])
    assert statuses(report)["beep_drift"] == "unavailable"
    assert report["beep_drift_ms"] is None
    assert report["raw_beep_drift_ms"] == pytest.approx(80)
    assert report["raw_beep_drift_criterion"]["status"] == "fail"
    assert all(len(values) == 2 for values in report["raw_beeps_mono_ms"].values())
