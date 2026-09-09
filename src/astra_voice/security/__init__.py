"""Проверка подлинности: подписи GPG и контрольные суммы."""

from astra_voice.security.verify import (
    PINNED_FINGERPRINTS,
    REVOKED_FINGERPRINTS,
    Verifier,
    VerifyResult,
)

__all__ = [
    "PINNED_FINGERPRINTS",
    "REVOKED_FINGERPRINTS",
    "VerifyResult",
    "Verifier",
]
