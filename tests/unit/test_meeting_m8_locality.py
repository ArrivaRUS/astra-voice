"""E13.M8 pre-connect locality, with filesystem sockets and fake libpulse."""

from __future__ import annotations

import ctypes
import os
import socket
import stat
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from itertools import cycle
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any

import pytest
from test_meeting_capture import Rig, wait_until
from test_meeting_m8_detector_source import PulseFake

from astra_voice.worker import ipc, pulse_stream
from astra_voice.worker.meeting_audio_local import (
    LocalAudioServer,
    own_socket_fds,
    resolve_local_server,
    socket_family,
)
from astra_voice.worker.meeting_detector import RETRY_SECONDS
from astra_voice.worker.meeting_observer import observer_metadata
from astra_voice.worker.monitor_stream import _ServerInfo

pytestmark = pytest.mark.unit
REMOTE = {"type": "meeting.detector.code", "purpose": "observe", "code": "remote", "duration_ms": 0}
PULSE_SERVER_MARKER = "private-pulse-host.example"
SOCKET_PATH_MARKER = "private-pulse-path-marker"


@pytest.fixture
def socket_root() -> Iterator[Path]:
    with TemporaryDirectory(dir="/tmp", prefix="m8-") as directory:
        yield Path(directory)


def make_socket(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.mknod(path, stat.S_IFSOCK | 0o600)
    return path


def address(path: Path) -> bytes:
    return b"unix:" + os.fsencode(path)


@pytest.mark.parametrize(
    ("case", "source"),
    [
        ("tcp", None),
        ("list", None),
        ("relative", None),
        ("file", None),
        ("link", None),
        ("control", None),
        ("unicode-control", None),
        ("absolute", "pulse_server"),
        ("unicode", "pulse_server"),
        ("empty-server", "pulse_runtime_path"),
        ("runtime", "pulse_runtime_path"),
        ("missing-runtime", None),
        ("relative-runtime", "xdg_runtime_dir"),
        ("xdg", "xdg_runtime_dir"),
        ("relative-xdg", "run_user"),
        ("empty", None),
    ],
)
def test_address_priority_and_validation(socket_root: Path, case: str, source: str | None) -> None:
    uid = 1234
    root = socket_root / "run"
    runtime = make_socket(socket_root / "r" / "native")
    xdg = make_socket(socket_root / "xdg-label" / "pulse" / "native")
    fallback = root / str(uid) / "pulse" / "native"
    if case != "empty":
        make_socket(fallback)
    absolute = make_socket(socket_root / "s")
    unicode = make_socket(socket_root / "абс-сокет")
    regular = socket_root / "file"
    regular.touch()
    link = socket_root / "link"
    link.symlink_to(absolute)
    control = make_socket(socket_root / "s\n")
    unicode_control = make_socket(socket_root / "s\u0085")
    cases: dict[str, tuple[dict[str, str], Path | None]] = {
        "tcp": ({"PULSE_SERVER": "tcp:h:4713"}, None),
        "list": ({"PULSE_SERVER": f"unix:{absolute} tcp:b"}, None),
        "relative": ({"PULSE_SERVER": "unix:relative"}, None),
        "file": ({"PULSE_SERVER": str(regular)}, None),
        "link": ({"PULSE_SERVER": f"unix:{link}"}, None),
        "control": ({"PULSE_SERVER": str(control)}, None),
        "unicode-control": ({"PULSE_SERVER": str(unicode_control)}, None),
        "absolute": ({"PULSE_SERVER": str(absolute)}, absolute),
        "unicode": ({"PULSE_SERVER": f"unix:{unicode}"}, unicode),
        "empty-server": ({"PULSE_SERVER": "", "PULSE_RUNTIME_PATH": str(runtime.parent)}, runtime),
        "runtime": ({"PULSE_RUNTIME_PATH": str(runtime.parent)}, runtime),
        "missing-runtime": (
            {
                "PULSE_RUNTIME_PATH": str(socket_root / "missing"),
                "XDG_RUNTIME_DIR": str(xdg.parents[1]),
            },
            None,
        ),
        "relative-runtime": (
            {"PULSE_RUNTIME_PATH": "relative", "XDG_RUNTIME_DIR": str(xdg.parents[1])},
            xdg,
        ),
        "xdg": ({"XDG_RUNTIME_DIR": str(xdg.parents[1])}, xdg),
        "relative-xdg": ({"XDG_RUNTIME_DIR": "relative"}, fallback),
        "empty": ({}, None),
    }
    env, expected = cases[case]
    result = resolve_local_server(env, uid, root)
    if expected is None:
        assert result is None
    else:
        assert result is not None
        assert result.source == source and result.address == address(expected)
        assert str(socket_root) not in repr(result)


@pytest.mark.parametrize("length", [107, 108])
def test_socket_address_length_boundary(socket_root: Path, length: int) -> None:
    prefix_bytes = len(os.fsencode(socket_root)) + 1
    assert prefix_bytes < 107
    native = make_socket(socket_root / ("a" * (length - prefix_bytes)))
    assert len(os.fsencode(native)) == length
    resolved = resolve_local_server({"PULSE_SERVER": str(native)}, 1234, socket_root)
    expected = LocalAudioServer(address(native), "pulse_server") if length == 107 else None
    assert resolved == expected


@pytest.mark.parametrize(
    "display", [None, ":0", ":0.1", "unix:0", "unix:0.1", "localhost:10.0", "host:0", ":0x", ""]
)
@pytest.mark.parametrize("ssh", [None, "SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY"])
def test_display_and_ssh(socket_root: Path, display: str | None, ssh: str | None) -> None:
    native = make_socket(socket_root / "native")
    env = {"PULSE_SERVER": str(native)}
    if display is not None:
        env["DISPLAY"] = display
    if ssh is not None:
        env[ssh] = ""  # Presence alone suppresses X11 access, even for an empty variable.
    result = resolve_local_server(env, 1234, socket_root)
    if display is None or (display in {":0", ":0.1", "unix:0", "unix:0.1"} and ssh is None):
        assert result == LocalAudioServer(address(native), "pulse_server")
    else:
        assert result is None


class ObserverPulse(PulseFake):
    def __init__(self, *, index: int, cookie: int, default_server: bytes | None) -> None:
        super().__init__()
        self.own_index, self.cookie = index, cookie
        self.default_server = default_server
        self.connections: list[tuple[Any, ...]] = []
        self.owner = threading.current_thread()
        self.before_dispatch: Callable[[], None] = lambda: None
        self.connect_error = False

    def call(self, name: str, *args: Any) -> Any:
        if name == "pa_context_connect":
            server = args[1] if args[1] is not None else self.default_server
            self.connections.append((server, *args[2:]))
            if self.connect_error:
                self.calls.append(name)
                return -1
        elif name == "pa_context_get_server_info":
            self.calls.append(name)
            row = _ServerInfo(cookie=self.cookie)
            args[1](1, ctypes.pointer(row), None)
            return self.new_op()
        elif name == "pa_mainloop_dispatch":
            self.before_dispatch()
        return super().call(name, *args)


class ObserverPulses:
    def __init__(
        self,
        patch: pytest.MonkeyPatch,
        *,
        cookies: tuple[int, ...] = (1,),
        setup: Callable[[ObserverPulse], None] = lambda pulse: None,
        client_conf: Path | None = None,
    ) -> None:
        self.pulses: list[ObserverPulse] = []
        default = (
            client_conf.read_bytes().partition(b"=")[2].strip() if client_conf is not None else None
        )
        pulses = self.pulses
        server_cookies = cycle(cookies)

        class Native(ObserverPulse):
            def __init__(self) -> None:
                super().__init__(
                    index=42 + len(pulses), cookie=next(server_cookies), default_server=default
                )
                setup(self)
                pulses.append(self)

        patch.setattr(pulse_stream, "_PulseAsync", Native)

    def count(self, function: str) -> int:
        return sum(pulse.calls.count(function) for pulse in self.pulses)

    def assert_no_pcm(self) -> None:
        for pulse in self.pulses:
            assert not any(name.startswith(("pa_stream_", "pa_simple_")) for name in pulse.calls)


def detector(rig: Rig, action: str, req: str) -> list[dict[str, Any]]:
    kind = "meeting.detector." + action
    message: dict[str, Any] = {"type": kind, "request_type": kind, "req": req}
    if action == "start":
        message["apps_version"] = 1
    reply = rig.state.handle(message)
    assert [item["type"] for item in reply] == ["meeting.ack"]
    return reply


def query(rig: Rig, req: str) -> None:
    kind = "meeting.call.query"
    assert rig.state.handle({"type": kind, "request_type": kind, "req": req}) == [
        {"type": "meeting.call.state", "request_type": kind, "req": req, "active": False}
    ]


def assert_refusal(rig: Rig, req: str) -> None:
    detector(rig, "start", req)
    assert rig.poll() == [
        {
            "type": "meeting.detector.started",
            "request_type": "meeting.detector.start",
            "req": req,
            "apps_version": 1,
        },
        REMOTE,
    ]


@pytest.mark.usefixtures("filesystem_euid")
@pytest.mark.parametrize("case", ["tcp", "missing", "file"])
def test_refusal_precedes_all_background_connections(
    socket_root: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    fallback = make_socket(socket_root / "local" / "native")
    non_socket = socket_root / SOCKET_PATH_MARKER
    non_socket.touch()
    conf = socket_root / "client.conf"
    conf.write_text(f"default-server = tcp:{PULSE_SERVER_MARKER}:4713\n")
    env: dict[str, str] = {}
    if case != "missing":
        env["PULSE_RUNTIME_PATH"] = str(fallback.parent)
        env["PULSE_SERVER"] = (
            f"tcp:{PULSE_SERVER_MARKER}:4713" if case == "tcp" else str(non_socket)
        )
    with monkeypatch.context() as patch:
        pulses = ObserverPulses(patch, client_conf=conf)
        for run in range(2):
            root = socket_root / f"r{run}"
            root.mkdir()
            rig = Rig(
                root,
                observer_address=lambda: resolve_local_server(env, 1234, socket_root),
                observer_factory=observer_metadata,
            )
            try:
                assert_refusal(rig, "observer-1")
                kind = "meeting.detector.start"
                duplicate = {
                    "type": kind,
                    "request_type": kind,
                    "req": "observer-1",
                    "apps_version": 1,
                }
                assert [m["type"] for m in rig.state.handle(duplicate)] == [
                    "meeting.ack",
                    "meeting.detector.started",
                ]
                wrong = {**duplicate, "apps_version": 2}
                assert rig.state.handle(wrong)[0]["code"] == "req-conflict"
                query(rig, "query-before-click")
                assert_refusal(rig, "observer-2")
                detector(rig, "stop", "stop")
                rig.until("meeting.detector.stopped")
                assert_refusal(rig, "observer-3")
                assert rig.state.observer._thread is None and rig.state.backend is None
            finally:
                rig.cleanup()
        assert pulses.pulses == []
        assert pulses.count("pa_context_new") == pulses.count("pa_context_connect") == 0


@pytest.mark.usefixtures("filesystem_euid")
@pytest.mark.parametrize("kind", ["sink_monitor", "none"])
@pytest.mark.parametrize("remote", ["tcp", "file"])
def test_manual_prepare_after_remote_preserves_warning_without_leaking_address(
    socket_root: Path, kind: str, remote: str
) -> None:
    non_socket = socket_root / SOCKET_PATH_MARKER
    non_socket.touch()
    server = f"tcp:{PULSE_SERVER_MARKER}:4713" if remote == "tcp" else str(non_socket)
    rig = Rig(
        socket_root,
        observer_address=lambda: resolve_local_server({"PULSE_SERVER": server}, 1234, socket_root),
    )
    try:
        assert_refusal(rig, "observe")
        rig.backend.server_local = False
        rig.prepare(kind)
        assert rig.backend.prepare_calls == [{"kind": kind}]
        prepared = next(m for m in rig.events if m["type"] == "meeting.capture.prepared")
        assert prepared["server_local"] is False
        data = b"".join(ipc.encode(m, role="meeting-capture") for m in rig.events)
        for marker in (PULSE_SERVER_MARKER, SOCKET_PATH_MARKER, "PULSE_SERVER"):
            assert marker.encode() not in data
            assert marker not in repr(rig.state)
        assert not rig.mics and not rig.backend.sources
    finally:
        rig.cleanup()


@pytest.mark.usefixtures("filesystem_euid")
def test_explicit_unix_connect_ignores_client_conf_and_survives_connect_error(
    socket_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    native = make_socket(socket_root / "native")
    conf = socket_root / "client.conf"
    conf.write_text(f"default-server = tcp:{PULSE_SERVER_MARKER}:4713\n")
    env = {"PULSE_RUNTIME_PATH": str(socket_root)}
    retry_ready = threading.Event()

    def wait(wake: threading.Event, seconds: float) -> bool:
        retry_ready.set()
        return wake.wait(seconds)

    with monkeypatch.context() as patch:
        first = True

        def fail_first(pulse: ObserverPulse) -> None:
            nonlocal first
            pulse.connect_error = first
            first = False

        pulses = ObserverPulses(patch, setup=fail_first, client_conf=conf)
        rig = Rig(
            socket_root,
            observer_address=lambda: resolve_local_server(env, 1234, socket_root),
            observer_factory=observer_metadata,
            observer_fds=lambda: {},
            observer_wait=wait,
        )
        try:
            env["PULSE_SERVER"] = f"tcp:{PULSE_SERVER_MARKER}:4713"
            detector(rig, "start", "start")
            rig.until("meeting.detector.started")
            assert retry_ready.wait(2)
            assert pulses.count("pa_context_disconnect") == 1
            rig.clock.now += 2 * RETRY_SECONDS
            wait_until(lambda: pulses.count("pa_context_get_server_info") == 1)
            assert [p.connections for p in pulses.pulses] == [
                [(address(native), pulse_stream.PA_CONTEXT_NOAUTOSPAWN, None)],
                [(address(native), pulse_stream.PA_CONTEXT_NOAUTOSPAWN, None)],
            ]
            assert pulses.count("pa_context_new") == pulses.count("pa_context_connect") == 2
            assert rig.state.backend is None and not rig.state.stopped
            assert rig.poll() == []
        finally:
            rig.cleanup()
        pulses.assert_no_pcm()


@pytest.mark.usefixtures("filesystem_euid")
def test_nonunix_transport_disables_observation_until_role_exit(
    socket_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    native = make_socket(socket_root / "native")
    fds = cycle(({}, {17: 999}))
    with monkeypatch.context() as patch:
        pulses = ObserverPulses(patch)
        rig = Rig(
            socket_root,
            observer_address=lambda: LocalAudioServer(address(native), "pulse_server"),
            observer_factory=observer_metadata,
            observer_fds=lambda: next(fds),
            observer_family=lambda fd, inode: socket.AF_INET,
        )
        try:
            detector(rig, "start", "start")
            assert rig.until("meeting.detector.started")["req"] == "start"
            assert rig.until("meeting.detector.code") == REMOTE
            assert pulses.count("pa_context_disconnect") == 1
            assert rig.state.observer.automation_off.is_set()
            observer = pulses.pulses[0].owner
            observer.join(2)
            assert not observer.is_alive()
            for _ in range(3):
                rig.clock.now += RETRY_SECONDS
                assert rig.poll() == []
            detector(rig, "stop", "stop")
            rig.until("meeting.detector.stopped")
            assert_refusal(rig, "restart")
        finally:
            rig.cleanup()
        assert pulses.count("pa_context_new") == pulses.count("pa_context_connect") == 1
        assert pulses.count("pa_context_disconnect") == 1
        pulses.assert_no_pcm()


@pytest.mark.parametrize("new", [None, {}, {17: 99, 18: 100}], ids=["incomplete", "zero", "two"])
@pytest.mark.usefixtures("filesystem_euid")
def test_ambiguous_fds_keep_observing_and_check_again_after_stop(
    socket_root: Path, monkeypatch: pytest.MonkeyPatch, new: dict[int, int] | None
) -> None:
    native = make_socket(socket_root / "native")
    fds = iter(({}, new, {17: 99}, {17: 100}))
    inspected: list[tuple[int, int]] = []

    def family(fd: int, inode: int) -> int:
        inspected.append((fd, inode))
        return socket.AF_UNIX

    with monkeypatch.context() as patch:
        pulses = ObserverPulses(patch)
        rig = Rig(
            socket_root,
            observer_address=lambda: LocalAudioServer(address(native), "pulse_server"),
            observer_factory=observer_metadata,
            observer_fds=lambda: next(fds),
            observer_family=family,
        )
        try:
            detector(rig, "start", "start")
            wait_until(lambda: rig.state.observer.registry.for_observer(1) == {42})
            assert rig.poll()[0]["type"] == "meeting.detector.started"
            assert inspected == [] and not rig.state.observer.automation_off.is_set()
            detector(rig, "stop", "stop")
            wait_until(lambda: not rig.state.observer.registry.for_observer(1))
            rig.until("meeting.detector.stopped")
            rig.clock.now += 2 * RETRY_SECONDS
            detector(rig, "start", "restart")
            wait_until(lambda: rig.state.observer.registry.for_observer(2) == {43})
            rig.until("meeting.detector.started")
            assert inspected == [(17, 100)]
            assert not any(m["type"] == "meeting.detector.code" for m in rig.events)
        finally:
            rig.cleanup()
        assert pulses.count("pa_context_disconnect") == 2
        pulses.assert_no_pcm()


def test_socket_scan_and_family_check_verify_inode() -> None:
    before = own_socket_fds()
    with socket.socket(socket.AF_UNIX) as local:
        after = own_socket_fds()
        assert before is not None and after is not None
        fd = local.fileno()
        assert before.get(fd) != after[fd]
        assert socket_family(fd, after[fd] + 1) is None
        try:
            local.getsockname()
        except PermissionError:
            pytest.skip("sandbox denies getsockname on Unix sockets")
        assert socket_family(fd, after[fd]) == socket.AF_UNIX


@pytest.mark.parametrize("native", [True, False], ids=["inet", "substitute-probe"])
def test_socket_family_preserves_nonunix_transport(
    native: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    try:
        probe = socket.socket(socket.AF_INET if native else socket.AF_UNIX)
    except PermissionError:
        if native:
            pytest.skip("sandbox denies AF_INET socket creation")
        raise
    with probe:
        if native:
            try:
                probe.getsockname()
            except PermissionError:
                pytest.skip("sandbox denies getsockname on AF_INET sockets")
        fds = own_socket_fds()
        assert fds is not None
        fd = probe.fileno()

        @contextmanager
        def replacement(*, fileno: int) -> Iterator[SimpleNamespace]:
            # Keep the real fd scan and inode check when family discovery is restricted.
            try:
                yield SimpleNamespace(family=socket.AF_INET)
            finally:
                os.close(fileno)

        with monkeypatch.context() as patch:
            if not native:
                patch.setattr(socket, "socket", replacement)
            assert socket_family(fd, fds[fd]) == socket.AF_INET
