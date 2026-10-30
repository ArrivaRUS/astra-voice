"""Журнал приложения.

Инвариант безопасности §6.4: распознанный текст не попадает в журнал никогда.
Фильтр :class:`RedactTextFilter` вырезает секретные поля из любой записи.
Для кода встреч дополнительно допускаются только события из конечной схемы,
без свободного текста, traceback и тела исключения.
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from astra_voice.meetings import events as meeting_events

LOG_FILE_NAME = "astra-voice.log"
MAX_BYTES = 1024 * 1024
BACKUP_COUNT = 4  # итого 5 файлов по 1 МБ
REDACTED = "<вырезано>"
SECRET_FIELDS = frozenset(
    {
        "text",
        "recognized_text",
        "transcript",
        "meeting_title",
        "speaker_name",
        "notice_text",
        "api_key",
        "authorization",
        "prompt",
        "completion",
        "segment_text",
    }
)
_RECORD_FIELDS = frozenset(logging.makeLogRecord({}).__dict__) | {"message", "asctime"}
_MEETING_LOGGERS = frozenset(
    {
        f"astra_voice.meetings.{name}"
        for name in (
            "apps",
            "events",
            "model",
            "policy",
            "segments",
            "settings_ext",
            "store",
            "timeline",
            "engine",
        )
    }
    | {
        f"astra_voice.worker.meeting_{name}"
        for name in ("capture", "transcribe", "diar", "contract", "roles")
    }
    | {"astra_voice.meetings"}
)


def _meeting_record(record: logging.LogRecord) -> bool:
    return (
        record.name == "astra_voice.meetings"
        or record.name.startswith(("astra_voice.meetings.", "astra_voice.worker.meeting_"))
        or "/astra_voice/meetings/" in record.pathname.replace("\\", "/")
        or "/astra_voice/worker/meeting_" in record.pathname.replace("\\", "/")
    )


def _guard_meeting_record(record: logging.LogRecord) -> None:
    error: BaseException | None = None
    if record.exc_info and isinstance(record.exc_info, tuple) and len(record.exc_info) > 1:
        error = record.exc_info[1]
    if error is None and isinstance(record.args, tuple):
        error = next((arg for arg in record.args if isinstance(arg, BaseException)), None)
    arguments = meeting_events.log_arguments(record.msg, record.args)
    if error is not None:
        record.msg = meeting_events.EXCEPTION
        record.args = (meeting_events.exception_kind(error),)
    elif arguments is None:
        record.msg = meeting_events.INVALID_LOG
        record.args = ()
    else:
        record.args = arguments
    record.exc_info = None
    record.exc_text = None
    record.stack_info = None
    record.__dict__.pop("message", None)
    if record.name not in _MEETING_LOGGERS:
        record.name = "astra_voice.meetings"
    for key in record.__dict__.keys() - _RECORD_FIELDS:
        record.__dict__[key] = REDACTED
    record.session_kind = "MEETING"


FORMAT = "%(asctime)s %(levelname)-7s [%(session_kind)s] %(name)s: %(message)s"


def _redact_nested(value: Any, active: set[int], depth: int = 0) -> Any:
    """Copy builtin containers without formatting objects or mutating callers.

    Cycles and excessive nesting are cut rather than rendered. Container
    subclasses are refused: their iteration hooks need not behave like builtins.
    Scalar arguments retain the legacy logger's formatting behavior.
    """
    if not isinstance(value, (dict, list, tuple)):
        return value
    if type(value) not in (dict, list, tuple) or depth >= 64 or id(value) in active:
        return REDACTED
    active.add(id(value))
    try:
        if type(value) is dict:
            return {
                key: REDACTED
                if isinstance(key, str) and key in SECRET_FIELDS
                else _redact_nested(item, active, depth + 1)
                for key, item in value.items()
            }
        items = [_redact_nested(item, active, depth + 1) for item in value]
        return tuple(items) if type(value) is tuple else items
    finally:
        active.remove(id(value))


class RedactTextFilter(logging.Filter):
    """Убирает распознанный текст из атрибутов записи и из ``record.args``."""

    def filter(self, record: logging.LogRecord) -> bool:
        if _meeting_record(record):
            _guard_meeting_record(record)
        for field in record.__dict__.keys() - _RECORD_FIELDS:
            record.__dict__[field] = (
                REDACTED
                if field in SECRET_FIELDS
                else _redact_nested(record.__dict__[field], set())
            )
        record.args = _redact_nested(record.args, set())
        return True


class SessionKindFilter(logging.Filter):
    """Добавляет в каждую запись вид сессии (KDE/FLY/OTHER)."""

    def __init__(self, session_kind: str) -> None:
        super().__init__()
        self.session_kind = session_kind

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "session_kind"):
            record.session_kind = self.session_kind
        return True


def _clear_own_handlers(logger: logging.Logger) -> None:
    for handler in list(logger.handlers):
        if getattr(handler, "_astra_voice", False):
            logger.removeHandler(handler)
            handler.close()


def setup_logging(
    session_kind: str = "OTHER",
    *,
    debug: bool = False,
    directory: Path | None = None,
) -> logging.Logger:
    """Настраивает корневой логгер: файл с ротацией 5×1 МБ + stderr."""
    if directory is None:
        from astra_voice.core.paths import log_dir

        directory = log_dir()
    directory.mkdir(parents=True, exist_ok=True)

    level = logging.DEBUG if debug else logging.INFO
    formatter = logging.Formatter(FORMAT)
    redact = RedactTextFilter()
    session = SessionKindFilter(session_kind)

    root = logging.getLogger()
    _clear_own_handlers(root)
    root.setLevel(level)
    for logger_name in ("urllib3", "requests"):
        logging.getLogger(logger_name).setLevel(logging.WARNING)

    file_handler = RotatingFileHandler(
        directory / LOG_FILE_NAME,
        maxBytes=MAX_BYTES,
        backupCount=BACKUP_COUNT,
        encoding="utf-8",
    )
    stream_handler = logging.StreamHandler(sys.stderr)
    stream_handler.setLevel(level if debug else logging.WARNING)
    for handler in (file_handler, stream_handler):
        handler.setFormatter(formatter)
        handler.addFilter(redact)
        handler.addFilter(session)
        setattr(handler, "_astra_voice", True)  # noqa: B010 — метка своего обработчика
        root.addHandler(handler)
    return root
