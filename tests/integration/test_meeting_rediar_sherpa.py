"""M7: on-disk publication through MeetingJobs and the real sherpa diar role.

Absent dependencies/models/corpus skip; denied IPC/ptrace fails honestly. Each
case has one shared 75s work deadline (plus bounded process/writer cleanup).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import ExitStack
from datetime import datetime
from io import StringIO
from pathlib import Path

import pytest

# Keep engine-only collection usable without the optional model-catalog parser.
pytest.importorskip("yaml", reason="Для каталога моделей нужен PyYAML")

from tools.meeting_diar_check import model_paths  # noqa: E402
from tools.meeting_rediar_check import (  # noqa: E402
    CHECK,
    WORK,
    LoggedRoleProcess,
    QueueRun,
    RoleSpawn,
    prepared_queue,
    role_state,
    trace_command,
)
from tools.meeting_transcribe_diar_check import isolated_diar_xdg, prepare_meeting  # noqa: E402

from astra_voice.core import paths  # noqa: E402
from astra_voice.meetings.model import Meeting, Speaker, SpeakerMap  # noqa: E402
from astra_voice.meetings.speakers import speaker_label  # noqa: E402
from astra_voice.meetings.store import MeetingStore, Writer  # noqa: E402
from astra_voice.models.diarization import catalog_diarization_hashes  # noqa: E402
from astra_voice.models.store import ModelStore  # noqa: E402
from astra_voice.security.files import private_directory  # noqa: E402
from astra_voice.worker.meeting_transcribe import _Job, _Reader  # noqa: E402
from astra_voice.worker.supervisor import worker_command  # noqa: E402
from helpers.meeting_trust import RAW_MODEL, SignedDiarPair, SigningUnavailable  # noqa: E402

pytestmark = [pytest.mark.engine, pytest.mark.usefixtures("filesystem_euid")]


@pytest.fixture(scope="module")
def inputs() -> tuple[dict[str, str], Path]:
    pytest.importorskip("sherpa_onnx", reason="Для диаризации нужен sherpa-onnx")
    names = ("ASTRA_VOICE_DIAR_MODELS", "ASTRA_VOICE_DIAR_CORPUS")
    if any(not os.environ.get(name) for name in names):
        pytest.skip("Задайте ASTRA_VOICE_DIAR_MODELS и ASTRA_VOICE_DIAR_CORPUS")
    models, corpus = (Path(os.environ[name]) for name in names)
    try:
        files = model_paths(models)
    except OSError:
        pytest.skip("Неполный комплект моделей диаризации")
    wav = corpus / "dion/far.wav"
    if not wav.is_file():
        pytest.skip("Нет dion/far.wav")
    return {
        "segmentation_dir": str(files[0].resolve(strict=True).parent),
        "embedder_dir": str(files[1].resolve(strict=True).parent),
        **catalog_diarization_hashes(),
    }, wav


@pytest.fixture
def queue(inputs: tuple[dict[str, str], Path], tmp_path: Path) -> Iterator[QueueRun]:
    models, wav = inputs
    started = time.monotonic()
    store, mid = prepare_meeting(tmp_path, {"far": wav}, duration_s=30)
    with ExitStack() as stack:
        try:
            run = stack.enter_context(prepared_queue(store, mid, models, deadline=started + 75))
        except SigningUnavailable as error:
            pytest.skip(f"sandbox cannot start temporary gpg-agent: {error}")
        yield run
    assert time.monotonic() - started < 90, "Тест превысил бюджет 90 с"


@pytest.fixture
def plain_queue(inputs: tuple[dict[str, str], Path], tmp_path: Path) -> Iterator[QueueRun]:
    models, wav = inputs
    started = time.monotonic()
    store, mid = prepare_meeting(tmp_path, {"far": wav}, duration_s=30)
    run = QueueRun(store, mid, models, deadline=started + 75)
    try:
        yield run
    finally:
        run.close()
        assert time.monotonic() - started < 90, "Тест превысил бюджет 90 с"


def initial(run: QueueRun, *, failed: bool = False) -> None:
    run.jobs.transcribe(run.mid)
    if failed:
        pid, _ = run.child_at_boundary(redo=False)
        os.kill(pid, signal.SIGKILL)
    run.until(
        lambda: not run.jobs.busy and run.jobs._pending == 0 and run.meeting().state == "done"
    )
    meeting = run.meeting()
    assert meeting.transcription.diar_status == ("failed" if failed else "done")
    text = run.transcript()
    assert text.labels_final and text.utterances
    if failed:
        assert {u.speaker for u in text.utterances} == {"others"}
        assert speaker_label(meeting, "others") == "Собеседники"
    else:
        labels = {u.speaker for u in text.utterances if u.speaker.startswith("S")}
        assert labels
        run.store.write(
            run.mid,
            "speakers.json",
            SpeakerMap(
                labels={label: Speaker(name=f"Имя {label}") for label in labels},
                transcript_generation=text.meeting_generation,
            ),
            writer=Writer.GUI,
        )
    assert meeting.audio.present
    assert (run.store.root / run.mid / "audio/far-0000.wav").is_file()


def assert_success(run: QueueRun, old: Meeting, before: tuple[bytes, bytes]) -> None:
    meeting, text = run.meeting(), run.transcript()
    assert meeting.meeting_generation == old.meeting_generation + 1
    old_generation = old.transcript_generation
    if old_generation is None:
        old_generation = old.meeting_generation
    assert text.meeting_generation == old_generation + 1
    assert meeting.transcript_generation is None  # normal published result uses meeting_generation
    assert run.published()[0] != before[0]
    assert text.labels_final and text.utterances
    names = run.store.read(run.mid, "speakers.json")
    assert isinstance(names, SpeakerMap)
    assert names.labels == {} and names.transcript_generation == text.meeting_generation
    assert all(
        speaker_label(meeting, u.speaker, names).startswith("Спикер ") for u in text.utterances
    )
    assert meeting.transcription.diar_status == "done" and meeting.rediar is None
    assert not any((run.store.root / run.mid / name).exists() for name in (WORK, CHECK))
    assert meeting.audio.present and meeting.audio.retain_days == 7
    assert not meeting.cleanup_pending
    assert meeting.transcription.finished_at is not None
    assert old.transcription.finished_at is not None
    assert datetime.fromisoformat(meeting.transcription.finished_at) > datetime.fromisoformat(
        old.transcription.finished_at
    )
    assert (run.store.root / run.mid / "audio/far-0000.wav").is_file()


def test_named_split_rediarize_publishes_new_generation(queue: QueueRun) -> None:
    initial(queue)
    old, before = queue.meeting(), queue.published()
    count = len({u.speaker for u in queue.transcript().utterances})
    path = queue.store.root / queue.mid / "transcript.json"
    with path.open("rb") as original:
        queue.start_redo()
        assert queue.published() == before
        queue.finish_redo()
        assert_success(queue, old, before)
        assert original.read() == before[0]  # old inode stays intact across publication
        assert path.stat().st_ino != os.fstat(original.fileno()).st_ino
    assert abs(len({u.speaker for u in queue.transcript().utterances}) - count) <= 1


def test_failed_split_rediarize_starts_audio_retention(queue: QueueRun) -> None:
    initial(queue, failed=True)
    old, before = queue.meeting(), queue.published()
    assert old.audio.retain_days == 7  # stored policy is held while diar_status=failed
    queue.start_redo()
    queue.finish_redo()
    assert_success(queue, old, before)


def test_kill9_rediar_role_recovers_from_checkpoint(
    queue: QueueRun,
    inputs: tuple[dict[str, str], Path],
) -> None:
    initial(queue)
    old, before = queue.meeting(), queue.published()
    queue.start_redo()
    pid, checkpoint = queue.child_at_boundary()
    assert checkpoint.meeting_generation == old.meeting_generation + 1
    assert checkpoint.reconcile.used_ms == 0  # diar precedes D5; no spent D5 is fabricated
    assert queue.published() == before
    os.kill(pid, signal.SIGKILL)
    # Do not deliver the terminal failure to the old queue. A program exit
    # preserves the durable redo descriptor and pair for startup recovery.
    queue.close()
    assert queue.published() == before
    disk = queue.store.read(queue.mid, CHECK)
    assert disk == checkpoint
    resumed = QueueRun(
        queue.store,
        queue.mid,
        inputs[0],
        deadline=queue.deadline,
        trusted_pair=queue.trusted_pair,
    )
    try:
        resumed.jobs.start_retention()
        resumed.jobs.recover()
        resumed.watch_published = before
        resumed.finish_redo()
        assert_success(resumed, old, before)
        final = resumed.transcript().reconcile
        assert checkpoint.reconcile.budget_ms == 0  # budget is established after diar
        assert final.budget_ms == final.speech_ms * 15 // 100
        assert final.used_ms >= checkpoint.reconcile.used_ms
        assert final.used_ms <= final.budget_ms
        assert set(checkpoint.reconcile.done_ids) <= set(final.done_ids)
        assert len(final.done_ids) == len(set(final.done_ids))
        assert any(e["type"] == "meeting.job.accepted" for e in resumed.events)
    finally:
        resumed.close()


def test_cancel_rediar_keeps_published_bytes_and_audio(queue: QueueRun) -> None:
    initial(queue)
    old, before = queue.meeting(), queue.published()
    queue.start_redo()
    pid, _ = queue.child_at_boundary()
    os.kill(pid, 0)
    assert queue.published() == before
    queue.jobs.cancel(queue.mid)
    queue.until(
        lambda: (
            queue.mid in queue.jobs.redo_results
            and not queue.jobs.busy
            and queue.jobs._pending == 0
        )
    )
    assert queue.jobs.redo_results[queue.mid] == "cancelled"
    assert queue.published() == before
    meeting = queue.meeting()
    assert meeting.rediar is None and meeting.audio == old.audio
    assert meeting.transcription.finished_at == old.transcription.finished_at
    assert not any((queue.store.root / queue.mid / name).exists() for name in (WORK, CHECK))
    assert (queue.store.root / queue.mid / "audio/far-0000.wav").is_file()
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_strace_rediar_role_writes_only_contract_outputs(queue: QueueRun, tmp_path: Path) -> None:
    if shutil.which("strace") is None:
        pytest.skip("strace не установлен")
    from test_meeting_diar_sherpa import trace_violations

    initial(queue)
    old, before = queue.meeting(), queue.published()
    # A fresh supervisor reads the trace option only for the redo process.
    queue.close()
    trace = tmp_path / "rediar.trace"
    models = queue.jobs.diar_models()
    assert models is not None
    traced = QueueRun(
        queue.store,
        queue.mid,
        models,
        trace=trace,
        deadline=queue.deadline,
        trusted_pair=queue.trusted_pair,
    )
    try:
        traced.start_redo()
        traced.finish_redo()
        assert_success(traced, old, before)
        text = trace.read_text()
        assert "openat(" in text and "write(" in text, "Пустая трасса не доказывает контракт"
        assert trace_violations(text) == []
    finally:
        traced.close()


def test_role_fixture_root_inventory_and_io_journal(plain_queue: QueueRun, tmp_path: Path) -> None:
    """Exercise the child factory/preflight/PCM path without any socket or role fake."""
    queue = plain_queue
    journal = StringIO()
    state = role_state(store_root=queue.store.root, journal=journal)
    request = {
        "meeting_id": queue.mid,
        "meeting_generation": queue.meeting().meeting_generation,
        "dir": str(queue.store.root / queue.mid),
        "tracks": ["mic", "far"],
    }
    try:
        job = _Job(request)
        state._check_dir(job)
        assert state.store.read(queue.mid, "meeting.json") == queue.meeting()
        with private_directory(
            queue.store.root / queue.mid / "audio", private_from=queue.store.root
        ) as audio:
            reader = _Reader(audio)
            try:
                spans, damaged, _, duration = state._inventory(job, reader)
                assert duration == 30_000 and len(spans) == 1 and not damaged
                samples = reader.read(spans[0], spans[0].first, spans[0].end)
                assert len(samples) == 30 * 16_000
            finally:
                reader.close()
        bad = _Job({**request, "dir": str(tmp_path / "outside-meetings")})
        state._run_job(bad)
        events = state.poll(128)
        assert any(e["type"] == "meeting.job.failed" and e["code"] == "io" for e in events)
        assert "_check_dir" in journal.getvalue() and "_Failed: io" in journal.getvalue()
        command = queue.command(9)
        assert command[-2] == str(queue.store.root)
        descriptor = os.open(command[-1], os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(journal.getvalue())
        assert "_check_dir" in queue.diagnostics() and "_Failed: io" in queue.diagnostics()
    finally:
        state.cleanup()


def test_trace_spawn_keeps_python_flags_fds_and_stderr(tmp_path: Path) -> None:
    """Check argv ordering and actual spawn diagnostics without needing ptrace/IPC sends."""
    command = worker_command(9, role="meeting-diar")
    command.insert(1, "-B")  # the exact transformation performed by DiarProcess
    traced = trace_command(command, tmp_path / "trace")
    python_index = traced.index(sys.executable)
    assert traced[python_index:] == command
    assert "-B" not in traced[:python_index]
    assert {"-f", "-qq", "-yy"} <= set(traced[:python_index])

    # Genuine descriptors and process, no fake role exit. This process only
    # checks spawn options and emits a known stderr diagnostic, not model output.
    lease = os.open(tmp_path / "lease", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    parent, child = socket.socketpair()
    environment = dict(os.environ)
    environment.pop("HOME", None)
    environment["TMPDIR"] = "/nonexistent/astra-voice-diar"
    script = (
        "import os, stat, sys; "
        "assert os.fstat(int(sys.argv[1])).st_ino == int(sys.argv[2]); "
        "assert stat.S_ISSOCK(os.fstat(int(sys.argv[3])).st_mode); "
        "assert os.environ.get('HOME') is None; "
        "assert os.environ['TMPDIR'] == '/nonexistent/astra-voice-diar'; "
        "assert os.getsid(0) == os.getpid(); "
        "sys.stderr.write('role-stderr-proof\\n'); sys.exit(2)"
    )
    try:
        with (tmp_path / "journal").open("w+", encoding="utf-8") as journal:
            process = RoleSpawn(journal, None, journal_path=tmp_path / "journal").Popen(
                [
                    sys.executable,
                    "-I",
                    "-B",
                    "-c",
                    script,
                    str(lease),
                    str(os.fstat(lease).st_ino),
                    str(child.fileno()),
                ],
                pass_fds=(child.fileno(), lease),
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            try:
                assert process.wait(timeout=5) == 2
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
            journal.seek(0)
            text = journal.read()
            assert "diar_exit=2" in text and "role-stderr-proof" in text
            assert process.stderr_path.stat().st_mode & 0o777 == 0o600
    finally:
        os.close(lease)
        parent.close()
        child.close()


def test_installed_corpus_pair_layout_without_signing(
    inputs: tuple[dict[str, str], Path],
    tmp_path: Path,
) -> None:
    """Real install/metadata wiring can be checked even when gpg-agent is denied."""
    models, _ = inputs
    names = ("model.onnx", "3dspeaker_speech_campplus_sv_zh_en_16k-common_advanced.onnx")
    files = {
        name: Path(models[key]) / name
        for name, key in zip(
            names,
            ("segmentation_dir", "embedder_dir"),
            strict=True,
        )
    }
    variables = (
        "XDG_DATA_HOME",
        "XDG_CONFIG_HOME",
        "XDG_CACHE_HOME",
        "XDG_STATE_HOME",
        "XDG_RUNTIME_DIR",
    )
    previous = {name: os.environ.get(name) for name in variables}
    with isolated_diar_xdg(tmp_path / "xdg"):
        pair = SignedDiarPair.stage(
            tmp_path / "unsigned",
            model_files=files,
            model_root=paths.model_store_dir_path(),
        )
        pointer = json.loads((pair.model_root / ".diarization/current.json").read_bytes())
        generation = pair.model_root / ".diarization" / pointer["generation"]
        records = ModelStore(generation).records(include_diarization=True)
        document = json.loads((pair.root / "catalog/catalog.json").read_bytes())
        entries = {e["id"]: e for e in document["models"]}
        assert len(records) == 2
        assert {tuple(row) for row in pointer["models"]} == {(r.id, r.revision) for r in records}
        for record in records:
            entry = entries[record.id]
            spec = entry["files"][0]
            installed = record.dir / spec["path"]
            raw = files[spec["path"]].read_bytes()
            assert installed.read_bytes() == raw
            assert spec["sha256"] == hashlib.sha256(raw).hexdigest()
            assert spec["size"] == entry["size_bytes"] == record.size_bytes == len(raw)
            assert record.state == "ok" and record.metadata_ok and not record.recheck
            assert not installed.is_symlink() and installed.stat().st_nlink == 1
            assert installed.stat().st_uid == os.geteuid()
            assert installed.stat().st_mode & 0o777 == 0o600
            metadata = record.dir.parent / (record.revision + ".json")
            assert metadata.is_file() and metadata.stat().st_mode & 0o777 == 0o600
        run = QueueRun(
            MeetingStore(tmp_path / "meetings"), "2026-11-10_120000-d1a2", models, trusted_pair=pair
        )
        state = role_state(store_root=run.store.root, journal=StringIO(), trusted_pair=pair)
        try:
            assert run.jobs.diar_models == pair.models
            assert state.diar_models_provider == pair.models
            assert run.command(9)[-3] == str(pair.root)
        finally:
            state.cleanup()
            run.close()
    assert {name: os.environ.get(name) for name in variables} == previous

    # Default unit installation still uses the tiny wire image and old root.
    tiny = SignedDiarPair.stage(tmp_path / "tiny")
    assert tiny.model_root == (tmp_path / "tiny/models").resolve()
    tiny_files = list(tiny.model_root.glob(".diarization/*/*/*/*.onnx"))
    assert len(tiny_files) == 2
    for installed in tiny_files:
        assert installed.read_bytes() == RAW_MODEL


@pytest.mark.parametrize("through_spawner", [False, True], ids=["process", "spawner"])
def test_fdopen_journal_uses_explicit_stderr_path(tmp_path: Path, through_spawner: bool) -> None:
    """Match child_main's fdopen journal; TextIO.name is a descriptor, not a path."""
    journal_path = tmp_path / "journal.log"
    descriptor = os.open(
        journal_path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
    )
    command = [
        sys.executable,
        "-I",
        "-B",
        "-c",
        "import sys; sys.stderr.write('fdopen-stderr-proof\\n'); sys.exit(7)",
    ]
    with os.fdopen(descriptor, "w", encoding="utf-8") as journal:
        assert isinstance(journal.name, int)
        if through_spawner:
            process = RoleSpawn(journal, None, journal_path=journal_path).Popen(command)
        else:
            process = LoggedRoleProcess(command, journal=journal, journal_path=journal_path)
        try:
            assert process.wait(timeout=5) == 7
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
        assert process.stderr_path.parent == journal_path.parent
        assert process.stderr_path != journal_path
        assert not process.stderr_path.is_symlink()
        assert process.stderr_path.stat().st_mode & 0o777 == 0o600
        assert "fdopen-stderr-proof" in process.stderr_path.read_text()
        text = journal_path.read_text()
        assert "diar_exit=7" in text and "fdopen-stderr-proof" in text
        assert str(process.stderr_path) in text
    assert journal_path.stat().st_mode & 0o777 == 0o600
