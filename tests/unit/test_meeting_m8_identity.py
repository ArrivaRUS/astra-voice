"""T-448/449: identity with fixture /proc, no sound, sockets or subprocess ss."""

from __future__ import annotations

import errno
import importlib
import json
import os
import subprocess
import sys
from dataclasses import is_dataclass, replace
from pathlib import Path

import pytest

from astra_voice.meetings import identity as identity_module
from astra_voice.meetings.apps import AppClass, AppDefinition, AppVerification, load_apps
from astra_voice.meetings.identity import (
    MAX_CLIENTS,
    PROC_LIMIT,
    Client,
    Identity,
    IdentityChecker,
    IdentityLimit,
    ProcessFacts,
    ProcessGone,
    ProcessKey,
    ProcessReused,
    ProcReader,
    ProcUnavailable,
    Reason,
    SocketEvidence,
    SocketFds,
    Verdict,
)
from astra_voice.meetings.identity_ss import (
    SocketInsufficiency,
    SocketMeasurement,
    SsStatus,
    _Endpoint,
)

pytestmark = pytest.mark.unit
PID = 123
UID = 1000
KEY = ProcessKey(PID, 555)
EXE = "/fixture-only/dion"
MARKER = "PRIVATE_TITLE_MAC_aa:bb:cc:dd:ee:ff_HOME_USER_LABEL"


def client(index: int = 1, /, **changes: object) -> Client:
    return Client(
        index,
        {
            "client.api": "pipewire-pulse",
            "pipewire.sec.pid": 999,
            "application.process.id": PID,
            "application.process.binary": "dion",
            **changes,
        },
    )


def stat(pid: int = PID, starttime: int = 555) -> str:
    # field 3 is state; field 22 (suffix index 19) is starttime.
    return f"{pid} (name ) with spaces) S " + "0 " * 18 + f"{starttime} 0\n"


@pytest.fixture
def proc(tmp_path: Path) -> ProcReader:
    folder = tmp_path / str(PID)
    folder.mkdir()
    (folder / "status").write_text(f"Name:\t{MARKER}\nUid:\t{UID}\t{UID}\t{UID}\t{UID}\n")
    (folder / "stat").write_text(stat())
    (folder / "exe").symlink_to(EXE)
    return ProcReader(tmp_path, uid=UID)


class Connections:
    def __init__(self, count: int | None = 1) -> None:
        self.count = count
        self.seen: list[ProcessKey] = []

    def evidence(self, process: ProcessKey) -> SocketEvidence:
        self.seen.append(process)
        return SocketEvidence(self.count)


def verified_catalog() -> tuple[AppDefinition, ...]:
    """Synthetic measured path: never promote spike candidates to product facts."""
    return tuple(
        replace(app, exe=(EXE,), verification=AppVerification.EXECUTABLE)
        if app.app_class is AppClass.DION
        else app
        for app in load_apps()
    )


def declared_only_catalog() -> tuple[AppDefinition, ...]:
    return tuple(
        replace(app, exe=(), verification=AppVerification.DECLARED_ONLY) for app in load_apps()
    )


def checker(proc: ProcReader, connections: Connections | None = None) -> IdentityChecker:
    return IdentityChecker(verified_catalog(), proc, uid=UID, connections=connections)


def test_verified_pulse_ignores_proxy_credentials(proc: ProcReader) -> None:
    sockets = Connections()
    result = checker(proc, sockets).check([client()])[0]
    assert result == Identity(Verdict.VERIFIED, AppClass.DION, Reason.CONNECTION)
    assert result.offer_allowed and result.variant1_allowed
    assert sockets.seen == [KEY]  # proxy PID 999 is never opened or queried


def test_proxy_sec_pid_alone_does_not_identify(proc: ProcReader) -> None:
    result = checker(proc, Connections()).check([client(**{"application.process.id": None})])[0]
    assert result.verdict is Verdict.MISMATCH and not result.offer_allowed
    # Missing client.api also never makes proxy sec.pid authoritative.
    result = checker(proc, Connections()).check([client(**{"client.api": None})])[0]
    assert result.verdict is Verdict.VERIFIED


def test_native_uses_exe_from_sec_pid_without_declared_binary(proc: ProcReader) -> None:
    result = checker(proc).check(
        [
            client(
                **{
                    "client.api": "pipewire",
                    "pipewire.sec.pid": PID,
                    "application.process.id": 999,
                    "application.process.binary": "fake",
                }
            )
        ]
    )[0]
    assert result == Identity(Verdict.VERIFIED, AppClass.DION, Reason.NATIVE)


@pytest.mark.parametrize("uids", [(UID + 1,) * 4, (UID, UID + 1, UID, UID)])
def test_foreign_uid_is_negative_without_reading_exe(
    proc: ProcReader, uids: tuple[int, ...]
) -> None:
    (proc.root / str(PID) / "status").write_text("Uid:\t" + "\t".join(map(str, uids)) + "\n")
    (proc.root / str(PID) / "exe").unlink()
    result = checker(proc).check([client()])[0]
    assert result.reason is Reason.UID and result.verdict is Verdict.MISMATCH
    assert not result.offer_allowed and not result.variant1_allowed


@pytest.mark.parametrize(
    "exe,binary,reason",
    [
        ("/tmp/dion", "dion", Reason.EXE),
        (EXE, "chromium", Reason.BINARY),
        (EXE + " (deleted)", "dion", Reason.BINARY),
        ("/usr/bin/pipewire-pulse", "dion", Reason.BINARY),
    ],
)
def test_exe_allowlist_and_basename(
    proc: ProcReader, exe: str, binary: str, reason: Reason
) -> None:
    link = proc.root / str(PID) / "exe"
    link.unlink()
    link.symlink_to(exe)
    result = checker(proc, Connections()).check([client(**{"application.process.binary": binary})])[
        0
    ]
    assert result.verdict is Verdict.MISMATCH and result.reason is reason


def test_process_gone_is_negative(proc: ProcReader) -> None:
    result = checker(proc).check([client(**{"application.process.id": 456})])[0]
    assert result.reason is Reason.DEAD and result.verdict is Verdict.MISMATCH


@pytest.mark.parametrize("field", ["stat", "status", "exe"])
def test_missing_proc_field_is_negative(proc: ProcReader, field: str) -> None:
    (proc.root / str(PID) / field).unlink()
    result = checker(proc, Connections()).check([client()])[0]
    assert result == Identity(Verdict.MISMATCH, AppClass.DION, Reason.DEAD)
    assert not result.variant1_allowed


def test_permission_denied_adapter_is_mismatch(
    proc: ProcReader, monkeypatch: pytest.MonkeyPatch
) -> None:
    def denied(pid: int) -> ProcessFacts:
        raise PermissionError(MARKER)

    monkeypatch.setattr(proc, "snapshot", denied)
    result = checker(proc).check([client()])[0]
    assert result == Identity(Verdict.MISMATCH, AppClass.DION, Reason.UNAVAILABLE)
    assert not result.offer_allowed and not result.variant1_allowed


def test_starttime_change_same_client_and_new_client_generation(proc: ProcReader) -> None:
    identity = checker(proc, Connections())
    assert identity.check([client()])[0].verdict is Verdict.VERIFIED
    (proc.root / str(PID) / "stat").write_text(stat(starttime=556))
    assert identity.check([client()])[0].reason is Reason.REUSED
    identity.forget(1)  # removed context index; never keep a permanent PID exclusion
    assert identity.check([client()])[0].verdict is Verdict.VERIFIED
    identity.reset()
    assert identity.check([client()])[0].verdict is Verdict.VERIFIED


def test_pid_reuse_during_proc_snapshot(proc: ProcReader, monkeypatch: pytest.MonkeyPatch) -> None:
    original = os.readlink

    def reuse(path: str, *, dir_fd: int | None = None) -> str:
        result = original(path, dir_fd=dir_fd)
        if path == "exe":
            (proc.root / str(PID) / "stat").write_text(stat(starttime=777))
        return result

    monkeypatch.setattr(os, "readlink", reuse)
    assert checker(proc).check([client()])[0].reason is Reason.REUSED


def test_pid_reuse_during_socket_check(proc: ProcReader) -> None:
    class Racing(Connections):
        def evidence(self, process: ProcessKey) -> SocketEvidence:
            (proc.root / str(PID) / "stat").write_text(stat(starttime=777))
            return SocketEvidence(1)

    assert checker(proc, Racing()).check([client()])[0].reason is Reason.REUSED


def test_own_context_excluded_before_counting_and_without_proc(proc: ProcReader) -> None:
    identity = checker(proc, Connections())
    results = identity.check([client(1), client(2)], own_client_indexes=frozenset({2}))
    assert results[0].verdict is Verdict.VERIFIED
    assert results[1] == Identity(Verdict.OWN, reason=Reason.OWN)
    (proc.root / str(PID) / "status").unlink()
    assert identity.check([client(2)], own_client_indexes=frozenset({2}))[0].verdict is Verdict.OWN


def test_native_own_starttime_does_not_exclude_reused_pid(proc: ProcReader) -> None:
    native = client(**{"client.api": "pipewire", "pipewire.sec.pid": PID})
    result = checker(proc).check([native], own_native_processes=frozenset({KEY}))[0]
    assert result.verdict is Verdict.OWN
    (proc.root / str(PID) / "stat").write_text(stat(starttime=556))
    result = checker(proc).check([native], own_native_processes=frozenset({KEY}))[0]
    assert result.verdict is Verdict.VERIFIED


def test_counter_mismatch_keeps_offer_disables_variant1_for_all_claimants(proc: ProcReader) -> None:
    results = checker(proc, Connections(1)).check([client(1), client(2)])
    assert all(r.verdict is Verdict.MISMATCH and r.reason is Reason.COUNTER for r in results)
    assert all(r.offer_allowed and not r.variant1_allowed for r in results)


def test_counter_counts_contexts_not_streams(proc: ProcReader) -> None:
    results = checker(proc, Connections()).check([client(1), client(1)])
    assert all(r.verdict is Verdict.VERIFIED for r in results)


def test_bad_claimant_still_counts_against_pid(proc: ProcReader) -> None:
    results = checker(proc, Connections()).check([client(1), client(2, **{"media.name": "x\n"})])
    assert results[0].reason is Reason.COUNTER


def test_no_connection_is_negative_not_counter(proc: ProcReader) -> None:
    result = checker(proc, Connections(0)).check([client()])[0]
    assert result.reason is Reason.NO_CONNECTION and result.verdict is Verdict.MISMATCH
    assert not result.offer_allowed


def test_missing_adapter_has_no_proof_of_ss_absence(proc: ProcReader) -> None:
    result = checker(proc).check([client()])[0]
    assert result == Identity(Verdict.DECLARED, AppClass.DION, Reason.SS_INSUFFICIENT)
    assert result.offer_allowed and not result.variant1_allowed


@pytest.mark.parametrize("count", [-1, MAX_CLIENTS + 1, True])
def test_bad_counter_is_declared(proc: ProcReader, count: int) -> None:
    result = checker(proc, Connections(count)).check([client()])[0]
    assert result.reason is Reason.LIMIT and result.verdict is Verdict.DECLARED


@pytest.mark.parametrize(
    "properties",
    [
        {"media.name": MARKER, "application.name": MARKER},
        {"media.name": MARKER + "\u202e\n"},
        {"application.process.binary": MARKER + "\u200b"},
        {"application.process.binary": "dion\x00"},
        {"media.name": MARKER * 250},
        {"application.process.id": "1" * 10240},
        {"application.name": "\ud800"},
        {"pipewire.sec.label": MARKER, "application.process.user": MARKER},
    ],
)
def test_raw_properties_never_reach_result_or_logs(
    proc: ProcReader,
    properties: dict[str, str],
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    incoming = client(**properties)
    result = checker(proc, Connections()).check([incoming])[0]
    public = json.dumps(
        {"verdict": result.verdict, "class": result.app_class, "reason": result.reason}
    )
    assert MARKER not in public + repr(result) + repr(incoming) + caplog.text
    assert capsys.readouterr() == ("", "")
    assert len(public) < 160
    assert all(ch not in public for ch in ("\u202e", "\u200b", "\x00"))


@pytest.mark.parametrize("field", ["status", "stat"])
def test_proc_read_limit_mismatch(proc: ProcReader, field: str) -> None:
    (proc.root / str(PID) / field).write_bytes(b"x" * (PROC_LIMIT + 1))
    assert checker(proc).check([client()])[0] == Identity(
        Verdict.MISMATCH, AppClass.DION, Reason.UNAVAILABLE
    )


@pytest.mark.parametrize("field", ["status", "stat"])
def test_proc_does_not_follow_field_symlinks(proc: ProcReader, field: str) -> None:
    path = proc.root / str(PID) / field
    target = proc.root / "outside"
    path.rename(target)
    path.symlink_to(target)
    assert checker(proc).check([client()])[0].verdict is Verdict.MISMATCH


def test_proc_does_not_follow_pid_or_root_symlink(proc: ProcReader) -> None:
    folder = proc.root / str(PID)
    target = proc.root / "outside"
    folder.rename(target)
    folder.symlink_to(target, target_is_directory=True)
    assert checker(proc).check([client()])[0].verdict is Verdict.MISMATCH
    rootlink = proc.root / "rootlink"
    rootlink.symlink_to(proc.root, target_is_directory=True)
    assert checker(ProcReader(rootlink)).check([client()])[0].verdict is Verdict.MISMATCH


def test_stat_comm_is_not_split_on_spaces(proc: ProcReader) -> None:
    assert proc.snapshot(PID).key == KEY


def test_client_and_binding_limits(proc: ProcReader) -> None:
    identity = checker(proc)
    assert all(r.reason is Reason.LIMIT for r in identity.check([client()] * (MAX_CLIENTS + 1)))
    identity._bindings = {i: ProcessKey(i + 1, 1) for i in range(MAX_CLIENTS)}
    assert identity.check([client(MAX_CLIENTS + 1)])[0].reason is Reason.LIMIT


def test_stream_fallback_and_conflict(proc: ProcReader) -> None:
    item = Client(1, {"client.api": "pipewire-pulse"}, client().properties)
    assert checker(proc, Connections()).check([item])[0].verdict is Verdict.VERIFIED
    item = replace(client(), stream_properties={"application.process.id": 456})
    assert checker(proc).check([item])[0].reason is Reason.CONFLICT


def test_browser_declared_only_has_no_service_name(proc: ProcReader) -> None:
    link = proc.root / str(PID) / "exe"
    link.unlink()
    link.symlink_to("/test-fixture/firefox")  # fixture, NOT a catalog path
    result = IdentityChecker(
        declared_only_catalog(), proc, uid=UID, connections=Connections()
    ).check([client(**{"application.process.binary": "firefox"})])[0]
    assert result == Identity(Verdict.DECLARED, AppClass.BROWSER, Reason.DECLARED_ONLY)
    assert result.offer_allowed and not result.variant1_allowed
    browser = next(app for app in declared_only_catalog() if app.app_class is AppClass.BROWSER)
    assert browser.display_class == "Браузер" and not browser.exe


def test_old_schema_derives_verification_and_roundtrips() -> None:
    for paths, expected in [
        ([], AppVerification.DECLARED_ONLY),
        ([EXE], AppVerification.EXECUTABLE),
    ]:
        app = AppDefinition.from_dict(
            {
                "id": "dion",
                "title": "DION",
                "binary": ["dion"],
                "exe": paths,
                "class": "dion",
            }
        )
        assert app.verification is expected
        assert AppDefinition.from_dict(app.to_dict()) == app


@pytest.mark.parametrize(
    "changes",
    [
        {"verification": "unknown"},
        {"unknown": MARKER},
        {"verification": "declared-only", "exe": [EXE]},
        {"verification": "executable", "exe": []},
    ],
)
def test_catalog_rejects_unknown_or_inconsistent_extensions(changes: dict[str, object]) -> None:
    from astra_voice.meetings.apps import AppDataError

    with pytest.raises(AppDataError, match="^Список программ встреч не соответствует схеме$"):
        AppDefinition.from_dict(
            {
                "id": "dion",
                "title": "DION",
                "binary": ["dion"],
                "exe": [],
                "class": "dion",
                **changes,
            }
        )


def test_import_identity_cannot_import_sound_capture_or_qt() -> None:
    script = """
import sys
class Guard:
    def find_spec(self, fullname, *args):
        forbidden = ("pulse_stream", "capture", "PyQt5", "sounddevice", "worker")
        if any(token in fullname for token in forbidden):
            raise AssertionError(fullname)
sys.meta_path.insert(0, Guard())
import astra_voice.meetings.identity
import astra_voice.meetings.identity_sockets
import astra_voice.meetings.identity_ss
assert not any("pulse_stream" in name for name in sys.modules)
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        env={**os.environ, "PYTHONPATH": "src"},
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr.decode()


def test_native_browser_without_measured_path_stays_declared(proc: ProcReader) -> None:
    link = proc.root / str(PID) / "exe"
    link.unlink()
    link.symlink_to("/fixture-only/chromium")
    item = client(**{"client.api": "libpipewire", "pipewire.sec.pid": PID})
    result = IdentityChecker(declared_only_catalog(), proc, uid=UID).check([item])[0]
    assert result == Identity(Verdict.DECLARED, AppClass.BROWSER, Reason.DECLARED_ONLY)
    assert result.offer_allowed and not result.variant1_allowed


def test_injected_exception_and_reason_cannot_leak(
    proc: ProcReader, caplog: pytest.LogCaptureFixture
) -> None:
    class Broken:
        def evidence(self, process: ProcessKey) -> SocketEvidence:
            return SocketEvidence(reason=MARKER)  # type: ignore[arg-type]

    result = IdentityChecker(verified_catalog(), proc, uid=UID, connections=Broken()).check(
        [client()]
    )[0]
    assert result == Identity(Verdict.DECLARED, AppClass.DION, Reason.SS_INSUFFICIENT)
    assert MARKER not in caplog.text + repr(result)


def test_legacy_positional_constructor_can_still_roundtrip() -> None:
    app = AppDefinition("dion", "DION", ("dion",), (EXE,), AppClass.DION)
    assert AppDefinition.from_dict(app.to_dict()) == app


@pytest.mark.parametrize(
    "connections,claimants,reason",
    [(0, 1, Reason.NO_CONNECTION), (1, 2, Reason.COUNTER)],
)
def test_review_declared_only_preserves_negative_sockets(
    proc: ProcReader,
    connections: int,
    claimants: int,
    reason: Reason,
) -> None:
    sockets = Connections(connections)
    identity = IdentityChecker(declared_only_catalog(), proc, uid=UID, connections=sockets)
    results = identity.check([client(index) for index in range(claimants)])
    assert sockets.seen == [KEY] * claimants
    assert all(result == Identity(Verdict.MISMATCH, AppClass.DION, reason) for result in results)
    assert all(not result.variant1_allowed for result in results)
    assert all(result.offer_allowed == (reason is Reason.COUNTER) for result in results)


def test_review_process_exits_inside_pinned_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A private, non-audio child waits for stdin until we end it after stat.
    with subprocess.Popen(
        [sys.executable, "-c", "import os; os.read(0, 1)"],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ) as child:
        original = ProcReader._read
        stopped = False

        def read_and_exit(directory: int, name: str) -> bytes:
            nonlocal stopped
            data = original(directory, name)
            if name == "stat" and not stopped:
                stopped = True
                child.terminate()
                child.wait(timeout=10)
            return data

        monkeypatch.setattr(ProcReader, "_read", staticmethod(read_and_exit))
        try:
            identity = IdentityChecker(load_apps(), ProcReader(), uid=os.getuid())
            result = identity.check([client(**{"application.process.id": child.pid})])[0]
            assert stopped and child.poll() is not None
            assert result == Identity(Verdict.MISMATCH, AppClass.DION, Reason.DEAD)
            assert not result.variant1_allowed and not result.offer_allowed
        finally:
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=10)


@pytest.mark.parametrize("error", [errno.ENOENT, errno.ESRCH])
def test_review_pinned_proc_disappearance_errno(
    proc: ProcReader,
    monkeypatch: pytest.MonkeyPatch,
    error: int,
) -> None:
    original = ProcReader._read

    def gone(directory: int, name: str) -> bytes:
        if name == "status":
            raise OSError(error, MARKER)
        return original(directory, name)

    monkeypatch.setattr(ProcReader, "_read", staticmethod(gone))
    result = checker(proc).check([client()])[0]
    assert result == Identity(Verdict.MISMATCH, AppClass.DION, Reason.DEAD)
    assert not result.variant1_allowed


@pytest.mark.parametrize("error", [errno.EACCES, errno.EPERM])
def test_review_pinned_proc_permission_is_not_disappearance(
    proc: ProcReader,
    monkeypatch: pytest.MonkeyPatch,
    error: int,
) -> None:
    original = ProcReader._read

    def denied(directory: int, name: str) -> bytes:
        if name == "status":
            raise OSError(error, MARKER)
        return original(directory, name)

    monkeypatch.setattr(ProcReader, "_read", staticmethod(denied))
    result = checker(proc).check([client()])[0]
    assert result == Identity(Verdict.MISMATCH, AppClass.DION, Reason.UNAVAILABLE)
    assert not result.offer_allowed and not result.variant1_allowed


@pytest.mark.parametrize("measured_exe", [False, True])
@pytest.mark.parametrize(
    "connections,claimants,reason",
    [(0, 1, Reason.NO_CONNECTION), (1, 2, Reason.COUNTER)],
)
@pytest.mark.parametrize("failure", [ProcUnavailable, PermissionError])
def test_review_repeat_snapshot_error_overrides_negative_sockets(
    proc: ProcReader,
    monkeypatch: pytest.MonkeyPatch,
    measured_exe: bool,
    connections: int,
    claimants: int,
    reason: Reason,
    failure: type[Exception],
) -> None:
    # Each claimant gets valid initial facts, socket evidence, then a failed
    # repeat snapshot; compare with the SAME input without the read failure.
    catalog = verified_catalog() if measured_exe else declared_only_catalog()
    items = [client(index) for index in range(claimants)]
    control = IdentityChecker(catalog, proc, uid=UID, connections=Connections(connections)).check(
        items
    )
    original = proc.snapshot
    reads = 0

    def flaky(pid: int) -> ProcessFacts:
        nonlocal reads
        reads += 1
        if reads % 2 == 0:
            raise failure(MARKER)
        return original(pid)

    monkeypatch.setattr(proc, "snapshot", flaky)
    sockets = Connections(connections)
    results = IdentityChecker(catalog, proc, uid=UID, connections=sockets).check(items)
    assert reads == 2 * claimants and len(sockets.seen) == claimants
    assert all(result.reason is reason for result in control)
    assert all(
        result == Identity(Verdict.MISMATCH, AppClass.DION, Reason.UNAVAILABLE)
        for result in results
    )
    assert all(not result.offer_allowed and not result.variant1_allowed for result in results)


@pytest.mark.parametrize("field", ["exe", "status"])
def test_repeat_eacces_overrides_proven_socket_zero(
    proc: ProcReader, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    reads = 0
    original_link, original_read = os.readlink, ProcReader._read

    def deny_second(name: str) -> None:
        nonlocal reads
        if name == field:
            reads += 1
            if reads == 2:
                raise PermissionError(errno.EACCES, MARKER)

    def readlink(path: str, *, dir_fd: int | None = None) -> str:
        deny_second(path)
        return original_link(path, dir_fd=dir_fd)

    def read(directory: int, name: str) -> bytes:
        deny_second(name)
        return original_read(directory, name)

    monkeypatch.setattr(os, "readlink", readlink)
    monkeypatch.setattr(ProcReader, "_read", staticmethod(read))
    sockets = Connections(0)
    result = checker(proc, sockets).check([client()])[0]
    assert reads == 2 and sockets.seen == [KEY]
    assert result == Identity(Verdict.MISMATCH, AppClass.DION, Reason.UNAVAILABLE)
    assert not result.offer_allowed and not result.variant1_allowed


def test_review_all_identity_dataclass_reprs_hide_process_generation() -> None:
    private_pid, private_starttime = 123456789, 987654321
    key = ProcessKey(private_pid, private_starttime)
    private_uids = (314159265, 271828182, 161803398, 141421356)
    private_exe = "/private/" + MARKER
    markers = (str(private_pid), str(private_starttime), *map(str, private_uids), private_exe)
    instances = {
        "Identity": Identity(Verdict.DECLARED),
        "ProcessKey": key,
        "ProcessFacts": ProcessFacts(key, private_uids, private_exe),
        "Client": Client(
            1, {"application.process.id": private_pid, "starttime": private_starttime}
        ),
        "SocketEvidence": SocketEvidence(),
        "SocketFds": SocketFds(key, {private_pid: private_starttime}, True),
        "SocketMeasurement": SocketMeasurement(
            SsStatus.OK,
            Reason.CONNECTION,
            {key: private_pid},
            _insufficient_by_pid={private_pid: SocketInsufficiency.FD_CHANGED},
        ),
        "_Endpoint": _Endpoint(
            b"u_str", True, str(private_pid).encode(), private_starttime, str(private_pid).encode()
        ),
    }
    seen: set[str] = set()
    leaks: list[str] = []
    assert identity_module.__file__ is not None
    for path in Path(identity_module.__file__).parent.glob("identity*.py"):
        module = importlib.import_module(f"astra_voice.meetings.{path.stem}")
        for name, cls in vars(module).items():
            if (
                not isinstance(cls, type)
                or not is_dataclass(cls)
                or cls.__module__ != module.__name__
            ):
                continue
            # New dataclasses require an explicit representative, not a silent skip.
            instance = instances[name]
            assert type(instance) is cls
            seen.add(name)
            representation = repr(instance)
            if any(marker in representation for marker in markers):
                leaks.append(name)
    assert seen == set(instances)
    assert not leaks, "private data leaked by dataclasses: " + ", ".join(leaks)


@pytest.mark.parametrize("measured_exe", [False, True])
@pytest.mark.parametrize(
    "failure,expected",
    [
        (ProcessGone, Identity(Verdict.MISMATCH, AppClass.DION, Reason.DEAD)),
        (ProcessReused, Identity(Verdict.MISMATCH, AppClass.DION, Reason.REUSED)),
        (IdentityLimit, Identity(Verdict.DECLARED, reason=Reason.LIMIT)),
    ],
)
def test_round3_socket_adapter_process_failures(
    proc: ProcReader,
    measured_exe: bool,
    failure: type[Exception],
    expected: Identity,
) -> None:
    calls: list[ProcessKey] = []

    class FailingSockets:
        def evidence(self, process: ProcessKey) -> SocketEvidence:
            calls.append(process)
            raise failure(MARKER)

    catalog = verified_catalog() if measured_exe else declared_only_catalog()
    result = IdentityChecker(catalog, proc, uid=UID, connections=FailingSockets()).check(
        [client()]
    )[0]
    assert calls == [KEY]  # valid first snapshot; failure comes from sockets
    assert result == expected
    assert not result.variant1_allowed and not result.offer_allowed


@pytest.mark.parametrize("failure", [ProcUnavailable, PermissionError, TimeoutError, ValueError])
def test_round3_socket_adapter_unavailability_remains_declared(
    proc: ProcReader,
    failure: type[Exception],
) -> None:
    class UnavailableSockets:
        def evidence(self, process: ProcessKey) -> SocketEvidence:
            raise failure(MARKER)

    result = IdentityChecker(
        verified_catalog(), proc, uid=UID, connections=UnavailableSockets()
    ).check([client()])[0]
    reason = Reason.SS_TIMEOUT if failure is TimeoutError else Reason.SS_INSUFFICIENT
    assert result == Identity(Verdict.DECLARED, AppClass.DION, reason)
    assert result.offer_allowed and not result.variant1_allowed


def test_round3_unexpected_socket_adapter_fault_is_not_claimed_as_ss_unavailable(
    proc: ProcReader,
) -> None:
    class BrokenSockets:
        def evidence(self, process: ProcessKey) -> SocketEvidence:
            raise RuntimeError(MARKER)

    result = IdentityChecker(verified_catalog(), proc, uid=UID, connections=BrokenSockets()).check(
        [client()]
    )[0]
    assert result == Identity(Verdict.DECLARED, reason=Reason.UNAVAILABLE)
    assert not result.variant1_allowed
