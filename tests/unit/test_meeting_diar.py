"""E13.M7: engine-free role, global identities, bounded D13 and adversarial PCM."""

from __future__ import annotations

import array
import importlib
import itertools
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from tools.meeting_diar_check import der, sealed_pcm

from astra_voice.meetings import diar_link as link
from astra_voice.worker import ipc
from astra_voice.worker import meeting_diar as diar
from astra_voice.worker.onnx_scan import collect_external_locations

pytestmark = pytest.mark.unit
IDENTITY = {"meeting_id": "2026-11-10_120000-d1a2", "meeting_generation": 1}
PROFILE = link.Profile(0.3, 0.45)


def command(kind: str, req: str | None = None, **fields: Any) -> dict[str, Any]:
    return {
        "type": kind,
        "request_type": kind,
        "req": req or kind.replace(".", "_"),
        **({} if kind == "ping" else IDENTITY),
        **fields,
    }


def centroid(angle: float, duration: float = 10) -> link.Centroid:
    return link.Centroid([np.cos(angle), np.sin(angle)], duration)


def drain(state: diar.MeetingDiarState, timeout: float = 3) -> list[dict[str, Any]]:
    events = []
    deadline = time.monotonic() + timeout
    while state.active is not None or not state.events.empty():
        events.extend(state.poll())
        assert time.monotonic() < deadline
        time.sleep(0.001)
    return events


class FakeEngine:
    def __init__(self, message: dict[str, Any]) -> None:
        self.calls = 0

    def process(self, audio: Any, cancel: threading.Event) -> Any:
        diar.check_cancel(cancel)
        self.calls += 1
        return [(0, 1000, 9)], {9: centroid(0)}


def loaded(factory: Any = FakeEngine) -> diar.MeetingDiarState:
    state = diar.MeetingDiarState(engine_factory=factory)
    response = state.handle(
        command(
            "diar.load",
            segmentation_dir="/no-open",
            embedder_dir="/no-open",
            threshold=0.3,
            tau=0.45,
            threads=2,
        )
    )
    assert response[0]["type"] == "meeting.ack"
    assert drain(state)[-1]["type"] == "diar.loaded"
    return state


def block(state: diar.MeetingDiarState, index: int, req: str | None = None) -> list[dict[str, Any]]:
    descriptor = sealed_pcm(np.zeros(16000, dtype="<f4"))
    message = command(
        "diar.block",
        req or f"block{index}",
        block_index=index,
        offset_ms=index * 1000,
        pcm_fd=77,
        block_s=1,
        expected_rtf=0.1,
    )
    assert state.handle(message, [descriptor])[0]["type"] == "meeting.ack"
    with pytest.raises(OSError):
        # Wait for read_pcm's finally even when the worker hasn't run yet.
        drain_result = drain(state)
        os.fstat(descriptor)
    return drain_result


def test_all_historical_speakers_including_silent_block() -> None:
    linker = link.SpeakerLinker(PROFILE)
    assert linker.link([(0, 1000, 2)], {2: centroid(0)}, 0)[0][2] == "S1"
    assert linker.link([(0, 1000, 2)], {2: centroid(2)}, 1000)[0][2] == "S2"
    assert linker.link([(0, 1000, 7)], {7: centroid(0.02)}, 2000)[0][2] == "S1"
    assert linker.globals[1].duration == 20


def test_first_appearance_not_local_number_and_one_to_one() -> None:
    linker = link.SpeakerLinker(PROFILE)
    result = linker.link([(100, 200, 1), (0, 90, 99)], {1: centroid(2), 99: centroid(0)}, 0)
    assert [r[2] for r in result] == ["S1", "S2"]
    # Deliberately bypass local merging to test assignment independently.
    weights = [[10.9, 10.8], [10.85, 0]]
    assert link.maximize_assignment(weights) == [(0, 1), (1, 0)]
    # Both rows prefer col 0; one global may be used only once.
    pairs = link.maximize_assignment([[20, 1], [19, 0]])
    assert len({c for _, c in pairs}) == 2


def test_assignment_matches_exhaustive_rectangles() -> None:
    rng = np.random.default_rng(17)
    for rows, cols in [(2, 4), (4, 2), (4, 4), (1, 3)]:
        weights = rng.integers(0, 10, (rows, cols)).tolist()
        actual = sum(weights[r][c] for r, c in link.maximize_assignment(weights))
        size = max(rows, cols)
        expected = max(
            sum(weights[r][p[r]] if r < rows and p[r] < cols else 0 for r in range(size))
            for p in itertools.permutations(range(size))
        )
        assert actual == expected


def test_consolidation_keeps_smallest_number_and_holes() -> None:
    linker = link.SpeakerLinker(PROFILE)
    linker.globals = {1: centroid(0), 2: centroid(2), 3: centroid(0.01), 4: centroid(4)}
    labels, total = linker.finish()
    assert labels == {"S1": "S1", "S2": "S2", "S3": "S1", "S4": "S4"}
    assert total == 3


@pytest.mark.parametrize("centres", [[0], [0, 1.2], [0, 1.3, 2.6, 4]])
def test_adaptive_clusters_1_2_4(centres: list[float]) -> None:
    vectors = [centroid(c + delta) for c in centres for delta in (-0.02, 0.02)]
    groups, cut = link.cluster(vectors, PROFILE.merge_ceiling)
    assert len(set(groups)) == len(centres)
    assert 0 < cut < PROFILE.merge_ceiling
    assert link.cluster(vectors, PROFILE.merge_ceiling) == (groups, cut)


def test_two_close_voices_within_ceiling_are_separable_by_gap() -> None:
    groups, _ = link.cluster([centroid(a) for a in (0, 0.01, 0.8, 0.81)], 0.45)
    assert groups == [0, 0, 2, 2]


@pytest.mark.parametrize("value", [[0, 0], [float("nan"), 1], [[1, 0]], []])
def test_invalid_features_refused(value: Any) -> None:
    with pytest.raises(ValueError):
        link.Centroid(value, 1)


def test_d13_large_block_empty_block_and_done_requires_all_parts() -> None:
    turns = [[i * 100, i * 100 + 90, "S64"] for i in range(5000)]
    parts = list(diar.block_parts(IDENTITY, 0, turns))
    assert len(parts) > 1
    assert all(len(ipc.encode(p, role=diar.ROLE)) <= 65536 for p in parts)
    assert [p["seq"] for p in parts] == list(range(len(parts)))
    assert [p["last"] for p in parts] == [False] * (len(parts) - 1) + [True]
    assembly = diar.BlockAssembly(IDENTITY, 0)
    for part in parts:
        assert not assembly.accept(part)
    done = {**command("diar.block"), "type": "diar.block.done", "block_index": 0, "segments": []}
    assert assembly.accept(done) and assembly.segments == turns
    assert list(diar.block_parts(IDENTITY, 1, []))[0]["last"] is True


@pytest.mark.parametrize("fault", ["duplicate", "gap", "incomplete", "wrong-block", "done-data"])
def test_d13_corruption_poisoned(fault: str) -> None:
    parts = list(diar.block_parts(IDENTITY, 0, [[i, i + 1, "S1"] for i in range(700)]))
    assembly = diar.BlockAssembly(IDENTITY, 0)
    done = {**command("diar.block"), "type": "diar.block.done", "block_index": 0, "segments": []}
    if fault != "gap":
        assembly.accept(parts[0])
    message = {
        "duplicate": parts[0],
        "gap": parts[1],
        "incomplete": done,
        "wrong-block": dict(parts[1], block_index=1),
        "done-data": dict(done, segments=[[0, 1, "S1"]]),
    }[fault]
    if fault == "done-data":
        assembly.accept(parts[1])
    with pytest.raises(ValueError):
        assembly.accept(message)
    assert assembly.failed and not assembly.done and assembly.segments == []
    with pytest.raises(ValueError):
        assembly.accept(done)


def test_role_one_pass_complete_retry_and_final_map() -> None:
    state = loaded()
    try:
        events = block(state, 0)
        assert [e["type"] for e in events] == ["diar.block.part", "diar.block.done"]
        assert state.engine.calls == 1
        replay = block(state, 0)
        assert replay == events and state.engine.calls == 1
        state.handle(command("diar.finish"))
        final = drain(state)[-1]
        assert final["type"] == "diar.finished" and final["label_map"] == {"S1": "S1"}
        assert not state.linker.globals and state.engine is None
    finally:
        state.cleanup()


def test_block_index_and_meeting_identity_fenced() -> None:
    state = loaded()
    try:
        fd = sealed_pcm(np.zeros(16000, dtype="<f4"))
        result = state.handle(
            command(
                "diar.block", block_index=1, offset_ms=0, pcm_fd=70, block_s=1, expected_rtf=0.1
            ),
            [fd],
        )
        assert result[0]["code"] == "bad-field"
        with pytest.raises(OSError):
            os.fstat(fd)
        wrong = command("diar.finish")
        wrong["meeting_generation"] = 99
        assert state.handle(wrong)[0]["code"] == "not-prepared"
    finally:
        state.cleanup()


def test_limits_are_atomic_no_growth(monkeypatch: pytest.MonkeyPatch) -> None:
    linker = link.SpeakerLinker(PROFILE)
    linker.link([(0, 10, 0)], {0: centroid(0)}, 0)
    before = dict(linker.globals)
    linker.blocks = link.MAX_BLOCKS
    with pytest.raises(link.LimitError):
        linker.link([(0, 10, 0)], {0: centroid(0)}, 10)
    assert linker.globals == before
    linker.blocks = 1
    linker.segments = link.MAX_SEGMENTS
    with pytest.raises(link.LimitError):
        linker.link([(0, 10, 0)], {0: centroid(0)}, 10)
    assert linker.globals == before
    monkeypatch.setattr(link, "MAX_SPEAKERS", 1)
    linker.segments = 0
    with pytest.raises(link.LimitError):
        linker.link([(0, 10, 0)], {0: centroid(2)}, 10)
    assert linker.globals == before


def test_cancel_and_ping_independent_of_bounded_full_output() -> None:
    class ManyEngine(FakeEngine):
        def process(self, audio: Any, cancel: threading.Event) -> Any:
            return [(i, i + 1, 0) for i in range(5000)], {0: centroid(0)}

    state = loaded(ManyEngine)
    fd = sealed_pcm(np.zeros(16000, dtype="<f4"))
    state.handle(
        command("diar.block", block_index=0, offset_ms=0, pcm_fd=77, block_s=1, expected_rtf=0.1),
        [fd],
    )
    deadline = time.monotonic() + 3
    while state.events.qsize() < diar.QUEUE_FRAMES:
        assert time.monotonic() < deadline
        time.sleep(0.01)
    assert state.handle(command("ping"))[0]["type"] == "pong"
    assert state.handle(command("diar.finish"))[0]["code"] == "not-prepared"
    started = time.monotonic()
    assert state.handle(command("diar.cancel"))[0]["type"] == "diar.cancelled"
    assert time.monotonic() - started < 1
    assert state.poll() == [] and state.events.empty() and state.stopped


@pytest.mark.parametrize("bad", ["file", "pipe", "socket", "size", "unsealed", "nan"])
def test_pcm_rejects_bad_descriptors_closes_all(tmp_path: Path, bad: str) -> None:
    other: Any = None
    if bad == "file":
        p = tmp_path / "pcm"
        p.write_bytes(b"\0" * 64000)
        fd = os.open(p, os.O_RDONLY)
    elif bad == "pipe":
        fd, other = os.pipe()
    elif bad == "socket":
        s, other = socket.socketpair()
        fd = s.detach()
    elif bad == "unsealed":
        fd = os.memfd_create("unsealed", os.MFD_ALLOW_SEALING)
        os.ftruncate(fd, 64000)
    else:
        audio = np.full(16000 if bad == "nan" else 1, np.nan if bad == "nan" else 0, dtype="<f4")
        fd = sealed_pcm(audio)
    try:
        with pytest.raises((OSError, ValueError)):
            diar.read_pcm(fd, 1, threading.Event())
        with pytest.raises(OSError):
            os.fstat(fd)
    finally:
        if isinstance(other, int):
            os.close(other)
        elif other is not None:
            other.close()


def test_pcm_uses_received_fd_not_peer_integer_and_requires_seals() -> None:
    audio = np.linspace(-0.9, 0.9, 16000, dtype="<f4")
    fd = sealed_pcm(audio)
    assert np.array_equal(diar.read_pcm(fd, 1, threading.Event()), audio)


def test_model_scans_both_before_import_and_rejects_external_or_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = tmp_path / "segmentation.onnx"
    model.write_bytes(b"\x08\x01")
    embedder = tmp_path / "embedder.onnx"
    embedder.write_bytes(b"\x08\x01")
    calls = []
    parser = collect_external_locations

    def scan(path: Path) -> list[str]:
        calls.append(path)
        return parser(path)

    monkeypatch.setattr(diar, "collect_external_locations", scan)
    with diar.checked_models([str(tmp_path), str(tmp_path)]) as paths:
        assert len(paths) == 2 and len(calls) == 2
    embedder.unlink()
    embedder.symlink_to(model)
    with pytest.raises(OSError):
        with diar.checked_models([str(tmp_path), str(tmp_path)]):
            pytest.fail("symlink accepted")
    embedder.unlink()
    embedder.write_bytes(b"\x08\x01")
    monkeypatch.setattr(diar, "collect_external_locations", lambda p: ["../escape"])
    with pytest.raises(ValueError, match="external"):
        with diar.checked_models([str(tmp_path), str(tmp_path)]):
            pytest.fail("external accepted")


def test_invalid_onnx_fails_load_before_runtime_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ["segmentation.onnx", "embedder.onnx"]:
        (tmp_path / name).write_bytes(b"bad protobuf")
    original = importlib.import_module

    def guarded(name: str, *args: Any, **kwargs: Any) -> Any:
        assert name != "sherpa_onnx"
        return original(name, *args, **kwargs)

    monkeypatch.setattr(importlib, "import_module", guarded)
    state = diar.MeetingDiarState()
    state.handle(
        command(
            "diar.load",
            segmentation_dir=str(tmp_path),
            embedder_dir=str(tmp_path),
            threshold=0.3,
            tau=0.45,
            threads=2,
        )
    )
    try:
        assert drain(state)[-1]["code"] == "model"
    finally:
        state.cleanup()


def test_scm_rights_exact_frame_boundaries_and_fd_cleanup() -> None:
    import collections

    receiver = diar.DiarReceiver()
    fd = sealed_pcm(np.zeros(16000, dtype="<f4"))
    wire = ipc.encode(command("ping"), role=diar.ROLE)
    rights = [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", [fd]).tobytes())]

    class Transport:
        chunks: Any = collections.deque(
            [
                (wire[:1], []),
                (wire[1:4], []),
                (wire[4:], []),
                (wire[:1], rights),
                (wire[1:4], []),
                (wire[4:], []),
            ]
        )

        def recvmsg(self, size: int, *args: Any) -> Any:
            data, ancillary = self.chunks.popleft()
            assert len(data) == size
            return data, ancillary, 0, None

    transport = Transport()
    try:
        first = None
        while first is None:
            first = receiver.receive(transport)
        assert first[1] == []
        second = None
        while second is None:
            second = receiver.receive(transport)
        assert second[0] == wire[4:] and second[1] == [fd]
    finally:
        receiver.close()
        os.close(fd)


@pytest.mark.parametrize(
    "module,forbidden",
    [
        ("astra_voice.worker.meeting_diar", ["onnxruntime", "onnx_asr"]),
        ("astra_voice.worker.meeting_transcribe", ["sherpa_onnx"]),
        ("astra_voice.meetings.diar_link", ["onnxruntime", "onnx_asr", "sherpa_onnx"]),
    ],
)
def test_import_gates_without_numpy(module: str, forbidden: list[str]) -> None:
    probe = """
import sys, importlib
sys.path.insert(0, sys.argv[1])
sys.modules["numpy"] = None
importlib.import_module(sys.argv[2])
for name in sys.argv[3:]:
    assert not any(m == name or m.startswith(name + ".") for m in sys.modules), name
"""
    result = subprocess.run(
        [
            sys.executable,
            "-B",
            "-I",
            "-c",
            probe,
            str(Path(__file__).resolve().parents[2] / "src"),
            module,
            *forbidden,
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr


def test_no_feature_persistence_source_and_wire_schema() -> None:
    for module in (diar, link):
        source = Path(str(module.__file__)).read_text()
        for forbidden in ("np.save", "tofile", "memmap", "pickle", "shelve", "marshal"):
            assert forbidden not in source
    from astra_voice.worker.meeting_contract import SCHEMAS

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            if value.get("type") == "array" and value.get("maxItems", 0) > 16:
                # Large arrays are records/tuples/text, never numeric vectors.
                assert value.get("items", {}).get("type") not in {"number", "integer"}
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(SCHEMAS[diar.ROLE])
    for field in ("embedding", "centroids", "vector"):
        bad = {**command("diar.finish"), field: [0.0] * 192}
        with pytest.raises(ipc.FrameError):
            ipc.encode(bad, role=diar.ROLE)


def test_der_collar_overlap_assignment_and_miss() -> None:
    ref = [(0.0, 2.0, "a"), (2.0, 4.0, "b")]
    assert der(ref, [(0.0, 2.0, "S2"), (2.0, 4.0, "S1")]) == 0
    assert der(ref, [(0.0, 4.0, "S1")]) == 50
    assert der(ref, []) == 100
    assert der([(0, 2, "a"), (0, 2, "b")], [(0, 2, "S1")]) == 50


def test_measure_breakdown_final_speech_and_provisional_births() -> None:
    from tools.meeting_diar_check import speaker_breakdown

    speakers, births = speaker_breakdown(
        [
            [[0, 700, "S1"], [800, 1300, "S2"]],
            [[1500, 1600, "S3"], [1600, 2000, "S1"]],
            [],
        ],
        {"S1": "S1", "S2": "S2", "S3": "S1"},
    )
    assert speakers == {
        "S1": {"speech_s": 1.2, "intervals": 3, "first_block": 0},
        "S2": {"speech_s": 0.5, "intervals": 1, "first_block": 0},
    }
    assert [b["new_global_speakers"] for b in births] == [2, 1, 0]
    assert [b["new_labels"] for b in births] == [["S1", "S2"], ["S3"], []]
    assert [b["provisional_speakers_total"] for b in births] == [2, 3, 3]


def test_measure_json_includes_final_speakers_and_block_births(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import json

    from tools import meeting_diar_check as check

    class FakeRole:
        peak_rss_kb = 123

        def __init__(self, **kwargs: Any) -> None:
            pass

        def __enter__(self) -> Any:
            return self

        def __exit__(self, *args: Any) -> None:
            pass

        def load(self, *args: Any) -> None:
            pass

        def sample_rss(self) -> None:
            pass

        def send(self, *args: Any) -> str:
            return "finish"

        def block(self, audio: Any, index: int, offset: int) -> list[list[Any]]:
            return (
                [[0, 400, "S1"], [500, 700, "S2"]]
                if index == 0
                else [[1000, 1100, "S3"], [1200, 1400, "S1"]]
            )

        def wait(self, *args: Any) -> dict[str, Any]:
            return {"speakers_total": 2, "label_map": {"S1": "S1", "S2": "S2", "S3": "S1"}}

    monkeypatch.setattr(check, "RoleProcess", FakeRole)
    monkeypatch.setattr(check, "wav_blocks", lambda *args: [np.zeros(16000), np.zeros(16000)])
    assert (
        check.main(
            ["unused.wav", "--models", "/models", "--duration", "2", "--block-s", "1", "--json"]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result["speaker_stats"] == {
        "S1": {"speech_s": 0.7, "intervals": 3, "first_block": 0},
        "S2": {"speech_s": 0.2, "intervals": 1, "first_block": 0},
    }
    assert [block["new_global_speakers"] for block in result["blocks"]] == [2, 1]


def six_block_scene(
    *, short_guest: bool = False, voices: int = 4
) -> tuple[link.SpeakerLinker, list[list[list[Any]]]]:
    """Independent brief outliers around one stable voice, plus a repeat guest."""
    linker = link.SpeakerLinker(link.DIAR_PROFILES["3dspeaker-campplus"])
    blocks = []
    for index in range(6):
        vectors = {}
        turns = []
        for speaker in range(voices):
            vector = np.zeros(16)
            vector[speaker] = 1
            vectors[speaker] = link.Centroid(vector, 200)
            turns.append((speaker * 200000, (speaker + 1) * 200000, speaker))
        vector = np.zeros(16)
        vector[0], vector[5 + index] = 0.5, np.sqrt(0.75)
        vectors[10] = link.Centroid(vector, 0.3)
        turns.append((1100000, 1100300, 10))
        if short_guest and index in (1, 4):
            vector = np.zeros(16)
            vector[1], vector[12] = 0.3, np.sqrt(0.91)
            vectors[11] = link.Centroid(vector, 0.3)
            turns.append((1101000, 1101300, 11))
        blocks.append(linker.link(turns, vectors, index * 1200000))
    return linker, blocks


def test_six_blocks_short_outliers_do_not_multiply_speakers() -> None:
    linker, blocks = six_block_scene()
    mapping, count = linker.finish()
    assert count == 4
    assert {mapping[rows[-1][2]] for rows in blocks} == {"S1"}
    assert mapping == linker.finish()[0]  # consolidation is deterministic and read-only


def test_short_distant_guest_repeated_in_two_blocks_survives() -> None:
    linker, blocks = six_block_scene(short_guest=True)
    mapping, count = linker.finish()
    guest = blocks[1][-1][2]
    assert blocks[4][-1][2] == guest
    assert mapping[guest] == guest
    assert count == 5
    assert guest not in {mapping[row[2]] for row in blocks[0]}


def test_support_consolidation_does_not_assume_four_speakers() -> None:
    linker, _ = six_block_scene(voices=5)
    assert linker.finish()[1] == 5


def test_weak_first_voice_merge_keeps_earlier_number_and_holes() -> None:
    linker = link.SpeakerLinker(link.DIAR_PROFILES["3dspeaker-campplus"])
    vectors = {0: link.Centroid([0.5, 0, 0, 0, np.sqrt(0.75)], 0.3)}
    turns = [(0, 300, 0)]
    for i in range(4):
        vector = np.zeros(5)
        vector[i] = 1
        vectors[i + 1] = link.Centroid(vector, 200)
        turns.append((1000 + i * 200000, 201000 + i * 200000, i + 1))
    rows = linker.link(turns, vectors, 0)
    assert [row[2] for row in rows] == ["S1", "S2", "S3", "S4", "S5"]
    mapping, count = linker.finish()
    assert count == 4
    assert mapping == {"S1": "S1", "S2": "S1", "S3": "S3", "S4": "S4", "S5": "S5"}


def test_short_orthogonal_singleton_is_not_forced_into_known_voice() -> None:
    linker = link.SpeakerLinker(link.DIAR_PROFILES["3dspeaker-campplus"])
    vectors = {0: link.Centroid([1, 0], 200), 1: link.Centroid([0, 1], 0.3)}
    linker.link([(0, 200000, 0), (201000, 201300, 1)], vectors, 0)
    assert linker.finish() == ({"S1": "S1", "S2": "S2"}, 2)


def test_repeat_guest_is_preserved_but_does_not_absorb_uncertain_noise() -> None:
    linker = link.SpeakerLinker(link.DIAR_PROFILES["3dspeaker-campplus"])
    for index in range(2):
        vectors = {
            0: link.Centroid([1, 0, 0], 200),
            1: link.Centroid([0.3, np.sqrt(0.91), 0], 0.3),
        }
        linker.link([(0, 200000, 0), (201000, 201300, 1)], vectors, index * 1200000)
    noise = link.Centroid([0.5, 0.4, np.sqrt(0.59)], 0.3)
    assert link.distance(noise, linker.globals[2]) < link.distance(noise, linker.globals[1])
    rows = linker.link([(0, 300, 0)], {0: noise}, 2400000)
    assert rows[0][2] == "S3"
    assert linker.finish() == ({"S1": "S1", "S2": "S2", "S3": "S1"}, 2)


def test_two_intervals_in_one_block_are_not_independent_block_evidence() -> None:
    linker = link.SpeakerLinker(link.DIAR_PROFILES["3dspeaker-campplus"])
    linker.link(
        [(0, 200000, 0), (201000, 201300, 1), (202000, 202300, 1)],
        {0: link.Centroid([1, 0], 200), 1: link.Centroid([0.5, np.sqrt(0.75)], 0.6)},
        0,
    )
    assert linker.finish() == ({"S1": "S1", "S2": "S1"}, 1)


def test_no_supported_voice_means_no_relaxed_merge() -> None:
    linker = link.SpeakerLinker(link.DIAR_PROFILES["3dspeaker-campplus"])
    linker.link(
        [(0, 300, 0), (1000, 1300, 1)],
        {0: link.Centroid([1, 0], 0.3), 1: link.Centroid([0.5, np.sqrt(0.75)], 0.3)},
        0,
    )
    assert linker.finish() == ({"S1": "S1", "S2": "S2"}, 2)


def test_supported_one_block_guest_keeps_ordinary_distance_cut() -> None:
    profile = link.DIAR_PROFILES["3dspeaker-campplus"]
    linker = link.SpeakerLinker(profile)
    duration_ms = round(profile.min_support_s * 1000)
    linker.link(
        [(0, 200000, 0), (201000, 201000 + duration_ms, 1)],
        {
            0: link.Centroid([1, 0], 200),
            1: link.Centroid([0.5, np.sqrt(0.75)], profile.min_support_s),
        },
        0,
    )
    assert linker.finish() == ({"S1": "S1", "S2": "S2"}, 2)
