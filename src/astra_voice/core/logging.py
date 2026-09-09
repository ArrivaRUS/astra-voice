"""Журнал приложения.

Инвариант безопасности §6.4: распознанный текст не попадает в журнал никогда.
Фильтр :class:`RedactTextFilter` вырезает поле ``text`` из любой записи.
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

LOG_FILE_NAME = "astra-voice.log"
MAX_BYTES = 1024 * 1024
BACKUP_COUNT = 4  # итого 5 файлов по 1 МБ
REDACTED = "<вырезано>"
SECRET_FIELDS = frozenset({"text", "recognized_text", "transcript"})

FORMAT = "%(asctime)s %(levelname)-7s [%(session_kind)s] %(name)s: %(message)s"


class RedactTextFilter(logging.Filter):
    """Убирает распознанный текст из атрибутов записи и из ``record.args``."""

    def filter(self, record: logging.LogRecord) -> bool:
        for field in SECRET_FIELDS:
            if field in record.__dict__:
                record.__dict__[field] = REDACTED
        if isinstance(record.args, dict):
            args: dict[str, Any] = dict(record.args)
            for field in SECRET_FIELDS & args.keys():
                args[field] = REDACTED
            record.args = args
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
