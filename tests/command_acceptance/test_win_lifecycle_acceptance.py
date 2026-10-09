"""Win arbitration and lifecycle acceptance, without real microphone/session changes.

Contract 8.5/8.7/9: only a deliberate hold records; an early chord reaches
the window manager, while a confirmed hold suppresses chords. Closed sessions
cancel old work and release input; recovery requires a fresh physical gesture.
The X11 cases require an explicitly declared, isolated test display.
"""

from __future__ import annotations

import os
import select
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest

from astra_voice.core.command_mode import SessionSnapshot
from astra_voice.core.dictation import DictationPhase
from astra_voice.platform.command_hotkey import CommandHotkeyBackend
from astra_voice.platform.cowork import DeliveryResult
from astra_voice.platform.hotkey import (
    GrabResult,
    HotkeyEvent,
    HotkeyManager,
    HotkeyMode,
    MappingEvent,
)
from astra_voice.platform.x11 import X11Display

from .test_mapping_loss import Rig
from .test_publication import PHRASE

CLOSED = [
    SessionSnapshot(known=True, locked=True),
    SessionSnapshot(known=True, locked=False, lock_requested=True),
    SessionSnapshot(known=False, locked=False),
    SessionSnapshot(known=True, locked=False, preparing_for_sleep=True),
    SessionSnapshot(known=True, locked=False, active=False),
]


@pytest.mark.unit
@pytest.mark.parametrize("closed", CLOSED)
@pytest.mark.parametrize("phase", ["pending", "recording", "processing"])
def test_closed_session_cancels_old_gesture_and_late_worker_result(
    monkeypatch: pytest.MonkeyPatch, closed: SessionSnapshot, phase: str
) -> None:
    rig = Rig(monkeypatch)
    if phase == "pending":
        rig.press()
    else:
        rig.hold()
        if phase == "processing":
            rig.event(HotkeyEvent("KeyRelease", 133, 320, mods=64))
            rig.hardware.fire_tail()
            assert rig.runtime.orchestrator.phase == DictationPhase.PROCESSING
    rig.command.snapshot = closed
    rig.runtime._command_session_changed()
    rig.runtime._command_session_changed()  # duplicate login1/ScreenSaver report
    rig.hardware.now += 1
    rig.manager.tick()  # old deferred callback after lock
    rig.worker("result", text=PHRASE)
    if phase == "pending":
        assert rig.commands() == []
    else:
        assert rig.commands().count("record.cancel") == 1
        rig.worker("cancelled")
    assert rig.command.client.calls == []
    assert rig.command.published == []
    rig.hardware.paste.assert_not_called()

    before = list(rig.commands())
    rig.command.snapshot = SessionSnapshot(known=True, locked=False)
    rig.runtime._command_session_changed()
    rig.event(HotkeyEvent("KeyRelease", 133, 2000, mods=64))  # stale old release
    rig.hardware.now += 1
    rig.manager.tick()
    assert rig.commands() == before
    rig.press()
    rig.hardware.now += 0.31
    rig.manager.tick()
    assert rig.commands() == [*before, "record.start"]


@pytest.mark.unit
@pytest.mark.parametrize("closed", CLOSED)
def test_closed_session_in_toggle_recording_cancels_without_physical_win_release(
    monkeypatch: pytest.MonkeyPatch, closed: SessionSnapshot
) -> None:
    rig = Rig(monkeypatch, HotkeyMode.TOGGLE)
    rig.press()
    rig.event(HotkeyEvent("KeyRelease", 133, 10, mods=64))
    assert rig.runtime.orchestrator.phase == DictationPhase.RECORDING
    rig.command.snapshot = closed
    rig.runtime._command_session_changed()
    assert rig.commands() == ["record.start", "record.cancel"]
    rig.worker("result", text=PHRASE)
    rig.worker("cancelled")
    assert rig.command.client.calls == []
    assert rig.command.published == []


@pytest.mark.unit
@pytest.mark.parametrize("closed", CLOSED)
def test_blocked_delivery_and_duplicate_reply_never_publish_or_resubmit(
    monkeypatch: pytest.MonkeyPatch, closed: SessionSnapshot
) -> None:
    rig = Rig(monkeypatch)
    rig.hold()
    rig.event(HotkeyEvent("KeyRelease", 133, 320, mods=64))
    rig.hardware.fire_tail()
    rig.worker("result", text=PHRASE)
    assert rig.runtime.orchestrator.phase == DictationPhase.DELIVERING
    assert len(rig.command.client.calls) == 1
    rig.command.snapshot = closed
    rig.runtime._command_session_changed()
    rig.command.client.reply(DeliveryResult("undelivered", "no_bus"))
    rig.command.client.reply(DeliveryResult("delivered", "none"))
    assert rig.command.published == []
    assert rig.command.presented == []
    rig.command.snapshot = SessionSnapshot(known=True, locked=False)
    rig.runtime._command_session_changed()
    rig.manager.tick()
    assert len(rig.command.client.calls) == 1
    assert rig.command.published == []
    rig.hardware.paste.assert_not_called()


@pytest.mark.unit
def test_early_chord_does_not_damage_next_command_or_ordinary_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = Rig(monkeypatch)
    rig.press()
    rig.hardware.now += 0.1
    rig.event(HotkeyEvent("KeyPress", 46, 100, mods=64))
    rig.event(HotkeyEvent("KeyRelease", 46, 110, mods=64))
    rig.event(HotkeyEvent("KeyRelease", 133, 120, mods=64))
    rig.hardware.now += 0.5
    rig.manager.tick()
    assert rig.commands() == []
    rig.hold()
    rig.event(HotkeyEvent("KeyPress", 9, 800, escape=True, mods=64))
    assert rig.commands() == ["record.start", "record.cancel"]
    rig.worker("cancelled")
    core = rig.runtime.orchestrator
    core.mode = "text"
    core._start()
    assert core.phase == DictationPhase.RECORDING
    assert rig.commands() == ["record.start", "record.cancel", "record.start"]
    rig.hardware.hotkey.ungrab.assert_not_called()


@pytest.mark.unit
@pytest.mark.parametrize("recording", [False, True])
def test_same_physical_keycode_mapping_loss_cancels_without_waiting_for_release(
    monkeypatch: pytest.MonkeyPatch, recording: bool
) -> None:
    rig = Rig(monkeypatch)
    if recording:
        rig.hold()
    else:
        rig.press()
    rig.event(
        MappingEvent(
            {"Super_L": GrabResult("not-grabbed", keycode=133)},
            None,
            regrabbed=True,
            masks_changed=True,
        )
    )
    rig.hardware.now += 1
    rig.manager.tick()
    assert rig.commands() == (["record.start", "record.cancel"] if recording else [])
    assert rig.command.client.calls == []
    assert rig.command.published == []


@pytest.mark.unit
def test_lock_unlock_epoch_discards_queued_confirmed_press_before_record_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = Rig(monkeypatch)
    rig.keyboard.events.append(HotkeyEvent("KeyPress", 133, 300, confirmed_hold=True))
    rig.command.snapshot = replace(rig.command.snapshot, blocked_epoch=1)
    rig.runtime._process_command_hotkey()
    rig.hardware.now += 1
    rig.manager.tick()
    assert rig.commands() == []
    assert rig.command.client.calls == []
    assert rig.command.published == []
    rig.hold()
    assert rig.commands() == ["record.start"]


class IsolatedKeyboard:
    """Real XTest input and independent WM client, no desktop services or Qt."""

    def __init__(self) -> None:
        from Xlib import XK, X, display

        self.wm = display.Display()
        self.target = display.Display()
        self.inject = display.Display()
        self.window = self.target.screen().root.create_window(
            0, 0, 100, 100, 0, X.CopyFromParent, event_mask=X.KeyPressMask | X.KeyReleaseMask
        )
        self.window.map()
        self.window.set_input_focus(X.RevertToParent, X.CurrentTime)
        self.target.sync()
        self.codes = {
            name: int(self.inject.keysym_to_keycode(XK.string_to_keysym(name)))
            for name in (
                "Super_L",
                "Control_L",
                "Shift_L",
                "l",
                "d",
                "e",
                "Tab",
                "space",
                "Caps_Lock",
            )
        }
        assert all(self.codes.values())
        self.backend = CommandHotkeyBackend()
        self.now = 1.0
        self.manager = HotkeyManager(self.backend, clock=lambda: self.now)
        self.manager.defer_single_super = True
        self.started: list[float] = []
        self.manager.on_press = lambda: self.started.append(self.now)
        assert self.manager.grab("Super_L", HotkeyMode.PTT).ok
        self.held: list[str] = []

    def key(self, name: str, pressed: bool) -> None:
        from Xlib import X
        from Xlib.ext import xtest

        xtest.fake_input(self.inject, X.KeyPress if pressed else X.KeyRelease, self.codes[name])
        self.inject.sync()
        if pressed:
            self.held.append(name)
        elif name in self.held:
            self.held.remove(name)
        self.pump()

    def pump(self, seconds: float = 0.02) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.manager.process_pending()
            self.manager.tick()
            self.wm.sync()
            time.sleep(0.002)

    def presses(self) -> list[int]:
        from Xlib import X

        events = []
        self.wm.sync()
        while self.wm.pending_events():
            event = self.wm.next_event()
            if event.type == X.KeyPress:
                assert int(event.window.id) == int(self.wm.screen().root.id)
                events.append(int(event.detail))
        return events

    def target_presses(self) -> list[int]:
        from Xlib import X

        events = []
        self.target.sync()
        while self.target.pending_events():
            event = self.target.next_event()
            if event.type == X.KeyPress:
                events.append(int(event.detail))
        return events

    def close(self) -> None:
        # Unfreeze before releasing synthetic keys, even on a failed assertion.
        from Xlib import X
        from Xlib.ext import xtest

        self.backend.close()
        for name in reversed(self.held):
            xtest.fake_input(self.inject, X.KeyRelease, self.codes[name])
        self.inject.sync()
        self.window.destroy()
        self.target.close()
        self.wm.close()
        self.inject.close()


@pytest.fixture
def isolated_keyboard() -> Iterator[IsolatedKeyboard]:
    display_name = os.environ.get("DISPLAY", "")
    if os.environ.get("ASTRA_VOICE_TEST_X11_ISOLATED") != "1" or display_name in {"", ":0", ":0.0"}:
        pytest.skip("requires ASTRA_VOICE_TEST_X11_ISOLATED=1 on a private Xvfb display")
    pytest.importorskip("Xlib")
    keyboard = IsolatedKeyboard()
    try:
        yield keyboard
    finally:
        keyboard.close()


@pytest.mark.xvfb
@pytest.mark.parametrize("delay", [0.0, 0.12, 0.25])
@pytest.mark.parametrize("extra", [None, "Control_L", "Shift_L"])
@pytest.mark.parametrize("shortcut", ["l", "d", "e", "Tab"])
def test_early_win_chord_reaches_independent_root_wm_grab_exactly_once(
    isolated_keyboard: IsolatedKeyboard, delay: float, extra: str | None, shortcut: str
) -> None:
    from Xlib import X

    keyboard = isolated_keyboard
    mask = X.Mod4Mask | {None: 0, "Control_L": X.ControlMask, "Shift_L": X.ShiftMask}[extra]
    keyboard.wm.screen().root.grab_key(
        keyboard.codes[shortcut], mask, False, X.GrabModeAsync, X.GrabModeAsync
    )
    keyboard.wm.sync()
    keyboard.key("Super_L", True)
    keyboard.pump(delay)
    if extra:
        keyboard.key(extra, True)
    keyboard.key(shortcut, True)
    keyboard.key(shortcut, False)
    if extra:
        keyboard.key(extra, False)
    keyboard.key("Super_L", False)
    keyboard.now += 0.5
    keyboard.pump()
    assert keyboard.started == [], "an early system chord opened command capture"
    assert keyboard.presses().count(keyboard.codes[shortcut]) == 1, (
        "WM shortcut swallowed/duplicated"
    )


@pytest.mark.xvfb
def test_confirmed_win_hold_suppresses_chord_then_keyboard_is_available(
    isolated_keyboard: IsolatedKeyboard,
) -> None:
    from Xlib import X

    keyboard = isolated_keyboard
    keyboard.wm.screen().root.grab_key(
        keyboard.codes["l"], X.Mod4Mask, False, X.GrabModeAsync, X.GrabModeAsync
    )
    keyboard.wm.sync()
    keyboard.key("Super_L", True)
    keyboard.now += 0.31
    keyboard.pump(0.31)
    assert len(keyboard.started) == 1
    keyboard.key("l", True)
    keyboard.key("l", False)
    keyboard.key("Super_L", False)
    assert keyboard.codes["l"] not in keyboard.presses()
    assert keyboard.codes["l"] not in keyboard.target_presses(), "recording leaked key into field"
    with X11Display() as rival:
        assert rival.grab_keyboard(), "released Win left an active keyboard grab"
        rival.ungrab_keyboard()


@pytest.mark.xvfb
@pytest.mark.parametrize("recording", [False, True])
def test_cancel_ungrab_releases_keyboard_even_while_physical_win_is_held(
    isolated_keyboard: IsolatedKeyboard, recording: bool
) -> None:
    keyboard = isolated_keyboard
    keyboard.key("Super_L", True)
    if recording:
        keyboard.now += 0.31
        keyboard.pump(0.31)
    keyboard.manager.ungrab()  # same operation used by lock/sleep/session-loss
    with X11Display() as rival:
        assert rival.grab_keyboard(), "lock cannot grab keyboard while old Super is physically held"
        rival.ungrab_keyboard()


@pytest.mark.xvfb
@pytest.mark.parametrize("modifier_first", ["Control_L", "Shift_L"])
def test_modifier_before_win_keeps_system_shortcut_available(
    isolated_keyboard: IsolatedKeyboard, modifier_first: str
) -> None:
    from Xlib import X

    keyboard = isolated_keyboard
    extra = X.ControlMask if modifier_first == "Control_L" else X.ShiftMask
    keyboard.wm.screen().root.grab_key(
        keyboard.codes["l"], X.Mod4Mask | extra, False, X.GrabModeAsync, X.GrabModeAsync
    )
    keyboard.wm.sync()
    keyboard.key(modifier_first, True)
    keyboard.key("Super_L", True)
    keyboard.key("l", True)
    keyboard.key("l", False)
    keyboard.key("Super_L", False)
    keyboard.key(modifier_first, False)
    keyboard.now += 0.5
    keyboard.pump(0.31)
    assert keyboard.started == []
    assert keyboard.presses().count(keyboard.codes["l"]) == 1


@pytest.mark.xvfb
def test_early_chord_then_new_hold_needs_no_reload_and_does_not_replay_twice(
    isolated_keyboard: IsolatedKeyboard,
) -> None:
    from Xlib import X

    keyboard = isolated_keyboard
    keyboard.wm.screen().root.grab_key(
        keyboard.codes["l"], X.Mod4Mask, False, X.GrabModeAsync, X.GrabModeAsync
    )
    keyboard.wm.sync()
    keyboard.key("Super_L", True)
    keyboard.now += 0.1
    keyboard.key("l", True)
    keyboard.key("l", False)
    keyboard.key("Super_L", False)
    keyboard.key("Super_L", True)
    keyboard.now += 0.31
    keyboard.pump(0.31)
    assert len(keyboard.started) == 1
    keyboard.key("l", True)
    keyboard.key("l", False)
    keyboard.key("Super_L", False)
    assert keyboard.presses().count(keyboard.codes["l"]) == 1


@pytest.mark.xvfb
def test_short_tap_is_swallowed_and_next_hold_starts_once(
    isolated_keyboard: IsolatedKeyboard,
) -> None:
    keyboard = isolated_keyboard
    keyboard.key("Super_L", True)
    keyboard.key("Super_L", False)
    keyboard.pump(0.35)
    assert keyboard.started == []
    assert keyboard.codes["Super_L"] not in keyboard.target_presses()
    keyboard.key("Super_L", True)
    keyboard.pump(0.35)
    assert len(keyboard.started) == 1
    keyboard.key("Super_L", False)


@pytest.mark.xvfb
@pytest.mark.parametrize("recording", [False, True])
@pytest.mark.parametrize("failure", ["kill", "stop", "eof", "parent_stall"])
def test_peer_failure_releases_held_super_within_one_second_and_requires_fresh_press(
    isolated_keyboard: IsolatedKeyboard, recording: bool, failure: str
) -> None:
    keyboard = isolated_keyboard
    keyboard.key("Super_L", True)
    if recording:
        keyboard.pump(0.35)
    before = len(keyboard.started)
    child = keyboard.backend.process
    assert child is not None
    began = time.monotonic()
    if failure == "parent_stall":
        time.sleep(0.75)  # intentionally stop GUI heartbeat, but not the X client
    elif failure == "eof":
        connection = keyboard.backend._connection
        assert connection is not None
        connection.shutdown(socket.SHUT_RDWR)
        keyboard.pump(0.05)
    else:
        os.kill(child.pid, signal.SIGKILL if failure == "kill" else signal.SIGSTOP)
        keyboard.pump(0.75)
    with X11Display() as rival:
        assert rival.grab_keyboard(), "peer failure prevented the locker taking keyboard"
        rival.ungrab_keyboard()
    assert time.monotonic() - began < 1
    keyboard.pump(0.05)
    assert child.poll() is not None
    assert len(keyboard.started) == before, "old confirmed press survived the failed lease"
    keyboard.manager.ungrab()
    assert not keyboard.manager.grab("Super_L", HotkeyMode.PTT).ok, "held old Super was rearmed"
    keyboard.key("Super_L", False)
    assert keyboard.manager.grab("Super_L", HotkeyMode.PTT).ok
    keyboard.key("Super_L", True)
    keyboard.pump(0.35)
    assert len(keyboard.started) == before + 1
    keyboard.key("Super_L", False)


@pytest.mark.xvfb
@pytest.mark.parametrize("recording", [False, True])
def test_native_mapping_regrab_with_same_super_keycode_releases_held_input(
    isolated_keyboard: IsolatedKeyboard, recording: bool
) -> None:
    from Xlib import XK

    keyboard = isolated_keyboard
    keyboard.key("Super_L", True)
    if recording:
        keyboard.pump(0.35)
    before = len(keyboard.started)
    caps = keyboard.codes["Caps_Lock"]
    original = keyboard.inject.get_keyboard_mapping(caps, 1)
    changed = [tuple(XK.string_to_keysym("F12") if code else 0 for code in original[0])]
    try:
        keyboard.inject.change_keyboard_mapping(caps, changed)
        keyboard.inject.sync()
        deadline = time.monotonic() + 0.3
        events = []
        while time.monotonic() < deadline:
            events.extend(keyboard.backend.poll_events())
            time.sleep(0.005)
        assert any(
            isinstance(event, MappingEvent)
            and "Super_L" in event.combos
            and not event.combos["Super_L"].ok
            for event in events
        ), "same-keycode mapping regrab did not report the lost release"
        if not recording:
            assert not any(
                isinstance(event, HotkeyEvent) and event.confirmed_hold for event in events
            ), "mapping loss allowed a delayed recording press"
        assert (
            keyboard.inject.keysym_to_keycode(XK.string_to_keysym("Super_L"))
            == keyboard.codes["Super_L"]
        )
        with X11Display() as rival:
            assert rival.grab_keyboard()
            rival.ungrab_keyboard()
        assert len(keyboard.started) == before
    finally:
        keyboard.manager.ungrab()
        keyboard.key("Super_L", False)
        keyboard.inject.change_keyboard_mapping(caps, original)
        keyboard.inject.sync()


_GUI_PEER = """
import sys
sys.path.insert(0, sys.argv[1])
from PyQt5.QtCore import QCoreApplication, QTimer
from astra_voice.platform.command_hotkey import CommandHotkeyBackend
from astra_voice.platform.hotkey import HotkeyManager, HotkeyMode
app = QCoreApplication([])
backend = CommandHotkeyBackend()
manager = HotkeyManager(backend)
manager.defer_single_super = True
manager.on_press = lambda: print('START', flush=True)
assert manager.grab('Super_L', HotkeyMode.PTT).ok
print('READY', flush=True)
def poll():
    manager.process_pending()
    manager.tick()
timer = QTimer()
timer.timeout.connect(poll)
timer.start(5)
app.exec()
"""


@pytest.mark.xvfb
@pytest.mark.parametrize("failure", ["stop", "kill"])
@pytest.mark.parametrize("recording", [False, True])
def test_separate_qt_gui_peer_failure_does_not_leave_held_grab_or_dispatch_old_press(
    isolated_keyboard: IsolatedKeyboard, failure: str, recording: bool
) -> None:
    import astra_voice

    keyboard = isolated_keyboard
    keyboard.manager.ungrab()
    peer = subprocess.Popen(
        [sys.executable, "-c", _GUI_PEER, str(Path(astra_voice.__file__).resolve().parent.parent)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        assert peer.stdout is not None
        assert select.select([peer.stdout], [], [], 2)[0], "Qt GUI peer did not become ready"
        assert peer.stdout.readline() == b"READY\n"
        keyboard.key("Super_L", True)
        if recording:
            assert select.select([peer.stdout], [], [], 0.5)[0]
            assert peer.stdout.readline() == b"START\n"
        began = time.monotonic()
        os.kill(peer.pid, signal.SIGSTOP if failure == "stop" else signal.SIGKILL)
        time.sleep(0.75)
        with X11Display() as rival:
            assert rival.grab_keyboard(), "stopped Qt GUI kept keyboard unavailable"
            rival.ungrab_keyboard()
        assert time.monotonic() - began < 1
        if failure == "stop":
            os.kill(peer.pid, signal.SIGCONT)
            time.sleep(0.12)
            assert not select.select([peer.stdout], [], [], 0)[0], "stale command press dispatched"
    finally:
        if peer.poll() is None:
            peer.kill()
        peer.communicate(timeout=2)
