"""Внешние папки и ссылки без окружения программы (arch/appimage.md §10.4, MN-3)."""

from __future__ import annotations

import logging
import subprocess
from collections.abc import Callable

from astra_voice.core import childenv

log = logging.getLogger(__name__)


def open_external(url: str, *, popen: Callable[..., object] = subprocess.Popen) -> bool:
    """Открывает разрешённый URL без ожидания; адрес в журнал не попадает."""
    if url.startswith("-") or not url.lower().startswith(("file://", "https://", "http://")):
        log.warning("Не удалось открыть ссылку: недопустимый адрес")
        return False
    try:
        popen(
            ["xdg-open", url],
            shell=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            env=childenv.clean_env(),
        )
    except OSError:
        log.warning("Не удалось открыть папку или ссылку")
        return False
    return True
