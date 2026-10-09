"""Native pointer-grab evidence on a fresh owned Xvfb, never the user's display."""

from __future__ import annotations

import time
from collections.abc import Iterator
from typing import Any, cast

import pytest
from test_command_lock_active_grab import owned_xvfb as _owned_xvfb

from astra_voice.platform.mouse_button import (
    MouseHoldManager,
    MouseHoldState,
    X11MouseBackend,
)
from astra_voice.platform.x11 import X11Display

pytestmark = pytest.mark.xvfb
owned_xvfb = _owned_xvfb


@pytest.fixture
def pointer_clients(owned_xvfb: str) -> Iterator[tuple[X11Display, Any, X11MouseBackend]]:
    from Xlib import X, display
    from Xlib.ext import xtest

    x11 = X11Display()
    peer = None
    backend = X11MouseBackend(x11)
    try:
        assert x11.open(owned_xvfb)
        peer = display.Display(owned_xvfb)
        assert peer.has_extension("XTEST"), "owned server must provide XTEST"
        # Xvfb lazily initialises the keyboard map. Discard only this setup's
        # MappingNotify events before registering any button gesture.
        x11._read_lock_masks()
        peer.sync()
        assert x11.d is not None
        x11.d.sync()
        while x11.d.pending_events():
            x11.d.next_event()
        yield x11, peer, backend
    finally:
        if x11.d is not None:
            x11.d.ungrab_pointer(X.CurrentTime)
            x11.d.ungrab_keyboard(X.CurrentTime)
            x11.d.sync()
        if peer is not None:
            peer.ungrab_pointer(X.CurrentTime)
            peer.ungrab_keyboard(X.CurrentTime)
            for button in (2, 8):
                xtest.fake_input(peer, X.ButtonRelease, button)
            peer.sync()
            peer.close()
        backend.close()


def pointer_grab(peer: Any) -> int:
    from Xlib import X

    reply = peer.screen().root.grab_pointer(
        False,
        X.ButtonPressMask | X.ButtonReleaseMask,
        X.GrabModeAsync,
        X.GrabModeAsync,
        X.NONE,
        X.NONE,
        X.CurrentTime,
    )
    return int(reply)


def press_and_wait(peer: Any, manager: MouseHoldManager, button: int) -> None:
    from Xlib import X
    from Xlib.ext import xtest

    xtest.fake_input(peer, X.ButtonPress, button)
    peer.sync()
    deadline = time.monotonic() + 1
    while manager.state != MouseHoldState.PENDING and time.monotonic() < deadline:
        manager.process_pending()
        time.sleep(0.002)
    assert manager.state == MouseHoldState.PENDING


@pytest.mark.parametrize("button", [2, 8], ids=["middle", "extra"])
@pytest.mark.parametrize("cancel", ["force", "close", "legacy"])
def test_cancel_frees_active_pointer_before_release_with_legacy_defect_control(
    pointer_clients: tuple[X11Display, Any, X11MouseBackend],
    button: int,
    cancel: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from Xlib import X

    x11, peer, backend = pointer_clients
    manager = MouseHoldManager(backend, allowed=lambda: True)
    assert manager.arm(button).ok
    press_and_wait(peer, manager, button)
    assert backend.button_down(button) is True
    assert pointer_grab(peer) == X.AlreadyGrabbed
    if cancel == "legacy":

        def passive_only() -> None:
            for mask in x11.mask_variants(0):
                x11.root.ungrab_button(button, mask)
            assert x11.d is not None
            x11.d.sync()

        monkeypatch.setattr(backend, "cancel", passive_only)
    if cancel == "close":
        manager.close()
    else:
        manager.cancel("lock")
    # No fake release has occurred. Core query_pointer cannot represent extra
    # buttons; the backend must obtain their held state through real XI2.
    if cancel != "close":
        assert backend.button_down(button) is True
    assert pointer_grab(peer) == (X.AlreadyGrabbed if cancel == "legacy" else X.GrabSuccess)


@pytest.mark.parametrize("button", [2, 8], ids=["middle", "extra"])
def test_held_rearm_requires_release_and_a_new_press(
    pointer_clients: tuple[X11Display, Any, X11MouseBackend], button: int
) -> None:
    from Xlib import X
    from Xlib.ext import xtest

    _x11, peer, backend = pointer_clients
    now = 0.0
    states: list[MouseHoldState] = []
    manager = MouseHoldManager(backend, allowed=lambda: True, clock=lambda: now)
    manager.on_state = lambda state, reason: states.append(state)
    assert manager.arm(button).ok
    press_and_wait(peer, manager, button)
    manager.cancel("capture")
    assert manager.arm(button).code == "held"
    now = 1.0
    manager.tick()
    assert MouseHoldState.RECORDING not in states
    xtest.fake_input(peer, X.ButtonRelease, button)
    peer.sync()
    assert manager.arm(button).ok
    manager.tick()
    assert MouseHoldState.RECORDING not in states
    press_and_wait(peer, manager, button)
    now += 0.3
    manager.tick()
    assert states[-1] == MouseHoldState.RECORDING
    xtest.fake_input(peer, X.ButtonRelease, button)
    peer.sync()
    deadline = time.monotonic() + 1
    while manager.state != MouseHoldState.PROCESSING and time.monotonic() < deadline:
        manager.process_pending()
        time.sleep(0.002)
    assert cast(MouseHoldState, states[-1]) == MouseHoldState.PROCESSING
    assert pointer_grab(peer) == X.GrabSuccess


def test_mouse_cancellation_does_not_release_keyboard_owned_by_another_connection(
    pointer_clients: tuple[X11Display, Any, X11MouseBackend], owned_xvfb: str
) -> None:
    from Xlib import X, display

    _x11, peer, backend = pointer_clients
    observer = display.Display(owned_xvfb)
    try:
        assert (
            peer.screen().root.grab_keyboard(False, X.GrabModeAsync, X.GrabModeAsync, X.CurrentTime)
            == X.GrabSuccess
        )
        manager = MouseHoldManager(backend, allowed=lambda: True)
        assert manager.arm(2).ok
        press_and_wait(peer, manager, 2)
        manager.cancel("lock")
        assert pointer_grab(observer) == X.GrabSuccess
        assert (
            observer.screen().root.grab_keyboard(
                False, X.GrabModeAsync, X.GrabModeAsync, X.CurrentTime
            )
            == X.AlreadyGrabbed
        )
    finally:
        observer.ungrab_pointer(X.CurrentTime)
        observer.close()


def test_busy_registration_preserves_other_clients_pointer_and_can_retry(
    pointer_clients: tuple[X11Display, Any, X11MouseBackend],
) -> None:
    from Xlib import X

    _x11, peer, backend = pointer_clients
    root = peer.screen().root
    root.grab_button(
        2,
        0,
        False,
        X.ButtonPressMask | X.ButtonReleaseMask,
        X.GrabModeAsync,
        X.GrabModeAsync,
        X.NONE,
        X.NONE,
    )
    peer.sync()
    manager = MouseHoldManager(backend, allowed=lambda: True)
    assert manager.arm(2).code == "busy"
    manager.cancel("disabled")
    root.ungrab_button(2, 0)
    peer.sync()
    assert manager.arm(2).ok
    press_and_wait(peer, manager, 2)
    assert pointer_grab(peer) == X.AlreadyGrabbed
    manager.cancel("exit")
    assert pointer_grab(peer) == X.GrabSuccess


@pytest.mark.parametrize("held_before_arm", [True, False], ids=["held-before-arm", "hold-start"])
def test_remapped_physical_extra_button_is_checked_by_its_logical_number(
    pointer_clients: tuple[X11Display, Any, X11MouseBackend], held_before_arm: bool
) -> None:
    from Xlib import X
    from Xlib.ext import xtest

    _x11, peer, backend = pointer_clients
    mapping = list(peer.get_pointer_mapping())
    assert len(mapping) >= 8, "owned Xvfb must expose a physical extra button"
    mapping[7] = 31
    assert peer.set_pointer_mapping(mapping) == X.MappingSuccess
    peer.sync()
    backend.poll_events()  # acknowledge setup's intentional MappingNotify
    now = 0.0
    manager = MouseHoldManager(backend, allowed=lambda: True, clock=lambda: now)
    if held_before_arm:
        xtest.fake_input(peer, X.ButtonPress, 8)
        peer.sync()
        assert backend.button_down(31) is True
        assert manager.arm(31).code == "held"
    else:
        assert manager.arm(31).ok
        xtest.fake_input(peer, X.ButtonPress, 8)
        peer.sync()
        deadline = time.monotonic() + 1
        while manager.state != MouseHoldState.PENDING and time.monotonic() < deadline:
            manager.process_pending()
            time.sleep(0.002)
        assert manager.state == MouseHoldState.PENDING
        assert backend.button_down(31) is True
        now = 0.3
        manager.tick()
        assert cast(MouseHoldState, manager.state) == MouseHoldState.RECORDING
