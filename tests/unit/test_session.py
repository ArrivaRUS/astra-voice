"""Вид сеанса: ветки по переменным окружения и запасной путь через X11."""

from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import Mock

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


def test_from_x11_survives_missing_display(monkeypatch: pytest.MonkeyPatch) -> None:
    display = SimpleNamespace(Display=Mock(side_effect=RuntimeError("no display")))
    monkeypatch.setitem(sys.modules, "Xlib", SimpleNamespace(X=SimpleNamespace(), display=display))
    assert session._from_x11(":42") is None


def test_kind_is_plain_string() -> None:
    assert SessionKind.FLY.value == "FLY"
    assert str(SessionKind.KDE.value) == "KDE"


@pytest.fixture
def x11(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    atoms = {
        "FLY_SESSION": 100,
        "_NET_SUPPORTING_WM_CHECK": 101,
        "_NET_WM_NAME": 102,
        "UTF8_STRING": 103,
        "WM_NAME": 39,
        "_FLY_WM_PID": 104,
    }
    root_props = {101: SimpleNamespace(property_type=33, format=32, value=[500])}
    wm_props = {
        101: SimpleNamespace(property_type=33, format=32, value=[500]),
        102: SimpleNamespace(property_type=103, format=8, value=b"fly-wm"),
    }
    root = Mock()
    root.get_full_property.side_effect = lambda atom, kind: root_props.get(atom)
    window = Mock()
    window.get_full_property.side_effect = lambda atom, kind: wm_props.get(atom)
    conn = Mock()
    conn.intern_atom.side_effect = lambda name, only_if_exists=True: atoms.get(name, 0)
    conn.screen.return_value = SimpleNamespace(root=root)
    conn.create_resource_object.return_value = window
    display = SimpleNamespace(Display=Mock(return_value=conn))
    monkeypatch.setitem(
        sys.modules, "Xlib", SimpleNamespace(X=SimpleNamespace(AnyPropertyType=0), display=display)
    )
    return SimpleNamespace(conn=conn, root=root_props, wm=wm_props, window=window)


def test_x11_root_fly_session(x11: SimpleNamespace) -> None:
    x11.root.clear()
    x11.root[100] = SimpleNamespace(property_type=31, format=8, value=b"fly")
    assert session._from_x11(":9") == SessionKind.FLY
    x11.conn.close.assert_called_once()


@pytest.mark.parametrize(
    "prop",
    [
        SimpleNamespace(property_type=31, format=8, value=b"Fly"),
        SimpleNamespace(property_type=31, format=8, value=b"fly\0"),
        SimpleNamespace(property_type=103, format=8, value=b"fly"),
        SimpleNamespace(property_type=31, format=32, value=b"fly"),
    ],
)
def test_x11_invalid_root_fly_session(x11: SimpleNamespace, prop: object) -> None:
    x11.root.clear()
    x11.root[100] = prop
    assert session._from_x11(":9") is None
    x11.conn.close.assert_called_once()


def test_x11_legacy_pid_alone_is_not_session(x11: SimpleNamespace) -> None:
    x11.root.clear()
    x11.root[104] = SimpleNamespace(property_type=6, format=32, value=[12345])
    assert session._from_x11(":9") is None
    x11.conn.close.assert_called_once()


@pytest.mark.parametrize(
    "name,expected", [(b"fly-wm", SessionKind.FLY), (b"KWin", SessionKind.KDE)]
)
def test_x11_ewmh_name(x11: SimpleNamespace, name: bytes, expected: SessionKind) -> None:
    x11.wm[102].value = name
    assert session._from_x11(":9") == expected
    x11.conn.close.assert_called_once()


@pytest.mark.parametrize("source", ["wm", "root"])
def test_x11_ewmh_wm_name(x11: SimpleNamespace, source: str) -> None:
    del x11.wm[102]
    getattr(x11, source)[39] = SimpleNamespace(property_type=31, format=8, value=b"fly-wm")
    assert session._from_x11(":9") == SessionKind.FLY
    x11.conn.close.assert_called_once()


@pytest.mark.parametrize(
    "target,atom,prop",
    [
        ("root", 101, SimpleNamespace(property_type=6, format=32, value=[500])),
        ("root", 101, SimpleNamespace(property_type=33, format=32, value=[500, 501])),
        ("root", 101, SimpleNamespace(property_type=33, format=32, value=[True])),
        ("wm", 101, None),
        ("wm", 101, SimpleNamespace(property_type=33, format=32, value=[501])),
        ("wm", 102, SimpleNamespace(property_type=103, format=8, value=b"butterfly")),
        ("wm", 102, SimpleNamespace(property_type=103, format=8, value=b"fly\0-wm")),
        ("wm", 102, SimpleNamespace(property_type=103, format=32, value=b"fly-wm")),
        ("wm", 102, SimpleNamespace(property_type=31, format=8, value=b"fly-wm")),
        ("wm", 102, SimpleNamespace(property_type=103, format=8, value=b"\xff")),
    ],
)
def test_x11_rejects_wrong_ewmh(x11: SimpleNamespace, target: str, atom: int, prop: object) -> None:
    getattr(x11, target)[atom] = prop
    x11.root[39] = SimpleNamespace(property_type=31, format=8, value=b"fly-wm")
    assert session._from_x11(":9") is None
    x11.conn.close.assert_called_once()


def test_x11_errors_close_and_redact(
    x11: SimpleNamespace, caplog: pytest.LogCaptureFixture
) -> None:
    x11.window.get_full_property.side_effect = RuntimeError("PRIVATE X11 CONTENT")
    with caplog.at_level("DEBUG"):
        assert session._from_x11(":9") is None
    assert "PRIVATE" not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)
    x11.conn.close.assert_called_once()


def test_x11_missing_library(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "Xlib", None)
    assert session._from_x11(":9") is None


@pytest.mark.parametrize("name", [b"fly-wm\0", b"  fly-wm  ", b"  fly-wm  \0\0"])
def test_x11_clean_ewmh_name(x11: SimpleNamespace, name: bytes) -> None:
    x11.wm[102].value = name
    assert session._from_x11(":9") == SessionKind.FLY


@pytest.mark.parametrize("marker", [b"fly\0", b" fly ", b" fly \0"])
def test_x11_session_marker_requires_exact_bytes(x11: SimpleNamespace, marker: bytes) -> None:
    x11.root.clear()
    x11.root[100] = SimpleNamespace(property_type=31, format=8, value=marker)
    assert session._from_x11(":9") is None


@pytest.mark.parametrize(
    "modern",
    [
        SimpleNamespace(property_type=31, format=8, value=b"fly-wm"),
        SimpleNamespace(property_type=103, format=32, value=b"fly-wm"),
        SimpleNamespace(property_type=103, format=8, value=[102, 108, 121]),
        SimpleNamespace(property_type=103, format=8, value=b"\xff"),
    ],
)
def test_x11_invalid_modern_uses_check_legacy(x11: SimpleNamespace, modern: object) -> None:
    x11.wm[102] = modern
    x11.wm[39] = SimpleNamespace(property_type=31, format=8, value=b"fly-wm\0")
    assert session._from_x11(":9") == SessionKind.FLY


def test_x11_both_names_invalid_cannot_use_root(x11: SimpleNamespace) -> None:
    x11.wm[102].property_type = 31
    x11.wm[39] = SimpleNamespace(property_type=103, format=8, value=b"fly-wm")
    x11.root[39] = SimpleNamespace(property_type=31, format=8, value=b"fly-wm")
    assert session._from_x11(":9") is None
