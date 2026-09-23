"""Состояния, иконки и подсказки трея (design/spec.md §9.1)."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

from astra_voice.core import paths
from astra_voice.core.theme import ThemeSource
from astra_voice.platform.session import SessionKind

if TYPE_CHECKING:
    from PyQt5.QtGui import QIcon

log = logging.getLogger(__name__)


class TrayState(Enum):
    IDLE = "idle"
    LISTENING = "listening"
    PROCESSING = "processing"
    DONE = "done"
    ERROR = "error"
    NOKEY = "nokey"


_TOOLTIPS = {
    TrayState.IDLE: "Astra Voice — готов · {hotkey}",
    TrayState.LISTENING: "Слушаю…",
    TrayState.PROCESSING: "Распознаю…",
    TrayState.DONE: "Готово",
    TrayState.ERROR: "Микрофон недоступен — открыть",
    TrayState.NOKEY: "Горячая клавиша не захвачена — выбрать другую",
}


def _explicit_svg(path: Path, *, dark: bool) -> bytes:
    """Явные цвета знака вместо currentColor; цвета состояний сохраняются.

    Файл рассчитан на перекраску оболочкой: цвет берётся из
    `<style id="current-color-scheme">`, а тёмный вариант — из
    `@media (prefers-color-scheme: dark)`. Когда значок грузится из файла
    (оболочка не подставила свой из темы), ни того, ни другого не происходит:
    Qt рендерит SVG Tiny и медиазапросы игнорирует, `currentColor` остаётся
    неразрешённым. На тёмной панели знак тогда либо чёрный, либо пустой —
    видно только то, что он нажимается. Поэтому цвет подставляем сами.
    """
    color = "#eff0f1" if dark else "#232629"
    return path.read_text(encoding="utf-8").replace("currentColor", color).encode("utf-8")


# Тема панели Fly: `[Variables] TaskbarColor` в ~/.fly/theme/current.themerc (после
# «Применить» в настройках оформления), иначе default.themerc пользователя, иначе
# системный. В поставке Astra по умолчанию `TaskbarColor = PrimaryDarkColor` —
# панель тёмная независимо от светлой темы окон, поэтому тёмный знак на ней не
# виден (машины заказчика на Fly, 21–22.09). Значок во Fly грузится из файла и
# перекрашивать его некому, кроме нас, — цвет берём по панели, а не по теме окон.
_FLY_THEME_FILES = ("current.themerc", "default.themerc")
_FLY_SYSTEM_THEME = Path("/usr/share/fly-wm/theme/default.themerc")
_THEME_LINE = re.compile(r"^\s*(?P<key>[A-Za-z0-9_]+)\s*=\s*(?P<value>.*?)\s*$")
_NAMED_COLORS = {
    "white": "#ffffff",
    "black": "#000000",
    "gray": "#bebebe",
    "grey": "#bebebe",
    "lightgray": "#d3d3d3",
    "lightgrey": "#d3d3d3",
    "darkgray": "#a9a9a9",
    "darkgrey": "#a9a9a9",
}


def fly_theme_files(home: Path | None = None) -> tuple[Path, ...]:
    base = (Path.home() if home is None else home) / ".fly" / "theme"
    return tuple(base / name for name in _FLY_THEME_FILES) + (_FLY_SYSTEM_THEME,)


def _theme_variables(path: Path) -> dict[str, str] | None:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    values: dict[str, str] = {}
    for line in text.splitlines():
        if line.lstrip().startswith((";", "#", "[")):
            continue
        match = _THEME_LINE.match(line)
        if match is None:
            continue
        value = match.group("value").strip().strip('"')
        values.setdefault(match.group("key"), value)
    return values


def color_is_dark(value: str) -> bool | None:
    """Тёмный ли цвет Fly: `#rrggbb`, имя X11 или переменная палитры (`PrimaryDarkColor`).

    None — сказать нельзя (пусто, картинка, незнакомое имя).
    """
    value = value.strip().strip('"')
    if not value:
        return None
    color = _NAMED_COLORS.get(value.lower(), value)
    if re.fullmatch(r"#[0-9a-fA-F]{6}", color):
        r, g, b = (int(color[i : i + 2], 16) for i in (1, 3, 5))
        return (0.299 * r + 0.587 * g + 0.114 * b) / 255 < 0.5
    lowered = value.lower()
    if "dark" in lowered:
        return True
    if "light" in lowered:
        return False
    return None


def fly_panel_dark(files: tuple[Path, ...] | None = None) -> bool | None:
    """Тёмная ли панель Fly по первому читаемому файлу темы; None — неизвестно."""
    for path in fly_theme_files() if files is None else files:
        values = _theme_variables(path)
        if values is None:
            continue
        if values.get("TaskbarImage", ""):
            log.debug("Панель Fly с картинкой (%s): цвет панели неизвестен", path)
            return None
        dark = color_is_dark(values.get("TaskbarColor", ""))
        log.debug(
            "Панель Fly по %s: TaskbarColor=%r → dark=%s", path, values.get("TaskbarColor"), dark
        )
        return dark
    return None


def find_tray_icon_path(name: str, size: int) -> Path:
    """Ищет SVG сначала в установленной теме, затем в данных дерева разработки.

    Если файла нет, ошибка перечисляет оба проверенных пути.
    """
    relative = Path(f"{size}x{size}") / "status" / f"{name}.svg"
    candidates = (
        paths.icon_theme_dir() / relative,
        paths.data_dir_static() / "icons" / "hicolor" / relative,
    )
    for path in candidates:
        if path.is_file():
            return path
    raise FileNotFoundError(f"Не найден SVG трея: {candidates[0]}; {candidates[1]}")


class TrayIconProvider:
    """Загрузка иконок требует QApplication; после смены темы нужен refresh()."""

    def __init__(
        self,
        session: SessionKind,
        theme: ThemeSource | None = None,
        *,
        panel_dark: Callable[[], bool | None] | None = None,
    ) -> None:
        self._session = session
        self._theme = theme
        self._panel_dark = fly_panel_dark if panel_dark is None else panel_dark
        self._cache: dict[TrayState, QIcon] = {}

    def _file_icon_dark(self) -> bool:
        """Цвет знака из файла: во Fly — по панели, иначе по теме окон."""
        if self._session == SessionKind.FLY:
            panel = self._panel_dark()
            if panel is not None:
                return panel
        return self._theme.dark if self._theme is not None else False

    def icon_name(self, state: TrayState) -> str:
        return f"astravoice-tray-{state.value}"

    def icon(self, state: TrayState) -> QIcon:
        from PyQt5.QtGui import QIcon, QPixmap

        if state in self._cache:
            return self._cache[state]

        try:
            name = self.icon_name(state)
            fly = self._session == SessionKind.FLY
            icon = QIcon() if fly else QIcon.fromTheme(name)
            if icon.isNull():
                dark = self._file_icon_dark()
                for size in (16, 22):
                    path = find_tray_icon_path(name, size)
                    # Из файла — всегда с явным цветом: перекрашивать здесь
                    # уже некому, в любой оболочке (У…, дефект тёмной панели).
                    data = _explicit_svg(path, dark=dark)
                    pixmap = QPixmap()
                    if not pixmap.loadFromData(data, "SVG") or pixmap.isNull():
                        raise ValueError(f"SVG не даёт непустой значок: {path}")
                    icon.addPixmap(pixmap)
            # Иконка темы может быть непустой до фактического рендера её файла.
            if icon.isNull() or icon.pixmap(16, 16).isNull():
                raise ValueError("Значок трея не даёт непустой QPixmap")
        except Exception as exc:
            log.warning("Не удалось загрузить значок трея %s: %s", state.value, exc)
            icon = QIcon()

        self._cache[state] = icon
        return icon

    def has_icon(self, state: TrayState) -> bool:
        """Есть ли непустой значок; неудачная загрузка тоже кэшируется до refresh()."""
        return not self.icon(state).isNull()

    def tooltip(self, state: TrayState, *, hotkey: str = "Ctrl+Space") -> str:
        return _TOOLTIPS[state].format(hotkey=hotkey)

    def refresh(self) -> None:
        """Сбросить иконки, чтобы следующий запрос учитывал новую тему панели."""
        self._cache.clear()
