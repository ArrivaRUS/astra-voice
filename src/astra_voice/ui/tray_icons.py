"""Состояния, иконки и подсказки трея (design/spec.md §9.1)."""

from __future__ import annotations

import logging
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

    def __init__(self, session: SessionKind, theme: ThemeSource | None = None) -> None:
        self._session = session
        self._theme = theme
        self._cache: dict[TrayState, QIcon] = {}

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
                dark = self._theme.dark if self._theme is not None else False
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
