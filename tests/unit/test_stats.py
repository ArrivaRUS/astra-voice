"""Локальная статистика: перцентили, ограничение истории, приватность и CLI."""

from __future__ import annotations

import json
import os
import stat
import time
from pathlib import Path

import pytest

from astra_voice import app
from astra_voice.core import paths
from astra_voice.core import stats as st
from astra_voice.core.version import __version__

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def isolated_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))


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


def test_oldest_events_are_evicted(path: Path) -> None:
    stats = st.Stats()
    for index in range(1002):
        stats.append("onboarding", step_reached=index, duration_s=1)
    events = stats.events()
    assert len(events) == 1000
    assert [event["step_reached"] for event in events] == list(range(2, 1002))
    assert st.Stats().events() == events
    assert len(json.loads(path.read_text())["events"]) == 1000


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
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    data = json.loads(path.read_text())
    assert set(data) == {"schema_version", "events"}
    assert data["schema_version"] == 1
    assert data["events"][0]["ts"] == 1234.5
    assert st.Stats().events() == stats.events()
    path.chmod(0o644)
    dictation(stats, 480)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert list(path.parent.iterdir()) == [path]


def test_failed_replace_preserves_previous_events(
    path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stats = st.Stats()
    dictation(stats, 100)
    before = path.read_bytes()

    def fail_replace(src: object, dst: object) -> None:
        raise OSError("cannot replace")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError):
        dictation(stats, 200)
    assert path.read_bytes() == before
    assert stats.summary()["dictations"] == 1
    assert list(path.parent.iterdir()) == [path]


def test_temp_symlink_is_not_followed(path: Path, tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.write_bytes(b"unchanged")
    path.with_name(".stats.json.tmp").symlink_to(target)
    with pytest.raises(OSError):
        st.Stats().clear()
    assert target.read_bytes() == b"unchanged"


def test_read_symlink_is_not_followed(path: Path, tmp_path: Path) -> None:
    stats = st.Stats()
    dictation(stats, 100)
    target = tmp_path / "target"
    path.rename(target)
    path.symlink_to(target)
    assert st.Stats().events() == []


def test_clear_empties_memory_and_file(path: Path) -> None:
    stats = st.Stats()
    dictation(stats, 100)
    stats.clear()
    assert stats.events() == []
    assert st.Stats().events() == []
    assert json.loads(path.read_text()) == {"schema_version": 1, "events": []}
    assert stats.summary()["result_shares"] == {"ok": 0.0, "empty": 0.0, "cancelled": 0.0}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_unknown_fields_are_discarded(path: Path, caplog: pytest.LogCaptureFixture) -> None:
    stats = st.Stats()
    stats.append(
        "onboarding", step_reached=1, duration_s=2, text="private-content", unknown="secret"
    )
    event = stats.events()[0]
    assert set(event) == {"type", "ts", "step_reached", "duration_s"}
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


def test_version_precedes_stats(capsys: pytest.CaptureFixture[str]) -> None:
    assert app.main(["--version", "--stats"]) == 0
    assert capsys.readouterr().out == f"{app.APP_NAME} {__version__}\n"
    assert not (Path(os.environ["XDG_STATE_HOME"]) / "astra-voice").exists()
