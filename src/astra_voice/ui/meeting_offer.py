"""GUI-side lifetime of the meeting offer (W1)."""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, Protocol

from astra_voice.core.command_mode import SessionSnapshot
from astra_voice.core.meeting_session import ERROR_TEXT

log = logging.getLogger(__name__)

_FRESH_MS = 1_000
OFFER_GUARD_MS = 500
_DISPLAY_MS = 60_000
_UNLOCK_QUERY_MS = 5_000
_INHERIT_MS = 15_000
_START_TIMEOUT_MS = 6_000
_KINDS = {"dion": "call-app", "browser": "call-browser"}

Outcome = Literal["shown", "refused", "accepted", "lock_closed"]
Origin = Literal["offer", "card"]


class OfferView(Protocol):
    def show(self, kind: str, seconds: int) -> None:
        """Show synchronously. The controller starts both clocks on return."""
        ...

    def update_remaining(self, seconds: int) -> None: ...

    def close(self) -> None: ...

    def starting(self) -> None: ...

    def start_failed(self, text: str) -> None: ...


@dataclass(repr=False)
class _Call:
    call_id: str
    app_class: str
    measured_ms: int
    origin: Origin
    epochs: tuple[int, int]
    received_tick: int
    deferred: Literal["expiry", "shown"] | None


@dataclass(repr=False)
class _Memory:
    state: Outcome
    call_ids: set[str]
    role_serial: int
    origin: Origin
    deadline: int | None = None


@dataclass(repr=False)
class _StartSlot:
    call: _Call
    role_serial: int
    pressed_at_ms: int
    deadline: int | None
    failed: bool = False


def _epochs(snapshot: SessionSnapshot) -> tuple[int, int]:
    return snapshot.blocked_epoch, snapshot.sleep_epoch


class OfferController:
    def __init__(
        self,
        *,
        clock: Callable[[], float],
        on_visibility_changed: Callable[[], None],
        on_start: Callable[[str, str], None],
        on_open_meetings: Callable[[], None] = lambda: None,
        on_start_timeout: Callable[[], None] = lambda: None,
    ) -> None:
        self._clock = clock
        self._visibility_changed = on_visibility_changed
        self.startRequested = on_start
        self._open_meetings = on_open_meetings
        self._start_timeout = on_start_timeout
        self._view: OfferView | None = None
        self._tick_number = 0
        self._role_serial: int | None = None
        self._detector_started = False
        self._calls: dict[str, str] = {}
        self._ended: set[str] = set()
        self._memory: dict[str, _Memory] = {}
        self._pending: _Call | None = None
        self._visible: _Call | None = None
        self._starting: _StartSlot | None = None
        self._shown_at: int | None = None
        self._pressed: tuple[str, int] | None = None
        self._guarded = False
        self._seconds = 0
        self._session: tuple[SessionSnapshot, int | None] | None = None
        self._session_ready = False
        self._query_at: int | None = None

    def now_ms(self) -> int:
        return int(self._clock() * 1000)

    def bind(self, view: OfferView | None) -> None:
        self.start_closed()
        if self._view is view:
            return
        self.suspend()
        self._view = view

    @property
    def bound(self) -> bool:
        return self._view is not None

    @property
    def visible(self) -> bool:
        return self._visible is not None

    @property
    def starting_slot(self) -> bool:
        return self._starting is not None

    @property
    def start_pressed_at_ms(self) -> int:
        assert self._starting is not None
        return self._starting.pressed_at_ms

    @property
    def state(self) -> str:
        if self._starting is not None:
            return "start-failed" if self._starting.failed else "starting"
        if self._visible is not None:
            return "shown_guard" if self._guarded else "shown"
        if self._pending is not None and self._pending.deferred is None:
            return "pending"
        return "idle"

    @property
    def presentation(self) -> tuple[str, int]:
        kind = _KINDS[self._visible.app_class] if self._visible is not None else ""
        return kind, self._seconds

    def new_role(self, serial: int) -> None:
        if self._role_serial is not None:
            self.role_ended(self._role_serial)
        self._role_serial = serial
        self._detector_started = False

    def role_ended(self, serial: int) -> None:
        if serial != self._role_serial:
            return
        self.detector_unavailable()
        ended_at = self.now_ms()
        self._role_serial = None
        self._calls.clear()
        self._ended.clear()
        for entry in self._memory.values():
            entry.call_ids.clear()
            if entry.deadline is not None and entry.deadline > ended_at:
                entry.deadline = None
        self.suspend()

    def detector_started(self) -> None:
        self._detector_started = True
        deadline = self.now_ms() + _INHERIT_MS
        for entry in self._memory.values():
            if entry.role_serial != self._role_serial and entry.deadline is None:
                entry.deadline = deadline

    def detector_unavailable(self) -> None:
        if not self._detector_started:
            self._memory = {
                key: entry
                for key, entry in self._memory.items()
                if entry.role_serial == self._role_serial
            }

    def _expire_memory(self, now: int) -> None:
        for app_class, entry in tuple(self._memory.items()):
            if entry.deadline is not None and now >= entry.deadline:
                del self._memory[app_class]

    def _card_allowed(self, app_class: str) -> bool:
        entry = self._memory.get(app_class)
        return entry is None or entry.state == "lock_closed"

    def _has_card_class(self) -> bool:
        return any(self._card_allowed(app_class) for app_class in _KINDS)

    def _suppressed(self, call: _Call) -> bool:
        entry = self._memory.get(call.app_class)
        if entry is None:
            return False
        if call.origin == "card":
            suppressed = not self._card_allowed(call.app_class)
        elif entry.role_serial != self._role_serial:
            assert self._role_serial is not None
            suppressed = True
            entry.role_serial = self._role_serial
            entry.origin = "offer"
            entry.deadline = None
        else:
            suppressed = (
                call.call_id in entry.call_ids
                or entry.state == "lock_closed"
                or entry.origin == "card"
            )
        if suppressed:
            entry.call_ids.add(call.call_id)
        return suppressed

    def _fresh(self, measured_ms: int, now: int) -> bool:
        return measured_ms > 0 and 0 <= now - measured_ms <= _FRESH_MS

    def track_call(self, call_id: str, app_class: str) -> None:
        if app_class in _KINDS and self._role_serial is not None:
            self._calls[call_id] = app_class

    def receive(
        self,
        *,
        call_id: str,
        app_class: str,
        measured_ms: int,
        origin: Origin,
        eligible: bool,
        session: SessionSnapshot,
    ) -> None:
        if origin == "card":
            self._query_at = None
        if app_class not in _KINDS or self._role_serial is None:
            return
        self.track_call(call_id, app_class)
        now = self.now_ms()
        if not (
            self.bound
            and eligible
            and session.allowed
            and self._pending is None
            and not self.visible
            and not self.starting_slot
            and call_id not in self._ended
            and self._fresh(measured_ms, now)
        ):
            return
        entry = self._memory.get(app_class)
        # Preserve the event at the deadline without consuming or reviving the old entry.
        waits_for_expiry = (
            entry is not None and entry.deadline is not None and now >= entry.deadline
        )
        call = _Call(
            call_id,
            app_class,
            measured_ms,
            origin,
            _epochs(session),
            self._tick_number,
            "expiry" if waits_for_expiry else None,
        )
        if call.deferred is not None or not self._suppressed(call):
            self._pending = call

    def _remember(self, call: _Call, outcome: Outcome, role_serial: int | None = None) -> None:
        serial = self._role_serial if role_serial is None else role_serial
        assert serial is not None
        entry = self._memory.get(call.app_class)
        ids = entry.call_ids if entry is not None else set()
        ids.add(call.call_id)
        deadline = (
            self.now_ms() + _INHERIT_MS
            if serial != self._role_serial and self._detector_started
            else None
        )
        self._memory[call.app_class] = _Memory(outcome, ids, serial, call.origin, deadline)

    def _take_visible(self) -> _Call | None:
        call, self._visible = self._visible, None
        if call is None:
            return None
        self._shown_at = None
        self._pressed = None
        self._guarded = False
        self._seconds = 0
        return call

    def _close(self, outcome: Outcome | None = None) -> None:
        call = self._take_visible()
        if call is None:
            return
        if outcome is not None:
            self._remember(call, outcome)
        assert self._view is not None
        self._view.close()
        self._visibility_changed()

    def start_resolved(self) -> None:
        if self._starting is not None:
            self._starting.deadline = None

    def start_done(self) -> None:
        self.start_closed()

    def start_failed(self, text: str) -> None:
        if self._starting is None or self._starting.failed:
            return
        self.start_resolved()
        self._starting.failed = True
        assert self._view is not None
        self._view.start_failed(text)
        self._visibility_changed()

    def start_aborted(self) -> None:
        if self._starting is not None:
            self._remember(self._starting.call, "lock_closed", self._starting.role_serial)
            self.start_closed()

    def start_closed(self) -> None:
        if self._starting is None:
            return
        self._starting = None
        assert self._view is not None
        self._view.close()
        self._visibility_changed()

    def suspend(self, *, for_capture: bool = False, clear_memory: bool = False) -> None:
        pending, self._pending = self._pending, None
        self._query_at = None
        if for_capture and pending is not None:
            if pending.deferred is None:
                self._remember(pending, "shown")
            else:
                pending.deferred = "shown"
                self._pending = pending
        if clear_memory:
            self._memory.clear()
            self._calls.clear()
            self._ended.clear()
            self.start_closed()
        self._close("shown" if for_capture else None)

    def call_ended(self, call_id: str, reason: str) -> bool:
        """Apply a known end event. Return True when it invalidates CARD."""
        app_class = self._calls.pop(call_id, None)
        self._ended.add(call_id)
        if app_class is None:
            return False
        pending, visible = self._pending, self._visible
        if reason in {"mic_released", "app_gone"}:
            if pending is not None and pending.app_class == app_class:
                self._pending = None
            self._memory.pop(app_class, None)
            if visible is not None and visible.app_class == app_class:
                self._close()
        elif reason in {"server_changed", "detector_off"}:
            if reason == "server_changed":
                entry = self._memory.get(app_class)
                if entry is not None:
                    entry.call_ids.discard(call_id)
                    if entry.state == "lock_closed":
                        entry.state = "shown"
            self.suspend()
            return reason == "server_changed"
        else:  # evicted
            if pending is not None and pending.call_id == call_id:
                self._pending = None
            if visible is not None and visible.call_id == call_id:
                self._close("shown")
            entry = self._memory.get(app_class)
            if entry is not None:
                entry.call_ids.discard(call_id)
        return False

    def reset_session(self) -> None:
        self._session = None
        self._session_ready = False
        self._query_at = None

    def session_changed(
        self, snapshot: SessionSnapshot, *, can_query: bool, revision: int | None = None
    ) -> bool:
        """Track screen transitions. Return whether an outstanding CARD is invalid."""
        previous, previous_revision = self._session or (None, None)
        if revision is not None and previous_revision is not None and revision <= previous_revision:
            return False
        ready = self._session_ready
        self._session_ready |= snapshot.known
        self._session = snapshot, revision
        epochs_changed = previous is not None and _epochs(previous) != _epochs(snapshot)
        missed_block = (
            epochs_changed
            and revision is not None
            and previous_revision is not None
            and revision > previous_revision + 1
        )
        if epochs_changed or not snapshot.allowed:
            self._query_at = None
        if self._starting is not None and (
            not snapshot.allowed
            or epochs_changed
            or self._starting.call.epochs != _epochs(snapshot)
        ):
            self.start_aborted()
        if not snapshot.allowed or (
            self._visible is not None and self._visible.epochs != _epochs(snapshot)
        ):
            if self._pending is not None and self._pending.deferred != "shown":
                self._pending = None
            self._close("lock_closed")
        if (
            can_query
            and ready
            and snapshot.allowed
            and previous is not None
            and (not previous.allowed or missed_block)
            and not self.visible
            and not self.starting_slot
            and self._pending is None
            and self._has_card_class()
        ):
            self._query_at = self.now_ms() + _UNLOCK_QUERY_MS
        return epochs_changed or not snapshot.allowed

    def begin_tick(self) -> None:
        self._tick_number += 1

    def tick(self, *, eligible: bool, session: SessionSnapshot) -> bool:
        """Advance W1 after the IPC pump. Return True once when CARD is due."""
        now = self.now_ms()
        self._expire_memory(now)
        if self._starting is not None:
            self._ended.clear()
            deadline = self._starting.deadline
            if deadline is not None and now >= deadline:
                self.start_failed(ERROR_TEXT["audio-unavailable"])
                self._start_timeout()
            return False
        call = self._pending
        if call is not None and call.received_tick < self._tick_number:
            self._pending = None
            if call.deferred == "shown":
                self._remember(call, "shown")
            elif (
                eligible
                and session.allowed
                and call.epochs == _epochs(session)
                and self._fresh(call.measured_ms, now)
                and (call.deferred is None or not self._suppressed(call))
            ):
                self._show(call)
        elif self._shown_at is not None:
            elapsed = now - self._shown_at
            self._guarded = elapsed < OFFER_GUARD_MS
            remaining = _DISPLAY_MS - elapsed
            if self._apply_press(eligible and session.allowed):
                self._ended.clear()
                return False
            if remaining <= 0:
                self._close("refused")
            else:
                seconds = (remaining + 999) // 1000
                if seconds != self._seconds:
                    self._seconds = seconds
                    assert self._view is not None
                    self._view.update_remaining(seconds)
        self._ended.clear()
        if self._query_at is None or now < self._query_at:
            return False
        self._query_at = None
        return (
            eligible
            and session.allowed
            and not self.visible
            and self._pending is None
            and self._has_card_class()
        )

    def _show(self, call: _Call) -> None:
        assert self._view is not None
        self._visible = call
        self._guarded = True
        self._seconds = _DISPLAY_MS // 1000
        self._query_at = None
        self._remember(call, "shown")
        try:
            self._view.show(_KINDS[call.app_class], self._seconds)
        except Exception:
            log.warning("meeting-offer-show-failed")
            if self._visible is call:
                self._visible = None
                self._guarded = False
                self._seconds = 0
                with contextlib.suppress(Exception):
                    self._view.close()
            return
        if self._visible is call:
            self._shown_at = self.now_ms()
            self._visibility_changed()

    def press(self, action: str, pressed_at_ms: int) -> bool:
        """Queue the first admitted press for the next tick."""
        if self._starting is not None:
            if not self._starting.failed or action not in {"close", "open_meetings"}:
                return False
            self.start_closed()
            if action == "open_meetings":
                self._open_meetings()
            return True
        if self._pressed is not None or action not in {"start", "refuse", "close"}:
            return False
        if not self._press_in_window(pressed_at_ms, self.now_ms()):
            return False
        self._pressed = action, pressed_at_ms
        return True

    def _press_in_window(self, pressed_at_ms: int, observed_at_ms: int) -> bool:
        shown_at = self._shown_at
        return (
            shown_at is not None
            and shown_at + OFFER_GUARD_MS
            <= pressed_at_ms
            <= observed_at_ms
            < shown_at + _DISPLAY_MS
        )

    def _apply_press(self, allowed: bool) -> bool:
        pressed, self._pressed = self._pressed, None
        if pressed is None or not allowed:
            return False
        action, pressed_at_ms = pressed
        if not self._press_in_window(pressed_at_ms, pressed_at_ms):
            return False
        if action == "start":
            call = self._take_visible()
            assert call is not None and self._role_serial is not None and self._view is not None
            self._remember(call, "accepted", self._role_serial)
            self._starting = _StartSlot(
                call, self._role_serial, pressed_at_ms, pressed_at_ms + _START_TIMEOUT_MS
            )
            self._view.starting()
            self.startRequested(call.call_id, call.app_class)
        else:
            self._close("refused")
        return True

    def dismiss(self) -> None:
        """The window manager closed W1. The in-window × uses press()."""
        if self._starting is not None:
            if self._starting.failed:
                self.start_closed()
            return
        self._close("refused")
