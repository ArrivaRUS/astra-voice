"""AppImage about controls and modal consent, with no filesystem action or real buses."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest
from PyQt5 import sip
from PyQt5.QtCore import (
    QObject,
    QPoint,
    Qt,
    QUrl,
    pyqtProperty,
    pyqtSignal,
    pyqtSlot,
    qInstallMessageHandler,
)
from PyQt5.QtGui import QFont
from PyQt5.QtQml import QQmlApplicationEngine, QQmlEngine
from PyQt5.QtQuick import QQuickItem
from PyQt5.QtTest import QTest

from astra_voice.core import paths
from astra_voice.platform import userinstall
from astra_voice.ui import appimage_management as management_module
from astra_voice.ui.about_bridge import AboutBridge
from astra_voice.ui.appimage_management import AppImageManagement
from astra_voice.ui.icons import install_icon_provider
from helpers.qt_app import get_qapplication

pytestmark = pytest.mark.xvfb
ROOT = Path(__file__).resolve().parents[2]
os.environ["QT_QUICK_CONTROLS_STYLE"] = "Default"


def value(name: str) -> Callable[[Any], Any]:
    return lambda self: self.values[name]


class Management(QObject):
    changed = pyqtSignal()

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[str, str]] = []
        self.refresh_count = 0
        self.values = {
            "state": "ready",
            "appPath": "/home/пользователь/" + "длинная-папка-<plain-text>-" * 9 + "/app",
            "busyReason": "",
            "resultText": "",
            "errorText": "",
            "confirmationId": "",
            "confirmationAction": "",
            "confirmationMessage": "",
        }

    state = pyqtProperty(str, value("state"), notify=changed)
    appPath = pyqtProperty(str, value("appPath"), notify=changed)
    busyReason = pyqtProperty(str, value("busyReason"), notify=changed)
    resultText = pyqtProperty(str, value("resultText"), notify=changed)
    errorText = pyqtProperty(str, value("errorText"), notify=changed)
    confirmationId = pyqtProperty(str, value("confirmationId"), notify=changed)
    confirmationAction = pyqtProperty(str, value("confirmationAction"), notify=changed)
    confirmationMessage = pyqtProperty(str, value("confirmationMessage"), notify=changed)
    canUnregister = pyqtProperty(bool, lambda self: self.state == "ready", notify=changed)
    canRemove = pyqtProperty(bool, lambda self: self.state == "ready", notify=changed)

    @pyqtSlot(str)
    def requestAction(self, action: str) -> None:
        self.calls.append(("request", action))
        self.values.update(
            state="confirming",
            confirmationId="consent-1",
            confirmationAction=action,
            confirmationMessage=(
                "Будут удалены установленные AppImage-копии Astra Voice из папки:\n"
                + self.appPath
                + "\n\nМодели, настройки и журналы останутся.\n\n"
                + "Сохранённые данные:\n"
                + self.appPath
                + "/models\n"
                + self.appPath
                + "/config"
            ),
        )
        self.changed.emit()

    @pyqtSlot(str)
    def confirmAction(self, token: str) -> None:
        self.calls.append(("confirm", token))
        self.values.update(state="removing", confirmationId="")
        self.changed.emit()

    @pyqtSlot(str)
    def cancelConfirmation(self, token: str) -> None:
        self.calls.append(("cancel", token))
        self.values.update(state="ready", confirmationId="")
        self.changed.emit()

    @pyqtSlot()
    def retry(self) -> None:
        self.calls.append(("retry", ""))

    @pyqtSlot()
    def refresh(self) -> None:
        self.refresh_count += 1


class ThemeSource(QObject):
    changed = pyqtSignal()

    def __init__(self, dark: bool) -> None:
        super().__init__()
        self._dark = dark

    dark = pyqtProperty(bool, lambda self: self._dark, notify=changed)
    accent = pyqtProperty(str, lambda self: "#1B3A73", notify=changed)


def find(window: Any, name: str) -> Any:
    found = window.findChild(QObject, name)
    assert found is not None, name
    return found


def descendants(item: QQuickItem) -> Iterator[QQuickItem]:
    yield item
    for child in item.childItems():
        yield from descendants(child)


def scale_fonts(item: QQuickItem, scale: float) -> None:
    fonts = [(child, child.property("font")) for child in descendants(item)]
    for child, original in fonts:
        if isinstance(original, QFont) and original.pixelSize() > 0:
            font = QFont(original)
            font.setPixelSize(round(original.pixelSize() * scale))
            child.setProperty("font", font)


def screenshot(window: Any, directory: Path, name: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    capture = window.contentItem().grabToImage()
    assert capture is not None
    for _ in range(30):
        if not capture.image().isNull():
            break
        QTest.qWait(20)
    assert capture.image().save(str(directory / name))


@contextmanager
def render(
    tmp_path: Path,
    *,
    width: int = 672,
    dark: bool = False,
    kind: str = "appimage-installed",
    bridge: bool = True,
    controller: AppImageManagement | None = None,
) -> Iterator[tuple[Any, Any, list[str]]]:
    app = get_qapplication()
    messages: list[str] = []
    old = qInstallMessageHandler(lambda _mode, _context, message: messages.append(message))
    engine = QQmlApplicationEngine()
    install_icon_provider(engine)
    management = controller if controller is not None else Management()
    theme = ThemeSource(dark)
    about = AboutBridge(
        installed=False,
        documents={},
        update_state=None,
        models_dir=tmp_path / "models",
        settings_dir=tmp_path / "settings",
        logs_dir=tmp_path / "logs",
    )
    about._install_kind = kind
    try:
        for name, obj in (
            ("aboutBridge", about),
            ("themeSource", theme),
            ("appImageManagement", management if bridge else None),
        ):
            engine.rootContext().setContextProperty(name, obj)
        QQmlEngine.setObjectOwnership(theme, QQmlEngine.CppOwnership)
        engine.globalObject().setProperty("themeSource", engine.newQObject(theme))
        engine.loadData(
            b"""
import QtQuick 2.15
import QtQuick.Controls 2.15
import "."
import "sections"
ApplicationWindow {
    visible: true
    width: 704; height: 720
    color: Theme.bgApp
    Rectangle { anchors.fill: parent; color: Theme.bgApp }
    Flickable {
        objectName: "viewport"
        anchors.fill: parent
        contentWidth: width
        contentHeight: section.height + 32
        clip: true
        About { id: section; objectName: "aboutSection"; x: 16; y: 16; width: parent.width - 32 }
    }
}
""",
            QUrl.fromLocalFile(str(ROOT / "qml/AppImageTest.qml")),
        )
        assert engine.rootObjects(), messages
        window = engine.rootObjects()[0]
        window.setWidth(width + 32)
        window.requestActivate()
        QTest.qWait(80)
        group = find(window, "aboutAppImageManagement")
        find(window, "viewport").setProperty("contentY", max(0, group.y()))
        QTest.qWait(30)
        yield window, management, messages
    finally:
        if controller is not None:
            assert controller.close()
        sip.delete(engine)
        sip.delete(about)
        sip.delete(management)
        sip.delete(theme)
        app.processEvents()
        qInstallMessageHandler(old)


def test_fake_management_matches_real_metaobject() -> None:
    for name in (
        "state",
        "appPath",
        "canUnregister",
        "canRemove",
        "busyReason",
        "resultText",
        "errorText",
        "confirmationId",
        "confirmationAction",
        "confirmationMessage",
    ):
        assert AppImageManagement.staticMetaObject.indexOfProperty(name) >= 0
    for method in (
        "requestAction(QString)",
        "confirmAction(QString)",
        "cancelConfirmation(QString)",
        "retry()",
        "refresh()",
    ):
        assert AppImageManagement.staticMetaObject.indexOfMethod(method.encode()) >= 0


@pytest.mark.parametrize("width", [480, 672])
@pytest.mark.parametrize("scale", [1.0, 1.5, 2.0])
@pytest.mark.parametrize("dark", [False, True])
def test_management_layout_no_clipping(
    tmp_path: Path, width: int, scale: float, dark: bool
) -> None:
    with render(tmp_path, width=width, dark=dark) as (window, _management, messages):
        group = find(window, "aboutAppImageManagement")
        scale_fonts(group, scale)
        QTest.qWait(40)
        for name in ("aboutUnregisterAppImage", "aboutRemoveAppImage"):
            button = find(window, name)
            assert button.width() <= width - 28
            content = button.property("contentItem")
            assert content.property("truncated") is False
            assert content.property("paintedHeight") <= content.height() + 1
            row = button.parentItem()
            assert button.x() >= 14
            assert button.x() + button.width() <= row.width() - 13
            assert button.y() + button.height() <= row.height() - 6
        path = find(window, "aboutAppImagePath")
        assert path.property("readOnly") is True and path.property("selectByMouse") is True
        assert path.property("textFormat") == 0  # TextEdit.PlainText
        assert path.property("contentWidth") <= path.width() + 1
        assert path.property("contentHeight") <= path.height() + 1
        assert not messages, messages
        directory = Path(os.environ.get("ASTRA_VOICE_SNAPSHOT_DIR", str(tmp_path)))
        directory.mkdir(parents=True, exist_ok=True)
        capture = window.contentItem().grabToImage()
        assert capture is not None
        for _ in range(30):
            if not capture.image().isNull():
                break
            QTest.qWait(20)
        assert capture.image().save(str(directory / f"appimage-{width}-{scale}-{dark}.png"))


@pytest.mark.parametrize("kind", ["deb", "source", "unknown"])
def test_non_appimage_hides_management(tmp_path: Path, kind: str) -> None:
    with render(tmp_path, kind=kind) as (window, _management, messages):
        assert find(window, "aboutAppImageManagement").property("visible") is False
        assert not messages, messages


def test_missing_controller_never_enables_actions(tmp_path: Path) -> None:
    with render(tmp_path, bridge=False) as (window, _management, messages):
        assert find(window, "aboutAppImageManagement").property("visible") is True
        for name in ("aboutUnregisterAppImage", "aboutRemoveAppImage"):
            assert find(window, name).property("enabled") is False
        assert "недоступно" in find(window, "aboutAppImageStatus").property("text")
        assert not messages, messages


@pytest.mark.parametrize("action", ["remove", "unregister"])
def test_confirmation_keyboard_and_tokens(tmp_path: Path, action: str) -> None:
    with render(tmp_path, width=480) as (window, management, messages):
        button = find(
            window, "aboutRemoveAppImage" if action == "remove" else "aboutUnregisterAppImage"
        )
        button.clicked.emit()
        QTest.qWait(80)
        dialog = find(window, "aboutAppImageDialog")
        cancel = dialog.findChild(QObject, "dialogCancel")
        confirm = dialog.findChild(QObject, "dialogConfirm")
        assert cancel is not None and confirm is not None
        assert dialog.property("visible") is True
        assert cancel.property("activeFocus") is True
        assert management.calls == [("request", action)]
        QTest.mouseClick(window, Qt.LeftButton, pos=QPoint(1, 1))
        assert dialog.property("visible") is True
        for key, focus in ((Qt.Key_Tab, confirm), (Qt.Key_Tab, cancel), (Qt.Key_Backtab, confirm)):
            QTest.keyClick(window, key)
            assert focus.property("activeFocus") is True
        QTest.keyClick(window, Qt.Key_Escape)
        QTest.qWait(80)
        assert dialog.property("visible") is False
        assert button.property("activeFocus") is True
        assert management.calls[-1] == ("cancel", "consent-1")
        button.clicked.emit()
        QTest.qWait(80)
        QTest.keyClick(window, Qt.Key_Tab)
        QTest.keyClick(window, Qt.Key_Return)
        QTest.qWait(80)
        assert management.calls[-1] == ("confirm", "consent-1")
        assert dialog.property("visible") is False
        assert not messages, messages


@pytest.mark.parametrize("focus_name", ["dialogCancel", "dialogConfirm"])
def test_confirmation_keyboard_scroll_reaches_all_consequences(
    tmp_path: Path, focus_name: str
) -> None:
    with render(tmp_path, width=480) as (window, management, messages):
        window.setHeight(440)
        find(window, "aboutRemoveAppImage").clicked.emit()
        QTest.qWait(80)
        dialog = find(window, "aboutAppImageDialog")
        body = dialog.property("contentItem")
        scale_fonts(body, 2.0)
        QTest.qWait(30)
        focused = dialog.findChild(QObject, focus_name)
        focused.forceActiveFocus(Qt.TabFocusReason)
        assert body.property("contentY") == 0
        QTest.keyClick(window, Qt.Key_PageDown)
        assert body.property("contentY") > 0
        QTest.keyClick(window, Qt.Key_PageUp)
        assert body.property("contentY") == 0
        QTest.keyClick(window, Qt.Key_End)
        assert body.property("contentY") == pytest.approx(
            body.property("contentHeight") - body.height()
        )
        QTest.keyClick(window, Qt.Key_Home)
        assert body.property("contentY") == 0
        assert focused.property("activeFocus") is True
        assert management.calls == [("request", "remove")]
        QTest.keyClick(window, Qt.Key_Escape)
        assert management.calls[-1] == ("cancel", "consent-1")
        assert not messages, messages


@pytest.mark.parametrize("action", ["unregister", "remove"])
@pytest.mark.parametrize("width", [480, 672])
@pytest.mark.parametrize("scale", [1.0, 2.0])
def test_real_backend_full_confirmation_and_keyboard_scroll(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, action: str, width: int, scale: float
) -> None:
    get_qapplication()
    # Only the filesystem snapshot is supplied by the fixture. The actual QObject,
    # requestAction/refresh worker and complete backend consent text are exercised.
    app_path = tmp_path / "домашняя-папка-пользователя" / "astra-voice" / "app"
    app_path.mkdir(parents=True)
    snapshot = userinstall.ManagementSnapshot(
        app_path=app_path,
        models_path=tmp_path / "сохранённые-данные" / "models",
        settings_path=tmp_path / "сохранённые-настройки" / "config",
        installed=("test-installed-copy",),
        menu="ours",
        icons=(),
        autostart_owned=True,
        autostart_program=str(app_path / "current" / "AppRun"),
        deb_available=True,
        identities=(),
    )
    monkeypatch.setattr(paths, "install_kind", lambda: paths.InstallKind.APPIMAGE_INSTALLED)
    monkeypatch.setattr(management_module, "_snapshot", lambda: snapshot)
    mutate = Mock(side_effect=AssertionError("UI render must never mutate an installation"))
    monkeypatch.setattr(userinstall, "unregister", mutate)
    monkeypatch.setattr(userinstall, "remove_program", mutate)
    runtime = Mock()
    runtime.appimage_remove_busy_reason.return_value = ""
    controller = AppImageManagement(
        runtime, own_lock_path=tmp_path / "lock", running_key="test-installed-copy"
    )
    with render(tmp_path, width=width, dark=scale == 2.0, controller=controller) as (
        window,
        _management,
        messages,
    ):
        for _ in range(100):
            if controller.state == "ready":
                break
            QTest.qWait(10)
        assert controller.state == "ready", controller.errorText
        window.setHeight(440)
        opener = find(
            window, "aboutRemoveAppImage" if action == "remove" else "aboutUnregisterAppImage"
        )
        opener.clicked.emit()
        for _ in range(100):
            if controller.state == "confirming":
                break
            QTest.qWait(10)
        assert controller.state == "confirming", controller.errorText
        dialog = find(window, "aboutAppImageDialog")
        assert dialog.property("message") == controller.confirmationMessage
        assert "Автозапуск будет переключён на системную версию" in controller.confirmationMessage
        if action == "remove":
            assert "Исходный файл .AppImage останется" in controller.confirmationMessage
            assert str(snapshot.models_path) in controller.confirmationMessage
            assert str(snapshot.settings_path) in controller.confirmationMessage
        else:
            assert "Копии программы останутся" in controller.confirmationMessage
            assert "Astra Voice продолжит работу" in controller.confirmationMessage
        body = dialog.property("contentItem")
        for part in (body, dialog.property("header"), dialog.property("footer")):
            scale_fonts(part, scale)
        QTest.qWait(50)
        directory = Path(os.environ.get("ASTRA_VOICE_SNAPSHOT_DIR", str(tmp_path)))
        prefix = f"real-{action}-{width}-{scale}"
        screenshot(window, directory, prefix + "-top.png")
        # D1 at 100% may fit in full; long D2 and enlarged text must scroll.
        if action == "remove" or scale > 1:
            assert body.property("contentHeight") > body.height()
        for focus_name in ("dialogCancel", "dialogConfirm"):
            focused = dialog.findChild(QObject, focus_name)
            focused.forceActiveFocus(Qt.TabFocusReason)
            QTest.keyClick(window, Qt.Key_End)
            assert body.property("contentY") == pytest.approx(
                body.property("contentHeight") - body.height()
            )
            assert focused.property("activeFocus") is True
            screenshot(window, directory, prefix + "-" + focus_name + "-bottom.png")
            QTest.keyClick(window, Qt.Key_Home)
            assert body.property("contentY") == 0
        QTest.keyClick(window, Qt.Key_Escape)
        QTest.qWait(50)
        assert not dialog.property("visible")
        assert opener.property("activeFocus") is True
        mutate.assert_not_called()
        runtime.reserve_appimage_removal.assert_not_called()
        assert app_path.is_dir()
        assert not messages, messages


@pytest.mark.parametrize("scale", [1.0, 1.5, 2.0])
@pytest.mark.parametrize("width", [480, 672])
@pytest.mark.parametrize("dark", [False, True])
def test_small_dialog_scroll_and_persistent_error(
    tmp_path: Path, scale: float, width: int, dark: bool
) -> None:
    with render(tmp_path, width=width, dark=dark) as (window, management, messages):
        window.setHeight(440)
        find(window, "aboutRemoveAppImage").clicked.emit()
        QTest.qWait(80)
        dialog = find(window, "aboutAppImageDialog")
        content = dialog.property("contentItem")
        for part in (content, dialog.property("header"), dialog.property("footer")):
            scale_fonts(part, scale)
        QTest.qWait(30)
        assert dialog.property("height") <= window.height() - 24
        assert content.property("contentHeight") > content.height()
        assert content.property("interactive") is True
        assert dialog.findChild(QObject, "dialogCancel").property("visible") is True
        assert dialog.findChild(QObject, "dialogConfirm").property("visible") is True
        for name in ("dialogCancel", "dialogConfirm"):
            button = dialog.findChild(QObject, name)
            label = button.property("contentItem")
            assert label.property("truncated") is False
            assert label.property("paintedHeight") <= label.height() + 1
            assert button.x() >= 0
            assert button.x() + button.width() <= button.parentItem().width() + 1
            assert button.y() + button.height() <= button.parentItem().height() + 1
        directory = Path(os.environ.get("ASTRA_VOICE_SNAPSHOT_DIR", str(tmp_path)))
        directory.mkdir(parents=True, exist_ok=True)
        capture = window.contentItem().grabToImage()
        assert capture is not None
        for _ in range(30):
            if not capture.image().isNull():
                break
            QTest.qWait(20)
        assert capture.image().save(str(directory / f"appimage-dialog-{width}-{scale}-{dark}.png"))
        management.values.update(
            state="error", confirmationId="", errorText="Часть копий могла остаться."
        )
        management.changed.emit()
        QTest.qWait(30)
        assert not dialog.property("visible")
        assert "Часть копий могла остаться" in find(window, "aboutAppImageStatus").property("text")
        find(window, "aboutRetryAppImage").clicked.emit()
        assert management.calls[-1] == ("retry", "")
        assert not messages, messages


@pytest.mark.parametrize("key", [Qt.Key_Return, Qt.Key_Space])
def test_initial_keyboard_activation_cancels(tmp_path: Path, key: int) -> None:
    with render(tmp_path) as (window, management, messages):
        find(window, "aboutRemoveAppImage").clicked.emit()
        QTest.qWait(80)
        QTest.keyClick(window, key)
        QTest.qWait(80)
        assert management.calls == [("request", "remove"), ("cancel", "consent-1")]
        assert not find(window, "aboutAppImageDialog").property("visible")
        assert not messages, messages


@pytest.mark.parametrize(
    ("state", "text"),
    [
        ("checking", "Проверяем установку"),
        ("unregistering", "Убираем регистрацию"),
        ("removing", "Готовим удаление"),
        ("busy", "Дождитесь вставки текста"),
        ("exiting", "Другие работающие копии будут удалены после их завершения"),
    ],
)
def test_pending_states_show_backend_outcome_without_claiming_deletion(
    tmp_path: Path, state: str, text: str
) -> None:
    with render(tmp_path) as (window, management, messages):
        management.values.update(state=state, busyReason=text, resultText=text)
        management.changed.emit()
        QTest.qWait(30)
        assert text in find(window, "aboutAppImageStatus").property("text")
        for name in ("aboutUnregisterAppImage", "aboutRemoveAppImage"):
            assert not find(window, name).property("enabled")
        assert not messages, messages


def test_new_confirmation_token_resets_focus_and_old_token_cannot_be_sent(tmp_path: Path) -> None:
    with render(tmp_path) as (window, management, messages):
        find(window, "aboutRemoveAppImage").clicked.emit()
        QTest.qWait(80)
        QTest.keyClick(window, Qt.Key_Tab)
        management.values.update(confirmationId="consent-2", confirmationMessage="Новый путь")
        management.changed.emit()
        QTest.qWait(80)
        dialog = find(window, "aboutAppImageDialog")
        assert dialog.findChild(QObject, "dialogCancel").property("activeFocus") is True
        QTest.keyClick(window, Qt.Key_Return)
        assert management.calls[-1] == ("cancel", "consent-2")
        assert not messages, messages


def test_dialog_returns_focus_to_path_when_action_becomes_unavailable(tmp_path: Path) -> None:
    with render(tmp_path) as (window, management, messages):
        find(window, "aboutRemoveAppImage").clicked.emit()
        QTest.qWait(80)
        management.values.update(state="error", confirmationId="", errorText="Копии изменились.")
        management.changed.emit()
        QTest.qWait(80)
        assert not find(window, "aboutAppImageDialog").property("visible")
        assert find(window, "aboutAppImagePath").property("activeFocus") is True
        assert management.calls == [("request", "remove")]
        assert not messages, messages


def test_portable_without_installed_copies_keeps_group_and_disables_actions(tmp_path: Path) -> None:
    with render(tmp_path, kind="appimage-portable") as (window, management, messages):
        management.values.update(state="absent", appPath="")
        management.changed.emit()
        QTest.qWait(30)
        assert find(window, "aboutAppImageManagement").property("visible") is True
        assert "нет установленных копий" in find(window, "aboutAppImagePath").property("text")
        for name in ("aboutUnregisterAppImage", "aboutRemoveAppImage"):
            assert not find(window, name).property("enabled")
        assert not messages, messages


def test_late_controller_updates_actions_and_confirmation(tmp_path: Path) -> None:
    with render(tmp_path, bridge=False) as (window, management, messages):
        assert not find(window, "aboutRemoveAppImage").property("enabled")
        engine = QQmlEngine.contextForObject(window).engine()
        engine.rootContext().setContextProperty("appImageManagement", management)
        QTest.qWait(50)
        assert find(window, "aboutRemoveAppImage").property("enabled")
        find(window, "aboutRemoveAppImage").clicked.emit()
        QTest.qWait(50)
        assert find(window, "aboutAppImageDialog").property("visible")
        assert management.calls == [("request", "remove")]
        assert not messages, messages


@pytest.mark.parametrize("trigger", ["reopen", "activation", "late"])
@pytest.mark.parametrize("initial", ["absent", "unregistered"])
def test_external_install_or_registration_refreshes_on_return(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, trigger: str, initial: str
) -> None:
    get_qapplication()
    current = userinstall.ManagementSnapshot(
        app_path=tmp_path / "app",
        models_path=tmp_path / "models",
        settings_path=tmp_path / "config",
        installed=() if initial == "absent" else ("installed-copy",),
        menu="absent",
        icons=(),
        autostart_owned=False,
        autostart_program=None,
        deb_available=False,
        identities=(),
    )
    monkeypatch.setattr(paths, "install_kind", lambda: paths.InstallKind.APPIMAGE_PORTABLE)
    read_snapshot = Mock(side_effect=lambda: current)
    monkeypatch.setattr(management_module, "_snapshot", read_snapshot)
    runtime = Mock()
    runtime.appimage_remove_busy_reason.return_value = ""
    controller = AppImageManagement(runtime, own_lock_path=tmp_path / "lock", running_key=None)
    with render(
        tmp_path, kind="appimage-portable", controller=controller, bridge=trigger != "late"
    ) as (window, _management, messages):
        for _ in range(100):
            if controller.state != "checking":
                break
            QTest.qWait(10)
        assert controller.state == ("absent" if initial == "absent" else "ready")
        assert not controller.canUnregister
        before = read_snapshot.call_count
        current = replace(current, installed=("installed-copy",), menu="ours")
        if trigger == "reopen":
            section = find(window, "aboutSection")
            section.setVisible(False)
            section.setVisible(True)
        elif trigger == "activation":
            assert window.isActive()
            window.activeChanged.emit()
        else:
            engine = QQmlEngine.contextForObject(window).engine()
            engine.rootContext().setContextProperty("appImageManagement", controller)
        for _ in range(100):
            if controller.canUnregister:
                break
            QTest.qWait(10)
        assert read_snapshot.call_count > before
        assert controller.state == "ready"
        assert find(window, "aboutUnregisterAppImage").property("enabled") is True
        assert find(window, "aboutRemoveAppImage").property("enabled") is True
        runtime.reserve_appimage_removal.assert_not_called()
        assert not messages, messages


@pytest.mark.parametrize(
    "state", ["confirming", "checking", "unregistering", "removing", "exiting", "error"]
)
def test_return_does_not_refresh_active_consent_or_operation(tmp_path: Path, state: str) -> None:
    with render(tmp_path) as (window, management, messages):
        if state == "confirming":
            find(window, "aboutRemoveAppImage").clicked.emit()
        else:
            management.values.update(state=state)
            management.changed.emit()
        QTest.qWait(30)
        before = management.refresh_count
        token = management.confirmationId
        section = find(window, "aboutSection")
        section.setVisible(False)
        section.setVisible(True)
        window.activeChanged.emit()
        # Replacing the context with a live operation must be guarded too.
        context = QQmlEngine.contextForObject(window).engine().rootContext()
        context.setContextProperty("appImageManagement", None)
        context.setContextProperty("appImageManagement", management)
        QTest.qWait(30)
        assert management.refresh_count == before
        assert management.state == state
        assert management.confirmationId == token
        if state == "confirming":
            assert find(window, "aboutAppImageDialog").property("visible")
        assert not messages, messages


@pytest.mark.parametrize("width,scale", [(480, 1.0), (480, 2.0), (672, 1.0), (672, 2.0)])
@pytest.mark.parametrize("state", ["ready", "absent", "busy", "partial", "error", "deb", "source"])
def test_management_state_render_matrix(
    tmp_path: Path, width: int, scale: float, state: str
) -> None:
    kind = state if state in {"deb", "source"} else "appimage-installed"
    with render(tmp_path, width=width, dark=scale == 2.0, kind=kind) as (
        window,
        management,
        messages,
    ):
        management.values.update(appPath="/home/пользователь/.local/lib/astra-voice/app")
        if state == "partial":
            management.values.update(
                state="error",
                errorText="Часть файлов убрать не удалось. Некоторые копии могут остаться. "
                "Модели, настройки и журналы сохранены.",
            )
        elif state == "error":
            management.values.update(state="error", errorText="Не удалось проверить установку.")
        elif state not in {"deb", "source"}:
            management.values.update(state=state, busyReason="Дождитесь завершения вставки текста.")
        management.changed.emit()
        group = find(window, "aboutAppImageManagement")
        scale_fonts(group, scale)
        if state in {"deb", "source"}:
            assert not group.property("visible")
            find(window, "viewport").setProperty("contentY", 0)
        else:
            assert group.property("visible")
        QTest.qWait(50)
        screenshot(
            window,
            Path(os.environ.get("ASTRA_VOICE_SNAPSHOT_DIR", str(tmp_path))),
            f"appimage-state-{state}-{width}-{scale}.png",
        )
        assert not messages, messages
