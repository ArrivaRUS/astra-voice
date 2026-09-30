"""Фоновый старт (--hidden): окно настроек не показывается само (жалоба 30.09)."""

from __future__ import annotations

import ast
import inspect
import re

import pytest

from astra_voice import app
from astra_voice.core.paths import qml_dir

pytestmark = pytest.mark.unit


def test_main_window_is_not_visible_on_load() -> None:
    """Показ окна решает Python (focus_shell), а не QML при загрузке."""
    text = (qml_dir() / "Main.qml").read_text(encoding="utf-8")
    root = text[text.index("ApplicationWindow {") :]
    # Свойства корня — строки с отступом ровно в 4 пробела до первого вложенного блока.
    own = re.findall(r"^ {4}visible:\s*(.+)$", root, flags=re.MULTILINE)
    assert own[:1] == ["false"], own


def test_session_restore_is_refused() -> None:
    """KDE не восстанавливает копию из сеанса: подсказка RestartNever на каждый saveState."""
    from unittest.mock import Mock

    from PyQt5.QtGui import QSessionManager

    manager = Mock()
    app._never_restart(manager)
    manager.setRestartHint.assert_called_once_with(QSessionManager.RestartNever)
    main = ast.parse(inspect.getsource(app.main)).body[0]
    connects = [
        ast.unparse(node)
        for node in ast.walk(main)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "connect"
        and ast.unparse(node.func.value) == "app.saveStateRequest"
    ]
    assert connects == ["app.saveStateRequest.connect(_never_restart)"]
