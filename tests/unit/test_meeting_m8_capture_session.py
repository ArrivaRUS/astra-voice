"""Capture-role session detection with a simulated X11 transport."""

from __future__ import annotations

import os
import socket
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest

from astra_voice.worker.meeting_capture import MeetingCaptureState
from astra_voice.worker.meeting_session import MeetingSession

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("filesystem_euid")]


@pytest.mark.parametrize(
    ("display", "available", "desktop", "ssh_variable", "expected_name", "fly"),
    [
        pytest.param("localhost:10.0", False, "", None, None, False, id="ssh-forwarded"),
        pytest.param("host:0", False, "", None, None, False, id="remote"),
        pytest.param(":0", False, "", None, "unix/:0", False, id="unix-missing"),
        pytest.param(":0", True, "", None, "unix/:0", True, id="unix-available"),
        pytest.param("unix:0", True, "", None, "unix/:0", True, id="unix-without-screen"),
        pytest.param("unix:0.1", True, "", None, "unix/:0.1", True, id="unix-screen"),
        pytest.param(":0.2", True, "", None, "unix/:0.2", True, id="screen"),
        pytest.param(":17.1", True, "", None, "unix/:17.1", True, id="nonzero-display"),
        pytest.param(":0", True, "", "SSH_CONNECTION", None, False, id="ssh-connection"),
        pytest.param(":0", True, "", "SSH_CLIENT", None, False, id="ssh-client"),
        pytest.param(":0", True, "", "SSH_TTY", None, False, id="ssh-tty"),
        pytest.param(" :0", True, "", None, None, False, id="leading-space"),
        pytest.param(":0 ", True, "", None, None, False, id="trailing-space"),
        pytest.param("localhost:10.0", False, "Fly", None, None, True, id="env-first"),
    ],
)
def test_capture_constructor_opens_only_an_explicit_unix_x_socket(
    monkeypatch: pytest.MonkeyPatch,
    display: str,
    available: bool,
    desktop: str,
    ssh_variable: str | None,
    expected_name: str | None,
    fly: bool,
) -> None:
    xdisplay = pytest.importorskip("Xlib.display")
    connect = pytest.importorskip("Xlib.support.connect")
    unix_connect = pytest.importorskip("Xlib.support.unix_connect")
    families: list[int] = []
    addresses: list[tuple[int, Any]] = []
    sockets: list[Mock] = []
    display_names: list[str | None] = []

    def make_socket(family: int, *_args: Any, **_kwargs: Any) -> Mock:
        families.append(family)
        sock = Mock()
        sockets.append(sock)

        def connect_socket(address: Any) -> None:
            addresses.append((family, address))
            if family != socket.AF_UNIX or not available:
                raise ConnectionRefusedError()

        sock.connect.side_effect = connect_socket
        return sock

    def open_display(name: str | None = None) -> Any:
        display_names.append(name)
        display_name, protocol, host, number, _screen = connect.get_display(name)
        sock = connect.get_socket(display_name, protocol, host, number)
        root = Mock()
        root.get_full_property.return_value = SimpleNamespace(
            property_type=31, format=8, value=b"fly"
        )
        conn = Mock()
        conn.screen.return_value.root = root
        conn.close.side_effect = sock.close
        return conn

    env = {"DISPLAY": display, "XDG_CURRENT_DESKTOP": desktop}
    if ssh_variable is not None:
        env[ssh_variable] = ""  # Presence is enough to decline X11 probing.
    with monkeypatch.context() as patch:
        patch.setattr(os, "environ", env)
        patch.setattr(socket, "socket", make_socket)
        patch.setattr(xdisplay, "Display", open_display)
        patch.setattr(
            unix_connect,
            "os",
            SimpleNamespace(environ=env, path=SimpleNamespace(exists=lambda _: available)),
        )
        patch.setattr(MeetingSession, "start", lambda self: None)
        state = MeetingCaptureState(observer_address=lambda: None)
    try:
        if expected_name is None:
            assert display_names == [] and families == [] and addresses == []
        else:
            assert display_names == [expected_name]
            assert families == [socket.AF_UNIX]
            number = expected_name.removeprefix("unix/:").split(".", 1)[0]
            expected_address = f"/tmp/.X11-unix/X{number}"
            if not available:
                expected_address = "\0" + expected_address
            assert addresses == [(socket.AF_UNIX, expected_address)]
            if available:
                sockets[0].close.assert_called_once_with()
        assert state.session.snapshot().lock_signal_unavailable is fly
    finally:
        state.cleanup()
