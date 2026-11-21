"""Mouse coordinates shared by the native and offscreen W1 probes."""

from PyQt5.QtCore import QPoint, QPointF
from PyQt5.QtGui import QStyleHints
from PyQt5.QtQuick import QQuickItem


def start_click_points(button: QQuickItem, hints: QStyleHints) -> tuple[QPoint, QPoint]:
    y = button.height() / 2
    first = button.mapToScene(QPointF(2, y)).toPoint()
    second = button.mapToScene(QPointF(button.width() - 2, y)).toPoint()
    distance = getattr(hints, "mouseDoubleClickDistance", hints.startDragDistance)()
    assert abs(second.x() - first.x()) > distance
    assert all(button.contains(button.mapFromScene(QPointF(p))) for p in (first, second))
    return first, second
