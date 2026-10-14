"""Метаданные источников: стабильный ABI и перечисление без записи звука."""

from __future__ import annotations

import ctypes
import json
import platform
import subprocess
from collections.abc import Iterator
from typing import Any, NoReturn

import pytest

from astra_voice.worker import audio
from astra_voice.worker import pulse_stream as ps

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def isolated_audio(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    def forbidden(*args: object, **kwargs: object) -> NoReturn:
        raise AssertionError("Перечисление не должно открывать микрофон")

    monkeypatch.setattr(ctypes, "CDLL", forbidden)
    monkeypatch.setattr(ps, "_source_metadata_warned", False)
    audio.set_device_introspection(None)
    audio.invalidate_device_cache()
    yield
    audio.set_device_introspection(None)
    audio.invalidate_device_cache()


@pytest.mark.skipif(platform.machine() != "x86_64", reason="ABI Linux x86_64")
def test_full_source_info_abi() -> None:
    assert ctypes.sizeof(audio._PaSampleSpec) == 12
    assert ctypes.sizeof(ps._PaChannelMap) == 132
    assert ctypes.sizeof(ps._PaCVolume) == 132
    assert ps._PaChannelMap.map.offset == ps._PaCVolume.values.offset == 4
    assert ctypes.sizeof(ps._PaSourcePortInfo) == 24
    assert ps._PaSourcePortInfo.priority.offset == 16
    assert ps._PaSourcePortInfo.available.offset == 20
    assert ctypes.sizeof(ps._PaSourceInfo) == 416
    offsets = {
        "name": 0,
        "index": 8,
        "description": 16,
        "sample_spec": 24,
        "channel_map": 36,
        "owner_module": 168,
        "volume": 172,
        "mute": 304,
        "monitor_of_sink": 308,
        "monitor_of_sink_name": 312,
        "latency": 320,
        "driver": 328,
        "flags": 336,
        "proplist": 344,
        "configured_latency": 352,
        "base_volume": 360,
        "state": 364,
        "n_volume_steps": 368,
        "card": 372,
        "n_ports": 376,
        "ports": 384,
        "active_port": 392,
        "n_formats": 400,
        "formats": 408,
    }
    assert {key: getattr(ps._PaSourceInfo, key).offset for key in offsets} == offsets


def native_device(info: ps._PaSourceInfo) -> audio.AudioDevice:
    source = ps.PulseStreamSource()
    source._info_rows = []
    source._source_info_callback(None, ctypes.pointer(info), 0, None)
    assert not source._info_failed
    assert source._stream is None and source._context is None
    return audio._build_devices(source._info_rows, metadata=source._info_metadata)[0]


@pytest.mark.parametrize("monitor", [False, True])
@pytest.mark.parametrize("available,expected", [(0, "unknown"), (1, "no"), (2, "yes")])
@pytest.mark.parametrize("state", [-1, 0, 1, 2])
def test_native_metadata(monitor: bool, available: int, expected: str, state: int) -> None:
    port = ps._PaSourcePortInfo(b"mic", b"Microphone", 100, available)
    info = ps._PaSourceInfo(b"custom-name", 42, b"Readable device")
    info.monitor_of_sink = 7 if monitor else ps.PA_INVALID_INDEX
    info.monitor_of_sink_name = b"sink" if monitor else None
    info.state = state
    info.active_port = ctypes.pointer(port)
    device = native_device(info)
    assert (device.monitor, device.available, device.state) == (monitor, expected, state)


def test_native_null_active_port_is_unknown() -> None:
    info = ps._PaSourceInfo(b"mic", 1, b"Microphone")
    info.monitor_of_sink = ps.PA_INVALID_INDEX
    info.state = 2
    assert native_device(info).available == "unknown"


def test_monitor_name_guard_does_not_dereference_string_pointer() -> None:
    info = ps._PaSourceInfo(b"mic", 1, b"Microphone")
    info.monitor_of_sink = ps.PA_INVALID_INDEX
    # Несогласованная пара: отсутствие sink index + ненулевой адрес строки.
    # Адрес заведомо нечитаемый; проверять нужно только значение указателя.
    name_pointer = ctypes.cast(
        ctypes.byref(info, ps._PaSourceInfo.monitor_of_sink_name.offset),
        ctypes.POINTER(ctypes.c_void_p),
    )
    name_pointer.contents.value = 1
    info.active_port = ctypes.cast(1, ctypes.POINTER(ps._PaSourcePortInfo))
    device = native_device(info)
    assert not device.monitor and device.available == "unknown" and device.state is None


@pytest.mark.parametrize("name,monitor", [(b"custom", False), (b"custom.monitor", True)])
@pytest.mark.parametrize("invalid", ["monitor_name", "monitor_index", "state", "available"])
def test_native_invalid_metadata_falls_back_once(
    name: bytes,
    monitor: bool,
    invalid: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    port = ps._PaSourcePortInfo(b"mic", b"Microphone", 100, 2)
    info = ps._PaSourceInfo(name, 1, b"Microphone")
    info.monitor_of_sink = ps.PA_INVALID_INDEX
    info.state = 2
    info.active_port = ctypes.pointer(port)
    if invalid == "monitor_name":
        info.monitor_of_sink_name = b"inconsistent"
    elif invalid == "monitor_index":
        info.monitor_of_sink = 0
    elif invalid == "state":
        info.state = 99
        # Ранний отказ не должен разыменовать даже этот неверный указатель.
        info.active_port = ctypes.cast(1, ctypes.POINTER(ps._PaSourcePortInfo))
    else:
        port.available = 99
    for _ in range(2):
        device = native_device(info)
        assert (device.monitor, device.available, device.state) == (monitor, "unknown", None)
    assert len(caplog.records) == 1
    assert caplog.records[0].levelname == "WARNING"


@pytest.mark.parametrize("shape", ["list", "dict"])
@pytest.mark.parametrize(
    "value,expected",
    [
        ("available", "yes"),
        ("not available", "no"),
        ("unknown", "unknown"),
        ("availability unknown", "unknown"),
        ("broken", "unknown"),
        (1, "no"),
    ],
)
def test_pactl_active_port_metadata(shape: str, value: object, expected: str) -> None:
    port = {"name": "jack", "availability": value}
    inactive_availability = "not available" if expected == "yes" else "available"
    ports = (
        [port, {"name": "other", "availability": inactive_availability}]
        if shape == "list"
        else {
            "jack": port,
            "other": {"availability": inactive_availability},
        }
    )
    payload = [
        {
            "name": "mic",
            "description": "Microphone",
            "state": "SUSPENDED",
            "active_port": "jack",
            "ports": ports,
        }
    ]
    calls: list[list[str]] = []

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if command == ["pactl", "list", "short", "sources"]:
            return subprocess.CompletedProcess(command, 0, "1\tmic\tdriver\n")
        assert command == ["pactl", "-f", "json", "list", "sources"]
        return subprocess.CompletedProcess(command, 0, json.dumps(payload))

    device = audio.list_devices(run=run)[0]
    assert (device.available, device.state, device.monitor) == (expected, 2, False)
    assert len(calls) == 2


@pytest.mark.parametrize("key", ["monitor_of_sink", "monitor_source"])
@pytest.mark.parametrize(
    "value,expected", [(0, True), (7, True), (None, False), (audio.U32_MAX, False), ("sink", True)]
)
def test_pactl_explicit_monitor(key: str, value: object, expected: bool) -> None:
    assert audio._pactl_source_metadata({key: value}, "custom").monitor is expected


@pytest.mark.parametrize("active", [None, "missing", {}])
def test_pactl_missing_active_port_is_unknown(active: object) -> None:
    item = {"active_port": active, "ports": [{"name": "jack", "availability": "no"}]}
    metadata = audio._pactl_source_metadata(item, "custom.monitor")
    assert metadata.monitor and metadata.available == "unknown"


def test_pipewire_availability_stays_unknown() -> None:
    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if command[0] == "pactl":
            return subprocess.CompletedProcess(command, 1, "")
        assert command == ["pw-dump"]
        payload = [
            {
                "id": 7,
                "info": {
                    "props": {
                        "media.class": "Audio/Source",
                        "node.name": "mic",
                        "node.description": "Mic",
                    }
                },
            }
        ]
        return subprocess.CompletedProcess(command, 0, json.dumps(payload))

    device = audio.list_devices(run=run)[0]
    assert device.available == "unknown" and device.state is None


def test_monitor_default_can_be_described_without_allowing_implicit_capture() -> None:
    monitor = audio.AudioDevice(1, "system", "Колонки", True)
    audio.set_device_introspection(lambda _: ([monitor], monitor.name))
    assert audio.default_device([monitor], allow_monitor=True) == monitor
    with pytest.raises(audio.AudioError, match="звук системы"):
        audio.default_device([monitor])


@pytest.mark.parametrize("stream", [None, 42])
@pytest.mark.parametrize(
    "event",
    [
        ps.PA_SUBSCRIPTION_EVENT_NEW,
        ps.PA_SUBSCRIPTION_EVENT_CHANGE,
        ps.PA_SUBSCRIPTION_EVENT_REMOVE,
    ],
)
def test_source_events_invalidate_both_caches(stream: int | None, event: int) -> None:
    source = ps.PulseStreamSource()
    source._stream = stream
    source._introspection_stale = False
    audio._device_cache.put_devices([audio.AudioDevice(1, "mic", "Mic", False)], lambda: 0)
    source._on_subscribe(1, ps.PA_SUBSCRIPTION_EVENT_SOURCE | event, 1, 0)
    assert source._introspection_stale
    assert audio._device_cache.get(lambda: 0) == (None, None)
