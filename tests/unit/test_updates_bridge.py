"""Мост обновлений: состояния строки, «Что нового» как простой текст, действия."""

from __future__ import annotations

import pytest

pytest.importorskip("requests")

# ruff: noqa: E402
from collections.abc import Iterator
from datetime import datetime
from typing import Any
from unittest.mock import Mock

from PyQt5 import sip
from PyQt5.QtTest import QSignalSpy

from astra_voice.ui.updates_bridge import (
    MAX_NOTES_DISPLAY_CHARS,
    STATES,
    UpdatesBridge,
    checked_text,
    display_notes,
)
from astra_voice.updates.checker import UpdateStatus

pytestmark = pytest.mark.unit

RELEASE = "https://github.com/ArrivaRUS/astra-voice/releases/tag/v0.2.1"
NOW = datetime(2026, 9, 29, 15, 0).timestamp()


def make_bridge(
    refusals: dict[str, str] | None = None, *, open_external: Mock | None = None
) -> tuple[UpdatesBridge, Mock]:
    checker = Mock(spec=["check_now", "refresh", "skip_version", "clear_skip", "remind_later"])
    table = refusals if refusals is not None else {}
    bridge = UpdatesBridge(
        checker,
        refusal=lambda kind: table.get(kind, ""),
        open_external=open_external,
        clock=lambda: NOW,
    )
    return bridge, checker


def test_default_is_disabled_without_checker() -> None:
    bridge = UpdatesBridge()
    assert bridge.state == "disabled"
    assert bridge.canCheckNow is False
    assert bridge.releasePageAvailable is False
    bridge.checkNow()
    bridge.skipVersion()
    bridge.openReleasePage()


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (UpdateStatus("disabled"), "disabled"),
        (UpdateStatus("policy-locked"), "policy-locked"),
        (UpdateStatus("checking", manual=True), "checking"),
        (UpdateStatus("uptodate", manual=True), "uptodate"),
        # «Установлена последняя версия» — только после ручной проверки.
        (UpdateStatus("uptodate"), "idle"),
        (UpdateStatus("available", version="0.2.1"), "available"),
        (UpdateStatus("unavailable"), "unavailable"),
        (UpdateStatus("error-net"), "error-net"),
        (UpdateStatus("skipped", version="0.2.1"), "skipped"),
    ],
)
def test_state_mapping(status: UpdateStatus, expected: str) -> None:
    bridge, _ = make_bridge()
    spy = QSignalSpy(bridge.statusChanged)
    bridge.set_status(status)
    assert bridge.state == expected
    assert expected in STATES
    assert len(spy) == (0 if expected == "disabled" else 1)
    bridge.set_status(status)
    assert len(spy) == (0 if expected == "disabled" else 1), "повтор снимка не шлёт сигнал"


def test_available_fields_and_snooze() -> None:
    bridge, _ = make_bridge(open_external=Mock(return_value=True))
    bridge.set_status(
        UpdateStatus(
            "available",
            version="0.2.1",
            notes="## Что нового\n- Первое\n* Второе",
            release_url=RELEASE,
            reminder_until=NOW + 60,
            last_success_at=datetime(2026, 9, 29, 14, 2).timestamp(),
        )
    )
    assert bridge.version == "0.2.1"
    assert bridge.notes == [
        {"text": "Первое", "bullet": True},
        {"text": "Второе", "bullet": True},
    ]
    assert bridge.snoozed is True
    assert bridge.checkedText == "Проверено сегодня в 14:02"
    assert bridge.releasePageAvailable is True


def test_foreign_release_url_is_never_opened() -> None:
    opener = Mock(return_value=True)
    bridge, _ = make_bridge(open_external=opener)
    bridge.set_status(
        UpdateStatus("available", version="0.2.1", release_url="https://evil.example/releases/")
    )
    assert bridge.releasePageAvailable is False
    bridge.openReleasePage()
    opener.assert_not_called()


def test_release_page_opens_only_through_injected_opener() -> None:
    opener = Mock(return_value=False)
    bridge, _ = make_bridge(open_external=opener)
    bridge.set_status(UpdateStatus("available", version="0.2.1", release_url=RELEASE))
    bridge.openReleasePage()
    opener.assert_called_once_with(RELEASE)
    opener.side_effect = OSError("нет браузера")
    bridge.openReleasePage()  # сбой внешней программы не поднимается в QML


def test_without_opener_release_page_is_hidden() -> None:
    bridge, _ = make_bridge()
    bridge.set_status(UpdateStatus("available", version="0.2.1", release_url=RELEASE))
    assert bridge.releasePageAvailable is False


def test_actions_reach_checker() -> None:
    bridge, checker = make_bridge()
    bridge.set_status(UpdateStatus("available", version="0.2.1"))
    bridge.checkNow()
    bridge.skipVersion()
    bridge.clearSkip()
    bridge.remindLater()
    checker.check_now.assert_called_once_with()
    checker.skip_version.assert_called_once_with("0.2.1")
    checker.clear_skip.assert_called_once_with()
    checker.remind_later.assert_called_once_with()


def test_skip_without_version_does_nothing() -> None:
    bridge, checker = make_bridge()
    bridge.skipVersion()
    checker.skip_version.assert_not_called()


@pytest.mark.parametrize("code", ["offline", "admin", "policy"])
def test_refused_manual_check_is_not_sent(code: str) -> None:
    refusals = {"check_app_manual": code, "download": "admin" if code == "admin" else ""}
    bridge, checker = make_bridge(refusals)
    assert bridge.canCheckNow is False
    assert bridge.checkRefusal == code
    bridge.checkNow()
    checker.check_now.assert_not_called()


def test_refresh_rereads_gate_and_refreshes_checker() -> None:
    refusals: dict[str, str] = {}
    bridge, checker = make_bridge(refusals)
    spy = QSignalSpy(bridge.networkChanged)
    assert bridge.canCheckNow is True
    refusals.update({"check_app_manual": "offline", "download": "offline"})
    bridge.refresh()
    assert bridge.canCheckNow is False
    assert bridge.networkRefusal == "offline"
    assert len(spy) == 1
    checker.refresh.assert_called_once_with()
    bridge.refresh()
    assert len(spy) == 1


def test_notes_markup_stays_literal_text() -> None:
    attack = '<img src="http://127.0.0.1:1/x.png"> <b>жирный</b>'
    assert display_notes(attack) == [{"text": attack, "bullet": False}]
    assert display_notes("- " + attack) == [{"text": attack, "bullet": True}]


def test_notes_are_capped_by_total_length() -> None:
    notes = "\n".join(f"- пункт {index}" for index in range(1000))
    shown = display_notes(notes)
    assert sum(len(str(line["text"])) for line in shown[:-1]) <= MAX_NOTES_DISPLAY_CHARS
    assert shown[-1] == {"text": "…", "bullet": False}
    assert all(line["bullet"] is True for line in shown[:-1])


def test_notes_drop_blank_lines_and_panel_heading() -> None:
    assert display_notes("\n\n## Что нового\nА\n\n\n* Б\n\n") == [
        {"text": "А", "bullet": False},
        {"text": "Б", "bullet": True},
    ]


def test_notes_keep_issue_numbers_and_strip_only_markdown_headings() -> None:
    """Ревью 29.09: «#123 …» — не заголовок; заголовок — только «#» и пробел."""
    assert display_notes("#123 исправлено\n### Исправления\n#tag") == [
        {"text": "#123 исправлено", "bullet": False},
        {"text": "Исправления", "bullet": False},
        {"text": "#tag", "bullet": False},
    ]


def test_current_version_is_exposed() -> None:
    assert UpdatesBridge(current_version="0.2.0").currentVersion == "0.2.0"


def test_checked_text_formats() -> None:
    assert checked_text(None, NOW) == ""
    earlier = datetime(2026, 9, 7, 9, 5).timestamp()
    assert checked_text(earlier, NOW) == "Проверено 07.09.2026 в 09:05"


@pytest.fixture
def real_tray(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """Настоящее меню трея без значка и без шины: start() не вызывается."""
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from astra_voice.ui import tray as tray_module
    from helpers.qt_app import get_qapplication

    get_qapplication()
    forbidden = Mock(side_effect=AssertionError("шина и настоящий значок запрещены"))
    monkeypatch.setattr(tray_module, "QSystemTrayIcon", forbidden)
    monkeypatch.setattr(tray_module, "QDBusConnection", forbidden)
    monkeypatch.setattr(tray_module, "_send_bus_command", forbidden)
    provider = Mock()
    provider.tooltip.return_value = "Astra Voice"
    tray = tray_module.Tray(provider, tray_factory=Mock)
    try:
        yield tray
    finally:
        sip.delete(tray._menu)
        sip.delete(tray)


def tray_updates_action(tray: Any) -> Any:
    actions = [action for action in tray._menu.actions() if action.text() == "Проверить обновления"]
    assert len(actions) == 1
    return actions[0]


@pytest.mark.parametrize(
    ("refusal", "enabled"),
    [("", True), ("offline", False), ("admin", False), ("policy", False)],
    ids=["normal", "offline", "admin", "policy"],
)
def test_tray_check_updates_item_follows_gate(real_tray: Any, refusal: str, enabled: bool) -> None:
    """Р9: пункт трея активен всегда, кроме офлайна и запрета администратором."""
    from astra_voice.app import _wire_tray_updates

    bridge, checker = make_bridge({"check_app_manual": refusal})
    shown = Mock()
    _wire_tray_updates(real_tray, bridge, shown)
    action = tray_updates_action(real_tray)
    assert action.isEnabled() is enabled
    action.trigger()
    if enabled:
        shown.assert_called_once_with()
        checker.check_now.assert_called_once_with()
    else:
        checker.check_now.assert_not_called()


def test_tray_item_tracks_offline_toggle(real_tray: Any) -> None:
    from astra_voice.app import _wire_tray_updates

    refusals: dict[str, str] = {}
    bridge, _ = make_bridge(refusals)
    _wire_tray_updates(real_tray, bridge, Mock())
    action = tray_updates_action(real_tray)
    assert action.isEnabled()
    refusals.update({"check_app_manual": "offline", "download": "offline"})
    bridge.refresh()
    assert not action.isEnabled()
    refusals.clear()
    bridge.refresh()
    assert action.isEnabled()


def test_tray_item_stays_disabled_without_checker(real_tray: Any) -> None:
    from astra_voice.app import _wire_tray_updates

    _wire_tray_updates(real_tray, UpdatesBridge(), Mock())
    assert not tray_updates_action(real_tray).isEnabled()
