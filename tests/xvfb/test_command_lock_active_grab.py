"""Forced command cancellation releases an active passive grab before key-up.

Each test owns a fresh Xvfb and two X11 connections. It never connects to an
inherited display, runs the application, or opens audio. Real-X evidence is
explicitly skipped when Xvfb is unavailable.
"""

from __future__ import annotations

import os
import select
import shutil
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from astra_voice.platform.hotkey import HotkeyManager, HotkeyMode, X11HotkeyBackend
from astra_voice.platform.x11 import X11Display

pytestmark = pytest.mark.xvfb


@pytest.fixture
def owned_xvfb(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    executable = shutil.which("Xvfb")
    if executable is None:
        pytest.skip("real X11 active-grab regression needs Xvfb; no server was started")
    pytest.importorskip("Xlib")
    env = os.environ.copy()
    for name in (
        "DISPLAY",
        "WAYLAND_DISPLAY",
        "XAUTHORITY",
        "QT_ACCESSIBILITY",
        "AT_SPI_BUS_ADDRESS",
    ):
        env.pop(name, None)
    env.update(
        QT_QPA_PLATFORM="offscreen",
        QT_QUICK_BACKEND="software",
        DBUS_SESSION_BUS_ADDRESS="unix:path=/nonexistent",
        DBUS_SYSTEM_BUS_ADDRESS="unix:path=/nonexistent",
        PULSE_SERVER="unix:/nonexistent",
    )
    read_fd, write_fd = os.pipe()
    server: subprocess.Popen[bytes] | None = None
    try:
        with (tmp_path / "owned-xvfb.log").open("wb") as log:
            try:
                server = subprocess.Popen(
                    [
                        executable,
                        "-displayfd",
                        str(write_fd),
                        "-screen",
                        "0",
                        "640x480x24",
                        "-nolisten",
                        "tcp",
                        "-ac",
                        "-noreset",
                    ],
                    env=env,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    pass_fds=(write_fd,),
                    start_new_session=True,
                )
            finally:
                os.close(write_fd)
            assert select.select([read_fd], [], [], 5)[0], "owned Xvfb readiness timeout"
            number = os.read(read_fd, 64).decode("ascii").strip()
            assert number.isdecimal(), "owned Xvfb did not provide a display number"
            assert server.poll() is None, "owned Xvfb exited during readiness handshake"
            # CI can allocate :0 for this newly created, owned server. Locally :0
            # is always refused before opening an X11 connection.
            assert int(number) > 0 or os.environ.get("CI", "").lower() == "true", (
                "refusing display :0 outside CI; no X11 connection was opened"
            )
            display_name = f":{number}"
            monkeypatch.setenv("DISPLAY", display_name)
            monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
            monkeypatch.delenv("XAUTHORITY", raising=False)
            yield display_name
    finally:
        os.close(read_fd)
        if server is not None:
            server.terminate()
            try:
                server.wait(timeout=3)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=3)


def key_is_held(connection: Any, keycode: int) -> bool:
    keymap = connection.query_keymap()
    return bool(int(keymap[keycode // 8]) & (1 << (keycode % 8)))


@pytest.mark.parametrize("combo", ["Super_L", "Super_R"], ids=["left-win", "right-win"])
@pytest.mark.parametrize("force_cancel", [True, False], ids=["forced-cancel", "legacy-control"])
def test_command_cancel_active_grab_contract_and_legacy_control(
    owned_xvfb: str,
    combo: str,
    force_cancel: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from Xlib import X, display
    from Xlib.ext import xtest

    x11 = X11Display()
    peer: Any = None
    keycode: int | None = None
    backend = X11HotkeyBackend(x11)
    manager = HotkeyManager(backend)
    deferred: list[str] = []
    manager.defer_single_super = True
    manager.on_deferred_press = lambda: deferred.append(combo)
    try:
        assert x11.open(owned_xvfb)
        peer = display.Display(owned_xvfb)
        assert peer.has_extension("XTEST"), "owned Xvfb must provide XTEST"
        result = manager.grab(combo, HotkeyMode.PTT)
        assert result.ok and result.keycode is not None
        keycode = result.keycode
        xtest.fake_input(peer, X.KeyPress, keycode)
        peer.sync()
        deadline = time.monotonic() + 1
        while not deferred and time.monotonic() < deadline:
            manager.process_pending()
            time.sleep(0.005)
        assert deferred == [combo], "the command manager must receive the held Win press"
        assert key_is_held(peer, keycode)
        assert (
            peer.screen().root.grab_keyboard(False, X.GrabModeAsync, X.GrabModeAsync, X.CurrentTime)
            == X.AlreadyGrabbed
        ), "precondition: passive Win grab must be active"

        # The lock/cancel path is required to free the keyboard immediately,
        # including the deferred short-Win phase before recording begins.
        if not force_cancel:
            # Keep the old ungrab-key-only path as a real-X defect control on
            # the same revision: passive ungrab does not cancel its active grab.
            monkeypatch.setattr(backend, "cancel_keyboard_grab", lambda: None, raising=False)
        manager.ungrab()
        assert x11.d is not None
        x11.d.sync()
        assert key_is_held(peer, keycode), "no synthetic key-up may hide the regression"
        status = peer.screen().root.grab_keyboard(
            False, X.GrabModeAsync, X.GrabModeAsync, X.CurrentTime
        )
        if force_cancel:
            assert status == X.GrabSuccess, (
                "forced cancellation must free the keyboard before physical key-up"
            )
        else:
            assert status == X.AlreadyGrabbed, (
                "legacy defect control must retain its active grab until physical key-up"
            )
    finally:
        # Also clean the deliberately retained active grab in the legacy control.
        if x11.d is not None:
            x11.d.ungrab_keyboard(X.CurrentTime)
            x11.d.sync()
        if peer is not None:
            peer.ungrab_keyboard(X.CurrentTime)
            if keycode is not None:
                xtest.fake_input(peer, X.KeyRelease, keycode)
            peer.sync()
            peer.close()
        manager.ungrab()
        backend.close()
