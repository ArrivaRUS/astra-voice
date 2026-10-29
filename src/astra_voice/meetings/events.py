"""Persisted lifecycle codes and transitions; no IPC or logging side effects."""

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
