"""Bounded, declarative meeting-app catalog; no process lookup or detector I/O."""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Self

from astra_voice.core.paths import data_dir_static
from astra_voice.meetings.model import array, choices, normalize_text, obj, text_schema
from astra_voice.models.schema import SchemaInvalid, SchemaUnavailable, validate
from astra_voice.security.files import read_private

MAX_APPS_BYTES = 64 * 1024
MAX_APPS = 128
APP_SCHEMA = obj(
    {
        "id": {"type": "string", "pattern": r"^[a-z0-9][a-z0-9_-]{0,63}$"},
        "title": text_schema(255, 1),
        "binary": array(text_schema(255, 1), 32, unique=True),
        "exe": array(text_schema(255, 1), 32, unique=True),
        "class": choices("dion", "browser", "other"),
    }
)
APPS_SCHEMA = obj({"schema": {"type": "integer", "const": 1}, "apps": array(APP_SCHEMA, MAX_APPS)})


class AppClass(StrEnum):
    DION = "dion"
    BROWSER = "browser"
    OTHER = "other"


class AppDataError(ValueError):
    """The message never includes app metadata or file paths."""


def _name(value: str) -> str:
    result = normalize_text(value, 255, minimum=1)
    if not result.strip() or len(result.encode("utf-8")) > 255:
        raise AppDataError("Недопустимое имя программы")
    return result


def _validated(data: object) -> dict[str, Any]:
    try:
        if not isinstance(data, dict):
            raise AppDataError("Недопустимые данные программы")
        raw = dict(data)
        for key in ("id", "title"):
            if isinstance(raw.get(key), str):
                raw[key] = _name(raw[key])
        for key in ("binary", "exe"):
            if isinstance(raw.get(key), list):
                raw[key] = [_name(item) if isinstance(item, str) else item for item in raw[key]]
        validate(raw, APP_SCHEMA)
        if not raw["binary"] and not raw["exe"]:
            raise AppDataError("Не задана идентичность программы")
        if any("/" in name or name in {".", ".."} for name in raw["binary"]):
            raise AppDataError("Недопустимое имя исполняемого файла")
        if any(
            not name.startswith("/") or any(part in {"", ".", ".."} for part in name.split("/")[1:])
            for name in raw["exe"]
        ):
            raise AppDataError("Недопустимый путь исполняемого файла")
        return raw
    except (ValueError, TypeError, UnicodeError, RecursionError, SchemaInvalid, SchemaUnavailable):
        raise AppDataError("Список программ встреч не соответствует схеме") from None


@dataclass(frozen=True)
class AppDefinition:
    id: str
    title: str
    binary: tuple[str, ...]
    exe: tuple[str, ...]
    app_class: AppClass

    @classmethod
    def from_dict(cls, data: object) -> Self:
        raw = _validated(data)
        return cls(
            raw["id"], raw["title"], tuple(raw["binary"]), tuple(raw["exe"]), AppClass(raw["class"])
        )

    def to_dict(self) -> dict[str, Any]:
        return _validated(
            {
                "id": self.id,
                "title": self.title,
                "binary": list(self.binary),
                "exe": list(self.exe),
                "class": self.app_class.value,
            }
        )

    @property
    def display_class(self) -> str:
        return {
            AppClass.DION: "DION",
            AppClass.BROWSER: "Браузер",
            AppClass.OTHER: "Другая программа",
        }[self.app_class]


def _unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise AppDataError("Повтор поля списка программ")
        result[key] = value
    return result


def load_apps() -> tuple[AppDefinition, ...]:
    """Production entry point: only the catalog from packaged static data."""
    return _load_apps(data_dir_static() / "meeting-apps.json")


def load_apps_for_tests(path: Path) -> tuple[AppDefinition, ...]:
    """Test-only path injection for fixtures; production must use load_apps()."""
    return _load_apps(path)


def _load_apps(path: Path) -> tuple[AppDefinition, ...]:
    """Missing means no known apps, never detection."""
    try:
        raw = read_private(path, MAX_APPS_BYTES)
        document = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique)
        # Shape/version first, individual strings normalize before length checks.
        header = obj(
            {"schema": {"type": "integer", "const": 1}, "apps": array({"type": "object"}, MAX_APPS)}
        )
        validate(document, header)
        if type(document["schema"]) is not int:
            raise AppDataError("Недопустимая версия списка программ")
        apps = tuple(AppDefinition.from_dict(item) for item in document["apps"])
        ids: set[str] = set()
        binaries: set[str] = set()
        executables: set[str] = set()
        for app in apps:
            if (
                app.id in ids
                or binaries.intersection(app.binary)
                or executables.intersection(app.exe)
            ):
                raise AppDataError("Неоднозначная идентичность программы")
            ids.add(app.id)
            binaries.update(app.binary)
            executables.update(app.exe)
        return apps
    except FileNotFoundError:
        return ()
    except (
        OSError,
        ValueError,
        TypeError,
        UnicodeError,
        RecursionError,
        SchemaInvalid,
        SchemaUnavailable,
    ):
        raise AppDataError("Не удалось загрузить список программ встреч") from None
