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
import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass, fields
from enum import StrEnum
from pathlib import Path
from typing import Any

from astra_voice.core.settings import Settings, is_valid_combo

log = logging.getLogger(__name__)

POLICY_PATH = Path("/etc/astra-voice/policy.conf")
APPIMAGE_DENIED_MESSAGE = (
    "Администратор запретил эту версию программы на компьютере. Используйте системную версию."
)
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
    # Только диагностика раскладки: не меняет статус, значения и блокировки.
    permissions_warning: str = ""

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


OFFLINE_KEY = "offline"


def _offline_value(raw: str, target: Path) -> bool:
    """offline= из политики: непонятное значение — офлайн (безопасная сторона), не ошибка файла."""
    text = raw.strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    log.warning("политика %s: непонятное значение offline, включаю работу без сети", target)
    return True


def _permissions_warning(fd: int, target: Path) -> str:
    """Проверить владельца и права того же файла, из которого читается политика."""
    try:
        info = os.fstat(fd)
    except OSError as exc:
        # Сбой диагностики не отменяет политику, если её содержимое читается.
        log.warning("политика %s: не удалось проверить владельца и права (%s)", target, exc)
        return "Не удалось проверить владельца и права файла правил."
    if info.st_uid != 0 or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        log.warning(
            "политика %s: небезопасные права (uid=%d, mode=%04o); "
            "ожидается владелец root и запрет записи группе и остальным",
            target,
            info.st_uid,
            stat.S_IMODE(info.st_mode),
        )
        return "Небезопасные права файла правил: обратитесь к администратору."
    return ""


def load(path: Path | None = None) -> Policy:
    """Читает политику. Ошибка разбора → статус ``invalid`` и пустые значения."""
    target = POLICY_PATH if path is None else path
    if not target.exists():
        return Policy(status=PolicyStatus.ABSENT)
    parser = configparser.ConfigParser(interpolation=None)
    permissions_warning = ""
    try:
        with target.open(encoding="utf-8") as stream:
            permissions_warning = _permissions_warning(stream.fileno(), target)
            text = stream.read()
        parser.read_string(text, source=str(target))
    except (OSError, UnicodeDecodeError, configparser.Error) as exc:
        log.warning("политика %s не разобрана (%s), считаю её отсутствующей", target, exc)
        return Policy(status=PolicyStatus.INVALID, permissions_warning=permissions_warning)

    raw: dict[str, str] = {}
    if parser.has_section(SECTION):
        raw.update(parser[SECTION])
    else:
        raw.update(parser.defaults())
    if not raw:
        log.warning("политика %s без секции [%s], считаю её отсутствующей", target, SECTION)
        return Policy(status=PolicyStatus.INVALID, permissions_warning=permissions_warning)

    explicit = {k.strip() for k in raw.pop(LOCKED_KEY, "").replace(",", " ").split() if k.strip()}
    values: dict[str, Any] = {}
    for key, value in raw.items():
        if key == OFFLINE_KEY:
            if _offline_value(value, target):
                values[key] = True
            else:
                # offline=false — не запрет: уйти в офлайн пользователь может сам.
                explicit.discard(key)
            continue
        try:
            values[key] = _coerce(key, value)
        except ValueError as exc:
            log.warning("политика %s: %s, считаю её невалидной", target, exc)
            return Policy(status=PolicyStatus.INVALID, permissions_warning=permissions_warning)
    return Policy(
        values=values,
        locked_keys=frozenset(values) | explicit,
        status=PolicyStatus.OK,
        permissions_warning=permissions_warning,
    )


def appimage_denied(policy: Policy) -> bool:
    """Совещательный запрет AppImage (arch/appimage.md §5, T1/MJ-4).

    INVALID и profile=secure сами по себе не запрещают запуск: это не сеть.
    """
    if policy.status is PolicyStatus.INVALID or "appimage" not in policy.values:
        return False
    raw = policy.values["appimage"]
    value = str(raw).strip().lower()
    if value in _FALSE or value == "deny":
        return True
    if value in _TRUE or value == "allow":
        return False
    log.warning("appimage=%r из политики недопустим, разрешаю запуск", raw)
    return False


def effective(settings: Settings, policy: Policy) -> Settings:
    """Возвращает копию настроек с наложенными значениями политики."""
    result = dataclasses.replace(settings, extra=dict(settings.extra))
    for key, value in policy.values.items():
        if key == "hotkey" and (not isinstance(value, str) or not is_valid_combo(value)):
            log.warning("hotkey=%r из политики недопустим, оставляю прежнее значение", value)
            continue
        if key in _SETTINGS_TYPES:
            setattr(result, key, value)
        else:
            result.extra[key] = value
    return result
