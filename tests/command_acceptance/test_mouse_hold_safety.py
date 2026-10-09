"""Independent multi-gesture safety checks; no runtime, audio, bus or X server."""

from __future__ import annotations

import json
import random
from collections.abc import Sequence
from pathlib import Path

import pytest

from astra_voice.core.settings import load, save
from astra_voice.platform.mouse_button import (
    MouseButtonEvent,
    MouseGrabResult,
    MouseHoldManager,
    MouseHoldState,
    MouseResetEvent,
)

pytestmark = pytest.mark.unit


class Pointer:
    def __init__(self) -> None:
        self.held = False
        self.registered: int | None = None
        self.events: list[MouseButtonEvent | MouseResetEvent] = []
        self.grabs: list[int] = []

    def grab(self, button: int) -> MouseGrabResult:
        self.grabs.append(button)
        if self.held:
            return MouseGrabResult("held")
        self.registered = button
        return MouseGrabResult("ok")

    def cancel(self) -> None:
        self.registered = None
        self.events.clear()

    def poll_events(self) -> Sequence[MouseButtonEvent | MouseResetEvent]:
        events, self.events = self.events, []
        return events

    def button_down(self, button: int) -> bool:
        return self.held

    def fileno(self) -> int:
        return -1

    def close(self) -> None:
        self.cancel()


def test_interleaved_gestures_never_record_cancelled_or_released_input() -> None:
    """Explore event order across generations, asserting admission rather than FSM internals."""
    pointer = Pointer()
    now = 0.0
    allowed = True
    manager = MouseHoldManager(pointer, allowed=lambda: allowed, clock=lambda: now)
    tokens: list[int] = []
    eligible: tuple[int, float] | None = None
    recording: int | None = None
    starts = stops = cancellations = 0

    def pending(generation: int) -> None:
        nonlocal eligible
        tokens.append(generation)
        eligible = generation, now + 0.3

    def state_changed(state: MouseHoldState, _reason: str) -> None:
        nonlocal recording, starts, stops, cancellations
        if state == MouseHoldState.RECORDING:
            assert eligible is not None
            assert eligible[0] == manager.generation and now >= eligible[1]
            assert allowed and pointer.held and pointer.registered == 2
            assert recording is None, "one press must not create two recordings"
            recording = manager.generation
            starts += 1
        elif state == MouseHoldState.PROCESSING:
            assert recording == manager.generation, "tap/cancel must never become processing"
            assert pointer.registered is None, "release pointer before handing off audio"
            recording = None
            stops += 1
        else:
            assert pointer.registered is None
            recording = None
            cancellations += 1

    manager.on_pending = pending
    manager.on_state = state_changed
    rng = random.Random(7731)
    actions = ("arm", "press", "release", "tick", "cancel", "reset", "deny", "allow", "done")
    for _ in range(5000):
        action = rng.choice(actions)
        if action == "arm":
            eligible = None
            manager.arm(2)
        elif action == "press":
            pointer.held = True
            pointer.events.append(MouseButtonEvent("press", 2))
            manager.process_pending()
        elif action == "release":
            pointer.held = False
            pointer.events.append(MouseButtonEvent("release", 2))
            # Deliberately let half the releases wait behind a timer callback.
            if rng.choice((False, True)):
                manager.process_pending()
        elif action == "tick":
            now += rng.choice((0.0, 0.299, 0.3, 1.0, 121.0))
            token = rng.choice((manager.generation, rng.choice(tokens))) if tokens else None
            manager.tick(generation=token)
        elif action == "cancel":
            eligible = None
            manager.cancel("capture-lease")
        elif action == "reset":
            eligible = None
            pointer.events.append(MouseResetEvent("connection-lost"))
            pointer.events.append(MouseButtonEvent("press", 2))
            manager.process_pending()
        elif action == "deny":
            allowed = False
            manager.tick()
        elif action == "allow":
            allowed = True
        else:
            manager.done(rng.choice(tokens) if tokens else -1)
    # Finish with a complete new gesture after the adversarial history; this
    # proves the invariant checks exercised a successful processing handoff.
    manager.cancel("reset-for-fresh-gesture")
    allowed = True
    pointer.held = False
    assert manager.arm(2).ok
    pointer.held = True
    pointer.events.append(MouseButtonEvent("press", 2))
    manager.process_pending()
    now += 0.3
    manager.tick(generation=manager.generation)
    pointer.held = False
    pointer.events.append(MouseButtonEvent("release", 2))
    manager.process_pending()
    manager.close()
    assert starts >= 3 and stops >= 1 and cancellations >= 50, "trace must exercise real gestures"


@pytest.mark.parametrize(
    "payload",
    [
        '{"command_mouse_enabled":true,"command_mouse_button":4}',
        '{"command_mouse_enabled":true,"command_mouse_button":true}',
        '{"command_mouse_enabled":true,"command_mouse_button":67108864}',
        '{"command_mouse_enabled":"true","command_mouse_button":8}',
        '{"command_mouse_enabled":true,"command_mouse_button":null}',
        '{"command_mouse_enabled":true,',
        "[]",
    ],
)
def test_corrupted_saved_mouse_preference_cannot_arm_after_load_or_resave(
    tmp_path: Path, payload: str
) -> None:
    path = tmp_path / "settings.json"
    path.write_text(payload)
    pointer = Pointer()
    settings = load(path, quarantine=False)
    manager = MouseHoldManager(pointer, allowed=lambda: settings.command_mouse_enabled)
    assert not settings.command_mouse_enabled
    assert manager.arm(settings.command_mouse_button).code == "blocked"
    assert pointer.grabs == []
    save(settings, path)
    settings = load(path)
    assert not settings.command_mouse_enabled
    assert manager.arm(settings.command_mouse_button).code == "blocked"
    assert pointer.grabs == []
    assert json.loads(path.read_text())["command_mouse_enabled"] is False
