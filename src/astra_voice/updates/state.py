"""Состояние проверок обновлений между запусками: даты и выбор пользователя.

Даты попытки и успеха хранятся раздельно (У41, T-36): сетевая ошибка сдвигает
только ``last_attempt_at``, «последний проверенный выпуск» остаётся прежним и
его возраст виден пользователю. Здесь же — «Пропустить эту версию» и
«Напомнить позже» (US-7.6). Ответы сервера сюда не пишутся — они в кэше
проверок (`net/update_cache.py`).
"""

from __future__ import annotations

import json
import logging
import math
import threading
from dataclasses import asdict, dataclass
from pathlib import Path

from astra_voice.net.github import parse_semver
from astra_voice.net.update_cache import read_private, write_private

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1
MAX_STATE_BYTES = 64 * 1024
SOURCES = ("app",)


@dataclass(frozen=True)
class SourceState:
    """Даты последней попытки и успеха, пропущенная версия и срок «напомнить позже»."""

    last_attempt_at: float | None = None
    last_success_at: float | None = None
    skipped_version: str | None = None
    remind_until: float | None = None


def _source(value: object) -> SourceState | None:
    if not isinstance(value, dict) or set(value) != set(SourceState.__dataclass_fields__):
        return None
    for name in ("last_attempt_at", "last_success_at", "remind_until"):
        number = value[name]
        if number is not None and (
            isinstance(number, bool)
            or not isinstance(number, (int, float))
            or not math.isfinite(number)
            or number < 0
        ):
            return None
    skipped = value["skipped_version"]
    if skipped is not None and parse_semver(skipped) is None:
        return None
    return SourceState(**value)


class StateStore:
    """Читает и атомарно заменяет update-state.json (0600)."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        if self._path is None:
            from astra_voice.core.paths import state_dir

            self._path = state_dir() / "update-state.json"
        return self._path

    def _read(self) -> dict[str, SourceState]:
        try:
            parsed: object = json.loads(read_private(self.path, MAX_STATE_BYTES).decode("utf-8"))
            if (
                not isinstance(parsed, dict)
                or set(parsed) != {"schema_version", "sources"}
                or parsed["schema_version"] != SCHEMA_VERSION
                or not isinstance(parsed["sources"], dict)
            ):
                raise ValueError("формат")
            result: dict[str, SourceState] = {}
            for key, value in parsed["sources"].items():
                item = _source(value)
                if key not in SOURCES or item is None:
                    raise ValueError("запись")
                result[key] = item
            return result
        except FileNotFoundError:
            return {}
        except (OSError, ValueError, UnicodeError, TypeError, RecursionError):
            log.warning("Состояние проверок обновлений недоступно или повреждено")
            return {}

    def get(self, source: str) -> SourceState:
        """Состояние источника; нет файла или он испорчен — пустое состояние."""
        with self._lock:
            return self._read().get(source, SourceState())

    def set(self, source: str, state: SourceState) -> None:
        """Сохраняет состояние источника; ошибка записи только в журнал."""
        if source not in SOURCES:
            raise ValueError("Неизвестный источник обновлений")
        if _source(asdict(state)) is None:
            raise ValueError("Неверное состояние проверки")
        with self._lock:
            sources = self._read()
            sources[source] = state
            raw = json.dumps(
                {
                    "schema_version": SCHEMA_VERSION,
                    "sources": {name: asdict(item) for name, item in sources.items()},
                },
                separators=(",", ":"),
            ).encode("utf-8")
            try:
                write_private(self.path, raw)
            except OSError:
                log.warning("Не удалось сохранить состояние проверок обновлений")
