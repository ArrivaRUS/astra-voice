"""Read-only, stdlib-only evidence for the E13.M2 isolated recording stand.

Architecture §5.7 inherits SP1 S1.5c's mean CPU budget (one core). Peak is
reported separately. Hourly rates from short samples are projections; they do
not satisfy the sixty-minute soak. Missing evidence always remains unavailable.
No imports of production capture code or experimental spikes are permitted here.
"""

from __future__ import annotations

import array
import json
import math
import os
import re
import stat
import sys
import wave
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MIB = 1024**2
JSON = dict[str, Any]


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
                metadata = (Path(parent) / name).lstat()
                if stat.S_ISLNK(metadata.st_mode):
                    return None
                if stat.S_ISREG(metadata.st_mode):
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
            "disk", disk, 230.0, "MiB/h", detail="logical file-byte growth, including sidecars"
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


def sample_to_monotonic(side: JSON, sample: int) -> float:
    """Map one epoch's absolute sample, with no extrapolation or gap crossing."""
    _validate_sidecar(side)
    if (
        type(sample) is not int
        or side["discontinuity"]
        or not side["sample_start"] <= sample <= side["sample_end"]
    ):
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
                    if absolute - last_loud > 640:
                        beeps.append(absolute)
                    last_loud = absolute
            position += len(block)
        if position != count or (side["closed"] and path.stat().st_size != 44 + position * 2):
            raise ValueError("truncated-or-extra-pcm")
    return side, beeps


def _startup_placeholder(side: JSON) -> bool:
    """Only the proven empty monotonic-to-server startup transition is skippable.

    Call after PCM and sidecar validation. A nonempty discontinuous epoch still
    has no usable clock; an all-placeholder capture still has no positive PCM.
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
    ends: dict[str, list[float]] = {"mic": [], "far": []}
    durable: dict[str, list[float]] = {"mic": [], "far": []}
    indexes: dict[str, list[int]] = {"mic": [], "far": []}
    frames = {"mic": 0, "far": 0}
    invalid = False
    clock_unknown = False
    beep_clock_unknown = False
    files = sorted(audio_root.glob("*.wav")) if not audio_root.is_symlink() else []
    for path in files:
        try:
            side, beeps = _read_segment(path, crash=crash_mono_ms is not None)
            track = side["track"]
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
                "valid": True,
            }
            if _startup_placeholder(side):
                item["clock_status"] = "empty-startup-placeholder"
                items.append(item)
                continue
            try:
                # Legacy checkpoints could be written after the last clock
                # knot. Bound loss by observed durable PCM, without inventing
                # a clock for the remaining tail or joining different epochs.
                last_sample = min(side["sample_end"], side["timeline"][-1][0])
                ends[track].append(sample_to_monotonic(side, last_sample))
                if last_sample < side["sample_end"]:
                    item["clock_status"] = "partial"
                    beep_clock_unknown = True
                checkpoint_sample = side["sample_start"] + side["frames_at_checkpoint"]
                durable_sample = min(checkpoint_sample, last_sample)
                if durable_sample > side["sample_start"]:
                    durable[track].append(sample_to_monotonic(side, durable_sample))
                for sample in beeps:
                    if sample < side["frames"]:
                        if side["sample_start"] + sample <= last_sample:
                            mapped[track].append(
                                sample_to_monotonic(side, side["sample_start"] + sample)
                            )
                        else:
                            beep_clock_unknown = True
            except ValueError:
                clock_unknown = True
                item["clock_status"] = "unavailable"
            items.append(item)
        except (OSError, ValueError, TypeError, KeyError, OverflowError, wave.Error, EOFError):
            invalid = True
            items.append({"file": path.name, "valid": False, "error": "invalid-segment-or-sidecar"})
    if any(sorted(values) != list(range(len(values))) for values in indexes.values()):
        invalid = True
    if all(indexes.values()) and not all(frames.values()):
        invalid = True  # an empty WAV header does not prove recording survived
    segments = 1.0 if invalid else (0.0 if all(indexes.values()) else None)
    expected = list(expected_beeps_ms)
    beep_known = (
        not invalid
        and not clock_unknown
        and not beep_clock_unknown
        and len(expected) >= 2
        and all(_finite(value) for value in expected)
        and all(b > a for a, b in zip(expected, expected[1:], strict=False))
        and all(len(mapped[track]) == len(expected) for track in mapped)
    )
    initial: float | None = None
    drift: float | None = None
    timecodes: float | None = None
    if beep_known:
        for values in mapped.values():
            values.sort()
        offsets = [mic - far for mic, far in zip(mapped["mic"], mapped["far"], strict=True)]
        initial = offsets[0]
        drift = max(abs(value - initial) for value in offsets)
        try:
            timecodes = max(
                abs(_recorded(intervals, actual) - _recorded(intervals, reference))
                for track in mapped
                for actual, reference in zip(mapped[track], expected, strict=True)
            )
        except (ValueError, KeyError, TypeError):
            pass
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
                    detail="max mic/far clock offset change from first beep",
                ),
                criterion(
                    "timecodes",
                    timecodes,
                    1000.0,
                    "ms",
                    detail="beep recorded axis vs playback schedule",
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
        "beep_drift_ms": drift,
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
