"""Real MeetingJobs/supervisor driver; only ASR is deterministic (M7).

The child entry point configures the same ASR/VAD seam as the transcription
integration driver. DiarProcess, its isolated launcher and model trust stay real.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import traceback
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, TextIO
from uuid import uuid4

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from astra_voice.core.meeting_jobs import MeetingJobs, ModelAdmission
from astra_voice.meetings.diar_models import installed_diar_models
from astra_voice.meetings.model import Checkpoint, Meeting, Transcript
from astra_voice.meetings.store import MeetingStore
from astra_voice.worker.meeting_transcribe import (
    MeetingTranscribeState,
    _Cancelled,
    _Job,
    energy_probabilities,
)
from astra_voice.worker.supervisor import WorkerSupervisor
from tools.meeting_transcribe_diar_check import CheckEngine, installed_pair, load_installed_pair

if TYPE_CHECKING:
    from helpers.meeting_trust import SignedDiarPair

LOAD = {
    "id": "gigaam-v3-e2e-rnnt-int8",
    "revision": "rediar-integration",
    "dir": "/fake-asr",
    "layout": "onnx-asr-gigaam-v3",
    "variant": "gigaam-v3-e2e-rnnt",
    "threads": 2,
    "min_ram_mb": 0,
}
WORK = "rediar-transcript.json"
CHECK = "rediar-checkpoint.json"


def log_stderr(journal: TextIO, name: str, path: Path, code: int) -> None:
    """Bound diagnostic output; fd2 remains suppressed by the real role entry point."""
    with path.open("rb") as stream:
        stream.seek(max(0, os.fstat(stream.fileno()).st_size - 8192))
        tail = stream.read().decode("utf-8", errors="replace")
    journal.write(f"{name}_exit={code}; stderr={path}\n{tail or '<empty>'}\n")
    journal.flush()


def trace_command(command: Sequence[str], trace: Path) -> list[str]:
    """Wrap the final Python argv, AFTER DiarProcess inserts its -B flag."""
    from tools.meeting_diar_check import DIAR_TRACE_CALLS

    return [
        "strace",
        "-f",
        "-qq",
        "-yy",
        "-s",
        "256",
        "-o",
        str(trace),
        "-e",
        f"trace={DIAR_TRACE_CALLS}",
        *command,
    ]


class LoggedRoleProcess(subprocess.Popen[bytes]):
    """Actual Popen with private stderr diagnostics on a confirmed nonzero exit."""

    def __init__(
        self,
        command: Sequence[str],
        *,
        journal: TextIO,
        journal_path: Path,
        **options: Any,
    ) -> None:
        self.journal = journal
        self.stderr_path = journal_path.with_name(f"diar-{uuid4().hex}.stderr")
        self.reported_exit = False
        descriptor = os.open(
            self.stderr_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
        )
        options["stderr"] = descriptor
        try:
            super().__init__(command, **options)
        except Exception:
            traceback.print_exc(file=journal)
            journal.flush()
            raise
        finally:
            os.close(descriptor)

    def wait(self, timeout: float | None = None) -> int:
        result = super().wait(timeout=timeout)
        if result != 0 and not self.reported_exit:
            self.reported_exit = True
            log_stderr(self.journal, "diar", self.stderr_path, result)
        return result


class RoleSpawn:
    """Only observe diar spawn; keep all product environment/fd/session options."""

    def __init__(self, journal: TextIO, trace: Path | None, *, journal_path: Path) -> None:
        self.journal, self.trace = journal, trace
        self.journal_path = journal_path

    def __getattr__(self, name: str) -> Any:
        return getattr(subprocess, name)

    def Popen(self, command: Sequence[str], **options: Any) -> LoggedRoleProcess:
        argv = list(command) if self.trace is None else trace_command(command, self.trace)
        return LoggedRoleProcess(
            argv,
            journal=self.journal,
            journal_path=self.journal_path,
            **options,
        )


class LoggedTranscribeState(MeetingTranscribeState):
    """Observe failures before the product converts them into an opaque io event."""

    def __init__(
        self,
        *,
        store: MeetingStore,
        journal: TextIO,
        trusted_pair: SignedDiarPair | None = None,
    ) -> None:
        self.journal = journal
        super().__init__(
            store=store,
            diar_models_provider=trusted_pair.models if trusted_pair is not None else None,
            engine_factory=lambda layout: CheckEngine(),
            vad_factory=lambda: ("energy-rms", energy_probabilities),
        )

    def _job(self, job: _Job) -> None:
        try:
            super()._job(job)
        except _Cancelled:
            raise
        except Exception:
            traceback.print_exc(file=self.journal)
            self.journal.flush()
            raise


def role_state(
    *,
    store_root: Path,
    journal: TextIO,
    trusted_pair: SignedDiarPair | None = None,
) -> MeetingTranscribeState:
    """Shared by the child entry point and the IPC-free preparation regression."""
    return LoggedTranscribeState(
        store=MeetingStore(store_root),
        journal=journal,
        trusted_pair=trusted_pair,
    )


@contextmanager
def prepared_queue(
    store: MeetingStore,
    mid: str,
    models: dict[str, str],
    *,
    deadline: float,
) -> Iterator[QueueRun]:
    with installed_pair(store.root.parent / "diar-trust", models) as pair:
        run = QueueRun(store, mid, models, deadline=deadline, trusted_pair=pair)
        try:
            yield run
        finally:
            run.close()


class QueueRun:
    """Drive actual supervisor IPC and queue ticks without a GUI/Qt session."""

    def __init__(
        self,
        store: MeetingStore,
        mid: str,
        models: dict[str, str],
        *,
        retention: int = 7,
        trace: Path | None = None,
        deadline: float | None = None,
        trusted_pair: SignedDiarPair | None = None,
    ) -> None:
        self.trusted_pair = trusted_pair
        self.store, self.mid, self.trace = store, mid, trace
        self.deadline = deadline if deadline is not None else time.monotonic() + 75
        self.roles: list[WorkerSupervisor] = []
        self.events: list[dict[str, Any]] = []
        self.journals: list[Path] = []
        self.watch_published: tuple[bytes, bytes] | None = None
        self.jobs = MeetingJobs(
            store,
            self.factory,
            audio_retention_days=lambda: retention,
            model_probe=lambda: ModelAdmission("ok", dict(LOAD)),
            diar_models=trusted_pair.models if trusted_pair is not None else installed_diar_models,
            echo_gate=False,
        )

    def factory(
        self,
        on_event: Callable[[dict[str, Any]], None],
        on_exit: Callable[[int], None],
    ) -> WorkerSupervisor:
        def event(message: dict[str, Any]) -> None:
            if self.watch_published is not None:
                assert self.published() == self.watch_published
                if message["type"] == "meeting.job.done":
                    self.watch_published = None  # publication can start in on_event
            self.events.append(message)
            on_event(message)

        role = WorkerSupervisor(
            event,
            on_exit=on_exit,
            use_qt=False,
            role="meeting-transcribe",
            command_factory=self.command,
        )
        self.roles.append(role)
        return role

    def command(self, fd: int) -> list[str]:
        journal = self.store.root.parent / f"rediar-role-{uuid4().hex}.log"
        self.journals.append(journal)
        return [
            sys.executable,
            "-I",
            "-B",
            str(Path(__file__).resolve()),
            str(fd),
            str(self.trace) if self.trace else "",
            str(self.trusted_pair.root) if self.trusted_pair is not None else "",
            str(self.store.root),
            str(journal),
        ]

    def diagnostics(self) -> str:
        parts = [f"events={self.events[-4:]}"]
        for path in self.journals:
            try:
                with path.open("rb") as stream:
                    stream.seek(max(0, os.fstat(stream.fileno()).st_size - 8192))
                    tail = stream.read().decode("utf-8", errors="replace")
            except OSError as error:
                tail = f"journal unavailable: {error}"
            parts.append(f"role journal {path}:\n{tail}")
        return "\n".join(parts)

    def step(self) -> None:
        assert time.monotonic() < self.deadline, (
            f"75s queue deadline: redo={self.jobs.redo_results}, "
            f"failures={self.jobs.failures}; {self.diagnostics()}"
        )
        if self.watch_published is not None:
            assert self.published() == self.watch_published
        for role in tuple(self.roles):
            role.pump()
        self.jobs.tick()
        assert not self.jobs.failures, f"Queue failed: {self.jobs.failures}; {self.diagnostics()}"
        time.sleep(0.01)

    def until(self, predicate: Callable[[], bool]) -> None:
        while not predicate():
            self.step()

    def meeting(self) -> Meeting:
        value = self.store.read(self.mid, "meeting.json")
        assert isinstance(value, Meeting)
        return value

    def transcript(self) -> Transcript:
        value = self.store.read(self.mid, "transcript.json")
        assert isinstance(value, Transcript)
        return value

    def published(self) -> tuple[bytes, bytes]:
        root = self.store.root / self.mid
        return (root / "transcript.json").read_bytes(), (root / "speakers.json").read_bytes()

    def child_at_boundary(self, *, redo: bool = True) -> tuple[int, Checkpoint]:
        """Observe a real child after the durable running-diar checkpoint.

        /proc enumeration is observational: no fake role or inferred PID. The
        driver requires both the checkpoint and the live child, without
        introducing a fake diar factory or a timing sleep inside the role.
        """
        filename = CHECK if redo else "checkpoint.json"
        while True:
            self.step()
            for role in self.roles:
                parent = role.process
                if parent is None or parent.poll() is not None:
                    continue
                # The executor thread owns Popen; Linux children are per TID.
                tasks = Path(f"/proc/{parent.pid}/task")
                pids = {
                    pid for path in tasks.glob("*/children") for pid in path.read_text().split()
                }
                for pid in pids:
                    command = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
                    if b"meeting-diar" not in command:
                        continue
                    checkpoint = self.store.read(self.mid, filename)
                    if isinstance(checkpoint, Checkpoint) and checkpoint.diar.status == "running":
                        return int(pid), checkpoint
            terminal = "meeting.job.done" if not redo else "meeting.job.failed"
            assert not any(e["type"] == terminal for e in self.events), self.events[-4:]

    def start_redo(self) -> None:
        self.watch_published = self.published()
        self.jobs.rediarize(self.mid)

    def finish_redo(self) -> None:
        self.until(
            lambda: (
                self.mid in self.jobs.redo_results
                and not self.jobs.busy
                and self.jobs._pending == 0
            )
        )
        assert self.jobs.redo_results[self.mid] == "done", (
            f"redo={self.jobs.redo_results}; {self.diagnostics()}"
        )

    def close(self) -> None:
        assert self.jobs.shutdown(timeout=3), "metadata writer did not stop"
        assert all(role.exited for role in self.roles), "transcriber was not reaped"


def child_main() -> int:
    import functools

    os.sched_setaffinity(0, sorted(os.sched_getaffinity(0))[:4])

    from astra_voice.worker import meeting_diar_client, meeting_transcribe
    from astra_voice.worker.main import meeting_main

    # fd2 is silenced by the strict role entry point. Keep an independent
    # private journal fd, including exceptions before _run_job swallows them.
    journal_path = Path(sys.argv[5])
    descriptor = os.open(journal_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as journal:
        journal.write(f"store_root={sys.argv[4]}\n")
        journal.flush()
        # Instrument the spawn boundary, not worker_command: the product has
        # already inserted Python's -B here. The stdlib subprocess module stays
        # unchanged, and the real DiarProcess still owns all IPC/lease handling.
        meeting_diar_client.subprocess = RoleSpawn(  # type: ignore[attr-defined,assignment]
            journal,
            Path(sys.argv[2]) if sys.argv[2] else None,
            journal_path=journal_path,
        )
        stderr_path = Path(str(journal_path) + ".stderr")
        stderr_fd = os.open(
            stderr_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
        )
        try:
            os.dup2(stderr_fd, 2)
        finally:
            os.close(stderr_fd)
        meeting_transcribe.MeetingTranscribeState = functools.partial(  # type: ignore[misc,assignment]
            role_state,
            store_root=Path(sys.argv[4]),
            trusted_pair=load_installed_pair(Path(sys.argv[3])) if sys.argv[3] else None,
            journal=journal,
        )
        try:
            result = meeting_main("meeting-transcribe", [sys.argv[1]])
        except Exception:
            traceback.print_exc(file=journal)
            raise
        finally:
            journal.flush()
        if result != 0:
            log_stderr(journal, "role", stderr_path, result)
        else:
            journal.write(f"role_exit={result}\n")
        return result


if __name__ == "__main__":
    raise SystemExit(child_main())
