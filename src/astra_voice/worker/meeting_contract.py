"""Role-scoped meetings v1. No audio, filesystem, inference or network access.

R5 overrides the older no-req keepalive/credit rows. Every command has an
immediate correlated outcome (meeting.ack for receipt of a long operation).
Its final response and physical capture.closed have separate absolute deadlines.
Only transcribe writes transcript/checkpoint; job.applied is a GUI application
receipt, never evidence of fsync. These schemas do not implement M2/M4/M7.
"""

from __future__ import annotations

import hashlib
import json
import math
from copy import deepcopy
from typing import Any

from astra_voice.meetings.model import (
    BOOL,
    IDENTIFIER,
    LABEL,
    MEETING_ID,
    MILLISECONDS,
    UINT,
    Interval,
    array,
    choices,
    mapping,
    normalize_text,
    nullable,
    obj,
    text_schema,
)
from astra_voice.meetings.segments import CLOCKS, CLOSE_REASONS, ClockAnchor
from astra_voice.models.schema import SchemaInvalid, SchemaUnavailable, validate

FAMILY = "astra-voice.meetings"
PROTOCOL = 1
ROLES = frozenset({"meeting-capture", "meeting-transcribe", "meeting-diar"})
MAX_REQUESTS = 16384  # Never evict a req within a process generation.
MAX_CACHE_BYTES = 16 * 1024 * 1024
MAX_REJECTIONS = 256  # Separate bounded reserve; never forget a refused req.
_REPLY_RESERVATION = 65536 + 1024
ASR_WINDOW_TIMEOUT_S = 60.0
CANCEL_TIMEOUT_S = 1.0
LEASE_INTERVAL_S = 1.0
LEASE_SILENCE_S = 5.0
S = text_schema(2048)
TRACK = choices("mic", "far")
TRACKS = array(TRACK, 2, unique=True)
STATE = choices("live", "paused", "lost", "ended")
PAUSE_REASONS = array(
    choices("user", "lock", "unknown", "lease", "indicators", "mic_change", "audio_failed", "disk"),
    8,
    unique=True,
)
IDENTITY = {"meeting_id": MEETING_ID, "meeting_generation": UINT}
CAPTURE = {**IDENTITY, "capture_id": IDENTIFIER, "state_rev": UINT}
PAUSE = {"pause_reasons": PAUSE_REASONS, "manual_resume_required": BOOL, "blocked_epoch": UINT}
SEGMENTS = obj({"mic": UINT, "far": UINT})
INTERVAL = Interval.SCHEMA
UTTERANCE = obj(
    {
        "id": IDENTIFIER,
        "track": TRACK,
        "start_ms": MILLISECONDS,
        "end_ms": MILLISECONDS,
        "text": text_schema(32768),
        "speaker": nullable(LABEL),
        "provisional": BOOL,
    }
)


def tuple_schema(*items: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "array",
        "prefixItems": list(items),
        "items": False,
        "minItems": len(items),
        "maxItems": len(items),
    }


TURNS = array(tuple_schema(MILLISECONDS, MILLISECONDS, LABEL), 2000)
MODEL = {
    "id": IDENTIFIER,
    "revision": text_schema(128, 1),
    "dir": S,
    "layout": S,
    "variant": S,
    "threads": {"type": "integer", "minimum": 1, "maximum": 256},
    "min_ram_mb": UINT,
}
FAR = obj(
    {
        "kind": choices("app_stream", "sink_monitor", "none"),
        "sink_input": UINT,
        "object_serial": UINT,
        "call_id": IDENTIFIER,
    },
    required=["kind"],
)
TARGET = obj(
    {
        "sink_input": UINT,
        "object_serial": UINT,
        "verified": choices("declared-verified", "declared", "mismatch", "unverified"),
    }
)
ADMISSION = {**CAPTURE, "blocked_epoch": UINT, "admission_mono_ms": UINT}
# Every listed field is required unless explicitly in optional.
COMMAND_FIELDS: dict[str, dict[str, dict[str, Any]]] = {
    "meeting-capture": {
        "meeting.detector.start": {"apps_version": UINT},
        "meeting.detector.stop": {},
        "meeting.call.query": {},
        "meeting.capture.prepare": {
            **IDENTITY,
            "dir": S,
            "far": FAR,
            "segment_s": {"const": 300, "type": "integer"},
            "fsync_s": {"const": 10, "type": "integer"},
            "segment_index_from": UINT,
        },
        "meeting.capture.open": ADMISSION,
        "meeting.capture.keepalive": {**CAPTURE, "seq": UINT},
        "meeting.capture.pause": {
            **CAPTURE,
            "reason": choices(
                "user",
                "lock",
                "unknown",
                "indicators",
                "mic_change",
                "audio_failed",
                "disk",
                "lease",
            ),
        },
        "meeting.capture.resume": ADMISSION,
        "meeting.capture.track.resume": {**ADMISSION, "track": choices("mic"), "mic_device": S},
        "meeting.capture.extend": {**ADMISSION, "max_ms": MILLISECONDS},
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
            "dir": S,
            "tracks": TRACKS,
            "variant": S,
            "diarization": BOOL,
            "self_track": choices("mic", "none"),
            "resume": BOOL,
            "echo_gate": BOOL,
            "limits": obj(
                {
                    "max_ms": MILLISECONDS,
                    "max_utterances": {"type": "integer", "minimum": 1, "maximum": 28800},
                    "max_blocks": {"type": "integer", "minimum": 1, "maximum": 12},
                }
            ),
        },
        "meeting.job.credit": {**IDENTITY, "n": {"type": "integer", "minimum": 1, "maximum": 64}},
        "meeting.job.cancel": IDENTITY,
        "meeting.job.applied": {**IDENTITY, "seq": UINT, "revision": UINT},
    },
    "meeting-diar": {
        "diar.load": {
            **IDENTITY,
            "segmentation_dir": S,
            "embedder_dir": S,
            "threshold": {"type": "number", "minimum": 0, "maximum": 10},
            "tau": {"type": "number", "minimum": 0, "maximum": 10},
            "threads": {"type": "integer", "minimum": 1, "maximum": 256},
        },
        "diar.block": {
            **IDENTITY,
            "block_index": {"type": "integer", "minimum": 0, "maximum": 11},
            "offset_ms": MILLISECONDS,
            "pcm_fd": {"type": "integer", "minimum": 3, "maximum": 2147483647},
            "block_s": {"type": "number", "exclusiveMinimum": 0, "maximum": 1200},
            "expected_rtf": {"type": "number", "exclusiveMinimum": 0, "maximum": 100},
        },
        "diar.finish": IDENTITY,
        "diar.cancel": IDENTITY,
        "ping": {},
    },
}
OPTIONAL: dict[str, dict[str, Any]] = {
    "meeting.capture.prepare": {"mic_device": S},
    "meeting.capture.resume": {"mic_device": S, "tracks": TRACKS},
    "meeting.job.start": {"diar_models": obj({"segmentation_dir": S, "embedder_dir": S})},
}
# Reply -> allowed request_type. Not every correlated receipt is a final ACK.
REPLIES: dict[str, dict[str, tuple[str, ...]]] = {
    "meeting-capture": {
        "meeting.detector.started": ("meeting.detector.start",),
        "meeting.detector.stopped": ("meeting.detector.stop",),
        "meeting.call.state": ("meeting.call.query",),
        "meeting.capture.prepared": ("meeting.capture.prepare",),
        "meeting.capture.started": (
            "meeting.capture.open",
            "meeting.capture.resume",
            "meeting.capture.extend",
        ),
        "meeting.capture.paused": ("meeting.capture.pause",),
        "meeting.capture.stopped": ("meeting.capture.stop",),
        "meeting.capture.closed": ("meeting.capture.pause", "meeting.capture.stop"),
        "meeting.capture.finalized": ("meeting.capture.pause", "meeting.capture.stop"),
        "meeting.capture.bye": ("meeting.capture.shutdown",),
        "meeting.capture.track": ("meeting.capture.track.resume",),
    },
    "meeting-transcribe": {
        "model.loaded": ("model.load",),
        "model.unloaded": ("model.unload",),
        "measured": ("measure",),
        "meeting.job.accepted": ("meeting.job.start",),
        "meeting.job.cancelled": ("meeting.job.cancel",),
    },
    "meeting-diar": {
        "diar.loaded": ("diar.load",),
        "diar.block.done": ("diar.block",),
        "diar.finished": ("diar.finish",),
        "diar.cancelled": ("diar.cancel",),
    },
}
REPLY_FIELDS: dict[str, dict[str, Any]] = {
    "meeting.detector.started": {"own_client_index": UINT},
    "meeting.detector.stopped": {},
    "meeting.call.state": {"active": BOOL},
    "meeting.capture.prepared": {
        **CAPTURE,
        **PAUSE,
        "mic_label": text_schema(120),
        "server_local": BOOL,
        "server_kind": choices("pipewire", "pulseaudio"),
        "far_available": BOOL,
    },
    "meeting.capture.started": {**CAPTURE, **PAUSE, "t0_mono_ms": UINT, "tracks": TRACKS},
    "meeting.capture.paused": {
        **CAPTURE,
        **PAUSE,
        "at_recorded_ms": MILLISECONDS,
        "segments_closed": UINT,
    },
    "meeting.capture.stopped": {
        **CAPTURE,
        "recorded_ms": MILLISECONDS,
        "segments": SEGMENTS,
        "lost_estimate_ms": UINT,
    },
    "meeting.capture.closed": {**CAPTURE, "tracks": TRACKS},
    "meeting.capture.finalized": {
        **CAPTURE,
        "segments": SEGMENTS,
        "intervals": array(INTERVAL, 9000),
        "status": choices("succeeded", "failed", "partial"),
    },
    "meeting.capture.bye": {},
    "model.loaded": {
        "id": IDENTIFIER,
        "revision": text_schema(128, 1),
        "variant": S,
        "load_ms": {"type": "number", "minimum": 0},
        "engine_version": S,
    },
    "model.unloaded": {},
    "measured": {"vm_hwm_kb": UINT, "pss_kb": nullable(UINT)},
    "meeting.job.accepted": {
        **IDENTITY,
        "resume_from": obj({"mic_ms": MILLISECONDS, "far_ms": MILLISECONDS}),
    },
    "meeting.job.cancelled": IDENTITY,
    "diar.loaded": IDENTITY,
    "diar.block.done": {**IDENTITY, "block_index": UINT, "segments": TURNS},
    "diar.finished": {**IDENTITY, "speakers_total": UINT, "label_map": mapping(LABEL, LABEL)},
    "diar.cancelled": IDENTITY,
}
OPTIONAL.update(
    {
        "meeting.call.state": {
            "call_id": IDENTIFIER,
            "app_class": choices("dion", "browser", "other"),
            "target": TARGET,
        },
        "meeting.capture.prepared": {"far_reason": text_schema(120)},
        "measured": {"sessions": UINT},
    }
)
TRACK_EVENT = {
    **CAPTURE,
    "track": TRACK,
    "state": STATE,
    "kind": choices("switched", "device-lost", "target-removed", "sink-moved", "reopened"),
    "at_recorded_ms": MILLISECONDS,
}
REPLY_FIELDS["meeting.capture.track"] = TRACK_EVENT
EVENT_FIELDS: dict[str, dict[str, dict[str, Any]]] = {
    "meeting-capture": {
        "meeting.call.started": {
            **IDENTITY,
            "call_id": IDENTIFIER,
            "app_class": choices("dion", "browser", "other"),
            "verified": choices("declared-verified", "declared", "mismatch", "unverified"),
            "has_target": BOOL,
        },
        "meeting.call.ended": {
            **IDENTITY,
            "call_id": IDENTIFIER,
            "reason": choices("mic_released", "app_gone"),
        },
        "meeting.capture.alive": {
            **CAPTURE,
            **PAUSE,
            "seq": UINT,
            "state": choices("prepared", "recording", "paused", "stopped"),
            "recorded_ms": MILLISECONDS,
            "tracks": obj({"mic": STATE, "far": STATE}),
            "free_mb": UINT,
            "cpu_pct": {"type": "number", "minimum": 0, "maximum": 100},
        },
        "meeting.capture.track": TRACK_EVENT,
        "meeting.capture.autopaused": {
            **CAPTURE,
            **PAUSE,
            "reason": choices("lease", "audio-failed", "disk-write"),
            "at_recorded_ms": MILLISECONDS,
        },
        "meeting.capture.segment": {
            **CAPTURE,
            "track": TRACK,
            "index": UINT,
            "frames": UINT,
            "closed": BOOL,
            "epoch_id": IDENTIFIER,
            "clock": choices(*CLOCKS),
            "clock_anchor": ClockAnchor.SCHEMA,
            "sample_start": UINT,
            "sample_end": UINT,
            "discontinuity": BOOL,
            "close_reason": nullable(choices(*CLOSE_REASONS)),
            "pause_reasons": PAUSE_REASONS,
        },
        "meeting.capture.warning": {
            **CAPTURE,
            "code": choices("gaps", "cpu", "late-read"),
            "value": {"type": "number", "minimum": 0},
        },
    },
    "meeting-transcribe": {
        "meeting.job.progress": {
            **IDENTITY,
            "stage": choices("vad", "asr", "diar", "link", "reconcile", "finalize"),
            "done_ms": MILLISECONDS,
            "total_ms": MILLISECONDS,
        },
        "meeting.job.utterance": {
            **IDENTITY,
            "seq": UINT,
            **UTTERANCE["properties"],
            "provisional": {"type": "boolean", "const": True},
        },
        "meeting.job.labels": {
            **IDENTITY,
            "seq": UINT,
            "items": array(tuple_schema(IDENTIFIER, LABEL), 2000),
            "final": BOOL,
            "speakers_total": UINT,
        },
        "meeting.job.waiting": {
            **IDENTITY,
            "reason": choices("memory"),
            "need_mb": UINT,
            "available_mb": UINT,
        },
        "meeting.job.done": {
            **IDENTITY,
            "utterances": UINT,
            "speakers": UINT,
            "diar_status": choices("done", "skipped", "failed"),
            "wall_ms": UINT,
            "peak_rss_kb": UINT,
            "echo_dropped": UINT,
            "transcript_revision": UINT,
        },
        "meeting.job.failed": {
            **IDENTITY,
            "code": choices("model", "memory", "limit", "damaged", "io", "cancelled-by-delete"),
        },
        "meeting.job.replace": {
            **IDENTITY,
            "seq": UINT,
            "replaces_id": IDENTIFIER,
            "expected_revision": UINT,
            "items": array(UTTERANCE, 2000),
        },
    },
    "meeting-diar": {
        "diar.block.part": {
            **IDENTITY,
            "block_index": UINT,
            "seq": UINT,
            "last": BOOL,
            "segments": TURNS,
        }
    },
}
OPTIONAL.update(
    {
        "meeting.capture.track": {"label": text_schema(120)},
        "meeting.capture.alive": {"reason": choices("lease", "indicators")},
        "meeting.job.progress": {"block": UINT, "blocks": UINT},
    }
)
ERROR_CODES = choices(
    "bad-field",
    "bad-frame",
    "unknown-message",
    "not-prepared",
    "admission-stale",
    "req-conflict",
    "hardening-failed",
    "audio-no-device",
    "audio-busy",
    "audio-failed",
    "target-gone",
    "target-mismatch",
    "dir-unwritable",
    "limit",
    "model",
    "memory",
    "io",
    "timeout",
    "hello-timeout",
    "protocol-mismatch",
    "worker-crashed",
    "load-timeout",
    "restart-limit",
    "worker-start",
)


def _schema(
    kind: str, fields: dict[str, Any], optional: dict[str, Any] | None = None
) -> dict[str, Any]:
    return obj({"type": {"const": kind}, **fields, **(optional or {})}, required=["type", *fields])


def schemas(role: str) -> dict[str, dict[str, Any]]:
    if role not in ROLES:
        raise ValueError("meeting-role-invalid")
    commands = COMMAND_FIELDS[role]
    result = {
        kind: _schema(
            kind, {"req": IDENTIFIER, "request_type": {"const": kind}, **fields}, OPTIONAL.get(kind)
        )
        for kind, fields in commands.items()
    }
    for kind, allowed in REPLIES[role].items():
        result[kind] = _schema(
            kind,
            {"req": IDENTIFIER, "request_type": choices(*allowed), **REPLY_FIELDS[kind]},
            OPTIONAL.get(kind),
        )
    for kind, fields in EVENT_FIELDS[role].items():
        schema = _schema(kind, fields, OPTIONAL.get(kind))
        result[kind] = {"anyOf": [result[kind], schema]} if kind in result else schema
    receipt_variants = []
    error_variants = []
    for request_type, fields in commands.items():
        identity = IDENTITY if "meeting_id" in fields else {}
        capture = {"capture_id": IDENTIFIER, "state_rev": UINT} if "capture_id" in fields else {}
        correlation = {"req": IDENTIFIER, "request_type": {"const": request_type}}
        receipt_variants.append(_schema("meeting.ack", {**correlation, **identity, **capture}))
        error_variants.append(
            _schema(
                "error",
                {**correlation, **identity, "code": ERROR_CODES, "message": text_schema(120)},
            )
        )
    result["meeting.ack"] = {"anyOf": receipt_variants}
    result["pong"] = _schema("pong", {"req": IDENTIFIER, "request_type": {"const": "ping"}})
    result["error"] = {
        "anyOf": [
            *error_variants,
            _schema("error", {**IDENTITY, "code": ERROR_CODES, "message": text_schema(120)}),
            _schema("error", {"code": ERROR_CODES, "message": text_schema(120)}),
        ]
    }
    result["hello"] = _schema(
        "hello",
        {
            "family": {"const": FAMILY},
            "protocol": {"const": PROTOCOL, "type": "integer"},
            "build": S,
            "runtime": obj({"python": S, "onnxruntime": nullable(S)}),
        },
    )
    return result


SCHEMAS = {role: schemas(role) for role in ROLES}


def _clean(value: Any, *, speech: bool = False) -> Any:
    if isinstance(value, str):
        text = normalize_text(value, 32768, multiline=speech)
        if speech and len(text.encode("utf-8")) > 32768:
            raise ValueError("meeting-text-limit")
        return text
    if isinstance(value, list):
        return [_clean(item, speech=speech) for item in value]
    if isinstance(value, dict):
        result: dict[str, Any] = {
            key: _clean(item, speech=key == "text") for key, item in value.items()
        }
        for start, end in (("start_ms", "end_ms"), ("sample_start", "sample_end")):
            if start in result and end in result and result[start] > result[end]:
                raise ValueError("meeting-interval-invalid")
        for key in ("segments",):
            if isinstance(result.get(key), list):
                for item in result[key]:
                    if isinstance(item, list) and len(item) == 3 and item[0] > item[1]:
                        raise ValueError("meeting-turn-invalid")
        return result
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("meeting-number-invalid")
    return value


def validate_message(message: dict[str, Any], role: str) -> dict[str, Any]:
    from astra_voice.worker.ipc import BAD_FIELD, PROTOCOL_MISMATCH, UNKNOWN_MESSAGE, FrameError

    if role not in ROLES:
        raise FrameError(PROTOCOL_MISMATCH, "meeting-role-invalid")
    if not isinstance(message, dict) or not isinstance(message.get("type"), str):
        raise FrameError(BAD_FIELD, "meeting-bad-field")
    kind = message["type"]
    if kind == "hello" and (
        message.get("family") != FAMILY
        or type(message.get("protocol")) is not int
        or message.get("protocol") != PROTOCOL
    ):
        raise FrameError(PROTOCOL_MISMATCH, "meeting-protocol-mismatch")
    if kind not in SCHEMAS[role]:
        raise FrameError(UNKNOWN_MESSAGE, "meeting-unknown-message")
    try:
        clean: dict[str, Any] = _clean(message)
        validate(clean, SCHEMAS[role][kind])
        return clean
    except (ValueError, TypeError, UnicodeError, RecursionError, SchemaInvalid, SchemaUnavailable):
        raise FrameError(BAD_FIELD, "meeting-bad-field") from None


def operation_timeout(message: dict[str, Any]) -> float:
    kind = message["type"]
    if kind == "diar.block":
        return min(
            900.0, max(120.0, 3 * float(message["block_s"]) * float(message["expected_rtf"]))
        )
    return {
        "meeting.capture.prepare": 5.0,
        "meeting.capture.open": 3.0,
        "meeting.capture.resume": 3.0,
        "meeting.capture.track.resume": 3.0,
        "meeting.capture.pause": 2.0,
        "meeting.capture.stop": 3.0,
        "meeting.capture.shutdown": 2.0,
        "meeting.job.start": 5.0,
        "model.load": 60.0,
        "diar.load": 60.0,
        "diar.finish": 900.0,
    }.get(kind, 1.0)


class RequestLedger:
    """No eviction within a generation; refuse new work at the memory bound."""

    def __init__(self) -> None:
        self.requests: dict[str, tuple[bytes, list[dict[str, Any]] | None]] = {}
        self.used_bytes = 0
        self.reserved_bytes = 0
        self._terminal_admitted = False
        self._rejected: set[str] = set()
        self.exhausted = False

    def begin(self, message: dict[str, Any]) -> tuple[str, list[dict[str, Any]] | None]:
        if self.exhausted:
            return "exhausted", None
        req = str(message["req"])
        fingerprint = hashlib.sha256(
            json.dumps(message, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        ).digest()
        previous = self.requests.get(req)
        if previous is not None:
            return (
                ("same", deepcopy(previous[1]))
                if previous[0] == fingerprint
                else ("conflict", None)
            )
        terminal = message["type"] == "meeting.capture.shutdown" and not self._terminal_admitted
        if (
            len(self.requests) - len(self._rejected) >= MAX_REQUESTS
            or self.used_bytes + self.reserved_bytes + _REPLY_RESERVATION > MAX_CACHE_BYTES
        ) and not terminal:
            if len(self._rejected) >= MAX_REJECTIONS:
                self.exhausted = True
                return "exhausted", None
            # Reserve refusal metadata independently of admitted-work capacity.
            # Only fixed fields are retained, not arbitrary request payloads.
            identity: dict[str, Any] = {
                key: message[key]
                for key in ("req", "request_type", "meeting_id", "meeting_generation")
                if key in message
            }
            refusal: dict[str, Any] = {
                "type": "error",
                "code": "limit",
                "message": "meeting-command-failed",
                **identity,
            }
            self.requests[req] = fingerprint, [refusal]
            self._rejected.add(req)
            return "limit", deepcopy([refusal])
        if message["type"] == "meeting.capture.shutdown":
            self._terminal_admitted = True
        self.reserved_bytes += _REPLY_RESERVATION
        self.requests[req] = fingerprint, None
        return "new", None

    def finish(self, req: str, replies: list[dict[str, Any]]) -> None:
        if req in self._rejected:
            raise ValueError("meeting-request-rejected")
        fingerprint, previous = self.requests[req]
        if previous is None:
            self.reserved_bytes -= _REPLY_RESERVATION
        else:
            self.used_bytes -= len(
                json.dumps(previous, ensure_ascii=False, separators=(",", ":")).encode()
            )
        self.used_bytes += len(
            json.dumps(replies, ensure_ascii=False, separators=(",", ":")).encode()
        )
        # Canonical JSON copies prevent callers mutating cached nested outcomes.
        self.requests[req] = fingerprint, json.loads(json.dumps(replies))
