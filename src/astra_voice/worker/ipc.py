"""Кадрирование и проверка JSON-сообщений между GUI и воркером."""

from __future__ import annotations

import json
import math
import platform
import re
import struct
from collections import deque
from dataclasses import dataclass, field
from importlib.metadata import PackageNotFoundError, version
from typing import Any

from astra_voice.core.version import __version__

PROTOCOL_VERSION = 1
MAX_FRAME_BYTES = 64 * 1024
MAX_TEXT_BYTES = 32 * 1024

FRAME_TOO_LARGE = "frame-too-large"
BAD_FRAME = "bad-frame"
UNKNOWN_MESSAGE = "unknown-message"
BAD_FIELD = "bad-field"

_UTTERANCE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_HEADER = struct.Struct(">I")


class FrameError(Exception):
    """Ошибка IPC с машинным кодом и пояснением без содержимого диктовки."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code: str = code
        self.message: str = message


@dataclass(frozen=True)
class _Schema:
    """Таблица типов обязательных и необязательных полей сообщения."""

    required: dict[str, tuple[type, ...]] = field(default_factory=dict)
    optional: dict[str, tuple[type, ...]] = field(default_factory=dict)


_STR = (str,)
_INT = (int,)
_NUMBER = (int, float)
_UTTERANCE = _Schema({"utterance_id": _STR})
_SCHEMAS = {
    "model.load": _Schema(
        {
            "id": _STR,
            "revision": _STR,
            "dir": _STR,
            "layout": _STR,
            "variant": _STR,
            "threads": _INT,
            "min_ram_mb": _INT,
        }
    ),
    "model.unload": _Schema(),
    "record.start": _Schema({"utterance_id": _STR}, optional={"device": _STR}),
    "record.stop": _UTTERANCE,
    "record.limit": _UTTERANCE,
    "record.cancel": _UTTERANCE,
    "recognize": _UTTERANCE,
    "transcribe.file": _Schema({"path": _STR}),
    "measure": _Schema(),
    "ping": _Schema(),
    "audio.close": _Schema(),
    "hello": _Schema({"protocol": _INT, "build": _STR, "runtime": (dict,)}),
    "model.loaded": _Schema(
        {
            "id": _STR,
            "revision": _STR,
            "variant": _STR,
            "load_ms": _NUMBER,
            "engine_version": _STR,
        }
    ),
    "result": _Schema({"utterance_id": _STR, "text": _STR, "t_ms": _NUMBER}),
    "cancelled": _UTTERANCE,
    "error": _Schema(
        {"code": _STR, "message": _STR},
        optional={"utterance_id": _STR, "request_type": _STR},
    ),
    "pong": _Schema(),
    "measured": _Schema(
        {"vm_hwm_kb": _INT, "pss_kb": (int, type(None))}, optional={"sessions": _INT}
    ),
    "level": _Schema({"utterance_id": _STR, "rms_dbfs": _NUMBER, "peak_dbfs": _NUMBER}),
    "silent": _UTTERANCE,
    "audio.ready": _Schema(optional={"device": _STR, "changed": _STR}),
    "audio.closed": _Schema(),
}
_RUNTIME = _Schema({"python": _STR, "onnxruntime": (str, type(None))})


def _fields(value: dict[str, Any], schema: _Schema) -> dict[str, Any]:
    """Проверяет известные поля и отбрасывает расширения протокола."""
    for name in schema.required:
        if name not in value:
            raise FrameError(BAD_FIELD, f"Отсутствует обязательное поле {name}.")
    result: dict[str, Any] = {}
    for name, types in (schema.required | schema.optional).items():
        if name not in value:
            continue
        item = value[name]
        # bool не является целым числом протокола, несмотря на наследование от int.
        if type(item) not in types:
            raise FrameError(BAD_FIELD, f"Неверный тип поля {name}.")
        if isinstance(item, float) and not math.isfinite(item):
            raise FrameError(BAD_FIELD, f"Поле {name} должно быть конечным числом.")
        result[name] = item
    return result


def _validate(msg: dict[str, Any]) -> dict[str, Any]:
    """Проверяет схему, формат идентификатора и размер текста в UTF-8."""
    if not isinstance(msg, dict):
        raise FrameError(BAD_FRAME, "Сообщение должно быть JSON-объектом.")
    if "type" not in msg or not isinstance(msg["type"], str):
        raise FrameError(BAD_FIELD, "Поле type должно быть строкой.")
    kind = msg["type"]
    if kind not in _SCHEMAS:
        raise FrameError(UNKNOWN_MESSAGE, "Неизвестный тип сообщения.")
    result = {"type": kind, **_fields(msg, _SCHEMAS[kind])}
    if "utterance_id" in result and not _UTTERANCE_ID.fullmatch(result["utterance_id"]):
        raise FrameError(BAD_FIELD, "Неверный формат utterance_id.")
    if "text" in result:
        text = result["text"]
        # Сначала число символов: для строки в 50 МБ не создаём ещё одну копию.
        if len(text) > MAX_TEXT_BYTES:
            raise FrameError(BAD_FIELD, "Поле text превышает 32 КиБ.")
        try:
            text_size = len(text.encode("utf-8"))
        except UnicodeEncodeError as exc:
            raise FrameError(BAD_FIELD, "Поле text не представимо в UTF-8.") from exc
        if text_size > MAX_TEXT_BYTES:
            raise FrameError(BAD_FIELD, "Поле text превышает 32 КиБ.")
    if kind == "hello":
        if result["protocol"] != PROTOCOL_VERSION:
            raise FrameError(BAD_FIELD, "Неподдерживаемая версия протокола.")
        result["runtime"] = _fields(result["runtime"], _RUNTIME)
    return result


def encode(msg: dict[str, Any]) -> bytes:
    """Проверяет сообщение и добавляет к компактному JSON префикс длины тела."""
    validated = _validate(msg)
    try:
        payload = json.dumps(validated, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    except (ValueError, UnicodeEncodeError) as exc:
        raise FrameError(BAD_FIELD, "Поля сообщения не представимы в JSON UTF-8.") from exc
    if len(payload) > MAX_FRAME_BYTES:
        raise FrameError(FRAME_TOO_LARGE, "Тело кадра превышает 64 КиБ.")
    return _HEADER.pack(len(payload)) + payload


def _reject_constant(value: str) -> None:
    """Запрещает расширения JSON NaN и Infinity."""
    raise ValueError("Недопустимая числовая константа JSON.")


def decode(payload: bytes) -> dict[str, Any]:
    """Разбирает одно тело кадра без префикса и проверяет схему сообщения."""
    if len(payload) > MAX_FRAME_BYTES:
        raise FrameError(FRAME_TOO_LARGE, "Тело кадра превышает 64 КиБ.")
    try:
        msg = json.loads(payload.decode("utf-8"), parse_constant=_reject_constant)
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise FrameError(BAD_FRAME, "Тело кадра должно быть JSON в UTF-8.") from exc
    return _validate(msg)


class FrameReader:
    """Неблокирующий сборщик; после опасной длины требуется закрыть соединение.

    Для независимой обработки ошибок каждого сообщения используйте ``feed_frames``
    и ``decode``. ``feed`` при ошибке сохраняет успешные сообщения и оставшиеся тела:
    следующий вызов (в том числе с ``b""``) продолжит обработку и вернёт накопленное.
    Ошибочное тело пропускается. После ``frame-too-large`` больше ничего не выдаётся.
    """

    def __init__(self) -> None:
        self.broken: bool = False
        self._buffer = bytearray()
        self._length: int | None = None
        self._pending: deque[bytes] = deque()
        self._ready: list[dict[str, Any]] = []

    def feed_frames(self, data: bytes) -> list[bytes]:
        """Собирает тела, проверяя префикс до копирования любых байтов тела."""
        if self.broken:
            return []
        frames: list[bytes] = []
        offset = 0
        view = memoryview(data)
        while offset < len(view):
            target = _HEADER.size if self._length is None else self._length
            count = min(target - len(self._buffer), len(view) - offset)
            self._buffer.extend(view[offset : offset + count])
            offset += count
            if len(self._buffer) < target:
                break
            if self._length is None:
                length = int(_HEADER.unpack(self._buffer)[0])
                self._buffer.clear()
                if length > MAX_FRAME_BYTES:
                    self.broken = True
                    self._pending.clear()
                    self._ready.clear()
                    raise FrameError(FRAME_TOO_LARGE, "Объявленная длина превышает 64 КиБ.")
                self._length = length
            if self._length is not None and len(self._buffer) == self._length:
                frames.append(bytes(self._buffer))
                self._buffer.clear()
                self._length = None
        return frames

    def feed(self, data: bytes) -> list[dict[str, Any]]:
        """Возвращает сообщения; первая ошибка выбрасывается без потери успешных."""
        self._pending.extend(self.feed_frames(data))
        while self._pending:
            self._ready.append(decode(self._pending.popleft()))
        messages, self._ready = self._ready, []
        return messages


def make_hello() -> dict[str, Any]:
    """Создаёт приветствие без загрузки ORT; отсутствие пакета допустимо."""
    ort_version: str | None = None
    try:
        ort_version = version("onnxruntime")
    except PackageNotFoundError:
        pass
    return {
        "type": "hello",
        "protocol": PROTOCOL_VERSION,
        "build": __version__,
        "runtime": {"python": platform.python_version(), "onnxruntime": ort_version},
    }


def error(code: str, message: str) -> dict[str, Any]:
    """Создаёт сообщение об ошибке воркера; коды движка также допустимы."""
    return {"type": "error", "code": code, "message": message}
