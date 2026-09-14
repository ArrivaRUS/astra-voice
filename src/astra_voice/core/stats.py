"""Локальная статистика по PRD §10: только разрешённые поля, последние 1000 событий."""

from __future__ import annotations

import json
import logging
import math
import os
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict

from astra_voice.core.paths import state_dir

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1
MAX_EVENTS = 1000
SAVE_INTERVAL_S = 30.0
Event = dict[str, str | int | float | bool]

_FIELDS: dict[str, frozenset[str]] = {
    "dictation": frozenset({"model_id", "audio_ms", "t_ms", "paste_ms", "cold", "result"}),
    "model_measure": frozenset({"model_id", "revision", "peak_rss_mb", "threads", "cpu"}),
    "mic_error": frozenset({"kind", "recovered_by"}),
    "update_check": frozenset({"source", "result"}),
    "update_apply": frozenset({"kind", "track", "result"}),
    "onboarding": frozenset({"step_reached", "duration_s"}),
}
_CHOICES = {
    ("dictation", "result"): ("ok", "empty", "cancelled"),
    ("mic_error", "kind"): ("none", "busy", "silent", "other"),
    ("mic_error", "recovered_by"): ("restart_wireplumber", "retry", "none"),
    ("update_check", "source"): ("models", "app", "catalog"),
    ("update_check", "result"): ("ok", "none", "unavailable", "ratelimit"),
    ("update_apply", "kind"): ("model", "app"),
    ("update_apply", "track"): ("A", "B", "file"),
}
_NUMBERS = frozenset({"audio_ms", "t_ms", "paste_ms", "peak_rss_mb", "duration_s"})


class Summary(TypedDict):
    """Счётчики всех диктовок, скорость прогретых; доли результатов от 0 до 1."""

    dictations: int
    p50_ms: float | None
    p95_ms: float | None
    results: dict[str, int]
    result_shares: dict[str, float]
    mic_errors: int


def percentile(values: Sequence[float], q: float) -> float | None:
    """Ближайший ранг: sorted(values)[ceil(n * q / 100) - 1], без интерполяции.

    q задаётся в процентах от 0 до 100 включительно; для q=0 берётся минимум.
    Пустой набор даёт None. Неверный q и неконечные значения вызывают ValueError.
    Исходная последовательность не изменяется.
    """
    if not math.isfinite(q) or not 0 <= q <= 100:
        raise ValueError("Процент должен быть от 0 до 100")
    if not values:
        return None
    if any(not math.isfinite(value) for value in values):
        raise ValueError("Значения должны быть конечными числами")
    ordered = sorted(values)
    return float(ordered[max(0, math.ceil(len(ordered) * q / 100) - 1)])


def _number(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and value >= 0
        and math.isfinite(value)
    )


def _event(event_type: str, fields: Mapping[str, object]) -> Event:
    if event_type not in _FIELDS:
        raise ValueError("Неизвестный вид события статистики")
    event: Event = {"type": event_type}
    for key, value in fields.items():
        if key not in _FIELDS[event_type]:
            log.warning("неизвестное поле статистики отброшено")
            continue
        choices = _CHOICES.get((event_type, key))
        if choices is not None:
            valid = isinstance(value, str) and value in choices
        elif key in _NUMBERS:
            valid = _number(value)
        elif key == "cold":
            valid = isinstance(value, bool)
        elif key == "threads":
            valid = type(value) is int and value > 0
        elif key == "step_reached":
            valid = isinstance(value, str) or (type(value) is int and value >= 0)
        else:
            valid = isinstance(value, str)
        if not valid or not isinstance(value, (str, int, float, bool)):
            raise ValueError("Неверное значение поля статистики")
        event[key] = value
    return event


@dataclass
class _Buffer:
    events: list[Event]
    last_save: float
    dirty: bool = False


# Несброшенные данные доступны и новым Stats (в том числе CLI) в этом процессе.
_pending: dict[Path, _Buffer] = {}


class Stats:
    """Хранилище одного процесса; события упорядочены по времени добавления.

    append принимает поля из таблицы PRD §10; type и ts назначаются хранилищем.
    Неизвестные поля отбрасываются, неверные значения вызывают ValueError.
    Методы вызываются из одного потока. append сбрасывает буфер не чаще раза
    в SAVE_INTERVAL_S секунд; при завершении владелец обязан вызвать flush().
    Владелец также вызывает flush() по периодическому таймеру SAVE_INTERVAL_S.
    """

    def __init__(self) -> None:
        self._path = state_dir() / "stats.json"
        pending = _pending.get(self._path)
        self._buffer = pending if pending is not None else _Buffer(self._load(), time.monotonic())

    def _load(self) -> list[Event]:
        try:
            fd = os.open(self._path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            if (
                not isinstance(data, dict)
                or type(data.get("schema_version")) is not int
                or data["schema_version"] != SCHEMA_VERSION
                or not isinstance(data.get("events"), list)
            ):
                raise ValueError("Неверный формат статистики")
            events: list[Event] = []
            for raw in data["events"]:
                if (
                    not isinstance(raw, dict)
                    or not isinstance(raw.get("type"), str)
                    or not _number(raw.get("ts"))
                ):
                    raise ValueError("Неверный формат события")
                event = _event(
                    raw["type"], {k: v for k, v in raw.items() if k not in {"type", "ts"}}
                )
                event["ts"] = float(raw["ts"])
                events.append(event)
            return events[-MAX_EVENTS:]
        except FileNotFoundError:
            return []
        except (OSError, ValueError, OverflowError, RecursionError):
            log.warning("не удалось прочитать статистику, беру пустой набор событий")
            return []

    def _save(self, events: list[Event]) -> None:
        tmp = self._path.with_name(f".{self._path.name}.tmp")
        payload = json.dumps(
            {"schema_version": SCHEMA_VERSION, "events": events},
            ensure_ascii=False,
            allow_nan=False,
        )
        tmp.unlink(missing_ok=True)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                os.fchmod(handle.fileno(), 0o600)
                handle.write(payload + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, self._path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

    def flush(self) -> None:
        """Атомарно сохраняет накопленное в файл 0600; при ошибке буфер остаётся."""
        if not self._buffer.dirty:
            return
        self._save(self._buffer.events)
        self._saved()

    def _saved(self) -> None:
        self._buffer.dirty = False
        self._buffer.last_save = time.monotonic()
        if _pending.get(self._path) is self._buffer:
            del _pending[self._path]

    def append(self, event_type: str, **fields: object) -> None:
        """Добавляет событие с Unix-временем (float), откладывая запись на диск."""
        event = _event(event_type, fields)
        event["ts"] = time.time()
        self._buffer.events.append(event)
        del self._buffer.events[:-MAX_EVENTS]
        self._buffer.dirty = True
        _pending[self._path] = self._buffer
        if time.monotonic() - self._buffer.last_save >= SAVE_INTERVAL_S:
            self.flush()

    def events(self) -> list[Event]:
        """Возвращает копии событий от старых к новым."""
        return [event.copy() for event in self._buffer.events]

    def clear(self) -> None:
        """Атомарно заменяет статистику пустым набором событий."""
        self._save([])
        self._buffer.events.clear()
        self._saved()

    def summary(self) -> Summary:
        """Скорость учитывает только dictation с явно указанным cold=False."""
        dictations = [event for event in self._buffer.events if event["type"] == "dictation"]
        timings = [
            float(event["t_ms"])
            for event in dictations
            if event.get("cold") is False and "t_ms" in event
        ]
        results = {
            result: sum(event.get("result") == result for event in dictations)
            for result in ("ok", "empty", "cancelled")
        }
        return {
            "dictations": len(dictations),
            "p50_ms": percentile(timings, 50),
            "p95_ms": percentile(timings, 95),
            "results": results,
            "result_shares": {
                result: count / len(dictations) if dictations else 0.0
                for result, count in results.items()
            },
            "mic_errors": sum(event["type"] == "mic_error" for event in self._buffer.events),
        }
