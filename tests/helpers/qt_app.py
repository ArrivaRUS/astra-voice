"""Общее получение QApplication для тестов меню и QML."""

from __future__ import annotations

import os

from PyQt5.QtCore import QCoreApplication
from PyQt5.QtWidgets import QApplication

_APP: QApplication | None = None


def get_qapplication() -> QApplication:
    """Вернуть единственный QApplication и удерживать его до конца процесса."""
    global _APP
    app = QCoreApplication.instance()
    if isinstance(app, QApplication):
        _APP = app
        return app
    if app is not None:
        raise RuntimeError(
            "QApplication уже создан как QCoreApplication — пересоздавать нельзя, см. урок 022"
        )
    if _APP is not None:
        raise RuntimeError("QApplication исчез после создания — пересоздавать нельзя, см. урок 022")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    _APP = QApplication([])
    return _APP
