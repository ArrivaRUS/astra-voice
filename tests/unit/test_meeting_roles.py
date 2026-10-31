"""Role skeletons: pure stubs, strict startup and no audio/model effects."""

from __future__ import annotations

import os
import resource
import select
import signal
import socket
from unittest.mock import Mock

import pytest
from test_meeting_ipc import COMMANDS, command

from astra_voice import bootstrap
from astra_voice.worker import main as worker_main
from astra_voice.worker.meeting_roles import MeetingRole, run_role

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def no_parent_stderr_redirection(monkeypatch: pytest.MonkeyPatch) -> None:
    """The entry point runs in pytest here; never redirect the runner's fd2."""
    from astra_voice.worker import meeting_roles

    monkeypatch.setattr(meeting_roles, "silence_native_stderr", lambda: True)


@pytest.mark.parametrize("role", COMMANDS)
def test_role_commands_are_correlated_stubs_and_idempotent(role: str) -> None:
    handler = MeetingRole(role, clock=lambda: 10.0)
    for kind in COMMANDS[role]:
        msg = command(role, kind, kind.replace(".", "_"))
        response = handler.handle(msg)
        assert response and all(
            r["req"] == msg["req"] and r["request_type"] == kind for r in response
        )
        assert handler.handle(msg) == response
        assert all(
            r["code"] in {"not-prepared", "admission-stale"}
            for r in response
            if r["type"] == "error"
        )
    original = command(role, "ping", "same")
    handler.handle(original)
    other = command(role, next(iter(COMMANDS[role])), "same")
    assert handler.handle(other)[0]["code"] == "req-conflict"


def test_capture_shutdown_bye_and_zero_without_audio() -> None:
    role = MeetingRole("meeting-capture")
    assert (
        role.handle(command(role.role, "meeting.capture.shutdown"))[0]["type"]
        == "meeting.capture.bye"
    )
    assert role.stopped


@pytest.mark.parametrize("phase", ["pdeath", "core", "oom", "dumpable", "parent"])
def test_strict_hardening_fails_closed(monkeypatch: pytest.MonkeyPatch, phase: str) -> None:
    monkeypatch.setattr(os, "getppid", lambda: 123 if phase != "parent" else 1)

    def prctl(option: int, value: int) -> None:
        if (phase == "pdeath" and option == worker_main.PR_SET_PDEATHSIG) or (
            phase == "dumpable" and option == worker_main.PR_SET_DUMPABLE
        ):
            raise OSError

    monkeypatch.setattr(worker_main, "_prctl", prctl)
    limit = Mock(side_effect=OSError if phase == "core" else None)
    monkeypatch.setattr(resource, "setrlimit", limit)
    monkeypatch.setattr(worker_main, "set_oom_score_adj", Mock(return_value=phase != "oom"))
    assert not worker_main.harden_process(123, strict=True, oom_score=300)


def test_strict_hardening_pdeathsig_capture(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(worker_main, "_get_dumpable", lambda: 0)
    monkeypatch.setattr(resource, "getrlimit", lambda limit: (0, 0))
    prctl = Mock()
    monkeypatch.setattr(worker_main, "_prctl", prctl)
    monkeypatch.setattr(os, "getppid", lambda: 123)
    monkeypatch.setattr(resource, "setrlimit", Mock())
    oom = Mock(return_value=True)
    monkeypatch.setattr(worker_main, "set_oom_score_adj", oom)
    assert worker_main.harden_process(123, strict=True, oom_score=300)
    prctl.assert_any_call(worker_main.PR_SET_PDEATHSIG, signal.SIGTERM)
    oom.assert_called_once_with(score=300)


@pytest.mark.parametrize("role", COMMANDS)
def test_bootstrap_registers_and_dispatches_roles(
    monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    assert role in bootstrap.COMMANDS
    monkeypatch.setattr(bootstrap, "_refuse_root_in_bundle", lambda: None)
    entry = Mock(return_value=17)
    monkeypatch.setattr(worker_main, "meeting_main", entry)
    assert bootstrap.main([role, "12"]) == 17
    entry.assert_called_once_with(role, ["12"])


@pytest.mark.parametrize(
    "role,args",
    [
        ("unknown", ["3"]),
        ("meeting-capture", []),
        ("meeting-capture", ["PRIVATE"]),
        ("meeting-capture", ["-1"]),
        ("meeting-capture", ["3", "extra"]),
    ],
)
def test_bad_startup_refused_before_hardening(
    monkeypatch: pytest.MonkeyPatch, role: str, args: list[str]
) -> None:
    harden = Mock()
    monkeypatch.setattr(worker_main, "harden_process", harden)
    monkeypatch.delenv("ASTRA_VOICE_IPC_FD", raising=False)
    assert worker_main.meeting_main(role, args) == 2
    harden.assert_not_called()


def test_failed_hardening_never_constructs_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(worker_main, "harden_process", Mock(return_value=False))
    constructor = Mock()
    monkeypatch.setattr(socket, "socket", constructor)
    assert worker_main.meeting_main("meeting-capture", ["3"]) == 3
    constructor.assert_not_called()


def test_worker_unknown_meetings_message() -> None:
    from astra_voice.worker.state import WorkerState

    state = WorkerState()
    try:
        assert (
            state.handle(command("meeting-capture", "meeting.capture.stop"))[0]["code"]
            == "unknown-message"
        )
    finally:
        state.close()


def test_run_role_shutdown_uses_only_inherited_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    incoming = [b"placeholder"]
    connection = Mock(spec=socket.socket)
    from astra_voice.worker import ipc

    incoming[0] = ipc.encode(
        command("meeting-capture", "meeting.capture.shutdown"), role="meeting-capture"
    )
    connection.recv.side_effect = incoming
    connection.send.side_effect = lambda data: len(data)
    monkeypatch.setattr(os, "getppid", lambda: 123)

    monkeypatch.setattr(select, "select", lambda r, w, x, t: (r, w, []))
    assert run_role(connection, "meeting-capture", parent_pid=123) == 0
    written = b"".join(call.args[0] for call in connection.send.call_args_list)
    assert [m["type"] for m in ipc.FrameReader(role="meeting-capture").feed(written)] == [
        "hello",
        "meeting.capture.bye",
    ]


def test_oversized_fd_environment_refused_before_hardening(monkeypatch: pytest.MonkeyPatch) -> None:
    harden = Mock()
    monkeypatch.setattr(worker_main, "harden_process", harden)
    monkeypatch.setenv("ASTRA_VOICE_IPC_FD", "9" * 5000)
    assert worker_main.meeting_main("meeting-capture", []) == 2
    harden.assert_not_called()


@pytest.mark.parametrize(
    "message",
    [
        {
            "type": "hello",
            "family": "astra-voice.meetings",
            "protocol": 1,
            "build": "1",
            "runtime": {"python": "3.12", "onnxruntime": None},
        },
        {"type": "pong", "req": "req1", "request_type": "ping"},
    ],
)
def test_wrong_direction_is_safe_protocol_error(message: dict[str, object]) -> None:
    from astra_voice.worker import ipc

    result = MeetingRole("meeting-capture").handle(message)
    assert result[0]["code"] == "unknown-message"
    ipc.encode(result[0], role="meeting-capture")


@pytest.mark.parametrize("role", COMMANDS)
def test_successful_role_start_has_strict_hardening_and_limits(
    monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    from astra_voice.worker import meeting_roles

    monkeypatch.setattr(os, "getppid", lambda: 123)
    harden = Mock(return_value=True)
    limits = Mock(return_value=True)
    monkeypatch.setattr(worker_main, "harden_process", harden)
    monkeypatch.setattr(worker_main, "harden_meeting_limits", limits)
    connection = Mock(spec=socket.socket)
    connection.family = socket.AF_UNIX
    connection.type = socket.SOCK_STREAM
    monkeypatch.setattr(socket, "socket", Mock(return_value=connection))
    entry = Mock(return_value=0)
    monkeypatch.setattr(meeting_roles, "run_role", entry)
    monkeypatch.delenv("HOME", raising=False)
    monkeypatch.setenv("TMPDIR", "/nonexistent/astra-voice-diar")
    assert worker_main.meeting_main(role, ["12"]) == 0
    harden.assert_called_once_with(
        123, strict=True, oom_score=300 if role == "meeting-capture" else 500
    )
    limits.assert_called_once_with(role)
    entry.assert_called_once_with(connection, role, parent_pid=123)


@pytest.mark.parametrize(
    "role,expected",
    [
        ("meeting-capture", 512 * 1024**2),
        ("meeting-transcribe", 2 * 1024**3),
        ("meeting-diar", 1536 * 1024**2),
    ],
)
def test_meeting_address_space_limits_are_applied_and_verified(
    monkeypatch: pytest.MonkeyPatch, role: str, expected: int
) -> None:
    limits = {resource.RLIMIT_AS: (resource.RLIM_INFINITY, resource.RLIM_INFINITY)}
    monkeypatch.setattr(resource, "getrlimit", lambda key: limits[key])
    monkeypatch.setattr(resource, "setrlimit", lambda key, value: limits.update({key: value}))
    monkeypatch.setattr(os, "getpriority", lambda which, who: 19)
    assert worker_main.harden_meeting_limits(role)
    assert limits[resource.RLIMIT_AS][0] == expected


def test_diar_environment_is_fail_closed_before_work(monkeypatch: pytest.MonkeyPatch) -> None:
    harden = Mock()
    monkeypatch.setattr(worker_main, "harden_process", harden)
    monkeypatch.setenv("HOME", "/private")
    monkeypatch.setenv("TMPDIR", "/tmp")
    assert worker_main.meeting_main("meeting-diar", ["12"]) == 2
    harden.assert_not_called()


def test_strict_hardening_checks_effective_core_and_dumpable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(os, "getppid", lambda: 123)
    monkeypatch.setattr(worker_main, "_prctl", Mock())
    monkeypatch.setattr(worker_main, "set_oom_score_adj", Mock(return_value=True))
    monkeypatch.setattr(resource, "setrlimit", Mock())
    monkeypatch.setattr(worker_main, "_get_dumpable", lambda: 1)
    monkeypatch.setattr(resource, "getrlimit", lambda limit: (0, 0))
    assert not worker_main.harden_process(123, strict=True)
    monkeypatch.setattr(worker_main, "_get_dumpable", lambda: 0)
    monkeypatch.setattr(resource, "getrlimit", lambda limit: (1, 1))
    assert not worker_main.harden_process(123, strict=True)


def test_shutdown_works_when_request_cache_is_full(monkeypatch: pytest.MonkeyPatch) -> None:
    from astra_voice.worker import meeting_contract

    monkeypatch.setattr(meeting_contract, "MAX_REQUESTS", 1)
    role = MeetingRole("meeting-capture")
    role.handle(command(role.role, "ping", "one"))
    assert role.handle(command(role.role, "ping", "two"))[0]["code"] == "limit"
    assert (
        role.handle(command(role.role, "meeting.capture.shutdown", "bye"))[0]["type"]
        == "meeting.capture.bye"
    )
    assert role.stopped


def test_meeting_entry_imports_no_audio_asr_or_qt() -> None:
    import subprocess
    import sys
    from pathlib import Path

    source = Path(__file__).resolve().parents[2] / "src"
    script = f"""
import sys
sys.path.insert(0,{str(source)!r})
from astra_voice.worker import main, ipc, meeting_roles
for name in (
    "astra_voice.worker.audio", "astra_voice.worker.state",
    "astra_voice.worker.pulse_stream", "onnxruntime", "numpy", "PyQt5", "PySide6",
):
    assert name not in sys.modules,name
assert ipc.make_hello(role="meeting-capture")["protocol"]==1
"""
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", script], capture_output=True, timeout=5, check=False
    )
    assert result.returncode == 0, result.stderr.decode()


@pytest.mark.parametrize("changed", [False, True])
def test_revision_ledger_remembers_refusal_after_space_frees(
    monkeypatch: pytest.MonkeyPatch, changed: bool
) -> None:
    from astra_voice.worker import meeting_contract as mc

    monkeypatch.setattr(mc, "MAX_CACHE_BYTES", mc._REPLY_RESERVATION + 1024)
    ledger = mc.RequestLedger()
    occupied = command("meeting-capture", "ping", "occupied")
    refused = command("meeting-capture", "meeting.call.query", "refused")
    assert ledger.begin(occupied)[0] == "new"
    assert ledger.begin(refused)[0] == "limit"
    ledger.finish("occupied", [{"type": "pong", "req": "occupied", "request_type": "ping"}])
    assert ledger.used_bytes + ledger.reserved_bytes + mc._REPLY_RESERVATION <= mc.MAX_CACHE_BYTES
    retry = command("meeting-capture", "ping", "refused") if changed else refused
    status, cached = ledger.begin(retry)
    assert status == ("conflict" if changed else "same")
    if not changed:
        assert cached and cached[0]["code"] == "limit"


def test_revision_refusal_reserve_exhaustion_terminates_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from astra_voice.worker import meeting_contract as mc

    monkeypatch.setattr(mc, "MAX_CACHE_BYTES", 0)
    monkeypatch.setattr(mc, "MAX_REJECTIONS", 1, raising=False)
    role = MeetingRole("meeting-capture")
    assert role.handle(command(role.role, "ping", "one"))[0]["code"] == "limit"
    assert not role.stopped
    assert role.handle(command(role.role, "ping", "two"))[0]["code"] == "limit"
    assert role.stopped


def test_revision_refusal_reserve_exhaustion_terminates_supervisor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from test_meeting_supervisor import detached

    from astra_voice.worker import meeting_contract as mc

    monkeypatch.setattr(mc, "MAX_CACHE_BYTES", 0)
    monkeypatch.setattr(mc, "MAX_REJECTIONS", 1, raising=False)
    s, events, _ = detached("meeting-transcribe")
    s.send(command(s.role, "ping", "one"))
    s.send(command(s.role, "ping", "two"))
    assert s.state == "stopped" and not s.ready and not s._restarts
    assert events[-1]["code"] == "limit"
