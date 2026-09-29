"""Защита работающей AppImage-копии от чистки (arch/appimage.md §1, §4; Р5)."""

from __future__ import annotations

import ast
import inspect
import logging
from pathlib import Path
from unittest.mock import Mock

import pytest

from astra_voice import app
from astra_voice.core import paths
from astra_voice.platform import autostart, userinstall
from helpers.appimage_bundle import KEY, make_bundle, module_path, tree_snapshot

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("HOME", str(home))
    for name in ("DATA", "CONFIG", "CACHE", "STATE"):
        monkeypatch.setenv(f"XDG_{name}_HOME", str(home / name.lower()))
    runtime = home / "run"
    runtime.mkdir(mode=0o700)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    monkeypatch.setattr(paths, "FALLBACK_TMP_DIR", tmp_path)
    monkeypatch.delenv(paths.APPIMAGE_DIR_ENV, raising=False)
    monkeypatch.delenv(paths.PORTABLE_ENV, raising=False)
    monkeypatch.delenv(paths.RESOURCES_ENV, raising=False)
    return home


@pytest.fixture
def installed_bundle(monkeypatch: pytest.MonkeyPatch) -> Path:
    bundle = make_bundle(paths.appimage_app_dir() / KEY)
    (bundle / paths.INSTALLED_MARKER).write_bytes(b"")
    monkeypatch.setattr(paths, "_code_file", lambda: module_path(bundle))
    assert paths.install_kind() is paths.InstallKind.APPIMAGE_INSTALLED
    return bundle


def test_installed_copy_writes_own_key(installed_bundle: Path, isolated_home: Path) -> None:
    """Даже после переключения current защищаем каталог работающего кода."""
    (installed_bundle.parent / "current").symlink_to("0.3.0-aaaaaaaaaaaa")

    app._mark_running_copy()

    marker = isolated_home / "run" / "astra-voice" / "running-key"
    assert marker.read_text(encoding="ascii") == KEY + "\n"


@pytest.mark.parametrize(
    "kind",
    [paths.InstallKind.DEB, paths.InstallKind.SOURCE, paths.InstallKind.APPIMAGE_PORTABLE],
)
def test_other_tracks_do_not_write(
    kind: paths.InstallKind,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_home: Path,
) -> None:
    if kind is paths.InstallKind.DEB:
        here = paths.INSTALL_LIB_DIR / "astra_voice" / "core" / "paths.py"
    elif kind is paths.InstallKind.APPIMAGE_PORTABLE:
        here = module_path(make_bundle(tmp_path / "portable"))
    else:
        here = tmp_path / "source" / "astra_voice" / "core" / "paths.py"
    monkeypatch.setattr(paths, "_code_file", lambda: here)
    assert paths.install_kind() is kind
    write = Mock()
    monkeypatch.setattr(userinstall, "write_running_key", write)
    before = tree_snapshot(tmp_path)

    app._mark_running_copy()

    write.assert_not_called()
    assert not (isolated_home / "run" / "astra-voice" / "running-key").exists()
    assert tree_snapshot(tmp_path) == before


@pytest.mark.parametrize("error_type", [OSError, paths.PathError, ValueError])
def test_write_failure_warns_without_home_path(
    error_type: type[Exception],
    installed_bundle: Path,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    marker = isolated_home / "run" / "astra-voice" / "running-key"
    write = Mock(side_effect=error_type(f"не удалось записать {marker}"))
    monkeypatch.setattr(userinstall, "write_running_key", write)

    with caplog.at_level(logging.WARNING, logger=app.__name__):
        app._mark_running_copy()

    write.assert_called_once_with(installed_bundle.name)
    assert len(caplog.records) == 1
    record = caplog.records[0]
    assert record.levelno == logging.WARNING
    assert str(isolated_home) not in record.getMessage()
    assert "~/run/astra-voice/running-key" in record.getMessage()
    assert record.exc_info is None
    assert not marker.exists()


def test_main_marks_only_after_lock_and_logging_before_qapplication() -> None:
    """Проверяем порядок и выход второго экземпляра без запуска main или Qt."""
    main = ast.parse(inspect.getsource(app.main)).body[0]
    assert isinstance(main, ast.FunctionDef)
    lock_guard = next(
        node
        for node in main.body
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.UnaryOp)
        and isinstance(node.test.op, ast.Not)
        and isinstance(node.test.operand, ast.Call)
        and isinstance(node.test.operand.func, ast.Attribute)
        and node.test.operand.func.attr == "tryLock"
    )
    assert len(lock_guard.body) == 1
    second_instance = lock_guard.body[0]
    assert isinstance(second_instance, ast.Return)
    assert isinstance(second_instance.value, ast.Call)
    assert isinstance(second_instance.value.func, ast.Name)
    assert second_instance.value.func.id == "_send_show"
    calls = {
        node.func.id: node
        for node in ast.walk(main)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    mark = calls["_mark_running_copy"]
    assert lock_guard.end_lineno is not None
    assert lock_guard.end_lineno < calls["setup_logging"].lineno < mark.lineno
    assert mark.lineno < calls["QApplication"].lineno
    # Единственный вызов — на верхнем уровне main, сразу после журналирования.
    marks = [
        node
        for node in ast.walk(main)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_mark_running_copy"
    ]
    assert marks == [mark]
    statement = next(
        node for node in main.body if isinstance(node, ast.Expr) and node.value is mark
    )
    previous = main.body[main.body.index(statement) - 1]
    assert isinstance(previous, ast.Expr)
    assert previous.value is calls["setup_logging"]


def test_register_argument_is_exact() -> None:
    assert app._parse_args(["--register"]).register
    assert not app._parse_args([]).register
    with pytest.raises(SystemExit) as error:
        app._parse_args(["--reg"])
    assert error.value.code == 2
    assert "--register" not in userinstall.SERVICE_FLAGS


@pytest.mark.parametrize("kind", [paths.InstallKind.SOURCE, paths.InstallKind.APPIMAGE_INSTALLED])
def test_register_precedes_lock_and_second_instance_show(
    kind: paths.InstallKind, monkeypatch: pytest.MonkeyPatch
) -> None:
    from PyQt5 import QtCore

    events: list[str] = []
    monkeypatch.setattr(paths, "install_kind", lambda: kind)
    register = Mock(side_effect=lambda: events.append("register"))
    monkeypatch.setattr(userinstall, "register", register)

    class Lock:
        def __init__(self, _path: str) -> None:
            events.append("lock")

        def tryLock(self, _timeout: int) -> bool:
            return False

    def show() -> int:
        events.append("show")
        return 0

    monkeypatch.setattr(QtCore, "QLockFile", Lock)
    monkeypatch.setattr(app, "_send_show", show)
    assert app.main(["--register"]) == 0
    assert events == (["register"] if kind is paths.InstallKind.APPIMAGE_INSTALLED else []) + [
        "lock",
        "show",
    ]
    assert register.call_count == (kind is paths.InstallKind.APPIMAGE_INSTALLED)


@pytest.mark.parametrize(
    "error_type",
    [userinstall.UserInstallError, OSError, paths.PathError, autostart.AutostartError],
)
def test_register_failure_warns_and_continues_without_qt(
    error_type: type[Exception],
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(paths, "install_kind", lambda: paths.InstallKind.APPIMAGE_INSTALLED)
    monkeypatch.setattr(
        userinstall, "register", Mock(side_effect=error_type(str(isolated_home / "bad")))
    )
    assert app.main(["--register", "--version"]) == 0
    output = capsys.readouterr()
    assert "Не удалось добавить Astra Voice в меню: ~/bad" in output.err
    assert str(isolated_home) not in output.err
    assert output.out.startswith("astra-voice ")


def test_unregister_is_service_flag_only_for_appimage(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """--unregister снимает регистрацию без GUI; в треке .deb и исходниках — отказ."""
    assert app._parse_args(["--unregister"]).unregister
    assert "--unregister" in userinstall.SERVICE_FLAGS
    unregister = Mock()
    monkeypatch.setattr(userinstall, "unregister", unregister)
    for kind in (paths.InstallKind.DEB, paths.InstallKind.SOURCE):
        monkeypatch.setattr(paths, "install_kind", lambda kind=kind: kind)
        assert app.main(["--unregister"]) == 2
    unregister.assert_not_called()
    for kind in (paths.InstallKind.APPIMAGE_INSTALLED, paths.InstallKind.APPIMAGE_PORTABLE):
        monkeypatch.setattr(paths, "install_kind", lambda kind=kind: kind)
        assert app.main(["--unregister", "--hidden"]) == 0
    assert unregister.call_count == 2
    assert capsys.readouterr().out.count("убран из меню и автозапуска") == 2


@pytest.mark.parametrize(
    "error_type",
    [userinstall.UserInstallError, OSError, paths.PathError, autostart.AutostartError, ValueError],
)
def test_unregister_failure_is_reported(
    error_type: type[Exception],
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(paths, "install_kind", lambda: paths.InstallKind.APPIMAGE_INSTALLED)
    monkeypatch.setattr(
        userinstall, "unregister", Mock(side_effect=error_type(str(isolated_home / "bad")))
    )
    assert app.main(["--unregister"]) == 1
    output = capsys.readouterr()
    assert "Не удалось убрать Astra Voice из меню и автозапуска: ~/bad" in output.err
    assert output.out == ""
