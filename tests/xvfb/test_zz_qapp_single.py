"""Удаление QML-корня остаётся наблюдаемым после остальных xvfb-модулей."""

from __future__ import annotations

from pathlib import Path

import pytest
from PyQt5 import sip
from PyQt5.QtCore import QUrl
from PyQt5.QtQuick import QQuickView

from helpers.qt_app import get_qapplication

pytestmark = pytest.mark.xvfb


def test_qml_root_wrapper_is_deleted_with_view(tmp_path: Path) -> None:
    get_qapplication()
    source = tmp_path / "single_app.qml"
    source.write_text("import QtQuick 2.15\nItem { width: 1; height: 1 }\n", encoding="utf-8")
    view = QQuickView()
    view.setSource(QUrl.fromLocalFile(str(source)))
    assert view.status() == QQuickView.Ready, [error.toString() for error in view.errors()]
    root = view.rootObject()
    assert root is not None
    sip.delete(view)
    assert sip.isdeleted(root) is True
