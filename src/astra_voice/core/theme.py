"""Источник темы рабочего стола: светлая/тёмная и акцентный цвет.

Читаем **файл конфигурации сессии**, а не палитру Qt (`design/spec.md` §12.1,
урок Astra Cowork: в процессе оверлея Qt-палитра врёт). KDE — `~/.config/kdeglobals`,
Fly — `~/.fly/paletterc` и `~/.config/fly-admin-themerc`.

Парсер работает без запущенного `QCoreApplication`: тему нужно знать **до**
`engine.load()`. Слежение за файлом (`start()`) требует Qt-цикла событий.
"""

from __future__ import annotations

import logging
import os
import re
import stat
from abc import ABC, abstractmethod
from collections.abc import Callable
from pathlib import Path
from typing import Any

from astra_voice.core import paths

__all__ = [
    "ThemeSource",
    "KdeThemeSource",
    "FlyThemeSource",
    "kde_config_path",
    "fly_palette_path",
    "fly_admin_themerc_path",
    "read_ini",
    "parse_color",
    "luma",
]

_COMMENT = ("#", ";")

# Имена файлов конфигурации сессии; каталоги считаются на каждый вызов, а не на импорте:
# XDG_CONFIG_HOME и HOME могут отличаться у сервиса, из-под которого нас запустят.
KDE_CONFIG_NAME = "kdeglobals"
FLY_PALETTE_NAME = "paletterc"


def kde_config_path() -> Path:
    """`$XDG_CONFIG_HOME/kdeglobals`, по умолчанию `~/.config/kdeglobals`.

    Каталог считаем тем же разбором XDG, что и `core.paths`, но берём РОДИТЕЛЯ нашего
    `config_dir()`: `kdeglobals` — чужой файл пользователя, каталог под него создавать
    и переставлять ему права нельзя (`config_dir()` делает и то, и другое).
    TODO(M1-A): когда в `core.paths` появится публичная `config_home()`, перейти на неё —
    сейчас переиспользуем приватный разбор, чтобы правило XDG было одно на проект.
    """
    return paths._xdg("XDG_CONFIG_HOME", Path.home() / ".config").parent / KDE_CONFIG_NAME


def fly_palette_path() -> Path:
    """`~/.fly/paletterc`. XDG к Fly не применяется — путь задан жёстко самой средой."""
    return Path.home() / ".fly" / FLY_PALETTE_NAME


def fly_admin_themerc_path() -> Path:
    """Путь Fly, не зависящий от XDG_CONFIG_HOME."""
    return Path.home() / ".config" / "fly-admin-themerc"


def read_ini(path: Path) -> dict[str, dict[str, str]]:
    """Мини-парсер INI в стиле KConfig: без интерполяции, дубль ключа — последний выигрывает."""
    out: dict[str, dict[str, str]] = {}
    try:
        raw_text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    return _parse_ini(raw_text)


def _parse_ini(raw_text: str) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    section = ""
    for raw in raw_text.splitlines():
        line = raw.strip()
        if not line or line.startswith(_COMMENT):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            out.setdefault(section, {})
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        out.setdefault(section, {})[key.strip()] = value.strip()
    return out


def parse_color(value: str) -> tuple[int, int, int] | None:
    """`14,23,41` (KConfig) или `#0E1729` → (r, g, b). Мусор → None."""
    text = (value or "").strip()
    if not text:
        return None
    if text.startswith("#") and len(text) >= 7:
        try:
            return (int(text[1:3], 16), int(text[3:5], 16), int(text[5:7], 16))
        except ValueError:
            return None
    parts = [p for p in re.split(r"[,\s]+", text) if p]
    if len(parts) >= 3:
        try:
            rgb = tuple(max(0, min(255, int(float(p)))) for p in parts[:3])
        except (ValueError, OverflowError):
            return None
        return (rgb[0], rgb[1], rgb[2])
    return None


def luma(rgb: tuple[int, int, int]) -> float:
    """Относительная яркость 0…255 по BT.601 — как KColorUtils «grayscale»."""
    return 0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2]


def to_hex(rgb: tuple[int, int, int]) -> str:
    return "#{:02X}{:02X}{:02X}".format(*rgb)


def scheme_is_dark(name: str) -> bool | None:
    """По имени цветовой схемы: `BreezeDark` → True, `Breeze` → None (имя не решает)."""
    low = (name or "").lower()
    if "dark" in low:
        return True
    if "light" in low:
        return False
    return None


class ThemeSource(ABC):
    """Тема сессии: тёмная ли она, акцент и базовые системные цвета.

    `subscribe()` регистрирует callback без аргументов — он зовётся, когда значения
    действительно изменились (перезапись файла без смены темы событие не рождает).
    """

    def __init__(self) -> None:
        self._listeners: list[Callable[[], None]] = []

    # ── контракт ────────────────────────────────────────────────────────────
    @property
    @abstractmethod
    def dark(self) -> bool:
        """True — тёмная тема сессии."""

    @property
    @abstractmethod
    def accent(self) -> str | None:
        """Акцентный цвет сессии `#RRGGBB` или None, если система его не задаёт."""

    @property
    def window_bg(self) -> str | None:
        """Системный фон окна `#RRGGBB` — для диагностики и «О программе»."""
        return None

    @property
    def text(self) -> str | None:
        """Системный цвет текста `#RRGGBB`."""
        return None

    # ── наблюдение ──────────────────────────────────────────────────────────
    def subscribe(self, callback: Callable[[], None]) -> None:
        self._listeners.append(callback)

    def unsubscribe(self, callback: Callable[[], None]) -> None:
        """Отсоединить владельца, который больше не использует источник."""
        if callback in self._listeners:
            self._listeners.remove(callback)

    def _notify(self) -> None:
        for callback in list(self._listeners):
            callback()

    def start(self) -> None:
        """Начать следить за источником (нужен цикл событий Qt)."""
        return None

    def stop(self) -> None:
        """Прекратить слежение и освободить наблюдатель."""
        return None

    def __repr__(self) -> str:
        return f"{type(self).__name__}(dark={self.dark}, accent={self.accent})"


class _FileThemeSource(ThemeSource):
    """Общая часть: читает INI-файл и следит за ним через QFileSystemWatcher.

    `kwriteconfig` и системные настройки переписывают файл **атомарно** (tmp + rename),
    поэтому наблюдатель теряет путь: следим ещё и за каталогом и восстанавливаем
    подписку на каждом событии.
    """

    def __init__(self, path: Path) -> None:
        super().__init__()
        self._path = Path(path)
        self._watcher: Any | None = None
        self._state: tuple[bool, str | None, str | None, str | None] = self._read()

    @property
    def path(self) -> Path:
        return self._path

    def _read(self) -> tuple[bool, str | None, str | None, str | None]:
        raise NotImplementedError

    @property
    def dark(self) -> bool:
        return self._state[0]

    @property
    def accent(self) -> str | None:
        return self._state[1]

    @property
    def window_bg(self) -> str | None:
        return self._state[2]

    @property
    def text(self) -> str | None:
        return self._state[3]

    def reload(self) -> bool:
        """Перечитать файл. True — значения изменились (подписчики уведомлены)."""
        state = self._read()
        # Даже при неизменных цветах источник мог переключиться на другой файл.
        self._rearm()
        if state == self._state:
            return False
        self._state = state
        self._notify()
        return True

    # ── QFileSystemWatcher ──────────────────────────────────────────────────
    def start(self) -> None:
        if self._watcher is not None:
            return
        from PyQt5.QtCore import QFileSystemWatcher

        self._watcher = QFileSystemWatcher()
        self._watcher.fileChanged.connect(self._on_changed)
        self._watcher.directoryChanged.connect(self._on_changed)
        self._rearm()
        self.reload()

    def stop(self) -> None:
        if self._watcher is None:
            return
        self._watcher.fileChanged.disconnect(self._on_changed)
        self._watcher.directoryChanged.disconnect(self._on_changed)
        self._watcher.deleteLater()
        self._watcher = None

    def _watch_paths(self) -> set[Path]:
        return {self._path}

    def _watch_directories(self) -> set[Path]:
        return set()

    def _rearm(self) -> None:
        """Вернуть путь под наблюдение: после атомарной перезаписи он выпадает из списка."""
        watcher = self._watcher
        if watcher is None:
            return
        paths_to_watch = self._watch_paths()
        wanted = {str(path) for path in paths_to_watch if path.is_file()}
        for directory in self._watch_directories() | {p.parent for p in paths_to_watch}:
            # Наблюдаем ближайшего существующего предка, если каталога ещё нет.
            while not directory.is_dir() and directory != directory.parent:
                directory = directory.parent
            if directory.is_dir():
                wanted.add(str(directory))
        current = set(watcher.files()) | set(watcher.directories())
        if current - wanted:
            watcher.removePaths(sorted(current - wanted))
        if wanted - current:
            watcher.addPaths(sorted(wanted - current))

    def _on_changed(self, _path: str) -> None:
        if self._watcher is not None:
            self.reload()


class KdeThemeSource(_FileThemeSource):
    """KDE / Plasma: `$XDG_CONFIG_HOME/kdeglobals` (по умолчанию `~/.config/kdeglobals`).

    Тёмность — сперва по имени схемы (`[General] ColorScheme`), затем по яркости фона
    окна (`[Colors:Window] BackgroundNormal`); нет данных — светлая.
    Акцент — `[General] AccentColor`, запасной — фон выделения `[Colors:Selection]`.
    """

    def __init__(self, path: Path | str | None = None) -> None:
        super().__init__(Path(path) if path is not None else kde_config_path())

    def _read(self) -> tuple[bool, str | None, str | None, str | None]:
        ini = read_ini(self._path)
        general = ini.get("General", {})
        window = ini.get("Colors:Window", {})
        selection = ini.get("Colors:Selection", {})

        bg = parse_color(window.get("BackgroundNormal", "") or window.get("BackgroundColor", ""))
        dark = scheme_is_dark(general.get("ColorScheme", ""))
        if dark is None:
            dark = luma(bg) < 128 if bg is not None else False

        accent_rgb = parse_color(general.get("AccentColor", ""))
        if accent_rgb is None:
            accent_rgb = parse_color(selection.get("BackgroundNormal", ""))
        fg = parse_color(window.get("ForegroundNormal", ""))
        return (
            dark,
            to_hex(accent_rgb) if accent_rgb else None,
            to_hex(bg) if bg else None,
            to_hex(fg) if fg else None,
        )


_FLY_MAX_BYTES = 256 * 1024


def _read_fly_ini(path: Path, roots: dict[Path, int] | None = None) -> dict[str, dict[str, str]]:
    """Ограниченное чтение обычного файла, без выхода через подмену симлинка.

    Для схем границы уже закреплены открытыми дескрипторами каталогов. Раскрываем
    внутренние ссылки и открываем компоненты относительно такого дескриптора
    через O_NOFOLLOW: подмена каталога между проверкой и open отвергается.
    O_NONBLOCK и fstat не дают зависнуть на FIFO или читать устройства.
    """
    fd: int | None = None
    try:
        resolved = path.resolve()
        if roots is not None:
            root = next((root for root in roots if resolved.is_relative_to(root)), None)
            if root is None:
                return {}
            fd = os.dup(roots[root])
            parts = resolved.relative_to(root).parts
        else:
            fd = os.open(resolved.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
            parts = resolved.parts[1:]
        if not parts:
            return {}
        for part in parts[:-1]:
            child = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd
            )
            os.close(fd)
            fd = child
        child = os.open(
            parts[-1],
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
            dir_fd=fd,
        )
        os.close(fd)
        fd = child
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > _FLY_MAX_BYTES:
            return {}
        # +1 обнаруживает рост после fstat; суммарное чтение всегда ограничено.
        chunks = bytearray()
        while len(chunks) <= _FLY_MAX_BYTES:
            chunk = os.read(fd, _FLY_MAX_BYTES + 1 - len(chunks))
            if not chunk:
                break
            chunks.extend(chunk)
        if len(chunks) > _FLY_MAX_BYTES or b"\x00" in chunks:
            return {}
        return _parse_ini(chunks.decode("utf-8"))
    except (OSError, RuntimeError, ValueError):
        return {}
    finally:
        if fd is not None:
            os.close(fd)


class FlyThemeSource(_FileThemeSource):
    """Fly: схема из paletterc, запасной фон, затем схема по имени из настроек.

    Имя схемы не определяет светлоту. Для схем обязателен фон Colors:Window;
    файлы схем разрешены только внутри системного и пользовательского каталогов.
    """

    def __init__(
        self,
        path: Path | str | None = None,
        *,
        admin_themerc: Path | str | None = None,
        schemes_dir: Path | str | None = None,
        user_schemes_dir: Path | str | None = None,
    ) -> None:
        self._admin = Path(admin_themerc) if admin_themerc is not None else fly_admin_themerc_path()
        self._schemes = (
            Path(schemes_dir) if schemes_dir is not None else Path("/usr/share/color-schemes")
        )
        self._user_schemes = (
            Path(user_schemes_dir)
            if user_schemes_dir is not None
            else Path.home() / ".local/share/color-schemes"
        )
        self._scheme_paths: set[Path] = set()
        self._warned = False
        super().__init__(Path(path) if path is not None else fly_palette_path())

    def _warn(self) -> None:
        if not self._warned:
            logging.getLogger("astra_voice").warning(
                "Не удалось прочитать тему Fly полностью; используется запасная тема"
            )
            self._warned = True

    def _scheme(self, path: Path, roots: dict[Path, int]) -> dict[str, dict[str, str]]:
        try:
            resolved = path.resolve()
            if path.suffix != ".colors" or not any(resolved.is_relative_to(root) for root in roots):
                return {}
            # И имя ссылки, и цель: переадресация ссылки и правка цели значимы.
            self._scheme_paths.update((path, resolved))
            return _read_fly_ini(path, roots)
        except (OSError, RuntimeError, ValueError):
            return {}

    def _read(self) -> tuple[bool, str | None, str | None, str | None]:
        roots: dict[Path, int] = {}
        system: Path | None = None
        try:
            for path in (self._schemes, self._user_schemes):
                try:
                    # Канонизируем только предков (HOME может быть ссылкой).
                    # Сам root не resolve(): его ссылка не расширяет доверие.
                    root = path.absolute()
                    root = root.parent.resolve() / root.name
                    if root.name == "..":
                        root = root.parent.parent
                    if path == self._schemes:
                        system = root
                    if root in roots:
                        continue
                    roots[root] = os.open(
                        root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
                    )
                except (OSError, RuntimeError, ValueError):
                    self._warn()
            return self._read_theme(roots, system)
        finally:
            for fd in roots.values():
                os.close(fd)

    def _read_theme(
        self, roots: dict[Path, int], system: Path | None
    ) -> tuple[bool, str | None, str | None, str | None]:
        self._scheme_paths.clear()
        variables = _read_fly_ini(self._path).get("Variables", {})
        accent = parse_color(variables.get("PrimaryColor", ""))
        ini: dict[str, dict[str, str]] = {}
        scheme = variables.get("ColorScheme", "")
        if scheme:
            ini = self._scheme(Path(scheme), roots)
            if not ini:
                self._warn()
        bg = parse_color(ini.get("Colors:Window", {}).get("BackgroundNormal", ""))
        if bg is None:
            ini = {}
            bg = parse_color(variables.get("BackgroundColor", ""))
        if bg is None:
            name = _read_fly_ini(self._admin).get("UiSettings", {}).get("ColorScheme", "")
            name = name.strip().casefold()
            if name and system is not None and system in roots:
                try:
                    candidates = sorted(
                        system / entry
                        for entry in os.listdir(roots[system])
                        if entry.endswith(".colors")
                    )
                except OSError:
                    candidates = []
                for candidate in candidates:
                    candidate_ini = self._scheme(candidate, {system: roots[system]})
                    general = candidate_ini.get("General", {})
                    if name not in (
                        general.get("Name", "").strip().casefold(),
                        general.get("Name[ru]", "").strip().casefold(),
                    ):
                        continue
                    bg = parse_color(
                        candidate_ini.get("Colors:Window", {}).get("BackgroundNormal", "")
                    )
                    if bg is not None:
                        ini = candidate_ini
                        break
        if bg is None:
            self._warn()
            return (False, None, None, None)
        if accent is None:
            accent = parse_color(ini.get("Colors:Selection", {}).get("BackgroundNormal", ""))
        fg = parse_color(ini.get("Colors:Window", {}).get("ForegroundNormal", ""))
        return (
            luma(bg) < 128,
            to_hex(accent) if accent else None,
            to_hex(bg),
            to_hex(fg) if fg else None,
        )

    def _watch_paths(self) -> set[Path]:
        return {self._path, self._admin} | self._scheme_paths

    def _watch_directories(self) -> set[Path]:
        return {self._schemes, self._user_schemes}
