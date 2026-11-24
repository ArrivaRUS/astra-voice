"""T-448 socket/rights matrix using laptop-shaped UNIX rows and fixture proc."""

from __future__ import annotations

import errno
import os
import selectors
import socket
import stat
import subprocess
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, BinaryIO

import pytest

from astra_voice.core.childenv import clean_env
from astra_voice.meetings import identity, identity_sockets, identity_ss
from astra_voice.meetings.apps import AppClass, load_apps
from astra_voice.meetings.identity import (
    Client,
    Identity,
    IdentityChecker,
    ProcessKey,
    ProcReader,
    Reason,
    Verdict,
)
from astra_voice.meetings.identity_ss import (
    IdentitySocketProbe,
    SocketInsufficiency,
    SocketMeasurement,
    SsStatus,
)

pytestmark = pytest.mark.unit
UID = 1000
SERVER = b"/run/user/1000z0_0_0x0_0x0/pulse/native"
FOREIGN = b"/run/user/102z0_0_0x0_0x0/pulse/native"
SECRET = "PRIVATE_PATH_TEST"
CONTROL = 501
ACCEPTED = 601
DION = "/opt/Dion/dion"


def row(
    inode: int,
    peer: int,
    *,
    name: bytes = b"*",
    peer_name: bytes = b"*",
    state: bytes = b"ESTAB",
) -> bytes:
    return b"u_str %s 0  0   %s  %d   %s  %d  \n" % (state, name, inode, peer_name, peer)


def pair(inode: int, peer: int, path: bytes = SERVER) -> bytes:
    return row(inode, peer) + row(peer, inode, name=path)


CONTROL_ROWS = pair(CONTROL, ACCEPTED)


def stat_row(pid: int, tick: int) -> str:
    return f"{pid} (fixture ) comm) S " + "0 " * 18 + f"{tick} 0\n"


def process(
    proc: ProcReader, pid: int, inodes: tuple[int, ...], *, exe: str = DION, uid: int = UID
) -> Path:
    root = proc.root / str(pid)
    directory = root / "fd"
    directory.mkdir(parents=True)
    (root / "stat").write_text(stat_row(pid, 91))
    (root / "status").write_text(f"Uid:\t{uid}\t{uid}\t{uid}\t{uid}\n")
    (root / "exe").symlink_to(exe)
    for fd, inode in enumerate(inodes):
        (directory / str(fd)).symlink_to(f"socket:[{inode}]")
    (directory / "20000").symlink_to("/unrelated")
    return directory


def claim(pid: int, index: int = 1, exe: str = DION) -> Client:
    return Client(
        index,
        {
            "client.api": "pipewire-pulse",
            "pipewire.sec.pid": 900,
            "application.process.binary": exe.rsplit("/", 1)[-1],
            "application.process.id": pid,
        },
    )


class Child:
    """A readable pipe and an explicit process lifecycle, with no subprocess."""

    def __init__(self, *, status: int = 0, stubborn: bool = False) -> None:
        read_fd, write_fd = os.pipe()
        self.stdout: BinaryIO = os.fdopen(read_fd, "rb", buffering=0)
        os.close(write_fd)
        self.returncode: int | None = None
        self.status = status
        self.stubborn = stubborn
        self.events: list[tuple[str, float | None]] = []

    def wait(self, timeout: float) -> int:
        self.events.append(("wait", timeout))
        if self.stubborn:
            raise subprocess.TimeoutExpired(SECRET, timeout)
        self.returncode = self.status
        return self.returncode

    def kill(self) -> None:
        self.events.append(("kill", None))

    def poll(self) -> int | None:
        self.events.append(("poll", None))
        return self.returncode


class Rig:
    def __init__(self, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.proc = ProcReader(root, uid=UID)
        self.probe = IdentitySocketProbe(self.proc)
        self.patch = monkeypatch
        self.calls: list[tuple[tuple[str, ...], dict[str, Any]]] = []
        self.spawn: Callable[[], Child] = Child
        self.children: list[Child] = []
        self.output = CONTROL_ROWS
        self.on_launch: Callable[[], object] = lambda: None
        self.control_fd = 12345
        monkeypatch.setattr(identity_ss, "_control", self.control)
        self._original_read = os.read
        monkeypatch.setattr(os, "read", self.read)
        self._offset = 0

    def control(self, fd: int) -> tuple[int, bytes]:
        assert fd == self.control_fd
        return CONTROL, SERVER

    def popen(self, argv: tuple[str, ...], **kwargs: Any) -> Child:
        self.calls.append((argv, kwargs))
        self.on_launch()
        child = self.spawn()
        self.children.append(child)
        self._offset = 0
        return child

    def read(self, descriptor: int, size: int) -> bytes:
        if (
            not self.children
            or self.children[-1].stdout.closed
            or descriptor != self.children[-1].stdout.fileno()
        ):
            return self._original_read(descriptor, size)
        chunk = self.output[self._offset : self._offset + size]
        self._offset += len(chunk)
        return chunk

    def measure(self, *pids: int, deadline: float | None = None) -> SocketMeasurement:
        with self.patch.context() as patch:
            patch.setattr(subprocess, "Popen", self.popen)
            return self.probe.measure(
                frozenset(pids),
                self.control_fd,
                time.monotonic() + 3 if deadline is None else deadline,
            )

    def identities(self, sample: SocketMeasurement, *claims: Client) -> tuple[Identity, ...]:
        return IdentityChecker(load_apps(), self.proc, uid=UID, connections=sample).check(claims)


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Rig]:
    test = Rig(tmp_path, monkeypatch)
    try:
        yield test
    finally:
        for child in test.children:
            child.stdout.close()


@pytest.mark.parametrize(
    "exe,n",
    [
        (DION, 1),
        (DION, 2),
        ("/usr/lib/chromium/chromium", 2),
        ("/opt/yandex/browser/yandex_browser", 2),
        ("/usr/lib/firefox/firefox", 1),
    ],
)
def test_catalog_processes_verify_across_same_path_servers(rig: Rig, exe: str, n: int) -> None:
    inodes = (701, 702)[:n]
    process(rig.proc, 41, (*inodes, 701), exe=exe)
    process(rig.proc, 42, (801,), uid=102)
    process(rig.proc, 7993, (ACCEPTED, 901), exe="/usr/bin/pipewire-pulse")
    process(rig.proc, 8905, (902,), exe="/usr/bin/pipewire-pulse")
    # Two accepted sockets with the same MAC path can belong to two
    # pipewire-pulse instances. SS has no process attribution in either row.
    rig.output += pair(701, 901) + pair(702, 902) + pair(801, 903, FOREIGN)
    sample = rig.measure(41, 42)
    assert sample.status is SsStatus.OK and sample.reason is Reason.CONNECTION
    assert sample.control_pair and sample.server_path_matches
    assert sample.evidence(ProcessKey(41, 91)).connections == n
    results = rig.identities(sample, *(claim(41, i, exe=exe) for i in range(n)))
    assert all(
        i.verdict is Verdict.VERIFIED and i.offer_allowed and i.variant1_allowed for i in results
    )
    foreign = rig.identities(sample, claim(42))[0]
    assert foreign.reason is Reason.UID and not foreign.offer_allowed


def test_two_claimants_one_socket_lose_variant1(rig: Rig) -> None:
    process(rig.proc, 41, (701, 701))
    rig.output += pair(701, 901)
    results = rig.identities(rig.measure(41), claim(41, 1), claim(41, 2))
    assert all(i == Identity(Verdict.MISMATCH, AppClass.DION, Reason.COUNTER) for i in results)
    assert all(i.offer_allowed and not i.variant1_allowed for i in results)


def test_proven_zero_ignores_other_server_and_own_control(rig: Rig) -> None:
    process(rig.proc, 41, (701, 702, CONTROL))
    # A foreign-looking path and an inode absent from -x (e.g. TCP) cannot
    # count for SP. An unrelated inaccessible connection does not imply P owns it.
    rig.output += pair(701, 901, FOREIGN) + pair(745, 945)
    sample = rig.measure(41)
    assert sample.status is SsStatus.OK
    assert sample.evidence(ProcessKey(41, 91)).connections == 0
    identity = rig.identities(sample, claim(41))[0]
    assert identity == Identity(Verdict.MISMATCH, AppClass.DION, Reason.NO_CONNECTION)
    assert not identity.offer_allowed and not identity.variant1_allowed


@pytest.mark.parametrize("mode", ["utility", "shared_pulse", "idle"])
def test_electron_tree_shares_ipc_without_losing_socket_evidence(rig: Rig, mode: str) -> None:
    main, zygote, renderer, utility = 41, 42, 43, 44
    inherited, ipc_client, ipc_peer, pulse = 5011, 5012, 5013, 701
    file_descriptors: dict[int, tuple[int, ...]] = {
        main: (inherited, ipc_peer),
        zygote: (inherited, ipc_client),
        renderer: (inherited, ipc_client),
        utility: (inherited, ipc_peer),
    }
    rig.output += pair(inherited, 6011, b"*") + pair(ipc_client, ipc_peer, b"*")
    if mode != "idle":
        file_descriptors[utility] += (pulse,)
        rig.output += pair(pulse, 901)
    if mode == "shared_pulse":
        file_descriptors[renderer] += (pulse,)
    for pid, inodes in file_descriptors.items():
        process(rig.proc, pid, inodes)

    sample = rig.measure(*file_descriptors)
    assert sample.status is SsStatus.OK
    expected: dict[int, int | None] = dict.fromkeys(file_descriptors, 0)
    if mode == "utility":
        expected[utility] = 1
    elif mode == "shared_pulse":
        expected[utility] = expected[renderer] = None

    results = rig.identities(sample, *(claim(pid, index=pid) for pid in file_descriptors))
    for (pid, count), result in zip(expected.items(), results, strict=True):
        key = ProcessKey(pid, 91)
        assert sample.evidence(key).connections == count
        assert sample.insufficient_reason(key) is (
            SocketInsufficiency.SHARED_CONNECTION if count is None else None
        )
        if count is None:
            assert result == Identity(Verdict.DECLARED, AppClass.DION, Reason.SS_INSUFFICIENT)
            assert result.offer_allowed and not result.variant1_allowed
        elif count == 0:
            assert result == Identity(Verdict.MISMATCH, AppClass.DION, Reason.NO_CONNECTION)
            assert not result.offer_allowed and not result.variant1_allowed
        else:
            assert result == Identity(Verdict.VERIFIED, AppClass.DION, Reason.CONNECTION)
            assert result.offer_allowed and result.variant1_allowed


@pytest.mark.parametrize("connections", [0, 1])
def test_missing_peer_with_other_known_path_is_irrelevant(rig: Rig, connections: int) -> None:
    process(rig.proc, 41, (702,) if connections == 0 else (701, 702))
    rig.output += row(702, 902, peer_name=FOREIGN)
    if connections:
        rig.output += pair(701, 901)
    sample = rig.measure(41)
    assert sample.status is SsStatus.OK
    assert sample.evidence(ProcessKey(41, 91)).connections == connections


@pytest.mark.parametrize(
    "source", ["server", "client", "state", "hidden-peer", "unknown-peer", "named-hidden-peer"]
)
def test_partial_pair_prevents_zero_and_partial_count(rig: Rig, source: str) -> None:
    process(rig.proc, 41, (701, 702))
    rig.output += pair(701, 901)
    if source == "server":
        rig.output += row(902, 702, name=SERVER)
    elif source == "client":
        rig.output += row(902, 703, name=SERVER) + row(702, 902)
    elif source == "state":
        rig.output += row(902, 702, name=SERVER) + row(702, 902, state=b"UNCONN")
    elif source == "hidden-peer":
        rig.output += row(702, 902)
    elif source == "named-hidden-peer":
        rig.output += row(702, 902, peer_name=SERVER)
    else:
        rig.output += row(702, 0)
    sample = rig.measure(41)
    assert sample.status is SsStatus.OK
    assert sample.evidence(ProcessKey(41, 91)).connections is None
    assert sample.insufficient_reason(ProcessKey(41, 91)) is SocketInsufficiency.INCOMPLETE_PEER
    identity = rig.identities(sample, claim(41))[0]
    assert identity == Identity(Verdict.DECLARED, AppClass.DION, Reason.SS_INSUFFICIENT)
    assert identity.offer_allowed and not identity.variant1_allowed


@pytest.mark.parametrize("partial", [False, True])
def test_shared_inode_with_another_candidate_is_insufficient(rig: Rig, partial: bool) -> None:
    process(rig.proc, 41, (701,))
    other = process(rig.proc, 42, (701,))
    if partial:
        (other / "3").touch()  # unreadable as a symlink, but socket 701 was observed
    rig.output += pair(701, 901)
    sample = rig.measure(41, 42)
    assert sample.status is SsStatus.OK
    assert all(sample.evidence(ProcessKey(pid, 91)).connections is None for pid in (41, 42))
    assert sample.insufficient_reason(ProcessKey(41, 91)) is SocketInsufficiency.SHARED_CONNECTION
    expected = (
        SocketInsufficiency.FD_UNREADABLE if partial else SocketInsufficiency.SHARED_CONNECTION
    )
    assert sample.insufficient_reason(ProcessKey(42, 91)) is expected
    assert all(not i.variant1_allowed for i in rig.identities(sample, claim(41), claim(42, 2)))


@pytest.mark.parametrize(
    "mode", ["fd_change", "pid_reuse", "fd_denied", "readlink_denied", "limit"]
)
def test_fd_walk_is_bracketed_and_complete(rig: Rig, mode: str) -> None:
    folder = process(rig.proc, 41, (701,))
    rig.output += pair(701, 901)
    with rig.patch.context() as patch:
        if mode == "fd_change":
            rig.on_launch = lambda: (folder / "0").rename(folder / "1")
        elif mode == "pid_reuse":
            rig.on_launch = lambda: (folder.parent / "stat").write_text(stat_row(41, 92))
        elif mode == "fd_denied":
            original_open = os.open

            def open_fd(path: str | Path, flags: int, *args: Any, **kwargs: Any) -> int:
                if path == "fd":
                    raise PermissionError(errno.EACCES, SECRET)
                return original_open(path, flags, *args, **kwargs)

            patch.setattr(os, "open", open_fd)
        elif mode == "readlink_denied":
            original_readlink = os.readlink

            def deny_fd(path: str, *, dir_fd: int | None = None) -> str:
                if path == "0":
                    raise PermissionError(errno.EACCES, SECRET)
                return original_readlink(path, dir_fd=dir_fd)

            patch.setattr(os, "readlink", deny_fd)
        else:
            patch.setattr("astra_voice.meetings.identity.MAX_FDS", 1)
        sample = rig.measure(41)
    assert sample.status is SsStatus.OK
    expected = (
        SocketInsufficiency.FD_CHANGED
        if mode in {"fd_change", "pid_reuse"}
        else SocketInsufficiency.FD_UNREADABLE
    )
    for tick in (91, 92):
        key = ProcessKey(41, tick)
        assert sample.evidence(key).connections is None
        assert sample.insufficient_reason(key) is expected
    identity = rig.identities(sample, claim(41))[0]
    assert identity.offer_allowed and not identity.variant1_allowed
    assert identity.reason is Reason.SS_INSUFFICIENT


def test_foreign_uid_never_opens_fd_directory(rig: Rig) -> None:
    (process(rig.proc, 41, (701,), uid=102) / "0").unlink()
    original_open = os.open

    def track(path: str | Path, flags: int, *args: Any, **kwargs: Any) -> int:
        assert path != "fd"
        return original_open(path, flags, *args, **kwargs)

    with rig.patch.context() as patch:
        patch.setattr(os, "open", track)
        sample = rig.measure(41)
    assert sample.status is SsStatus.OK
    assert sample.evidence(ProcessKey(41, 91)).connections is None


@pytest.mark.parametrize("failed_read", [1, 2])
def test_eacces_on_exe_has_no_offer_even_when_ss_is_absent(rig: Rig, failed_read: int) -> None:
    process(rig.proc, 41, (701,))
    original = os.readlink
    readings = 0

    def deny_exe(path: str, *, dir_fd: int | None = None) -> str:
        nonlocal readings
        if path == "exe":
            readings += 1
            if readings == failed_read:
                raise PermissionError(errno.EACCES, SECRET)
        return original(path, dir_fd=dir_fd)

    sample = SocketMeasurement(SsStatus.ABSENT, Reason.SS_UNAVAILABLE)
    with rig.patch.context() as patch:
        patch.setattr(os, "readlink", deny_exe)
        identity = rig.identities(sample, claim(41))[0]
    assert identity == Identity(Verdict.MISMATCH, AppClass.DION, Reason.UNAVAILABLE)
    assert not identity.offer_allowed and not identity.variant1_allowed


@pytest.mark.parametrize("name", [b"/tmp/unrelated socket", b"@unrelated abstract socket"])
def test_space_in_unrelated_socket_name_preserves_verified(rig: Rig, name: bytes) -> None:
    process(rig.proc, 41, (701,))
    rig.output += pair(701, 901) + row(1234, 4321, name=name)
    sample = rig.measure(41)
    assert sample.status is SsStatus.OK
    assert rig.identities(sample, claim(41))[0] == Identity(
        Verdict.VERIFIED, AppClass.DION, Reason.CONNECTION
    )


def test_ambiguous_row_containing_server_path_fails_entire_sample(rig: Rig) -> None:
    process(rig.proc, 41, (701,))
    process(rig.proc, 42, ())
    rig.output += pair(701, 901) + row(2345, 999, name=b"/tmp/a 999 " + SERVER + b".suffix")
    sample = rig.measure(41, 42)
    assert_failed_rights(rig, sample, Reason.SS_INSUFFICIENT)
    assert sample.evidence(ProcessKey(42, 91)).connections is None


def test_doubtful_numeric_tokens_only_invalidate_matching_candidates(rig: Rig) -> None:
    process(rig.proc, 41, (701,))
    process(rig.proc, 42, (702,))
    process(rig.proc, 43, ())
    rig.output += pair(701, 901) + pair(702, 902)
    # 701 could be a shifted numeric column. It is never parsed as an edge.
    rig.output += row(2345, 6543, name=b"/tmp/socket 000701 text")
    sample = rig.measure(41, 42, 43)
    assert sample.status is SsStatus.OK
    assert sample.evidence(ProcessKey(41, 91)).connections is None
    assert sample.insufficient_reason(ProcessKey(41, 91)) is SocketInsufficiency.INCOMPLETE_PEER
    assert sample.evidence(ProcessKey(42, 91)).connections == 1
    assert sample.evidence(ProcessKey(43, 91)).connections == 0


@pytest.mark.parametrize("inode", [CONTROL, ACCEPTED])
def test_doubtful_inode_at_either_end_of_control_pair_fails_sample(rig: Rig, inode: int) -> None:
    process(rig.proc, 41, (701,))
    rig.output += pair(701, 901) + row(1234, inode, name=b"@unrelated socket")
    assert_failed_rights(rig, rig.measure(41), Reason.SS_INSUFFICIENT)


@pytest.mark.parametrize(
    "bad_rows,pair_visible,path_matches",
    [
        (b"", None, None),
        (row(CONTROL, ACCEPTED), False, None),
        (CONTROL_ROWS.replace(b"*  501", b"*  503"), False, None),
        (pair(CONTROL, ACCEPTED, FOREIGN), True, False),
        (b"cannot parse " + SECRET.encode() + b"\n", None, None),
        (CONTROL_ROWS.rstrip(), None, None),
        (CONTROL_ROWS + b"u_str ESTAB 0 0", None, None),
        (CONTROL_ROWS + row(1234, 4321, name=b"@contains\0control"), None, None),
        (CONTROL_ROWS + row(1234, 4321, name=b"@contains\x7fcontrol"), None, None),
        (CONTROL_ROWS + row(CONTROL, 0), None, None),
        (CONTROL_ROWS + row(902, 0, name=SERVER), True, True),
    ],
    ids=[
        "blind",
        "one-sided",
        "nonreciprocal",
        "other-path",
        "garbage",
        "truncated",
        "truncated-after-complete",
        "nul",
        "del",
        "duplicate",
        "missing-server-peer",
    ],
)
def test_invalid_table_or_control_fails_closed(
    rig: Rig, bad_rows: bytes, pair_visible: bool | None, path_matches: bool | None
) -> None:
    process(rig.proc, 41, (701,))
    rig.output = bad_rows
    sample = rig.measure(41)
    assert sample.status is SsStatus.FAILED and sample.reason is Reason.SS_INSUFFICIENT
    assert sample.control_pair is pair_visible and sample.server_path_matches is path_matches
    assert SECRET not in repr(sample)
    assert_failed_rights(rig, sample, Reason.SS_INSUFFICIENT)


@pytest.mark.parametrize(
    "bad_row",
    [
        pytest.param(b"cannot parse\n", id="garbage-with-lf"),
        pytest.param(b"u_str ESTAB 0 0 /tmp/other 123\n", id="truncated-with-lf"),
        pytest.param(b"u_str ESTAB 0 0 /tmp/other 123", id="truncated-without-lf"),
        pytest.param(b"u_str ESTAB 0 0 /tmp/other name 123\n", id="missing-peer-fields"),
        pytest.param(b"u_str ESTAB 0 0 /tmp/no inode * 456\n", id="missing-local-inode"),
        pytest.param(b"u_str ESTAB 0 0 /tmp/socket name 123 * peer\n", id="bad-peer-inode"),
        pytest.param(b"nonsense ESTAB 0 0 /tmp/socket name 123 * 456\n", id="bad-kind"),
        pytest.param(b"u_str DISCONNECTED 0 0 /tmp/socket name 123 * 456\n", id="bad-state"),
        pytest.param(b"u_str ESTAB 0 queue /tmp/socket name 123 * 456\n", id="bad-queue"),
    ],
)
def test_malformed_row_rejects_otherwise_complete_sample(rig: Rig, bad_row: bytes) -> None:
    process(rig.proc, 41, (701,))
    rig.output += pair(701, 901) + bad_row
    assert_failed_rights(rig, rig.measure(41), Reason.SS_INSUFFICIENT)


def test_control_fd_peer_changes_during_measurement(rig: Rig) -> None:
    process(rig.proc, 41, (701,))
    samples = iter(((CONTROL, SERVER), (CONTROL, FOREIGN)))
    rig.patch.setattr(identity_ss, "_control", lambda fd: next(samples))
    assert_failed_rights(rig, rig.measure(41), Reason.SS_INSUFFICIENT)


def assert_failed_rights(rig: Rig, sample: SocketMeasurement, reason: Reason) -> None:
    assert sample.status is SsStatus.FAILED and sample.reason is reason
    assert sample.evidence(ProcessKey(41, 91)).connections is None
    assert sample.insufficient_reason(ProcessKey(41, 91)) is SocketInsufficiency.OTHER
    identity = rig.identities(sample, claim(41))[0]
    assert identity == Identity(Verdict.DECLARED, AppClass.DION, reason)
    assert identity.offer_allowed and not identity.variant1_allowed


def test_failed_result_cannot_report_absence_rights(rig: Rig) -> None:
    process(rig.proc, 41, (701,))
    sample = SocketMeasurement(SsStatus.FAILED, Reason.SS_UNAVAILABLE)
    assert sample.evidence(ProcessKey(41, 91)).reason is Reason.SS_INSUFFICIENT
    identity = rig.identities(sample, claim(41))[0]
    assert identity.offer_allowed and not identity.variant1_allowed


def test_nonzero_exit_rejects_even_valid_output(rig: Rig) -> None:
    process(rig.proc, 41, (701,))
    rig.output += pair(701, 901)
    rig.spawn = lambda: Child(status=1)
    assert_failed_rights(rig, rig.measure(41), Reason.SS_INSUFFICIENT)


def test_output_limit_kills_and_reaps(rig: Rig) -> None:
    process(rig.proc, 41, (701,))
    rig.output = b"x" * (4_194_304 + 1)
    assert_failed_rights(rig, rig.measure(41), Reason.SS_LIMIT)
    child = rig.children[0]
    assert [event for event, _ in child.events] == ["kill", "wait"]
    assert child.stdout.closed and child.returncode is not None


def test_output_limit_boundary_reaches_parser(rig: Rig) -> None:
    process(rig.proc, 41, ())
    # Exactly 4 MiB is passed on to the parser, where the syntax is invalid.
    rig.output = b"x" * 4_194_304
    assert_failed_rights(rig, rig.measure(41), Reason.SS_INSUFFICIENT)


def test_ss_output_limit_is_four_mib() -> None:
    assert identity_sockets.SS_LIMIT == 4_194_304


def test_proc_fd_limit_is_8192() -> None:
    assert identity.MAX_FDS == 8192


@pytest.mark.parametrize("remaining,launches", [(0.019999, False), (0.02, True), (0.020001, True)])
def test_ss_minimum_start_budget(rig: Rig, remaining: float, launches: bool) -> None:
    process(rig.proc, 41, (701,))
    rig.output += pair(701, 901)
    rig.patch.setattr(time, "monotonic", lambda: 100.0)
    sample = rig.measure(41, deadline=100.0 + remaining)
    assert len(rig.calls) == int(launches)
    if launches:
        assert sample.evidence(ProcessKey(41, 91)).connections == 1
    else:
        assert_failed_rights(rig, sample, Reason.SS_TIMEOUT)
        assert not rig.children


@pytest.mark.parametrize("duration", [0.02, 1.0])
def test_ss_deadline_kill_wait_blocks_second_child_and_shutdown(rig: Rig, duration: float) -> None:
    process(rig.proc, 41, (701,))
    now = [100.0]
    waits: list[float] = []
    rig.patch.setattr(time, "monotonic", lambda: now[0])

    class TimeoutSelector:
        def __enter__(self) -> TimeoutSelector:
            return self

        def __exit__(self, *args: object) -> None:
            pass

        def register(self, descriptor: int, events: int) -> None:
            pass

        def select(self, timeout: float) -> list[object]:
            waits.append(timeout)
            now[0] += timeout
            return []

    rig.patch.setattr(selectors, "DefaultSelector", TimeoutSelector)
    rig.spawn = lambda: Child(stubborn=True)
    assert_failed_rights(rig, rig.measure(41, deadline=now[0] + duration), Reason.SS_TIMEOUT)
    assert waits == [pytest.approx(min(duration, 0.3))]
    child = rig.children[0]
    assert child.events == [("kill", None), ("wait", 0)]
    assert child.stdout.closed
    assert_failed_rights(rig, rig.measure(41, deadline=now[0] + 1), Reason.SS_INSUFFICIENT)
    assert len(rig.calls) == 1 and child.events[-1] == ("poll", None)
    child.stubborn = False
    assert rig.probe.shutdown(now[0] + 0.1)
    assert child.returncode is not None and child.events[-2][0] == "kill"


def test_expired_request_launches_no_child(rig: Rig) -> None:
    process(rig.proc, 41, ())
    assert_failed_rights(rig, rig.measure(41, deadline=time.monotonic() - 1), Reason.SS_TIMEOUT)
    assert not rig.calls


def test_child_launch_hygiene_and_no_reuse_of_measurement(rig: Rig) -> None:
    process(rig.proc, 41, (701,))
    rig.output += pair(701, 901)
    rig.patch.setenv("APPDIR", SECRET)
    rig.patch.setenv("LD_PRELOAD", SECRET)
    rig.patch.setenv("ASTRA_VOICE_ORIG_LD_PRELOAD", SECRET)
    cleaned: list[dict[str, str]] = []

    def clean(base: dict[str, str]) -> dict[str, str]:
        env = clean_env(base)
        cleaned.append(env)
        return env

    rig.patch.setattr(identity_sockets, "clean_env", clean)
    for _ in range(2):
        assert rig.measure(41).evidence(ProcessKey(41, 91)).connections == 1
    assert len(rig.calls) == len(cleaned) == 2
    for argv, kwargs in rig.calls:
        assert argv == ("/usr/bin/ss", "-x", "-H")
        assert kwargs == {
            "shell": False,
            "close_fds": True,
            "env": {"LC_ALL": "C", "PATH": "/usr/bin:/bin"},
            "stdin": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "stdout": subprocess.PIPE,
            "bufsize": 0,
        }
    assert all(c.returncode == 0 and c.stdout.closed for c in rig.children)


@pytest.mark.parametrize(
    "error,entry,untrusted,status",
    [
        (errno.ENOENT, "missing", None, SsStatus.ABSENT),
        *[
            (errno.ENOENT, "missing", (component, defect), SsStatus.FAILED)
            for component in ("/", "usr", "bin")
            for defect in ("owner", "group-writable", "world-writable")
        ],
        (errno.ENOENT, "exists", None, SsStatus.FAILED),
        (errno.ENOENT, "symlink", None, SsStatus.FAILED),
        (errno.ENOENT, "inaccessible", None, SsStatus.FAILED),
        (errno.EACCES, "missing", None, SsStatus.FAILED),
        (errno.ENOEXEC, "missing", None, SsStatus.FAILED),
        (errno.EAGAIN, "missing", None, SsStatus.FAILED),
        (errno.ENOMEM, "missing", None, SsStatus.FAILED),
    ],
)
def test_absent_requires_evidence_of_missing_system_entry(
    rig: Rig, error: int, entry: str, untrusted: tuple[str, str] | None, status: SsStatus
) -> None:
    process(rig.proc, 41, (701,))
    directories = {"/": (10101, None), "usr": (10102, 10101), "bin": (10103, 10102)}
    directory_fds = {fd: component for component, (fd, _) in directories.items()}
    original_stat, original_fstat = os.stat, os.fstat
    original_open, original_close = os.open, os.close
    inspected: list[int] = []
    opened: list[str] = []
    live: set[int] = set()

    def open_dir(path: str | Path, flags: int, *args: Any, **kwargs: Any) -> int:
        component = str(path)
        if component not in directories:
            return original_open(path, flags, *args, **kwargs)
        fd, parent = directories[component]
        assert not args and flags & os.O_NOFOLLOW and flags & os.O_DIRECTORY
        assert kwargs == ({} if parent is None else {"dir_fd": parent})
        assert parent is None or parent in live
        opened.append(component)
        live.add(fd)
        return fd

    def close_dir(fd: int) -> None:
        if fd not in directory_fds:
            original_close(fd)
        else:
            live.remove(fd)

    def fstat_dir(fd: int) -> Any:
        if fd not in directory_fds:
            return original_fstat(fd)
        assert fd in live
        component = directory_fds[fd]
        owner = UID if untrusted == (component, "owner") else 0
        mode = 0o755
        if untrusted == (component, "group-writable"):
            mode |= stat.S_IWGRP
        elif untrusted == (component, "world-writable"):
            mode |= stat.S_IWOTH
        return SimpleNamespace(st_uid=owner, st_mode=mode)

    def stat_ss(path: str, *args: Any, **kwargs: Any) -> Any:
        if path != "ss":
            return original_stat(path, *args, **kwargs)
        assert kwargs == {"dir_fd": 10103, "follow_symlinks": False}
        assert 10103 in live
        inspected.append(1)
        if entry == "missing":
            raise FileNotFoundError(errno.ENOENT, SECRET)
        if entry == "inaccessible":
            raise PermissionError(errno.EACCES, SECRET)
        return SimpleNamespace(st_mode=stat.S_IFLNK if entry == "symlink" else stat.S_IFREG)

    def spawn() -> Child:
        raise OSError(error, SECRET)

    rig.spawn = spawn
    with rig.patch.context() as patch:
        patch.setattr(os, "open", open_dir)
        patch.setattr(os, "close", close_dir)
        patch.setattr(os, "fstat", fstat_dir)
        patch.setattr(os, "stat", stat_ss)
        sample = rig.measure(41)
    assert sample.status is status
    expected_chain = []
    if error == errno.ENOENT:
        for component in directories:
            expected_chain.append(component)
            if untrusted is not None and component == untrusted[0]:
                break
    assert opened == expected_chain and not live
    assert bool(inspected) == (error == errno.ENOENT and untrusted is None)
    if status is SsStatus.ABSENT:
        assert sample.reason is Reason.SS_UNAVAILABLE
        assert sample.evidence(ProcessKey(41, 91)).connections is None
        identity = rig.identities(sample, claim(41))[0]
        assert identity == Identity(Verdict.DECLARED, AppClass.DION, Reason.SS_UNAVAILABLE)
        assert identity.offer_allowed and identity.variant1_allowed
    else:
        assert_failed_rights(rig, sample, Reason.SS_INSUFFICIENT)


@pytest.mark.parametrize(
    "family,kind,path,valid",
    [
        (socket.AF_UNIX, socket.SOCK_STREAM, os.fsdecode(SERVER), True),
        (socket.AF_INET, socket.SOCK_STREAM, ("127.0.0.1", 0), False),
        (socket.AF_UNIX, socket.SOCK_DGRAM, os.fsdecode(SERVER), False),
        (socket.AF_UNIX, socket.SOCK_STREAM, b"\0abstract", False),
        (socket.AF_UNIX, socket.SOCK_STREAM, "", False),
    ],
)
def test_control_reads_peer_of_duplicate_fd(
    monkeypatch: pytest.MonkeyPatch,
    family: int,
    kind: int,
    path: str | bytes | tuple[str, int],
    valid: bool,
) -> None:
    original = 41
    duplicate = 42
    closed: list[int] = []

    class Socket:
        def __init__(self, *, fileno: int) -> None:
            assert fileno == duplicate
            self.family = family
            self.type = kind

        def __enter__(self) -> Socket:
            return self

        def __exit__(self, *args: object) -> None:
            closed.append(duplicate)

        def fileno(self) -> int:
            return duplicate

        def getpeername(self) -> str | bytes | tuple[str, int]:
            return path

    def fstat(descriptor: int) -> Any:
        assert descriptor == duplicate
        return SimpleNamespace(st_mode=stat.S_IFSOCK, st_ino=CONTROL)

    with monkeypatch.context() as patch:
        patch.setattr(os, "dup", lambda fd: duplicate if fd == original else -1)
        patch.setattr(os, "fstat", fstat)
        patch.setattr(socket, "socket", Socket)
        if valid:
            assert identity_ss._control(original) == (CONTROL, SERVER)
        else:
            with pytest.raises(ValueError):
                identity_ss._control(original)
    assert closed == [duplicate]


def test_fd_limit_accepts_exactly_the_limit(rig: Rig) -> None:
    process(rig.proc, 41, (701,))
    rig.patch.setattr("astra_voice.meetings.identity.MAX_FDS", 2)
    rig.output += pair(701, 901)
    assert rig.measure(41).evidence(ProcessKey(41, 91)).connections == 1


def test_eof_without_child_exit_is_still_timeout(rig: Rig) -> None:
    process(rig.proc, 41, (701,))
    rig.spawn = lambda: Child(stubborn=True)
    assert_failed_rights(rig, rig.measure(41), Reason.SS_TIMEOUT)
    child = rig.children[0]
    assert [event for event, _ in child.events] == ["wait", "kill", "wait"]
    assert child.stdout.closed


@pytest.mark.parametrize("verdict", list(Verdict))
@pytest.mark.parametrize("reason", list(Reason))
def test_identity_rights_are_closed(verdict: Verdict, reason: Reason) -> None:
    allowed = {
        (Verdict.VERIFIED, Reason.CONNECTION): (True, True),
        (Verdict.VERIFIED, Reason.NATIVE): (True, True),
        (Verdict.MISMATCH, Reason.COUNTER): (True, False),
        (Verdict.DECLARED, Reason.SS_UNAVAILABLE): (True, True),
        (Verdict.DECLARED, Reason.SS_TIMEOUT): (True, False),
        (Verdict.DECLARED, Reason.SS_LIMIT): (True, False),
        (Verdict.DECLARED, Reason.SS_INSUFFICIENT): (True, False),
        (Verdict.DECLARED, Reason.DECLARED_ONLY): (True, False),
    }
    identity = Identity(verdict, AppClass.DION, reason)
    assert (identity.offer_allowed, identity.variant1_allowed) == allowed.get(
        (verdict, reason), (False, False)
    )
