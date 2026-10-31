"""Offline meeting data contracts (meeting IPC belongs to a separate family)."""


class MeetingValidationError(ValueError):
    """Invalid meeting data; messages never include user content or paths."""
