"""Кольцо фокуса в разделе «Модели»: только с клавиатуры и целиком внутри клипа тела.

Регресс UI-20261009-01 (docs/issues/2026-10-12-model-filter-outline-rca.md): щелчок мышью
по чипу фильтра включал кольцо фокуса, а клип прокручиваемого тела срезал его верх
(у «Все языки» — ещё и левый край), и кольцо выглядело «скобкой».

Окно — настоящий Main.qml через render_settings из test_onboarding (FakeSettings, offscreen
или xcb). Пиксельные проверки здесь доказывают отрисовку кольца, а не поведение фокуса
на живом рабочем столе (урок 011): живая проверка мыши и Tab — на ALSE.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest
import test_onboarding as onboarding
from PyQt5.QtCore import QPointF, QRectF, Qt
from PyQt5.QtGui import QColor, QImage
from PyQt5.QtQuick import QQuickItem, QQuickWindow
from PyQt5.QtTest import QTest
from test_onboarding import (
    FakeSettings,
    activate_for_keyclick,
    assert_no_messages,
    grab_frame,
    render_settings,
    settle_pointer,
    theme_color,
    theme_number,
    visual_tree,
)

from helpers.qt_app import get_qapplication

pytestmark = pytest.mark.xvfb

CHIPS = ("allLanguagesChip", "domesticChip")
#: Кольцо: зазор focusOffset и линия focusWidth снаружи элемента (сквозное правило 1).
RING_OUTSET = theme_number("focusOffset") + theme_number("focusWidth")
#: Полоса вокруг чипа, в которой не должно быть ни одного пикселя кольца.
NO_RING_BAND = 6
#: Допуск на сглаживание: канал цвета пикселя линии кольца не дальше этого от токена.
COLOR_TOLERANCE = 24
#: Предел нажатий Tab, чтобы обход не зациклился на сломанной цепочке фокуса.
TAB_LIMIT = 60


@pytest.fixture(scope="module")
def qt_app() -> Any:
    return get_qapplication()


def named(window: QQuickWindow, name: str) -> QQuickItem:
    items = [item for item in visual_tree(window.contentItem()) if item.objectName() == name]
    assert len(items) == 1, f"ожидался один элемент {name}, найдено {len(items)}"
    return items[0]


def scene_rect(item: QQuickItem) -> QRectF:
    top_left = item.mapToScene(QPointF(0, 0))
    return QRectF(top_left.x(), top_left.y(), item.width(), item.height())


def ring_item(chip: QQuickItem) -> QQuickItem:
    """Кольцо фокуса чипа — единственный дочерний элемент, выходящий за чип."""
    rings = [
        child
        for child in chip.childItems()
        if child.width() > chip.width() + 1 and child.height() > chip.height() + 1
    ]
    assert len(rings) == 1, f"{chip.objectName()}: не найдено единственное кольцо фокуса"
    return rings[0]


def is_ring(color: QColor, ring: QColor) -> bool:
    return all(
        abs(channel - target) <= COLOR_TOLERANCE
        for channel, target in zip(
            (color.red(), color.green(), color.blue()),
            (ring.red(), ring.green(), ring.blue()),
            strict=True,
        )
    )


def ring_pixels(image: QImage, ring: QColor, x0: float, y0: float, x1: float, y1: float) -> int:
    """Число пикселей цвета кольца в прямоугольнике [x0, x1) × [y0, y1) логических px."""
    dpr = image.devicePixelRatio()
    left = max(0, int(x0 * dpr))
    top = max(0, int(y0 * dpr))
    right = min(image.width(), int(x1 * dpr))
    bottom = min(image.height(), int(y1 * dpr))
    return sum(
        is_ring(image.pixelColor(x, y), ring)
        for y in range(top, bottom)
        for x in range(left, right)
    )


def ring_sides(image: QImage, ring: QColor, chip: QRectF) -> dict[str, int]:
    """Пиксели кольца на каждой стороне: линия 2 px снаружи зазора, середина стороны."""
    width = theme_number("focusWidth")
    left, top, right, bottom = chip.left(), chip.top(), chip.right(), chip.bottom()
    # Середина стороны — без скруглённых углов кольца (радиус focusRadius).
    radius = theme_number("focusRadius")
    x0, x1 = left + radius, right - radius
    y0, y1 = top + radius - RING_OUTSET, bottom - radius + RING_OUTSET
    return {
        "top": ring_pixels(image, ring, x0, top - RING_OUTSET, x1, top - RING_OUTSET + width),
        "bottom": ring_pixels(
            image, ring, x0, bottom + RING_OUTSET - width, x1, bottom + RING_OUTSET
        ),
        "left": ring_pixels(image, ring, left - RING_OUTSET, y0, left - RING_OUTSET + width, y1),
        "right": ring_pixels(image, ring, right + RING_OUTSET - width, y0, right + RING_OUTSET, y1),
    }


def tab_until(app: Any, window: QQuickWindow, name: str) -> list[str]:
    """Жмёт Tab, пока фокус не встанет на элемент name; возвращает пройденный путь."""
    path: list[str] = []
    for _ in range(TAB_LIMIT):
        focus = window.activeFocusItem()
        if focus is not None and focus.objectName() == name:
            return path
        QTest.keyClick(window, Qt.Key_Tab)
        app.processEvents()
        focus = window.activeFocusItem()
        path.append(focus.objectName() or focus.metaObject().className() if focus else "-")
    pytest.fail(f"Tab не довёл фокус до {name} за {TAB_LIMIT} нажатий: {path}")


def settle_reveal(app: Any) -> None:
    """revealPageItem вызывается через Qt.callLater после смены фокуса."""
    for _ in range(3):
        app.processEvents()
        QTest.qWait(20)
    app.processEvents()


def body_flickable(item: QQuickItem) -> QQuickItem:
    """Тело раздела — самый внешний Flickable над элементом (Main.qml, id body)."""
    found: QQuickItem | None = None
    ancestor = item.parentItem()
    while ancestor is not None:
        if ancestor.inherits("QQuickFlickable"):
            found = ancestor
        ancestor = ancestor.parentItem()
    assert found is not None, f"{item.objectName()}: нет прокручиваемого тела над элементом"
    return found


def is_inside(item: QQuickItem, container: QQuickItem) -> bool:
    ancestor = item.parentItem()
    while ancestor is not None:
        if ancestor == container:
            return True
        ancestor = ancestor.parentItem()
    return False


def describe(item: QQuickItem) -> str:
    text = item.property("text") or item.property("label") or item.property("modelId") or ""
    return f"{item.objectName() or item.metaObject().className()}«{text}»"


@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
def test_filter_chip_mouse_click_shows_no_focus_ring(qt_app: Any, dark: bool) -> None:
    ring_color = theme_color("stateFocusRing", dark)
    results: list[tuple[str, bool, bool, bool, int]] = []

    def inspect(window: Any) -> None:
        chips = {name: named(window, name) for name in CHIPS}
        assert chips["allLanguagesChip"].property("active") is True
        assert chips["domesticChip"].property("active") is False
        # Сначала «Только отечественные» (как у владельца), затем обратно «Все языки».
        for name in ("domesticChip", "allLanguagesChip"):
            chip = chips[name]
            center = chip.mapToScene(QPointF(chip.width() / 2, chip.height() / 2)).toPoint()
            QTest.mouseClick(window, Qt.LeftButton, Qt.NoModifier, center)
            qt_app.processEvents()
            # Указатель уводим, чтобы наведение не меняло кадр.
            settle_pointer(qt_app, window)
            settle_reveal(qt_app)
            image = grab_frame(qt_app, window, f"mouse {name}, dark={dark}")
            rect = scene_rect(chip)
            pixels = ring_pixels(
                image,
                ring_color,
                rect.left() - NO_RING_BAND,
                rect.top() - NO_RING_BAND,
                rect.right() + NO_RING_BAND,
                rect.bottom() + NO_RING_BAND,
            )
            other = chips["allLanguagesChip" if name == "domesticChip" else "domesticChip"]
            results.append(
                (
                    name,
                    chip.property("active") is True,
                    other.property("active") is False,
                    ring_item(chip).isVisible(),
                    pixels,
                )
            )

    _, messages = render_settings(
        qt_app, dark, fake=FakeSettings(), section="models", inspect=inspect
    )
    assert [name for name, *_ in results] == ["domesticChip", "allLanguagesChip"], results
    for name, active, other_inactive, ring_visible, pixels in results:
        assert active and other_inactive, f"{name}: щелчок не переключил фильтр: {results}"
        assert not ring_visible, f"{name}: после щелчка мышью видно кольцо фокуса: {results}"
        assert pixels == 0, (
            f"{name}: после щелчка мышью вокруг чипа {pixels} px цвета кольца "
            f"{ring_color.name()}: {results}"
        )
    # Сообщения Qt — последними: сначала видна поведенческая причина падения.
    assert_no_messages(messages, f"mouse focus ring, dark={dark}")


@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
def test_filter_chip_keyboard_ring_is_complete(qt_app: Any, dark: bool) -> None:
    ring_color = theme_color("stateFocusRing", dark)
    sides: dict[str, dict[str, int]] = {}
    states: dict[str, tuple[str, bool, bool]] = {}
    path: list[str] = []
    active_window: list[bool] = []

    def inspect(window: Any) -> None:
        chips = {name: named(window, name) for name in CHIPS}
        active_window.append(activate_for_keyclick(window))
        path.extend(tab_until(qt_app, window, "allLanguagesChip"))

        def press(key: Any, modifiers: Any = Qt.NoModifier) -> str:
            QTest.keyClick(window, key, modifiers)
            qt_app.processEvents()
            focus = window.activeFocusItem()
            return focus.objectName() if focus is not None else "-"

        for name in CHIPS:
            if name == "domesticChip":
                # Следующий элемент цепочки после «Все языки» — «Только отечественные».
                press(Qt.Key_Tab)
            focus = window.activeFocusItem()
            assert focus == chips[name], (
                f"фокус на {focus.objectName() if focus else None}, ожидался {name}; "
                f"путь Tab {path}, window active={active_window}"
            )
            settle_reveal(qt_app)
            assert ring_item(chips[name]).isVisible(), f"{name}: кольцо с клавиатуры не видно"
            image = grab_frame(qt_app, window, f"tab {name}, dark={dark}")
            sides[name] = ring_sides(image, ring_color, scene_rect(chips[name]))

        def record(case: str, focus: str) -> None:
            states[case] = (
                focus,
                chips["domesticChip"].property("active") is True,
                chips["allLanguagesChip"].property("active") is True,
            )

        # Space, Return и Enter (цифровой блок) активируют чип в фокусе (FilterChip Keys).
        record("space", press(Qt.Key_Space))
        press(Qt.Key_Backtab, Qt.ShiftModifier)
        record("return", press(Qt.Key_Return))
        press(Qt.Key_Tab)
        record("enter", press(Qt.Key_Enter))

    _, messages = render_settings(
        qt_app, dark, fake=FakeSettings(), section="models", inspect=inspect
    )
    assert set(sides) == set(CHIPS), sides
    for name, counts in sides.items():
        missing = [side for side, count in counts.items() if count == 0]
        assert not missing, (
            f"{name}: у кольца фокуса нет сторон {missing} (срезано клипом?): {sides}"
        )
    # (фокус после клавиши, «Только отечественные» активен, «Все языки» активен)
    assert states == {
        "space": ("domesticChip", True, False),
        "return": ("allLanguagesChip", False, True),
        "enter": ("domesticChip", True, False),
    }, f"клавиши активации чипов: {states}"
    # Сообщения Qt — последними: сначала видна поведенческая причина падения.
    assert_no_messages(messages, f"keyboard focus ring, dark={dark}")


def overflowing_settings() -> FakeSettings:
    """Двенадцать моделей: тело «Моделей» не помещается даже в окно 1024×620."""
    fake = FakeSettings()
    fake.models = [
        *fake.models,
        *[
            {**copy.deepcopy(fake.models[1]), "id": f"extra-{i}", "name": f"T-one {i}"}
            for i in range(10)
        ],
    ]
    return fake


def set_window_size(monkeypatch: Any, width: int, height: int) -> None:
    """render_settings и grab_frame берут размер окна из констант test_onboarding."""
    monkeypatch.setattr(onboarding, "WIDTH", width)
    monkeypatch.setattr(onboarding, "HEIGHT", height)


#: Обход Tab по разделу: сайдбар, шапка, чипы и все карточки с кнопками.
WALK_LIMIT = 200
MIN_W, MIN_H = int(theme_number("sizeWindowMinW")), int(theme_number("sizeWindowMinH"))


@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
@pytest.mark.parametrize(
    ("width", "height", "overflow"),
    [(1024, 620, False), (MIN_W, MIN_H, True)],
    ids=["fits-1024x620", f"overflow-{MIN_W}x{MIN_H}"],
)
def test_focus_rings_fit_inside_page_viewport(
    qt_app: Any, monkeypatch: Any, width: int, height: int, overflow: bool, dark: bool
) -> None:
    set_window_size(monkeypatch, width, height)
    checked: list[str] = []
    clipped: list[str] = []
    scrolled: list[str] = []
    active_window: list[bool] = []

    def inspect(window: Any) -> None:
        chip = named(window, "allLanguagesChip")
        body = body_flickable(chip)
        active_window.append(activate_for_keyclick(window))
        tab_until(qt_app, window, "allLanguagesChip")
        # Держим сами обёртки, а не id(): id() освобождённой обёртки PyQt может повториться.
        seen: list[QQuickItem] = []
        for _ in range(WALK_LIMIT):
            settle_reveal(qt_app)
            focus = window.activeFocusItem()
            assert focus is not None, f"фокус потерян после {checked}"
            if any(focus == item for item in seen):
                break
            seen.append(focus)
            if is_inside(focus, body):
                top_left = focus.mapToItem(body, QPointF(-RING_OUTSET, -RING_OUTSET))
                bottom_right = focus.mapToItem(
                    body, QPointF(focus.width() + RING_OUTSET, focus.height() + RING_OUTSET)
                )
                ring = (top_left.x(), top_left.y(), bottom_right.x(), bottom_right.y())
                label = describe(focus)
                checked.append(label)
                content_y = float(body.property("contentY"))
                if content_y > 0.5:
                    scrolled.append(label)
                if not (
                    ring[0] >= -0.5
                    and ring[1] >= -0.5
                    and ring[2] <= body.width() + 0.5
                    and ring[3] <= body.height() + 0.5
                ):
                    clipped.append(
                        f"{label}: кольцо {tuple(round(v, 1) for v in ring)}, "
                        f"тело 0..{body.width():g} × 0..{body.height():g}, "
                        f"contentY={content_y:g}"
                    )
            QTest.keyClick(window, Qt.Key_Tab)
            qt_app.processEvents()
        else:
            pytest.fail(f"обход Tab не замкнулся за {WALK_LIMIT} нажатий: {checked}")

    fake = overflowing_settings() if overflow else FakeSettings()
    _, messages = render_settings(qt_app, dark, fake=fake, section="models", inspect=inspect)
    case = f"{width}x{height}, overflow={overflow}"
    # Обход обязан пройти чипы и карточки (боковые стороны кольца карточки — у краёв тела).
    assert len(checked) >= 3 and checked[0].startswith("allLanguagesChip"), (
        f"{case}: обход Tab по разделу «Модели»: {checked}; window active={active_window}"
    )
    assert any(label.startswith("domesticChip") for label in checked), checked
    cards = [label for label in checked if label.startswith("CatalogCard")]
    assert cards, f"{case}: Tab не дошёл ни до одной карточки: {checked}"
    if overflow:
        # revealPageItem прокрутил тело к карточкам ниже первого экрана.
        assert len(cards) >= 10, f"{case}: карточки в обходе: {cards}"
        assert any(label.startswith("CatalogCard") for label in scrolled), (
            f"{case}: тело не прокручивалось к фокусу (revealPageItem): {checked}"
        )
    assert not clipped, f"{case}: кольцо фокуса выходит за клип тела:\n" + "\n".join(clipped)
    # Сообщения Qt — последними: сначала видна поведенческая причина падения.
    assert_no_messages(messages, f"focus rings inside viewport, {case}, dark={dark}")


def body_scrollbar(body: QQuickItem) -> QQuickItem:
    bars = [item for item in visual_tree(body) if item.inherits("QQuickScrollBar")]
    assert len(bars) == 1, f"ожидалась одна полоса прокрутки тела, найдено {len(bars)}"
    return bars[0]


def handle_rect(bar: QQuickItem) -> QRectF:
    handle = bar.property("contentItem")
    assert handle is not None, "у полосы прокрутки нет ползунка"
    return scene_rect(handle)


#: Геометрия ползунка на main 2b5812a (до UI-20261009-01): ширина
#: scrollbarW − leftPadding (2, стиль Default) − scrollbarRight = 8 − 2 − 4 = 2;
#: x — правый край колонки минус scrollbarRight минус ширина; дорожка — тело ± 2.
#: Расширение клипа под кольцо фокуса не должно сдвигать полосу ни на пиксель.
SCROLLBAR_CASES = {
    "1024x620": (1024, 620, QRectF(996, 68, 2, 566 - 68)),
    f"{MIN_W}x{MIN_H}": (MIN_W, MIN_H, QRectF(872, 68, 2, 534 - 68)),
}


@pytest.mark.parametrize("case", SCROLLBAR_CASES)
def test_body_scrollbar_handle_keeps_main_geometry(
    qt_app: Any, monkeypatch: Any, case: str
) -> None:
    width, height, track = SCROLLBAR_CASES[case]
    set_window_size(monkeypatch, width, height)
    found: dict[str, Any] = {}

    def inspect(window: Any) -> None:
        body = body_flickable(named(window, "allLanguagesChip"))
        bar = body_scrollbar(body)
        found["visible"] = bar.isVisible()
        found["size"] = float(bar.property("size"))
        found["padding"] = tuple(
            bar.property(name)
            for name in ("leftPadding", "rightPadding", "topPadding", "bottomPadding")
        )
        # Полоса гаснет через opacity; active — это то, что видит пользователь при прокрутке.
        bar.setProperty("active", False)
        qt_app.processEvents()
        hidden = grab_frame(qt_app, window, f"scrollbar hidden {case}")
        bar.setProperty("active", True)
        qt_app.processEvents()
        found["active"] = bar.property("active")
        found["top"] = handle_rect(bar)
        shown = grab_frame(qt_app, window, f"scrollbar shown {case}")
        rect = found["top"]
        dpr = shown.devicePixelRatio()
        found["drawn"] = sum(
            shown.pixel(x, y) != hidden.pixel(x, y)
            for y in range(int(rect.top() * dpr), int(rect.bottom() * dpr))
            for x in range(int((rect.left() - 2) * dpr), int((rect.right() + 2) * dpr))
        )
        maximum = float(body.property("contentHeight")) - body.height()
        body.setProperty("contentY", maximum)
        qt_app.processEvents()
        bar.setProperty("active", True)
        qt_app.processEvents()
        found["bottom"] = handle_rect(bar)

    _, messages = render_settings(
        qt_app, False, fake=overflowing_settings(), section="models", inspect=inspect
    )
    assert found["visible"] and found["size"] < 1, f"{case}: тело не переполнено: {found}"
    assert found["active"] is True, found
    top, bottom = found["top"], found["bottom"]
    assert top.width() == pytest.approx(2), f"{case}: ширина ползунка {top.width()}: {found}"
    assert (top.left(), top.top()) == pytest.approx((track.left(), track.top())), (
        f"{case}: ползунок в начале прокрутки {top}, ожидался x={track.left()} y={track.top()}: "
        f"{found}"
    )
    assert top.height() == pytest.approx(found["size"] * track.height(), abs=0.5), (
        f"{case}: длина ползунка {top.height()} при size={found['size']} и дорожке "
        f"{track.height()}: {found}"
    )
    assert (bottom.left(), bottom.bottom()) == pytest.approx((track.left(), track.bottom())), (
        f"{case}: ползунок в конце прокрутки {bottom}, ожидался низ дорожки {track.bottom()}: "
        f"{found}"
    )
    assert found["drawn"] > 0, f"{case}: активный ползунок не нарисован ни одним пикселем: {found}"
    # Сообщения Qt — последними: сначала видна поведенческая причина падения.
    assert_no_messages(messages, f"body scrollbar {case}")
