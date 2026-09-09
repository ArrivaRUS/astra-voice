"""Источник темы рабочего стола: светлая/тёмная и акцентный цвет.

Читаем **файл конфигурации сессии**, а не палитру Qt (`design/spec.md` §12.1,
урок Astra Cowork: в процессе оверлея Qt-палитра врёт). KDE — `~/.config/kdeglobals`,
Fly — `~/.fly/paletterc` (заглушка, M9).

Парсер работает без запущенного `QCoreApplication`: тему нужно знать **до**
`engine.load()`. Слежение за файлом (`start()`) требует Qt-цикла событий.
"""
from __future__ import annotations

import re
from abc import ABC, abstractmethod
from collections.abc import Callable
from pathlib import Path
from typing import Any

__all__ = ["ThemeSource", "KdeThemeSource", "FlyThemeSource", "read_ini", "parse_color", "luma"]

_COMMENT = ("#", ";")


def read_ini(path: Path) -> dict[str, dict[str, str]]:
    """Мини-парсер INI в стиле KConfig: без интерполяции, дубль ключа — последний выигрывает."""
    out: dict[str, dict[str, str]] = {}
    try:
        raw_text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
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
        except ValueError:
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

    def _notify(self) -> None:
        for callback in list(self._listeners):
            callback()

    def start(self) -> None:
        """Начать следить за источником (нужен цикл событий Qt). Не обязателен: источник
        без слежения (заглушка Fly) просто ничего не делает."""
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

    def stop(self) -> None:
        if self._watcher is None:
            return
        self._watcher.deleteLater()
        self._watcher = None

    def _rearm(self) -> None:
        """Вернуть путь под наблюдение: после атомарной перезаписи он выпадает из списка."""
        watcher = self._watcher
        if watcher is None:
            return
        parent = str(self._path.parent)
        if parent not in watcher.directories() and self._path.parent.is_dir():
            watcher.addPath(parent)
        target = str(self._path)
        if target not in watcher.files() and self._path.exists():
            watcher.addPath(target)

    def _on_changed(self, _path: str) -> None:
        self._rearm()
        self.reload()


class KdeThemeSource(_FileThemeSource):
    """KDE / Plasma: `~/.config/kdeglobals`.

    Тёмность — сперва по имени схемы (`[General] ColorScheme`), затем по яркости фона
    окна (`[Colors:Window] BackgroundNormal`); нет данных — светлая.
    Акцент — `[General] AccentColor`, запасной — фон выделения `[Colors:Selection]`.
    """

    DEFAULT_PATH = Path.home() / ".config" / "kdeglobals"

    def __init__(self, path: Path | str | None = None) -> None:
        super().__init__(Path(path) if path is not None else self.DEFAULT_PATH)

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


class FlyThemeSource(_FileThemeSource):
    """Fly (ALSE): `~/.fly/paletterc`.

    **Заглушка M1.** Разбор палитры Fly — задача M9 (`arch/plan-synth.md` §8): секция
    называется `[Variables]`, значения — пути к схемам. Пока честно отдаём светлую тему
    без акцента, чтобы окно во Fly не оказалось тёмным по ошибке.
    """

    DEFAULT_PATH = Path.home() / ".fly" / "paletterc"

    def __init__(self, path: Path | str | None = None) -> None:
        super().__init__(Path(path) if path is not None else self.DEFAULT_PATH)

    def _read(self) -> tuple[bool, str | None, str | None, str | None]:
        # TODO(M9): разобрать [Variables] ColorScheme / BackgroundColor и слежение за темой Fly.
        return (False, None, None, None)
