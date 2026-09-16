"""Строгий разбор встроенного каталога после проверки отсоединённой подписи."""

from __future__ import annotations

import hashlib
import hmac
import json
import posixpath
import re
import stat
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn, cast
from urllib.parse import unquote

from astra_voice.core import paths
from astra_voice.net.hosts import host_allowed
from astra_voice.security.verify import Verifier

CATALOG_MAX_BYTES = 1 << 20
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class CatalogError(Exception):
    """Ошибка каталога со стабильным кодом и понятным пользователю сообщением."""

    code: str
    message: str

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class _RemoteReferenceError(Exception):
    """Запасной тип ошибки для версий jsonschema без RefResolutionError."""


@dataclass(frozen=True)
class FileSpec:
    """Проверенные путь, размер и контрольная сумма одного файла модели."""

    path: str
    sha256: str
    size: int
    url_path: str


@dataclass(frozen=True)
class CatalogEntry:
    """Одна ревизия модели и полный список её файлов."""

    id: str
    revision: str
    name: str
    description: str
    size_bytes: int
    min_ram_mb: int
    layout: str
    variant: str
    recommended: bool
    host: str
    files: tuple[FileSpec, ...]


@dataclass(frozen=True)
class RevokedEntry:
    """Отозванная ревизия модели с причиной отзыва."""

    model_id: str
    revision: str
    reason: str


@dataclass(frozen=True)
class Catalog:
    """Проверенный каталог и список отозванных ревизий."""

    serial: int
    trust_epoch: int
    revoked: tuple[RevokedEntry, ...]
    entries: tuple[CatalogEntry, ...]

    def entry(self, model_id: str) -> CatalogEntry | None:
        """Возвращает модель по идентификатору или None, если её нет."""
        return next((entry for entry in self.entries if entry.id == model_id), None)

    def is_revoked(self, model_id: str, revision: str) -> bool:
        """Проверяет отзыв именно указанной модели и ревизии."""
        return any(
            entry.model_id == model_id and entry.revision == revision for entry in self.revoked
        )


def _read_limited(path: Path, label: str) -> bytes:
    """Проверяет размер до чтения и ограничивает фактическое число прочитанных байт."""
    try:
        info = path.stat()
        if not stat.S_ISREG(info.st_mode):
            raise CatalogError("not-found", f"{label}: файл не найден.")
        if info.st_size > CATALOG_MAX_BYTES:
            raise CatalogError("too-large", f"{label}: файл слишком большой.")
        with path.open("rb") as stream:
            raw = stream.read(CATALOG_MAX_BYTES + 1)
    except FileNotFoundError:
        raise CatalogError("not-found", f"{label}: файл не найден.") from None
    except OSError:
        raise CatalogError("bad-schema", f"{label}: не удалось прочитать файл.") from None
    if len(raw) > CATALOG_MAX_BYTES:
        raise CatalogError("too-large", f"{label}: файл слишком большой.")
    return raw


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Отвергает повторяющиеся ключи на любой глубине JSON (У34)."""
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise CatalogError("bad-schema", "В каталоге повторяются ключи JSON.")
        result[key] = value
    return result


def _reject_constant(value: str) -> object:
    """Запрещает расширения Python для JSON: NaN и бесконечности."""
    raise CatalogError("bad-schema", "В каталоге записано недопустимое число.")


def _parse_json(raw: bytes) -> object:
    try:
        result: object = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (ValueError, RecursionError):
        raise CatalogError("bad-schema", "Не удалось разобрать JSON каталога.") from None
    return result


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise CatalogError("bad-schema", "В каталоге ожидался объект.")
    return cast(dict[str, object], value)


def _array(value: object) -> list[object]:
    if not isinstance(value, list):
        raise CatalogError("bad-schema", "В каталоге ожидался список.")
    return cast(list[object], value)


def _string(value: object) -> str:
    if not isinstance(value, str):
        raise CatalogError("bad-schema", "В каталоге ожидалась строка.")
    return value


def _positive_integer(value: object) -> int:
    # JSON Schema допускает 1.0 как integer; контракт требует именно целое число.
    if type(value) is not int or value <= 0:
        raise CatalogError("bad-schema", "В каталоге ожидалось целое число больше нуля.")
    return value


def _boolean(value: object) -> bool:
    if not isinstance(value, bool):
        raise CatalogError("bad-schema", "В каталоге ожидалось логическое значение.")
    return value


def _identifier(value: object) -> str:
    result = _string(value)
    if ID_RE.fullmatch(result) is None:
        raise CatalogError("bad-schema", "В каталоге недопустимый идентификатор или ревизия.")
    return result


def _unsafe_path_text(path: str) -> bool:
    return "\\" in path or any(unicodedata.category(char) in {"Cc", "Cf", "Cs"} for char in path)


def _check_path(path: str) -> None:
    """Проверяет исходные компоненты до нормализации, записи на диск и сети (У6)."""
    if path.startswith("/") or re.match(r"^[A-Za-z]:", path) or _unsafe_path_text(path):
        raise CatalogError("bad-path", "В каталоге недопустимый путь к файлу.")
    if len(path.encode("utf-8")) > 1024:
        raise CatalogError("bad-path", "В каталоге слишком длинный путь к файлу.")
    for part in path.split("/"):
        if part in {"", ".", ".."}:
            raise CatalogError("bad-path", "В каталоге недопустимые части пути к файлу.")
        if len(part.encode("utf-8")) > 255:
            raise CatalogError("bad-path", "В каталоге слишком длинное имя файла.")


def _check_url_path(url_path: str, revision: str) -> None:
    """Проверяет путь URL и закодированные обходы через родительский каталог."""
    decoded = url_path
    # Ограничиваем и вложенное percent-кодирование, чтобы разбор оставался линейным.
    for _ in range(8):
        if (
            not decoded.startswith("/")
            or decoded.startswith("//")
            or _unsafe_path_text(decoded)
            or "?" in decoded
            or "#" in decoded
            or any(char.isspace() for char in decoded)
            or ".." in decoded.split("/")
        ):
            raise CatalogError("bad-path", "В каталоге недопустимый путь для скачивания.")
        try:
            normalized = unquote(decoded, errors="strict")
        except UnicodeError:
            raise CatalogError(
                "bad-path", "В каталоге неверно закодирован путь скачивания."
            ) from None
        if normalized == decoded:
            break
        decoded = normalized
    else:
        raise CatalogError("bad-path", "В каталоге слишком сложное кодирование пути скачивания.")
    components = posixpath.normpath(decoded).split("/")
    if ".." in components or revision not in components:
        raise CatalogError("bad-path", "Путь скачивания не содержит ревизию модели.")


def _file(value: object) -> FileSpec:
    data = _object(value)
    return FileSpec(
        path=_string(data.get("path")),
        sha256=_string(data.get("sha256")),
        size=_positive_integer(data.get("size")),
        url_path=_string(data.get("url_path")),
    )


def _model(value: object) -> CatalogEntry:
    data = _object(value)
    model_id = _identifier(data.get("id"))
    revision = _identifier(data.get("revision"))
    host = _string(data.get("host"))
    if not host_allowed(host):
        raise CatalogError("bad-schema", "Источник модели отсутствует в списке разрешённых.")
    files = tuple(_file(item) for item in _array(data.get("files")))
    if len({file.path for file in files}) != len(files):
        raise CatalogError("bad-schema", "В модели повторяются пути к файлам.")
    size_bytes = _positive_integer(data.get("size_bytes"))
    if sum(file.size for file in files) != size_bytes:
        raise CatalogError("bad-schema", "Размер модели не совпадает с суммой размеров файлов.")
    return CatalogEntry(
        id=model_id,
        revision=revision,
        name=_string(data.get("name")),
        description=_string(data.get("description")),
        size_bytes=size_bytes,
        min_ram_mb=_positive_integer(data.get("min_ram_mb")),
        layout=_string(data.get("layout")),
        variant=_string(data.get("variant")),
        recommended=_boolean(data.get("recommended")),
        host=host,
        files=files,
    )


def _revoked(value: object) -> RevokedEntry:
    data = _object(value)
    return RevokedEntry(
        model_id=_identifier(data.get("model_id")),
        revision=_identifier(data.get("revision")),
        reason=_string(data.get("reason")),
    )


def load_builtin(verifier: Verifier, *, root: Path | None = None) -> Catalog:
    """Читает каталог: лимит → подпись → JSON → SHA-256 схемы → схема → поля."""
    directory = (paths.data_dir_static() if root is None else root).resolve()
    catalog_path = (directory / "catalog.json").resolve()
    sig_path = (directory / "catalog.json.sig").resolve()
    raw = _read_limited(catalog_path, "Каталог")
    try:
        if not sig_path.is_file() or not verifier.verify_detached(catalog_path, sig_path):
            raise CatalogError("bad-signature", "Не удалось подтвердить подпись каталога.")
    except (OSError, UnicodeError):
        raise CatalogError("bad-signature", "Не удалось проверить подпись каталога.") from None
    # Проверка подписи читает файл по пути: замечаем изменение байтов за это время.
    if _read_limited(catalog_path, "Каталог") != raw:
        raise CatalogError("bad-signature", "Каталог изменился во время проверки подписи.")
    document = _parse_json(raw)
    schema_raw = _read_limited(directory / "catalog.schema.json", "Схема каталога")
    schema_sha256 = document.get("schema_sha256") if isinstance(document, dict) else None
    if (
        not isinstance(schema_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", schema_sha256) is None
        or not hmac.compare_digest(hashlib.sha256(schema_raw).hexdigest(), schema_sha256)
    ):
        raise CatalogError("bad-schema", "Не удалось подтвердить схему каталога.")
    schema = _parse_json(schema_raw)
    try:
        import jsonschema
    except ImportError:
        raise CatalogError(
            "bad-schema", "Не удалось проверить каталог: недоступен модуль jsonschema."
        ) from None
    ref_resolution_error = getattr(
        jsonschema.exceptions, "RefResolutionError", _RemoteReferenceError
    )

    class _LocalRefResolver(jsonschema.RefResolver):
        def resolve_remote(self, uri: str) -> NoReturn:
            # В jsonschema 4.10.3 отсутствие handler включает requests/urllib.
            # Запрещаем загрузку для любой схемы URI, включая неизвестные.
            raise ref_resolution_error("Удалённые ссылки в схеме каталога запрещены.")

    try:
        cls = jsonschema.Draft202012Validator
        cls.check_schema(schema)
        resolver = _LocalRefResolver.from_schema(schema)
        resolver.handlers.update(
            dict.fromkeys(("http", "https", "ftp", "file", "data"), resolver.resolve_remote)
        )
        cls(
            schema,
            resolver=resolver,
            format_checker=cls.FORMAT_CHECKER,
        ).validate(document)
    except (
        jsonschema.ValidationError,
        jsonschema.SchemaError,
        ref_resolution_error,
        RecursionError,
    ):
        raise CatalogError("bad-schema", "Каталог не соответствует схеме.") from None
    data = _object(document)
    _positive_integer(data.get("manifest_version"))
    entries = tuple(_model(item) for item in _array(data.get("models")))
    if len({entry.id for entry in entries}) != len(entries):
        raise CatalogError("bad-schema", "В каталоге повторяются идентификаторы моделей.")
    revoked = tuple(_revoked(item) for item in _array(data.get("revoked")))
    for entry in entries:
        for file in entry.files:
            _check_path(file.path)
            _check_url_path(file.url_path, entry.revision)
            if re.fullmatch(r"[0-9a-f]{64}", file.sha256) is None:
                raise CatalogError("bad-schema", "В каталоге неверная контрольная сумма файла.")
    return Catalog(
        serial=_positive_integer(data.get("serial")),
        trust_epoch=_positive_integer(data.get("trust_epoch")),
        revoked=revoked,
        entries=entries,
    )
