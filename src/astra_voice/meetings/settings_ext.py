"""Additive schema 3 user settings; policy interpretation belongs to M1 step 2.

The feature and call offers start disabled; processing preferences follow §3.7.
Loading and disabling settings never traverses or deletes meeting data.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from astra_voice.meetings import MeetingValidationError, events
from astra_voice.meetings.model import normalize_text

if TYPE_CHECKING:
    from astra_voice.core.settings import Settings
    from astra_voice.meetings.policy import MeetingPolicy, PolicyValue

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
        log.warning(events.SETTING_INVALID, events.setting_key(name))
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


@dataclass(frozen=True)
class EffectiveMeetingSettings:
    """A settings copy plus UI metadata; computing it performs no I/O or opt-in."""

    settings: Settings
    policy: MeetingPolicy
    controls: Mapping[str, PolicyValue[object]]
    explanation: str
    key_editable: bool = True  # V3: the API key is not in Settings or policy.

    @property
    def recording_allowed(self) -> bool:
        return self.settings.meetings_enabled and self.policy.recording_allowed

    @property
    def detector_allowed(self) -> bool:
        return (
            self.recording_allowed
            and self.settings.meeting_offer_on_call
            and self.policy.detector_allowed
        )


def effective_settings(settings: Settings, policy: MeetingPolicy) -> EffectiveMeetingSettings:
    """Intersect user choices with meeting policy without enabling the feature.

    Local imports avoid a settings → policy → settings import cycle. No UI,
    filesystem access, subscriptions or writes are triggered by this function.
    """
    from astra_voice.meetings.policy import (
        POLICY_EXPLANATION,
        REASON_ADMIN,
        REASON_MANUAL,
        REASON_RECORD_OFF,
        PolicyValue,
        RecordMode,
        validate_endpoint,
    )

    result = replace(settings, extra=dict(settings.extra))
    controls: dict[str, PolicyValue[object]] = {}
    for name in MEETING_DEFAULTS:
        value = validate_setting(name, getattr(settings, name))
        setattr(result, name, value)
        controls[name] = PolicyValue(value)
    for name in (
        "meeting_diarization",
        "meeting_audio_retention_days",
        "meeting_notice_text",
        "meeting_ai_endpoint",
    ):
        decision = policy.decisions[name]
        if decision.locked:
            value = (
                "" if name == "meeting_ai_endpoint" and decision.value == "off" else decision.value
            )
            setattr(result, name, value)
            controls[name] = PolicyValue(value, True, decision.reason, decision.code)
    if policy.meeting_record.value == RecordMode.OFF:
        reason = (
            REASON_RECORD_OFF
            if policy.meeting_record.reason == REASON_ADMIN
            else policy.meeting_record.reason
        )
        for name in ("meetings_enabled", "meeting_offer_on_call"):
            setattr(result, name, False)
            controls[name] = PolicyValue(False, True, reason, policy.meeting_record.code)
    elif policy.meeting_record.value == RecordMode.MANUAL:
        result.meeting_offer_on_call = False
        controls["meeting_offer_on_call"] = PolicyValue(
            False, True, REASON_MANUAL, policy.meeting_record.code
        )
    if result.meeting_ai_endpoint:
        try:
            # A user URL cannot borrow a plaintext grant intended for an endpoint
            # from the same administrative snapshot. DNS/transport remains M9.
            result.meeting_ai_endpoint = validate_endpoint(
                result.meeting_ai_endpoint,
                allow_http=policy.meeting_ai_endpoint.locked
                and policy.meeting_ai_allow_plaintext.value,
            )
        except (ValueError, UnicodeError):
            result.meeting_ai_endpoint = ""
    endpoint_control = controls["meeting_ai_endpoint"]
    controls["meeting_ai_endpoint"] = replace(endpoint_control, value=result.meeting_ai_endpoint)
    return EffectiveMeetingSettings(
        result,
        policy,
        MappingProxyType(controls),
        POLICY_EXPLANATION if any(d.locked for d in policy.decisions.values()) else "",
    )
