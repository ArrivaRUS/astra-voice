"""E13 tray menu contract, without registration or an actual recorder."""

from __future__ import annotations

from unittest.mock import Mock

import pytest
from test_tray import Harness
from test_tray import forbid_real_x as forbid_real_x
from test_tray import harness as harness
from test_tray import qapp as qapp

from astra_voice.platform.session import SessionKind
from astra_voice.ui.tray_icons import TrayIconProvider, TrayState

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def meeting_tooltip_contract(harness: Harness) -> None:
    # Existing legacy harness predates the optional meeting clock parameter.
    harness.provider.tooltip.side_effect = TrayIconProvider(SessionKind.OTHER).tooltip


def labels(h: Harness) -> list[str]:
    return [
        action.text()
        for action in h.menu.actions()
        if action.isVisible() and not action.isSeparator()
    ]


def test_meeting_actions_appear_only_when_user_enabled(harness: Harness) -> None:
    h = harness
    before = labels(h)
    assert "Записать встречу" not in before
    h.tray.set_meetings_enabled(True)
    assert labels(h).count("Записать встречу") == 1 and "Встречи…" in labels(h)
    h.tray.set_meetings_enabled(False)
    assert labels(h) == before


@pytest.mark.parametrize("paused", [False, True])
def test_recording_menu_is_first_block_with_pause_stop_and_notice(
    harness: Harness, paused: bool
) -> None:
    h = harness
    state = TrayState.MEETING_PAUSED if paused else TrayState.MEETING_REC
    h.tray.set_meeting_state(state, elapsed_seconds=2530)
    assert labels(h)[:4] == [
        "● Запись встречи на паузе · 0:42:10" if paused else "● Идёт запись встречи · 0:42:10",
        "Продолжить" if paused else "Пауза",
        "Остановить запись встречи",
        "Текст для участников…",
    ]
    assert not h.menu.actions()[0].isEnabled()
    assert "Записать встречу" not in labels(h)
    assert "Встречи…" in labels(h)


def test_conditional_actions_are_present_only_when_available(harness: Harness) -> None:
    h = harness
    h.tray.set_meeting_state(
        TrayState.MEETING_REC, limit_soon=True, can_extend=True, mic_name="Встроенный микрофон"
    )
    assert labels(h)[1:3] == ["Продлить на час", "Продолжить с «Встроенный микрофон»"]
    h.tray.set_meeting_state(TrayState.MEETING_REC, limit_soon=True, can_extend=False)
    assert "Продлить на час" not in labels(h)
    assert not any(label.startswith("Продолжить с") for label in labels(h))


def test_tray_recorder_actions_are_callbacks_only(harness: Harness) -> None:
    h = harness
    h.tray.on_meeting_record = Mock()
    h.tray.on_meeting_pause = Mock()
    h.tray.on_meeting_stop = Mock()
    h.tray.on_meeting_notice = Mock()
    h.tray.on_meeting_open = Mock()
    h.tray.set_meetings_enabled(True)
    next(a for a in h.menu.actions() if a.text() == "Записать встречу").trigger()
    h.tray.on_meeting_record.assert_called_once_with()
    h.tray.set_meeting_state(TrayState.MEETING_REC)
    for text in ["Пауза", "Остановить запись встречи", "Текст для участников…", "Встречи…"]:
        next(a for a in h.menu.actions() if a.text() == text).trigger()
    h.tray.on_meeting_pause.assert_called_once_with()
    h.tray.on_meeting_stop.assert_called_once_with()
    h.tray.on_meeting_notice.assert_called_once_with()
    h.tray.on_meeting_open.assert_called_once_with()


def test_dictation_overlays_then_restores_meeting_without_losing_pause(harness: Harness) -> None:
    h = harness
    h.tray.set_meeting_state(TrayState.MEETING_PAUSED, elapsed_seconds=10)
    for state in [TrayState.LISTENING, TrayState.PROCESSING, TrayState.DONE]:
        h.tray.set_state(state)
        assert h.tray._state is state
        assert "Остановить запись встречи" in labels(h)
    h.tray._done_expired()
    assert h.tray._state is TrayState.MEETING_PAUSED
    h.tray.set_meeting_state(None)
    assert h.tray._state.value == "idle"


@pytest.mark.parametrize(
    "state,name,tooltip",
    [
        ("MEETING_REC", "meeting-rec", "Идёт запись встречи · 0:42:10"),
        ("MEETING_PAUSED", "meeting-paused", "Запись встречи на паузе · 0:42:10"),
    ],
)
def test_meeting_icon_names_and_tooltips(state: str, name: str, tooltip: str) -> None:
    provider = TrayIconProvider(SessionKind.OTHER)
    status = getattr(TrayState, state)
    assert provider.icon_name(status) == "astravoice-tray-" + name
    assert provider.tooltip(status, elapsed_seconds=2530) == tooltip


def test_limit_soon_status_and_four_hour_ceiling(harness: Harness) -> None:
    h = harness
    h.tray.set_meeting_state(
        TrayState.MEETING_REC,
        elapsed_seconds=6912,
        limit_soon=True,
        can_extend=True,
        limit_remaining_seconds=288,
    )
    assert labels(h)[0] == "● Идёт запись встречи · 1:55:12 · остановится через 4:48"
    assert "Продлить на час" in labels(h)
    h.tray.set_meeting_state(
        TrayState.MEETING_REC, elapsed_seconds=14400, limit_soon=True, can_extend=True
    )
    assert "Продлить на час" not in labels(h)
