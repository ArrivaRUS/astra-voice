"""`themeSource` для QML: обёртка над `core.theme.ThemeSource`.

`app.py` кладёт объект в контекст движка под именем `themeSource` **до** `engine.load()`;
`qml/Theme.qml` читает из него один флаг `dark`, всё остальное — токены дизайн-системы.
Смена темы в системе → сигнал `changed` → биндинги QML пересчитываются сами.
"""
from __future__ import annotations

from PyQt5.QtCore import QObject, pyqtProperty, pyqtSignal
from PyQt5.QtGui import QColor

from astra_voice.core.theme import ThemeSource

__all__ = ["ThemeBridge"]

# Запасные значения, если система цветов не сообщает: белое окно с чёрным текстом
# заведомо читаемо, а Theme.qml всё равно рисует своими токенами.
_FALLBACK_ACCENT = "#1B3A73"
_FALLBACK_WINDOW_BG = "#F7F9FC"
_FALLBACK_TEXT = "#0E1729"


class ThemeBridge(QObject):
    """QObject-фасад источника темы. Свойства только для чтения, сигнал один — `changed`."""

    changed = pyqtSignal()

    def __init__(self, source: ThemeSource, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._source = source
        self._source.subscribe(self._on_source_changed)

    @property
    def source(self) -> ThemeSource:
        return self._source

    def _on_source_changed(self) -> None:
        self.changed.emit()

    @pyqtProperty(bool, notify=changed)
    def dark(self) -> bool:
        return self._source.dark

    @pyqtProperty(QColor, notify=changed)
    def accent(self) -> QColor:
        return QColor(self._source.accent or _FALLBACK_ACCENT)

    @pyqtProperty(QColor, notify=changed)
    def windowBg(self) -> QColor:
        return QColor(self._source.window_bg or _FALLBACK_WINDOW_BG)

    @pyqtProperty(QColor, notify=changed)
    def text(self) -> QColor:
        return QColor(self._source.text or _FALLBACK_TEXT)
