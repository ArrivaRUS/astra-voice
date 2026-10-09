"""Command handoff through the real broker on an explicitly isolated Xvfb.

No KWin lease is acquired: these tests exercise IPC, passive/active X grabs,
Raw input dispatch, and the physical release boundary without host settings.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from typing import Any

import pytest

from astra_voice.platform.command_hotkey import CommandHotkeyBackend
from astra_voice.platform.hotkey import (
    HotkeyEvent,
    HotkeyManager,
    HotkeyMode,
    HotkeyState,
    X11HotkeyBackend,
)

pytestmark = pytest.mark.xvfb


class NativeKeyboard:
    def __init__(self) -> None:
        from Xlib import XK, display

        self.source = display.Display()
        self.code = self.source.keysym_to_keycode(XK.string_to_keysym("Super_L"))
        self.backend = CommandHotkeyBackend(manage_menu=False)
        self.manager = HotkeyManager(self.backend)
        self.manager.defer_single_super = True
        self.peer = X11HotkeyBackend()
        self.starts: list[str] = []
        self.manager.on_state = self.state_changed

    def state_changed(self, state: HotkeyState, reason: str) -> None:
        if state == HotkeyState.RECORDING:
            self.starts.append(reason)

    def key(self, pressed: bool) -> None:
        from Xlib import X
        from Xlib.ext import xtest

        xtest.fake_input(self.source, X.KeyPress if pressed else X.KeyRelease, self.code)
        self.source.sync()

    def pump(self, seconds: float, *, collect: bool = False) -> list[HotkeyEvent]:
        keys: list[HotkeyEvent] = []
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if collect:
                keys.extend(e for e in self.backend.poll_events() if isinstance(e, HotkeyEvent))
            else:
                self.manager.process_pending()
                self.manager.tick()
            time.sleep(0.005)
        return keys

    def assert_passive_key_free(self) -> None:
        try:
            assert self.peer.grab_combo("Super_L").ok
        finally:
            self.peer.ungrab_combo("Super_L")

    def close(self) -> None:
        self.backend.close()
        self.peer.close()
        self.key(False)
        self.source.close()


@pytest.fixture
def keyboard() -> Iterator[NativeKeyboard]:
    if os.environ.get("ASTRA_VOICE_TEST_X11_ISOLATED") != "1" or os.environ.get("DISPLAY", "") in {
        "",
        ":0",
        ":0.0",
    }:
        pytest.skip("requires an explicitly isolated Xvfb; live displays are never used")
    rig = NativeKeyboard()
    try:
        assert rig.manager.grab("Super_L", HotkeyMode.PTT).ok
        assert rig.backend.process is not None
        assert rig.backend._menu_token is None
        yield rig
    finally:
        rig.close()


def test_native_pause_drops_input_but_keeps_broker_for_fresh_hold(keyboard: NativeKeyboard) -> None:
    rig = keyboard
    process, connection = rig.backend.process, rig.backend._connection
    assert process is not None
    assert rig.peer.grab_combo("Super_L").code == "busy", "initial command grab must exist"
    rig.manager.suspend()
    rig.assert_passive_key_free()
    rig.key(True)
    keys = rig.pump(0.75, collect=True)  # Longer than both hold and heartbeat health deadlines.
    rig.key(False)
    keys.extend(rig.pump(0.1, collect=True))
    assert keys == [], "paused broker must not forward Core/Raw command edges"
    assert rig.starts == []
    assert process.poll() is None
    assert rig.backend.process is process and rig.backend._connection is connection
    assert rig.manager.rearm("Super_L", HotkeyMode.PTT).ok
    assert rig.backend.process is process and rig.backend._connection is connection
    rig.pump(0.35)
    assert rig.starts == [], "resume alone must not replay the paused gesture"
    rig.key(True)
    rig.pump(0.4)
    assert rig.starts == ["press"]
    rig.key(False)
    rig.pump(0.1)
    assert rig.manager.fsm.state == HotkeyState.PROCESSING


def test_native_held_win_cannot_become_a_new_gesture_on_resume(keyboard: NativeKeyboard) -> None:
    rig = keyboard
    rig.key(True)
    rig.pump(0.05)  # Broker has a pending Win hold, GUI has not started recording.
    assert rig.starts == []
    rig.manager.suspend()
    assert not rig.manager.rearm("Super_L", HotkeyMode.PTT).ok
    rig.assert_passive_key_free()
    rig.pump(0.4)
    assert rig.starts == []
    rig.key(False)
    rig.pump(0.1)
    assert rig.manager.rearm("Super_L", HotkeyMode.PTT).ok
    rig.pump(0.35)
    assert rig.starts == []
    rig.key(True)
    rig.pump(0.4)
    assert rig.starts == ["press"]


def test_native_final_teardown_after_pause_releases_process_and_x_resources(
    keyboard: NativeKeyboard,
) -> None:
    from Xlib import X

    rig = keyboard
    process = rig.backend.process
    assert process is not None
    rig.manager.suspend()
    rig.manager.ungrab()
    rig.backend.close()
    assert process.poll() is not None
    assert rig.backend.process is None and rig.backend._connection is None
    assert rig.backend.fileno() == -1
    assert rig.manager._suspended_combo is None
    rig.assert_passive_key_free()
    root: Any = rig.source.screen().root
    try:
        assert (
            root.grab_keyboard(False, X.GrabModeAsync, X.GrabModeAsync, X.CurrentTime)
            == X.GrabSuccess
        )
    finally:
        rig.source.ungrab_keyboard(X.CurrentTime)
        rig.source.sync()
