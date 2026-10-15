"""M7 US-6.1–6.3: реальные карточки/строка и безопасные снимки двух тем.

Все PNG — только ASTRA_VOICE_SNAPSHOT_DIR_MODEL_UPDATES либо tmp_path.
Модуль не пишет и не читает эталонные снимки design/refs.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest
from PyQt5 import sip
from PyQt5.QtCore import (
    QCoreApplication,
    QEvent,
    QObject,
    QPointF,
    QRectF,
    QSizeF,
    QUrl,
    pyqtProperty,
    qInstallMessageHandler,
)
from PyQt5.QtGui import QImage
from PyQt5.QtQml import QQmlApplicationEngine, QQmlComponent, QQmlEngine, QQmlExpression
from PyQt5.QtQuick import QQuickWindow
from PyQt5.QtTest import QTest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tests"))
sys.path.insert(0, str(REPO / "tests/unit"))
sys.path.insert(0, str(REPO / "tests/xvfb"))

from test_model_update_ui import RevisionPort, make_catalog  # noqa: E402
from test_models_live import (  # noqa: E402
    HEIGHT,
    WIDTH,
    LiveWindow,
    card_item,
    card_state,
    open_models_section,
    press,
    settle,
    visible_button,
    visible_texts,
    visual_tree,
)

from astra_voice.core import settings as settings_mod  # noqa: E402
from astra_voice.ui.bridges import SettingsBridge  # noqa: E402
from astra_voice.ui.icons import install_icon_provider  # noqa: E402
from astra_voice.ui.model_downloads import ModelDownloads  # noqa: E402
from astra_voice.ui.updates_bridge import UpdatesBridge  # noqa: E402
from astra_voice.updates.checker import UpdateStatus  # noqa: E402
from astra_voice.updates.model_updates import FAILED_MESSAGE, ModelUpdateNotice  # noqa: E402
from helpers.qt_app import get_qapplication  # noqa: E402
from helpers.scripted_model_port import FIRST  # noqa: E402

pytestmark = pytest.mark.xvfb
os.environ["QT_QUICK_CONTROLS_STYLE"] = "Default"
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QUICK_BACKEND", "software")


class FixedTheme(QObject):
    def __init__(self, dark: bool) -> None:
        super().__init__()
        self._dark = dark

    @pyqtProperty(bool, constant=True)
    def dark(self) -> bool:
        return self._dark


class RevisionWindow(LiveWindow):
    port: RevisionPort


@pytest.fixture(scope="module")
def app() -> Any:
    return get_qapplication()


@contextmanager
def model_window(
    app: Any, *, dark: bool = False, scene: str = "update-available", previous: bool = False
) -> Iterator[tuple[RevisionWindow, UpdatesBridge]]:
    port = RevisionPort()
    port.catalog = (FIRST,)
    port.records = {(FIRST.id, "r0"): "ok"}
    if scene == "new":
        port.catalog = (replace(FIRST, recommended=False),)
        port.records = {}
        port.current = None
    elif scene == "removed":
        port.catalog = (
            replace(FIRST, revision="r0", recommended=False, removed_from_catalog=True),
        )
        port.current = None
    if previous:
        port.previous = "r-1"
        port.records[(FIRST.id, "r-1")] = "ok"
    downloads = ModelDownloads(port)
    if scene == "new":
        downloads.accept_catalog(make_catalog(port), frozenset({FIRST.id}))
    bridge = SettingsBridge(
        settings_mod.from_dict({}),
        downloads=downloads,
        save=Mock(),
        device_provider=lambda: [],
        open_url=lambda _url: False,
        dialog_factory=lambda: "",
    )
    updates = UpdatesBridge()
    updates.set_status(UpdateStatus("uptodate"))
    downloads.modelRevisionChanged.connect(updates.clear_model_update)
    live = RevisionWindow(port, downloads, bridge)

    def handler(_mode: Any, _context: Any, message: str) -> None:
        live.messages.append(message)

    old_handler = qInstallMessageHandler(handler)
    engine = QQmlApplicationEngine()
    install_icon_provider(engine)
    theme = FixedTheme(dark)
    try:
        engine.rootContext().setContextProperty("themeSource", theme)
        engine.rootContext().setContextProperty("showOnboarding", False)
        engine.rootContext().setContextProperty("settingsBridge", bridge)
        engine.rootContext().setContextProperty("updatesBridge", updates)
        engine.load(QUrl.fromLocalFile(str(REPO / "qml/Main.qml")))
        assert len(engine.rootObjects()) == 1, live.messages
        live.window = engine.rootObjects()[0]
        assert isinstance(live.window, QQuickWindow)
        live.window.setWidth(WIDTH)
        live.window.setHeight(HEIGHT)
        background_component = QQmlComponent(engine)
        background_component.setData(
            b"import QtQuick 2.15; Rectangle { anchors.fill: parent; z: -1 }", QUrl()
        )
        background = background_component.create()
        assert background is not None
        background.setProperty("color", live.window.color())
        background.setParent(live.window.contentItem())
        background.setParentItem(live.window.contentItem())
        assert live.window.setProperty("freezeAnimations", True)
        live.window.show()
        QTest.qWait(180)
        app.processEvents()
        open_models_section(app, live.window)
        yield live, updates
        assert not live.messages, "\n".join(live.messages)
    finally:
        port.open_gates()
        downloads.shutdown()
        live.window = None
        sip.delete(engine)
        app.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        sip.delete(bridge)
        sip.delete(downloads)
        sip.delete(updates)
        sip.delete(theme)
        qInstallMessageHandler(old_handler)


def footer(live: LiveWindow) -> Any:
    bars = [
        item
        for item in visual_tree(live.root)
        if item.metaObject().indexOfProperty("updateClickable") >= 0
    ]
    assert len(bars) == 1
    return bars[0]


def theme_value(item: Any, name: str) -> Any:
    expression = QQmlExpression(QQmlEngine.contextForObject(item), item, "Theme." + name)
    result, undefined = expression.evaluate()
    assert not undefined and not expression.hasError(), expression.error().toString()
    return result


def bounds(item: Any, card: Any) -> QRectF:
    return QRectF(item.mapToItem(card, QPointF()), QSizeF(item.width(), item.height()))


def within(item: Any, ancestor: Any) -> bool:
    while item is not None:
        if item is ancestor:
            return True
        item = item.parentItem()
    return False


def assert_card_geometry_and_badges(card: Any) -> None:
    badges = [
        item
        for item in visual_tree(card)
        if item.isVisible() and item.objectName() in {"modelExtraBadge", "modelStateBadge"}
    ]
    assert len(badges) <= 2
    state_badges = [item for item in badges if item.objectName() == "modelStateBadge"]
    assert len(state_badges) == 1
    buttons = [
        item for item in visual_tree(card) if item.isVisible() and item.property("small") is True
    ]
    for button in buttons:
        rect = bounds(button, card)
        assert rect.left() >= 0 and rect.right() <= card.width()
        assert rect.top() >= 0 and rect.bottom() <= card.height()
        assert button.width() >= button.implicitWidth() - 1
        for other in buttons:
            if other is not button:
                assert not rect.intersects(bounds(other, card))
        for label in visual_tree(card):
            if (
                label.isVisible()
                and label.property("text")
                and label.property("textFormat") is not None
                and not any(within(label, owner) for owner in buttons)
            ):
                assert not rect.intersects(bounds(label, card)), (
                    button.property("text"),
                    label.property("text"),
                    rect,
                    bounds(label, card),
                )
    # Все строки каталога и раскрытая причина — буквальный текст, без rich text.
    literal = {card.property("modelTitle"), card.property("purpose"), card.property("reason")}
    for item in visual_tree(card):
        if item.isVisible() and item.property("text") in literal - {"", None}:
            assert item.property("textFormat") == 0  # Text.PlainText


def screenshot(app: Any, live: LiveWindow, tmp_path: Path, name: str) -> Path:
    directory = Path(os.environ.get("ASTRA_VOICE_SNAPSHOT_DIR_MODEL_UPDATES") or tmp_path)
    directory.mkdir(parents=True, exist_ok=True)
    # Software/offscreen не всегда умеет grabWindow; фон создан вместе с окном.
    image = QImage()
    pending = None
    for _ in range(40):
        image = live.window.grabWindow()
        if image.isNull():
            if pending is None:
                pending = live.window.contentItem().grabToImage()
            image = pending.image()
        if not image.isNull():
            break
        QTest.qWait(25)
        app.processEvents()
    assert not image.isNull(), "окно не отдало снимок"
    ratio = image.devicePixelRatio()
    assert (image.width(), image.height()) == (round(WIDTH * ratio), round(HEIGHT * ratio))
    if ratio != 1:
        image = image.scaled(WIDTH, HEIGHT)
        image.setDevicePixelRatio(1)
    assert (image.width(), image.height()) == (1024, 620)
    path = directory / name
    assert image.save(str(path), "PNG")
    return path


@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
@pytest.mark.parametrize(
    "scene", ["update-available", "updating", "update-failed", "new", "removed", "model-update"]
)
def test_model_update_states_and_snapshots(
    app: Any, tmp_path: Path, dark: bool, scene: str
) -> None:
    with model_window(app, dark=dark, scene=scene) as (live, updates):
        if scene in {"update-available", "updating", "update-failed"}:
            assert card_state(live, FIRST.id) == "update-available"
            assert "Обновление доступно" in visible_texts(card_item(live, FIRST.id))
            update = visible_button(card_item(live, FIRST.id), "Обновить")
            if scene != "update-available":
                if scene == "update-failed":
                    live.port.next_failure = "selfcheck"
                    live.port.revision_gate.set()
                press(update)
                settle(live, lambda: card_state(live, FIRST.id) == scene, scene)
                texts = visible_texts(card_item(live, FIRST.id))
                if scene == "updating":
                    assert "Обновляю…" in texts
                    assert visible_button(card_item(live, FIRST.id), "Отмена").isEnabled()
                else:
                    assert FAILED_MESSAGE in texts
                    assert visible_button(card_item(live, FIRST.id), "Повторить").isEnabled()
                    press(visible_button(card_item(live, FIRST.id), "Подробнее"))
                    assert "selfcheck" in visible_texts(card_item(live, FIRST.id))
        elif scene == "new":
            assert "Новое" in visible_texts(card_item(live, FIRST.id))
        elif scene == "removed":
            texts = visible_texts(card_item(live, FIRST.id))
            assert "Снята с каталога — обновлений не будет" in texts
            assert visible_button(card_item(live, FIRST.id), "Сделать рабочей").isEnabled()
            assert visible_button(card_item(live, FIRST.id), "Удалить").isEnabled()
        else:
            updates.set_model_update(ModelUpdateNotice(FIRST.id, FIRST.name, "r0", "r1"))
            app.processEvents()
            assert footer(live).property("updateText") == "Обновление модели: " + FIRST.name
            assert footer(live).property("updateClickable") is False
            assert footer(live).property("updateAccent") is False
        assert "Что нового" not in visible_texts(card_item(live, FIRST.id))
        card = card_item(live, FIRST.id)
        assert_card_geometry_and_badges(card)
        badges = {
            item.property("text")
            for item in visual_tree(card)
            if item.isVisible() and item.objectName() in {"modelExtraBadge", "modelStateBadge"}
        }
        if scene in {"update-available", "updating"}:
            assert badges == {
                "Установлена и активна",
                "Обновление доступно" if scene == "update-available" else "Обновляю…",
            }
            assert "Рекомендуем" not in badges
        if scene == "update-available":
            state_badge = next(
                item for item in visual_tree(card) if item.objectName() == "modelStateBadge"
            )
            assert state_badge.property("color") == theme_value(card, "warningBg")
            assert state_badge.property("textColor") == theme_value(card, "warningInk")
        elif scene == "model-update":
            assert footer(live).property("updateColor") == theme_value(card, "fgMuted")
        path = screenshot(app, live, tmp_path, f"{scene}-{'dark' if dark else 'light'}.png")
        assert path.is_file()
        if scene in {"update-available", "updating", "update-failed"}:
            if scene == "update-available":
                press(visible_button(card_item(live, FIRST.id), "Обновить"))
                settle(live, live.port.revision_started.is_set, "начало обновления")
            elif scene == "update-failed":
                press(visible_button(card_item(live, FIRST.id), "Повторить"))
            live.port.revision_gate.set()
            settle(live, lambda: card_state(live, FIRST.id) == "ok", "успешное обновление")
            assert live.port.current == (FIRST.id, FIRST.revision)
            assert "Обновляю…" not in visible_texts(card_item(live, FIRST.id))


@pytest.mark.parametrize("previous", [False, True])
@pytest.mark.parametrize("active", [False, True])
def test_previous_link_only_for_active_with_previous(
    app: Any, previous: bool, active: bool
) -> None:
    with model_window(
        app, previous=previous, scene="update-available" if active else "removed"
    ) as (live, _updates):
        texts = visible_texts(card_item(live, FIRST.id))
        assert ("Вернуть предыдущую версию" in texts) is (active and previous)
        assert_card_geometry_and_badges(card_item(live, FIRST.id))


@pytest.mark.parametrize(
    ("state", "hidden", "model_visible"),
    [
        ("idle", False, True),
        ("disabled", False, True),
        ("uptodate", True, True),
        ("uptodate", False, False),
        ("checking", False, False),
        ("available", False, False),
        ("unavailable", False, False),
        ("error-net", False, False),
        ("policy-locked", False, False),
    ],
)
def test_program_status_takes_priority(
    app: Any, state: str, hidden: bool, model_visible: bool
) -> None:
    with model_window(app) as (live, updates):
        updates.set_model_update(ModelUpdateNotice(FIRST.id, FIRST.name, "r0", "r1"))
        bar = footer(live)
        assert bar.setProperty("updateState", state)
        if hidden:
            assert bar.setProperty("uptodateShown", False)
        app.processEvents()
        assert str(bar.property("updateText")).startswith("Обновление модели:") is model_visible
        if model_visible:
            assert bar.property("updateClickable") is False
            assert bar.property("updateAccent") is False


def test_success_and_revert_clear_model_notice(app: Any) -> None:
    with model_window(app) as (live, updates):
        notice = ModelUpdateNotice(FIRST.id, FIRST.name, "r0", "r1")
        updates.set_model_update(notice)
        press(visible_button(card_item(live, FIRST.id), "Обновить"))
        live.port.revision_gate.set()
        settle(live, lambda: card_state(live, FIRST.id) == "ok", "установка ревизии")
        assert updates.modelUpdateText == ""
        updates.set_model_update(notice)
        press(visible_button(card_item(live, FIRST.id), "Вернуть предыдущую версию"))
        settle(
            live,
            lambda: live.port.current == (FIRST.id, "r0") and live.downloads._model_thread is None,
            "откат",
        )
        assert updates.modelUpdateText == ""


def test_failed_update_actions_hide_when_model_becomes_inactive(app: Any) -> None:
    with model_window(app, previous=True) as (live, _updates):
        live.port.next_failure = "selfcheck"
        live.port.revision_gate.set()
        press(visible_button(card_item(live, FIRST.id), "Обновить"))
        settle(live, lambda: card_state(live, FIRST.id) == "update-failed", "ошибка ревизии")
        assert "Повторить" in visible_texts(card_item(live, FIRST.id))
        live.port.current = None
        live.downloads.modelsChanged.emit()
        app.processEvents()
        texts = visible_texts(card_item(live, FIRST.id))
        assert FAILED_MESSAGE in texts
        assert not {"Повторить", "Обновить", "Вернуть предыдущую версию"} & texts
        assert visible_button(card_item(live, FIRST.id), "Сделать рабочей").isEnabled()
