"""Вид сеанса: ветки по переменным окружения и запасной путь через X11."""

from __future__ import annotations

import pytest

from astra_voice.platform import session
from astra_voice.platform.session import SessionKind

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({"XDG_CURRENT_DESKTOP": "KDE"}, SessionKind.KDE),
        ({"XDG_CURRENT_DESKTOP": "KDE:plasma"}, SessionKind.KDE),
        ({"XDG_CURRENT_DESKTOP": "Fly"}, SessionKind.FLY),
        ({"XDG_CURRENT_DESKTOP": "FLY"}, SessionKind.FLY),
        ({"XDG_CURRENT_DESKTOP": "X-Fly:KDE"}, SessionKind.FLY),
        ({"DESKTOP_SESSION": "fly"}, SessionKind.FLY),
        ({"XDG_CURRENT_DESKTOP": "GNOME"}, SessionKind.OTHER),
        ({}, SessionKind.OTHER),
    ],
)
def test_env_branches(env: dict[str, str], expected: SessionKind) -> None:
    assert session.detect(env) == expected


def test_x11_used_only_without_env_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def fake(display: str) -> SessionKind:
        calls.append(display)
        return SessionKind.KDE

    monkeypatch.setattr(session, "_from_x11", fake)
    assert session.detect({"XDG_CURRENT_DESKTOP": "Fly", "DISPLAY": ":0"}) == SessionKind.FLY
    assert calls == []
    assert session.detect({"DISPLAY": ":9"}) == SessionKind.KDE
    assert calls == [":9"]


def test_x11_unavailable_gives_other(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(session, "_from_x11", lambda display: None)
    assert session.detect({"DISPLAY": ":0"}) == SessionKind.OTHER


def test_from_x11_survives_missing_display() -> None:
    assert session._from_x11(":42") is None


def test_kind_is_plain_string() -> None:
    assert SessionKind.FLY.value == "FLY"
    assert str(SessionKind.KDE.value) == "KDE"
