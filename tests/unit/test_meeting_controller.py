"""Controller admission, deadlines and durable GUI ownership (no GUI or audio)."""

from __future__ import annotations

import ast
import os
from pathlib import Path
from typing import Any

import pytest

from astra_voice.core.command_mode import SessionSnapshot
from astra_voice.core.meeting_session import MeetingBridge, MeetingController, StorageStatus
from astra_voice.meetings.events import MeetingState
from astra_voice.meetings.model import Meeting
from astra_voice.meetings.segments import CaptureJournal
from astra_voice.meetings.store import MeetingStore, MeetingStoreError, Writer
from astra_voice.platform.session import SessionKind
from astra_voice.worker import ipc

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("filesystem_euid")]
MID = "2026-11-01_120000-abcd"


class Supervisor:
    ready = True
    exited = False
    generation = 1

    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []
        self.kills = 0

    def send(self, message: dict[str, Any], *, timeout: float | None = None) -> dict[str, Any]:
        ipc.decode(ipc.encode(message, role="meeting-capture")[4:], role="meeting-capture")
        self.messages.append(message)
        return message

    def set_meeting_generation(self, meeting_id: str, generation: int) -> None:
        pass

    def kill(self) -> None:
        self.kills += 1

    def pump(self, timeout: float = 0.0) -> None:
        pass


class Rig:
    def __init__(
        self, root: Path, kind: SessionKind = SessionKind.KDE, *, asynchronous: bool = False
    ) -> None:
        self.now = 100.0
        self.snapshot = SessionSnapshot(known=True, locked=False, blocked_epoch=7)
        self.storage = StorageStatus(1000, True, False)
        self.supervisor = Supervisor()
        self.store = MeetingStore(root / "meetings")
        self.controller = MeetingController(
            self.store,
            self.supervisor,
            session=lambda: self.snapshot,
            session_kind=kind,
            clock=lambda: self.now,
            storage_probe=lambda path: self.storage,
            id_factory=lambda: MID,
            enabled=lambda: True,
            policy_allowed=lambda: True,
            metadata_submit=None if asynchronous else lambda operation: operation(),
            filesystem_submit=lambda operation: operation(),
        )
        self.bridge = MeetingBridge(self.controller)

    def event(self, kind: str, **values: Any) -> None:
        request = self.supervisor.messages[-1]
        event: dict[str, Any] = {
            "type": "meeting.capture." + kind,
            "meeting_id": MID,
            "meeting_generation": 0,
            "capture_id": "capture1",
            "state_rev": 0,
        }
        if kind in {"prepared", "started", "paused"}:
            event.update(
                req=request["req"],
                request_type=request["type"],
                pause_reasons=[],
                manual_resume_required=False,
                blocked_epoch=2,
            )
        if kind == "prepared":
            event.update(
                mic_label="", server_local=True, server_kind="pipewire", far_available=False
            )
        if kind == "started":
            event.update(t0_mono_ms=int(self.now * 1000), tracks=["mic"], state_rev=1)
        event.update(values)
        ipc.decode(ipc.encode(event, role="meeting-capture")[4:], role="meeting-capture")
        self.controller.handle_event({**event, "generation": self.supervisor.generation})

    def recording(self) -> None:
        self.bridge.on_start_clicked()
        self.event("prepared")
        self.event("started")


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    return Rig(tmp_path)


def test_reservation_not_visible_and_commit(rig: Rig) -> None:
    rig.store.reserve(MID)
    assert rig.store.list() == []
    rig.store.commit_reserved(Meeting(id=MID, started_at="2026-11-01T12:00:00Z"))
    assert rig.store.list()[0].id == MID
    with pytest.raises(MeetingStoreError):
        rig.store.discard_reserved(MID)


def test_reservation_refuses_reuse_and_nonempty_discard(rig: Rig) -> None:
    rig.store.reserve(MID)
    with pytest.raises(MeetingStoreError):
        rig.store.reserve(MID)
    (rig.store.root / MID / "foreign").write_text("retain")
    with pytest.raises(MeetingStoreError):
        rig.store.discard_reserved(MID)
    assert (rig.store.root / MID / "foreign").exists()


@pytest.mark.parametrize(
    "snapshot",
    [
        SessionSnapshot(),
        SessionSnapshot(known=True, locked=True),
        SessionSnapshot(known=True, locked=False, active=False),
    ],
)
def test_session_rejected_before_prepare(rig: Rig, snapshot: SessionSnapshot) -> None:
    rig.snapshot = snapshot
    rig.bridge.on_start_clicked()
    assert rig.supervisor.messages == []
    assert rig.store.list() == []


def test_fly_missing_signal_allows_start(tmp_path: Path) -> None:
    rig = Rig(tmp_path, SessionKind.FLY)
    rig.snapshot = SessionSnapshot(known=False, supported=False)
    rig.bridge.on_start_clicked()
    assert rig.controller.lock_signal_unavailable
    assert rig.supervisor.messages[-1]["type"].endswith("prepare")


def test_prepare_timeout_does_not_publish_empty_meeting(rig: Rig) -> None:
    rig.bridge.on_start_clicked()
    assert rig.store.list() == []
    rig.now += 5.01
    rig.controller.tick()
    assert rig.controller.error_code == "audio-unavailable"
    assert rig.supervisor.kills == 1
    assert (rig.store.root / MID).exists()  # no deletion before confirmed exit
    rig.controller.on_process_exit(3)
    assert rig.store.list() == []
    assert not (rig.store.root / MID).exists()
    rig.event("prepared")
    assert not any(m["type"].endswith(".open") for m in rig.supervisor.messages)


def test_prepared_rechecks_gui_epoch_and_closes_without_open(rig: Rig) -> None:
    rig.bridge.on_start_clicked()
    rig.snapshot = SessionSnapshot(known=True, locked=False, blocked_epoch=8)
    rig.event("prepared")
    assert not any(m["type"].endswith(".open") for m in rig.supervisor.messages)
    assert rig.store.list() == []


def test_open_uses_capture_epoch_and_current_admission(rig: Rig) -> None:
    rig.bridge.on_start_clicked()
    rig.event("prepared", server_local=False)
    opened = rig.supervisor.messages[-1]
    assert opened["blocked_epoch"] == 2
    assert opened["admission_mono_ms"] == 100000
    assert rig.store.list()[0].warnings.audio_server_remote


def test_lock_pauses_and_unlock_does_not_resume(rig: Rig) -> None:
    rig.recording()
    rig.snapshot = SessionSnapshot(known=True, locked=True, blocked_epoch=8)
    rig.controller.on_session_changed()
    assert rig.supervisor.messages[-1]["reason"] == "lock"
    rig.snapshot = SessionSnapshot(known=True, locked=False, blocked_epoch=8)
    before = len(rig.supervisor.messages)
    rig.controller.on_session_changed()
    assert len(rig.supervisor.messages) == before
    assert rig.controller.manual_resume_required


def test_closed_is_not_saved_and_escalation_waits_for_exit(rig: Rig) -> None:
    rig.recording()
    rig.bridge.on_stop_clicked()
    rig.now += 1.001
    rig.controller.tick()
    assert rig.controller.state == "closing-unknown"
    assert rig.supervisor.kills == 1
    before = len(rig.supervisor.messages)
    rig.bridge.on_start_clicked()
    assert len(rig.supervisor.messages) == before
    rig.controller.on_process_exit(3)
    assert rig.controller.state == "stopped"
    assert not rig.controller.saved


def test_closed_then_finalized_persists_recorded(rig: Rig) -> None:
    rig.recording()
    rig.now += 20
    rig.bridge.on_stop_clicked()
    rig.event("closed", tracks=["mic"], state_rev=2)
    assert rig.controller.state == "stopped"
    assert not rig.controller.saved
    rig.event(
        "finalized",
        segments={"mic": 1, "far": 0},
        intervals=[
            {"t_mono_ms": 100000, "recorded_ms": 0},
            {"t_mono_ms": 120000, "recorded_ms": 20000},
        ],
        status="succeeded",
        state_rev=2,
    )
    assert rig.controller.saved
    assert rig.store.list()[0].state == MeetingState.RECORDED


def test_limit_extend_hard_cap_and_warning(rig: Rig) -> None:
    rig.recording()
    for _ in range(3):
        rig.bridge.on_extend_clicked()
    assert rig.controller.meeting is not None
    assert rig.controller.meeting.limit_ms == 14400000
    assert len([m for m in rig.supervisor.messages if m["type"].endswith("extend")]) == 2
    rig.now += 14400 - 300
    rig.controller.tick()
    assert rig.controller.limit_warning
    rig.now += 300
    rig.controller.tick()
    assert rig.controller.state == "stopping"


def test_pause_excluded_from_limit_and_long_pause_stops(rig: Rig) -> None:
    rig.recording()
    rig.now += 10
    rig.bridge.on_pause_clicked()
    rig.event("closed", tracks=["mic"], state_rev=2)
    rig.now += 3500
    assert rig.controller.recorded_ms == 10000
    rig.controller.tick()
    assert rig.controller.state == "paused"
    rig.now += 101
    rig.controller.tick()
    assert rig.controller.state == "stopping"


@pytest.mark.parametrize("cause", ["sleep", "inactive", "disk"])
def test_terminal_stop_sources(rig: Rig, cause: str) -> None:
    rig.recording()
    if cause == "sleep":
        rig.snapshot = SessionSnapshot(known=True, locked=False, preparing_for_sleep=True)
    elif cause == "inactive":
        rig.snapshot = SessionSnapshot(known=True, locked=False, active=False)
    else:
        rig.storage = StorageStatus(199, True, False)
        rig.now += 10
    rig.controller.tick()
    assert rig.controller.state == "stopping"


@pytest.mark.parametrize("reason", ["lock", "lease"])
def test_recover_durable_boundaries_without_start(rig: Rig, reason: str) -> None:
    rig.store.create(
        Meeting(id=MID, started_at="2026-11-01T12:00:00Z", state=MeetingState.RECORDING)
    )
    journal = CaptureJournal(
        MID,
        0,
        "capture1",
        boundaries=[
            {"kind": "open", "t_mono_ms": 100000, "recorded_ms": 0, "reason": None},
            {"kind": "close", "t_mono_ms": 105000, "recorded_ms": 5000, "reason": reason},
        ],
    )
    rig.store.write(MID, "capture.json", journal, writer=Writer.CAPTURE)
    rig.controller.recover()
    meeting = rig.store.list()[0]
    assert meeting.state == MeetingState.INTERRUPTED
    assert meeting.recorded_ms == 5000
    assert [(edge.t_mono_ms, edge.recorded_ms) for edge in meeting.intervals] == [
        (100000, 0),
        (105000, 5000),
    ]
    assert meeting.marks[-1].reason == reason
    assert MID in rig.controller.interrupted_dialog_ids
    assert rig.supervisor.messages == []


def _opening_calls(source: str) -> list[tuple[str, str, str]]:
    tree = ast.parse(source)
    typed = {"MeetingController"}
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.arg)
            and node.annotation is not None
            and ast.unparse(node.annotation) == "MeetingController"
        ):
            typed.add(node.arg)
        elif (
            isinstance(node, ast.AnnAssign) and ast.unparse(node.annotation) == "MeetingController"
        ):
            typed.add(ast.unparse(node.target))
        elif (
            isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Call)
            and ast.unparse(node.value.func) == "MeetingController"
        ):
            typed.update(ast.unparse(target) for target in node.targets)
    found: list[tuple[str, str, str]] = []

    class Visitor(ast.NodeVisitor):
        cls = ""
        method = ""

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            previous, self.cls = self.cls, node.name
            self.generic_visit(node)
            self.cls = previous

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            previous, self.method = self.method, node.name
            self.generic_visit(node)
            self.method = previous

        def visit_Call(self, node: ast.Call) -> None:
            fn = node.func
            if isinstance(fn, ast.Attribute) and fn.attr in {"start", "resume", "extend"}:
                receiver = ast.unparse(fn.value)
                if (
                    receiver in typed
                    or receiver == "self.controller"
                    or receiver.startswith("MeetingController(")
                ):
                    found.append((self.cls, self.method, fn.attr))
            self.generic_visit(node)

    Visitor().visit(tree)
    return found


def test_only_click_bridge_calls_controller_opening_methods() -> None:
    calls = [
        call for path in Path("src").rglob("*.py") for call in _opening_calls(path.read_text())
    ]
    assert sorted(calls) == sorted(
        [("MeetingBridge", f"on_{name}_clicked", name) for name in ("start", "resume", "extend")]
    )
    assert _opening_calls("def automatic(controller: MeetingController): controller.start()") == [
        ("", "automatic", "start")
    ]


def test_reservation_crash_after_metadata_still_visible(rig: Rig) -> None:
    rig.store.reserve(MID)
    rig.store.commit_reserved(
        Meeting(id=MID, started_at="2026-11-01T12:00:00Z", state=MeetingState.RECORDING)
    )
    marker = rig.store.root / MID / ".preparing"
    marker.write_bytes(b"1\n")
    marker.chmod(0o600)
    assert len(rig.store.list()) == 1
    rig.controller.recover()
    assert rig.store.list()[0].state == MeetingState.INTERRUPTED


def test_reservation_commit_failure_can_be_discarded(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:

    rig.store.reserve(MID)
    original = os.mkdir

    def failing(path: Any, *args: Any, **kwargs: Any) -> None:
        if path == "exports":
            raise OSError("injected")
        original(path, *args, **kwargs)

    monkeypatch.setattr(os, "mkdir", failing)
    with pytest.raises(MeetingStoreError):
        rig.store.commit_reserved(Meeting(id=MID, started_at="2026-11-01T12:00:00Z"))
    rig.store.discard_reserved(MID)
    assert rig.store.list() == []


def test_autonomous_lease_resumes_via_alive(rig: Rig) -> None:
    rig.recording()
    rig.event(
        "autopaused",
        reason="lease",
        at_recorded_ms=1000,
        pause_reasons=["lease"],
        manual_resume_required=False,
        blocked_epoch=2,
        state_rev=2,
    )
    rig.event("closed", tracks=["mic"], state_rev=2)
    rig.event(
        "alive",
        seq=1,
        state="recording",
        recorded_ms=1000,
        tracks={"mic": "live", "far": "ended"},
        free_mb=1000,
        cpu_pct=0,
        pause_reasons=[],
        manual_resume_required=False,
        blocked_epoch=2,
        state_rev=3,
    )
    assert rig.controller.state == "recording"
    assert rig.controller.pause_reasons == set()


def test_prepared_audio_error_without_capture_identity(rig: Rig) -> None:
    rig.bridge.on_start_clicked()
    request = rig.supervisor.messages[-1]
    rig.controller.handle_event(
        {
            "type": "error",
            "code": "audio-failed",
            "message": "meeting-command-failed",
            "req": request["req"],
            "request_type": request["type"],
            "meeting_id": MID,
            "meeting_generation": 0,
        }
    )
    assert rig.controller.error_code == "audio-unavailable"
    assert rig.controller.state == "closing-unknown"


def test_resume_waits_for_finalization(rig: Rig) -> None:
    rig.recording()
    rig.bridge.on_pause_clicked()
    rig.event("closed", tracks=["mic"], state_rev=2)
    assert not rig.bridge.on_resume_clicked()


def test_network_storage_warning_persisted_and_thresholds(rig: Rig) -> None:
    rig.storage = StorageStatus(599, True, True)
    assert not rig.bridge.on_start_clicked()
    assert rig.supervisor.messages == []
    assert rig.controller.error_text == (
        "Мало места на диске: для записи встречи нужно около 600 МБ. Освободите "
        "место или удалите старые встречи."
    )
    rig.storage = StorageStatus(600, True, True)
    rig.recording()
    assert rig.store.list()[0].warnings.storage_network
    assert rig.controller.warnings[0] == (
        "Домашняя папка находится на сетевом диске — запись встречи будет "
        "храниться не только на этом компьютере. Записывайте, только если это "
        "разрешено в вашей организации."
    )


def test_stale_process_generation_does_not_open(rig: Rig) -> None:
    rig.bridge.on_start_clicked()
    request = rig.supervisor.messages[-1]
    rig.controller.handle_event(
        {
            "type": "meeting.capture.prepared",
            "req": request["req"],
            "request_type": request["type"],
            "meeting_id": MID,
            "meeting_generation": 0,
            "capture_id": "old",
            "state_rev": 0,
            "pause_reasons": [],
            "manual_resume_required": False,
            "blocked_epoch": 2,
            "mic_label": "",
            "server_local": True,
            "server_kind": "pipewire",
            "far_available": False,
            "generation": 0,
        }
    )
    assert len(rig.supervisor.messages) == 1


def test_reserve_write_failure_does_not_poison_catalogue(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    import astra_voice.meetings.store as module

    def fail(*args: Any, **kwargs: Any) -> None:
        raise OSError("injected")

    monkeypatch.setattr(module, "write_private", fail)
    with pytest.raises(MeetingStoreError):
        rig.store.reserve(MID)
    assert rig.store.list() == []


def test_closed_without_finalized_recovered_after_gui_death(rig: Rig) -> None:
    rig.recording()
    rig.bridge.on_stop_clicked()
    rig.event("closed", tracks=["mic"], state_rev=2)
    rig.controller.recover()
    assert rig.store.list()[0].state == MeetingState.INTERRUPTED


def test_confirmed_prepare_exit_allows_new_click(rig: Rig) -> None:
    rig.bridge.on_start_clicked()
    rig.now += 6
    rig.controller.tick()
    rig.controller.on_process_exit(3)
    assert rig.bridge.on_start_clicked()


def test_decorated_supervisor_prepared_event(rig: Rig) -> None:
    rig.bridge.on_start_clicked()
    request = rig.supervisor.messages[-1]
    event = {
        "type": "meeting.capture.prepared",
        "req": request["req"],
        "request_type": request["type"],
        "meeting_id": MID,
        "meeting_generation": 0,
        "capture_id": "current",
        "state_rev": 0,
        "pause_reasons": [],
        "manual_resume_required": False,
        "blocked_epoch": 2,
        "mic_label": "",
        "server_local": True,
        "server_kind": "pipewire",
        "far_available": True,
        "generation": 1,
    }
    rig.controller.handle_event(event)
    assert rig.supervisor.messages[-1]["type"] == "meeting.capture.open"


def test_hanging_metadata_writer_does_not_stall_control(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading

    rig = Rig(tmp_path, asynchronous=True)
    entered, release, returned = threading.Event(), threading.Event(), threading.Event()
    original = rig.store.write

    def hanging(*args: Any, **kwargs: Any) -> None:
        entered.set()
        assert release.wait(2)
        original(*args, **kwargs)

    monkeypatch.setattr(rig.store, "write", hanging)
    rig.bridge.on_start_clicked()
    rig.event("prepared")

    def started() -> None:
        rig.event("started")
        returned.set()

    thread = threading.Thread(target=started, daemon=True)
    thread.start()
    try:
        assert entered.wait(1)
        assert returned.wait(0.2), "GUI started callback blocked by metadata fsync"
        rig.snapshot = SessionSnapshot(known=True, locked=True, blocked_epoch=8)
        rig.controller.on_session_changed()
        assert rig.supervisor.messages[-1]["reason"] == "lock"
        rig.now += 1.01
        rig.controller.tick()
        assert rig.supervisor.kills == 1
    finally:
        release.set()
        thread.join(2)


def test_recovery_never_interpolates_clock_epoch_gap(rig: Rig) -> None:
    from astra_voice.meetings import MeetingValidationError
    from astra_voice.meetings.segments import SegmentSidecar
    from astra_voice.meetings.timeline import monotonic_to_recorded

    rig.store.create(
        Meeting(id=MID, started_at="2026-11-01T12:00:00Z", state=MeetingState.RECORDING)
    )
    rig.store.write(
        MID,
        "capture.json",
        CaptureJournal(
            MID,
            0,
            "capture1",
            boundaries=[{"kind": "open", "t_mono_ms": 100000, "recorded_ms": 0, "reason": None}],
        ),
        writer=Writer.CAPTURE,
    )
    for index, mono in enumerate((100000, 110000)):
        side = SegmentSidecar(
            MID,
            f"epoch{index}",
            capture_id="capture1",
            index=index,
            opened_t_mono_ms=mono,
            recorded_ms_at_open=index * 1000,
            frames=16000,
            frames_at_checkpoint=16000,
            sample_end=16000,
            timeline=[[0, mono], [16000, mono + 1000]],
            discontinuity=index == 0,
        )
        rig.store.write(MID, f"audio/mic-{index:04d}.json", side, writer=Writer.CAPTURE)
    rig.controller.recover()
    meeting = rig.store.list()[0]
    with pytest.raises(MeetingValidationError):
        monotonic_to_recorded([x.to_dict() for x in meeting.intervals], 106000)
    assert monotonic_to_recorded([x.to_dict() for x in meeting.intervals], 110500) == 1500
    assert meeting.recorded_ms == 2000


def test_replace_supervisor_only_after_confirmed_exit(rig: Rig) -> None:
    rig.bridge.on_start_clicked()
    replacement = Supervisor()
    assert not rig.controller.attach_supervisor(replacement)
    rig.now += 6
    rig.controller.tick()
    assert not rig.controller.attach_supervisor(replacement)
    rig.controller.on_process_exit(3)
    assert rig.controller.attach_supervisor(replacement)
    assert replacement.messages == []
    assert rig.bridge.on_start_clicked()
    assert replacement.messages[0]["type"].endswith("prepare")


@pytest.mark.parametrize("clicks", [0, 1, 2])
def test_limits_exact_boundaries(rig: Rig, clicks: int) -> None:
    rig.recording()
    for _ in range(clicks):
        rig.bridge.on_extend_clicked()
    rig.now += 7200 + clicks * 3600 - 301
    rig.controller.tick()
    assert not rig.controller.limit_warning
    rig.now += 1
    rig.controller.tick()
    assert rig.controller.limit_warning
    assert rig.controller.state == "recording"
    rig.now += 300
    rig.controller.tick()
    assert rig.controller.state == "stopping"


def test_admission_prechecks_order_and_short_circuit(rig: Rig) -> None:
    trace: list[str] = []

    def gate(label: str) -> bool:
        trace.append(label)
        return True

    def session() -> SessionSnapshot:
        trace.append("session")
        return rig.snapshot

    def storage(path: Path) -> StorageStatus:
        trace.append("storage")
        return rig.storage

    rig.controller.enabled = lambda: gate("enabled")
    rig.controller.policy_allowed = lambda: gate("policy")
    rig.controller.session = session
    rig.controller.storage_probe = storage
    rig.bridge.on_start_clicked()
    assert trace[:4] == ["enabled", "policy", "session", "storage"]
    trace.clear()
    rig.bridge.on_start_clicked()
    assert trace == ["enabled", "policy"]


@pytest.mark.parametrize("magic", [0x6969, 0xFF534D42, 0xFE534D42])
def test_storage_probe_statfs_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, magic: int
) -> None:
    from astra_voice.core import meeting_session as module

    monkeypatch.setattr(module, "filesystem_type", lambda fd: magic)
    status = module.probe_storage(tmp_path / "meetings")
    assert status.writable and status.network and status.free_mb > 0


def test_inert_inputs_never_start(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ASTRA_VOICE_MEETING_RECORDING", "1")
    monkeypatch.setattr("sys.argv", ["astra-voice", "--record-meeting"])
    rig.controller.tick()
    rig.controller.recover()
    rig.controller.on_session_changed()
    assert rig.supervisor.messages == []


def test_lock_epoch_requires_authoritative_snapshot_before_resume(rig: Rig) -> None:
    rig.recording()
    rig.bridge.on_pause_clicked()
    rig.event("closed", tracks=["mic"], state_rev=2)
    rig.event(
        "finalized", segments={"mic": 0, "far": 0}, intervals=[], status="succeeded", state_rev=2
    )
    rig.snapshot = SessionSnapshot(known=True, locked=True, blocked_epoch=8)
    rig.controller.on_session_changed()
    rig.event("closed", tracks=[], state_rev=3)
    rig.event(
        "finalized", segments={"mic": 0, "far": 0}, intervals=[], status="succeeded", state_rev=3
    )
    rig.snapshot = SessionSnapshot(known=True, locked=False, blocked_epoch=8)
    assert not rig.bridge.on_resume_clicked()
    rig.event(
        "alive",
        seq=1,
        state="paused",
        recorded_ms=0,
        tracks={"mic": "paused", "far": "ended"},
        free_mb=1000,
        cpu_pct=0,
        pause_reasons=["user", "lock"],
        manual_resume_required=True,
        blocked_epoch=3,
        state_rev=3,
    )
    assert rig.bridge.on_resume_clicked()
    assert rig.supervisor.messages[-1]["blocked_epoch"] == 3


def test_resume_admission_refusal_keeps_paused_session(rig: Rig) -> None:
    rig.recording()
    rig.bridge.on_pause_clicked()
    rig.event("closed", tracks=["mic"], state_rev=2)
    rig.event(
        "finalized", segments={"mic": 0, "far": 0}, intervals=[], status="succeeded", state_rev=2
    )
    assert rig.bridge.on_resume_clicked()
    request = rig.supervisor.messages[-1]
    rig.controller.handle_event(
        {
            "type": "error",
            "code": "admission-stale",
            "message": "meeting-command-failed",
            "req": request["req"],
            "request_type": request["type"],
            "meeting_id": MID,
            "meeting_generation": 0,
        }
    )
    assert rig.controller.state == "paused"
    assert rig.supervisor.kills == 0


def test_unbound_policy_defaults_to_disabled(tmp_path: Path) -> None:
    supervisor = Supervisor()
    controller = MeetingController(
        MeetingStore(tmp_path / "meetings"),
        supervisor,
        session=lambda: SessionSnapshot(known=True, locked=False),
        session_kind=SessionKind.KDE,
    )
    assert not MeetingBridge(controller).on_start_clicked()
    assert supervisor.messages == []
    assert controller.error_code == "disabled"


def test_reservation_normalizes_restrictive_umask(rig: Rig) -> None:
    import os

    rig.store.root.mkdir(mode=0o700)
    previous = os.umask(0o777)
    try:
        rig.store.reserve(MID)
    finally:
        os.umask(previous)
    assert (rig.store.root / MID).stat().st_mode & 0o777 == 0o700
    rig.store.discard_reserved(MID)


def test_real_capture_supervisor_lock_epoch_gate(tmp_path: Path) -> None:
    from astra_voice.worker.meeting_capture import MeetingCaptureState
    from astra_voice.worker.meeting_session import SessionAdmission
    from astra_voice.worker.supervisor import WorkerSupervisor

    local = SessionAdmission(kind="kde", clock=lambda: 100.0, max_age=999)
    local.update(known=True, active=True, locked=False, sleeping=False)
    local.drain()
    capture = MeetingCaptureState(clock=lambda: 100.0, session=local)
    identity = {
        "meeting_id": MID,
        "meeting_generation": 0,
        "capture_id": "capture1",
        "state_rev": 2,
    }
    capture.identity = dict(identity)
    capture.state = "paused"
    capture.pause_reasons = {"user"}
    capture.manual_resume_required = True
    snapshot = [SessionSnapshot(known=True, locked=True, blocked_epoch=7)]
    delivered: list[str] = []

    def deliver(event: dict[str, Any]) -> None:
        delivered.append(event["type"])
        controller.handle_event(event)

    supervisor = WorkerSupervisor(
        deliver, clock=lambda: 100.0, use_qt=False, role="meeting-capture"
    )
    supervisor.state = "running"
    supervisor._hello_accepted = True
    controller = MeetingController(
        MeetingStore(tmp_path / "meetings"),
        supervisor,
        session=lambda: snapshot[0],
        session_kind=SessionKind.KDE,
        clock=lambda: 100.0,
        metadata_submit=lambda job: None,
        enabled=lambda: True,
        policy_allowed=lambda: True,
    )
    controller.meeting = Meeting(
        id=MID, started_at="2026-11-01T12:00:00Z", state=MeetingState.PAUSED
    )
    controller.state = "paused"
    controller._identity = dict(identity)
    controller.pause_reasons = {"user"}
    controller.on_session_changed()
    requests = ipc.FrameReader(role="meeting-capture").feed(bytes(supervisor._outgoing))
    supervisor._outgoing.clear()
    for request in requests:
        for reply in capture.handle(request):
            supervisor._accept_meeting(reply, supervisor.generation)
    assert delivered == ["meeting.capture.closed", "meeting.capture.finalized"]
    local.signal("Unlock")
    local.update(known=True, active=True, locked=False, sleeping=False)
    snapshot[0] = SessionSnapshot(known=True, locked=False, blocked_epoch=7)
    assert not MeetingBridge(controller).on_resume_clicked()


def test_recovery_overlapping_tracks_never_regresses_recorded_time(rig: Rig) -> None:
    from astra_voice.meetings.segments import SegmentSidecar

    rig.store.create(
        Meeting(id=MID, started_at="2026-11-01T12:00:00Z", state=MeetingState.RECORDING)
    )
    for track, mono in (("mic", 100000), ("far", 100500)):
        side = SegmentSidecar(
            MID,
            track,
            track=track,
            opened_t_mono_ms=mono,
            frames=16000,
            frames_at_checkpoint=16000,
            sample_end=16000,
            timeline=[[0, mono], [16000, mono + 1000]],
        )
        rig.store.write(MID, f"audio/{track}-0000.json", side, writer=Writer.CAPTURE)
    rig.controller.recover()
    values = [edge.recorded_ms for edge in rig.store.list()[0].intervals]
    assert values == sorted(values)
    assert values[-1] == 1500


def test_commit_published_then_failed_exit_recovers(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = rig.store.commit_reserved

    def publish_then_fail(meeting: Meeting) -> None:
        original(meeting)
        raise MeetingStoreError("injected-post-publication-fsync")

    monkeypatch.setattr(rig.store, "commit_reserved", publish_then_fail)
    rig.bridge.on_start_clicked()
    rig.event("prepared")
    rig.controller.on_process_exit(3)
    assert rig.controller.state == "stopped"
    assert rig.store.list()[0].state == MeetingState.INTERRUPTED


def test_terminal_finalize_retires_role_before_next_click(rig: Rig) -> None:
    rig.recording()
    rig.bridge.on_stop_clicked()
    rig.event("closed", tracks=["mic"], state_rev=2)
    rig.event(
        "finalized", segments={"mic": 0, "far": 0}, intervals=[], status="succeeded", state_rev=2
    )
    assert rig.supervisor.messages[-1]["type"] == "meeting.capture.shutdown"
    assert not rig.bridge.on_start_clicked()
    request = rig.supervisor.messages[-1]
    rig.controller.handle_event(
        {
            "type": "meeting.capture.bye",
            "req": request["req"],
            "request_type": request["type"],
            "generation": 1,
        }
    )
    assert not rig.controller.attach_supervisor(Supervisor())
    rig.supervisor.exited = True
    rig.controller.tick()
    assert rig.controller.attach_supervisor(Supervisor())


def test_exit_before_gui_metadata_fsync_does_not_mark_success_interrupted(rig: Rig) -> None:
    jobs: list[Any] = []
    rig.controller._metadata_submit = jobs.append
    rig.recording()
    rig.bridge.on_stop_clicked()
    rig.event("closed", tracks=["mic"], state_rev=2)
    rig.event(
        "finalized", segments={"mic": 0, "far": 0}, intervals=[], status="succeeded", state_rev=2
    )
    rig.controller.on_process_exit(0)
    for job in jobs:
        job()
    rig.controller.tick()
    assert rig.store.list()[0].state == MeetingState.RECORDED
    assert rig.controller.saved


@pytest.mark.parametrize(
    "order", [("closed", "finalized", "alive"), ("alive", "finalized", "closed")]
)
def test_autonomous_stop_event_order_retires_and_saves(rig: Rig, order: tuple[str, ...]) -> None:
    rig.recording()
    for kind in order:
        if kind == "closed":
            rig.event(kind, tracks=["mic"], state_rev=2)
        elif kind == "finalized":
            rig.event(
                kind,
                segments={"mic": 1, "far": 0},
                intervals=[
                    {"t_mono_ms": 100000, "recorded_ms": 0},
                    {"t_mono_ms": 100100, "recorded_ms": 100},
                ],
                status="succeeded",
                state_rev=2,
            )
        else:
            rig.event(
                kind,
                seq=1,
                state="stopped",
                recorded_ms=100,
                tracks={"mic": "ended", "far": "ended"},
                free_mb=1000,
                cpu_pct=0,
                pause_reasons=[],
                manual_resume_required=False,
                blocked_epoch=3,
                state_rev=2,
            )
            if "closed" not in order[: order.index(kind)]:
                assert rig.controller.state != "stopped"
    assert rig.controller.state == "stopped"
    assert rig.controller.saved
    assert rig.supervisor.messages[-1]["type"] == "meeting.capture.shutdown"
    assert rig.store.list()[0].state == MeetingState.RECORDED


@pytest.mark.parametrize(
    ("reported_reason", "safe_reason"),
    [
        ("target-gone", "target-gone"),
        ("target-mismatch", "target-mismatch"),
        ("monitor-stream-unsupported", "monitor-stream-unsupported"),
        ("device /private/call-source host=example.invalid", "unavailable"),
    ],
)
def test_selected_far_unavailable_requires_fresh_mic_click(
    rig: Rig, reported_reason: str, safe_reason: str
) -> None:
    far = {"kind": "app_stream", "sink_input": 1, "object_serial": 2, "call_id": "call1"}
    assert rig.bridge.on_start_clicked(far)
    rig.event("prepared", far_available=False, far_reason=reported_reason)
    assert rig.store.list() == []
    assert not any(message["type"].endswith(".open") for message in rig.supervisor.messages)
    assert rig.controller.far_available is False
    assert rig.controller.far_reason == safe_reason
    assert rig.controller.mic_only_available
    assert rig.controller.error_code == "far-unavailable"
    assert rig.controller.error_text == ""
    assert rig.supervisor.messages[-1]["type"] == "meeting.capture.stop"
    rig.event("closed", tracks=[], state_rev=1)
    rig.event(
        "finalized", segments={"mic": 0, "far": 0}, intervals=[], status="succeeded", state_rev=1
    )
    assert rig.supervisor.messages[-1]["type"] == "meeting.capture.shutdown"
    assert (rig.store.root / MID).exists()
    assert not rig.bridge.on_start_clicked()
    rig.controller.on_process_exit(0)
    assert not (rig.store.root / MID).exists()
    replacement = Supervisor()
    assert rig.controller.attach_supervisor(replacement)
    assert replacement.messages == []
    assert rig.bridge.on_start_clicked()
    assert replacement.messages[-1]["type"] == "meeting.capture.prepare"
    assert replacement.messages[-1]["far"] == {"kind": "none"}


def test_selected_far_available_still_opens(rig: Rig) -> None:
    assert rig.bridge.on_start_clicked(
        {"kind": "app_stream", "sink_input": 1, "object_serial": 2, "call_id": "call1"}
    )
    rig.event("prepared", far_available=True)
    assert rig.supervisor.messages[-1]["type"] == "meeting.capture.open"
    assert rig.store.list()[0].variant == "app_stream"


def test_click_epoch_survives_lock_unlock_during_storage_probe(rig: Rig) -> None:
    def storage(path: Path) -> StorageStatus:
        rig.snapshot = SessionSnapshot(known=True, locked=False, blocked_epoch=8)
        return rig.storage

    rig.controller.storage_probe = storage
    assert not rig.bridge.on_start_clicked()
    assert not any(message["type"].endswith(".prepare") for message in rig.supervisor.messages)
    assert rig.store.list() == []
    assert rig.controller.error_code == "session-blocked"


def test_pause_inflight_keepalive_stale_revision_is_not_fatal(rig: Rig) -> None:
    from astra_voice.worker.meeting_capture import MeetingCaptureState

    rig.recording()
    rig.now += 0.9
    rig.bridge.on_pause_clicked()
    # Capture processed pause, but its ACK/closed are still in the pipe.
    capture = MeetingCaptureState(clock=lambda: rig.now)
    capture.identity = {**rig.controller._identity, "state_rev": 2}
    capture.state = "paused"
    capture.pause_reasons = {"user"}
    capture.manual_resume_required = True
    rig.now += 0.2
    rig.controller.tick()
    heartbeats = [m for m in rig.supervisor.messages if m["type"].endswith(".keepalive")]
    for message in heartbeats:
        for reply in capture.handle(message):
            rig.controller.handle_event(reply)
    assert rig.supervisor.kills == 0
    assert rig.controller.state == "paused"


@pytest.mark.parametrize("phase", ["probe-start", "reserve", "commit", "probe-tick"])
def test_filesystem_wait_never_blocks_controller(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    import threading

    from astra_voice.core.meeting_session import MetadataWriter

    entered, release, returned = threading.Event(), threading.Event(), threading.Event()
    if phase == "probe-tick":
        rig.recording()
        rig.now += 10
    elif phase == "commit":
        rig.bridge.on_start_clicked()
    # The default implementation must offload filesystem work; injecting a real
    # writer here keeps other rig setup synchronous and deterministic.
    rig.controller._filesystem_submit = MetadataWriter().submit
    if phase.startswith("probe"):
        original = rig.controller.storage_probe

        def blocked_probe(path: Path) -> StorageStatus:
            entered.set()
            assert release.wait(3)
            return original(path)

        rig.controller.storage_probe = blocked_probe
    elif phase == "reserve":
        original_reserve = rig.store.reserve

        def blocked_reserve(meeting_id: str) -> None:
            entered.set()
            assert release.wait(3)
            original_reserve(meeting_id)

        monkeypatch.setattr(rig.store, "reserve", blocked_reserve)
    else:
        original_commit = rig.store.commit_reserved

        def blocked_commit(meeting: Meeting) -> None:
            entered.set()
            assert release.wait(3)
            original_commit(meeting)

        monkeypatch.setattr(rig.store, "commit_reserved", blocked_commit)

    def invoke() -> None:
        try:
            if phase == "probe-tick":
                rig.controller.tick()
            elif phase == "commit":
                rig.event("prepared")
            else:
                rig.bridge.on_start_clicked()
        finally:
            returned.set()

    caller = threading.Thread(target=invoke)
    caller.start()
    try:
        assert entered.wait(1)
        assert returned.wait(0.2), "filesystem wait blocked the control callback"
        if phase == "probe-tick":
            rig.bridge.on_pause_clicked()
            rig.now += 1.001
            rig.controller.tick()
            assert rig.supervisor.kills == 1
        else:
            rig.snapshot = SessionSnapshot(known=True, locked=True, blocked_epoch=8)
            rig.controller.on_session_changed()
            rig.now += 1.001
            rig.controller.tick()
            assert rig.supervisor.kills == 1
    finally:
        release.set()
        caller.join(3)


def test_failed_finalization_recovers_durable_edges_after_exit(rig: Rig) -> None:
    rig.recording()
    rig.store.write(
        MID,
        "capture.json",
        CaptureJournal(
            MID,
            0,
            "capture1",
            boundaries=[
                {"kind": "open", "t_mono_ms": 100000, "recorded_ms": 0, "reason": None},
                {"kind": "close", "t_mono_ms": 105000, "recorded_ms": 5000, "reason": "disk"},
            ],
        ),
        writer=Writer.CAPTURE,
    )
    rig.now += 7
    rig.bridge.on_stop_clicked()
    rig.event("closed", tracks=["mic"], state_rev=2)
    rig.event(
        "finalized", segments={"mic": 0, "far": 0}, intervals=[], status="failed", state_rev=2
    )
    rig.controller.on_process_exit(3)
    meeting = rig.store.list()[0]
    assert meeting.recorded_ms == 5000
    assert len(meeting.intervals) == 2
    assert meeting.state == MeetingState.INTERRUPTED
    assert meeting.marks[-1].reason == "disk"
    assert not rig.controller.saved


@pytest.mark.parametrize("phase", ["probe", "reserve", "ready"])
def test_prepare_rechecks_click_admission_at_send(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    def blocked() -> None:
        rig.snapshot = SessionSnapshot(known=True, locked=False, blocked_epoch=8)

    if phase == "probe":

        def probe(path: Path) -> StorageStatus:
            blocked()
            return rig.storage

        rig.controller.storage_probe = probe
    elif phase == "reserve":
        original = rig.store.reserve

        def reserve(meeting_id: str) -> None:
            original(meeting_id)
            blocked()

        monkeypatch.setattr(rig.store, "reserve", reserve)
    else:
        rig.supervisor.ready = False
    rig.bridge.on_start_clicked()
    if phase == "ready":
        blocked()
        rig.supervisor.ready = True
        rig.controller.tick()
    assert not any(m["type"].endswith(".prepare") for m in rig.supervisor.messages)
    assert rig.controller.error_code == "session-blocked"


def test_resume_click_after_pause_deadline_stops_before_tick(rig: Rig) -> None:
    rig.recording()
    rig.bridge.on_pause_clicked()
    rig.event("closed", tracks=["mic"], state_rev=2)
    rig.event(
        "finalized", segments={"mic": 0, "far": 0}, intervals=[], status="succeeded", state_rev=2
    )
    rig.now += 3600.001
    assert not rig.bridge.on_resume_clicked()
    assert rig.controller.state == "stopping"
    assert rig.supervisor.messages[-1]["type"] == "meeting.capture.stop"
    assert not any(m["type"].endswith(".resume") for m in rig.supervisor.messages)


def test_recovery_duration_ignores_unconfirmed_gui_estimate(rig: Rig) -> None:
    rig.store.create(
        Meeting(
            id=MID,
            started_at="2026-11-01T12:00:00Z",
            state=MeetingState.RECORDING,
            recorded_ms=19000,
        )
    )
    rig.store.write(
        MID,
        "capture.json",
        CaptureJournal(
            MID,
            0,
            "capture1",
            boundaries=[
                {"kind": "open", "t_mono_ms": 100000, "recorded_ms": 0, "reason": None},
                {"kind": "close", "t_mono_ms": 110000, "recorded_ms": 10000, "reason": "stop"},
            ],
        ),
        writer=Writer.CAPTURE,
    )
    rig.controller.recover()
    meeting = rig.store.list()[0]
    assert meeting.recorded_ms == 10000
    assert meeting.recorded_ms == meeting.intervals[-1].recorded_ms


@pytest.mark.parametrize("phase", ["preflight", "commit"])
def test_late_filesystem_completion_after_confirmed_exit_never_opens(rig: Rig, phase: str) -> None:
    jobs: list[Any] = []
    if phase == "commit":
        rig.bridge.on_start_clicked()
    rig.controller._filesystem_submit = jobs.append
    if phase == "preflight":
        rig.bridge.on_start_clicked()
    else:
        rig.event("prepared")
    rig.bridge.on_stop_clicked()
    rig.controller.on_process_exit(0)
    assert not rig.bridge.on_start_clicked()
    while jobs:
        jobs.pop(0)()
        rig.controller.tick()
    assert not any(m["type"].endswith(".open") for m in rig.supervisor.messages)
    if phase == "preflight":
        assert not any(m["type"].endswith(".prepare") for m in rig.supervisor.messages)
        assert not (rig.store.root / MID).exists()
    else:
        assert rig.store.list()[0].state == MeetingState.INTERRUPTED
    assert rig.controller.state == "stopped"


def test_restart_recovers_failed_finalization_already_marked_interrupted(rig: Rig) -> None:
    rig.recording()
    rig.store.write(
        MID,
        "capture.json",
        CaptureJournal(
            MID,
            0,
            "capture1",
            boundaries=[
                {"kind": "open", "t_mono_ms": 100000, "recorded_ms": 0, "reason": None},
                {"kind": "close", "t_mono_ms": 105000, "recorded_ms": 5000, "reason": "disk"},
            ],
        ),
        writer=Writer.CAPTURE,
    )
    rig.bridge.on_stop_clicked()
    rig.event("closed", tracks=["mic"], state_rev=2)
    rig.event(
        "finalized", segments={"mic": 0, "far": 0}, intervals=[], status="failed", state_rev=2
    )
    assert rig.store.list()[0].state == MeetingState.INTERRUPTED
    restarted = Rig(rig.store.root.parent)
    restarted.controller.recover()
    meeting = rig.store.list()[0]
    assert meeting.recorded_ms == 5000
    assert len(meeting.intervals) == 2
    assert MID in restarted.controller.interrupted_dialog_ids
    assert restarted.supervisor.messages == []


def test_late_stale_heartbeat_error_after_pause_ack_is_harmless(rig: Rig) -> None:
    rig.recording()
    rig.now += 1
    rig.controller.tick()
    heartbeat = rig.supervisor.messages[-1]
    rig.bridge.on_pause_clicked()
    rig.event("closed", tracks=["mic"], state_rev=2)
    rig.controller.handle_event(
        {
            "type": "error",
            "code": "not-prepared",
            "message": "meeting-command-failed",
            "meeting_id": MID,
            "meeting_generation": 0,
            "req": heartbeat["req"],
            "request_type": heartbeat["type"],
        }
    )
    assert rig.controller.state == "paused"
    assert rig.supervisor.kills == 0


def test_keepalive_correlation_is_released_after_ack(rig: Rig) -> None:
    rig.recording()
    rig.now += 1
    rig.controller.tick()
    request = rig.supervisor.messages[-1]
    assert request["type"] == "meeting.capture.keepalive"
    rig.controller.handle_event(
        {
            "type": "meeting.ack",
            "req": request["req"],
            "request_type": request["type"],
            "meeting_id": MID,
            "meeting_generation": 0,
            "capture_id": "capture1",
            "state_rev": 1,
        }
    )
    assert request["req"] not in rig.controller._requests


def test_completed_preflight_past_deadline_never_prepares(rig: Rig) -> None:
    jobs: list[Any] = []
    rig.controller._filesystem_submit = jobs.append
    rig.bridge.on_start_clicked()
    rig.now += 5.001
    jobs.pop(0)()
    rig.controller.tick()
    assert not any(m["type"].endswith(".prepare") for m in rig.supervisor.messages)
    assert rig.controller.state == "closing-unknown"


@pytest.mark.parametrize("blocked", ["lock", "sleep"])
def test_session_cancel_before_capture_hello_uses_exit_escalation(rig: Rig, blocked: str) -> None:
    from astra_voice.worker.supervisor import WorkerSupervisor

    supervisor = WorkerSupervisor(
        lambda event: None, clock=lambda: rig.now, use_qt=False, role="meeting-capture"
    )
    supervisor.state = "running"
    rig.controller.supervisor = supervisor
    assert not supervisor.ready
    assert rig.bridge.on_start_clicked()
    rig.snapshot = SessionSnapshot(
        known=True,
        locked=blocked == "lock",
        preparing_for_sleep=blocked == "sleep",
        blocked_epoch=8,
    )
    rig.controller.tick()
    assert rig.controller.state == "closing-unknown"
    assert not supervisor._outgoing
    assert not supervisor._meeting_requests
    assert (rig.store.root / MID).exists()
    rig.controller.tick()  # exited is true: no spawned process in this isolated test.
    assert rig.controller.state == "stopped"
    assert not (rig.store.root / MID).exists()
