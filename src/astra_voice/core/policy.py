"""Административная политика ``/etc/astra-voice/policy.conf`` (минимум для M1).

Файл только читается. Ключи, заданные администратором, перекрывают настройки
пользователя и блокируют соответствующие строки интерфейса. Полная проверка
по требованию У37 — milestone M7.

Три состояния: файла нет (``absent``), прочитан (``ok``), есть, но разобрать
не удалось (``invalid``). Невалидный файл действует как отсутствующий, но
статус попадает в журнал и в окно «О программе».
"""

from __future__ import annotations

import configparser
import dataclasses
import logging
from collections.abc import Mapping
from dataclasses import dataclass, fields
from enum import StrEnum
from pathlib import Path
from typing import Any

from astra_voice.core.settings import Settings

log = logging.getLogger(__name__)

POLICY_PATH = Path("/etc/astra-voice/policy.conf")
SECTION = "astra-voice"
LOCKED_KEY = "locked"
_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


class PolicyStatus(StrEnum):
    ABSENT = "absent"
    OK = "ok"
    INVALID = "invalid"


@dataclass(frozen=True)
class Policy:
    values: Mapping[str, Any] = dataclasses.field(default_factory=dict)
    locked_keys: frozenset[str] = frozenset()
    status: PolicyStatus = PolicyStatus.ABSENT

    def is_locked(self, key: str) -> bool:
        return key in self.locked_keys


_SETTINGS_TYPES = {f.name: f.type for f in fields(Settings) if f.name != "extra"}


def _coerce(key: str, raw: str) -> Any:
    """Приводит строку INI к типу одноимённой настройки; иначе оставляет строкой."""
    text = raw.strip()
    default = getattr(Settings(), key, None) if key in _SETTINGS_TYPES else None
    if isinstance(default, bool):
        low = text.lower()
        if low in _TRUE:
            return True
        if low in _FALSE:
            return False
        raise ValueError(f"{key}: ожидалось логическое значение, получено {raw!r}")
    if isinstance(default, int):
        return int(text)
    return text


def load(path: Path | None = None) -> Policy:
    """Читает политику. Ошибка разбора → статус ``invalid`` и пустые значения."""
    target = POLICY_PATH if path is None else path
    if not target.exists():
        return Policy(status=PolicyStatus.ABSENT)
    parser = configparser.ConfigParser(interpolation=None)
    try:
        text = target.read_text(encoding="utf-8")
        parser.read_string(text, source=str(target))
    except (OSError, UnicodeDecodeError, configparser.Error) as exc:
        log.warning("политика %s не разобрана (%s), считаю её отсутствующей", target, exc)
        return Policy(status=PolicyStatus.INVALID)

    raw: dict[str, str] = {}
    if parser.has_section(SECTION):
        raw.update(parser[SECTION])
    else:
        raw.update(parser.defaults())
    if not raw:
        log.warning("политика %s без секции [%s], считаю её отсутствующей", target, SECTION)
        return Policy(status=PolicyStatus.INVALID)

    explicit = {k.strip() for k in raw.pop(LOCKED_KEY, "").replace(",", " ").split() if k.strip()}
    values: dict[str, Any] = {}
    for key, value in raw.items():
        try:
            values[key] = _coerce(key, value)
        except ValueError as exc:
            log.warning("политика %s: %s, считаю её невалидной", target, exc)
            return Policy(status=PolicyStatus.INVALID)
    return Policy(
        values=values,
        locked_keys=frozenset(values) | explicit,
        status=PolicyStatus.OK,
    )


def effective(settings: Settings, policy: Policy) -> Settings:
    """Возвращает копию настроек с наложенными значениями политики."""
    result = dataclasses.replace(settings, extra=dict(settings.extra))
    for key, value in policy.values.items():
        if key in _SETTINGS_TYPES:
            setattr(result, key, value)
        else:
            result.extra[key] = value
    return result
