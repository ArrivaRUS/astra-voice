"""Настройки пользователя: ``settings.json`` 0600 в каталоге конфигурации.

Запись атомарна (временный файл + ``os.replace``, ``fsync`` файла и каталога), чтение — с
миграциями вперёд по ``schema_version``. Испорченный файл не удаляется, а
переименовывается в ``settings.json.bak-<метка времени>``.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

SCHEMA_VERSION = 2
HOTKEY_MODES = ("ptt", "toggle")


def is_valid_combo(combo: str) -> bool:
    """Одна основная клавиша и обязательный модификатор; Shift — только дополнительный."""
    modifiers = {"ctrl", "control", "alt", "meta", "super", "win"}
    parts = [part.strip().lower() for part in combo.split("+")]
    return (
        any(part in modifiers for part in parts)
        and sum(1 for part in parts if part and part not in modifiers and part != "shift") == 1
    )


def is_valid_command_combo(combo: str) -> bool:
    """Команда допускает одиночную левую/правую Win и обычные сочетания."""
    return combo.strip().lower() in {"super_l", "super_r", "win", "super"} or is_valid_combo(combo)


def is_valid_command_mouse_button(button: object) -> bool:
    """Только логическая средняя или дополнительная кнопка; bool не номер."""
    return type(button) is int and (button == 2 or 8 <= button <= 31)


@dataclass
class Settings:
    """Значения настроек. Незнакомые ключи сохраняются в ``extra``."""

    hotkey: str = "Ctrl+Space"
    hotkey_mode: str = "ptt"
    # Пользовательские настройки; policy command_hotkey отменена контрактом 1.7.
    command_hotkey: str = "Super_L"
    command_enabled: bool = True
    command_preview: bool = False
    command_mouse_enabled: bool = False
    command_mouse_button: int = 2
    sound_cues_enabled: bool = False
    pill_enabled: bool = True
    autostart: bool = True  # PRD, экран 5 онбординга (решение заказчика)
    model_id: str | None = None
    device_label: str | None = None
    device_label_id: str | None = None
    language: str = "ru"
    # AppImage: включены по умолчанию; deb: выключены. load() применяет трек.
    # Сам Settings() не разрешает плановые проверки; политика и офлайн главнее.
    # До прохождения экрана 1 онбординга плановая проверка не запускается.
    check_app_updates: bool = False
    check_model_updates: bool = False
    # Офлайн-режим пользователя (PRD F14.2): перекрывает оба тумблера и скачивание
    # моделей. Имя совпадает с ключом ``offline`` в policy.conf — политика его блокирует.
    offline: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"schema_version": SCHEMA_VERSION}
        data.update(self.extra)
        for f in fields(self):
            if f.name != "extra":
                data[f.name] = getattr(self, f.name)
        return data


def update_checks_default(appimage: bool | None = None) -> bool:
    """Трек проверяется при вызове, без чтения файловой системы при импорте."""
    if appimage is None:
        from astra_voice.core import paths

        appimage = paths.install_kind().is_appimage
    return appimage


def defaults(*, appimage: bool | None = None, locked: Iterable[str] = ()) -> Settings:
    """Свежие настройки трека; замок без значения сохраняет старое off."""
    enabled = update_checks_default(appimage)
    locked = frozenset(locked)
    return Settings(
        check_app_updates=enabled and "check_app_updates" not in locked,
        check_model_updates=enabled and "check_model_updates" not in locked,
        extra={"update_defaults_migrated": True} if enabled else {},
    )


def _enable_update_checks(
    data: dict[str, Any], *, appimage: bool, locked: Iterable[str] = ()
) -> dict[str, Any]:
    # До 0.1.5 сохранялись все дефолты, поэтому старое false не позволяет
    # отличить выбор пользователя. В AppImage включаем оба один раз; deb не меняем.
    if not appimage or data.get("update_defaults_migrated") is True:
        return data
    data = {**data, "update_defaults_migrated": True}
    for key in ("check_app_updates", "check_model_updates"):
        if key not in locked:
            data[key] = True
    return data


# Миграции вперёд: ключ — версия, из которой мигрируем; значение — функция в версию+1.
MIGRATIONS: dict[int, Callable[[dict[str, Any]], dict[str, Any]]] = {
    0: dict,
    1: dict,
}


def _migrate(data: dict[str, Any]) -> dict[str, Any]:
    raw = data.get("schema_version", 0)
    current = raw if type(raw) is int and raw >= 0 else 0
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
    if name in ("model_id", "device_label", "device_label_id"):
        return value is None or isinstance(value, str)
    if isinstance(default, bool):
        return isinstance(value, bool)
    return isinstance(value, type(default)) and not isinstance(value, bool)


def from_dict(
    data: dict[str, Any], *, appimage: bool = False, locked: Iterable[str] = ()
) -> Settings:
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
    settings = defaults(appimage=appimage, locked=locked)
    settings.extra = extra
    for name, value in values.items():
        if _type_ok(name, value, getattr(settings, name)):
            setattr(settings, name, value)
        else:
            log.warning("настройка %r негодного типа, беру значение по умолчанию", name)
    if not is_valid_combo(settings.hotkey):
        log.warning("hotkey=%r недопустим, беру значение по умолчанию", settings.hotkey)
        settings.hotkey = Settings.hotkey
    # Дефолт Win допустим только при отсутствии ключа. Ошибка загруженной
    # настройки не должна неожиданно отбирать у рабочего стола одиночную Win.
    if "command_hotkey" in values and (
        not isinstance(values["command_hotkey"], str)
        or not is_valid_command_combo(settings.command_hotkey)
    ):
        log.warning("command_hotkey недопустим, режим команды выключен")
        settings.command_hotkey = ""
        settings.command_enabled = False
    if "command_enabled" in values and not isinstance(values["command_enabled"], bool):
        settings.command_enabled = False
    if "command_mouse_button" in values and not is_valid_command_mouse_button(
        values["command_mouse_button"]
    ):
        log.warning("command_mouse_button недопустим, управление мышью выключено")
        settings.command_mouse_button = Settings.command_mouse_button
        settings.command_mouse_enabled = False
    if "command_mouse_enabled" in values and type(values["command_mouse_enabled"]) is not bool:
        settings.command_mouse_enabled = False
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


def load(
    path: Path | None = None,
    *,
    quarantine: bool = True,
    locked: Iterable[str] = (),
    appimage: bool | None = None,
) -> Settings:
    """Читает настройки и сохраняет миграцию схемы и AppImage-маркер.

    ``quarantine=False`` отключает переименование повреждённого файла и запись
    миграции/маркера: файл остаётся нетронутым.
    """
    appimage = update_checks_default(appimage)
    locked = frozenset(locked)
    if path is None:
        from astra_voice.core.paths import settings_path

        path = settings_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return defaults(appimage=appimage, locked=locked)
    except OSError as exc:  # noqa: BLE001
        log.warning("не удалось прочитать %s: %s", path, exc)
        return defaults(appimage=appimage, locked=locked)
    try:
        data = json.loads(raw)
    except ValueError:
        if quarantine:
            _quarantine(path)
        return defaults(appimage=appimage, locked=locked)
    if not isinstance(data, dict):
        if quarantine:
            _quarantine(path)
        return defaults(appimage=appimage, locked=locked)
    previous_schema = data.get("schema_version")
    previous_marker = data.get("update_defaults_migrated")
    migrated = _enable_update_checks(_migrate(data), appimage=appimage, locked=locked)
    settings = from_dict(migrated, appimage=appimage, locked=locked)
    if quarantine and (
        (previous_schema != SCHEMA_VERSION and migrated["schema_version"] == SCHEMA_VERSION)
        or (previous_marker is not True and migrated.get("update_defaults_migrated") is True)
    ):
        try:
            save(settings, path)
        except OSError as exc:
            log.warning("не удалось сохранить миграцию %s: %s", path, exc)
    return settings


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
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError as exc:
            log.warning(
                "настройки сохранены, но не удалось синхронизировать каталог %s: %s",
                path.parent,
                exc,
            )
    finally:
        tmp.unlink(missing_ok=True)
