"""Журнал: ротация, вид сессии, вырезание распознанного текста."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pytest

from astra_voice.core import logging as av_logging

pytestmark = pytest.mark.unit


@pytest.fixture
def log_dir(tmp_path: Path) -> Iterator[Path]:
    yield tmp_path
    root = logging.getLogger()
    av_logging._clear_own_handlers(root)


def test_recognized_text_never_reaches_file(log_dir: Path) -> None:
    av_logging.setup_logging("KDE", directory=log_dir)
    logging.getLogger("test").info(
        "диктовка завершена", extra={"text": "совершенно секретная фраза"}
    )
    logging.shutdown()
    content = (log_dir / av_logging.LOG_FILE_NAME).read_text(encoding="utf-8")
    assert "совершенно секретная фраза" not in content


def test_filter_redacts_all_secret_fields() -> None:
    record = logging.LogRecord("t", logging.INFO, __file__, 1, "msg", None, None)
    record.__dict__["text"] = "тайна"
    record.__dict__["transcript"] = "тайна"
    av_logging.RedactTextFilter().filter(record)
    assert record.__dict__["text"] == av_logging.REDACTED
    assert record.__dict__["transcript"] == av_logging.REDACTED


def test_filter_redacts_dict_args() -> None:
    record = logging.LogRecord("t", logging.INFO, __file__, 1, "%(text)s", None, None)
    record.args = {"text": "тайна", "t_ms": 42}
    av_logging.RedactTextFilter().filter(record)
    assert record.args == {"text": av_logging.REDACTED, "t_ms": 42}


def test_session_kind_in_format(log_dir: Path) -> None:
    av_logging.setup_logging("FLY", directory=log_dir)
    logging.getLogger("test").info("старт")
    logging.shutdown()
    content = (log_dir / av_logging.LOG_FILE_NAME).read_text(encoding="utf-8")
    assert "[FLY]" in content


def test_debug_flag_raises_level(log_dir: Path) -> None:
    av_logging.setup_logging("OTHER", debug=True, directory=log_dir)
    assert logging.getLogger().level == logging.DEBUG
    av_logging.setup_logging("OTHER", directory=log_dir)
    assert logging.getLogger().level == logging.INFO


def test_rotation_settings(log_dir: Path) -> None:
    root = av_logging.setup_logging("KDE", directory=log_dir)
    rotating = [h for h in root.handlers if isinstance(h, RotatingFileHandler)]
    assert len(rotating) == 1
    assert rotating[0].maxBytes == 1024 * 1024
    assert rotating[0].backupCount == 4


def test_setup_is_idempotent(log_dir: Path) -> None:
    av_logging.setup_logging("KDE", directory=log_dir)
    before = len(logging.getLogger().handlers)
    av_logging.setup_logging("KDE", directory=log_dir)
    assert len(logging.getLogger().handlers) == before
