"""Окружение для внешних дочерних процессов (``arch/appimage.md`` §10.4, MN-3, BL-1 п.2).

Программы пользователя (``xdg-open``, браузер), ``pactl`` и новый файл AppImage
при обновлении не должны наследовать переменные нашего бандла и настройки,
которые ``AppRun`` и ``bootstrap.py`` поменяли только для процесса Astra Voice.
Воркер сюда не относится: он часть программы.

Как это устроено. Перед тем как снять или перезаписать переменную из
:data:`RESTORABLE`, ``AppRun`` и ``bootstrap`` запоминают исходное значение в
``ASTRA_VOICE_ORIG_<ИМЯ>``, а имена, которых не было, — в
``ASTRA_VOICE_ORIG_UNSET`` (через пробел). Запоминается только первое значение:
вложенный ``AppRun`` и ``bootstrap`` после него уже ничего не перезаписывают.
:func:`clean_env` возвращает детям исходные значения; переменные, которые мы не
трогали, остаются как есть.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping, MutableMapping
from pathlib import Path

from astra_voice.core import paths

ORIG_PREFIX = "ASTRA_VOICE_ORIG_"
ORIG_UNSET = "ASTRA_VOICE_ORIG_UNSET"

# Что снимает или ставит AppRun (тот же список — в packaging/appimage/AppRun).
APPRUN_MANAGED = (
    "PYTHONHOME",
    "PYTHONPATH",
    "PYTHONSTARTUP",
    "PYTHONUSERBASE",
    "QT_PLUGIN_PATH",
    "QML2_IMPORT_PATH",
    "QML_IMPORT_PATH",
    "QT_QPA_PLATFORMTHEME",
    "LD_PRELOAD",
    "LD_LIBRARY_PATH",
    "LD_AUDIT",
    "SSL_CERT_FILE",
)
# Что AppRun меняет только по условию (кириллица в пути при однобайтовой локали), не снимая.
APPRUN_CONDITIONAL = ("LC_ALL", "LC_CTYPE")
# Что ставит bootstrap.py (стиль и рендер Qt Quick, запрет autospawn PulseAudio).
BOOTSTRAP_MANAGED = (
    "QT_QUICK_CONTROLS_STYLE",
    "QT_QUICK_BACKEND",
    "QT_XCB_GL_INTEGRATION",
    "PULSE_CLIENTCONFIG",
)
RESTORABLE = frozenset(APPRUN_MANAGED + APPRUN_CONDITIONAL + BOOTSTRAP_MANAGED)

# Переменные runtime AppImage: снимаются всегда (APPIMAGE* — по префиксу, включая
# APPIMAGE_EXTRACT_AND_RUN, иначе новый файл снова распакуется в общий /tmp, BL-1 п.2).
REMOVED_NAMES = frozenset({"APPDIR", "ARGV0", "OWD"})
REMOVED_PREFIXES = ("ASTRA_VOICE_", "APPIMAGE")
TMP_SUBDIR = "tmp"
PULSE_CLIENTCONFIG = "PULSE_CLIENTCONFIG"


def private_tmp_dir() -> Path:
    """``<cache_dir>/tmp`` — 0700, наш uid, не ссылка."""
    return paths.ensure_private_dir(paths.cache_dir() / TMP_SUBDIR)


def save_originals(names: Iterable[str], env: MutableMapping[str, str] | None = None) -> None:
    """Запоминает исходные значения ``names`` до их изменения (только первый раз)."""
    target = os.environ if env is None else env
    unset = target.get(ORIG_UNSET, "").split()
    for name in names:
        if name not in RESTORABLE:
            raise ValueError(f"переменная не из списка восстанавливаемых: {name}")
        if ORIG_PREFIX + name in target or name in unset:
            continue
        if name in target:
            target[ORIG_PREFIX + name] = target[name]
        else:
            unset.append(name)
    if unset:
        target[ORIG_UNSET] = " ".join(unset)


def _removed(name: str) -> bool:
    return name in REMOVED_NAMES or name.startswith(REMOVED_PREFIXES)


def clean_env(
    base: Mapping[str, str] | None = None,
    *,
    private_tmp: bool = False,
    keep_pulse_config: bool = False,
) -> dict[str, str]:
    """Копия окружения для внешней программы; исходное окружение не меняется.

    Снимаются ``APPIMAGE*``, ``APPDIR``, ``ARGV0``, ``OWD`` и все ``ASTRA_VOICE_*``;
    переменные из :data:`RESTORABLE` получают исходные значения (или снимаются,
    если их не было).

    ``private_tmp`` — ``TMPDIR`` в приватном каталоге внутри ``cache_dir()``: только
    для запуска нового файла AppImage (распакуется туда, а не в общий ``/tmp``);
    для ``xdg-open`` и браузера — нет, их временные файлы не наше дело.
    ``keep_pulse_config`` — оставить наш ``PULSE_CLIENTCONFIG`` (запрет autospawn)
    для ``pactl``; программам пользователя возвращается исходный.
    """
    source = os.environ if base is None else base
    env = {name: value for name, value in source.items() if not _removed(name)}
    for name in source.get(ORIG_UNSET, "").split():
        if name in RESTORABLE:
            env.pop(name, None)
    for key, value in source.items():
        name = key[len(ORIG_PREFIX) :]
        if key.startswith(ORIG_PREFIX) and name in RESTORABLE:
            env[name] = value
    if keep_pulse_config and PULSE_CLIENTCONFIG in source:
        env[PULSE_CLIENTCONFIG] = source[PULSE_CLIENTCONFIG]
    if private_tmp:
        env["TMPDIR"] = str(private_tmp_dir())
    return env
