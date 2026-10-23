"""Private bounded launch guard, separate from tray insertion provenance."""

from __future__ import annotations

import fcntl
import json
import os
import pwd
import stat
from pathlib import Path

from .fly_themerc import ThemercError, open_directory, read_fd


class FlyTrayLoopGuard:
    """Worker-only operations; locked regular 0600 file below the passwd home."""

    def __init__(self, state_path: Path) -> None:
        self.path = state_path.with_name("fly-tray-loop.json")

    def reset(self) -> None:
        self._update(reset=True, disappeared=False)

    def allow(self, disappeared: bool) -> bool:
        return self._update(reset=False, disappeared=disappeared)

    def restart_started(self) -> None:
        self._update(reset=False, disappeared=False, restart=True)

    def restart_confirmed(self) -> None:
        self._update(reset=False, disappeared=False, restart=False)

    def _update(self, *, reset: bool, disappeared: bool, restart: bool | None = None) -> bool:
        home = Path(pwd.getpwuid(os.getuid()).pw_dir)
        if (
            os.getuid() == 0
            or os.getuid() != os.geteuid()
            or home != Path.home()
            or not self.path.is_absolute()
            or ".." in self.path.parts
        ):
            raise ThemercError("administrator")
        try:
            return self._open_update(home, reset=reset, disappeared=disappeared, restart=restart)
        except FileNotFoundError:
            if restart is not True and (reset or not disappeared):
                return True
            raise

    def _open_update(
        self, home: Path, *, reset: bool, disappeared: bool, restart: bool | None
    ) -> bool:
        create = (disappeared and not reset) or restart is True
        with open_directory(self.path.parent, home=home, create=create) as directory:
            flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
            created = False
            if create:
                try:
                    fd = os.open(
                        self.path.name,
                        flags | os.O_CREAT | os.O_EXCL,
                        0o600,
                        dir_fd=directory,
                    )
                    created = True
                except FileExistsError:
                    fd = os.open(self.path.name, flags, dir_fd=directory)
            else:
                fd = os.open(self.path.name, flags, dir_fd=directory)
            try:
                info = os.fstat(fd)
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_uid != os.getuid()
                    or info.st_nlink != 1
                    or stat.S_IMODE(info.st_mode) != 0o600
                ):
                    raise ThemercError("state")
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                data, _ = read_fd(fd, limit=256)
                count, blocked, in_progress = 0, False, False
                if not data and not created:
                    raise ThemercError("state")
                if data:
                    try:
                        value = json.loads(data)
                        if (
                            not isinstance(value, dict)
                            or set(value)
                            not in (
                                {"count", "blocked"},
                                {"count", "blocked", "restart_in_progress"},
                            )
                            or type(value["count"]) is not int
                            or not 0 <= value["count"] <= 4
                            or type(value["blocked"]) is not bool
                            or type(value.get("restart_in_progress", False)) is not bool
                            or value["blocked"] != (value["count"] > 3)
                        ):
                            raise ValueError
                        count, blocked = value["count"], value["blocked"]
                        in_progress = value.get("restart_in_progress", False)
                    except (ValueError, TypeError, RecursionError):
                        raise ThemercError("state") from None
                if reset:
                    count, blocked, in_progress = 0, False, False
                elif restart is not None:
                    in_progress = restart
                elif disappeared and not blocked and not in_progress:
                    count = min(4, count + 1)
                    blocked = count > 3
                elif not disappeared and not blocked and not in_progress:
                    count = 0
                encoded = json.dumps(
                    {"count": count, "blocked": blocked, "restart_in_progress": in_progress}
                ).encode("ascii")
                # A torn/crashed write fails closed on the next read, never enables retries.
                if encoded != data:
                    os.lseek(fd, 0, os.SEEK_SET)
                    view = memoryview(encoded)
                    while view:
                        written = os.write(fd, view)
                        if written <= 0:
                            raise ThemercError("io")
                        view = view[written:]
                    os.ftruncate(fd, len(encoded))
                    os.fsync(fd)
                    os.fsync(directory)
                return not blocked and not in_progress
            finally:
                os.close(fd)
