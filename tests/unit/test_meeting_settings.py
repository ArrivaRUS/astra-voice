"""E13 M1: additive settings migration and old-reader interoperability."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

from astra_voice.core import settings
from astra_voice.meetings import settings_ext

pytestmark = pytest.mark.unit


@pytest.fixture
def schema2_reader(tmp_path: Path) -> ModuleType:
    """Use the actual baseline reader, not a reimplementation of schema 2."""
    # Exact src/astra_voice/core/settings.py from 316d91d, vendored so shallow
    # CI checkouts exercise the real old reader without Git/network dependencies.
    source = (Path(__file__).parents[1] / "fixtures/settings-schema2-316d91d.txt").read_bytes()
    assert hashlib.sha256(source).hexdigest() == (
        "7d0e0a1e269b610cead828c8b98619b8e81fed0b0299be475d1ac2a370536b31"
    )
    path = tmp_path / "schema2_settings.py"
    path.write_bytes(source)
    spec = importlib.util.spec_from_file_location("e13_schema2_reader", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_meeting_defaults_are_disabled() -> None:
    raw = settings.Settings().to_dict()
    assert settings.SCHEMA_VERSION == raw["schema_version"] == 3
    for key in (
        "meetings_enabled",
        "meeting_offer_on_call",
        "meeting_ai_replace_names",
    ):
        assert raw[key] is False
    assert raw["meeting_transcribe_immediately"] is True
    assert raw["meeting_diarization"] is True
    assert raw["meeting_ai_endpoint"] == ""
    assert raw["meeting_audio_retention_days"] == raw["meeting_retention_days"] == 0
    assert raw["meeting_notice_text"] == "Я веду запись и расшифровку этой встречи для протокола."
    assert raw["meeting_invite_text"] == (
        "На встрече может вестись запись и расшифровка для протокола. "
        "Запись хранится только на компьютере участника, который её ведёт."
    )


def test_dictation_positional_constructor_keeps_its_meaning() -> None:
    loaded = settings.Settings("Alt+Space")
    assert loaded.hotkey == "Alt+Space"
    assert loaded.meetings_enabled is False


def test_migration_2_3_2_preserves_old_and_new_fields(
    tmp_path: Path, schema2_reader: ModuleType
) -> None:
    path = tmp_path / "settings.json"
    original = schema2_reader.Settings(
        hotkey="Ctrl+Alt+X",
        model_id="kept-model",
        sound_cues_enabled=True,
        extra={"future": {"nested": [1, 2]}},
    )
    schema2_reader.save(original, path)
    migrated = settings.load(path, appimage=False)
    assert json.loads(path.read_text())["schema_version"] == 3
    new_fields = {
        "meetings_enabled": True,
        "meeting_offer_on_call": True,
        "meeting_transcribe_immediately": True,
        "meeting_diarization": True,
        "meeting_ai_replace_names": True,
        "meeting_self_label": "Тест",
        "meeting_audio_retention_days": 30,
        "meeting_retention_days": 90,
        "meeting_notice_text": "Уведомление",
        "meeting_invite_text": "Приглашение",
        "meeting_ai_endpoint": "https://example.org/v1",
        "meeting_ai_model": "test-model",
    }
    assert set(new_fields) == set(settings_ext.MEETING_DEFAULTS)
    for key, value in new_fields.items():
        setattr(migrated, key, value)
    settings.save(migrated, path)
    older = schema2_reader.load(path, appimage=False)
    for key, value in new_fields.items():
        assert older.extra[key] == value
    assert older.to_dict()["schema_version"] == 2
    older.language = "fr"
    schema2_reader.save(older, path)
    assert json.loads(path.read_text())["schema_version"] == 2
    again = settings.load(path, appimage=False)
    assert again.meetings_enabled and again.meeting_self_label == "Тест"
    assert again.language == "fr" and again.hotkey == original.hotkey
    assert again.model_id == original.model_id and again.sound_cues_enabled
    assert again.extra["future"] == original.extra["future"]
    for key, value in original.to_dict().items():
        if key != "schema_version":
            assert again.to_dict()[key] == ("fr" if key == "language" else value), key
    for key, value in new_fields.items():
        assert again.to_dict()[key] == value, key
    assert not list(tmp_path.glob("*.bak-*"))


@pytest.mark.parametrize(
    "key,value",
    [
        ("meetings_enabled", "true"),
        ("meeting_offer_on_call", 1),
        ("meeting_audio_retention_days", -1),
        ("meeting_audio_retention_days", 366),
        ("meeting_retention_days", True),
        ("meeting_retention_days", 31),
        ("meeting_retention_days", 36501),
        ("meeting_notice_text", "x" * 501),
        ("meeting_self_label", "x" * 41),
        ("meeting_self_label", "A\u202eB"),
    ],
)
def test_invalid_meeting_key_defaults_without_touching_dictation(key: str, value: object) -> None:
    loaded = settings.from_dict({key: value, "hotkey": "Ctrl+Alt+X", "sound_cues_enabled": True})
    assert getattr(loaded, key) == getattr(settings_ext.MeetingSettings(), key)
    assert loaded.hotkey == "Ctrl+Alt+X" and loaded.sound_cues_enabled


def test_disable_preserves_meetings_and_nfc(tmp_path: Path) -> None:
    meeting = tmp_path / "meetings" / "private.txt"
    meeting.parent.mkdir()
    meeting.write_bytes(b"preserved")
    path = tmp_path / "settings.json"
    loaded = settings.from_dict({"meetings_enabled": True, "meeting_self_label": "е\u0308"})
    assert loaded.meeting_self_label == "ё"
    loaded.meetings_enabled = False
    settings.save(loaded, path)
    assert meeting.read_bytes() == b"preserved"
    assert not settings.load(path, appimage=False).meetings_enabled


def test_invalid_setting_warns_with_key_only(caplog: pytest.LogCaptureFixture) -> None:
    for key, value in (
        ("meeting_self_label", "PRIVATE_MARKER\u202e"),
        ("meeting_retention_days", -1),
        ("meetings_enabled", "PRIVATE_MARKER"),
    ):
        caplog.clear()
        with caplog.at_level("WARNING", logger=settings_ext.__name__):
            settings_ext.validate_setting(key, value)
        assert len(caplog.records) == 1
        assert key in caplog.text and "PRIVATE_MARKER" not in caplog.text
