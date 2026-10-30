"""Контурные иконки Astra Voice.

Собственные контуры (не Breeze) из design/mockups/directions/_base.py:261 (_IC).
Правила: design/tokens.json icon.* — viewBox 0 0 16 16, stroke 1.5,
fill none по умолчанию, скруглённые концы и стыки, цвет задаёт вызывающий
компонент. Глифы встреч: design/meetings/tokens.md §8.2; fill=True включает
заливку rec/stop/play/more по решению №34, rec сохраняет контур внешнего кольца.
"""

from __future__ import annotations

import logging
import math
import re
from functools import lru_cache

from PyQt5 import sip
from PyQt5.QtCore import QBuffer, QByteArray, QIODevice, QSize
from PyQt5.QtGui import QImage, QImageReader
from PyQt5.QtQml import QQmlEngine, QQmlImageProviderBase
from PyQt5.QtQuick import QQuickImageProvider

log = logging.getLogger(__name__)
PROVIDER_ID = "avicon"
_providers: list[IconProvider] = []

PATHS: dict[str, str] = {
    "chev": "M6.5 3.5L11 8l-4.5 4.5",
    "chevd": "M3.5 6.5L8 11l4.5-4.5",
    "check": "M3 8.4l3.4 3.3L13 4.6",
    "down": "M8 2.8v7.4m0 0l-3-3m3 3l3-3M2.8 13.2h10.4",
    "folder": "M2.2 4.4h4.2l1.3 1.7h6.1v7.5H2.2z",
    "refresh": "M13.2 8a5.2 5.2 0 1 1-1.6-3.7M13.4 2.6v2.9h-2.9",
    "file": "M4 2.2h5l3 3v8.6H4zM9 2.2v3.2h3",
    "shield": "M8 2.2l5 1.9v3.7c0 3-2.1 5.3-5 6.1-2.9-.8-5-3.1-5-6.1V4.1z",
    "lock": "M4.4 7.2h7.2v6H4.4zM5.9 7.2V5.4a2.1 2.1 0 0 1 4.2 0v1.8",
    "alert": "M8 2.6l6 10.8H2zM8 6.6v3.1M8 11.4v.1",
    "info": "M8 14A6 6 0 1 0 8 2a6 6 0 0 0 0 12zM8 7.4v3.6M8 5.2v.1",
    "cog": (
        "M8 10.1a2.1 2.1 0 1 0 0-4.2 2.1 2.1 0 0 0 0 4.2zM8 1.9v1.7M8 12.4v1.7"
        "M2.7 8h1.7M11.6 8h1.7M4.2 4.2l1.2 1.2M10.6 10.6l1.2 1.2"
        "M11.8 4.2l-1.2 1.2M5.4 10.6l-1.2 1.2"
    ),
    "globe": "M8 14A6 6 0 1 0 8 2a6 6 0 0 0 0 12zM2.2 8h11.6M8 2a9 9 0 0 1 0 12A9 9 0 0 1 8 2z",
    "trash": "M3.4 4.6h9.2M6.2 4.6V3.2h3.6v1.4M4.6 4.6l.6 8.4h5.6l.6-8.4",
    "power": "M8 2.4v5.4M4.6 4.4a4.8 4.8 0 1 0 6.8 0",
    "search": "M7.2 12a4.8 4.8 0 1 0 0-9.6 4.8 4.8 0 0 0 0 9.6zM10.8 10.8L13.6 13.6",
    "x": "M4.2 4.2l7.6 7.6M11.8 4.2l-7.6 7.6",
    "chip": (
        "M5.4 5.4h5.2v5.2H5.4zM6.6 2.6v2.8M9.4 2.6v2.8M6.6 10.6v2.8"
        "M9.4 10.6v2.8M2.6 6.6h2.8M2.6 9.4h2.8M10.6 6.6h2.8M10.6 9.4h2.8"
    ),
    "sliders": "M3 5h10M3 11h10M6.2 3.2v3.6M10.4 9.2v3.6",
    "out": "M6.2 3.2H3.2v9.6h9.6V9.8M9.4 2.8h3.8v3.8M13.2 2.8L7.6 8.4",
    "clock": "M8 14A6 6 0 1 0 8 2a6 6 0 0 0 0 12zM8 4.7V8l2.4 1.5",
}

MEETING_PATHS = {
    "meeting": (
        "M2.5 3.5h7a1 1 0 0 1 1 1v4a1 1 0 0 1-1 1H6l-2.5 2v-2h-1a1 1 0 0 1-1-1v-4a1 1 0"
        " 0 1 1-1zM12.5 6.5h1a1 1 0 0 1 1 1v4a1 1 0 0 1-1 1h-1v2l-2.5-2H8"
    ),
    "rec": (
        "M13.6 8a5.6 5.6 0 1 0-11.2 0 5.6 5.6 0 1 0 11.2 0zM10.4 8a2.4 2.4 0 1 0-4.8 0 "
        "2.4 2.4 0 1 0 4.8 0z"
    ),
    "pause": "M6 3.5v9M10 3.5v9",
    "stop": (
        "M5.2 4h5.6a1.2 1.2 0 0 1 1.2 1.2v5.6a1.2 1.2 0 0 1-1.2 1.2H5.2A1.2 1.2 0 0 1 4"
        " 10.8V5.2A1.2 1.2 0 0 1 5.2 4z"
    ),
    "play": "M5 3.3v9.4L12.8 8z",
    "chat": (
        "M2.5 3h11a1 1 0 0 1 1 1v6.5a1 1 0 0 1-1 1H7l-3 2.5v-2.5H2.5a1 1 0 0 1-1-1V4a1 "
        "1 0 0 1 1-1zM4.5 6h7M4.5 8.5h4.5"
    ),
    "collapse": "M2 4.5 5.5 8 2 11.5M14 4.5 10.5 8 14 11.5",
    "expand": "M5.5 4.5 2 8l3.5 3.5M10.5 4.5 14 8l-3.5 3.5",
    "chevl": "M10 4L6 8l4 4",
    "more": (
        "M4.4 8a.9.9 0 1 0-1.8 0 .9.9 0 1 0 1.8 0zM8.9 8a.9.9 0 1 0-1.8 0 .9.9 0 1 0 1."
        "8 0zM13.4 8a.9.9 0 1 0-1.8 0 .9.9 0 1 0 1.8 0z"
    ),
    "home": "M2.5 7.5 8 3l5.5 4.5M4 6.5v6.5h8V6.5M6.8 13v-3.2h2.4V13",
    "mic": (
        "M8 2a2 2 0 0 1 2 2v3.5a2 2 0 0 1-4 0V4a2 2 0 0 1 2-2zM3.8 7.8a4.2 4.2 0 0 0 8.4 0M8 12v2.2"
    ),
    "people": (
        "M8.2 5.5a2.2 2.2 0 1 0-4.4 0 2.2 2.2 0 1 0 4.4 0zM2 13c.3-2.3 1.9-3.6 4-3.6s3."
        "7 1.3 4 3.6M13.1 6a1.8 1.8 0 1 0-3.6 0 1.8 1.8 0 1 0 3.6 0zM11 9.4c1.7.1 2.8 1"
        ".2 3 3.1"
    ),
    "edit": "M10.8 2.7l2.5 2.5-7.8 7.8-3 .5.5-3z",
    "save": "M8 2.2v8M4.8 7.2 8 10.4l3.2-3.2M2.8 12.2v1.5h10.4v-1.5",
    "spark": (
        "M8 1.8l1.4 4 4 1.4-4 1.4L8 12.6 6.6 8.6l-4-1.4 4-1.4zM12.6 11.2l.6 1.5 1.5.6-1"
        ".5.6-.6 1.5-.6-1.5-1.5-.6 1.5-.6z"
    ),
    "merge": "M4 2.5v3.2c0 1.8 1.5 3 4 3s4-1.2 4-3V2.5M8 8.7v4.8M5.8 11.4 8 13.6l2.2-2.2",
    "copy": (
        "M6.7 5.5h5.6a1.2 1.2 0 0 1 1.2 1.2v6.1a1.2 1.2 0 0 1-1.2 1.2H6.7a1.2 1.2 0 0 1"
        "-1.2-1.2V6.7a1.2 1.2 0 0 1 1.2-1.2zM10.5 5.5V3a1 1 0 0 0-1-1h-6a1 1 0 0 0-1 1v"
        "7.5a1 1 0 0 0 1 1h2"
    ),
    "head": (
        "M2.8 10V8a5.2 5.2 0 0 1 10.4 0v2M3 9.2h1a1 1 0 0 1 1 1v2.3a1 1 0 0 1-1 1H3a1 1"
        " 0 0 1-1-1v-2.3a1 1 0 0 1 1-1zM12 9.2h1a1 1 0 0 1 1 1v2.3a1 1 0 0 1-1 1h-1a1 1"
        " 0 0 1-1-1v-2.3a1 1 0 0 1 1-1z"
    ),
    "speaker": (
        "M2.5 6.2h2.7L8.8 3v10L5.2 9.8H2.5zM11 5.5a3.5 3.5 0 0 1 0 5M12.8 3.8a6 6 0 0 1 0 8.4"
    ),
    "doc": "M4 1.8h8V14H4zM6 5h4M6 7.5h4M6 10h2.5",
    "chart": "M2.5 13.5h11M4.5 11V8M8 11V4.5M11.5 11V6.5",
}

MEETING_NAMES = frozenset(MEETING_PATHS)
FILLED_NAMES = frozenset({"rec", "stop", "play", "more"})
PATHS.update(MEETING_PATHS)

_REQUEST = re.compile(
    r"([^?&/]+)\?c=([0-9a-f]{6}(?:[0-9a-f]{2})?)"
    r"&w=((?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?)(?:&f=([01]))?\Z"
)


def _transparent(size: QSize) -> QImage:
    image = QImage(size, QImage.Format_ARGB32_Premultiplied)
    image.fill(0)
    return image


@lru_cache(maxsize=512)
def _render_icon(
    name: str, color: str, stroke: float, width: int, height: int, fill: bool = False
) -> QImage:
    opacity = int(color[6:], 16) / 255 if len(color) == 8 else 1.0
    paint_fill = fill and name in FILLED_NAMES
    outline = PATHS[name]
    solid = ""
    if paint_fill and name == "rec":
        outline, dot = outline.split("zM", 1)
        outline += "z"
        solid = f'<path d="M{dot}" fill="#{color[:6]}" fill-opacity="{opacity}" stroke="none"/>'
        paint_fill = False
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 16 16">'
        f'<path d="{outline}" fill="{"#" + color[:6] if paint_fill else "none"}" '
        f'fill-opacity="{opacity}" stroke="{"none" if paint_fill else "#" + color[:6]}" '
        f'stroke-opacity="{opacity}" stroke-width="{stroke}" '
        f'stroke-linecap="round" stroke-linejoin="round"/>{solid}</svg>'
    ).encode("ascii")
    buffer = QBuffer()
    buffer.setData(QByteArray(svg))
    buffer.open(QIODevice.ReadOnly)
    reader = QImageReader(buffer, b"svg")
    reader.setScaledSize(QSize(width, height))
    image = reader.read()
    if image.isNull():
        log.warning("icon_render_failed")
        return _transparent(QSize(width, height))
    return image.convertToFormat(QImage.Format_ARGB32_Premultiplied)


class IconProvider(QQuickImageProvider):
    def __init__(self) -> None:
        super().__init__(QQmlImageProviderBase.Image)

    def requestImage(self, id: str, requestedSize: QSize) -> tuple[QImage, QSize]:
        if not requestedSize.isValid() or requestedSize.width() <= 0 or requestedSize.height() <= 0:
            size = QSize(16, 16)
        else:
            size = QSize(min(requestedSize.width(), 256), min(requestedSize.height(), 256))
        match = _REQUEST.fullmatch(id)
        if match is None or match[1] not in PATHS:
            log.warning("icon_request_invalid")
            return _transparent(size), size
        try:
            stroke = float(match[3])
        except ValueError:
            stroke = math.nan
        if not math.isfinite(stroke) or not 0.5 <= stroke <= 4 or requestedSize != size:
            log.warning("icon_request_invalid")
            return _transparent(size), size
        # Копия QImage разделяет пиксели до первой записи, затем отделяется,
        # не меняя исходное изображение в кеше.
        return QImage(
            _render_icon(match[1], match[2], stroke, size.width(), size.height(), match[4] == "1")
        ), size


def install_icon_provider(engine: QQmlEngine) -> None:
    if engine.imageProvider(PROVIDER_ID) is not None:
        return
    provider = IconProvider()
    # C++-провайдером владеет движок (/Transfer/), но без живой Python-обёртки
    # requestImage уходит в пустую базовую реализацию. Храним обёртку здесь и
    # при каждой установке убираем удалённые провайдеры. Не подключаем лямбду
    # с замыканием к engine.destroyed: GC может обнулить её __closure__ через
    # устаревшую обёртку движка, что приводит к segfault (урок 021).
    _providers[:] = [p for p in _providers if not sip.isdeleted(p)]
    _providers.append(provider)
    engine.addImageProvider(PROVIDER_ID, provider)
