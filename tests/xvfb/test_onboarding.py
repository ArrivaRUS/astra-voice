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
SNAPSHOTS = REPO / "design/refs/impl/onboarding"
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


class FakeOnboarding(QObject):
    """Полный изменяемый контракт; слоты записывают вызовы без побочных действий."""

    changed = pyqtSignal()

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[str] = []
        self._step: int = 1
        self._policyLocked: bool = False
        self._modelState: str = "downloadable"
        self._modelName: str = "GigaAM v3 RNN-T"
        self._modelSize: str = "231,9 МБ"
        self._modelHost: str = "huggingface.co"
        self._progress: float = 0.43
        self._speed: str = "5,2 МБ/с"
        self._eta: str = "~25 с"
        self._hotkey: str = "Ctrl + Space"
        self._hotkeyMode: str = "ptt"
        self._captureState: str = "idle"
        self._devices: list[str] = ["Системный по умолчанию"]
        self._device: str = "Системный по умолчанию"
        self._level: float = 0.6
        self._testText: str = "Проверка связи, раз, два, три."
        self._testState: str = "done"
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

    def _get_modelHost(self) -> str:
        return self._modelHost

    def _set_modelHost(self, value: str) -> None:
        self._modelHost = value
        self.changed.emit()

    modelHost = pyqtProperty(str, _get_modelHost, _set_modelHost, notify=changed)

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

    def _get_devices(self) -> list[str]:
        return self._devices

    def _set_devices(self, value: list[str]) -> None:
        self._devices = value
        self.changed.emit()

    devices = pyqtProperty(list, _get_devices, _set_devices, notify=changed)

    def _get_device(self) -> str:
        return self._device

    def _set_device(self, value: str) -> None:
        self._device = value
        self.changed.emit()

    device = pyqtProperty(str, _get_device, _set_device, notify=changed)

    def _get_level(self) -> float:
        return self._level

    def _set_level(self, value: float) -> None:
        self._level = value
        self.changed.emit()

    level = pyqtProperty(float, _get_level, _set_level, notify=changed)

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

    @pyqtSlot()
    def endCapture(self) -> None:
        self.calls.append("endCapture")

    @pyqtSlot()
    def testPhrase(self) -> None:
        self.calls.append("testPhrase")

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
