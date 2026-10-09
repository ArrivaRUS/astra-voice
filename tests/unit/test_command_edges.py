"""Adversarial command transitions with fake input/transport; no native grabs."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock

import pytest
from PyQt5.QtCore import QCoreApplication, Qt
from test_runtime import Rig

from astra_voice.core.command_mode import CommandMode, SessionSnapshot
from astra_voice.core.settings import from_dict
from astra_voice.models.store import ModelRecord
from astra_voice.platform.cowork import DeliveryResult
from astra_voice.platform.hotkey import (
    GrabResult,
    HotkeyBackend,
    HotkeyEvent,
    HotkeyManager,
    HotkeyMode,
    HotkeyState,
)
from astra_voice.platform.session import SessionKind
from astra_voice.platform.session_state import SessionMonitor
from astra_voice.runtime import DictationRuntime

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("sleep", [False, True])
def test_queued_block_and_restore_cannot_lose_preview_invalidation(
    monkeypatch: pytest.MonkeyPatch, sleep: bool
) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; sys.path[:0] = ['src', 'tests/unit']; "
            "from PyQt5.QtCore import QCoreApplication; app = QCoreApplication([]); "
            "from test_command_edges import _queued_transition; "
            "import pytest; " + f"_queued_transition(pytest.MonkeyPatch(), {sleep!r})",
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def _queued_transition(monkeypatch: pytest.MonkeyPatch, sleep: bool) -> None:
    rig = Rig(monkeypatch, session_kind=SessionKind.KDE)
    runtime = rig.runtime
    monitor = SessionMonitor()
    runtime._command_session = monitor
    runtime.command_hotkey = Mock()
    runtime.command_hotkey.signature.return_value = (133, 0)
    runtime.command_hotkey.fileno.return_value = -1
    runtime._preview_send = Mock()
    sent = runtime._preview_send
    runtime.on_command_preview = Mock()
    monitor.changed.connect(runtime._command_session_changed, Qt.QueuedConnection)
    blocked = SessionSnapshot(
        known=True, locked=True, preparing_for_sleep=sleep, blocked_epoch=1, sleep_epoch=int(sleep)
    )
    restored = SessionSnapshot(known=True, locked=False, blocked_epoch=1, sleep_epoch=int(sleep))
    monitor._worker.snapshot = blocked
    monitor._worker.updated.emit(blocked)
    monitor._worker.snapshot = restored
    monitor._worker.updated.emit(restored)
    assert runtime._preview_send is sent  # GUI has not processed either transition yet.
    QCoreApplication.processEvents()
    QCoreApplication.processEvents()
    assert runtime._preview_send is None
    runtime.on_command_preview.assert_called_with("")
    runtime.confirm_command()
    sent.assert_not_called()
    runtime.shutdown()


@pytest.mark.parametrize("sleep", [False, True])
@pytest.mark.parametrize("stage", ["before_submit", "after_submit"])
def test_epoch_latch_closes_old_capture_even_when_session_is_already_allowed(
    sleep: bool, stage: str
) -> None:
    snapshot = SessionSnapshot(known=True, locked=False)
    client, publish, present = Mock(), Mock(return_value=True), Mock()
    mode = CommandMode(
        client=client,
        session=lambda: snapshot,
        model_trusted=lambda: True,
        publish=publish,
        present=present,
    )
    mode.begin()
    results: list[DeliveryResult] = []
    if stage == "after_submit":
        mode.deliver("синтетическая команда", results.append)
    snapshot = SessionSnapshot(known=True, locked=False, blocked_epoch=1, sleep_epoch=int(sleep))
    assert mode.allowed and not mode.publication_allowed
    if stage == "before_submit":
        mode.deliver("синтетическая команда", results.append)
        client.submit.assert_not_called()
    else:
        client.submit.call_args.args[1](DeliveryResult("unknown", "timeout"))
    publish.assert_not_called()
    present.assert_not_called()
    assert len(results) == 1
    if sleep:
        assert results[0].reason == "suspended"
    # Explicit recovery after unlock is permitted, without a new Submit.
    assert mode.recover()
    publish.assert_called_once_with("синтетическая команда")


@pytest.mark.parametrize("mutation", ["generation", "manual", "request", "revocation", "loading"])
def test_model_trust_rechecks_identity_after_store_io(tmp_path: Path, mutation: str) -> None:
    request = {"id": "model", "revision": "r1", "dir": str(tmp_path)}
    runtime = SimpleNamespace(
        settings=from_dict({}),
        _model_load_request=request,
        _model_load_generation=7,
        _command_capture_model=(7, dict(request)),
        supervisor=SimpleNamespace(generation=7),
        revocation_unknown=False,
        _revoked_check=lambda *args: False,
        _loading_model=False,
    )

    def current() -> ModelRecord:
        if mutation == "generation":
            runtime.supervisor.generation += 1
        elif mutation == "manual":
            runtime.settings = from_dict({"model_dir": "/tmp/manual"})
        elif mutation == "request":
            runtime._model_load_request = {**request, "revision": "r2"}
        elif mutation == "loading":
            runtime._loading_model = True
        else:
            runtime.revocation_unknown = True
        return ModelRecord("model", "r1", tmp_path, "flat", "rnnt", 123)

    runtime.model_store = SimpleNamespace(current=current)
    assert not DictationRuntime._command_model_trusted(cast(Any, runtime))


def manager(mode: HotkeyMode = HotkeyMode.PTT) -> HotkeyManager:
    backend = Mock(spec=HotkeyBackend)
    backend.grab_combo.return_value = GrabResult("ok", keycode=133)
    backend.grab_escape.return_value = GrabResult("ok", keycode=9)
    backend.poll_events.return_value = []
    result = HotkeyManager(backend)
    result.defer_single_super = True
    result.on_state = Mock()
    result.on_press = Mock()
    assert result.grab("Super_L", mode).ok
    return result


def test_single_win_short_tap_never_enters_recording_or_toggle() -> None:
    hotkey = manager()
    hotkey.handle_event(HotkeyEvent("KeyPress", 133, 1), 1.0)
    hotkey.tick(1.299)
    hotkey.handle_event(HotkeyEvent("KeyRelease", 133, 2), 1.299)
    hotkey.tick(2.0)
    assert hotkey.fsm.state == HotkeyState.IDLE
    assert hotkey.fsm.mode == HotkeyMode.PTT
    cast(Mock, hotkey.on_state).assert_not_called()
    cast(Mock, hotkey.on_press).assert_not_called()


def test_single_win_hold_starts_once_then_release_processes() -> None:
    hotkey = manager()
    hotkey.handle_event(HotkeyEvent("KeyPress", 133, 1), 1.0)
    hotkey.tick(1.3)
    hotkey.tick(1.5)
    assert hotkey.fsm.state == HotkeyState.RECORDING
    cast(Mock, hotkey.on_press).assert_called_once()
    hotkey.handle_event(HotkeyEvent("KeyRelease", 133, 2), 1.6)
    assert str(hotkey.fsm.state.value) == "processing"


def test_other_key_or_ungrab_cancels_pending_super_hold() -> None:
    for ungrab in (False, True):
        hotkey = manager()
        hotkey.handle_event(HotkeyEvent("KeyPress", 133, 1), 1.0)
        if ungrab:
            hotkey.ungrab()
        else:
            hotkey.handle_event(HotkeyEvent("KeyPress", 65, 2), 1.1)
        hotkey.tick(1.5)
        assert hotkey.fsm.state == HotkeyState.IDLE
        cast(Mock, hotkey.on_press).assert_not_called()


def test_explicit_toggle_keeps_immediate_press_behavior() -> None:
    hotkey = manager(HotkeyMode.TOGGLE)
    hotkey.handle_event(HotkeyEvent("KeyPress", 133, 1), 1.0)
    hotkey.handle_event(HotkeyEvent("KeyRelease", 133, 2), 1.1)
    assert hotkey.fsm.state == HotkeyState.RECORDING
    assert hotkey.fsm.mode == HotkeyMode.TOGGLE


@pytest.mark.parametrize("raises", [False, True])
def test_worker_model_guard_closes_gap_after_initial_admission(
    monkeypatch: pytest.MonkeyPatch, raises: bool
) -> None:
    from astra_voice.platform import cowork

    worker = cowork._Worker("unix:path=/synthetic")
    worker.connection = Mock()
    worker.connection.isConnected.return_value = True
    worker.owner = ":1.23"
    trusted = True

    def admission() -> bool:
        nonlocal trusted
        # Represents a queued GUI setting/generation change after deliver().
        trusted = False
        return True

    def model_guard() -> bool:
        if raises:
            raise RuntimeError("synthetic store error")
        return trusted

    worker.admission_guard = admission
    worker.model_guard = model_guard
    monkeypatch.setattr(cowork, "_Call", Mock(return_value=Mock()))
    monkeypatch.setattr(cowork, "method", Mock())
    results: list[DeliveryResult] = []
    worker.result.connect(lambda serial, result: results.append(result))
    now = cowork.monotonic_ms()
    worker.submit(
        1,
        "синтетическая команда",
        (
            {
                "request_id": "a" * 32,
                "deadline_mono_ms": now + 300,
                "sent_mono_ms": now,
                "sent_boot_ms": now,
            },
            cowork._Ticket(),
        ),
    )
    worker.connection.callWithCallback.assert_not_called()
    assert results == [DeliveryResult("undelivered", "model_untrusted")]
