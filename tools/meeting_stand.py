"""Opt-in real capture on disposable, hardware-free audio servers.

Every child starts a private session with an explicit environment. The capture
uses the production supervisor/launcher/protocol; Fly admission is an existing
product mode, not an injected capture backend. No host audio client is invoked.
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import re
import shutil
import signal
import struct
import subprocess
import tempfile
import threading
import time
import uuid
import wave
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from astra_voice.meetings.model import Meeting
from astra_voice.meetings.store import MeetingStore, new_meeting_id
from astra_voice.worker.supervisor import WorkerSupervisor, worker_command


class StandUnavailable(RuntimeError):
    """Fixed diagnostic code, never external command output."""


class StandFailure(RuntimeError):
    """Authoritative failure outcome, distinct from unavailable observations."""


def private_environment(root: Path) -> dict[str, str]:
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    root.chmod(0o700)
    locations = {
        "HOME": "home",
        "XDG_RUNTIME_DIR": "runtime",
        "XDG_CONFIG_HOME": "config",
        "XDG_CACHE_HOME": "cache",
        "XDG_STATE_HOME": "state",
        "XDG_DATA_HOME": "data",
        "TMPDIR": "tmp",
    }
    env = {key: str(root / name) for key, name in locations.items()}
    for value in env.values():
        Path(value).mkdir(mode=0o700, exist_ok=True)
    runtime = root / "runtime"
    (runtime / "pulse").mkdir(mode=0o700, exist_ok=True)
    pulse_config = root / "config/pulse"
    pulse_config.mkdir(mode=0o700, exist_ok=True)
    client = pulse_config / "client.conf"
    client.write_text("autospawn = no\nauto-connect-localhost = no\nauto-connect-display = no\n")
    env.update(
        {
            "PATH": "/usr/bin:/bin",
            "LANG": "C",
            "LC_ALL": "C",
            "PIPEWIRE_RUNTIME_DIR": str(runtime),
            "PIPEWIRE_REMOTE": "pipewire-0",
            "PULSE_SERVER": f"unix:{runtime}/pulse/native",
            "PULSE_RUNTIME_PATH": str(runtime / "pulse"),
            "PULSE_CLIENTCONFIG": str(client),
            "DBUS_SYSTEM_BUS_ADDRESS": f"unix:path={runtime}/absent-system-bus",
            "DBUS_SESSION_BUS_ADDRESS": f"unix:path={runtime}/absent-session-bus",
            "XDG_CURRENT_DESKTOP": "FLY",
            "QT_QPA_PLATFORM": "offscreen",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    return env


def capture_profile_paths(profile: Path) -> dict[str, Path]:
    return {
        name: profile if name == "normal" else Path(f"{profile}.{name}")
        for name in ("normal", "target-gone", "fallback", "crash")
    }


def capture_command(
    fd: int,
    env: dict[str, str],
    *,
    profile: Path | None = None,
    profile_gate: Path | None = None,
) -> list[str]:
    # exec env retains the supervisor's PID, new process group, and passed IPC FD.
    command = worker_command(fd, role="meeting-capture")
    if profile is not None:
        if profile_gate is None:
            raise ValueError("capture-profile-gate-required")
        command = [
            *command[:2],
            str(Path(__file__).with_name("meeting_stand_profile.py").resolve()),
            str(profile),
            str(profile_gate),
            *command[2:],
        ]
    return [
        "/usr/bin/env",
        "-i",
        *(f"{key}={value}" for key, value in sorted(env.items())),
        *command,
    ]


def _process_identity(pid: int) -> tuple[int, int, int] | None:
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return int(fields[2]), int(fields[3]), int(fields[19])
    except (OSError, ValueError, IndexError):
        return None


class ProcessOwner:
    def __init__(self, env: dict[str, str]) -> None:
        self.env = env
        self.interrupted = False
        self.processes: list[subprocess.Popen[bytes]] = []
        self.identities: dict[int, dict[int, tuple[int, int, int]]] = {}

    def check_interrupt(self) -> None:
        if self.interrupted:
            raise KeyboardInterrupt

    def register(self, process: subprocess.Popen[bytes]) -> None:
        self.processes.append(process)
        identity = _process_identity(process.pid)
        self.identities[process.pid] = {process.pid: identity} if identity is not None else {}

    def start(self, argv: list[str], *, output: bool = False) -> subprocess.Popen[bytes]:
        process = subprocess.Popen(
            argv,
            env=self.env,
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE if output else subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.register(process)
        self.check_interrupt()
        return process

    def command(self, argv: list[str], *, timeout: float = 3) -> bytes:
        process = self.start(argv, output=True)
        try:
            stdout, _ = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.terminate(process)
            raise StandUnavailable("command-timeout") from None
        # These fixed single-shot programs (pactl/version) cannot spawn helpers.
        self.processes.remove(process)
        self.identities.pop(process.pid, None)
        if process.returncode:
            raise StandUnavailable("command-failed")
        self.check_interrupt()
        return stdout

    def _owned_group(self, process: subprocess.Popen[bytes]) -> bool:
        pgid = process.pid
        members: dict[int, tuple[int, int, int]] = {}
        for entry in Path("/proc").iterdir():
            if not entry.name.isdecimal():
                continue
            identity = _process_identity(int(entry.name))
            if identity is not None and identity[:2] == (pgid, pgid):
                members[int(entry.name)] = identity
        if not members:
            return False
        previous = self.identities.get(pgid, {})
        # A PID is not authority: at least one member must retain its birth time.
        if not any(previous.get(pid) == birth for pid, birth in members.items()):
            raise StandUnavailable("cleanup-ownership-unconfirmed")
        self.identities[pgid] = members
        return True

    def terminate(self, process: subprocess.Popen[bytes]) -> None:
        for sig in (signal.SIGTERM, signal.SIGKILL):
            if self._owned_group(process):
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, sig)
            if sig == signal.SIGTERM:
                try:
                    process.wait(timeout=0.4)
                except subprocess.TimeoutExpired:
                    pass
        process.wait(timeout=2)
        # Leaders have been reaped. Any remaining live member is a cleanup failure.
        for pid, birth in self.identities.get(process.pid, {}).items():
            if _process_identity(pid) == birth:
                try:
                    state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
                except OSError:
                    continue
                if state != "Z":
                    raise StandUnavailable("cleanup-unconfirmed")
        self.identities.pop(process.pid, None)
        if process in self.processes:
            self.processes.remove(process)

    def close(self) -> None:
        failed = False
        for process in reversed(list(self.processes)):
            try:
                self.terminate(process)
            except (OSError, subprocess.TimeoutExpired, StandUnavailable):
                failed = True
        if failed:
            raise StandUnavailable("cleanup-unconfirmed")


def preflight(server: str) -> None:
    binaries = ["pactl", "paplay", "env"] + (
        ["pipewire", "wireplumber", "pipewire-pulse"] if server == "pipewire" else ["pulseaudio"]
    )
    if any(shutil.which(binary, path="/usr/bin:/bin") is None for binary in binaries):
        raise StandUnavailable("missing-binary")


@contextlib.contextmanager
def interruption(owner: ProcessOwner) -> Iterator[None]:
    previous: dict[int, Any] = {}

    def interrupt(signum: int, frame: Any) -> None:
        owner.interrupted = True

    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGINT, signal.SIGTERM):
            previous[sig] = signal.signal(sig, interrupt)
    try:
        yield
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


def _criterion(name: str, passed: bool) -> dict[str, Any]:
    return {
        "name": name,
        "status": "pass" if passed else "fail",
        "value": passed,
        "limit": True,
        "unit": "bool",
        "detail": "",
    }


def _snapshot_identity(kind: str, row: dict[str, Any]) -> dict[str, Any]:
    def present(value: Any) -> bool:
        return (isinstance(value, int) and not isinstance(value, bool) and value >= 0) or (
            isinstance(value, str) and bool(value.strip())
        )

    for key in ("index", "id"):
        if present(row.get(key)):
            return {key: row[key]}
    if kind in {"sink-inputs", "source-outputs"}:
        props = row.get("properties", {})
        if not isinstance(props, dict):
            raise StandUnavailable("invalid-server-response")
        serial, pid = props.get("object.serial"), props.get("application.process.id")
        if present(serial):
            return {"serial": serial}
        if present(pid) and (present(row.get("name")) or present(row.get("driver"))):
            return {
                "pid": pid,
                "name": row.get("name"),
                "driver": row.get("driver"),
            }
    elif present(row.get("name")):
        # pactl 16.1/17.0 omits module indexes, on either audio backend.
        # The enclosing sorted list retains multiplicity of equal signatures.
        return {
            "name": row["name"],
            "driver": row.get("driver"),
            **(
                {"argument": row.get("argument"), "arguments": row.get("arguments")}
                if kind == "modules"
                else {}
            ),
        }
    raise StandUnavailable("snapshot-identity-unavailable")


def _stand_error_code(exc: Exception) -> str:
    traceback = exc.__traceback__
    while traceback is not None and traceback.tb_next is not None:
        traceback = traceback.tb_next
    basename = Path(traceback.tb_frame.f_code.co_filename).name if traceback else "unknown"
    if re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", basename) is None:
        basename = "unknown"
    exception_type = type(exc).__name__
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,79}", exception_type) is None:
        exception_type = "Exception"
    line = traceback.tb_lineno if traceback else 0
    return f"stand-error:{exception_type}@{basename}:{line}"


class AudioServer:
    def __init__(self, owner: ProcessOwner, root: Path, kind: str) -> None:
        self.owner, self.root, self.kind = owner, root, kind
        self.daemons: list[subprocess.Popen[bytes]] = []
        self.versions: dict[str, str] = {}

    def pactl(self, *args: str) -> bytes:
        return self.owner.command(
            ["/usr/bin/pactl", f"--server={self.owner.env['PULSE_SERVER']}", *args]
        )

    def rows(self, kind: str) -> list[dict[str, Any]]:
        value: Any = json.loads(self.pactl("--format=json", "list", kind))
        if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
            raise StandUnavailable("invalid-server-response")
        return value

    def start(self) -> None:
        config = self.root / "config"
        for binary in ["pipewire", "wireplumber"] if self.kind == "pipewire" else ["pulseaudio"]:
            output = self.owner.command([f"/usr/bin/{binary}", "--version"])
            found = re.search(rb"[0-9]+\.[0-9]+(?:\.[0-9]+)?", output)
            if found is None:
                raise StandUnavailable("server-version-unavailable")
            self.versions[binary] = found.group().decode("ascii")
        if self.kind == "pipewire":
            version = self.owner.command(["/usr/bin/wireplumber", "--version"])
            # Only the feature-profile configuration dialect is safe here.
            if b"0.4." in version:
                raise StandUnavailable("wireplumber-profile-unavailable")
            pw = config / "pipewire"
            pw.mkdir(mode=0o700)
            self.owner.env["PIPEWIRE_CONFIG_DIR"] = str(pw)
            modules = [
                "protocol-native",
                "metadata",
                "spa-node-factory",
                "client-node",
                "client-device",
                "access",
                "adapter",
                "link-factory",
                "session-manager",
            ]
            (pw / "pipewire.conf").write_text(
                "context.properties = { core.daemon = true core.name = pipewire-0 "
                "support.dbus = false default.clock.rate = 48000 }\n"
                "context.spa-libs = { audio.convert.* = audioconvert/libspa-audioconvert "
                "support.* = support/libspa-support }\ncontext.modules = [\n"
                + "\n".join("{ name = libpipewire-module-" + name + " }" for name in modules)
                + "\n]\n"
            )
            (pw / "client.conf").write_text(
                "context.properties = { support.dbus = false }\n"
                "context.modules = [ { name = libpipewire-module-protocol-native } "
                "{ name = libpipewire-module-client-node } "
                "{ name = libpipewire-module-metadata } ]\n"
            )
            (pw / "pipewire-pulse.conf").write_text(
                "context.properties = { support.dbus = false }\n"
                "context.spa-libs = { audio.convert.* = audioconvert/libspa-audioconvert "
                "support.* = support/libspa-support }\ncontext.modules = [\n"
                + "\n".join(
                    "{ name = libpipewire-module-" + name + " }"
                    for name in [
                        "protocol-native",
                        "client-node",
                        "adapter",
                        "metadata",
                        "protocol-pulse",
                    ]
                )
                + '\n]\npulse.properties = { server.address = [ "unix:native" ] }\n'
            )
            wp = config / "wireplumber"
            wp.mkdir(mode=0o700)
            self.owner.env["WIREPLUMBER_CONFIG_DIR"] = str(wp)
            # Distribution policy definitions, never /etc or user configuration.
            stock = Path("/usr/share/wireplumber/wireplumber.conf").read_text()
            if "wireplumber.profiles =" not in stock:
                raise StandUnavailable("wireplumber-profile-unavailable")
            tail = stock[stock.index("wireplumber.profiles =") :]
            disabled = [
                "hardware.audio",
                "hardware.bluetooth",
                "hardware.video-capture",
                "monitor.alsa",
                "monitor.alsa-midi",
                "monitor.bluez",
                "monitor.bluez-midi",
                "monitor.v4l2",
                "monitor.libcamera",
                "support.dbus",
                "support.system-dbus",
                "support.portal-permissionstore",
                "support.reserve-device",
                "support.logind",
                "support.modem-manager",
                "support.mpris",
                "script.client.access-portal",
            ]
            (wp / "wireplumber.conf").write_text(
                "context.properties = { support.dbus = false }\n"
                "context.modules = [ { name = libpipewire-module-protocol-native } "
                "{ name = libpipewire-module-metadata } ]\n"
                + tail
                + "\nwireplumber.profiles = { stand = { inherits = [ policy ]\n"
                + "\n".join(name + " = disabled" for name in disabled)
                + "\n} }\n"
            )
            self.daemons.append(self.owner.start(["/usr/bin/pipewire"]))
            self.daemons.append(self.owner.start(["/usr/bin/wireplumber", "--profile=stand"]))
            self.daemons.append(self.owner.start(["/usr/bin/pipewire-pulse"]))
        else:
            pa = config / "stand.pa"
            pa.write_text(
                f"load-module module-native-protocol-unix socket={self.root}/runtime/pulse/native\n"
            )
            self.daemons.append(
                self.owner.start(
                    [
                        "/usr/bin/pulseaudio",
                        "-n",
                        f"--file={pa}",
                        "--daemonize=no",
                        "--use-pid-file=no",
                        "--exit-idle-time=-1",
                        "--disable-shm=yes",
                    ]
                )
            )
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            self.owner.check_interrupt()
            if any(process.poll() is not None for process in self.daemons):
                raise StandUnavailable("server-start-failed")
            if (self.root / "runtime/pulse/native").is_socket():
                try:
                    self.pactl("info")
                    break
                except StandUnavailable:
                    pass
            time.sleep(0.1)
        else:
            raise StandUnavailable("server-start-timeout")
        for name in ("stand_far", "stand_mic"):
            self.pactl(
                "load-module", "module-null-sink", f"sink_name={name}", "rate=16000", "channels=1"
            )
        self.pactl("set-default-sink", "stand_far")
        self.pactl("set-default-source", "stand_mic.monitor")
        # No actual hardware is allowed into this graph, even with bad policy.
        if {row["name"] for row in self.rows("sinks")} != {"stand_far", "stand_mic"}:
            raise StandUnavailable("unexpected-audio-object")
        if {row["name"] for row in self.rows("sources")} != {
            "stand_far.monitor",
            "stand_mic.monitor",
        }:
            raise StandUnavailable("unexpected-audio-object")

    def snapshot(self) -> dict[str, Any]:
        # Compare while the SAME players remain alive. Retain identity, routing,
        # gain and module arguments, but omit transient pactl control clients.
        snapshot: dict[str, Any] = {
            "defaults": self.pactl("get-default-sink").decode().strip()
            + "/"
            + self.pactl("get-default-source").decode().strip(),
        }
        for kind in ("sinks", "sources", "modules", "sink-inputs", "source-outputs"):
            objects = []
            weak_stream_identities: set[str] = set()
            for row in self.rows(kind):
                identity = _snapshot_identity(kind, row)
                if kind in {"sink-inputs", "source-outputs"} and "pid" in identity:
                    # PID plus driver/name cannot distinguish two streams from
                    # one client; a route swap must never become a false PASS.
                    signature = json.dumps(identity, sort_keys=True)
                    if signature in weak_stream_identities:
                        raise StandUnavailable("snapshot-identity-unavailable")
                    weak_stream_identities.add(signature)
                item = {
                    "identity": identity,
                    "name": row.get("name"),
                    "driver": row.get("driver"),
                }
                if kind == "modules":
                    item.update(argument=row.get("argument"), arguments=row.get("arguments"))
                elif kind in {"sink-inputs", "source-outputs"}:
                    props = row.get("properties", {})
                    if not isinstance(props, dict):
                        raise StandUnavailable("invalid-server-response")
                    route = "sink" if kind == "sink-inputs" else "source"
                    item.update(
                        {
                            route: row.get(route),
                            "pid": props.get("application.process.id"),
                            "serial": props.get("object.serial"),
                            "mute": row.get("mute"),
                            "volume": row.get("volume"),
                        }
                    )
                objects.append(json.dumps(item, sort_keys=True))
            snapshot[kind.replace("-", "_")] = sorted(objects)
        return snapshot

    def play(self, path: Path, sink: str, *, foreign: bool = False) -> subprocess.Popen[bytes]:
        return self.owner.start(
            [
                "/usr/bin/paplay",
                f"--server={self.owner.env['PULSE_SERVER']}",
                f"--device={sink}",
                "--client-name=stand-foreign" if foreign else "--client-name=DION",
                "--property=application.id=org.astravoice.stand.foreign"
                if foreign
                else "--property=application.id=ru.dion.desktop",
                "--property=media.role=music" if foreign else "--property=media.role=phone",
                "--property=object.serial=7001",
                str(path),
            ]
        )

    def target(self, process: subprocess.Popen[bytes]) -> dict[str, Any]:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            for row in self.rows("sink-inputs"):
                props = row.get("properties", {})
                if str(props.get("application.process.id")) == str(process.pid):
                    return {
                        "kind": "app_stream",
                        "sink_input": int(row["index"]),
                        "object_serial": int(props["object.serial"]),
                    }
            time.sleep(0.05)
        raise StandUnavailable("target-unavailable")


def write_signal(
    path: Path,
    seconds: float,
    *,
    foreign: bool = False,
    train_end_s: float | None = None,
    lead_s: int = 10,
) -> list[float]:
    end = seconds if train_end_s is None else min(seconds, train_end_s)
    onsets = [float(second) for second in range(lead_s, math.ceil(end), 5)]
    rate = 16000
    # Bounded memory: stream one second at a time, including the 60-minute case.
    tone = bytearray(rate * 2)
    for sample in range(rate if foreign else 2400):
        value = int(16000 * math.sin(2 * math.pi * (1700 if foreign else 1000) * sample / rate))
        struct.pack_into("<h", tone, sample * 2, value)
    silence = bytes(rate * 2)
    with wave.open(str(path), "wb") as output:
        output.setparams((1, 2, rate, 0, "NONE", "not compressed"))
        for second in range(math.ceil(seconds)):
            output.writeframesraw(tone if foreign or second in onsets else silence)
    return onsets


class Capture:
    def __init__(
        self, owner: ProcessOwner, directory: Path, *, profile: Path | None = None
    ) -> None:
        self.owner, self.directory = owner, directory
        self.profile = profile
        self.profile_gate = directory / ".profile-start"
        self.profile_activated = False
        self.events: list[dict[str, Any]] = []
        self.identity: dict[str, Any] = {"meeting_id": directory.name, "meeting_generation": 1}
        self.supervisor = WorkerSupervisor(
            self.events.append,
            role="meeting-capture",
            use_qt=False,
            command_factory=lambda fd: capture_command(
                fd, owner.env, profile=self.profile, profile_gate=self.profile_gate
            ),
        )
        self.sequence = 0
        self.keepalive_at = 0.0

    def start(self) -> None:
        self.supervisor.set_meeting_generation(self.directory.name, 1)
        self.supervisor.start()
        assert self.supervisor.process is not None
        self.owner.register(self.supervisor.process)
        self.owner.check_interrupt()
        deadline = time.monotonic() + 4
        while not self.supervisor.ready and time.monotonic() < deadline:
            self.supervisor.pump(0.05)
        if not self.supervisor.ready:
            raise StandUnavailable("capture-start-failed")

    def send(self, action: str, **fields: Any) -> str:
        req = uuid.uuid4().hex
        # Shutdown is process-scoped, including after a successful capture.
        # Its M1 schema intentionally carries no meeting/capture identity.
        identity = {} if action == "meeting.capture.shutdown" else self.identity
        self.supervisor.send(
            {"type": action, "request_type": action, "req": req, **identity, **fields}
        )
        return req

    def wait(
        self, kind: str, req: str | None = None, timeout: float = 6, *, since: int = 0
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.owner.check_interrupt()
            self.supervisor.pump(0.05)
            for event in self.events[since:]:
                if event["type"] == "error":
                    phase = {
                        "meeting.capture.prepared": "prepare",
                        "meeting.capture.started": "open",
                        "meeting.capture.finalized": "stop",
                        "meeting.capture.bye": "shutdown",
                        "meeting.capture.track": "track",
                    }.get(kind, "wait")
                    code = event.get("code")
                    safe_codes = {
                        "admission-stale",
                        "audio-failed",
                        "dir-unwritable",
                        "limit",
                        "io",
                        "target-mismatch",
                        "target-gone",
                        "not-prepared",
                        "timeout",
                        "hello-timeout",
                        "worker-crashed",
                        "protocol",
                        "worker-start",
                    }
                    if not isinstance(code, str) or code not in safe_codes:
                        code = "unknown"
                    raise StandUnavailable(f"capture-operation-failed:{phase}:{code}")
                if event["type"] == kind and (req is None or event.get("req") == req):
                    for key in ("capture_id", "state_rev"):
                        if key in event:
                            self.identity[key] = event[key]
                    return event
        raise StandUnavailable("capture-event-timeout")

    def prepare(self, far: dict[str, Any]) -> None:
        req = self.send(
            "meeting.capture.prepare",
            dir=str(self.directory),
            far=far,
            mic_device="stand_mic.monitor",
            segment_s=300,
            fsync_s=10,
            segment_index_from=0,
        )
        prepared = self.wait("meeting.capture.prepared", req)
        if not prepared["far_available"] or not prepared["server_local"]:
            raise StandUnavailable("capture-target-unavailable")

    def open(self) -> None:
        req = self.send(
            "meeting.capture.open", blocked_epoch=0, admission_mono_ms=int(time.monotonic() * 1000)
        )
        try:
            self.wait("meeting.capture.started", req)
        except StandUnavailable as exc:
            if self.profile is not None:
                self.finish_failed_profile(str(exc))
            raise
        if self.profile is not None:
            self.activate_profile()

    def activate_profile(self) -> None:
        process = self.supervisor.process
        if process is None or not self.owner._owned_group(process) or process.poll() is not None:
            raise StandUnavailable("capture-profile-worker-unavailable")
        fd = os.open(self.profile_gate, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(f"start:{process.pid}")
        os.kill(process.pid, signal.SIGUSR1)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            self.owner.check_interrupt()
            self.tick()
            try:
                assert self.profile is not None
                data = json.loads(self.profile.read_text())
            except (OSError, ValueError):
                continue
            if data.get("process_id") != process.pid:
                continue
            if data.get("status") == "running" and data.get("activated") is True:
                self.profile_activated = True
                return
            if data.get("status") in {"activation-failed", "partial", "not-started"}:
                raise StandUnavailable("capture-profile-activation-failed")
        raise StandUnavailable("capture-profile-activation-timeout")

    def finish_failed_profile(self, diagnostic: str) -> None:
        # Retain the original safe open failure even if bounded shutdown fails.
        # Earlier errors must not hide the new correlated shutdown reply.
        process = self.supervisor.process
        if process is None:
            return
        try:
            since = len(self.events)
            req = self.send("meeting.capture.shutdown")
            self.wait("meeting.capture.bye", req, timeout=5, since=since)
            process.wait(timeout=2)
            self.supervisor.stop()
            self.owner.terminate(process)
            if self.profile is not None:
                from tools.meeting_stand_profile import atomic_dump

                data = json.loads(self.profile.read_text())
                data["capture_error"] = diagnostic
                atomic_dump(self.profile, data)
        except (OSError, ValueError, StandUnavailable, subprocess.TimeoutExpired):
            # run_stand's ProcessOwner still owns bounded cleanup; the previous
            # atomic checkpoint remains useful even when shutdown is unavailable.
            pass

    def tick(self) -> None:
        self.owner.check_interrupt()
        now = time.monotonic()
        if now >= self.keepalive_at:
            self.sequence += 1
            self.send("meeting.capture.keepalive", seq=self.sequence)
            self.keepalive_at = now + 1
        self.supervisor.pump(0.05)
        if any(event["type"] in {"error", "meeting.capture.autopaused"} for event in self.events):
            raise StandUnavailable("capture-interrupted")

    def stop(self) -> None:
        req = self.send("meeting.capture.stop")
        result = self.wait("meeting.capture.finalized", req, timeout=5)
        if result["status"] != "succeeded":
            raise StandFailure("capture-finalization-failed")
        process = self.supervisor.process
        if process is not None:
            self.owner._owned_group(process)
            if self.profile is not None:
                # EOF -> restoring the default SIGTERM handler -> profile dump
                # must finish before supervisor.stop can send another signal.
                # Keep the handle: pump may reap it while receiving bye.
                req = self.send("meeting.capture.shutdown")
                self.wait("meeting.capture.bye", req, timeout=5)
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    raise StandUnavailable("capture-profile-exit-timeout") from None
        self.supervisor.stop()
        if process is not None:
            self.owner.terminate(process)
            if self.profile is not None:
                if process.returncode != 0:
                    raise StandUnavailable("capture-profile-write-failed")
                try:
                    data = json.loads(self.profile.read_text())
                except (OSError, ValueError):
                    raise StandUnavailable("capture-profile-write-failed") from None
                if (
                    not isinstance(data, dict)
                    or data.get("format") != "astra-capture-sampling-v1"
                    or data.get("status")
                    != ("complete" if self.profile_activated else "not-started")
                ):
                    raise StandUnavailable("capture-profile-incomplete")

    def intervals(self) -> list[dict[str, int]]:
        journal = json.loads((self.directory / "capture.json").read_text())
        return [
            {"t_mono_ms": row["t_mono_ms"], "recorded_ms": row["recorded_ms"]}
            for row in journal["boundaries"]
            if row["kind"] in {"open", "close"}
        ]


def _far_evidence(directory: Path) -> dict[str, bytes]:
    return {path.name: path.read_bytes() for path in (directory / "audio").glob("far-*")}


def meeting_directory(root: Path) -> Path:
    started = datetime.now(UTC)
    identity = new_meeting_id(started)
    store = MeetingStore(root / "meetings")
    store.create(Meeting(id=identity, started_at=started.isoformat(), meeting_generation=1))
    return root / "meetings" / identity


def _has_signal(directory: Path) -> bool:
    for track in ("mic", "far"):
        nonzero = False
        for path in (directory / "audio").glob(f"{track}-*.wav"):
            with wave.open(str(path), "rb") as source:
                while chunk := source.readframes(16000):
                    nonzero |= any(
                        abs(sample[0]) > 4000 for sample in struct.iter_unpack("<h", chunk)
                    )
        if not nonzero:
            return False
    return True


def exercise(
    owner: ProcessOwner, root: Path, minutes: float, server: str, profile: Path | None = None
) -> dict[str, Any]:
    from tools.meeting_stand_metrics import (
        ProcSampler,
        analyze_exclusivity,
        analyze_recording,
        summarize_resources,
        verdict,
    )

    audio = AudioServer(owner, root, server)
    audio.start()
    signal_path = root / "beeps.wav"
    seconds = minutes * 60
    onsets = write_signal(signal_path, seconds + 180, train_end_s=seconds - 5)
    music = root / "foreign.wav"
    write_signal(music, seconds + 180, foreign=True)
    sentinel = audio.play(music, "stand_far", foreign=True)
    audio.target(sentinel)  # Wait for the persistent competing stream to exist.
    baseline = audio.snapshot()
    sampler = ProcSampler()
    samples: list[dict[str, Any]] = []
    criteria: list[dict[str, Any]] = []
    reports: dict[str, Any] = {}

    profiles = capture_profile_paths(profile) if profile is not None else {}

    def new_capture(scenario: str) -> Capture:
        capture = Capture(owner, meeting_directory(root), profile=profiles.get(scenario))
        capture.start()
        return capture

    def sample_resources(capture: Capture, now: float) -> None:
        assert capture.supervisor.process is not None
        samples.append(
            sampler.sample(
                {
                    "capture": [capture.supervisor.process.pid],
                    "server": [process.pid for process in audio.daemons],
                },
                now_s=now,
                disk_root=capture.directory,
            )
        )

    def observe(capture: Capture, duration: float, *, resources: bool = False) -> None:
        started = time.monotonic()
        deadline, sample_at = started + duration, started + 1
        if resources:
            sample_resources(capture, started)
        while time.monotonic() < deadline:
            if sentinel.poll() is not None:
                raise StandUnavailable("sentinel-exited")
            capture.tick()
            now = time.monotonic()
            if resources and now >= sample_at:
                sample_resources(capture, now)
                sample_at = now + 1
        if resources:
            sample_resources(capture, time.monotonic())

    def prepare(capture: Capture, target: dict[str, Any], prefix: str = "") -> None:
        before = audio.snapshot()
        capture.prepare(target)
        criteria.append(
            _criterion(prefix + "prepare_zero_streams", not audio.rows("source-outputs"))
        )
        criteria.append(
            _criterion(
                prefix + "prepare_zero_pcm", not any((capture.directory / "audio").iterdir())
            )
        )
        criteria.append(_criterion(prefix + "prepare_graph_unchanged", audio.snapshot() == before))

    started_ms = time.monotonic() * 1000
    far = audio.play(signal_path, "stand_far")
    mic = audio.play(signal_path, "stand_mic")
    target = audio.target(far)
    audio.target(mic)
    normal_routes = audio.snapshot()
    capture = new_capture("normal")
    prepare(capture, target)
    capture.open()
    if time.monotonic() * 1000 >= started_ms + 9000:
        raise StandUnavailable("capture-startup-too-slow")
    observe(capture, seconds, resources=True)
    capture.stop()
    # Both the target and competing player survive stop; route changes cannot
    # disappear merely because the stand removed all sink-inputs first.
    criteria.append(_criterion("normal_graph_unchanged", audio.snapshot() == normal_routes))
    expected_beeps = [started_ms + onset * 1000 for onset in onsets]
    reports["normal"] = analyze_recording(
        capture.directory / "audio", expected_beeps_ms=expected_beeps, intervals=capture.intervals()
    )
    reports["exclusivity"] = analyze_exclusivity(
        capture.directory / "audio", expected_beeps_ms=expected_beeps
    )
    reports["resources"] = summarize_resources(samples)
    for section in ("normal", "exclusivity", "resources"):
        criteria.extend(reports[section]["criteria"])

    # Target disappearance is separate from normal stop. The competing stream
    # has already been playing on the same sink and remains alive for >=10s.
    lost = new_capture("target-gone")
    prepare(lost, target, "target_loss_")
    lost.open()
    observe(lost, 0.5)
    owner.terminate(far)
    owner.terminate(mic)
    ended = lost.wait("meeting.capture.track", timeout=3)
    criteria.append(
        _criterion(
            "far_target_ended", ended.get("track") == "far" and ended.get("state") == "ended"
        )
    )
    observe(lost, 0.5)
    before = _far_evidence(lost.directory)
    observe(lost, 10)
    criteria.append(
        _criterion(
            "foreign_audio_rejected", bool(before) and before == _far_evidence(lost.directory)
        )
    )
    lost.stop()
    criteria.append(_criterion("target_loss_graph_unchanged", audio.snapshot() == baseline))

    # Explicit A path is a separate request; never silently downgrade B-prime.
    a_path = root / "fallback.wav"
    write_signal(a_path, 30, lead_s=2)
    a_far = audio.play(a_path, "stand_far")
    a_mic = audio.play(a_path, "stand_mic")
    audio.target(a_far)
    audio.target(a_mic)
    fallback_routes = audio.snapshot()
    fallback = new_capture("fallback")
    prepare(fallback, {"kind": "sink_monitor"}, "fallback_")
    fallback.open()
    observe(fallback, 6)
    fallback.stop()
    criteria.append(_criterion("fallback_pcm_present", _has_signal(fallback.directory)))
    criteria.append(_criterion("fallback_graph_unchanged", audio.snapshot() == fallback_routes))
    owner.terminate(a_far)
    owner.terminate(a_mic)

    # A separate bounded crash session keeps --minutes 2 below four minutes.
    crash_far = audio.play(signal_path, "stand_far")
    crash_mic = audio.play(signal_path, "stand_mic")
    crash_target = audio.target(crash_far)
    audio.target(crash_mic)
    crash_routes = audio.snapshot()
    crash = new_capture("crash")
    prepare(crash, crash_target, "crash_")
    crash.open()
    observe(crash, 35)
    assert crash.supervisor.process is not None
    crash_mono_ms = time.monotonic() * 1000
    if not owner._owned_group(crash.supervisor.process):
        raise StandUnavailable("capture-exit-unexpected")
    os.killpg(crash.supervisor.process.pid, signal.SIGKILL)
    crash.supervisor.process.wait(timeout=3)
    crash.supervisor.stop()
    owner.terminate(crash.supervisor.process)
    deadline = time.monotonic() + 3
    while audio.rows("source-outputs") and time.monotonic() < deadline:
        time.sleep(0.05)
    criteria.append(_criterion("crash_graph_unchanged", audio.snapshot() == crash_routes))
    reports["crash"] = analyze_recording(
        crash.directory / "audio", intervals=crash.intervals(), crash_mono_ms=crash_mono_ms
    )
    criteria.extend(reports["crash"]["criteria"])
    owner.terminate(crash_far)
    owner.terminate(crash_mic)
    criteria.append(_criterion("final_graph_unchanged", audio.snapshot() == baseline))
    return {
        "criteria": criteria,
        "measurements": reports,
        "rc": verdict(criteria),
        "server_versions": audio.versions,
        "session_mode": "isolated-fly-admission",
        "full_soak": reports["resources"]["full_soak"],
        "projected": reports["resources"]["projected"],
        "crash_planned_s": 70,
        "crash_kill_after_s": 35,
    }


def _unavailable_criteria() -> list[dict[str, Any]]:
    budgets = {
        "capture_cpu": (3.0, "% one core"),
        "capture_rss": (60.0, "MiB"),
        "capture_rss_growth": (20.0, "MiB/h"),
        "disk": (230.0, "MiB/h"),
        "capture_role_limits": (0.0, "violations"),
        "beep_drift": (50.0, "ms"),
        "timecodes": (1000.0, "ms"),
        "crash_loss": (10000.0, "ms"),
        "farforeign_rms_dbfs": (-60.0, "dBFS"),
        "segments": (0.0, "invalid"),
    }
    return [
        {
            "name": name,
            "status": "unavailable",
            "value": None,
            "limit": limit,
            "unit": unit,
            "detail": "stand-incomplete",
        }
        for name, (limit, unit) in budgets.items()
    ] + [
        {
            "name": name,
            "status": "unavailable",
            "value": None,
            "limit": True,
            "unit": "bool",
            "detail": "stand-incomplete",
        }
        for name in (
            "prepare_zero_streams",
            "prepare_zero_pcm",
            "normal_graph_unchanged",
            "crash_graph_unchanged",
            "far_target_ended",
            "foreign_audio_rejected",
            "fallback_pcm_present",
        )
    ]


def run_stand(
    minutes: float = 60,
    server: str = "pipewire",
    keep: bool = False,
    profile: Path | None = None,
) -> tuple[int, dict[str, Any]]:
    report: dict[str, Any] = {
        "server": server,
        "minutes": minutes,
        "diagnostics": [],
        "criteria": _unavailable_criteria(),
        "cleanup": True,
        "exit_code": 2,
        "verdict": "unavailable",
    }
    if profile is not None:
        profile = profile.absolute()
        report["profiling"] = {
            "paths": {name: str(path) for name, path in capture_profile_paths(profile).items()},
            "format": "astra-capture-sampling-v1",
            "timer": "proc-thread-cpu-ticks",
            "backend": "stdlib-sampling",
            "thread_start_fallback": "sections-only-final-counters",
            "activation": "after-meeting.capture.started",
            "startup_profiled": False,
            "sections_timing": "inclusive-do-not-sum-nested-sections",
            "section_timer": "time.thread_time",
            "threads": "live-threads-excluding-sampler",
            "interval_seconds": 0.1,
            "cpu_attribution": "approximate-running-stack-only",
            "checkpoint_interval_seconds": 1.0,
            "resource_measurements_include_profiler_overhead": True,
            "sigkill_dump_guaranteed": False,
        }
    if not math.isfinite(minutes) or minutes <= 0 or server not in {"pipewire", "pulseaudio"}:
        report["diagnostics"] = ["invalid-arguments"]
        return 2, report
    root: Path | None = None
    owner = ProcessOwner({})
    rc = 2
    with interruption(owner):
        try:
            preflight(server)
            try:
                root = Path(tempfile.mkdtemp(prefix="av-meeting-stand-", dir="/tmp"))
            except OSError:
                raise StandUnavailable("workspace-create-failed") from None
            try:
                owner.env = private_environment(root)
            except OSError:
                raise StandUnavailable("environment-create-failed") from None
            report.update(exercise(owner, root, minutes, server, profile))
            owner.check_interrupt()
            rc = int(report.pop("rc"))
        except KeyboardInterrupt:
            report["diagnostics"].append("interrupted")
        except StandFailure as exc:
            report["diagnostics"].append(str(exc))
            report["criteria"].append(
                {
                    "name": "capture_session",
                    "status": "fail",
                    "value": 1,
                    "limit": 0,
                    "unit": "failures",
                    "detail": str(exc),
                }
            )
            rc = 1
        except StandUnavailable as exc:
            report["diagnostics"].append(str(exc))
        except Exception as exc:
            report["diagnostics"].append(_stand_error_code(exc))
        finally:
            if threading.current_thread() is threading.main_thread():
                signal.signal(signal.SIGINT, signal.SIG_IGN)
                signal.signal(signal.SIGTERM, signal.SIG_IGN)
            try:
                owner.close()
                report["cleanup"] = True
            except Exception:
                report["cleanup"] = False
                report["diagnostics"].append("cleanup-unconfirmed")
                rc = 2
            if root is not None and not keep and report["cleanup"]:
                try:
                    shutil.rmtree(root)
                    report["artifacts_removed"] = True
                except FileNotFoundError:
                    report["artifacts_removed"] = True
                except OSError:
                    report["diagnostics"].append("artifact-delete-failed")
                    report["artifacts"] = str(root)
                    rc = 2
            report["exit_code"] = rc
            report["verdict"] = {0: "pass", 1: "fail", 2: "unavailable"}[rc]
            if root is not None and (keep or not report["cleanup"]):
                report["artifacts"] = str(root)
                try:
                    (root / "report.json").write_text(
                        json.dumps(report, ensure_ascii=True, indent=2) + "\n"
                    )
                except OSError:
                    report["diagnostics"].append("artifact-write-failed")
                    rc = 2
                    report["exit_code"], report["verdict"] = rc, "unavailable"
    return rc, report
