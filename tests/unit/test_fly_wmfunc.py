"""ИБ72: offline subprocess/X11 doubles; never restart a real window manager."""

from __future__ import annotations

import errno
import logging
import os
import stat
import subprocess
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest

from astra_voice.platform import fly_wmfunc as helper
from astra_voice.platform import session

pytestmark = pytest.mark.unit
_REAL_PROBE_PATHS = helper._probe_paths
_REAL_RUN = subprocess.run


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(helper, "_connect", Mock(side_effect=AssertionError("real X forbidden")))
    monkeypatch.setattr(subprocess, "run", Mock(side_effect=AssertionError("exec forbidden")))
    monkeypatch.setattr(session, "detect", lambda: session.SessionKind.FLY)
    monkeypatch.setattr(helper, "_probe_paths", lambda: ["/trusted/app", "/trusted/vendor"])


@pytest.fixture
def binary(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        os,
        "lstat",
        Mock(
            return_value=SimpleNamespace(
                st_mode=stat.S_IFREG | 0o755,
                st_uid=0,
            )
        ),
    )
    monkeypatch.setattr(os, "access", Mock(return_value=True))


@pytest.mark.parametrize("command", ["", "FLYWM_RESTART;touch /tmp/pwn", "FLYWM_QUIT"])
def test_unknown_rejected_before_io(monkeypatch: pytest.MonkeyPatch, command: str) -> None:
    probe = Mock(side_effect=AssertionError("binary must not be inspected"))
    monkeypatch.setattr(os, "lstat", probe)
    for call in (helper.check_binary, helper.run):
        with pytest.raises(ValueError):
            call(command)
    probe.assert_not_called()


@pytest.mark.parametrize("command", ["FLYWM_RESTART", "FLYWM_UPDATE_STARTMENU"])
def test_absolute_command(binary: None, command: str) -> None:
    assert helper.check_binary(command) == ["/usr/bin/fly-wmfunc", command]
    os.lstat.assert_called_with("/usr/bin/fly-wmfunc")  # type: ignore[attr-defined]
    os.access.assert_called_with("/usr/bin/fly-wmfunc", os.X_OK)  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "mode,uid",
    [
        (stat.S_IFLNK | 0o777, 0),
        (stat.S_IFDIR | 0o755, 0),
        (stat.S_IFREG | 0o775, 0),
        (stat.S_IFREG | 0o757, 0),
        (stat.S_IFREG | 0o755, 1000),
    ],
)
def test_untrusted_binary(
    binary: None, monkeypatch: pytest.MonkeyPatch, mode: int, uid: int
) -> None:
    monkeypatch.setattr(
        os,
        "lstat",
        Mock(
            return_value=SimpleNamespace(
                st_mode=mode,
                st_uid=uid,
            )
        ),
    )
    with pytest.raises(helper.FlyWmError, match="binary_unsafe"):
        helper.check_binary()


def test_binary_not_executable(binary: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "access", Mock(return_value=False))
    with pytest.raises(helper.FlyWmError, match="binary_unavailable"):
        helper.check_binary()


def test_missing_binary_redacts_exception(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(os, "lstat", Mock(side_effect=OSError(errno.ENOENT, "SECRET")))
    with caplog.at_level(logging.WARNING), pytest.raises(helper.FlyWmError) as exc:
        helper.check_binary()
    assert exc.value.code == "binary_unavailable"
    assert "SECRET" not in caplog.text + str(exc.value)
    assert "stage=binary" in caplog.text and "errno=2" in caplog.text
    assert exc.value.__cause__ is None


@pytest.fixture
def connection(monkeypatch: pytest.MonkeyPatch) -> Mock:
    conn = Mock()
    conn.intern_atom.return_value = 17
    conn.screen().root.get_full_property.return_value = SimpleNamespace(
        value=[12345],
        format=32,
        property_type=6,
    )
    conn.get_default_screen.return_value = 2
    monkeypatch.setattr(helper, "_connect", Mock(return_value=conn))
    return conn


@pytest.fixture
def process(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(helper, "_check_process", Mock())


def test_live_session_closes_connection(connection: Mock, process: None) -> None:
    helper._check_session_local()
    connection.intern_atom.assert_called_once_with("_FLY_WM_PID", only_if_exists=True)
    helper._check_process.assert_called_once_with(12345)  # type: ignore[attr-defined]
    connection.close.assert_called_once()


@pytest.mark.parametrize("kind", [session.SessionKind.KDE, session.SessionKind.OTHER])
def test_wrong_session(monkeypatch: pytest.MonkeyPatch, kind: session.SessionKind) -> None:
    monkeypatch.setattr(session, "detect", lambda: kind)
    with pytest.raises(helper.FlyWmError, match="session_not_fly"):
        helper._check_session_local()


@pytest.mark.parametrize("value", [None, [], [0], [-1], [1, 2], ["1"]])
def test_stale_or_invalid_atom(connection: Mock, value: object) -> None:
    connection.screen().root.get_full_property.return_value = (
        None if value is None else SimpleNamespace(value=value, format=32, property_type=6)
    )
    with pytest.raises(helper.FlyWmError, match="wm_unavailable"):
        helper._check_session_local()
    connection.close.assert_called_once()


def test_no_atom(connection: Mock) -> None:
    connection.intern_atom.return_value = 0
    with pytest.raises(helper.FlyWmError, match="wm_unavailable"):
        helper._check_session_local()
    connection.close.assert_called_once()


@pytest.mark.parametrize("failure", ["dead", "foreign", "wrong-exe"])
def test_process_identity(monkeypatch: pytest.MonkeyPatch, failure: str) -> None:
    monkeypatch.setattr(os, "open", Mock(return_value=42))
    close = Mock()
    monkeypatch.setattr(os, "close", close)
    monkeypatch.setattr(
        os,
        "fstat",
        Mock(
            return_value=SimpleNamespace(
                st_uid=os.getuid() + (failure == "foreign"),
            )
        ),
    )
    read = Mock(return_value="/usr/bin/other" if failure == "wrong-exe" else "/usr/bin/fly-wm")
    if failure == "dead":
        read.side_effect = FileNotFoundError(errno.ENOENT, "private-process-path")
    monkeypatch.setattr(os, "readlink", read)
    with pytest.raises(helper.FlyWmError, match="wm_unavailable"):
        helper._check_process(12345)
    close.assert_called_once_with(42)


def test_process_identity_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "open", Mock(return_value=42))
    monkeypatch.setattr(os, "close", Mock())
    monkeypatch.setattr(os, "fstat", Mock(return_value=SimpleNamespace(st_uid=os.getuid())))
    monkeypatch.setattr(os, "readlink", Mock(return_value="/usr/bin/fly-wm"))
    helper._check_process(12345)
    os.readlink.assert_called_once_with("exe", dir_fd=42)  # type: ignore[attr-defined]


@pytest.mark.parametrize("owner", [0, 987])
def test_tray_owner(connection: Mock, owner: int) -> None:
    connection.get_selection_owner.return_value = SimpleNamespace(id=owner) if owner else 0
    assert helper._tray_owner_local() == owner
    connection.intern_atom.assert_called_once_with("_NET_SYSTEM_TRAY_S2", only_if_exists=True)
    connection.close.assert_called_once()


def test_tray_missing_atom(connection: Mock) -> None:
    connection.intern_atom.return_value = 0
    assert helper._tray_owner_local() == 0
    connection.get_selection_owner.assert_not_called()
    connection.close.assert_called_once()


@pytest.mark.parametrize("command", ["FLYWM_RESTART", "FLYWM_UPDATE_STARTMENU"])
def test_run_contract(binary: None, monkeypatch: pytest.MonkeyPatch, command: str) -> None:
    check = Mock()
    monkeypatch.setattr(helper, "check_session", check)
    run = Mock(return_value=subprocess.CompletedProcess([], 0))
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setenv("PATH", "/attacker")
    for key in ("LD_LIBRARY_PATH", "LD_PRELOAD", "LD_AUDIT"):
        monkeypatch.setenv(key, "/bundle")
        monkeypatch.setenv("ASTRA_VOICE_ORIG_" + key, "/attacker")
    monkeypatch.setenv("APPIMAGE_ATTACK", "unsafe")
    helper.run(command)
    assert run.call_args.args == (["/usr/bin/fly-wmfunc", command],)
    kwargs = run.call_args.kwargs
    assert kwargs["shell"] is False and kwargs["timeout"] == 5
    assert kwargs["stdin"] == kwargs["stdout"] == kwargs["stderr"] == subprocess.DEVNULL
    assert not any(k.startswith("APPIMAGE") for k in kwargs["env"])
    if command == "FLYWM_RESTART":
        check.assert_called_once()
        assert not {"LD_LIBRARY_PATH", "LD_PRELOAD", "LD_AUDIT"} & kwargs["env"].keys()
    else:
        check.assert_not_called()


@pytest.mark.parametrize(
    "failure,code",
    [
        (subprocess.TimeoutExpired("SECRET", 5, output=b"SECRET"), "timeout"),
        (OSError(errno.EACCES, "SECRET"), "launch_failed"),
        (None, "exit_nonzero"),
    ],
)
def test_launch_errors(
    binary: None,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failure: Exception | None,
    code: str,
) -> None:
    monkeypatch.setattr(helper, "check_session", Mock())
    monkeypatch.setattr(
        subprocess,
        "run",
        Mock(
            side_effect=failure,
            return_value=subprocess.CompletedProcess([], 7),
        ),
    )
    with caplog.at_level(logging.WARNING), pytest.raises(helper.FlyWmError) as exc:
        helper.run("FLYWM_RESTART")
    assert exc.value.code == code
    assert "SECRET" not in caplog.text + str(exc.value)
    assert "stage=run" in caplog.text


def test_restart_stops_before_launch_on_stale_wm(
    binary: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        helper, "check_session", Mock(side_effect=helper.FlyWmError("wm_unavailable"))
    )
    with pytest.raises(helper.FlyWmError, match="wm_unavailable"):
        helper.run("FLYWM_RESTART")
    subprocess.run.assert_not_called()  # type: ignore[attr-defined]


def test_restart_rechecks_binary_after_preflight(
    binary: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper.check_binary()
    monkeypatch.setattr(os, "lstat", Mock(side_effect=FileNotFoundError(errno.ENOENT, "gone")))
    with pytest.raises(helper.FlyWmError, match="binary_unavailable"):
        helper.run("FLYWM_RESTART")
    subprocess.run.assert_not_called()  # type: ignore[attr-defined]


@pytest.mark.parametrize("operation", [helper._check_session_local, helper._tray_owner_local])
def test_x11_failure_redacted(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, operation: object
) -> None:
    from collections.abc import Callable
    from typing import cast

    monkeypatch.setattr(helper, "_connect", Mock(side_effect=RuntimeError("SECRET DISPLAY")))
    with pytest.raises(helper.FlyWmError, match="display_unavailable"):
        cast(Callable[[], object], operation)()
    assert "SECRET" not in caplog.text


def test_property_failure_still_closes(connection: Mock) -> None:
    connection.screen().root.get_full_property.side_effect = RuntimeError("secret server")
    with pytest.raises(helper.FlyWmError, match="display_unavailable"):
        helper._check_session_local()
    connection.close.assert_called_once()


def test_invalid_property_type(connection: Mock) -> None:
    connection.screen().root.get_full_property.return_value.property_type = 31
    with pytest.raises(helper.FlyWmError, match="wm_unavailable"):
        helper._check_session_local()
    connection.close.assert_called_once()


@pytest.mark.parametrize("operation", ["session", "tray"])
def test_bounded_probe_timeout(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, operation: str
) -> None:
    monkeypatch.setattr(session, "detect", Mock(side_effect=AssertionError("parent X11 forbidden")))
    probe = Mock(side_effect=subprocess.TimeoutExpired("SECRET", 0.75, output=b"SECRET"))
    monkeypatch.setattr(subprocess, "run", probe)
    with pytest.raises(helper.FlyWmError, match="display_unavailable"):
        if operation == "session":
            helper.check_session()
        else:
            helper.tray_owner()
    assert probe.call_args.kwargs["timeout"] == 0.75
    assert "SECRET" not in caplog.text
    session.detect.assert_not_called()  # type: ignore[attr-defined]


@pytest.mark.parametrize("operation,value", [("session", 0), ("tray", 4294967295)])
def test_bounded_probe_protocol(
    monkeypatch: pytest.MonkeyPatch, operation: str, value: int
) -> None:
    import json
    import sys

    monkeypatch.setattr(session, "detect", Mock(side_effect=AssertionError("parent X11 forbidden")))
    probe = Mock(return_value=subprocess.CompletedProcess([], 0, stdout=f"{value}\n".encode()))
    monkeypatch.setattr(subprocess, "run", probe)
    for key in ("LD_LIBRARY_PATH", "LD_PRELOAD", "LD_AUDIT"):
        monkeypatch.setenv(key, "/bundle")
        monkeypatch.setenv("ASTRA_VOICE_ORIG_" + key, "/attacker")
    monkeypatch.setenv("PYTHONPATH", "/attacker")
    if operation == "session":
        helper.check_session()
    else:
        assert helper.tray_owner(timeout=0.125) == value
    argv = probe.call_args.args[0]
    assert argv[:5] == [sys.executable, "-I", "-S", "-B", "-c"]
    assert argv[-1] == operation
    assert all(os.path.isabs(p) for p in json.loads(argv[-2]))
    kwargs = probe.call_args.kwargs
    assert kwargs["shell"] is False
    assert kwargs["stdin"] == kwargs["stderr"] == subprocess.DEVNULL
    assert kwargs["stdout"] == subprocess.PIPE
    assert kwargs["timeout"] == (0.75 if operation == "session" else 0.125)
    assert not {"LD_LIBRARY_PATH", "LD_PRELOAD", "LD_AUDIT", "PYTHONPATH"} & kwargs["env"].keys()
    session.detect.assert_not_called()  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "output", [b"", b"secret", b"-1\n", b"4294967296\n", b"0\nSECRET", b"1" * 12]
)
def test_bounded_probe_rejects_output(monkeypatch: pytest.MonkeyPatch, output: bytes) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        Mock(
            return_value=subprocess.CompletedProcess(
                [],
                0,
                stdout=output,
            )
        ),
    )
    with pytest.raises(helper.FlyWmError, match="display_unavailable"):
        helper.tray_owner()


@pytest.mark.parametrize("timeout", [0, -1, 1, float("inf"), float("nan")])
def test_bounded_probe_validates_budget(timeout: float) -> None:
    with pytest.raises(ValueError):
        helper.tray_owner(timeout=timeout)
    subprocess.run.assert_not_called()  # type: ignore[attr-defined]


def test_probe_timeout_kills_and_reaps_fake_child(monkeypatch: pytest.MonkeyPatch) -> None:
    process = MagicMock()
    process.__enter__.return_value = process
    process.communicate.side_effect = subprocess.TimeoutExpired("private argv", 0.75)
    monkeypatch.setattr(subprocess, "Popen", Mock(return_value=process))
    monkeypatch.setattr(subprocess, "run", _REAL_RUN)
    with pytest.raises(helper.FlyWmError, match="display_unavailable"):
        helper.check_session()
    process.kill.assert_called_once()
    process.wait.assert_called_once()
    process.__exit__.assert_called_once()


@pytest.mark.parametrize(
    "code,expected",
    [
        (20, "session_not_fly"),
        (21, "wm_unavailable"),
        (22, "display_unavailable"),
        (-9, "display_unavailable"),
    ],
)
def test_probe_error_codes(monkeypatch: pytest.MonkeyPatch, code: int, expected: str) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        Mock(
            return_value=subprocess.CompletedProcess(
                [],
                code,
                stdout=b"private exception",
            )
        ),
    )
    with pytest.raises(helper.FlyWmError, match=expected):
        helper.check_session()


@pytest.mark.parametrize("operation,value", [("session", 0), ("tray", 4294967295)])
def test_probe_child_numeric_only(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], operation: str, value: int
) -> None:
    monkeypatch.setattr(helper, "_check_session_local", Mock())
    monkeypatch.setattr(helper, "_tray_owner_local", Mock(return_value=value))
    assert helper._probe_child(operation) == 0
    assert capsys.readouterr().out == f"{value}\n"


@pytest.mark.parametrize(
    "error,code",
    [
        (helper.FlyWmError("session_not_fly"), 20),
        (helper.FlyWmError("wm_unavailable"), 21),
        (helper.FlyWmError("display_unavailable"), 22),
        (RuntimeError("private exception"), 22),
    ],
)
def test_probe_child_errors_have_no_output(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], error: Exception, code: int
) -> None:
    monkeypatch.setattr(helper, "_check_session_local", Mock(side_effect=error))
    assert helper._probe_child("session") == code
    assert capsys.readouterr().out == ""


def test_probe_paths_do_not_copy_import_path(monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib.util
    import sys
    from pathlib import Path

    monkeypatch.setattr(sys, "path", ["", "/attacker", *sys.path])
    specs = [
        SimpleNamespace(origin="/trusted/vendor/Xlib/__init__.py"),
        SimpleNamespace(origin="/trusted/python/six.py"),
    ]
    monkeypatch.setattr(importlib.util, "find_spec", Mock(side_effect=specs))
    assert _REAL_PROBE_PATHS() == [
        str(Path(helper.__file__).resolve().parents[2]),
        "/trusted/vendor",
        "/trusted/python",
    ]


@pytest.mark.parametrize(
    "spec",
    [None, SimpleNamespace(origin=None), SimpleNamespace(origin="relative/Xlib/__init__.py")],
)
def test_probe_paths_missing_or_relative_fail(
    monkeypatch: pytest.MonkeyPatch, spec: object
) -> None:
    import importlib.util

    monkeypatch.setattr(importlib.util, "find_spec", Mock(return_value=spec))
    with pytest.raises(helper.FlyWmError, match="display_unavailable"):
        _REAL_PROBE_PATHS()
