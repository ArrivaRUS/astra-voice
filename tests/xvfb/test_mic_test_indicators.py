"""Проверка микрофона с настоящей QML-пилюлей и стражем, без трея и звука."""

from __future__ import annotations

import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from unittest.mock import Mock

import pytest
from PyQt5 import sip
from PyQt5.QtCore import QEventLoop, QTimer
from PyQt5.QtWidgets import QApplication

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from astra_voice.core.dictation import (  # noqa: E402
    DictationOrchestrator,
    DictationPhase,
    MicrophoneTestUpdate,
)
from astra_voice.platform.paste import PasteMode  # noqa: E402
from astra_voice.platform.session import SessionKind  # noqa: E402
from astra_voice.ui import notify  # noqa: E402
from astra_voice.ui.indicators import IndicatorGuard  # noqa: E402
from astra_voice.ui.pill import Pill, PillState  # noqa: E402
from astra_voice.ui.tray import Tray  # noqa: E402
from astra_voice.ui.tray_icons import TrayState  # noqa: E402
from helpers.qt_app import get_qapplication  # noqa: E402

pytestmark = pytest.mark.xvfb


class AbsentTray:
    """Порт трея без регистрации, как в сеансе Fly без области уведомлений."""

    registered = False

    def __init__(self) -> None:
        self.state = TrayState.IDLE
        self.has_last_text = False

    def set_state(self, state: TrayState, tooltip: str | None = None) -> None:
        self.state = state

    def set_has_last_text(self, value: bool) -> None:
        self.has_last_text = value


@dataclass
class MicTestRig:
    app: QApplication
    core: DictationOrchestrator
    pill: Pill
    tray: AbsentTray
    guard: IndicatorGuard
    commands: list[dict[str, Any]]
    updates: list[MicrophoneTestUpdate]
    lost: Mock
    notice: Mock
    paste: Mock
    stats: Mock


def run_events(milliseconds: int) -> None:
    """Ждём реальное время, оставляя Qt доставлять expose и тики стража."""
    loop = QEventLoop()
    timer = QTimer()
    timer.setSingleShot(True)
    timer.timeout.connect(loop.quit)
    timer.start(milliseconds)
    loop.exec_()


@pytest.fixture
def mic_rig(monkeypatch: pytest.MonkeyPatch) -> Iterator[MicTestRig]:
    app = get_qapplication()
    pill = Pill(session=SessionKind.FLY)
    tray = AbsentTray()
    guard = IndicatorGuard(pill, cast(Tray, tray))
    commands: list[dict[str, Any]] = []
    updates: list[MicrophoneTestUpdate] = []
    timers: list[QTimer] = []
    notice = Mock()
    monkeypatch.setattr(notify, "notify_indicators_lost", notice)
    paste, stats = Mock(), Mock()

    def schedule(milliseconds: int, callback: Callable[[], None]) -> object:
        timer = QTimer()
        timer.setSingleShot(True)
        timer.timeout.connect(callback)
        timers.append(timer)
        timer.start(milliseconds)
        return timer

    def cancel_timer(handle: object) -> None:
        assert isinstance(handle, QTimer)
        handle.stop()

    def send(message: dict[str, Any], *, timeout: float | None = None) -> None:
        commands.append(message)
        # Только транспорт/микрофон подставные: автомат сам получает ACK отмены.
        if message["type"] == "record.cancel":
            core.on_worker_event(
                {"type": "cancelled", "generation": 1, "utterance_id": message["utterance_id"]}
            )

    core = DictationOrchestrator(
        send=send,
        generation=lambda: 1,
        restart_worker=Mock(),
        pill=pill,
        tray=tray,
        paste=paste,
        active_window=Mock(side_effect=AssertionError("Проверка не читает фокус")),
        schedule=schedule,
        cancel_timer=cancel_timer,
        hotkey_done=Mock(),
        hotkey_cancel=Mock(),
        hotkey_idle=lambda: True,
        set_recording=guard.set_recording,
        paste_mode=lambda: PasteMode.AUTO,
        record_params=Mock(side_effect=AssertionError("Проверка использует свой микрофон")),
        stats=stats,
    )
    lost = Mock(wraps=core.on_indicators_lost)
    guard.on_stop_recording = lost
    try:
        yield MicTestRig(
            app, core, pill, tray, guard, commands, updates, lost, notice, paste, stats
        )
    finally:
        core.shutdown()
        guard.set_recording(False)
        for timer in timers:
            timer.stop()
            sip.delete(timer)
        view = pill._view
        sip.delete(guard)
        sip.delete(pill)
        sip.delete(view)
        app.processEvents()


@pytest.mark.parametrize("pill_enabled", [True, False])
def test_microphone_test_keeps_recording_without_tray(
    mic_rig: MicTestRig, pill_enabled: bool
) -> None:
    rig = mic_rig
    rig.pill.set_enabled(pill_enabled)
    assert not rig.tray.registered
    assert rig.core.start_test("", rig.updates.append)
    assert rig.pill.state is PillState.LISTENING
    run_events(2100)
    assert rig.core.phase is DictationPhase.RECORDING
    assert rig.core.test_active and rig.guard.recording
    assert rig.pill.state is PillState.LISTENING
    assert rig.pill.visible and rig.guard.ok
    assert [message["type"] for message in rig.commands] == ["record.start"]
    rig.lost.assert_not_called()
    rig.notice.assert_not_called()
    rig.paste.assert_not_called()
    rig.stats.append.assert_not_called()
    assert rig.core.last_text is None
    rig.core.cancel_test()
    assert rig.core.phase.value == "idle"
    assert not rig.pill.visible
    rig.pill.set_enabled(True)
    assert rig.pill.state is PillState.HIDDEN
    assert not rig.pill.visible


def test_hidden_pill_and_absent_tray_stop_microphone_test(mic_rig: MicTestRig) -> None:
    rig = mic_rig
    assert rig.core.start_test("", rig.updates.append)
    rig.pill.hide()
    assert not rig.pill.visible and not rig.tray.registered
    run_events(2100)
    rig.lost.assert_called_once_with()
    rig.notice.assert_called_once_with()
    assert [message["type"] for message in rig.commands] == ["record.start", "record.cancel"]
    assert rig.core.phase.value == "idle"
    assert not rig.core.test_active and not rig.guard.recording
    assert rig.pill.state is PillState.HIDDEN
    assert not rig.pill.visible
    rig.paste.assert_not_called()
    rig.stats.append.assert_not_called()
    assert rig.core.last_text is None
