"""A recoverable lease of KWin's single ModifierOnlyShortcuts/Meta value.

GUI and broker share the fixed journal and lock, and restore each other's lease
on heartbeat/EOF failure. No Qt import is needed for recovery. The caller must
register the no-op endpoint on its own unique session-bus connection *before*
acquire, and retain that endpoint until release. ``recover`` is for startup
(after excluding a live previous instance), never a periodic janitor.

The journal is durable before the first KConfig write. All writers in this
module serialize; KConfig has no key compare-and-swap against unrelated writers.
An external edit between our final read and write is therefore an unavoidable
race. Changes observed before restoration are preserved. Both peers killed,
frozen, or powered off together require recovery on the next application start;
this module does not claim a third watchdog or a system-wide crash guarantee.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import math
import os
import re
import stat
import subprocess
import time
import uuid
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

from astra_voice.core import childenv

NOOP_INTERFACE = "org.astralinux.AstraVoice.CommandMetaLease1"
NOOP_METHOD = "Ignore"
JOURNAL_NAME = "lease.json"
LOCK_NAME = "lease.lock"
_MAX_VALUE = 8192
_MAX_JOURNAL = 65536
_SERVICE = re.compile(r":1\.[0-9]+", re.ASCII)
_GENERATION = re.compile(r"[0-9a-f]{32}", re.ASCII)
Runner = Callable[[Sequence[str], float], str]


class KWinLeaseError(RuntimeError):
    """Lease cannot safely be acquired or restored; retry recovery later."""


class LeaseBusyError(KWinLeaseError):
    """Another peer holds the journal lock beyond the bounded wait."""


def object_path(generation: str) -> str:
    if not _GENERATION.fullmatch(generation):
        raise KWinLeaseError("Invalid lease generation")
    return f"/org/astralinux/AstraVoice/CommandMetaLease/g{generation}"


def lease_token(unique_service: str, generation: str) -> str:
    """Only a unique connection plus our fixed no-argument method is accepted."""
    if not _SERVICE.fullmatch(unique_service) or len(unique_service) > 64:
        raise KWinLeaseError("A unique session-bus service is required")
    return ",".join((unique_service, object_path(generation), NOOP_INTERFACE, NOOP_METHOD))


def _validate_token(token: str) -> None:
    pieces = token.split(",")
    if len(pieces) != 4:
        raise KWinLeaseError("Invalid lease token")
    generation = pieces[1].rsplit("/g", 1)[-1]
    if lease_token(pieces[0], generation) != token:
        raise KWinLeaseError("Invalid lease endpoint")


def _xdg(name: str, fallback: Path) -> Path:
    raw = os.environ.get(name, "")
    return Path(raw) if raw.startswith("/") else fallback


def _run(argv: Sequence[str], timeout: float, *, lock_fd: int) -> str:
    try:
        result = subprocess.run(
            list(argv),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            shell=False,
            timeout=timeout,
            check=True,
            env=childenv.clean_env(),
            # A writer surviving its owner must keep peers out until it exits.
            # pass_fds clears CLOEXEC in the child without a Python preexec_fn.
            pass_fds=(lock_fd,),
        )
    except (OSError, subprocess.SubprocessError, UnicodeError) as exc:
        raise KWinLeaseError("KWin configuration command failed") from exc
    return result.stdout.decode("utf-8")


@dataclass(frozen=True)
class MetaValue:
    """Effective KConfig string with absence distinct from an explicit empty."""

    present: bool
    value: str = ""


@dataclass(frozen=True)
class _Journal:
    token: str
    previous: MetaValue


def _check_directory(info: os.stat_result, *, private: bool = False) -> None:
    if not stat.S_ISDIR(info.st_mode) or info.st_uid not in (0, os.geteuid()):
        raise KWinLeaseError("Unsafe lease directory ownership")
    if private:
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise KWinLeaseError("Lease directory must be private (0700)")
    elif info.st_mode & 0o022 and not (info.st_uid == 0 and info.st_mode & stat.S_ISVTX):
        raise KWinLeaseError("Writable lease path ancestor")


@contextlib.contextmanager
def _directory(path: Path, *, create: bool, private: bool = False) -> Iterator[int]:
    """Walk dirfds without following symlinks, including ancestor components."""
    if not path.is_absolute() or ".." in path.parts:
        raise KWinLeaseError("An absolute normalized path is required")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for component in path.parts[1:]:
            if create:
                try:
                    os.mkdir(component, mode=0o700, dir_fd=fd)
                    os.fsync(fd)
                except FileExistsError:
                    pass
            next_fd = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=fd,
            )
            os.close(fd)
            fd = next_fd
            _check_directory(os.fstat(fd))
        _check_directory(os.fstat(fd), private=private)
        yield fd
    finally:
        os.close(fd)


def _check_file(fd: int, *, private: bool) -> os.stat_result:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1:
        raise KWinLeaseError("Unsafe configuration or lease file")
    mode = stat.S_IMODE(info.st_mode)
    if (private and mode != 0o600) or (not private and mode & 0o022):
        raise KWinLeaseError("Unsafe configuration or lease file permissions")
    return info


class KWinCommandLease:
    """One fixed journal, usable by either process without a GUI dependency.

    Optional paths and runner are injection points for tests, never IPC fields.
    No process may supply a restore destination or journal path over the wire.
    Exceptions retain the journal whenever restoration has not been verified.
    Each subprocess and lock wait is bounded, not an entire multi-command call.
    Native commands inherit the flock descriptor: if an owner dies, its surviving
    writer fences recovery until exit. A stopped orphan causes bounded busy
    failures (journal retained); callers retry after the writer can finish.
    """

    def __init__(
        self,
        *,
        state_dir: Path | None = None,
        config_path: Path | None = None,
        runner: Runner | None = None,
        timeout: float = 1.0,
        lock_timeout: float = 0.5,
    ) -> None:
        if not all(math.isfinite(v) and 0 < v <= 10 for v in (timeout, lock_timeout)):
            raise ValueError("Lease timeouts must be finite, positive, and at most 10s")
        self.state_dir = state_dir or (
            _xdg("XDG_STATE_HOME", Path.home() / ".local/state")
            / "astra-voice"
            / "kwin-command-lease"
        )
        self.config_path = config_path or (
            _xdg("XDG_CONFIG_HOME", Path.home() / ".config") / "kwinrc"
        )
        self._runner = runner
        self._lock_fd: int | None = None
        self.timeout = timeout
        self.lock_timeout = lock_timeout

    @contextlib.contextmanager
    def _locked(self) -> Iterator[int]:
        try:
            with _directory(self.state_dir, create=True, private=True) as directory:
                fd = os.open(
                    LOCK_NAME,
                    os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                    0o600,
                    dir_fd=directory,
                )
                try:
                    _check_file(fd, private=True)
                    deadline = time.monotonic() + self.lock_timeout
                    while True:
                        try:
                            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                            break
                        except BlockingIOError:
                            if time.monotonic() >= deadline:
                                raise LeaseBusyError("KWin lease peer is busy") from None
                            time.sleep(min(0.01, max(0, deadline - time.monotonic())))
                    self._lock_fd = fd
                    try:
                        yield directory
                    finally:
                        # Do not LOCK_UN: a surviving child shares this open file
                        # description and must retain the lock after owner death.
                        self._lock_fd = None
                finally:
                    os.close(fd)
        except OSError as exc:
            raise KWinLeaseError("Cannot safely access KWin lease files") from exc

    def _read_journal(self, directory: int) -> _Journal | None:
        try:
            fd = os.open(
                JOURNAL_NAME,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                dir_fd=directory,
            )
        except FileNotFoundError:
            return None
        try:
            info = _check_file(fd, private=True)
            if info.st_size > _MAX_JOURNAL:
                raise KWinLeaseError("Oversized KWin lease journal")
            with os.fdopen(fd, "r", encoding="utf-8", closefd=False) as stream:
                data = json.loads(stream.read(_MAX_JOURNAL + 1))
        except (ValueError, UnicodeError) as exc:
            raise KWinLeaseError("Invalid KWin lease journal") from exc
        finally:
            os.close(fd)
        if (
            not isinstance(data, dict)
            or set(data) != {"version", "token", "present", "value"}
            or type(data["version"]) is not int
            or data["version"] != 1
            or type(data["present"]) is not bool
            or not isinstance(data["token"], str)
            or not isinstance(data["value"], str)
            or len(data["value"]) > _MAX_VALUE
            or "\0" in data["value"]
            or (not data["present"] and data["value"] != "")
        ):
            raise KWinLeaseError("Invalid KWin lease journal schema")
        _validate_token(data["token"])
        return _Journal(data["token"], MetaValue(data["present"], data["value"]))

    def _write_journal(self, directory: int, journal: _Journal) -> None:
        data = json.dumps(
            {
                "version": 1,
                "token": journal.token,
                "present": journal.previous.present,
                "value": journal.previous.value,
            },
            ensure_ascii=False,
        ).encode("utf-8")
        name = f".lease-{uuid.uuid4().hex}.tmp"
        fd = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=directory,
        )
        try:
            with os.fdopen(fd, "wb", closefd=False) as stream:
                stream.write(data)
                stream.flush()
                os.fsync(fd)
            os.replace(name, JOURNAL_NAME, src_dir_fd=directory, dst_dir_fd=directory)
            os.fsync(directory)
        finally:
            os.close(fd)
            try:
                os.unlink(name, dir_fd=directory)
            except FileNotFoundError:
                pass

    def _config_check(self) -> None:
        # KConfig updates this file itself. Hold no stale fd across its atomic rename.
        with _directory(self.config_path.parent, create=False) as directory:
            try:
                fd = os.open(
                    self.config_path.name,
                    os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                    dir_fd=directory,
                )
            except FileNotFoundError:
                return
            try:
                _check_file(fd, private=False)
            finally:
                os.close(fd)

    def _command(self, argv: list[str]) -> str:
        try:
            if self._runner is not None:
                # Test-only injection. Such runners must not launch unfenced
                # asynchronous configuration writers.
                return self._runner(argv, self.timeout)
            if self._lock_fd is None:
                raise KWinLeaseError("Configuration command requires the lease lock")
            return _run(argv, self.timeout, lock_fd=self._lock_fd)
        except KWinLeaseError:
            raise
        except (OSError, subprocess.SubprocessError, UnicodeError) as exc:
            raise KWinLeaseError("KWin configuration command failed") from exc

    def _key_args(self) -> list[str]:
        self._config_check()
        return [
            "--file",
            str(self.config_path),
            "--group",
            "ModifierOnlyShortcuts",
            "--key",
            "Meta",
        ]

    def _read_meta(self) -> MetaValue:
        # Two distinct defaults distinguish absence even if the actual value equals
        # either sentinel. Only remove the utility's final newline, never whitespace.
        defaults = [f"astra-voice-absent-{uuid.uuid4().hex}" for _ in range(2)]
        values = []
        for default in defaults:
            output = self._command(
                ["/usr/bin/kreadconfig5", *self._key_args(), "--default", default]
            )
            if not output.endswith("\n") or len(output) > _MAX_VALUE + 1 or "\0" in output:
                raise KWinLeaseError("Invalid KConfig readback")
            values.append(output[:-1])
        if values == defaults:
            return MetaValue(False)
        if values[0] != values[1]:
            raise KWinLeaseError("Meta changed while reading KConfig")
        return MetaValue(True, values[0])

    def _write_meta(self, value: MetaValue) -> None:
        args = ["/usr/bin/kwriteconfig5", *self._key_args()]
        # -- stops custom values beginning with '-' becoming CLI options.
        args.extend(["--", value.value] if value.present else ["--delete"])
        self._command(args)
        if self._read_meta() != value:
            raise KWinLeaseError("KConfig write did not match readback")

    def _sync_config(self) -> None:
        with _directory(self.config_path.parent, create=False) as directory:
            try:
                fd = os.open(
                    self.config_path.name,
                    os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                    dir_fd=directory,
                )
            except FileNotFoundError:
                pass
            else:
                try:
                    _check_file(fd, private=False)
                    os.fsync(fd)
                finally:
                    os.close(fd)
            os.fsync(directory)

    def _reconfigure(self) -> None:
        self._command(["/usr/bin/qdbus", "org.kde.KWin", "/KWin", "org.kde.KWin.reconfigure"])

    def _restore(self, directory: int, journal: _Journal) -> None:
        current = self._read_meta()
        if current == MetaValue(True, journal.token):
            self._write_meta(journal.previous)
        self._sync_config()
        # Also retry after a crash between restoring the file and reconfigure, or
        # after a foreign edit. Never restore an observed foreign value.
        self._reconfigure()
        os.unlink(JOURNAL_NAME, dir_fd=directory)
        os.fsync(directory)

    def acquire(self, unique_service: str, generation: str) -> str:
        """Acquire only with no journal; startup recovery is an explicit operation."""
        token = lease_token(unique_service, generation)
        with self._locked() as directory:
            old = self._read_journal(directory)
            if old is not None:
                raise LeaseBusyError("Existing KWin lease requires explicit recovery")
            previous = self._read_meta()
            journal = _Journal(token, previous)
            self._write_journal(directory, journal)
            try:
                self._write_meta(MetaValue(True, token))
                self._sync_config()
                self._reconfigure()
            except Exception as exc:
                try:
                    self._restore(directory, journal)
                except Exception as restore_exc:
                    raise KWinLeaseError(
                        "Acquire failed; journal retained for recovery"
                    ) from restore_exc
                raise KWinLeaseError("Acquire failed; previous Meta restored") from exc
        return token

    def release(self, expected_token: str) -> bool:
        """Restore only this generation; an obsolete peer cannot release a new one."""
        _validate_token(expected_token)
        with self._locked() as directory:
            journal = self._read_journal(directory)
            if journal is None or journal.token != expected_token:
                return False
            self._restore(directory, journal)
            return True

    def recover(self) -> bool:
        """Startup recovery from the known local journal, including both-peers-dead."""
        with self._locked() as directory:
            journal = self._read_journal(directory)
            if journal is None:
                return False
            self._restore(directory, journal)
            return True
