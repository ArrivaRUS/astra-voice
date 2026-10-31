"""Schema 1 disk models, R5 additions and BL-3 text/numeric checkpoints.

Unknown disk fields are rejected at every level. NFC precedes length checks.
Cc/Cf (and lone surrogates) are forbidden; only utterance text admits LF/TAB.
No acoustic arrays, binary blobs, executable payloads or file paths are accepted.
"""

from __future__ import annotations

import json
import math
import re
import types
import unicodedata
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, ClassVar, Self, Union, get_args, get_origin, get_type_hints

from astra_voice.meetings import MeetingValidationError as MeetingValidationError
from astra_voice.meetings.events import MeetingState, PhaseStatus, ProcessingStatus
from astra_voice.models.schema import SchemaInvalid, SchemaUnavailable, validate

MAX_MEETING_MS = 14_400_000
MAX_DOCUMENT_BYTES = 32 * 1024 * 1024
UINT_MAX = 2**64 - 1
MEETING_ID_PATTERN = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}_[0-9]{6}-[a-z0-9]{4}$"
ID_PATTERN = r"^[A-Za-z0-9_-]{1,64}$"
LABEL_PATTERN = r"^(me|others|S[1-9][0-9]*)$"
Schema = dict[str, Any]
UINT: Schema = {"type": "integer", "minimum": 0, "maximum": UINT_MAX}
MILLISECONDS: Schema = {"type": "integer", "minimum": 0, "maximum": MAX_MEETING_MS}
BOOL: Schema = {"type": "boolean"}
VERSION: Schema = {"type": "integer", "const": 1}
TIMESTAMP: Schema = {"type": "string", "format": "date-time", "maxLength": 40}
MEETING_ID: Schema = {"type": "string", "pattern": MEETING_ID_PATTERN}
IDENTIFIER: Schema = {"type": "string", "pattern": ID_PATTERN}
HASH: Schema = {"type": "string", "pattern": r"^[a-f0-9]{64}$"}
LABEL: Schema = {"type": "string", "pattern": LABEL_PATTERN}


def text_schema(limit: int, minimum: int = 0) -> Schema:
    return {"type": "string", "minLength": minimum, "maxLength": limit}


def choices(*values: str) -> Schema:
    return {"type": "string", "enum": list(values)}


def nullable(schema: Schema) -> Schema:
    return {"anyOf": [schema, {"type": "null"}]}


def array(schema: Schema, maximum: int, *, unique: bool = False) -> Schema:
    return {"type": "array", "items": schema, "maxItems": maximum, "uniqueItems": unique}


def obj(properties: dict[str, Schema], *, required: list[str] | None = None) -> Schema:
    return {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
        "required": list(properties) if required is None else required,
    }


def mapping(value: Schema, key: Schema = IDENTIFIER, maximum: int = 9000) -> Schema:
    return {
        "type": "object",
        "propertyNames": key,
        "additionalProperties": value,
        "maxProperties": maximum,
    }


def normalize_text(value: str, limit: int, *, multiline: bool = False, minimum: int = 0) -> str:
    normalized = unicodedata.normalize("NFC", value)
    if not minimum <= len(normalized) <= limit or any(
        unicodedata.category(c) in {"Cc", "Cf", "Cs"} and not (multiline and c in "\n\t")
        for c in normalized
    ):
        raise MeetingValidationError("Недопустимое текстовое поле встречи")
    return normalized


def _normalize(value: Any, *, speech: bool = False) -> Any:
    if isinstance(value, str):
        return normalize_text(value, 1_000_000, multiline=speech)
    if value is None or type(value) is bool or type(value) is int:
        return value
    if type(value) is float and math.isfinite(value):
        return value
    if isinstance(value, list):
        return [_normalize(item) for item in value]
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        normalized = {
            normalize_text(key, 128): _normalize(item, speech=key == "text")
            for key, item in value.items()
        }
        if len(normalized) != len(value):
            raise MeetingValidationError("Повтор нормализованного поля")
        return normalized
    raise MeetingValidationError("Недопустимый тип поля встречи")


def _decode(annotation: Any, value: Any) -> Any:
    origin = get_origin(annotation)
    if origin in (types.UnionType, Union):
        if value is None:
            return None
        return _decode(next(arg for arg in get_args(annotation) if arg is not type(None)), value)
    if origin is list:
        return [_decode(get_args(annotation)[0], item) for item in value]
    if origin is dict:
        return {key: _decode(get_args(annotation)[1], item) for key, item in value.items()}
    if isinstance(annotation, type) and issubclass(annotation, JsonModel):
        return annotation._build(value)
    if isinstance(annotation, type) and issubclass(annotation, StrEnum):
        return annotation(value)
    return value


class JsonModel:
    SCHEMA: ClassVar[Schema]

    @classmethod
    def _build(cls, data: dict[str, Any]) -> Self:
        hints = get_type_hints(cls)
        return cls(**{key: _decode(hints[key], value) for key, value in data.items()})

    @classmethod
    def _check(cls, data: dict[str, Any]) -> None:
        """Cross-field checks, recursively including nested dataclasses."""
        hints = get_type_hints(cls)
        for key, value in data.items():
            cls._check_nested(hints[key], value)
        if "start_ms" in data and data["end_ms"] < data["start_ms"]:
            raise MeetingValidationError("Недопустимый временной диапазон")

    @classmethod
    def _check_nested(cls, hint: Any, value: Any) -> None:
        if isinstance(hint, type) and issubclass(hint, JsonModel):
            hint._check(value)
        elif get_origin(hint) is list:
            for item in value:
                cls._check_nested(get_args(hint)[0], item)
        elif get_origin(hint) is dict:
            for item in value.values():
                cls._check_nested(get_args(hint)[1], item)
        elif get_origin(hint) in (types.UnionType, Union) and value is not None:
            cls._check_nested(next(arg for arg in get_args(hint) if arg is not type(None)), value)

    @staticmethod
    def _check_size(data: object) -> None:
        # Structural JSON Schema limits alone cannot express a UTF-8 byte budget.
        # Count encoder chunks without allocating a potentially huge document.
        encoder = json.JSONEncoder(ensure_ascii=False, allow_nan=False)
        size = 1  # trailing LF, shared with the store wire-on-disk encoding
        for chunk in encoder.iterencode(data):
            size += len(chunk.encode("utf-8"))
            if size > MAX_DOCUMENT_BYTES:
                raise MeetingValidationError("Документ встречи слишком велик")

    @classmethod
    def _validated(cls, data: object) -> dict[str, Any]:
        try:
            normalized = _normalize(data)
            cls._check_size(normalized)
            validate(normalized, cls.SCHEMA)
            if not isinstance(normalized, dict):
                raise MeetingValidationError("Недопустимый документ встречи")
            # JSON Schema accepts 1.0 as integer; disk uints must be Python ints.
            cls._integer_types(normalized, cls.SCHEMA)
            cls._check(normalized)
            return normalized
        except (
            SchemaInvalid,
            SchemaUnavailable,
            TypeError,
            ValueError,
            OverflowError,
            RecursionError,
            KeyError,
        ):
            raise MeetingValidationError("Документ встречи не соответствует схеме") from None

    @classmethod
    def _integer_types(cls, value: Any, schema: Schema) -> None:
        if "anyOf" in schema:
            if value is not None:
                cls._integer_types(value, schema["anyOf"][0])
            return
        if schema.get("type") == "integer" and type(value) is not int:
            raise MeetingValidationError("Недопустимое целое число")
        if schema.get("format") == "date-time":
            # Some supported jsonschema installations lack optional RFC3339 extras.
            if (
                re.fullmatch(
                    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
                    r"(?:\.[0-9]+)?(?:Z|[+-][0-9]{2}:[0-9]{2})",
                    value,
                )
                is None
                or datetime.fromisoformat(value).utcoffset() is None
            ):
                raise MeetingValidationError("Недопустимая временная метка")
        if isinstance(value, dict):
            for key, item in value.items():
                child = schema.get("properties", {}).get(
                    key, schema.get("additionalProperties", {})
                )
                if isinstance(child, dict):
                    cls._integer_types(item, child)
        if isinstance(value, list) and isinstance(schema.get("items"), dict):
            for item in value:
                cls._integer_types(item, schema["items"])

    @classmethod
    def from_dict(cls, data: object) -> Self:
        return cls._build(cls._validated(data))

    def to_dict(self) -> dict[str, Any]:
        if not is_dataclass(self):
            raise MeetingValidationError("Недопустимая модель встречи")
        return self._validated(asdict(self))


@dataclass
class Interval(JsonModel):
    t_mono_ms: int = 0
    recorded_ms: int = 0
    SCHEMA = obj({"t_mono_ms": UINT, "recorded_ms": MILLISECONDS})


@dataclass
class Mark(JsonModel):
    at_ms: int = 0
    kind: str = "pause"
    reason: str | None = None
    SCHEMA = obj(
        {
            "at_ms": MILLISECONDS,
            "kind": choices(
                "pause",
                "resume",
                "mic_gap_start",
                "mic_gap_end",
                "far_ended",
                "limit_extended",
                "damaged",
                "indicators_lost",
            ),
            "reason": nullable(
                choices(
                    "user",
                    "lock",
                    "unknown",
                    "lease",
                    "indicators",
                    "mic_change",
                    "audio_failed",
                    "disk",
                    "sleep",
                    "session",
                    "limit",
                    "stop",
                )
            ),
        }
    )


@dataclass
class Track(JsonModel):
    segments: int = 0
    samples: int = 0
    SCHEMA = obj({"segments": {**UINT, "maximum": 9999}, "samples": UINT})


@dataclass
class Tracks(JsonModel):
    mic: Track = field(default_factory=Track)
    far: Track = field(default_factory=Track)
    SCHEMA = obj({"mic": Track.SCHEMA, "far": Track.SCHEMA})


@dataclass
class Audio(JsonModel):
    present: bool = True
    deleted_at: str | None = None
    retain_days: int = 0
    SCHEMA = obj(
        {
            "present": BOOL,
            "deleted_at": nullable(TIMESTAMP),
            "retain_days": {**UINT, "maximum": 365},
        }
    )


@dataclass
class Transcription(JsonModel):
    model_id: str = "unspecified"
    revision: str = "unspecified"
    diarization: bool = False
    diar_status: PhaseStatus = PhaseStatus.PENDING
    finished_at: str | None = None
    wall_ms: int = 0
    peak_rss_kb: int = 0
    SCHEMA = obj(
        {
            "model_id": IDENTIFIER,
            "revision": text_schema(128, 1),
            "diarization": BOOL,
            "diar_status": choices(*PhaseStatus),
            "finished_at": nullable(TIMESTAMP),
            "wall_ms": UINT,
            "peak_rss_kb": UINT,
        }
    )


@dataclass
class Warnings(JsonModel):
    storage_network: bool = False
    audio_server_remote: bool = False
    whole_output: bool = False
    SCHEMA = obj({"storage_network": BOOL, "audio_server_remote": BOOL, "whole_output": BOOL})


@dataclass
class AiAttempt(JsonModel):
    host: str
    host_class: str = "external"
    template: str = "summary"
    at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    SCHEMA = obj(
        {
            "host_class": choices("internal", "external"),
            "host": {
                "type": "string",
                "maxLength": 253,
                "pattern": r"^[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?$",
            },
            "template": IDENTIFIER,
            "at": TIMESTAMP,
        }
    )


@dataclass
class AiHistory(JsonModel):
    sent_attempted: list[AiAttempt] = field(default_factory=list)
    SCHEMA = obj({"sent_attempted": array(AiAttempt.SCHEMA, 1000)})


@dataclass
class Meeting(JsonModel):
    id: str
    started_at: str
    schema: int = 1
    meeting_generation: int = 0
    state: MeetingState = MeetingState.RECORDED
    variant: str = "mic_only"
    app_class: str | None = None
    title: str = "Встреча"
    ended_at: str | None = None
    recorded_ms: int = 0
    limit_ms: int = 7_200_000
    intervals: list[Interval] = field(default_factory=list)
    marks: list[Mark] = field(default_factory=list)
    tracks: Tracks = field(default_factory=Tracks)
    audio: Audio = field(default_factory=Audio)
    transcription: Transcription = field(default_factory=Transcription)
    self_label: str = "Я"
    warnings: Warnings = field(default_factory=Warnings)
    ai: AiHistory = field(default_factory=AiHistory)
    size_bytes: int = 0
    processing_status: ProcessingStatus = ProcessingStatus.PENDING
    cancelled: bool = False
    cleanup_pending: bool = False
    SCHEMA = obj(
        {
            "schema": VERSION,
            "id": MEETING_ID,
            "started_at": TIMESTAMP,
            "meeting_generation": UINT,
            "state": choices(*MeetingState),
            "variant": choices("app_stream", "mic_only", "sink_monitor"),
            "app_class": nullable(choices("dion", "browser", "other")),
            "title": text_schema(120, 1),
            "ended_at": nullable(TIMESTAMP),
            "recorded_ms": MILLISECONDS,
            "limit_ms": {"type": "integer", "enum": [7_200_000, 10_800_000, 14_400_000]},
            "intervals": array(Interval.SCHEMA, 20000),
            "marks": array(Mark.SCHEMA, 20000),
            "tracks": Tracks.SCHEMA,
            "audio": Audio.SCHEMA,
            "transcription": Transcription.SCHEMA,
            "self_label": text_schema(40, 1),
            "warnings": Warnings.SCHEMA,
            "ai": AiHistory.SCHEMA,
            "size_bytes": UINT,
            "processing_status": choices(*ProcessingStatus),
            "cancelled": BOOL,
            "cleanup_pending": BOOL,
        }
    )


@dataclass
class Utterance(JsonModel):
    id: str
    seq: int = 0
    track: str = "mic"
    start_ms: int = 0
    end_ms: int = 0
    text: str = ""
    speaker: str = "me"
    final: bool = False
    echo: bool = False
    SCHEMA = obj(
        {
            "id": IDENTIFIER,
            "seq": UINT,
            "track": choices("mic", "far"),
            "start_ms": MILLISECONDS,
            "end_ms": MILLISECONDS,
            "text": text_schema(65536),
            "speaker": LABEL,
            "final": BOOL,
            "echo": BOOL,
        }
    )


@dataclass
class Transcript(JsonModel):
    meeting_id: str
    schema: int = 1
    meeting_generation: int = 0
    transcript_revision: int = 0
    utterances: list[Utterance] = field(default_factory=list)
    labels_final: bool = False
    SCHEMA = obj(
        {
            "schema": VERSION,
            "meeting_id": MEETING_ID,
            "meeting_generation": UINT,
            "transcript_revision": UINT,
            "utterances": array(Utterance.SCHEMA, 9000),
            "labels_final": BOOL,
        }
    )

    @classmethod
    def _check(cls, data: dict[str, Any]) -> None:
        super()._check(data)
        ids = [u["id"] for u in data["utterances"]]
        seqs = [u["seq"] for u in data["utterances"]]
        if len(set(ids)) != len(ids) or len(set(seqs)) != len(seqs):
            raise MeetingValidationError("Повтор реплики")


@dataclass
class ModelIdentity(JsonModel):
    id: str = "unspecified"
    revision: str = "unspecified"
    variant: str = "int8"
    vad: str = "silero-v5"
    engine: str = "onnx-asr"
    SCHEMA = obj(
        {
            "id": IDENTIFIER,
            "revision": text_schema(128, 1),
            "variant": IDENTIFIER,
            "vad": text_schema(128, 1),
            "engine": text_schema(128, 1),
        }
    )


@dataclass
class AsrCursor(JsonModel):
    done_until_ms: int = 0
    SCHEMA = obj({"done_until_ms": MILLISECONDS})


@dataclass
class AsrProgress(JsonModel):
    mic: AsrCursor = field(default_factory=AsrCursor)
    far: AsrCursor = field(default_factory=AsrCursor)
    SCHEMA = obj({"mic": AsrCursor.SCHEMA, "far": AsrCursor.SCHEMA})


@dataclass
class DiarTurn(JsonModel):
    label: str = "S1"
    start_ms: int = 0
    end_ms: int = 0
    SCHEMA = obj({"label": IDENTIFIER, "start_ms": MILLISECONDS, "end_ms": MILLISECONDS})


@dataclass
class DiarProgress(JsonModel):
    status: PhaseStatus = PhaseStatus.PENDING
    block_ms: int = 1_200_000
    algo: str = "sherpa-pyannote3+3dspeaker-campplus+link-v1"
    threshold: float = 1.05
    tau: float = 0.6
    turns: list[DiarTurn] = field(default_factory=list)
    label_map: dict[str, str] = field(default_factory=dict)
    SCHEMA = obj(
        {
            "status": choices(*PhaseStatus),
            "block_ms": {"type": "integer", "minimum": 1, "maximum": 1_800_000},
            "algo": text_schema(128, 1),
            "threshold": {"type": "number", "minimum": 0, "maximum": 10},
            "tau": {"type": "number", "minimum": 0, "maximum": 1},
            "turns": array(DiarTurn.SCHEMA, 90000),
            "label_map": mapping(IDENTIFIER),
        }
    )


@dataclass
class ReconcileProgress(JsonModel):
    status: PhaseStatus = PhaseStatus.PENDING
    done_ids: list[str] = field(default_factory=list)
    used_ms: int = 0
    budget_ms: int = 0
    speech_ms: int = 0
    SCHEMA = obj(
        {
            "status": choices(*PhaseStatus),
            "done_ids": array(IDENTIFIER, 9000, unique=True),
            "used_ms": MILLISECONDS,
            "budget_ms": MILLISECONDS,
            "speech_ms": MILLISECONDS,
        }
    )

    @classmethod
    def _check(cls, data: dict[str, Any]) -> None:
        super()._check(data)
        if data["used_ms"] > data["budget_ms"] or 100 * data["budget_ms"] > 15 * data["speech_ms"]:
            raise MeetingValidationError("Недопустимый бюджет сверки")


@dataclass
class ProcessingLimits(JsonModel):
    utterances: int = 9000
    blocks: int = 12
    SCHEMA = obj(
        {
            "utterances": {"type": "integer", "minimum": 1, "maximum": 9000},
            "blocks": {"type": "integer", "minimum": 1, "maximum": 12},
        }
    )


@dataclass
class Checkpoint(JsonModel):
    meeting_id: str
    schema: int = 1
    meeting_generation: int = 0
    transcript_revision: int = 0
    input_manifest_sha256: str = "0" * 64
    processing_config_sha256: str = "0" * 64
    model: ModelIdentity = field(default_factory=ModelIdentity)
    asr: AsrProgress = field(default_factory=AsrProgress)
    diar: DiarProgress = field(default_factory=DiarProgress)
    reconcile: ReconcileProgress = field(default_factory=ReconcileProgress)
    limits: ProcessingLimits = field(default_factory=ProcessingLimits)
    SCHEMA = obj(
        {
            "schema": VERSION,
            "meeting_id": MEETING_ID,
            "meeting_generation": UINT,
            "transcript_revision": UINT,
            "input_manifest_sha256": HASH,
            "processing_config_sha256": HASH,
            "model": ModelIdentity.SCHEMA,
            "asr": AsrProgress.SCHEMA,
            "diar": DiarProgress.SCHEMA,
            "reconcile": ReconcileProgress.SCHEMA,
            "limits": ProcessingLimits.SCHEMA,
        }
    )

    def compatible_with(
        self,
        transcript: Transcript,
        *,
        input_manifest_sha256: str,
        processing_config_sha256: str,
        model: ModelIdentity,
    ) -> bool:
        """Data compatibility only; never substitutes for current model trust checks."""
        self.to_dict()
        transcript.to_dict()
        return (
            self.meeting_id == transcript.meeting_id
            and self.meeting_generation == transcript.meeting_generation
            and self.transcript_revision == transcript.transcript_revision
            and self.input_manifest_sha256 == input_manifest_sha256
            and self.processing_config_sha256 == processing_config_sha256
            and self.model.to_dict() == model.to_dict()
        )


@dataclass
class Speaker(JsonModel):
    name: str | None = None
    merged_into: str | None = None
    SCHEMA = obj({"name": nullable(text_schema(60)), "merged_into": nullable(LABEL)}, required=[])


@dataclass
class SpeakerMap(JsonModel):
    schema: int = 1
    labels: dict[str, Speaker] = field(default_factory=dict)
    renumbered: bool = False
    SCHEMA = obj({"schema": VERSION, "labels": mapping(Speaker.SCHEMA, LABEL), "renumbered": BOOL})

    @classmethod
    def _check(cls, data: dict[str, Any]) -> None:
        super()._check(data)
        labels = data["labels"]
        for label in labels:
            seen = {label}
            target = labels[label].get("merged_into")
            while target is not None:
                if target not in labels or target in seen:
                    raise MeetingValidationError("Недопустимое объединение участников")
                seen.add(target)
                target = labels[target].get("merged_into")


MEETING_SCHEMA = Meeting.SCHEMA
TRANSCRIPT_SCHEMA = Transcript.SCHEMA
CHECKPOINT_SCHEMA = Checkpoint.SCHEMA
SPEAKERS_SCHEMA = SpeakerMap.SCHEMA
