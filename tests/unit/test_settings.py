"""Настройки: дефолты, права 0600, атомарная запись, миграции, порча файла."""

from __future__ import annotations

import json
import stat
from pathlib import Path
from typing import Any

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
