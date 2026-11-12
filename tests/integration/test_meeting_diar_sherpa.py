"""E13.M7 real role over the supplied read-only DION/synth calibration corpus.

Missing runtime/models/corpus skip explicitly. A present but sandbox-denied
strace/IPC run is a failure, not privacy/quality evidence. Run with -m engine.
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import time
from pathlib import Path
from typing import Any

import pytest
from tools.meeting_diar_check import RoleProcess, measure, model_paths, sealed_pcm, wav_blocks

pytestmark = [pytest.mark.engine, pytest.mark.usefixtures("filesystem_euid")]


@pytest.fixture(scope="module")
def inputs() -> tuple[Path, Path]:
    pytest.importorskip("sherpa_onnx", reason="Для диаризации нужен sherpa-onnx")
    names = ("ASTRA_VOICE_DIAR_MODELS", "ASTRA_VOICE_DIAR_CORPUS")
    if any(not os.environ.get(name) for name in names):
        pytest.skip("Задайте ASTRA_VOICE_DIAR_MODELS и ASTRA_VOICE_DIAR_CORPUS")
    models, corpus = (Path(os.environ[name]) for name in names)
    try:
        required = list(model_paths(models))
    except OSError:
        pytest.fail("Неполный явно заданный комплект моделей диаризации")
    required += [
        corpus / name / file for name in ("dion", "synth") for file in ("far.wav", "ref.rttm")
    ]
    if any(not path.is_file() for path in required):
        pytest.skip("Неполный комплект моделей или корпуса диаризации")
    return models, corpus


def test_dion_real_role_speaker_count_and_der(inputs: tuple[Path, Path]) -> None:
    models, corpus = inputs
    result = measure(corpus / "dion/far.wav", models, reference=corpus / "dion/ref.rttm")
    assert 1 <= result["speakers_total"] <= 3, result
    assert result["der_percent"] <= 26, result
    assert result["peak_rss_kb"] * 1024 <= 1_200_000_000, result


def test_synth_two_blocks_and_single_baseline(inputs: tuple[Path, Path]) -> None:
    models, corpus = inputs
    # Shorter CI cut tests the exact same linking path; the full 30 min and
    # 2-hour acceptance measurements are CLI runs, not repeated in pytest.
    kwargs: dict[str, Any] = {"reference": corpus / "synth/ref.rttm", "duration": 120}
    whole = measure(corpus / "synth/far.wav", models, block_s=120, **kwargs)
    blocked = measure(corpus / "synth/far.wav", models, block_s=60, **kwargs)
    assert 3 <= blocked["speakers_total"] <= 5, blocked
    assert blocked["der_percent"] <= whole["der_percent"] + 3, (whole, blocked)


def _trace_arguments(text: str) -> list[str]:
    """Split strace arguments without splitting strings, structs or fd labels."""
    parts: list[str] = []
    start = 0
    nesting = 0
    quoted = escaped = annotation = False
    for index, char in enumerate(text):
        if escaped:
            escaped = False
        elif quoted:
            if char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif annotation:
            if char == ">" and (index == 0 or text[index - 1] != "-"):
                annotation = False
        elif char == "<":
            annotation = True
        elif char in "([{":
            nesting += 1
        elif char in ")]}":
            nesting -= 1
        elif char == "," and nesting == 0:
            parts.append(text[start:index].strip())
            start = index + 1
    parts.append(text[start:].strip())
    return parts


def _trace_fd(token: str) -> int | None:
    match = re.fullmatch(r"\s*(-?\d+)(?:<.*>(?:\(deleted\))?)?\s*", token)
    return int(match[1]) if match else None


_TRACE_MEMORY = r"\s*\d+</memfd:[^>]*>(?:\(deleted\))?\s*"
_TRACE_SOCKET = r"\s*\d+<(?:UNIX.*|socket:\[.*\])>\s*"
_TRACE_CONTROL = r"\s*\d+<(?:/dev/null|/proc/(?:self|\d+)/oom_score_adj)>\s*"
_TRACE_WRITES = {"write", "pwrite64", "writev"}
_TRACE_OUTPUTS = {
    "sendfile": 0,
    "copy_file_range": 2,
    "splice": 2,
    "tee": 1,
    "pwritev": 0,
    "pwritev2": 0,
    "ftruncate": 0,
    "fallocate": 0,
}


class _TraceDescriptors:
    """Proven memory fds; shared tables and conservative unknown clone groups."""

    def __init__(self) -> None:
        self.tables: dict[str, set[int]] = {}
        self.touched: dict[str, set[int]] = {}
        self.uncertain: dict[str, set[str]] = {}

    def table(self, pid: str) -> set[int]:
        self.touched.setdefault(pid, set())
        self.uncertain.setdefault(pid, {pid})
        return self.tables.setdefault(pid, set())

    def revoke(self, pid: str, descriptor: int) -> None:
        self.table(pid)
        self.touched[pid].add(descriptor)
        for peer in self.uncertain[pid]:
            self.tables.get(peer, set()).discard(descriptor)

    def replace(self, pid: str, descriptor: int, memory: bool) -> None:
        self.revoke(pid, descriptor)
        if memory:
            self.tables[pid].add(descriptor)

    def child(self, pid: str, child: str, *, shared: bool, known: bool) -> None:
        parent_table = self.table(pid)
        # The child can run before strace prints the parent's resumed clone.
        child_table = self.table(child).copy()
        if known:
            inherited = parent_table if shared else parent_table.copy()
            inherited.difference_update(self.touched[child])
            inherited.update(child_table)
            self.tables[child] = inherited
        else:
            parent_table.clear()
        if shared or not known:
            group = self.uncertain[pid] | self.uncertain[child]
            for peer in group:
                self.uncertain[peer] = group

    def exit(self, pid: str) -> None:
        self.tables.pop(pid, None)
        self.touched.pop(pid, None)
        group = self.uncertain.pop(pid, set())
        group.discard(pid)


def trace_violations(text: str) -> list[str]:
    """Audit descriptor provenance even when nondumpable tracees hide -yy names.

    Successful memfd_create proves a memory fd in its PID. Close/replacement
    revokes it; dup copies provenance. Visible fork/clone flags copy or share
    the table. Undecodable clone flags revoke knowledge conservatively rather
    than assuming a shared or inherited fd is safe. Unknown writes still fail.
    /dev/null and /proc/*/oom_score_adj remain the explicit hardening controls.
    """
    violations: list[str] = []
    pending: dict[str, str] = {}
    descriptors = _TraceDescriptors()

    def memory(pid: str, token: str) -> bool:
        descriptor = _trace_fd(token)
        annotated = bool(re.fullmatch(_TRACE_MEMORY, token))
        table = descriptors.table(pid)
        if "<" in token and not annotated:
            # A contrary -yy annotation is stronger than stale numeric history.
            if descriptor is not None:
                descriptors.revoke(pid, descriptor)
        return annotated or (descriptor is not None and descriptor in table)

    def output_allowed(pid: str, name: str, args: list[str]) -> bool:
        position = 0 if name in _TRACE_WRITES else _TRACE_OUTPUTS.get(name)
        if position is not None:
            if len(args) <= position:
                return False
            token = args[position]
            known_memory = memory(pid, token)
            if name not in _TRACE_WRITES:
                # New transfer/truncation calls require own traced provenance
                # (or an explicit socket), not merely a printable memfd name.
                descriptor = _trace_fd(token)
                known_memory = descriptor is not None and descriptor in descriptors.table(pid)
            return (
                known_memory
                or bool(re.fullmatch(_TRACE_SOCKET, token))
                or (name in _TRACE_WRITES and bool(re.fullmatch(_TRACE_CONTROL, token)))
            )
        if name == "mmap" and len(args) >= 6:
            return not (
                "MAP_SHARED" in args[3]
                and "PROT_WRITE" in args[2]
                and "MAP_ANONYMOUS" not in args[3]
            ) or memory(pid, args[4])
        return False

    def check_pending(pid: str, body: str) -> None:
        partial = re.match(r"(\w+)\((.*)", body)
        if partial is None:
            violations.append("unfinished")
            return
        name, raw = partial.groups()
        read_open = (
            name in {"open", "openat", "openat2"}
            and "O_RDONLY" in raw
            and not re.search(r"\bO_(WRONLY|RDWR|CREAT|TRUNC)\b", raw)
        )
        metadata = name in {
            "close",
            "dup",
            "dup2",
            "dup3",
            "fcntl",
            "memfd_create",
            "fork",
            "vfork",
            "clone",
            "clone3",
        }
        if not (read_open or metadata or output_allowed(pid, name, _trace_arguments(raw.rstrip()))):
            violations.append("unfinished")

    for line in text.splitlines():
        match = re.match(r"\s*(\d+)\s+(.*)", line)
        if match is None:
            continue
        pid, body = match.groups()
        table = descriptors.table(pid)
        if body.startswith("+++ exited") or body.startswith("+++ killed"):
            if pid in pending:
                check_pending(pid, pending.pop(pid))
            descriptors.exit(pid)  # A reused PID must not inherit stale entries.
            continue
        if "<unfinished ...>" in body:
            prefix = body.split("<unfinished ...>")[0]
            pending[pid] = prefix
            # Revoke a potentially closed/replaced target before other threads
            # sharing CLONE_FILES can issue a write in an interleaved trace.
            partial = re.match(r"(close|dup2|dup3)\((.*)", prefix)
            if partial:
                arguments = _trace_arguments(partial[2].rstrip())
                position = 0 if partial[1] == "close" else 1
                if len(arguments) > position:
                    target = _trace_fd(arguments[position].rstrip(") "))
                    if target is not None:
                        descriptors.revoke(pid, target)
            continue
        if body.startswith("<... "):
            resumed = re.match(r"<\.\.\. (\w+) resumed>(.*)", body)
            if resumed is None or pid not in pending:
                violations.append("unresolved-resume")
                continue
            prefix = pending.pop(pid)
            if not prefix.startswith(resumed[1] + "("):
                violations.append("unresolved-resume")
                continue
            body = prefix + resumed[2]
        call = re.match(r"(\w+)\((.*)\)\s*=\s*(.*)", body)
        if call is None:
            continue
        name, raw, result = call.groups()
        args = _trace_arguments(raw)
        returned = re.match(r"(-?\d+)(?=<|\s|$)", result)
        value = int(returned[1]) if returned else -1
        if name == "memfd_create" and value >= 0:
            descriptors.replace(pid, value, True)
        elif name == "close":
            descriptor = _trace_fd(args[0])
            if descriptor is not None:
                descriptors.revoke(pid, descriptor)
        elif name in {"dup", "dup2", "dup3", "fcntl"} and value >= 0:
            if name != "fcntl" or (len(args) > 1 and args[1] in {"F_DUPFD", "F_DUPFD_CLOEXEC"}):
                source = _trace_fd(args[0])
                inherited = source is not None and source in table
                descriptors.replace(pid, value, inherited)
        elif name in {"fork", "vfork", "clone", "clone3"} and value > 0:
            descriptors.child(
                pid,
                str(value),
                shared="CLONE_FILES" in raw,
                known=name in {"fork", "vfork"} or "flags=" in raw,
            )
        elif name in {"open", "openat", "openat2", "creat"}:
            if value >= 0:
                descriptors.replace(pid, value, False)
            if name == "creat" or re.search(r"\bO_(WRONLY|RDWR|CREAT|TRUNC)\b", raw):
                paths = re.findall(r'"([^\"]+)"', raw)
                if not paths or not (
                    paths[0] == "/dev/null"
                    or re.fullmatch(r"/proc/(?:self|\d+)/oom_score_adj", paths[0])
                ):
                    violations.append("write-open")
        elif name in _TRACE_WRITES or name in _TRACE_OUTPUTS:
            if not output_allowed(pid, name, args):
                violations.append("file-write")
        elif name == "mmap" and not output_allowed(pid, name, args):
            violations.append("shared-file-write")
    for pid, body in pending.items():
        check_pending(pid, body)
    return violations


def trace_run(inputs: tuple[Path, Path], trace: Path, *, killed: bool = False) -> None:
    if shutil.which("strace") is None:
        pytest.skip("strace не установлен")
    models, corpus = inputs
    trace.parent.mkdir(parents=True, exist_ok=True)
    if not killed:
        measure(corpus / "dion/far.wav", models, duration=30, trace=trace)
    else:
        with RoleProcess(trace=trace) as role:
            role.load(models)
            audio = next(wav_blocks(corpus / "synth/far.wav", 0, 1200, 1200))
            descriptor = sealed_pcm(audio)
            try:
                role.send(
                    "diar.block",
                    descriptor=descriptor,
                    block_index=0,
                    offset_ms=0,
                    pcm_fd=descriptor,
                    block_s=len(audio) / 16000,
                    expected_rtf=0.1,
                )
            finally:
                os.close(descriptor)
            role.wait("meeting.ack", 1)
            time.sleep(0.2)
            # process is strace when tracing; target only its own traced child.
            children = (
                Path(f"/proc/{role.process.pid}/task/{role.process.pid}/children")
                .read_text()
                .split()
            )
            assert len(children) == 1
            os.kill(int(children[0]), signal.SIGKILL)
            role.process.wait(timeout=5)
    text = trace.read_text()
    assert "openat(" in text and "write(" in text, "Пустой strace не доказывает T-415"
    assert trace_violations(text) == []


def test_strace_no_files_written_by_real_role(inputs: tuple[Path, Path]) -> None:
    root = Path(__file__).resolve().parents[2] / ".artifacts/e13-m7/tmp"
    trace_run(inputs, root / "diar-integration.trace")


def test_kill9_mid_block_leaves_no_files(inputs: tuple[Path, Path]) -> None:
    # Auditing all write/open syscalls also covers files outside a test folder,
    # and deleted files; no inference from an empty output directory alone.
    root = Path(__file__).resolve().parents[2] / ".artifacts/e13-m7/tmp"
    trace_run(inputs, root / "diar-kill9.trace", killed=True)


def test_cancel_during_native_block_exits_within_one_second(inputs: tuple[Path, Path]) -> None:
    models, corpus = inputs
    with RoleProcess() as role:
        role.load(models)
        audio = next(wav_blocks(corpus / "synth/far.wav", 0, 1200, 1200))
        descriptor = sealed_pcm(audio)
        try:
            role.send(
                "diar.block",
                descriptor=descriptor,
                block_index=0,
                offset_ms=0,
                pcm_fd=descriptor,
                block_s=1200,
                expected_rtf=1.0,
            )
        finally:
            os.close(descriptor)
        role.wait("meeting.ack", 1)
        time.sleep(0.2)
        started = time.monotonic()
        role.send("diar.cancel")
        assert role.wait("diar.cancelled", 1)["type"] == "diar.cancelled"
        assert role.process.wait(timeout=max(0.01, 1 - (time.monotonic() - started))) == 0
        assert time.monotonic() - started <= 1
