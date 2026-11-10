"""Small catalog integration seam; no environment or adjacent-directory discovery."""

from __future__ import annotations

from collections.abc import Callable

from astra_voice.core.settings import Settings
from astra_voice.meetings.policy import MeetingPolicy
from astra_voice.meetings.settings_ext import effective_settings


def installed_diar_models() -> tuple[str, str] | None:
    """Trusted installed (segmentation_dir, embedder_dir); catalog step replaces this body."""
    return None


def enabled_diar_models(
    settings: Settings,
    policy: MeetingPolicy,
    provider: Callable[[], tuple[str, str] | None] = installed_diar_models,
) -> tuple[str, str] | None:
    if not effective_settings(settings, policy).settings.meeting_diarization:
        return None
    return provider()
