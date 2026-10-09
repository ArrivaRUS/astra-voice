"""Unprivileged X11 command-key owner; never records or exports arbitrary keys."""

from __future__ import annotations

import select
import socket
import struct
import sys
import time
from dataclasses import asdict, replace
from typing import Any

from astra_voice.platform.command_hotkey import (
    HEALTH_LIMIT_S,
    boot_time,
    receive_packet,
    send_packet,
)
from astra_voice.platform.hotkey import (
    PTT_THRESHOLD_S,
    GrabResult,
    HotkeyEvent,
    X11HotkeyBackend,
)


class Broker:
    def __init__(self, connection: socket.socket) -> None:
        self.connection = connection
        self.backend = X11HotkeyBackend()
        self.combo: str | None = None
        self.keycode: int | None = None
        self.pending: HotkeyEvent | None = None
        self.freeze_deadline = 0.0
        self.heartbeat_deadline = boot_time() + HEALTH_LIMIT_S
        self.raw_opcode: int | None = None
        self.generation = 0
        self.active = False
        self.released = False
        self.lease_token: str | None = None

    def send(self, kind: str, **fields: Any) -> None:
        send_packet(self.connection, {"kind": kind, **fields})

    def _raw_selection(self, enabled: bool) -> bool:
        from Xlib.ext import xinput

        x = self.backend._x
        if not x.open():
            return False
        if enabled:
            if not x.d.has_extension(xinput.extname):
                return False
            x.d.xinput_query_version()
            self.raw_opcode = int(x.d.query_extension(xinput.extname).major_opcode)
        if self.raw_opcode is not None:
            mask = (xinput.RawKeyPressMask | xinput.RawKeyReleaseMask) if enabled else 0
            x.root.xinput_select_events([(xinput.AllDevices, mask)])
            x.d.sync()
        return True

    def cancel(self) -> None:
        self.pending = None
        self.active = self.released = False
        self.backend.cancel_keyboard_grab()
        self.generation += 1

    def command(self, message: dict[str, Any]) -> None:
        operation = message.get("op")
        if operation == "ping" and set(message) == {"op"}:
            self.heartbeat_deadline = boot_time() + HEALTH_LIMIT_S
            self.send("pong")
            return
        sequence = message.get("id")
        if type(sequence) is not int or sequence <= 0:
            raise ValueError("invalid broker sequence")
        fields = {
            "signature": {"combo"},
            "probe": {"combo"},
            "grab": {"combo", "defer"},
            "ungrab": {"combo"},
            "held": {"keycode"},
            "cancel": set(),
            "escape": set(),
            "unescape": set(),
            "lease": {"token"},
        }
        if operation not in fields or set(message) != {"op", "id", *fields[operation]}:
            raise ValueError("invalid broker operation")
        if "combo" in message and (
            not isinstance(message["combo"], str) or not 0 < len(message["combo"]) <= 128
        ):
            raise ValueError("invalid broker combo")
        value: Any = None
        if operation == "signature":
            value = self.backend.combo_signature(message["combo"])
        elif operation == "probe":
            probe = X11HotkeyBackend()
            try:
                value = asdict(probe.grab_combo(message["combo"]))
            finally:
                probe.close()
        elif operation == "grab":
            if type(message["defer"]) is not bool:
                raise ValueError("invalid broker mode")
            combo = message["combo"]
            deferred = message["defer"] and combo.lower() in ("super_l", "super_r", "super", "win")
            self.backend.synchronous_super = deferred
            if deferred and not self._raw_selection(True):
                value = asdict(GrabResult("not-grabbed"))
            else:
                if not deferred:
                    self._raw_selection(False)
                result = self.backend.grab_combo(combo)
                value = asdict(result)
                if result.ok:
                    self.combo, self.keycode = combo, result.keycode
                elif deferred:
                    self._raw_selection(False)
        elif operation == "ungrab":
            self.cancel()
            self.backend.ungrab_combo(message["combo"])
            if self.combo == message["combo"]:
                self.combo = self.keycode = None
                self._raw_selection(False)
            self.drain_x()
        elif operation == "cancel":
            self.cancel()
        elif operation == "held":
            keycode = message["keycode"]
            if type(keycode) is not int or not 8 <= keycode <= 255:
                raise ValueError("invalid broker key")
            value = self.backend.key_is_down(keycode)
        elif operation == "escape":
            value = asdict(self.backend.grab_escape())
        elif operation == "unescape":
            self.backend.ungrab_escape()
        elif operation == "lease":
            from astra_voice.platform.kwin_command_lease import lease_token

            token = message["token"]
            if not isinstance(token, str) or len(token) > 512 or self.combo is not None:
                raise ValueError("invalid menu lease")
            parts = token.split(",")
            if len(parts) != 4 or lease_token(parts[0], parts[1].rsplit("/g", 1)[-1]) != token:
                raise ValueError("invalid menu lease")
            self.lease_token = token
        self.send("reply", id=sequence, value=value)

    def _raw(self, event: Any) -> None:
        from Xlib import X
        from Xlib.ext import xinput

        if event.evtype not in (xinput.RawKeyPress, xinput.RawKeyRelease):
            return
        if not isinstance(event.data, bytes) or len(event.data) < 12:
            raise ValueError("invalid raw event")
        device, _timestamp, keycode, source = struct.unpack_from("=HIIH", event.data)
        if device != source:  # only slave events are delivered before the frozen master
            return
        if self.pending is None:
            if self.active and event.evtype == xinput.RawKeyRelease and keycode == self.keycode:
                self.released = True
            return
        if event.evtype == xinput.RawKeyPress and keycode != self.keycode:
            # Replay the ORIGINAL Super press, not this second key. The subsequent
            # key can then activate the WM's passive root grab naturally.
            self.backend._x.d.allow_events(X.ReplayKeyboard, X.CurrentTime)
            self.backend._x.d.flush()
            self.pending = None
        elif event.evtype == xinput.RawKeyRelease and keycode == self.keycode:
            # A short tap is swallowed. XUngrabKeyboard also thaws queued events.
            self.cancel()

    def drain_x(self) -> None:
        from Xlib import X

        x = self.backend._x
        count = 0
        while x.pending_events():
            count += 1
            if count > 512 or boot_time() >= self.heartbeat_deadline:
                raise ValueError("broker input deadline")
            event = x.next_event()
            if event.type == 35 and event.extension == self.raw_opcode:
                self._raw(event)
            elif event.type == X.MappingNotify and event.request in (
                X.MappingKeyboard,
                X.MappingModifier,
            ):
                mapping = self.backend._refresh_mapping(event)
                if mapping.regrabbed or any(not result.ok for result in mapping.combos.values()):
                    lost_release = self.pending is not None or self.active
                    self.cancel()
                    if lost_release and self.combo is not None:
                        mapping = replace(
                            mapping,
                            combos={**mapping.combos, self.combo: GrabResult("not-grabbed")},
                        )
                if self.combo in mapping.combos:
                    self.keycode = mapping.combos[self.combo].keycode
                self.send("mapping", event=asdict(mapping))
            elif event.type in (X.KeyPress, X.KeyRelease):
                escape = self.backend._escape
                # No unrelated keycode, modifier, text, repr or log crosses IPC.
                if event.detail != self.keycode and (
                    escape is None or event.detail != escape.keycode
                ):
                    continue
                key = HotkeyEvent(
                    "KeyPress" if event.type == X.KeyPress else "KeyRelease",
                    int(event.detail),
                    int(event.time),
                    escape is not None and event.detail == escape.keycode,
                    x.semantic_modifiers(int(event.state)),
                )
                if (
                    self.backend.synchronous_super
                    and key.keycode == self.keycode
                    and key.kind == "KeyPress"
                    and not self.active
                ):
                    if self.pending is None:
                        self.pending = key
                        self.freeze_deadline = boot_time() + PTT_THRESHOLD_S
                else:
                    if key.keycode == self.keycode and key.kind == "KeyRelease" and self.released:
                        self.active = self.released = False
                    self.send("key", event=asdict(key))

    def tick(self) -> None:
        from Xlib import X

        if self.pending is not None and boot_time() >= self.freeze_deadline:
            key, self.pending = self.pending, None
            self.active = True
            self.backend._x.d.allow_events(X.AsyncKeyboard, X.CurrentTime)
            self.backend._x.d.flush()
            self.send("key", event=asdict(replace(key, confirmed_hold=True)))

    def run(self) -> None:
        try:
            while True:
                now = boot_time()
                if now >= self.heartbeat_deadline:
                    return
                deadline = self.heartbeat_deadline
                if self.pending is not None:
                    deadline = min(deadline, self.freeze_deadline)
                readers = [self.connection.fileno()]
                if self.backend.fileno() >= 0:
                    readers.append(self.backend.fileno())
                ready, _, _ = select.select(readers, [], [], min(0.05, max(0.0, deadline - now)))
                # No queued heartbeat may resurrect a lease expired during sleep.
                if boot_time() >= self.heartbeat_deadline:
                    return
                if self.connection.fileno() in ready:
                    for _ in range(64):
                        self.command(receive_packet(self.connection))
                        if not select.select([self.connection], [], [], 0)[0]:
                            break
                    else:
                        return
                self.drain_x()  # queued early chord/release wins over a delayed timer
                self.tick()
        finally:
            self.backend.cancel_keyboard_grab()
            self.backend.close()
            self.connection.close()
            if self.lease_token is not None:
                from astra_voice.platform.kwin_command_lease import KWinCommandLease, KWinLeaseError

                for _ in range(3):
                    try:
                        KWinCommandLease().release(self.lease_token)
                        break
                    except KWinLeaseError:
                        # The journal remains recoverable if an orphan writer
                        # still owns the lock or KWin is unavailable.
                        time.sleep(0.2)


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) != 1 or not arguments[0].isdigit():
        return 2
    try:
        connection = socket.socket(fileno=int(arguments[0]))
        connection.setblocking(False)
        Broker(connection).run()
    except Exception:
        # Input details and arbitrary Xlib event repr must never reach logs.
        return 1
    return 0
