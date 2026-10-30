"""T-401/T-212: wire fixtures independent of role schema implementation."""

from __future__ import annotations

import json
import struct
from pathlib import Path
from typing import Any

import pytest

from astra_voice.worker import ipc

pytestmark = pytest.mark.unit
MID = "2026-10-29_120000-ab12"
IDENTITY: dict[str, Any] = {"meeting_id": MID, "meeting_generation": 7}
CAPTURE: dict[str, Any] = {**IDENTITY, "capture_id": "capture1", "state_rev": 2}
PAUSE = {"pause_reasons": ["lock"], "manual_resume_required": True, "blocked_epoch": 3}
MODEL = {
    "id": "gigaam",
    "revision": "r1",
    "dir": "/not-opened",
    "layout": "x",
    "variant": "cpu",
    "threads": 2,
    "min_ram_mb": 512,
}
UTT = {
    "id": "u1",
    "track": "mic",
    "start_ms": 0,
    "end_ms": 10,
    "text": "Текст",
    "speaker": "me",
    "provisional": True,
}
COMMANDS: dict[str, dict[str, dict[str, Any]]] = {
    "meeting-capture": {
        "meeting.detector.start": {"apps_version": 1},
        "meeting.detector.stop": {},
        "meeting.call.query": {},
        "meeting.capture.prepare": {
            **IDENTITY,
            "dir": "/not-opened",
            "far": {"kind": "app_stream", "sink_input": 1, "object_serial": 2, "call_id": "call1"},
            "segment_s": 300,
            "fsync_s": 10,
            "segment_index_from": 0,
        },
        "meeting.capture.open": {**CAPTURE, "blocked_epoch": 3, "admission_mono_ms": 1000},
        "meeting.capture.keepalive": {**CAPTURE, "seq": 1},
        "meeting.capture.pause": {**CAPTURE, "reason": "lock"},
        "meeting.capture.resume": {
            **CAPTURE,
            "blocked_epoch": 3,
            "admission_mono_ms": 1000,
            "tracks": ["mic", "far"],
        },
        "meeting.capture.track.resume": {
            **CAPTURE,
            "blocked_epoch": 3,
            "admission_mono_ms": 1000,
            "track": "mic",
            "mic_device": "default",
        },
        "meeting.capture.extend": {
            **CAPTURE,
            "blocked_epoch": 3,
            "admission_mono_ms": 1000,
            "max_ms": 14400000,
        },
        "meeting.capture.stop": CAPTURE,
        "meeting.capture.shutdown": {},
        "ping": {},
    },
    "meeting-transcribe": {
        "model.load": MODEL,
        "model.unload": {},
        "ping": {},
        "measure": {},
        "meeting.job.start": {
            **IDENTITY,
            "dir": "/not-opened",
            "tracks": ["mic", "far"],
            "variant": "cpu",
            "diarization": True,
            "diar_models": {"segmentation_dir": "/seg", "embedder_dir": "/emb"},
            "self_track": "mic",
            "resume": True,
            "echo_gate": False,
            "limits": {"max_ms": 14400000, "max_utterances": 28800, "max_blocks": 12},
        },
        "meeting.job.credit": {**IDENTITY, "n": 32},
        "meeting.job.cancel": IDENTITY,
        "meeting.job.applied": {**IDENTITY, "seq": 1, "revision": 2},
    },
    "meeting-diar": {
        "diar.load": {
            **IDENTITY,
            "segmentation_dir": "/seg",
            "embedder_dir": "/emb",
            "threshold": 0.7,
            "tau": 0.9,
            "threads": 2,
        },
        "diar.block": {
            **IDENTITY,
            "block_index": 0,
            "offset_ms": 0,
            "pcm_fd": 3,
            "block_s": 1200,
            "expected_rtf": 0.1,
        },
        "diar.finish": IDENTITY,
        "diar.cancel": IDENTITY,
        "ping": {},
    },
}


def command(role: str, kind: str, req: str = "req1") -> dict[str, Any]:
    return {"type": kind, "req": req, "request_type": kind, **COMMANDS[role][kind]}


def ack(reply_type: str, request: str, **fields: Any) -> dict[str, Any]:
    return {"type": reply_type, "req": "req1", "request_type": request, **fields}


REPLIES: dict[str, list[dict[str, Any]]] = {
    "meeting-capture": [
        ack("meeting.detector.started", "meeting.detector.start", own_client_index=1),
        ack("meeting.detector.stopped", "meeting.detector.stop"),
        ack(
            "meeting.call.state",
            "meeting.call.query",
            active=True,
            call_id="call1",
            app_class="dion",
            target={"sink_input": 1, "object_serial": 2, "verified": "declared-verified"},
        ),
        ack(
            "meeting.capture.prepared",
            "meeting.capture.prepare",
            **CAPTURE,
            **PAUSE,
            mic_label="Микрофон",
            server_local=True,
            server_kind="pipewire",
            far_available=True,
        ),
        ack(
            "meeting.capture.started",
            "meeting.capture.open",
            **CAPTURE,
            **PAUSE,
            t0_mono_ms=1000,
            tracks=["mic", "far"],
        ),
        ack(
            "meeting.capture.paused",
            "meeting.capture.pause",
            **CAPTURE,
            **PAUSE,
            at_recorded_ms=10,
            segments_closed=1,
        ),
        ack(
            "meeting.capture.stopped",
            "meeting.capture.stop",
            **CAPTURE,
            recorded_ms=10,
            segments={"mic": 1, "far": 1},
            lost_estimate_ms=0,
        ),
        ack("meeting.capture.closed", "meeting.capture.stop", **CAPTURE, tracks=["mic", "far"]),
        ack(
            "meeting.capture.finalized",
            "meeting.capture.stop",
            **CAPTURE,
            segments={"mic": 1, "far": 1},
            intervals=[{"t_mono_ms": 1000, "recorded_ms": 0}],
            status="succeeded",
        ),
        ack("meeting.capture.bye", "meeting.capture.shutdown"),
        ack(
            "meeting.capture.track",
            "meeting.capture.track.resume",
            **CAPTURE,
            track="mic",
            state="live",
            kind="reopened",
            at_recorded_ms=10,
        ),
    ],
    "meeting-transcribe": [
        ack(
            "model.loaded",
            "model.load",
            id="gigaam",
            revision="r1",
            variant="cpu",
            load_ms=10,
            engine_version="1",
        ),
        ack("model.unloaded", "model.unload"),
        ack("measured", "measure", vm_hwm_kb=100, pss_kb=None),
        ack(
            "meeting.job.accepted",
            "meeting.job.start",
            **IDENTITY,
            resume_from={"mic_ms": 0, "far_ms": 0},
        ),
        ack("meeting.job.cancelled", "meeting.job.cancel", **IDENTITY),
    ],
    "meeting-diar": [
        ack("diar.loaded", "diar.load", **IDENTITY),
        ack("diar.block.done", "diar.block", **IDENTITY, block_index=0, segments=[[0, 10, "S1"]]),
        ack("diar.finished", "diar.finish", **IDENTITY, speakers_total=1, label_map={"S2": "S1"}),
        ack("diar.cancelled", "diar.cancel", **IDENTITY),
    ],
}
EVENTS: dict[str, list[dict[str, Any]]] = {
    "meeting-capture": [
        {
            "type": "meeting.call.started",
            **IDENTITY,
            "call_id": "call1",
            "app_class": "browser",
            "verified": "declared",
            "has_target": True,
        },
        {"type": "meeting.call.ended", **IDENTITY, "call_id": "call1", "reason": "mic_released"},
        {
            "type": "meeting.capture.alive",
            **CAPTURE,
            **PAUSE,
            "seq": 1,
            "state": "paused",
            "recorded_ms": 10,
            "tracks": {"mic": "paused", "far": "ended"},
            "free_mb": 1000,
            "cpu_pct": 1.0,
        },
        {
            "type": "meeting.capture.track",
            **CAPTURE,
            "track": "mic",
            "state": "lost",
            "kind": "device-lost",
            "label": "Микрофон",
            "at_recorded_ms": 10,
        },
        {
            "type": "meeting.capture.autopaused",
            **CAPTURE,
            **PAUSE,
            "reason": "lease",
            "at_recorded_ms": 10,
        },
        {
            "type": "meeting.capture.segment",
            **CAPTURE,
            "track": "mic",
            "index": 0,
            "frames": 100,
            "closed": True,
            "epoch_id": "epoch1",
            "clock": "pulse",
            "clock_anchor": {"server_ms": 100, "t_mono_ms": 1000},
            "sample_start": 0,
            "sample_end": 100,
            "discontinuity": False,
            "close_reason": "user",
            "pause_reasons": ["user"],
        },
        {"type": "meeting.capture.warning", **CAPTURE, "code": "gaps", "value": 1},
    ],
    "meeting-transcribe": [
        {
            "type": "meeting.job.progress",
            **IDENTITY,
            "stage": "asr",
            "done_ms": 0,
            "total_ms": 10,
            "block": 0,
            "blocks": 1,
        },
        {"type": "meeting.job.utterance", **IDENTITY, "seq": 1, **UTT},
        {
            "type": "meeting.job.labels",
            **IDENTITY,
            "seq": 2,
            "items": [["u1", "S1"]],
            "final": True,
            "speakers_total": 1,
        },
        {
            "type": "meeting.job.waiting",
            **IDENTITY,
            "reason": "memory",
            "need_mb": 1000,
            "available_mb": 500,
        },
        {
            "type": "meeting.job.done",
            **IDENTITY,
            "utterances": 1,
            "speakers": 1,
            "diar_status": "done",
            "wall_ms": 100,
            "peak_rss_kb": 1000,
            "echo_dropped": 0,
            "transcript_revision": 2,
        },
        {"type": "meeting.job.failed", **IDENTITY, "code": "damaged"},
        {
            "type": "meeting.job.replace",
            **IDENTITY,
            "seq": 3,
            "replaces_id": "u1",
            "expected_revision": 1,
            "items": [UTT],
        },
    ],
    "meeting-diar": [
        {
            "type": "diar.block.part",
            **IDENTITY,
            "block_index": 0,
            "seq": 1,
            "last": True,
            "segments": [[0, 10, "S1"]],
        },
    ],
}
CASES = [(role, command(role, kind)) for role in COMMANDS for kind in COMMANDS[role]]
CASES += [(role, msg) for role in COMMANDS for msg in REPLIES[role] + EVENTS[role]]
CASES += [(role, ack("pong", "ping")) for role in COMMANDS]
CASES += [
    (
        role,
        ack(
            "meeting.ack",
            next(iter(COMMANDS[role])),
            **(IDENTITY if role == "meeting-diar" else {}),
        ),
    )
    for role in COMMANDS
]
CASES += [
    (role, ack("error", "ping", code="not-prepared", message="meeting-not-prepared"))
    for role in COMMANDS
]


@pytest.mark.parametrize("role,message", CASES, ids=[f"{r}:{m['type']}" for r, m in CASES])
def test_all_meeting_messages_roundtrip(role: str, message: dict[str, Any]) -> None:
    frame = ipc.encode(message, role=role)
    assert ipc.FrameReader(role=role).feed(frame[:3]) == []
    reader = ipc.FrameReader(role=role)
    assert reader.feed(frame) == [message]
    assert ipc.decode(frame[4:], role=role) == message


def test_dictation_schema_snapshot_and_t212() -> None:
    snapshot = json.loads(
        (Path(__file__).parents[1] / "fixtures/dictation-v4-schemas.json").read_text()
    )
    actual = {
        key: {
            "required": {k: [t.__name__ for t in v] for k, v in s.required.items()},
            "optional": {k: [t.__name__ for t in v] for k, v in s.optional.items()},
        }
        for key, s in ipc._SCHEMAS.items()
    }
    assert actual == snapshot and ipc.PROTOCOL_VERSION == 4
    msg = {"type": "record.start", "utterance_id": "u1"}
    assert ipc.decode(ipc.encode({**msg, **CAPTURE, "far": {"kind": "sink_monitor"}})[4:]) == msg
    with pytest.raises(ipc.FrameError) as caught:
        ipc.encode(command("meeting-capture", "meeting.capture.stop"))
    assert caught.value.code == "unknown-message"


@pytest.mark.parametrize("role", COMMANDS)
def test_role_hello_and_reject_foreign_messages(role: str) -> None:
    hello = ipc.make_hello(role=role)
    assert hello["family"] == "astra-voice.meetings" and hello["protocol"] == 1
    assert ipc.decode(ipc.encode(hello, role=role)[4:], role=role) == hello
    for field, value in [("family", "foreign"), ("protocol", 4)]:
        with pytest.raises(ipc.FrameError) as caught:
            ipc.decode(json.dumps({**hello, field: value}).encode(), role=role)
        assert caught.value.code == ipc.PROTOCOL_MISMATCH
    with pytest.raises(ipc.FrameError) as caught:
        ipc.encode({"type": "record.start", "utterance_id": "u1"}, role=role)
    assert caught.value.code == ipc.UNKNOWN_MESSAGE


@pytest.mark.parametrize(
    "field,value",
    [
        ("req", ""),
        ("req", "x" * 65),
        ("request_type", "ping"),
        ("meeting_generation", -1),
        ("meeting_generation", True),
        ("state_rev", -1),
        ("tracks", ["secret"]),
        ("unknown", "PRIVATE"),
    ],
)
def test_invalid_meeting_fields(field: str, value: object) -> None:
    msg = command("meeting-capture", "meeting.capture.resume")
    with pytest.raises(ipc.FrameError) as caught:
        ipc.encode({**msg, field: value}, role="meeting-capture")
    assert caught.value.code == ipc.BAD_FIELD


@pytest.mark.parametrize("role,message", CASES)
def test_commands_and_acks_need_both_correlation_fields(role: str, message: dict[str, Any]) -> None:
    if "req" not in message:
        assert "meeting_id" in message and "meeting_generation" in message
        return
    for field in ("req", "request_type"):
        with pytest.raises(ipc.FrameError):
            ipc.encode({k: v for k, v in message.items() if k != field}, role=role)


def test_nested_bad_data_and_existing_frame_limits() -> None:
    role = "meeting-transcribe"
    base = {
        "type": "meeting.job.replace",
        **IDENTITY,
        "seq": 1,
        "replaces_id": "u1",
        "expected_revision": 1,
        "items": [UTT],
    }
    for items in [
        [{**UTT, "end_ms": -1}],
        [{**UTT, "text": "я" * 16385}],
        [{**UTT, "start_ms": 11}],
        [{**UTT, "secret": "x"}],
    ]:
        with pytest.raises(ipc.FrameError) as caught:
            ipc.encode({**base, "items": items}, role=role)
        assert caught.value.code == ipc.BAD_FIELD
    payload = json.dumps({**base, "items": [{**UTT, "text": "x" * 32000}] * 3}).encode()
    with pytest.raises(ipc.FrameError) as caught:
        ipc.decode(payload, role=role)
    assert caught.value.code == ipc.BAD_FIELD
    reader = ipc.FrameReader(role=role)
    with pytest.raises(ipc.FrameError) as caught:
        reader.feed(struct.pack(">I", ipc.MAX_FRAME_BYTES + 1))
    assert caught.value.code == ipc.BAD_FIELD and reader.broken


def test_m1_capture_boundaries_use_disk_units_and_intervals() -> None:
    from astra_voice.meetings.model import Interval
    from astra_voice.meetings.segments import ClockAnchor, SegmentSidecar

    sidecar = SegmentSidecar(
        meeting_id=MID,
        meeting_generation=7,
        capture_id="capture1",
        state_rev=2,
        epoch_id="epoch1",
        clock_anchor=ClockAnchor(server_ms=100, t_mono_ms=1000),
        closed=True,
        frames=100,
        sample_end=100,
        close_reason="user",
    )
    raw = sidecar.to_dict()
    message = {key: raw[key] for key in EVENTS["meeting-capture"][5] if key != "type"}
    message["type"] = "meeting.capture.segment"
    assert (
        ipc.decode(ipc.encode(message, role="meeting-capture")[4:], role="meeting-capture")
        == message
    )
    final = ack(
        "meeting.capture.finalized",
        "meeting.capture.stop",
        **CAPTURE,
        segments={"mic": 1, "far": 0},
        intervals=[Interval(t_mono_ms=1000, recorded_ms=0).to_dict()],
        status="succeeded",
    )
    assert (
        ipc.decode(ipc.encode(final, role="meeting-capture")[4:], role="meeting-capture") == final
    )


@pytest.mark.parametrize("role", COMMANDS)
def test_schema_inventory_has_no_untested_message(role: str) -> None:
    from astra_voice.worker.meeting_contract import SCHEMAS

    assert set(SCHEMAS[role]) == {msg["type"] for r, msg in CASES if r == role} | {"hello"}


@pytest.mark.parametrize(
    "role,kind",
    [
        ("meeting-capture", "meeting.capture.keepalive"),
        ("meeting-transcribe", "meeting.job.credit"),
        ("meeting-diar", "diar.load"),
    ],
)
def test_receipt_requires_meeting_identity_and_capture_identity_when_applicable(
    role: str, kind: str
) -> None:
    identity = CAPTURE if role == "meeting-capture" else IDENTITY
    message = ack("meeting.ack", kind, **identity)
    for field in identity:
        with pytest.raises(ipc.FrameError) as caught:
            ipc.encode({key: value for key, value in message.items() if key != field}, role=role)
        assert caught.value.code == ipc.BAD_FIELD


def test_utterance_event_is_provisional_but_replacement_may_be_final() -> None:
    event = EVENTS["meeting-transcribe"][1]
    with pytest.raises(ipc.FrameError) as caught:
        ipc.encode({**event, "provisional": False}, role="meeting-transcribe")
    assert caught.value.code == ipc.BAD_FIELD
    replacement = EVENTS["meeting-transcribe"][-1]
    replacement = {**replacement, "items": [{**UTT, "provisional": False}]}
    assert (
        ipc.decode(
            ipc.encode(replacement, role="meeting-transcribe")[4:], role="meeting-transcribe"
        )
        == replacement
    )


def test_model_loaded_preserves_existing_fractional_load_time() -> None:
    message = {**REPLIES["meeting-transcribe"][0], "load_ms": 12.5}
    assert (
        ipc.decode(ipc.encode(message, role="meeting-transcribe")[4:], role="meeting-transcribe")
        == message
    )


@pytest.mark.parametrize("value", [-1, float("nan"), float("inf"), True])
def test_model_loaded_rejects_invalid_load_time(value: object) -> None:
    with pytest.raises(ipc.FrameError) as caught:
        ipc.encode(
            {**REPLIES["meeting-transcribe"][0], "load_ms": value}, role="meeting-transcribe"
        )
    assert caught.value.code == ipc.BAD_FIELD
