"""Independent keyboard contracts exercised on actual Qt/QML under private Xvfb.

Backend actions are recorded, never executed. Prior-warning and held-key cases
use the real UpdatesBridge with a fake controller and actual private ready file.
"""

from __future__ import annotations

import importlib
import os
from collections.abc import Callable
from functools import cache
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
from helpers.snapshots import snapshot_dir, write_snapshot

states: Any = importlib.import_module("test_update_states")
pytestmark = pytest.mark.xvfb


@cache
def snapshots() -> Path:
    return snapshot_dir(
        "ASTRA_VOICE_SNAPSHOT_DIR_PROGRAM_UPDATES",
        "program-updates-keyboard-independent",
        "design/refs/impl/program-updates-keyboard-independent",
    )


@pytest.fixture(scope="module")
def app() -> Any:
    assert os.environ.get("QT_QPA_PLATFORM") == "xcb", "keyboard acceptance requires real Xvfb/xcb"
    app = states.get_qapplication()
    states.onboarding.register_ui_fonts()
    return app


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
    write_snapshot(
        snapshots(), f"{name}.png", states.grab(app, window, states.WIDTH, states.HEIGHT)
    )


def assert_heading_ring_follows_visible_content(window: Any, *, compact: bool = True) -> None:
    content = item(window, "updateHeadingContent")
    ring = item(window, "updateHeadingFocusRing")
    # Measure actual visible children independently; Qt childrenRect includes
    # hidden badge/version items, which must not enlarge a keyboard ring.
    children = [child for child in content.childItems() if child.isVisible()]
    assert children
    points = [child.mapToItem(ring.parentItem(), QPointF(0, 0)) for child in children]
    left = min(point.x() for point in points)
    top = min(point.y() for point in points)
    right = max(point.x() + child.width() for point, child in zip(points, children, strict=True))
    bottom = max(point.y() + child.height() for point, child in zip(points, children, strict=True))
    assert ring.x() == pytest.approx(left - 4)
    assert ring.y() == pytest.approx(top - 4)
    assert ring.width() == pytest.approx(right - left + 8)
    assert ring.height() == pytest.approx(bottom - top + 8)
    if compact:
        assert ring.width() < item(window, "updatePanel").width() - 100


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
    common = ["checkAppUpdatesToggle", "checkModelUpdatesToggle", "offlineToggle"]
    if scenario == "available":
        updates = available()
        order = [
            "updatePrimary",
            "updateSkip",
            *common,
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
        order = ["updateCancel", *common]
    else:
        updates = available(canDownload=False, releasePageAvailable=False)
        order = ["updateSkip", *common]

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
            ("updateSkip", "skipVersion"),
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


@pytest.mark.parametrize("phase", ["idle", "readyappimage"])
@pytest.mark.parametrize("activation", ["mouse", "return", "space"])
def test_status_bar_activation_only_opens_network_panel(
    app: Any, activation: str, phase: str
) -> None:
    updates = available(
        downloadPhase=phase,
        canDownload=phase == "idle",
        canOpenFolder=phase == "readyappimage",
        canInstallAndRestart=phase == "readyappimage",
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
        assert item(window, "updateHeadingFocusRing").isVisible() is (activation != "mouse")
        assert_heading_ring_follows_visible_content(window)
        assert updates.calls == []
        snapshot(app, window, f"status-{phase}-{activation}")

    render(app, updates, inspect, section="general")


def test_wrapped_heading_keyboard_ring_covers_all_visible_rows(app: Any) -> None:
    updates = available(version="0.2.1-" + "abcdef" * 30)
    original_size = states.WIDTH, states.HEIGHT
    states.WIDTH, states.HEIGHT = 900, 588

    def inspect(window: Any) -> None:
        settle(app, window)
        item(window, "statusUpdate").forceActiveFocus(Qt.TabFocusReason)
        settle(app, window)
        QTest.keyClick(window, Qt.Key_Return)
        QTest.qWait(240)
        settle(app, window)
        assert_focus_visible(window, "updatePanelHeading")
        version = item(window, "updateReleaseVersion")
        title = item(window, "updatePanelTitle")
        badge = item(window, "updateNewBadge")
        assert version.height() > title.height(), "fixture must wrap the long version token"
        assert version.y() >= title.height() + 9
        assert badge.y() >= version.y() + version.height() + 9
        assert item(window, "updateHeadingFocusRing").isVisible() is True
        assert_heading_ring_follows_visible_content(window, compact=False)
        assert updates.calls == []
        snapshot(app, window, "wrapped-heading-keyboard-900x588")

    try:
        render(app, updates, inspect, section="general")
    finally:
        states.WIDTH, states.HEIGHT = original_size


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


@pytest.mark.parametrize("activation", ["mouse", "mouse-after-keyboard", "return", "space"])
def test_download_focus_preserves_mouse_or_keyboard_origin(
    app: Any, tmp_path: Path, activation: str
) -> None:
    bridge, controller, checker, install, value = real_bridge()

    def inspect(window: Any) -> None:
        settle(app, window)
        primary = item(window, "updatePrimary")
        if activation.startswith("mouse"):
            if activation == "mouse-after-keyboard":
                primary.forceActiveFocus(Qt.TabFocusReason)
                settle(app, window)
                assert primary.property("visualFocus") is True
            states.onboarding.click_item(primary)
        else:
            # A key gesture must establish keyboard modality even after mouse
            # focus; assigning TabFocusReason here would hide this regression.
            primary.forceActiveFocus(Qt.OtherFocusReason)
            settle(app, window)
            assert primary.property("visualFocus") is False
            QTest.keyClick(window, Qt.Key_Return if activation == "return" else Qt.Key_Space)
        settle(app, window)
        assert controller.starts == [value]
        assert bridge.downloadPhase == "metadata"
        assert_focus_visible(window, "updateCancel")
        keyboard = activation in {"return", "space"}
        assert item(window, "updateCancel").property("visualFocus") is keyboard
        assert item(window, "updateHeadingFocusRing").isVisible() is False
        snapshot(app, window, f"download-{activation}-cancel-focus")

        folder = tmp_path / "verified"
        folder.mkdir(mode=0o700)
        path = folder / value.artifact.name
        path.write_bytes(b"12345678")
        path.chmod(0o755)
        result = VerifiedDownload(path, value, FileIdentity.from_stat(path.stat()))
        bridge.set_download_status(DownloadStatus(1, "ready", result=result))
        settle(app, window)
        assert_focus_visible(window, "updatePanelHeading")
        assert item(window, "updateHeadingFocusRing").isVisible() is keyboard
        install.assert_not_called()
        checker.remind_later.assert_not_called()
        snapshot(app, window, f"download-{activation}-heading-focus")

    render(app, bridge, inspect)


@pytest.mark.parametrize("initial_activation", ["mouse", "keyboard"])
def test_focus_origin_tracks_user_input_during_download(
    app: Any, tmp_path: Path, initial_activation: str
) -> None:
    bridge, controller, checker, install, value = real_bridge()

    def inspect(window: Any) -> None:
        settle(app, window)
        primary = item(window, "updatePrimary")
        if initial_activation == "mouse":
            states.onboarding.click_item(primary)
        else:
            primary.forceActiveFocus(Qt.TabFocusReason)
            settle(app, window)
            QTest.keyClick(window, Qt.Key_Return)
        settle(app, window)
        assert controller.starts == [value]
        assert_focus_visible(window, "updateCancel")
        if initial_activation == "mouse":
            assert item(window, "updateCancel").property("visualFocus") is False
            QTest.keyClick(window, Qt.Key_Tab)
            settle(app, window)
            assert_focus_visible(window, "checkAppUpdatesToggle")
            QTest.keyClick(window, Qt.Key_Backtab, Qt.ShiftModifier)
            settle(app, window)
            assert_focus_visible(window, "updateCancel")
            assert item(window, "updateCancel").property("visualFocus") is True
            folder = tmp_path / "verified"
            folder.mkdir(mode=0o700)
            path = folder / value.artifact.name
            path.write_bytes(b"12345678")
            path.chmod(0o755)
            result = VerifiedDownload(path, value, FileIdentity.from_stat(path.stat()))
            bridge.set_download_status(DownloadStatus(1, "ready", result=result))
            settle(app, window)
            assert_focus_visible(window, "updatePanelHeading")
            assert item(window, "updateHeadingFocusRing").isVisible() is True
            assert_heading_ring_follows_visible_content(window)
        else:
            assert item(window, "updateCancel").property("visualFocus") is True
            states.onboarding.click_item(item(window, "updateCancel"))
            settle(app, window)
            assert controller.cancels == 1
            assert item(window, "updateCancel").property("visualFocus") is False
            bridge.set_download_status(DownloadStatus(1, "cancelled"))
            settle(app, window)
            assert_focus_visible(window, "updatePrimary")
            assert item(window, "updatePrimary").property("visualFocus") is False
            assert item(window, "updateHeadingFocusRing").isVisible() is False
        install.assert_not_called()
        checker.remind_later.assert_not_called()

    render(app, bridge, inspect)


@pytest.mark.parametrize(
    "key", [Qt.Key_Return, Qt.Key_Space, Qt.Key_Escape], ids=["enter", "space", "escape"]
)
def test_keyboard_cancel_returns_focus_with_ring_to_primary(app: Any, key: int) -> None:
    bridge, controller, checker, install, value = real_bridge()

    def inspect(window: Any) -> None:
        settle(app, window)
        states.onboarding.click_item(item(window, "updatePrimary"))
        settle(app, window)
        assert controller.starts == [value]
        assert_focus_visible(window, "updateCancel")
        # Cancellation itself must establish keyboard origin after mouse input.
        assert item(window, "updateCancel").property("visualFocus") is False
        QTest.keyClick(window, key)
        settle(app, window)
        assert controller.cancels == 1
        assert bridge.downloadCancelling is True
        assert bridge.canCancelDownload is False
        assert bridge.downloadPhase == "metadata"
        bridge.set_download_status(DownloadStatus(1, "cancelled"))
        settle(app, window)
        assert bridge.downloadPhase == "cancelled"
        assert_focus_visible(window, "updatePrimary")
        assert item(window, "updatePrimary").property("visualFocus") is True
        assert item(window, "updateHeadingFocusRing").isVisible() is False
        install.assert_not_called()
        checker.remind_later.assert_not_called()

    render(app, bridge, inspect)


@pytest.mark.parametrize(
    "target", ["updatePrimary", "updateCancel"], ids=["same-button", "other-button"]
)
def test_mouse_after_space_does_not_keep_keyboard_focus_ring(app: Any, target: str) -> None:
    bridge, controller, checker, install, value = real_bridge()

    def inspect(window: Any) -> None:
        settle(app, window)
        primary = item(window, "updatePrimary")
        primary.forceActiveFocus(Qt.OtherFocusReason)
        settle(app, window)
        QTest.keyClick(window, Qt.Key_Space)
        settle(app, window)
        assert controller.starts == [value]
        assert_focus_visible(window, "updateCancel")
        assert item(window, "updateCancel").property("visualFocus") is True
        assert primary.property("activationKeyboard") is False
        if target == "updatePrimary":
            # A later operation on the same button must start with mouse origin.
            bridge.set_download_status(DownloadStatus(1, "cancelled"))
            settle(app, window)
            assert_focus_visible(window, "updatePrimary")
            assert primary.property("visualFocus") is True
        states.onboarding.click_item(item(window, target))
        settle(app, window)
        if target == "updatePrimary":
            assert controller.starts == [value, value]
            assert bridge.downloadPhase == "metadata"
            assert_focus_visible(window, "updateCancel")
            assert item(window, "updateCancel").property("visualFocus") is False
        else:
            assert controller.cancels == 1
            bridge.set_download_status(DownloadStatus(1, "cancelled"))
            settle(app, window)
            assert_focus_visible(window, "updatePrimary")
            assert primary.property("visualFocus") is False
        assert item(window, "updateHeadingFocusRing").isVisible() is False
        install.assert_not_called()
        checker.remind_later.assert_not_called()

    render(app, bridge, inspect)


@pytest.mark.parametrize("departure", ["section", "hide"])
def test_available_departure_keeps_highlight_without_implicit_reminder(
    app: Any, departure: str
) -> None:
    bridge, controller, checker, install, _ = real_bridge()

    def inspect(window: Any) -> None:
        settle(app, window)
        assert bridge.state == "available" and bridge.downloadPhase == "idle"
        assert bridge.snoozed is False
        if departure == "section":
            states.onboarding.select_section(app, window, "general")
        else:
            window.hide()
        QTest.qWait(240)
        app.processEvents()
        checker.remind_later.assert_not_called()
        assert bridge.snoozed is False
        assert controller.starts == []
        install.assert_not_called()
        if departure == "section":
            states.onboarding.select_section(app, window, "network")
        else:
            window.show()
        settle(app, window)
        assert item(window, "updatePrimary").property("enabled") is True
        assert item(window, "statusUpdateText").property("text") == (
            "Доступна версия 1.5.0 · Подробнее"
        )
        assert item(window, "updatePanel").isVisible()
        checker.remind_later.assert_not_called()
        assert bridge.snoozed is False

    render(app, bridge, inspect)


@pytest.mark.parametrize("destination", ["checkAppUpdatesToggle", "updateReleaseVersion"])
@pytest.mark.parametrize("selection_timing", ["before-terminal", "after-terminal"])
def test_pending_cancellation_does_not_steal_newly_selected_focus(
    app: Any, destination: str, selection_timing: str
) -> None:
    bridge, controller, checker, install, value = real_bridge()

    def inspect(window: Any) -> None:
        settle(app, window)
        states.onboarding.click_item(item(window, "updatePrimary"))
        settle(app, window)
        assert controller.starts == [value]
        assert_focus_visible(window, "updateCancel")
        states.onboarding.click_item(item(window, "updateCancel"))
        settle(app, window)
        assert controller.cancels == 1
        assert bridge.downloadPhase == "metadata"
        assert bridge.downloadCancelling is True and bridge.canCancelDownload is False
        if selection_timing == "after-terminal":
            bridge.set_download_status(DownloadStatus(1, "cancelled"))
            # Select a new target synchronously, before the QML focus callback
            # scheduled by the terminal phase has had a chance to run.
            item(window, destination).forceActiveFocus(Qt.TabFocusReason)
        else:
            if destination == "checkAppUpdatesToggle":
                item(window, destination).forceActiveFocus(Qt.TabFocusReason)
            else:
                states.onboarding.click_item(item(window, destination))
            settle(app, window)
            assert_focus_visible(window, destination)
            bridge.set_download_status(DownloadStatus(1, "cancelled"))
        settle(app, window)
        assert bridge.downloadPhase == "cancelled"
        assert_focus_visible(window, destination)
        assert item(window, "updatePrimary").hasActiveFocus() is False
        install.assert_not_called()
        checker.remind_later.assert_not_called()

    render(app, bridge, inspect)


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
            {"text": f"Проверенные сведения о выпуске, пункт {i}", "bullet": True}
            for i in range(12)
        ],
    )

    def inspect(window: Any) -> None:
        settle(app, window)
        primary = item(window, "updatePrimary")
        position = primary.mapToItem(window.contentItem(), QPointF(0, 0))
        assert position.y() > window.height() - 36, "test must exercise actions below the viewport"
        item(window, "updatePanelHeading").forceActiveFocus(Qt.TabFocusReason)
        settle(app, window)
        for name in ("updatePrimary", "updateSkip"):
            QTest.keyClick(window, Qt.Key_Tab)
            settle(app, window)
            assert_focus_visible(window, name)
        snapshot(app, window, "offline-ready-deb-bottom-actions")
        for name in ("updatePrimary",):
            QTest.keyClick(window, Qt.Key_Backtab, Qt.ShiftModifier)
            settle(app, window)
            assert_focus_visible(window, name)
        QTest.keyClick(window, Qt.Key_Space)
        settle(app, window)
        assert updates.calls == ["openFolder"]

    render(app, updates, inspect)


@pytest.mark.parametrize("scenario", ["idle", "checking", "uptodate", "skipped"])
def test_compact_forward_reverse_tab_order_includes_model_preference(
    app: Any, scenario: str
) -> None:
    updates = states.FakeUpdates(state=scenario, version="0.2.1", manual=True)
    toggles = ["checkAppUpdatesToggle", "checkModelUpdatesToggle", "offlineToggle"]
    order = ["checkNowButton"] if scenario != "checking" else []
    if scenario == "skipped":
        order.append("updateShowSkipped")
    order += toggles

    def inspect(window: Any) -> None:
        settle(app, window)
        item(window, "updatePanelHeading").forceActiveFocus(Qt.TabFocusReason)
        QTest.keyClick(window, Qt.Key_Tab)
        settle(app, window)
        assert_focus_visible(window, order[0])
        for name in order[1:]:
            QTest.keyClick(window, Qt.Key_Tab)
            settle(app, window)
            assert_focus_visible(window, name)
        for name in reversed(order[:-1]):
            QTest.keyClick(window, Qt.Key_Backtab, Qt.ShiftModifier)
            settle(app, window)
            assert_focus_visible(window, name)
        assert updates.calls == []

    render(app, updates, inspect)


@pytest.mark.parametrize(
    "settings_values,expected",
    [
        ({}, "checkAppUpdatesToggle"),
        ({"_offline": True}, "offlineToggle"),
        ({"_lockedSettings": ["check_app_updates"]}, "checkModelUpdatesToggle"),
    ],
    ids=["online", "offline", "first-toggle-locked"],
)
def test_checking_heading_tab_skips_disabled_toggles_and_shows_focus_ring(
    app: Any, settings_values: dict[str, Any], expected: str
) -> None:
    updates = states.FakeUpdates(state="checking", manual=True)
    settings = states.make_settings(settings_values)

    def inspect(window: Any) -> None:
        settle(app, window)
        assert item(window, "checkNowButton").property("enabled") is False
        if expected != "checkAppUpdatesToggle":
            assert item(window, "checkAppUpdatesToggle").property("enabled") is False
        if expected == "offlineToggle":
            assert item(window, "checkModelUpdatesToggle").property("enabled") is False
        item(window, "updatePanelHeading").forceActiveFocus(Qt.TabFocusReason)
        QTest.keyClick(window, Qt.Key_Tab)
        settle(app, window)
        assert_focus_visible(window, expected)
        assert item(window, expected).property("focusReason") == Qt.TabFocusReason
        assert item(window, expected).property("visualFocus") is True
        assert updates.calls == []

    try:
        _, _, messages = states.render_network(app, False, updates, settings, inspect)
        assert not messages, "\n".join(messages)
    finally:
        sip.delete(updates)
        sip.delete(settings)
        app.processEvents()


@pytest.mark.parametrize("activation", ["mouse", "return", "enter", "space"])
@pytest.mark.parametrize("result", ["uptodate", "available", "unavailable", "error-net"])
def test_manual_check_keeps_heading_focus_and_ring_through_result(
    app: Any, activation: str, result: str
) -> None:
    class CheckingUpdates(states.FakeUpdates):
        @states.pyqtSlot()
        def checkNow(self) -> None:
            self.calls.append("checkNow")
            self.update(state="checking", manual=True)

    updates = CheckingUpdates(state="idle")

    def inspect(window: Any) -> None:
        settle(app, window)
        button = item(window, "checkNowButton")
        if activation == "mouse":
            states.onboarding.click_item(button)
        else:
            button.forceActiveFocus(Qt.TabFocusReason)
            settle(app, window)
            key = {"return": Qt.Key_Return, "enter": Qt.Key_Enter, "space": Qt.Key_Space}[
                activation
            ]
            QTest.keyClick(window, key)
        settle(app, window)
        assert updates.calls == ["checkNow"]
        assert_focus_visible(window, "updatePanelHeading")
        assert item(window, "updateHeadingFocusRing").isVisible() is (activation != "mouse")
        assert_heading_ring_follows_visible_content(window)
        assert button.property("enabled") is False
        updates.update(
            state=result,
            version="0.2.1",
            notes=states.NOTES,
            checkedText="Проверено сегодня в 14:02",
        )
        settle(app, window)
        assert_focus_visible(window, "updatePanelHeading")
        assert item(window, "updateHeadingFocusRing").isVisible() is (activation != "mouse")
        assert_heading_ring_follows_visible_content(window)
        QTest.keyClick(window, Qt.Key_Return)
        settle(app, window)
        assert updates.calls == ["checkNow"], "result heading must not trigger another action"
        if result == "uptodate":
            QTest.qWait(3200)
            settle(app, window)
            assert "Установлена последняя версия" in states.onboarding.visible_texts(
                item(window, "updatePanel")
            )
            assert_focus_visible(window, "updatePanelHeading")
        QTest.keyClick(window, Qt.Key_Tab)
        settle(app, window)
        assert_focus_visible(window, "updatePrimary" if result == "available" else "checkNowButton")

    render(app, updates, inspect)
