"""Настоящая QML-пилюля: 12 состояний без сообщений Qt и 10 PNG для DesignReviewer.

xvfb в системе не установлен. Запуск без X-сервера:
QT_QPA_PLATFORM=offscreen QT_QUICK_BACKEND=software pytest -m xvfb.
Снимки всегда включают поля тени 18/18/18/24 px и принимаются только при альфе
фона 240 ± 1 в центре пилюли. Каталог снимков задаётся ASTRA_VOICE_SNAPSHOT_DIR
или по умолчанию design/refs/impl/pill. PNG публикуются из временного каталога
атомарной заменой только при изменении пикселей (или отсутствии читаемого PNG)
и после успешных проверок всего модуля; метаданные PNG не учитываются.
Референс design/refs/09-pill.png проверяется на наличие, без попиксельного сравнения.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from hashlib import sha256
from math import ceil, floor
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from helpers.qt_app import get_qapplication  # noqa: E402

pytestmark = pytest.mark.xvfb
REPO = Path(__file__).resolve().parents[2]
SNAPSHOTS = REPO / Path(os.environ.get("ASTRA_VOICE_SNAPSHOT_DIR") or "design/refs/impl/pill")
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
class DetailsButtonMeasurement:
    visible: bool
    background_visible: bool
    radius: float
    background_color: str
    background_alpha: float
    font_pixel_size: int
    color: str


@dataclass
class RenderedState:
    width: float
    height: float
    visible: bool
    item_visible: bool
    close_visible: bool
    details_button: DetailsButtonMeasurement
    texts: list[TextMeasurement]
    bars: list[tuple[int, float, float, str]]
    messages: list[str]
    snapshot: Path | None
    shadow_present: bool | None


@pytest.fixture(scope="session")
def pill_app() -> Any:
    return get_qapplication()


def visual_tree(root: Any) -> Iterator[Any]:
    """childItems включает делегаты Repeater, отсутствующие в QObject.children()."""
    yield root
    for child in root.childItems():
        yield from visual_tree(child)


def pill_background_alpha(image: Any, pill_width: float, pill_height: float) -> int:
    """Альфа фона в центре, без вклада наложенных букв и иконок.

    Берём минимум на центральной вертикали в средней половине высоты пилюли:
    текст повышает альфу, а свободный фон остаётся виден над/под глифами.
    Скругления, края и внешняя тень в эту область не попадают.
    """
    dpr = image.devicePixelRatio()
    center_x = floor((18 + pill_width / 2) * dpr)
    top = floor((18 + pill_height / 4) * dpr)
    bottom = ceil((18 + pill_height * 3 / 4) * dpr)
    return min(int(image.pixelColor(center_x, y).alpha()) for y in range(top, bottom))


def assert_saved_snapshot(state: str, rendered: RenderedState) -> None:
    from PyQt5.QtGui import QImage

    snapshot = rendered.snapshot
    assert snapshot is not None and snapshot.is_file(), f"{state}: отсутствует PNG"
    image = QImage(str(snapshot))
    assert not image.isNull(), f"повреждён PNG: {snapshot}"
    expected_size = (ceil(rendered.width) + 36, ceil(rendered.height) + 42)
    actual_size = (image.width(), image.height())
    assert actual_size == expected_size, (
        f"{state}: размер PNG {actual_size}, ожидался {expected_size}"
    )
    alpha = pill_background_alpha(image, rendered.width, rendered.height)
    assert abs(alpha - 240) <= 1, (
        f"{state}: альфа фона PNG в центре {alpha}, ожидалась 240 ± 1; размер {actual_size}"
    )


def capture_state(
    app: Any,
    state: str,
    *,
    text: str | None = None,
    snapshot_dir: Path,
    extra_wait_ms: int = 0,
) -> RenderedState:
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
        assert root.setProperty("freezeAnimations", True), (
            f"{state}: в QML нет свойства freezeAnimations — заморозка анимаций не сработала"
        )
        if state in INVISIBLE:
            # Проверяем настоящее скрытие уже показанного окна через Python-мост.
            pill.show_state(PillState.LISTENING)
            app.processEvents()
            assert view.isVisible()
        if state == "listening":
            for level in LEVELS:
                pill.show_state(PillState.LISTENING, level=level)
        else:
            if text is None and state == "error":
                text = LABELS[state]
            pill.show_state(PillState(state), text=text)
        # Даём время на Row polish и глифы; завершение появления проверяем по PNG ниже.
        QTest.qWait(180 + extra_wait_ms)
        app.processEvents()
        assert root.property("avState") == state
        pill_width = float(root.property("pillWidth"))
        pill_height = float(root.property("pillHeight"))
        expected_size = (ceil(pill_width) + 36, ceil(pill_height) + 42)
        window_size = (view.width(), view.height())
        assert window_size == expected_size, (
            f"{state}: размер окна {window_size}, ожидался {expected_size}"
        )

        snapshot = None
        shadow_present = None
        if state not in INVISIBLE:
            assert view.isVisible() and view.isExposed()
            origin = root.mapToItem(view.contentItem(), QPointF(0, 0))
            assert (origin.x(), origin.y()) == (18, 18), f"{state}: неверные поля пилюли"
            grab = None
            for attempt in range(1, 21):
                if attempt > 1:
                    QTest.qWait(25)
                image = view.grabWindow()
                if image.isNull():
                    # Некоторые сборки Qt5/offscreen не реализуют grabWindow даже после show.
                    if grab is None:
                        grab = view.contentItem().grabToImage()
                    if grab is not None:
                        image = grab.image()
                        if not image.isNull():
                            # Готовый, но неподходящий кадр нужно снять заново.
                            grab = None
                dpr = image.devicePixelRatio()
                actual_size = (image.width(), image.height())
                alpha = None
                if image.isNull():
                    failure = "пустой снимок"
                    if grab is None:
                        failure += "; grabToImage не запустился"
                    continue
                if actual_size != expected_size:
                    failure = "недопустимый размер снимка"
                    continue
                alpha = pill_background_alpha(image, pill_width, pill_height)
                if abs(alpha - 240) <= 1:
                    break
                failure = "фон пилюли не достиг альфы 240 ± 1"
            else:
                pytest.fail(
                    f"{state}: {failure}; размер снимка {actual_size} (DPR={dpr}); "
                    f"альфа фона в центре: {alpha}; ожидался размер {expected_size}; "
                    f"QML pillWidth={pill_width}, pillHeight={pill_height}; попыток: {attempt}"
                )
            center_x = floor((origin.x() + pill_width / 2) * dpr)
            bottom = ceil((origin.y() + pill_height) * dpr)
            # Под центром пилюли должна быть полупрозрачная чёрная внешняя тень.
            shadow_present = any(
                0 < (pixel := image.pixelColor(center_x, y)).alpha() < 255
                and pixel.red() == pixel.green() == pixel.blue() == 0
                for y in range(bottom, image.height())
            )
            snapshot_dir.mkdir(parents=True, exist_ok=True)
            snapshot = snapshot_dir / f"{state}.png"
            assert image.save(str(snapshot), "PNG"), f"не удалось сохранить {snapshot}"

        texts = []
        bars = []
        close_buttons = []
        details_buttons = []
        for item in visual_tree(root):
            meta = item.metaObject()
            # У кнопки «×» есть круглая подложка; глиф состояния cancelled — без неё.
            if item.property("name") == "x" and item.parentItem().property("radius") is not None:
                close_buttons.append(item.parentItem())
            if meta.indexOfProperty("elide") >= 0:
                if item.property("text") == "›":
                    background = item.parentItem()
                    assert background is not None and background.property("radius") is not None, (
                        f"{state}: у кнопки «›» отсутствует круглая подложка"
                    )
                    background_color = background.property("color")
                    details_buttons.append(
                        DetailsButtonMeasurement(
                            visible=item.isVisible(),
                            background_visible=background.isVisible(),
                            radius=float(background.property("radius")),
                            background_color=background_color.name().upper(),
                            background_alpha=background_color.alphaF(),
                            font_pixel_size=int(item.property("font").pixelSize()),
                            color=item.property("color").name().upper(),
                        )
                    )
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
        assert len(details_buttons) == 1, (
            f"{state}: должна существовать одна кнопка «›», найдено {len(details_buttons)}"
        )
        return RenderedState(
            width=pill_width,
            height=pill_height,
            visible=view.isVisible(),
            item_visible=root.isVisible(),
            close_visible=close_buttons[0].isVisible(),
            details_button=details_buttons[0],
            texts=texts,
            bars=sorted(bars),
            messages=messages,
            snapshot=snapshot,
            shadow_present=shadow_present,
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
def rendered_states(
    pill_app: Any, request: pytest.FixtureRequest
) -> Iterator[dict[str, RenderedState]]:
    from PyQt5.QtGui import QImage

    # Общая фикстура гарантирует снимки и при отдельном запуске теста референса.
    # Соседний staging сохраняет атомарность замены и не попадает в glob снимков.
    SNAPSHOTS.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".pill-", dir=SNAPSHOTS.parent))
    failures_before = request.session.testsfailed
    try:
        rendered = {state: capture_state(pill_app, state, snapshot_dir=staging) for state in LABELS}
        expected = {f"{state}.png" for state in LABELS if state not in INVISIBLE}
        assert len(expected) == 10
        assert {path.name for path in staging.glob("*.png")} == expected
        assert {path.name for path in SNAPSHOTS.glob("*.png")} <= expected
        for state in LABELS.keys() - INVISIBLE:
            assert_saved_snapshot(state, rendered[state])
        yield rendered
        # При ошибке съёмки или любой проверки модуля прежние PNG остаются нетронутыми.
        if request.session.testsfailed == failures_before:
            SNAPSHOTS.mkdir(parents=True, exist_ok=True)
            for name in sorted(expected):
                source = staging / name
                destination = SNAPSHOTS / name
                current = QImage(str(destination)).convertToFormat(QImage.Format_RGBA8888)
                candidate = QImage(str(source)).convertToFormat(QImage.Format_RGBA8888)
                if current.isNull() or current != candidate:
                    os.replace(source, destination)
    finally:
        shutil.rmtree(staging)


def assert_pill_geometry_and_text(state: str, rendered: RenderedState) -> None:
    assert 172 <= rendered.width <= 320
    assert rendered.height == 36
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


@pytest.mark.parametrize("state", LABELS)
def test_pill_state(state: str, rendered_states: dict[str, RenderedState]) -> None:
    rendered = rendered_states[state]
    assert not rendered.messages, f"{state}: сообщения Qt\n" + "\n".join(rendered.messages)
    assert_pill_geometry_and_text(state, rendered)
    assert rendered.visible == (state not in INVISIBLE)
    assert rendered.item_visible == rendered.visible
    assert rendered.close_visible == (state in {"listening", "listening-silent"}), (
        f"{state}: неверная видимость кнопки «×»"
    )
    details = rendered.details_button
    assert details.visible == (state == "error"), f"{state}: неверная видимость знака «›»"
    assert details.background_visible == (state == "error"), (
        f"{state}: неверная видимость подложки кнопки «›»"
    )
    if state == "error":
        assert details.radius == 11, (
            f"{state}: радиус подложки кнопки «›» {details.radius}, ожидался 11"
        )
        assert details.background_color == "#FFFFFF", (
            f"{state}: цвет подложки кнопки «›» {details.background_color}, ожидался #FFFFFF"
        )
        assert abs(details.background_alpha - 0.12) <= 0.005, (
            f"{state}: альфа подложки кнопки «›» {details.background_alpha}, ожидалась 0.12 ± 0.005"
        )
        assert details.font_pixel_size == 13, (
            f"{state}: размер шрифта знака «›» {details.font_pixel_size} px, ожидался 13 px"
        )
        assert details.color == "#C4CDDC", (
            f"{state}: цвет знака «›» {details.color}, ожидался #C4CDDC"
        )
    # Цвета, столбики, «×» и «›» измеряются в QML, независимо от полей снимка.
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
        assert rendered.snapshot is not None
        assert rendered.snapshot.name == f"{state}.png"
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
    elif state in {"listening-silent", "limit"}:
        assert len(rendered.bars) == 9
        assert all(height == 4 for _, _, height, _ in rendered.bars)


@pytest.mark.parametrize("state", [state for state in LABELS if state not in INVISIBLE])
def test_pill_snapshots_are_deterministic(
    state: str,
    pill_app: Any,
    rendered_states: dict[str, RenderedState],
    tmp_path: Path,
) -> None:
    first = rendered_states[state]
    # Сдвигаем фазу на четверть периода, чтобы без заморозки сравнение падало
    # даже при одинаковой скорости двух рендеров. Повторные PNG не публикуются.
    second = capture_state(pill_app, state, snapshot_dir=tmp_path, extra_wait_ms=250)
    assert not second.messages, f"{state}: сообщения Qt\n" + "\n".join(second.messages)
    assert_saved_snapshot(state, second)
    assert first.snapshot is not None and second.snapshot is not None
    first_hash = sha256(first.snapshot.read_bytes()).hexdigest()
    second_hash = sha256(second.snapshot.read_bytes()).hexdigest()
    assert first_hash == second_hash, (
        f"{state}: PNG зависит от времени съёмки: {first_hash} != {second_hash}"
    )


@pytest.mark.parametrize("state", [state for state in LABELS if state not in INVISIBLE])
def test_pill_snapshot_shadow(state: str, rendered_states: dict[str, RenderedState]) -> None:
    rendered = rendered_states[state]
    assert rendered.shadow_present, f"{state}: поле есть в снимке, но внешняя тень отсутствует"


def test_clipboard_window_changed_label(pill_app: Any, tmp_path: Path) -> None:
    from astra_voice.ui.pill import CLIPBOARD_WINDOW_CHANGED

    state = "clipboard-only"
    rendered = capture_state(pill_app, state, text=CLIPBOARD_WINDOW_CHANGED, snapshot_dir=tmp_path)
    assert not rendered.messages, f"{state}: сообщения Qt\n" + "\n".join(rendered.messages)
    assert rendered.visible and rendered.item_visible
    assert_pill_geometry_and_text(state, rendered)
    captions = [text for text in rendered.texts if text.visible]
    assert len(captions) == 1
    assert captions[0].text == CLIPBOARD_WINDOW_CHANGED
    assert captions[0].color == "#F2F5FA"
    assert not rendered.close_visible
    assert rendered.snapshot is not None and rendered.snapshot.is_file()


def test_design_reference_and_snapshots_exist(rendered_states: dict[str, RenderedState]) -> None:
    assert (REPO / "design/refs/09-pill.png").is_file()
    expected = {f"{state}.png" for state in LABELS if state not in INVISIBLE}
    snapshots = [rendered.snapshot for rendered in rendered_states.values() if rendered.snapshot]
    assert len(snapshots) == len(expected) == 10
    assert {path.name for path in snapshots[0].parent.glob("*.png")} == expected
    for state in LABELS.keys() - INVISIBLE:
        assert_saved_snapshot(state, rendered_states[state])
