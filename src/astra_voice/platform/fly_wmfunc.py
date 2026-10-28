"""ИБ72: narrow, synchronous boundary for Fly WM operations.

Call from a worker, never the GUI thread. This module does not modify settings.
X11 connections and process descriptors belong to one call only.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import re
import stat
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from astra_voice.core import childenv
from astra_voice.platform import session

log = logging.getLogger(__name__)
_BINARY = "/usr/bin/fly-wmfunc"
_XA_CARDINAL = 6  # X11 predefined CARDINAL atom.
_COMMANDS = frozenset({"FLYWM_UPDATE_STARTMENU", "FLYWM_RESTART"})
_PROBE_TIMEOUT = 0.75
_PROBE_ERRORS = {20: "session_not_fly", 21: "wm_unavailable", 22: "display_unavailable"}
_PROBE_SCRIPT = """import json, sys
sys.path.extend(json.loads(sys.argv[1]))
from astra_voice.platform.fly_wmfunc import _probe_child
raise SystemExit(_probe_child(sys.argv[2]))
"""


class FlyWmError(RuntimeError):
    """Stable error code; no command output or private paths in the message."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _failure(stage: str, code: str, error: OSError | None = None) -> FlyWmError:
    log.warning("fly_wmfunc stage=%s code=%s errno=%s", stage, code, error.errno if error else 0)
    return FlyWmError(code)


def check_binary(command: str = "FLYWM_RESTART") -> list[str]:
    """Validate the closed command set before any I/O; never search PATH."""
    if command not in _COMMANDS:
        raise ValueError("unsupported Fly WM command")
    try:
        info = os.lstat(_BINARY)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise _failure("binary", "binary_unsafe")
        if not os.access(_BINARY, os.X_OK):
            raise _failure("binary", "binary_unavailable")
    except OSError as exc:
        raise _failure("binary", "binary_unavailable", exc) from None
    return [_BINARY, command]


def _connect() -> Any:
    from Xlib import display

    name = os.environ.get("DISPLAY", "").strip()
    if not name:
        raise _failure("session", "display_unavailable")
    return display.Display(name)


@contextmanager
def _connection(stage: str) -> Iterator[Any]:
    conn = None
    try:
        conn = _connect()
        yield conn
    except FlyWmError:
        raise
    except Exception as exc:  # Xlib has several exception classes, sometimes no dependency.
        code = "display_unavailable"
        if conn is not None and stage == "session":
            try:
                from Xlib.error import BadAtom, BadWindow
            except ImportError:  # Optional Xlib; connection failures keep their existing code.
                pass
            else:
                if isinstance(exc, (BadAtom, BadWindow)):
                    code = "wm_unavailable"
        error = exc if isinstance(exc, OSError) else None
        raise _failure(stage, code, error) from None
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # Do not replace the original result with a close failure.
                log.warning("fly_wmfunc stage=close code=display_unavailable errno=0")


def _check_process(pid: int) -> None:
    fd = -1
    try:
        # Anchor proc lookup to this process directory, avoiding separate pathname lookups
        # against a potentially recycled PID. A zombie/dead process has no exe link.
        fd = os.open(f"/proc/{pid}", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        if os.fstat(fd).st_uid != os.getuid():
            raise _failure("session", "wm_unavailable")
        executable = os.readlink("exe", dir_fd=fd)
        if executable.rsplit("/", 1)[-1] != "fly-wm":
            raise _failure("session", "wm_unavailable")
    except OSError as exc:
        raise _failure("session", "wm_unavailable", exc) from None
    finally:
        if fd >= 0:
            os.close(fd)


def _check_session_local() -> None:
    """Require Fly plus a legacy PID or a live, self-linked EWMH Fly window.

    Any PID used for verification must identify a live, same-user fly-wm. Fly versions
    with no PID properties can instead be identified by the EWMH window alone.
    """
    if session.detect() is not session.SessionKind.FLY:
        raise _failure("session", "session_not_fly")
    with _connection("session") as conn:
        root = conn.screen().root
        try:
            prop = session._x11_property(conn, root, "_FLY_WM_PID")
            if prop is not None:
                # A malformed legacy PID must not fall through to the weaker EWMH path.
                pid = session._x11_uint32(prop, _XA_CARDINAL)
            else:
                window, name = session._ewmh_wm(conn, root)
                if name != "fly-wm":
                    raise ValueError("not Fly WM")
                prop = session._x11_property(conn, window, "_NET_WM_PID")
                if prop is None:
                    return
                pid = session._x11_uint32(prop, _XA_CARDINAL)
        except ValueError:
            raise _failure("session", "wm_unavailable") from None
        _check_process(pid)


def _tray_owner_local() -> int:
    """Selection owner on this DISPLAY's default screen, or zero if absent."""
    with _connection("tray") as conn:
        atom = conn.intern_atom(
            f"_NET_SYSTEM_TRAY_S{conn.get_default_screen()}", only_if_exists=True
        )
        if not atom:
            return 0
        owner = conn.get_selection_owner(atom)
        return int(owner.id) if owner else 0


def _probe_paths() -> list[str]:
    """Only the installed application and its two pure-Python Xlib dependency roots.

    Do not forward sys.path: it can contain cwd, PYTHONPATH and user-site entries.
    The bundled interpreter uses RUNPATH and needs no inherited LD_* settings.
    """
    roots = [str(Path(__file__).resolve().parents[2])]
    for module in ("Xlib", "six"):
        spec = importlib.util.find_spec(module)
        if spec is None or spec.origin is None or not os.path.isabs(spec.origin):
            raise _failure("probe", "display_unavailable")
        source = Path(spec.origin)
        root = source.parent.parent if module == "Xlib" else source.parent
        if str(root) not in roots:
            roots.append(str(root))
    return roots


def _system_env() -> dict[str, str]:
    env = childenv.clean_env()
    # clean_env restores original loader values. System WM/probe must not use them.
    for name in tuple(env):
        if name in {"LD_LIBRARY_PATH", "LD_PRELOAD", "LD_AUDIT"} or name.startswith(
            ("APPIMAGE", "PYTHON")
        ):
            del env[name]
    return env


def _probe_child(operation: str) -> int:
    """Private child entry: bounded numeric output and fixed exit codes only."""
    try:
        if operation == "session":
            _check_session_local()
            value = 0
        elif operation == "tray":
            value = _tray_owner_local()
        else:
            return 22
        if not 0 <= value <= 0xFFFFFFFF:
            return 22
    except FlyWmError as exc:
        return next((key for key, code in _PROBE_ERRORS.items() if code == exc.code), 22)
    except Exception:
        return 22
    print(value, flush=True)
    return 0


def _probe(operation: str, timeout: float = _PROBE_TIMEOUT) -> int:
    if operation not in {"session", "tray"}:
        raise ValueError("unsupported Fly probe")
    if not 0 < timeout <= _PROBE_TIMEOUT:
        raise ValueError("invalid Fly probe timeout")
    try:
        if not os.path.isabs(sys.executable):
            raise _failure("probe", "display_unavailable")
        argv = [
            sys.executable,
            "-I",
            "-S",
            "-B",
            "-c",
            _PROBE_SCRIPT,
            json.dumps(_probe_paths()),
            operation,
        ]
        result = subprocess.run(
            argv,
            shell=False,
            timeout=timeout,
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=_system_env(),
        )
    except subprocess.TimeoutExpired:
        # run kills and reaps the child before raising; even handshake/close is bounded.
        raise _failure("probe", "display_unavailable") from None
    except (OSError, ImportError, ValueError) as exc:
        error = exc if isinstance(exc, OSError) else None
        raise _failure("probe", "display_unavailable", error) from None
    if result.returncode:
        raise _failure("probe", _PROBE_ERRORS.get(result.returncode, "display_unavailable"))
    output = result.stdout
    if (
        not isinstance(output, bytes)
        or len(output) > 11
        or re.fullmatch(rb"(0|[1-9][0-9]{0,9})\n", output) is None
        or int(output) > 0xFFFFFFFF
    ):
        raise _failure("probe", "display_unavailable")
    return int(output)


def check_session() -> None:
    """Require a live Fly session; all X11/detection I/O runs in a bounded child."""
    if _probe("session") != 0:
        raise _failure("probe", "display_unavailable")


def tray_owner(*, timeout: float = _PROBE_TIMEOUT) -> int:
    """Tray owner or zero; caller may pass a smaller remaining deadline budget."""
    return _probe("tray", timeout)


def run(command: str) -> None:
    """Revalidate immediately before launch; bound the child to five seconds."""
    argv = check_binary(command)
    if command == "FLYWM_RESTART":
        check_session()
    env = _system_env() if command == "FLYWM_RESTART" else childenv.clean_env()
    try:
        result = subprocess.run(
            argv,
            shell=False,
            timeout=5,
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
        )
    except subprocess.TimeoutExpired:
        raise _failure("run", "timeout") from None
    except OSError as exc:
        raise _failure("run", "launch_failed", exc) from None
    if result.returncode:
        raise _failure("run", "exit_nonzero")
    log.info("fly_wmfunc stage=run code=ok errno=0")
