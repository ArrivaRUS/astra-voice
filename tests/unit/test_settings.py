"""Настройки: дефолты, права 0600, атомарная запись, миграции, порча файла."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any
from unittest.mock import ANY, Mock, call

import pytest

from astra_voice.core import settings as st

pytestmark = pytest.mark.unit


@pytest.fixture
def path(tmp_path: Path) -> Path:
    return tmp_path / "settings.json"


def test_defaults_when_file_absent(path: Path) -> None:
    s = st.load(path)
    assert s.hotkey == "Ctrl+Space"
    assert s.hotkey_mode == "ptt"
    assert s.pill_enabled is True
    assert s.autostart is True
    assert s.model_id is None
    assert s.language == "ru"
    assert s.check_app_updates is False
    assert s.check_model_updates is False
    assert not path.exists()


def test_save_creates_0600_and_roundtrips(path: Path) -> None:
    s = st.load(path)
    s.hotkey = "Ctrl+Alt+D"
    s.model_id = "gigaam-v3-rnnt"
    st.save(s, path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["schema_version"] == st.SCHEMA_VERSION
    again = st.load(path)
    assert again.hotkey == "Ctrl+Alt+D"
    assert again.model_id == "gigaam-v3-rnnt"


def test_save_leaves_no_temp_file(path: Path) -> None:
    st.save(st.Settings(), path)
    assert [p.name for p in path.parent.iterdir()] == ["settings.json"]


def test_save_syncs_directory_after_replace(path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path.write_text("old settings", encoding="utf-8")
    path.chmod(0o644)
    operations = Mock()
    for name in ("open", "fsync", "replace", "close"):
        spy = Mock(wraps=getattr(os, name))
        operations.attach_mock(spy, name)
        monkeypatch.setattr(os, name, spy)
    settings = st.Settings(hotkey="Ctrl+Alt+D", extra={"device": "Микрофон"})

    st.save(settings, path)

    directory_fd = operations.fsync.call_args.args[0]
    assert operations.mock_calls == [
        call.open(
            path.with_name(".settings.json.tmp"),
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW,
            0o600,
        ),
        call.fsync(ANY),
        call.replace(path.with_name(".settings.json.tmp"), path),
        call.open(path.parent, os.O_RDONLY | os.O_DIRECTORY),
        call.fsync(directory_fd),
        call.close(directory_fd),
    ]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text(encoding="utf-8")) == settings.to_dict()
    assert list(path.parent.iterdir()) == [path]


@pytest.mark.parametrize("stage", ["chmod", "replace", "fsync"])
def test_save_failure_cleans_temp_file(
    path: Path, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    st.save(st.Settings(hotkey="Ctrl+Q"), path)
    monkeypatch.setattr(os, stage, Mock(side_effect=OSError("save failed")))

    with pytest.raises(OSError, match="save failed"):
        st.save(st.Settings(hotkey="Ctrl+W"), path)

    assert list(path.parent.iterdir()) == [path]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert st.load(path).hotkey == "Ctrl+Q"


@pytest.mark.parametrize("stage", ["directory-open", "directory-fsync"])
def test_save_directory_failure_warns_after_successful_replace(
    path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, stage: str
) -> None:
    st.save(st.Settings(hotkey="Ctrl+Q"), path)
    error = OSError("save failed")
    close = Mock(wraps=os.close)
    monkeypatch.setattr(os, "close", close)
    if stage == "directory-open":
        temporary_fd = os.open(
            path.with_name(".settings.json.tmp"), os.O_WRONLY | os.O_CREAT, 0o600
        )
        monkeypatch.setattr(os, "open", Mock(side_effect=[temporary_fd, error]))
    else:
        monkeypatch.setattr(os, "fsync", Mock(side_effect=[None, error]))

    with caplog.at_level("WARNING", logger=st.__name__):
        st.save(st.Settings(hotkey="Ctrl+W"), path)

    assert list(path.parent.iterdir()) == [path]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert st.load(path).hotkey == "Ctrl+W"
    assert any(
        record.name == st.__name__
        and record.levelname == "WARNING"
        and "save failed" in record.getMessage()
        and str(path.parent) in record.getMessage()
        for record in caplog.records
    )
    if stage == "directory-fsync":
        close.assert_called_once()
        with pytest.raises(OSError):
            os.fstat(close.call_args.args[0])


def test_broken_json_is_quarantined(path: Path) -> None:
    path.write_text("{это не json", encoding="utf-8")
    s = st.load(path)
    assert s.hotkey == "Ctrl+Space"
    backups = list(path.parent.glob("settings.json.bak-*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "{это не json"
    assert not path.exists()


def test_non_object_json_is_quarantined(path: Path) -> None:
    path.write_text("[1, 2, 3]", encoding="utf-8")
    st.load(path)
    assert list(path.parent.glob("settings.json.bak-*"))


def test_unknown_keys_are_preserved(path: Path) -> None:
    path.write_text(
        json.dumps({"schema_version": 1, "будущий_ключ": 5, "hotkey": "Ctrl+Q"}),
        encoding="utf-8",
    )
    s = st.load(path)
    assert s.extra["будущий_ключ"] == 5
    st.save(s, path)
    assert json.loads(path.read_text(encoding="utf-8"))["будущий_ключ"] == 5


def test_bad_types_fall_back_to_defaults(path: Path) -> None:
    path.write_text(
        json.dumps({"schema_version": 1, "pill_enabled": "да", "hotkey": 42, "model_id": 7}),
        encoding="utf-8",
    )
    s = st.load(path)
    assert s.pill_enabled is True
    assert s.hotkey == "Ctrl+Space"
    assert s.model_id is None


def test_unknown_hotkey_mode_falls_back(path: Path) -> None:
    path.write_text(json.dumps({"schema_version": 1, "hotkey_mode": "тыр-пыр"}), encoding="utf-8")
    assert st.load(path).hotkey_mode == "ptt"


def test_forward_migration_runs(path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def v0_to_v1(data: dict[str, Any]) -> dict[str, Any]:
        data["hotkey"] = data.pop("shortcut", "Ctrl+Space")
        return data

    monkeypatch.setattr(st, "MIGRATIONS", {0: v0_to_v1})
    path.write_text(json.dumps({"schema_version": 0, "shortcut": "Ctrl+Shift+V"}), encoding="utf-8")
    s = st.load(path)
    assert s.hotkey == "Ctrl+Shift+V"
    assert "shortcut" not in s.extra


def test_missing_schema_version_is_zero(path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []

    def v0_to_v1(data: dict[str, Any]) -> dict[str, Any]:
        calls.append(0)
        return data

    monkeypatch.setattr(st, "MIGRATIONS", {0: v0_to_v1})
    path.write_text(json.dumps({"hotkey": "Ctrl+Space"}), encoding="utf-8")
    st.load(path)
    assert calls == [0]


def test_future_schema_version_kept(path: Path) -> None:
    path.write_text(json.dumps({"schema_version": 99, "hotkey": "Ctrl+J"}), encoding="utf-8")
    assert st.load(path).hotkey == "Ctrl+J"
