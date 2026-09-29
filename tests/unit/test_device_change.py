"""Классификация смены микрофона и контракт захвата без звуковой службы."""

from __future__ import annotations

import ctypes
import subprocess
import threading
import unicodedata
from collections.abc import Callable
from typing import NoReturn
from unittest.mock import Mock

import pytest
from test_audio import ControlledSource

from astra_voice.worker.audio import (
    DEVICE_CHANGE_BUDGET_S,
    ERROR_FAILED,
    KIND_DEVICE_LOST,
    KIND_SWITCHED,
    REASON_KILLED,
    REASON_MISMATCH,
    REASON_MOVED,
    REASON_REMOVED,
    AudioCapture,
    AudioDevice,
    AudioError,
    DeviceChange,
    _clean_device_description,
    classify_change,
)

pytestmark = pytest.mark.unit

ORIGINAL = AudioDevice(1, "mic.original", "Исходный микрофон", False)
NEXT = AudioDevice(2, "mic.next", "Новый микрофон", False)
MONITOR = AudioDevice(3, "output.monitor", "Монитор", True)


@pytest.fixture(autouse=True)
def no_native_audio(monkeypatch: pytest.MonkeyPatch) -> None:
    """Запрещает тестам выходить к процессам и libpulse."""

    def forbidden(*args: object, **kwargs: object) -> NoReturn:
        raise AssertionError("Тест не должен использовать звуковую службу")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(ctypes, "CDLL", forbidden)


@pytest.mark.parametrize(
    ("reason", "default_mode", "devices", "default", "target", "expected"),
    [
        (REASON_REMOVED, False, [NEXT], None, None, (KIND_DEVICE_LOST, None)),
        (REASON_KILLED, False, [NEXT], None, None, (KIND_DEVICE_LOST, None)),
        (REASON_REMOVED, True, [NEXT], NEXT, None, (KIND_SWITCHED, NEXT.label)),
        (REASON_KILLED, True, [MONITOR], MONITOR, None, (KIND_DEVICE_LOST, None)),
        (REASON_REMOVED, True, [ORIGINAL], ORIGINAL, None, (KIND_DEVICE_LOST, None)),
        (REASON_REMOVED, True, [NEXT], None, None, (KIND_DEVICE_LOST, None)),
        (REASON_MOVED, False, [ORIGINAL, NEXT], None, NEXT.name, (KIND_SWITCHED, NEXT.label)),
        (REASON_MOVED, False, [NEXT], None, NEXT.name, (KIND_DEVICE_LOST, None)),
        (REASON_MOVED, True, [MONITOR], None, MONITOR.name, (KIND_DEVICE_LOST, None)),
        (REASON_MOVED, True, [NEXT], None, "missing", (KIND_DEVICE_LOST, None)),
        (REASON_MISMATCH, True, [NEXT], None, NEXT.name, (KIND_SWITCHED, NEXT.label)),
        (REASON_REMOVED, True, None, NEXT, None, (KIND_DEVICE_LOST, None)),
    ],
)
def test_classify_change(
    reason: str,
    default_mode: bool,
    devices: list[AudioDevice] | None,
    default: AudioDevice | None,
    target: str | None,
    expected: tuple[str, str | None],
) -> None:
    """Решение зависит от причины, режима и свежей идентичности устройств."""
    change = DeviceChange(reason, ORIGINAL, default_mode, moved_to_name=target)
    assert classify_change(change, devices, default) == expected


def test_classification_uses_bounded_clean_label() -> None:
    """Подпись проходит путь очистки AudioDevice и ограничение длины."""
    description = _clean_device_description("А\x00\u202e" * 150)
    target = AudioDevice(2, NEXT.name, description, False)
    change = DeviceChange(REASON_MOVED, ORIGINAL, True, moved_to_name=target.name)
    kind, label = classify_change(change, [target], None)
    assert kind == KIND_SWITCHED
    assert label == target.label
    assert label is not None and len(label) <= 120
    assert all(unicodedata.category(char) not in {"Cc", "Cf"} for char in label)


def _capture(
    source: ControlledSource,
    *,
    changed: Mock | None,
    events: Mock,
    errors: Mock,
    samples: Mock,
    clock: Callable[[], float] = lambda: 0.0,
    devices_fn: Callable[..., list[AudioDevice]] = lambda **_: [NEXT],
    default_fn: Callable[..., AudioDevice] = lambda *_args, **_kwargs: NEXT,
) -> AudioCapture:
    """Собирает захват с проверяемыми callback-ами и без системных запросов."""
    return AudioCapture(
        source=source,
        on_samples=samples,
        on_event=events,
        on_error=errors,
        on_device_change=changed,
        list_devices_fn=devices_fn,
        default_device_fn=default_fn,
        clock=clock,
    )


def _wait(capture: AudioCapture) -> None:
    """Ждёт завершения потока захвата и запрещает оставлять его после теста."""
    thread = capture._thread
    assert thread is not None
    thread.join(timeout=5)
    assert not thread.is_alive()


def test_empty_read_preserves_watchdogs_and_levels() -> None:
    """Пустой опрос не превращается в порцию тишины и не снимает сторож."""
    source = ControlledSource()
    change = DeviceChange(REASON_REMOVED, ORIGINAL, False)
    events, errors, samples, changed = Mock(), Mock(), Mock(), Mock()
    capture = _capture(source, changed=changed, events=events, errors=errors, samples=samples)
    capture._first_chunk_deadline = 2.0
    capture._silent_uid = "uid"
    running = threading.Event()
    running.set()
    source.chunks.put(b"")
    source.chunks.put(None)

    original_read = source.read_chunk
    calls = 0

    def read() -> bytes | None:
        nonlocal calls
        calls += 1
        if calls == 2:
            assert capture._first_chunk_deadline == 2.0
            assert capture._silent_uid == "uid"
            assert samples.call_count == 0
            assert events.call_count == 0
            source.device_change = change
        return original_read()

    source.read_chunk = read  # type: ignore[method-assign]
    capture._read("uid", running, 100.0)
    changed.assert_called_once_with("uid", KIND_DEVICE_LOST, None)
    errors.assert_not_called()
    samples.assert_not_called()
    events.assert_not_called()


def test_change_while_reading_reports_and_exits() -> None:
    """Смена отдаётся автомату ровно раз и завершает поток без audio.error."""
    source = ControlledSource()
    source.device_change = DeviceChange(REASON_MOVED, ORIGINAL, False, NEXT.name)
    source.chunks.put(None)
    events, errors, samples, changed = Mock(), Mock(), Mock(), Mock()
    capture = _capture(
        source,
        changed=changed,
        events=events,
        errors=errors,
        samples=samples,
        devices_fn=lambda **_: [ORIGINAL, NEXT],
    )
    # Смена после open, во время первого чтения.
    source.device_change = None
    original_read = source.read_chunk

    def read() -> bytes | None:
        source.device_change = DeviceChange(REASON_MOVED, ORIGINAL, False, NEXT.name)
        return original_read()

    source.read_chunk = read  # type: ignore[method-assign]
    capture.start("uid", ORIGINAL.name, limit_s=10)
    _wait(capture)
    changed.assert_called_once_with("uid", KIND_SWITCHED, NEXT.label)
    errors.assert_not_called()
    samples.assert_not_called()
    assert any(call.args[0]["type"] == "audio.ready" for call in events.call_args_list)


def test_change_after_open_skips_ready() -> None:
    """Несовпадение сразу после открытия не даёт ошибочного audio.ready."""
    source = ControlledSource()
    source.device_change = DeviceChange(REASON_MISMATCH, ORIGINAL, True, NEXT.name)
    events, errors, samples, changed = Mock(), Mock(), Mock(), Mock()
    capture = _capture(source, changed=changed, events=events, errors=errors, samples=samples)
    capture.start("uid", None, limit_s=10)
    _wait(capture)
    changed.assert_called_once_with("uid", KIND_SWITCHED, NEXT.label)
    events.assert_not_called()
    errors.assert_not_called()


def test_change_after_release_still_reports() -> None:
    """Снятие running во время read_chunk не скрывает установленную смену."""
    source = ControlledSource()
    source.chunks.put(None)
    events, errors, samples, changed = Mock(), Mock(), Mock(), Mock()
    capture = _capture(source, changed=changed, events=events, errors=errors, samples=samples)
    original_read = source.read_chunk

    def read() -> bytes | None:
        capture.request_stop()
        source.device_change = DeviceChange(REASON_REMOVED, ORIGINAL, False)
        return original_read()

    source.read_chunk = read  # type: ignore[method-assign]
    capture.start("uid", ORIGINAL.name, limit_s=10)
    _wait(capture)
    changed.assert_called_once_with("uid", KIND_DEVICE_LOST, None)
    errors.assert_not_called()


@pytest.mark.parametrize("failure", ["error", "expired"])
def test_unavailable_device_list_reports_lost(failure: str) -> None:
    """Отказ списка и истёкший бюджет дают исход без подписи, без ошибки записи."""
    source = ControlledSource()
    source.device_change = DeviceChange(REASON_REMOVED, ORIGINAL, True)
    now = [0.0]

    def devices(*, deadline: object) -> list[AudioDevice]:
        if failure == "error":
            raise AudioError(ERROR_FAILED, "Нет списка")
        now[0] += DEVICE_CHANGE_BUDGET_S
        return [NEXT]

    events, errors, samples, changed = Mock(), Mock(), Mock(), Mock()
    capture = _capture(
        source,
        changed=changed,
        events=events,
        errors=errors,
        samples=samples,
        clock=lambda: now[0],
        devices_fn=devices,
    )
    capture.start("uid", None, limit_s=10)
    _wait(capture)
    changed.assert_called_once_with("uid", KIND_DEVICE_LOST, None)
    errors.assert_not_called()


def test_change_without_handler_preserves_audio_error() -> None:
    """Старые владельцы получают прежнюю ошибку прерванной записи."""
    source = ControlledSource()
    source.device_change = DeviceChange(REASON_REMOVED, ORIGINAL, False)
    events, errors, samples = Mock(), Mock(), Mock()
    capture = _capture(source, changed=None, events=events, errors=errors, samples=samples)
    capture.start("uid", ORIGINAL.name, limit_s=10)
    _wait(capture)
    errors.assert_called_once_with("uid", ERROR_FAILED, "Запись звука прервалась.")
