"""Независимый контракт CLI удаления AppImage (arch/appimage.md §4, R3.4).

Все файлы установки, регистрация и пользовательские данные настоящие, но
создаются только внутри временного HOME/XDG. GUI и AppRun не запускаются.
"""

from __future__ import annotations

import fcntl
import os
import subprocess
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from PyQt5.QtCore import QLockFile

from astra_voice import app, bootstrap
from astra_voice.core import paths
from astra_voice.platform import autostart, userinstall
from conftest import REAL_GETEUID
from helpers.appimage_bundle import KEY, make_bundle, module_path, tree_snapshot

pytestmark = pytest.mark.unit

OLD_KEY = "0.1.0-aaaaaaaaaaaa"


@pytest.fixture(autouse=True)
def profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for name in ("DATA", "CONFIG", "STATE", "CACHE"):
        monkeypatch.setenv(f"XDG_{name}_HOME", str(home / name.lower()))
    runtime = home / "run"
    runtime.mkdir(mode=0o700)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("XDG_CONFIG_DIRS", str(home / "system-config"))
    monkeypatch.setattr(paths, "FALLBACK_TMP_DIR", tmp_path)
    monkeypatch.setattr(paths, "USER_RUNTIME_ROOT", tmp_path / "user-runtime")
    monkeypatch.setattr(paths, "SYSTEM_EXECUTABLE", home / "absent-system-executable")
    for name in (paths.APPIMAGE_DIR_ENV, paths.PORTABLE_ENV, paths.RESOURCES_ENV, "APPIMAGE"):
        monkeypatch.delenv(name, raising=False)
    return home


def put(path: Path, data: bytes = b"keep") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def installation(monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    current = make_bundle(paths.appimage_app_dir() / KEY, icons=True)
    (current / paths.INSTALLED_MARKER).touch()
    old = make_bundle(current.parent / OLD_KEY, version="0.1.0", build_id="aaaaaaaaaaaa")
    (old / paths.INSTALLED_MARKER).touch()
    (current.parent / ".install.lock").touch(mode=0o600)
    (current.parent / "current").symlink_to(KEY)
    (current.parent / "previous").symlink_to(OLD_KEY)
    monkeypatch.setattr(paths, "_code_file", lambda: module_path(current))
    assert paths.install_kind() is paths.InstallKind.APPIMAGE_INSTALLED
    return current, old


def register(profile: Path) -> tuple[Path, Path, tuple[Path, ...]]:
    startup = put(
        profile / "config/autostart/astra-voice.desktop",
        autostart.entry_bytes(autostart.DEB_EXECUTABLE),
    )
    result = userinstall.register()
    assert result.menu == "written" and len(result.icons_written) == 2
    return profile / "data/applications/astra-voice.desktop", startup, result.icons_written


def test_cli_removes_installed_copies_and_registration_but_keeps_user_data(
    profile: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    current, old = installation(monkeypatch)
    menu, startup, icons = register(profile)
    kept_files = [
        put(profile / "data/astra-voice/models/gigaam/model.onnx", b"weights"),
        put(profile / "config/astra-voice/settings.json", b'{"language":"ru"}'),
        put(profile / "state/astra-voice/history.json", b"history"),
        put(profile / "data/applications/other.desktop", b"other menu"),
        put(profile / "data/icons/hicolor/48x48/apps/other.png", b"other icon"),
        put(profile / "data/icons/hicolor/22x22/status/astravoice-tray-idle.svg", b"tray"),
        put(current.parent / "notes.txt", b"user notes"),
    ]
    before = {path: (path.read_bytes(), path.stat().st_mode) for path in kept_files}

    assert app.main(["--uninstall"]) == 0

    assert not current.exists() and not old.exists()
    assert not os.path.lexists(current.parent / "current")
    assert not os.path.lexists(current.parent / "previous")
    assert not menu.exists() and not startup.exists()
    assert all(not os.path.lexists(icon) for icon in icons)
    assert {path: (path.read_bytes(), path.stat().st_mode) for path in kept_files} == before
    output = capsys.readouterr().out.lower()
    assert "модел" in output and "настрой" in output


@pytest.mark.parametrize("foreign_kind", ["file", "symlink", "directory"])
def test_cli_keeps_foreign_menu_and_autostart(
    profile: Path, monkeypatch: pytest.MonkeyPatch, foreign_kind: str
) -> None:
    current, old = installation(monkeypatch)
    for name in ("data/applications", "config/autostart"):
        entry = profile / name / "astra-voice.desktop"
        entry.parent.mkdir(parents=True)
        if foreign_kind == "file":
            entry.write_bytes(b"[Desktop Entry]\nExec=other-program\nHidden=true\n")
        elif foreign_kind == "directory":
            put(entry / "user-note", b"foreign directory")
        else:
            target = put(profile / (name.replace("/", "-") + ".desktop"), b"foreign target")
            entry.symlink_to(target)
    watched = (profile / "data/applications", profile / "config/autostart")
    before = {root: tree_snapshot(root) for root in watched}
    targets = {p: p.read_bytes() for p in profile.glob("*.desktop")}

    assert app.main(["--uninstall"]) == 0

    assert not current.exists() and not old.exists()
    assert {root: tree_snapshot(root) for root in watched} == before
    assert {p: p.read_bytes() for p in targets} == targets


def test_cli_returns_managed_autostart_to_available_deb(
    profile: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    current, old = installation(monkeypatch)
    menu, startup, _ = register(profile)
    startup.write_bytes(startup.read_bytes() + b"Hidden=true\n# user comment\n")
    executable = put(profile / "fake-deb", b"#!/bin/sh\nexit 97\n")
    executable.chmod(0o755)
    monkeypatch.setattr(paths, "SYSTEM_EXECUTABLE", executable)
    before = executable.read_bytes()

    assert app.main(["--uninstall"]) == 0

    assert not current.exists() and not old.exists() and not menu.exists()
    assert b"Exec=/usr/bin/astra-voice --hidden\n" in startup.read_bytes()
    assert b"Hidden=true\n# user comment\n" in startup.read_bytes()
    assert executable.read_bytes() == before


def test_cli_deb_refusal_changes_no_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    installation(monkeypatch)
    put(paths.appimage_app_dir().parent / "models/model.onnx", b"weights")
    monkeypatch.setattr(
        paths, "_code_file", lambda: Path("/usr/lib/astra-voice/astra_voice/core/paths.py")
    )
    assert paths.install_kind() is paths.InstallKind.DEB
    before = tree_snapshot(tmp_path)

    assert app.main(["--uninstall"]) == 2

    assert tree_snapshot(tmp_path) == before


def test_bootstrap_root_refusal_changes_no_files(
    profile: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installation(monkeypatch)
    register(profile)
    before = tree_snapshot(tmp_path)
    monkeypatch.setattr(os, "geteuid", lambda: 0)

    assert bootstrap.main(["app", "--uninstall"]) == 3

    assert tree_snapshot(tmp_path) == before


@pytest.mark.parametrize("damaged", ["current", "copy", "nested", "app"])
def test_cli_never_deletes_through_damaged_installation_links(
    profile: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, damaged: str
) -> None:
    current, _ = installation(monkeypatch)
    outside = tmp_path / "outside"
    put(outside / OLD_KEY / "precious", b"outside installation")
    before = tree_snapshot(outside)
    if damaged == "current":
        (current.parent / "current").unlink()
        (current.parent / "current").symlink_to(outside)
    elif damaged == "copy":
        (current.parent / "0.4.0-bbbbbbbbbbbb").symlink_to(outside)
    elif damaged == "nested":
        (current / "external").symlink_to(outside)
    else:
        app_dir = current.parent
        app_dir.rename(app_dir.with_name("saved-app"))
        app_dir.symlink_to(outside)
        portable = make_bundle(tmp_path / "portable")
        monkeypatch.setattr(paths, "_code_file", lambda: module_path(portable))

    assert app.main(["--uninstall"]) in (0, 1)

    assert tree_snapshot(outside) == before


def test_existing_removal_contract_keeps_running_copy_until_marker_is_removed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current, old = installation(monkeypatch)
    userinstall.write_running_key(old.name)
    before = tree_snapshot(old)

    result = userinstall.remove_program()

    assert result.kept == (old.name,)
    after = tree_snapshot(old)
    assert after.pop(userinstall.REMOVE_ON_EXIT)[1] == b""
    assert after == before and not current.exists()
    (paths.runtime_dir() / "running-key").unlink()
    result = userinstall.remove_program()
    assert result.kept == () and not old.exists()


@pytest.mark.parametrize("foreign_kind", ["bytes", "symlink", "directory"])
def test_cli_preserves_replaced_app_icon(
    profile: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, foreign_kind: str
) -> None:
    current, old = installation(monkeypatch)
    _, _, icons = register(profile)
    replaced = icons[0]
    replaced.unlink()
    if foreign_kind == "bytes":
        replaced.write_bytes(b"custom user icon")
    elif foreign_kind == "directory":
        put(replaced / "custom icon", b"foreign directory")
    else:
        target = put(tmp_path / "outside-icon", b"custom user icon")
        replaced.symlink_to(target)
    before = tree_snapshot(replaced.parent)
    outside = {p: p.read_bytes() for p in tmp_path.glob("outside-*")}

    assert app.main(["--uninstall"]) == 0

    assert not current.exists() and not old.exists()
    assert tree_snapshot(replaced.parent) == before
    assert {p: p.read_bytes() for p in outside} == outside
    assert all(not icon.exists() for icon in icons[1:])


@contextmanager
def running_lock(directory: Path) -> Iterator[None]:
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock = QLockFile(str(directory / "lock"))
    lock.setStaleLockTime(0)
    assert lock.tryLock(0), "isolated runtime must initially be unlocked"
    try:
        yield
    finally:
        lock.unlock()


@pytest.mark.parametrize("active_key", [KEY, OLD_KEY])
def test_cli_defers_live_copy_and_exit_hook_removes_it(
    profile: Path, monkeypatch: pytest.MonkeyPatch, active_key: str
) -> None:
    current, old = installation(monkeypatch)
    menu, startup, _ = register(profile)
    active, inactive = (current, old) if active_key == KEY else (old, current)
    before = tree_snapshot(active)
    userinstall.write_running_key(active_key)
    runtime = paths.runtime_dir()
    with running_lock(runtime):
        assert app.main(["--uninstall"]) == 0
        after = tree_snapshot(active)
        assert after.pop(userinstall.REMOVE_ON_EXIT)[1] == b""
        assert after == before and not inactive.exists()
        assert (runtime / "running-key").read_text().strip() == active_key
        assert not menu.exists() and not startup.exists()
        userinstall.finish_running_copy(active_key)
        assert not active.exists()
        assert not (runtime / "running-key").exists()


@pytest.mark.parametrize("marker_kind", ["missing", "invalid", "symlink"])
def test_cli_refuses_live_lock_without_trusted_key_before_changes(
    profile: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, marker_kind: str
) -> None:
    installation(monkeypatch)
    register(profile)
    runtime = paths.runtime_dir()
    if marker_kind == "invalid":
        put(runtime / "running-key", b"../../outside\n")
    elif marker_kind == "symlink":
        target = put(tmp_path / "outside-key", KEY.encode())
        (runtime / "running-key").symlink_to(target)
    with running_lock(runtime):
        before = tree_snapshot(tmp_path)
        assert app.main(["--uninstall"]) == 1
        assert tree_snapshot(tmp_path) == before


def test_cli_clears_stale_marker_only_after_acquiring_runtime_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current, old = installation(monkeypatch)
    userinstall.write_running_key(KEY)
    runtime = paths.runtime_dir()
    assert not (runtime / "lock").exists()

    assert app.main(["--uninstall"]) == 0

    assert not current.exists() and not old.exists()
    assert not (runtime / "running-key").exists()


def test_reinstall_same_key_cancels_pending_exit_removal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current, _ = installation(monkeypatch)
    userinstall.write_running_key(KEY)
    with running_lock(paths.runtime_dir()):
        assert app.main(["--uninstall"]) == 0
        assert (current / userinstall.REMOVE_ON_EXIT).is_file()
        # Уже проверенная копия переиспользуется: интерпретатор и AppRun не запускаются.
        result = userinstall.install_from_dir(current)
        assert not result.copied
        assert os.readlink(current.parent / "current") == KEY
        assert not (current / userinstall.REMOVE_ON_EXIT).exists()
        before = tree_snapshot(current)
        userinstall.finish_running_copy(KEY)
        assert tree_snapshot(current) == before


def test_exit_keeps_copy_while_another_runtime_has_same_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current, _ = installation(monkeypatch)
    userinstall.write_running_key(KEY)
    first_runtime = paths.runtime_dir()
    other_runtime = tmp_path / "user-runtime" / str(os.getuid()) / "astra-voice"
    other_runtime.mkdir(mode=0o700, parents=True)
    put(other_runtime / "running-key", (KEY + "\n").encode())
    with running_lock(first_runtime), running_lock(other_runtime):
        assert app.main(["--uninstall"]) == 0
        userinstall.finish_running_copy(KEY)
        assert current.is_dir() and (current / userinstall.REMOVE_ON_EXIT).is_file()
        assert not (first_runtime / "running-key").exists()
        assert (other_runtime / "running-key").read_text().strip() == KEY
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(other_runtime.parent))
        userinstall.finish_running_copy(KEY)
        assert not current.exists() and not (other_runtime / "running-key").exists()


@pytest.mark.parametrize("marker_kind", ["symlink", "fifo", "directory"])
def test_exit_never_follows_nonregular_pending_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, marker_kind: str
) -> None:
    current, _ = installation(monkeypatch)
    userinstall.write_running_key(KEY)
    # remove_program откладывает живую копию без взаимодействия с GUI.
    userinstall.remove_program()
    pending = current / userinstall.REMOVE_ON_EXIT
    pending.unlink()
    outside = put(tmp_path / "outside-pending", b"precious")
    if marker_kind == "symlink":
        pending.symlink_to(outside)
    elif marker_kind == "fifo":
        os.mkfifo(pending)
    else:
        pending.mkdir()
    # Snapshot helper читает только обычные файлы: FIFO никогда не открывается.
    before = tree_snapshot(current)

    userinstall.finish_running_copy(KEY)

    assert tree_snapshot(current) == before and outside.read_bytes() == b"precious"


def test_exit_does_not_remove_copy_for_another_running_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current, old = installation(monkeypatch)
    userinstall.write_running_key(old.name)
    put(current / userinstall.REMOVE_ON_EXIT, b"")
    before = tree_snapshot(tmp_path)

    userinstall.finish_running_copy(current.name)

    assert tree_snapshot(tmp_path) == before


@pytest.mark.parametrize("ancestor", ["app-data", "menu", "icons", "autostart"])
def test_cli_does_not_remove_files_through_symlinked_ancestors(
    profile: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ancestor: str
) -> None:
    installation(monkeypatch)
    register(profile)
    location = {
        "app-data": profile / "data/astra-voice",
        "menu": profile / "data/applications",
        "icons": profile / "data/icons",
        "autostart": profile / "config/autostart",
    }[ancestor]
    outside = tmp_path / "outside-tree"
    location.rename(outside)
    location.symlink_to(outside)
    before = tree_snapshot(outside)

    assert app.main(["--uninstall"]) in (0, 1)

    assert tree_snapshot(outside) == before


def test_cli_refuses_to_mutate_registration_while_install_lock_is_held(
    profile: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current, _ = installation(monkeypatch)
    register(profile)
    install_lock = current.parent / ".install.lock"
    with install_lock.open("w") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        monkeypatch.setattr(userinstall, "LOCK_TIMEOUT_S", 0.0)
        before = tree_snapshot(tmp_path)
        assert app.main(["--uninstall"]) == 1
        assert tree_snapshot(tmp_path) == before


@pytest.mark.parametrize("failure_mode", ["permission_error", "real_readonly"])
def test_cli_uses_fallback_when_session_runtime_parent_is_not_writable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_mode: str,
) -> None:
    current, old = installation(monkeypatch)
    if failure_mode == "real_readonly" and REAL_GETEUID() == 0:
        pytest.skip("root может создавать каталоги под 0555; PermissionError проверяется отдельно")
    session_root = tmp_path / "readonly-run-user"
    session_root.mkdir(mode=0o555)
    monkeypatch.setattr(paths, "USER_RUNTIME_ROOT", session_root)
    before = tree_snapshot(session_root)
    denied: list[Path] = []
    unavailable = session_root / str(os.getuid()) / "astra-voice"
    original_mkdir = Path.mkdir

    def runtime_mkdir(
        directory: Path, mode: int = 0o777, parents: bool = False, exist_ok: bool = False
    ) -> None:
        if directory == unavailable:
            denied.append(directory)
            raise PermissionError("temporary session runtime is unavailable")
        original_mkdir(directory, mode=mode, parents=parents, exist_ok=exist_ok)

    if failure_mode == "permission_error":
        monkeypatch.setattr(Path, "mkdir", runtime_mkdir)
    try:
        assert app.main(["--uninstall"]) == 0
        assert not current.exists() and not old.exists()
        assert tree_snapshot(session_root) == before
        assert session_root.stat().st_mode & 0o777 == 0o555
        if failure_mode == "permission_error":
            assert denied == [unavailable]
    finally:
        # Только свой temporary fixture: вернуть права для уборки pytest.
        session_root.chmod(0o700)


def test_exit_preserves_code_if_captured_checker_thread_is_still_alive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current, _ = installation(monkeypatch)
    userinstall.write_running_key(KEY)
    userinstall.remove_program(keep=KEY)
    release = threading.Event()
    thread = threading.Thread(target=release.wait, daemon=True)
    checker = SimpleNamespace(_thread=thread)
    thread.start()
    try:
        processes, threads = app._copy_shutdown_resources(None, None, checker)
        checker._thread = None  # stop/join timeout может отпустить ссылку, сохранив поток.
        app._finish_running_copy(KEY, processes, threads, stopped=True)
        assert thread.is_alive() and current.is_dir()
        assert (current / userinstall.REMOVE_ON_EXIT).is_file()
        assert (current / userinstall.SHUTDOWN_INCOMPLETE).is_file()
        assert userinstall.read_running_key() == KEY
        before = tree_snapshot(tmp_path)
        assert app.main(["--uninstall"]) == 1
        assert tree_snapshot(tmp_path) == before
    finally:
        release.set()
        thread.join(timeout=2)
        assert not thread.is_alive()


@pytest.mark.parametrize("worker_slot", ["current", "candidate", "retired"])
def test_exit_preserves_code_if_captured_worker_process_is_still_alive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    worker_slot: str,
) -> None:
    current, _ = installation(monkeypatch)
    userinstall.write_running_key(KEY)
    userinstall.remove_program(keep=KEY)
    child = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stdin.buffer.read(1)"],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    supervisor = SimpleNamespace(
        process=child if worker_slot != "retired" else None,
        _retired=[child] if worker_slot == "retired" else [],
    )
    runtime = SimpleNamespace(
        supervisor=None if worker_slot == "candidate" else supervisor,
        _switch_candidate=supervisor if worker_slot == "candidate" else None,
    )
    try:
        processes, threads = app._copy_shutdown_resources(runtime, None, None)
        supervisor.process = None  # shutdown не должен потерять проверку живого Popen.
        supervisor._retired = []
        runtime._switch_candidate = None
        app._finish_running_copy(KEY, processes, threads, stopped=True)
        assert child.poll() is None and current.is_dir()
        assert (current / userinstall.REMOVE_ON_EXIT).is_file()
        assert (current / userinstall.SHUTDOWN_INCOMPLETE).is_file()
        assert userinstall.read_running_key() == KEY
        before = tree_snapshot(tmp_path)
        assert app.main(["--uninstall"]) == 1
        assert tree_snapshot(tmp_path) == before
    finally:
        if child.stdin is not None:
            child.stdin.close()
        child.wait(timeout=2)


@contextmanager
def waiting_child(*, separate_session: bool) -> Iterator[subprocess.Popen[bytes]]:
    """Собственный безвредный ребёнок жив до EOF; всегда завершается и reap-ится."""
    child = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stdin.buffer.read(1)"],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=separate_session,
    )
    try:
        assert child.poll() is None
        assert (os.getsid(child.pid) != os.getsid(0)) == separate_session
        yield child
    finally:
        if child.stdin is not None:
            child.stdin.close()
        try:
            child.wait(timeout=2)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=2)


@pytest.mark.parametrize("separate_session", [False, True])
@pytest.mark.parametrize("pending", [False, True])
def test_exit_distinguishes_external_session_from_untracked_own_child(
    monkeypatch: pytest.MonkeyPatch, separate_session: bool, pending: bool
) -> None:
    current, _ = installation(monkeypatch)
    userinstall.write_running_key(KEY)
    if pending:
        userinstall.remove_program(keep=KEY)
    with waiting_child(separate_session=separate_session) as child:
        # Нет capture-списка: ресурс потерян runtime либо создан внешним launcher.
        app._finish_running_copy(KEY, [], [], stopped=True)
        assert child.poll() is None  # Cleanup приложения не останавливает внешние процессы.
        if separate_session:
            assert userinstall.read_running_key() is None
            if pending:
                assert not current.exists()
            else:
                assert current.is_dir()
                assert not (current / userinstall.SHUTDOWN_INCOMPLETE).exists()
        else:
            assert current.is_dir() and userinstall.read_running_key() == KEY
            assert (current / userinstall.SHUTDOWN_INCOMPLETE).is_file()
            assert (current / userinstall.REMOVE_ON_EXIT).is_file() == pending


def test_failed_switch_lost_supervisor_still_protects_pending_copy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current, _ = installation(monkeypatch)
    userinstall.write_running_key(KEY)
    userinstall.remove_program(keep=KEY)
    with waiting_child(separate_session=False) as child:
        old_supervisor = SimpleNamespace(process=child, _retired=[])
        replacement = SimpleNamespace(process=None, _retired=[])
        runtime = SimpleNamespace(supervisor=old_supervisor, _switch_candidate=replacement)
        # Failed stop после публикации replacement теряет старый supervisor.
        runtime.supervisor = replacement
        runtime._switch_candidate = None
        del old_supervisor
        processes, threads = app._copy_shutdown_resources(runtime, None, None)
        assert child not in processes

        app._finish_running_copy(KEY, processes, threads, stopped=True)

        assert child.poll() is None and current.is_dir()
        assert (current / userinstall.REMOVE_ON_EXIT).is_file()
        assert (current / userinstall.SHUTDOWN_INCOMPLETE).is_file()
        assert userinstall.read_running_key() == KEY


@pytest.mark.parametrize(
    "fault",
    [
        "missing-proc",
        "denied-proc",
        "vanished-task",
        "invalid-children",
        "denied-stat",
        "malformed-stat",
        "invalid-state",
        "invalid-ppid",
        "invalid-pgrp",
        "invalid-session",
        "truncated-stat",
        "nonascii-stat",
    ],
)
def test_unknown_proc_state_protects_pending_copy(
    monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    current, _ = installation(monkeypatch)
    userinstall.write_running_key(KEY)
    userinstall.remove_program(keep=KEY)
    task_root = Path("/proc/self/task")
    original_iterdir = Path.iterdir
    original_read_text = Path.read_text
    with waiting_child(separate_session=True) as child:
        stat_path = Path(f"/proc/{child.pid}/stat")
        record = original_read_text(stat_path, encoding="ascii")

        def proc_iterdir(directory: Path) -> Iterator[Path]:
            if directory == task_root:
                if fault == "missing-proc":
                    raise FileNotFoundError("temporary unavailable procfs")
                if fault == "denied-proc":
                    raise PermissionError("temporary denied procfs")
            return original_iterdir(directory)

        def proc_read_text(
            path: Path, encoding: str | None = None, errors: str | None = None
        ) -> str:
            if path.parent.parent == task_root and path.name == "children":
                if fault == "vanished-task":
                    raise FileNotFoundError("task vanished after enumeration")
                if fault == "invalid-children":
                    return "not-a-pid\n"
            if path == stat_path:
                if fault == "denied-stat":
                    raise PermissionError("temporary denied process stat")
                if fault == "malformed-stat":
                    return "not a Linux stat record"
                if fault == "nonascii-stat":
                    raise UnicodeDecodeError("ascii", b"\xff", 0, 1, "invalid proc stat")
                prefix, separator, tail = record.rpartition(") ")
                values = tail.split()
                # Поля Linux proc_pid_stat: state, ppid, pgrp, session.
                corrupt_index = {
                    "invalid-state": 0,
                    "invalid-ppid": 1,
                    "invalid-pgrp": 2,
                    "invalid-session": 3,
                }.get(fault)
                if corrupt_index is not None:
                    values[corrupt_index] = "?"
                    return prefix + separator + " ".join(values)
                if fault == "truncated-stat":
                    return prefix + separator + " ".join(values[:2])
            return original_read_text(path, encoding=encoding, errors=errors)

        monkeypatch.setattr(Path, "iterdir", proc_iterdir)
        monkeypatch.setattr(Path, "read_text", proc_read_text)

        app._finish_running_copy(KEY, [], [], stopped=True)

        assert child.poll() is None and current.is_dir()
        assert (current / userinstall.REMOVE_ON_EXIT).is_file()
        assert (current / userinstall.SHUTDOWN_INCOMPLETE).is_file()
        assert userinstall.read_running_key() == KEY


def test_vanished_child_pid_is_finished_even_if_children_list_is_stale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current, _ = installation(monkeypatch)
    userinstall.write_running_key(KEY)
    userinstall.remove_program(keep=KEY)
    original_read_text = Path.read_text
    with waiting_child(separate_session=False) as child:
        stat_path = Path(f"/proc/{child.pid}/stat")

        def vanished_stat(
            path: Path, encoding: str | None = None, errors: str | None = None
        ) -> str:
            if path == stat_path:
                raise FileNotFoundError("child exited after reading task children")
            return original_read_text(path, encoding=encoding, errors=errors)

        monkeypatch.setattr(Path, "read_text", vanished_stat)
        app._finish_running_copy(KEY, [], [], stopped=True)
        assert not current.exists() and userinstall.read_running_key() is None


def test_real_zombie_child_is_finished_before_reaping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current, _ = installation(monkeypatch)
    userinstall.write_running_key(KEY)
    userinstall.remove_program(keep=KEY)
    with waiting_child(separate_session=False) as child:
        assert child.stdin is not None
        child.stdin.close()
        info = os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOWAIT)
        assert info is not None and info.si_pid == child.pid
        record = Path(f"/proc/{child.pid}/stat").read_text(encoding="ascii")
        assert record.rpartition(") ")[2].split()[0] == "Z"

        app._finish_running_copy(KEY, [], [], stopped=True)

        assert not current.exists() and userinstall.read_running_key() is None


def test_captured_own_child_in_separate_session_still_blocks_removal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current, _ = installation(monkeypatch)
    userinstall.write_running_key(KEY)
    userinstall.remove_program(keep=KEY)
    with waiting_child(separate_session=True) as child:
        app._finish_running_copy(KEY, [child], [], stopped=True)
        assert child.poll() is None and current.is_dir()
        assert (current / userinstall.SHUTDOWN_INCOMPLETE).is_file()
        assert (current / userinstall.REMOVE_ON_EXIT).is_file()
        assert userinstall.read_running_key() == KEY


def test_real_supervisor_launch_keeps_own_child_in_parent_session() -> None:
    from astra_voice.worker.supervisor import WorkerSupervisor

    supervisor = WorkerSupervisor(
        on_event=lambda event: None,
        command_factory=lambda fd: [
            sys.executable,
            "-c",
            "import os,sys; os.read(int(sys.argv[1]), 1)",
            str(fd),
        ],
        use_qt=False,
    )
    try:
        supervisor.start()
        child = supervisor.process
        assert child is not None and child.poll() is None
        assert os.getsid(child.pid) == os.getsid(0)
        assert not app._children_stopped()
    finally:
        # Завершает только своего Python ребёнка и закрывает свою socketpair.
        supervisor.stop()
    assert child.poll() is not None


@pytest.mark.parametrize("launcher", ["sound", "external"])
def test_external_launchers_request_a_separate_session_without_starting_gui(
    launcher: str,
) -> None:
    from astra_voice.platform import external, sound

    calls: list[dict[str, object]] = []

    def capture(command: list[str], **kwargs: object) -> object:
        calls.append(kwargs)
        return object()

    if launcher == "sound":
        assert sound._spawn(["uninstall-test-do-not-execute"], popen=capture)
    else:
        assert external.open_external("file:///nonexistent/uninstall-test", popen=capture)
    assert len(calls) == 1
    assert calls[0]["start_new_session"] is True
    assert calls[0]["shell"] is False
