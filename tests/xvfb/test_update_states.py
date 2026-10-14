"""M7-ядро v0.2: строка-статус, панель «Что нового» и раздел «Сеть» в двух темах.

Каталог снимков задаётся ASTRA_VOICE_SNAPSHOT_DIR_UPDATES
или по умолчанию design/refs/impl/updates. Снимки сверяются глазами с
design/refs/04-footer-states*.png, 04-update-panel*.png и 04-network*.png;
сравнение по пикселям не делается — макеты рисуют ещё не реализованные состояния.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from PyQt5 import sip
from PyQt5.QtCore import QObject, QUrl, pyqtProperty, pyqtSignal, pyqtSlot, qInstallMessageHandler
from PyQt5.QtGui import QFont, QFontDatabase, QFontInfo, QImage
from PyQt5.QtQml import QQmlApplicationEngine, QQmlComponent
from PyQt5.QtQuick import QQuickView, QQuickWindow
from PyQt5.QtTest import QTest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from astra_voice.ui.icons import install_icon_provider
from helpers.qt_app import get_qapplication  # noqa: E402

pytestmark = pytest.mark.xvfb
REPO = Path(__file__).resolve().parents[2]
# Промежуточные прогоны разработки не трогают эталоны репозитория.
SNAPSHOTS = REPO / Path(
    os.environ.get("ASTRA_VOICE_SNAPSHOT_DIR_UPDATES") or "design/refs/impl/updates"
)
FOOTER_W, FOOTER_H = 760, 36
WIDTH, HEIGHT = 1024, 620
NOTE_LINES = (
    "Каталог моделей: 12 моделей, память в работе замеряется на вашем компьютере",
    "Автозапуск при входе в систему",
    "Исправлено: пилюля перекрывалась панелью при смене раскладки",
)
NOTES = [{"text": line, "bullet": True} for line in NOTE_LINES]

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


# Фейки моста настроек и темы, хелперы кадра — общие с тестом онбординга.
onboarding = load_module("_update_states_onboarding", REPO / "tests/xvfb/test_onboarding.py")


def _value(name: str) -> Callable[[Any], Any]:
    return lambda self: self._values[name]


class FakeUpdates(QObject):
    """Контракт updatesBridge (docs/ui-bridge.md §3.8); слоты пишут вызовы."""

    statusChanged = pyqtSignal()
    networkChanged = pyqtSignal()
    downloadChanged = pyqtSignal()

    def __init__(self, **values: Any) -> None:
        super().__init__()
        self.calls: list[str] = []
        self._values: dict[str, Any] = {
            "state": "disabled",
            "version": "",
            "notes": [],
            "currentVersion": "0.2.0",
            "snoozed": False,
            "manual": False,
            "checkedText": "",
            "releasePageAvailable": False,
            "checkRefusal": "",
            "networkRefusal": "",
            "canCheckNow": True,
            "restState": "idle",
        }
        self._values.update(
            {
                "downloadPhase": "idle",
                "artifactTrack": "appimage",
                "artifactSizeText": "100 МБ",
                "artifactSize": 100000000,
                "downloadReceived": 0,
                "downloadTotal": 100000000,
                "downloadPercent": 0,
                "downloadRetryAt": 0.0,
                "downloadError": "",
                "downloadErrorText": "",
                "downloadRetryText": "",
                "folderPath": "",
                "adminInstruction": "",
                "installRefusal": "",
                "installRefusalText": "",
                "downloadBusy": False,
                "downloadCancelling": False,
                "canDownload": True,
                "canRetryDownload": False,
                "canCancelDownload": False,
                "canOpenFolder": False,
                "canInstallAndRestart": False,
                "canSkipVersion": True,
                "canRemindLater": True,
            }
        )
        self._values.update(values)

    state = pyqtProperty(str, _value("state"), notify=statusChanged)
    version = pyqtProperty(str, _value("version"), notify=statusChanged)
    notes = pyqtProperty("QVariantList", _value("notes"), notify=statusChanged)
    currentVersion = pyqtProperty(str, _value("currentVersion"), constant=True)
    snoozed = pyqtProperty(bool, _value("snoozed"), notify=statusChanged)
    manual = pyqtProperty(bool, _value("manual"), notify=statusChanged)
    checkedText = pyqtProperty(str, _value("checkedText"), notify=statusChanged)
    releasePageAvailable = pyqtProperty(bool, _value("releasePageAvailable"), notify=statusChanged)
    checkRefusal = pyqtProperty(str, _value("checkRefusal"), notify=networkChanged)
    networkRefusal = pyqtProperty(str, _value("networkRefusal"), notify=networkChanged)
    canCheckNow = pyqtProperty(bool, _value("canCheckNow"), notify=networkChanged)
    restState = pyqtProperty(str, _value("restState"), notify=networkChanged)

    downloadPhase = pyqtProperty(str, _value("downloadPhase"), notify=downloadChanged)
    artifactTrack = pyqtProperty(str, _value("artifactTrack"), notify=downloadChanged)
    artifactSizeText = pyqtProperty(str, _value("artifactSizeText"), notify=downloadChanged)
    downloadError = pyqtProperty(str, _value("downloadError"), notify=downloadChanged)
    downloadErrorText = pyqtProperty(str, _value("downloadErrorText"), notify=downloadChanged)
    downloadRetryText = pyqtProperty(str, _value("downloadRetryText"), notify=downloadChanged)
    folderPath = pyqtProperty(str, _value("folderPath"), notify=downloadChanged)
    adminInstruction = pyqtProperty(str, _value("adminInstruction"), notify=downloadChanged)
    installRefusal = pyqtProperty(str, _value("installRefusal"), notify=downloadChanged)
    installRefusalText = pyqtProperty(str, _value("installRefusalText"), notify=downloadChanged)
    downloadBusy = pyqtProperty(bool, _value("downloadBusy"), notify=downloadChanged)
    downloadCancelling = pyqtProperty(bool, _value("downloadCancelling"), notify=downloadChanged)
    canDownload = pyqtProperty(bool, _value("canDownload"), notify=downloadChanged)
    canRetryDownload = pyqtProperty(bool, _value("canRetryDownload"), notify=downloadChanged)
    canCancelDownload = pyqtProperty(bool, _value("canCancelDownload"), notify=downloadChanged)
    canOpenFolder = pyqtProperty(bool, _value("canOpenFolder"), notify=downloadChanged)
    canInstallAndRestart = pyqtProperty(
        bool, _value("canInstallAndRestart"), notify=downloadChanged
    )
    canSkipVersion = pyqtProperty(bool, _value("canSkipVersion"), notify=downloadChanged)
    canRemindLater = pyqtProperty(bool, _value("canRemindLater"), notify=downloadChanged)
    artifactSize = pyqtProperty("qlonglong", _value("artifactSize"), notify=downloadChanged)
    downloadReceived = pyqtProperty("qlonglong", _value("downloadReceived"), notify=downloadChanged)
    downloadTotal = pyqtProperty("qlonglong", _value("downloadTotal"), notify=downloadChanged)
    downloadPercent = pyqtProperty(int, _value("downloadPercent"), notify=downloadChanged)
    downloadRetryAt = pyqtProperty(float, _value("downloadRetryAt"), notify=downloadChanged)

    def update(self, **values: Any) -> None:
        self._values.update(values)
        self.statusChanged.emit()
        self.networkChanged.emit()
        self.downloadChanged.emit()

    @pyqtSlot()
    def checkNow(self) -> None:  # noqa: N802
        self.calls.append("checkNow")

    @pyqtSlot()
    def skipVersion(self) -> None:  # noqa: N802
        self.calls.append("skipVersion")

    @pyqtSlot()
    def clearSkip(self) -> None:  # noqa: N802
        self.calls.append("clearSkip")

    @pyqtSlot()
    def remindLater(self) -> None:  # noqa: N802
        self.calls.append("remindLater")

    @pyqtSlot()
    def openReleasePage(self) -> None:  # noqa: N802
        self.calls.append("openReleasePage")

    @pyqtSlot()
    def download(self) -> None:  # noqa: N802
        self.calls.append("download")

    @pyqtSlot()
    def cancelDownload(self) -> None:  # noqa: N802
        self.calls.append("cancelDownload")

    @pyqtSlot()
    def retryDownload(self) -> None:  # noqa: N802
        self.calls.append("retryDownload")

    @pyqtSlot()
    def openFolder(self) -> None:  # noqa: N802
        self.calls.append("openFolder")

    @pyqtSlot()
    def installAndRestart(self) -> None:  # noqa: N802
        self.calls.append("installAndRestart")

    @pyqtSlot()
    def refreshCapabilities(self) -> None:  # noqa: N802
        pass


AVAILABLE = {"state": "available", "version": "0.2.1", "notes": NOTES, "releasePageAvailable": True}
FOOTER_CASES: dict[str, dict[str, Any]] = {
    "disabled": {},
    "policy-locked": {"updateState": "policy-locked"},
    "checking": {"updateState": "checking"},
    "uptodate": {"updateState": "uptodate"},
    "available": {"updateState": "available", "updateVersion": "0.2.1"},
    "available-snoozed": {
        "updateState": "available",
        "updateVersion": "0.2.1",
        "updateSnoozed": True,
    },
    "unavailable": {"updateState": "unavailable"},
    "error-net": {"updateState": "error-net"},
    "skipped": {"updateState": "skipped", "updateVersion": "0.2.1"},
    "idle": {"updateState": "idle"},
}
# Раздел «Сеть»: (значения updatesBridge, изменения моста настроек).
NETWORK_CASES: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {
    "network-base": ({}, {}),
    "network-offline": (
        {"checkRefusal": "offline", "networkRefusal": "offline", "canCheckNow": False},
        {"_offline": True},
    ),
    "network-policy": (
        {"state": "policy-locked", "checkRefusal": "policy", "canCheckNow": False},
        {"_lockedSettings": ["check_app_updates"]},
    ),
    "panel-available": (AVAILABLE, {"_checkAppUpdates": True}),
    "panel-checking": ({"state": "checking", "manual": True}, {}),
    "panel-uptodate": (
        {"state": "uptodate", "manual": True, "checkedText": "Проверено сегодня в 14:02"},
        {"_checkAppUpdates": True},
    ),
    "panel-unavailable": ({"state": "unavailable"}, {"_checkAppUpdates": True}),
    "panel-skipped": ({"state": "skipped", "version": "0.2.1"}, {"_checkAppUpdates": True}),
}


@pytest.fixture(scope="module")
def app() -> Any:
    return get_qapplication()


@pytest.fixture(scope="module", autouse=True)
def pt_root_ui_fonts(app: Any) -> None:
    # Use the actual font so fallback geometry cannot conceal final-glyph clipping.
    paths = sorted((REPO / "tests/fixtures/fonts/pt-root-ui").glob("*.ttf"))
    assert paths
    for path in paths:
        assert QFontDatabase.addApplicationFont(str(path)) >= 0, path
    # Registration is process-global and intentionally lasts until process exit.
    # removeApplicationFont causes SIGSEGV in subsequent renders: Qt text/scene
    # caches retain these fonts even after the QML engines have been destroyed.


def collect_messages() -> tuple[list[str], Any]:
    messages: list[str] = []

    def handler(_mode: Any, _context: Any, message: str) -> None:
        messages.append(message)

    return messages, qInstallMessageHandler(handler)


def save_snapshot(image: QImage, name: str) -> Path:
    SNAPSHOTS.mkdir(parents=True, exist_ok=True)
    path = SNAPSHOTS / name
    temporary = SNAPSHOTS / f".{name}.tmp"
    assert image.save(str(temporary), "PNG"), f"не удалось сохранить {temporary}"
    os.replace(temporary, path)
    return path


def grab(app: Any, window: QQuickWindow, width: int, height: int) -> QImage:
    """grabWindow с запасным grabToImage: Qt5/offscreen может не уметь первый."""
    pending = None
    image = QImage()
    for _ in range(40):
        image = window.grabWindow()
        if image.isNull():
            if pending is None:
                pending = window.contentItem().grabToImage()
            if pending is not None:
                image = pending.image()
                if not image.isNull():
                    pending = None
        if not image.isNull() and (image.width(), image.height()) == (
            round(width * image.devicePixelRatio()),
            round(height * image.devicePixelRatio()),
        ):
            return image
        QTest.qWait(25)
        app.processEvents()
    pytest.fail(f"кадр не готов: {image.width()}×{image.height()}")


def add_background(engine: Any, window: Any) -> Any:
    """grabToImage в Qt5/software не включает цвет очистки окна — повторяем его под содержимым."""
    component = QQmlComponent(engine)
    component.setData(b"import QtQuick 2.15; Rectangle { anchors.fill: parent; z: -1 }", QUrl())
    background = component.create()
    assert background is not None
    background.setProperty("color", window.color())
    background.setParent(window.contentItem())
    background.setParentItem(window.contentItem())
    return component, background


def render_footer(
    app: Any,
    dark: bool,
    values: dict[str, Any],
    inspect: Callable[[Any], None] | None = None,
) -> tuple[QImage, Any, list[str]]:
    messages, previous = collect_messages()
    view = QQuickView()
    install_icon_provider(view.engine())
    theme = onboarding.FakeTheme(dark)
    try:
        view.rootContext().setContextProperty("themeSource", theme)
        view.setResizeMode(QQuickView.SizeRootObjectToView)
        view.resize(FOOTER_W, FOOTER_H)
        component = QQmlComponent(
            view.engine(), QUrl.fromLocalFile(str(REPO / "qml/StatusBar.qml"))
        )
        bar = component.create(view.rootContext())
        assert bar is not None, [error.toString() for error in component.errors()]
        for name, value in values.items():
            assert bar.setProperty(name, value), name
        view.setContent(QUrl.fromLocalFile(str(REPO / "qml/StatusBar.qml")), component, bar)
        view.show()
        QTest.qWait(120)
        app.processEvents()
        image = grab(app, view, FOOTER_W, FOOTER_H)
        if inspect is not None:
            inspect(view)
        texts = {
            item.property("text")
            for item in onboarding.visual_tree(bar)
            if item.isVisible() and isinstance(item.property("text"), str)
        }
        return image, texts, messages
    finally:
        sip.delete(view)
        sip.delete(theme)
        app.processEvents()
        qInstallMessageHandler(previous)


def render_network(
    app: Any,
    dark: bool,
    updates: FakeUpdates,
    settings: Any,
    inspect: Callable[[Any], None] | None = None,
    section: str = "network",
) -> tuple[QImage, set[str], list[str]]:
    messages, previous = collect_messages()
    engine = QQmlApplicationEngine()
    install_icon_provider(engine)
    theme = onboarding.FakeTheme(dark)
    try:
        context = engine.rootContext()
        context.setContextProperty("themeSource", theme)
        context.setContextProperty("showOnboarding", False)
        context.setContextProperty("settingsBridge", settings)
        context.setContextProperty("updatesBridge", updates)
        engine.load(QUrl.fromLocalFile(str(REPO / "qml/Main.qml")))
        roots = engine.rootObjects()
        assert len(roots) == 1, messages
        window = roots[0]
        window.setWidth(WIDTH)
        window.setHeight(HEIGHT)
        assert window.setProperty("freezeAnimations", True)
        _background = add_background(engine, window)
        window.show()
        QTest.qWait(180)
        app.processEvents()
        onboarding.select_section(app, window, section)
        onboarding.settle_pointer(app, window)
        image = grab(app, window, WIDTH, HEIGHT)
        texts = onboarding.visible_texts(window.contentItem())
        if inspect is not None:
            inspect(window)
            app.processEvents()
        return image, texts, messages
    finally:
        sip.delete(engine)
        sip.delete(theme)
        app.processEvents()
        qInstallMessageHandler(previous)


def make_settings(changes: dict[str, Any]) -> Any:
    settings = onboarding.FakeSettings()
    for name, value in changes.items():
        setattr(settings, name, value)
    return settings


def test_fake_updates_matches_real_bridge() -> None:
    from astra_voice.ui.updates_bridge import UpdatesBridge

    def members(meta: Any) -> set[str]:
        properties = {
            meta.property(i).name() for i in range(meta.propertyOffset(), meta.propertyCount())
        }
        methods = {
            bytes(meta.method(i).methodSignature()).decode()
            for i in range(meta.methodOffset(), meta.methodCount())
        }
        return properties | methods

    assert members(FakeUpdates.staticMetaObject) == members(UpdatesBridge.staticMetaObject)


@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
@pytest.mark.parametrize("case", FOOTER_CASES)
def test_footer_state_snapshot(app: Any, case: str, dark: bool) -> None:
    image, texts, messages = render_footer(app, dark, FOOTER_CASES[case])
    assert not messages, "\n".join(messages)
    expected = {
        "disabled": "Проверка обновлений отключена",
        "policy-locked": "Проверка обновлений отключена (задано администратором)",
        "checking": "Проверяю обновления…",
        "uptodate": "Установлена последняя версия",
        "available": "Доступна версия 0.2.1 · Подробнее",
        "available-snoozed": "Доступна версия 0.2.1 · Подробнее",
        "unavailable": "Источник обновлений недоступен · Повторить",
        "error-net": "Не удалось проверить обновления · Повторить",
        "skipped": "Версия 0.2.1 пропущена · Показать",
        "idle": None,
    }[case]
    if expected is not None:
        assert expected in texts
    else:
        assert not {text for text in texts if "бновлен" in text}
    assert "v0.2.0" in texts
    save_snapshot(image, f"footer-{case}-{'dark' if dark else 'light'}.png")


@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"state": "disabled"}, "Проверка обновлений отключена"),
        ({"state": "policy-locked"}, "Проверка обновлений отключена (задано администратором)"),
        ({"state": "checking"}, "Проверяю обновления…"),
        ({"state": "uptodate"}, "Установлена последняя версия"),
        ({}, "Доступна версия 0.1.4 · Подробнее"),
        ({"snoozed": True}, "Доступна версия 0.1.4 · Подробнее"),
        ({"state": "unavailable"}, "Источник обновлений недоступен · Повторить"),
        ({"state": "error-net"}, "Не удалось проверить обновления · Повторить"),
        ({"state": "skipped"}, "Версия 0.1.4 пропущена · Показать"),
        ({"networkRefusal": "offline"}, "Офлайн-режим · Подробнее"),
        ({"downloadPhase": "metadata"}, "Проверяю сведения об обновлении…"),
        ({"downloadPhase": "downloading", "downloadPercent": 42}, "Скачиваю обновление · 42 %"),
        ({"downloadPhase": "verifying"}, "Проверяю обновление…"),
        ({"downloadPhase": "readydeb"}, "Пакет проверен · Открыть обновления"),
        ({"downloadPhase": "readyappimage"}, "Обновление готово · Открыть обновления"),
        ({"downloadPhase": "installing"}, "Устанавливаю обновление…"),
        ({"downloadPhase": "error"}, "Обновление не выполнено · Подробнее"),
        ({"downloadPhase": "cancelled"}, "Доступна версия 0.1.4 · Подробнее"),
    ],
    ids=[
        "disabled",
        "policy-locked",
        "checking",
        "uptodate",
        "available",
        "snoozed",
        "unavailable",
        "error-net",
        "skipped",
        "offline",
        "metadata",
        "downloading",
        "verifying",
        "readydeb",
        "readyappimage",
        "installing",
        "error",
        "cancelled",
    ],
)
def test_full_window_status_text_is_not_truncated_with_pt_root_ui(
    app: Any,
    pt_root_ui_fonts: None,
    dark: bool,
    overrides: dict[str, Any],
    expected: str,
) -> None:
    updates = FakeUpdates(**{**AVAILABLE, "version": "0.1.4", **overrides})
    settings = make_settings({})

    def inspect(window: Any) -> None:
        assert (window.width(), window.height()) == (1024, 620)
        label = find_object(window, "statusUpdateText")
        assert label.isVisible()
        assert QFontInfo(label.property("font")).family() == "PT Root UI"
        assert label.property("text") == expected
        assert label.property("truncated") is False
        assert updates.calls == []

    try:
        _, _, messages = render_network(app, dark, updates, settings, inspect)
        assert not messages, "\n".join(messages)
    finally:
        sip.delete(updates)
        sip.delete(settings)


@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
def test_narrow_footer_elides_real_policy_message_with_pt_root_ui(
    app: Any,
    pt_root_ui_fonts: None,
    dark: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Main.qml has a 900 px minimum; render its footer independently at 640 px.
    monkeypatch.setattr(sys.modules[__name__], "FOOTER_W", 640)

    def inspect(window: Any) -> None:
        assert window.width() == 640
        label = find_object(window, "statusUpdateText")
        update = find_object(window, "statusUpdate")
        content = update.parentItem().parentItem()
        assert label.isVisible()
        assert QFontInfo(label.property("font")).family() == "PT Root UI"
        assert label.property("text") == ("Проверка обновлений отключена (задано администратором)")
        assert label.property("truncated") is True
        assert update.width() <= max(0, content.width() * 0.72 - 90) + 1

    _, _, messages = render_footer(app, dark, {"updateState": "policy-locked"}, inspect)
    assert not messages, "\n".join(messages)


@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
def test_available_badge_uses_real_medium_font_with_pt_root_ui(
    app: Any, pt_root_ui_fonts: None, dark: bool
) -> None:
    updates = FakeUpdates(**AVAILABLE)
    settings = make_settings({})

    def inspect(window: Any) -> None:
        badge = find_object(window, "updateNewBadgeText")
        assert badge.isVisible()
        assert badge.property("text") == "Новая"
        font = badge.property("font")
        info = QFontInfo(font)
        assert info.family() == "PT Root UI"
        assert font.weight() == QFont.Medium
        assert info.styleName() == "Medium" or info.weight() == 57
        assert updates.calls == []

    try:
        _, _, messages = render_network(app, dark, updates, settings, inspect)
        assert not messages, "\n".join(messages)
    finally:
        sip.delete(updates)
        sip.delete(settings)


@pytest.fixture
def status_bar(app: Any) -> Iterator[Any]:
    engine = QQmlApplicationEngine()
    install_icon_provider(engine)
    component = QQmlComponent(engine, QUrl.fromLocalFile(str(REPO / "qml/StatusBar.qml")))
    bar = component.create()
    assert bar is not None, [error.toString() for error in component.errors()]
    try:
        yield bar
    finally:
        sip.delete(bar)
        sip.delete(engine)
        app.processEvents()


def test_footer_uptodate_hides_after_3000_ms(status_bar: Any) -> None:
    bar = status_bar
    assert bar.setProperty("updateState", "uptodate")
    assert bar.property("updateText") == "Установлена последняя версия"
    QTest.qWait(2800)
    assert bar.property("updateText") == "Установлена последняя версия"
    QTest.qWait(400)
    assert bar.property("updateText") == ""


def test_footer_uptodate_returns_to_disabled_when_toggle_off(status_bar: Any) -> None:
    """Ревью 29.09: после ручной проверки при выключенном тумблере строка не пустеет."""
    bar = status_bar
    assert bar.setProperty("updateRestState", "disabled")
    assert bar.setProperty("updateState", "uptodate")
    assert bar.property("updateText") == "Установлена последняя версия"
    assert bar.property("updateIcon") == "check"
    QTest.qWait(3200)
    assert bar.property("updateText") == "Проверка обновлений отключена"
    assert bar.property("updateIcon") == ""


@pytest.mark.parametrize(
    ("state", "clickable"),
    [
        ("disabled", False),
        ("policy-locked", False),
        ("checking", False),
        ("uptodate", False),
        ("available", True),
        ("unavailable", True),
        ("error-net", True),
        ("skipped", True),
    ],
)
def test_footer_clickable_states(status_bar: Any, state: str, clickable: bool) -> None:
    bar = status_bar
    assert bar.setProperty("updateState", state)
    assert bar.property("updateClickable") is clickable
    # Акцент — только у «Доступна версия» без «Напомнить позже».
    assert bar.property("updateAccent") is (state == "available")
    if state == "available":
        assert bar.setProperty("updateSnoozed", True)
        assert bar.property("updateAccent") is False


@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
@pytest.mark.parametrize("case", NETWORK_CASES)
def test_network_section_snapshot(app: Any, case: str, dark: bool) -> None:
    values, changes = NETWORK_CASES[case]
    updates = FakeUpdates(**values)
    settings = make_settings(changes)
    try:
        image, texts, messages = render_network(app, dark, updates, settings)
    finally:
        sip.delete(updates)
        sip.delete(settings)
    assert not messages, "\n".join(messages)
    assert {"Сетевые проверки", "Проверять обновления утилиты", "Офлайн-режим"} <= texts
    # Подзаголовок target M7/action2b учитывает и скачивание программы.
    assert "Сетевые проверки и обновления программы" in texts
    assert "Четыре сетевых действия — и ни одного больше" not in texts
    # Тумблер моделей виден и сохраняет выбор; ручная проверка каталога ещё не реализована.
    assert "Проверять обновления моделей" in texts
    assert "Обновить из файла…" not in texts
    assert not {text for text in texts if "trust" in text.lower()}
    panel_title = {
        "network-base": "Установлена версия",
        "network-offline": "Установлена версия",
        "network-policy": "Установлена версия",
        "panel-available": "Доступна версия",
        "panel-checking": "Проверяю обновления…",
        "panel-uptodate": "Установлена последняя версия",
        "panel-skipped": "Версия",
        "panel-unavailable": "Источник обновлений недоступен",
    }.get(case)
    if panel_title is not None:
        assert panel_title in texts
    if case == "panel-available":
        assert {"Что нового", *NOTE_LINES, "Скачать новую версию", "Пропустить эту версию"} <= texts
        assert not {"Страница выпуска", "Напомнить позже"} & texts
    if case == "panel-uptodate":
        assert {"Установлена последняя версия", "0.2.0"} <= texts
    if case == "network-offline":
        assert "Проверить сейчас нельзя: включён офлайн-режим." in texts
    save_snapshot(image, f"{case}-{'dark' if dark else 'light'}.png")


def find_object(window: Any, name: str) -> Any:
    found = [
        item for item in onboarding.visual_tree(window.contentItem()) if item.objectName() == name
    ]
    assert len(found) == 1, name
    return found[0]


def test_panel_buttons_call_bridge(app: Any) -> None:
    updates = FakeUpdates(**AVAILABLE)
    settings = make_settings({})

    def inspect(window: Any) -> None:
        lines = [
            item
            for item in onboarding.visual_tree(window.contentItem())
            if item.objectName() == "updateNoteLine"
        ]
        assert [line.property("text") for line in lines] == list(NOTE_LINES)
        assert all(line.property("textFormat") == 0 for line in lines)  # Text.PlainText
        # Висячий отступ: текст пункта начинается с x = 17 и переносится там же.
        assert all(line.property("x") == 17 for line in lines)
        for name in ("updatePrimary", "updateSkip"):
            onboarding.click_item(find_object(window, name))
            app.processEvents()

    try:
        render_network(app, False, updates, settings, inspect)
        assert updates.calls == ["download", "skipVersion"]
    finally:
        sip.delete(updates)
        sip.delete(settings)


@pytest.mark.parametrize("release_available", [False, True])
def test_removed_panel_actions_absent_even_with_backend_capability(
    app: Any, release_available: bool
) -> None:
    updates = FakeUpdates(**{**AVAILABLE, "releasePageAvailable": release_available})
    settings = make_settings({})
    try:

        def inspect(window: Any) -> None:
            names = {item.objectName() for item in onboarding.visual_tree(window.contentItem())}
            assert not {"updateReleasePage", "updateRemindLater"} & names

        _, texts, messages = render_network(app, False, updates, settings, inspect)
        assert not messages, "\n".join(messages)
        assert not {"Страница выпуска", "Напомнить позже"} & texts
        assert "Пропустить эту версию" in texts
        assert updates.calls == []
    finally:
        sip.delete(updates)
        sip.delete(settings)


@pytest.mark.parametrize(
    ("values", "changes", "enabled"),
    [
        ({}, {}, True),
        ({"checkRefusal": "offline", "networkRefusal": "offline", "canCheckNow": False}, {}, False),
        ({"checkRefusal": "policy", "canCheckNow": False}, {}, False),
    ],
    ids=["normal", "offline", "policy"],
)
def test_check_now_button(
    app: Any, values: dict[str, Any], changes: dict[str, Any], enabled: bool
) -> None:
    updates = FakeUpdates(**values)
    settings = make_settings(changes)

    def inspect(window: Any) -> None:
        button = find_object(window, "checkNowButton")
        assert button.isVisible()
        panel = find_object(window, "updatePanel")
        parent = button.parentItem()
        while parent is not None and parent is not panel:
            parent = parent.parentItem()
        assert parent is panel, "check action must be inside the result panel"
        assert button.property("enabled") is enabled
        if enabled:
            onboarding.click_item(button)

    try:
        render_network(app, False, updates, settings, inspect)
        assert updates.calls == (["checkNow"] if enabled else [])
    finally:
        sip.delete(updates)
        sip.delete(settings)


def test_offline_toggle_writes_setting_and_policy_lock_blocks(app: Any) -> None:
    updates = FakeUpdates()
    settings = make_settings({})

    def inspect(window: Any) -> None:
        toggle = find_object(window, "offlineToggle")
        onboarding.click_item(toggle)
        app.processEvents()
        assert settings.offline is True and toggle.property("checked") is True
        # Клик не отвязывает тумблер: мост вернул «выкл» — тумблер следует за ним.
        settings.offline = False
        app.processEvents()
        assert toggle.property("checked") is False
        settings.offline = True
        app.processEvents()
        assert toggle.property("checked") is True

    try:
        render_network(app, False, updates, settings, inspect)
        assert settings.offline is True
    finally:
        sip.delete(updates)
        sip.delete(settings)

    updates = FakeUpdates(networkRefusal="admin", checkRefusal="admin", canCheckNow=False)
    settings = make_settings({})

    def inspect_locked(window: Any) -> None:
        toggle = find_object(window, "offlineToggle")
        # Заблокированный тумблер сохраняет цвет своего положения: «включён».
        assert toggle.property("checked") is True
        assert toggle.property("enabled") is False
        onboarding.click_item(toggle)

    try:
        render_network(app, False, updates, settings, inspect_locked)
        assert settings.offline is False
    finally:
        sip.delete(updates)
        sip.delete(settings)


def test_footer_click_opens_network_and_unskips(app: Any) -> None:
    updates = FakeUpdates(state="skipped", version="0.2.1")
    settings = make_settings({})
    messages, previous = collect_messages()
    engine = QQmlApplicationEngine()
    install_icon_provider(engine)
    try:
        context = engine.rootContext()
        context.setContextProperty("showOnboarding", False)
        context.setContextProperty("settingsBridge", settings)
        context.setContextProperty("updatesBridge", updates)
        engine.load(QUrl.fromLocalFile(str(REPO / "qml/Main.qml")))
        window = engine.rootObjects()[0]
        window.setWidth(WIDTH)
        window.setHeight(HEIGHT)
        window.show()
        QTest.qWait(150)
        onboarding.click_item(find_object(window, "statusUpdate"))
        app.processEvents()
        assert updates.calls == ["clearSkip"]
        sidebar = onboarding.settings_sidebar(window)
        keys = [item["key"] for item in onboarding.section_descriptions(window)]
        assert sidebar.property("currentIndex") == keys.index("network")
    finally:
        sip.delete(engine)
        sip.delete(updates)
        sip.delete(settings)
        app.processEvents()
        qInstallMessageHandler(previous)
    assert not messages, "\n".join(messages)


@pytest.fixture(autouse=True, scope="module")
def _snapshot_dir() -> Iterator[None]:
    SNAPSHOTS.mkdir(parents=True, exist_ok=True)
    yield


def test_env_offline_toggle_shows_actual_state_after_user_turns_off(app: Any) -> None:
    """Повторное ревью: пользователь выключил офлайн, но окружение держит его — тумблер «вкл»."""
    updates = FakeUpdates(checkRefusal="offline", networkRefusal="offline", canCheckNow=False)
    settings = make_settings({"_offline": True})

    def inspect(window: Any) -> None:
        toggle = find_object(window, "offlineToggle")
        assert toggle.property("checked") is True
        onboarding.click_item(toggle)
        app.processEvents()
        assert settings.offline is False
        assert toggle.property("checked") is True
        # Офлайн теперь держит окружение: тумблер заблокирован, подпись об этом.
        assert toggle.property("enabled") is False
        texts = onboarding.visible_texts(window.contentItem())
        assert "Включён при запуске программы — выключить здесь нельзя" in texts

    try:
        render_network(app, False, updates, settings, inspect)
    finally:
        sip.delete(updates)
        sip.delete(settings)


@pytest.mark.parametrize(
    ("values", "changes", "hidden"),
    [
        ({}, {}, False),
        ({"networkRefusal": "offline"}, {}, True),
        ({}, {"_offline": True}, True),
    ],
    ids=["online", "env-offline", "user-offline"],
)
def test_models_download_button_hidden_offline(
    app: Any, values: dict[str, Any], changes: dict[str, Any], hidden: bool
) -> None:
    updates = FakeUpdates(**values)
    settings = make_settings(changes)
    try:
        _, texts, messages = render_network(app, False, updates, settings, section="models")
        assert not messages, "\n".join(messages)
        assert ("Скачать выбранное" in texts) is not hidden
    finally:
        sip.delete(updates)
        sip.delete(settings)


def test_panel_marker_and_mono_version(app: Any) -> None:
    updates = FakeUpdates(**AVAILABLE)
    settings = make_settings({})

    def inspect(window: Any) -> None:
        markers = [
            item
            for item in onboarding.visual_tree(window.contentItem())
            if item.objectName() == "updateNoteMarker" and item.isVisible()
        ]
        assert len(markers) == len(NOTE_LINES)
        for marker in markers:
            assert (marker.property("x"), marker.width(), marker.height()) == (2, 4, 4)
            assert marker.property("radius") == 2
            assert 0 < marker.property("y") < 14

    try:
        render_network(app, False, updates, settings, inspect)
    finally:
        sip.delete(updates)
        sip.delete(settings)

    updates = FakeUpdates(state="uptodate", manual=True)
    settings = make_settings({})

    def inspect_version(window: Any) -> None:
        version = find_object(window, "updateInstalledVersion")
        assert version.property("text") == "0.2.0"
        assert "Mono" in version.property("font").family()

    try:
        _, texts, _ = render_network(app, False, updates, settings, inspect_version)
        assert "Установлена последняя версия" in texts
    finally:
        sip.delete(updates)
        sip.delete(settings)


@pytest.mark.parametrize(
    "field,name,host",
    [
        ("checkAppUpdates", "checkAppUpdatesToggle", "github.com"),
        ("checkModelUpdates", "checkModelUpdatesToggle", "huggingface.co"),
    ],
)
@pytest.mark.parametrize("mode", ["ordinary", "offline", "policy"])
def test_network_check_toggles_visible_and_follow_effective_settings(
    app: Any, field: str, name: str, host: str, mode: str
) -> None:
    changes: dict[str, Any] = {}
    values: dict[str, Any] = {}
    if mode == "offline":
        changes["_offline"] = True
        values = {"networkRefusal": "offline", "checkRefusal": "offline", "canCheckNow": False}
    elif mode == "policy":
        changes["_lockedSettings"] = ["check_app_updates", "check_model_updates"]
        changes["_checkAppUpdates"] = changes["_checkModelUpdates"] = False
        values = {"networkRefusal": "admin", "checkRefusal": "admin", "canCheckNow": False}
    updates = FakeUpdates(**values)
    settings = make_settings(changes)

    def inspect(window: Any) -> None:
        control = find_object(window, name)
        assert control.isVisible()
        assert control.property("checked") is (mode != "policy")
        assert control.property("enabled") is (mode == "ordinary")
        texts = onboarding.visible_texts(window.contentItem())
        hint = (
            ("Раз в сутки, " if field == "checkAppUpdates" else "")
            + host
            + ". Скачивание — только по вашему нажатию."
            if mode == "ordinary"
            else "Не работает, пока включён офлайн-режим."
            if mode == "offline"
            else "Администратор отключил проверку обновлений на этом компьютере."
        )
        assert hint in texts
        before = settings.property(field)
        onboarding.click_item(control)
        app.processEvents()
        assert settings.property(field) is (not before if mode == "ordinary" else before)
        assert updates.calls == []

    try:
        _, _, messages = render_network(app, False, updates, settings, inspect)
        assert not messages, "\n".join(messages)
    finally:
        sip.delete(updates)
        sip.delete(settings)


@pytest.mark.parametrize("manual", [False, True], ids=["scheduled", "manual"])
def test_checking_is_visible_in_result_panel_and_retains_manual_result(
    app: Any, manual: bool
) -> None:
    updates = FakeUpdates(state="checking", manual=manual)
    settings = make_settings({})

    def inspect(window: Any) -> None:
        panel = find_object(window, "updatePanel")
        button = find_object(window, "checkNowButton")
        assert panel.isVisible() and button.isVisible()
        assert button.property("enabled") is False
        texts = onboarding.visible_texts(panel)
        assert "Проверяю обновления…" in texts
        assert "Ищу новую версию и проверяю подпись сведений о ней." in texts
        updates.update(state="uptodate", manual=True, checkedText="Проверено сегодня в 14:02")
        app.processEvents()
        assert button.property("enabled") is True
        assert "Установлена последняя версия" in onboarding.visible_texts(panel)
        QTest.qWait(3200)
        app.processEvents()
        assert "Установлена последняя версия" in onboarding.visible_texts(panel)
        assert "0.2.0" in onboarding.visible_texts(panel)
        assert updates.calls == []

    try:
        _, _, messages = render_network(app, False, updates, settings, inspect)
        assert not messages, "\n".join(messages)
    finally:
        sip.delete(updates)
        sip.delete(settings)


@pytest.mark.parametrize("state", ["idle", "disabled", "unavailable", "error-net", "skipped"])
def test_compact_panel_is_always_visible_with_manual_check(app: Any, state: str) -> None:
    updates = FakeUpdates(state=state, version="0.2.1" if state == "skipped" else "")
    settings = make_settings({"_checkAppUpdates": state != "disabled"})

    def inspect(window: Any) -> None:
        panel = find_object(window, "updatePanel")
        button = find_object(window, "checkNowButton")
        assert panel.isVisible() and button.isVisible() and button.property("enabled")
        assert "Вручную" not in onboarding.visible_texts(window.contentItem())
        assert "Проверить сейчас" in onboarding.visible_texts(panel)
        onboarding.click_item(button)
        assert updates.calls == ["checkNow"]

    try:
        _, _, messages = render_network(app, False, updates, settings, inspect)
        assert not messages, "\n".join(messages)
    finally:
        sip.delete(updates)
        sip.delete(settings)
