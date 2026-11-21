"""X1–X4: real xcb window, XTest input and isolated X11 focus."""

from __future__ import annotations

import os
import threading
from collections import deque
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest
from PyQt5 import sip
from PyQt5.QtCore import QCoreApplication, QEvent, QObject, QPoint, QPointF, QRect, Qt, pyqtSignal
from PyQt5.QtGui import QCursor, QExposeEvent, QGuiApplication, QMouseEvent, QRegion
from PyQt5.QtQuick import QQuickItem

from astra_voice.core.command_mode import SessionSnapshot
from astra_voice.core.meeting_session import StorageStatus
from astra_voice.meetings.store import MeetingStore
from astra_voice.platform.session import SessionKind
from astra_voice.platform.x11 import TIMESTAMP_ATOM, X11Display
from astra_voice.ui.meeting_offer_window import MeetingOffer
from astra_voice.ui.meeting_recording import AudioHints, MeetingRecording
from astra_voice.ui.meeting_texts import W1_CALL_TEXTS
from helpers import paste_target, wait_until
from helpers import xapp as xapp
from helpers import xdisplay as xdisplay
from helpers.qt_app import get_qapplication

pytestmark = pytest.mark.xvfb
pytest.importorskip("Xlib")


class Session(QObject):
    changed = pyqtSignal(object)

    def snapshot(self) -> SessionSnapshot:
        return SessionSnapshot(known=True, locked=False)

    def start(self) -> None:
        pass

    def close(self) -> None:
        pass


@pytest.fixture
def isolated_x11() -> None:
    assert os.environ.get("ASTRA_VOICE_TEST_X11_ISOLATED") == "1"
    display = os.environ.get("DISPLAY", "")
    assert display.startswith(":") and display.split(".")[0] != ":0"


@pytest.fixture
def offer(
    isolated_x11: None,
    xapp: Any,
    tmp_path: Path,
) -> Iterator[tuple[MeetingOffer, MeetingRecording, list[float], Mock]]:
    now = [100.0]
    monitor = Session()
    role = Mock(ready=True, exited=False, generation=1)
    recording = MeetingRecording(
        MeetingStore(tmp_path / "meetings"),
        enabled=lambda: True,
        policy_allowed=lambda: True,
        detector_allowed=lambda: True,
        session_kind=SessionKind.KDE,
        session_factory=lambda: monitor,
        owns_session=False,
        supervisor_factory=lambda *_args: role,
        clock=lambda: now[0],
        storage_probe=lambda _path: StorageStatus(1000, True, False),
        audio_probe=AudioHints,
        microphone=lambda: "test-mic",
        submit=lambda work: work(),
    )
    window = MeetingOffer(recording)
    recording.bind_offer(window)
    try:
        recording.tick()
        request = role.send.call_args.args[0]
        assert request["type"] == "meeting.detector.start"
        recording._event(
            {
                "type": "meeting.detector.started",
                "request_type": request["type"],
                "req": request["req"],
                "apps_version": request["apps_version"],
                "generation": role.generation,
            }
        )
        assert recording._offer_eligible()
        recording._control_timer.stop()
        recording._lease_timer.stop()
        yield window, recording, now, role
    finally:
        window.shutdown()
        recording.shutdown()
        sip.delete(window)
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        xapp.processEvents()


def _show(window: MeetingOffer, recording: MeetingRecording, call_id: str = "call-w1") -> None:
    recording._event(
        {
            "type": "meeting.call.started",
            "call_id": call_id,
            "app_class": "dion",
            "variant1": True,
            "measured_mono_ms": recording.offer.now_ms(),
            "generation": 1,
        }
    )
    recording.tick()
    assert recording.offer.state == "shown_guard"
    assert window._view.source().fileName() == "MeetingOffer.qml"
    assert window._root is not None and window._root.property("visible")
    title = window._root.findChild(QQuickItem, "offerTitle")
    assert title is not None and title.property("text") == W1_CALL_TEXTS["call-app"]["title"]
    wait_until(window._view.isExposed, timeout=2)


def _click_start(x: X11Display, window: MeetingOffer) -> None:
    from Xlib import X
    from Xlib.ext import xtest

    view = window._view
    button = window._root.findChild(QQuickItem, "offerStart")
    assert button is not None and button.isVisible()
    assert view.devicePixelRatio() == 1, "XTest coordinates use the isolated Xvfb pixels"
    position = view.mapToGlobal(
        button.mapToScene(QPointF(button.width() / 2, button.height() / 2)).toPoint()
    )
    xtest.fake_input(x.d, X.MotionNotify, x=position.x(), y=position.y())
    xtest.fake_input(x.d, X.ButtonPress, 1)
    xtest.fake_input(x.d, X.ButtonRelease, 1)
    x.d.sync()


def _focus(x: X11Display) -> int:
    focus = x.d.get_input_focus().focus
    return focus if isinstance(focus, int) else int(focus.id)


def _assert_wm_properties(x: X11Display, window: MeetingOffer) -> None:
    from Xlib import Xatom

    connection = x.d
    native = connection.create_resource_object("window", int(window._view.winId()))
    names = connection.intern_atom

    def values(name: str, kind: int, resource: Any = native) -> list[int]:
        prop = resource.get_full_property(names(name), kind)
        assert prop is not None and prop.format == 32, name
        return list(prop.value)

    types = values("_NET_WM_WINDOW_TYPE", Xatom.ATOM)
    assert types and types[0] == names("_NET_WM_WINDOW_TYPE_NOTIFICATION")
    states = values("_NET_WM_STATE", Xatom.ATOM)
    assert {
        names("_NET_WM_STATE_ABOVE"),
        names("_NET_WM_STATE_SKIP_TASKBAR"),
        names("_NET_WM_STATE_SKIP_PAGER"),
    } <= set(states)
    assert names("_NET_WM_STATE_DEMANDS_ATTENTION") not in states
    assert values("_NET_WM_USER_TIME", Xatom.CARDINAL) == [0]
    link = native.get_full_property(names("_NET_WM_USER_TIME_WINDOW"), Xatom.WINDOW)
    if link is not None:
        assert link.format == 32 and len(link.value) == 1 and link.value[0]
        auxiliary = connection.create_resource_object("window", int(link.value[0]))
        assert values("_NET_WM_USER_TIME", Xatom.CARDINAL, auxiliary) == [0]
    assert native.get_wm_hints().input == 0


def test_w1_wm_properties_and_exposure_clock_protocol(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exercise ICCCM/EWMH requests and X1's oracle without a listening X server."""
    from Xlib import X, Xatom

    app = get_qapplication()
    if app.platformName() == "xcb":
        assert os.environ.get("ASTRA_VOICE_TEST_X11_ISOLATED") == "1"
        display = os.environ.get("DISPLAY", "")
        assert display.startswith(":") and display.split(".")[0] != ":0"
    recording = Mock(offer=Mock(now_ms=lambda: 100_000))
    window = MeetingOffer(recording)
    window.show("call-app", 60)
    wait_until(window._view.isExposed)
    assert window._view.source().fileName() == "MeetingOffer.qml"
    assert window._root.property("visible")
    title = window._root.findChild(QQuickItem, "offerTitle")
    assert title.property("text") == W1_CALL_TEXTS["call-app"]["title"]
    window_id = int(window._view.winId())
    user_id = window_id + 1
    properties: dict[tuple[int, str], Any] = {}
    events: deque[Any] = deque()
    main = Mock(id=window_id)
    aux = Mock(id=user_id)

    def timestamp_event(atom: str, time: int, *, sent: bool = False) -> SimpleNamespace:
        return SimpleNamespace(
            type=X.PropertyNotify,
            window=main,
            atom=atom,
            time=time,
            state=X.PropertyNewValue,
            send_event=sent,
        )

    def write(wid: int, name: str, kind: int, fmt: int, value: list[int]) -> None:
        properties[wid, name] = SimpleNamespace(format=fmt, value=value)
        if name == TIMESTAMP_ATOM:
            assert kind == Xatom.STRING and fmt == 8
            events.append(timestamp_event(TIMESTAMP_ATOM, 3, sent=True))
            events.append(timestamp_event("_unrelated", 4))
            events.append(timestamp_event(TIMESTAMP_ATOM, 10_000))

    for native in (main, aux):
        native.change_property.side_effect = lambda *args, wid=native.id: write(wid, *args)
        native.get_full_property.side_effect = lambda name, _kind, wid=native.id: properties.get(
            (wid, name)
        )
    main.get_wm_hints.return_value = SimpleNamespace(input=0)
    main.get_attributes.return_value = SimpleNamespace(
        map_state=X.IsViewable,
        your_event_mask=X.VisibilityChangeMask,
    )
    connection = Mock()
    connection.create_resource_object.side_effect = lambda _kind, wid: {
        window_id: main,
        user_id: aux,
    }[wid]
    connection.intern_atom.side_effect = str
    connection.pending_events.side_effect = lambda: len(events)
    connection.next_event.side_effect = events.popleft
    try:
        with monkeypatch.context() as patch:
            patch.setattr(QGuiApplication, "platformName", lambda: "xcb")
            patch.setattr(window, "_xcb", True)
            patch.setattr(window._x11, "d", connection)
            window._configure_native_window()
            _assert_wm_properties(window._x11, window)

            # A stale event and a synthetic one cannot supply this show epoch.
            events.append(timestamp_event(TIMESTAMP_ATOM, 1))
            assert window._sample_x_time() == 10_000
            assert [
                call.kwargs["event_mask"] for call in main.change_attributes.call_args_list
            ] == [
                X.VisibilityChangeMask | X.PropertyChangeMask,
                X.VisibilityChangeMask,
            ]
            main.get_attributes.return_value.map_state = X.IsUnmapped
            assert window._sample_x_time() is None

            write(window_id, "_NET_WM_USER_TIME_WINDOW", Xatom.WINDOW, 32, [user_id])

            def qt_user_time_write() -> None:
                write(window_id, "_NET_WM_USER_TIME", Xatom.CARDINAL, 32, [10_000])
                write(user_id, "_NET_WM_USER_TIME", Xatom.CARDINAL, 32, [10_000])

            qt_user_time_write()
            write(window_id, "_NET_WM_USER_TIME", Xatom.CARDINAL, 32, [0])
            with pytest.raises(AssertionError):
                _assert_wm_properties(window._x11, window)
            patch.setattr(QGuiApplication, "sync", qt_user_time_write)
            main.get_attributes.return_value.map_state = X.IsViewable
            window._stop_tracking_frames()
            patch.setattr(window, "_waiting_for_expose", True)
            patch.setattr(window, "_first_frame_at_ms", None)
            patch.setattr(window, "_frame_x_time", None)
            view = window._view
            blocked = view.blockSignals(True)
            QCoreApplication.sendEvent(
                view, QExposeEvent(QRegion(0, 0, view.width(), view.height()))
            )
            view.blockSignals(blocked)
            assert window._frame_x_time is None
            _assert_wm_properties(window._x11, window)
            view.frameSwapped.emit()
            QCoreApplication.sendPostedEvents(None, QEvent.MetaCall)
            assert window._frame_x_time == 10_000
            _assert_wm_properties(window._x11, window)

            qt_user_time_write()
            button = window._root.findChild(QQuickItem, "offerStart")
            point = button.mapToScene(QPointF(button.width() / 2, button.height() / 2))
            event = QMouseEvent(
                QEvent.MouseButtonPress, point, Qt.LeftButton, Qt.LeftButton, Qt.NoModifier
            )
            event.setTimestamp(10_100)
            QCoreApplication.sendEvent(view, event)
            recording.press_offer.assert_not_called()
            _assert_wm_properties(window._x11, window)
    finally:
        window.shutdown()
        sip.delete(window)
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        app.processEvents()


def test_x1_x2_x3_click_focus_and_guard(
    offer: tuple[MeetingOffer, MeetingRecording, list[float], Mock],
    xdisplay: X11Display,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from Xlib import XK, X, Xatom
    from Xlib.ext import xtest

    window, recording, now, role = offer
    observed = threading.Event()
    presses: list[tuple[str, int]] = []
    actual_press = recording.press_offer
    intent = Mock(wraps=recording.offer.startRequested)

    def spy(action: str, pressed_at_ms: int) -> bool:
        presses.append((action, pressed_at_ms))
        return actual_press(action, pressed_at_ms)

    with paste_target(tmp_path / "target") as target, monkeypatch.context() as patch:
        patch.setattr(recording, "press_offer", spy)
        patch.setattr(recording.offer, "startRequested", intent)
        window._view.pointerDispatched.connect(observed.set)
        target.activate(xdisplay)
        start_focus = _focus(xdisplay)
        assert start_focus == target.window
        active = xdisplay.active_window()
        wm = xdisplay.root.get_full_property(
            xdisplay.d.intern_atom("_NET_SUPPORTING_WM_CHECK"),
            Xatom.WINDOW,
        )

        def still_focused() -> None:
            # Without a WM paste_target writes EWMH itself. CI then proves native
            # input focus, while the WM's active-window policy is a laptop check.
            assert _focus(xdisplay) == start_focus
            if wm is not None:
                assert xdisplay.active_window() == active == target.window

        _show(window, recording)
        assert window._view.property("_q_showWithoutActivating") is True
        still_focused()
        _assert_wm_properties(xdisplay, window)
        wait_until(lambda: window._frame_x_time is not None, timeout=2)
        frame_x_time = window._frame_x_time
        assert frame_x_time is not None, "PropertyNotify must provide the real XCB epoch"
        now[0] = 100.1
        _click_start(xdisplay, window)
        wait_until(observed.is_set)
        assert not presses
        still_focused()
        _assert_wm_properties(xdisplay, window)
        recording.tick()
        assert not recording.dialog_open and window._view.isVisible()
        intent.assert_not_called()

        now[0] = 100.6
        keycode = xdisplay.d.keysym_to_keycode(XK.string_to_keysym("Return"))
        assert keycode
        xtest.fake_input(xdisplay.d, X.KeyPress, keycode)
        xtest.fake_input(xdisplay.d, X.KeyRelease, keycode)
        xdisplay.d.sync()
        target.expect_text("\n")
        recording.tick()
        still_focused()
        assert not presses and not recording.dialog_open
        intent.assert_not_called()

        observed.clear()
        # Age the sampled epoch on the test clock. XTest still supplies the real
        # mouse event and its server timestamp, with no wall-time sleep.
        with monkeypatch.context() as clock_patch:
            clock_patch.setattr(window, "_frame_x_time", (frame_x_time - 500) & 0xFFFFFFFF)
            _click_start(xdisplay, window)
            wait_until(observed.is_set)
        assert presses == [("start", 100_600)]
        still_focused()
        _assert_wm_properties(xdisplay, window)
        recording.tick()
        assert recording.dialog_open and not window._view.isVisible()
        intent.assert_called_once_with()
        assert not any(
            call.args[0]["type"] in {"meeting.capture.prepare", "meeting.capture.open"}
            for call in role.send.call_args_list
        )


def test_x4_real_cursor_moves_w1_only_for_the_card(
    offer: tuple[MeetingOffer, MeetingRecording, list[float], Mock],
    xdisplay: X11Display,
    xapp: Any,
) -> None:
    window, recording, _, _ = offer
    # With no decorated panel in Xvfb the primary available area is the work area.
    area = xapp.primaryScreen().availableGeometry()
    normal = QRect(area.x() + (area.width() - 380) // 2, area.y() + 12, 380, 171)
    corner_x = area.right() - 12 - 379
    on_card = QPoint(normal.center().x(), normal.bottom())
    below = on_card + QPoint(0, 1)

    def move(point: QPoint) -> None:
        QCursor.setPos(point)
        xapp.sync()
        assert QCursor.pos() == point, "X11 cursor did not reach the test position"

    original = QCursor.pos()
    try:
        move(on_card)
        assert normal.contains(QCursor.pos()), "cursor must be inside the visible card"
        _show(window, recording)
        assert window._view.position() + QPoint(36, 36) == QPoint(corner_x, normal.y())
        _assert_wm_properties(xdisplay, window)
        recording._event(
            {
                "type": "meeting.call.ended",
                "call_id": "call-w1",
                "reason": "mic_released",
                "generation": 1,
            }
        )
        move(below)
        assert not normal.contains(QCursor.pos()) and normal.adjusted(0, 0, 0, 36).contains(
            QCursor.pos()
        )
        _show(window, recording, "call-after")
        assert window._view.position() + QPoint(36, 36) == normal.topLeft()
    finally:
        move(original)
