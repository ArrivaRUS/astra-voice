"""Author regressions for local assignment and runtime lease lifecycle."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from PyQt5 import sip
from PyQt5.QtCore import QEvent, QObject, Qt
from PyQt5.QtGui import QKeyEvent
from PyQt5.QtTest import QTest

from astra_voice.ui.command_mouse_capture import CommandMouseCapture, mouse_button_label
from helpers.qt_app import get_qapplication

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def application() -> None:
    get_qapplication()


class MouseHost:
    command_mouse_status = "ready"
    command_mouse_status_message = "Готово"
    command_mouse_can_edit = True
    on_command_mouse_changed: Callable[[], None] | None = None
    on_command_changed: Callable[[], None] | None = None
    on_command_preview: Callable[[str], None] | None = None

    def __init__(self) -> None:
        self.token: object | None = None
        self.ended: list[object] = []
        self.reloads = 0
        self.valid = True
        self.deny = False
        self.notify_on_begin = False

    def begin_command_mouse_capture(self) -> object | None:
        if self.deny:
            return None
        assert self.token is None
        self.token = object()
        if self.notify_on_begin:
            self.notify()
        return self.token

    def command_mouse_capture_valid(self, token: object) -> bool:
        return self.valid and token is self.token

    def end_command_mouse_capture(self, token: object) -> None:
        self.ended.append(token)
        if token is self.token:
            self.token = None

    def reload_command_mouse(self) -> None:
        self.reloads += 1

    def notify(self) -> None:
        assert self.on_command_mouse_changed is not None
        self.on_command_mouse_changed()


def make_capture(**kwargs: object) -> tuple[CommandMouseCapture, MouseHost]:
    capture = CommandMouseCapture(**kwargs)  # type: ignore[arg-type]
    host = MouseHost()
    capture.bind_host(host)
    return capture, host


@pytest.mark.parametrize("qt_button,logical", [(4, 2), (8, 8), (16, 9), (1 << 26, 31)])
def test_gesture_requires_matching_complete_release(qt_button: int, logical: int) -> None:
    capture, host = make_capture()
    assert capture.begin()
    capture.press(qt_button)
    assert capture.state == "pressed"
    assert capture.pending_button == logical
    assert not capture.ready()
    capture.release(qt_button, 0)
    assert capture.ready()
    assert host.token is not None
    capture.cancel()
    assert len(host.ended) == 1


@pytest.mark.parametrize("button", [0, 1, 2, 3, 5, 6, 12, -1, True, 1 << 27])
def test_invalid_qt_button_never_stages(button: int) -> None:
    capture, _ = make_capture()
    assert capture.begin()
    capture.press(button)
    capture.release(button, 0)
    assert not capture.ready()
    assert capture.pending_button == 0
    capture.cancel()


@pytest.mark.parametrize("release,remaining", [(8, 0), (4, 1), (4, 4), (4, -1), (4, True)])
def test_mismatched_or_incomplete_release_invalidates(release: int, remaining: int) -> None:
    capture, _ = make_capture()
    assert capture.begin()
    capture.press(4)
    capture.release(release, remaining)
    assert not capture.ready()
    assert capture.pending_button == 0
    capture.cancel()


def test_chord_requires_release_then_fresh_gesture() -> None:
    capture, _ = make_capture()
    assert capture.begin()
    capture.press(4)
    capture.press(8)
    capture.release(4, 8)
    capture.release(8, 0)
    assert not capture.ready()
    capture.press(8)
    capture.release(8, 0)
    assert capture.ready() and capture.pending_button == 8
    capture.cancel()


def test_absent_partial_denied_and_boolean_ports_fail_closed() -> None:
    capture = CommandMouseCapture()
    assert not capture.begin()
    capture.bind_host(object())  # type: ignore[arg-type]
    assert not capture.can_edit
    capture, host = make_capture()
    host.deny = True
    assert not capture.begin()
    host.begin_command_mouse_capture = lambda: True  # type: ignore[method-assign]
    assert not capture.begin()
    assert capture.state != "ready"


def test_reentrant_notification_from_begin_accepts_valid_lease() -> None:
    capture, host = make_capture()
    host.notify_on_begin = True
    assert capture.select(8)
    assert capture.ready()
    capture.cancel()


@pytest.mark.parametrize("action", ["press", "release", "ready", "notify"])
def test_revoked_lease_cancels_before_action(action: str) -> None:
    capture, host = make_capture()
    assert capture.begin()
    capture.press(4)
    host.valid = False
    if action == "press":
        capture.press(8)
    elif action == "release":
        capture.release(4, 0)
    elif action == "ready":
        assert not capture.ready()
    else:
        host.notify()
    assert capture.state == "idle" and capture.pending_button == 0
    assert len(host.ended) == 1


def test_replacement_and_stale_callback_do_not_cancel_new_host() -> None:
    capture, first = make_capture()
    assert capture.select(8)
    callback = first.on_command_mouse_changed
    second = MouseHost()
    capture.bind_host(second)
    assert len(first.ended) == 1
    assert capture.select(9)
    assert callback is not None
    callback()
    assert capture.ready() and capture.pending_button == 9
    capture.cancel()
    capture.cancel()
    assert len(second.ended) == 1


def test_timeout_and_stale_timeout() -> None:
    now = [0.0]
    capture, host = make_capture(clock=lambda: now[0])
    assert capture.select(8)
    now[0] = 29.0
    capture._expire()
    assert capture.ready()
    capture.cancel()
    assert capture.select(9)
    now[0] = 30.0
    capture._expire()
    assert capture.ready()
    now[0] = 59.0
    capture._expire()
    assert capture.state == "idle" and len(host.ended) == 2


@pytest.mark.parametrize("kind", [QEvent.Hide, QEvent.Close, QEvent.WindowDeactivate])
def test_window_events_cancel(kind: int) -> None:
    capture, host = make_capture()
    window = QObject()
    capture.attach_window(window)
    assert capture.select(8)
    capture.eventFilter(window, QEvent(kind))
    assert capture.state == "idle" and len(host.ended) == 1


def test_escape_window_destruction_and_parent_destruction() -> None:
    parent = QObject()
    capture, host = make_capture(parent=parent)
    window = QObject()
    capture.attach_window(window)
    assert capture.select(8)
    capture.eventFilter(window, QKeyEvent(QEvent.KeyPress, Qt.Key_Escape, Qt.NoModifier))
    assert capture.state == "idle"
    assert capture.select(8)
    sip.delete(window)
    assert capture.state == "idle"
    assert capture.select(8)
    callback = host.on_command_mouse_changed
    sip.delete(parent)
    assert len(host.ended) == 3 and host.token is None
    assert callback is not None
    callback()  # no signal emitted on a deleted QObject


def test_button_names_are_logical_and_neutral() -> None:
    assert mouse_button_label(2) == "Средняя кнопка"
    assert mouse_button_label(8) == "Дополнительная кнопка 1"
    assert mouse_button_label(9) == "Дополнительная кнопка 2"
    assert mouse_button_label(31) == "Кнопка мыши 31"
    assert mouse_button_label(0) == ""


def test_qt_timer_actually_releases_lease() -> None:
    capture, host = make_capture()
    capture.TIMEOUT_MS = 1
    assert capture.select(8)
    QTest.qWait(15)
    assert capture.state == "idle" and len(host.ended) == 1


def test_minimize_releases_lease() -> None:
    class Window(QObject):
        def windowState(self) -> int:  # noqa: N802
            return int(Qt.WindowMinimized)

    capture, host = make_capture()
    window = Window()
    capture.attach_window(window)
    assert capture.select(8)
    capture.eventFilter(window, QEvent(QEvent.WindowStateChange))
    assert capture.state == "idle" and len(host.ended) == 1


def test_cancel_during_acquisition_releases_returned_stale_token() -> None:
    capture, host = make_capture()
    original = host.begin_command_mouse_capture

    def begin() -> object | None:
        token = original()
        capture.cancel()
        return token

    host.begin_command_mouse_capture = begin  # type: ignore[method-assign]
    assert not capture.begin()
    assert capture.state == "idle" and host.token is None and len(host.ended) == 1


def test_action_after_deadline_cancels_even_before_timer_delivery() -> None:
    now = [1.0]
    capture, host = make_capture(clock=lambda: now[0])
    assert capture.select(8)
    now[0] = 31.0
    assert not capture.ready()
    assert capture.state == "idle" and len(host.ended) == 1


def test_reentrant_open_from_changed_does_not_stage_old_candidate() -> None:
    capture, host = make_capture()
    replaced = [False]

    def replace_capture() -> None:
        if capture.state == "capturing" and not replaced[0]:
            replaced[0] = True
            capture.cancel()
            assert capture.begin()

    capture.changed.connect(replace_capture)
    assert not capture.select(8)
    assert capture.state == "capturing" and capture.pending_button == 0
    assert len(host.ended) == 1
    capture.cancel()
