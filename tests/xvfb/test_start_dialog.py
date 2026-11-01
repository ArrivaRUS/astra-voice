"""M2 W4 contract, keyboard behaviour and private light/dark contact sheets."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from PyQt5.QtCore import (
    QObject,
    QPoint,
    QPointF,
    Qt,
    QUrl,
    pyqtProperty,
    pyqtSignal,
    pyqtSlot,
    qInstallMessageHandler,
)
from PyQt5.QtGui import QGuiApplication, QImage, QPainter
from PyQt5.QtQml import QQmlApplicationEngine
from PyQt5.QtTest import QTest
from test_meetings_section import Bridge, ThemeSource, grab
from test_meetings_section import meetings_app as meetings_app
from test_meetings_section import rendered as rendered

from astra_voice.ui.icons import install_icon_provider
from helpers.snapshots import write_snapshot

pytestmark = pytest.mark.xvfb
REPO = Path(__file__).resolve().parents[2]
SHOTS = Path("/tmp/e13-m2-s3b-shots")
FLY = (
    "В этой сессии программа не узнаёт о блокировке экрана: если заблокировать экран, "
    "запись продолжится. Приостановите или остановите запись сами, когда отходите от компьютера."
)
COPY = {
    "title": "Записать встречу",
    "heading": "Что записывать",
    "microphone": "Микрофон",
    "speakers": (
        "В гарнитуре запись будет чище: без неё голоса собеседников попадут и в ваш микрофон."
    ),
    "reminder": "После начала записи появится текст, чтобы предупредить участников.",
    "cancel": "Отмена",
    "start": "Начать запись",
    "starting": "Начинаю…",
    "openMeetings": "Открыть встречи",
    "retry": "Повторить",
    "interruptedTitle": "Запись встречи прервалась",
    "close": "Закрыть",
}
ERRORS = {
    "error-disk": (
        "Мало места на диске: для записи встречи нужно около 600 МБ. Освободите место "
        "или удалите старые встречи."
    ),
    "error-audio": (
        "Не удалось начать запись: звуковая служба не отвечает. Попробуйте ещё раз "
        "или обратитесь к администратору."
    ),
    "error-folder": (
        "Не удалось начать запись: папка встреч недоступна для записи. Обратитесь к администратору."
    ),
    "error-busy": "Уже идёт запись встречи",
}
WARNINGS = {
    "warn-nfs": (
        "Домашняя папка находится на сетевом диске — запись встречи будет храниться не только "
        "на этом компьютере. Записывайте, только если это разрешено в вашей организации."
    ),
    "warn-remote-audio": (
        "Звуковая служба работает не на этом компьютере — звук встречи пройдёт через другой "
        "компьютер. Записывайте, только если это разрешено в вашей организации."
    ),
    "fly": FLY,
}
STATES = ["default", "call", "browser", "no-app-split", "speakers", "starting", *ERRORS, *WARNINGS]


class RecordingBridge(Bridge):
    """Presentation fixture; its Qt API must remain a real bridge subset."""

    changed = pyqtSignal()

    def __init__(self, state: str = "default", *, dev: bool = True) -> None:
        super().__init__("empty")
        self.w4_state, self.dev, self.open = state, dev, True
        self.selected = (
            "call"
            if state in {"call", "browser", "no-app-split", "speakers", "starting"}
            else "room"
        )
        self.starts = self.retries = self.closed = self.opened_meetings = 0
        self.interrupted = False
        self.interrupted_remaining = 0
        self._extra_warnings: list[str] | None = None

    recordingDevEnabled = pyqtProperty(bool, lambda self: self.dev, notify=changed)
    startDialogOpen = pyqtProperty(bool, lambda self: self.open, notify=changed)
    startState = pyqtProperty(str, lambda self: self.w4_state, notify=changed)
    startTexts = pyqtProperty("QVariantMap", lambda self: COPY, constant=True)
    selectedSource = pyqtProperty(str, lambda self: self.selected, notify=changed)
    startError = pyqtProperty(str, lambda self: ERRORS.get(self.w4_state, ""), notify=changed)
    startWarnings = pyqtProperty(
        "QStringList",
        lambda self: (
            self._extra_warnings
            if self._extra_warnings is not None
            else [WARNINGS[self.w4_state]]
            if self.w4_state in WARNINGS
            else []
        ),
        notify=changed,
    )
    startCanStart = pyqtProperty(
        bool,
        lambda self: (
            self.w4_state not in {*ERRORS, "starting"}
            and not (self.w4_state == "no-app-split" and self.selected == "call")
        ),
        notify=changed,
    )
    startSpeakers = pyqtProperty(bool, lambda self: self.w4_state == "speakers", notify=changed)
    interruptedOpen = pyqtProperty(bool, lambda self: self.interrupted, notify=changed)
    interruptedText = pyqtProperty(
        str, lambda self: "Запись встречи прервалась. Сохранено 0:37:02", notify=changed
    )

    @pyqtProperty("QVariantList", notify=changed)
    def startOptions(self) -> list[dict[str, Any]]:
        options = [
            {
                "id": "room",
                "title": "Встреча в комнате — только микрофон",
                "caption": "Звук компьютера не записывается.",
                "enabled": True,
                "warning": "",
            },
            {
                "id": "all",
                "title": "Весь звук компьютера и микрофон",
                "caption": (
                    "Для программы видеосвязи, которую Astra Voice не узнаёт. В запись "
                    "попадут все звуки компьютера: уведомления, музыка, другие программы."
                ),
                "enabled": True,
                "warning": "",
            },
        ]
        if self.w4_state in {"call", "browser", "no-app-split", "speakers", "starting"}:
            browser = self.w4_state == "browser"
            options.insert(
                0,
                {
                    "id": "call",
                    "title": "Звонок в браузере" if browser else "Звонок в DION",
                    "caption": (
                        "Ваш микрофон и звук браузера — всех вкладок, не только вкладки звонка."
                        if browser
                        else (
                            "Ваш микрофон и звук DION. Другие звуки компьютера в запись не попадут."
                        )
                    ),
                    "enabled": self.w4_state != "no-app-split",
                    "warning": (
                        "На этом компьютере программа не умеет отделить звук видеосвязи — "
                        "в запись попадёт весь звук компьютера."
                    )
                    if self.w4_state == "no-app-split"
                    else "",
                },
            )
        return options

    @pyqtSlot()
    def requestRecording(self) -> None:
        self.open = self.dev
        self.changed.emit()

    @pyqtSlot()
    def closeStartDialog(self) -> None:
        self.open = False
        self.closed += 1
        self.changed.emit()

    @pyqtSlot(str)
    def selectRecordingSource(self, source: str) -> None:
        self.selected = source
        self.changed.emit()

    @pyqtSlot()
    def on_start_clicked(self) -> None:
        self.starts += 1
        self.w4_state = "starting"
        self.changed.emit()

    @pyqtSlot()
    def retryStart(self) -> None:
        self.retries += 1

    @pyqtSlot()
    def closeInterrupted(self) -> None:
        self.interrupted = self.interrupted_remaining > 0
        self.interrupted_remaining = max(0, self.interrupted_remaining - 1)
        self.changed.emit()

    @pyqtSlot()
    def openInterruptedMeetings(self) -> None:
        self.opened_meetings += 1
        self.interrupted = self.open = False
        self.changed.emit()


def texts(item: Any) -> list[str]:
    found: list[str] = []
    value = item.property("text")
    if isinstance(value, str):
        found.append(value)
    for child in item.childItems():
        found.extend(texts(child))
    return found


def visual_items(item: Any) -> Iterator[Any]:
    yield item
    for child in item.childItems():
        yield from visual_items(child)


@pytest.fixture
def start_window(meetings_app: Any) -> Iterator[Any]:
    live: list[tuple[Any, Any, Any, Any]] = []
    messages: list[str] = []
    old = qInstallMessageHandler(lambda _m, _c, message: messages.append(message))

    def create(state: str, dark: bool = False) -> tuple[Any, RecordingBridge, list[str]]:
        messages.clear()
        for _engine, previous, _source, _bridge in live:
            previous.hide()
        engine = QQmlApplicationEngine()
        source, bridge = ThemeSource(dark), RecordingBridge(state)
        engine.rootContext().setContextProperty("themeSource", source)
        engine.rootContext().setContextProperty("meetingBridge", bridge)
        install_icon_provider(engine)
        engine.load(QUrl.fromLocalFile(str(REPO / "qml/StartDialog.qml")))
        assert engine.rootObjects(), messages
        window = engine.rootObjects()[0]
        window.show()
        window.requestActivate()
        QTest.qWait(35)
        meetings_app.processEvents()
        live.append((engine, window, source, bridge))
        assert not messages, messages
        return window, bridge, messages

    yield create
    from PyQt5 import sip

    for engine, window, _source, _bridge in live:
        window.hide()
        sip.delete(engine)
    meetings_app.processEvents()
    qInstallMessageHandler(old)


def test_start_dialog_exists_before_rendering() -> None:
    assert (REPO / "qml/StartDialog.qml").is_file(), "W4 is not implemented"


@pytest.mark.parametrize("state", STATES)
def test_w4_state_composition_and_literal_copy(start_window: Any, state: str) -> None:
    window, bridge, messages = start_window(state)
    assert window.width() == 460 and window.height() <= 600
    content = texts(window.contentItem())
    assert COPY["heading"] in content and COPY["reminder"] in content
    assert COPY["microphone"] in content
    for option in bridge.startOptions:
        assert option["title"] in content and option["caption"] in content
        if option["warning"]:
            assert option["warning"] in content
    start = window.findChild(QObject, "meetingStartConfirm")
    assert start is not None and start.property("enabled") == (
        state == "starting" or bridge.startCanStart
    )
    if state == "starting":
        assert start.property("text") == "Начинаю…"
        options = [
            item
            for item in visual_items(window.contentItem())
            if item.objectName() == "meetingStartOption"
        ]
        assert len(options) == len(bridge.startOptions)
        for option in options:
            assert not option.property("enabled")
    if state in ERRORS:
        banner = window.findChild(QObject, "meetingStartError")
        assert banner is not None and banner.property("visible")
        assert banner.property("variant") == "error" and ERRORS[state] in content
    if state in WARNINGS:
        assert WARNINGS[state] in content and start.property("enabled")
    headset = window.findChild(QObject, "meetingStartSpeakers")
    assert headset is not None and headset.property("visible") == (state == "speakers")
    assert bridge.starts == 0 and not messages


def test_w4_focus_arrows_and_escape_never_start(start_window: Any) -> None:
    window, bridge, messages = start_window("call")
    start = window.findChild(QObject, "meetingStartConfirm")
    assert start is not None and start.property("activeFocus")
    assert start.property("visualFocus")
    QTest.keyClick(window, Qt.Key_Down)
    assert bridge.selectedSource == "room"
    QTest.keyClick(window, Qt.Key_Up)
    assert bridge.selectedSource == "call"
    QTest.keyClick(window, Qt.Key_Escape)
    assert not bridge.startDialogOpen and bridge.starts == 0
    assert not messages


def test_w4_no_app_split_requires_explicit_alternative(start_window: Any) -> None:
    window, bridge, messages = start_window("no-app-split")
    start = window.findChild(QObject, "meetingStartConfirm")
    assert start is not None and not start.property("enabled")
    options = [
        item
        for item in visual_items(window.contentItem())
        if item.objectName() == "meetingStartOption"
    ]
    assert not options[0].property("enabled")
    room = options[1]
    point = room.mapToScene(QPointF(room.width() / 2, room.height() / 2))
    QTest.mouseClick(
        window, Qt.LeftButton, Qt.NoModifier, QPoint(round(point.x()), round(point.y()))
    )
    assert bridge.selectedSource == "room" and start.property("enabled")
    assert bridge.starts == 0 and not messages


@pytest.mark.parametrize("input_kind", ["pointer", "keyboard"])
def test_w4_start_click_routes_only_to_click_handler(start_window: Any, input_kind: str) -> None:
    window, bridge, messages = start_window("default")
    if input_kind == "keyboard":
        QTest.keyClick(window, Qt.Key_Return)
    else:
        button = window.findChild(QObject, "meetingStartConfirm")
        assert button is not None
        point = button.mapToScene(QPointF(button.width() / 2, button.height() / 2))
        QTest.mouseClick(
            window, Qt.LeftButton, Qt.NoModifier, QPoint(round(point.x()), round(point.y()))
        )
    assert bridge.starts == 1 and bridge.startState == "starting"
    assert not messages


@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
@pytest.mark.parametrize("aspect", ["geometry", "palette"])
def test_w4_starting_keeps_spinner_text_inside_primary_button(
    start_window: Any, dark: bool, aspect: str
) -> None:
    window, _bridge, messages = start_window("starting", dark)
    button = window.findChild(QObject, "meetingStartConfirm")
    assert button is not None
    label = next(
        item
        for item in visual_items(button)
        if item.property("text") == "Начинаю…" and item.property("textFormat") is not None
    )
    spinner = next(
        item for item in visual_items(button) if "BusyIndicator" in item.metaObject().className()
    )
    if aspect == "geometry":
        assert spinner.property("running") and spinner.property("visible")
        assert spinner.width() >= 13 and spinner.height() >= 13
        assert label.width() >= label.implicitWidth()
        right = label.mapToItem(button, QPointF(label.width(), 0)).x()
        assert right <= button.width() - button.property("rightPadding") + 0.5
        left = spinner.mapToItem(button, QPointF(0, 0)).x()
        assert left >= button.property("leftPadding") - 0.5
    else:
        assert button.property("fgColor").name().lower() == ("#0b1220" if dark else "#ffffff")
        image = grab(window)
        pixel = button.mapToScene(QPointF(8, button.height() / 2))
        assert image.pixelColor(round(pixel.x()), round(pixel.y())).name().lower() == (
            "#6c93e8" if dark else "#1b3a73"
        )
    assert not messages


@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
def test_w4_starting_ignores_pointer_return_and_space(start_window: Any, dark: bool) -> None:
    window, bridge, messages = start_window("starting", dark)
    button = window.findChild(QObject, "meetingStartConfirm")
    assert button is not None
    point = button.mapToScene(QPointF(button.width() / 2, button.height() / 2))
    QTest.mouseClick(
        window, Qt.LeftButton, Qt.NoModifier, QPoint(round(point.x()), round(point.y()))
    )
    button.forceActiveFocus(Qt.TabFocusReason)
    QTest.keyClick(window, Qt.Key_Return)
    QTest.keyClick(window, Qt.Key_Space)
    assert bridge.starts == 0 and not messages


@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
def test_w4_combined_fly_network_remote_warnings_keep_controls_visible(
    start_window: Any, dark: bool
) -> None:
    # A validated call target plus Fly, network storage and remote audio is the
    # largest real preview: three options and three independent warnings.
    window, bridge, messages = start_window("call", dark)
    bridge._extra_warnings = [FLY, WARNINGS["warn-nfs"], WARNINGS["warn-remote-audio"]]
    bridge.changed.emit()
    QTest.qWait(30)
    assert window.height() <= 600
    start = window.findChild(QObject, "meetingStartConfirm")
    cancel = window.findChild(QObject, "meetingStartCancel")
    assert start is not None and cancel is not None
    assert start.property("enabled") and cancel.property("enabled")
    footer = start.parentItem()
    assert footer.mapToScene(QPointF(0, footer.height())).y() <= window.height()
    options = window.findChild(QObject, "meetingStartOptionsScroll")
    assert options is not None and options.property("contentHeight") > options.property("height")
    for warning in bridge._extra_warnings:
        assert warning in texts(window.contentItem())
    image = grab(window)
    write_snapshot(SHOTS, f"w4-combined-warnings-{'dark' if dark else 'light'}.png", image)
    assert bridge.starts == 0 and not messages


@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
def test_w4_arrow_selection_reveals_whole_option_without_moving_start_focus(
    start_window: Any, dark: bool
) -> None:
    window, bridge, messages = start_window("call", dark)
    bridge._extra_warnings = [FLY, WARNINGS["warn-nfs"], WARNINGS["warn-remote-audio"]]
    bridge.changed.emit()
    QTest.qWait(30)
    start = window.findChild(QObject, "meetingStartConfirm")
    scroll = window.findChild(QObject, "meetingStartOptionsScroll")
    assert start is not None and scroll is not None and start.property("activeFocus")
    QTest.keyClick(window, Qt.Key_Down)
    QTest.keyClick(window, Qt.Key_Down)
    QTest.qWait(20)
    assert bridge.selectedSource == "all"
    selected = next(
        item
        for item in visual_items(scroll)
        if item.objectName() == "meetingStartOption" and item.property("selected")
    )
    top = selected.mapToItem(scroll, QPointF(0, 0)).y()
    bottom = selected.mapToItem(scroll, QPointF(0, selected.height())).y()
    assert top >= -0.5 and bottom <= scroll.height() + 0.5, (top, bottom, scroll.height())
    assert start.property("activeFocus") and bridge.starts == 0 and not messages


def test_dev_flag_hides_recording_entry(rendered: Any) -> None:
    bridge = RecordingBridge(dev=False)
    bridge.open = False
    window, _bridge, messages = rendered("empty", False, bridge)
    button = window.findChild(QObject, "meetingRecord")
    assert button is not None and not button.property("visible")
    assert not messages


@pytest.mark.parametrize("action", ["confirm", "cancel"])
def test_interrupted_dialog_has_only_navigation_and_close(rendered: Any, action: str) -> None:
    bridge = RecordingBridge(dev=False)
    bridge.open = False
    window, _bridge, messages = rendered("empty", False, bridge)
    bridge.interrupted = True
    bridge.changed.emit()
    QTest.qWait(25)
    dialog = window.findChild(QObject, "meetingInterruptedDialog")
    assert dialog is not None and dialog.property("visible")
    assert dialog.property("heading") == COPY["interruptedTitle"]
    assert dialog.property("message") == bridge.interruptedText
    assert dialog.property("confirmText") == "Открыть встречи"
    assert dialog.property("cancelText") == "Закрыть"
    if action == "confirm":
        QTest.keyClick(window, Qt.Key_Tab)
        QTest.keyClick(window, Qt.Key_Return)
        assert bridge.opened_meetings == 1
    else:
        QTest.keyClick(window, Qt.Key_Escape)
    assert not bridge.interruptedOpen and bridge.starts == 0
    assert not messages


def test_w4_native_modality_and_explicit_section_entry(rendered: Any) -> None:
    bridge = RecordingBridge()
    bridge.open = False
    window, _bridge, messages = rendered("empty", False, bridge)
    start = window.findChild(QObject, "meetingStartWindow")
    assert start is not None and not start.property("visible")
    bridge.requestRecording()
    QTest.qWait(30)
    assert start.property("visible") and start.property("modality") == Qt.WindowModal
    assert start.transientParent() is window
    assert bridge.starts == 0
    assert not messages


def test_interrupted_recovery_reveals_hidden_window_only_once(rendered: Any) -> None:
    bridge = RecordingBridge(dev=False)
    bridge.open = False
    window, _bridge, messages = rendered("empty", False, bridge)
    window.hide()
    QTest.qWait(20)
    bridge.changed.emit()
    assert not window.isVisible()
    bridge.interrupted = True
    bridge.changed.emit()
    QTest.qWait(25)
    dialog = window.findChild(QObject, "meetingInterruptedDialog")
    assert window.isVisible(), "recovery Popup must reveal its hidden parent window"
    assert dialog is not None and dialog.property("visible")
    window.hide()
    for _ in range(3):
        bridge.changed.emit()
        QTest.qWait(10)
    assert not window.isVisible(), "unchanged recovery must not steal focus again"
    bridge.closeInterrupted()
    bridge.interrupted = True
    bridge.changed.emit()
    QTest.qWait(25)
    assert window.isVisible() and dialog.property("visible")
    assert bridge.starts == 0 and not bridge.startDialogOpen
    assert bridge.opened_meetings == 0
    assert messages == (
        ["This plugin does not support raise()"] * 2
        if QGuiApplication.platformName() == "offscreen"
        else []
    )


def test_interrupted_recovery_already_pending_at_hidden_window_creation(meetings_app: Any) -> None:
    from PyQt5 import sip

    messages: list[str] = []
    previous = qInstallMessageHandler(lambda _m, _c, message: messages.append(message))
    engine = QQmlApplicationEngine()
    source, bridge = ThemeSource(False), RecordingBridge(dev=False)
    bridge.open, bridge.interrupted = False, True
    engine.rootContext().setContextProperty("themeSource", source)
    engine.rootContext().setContextProperty("meetingBridge", bridge)
    install_icon_provider(engine)
    try:
        engine.load(QUrl.fromLocalFile(str(REPO / "qml/Main.qml")))
        assert engine.rootObjects(), messages
        window = engine.rootObjects()[0]
        QTest.qWait(30)
        assert window.isVisible()  # no explicit window.show() in this test
        dialog = window.findChild(QObject, "meetingInterruptedDialog")
        assert dialog is not None and dialog.property("visible")
        assert bridge.starts == 0 and not bridge.startDialogOpen
        assert messages == (
            ["This plugin does not support raise()"]
            if QGuiApplication.platformName() == "offscreen"
            else []
        )
    finally:
        sip.delete(engine)
        meetings_app.processEvents()
        qInstallMessageHandler(previous)


def test_interrupted_recovery_next_identical_message_remains_reachable(rendered: Any) -> None:
    bridge = RecordingBridge(dev=False)
    bridge.open = False
    window, _bridge, messages = rendered("empty", False, bridge)
    bridge.interrupted, bridge.interrupted_remaining = True, 1
    bridge.changed.emit()
    QTest.qWait(25)
    dialog = window.findChild(QObject, "meetingInterruptedDialog")
    assert dialog is not None and dialog.property("visible")
    QTest.keyClick(window, Qt.Key_Escape)
    QTest.qWait(30)
    assert bridge.interruptedOpen and dialog.property("visible")
    QTest.keyClick(window, Qt.Key_Escape)
    QTest.qWait(30)
    assert not bridge.interruptedOpen and not dialog.property("visible")
    assert bridge.starts == 0 and not bridge.startDialogOpen and not messages


def test_recording_fixture_does_not_invent_real_bridge_api(meetings_app: Any) -> None:
    from astra_voice.core.settings import Settings
    from astra_voice.ui.meeting_bridge import MeetingBridge

    real = MeetingBridge(Settings(), known_apps=(), submit=lambda work: work())
    fixture = RecordingBridge()
    try:
        actual, fake = real.metaObject(), fixture.metaObject()
        for index in range(fake.propertyCount()):
            name = fake.property(index).name()
            actual_index = actual.indexOfProperty(name)
            assert actual_index >= 0, name
            assert actual.property(actual_index).typeName() == fake.property(index).typeName(), name
        for index in range(fake.methodOffset(), fake.methodCount()):
            signature = fake.method(index).methodSignature()
            assert actual.indexOfMethod(signature) >= 0, bytes(signature)
        for key, value in COPY.items():
            assert real.startTexts[key] == value
    finally:
        real.shutdown()


@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
def test_w4_light_dark_1280_contact_sheets(start_window: Any, dark: bool) -> None:
    images: list[tuple[str, QImage]] = []
    theme = "dark" if dark else "light"
    for state in STATES:
        window, _bridge, messages = start_window(state, dark)
        start = window.findChild(QObject, "meetingStartConfirm")
        assert start is not None
        if start.property("enabled"):
            assert start.property("activeFocus") and start.property("visualFocus")
        image = grab(window)
        assert image.width() == 460 and not image.isNull()
        write_snapshot(SHOTS, f"w4-{state}-{theme}.png", image)
        images.append((state, image))
        assert not messages
    board = QImage(1280, ((len(images) + 1) // 2) * 650 + 30, QImage.Format_RGB32)
    board.fill(Qt.GlobalColor.black if dark else Qt.GlobalColor.lightGray)
    painter = QPainter(board)
    painter.setPen(Qt.GlobalColor.white if dark else Qt.GlobalColor.black)
    for index, (state, image) in enumerate(images):
        x, y = 80 + index % 2 * 640, 30 + index // 2 * 650
        painter.drawText(x, y, f"W4 · {state}")
        painter.drawImage(x, y + 12, image)
    painter.end()
    write_snapshot(SHOTS, f"w4-{theme}-1280.png", board)
