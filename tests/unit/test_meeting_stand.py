"""Offline orchestration contracts; no connection to a user's audio server."""

from __future__ import annotations

import json
import os
import signal
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest
from tools import meeting_stand as stand

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("filesystem_euid")]


def test_private_environment_ignores_inherited_endpoints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for key in (
        "PULSE_SERVER",
        "DBUS_SESSION_BUS_ADDRESS",
        "NOTIFY_SOCKET",
        "PIPEWIRE_REMOTE",
        "DISPLAY",
        "LD_PRELOAD",
    ):
        monkeypatch.setenv(key, "host-secret")
    env = stand.private_environment(tmp_path)
    assert "host-secret" not in env.values()
    assert env["PULSE_SERVER"] == f"unix:{tmp_path}/runtime/pulse/native"
    assert env["PIPEWIRE_RUNTIME_DIR"] == env["XDG_RUNTIME_DIR"]
    assert env["XDG_CURRENT_DESKTOP"] == "FLY"
    for key in ("HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_RUNTIME_DIR"):
        assert Path(env[key]).is_relative_to(tmp_path)
        assert Path(env[key]).stat().st_mode & 0o777 == 0o700


def test_missing_binary_fails_without_start(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("tools.meeting_stand.shutil.which", lambda *args, **kwargs: None)
    spawn = Mock(side_effect=AssertionError("must not start"))
    monkeypatch.setattr("tools.meeting_stand.subprocess.Popen", spawn)
    rc, report = stand.run_stand(minutes=1)
    assert rc == 2
    assert report["diagnostics"] == ["missing-binary"]
    spawn.assert_not_called()


@pytest.mark.parametrize("minutes", [0, -1, float("nan"), float("inf")])
def test_invalid_duration_does_not_start(minutes: float) -> None:
    rc, report = stand.run_stand(minutes=minutes)
    assert rc == 2 and report["diagnostics"] == ["invalid-arguments"]


def test_cleanup_never_signals_recycled_pid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = stand.ProcessOwner(stand.private_environment(tmp_path))
    process = Mock(pid=987654)
    owner.register(process)
    monkeypatch.setattr(owner, "_owned_group", lambda child: False)
    killpg = Mock()
    monkeypatch.setattr("tools.meeting_stand.os.killpg", killpg)
    owner.close()
    killpg.assert_not_called()
    process.wait.assert_called()


def test_cleanup_signals_only_verified_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = stand.ProcessOwner(stand.private_environment(tmp_path))
    process = Mock(pid=987654)
    owner.register(process)
    monkeypatch.setattr(owner, "_owned_group", lambda child: True)
    killpg = Mock()
    monkeypatch.setattr("tools.meeting_stand.os.killpg", killpg)
    owner.close()
    assert [call.args for call in killpg.call_args_list] == [
        (987654, signal.SIGTERM),
        (987654, signal.SIGKILL),
    ]


def test_subprocess_is_new_session_and_private_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = stand.private_environment(tmp_path)
    popen = Mock(return_value=Mock(pid=987654))
    monkeypatch.setattr("tools.meeting_stand.subprocess.Popen", popen)
    owner = stand.ProcessOwner(env)
    result = owner.start(["/bin/true"])
    assert owner.processes == [result]
    assert popen.call_args.kwargs["start_new_session"] is True
    assert popen.call_args.kwargs["env"] == env


def test_worker_command_resets_environment(tmp_path: Path) -> None:
    env = stand.private_environment(tmp_path)
    argv = stand.capture_command(9, env)
    assert argv[:2] == ["/usr/bin/env", "-i"]
    assert f"PULSE_SERVER={env['PULSE_SERVER']}" in argv
    assert argv[-2:] == ["meeting-capture", "9"]
    assert os.environ.get("PULSE_SERVER") != env["PULSE_SERVER"]


@pytest.mark.parametrize("keep", [False, True])
def test_failure_always_cleans_processes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, keep: bool
) -> None:
    monkeypatch.setattr(stand, "preflight", lambda server: None)
    monkeypatch.setattr(
        "tools.meeting_stand.tempfile.mkdtemp", lambda **kwargs: str(tmp_path / "private")
    )
    close = Mock()
    monkeypatch.setattr(stand.ProcessOwner, "close", close)
    failure = RuntimeError("secret device host")

    def origin() -> None:
        raise failure

    def exercise(*args: object) -> None:
        origin()

    monkeypatch.setattr(stand, "exercise", exercise)
    rc, report = stand.run_stand(minutes=1, keep=keep)
    traceback = failure.__traceback__
    assert traceback is not None
    while traceback.tb_next is not None:
        traceback = traceback.tb_next
    assert traceback.tb_frame.f_code.co_name == "origin"
    assert traceback.tb_lineno > 0
    assert rc == 2 and report["diagnostics"] == [
        f"stand-error:RuntimeError@test_meeting_stand.py:{traceback.tb_lineno}"
    ]
    close.assert_called_once()
    assert (tmp_path / "private").exists() is keep
    assert "secret" not in str(report)


@pytest.mark.parametrize(
    "filename,basename",
    [
        ("/sensitive/user/worker.py", "worker.py"),
        ("/sensitive/user/секрет.py", "unknown"),
        ("/sensitive/user/worker\nsecret.py", "unknown"),
        ("/sensitive/user/" + "x" * 129 + ".py", "unknown"),
    ],
)
def test_unexpected_failure_diagnostic_sanitizes_traceback_filename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, filename: str, basename: str
) -> None:
    failure = KeyError("private sound device")

    def origin(*args: object) -> None:
        raise failure

    origin.__code__ = origin.__code__.replace(co_filename=filename)
    monkeypatch.setattr(stand, "preflight", lambda server: None)
    monkeypatch.setattr(
        "tools.meeting_stand.tempfile.mkdtemp", lambda **kwargs: str(tmp_path / "private")
    )
    monkeypatch.setattr(stand.ProcessOwner, "close", Mock())
    monkeypatch.setattr(stand, "exercise", origin)
    rc, report = stand.run_stand(minutes=1)
    traceback = failure.__traceback__
    assert traceback is not None
    while traceback.tb_next is not None:
        traceback = traceback.tb_next
    assert traceback.tb_lineno > 0
    assert rc == 2
    assert report["diagnostics"] == [f"stand-error:KeyError@{basename}:{traceback.tb_lineno}"]
    assert "sensitive" not in str(report)
    assert "private sound" not in str(report)


def test_interrupt_restores_signal_handler_and_cleans(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(stand, "preflight", lambda server: None)
    monkeypatch.setattr(
        "tools.meeting_stand.tempfile.mkdtemp", lambda **kwargs: str(tmp_path / "private")
    )
    close = Mock()
    monkeypatch.setattr(stand.ProcessOwner, "close", close)
    monkeypatch.setattr(stand, "exercise", Mock(side_effect=KeyboardInterrupt))
    previous = signal.getsignal(signal.SIGTERM)
    rc, report = stand.run_stand(minutes=1)
    assert rc == 2 and report["diagnostics"] == ["interrupted"]
    assert signal.getsignal(signal.SIGTERM) == previous
    close.assert_called_once()


def test_signal_during_spawn_is_deferred_until_child_registered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = stand.ProcessOwner(stand.private_environment(tmp_path))
    process = Mock(pid=987654)

    def spawn(*args: object, **kwargs: object) -> Mock:
        os.kill(os.getpid(), signal.SIGTERM)
        return process

    monkeypatch.setattr("tools.meeting_stand.subprocess.Popen", spawn)
    with stand.interruption(owner):
        with pytest.raises(KeyboardInterrupt):
            owner.start(["/bin/true"])
    assert owner.processes == [process]


def test_signal_during_cleanup_is_ignored_and_handler_restored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(stand, "preflight", lambda server: None)
    monkeypatch.setattr(
        "tools.meeting_stand.tempfile.mkdtemp", lambda **kwargs: str(tmp_path / "private")
    )
    monkeypatch.setattr(stand, "exercise", Mock(side_effect=KeyboardInterrupt))
    completed: list[bool] = []

    def close(self: stand.ProcessOwner) -> None:
        os.kill(os.getpid(), signal.SIGTERM)
        completed.append(True)

    monkeypatch.setattr(stand.ProcessOwner, "close", close)
    previous = signal.getsignal(signal.SIGTERM)
    rc, report = stand.run_stand(minutes=1)
    assert rc == 2 and report["cleanup"] is True and completed
    assert signal.getsignal(signal.SIGTERM) == previous


def test_capture_commands_use_real_wire_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from astra_voice.worker.meeting_contract import validate_message

    directory = stand.meeting_directory(tmp_path)
    owner = stand.ProcessOwner(stand.private_environment(tmp_path))
    capture = stand.Capture(owner, directory)
    messages: list[dict[str, object]] = []

    def send(message: dict[str, object]) -> None:
        messages.append(validate_message(message, "meeting-capture"))

    monkeypatch.setattr(capture.supervisor, "send", send)
    capture.send(
        "meeting.capture.prepare",
        dir=str(directory),
        far={"kind": "sink_monitor"},
        mic_device="stand_mic.monitor",
        segment_s=300,
        fsync_s=10,
        segment_index_from=0,
    )
    capture.identity.update(capture_id="capture-1", state_rev=0)
    capture.send("meeting.capture.open", blocked_epoch=0, admission_mono_ms=1)
    capture.send("meeting.capture.keepalive", seq=1)
    capture.send("meeting.capture.stop")
    assert len(messages) == 4
    assert (directory / "meeting.json").is_file()
    assert (directory / "audio").is_dir()


def test_train_has_silent_startup_and_termination_margin(tmp_path: Path) -> None:
    import wave

    path = tmp_path / "beeps.wav"
    onsets = stand.write_signal(path, 90, train_end_s=55)
    assert onsets == list(range(10, 55, 5))
    with wave.open(str(path), "rb") as wav:
        assert not any(wav.readframes(160000))
        assert any(wav.readframes(2400))
        wav.setpos(55 * 16000)
        assert not any(wav.readframes(35 * 16000))


@pytest.mark.parametrize("field", ["sink", "object.serial", "volume", "argument"])
def test_snapshot_detects_persistent_route_and_module_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:

    owner = stand.ProcessOwner(stand.private_environment(tmp_path))
    server = stand.AudioServer(owner, tmp_path, "pipewire")
    inputs: list[dict[str, Any]] = [
        {
            "index": 10,
            "sink": 2,
            "mute": False,
            "volume": {"mono": {"value": 65536}},
            "properties": {"application.process.id": "42", "object.serial": "77"},
        }
    ]
    modules: list[dict[str, Any]] = [
        {"index": 4, "name": "module-null-sink", "argument": "rate=16000"}
    ]

    def rows(kind: str) -> list[dict[str, Any]]:
        return inputs if kind == "sink-inputs" else modules if kind == "modules" else []

    monkeypatch.setattr(server, "rows", rows)
    monkeypatch.setattr(server, "pactl", lambda *args: b"stand\n")
    before = server.snapshot()
    if field == "argument":
        modules[0][field] = "rate=48000"
    elif field == "object.serial":
        inputs[0]["properties"][field] = "78"
    else:
        inputs[0][field] = 9
    assert before != server.snapshot()


@pytest.fixture(params=["pulseaudio-16.1", "pipewire-pulse"])
def snapshot_server(
    request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[stand.AudioServer, dict[str, list[dict[str, Any]]]]:
    fixture = Path(__file__).parents[1] / "fixtures/meeting_stand" / f"{request.param}.json"
    rows: dict[str, list[dict[str, Any]]] = json.loads(fixture.read_text())["rows"]
    owner = stand.ProcessOwner(stand.private_environment(tmp_path))
    server = stand.AudioServer(owner, tmp_path, "pipewire")
    monkeypatch.setattr(server, "rows", lambda kind: rows[kind])
    monkeypatch.setattr(server, "pactl", lambda *args: b"stand\n")
    return server, rows


def test_snapshot_accepts_pactl_modules_without_index_and_ignores_order(
    snapshot_server: tuple[stand.AudioServer, dict[str, list[dict[str, Any]]]],
) -> None:
    server, rows = snapshot_server
    assert all("index" not in module for module in rows["modules"])
    before = server.snapshot()
    for items in rows.values():
        items.reverse()
    assert server.snapshot() == before


@pytest.mark.parametrize("kind", ["sinks", "sources", "sink-inputs", "source-outputs"])
@pytest.mark.parametrize("identity", ["id", "fallback"])
def test_snapshot_accepts_alternate_identity_without_index(
    snapshot_server: tuple[stand.AudioServer, dict[str, list[dict[str, Any]]]],
    kind: str,
    identity: str,
) -> None:
    server, rows = snapshot_server
    for row in rows[kind]:
        index = row.pop("index")
        if identity == "id":
            row["id"] = index
    before = server.snapshot()
    rows[kind].reverse()
    assert server.snapshot() == before
    if identity == "id":
        rows[kind][0]["id"] = 999
    elif kind in {"sinks", "sources"}:
        rows[kind][0]["name"] = "replacement"
    else:
        rows[kind][0]["properties"]["application.process.id"] = "999"
    assert server.snapshot() != before


@pytest.mark.parametrize("kind", ["modules", "sinks", "sources", "sink-inputs", "source-outputs"])
def test_snapshot_preserves_duplicate_multiplicity(
    snapshot_server: tuple[stand.AudioServer, dict[str, list[dict[str, Any]]]], kind: str
) -> None:
    server, rows = snapshot_server
    before = server.snapshot()
    rows[kind].append(dict(rows[kind][0]))
    assert server.snapshot() != before


@pytest.mark.parametrize("kind", ["sink-inputs", "source-outputs"])
@pytest.mark.parametrize("field", ["route", "pid", "serial", "mute", "volume"])
def test_snapshot_compares_both_stream_directions(
    snapshot_server: tuple[stand.AudioServer, dict[str, list[dict[str, Any]]]],
    kind: str,
    field: str,
) -> None:
    server, rows = snapshot_server
    before = server.snapshot()
    stream = rows[kind][0]
    if field == "route":
        stream["sink" if kind == "sink-inputs" else "source"] = 999
    elif field in {"pid", "serial"}:
        stream["properties"]["application.process.id" if field == "pid" else "object.serial"] = (
            "999"
        )
    else:
        stream[field] = True if field == "mute" else {"mono": {"value": 42}}
    assert server.snapshot() != before


@pytest.mark.parametrize("field", ["argument", "arguments", "driver", "name", "id", "index"])
def test_snapshot_compares_module_identity_and_arguments(
    snapshot_server: tuple[stand.AudioServer, dict[str, list[dict[str, Any]]]], field: str
) -> None:
    server, rows = snapshot_server
    module = rows["modules"][0]
    if field == "arguments":
        module[field] = module.pop("argument")
    elif field in {"id", "index"}:
        module[field] = 1
    before = server.snapshot()
    module[field] = 999 if field in {"id", "index"} else "changed"
    assert server.snapshot() != before


@pytest.mark.parametrize("kind", ["modules", "sinks", "sources", "sink-inputs", "source-outputs"])
@pytest.mark.parametrize("invalid", [{}, {"index": None, "id": ""}, {"index": False}])
def test_snapshot_missing_identity_is_unavailable_instead_of_false_pass(
    snapshot_server: tuple[stand.AudioServer, dict[str, list[dict[str, Any]]]],
    kind: str,
    invalid: dict[str, Any],
) -> None:
    server, rows = snapshot_server
    rows[kind] = [invalid.copy()]
    with pytest.raises(stand.StandUnavailable, match="^snapshot-identity-unavailable$"):
        server.snapshot()


@pytest.mark.parametrize("kind", ["sink-inputs", "source-outputs"])
def test_snapshot_stream_pid_alone_is_not_sufficient_identity(
    snapshot_server: tuple[stand.AudioServer, dict[str, list[dict[str, Any]]]], kind: str
) -> None:
    server, rows = snapshot_server
    rows[kind] = [{"properties": {"application.process.id": "42"}}]
    with pytest.raises(stand.StandUnavailable, match="^snapshot-identity-unavailable$"):
        server.snapshot()


@pytest.mark.parametrize("kind", ["sink-inputs", "source-outputs"])
@pytest.mark.parametrize("same_route", [False, True])
@pytest.mark.parametrize("serial", [None, "", False, -1])
def test_snapshot_ambiguous_pid_fallback_cannot_hide_swapped_routes(
    snapshot_server: tuple[stand.AudioServer, dict[str, list[dict[str, Any]]]],
    kind: str,
    same_route: bool,
    serial: str | int | None,
) -> None:
    server, rows = snapshot_server
    route = "sink" if kind == "sink-inputs" else "source"
    rows[kind] = [
        {
            "driver": "protocol-native.c",
            route: 1 if same_route else index,
            "properties": {
                "application.process.id": "42",
                "media.name": name,
                "object.serial": serial,
            },
        }
        for index, name in [(1, "stream-A"), (2, "stream-B")]
    ]
    with pytest.raises(stand.StandUnavailable, match="^snapshot-identity-unavailable$"):
        server.snapshot()


@pytest.mark.parametrize(
    "phase,code",
    [
        ("mkdtemp", "workspace-create-failed"),
        ("environment", "environment-create-failed"),
        ("write", "artifact-write-failed"),
        ("delete", "artifact-delete-failed"),
    ],
)
def test_filesystem_failure_returns_sanitized_report_and_cleans(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str, code: str
) -> None:
    monkeypatch.setattr(stand, "preflight", lambda server: None)
    monkeypatch.setattr(
        "tools.meeting_stand.tempfile.mkdtemp", lambda **kwargs: str(tmp_path / "private")
    )
    monkeypatch.setattr(stand, "exercise", lambda *args: {"rc": 0, "criteria": []})
    close = Mock()
    monkeypatch.setattr(stand.ProcessOwner, "close", close)
    failure = Mock(side_effect=OSError("sensitive host/device path"))
    if phase == "mkdtemp":
        monkeypatch.setattr("tools.meeting_stand.tempfile.mkdtemp", failure)
    elif phase == "environment":
        monkeypatch.setattr(stand, "private_environment", failure)
    elif phase == "delete":
        monkeypatch.setattr("tools.meeting_stand.shutil.rmtree", failure)
    else:
        original = Path.write_text

        def write(self: Path, data: str, *args: object, **kwargs: object) -> int:
            if self.name == "report.json":
                raise OSError("sensitive host/device path")
            return original(self, data)

        monkeypatch.setattr(Path, "write_text", write)
    rc, report = stand.run_stand(minutes=1, keep=phase == "write")
    assert rc == 2 and report["exit_code"] == 2 and report["verdict"] == "unavailable"
    assert code in report["diagnostics"]
    assert report["cleanup"] is True
    assert "sensitive" not in str(report)
    close.assert_called_once()


def test_exercise_complete_sequence_keeps_sentinel_and_uses_all_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:

    from tools import meeting_stand_metrics as metrics

    now = [100.0]
    events: list[str] = []
    players: list[Any] = []
    captures: list[Any] = []
    analyses: list[str] = []
    owner = Mock(spec=stand.ProcessOwner)
    owner._owned_group.return_value = True

    def terminate(process: Any) -> None:
        events.append(f"terminate:{process.pid}")
        process.live = False

    owner.terminate.side_effect = terminate
    monkeypatch.setattr("tools.meeting_stand.time.monotonic", lambda: now[0])
    monkeypatch.setattr(
        "tools.meeting_stand.time.sleep", lambda seconds: now.__setitem__(0, now[0] + seconds)
    )
    server = Mock()
    server.daemons = [Mock(pid=99)]
    server.versions = {"pipewire": "1.6.2"}

    def play(path: Path, sink: str, *, foreign: bool = False) -> Any:
        process = Mock(pid=len(players) + 1000)
        process.live = True
        process.poll.side_effect = lambda: None if process.live else 0
        process.foreign = foreign
        process.sink = sink
        players.append(process)
        events.append("sentinel" if foreign else "play")
        return process

    server.play.side_effect = play
    server.target.side_effect = lambda process: {
        "kind": "app_stream",
        "sink_input": process.pid,
        "object_serial": 7,
    }
    server.rows.side_effect = lambda kind: (
        [{"index": c.supervisor.process.pid} for c in captures if c.active]
        if kind == "source-outputs"
        else []
    )
    snapshots: list[tuple[int, ...]] = []

    def snapshot() -> dict[str, Any]:
        live = tuple(p.pid for p in players if p.live)
        snapshots.append(live)
        return {"routes": live, "outputs": len([c for c in captures if c.active])}

    server.snapshot.side_effect = snapshot
    monkeypatch.setattr(stand, "AudioServer", lambda *args: server)

    class FakeCapture:
        def __init__(self, owner: Any, directory: Path) -> None:
            self.directory = directory
            self.active = False
            self.supervisor = Mock(process=Mock(pid=2000 + len(captures)))
            self.supervisor.stop.side_effect = self.stopped
            captures.append(self)

        def start(self) -> None:
            events.append("start")

        def prepare(self, target: dict[str, Any]) -> None:
            events.append("prepare:" + target["kind"])
            assert players[0].foreign and players[0].live

        def open(self) -> None:
            events.append("open")
            self.active = True
            (self.directory / "audio/far-0000.wav").write_bytes(b"fixed-evidence")

        def tick(self) -> None:
            assert players[0].live
            now[0] += 0.25

        def stopped(self) -> None:
            self.active = False

        def stop(self) -> None:
            events.append("stop")
            self.stopped()

        def wait(self, kind: str, **kwargs: Any) -> dict[str, str]:
            assert players[0].live
            events.append("target-ended")
            return {"track": "far", "state": "ended"}

        def intervals(self) -> list[dict[str, int]]:
            return []

    def killpg(pid: int, number: int) -> None:
        assert number == signal.SIGKILL
        assert players[0].live and sum(p.live for p in players) == 3
        events.append("kill9")
        for capture in captures:
            if capture.supervisor.process.pid == pid:
                capture.active = False

    monkeypatch.setattr("tools.meeting_stand.os.killpg", killpg)
    monkeypatch.setattr(stand, "Capture", FakeCapture)
    monkeypatch.setattr(stand, "_has_signal", lambda directory: True)
    sampler = Mock()
    sampler.sample.side_effect = lambda roles, **kwargs: {"t_mono_s": kwargs["now_s"]}
    monkeypatch.setattr(metrics, "ProcSampler", lambda: sampler)

    def analyze(path: Path, **kwargs: Any) -> dict[str, Any]:
        analyses.append("crash" if "crash_mono_ms" in kwargs else "normal")
        return {"criteria": [{"name": analyses[-1], "status": "pass"}]}

    def exclusive(path: Path, **kwargs: Any) -> dict[str, Any]:
        analyses.append("exclusive")
        assert players[0].live and kwargs["expected_beeps_ms"]
        return {"criteria": [{"name": "farforeign_rms_dbfs", "status": "pass"}]}

    def resources(samples: list[dict[str, Any]]) -> dict[str, Any]:
        assert samples[-1]["t_mono_s"] - samples[0]["t_mono_s"] >= 120
        return {"criteria": [], "full_soak": False, "projected": True}

    monkeypatch.setattr(metrics, "analyze_recording", analyze)
    monkeypatch.setattr(metrics, "analyze_exclusivity", exclusive, raising=False)
    monkeypatch.setattr(metrics, "summarize_resources", resources)
    result = stand.exercise(owner, tmp_path, 2, "pipewire")
    assert result["rc"] == 0
    import wave

    with wave.open(str(tmp_path / "beeps.wav"), "rb") as wav:
        assert wav.getnframes() / wav.getframerate() >= 120 + 180
    assert events[0] == "sentinel"
    assert analyses == ["normal", "exclusive", "crash"]
    assert events.count("prepare:app_stream") == 3
    assert events.count("prepare:sink_monitor") == 1
    assert events.count("kill9") == 1
    assert all(1000 in snapshot for snapshot in snapshots)
    assert any(len(snapshot) == 3 for snapshot in snapshots)
    assert len(captures) == 4
    names = {item["name"] for item in result["criteria"]}
    assert {
        "prepare_zero_pcm",
        "normal_graph_unchanged",
        "crash_graph_unchanged",
        "foreign_audio_rejected",
        "farforeign_rms_dbfs",
    } <= names
    assert now[0] < 340  # --minutes 2 plus all side scenarios remains under four minutes.


@pytest.mark.parametrize("status", ["failed", "partial"])
def test_authoritative_finalization_failure_is_fail_not_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    capture = stand.Capture(stand.ProcessOwner({}), tmp_path / "meeting")
    monkeypatch.setattr(capture, "send", lambda *args, **kwargs: "stop-request")
    monkeypatch.setattr(capture, "wait", lambda *args, **kwargs: {"status": status})
    monkeypatch.setattr(stand, "preflight", lambda server: None)
    monkeypatch.setattr(
        "tools.meeting_stand.tempfile.mkdtemp", lambda **kwargs: str(tmp_path / "private")
    )
    monkeypatch.setattr(stand, "exercise", lambda *args: capture.stop())
    rc, report = stand.run_stand(minutes=1)
    assert rc == 1 and report["exit_code"] == 1 and report["verdict"] == "fail"
    assert report["cleanup"] is True
    assert "capture-finalization-failed" in report["diagnostics"]
    assert any(
        row["name"] == "capture_session" and row["status"] == "fail" for row in report["criteria"]
    )
