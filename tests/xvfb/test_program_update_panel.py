"""Author checks for the real M7/action2b QML panel (isolated Xvfb)."""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from typing import Any

import pytest
from PyQt5 import sip
from PyQt5.QtCore import QPointF, Qt
from PyQt5.QtTest import QTest

pytestmark = pytest.mark.xvfb
REPO = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "_program_panel_states", Path(__file__).with_name("test_update_states.py")
)
assert _spec and _spec.loader
states: Any = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = states
_spec.loader.exec_module(states)
SNAPSHOTS = Path(
    os.environ.get("ASTRA_VOICE_SNAPSHOT_DIR_PROGRAM_UPDATES", "/tmp/astra-program-update-panel")
)
NOTES = [
    {"text": "Улучшена работа настроек программы.", "bullet": True},
    {"text": "Исправлены ошибки проверки обновлений.", "bullet": True},
]


@pytest.fixture(scope="module")
def app() -> Any:
    return states.get_qapplication()


def values(phase: str, track: str = "appimage") -> dict[str, Any]:
    busy = phase in {"metadata", "downloading", "verifying"}
    return {
        **states.AVAILABLE,
        "notes": NOTES,
        "artifactTrack": track,
        "downloadPhase": phase,
        "downloadBusy": busy,
        "canCancelDownload": busy,
        "canDownload": phase in {"idle", "cancelled"},
        "canRetryDownload": phase == "error",
        "canInstallAndRestart": phase == "readyappimage",
        "canOpenFolder": phase in {"readydeb", "readyappimage"},
        "canSkipVersion": not busy,
        "canRemindLater": not busy,
        "downloadPercent": 42,
        "downloadReceived": 42000000,
        "folderPath": "/home/user/.cache/astra-voice/updates/verified-0.2.1",
        "adminInstruction": "sudo apt install ./astra-voice_0.2.1_amd64.deb"
        if track == "deb"
        else "",
        "downloadError": "signature-invalid" if phase == "error" else "",
        "downloadErrorText": (
            "Подпись или целостность не подтверждены. Этот файл нельзя использовать."
        )
        if phase == "error"
        else "",
    }


@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
@pytest.mark.parametrize("size", [(1024, 620), (900, 588)], ids=["normal", "minimum"])
@pytest.mark.parametrize(
    "phase",
    [
        "idle",
        "metadata",
        "downloading",
        "verifying",
        "readydeb",
        "readyappimage",
        "error",
        "cancelled",
        "installing",
        "offline",
    ],
)
def test_actual_program_panel(app: Any, dark: bool, size: tuple[int, int], phase: str) -> None:
    bridge_values = values(
        "idle" if phase == "offline" else phase, "deb" if phase == "readydeb" else "appimage"
    )
    if phase == "offline":
        bridge_values.update(
            networkRefusal="offline", checkRefusal="offline", canCheckNow=False, canDownload=False
        )
    updates = states.FakeUpdates(**bridge_values)
    settings = states.make_settings({})
    original_size = states.WIDTH, states.HEIGHT
    states.WIDTH, states.HEIGHT = size
    name = f"program-{phase}-{'dark' if dark else 'light'}-{size[0]}x{size[1]}"

    def inspect(window: Any) -> None:
        panel = states.find_object(window, "updatePanel")
        operation = states.find_object(window, "updateOperation")
        primary = states.find_object(window, "updatePrimary")
        cancel = states.find_object(window, "updateCancel")
        assert panel.width() == size[0] - 184 - 44
        assert operation.height() >= (190 if phase == "readydeb" else 120)
        assert primary.width() == 264
        assert cancel.parentItem().width() == 92
        assert primary.x() == 0 and cancel.parentItem().x() == 272
        assert bool(cancel.property("visible")) == (
            phase in {"metadata", "downloading", "verifying"}
        )
        assert states.find_object(window, "updateReleaseVersion").property("textFormat") == 0
        if phase == "offline":
            assert states.find_object(window, "updateNewBadge").property("visible") is False
            assert "сохранённые сведения о версии" in states.find_object(
                window, "updateArtifactMetadata"
            ).property("text")
            assert (
                states.find_object(window, "updateOperationTitle").property("text")
                == "Скачивание недоступно"
            )
            assert (
                states.find_object(window, "statusUpdateText").property("text")
                == "Офлайн-режим · Подробнее"
            )
            assert primary.property("enabled") is False
        point = panel.mapToItem(window.contentItem(), QPointF(0, 0))
        image = states.grab(app, window, *size)
        crop = image.copy(
            int(point.x()),
            int(point.y()),
            int(panel.width()),
            min(int(panel.height()), size[1] - int(point.y()) - 36),
        )
        SNAPSHOTS.mkdir(parents=True, exist_ok=True)
        assert crop.save(str(SNAPSHOTS / f"{name}-panel.png"))

    try:
        image, texts, messages = states.render_network(app, dark, updates, settings, inspect)
        assert not messages, "\n".join(messages)
        assert "Что нового" in texts
        if phase == "error":
            assert "Не удалось проверить обновление" in texts
        assert not updates.calls  # Opening the panel never starts download / install.
        assert image.save(str(SNAPSHOTS / f"{name}.png"))
    finally:
        states.WIDTH, states.HEIGHT = original_size
        sip.delete(updates)
        sip.delete(settings)


@pytest.mark.parametrize(
    ("phase", "overrides", "action"),
    [
        ("idle", {}, "download"),
        ("error", {}, "retryDownload"),
        (
            "error",
            {
                "canRetryDownload": False,
                "canDownload": True,
                "canOpenFolder": True,
                "downloadError": "launch-unconfirmed",
            },
            "download",
        ),
        ("readydeb", {}, "openFolder"),
        ("readyappimage", {}, "installAndRestart"),
        (
            "readyappimage",
            {
                "canInstallAndRestart": False,
                "installRefusal": "unsupported",
                "installRefusalText": "Установка этой сборки недоступна.",
            },
            "openFolder",
        ),
        (
            "error",
            {
                "canInstallAndRestart": True,
                "canRetryDownload": False,
                "downloadError": "prepare-failed",
            },
            "installAndRestart",
        ),
    ],
)
def test_explicit_primary_only(
    app: Any, phase: str, overrides: dict[str, Any], action: str
) -> None:
    updates = states.FakeUpdates(
        **{**values(phase, "deb" if phase == "readydeb" else "appimage"), **overrides}
    )
    settings = states.make_settings({})
    try:
        _, _, messages = states.render_network(
            app,
            False,
            updates,
            settings,
            lambda w: states.onboarding.click_item(states.find_object(w, "updatePrimary")),
        )
        assert not messages, "\n".join(messages)
        assert updates.calls == [action]
    finally:
        sip.delete(updates)
        sip.delete(settings)


def test_local_ready_offline_and_busy_refusal(app: Any) -> None:
    updates = states.FakeUpdates(**{**values("readyappimage"), "networkRefusal": "offline"})
    settings = states.make_settings({})

    def inspect(window: Any) -> None:
        primary = states.find_object(window, "updatePrimary")
        assert primary.property("enabled") is True
        updates.update(
            canInstallAndRestart=False,
            installRefusal="busy",
            installRefusalText="Дождитесь завершения текущей операции.",
        )
        app.processEvents()
        assert primary.property("enabled") is False
        assert "Завершите диктовку, чтобы установить обновление" in states.onboarding.visible_texts(
            window.contentItem()
        )

    try:
        _, _, messages = states.render_network(app, False, updates, settings, inspect)
        assert not messages, "\n".join(messages)
        assert updates.calls == []
    finally:
        sip.delete(updates)
        sip.delete(settings)


def test_cancel_escape_and_ready_focus(app: Any) -> None:
    updates = states.FakeUpdates(**values("metadata"))
    settings = states.make_settings({})

    def inspect(window: Any) -> None:
        cancel = states.find_object(window, "updateCancel")
        cancel.forceActiveFocus()
        QTest.keyClick(window, Qt.Key_Escape)
        app.processEvents()
        assert updates.calls == ["cancelDownload"]
        updates.update(
            downloadPhase="readyappimage",
            downloadBusy=False,
            canCancelDownload=False,
            canInstallAndRestart=True,
        )
        app.processEvents()
        assert states.find_object(window, "updatePanelHeading").hasActiveFocus()
        assert states.grab(app, window, states.WIDTH, states.HEIGHT).save(
            str(SNAPSHOTS / "program-heading-focus-light.png")
        )
        QTest.keyClick(window, Qt.Key_Return)
        assert updates.calls == ["cancelDownload"]
        QTest.keyClick(window, Qt.Key_Tab)
        assert states.find_object(window, "updatePrimary").hasActiveFocus()

    try:
        _, _, messages = states.render_network(app, False, updates, settings, inspect)
        assert not messages, "\n".join(messages)
    finally:
        sip.delete(updates)
        sip.delete(settings)


def test_notes_are_plain_and_long_content_wraps(app: Any) -> None:
    updates = states.FakeUpdates(
        **{
            **values("readydeb", "deb"),
            "version": "0.2.1-" + "abcdef" * 30,
            "notes": [
                {
                    "text": '<img src="https://invalid.example/x">'
                    + "release" * 60
                    + "\nВторой абзац",
                    "bullet": True,
                }
            ],
            "folderPath": "/home/user/" + "longfolder" * 40,
        }
    )
    settings = states.make_settings({})

    def inspect(window: Any) -> None:
        for name in ("updateFolderPath", "updateAdminInstruction", "updateReleaseVersion"):
            item = states.find_object(window, name)
            assert item.property("textFormat") == 0
            assert item.property("readOnly") is True
            assert item.width() <= states.find_object(window, "updatePanel").width()
        line = states.find_object(window, "updateNoteLine")
        assert line.property("textFormat") == 0
        assert line.height() > 20

    try:
        _, _, messages = states.render_network(app, True, updates, settings, inspect)
        assert not messages, "\n".join(messages)
    finally:
        sip.delete(updates)
        sip.delete(settings)


@pytest.mark.parametrize("track", ["appimage", "deb"])
def test_action_slots_stay_fixed_and_focus_stays_local(app: Any, track: str) -> None:
    updates = states.FakeUpdates(**values("idle", track))
    settings = states.make_settings({})

    def inspect(window: Any) -> None:
        action_row = states.find_object(window, "updateActionRow")
        original_y = action_row.y()
        outside = states.find_object(window, "checkAppUpdatesToggle")
        outside.forceActiveFocus()
        for phase in (
            "metadata",
            "downloading",
            "verifying",
            "readydeb" if track == "deb" else "readyappimage",
        ):
            updates.update(**values(phase, track))
            QTest.qWait(30)
            app.processEvents()
            assert action_row.y() == original_y
            assert outside.hasActiveFocus()
        updates.update(downloadPhase="error", downloadError="signature-invalid")
        app.processEvents()
        assert outside.hasActiveFocus()
        QTest.keyClick(window, Qt.Key_Escape)
        assert updates.calls == []

    try:
        _, _, messages = states.render_network(app, False, updates, settings, inspect)
        assert not messages, "\n".join(messages)
    finally:
        sip.delete(updates)
        sip.delete(settings)


def test_bridge_missing_then_connected_does_not_start_download(app: Any) -> None:
    messages, previous = states.collect_messages()
    engine = states.QQmlApplicationEngine()
    states.install_icon_provider(engine)
    updates = states.FakeUpdates(**values("idle"))
    settings = states.make_settings({})
    try:
        context = engine.rootContext()
        context.setContextProperty("showOnboarding", False)
        context.setContextProperty("settingsBridge", settings)
        engine.load(states.QUrl.fromLocalFile(str(REPO / "qml/Main.qml")))
        window = engine.rootObjects()[0]
        window.show()
        QTest.qWait(150)
        states.onboarding.select_section(app, window, "network")
        assert states.find_object(window, "checkNowButton").property("enabled") is False
        context.setContextProperty("updatesBridge", updates)
        QTest.qWait(50)
        app.processEvents()
        assert states.find_object(window, "updatePrimary").property("enabled") is True
        assert updates.calls == []
    finally:
        sip.delete(engine)
        sip.delete(updates)
        sip.delete(settings)
        app.processEvents()
        states.qInstallMessageHandler(previous)
    assert not messages, "\n".join(messages)


@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
def test_narrow_column_with_long_notes_and_empty_notes(app: Any, dark: bool) -> None:
    updates = states.FakeUpdates(
        **{
            **values("idle"),
            "version": "0.2.1-" + "abcdef" * 30,
            "notes": [
                {"text": '<img src="https://invalid.example/x">' + "release" * 60, "bullet": True}
            ],
        }
    )
    settings = states.make_settings({})

    def inspect(window: Any) -> None:
        panel = states.find_object(window, "updatePanel")
        panel.setWidth(480)
        QTest.qWait(50)
        app.processEvents()
        assert panel.width() == 480
        action_row = states.find_object(window, "updateActionRow")
        release = states.find_object(window, "updateReleasePage")
        primary = states.find_object(window, "updatePrimary")
        cancel = states.find_object(window, "updateCancel")
        assert release.y() >= primary.height() + 8
        assert cancel.parentItem().x() == 272
        version = states.find_object(window, "updateReleaseVersion")
        assert version.height() > 20
        SNAPSHOTS.mkdir(parents=True, exist_ok=True)
        assert states.grab(app, window, states.WIDTH, states.HEIGHT).save(
            str(SNAPSHOTS / f"program-stress-{'dark' if dark else 'light'}.png")
        )
        primary.forceActiveFocus()
        QTest.qWait(50)
        point = primary.mapToItem(window.contentItem(), QPointF(0, 0))
        assert point.y() >= 0 and point.y() + primary.height() < window.height() - 36
        assert states.grab(app, window, states.WIDTH, states.HEIGHT).save(
            str(SNAPSHOTS / f"program-stress-actions-{'dark' if dark else 'light'}.png")
        )
        updates.update(notes=[], version="0.2.1")
        QTest.qWait(50)
        app.processEvents()
        assert "Описание изменений не предоставлено" in states.onboarding.visible_texts(
            window.contentItem()
        )
        assert action_row.y() > 0

    try:
        _, _, messages = states.render_network(app, dark, updates, settings, inspect)
        assert not messages, "\n".join(messages)
    finally:
        sip.delete(updates)
        sip.delete(settings)


@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
def test_artificial_font_stress_150_percent(app: Any, dark: bool) -> None:
    """Harness font override; this does not change the system or production Theme."""
    from PyQt5.QtGui import QFont, QFontMetricsF

    updates = states.FakeUpdates(**values("readyappimage"))
    settings = states.make_settings({})

    def inspect(window: Any) -> None:
        panel = states.find_object(window, "updatePanel")
        panel.setWidth(480)
        QTest.qWait(50)
        originals = []
        for item in states.onboarding.visual_tree(panel):
            font = item.property("font")
            if isinstance(font, QFont) and font.pixelSize() > 0:
                originals.append((item, QFont(font), item.property("lineHeight")))
        for item, original, line_height in originals:
            font = QFont(original)
            font.setPixelSize(round(original.pixelSize() * 1.5))
            assert item.setProperty("font", font)
            if isinstance(line_height, (int, float)) and line_height > 2:
                item.setProperty("lineHeight", line_height * 1.5)
            if item.property("variant") is not None:
                item.setProperty("implicitHeight", max(51, QFontMetricsF(font).height() + 12))
        QTest.qWait(60)
        primary = states.find_object(window, "updatePrimary")
        release = states.find_object(window, "updateReleasePage")
        primary.forceActiveFocus()
        QTest.qWait(50)
        assert primary.property("text") == "Установить и перезапустить"
        assert primary.width() >= primary.implicitWidth()
        assert primary.width() <= panel.width() - 34
        assert primary.height() >= 51
        assert release.y() >= primary.height() + 8
        point = primary.mapToItem(window.contentItem(), QPointF(0, 0))
        assert point.y() + primary.height() < window.height() - 36
        assert states.grab(app, window, states.WIDTH, states.HEIGHT).save(
            str(SNAPSHOTS / f"program-font150-{'dark' if dark else 'light'}.png")
        )
        for name in ("updatePrimary", "updateReleasePage", "updateSkip", "updateRemindLater"):
            control = states.find_object(window, name)
            control.forceActiveFocus()
            QTest.qWait(20)
            pos = control.mapToItem(window.contentItem(), QPointF(0, 0))
            assert pos.y() >= 0 and pos.y() + control.height() < window.height() - 36
        assert updates.calls == []

    try:
        _, _, messages = states.render_network(app, dark, updates, settings, inspect)
        assert not messages, "\n".join(messages)
    finally:
        sip.delete(updates)
        sip.delete(settings)


@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
@pytest.mark.parametrize("phase", ["verifying", "readydeb", "readyappimage"])
def test_offline_preserves_local_phase_and_action(app: Any, dark: bool, phase: str) -> None:
    updates = states.FakeUpdates(
        **{
            **values(phase, "deb" if phase == "readydeb" else "appimage"),
            "networkRefusal": "offline",
            "checkRefusal": "offline",
            "canCheckNow": False,
            "canDownload": False,
            "canRetryDownload": False,
        }
    )
    settings = states.make_settings({})
    original_size = states.WIDTH, states.HEIGHT
    states.WIDTH, states.HEIGHT = 900, 588

    def inspect(window: Any) -> None:
        primary = states.find_object(window, "updatePrimary")
        assert primary.property("enabled") is (phase != "verifying")
        expected_footer = {
            "verifying": "Проверяю обновление…",
            "readydeb": "Пакет проверен · Открыть обновления",
            "readyappimage": "Обновление готово · Открыть обновления",
        }[phase]
        assert states.find_object(window, "statusUpdateText").property("text") == expected_footer
        assert (
            states.find_object(window, "updateOperationTitle").property("text")
            != "Скачивание недоступно"
        )
        if phase == "verifying":
            assert (
                states.find_object(window, "updateProgress").property("Accessible.ignored")
                is not True
            )
        else:
            states.onboarding.click_item(primary)

    try:
        image, _, messages = states.render_network(app, dark, updates, settings, inspect)
        assert not messages, "\n".join(messages)
        assert updates.calls == (
            []
            if phase == "verifying"
            else ["openFolder" if phase == "readydeb" else "installAndRestart"]
        )
        assert image.save(
            str(SNAPSHOTS / f"program-offline-{phase}-{'dark' if dark else 'light'}-900x588.png")
        )
    finally:
        states.WIDTH, states.HEIGHT = original_size
        sip.delete(updates)
        sip.delete(settings)


@pytest.mark.parametrize("scenario", ["available", "busy", "disabled-and-hidden"])
def test_panel_forward_reverse_focus_actions(app: Any, scenario: str) -> None:
    bridge_values = values("metadata" if scenario == "busy" else "idle")
    order = ["updatePrimary", "updateReleasePage", "updateSkip", "updateRemindLater"]
    if scenario == "busy":
        bridge_values.update(canSkipVersion=False, canRemindLater=False)
        order = ["updateCancel", "updateReleasePage"]
    elif scenario == "disabled-and-hidden":
        bridge_values.update(canDownload=False, releasePageAvailable=False)
        order = ["updateSkip", "updateRemindLater"]
    updates = states.FakeUpdates(**bridge_values)
    settings = states.make_settings({})

    def inspect(window: Any) -> None:
        window.requestActivate()
        first = states.find_object(window, order[0])
        first.forceActiveFocus(Qt.TabFocusReason)
        QTest.qWait(40)
        for name in order[1:]:
            QTest.keyClick(window, Qt.Key_Tab)
            QTest.qWait(30)
            assert states.find_object(window, name).hasActiveFocus(), name
        for name in reversed(order[:-1]):
            QTest.keyClick(window, Qt.Key_Backtab, Qt.ShiftModifier)
            QTest.qWait(30)
            assert states.find_object(window, name).hasActiveFocus(), name
        heading = states.find_object(window, "updatePanelHeading")
        heading.forceActiveFocus(Qt.TabFocusReason)
        QTest.keyClick(window, Qt.Key_Tab)
        QTest.qWait(30)
        assert first.hasActiveFocus()
        assert updates.calls == []

    try:
        _, _, messages = states.render_network(app, False, updates, settings, inspect)
        assert not messages, "\n".join(messages)
    finally:
        sip.delete(updates)
        sip.delete(settings)


@pytest.mark.parametrize("key", [Qt.Key_Return, Qt.Key_Enter], ids=["return", "enter"])
@pytest.mark.parametrize(
    ("phase", "button", "action", "checker_state"),
    [
        ("idle", "updatePrimary", "download", "available"),
        ("readyappimage", "updatePrimary", "installAndRestart", "available"),
        ("metadata", "updateCancel", "cancelDownload", "available"),
        ("idle", "updateReleasePage", "openReleasePage", "available"),
        ("idle", "updateSkip", "skipVersion", "available"),
        ("idle", "updateRemindLater", "remindLater", "available"),
        ("idle", "updateCheckAgain", "checkNow", "uptodate"),
        ("idle", "updateShowSkipped", "clearSkip", "skipped"),
    ],
)
def test_enter_gesture_executes_only_focused_panel_action(
    app: Any, key: int, phase: str, button: str, action: str, checker_state: str
) -> None:
    updates = states.FakeUpdates(**{**values(phase), "state": checker_state, "manual": True})
    settings = states.make_settings({})

    def inspect(window: Any) -> None:
        window.requestActivate()
        control = states.find_object(window, button)
        control.forceActiveFocus(Qt.TabFocusReason)
        QTest.qWait(40)
        QTest.keyClick(window, key)
        QTest.qWait(30)
        assert updates.calls == [action]

    try:
        _, _, messages = states.render_network(app, False, updates, settings, inspect)
        assert not messages, "\n".join(messages)
    finally:
        sip.delete(updates)
        sip.delete(settings)


@pytest.mark.parametrize("key", [Qt.Key_Return, Qt.Key_Enter], ids=["return", "enter"])
def test_enter_repeat_orphan_release_and_ready_transition_are_safe(app: Any, key: int) -> None:
    from PyQt5.QtCore import QEvent
    from PyQt5.QtGui import QGuiApplication, QKeyEvent

    class DownloadFake(states.FakeUpdates):
        @states.pyqtSlot()
        def download(self) -> None:
            # Real UpdatesBridge publishes metadata before download() returns.
            self.calls.append("download")
            self.update(**values("metadata"))

    updates = DownloadFake(**values("idle"))
    settings = states.make_settings({})

    def repeat(window: Any) -> None:
        for kind in (QEvent.KeyPress, QEvent.KeyRelease):
            QGuiApplication.sendEvent(window, QKeyEvent(kind, key, Qt.NoModifier, "\r", True, 1))
        app.processEvents()

    def inspect(window: Any) -> None:
        window.requestActivate()
        primary = states.find_object(window, "updatePrimary")
        primary.forceActiveFocus(Qt.TabFocusReason)
        QTest.qWait(40)
        QTest.keyRelease(window, key)
        repeat(window)
        assert updates.calls == []
        QTest.keyPress(window, key)
        states.find_object(window, "updateReleasePage").forceActiveFocus(Qt.TabFocusReason)
        QTest.keyRelease(window, key)
        assert updates.calls == []
        primary.forceActiveFocus(Qt.TabFocusReason)
        QTest.keyClick(window, key)
        QTest.qWait(30)
        assert updates.calls == ["download"]
        assert states.find_object(window, "updateCancel").hasActiveFocus()
        repeat(window)
        updates.update(**values("readyappimage"))
        QTest.qWait(30)
        assert states.find_object(window, "updatePanelHeading").hasActiveFocus()
        repeat(window)
        QTest.keyRelease(window, key)
        assert updates.calls == ["download"]
        QTest.keyClick(window, Qt.Key_Tab)
        QTest.keyClick(window, key)
        QTest.qWait(30)
        assert updates.calls == ["download", "installAndRestart"]
        QTest.keyRelease(window, key)
        assert updates.calls == ["download", "installAndRestart"]

    try:
        _, _, messages = states.render_network(app, False, updates, settings, inspect)
        assert not messages, "\n".join(messages)
    finally:
        sip.delete(updates)
        sip.delete(settings)
