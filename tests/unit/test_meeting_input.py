"""UI edits reject invalid input; failed persistence stays visible and retryable."""

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import Mock

import pytest
from PyQt5.QtCore import QCoreApplication
from PyQt5.QtTest import QSignalSpy

from astra_voice.core.settings import Settings
from astra_voice.meetings.apps import AppClass, AppDefinition
from astra_voice.meetings.settings_ext import MEETING_DEFAULTS
from astra_voice.ui.meeting_bridge import MeetingBridge

pytestmark = pytest.mark.unit
SAVE_ERROR = "Не удалось сохранить настройки. Попробуйте ещё раз."
TEXT_LIMITS = {
    "meeting_notice_text": 500,
    "meeting_invite_text": 500,
    "meeting_self_label": 40,
    "meeting_ai_endpoint": 2048,
    "meeting_ai_model": 128,
}
OLD_VALUES: dict[str, object] = {
    "meeting_offer_on_call": True,
    "meeting_transcribe_immediately": False,
    "meeting_diarization": False,
    "meeting_audio_retention_days": 7,
    "meeting_retention_days": 30,
    "meeting_notice_text": "Прежнее уведомление",
    "meeting_invite_text": "Прежнее приглашение",
    "meeting_self_label": "Прежнее имя",
    "meeting_ai_endpoint": "https://old.example/v1",
    "meeting_ai_model": "old-model",
    "meeting_ai_replace_names": True,
}


@pytest.fixture(scope="module", autouse=True)
def application() -> QCoreApplication:
    from helpers.qt_app import get_qapplication

    return get_qapplication()


@pytest.fixture
def editor() -> Iterator[tuple[MeetingBridge, Settings, Settings, Mock]]:
    stored, mirror, save = Settings(), Settings(), Mock()
    for key, value in OLD_VALUES.items():
        setattr(stored, key, value)
        setattr(mirror, key, value)
    app = AppDefinition("dion", "DION", ("dion",), (), AppClass.DION)
    bridge = MeetingBridge(stored, mirror=mirror, save=save, store=Mock(), known_apps=(app,))
    yield bridge, stored, mirror, save
    bridge.shutdown()


@pytest.mark.parametrize(
    "key,value",
    [(key, None) for key in OLD_VALUES]
    + [("meeting_audio_retention_days", value) for value in (-1, 366, True, "7", 7.0)]
    + [("meeting_retention_days", value) for value in (-1, 7, 366, False, "30", 30.0)]
    + [("meeting_diarization", value) for value in (0, 1, "false")]
    + [(key, "a" * (limit + 1)) for key, limit in TEXT_LIMITS.items()]
    + [(key, "bad" + char) for key in TEXT_LIMITS for char in ("\x00", "\n", "\u202e")]
    + [
        ("meeting_ai_endpoint", value)
        for value in (
            "https://http://evil.example",
            "https://example.com/%0a",
            "http://127.0.0.1/v1",
            "https://example.com:",
        )
    ],
)
def test_invalid_input_preserves_saved_and_mirror_without_write(
    editor: tuple[MeetingBridge, Settings, Settings, Mock], key: str, value: object
) -> None:
    bridge, stored, mirror, save = editor
    changed = QSignalSpy(bridge.changed)
    bridge.setSetting(key, value)
    assert getattr(stored, key) == getattr(mirror, key) == OLD_VALUES[key]
    save.assert_not_called()
    assert len(changed) == 1  # Restore the control's effective value after a rejected edit.


@pytest.mark.parametrize("key", TEXT_LIMITS)
def test_explicit_empty_text_is_allowed_reset(
    editor: tuple[MeetingBridge, Settings, Settings, Mock], key: str
) -> None:
    bridge, stored, mirror, save = editor
    bridge.setSetting(key, "")
    assert getattr(stored, key) == getattr(mirror, key) == MEETING_DEFAULTS[key]
    save.assert_called_once()


@pytest.mark.parametrize("key", [key for key in TEXT_LIMITS if key != "meeting_ai_endpoint"])
def test_nfc_normalization_precedes_length_validation(
    editor: tuple[MeetingBridge, Settings, Settings, Mock], key: str
) -> None:
    bridge, stored, mirror, save = editor
    bridge.setSetting(key, "e\u0301" * TEXT_LIMITS[key])
    assert getattr(stored, key) == getattr(mirror, key) == "é" * TEXT_LIMITS[key]
    save.assert_called_once()


@pytest.mark.parametrize("failure", [OSError("/private/path"), ValueError("private content")])
def test_failed_edit_exposes_only_fixed_error_until_successful_save(
    editor: tuple[MeetingBridge, Settings, Settings, Mock], failure: Exception
) -> None:
    bridge, stored, mirror, save = editor
    changed = QSignalSpy(bridge.changed)
    assert bridge.saveError == ""
    save.side_effect = failure
    bridge.setSetting("meeting_self_label", "Новое имя")
    assert bridge.saveError == SAVE_ERROR
    assert (
        stored.meeting_self_label == mirror.meeting_self_label == OLD_VALUES["meeting_self_label"]
    )
    assert len(changed) == 1
    bridge.setSetting("meeting_self_label", "bad\u202e")
    assert bridge.saveError == SAVE_ERROR
    assert save.call_count == 1
    save.side_effect = None
    bridge.setSetting("meeting_self_label", "Новое имя")
    assert bridge.saveError == ""
    assert stored.meeting_self_label == mirror.meeting_self_label == "Новое имя"
    assert save.call_count == 2


def test_d1_failed_save_stays_open_and_retry_commits_opt_in(
    editor: tuple[MeetingBridge, Settings, Settings, Mock],
) -> None:
    bridge, stored, mirror, save = editor
    save.side_effect = OSError("/private/path")
    bridge.openResponsibility()
    bridge.acceptResponsibility(False)
    assert bridge.responsibilityOpen
    assert not stored.meetings_enabled and not mirror.meetings_enabled
    assert bridge.saveError == SAVE_ERROR
    bridge.cancelResponsibility()
    assert bridge.saveError == SAVE_ERROR
    save.side_effect = None
    bridge.openResponsibility()
    bridge.acceptResponsibility(False)
    assert not bridge.responsibilityOpen
    assert stored.meetings_enabled and mirror.meetings_enabled
    assert bridge.saveError == ""


def test_unavailable_offer_republishes_effective_value() -> None:
    save = Mock()
    bridge = MeetingBridge(Settings(), save=save, store=Mock(), known_apps=())
    changed = QSignalSpy(bridge.changed)
    bridge.setSetting("meeting_offer_on_call", True)
    assert bridge.settings["meeting_offer_on_call"] is False
    save.assert_not_called()
    assert len(changed) == 1
    bridge.shutdown()
