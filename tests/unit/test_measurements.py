"""Замеры GUI не содержат текста и переживают ошибки файла."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from astra_voice.core.measurements import MeasurementTracker, read_measurements
from astra_voice.core.version import __version__

pytestmark = pytest.mark.unit


def test_first_success_requests_measure_and_warm_median(tmp_path: Path) -> None:
    path = tmp_path / "measurements.json"
    tracker = MeasurementTracker(path)
    tracker.set_model("model", "rev", 2)
    assert tracker.dictation(audio_ms=1000, infer_ms=100, cold=True)
    tracker.measured(1536)
    saved = json.loads(path.read_text())["model@rev"]
    assert saved["ram_mb"] == 2
    assert saved["rtfx"] is None
    assert saved["threads"] == 2
    assert saved["build"] == __version__
    assert isinstance(saved["measured_at"], str)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    for divisor in (10, 30, 20, 50):
        assert not tracker.dictation(audio_ms=1000, infer_ms=1000 / divisor, cold=False)
    assert json.loads(path.read_text())["model@rev"]["rtfx"] is None
    tracker.dictation(audio_ms=1000, infer_ms=25, cold=False)
    saved = json.loads(path.read_text())["model@rev"]
    assert saved["rtfx"] == pytest.approx(30)
    assert "text" not in json.dumps(saved)


def test_failed_request_retries_and_saves_rtfx_without_ram(tmp_path: Path) -> None:
    path = tmp_path / "measurements.json"
    tracker = MeasurementTracker(path)
    tracker.set_model("model", "rev", 2)
    assert tracker.dictation(audio_ms=1000, infer_ms=100, cold=True)
    tracker.measure_failed()
    for index in range(5):
        assert tracker.dictation(audio_ms=1000, infer_ms=100, cold=False) == (index == 0)
    saved = json.loads(path.read_text())["model@rev"]
    assert saved["rtfx"] == 10
    assert saved["ram_mb"] is None


def test_failed_measure_retries_even_without_next_result_timings(tmp_path: Path) -> None:
    tracker = MeasurementTracker(tmp_path / "measurements.json")
    tracker.set_model("model", "rev", 2)
    assert tracker.dictation(audio_ms=1000, infer_ms=100, cold=True)
    tracker.measure_failed()
    assert tracker.dictation(audio_ms=None, infer_ms=None, cold=False)


def test_new_longest_audio_and_fifth_warm_run_request_measure(tmp_path: Path) -> None:
    tracker = MeasurementTracker(tmp_path / "measurements.json")
    tracker.set_model("model", "rev", 2)
    assert tracker.dictation(audio_ms=1000, infer_ms=100, cold=True)
    tracker.measured(2000)
    for _ in range(4):
        assert not tracker.dictation(audio_ms=1000, infer_ms=100, cold=False)
    assert tracker.dictation(audio_ms=1000, infer_ms=100, cold=False)
    tracker.measured(1000)
    assert tracker.ram_mb == 2
    assert tracker.dictation(audio_ms=2000, infer_ms=100, cold=False)


def test_longer_audio_while_measure_in_flight_queues_new_request(tmp_path: Path) -> None:
    tracker = MeasurementTracker(tmp_path / "measurements.json")
    tracker.set_model("model", "rev", 2)
    assert tracker.dictation(audio_ms=1000, infer_ms=100, cold=True)
    assert not tracker.dictation(audio_ms=2000, infer_ms=100, cold=False)
    assert tracker.measured(2000)
    assert tracker.requested
    assert not tracker.measured(1000)
    assert tracker.ram_mb == 2


def test_warm_window_and_unchanged_file(tmp_path: Path) -> None:
    path = tmp_path / "measurements.json"
    tracker = MeasurementTracker(path)
    tracker.set_model("model", "rev", 2)
    tracker.dictation(audio_ms=1000, infer_ms=100, cold=True)
    tracker.measured(1000)
    for _ in range(5):
        tracker.dictation(audio_ms=1000, infer_ms=100, cold=False)
    before = path.stat().st_mtime_ns
    for _ in range(25):
        tracker.dictation(audio_ms=1000, infer_ms=100, cold=False)
    assert len(tracker.warm_runs) == 20
    assert path.stat().st_mtime_ns == before


def test_read_rejects_large_and_non_regular_file(tmp_path: Path) -> None:
    path = tmp_path / "measurements.json"
    path.write_bytes(b" " * ((1 << 20) + 1))
    assert read_measurements(path) == {}
    path.unlink()
    path.symlink_to(tmp_path / "missing")
    assert read_measurements(path) == {}


@pytest.mark.parametrize("revision,threads", [("next", 2), ("rev", 4)])
def test_change_discards_measure_and_warm_runs(tmp_path: Path, revision: str, threads: int) -> None:
    path = tmp_path / "measurements.json"
    tracker = MeasurementTracker(path)
    tracker.set_model("model", "rev", 2)
    tracker.dictation(audio_ms=1000, infer_ms=100, cold=False)
    tracker.measured(1024)
    tracker.set_model("model", revision, threads)
    assert tracker.warm_runs == []
    assert tracker.ram_mb is None
    assert "model@rev" not in json.loads(path.read_text())
    assert tracker.dictation(audio_ms=1000, infer_ms=100, cold=True)


def test_bad_file_does_not_block_measurement(tmp_path: Path) -> None:
    path = tmp_path / "measurements.json"
    path.write_text("bad json")
    tracker = MeasurementTracker(path)
    tracker.set_model("model", "rev", 2)
    assert tracker.dictation(audio_ms=1000, infer_ms=100, cold=True)
    tracker.measured(1024)
    assert json.loads(path.read_text())["model@rev"]["ram_mb"] == 1


def test_old_result_without_timings_does_not_start_measurement(tmp_path: Path) -> None:
    tracker = MeasurementTracker(tmp_path / "measurements.json")
    tracker.set_model("model", "rev", 2)
    assert not tracker.dictation(audio_ms=None, infer_ms=None, cold=True)
    assert not tracker.requested
    assert tracker.warm_runs == []
    assert tracker.dictation(audio_ms=1000, infer_ms=100, cold=True)
    assert tracker.requested


@pytest.mark.parametrize("build", [None, "other-version"])
def test_old_build_measurement_is_ignored(tmp_path: Path, build: str | None) -> None:
    path = tmp_path / "measurements.json"
    saved = {"ram_mb": 900, "rtfx": 20, "threads": 2, "measured_at": "old"}
    if build is not None:
        saved["build"] = build
    path.write_text(json.dumps({"model@rev": saved}))
    assert read_measurements(path) == {}
    tracker = MeasurementTracker(path)
    tracker.set_model("model", "rev", 2)
    assert tracker.ram_mb is None
    assert tracker.dictation(audio_ms=1000, infer_ms=100, cold=True)


def test_repeated_measurement_keeps_timestamp_and_file(tmp_path: Path) -> None:
    path = tmp_path / "measurements.json"
    tracker = MeasurementTracker(path)
    tracker.set_model("model", "rev", 2)
    tracker.measured(2000)
    measured_at = tracker.measured_at
    before = path.stat().st_mtime_ns
    tracker.measured(1000)
    assert tracker.measured_at == measured_at
    assert path.stat().st_mtime_ns == before
