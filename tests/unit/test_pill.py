"""Ядро пилюли: фейковые окно, QML и часы, без приложения Qt и дисплея."""

from __future__ import annotations

import ast
import logging
import os
import subprocess
import sys
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from importlib.util import resolve_name
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import Mock, call

import pytest
from PyQt5.QtCore import QCoreApplication, QEvent, QObject, QPoint, QRect, Qt, QUrl
from PyQt5.QtGui import QRegion
from PyQt5.QtQuick import QQuickView

from astra_voice.core.paths import qml_dir
from astra_voice.platform.session import SessionKind
from astra_voice.ui import indicators, notify
from astra_voice.ui import pill as module
from astra_voice.ui.pill import (
    CLIPBOARD_REASONS,
    CLIPBOARD_WINDOW_CHANGED,
    ERROR_BUFFER_CLEARED,
    ERROR_MICROPHONE_UNAVAILABLE,
    ERROR_REASONS,
    ERROR_RECOGNITION_FAILED,
    ERROR_RECOGNITION_RESTARTED,
    STATE_DURATION_MS,
    Pill,
    PillState,
)

if TYPE_CHECKING:
    from astra_voice.ui.tray import Tray

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]

EXPECTED_DURATIONS = {
    PillState.DONE: 500,
    PillState.CLIPBOARD_ONLY: 1200,
    PillState.EMPTY: 1000,
    PillState.CANCELLED: 800,
    PillState.ERROR: 3000,
    PillState.LIMIT: 2000,
    PillState.LOADING_MODEL: 5000,
}


class Signal:
    def __init__(self) -> None:
        self.callbacks: list[Callable[[], None]] = []

    def connect(self, callback: Callable[[], None], connection: object = None) -> None:
        self.callbacks.append(callback)

    def emit(self) -> None:
        for callback in self.callbacks:
            callback()


@dataclass
class Clock:
    now: int = 0
    pending: list[tuple[int, Callable[[], None]]] = field(default_factory=list)
    timers: list[Timer] = field(default_factory=list)

    def __call__(self, parent: QObject) -> Timer:
        timer = Timer()
        self.timers.append(timer)
        return timer

    def singleShot(self, milliseconds: int, callback: Callable[[], None]) -> None:
        self.pending.append((self.now + milliseconds, callback))

    def advance(self, milliseconds: int) -> None:
        end = self.now + milliseconds
        while self.pending:
            event = min(self.pending, key=lambda item: item[0])
            if event[0] > end:
                break
            self.pending.remove(event)
            self.now = event[0]
            event[1]()
        self.now = end


class Timer:
    def __init__(self) -> None:
        self.timeout = Signal()
        self.interval = -1
        self.active = False
        self.single_shot = False

    def setSingleShot(self, value: bool) -> None:
        self.single_shot = value

    def setInterval(self, value: int) -> None:
        self.interval = value

    def isActive(self) -> bool:
        return self.active

    def start(self) -> None:
        self.active = True

    def stop(self) -> None:
        self.active = False

    def fire(self) -> None:
        if self.active:
            if self.single_shot:
                self.active = False
            self.timeout.emit()


@dataclass
class Harness:
    pill: Pill
    view: Mock
    root: Mock
    properties: dict[str, Any]
    factory: Mock
    clock: Clock
    x11: Mock
    app: Mock
    ewmh: Mock


class BadWindow(Exception):
    pass


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> Harness:
    forbidden_view = Mock(side_effect=AssertionError("Настоящий QQuickView запрещён"))
    forbidden_view.SizeViewToRootObject = QQuickView.SizeViewToRootObject
    monkeypatch.setattr(module, "QQuickView", forbidden_view)
    clock = Clock()
    monkeypatch.setattr(module, "QTimer", clock)
    monkeypatch.setattr(module, "monotonic", lambda: clock.now / 1000)
    ewmh = Mock()
    errors = Mock(BadWindow=BadWindow)
    errors.CatchError.return_value.get_error.return_value = None
    monkeypatch.setitem(
        sys.modules,
        "Xlib",
        Mock(
            Xatom=Mock(CARDINAL=6, ATOM=4),
            X=Mock(
                IsUnmapped=0,
                IsViewable=2,
                InputOnly=2,
                Above=0,
                SubstructureRedirectMask=1,
                SubstructureNotifyMask=2,
            ),
            error=errors,
        ),
    )
    monkeypatch.setitem(sys.modules, "Xlib.protocol", Mock(event=ewmh))
    x11 = Mock(close=lambda: None)
    conn, root_window = x11.d, x11.root
    x11.open.return_value = True
    x11.active_window.return_value = None
    x11.client_list_stacking.return_value = []
    x11.window_geometry.return_value = (948, 1178, 224, 78)
    x11.d.intern_atom.side_effect = lambda name, **kwargs: name
    x11.d.get_selection_owner.return_value = 1
    x11.d.create_resource_object.return_value.get_attributes.return_value.map_state = 0
    x11.d.create_resource_object.return_value.get_attributes.return_value.win_class = 1
    x11.d.create_resource_object.return_value.get_full_property.return_value = None
    x11.root.get_full_property.return_value = None
    x11.root.query_tree.return_value.children = [Mock(id=42)]
    monkeypatch.setattr(module, "X11Display", Mock(return_value=x11))
    # Конструктор X11Display ленивый, как настоящий.
    x11.d = x11.root = None

    def open_display() -> bool:
        x11.d, x11.root = conn, root_window
        return True

    x11.open.side_effect = open_display
    app = Mock()
    app.platformName.return_value = "xcb"
    app.primaryScreen.return_value.availableGeometry.return_value = QRect(100, 200, 1920, 1080)
    app.primaryScreen.return_value.geometry.return_value = QRect(100, 200, 1920, 1200)
    monkeypatch.setattr(module, "QGuiApplication", app)
    monkeypatch.setattr(QCoreApplication, "processEvents", app.processEvents)
    properties: dict[str, Any] = {"pillWidth": 187.6, "pillHeight": 36}
    root = Mock(
        cancelClicked=Signal(),
        detailsClicked=Signal(),
        widthChanged=Signal(),
        heightChanged=Signal(),
    )
    root.property.side_effect = properties.__getitem__
    root.setProperty.side_effect = properties.__setitem__
    # QObject может уничтожаться при сборке циклов: его слот не должен обращаться
    # к уже разобранным внутренностям Mock.
    event_filters: list[Pill] = []
    view = Mock(
        widthChanged=Signal(),
        heightChanged=Signal(),
        visibleChanged=Signal(),
        deleteLater=lambda: None,
        installEventFilter=Mock(side_effect=event_filters.append),
    )
    view.winId.return_value = 42
    view.rootObject.return_value = root
    view.isVisible.return_value = False
    view.isExposed.return_value = False
    # Экран самого окна намеренно другой: запасной путь обязан выбрать primaryScreen.
    view.screen.return_value.availableGeometry.return_value = QRect(2020, 200, 1600, 900)

    def show() -> None:
        changed = not view.isVisible()
        view.isVisible.return_value = True
        view.isExposed.return_value = True
        conn.create_resource_object.return_value.get_attributes.return_value.map_state = 2
        if changed:
            view.visibleChanged.emit()
            for event_filter in event_filters:
                event_filter.eventFilter(view, QEvent(QEvent.Expose))

    def hide() -> None:
        changed = view.isVisible()
        view.isVisible.return_value = False
        view.isExposed.return_value = False
        conn.create_resource_object.return_value.get_attributes.return_value.map_state = 0
        if changed:
            view.visibleChanged.emit()

    def resize(width: int, height: int) -> None:
        view.width.return_value = width
        view.height.return_value = height

    view.show.side_effect = show
    view.hide.side_effect = hide
    view.resize.side_effect = resize
    factory = Mock(return_value=view)
    pill = Pill(view_factory=factory)
    # Тесты настраивают фейковый транспорт до первого open(), не создавая соединение.
    x11.d, x11.root = conn, root_window
    return Harness(pill, view, root, properties, factory, clock, x11, app, ewmh)


def test_exact_enum_and_durations() -> None:
    assert {state.name: state.value for state in PillState} == {
        "HIDDEN": "hidden",
        "LOADING_MODEL": "loading-model",
        "LISTENING": "listening",
        "LISTENING_SILENT": "listening-silent",
        "LIMIT": "limit",
        "PROCESSING": "processing",
        "DONE": "done",
        "CLIPBOARD_ONLY": "clipboard-only",
        "EMPTY": "empty",
        "CANCELLED": "cancelled",
        "ERROR": "error",
        "DISABLED": "disabled",
    }
    assert STATE_DURATION_MS == EXPECTED_DURATIONS


def test_window_preloaded_once_with_flags_and_shadow_space(harness: Harness) -> None:
    harness.factory.assert_called_once_with()
    harness.view.installEventFilter.assert_called_once_with(harness.pill)
    harness.view.setSource.assert_called_once_with(QUrl.fromLocalFile(str(qml_dir() / "Pill.qml")))
    harness.view.setFlags.assert_called_once_with(
        Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.WindowDoesNotAcceptFocus
    )
    harness.view.setColor.assert_called_once_with(Qt.transparent)
    harness.view.setResizeMode.assert_called_once_with(QQuickView.SizeViewToRootObject)
    harness.view.resize.assert_called_with(224, 78)
    harness.view.setPosition.assert_called_with(948, 1178)
    assert harness.properties["x"] == harness.properties["y"] == 18
    assert harness.pill.state is PillState.HIDDEN
    assert not harness.pill.visible
    harness.view.show.assert_not_called()
    for state in PillState:
        harness.pill.show_state(state)
    harness.factory.assert_called_once_with()
    assert harness.view.setSource.call_count == 1


def test_custom_url_session_and_parent(harness: Harness) -> None:
    url = QUrl.fromLocalFile("/tmp/custom-pill.qml")
    parent = QObject()
    pill = Pill(session=SessionKind.FLY, qml_url=url, view_factory=harness.factory, parent=parent)
    harness.view.setSource.assert_called_with(url)
    assert pill.parent() is parent
    assert pill.state is PillState.HIDDEN


def test_missing_qml_root_fails_during_init(harness: Harness) -> None:
    harness.view.rootObject.return_value = None
    with pytest.raises(RuntimeError, match="QML"):
        Pill(view_factory=harness.factory)
    harness.view.show.assert_not_called()


def test_size_tracks_deferred_qml_layout(harness: Harness) -> None:
    harness.pill.show_state(PillState.ERROR, text=ERROR_MICROPHONE_UNAVAILABLE)
    harness.properties["pillWidth"] = 319.2
    harness.root.widthChanged.emit()
    harness.view.resize.assert_called_with(356, 78)
    harness.properties["pillHeight"] = 40
    harness.root.heightChanged.emit()
    harness.view.resize.assert_called_with(356, 82)
    # SizeViewToRootObject может позже убрать поля тени; восстанавливаем их.
    harness.view.resize(320, 40)
    harness.view.widthChanged.emit()
    harness.view.resize.assert_called_with(356, 82)
    harness.view.resize(320, 40)
    harness.view.heightChanged.emit()
    harness.view.resize.assert_called_with(356, 82)
    harness.app.primaryScreen.return_value = None
    harness.root.widthChanged.emit()


@pytest.mark.parametrize("state", list(PillState))
def test_visibility_and_qml_state(harness: Harness, state: PillState) -> None:
    harness.pill.show_state(state)
    assert harness.pill.state is state
    assert harness.properties["avState"] == state.value
    assert harness.pill.visible == (state not in (PillState.HIDDEN, PillState.DISABLED))
    harness.view.isVisible.return_value = False
    assert not harness.pill.visible


def test_visibility_tracks_exposure_events(harness: Harness) -> None:
    harness.pill.show_state(PillState.LISTENING)
    assert harness.pill.visible
    harness.view.isExposed.return_value = False
    assert harness.pill.visible  # Чтение свойства не опрашивает backend.
    assert not harness.pill.eventFilter(harness.view, QEvent(QEvent.Expose))
    assert not harness.pill.visible
    harness.view.isExposed.return_value = True
    assert not harness.pill.visible
    assert not harness.pill.eventFilter(harness.view, QEvent(QEvent.Expose))
    assert harness.pill.visible
    harness.view.isVisible.return_value = False
    assert not harness.pill.visible


@pytest.mark.parametrize("exposes", [False, True])
def test_visible_waits_for_expose_without_processing_events(
    harness: Harness, exposes: bool
) -> None:
    def show_without_event() -> None:
        harness.view.isVisible.return_value = True
        harness.view.isExposed.return_value = exposes
        harness.view.visibleChanged.emit()

    harness.view.show.side_effect = show_without_event
    # Старый getter обработал бы вложенное hide() и изменил эпизод стража.
    harness.app.processEvents.side_effect = lambda *args: harness.pill.hide()
    harness.pill.show_state(PillState.LISTENING)
    assert harness.pill.visible
    harness.clock.advance(999)
    assert harness.pill.visible
    assert not harness.pill.eventFilter(harness.view, QEvent(QEvent.Expose))
    assert harness.pill.visible == exposes
    assert harness.pill.visible == exposes
    harness.pill.hide()
    assert not harness.pill.visible
    harness.pill.show_state(PillState.LISTENING)
    assert harness.pill.visible
    harness.pill.eventFilter(harness.view, QEvent(QEvent.Expose))
    assert harness.pill.visible == exposes
    assert harness.app.processEvents.call_count == 0


def test_exposure_wait_expires_despite_repeated_show_requests(harness: Harness) -> None:
    def show_without_exposure() -> None:
        harness.view.isVisible.return_value = True
        harness.view.visibleChanged.emit()

    harness.view.show.side_effect = show_without_exposure
    harness.pill.show_state(PillState.LISTENING)
    assert harness.pill.visible
    for _ in range(9):
        harness.clock.advance(100)
        harness.pill.show_state(PillState.LISTENING)
        harness.pill.set_forced(True)
        assert harness.pill.visible
    harness.clock.advance(99)
    assert harness.pill.visible
    harness.clock.advance(1)
    assert not harness.pill.visible
    harness.pill.set_forced(True)
    harness.clock.advance(1000)
    assert not harness.pill.visible
    harness.view.hide.assert_called_once_with()  # Только начальный HIDDEN.
    harness.view.isExposed.return_value = True
    harness.pill.eventFilter(harness.view, QEvent(QEvent.Expose))
    assert harness.pill.visible
    assert harness.app.processEvents.call_count == 0


@pytest.mark.parametrize("hide", ["hidden", "disabled", "setting"])
def test_explicit_hide_cancels_exposure_wait(harness: Harness, hide: str) -> None:
    harness.view.show.side_effect = lambda: setattr(harness.view.isVisible, "return_value", True)
    harness.pill.show_state(PillState.LISTENING)
    assert harness.pill.visible
    if hide == "setting":
        harness.pill.set_enabled(False)
    else:
        harness.pill.show_state(PillState(hide))
    assert not harness.pill.visible
    harness.clock.advance(1000)
    assert not harness.pill.visible


@pytest.mark.parametrize("hide", ["event", "signal", "method"])
def test_hide_clears_last_exposure(harness: Harness, hide: str) -> None:
    harness.pill.show_state(PillState.LISTENING)
    assert harness.pill.visible
    if hide == "event":
        assert not harness.pill.eventFilter(harness.view, QEvent(QEvent.Hide))
    elif hide == "signal":
        harness.view.hide()
    else:
        harness.pill.hide()
    # Окно получает ограниченное ожидание новой экспозиции при перепоказе.
    harness.view.show.side_effect = lambda: setattr(harness.view.isVisible, "return_value", True)
    harness.view.isVisible.return_value = True
    harness.pill.show_state(PillState.LISTENING)
    harness.clock.advance(1000)
    assert not harness.pill.visible
    harness.view.isExposed.return_value = True
    harness.pill.eventFilter(harness.view, QEvent(QEvent.Expose))
    assert harness.pill.visible


def test_filter_ignores_other_objects_and_unrelated_events(harness: Harness) -> None:
    harness.pill.show_state(PillState.LISTENING)
    assert not harness.pill.eventFilter(QObject(), QEvent(QEvent.Hide))
    assert not harness.pill.eventFilter(harness.view, QEvent(QEvent.UpdateRequest))
    assert harness.pill.visible
    harness.pill.hide()
    assert not harness.pill.eventFilter(QObject(), QEvent(QEvent.Expose))
    assert not harness.pill.visible


@pytest.mark.parametrize("platform", ["xcb", "offscreen"])
@pytest.mark.parametrize("failure", [AttributeError, NotImplementedError])
def test_missing_exposure_support_never_claims_visibility(
    harness: Harness, platform: str, failure: type[Exception]
) -> None:
    harness.app.platformName.return_value = platform
    harness.pill.show_state(PillState.LISTENING)
    harness.view.isExposed.side_effect = failure
    harness.pill.eventFilter(harness.view, QEvent(QEvent.Expose))
    assert not harness.pill.visible
    harness.pill.hide()
    assert not harness.pill.visible


@pytest.mark.parametrize(("state", "duration"), list(EXPECTED_DURATIONS.items()))
def test_expiry_at_exact_deadline(harness: Harness, state: PillState, duration: int) -> None:
    harness.pill.show_state(state)
    assert len(harness.clock.pending) == 1
    harness.clock.advance(duration - 1)
    assert harness.pill.state is state
    assert harness.pill.visible
    harness.clock.advance(1)
    assert harness.pill.state is PillState.HIDDEN
    assert harness.properties["avState"] == "hidden"
    assert not harness.pill.visible


@pytest.mark.parametrize("state", [s for s in PillState if s not in EXPECTED_DURATIONS])
def test_persistent_states_have_no_timer(harness: Harness, state: PillState) -> None:
    harness.pill.show_state(state)
    assert not harness.clock.pending
    harness.clock.advance(60_000)
    assert harness.pill.state is state


@pytest.mark.parametrize("state", list(PillState))
def test_new_state_cancels_previous_timer(harness: Harness, state: PillState) -> None:
    harness.pill.show_state(PillState.DONE)
    harness.clock.advance(400)
    harness.pill.show_state(state)
    harness.clock.advance(100)
    assert harness.pill.state is state
    if state in EXPECTED_DURATIONS:
        harness.clock.advance(EXPECTED_DURATIONS[state] - 101)
        assert harness.pill.state is state
        harness.clock.advance(1)
        assert harness.pill.state is PillState.HIDDEN


def test_hide_cancels_pending_expiry(harness: Harness) -> None:
    harness.pill.show_state(PillState.ERROR, text=ERROR_MICROPHONE_UNAVAILABLE)
    harness.pill.hide()
    assert harness.pill.state.value == "hidden"
    assert not harness.pill.visible
    harness.pill.show_state(PillState.LISTENING)
    harness.clock.advance(3000)
    assert harness.pill.state is PillState.LISTENING


def test_level_history_shift_and_listening_transitions(harness: Harness) -> None:
    assert harness.properties["levels"] == [0.0] * 9
    samples = [i / 10 for i in range(10)]
    for index, level in enumerate(samples):
        harness.pill.show_state(PillState.LISTENING, level=level)
        assert harness.properties["levels"] == ([0.0] * 9 + samples[: index + 1])[-9:]
    old_list = harness.properties["levels"]
    harness.pill.show_state(PillState.LISTENING_SILENT, level=0.0)
    assert harness.properties["levels"] == samples[2:] + [0.0]
    assert old_list == samples[1:]
    harness.pill.show_state(PillState.LISTENING)
    assert harness.properties["levels"] == samples[2:] + [0.0]


@pytest.mark.parametrize(
    "state", [s for s in PillState if s not in (PillState.LISTENING, PillState.LISTENING_SILENT)]
)
def test_non_listening_states_clear_history(harness: Harness, state: PillState) -> None:
    harness.pill.show_state(PillState.LISTENING, level=0.9)
    harness.pill.show_state(state, level=0.5)
    assert harness.properties["levels"] == [0.0] * 9
    harness.pill.show_state(PillState.LISTENING)
    assert harness.properties["levels"] == [0.0] * 9


@pytest.mark.parametrize("state", list(PillState))
def test_disabled_and_forced_priority(harness: Harness, state: PillState) -> None:
    harness.pill.set_enabled(False)
    harness.pill.show_state(state)
    assert harness.pill.state is PillState.DISABLED
    assert harness.properties["avState"] == "disabled"
    assert not harness.pill.visible
    harness.pill.set_forced(True)
    assert harness.pill.state is state
    assert harness.pill.visible == (state not in (PillState.HIDDEN, PillState.DISABLED))
    harness.pill.set_enabled(False)
    assert harness.pill.state is state
    harness.pill.set_forced(False)
    assert harness.pill.state is PillState.DISABLED
    assert not harness.pill.visible
    harness.pill.set_enabled(True)
    assert harness.pill.state is state


def test_disable_during_recording_and_force_before_show(harness: Harness) -> None:
    harness.pill.show_state(PillState.LISTENING, level=0.8)
    harness.pill.set_enabled(False)
    assert not harness.pill.visible
    assert harness.properties["levels"] == [0.0] * 9
    harness.pill.set_forced(True)
    assert harness.pill.visible
    assert harness.pill.state is PillState.LISTENING
    harness.pill.show_state(PillState.LISTENING_SILENT, level=0.0)
    assert harness.pill.visible
    harness.pill.hide()
    assert harness.pill.state is PillState.HIDDEN
    assert not harness.pill.visible


def test_settings_do_not_extend_timer_or_restore_expired_state(harness: Harness) -> None:
    harness.pill.show_state(PillState.DONE)
    harness.clock.advance(400)
    harness.pill.set_enabled(False)
    harness.pill.set_forced(True)
    harness.clock.advance(100)
    assert harness.pill.state is PillState.HIDDEN
    harness.pill.set_forced(False)
    harness.pill.set_enabled(True)
    assert harness.pill.state is PillState.HIDDEN
    harness.pill.set_enabled(False)
    harness.pill.show_state(PillState.ERROR, text=ERROR_MICROPHONE_UNAVAILABLE)
    harness.clock.advance(3000)
    harness.pill.set_forced(True)
    assert harness.pill.state is PillState.HIDDEN
    assert harness.properties["label"] == ""


@pytest.mark.parametrize(
    ("signal", "attribute"),
    [("cancelClicked", "on_cancel_clicked"), ("detailsClicked", "on_details_clicked")],
)
def test_callbacks_and_none(harness: Harness, signal: str, attribute: str) -> None:
    emitter = getattr(harness.root, signal)
    assert getattr(harness.pill, attribute) is None
    emitter.emit()
    callback = Mock()
    setattr(harness.pill, attribute, callback)
    emitter.emit()
    callback.assert_called_once_with()
    setattr(harness.pill, attribute, None)
    emitter.emit()
    callback.assert_called_once_with()


@pytest.mark.parametrize("state", list(PillState))
def test_unregistered_text_never_reaches_qml_or_logs(
    harness: Harness, state: PillState, caplog: pytest.LogCaptureFixture
) -> None:
    marker = "DICTATION_MARKER_никакого_распознанного_текста"
    harness.pill.show_state(PillState.ERROR, text=ERROR_MICROPHONE_UNAVAILABLE)
    harness.root.setProperty.reset_mock()
    with caplog.at_level(logging.DEBUG, logger=module.__name__):
        harness.pill.show_state(state, text=marker)
        expected = ERROR_RECOGNITION_FAILED if state is PillState.ERROR else ""
        assert harness.properties["label"] == expected
        assert marker not in repr(harness.root.setProperty.call_args_list)
        assert marker not in vars(harness.pill).values()
        harness.pill.set_enabled(False)
        harness.pill.set_forced(True)
        harness.clock.advance(5000)
        harness.pill.hide()
    assert marker not in caplog.text
    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert [record.message for record in warnings] == (
        ["Пилюля: причина вне реестра"]
        if state in (PillState.ERROR, PillState.CLIPBOARD_ONLY)
        else []
    )
    state_records = [r for r in caplog.records if r.msg == "Пилюля: %s"]
    assert all(record.args in [(s.name,) for s in PillState] for record in state_records)
    assert harness.properties["label"] == ""
    assert marker not in vars(harness.pill).values()


def test_registered_clipboard_reason_reaches_qml(
    harness: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    harness.pill.show_state(PillState.CLIPBOARD_ONLY, text=CLIPBOARD_WINDOW_CHANGED)
    assert harness.properties["label"] == CLIPBOARD_WINDOW_CHANGED
    assert not caplog.records
    harness.pill.set_enabled(False)
    assert harness.properties["label"] == ""
    harness.pill.set_forced(True)
    assert harness.properties["label"] == CLIPBOARD_WINDOW_CHANGED
    harness.clock.advance(EXPECTED_DURATIONS[PillState.CLIPBOARD_ONLY])
    assert harness.properties["label"] == ""


def test_clipboard_without_text_clears_previous_reason(harness: Harness) -> None:
    harness.pill.show_state(PillState.CLIPBOARD_ONLY, text=CLIPBOARD_WINDOW_CHANGED)
    harness.pill.show_state(PillState.CLIPBOARD_ONLY)
    assert harness.properties["label"] == ""


@pytest.mark.parametrize("reason", ["", ERROR_MICROPHONE_UNAVAILABLE, "DICTATION_MARKER_текст"])
def test_unregistered_clipboard_reason_uses_fallback_and_is_not_logged(
    harness: Harness, reason: str, caplog: pytest.LogCaptureFixture
) -> None:
    harness.pill.show_state(PillState.CLIPBOARD_ONLY, text=CLIPBOARD_WINDOW_CHANGED)
    harness.root.setProperty.reset_mock()
    with caplog.at_level(logging.DEBUG, logger=module.__name__):
        harness.pill.show_state(PillState.CLIPBOARD_ONLY, text=reason)
    assert harness.properties["label"] == ""
    if reason:
        assert reason not in repr(harness.root.setProperty.call_args_list)
        assert reason not in vars(harness.pill).values()
        assert reason not in caplog.text
    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert [record.message for record in warnings] == ["Пилюля: причина вне реестра"]


def test_clipboard_registry_is_exact() -> None:
    assert CLIPBOARD_REASONS == frozenset(("Окно сменилось — текст в буфере",))
    assert CLIPBOARD_REASONS == frozenset((CLIPBOARD_WINDOW_CHANGED,))


@pytest.mark.parametrize("state", [s for s in PillState if s is not PillState.CLIPBOARD_ONLY])
def test_clipboard_reason_does_not_apply_to_other_states(
    harness: Harness, state: PillState
) -> None:
    harness.pill.show_state(PillState.CLIPBOARD_ONLY, text=CLIPBOARD_WINDOW_CHANGED)
    harness.pill.show_state(state, text=CLIPBOARD_WINDOW_CHANGED)
    assert harness.properties["label"] == (
        ERROR_RECOGNITION_FAILED if state is PillState.ERROR else ""
    )


def test_error_without_text_clears_previous_reason(harness: Harness) -> None:
    harness.pill.show_state(PillState.ERROR, text=ERROR_MICROPHONE_UNAVAILABLE)
    harness.pill.show_state(PillState.ERROR)
    assert harness.properties["label"] == ERROR_RECOGNITION_FAILED


@pytest.mark.parametrize("reason", sorted(ERROR_REASONS))
def test_registered_error_reasons_reach_qml(
    harness: Harness, reason: str, caplog: pytest.LogCaptureFixture
) -> None:
    harness.pill.show_state(PillState.ERROR, text=reason)
    assert harness.properties["label"] == reason
    assert not caplog.records


def test_error_registry_is_exact() -> None:
    assert ERROR_REASONS == {
        "Микрофон недоступен",
        "Распознавание перезапущено",
        "Буфер очищен",
        "Не удалось распознать",
    }
    assert ERROR_REASONS == {
        ERROR_MICROPHONE_UNAVAILABLE,
        ERROR_RECOGNITION_RESTARTED,
        ERROR_BUFFER_CLEARED,
        ERROR_RECOGNITION_FAILED,
    }


@pytest.mark.parametrize(
    "reason", ["", "Нет микрофона", "Не удалось распознать: СЕКРЕТ", "x" * 10000]
)
def test_unregistered_reasons_use_fallback(harness: Harness, reason: str) -> None:
    harness.pill.show_state(PillState.ERROR, text=reason)
    assert harness.properties["label"] == ERROR_RECOGNITION_FAILED


def test_active_window_selects_screen_by_center(harness: Harness) -> None:
    harness.x11.active_window.return_value = 91
    harness.x11.window_geometry.return_value = (2100, -200, 800, 600)
    screen = harness.app.screenAt.return_value
    screen.availableGeometry.return_value = QRect(1920, -300, 1600, 850)
    harness.pill.show_state(PillState.LISTENING)
    harness.x11.window_geometry.assert_called_with(91)
    harness.app.screenAt.assert_called_with(QPoint(2500, 100))
    harness.view.setPosition.assert_called_with(2608, 448)


@pytest.mark.parametrize("failure", ["no_active", "no_geometry", "no_screen", "x11_error"])
def test_active_screen_falls_back_to_primary(harness: Harness, failure: str) -> None:
    harness.x11.active_window.return_value = 91
    harness.x11.window_geometry.return_value = (2100, 0, 800, 600)
    if failure == "no_active":
        harness.x11.active_window.return_value = None
    elif failure == "no_geometry":
        harness.x11.window_geometry.return_value = None
    elif failure == "no_screen":
        harness.app.screenAt.return_value = None
    else:
        harness.x11.active_window.side_effect = RuntimeError("X11 unavailable")
    harness.pill.show_state(PillState.LISTENING)
    harness.view.setPosition.assert_called_with(948, 1178)


def test_no_screen_is_safe(harness: Harness) -> None:
    harness.app.primaryScreen.return_value = None
    harness.view.setPosition.reset_mock()
    harness.pill.show_state(PillState.LISTENING)
    harness.view.setPosition.assert_not_called()


def test_visible_edge_offset_matches_live_kde(harness: Harness) -> None:
    harness.app.primaryScreen.return_value.availableGeometry.return_value = QRect(0, 0, 1920, 1148)
    harness.pill._place()
    x, y = harness.view.setPosition.call_args.args
    assert x == (1920 - 224) // 2
    assert y + 18 + 36 == 1100


def test_workarea_overrides_available_geometry(harness: Harness) -> None:
    screen = harness.app.primaryScreen.return_value
    screen.geometry.return_value = QRect(0, 0, 1920, 1200)
    screen.availableGeometry.return_value = QRect(0, 0, 1920, 1200)
    harness.x11.root.get_full_property.side_effect = lambda atom, kind: (
        Mock(format=32, value=[0, 0, 1920, 1148]) if atom == "_NET_WORKAREA" else None
    )
    harness.pill.show_state(PillState.LISTENING)
    harness.view.setPosition.assert_called_with(848, 1046)
    harness.x11.root.get_full_property.assert_any_call("_NET_WORKAREA", 6)


def test_workarea_uses_current_desktop_and_selected_monitor(harness: Harness) -> None:
    harness.x11.active_window.return_value = 91
    harness.x11.window_geometry.return_value = (2100, 0, 800, 600)
    screen = harness.app.screenAt.return_value
    screen.geometry.return_value = QRect(1920, 0, 1920, 1200)
    screen.availableGeometry.return_value = QRect(1920, 0, 1920, 1200)
    harness.x11.root.get_full_property.side_effect = lambda atom, kind: Mock(
        format=32,
        value=[0, 0, 3840, 1200, 0, 0, 3840, 1148] if atom == "_NET_WORKAREA" else [1],
    )
    harness.pill.show_state(PillState.LISTENING)
    harness.view.setPosition.assert_called_with(2768, 1046)


@pytest.mark.parametrize(
    "prop",
    [
        None,
        Mock(format=8, value=[0, 0, 1920, 1148]),
        Mock(format=32, value=[0, 0]),
        Mock(format=32, value=[0, 0, 0, 1148]),
    ],
)
def test_invalid_workarea_uses_available_geometry(harness: Harness, prop: Mock | None) -> None:
    harness.x11.root.get_full_property.return_value = prop
    harness.pill.show_state(PillState.LISTENING)
    harness.view.setPosition.assert_called_with(948, 1178)


def test_ewmh_uses_one_lazy_connection_before_map_and_after_show(harness: Harness) -> None:
    harness.x11.open.assert_not_called()
    window = harness.x11.d.create_resource_object.return_value
    window.change_property.assert_not_called()
    harness.pill.show_state(PillState.LISTENING)
    window.change_property.assert_any_call(
        "_NET_WM_WINDOW_TYPE", 4, 32, ["_NET_WM_WINDOW_TYPE_NOTIFICATION"]
    )
    window.change_property.assert_any_call("_NET_WM_DESKTOP", 6, 32, [0xFFFFFFFF])
    window.change_property.assert_called_with("_NET_WM_USER_TIME", 6, 32, [0])
    writes = window.change_property.call_args_list
    assert any(c.args[3] == ["_NET_WM_STATE_ABOVE"] for c in writes)
    assert any(
        c.args[3] == ["_NET_WM_STATE_SKIP_TASKBAR", "_NET_WM_STATE_SKIP_PAGER"] for c in writes
    )
    after_show, _ = harness.clock.timers
    after_show.fire()
    assert harness.pill.visible
    assert harness.ewmh.ClientMessage.call_count == 4
    harness.x11.open.assert_called_once_with()
    harness.view.winId.assert_called_once_with()
    harness.factory.assert_called_once_with()
    assert len(harness.clock.timers) == 2


@pytest.mark.parametrize("session", list(SessionKind))
def test_fly_atoms_only_for_fly_before_first_map(harness: Harness, session: SessionKind) -> None:
    window = harness.x11.d.create_resource_object.return_value
    window.change_property.reset_mock()
    pill = Pill(session=session, view_factory=harness.factory)
    harness.view.show.assert_not_called()
    pill.show_state(PillState.LISTENING)
    writes = window.change_property.call_args_list
    for name in ("_FLY_WM_WINDOW_MAP_ANIMATION", "_FLY_WM_FADE_SHOW"):
        assert (call(name, 6, 32, [0]) in writes) == (session is SessionKind.FLY)
    assert pill.visible


@pytest.mark.parametrize("owner", [0, 123])
def test_mask_only_without_compositor_and_tracks_resize(harness: Harness, owner: int) -> None:
    harness.x11.d.get_selection_owner.return_value = owner
    pill = Pill(view_factory=harness.factory)
    pill.show_state(PillState.LISTENING)
    harness.x11.d.intern_atom.assert_any_call("_NET_WM_CM_S0")
    harness.x11.d.get_selection_owner.assert_called_with("_NET_WM_CM_S0")
    if owner:
        assert harness.view.setMask.call_args.args[0].isEmpty()
        return
    region = harness.view.setMask.call_args.args[0]
    assert isinstance(region, QRegion)
    assert region.boundingRect() == QRect(18, 18, 188, 36)
    assert not region.contains(QPoint(18, 18))
    assert region.contains(QPoint(36, 18))
    assert region.contains(QPoint(18, 36))
    harness.properties["pillWidth"] = 300
    harness.root.widthChanged.emit()
    assert harness.view.setMask.call_args.args[0].boundingRect() == QRect(18, 18, 300, 36)
    harness.properties["pillHeight"] = 40
    harness.view.heightChanged.emit()
    assert harness.view.setMask.call_args.args[0].boundingRect() == QRect(18, 18, 300, 40)


def test_compositor_switch_refreshes_and_removes_mask_at_each_show(harness: Harness) -> None:
    for owner in (1, 0, 123, 0):
        harness.x11.d.get_selection_owner.return_value = owner
        harness.pill.show_state(PillState.LISTENING)
        assert harness.view.setMask.call_args.args[0].isEmpty() == bool(owner)
        harness.pill.hide()
    assert harness.x11.d.get_selection_owner.call_count == 4
    harness.x11.open.assert_called_once_with()


@pytest.mark.parametrize("hide", ["explicit", "expiry", "disabled", "external"])
def test_reassert_timer_lives_only_while_visible(harness: Harness, hide: str) -> None:
    after_show, above = harness.clock.timers
    assert above.interval == 1000
    assert after_show.interval == 0 and after_show.single_shot
    assert not above.active and not after_show.active
    harness.pill.show_state(PillState.DONE)
    assert above.active and after_show.active
    if hide == "explicit":
        harness.pill.hide()
    elif hide == "expiry":
        harness.clock.advance(500)
    elif hide == "disabled":
        harness.pill.set_enabled(False)
    else:
        harness.view.hide()
    assert not above.active and not after_show.active
    harness.x11.client_list_stacking.reset_mock()
    after_show.fire()
    above.fire()
    harness.x11.client_list_stacking.assert_not_called()
    harness.pill.set_enabled(True)
    harness.pill.show_state(PillState.LISTENING)
    assert above.active


@pytest.mark.parametrize(
    ("stack", "mapped", "raises"),
    [
        ([91, 42], 2, False),
        ([42, 91], 2, True),
        ([42, 91], 0, False),
        ([42, 91], 1, False),
        ([], 2, False),
        ([91], 2, True),
    ],
)
def test_reassert_only_when_visible_window_is_above(
    harness: Harness, stack: list[int], mapped: int, raises: bool
) -> None:
    harness.x11.client_list_stacking.return_value = stack
    if stack == [91]:
        harness.x11.root.query_tree.return_value.children = [Mock(id=91)]
    harness.pill.show_state(PillState.LISTENING)
    harness.x11.d.create_resource_object.return_value.get_attributes.return_value.map_state = mapped
    harness.ewmh.reset_mock()
    harness.clock.timers[1].fire()
    harness.view.raise_.assert_not_called()
    above = call(
        window=42,
        client_type="_NET_WM_STATE",
        data=(32, [1, "_NET_WM_STATE_ABOVE", 0, 1, 0]),
    )
    assert (above in harness.ewmh.ClientMessage.call_args_list) == raises
    if raises:
        harness.ewmh.ClientMessage.assert_any_call(
            window=42,
            client_type="_NET_WM_STATE",
            data=(32, [1, "_NET_WM_STATE_ABOVE", 0, 1, 0]),
        )
    else:
        harness.x11.open.assert_called_once_with()


def test_destroyed_client_does_not_hide_next_visible_client(harness: Harness) -> None:
    harness.x11.client_list_stacking.return_value = [42, 92, 91]
    harness.pill.show_state(PillState.LISTENING)
    window: Mock = harness.x11.d.create_resource_object.return_value

    def resource(kind: str, wid: int) -> Mock:
        if wid == 91:
            raise BadWindow("BadWindow")
        return window

    harness.x11.d.create_resource_object.side_effect = resource
    harness.clock.timers[1].fire()
    assert not harness.pill.visible
    harness.ewmh.ClientMessage.assert_any_call(
        window=42,
        client_type="_NET_WM_STATE",
        data=(32, [1, "_NET_WM_STATE_ABOVE", 0, 1, 0]),
    )
    harness.view.raise_.assert_not_called()


def test_occluded_pill_still_reasserts_without_restarting_timers(harness: Harness) -> None:
    harness.pill.show_state(PillState.LISTENING)
    harness.clock.timers[0].fire()
    harness.x11.client_list_stacking.return_value = [42, 91]
    harness.view.isExposed.return_value = False
    harness.pill.eventFilter(harness.view, QEvent(QEvent.Expose))
    harness.ewmh.reset_mock()
    for _ in range(10):
        harness.clock.timers[1].fire()
    assert not harness.pill.visible
    harness.view.raise_.assert_not_called()
    above = call(
        window=42,
        client_type="_NET_WM_STATE",
        data=(32, [1, "_NET_WM_STATE_ABOVE", 0, 1, 0]),
    )
    assert harness.ewmh.ClientMessage.call_args_list.count(above) == 10
    assert not harness.clock.timers[0].active
    harness.x11.open.assert_called_once_with()


@pytest.mark.parametrize("forced", [False, True])
@pytest.mark.parametrize(
    ("stack", "rival", "visible"),
    [
        ([42, 91], (948, 1178, 224, 78), False),
        ([42, 91], (1100, 1200, 500, 100), False),
        ([42, 91], (1172, 1178, 500, 100), True),  # Касание границ, без пересечения.
        ([42, 91], (2000, 0, 1920, 1080), True),  # Другой монитор.
        ([91, 42], (948, 1178, 224, 78), True),
    ],
)
def test_visibility_checks_stacking_and_geometry(
    harness: Harness,
    forced: bool,
    stack: list[int],
    rival: tuple[int, int, int, int],
    visible: bool,
) -> None:
    harness.x11.client_list_stacking.return_value = stack
    harness.x11.window_geometry.side_effect = lambda wid: (
        (948, 1178, 224, 78) if wid == 42 else rival
    )
    harness.pill.show_state(PillState.LISTENING)
    harness.view.hide.reset_mock()
    harness.pill.set_forced(forced)
    harness.clock.timers[1].fire()
    assert harness.pill.visible == visible
    # Пилюля только сообщает видимость; состояние записи остаётся прежним.
    assert harness.pill.state is PillState.LISTENING
    harness.view.hide.assert_not_called()


def test_visible_uses_tick_cache_without_x11_or_event_processing(harness: Harness) -> None:
    harness.pill.show_state(PillState.LISTENING)
    for stack, visible in [([91, 42], True), ([42, 91], False), ([91, 42], True)]:
        previous = harness.pill.visible
        harness.x11.client_list_stacking.return_value = stack
        assert harness.pill.visible == previous
        harness.clock.timers[1].fire()
        requests = list(harness.x11.mock_calls)
        exposures = harness.view.isExposed.call_count
        for _ in range(100):
            assert harness.pill.visible == visible
        assert harness.x11.mock_calls == requests
        assert harness.view.isExposed.call_count == exposures
    assert harness.app.processEvents.call_count == 0
    harness.x11.open.assert_called_once_with()


def fake_fullscreen(harness: Harness) -> Mock:
    active = Mock()
    active.get_full_property.return_value = Mock(format=32, value=["_NET_WM_STATE_FULLSCREEN"])
    pill_window = harness.x11.d.create_resource_object.return_value
    harness.x11.d.create_resource_object.side_effect = lambda kind, wid: (
        active if wid == 91 else pill_window
    )
    harness.x11.active_window.return_value = 91
    harness.app.screenAt.return_value = None
    return active


@pytest.mark.parametrize("trigger", ["show", "tick"])
def test_fullscreen_switches_flags_only_on_mode_changes(harness: Harness, trigger: str) -> None:
    harness.pill.show_state(PillState.LISTENING, level=0.8)
    harness.clock.timers[0].fire()
    active = fake_fullscreen(harness)
    base_flags = harness.view.setFlags.call_args.args[0]
    harness.view.hide.reset_mock()
    generation = harness.pill._timer_generation

    def update() -> None:
        if trigger == "show":
            harness.pill.show_state(PillState.LISTENING)
        else:
            harness.clock.timers[1].fire()

    update()
    harness.view.setFlags.assert_called_with(base_flags | Qt.BypassWindowManagerHint)
    assert harness.view.setFlags.call_count == 2
    assert harness.view.hide.call_count == 1
    assert harness.pill.state is PillState.LISTENING
    assert harness.properties["levels"][-1] == 0.8
    for _ in range(5):
        update()
    assert harness.view.setFlags.call_count == 2
    assert harness.view.hide.call_count == 1
    active.get_full_property.return_value = Mock(format=32, value=[])
    update()
    harness.view.setFlags.assert_called_with(base_flags)
    assert harness.view.setFlags.call_count == 3
    assert harness.view.hide.call_count == 2
    for _ in range(5):
        update()
    assert harness.view.setFlags.call_count == 3
    assert harness.view.hide.call_count == 2
    if trigger == "tick":
        assert harness.pill._timer_generation == generation
    harness.factory.assert_called_once_with()
    harness.view.setSource.assert_called_once()
    harness.view.destroy.assert_not_called()
    harness.view.requestActivate.assert_not_called()
    harness.view.raise_.assert_not_called()
    harness.x11.open.assert_called_once_with()
    assert harness.app.processEvents.call_count == 0


@pytest.mark.parametrize("fullscreen", [False, True])
@pytest.mark.parametrize("trigger", ["show", "tick"])
def test_mode_switch_preserves_visibility_until_exposure_or_timeout(
    harness: Harness, fullscreen: bool, trigger: str
) -> None:
    active = fake_fullscreen(harness)
    active.get_full_property.return_value.value = [] if fullscreen else ["_NET_WM_STATE_FULLSCREEN"]
    harness.pill.show_state(PillState.LISTENING)
    assert harness.pill.visible
    active.get_full_property.return_value.value = ["_NET_WM_STATE_FULLSCREEN"] if fullscreen else []
    switching: list[bool] = []

    def hide_during_switch() -> None:
        harness.view.isVisible.return_value = False
        harness.view.isExposed.return_value = False
        harness.view.visibleChanged.emit()
        harness.pill.eventFilter(harness.view, QEvent(QEvent.Hide))
        harness.pill.eventFilter(harness.view, QEvent(QEvent.Expose))
        switching.append(harness.pill.visible)

    def show_without_exposure() -> None:
        harness.view.isVisible.return_value = True
        harness.view.visibleChanged.emit()

    harness.view.hide.side_effect = hide_during_switch
    harness.view.show.side_effect = show_without_exposure
    flag_calls = harness.view.setFlags.call_count
    if trigger == "show":
        harness.pill.show_state(PillState.LISTENING)
    else:
        harness.clock.timers[1].fire()
    assert switching == [True]
    assert harness.pill.visible
    for _ in range(9):
        harness.clock.advance(100)
        harness.pill.show_state(PillState.LISTENING)
        harness.pill.set_forced(True)
        harness.clock.timers[1].fire()
        assert harness.pill.visible
    assert switching == [True]
    assert harness.view.setFlags.call_count == flag_calls + 1
    harness.clock.advance(99)
    assert harness.pill.visible
    harness.clock.advance(1)
    assert not harness.pill.visible
    harness.pill.set_forced(True)
    assert not harness.pill.visible
    harness.view.isExposed.return_value = True
    harness.pill.eventFilter(harness.view, QEvent(QEvent.Expose))
    assert harness.pill.visible
    assert harness.app.processEvents.call_count == 0


def test_rival_gone_does_not_stop_recording_without_tray(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(indicators, "QTimer", harness.clock)
    monkeypatch.setattr(indicators, "monotonic", lambda: harness.clock.now / 1000)
    events: list[str] = []
    monkeypatch.setattr(notify, "notify_indicators_lost", lambda: events.append("notify"))
    tray = Mock(registered=True)
    guard = indicators.IndicatorGuard(harness.pill, cast("Tray", tray))
    guard.on_stop_recording = lambda: events.append("stop")
    fake_fullscreen(harness)
    harness.pill.show_state(PillState.LISTENING)
    guard.set_recording(True)
    harness.clock.advance(1500)
    tray.registered = False
    assert guard.check()
    harness.clock.advance(1500)  # Оба грейса стража уже закончились.
    assert guard.check()

    def show_without_exposure() -> None:
        harness.view.isVisible.return_value = True
        harness.view.visibleChanged.emit()

    harness.view.show.side_effect = show_without_exposure
    harness.x11.active_window.return_value = None
    harness.clock.timers[1].fire()
    assert not harness.view.isExposed()
    assert guard.check()
    harness.clock.advance(999)
    assert guard.check()
    harness.view.isExposed.return_value = True
    harness.pill.eventFilter(harness.view, QEvent(QEvent.Expose))
    harness.clock.advance(1001)
    assert guard.check()
    assert guard.recording
    assert events == []
    assert harness.app.processEvents.call_count == 0


def test_fullscreen_before_first_show_reapplies_properties_to_new_xid(harness: Harness) -> None:
    fake_fullscreen(harness)
    harness.view.winId.return_value = 84
    harness.pill.show_state(PillState.LISTENING)
    assert harness.view.setFlags.call_args.args[0] & Qt.BypassWindowManagerHint
    assert harness.pill._wid == 84
    harness.x11.d.create_resource_object.assert_any_call("window", 84)
    window = harness.x11.d.create_resource_object.return_value
    window.change_property.assert_any_call("_NET_WM_DESKTOP", 6, 32, [0xFFFFFFFF])
    window.change_property.assert_any_call("_NET_WM_USER_TIME", 6, 32, [0])
    harness.view.show.assert_called_once_with()
    harness.factory.assert_called_once_with()
    harness.view.setSource.assert_called_once()


@pytest.mark.parametrize(
    ("root_stack", "rival_geometry", "visible"),
    [
        ([800, 42], (0, 0, 1920, 1280), True),
        ([42, 800], (0, 0, 1920, 1280), False),
        ([42, 800], (2000, 0, 1920, 1280), True),
        ([800], (0, 0, 1920, 1280), False),
    ],
)
def test_compatibility_visibility_uses_actual_root_stacking(
    harness: Harness,
    root_stack: list[int],
    rival_geometry: tuple[int, int, int, int],
    visible: bool,
) -> None:
    fake_fullscreen(harness)
    # В EWMH остался только клиент fullscreen; в root tree — его рамка и пилюля.
    harness.x11.client_list_stacking.return_value = [91]
    harness.x11.root.query_tree.return_value.children = [Mock(id=wid) for wid in root_stack]
    harness.x11.window_geometry.side_effect = lambda wid: (
        (948, 1178, 224, 78) if wid == 42 else rival_geometry
    )
    harness.pill.show_state(PillState.LISTENING)
    harness.pill.set_forced(True)
    harness.clock.timers[0].fire()
    assert harness.pill.visible == visible
    harness.x11.root.query_tree.assert_called()
    assert harness.pill.state is PillState.LISTENING
    assert harness.view.setFlags.call_args.args[0] & Qt.BypassWindowManagerHint


def test_compatibility_restack_is_verified_before_claiming_visibility(harness: Harness) -> None:
    fake_fullscreen(harness)
    harness.pill.show_state(PillState.LISTENING)
    harness.x11.client_list_stacking.return_value = [91]
    tree = harness.x11.root.query_tree.return_value
    tree.children = [Mock(id=42), Mock(id=800)]
    window = harness.x11.d.create_resource_object.return_value

    def restack(**kwargs: int) -> None:
        tree.children.reverse()

    window.configure.side_effect = restack
    harness.clock.timers[1].fire()
    window.configure.assert_called_once_with(stack_mode=0)
    assert harness.pill.visible
    harness.view.raise_.assert_not_called()


def test_show_state_remaps_externally_unmapped_window(harness: Harness) -> None:
    harness.pill.show_state(PillState.LISTENING)
    harness.view.isExposed.return_value = False
    harness.pill.eventFilter(harness.view, QEvent(QEvent.Expose))
    assert not harness.pill.visible
    assert harness.view.isVisible()
    harness.view.reset_mock()
    harness.pill.show_state(PillState.LISTENING)
    operations = [c for c in harness.view.mock_calls if c in (call.hide(), call.show())]
    assert operations == [call.hide(), call.show()]
    assert harness.pill.visible
    harness.view.setFlags.assert_not_called()
    harness.factory.assert_called_once_with()
    harness.pill.show_state(PillState.LISTENING)
    harness.view.hide.assert_called_once_with()


@pytest.mark.parametrize("compatibility", [False, True])
def test_lift_removes_attention_without_activation(harness: Harness, compatibility: bool) -> None:
    if compatibility:
        fake_fullscreen(harness)
    harness.pill.show_state(PillState.LISTENING)
    harness.x11.client_list_stacking.return_value = [42, 800]
    harness.x11.root.query_tree.return_value.children = [Mock(id=42), Mock(id=800)]
    window = harness.x11.d.create_resource_object.return_value
    window.get_full_property.return_value = Mock(
        format=32, value=["_NET_WM_STATE_ABOVE", "_NET_WM_STATE_DEMANDS_ATTENTION"]
    )
    window.change_property.reset_mock()
    harness.ewmh.reset_mock()
    harness.clock.timers[1].fire()
    harness.view.raise_.assert_not_called()
    harness.view.requestActivate.assert_not_called()
    if compatibility:
        assert window.change_property.call_args.args[3] == ["_NET_WM_STATE_ABOVE"]
    else:
        harness.ewmh.ClientMessage.assert_any_call(
            window=42,
            client_type="_NET_WM_STATE",
            data=(32, [0, "_NET_WM_STATE_DEMANDS_ATTENTION", 0, 1, 0]),
        )
    for message in harness.ewmh.ClientMessage.call_args_list:
        data = message.kwargs["data"][1]
        assert message.kwargs["client_type"] != "_NET_ACTIVE_WINDOW"
        assert data[0] != 1 or "_NET_WM_STATE_DEMANDS_ATTENTION" not in data[1:3]


@pytest.mark.parametrize("failure", ["stack", "sync", "attributes", "send", "async_error"])
@pytest.mark.parametrize("reopens", [False, True])
def test_x11_loss_closes_connection_and_retries_only_once(
    harness: Harness, failure: str, reopens: bool, caplog: pytest.LogCaptureFixture
) -> None:
    from Xlib import error

    harness.pill.show_state(PillState.LISTENING)
    conn, root = harness.x11.d, harness.x11.root
    harness.x11.client_list_stacking.return_value = [42, 91]
    failures = {
        "stack": harness.x11.client_list_stacking,
        "sync": conn.sync,
        "attributes": conn.create_resource_object.return_value.get_attributes,
        "send": root.send_event,
    }
    if failure == "async_error":
        error.CatchError.return_value.get_error.return_value = RuntimeError("BadAccess")
    else:
        failures[failure].side_effect = RuntimeError("connection lost")

    def close() -> None:
        harness.x11.d = harness.x11.root = None

    def reopen() -> bool:
        if reopens:
            harness.x11.d, harness.x11.root = conn, root
        return reopens

    # destroyed уже подключён к безопасному слоту; этот Mock проверяет отказ в рантайме.
    harness.x11.close = Mock(side_effect=close)
    harness.x11.open.side_effect = reopen
    with caplog.at_level(logging.DEBUG, logger=module.__name__):
        harness.clock.timers[1].fire()
    assert "re-assert приостановлен" in caplog.text
    harness.x11.close.assert_called_once_with()
    harness.x11.open.assert_called_once_with()
    for operation in failures.values():
        operation.side_effect = None
    error.CatchError.return_value.get_error.return_value = None
    harness.view.raise_.reset_mock()
    for _ in range(10):
        harness.ewmh.reset_mock()
        harness.clock.timers[1].fire()
        above = call(
            window=42,
            client_type="_NET_WM_STATE",
            data=(32, [1, "_NET_WM_STATE_ABOVE", 0, 1, 0]),
        )
        assert (above in harness.ewmh.ClientMessage.call_args_list) == reopens
    assert harness.x11.open.call_count == 2
    harness.view.raise_.assert_not_called()
    if reopens:
        conn.sync.side_effect = RuntimeError("lost again")
        for _ in range(10):
            harness.clock.timers[1].fire()
        assert harness.x11.close.call_count == 2
        assert harness.x11.open.call_count == 2


def test_connection_closes_when_qobject_is_destroyed(harness: Harness) -> None:
    from PyQt5 import sip

    pending_close = [True]
    harness.x11.close = pending_close.clear
    pill = Pill(view_factory=harness.factory)
    pill.show_state(PillState.LISTENING)
    assert pending_close
    sip.delete(pill)
    assert not pending_close


@pytest.mark.parametrize("failure", ["open", "no_display", "no_xlib", "offscreen"])
def test_unavailable_x11_never_breaks_pill(
    harness: Harness, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    harness.x11.d = None
    harness.x11.active_window.return_value = None
    if failure == "open":
        harness.x11.open.side_effect = RuntimeError("X11 unavailable")
    elif failure == "no_xlib":
        monkeypatch.setitem(sys.modules, "Xlib", None)
        harness.x11.open.side_effect = None
        harness.x11.open.return_value = False
    elif failure == "offscreen":
        harness.app.platformName.return_value = "offscreen"
    else:
        harness.x11.open.side_effect = None
        harness.x11.open.return_value = False
    harness.ewmh.reset_mock()
    pill = Pill(session=SessionKind.FLY, view_factory=harness.factory)
    pill.show_state(PillState.LISTENING)
    for timer in harness.clock.timers:
        timer.fire()
    assert pill.visible
    harness.view.setPosition.assert_called_with(948, 1178)
    harness.view.setMask.assert_not_called()
    assert not harness.ewmh.mock_calls
    pill.hide()
    assert not pill.visible


def test_x11_errors_and_missing_xlib_after_init_are_safe(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness.x11.root.get_full_property.side_effect = RuntimeError("X11 unavailable")
    harness.x11.d.create_resource_object.side_effect = RuntimeError("X11 unavailable")
    harness.pill.show_state(PillState.LISTENING)
    harness.clock.timers[0].fire()
    monkeypatch.setitem(sys.modules, "Xlib", None)
    harness.pill._apply_ewmh()
    harness.pill._resize()
    harness.clock.timers[1].fire()
    assert not harness.pill.visible  # Потеря X-проверки не подтверждает видимость.
    harness.pill.hide()


def test_show_latency_logged_through_return_from_show(
    harness: Harness, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    ticks = iter([1.0, 1.025])
    monkeypatch.setattr(module, "perf_counter", lambda: next(ticks))
    with caplog.at_level(logging.DEBUG, logger=module.__name__):
        harness.pill.show_state(PillState.LISTENING)
    assert "show_state → show() 25.00 мс" in caplog.text


def test_real_qml_clears_root_and_presentation_labels() -> None:
    # Отдельный процесс сохраняет unit-набор независимым от глобального QApplication.
    # Проверяем настоящий QML, включая прямое изменение avState без Python-моста.
    source = """
from PyQt5 import sip
from PyQt5.QtCore import QObject
from PyQt5.QtWidgets import QApplication
from PyQt5.QtQuick import QQuickView
from astra_voice.ui.pill import Pill, PillState, ERROR_MICROPHONE_UNAVAILABLE

app = QApplication([])
view = QQuickView()
pill = Pill(view_factory=lambda: view)
root = view.rootObject()
presentation = next(
    child for child in root.findChildren(QObject)
    if child.metaObject().indexOfProperty("label") >= 0
    and child.metaObject().indexOfProperty("name") >= 0
)
for transition in ("hidden", "disabled", "setting", "expiry"):
    pill.set_enabled(True)
    pill.show_state(PillState.ERROR, text=ERROR_MICROPHONE_UNAVAILABLE)
    app.processEvents()
    assert pill.visible
    assert presentation.property("label") == ERROR_MICROPHONE_UNAVAILABLE
    if transition == "setting":
        pill.set_enabled(False)
    elif transition == "expiry":
        pill._expire(pill._timer_generation)
    else:
        pill.show_state(PillState(transition))
    assert root.property("label") == ""
    assert presentation.property("label") == ""
    assert not pill.visible
for state in ("hidden", "disabled"):
    root.setProperty("avState", "error")
    root.setProperty("label", ERROR_MICROPHONE_UNAVAILABLE)
    assert presentation.property("label") == ERROR_MICROPHONE_UNAVAILABLE
    root.setProperty("avState", state)
    assert presentation.property("label") == ""
pill.hide()
sip.delete(pill)
sip.delete(view)
"""
    result = subprocess.run(
        [sys.executable, "-c", source],
        cwd=ROOT,
        env={
            **os.environ,
            "PYTHONPATH": str(ROOT / "src"),
            "QT_QPA_PLATFORM": "offscreen",
            "QT_QUICK_BACKEND": "software",
        },
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def _qualified_pill_name(node: ast.AST, aliases: dict[str, str]) -> str:
    if isinstance(node, ast.Name):
        return aliases.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        return f"{_qualified_pill_name(node.value, aliases)}.{node.attr}"
    return ""


def _pill_text_violations(source: str, *, package: str = "astra_voice") -> list[str]:
    """Гейт для всех будущих вызовов show_state, включая app.py и псевдонимы.

    Разрешены строковые литералы и импорты именно из реестра pill, без перезаписи
    или затенения. Любая **распаковка может скрывать text, поэтому запрещена.
    """
    tree = ast.parse(source)
    nodes = list(ast.walk(tree))
    aliases: dict[str, str] = {}
    bindings = Counter(
        node.id
        for node in nodes
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del))
    )
    bindings.update(node.arg for node in nodes if isinstance(node, ast.arg))
    bindings.update(
        node.name
        for node in nodes
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    )
    for node in nodes:
        if isinstance(node, ast.ImportFrom):
            imported_module = node.module or ""
            if node.level:
                imported_module = resolve_name("." * node.level + imported_module, package)
            for item in node.names:
                name = item.asname or item.name
                aliases[name] = f"{imported_module}.{item.name}"
                bindings[name] += 1
        elif isinstance(node, ast.Import):
            for item in node.names:
                name = item.asname or item.name.split(".")[0]
                aliases[name] = item.name if item.asname else name
                bindings[name] += 1
    # send = pill.show_state; another = send — такие вызовы тоже проверяются.
    for _ in nodes:
        changed = False
        for node in nodes:
            if not isinstance(node, (ast.Assign, ast.AnnAssign)) or node.value is None:
                continue
            name = _qualified_pill_name(node.value, aliases)
            if name.rsplit(".", 1)[-1] != "show_state":
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name) and target.id not in aliases:
                    aliases[target.id] = name
                    changed = True
        if not changed:
            break

    registry_names = {
        f"astra_voice.ui.pill.{name}"
        for name, value in vars(module).items()
        if name.isupper() and isinstance(value, str) and value in ERROR_REASONS | CLIPBOARD_REASONS
    }
    overwritten = {
        _qualified_pill_name(node, aliases)
        for node in nodes
        if isinstance(node, ast.Attribute) and isinstance(node.ctx, (ast.Store, ast.Del))
    }
    problems: list[str] = []
    for node in nodes:
        if not isinstance(node, ast.Call):
            continue
        if _qualified_pill_name(node.func, aliases).rsplit(".", 1)[-1] != "show_state":
            continue
        for keyword in node.keywords:
            if keyword.arg is None:
                problems.append(f"{node.lineno}: распаковка может скрывать text")
            elif keyword.arg == "text":
                value = keyword.value
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    continue
                name = _qualified_pill_name(value, aliases)
                base = value
                while isinstance(base, ast.Attribute):
                    base = base.value
                if (
                    name in registry_names - overwritten
                    and isinstance(base, ast.Name)
                    and base.id in aliases
                    and bindings[base.id] == 1
                ):
                    continue
                problems.append(f"{node.lineno}: text не литерал и не константа реестра pill")
    return problems


def test_project_pill_text_contains_no_dictation() -> None:
    paths = sorted((ROOT / "src/astra_voice").rglob("*.py"))
    assert ROOT / "src/astra_voice/app.py" in paths
    problems = [
        f"{path.relative_to(ROOT)}:{problem}"
        for path in paths
        for problem in _pill_text_violations(
            path.read_text(encoding="utf-8"),
            package=".".join(path.parent.relative_to(ROOT / "src").parts),
        )
    ]
    assert not problems, "\n".join(problems)


@pytest.mark.parametrize(
    "source",
    [
        "pill.show_state(state, text=text)",
        'pill.show_state(state, text=f"Ошибка: {text}")',
        'pill.show_state(state, text="Ошибка: {}".format(text))',
        'pill.show_state(state, text="Ошибка: " + text)',
        'pill.show_state(state, text="Ошибка: %s" % text)',
        'pill.show_state(state, text="Ошибка: " + "ещё")',
        "pill.show_state(state, text=None)",
        "pill.show_state(state, **kwargs)",
        "send = pill.show_state\nsend(state, text=text)",
        "send = pill.show_state\nother = send\nother(state, text=text)",
        'REASON = "Ошибка"\npill.show_state(state, text=REASON)',
        "from foreign import ERROR_BUFFER_CLEARED\n"
        "pill.show_state(state, text=ERROR_BUFFER_CLEARED)",
        "from astra_voice.ui.pill import ERROR_REASONS\n"
        "pill.show_state(state, text=next(iter(ERROR_REASONS)))",
        "from astra_voice.ui.pill import ERROR_BUFFER_CLEARED as REASON\n"
        "REASON = text\npill.show_state(state, text=REASON)",
        "from astra_voice.ui.pill import ERROR_BUFFER_CLEARED as REASON\n"
        "def display(REASON):\n    pill.show_state(state, text=REASON)",
        "import astra_voice.ui.pill as reasons\n"
        "reasons.ERROR_BUFFER_CLEARED = text\n"
        "pill.show_state(state, text=reasons.ERROR_BUFFER_CLEARED)",
        "from astra_voice.ui import pill as reasons\n"
        "def display(reasons):\n    pill.show_state(state, text=reasons.ERROR_BUFFER_CLEARED)",
        "from astra_voice.ui.pill import ERROR_BUFFER_CLEARED as REASON\n"
        "from foreign import REASON\npill.show_state(state, text=REASON)",
        "import astra_voice.ui.pill as reasons\n"
        "def reasons():\n    pass\npill.show_state(state, text=reasons.ERROR_BUFFER_CLEARED)",
    ],
)
@pytest.mark.parametrize("clipboard", [False, True])
def test_pill_ast_rejects_dynamic_text(source: str, clipboard: bool) -> None:
    if clipboard:
        source = source.replace("ERROR_BUFFER_CLEARED", "CLIPBOARD_WINDOW_CHANGED").replace(
            "ERROR_REASONS", "CLIPBOARD_REASONS"
        )
    assert _pill_text_violations(source)


@pytest.mark.parametrize(
    "source",
    [
        "pill.show_state(state)",
        "pill.show_state(state, level=level)",
        'pill.show_state(state, text="Буфер очищен")',
        'show = pill.show_state\nshow(state, text="Буфер очищен")',
        "from astra_voice.ui.pill import ERROR_BUFFER_CLEARED\n"
        "pill.show_state(state, text=ERROR_BUFFER_CLEARED)",
        "from .ui.pill import ERROR_BUFFER_CLEARED as REASON\npill.show_state(state, text=REASON)",
        "import astra_voice.ui.pill as reasons\n"
        "pill.show_state(state, text=reasons.ERROR_BUFFER_CLEARED)",
        "from astra_voice.ui import pill as reasons\n"
        "pill.show_state(state, text=reasons.ERROR_BUFFER_CLEARED)",
        "import astra_voice.ui.pill\n"
        "pill.show_state(state, text=astra_voice.ui.pill.ERROR_BUFFER_CLEARED)",
    ],
)
@pytest.mark.parametrize("clipboard", [False, True])
def test_pill_ast_accepts_literals_and_registry_constants(source: str, clipboard: bool) -> None:
    if clipboard:
        source = source.replace("ERROR_BUFFER_CLEARED", "CLIPBOARD_WINDOW_CHANGED")
    assert not _pill_text_violations(source)
