"""Author unit tests: no X server, Qt, microphone, D-Bus or Command1 calls."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock, call

import pytest

from astra_voice.platform.mouse_button import (
    MouseButtonEvent,
    MouseGrabResult,
    MouseHoldManager,
    MouseHoldState,
    MouseResetEvent,
    X11MouseBackend,
)
from astra_voice.platform.x11 import X11Display

pytestmark = pytest.mark.unit


class Backend:
    def __init__(self) -> None:
        self.events: list[MouseButtonEvent | MouseResetEvent] = []
        self.held: bool | None = False
        self.grabbed: int | None = None
        self.cancellations = 0
        self.closed = False

    def grab(self, button: int) -> MouseGrabResult:
        if self.held is not False:
            return MouseGrabResult("held" if self.held else "not-grabbed")
        self.grabbed = button
        return MouseGrabResult("ok")

    def cancel(self) -> None:
        self.cancellations += 1
        self.grabbed = None
        self.events.clear()

    def poll_events(self) -> list[MouseButtonEvent | MouseResetEvent]:
        events, self.events = self.events, []
        return events

    def button_down(self, button: int) -> bool | None:
        return self.held

    def fileno(self) -> int:
        return -1

    def close(self) -> None:
        self.cancel()
        self.closed = True


class Rig:
    def __init__(self) -> None:
        self.backend = Backend()
        self.now = 0.0
        self.allowed = True
        self.manager = MouseHoldManager(
            self.backend, allowed=lambda: self.allowed, clock=lambda: self.now
        )
        self.states: list[tuple[MouseHoldState, str]] = []
        self.pending: list[int] = []
        self.manager.on_state = lambda state, reason: self.states.append((state, reason))
        self.manager.on_pending = self.pending.append
        assert self.manager.arm(2).ok

    def press(self) -> None:
        self.backend.held = True
        self.backend.events = [MouseButtonEvent("press", 2)]
        self.manager.process_pending()

    def record(self) -> None:
        self.press()
        self.now = 0.3
        self.manager.tick(generation=self.pending[-1])
        assert self.manager.state == MouseHoldState.RECORDING


@pytest.mark.parametrize("elapsed", [0.0, 0.299, 0.3, 2.0])
def test_release_in_queue_precedes_timer_even_when_event_loop_late(elapsed: float) -> None:
    r = Rig()
    r.press()
    r.now = elapsed
    r.backend.held = False
    r.backend.events = [MouseButtonEvent("release", 2)]
    r.manager.tick(generation=r.pending[-1])
    assert r.states == [(MouseHoldState.IDLE, "tap")]
    assert r.backend.grabbed is None


def test_exact_threshold_record_release_process_and_done_requires_rearm() -> None:
    r = Rig()
    r.press()
    r.now = 0.299
    r.manager.tick()
    assert not r.states
    r.now = 0.3
    r.manager.tick()
    assert r.states == [(MouseHoldState.RECORDING, "hold")]
    token = r.manager.generation
    r.backend.held = False
    r.backend.events = [MouseButtonEvent("release", 2)]
    r.manager.process_pending()
    assert r.states[-1] == (MouseHoldState.PROCESSING, "release")
    assert r.backend.grabbed is None
    r.manager.done(token - 1)
    assert r.manager.state == MouseHoldState.PROCESSING
    r.manager.done(token)
    assert r.states[-1] == (MouseHoldState.IDLE, "done")
    assert r.manager.arm(2).ok


def test_duplicate_press_does_not_restart_pending_deadline() -> None:
    r = Rig()
    r.press()
    deadline = r.manager.deadline
    r.now = 0.2
    r.press()
    assert r.manager.deadline == deadline
    assert len(r.pending) == 1


@pytest.mark.parametrize("phase", ["pending", "recording", "processing"])
@pytest.mark.parametrize(
    "reason", ["lock", "sleep", "unknown", "disabled", "capture", "owner-lost"]
)
def test_cancel_at_each_phase_requires_fresh_gesture(phase: str, reason: str) -> None:
    r = Rig()
    if phase == "pending":
        r.press()
    else:
        r.record()
        if phase == "processing":
            r.backend.events = [MouseButtonEvent("release", 2)]
            r.manager.process_pending()
    old = r.manager.generation
    r.manager.cancel(reason)
    assert r.manager.state == MouseHoldState.IDLE
    assert r.manager.deadline is None and r.backend.grabbed is None
    assert r.manager.arm(2).code == "held"
    r.backend.held = False  # Release delivered to another client, not this manager.
    assert r.manager.arm(2).ok
    r.manager.tick(generation=old)
    assert r.manager.state == MouseHoldState.IDLE
    r.press()
    r.now += 0.3
    r.manager.tick()
    assert r.manager.state.value == "recording"


@pytest.mark.parametrize("held", [False, None])
def test_missing_release_or_unknown_button_state_never_starts(held: bool | None) -> None:
    r = Rig()
    r.press()
    r.now = 1.0
    r.backend.held = held
    r.manager.tick()
    assert r.states == [(MouseHoldState.IDLE, "hold-lost")]


def test_default_and_changed_admission_are_fail_closed() -> None:
    assert MouseHoldManager(Backend()).arm(2).code == "blocked"
    r = Rig()
    r.press()
    r.allowed = False
    r.now = 1.0
    r.manager.tick()
    assert r.states == [(MouseHoldState.IDLE, "blocked")]
    assert r.backend.grabbed is None


def test_unknown_or_raising_admission_guard_cannot_record() -> None:
    r = Rig()
    r.manager.allowed = Mock(side_effect=RuntimeError("unavailable"))
    r.press()
    assert r.manager.state == MouseHoldState.IDLE
    assert r.backend.grabbed is None


def test_watchdog_stops_at_existing_record_limit_and_ungrabs_before_callback() -> None:
    r = Rig()
    r.record()
    assert r.manager.deadline == 120.3
    r.now = 120.3
    r.manager.on_state = lambda state, reason: (
        r.states.append((state, reason)) if r.backend.grabbed is None else pytest.fail("live grab")
    )
    r.manager.tick()
    assert r.states[-1] == (MouseHoldState.PROCESSING, "limit")
    assert r.manager.deadline is None


def test_mapping_loss_and_old_same_batch_press_cannot_restart() -> None:
    r = Rig()
    r.record()
    r.backend.held = False

    def rearm(state: MouseHoldState, reason: str) -> None:
        r.states.append((state, reason))
        if reason == "mapping-changed":
            assert r.manager.arm(2).ok

    r.manager.on_state = rearm
    r.backend.events = [MouseResetEvent("mapping-changed"), MouseButtonEvent("press", 2)]
    r.manager.process_pending()
    r.now += 1
    r.manager.tick()
    assert r.manager.state == MouseHoldState.IDLE
    assert len(r.pending) == 1


def test_pending_callback_cancel_rearm_drops_rest_of_old_batch() -> None:
    r = Rig()

    def reserve(token: int) -> None:
        r.pending.append(token)
        assert r.manager.arm(2).ok

    r.manager.on_pending = reserve
    r.backend.events = [MouseButtonEvent("press", 2), MouseButtonEvent("press", 2)]
    r.manager.process_pending()
    assert len(r.pending) == 1
    assert r.manager.state == MouseHoldState.IDLE


@pytest.mark.parametrize("stage", ["pending", "recording"])
def test_callback_exception_releases_resources(stage: str) -> None:
    r = Rig()
    failure = Mock(side_effect=RuntimeError("callback failed"))
    if stage == "pending":
        r.manager.on_pending = failure
        with pytest.raises(RuntimeError):
            r.press()
    else:
        r.press()
        r.manager.on_state = failure
        r.now = 0.3
        with pytest.raises(RuntimeError):
            r.manager.tick()
    assert r.backend.grabbed is None
    assert r.manager.state == MouseHoldState.IDLE


def test_close_is_terminal_and_only_selected_unmodified_press_can_start() -> None:
    r = Rig()
    r.backend.events = [MouseButtonEvent("press", 1), MouseButtonEvent("release", 2)]
    r.manager.process_pending()
    assert not r.pending and not r.states
    r.backend.events = [MouseButtonEvent("press", 2, modifiers=4)]
    r.manager.process_pending()
    assert not r.pending and r.backend.grabbed is None
    r.manager.close()
    assert r.backend.closed and r.manager.arm(2).code == "closed"


@pytest.fixture
def xrig(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    pytest.importorskip("Xlib")
    x = X11Display()
    conn = Mock()
    x.d = conn
    x.root = conn.root
    queue: list[Any] = []
    conn.pending_events.side_effect = lambda: len(queue)
    conn.next_event.side_effect = lambda: queue.pop(0)
    conn.root.query_pointer.return_value = SimpleNamespace(mask=0, same_screen=True)
    monkeypatch.setattr(
        x, "_read_lock_masks", lambda: {"Lock": 2, "Num_Lock": 16, "Scroll_Lock": 0}
    )
    pointer_query = Mock(return_value=None)
    monkeypatch.setattr("astra_voice.platform.mouse_button._query_pointer_buttons", pointer_query)
    return SimpleNamespace(
        x=x, conn=conn, queue=queue, backend=X11MouseBackend(x), pointer_query=pointer_query
    )


def test_exact_button_lock_only_grabs_and_forced_pointer_release(xrig: SimpleNamespace) -> None:
    from Xlib import X

    assert xrig.backend.grab(2).ok
    calls = xrig.conn.root.grab_button.call_args_list
    assert {item.args[1] for item in calls} == {0, 2, 16, 18}
    assert all(
        item.args == (2, item.args[1], False, 12, X.GrabModeAsync, X.GrabModeAsync, 0, 0)
        for item in calls
    )
    xrig.conn.reset_mock()
    xrig.backend.cancel()
    assert {item.args for item in xrig.conn.root.ungrab_button.call_args_list} == {
        (2, 0),
        (2, 2),
        (2, 16),
        (2, 18),
    }
    calls = xrig.conn.mock_calls
    release = calls.index(call.ungrab_pointer(0))
    assert calls[release + 1] == call.sync()
    xrig.backend.cancel()  # Active grabs must be released even without passive bookkeeping.
    assert xrig.conn.ungrab_pointer.call_count == 2


@pytest.mark.parametrize("button", [0, 1, 3, 4, 5, 6, 7, 32, True])
def test_invalid_buttons_never_register(xrig: SimpleNamespace, button: int) -> None:
    assert xrig.backend.grab(button).code == "bad-button"
    xrig.conn.root.grab_button.assert_not_called()


@pytest.mark.parametrize("when", ["before", "during"])
def test_held_before_or_during_registration_is_rejected(xrig: SimpleNamespace, when: str) -> None:
    xrig.conn.root.query_pointer.side_effect = (
        [SimpleNamespace(mask=512, same_screen=True)]
        if when == "before"
        else [
            SimpleNamespace(mask=0, same_screen=True),
            SimpleNamespace(mask=512, same_screen=True),
        ]
    )
    assert xrig.backend.grab(2).code == "held"
    assert xrig.backend._button is None
    if when == "before":
        xrig.conn.root.grab_button.assert_not_called()
    else:
        assert xrig.conn.root.ungrab_button.call_count == 4


@pytest.mark.parametrize("error", ["busy", "async", "exception"])
def test_partial_grab_failure_rolls_back_all_masks(
    xrig: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, error: str
) -> None:
    from Xlib import error as xerror

    if error == "busy":
        catcher = Mock()
        catcher.get_error.side_effect = [None, object()]
        monkeypatch.setattr(xerror, "CatchError", lambda *args: catcher)
    elif error == "async":
        xrig.conn.root.grab_button.side_effect = lambda *args, **kwargs: xrig.x._on_error(
            None, None
        )
    else:
        xrig.conn.root.grab_button.side_effect = RuntimeError("gone")
    assert xrig.backend.grab(2).code == ("busy" if error == "busy" else "not-grabbed")
    assert xrig.conn.root.ungrab_button.call_count == 4
    assert xrig.backend._button is None
    xrig.conn.ungrab_pointer.assert_called_with(0)


@pytest.mark.parametrize("failure", ["ungrab", "sync", "async"])
def test_failed_cancel_closes_own_connection(xrig: SimpleNamespace, failure: str) -> None:
    assert xrig.backend.grab(2).ok
    if failure == "ungrab":
        xrig.conn.root.ungrab_button.side_effect = RuntimeError("gone")
    elif failure == "sync":
        xrig.conn.sync.side_effect = RuntimeError("gone")
    else:
        xrig.conn.ungrab_pointer.side_effect = lambda *args: xrig.x._on_error(None, None)
    xrig.backend.cancel()
    assert xrig.x.d is None
    xrig.conn.close.assert_called_once()


def test_mapping_cancels_all_old_events_and_connection_error_reports_loss(
    xrig: SimpleNamespace,
) -> None:
    from Xlib import X

    assert xrig.backend.grab(2).ok
    press = SimpleNamespace(type=X.ButtonPress, detail=2, time=100, state=0, send_event=False)
    xrig.queue.extend([press, SimpleNamespace(type=X.MappingNotify), press])
    assert xrig.backend.poll_events() == [MouseResetEvent("mapping-changed")]
    assert not xrig.queue and xrig.backend._button is None
    xrig.conn.pending_events.side_effect = RuntimeError("lost")
    assert xrig.backend.poll_events() == [MouseResetEvent("connection-lost")]
    assert xrig.x.d is None


def test_normal_events_strip_locks_but_reject_send_event(xrig: SimpleNamespace) -> None:
    from Xlib import X

    assert xrig.backend.grab(2).ok
    xrig.queue.extend(
        [
            SimpleNamespace(type=X.ButtonPress, detail=2, time=1, state=18, send_event=False),
            SimpleNamespace(type=X.ButtonRelease, detail=2, time=2, state=530, send_event=False),
            SimpleNamespace(type=X.ButtonPress, detail=2, time=3, state=0, send_event=True),
        ]
    )
    assert xrig.backend.poll_events() == [
        MouseButtonEvent("press", 2, 1),
        MouseButtonEvent("release", 2, 2),
    ]


def xi2_button_info(xrig: SimpleNamespace, physical_buttons: int, held: int | None) -> Any:
    from Xlib.ext import xinput

    # QueryDevice may expose physical state despite logical core events.
    state = xinput.ButtonMask(
        0 if held is None else 1 << (min(8, physical_buttons) - 1), physical_buttons
    )
    xrig.pointer_query.return_value = (0 if held is None else 1 << held).to_bytes(32, "little")
    info = SimpleNamespace(type=xinput.ButtonClass, state=state)
    device = SimpleNamespace(deviceid=2, use=xinput.MasterPointer, enabled=True, classes=[info])
    xrig.conn.xinput_query_version.return_value = SimpleNamespace(major_version=2)
    xrig.conn.xinput_query_device.return_value = SimpleNamespace(devices=[device])
    return info


@pytest.mark.parametrize("physical_buttons", [1, 10, 31, 32, 33])
@pytest.mark.parametrize("button", [8, 9, 31])
def test_xi2_query_pointer_uses_logical_mask_not_device_state(
    xrig: SimpleNamespace, button: int, physical_buttons: int
) -> None:
    xi2_button_info(xrig, physical_buttons, button)
    assert xrig.backend.button_down(button) is True
    xrig.pointer_query.assert_called_once_with(xrig.x.root, 2)
    xrig.conn.root.query_pointer.assert_not_called()
    xi2_button_info(xrig, physical_buttons, None)
    assert xrig.backend.button_down(button) is False
    assert xrig.backend.grab(button).ok


@pytest.mark.parametrize("when", ["before", "during"])
def test_remapped_button_held_before_or_during_arm_is_rejected(
    xrig: SimpleNamespace, when: str
) -> None:
    xi2_button_info(xrig, 10, 31 if when == "before" else None)
    if when == "during":
        xrig.conn.root.grab_button.side_effect = lambda *a, **kw: xi2_button_info(xrig, 10, 31)
    assert xrig.backend.grab(31).code == "held"
    assert xrig.backend._button is None
    if when == "before":
        xrig.conn.root.grab_button.assert_not_called()
    else:
        assert xrig.conn.root.ungrab_button.call_count == 4


def test_remapped_button_starts_only_while_logically_held(xrig: SimpleNamespace) -> None:
    from Xlib import X

    now = 0.0
    manager = MouseHoldManager(xrig.backend, allowed=lambda: True, clock=lambda: now)
    xi2_button_info(xrig, 10, None)
    assert manager.arm(31).ok
    xi2_button_info(xrig, 10, 31)
    xrig.queue.append(
        SimpleNamespace(type=X.ButtonPress, detail=31, time=1, state=0, send_event=False)
    )
    manager.process_pending()
    assert manager.state == MouseHoldState.PENDING
    now = 0.3
    manager.tick()
    assert manager.state.value == "recording"
    manager.close()


@pytest.mark.parametrize("unavailable", [None, b"", b"\x00"])
def test_xi2_unknown_master_state_is_not_treated_as_released(
    xrig: SimpleNamespace, unavailable: bytes | None
) -> None:
    from Xlib.ext import xinput

    xi2_button_info(xrig, 10, None)
    second = SimpleNamespace(deviceid=7, use=xinput.MasterPointer, enabled=True, classes=[])
    xrig.conn.xinput_query_device.return_value.devices.append(second)
    xrig.pointer_query.side_effect = lambda root, deviceid: (
        bytes(32) if deviceid == 2 else unavailable
    )
    assert xrig.backend.button_down(31) is None
    assert xrig.backend.grab(31).code == "not-grabbed"
    xrig.conn.root.grab_button.assert_not_called()


def test_xi2_query_failure_cannot_arm(xrig: SimpleNamespace) -> None:
    xi2_button_info(xrig, 10, None)
    xrig.pointer_query.side_effect = RuntimeError("connection lost")
    assert xrig.backend.button_down(31) is None
    assert xrig.backend.grab(31).code == "not-grabbed"
    xrig.conn.root.grab_button.assert_not_called()


@pytest.mark.parametrize("held", [False, True])
def test_xi2_queries_every_enabled_master_and_ignores_slaves(
    xrig: SimpleNamespace, held: bool
) -> None:
    from Xlib.ext import xinput

    xi2_button_info(xrig, 10, None)
    xrig.conn.xinput_query_device.return_value.devices.extend(
        [
            SimpleNamespace(deviceid=7, use=xinput.MasterPointer, enabled=True),
            SimpleNamespace(deviceid=8, use=xinput.MasterPointer, enabled=False),
            SimpleNamespace(deviceid=9, use=xinput.SlavePointer, enabled=True),
        ]
    )
    xrig.pointer_query.side_effect = lambda root, deviceid: (
        (1 << 31).to_bytes(32, "little") if held and deviceid == 7 else bytes(32)
    )
    assert xrig.backend.button_down(31) is held
    assert xrig.pointer_query.call_args_list == [call(xrig.x.root, 2), call(xrig.x.root, 7)]


@pytest.mark.parametrize(
    ("same_screen", "mask_words", "buttons", "valid"),
    [
        (True, 8, (1 << 31).to_bytes(32, "little"), True),
        (True, 8, bytes(32), True),
        (False, 8, bytes(32), False),
        (True, 0, b"", False),
        (True, 8, bytes(4), False),
        (True, 1, bytes(32), False),
        (True, 8, b"\x01" + bytes(31), False),
    ],
)
def test_xi_query_pointer_decodes_wire_reply_and_rejects_unknown(
    monkeypatch: pytest.MonkeyPatch,
    same_screen: bool,
    mask_words: int,
    buttons: bytes,
    valid: bool,
) -> None:
    import struct

    from Xlib.protocol import rq

    from astra_voice.platform.mouse_button import _query_pointer_buttons

    root = Mock()
    root.display.get_extension_major.return_value = 131
    wire = (
        struct.pack("=BBHI", 1, 0, 17, 6 + len(buttons) // 4)
        + bytes(24)
        + struct.pack("=BBH", same_screen, 0, mask_words)
        + bytes(20)
        + buttons
    )

    def reply_init(request: Any, **kwargs: Any) -> None:
        assert kwargs == {"display": root.display, "opcode": 131, "window": root, "deviceid": 7}
        request._data, remaining = request._reply.parse_binary(wire, root.display)
        assert not remaining

    monkeypatch.setattr(rq.ReplyRequest, "__init__", reply_init)
    assert _query_pointer_buttons(root, 7) == (buttons if valid else None)


def test_extra_button_unknown_without_xi2_cannot_arm(xrig: SimpleNamespace) -> None:
    xrig.conn.has_extension.return_value = False
    assert xrig.backend.button_down(8) is None
    assert xrig.backend.grab(8).code == "not-grabbed"
    xrig.conn.root.grab_button.assert_not_called()
