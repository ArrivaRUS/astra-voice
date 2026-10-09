"""Lost command key must not leave capture running without a release event.

PRD F17 reuses cancellation/record limits of text mode, but an unavailable
command key is not permission to record until 120 s. Contract §8.7 requires
session gates and removal of grabs at lock. Exercise public event flow through
actual HotkeyManager and runtime, with no X11, microphone or bus connection.
"""

from __future__ import annotations

from collections.abc import Sequence
from importlib import import_module
from typing import Any
from unittest.mock import Mock

import pytest

from astra_voice.core.command_mode import SessionSnapshot
from astra_voice.core.dictation import DictationPhase
from astra_voice.core.settings import from_dict
from astra_voice.platform.hotkey import (
    GrabResult,
    HotkeyEvent,
    HotkeyManager,
    HotkeyMode,
    MappingEvent,
)
from astra_voice.platform.session import SessionKind

from .test_publication import PHRASE
from .test_publication import Rig as CommandRig

RuntimeRig = import_module("unit.test_runtime").Rig
pytestmark = pytest.mark.unit


class Keyboard:
    def __init__(self) -> None:
        self.events: list[HotkeyEvent | MappingEvent] = []
        self.grabs: list[str] = []
        self.signature: tuple[int, int] | None = (133, 0)
        self.result = GrabResult("ok", keycode=133)

    def combo_signature(self, combo: str) -> tuple[int, int] | None:
        return self.signature

    def grab_combo(self, combo: str) -> GrabResult:
        self.grabs.append(combo)
        return self.result

    def ungrab_combo(self, combo: str) -> GrabResult:
        return GrabResult("ok")

    def grab_escape(self) -> GrabResult:
        return GrabResult("ok", keycode=9)

    def ungrab_escape(self) -> None:
        pass

    def poll_events(self, timeout: float = 0.0) -> Sequence[HotkeyEvent | MappingEvent]:
        events, self.events = self.events, []
        return events

    def fileno(self) -> int:
        return -1


class Rig:
    def __init__(self, monkeypatch: pytest.MonkeyPatch, mode: HotkeyMode = HotkeyMode.PTT) -> None:
        self.hardware: Any = RuntimeRig(
            monkeypatch,
            from_dict({"command_hotkey": "Super_L", "hotkey_mode": mode.value}),
            session_kind=SessionKind.KDE,
        )
        self.runtime = self.hardware.runtime
        self.command = CommandRig()
        self.runtime._command_session = Mock(snapshot=lambda: self.command.snapshot)
        self.runtime._command_mode = self.command.mode
        self.runtime.orchestrator.command_mode = self.command.mode
        self.runtime._loading_model = False
        self.runtime._selfcheck = "ok"
        self.runtime._pending_test = None
        self.hardware.hotkey.signature.return_value = (65, 4)
        self.keyboard = Keyboard()
        self.manager = HotkeyManager(self.keyboard, clock=lambda: self.hardware.now)
        self.manager.on_state = self.runtime._on_command_hotkey_state
        self.runtime.command_hotkey = self.manager
        assert self.runtime.reload_command_hotkey()

    def commands(self) -> list[str]:
        return [str(item.args[0]["type"]) for item in self.hardware.supervisor.send.call_args_list]

    def event(self, event: HotkeyEvent | MappingEvent) -> None:
        self.keyboard.events.append(event)
        self.runtime._process_command_hotkey()

    def press(self) -> None:
        self.event(HotkeyEvent("KeyPress", 133, int(self.hardware.now * 1000)))

    def hold(self) -> None:
        self.press()
        self.hardware.now += 0.31
        self.manager.tick()
        assert self.runtime.orchestrator.phase == DictationPhase.RECORDING
        assert self.commands() == ["record.start"]

    def mapping(self, result: GrabResult) -> None:
        self.keyboard.result = result
        self.event(MappingEvent({"Super_L": result}, None, regrabbed=True, keycode_changed=True))

    def worker(self, kind: str, **fields: object) -> None:
        core = self.runtime.orchestrator
        core.on_worker_event(
            {
                "type": kind,
                "generation": self.hardware.supervisor.generation,
                "utterance_id": core._utterance_id,
                **fields,
            }
        )


def test_pending_single_win_lost_before_300ms_never_opens_microphone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = Rig(monkeypatch)
    rig.press()
    assert rig.commands() == []
    rig.mapping(GrabResult("busy", keycode=134))
    rig.hardware.now += 0.4
    rig.manager.tick()  # late 300 ms callback for the pre-MappingNotify press
    assert rig.commands() == []
    rig.hardware.guard.set_recording.assert_not_called()
    assert rig.command.client.calls == []


@pytest.mark.parametrize("mode", [HotkeyMode.PTT, HotkeyMode.TOGGLE])
def test_mapping_loss_while_recording_cancels_without_waiting_for_release_or_limit(
    monkeypatch: pytest.MonkeyPatch, mode: HotkeyMode
) -> None:
    rig = Rig(monkeypatch, mode)
    rig.hold()
    rig.mapping(GrabResult("busy", keycode=134))
    assert rig.commands() == ["record.start", "record.cancel"]
    rig.hardware.guard.set_recording.assert_called_with(False)
    rig.worker("result", text=PHRASE)  # old worker response before acknowledgement
    rig.worker("cancelled")
    rig.hardware.now += 121
    rig.manager.tick()  # no physical release ever arrives from the lost grab
    assert rig.commands() == ["record.start", "record.cancel"]
    assert rig.command.client.calls == []
    assert rig.command.published == []
    rig.hardware.paste.assert_not_called()


def test_busy_to_ready_recovery_requires_a_fresh_command_gesture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = Rig(monkeypatch)
    rig.hold()
    rig.mapping(GrabResult("busy", keycode=134))
    assert "record.cancel" in rig.commands()
    rig.worker("cancelled")
    before = list(rig.commands())
    rig.keyboard.signature = (133, 0)
    rig.mapping(GrabResult("ok", keycode=133))
    assert rig.runtime.reload_command_hotkey()
    rig.hardware.now += 0.31
    rig.manager.tick()
    assert rig.commands() == before
    rig.press()
    rig.hardware.now += 0.31
    rig.manager.tick()
    assert rig.commands() == [*before, "record.start"]


def test_mapping_collision_stops_command_but_does_not_ungrab_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = Rig(monkeypatch)
    rig.hold()
    rig.keyboard.signature = (65, 4)  # current map aliases the text role
    rig.mapping(GrabResult("ok", keycode=65, mods=4))
    assert rig.commands() == ["record.start", "record.cancel"]
    rig.hardware.guard.set_recording.assert_called_with(False)
    rig.hardware.hotkey.ungrab.assert_not_called()
    assert not rig.runtime.reload_command_hotkey()
    rig.hardware.now += 0.31
    rig.manager.tick()
    assert rig.commands() == ["record.start", "record.cancel"]


def test_command_mapping_loss_does_not_cancel_an_independent_text_recording(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = Rig(monkeypatch)
    core = rig.runtime.orchestrator
    core.mode = "text"
    core._start()
    assert core.phase == DictationPhase.RECORDING
    rig.mapping(GrabResult("busy", keycode=134))
    assert rig.commands() == ["record.start"]
    rig.hardware.guard.set_recording.assert_called_once_with(True)
    rig.hardware.hotkey.ungrab.assert_not_called()
    assert core.mode == "text"


@pytest.mark.parametrize(
    "closed",
    [
        SessionSnapshot(known=True, locked=True),
        SessionSnapshot(known=False, locked=False),
        SessionSnapshot(known=True, locked=False, preparing_for_sleep=True),
    ],
)
def test_old_mapping_and_threshold_callback_after_lock_cannot_start_capture(
    monkeypatch: pytest.MonkeyPatch, closed: SessionSnapshot
) -> None:
    rig = Rig(monkeypatch)
    rig.press()
    rig.command.snapshot = closed
    rig.runtime._command_session_changed()
    rig.mapping(GrabResult("ok", keycode=133))  # stale success from old subscription/grab
    rig.hardware.now += 0.4
    rig.manager.tick()
    assert rig.commands() == []
    assert not rig.runtime.reload_command_hotkey()
    rig.manager.tick()
    assert rig.commands() == []
    assert rig.command.client.calls == []


def test_repeated_busy_mapping_does_not_send_repeated_cancel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = Rig(monkeypatch)
    rig.hold()
    rig.mapping(GrabResult("busy", keycode=134))
    rig.mapping(GrabResult("busy", keycode=134))
    assert rig.commands() == ["record.start", "record.cancel"]


def test_successful_keycode_shift_during_hold_cannot_wait_for_old_physical_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = Rig(monkeypatch)
    rig.hold()
    rig.keyboard.signature = (134, 0)
    rig.mapping(GrabResult("ok", keycode=134))
    assert rig.commands() == ["record.start", "record.cancel"]
    rig.hardware.guard.set_recording.assert_called_with(False)
    rig.event(HotkeyEvent("KeyRelease", 133, int(rig.hardware.now * 1000)))
    rig.worker("result", text=PHRASE)
    assert rig.command.client.calls == []
    assert rig.commands() == ["record.start", "record.cancel"]


def test_successful_remap_clears_pending_threshold_without_false_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = Rig(monkeypatch)
    rig.press()
    rig.keyboard.signature = (134, 0)
    rig.mapping(GrabResult("ok", keycode=134))
    rig.hardware.now += 0.4
    rig.manager.tick()
    rig.event(HotkeyEvent("KeyRelease", 133, int(rig.hardware.now * 1000)))
    assert rig.commands() == []
    rig.hardware.guard.set_recording.assert_not_called()


def test_unchanged_successful_mapping_does_not_cancel_intentional_recording(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = Rig(monkeypatch)
    rig.hold()
    rig.event(MappingEvent({"Super_L": GrabResult("ok", keycode=133)}, None))
    assert rig.commands() == ["record.start"]
    rig.hardware.guard.set_recording.assert_called_once_with(True)
    rig.hardware.now += 1.0
    rig.event(HotkeyEvent("KeyRelease", 133, int(rig.hardware.now * 1000)))
    rig.hardware.fire_tail()
    assert rig.commands() == ["record.start", "record.stop", "recognize"]


def test_loss_after_release_cancels_tail_before_it_can_request_recognition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = Rig(monkeypatch)
    rig.hold()
    rig.hardware.now += 1.0
    rig.event(HotkeyEvent("KeyRelease", 133, int(rig.hardware.now * 1000)))
    assert rig.commands() == ["record.start"]  # release tail is still recording
    rig.mapping(GrabResult("busy", keycode=134))
    assert rig.commands() == ["record.start", "record.cancel"]
    rig.hardware.guard.set_recording.assert_called_with(False)
    rig.runtime.orchestrator._release_tail_elapsed()  # callback already dispatched before cancel
    assert rig.commands() == ["record.start", "record.cancel"]
    assert rig.command.client.calls == []


def test_loss_while_recognizing_drops_late_worker_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = Rig(monkeypatch)
    rig.hold()
    rig.hardware.now += 1.0
    rig.event(HotkeyEvent("KeyRelease", 133, int(rig.hardware.now * 1000)))
    rig.hardware.fire_tail()
    assert rig.runtime.orchestrator.phase == DictationPhase.PROCESSING
    rig.mapping(GrabResult("busy", keycode=134))
    assert rig.commands() == ["record.start", "record.stop", "recognize", "record.cancel"]
    rig.worker("result", text=PHRASE)
    assert rig.command.client.calls == []
    assert rig.command.published == []
    rig.hardware.paste.assert_not_called()


def test_recovery_timer_restores_free_key_within_30s_without_opening_microphone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = Rig(monkeypatch)
    rig.hold()
    rig.mapping(GrabResult("busy", keycode=134))
    rig.worker("cancelled")
    retries = [t for t in rig.hardware.timers if t.active and not t.single_shot]
    assert len(retries) == 1
    assert 0 < retries[0].interval <= 30_000  # PRD F2.11 bound, independent of timer constant
    rig.keyboard.result = GrabResult("ok", keycode=133)
    before = list(rig.commands())
    retries[0].fire()  # the user only frees the key; there is no explicit retry gesture
    assert not retries[0].active and retries[0].deleted
    assert rig.commands() == before
    rig.press()
    rig.hardware.now += 0.31
    rig.manager.tick()
    assert rig.commands() == [*before, "record.start"]


def test_retry_queued_before_lock_cannot_grab_or_start_after_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = Rig(monkeypatch)
    rig.press()
    rig.mapping(GrabResult("busy", keycode=134))
    retries = [t for t in rig.hardware.timers if t.active and not t.single_shot]
    assert len(retries) == 1
    rig.command.snapshot = SessionSnapshot(known=True, locked=True)
    rig.runtime._command_session_changed()
    grabbed = list(rig.keyboard.grabs)
    rig.keyboard.result = GrabResult("ok", keycode=133)
    retries[0].timeout.emit()  # timeout was queued before stop/deleteLater
    assert rig.keyboard.grabs == grabbed
    assert rig.commands() == []
    assert not retries[0].active and retries[0].deleted


def test_auto_retry_detects_new_text_collision_without_stealing_text_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = Rig(monkeypatch)
    rig.press()
    rig.mapping(GrabResult("busy", keycode=134))
    retries = [t for t in rig.hardware.timers if t.active and not t.single_shot]
    assert len(retries) == 1
    grabbed = list(rig.keyboard.grabs)
    rig.keyboard.signature = (65, 4)
    rig.keyboard.result = GrabResult("ok", keycode=65, mods=4)
    retries[0].fire()
    assert rig.keyboard.grabs == grabbed
    rig.hardware.hotkey.ungrab.assert_not_called()
    assert not retries[0].active and retries[0].deleted
    assert rig.commands() == []
