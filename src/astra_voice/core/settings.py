"""Настройки пользователя: ``settings.json`` 0600 в каталоге конфигурации.

Запись атомарна (временный файл + ``fsync`` + ``os.replace``), чтение — с
миграциями вперёд по ``schema_version``. Испорченный файл не удаляется, а
переименовывается в ``settings.json.bak-<метка времени>``.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1
HOTKEY_MODES = ("ptt", "toggle")


@dataclass
class Settings:
    """Значения настроек. Незнакомые ключи сохраняются в ``extra``."""

    hotkey: str = "Ctrl+Space"
    hotkey_mode: str = "ptt"
    pill_enabled: bool = True
    autostart: bool = True  # PRD, экран 5 онбординга (решение заказчика)
    model_id: str | None = None
    language: str = "ru"
    # Сеть по умолчанию выключена во всех профилях (PRD §«Сеть по умолчанию»,
    # синтез-план §6.5): ни одного соединения без явного действия пользователя.
    check_app_updates: bool = False
    check_model_updates: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"schema_version": SCHEMA_VERSION}
        data.update(self.extra)
        for f in fields(self):
            if f.name != "extra":
                data[f.name] = getattr(self, f.name)
        return data


# Миграции вперёд: ключ — версия, из которой мигрируем; значение — функция в версию+1.
MIGRATIONS: dict[int, Callable[[dict[str, Any]], dict[str, Any]]] = {}


def _migrate(data: dict[str, Any]) -> dict[str, Any]:
    raw = data.get("schema_version", 0)
    current = raw if isinstance(raw, int) else 0
    while current < SCHEMA_VERSION:
        step = MIGRATIONS.get(current)
        if step is None:
            log.warning("нет миграции settings.json с версии %s, беру как есть", current)
            break
        data = step(data)
        current += 1
    if current > SCHEMA_VERSION:
        log.warning("settings.json версии %s новее приложения (%s)", current, SCHEMA_VERSION)
    data["schema_version"] = current
    return data


def _type_ok(name: str, value: Any, default: Any) -> bool:
    """Тип значения совпадает с типом дефолта (bool и int не путаются)."""
    if name == "model_id":
        return value is None or isinstance(value, str)
    if isinstance(default, bool):
        return isinstance(value, bool)
    return isinstance(value, type(default)) and not isinstance(value, bool)


def from_dict(data: dict[str, Any]) -> Settings:
    """Строит :class:`Settings`, подставляя дефолты вместо негодных значений."""
    known = {f.name: f for f in fields(Settings) if f.name != "extra"}
    values: dict[str, Any] = {}
    extra: dict[str, Any] = {}
    for key, value in data.items():
        if key == "schema_version":
            continue
        if key not in known:
            extra[key] = value
            continue
        values[key] = value
    settings = Settings(extra=extra)
    for name, value in values.items():
        if _type_ok(name, value, getattr(settings, name)):
            setattr(settings, name, value)
        else:
            log.warning("настройка %r негодного типа, беру значение по умолчанию", name)
    if settings.hotkey_mode not in HOTKEY_MODES:
        log.warning("hotkey_mode=%r неизвестен, беру 'ptt'", settings.hotkey_mode)
        settings.hotkey_mode = "ptt"
    return settings


def _quarantine(path: Path) -> None:
    backup = path.with_name(f"{path.name}.bak-{int(time.time())}")
    try:
        os.replace(path, backup)
    except OSError as exc:  # noqa: BLE001
        log.warning("не удалось переименовать испорченный %s: %s", path, exc)
    else:
        log.warning("%s испорчен, переименован в %s, беру значения по умолчанию", path, backup)


def load(path: Path | None = None) -> Settings:
    """Читает настройки; при любой порче возвращает дефолты."""
    if path is None:
        from astra_voice.core.paths import settings_path

        path = settings_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return Settings()
    except OSError as exc:  # noqa: BLE001
        log.warning("не удалось прочитать %s: %s", path, exc)
        return Settings()
    try:
        data = json.loads(raw)
    except ValueError:
        _quarantine(path)
        return Settings()
    if not isinstance(data, dict):
        _quarantine(path)
        return Settings()
    return from_dict(_migrate(data))


def save(settings: Settings, path: Path | None = None) -> None:
    """Атомарно записывает настройки с правами 0600."""
    if path is None:
        from astra_voice.core.paths import settings_path

        path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    payload = json.dumps(settings.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
