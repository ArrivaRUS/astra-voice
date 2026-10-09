"""Independent runtime admission/ownership evidence using fully fake hardware ports."""

from __future__ import annotations

from dataclasses import replace
from importlib import import_module
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest

from astra_voice.core.command_mode import CommandMode, SessionSnapshot
from astra_voice.core.dictation import DictationPhase
from astra_voice.core.settings import Settings
from astra_voice.platform.hotkey import (
    GrabResult,
    HotkeyEvent,
    HotkeyManager,
    HotkeyMode,
    ResultCode,
)
from astra_voice.platform.mouse_button import MouseButtonEvent, MouseHoldManager, MouseHoldState
from astra_voice.platform.session import SessionKind
from helpers.qt_app import get_qapplication

from .test_mouse_hold_safety import Pointer
from .test_publication import PHRASE, FakeClient

RuntimeRig = import_module("unit.test_runtime").Rig
pytestmark = pytest.mark.unit


class Keys:
    def __init__(self) -> None:
        self.events: list[HotkeyEvent] = []
        self.escape = False
        self.result: ResultCode = "ok"

    def combo_signature(self, combo: str) -> tuple[int, int]:
        return {"Ctrl+Space": (65, 4), "Ctrl+F9": (75, 4), "Super_L": (133, 0)}[combo]

    def grab_combo(self, combo: str) -> GrabResult:
        keycode, mods = self.combo_signature(combo)
        return GrabResult(self.result, keycode=keycode, mods=mods)

    def ungrab_combo(self, combo: str) -> GrabResult:
        return GrabResult("ok")

    def grab_escape(self) -> GrabResult:
        self.escape = True
        return GrabResult("ok", keycode=9)

    def ungrab_escape(self) -> None:
        self.escape = False

    def cancel_keyboard_grab(self) -> None:
        pass

    def poll_events(self, timeout: float = 0.0) -> list[HotkeyEvent]:
        events, self.events = self.events, []
        return events

    def fileno(self) -> int:
        return -1

    def close(self) -> None:
        self.escape = False


class Rig:
    def __init__(
        self, monkeypatch: pytest.MonkeyPatch, *, keyboard_mode: str = "ptt", preview: bool = False
    ) -> None:
        get_qapplication()
        self.keys = Keys()
        self.text = HotkeyManager(self.keys, clock=lambda: self.hardware.now)
        self.hardware: Any = RuntimeRig(
            monkeypatch,
            Settings(command_hotkey="Ctrl+F9", hotkey_mode=keyboard_mode, command_preview=preview),
            hotkey_factory=lambda: self.text,
            session_kind=SessionKind.KDE,
        )
        self.runtime = self.hardware.runtime
        self.snapshot = SessionSnapshot(known=True, locked=False)
        self.runtime._command_session = Mock(snapshot=lambda: self.snapshot)
        self.runtime.command_installed = True
        self.runtime.command_available = True
        self.runtime._loading_model = False
        self.runtime._selfcheck = "ok"
        self.runtime._pending_test = None
        self.request = {"id": "gigaam", "revision": "v3", "dir": "/tmp/mouse-test-model"}
        self.runtime._model_load_request = self.request
        self.runtime._model_load_generation = self.hardware.supervisor.generation
        self.record = SimpleNamespace(
            state="ok",
            recheck=False,
            metadata_ok=True,
            id="gigaam",
            revision="v3",
            dir="/tmp/mouse-test-model",
        )
        self.runtime.model_store = Mock(current=lambda: self.record)
        self.runtime._revoked_check = lambda model_id, revision: False
        self.runtime.revocation_unknown = False
        self.client = FakeClient()
        self.published: list[str] = []
        self.presented: list[object] = []
        self.mode = CommandMode(
            client=self.client,
            session=self.runtime._command_snapshot,
            model_trusted=self.runtime._command_model_trusted,
            publish=self.publish,
            present=self.presented.append,
        )
        self.runtime._command_mode = self.mode
        self.runtime.orchestrator.command_mode = self.mode
        self.runtime.orchestrator._command_preview = self.runtime._preview_command
        self.runtime.on_command_preview = Mock()
        self.command_keys = Keys()
        self.command = HotkeyManager(self.command_keys, clock=lambda: self.hardware.now)
        self.command.on_state = self.runtime._on_command_hotkey_state
        self.runtime.command_hotkey = self.command
        assert self.text.grab("Ctrl+Space", HotkeyMode(keyboard_mode)).ok
        assert self.runtime.reload_command_hotkey()
        self.pointer = Pointer()
        self.mouse = MouseHoldManager(self.pointer, clock=lambda: self.hardware.now)
        self.mouse_factory = Mock(return_value=self.mouse)
        self.runtime._mouse_factory = self.mouse_factory

    def arm_mouse(self) -> None:
        self.runtime.settings.command_mouse_enabled = True
        self.runtime.reload_command_mouse()
        assert self.pointer.registered == 2

    def mouse_event(self, kind: str) -> None:
        self.pointer.held = kind == "press"
        self.pointer.events.append(MouseButtonEvent(kind, 2))  # type: ignore[arg-type]
        self.runtime._process_mouse()

    def hold(self) -> None:
        self.mouse_event("press")
        self.hardware.now += 0.31
        self.mouse.tick(generation=self.mouse.generation)

    def key(self, source: str, kind: str) -> None:
        manager, code = (self.text, 65) if source == "text" else (self.command, 75)
        manager.handle_event(HotkeyEvent(kind, code, 1, mods=4), self.hardware.now)  # type: ignore[arg-type]

    def commands(self) -> list[str]:
        return [str(call.args[0]["type"]) for call in self.hardware.supervisor.send.call_args_list]

    def worker(self, kind: str, **fields: object) -> None:
        self.runtime._on_worker_event(
            {
                "type": kind,
                "generation": self.hardware.supervisor.generation,
                "utterance_id": self.runtime.orchestrator._utterance_id,
                **fields,
            }
        )

    def close(self) -> None:
        self.runtime.shutdown()

    def publish(self, text: str) -> bool:
        self.published.append(text)
        return True


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch) -> Any:
    instance = Rig(monkeypatch)
    try:
        yield instance
    finally:
        instance.close()


def test_mouse_pending_reserves_input_before_keyboard_or_text(rig: Rig) -> None:
    rig.arm_mouse()
    rig.mouse_event("press")
    rig.key("command", "KeyPress")
    rig.key("text", "KeyPress")
    rig.key("command", "KeyRelease")
    rig.key("text", "KeyRelease")
    assert rig.commands() == []
    rig.hardware.now += 0.31
    rig.mouse.tick()
    assert rig.commands() == ["record.start"]
    rig.mouse_event("release")
    rig.hardware.fire_tail()
    rig.worker("result", text=PHRASE)
    assert len(rig.client.calls) == 1 and rig.client.calls[0][0] == PHRASE
    rig.hardware.paste.assert_not_called()


def test_default_mouse_off_creates_no_reader_and_keeps_text_dictation(rig: Rig) -> None:
    assert not rig.runtime.settings.command_mouse_enabled
    rig.runtime.reload_command_mouse()
    rig.mouse_factory.assert_not_called()
    assert rig.runtime.command_mouse is None and rig.pointer.grabs == []
    rig.key("text", "KeyPress")
    rig.hardware.now += 0.31
    rig.key("text", "KeyRelease")
    rig.hardware.fire_tail()
    rig.worker("result", text=PHRASE)
    rig.hardware.paste.assert_called_once()
    assert rig.client.calls == [] and rig.published == []


@pytest.mark.parametrize("source", ["command", "text"])
def test_mouse_release_and_cancel_cannot_stop_keyboard_owner(rig: Rig, source: str) -> None:
    rig.arm_mouse()
    rig.key(source, "KeyPress")
    assert rig.commands() == ["record.start"]
    rig.mouse_event("press")
    rig.mouse_event("release")
    rig.mouse.cancel("mapping-changed")
    rig.mouse.done(rig.mouse.generation - 1)
    assert rig.commands() == ["record.start"]
    assert rig.runtime.orchestrator.phase == DictationPhase.RECORDING
    rig.hardware.now += 0.31  # preserve the existing keyboard tap-to-toggle contract
    rig.key(source, "KeyRelease")
    rig.hardware.fire_tail()
    assert rig.commands() == ["record.start", "record.stop", "recognize"]


def test_escape_cancels_pending_and_old_deadline_cannot_open_audio(rig: Rig) -> None:
    rig.arm_mouse()
    rig.mouse_event("press")
    token = rig.mouse.generation
    assert rig.keys.escape
    rig.text.handle_event(HotkeyEvent("KeyPress", 9, 1, escape=True), rig.hardware.now)
    rig.hardware.now += 1
    rig.mouse.tick(generation=token)
    assert rig.commands() == [] and rig.pointer.registered is None
    assert not rig.keys.escape
    assert rig.client.calls == []


@pytest.mark.parametrize("elapsed", [0.299, 0.3, 1.0])
def test_release_queued_before_runtime_deadline_never_opens_audio(rig: Rig, elapsed: float) -> None:
    rig.arm_mouse()
    rig.mouse_event("press")
    token = rig.mouse.generation
    rig.pointer.held = False
    rig.pointer.events.append(MouseButtonEvent("release", 2))
    rig.hardware.now += elapsed
    rig.mouse.tick(generation=token)
    assert rig.commands() == [] and rig.client.calls == []
    assert not rig.keys.escape


@pytest.mark.parametrize("preview", [False, True])
def test_mouse_is_strict_hold_even_when_keyboard_is_toggle(
    monkeypatch: pytest.MonkeyPatch, preview: bool
) -> None:
    r = Rig(monkeypatch, keyboard_mode="toggle", preview=preview)
    try:
        r.arm_mouse()
        r.hold()
        assert r.commands() == ["record.start"]
        r.mouse_event("release")
        r.hardware.fire_tail()
        assert r.commands() == ["record.start", "record.stop", "recognize"]
        r.worker("result", text=PHRASE)
        if preview:
            assert r.runtime._preview_send is not None and r.client.calls == []
            r.runtime.command_preview_shown()
            r.runtime.confirm_command()
        assert len(r.client.calls) == 1 and r.client.calls[0][0] == PHRASE
        r.runtime.confirm_command()
        assert len(r.client.calls) == 1
        assert r.published == []
        r.hardware.paste.assert_not_called()
    finally:
        r.close()


def test_mouse_admission_precedes_win_confirmation_despite_earlier_raw_press(rig: Rig) -> None:
    rig.runtime.settings.command_hotkey = "Super_L"
    assert rig.runtime.reload_command_hotkey()
    rig.arm_mouse()
    rig.command.handle_event(HotkeyEvent("KeyPress", 133, 1), rig.hardware.now)
    rig.mouse_event("press")
    rig.hardware.now += 0.31
    rig.command.tick()
    rig.mouse.tick()
    assert rig.commands() == ["record.start"]
    rig.command.handle_event(HotkeyEvent("KeyRelease", 133, 2), rig.hardware.now)
    assert rig.commands() == ["record.start"]
    rig.mouse_event("release")
    rig.hardware.fire_tail()
    assert rig.commands() == ["record.start", "record.stop", "recognize"]


@pytest.mark.parametrize("lifecycle", ["ungrab", "reload", "failed-grab", "shutdown"])
def test_text_reader_lifecycle_revokes_pending_mouse_without_submission(
    rig: Rig, lifecycle: str
) -> None:
    rig.arm_mouse()
    rig.mouse_event("press")
    token = rig.mouse.generation
    assert rig.keys.escape
    if lifecycle == "ungrab":
        rig.text.ungrab()
    elif lifecycle == "shutdown":
        rig.runtime.shutdown()
    elif lifecycle == "reload":
        rig.text.grab("Ctrl+Space", HotkeyMode.PTT)
    else:
        rig.keys.result = "busy"
        rig.text.grab("Super_L", HotkeyMode.PTT)
    rig.hardware.now += 1
    rig.mouse.tick(generation=token)
    assert rig.commands() == [] and rig.client.calls == []
    assert rig.pointer.registered is None and not rig.keys.escape


def test_old_timer_escape_lease_and_worker_result_cannot_take_over_new_gesture(rig: Rig) -> None:
    rig.arm_mouse()
    rig.hold()
    old_timer = rig.mouse.generation
    old_escape = rig.text._external_escape_token
    old_utterance = rig.runtime.orchestrator._utterance_id
    rig.text.handle_event(HotkeyEvent("KeyPress", 9, 1, escape=True), rig.hardware.now)
    rig.worker("cancelled")
    rig.pointer.held = False
    rig.runtime.reload_command_mouse()
    rig.hold()
    assert rig.runtime.orchestrator._utterance_id != old_utterance
    before = rig.commands()
    rig.text.release_external_escape(old_escape)
    assert rig.keys.escape
    rig.mouse.tick(generation=old_timer)
    rig.runtime._on_worker_event(
        {
            "type": "result",
            "generation": rig.hardware.supervisor.generation,
            "utterance_id": old_utterance,
            "text": "stale private utterance",
        }
    )
    assert rig.commands() == before and rig.client.calls == []
    assert rig.runtime.orchestrator.phase == DictationPhase.RECORDING
    rig.mouse_event("release")
    rig.hardware.fire_tail()
    rig.worker("result", text=PHRASE)
    assert len(rig.client.calls) == 1 and rig.client.calls[0][0] == PHRASE


@pytest.mark.parametrize("phase", ["pending", "recording", "processing", "preview"])
@pytest.mark.parametrize("loss", ["model-trust", "epoch", "commands-off", "mouse-off", "absent"])
def test_loss_of_admission_during_gesture_prevents_late_submit_and_publication(
    monkeypatch: pytest.MonkeyPatch, phase: str, loss: str
) -> None:
    r = Rig(monkeypatch, preview=True)
    try:
        r.arm_mouse()
        r.mouse_event("press")
        token = r.mouse.generation
        if phase != "pending":
            r.hardware.now += 0.31
            r.mouse.tick()
        if phase in ("processing", "preview"):
            r.mouse_event("release")
            r.hardware.fire_tail()
        if phase == "preview":
            r.worker("result", text=PHRASE)
            r.runtime.command_preview_shown()
            assert r.runtime._preview_send is not None
        starts_before_loss = r.commands().count("record.start")
        if loss == "model-trust":
            r.record.metadata_ok = False
            r.worker("audio.ready")  # runtime rechecks trusted model before dispatch to core
        elif loss == "epoch":
            r.snapshot = replace(r.snapshot, blocked_epoch=r.snapshot.blocked_epoch + 1)
            r.runtime._command_session_changed()
        elif loss == "commands-off":
            r.runtime.settings.command_enabled = False
            r.runtime.reload_command_hotkey()
        elif loss == "mouse-off":
            r.runtime.settings.command_mouse_enabled = False
            r.runtime.reload_command_mouse()
        else:
            monkeypatch.setattr("astra_voice.runtime.is_installed", lambda: False)
            r.runtime.refresh_command_status()
        r.hardware.now += 1
        r.mouse.tick(generation=token)
        r.worker("result", text=PHRASE)
        r.runtime.confirm_command()
        assert r.client.calls == [] and r.published == [] and r.presented == []
        r.hardware.paste.assert_not_called()
        assert r.commands().count("record.start") == starts_before_loss
        if loss == "epoch" and not r.pointer.held:
            # A new allowed epoch may restore a passive registration once the
            # old button is up. This is distinct from retaining its active grab.
            assert r.mouse.state == MouseHoldState.IDLE and r.mouse.generation != token
        else:
            assert r.pointer.registered is None
    finally:
        r.close()


@pytest.mark.parametrize("phase", ["pending", "recording", "processing", "preview"])
@pytest.mark.parametrize(
    "closed",
    [
        SessionSnapshot(),
        SessionSnapshot(known=True, locked=True),
        SessionSnapshot(known=True, locked=False, active=False),
        SessionSnapshot(known=True, locked=False, preparing_for_sleep=True),
    ],
)
def test_session_loss_revokes_gesture_and_late_publication(
    monkeypatch: pytest.MonkeyPatch, phase: str, closed: SessionSnapshot
) -> None:
    r = Rig(monkeypatch, preview=True)
    try:
        r.arm_mouse()
        r.mouse_event("press")
        token = r.mouse.generation
        if phase != "pending":
            r.hardware.now += 0.31
            r.mouse.tick()
            assert "record.start" in r.commands()
        if phase in ("processing", "preview"):
            r.mouse_event("release")
            r.hardware.fire_tail()
        if phase == "preview":
            r.worker("result", text=PHRASE)
            r.runtime.command_preview_shown()
            assert r.runtime._preview_send is not None
        r.snapshot = closed
        r.runtime._command_session_changed()
        r.hardware.now += 1
        r.mouse.tick(generation=token)
        r.worker("result", text=PHRASE)
        r.runtime.confirm_command()
        assert r.pointer.registered is None
        assert r.client.calls == [] and r.published == [] and r.presented == []
        r.hardware.paste.assert_not_called()
        r.snapshot = replace(SessionSnapshot(known=True, locked=False), blocked_epoch=1)
        r.runtime._command_session_changed()
        r.mouse.tick(generation=token)
        r.runtime.confirm_command()
        assert r.client.calls == []
    finally:
        r.close()


@pytest.mark.parametrize(
    "denied", ["absent", "commands-off", "mouse-off", "untrusted", "model-missing", "selfcheck"]
)
def test_closed_admission_does_not_create_audio(rig: Rig, denied: str) -> None:
    if denied == "absent":
        rig.runtime.command_installed = False
    elif denied == "commands-off":
        rig.runtime.settings.command_enabled = False
    elif denied == "untrusted":
        rig.record.metadata_ok = False
    elif denied == "model-missing":
        rig.runtime._model_load_request = None
    elif denied == "selfcheck":
        rig.runtime._selfcheck = "failed"
    rig.runtime.settings.command_mouse_enabled = denied != "mouse-off"
    rig.runtime.reload_command_mouse()
    rig.mouse_event("press")
    rig.hardware.now += 1
    rig.mouse.tick()
    assert rig.commands() == [] and rig.client.calls == []
    assert rig.pointer.registered is None
