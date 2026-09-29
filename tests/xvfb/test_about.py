"""M9-а v0.2: раздел «О программе» в двух темах (PRD F13, референс 06-about*.png).

Каталог снимков задаётся ASTRA_VOICE_SNAPSHOT_DIR_ABOUT или по умолчанию
design/refs/impl/about. Снимки сверяются глазами с design/refs/06-about*.png;
сравнение по пикселям не делается. Два кадра на состояние: окно обычного
размера (верх раздела, как в макете) и высокое окно — раздел целиком.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from PyQt5 import sip
from PyQt5.QtCore import QObject, QUrl, pyqtProperty, pyqtSignal, pyqtSlot, qInstallMessageHandler
from PyQt5.QtGui import QImage
from PyQt5.QtQml import QQmlApplicationEngine
from PyQt5.QtTest import QTest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from astra_voice.ui.icons import install_icon_provider

pytestmark = pytest.mark.xvfb
REPO = Path(__file__).resolve().parents[2]
# Промежуточные прогоны разработки не трогают эталоны репозитория.
SNAPSHOTS = REPO / Path(
    os.environ.get("ASTRA_VOICE_SNAPSHOT_DIR_ABOUT") or "design/refs/impl/about"
)
WIDTH, HEIGHT = 1024, 620
TALL = 1200

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


onboarding = load_module("_about_onboarding", REPO / "tests/xvfb/test_onboarding.py")
updates_states = load_module("_about_update_states", REPO / "tests/xvfb/test_update_states.py")


def _value(name: str) -> Callable[[Any], Any]:
    return lambda self: self._values[name]


class FakeAbout(QObject):
    """Контракт aboutBridge (docs/ui-bridge.md §3.9); слоты пишут вызовы."""

    infoChanged = pyqtSignal()
    statsChanged = pyqtSignal()
    updatesChanged = pyqtSignal()

    def __init__(self, **values: Any) -> None:
        super().__init__()
        self.calls: list[str] = []
        self._values: dict[str, Any] = {
            "version": "0.2.0",
            "buildDate": "01.10.2026",
            "installKind": "deb",
            "pythonVersion": "3.11.2",
            "qtVersion": "5.15.8",
            "pyqtVersion": "5.15.9",
            "onnxruntimeVersion": "1.24.4",
            "onnxAsrVersion": "0.12.0",
            "licenseAvailable": True,
            "noticeAvailable": True,
            "privacyAvailable": True,
            "settingsPath": "~/.config/astra-voice",
            "modelsPath": "~/.local/share/astra-voice/models",
            "logsPath": "~/.local/share/astra-voice/logs",
            "settingsFolderAvailable": True,
            "modelsFolderAvailable": True,
            "logsFolderAvailable": True,
            "modelsSize": "680 МБ",
            "logsSize": "2,1 МБ",
            "lastAttemptText": "сегодня в 14:05",
            "lastSuccessText": "28.09.2026 в 10:12",
            "statsAvailable": True,
            "statsCount": 148,
            "statsText": "обычно 0,38 с, в худших случаях 0,61 с · 148 диктовок",
        }
        self._values.update(values)

    version = pyqtProperty(str, _value("version"), constant=True)
    buildDate = pyqtProperty(str, _value("buildDate"), constant=True)
    installKind = pyqtProperty(str, _value("installKind"), constant=True)
    pythonVersion = pyqtProperty(str, _value("pythonVersion"), constant=True)
    qtVersion = pyqtProperty(str, _value("qtVersion"), constant=True)
    pyqtVersion = pyqtProperty(str, _value("pyqtVersion"), constant=True)
    onnxruntimeVersion = pyqtProperty(str, _value("onnxruntimeVersion"), constant=True)
    onnxAsrVersion = pyqtProperty(str, _value("onnxAsrVersion"), constant=True)
    licenseAvailable = pyqtProperty(bool, _value("licenseAvailable"), notify=infoChanged)
    noticeAvailable = pyqtProperty(bool, _value("noticeAvailable"), notify=infoChanged)
    privacyAvailable = pyqtProperty(bool, _value("privacyAvailable"), notify=infoChanged)
    settingsPath = pyqtProperty(str, _value("settingsPath"), constant=True)
    modelsPath = pyqtProperty(str, _value("modelsPath"), constant=True)
    logsPath = pyqtProperty(str, _value("logsPath"), constant=True)
    settingsFolderAvailable = pyqtProperty(
        bool, _value("settingsFolderAvailable"), notify=infoChanged
    )
    modelsFolderAvailable = pyqtProperty(bool, _value("modelsFolderAvailable"), notify=infoChanged)
    logsFolderAvailable = pyqtProperty(bool, _value("logsFolderAvailable"), notify=infoChanged)
    modelsSize = pyqtProperty(str, _value("modelsSize"), notify=infoChanged)
    logsSize = pyqtProperty(str, _value("logsSize"), notify=infoChanged)
    lastAttemptText = pyqtProperty(str, _value("lastAttemptText"), notify=updatesChanged)
    lastSuccessText = pyqtProperty(str, _value("lastSuccessText"), notify=updatesChanged)
    statsAvailable = pyqtProperty(bool, _value("statsAvailable"), constant=True)
    statsCount = pyqtProperty(int, _value("statsCount"), notify=statsChanged)
    statsText = pyqtProperty(str, _value("statsText"), notify=statsChanged)

    def _call(self, name: str) -> None:
        self.calls.append(name)

    @pyqtSlot()
    def openLicense(self) -> None:  # noqa: N802
        self._call("openLicense")

    @pyqtSlot()
    def openNotice(self) -> None:  # noqa: N802
        self._call("openNotice")

    @pyqtSlot()
    def openPrivacy(self) -> None:  # noqa: N802
        self._call("openPrivacy")

    @pyqtSlot()
    def openSettingsFolder(self) -> None:  # noqa: N802
        self._call("openSettingsFolder")

    @pyqtSlot()
    def openLogsFolder(self) -> None:  # noqa: N802
        self._call("openLogsFolder")

    @pyqtSlot()
    def clearStats(self) -> None:  # noqa: N802
        self._call("clearStats")
        self._values.update(statsCount=0, statsText="")
        self.statsChanged.emit()

    @pyqtSlot()
    def refresh(self) -> None:
        self._call("refresh")


class FakeInfo(QObject):
    """appInfo с политикой и видом сеанса — то, что читает раздел."""

    debugChanged = pyqtSignal()
    showSection = pyqtSignal(str, arguments=["section"])

    def __init__(self, session: str, policy: str) -> None:
        super().__init__()
        self._session = session
        self._policy = policy

    @pyqtProperty(str, constant=True)
    def version(self) -> str:
        return "0.2.0"

    @pyqtProperty(str, constant=True)
    def sessionKind(self) -> str:  # noqa: N802
        return self._session

    @pyqtProperty(str, constant=True)
    def policyStatus(self) -> str:  # noqa: N802
        return self._policy

    @pyqtProperty(bool, notify=debugChanged)
    def debug(self) -> bool:
        return False


# (значения aboutBridge, sessionKind, policyStatus, есть ли updatesBridge)
CASES: dict[str, tuple[dict[str, Any], str, str, bool]] = {
    "about-base": ({}, "KDE", "absent", True),
    "about-empty": (
        {
            "statsCount": 0,
            "statsText": "",
            "lastAttemptText": "",
            "lastSuccessText": "",
            "buildDate": "",
            "installKind": "source",
            "onnxruntimeVersion": "",
            "modelsSize": "",
            "logsSize": "",
            "modelsFolderAvailable": False,
            "logsFolderAvailable": False,
        },
        "FLY",
        "invalid",
        False,
    ),
}


@pytest.fixture(scope="module")
def app() -> Any:
    from helpers.qt_app import get_qapplication

    return get_qapplication()


def render_about(
    app: Any,
    dark: bool,
    about: FakeAbout | None,
    info: FakeInfo,
    *,
    height: int = HEIGHT,
    updates: Any = None,
    settings: Any = None,
    inspect: Callable[[Any], None] | None = None,
) -> tuple[QImage, set[str], list[str]]:
    messages: list[str] = []

    def handler(_mode: Any, _context: Any, message: str) -> None:
        messages.append(message)

    previous = qInstallMessageHandler(handler)
    engine = QQmlApplicationEngine()
    install_icon_provider(engine)
    theme = onboarding.FakeTheme(dark)
    own_settings = settings is None
    if settings is None:
        settings = onboarding.FakeSettings()
    try:
        context = engine.rootContext()
        context.setContextProperty("themeSource", theme)
        context.setContextProperty("showOnboarding", False)
        context.setContextProperty("settingsBridge", settings)
        context.setContextProperty("appInfo", info)
        context.setContextProperty("aboutBridge", about)
        if updates is not None:
            context.setContextProperty("updatesBridge", updates)
        engine.load(QUrl.fromLocalFile(str(REPO / "qml/Main.qml")))
        roots = engine.rootObjects()
        assert len(roots) == 1, messages
        window = roots[0]
        window.setWidth(WIDTH)
        window.setHeight(height)
        assert window.setProperty("freezeAnimations", True)
        _background = updates_states.add_background(engine, window)
        window.show()
        QTest.qWait(180)
        app.processEvents()
        onboarding.select_section(app, window, "about")
        onboarding.settle_pointer(app, window)
        image = updates_states.grab(app, window, WIDTH, height)
        texts = onboarding.visible_texts(window.contentItem())
        if inspect is not None:
            inspect(window)
            app.processEvents()
        return image, texts, messages
    finally:
        sip.delete(engine)
        sip.delete(theme)
        if own_settings:
            sip.delete(settings)
        app.processEvents()
        qInstallMessageHandler(previous)


def save_snapshot(image: QImage, name: str) -> Path:
    SNAPSHOTS.mkdir(parents=True, exist_ok=True)
    path = SNAPSHOTS / name
    temporary = SNAPSHOTS / f".{name}.tmp"
    assert image.save(str(temporary), "PNG"), f"не удалось сохранить {temporary}"
    os.replace(temporary, path)
    return path


def find_object(window: Any, name: str) -> Any:
    found = [
        item for item in onboarding.visual_tree(window.contentItem()) if item.objectName() == name
    ]
    assert len(found) == 1, name
    return found[0]


def test_fake_about_matches_real_bridge() -> None:
    from astra_voice.ui.about_bridge import AboutBridge

    def members(meta: Any) -> set[str]:
        properties = {
            meta.property(i).name() for i in range(meta.propertyOffset(), meta.propertyCount())
        }
        methods = {
            bytes(meta.method(i).methodSignature()).decode()
            for i in range(meta.methodOffset(), meta.methodCount())
        }
        return properties | methods

    assert members(FakeAbout.staticMetaObject) == members(AboutBridge.staticMetaObject)


@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
@pytest.mark.parametrize("case", CASES)
def test_about_section_snapshot(app: Any, case: str, dark: bool) -> None:
    values, session, policy, with_updates = CASES[case]
    theme = "dark" if dark else "light"
    for height, suffix in ((HEIGHT, ""), (TALL, "-full")):
        about = FakeAbout(**values)
        info = FakeInfo(session, policy)
        updates = updates_states.FakeUpdates() if with_updates else None
        try:
            image, texts, messages = render_about(
                app, dark, about, info, height=height, updates=updates
            )
        finally:
            sip.delete(about)
            sip.delete(info)
            if updates is not None:
                sip.delete(updates)
        assert not messages, "\n".join(messages)
        assert {"Astra Voice", "0.2.0", "Версии", "Лицензии и приватность"} <= texts
        # Скрыто до своих вех: «Обновить из файла…» (M8), «Пройти настройку заново» (US-1.8).
        assert "Обновить из файла…" not in texts
        assert "Пройти настройку заново" not in texts
        # Ревизия модели — технический идентификатор, её место в «Отладке».
        assert not {text for text in texts if "322c3b2" in text}
        # Кнопки документов — простым языком, без имён файлов.
        assert {"Лицензия программы", "Лицензии компонентов", "Приватность"} <= texts
        assert not {"Открыть LICENSE", "NOTICE", "PRIVACY"} & texts
        if height == TALL:
            assert {
                "Данные на диске",
                "Окружение",
                "GigaAM v3 RNN-T",
                "Библиотеки",
                "Qt 5 — LGPL-3.0 · The Qt Company; PyQt5 — GPL-3.0 · Riverbank Computing; "
                "onnxruntime — MIT · Microsoft",
                "GPL-3.0-or-later",
                "Правила администратора",
                "Проверка обновлений",
            } <= texts
            if case == "about-base":
                assert {
                    "Сборка от 01.10.2026 · пакет deb",
                    "Python 3.11.2 · Qt 5.15.8 · PyQt5 5.15.9 · onnxruntime 1.24.4 · "
                    "onnx-asr 0.12.0",
                    "680 МБ",
                    "2,1 МБ",
                    "KDE Plasma",
                    "Не заданы — все настройки в ваших руках",
                    "Последняя попытка: сегодня в 14:05 · последний успех: 28.09.2026 в 10:12",
                    "обычно 0,38 с, в худших случаях 0,61 с · 148 диктовок",
                    "Проверить обновления",
                } <= texts
            else:
                assert {
                    "Запуск из исходного кода",
                    "Python 3.11.2 · Qt 5.15.8 · PyQt5 5.15.9 · onnxruntime не найден · "
                    "onnx-asr 0.12.0",
                    "Fly",
                    "Ещё не проверялись",
                    "Пока нет данных",
                    "Появится после первой диктовки",
                } <= texts
                assert "Проверить обновления" not in texts
        save_snapshot(image, f"{case}{suffix}-{theme}.png")


def test_about_buttons_call_bridge(app: Any) -> None:
    about = FakeAbout()
    info = FakeInfo("KDE", "ok")
    updates = updates_states.FakeUpdates()

    def inspect(window: Any) -> None:
        for name in (
            "aboutOpenLicense",
            "aboutOpenNotice",
            "aboutOpenPrivacy",
            "aboutOpenSettings",
            "aboutOpenLogs",
            "aboutCheckUpdates",
        ):
            onboarding.click_item(find_object(window, name))
            app.processEvents()
        onboarding.click_item(find_object(window, "aboutClearStats"))
        QTest.qWait(50)
        app.processEvents()
        dialog = next(
            item
            for item in onboarding.visual_tree(window.contentItem())
            if item.isVisible() and item.property("text") == "Очистить статистику?"
        )
        assert dialog is not None
        assert (
            "Вся локальная статистика начнётся заново: диктовки, время распознавания, "
            "проверки обновлений, ошибки микрофона."
        ) in onboarding.visible_texts(window.contentItem())
        confirm = [
            item
            for item in onboarding.visual_tree(window.contentItem())
            if item.isVisible()
            and item.property("text") == "Очистить"
            and item.metaObject().indexOfSignal(b"clicked()") >= 0
            and item.objectName() != "aboutClearStats"
        ]
        assert len(confirm) == 1
        onboarding.click_item(confirm[0])
        QTest.qWait(50)
        app.processEvents()
        assert find_object(window, "aboutStats").property("text") == "Пока нет данных"
        # Статистики нет — строка целиком в виде «выключено» (макет 06-about, empty).
        stats_row = next(
            item
            for item in onboarding.visual_tree(window.contentItem())
            if item.property("label") == "Статистика распознавания"
        )
        assert stats_row.property("rowEnabled") is False
        assert find_object(window, "aboutClearStats").property("enabled") is False

    try:
        _, texts, messages = render_about(
            app, False, about, info, height=TALL, updates=updates, inspect=inspect
        )
        assert not messages, "\n".join(messages)
        assert "Применены: строки с замком задал администратор" in texts
        assert about.calls == [
            "refresh",
            "openLicense",
            "openNotice",
            "openPrivacy",
            "openSettingsFolder",
            "openLogsFolder",
            "clearStats",
        ]
        assert updates.calls == ["checkNow"]
    finally:
        sip.delete(about)
        sip.delete(info)
        sip.delete(updates)


def test_about_models_folder_uses_settings_bridge(app: Any) -> None:
    """«Открыть» у моделей — тот же слот, что и в разделе «Модели»."""
    about = FakeAbout()
    info = FakeInfo("KDE", "absent")
    settings = onboarding.FakeSettings()

    def inspect(window: Any) -> None:
        onboarding.click_item(find_object(window, "aboutOpenModels"))
        app.processEvents()

    try:
        render_about(app, False, about, info, height=TALL, settings=settings, inspect=inspect)
        assert "openModelsFolder" in settings.calls
        assert about.calls == ["refresh"]
    finally:
        sip.delete(about)
        sip.delete(info)
        sip.delete(settings)


def test_about_without_bridges_shows_version(app: Any) -> None:
    """Без aboutBridge раздел открывается: версия из appInfo, кнопки документов неактивны."""
    info = FakeInfo("OTHER", "absent")

    def inspect(window: Any) -> None:
        assert find_object(window, "aboutOpenNotice").property("enabled") is False
        assert find_object(window, "aboutOpenLicense").property("enabled") is False

    try:
        _, texts, messages = render_about(app, True, None, info, height=TALL, inspect=inspect)
        assert not messages, "\n".join(messages)
        assert {"Astra Voice", "0.2.0", "Другое окружение", "Нет сведений"} <= texts
        assert "Компоненты" not in texts
    finally:
        sip.delete(info)
