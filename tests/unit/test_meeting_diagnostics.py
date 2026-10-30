"""T-439: archive members are generated allowlisted summaries, never raw files."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from unittest.mock import Mock

import pytest

from astra_voice.core.diagnostics import collect_diagnostics, endpoint_class
from astra_voice.core.settings import Settings

pytestmark = pytest.mark.unit
MARKER = "PRIVATE_DIAGNOSTICS_MARKER"


@pytest.mark.parametrize(
    "endpoint,expected",
    [
        ("", "unset"),
        ("off", "off"),
        ("https://127.0.0.1/private", "internal"),
        ("https://[::1]/private", "internal"),
        ("https://10.0.0.4/private", "internal"),
        ("https://[fd00::1]/private", "internal"),
        ("https://api.example.org/private", "external"),
        ("https://private.corp/secret", "external"),
        ("https://user:secret@example.org", "unset"),
    ],
)
def test_endpoint_summary_is_class_only_without_dns(
    monkeypatch: pytest.MonkeyPatch, endpoint: str, expected: str
) -> None:
    import socket

    lookup = Mock(side_effect=AssertionError("DNS forbidden"))
    monkeypatch.setattr(socket, "getaddrinfo", lookup)
    assert endpoint_class(endpoint) == expected
    lookup.assert_not_called()


def test_archive_whitelist_excludes_data_keys_checkpoint_logs_and_settings_text(
    tmp_path: Path,
) -> None:
    data = tmp_path / "data"
    data.mkdir(mode=0o700)
    for relative in [
        "meetings/id/meeting.json",
        "meetings/id/transcript.json",
        "meetings/id/checkpoint.json",
        "checkpoint.json",
        "meeting-ai-key",
        "logs/astra-voice.log",
        "settings.json",
        "other.txt",
    ]:
        path = data / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(MARKER)
    (data / "stats.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "events": [
                    {
                        "type": "meeting_capture",
                        "ts": 1,
                        "bytes": 42,
                        "app_class": "dion",
                        "segment_text": MARKER,
                        "code": MARKER,
                    }
                ],
            }
        )
    )
    settings = Settings(
        meeting_ai_endpoint="https://api.example.org/" + MARKER,
        meeting_notice_text=MARKER,
        meeting_self_label=MARKER,
        extra={"future": MARKER},
    )
    (data / "stats.json").chmod(0o600)
    output = tmp_path / "diagnostics.zip"
    collect_diagnostics(output, settings=settings, data_dir=data, stats_dir=data)
    with zipfile.ZipFile(output) as archive:
        assert set(archive.namelist()) == {"summary.json", "stats.json"}
        content = b"".join(archive.read(name) for name in archive.namelist())
        assert MARKER.encode() not in content
        assert b"api.example.org" not in content
        summary = json.loads(archive.read("summary.json"))
        assert summary["settings"]["meeting_ai_endpoint"] == "external"
        assert json.loads(archive.read("stats.json"))["events"][0]["bytes"] == 42


def test_stats_symlink_is_not_followed(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir(mode=0o700)
    outside = tmp_path / "outside"
    outside.write_text(MARKER)
    (data / "stats.json").symlink_to(outside)
    output = tmp_path / "diagnostics.zip"
    collect_diagnostics(output, settings=Settings(), data_dir=data, stats_dir=data)
    with zipfile.ZipFile(output) as archive:
        assert json.loads(archive.read("stats.json"))["events"] == []
    assert outside.read_text() == MARKER


def test_app_backend_delegates_without_loading_ui(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from astra_voice import app
    from astra_voice.core import diagnostics

    output = tmp_path / "diag.zip"
    data = tmp_path / "data"
    settings = Settings()
    backend = Mock(return_value=output)
    monkeypatch.setattr(diagnostics, "collect_diagnostics", backend)
    assert app.collect_diagnostics(output, settings=settings, data_dir=data) == output
    backend.assert_called_once_with(output, settings=settings, data_dir=data)


def test_default_archive_reads_real_xdg_stats_separately_from_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from astra_voice.core import paths
    from astra_voice.core import stats as st

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setattr(st, "_pending", {})
    stats = st.Stats()
    stats.append("meeting_capture", bytes=123, app_class="browser")
    stats.flush()
    output = tmp_path / "diag.zip"
    collect_diagnostics(output, settings=Settings(), data_dir=paths.data_dir_path())
    with zipfile.ZipFile(output) as archive:
        saved = json.loads(archive.read("stats.json"))["events"]
    assert saved[0]["bytes"] == 123 and saved[0]["app_class"] == "browser"


def test_source_allowlist_and_legacy_stats_strip_free_strings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from astra_voice.core import diagnostics
    from astra_voice.security.files import read_private

    data = tmp_path / "data"
    data.mkdir(mode=0o700)
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    (state / "stats.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "events": [
                    {
                        "type": "dictation",
                        "ts": 1,
                        "model_id": MARKER,
                        "result": "ok",
                        "audio_ms": 42,
                        "text": MARKER,
                    }
                ],
            }
        )
    )
    reader = Mock(wraps=read_private)
    (state / "stats.json").chmod(0o600)
    monkeypatch.setattr(diagnostics, "read_private", reader)
    output = tmp_path / "diag.zip"
    collect_diagnostics(output, settings=Settings(), data_dir=data, stats_dir=state)
    assert len(reader.call_args_list) == 1 and reader.call_args.args[0] == Path("stats.json")
    with zipfile.ZipFile(output) as archive:
        snapshot = archive.read("stats.json")
        assert MARKER.encode() not in snapshot
        assert json.loads(snapshot)["events"][0]["audio_ms"] == 42


def test_stats_source_cannot_be_meeting_subtree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from astra_voice.core import diagnostics

    data = tmp_path / "data"
    reader = Mock(side_effect=AssertionError("meeting files must not be opened"))
    monkeypatch.setattr(diagnostics, "read_private", reader)
    collect_diagnostics(
        tmp_path / "diag.zip", settings=Settings(), data_dir=data, stats_dir=data / "meetings/id"
    )
    reader.assert_not_called()


@pytest.mark.parametrize(
    "source", ["meetings/id/transcript.json", "checkpoint.json", "meeting-ai-key"]
)
def test_stats_hardlink_to_excluded_valid_document_produces_empty_snapshot(
    tmp_path: Path, source: str
) -> None:
    import os

    data, state = tmp_path / "data", tmp_path / "state"
    data.mkdir(mode=0o700)
    state.mkdir(mode=0o700)
    excluded = data / source
    excluded.parent.mkdir(parents=True, exist_ok=True)
    excluded.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "events": [
                    {"type": "meeting_capture", "ts": 1, "bytes": 987654, "app_class": "dion"}
                ],
            }
        )
    )
    excluded.chmod(0o600)
    os.link(excluded, state / "stats.json")
    output = tmp_path / "diag.zip"
    collect_diagnostics(output, settings=Settings(), data_dir=data, stats_dir=state)
    with zipfile.ZipFile(output) as archive:
        assert json.loads(archive.read("stats.json")) == {"schema_version": 1, "events": []}
    assert excluded.stat().st_nlink == 2
