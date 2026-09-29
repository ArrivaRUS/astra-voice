"""Раздел «Модели» на настоящем мосте: очередь → ``SettingsBridge`` → карточка и полоса.

В остальных xvfb-тестах карточку кормит подставной ``FakeSettings``. Здесь
``Main.qml`` получает настоящий ``SettingsBridge`` над настоящей очередью
``ModelDownloads``; подставной только загрузчик (``helpers.scripted_model_port``):
без сети и файлов, но в рабочем потоке очереди, с событиями через канал задания.
Состояния — спецификация §5.4 (14–17а) и полоса загрузки §10.3.

Снимков модуль не пишет: эталоны карточки остаются за ``test_onboarding.py``.
"""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest
from PyQt5 import sip
from PyQt5.QtCore import (
    QCoreApplication,
    QEvent,
    QMetaObject,
    QObject,
    QPoint,
    QPointF,
    Qt,
    QUrl,
    pyqtProperty,
    pyqtSignal,
    qInstallMessageHandler,
)
from PyQt5.QtQml import QQmlApplicationEngine
from PyQt5.QtQuick import QQuickView, QQuickWindow
from PyQt5.QtTest import QTest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from astra_voice.core import settings as settings_mod
from astra_voice.models.downloader import DownloadError
from astra_voice.ui.bridges import OnboardingController, SettingsBridge
from astra_voice.ui.icons import install_icon_provider
from astra_voice.ui.model_downloads import ModelDownloads
from helpers.qt_app import get_qapplication  # noqa: E402
from helpers.scripted_model_port import FIRST, SECOND, ScriptedModelPort  # noqa: E402

pytestmark = pytest.mark.xvfb
REPO = Path(__file__).resolve().parents[2]
WIDTH, HEIGHT = 1024, 620

os.environ["QT_QUICK_CONTROLS_STYLE"] = "Default"
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QUICK_BACKEND", "software")


class FakeTheme(QObject):
    changed = pyqtSignal()

    @pyqtProperty(bool, notify=changed)
    def dark(self) -> bool:
        return False


@dataclass
class LiveWindow:
    port: ScriptedModelPort
    downloads: ModelDownloads
    bridge: SettingsBridge
    window: Any = None
    messages: list[str] = field(default_factory=list)

    @property
    def root(self) -> Any:
        return self.window.contentItem()


def visual_tree(root: Any) -> Iterator[Any]:
    """childItems включает делегаты Repeater, отсутствующие в QObject.children()."""
    yield root
    for child in root.childItems():
        yield from visual_tree(child)


def visible_texts(root: Any) -> set[str]:
    return {
        item.property("text")
        for item in visual_tree(root)
        if item.isVisible() and isinstance(item.property("text"), str)
    }


def visible_button(root: Any, text: str) -> Any:
    buttons = [
        item
        for item in visual_tree(root)
        if item.isVisible()
        and item.property("text") == text
        and item.metaObject().indexOfSignal(b"clicked()") >= 0
    ]
    assert len(buttons) == 1, f"ожидалась одна кнопка «{text}», найдено {len(buttons)}"
    return buttons[0]


def press(button: Any) -> None:
    """Нажатие так же, как в test_onboarding.py: сигнал clicked самой кнопки."""
    assert button.isEnabled()
    QMetaObject.invokeMethod(button, "clicked", Qt.DirectConnection)


def click_item(item: Any) -> None:
    assert item.isVisible()
    position = item.mapToScene(QPointF(item.width() / 2, item.height() / 2))
    QTest.mouseClick(item.window(), Qt.LeftButton, Qt.NoModifier, position.toPoint())


def settle(
    live: LiveWindow, predicate: Callable[[], bool], what: str, timeout_s: float = 5.0
) -> None:
    """Ждёт условия, разбирая события задания и удаляя пересобранные делегаты.

    Каждое ``modelsChanged`` пересобирает делегаты Repeater, поэтому элементы
    карточек ищутся заново после каждого ожидания и нажатия.
    """
    deadline = time.monotonic() + timeout_s
    while True:
        QTest.qWait(10)
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        if predicate():
            return
        if time.monotonic() > deadline:
            pytest.fail(f"не дождались: {what}; сообщения Qt: {live.messages}")


def card_item(live: LiveWindow, model_id: str) -> Any:
    cards = [
        item
        for item in visual_tree(live.root)
        if item.isVisible() and item.property("modelId") == model_id
    ]
    assert len(cards) == 1, f"ожидалась одна карточка {model_id}, найдено {len(cards)}"
    return cards[0]


def card_state(live: LiveWindow, model_id: str) -> str:
    return str(card_item(live, model_id).property("cardState"))


def strip_item(live: LiveWindow) -> Any:
    strips = [
        item
        for item in visual_tree(live.root)
        if item.metaObject().indexOfProperty("downloadState") >= 0
        and item.metaObject().indexOfProperty("sourceText") >= 0
    ]
    assert len(strips) == 1, f"ожидалась одна полоса загрузки, найдено {len(strips)}"
    return strips[0]


def strip_state(live: LiveWindow) -> str:
    strip = strip_item(live)
    return str(strip.property("downloadState")) if strip.isVisible() else "hidden"


def settings_sidebar(window: Any) -> Any:
    return next(
        item
        for item in visual_tree(window.contentItem())
        if item.metaObject().indexOfProperty("debugCurrent") >= 0
    )


def open_models_section(app: Any, window: Any) -> None:
    sidebar = settings_sidebar(window)
    descriptions = list(sidebar.property("sections").toVariant())
    index = [description["key"] for description in descriptions].index("models")
    items = [
        item
        for item in visual_tree(sidebar)
        if item.isVisible() and item.property("title") == descriptions[index]["title"]
    ]
    assert len(items) == 1, f"ожидался один пункт меню «{descriptions[index]['title']}»"
    click_item(items[0])
    app.processEvents()
    assert sidebar.property("currentIndex") == index
    # Переход между разделами — плавное появление (motion.duration.enter = 160).
    QTest.qWait(220)
    app.processEvents()
    # Указатель — в угол без элементов с наведением, как в test_onboarding.py.
    QTest.mouseMove(window, QPoint(1, HEIGHT - 2))
    app.processEvents()


@contextmanager
def live_models_window(app: Any) -> Iterator[LiveWindow]:
    """Настоящий Main.qml, открытый на разделе «Модели», над живой очередью."""
    port = ScriptedModelPort()
    downloads = ModelDownloads(port)
    bridge = SettingsBridge(
        settings_mod.from_dict({}),
        downloads=downloads,
        save=Mock(),
        # Ни микрофонов, ни файлового менеджера, ни диалогов на машине прогона.
        device_provider=lambda: [],
        open_url=lambda _url: False,
        dialog_factory=lambda: "",
    )
    live = LiveWindow(port, downloads, bridge)

    def handler(_mode: Any, _context: Any, message: str) -> None:
        live.messages.append(message)

    previous = qInstallMessageHandler(handler)
    engine = QQmlApplicationEngine()
    install_icon_provider(engine)
    theme = FakeTheme()
    try:
        engine.rootContext().setContextProperty("themeSource", theme)
        engine.rootContext().setContextProperty("showOnboarding", False)
        engine.rootContext().setContextProperty("settingsBridge", bridge)
        engine.load(QUrl.fromLocalFile(str(REPO / "qml/Main.qml")))
        roots = engine.rootObjects()
        assert len(roots) == 1, live.messages
        window = roots[0]
        assert isinstance(window, QQuickWindow)
        window.setWidth(WIDTH)
        window.setHeight(HEIGHT)
        # «Модель готова» без заморозки исчезает через 3 с — проверка не должна гнаться за ней.
        assert window.setProperty("freezeAnimations", True)
        window.show()
        QTest.qWait(180)
        app.processEvents()
        assert window.isVisible()
        live.window = window
        open_models_section(app, window)
        yield live
    finally:
        # Ворота открыты до остановки: рабочий поток не ждёт до своего предела.
        port.open_gates()
        downloads.shutdown()
        try:
            live.window = None
            sip.delete(engine)
            app.processEvents()
            QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
            sip.delete(bridge)
            sip.delete(downloads)
            sip.delete(theme)
            app.processEvents()
        finally:
            qInstallMessageHandler(previous)


def assert_no_messages(live: LiveWindow, case: str) -> None:
    assert not live.messages, f"{case}: сообщения Qt\n" + "\n".join(live.messages)


@pytest.fixture(scope="session")
def models_app() -> Any:
    return get_qapplication()


def select_and_download(live: LiveWindow, *model_ids: str) -> None:
    """Отметить карточки щелчком и нажать «Скачать выбранное», как человек."""
    for model_id in model_ids:
        click_item(card_item(live, model_id))

        def selected(model_id: str = model_id) -> bool:
            return card_item(live, model_id).property("selected") is True

        settle(live, selected, f"отметка карточки {model_id}")
    press(visible_button(live.root, "Скачать выбранное"))


def test_live_queue_download_verify_ready(models_app: Any) -> None:
    with live_models_window(models_app) as live:
        select_and_download(live, FIRST.id, SECOND.id)
        settle(live, lambda: card_state(live, SECOND.id) == "queued", "очередь из двух карточек")

        # §5.4, 14–15: активная и ожидающая карточки, у обеих «Отмена».
        first, second = card_item(live, FIRST.id), card_item(live, SECOND.id)
        assert first.property("cardState") == "downloading"
        assert first.property("statusLabel") == "Загружается"
        assert first.property("selectionMark") == "locked"
        visible_button(first, "Отмена")
        assert second.property("cardState") == "queued"
        assert second.property("statusLabel") == "В очереди"
        visible_button(second, "Отмена")

        # §10.3, состояние 1: источник, счётчик и определённая дорожка из рабочего потока.
        settle(
            live,
            lambda: (
                strip_item(live).property("sourceText") == "Скачиваю с huggingface.co"
                and strip_item(live).property("progress") > 0
            ),
            "источник и прогресс в полосе",
        )
        strip = strip_item(live)
        assert strip.isVisible()
        assert strip.property("downloadState") == "downloading"
        assert strip.property("title") == "Загружается Первая модель"
        assert strip.property("downloadCounter") == "1 из 2"
        assert str(strip.property("tail")).startswith("Скачиваю с huggingface.co · ")
        assert strip.property("tail") in visible_texts(strip)
        assert strip.property("progress") == pytest.approx(113_000_000 / 370_000_000)

        # §5.4, 16 и §10.3, состояние 2: установка и самопроверка.
        live.port.download_gate.set()
        settle(live, lambda: card_state(live, FIRST.id) == "verifying", "проверка модели")
        first = card_item(live, FIRST.id)
        assert first.property("statusLabel") == "Проверяю…"
        assert "Отмена недоступна" in visible_texts(first)
        assert "Отмена" not in visible_texts(first)
        strip = strip_item(live)
        assert (strip.property("downloadState"), strip.property("title")) == (
            "verifying",
            "Проверяю модель…",
        )
        assert strip.property("sourceText") == ""

        # §10.3, состояние 3: «Модель готова», обе карточки — в «Установленных».
        live.port.install_gate.set()
        settle(live, lambda: strip_state(live) == "done", "готовая очередь")
        assert strip_item(live).property("title") == "Модель готова"
        assert "Модель готова" in visible_texts(strip_item(live))
        assert card_item(live, FIRST.id).property("statusLabel") == "Установлена и активна"
        assert card_item(live, SECOND.id).property("statusLabel") == "Установлена"
        assert "Установленные · 2" in visible_texts(live.root)
    assert_no_messages(live, "живая очередь до готовности")


@pytest.mark.parametrize("where", ["strip", "card"])
def test_live_failed_download_retry(models_app: Any, where: str) -> None:
    with live_models_window(models_app) as live:
        live.port.failures[FIRST.id] = DownloadError("no-network")
        live.port.open_gates()
        select_and_download(live, FIRST.id)
        settle(live, lambda: strip_state(live) == "failed", "ошибка в полосе")

        # §5.4, 17: ошибка в нижней строке, «Повторить», выбор снова доступен.
        card = card_item(live, FIRST.id)
        assert card.property("cardState") == "failed"
        assert card.property("selectionMark") == "off"
        assert "Не удалось загрузить модель — нет связи с сервером" in visible_texts(card)
        # §10.3, состояние 4.
        strip = strip_item(live)
        assert strip.property("title") == "Не удалось загрузить модель"

        if where == "strip":
            press(visible_button(strip, "Повторить"))
        else:
            press(visible_button(card, "Повторить"))
        settle(live, lambda: strip_state(live) == "done", "повтор до готовности")
        assert card_item(live, FIRST.id).property("statusLabel") == "Установлена и активна"
        assert live.port.download_calls == [FIRST.id, FIRST.id]
    assert_no_messages(live, f"повтор из {where}")


@pytest.mark.parametrize("action", ["Повторить", "Отмена"])
def test_live_no_space_pause(models_app: Any, action: str) -> None:
    with live_models_window(models_app) as live:
        # При выборе место было, к началу загрузки кончилось: иначе «Скачать выбранное»
        # неактивна и до паузы не дойти.
        live.port.job_space = False
        live.port.open_gates()
        select_and_download(live, FIRST.id)
        settle(live, lambda: card_state(live, FIRST.id) == "paused-no-space", "пауза по месту")

        # §5.4, 17а: сообщение с размером, «Повторить» и «Отмена», карточка подсвечена.
        card = card_item(live, FIRST.id)
        assert card.property("highlighted") is True
        assert card.property("selectionMark") == "locked"
        assert "Не хватает места — освободите 13 МБ" in visible_texts(card)
        visible_button(card, "Повторить")
        visible_button(card, "Отмена")
        # §10.3, состояние 5: уточнение и «Открыть папку моделей» (её не нажимаем).
        settle(live, lambda: strip_state(live) == "no-space", "полоса «нет места»")
        strip = strip_item(live)
        assert strip.property("title") == "Не хватает места на диске"
        assert "нужно ещё 13 МБ" in visible_texts(strip)
        visible_button(strip, "Открыть папку моделей")

        if action == "Повторить":
            live.port.job_space = None
        press(visible_button(card_item(live, FIRST.id), action))
        if action == "Повторить":
            settle(live, lambda: strip_state(live) == "done", "готово после паузы")
            assert card_item(live, FIRST.id).property("badge") == "active"
            assert live.port.discarded == []
        else:
            settle(live, lambda: strip_state(live) == "hidden", "полоса скрыта после отмены")
            assert card_item(live, FIRST.id).property("cardState") == "available"
            assert live.port.discarded == [FIRST.id]
            assert live.port.download_calls == []
    assert_no_messages(live, f"пауза по месту, {action}")


def test_live_cancel_queued_then_active(models_app: Any) -> None:
    with live_models_window(models_app) as live:
        select_and_download(live, FIRST.id, SECOND.id)
        settle(live, live.port.downloading.is_set, "загрузка первой модели")

        # «Отмена» у ожидающей — убрать из очереди (§5.4, 15).
        press(visible_button(card_item(live, SECOND.id), "Отмена"))
        settle(live, lambda: card_state(live, SECOND.id) == "available", "вторая вне очереди")
        assert strip_item(live).property("downloadCounter") == ""
        assert strip_state(live) == "downloading"

        # «Отмена» у активной — загрузка прервана, частичные файлы удалены (§5.4, 14).
        press(visible_button(card_item(live, FIRST.id), "Отмена"))
        settle(
            live,
            lambda: strip_state(live) == "hidden" and live.downloads._model_thread is None,
            "отмена активной загрузки",
        )
        assert card_state(live, FIRST.id) == "available"
        assert "Отмена" not in visible_texts(card_item(live, FIRST.id))
        assert live.port.discarded == [FIRST.id]
        assert live.port.download_calls == [FIRST.id]
    assert_no_messages(live, "отмена очереди")


@contextmanager
def live_onboarding_window(app: Any) -> Iterator[LiveWindow]:
    """Настоящий Onboarding.qml на шаге 2 над настоящим контроллером и живой очередью."""
    port = ScriptedModelPort()
    downloads = ModelDownloads(port)
    settings = settings_mod.Settings(extra={"onboarding_language_set": True, "onboarding_step": 2})
    bridge = SettingsBridge(settings, downloads=downloads, save=Mock(), device_provider=lambda: [])
    controller = OnboardingController(
        bridge,
        settings=settings,
        downloads=downloads,
        device_provider=lambda: [],
        dialog_factory=lambda: "",
        open_url=lambda _url: False,
    )
    live = LiveWindow(port, downloads, bridge)

    def handler(_mode: Any, _context: Any, message: str) -> None:
        live.messages.append(message)

    previous = qInstallMessageHandler(handler)
    view = QQuickView()
    install_icon_provider(view.engine())
    theme = FakeTheme()
    try:
        view.rootContext().setContextProperty("onboarding", controller)
        view.rootContext().setContextProperty("themeSource", theme)
        view.setResizeMode(QQuickView.SizeRootObjectToView)
        view.resize(WIDTH, HEIGHT)
        view.setSource(QUrl.fromLocalFile(str(REPO / "qml/onboarding/Onboarding.qml")))
        assert view.status() == QQuickView.Ready, [error.toString() for error in view.errors()]
        root = view.rootObject()
        assert root is not None
        assert root.setProperty("freezeAnimations", True)
        view.show()
        QTest.qWait(180)
        app.processEvents()
        assert root.property("step") == 2
        live.window = view
        QTest.mouseMove(view, QPoint(1, HEIGHT - 2))
        app.processEvents()
        yield live
    finally:
        port.open_gates()
        downloads.shutdown()
        try:
            live.window = None
            sip.delete(view)
            app.processEvents()
            QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
            controller.shutdown()
            sip.delete(controller)
            sip.delete(bridge)
            sip.delete(downloads)
            sip.delete(theme)
            app.processEvents()
        finally:
            qInstallMessageHandler(previous)


def test_live_onboarding_strip_retry(models_app: Any) -> None:
    """Шаг 2 мастера: «Повторить» полосы после ошибки снова качает модель (§10.3, 4)."""
    with live_onboarding_window(models_app) as live:
        live.port.failures[FIRST.id] = DownloadError("no-network")
        live.port.open_gates()
        click_item(card_item(live, FIRST.id))
        settle(
            live,
            lambda: card_item(live, FIRST.id).property("selected") is True,
            "отметка карточки в мастере",
        )
        # «Продолжить» вдобавок сменил бы шаг; очередь запускаем тем же слотом, что и он.
        live.downloads.startSelectedDownloads()
        settle(live, lambda: strip_state(live) == "failed", "ошибка в полосе мастера")
        strip = strip_item(live)
        assert strip.property("title") == "Не удалось загрузить модель"
        assert "Не удалось загрузить модель — нет связи с сервером" in visible_texts(
            card_item(live, FIRST.id)
        )

        press(visible_button(strip, "Повторить"))
        settle(live, lambda: strip_state(live) == "done", "повтор в мастере до готовности")
        assert strip_item(live).property("title") == "Модель готова"
        assert live.port.download_calls == [FIRST.id, FIRST.id]
    assert_no_messages(live, "повтор из полосы мастера")
