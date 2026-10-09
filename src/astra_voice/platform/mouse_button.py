"""Optional command PTT by mouse: one X11 connection, no Qt, microphone or transport.

All methods and callbacks run in the owner's event loop. The owner services
fileno() and deadline, and calls cancel() on every admission/lifecycle loss.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any, Literal, Protocol

from astra_voice.core.constants import RECORD_LIMIT_S
from astra_voice.core.settings import is_valid_command_mouse_button
from astra_voice.platform.x11 import X11Display

log = logging.getLogger(__name__)
HOLD_THRESHOLD_S = 0.3
MouseResultCode = Literal["ok", "busy", "bad-button", "not-grabbed", "held", "blocked", "closed"]


def qt_button_to_logical(button: int) -> int | None:
    """Translate one Qt MouseButton value, never a Qt MouseButtons bitset."""
    if type(button) is not int:
        return None
    if button == 4:  # Qt.MiddleButton
        return 2
    if button > 0 and button & (button - 1) == 0:
        bit = button.bit_length() - 1
        if 3 <= bit <= 26:
            return bit + 5
    return None


@dataclass(frozen=True)
class MouseGrabResult:
    code: MouseResultCode

    @property
    def ok(self) -> bool:
        return self.code == "ok"


@dataclass(frozen=True)
class MouseButtonEvent:
    kind: Literal["press", "release"]
    button: int
    time: int = 0  # X server time, not the monotonic hold deadline.
    modifiers: int = 0  # Semantic modifiers, excluding locks and pointer buttons.


@dataclass(frozen=True)
class MouseResetEvent:
    reason: str


class MouseBackend(Protocol):
    def grab(self, button: int) -> MouseGrabResult: ...
    def cancel(self) -> None: ...
    def poll_events(self) -> Sequence[MouseButtonEvent | MouseResetEvent]: ...
    def button_down(self, button: int) -> bool | None: ...
    def fileno(self) -> int: ...
    def close(self) -> None: ...


def _query_pointer_buttons(root: Any, deviceid: int) -> bytes | None:
    """XIQueryPointer on the existing connection; python-xlib lacks this request."""
    from Xlib.ext import xinput
    from Xlib.protocol import rq

    class XIQueryPointer(rq.ReplyRequest):
        # XI2proto.h: opcode 40, 12-byte request, 56-byte fixed reply.
        _request = rq.Struct(
            rq.Card8("opcode"),
            rq.Opcode(40),
            rq.RequestLength(),
            rq.Window("window"),
            rq.Card16("deviceid"),
            rq.Pad(2),
        )
        _reply = rq.Struct(
            rq.ReplyCode(),
            rq.Pad(1),
            rq.Card16("sequence_number"),
            rq.ReplyLength(),
            rq.Pad(24),  # root, child and four FP1616 coordinates
            rq.Bool("same_screen"),
            rq.Pad(1),
            rq.Card16("buttons_len"),  # mask length in units of four bytes
            rq.Pad(20),  # modifier and group state
            rq.Binary("buttons"),
        )

    reply = XIQueryPointer(
        display=root.display,
        opcode=root.display.get_extension_major(xinput.extname),
        window=root,
        deviceid=deviceid,
    )
    buttons = reply.buttons
    if (
        not reply.same_screen
        or not isinstance(buttons, bytes)
        or not buttons
        or len(buttons) != 4 * reply.buttons_len
        or buttons[0] & 1  # protocol bit zero is reserved
    ):
        return None
    return buttons


class X11MouseBackend:
    """Owns an exclusive display; do not share it with keyboard/capture readers.

    Core XGrabButton supplies logical press/release events. XIQueryPointer is
    a read-only logical state query for buttons >5, not a raw-event subscription.
    """

    def __init__(self, display: X11Display | None = None) -> None:
        self._x = display if display is not None else X11Display()
        self._button: int | None = None
        self._masks: tuple[int, ...] = ()
        self._closed = False

    def button_down(self, button: int) -> bool | None:
        if not is_valid_command_mouse_button(button) or self._x.d is None:
            return None
        try:
            if button == 2:
                from Xlib import X

                pointer = self._x.root.query_pointer()
                return bool(pointer.mask & X.Button2Mask) if pointer.same_screen else None
            from Xlib.ext import xinput

            conn = self._x.d
            if not conn.has_extension("XInputExtension"):
                return None
            version = conn.xinput_query_version()
            if version.major_version < 2:
                return None
            devices = conn.xinput_query_device(xinput.AllMasterDevices).devices
            found = False
            down = False
            for device in devices:
                if device.use != xinput.MasterPointer or not device.enabled:
                    continue
                # QueryDevice is for enumeration only: some X servers expose
                # physical ButtonClass state even when core events are remapped.
                buttons = _query_pointer_buttons(self._x.root, device.deviceid)
                if buttons is None or button // 8 >= len(buttons):
                    return None
                down |= bool(buttons[button // 8] & (1 << (button % 8)))
                found = True
            return down if found else None
        except Exception as exc:
            log.debug("состояние кнопки мыши недоступно: %s", type(exc).__name__)
            return None

    def grab(self, button: int) -> MouseGrabResult:
        self.cancel()
        if self._closed:
            return MouseGrabResult("closed")
        if not is_valid_command_mouse_button(button):
            return MouseGrabResult("bad-button")
        if not self._x.open():
            return MouseGrabResult("not-grabbed")
        try:
            from Xlib import X, error

            conn = self._x.d
            before = self._x._error_count
            self._x.lock_masks = self._x._read_lock_masks()
            conn.sync()
            if self._x._error_count != before:
                return MouseGrabResult("not-grabbed")
            held = self.button_down(button)
            if held is not False:
                return MouseGrabResult("held" if held else "not-grabbed")
            self._button = button
            self._masks = tuple(self._x.mask_variants(0))
            for mask in self._masks:
                if not 0 <= mask <= 255:
                    raise ValueError("invalid modifier mask")
                catcher = error.CatchError(error.BadAccess)
                self._x.root.grab_button(
                    button,
                    mask,
                    False,
                    X.ButtonPressMask | X.ButtonReleaseMask,
                    X.GrabModeAsync,
                    X.GrabModeAsync,
                    X.NONE,
                    X.NONE,
                    onerror=catcher,
                )
                conn.sync()
                if catcher.get_error() is not None:
                    self.cancel()
                    return MouseGrabResult("busy")
                if self._x._error_count != before:
                    self.cancel()
                    return MouseGrabResult("not-grabbed")
            # Reject a press racing registration too: no held-before-arm gesture.
            held = self.button_down(button)
            if held is not False:
                self.cancel()
                return MouseGrabResult("held" if held else "not-grabbed")
            return MouseGrabResult("ok")
        except Exception as exc:
            log.debug("захват кнопки мыши не выполнен: %s", type(exc).__name__)
            self.cancel()
            return MouseGrabResult("not-grabbed")

    def cancel(self) -> None:
        """Remove passive registrations AND an already activated pointer grab."""
        button, self._button = self._button, None
        masks, self._masks = self._masks, ()
        conn = self._x.d
        if conn is None:
            return
        failed = False
        before = self._x._error_count
        if button is not None:
            for mask in masks:
                try:
                    self._x.root.ungrab_button(button, mask)
                except Exception:
                    failed = True
        try:
            conn.ungrab_pointer(0)  # CurrentTime, even with no passive registrations left.
            conn.sync()
            failed |= self._x._error_count != before
            while conn.pending_events():
                conn.next_event()
        except Exception:
            failed = True
        if failed:
            # Never keep a possibly live grab after a failed explicit cancellation.
            self._x.close()

    def poll_events(self) -> list[MouseButtonEvent | MouseResetEvent]:
        if self._x.d is None:
            return [MouseResetEvent("connection-lost")] if self._button is not None else []
        try:
            from Xlib import X

            events: list[MouseButtonEvent | MouseResetEvent] = []
            while self._x.pending_events():
                event = self._x.next_event()
                if event.type == X.MappingNotify:
                    # A changed pointer/keyboard map invalidates the current gesture.
                    # Next explicit arm re-reads lock masks; no automatic continuation.
                    self.cancel()
                    return [MouseResetEvent("mapping-changed")]
                if event.type in (X.ButtonPress, X.ButtonRelease) and not event.send_event:
                    events.append(
                        MouseButtonEvent(
                            "press" if event.type == X.ButtonPress else "release",
                            int(event.detail),
                            int(event.time),
                            self._x.semantic_modifiers(int(event.state)),
                        )
                    )
            return events
        except Exception:
            self.cancel()
            return [MouseResetEvent("connection-lost")]

    def fileno(self) -> int:
        return self._x.fileno()

    def close(self) -> None:
        self._closed = True
        self.cancel()
        self._x.close()


class MouseHoldState(Enum):
    IDLE = "idle"
    PENDING = "pending"
    RECORDING = "recording"
    PROCESSING = "processing"


class MouseHoldManager:
    """Strict hold FSM; default admission denies all commands.

    on_pending(generation) reserves the shared runtime input owner and schedules
    tick(generation=...). on_state(state, reason) reports recording/processing
    and terminal idle. No callback directly owns a microphone or Command1.
    """

    def __init__(
        self,
        backend: MouseBackend | None = None,
        *,
        allowed: Callable[[], bool] = lambda: False,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.backend = backend if backend is not None else X11MouseBackend()
        self.allowed = allowed
        self._clock = clock
        self.on_pending: Callable[[int], None] | None = None
        self.on_state: Callable[[MouseHoldState, str], None] | None = None
        self.state = MouseHoldState.IDLE
        self.last_result = MouseGrabResult("not-grabbed")
        self.generation = 0
        self.deadline: float | None = None
        self._button: int | None = None
        self._closed = False

    def _allowed(self) -> bool:
        try:
            return self.allowed() is True
        except Exception:
            return False

    def _emit(self, state: MouseHoldState, reason: str) -> None:
        self.state = state
        if self.on_state is not None:
            try:
                self.on_state(state, reason)
            except Exception:
                self.cancel("callback-error")
                raise

    def arm(self, button: int) -> MouseGrabResult:
        self.cancel("rearm")
        if self._closed:
            result = MouseGrabResult("closed")
        elif not is_valid_command_mouse_button(button):
            result = MouseGrabResult("bad-button")
        elif not self._allowed():
            result = MouseGrabResult("blocked")
        else:
            result = self.backend.grab(button)
            if result.ok:
                self._button = button
        self.last_result = result
        return result

    def cancel(self, reason: str = "cancel") -> None:
        self.generation += 1
        was_active = self._button is not None or self.state != MouseHoldState.IDLE
        self._button = None
        self.deadline = None
        self.backend.cancel()
        self.last_result = MouseGrabResult("not-grabbed")
        self.state = MouseHoldState.IDLE
        if was_active:
            self._emit(MouseHoldState.IDLE, reason)

    def process_pending(self) -> None:
        generation = self.generation
        events = self.backend.poll_events()
        for event in events:
            if generation != self.generation:
                break
            if isinstance(event, MouseResetEvent):
                self.cancel(event.reason)
                break
            self._handle_event(event)

    def _handle_event(self, event: MouseButtonEvent) -> None:
        if self._button is None or event.button != self._button:
            return
        if not self._allowed():
            self.cancel("blocked")
            return
        if event.kind == "press" and self.state == MouseHoldState.IDLE:
            if event.modifiers:
                self.cancel("modified-press")
                return
            self.state = MouseHoldState.PENDING
            self.deadline = self._clock() + HOLD_THRESHOLD_S
            if self.on_pending is not None:
                try:
                    self.on_pending(self.generation)
                except Exception:
                    self.cancel("callback-error")
                    raise
        elif event.kind == "release":
            if self.state == MouseHoldState.PENDING:
                self.cancel("tap")
            elif self.state == MouseHoldState.RECORDING:
                self._stop("release")

    def _stop(self, reason: str) -> None:
        self._button = None
        self.deadline = None
        self.backend.cancel()
        self._emit(MouseHoldState.PROCESSING, reason)

    def tick(self, *, generation: int | None = None) -> None:
        if generation is not None and generation != self.generation:
            return
        current = self.generation
        # An expired timer must not start recording over a queued physical release.
        self.process_pending()
        if current != self.generation or self._button is None:
            return
        if not self._allowed():
            self.cancel("blocked")
            return
        now = self._clock()
        if self.deadline is None or now < self.deadline:
            return
        if self.state == MouseHoldState.PENDING:
            if self.backend.button_down(self._button) is not True:
                self.cancel("hold-lost")
                return
            self.deadline = now + RECORD_LIMIT_S
            self._emit(MouseHoldState.RECORDING, "hold")
        elif self.state == MouseHoldState.RECORDING:
            self._stop("limit")

    def done(self, generation: int) -> None:
        if generation == self.generation and self.state == MouseHoldState.PROCESSING:
            self.cancel("done")

    def fileno(self) -> int:
        return self.backend.fileno()

    def close(self) -> None:
        self._closed = True
        self.cancel("closed")
        self.backend.close()
