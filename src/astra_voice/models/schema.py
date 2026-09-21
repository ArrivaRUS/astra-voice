"""Проверка каталога по JSON-схеме за фасадом (A-08).

Модуль `jsonschema` есть не в каждом парке (у заказчика он был, на соседней
машине — нет, и пакет не ставился вовсе). Поэтому проверка схемой обязательна
только там, где источник каталога чужой: для встроенного каталога целостность
уже доказана подписью и отпечатком схемы, а разбор полей идёт строгим кодом
`catalog.py`. Тот же фасад понадобится пользовательской сборке v1.1, где
модуль можно принести с собой.
"""

from __future__ import annotations

import logging
from typing import NoReturn

log = logging.getLogger(__name__)


class SchemaUnavailable(Exception):
    """Модуль проверки недоступен или несовместим по версии."""


class SchemaInvalid(Exception):
    """Документ не соответствует схеме."""


class _RemoteReferenceError(Exception):
    """Запасной тип ошибки для версий jsonschema без RefResolutionError."""


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
            cls.check_schema(schema)
            resolver = _LocalRefResolver.from_schema(schema)
            resolver.handlers.update(
                dict.fromkeys(("http", "https", "ftp", "file", "data"), resolver.resolve_remote)
            )
            validator = cls(schema, resolver=resolver, format_checker=cls.FORMAT_CHECKER)
        except (AttributeError, TypeError):
            raise SchemaUnavailable("Несовместимая версия jsonschema.") from None
        validator.validate(document)
    except (AttributeError, TypeError):
        raise SchemaInvalid("Каталог не соответствует схеме.") from None
    except (
        jsonschema.ValidationError,
        jsonschema.SchemaError,
        ref_resolution_error,
        RecursionError,
    ):
        raise SchemaInvalid("Каталог не соответствует схеме.") from None
