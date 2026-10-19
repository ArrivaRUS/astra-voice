"""Значок приложения для окон: явный ``QGuiApplication.setWindowIcon``.

Без него значок окна в панели задач зависит от того, сопоставит ли оконный
менеджер ``WM_CLASS`` с записью меню: KDE это делает, Fly 1.8 — нет и
показывает значок по умолчанию. Явный значок приложения Qt выставляет каждому
окну (на X11 — ``_NET_WM_ICON``), поэтому он виден в любой оболочке.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from astra_voice.core import paths

if TYPE_CHECKING:
    from PyQt5.QtGui import QIcon

log = logging.getLogger(__name__)

APP_ICON_NAME = "astravoice"
# Крупнее не берём: Qt кладёт в _NET_WM_ICON каждого окна все размеры значка,
# а 512×512 ARGB — это ещё 1 МиБ на окно ради размера, который панели не нужен.
MAX_WINDOW_ICON_SIZE = 256


def _square_size(name: str) -> int | None:
    width, separator, height = name.partition("x")
    if not separator or width != height or not width.isascii() or not width.isdecimal():
        return None
    size = int(width)
    return size if 0 < size <= MAX_WINDOW_ICON_SIZE else None


def app_icon_files() -> tuple[Path, ...]:
    """PNG значка приложения по возрастанию размера из первого корня, где они есть.

    Корни — :func:`paths.icon_theme_roots`. Размеры из разных корней не смешиваем.
    """
    for root in paths.icon_theme_roots():
        found: list[tuple[int, Path]] = []
        try:
            for directory in root.iterdir():
                size = _square_size(directory.name)
                icon = directory / "apps" / f"{APP_ICON_NAME}.png"
                if size is not None and icon.is_file():
                    found.append((size, icon))
        except OSError:
            continue
        if found:
            return tuple(icon for _, icon in sorted(found))
    return ()


def app_icon() -> QIcon:
    """Значок из :func:`app_icon_files`; пустой ``QIcon``, если файлов нет или они битые.

    Требует существующего ``QGuiApplication``: Qt грузит PNG в ``QPixmap``.
    """
    from PyQt5.QtGui import QIcon, QPixmap

    icon = QIcon()
    for path in app_icon_files():
        pixmap = QPixmap(str(path))
        if not pixmap.isNull():
            icon.addPixmap(pixmap)
    return icon


def install_app_icon(app: object) -> bool:
    """Ставит значок приложения всем окнам ``app``; без значка — молча ``False``.

    Вызывать сразу после создания ``QApplication`` и до создания окон и QML.
    """
    from PyQt5.QtGui import QGuiApplication

    if not isinstance(app, QGuiApplication):
        log.debug("Значок приложения не задан: нет QGuiApplication")
        return False
    try:
        icon = app_icon()
    except Exception:  # noqa: BLE001 — без значка окна всё равно должны открыться
        log.debug("Значок приложения не задан: ошибка загрузки")
        return False
    if icon.isNull():
        log.debug("Значок приложения не задан: нет пригодного %s.png", APP_ICON_NAME)
        return False
    app.setWindowIcon(icon)
    return True
