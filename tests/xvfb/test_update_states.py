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
from PyQt5.QtGui import QImage
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
NOTES = (
    "• Каталог моделей: 12 моделей, память в работе замеряется на вашем компьютере\n"
    "• Автозапуск при входе в систему\n"
    "• Исправлено: пилюля перекрывалась панелью при смене раскладки"
)

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

    def __init__(self, **values: Any) -> None:
        super().__init__()
        self.calls: list[str] = []
        self._values: dict[str, Any] = {
            "state": "disabled",
            "version": "",
            "notes": "",
            "snoozed": False,
            "manual": False,
            "checkedText": "",
            "releasePageAvailable": False,
            "checkRefusal": "",
            "networkRefusal": "",
            "canCheckNow": True,
            "restState": "idle",
        }
        self._values.update(values)

    state = pyqtProperty(str, _value("state"), notify=statusChanged)
    version = pyqtProperty(str, _value("version"), notify=statusChanged)
    notes = pyqtProperty(str, _value("notes"), notify=statusChanged)
    snoozed = pyqtProperty(bool, _value("snoozed"), notify=statusChanged)
    manual = pyqtProperty(bool, _value("manual"), notify=statusChanged)
    checkedText = pyqtProperty(str, _value("checkedText"), notify=statusChanged)
    releasePageAvailable = pyqtProperty(bool, _value("releasePageAvailable"), notify=statusChanged)
    checkRefusal = pyqtProperty(str, _value("checkRefusal"), notify=networkChanged)
    networkRefusal = pyqtProperty(str, _value("networkRefusal"), notify=networkChanged)
    canCheckNow = pyqtProperty(bool, _value("canCheckNow"), notify=networkChanged)
    restState = pyqtProperty(str, _value("restState"), notify=networkChanged)

    def update(self, **values: Any) -> None:
        self._values.update(values)
        self.statusChanged.emit()
        self.networkChanged.emit()

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


def render_footer(app: Any, dark: bool, values: dict[str, Any]) -> tuple[QImage, Any, list[str]]:
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
        onboarding.select_section(app, window, "network")
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
        "error-net": "Не удалось скачать обновление · Повторить",
        "skipped": "Версия 0.2.1 пропущена · Показать",
        "idle": None,
    }[case]
    if expected is not None:
        assert expected in texts
    else:
        assert not {text for text in texts if "бновлен" in text}
    assert "v0.2.0" in texts
    save_snapshot(image, f"footer-{case}-{'dark' if dark else 'light'}.png")


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
    # Скрыто до своих вех: тумблер моделей (v1.0), «Обновить из файла…» и ключи (M8).
    assert "Проверять обновления моделей" not in texts
    assert "Обновить из файла…" not in texts
    assert not {text for text in texts if "trust" in text.lower()}
    panel_title = {
        "panel-available": "Доступна версия 0.2.1",
        "panel-checking": "Проверяю обновления…",
        "panel-uptodate": "Установлена последняя версия",
        "panel-skipped": "Версия 0.2.1 пропущена",
        "panel-unavailable": "Источник обновлений недоступен",
    }.get(case)
    if panel_title is not None:
        assert panel_title in texts
    if case == "panel-available":
        assert {"Что нового", NOTES, "Страница выпуска", "Пропустить эту версию"} <= texts
    if case == "network-offline":
        assert "Недоступно: включён офлайн-режим" in texts
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
        notes = find_object(window, "updateNotes")
        assert notes.property("textFormat") == 0  # Text.PlainText
        for name in ("updateReleasePage", "updateSkip", "updateRemindLater"):
            onboarding.click_item(find_object(window, name))
            app.processEvents()

    try:
        render_network(app, False, updates, settings, inspect)
        assert updates.calls == ["openReleasePage", "skipVersion", "remindLater"]
    finally:
        sip.delete(updates)
        sip.delete(settings)


def test_release_page_button_hidden_without_opener(app: Any) -> None:
    updates = FakeUpdates(**{**AVAILABLE, "releasePageAvailable": False})
    settings = make_settings({})
    try:
        _, texts, _ = render_network(app, False, updates, settings)
        assert "Страница выпуска" not in texts
        assert "Пропустить эту версию" in texts
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
        onboarding.click_item(find_object(window, "offlineToggle"))

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
