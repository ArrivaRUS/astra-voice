"""E13 M1: section and D1 rendering, keyboard contract and private snapshots."""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest
from PyQt5.QtCore import (
    QObject,
    Qt,
    QUrl,
    pyqtProperty,
    pyqtSignal,
    pyqtSlot,
    qInstallMessageHandler,
)
from PyQt5.QtGui import QFontDatabase
from PyQt5.QtQml import QQmlApplicationEngine
from PyQt5.QtTest import QTest

from astra_voice.meetings.settings_ext import MeetingSettings
from astra_voice.ui.icons import install_icon_provider
from helpers.qt_app import get_qapplication
from helpers.snapshots import snapshot_dir, write_snapshot

pytestmark = pytest.mark.xvfb
REPO = Path(__file__).resolve().parents[2]
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QUICK_BACKEND", "software")


class ThemeSource(QObject):
    def __init__(self, dark: bool) -> None:
        super().__init__()
        self._dark = dark

    @pyqtProperty(bool, constant=True)
    def dark(self) -> bool:
        return self._dark


class Bridge(QObject):
    changed = pyqtSignal()

    def __init__(self, state: str) -> None:
        super().__init__()
        self.state = state
        self.dialog = False
        self.long_loading = True
        self.title = "Планирование недели"
        self.kind = "success"
        self.row_active = False
        self.actions = ["Сохранить как…", "Удалить"]
        self.accepted: list[bool] = []
        self.values = asdict(
            MeetingSettings(meetings_enabled=state not in {"off", "off-saved", "policy-off"})
        )

    @pyqtProperty(str, notify=changed)
    def sectionState(self) -> str:
        return self.state

    @pyqtProperty(bool, notify=changed)
    def enabled(self) -> bool:
        return bool(self.values["meetings_enabled"])

    @pyqtProperty(bool, notify=changed)
    def canEnable(self) -> bool:
        return self.state != "policy-off"

    @pyqtProperty(bool, notify=changed)
    def recording(self) -> bool:
        return self.state == "recording"

    @pyqtProperty(bool, notify=changed)
    def loadingVisible(self) -> bool:
        return self.state == "loading" and self.long_loading

    @pyqtProperty(bool, notify=changed)
    def loading(self) -> bool:
        return self.state == "loading"

    @pyqtProperty(bool, notify=changed)
    def readError(self) -> bool:
        return self.state == "error"

    @pyqtProperty("QVariantList", notify=changed)
    def meetings(self) -> list[dict[str, object]]:
        if self.state not in {"success", "off-saved"}:
            return []
        return [
            {
                "id": "test",
                "title": self.title,
                "subtitle": "12.10.2026 · 14:00 · 0:42:10 · 42 МБ",
                "badge": "✓ Готово",
                "badgeKind": self.kind,
                "action": "Открыть",
                "actions": self.actions,
                "progress": 0,
                "active": self.row_active,
            }
        ]

    @pyqtProperty(str, notify=changed)
    def totalText(self) -> str:
        return "Всего: 1 встреча · 42 МБ"

    @pyqtProperty("QVariantMap", notify=changed)
    def settings(self) -> dict[str, object]:
        return self.values

    @pyqtProperty("QVariantMap", notify=changed)
    def locked(self) -> dict[str, bool]:
        return {
            "meeting_offer_on_call": self.state == "policy-some",
            "meeting_audio_retention_days": self.state == "policy-some",
        }

    @pyqtProperty("QVariantMap", constant=True)
    def texts(self) -> dict[str, str]:
        return {}

    @pyqtProperty(str, notify=changed)
    def policyNote(self) -> str:
        return (
            "Часть настроек задана администратором вашей организации — "
            "они показаны серым и недоступны для изменения"
            if self.state == "policy-some"
            else ""
        )

    @pyqtProperty(str, notify=changed)
    def policyInvalidText(self) -> str:
        return (
            "Значение политики некорректно — обратитесь к администратору"
            if self.state == "policy-off"
            else ""
        )

    @pyqtProperty(str, notify=changed)
    def flyWarning(self) -> str:
        return ""

    @pyqtProperty(bool, notify=changed)
    def responsibilityOpen(self) -> bool:
        return self.dialog

    @pyqtProperty("QVariantList", constant=True)
    def responsibilityItems(self) -> list[dict[str, object]]:
        return []

    @pyqtProperty(str, constant=True)
    def responsibilityParagraph(self) -> str:
        return "На рабочем компьютере действуют правила вашей организации о записи совещаний."

    @pyqtProperty(bool, constant=True)
    def detectorVisible(self) -> bool:
        return True

    @pyqtProperty(bool, constant=True)
    def detectorEnabled(self) -> bool:
        return True

    @pyqtProperty(str, constant=True)
    def detectorCaption(self) -> str:
        return "Программа только спросит — запись сама не начнётся"

    @pyqtProperty(str, constant=True)
    def knownAppsText(self) -> str:
        return "DION · звонок в браузере"

    @pyqtProperty(bool, constant=True)
    def aiDisabled(self) -> bool:
        return False

    @pyqtProperty(bool, constant=True)
    def aiAdminAddress(self) -> bool:
        return False

    @pyqtProperty(str, constant=True)
    def endpointCaption(self) -> str:
        return ""

    @pyqtSlot()
    def openResponsibility(self) -> None:
        if self.canEnable:
            self.dialog = True
            self.changed.emit()

    @pyqtSlot()
    def cancelResponsibility(self) -> None:
        self.dialog = False
        self.changed.emit()

    @pyqtSlot(bool)
    def acceptResponsibility(self, detector: bool) -> None:
        self.accepted.append(detector)
        self.cancelResponsibility()

    @pyqtSlot()
    def retry(self) -> None:
        pass

    @pyqtSlot()
    def requestRecording(self) -> None:
        pass

    @pyqtSlot()
    def disableMeetings(self) -> None:
        pass

    @pyqtSlot(str, "QVariant")
    def setSetting(self, key: str, value: object) -> None:
        self.values[key] = value
        self.changed.emit()

    @pyqtSlot(str, str)
    def requestMeetingAction(self, meeting_id: str, action: str) -> None:
        pass


@pytest.fixture(scope="module")
def meetings_app() -> Any:
    app = get_qapplication()
    # Resolve the real UI font before Main.qml can cache a missing-family fallback
    # for this process, including later onboarding renders in a different engine.
    paths = sorted((REPO / "tests/fixtures/fonts/pt-root-ui").glob("*.ttf"))
    assert paths
    for path in paths:
        assert QFontDatabase.addApplicationFont(str(path)) >= 0, path
    # Keep fonts registered: live Qt text caches can retain them after engine teardown.
    return app


@pytest.fixture
def rendered(meetings_app: Any) -> Iterator[Any]:
    app = meetings_app
    messages: list[str] = []
    previous = qInstallMessageHandler(lambda _m, _c, message: messages.append(message))
    active: list[tuple[Any, Any, Any, list[str]]] = []

    def create(
        state: str, dark: bool, supplied: Any = None, factory: Any = None
    ) -> tuple[Any, Any, list[str]]:
        messages.clear()
        engine = QQmlApplicationEngine()
        if factory is not None:
            engine.setNetworkAccessManagerFactory(factory)
        theme, bridge = ThemeSource(dark), supplied if supplied is not None else Bridge(state)
        engine.rootContext().setContextProperty("themeSource", theme)
        engine.rootContext().setContextProperty("meetingBridge", bridge)
        install_icon_provider(engine)
        try:
            engine.load(QUrl.fromLocalFile(str(REPO / "qml/Main.qml")))
            assert engine.rootObjects(), messages
            window = engine.rootObjects()[0]
            sidebar = window.findChild(QObject, "settingsSidebar")
            assert sidebar is not None, "meeting sidebar integration is missing"
            window.setProperty("freezeAnimations", True)
            assert sidebar.setProperty("currentIndex", 3)
            window.setWidth(1024)
            window.setHeight(588)
            from test_update_states import add_background

            background = add_background(engine, window)
            window.show()
            QTest.qWait(100)
            app.processEvents()
            assert window.findChild(QObject, "meetingsSection") is not None
            assert not messages, messages
        finally:
            pass
        active.append((engine, window, (theme, bridge, factory, background), messages))
        return window, bridge, messages

    yield create
    from PyQt5 import sip

    for engine, window, _owners, _messages in active:
        window.close()
        # Destroy the engine while context QObject owners are still alive.
        sip.delete(engine)
    app.processEvents()
    qInstallMessageHandler(previous)


def grab(window: Any) -> Any:
    # Qt5/offscreen may not implement QQuickWindow.grabWindow; use the same
    # content-item fallback as test_pill_states instead of accepting blank PNG.
    from PyQt5.QtGui import QImage, QPainter

    assert window.property("color").alpha() == 255, window.property("color").getRgb()
    result = None
    for _attempt in range(20):
        image = window.grabWindow()
        if not image.isNull():
            captured = image
            opaque = QImage(captured.size(), QImage.Format_ARGB32_Premultiplied)
            opaque.fill(window.property("color"))
            painter = QPainter(opaque)
            painter.drawImage(0, 0, captured)
            painter.end()
            return opaque
        if result is None:
            result = window.contentItem().grabToImage()
        QTest.qWait(25)
        if result is not None and not result.image().isNull():
            captured = result.image()
            # contentItem snapshots omit the window clear color; composite it
            # explicitly so transparent pixels represent the actual app surface.
            image = QImage(captured.size(), QImage.Format_ARGB32_Premultiplied)
            image.fill(window.property("color"))
            painter = QPainter(image)
            painter.drawImage(0, 0, captured)
            painter.end()
            return image
    raise AssertionError("Qt did not produce a section image")


def snapshots() -> Path:
    # Always outside design; this task never replaces owner-approved references.
    env_var = (
        "ASTRA_VOICE_SNAPSHOT_DIR_MEETINGS"
        if os.environ.get("ASTRA_VOICE_SNAPSHOT_DIR_MEETINGS")
        else "ASTRA_VOICE_SNAPSHOT_DIR"
    )
    # This test deliberately never opts into design reference updates.
    from unittest.mock import patch

    with patch.dict(os.environ, {"ASTRA_VOICE_UPDATE_REFS": "0"}):
        return snapshot_dir(
            env_var,
            "meetings",
            "design/refs/impl/meetings",
        )


@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
@pytest.mark.parametrize("state", ["off", "policy-off", "policy-some", "empty", "success"])
def test_meeting_section_states_render_without_warnings_and_snapshot(
    rendered: Any, state: str, dark: bool
) -> None:
    window, _bridge, messages = rendered(state, dark)
    image = grab(window)
    assert not image.isNull()
    assert image.pixelColor(900, 400).alpha() == 255, (
        image.width(),
        image.height(),
        image.pixelColor(0, 0).getRgb(),
        window.property("color").getRgb(),
    )
    name = f"meetings-section-{state}-{'dark' if dark else 'light'}.png"
    write_snapshot(snapshots(), name, image)
    assert not messages


@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
def test_responsibility_dialog_focus_tab_escape_and_snapshot(rendered: Any, dark: bool) -> None:
    window, bridge, messages = rendered("off", dark)
    bridge.openResponsibility()
    QTest.qWait(30)
    dialog = window.findChild(QObject, "meetingResponsibility")
    cancel = window.findChild(QObject, "meetingResponsibilityCancel")
    accept = window.findChild(QObject, "meetingResponsibilityAccept")
    detector = window.findChild(QObject, "meetingResponsibilityDetector")
    assert dialog is not None and dialog.property("visible")
    assert cancel is not None and accept is not None and detector is not None
    assert dialog.property("height") <= window.height() - 48
    assert cancel.property("activeFocus")
    image = grab(window)
    assert not image.isNull()
    write_snapshot(snapshots(), f"meetings-d1-{'dark' if dark else 'light'}.png", image)
    QTest.keyClick(window, Qt.Key_Tab)
    assert accept.property("activeFocus")
    QTest.keyClick(window, Qt.Key_Tab)
    assert detector.property("activeFocus")
    QTest.keyClick(window, Qt.Key_Tab)
    assert cancel.property("activeFocus")
    QTest.keyClick(window, Qt.Key_Escape)
    assert not bridge.dialog and not bridge.accepted
    assert not messages


def test_loading_stays_blank_until_skeleton_delay(rendered: Any) -> None:
    window, bridge, _messages = rendered("loading", False)
    group = window.findChild(QObject, "meetingList")
    assert group is not None
    bridge.long_loading = False
    bridge.changed.emit()
    assert not group.property("visible")
    bridge.long_loading = True
    bridge.changed.emit()
    assert group.property("visible")


@pytest.mark.parametrize("mode", ["prompt", "manual", "off"])
def test_real_bridge_d1_enforces_human_opt_in_and_policy(rendered: Any, mode: str) -> None:
    from unittest.mock import Mock

    from astra_voice.core.policy import Policy
    from astra_voice.core.settings import Settings
    from astra_voice.meetings.apps import AppClass, AppDefinition
    from astra_voice.ui.meeting_bridge import MeetingBridge
    from astra_voice.ui.meeting_texts import RESPONSIBILITY_ITEMS

    saved = Mock()
    bridge = MeetingBridge(
        Settings(),
        policy=Policy(raw={"meeting_record": mode}),
        store=Mock(list=Mock(return_value=[])),
        save=saved,
        known_apps=(AppDefinition("dion", "DION", ("dion",), (), AppClass.DION),),
        submit=lambda work: work(),
    )
    window, _bridge, messages = rendered("off", False, bridge)
    try:
        bridge.openResponsibility()
        QTest.qWait(30)
        dialog = window.findChild(QObject, "meetingResponsibility")
        assert dialog is not None
        if mode == "off":
            assert not dialog.property("visible") and not bridge.enabled
            assert not saved.called
            return
        assert dialog.property("visible")
        texts: list[str] = []

        def visit(item: Any) -> None:
            value = item.property("text")
            if isinstance(value, str):
                texts.append(value)
            for child in item.childItems():
                visit(child)

        visit(window.contentItem())
        for item in RESPONSIBILITY_ITEMS:
            assert item["title"] in texts and item["text"] in texts
        assert bridge.responsibilityParagraph in texts
        detector = window.findChild(QObject, "meetingResponsibilityDetector")
        assert detector is not None and detector.property("enabled") == (mode != "manual")
        accept: Any = window.findChild(QObject, "meetingResponsibilityAccept")
        assert accept is not None
        accept.forceActiveFocus(Qt.TabFocusReason)
        QTest.keyClick(window, Qt.Key_Return)
        assert bridge.enabled and not bridge.responsibilityOpen
        assert saved.call_count == 1
        assert not bridge.settings["meeting_offer_on_call"]
        assert not messages, messages
    finally:
        bridge.shutdown()


def test_elided_meeting_title_tooltip_is_literal_and_cannot_request_html_images(
    rendered: Any,
) -> None:
    # Fail before rendering the known AutoText shape: some Qt5 builds block
    # while that vulnerable attached tooltip parses its external image.
    source = (REPO / "qml/sections/Meetings.qml").read_text()
    assert "ToolTip.text: text" not in source
    from PyQt5.QtCore import QPoint, QPointF
    from test_qml_textformat import RecordingNetworkFactory

    attack = '<img src="http://127.0.0.1:1/meeting-private.png">' + "Очень длинное название " * 3
    bridge = Bridge("success")
    bridge.title = attack
    factory = RecordingNetworkFactory()
    window, _bridge, _messages = rendered("success", False, bridge, factory)

    def visual_items(item: Any) -> Iterator[Any]:
        yield item
        for child in item.childItems():
            yield from visual_items(child)

    name = next(
        (item for item in visual_items(window.contentItem()) if item.objectName() == "meetingName"),
        None,
    )
    assert name is not None and name.property("truncated")
    point = name.mapToScene(QPointF(5, 5))
    QTest.mouseMove(window, QPoint(round(point.x()), round(point.y())))
    QTest.qWait(600)
    title_items: list[Any] = []

    def visit(item: Any) -> None:
        if item.property("text") == attack and item.metaObject().indexOfProperty("textFormat") >= 0:
            title_items.append(item)
        for child in item.childItems():
            visit(child)

    visit(window.contentItem())
    assert len(title_items) >= 2  # label plus actual opened tooltip
    assert all(item.property("textFormat") == 0 for item in title_items)  # Text.PlainText
    assert factory.urls == []


def visual_named(window: Any, name: str) -> Any:
    def items(item: Any) -> Iterator[Any]:
        yield item
        for child in item.childItems():
            yield from items(child)

    return next((item for item in items(window.contentItem()) if item.objectName() == name), None)


def test_failed_meeting_badge_uses_danger_palette(rendered: Any) -> None:
    bridge = Bridge("success")
    bridge.kind = "danger"
    window, _bridge, _messages = rendered("success", False, bridge)
    badge = visual_named(window, "meetingBadge")
    assert badge is not None
    assert badge.property("color").name().lower() == "#fbeceb"


def test_paused_meeting_keeps_stop_menu(rendered: Any) -> None:
    bridge = Bridge("success")
    bridge.row_active = True
    bridge.actions = ["Остановить"]
    window, _bridge, _messages = rendered("success", False, bridge)
    more = visual_named(window, "meetingMore")
    assert more is not None and more.property("visible")


@pytest.mark.parametrize("days", [14, 45])
def test_custom_policy_retention_is_shown_in_locked_select(rendered: Any, days: int) -> None:
    bridge = Bridge("policy-some")
    bridge.values["meeting_audio_retention_days"] = days
    window, _bridge, _messages = rendered("policy-some", False, bridge)
    select = visual_named(window, "meetingAudioRetention")
    assert select is not None
    assert select.property("currentText") == f"{days} дней"
    assert not select.property("enabled")


def test_snapshot_reference_update_flag_cannot_write_design(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from helpers import snapshots as helpers_snapshots

    target = tmp_path / "snapshots"
    monkeypatch.setenv("ASTRA_VOICE_UPDATE_REFS", "1")
    monkeypatch.setenv("ASTRA_VOICE_SNAPSHOT_DIR_MEETINGS", str(target))
    ensure = helpers_snapshots._ensure_directory

    def safe_directory(path: Path) -> None:
        assert not path.resolve().is_relative_to(REPO / "design")
        ensure(path)

    monkeypatch.setattr(helpers_snapshots, "_ensure_directory", safe_directory)
    assert snapshots() == target
    assert os.environ["ASTRA_VOICE_UPDATE_REFS"] == "1"


@pytest.mark.parametrize(
    "field_name,key",
    [
        ("meetingNoticeFieldInput", "meeting_notice_text"),
        ("meetingSelfLabelFieldInput", "meeting_self_label"),
    ],
)
def test_clearing_text_and_blurring_restores_effective_default(
    rendered: Any, field_name: str, key: str
) -> None:
    from unittest.mock import Mock

    from astra_voice.core.settings import Settings
    from astra_voice.ui.meeting_bridge import MeetingBridge

    bridge = MeetingBridge(
        Settings(meetings_enabled=True),
        store=Mock(list=Mock(return_value=[])),
        save=Mock(),
        known_apps=(),
        submit=lambda work: work(),
    )
    window, _bridge, _messages = rendered("empty", False, bridge)
    try:
        field = visual_named(window, field_name)
        assert field is not None
        expected = bridge.settings[key]
        field.forceActiveFocus(Qt.TabFocusReason)
        QTest.keyClick(window, Qt.Key_A, Qt.ControlModifier)
        QTest.keyClick(window, Qt.Key_Backspace)
        QTest.keyClick(window, Qt.Key_Tab)
        assert field.property("text") == expected
    finally:
        bridge.shutdown()


def test_meeting_qml_uses_display_guillemets() -> None:
    for name in ("sections/Meetings.qml", "components/ResponsibilityDialog.qml"):
        source = (REPO / "qml" / name).read_text()
        assert "„" not in source and "“" not in source, name


def test_responsibility_fallback_icons_match_accepted_mockup(rendered: Any) -> None:
    window, bridge, _messages = rendered("off", False)
    bridge.openResponsibility()
    QTest.qWait(30)
    dialog = window.findChild(QObject, "meetingResponsibility")
    assert dialog is not None
    items = dialog.property("fallbackItems").toVariant()
    assert [item["icon"] for item in items] == ["meeting", "rec", "home", "alert", "people"]


def test_responsibility_titles_use_medium_weight_without_trailing_periods(rendered: Any) -> None:
    from PyQt5.QtGui import QFont, QFontInfo

    window, bridge, _messages = rendered("off", False)
    bridge.openResponsibility()
    QTest.qWait(30)
    dialog = window.findChild(QObject, "meetingResponsibility")
    assert dialog is not None
    items = dialog.property("fallbackItems").toVariant()
    expected = [
        "Что записывается",
        "Когда",
        "Где хранится",
        "Предупреждать участников — ваша обязанность",
        "Только свои встречи",
    ]
    assert [item["title"] for item in items] == expected

    def walk(item: Any) -> Iterator[Any]:
        yield item
        for child in item.childItems():
            yield from walk(child)

    titles = [item for item in walk(window.contentItem()) if item.property("text") in expected]
    assert len(titles) == 5
    for item in titles:
        font = item.property("font")
        assert font.pixelSize() == 14 and font.weight() == QFont.Medium
        assert QFontInfo(font).family() == "PT Root UI"
        assert QFontInfo(font).weight() == QFont.Medium


def test_responsibility_header_uses_application_brand_mark(rendered: Any) -> None:
    window, bridge, _messages = rendered("off", False)
    bridge.openResponsibility()
    QTest.qWait(30)
    dialog = window.findChild(QObject, "meetingResponsibility")
    assert dialog is not None and dialog.property("brandHeader") is True
    mark = visual_named(window, "dialogHeaderBrand")
    assert mark is not None and mark.property("visible")
    assert mark.property("vbW") == 22 and mark.property("tray")
    assert mark.property("size") == 22


@pytest.mark.parametrize("label", ["Текст для чата встречи", "Текст для приглашения на встречу"])
def test_unfocused_meeting_fields_show_text_from_start(rendered: Any, label: str) -> None:
    window, _bridge, _messages = rendered("empty", False)

    def walk(item: Any) -> Iterator[Any]:
        yield item
        for child in item.childItems():
            yield from walk(child)

    row = next(item for item in walk(window.contentItem()) if item.property("label") == label)
    field = row.property("field")
    assert field is not None and not field.property("activeFocus")
    assert field.property("cursorPosition") == 0
    assert field.positionToRectangle(0).x() >= -1


def test_d1_save_failure_is_displayed_and_retry_can_enable(rendered: Any) -> None:
    from unittest.mock import Mock

    from astra_voice.core.settings import Settings
    from astra_voice.ui.meeting_bridge import MeetingBridge

    saved = Mock(side_effect=[OSError("PRIVATE_MARKER /secret/path"), None])
    bridge = MeetingBridge(
        Settings(),
        store=Mock(list=Mock(return_value=[])),
        save=saved,
        known_apps=(),
        submit=lambda work: work(),
    )
    window, _bridge, messages = rendered("off", False, bridge)
    try:
        bridge.openResponsibility()
        QTest.qWait(30)
        accept = window.findChild(QObject, "meetingResponsibilityAccept")
        assert accept is not None
        accept.forceActiveFocus(Qt.TabFocusReason)
        QTest.keyClick(window, Qt.Key_Return)
        error = visual_named(window, "meetingResponsibilitySaveError")
        assert error is not None and error.property("visible")
        assert error.property("body") == "Не удалось сохранить настройки. Попробуйте ещё раз."
        assert bridge.responsibilityOpen and not bridge.enabled
        accept.forceActiveFocus(Qt.TabFocusReason)
        QTest.keyClick(window, Qt.Key_Return)
        assert bridge.enabled and not bridge.responsibilityOpen and bridge.saveError == ""
        assert saved.call_count == 2 and not messages
    finally:
        bridge.shutdown()


def test_section_save_failure_is_displayed_and_cleared_after_success(rendered: Any) -> None:
    from unittest.mock import Mock

    from astra_voice.core.settings import Settings
    from astra_voice.ui.meeting_bridge import MeetingBridge

    attempts = 0

    def save(_settings: object) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("PRIVATE_MARKER /secret/path")

    saved = Mock(side_effect=save)
    bridge = MeetingBridge(
        Settings(meetings_enabled=True),
        store=Mock(list=Mock(return_value=[])),
        save=saved,
        known_apps=(),
        submit=lambda work: work(),
    )
    window, _bridge, messages = rendered("empty", False, bridge)
    try:
        field = visual_named(window, "meetingNoticeFieldInput")
        assert field is not None
        field.forceActiveFocus(Qt.TabFocusReason)
        QTest.keyClick(window, Qt.Key_A, Qt.ControlModifier)
        for character in "replacement":
            QTest.keyClick(window, ord(character.upper()))
        button = visual_named(window, "meetingDisable")
        assert button is not None
        button.forceActiveFocus(Qt.TabFocusReason)
        QTest.qWait(1)
        error = visual_named(window, "meetingSaveError")
        assert error is not None and error.property("visible")
        assert error.property("body") == "Не удалось сохранить настройки. Попробуйте ещё раз."
        field.forceActiveFocus(Qt.TabFocusReason)
        QTest.keyClick(window, Qt.Key_A, Qt.ControlModifier)
        for character in "replacement":
            QTest.keyClick(window, ord(character.upper()))
        button = visual_named(window, "meetingDisable")
        assert button is not None
        button.forceActiveFocus(Qt.TabFocusReason)
        QTest.qWait(1)
        assert bridge.saveError == "" and not error.property("visible") and not messages
        assert saved.call_count == 2
    finally:
        bridge.shutdown()


def test_offer_toggle_is_unavailable_without_detector_apps(rendered: Any) -> None:
    from unittest.mock import Mock

    from astra_voice.core.settings import Settings
    from astra_voice.ui.meeting_bridge import MeetingBridge

    saved = Mock()
    bridge = MeetingBridge(
        Settings(meetings_enabled=True),
        store=Mock(list=Mock(return_value=[])),
        save=saved,
        known_apps=(),
        submit=lambda work: work(),
    )
    window, _bridge, messages = rendered("empty", False, bridge)
    try:
        toggle = visual_named(window, "meetingOfferToggle")
        assert toggle is not None and not toggle.property("enabled")
        assert not toggle.property("checked")
        toggle.forceActiveFocus(Qt.TabFocusReason)
        QTest.keyClick(window, Qt.Key_Space)
        assert not toggle.property("checked") and not bridge.settings["meeting_offer_on_call"]
        assert not saved.called and not messages
    finally:
        bridge.shutdown()
