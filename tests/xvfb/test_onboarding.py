"""Пять шагов и настройки в двух темах: Qt5, состояния мостов и детерминированные PNG.

Все записи тестов, включая временные PNG, ограничены SNAPSHOTS. Публикация
через os.replace выполняется в teardown только после успеха всего модуля.
"""

from __future__ import annotations

import os
import sys
import tempfile
from collections.abc import Callable, Iterator
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest
from PyQt5 import sip
from PyQt5.QtCore import (
    QBuffer,
    QByteArray,
    QIODevice,
    QMetaObject,
    QObject,
    Qt,
    QUrl,
    pyqtProperty,
    pyqtSignal,
    pyqtSlot,
    qInstallMessageHandler,
)
from PyQt5.QtGui import QImage
from PyQt5.QtQml import QQmlApplicationEngine, QQmlComponent
from PyQt5.QtQuick import QQuickView, QQuickWindow
from PyQt5.QtTest import QTest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from helpers.qt_app import get_qapplication  # noqa: E402

pytestmark = pytest.mark.xvfb
REPO = Path(__file__).resolve().parents[2]
# Промежуточные прогоны разработки не трогают эталоны репозитория.
SNAPSHOTS = REPO / Path(os.environ.get("ASTRA_VOICE_SNAPSHOT_DIR") or "design/refs/impl/onboarding")
WIDTH, HEIGHT = 900, 588
STEP_NAMES = {
    1: "01-welcome",
    2: "02-model",
    3: "03-hotkey",
    4: "04-mic",
    5: "05-done",
    6: "06-settings-general",
}
CASES = [(step, dark) for step in STEP_NAMES for dark in (False, True)]
MODEL_STATES = (
    "absent",
    "downloadable",
    "downloading",
    "verifying",
    "installing",
    "installed",
    "broken",
    "no-network",
    "no-space",
    "no-ram",
    "cancelled",
)
CAPTURE_STATES = (
    "idle",
    "capturing",
    "captured",
    "success",
    "conflict",
    "duplicate",
    "not-grabbed",
)

os.environ["QT_QUICK_CONTROLS_STYLE"] = "Default"
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QUICK_BACKEND", "software")


class FakeTheme(QObject):
    changed = pyqtSignal()

    def __init__(self, dark: bool) -> None:
        super().__init__()
        self._dark = dark

    @pyqtProperty(bool, notify=changed)
    def dark(self) -> bool:
        return self._dark


class FakeAppInfo(QObject):
    """Только используемый Main.qml контракт настоящего AppInfo из _make_app_info."""

    debugChanged = pyqtSignal()
    showSection = pyqtSignal(str, arguments=["section"])

    @pyqtProperty(str, constant=True)
    def version(self) -> str:
        return "0.1.0"

    @pyqtProperty(str, constant=True)
    def sessionKind(self) -> str:  # noqa: N802 — имя свойства для QML
        return "OTHER"

    @pyqtProperty(bool, notify=debugChanged)
    def debug(self) -> bool:
        return True


class FakeOnboarding(QObject):
    """Полный изменяемый контракт; слоты записывают вызовы без побочных действий."""

    changed = pyqtSignal()
    deviceResolvedChanged = pyqtSignal()

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[str] = []
        self._step: int = 1
        self._policyLocked: bool = False
        self._policyLockedText: str = "Задано администратором"
        self._modelState: str = "downloadable"
        self._modelName: str = "GigaAM v3 RNN-T"
        self._modelSize: str = "231,9 МБ"
        self._modelRam: str = "415 МБ"
        self._modelHost: str = "huggingface.co"
        self._modelMessage: str = ""
        self._progress: float = 0.43
        self._speed: str = "5,2 МБ/с"
        self._eta: str = "~25 с"
        self._hotkey: str = "Ctrl + Space"
        self._hotkeyMode: str = "ptt"
        self._captureState: str = "idle"
        self._captureMessage: str = ""
        self._pendingCombo: str = ""
        self._freeCandidates: list[str] = []
        self._devices: list[dict[str, str]] = [
            {"id": "", "name": "Системный по умолчанию"},
            {"id": "builtin", "name": "Встроенный микрофон"},
        ]
        self._device: str = ""
        self._deviceResolved: str = "Встроенный микрофон"
        self._level: float = 1.0
        self._peak: str = "−18 дБ"
        self._testDuration: str = "0,31 с"
        self._testPhrase: str = "Сегодня хороший день для прогулки."
        self._testText: str = "Проверка связи, раз, два, три."
        self._testState: str = "done"
        self._testMessage: str = ""
        self._canFinish: bool = True
        self._language: str = "ru"
        self._checkAppUpdates: bool = False
        self._checkModelUpdates: bool = False

    def _get_step(self) -> int:
        return self._step

    def _set_step(self, value: int) -> None:
        self._step = value
        self.changed.emit()

    step = pyqtProperty(int, _get_step, _set_step, notify=changed)

    def _get_policyLocked(self) -> bool:
        return self._policyLocked

    def _set_policyLocked(self, value: bool) -> None:
        self._policyLocked = value
        self.changed.emit()

    policyLocked = pyqtProperty(bool, _get_policyLocked, _set_policyLocked, notify=changed)

    def _get_policyLockedText(self) -> str:
        return self._policyLockedText

    def _set_policyLockedText(self, value: str) -> None:
        self._policyLockedText = value
        self.changed.emit()

    policyLockedText = pyqtProperty(
        str, _get_policyLockedText, _set_policyLockedText, notify=changed
    )

    def _get_modelState(self) -> str:
        return self._modelState

    def _set_modelState(self, value: str) -> None:
        self._modelState = value
        self.changed.emit()

    modelState = pyqtProperty(str, _get_modelState, _set_modelState, notify=changed)

    def _get_modelName(self) -> str:
        return self._modelName

    def _set_modelName(self, value: str) -> None:
        self._modelName = value
        self.changed.emit()

    modelName = pyqtProperty(str, _get_modelName, _set_modelName, notify=changed)

    def _get_modelSize(self) -> str:
        return self._modelSize

    def _set_modelSize(self, value: str) -> None:
        self._modelSize = value
        self.changed.emit()

    modelSize = pyqtProperty(str, _get_modelSize, _set_modelSize, notify=changed)

    def _get_modelRam(self) -> str:
        return self._modelRam

    def _set_modelRam(self, value: str) -> None:
        self._modelRam = value
        self.changed.emit()

    modelRam = pyqtProperty(str, _get_modelRam, _set_modelRam, notify=changed)

    def _get_modelHost(self) -> str:
        return self._modelHost

    def _set_modelHost(self, value: str) -> None:
        self._modelHost = value
        self.changed.emit()

    modelHost = pyqtProperty(str, _get_modelHost, _set_modelHost, notify=changed)

    def _get_modelMessage(self) -> str:
        return self._modelMessage

    def _set_modelMessage(self, value: str) -> None:
        self._modelMessage = value
        self.changed.emit()

    modelMessage = pyqtProperty(str, _get_modelMessage, _set_modelMessage, notify=changed)

    def _get_progress(self) -> float:
        return self._progress

    def _set_progress(self, value: float) -> None:
        self._progress = value
        self.changed.emit()

    progress = pyqtProperty(float, _get_progress, _set_progress, notify=changed)

    def _get_speed(self) -> str:
        return self._speed

    def _set_speed(self, value: str) -> None:
        self._speed = value
        self.changed.emit()

    speed = pyqtProperty(str, _get_speed, _set_speed, notify=changed)

    def _get_eta(self) -> str:
        return self._eta

    def _set_eta(self, value: str) -> None:
        self._eta = value
        self.changed.emit()

    eta = pyqtProperty(str, _get_eta, _set_eta, notify=changed)

    def _get_hotkey(self) -> str:
        return self._hotkey

    def _set_hotkey(self, value: str) -> None:
        self._hotkey = value
        self.changed.emit()

    hotkey = pyqtProperty(str, _get_hotkey, _set_hotkey, notify=changed)

    def _get_hotkeyMode(self) -> str:
        return self._hotkeyMode

    def _set_hotkeyMode(self, value: str) -> None:
        self._hotkeyMode = value
        self.changed.emit()

    hotkeyMode = pyqtProperty(str, _get_hotkeyMode, _set_hotkeyMode, notify=changed)

    def _get_captureState(self) -> str:
        return self._captureState

    def _set_captureState(self, value: str) -> None:
        self._captureState = value
        self.changed.emit()

    captureState = pyqtProperty(str, _get_captureState, _set_captureState, notify=changed)

    def _get_captureMessage(self) -> str:
        return self._captureMessage

    def _set_captureMessage(self, value: str) -> None:
        self._captureMessage = value
        self.changed.emit()

    captureMessage = pyqtProperty(str, _get_captureMessage, _set_captureMessage, notify=changed)

    def _get_pendingCombo(self) -> str:
        return self._pendingCombo

    def _set_pendingCombo(self, value: str) -> None:
        self._pendingCombo = value
        self.changed.emit()

    pendingCombo = pyqtProperty(str, _get_pendingCombo, _set_pendingCombo, notify=changed)

    def _get_freeCandidates(self) -> list[str]:
        return self._freeCandidates

    def _set_freeCandidates(self, value: list[str]) -> None:
        self._freeCandidates = value
        self.changed.emit()

    freeCandidates = pyqtProperty(
        "QStringList", _get_freeCandidates, _set_freeCandidates, notify=changed
    )

    def _get_devices(self) -> list[dict[str, str]]:
        return self._devices

    def _set_devices(self, value: list[dict[str, str]]) -> None:
        self._devices = value
        self.changed.emit()

    devices = pyqtProperty("QVariantList", _get_devices, _set_devices, notify=changed)

    def _get_device(self) -> str:
        return self._device

    def _set_device(self, value: str) -> None:
        self._device = value
        self.changed.emit()

    device = pyqtProperty(str, _get_device, _set_device, notify=changed)

    @pyqtProperty(str, notify=deviceResolvedChanged)
    def deviceResolved(self) -> str:  # noqa: N802 — имя свойства для QML
        return self._deviceResolved

    def _get_level(self) -> float:
        return self._level

    def _set_level(self, value: float) -> None:
        self._level = value
        self.changed.emit()

    level = pyqtProperty(float, _get_level, _set_level, notify=changed)

    def _get_peak(self) -> str:
        return self._peak

    def _set_peak(self, value: str) -> None:
        self._peak = value
        self.changed.emit()

    peak = pyqtProperty(str, _get_peak, _set_peak, notify=changed)

    def _get_testDuration(self) -> str:
        return self._testDuration

    def _set_testDuration(self, value: str) -> None:
        self._testDuration = value
        self.changed.emit()

    testDuration = pyqtProperty(str, _get_testDuration, _set_testDuration, notify=changed)

    def _get_testPhrase(self) -> str:
        return self._testPhrase

    def _set_testPhrase(self, value: str) -> None:
        self._testPhrase = value
        self.changed.emit()

    testPhrase = pyqtProperty(str, _get_testPhrase, _set_testPhrase, notify=changed)

    def _get_testText(self) -> str:
        return self._testText

    def _set_testText(self, value: str) -> None:
        self._testText = value
        self.changed.emit()

    testText = pyqtProperty(str, _get_testText, _set_testText, notify=changed)

    def _get_testState(self) -> str:
        return self._testState

    def _set_testState(self, value: str) -> None:
        self._testState = value
        self.changed.emit()

    testState = pyqtProperty(str, _get_testState, _set_testState, notify=changed)

    def _get_testMessage(self) -> str:
        return self._testMessage

    def _set_testMessage(self, value: str) -> None:
        self._testMessage = value
        self.changed.emit()

    testMessage = pyqtProperty(str, _get_testMessage, _set_testMessage, notify=changed)

    def _get_canFinish(self) -> bool:
        return self._canFinish

    def _set_canFinish(self, value: bool) -> None:
        self._canFinish = value
        self.changed.emit()

    canFinish = pyqtProperty(bool, _get_canFinish, _set_canFinish, notify=changed)

    def _get_language(self) -> str:
        return self._language

    def _set_language(self, value: str) -> None:
        self._language = value
        self.changed.emit()

    language = pyqtProperty(str, _get_language, _set_language, notify=changed)

    def _get_checkAppUpdates(self) -> bool:
        return self._checkAppUpdates

    def _set_checkAppUpdates(self, value: bool) -> None:
        self._checkAppUpdates = value
        self.changed.emit()

    checkAppUpdates = pyqtProperty(bool, _get_checkAppUpdates, _set_checkAppUpdates, notify=changed)

    def _get_checkModelUpdates(self) -> bool:
        return self._checkModelUpdates

    def _set_checkModelUpdates(self, value: bool) -> None:
        self._checkModelUpdates = value
        self.changed.emit()

    checkModelUpdates = pyqtProperty(
        bool, _get_checkModelUpdates, _set_checkModelUpdates, notify=changed
    )

    @pyqtSlot()
    def next(self) -> None:
        self.calls.append("next")

    @pyqtSlot()
    def back(self) -> None:
        self.calls.append("back")

    @pyqtSlot()
    def skip(self) -> None:
        self.calls.append("skip")

    @pyqtSlot()
    def download(self) -> None:
        self.calls.append("download")

    @pyqtSlot()
    def cancelDownload(self) -> None:
        self.calls.append("cancelDownload")

    @pyqtSlot(str)
    def installFromPath(self, path: str) -> None:
        self.calls.append("installFromPath")

    @pyqtSlot()
    def pickInstallPath(self) -> None:
        self.calls.append("pickInstallPath")

    @pyqtSlot()
    def beginCapture(self) -> None:
        self.calls.append("beginCapture")

    @pyqtSlot(str)
    def endCapture(self, combo: str) -> None:
        self.calls.append("endCapture")

    @pyqtSlot()
    def cancelCapture(self) -> None:
        self.calls.append("cancelCapture")

    @pyqtSlot()
    def keepCombo(self) -> None:
        self.calls.append("keepCombo")

    @pyqtSlot()
    def refreshCandidates(self) -> None:
        self.calls.append("refreshCandidates")

    @pyqtSlot()
    def startTest(self) -> None:
        self.calls.append("startTest")

    @pyqtSlot()
    def stopTest(self) -> None:
        self.calls.append("stopTest")

    @pyqtSlot()
    def finish(self) -> None:
        self.calls.append("finish")


class FakeSettings(QObject):
    """Изменяемый контракт настроек без системных побочных действий."""

    changed = pyqtSignal()

    def __init__(self) -> None:
        super().__init__()
        self._hotkey: str = "Ctrl + Space"
        self._hotkeyMode: str = "ptt"
        self._pillEnabled: bool = True
        self._language: str = "ru"
        self._checkAppUpdates: bool = False
        self._checkModelUpdates: bool = False
        self._autostart: bool = True
        self._device: str = "Системный по умолчанию"
        self._hotkeyStatus: str = "ok"
        self._lockedSettings: list[str] = []
        self._saveError: str = ""
        self._modelSelfcheck: str = "idle"

    def _get_hotkey(self) -> str:
        return self._hotkey

    def _set_hotkey(self, value: str) -> None:
        self._hotkey = value
        self.changed.emit()

    hotkey = pyqtProperty(str, _get_hotkey, _set_hotkey, notify=changed)

    def _get_hotkeyMode(self) -> str:
        return self._hotkeyMode

    def _set_hotkeyMode(self, value: str) -> None:
        self._hotkeyMode = value
        self.changed.emit()

    hotkeyMode = pyqtProperty(str, _get_hotkeyMode, _set_hotkeyMode, notify=changed)

    def _get_pillEnabled(self) -> bool:
        return self._pillEnabled

    def _set_pillEnabled(self, value: bool) -> None:
        self._pillEnabled = value
        self.changed.emit()

    pillEnabled = pyqtProperty(bool, _get_pillEnabled, _set_pillEnabled, notify=changed)

    def _get_language(self) -> str:
        return self._language

    def _set_language(self, value: str) -> None:
        self._language = value
        self.changed.emit()

    language = pyqtProperty(str, _get_language, _set_language, notify=changed)

    def _get_checkAppUpdates(self) -> bool:
        return self._checkAppUpdates

    def _set_checkAppUpdates(self, value: bool) -> None:
        self._checkAppUpdates = value
        self.changed.emit()

    checkAppUpdates = pyqtProperty(bool, _get_checkAppUpdates, _set_checkAppUpdates, notify=changed)

    def _get_checkModelUpdates(self) -> bool:
        return self._checkModelUpdates

    def _set_checkModelUpdates(self, value: bool) -> None:
        self._checkModelUpdates = value
        self.changed.emit()

    checkModelUpdates = pyqtProperty(
        bool, _get_checkModelUpdates, _set_checkModelUpdates, notify=changed
    )

    def _get_autostart(self) -> bool:
        return self._autostart

    def _set_autostart(self, value: bool) -> None:
        self._autostart = value
        self.changed.emit()

    autostart = pyqtProperty(bool, _get_autostart, _set_autostart, notify=changed)

    def _get_device(self) -> str:
        return self._device

    def _set_device(self, value: str) -> None:
        self._device = value
        self.changed.emit()

    device = pyqtProperty(str, _get_device, _set_device, notify=changed)

    def _get_hotkeyStatus(self) -> str:
        return self._hotkeyStatus

    def _set_hotkeyStatus(self, value: str) -> None:
        self._hotkeyStatus = value
        self.changed.emit()

    hotkeyStatus = pyqtProperty(str, _get_hotkeyStatus, _set_hotkeyStatus, notify=changed)

    def _get_lockedSettings(self) -> list[str]:
        return self._lockedSettings

    def _set_lockedSettings(self, value: list[str]) -> None:
        self._lockedSettings = value
        self.changed.emit()

    lockedSettings = pyqtProperty(
        "QStringList", _get_lockedSettings, _set_lockedSettings, notify=changed
    )

    def _get_saveError(self) -> str:
        return self._saveError

    saveError = pyqtProperty(str, _get_saveError, notify=changed)

    def _get_modelSelfcheck(self) -> str:
        return self._modelSelfcheck

    modelSelfcheck = pyqtProperty(str, _get_modelSelfcheck, notify=changed)

    @pyqtSlot(str, result=bool)
    def is_locked(self, name: str) -> bool:
        return False

    @pyqtSlot()
    def retryHotkey(self) -> None:
        pass


@pytest.fixture(scope="session")
def onboarding_app() -> Any:
    return get_qapplication()


def visual_tree(root: Any) -> Iterator[Any]:
    """childItems включает делегаты Repeater, отсутствующие в QObject.children()."""
    yield root
    for child in root.childItems():
        yield from visual_tree(child)


def snapshot_name(step: int, dark: bool) -> str:
    return f"{STEP_NAMES[step]}-{'dark' if dark else 'light'}.png"


def assert_frame(image: QImage) -> None:
    assert not image.isNull(), "пустой снимок"
    dpr = image.devicePixelRatio()
    expected = (round(WIDTH * dpr), round(HEIGHT * dpr))
    assert (image.width(), image.height()) == expected, (
        f"размер {(image.width(), image.height())}, ожидался {expected}, DPR={dpr}"
    )
    first = image.pixel(0, 0)
    assert any(
        image.pixel(x, y) != first for y in range(image.height()) for x in range(image.width())
    ), "снимок залит одним цветом"


def grab_frame(app: Any, window: QQuickWindow, case: str) -> QImage:
    """Общий захват QQuickView и ApplicationWindow с повторами для Qt5/offscreen."""
    grab = None
    for attempt in range(1, 21):
        if attempt > 1:
            QTest.qWait(25)
            app.processEvents()
        image = window.grabWindow()
        if image.isNull():
            # Qt5/offscreen может не реализовывать grabWindow даже после show.
            if grab is None:
                grab = window.contentItem().grabToImage()
            if grab is not None:
                image = grab.image()
                if not image.isNull():
                    grab = None
        if not image.isNull():
            dpr = image.devicePixelRatio()
            if (image.width(), image.height()) == (
                round(WIDTH * dpr),
                round(HEIGHT * dpr),
            ):
                break
    else:
        pytest.fail(
            f"{case}: снимок не готов после {attempt} попыток; "
            f"размер {(image.width(), image.height())}, DPR={image.devicePixelRatio()}"
        )
    assert_frame(image)
    return image


def render_onboarding(
    app: Any,
    fake: FakeOnboarding,
    dark: bool,
    *,
    extra_wait_ms: int = 0,
    inspect: Callable[[Any], None] | None = None,
) -> tuple[QImage, list[str]]:
    """Снимает настоящее окно, перехватывая Qt вплоть до удаления окна и мостов."""
    messages: list[str] = []

    def handler(_mode: Any, _context: Any, message: str) -> None:
        messages.append(message)

    previous = qInstallMessageHandler(handler)
    view = QQuickView()
    theme = FakeTheme(dark)
    try:
        view.rootContext().setContextProperty("onboarding", fake)
        view.rootContext().setContextProperty("themeSource", theme)
        view.setResizeMode(QQuickView.SizeRootObjectToView)
        view.resize(WIDTH, HEIGHT)
        view.setSource(QUrl.fromLocalFile(str(REPO / "qml/onboarding/Onboarding.qml")))
        assert view.status() == QQuickView.Ready, [error.toString() for error in view.errors()]
        root = view.rootObject()
        assert root is not None
        view.show()
        QTest.qWait(180 + extra_wait_ms)
        app.processEvents()
        assert view.status() == QQuickView.Ready, [error.toString() for error in view.errors()]
        assert root.property("step") == fake.step
        assert any(
            item.isVisible() and item.property("text") == f"Шаг {fake.step} из 5"
            for item in visual_tree(root)
        ), f"не показан шаг {fake.step}"
        assert view.isVisible() and view.isExposed()

        image = grab_frame(app, view, snapshot_name(fake.step, dark))
        if inspect is not None:
            inspect(root)
            app.processEvents()
        return image, messages
    finally:
        # Контекстные объекты живут дольше движка; сообщения деструкторов тоже учитываются.
        try:
            sip.delete(view)
            sip.delete(theme)
            app.processEvents()
        finally:
            qInstallMessageHandler(previous)


def render_settings(app: Any, dark: bool, *, extra_wait_ms: int = 0) -> tuple[QImage, list[str]]:
    """Загружает раздел «Общие» настоящего Main.qml без appInfo."""
    messages: list[str] = []

    def handler(_mode: Any, _context: Any, message: str) -> None:
        messages.append(message)

    previous = qInstallMessageHandler(handler)
    engine = QQmlApplicationEngine()
    theme = FakeTheme(dark)
    settings = FakeSettings()
    try:
        engine.rootContext().setContextProperty("themeSource", theme)
        engine.rootContext().setContextProperty("showOnboarding", False)
        engine.rootContext().setContextProperty("settingsBridge", settings)
        engine.load(QUrl.fromLocalFile(str(REPO / "qml/Main.qml")))
        roots = engine.rootObjects()
        assert len(roots) == 1, messages
        window = roots[0]
        assert isinstance(window, QQuickWindow)
        window.setWidth(WIDTH)
        window.setHeight(HEIGHT)
        # Qt5/software grabToImage не включает цвет очистки QQuickWindow.
        # Повторяем его под содержимым, чтобы запасной захват сохранил фон окна.
        background_component = QQmlComponent(engine)
        background_component.setData(
            b"import QtQuick 2.15; Rectangle { anchors.fill: parent; z: -1 }", QUrl()
        )
        background = background_component.create()
        assert background is not None, [error.toString() for error in background_component.errors()]
        background.setProperty("color", window.color())
        background.setParent(window.contentItem())
        background.setParentItem(window.contentItem())
        window.show()
        QTest.qWait(180 + extra_wait_ms)
        app.processEvents()
        assert window.isVisible() and window.isExposed()
        assert (window.width(), window.height()) == (WIDTH, HEIGHT)
        assert window.property("onboardingVisible") is False
        assert any(
            item.isVisible() and item.property("text") == "Диктовка, индикация и запуск"
            for item in visual_tree(window.contentItem())
        ), "не показан раздел «Общие»"
        image = grab_frame(app, window, snapshot_name(6, dark))
        return image, messages
    finally:
        # Движок владеет окном; контекстные объекты удаляются после него.
        try:
            sip.delete(engine)
            sip.delete(settings)
            sip.delete(theme)
            app.processEvents()
        finally:
            qInstallMessageHandler(previous)


@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
def test_show_section_only_navigates_to_available_pages(onboarding_app: Any, dark: bool) -> None:
    messages: list[str] = []

    def handler(_mode: Any, _context: Any, message: str) -> None:
        messages.append(message)

    previous = qInstallMessageHandler(handler)
    engine = QQmlApplicationEngine()
    app_info = FakeAppInfo()
    theme = FakeTheme(dark)
    try:
        engine.rootContext().setContextProperty("appInfo", app_info)
        engine.rootContext().setContextProperty("themeSource", theme)
        engine.rootContext().setContextProperty("showOnboarding", False)
        engine.load(QUrl.fromLocalFile(str(REPO / "qml/Main.qml")))
        roots = engine.rootObjects()
        assert len(roots) == 1, messages
        window = roots[0]
        assert isinstance(window, QQuickWindow)
        sidebar = next(
            item
            for item in visual_tree(window.contentItem())
            if item.metaObject().indexOfProperty("debugCurrent") >= 0
        )
        sections = sidebar.property("sections").toVariant()
        assert [section["key"] for section in sections] == [
            "general",
            "models",
            "output",
            "network",
            "advanced",
            "about",
        ]
        assert window.property("sectionIndices").toVariant() == {
            **{section["key"]: index for index, section in enumerate(sections)},
            "debug": len(sections),
        }

        def visible_texts() -> set[str]:
            return {
                item.property("text")
                for item in visual_tree(window.contentItem())
                if item.isVisible() and isinstance(item.property("text"), str)
            }

        def show_section(section: str, expected_index: int) -> None:
            window.hide()
            assert not window.isVisible()
            app_info.showSection.emit(section)
            onboarding_app.processEvents()
            assert window.isVisible()
            assert sidebar.property("currentIndex") == expected_index

        assert sidebar.property("currentIndex") == 0
        unavailable = ("models", "output", "network", "advanced", "about", "unknown")
        for section in unavailable:
            show_section(section, 0)
            assert "Диктовка, индикация и запуск" in visible_texts()

        show_section("debug", len(sections))
        assert sidebar.property("debugCurrent") is True
        assert {
            "Отладка",
            "Скрытый раздел: Ctrl + Shift + D",
            "Здесь будут сведения для поддержки",
        } <= visible_texts()
        assert "Диктовка, индикация и запуск" not in visible_texts()
        assert "Горячая клавиша" not in visible_texts()
        for section in unavailable:
            show_section(section, len(sections))
            assert "Здесь будут сведения для поддержки" in visible_texts()

        show_section("general", 0)
        assert {"Общие", "Диктовка, индикация и запуск", "Горячая клавиша"} <= visible_texts()
        assert "Здесь будут сведения для поддержки" not in visible_texts()
    finally:
        try:
            sip.delete(engine)
            sip.delete(app_info)
            sip.delete(theme)
            onboarding_app.processEvents()
        finally:
            qInstallMessageHandler(previous)

    # Qt/offscreen не реализует raise(); все остальные сообщения остаются ошибками.
    assert_no_messages(
        [message for message in messages if message != "This plugin does not support raise()"],
        "showSection",
    )


def render_case(
    app: Any, step: int, dark: bool, *, extra_wait_ms: int = 0
) -> tuple[QImage, list[str]]:
    if step == 6:
        return render_settings(app, dark, extra_wait_ms=extra_wait_ms)
    fake = FakeOnboarding()
    fake.step = step
    return render_onboarding(app, fake, dark, extra_wait_ms=extra_wait_ms)


def assert_no_messages(messages: list[str], case: str) -> None:
    assert not messages, f"{case}: сообщения Qt\n" + "\n".join(messages)


def png_bytes(image: QImage) -> bytes:
    """Кодирует второй рендер в памяти, не публикуя и не записывая его на диск."""
    data = QByteArray()
    buffer = QBuffer(data)
    assert buffer.open(QIODevice.WriteOnly)
    try:
        assert image.save(buffer, "PNG"), "не удалось закодировать PNG"
    finally:
        buffer.close()
    return bytes(data)


def assert_saved_snapshot(path: Path, original: QImage) -> None:
    assert path.is_file(), f"отсутствует PNG: {path}"
    image = QImage(str(path))
    assert not image.isNull(), f"повреждён PNG: {path}"
    # PNG может не сохранять DPR; сравниваем физические размеры с исходным кадром.
    assert (image.width(), image.height()) == (original.width(), original.height()), (
        f"{path}: неверный размер PNG"
    )


@pytest.fixture(scope="module", autouse=True)
def rendered_steps(
    onboarding_app: Any, request: pytest.FixtureRequest
) -> Iterator[dict[tuple[int, bool], tuple[QImage, list[str], Path]]]:
    """Готовит двенадцать кадров; публикует их только после успеха всех тестов модуля."""
    failures_before = request.session.testsfailed
    SNAPSHOTS.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".onboarding-", dir=SNAPSHOTS) as temporary:
        staging = Path(temporary)
        rendered: dict[tuple[int, bool], tuple[QImage, list[str], Path]] = {}
        for step, dark in CASES:
            image, messages = render_case(onboarding_app, step, dark)
            name = snapshot_name(step, dark)
            assert_no_messages(messages, name)
            path = staging / name
            assert image.save(str(path), "PNG"), f"не удалось сохранить {path}"
            assert_saved_snapshot(path, image)
            rendered[step, dark] = image, messages, path
        yield rendered
        if request.session.testsfailed == failures_before:
            for image, messages, path in rendered.values():
                assert_no_messages(messages, path.name)
                assert_saved_snapshot(path, image)
            for _, _, path in rendered.values():
                os.replace(path, SNAPSHOTS / path.name)
            for image, _, path in rendered.values():
                assert_saved_snapshot(SNAPSHOTS / path.name, image)
            for step in range(1, 6):
                for theme in ("light", "dark"):
                    (SNAPSHOTS / f"step{step}-{theme}.png").unlink(missing_ok=True)


@pytest.mark.parametrize("step", STEP_NAMES, ids=STEP_NAMES.values())
@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
def test_steps_render_without_warnings(
    step: int,
    dark: bool,
    rendered_steps: dict[tuple[int, bool], tuple[QImage, list[str], Path]],
) -> None:
    image, messages, _ = rendered_steps[step, dark]
    assert_no_messages(messages, snapshot_name(step, dark))
    assert_frame(image)


def test_step_snapshots_are_saved(
    rendered_steps: dict[tuple[int, bool], tuple[QImage, list[str], Path]],
) -> None:
    """Проверяет подготовленные PNG; атомарная публикация отложена до teardown."""
    assert set(rendered_steps) == set(CASES)
    expected = {snapshot_name(step, dark) for step, dark in CASES}
    assert len(expected) == 12
    paths = [path for _, _, path in rendered_steps.values()]
    assert {path.name for path in paths[0].parent.glob("*.png")} == expected
    for image, messages, path in rendered_steps.values():
        assert_no_messages(messages, path.name)
        assert_saved_snapshot(path, image)


@pytest.mark.parametrize("step", STEP_NAMES, ids=STEP_NAMES.values())
@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
def test_snapshots_are_deterministic(
    step: int,
    dark: bool,
    onboarding_app: Any,
    rendered_steps: dict[tuple[int, bool], tuple[QImage, list[str], Path]],
) -> None:
    _, _, first = rendered_steps[step, dark]
    second, messages = render_case(onboarding_app, step, dark, extra_wait_ms=250)
    assert_no_messages(messages, first.name)
    first_hash = sha256(first.read_bytes()).hexdigest()
    second_hash = sha256(png_bytes(second)).hexdigest()
    assert first_hash == second_hash, (
        f"{first.name}: PNG зависит от времени съёмки: {first_hash} != {second_hash}"
    )


@pytest.mark.parametrize("state", MODEL_STATES)
def test_model_card_states_render(state: str, onboarding_app: Any) -> None:
    fake = FakeOnboarding()
    fake.step = 2
    fake.modelState = state
    _, messages = render_onboarding(onboarding_app, fake, False)
    assert_no_messages(messages, f"modelState={state}")


@pytest.mark.parametrize("state", CAPTURE_STATES)
def test_capture_states_render(state: str, onboarding_app: Any) -> None:
    fake = FakeOnboarding()
    fake.step = 3
    fake.captureState = state
    _, messages = render_onboarding(onboarding_app, fake, False)
    assert_no_messages(messages, f"captureState={state}")


@pytest.mark.parametrize(
    ("key", "modifiers", "accepted_modifiers"),
    [
        (Qt.Key_Space, Qt.NoModifier, Qt.ControlModifier),
        (Qt.Key_A, Qt.NoModifier, Qt.ControlModifier),
        (Qt.Key_5, Qt.NoModifier, Qt.ControlModifier),
        (Qt.Key_F5, Qt.NoModifier, Qt.ControlModifier),
        (Qt.Key_A, Qt.ShiftModifier, Qt.ControlModifier | Qt.ShiftModifier),
        (Qt.Key_Space, Qt.ShiftModifier, Qt.AltModifier),
        (Qt.Key_F5, Qt.ShiftModifier, Qt.MetaModifier),
    ],
    ids=["space", "letter", "digit", "f5", "shift-letter", "shift-space", "shift-f5"],
)
def test_capture_requires_ctrl_alt_or_win(
    key: Any, modifiers: Any, accepted_modifiers: Any, onboarding_app: Any
) -> None:
    fake = FakeOnboarding()
    fake.step = 3
    fake.captureState = "capturing"

    def inspect(root: Any) -> None:
        view = root.window()
        assert isinstance(view, QQuickView)
        capture = next(item for item in visual_tree(root) if item.property("captureActive"))
        assert capture.hasActiveFocus()
        QTest.keyClick(view, key, modifiers)
        onboarding_app.processEvents()
        assert "endCapture" not in fake.calls
        assert fake.captureState == capture.property("state7") == "capturing"
        hint = next(
            item
            for item in visual_tree(root)
            if item.property("text") == "Удерживайте Ctrl, Alt или Win и нажмите клавишу"
        )
        assert hint.isVisible() and hint.height() > 0

        QTest.keyClick(view, key, accepted_modifiers)
        onboarding_app.processEvents()
        assert fake.calls == ["endCapture"]
        assert hint.property("text") == ""
        assert not hint.isVisible() and hint.height() == 0

    _, messages = render_onboarding(onboarding_app, fake, False, inspect=inspect)
    assert_no_messages(messages, "capture requires Ctrl/Alt/Win")


def test_capture_ignores_other_keys_and_clears_hint_on_exit(onboarding_app: Any) -> None:
    fake = FakeOnboarding()
    fake.step = 3
    fake.captureState = "capturing"

    def inspect(root: Any) -> None:
        view = root.window()
        assert isinstance(view, QQuickView)
        capture = next(item for item in visual_tree(root) if item.property("captureActive"))
        assert capture.hasActiveFocus()
        for key in (Qt.Key_Control, Qt.Key_Shift, Qt.Key_Alt, Qt.Key_Meta, Qt.Key_Comma):
            QTest.keyClick(view, key, Qt.NoModifier)
            onboarding_app.processEvents()
            assert fake.calls == []
            assert capture.property("captureHint") == ""
            assert capture.property("state7") == "capturing"

        QTest.keyClick(view, Qt.Key_Space, Qt.NoModifier)
        assert capture.property("captureHint") != ""
        assert fake.calls == []
        QTest.keyClick(view, Qt.Key_Escape, Qt.NoModifier)
        assert fake.calls == ["cancelCapture"]
        # Фейк только записывает вызов; переход состояния задаём как ответ моста.
        fake.captureState = "idle"
        onboarding_app.processEvents()
        assert capture.property("captureActive") is False
        assert capture.property("captureHint") == ""
        fake.captureState = "capturing"
        onboarding_app.processEvents()
        assert capture.property("captureActive") is True
        assert capture.property("captureHint") == ""

    _, messages = render_onboarding(onboarding_app, fake, False, inspect=inspect)
    assert_no_messages(messages, "capture ignored keys and exit")


def test_policy_locked_step1(onboarding_app: Any) -> None:
    fake = FakeOnboarding()
    fake.policyLocked = True

    def inspect(root: Any) -> None:
        assert any(
            item.isVisible() and item.property("text") == "Задано администратором"
            for item in visual_tree(root)
        ), "нет видимой подписи «Задано администратором»"

    _, messages = render_onboarding(onboarding_app, fake, False, inspect=inspect)
    assert_no_messages(messages, "policyLocked=True")


@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
def test_mic_test_button_starts_and_stops_including_processing(
    onboarding_app: Any, dark: bool
) -> None:
    fake = FakeOnboarding()
    fake.step = 4

    def inspect(root: Any) -> None:
        for state in ("idle", "recording", "processing", "done", "error"):
            fake.testState = state
            onboarding_app.processEvents()
            testing = state in ("recording", "processing")
            text = "Остановить" if testing else "Тестовая диктовка"
            buttons = [
                item
                for item in visual_tree(root)
                if item.isVisible()
                and item.property("text") == text
                and item.metaObject().indexOfSignal(b"clicked()") >= 0
            ]
            assert len(buttons) == 1, state
            assert buttons[0].isEnabled(), state
            fake.calls.clear()
            QMetaObject.invokeMethod(buttons[0], "clicked", Qt.DirectConnection)
            assert fake.calls == ["stopTest" if testing else "startTest"], state

    _, messages = render_onboarding(onboarding_app, fake, dark, inspect=inspect)
    assert_no_messages(messages, "microphone test button")


@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
def test_mic_card_with_resolved_default_device_is_57px(onboarding_app: Any, dark: bool) -> None:
    fake = FakeOnboarding()
    fake.step = 4

    def inspect(root: Any) -> None:
        row = next(item for item in visual_tree(root) if item.property("label") == "Микрофон")
        assert row.property("sub") == "Встроенный микрофон"
        assert row.height() == 55
        assert row.parentItem().parentItem().height() == 57

    _, messages = render_onboarding(onboarding_app, fake, dark, inspect=inspect)
    assert_no_messages(messages, "resolved default microphone card height")


@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
def test_mic_explanation_only_shows_resolved_default_device(
    onboarding_app: Any, dark: bool
) -> None:
    fake = FakeOnboarding()
    fake.step = 4
    builtin = "Встроенный микрофон"
    system_default = "Системный по умолчанию"

    def inspect(root: Any) -> None:
        row = next(item for item in visual_tree(root) if item.property("label") == "Микрофон")
        selector = next(
            item
            for item in visual_tree(row)
            if item.metaObject().indexOfProperty("currentText") >= 0
        )
        for device, resolved, selected, explanation in (
            ("", builtin, system_default, builtin),
            ("builtin", builtin, builtin, ""),
            ("", "", system_default, ""),
            ("", builtin, system_default, builtin),
        ):
            fake.device = device
            fake._deviceResolved = resolved
            fake.deviceResolvedChanged.emit()
            onboarding_app.processEvents()
            assert row.property("sub") == explanation
            assert selector.property("currentText") == selected
            texts = [
                item.property("text")
                for item in visual_tree(root)
                if item.isVisible() and isinstance(item.property("text"), str)
            ]
            assert texts.count(system_default) == int(device == "")
            assert texts.count(builtin) == int(bool(explanation) or device == "builtin")
            assert any(
                item.isVisible() and item.property("text") == selected
                for item in visual_tree(selector)
            )

    _, messages = render_onboarding(onboarding_app, fake, dark, inspect=inspect)
    assert_no_messages(messages, "resolved microphone explanation")


@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
@pytest.mark.parametrize("default_index", [0, 1, None], ids=["first", "second", "absent"])
def test_mic_missing_device_displays_fallback_without_writing_bridge(
    onboarding_app: Any, dark: bool, default_index: int | None
) -> None:
    fake = FakeOnboarding()
    fake.step = 4
    devices = [{"id": "builtin", "name": "Встроенный микрофон"}]
    if default_index is not None:
        devices.insert(default_index, {"id": "", "name": "Системный по умолчанию"})
    fake.devices = devices
    fake.device = "ghost-id"
    explanation = "Выбранный раньше микрофон не найден — включён системный по умолчанию"

    def inspect(root: Any) -> None:
        selector = next(
            item
            for item in visual_tree(root)
            if item.isVisible() and item.metaObject().indexOfProperty("currentText") >= 0
        )
        expected_index = default_index if default_index is not None else 0
        assert selector.property("currentIndex") == expected_index
        assert selector.property("currentText") == devices[expected_index]["name"]
        assert any(
            item.isVisible() and item.property("text") == devices[expected_index]["name"]
            for item in visual_tree(selector)
        )
        assert any(
            item.isVisible() and item.property("text") == explanation for item in visual_tree(root)
        )
        assert fake.device == "ghost-id"
        assert fake.calls == []
        row = next(item for item in visual_tree(root) if item.property("label") == "Микрофон")
        assert row.property("sub") == ""

        fake.device = "builtin"
        onboarding_app.processEvents()
        assert selector.property("currentText") == "Встроенный микрофон"
        assert not any(
            item.isVisible() and item.property("text") == explanation for item in visual_tree(root)
        )
        QTest.qWait(25)  # Дожидаемся кадра с обновлённой геометрией Column.
        assert row.property("sub") == ""
        assert row.parentItem().parentItem().height() == 50

    _, messages = render_onboarding(onboarding_app, fake, dark, inspect=inspect)
    assert_no_messages(messages, "missing microphone")


@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
def test_mic_silence_is_only_reported_while_recording(onboarding_app: Any, dark: bool) -> None:
    fake = FakeOnboarding()
    fake.step = 4
    fake.level = 0
    fake.testText = ""
    prompt = "Нажмите «Тестовая диктовка», чтобы проверить микрофон"
    silence = "Звука с этого микрофона пока нет"
    warning = "Микрофон молчит"

    def inspect(root: Any) -> None:
        for state, level in (
            ("idle", 0),
            ("recording", 0),
            ("recording", 0.5),
            ("processing", 0),
            ("done", 0),
            ("error", 0),
            ("idle", 0),
        ):
            fake.testState = state
            fake.level = level
            onboarding_app.processEvents()
            texts = {
                item.property("text")
                for item in visual_tree(root)
                if item.isVisible() and isinstance(item.property("text"), str)
            }
            assert (prompt in texts) == (state == "idle"), (state, level)
            silent_recording = state == "recording" and level == 0
            assert (silence in texts) == silent_recording, (state, level)
            assert (warning in texts) == silent_recording, (state, level)

    _, messages = render_onboarding(onboarding_app, fake, dark, inspect=inspect)
    assert_no_messages(messages, "microphone silence")


def test_finish_calls_bridge(onboarding_app: Any) -> None:
    """Вызывает настоящий сигнал clicked() кнопки «Готово» через метаобъект Qt5."""
    fake = FakeOnboarding()
    fake.step = 5
    fake.canFinish = True

    def inspect(root: Any) -> None:
        buttons = [
            item
            for item in visual_tree(root)
            if item.property("text") == "Готово"
            and item.metaObject().indexOfSignal(b"clicked()") >= 0
            and item.isVisible()
        ]
        assert len(buttons) == 1, f"ожидалась одна кнопка «Готово», найдено {len(buttons)}"
        assert buttons[0].isEnabled()
        assert fake.calls == []
        QMetaObject.invokeMethod(buttons[0], "clicked", Qt.DirectConnection)
        assert fake.calls == ["finish"]

    _, messages = render_onboarding(onboarding_app, fake, False, inspect=inspect)
    assert_no_messages(messages, "finish")
