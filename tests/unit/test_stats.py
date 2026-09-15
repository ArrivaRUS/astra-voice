"""Локальная статистика: перцентили, ограничение истории, приватность и CLI."""

from __future__ import annotations

import json
import os
import runpy
import stat
import sys
import time
from pathlib import Path
from unittest.mock import Mock

import pytest
from test_runtime import Rig

from astra_voice import app
from astra_voice.core import paths
from astra_voice.core import stats as st
from astra_voice.core.version import __version__
from astra_voice.runtime import DictationRuntime

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def isolated_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(st, "_pending", {})


@pytest.fixture
def path() -> Path:
    return paths.state_dir() / "stats.json"


def dictation(stats: st.Stats, t_ms: float, *, cold: bool = False, result: str = "ok") -> None:
    stats.append(
        "dictation",
        model_id="gigaam-v3",
        audio_ms=6000,
        t_ms=t_ms,
        paste_ms=10,
        cold=cold,
        result=result,
    )


def test_percentile_empty() -> None:
    assert st.percentile([], 50) is None


@pytest.mark.parametrize("q", [0, 50, 95, 100])
def test_percentile_single(q: float) -> None:
    assert st.percentile([42.5], q) == 42.5


def test_percentile_exactly_1000() -> None:
    values = [float(value) for value in range(1000, 0, -1)]
    assert st.percentile(values, 50) == 500.0
    assert st.percentile(values, 95) == 950.0
    assert st.percentile(values, 0) == 1.0
    assert st.percentile(values, 100) == 1000.0


def test_percentile_nearest_rank_does_not_mutate_input() -> None:
    values = [40.0, 10.0, 30.0, 20.0]
    assert st.percentile(values, 50) == 20.0
    assert st.percentile(values, 95) == 40.0
    assert values == [40.0, 10.0, 30.0, 20.0]


@pytest.mark.parametrize("q", [-1, 101, float("nan"), float("inf")])
def test_percentile_rejects_invalid_percentage(q: float) -> None:
    with pytest.raises(ValueError):
        st.percentile([1.0], q)


def test_state_dir_is_private_and_follows_xdg(tmp_path: Path) -> None:
    assert paths.state_dir() == tmp_path / "state" / "astra-voice"
    assert stat.S_IMODE(paths.state_dir().stat().st_mode) == 0o700


@pytest.mark.parametrize("xdg", [None, "", "relative"])
def test_state_dir_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, xdg: str | None
) -> None:
    if xdg is None:
        monkeypatch.delenv("XDG_STATE_HOME")
    else:
        monkeypatch.setenv("XDG_STATE_HOME", xdg)
    assert paths.state_dir() == tmp_path / "home" / ".local" / "state" / "astra-voice"


def test_state_dir_rejects_symlink(tmp_path: Path) -> None:
    base = tmp_path / "state"
    base.mkdir()
    (base / "astra-voice").symlink_to(tmp_path)
    with pytest.raises(paths.PathError):
        paths.state_dir()


def test_oldest_events_are_evicted(path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(time, "monotonic", lambda: 0.0)
    stats = st.Stats()
    for index in range(1002):
        stats.append("onboarding", step_reached=index, duration_s=1)
    events = stats.events()
    assert len(events) == 1000
    assert [event["step_reached"] for event in events] == list(range(2, 1002))
    assert st.Stats().events() == events
    assert not path.exists()
    stats.flush()
    assert json.loads(path.read_text())["events"] == events
    stats = st.Stats()
    stats.append("onboarding", step_reached=1002, duration_s=1)
    assert [event["step_reached"] for event in stats.events()] == list(range(3, 1003))
    stats.flush()
    assert json.loads(path.read_text())["events"] == stats.events()
    assert st.Stats().events() == stats.events()


def test_many_appends_save_only_once_per_interval(
    path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = 0.0
    monkeypatch.setattr(time, "monotonic", lambda: now)
    stats = st.Stats()
    saves = 0
    replace = os.replace

    def count_replace(src: Path, dst: Path) -> None:
        nonlocal saves
        replace(src, dst)
        saves += 1

    monkeypatch.setattr(os, "replace", count_replace)
    for _ in range(100):
        dictation(stats, 100)
    assert saves == 0
    assert not path.exists()
    now = st.SAVE_INTERVAL_S
    for _ in range(100):
        dictation(stats, 200)
    assert saves == 1
    assert len(json.loads(path.read_text())["events"]) == 101
    now = 2 * st.SAVE_INTERVAL_S - 0.001
    dictation(stats, 300)
    assert saves == 1
    now = 2 * st.SAVE_INTERVAL_S
    dictation(stats, 400)
    assert saves == 2
    assert json.loads(path.read_text())["events"] == stats.events()
    stats.flush()
    assert saves == 2


def test_flush_saves_all_pending_events(path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(time, "monotonic", lambda: 0.0)
    stats = st.Stats()
    for index in range(200):
        dictation(stats, index)
    assert not path.exists()
    stats.flush()
    assert json.loads(path.read_text()) == {"schema_version": 1, "events": stats.events()}
    assert len(st.Stats().events()) == 200
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_pending_events_are_visible_to_readers(path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(time, "monotonic", lambda: 0.0)
    stats = st.Stats()
    dictation(stats, 100)
    reader = st.Stats()
    dictation(stats, 200, result="empty")
    assert not path.exists()
    assert reader.events() == stats.events()
    assert reader.summary() == stats.summary()
    assert stats.summary()["dictations"] == 2
    assert stats.summary()["p50_ms"] == 100.0
    assert stats.summary()["p95_ms"] == 200.0
    reader.flush()
    assert json.loads(path.read_text())["events"] == stats.events()


def test_cold_events_count_but_do_not_affect_percentiles() -> None:
    stats = st.Stats()
    dictation(stats, 100)
    dictation(stats, 200, result="empty")
    dictation(stats, 300, result="cancelled")
    dictation(stats, 9000, cold=True)
    stats.append("mic_error", kind="busy", recovered_by="retry")
    summary = st.Stats().summary()
    assert summary == {
        "dictations": 4,
        "p50_ms": 200.0,
        "p95_ms": 300.0,
        "results": {"ok": 2, "empty": 1, "cancelled": 1},
        "result_shares": {"ok": 0.5, "empty": 0.25, "cancelled": 0.25},
        "mic_errors": 1,
    }
    assert any(event.get("cold") is True for event in st.Stats().events())


def test_only_cold_events_have_no_speed() -> None:
    stats = st.Stats()
    dictation(stats, 9000, cold=True)
    assert stats.summary()["p50_ms"] is None
    assert stats.summary()["p95_ms"] is None
    assert stats.summary()["dictations"] == 1


@pytest.mark.parametrize(
    "payload",
    [
        b"{private-content",
        b"\xff",
        b"[]",
        b'{"schema_version": 1, "events": null}',
        b'{"schema_version": 2, "events": []}',
        b'{"schema_version": 1, "events": [42]}',
        b'{"schema_version": 1, "events": [{"type": "dictation", "ts": "private-content"}]}',
    ],
)
def test_corrupt_file_is_empty_without_logging_contents(
    path: Path, caplog: pytest.LogCaptureFixture, payload: bytes
) -> None:
    path.write_bytes(payload)
    assert st.Stats().events() == []
    assert any(record.levelname == "WARNING" for record in caplog.records)
    assert "private-content" not in caplog.text


def test_unreadable_file_is_empty(
    path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def denied(*args: object, **kwargs: object) -> int:
        raise PermissionError("private-content")

    monkeypatch.setattr(os, "open", denied)
    assert st.Stats().events() == []
    assert caplog.records
    assert "private-content" not in caplog.text


def test_save_is_private_and_roundtrips(path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(time, "time", lambda: 1234.5)
    stats = st.Stats()
    dictation(stats, 310)
    stats.flush()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    data = json.loads(path.read_text())
    assert set(data) == {"schema_version", "events"}
    assert data["schema_version"] == 1
    assert data["events"][0]["ts"] == 1234.5
    assert st.Stats().events() == stats.events()
    path.chmod(0o644)
    dictation(stats, 480)
    stats.flush()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert list(path.parent.iterdir()) == [path]


def test_failed_replace_preserves_previous_events(
    path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stats = st.Stats()
    dictation(stats, 100)
    stats.flush()
    before = path.read_bytes()
    replace = os.replace

    def fail_replace(src: object, dst: object) -> None:
        raise OSError("cannot replace")

    monkeypatch.setattr(os, "replace", fail_replace)
    dictation(stats, 200)
    with pytest.raises(OSError):
        stats.flush()
    assert path.read_bytes() == before
    assert stats.summary()["dictations"] == 2
    assert list(path.parent.iterdir()) == [path]
    monkeypatch.setattr(os, "replace", replace)
    stats.flush()
    assert json.loads(path.read_text())["events"] == stats.events()


def test_failed_fsync_preserves_file_and_pending_events(
    path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stats = st.Stats()
    dictation(stats, 100)
    stats.flush()
    before = path.read_bytes()
    dictation(stats, 200)

    def fail_fsync(fd: int) -> None:
        raise OSError("cannot sync")

    with monkeypatch.context() as patch:
        patch.setattr(os, "fsync", fail_fsync)
        with pytest.raises(OSError):
            stats.flush()
    assert path.read_bytes() == before
    assert list(path.parent.iterdir()) == [path]
    assert stats.summary()["dictations"] == 2
    stats.flush()
    assert json.loads(path.read_text())["events"] == stats.events()


def test_shutdown_flushes_after_worker_stop(path: Path) -> None:
    stats = st.Stats()
    dictation(stats, 100)
    runtime = Mock(
        _closed=False,
        _started=False,
        notifier=None,
        tick_timer=None,
        stats_timer=None,
        timers=set(),
        stats=stats,
    )
    runtime._cleanup = DictationRuntime._cleanup
    runtime.supervisor.stop.side_effect = lambda: dictation(stats, 200)
    DictationRuntime.shutdown(runtime)
    assert json.loads(path.read_text())["events"] == stats.events()
    assert len(stats.events()) == 2
    DictationRuntime.shutdown(runtime)
    runtime.supervisor.stop.assert_called_once_with()


def test_temp_symlink_is_not_followed(path: Path, tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.write_bytes(b"unchanged")
    path.with_name(".stats.json.tmp").symlink_to(target)
    stats = st.Stats()
    dictation(stats, 100)
    stats.flush()
    assert target.read_bytes() == b"unchanged"
    assert json.loads(path.read_text())["events"] == stats.events()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not path.with_name(".stats.json.tmp").is_symlink()


def test_read_symlink_is_not_followed(path: Path, tmp_path: Path) -> None:
    stats = st.Stats()
    dictation(stats, 100)
    stats.flush()
    target = tmp_path / "target"
    path.rename(target)
    path.symlink_to(target)
    assert st.Stats().events() == []


def test_clear_empties_memory_and_file(path: Path) -> None:
    stats = st.Stats()
    dictation(stats, 100)
    stats.flush()
    dictation(stats, 200)
    reader = st.Stats()
    stats.clear()
    assert stats.events() == []
    assert reader.events() == []
    assert st.Stats().events() == []
    assert json.loads(path.read_text()) == {"schema_version": 1, "events": []}
    assert stats.summary()["result_shares"] == {"ok": 0.0, "empty": 0.0, "cancelled": 0.0}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    stats.flush()
    assert json.loads(path.read_text())["events"] == []


def test_unknown_fields_are_discarded(path: Path, caplog: pytest.LogCaptureFixture) -> None:
    stats = st.Stats()
    stats.append(
        "onboarding", step_reached=1, duration_s=2, text="private-content", unknown="secret"
    )
    event = stats.events()[0]
    assert set(event) == {"type", "ts", "step_reached", "duration_s"}
    stats.flush()
    assert "private-content" not in path.read_text()
    assert "secret" not in path.read_text()
    assert len(caplog.records) == 2
    assert "private-content" not in caplog.text
    assert "secret" not in caplog.text


def test_unknown_fields_from_disk_are_not_resaved(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "events": [{"type": "onboarding", "ts": 1, "text": "private-content"}],
            }
        )
    )
    stats = st.Stats()
    stats.append("onboarding", step_reached=2, duration_s=1)
    stats.flush()
    assert "private-content" not in path.read_text()


@pytest.mark.parametrize(
    ("event_type", "fields"),
    [
        (
            "model_measure",
            {
                "model_id": "gigaam",
                "revision": "v3",
                "peak_rss_mb": 700,
                "threads": 2,
                "cpu": "Core Ultra 7 255U",
            },
        ),
        ("update_check", {"source": "models", "result": "none"}),
        ("update_apply", {"kind": "app", "track": "B", "result": "ok"}),
    ],
)
def test_other_event_schemas(event_type: str, fields: dict[str, object]) -> None:
    stats = st.Stats()
    stats.append(event_type, **fields)
    event = st.Stats().events()[0]
    assert event == {"type": event_type, "ts": event["ts"], **fields}
    assert isinstance(event["ts"], float)


def test_returned_events_cannot_mutate_storage() -> None:
    stats = st.Stats()
    dictation(stats, 100)
    events = stats.events()
    events[0]["t_ms"] = 9000
    events.clear()
    assert stats.summary()["p50_ms"] == 100.0


@pytest.fixture
def no_gui(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_start() -> Path:
        pytest.fail("--stats must return before GUI startup")

    monkeypatch.setattr(app, "lock_path", fail_start)
    monkeypatch.delenv("DISPLAY", raising=False)


@pytest.mark.usefixtures("no_gui")
def test_cli_empty(capsys: pytest.CaptureFixture[str]) -> None:
    assert app.main(["--stats"]) == 0
    assert capsys.readouterr().out == "Пока нет данных.\n"


@pytest.mark.usefixtures("no_gui")
def test_cli_summary(capsys: pytest.CaptureFixture[str]) -> None:
    stats = st.Stats()
    dictation(stats, 310)
    dictation(stats, 480, result="empty")
    dictation(stats, 9000, cold=True, result="cancelled")
    stats.append("mic_error", kind="none", recovered_by="none")
    assert app.main(["--stats", "--debug-transcribe", "unused.wav"]) == 0
    assert capsys.readouterr().out == (
        "Диктовок: 3\n"
        "Скорость: обычно 310 мс, в худших случаях 480 мс\n"
        "Успешно: 1, пусто: 1, отменено: 1\n"
        "Ошибок микрофона: 1\n"
    )


@pytest.mark.usefixtures("no_gui")
def test_cli_only_cold(capsys: pytest.CaptureFixture[str]) -> None:
    dictation(st.Stats(), 9000, cold=True)
    assert app.main(["--stats"]) == 0
    assert "Скорость: пока нет данных.\n" in capsys.readouterr().out


def test_validate_sees_pending_events(
    path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "path", sys.path.copy())
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setattr(time, "monotonic", lambda: 0.0)
    module = runpy.run_path(str(Path(__file__).resolve().parents[2] / "tools/validate"))
    stats = st.Stats()
    dictation(stats, 310)
    dictation(stats, 480)
    assert not path.exists()
    assert module["main"](["stats", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["events"] == payload["dictations"] == 2
    assert payload["p50_ms"] == 310.0
    assert payload["p95_ms"] == 480.0
    assert not path.exists()


def test_version_precedes_stats(capsys: pytest.CaptureFixture[str]) -> None:
    assert app.main(["--version", "--stats"]) == 0
    assert capsys.readouterr().out == f"{app.APP_NAME} {__version__}\n"
    assert not (Path(os.environ["XDG_STATE_HOME"]) / "astra-voice").exists()


def test_runtime_timer_flushes_without_new_append(
    path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = Rig(monkeypatch)
    stats = st.Stats()
    rig.runtime.stats = stats
    flush = Mock(wraps=stats.flush)
    monkeypatch.setattr(stats, "flush", flush)
    rig.runtime.start()
    timer = next(t for t in rig.timers if t.interval == int(st.SAVE_INTERVAL_S * 1000))
    assert not timer.single_shot
    for count in (1, 2):
        dictation(stats, count * 100)
        if count == 1:
            assert not path.exists()
        else:
            events = json.loads(path.read_text())["events"]
            assert len([event for event in events if event["type"] == "dictation"]) == count - 1
        timer.fire()
        assert flush.call_count == count
        assert json.loads(path.read_text())["events"] == stats.events()
    rig.runtime.shutdown()
    assert timer.deleted and not timer.active
    flush.reset_mock()
    timer.timeout.emit()
    flush.assert_not_called()


def test_runtime_stats_timer_retries_after_save_failure(
    path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    rig = Rig(monkeypatch)
    stats = st.Stats()
    rig.runtime.stats = stats
    rig.runtime.start()
    timer = next(t for t in rig.timers if t.interval == int(st.SAVE_INTERVAL_S * 1000))
    dictation(stats, 100)
    with monkeypatch.context() as patch:
        patch.setattr(stats, "flush", Mock(side_effect=OSError("private-content")))
        timer.fire()
    assert not path.exists()
    assert "private-content" not in caplog.text
    timer.fire()
    assert json.loads(path.read_text())["events"] == stats.events()
    rig.runtime.shutdown()


@pytest.mark.parametrize("result", ["ok", "fail"])
def test_model_selfcheck_schema_roundtrips(path: Path, result: str) -> None:
    stats = st.Stats()
    fields = dict(
        model_id="gigaam",
        revision="r3",
        result=result,
        engine_version="1.24.4",
        cpu_model="Intel Xeon",
    )
    stats.append("model_selfcheck", **fields)
    stats.flush()
    event = st.Stats().events()[0]
    assert event == {"type": "model_selfcheck", "ts": event["ts"], **fields}


@pytest.mark.parametrize("key_role", ["text", "command"])
@pytest.mark.parametrize("result", ["ok", "busy", "regrabbed"])
@pytest.mark.parametrize("attempts", [0, 1, 2.5])
def test_hotkey_grab_schema(key_role: str, result: str, attempts: int | float) -> None:
    stats = st.Stats()
    stats.append("hotkey_grab", key_role=key_role, result=result, attempts=attempts)
    event = stats.events()[0]
    assert event == {
        "type": "hotkey_grab",
        "ts": event["ts"],
        "key_role": key_role,
        "result": result,
        "attempts": attempts,
    }


@pytest.mark.parametrize("kind", ["device-changed", "device-lost"])
def test_microphone_device_events(kind: str) -> None:
    stats = st.Stats()
    stats.append("mic_error", kind=kind, recovered_by="none")
    event = stats.events()[0]
    assert event == {"type": "mic_error", "ts": event["ts"], "kind": kind, "recovered_by": "none"}


@pytest.mark.parametrize(
    ("event_type", "fields"),
    [
        ("model_selfcheck", {"result": "garbage"}),
        ("model_selfcheck", {"result": True}),
        ("model_selfcheck", {"engine_version": 123}),
        ("model_selfcheck", {"cpu_model": []}),
        ("hotkey_grab", {"key_role": "garbage"}),
        ("hotkey_grab", {"result": "garbage"}),
        ("mic_error", {"kind": "device-garbage"}),
        *[
            ("hotkey_grab", {"attempts": value})
            for value in (True, -1, "2", None, float("nan"), float("inf"))
        ],
    ],
)
def test_new_schema_rejects_invalid_values(event_type: str, fields: dict[str, object]) -> None:
    stats = st.Stats()
    with pytest.raises(ValueError):
        stats.append(event_type, **fields)
    assert stats.events() == []


@pytest.mark.parametrize("event_type", ["model_selfcheck", "hotkey_grab"])
def test_new_schema_discards_unknown_fields(
    path: Path, caplog: pytest.LogCaptureFixture, event_type: str
) -> None:
    stats = st.Stats()
    stats.append(event_type, text="private-content", garbage="private-content")
    event = stats.events()[0]
    assert event == {"type": event_type, "ts": event["ts"]}
    stats.flush()
    assert "private-content" not in path.read_text()
    assert "private-content" not in caplog.text
