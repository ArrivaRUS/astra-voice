"""Bounded stand-only statistical profiler; output is JSON, never pstats.

Python stacks are wall-time observations. Linux per-thread CPU tick deltas are
attributed only to runnable threads at the END of each interval, not exact function
CPU timings. Short work followed by a wait can therefore be charged to the wait.
Native frames, GIL starvation, short-lived threads and tick quantisation limit
coverage. Separate process/sampler clocks and unassigned CPU expose these limits.
No per-call profile hooks, PCM, locals or thread names are collected. Fixed
section wrappers measure inclusive thread CPU exactly; nested times must not be summed.
"""

from __future__ import annotations

import functools
import json
import os
import runpy
import select
import signal
import stat
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path
from types import CodeType, FrameType, FunctionType
from typing import Any

FORMAT = "astra-capture-sampling-v1"
INTERVAL = 0.1
CHECKPOINT_INTERVAL = 1.0
MAX_STACKS = 1024
MAX_DEPTH = 16
MAX_THREADS = 256


# Exact, inclusive CPU measurements at a small fixed set of capture boundaries.
# Never import product code here: its normal imports occur after strict entry
# hardening. After the external start grant, patch only already loaded modules.
SECTION_TARGETS = {
    "astra_voice.worker.meeting_capture": (
        ("MeetingCaptureState", "poll"),
        ("MeetingCaptureState", "handle"),
        ("_Archive", "_pcm"),
        ("_Archive", "_checkpoint"),
    ),
    "astra_voice.worker.monitor_stream": (
        ("MonitorStreamSource", "read_chunk"),
        ("NativePulse", "read_chunk"),
        (None, "_sample_clock"),
    ),
    "astra_voice.worker.pulse_stream": (("PulseStreamSource", "read_chunk"),),
    "astra_voice.worker.meeting_session": (("MeetingSession", "tick"),),
    "astra_voice.meetings.store": (("MeetingWriter", "write"),),
}


class Sections:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.rows: dict[str, dict[str, Any]] = {}
        self.patched: list[tuple[Any, str, Any, Any]] = []
        for targets in SECTION_TARGETS.values():
            for owner, method in targets:
                label = f"{owner}.{method}" if owner else method
                self.rows[label] = {
                    "name": label,
                    "label": label,
                    "installed": False,
                    "calls": 0,
                    "exceptions": 0,
                    "cpu_seconds": 0.0,
                    "wall_seconds": 0.0,
                }

    def wrap(self, original: Callable[..., Any], label: str) -> Callable[..., Any]:
        @functools.wraps(original)
        def measured(*args: Any, **kwargs: Any) -> Any:
            cpu, wall = time.thread_time(), time.monotonic()
            failed = False
            try:
                return original(*args, **kwargs)
            except BaseException:
                failed = True
                raise
            finally:
                elapsed_cpu, elapsed_wall = time.thread_time() - cpu, time.monotonic() - wall
                with self.lock:
                    row = self.rows[label]
                    row["calls"] += 1
                    row["exceptions"] += int(failed)
                    row["cpu_seconds"] += elapsed_cpu
                    row["wall_seconds"] += elapsed_wall

        return measured

    def patch_loaded(self, name: str) -> None:
        targets = SECTION_TARGETS.get(name)
        module = sys.modules.get(name)
        if targets is None or module is None:
            return
        if getattr(getattr(module, "__spec__", None), "_initializing", False):
            return
        with self.lock:
            for owner_name, method in targets:
                label = f"{owner_name}.{method}" if owner_name else method
                if self.rows[label]["installed"]:
                    continue
                owner = vars(module).get(owner_name) if owner_name else module
                if owner is None:
                    continue
                original = vars(owner).get(method)
                if not isinstance(original, FunctionType):
                    continue
                wrapper = self.wrap(original, label)
                setattr(owner, method, wrapper)
                self.patched.append((owner, method, original, wrapper))
                self.rows[label]["installed"] = True

    def install(self) -> None:
        for name in SECTION_TARGETS:
            self.patch_loaded(name)

    def restore(self) -> None:
        with self.lock:
            for owner, method, original, wrapper in reversed(self.patched):
                if vars(owner).get(method) is wrapper:
                    setattr(owner, method, original)

    def snapshot(self) -> list[dict[str, Any]]:
        with self.lock:
            return [dict(row) for row in self.rows.values()]


class Sampler:
    def __init__(self, output: Path, *, max_stacks: int = MAX_STACKS) -> None:
        self.output = output
        self.sections = Sections()
        self.max_stacks = max_stacks
        self.started = time.monotonic()
        self.started_ticks = int(time.clock_gettime(time.CLOCK_BOOTTIME) * os.sysconf("SC_CLK_TCK"))
        self.cpu_started = time.process_time()
        self.tick_seconds = 1.0 / os.sysconf("SC_CLK_TCK")
        self.locations: dict[CodeType, str] = {}
        self.histogram: dict[tuple[str, ...], list[float]] = {}
        self.overflow = [0.0, 0.0]
        self.previous: dict[tuple[int, int], int] = {}
        self.thread_cpu: dict[tuple[int, int], float] = {}
        self.other_thread_cpu = 0.0
        self.unattributed_cpu = 0.0
        self.samples = 0
        self.missing_threads = 0
        self.errors: set[str] = set()
        self.sampler_cpu = 0.0
        self.sections_only = False
        self.max_interval = 0.0
        self.last_sample = self.started
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, name="stand-sampler", daemon=True)

    def record(self, stack: tuple[str, ...], cpu: float) -> None:
        row = self.histogram.get(stack)
        if row is None:
            if len(self.histogram) >= self.max_stacks:
                row = self.overflow
            else:
                row = self.histogram[stack] = [0.0, 0.0]
        row[0] += 1
        row[1] += cpu

    def stack(self, frame: FrameType) -> tuple[str, ...]:
        result: list[str] = []
        current: FrameType | None = frame
        while current is not None and len(result) < MAX_DEPTH:
            code = current.f_code
            # Source location only, never frame locals, source text or absolute paths.
            location = self.locations.get(code)
            if location is None:
                filename = code.co_filename.rsplit("/", 1)[-1][:100]
                location = f"{filename}:{code.co_firstlineno}:{code.co_name[:100]}"
                if len(self.locations) < MAX_STACKS:
                    self.locations[code] = location
            result.append(location)
            current = current.f_back
        return tuple(result)

    def sample(self) -> None:
        now = time.monotonic()
        self.max_interval = max(self.max_interval, now - self.last_sample)
        self.last_sample = now
        sampler_tid = threading.get_native_id()
        native_ids = {
            t.native_id: t.ident for t in threading.enumerate() if t.native_id is not None
        }
        frames = sys._current_frames()
        previous: dict[tuple[int, int], int] = {}
        seen_tids: set[int] = set()
        with os.scandir("/proc/self/task") as tasks:
            for index, task in enumerate(tasks):
                if index >= MAX_THREADS:
                    self.errors.add("thread-cap-reached")
                    break
                tid = int(task.name)
                seen_tids.add(tid)
                if tid == sampler_tid:
                    continue
                try:
                    with open(task.path + "/stat", "rb") as stat:
                        fields = stat.read().rsplit(b")", 1)[1].split()
                    ticks = int(fields[11]) + int(fields[12])
                    key = (tid, int(fields[19]))  # starttime prevents TID reuse attribution
                except (OSError, ValueError, IndexError):
                    self.missing_threads += 1
                    continue
                previous[key] = ticks
                # A new thread born during this profile has no prior CPU to
                # subtract. Counting from its first observation would lose its
                # startup work (often most of a short-lived capture thread).
                baseline = 0 if key[1] >= self.started_ticks else ticks
                delta = max(0, ticks - self.previous.get(key, baseline)) * self.tick_seconds
                ident = native_ids.get(tid)
                frame = frames.get(ident) if ident is not None else None
                if key in self.thread_cpu or len(self.thread_cpu) < MAX_THREADS:
                    self.thread_cpu[key] = self.thread_cpu.get(key, 0.0) + delta
                else:
                    self.other_thread_cpu += delta
                stack = self.stack(frame) if frame is not None else ("<native-or-unmapped-thread>",)
                # A CPU tick can be published after the work, while the thread
                # sleeps. Never label that tick as CPU spent in a waiting stack.
                attributed = delta if fields[0] == b"R" and frame is not None else 0.0
                self.unattributed_cpu += delta - attributed
                self.record((f"<thread:{tid}>", *stack), attributed)
        # Retain only currently observed threads, never all historical IDs/frames.
        # Keep a baseline when a listed task had a transient stat read failure.
        # Removed tasks are dropped, so state is bounded by live tasks.
        self.previous = {key: value for key, value in self.previous.items() if key[0] in seen_tids}
        self.previous.update(previous)
        self.samples += 1

    def snapshot(self, status: str) -> dict[str, Any]:
        return {
            "activation": "after-meeting.capture.started",
            "activated": self.sections_only or self.thread.ident is not None,
            "mode": "sections-only" if self.sections_only else "sampling-and-sections",
            "sampling_status": ("unavailable-thread-start" if self.sections_only else "enabled"),
            "sections_timing": "inclusive-do-not-sum-nested-sections",
            "sections": self.sections.snapshot(),
            "format": FORMAT,
            "process_id": os.getpid(),
            "started_monotonic_seconds": self.started,
            "status": status,
            "interval_seconds": INTERVAL,
            "checkpoint_interval_seconds": None if self.sections_only else CHECKPOINT_INTERVAL,
            "elapsed_seconds": time.monotonic() - self.started,
            "process_cpu_seconds": time.process_time() - self.cpu_started,
            "sampler_cpu_seconds": self.sampler_cpu,
            "cpu_tick_seconds": self.tick_seconds,
            "cpu_attribution": "approximate-running-stack-only",
            "limitations": [
                "CPU ticks are quantized; runnable end-of-interval stacks are approximate",
                "CPU of currently sleeping or unmapped threads is unattributed",
                "Python stacks only; native work has no native call stack",
                "GIL starvation and short-lived threads can cause missing CPU samples",
                "Old threads need a baseline; exited-between-samples CPU is missing",
                "Process CPU includes sampler and unsampled work after activation",
                "Sections-only fallback writes counters at orderly exit, not periodic checkpoints",
            ],
            "threads": [
                {"native_tid": tid, "start_ticks": start, "cpu_seconds": cpu}
                for (tid, start), cpu in self.thread_cpu.items()
            ],
            "other_thread_cpu_seconds": self.other_thread_cpu,
            "unattributed_cpu_seconds": self.unattributed_cpu,
            "samples": self.samples,
            "max_observed_interval_seconds": self.max_interval,
            "missing_thread_reads": self.missing_threads,
            "errors": sorted(self.errors),
            "limits": {"stacks": self.max_stacks, "depth": MAX_DEPTH, "threads": MAX_THREADS},
            "overflow": {"wall_samples": int(self.overflow[0]), "cpu_seconds": self.overflow[1]},
            "stacks": [
                {"frames": stack, "wall_samples": int(row[0]), "cpu_seconds": row[1]}
                for stack, row in self.histogram.items()
            ],
        }

    def dump(self, status: str) -> None:
        atomic_dump(self.output, self.snapshot(status))

    def _run(self) -> None:
        cpu_started = time.thread_time()
        checkpoint = time.monotonic()
        try:
            while not self.stop_event.is_set():
                self.sample()
                self.sampler_cpu = time.thread_time() - cpu_started
                if time.monotonic() >= checkpoint:
                    try:
                        self.dump("running")
                    except OSError:
                        self.errors.add("checkpoint-write-failed")
                    checkpoint = time.monotonic() + CHECKPOINT_INTERVAL
                self.stop_event.wait(INTERVAL)
        except Exception:
            self.errors.add("sampler-failed")
        finally:
            self.sampler_cpu = time.thread_time() - cpu_started
            if self.errors:
                try:
                    self.dump("partial")
                except OSError:
                    pass

    def start(self) -> None:
        # Only the sampling thread writes running snapshots; the final writer
        # joins it first. This avoids concurrent histogram/snapshot iteration.
        self.dump("activating")
        try:
            self.thread.start()
        except (RuntimeError, OSError, MemoryError):
            # The hardened worker may have no address-space budget for another
            # native thread. Keep already-installed exact section counters;
            # never retry, relax limits or create a replacement writer thread.
            if self.thread.ident is not None:
                raise
            self.sections_only = True
            self.dump("running")

    def close(self) -> None:
        self.stop_event.set()
        if self.thread.ident is not None:
            self.thread.join(timeout=2)
        if self.thread.is_alive():
            raise RuntimeError("sampler-stop-timeout")
        self.dump("complete" if not self.errors else "partial")


def atomic_dump(output: Path, data: dict[str, Any]) -> None:
    descriptor, filename = tempfile.mkstemp(prefix=".av-capture-profile-", dir=output.parent)
    temporary = Path(filename)
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(data, stream, separators=(",", ":"))
            stream.write("\n")
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)


class Activation:
    """Stand-only external grant. No sampler, wrappers or product imports while waiting.

    The owner writes a PID-bound grant in its 0700 workspace, then sends SIGUSR1
    only after receiving the correlated started event. The signal alone grants
    nothing. No inherited FD or product protocol/hardening change is needed.
    """

    def __init__(self, output: Path, gate: Path) -> None:
        self.output, self.gate = output, gate
        self.sampler: Sampler | None = None
        self.errors: set[str] = set()
        self.attempted = False
        self.closed = False
        self.requested = False
        self.original_select = select.select
        self.control_ident = threading.get_ident()

    def dump_waiting(self, status: str) -> None:
        atomic_dump(
            self.output,
            {
                "format": FORMAT,
                "process_id": os.getpid(),
                "status": status,
                "activation": "after-meeting.capture.started",
                "activated": False,
                "samples": 0,
                "sections": [],
                "stacks": [],
                "threads": [],
                "errors": sorted(self.errors),
            },
        )

    def request(self, signum: int, frame: FrameType | None) -> None:
        self.requested = True

    def select(self, *args: Any, **kwargs: Any) -> Any:
        if self.requested and not self.closed and threading.get_ident() == self.control_ident:
            self.requested = False
            self.activate()
        return self.original_select(*args, **kwargs)

    def activate(self) -> None:
        if self.attempted or self.closed:
            return
        try:
            fd = os.open(self.gate, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            try:
                info = os.fstat(fd)
                valid = (
                    stat.S_ISREG(info.st_mode)
                    and info.st_uid == os.getuid()
                    and info.st_mode & 0o777 == 0o600
                    and info.st_nlink == 1
                )
                grant = os.read(fd, 64) if valid else b""
            finally:
                os.close(fd)
        except OSError:
            return
        if grant != f"start:{os.getpid()}".encode():
            return
        # Called at the next control select boundary, outside capture locks.
        # The signal handler itself only sets a flag.
        self.attempted = True
        select.select = self.original_select
        try:
            self.sampler = Sampler(self.output)
            self.sampler.sections.install()
            self.sampler.start()
        except Exception:
            self.errors.add("capture-profile-activation-failed")
            if self.sampler is not None:
                self.sampler.errors.update(self.errors)
                self.sampler.sections.restore()
                self.sampler.stop_event.set()
            try:
                self.dump_waiting("activation-failed")
            except OSError:
                pass

    def close(self) -> None:
        self.closed = True
        if self.sampler is None:
            self.dump_waiting("activation-failed" if self.attempted else "not-started")
            return
        self.sampler.errors.update(self.errors)
        self.sampler.sections.restore()
        self.sampler.close()


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 5 or args[3] != "meeting-capture":
        return 2
    output, gate, bootstrap, role, descriptor = args
    activation = Activation(Path(output), Path(gate))
    try:
        activation.dump_waiting("waiting-for-start")
    except Exception:
        print("capture-profile-start-failed", file=sys.stderr)
        return 2
    previous_signal = signal.signal(signal.SIGUSR1, activation.request)
    select.select = activation.select
    previous_argv = sys.argv
    sys.argv = [bootstrap, role, descriptor]
    try:
        runpy.run_path(bootstrap, run_name="__main__")
    except SystemExit as exc:
        if exc.code not in (None, 0):
            activation.errors.add("capture-profile-bootstrap-exit")
        raise
    except Exception:
        activation.errors.add("capture-profile-bootstrap-failed")
        print("capture-profile-bootstrap-failed", file=sys.stderr)
        return 2
    finally:
        sys.argv = previous_argv
        select.select = activation.original_select
        activation.closed = True
        # Keep our inert handler until exit: a queued duplicate grant cannot
        # terminate the process while its final checkpoint is being written.
        try:
            activation.close()
        except Exception:
            print("capture-profile-final-write-failed", file=sys.stderr)
        finally:
            signal.signal(signal.SIGUSR1, previous_signal)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
