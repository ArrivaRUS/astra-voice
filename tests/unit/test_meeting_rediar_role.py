"""5в-3: operation fencing, shadow text, crash recovery and the D5 budget."""

from __future__ import annotations

import copy
import errno
import hashlib
import json
import os
import shutil
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from astra_voice.meetings import diar_models, model, store
from astra_voice.meetings.events import MeetingState, PhaseStatus
from astra_voice.models import diarization
from astra_voice.models.catalog import Catalog
from astra_voice.models.schema import SchemaInvalid, validate
from astra_voice.worker import ipc
from astra_voice.worker import meeting_contract as contract
from astra_voice.worker import meeting_diar as diar
from astra_voice.worker import meeting_transcribe as mt
from helpers.meeting_audio import MID, Rig, make_disk, tone, write_track

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("filesystem_euid", "synthetic_diar_models")]
WORK = "rediar-transcript.json"
CHECK = "rediar-checkpoint.json"
MODELS = {"segmentation_dir": "/absent/segmentation", "embedder_dir": "/absent/embedder"}


class Crash(BaseException):
    """Uncatchable at the role's ordinary error boundaries, like process death."""


def tree(disk: store.MeetingStore) -> dict[str, bytes]:
    base = disk.root / MID
    return {str(p.relative_to(base)): p.read_bytes() for p in base.rglob("*") if p.is_file()}


def published(disk: store.MeetingStore) -> tuple[bytes, bytes]:
    base = disk.root / MID
    return (base / "transcript.json").read_bytes(), (base / "speakers.json").read_bytes()


def put(disk: store.MeetingStore, name: str, value: Any) -> None:
    path = disk.root / MID / name
    path.write_bytes(value if isinstance(value, bytes) else json.dumps(value).encode())
    path.chmod(0o600)


def prepare(root: Path) -> store.MeetingStore:
    disk = make_disk(root)
    write_track(disk, "far", tone(50, 0.4))
    write_track(disk, "mic", tone(2, 0.3))
    rows = [
        model.Utterance(
            "source",
            track="far",
            start_ms=1000,
            end_ms=3000,
            text="первый второй",
            speaker="S9",
            final=True,
            multiple_speakers=True,
        ),
        model.Utterance(
            "filler",
            seq=1,
            track="far",
            start_ms=5000,
            end_ms=25000,
            text="фон",
            speaker="S9",
            final=True,
        ),
        model.Utterance(
            "filler2",
            seq=2,
            track="far",
            start_ms=26000,
            end_ms=46000,
            text="фон",
            speaker="S9",
            final=True,
        ),
    ]
    disk.write(
        MID,
        "transcript.json",
        model.Transcript(
            meeting_id=MID,
            transcript_revision=40,
            labels_final=True,
            utterances=rows,
            reconcile=model.ReconcileProgress(status=PhaseStatus.DONE),
        ),
        writer=store.Writer.TRANSCRIBE,
    )
    initial = disk.read(MID, "meeting.json")
    initial.state = MeetingState.DONE
    put(disk, "meeting.json", initial.to_dict())
    disk.write(
        MID,
        "speakers.json",
        model.SpeakerMap(labels={"S9": model.Speaker(name="Прежнее имя")}),
        writer=store.Writer.GUI,
    )
    meeting = disk.read(MID, "meeting.json")
    meeting.state = MeetingState.DONE
    meeting.audio.present = True
    meeting.transcript_generation = 0
    meeting.meeting_generation = 1
    meeting.rediar = model.Rediar(1, disk.transcript_state(MID).digest)
    disk.write(MID, "meeting.json", meeting, writer=store.Writer.GUI)
    return disk


class FakeDiar:
    def __init__(self, h: Harness) -> None:
        self.h = h
        self.alive = True
        self.peak_rss_kb = 100
        self.overall_deadline = float("inf")

    def load(self, models: dict[str, str]) -> None:
        assert models == self.h.request["diar_models"]

    def block(self, audio: Any, index: int, offset: int) -> list[list[Any]]:
        self.h.blocks.append(index)
        if self.h.split:
            return [[0, 2000, "S1"], [2000, 50000, "S2"]]
        return [[offset, offset + len(audio) // 16, "S1"]]

    def finish(self) -> dict[str, Any]:
        return {
            "label_map": {"S1": "S1", **({"S2": "S2"} if self.h.split else {})},
            "speakers_total": 2 if self.h.split else 1,
        }

    def close(self, *, completed: bool = False) -> bool:
        self.alive = False
        return completed


class Harness:
    """Synchronous executor with real disk, synthetic models and GUI receipts."""

    def __init__(self, disk: store.MeetingStore, *, split: bool = False) -> None:
        self.disk, self.split = disk, split
        self.rig = Rig(disk, vad=lambda: pytest.fail("rediar must not initialize VAD"))
        self.rig.load()
        self.state = self.rig.state
        self.request: dict[str, Any] = {
            "type": "meeting.job.start",
            "request_type": "meeting.job.start",
            "req": "redo",
            "meeting_id": MID,
            "meeting_generation": 1,
            "dir": str(disk.root / MID),
            "operation": "rediarize",
            "tracks": ["mic", "far"],
            "variant": "app_stream",
            "diarization": True,
            "diar_models": dict(MODELS),
            "self_track": "mic",
            "resume": True,
            "echo_gate": False,
            "limits": {"max_ms": 14_400_000, "max_utterances": 9000, "max_blocks": 12},
        }
        self.events: list[dict[str, Any]] = []
        self.blocks: list[int] = []
        self.calls: list[int] = []
        self.roles: list[FakeDiar] = []
        self.state.diar_factory = self.factory
        self.state._emit = self.emit  # type: ignore[method-assign, assignment]
        self.state._recognize = self.recognize  # type: ignore[method-assign]

    def factory(self, identity: Any, cancelled: Any, **kwargs: Any) -> FakeDiar:
        assert self.state.engine is None and self.rig.engine.unloads > 0
        cp = self.disk.read(MID, CHECK)
        assert cp.asr.status == PhaseStatus.DONE and cp.diar.status == PhaseStatus.RUNNING
        role = FakeDiar(self)
        self.roles.append(role)
        return role

    def recognize(self, job: mt._Job, audio: Any) -> str:
        assert self.split and all(not role.alive for role in self.roles)
        assert self.disk.read(MID, CHECK).diar.status == PhaseStatus.DONE
        self.calls.append(len(audio) // 16)
        return ["чужой первый чужой", "первый", "чужой второй чужой", "второй"][
            (len(self.calls) - 1) % 4
        ]

    def emit(self, job: mt._Job, event: dict[str, Any]) -> None:
        ipc.encode(event, role=mt.ROLE)
        self.events.append(copy.deepcopy(event))
        if event["type"] == "meeting.job.replace":
            job.replacements[event["seq"]] = (event["expected_revision"] + 1, True)

    def run(self) -> dict[str, Any]:
        job = mt._Job(copy.deepcopy(self.request))
        job.credits = 64
        job.delivered.set()
        self.state._run_job(job)
        while not self.state.outbox.empty():
            _, _, event = self.state.outbox.get_nowait()
            ipc.encode(event, role=mt.ROLE)
            self.events.append(event)
        return next(
            e
            for e in reversed(self.events)
            if e["type"] in {"meeting.job.done", "meeting.job.failed"}
        )

    def close(self) -> None:
        self.rig.close()


@pytest.fixture
def harness(tmp_path: Path) -> Iterator[Harness]:
    h = Harness(prepare(tmp_path))
    yield h
    h.close()


def test_shadow_seed_accepted_final_and_no_voice_features(
    harness: Harness, monkeypatch: Any
) -> None:
    h = harness
    before = published(h.disk)
    writes = []
    original = store.MeetingWriter.write

    def capture(owner: Any, name: str, document: Any) -> None:
        original(owner, name, document)
        writes.append((name, copy.deepcopy(document.data)))
        assert name in {WORK, CHECK}
        # Accepted is queued only once BOTH seed documents have fsynced.
        if len(writes) <= 2:
            assert h.state.outbox.empty()

    monkeypatch.setattr(store.MeetingWriter, "write", capture)
    done = h.run()
    assert done["type"] == "meeting.job.done" and done["diar_status"] == "done"
    assert not h.calls and not h.rig.engine.calls
    assert published(h.disk) == before
    seed = writes[0][1]
    assert seed["transcript_revision"] == 41
    assert not seed["labels_final"]
    assert all(not u["final"] and not u["multiple_speakers"] for u in seed["utterances"])
    assert {u["speaker"] for u in seed["utterances"]} == {"others"}
    assert writes[1][1]["asr"]["status"] == "done"
    accepted = next(e for e in h.events if e["type"] == "meeting.job.accepted")
    assert accepted["transcript_revision"] == 41
    final = h.disk.read(MID, WORK)
    assert final.labels_final and final.reconcile.status == PhaseStatus.DONE
    assert final.transcript_revision == done["transcript_revision"]
    assert not (h.disk.root / MID / CHECK).exists()
    for name, data in writes:
        cls = model.Transcript if name == WORK else model.Checkpoint
        cls.from_dict(json.loads(json.dumps(data)))
        for key in ("embeddings", "centroids", "voice_features"):
            poisoned = copy.deepcopy(data)
            poisoned[key] = [0.1] * 32
            with pytest.raises(ValueError):
                cls.from_dict(poisoned)
        serialized = json.dumps(data).lower()
        assert not any(
            k in serialized for k in ('"embeddings":', '"centroids":', '"voice_features":')
        )
    # Final shadow text shortcuts even with its checkpoint already removed.
    h.blocks.clear()
    h.events.clear()
    again = h.run()
    assert again["diar_status"] == "done" and not h.blocks and not h.calls
    assert h.disk.read(MID, WORK) == final


@pytest.mark.parametrize("value", ["unknown", None, True, False, 1, {}, []])
def test_operation_schema_rejects_bad_values(harness: Harness, value: Any) -> None:
    h = harness
    before = tree(h.disk)
    with pytest.raises(ipc.FrameError):
        contract.validate_message({**h.request, "operation": value}, mt.ROLE)
    assert tree(h.disk) == before and not h.calls


def test_old_role_schema_rejects_operation_and_nothing_changes(harness: Harness) -> None:
    h = harness
    old = copy.deepcopy(contract.SCHEMAS[mt.ROLE]["meeting.job.start"])
    del old["properties"]["operation"]
    before = tree(h.disk)
    with pytest.raises(SchemaInvalid):
        validate(h.request, old)
    assert contract.PROTOCOL == 1
    assert tree(h.disk) == before


@pytest.mark.parametrize(
    "case",
    [
        "no-descriptor",
        "no-operation",
        "generation",
        "descriptor-generation",
        "resume",
        "diarization",
        "models",
        "state",
        "audio",
        "cleanup",
        "target",
        "sha",
        "dir",
    ],
)
def test_preflight_refuses_before_lease_or_load(
    harness: Harness, monkeypatch: Any, case: str
) -> None:
    h = harness
    meeting = h.disk.read(MID, "meeting.json")
    if case == "no-descriptor":
        meeting.rediar = None
    elif case == "no-operation":
        del h.request["operation"]
    elif case == "generation":
        h.request["meeting_generation"] = 2
    elif case == "descriptor-generation":
        meeting.meeting_generation = 2
        h.request["meeting_generation"] = 2
    elif case in {"resume", "diarization"}:
        h.request[case] = False
    elif case == "models":
        del h.request["diar_models"]
    elif case == "state":
        meeting.state = MeetingState.TRANSCRIBING
    elif case == "audio":
        meeting.audio.present = False
    elif case == "cleanup":
        meeting.cleanup_pending = True
    elif case == "target":
        for p in (h.disk.root / MID / "audio").glob("far-*.wav"):
            p.unlink()
    elif case == "sha":
        meeting.rediar.base_sha256 = "a" * 64
    elif case == "dir":
        h.request["dir"] = str(h.disk.root / MID / "audio")
    # Deliberately malformed cross-field state is read through the real schema.
    put(h.disk, "meeting.json", meeting.to_dict())
    before = tree(h.disk)
    monkeypatch.setattr(
        h.state, "_ensure_model", lambda *a, **k: pytest.fail("preflight loaded model")
    )
    monkeypatch.setattr(h.disk, "writer", lambda *a, **k: pytest.fail("preflight took lease"))
    assert h.run()["code"] == "io"
    assert not any(e["type"] == "meeting.job.accepted" for e in h.events)
    assert tree(h.disk) == before and not h.calls


@pytest.mark.parametrize("failure", ["crash", "memory", "deadline", "load", "map", "exit"])
def test_redo_diar_failure_never_publishes_fallback(
    harness: Harness, monkeypatch: Any, failure: str
) -> None:
    h = harness
    monkeypatch.setattr(mt, "DIAR_BLOCK_MS", 25000)
    monkeypatch.setattr(mt, "DIAR_PAUSE_SEARCH_MS", 0)
    original = h.factory

    def factory(*args: Any, **kwargs: Any) -> FakeDiar:
        role = original(*args, **kwargs)
        block = role.block

        def fail_block(audio: Any, index: int, offset: int) -> list[list[Any]]:
            if index == 1:
                raise {"memory": MemoryError, "deadline": TimeoutError}.get(failure, OSError)()
            return block(audio, index, offset)

        if failure in {"crash", "memory", "deadline"}:
            role.block = fail_block  # type: ignore[method-assign]
        elif failure == "load":
            role.load = lambda models: (_ for _ in ()).throw(ValueError())  # type: ignore[method-assign]
        elif failure == "map":
            role.finish = lambda: {"label_map": {}, "speakers_total": 0}  # type: ignore[method-assign]
        else:

            def close(*, completed: bool = False) -> bool:
                role.alive = False
                return False

            role.close = close  # type: ignore[method-assign]
        return role

    h.state.diar_factory = factory
    before = published(h.disk)
    done = h.run()
    assert done["type"] == "meeting.job.done" and done["diar_status"] == "failed"
    assert published(h.disk) == before and not h.calls
    assert not h.disk.read(MID, WORK).labels_final
    assert not any(e.get("final") for e in h.events if e["type"] == "meeting.job.labels")
    assert all(not role.alive for role in h.roles)


@pytest.mark.parametrize("name", ["transcript.json", "checkpoint.json"])
def test_store_refuses_published_write_during_redo(harness: Harness, name: str) -> None:
    before = published(harness.disk)
    cls = model.Transcript if name == "transcript.json" else model.Checkpoint
    with pytest.raises(store.MeetingStoreError):
        harness.disk.write(
            MID, name, cls(meeting_id=MID, meeting_generation=1), writer=store.Writer.TRANSCRIBE
        )
    assert published(harness.disk) == before


@pytest.mark.parametrize("name", [WORK, CHECK])
@pytest.mark.parametrize("code", [errno.ENOSPC, errno.EIO, errno.EACCES])
def test_redo_io_failure_keeps_published(
    harness: Harness, monkeypatch: Any, name: str, code: int
) -> None:
    h = harness
    before = published(h.disk)
    original = store.MeetingWriter.write

    def fail(owner: Any, filename: str, document: Any) -> None:
        if filename == name:
            raise OSError(code, "synthetic write failure")
        original(owner, filename, document)

    monkeypatch.setattr(store.MeetingWriter, "write", fail)
    assert h.run()["code"] == "io"
    assert published(h.disk) == before and not h.calls
    assert not any(e["type"] == "meeting.job.accepted" for e in h.events)


def interrupt(h: Harness, monkeypatch: Any, *, d5: bool) -> None:
    """Leave a real compatible pair before D5 or at its first reservation."""
    original = store.MeetingWriter.write

    def write(owner: Any, name: str, document: Any) -> None:
        original(owner, name, document)
        if name == CHECK and (document.document.reconcile.inflight_id if d5 else True):
            raise Crash

    with monkeypatch.context() as patch:
        patch.setattr(store.MeetingWriter, "write", write)
        with pytest.raises(Crash):
            h.run()
    h.close()


@pytest.mark.parametrize("started", [False, True])
@pytest.mark.parametrize(
    "damage", ["config", "model", "algorithm", "manifest", "missing", "corrupt", "ahead"]
)
def test_incompatible_pair_reseeds_only_before_d5(
    tmp_path: Path, monkeypatch: Any, started: bool, damage: str
) -> None:
    disk = prepare(tmp_path)
    first = Harness(disk, split=started)
    interrupt(first, monkeypatch, d5=started)
    text = disk.read(MID, WORK)
    cp = disk.read(MID, CHECK)
    previous = published(disk)
    if damage == "missing":
        (disk.root / MID / CHECK).unlink()
    elif damage == "corrupt":
        put(disk, CHECK, b'{"broken"')
    else:
        if damage == "config":
            cp.processing_config_sha256 = "a" * 64
        elif damage == "model":
            cp.model.revision = "changed"
        elif damage == "algorithm":
            cp.diar.algo = "changed"
            cp.processing_config_sha256 = "b" * 64
        elif damage == "manifest":
            cp.input_manifest_sha256 = "a" * 64
        else:
            cp.transcript_revision += 1
        put(disk, CHECK, cp.to_dict())
    stable = tree(disk)
    second = Harness(store.MeetingStore(disk.root), split=started)
    try:
        outcome = second.run()
        assert published(disk) == previous
        if started:
            assert outcome["type"] == "meeting.job.failed" and outcome["code"] == "io"
            assert tree(disk) == stable and not second.calls and not second.blocks
            assert disk.read(MID, WORK).reconcile.used_ms == text.reconcile.used_ms == 5200
        else:
            assert outcome["diar_status"] == "done" and second.blocks
            assert disk.read(MID, WORK).transcript_revision > text.transcript_revision
            accepted = next(e for e in second.events if e["type"] == "meeting.job.accepted")
            assert accepted["transcript_revision"] == text.transcript_revision + 1
    finally:
        second.close()


def test_work_names_survive_descriptor_cancellation(harness: Harness, monkeypatch: Any) -> None:
    h = harness
    before = published(h.disk)
    original = store.MeetingWriter.write
    writes = []

    def cancel(owner: Any, name: str, document: Any) -> None:
        writes.append(name)
        original(owner, name, document)
        if name == WORK:
            meeting = h.disk.read(MID, "meeting.json")
            meeting.rediar = None
            put(h.disk, "meeting.json", meeting.to_dict())

    monkeypatch.setattr(store.MeetingWriter, "write", cancel)
    assert h.run()["code"] == "io"
    assert writes == [WORK, CHECK]
    assert published(h.disk) == before


@pytest.mark.parametrize("bad", ["absent", "unverified", "revoked", "missing-sha", "wrong-sha"])
def test_pair_trust_failures_preserve_published(
    harness: Harness, tmp_path: Path, monkeypatch: Any, bad: str
) -> None:
    h = harness
    before = published(h.disk)
    if bad in {"absent", "unverified", "revoked"}:
        if bad == "unverified":
            pair = diar_models.installed_diar_models(
                catalog_provider=lambda: Catalog(serial=0, trust_epoch=0, entries=(), revoked=())
            )
        else:
            # The established signed-catalog gate supplies no pair on missing/revoked models.
            monkeypatch.setattr(diarization, "trusted_diarization_models", lambda *a, **k: None)
            pair = diar_models.installed_diar_models()
        h.state.diar_models_provider = lambda: pair
        assert pair is None
        h.request.pop("diar_models")
    else:
        dirs = [tmp_path / "seg", tmp_path / "emb"]
        for path, names in zip(dirs, diar.MODEL_FILES, strict=True):
            path.mkdir()
            (path / names[0]).write_bytes(b"\x08\x01")
        h.request["diar_models"] = {"segmentation_dir": str(dirs[0]), "embedder_dir": str(dirs[1])}
        if bad == "wrong-sha":
            h.request["diar_models"].update(
                segmentation_sha256="a" * 64,
                embedder_sha256=hashlib.sha256(b"\x08\x01").hexdigest(),
            )
        # Admission in this snapshot-validation test deliberately supplies the
        # malformed pair; checked_models must still reject it inside the role.
        h.state.diar_models_provider = lambda: dict(h.request["diar_models"])
        original = h.factory

        def factory(*args: Any, **kwargs: Any) -> FakeDiar:
            role = original(*args, **kwargs)

            def load(models: dict[str, str]) -> None:
                with diar.checked_models(
                    [models["segmentation_dir"], models["embedder_dir"]],
                    [models.get("segmentation_sha256"), models.get("embedder_sha256")],
                ):
                    pytest.fail("untrusted snapshots accepted")

            role.load = load  # type: ignore[method-assign]
            return role

        h.state.diar_factory = factory
    result = h.run()
    if bad in {"absent", "unverified", "revoked"}:
        assert result["code"] == "io"
        assert not (h.disk.root / MID / WORK).exists()
    else:
        assert result["diar_status"] == "failed"
        assert not h.disk.read(MID, WORK).labels_final
    assert published(h.disk) == before and not h.calls and not h.rig.engine.calls


def trace_run(
    h: Harness, monkeypatch: Any, crash_at: int | None = None
) -> list[tuple[str, str, str]]:
    """Trace BEFORE/AFTER both fsyncs and replace of every working publication."""
    trace: list[tuple[str, str, str]] = []
    active: list[str | None] = [None]
    write, fsync, replace = store.MeetingWriter.write, os.fsync, os.replace
    remove = store.MeetingWriter.remove

    def boundary(op: str, when: str) -> None:
        if active[0] is not None:
            trace.append((active[0], op, when))
            if len(trace) - 1 == crash_at:
                raise Crash

    def traced_write(owner: Any, name: str, document: Any) -> None:
        active[0] = name
        try:
            write(owner, name, document)
        finally:
            active[0] = None

    def traced_remove(owner: Any, name: str) -> None:
        active[0] = name
        try:
            boundary("remove", "before")
            remove(owner, name)
            boundary("remove", "after")
        finally:
            active[0] = None

    def traced_fsync(fd: int) -> None:
        boundary("fsync", "before")
        fsync(fd)
        boundary("fsync", "after")

    def traced_replace(*args: Any, **kwargs: Any) -> None:
        boundary("replace", "before")
        replace(*args, **kwargs)
        boundary("replace", "after")

    with monkeypatch.context() as patch:
        patch.setattr(store.MeetingWriter, "write", traced_write)
        patch.setattr(store.MeetingWriter, "remove", traced_remove)
        patch.setattr(os, "fsync", traced_fsync)
        patch.setattr(os, "replace", traced_replace)
        if crash_at is None:
            assert h.run()["diar_status"] == "done"
        else:
            with pytest.raises(Crash):
                h.run()
            assert len(trace) == crash_at + 1
    return trace


# Fixed collection size is checked against the live trace: new boundaries must
# fail the test rather than silently fall outside the parametrized coverage.
CRASH_BOUNDARY_COUNT = 148


@dataclass(frozen=True)
class CrashReference:
    template: Path
    trace: tuple[tuple[str, str, str], ...]


@pytest.fixture(scope="module")
def crash_reference(
    tmp_path_factory: pytest.TempPathFactory, synthetic_diar_models: None
) -> Iterator[CrashReference]:
    directory = tmp_path_factory.mktemp("rediar-crash-reference")
    template = prepare(directory / "template")
    template_before = tree(template)
    # copytree makes independent private files, including the empty lock files;
    # no live writer/cache/model or open file description is shared by cases.
    baseline_root = directory / "baseline" / "meetings"
    shutil.copytree(template.root, baseline_root)
    baseline = Harness(store.MeetingStore(baseline_root), split=True)
    try:
        with pytest.MonkeyPatch.context() as patch:
            trace = trace_run(baseline, patch)
        assert baseline.disk.read(MID, WORK).reconcile.used_ms == 5200
        assert len(baseline.calls) == 4
    finally:
        baseline.close()
    # The baseline precedes function-scoped meeting_threads; check its cleanup
    # explicitly instead of losing that guard when moving it to module scope.
    assert not baseline.state.thread.is_alive()
    assert len(trace) == CRASH_BOUNDARY_COUNT
    (directory / "boundaries.json").write_text(json.dumps(trace))
    yield CrashReference(template.root, tuple(trace))
    assert tree(template) == template_before


def test_redo_durable_boundary_trace(crash_reference: CrashReference) -> None:
    trace = crash_reference.trace
    assert {row[0] for row in trace} == {WORK, CHECK}
    assert {row[1:] for row in trace} == {
        (op, when) for op in ("fsync", "replace", "remove") for when in ("before", "after")
    }
    assert set(trace) == {
        (name, op, when)
        for name in (WORK, CHECK)
        for op in ("fsync", "replace")
        for when in ("before", "after")
    } | {(CHECK, "remove", when) for when in ("before", "after")}


@pytest.mark.parametrize(
    "index",
    range(CRASH_BOUNDARY_COUNT),
    ids=[f"boundary-{index:03d}" for index in range(CRASH_BOUNDARY_COUNT)],
)
def test_redo_resume_crash_at_each_durable_boundary(
    tmp_path: Path, monkeypatch: Any, crash_reference: CrashReference, index: int
) -> None:
    trace = crash_reference.trace
    boundary = trace[index]
    shutil.copytree(crash_reference.template, tmp_path / "meetings")
    disk = store.MeetingStore(tmp_path / "meetings")
    first = Harness(disk, split=True)
    before = published(disk)
    try:
        assert trace_run(first, monkeypatch, index)[-1] == boundary
    finally:
        first.close()
    work = disk.read(MID, WORK) if (disk.root / MID / WORK).exists() else None
    reserved = work is not None and work.reconcile.used_ms > 0
    cp = disk.read(MID, CHECK) if (disk.root / MID / CHECK).exists() else None
    second = Harness(store.MeetingStore(disk.root), split=True)
    try:
        done = second.run()
        assert done["type"] == "meeting.job.done" and done["diar_status"] == "done", (
            index,
            boundary,
            done,
        )
        final = disk.read(MID, WORK)
        assert final.labels_final and final.reconcile.status == PhaseStatus.DONE
        assert final.reconcile.used_ms == 5200 and final.reconcile.budget_ms == 6300
        assert final.reconcile.done_ids == ["source"]
        assert final.reconcile.inflight_id is None and final.reconcile.pending is None
        assert published(disk) == before
        if reserved:
            assert not second.calls and not second.blocks, (index, boundary)
        else:
            assert len(second.calls) == 4
            if cp is not None and cp.diar.status == PhaseStatus.DONE:
                assert not second.blocks
            else:
                assert second.blocks == [0]  # interrupted diar restarts the entire track
        if work is not None:
            assert final.transcript_revision >= work.transcript_revision
    finally:
        second.close()


@pytest.mark.parametrize("revision", [3, 80])
def test_seed_revision_continues_maximum_and_resets_me(
    tmp_path: Path, monkeypatch: Any, revision: int
) -> None:
    disk = prepare(tmp_path)
    first = Harness(disk)
    interrupt(first, monkeypatch, d5=False)
    text = disk.read(MID, WORK)
    text.transcript_revision = revision
    put(disk, WORK, text.to_dict())
    cp = disk.read(MID, CHECK)
    cp.transcript_revision = revision
    cp.processing_config_sha256 = "c" * 64
    put(disk, CHECK, cp.to_dict())
    # Include the independent microphone in the immutable published source.
    source = disk.read(MID, "transcript.json")
    source.utterances.insert(
        1,
        model.Utterance(
            "mic-u",
            seq=1,
            track="mic",
            start_ms=1500,
            end_ms=2000,
            text="я",
            speaker="S8",
            final=True,
            multiple_speakers=True,
        ),
    )
    for seq, row in enumerate(source.utterances):
        row.seq = seq
    put(disk, "transcript.json", source.to_dict())
    meeting = disk.read(MID, "meeting.json")
    meeting.rediar.base_sha256 = disk.transcript_state(MID).digest
    put(disk, "meeting.json", meeting.to_dict())
    h = Harness(disk)
    original = store.MeetingWriter.write
    seeds: list[model.Transcript] = []

    def capture(owner: Any, name: str, doc: Any) -> None:
        if name == WORK and not seeds:
            seeds.append(copy.deepcopy(doc.document))
        original(owner, name, doc)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(store.MeetingWriter, "write", capture)
            assert h.run()["diar_status"] == "done"
        assert seeds[0].transcript_revision == max(40, revision) + 1
        assert [u.speaker for u in seeds[0].utterances] == ["others", "me", "others", "others"]
        assert all(not u.final and not u.multiple_speakers for u in seeds[0].utterances)
    finally:
        h.close()


@pytest.mark.parametrize("damage", ["status", "budget", "used", "inflight", "receipt"])
def test_d5_inconsistent_embedded_state_fails_without_reset(
    tmp_path: Path, monkeypatch: Any, damage: str
) -> None:
    disk = prepare(tmp_path)
    first = Harness(disk, split=True)
    interrupt(first, monkeypatch, d5=True)
    text = disk.read(MID, WORK)
    cp = disk.read(MID, CHECK)
    if damage == "status":
        text.reconcile.status = cp.reconcile.status = PhaseStatus.PENDING
    else:
        # Text one revision ahead is legitimate only if it retains the reservation.
        text.transcript_revision += 1
        if damage == "budget":
            text.reconcile.budget_ms = 6000
        elif damage == "used":
            text.reconcile.used_ms = 0
        elif damage == "inflight":
            text.reconcile.inflight_id = None
        else:
            cp.reconcile.done_ids = ["lost-receipt"]
    put(disk, WORK, text.to_dict())
    put(disk, CHECK, cp.to_dict())
    stable = tree(disk)
    h = Harness(store.MeetingStore(disk.root), split=True)
    try:
        assert h.run()["code"] == "io"
        assert tree(disk) == stable and not h.calls and not h.blocks
    finally:
        h.close()


def final_pair(h: Harness) -> tuple[model.Transcript, model.Checkpoint]:
    text = h.disk.read(MID, "transcript.json")
    text.meeting_generation = 1
    text.transcript_revision = 50
    text.reconcile = model.ReconcileProgress(
        status=PhaseStatus.DONE,
        used_ms=5200,
        budget_ms=6300,
        speech_ms=42000,
        done_ids=["source"],
        replaced_ids=["source"],
        failed=True,
    )
    checkpoint = model.Checkpoint(
        meeting_id=MID,
        meeting_generation=1,
        transcript_revision=50,
        asr=model.AsrProgress(status=PhaseStatus.DONE),
        diar=model.DiarProgress(status=PhaseStatus.DONE),
        reconcile=copy.deepcopy(text.reconcile),
    )
    return text, checkpoint


@pytest.mark.parametrize(
    "damage",
    ["ahead", "used", "budget", "speech", "done_ids", "replaced_ids", "pristine", "corrupt"],
)
def test_final_rediar_rejects_contradictory_checkpoint_without_writes(
    harness: Harness, monkeypatch: Any, damage: str
) -> None:
    h = harness
    text, checkpoint = final_pair(h)
    checkpoint.transcript_revision = 49
    if damage == "ahead":
        checkpoint.transcript_revision = 51
        checkpoint.reconcile.used_ms = 6200
    elif damage == "used":
        checkpoint.reconcile.used_ms = 6200
    elif damage == "budget":
        checkpoint.reconcile.budget_ms = 6200
    elif damage == "speech":
        checkpoint.reconcile.speech_ms = 50000
    elif damage in {"done_ids", "replaced_ids"}:
        setattr(checkpoint.reconcile, damage, ["lost-receipt"])
    elif damage == "pristine":
        checkpoint.reconcile = model.ReconcileProgress()
    put(h.disk, WORK, text.to_dict())
    put(h.disk, CHECK, b'{"broken"' if damage == "corrupt" else checkpoint.to_dict())
    before = tree(h.disk)

    def forbidden(*args: Any, **kwargs: Any) -> None:
        pytest.fail("a contradictory final pair must not be written or removed")

    monkeypatch.setattr(store.MeetingWriter, "write", forbidden)
    monkeypatch.setattr(store.MeetingWriter, "remove", forbidden)
    result = h.run()
    assert result["type"] == "meeting.job.failed" and result["code"] == "io"
    assert not any(e["type"] in {"meeting.job.done", "meeting.job.accepted"} for e in h.events)
    assert tree(h.disk) == before
    assert not h.calls and not h.blocks and not h.rig.engine.calls


@pytest.mark.parametrize("checkpoint_kind", ["absent", "same", "lagging"])
def test_final_rediar_fast_path_accepts_consistent_checkpoint(
    harness: Harness, monkeypatch: Any, checkpoint_kind: str
) -> None:
    h = harness
    text, checkpoint = final_pair(h)
    put(h.disk, WORK, text.to_dict())
    if checkpoint_kind == "lagging":
        checkpoint.transcript_revision = 49
        checkpoint.reconcile.status = PhaseStatus.RUNNING
        checkpoint.reconcile.done_ids = []
        checkpoint.reconcile.replaced_ids = []
        checkpoint.reconcile.inflight_id = "source"
        checkpoint.reconcile.failed = False
    if checkpoint_kind != "absent":
        put(h.disk, CHECK, checkpoint.to_dict())
    before = (h.disk.root / MID / WORK).read_bytes()
    original = published(h.disk)

    def forbidden(*args: Any, **kwargs: Any) -> None:
        pytest.fail("a consistent final pair must not be recomputed or rewritten")

    monkeypatch.setattr(store.MeetingWriter, "write", forbidden)
    result = h.run()
    assert result["type"] == "meeting.job.done" and result["diar_status"] == "done"
    assert result["transcript_revision"] == 50
    assert (h.disk.root / MID / WORK).read_bytes() == before
    assert published(h.disk) == original
    assert not (h.disk.root / MID / CHECK).exists()
    assert not h.calls and not h.blocks and not h.rig.engine.calls
