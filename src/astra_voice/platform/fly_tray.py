"""US-8.6 step A: explicit, two-phase edits of the Fly tray priority list.

No Qt, process execution, session probing, or startup hooks. Step B must perform
session/binary checks and reserve application idle time BEFORE confirming, on a
worker thread, and hold that reservation through the restart watchdog.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import logging
import os
import pwd
import re
import secrets
import stat
import threading
import time
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from .fly_themerc import (
    READ_FLAGS,
    ThemercError,
    fingerprint,
    open_directory,
    read_fd,
)

TOKEN = b"astra-voice"
assert re.fullmatch(rb"[A-Za-z0-9._-]+", TOKEN)
OP_TTL = 60.0
MAX_LINE = 8192
_KEY = re.compile(rb"^[ \t]*TrayPriorityList[ \t]*=")
_BACKUP = re.compile(r"current\.themerc\.\d{8}T\d{6}Z\.[0-9a-f]{16}\Z")
_TEMP = re.compile(r"\.current\.themerc\.astra-voice\.[0-9a-f]{32}\.tmp\Z")
_LOG = logging.getLogger(__name__)
_MESSAGES = {
    "ok": "Настройка сохранена",
    "already": "Значок уже добавлен в список панели",
    "absent": "Значок можно добавить в список панели",
    "foreign": "Значок на панели (настроено вне программы)",
    "no-file": "Файл настроек Fly не найден",
    "no-key": "Настройка лотка не найдена",
    "ambiguous": "Настройка лотка неоднозначна — проверьте её вручную",
    "grammar": "Формат настройки лотка не поддерживается",
    "section": "Раздел настройки лотка не подтверждён",
    "administrator": "Задано администратором",
    "symlink": "Настройка Fly является ссылкой",
    "not-regular": "Настройка Fly не является обычным файлом",
    "too-large": "Файл настроек Fly слишком велик",
    "changed": "Файл Fly изменился, попробуйте ещё раз",
    "hidden": "Значок скрыт настройками Fly",
    "state": "Не удалось проверить происхождение настройки",
    "consent": "Подготовьте операцию заново",
    "busy": "Другая операция с панелью ещё выполняется",
    "io": "Не удалось сохранить настройку Fly",
}

Kind = Literal["absent", "present_ours", "present_foreign", "unavailable"]
Action = Literal["show", "revert"]


class FlyTrayError(ThemercError):
    @property
    def message(self) -> str:
        return _MESSAGES[self.code]


@dataclass(frozen=True)
class TrayPriorityStatus:
    kind: Kind
    code: str
    message: str
    applied: bool = False


@dataclass(frozen=True)
class Result:
    ok: bool
    changed: bool
    code: str
    message: str


@dataclass(frozen=True)
class _Snapshot:
    data: bytes
    info: os.stat_result
    attrs: dict[str, bytes]

    def same(self, other: _Snapshot) -> bool:
        return (
            fingerprint(self.info) == fingerprint(other.info)
            and _digest(self.data) == _digest(other.data)
            and self.attrs == other.attrs
        )


@dataclass(frozen=True)
class _Staged:
    name: str
    snapshot: _Snapshot


@dataclass(frozen=True)
class _Line:
    start: int
    end: int
    prefix: bytes
    value: bytes
    suffix: bytes

    def render(self, value: bytes) -> bytes:
        line = self.prefix + value + self.suffix
        if len(line) > MAX_LINE:
            raise FlyTrayError("too-large")
        return line

    def count(self) -> int:
        return sum(item.lower() == TOKEN for item in self.value.split(b":"))


@dataclass(frozen=True)
class _Prepared:
    action: Action
    expires: float
    snapshot: _Snapshot
    state: dict[str, str | bool] | None
    apprc: bytes


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _attrs(fd: int) -> dict[str, bytes]:
    return {name: os.getxattr(fd, name) for name in os.listxattr(fd)}


def _error(error: OSError | ThemercError) -> FlyTrayError:
    if isinstance(error, ThemercError):
        return FlyTrayError(error.code, error.error_number)
    code = {
        errno.ENOENT: "no-file",
        errno.ELOOP: "symlink",
        errno.ENOTDIR: "symlink",
        errno.EACCES: "administrator",
        errno.EPERM: "administrator",
        errno.EROFS: "administrator",
    }.get(error.errno or 0, "io")
    return FlyTrayError(code, error.errno or 0)


def _parse(data: bytes) -> _Line:
    found: list[_Line] = []
    offset = 0
    for index, raw in enumerate(data.split(b"\n")):
        end = offset + len(raw)
        ending = b"\n" if end < len(data) else b""
        body = raw
        if body.endswith(b"\r"):
            body, ending = body[:-1], b"\r" + ending
        bom = b"\xef\xbb\xbf" if index == 0 and body.startswith(b"\xef\xbb\xbf") else b""
        body = body[len(bom) :]
        match = _KEY.match(body)
        if match:
            value = body[match.end() :]
            # Keep spacing around '=' and after the value verbatim.
            leading = value[: len(value) - len(value.lstrip(b" \t"))]
            value = value[len(leading) :]
            trailing = value[len(value.rstrip(b" \t")) :]
            value = value[: len(value) - len(trailing)] if trailing else value
            # end already includes CR; LF is the only byte not included in raw.
            found.append(
                _Line(
                    offset,
                    end + int(end < len(data)),
                    bom + body[: match.end()] + leading,
                    value,
                    trailing + ending,
                )
            )
        offset = end + 1
    if not found:
        raise FlyTrayError("no-key")
    if len(found) != 1:
        raise FlyTrayError("ambiguous")
    line = found[0]
    # Fly 1.8.4's [Variables] was confirmed in a live file on 27.10.
    # Only the last header before the key determines its section; keep flat files.
    section = None
    for part in data[: line.start].split(b"\n"):
        header = part.removesuffix(b"\r").lstrip(b"\xef\xbb\xbf \t")
        if header.startswith(b"["):
            section = header
    if section not in (None, b"[Variables]"):
        raise FlyTrayError("section")
    value = line.value
    if (
        any(byte < 32 or byte == 127 for byte in value)
        or any(char in value for char in (b'"', b"'", b"#", b";"))
        or value.endswith(b"\\")
    ):
        raise FlyTrayError("grammar")
    elements = value.split(b":")
    if value and (
        any(
            not item or item.strip(b" \t") != item or b" " in item
            for item in (elements[:-1] if value.endswith(b":") else elements)
        )
    ):
        raise FlyTrayError("grammar")
    line.render(value)
    if line.count() > 1:
        raise FlyTrayError("ambiguous")
    return line


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError(errno.EIO, "write")
        view = view[written:]


def _check_temporary(directory: int, name: str, expected: _Snapshot) -> None:
    fd = os.open(name, READ_FLAGS, dir_fd=directory)
    try:
        data, info = read_fd(fd)
        if not expected.same(_Snapshot(data, info, _attrs(fd))):
            raise FlyTrayError("changed")
    finally:
        os.close(fd)


def _unlink_owned_temp(directory: int, name: str, created: os.stat_result) -> None:
    try:
        info = os.stat(name, dir_fd=directory, follow_symlinks=False)
        if (info.st_dev, info.st_ino) == (created.st_dev, created.st_ino):
            os.unlink(name, dir_fd=directory)
    except FileNotFoundError:
        pass


class FlyTrayPriority:
    """An explicit worker-thread API. Construct/status/prepare never write files.

    Paths are injectable for tests but must remain below the passwd home. The
    token argument is compatibility-only: the only permitted token is the constant.
    Unknown sections, lost provenance and inaccessible metadata fail closed.
    """

    def __init__(
        self,
        themerc: Path | None = None,
        backups_dir: Path | None = None,
        state_path: Path | None = None,
        token: str = "astra-voice",
    ) -> None:
        if token != TOKEN.decode("ascii"):
            raise ValueError("Unsupported tray token")
        home = Path.home()
        self.themerc = themerc if themerc is not None else home / ".fly/theme/current.themerc"
        base = home / ".local/share/astra-voice"
        self.backups_dir = backups_dir if backups_dir is not None else base / "backups/fly-theme"
        self.state_path = state_path if state_path is not None else base / "fly-tray.json"
        self._pending: dict[str, _Prepared] = {}
        self._mutex = threading.Lock()

    @staticmethod
    def restart_command() -> list[str]:
        return ["/usr/bin/fly-wmfunc", "FLYWM_RESTART"]

    def apply(self) -> Result:
        """Runtime facade; caller in step B owns Fly/idle/restart guards."""
        return self._perform("show")

    def revert(self) -> Result:
        """Remove our insertion, preserving later foreign changes."""
        return self._perform("revert")

    def _perform(self, action: Action) -> Result:
        try:
            return self.confirm(self.prepare(action))
        except FlyTrayError as error:
            return Result(False, False, error.code, error.message)

    def _home(self) -> Path:
        home = Path(pwd.getpwuid(os.getuid()).pw_dir)
        if (
            os.getuid() == 0
            or os.geteuid() != os.getuid()
            or not home.is_absolute()
            or home != Path.home()
            or ".." in home.parts
        ):
            raise FlyTrayError("administrator")
        for path in (self.themerc, self.backups_dir, self.state_path):
            if not path.is_absolute() or ".." in path.parts or not path.is_relative_to(home):
                raise FlyTrayError("administrator")
        if self.themerc.name.endswith(".fly-kiosk"):
            raise FlyTrayError("administrator")
        return home

    def _snapshot(self, directory: int) -> _Snapshot:
        fd = os.open(self.themerc.name, READ_FLAGS, dir_fd=directory)
        try:
            data, info = read_fd(fd)
            if info.st_uid != os.getuid() or info.st_nlink != 1 or not data:
                raise FlyTrayError("administrator")
            if not info.st_mode & stat.S_IWUSR:
                raise FlyTrayError("administrator")
            attrs = _attrs(fd)
            if fingerprint(os.fstat(fd)) != fingerprint(info):
                raise FlyTrayError("changed")
            probe = os.open(
                self.themerc.name,
                os.O_WRONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                dir_fd=directory,
            )
            try:
                if fingerprint(os.fstat(probe)) != fingerprint(info):
                    raise FlyTrayError("changed")
            finally:
                os.close(probe)
            return _Snapshot(data, info, attrs)
        finally:
            os.close(fd)

    def _state(self, home: Path) -> dict[str, str | bool] | None:
        try:
            with open_directory(self.state_path.parent, home=home) as directory:
                fd = os.open(self.state_path.name, READ_FLAGS, dir_fd=directory)
                try:
                    data, info = read_fd(fd, limit=16384)
                    if (
                        info.st_uid != os.getuid()
                        or info.st_nlink != 1
                        or stat.S_IMODE(info.st_mode) != 0o600
                    ):
                        raise FlyTrayError("state")
                finally:
                    os.close(fd)
        except FileNotFoundError:
            return None
        try:
            value = json.loads(data)
            if (
                not isinstance(value, dict)
                or value.get("added") is not True
                or value.get("inserted") not in {":astra-voice", "astra-voice:"}
            ):
                raise ValueError
            return {"added": True, "inserted": value["inserted"]}
        except (ValueError, TypeError, RecursionError):
            raise FlyTrayError("state") from None

    def _apprc(self, home: Path) -> bytes:
        try:
            with open_directory(home / ".fly", home=home) as directory:
                fd = os.open("apprc", READ_FLAGS, dir_fd=directory)
                try:
                    return read_fd(fd)[0]
                finally:
                    os.close(fd)
        except FileNotFoundError:
            return b""

    def _inspect(
        self, snapshot: _Snapshot, home: Path
    ) -> tuple[TrayPriorityStatus, _Line, dict[str, str | bool] | None, bytes]:
        line = _parse(snapshot.data)
        for raw in snapshot.data.removeprefix(b"\xef\xbb\xbf").split(b"\n"):
            key, sep, value = raw.strip().partition(b"=")
            if not sep or key.startswith((b"#", b";")):
                continue
            if (
                key.strip() == b"UseSystemTray" and value.strip().lower() in {b"false", b"0", b"no"}
            ) or (
                key.strip() == b"TrayBlackList"
                and any(
                    rule.strip() and rule.strip().lower() in TOKEN for rule in value.split(b":")
                )
            ):
                raise FlyTrayError("hidden")
        apprc = self._apprc(home)
        for raw in apprc.split(b"\n"):
            # One class pattern followed by flags; comments/other rules are not flags.
            fields = raw.split(b"#", 1)[0].split(b";", 1)[0].split()
            if fields and fields[0].lower() in TOKEN and b"NO_TRAY" in fields[1:]:
                raise FlyTrayError("hidden")
        state = self._state(home)
        kind: Kind = (
            "absent"
            if not line.count()
            else ("present_ours" if state is not None else "present_foreign")
        )
        code = {"absent": "absent", "present_ours": "already", "present_foreign": "foreign"}[kind]
        return (
            TrayPriorityStatus(kind, code, _MESSAGES[code], state is not None),
            line,
            state,
            apprc,
        )

    @staticmethod
    def _log(stage: str, code: str, op: str = "-", error_number: int = 0) -> None:
        _LOG.info("fly_tray stage=%s result=%s errno=%d op=%s", stage, code, error_number, op)

    def status(self) -> TrayPriorityStatus:
        try:
            home = self._home()
            with open_directory(self.themerc.parent, home=home) as directory:
                return self._inspect(self._snapshot(directory), home)[0]
        except (OSError, ThemercError) as exc:
            error = _error(exc)
            self._log("check", error.code, error_number=error.error_number)
            return TrayPriorityStatus("unavailable", error.code, error.message)

    def prepare(self, action: Action) -> str:
        if action not in {"show", "revert"}:
            raise ValueError("Unsupported tray action")
        try:
            home = self._home()
            with open_directory(self.themerc.parent, home=home) as directory:
                snapshot = self._snapshot(directory)
                status, line, state, apprc = self._inspect(snapshot, home)
                if action == "revert" and status.kind == "present_foreign":
                    raise FlyTrayError("state")
                if action == "show" and not line.count():
                    line.render(line.value + self._addition(line)[1])
            op = secrets.token_hex(16)
            with self._mutex:
                self._pending.clear()
                self._pending[op] = _Prepared(
                    action, time.monotonic() + OP_TTL, snapshot, state, apprc
                )
            self._log("check", "ok", op)
            return op
        except (OSError, ThemercError) as exc:
            error = _error(exc)
            self._log("check", error.code, error_number=error.error_number)
            raise error from None

    @staticmethod
    def _addition(line: _Line) -> tuple[str, bytes]:
        if not line.value:
            return "empty", TOKEN + b":"
        if line.value.endswith(b":"):
            return "trailing", TOKEN + b":"
        return "colon-prefix", b":" + TOKEN

    @contextmanager
    def _locked(self, home: Path) -> Iterator[int]:
        with open_directory(self.backups_dir, home=home, create=True) as directory:
            if stat.S_IMODE(os.fstat(directory).st_mode) != 0o700:
                raise FlyTrayError("administrator")
            fd = os.open(
                "fly-theme.lock",
                os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                0o600,
                dir_fd=directory,
            )
            try:
                info = os.fstat(fd)
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_uid != os.getuid()
                    or info.st_nlink != 1
                    or stat.S_IMODE(info.st_mode) != 0o600
                ):
                    raise FlyTrayError("administrator")
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise FlyTrayError("busy") from None
                yield directory
            finally:
                os.close(fd)

    def _backup(self, directory: int, data: bytes) -> None:
        name = (
            "current.themerc."
            + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            + "."
            + secrets.token_hex(8)
        )
        fd = os.open(
            name,
            os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=directory,
        )
        complete = False
        try:
            os.fchmod(fd, 0o600)
            _write_all(fd, data)
            os.fsync(fd)
            os.lseek(fd, 0, os.SEEK_SET)
            if _digest(read_fd(fd)[0]) != _digest(data):
                raise FlyTrayError("io")
            os.fsync(directory)
            complete = True
        finally:
            os.close(fd)
            if not complete:
                os.unlink(name, dir_fd=directory)
        copies: list[tuple[int, str]] = []
        for candidate in os.listdir(directory):
            if not _BACKUP.fullmatch(candidate):
                continue
            info = os.stat(candidate, dir_fd=directory, follow_symlinks=False)
            if stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and info.st_nlink == 1:
                copies.append((info.st_mtime_ns, candidate))
        for _, candidate in sorted(copies)[:-5]:
            os.unlink(candidate, dir_fd=directory)
        os.fsync(directory)

    @staticmethod
    def _metadata(fd: int, source: _Snapshot) -> None:
        try:
            current = os.fstat(fd)
            if current.st_uid != source.info.st_uid or current.st_gid != source.info.st_gid:
                os.fchown(fd, source.info.st_uid, source.info.st_gid)
            os.fchmod(fd, stat.S_IMODE(source.info.st_mode) & 0o666)
            for name in os.listxattr(fd):
                if name not in source.attrs:
                    os.removexattr(fd, name)
            for name, value in source.attrs.items():
                os.setxattr(fd, name, value)
            current = os.fstat(fd)
            if (
                stat.S_IMODE(current.st_mode) != stat.S_IMODE(source.info.st_mode) & 0o666
                or current.st_uid != source.info.st_uid
                or current.st_gid != source.info.st_gid
                or _attrs(fd) != source.attrs
            ):
                raise FlyTrayError("administrator")
        except OSError as exc:
            raise FlyTrayError("administrator", exc.errno or 0) from None

    @contextmanager
    def _stage(
        self, directory: int, data: bytes, *, source: _Snapshot | None = None
    ) -> Iterator[_Staged]:
        prefix = ".current.themerc.astra-voice." if source is not None else ".fly-tray."
        name = prefix + secrets.token_hex(16) + ".tmp"
        fd = os.open(
            name,
            os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=directory,
        )
        created = os.fstat(fd)
        try:
            try:
                _write_all(fd, data)
                if source is not None:
                    self._metadata(fd, source)
                else:
                    os.fchmod(fd, 0o600)
                os.fsync(fd)
                os.lseek(fd, 0, os.SEEK_SET)
                written, info = read_fd(fd)
                if written != data:
                    raise FlyTrayError("changed")
                temporary = _Snapshot(written, info, _attrs(fd))
            finally:
                os.close(fd)
            _check_temporary(directory, name, temporary)
            yield _Staged(name, temporary)
        finally:
            _unlink_owned_temp(directory, name, created)

    @staticmethod
    def _matches_output(actual: _Snapshot, data: bytes, metadata: _Snapshot) -> bool:
        return (
            actual.data == data
            and actual.attrs == metadata.attrs
            and stat.S_IMODE(actual.info.st_mode) == stat.S_IMODE(metadata.info.st_mode) & 0o666
            and (actual.info.st_uid, actual.info.st_gid)
            == (metadata.info.st_uid, metadata.info.st_gid)
        )

    def _restore(self, directory: int, expected: _Snapshot, undo: _Staged) -> None:
        # All rollback bytes and metadata were persisted BEFORE the first rename.
        # Recovery must not allocate or write data on a full filesystem.
        _check_temporary(directory, undo.name, undo.snapshot)
        if not expected.same(self._snapshot(directory)):
            raise FlyTrayError("changed")
        os.replace(undo.name, self.themerc.name, src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
        if not self._matches_output(self._snapshot(directory), undo.snapshot.data, undo.snapshot):
            raise FlyTrayError("changed")

    def _replace(self, directory: int, before: _Snapshot, data: bytes, undo: _Staged) -> _Snapshot:
        with self._stage(directory, data, source=before) as staged:
            installed = False
            try:
                _check_temporary(directory, staged.name, staged.snapshot)
                if not before.same(self._snapshot(directory)):
                    raise FlyTrayError("changed")
                os.replace(
                    staged.name, self.themerc.name, src_dir_fd=directory, dst_dir_fd=directory
                )
                installed = True
                # Uncooperative writers can still race this rename: accepted risk П34.
                os.fsync(directory)
                after = self._snapshot(directory)
                if not self._matches_output(after, data, before) or (
                    after.info.st_dev,
                    after.info.st_ino,
                ) != (staged.snapshot.info.st_dev, staged.snapshot.info.st_ino):
                    raise FlyTrayError("changed")
                return after
            except (OSError, ThemercError):
                if installed:
                    try:
                        observed = self._snapshot(directory)
                        if self._matches_output(observed, data, before) and (
                            observed.info.st_dev,
                            observed.info.st_ino,
                        ) == (staged.snapshot.info.st_dev, staged.snapshot.info.st_ino):
                            self._restore(directory, observed, undo)
                    except (OSError, ThemercError):
                        pass
                raise

    def _publish_state(self, directory: int, staged: _Staged | None) -> None:
        if staged is None:
            try:
                os.unlink(self.state_path.name, dir_fd=directory)
            except FileNotFoundError:
                pass
        else:
            _check_temporary(directory, staged.name, staged.snapshot)
            os.replace(
                staged.name, self.state_path.name, src_dir_fd=directory, dst_dir_fd=directory
            )
        os.fsync(directory)

    def _save_state(self, home: Path, state: dict[str, str | bool] | None) -> None:
        with open_directory(self.state_path.parent, home=home, create=True) as directory:
            if state is None:
                self._publish_state(directory, None)
            else:
                data = json.dumps(state, sort_keys=True).encode("ascii")
                with self._stage(directory, data) as staged:
                    self._publish_state(directory, staged)

    def _transaction(
        self,
        home: Path,
        directory: int,
        current: _Snapshot,
        data: bytes,
        state: dict[str, str | bool] | None,
        new_state: dict[str, str | bool] | None,
    ) -> None:
        with ExitStack() as stack:
            undo = stack.enter_context(self._stage(directory, current.data, source=current))
            state_fd = stack.enter_context(
                open_directory(self.state_path.parent, home=home, create=True)
            )
            old = (
                None
                if state is None
                else stack.enter_context(
                    self._stage(state_fd, json.dumps(state, sort_keys=True).encode("ascii"))
                )
            )
            new = (
                None
                if new_state is None
                else stack.enter_context(
                    self._stage(state_fd, json.dumps(new_state, sort_keys=True).encode("ascii"))
                )
            )
            # Reserve state bytes and directory entries before committing themerc.
            os.fsync(state_fd)
            after = self._replace(directory, current, data, undo)
            try:
                if self._state(home) != state:
                    raise FlyTrayError("changed")
                self._publish_state(state_fd, new)
            except (OSError, ThemercError):
                try:
                    if after.same(self._snapshot(directory)):
                        try:
                            self._restore(directory, after, undo)
                        except (OSError, ThemercError):
                            pass
                        # A directory fsync can fail after the rollback rename;
                        # still restore state using its prewritten inode.
                        restored = self._snapshot(directory)
                        if (
                            self._matches_output(restored, current.data, current)
                            and self._state(home) == new_state
                        ):
                            self._publish_state(state_fd, old)
                except (OSError, ThemercError):
                    pass
                raise

    def _cleanup(self, directory: int) -> None:
        for name in os.listdir(directory):
            if _TEMP.fullmatch(name):
                info = os.stat(name, dir_fd=directory, follow_symlinks=False)
                if stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and info.st_nlink == 1:
                    os.unlink(name, dir_fd=directory)

    def confirm(self, op_id: str) -> Result:
        with self._mutex:
            prepared = self._pending.pop(op_id, None)
        op = op_id if prepared is not None else "-"
        try:
            if prepared is None or time.monotonic() >= prepared.expires:
                raise FlyTrayError("consent")
            home = self._home()
            # Check before making even a lock/backup for a no-op or stale consent.
            with open_directory(self.themerc.parent, home=home) as directory:
                current = self._snapshot(directory)
                status, line, state, apprc = self._inspect(current, home)
                if (
                    not prepared.snapshot.same(current)
                    or state != prepared.state
                    or apprc != prepared.apprc
                ):
                    raise FlyTrayError("changed")
                if prepared.action == "show" and line.count():
                    return Result(True, False, "already", _MESSAGES["already"])
                with self._locked(home) as copies:
                    # Re-read only under flock: another instance may have committed meanwhile.
                    current = self._snapshot(directory)
                    status, line, state, apprc = self._inspect(current, home)
                    if (
                        not prepared.snapshot.same(current)
                        or state != prepared.state
                        or apprc != prepared.apprc
                    ):
                        raise FlyTrayError("changed")
                    self._cleanup(directory)
                    if prepared.action == "revert" and not line.count():
                        if state is not None:
                            self._save_state(home, None)
                        return Result(True, False, "already", _MESSAGES["already"])
                    if prepared.action == "show":
                        _, inserted = self._addition(line)
                        edited = line.render(line.value + inserted)
                        new_state: dict[str, str | bool] | None = {
                            "added": True,
                            "inserted": inserted.decode("ascii"),
                        }
                    else:
                        if state is None or status.kind != "present_ours":
                            raise FlyTrayError("state")
                        elements = line.value.split(b":")
                        index = next(i for i, item in enumerate(elements) if item.lower() == TOKEN)
                        # The prefix-colon belonged to our insertion. If another element
                        # now follows, preserve its separator and remove the token instead.
                        if state["inserted"] == ":astra-voice" and index == len(elements) - 1:
                            value = b":".join(elements[:index])
                        else:
                            del elements[index]
                            value = b":".join(elements)
                        edited = line.render(value)
                        new_state = None
                    data = current.data[: line.start] + edited + current.data[line.end :]
                    self._log("backup", "ok", op)
                    self._backup(copies, current.data)
                    self._transaction(home, directory, current, data, state, new_state)
                    self._log("verify", "ok", op)
                    return Result(True, True, "ok", _MESSAGES["ok"])
        except (OSError, ThemercError) as exc:
            error = _error(exc)
            self._log("check", error.code, op, error.error_number)
            return Result(False, False, error.code, error.message)
