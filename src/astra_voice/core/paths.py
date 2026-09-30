"""Пути приложения: XDG-каталоги пользователя, корень ресурсов и способ установки.

Всё relocatable: единственные абсолютные пути в коде — стандартные места
установки ``/usr/lib/astra-voice`` и ``/usr/share/astra-voice``.

Способ установки (``arch/appimage.md`` §1–§2): пакет ``.deb``, копия AppImage в
домашней папке (``<data_dir>/app/<KEY>/``), AppImage без установки (монтирование,
распаковка) или дерево исходников.
"""

from __future__ import annotations

import enum
import os
import re
import stat
from pathlib import Path

APP_NAME = "astra-voice"
LOCK_TIMEOUT_MS = 100
INSTALL_LIB_DIR = Path("/usr/lib/astra-voice")
INSTALL_SHARE_DIR = Path("/usr/share/astra-voice")
# Лаунчер пакета .deb (scripts/astra-voice). Тесты подменяют: настоящий не запускать.
SYSTEM_EXECUTABLE = Path("/usr/bin/astra-voice")
RESOURCES_ENV = "ASTRA_VOICE_RESOURCES"
# Запасной корень runtime-каталога (тесты подменяют).
FALLBACK_TMP_DIR = Path("/tmp")
# Корень каталогов сеанса logind (/run/user/<uid>) — только для чтения без XDG_RUNTIME_DIR.
USER_RUNTIME_ROOT = Path("/run/user")

# AppImage (arch/appimage.md §1). Каталог бандла ставит AppRun.
APPIMAGE_DIR_ENV = "ASTRA_VOICE_APPIMAGE_DIR"
# Явный запуск без установки в домашнюю папку (замена ASTRA_VOICE_SELFINSTALL спайка).
PORTABLE_ENV = "ASTRA_VOICE_PORTABLE"
BUILD_MARKER = ".astra-voice-build"
INSTALLED_MARKER = ".installed-ok"
APPIMAGE_APP_SUBDIR = "app"
APPIMAGE_CURRENT = "current"
APPIMAGE_PREVIOUS = "previous"
APPIMAGE_LAUNCHER = "AppRun"
# <AppDir>/usr/lib/astra-voice/astra_voice/core/paths.py → parents[5] = <AppDir>
_BUNDLE_DEPTH = 5
# Та же грамматика в packaging/appimage/keylib.sh; совпадение проверяет T-181.
APPIMAGE_KEY_RE = re.compile(r"\d+\.\d+\.\d+(?:~[A-Za-z0-9.]+)?-[0-9a-f]{12}", re.ASCII)

# src/astra_voice/core/paths.py → src/astra_voice/core → src/astra_voice → src → корень
_REPO_ROOT = Path(__file__).resolve().parents[3]


class PathError(RuntimeError):
    """Каталог существует, но небезопасен (symlink или чужой владелец)."""


class InstallKind(enum.Enum):
    """Какая копия программы работает (``arch/appimage.md`` §2)."""

    DEB = "deb"
    APPIMAGE_INSTALLED = "appimage-installed"
    APPIMAGE_PORTABLE = "appimage-portable"
    SOURCE = "source"

    @property
    def is_appimage(self) -> bool:
        return self in (InstallKind.APPIMAGE_INSTALLED, InstallKind.APPIMAGE_PORTABLE)


def _code_file() -> Path:
    """Фактическое расположение этого модуля (тесты подменяют)."""
    return Path(__file__).resolve()


def _xdg(env_name: str, default: Path) -> Path:
    # Без strip(): значение берётся как есть, как в AppRun (`case $X in /*)`) —
    # иначе Python и sh разошлись бы в каталоге установки AppImage.
    raw = os.environ.get(env_name, "")
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


def ensure_private_dir(path: Path) -> Path:
    """Гарантирует приватный каталог по заданному пути."""
    return _ensure_private_dir(path)


def config_dir_path() -> Path:
    """Путь каталога настроек без создания (для показа в «О программе»)."""
    return _xdg("XDG_CONFIG_HOME", Path.home() / ".config")


def data_dir_path() -> Path:
    """Путь каталога данных без создания."""
    return _xdg("XDG_DATA_HOME", Path.home() / ".local" / "share")


def log_dir_path() -> Path:
    """Путь каталога журналов без создания."""
    return data_dir_path() / "logs"


def model_store_dir_path() -> Path:
    """Путь каталога моделей без создания."""
    return data_dir_path() / "models"


def config_dir() -> Path:
    """``$XDG_CONFIG_HOME/astra-voice`` (по умолчанию ``~/.config/astra-voice``)."""
    return _ensure_private_dir(_xdg("XDG_CONFIG_HOME", Path.home() / ".config"))


def data_dir() -> Path:
    """``$XDG_DATA_HOME/astra-voice`` (по умолчанию ``~/.local/share/astra-voice``)."""
    return _ensure_private_dir(_xdg("XDG_DATA_HOME", Path.home() / ".local" / "share"))


def measurements_path() -> Path:
    """Файл замеров моделей в приватном каталоге данных."""
    return data_dir() / "measurements.json"


def model_store_dir() -> Path:
    """Каталог моделей; этот же путь использует ModelStore по умолчанию."""
    return data_dir() / "models"


def cache_dir() -> Path:
    """``$XDG_CACHE_HOME/astra-voice`` (по умолчанию ``~/.cache/astra-voice``)."""
    return _ensure_private_dir(_xdg("XDG_CACHE_HOME", Path.home() / ".cache"))


def state_dir() -> Path:
    """``$XDG_STATE_HOME/astra-voice`` (по умолчанию ``~/.local/state/astra-voice``)."""
    return _ensure_private_dir(_xdg("XDG_STATE_HOME", Path.home() / ".local" / "state"))


def runtime_dir() -> Path:
    """``$XDG_RUNTIME_DIR/astra-voice``; запасной вариант — ``/tmp/astra-voice-<uid>``.

    Каталог всегда 0700, принадлежит текущему пользователю и не является
    символической ссылкой (иначе — :class:`PathError`).
    """
    *preferred, fallback = _runtime_dir_candidates()
    for candidate in preferred:
        try:
            return _ensure_private_dir(candidate)
        except (PathError, OSError):
            pass
    return _ensure_private_dir(fallback)


def _runtime_dir_candidates(*, reading: bool = False) -> tuple[Path, ...]:
    """Кандидаты ``runtime_dir()`` по порядку; последний — запасной в ``/tmp``.

    ``reading`` — для чтения чужих отметок: без ``XDG_RUNTIME_DIR`` (терминал через
    ``runuser``, cron) программа сеанса всё равно могла писать в ``/run/user/<uid>``.
    """
    raw = os.environ.get("XDG_RUNTIME_DIR", "").strip()
    fallback = FALLBACK_TMP_DIR / f"{APP_NAME}-{os.getuid()}"
    if raw.startswith("/"):
        return (Path(raw) / APP_NAME, fallback)
    if reading:
        return (USER_RUNTIME_ROOT / str(os.getuid()) / APP_NAME, fallback)
    return (fallback,)


def existing_runtime_dirs() -> tuple[Path, ...]:
    """Все кандидаты для чтения, уже существующие как наш каталог (не symlink), по порядку."""
    found: list[Path] = []
    for candidate in _runtime_dir_candidates(reading=True):
        try:
            info = candidate.lstat()
        except OSError:
            continue
        if stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid():
            found.append(candidate)
    return tuple(found)


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
    """Корень ресурсов: переменная окружения → бандл AppImage → ``/usr/share`` → репозиторий."""
    raw = os.environ.get(RESOURCES_ENV, "").strip()
    if raw:
        return Path(raw)
    here = _code_file()
    if here.is_relative_to(INSTALL_LIB_DIR):
        return INSTALL_SHARE_DIR
    bundle = bundle_root()
    if bundle is not None:
        return bundle / "usr" / "share" / APP_NAME
    return _REPO_ROOT


def qml_dir() -> Path:
    return resource_root() / "qml"


def data_dir_static() -> Path:
    """Каталог поставляемых данных (иконки, звуки, каталог моделей)."""
    return resource_root() / "data"


def icon_theme_dir() -> Path:
    """Каталог значков установленной темы рядом с корнем ресурсов."""
    return resource_root().parent / "icons" / "hicolor"


def _is_bundle_root(candidate: Path, here: Path) -> bool:
    return here.is_relative_to(candidate) and (candidate / BUILD_MARKER).is_file()


def bundle_root() -> Path | None:
    """Корень AppDir, из которого работает этот код, или ``None`` вне AppImage.

    Сначала ``ASTRA_VOICE_APPIMAGE_DIR`` (ставит ``AppRun``), затем раскладка
    бандла от расположения модуля. Каталог признаётся бандлом, только если код
    действительно лежит внутри него и в корне есть маркер сборки.
    """
    here = _code_file()
    raw = os.environ.get(APPIMAGE_DIR_ENV, "").strip()
    if raw.startswith("/"):
        candidate = Path(raw).resolve()
        if _is_bundle_root(candidate, here):
            return candidate
    if len(here.parents) > _BUNDLE_DEPTH:
        candidate = here.parents[_BUNDLE_DEPTH]
        if _is_bundle_root(candidate, here):
            return candidate
    return None


def is_appimage_key(name: str) -> bool:
    """Имя каталога копии ``<версия>-<build_id>`` в ``app/`` (маска чистки и детекта)."""
    return APPIMAGE_KEY_RE.fullmatch(name) is not None


def appimage_app_dir() -> Path:
    """``<data_dir>/app`` — только копии программы; каталог не создаётся."""
    return _xdg("XDG_DATA_HOME", Path.home() / ".local" / "share") / APPIMAGE_APP_SUBDIR


def appimage_current_link() -> Path:
    """Симлинк ``app/current`` — стабильная точка входа меню и автозапуска."""
    return appimage_app_dir() / APPIMAGE_CURRENT


def appimage_previous_link() -> Path:
    """Симлинк ``app/previous`` — предыдущая версия (откат)."""
    return appimage_app_dir() / APPIMAGE_PREVIOUS


def appimage_current_apprun() -> Path:
    """``<data_dir>/app/current/AppRun`` — единственный путь для меню и автозапуска."""
    return appimage_current_link() / APPIMAGE_LAUNCHER


def check_appimage_launcher(path: Path) -> Path:
    """Проверка пути, который попадёт в ``Exec`` меню и автозапуска.

    Возвращает исходный путь через ``current``, проверив установленную копию.
    """
    error = "Не удалось найти установленную программу. Переустановите Astra Voice."
    app = appimage_app_dir().absolute()
    launcher = path.absolute()
    current = app / APPIMAGE_CURRENT
    if (
        ".." in path.parts
        or not launcher.is_relative_to(app)
        or launcher != current / APPIMAGE_LAUNCHER
    ):
        raise PathError(error)
    try:
        # readlink также отклоняет обычный каталог вместо ссылки current.
        target = Path(os.readlink(current))
        resolved_app = app.resolve(strict=True)
        resolved_target = (app / target).resolve(strict=True)
        resolved_launcher = launcher.resolve(strict=True)
        valid = (
            ".." not in target.parts
            and is_appimage_key(target.name)
            and is_appimage_key(resolved_target.name)
            and resolved_target.is_relative_to(resolved_app)
            and resolved_target.parent == resolved_app
            and resolved_target.is_dir()
            and resolved_launcher.is_relative_to(resolved_app)
            and resolved_launcher.is_file()
        )
    except (OSError, RuntimeError, ValueError):
        raise PathError(error) from None
    if not valid:
        raise PathError(error)
    return path


def install_kind() -> InstallKind:
    """Способ установки работающей копии (``arch/appimage.md`` §2, T-163).

    ``DEB`` — код лежит в ``/usr/lib/astra-voice``; ``APPIMAGE_INSTALLED`` — бандл
    лежит прямо в ``<data_dir>/app/<KEY>`` (имя по маске, не ``.tmp-*``) и помечен
    ``.installed-ok``;
    ``APPIMAGE_PORTABLE`` — любой другой бандл (монтирование FUSE, распаковка);
    ``SOURCE`` — дерево исходников.
    """
    if _code_file().is_relative_to(INSTALL_LIB_DIR):
        return InstallKind.DEB
    bundle = bundle_root()
    if bundle is None:
        return InstallKind.SOURCE
    app_dir = appimage_app_dir()
    try:
        app_dir = app_dir.resolve()
    except OSError:
        return InstallKind.APPIMAGE_PORTABLE
    if (
        bundle.parent == app_dir
        and is_appimage_key(bundle.name)
        and (bundle / INSTALLED_MARKER).is_file()
    ):
        return InstallKind.APPIMAGE_INSTALLED
    return InstallKind.APPIMAGE_PORTABLE
