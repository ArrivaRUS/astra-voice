"""KWin lease transactions, crash cuts, and local filesystem trust boundaries.

Every config command is injected; no desktop configuration utility is executed.
"""

from __future__ import annotations

import fcntl
import json
import os
import stat
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest

from astra_voice.platform import kwin_command_lease as module
from astra_voice.platform.kwin_command_lease import (
    JOURNAL_NAME,
    LOCK_NAME,
    KWinCommandLease,
    KWinLeaseError,
    LeaseBusyError,
    MetaValue,
    lease_token,
    object_path,
)

pytestmark = pytest.mark.unit
GENERATION = "a" * 32
TOKEN = lease_token(":1.123", GENERATION)
_REAL_GETEUID = os.geteuid


@pytest.fixture(autouse=True)
def namespace_root_owners(monkeypatch: pytest.MonkeyPatch, nonroot_euid: None) -> None:
    # Lease checks real filesystem ownership; the shared nonroot_euid fixture
    # fakes UID 1000 for application root guards, even when CI runs as root.
    monkeypatch.setattr(os, "geteuid", _REAL_GETEUID)
    # Codex's user namespace maps host root to overflow uid 65534. Normalize
    # only these trusted system ancestor inodes for tests; production remains
    # fail-closed for directories owned by any other user.
    roots = {
        (info.st_dev, info.st_ino)
        for info in (Path("/").stat(), Path("/tmp").stat(), Path("/home").stat())
        if info.st_uid == 65534
    }
    check = module._check_directory

    def normalized(info: os.stat_result, *, private: bool = False) -> None:
        if (info.st_dev, info.st_ino) in roots:
            values = list(info)
            values[4] = 0
            info = os.stat_result(values)
        check(info, private=private)

    monkeypatch.setattr(module, "_check_directory", normalized)


class Crash(BaseException):
    """An abrupt process death must bypass Python exception rollback."""


class ConfigRunner:
    def __init__(self, config: Path, previous: MetaValue) -> None:
        self.config = config
        self.meta = previous
        self.calls: list[list[str]] = []
        self.before: Callable[[str], None] = lambda _event: None
        self.after: Callable[[str], None] = lambda _event: None
        self.persist()

    def persist(self) -> None:
        self.config.write_text(
            json.dumps(
                {
                    "Meta": {"present": self.meta.present, "value": self.meta.value},
                    "unrelated": "preserve me",
                }
            )
        )

    def __call__(self, argv: Sequence[str], timeout: float) -> str:
        assert timeout == 0.2
        args = list(argv)
        self.calls.append(args)
        name = Path(args[0]).name
        self.before(name)
        if name in ("kreadconfig5", "kwriteconfig5"):
            assert args[1:7] == [
                "--file",
                str(self.config),
                "--group",
                "ModifierOnlyShortcuts",
                "--key",
                "Meta",
            ]
        if name == "kreadconfig5":
            assert args[7] == "--default"
            output = (self.meta.value if self.meta.present else args[8]) + "\n"
        elif name == "kwriteconfig5":
            if args[7:] == ["--delete"]:
                self.meta = MetaValue(False)
            else:
                assert args[7] == "--"
                self.meta = MetaValue(True, args[8])
            self.persist()
            output = ""
        else:
            assert args == [
                "/usr/bin/qdbus",
                "org.kde.KWin",
                "/KWin",
                "org.kde.KWin.reconfigure",
            ]
            output = ""
        self.after(name)
        return output


def setup_lease(
    tmp_path: Path, previous: MetaValue | None = None
) -> tuple[
    KWinCommandLease,
    ConfigRunner,
]:
    runner = ConfigRunner(tmp_path / "kwinrc", previous or MetaValue(False))
    lease = KWinCommandLease(
        state_dir=tmp_path / "lease",
        config_path=runner.config,
        runner=runner,
        timeout=0.2,
        lock_timeout=0.03,
    )
    return lease, runner


def peer(lease: KWinCommandLease, runner: ConfigRunner) -> KWinCommandLease:
    return KWinCommandLease(
        state_dir=lease.state_dir,
        config_path=lease.config_path,
        runner=runner,
        timeout=0.2,
        lock_timeout=0.03,
    )


@pytest.mark.parametrize(
    "previous",
    [
        MetaValue(False),
        MetaValue(True, ""),
        MetaValue(True, "org.kde.plasmashell,/x,org.kde.Plasma,y"),
        MetaValue(True, " --custom, whitespace\r\n\n "),
        MetaValue(True, "--delete"),
        MetaValue(True, "кириллица"),
    ],
)
def test_peer_roundtrip_preserves_absence_and_exact_values(
    tmp_path: Path, previous: MetaValue
) -> None:
    lease, runner = setup_lease(tmp_path, previous)
    assert lease.acquire(":1.123", GENERATION) == TOKEN
    assert runner.meta == MetaValue(True, TOKEN)
    assert stat.S_IMODE(lease.state_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE((lease.state_dir / JOURNAL_NAME).stat().st_mode) == 0o600
    assert stat.S_IMODE((lease.state_dir / LOCK_NAME).stat().st_mode) == 0o600
    assert peer(lease, runner).release(TOKEN)
    assert runner.meta == previous
    assert json.loads(runner.config.read_text())["unrelated"] == "preserve me"
    assert not (lease.state_dir / JOURNAL_NAME).exists()
    assert not lease.release(TOKEN)


def test_journal_is_already_fsynced_before_any_kwrite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lease, runner = setup_lease(tmp_path)
    original = os.fsync
    synced: list[tuple[int, int]] = []

    def fsync(fd: int) -> None:
        info = os.fstat(fd)
        synced.append((info.st_dev, info.st_ino))
        original(fd)

    monkeypatch.setattr(os, "fsync", fsync)

    def before(event: str) -> None:
        if event == "kwriteconfig5":
            for path in (lease.state_dir, lease.state_dir / JOURNAL_NAME):
                info = path.stat()
                assert (info.st_dev, info.st_ino) in synced
            assert json.loads((lease.state_dir / JOURNAL_NAME).read_text())["token"] == TOKEN

    runner.before = before
    lease.acquire(":1.123", GENERATION)


@pytest.mark.parametrize(
    "phase",
    [
        "before_journal",
        "after_journal",
        "before_write",
        "after_write",
        "readback",
        "reconfigure",
        "after_reconfigure",
    ],
)
def test_startup_recovers_every_acquisition_crash_cut(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    previous = MetaValue(True, "user action")
    lease, runner = setup_lease(tmp_path, previous)
    save = lease._write_journal

    def journal(directory: int, record: object) -> None:
        if phase == "before_journal":
            raise Crash()
        save(directory, record)  # type: ignore[arg-type]
        if phase == "after_journal":
            raise Crash()

    monkeypatch.setattr(lease, "_write_journal", journal)
    written = False

    def before(event: str) -> None:
        if (
            (phase == "before_write" and event == "kwriteconfig5")
            or (phase == "reconfigure" and event == "qdbus")
            or (phase == "readback" and written and event == "kreadconfig5")
        ):
            raise Crash()

    def after(event: str) -> None:
        nonlocal written
        if event == "kwriteconfig5":
            written = True
        if (phase == "after_write" and event == "kwriteconfig5") or (
            phase == "after_reconfigure" and event == "qdbus"
        ):
            raise Crash()

    runner.before, runner.after = before, after
    with pytest.raises(Crash):
        lease.acquire(":1.123", GENERATION)
    runner.before = runner.after = lambda _: None
    assert peer(lease, runner).recover() is (phase != "before_journal")
    assert runner.meta == previous
    assert not (lease.state_dir / JOURNAL_NAME).exists()


@pytest.mark.parametrize(
    "phase",
    ["before_write", "after_write", "readback", "reconfigure", "after_reconfigure", "after_unlink"],
)
def test_startup_recovers_every_restoration_crash_cut(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    previous = MetaValue(False)
    lease, runner = setup_lease(tmp_path, previous)
    lease.acquire(":1.123", GENERATION)
    written = False

    def before(event: str) -> None:
        if (
            (phase == "before_write" and event == "kwriteconfig5")
            or (phase == "reconfigure" and event == "qdbus")
            or (phase == "readback" and written and event == "kreadconfig5")
        ):
            raise Crash()

    def after(event: str) -> None:
        nonlocal written
        if event == "kwriteconfig5":
            written = True
        if (phase == "after_write" and event == "kwriteconfig5") or (
            phase == "after_reconfigure" and event == "qdbus"
        ):
            raise Crash()

    unlink = os.unlink

    def crash_unlink(path: str, *, dir_fd: int | None = None) -> None:
        unlink(path, dir_fd=dir_fd)
        if path == JOURNAL_NAME and phase == "after_unlink":
            raise Crash()

    monkeypatch.setattr(os, "unlink", crash_unlink)
    runner.before, runner.after = before, after
    with pytest.raises(Crash):
        lease.release(TOKEN)
    runner.before = runner.after = lambda _: None
    monkeypatch.setattr(os, "unlink", unlink)
    assert peer(lease, runner).recover() is (phase != "after_unlink")
    assert runner.meta == previous


@pytest.mark.parametrize(
    "foreign", [MetaValue(False), MetaValue(True, ""), MetaValue(True, "user changed")]
)
def test_foreign_edit_is_never_overwritten(tmp_path: Path, foreign: MetaValue) -> None:
    lease, runner = setup_lease(tmp_path)
    lease.acquire(":1.123", GENERATION)
    runner.meta = foreign
    runner.persist()
    runner.calls.clear()
    assert lease.release(TOKEN)
    assert runner.meta == foreign
    assert not any("kwriteconfig5" in call[0] for call in runner.calls)


def test_old_peer_cannot_release_new_generation(tmp_path: Path) -> None:
    lease, runner = setup_lease(tmp_path)
    lease.acquire(":1.123", GENERATION)
    with pytest.raises(LeaseBusyError):
        peer(lease, runner).acquire(":1.124", "b" * 32)
    assert lease.release(TOKEN)
    new_token = peer(lease, runner).acquire(":1.124", "b" * 32)
    assert not lease.release(TOKEN)
    assert runner.meta == MetaValue(True, new_token)
    assert lease.release(new_token)


@pytest.mark.parametrize("event", ["kwriteconfig5", "kreadconfig5", "qdbus"])
def test_acquire_failure_rolls_back(tmp_path: Path, event: str) -> None:
    lease, runner = setup_lease(tmp_path)
    failed = False
    wrote = False

    def before(name: str) -> None:
        nonlocal failed, wrote
        if name == "kwriteconfig5":
            wrote = True
        if wrote and name == event and not failed:
            failed = True
            raise subprocess.TimeoutExpired(name, 0.2)

    runner.before = before
    with pytest.raises(KWinLeaseError, match="previous Meta restored"):
        lease.acquire(":1.123", GENERATION)
    assert runner.meta == MetaValue(False)
    assert not (lease.state_dir / JOURNAL_NAME).exists()


def test_readback_mismatch_preserves_foreign_value(tmp_path: Path) -> None:
    lease, runner = setup_lease(tmp_path)

    def after(event: str) -> None:
        if event == "kwriteconfig5":
            runner.meta = MetaValue(True, "concurrent editor")
            runner.persist()

    runner.after = after
    with pytest.raises(KWinLeaseError):
        lease.acquire(":1.123", GENERATION)
    assert runner.meta.value == "concurrent editor"
    assert not (lease.state_dir / JOURNAL_NAME).exists()


def test_restore_failure_retains_journal_for_plain_peer_retry(tmp_path: Path) -> None:
    lease, runner = setup_lease(tmp_path, MetaValue(True, ""))
    lease.acquire(":1.123", GENERATION)

    def failure(name: str) -> None:
        if name == "qdbus":
            raise OSError("bus disappeared")

    runner.before = failure
    with pytest.raises(KWinLeaseError):
        lease.release(TOKEN)
    assert runner.meta == MetaValue(True, "")
    assert (lease.state_dir / JOURNAL_NAME).exists()
    runner.before = lambda _: None
    assert peer(lease, runner).release(TOKEN)
    assert not (lease.state_dir / JOURNAL_NAME).exists()


def test_failed_acquire_and_failed_rollback_retains_journal(tmp_path: Path) -> None:
    lease, runner = setup_lease(tmp_path)

    def failure(name: str) -> None:
        if name == "qdbus":
            raise OSError("bus gone")

    runner.before = failure
    with pytest.raises(KWinLeaseError, match="journal retained"):
        lease.acquire(":1.123", GENERATION)
    assert (lease.state_dir / JOURNAL_NAME).exists()
    runner.before = lambda _: None
    assert peer(lease, runner).recover()
    assert runner.meta == MetaValue(False)


def test_flock_is_bounded_and_does_not_touch_configuration(tmp_path: Path) -> None:
    lease, runner = setup_lease(tmp_path)
    with lease._locked():
        with pytest.raises(LeaseBusyError):
            peer(lease, runner).recover()
    assert runner.calls == []


@pytest.mark.parametrize("target", ["state", "lock", "journal", "config", "ancestor"])
def test_symlinks_are_rejected_without_configuration_commands(tmp_path: Path, target: str) -> None:
    lease, runner = setup_lease(tmp_path)
    lease.state_dir.mkdir(mode=0o700)
    if target == "state":
        lease.state_dir.rmdir()
        lease.state_dir.symlink_to(tmp_path, target_is_directory=True)
    elif target == "ancestor":
        link = tmp_path / "linked"
        link.symlink_to(tmp_path, target_is_directory=True)
        lease.state_dir = link / "lease"
    else:
        path = (
            runner.config
            if target == "config"
            else lease.state_dir
            / {
                "lock": LOCK_NAME,
                "journal": JOURNAL_NAME,
            }[target]
        )
        if path.exists():
            path.unlink()
        path.symlink_to(tmp_path / "somewhere")
    with pytest.raises(KWinLeaseError):
        lease.acquire(":1.123", GENERATION)
    assert runner.calls == []


@pytest.mark.parametrize("target,mode", [("state", 0o755), ("lock", 0o644), ("journal", 0o644)])
def test_nonprivate_files_are_rejected(tmp_path: Path, target: str, mode: int) -> None:
    lease, runner = setup_lease(tmp_path)
    lease.state_dir.mkdir(mode=0o700)
    path = lease.state_dir
    if target != "state":
        path /= LOCK_NAME if target == "lock" else JOURNAL_NAME
        path.write_text("{}")
    path.chmod(mode)
    with pytest.raises(KWinLeaseError):
        lease.acquire(":1.123", GENERATION)
    assert runner.calls == []


@pytest.mark.parametrize("bad", ["{}", "[]", "not json", '{"version": true}', "x" * 65537])
def test_corrupt_journal_is_retained_and_never_used(tmp_path: Path, bad: str) -> None:
    lease, runner = setup_lease(tmp_path)
    lease.state_dir.mkdir(mode=0o700)
    path = lease.state_dir / JOURNAL_NAME
    path.write_text(bad)
    path.chmod(0o600)
    with pytest.raises(KWinLeaseError):
        lease.recover()
    assert path.read_text() == bad
    assert runner.calls == []


@pytest.mark.parametrize(
    "service,generation",
    [
        ("org.kde.plasmashell", GENERATION),
        (":2.123", GENERATION),
        (":1.2,extra", GENERATION),
        (":1.2", "../escape"),
        (":1.2", "a" * 31),
        (":1.2", "A" * 32),
    ],
)
def test_token_rejects_nonunique_destinations_and_arbitrary_paths(
    service: str, generation: str
) -> None:
    with pytest.raises(KWinLeaseError):
        lease_token(service, generation)


def test_tuple_has_only_fixed_noop_interface_and_method() -> None:
    assert TOKEN.split(",") == [
        ":1.123",
        object_path(GENERATION),
        "org.astralinux.AstraVoice.CommandMetaLease1",
        "Ignore",
    ]


def test_lock_inode_is_kept_for_other_waiting_peers(tmp_path: Path) -> None:
    lease, runner = setup_lease(tmp_path)
    lease.acquire(":1.123", GENERATION)
    path = lease.state_dir / LOCK_NAME
    inode = path.stat().st_ino
    lease.release(TOKEN)
    assert path.stat().st_ino == inode
    fd = os.open(path, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(fd)


@pytest.mark.parametrize("suffix", ["1", "2"])
def test_existing_value_equal_to_absence_sentinel_is_present(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    suffix: str,
) -> None:
    lease, runner = setup_lease(tmp_path, MetaValue(True, f"astra-voice-absent-{suffix}"))
    from types import SimpleNamespace

    ids = iter([SimpleNamespace(hex="1"), SimpleNamespace(hex="2")])
    monkeypatch.setattr("astra_voice.platform.kwin_command_lease.uuid.uuid4", lambda: next(ids))
    assert lease._read_meta() == runner.meta


def test_changing_value_during_presence_read_aborts_without_writing(tmp_path: Path) -> None:
    lease, runner = setup_lease(tmp_path)

    def change(name: str) -> None:
        if name == "kreadconfig5":
            runner.meta = MetaValue(True, "foreign")

    runner.after = change
    with pytest.raises(KWinLeaseError, match="changed while reading"):
        lease.acquire(":1.123", GENERATION)
    assert not (lease.state_dir / JOURNAL_NAME).exists()
    assert not any("kwriteconfig5" in args[0] for args in runner.calls)


def test_foreign_owned_ancestor_is_rejected(tmp_path: Path) -> None:
    values = list(tmp_path.stat())
    values[4] = os.geteuid() + 1234
    with pytest.raises(KWinLeaseError, match="ownership"):
        module._check_directory(os.stat_result(values))


@pytest.mark.parametrize("change", ["owner", "hardlink", "fifo"])
def test_nonregular_or_untrusted_journal_file_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    path = tmp_path / "file"
    path.write_text("value")
    path.chmod(0o600)
    values = list(path.stat())
    if change == "owner":
        values[4] = os.geteuid() + 1234
    elif change == "hardlink":
        values[3] = 2
    else:
        values[0] = stat.S_IFIFO | 0o600
    info = os.stat_result(values)
    monkeypatch.setattr("astra_voice.platform.kwin_command_lease.os.fstat", lambda _: info)
    with pytest.raises(KWinLeaseError, match="Unsafe configuration or lease file"):
        module._check_file(-1, private=True)


def test_runner_has_bounded_no_shell_call_and_preserves_crlf(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        seen.update(kwargs)
        assert argv == ["/usr/bin/kreadconfig5", "--key", "Meta"]
        return subprocess.CompletedProcess(argv, 0, b"  x\r\n\n")

    monkeypatch.setattr("astra_voice.platform.kwin_command_lease.subprocess.run", run)
    monkeypatch.setattr("astra_voice.platform.kwin_command_lease.childenv.clean_env", lambda: {})
    assert module._run(["/usr/bin/kreadconfig5", "--key", "Meta"], 0.2, lock_fd=123) == "  x\r\n\n"
    assert seen["shell"] is False
    assert seen["timeout"] == 0.2
    assert seen["check"] is True
    assert seen["stdin"] == subprocess.DEVNULL
    assert seen["pass_fds"] == (123,)
    assert "preexec_fn" not in seen


def test_orphan_native_writer_fences_peer_recovery(tmp_path: Path) -> None:
    """Kill the owner while a real kwrite child is stopped before exec.

    Run the fork/subreaper harness in a fresh interpreter, so the test is safe
    even when the main pytest process already has Qt or worker threads. Native
    KConfig tools see only this temporary file; qdbus is always stubbed.
    """
    import sys

    if not all(Path(f"/usr/bin/{name}").is_file() for name in ("kreadconfig5", "kwriteconfig5")):
        pytest.skip("native KConfig utilities unavailable")
    harness = tmp_path / "orphan_writer.py"
    harness.write_text(
        r"""
import ctypes
import os
import select
import signal
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, sys.argv[1])
from astra_voice.platform import kwin_command_lease as lease_module

root = Path(sys.argv[2])
state, config = root / "state", root / "kwinrc"
config.write_text("[ModifierOnlyShortcuts]\nMeta=original-value\n[Other]\nKeep=yes\n")
# Narrow adaptation for Codex's root->65534 namespace mapping, exactly as in
# the module fixture. Do not relax arbitrary ancestor ownership validation.
root_inodes = {(s.st_dev, s.st_ino) for s in
               (Path("/").stat(), Path("/tmp").stat(), Path("/home").stat())
               if s.st_uid == 65534}
check_directory = lease_module._check_directory

def namespace_directory(info, *, private=False):
    if (info.st_dev, info.st_ino) in root_inodes:
        fields = list(info)
        fields[4] = 0
        info = os.stat_result(fields)
    check_directory(info, private=private)

lease_module._check_directory = namespace_directory
# Become the orphan's parent so every test process can be reaped, including
# on assertion failures. This affects only this isolated harness process.
assert ctypes.CDLL(None).prctl(36, 1, 0, 0, 0) == 0
native_run = subprocess.run

def config_only_run(argv, **kwargs):
    if argv[0] == "/usr/bin/qdbus":
        return subprocess.CompletedProcess(argv, 0, b"")
    return native_run(argv, **kwargs)

lease_module.subprocess.run = config_only_run
read_fd, write_fd = os.pipe()
owner = os.fork()
if owner == 0:
    os.close(read_fd)
    def stop_before_write(argv, **kwargs):
        if argv[0] != "/usr/bin/kwriteconfig5":
            return config_only_run(argv, **kwargs)
        # Preserve production pass_fds. The old implementation passed no lock
        # here, which allowed recovery to delete the journal before late write.
        kwargs["pass_fds"] = (*kwargs.get("pass_fds", ()), write_fd)
        code = (
            "import os,signal,sys; "
            "os.write(int(sys.argv[1]), str(os.getpid()).encode()); "
            "os.close(int(sys.argv[1])); "
            "os.kill(os.getpid(),signal.SIGSTOP); "
            "os.execv(sys.argv[2],sys.argv[2:])"
        )
        return native_run([sys.executable, "-I", "-c", code, str(write_fd), *argv], **kwargs)
    lease_module.subprocess.run = stop_before_write
    try:
        lease_module.KWinCommandLease(
            state_dir=state, config_path=config, timeout=5,
        ).acquire(":1.123", "a" * 32)
    finally:
        os._exit(2)

os.close(write_fd)
writer = None

def wait_child(pid, flags=0):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        found, status = os.waitpid(pid, flags | os.WNOHANG)
        if found:
            return status
        time.sleep(0.005)
    raise AssertionError("child did not reach expected state")

try:
    assert select.select([read_fd], [], [], 5)[0], "writer did not start"
    writer = int(os.read(read_fd, 64))
    os.close(read_fd)
    os.kill(owner, signal.SIGKILL)
    wait_child(owner)
    owner = None
    assert os.WIFSTOPPED(wait_child(writer, os.WUNTRACED))
    lease = lease_module.KWinCommandLease(
        state_dir=state, config_path=config, timeout=1, lock_timeout=0.05,
    )
    started = time.monotonic()
    try:
        lease.recover()
    except lease_module.LeaseBusyError:
        pass
    else:
        raise AssertionError("recovery passed a surviving writer")
    assert time.monotonic() - started < 1, "peer recovery was not bounded"
    assert (state / lease_module.JOURNAL_NAME).exists(), "recovery lost its journal"
    assert "Meta=original-value" in config.read_text()
    os.kill(writer, signal.SIGCONT)
    assert os.waitstatus_to_exitcode(wait_child(writer)) == 0
    writer = None
    assert lease_module.lease_token(":1.123", "a" * 32) in config.read_text()
    assert lease.recover()
    restored = config.read_text()
    assert "Meta=original-value" in restored and "Keep=yes" in restored
    assert not (state / lease_module.JOURNAL_NAME).exists()
    assert not lease.recover()
    print("orphan writer fenced; bounded recovery retained journal; restore verified")
finally:
    for pid in (owner, writer):
        if pid is not None:
            try:
                os.kill(pid, signal.SIGKILL)
                wait_child(pid)
            except (ProcessLookupError, ChildProcessError):
                pass
""",
        encoding="utf-8",
    )
    env = os.environ.copy()
    for name in ("DISPLAY", "WAYLAND_DISPLAY", "QT_ACCESSIBILITY", "AT_SPI_BUS_ADDRESS"):
        env.pop(name, None)
    env.update(
        DBUS_SESSION_BUS_ADDRESS="unix:path=/nonexistent",
        QT_QPA_PLATFORM="offscreen",
        XDG_CONFIG_HOME=str(tmp_path),
        XDG_STATE_HOME=str(tmp_path / "xdg-state"),
    )
    result = subprocess.run(
        [sys.executable, "-I", str(harness), str(Path(module.__file__).parents[2]), str(tmp_path)],
        env=env,
        capture_output=True,
        text=True,
        timeout=25,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "orphan writer fenced" in result.stdout
