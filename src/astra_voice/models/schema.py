"""Проверка каталога по JSON-схеме за фасадом (A-08).

В AppImage модуль `jsonschema` обязателен: состав бандла наш
(arch/appimage.md §10.11, §11). В DEB и SOURCE встроенный каталог допускает
пропуск проверки с предупреждением: целостность уже доказана подписью
и отпечатком схемы, а разбор полей идёт строгим кодом `catalog.py`.
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from typing import Any, NoReturn

log = logging.getLogger(__name__)


class SchemaUnavailable(Exception):
    """Модуль проверки недоступен или несовместим по версии."""


class SchemaInvalid(Exception):
    """Документ не соответствует схеме."""


class _RemoteReferenceError(Exception):
    """Запасной тип ошибки для версий jsonschema без RefResolutionError."""


def require_available() -> None:
    """Проверяет совместимость модуля для гейта бандла (arch/appimage.md §11).

    Пустые документ и схема проходят тот же путь, что и каталог в `validate`.
    """
    try:
        validate({}, {})
    except SchemaInvalid:
        raise SchemaUnavailable("Несовместимая версия jsonschema.") from None


@lru_cache(maxsize=128)
def _check_schema(validator_class: Any, encoded: str) -> None:
    """Cache only successful meta-validation of an exact schema snapshot.

    Keys include the validator class and JSON content, never mutable identity.
    Resolvers and validators remain per-call because their state is not shared
    safely across the capture/control/storage threads.
    """
    validator_class.check_schema(json.loads(encoded))


def validate(document: object, schema: object) -> None:
    """Проверяет документ по схеме; удалённые ссылки в схеме запрещены."""
    try:
        import jsonschema
    except ImportError:
        raise SchemaUnavailable("Модуль jsonschema недоступен.") from None

    try:
        ref_resolution_error = getattr(
            jsonschema.exceptions, "RefResolutionError", _RemoteReferenceError
        )
    except AttributeError:
        # Подменённый или урезанный модуль без подмодуля exceptions.
        raise SchemaUnavailable("Несовместимая версия jsonschema.") from None

    try:
        try:

            class _LocalRefResolver(jsonschema.RefResolver):
                def resolve_remote(self, uri: str) -> NoReturn:
                    # В jsonschema 4.10.3 отсутствие handler включает requests/urllib.
                    # Запрещаем загрузку для любой схемы URI, включая неизвестные.
                    raise ref_resolution_error("Удалённые ссылки в схеме каталога запрещены.")

            cls = jsonschema.Draft202012Validator
            # Snapshot before checking/using it: later in-place edits must not
            # reuse a successful check for different content.
            encoded = json.dumps(schema, ensure_ascii=True, allow_nan=False, sort_keys=True)
            snapshot = json.loads(encoded)
            if snapshot != schema:
                # Do not make invalid Python schemas valid by JSON coercion
                # (for example required=("id",) becoming required=["id"]).
                raise SchemaInvalid("Каталог не соответствует схеме.")
            _check_schema(cls, encoded)
            resolver = _LocalRefResolver.from_schema(snapshot)
            resolver.handlers.update(
                dict.fromkeys(("http", "https", "ftp", "file", "data"), resolver.resolve_remote)
            )
            validator = cls(snapshot, resolver=resolver, format_checker=cls.FORMAT_CHECKER)
        except (AttributeError, TypeError):
            raise SchemaUnavailable("Несовместимая версия jsonschema.") from None
        validator.validate(document)
    except (AttributeError, TypeError, ValueError):
        raise SchemaInvalid("Каталог не соответствует схеме.") from None
    except (
        jsonschema.ValidationError,
        jsonschema.SchemaError,
        ref_resolution_error,
        RecursionError,
    ):
        raise SchemaInvalid("Каталог не соответствует схеме.") from None
