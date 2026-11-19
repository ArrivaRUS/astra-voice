"""Bounded lifetime of the single ss child owned by an identity-checking role."""

from __future__ import annotations

import errno
import math
import os
import selectors
import stat
import subprocess
import threading
import time

from astra_voice.core.childenv import clean_env
from astra_voice.meetings.identity import DIRECTORY_FLAGS, Reason

SS_ARGV = ("/usr/bin/ss", "-x", "-H")
SS_TIMEOUT = 0.3
SS_MIN_START_BUDGET = 0.02
SS_LIMIT = 4 * 1024 * 1024
READ_SIZE = 64 * 1024


class SsFailure(Exception):
    def __init__(self, reason: Reason) -> None:
        super().__init__(reason.value)
        self.reason = reason


def _trusted(directory: int) -> bool:
    info = os.fstat(directory)
    return info.st_uid == 0 and not info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)


def _ss_absent() -> bool:
    """Distinguish a missing directory entry from a missing interpreter."""
    try:
        directory = os.open("/", DIRECTORY_FLAGS)
        try:
            if not _trusted(directory):
                return False
            for component in ("usr", "bin"):
                next_directory = os.open(component, DIRECTORY_FLAGS, dir_fd=directory)
                os.close(directory)
                directory = next_directory
                if not _trusted(directory):
                    return False
            try:
                os.stat("ss", dir_fd=directory, follow_symlinks=False)
            except FileNotFoundError:
                return True
        finally:
            os.close(directory)
    except OSError:
        pass
    return False


class SsRunner:
    """Keep only the child handle until reaped. Call shutdown at role shutdown."""

    def __init__(self) -> None:
        self._child: subprocess.Popen[bytes] | None = None
        self._lock = threading.Lock()

    def run(self, deadline: float) -> bytearray:
        if not self._lock.acquire(blocking=False):
            raise SsFailure(Reason.SS_INSUFFICIENT)
        try:
            return self._run(deadline)
        finally:
            self._lock.release()

    def _run(self, deadline: float) -> bytearray:
        if self._child is not None and self._child.poll() is None:
            raise SsFailure(Reason.SS_INSUFFICIENT)
        self._child = None
        env = clean_env({"LC_ALL": "C", "PATH": "/usr/bin:/bin"})
        now = time.monotonic()
        if not math.isfinite(deadline) or deadline < now + SS_MIN_START_BUDGET:
            raise SsFailure(Reason.SS_TIMEOUT)
        deadline = min(deadline, now + SS_TIMEOUT)
        try:
            child = subprocess.Popen(
                SS_ARGV,
                shell=False,
                env=env,
                close_fds=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=0,
            )
        except OSError as exc:
            reason = (
                Reason.SS_UNAVAILABLE
                if exc.errno == errno.ENOENT and _ss_absent()
                else Reason.SS_INSUFFICIENT
            )
            self._remaining(deadline)
            raise SsFailure(reason) from None

        self._child = child
        try:
            if child.stdout is None:
                raise SsFailure(Reason.SS_INSUFFICIENT)
            with child.stdout as stream:
                output = self._read(stream.fileno(), deadline)
            if child.wait(timeout=self._remaining(deadline)) != 0:
                raise SsFailure(Reason.SS_INSUFFICIENT)
            self._remaining(deadline)
            return output
        except subprocess.TimeoutExpired:
            raise SsFailure(Reason.SS_TIMEOUT) from None
        except OSError:
            raise SsFailure(Reason.SS_INSUFFICIENT) from None
        finally:
            self._reap(deadline)

    @staticmethod
    def _remaining(deadline: float) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise SsFailure(Reason.SS_TIMEOUT)
        return remaining

    @staticmethod
    def _read(descriptor: int, deadline: float) -> bytearray:
        os.set_blocking(descriptor, False)
        output = bytearray()
        with selectors.DefaultSelector() as selector:
            selector.register(descriptor, selectors.EVENT_READ)
            while True:
                if not selector.select(SsRunner._remaining(deadline)):
                    raise SsFailure(Reason.SS_TIMEOUT)
                SsRunner._remaining(deadline)
                try:
                    chunk = os.read(descriptor, min(READ_SIZE, SS_LIMIT + 1 - len(output)))
                except BlockingIOError:
                    continue
                if not chunk:
                    return output
                output.extend(chunk)
                if len(output) > SS_LIMIT:
                    raise SsFailure(Reason.SS_LIMIT)

    def _reap(self, deadline: float) -> bool:
        child = self._child
        if child is None:
            return True
        if child.returncode is None:
            try:
                child.kill()
            except OSError:
                pass
            try:
                remaining = (
                    max(0.0, deadline - time.monotonic()) if math.isfinite(deadline) else 0.0
                )
                child.wait(timeout=remaining)
            except (OSError, subprocess.TimeoutExpired):
                return False
        self._child = None
        return True

    def shutdown(self, deadline: float) -> bool:
        """True once there is no outstanding child; caller may retry if false."""
        if not self._lock.acquire(blocking=False):
            return False
        try:
            return self._reap(deadline)
        finally:
            self._lock.release()
