"""Independent keyboard contracts exercised on actual Qt/QML under private Xvfb.

Backend actions are recorded, never executed. Prior-warning and held-key cases
use the real UpdatesBridge with a fake controller and actual private ready file.
"""

from __future__ import annotations

import importlib
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest
from PyQt5 import sip
from PyQt5.QtCore import QEvent, QPointF, Qt
from PyQt5.QtGui import QGuiApplication, QKeyEvent
from PyQt5.QtTest import QTest

from astra_voice.ui.updates_bridge import UpdatesBridge
from astra_voice.updates.checker import UpdateStatus
from astra_voice.updates.download import DownloadStatus, FileIdentity, VerifiedDownload
from astra_voice.updates.release import VerifiedArtifact, VerifiedRelease

states = importlib.import_module("xvfb.test_update_states")
pytestmark = pytest.mark.xvfb
SNAPSHOTS = Path("/tmp/astra-updater-independent-keyboard")


@pytest.fixture(scope="module")
def app() -> Any:
    assert os.environ.get("QT_QPA_PLATFORM") == "xcb", "keyboard acceptance requires real Xvfb/xcb"
    return states.get_qapplication()


def settle(app: Any, window: Any) -> None:
    window.requestActivate()
    QTest.qWait(70)  # Let Qt deliver focus events and polish layouts before geometry assertions.
    app.processEvents()


def item(window: Any, name: str) -> Any:
    return states.find_object(window, name)


def assert_focus_visible(window: Any, name: str) -> None:
    control = item(window, name)
    focused = window.activeFocusItem()
    assert control.hasActiveFocus(), (
        f"expected {name}, focused {focused.objectName() if focused else None}"
    )
    assert control.isVisible() and control.property("enabled") is not False
    position = control.mapToItem(window.contentItem(), QPointF(0, 0))
    assert position.y() >= 4 and position.y() + control.height() + 4 <= window.height() - 36


def snapshot(app: Any, window: Any, name: str) -> None:
    SNAPSHOTS.mkdir(parents=True, exist_ok=True)
    assert states.grab(app, window, states.WIDTH, states.HEIGHT).save(
        str(SNAPSHOTS / f"{name}.png")
    )


def render(
    app: Any, updates: Any, inspect: Callable[[Any], None], *, section: str = "network"
) -> None:
    settings = states.make_settings({"_checkAppUpdates": True})
    try:
        _, _, messages = states.render_network(
            app, False, updates, settings, inspect, section=section
        )
        assert not messages, "\n".join(messages)
    finally:
        sip.delete(updates)
        sip.delete(settings)
        app.processEvents()


def available(**overrides: Any) -> Any:
    return states.FakeUpdates(**{**states.AVAILABLE, **overrides})


@pytest.mark.parametrize("scenario", ["available", "busy", "disabled-and-hidden"])
def test_full_forward_and_reverse_tab_order_skips_unavailable_controls(
    app: Any, scenario: str
) -> None:
    common = ["checkAppUpdatesToggle", "offlineToggle"]
    if scenario == "available":
        updates = available()
        order = [
            "updatePrimary",
            "updateReleasePage",
            "updateSkip",
            "updateRemindLater",
            *common,
            "checkNowButton",
        ]
    elif scenario == "busy":
        updates = available(
            downloadPhase="metadata",
            downloadBusy=True,
            canCancelDownload=True,
            canDownload=False,
            canSkipVersion=False,
            canRemindLater=False,
        )
        order = ["updateCancel", "updateReleasePage", *common]
    else:
        updates = available(canDownload=False, releasePageAvailable=False)
        order = ["updateSkip", "updateRemindLater", *common, "checkNowButton"]

    def inspect(window: Any) -> None:
        settle(app, window)
        item(window, order[0]).forceActiveFocus(Qt.TabFocusReason)
        settle(app, window)
        assert_focus_visible(window, order[0])
        for name in order[1:]:
            QTest.keyClick(window, Qt.Key_Tab)
            settle(app, window)
            assert_focus_visible(window, name)
        snapshot(app, window, f"tab-{scenario}")
        for name in reversed(order[:-1]):
            QTest.keyClick(window, Qt.Key_Backtab, Qt.ShiftModifier)
            settle(app, window)
            assert_focus_visible(window, name)
        assert updates.calls == []

    render(app, updates, inspect)


def test_space_executes_only_the_focused_control(app: Any) -> None:
    updates = available()

    def inspect(window: Any) -> None:
        settle(app, window)
        for name, action in (
            ("updateReleasePage", "openReleasePage"),
            ("updateSkip", "skipVersion"),
            ("updateRemindLater", "remindLater"),
            ("updatePrimary", "download"),
        ):
            item(window, name).forceActiveFocus(Qt.TabFocusReason)
            settle(app, window)
            before = len(updates.calls)
            QTest.keyClick(window, Qt.Key_Space)
            settle(app, window)
            assert updates.calls[before:] == [action]
        assert "installAndRestart" not in updates.calls
        item(window, "checkAppUpdatesToggle").forceActiveFocus(Qt.TabFocusReason)
        settle(app, window)
        before_calls = list(updates.calls)
        checked = item(window, "checkAppUpdatesToggle").property("checked")
        QTest.keyClick(window, Qt.Key_Space)
        settle(app, window)
        assert item(window, "checkAppUpdatesToggle").property("checked") != checked
        assert updates.calls == before_calls

    render(app, updates, inspect)


@pytest.mark.parametrize("location", ["panel", "toggle", "sidebar", "other-section"])
def test_escape_cancels_busy_only_when_focus_is_inside_panel(app: Any, location: str) -> None:
    updates = available(
        downloadPhase="downloading",
        downloadBusy=True,
        canCancelDownload=True,
        canDownload=False,
        canSkipVersion=False,
        canRemindLater=False,
    )

    def inspect(window: Any) -> None:
        settle(app, window)
        if location == "panel":
            item(window, "updateCancel").forceActiveFocus(Qt.TabFocusReason)
        elif location == "toggle":
            item(window, "checkAppUpdatesToggle").forceActiveFocus(Qt.TabFocusReason)
        elif location == "sidebar":
            states.onboarding.settings_sidebar(window).forceActiveFocus(Qt.TabFocusReason)
        else:
            states.onboarding.select_section(app, window, "general")
        settle(app, window)
        QTest.keyClick(window, Qt.Key_Escape)
        settle(app, window)
        assert updates.calls == (["cancelDownload"] if location == "panel" else [])

    render(app, updates, inspect)


@pytest.mark.parametrize("activation", ["mouse", "return", "space"])
def test_status_bar_activation_only_opens_network_panel(app: Any, activation: str) -> None:
    updates = available(
        downloadPhase="readyappimage",
        canDownload=False,
        canOpenFolder=True,
        canInstallAndRestart=True,
    )

    def inspect(window: Any) -> None:
        settle(app, window)
        status = item(window, "statusUpdate")
        if activation == "mouse":
            states.onboarding.click_item(status)
        else:
            status.forceActiveFocus(Qt.TabFocusReason)
            settle(app, window)
            QTest.keyClick(window, Qt.Key_Return if activation == "return" else Qt.Key_Space)
        QTest.qWait(240)
        settle(app, window)
        sidebar = states.onboarding.settings_sidebar(window)
        sections = states.onboarding.section_descriptions(window)
        assert sections[sidebar.property("currentIndex")]["key"] == "network"
        assert_focus_visible(window, "updatePanelHeading")
        assert updates.calls == []
        snapshot(app, window, f"status-{activation}")

    render(app, updates, inspect, section="general")


class Controller:
    def __init__(self) -> None:
        self.starts: list[VerifiedRelease] = []
        self.cancels = 0

    def start(self, value: VerifiedRelease) -> int:
        self.starts.append(value)
        return len(self.starts)

    def cancel(self) -> None:
        self.cancels += 1


def real_bridge(
    *, prior_warning: bool = False
) -> tuple[UpdatesBridge, Controller, Mock, Mock, VerifiedRelease]:
    controller = Controller()
    checker = Mock(spec=["check_now", "refresh", "skip_version", "clear_skip", "remind_later"])
    install = Mock(return_value=True)
    value = VerifiedRelease(
        "1.5.0",
        "v1.5.0",
        "2026-10-09T12:00:00Z",
        "1.8",
        "https://github.com/ArrivaRUS/astra-voice/releases/tag/v1.5.0",
        VerifiedArtifact("appimage", "Astra_Voice-1.5.0-x86_64.AppImage", "a" * 64, 8, "unused"),
        b"worker-only-sums",
        b"worker-only-signature",
        b"worker-only-json",
    )
    bridge = UpdatesBridge(
        checker,
        controller=controller,
        request_install=install,
        install_refusal=lambda: "",
        open_external=Mock(return_value=True),
        refusal=lambda kind: "",
    )
    if prior_warning:
        bridge.restore_install_error("1.0.1", "launch-unconfirmed")
    bridge.set_status(
        UpdateStatus(
            "available",
            version=value.version,
            raw_tag=value.raw_tag,
            notes="- Свежие изменения версии 1.5.0",
            release_url=value.release_url,
            verified_release=value,
        )
    )
    return bridge, controller, checker, install, value


def test_prior_launch_warning_with_fresh_real_candidate_needs_explicit_download(app: Any) -> None:
    bridge, controller, checker, install, value = real_bridge(prior_warning=True)

    def inspect(window: Any) -> None:
        settle(app, window)
        primary = item(window, "updatePrimary")
        assert primary.property("enabled") is True
        assert primary.property("text") == "Скачать новую версию"
        assert item(window, "updateReleaseVersion").property("text") == value.version
        assert item(window, "updateNoteLine").property("text") == "Свежие изменения версии 1.5.0"
        assert controller.starts == []
        install.assert_not_called()
        checker.check_now.assert_not_called()
        primary.forceActiveFocus(Qt.TabFocusReason)
        settle(app, window)
        QTest.keyClick(window, Qt.Key_Space)
        settle(app, window)
        assert controller.starts == [value]
        assert bridge.downloadPhase == "metadata"
        assert_focus_visible(window, "updateCancel")
        install.assert_not_called()
        checker.check_now.assert_not_called()
        snapshot(app, window, "prior-warning-explicit-download")

    render(app, bridge, inspect)


@pytest.mark.parametrize("key", [Qt.Key_Return, Qt.Key_Enter], ids=["return", "enter"])
def test_held_return_across_download_ready_cannot_install(
    app: Any, tmp_path: Path, key: Any
) -> None:
    bridge, controller, _, install, _ = real_bridge()

    def inspect(window: Any) -> None:
        settle(app, window)
        item(window, "updatePrimary").forceActiveFocus(Qt.TabFocusReason)
        settle(app, window)
        QTest.keyPress(window, key)
        settle(app, window)
        if not controller.starts:
            # Qt may activate this control on release; no timing requirement.
            QTest.keyRelease(window, key)
            settle(app, window)
        assert len(controller.starts) == 1
        value = controller.starts[0]
        folder = tmp_path / "verified"
        folder.mkdir(mode=0o700)
        path = folder / value.artifact.name
        path.write_bytes(b"12345678")
        path.chmod(0o755)
        result = VerifiedDownload(path, value, FileIdentity.from_stat(path.stat()))
        bridge.set_download_status(DownloadStatus(1, "ready", result=result))
        settle(app, window)
        assert_focus_visible(window, "updatePanelHeading")
        for _ in range(3):
            event = QKeyEvent(QEvent.KeyPress, key, Qt.NoModifier, "\r", True, 1)
            QGuiApplication.sendEvent(window, event)
            settle(app, window)
        # End the held gesture, or deliver a harmless orphan release if Qt
        # activated on release before verification completed.
        QTest.keyRelease(window, key)
        settle(app, window)
        assert len(controller.starts) == 1
        assert bridge.downloadPhase == "readyappimage"
        install.assert_not_called()
        snapshot(app, window, f"held-enter-{int(key)}-neutral-heading")
        QTest.keyClick(window, Qt.Key_Tab)
        settle(app, window)
        assert_focus_visible(window, "updatePrimary")
        QTest.keyClick(window, Qt.Key_Space)
        settle(app, window)
        install.assert_called_once_with(result)
        assert bridge.downloadPhase == "installing"

    render(app, bridge, inspect)


def test_progress_error_do_not_steal_focus_from_existing_toggle(app: Any) -> None:
    updates = available()

    def inspect(window: Any) -> None:
        settle(app, window)
        item(window, "checkAppUpdatesToggle").forceActiveFocus(Qt.TabFocusReason)
        settle(app, window)
        for phase in ("metadata", "downloading", "verifying", "readyappimage", "error"):
            busy = phase in {"metadata", "downloading", "verifying"}
            updates.update(
                downloadPhase=phase,
                downloadBusy=busy,
                canCancelDownload=busy,
                canDownload=False,
                canInstallAndRestart=phase == "readyappimage",
                canSkipVersion=not busy,
                canRemindLater=not busy,
                downloadError="hash-mismatch" if phase == "error" else "",
            )
            settle(app, window)
            assert_focus_visible(window, "checkAppUpdatesToggle")
        assert updates.calls == []
        snapshot(app, window, "toggle-focus-after-error")

    render(app, updates, inspect)


@pytest.mark.parametrize("key", [Qt.Key_Return, Qt.Key_Enter], ids=["return", "enter"])
def test_enter_activates_only_focused_primary(app: Any, key: Any) -> None:
    updates = available()

    def inspect(window: Any) -> None:
        settle(app, window)
        item(window, "updatePrimary").forceActiveFocus(Qt.TabFocusReason)
        settle(app, window)

        def repeats() -> None:
            for event_type in (QEvent.KeyPress, QEvent.KeyRelease):
                event = QKeyEvent(event_type, key, Qt.NoModifier, "\r", True, 1)
                QGuiApplication.sendEvent(window, event)
            settle(app, window)

        repeats()
        QTest.keyRelease(window, key)  # Orphan release is not a gesture.
        settle(app, window)
        assert updates.calls == []
        QTest.keyClick(window, key)
        settle(app, window)
        assert updates.calls == ["download"]
        repeats()
        QTest.keyRelease(window, key)
        settle(app, window)
        assert updates.calls == ["download"]

    render(app, updates, inspect)


def test_offline_ready_deb_bottom_actions_reachable_by_tab_and_scroll(app: Any) -> None:
    updates = available(
        artifactTrack="deb",
        downloadPhase="readydeb",
        canDownload=False,
        canOpenFolder=True,
        canInstallAndRestart=False,
        releasePageAvailable=False,
        canCheckNow=False,
        networkRefusal="offline",
        checkRefusal="offline",
        folderPath="/tmp/" + "verified-download-folder/" * 10,
        adminInstruction="sudo apt install ./astra-voice_0.2.1_amd64.deb",
        notes=[
            {"text": f"Проверенные сведения о выпуске, пункт {i}", "bullet": True} for i in range(8)
        ],
    )

    def inspect(window: Any) -> None:
        settle(app, window)
        primary = item(window, "updatePrimary")
        position = primary.mapToItem(window.contentItem(), QPointF(0, 0))
        assert position.y() > window.height() - 36, "test must exercise actions below the viewport"
        item(window, "updatePanelHeading").forceActiveFocus(Qt.TabFocusReason)
        settle(app, window)
        for name in ("updatePrimary", "updateSkip", "updateRemindLater"):
            QTest.keyClick(window, Qt.Key_Tab)
            settle(app, window)
            assert_focus_visible(window, name)
        snapshot(app, window, "offline-ready-deb-bottom-actions")
        for name in ("updateSkip", "updatePrimary"):
            QTest.keyClick(window, Qt.Key_Backtab, Qt.ShiftModifier)
            settle(app, window)
            assert_focus_visible(window, name)
        QTest.keyClick(window, Qt.Key_Space)
        settle(app, window)
        assert updates.calls == ["openFolder"]

    render(app, updates, inspect)
