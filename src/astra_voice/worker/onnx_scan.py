"""Проверка внешних данных ONNX без загрузки рантайма и копирования весов."""

from __future__ import annotations

import mmap
import os
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

_MAX_DEPTH = 64

# У7: ModelProto.training_info и ModelProto.functions отклоняем даже пустыми.
# Отказ вместо обхода не требует воспроизводить схемы TrainingInfoProto и
# FunctionProto: ошибка в номере поля могла бы снова скрыть external_data.
_UNSUPPORTED_MODEL_FIELDS = {20: "обучающие графы", 25: "локальные функции"}

# Только поля сообщений, через которые схема ONNX допускает доступ к тензорам.
_CHILDREN: dict[str, dict[int, str]] = {
    "model": {7: "graph"},
    "graph": {1: "node", 5: "tensor", 15: "sparse"},
    "sparse": {1: "tensor", 2: "tensor"},
    "node": {5: "attribute"},
    "attribute": {
        5: "tensor",
        6: "graph",
        10: "tensor",
        11: "graph",
        22: "sparse",
        23: "sparse",
    },
}


class ProtobufError(ValueError):
    """Повреждённый protobuf, неподдерживаемая схема или превышение глубины."""


@dataclass(frozen=True)
class _Field:
    """Поле protobuf со смещениями содержимого и значением varint."""

    number: int
    wire: int
    start: int
    end: int
    value: int = 0


def _varint(data: memoryview, pos: int, end: int) -> tuple[int, int]:
    """Читает ограниченный 64 битами varint внутри текущего сообщения."""
    value = 0
    for shift in range(0, 70, 7):
        if pos >= end:
            raise ProtobufError("Обрезанный varint")
        byte = data[pos]
        pos += 1
        if shift == 63 and byte > 1:
            raise ProtobufError("Переполнение varint")
        value |= (byte & 0x7F) << shift
        if byte < 0x80:
            return value, pos
    raise ProtobufError("Слишком длинный varint")


def _fields(data: memoryview, start: int, end: int) -> Iterator[_Field]:
    """Читает поля, проверяя границы; содержимое байтовых полей не копирует."""
    pos = start
    while pos < end:
        tag, pos = _varint(data, pos, end)
        number, wire = tag >> 3, tag & 7
        if not 0 < number < (1 << 29):
            raise ProtobufError("Недопустимый номер поля protobuf")
        value = 0
        if wire == 0:
            start = pos
            value, pos = _varint(data, pos, end)
        elif wire == 2:
            size, pos = _varint(data, pos, end)
            start = pos
            pos += size
        elif wire in (1, 5):
            start = pos
            pos += 8 if wire == 1 else 4
        else:
            raise ProtobufError(f"Недопустимый тип поля protobuf: {wire}")
        if pos > end:
            raise ProtobufError("Поле protobuf выходит за границу сообщения")
        yield _Field(number, wire, start, pos, value)


def _require_wire(field: _Field, expected: int) -> None:
    """Проверяет соответствие типа поля схеме ONNX."""
    if field.wire != expected:
        raise ProtobufError(f"Неверный тип поля ONNX {field.number}: {field.wire}")


def _entry_locations(data: memoryview, start: int, end: int) -> list[str]:
    """Читает location из StringStringEntryProto, учитывая повторы строк."""
    is_location = False
    values: list[tuple[int, int]] = []
    for field in _fields(data, start, end):
        if field.number not in (1, 2):
            continue
        _require_wire(field, 2)
        if field.number == 1:
            # Не декодируем произвольные ключи и не копируем большие поля.
            if field.end - field.start == len(b"location"):
                with data[field.start : field.end] as key:
                    is_location |= bytes(key) == b"location"
        else:
            values.append((field.start, field.end))
    if not is_location:
        return []
    locations: list[str] = []
    for value_start, value_end in values:
        try:
            with data[value_start:value_end] as value:
                locations.append(str(value, "utf-8"))
        except UnicodeDecodeError as exc:
            raise ProtobufError("location содержит некорректный UTF-8") from exc
    # Отсутствующее поле value имеет пустое строковое значение в protobuf.
    return locations or [""]


def _tensor_locations(data: memoryview, start: int, end: int) -> list[str]:
    """Собирает внешние ссылки TensorProto независимо от порядка полей."""
    external = False
    locations: list[str] = []
    for field in _fields(data, start, end):
        if field.number == 14:
            _require_wire(field, 0)
            external |= field.value == 1
        elif field.number == 13:
            _require_wire(field, 2)
            external = True
            locations.extend(_entry_locations(data, field.start, field.end))
    if external and not locations:
        raise ProtobufError("Внешний тензор не содержит location")
    return locations


def _walk(data: memoryview, start: int, end: int, kind: str, depth: int) -> Iterator[str]:
    """Обходит вложенные сообщения строго по схеме с ограничением глубины."""
    if depth > _MAX_DEPTH:
        raise ProtobufError(f"Глубина вложенности ONNX превышает {_MAX_DEPTH}")
    if kind == "tensor":
        yield from _tensor_locations(data, start, end)
        return
    children = _CHILDREN[kind]
    for field in _fields(data, start, end):
        if kind == "model" and field.number in _UNSUPPORTED_MODEL_FIELDS:
            raise ProtobufError(
                f"Модель содержит {_UNSUPPORTED_MODEL_FIELDS[field.number]}; "
                "такие модели движок не принимает — их тензоры "
                "не покрываются проверкой external_data"
            )
        child = children.get(field.number)
        if child is not None:
            _require_wire(field, 2)
            yield from _walk(data, field.start, field.end, child, depth + 1)


def collect_external_locations(onnx_path: Path) -> list[str]:
    """Возвращает внешние location всех доступных по схеме тензоров ONNX.

    Веса отображаются в память и пропускаются по смещениям, поэтому даже файл
    больше 512 МиБ не читается целиком. Повреждения, локальные функции и
    обучающие графы вызывают ProtobufError.
    """
    with onnx_path.open("rb") as source:
        if os.fstat(source.fileno()).st_size == 0:
            raise ProtobufError("Пустой файл ONNX")
        with mmap.mmap(source.fileno(), 0, access=mmap.ACCESS_READ) as mapped:
            with memoryview(mapped) as data:
                return list(_walk(data, 0, len(data), "model", 0))


def _check_location(model_dir: Path, root: Path, files: set[str], location: str) -> None:
    """Проверяет границы, состав модели и тип файла внешних данных."""
    relative = Path(location)
    if not location or "\x00" in location or relative.is_absolute() or ".." in relative.parts:
        raise ValueError("Требуется непустой относительный путь без '..' и NUL")
    normalized = Path(os.path.normpath(location))
    target = model_dir / normalized
    if not target.is_relative_to(model_dir):
        raise ValueError("Путь выходит за каталог модели")
    if normalized.as_posix() not in files:
        raise ValueError("Файл не входит в официальную раскладку модели")
    resolved = target.resolve(strict=True)
    if not resolved.is_relative_to(root):
        raise ValueError("Симлинк выходит за каталог модели")
    if not resolved.is_file():
        raise ValueError("Внешние данные не являются обычным файлом")


def scan_model_dir(model_dir: Path, files: Iterable[str]) -> None:
    """Отклоняет повреждённые ONNX и внешние ссылки за пределы раскладки.

    Проверка лишних файлов остаётся обязанностью engine.check_layout.
    Содержимое файлов внешних данных при сканировании не открывается.
    """
    # Ленивый импорт сохраняет общий тип ошибки без цикла импорта модулей.
    from astra_voice.worker.engine import ExternalDataError

    allowed = set(files)
    for name in sorted(allowed):
        if not name.endswith(".onnx"):
            continue
        location = "<не прочитан>"
        try:
            root = model_dir.resolve(strict=True)
            locations = collect_external_locations(model_dir / name)
            for location in locations:
                _check_location(model_dir, root, allowed, location)
        except (OSError, ValueError, RuntimeError) as exc:
            # Не включаем текст системной ошибки: он может содержать весь путь.
            reason = str(exc) if isinstance(exc, ValueError) else "Ошибка доступа к пути"
            raise ExternalDataError(
                f"ONNX {name!r}, location={location[:200]!r}: {reason}"
            ) from exc
