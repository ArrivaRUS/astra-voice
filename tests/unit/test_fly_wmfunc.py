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
    # Keep fake proc descriptors away from pytest's own file inspection.
    process_os = SimpleNamespace(**vars(os))
    monkeypatch.setattr(helper, "os", process_os)
    monkeypatch.setattr(process_os, "open", Mock(return_value=42))
    close = Mock()
    monkeypatch.setattr(process_os, "close", close)
    monkeypatch.setattr(
        process_os,
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
    monkeypatch.setattr(process_os, "readlink", read)
    with pytest.raises(helper.FlyWmError, match="wm_unavailable"):
        helper._check_process(12345)
    close.assert_called_once_with(42)


def test_process_identity_success(monkeypatch: pytest.MonkeyPatch) -> None:
    process_os = SimpleNamespace(**vars(os))
    monkeypatch.setattr(helper, "os", process_os)
    monkeypatch.setattr(process_os, "open", Mock(return_value=42))
    monkeypatch.setattr(process_os, "close", Mock())
    monkeypatch.setattr(process_os, "fstat", Mock(return_value=SimpleNamespace(st_uid=os.getuid())))
    monkeypatch.setattr(process_os, "readlink", Mock(return_value="/usr/bin/fly-wm"))
    helper._check_process(12345)
    process_os.readlink.assert_called_once_with("exe", dir_fd=42)


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
    with pytest.raises(helper.FlyWmError, match="wm_unavailable"):
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
        (-9, "wm_unavailable"),
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


@pytest.fixture
def ewmh_connection(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    atoms = {
        "_FLY_WM_PID": 100,
        "_NET_SUPPORTING_WM_CHECK": 101,
        "_NET_WM_NAME": 102,
        "UTF8_STRING": 103,
        "WM_NAME": 39,
        "_NET_WM_PID": 104,
    }
    root_props = {101: SimpleNamespace(property_type=33, format=32, value=[500])}
    wm_props = {
        101: SimpleNamespace(property_type=33, format=32, value=[500]),
        102: SimpleNamespace(property_type=103, format=8, value=b"fly-wm"),
        104: SimpleNamespace(property_type=6, format=32, value=[12345]),
    }
    root = Mock()
    root.get_full_property.side_effect = lambda atom, kind: root_props.get(atom)
    window = Mock()
    window.get_full_property.side_effect = lambda atom, kind: wm_props.get(atom)
    conn = Mock()
    conn.intern_atom.side_effect = lambda name, only_if_exists=True: atoms.get(name, 0)
    conn.screen.return_value = SimpleNamespace(root=root)
    conn.create_resource_object.return_value = window
    monkeypatch.setattr(helper, "_connect", Mock(return_value=conn))
    return SimpleNamespace(conn=conn, root=root_props, wm=wm_props, window=window)


def test_ewmh_pid_without_legacy(ewmh_connection: SimpleNamespace, process: None) -> None:
    helper._check_session_local()
    helper._check_process.assert_called_once_with(12345)  # type: ignore[attr-defined]
    ewmh_connection.conn.create_resource_object.assert_called_once_with("window", 500)
    ewmh_connection.conn.close.assert_called_once()


def test_ewmh_without_pid(ewmh_connection: SimpleNamespace, process: None) -> None:
    del ewmh_connection.wm[104]
    helper._check_session_local()
    helper._check_process.assert_not_called()  # type: ignore[attr-defined]
    ewmh_connection.conn.close.assert_called_once()


@pytest.mark.parametrize("source", ["wm", "root"])
def test_ewmh_legacy_name(ewmh_connection: SimpleNamespace, process: None, source: str) -> None:
    del ewmh_connection.wm[102]
    getattr(ewmh_connection, source)[39] = SimpleNamespace(
        property_type=31, format=8, value=b"fly-wm"
    )
    helper._check_session_local()
    ewmh_connection.conn.close.assert_called_once()


@pytest.mark.parametrize(
    "target,atom,prop",
    [
        ("root", 101, None),
        ("root", 101, SimpleNamespace(property_type=6, format=32, value=[500])),
        ("root", 101, SimpleNamespace(property_type=33, format=8, value=[500])),
        ("root", 101, SimpleNamespace(property_type=33, format=32, value=[500, 501])),
        ("root", 101, SimpleNamespace(property_type=33, format=32, value=[True])),
        ("root", 101, SimpleNamespace(property_type=33, format=32, value=[0])),
        ("root", 101, SimpleNamespace(property_type=33, format=32, value=[-1])),
        ("root", 101, SimpleNamespace(property_type=33, format=32, value=[2**32])),
        ("wm", 101, None),
        ("wm", 101, SimpleNamespace(property_type=33, format=32, value=[501])),
        ("wm", 101, SimpleNamespace(property_type=6, format=32, value=[500])),
        ("wm", 102, SimpleNamespace(property_type=103, format=8, value=b"KWin")),
        ("wm", 102, SimpleNamespace(property_type=103, format=8, value=b"fly\0-wm")),
        ("wm", 102, SimpleNamespace(property_type=103, format=8, value=b"Fly-wm")),
        ("wm", 102, SimpleNamespace(property_type=103, format=8, value=b"")),
        ("wm", 102, SimpleNamespace(property_type=31, format=8, value=b"fly-wm")),
        ("wm", 102, SimpleNamespace(property_type=103, format=32, value=b"fly-wm")),
        ("wm", 102, SimpleNamespace(property_type=103, format=8, value=[102, 108, 121])),
        ("wm", 104, SimpleNamespace(property_type=31, format=32, value=[12345])),
        ("wm", 104, SimpleNamespace(property_type=6, format=8, value=[12345])),
        ("wm", 104, SimpleNamespace(property_type=6, format=32, value=[])),
        ("wm", 104, SimpleNamespace(property_type=6, format=32, value=[1, 2])),
        ("wm", 104, SimpleNamespace(property_type=6, format=32, value=[True])),
        ("wm", 104, SimpleNamespace(property_type=6, format=32, value=[0])),
        ("wm", 104, SimpleNamespace(property_type=6, format=32, value=[-1])),
        ("wm", 104, SimpleNamespace(property_type=6, format=32, value=[2**32])),
        ("wm", 104, SimpleNamespace(property_type=6, format=32, value=["12345"])),
        ("root", 100, SimpleNamespace(property_type=31, format=32, value=[12345])),
    ],
)
def test_ewmh_rejects_malformed_or_wrong_identity(
    ewmh_connection: SimpleNamespace, process: None, target: str, atom: int, prop: object
) -> None:
    getattr(ewmh_connection, target)[atom] = prop
    # A root name must never override a wrong or malformed check-window name.
    ewmh_connection.root[39] = SimpleNamespace(property_type=31, format=8, value=b"fly-wm")
    with pytest.raises(helper.FlyWmError, match="wm_unavailable"):
        helper._check_session_local()
    helper._check_process.assert_not_called()  # type: ignore[attr-defined]
    ewmh_connection.conn.close.assert_called_once()


@pytest.mark.parametrize("value", [b"KWin", b"", b"fly\0-wm"])
def test_ewmh_root_cannot_override_wm_name(
    ewmh_connection: SimpleNamespace, process: None, value: bytes
) -> None:
    del ewmh_connection.wm[102]
    ewmh_connection.wm[39] = SimpleNamespace(property_type=31, format=8, value=value)
    ewmh_connection.root[39] = SimpleNamespace(property_type=31, format=8, value=b"fly-wm")
    with pytest.raises(helper.FlyWmError, match="wm_unavailable"):
        helper._check_session_local()
    helper._check_process.assert_not_called()  # type: ignore[attr-defined]


def test_ewmh_requires_name(ewmh_connection: SimpleNamespace, process: None) -> None:
    del ewmh_connection.wm[102]
    with pytest.raises(helper.FlyWmError, match="wm_unavailable"):
        helper._check_session_local()


def test_ewmh_foreign_pid(
    ewmh_connection: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    process_os = SimpleNamespace(**vars(os))
    monkeypatch.setattr(helper, "os", process_os)
    process_os.open = Mock(return_value=42)
    process_os.close = Mock()
    process_os.fstat = Mock(return_value=SimpleNamespace(st_uid=os.getuid() + 1))
    process_os.readlink = Mock(side_effect=AssertionError("foreign executable must not be read"))
    with pytest.raises(helper.FlyWmError, match="wm_unavailable"):
        helper._check_session_local()
    process_os.open.assert_called_once()
    process_os.close.assert_called_once_with(42)
    ewmh_connection.conn.close.assert_called_once()


@pytest.mark.parametrize("error_name", ["BadWindow", "BadAtom"])
@pytest.mark.parametrize("legacy", [False, True])
def test_stale_x11_property_is_wm_unavailable(
    ewmh_connection: SimpleNamespace,
    caplog: pytest.LogCaptureFixture,
    error_name: str,
    legacy: bool,
) -> None:
    errors = pytest.importorskip("Xlib.error")
    error = getattr(errors, error_name)(
        SimpleNamespace(get_resource_class=lambda name: None), bytes(32)
    )
    if legacy:
        ewmh_connection.conn.screen().root.get_full_property.side_effect = error
    else:
        ewmh_connection.window.get_full_property.side_effect = error
    with caplog.at_level(logging.DEBUG), pytest.raises(helper.FlyWmError) as exc:
        helper._check_session_local()
    assert exc.value.code == "wm_unavailable"
    assert exc.value.__cause__ is None
    assert "resource_id" not in caplog.text + str(exc.value)
    assert all(record.exc_info is None for record in caplog.records)
    ewmh_connection.conn.close.assert_called_once()


@pytest.mark.parametrize("legacy_type", [31, 6])
def test_ewmh_modern_name_takes_precedence(
    ewmh_connection: SimpleNamespace, process: None, legacy_type: int
) -> None:
    ewmh_connection.wm[39] = SimpleNamespace(property_type=legacy_type, format=8, value=b"KWin")
    helper._check_session_local()
    helper._check_process.assert_called_once_with(12345)  # type: ignore[attr-defined]
    ewmh_connection.conn.close.assert_called_once()


def test_ewmh_wrong_modern_name_cannot_fall_back(
    ewmh_connection: SimpleNamespace, process: None
) -> None:
    ewmh_connection.wm[102].value = b"KWin"
    ewmh_connection.wm[39] = SimpleNamespace(property_type=31, format=8, value=b"fly-wm")
    with pytest.raises(helper.FlyWmError, match="wm_unavailable"):
        helper._check_session_local()
    helper._check_process.assert_not_called()  # type: ignore[attr-defined]


def test_legacy_pid_takes_precedence(ewmh_connection: SimpleNamespace, process: None) -> None:
    ewmh_connection.root[100] = SimpleNamespace(property_type=6, format=32, value=[6789])
    ewmh_connection.wm[102].value = b"KWin"
    helper._check_session_local()
    helper._check_process.assert_called_once_with(6789)  # type: ignore[attr-defined]
    ewmh_connection.conn.create_resource_object.assert_not_called()
    ewmh_connection.conn.screen().root.get_full_property.assert_called_once_with(100, 0)


def test_wrong_type_is_not_requested_as_absent(
    ewmh_connection: SimpleNamespace, process: None
) -> None:
    # XGetProperty(type=CARDINAL) would obscure the wrong-type value; request Any.
    def property_value(atom: int, kind: int) -> SimpleNamespace | None:
        assert kind == 0
        return SimpleNamespace(property_type=31, format=32, value=[12345])

    ewmh_connection.conn.screen().root.get_full_property.side_effect = property_value
    with pytest.raises(helper.FlyWmError, match="wm_unavailable"):
        helper._check_session_local()
    ewmh_connection.conn.create_resource_object.assert_not_called()


@pytest.mark.parametrize("name", [b"fly-wm\0", b"  fly-wm  ", b"  fly-wm  \0\0"])
@pytest.mark.parametrize("modern", [True, False])
def test_ewmh_clean_name(
    ewmh_connection: SimpleNamespace, process: None, name: bytes, modern: bool
) -> None:
    if modern:
        ewmh_connection.wm[102].value = name
    else:
        del ewmh_connection.wm[102]
        ewmh_connection.wm[39] = SimpleNamespace(property_type=31, format=8, value=name)
    helper._check_session_local()
    helper._check_process.assert_called_once_with(12345)  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "modern",
    [
        SimpleNamespace(property_type=31, format=8, value=b"fly-wm"),
        SimpleNamespace(property_type=103, format=32, value=b"fly-wm"),
        SimpleNamespace(property_type=103, format=8, value=[102, 108, 121]),
        SimpleNamespace(property_type=103, format=8, value=b"\xff"),
    ],
)
def test_ewmh_invalid_modern_uses_typed_check_legacy(
    ewmh_connection: SimpleNamespace, process: None, modern: object
) -> None:
    ewmh_connection.wm[102] = modern
    ewmh_connection.wm[39] = SimpleNamespace(property_type=31, format=8, value=b"fly-wm\0")
    helper._check_session_local()
    helper._check_process.assert_called_once_with(12345)  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "failure,exit_code,detail",
    [
        ("missing", 23, "wm_check_missing"),
        ("root-type", 23, "wm_check_missing"),
        ("root-count", 23, "wm_check_missing"),
        ("root-id", 23, "wm_check_missing"),
        ("link-missing", 24, "wm_check_link"),
        ("link-mismatch", 24, "wm_check_link"),
        ("link-type", 24, "wm_check_link"),
        ("check-bad-window", 24, "wm_check_link"),
        ("pid-bad-window", 24, "wm_check_link"),
        ("names-missing", 25, "wm_name_type"),
        ("names-type", 25, "wm_name_type"),
        ("modern-decode", 25, "wm_name_type"),
        ("modern-malformed-root", 25, "wm_name_type"),
        ("legacy-malformed-root", 25, "wm_name_type"),
        ("foreign", 26, "wm_name_mismatch"),
        ("interior-nul", 26, "wm_name_mismatch"),
        ("legacy-pid", 27, "wm_pid_invalid"),
        ("ewmh-pid", 27, "wm_pid_invalid"),
        ("process-dead", 28, "wm_process"),
        ("process-uid", 28, "wm_process"),
        ("process-exe", 28, "wm_process"),
        ("root-bad-window", 24, "wm_check_link"),
        ("bad-atom", 29, "wm_x11_error"),
        ("read-error", 29, "wm_x11_error"),
    ],
)
def test_session_branch_child_protocol(
    ewmh_connection: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
    failure: str,
    exit_code: int,
    detail: str,
) -> None:
    x11 = ewmh_connection
    # Real identity check with fake proc syscalls: never inspect a live process.
    process_os = SimpleNamespace(**vars(os))
    monkeypatch.setattr(helper, "os", process_os)
    process_os.open = Mock(return_value=42)
    process_os.close = Mock()
    process_os.fstat = Mock(return_value=SimpleNamespace(st_uid=os.getuid()))
    process_os.readlink = Mock(return_value="/usr/bin/fly-wm")
    if failure == "missing":
        x11.root.clear()
    elif failure.startswith("root-") and failure != "root-bad-window":
        if failure == "root-type":
            x11.root[101].property_type = 6
        else:
            x11.root[101].value = [500, 501] if failure == "root-count" else [0]
    elif failure == "link-missing":
        del x11.wm[101]
    elif failure == "link-mismatch":
        x11.wm[101].value = [501]
    elif failure == "link-type":
        x11.wm[101].property_type = 6
    elif failure == "names-missing":
        del x11.wm[102]
    elif failure == "names-type":
        x11.wm[102].property_type = 31
        x11.wm[39] = SimpleNamespace(property_type=103, format=8, value=b"fly-wm")
        x11.root[39] = SimpleNamespace(property_type=31, format=8, value=b"fly-wm")
    elif failure == "modern-decode":
        x11.wm[102].value = b"\xffPRIVATE_CANARY"
    elif failure == "modern-malformed-root":
        x11.wm[102].format = 32
        x11.root[39] = SimpleNamespace(property_type=31, format=8, value=b"fly-wm")
    elif failure == "legacy-malformed-root":
        del x11.wm[102]
        x11.wm[39] = SimpleNamespace(property_type=31, format=32, value=b"fly-wm")
        x11.root[39] = SimpleNamespace(property_type=31, format=8, value=b"fly-wm")
    elif failure == "foreign":
        x11.wm[102].value = b"PRIVATE_CANARY"
        x11.wm[39] = SimpleNamespace(property_type=31, format=8, value=b"fly-wm")
    elif failure == "interior-nul":
        x11.wm[102].value = b"fly\0-wm"
    elif failure == "legacy-pid":
        x11.root[100] = SimpleNamespace(property_type=31, format=32, value=[12345])
    elif failure == "ewmh-pid":
        x11.wm[104].value = [True]
    elif failure == "process-dead":
        process_os.readlink.side_effect = FileNotFoundError(errno.ENOENT, "PRIVATE_CANARY")
    elif failure == "process-uid":
        process_os.fstat.return_value.st_uid += 1
    elif failure == "process-exe":
        process_os.readlink.return_value = "/PRIVATE_CANARY/other"
    elif failure == "read-error":
        x11.window.get_full_property.side_effect = RuntimeError("PRIVATE_CANARY")
    else:
        errors = pytest.importorskip("Xlib.error")
        error_cls = errors.BadAtom if failure == "bad-atom" else errors.BadWindow
        error = error_cls(SimpleNamespace(get_resource_class=lambda name: None), bytes(32))
        if failure == "root-bad-window":
            x11.conn.screen().root.get_full_property.side_effect = error
        elif failure == "pid-bad-window":

            def read_property(atom: int, kind: int) -> object:
                if atom == 104:
                    raise error
                return x11.wm.get(atom)

            x11.window.get_full_property.side_effect = read_property
        else:
            x11.window.get_full_property.side_effect = error
    with pytest.raises(helper.FlyWmError) as exc:
        helper._check_session_local()
    assert exc.value.code == str(exc.value) == "wm_unavailable"
    assert exc.value.detail == detail
    assert helper._probe_child("session") == exit_code
    assert capsys.readouterr().out == ""
    assert "PRIVATE_CANARY" not in caplog.text + str(exc.value)
    assert all(record.exc_info is None for record in caplog.records)
    assert x11.conn.close.call_count == 2


@pytest.mark.parametrize(
    "exit_code,detail",
    [
        (21, "unknown"),
        (23, "wm_check_missing"),
        (24, "wm_check_link"),
        (25, "wm_name_type"),
        (26, "wm_name_mismatch"),
        (27, "wm_pid_invalid"),
        (28, "wm_process"),
        (29, "wm_x11_error"),
        (-9, "unknown"),
        (255, "unknown"),
    ],
)
def test_parent_fixed_wm_detail(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    exit_code: int,
    detail: str,
) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        Mock(
            return_value=subprocess.CompletedProcess(
                [], exit_code, stdout=b"PRIVATE_CANARY", stderr=b"PRIVATE_CANARY"
            )
        ),
    )
    with pytest.raises(helper.FlyWmError) as exc:
        helper.check_session()
    assert exc.value.code == str(exc.value) == "wm_unavailable"
    assert exc.value.detail == detail
    assert [r.getMessage() for r in caplog.records] == [
        f"fly_wmfunc stage=probe code=wm_unavailable detail={detail}"
    ]
    assert "PRIVATE_CANARY" not in caplog.text
