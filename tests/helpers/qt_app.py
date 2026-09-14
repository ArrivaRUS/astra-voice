"""Общее получение QApplication для тестов меню и QML."""

from __future__ import annotations

import os

from PyQt5 import sip
from PyQt5.QtCore import QCoreApplication
from PyQt5.QtWidgets import QApplication


def get_qapplication() -> QApplication:
    """Вернуть QApplication, заменив приложение без поддержки виджетов.

    Это временный мост между зонами тестов до общего conftest: соседняя зона
    может первой создать голый QCoreApplication, а QMenu/QQuickView нужен
    QApplication. Решение об общем conftest принимает Юрка. QApplication
    совместим с последующим QCoreApplication.instance() or QCoreApplication([]).
    """
    app = QCoreApplication.instance()
    if isinstance(app, QApplication):
        return app

    found = f"{app!r} (тип {type(app).__module__}.{type(app).__qualname__})"
    message = (
        f"Не удалось получить QApplication: QCoreApplication.instance() вернул {found}; "
        "для QMenu/QQuickView нужен QApplication"
    )
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    try:
        if app is not None:
            sip.delete(app)
        app = QApplication([])
    except Exception as exc:
        raise RuntimeError(message) from exc
    if not isinstance(app, QApplication):
        raise RuntimeError(f"{message}; после создания получен {app!r} (тип {type(app)!r})")
    return app
