"""US-2.6: строка «Держать модель в памяти» в «Продвинутых» с настоящим мостом настроек.

Снимок раздела (светлая тема) пишется только в каталог из ASTRA_VOICE_SNAPSHOT_DIR_US26;
без переменной снимок не сохраняется — эталоны репозитория тесты не трогают.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType
from typing import Any, cast
from unittest.mock import Mock

import pytest
from PyQt5 import sip
from PyQt5.QtCore import Qt, QUrl, qInstallMessageHandler
from PyQt5.QtQml import QQmlApplicationEngine
from PyQt5.QtQuick import QQuickItem, QQuickWindow
from PyQt5.QtTest import QTest

from astra_voice.core.settings import Settings
from astra_voice.ui.bridges import SettingsApply, SettingsBridge
from astra_voice.ui.icons import install_icon_provider
from helpers.qt_app import get_qapplication

pytestmark = pytest.mark.xvfb
REPO = Path(__file__).resolve().parents[2]
WIDTH, TALL = 1024, 900
LABELS = ["Всегда", "5 мин", "15 мин", "60 мин"]
MINUTES = [0, 5, 15, 60]

os.environ["QT_QUICK_CONTROLS_STYLE"] = "Default"
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QUICK_BACKEND", "software")


def load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


onboarding = load_module("_us26_onboarding", REPO / "tests/xvfb/test_onboarding.py")
updates_states = load_module("_us26_update_states", REPO / "tests/xvfb/test_update_states.py")


def find(window: QQuickWindow, name: str) -> QQuickItem:
    found = [
        item for item in onboarding.visual_tree(window.contentItem()) if item.objectName() == name
    ]
    assert len(found) == 1, name
    return cast(QQuickItem, found[0])


@contextmanager
def advanced(minutes: int = 0, height: int = 620) -> Iterator[tuple[Any, ...]]:
    app = get_qapplication()
    messages: list[str] = []
    previous = qInstallMessageHandler(lambda _mode, _context, message: messages.append(message))
    settings = Settings(model_unload_minutes=minutes)
    save = Mock()
    apply = Mock(spec=SettingsApply)
    bridge = SettingsBridge(settings, save=save, apply=apply, device_provider=lambda: [])
    theme = onboarding.FakeTheme(False)
    engine = QQmlApplicationEngine()
    install_icon_provider(engine)
    try:
        context = engine.rootContext()
        context.setContextProperty("themeSource", theme)
        context.setContextProperty("showOnboarding", False)
        # Как в приложении: QML загружается раньше, настоящий мост приходит позже.
        engine.load(QUrl.fromLocalFile(str(REPO / "qml/Main.qml")))
        assert engine.rootObjects(), messages
        window = cast(QQuickWindow, engine.rootObjects()[0])
        window.setWidth(WIDTH)
        window.setHeight(height)
        assert window.setProperty("freezeAnimations", True)
        background = updates_states.add_background(engine, window)
        window.show()
        QTest.qWait(120)
        onboarding.select_section(app, window, "advanced")
        selector = find(window, "modelUnloadSelector")
        assert not selector.isEnabled() and selector.property("currentIndex") == 0
        context.setContextProperty("settingsBridge", bridge)
        QTest.qWait(60)
        window.setWidth(WIDTH)
        window.setHeight(height)
        app.processEvents()
        yield app, window, selector, bridge, settings, save, apply, messages
        del background
    finally:
        for root in engine.rootObjects():
            root.close()
        sip.delete(engine)
        sip.delete(bridge)
        sip.delete(theme)
        app.processEvents()
        qInstallMessageHandler(previous)


def choose_with_keyboard(window: QQuickWindow, selector: QQuickItem, key: Qt.Key) -> None:
    selector.forceActiveFocus(Qt.TabFocusReason)
    QTest.keyClick(window, key)
    QTest.qWait(40)


@pytest.mark.parametrize("minutes", MINUTES)
def test_late_bridge_shows_saved_choice_without_saving(minutes: int) -> None:
    with advanced(minutes) as (_app, window, selector, _bridge, _settings, save, apply, messages):
        assert selector.isEnabled()
        assert selector.property("currentIndex") == MINUTES.index(minutes)
        assert selector.property("displayText") == LABELS[MINUTES.index(minutes)]
        assert selector.property("width") == 236
        row = find(window, "modelUnloadRow")
        assert row.property("label") == "Держать модель в памяти"
        assert row.property("sub")
        save.assert_not_called()
        apply.model_unload_minutes.assert_not_called()
        assert not messages, messages


def test_user_choice_is_saved_applied_and_failed_save_rolls_back() -> None:
    with advanced() as (_app, window, selector, bridge, settings, save, apply, messages):
        choose_with_keyboard(window, selector, Qt.Key_Down)
        assert selector.property("currentIndex") == 1
        assert settings.model_unload_minutes == 5 and bridge.modelUnloadMinutes == 5
        save.assert_called_once_with(settings)
        apply.model_unload_minutes.assert_called_once_with(5)
        save.reset_mock()
        apply.model_unload_minutes.reset_mock()
        save.side_effect = OSError("private test failure")
        choose_with_keyboard(window, selector, Qt.Key_Down)
        assert settings.model_unload_minutes == 5 and bridge.modelUnloadMinutes == 5
        assert selector.property("currentIndex") == 1  # список вернулся к сохранённому
        assert bridge.saveError == "Не удалось сохранить настройки"
        apply.model_unload_minutes.assert_not_called()
        save.side_effect = None
        choose_with_keyboard(window, selector, Qt.Key_Up)
        assert settings.model_unload_minutes == 0 and selector.property("currentIndex") == 0
        apply.model_unload_minutes.assert_called_once_with(0)
        assert not messages, messages


def test_snapshot_advanced_section_light() -> None:
    directory = os.environ.get("ASTRA_VOICE_SNAPSHOT_DIR_US26")
    with advanced(15, TALL) as (app, window, selector, _bridge, _s, _save, _apply, messages):
        onboarding.settle_pointer(app, window)
        image = updates_states.grab(app, window, WIDTH, TALL)
        assert not image.isNull()
        assert selector.property("displayText") == "15 мин"
        assert not messages, messages
    if not directory:
        pytest.skip("ASTRA_VOICE_SNAPSHOT_DIR_US26 не задан: снимок не сохраняется")
    target = Path(directory)
    target.mkdir(parents=True, exist_ok=True)
    assert image.save(str(target / "advanced-model-unload-light.png"), "PNG")
