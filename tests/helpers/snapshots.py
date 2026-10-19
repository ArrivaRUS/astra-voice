"""Выбор каталога снимков без неявной перезаписи эталонов репозитория."""

from __future__ import annotations

import os
import tempfile
import warnings
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def snapshot_dir(env_var: str, set_name: str, refs_default: str) -> Path:
    """Вернуть каталог снимков; относительные пути считаются от корня репозитория.

    Только ASTRA_VOICE_UPDATE_REFS=1 выбирает refs_default в design/refs,
    игнорируя env_var. Без флага принимается явный env_var вне design/refs;
    иначе снимки направляются в системный временный каталог с предупреждением.
    Символические ссылки разрешаются перед проверкой. Каталоги здесь не создаются.
    """
    refs = (REPO / "design/refs").resolve()
    if os.environ.get("ASTRA_VOICE_UPDATE_REFS") == "1":
        destination = (REPO / refs_default).resolve()
        if not destination.is_relative_to(refs):
            raise ValueError("refs_default должен находиться внутри design/refs")
        return destination

    explicit = os.environ.get(env_var)
    if explicit:
        destination = (REPO / explicit).resolve()
        if not destination.is_relative_to(refs):
            return destination

    destination = Path(tempfile.gettempdir()) / "astra-voice-snapshots" / set_name
    if destination.resolve().is_relative_to(refs):
        raise ValueError("Временный каталог снимков не должен указывать в design/refs")
    warnings.warn(
        f"Снимки направлены во временный каталог {destination}; эталоны design/refs "
        "не тронуты. Для их обновления задайте ASTRA_VOICE_UPDATE_REFS=1.",
        UserWarning,
        stacklevel=2,
    )
    return destination
