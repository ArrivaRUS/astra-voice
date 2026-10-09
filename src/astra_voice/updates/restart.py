"""Prepare an AppImage restart off the GUI thread; launch only after teardown.

Attempt records are bounded diagnostics, never executable authority. A successful
Popen is only a launch request; a later matching AppImage version clears notice.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import stat
import subprocess
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from astra_voice.core import paths
from astra_voice.core.childenv import clean_env
from astra_voice.core.policy import Policy, PolicyStatus, appimage_denied
from astra_voice.net.update_cache import read_private, write_private
from astra_voice.updates.download import FileIdentity, VerifiedDownload
from astra_voice.updates.release import ReleaseMetadata, ReleaseMetadataError

log = logging.getLogger(__name__)
_MAX_ATTEMPT_BYTES = 4096
_VERSION = re.compile(r"(?:0|[1-9][0-9]{0,8})\.(?:0|[1-9][0-9]{0,8})\.(?:0|[1-9][0-9]{0,8})")
_ERRORS = frozenset(
    {
        "",
        "cancelled",
        "policy",
        "admin",
        "appimage-denied",
        "busy",
        "unsupported",
        "path-unsafe",
        "file-changed",
        "hash-mismatch",
        "signature-invalid",
        "metadata-invalid",
        "prepare-failed",
        "shutdown-incomplete",
        "launch-failed",
        "launch-unconfirmed",
    }
)
_STAGES = frozenset({"prepared", "launch_requested", "launch_failed", "shutdown_failed"})


class RestartError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code if code in _ERRORS else "prepare-failed"
        super().__init__(self.code)


def install_refusal(policy: Policy) -> str:
    """Local capability; offline alone does not forbid a verified local image."""
    if policy.status is PolicyStatus.INVALID:
        return "policy"
    if appimage_denied(policy):
        return "appimage-denied"
    updates = policy.values.get("updates", "")
    if str(updates).strip().lower() == "admin":
        return "admin"
    if str(updates).strip().lower() not in ("", "allow", "yes", "on", "true", "1"):
        return "policy"
    return ""


def _no_symlinks(path: Path) -> None:
    if (
        not path.is_absolute()
        or ".." in path.parts
        or any(part.is_symlink() for part in (path, *path.parents))
    ):
        raise RestartError("path-unsafe")


def _private_directory(path: Path) -> None:
    _no_symlinks(path)
    info = path.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise RestartError("path-unsafe")


def _identity(path: Path) -> FileIdentity:
    _no_symlinks(path)
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_nlink != 1
        or stat.S_IMODE(info.st_mode) != 0o755
    ):
        raise RestartError("file-changed")
    return FileIdentity.from_stat(info)


@dataclass(frozen=True)
class AttemptNotice:
    version: str
    error: str


@dataclass(frozen=True)
class PendingRestart:
    download: VerifiedDownload
    attempt_id: str
    bundle_id: str
    environment: tuple[tuple[str, str], ...] = field(repr=False)


class RestartPreparer:
    def __init__(
        self,
        metadata: ReleaseMetadata,
        current_version: str,
        *,
        policy: Callable[[], Policy],
        record_path: Path | None = None,
    ) -> None:
        self._metadata = metadata
        self._current_version = current_version
        self._policy = policy
        self.record_path = (
            record_path
            if record_path is not None
            else paths.cache_dir_path() / "updates" / "restart-attempt.json"
        )

    def _guard(self, cancel: threading.Event | None = None) -> None:
        if cancel is not None and cancel.is_set():
            raise RestartError("cancelled")
        reason = install_refusal(self._policy())
        if reason:
            raise RestartError(reason)

    def _record_safe(self, *, create: bool) -> None:
        _no_symlinks(self.record_path)
        if create:
            # Parent chain inspected before helpers may mkdir/chmod through XDG.
            if self.record_path.parent == paths.cache_dir_path() / "updates":
                paths.ensure_private_dir(paths.cache_dir_path())
            paths.ensure_private_dir(self.record_path.parent)
        _private_directory(self.record_path.parent)
        try:
            info = self.record_path.lstat()
        except FileNotFoundError:
            return
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise RestartError("path-unsafe")

    def _record(self, pending: PendingRestart, stage: str, error: str = "") -> None:
        if stage not in _STAGES or error not in _ERRORS:
            raise ValueError("Unknown restart stage/error")
        self._record_safe(create=True)
        body = json.dumps(
            {
                "schema": 1,
                "attempt_id": pending.attempt_id,
                "target_version": pending.download.release.version,
                "stage": stage,
                "error": error,
                "bundle_id": pending.bundle_id,
            },
            separators=(",", ":"),
        ).encode()
        if len(body) > _MAX_ATTEMPT_BYTES:
            raise RestartError("prepare-failed")
        write_private(self.record_path, body)

    def notice(self, current_version: str, *, is_appimage: bool) -> AttemptNotice | None:
        """Read diagnostics only: no path, environment or command comes from JSON."""
        try:
            self._record_safe(create=False)
            data = json.loads(read_private(self.record_path, _MAX_ATTEMPT_BYTES))
            if not isinstance(data, dict) or set(data) != {
                "schema",
                "attempt_id",
                "target_version",
                "stage",
                "error",
                "bundle_id",
            }:
                return None
            if (
                type(data["schema"]) is not int
                or data["schema"] != 1
                or not isinstance(data["target_version"], str)
                or not _VERSION.fullmatch(data["target_version"])
                or not isinstance(data["attempt_id"], str)
                or not re.fullmatch(r"[0-9a-f]{32}", data["attempt_id"])
                or not isinstance(data["bundle_id"], str)
                or not re.fullmatch(r"[0-9a-f]{64}", data["bundle_id"])
                or not isinstance(data["stage"], str)
                or data["stage"] not in _STAGES
                or not isinstance(data["error"], str)
                or data["error"] not in _ERRORS
            ):
                return None
            if is_appimage and current_version == data["target_version"]:
                self.record_path.unlink()
                return None
            return AttemptNotice(data["target_version"], data["error"] or "launch-unconfirmed")
        except (OSError, ValueError, TypeError, RecursionError, RestartError):
            return None

    def prepare(self, result: VerifiedDownload, *, cancel: threading.Event) -> PendingRestart:
        try:
            self._guard(cancel)
            if result.release.artifact.track != "appimage":
                raise RestartError("unsupported")
            if result.path.name != result.release.artifact.name:
                raise RestartError("file-changed")
            _private_directory(result.path.parent)
            if _identity(result.path) != result.identity:
                raise RestartError("file-changed")
            trusted = self._metadata.validate_local(
                result.path.parent,
                result.release.raw_tag,
                self._current_version,
                "appimage",
                cancel=cancel,
            )
            if trusted != result.release:
                raise RestartError("metadata-invalid")
            self._guard(cancel)
            fd = os.open(result.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as source:
                if FileIdentity.from_stat(os.fstat(source.fileno())) != result.identity:
                    raise RestartError("file-changed")
                remaining = trusted.artifact.size
                digest = hashlib.sha256()
                while remaining:
                    self._guard(cancel)
                    block = source.read(min(1 << 20, remaining))
                    if not block:
                        raise RestartError("file-changed")
                    digest.update(block)
                    remaining -= len(block)
                if source.read(1) or digest.hexdigest() != trusted.artifact.sha256:
                    raise RestartError("hash-mismatch")
                if FileIdentity.from_stat(os.fstat(source.fileno())) != result.identity:
                    raise RestartError("file-changed")
            # clean_env creates/chmods cache/tmp, so inspect ancestors first.
            _no_symlinks(paths.cache_dir_path() / "tmp")
            environment = clean_env(private_tmp=True)
            _private_directory(Path(environment["TMPDIR"]))
            self._guard(cancel)
            if _identity(result.path) != result.identity:
                raise RestartError("file-changed")
            pending = PendingRestart(
                result,
                uuid.uuid4().hex,
                hashlib.sha256(os.fsencode(result.path.parent)).hexdigest(),
                tuple(sorted(environment.items())),
            )
            self._record(pending, "prepared")
            self._guard(cancel)
            return pending
        except ReleaseMetadataError as error:
            raise RestartError(error.code) from None
        except (OSError, paths.PathError):
            raise RestartError("prepare-failed") from None

    def fail(self, pending: PendingRestart, error: str) -> None:
        try:
            self._record(
                pending,
                "shutdown_failed" if error == "shutdown-incomplete" else "launch_failed",
                error if error in _ERRORS else "prepare-failed",
            )
        except (OSError, ValueError, RestartError, paths.PathError):
            log.warning("Не удалось записать результат попытки перезапуска")

    def launch(self, pending: PendingRestart, *, shutdown_complete: bool) -> int:
        """Caller has joined workers and released IPC/instance lock before this."""
        if not shutdown_complete:
            self.fail(pending, "shutdown-incomplete")
            return 1
        try:
            self._guard()
            if _identity(pending.download.path) != pending.download.identity:
                raise RestartError("file-changed")
            environment = dict(pending.environment)
            _private_directory(Path(environment["TMPDIR"]))
            self._record(pending, "launch_requested")
            subprocess.Popen(  # noqa: S603 — signed local image, fixed runtime argument
                [str(pending.download.path), "--appimage-extract-and-run"],
                env=environment,
                close_fds=True,
                start_new_session=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            # Do not write after Popen: the new copy may already read the record.
            return 0
        except RestartError as error:
            self.fail(pending, error.code)
        except (OSError, ValueError, KeyError, paths.PathError):
            self.fail(pending, "launch-failed")
        log.warning("Новая версия не запущена; проверенный файл сохранён")
        return 1


class RestartController:
    """Own preparation worker; GUI callbacks must be queued by the caller."""

    def __init__(
        self,
        preparer: RestartPreparer,
        *,
        on_prepared: Callable[[PendingRestart | None, str], None] | None = None,
    ) -> None:
        self.preparer = preparer
        self.on_prepared = on_prepared
        self._lock = threading.Lock()
        self._closed = False
        self._cancel = threading.Event()
        self._worker: threading.Thread | None = None

    @property
    def worker_threads(self) -> tuple[threading.Thread, ...]:
        with self._lock:
            return (self._worker,) if self._worker is not None else ()

    def start(self, result: VerifiedDownload) -> bool:
        with self._lock:
            if self._closed or (self._worker is not None and self._worker.is_alive()):
                return False
            self._cancel = threading.Event()
            self._worker = threading.Thread(target=self._run, args=(result,), name="update-prepare")
            try:
                self._worker.start()
            except RuntimeError:
                self._worker = None
                return False
            return True

    def _run(self, result: VerifiedDownload) -> None:
        pending = None
        error = ""
        try:
            pending = self.preparer.prepare(result, cancel=self._cancel)
        except RestartError as failure:
            error = failure.code
        except Exception:  # noqa: BLE001 — worker failure cannot silently leave GUI installing
            log.warning("Подготовка перезапуска не завершена", exc_info=True)
            error = "prepare-failed"
        with self._lock:
            callback = None if self._closed else self.on_prepared
        if callback is not None:
            try:
                callback(pending, error)
            except Exception:  # noqa: BLE001 — callback must not strand the owned worker
                log.warning("Не удалось передать результат подготовки перезапуска")

    def close(self, timeout: float | None = None) -> bool:
        with self._lock:
            self._closed = True
            self._cancel.set()
            worker = self._worker
        if worker is threading.current_thread():
            return False
        if worker is not None:
            worker.join(timeout)
            if worker.is_alive():
                return False
        with self._lock:
            self._worker = None
        return True
