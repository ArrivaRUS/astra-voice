"""Resolve an explicit local Pulse address before any automatic connection."""

from __future__ import annotations

import os
import re
import socket
import stat
import threading
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

AddressSource = Literal["pulse_server", "pulse_runtime_path", "xdg_runtime_dir", "run_user"]
SocketFds = dict[int, int] | None
# Innermost lock: only native context creation and the observer's fd snapshots.
CONNECT_LOCK = threading.Lock()
SSH_VARIABLES = ("SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY")
LOCAL_DISPLAY = re.compile(r"(?:unix)?:[0-9]+(?:\.[0-9]+)?")
MAX_FDS = 8192


@dataclass(frozen=True)
class LocalAudioServer:
    address: bytes = field(repr=False)
    source: AddressSource


def local_server_address(server: bytes | None) -> bytes | None:
    """Only a single explicit Unix pathname, suitable for libpulse's address list."""
    if not isinstance(server, bytes):
        return None
    path = server.removeprefix(b"unix:")
    if not path.startswith(b"/") or len(path) >= 108 or any(c <= 32 or c == 127 for c in path):
        return None
    if any(unicodedata.category(char) in {"Cc", "Cf"} for char in os.fsdecode(path)):
        return None
    return b"unix:" + path


def address_candidates(
    env: Mapping[str, str], uid: int, run_user: Path
) -> dict[AddressSource, str]:
    candidates: dict[AddressSource, str] = {}
    if server := env.get("PULSE_SERVER"):
        candidates["pulse_server"] = server
    if os.path.isabs(runtime := env.get("PULSE_RUNTIME_PATH", "")):
        candidates["pulse_runtime_path"] = os.path.join(runtime, "native")
    if os.path.isabs(xdg := env.get("XDG_RUNTIME_DIR", "")):
        candidates["xdg_runtime_dir"] = os.path.join(xdg, "pulse", "native")
    candidates["run_user"] = str(run_user / str(uid) / "pulse" / "native")
    return candidates


def socket_address(candidate: str) -> bytes | None:
    try:
        address = local_server_address(os.fsencode(candidate))
        if address is not None and stat.S_ISSOCK(os.lstat(address.removeprefix(b"unix:")).st_mode):
            return address
    except (OSError, ValueError):
        pass
    return None


def resolve_local_server(
    env: Mapping[str, str], uid: int, run_user: Path = Path("/run/user")
) -> LocalAudioServer | None:
    if "DISPLAY" in env and (
        not LOCAL_DISPLAY.fullmatch(env["DISPLAY"]) or any(name in env for name in SSH_VARIABLES)
    ):
        return None
    source, candidate = next(iter(address_candidates(env, uid, run_user).items()))
    address = socket_address(candidate)
    return LocalAudioServer(address, source) if address is not None else None


def own_socket_fds() -> SocketFds:
    """Snapshot only our socket descriptors. A partial scan provides no evidence."""
    fds: dict[int, int] = {}
    try:
        with os.scandir("/proc/self/fd") as entries:
            for count, entry in enumerate(entries):
                if count >= MAX_FDS:
                    return None
                link = os.readlink(entry.path)
                if link.startswith("socket:[") and link.endswith("]"):
                    fds[int(entry.name)] = int(link[8:-1])
    except (OSError, ValueError):
        return None
    return fds


def socket_family(fd: int, inode: int) -> int | None:
    """Use a pinned duplicate, checking the descriptor has not been reused."""
    try:
        duplicate = os.dup(fd)
        try:
            if os.fstat(duplicate).st_ino != inode:
                return None
            with socket.socket(fileno=duplicate) as control:
                duplicate = -1  # The socket owns it now.
                return control.family
        finally:
            if duplicate >= 0:
                os.close(duplicate)
    except OSError:
        return None
