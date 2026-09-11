"""T-21: кадрирование IPC, проверка схемы и ограничения памяти."""

from __future__ import annotations

import json
import platform
import struct
import sys
import tracemalloc
from importlib.metadata import PackageNotFoundError
from typing import Any
from unittest.mock import Mock

import pytest

from astra_voice.core.version import __version__
from astra_voice.worker import ipc

pytestmark = pytest.mark.unit

_MESSAGES: list[dict[str, Any]] = [
    {
        "type": "model.load",
        "id": "gigaam",
        "revision": "v3",
        "dir": "/models/gigaam",
        "layout": "onnx-asr-gigaam-v3",
        "variant": "int8",
        "threads": 2,
        "min_ram_mb": 768,
    },
    {"type": "model.unload"},
    {"type": "record.start", "utterance_id": "a-B_09"},
    {"type": "record.stop", "utterance_id": "a-B_09"},
    {"type": "record.cancel", "utterance_id": "a-B_09"},
    {"type": "recognize", "utterance_id": "a-B_09"},
    {"type": "transcribe.file", "path": "/tmp/запись.wav"},
    {"type": "measure"},
    {"type": "ping"},
    {"type": "audio.close"},
    {
        "type": "hello",
        "protocol": 1,
        "build": "0.1.0",
        "runtime": {"python": "3.11.0", "onnxruntime": None},
    },
    {
        "type": "model.loaded",
        "id": "gigaam",
        "revision": "v3",
        "variant": "int8",
        "load_ms": 12.5,
        "engine_version": "0.12.0",
    },
    {"type": "result", "utterance_id": "a-B_09", "text": "Проверка", "t_ms": 42},
    {"type": "cancelled", "utterance_id": "a-B_09"},
    {"type": "error", "code": "bad-field", "message": "Неверное поле"},
    {"type": "pong"},
    {"type": "measured", "vm_hwm_kb": 1200, "pss_kb": 800},
    {"type": "audio.ready"},
    {"type": "audio.closed"},
]


def _payload(msg: dict[str, Any]) -> bytes:
    """Сериализует данные без валидации для отрицательных проверок."""
    return json.dumps(msg, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _frame(payload: bytes) -> bytes:
    """Добавляет префикс к произвольному, в том числе ошибочному, телу."""
    return struct.pack(">I", len(payload)) + payload


@pytest.mark.parametrize("msg", _MESSAGES, ids=lambda msg: msg["type"])
def test_round_trip(msg: dict[str, Any]) -> None:
    frame = ipc.encode(msg)
    assert frame == _frame(_payload(msg))
    assert ipc.decode(frame[4:]) == msg
    assert ipc.FrameReader().feed(frame) == [msg]


@pytest.mark.parametrize("split", [1, 2, 3, 4, 9, 12])
def test_partial_frame(split: int) -> None:
    msg = {"type": "ping"}
    frame = ipc.encode(msg)
    reader = ipc.FrameReader()
    assert reader.feed(frame[:split]) == []
    assert reader.feed(frame[split:]) == [msg]
    assert reader.feed(b"") == []


def test_several_frames_and_partial_tail() -> None:
    reader = ipc.FrameReader()
    messages = [{"type": "ping"}, {"type": "pong"}, {"type": "measure"}]
    frames = [ipc.encode(msg) for msg in messages]
    assert reader.feed(b"".join(frames) + frames[0][:2]) == messages
    assert reader.feed(frames[0][2:]) == [messages[0]]


def test_bytewise_utf8_frame() -> None:
    msg = {"type": "result", "utterance_id": "u", "text": "Привет 🎤", "t_ms": 1.5}
    reader = ipc.FrameReader()
    result: list[dict[str, Any]] = []
    for byte in ipc.encode(msg):
        result.extend(reader.feed(bytes([byte])))
    assert result == [msg]


@pytest.mark.parametrize("length", [ipc.MAX_FRAME_BYTES + 1, 0x80000000, 0xFFFFFFFF])
def test_oversized_prefix_breaks_reader_without_body_allocation(length: int) -> None:
    reader = ipc.FrameReader()
    prefix = struct.pack(">I", length)
    assert reader.feed(prefix[:3]) == []
    tracemalloc.start()
    try:
        with pytest.raises(ipc.FrameError) as caught:
            reader.feed(prefix[3:])
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert caught.value.code == "frame-too-large"
    assert peak < 256 * 1024
    assert reader.broken
    assert reader.feed(ipc.encode({"type": "ping"})) == []
    assert reader.feed_frames(ipc.encode({"type": "pong"})) == []


def test_oversized_body_is_not_copied() -> None:
    reader = ipc.FrameReader()
    data = struct.pack(">I", 0x80000000) + b"x" * (1024 * 1024)
    tracemalloc.start()
    try:
        with pytest.raises(ipc.FrameError) as caught:
            reader.feed_frames(data)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert caught.value.code == "frame-too-large"
    assert peak < 256 * 1024


def test_frame_size_boundary() -> None:
    msg = ipc.error("engine-failure", "")
    msg["message"] = "x" * (ipc.MAX_FRAME_BYTES - len(_payload(msg)))
    frame = ipc.encode(msg)
    assert len(frame) == 4 + ipc.MAX_FRAME_BYTES
    assert ipc.FrameReader().feed(frame) == [msg]
    msg["message"] += "x"
    with pytest.raises(ipc.FrameError) as caught:
        ipc.encode(msg)
    assert caught.value.code == "frame-too-large"
    with pytest.raises(ipc.FrameError) as caught:
        ipc.decode(_payload(msg))
    assert caught.value.code == "frame-too-large"


@pytest.mark.parametrize(
    "payload", [b"\xff", b"not json", b"[]", b"null", b"42", b"", b'{"type":NaN}']
)
def test_bad_payload_and_recovery(payload: bytes) -> None:
    reader = ipc.FrameReader()
    with pytest.raises(ipc.FrameError) as caught:
        reader.feed(_frame(payload))
    assert caught.value.code == "bad-frame"
    assert not reader.broken
    assert reader.feed(ipc.encode({"type": "ping"})) == [{"type": "ping"}]


@pytest.mark.parametrize("length", [ipc.MAX_TEXT_BYTES + 1, 50 * 1024 * 1024])
def test_oversized_text_rejected_before_serialization(length: int) -> None:
    msg = {"type": "result", "utterance_id": "u", "text": "x" * length, "t_ms": 1}
    tracemalloc.start()
    try:
        with pytest.raises(ipc.FrameError) as caught:
            ipc.encode(msg)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert caught.value.code == "bad-field"
    assert peak < 256 * 1024


def test_text_limit_counts_utf8_bytes() -> None:
    msg: dict[str, Any] = {"type": "result", "utterance_id": "u", "text": "я" * 16384, "t_ms": 0}
    assert ipc.FrameReader().feed(ipc.encode(msg)) == [msg]
    msg["text"] += "я"
    with pytest.raises(ipc.FrameError) as caught:
        ipc.encode(msg)
    assert caught.value.code == "bad-field"
    with pytest.raises(ipc.FrameError) as caught:
        ipc.decode(_payload(msg))
    assert caught.value.code == "bad-field"


@pytest.mark.parametrize("utterance_id", ["", "a" * 65, "a b", "../", "я", "a\n"])
def test_invalid_utterance_id(utterance_id: str) -> None:
    msg = {"type": "record.start", "utterance_id": utterance_id}
    with pytest.raises(ipc.FrameError) as caught:
        ipc.encode(msg)
    assert caught.value.code == "bad-field"
    with pytest.raises(ipc.FrameError) as caught:
        ipc.decode(_payload(msg))
    assert caught.value.code == "bad-field"


@pytest.mark.parametrize("utterance_id", ["a", "A0_-" * 16])
def test_valid_utterance_id_boundaries(utterance_id: str) -> None:
    msg = {"type": "recognize", "utterance_id": utterance_id}
    assert ipc.FrameReader().feed(ipc.encode(msg)) == [msg]


@pytest.mark.parametrize("msg", _MESSAGES, ids=lambda msg: msg["type"])
def test_required_fields_and_types(msg: dict[str, Any]) -> None:
    for name in msg:
        missing = {key: value for key, value in msg.items() if key != name}
        with pytest.raises(ipc.FrameError) as caught:
            ipc.decode(_payload(missing))
        assert caught.value.code == "bad-field"
        with pytest.raises(ipc.FrameError) as caught:
            ipc.encode({**msg, name: True})
        assert caught.value.code == "bad-field"


@pytest.mark.parametrize(
    "changes",
    [
        {"protocol": 2},
        {"runtime": {}},
        {"runtime": {"python": "3.11"}},
        {"runtime": {"onnxruntime": None}},
        {"runtime": {"python": 3.11, "onnxruntime": None}},
        {"runtime": {"python": "3.11", "onnxruntime": 1}},
    ],
)
def test_invalid_hello(changes: dict[str, Any]) -> None:
    msg = {**_MESSAGES[10], **changes}
    with pytest.raises(ipc.FrameError) as caught:
        ipc.encode(msg)
    assert caught.value.code == "bad-field"


def test_unknown_message() -> None:
    with pytest.raises(ipc.FrameError) as caught:
        ipc.decode(b'{"type":"not-a-message"}')
    assert caught.value.code == "unknown-message"
    assert caught.value.message
    assert str(caught.value) == caught.value.message


def test_extra_fields_are_ignored() -> None:
    assert ipc.encode({"type": "ping", "extension": object()}) == ipc.encode({"type": "ping"})
    assert ipc.decode(b'{"type":"pong","extension":{"anything":true}}') == {"type": "pong"}
    msg = _MESSAGES[10]
    extended = {**msg, "runtime": {**msg["runtime"], "extension": object()}}
    assert ipc.decode(ipc.encode(extended)[4:]) == msg


def test_feed_frames_keeps_good_frames_around_bad_message() -> None:
    good = ipc.encode({"type": "ping"})
    bad = b'{"type":"result"}'
    reader = ipc.FrameReader()
    frames = reader.feed_frames(good + _frame(bad) + good)
    assert frames == [good[4:], bad, good[4:]]
    assert ipc.decode(frames[0]) == {"type": "ping"}
    with pytest.raises(ipc.FrameError) as caught:
        ipc.decode(frames[1])
    assert caught.value.code == "bad-field"
    assert ipc.decode(frames[2]) == {"type": "ping"}
    assert not reader.broken


def test_feed_keeps_messages_across_repeated_errors() -> None:
    reader = ipc.FrameReader()
    good = ipc.encode({"type": "ping"})
    bad_schema = _frame(b'{"type":"result"}')
    with pytest.raises(ipc.FrameError) as caught:
        reader.feed(good + bad_schema + _frame(b"[]") + good)
    assert caught.value.code == "bad-field"
    with pytest.raises(ipc.FrameError) as caught:
        reader.feed(b"")
    assert caught.value.code == "bad-frame"
    assert reader.feed(ipc.encode({"type": "pong"})) == [
        {"type": "ping"},
        {"type": "ping"},
        {"type": "pong"},
    ]
    assert reader.feed(b"") == []


def test_broken_reader_discards_saved_messages() -> None:
    reader = ipc.FrameReader()
    with pytest.raises(ipc.FrameError):
        reader.feed(ipc.encode({"type": "ping"}) + _frame(b"[]"))
    with pytest.raises(ipc.FrameError) as caught:
        reader.feed(struct.pack(">I", 0x80000000))
    assert caught.value.code == "frame-too-large"
    assert reader.feed(b"") == []


def test_hello_without_onnxruntime(monkeypatch: pytest.MonkeyPatch) -> None:
    metadata_version = Mock(side_effect=PackageNotFoundError("onnxruntime"))
    monkeypatch.setattr(ipc, "version", metadata_version)
    hello = ipc.make_hello()
    metadata_version.assert_called_once_with("onnxruntime")
    assert type(hello["protocol"]) is int
    assert hello == {
        "type": "hello",
        "protocol": 1,
        "build": __version__,
        "runtime": {"python": platform.python_version(), "onnxruntime": None},
    }
    assert ipc.FrameReader().feed(ipc.encode(hello)) == [hello]


def test_hello_with_onnxruntime_version(monkeypatch: pytest.MonkeyPatch) -> None:
    metadata_version = Mock(return_value="1.2.3")
    monkeypatch.setattr(ipc, "version", metadata_version)
    assert ipc.make_hello()["runtime"]["onnxruntime"] == "1.2.3"
    metadata_version.assert_called_once_with("onnxruntime")


def test_hello_does_not_import_onnxruntime(monkeypatch: pytest.MonkeyPatch) -> None:
    """Чтение установленной версии не добавляет тяжёлый рантайм в процесс."""
    monkeypatch.delitem(sys.modules, "onnxruntime", raising=False)
    monkeypatch.setattr(ipc, "version", Mock(return_value="1.2.3"))

    ipc.make_hello()

    assert "onnxruntime" not in sys.modules


def test_error_helper() -> None:
    msg = ipc.error(ipc.BAD_FIELD, "Отсутствует поле")
    assert msg == {"type": "error", "code": "bad-field", "message": "Отсутствует поле"}
    assert ipc.FrameReader().feed(ipc.encode(msg)) == [msg]
