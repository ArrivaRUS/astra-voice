"""Пути приложения: XDG-каталоги пользователя и корень ресурсов.

Всё relocatable: единственные абсолютные пути в коде — стандартные места
установки ``/usr/lib/astra-voice`` и ``/usr/share/astra-voice``.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

APP_NAME = "astra-voice"
INSTALL_LIB_DIR = Path("/usr/lib/astra-voice")
INSTALL_SHARE_DIR = Path("/usr/share/astra-voice")
RESOURCES_ENV = "ASTRA_VOICE_RESOURCES"
# Запасной корень runtime-каталога (тесты подменяют).
FALLBACK_TMP_DIR = Path("/tmp")

# src/astra_voice/core/paths.py → src/astra_voice/core → src/astra_voice → src → корень
_REPO_ROOT = Path(__file__).resolve().parents[3]


class PathError(RuntimeError):
    """Каталог существует, но небезопасен (symlink или чужой владелец)."""


def _xdg(env_name: str, default: Path) -> Path:
    raw = os.environ.get(env_name, "").strip()
    base = Path(raw) if raw.startswith("/") else default
    return base / APP_NAME


def _ensure_private_dir(path: Path) -> Path:
    """Создаёт каталог 0700 и проверяет, что он не symlink и принадлежит нам."""
    if path.is_symlink():
        raise PathError(f"{path} — символическая ссылка")
    path.mkdir(parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode):
        raise PathError(f"{path} — не каталог")
    if info.st_uid != os.getuid():
        raise PathError(f"{path} принадлежит uid {info.st_uid}, а не {os.getuid()}")
    if stat.S_IMODE(info.st_mode) != 0o700:
        path.chmod(0o700)
    return path


def config_dir() -> Path:
    """``$XDG_CONFIG_HOME/astra-voice`` (по умолчанию ``~/.config/astra-voice``)."""
    return _ensure_private_dir(_xdg("XDG_CONFIG_HOME", Path.home() / ".config"))


def data_dir() -> Path:
    """``$XDG_DATA_HOME/astra-voice`` (по умолчанию ``~/.local/share/astra-voice``)."""
    return _ensure_private_dir(_xdg("XDG_DATA_HOME", Path.home() / ".local" / "share"))


def cache_dir() -> Path:
    """``$XDG_CACHE_HOME/astra-voice`` (по умолчанию ``~/.cache/astra-voice``)."""
    return _ensure_private_dir(_xdg("XDG_CACHE_HOME", Path.home() / ".cache"))


def runtime_dir() -> Path:
    """``$XDG_RUNTIME_DIR/astra-voice``; запасной вариант — ``/tmp/astra-voice-<uid>``.

    Каталог всегда 0700, принадлежит текущему пользователю и не является
    символической ссылкой (иначе — :class:`PathError`).
    """
    raw = os.environ.get("XDG_RUNTIME_DIR", "").strip()
    if raw.startswith("/"):
        try:
            return _ensure_private_dir(Path(raw) / APP_NAME)
        except (PathError, OSError):
            pass
    fallback = FALLBACK_TMP_DIR / f"{APP_NAME}-{os.getuid()}"
    return _ensure_private_dir(fallback)


def log_dir() -> Path:
    """Каталог журналов: ``<data_dir>/logs``."""
    return _ensure_private_dir(data_dir() / "logs")


def settings_path() -> Path:
    return config_dir() / "settings.json"


def lock_path() -> Path:
    return runtime_dir() / "lock"


def ipc_socket_path() -> Path:
    return runtime_dir() / "ipc"


def resource_root() -> Path:
    """Корень ресурсов: переменная окружения → ``/usr/share`` → корень репозитория."""
    raw = os.environ.get(RESOURCES_ENV, "").strip()
    if raw:
        return Path(raw)
    here = Path(__file__).resolve()
    if here.is_relative_to(INSTALL_LIB_DIR):
        return INSTALL_SHARE_DIR
    return _REPO_ROOT


def qml_dir() -> Path:
    return resource_root() / "qml"


def data_dir_static() -> Path:
    """Каталог поставляемых данных (иконки, звуки, каталог моделей)."""
    return resource_root() / "data"
