"""Lifecycle edges and finite log/Stats vocabulary; no I/O or logging effects."""

import math
from collections.abc import Mapping
from enum import StrEnum

from astra_voice.meetings import MeetingValidationError


class MeetingState(StrEnum):
    RECORDING = "recording"
    PAUSED = "paused"
    RECORDED = "recorded"
    TRANSCRIBING = "transcribing"
    WAITING_MEMORY = "waiting_memory"
    NEEDS_MODEL = "needs_model"
    DONE = "done"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    DELETING = "deleting"


class ProcessingStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    PARTIAL = "partial"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class PhaseStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"


class PauseReason(StrEnum):
    USER = "user"
    LOCK = "lock"
    UNKNOWN = "unknown"
    LEASE = "lease"
    INDICATORS = "indicators"
    MIC_CHANGE = "mic_change"
    AUDIO_FAILED = "audio_failed"
    DISK = "disk"


_EDGES: dict[MeetingState, frozenset[MeetingState]] = {
    MeetingState.RECORDING: frozenset(
        {MeetingState.PAUSED, MeetingState.RECORDED, MeetingState.INTERRUPTED, MeetingState.FAILED}
    ),
    MeetingState.PAUSED: frozenset(
        {
            MeetingState.RECORDING,
            MeetingState.RECORDED,
            MeetingState.INTERRUPTED,
            MeetingState.FAILED,
        }
    ),
    MeetingState.RECORDED: frozenset({MeetingState.TRANSCRIBING, MeetingState.INTERRUPTED}),
    MeetingState.TRANSCRIBING: frozenset(
        {
            MeetingState.DONE,
            MeetingState.FAILED,
            MeetingState.WAITING_MEMORY,
            MeetingState.NEEDS_MODEL,
            MeetingState.RECORDED,
            MeetingState.INTERRUPTED,
        }
    ),
    MeetingState.WAITING_MEMORY: frozenset(
        {
            MeetingState.TRANSCRIBING,
            MeetingState.RECORDED,
            MeetingState.NEEDS_MODEL,
            MeetingState.FAILED,
        }
    ),
    MeetingState.NEEDS_MODEL: frozenset({MeetingState.TRANSCRIBING, MeetingState.RECORDED}),
    MeetingState.DONE: frozenset({MeetingState.TRANSCRIBING}),
    MeetingState.FAILED: frozenset({MeetingState.TRANSCRIBING}),
    MeetingState.INTERRUPTED: frozenset({MeetingState.TRANSCRIBING}),
    MeetingState.DELETING: frozenset(),
}


def transition(current: MeetingState, target: MeetingState) -> MeetingState:
    """Validate a lifecycle edge; tombstones never admit new operations."""
    if not isinstance(current, MeetingState) or not isinstance(target, MeetingState):
        raise MeetingValidationError("Недопустимое состояние встречи")
    if current not in _EDGES or (
        target not in _EDGES[current]
        and not (target == MeetingState.DELETING and current != MeetingState.DELETING)
    ):
        raise MeetingValidationError("Недопустимый переход встречи")
    return target


class PolicyCode(StrEnum):
    RECORD_USER = "meeting-policy-record-user"
    RECORD_ADMIN = "meeting-policy-record-admin"
    RECORD_INVALID = "meeting-policy-record-invalid"
    DIAR_USER = "meeting-policy-diar-user"
    DIAR_ADMIN = "meeting-policy-diar-admin"
    DIAR_INVALID = "meeting-policy-diar-invalid"
    ENDPOINT_USER = "meeting-policy-endpoint-user"
    ENDPOINT_ADMIN = "meeting-policy-endpoint-admin"
    ENDPOINT_INVALID = "meeting-policy-endpoint-invalid"
    EXTERNAL_USER = "meeting-policy-external-user"
    EXTERNAL_ADMIN = "meeting-policy-external-admin"
    EXTERNAL_INVALID = "meeting-policy-external-invalid"
    RETENTION_USER = "meeting-policy-retention-user"
    RETENTION_ADMIN = "meeting-policy-retention-admin"
    RETENTION_INVALID = "meeting-policy-retention-invalid"
    NOTICE_USER = "meeting-policy-notice-user"
    NOTICE_ADMIN = "meeting-policy-notice-admin"
    NOTICE_INVALID = "meeting-policy-notice-invalid"
    PLAINTEXT_USER = "meeting-policy-plaintext-user"
    PLAINTEXT_ADMIN = "meeting-policy-plaintext-admin"
    PLAINTEXT_INVALID = "meeting-policy-plaintext-invalid"
    INVALID = "meeting-policy-invalid"
    UNSAFE = "meeting-policy-unsafe"
    UNKNOWN_KEY = "meeting-policy-unknown-key"
    SECURE = "meeting-policy-plaintext-secure"
    HTTP_DENIED = "meeting-policy-http-denied"
    USER_SETTING = "meeting-policy-user-setting"


class SettingKey(StrEnum):
    ENABLED = "meetings_enabled"
    OFFER = "meeting_offer_on_call"
    TRANSCRIBE = "meeting_transcribe_immediately"
    DIARIZATION = "meeting_diarization"
    AUDIO_RETENTION = "meeting_audio_retention_days"
    RETENTION = "meeting_retention_days"
    NOTICE = "meeting_notice_text"
    INVITE = "meeting_invite_text"
    SELF_LABEL = "meeting_self_label"
    ENDPOINT = "meeting_ai_endpoint"
    MODEL = "meeting_ai_model"
    REPLACE_NAMES = "meeting_ai_replace_names"
    UNKNOWN = "unknown"


class ExceptionClass(StrEnum):
    JSON = "JSONDecodeError"
    REQUEST = "RequestException"
    HTTP = "HTTPError"
    CONNECTION = "ConnectionError"
    TIMEOUT = "Timeout"
    CONNECT_TIMEOUT = "ConnectTimeout"
    READ_TIMEOUT = "ReadTimeout"
    RUNTIME = "RuntimeError"
    VALUE = "ValueError"
    TYPE = "TypeError"
    OS = "OSError"
    PERMISSION = "PermissionError"
    MEMORY = "MemoryError"
    ENGINE = "EngineError"
    EXCEPTION = "Exception"


class DetectorPurpose(StrEnum):
    OBSERVE = "observe"
    OFFER = "offer"
    CARD = "card"
    START = "start"


class DetectorCode(StrEnum):
    IDENTITY_MISMATCH = "identity_mismatch"
    IDENTITY_MISMATCH_COUNTER = "identity_mismatch_counter"
    VERIFICATION_UNAVAILABLE = "verification=unavailable"
    VERIFICATION_FAILED = "verification=failed"
    PULSE_FAILED = "pulse_failed"
    PICTURE_CHANGED = "picture_changed"
    BUDGET = "budget"
    BUSY = "busy"
    RATE = "rate"
    REMOTE = "remote"
    TARGET_GONE = "target-gone"
    TARGET_MISMATCH = "target-mismatch"
    NO_CALL = "no_call"


# Constant templates are the only messages accepted in meeting-scoped logs.
# POLICY_REJECTED preserves the existing finite policy code vocabulary exactly.
POLICY_REJECTED = "%s"
SETTING_INVALID = "meeting-setting-invalid setting=%s"
EXCEPTION = "meeting-exception exception=%s"
INVALID_LOG = "meeting-invalid-log"
CATALOG_LOAD_FAILED = "meeting-catalog-load-failed"
CAPTURE_STATS = "meeting-capture duration_ms=%d bytes=%d"
NATIVE_STDERR = "meeting-native-stderr bytes=%d lines=%d"
DETECTOR_CODE = "meeting-detector purpose=%s code=%s duration_ms=%d"
START_LATENCY = "meeting-start origin=%s outcome=%s latency_ms=%d"
START_FAILED = "meeting-start-failed code=%s request=%s"


class StartOrigin(StrEnum):
    OFFER = "offer"
    CARD = "card"


class StartOutcome(StrEnum):
    STARTED = "started"
    CALL_LOST = "call-lost"
    FAILED = "start-failed"
    ABORTED = "aborted"


class StartFailureCode(StrEnum):
    # Wire vocabulary from worker.meeting_contract.ERROR_CODES.
    BAD_FIELD = "bad-field"
    BAD_FRAME = "bad-frame"
    UNKNOWN_MESSAGE = "unknown-message"
    NOT_PREPARED = "not-prepared"
    ADMISSION_STALE = "admission-stale"
    REQ_CONFLICT = "req-conflict"
    HARDENING_FAILED = "hardening-failed"
    AUDIO_NO_DEVICE = "audio-no-device"
    AUDIO_BUSY = "audio-busy"
    AUDIO_FAILED = "audio-failed"
    TARGET_GONE = "target-gone"
    TARGET_MISMATCH = "target-mismatch"
    DIR_UNWRITABLE = "dir-unwritable"
    LIMIT = "limit"
    MODEL = "model"
    MODEL_UNTRUSTED = "model-untrusted"
    EXTERNAL_DATA = "external-data"
    MEMORY = "memory"
    IO = "io"
    TIMEOUT = "timeout"
    HELLO_TIMEOUT = "hello-timeout"
    PROTOCOL_MISMATCH = "protocol-mismatch"
    WORKER_CRASHED = "worker-crashed"
    LOAD_TIMEOUT = "load-timeout"
    RESTART_LIMIT = "restart-limit"
    WORKER_START = "worker-start"
    OTHER = "other"

    @classmethod
    def _missing_(cls, value: object) -> "StartFailureCode":
        return cls.OTHER


class StartFailureRequest(StrEnum):
    # Wire vocabulary from worker.meeting_contract.COMMAND_FIELDS.
    DETECTOR_START = "meeting.detector.start"
    DETECTOR_STOP = "meeting.detector.stop"
    CALL_QUERY = "meeting.call.query"
    CAPTURE_PREPARE = "meeting.capture.prepare"
    CAPTURE_OPEN = "meeting.capture.open"
    CAPTURE_KEEPALIVE = "meeting.capture.keepalive"
    CAPTURE_PAUSE = "meeting.capture.pause"
    CAPTURE_RESUME = "meeting.capture.resume"
    CAPTURE_TRACK_RESUME = "meeting.capture.track.resume"
    CAPTURE_EXTEND = "meeting.capture.extend"
    CAPTURE_STOP = "meeting.capture.stop"
    CAPTURE_SHUTDOWN = "meeting.capture.shutdown"
    PING = "ping"
    MODEL_LOAD = "model.load"
    MODEL_UNLOAD = "model.unload"
    MEASURE = "measure"
    JOB_START = "meeting.job.start"
    JOB_CREDIT = "meeting.job.credit"
    JOB_CANCEL = "meeting.job.cancel"
    JOB_APPLIED = "meeting.job.applied"
    DIAR_LOAD = "diar.load"
    DIAR_BLOCK = "diar.block"
    DIAR_FINISH = "diar.finish"
    DIAR_CANCEL = "diar.cancel"
    OTHER = "other"

    @classmethod
    def _missing_(cls, value: object) -> "StartFailureRequest":
        return cls.OTHER


LOG_FIELDS: dict[str, tuple[type[StrEnum] | None, ...]] = {
    POLICY_REJECTED: (PolicyCode,),
    SETTING_INVALID: (SettingKey,),
    EXCEPTION: (ExceptionClass,),
    INVALID_LOG: (),
    CATALOG_LOAD_FAILED: (),
    CAPTURE_STATS: (None, None),
    NATIVE_STDERR: (None, None),
    DETECTOR_CODE: (DetectorPurpose, DetectorCode, None),
    START_LATENCY: (StartOrigin, StartOutcome, None),
    START_FAILED: (StartFailureCode, StartFailureRequest),
}


def policy_code(value: object) -> PolicyCode:
    return value if isinstance(value, PolicyCode) else PolicyCode.INVALID


def setting_key(value: object) -> SettingKey:
    return next((key for key in SettingKey if key.value == value), SettingKey.UNKNOWN)


def exception_kind(error: BaseException | None) -> ExceptionClass:
    """Never call str/repr or inspect exception bodies/URLs/traceback frames."""
    name = type(error).__name__
    try:
        return ExceptionClass(name)
    except ValueError:
        return ExceptionClass.EXCEPTION


def is_number(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and type(value) is not bool
        and 0 <= value <= 2**64 - 1
        and (type(value) is int or math.isfinite(value))
    )


def number(value: object) -> int | float:
    return value if isinstance(value, (int, float)) and is_number(value) else 0


def log_arguments(message: object, args: object) -> tuple[object, ...] | None:
    if type(message) is not str or message not in LOG_FIELDS or not isinstance(args, tuple):
        return None
    fields = LOG_FIELDS[message]
    if len(args) != len(fields):
        return None
    result: list[object] = []
    for value, kind in zip(args, fields, strict=True):
        if kind is None:
            if not is_number(value):
                return None
            result.append(value)
        elif not isinstance(value, kind):
            return None
        else:
            result.append(value)
    return tuple(result)


STATS_TYPES = frozenset(
    {
        "meeting_capture",
        "meeting_transcribe",
        "meeting_diar",
        "meeting_ai",
        "meeting_detector",
        "meeting_policy",
        "meeting_lifecycle",
        "meeting_unknown",
    }
)
STATS_NUMBERS = frozenset(
    {
        "duration_ms",
        "audio_ms",
        "recorded_ms",
        "wall_ms",
        "infer_ms",
        "bytes",
        "frames",
        "segments",
        "gaps",
        "lost_estimate_ms",
        "utterances",
        "speakers",
        "free_mb",
        "cpu_pct",
        "need_mb",
        "available_mb",
        "count",
        "seq",
        "stderr_bytes",
        "stderr_lines",
    }
)
STATS_CHOICES = {
    "app_class": frozenset({"dion", "browser", "other"}),
    "result": frozenset(
        {"ok", "failed", "cancelled", "partial", "skipped", "succeeded", "unknown"}
    ),
    "state": frozenset(MeetingState),
    "reason": frozenset(PauseReason) | {"memory", "timeout", "limit", "unknown"},
    "stage": frozenset({"vad", "asr", "diar", "link", "reconcile", "finalize"}),
    "track": frozenset({"mic", "far"}),
    "endpoint_class": frozenset({"off", "internal", "external", "unset"}),
    "exception": frozenset(ExceptionClass),
    "code": frozenset(PolicyCode)
    | frozenset(DetectorCode)
    | {
        "timeout",
        "limit",
        "not-prepared",
        "admission-stale",
        "req-conflict",
        "hardening-failed",
        "unknown",
        "meeting-exception",
        "meeting-invalid-log",
    },
}


def stats_event(
    event_type: str, fields: Mapping[str, object]
) -> dict[str, str | int | float | bool]:
    """Drop all unknown strings, including unknown event names, without raising."""
    result: dict[str, str | int | float | bool] = {
        "type": event_type
        if type(event_type) is str and event_type in STATS_TYPES
        else "meeting_unknown"
    }
    for key, value in fields.items():
        if type(key) is not str:
            continue
        if key in STATS_NUMBERS and is_number(value) and isinstance(value, (int, float)):
            result[key] = value
        elif key in STATS_CHOICES:
            if type(value) is str and value in STATS_CHOICES[key]:
                result[key] = value
            elif (
                type(value) in {MeetingState, PauseReason, ExceptionClass, PolicyCode, DetectorCode}
                and isinstance(value, StrEnum)
                and value in STATS_CHOICES[key]
            ):
                result[key] = value.value
    return result
