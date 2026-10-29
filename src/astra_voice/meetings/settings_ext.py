"""Additive schema 3 user settings; policy interpretation belongs to M1 step 2.

The feature and call offers start disabled; processing preferences follow §3.7.
Loading and disabling settings never traverses or deletes meeting data.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from typing import Any

from astra_voice.meetings import MeetingValidationError
from astra_voice.meetings.model import normalize_text

NOTICE_TEXT = "Я веду запись и расшифровку этой встречи для протокола."
INVITE_TEXT = (
    "На встрече может вестись запись и расшифровка для протокола. "
    "Запись хранится только на компьютере участника, который её ведёт."
)
log = logging.getLogger(__name__)


@dataclass(kw_only=True)
class MeetingSettings:
    meetings_enabled: bool = False
    meeting_offer_on_call: bool = False
    meeting_transcribe_immediately: bool = True
    meeting_diarization: bool = True
    meeting_audio_retention_days: int = 0
    meeting_retention_days: int = 0
    meeting_notice_text: str = NOTICE_TEXT
    meeting_invite_text: str = INVITE_TEXT
    meeting_self_label: str = "Я"
    meeting_ai_endpoint: str = ""
    meeting_ai_model: str = ""
    meeting_ai_replace_names: bool = False


MEETING_DEFAULTS: dict[str, Any] = asdict(MeetingSettings())


def migrate_v2(data: dict[str, Any]) -> dict[str, Any]:
    """Only add missing keys. Preserve existing choices and unknown settings."""
    return {**MEETING_DEFAULTS, **data}


def validate_setting(name: str, value: Any) -> Any:
    """A bad meeting key closes only itself and never alters dictation settings."""
    default = MEETING_DEFAULTS[name]

    def invalid() -> Any:
        log.warning("Настройка встреч %s недопустима, использовано значение по умолчанию", name)
        return default

    if type(value) is not type(default):
        return invalid()
    if isinstance(default, bool):
        return value
    if isinstance(default, int):
        if name == "meeting_audio_retention_days":
            return value if 0 <= value <= 365 else invalid()
        return value if value in (0, 30, 90, 365) else invalid()
    limits = {
        "meeting_notice_text": 500,
        "meeting_invite_text": 500,
        "meeting_self_label": 40,
        "meeting_ai_endpoint": 2048,
        "meeting_ai_model": 128,
    }
    try:
        return normalize_text(
            value,
            limits[name],
            minimum=1
            if name in {"meeting_notice_text", "meeting_invite_text", "meeting_self_label"}
            else 0,
        )
    except MeetingValidationError:
        return invalid()
