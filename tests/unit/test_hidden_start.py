"""Фоновый старт (--hidden): окно настроек не показывается само (жалоба 30.09)."""

from __future__ import annotations

import ast
import inspect
from collections.abc import Iterator

import pytest
from PyQt5 import sip
from PyQt5.QtCore import QObject, QUrl, pyqtProperty, pyqtSignal, pyqtSlot
from PyQt5.QtQml import QQmlApplicationEngine
from PyQt5.QtQuick import QQuickWindow
from PyQt5.QtWidgets import QApplication

from astra_voice import app
from astra_voice.core.paths import qml_dir
from astra_voice.ui.icons import install_icon_provider
from helpers.qt_app import get_qapplication

pytestmark = pytest.mark.unit


@pytest.fixture
def loaded_main(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[QApplication, QQmlApplicationEngine, QQuickWindow, FakeOnboarding]]:
    monkeypatch.setenv("QT_QUICK_CONTROLS_STYLE", "Default")
    qt_app = get_qapplication()
    engine = QQmlApplicationEngine()
    fake: FakeOnboarding | None = None
    try:
        install_icon_provider(engine)
        context = engine.rootContext()
        context.setContextProperty("showOnboarding", False)
        context.setContextProperty("themeSource", None)
        engine.load(QUrl.fromLocalFile(str(qml_dir() / "Main.qml")))
        roots = engine.rootObjects()
        assert len(roots) == 1
        window = roots[0]
        assert isinstance(window, QQuickWindow)
        fake = FakeOnboarding(window)
        yield qt_app, engine, window, fake
    finally:
        sip.delete(engine)
        if fake is not None:
            sip.delete(fake)
        qt_app.processEvents()


def test_main_window_is_not_visible_on_load(
    loaded_main: tuple[QApplication, QQmlApplicationEngine, QQuickWindow, FakeOnboarding],
) -> None:
    """Показ окна решает Python (focus_shell), а не QML при загрузке."""
    _, engine, _, _ = loaded_main
    assert engine.rootObjects()[0].isVisible() is False


class FakeOnboarding(QObject):
    """Контракт шага 4; вызовы монитора запоминают видимость окна."""

    changed = pyqtSignal()

    def __init__(self, window: QQuickWindow) -> None:
        super().__init__()
        self.window = window
        self.calls: list[tuple[str, bool]] = []

    @pyqtProperty(int, notify=changed)
    def step(self) -> int:
        return 4

    @pyqtProperty(int, constant=True)
    def totalSteps(self) -> int:  # noqa: N802
        return 5

    @pyqtProperty(str, notify=changed)
    def levelState(self) -> str:  # noqa: N802
        return "idle"

    @pyqtProperty(str, notify=changed)
    def levelMessage(self) -> str:  # noqa: N802
        return ""

    @pyqtProperty(bool, notify=changed)
    def done(self) -> bool:
        return False

    @pyqtProperty(bool, constant=True)
    def canFinish(self) -> bool:  # noqa: N802
        return False

    @pyqtProperty(str, constant=True)
    def downloadState(self) -> str:  # noqa: N802
        return "idle"

    @pyqtProperty(str, constant=True)
    def downloadTitle(self) -> str:  # noqa: N802
        return ""

    @pyqtProperty(str, constant=True)
    def downloadCounter(self) -> str:  # noqa: N802
        return ""

    @pyqtProperty(str, constant=True)
    def downloadSource(self) -> str:  # noqa: N802
        return ""

    @pyqtProperty(float, constant=True)
    def downloadProgress(self) -> float:  # noqa: N802
        return 0.0

    @pyqtProperty(str, constant=True)
    def speed(self) -> str:
        return ""

    @pyqtProperty(str, constant=True)
    def eta(self) -> str:
        return ""

    @pyqtProperty(str, constant=True)
    def downloadDetail(self) -> str:  # noqa: N802
        return ""

    @pyqtProperty(str, constant=True)
    def device(self) -> str:
        return ""

    @pyqtProperty(str, constant=True)
    def deviceResolved(self) -> str:  # noqa: N802
        return ""

    @pyqtProperty("QVariantList", constant=True)
    def devices(self) -> list[dict[str, str]]:
        return [{"id": "", "name": "Системный по умолчанию"}]

    @pyqtProperty("QVariantList", constant=True)
    def models(self) -> list[dict[str, str]]:
        return []

    @pyqtProperty(float, constant=True)
    def level(self) -> float:
        return 0.0

    @pyqtProperty(str, constant=True)
    def peak(self) -> str:
        return ""

    @pyqtProperty(str, constant=True)
    def testDuration(self) -> str:  # noqa: N802
        return ""

    @pyqtProperty(bool, constant=True)
    def modelReady(self) -> bool:  # noqa: N802
        return False

    @pyqtProperty(str, constant=True)
    def testPhrase(self) -> str:  # noqa: N802
        return ""

    @pyqtProperty(str, constant=True)
    def testText(self) -> str:  # noqa: N802
        return ""

    @pyqtProperty(str, constant=True)
    def testState(self) -> str:  # noqa: N802
        return "idle"

    @pyqtProperty(str, constant=True)
    def testMessage(self) -> str:  # noqa: N802
        return ""

    @pyqtSlot()
    def startLevelMonitor(self) -> None:  # noqa: N802
        self.calls.append(("start", self.window.isVisible()))

    @pyqtSlot()
    def stopLevelMonitor(self) -> None:  # noqa: N802
        self.calls.append(("stop", self.window.isVisible()))


def test_mic_monitor_follows_main_window_visibility(
    loaded_main: tuple[QApplication, QQmlApplicationEngine, QQuickWindow, FakeOnboarding],
) -> None:
    qt_app, engine, window, fake = loaded_main
    context = engine.rootContext()
    context.setContextProperty("onboarding", fake)
    context.setContextProperty("showOnboarding", True)
    qt_app.processEvents()

    assert not any(name == "start" for name, _ in fake.calls)

    window.setVisible(True)
    qt_app.processEvents()
    assert fake.calls[-1] == ("start", True)
    assert ("start", False) not in fake.calls

    window.setVisible(False)
    qt_app.processEvents()
    assert fake.calls[-1][0] == "stop"

    window.setVisible(True)
    qt_app.processEvents()
    assert fake.calls[-1] == ("start", True)
    assert ("start", False) not in fake.calls


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
