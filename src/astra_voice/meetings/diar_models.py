"""Small catalog integration seam; no environment or adjacent-directory discovery."""

from __future__ import annotations

from collections.abc import Callable

from astra_voice.core.settings import Settings
from astra_voice.meetings.policy import MeetingPolicy
from astra_voice.meetings.settings_ext import effective_settings
from astra_voice.models import diarization
from astra_voice.models.catalog import DIARIZATION_MODEL_IDS, Catalog, CatalogError
from astra_voice.models.catalog_effective import check_revoked
from astra_voice.models.store import ModelStore, StoreError


def installed_diar_models(
    store: ModelStore | None = None,
    *,
    catalog_provider: Callable[[], Catalog] | None = None,
) -> dict[str, str] | None:
    """Directories and expected hashes from the signed catalog; fail closed."""
    return nonblocking_diar_models(
        lambda: (
            diarization.trusted_diarization_models(store)
            if catalog_provider is None
            else diarization.trusted_diarization_models(store, catalog_provider=catalog_provider)
        )
    )


def nonblocking_diar_models(provider: Callable[[], dict[str, str] | None]) -> dict[str, str] | None:
    """Do not queue a metadata/control write behind the pair's mutation lock.

    The service uses this reentrant lock for the entire install/remove operation.
    Hold it across the trusted read so another writer cannot win between the
    nonblocking check and the provider's own lock acquisition.
    """
    if not diarization._PAIR_LOCK.acquire(blocking=False):
        return None
    try:
        return provider()
    finally:
        diarization._PAIR_LOCK.release()


def diar_models_revoked(catalog_provider: Callable[[], Catalog]) -> bool:
    """Only a known revocation hides retry; unavailable catalogs stay fail-closed."""
    try:
        catalog = catalog_provider()
        if not catalog.verified:
            return False
        for model_id in DIARIZATION_MODEL_IDS:
            entry = catalog.entry(model_id)
            if entry is not None:
                check_revoked(
                    model_id, entry.revision, catalog, include_snapshot=True, read_only=True
                )
    except StoreError as error:
        return error.code == "revoked"
    except (CatalogError, OSError, ValueError, RuntimeError):
        return False
    return False


def enabled_diar_models(
    settings: Settings,
    policy: MeetingPolicy,
    provider: Callable[[], dict[str, str] | None] = installed_diar_models,
) -> dict[str, str] | None:
    if not effective_settings(settings, policy).settings.meeting_diarization:
        return None
    return provider()
