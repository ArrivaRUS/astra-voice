"""M2 row states use real meeting presentation and established icon glyphs."""

from __future__ import annotations

from typing import Any
from unittest.mock import Mock

import pytest
from test_meetings_section import grab, visual_named
from test_meetings_section import meetings_app as meetings_app
from test_meetings_section import rendered as rendered
from test_start_dialog import SHOTS, texts

from astra_voice.core.settings import Settings
from astra_voice.meetings.events import MeetingState
from astra_voice.meetings.model import Mark, Meeting
from astra_voice.platform.session import SessionKind
from astra_voice.ui.meeting_bridge import MeetingBridge
from helpers.snapshots import write_snapshot

pytestmark = pytest.mark.xvfb


def make_bridge(state: MeetingState) -> MeetingBridge:
    meeting = Meeting(
        id="2026-11-01_120000-ab12",
        title="Планирование недели",
        started_at="2026-11-01T12:00:00Z",
        recorded_ms=2530000,
        state=state,
        marks=[Mark(2530000, "pause", "lock")] if state == MeetingState.PAUSED else [],
    )
    return MeetingBridge(
        Settings(meetings_enabled=True),
        known_apps=(),
        store=Mock(list=Mock(return_value=[meeting])),
        save=Mock(),
        submit=lambda work: work(),
    )


@pytest.mark.parametrize(
    "state,badge,glyph",
    [
        (MeetingState.RECORDING, "Запись", "rec"),
        (MeetingState.PAUSED, "Пауза", "pause"),
        (MeetingState.RECORDED, "Записана", ""),
        (MeetingState.INTERRUPTED, "Прервана", ""),
    ],
)
def test_m2_rows_literal_state_and_real_glyph(
    rendered: Any,
    state: MeetingState,
    badge: str,
    glyph: str,
) -> None:
    bridge = make_bridge(state)
    try:
        window, _bridge, messages = rendered("success", False, bridge)
        values = texts(window.contentItem())
        assert badge in values
        if glyph:
            symbol = visual_named(window, "meetingBadgeSymbol")
            assert symbol is not None and symbol.property("name") == glyph
            assert symbol.property("visible")
            assert "· 0:42:10" in values
        if state == MeetingState.PAUSED:
            assert "Экран был заблокирован — продолжите вручную" in values
        if state == MeetingState.INTERRUPTED:
            assert "Сохранено 0:42:10 — программа закрылась во время записи" in values
        if state in {MeetingState.RECORDED, MeetingState.INTERRUPTED}:
            action = visual_named(window, "meetingRowAction")
            assert action is not None and action.property("text") == "Расшифровать"
            assert not action.property("enabled")
        assert not messages
    finally:
        bridge.shutdown()


@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
def test_m2_section_recording_row_snapshot(rendered: Any, dark: bool) -> None:
    bridge = make_bridge(MeetingState.RECORDING)
    try:
        window, _bridge, messages = rendered("success", dark, bridge)
        window.setWidth(1280)
        window.setHeight(720)
        from PyQt5.QtTest import QTest

        QTest.qWait(30)
        write_snapshot(
            SHOTS, f"meetings-recording-{'dark' if dark else 'light'}-1280.png", grab(window)
        )
        assert not messages
    finally:
        bridge.shutdown()


@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
@pytest.mark.parametrize("screen", ["section", "responsibility"])
def test_fly_section_and_responsibility_have_no_lock_banner(
    rendered: Any, dark: bool, screen: str
) -> None:
    from PyQt5.QtCore import QObject
    from PyQt5.QtTest import QTest

    bridge = MeetingBridge(
        Settings(),
        session=SessionKind.FLY,
        known_apps=(),
        store=Mock(list=Mock(return_value=[])),
        save=Mock(),
        submit=lambda work: work(),
    )
    try:
        window, _bridge, messages = rendered("off", dark, bridge)
        if screen == "responsibility":
            bridge.openResponsibility()
            QTest.qWait(30)
            assert bridge.responsibilityOpen
            content = window.findChild(QObject, "meetingResponsibility").property("contentItem")
        else:
            content = window.contentItem()
        assert not any("блокировка экрана" in value for value in texts(content))
        write_snapshot(SHOTS, f"fly-{screen}-{'dark' if dark else 'light'}.png", grab(window))
        assert not messages
    finally:
        bridge.shutdown()
