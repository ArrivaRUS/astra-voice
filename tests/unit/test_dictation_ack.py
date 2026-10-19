"""Подтверждения закрытия: настоящий оркестратор, страж и ручная доставка IPC."""

from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING, cast
from unittest.mock import Mock

import pytest
from test_dictation import Rig
from test_indicators import FakeTimer

from astra_voice.core.dictation import (
    CANCEL_RESTART_MS,
    CANCEL_TIMEOUT_MS,
    DictationPhase,
    MicrophoneLevelUpdate,
)
from astra_voice.platform.hotkey import HotkeyState
from astra_voice.ui import indicators
from astra_voice.ui.indicators import IndicatorGuard
from astra_voice.ui.pill import STATE_DURATION_MS, PillState
from astra_voice.ui.tray_icons import TrayState

if TYPE_CHECKING:
    from astra_voice.ui.pill import Pill
    from astra_voice.ui.tray import Tray

pytestmark = pytest.mark.unit


@pytest.fixture
def guarded_rig(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[Rig, IndicatorGuard]]:
    """Настоящий IndicatorGuard; UI и таймеры без окон, звука и D-Bus."""
    rig = Rig()
    monkeypatch.setattr(indicators, "QTimer", FakeTimer)
    monkeypatch.setattr(rig.pill, "set_forced", Mock(), raising=False)
    monkeypatch.setattr(rig.tray, "registered", True, raising=False)
    guard = IndicatorGuard(cast("Pill", rig.pill), cast("Tray", rig.tray))

    def recording(value: bool) -> None:
        rig.set_recording(value)
        guard.set_recording(value)

    rig.core._set_recording = recording
    try:
        yield rig, guard
    finally:
        guard.stop()


@pytest.mark.parametrize("request_type", [None, "record.start", "record.stop", "recognize"])
@pytest.mark.parametrize("kind", ["cancelled", "error"])
def test_cancel_ack_requires_worker_request_type(
    guarded_rig: tuple[Rig, IndicatorGuard], request_type: str | None, kind: str
) -> None:
    rig, guard = guarded_rig
    rig.start()
    rig.core.cancel("tray")
    event: dict[str, object] = {
        "type": kind,
        "utterance_id": rig.uid,
        "generation": rig.worker_generation,
        "code": "bad-state",
    }
    if request_type is not None:
        event["request_type"] = request_type
    rig.core.on_worker_event(event)
    assert rig.core.cleanup_pending and rig.recording and guard.recording
    assert rig.tray.state is TrayState.LISTENING and not rig.pill.hidden
    rig.core.on_worker_event({**event, "request_type": "record.cancel"})
    assert not rig.core.cleanup_pending and not rig.recording and not guard.recording
    rig.restart_worker.assert_not_called()


@pytest.mark.parametrize("kind", ["cancelled", "error", "audio.closed", "restart"])
def test_level_indicators_wait_for_close_ack_or_completed_restart(
    guarded_rig: tuple[Rig, IndicatorGuard], kind: str
) -> None:
    """ИБ-50: отмена и таймаут не выключают страж над незакрытым источником."""
    rig, guard = guarded_rig
    updates: list[MicrophoneLevelUpdate] = []
    source_open = False

    def send(message: dict[str, object]) -> None:
        nonlocal source_open
        if message["type"] == "record.start":
            source_open = True
        # record.cancel/audio.close запросили закрытие, но оно ещё заблокировано.

    def restart() -> None:
        nonlocal source_open
        assert rig.recording and guard.recording and source_open
        source_open = False

    rig.during_send = send
    rig.restart_worker.side_effect = restart
    assert rig.core.start_level_monitor("fake-mic", updates.append)
    rig.core.stop_level_monitor()
    assert source_open and rig.recording and guard.recording
    assert not rig.core.level_active and rig.core.cleanup_pending
    assert not rig.pill.hidden and rig.tray.state is TrayState.LISTENING
    assert updates == [MicrophoneLevelUpdate("listening")]
    if kind in ("audio.closed", "restart"):
        rig.timer(CANCEL_TIMEOUT_MS).fire()
        assert rig.commands()[-1] == "audio.close"
        assert source_open and rig.recording and guard.recording
        assert not rig.pill.hidden and rig.tray.state is TrayState.LISTENING
    if kind == "restart":
        rig.timer(CANCEL_RESTART_MS).fire()
        rig.restart_worker.assert_called_once_with()
    else:
        # Ответ отправлен после успешного физического закрытия этой фразы.
        source_open = False
        rig.core.on_worker_event(
            {
                "type": kind,
                "utterance_id": rig.uid,
                "generation": rig.worker_generation,
                "request_type": "audio.close" if kind == "audio.closed" else "record.cancel",
                "code": "bad-state",
            }
        )
        rig.restart_worker.assert_not_called()
    assert not source_open and not rig.recording and not guard.recording
    assert rig.pill.hidden and rig.tray.state is TrayState.IDLE
    assert updates[-1] == MicrophoneLevelUpdate("idle")
    assert not rig.core.cleanup_pending and rig.core.phase is DictationPhase.IDLE
    assert not [timer for timer in rig.timers if not timer.cancelled and not timer.fired]


@pytest.mark.parametrize("kind", ["cancelled", "error", "audio.closed"])
@pytest.mark.parametrize("request_type", [None, "record.stop", "recognize"])
def test_level_rejects_untyped_or_unrelated_close_reply(
    guarded_rig: tuple[Rig, IndicatorGuard], kind: str, request_type: str | None
) -> None:
    rig, guard = guarded_rig
    updates: list[MicrophoneLevelUpdate] = []
    assert rig.core.start_level_monitor("fake-mic", updates.append)
    rig.core.stop_level_monitor()
    rig.timer(CANCEL_TIMEOUT_MS).fire()
    event: dict[str, object] = {
        "type": kind,
        "generation": rig.worker_generation,
        "utterance_id": rig.uid,
        "code": "bad-state",
    }
    if request_type is not None:
        event["request_type"] = request_type
    rig.core.on_worker_event(event)
    assert rig.core.cleanup_pending and rig.recording and guard.recording
    assert not rig.pill.hidden and rig.tray.state is TrayState.LISTENING
    assert updates == [MicrophoneLevelUpdate("listening")]
    rig.core.on_worker_event(
        {"type": "audio.closed", "request_type": "audio.close", "generation": 1}
    )
    assert not rig.core.cleanup_pending and not rig.recording and not guard.recording


def test_level_failed_restart_keeps_indicators_and_cleanup_until_worker_is_stopped(
    guarded_rig: tuple[Rig, IndicatorGuard], caplog: pytest.LogCaptureFixture
) -> None:
    rig, guard = guarded_rig
    updates: list[MicrophoneLevelUpdate] = []
    assert rig.core.start_level_monitor("fake-mic", updates.append)
    rig.core.stop_level_monitor()
    rig.timer(CANCEL_TIMEOUT_MS).fire()
    rig.restart_worker.side_effect = RuntimeError("PRIVATE-RESTART-DETAIL")
    rig.timer(CANCEL_RESTART_MS).fire()
    assert rig.core.cleanup_pending and rig.recording and guard.recording
    assert not rig.pill.hidden and rig.tray.state is TrayState.LISTENING
    assert updates[-1].state == "error"
    assert not rig.core.start_level_monitor("fake-mic", Mock())
    rig.restart_worker.side_effect = None
    rig.timer(CANCEL_RESTART_MS).fire()
    assert not rig.core.cleanup_pending and not rig.recording and not guard.recording
    assert rig.pill.hidden and rig.tray.state is TrayState.IDLE
    assert "PRIVATE-RESTART-DETAIL" not in caplog.text


@pytest.mark.parametrize("ack", ["record.stopped", "cancelled", "bad-state"])
@pytest.mark.parametrize("tail_elapsed", [False, True])
def test_terminal_capture_ack_preserves_one_original_finish_timer(
    ack: str, tail_elapsed: bool
) -> None:
    rig = Rig()
    rig.start()
    rig.event("error", code="audio-failed")
    duration = STATE_DURATION_MS[PillState.ERROR]
    original = rig.timer(duration)
    if tail_elapsed:
        original.fire()
        assert rig.recording and rig.tray.state is TrayState.LISTENING
    if ack == "bad-state":
        rig.event("error", code="bad-state", request_type="record.cancel")
    else:
        rig.event(ack)
    assert len([timer for timer in rig.timers if timer.delay == duration]) == 1
    assert not rig.core.cleanup_pending
    if not tail_elapsed:
        assert rig.timer(duration) is original
        assert rig.core.phase is DictationPhase.FINISHING
        original.fire()
    assert rig.core.phase is DictationPhase.IDLE
    assert rig.tray.state is TrayState.IDLE


@pytest.mark.parametrize("next_recording", [False, True])
def test_async_job_cancel_requests_own_close_ack_without_touching_next_phrase(
    guarded_rig: tuple[Rig, IndicatorGuard],
    next_recording: bool,
) -> None:
    rig, guard = guarded_rig
    rig.ack_stop = next_recording
    rig.start()
    first_uid = rig.uid
    rig.stop()
    if next_recording:
        rig.now += 1
        rig.start()
        assert rig.uid != first_uid
    assert guard.recording and rig.recording
    rig.core.on_worker_event(
        {
            "type": "cancelled",
            "utterance_id": first_uid,
            "request_type": "recognize",
            "generation": rig.worker_generation,
        }
    )
    assert rig.sent[-1][0] == {"type": "record.cancel", "utterance_id": first_uid}
    if next_recording:
        assert rig.fsm.state is HotkeyState.RECORDING
    assert guard.recording and rig.recording
    assert rig.core.cleanup_pending
    rig.core.on_worker_event(
        {
            "type": "cancelled",
            "utterance_id": first_uid,
            "request_type": "record.cancel",
            "generation": rig.worker_generation,
        }
    )
    assert guard.recording is next_recording
    assert rig.recording is next_recording
    assert not any(c._utterance_id == first_uid and c._cancel_pending for c in rig.core._contexts)
    assert rig.core.cleanup_pending is next_recording
    if next_recording:
        assert rig.fsm.state is HotkeyState.RECORDING
        next_uid = rig.uid
        rig.stop()
        assert {"type": "record.stop", "utterance_id": next_uid} in [m for m, _ in rig.sent]
        assert {"type": "recognize", "utterance_id": next_uid} in [m for m, _ in rig.sent]
    assert not rig.pasted
    rig.restart_worker.assert_not_called()


def test_audio_closed_does_not_ack_running_recognition_cancel(
    guarded_rig: tuple[Rig, IndicatorGuard],
) -> None:
    rig, guard = guarded_rig
    rig.ack_stop = False
    rig.start()
    rig.stop()
    rig.core.cancel("tray")
    rig.timer(CANCEL_TIMEOUT_MS).fire()
    assert guard.recording
    rig.core.on_worker_event(
        {
            "type": "audio.closed",
            "request_type": "audio.close",
            "generation": rig.worker_generation,
        }
    )
    assert not guard.recording and not rig.recording
    assert rig.core.cleanup_pending
    assert not rig.timer(CANCEL_RESTART_MS).cancelled
    rig.event("cancelled", request_type="record.cancel")
    assert not rig.core.cleanup_pending
    assert not any(
        t.delay == CANCEL_RESTART_MS and not t.cancelled and not t.fired for t in rig.timers
    )
    rig.restart_worker.assert_not_called()
