"""Read-only evidence for the E13.M2 isolated recording stand.

Architecture §5.7 inherits SP1 S1.5c's mean CPU budget (one core). Peak is
reported separately. Hourly rates from short samples are projections; they do
not satisfy the sixty-minute soak. Missing evidence always remains unavailable.
No imports of production capture code or experimental spikes are permitted here.
The pure product timeline is shared with M4/M7 so the beep gate measures the
actual timestamp contract. Raw piecewise errors remain separately visible.
"""

from __future__ import annotations

import array
import json
import math
import os
import re
import stat
import statistics
import sys
import wave
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from astra_voice.meetings.segments import SegmentSidecar
from astra_voice.meetings.timeline import EpochTimeline, SegmentTimeline

MIB = 1024**2
JSON = dict[str, Any]
_CAPTURE_TEMP = re.compile(r"\.(?:capture|meeting|(?:mic|far)-[0-9]{4,})-[0-9a-f]{32}\.tmp\Z")


def _finite(value: object) -> bool:
    return type(value) in (int, float) and math.isfinite(value)  # type: ignore[arg-type]


def criterion(name: str, value: float | None, limit: float, unit: str, *, detail: str = "") -> JSON:
    """Upper-bound criterion; None and nonfinite values never count as success."""
    known = value is not None and _finite(value)
    return {
        "name": name,
        "status": ("pass" if value <= limit else "fail")
        if known and value is not None
        else "unavailable",
        "value": value if known else None,
        "limit": limit,
        "unit": unit,
        "detail": detail,
    }


def verdict(criteria: Sequence[Mapping[str, Any]]) -> int:
    if any(row.get("status") == "fail" for row in criteria):
        return 1
    if not criteria or any(row.get("status") != "pass" for row in criteria):
        return 2
    return 0


@dataclass(frozen=True)
class ProcStat:
    pid: int
    ticks: int
    start_ticks: int
    state: str


def parse_proc_stat(text: str) -> ProcStat:
    """Linux comm may itself contain spaces and parentheses; use the last ')'."""
    try:
        left, right = text.index("("), text.rindex(")")
        fields = text[right + 1 :].split()
        result = ProcStat(
            int(text[:left].strip()), int(fields[11]) + int(fields[12]), int(fields[19]), fields[0]
        )
        if right <= left or result.pid <= 0 or result.ticks < 0 or result.start_ticks < 0:
            raise ValueError("invalid-proc-stat")
        return result
    except (IndexError, ValueError):
        raise ValueError("invalid-proc-stat") from None


def _rss(text: str) -> int | None:
    for line in text.splitlines():
        match = re.fullmatch(r"VmRSS:\s+(\d+)\s+kB\s*", line)
        if match:
            return int(match[1]) * 1024
    return None


def _limits(text: str) -> JSON:
    result: JSON = {}
    for name, key in (("Max core file size", "core"), ("Max address space", "as")):
        match = re.search(
            r"^" + name + r"\s+(\d+|unlimited)\s+(\d+|unlimited)\s+bytes\s*$", text, re.MULTILINE
        )
        if match:
            for index, suffix in ((1, "soft"), (2, "hard")):
                result[f"{key}_{suffix}"] = (
                    None if match[index] == "unlimited" else int(match[index])
                )
    return result


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None


def _disk_bytes(root: Path) -> int | None:
    """Logical regular-file bytes; incomplete walks or symlinks are unknown."""
    total = 0
    try:
        if root.is_symlink() or not root.is_dir():
            return None

        def on_error(error: OSError) -> None:
            raise error

        for parent, directories, files in os.walk(root, onerror=on_error, followlinks=False):
            for name in (*directories, *files):
                temporary = bool(_CAPTURE_TEMP.fullmatch(name))
                try:
                    metadata = (Path(parent) / name).lstat()
                except FileNotFoundError:
                    if temporary:
                        continue  # Atomic writer removed its transient publication file.
                    raise
                if stat.S_ISLNK(metadata.st_mode):
                    return None
                if temporary and not stat.S_ISREG(metadata.st_mode):
                    return None
                if stat.S_ISREG(metadata.st_mode):
                    if temporary:
                        if (
                            metadata.st_uid != os.geteuid()
                            or stat.S_IMODE(metadata.st_mode) != 0o600
                            or metadata.st_nlink != 1
                        ):
                            return None
                        continue
                    total += metadata.st_size
        return total
    except OSError:
        return None


class ProcSampler:
    """Caller supplies explicit role PIDs; no guesses based on process names."""

    def __init__(self, proc_root: Path = Path("/proc"), *, hz: int | None = None) -> None:
        self.proc_root = proc_root
        self.hz = int(os.sysconf("SC_CLK_TCK")) if hz is None else hz
        if self.hz <= 0:
            raise ValueError("invalid-clock-tick-rate")
        self.previous: dict[int, tuple[ProcStat, float]] = {}
        self.last_time: float | None = None

    def sample(self, roles: Mapping[str, Sequence[int]], *, now_s: float, disk_root: Path) -> JSON:
        if not _finite(now_s) or (self.last_time is not None and now_s <= self.last_time):
            raise ValueError("nonmonotonic-sampling-clock")
        self.last_time = now_s
        rows = []
        for role, pids in sorted(roles.items()):
            for pid in sorted(set(pids)):
                row: JSON = {
                    "role": role,
                    "pid": pid,
                    "start_ticks": None,
                    "cpu_pct": None,
                    "rss_bytes": None,
                    "limits": {},
                    "oom_score_adj": None,
                }
                directory = self.proc_root / str(pid)
                try:
                    current = parse_proc_stat(_read(directory / "stat") or "")
                    if current.pid != pid or current.state in {"Z", "X", "x"}:
                        raise ValueError("process-unavailable")
                    row["start_ticks"] = current.start_ticks
                    row["rss_bytes"] = _rss(_read(directory / "status") or "")
                    row["limits"] = _limits(_read(directory / "limits") or "")
                    oom = _read(directory / "oom_score_adj")
                    row["oom_score_adj"] = int(oom) if oom is not None else None
                    previous = self.previous.get(pid)
                    if (
                        previous is not None
                        and previous[0].start_ticks == current.start_ticks
                        and current.ticks >= previous[0].ticks
                    ):
                        row["cpu_pct"] = (
                            (current.ticks - previous[0].ticks)
                            * 100
                            / (self.hz * (now_s - previous[1]))
                        )
                    check = parse_proc_stat(_read(directory / "stat") or "")
                    if check.start_ticks != current.start_ticks:
                        raise ValueError("process-reused")
                    self.previous[pid] = current, now_s
                except (ValueError, OSError):
                    self.previous.pop(pid, None)
                    row.update(
                        start_ticks=None,
                        cpu_pct=None,
                        rss_bytes=None,
                        limits={},
                        oom_score_adj=None,
                    )
                rows.append(row)
        return {"t_mono_s": now_s, "processes": rows, "disk_bytes": _disk_bytes(disk_root)}


def summarize_resources(samples: Sequence[JSON]) -> JSON:
    duration = samples[-1]["t_mono_s"] - samples[0]["t_mono_s"] if len(samples) >= 2 else 0.0
    if duration < 0 or any(
        b["t_mono_s"] <= a["t_mono_s"] for a, b in zip(samples, samples[1:], strict=False)
    ):
        raise ValueError("nonmonotonic-samples")
    captures = [
        [p for p in row["processes"] if p["role"] in {"capture", "meeting-capture"}]
        for row in samples
    ]
    complete = bool(samples) and all(captures)
    cpu_intervals = []
    for index in range(1, len(samples)):
        procs = captures[index]
        before = {(p["pid"], p["start_ticks"]) for p in captures[index - 1]}
        after = {(p["pid"], p["start_ticks"]) for p in procs}
        if not procs or before != after or any(p["cpu_pct"] is None for p in procs):
            complete = False
        else:
            cpu_intervals.append(
                (
                    sum(p["cpu_pct"] for p in procs),
                    samples[index]["t_mono_s"] - samples[index - 1]["t_mono_s"],
                )
            )
    mean = (
        sum(cpu * elapsed for cpu, elapsed in cpu_intervals) / duration
        if (complete and cpu_intervals and duration > 0)
        else None
    )
    peak = max((cpu for cpu, _ in cpu_intervals), default=None)
    rss_known = bool(captures) and all(
        procs and all(p["rss_bytes"] is not None for p in procs) for procs in captures
    )
    rss = (
        max((sum(p["rss_bytes"] for p in procs) / MIB for procs in captures), default=None)
        if rss_known
        else None
    )
    first_ids = {(p["pid"], p["start_ticks"]) for p in captures[0]} if captures else set()
    stable = bool(first_ids) and all(
        {(p["pid"], p["start_ticks"]) for p in procs} == first_ids for procs in captures
    )
    growth = (
        (
            (sum(p["rss_bytes"] for p in captures[-1]) - sum(p["rss_bytes"] for p in captures[0]))
            / MIB
            * 3600
            / duration
        )
        if rss_known and stable and duration > 0
        else None
    )
    disk = (
        ((samples[-1]["disk_bytes"] - samples[0]["disk_bytes"]) / MIB * 3600 / duration)
        if duration > 0 and all(row["disk_bytes"] is not None for row in samples)
        else None
    )
    if disk is not None and (
        disk < 0
        or any(
            b["disk_bytes"] < a["disk_bytes"] for a, b in zip(samples, samples[1:], strict=False)
        )
    ):
        disk = None  # deletion would hide bytes written
    disk_problem = (
        "insufficient-resource-duration"
        if duration <= 0
        else "missing-disk-samples"
        if any(row["disk_bytes"] is None for row in samples)
        else "decreasing-committed-disk-bytes"
        if disk is None
        else "committed-file-byte-growth"
    )
    limits: float | None = 0.0 if captures and all(captures) else None
    for procs in captures:
        for process in procs:
            observed = process["limits"]
            if not {"core_soft", "core_hard", "as_soft"}.issubset(observed) or (
                process["oom_score_adj"] is None
            ):
                if limits != 1.0:
                    limits = None
            elif not (
                observed["core_soft"] == observed["core_hard"] == 0
                and observed["as_soft"] is not None
                and 0 < observed["as_soft"] <= 512 * MIB
                and process["oom_score_adj"] == 500
            ):
                limits = 1.0
    growth_criterion = criterion("capture_rss_growth", growth, 20.0, "MiB/h")
    if duration < 3600 and growth is not None:
        growth_criterion.update(
            status="provisional",
            detail="endpoint projection includes startup; requires >=3600s before PASS/FAIL",
        )
    criteria = [
        criterion(
            "capture_cpu", mean, 3.0, "% one core", detail="time-weighted mean; §5.7 / SP1 S1.5c"
        ),
        criterion("capture_rss", rss, 60.0, "MiB"),
        *([growth_criterion] if growth_criterion["status"] != "provisional" else []),
        criterion(
            "disk",
            disk,
            230.0,
            "MiB/h",
            detail=(
                f"{disk_problem}; logical file-byte growth including sidecars, "
                "excluding private atomic publication temporaries"
            ),
        ),
        criterion(
            "capture_role_limits",
            limits,
            0.0,
            "violations",
            detail=(
                "observed /proc: core=0/0, AS soft<=512MiB, oom_score_adj=500; prctl not observed"
            ),
        ),
    ]
    return {
        "duration_s": duration,
        "projected": duration < 3600,
        "full_soak": duration >= 3600,
        "soak_criterion": criterion(
            "soak_duration",
            0.0 if duration >= 3600 else None,
            0.0,
            "missing",
            detail="requires >=3600s observed; short runs are provisional",
        ),
        "capture_cpu_mean_pct": mean,
        "capture_cpu_peak_pct": peak,
        "rss_peak_mib": rss,
        "growth_mib_per_hour": growth,
        "growth_criterion": growth_criterion,
        "disk_mib_per_hour": disk,
        "disk_diagnostics": {
            "sample_count": len(samples),
            "missing_samples": sum(row["disk_bytes"] is None for row in samples),
            "decreasing_steps": sum(
                a["disk_bytes"] is not None
                and b["disk_bytes"] is not None
                and b["disk_bytes"] < a["disk_bytes"]
                for a, b in zip(samples, samples[1:], strict=False)
            ),
        },
        "criteria": criteria,
    }


def _uint(value: Any) -> bool:
    return type(value) is int and value >= 0


def _validate_sidecar(side: JSON) -> None:
    for key in (
        "frames",
        "frames_at_checkpoint",
        "sample_start",
        "sample_end",
        "index",
        "opened_t_mono_ms",
    ):
        if not _uint(side.get(key)):
            raise ValueError("invalid-sidecar-integer")
    if (
        side.get("schema") != 1
        or side.get("track") not in {"mic", "far"}
        or side.get("rate") != 16000
        or side.get("channels") != 1
        or side.get("width") != 2
        or type(side.get("closed")) is not bool
        or type(side.get("discontinuity")) is not bool
        or not isinstance(side.get("epoch_id"), str)
        or not side["epoch_id"]
        or side.get("clock") not in {"pulse", "pipewire", "monotonic"}
        or side["sample_end"] - side["sample_start"] != side["frames"]
        or not 0 <= side["frames_at_checkpoint"] <= side["frames"] <= 4800000
    ):
        raise ValueError("invalid-sidecar")
    points = side.get("timeline")
    if not isinstance(points, list) or not 1 <= len(points) <= 302:
        raise ValueError("invalid-timeline")
    for index, point in enumerate(points):
        if (
            not isinstance(point, list)
            or len(point) != 2
            or not all(_uint(v) for v in point)
            or not side["sample_start"] <= point[0] <= side["sample_end"]
            or (index and (point[0] <= points[index - 1][0] or point[1] <= points[index - 1][1]))
        ):
            raise ValueError("invalid-timeline")
    anchor = side.get("clock_anchor")
    if (
        not isinstance(anchor, dict)
        or not _uint(anchor.get("server_ms"))
        or not _uint(anchor.get("t_mono_ms"))
        or anchor["t_mono_ms"] != side["opened_t_mono_ms"]
        or points[0] != [side["sample_start"], anchor["t_mono_ms"]]
    ):
        raise ValueError("invalid-clock-anchor")


def sample_to_monotonic(side: JSON, sample: int) -> int:
    """Single-segment product mapping; complete recordings reuse an epoch fit."""
    _validate_sidecar(side)
    return SegmentTimeline(
        side["sample_start"],
        side["sample_end"],
        side["discontinuity"],
        tuple((s, m) for s, m in side["timeline"]),
    ).sample_to_monotonic(sample)


def _raw_sample_to_monotonic(side: JSON, sample: int) -> float:
    """Legacy piecewise diagnostic only; no extrapolation or gap crossing."""
    _validate_sidecar(side)
    if type(sample) is not int or not side["sample_start"] <= sample <= side["sample_end"]:
        raise ValueError("unmapped-sample")
    for offset, mono in side["timeline"]:
        if sample == offset:
            return float(mono)
    for (left, start), (right, end) in zip(side["timeline"], side["timeline"][1:], strict=False):
        if left < sample < right:
            return float(start + (sample - left) * (end - start) / (right - left))
    raise ValueError("missing-clock-knots")


def _recorded(intervals: Sequence[Mapping[str, int]], mono: float) -> float:
    if not intervals or len(intervals) % 2:
        raise ValueError("unpaired-intervals")
    previous_end, previous_recorded = -1, 0
    for left, right in zip(intervals[::2], intervals[1::2], strict=True):
        start, end = left["t_mono_ms"], right["t_mono_ms"]
        if (
            not all(
                _uint(edge.get(key))
                for edge in (left, right)
                for key in ("t_mono_ms", "recorded_ms")
            )
            or start < previous_end
            or end < start
            or left["recorded_ms"] != previous_recorded
            # Both axes truncate independent monotonic timestamps to integer
            # milliseconds. Their interval differences may differ by 1ms.
            or abs(right["recorded_ms"] - left["recorded_ms"] - (end - start)) > 1
        ):
            raise ValueError("invalid-intervals")
        previous_end = end
        previous_recorded = right["recorded_ms"]
    # Validate every durable boundary before mapping any point. An early return
    # would hide a corrupt tail, a shifted origin or recorded time during pause.
    for left, right in zip(intervals[::2], intervals[1::2], strict=True):
        start, end = left["t_mono_ms"], right["t_mono_ms"]
        if start <= mono <= end:
            return float(left["recorded_ms"] + mono - start)
    raise ValueError("timeline-gap")


def _read_segment(path: Path, *, crash: bool) -> tuple[JSON, list[int]]:
    side_path = path.with_suffix(".json")
    if path.is_symlink() or side_path.is_symlink():
        raise ValueError("symlink-segment")
    side = json.loads(side_path.read_text(encoding="utf-8"))
    if not isinstance(side, dict):
        raise ValueError("invalid-sidecar-object")
    _validate_sidecar(side)
    if path.name != f"{side['track']}-{side['index']:04d}.wav" or (
        not crash and not side["closed"]
    ):
        raise ValueError("unfinalized-or-misnamed-segment")
    beeps: list[int] = []
    position, last_loud = 0, -16000
    with wave.open(str(path), "rb") as source:
        count = source.getnframes()
        if (
            source.getparams()[:3] != (1, 2, 16000)
            or source.getcomptype() != "NONE"
            or count < side["frames"]
            or (side["closed"] and count != side["frames"])
        ):
            raise ValueError("wav-sidecar-mismatch")
        while data := source.readframes(65536):
            block = array.array("h")
            block.frombytes(data)
            if sys.byteorder != "little":
                block.byteswap()
            for index, value in enumerate(block):
                if abs(value) >= 4000:
                    absolute = position + index
                    # write_signal emits a 150ms marker every five seconds.
                    # A short dropout inside that known pulse is not a second
                    # marker; never merge separate scheduled pulses or invent
                    # an onset when an entire marker is missing.
                    if absolute - last_loud > 640 and (not beeps or absolute - beeps[-1] >= 2400):
                        beeps.append(absolute)
                    last_loud = absolute
            position += len(block)
        if position != count or (side["closed"] and path.stat().st_size != 44 + position * 2):
            raise ValueError("truncated-or-extra-pcm")
    return side, beeps


def _startup_placeholder(side: JSON) -> bool:
    """Only the proven empty monotonic-to-server startup transition is skippable.

    Call after PCM and sidecar validation. A terminal discontinuity does not
    invalidate observed knots; an all-placeholder capture has no positive PCM.
    """
    return bool(
        side["index"] == 0
        and side["frames"] == side["frames_at_checkpoint"] == 0
        and side["sample_start"] == side["sample_end"] == 0
        and side["clock"] == "monotonic"
        and side["closed"]
        and side["discontinuity"]
        and side.get("close_reason") == "clock_change"
        and len(side["timeline"]) == 1
    )


@dataclass(frozen=True)
class _MarkerEdge:
    side: JSON
    age_frames: int | None
    tail_gap_frames: int | None


def _continue_marker(
    path: Path, side: JSON, beeps: list[int], previous: _MarkerEdge | None
) -> tuple[list[int], _MarkerEdge, bool]:
    """Carry detector state, never a clock fit, across an observed adjacent edge.

    Epoch counters can restart at zero. Require consecutive files of the same
    capture, observed clock edges equal to 1ms quantisation, and PCM consistent
    with the same known 150ms pulse. Pauses and nonadjacent clocks never merge.
    """
    continued = bool(
        previous is not None
        and previous.age_frames is not None
        and previous.tail_gap_frames is not None
        and beeps
        and 0 <= beeps[0] < side["frames"]
        and previous.side.get("close_reason", "segment") in {"segment", "clock_change"}
        and side["index"] == previous.side["index"] + 1
        and all(
            side.get(key) == previous.side.get(key)
            for key in ("meeting_id", "meeting_generation", "capture_id", "track")
        )
        and (
            side["epoch_id"] != previous.side["epoch_id"]
            or side["sample_start"] == previous.side["sample_end"]
        )
        and abs(side["timeline"][0][1] - previous.side["timeline"][-1][1]) <= 1
        and previous.age_frames + beeps[0] < 2400
        and previous.tail_gap_frames + beeps[0] <= 640
    )
    if continued:
        beeps = beeps[1:]
    observed_beeps = [sample for sample in beeps if sample < side["frames"]]
    age = (
        side["frames"] - observed_beeps[-1]
        if observed_beeps
        else (
            previous.age_frames + side["frames"]
            if continued and previous is not None and previous.age_frames is not None
            else None
        )
    )
    start = max(0, side["frames"] - 640)
    with wave.open(str(path), "rb") as stream:
        stream.setpos(start)
        tail = array.array("h", stream.readframes(side["frames"] - start))
    if sys.byteorder != "little":
        tail.byteswap()
    gap = next((len(tail) - i for i in range(len(tail) - 1, -1, -1) if abs(tail[i]) >= 4000), None)
    return beeps, _MarkerEdge(side, age, gap), continued


def _beep_error_fit(hours: Sequence[float], errors: Sequence[float]) -> JSON:
    """OLS diagnostics, never an acceptance estimator or detector calibration.

    x is hours since the first scheduled marker; y is a timestamp error in ms.
    Sigma uses N (population RMS after fitting); the separate regression sigma
    uses N-2. The slope SE assumes independent, homoscedastic residuals and is
    NOT a confidence interval for the generally correlated timeline errors.
    """
    mean_x, mean_y = statistics.mean(hours), statistics.mean(errors)
    sxx = sum((x - mean_x) ** 2 for x in hours)
    slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(hours, errors, strict=True)) / sxx
    intercept = mean_y - slope * mean_x
    residuals = [y - (intercept + slope * x) for x, y in zip(hours, errors, strict=True)]
    squared = sum(r * r for r in residuals)
    regression_sigma = math.sqrt(squared / (len(errors) - 2))
    return {
        "intercept_ms": intercept,
        "slope_ms_per_hour": slope,
        "span_change_ms": slope * (hours[-1] - hours[0]),
        "residual_sigma_ms": math.sqrt(squared / len(errors)),
        "residual_sigma_dof_ms": regression_sigma,
        "residual_max_abs_ms": max(abs(r) for r in residuals),
        "iid_slope_se_ms_per_hour": regression_sigma / math.sqrt(sxx),
    }


def _beep_minute_windows(elapsed_ms: Sequence[float], offsets: Sequence[float]) -> JSON:
    """Fixed half-open 60s windows from first marker, without discarding startup.

    A full window ends no later than the last observed marker and has >=3
    expected/observed paired markers. All partial windows remain visible but
    cannot contribute to full-window excursion statistics. A complete train is
    validated by the caller; no unmatched or missing beep is filled in here.
    """
    groups: dict[int, list[float]] = {}
    for elapsed, offset in zip(elapsed_ms, offsets, strict=True):
        groups.setdefault(int(elapsed // 60000), []).append(offset)
    items: list[JSON] = [
        {
            "start_s": index * 60,
            "beep_count": len(values),
            "complete": (index + 1) * 60000 <= elapsed_ms[-1] and len(values) >= 3,
            "median_offset_ms": statistics.median(values),
        }
        for index, values in groups.items()
    ]
    full = [row["median_offset_ms"] for row in items if row["complete"]]
    # Never silently move the baseline past an insufficient first window.
    known = len(full) >= 2 and items[0]["complete"]
    return {
        "items": items,
        "full_window_count": len(full),
        "max_change_from_first_ms": max(abs(v - full[0]) for v in full) if known else None,
        "range_ms": max(full) - min(full) if known else None,
    }


def beep_diagnostics(expected_ms: Sequence[float], mapped: Mapping[str, Sequence[float]]) -> JSON:
    """Explain raw errors without subtracting a fitted trend from the raw gate.

    Residuals combine playback/capture clock/timeline/detector effects. They are
    NOT detector noise: onset accuracy requires a separate PCM experiment.
    Ordered complete trains and a <half-spacing association are required here.
    Even a long, small-slope fit cannot prove the <=50ms maximum-error contract.
    """
    unavailable: JSON = {"status": "unavailable", "fits": {}}
    if (
        len(expected_ms) < 3
        or not all(_finite(v) for v in expected_ms)
        or any(b <= a for a, b in zip(expected_ms, expected_ms[1:], strict=False))
    ):
        return unavailable
    half_spacing = min(b - a for a, b in zip(expected_ms, expected_ms[1:], strict=False)) / 2
    for track in ("mic", "far"):
        values = mapped.get(track, ())
        if (
            len(values) != len(expected_ms)
            or not all(_finite(v) for v in values)
            or any(b <= a for a, b in zip(values, values[1:], strict=False))
            or any(abs(v - e) >= half_spacing for v, e in zip(values, expected_ms, strict=True))
        ):
            return unavailable
    elapsed = [v - expected_ms[0] for v in expected_ms]
    hours = [v / 3600000 for v in elapsed]
    errors = {
        track: [v - e for v, e in zip(mapped[track], expected_ms, strict=True)]
        for track in ("mic", "far")
    }
    errors["mic_minus_far"] = [a - b for a, b in zip(mapped["mic"], mapped["far"], strict=True)]
    fits = {track: _beep_error_fit(hours, values) for track, values in errors.items()}
    offsets = errors["mic_minus_far"]
    worst = max(range(len(offsets)), key=lambda i: abs(offsets[i] - offsets[0]))
    change = offsets[worst] - offsets[0]
    trend = fits["mic_minus_far"]["slope_ms_per_hour"] * hours[worst]
    return {
        "status": "diagnostic",
        "basis": (
            "OLS; residual combines playback/capture clock/timeline/detector; not detector noise"
        ),
        "uncertainty": "iid slope SE is conditional; correlated errors invalidate an iid CI",
        "beep_count": len(expected_ms),
        "observed_span_s": elapsed[-1] / 1000,
        "hourly_projection": elapsed[-1] < 3600000,
        "fits": fits,
        "raw_max_change_ms": abs(change),
        "worst_change": {
            "index": worst,
            "elapsed_s": elapsed[worst] / 1000,
            "initial_offset_ms": offsets[0],
            "offset_ms": offsets[worst],
            "trend_contribution_ms": trend,
            "residual_change_ms": change - trend,
        },
        "minute_windows": _beep_minute_windows(elapsed, offsets),
    }


def analyze_recording(
    audio_root: Path,
    *,
    expected_beeps_ms: Sequence[float] = (),
    intervals: Sequence[Mapping[str, int]] = (),
    crash_mono_ms: float | None = None,
) -> JSON:
    """Read actual capture sidecars/WAVs; silence detector expects isolated beeps.

    Beeps are matched by order only when the entire expected train is present
    on both tracks. Crash checkpoints use min(mic, far) durable coverage, never
    uncheckpointed bytes, wall-clock mtime, or a synthesized final close.
    """
    items: list[JSON] = []
    mapped: dict[str, list[float]] = {"mic": [], "far": []}
    raw_mapped: dict[str, list[float]] = {"mic": [], "far": []}
    ends: dict[str, list[float]] = {"mic": [], "far": []}
    durable: dict[str, list[float]] = {"mic": [], "far": []}
    indexes: dict[str, list[int]] = {"mic": [], "far": []}
    frames = {"mic": 0, "far": 0}
    invalid = False
    clock_unknown = False
    beep_clock_unknown = False
    raw_clock_unknown = False
    prepared: list[tuple[JSON, list[int], JSON]] = []
    marker_edges: dict[str, _MarkerEdge] = {}
    mapped_epochs: dict[str, list[str]] = {"mic": [], "far": []}
    clock_issues: list[JSON] = []
    files = sorted(audio_root.glob("*.wav")) if not audio_root.is_symlink() else []
    for path in files:
        try:
            side, beeps = _read_segment(path, crash=crash_mono_ms is not None)
            track = side["track"]
            beeps, marker_edges[track], continued = _continue_marker(
                path, side, beeps, marker_edges.get(track)
            )
            indexes[track].append(side["index"])
            frames[track] += side["frames"]
            item: JSON = {
                "file": path.name,
                "track": track,
                "epoch_id": side["epoch_id"],
                "frames": side["frames"],
                "frames_at_checkpoint": side["frames_at_checkpoint"],
                "closed": side["closed"],
                "beeps": len(beeps),
                "beep_local_samples": beeps,
                "valid": True,
                "continued_marker_at_boundary": continued,
            }
            if _startup_placeholder(side):
                item["clock_status"] = "empty-startup-placeholder"
                items.append(item)
                continue
            prepared.append((side, beeps, item))
            items.append(item)
        except (OSError, ValueError, TypeError, KeyError, OverflowError, wave.Error, EOFError):
            invalid = True
            items.append({"file": path.name, "valid": False, "error": "invalid-segment-or-sidecar"})
    # One product estimate for each continuous track/epoch. In particular,
    # querying the sample shared by two files must use the SAME fitted clock.
    groups: dict[tuple[str, str], list[JSON]] = {}
    for side, _, _ in prepared:
        groups.setdefault((side["track"], side["epoch_id"]), []).append(side)
    epoch_maps: dict[tuple[str, str], EpochTimeline] = {}
    for key, sides in groups.items():
        try:
            epoch_maps[key] = EpochTimeline.from_segments([SegmentSidecar(**s) for s in sides])
        except (ValueError, TypeError, KeyError, OverflowError):
            clock_unknown = True
            clock_issues.append({"track": key[0], "epoch_id": key[1], "code": "unmapped-epoch"})
    for side, beeps, item in prepared:
        track = side["track"]
        # Preserve the original diagnostic even when a legacy seam prevents
        # construction of a shared product clock. Never repair that seam.
        try:
            raw_mapped[track].extend(
                _raw_sample_to_monotonic(side, side["sample_start"] + sample)
                for sample in beeps
                if sample < side["frames"]
            )
        except ValueError:
            raw_clock_unknown = True
        mapper = epoch_maps.get((track, side["epoch_id"]))
        if mapper is None:
            item["clock_status"] = "unavailable"
            continue
        item["mapping_method"] = mapper.mapping_method
        try:
            # Legacy checkpoints could be written after the last clock
            # knot. Bound loss by observed durable PCM, without inventing
            # a clock for the remaining tail or joining different epochs.
            last_sample = min(side["sample_end"], side["timeline"][-1][0])
            ends[track].append(mapper.sample_to_monotonic(last_sample))
            if last_sample < side["sample_end"]:
                item["clock_status"] = "partial"
                beep_clock_unknown = True
                clock_issues.append(
                    {
                        "track": track,
                        "epoch_id": side["epoch_id"],
                        "code": "unmapped-epoch:partial-clock-coverage",
                    }
                )
            checkpoint_sample = side["sample_start"] + side["frames_at_checkpoint"]
            durable_sample = min(checkpoint_sample, last_sample)
            if durable_sample > side["sample_start"]:
                durable[track].append(mapper.sample_to_monotonic(durable_sample))
            for sample in beeps:
                if sample < side["frames"]:
                    if side["sample_start"] + sample <= last_sample:
                        mapped[track].append(
                            mapper.sample_to_monotonic(side["sample_start"] + sample)
                        )
                        mapped_epochs[track].append(side["epoch_id"])
                    else:
                        beep_clock_unknown = True
        except ValueError:
            clock_unknown = True
            item["clock_status"] = "unavailable"
            clock_issues.append(
                {"track": track, "epoch_id": side["epoch_id"], "code": "unmapped-epoch:sample"}
            )
    if any(sorted(values) != list(range(len(values))) for values in indexes.values()):
        invalid = True
    if all(indexes.values()) and not all(frames.values()):
        invalid = True  # an empty WAV header does not prove recording survived
    segments = 1.0 if invalid else (0.0 if all(indexes.values()) else None)
    expected = list(expected_beeps_ms)
    expected_known = (
        not invalid
        and len(expected) >= 2
        and all(_finite(value) for value in expected)
        and all(b > a for a, b in zip(expected, expected[1:], strict=False))
    )
    beep_known = (
        expected_known
        and not clock_unknown
        and not beep_clock_unknown
        and all(len(mapped[track]) == len(expected) for track in mapped)
    )
    raw_known = (
        expected_known
        and not raw_clock_unknown
        and all(len(raw_mapped[track]) == len(expected) for track in raw_mapped)
    )
    initial: float | None = None
    drift: float | None = None
    timecodes: float | None = None
    epoch_pairs: list[JSON] = []
    epoch_jumps: list[JSON] = []
    # Validate the captured segment/epoch order before the legacy raw metric
    # sorts marker times. Sorting must not manufacture diagnostic provenance.
    diagnostics = (
        beep_diagnostics(expected, mapped) if beep_known else {"status": "unavailable", "fits": {}}
    )
    raw_diagnostics = (
        beep_diagnostics(expected, raw_mapped)
        if raw_known
        else {"status": "unavailable", "fits": {}}
    )
    raw_drift: float | None = None
    if raw_known:
        raw_offsets = [
            mic - far
            for mic, far in zip(sorted(raw_mapped["mic"]), sorted(raw_mapped["far"]), strict=True)
        ]
        raw_drift = max(abs(value - raw_offsets[0]) for value in raw_offsets)
    if beep_known:
        offsets = [mic - far for mic, far in zip(mapped["mic"], mapped["far"], strict=True)]
        initial = offsets[0]
        # Match scheduled markers in PCM order, never by sorting clocks through
        # a backward jump. Restart the offset baseline only at an epoch boundary.
        for index, offset in enumerate(offsets):
            pair = (mapped_epochs["mic"][index], mapped_epochs["far"][index])
            if not epoch_pairs or pair != (
                epoch_pairs[-1]["mic_epoch"],
                epoch_pairs[-1]["far_epoch"],
            ):
                if index:
                    epoch_jumps.append(
                        {"beep_index": index, "offset_change_ms": offset - offsets[index - 1]}
                    )
                epoch_pairs.append(
                    {
                        "mic_epoch": pair[0],
                        "far_epoch": pair[1],
                        "first_beep": index,
                        "beep_count": 0,
                        "initial_offset_ms": offset,
                        "drift_ms": 0.0,
                    }
                )
            part = epoch_pairs[-1]
            part["beep_count"] += 1
            part["drift_ms"] = max(part["drift_ms"], abs(offset - part["initial_offset_ms"]))
        for part in epoch_pairs:
            part["status"] = "measured" if part["beep_count"] >= 2 else "unmeasured"
        measured = [part["drift_ms"] for part in epoch_pairs if part["beep_count"] >= 2]
        drift = max(measured, default=None)
        try:
            timecodes = max(
                abs(_recorded(intervals, actual) - _recorded(intervals, reference))
                for track in mapped
                for actual, reference in zip(mapped[track], expected, strict=True)
            )
        except (ValueError, KeyError, TypeError):
            pass
    beep_problem = (
        "invalid-segment-or-sidecar"
        if invalid
        else "unmapped-epoch"
        if clock_unknown or beep_clock_unknown
        else "missing-or-invalid-beep-schedule"
        if not expected_known
        else "beep-count-mismatch"
        if not beep_known
        else "insufficient-within-epoch-beeps"
        if drift is None
        else "observed-epoch-pairs"
    )
    timecode_problem = (
        beep_problem
        if not beep_known
        else "unmapped-recorded-axis"
        if timecodes is None
        else "observed"
    )
    crash_loss: float | None = None
    if (
        crash_mono_ms is not None
        and _finite(crash_mono_ms)
        and not invalid
        and not clock_unknown
        and all(durable.values())
    ):
        latest = min(max(values) for values in durable.values())
        if latest <= crash_mono_ms:
            crash_loss = float(crash_mono_ms - latest)
    criteria = [
        criterion(
            "segments", segments, 0.0, "invalid", detail="PCM payload and real capture sidecars"
        ),
    ]
    if crash_mono_ms is None or expected_beeps_ms:
        criteria.extend(
            [
                criterion(
                    "beep_drift",
                    drift,
                    50.0,
                    "ms",
                    detail=(
                        f"{beep_problem}; product epoch clock: max mic/far offset change "
                        "within contiguous epoch pairs; cross-epoch jumps are diagnostic"
                    ),
                ),
                criterion(
                    "timecodes",
                    timecodes,
                    1000.0,
                    "ms",
                    detail=f"{timecode_problem}; beep recorded axis vs playback schedule",
                ),
            ]
        )
    if crash_mono_ms is not None:
        criteria.append(
            criterion(
                "crash_loss",
                crash_loss,
                10000.0,
                "ms",
                detail="conservative loss bound: SIGKILL mono minus last mapped durable PCM",
            )
        )
    return {
        "segments": items,
        "beeps_mono_ms": mapped,
        "expected_beeps_ms": expected,
        "beep_initial_offset_ms": initial,
        "beep_epochs": mapped_epochs,
        "epoch_pairs": epoch_pairs,
        "epoch_jumps": epoch_jumps,
        "clock_issues": clock_issues,
        "beep_drift_ms": drift,
        "beep_diagnostics": diagnostics,
        "raw_beeps_mono_ms": raw_mapped,
        "raw_beep_drift_ms": raw_drift,
        "raw_beep_diagnostics": raw_diagnostics,
        "raw_beep_drift_criterion": criterion(
            "raw_beep_drift",
            raw_drift,
            50.0,
            "ms",
            detail="legacy piecewise diagnostic; historical failures remain visible",
        ),
        "timecode_error_ms": timecodes,
        "crash_loss_ms": crash_loss,
        "crash_mono_ms": crash_mono_ms,
        "durable_mono_ms": {track: max(values, default=None) for track, values in durable.items()},
        "crash_loss_basis": "last-mapped-durable-knot",
        "latest_far_mono_ms": max(ends["far"], default=None),
        "criteria": criteria,
    }


def _covered(start: float, end: float, spans: Sequence[tuple[float, float]]) -> bool:
    """Require continuous observed clock coverage; never bridge a segment gap."""
    cursor = start
    for left, right in sorted(spans):
        if right < cursor:
            continue
        if left > cursor:
            return False
        cursor = max(cursor, right)
        if cursor >= end:
            return True
    return False


def analyze_exclusivity(audio_root: Path, *, expected_beeps_ms: Sequence[float]) -> JSON:
    """Measure foreign audio in far's observed quiet gaps during target playback.

    The stand plays a continuous 1700Hz sentinel alongside the target's 150ms
    1000Hz markers. Guard=1250ms permits the declared 1s timestamp error plus
    100ms capture fragsize and 150ms margin. Only inter-marker gaps count;
    silence before/after the two players' common activity cannot dilute a leak.
    Files are read in bounded PCM chunks. No clock extrapolation is permitted.
    """
    guard_ms, beep_ms, floor_dbfs = 1250.0, 150.0, -120.0
    value: float | None = None
    count = 0
    mapped_ms = 0.0
    energy = 0
    problem = "insufficient-quiet-pcm"
    expected = list(expected_beeps_ms)
    valid_train = (
        len(expected) >= 2
        and all(_finite(v) for v in expected)
        and all(b > a for a, b in zip(expected, expected[1:], strict=False))
    )
    files = sorted(audio_root.glob("far-*.wav")) if not audio_root.is_symlink() else []
    if valid_train and files:
        try:
            segments: list[tuple[Path, JSON]] = []
            coverage: list[tuple[float, float]] = []
            indexes = []
            for path in files:
                side, _ = _read_segment(path, crash=False)
                indexes.append(side["index"])
                if _startup_placeholder(side):
                    continue
                if side["track"] != "far" or side["frames"] <= 0:
                    raise ValueError("missing-far-pcm")
                # Check both extrema before considering any quiet window. A
                # partial timeline must not turn unclocked PCM into silence.
                start = sample_to_monotonic(side, side["sample_start"])
                end = sample_to_monotonic(side, side["sample_end"])
                coverage.append((start, end))
                segments.append((path, side))
            if sorted(indexes) != list(range(len(indexes))):
                raise ValueError("missing-far-segment")
            if not all(_covered(onset, onset + beep_ms, coverage) for onset in expected):
                raise ValueError("beeps-outside-clock-coverage")
            quiet = [
                (left + beep_ms + guard_ms, right - guard_ms)
                for left, right in zip(expected, expected[1:], strict=False)
                if right - left > beep_ms + 2 * guard_ms
            ]
            for path, side in segments:
                ranges: list[tuple[int, int]] = []
                for (first_sample, first_ms), (last_sample, last_ms) in zip(
                    side["timeline"], side["timeline"][1:], strict=False
                ):
                    for quiet_start, quiet_end in quiet:
                        left, right = max(first_ms, quiet_start), min(last_ms, quiet_end)
                        if left >= right:
                            continue
                        # ceil selects only actual samples inside the half-open
                        # quiet window, including at nonintegral clock slopes.
                        low = math.ceil(
                            first_sample
                            + (left - first_ms)
                            * (last_sample - first_sample)
                            / (last_ms - first_ms)
                        )
                        high = math.ceil(
                            first_sample
                            + (right - first_ms)
                            * (last_sample - first_sample)
                            / (last_ms - first_ms)
                        )
                        if low < high:
                            ranges.append((low - side["sample_start"], high - side["sample_start"]))
                            mapped_ms += (
                                (high - low) * (last_ms - first_ms) / (last_sample - first_sample)
                            )
                with wave.open(str(path), "rb") as source:
                    for low, high in sorted(ranges):
                        source.setpos(low)
                        remaining = high - low
                        while remaining:
                            block = array.array("h")
                            block.frombytes(source.readframes(min(65536, remaining)))
                            if not block or len(block) > remaining:
                                raise ValueError("truncated-quiet-pcm")
                            if sys.byteorder != "little":
                                block.byteswap()
                            energy += sum(sample * sample for sample in block)
                            count += len(block)
                            remaining -= len(block)
            if count >= 16000 and mapped_ms >= 1000:
                rms = math.sqrt(energy / count) / 32768.0
                value = max(floor_dbfs, 20 * math.log10(rms)) if rms else floor_dbfs
                problem = "observed-inter-beep-pcm"
        except (OSError, ValueError, TypeError, KeyError, OverflowError, wave.Error, EOFError):
            problem = "invalid-far-pcm-or-clock"
            # Partial measurements cannot stand in for all validated segments.
            count, mapped_ms = 0, 0.0
    elif not valid_train:
        problem = "missing-or-invalid-beep-schedule"
    return {
        "farforeign_rms_dbfs": value,
        "quiet_duration_s": min(count / 16000.0, mapped_ms / 1000.0),
        "quiet_frames": count,
        "guard_ms": guard_ms,
        "beep_duration_ms": beep_ms,
        "floor_dbfs": floor_dbfs,
        "criteria": [
            criterion(
                "farforeign_rms_dbfs",
                value,
                -60.0,
                "dBFS",
                detail=f"{problem}; >=1s quiet PCM required; silence floor=-120dBFS",
            )
        ],
    }
