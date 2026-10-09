"""Real QML, SettingsBridge, capture controller and runtime; external hardware is fake."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import Mock

import pytest
from PyQt5.QtCore import QMetaObject, QPointF, Qt
from PyQt5.QtTest import QTest
from test_command_mouse_qml import mouse_window
from test_command_settings_layout import tree
from test_onboarding import ensure_control_in_viewport

from command_acceptance.test_mouse_input_ownership import Rig
from helpers.qt_app import get_qapplication

pytestmark = pytest.mark.xvfb


@pytest.fixture
def capture(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[Any, ...]]:
    r = Rig(monkeypatch)
    monkeypatch.setattr("astra_voice.runtime.is_installed", lambda: True)
    r.runtime._command_client = Mock()  # status/discovery must not connect to a bus
    try:
        with mouse_window(900, runtime=r.runtime) as result:
            yield (r, *result)
    finally:
        r.close()


def control(window: Any, name: str) -> Any:
    return next(item for item in tree(window.contentItem()) if item.objectName() == name)


def test_local_qt_capture_stages_until_apply_without_recording(capture: Any) -> None:
    r, window, bridge, _settings, saved, messages = capture
    body = next(
        item
        for item in tree(window.contentItem())
        if item.metaObject().indexOfProperty("contentY") >= 0
    )
    choose = control(window, "commandMouseChoose")
    assert choose.isEnabled()
    ensure_control_in_viewport(get_qapplication(), body, choose)
    position = choose.mapToItem(
        window.contentItem(), QPointF(choose.width() / 2, choose.height() / 2)
    ).toPoint()
    QTest.mousePress(window, Qt.LeftButton, pos=position)
    assert bridge.commandMouseCaptureState == "idle"
    QTest.mouseRelease(window, Qt.LeftButton, pos=position)
    QTest.qWait(30)
    assert bridge.commandMouseCaptureState == "capturing"
    field = control(window, "commandMouseCaptureField")
    ensure_control_in_viewport(get_qapplication(), body, field)
    position = field.mapToItem(window.contentItem(), QPointF(20, 20)).toPoint()
    QTest.mouseClick(window, Qt.RightButton, pos=position)
    assert bridge.commandMousePendingButton == 0
    assert not bridge.applyCommandMouseButton()
    QTest.mousePress(window, Qt.MiddleButton, pos=position)
    assert bridge.commandMouseCaptureState == "pressed" and bridge.commandMousePendingButton == 2
    r.key("text", "KeyPress")
    r.key("command", "KeyPress")
    saved.assert_not_called()
    assert r.commands() == []
    QTest.mouseRelease(window, Qt.MiddleButton, pos=position)
    QTest.qWait(20)
    assert bridge.commandMouseCaptureState == "ready"
    apply = control(window, "commandMouseApply")
    assert apply.isVisible() and apply.isEnabled()
    QMetaObject.invokeMethod(apply, "clicked", Qt.DirectConnection)
    assert bridge.commandMouseCaptureState == "idle" and not bridge.commandMouseEnabled
    assert saved.call_count == 1 and r.commands() == [] and r.client.calls == []
    assert not messages, messages


@pytest.mark.parametrize("leave", ["escape", "navigation", "hide"])
def test_qml_cancel_revokes_runtime_lease_and_stale_candidate(capture: Any, leave: str) -> None:
    r, window, bridge, _settings, saved, messages = capture
    QMetaObject.invokeMethod(control(window, "commandMouseChoose"), "clicked", Qt.DirectConnection)
    QTest.qWait(30)
    assert bridge.commandMouseCaptureState == "capturing"
    token = bridge._mouse_capture._lease.token
    if leave == "escape":
        QTest.keyClick(window, Qt.Key_Escape)
    elif leave == "hide":
        window.hide()
    else:
        sidebar = next(
            item
            for item in tree(window.contentItem())
            if item.metaObject().indexOfProperty("sections") >= 0
        )
        sidebar.setProperty("currentIndex", 0)
    QTest.qWait(20)
    bridge.commandMouseCaptureReleased(4, 0)
    assert bridge.commandMouseCaptureState == "idle"
    assert not r.runtime.command_mouse_capture_valid(token)
    assert not bridge.applyCommandMouseButton()
    saved.assert_not_called()
    assert r.commands() == [] and r.client.calls == []
    assert not messages, messages
