"""Окружение для внешних дочерних процессов (``arch/appimage.md`` §10.4, MN-3, BL-1 п.2).

Программы пользователя (``xdg-open``, браузер), ``pactl`` и новый файл AppImage
при обновлении не должны наследовать переменные нашего бандла и Qt-настройки
процесса Astra Voice. Воркер сюда не относится: он часть программы.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from astra_voice.core import paths

# Переменные runtime AppImage и принудительные настройки процесса.
REMOVED_NAMES = frozenset(
    {
        "APPIMAGE",
        "APPDIR",
        "ARGV0",
        "OWD",
        # Иначе новый файл AppImage снова распакуется в общий /tmp (BL-1 п.2).
        "APPIMAGE_EXTRACT_AND_RUN",
        # Пути модулей Qt и QML бандла.
        "QT_PLUGIN_PATH",
        "QT_QPA_PLATFORM_PLUGIN_PATH",
        "QML2_IMPORT_PATH",
        "QML_IMPORT_PATH",
        # bootstrap.py ставит их только для себя (стиль и программный рендер).
        "QT_QUICK_CONTROLS_STYLE",
        "QT_XCB_GL_INTEGRATION",
    }
)
REMOVED_PREFIXES = ("ASTRA_VOICE_", "QT_QUICK_")
TMP_SUBDIR = "tmp"


def private_tmp_dir() -> Path:
    """``<cache_dir>/tmp`` — 0700, наш uid, не ссылка."""
    return paths.ensure_private_dir(paths.cache_dir() / TMP_SUBDIR)


def _removed(name: str) -> bool:
    return name in REMOVED_NAMES or name.startswith(REMOVED_PREFIXES)


def clean_env(base: Mapping[str, str] | None = None, *, private_tmp: bool = True) -> dict[str, str]:
    """Копия окружения без переменных бандла; исходное окружение не меняется.

    ``private_tmp`` (по умолчанию, как в архитектуре) ставит ``TMPDIR`` в
    приватный каталог внутри ``cache_dir()``: запущенный из программы файл
    AppImage распакуется туда, а не в общий ``/tmp``. ``SSL_CERT_FILE`` и
    остальное окружение пользователя сохраняются.
    """
    source = os.environ if base is None else base
    env = {name: value for name, value in source.items() if not _removed(name)}
    if private_tmp:
        env["TMPDIR"] = str(private_tmp_dir())
    return env
