#!/usr/bin/env python3
"""Opt-in Win probe. The default execution never imports Qt or opens X11."""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import re
import signal
import sys
import time
from pathlib import Path


def emit(event: str, **fields: object) -> None:
    print(json.dumps({"event": event, **fields}), flush=True)


def parent_death_guard(expected_parent: int) -> None:
    # Linux PR_SET_PDEATHSIG. No inherited X connection; parent death kills only us.
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0:
        raise RuntimeError("parent_guard_failed")
    if os.getppid() != expected_parent:
        raise RuntimeError("parent_gone")


def simulate(mode: str) -> int:
    """Process-only test child: never import GUI/hardware modules."""
    if mode == "stop":
        print("HB", flush=True)
        os.kill(os.getpid(), signal.SIGSTOP)
    elif mode == "silent":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    elif mode == "exit":
        print("HB", flush=True)
        return 0
    while True:
        if mode == "heartbeat":
            print("HB", flush=True)
        time.sleep(0.05)


def live_child(display: str, duration: float) -> int:
    os.environ["DISPLAY"] = display
    os.environ["QT_QPA_PLATFORM"] = "xcb"
    os.environ["DBUS_SESSION_BUS_ADDRESS"] = "unix:path=/nonexistent"
    os.environ["DBUS_SYSTEM_BUS_ADDRESS"] = "unix:path=/nonexistent"
    os.environ["QT_ACCESSIBILITY"] = "0"
    os.environ["NO_AT_BRIDGE"] = "1"
    os.environ.pop("WAYLAND_DISPLAY", None)
    os.environ.pop("AT_SPI_BUS_ADDRESS", None)
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
    from PyQt5.QtCore import QSocketNotifier, QTimer
    from PyQt5.QtWidgets import QApplication, QLabel, QPushButton, QVBoxLayout, QWidget

    from astra_voice.platform.hotkey import HotkeyEvent, MappingEvent, X11HotkeyBackend

    app = QApplication(["command-win-probe"])
    window = QWidget()
    window.setWindowTitle("Astra Win probe — no microphone")
    label = QLabel("Готовлю пробник. Только Win. Микрофон выключен.")
    stop = QPushButton("Стоп — освободить Win")
    layout = QVBoxLayout(window)
    layout.addWidget(label)
    layout.addWidget(stop)
    window.resize(520, 150)
    backend = X11HotkeyBackend()
    started = time.monotonic()
    keycode: int | None = None
    pressed_at: float | None = None
    recording = False
    suppressed = False
    closed = False
    notifier = None
    counter = 0

    def state(code: str) -> None:
        nonlocal counter
        counter += 1
        label.setText(code + "\nТолько индикатор, без аудио. Стоп освобождает Win.")
        emit(code, count=counter, elapsed_ms=round((time.monotonic() - started) * 1000))

    def close() -> None:
        nonlocal closed, pressed_at, recording
        if closed:
            return
        closed = True
        pressed_at, recording = None, False
        if notifier is not None:
            notifier.setEnabled(False)
        backend.close()
        state("closed")

    def poll() -> None:
        nonlocal pressed_at, recording, suppressed
        for event in backend.poll_events():
            if isinstance(event, MappingEvent):
                state("mapping_changed_stop")
                close()
                app.quit()
                return
            if not isinstance(event, HotkeyEvent):
                continue
            now = time.monotonic()
            if event.keycode == keycode:
                if event.kind == "KeyPress" and pressed_at is None:
                    pressed_at, recording, suppressed = now, False, False
                    state("pending")
                elif event.kind == "KeyRelease" and pressed_at is not None:
                    state(
                        "would_record_end"
                        if recording
                        else "early_chord_end_FAIL"
                        if suppressed
                        else "tap_ignored"
                    )
                    pressed_at, recording, suppressed = None, False, False
            elif event.kind == "KeyPress" and pressed_at is not None:
                if now - pressed_at < 0.3 and not recording:
                    suppressed = True
                    state("early_chord_FAIL_no_replay")
                else:
                    state("held_other_key_discarded")

    def tick() -> None:
        nonlocal recording
        print("HB", flush=True)
        if closed:
            return
        now = time.monotonic()
        if now - started >= duration:
            close()
            app.quit()
            return
        if pressed_at is not None:
            if now - pressed_at >= 5:
                state("hold_limit_stop")
                close()
                app.quit()
            elif now - pressed_at >= 0.3 and not recording and not suppressed:
                recording = True
                state("would_record")

    def arm() -> None:
        nonlocal keycode, notifier
        if not window.isActiveWindow():
            state("focus_not_ours_stop")
            close()
            app.quit()
            return
        result = backend.grab_combo("Super_L")
        if not result.ok:
            state("grab_failed_stop")
            close()
            app.quit()
            return
        keycode = result.keycode
        notifier = QSocketNotifier(backend.fileno(), QSocketNotifier.Read, window)
        notifier.activated.connect(poll)
        state("armed")

    stop.clicked.connect(app.quit)
    app.aboutToQuit.connect(close)
    signal.signal(signal.SIGTERM, lambda *_: app.quit())
    signal.signal(signal.SIGINT, lambda *_: app.quit())
    timer = QTimer(window)
    timer.timeout.connect(tick)
    timer.start(50)
    window.show()
    window.activateWindow()
    # No grab until the operator's own visible test window has focus.
    QTimer.singleShot(250, arm)
    print("HB", flush=True)
    try:
        return int(app.exec_())
    finally:
        close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live-display")
    parser.add_argument("--confirm-live", action="store_true")
    parser.add_argument("--duration", type=float, default=15.0)
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--parent-pid", type=int, help=argparse.SUPPRESS)
    parser.add_argument(
        "--simulate", choices=("heartbeat", "silent", "stop", "exit"), help=argparse.SUPPRESS
    )
    args = parser.parse_args()
    if not 0 < args.duration <= 60:
        parser.error("duration must be >0 and <=60 seconds")
    if args.child:
        if args.parent_pid is None:
            parser.error("child requires parent identity")
        parent_death_guard(args.parent_pid)
        if args.simulate:
            return simulate(args.simulate)
        if not args.confirm_live or not args.live_display:
            parser.error("live child requires explicit confirmation/display")
        return live_child(args.live_display, args.duration)
    if not args.live_display and not args.confirm_live:
        emit("dry_run_no_X", limit_s=args.duration, early_chord="FAIL_no_replay")
        return 0
    if (
        not args.confirm_live
        or not args.live_display
        or not re.fullmatch(r":\d+(?:\.\d+)?", args.live_display)
    ):
        parser.error("use --confirm-live and an explicitly observed local X11 --live-display :N")
    from supervisor import supervise

    return supervise(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "--child",
            "--parent-pid",
            str(os.getpid()),
            "--confirm-live",
            "--live-display",
            args.live_display,
            "--duration",
            str(args.duration),
        ],
        duration=args.duration,
    )


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        emit("probe_failed")
        raise SystemExit(2) from None
