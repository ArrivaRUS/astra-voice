"""Настоящая QML-пилюля: 12 состояний без сообщений Qt и 10 PNG для DesignReviewer.

xvfb в системе не установлен. Запуск без X-сервера:
QT_QPA_PLATFORM=offscreen QT_QUICK_BACKEND=software pytest -m xvfb.
Снимки в design/refs/impl/pill/ включают поля тени окна; сама пилюля имеет высоту 36.
Референс design/refs/09-pill.png проверяется на наличие, без попиксельного сравнения.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass
from math import ceil, floor
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.xvfb
REPO = Path(__file__).resolve().parents[2]
SNAPSHOTS = REPO / "design/refs/impl/pill"
LABELS = {
    "hidden": "",
    "loading-model": "Загружаю модель…",
    "listening": "Слушаю",
    "listening-silent": "Микрофон молчит",
    "limit": "Достигнут лимит записи",
    "processing": "Распознаю…",
    "done": "Готово",
    "clipboard-only": "Скопировано в буфер",
    "empty": "Ничего не распознано",
    "cancelled": "Отменено",
    "error": "Не удалось распознать",
    "disabled": "",
}
INVISIBLE = {"hidden", "disabled"}
# Девять кадров, включая нижнюю границу, максимум и округление половины вверх в JS.
LEVELS = [0.05, 0.125, 0.175, 0.25, 0.375, 0.5, 0.675, 0.825, 1.0]

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QUICK_BACKEND", "software")


@dataclass
class TextMeasurement:
    text: str
    elide: int
    content_width: float
    width: float
    left: float
    right: float
    visible: bool
    color: str


@dataclass
class RenderedState:
    width: float
    height: float
    visible: bool
    item_visible: bool
    close_visible: bool
    texts: list[TextMeasurement]
    bars: list[tuple[int, float, float, str]]
    messages: list[str]
    snapshot: Path | None


@pytest.fixture(scope="session")
def pill_app() -> Any:
    pytest.importorskip("PyQt5.QtQuick", reason="нужен python3-pyqt5.qtquick")
    from PyQt5.QtWidgets import QApplication

    # QApplication совместим и с последующими тестами настоящего QMenu.
    app = QApplication.instance() or QApplication([])
    assert isinstance(app, QApplication)
    return app


def visual_tree(root: Any) -> Iterator[Any]:
    """childItems включает делегаты Repeater, отсутствующие в QObject.children()."""
    yield root
    for child in root.childItems():
        yield from visual_tree(child)


def capture_state(app: Any, state: str) -> RenderedState:
    from PyQt5 import sip
    from PyQt5.QtCore import QPointF, QUrl, qInstallMessageHandler
    from PyQt5.QtQuick import QQuickView
    from PyQt5.QtTest import QTest

    from astra_voice.ui.pill import Pill, PillState

    messages: list[str] = []

    def handler(_mode: Any, _context: Any, text: str) -> None:
        messages.append(text)

    previous = qInstallMessageHandler(handler)
    view = QQuickView()
    pill = None
    try:
        pill = Pill(
            qml_url=QUrl.fromLocalFile(str(REPO / "qml/Pill.qml")),
            view_factory=lambda: view,
        )
        assert view.status() == QQuickView.Ready, [e.toString() for e in view.errors()]
        root = view.rootObject()
        assert root is not None
        if state in INVISIBLE:
            # Проверяем настоящее скрытие уже показанного окна через Python-мост.
            pill.show_state(PillState.LISTENING)
            app.processEvents()
            assert view.isVisible()
        if state == "listening":
            for level in LEVELS:
                pill.show_state(PillState.LISTENING, level=level)
        else:
            pill.show_state(PillState(state), text=LABELS[state] if state == "error" else None)
        # Завершаем переход opacity (160/120 мс), Row polish и загрузку глифов.
        QTest.qWait(180)
        app.processEvents()
        assert root.property("avState") == state

        snapshot = None
        if state not in INVISIBLE:
            assert view.isVisible() and view.isExposed()
            image = view.grabWindow()
            if image.isNull():
                # Некоторые сборки Qt5/offscreen не реализуют grabWindow даже после show.
                grab = view.contentItem().grabToImage()
                assert grab is not None, f"{state}: grabToImage не запустился"
                for _ in range(20):
                    QTest.qWait(10)
                    image = grab.image()
                    if not image.isNull():
                        break
            assert not image.isNull(), f"{state}: пустой снимок"
            dpr = image.devicePixelRatio()
            assert image.width() / dpr == ceil(root.width()) + 36
            assert image.height() / dpr == 78  # 36 + два поля по 18 + смещение тени 6.
            assert any(
                image.pixelColor(x, image.height() // 2).alpha() > 0 for x in range(image.width())
            ), f"{state}: полностью прозрачный снимок"
            SNAPSHOTS.mkdir(parents=True, exist_ok=True)
            snapshot = SNAPSHOTS / f"{state}.png"
            assert image.save(str(snapshot), "PNG"), f"не удалось сохранить {snapshot}"

        texts = []
        bars = []
        close_buttons = []
        for item in visual_tree(root):
            meta = item.metaObject()
            # У кнопки «×» есть круглая подложка; глиф состояния cancelled — без неё.
            if item.property("name") == "x" and item.parentItem().property("radius") is not None:
                close_buttons.append(item.parentItem())
            if meta.indexOfProperty("elide") >= 0:
                left = item.mapToItem(root, QPointF(0, 0)).x()
                texts.append(
                    TextMeasurement(
                        text=str(item.property("text")),
                        elide=int(item.property("elide")),
                        content_width=float(item.property("contentWidth")),
                        width=item.width(),
                        left=left,
                        right=left + item.width(),
                        visible=item.isVisible(),
                        color=item.property("color").name().upper(),
                    )
                )
            if meta.indexOfProperty("level") >= 0:
                bars.append(
                    (
                        int(item.property("index")),
                        item.property("level"),
                        item.height(),
                        item.property("color").name().upper(),
                    )
                )
        assert len(close_buttons) == 1, f"{state}: должна существовать одна кнопка «×»"
        return RenderedState(
            width=root.width(),
            height=root.height(),
            visible=view.isVisible(),
            item_visible=root.isVisible(),
            close_visible=close_buttons[0].isVisible(),
            texts=texts,
            bars=sorted(bars),
            messages=messages,
            snapshot=snapshot,
        )
    finally:
        # Деструкторы также входят в перехват; таймеры не переживают своё окно.
        try:
            if pill is not None:
                pill.hide()
                sip.delete(pill)
            sip.delete(view)
            app.processEvents()
        finally:
            qInstallMessageHandler(previous)


@pytest.fixture(scope="module")
def rendered_states(pill_app: Any) -> dict[str, RenderedState]:
    # Общая фикстура гарантирует снимки и при отдельном запуске теста референса.
    return {state: capture_state(pill_app, state) for state in LABELS}


@pytest.mark.parametrize("state", LABELS)
def test_pill_state(state: str, rendered_states: dict[str, RenderedState]) -> None:
    rendered = rendered_states[state]
    assert not rendered.messages, f"{state}: сообщения Qt\n" + "\n".join(rendered.messages)
    assert 172 <= rendered.width <= 320
    assert rendered.height == 36
    assert rendered.visible == (state not in INVISIBLE)
    assert rendered.item_visible == rendered.visible
    assert rendered.close_visible == (state in {"listening", "listening-silent"}), (
        f"{state}: неверная видимость кнопки «×»"
    )
    assert rendered.texts, "проверка Text не должна быть пустой"
    for text in rendered.texts:
        # QQuickText::ElideNone == 3 (QQuickText не экспортирован классом в PyQt5).
        assert text.elide == 3, f"{state}: elide у {text.text!r}"
        assert text.content_width <= text.width + 0.01, f"{state}: обрезан {text.text!r}"
        if text.visible:
            assert 10 <= text.left and text.right <= rendered.width - 10, (
                f"{state}: {text.text!r} выходит за паддинги пилюли: "
                f"{text.left}…{text.right}, ширина {rendered.width}"
            )

    # «—» и «›» — символы иконок со своими цветами по §8.4; здесь проверяется подпись.
    captions = [text for text in rendered.texts if text.text not in {"—", "›"}]
    assert len(captions) == 1
    caption = captions[0]
    if state not in INVISIBLE:
        assert caption.text == LABELS[state]
    assert caption.color == ("#F0645F" if state == "error" else "#F2F5FA")

    if state in INVISIBLE:
        assert rendered.snapshot is None
        assert not (SNAPSHOTS / f"{state}.png").exists()
    else:
        assert rendered.snapshot == SNAPSHOTS / f"{state}.png"
        assert rendered.snapshot.is_file()

    if state in {"listening", "listening-silent", "limit"}:
        assert len(rendered.bars) == 9
        expected_color = "#12B3A0" if state == "listening" else "#8C97AC"
        assert all(color == expected_color for _, _, _, color in rendered.bars), (
            f"{state}: неверный цвет столбиков, ожидался {expected_color}"
        )

    if state == "listening":
        assert [index for index, _, _, _ in rendered.bars] == list(range(9))
        assert [level for _, level, _, _ in rendered.bars] == LEVELS
        # Math.round в QML округляет .5 вверх; Python round использует ties-to-even.
        assert [height for _, _, height, _ in rendered.bars] == [
            max(3, floor(level * 20 + 0.5)) for level in LEVELS
        ]
    elif state == "listening-silent":
        assert len(rendered.bars) == 9
        assert all(height == 4 for _, _, height, _ in rendered.bars)
    elif state == "limit":
        assert all(
            height == max(3, floor(level * 20 + 0.5)) for _, level, height, _ in rendered.bars
        )


def test_design_reference_and_snapshots_exist(rendered_states: dict[str, RenderedState]) -> None:
    from PyQt5.QtGui import QImage

    assert (REPO / "design/refs/09-pill.png").is_file()
    expected = {f"{state}.png" for state in LABELS if state not in INVISIBLE}
    assert {path.name for path in SNAPSHOTS.glob("*.png")} == expected
    for state in LABELS.keys() - INVISIBLE:
        snapshot = rendered_states[state].snapshot
        assert snapshot is not None and snapshot.is_file()
        assert not QImage(str(snapshot)).isNull(), f"повреждён PNG: {snapshot}"
