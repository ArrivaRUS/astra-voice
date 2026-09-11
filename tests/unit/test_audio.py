"""Захват PCM и автомат записи без звуковой подсистемы, сети и дисплея."""

from __future__ import annotations

import ctypes
import json
import logging
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import wave
from array import array
from collections.abc import Callable, Iterator
from functools import partial
from pathlib import Path
from queue import Empty, Queue
from typing import Any, NoReturn, cast
from unittest.mock import Mock

import pytest

from astra_voice.worker.audio import (
    CHANNELS,
    CHUNK_BYTES,
    ERROR_BUSY,
    ERROR_FAILED,
    ERROR_NO_DEVICE,
    LEVEL_RATE_HZ,
    MAX_DEVICE_LABEL,
    OPEN_DEADLINE_S,
    OPEN_FIRST_PAUSE_S,
    OPEN_FIRST_RETRIES,
    OPEN_RETRIES,
    OPEN_RETRY_MS,
    OPEN_TOTAL_DEADLINE_S,
    PA_ERR_ACCESS,
    PA_ERR_BUSY,
    PA_ERR_NOENTITY,
    RATE,
    SAMPLE_BYTES,
    SILENCE_HOLD_S,
    AudioCapture,
    AudioDevice,
    AudioError,
    AudioSource,
    CaptureStopTimeout,
    PulseSimpleSource,
    WavFileSource,
    _OpenDeadline,
    _Pulse,
    dbfs,
    default_device,
    list_devices,
    normalize,
    resolve_device,
)
from astra_voice.worker.ipc import FrameError, encode
from astra_voice.worker.state import Message, State, WorkerState

# tests не пакет; подключаем общие фейки так же, как test_worker_state.py.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fakes import FakeCancelToken, FakeEngine  # noqa: E402

pytestmark = pytest.mark.unit
TIMEOUT = 5.0
WavFactory = Callable[[int, int], Path]
Run = Callable[..., subprocess.CompletedProcess[str]]


@pytest.fixture(autouse=True)
def no_system_audio(monkeypatch: pytest.MonkeyPatch) -> None:
    """Запрещает реальные процессы и загрузку библиотек даже при ошибке теста."""

    def forbidden(*args: object, **kwargs: object) -> NoReturn:
        """Прерывает случайный выход за границы модульного теста."""
        raise AssertionError("Тест не должен запускать процессы или загружать libpulse")

    # Popen блокирует и subprocess.run, захваченный аргументом по умолчанию.
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(ctypes, "CDLL", forbidden)


@pytest.fixture
def short_sources() -> str:
    """Возвращает краткий список микрофона и монитора колонок."""
    return (
        "1\talsa_input.pci.microphone\tmodule-alsa-card.c\ts16le 1ch 16000Hz\tRUNNING\n"
        "2\talsa_output.pci.stereo.monitor\tmodule-alsa-card.c\ts16le 2ch 48000Hz\tIDLE\n"
    )


@pytest.fixture
def descriptions() -> list[dict[str, str]]:
    """Сохраняет кириллицу и пробелы в человекочитаемых описаниях."""
    return [
        {"name": "alsa_input.pci.microphone", "description": "  Встроенный микрофон Ё  "},
        {"name": "alsa_output.pci.stereo.monitor", "description": "Монитор встроенного звука"},
    ]


def pactl_run(short: str, details: str | Exception = "[]", code: int = 0) -> Run:
    """Подставляет ответы обеих команд pactl, не вызывая subprocess."""

    def run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        """Выбирает фикстуру по точной команде."""
        if args == ["pactl", "list", "short", "sources"]:
            return subprocess.CompletedProcess(args, 0, short, "")
        assert args == ["pactl", "-f", "json", "list", "sources"]
        if isinstance(details, Exception):
            raise details
        return subprocess.CompletedProcess(args, code, details, "")

    return run


def default_source_run(output: str | Exception, code: int = 0) -> Run:
    """Подставляет результат запроса умолчания, не обращаясь к звуковому серверу."""

    def run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        """Принимает только фиксированную команду поиска источника."""
        assert args == ["pactl", "get-default-source"]
        if isinstance(output, Exception):
            raise output
        return subprocess.CompletedProcess(args, code, output, "техническая ошибка")

    return run


@pytest.mark.parametrize("elapsed", [None, 0.0, 6.75])
def test_default_device_command_and_deadline(
    elapsed: float | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """У44: имя ищется точно; команда без shell делит общий бюджет открытия."""
    microphone = AudioDevice(2, "alsa_input.usb.microphone", "USB-микрофон", False)
    devices = [AudioDevice(1, "alsa_input.other", "Другой микрофон", False), microphone]
    run = Mock(wraps=default_source_run(f"  {microphone.name}\n"))
    monkeypatch.setenv("LC_ALL", "ru_RU.UTF-8")
    monkeypatch.setenv("ASTRA_VOICE_TEST_ENV", "preserved")
    now = 0.0
    deadline = None if elapsed is None else _OpenDeadline(lambda: now)
    now = elapsed or 0.0

    assert default_device(devices, run=run, deadline=deadline) is microphone
    run.assert_called_once()
    assert run.call_args.args == (["pactl", "get-default-source"],)
    kwargs = run.call_args.kwargs
    assert kwargs["capture_output"] is True
    assert kwargs["text"] is True
    assert kwargs["shell"] is False
    assert kwargs["check"] is False
    assert kwargs["timeout"] == min(5, OPEN_TOTAL_DEADLINE_S - now)
    assert kwargs["env"]["LC_ALL"] == "C"
    assert kwargs["env"]["ASTRA_VOICE_TEST_ENV"] == "preserved"


def test_default_device_expired_deadline_never_runs_command() -> None:
    """Исчерпанный общий бюджет запрещает даже запуск pactl."""
    now = 0.0
    deadline = _OpenDeadline(lambda: now)
    now = OPEN_TOTAL_DEADLINE_S
    run = Mock()
    with pytest.raises(AudioError) as caught:
        default_device([], run=run, deadline=deadline)
    assert caught.value.code == ERROR_FAILED
    run.assert_not_called()


def test_list_devices_descriptions_and_monitor(
    short_sources: str, descriptions: list[dict[str, str]]
) -> None:
    """Имена, индексы, кириллица и предупреждение о звуке системы сохраняются."""
    devices = list_devices(
        run=pactl_run(short_sources, json.dumps(descriptions, ensure_ascii=False))
    )
    assert [(item.index, item.name, item.monitor) for item in devices] == [
        (1, descriptions[0]["name"], False),
        (2, descriptions[1]["name"], True),
    ]
    for device, expected in zip(devices, descriptions, strict=True):
        assert device.description == expected["description"].strip()
    assert devices[0].label == descriptions[0]["description"].strip()
    assert "звук системы" in devices[1].label.casefold()
    assert descriptions[1]["description"] in devices[1].label


@pytest.mark.parametrize(
    ("details", "code"),
    [
        (OSError("JSON не поддерживается"), 0),
        (subprocess.CalledProcessError(1, "pactl"), 0),
        ("это не JSON", 0),
        ('{"sources": []}', 0),
        ('[{"name": "alsa_input.pci.microphone", "description": "описание"}]', 1),
    ],
    ids=["unsupported", "command-error", "garbage", "wrong-shape", "nonzero"],
)
def test_list_devices_description_fallback(
    short_sources: str, details: str | Exception, code: int
) -> None:
    """Недоступные описания заменяются понятными названиями без потери устройств."""
    devices = list_devices(run=pactl_run(short_sources, details, code))
    assert len(devices) == 2
    # Техническое имя больше не служит запасным текстом интерфейса (У39).
    assert [device.description for device in devices] == ["Микрофон", "Звук системы"]
    assert all("alsa_" not in device.description + device.label for device in devices)
    assert devices[1].monitor


@pytest.mark.parametrize(
    ("description", "expected"),
    [
        (" \n\r\tМикрофон\u00a0\u2028\u2029 USB\x00\u202e\u200b \t", "Микрофон USB"),
        (" \n\r\t\x00\x01\x7f\u202e\u200b ", None),
        ("\x00\u202e" * 200 + "Микрофон", "Микрофон"),
    ],
)
def test_list_devices_cleans_descriptions(
    short_sources: str, description: str, expected: str | None
) -> None:
    """T-41: очистка до AudioDevice сохраняет слова и включает запасные подписи."""
    details = [
        {"name": line.split("\t")[1], "description": description}
        for line in short_sources.splitlines()
    ]
    devices = list_devices(run=pactl_run(short_sources, json.dumps(details)))
    assert [device.description for device in devices] == (
        [expected, expected] if expected is not None else ["Микрофон", "Звук системы"]
    )
    assert devices[1].label.endswith(" (звук системы)")


def test_fallback_names_are_numbered(short_sources: str) -> None:
    """Однотипные источники различаются по порядку, без технических имён."""
    short = short_sources + (
        "3\talsa_input.usb.second\n4\talsa_output.usb.second.monitor\n5\talsa_input.usb.third\n"
    )
    devices = list_devices(run=pactl_run(short))
    assert [device.description for device in devices] == [
        "Микрофон",
        "Звук системы",
        "Микрофон 2",
        "Звук системы 2",
        "Микрофон 3",
    ]
    assert all("alsa_" not in device.description + device.label for device in devices)
    assert all("(звук системы)" in device.label for device in devices if device.monitor)


@pytest.mark.parametrize("monitor", [False, True])
def test_device_label_is_bounded(monitor: bool) -> None:
    """Длинная кириллическая подпись обрезается, пометка монитора сохраняется."""
    description = "Ё" * (MAX_DEVICE_LABEL + 50)
    device = AudioDevice(1, "alsa_input.private", description, monitor)
    assert len(device.label) == MAX_DEVICE_LABEL
    assert "…" in device.label
    assert device.label.endswith(" (звук системы)" if monitor else "…")
    assert "alsa_" not in device.label


def test_list_devices_skips_broken_rows(short_sources: str) -> None:
    """Битые строки между корректными не мешают прочитать оставшиеся источники."""
    first, second = short_sources.splitlines(keepends=True)
    broken = "\nбез табуляции\nнет-индекса\tbad\n-1\tbad\n3\t\n\tbad\n4\t   \n"
    devices = list_devices(run=pactl_run(first + broken + second))
    assert [(device.index, device.name) for device in devices] == [
        (1, "alsa_input.pci.microphone"),
        (2, "alsa_output.pci.stereo.monitor"),
    ]


def test_list_devices_nonzero_short_list() -> None:
    """Ошибка обязательного краткого списка выдаёт машинный код audio-failed."""

    def run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        """Имитирует отказ первой команды, запрещая продолжение опроса."""
        assert args == ["pactl", "list", "short", "sources"]
        return subprocess.CompletedProcess(args, 1, "", "сервер недоступен")

    with pytest.raises(AudioError) as caught:
        list_devices(run=run)
    assert caught.value.code == ERROR_FAILED


def test_resolve_device_fail_closed(short_sources: str) -> None:
    """T-35: неизвестное имя не подменяется первым или системным устройством."""
    devices = list_devices(run=pactl_run(short_sources))
    assert resolve_device(None, devices) is None
    assert resolve_device(devices[1].name, devices) is devices[1]
    with pytest.raises(AudioError) as caught:
        resolve_device("missing.microphone", devices)
    assert caught.value.code == ERROR_NO_DEVICE


def test_unknown_device_never_loads_libpulse(
    short_sources: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Неизвестный источник отклоняется до CDLL: молчаливой подмены нет."""
    calls: list[str] = []

    def cdll(name: str, **kwargs: object) -> NoReturn:
        """Регистрирует запрещённую попытку загрузки библиотеки."""
        calls.append(name)
        raise AssertionError("До libpulse доходить нельзя")

    monkeypatch.setattr(ctypes, "CDLL", cdll)
    devices = list_devices(run=pactl_run(short_sources))
    source = PulseSimpleSource(devices=lambda: devices)
    with pytest.raises(AudioError) as caught:
        source.open("missing.microphone")
    assert caught.value.code == ERROR_NO_DEVICE
    assert calls == []
    assert not source.is_open


class FakeFunction:
    """Запоминает явные присваивания ABI, включая restype=None для void."""

    def __init__(self) -> None:
        """Оставляет таблицу пустой до настройки обёрткой."""
        self.assigned: dict[str, object] = {}

    def __setattr__(self, name: str, value: object) -> None:
        """Отличает отсутствие присваивания от явного значения None."""
        if name != "assigned":
            self.assigned[name] = value
        object.__setattr__(self, name, value)


class FakeLibrary:
    """Выдаёт функции с регистрируемыми атрибутами вместо нативного кода."""

    def __init__(self) -> None:
        """Создаёт независимую таблицу символов библиотеки."""
        self.functions: dict[str, FakeFunction] = {}

    def __getattr__(self, name: str) -> FakeFunction:
        """Сохраняет один объект на каждый запрошенный символ."""
        return self.functions.setdefault(name, FakeFunction())


@pytest.fixture
def pulse_library(monkeypatch: pytest.MonkeyPatch) -> Mock:
    """Подменяет только ABI библиотек, сохраняя настоящий _Pulse.open с дедлайном."""
    simple = Mock()
    simple.pa_simple_new.return_value = 123
    simple.pa_simple_get_latency.return_value = 1200
    simple.pa_simple_read.return_value = 0
    pulse = Mock()
    pulse.pa_strerror.return_value = b"fake error"
    libraries = {"libpulse-simple.so.0": simple, "libpulse.so.0": pulse}
    monkeypatch.setattr(ctypes, "CDLL", lambda name, **kwargs: libraries[name])
    return simple


@pytest.mark.parametrize(
    ("error_code", "expected"),
    [
        (PA_ERR_BUSY, ERROR_BUSY),
        (PA_ERR_ACCESS, ERROR_BUSY),
        (PA_ERR_NOENTITY, ERROR_NO_DEVICE),
        (0, ERROR_FAILED),
        (17, ERROR_FAILED),
    ],
)
def test_pulse_null_maps_errors_and_retries(
    pulse_library: Mock,
    error_code: int,
    expected: str,
) -> None:
    """NULL даёт публичный код, ровно OPEN_RETRIES попыток и паузы между ними."""
    timeline: list[tuple[str, float]] = []

    def refuse(*args: Any) -> None:
        """Возвращает ошибку через настоящий выходной указатель ctypes."""
        ctypes.cast(args[-1], ctypes.POINTER(ctypes.c_int))[0] = error_code
        timeline.append(("open", 0))

    pulse_library.pa_simple_new.side_effect = refuse
    # У44: проверка NULL-дескриптора требует конкретного микрофона, dev=NULL запрещён.
    device = AudioDevice(1, "alsa_input.chosen", "Микрофон", False)
    source = PulseSimpleSource(
        devices=lambda: [device], sleep=lambda delay: timeline.append(("sleep", delay))
    )
    with pytest.raises(AudioError) as caught:
        source.open(device.name)
    assert caught.value.code == expected
    assert pulse_library.pa_simple_new.call_count == OPEN_RETRIES
    assert timeline == [("open", 0), ("sleep", OPEN_RETRY_MS / 1000)] * (OPEN_RETRIES - 1) + [
        ("open", 0)
    ]
    assert not source.is_open
    assert source.selected_device is None
    source.close()
    pulse_library.pa_simple_free.assert_not_called()
    pulse_library.pa_simple_get_latency.assert_not_called()


def test_pulse_deadline_frees_late_handle(
    pulse_library: Mock,
    monkeypatch: pytest.MonkeyPatch,
    wait_capture: Callable[[], None],
) -> None:
    """T-40: поздний дескриптор освобождает тот же поток, который вошёл в C-вызов."""
    entered = threading.Event()
    release = threading.Event()
    now = 0.0
    results: list[tuple[ctypes.c_void_p | None, int]] = []
    owners: list[threading.Thread] = []

    def hung_new(*args: object) -> int:
        """Держит дескриптор до явного разрешения после истечения дедлайна."""
        owners.append(threading.current_thread())
        entered.set()
        assert release.wait(TIMEOUT)
        return 123

    def free(handle: ctypes.c_void_p) -> None:
        assert handle.value == 123
        owners.append(threading.current_thread())

    pulse_library.pa_simple_new.side_effect = hung_new
    pulse_library.pa_simple_free.side_effect = free
    monkeypatch.setattr("astra_voice.worker.audio.time", Mock(monotonic=lambda: now))
    pulse = _Pulse()
    owner = threading.Thread(target=lambda: results.append(pulse.open("alsa_input.chosen")))
    owner.start()
    try:
        assert entered.wait(TIMEOUT)
        now = OPEN_DEADLINE_S + 1
        # Старое ожидание бросало pulse-open в фоне. Теперь до возврата C-вызова
        # жив сам владелец: дедлайн не даёт права передать или освободить handle.
        assert owner.is_alive()
        assert results == []
        pulse_library.pa_simple_new.assert_called_once()
        pulse_library.pa_simple_free.assert_not_called()
    finally:
        release.set()
        wait_capture()
    pulse_library.pa_simple_free.assert_called_once()
    assert pulse_library.pa_simple_free.call_args.args[0].value == 123
    assert results == [(None, 0)]
    assert owners == [owner, owner]


def test_pulse_reopen_and_close_after_exception(pulse_library: Mock) -> None:
    """Повторное открытие и закрытие после сбоя освобождают каждый дескриптор один раз."""
    device = AudioDevice(1, "alsa_input.chosen", "Микрофон", False)
    pulse_library.pa_simple_new.side_effect = [123, 456]
    pulse_library.pa_simple_flush.return_value = -1
    source = PulseSimpleSource(devices=lambda: [device])
    try:
        source.open(device.name)
        assert source.selected_device is device
        source.open(device.name)
        assert [call.args[0].value for call in pulse_library.pa_simple_free.call_args_list] == [123]
        with pytest.raises(AudioError) as caught:
            source.flush()
        assert caught.value.code == ERROR_FAILED
    finally:
        source.close()
        source.close()
    assert [call.args[0].value for call in pulse_library.pa_simple_free.call_args_list] == [
        123,
        456,
    ]
    assert not source.is_open
    assert source.selected_device is None


@pytest.mark.parametrize("latency", [1200, (1 << 64) - 1])
def test_pulse_latency_only_on_explicit_diagnostic_request(
    pulse_library: Mock,
    latency: int,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """T-40: открытие не запрашивает timing-info; диагностика доступна явно."""
    pulse_library.pa_simple_get_latency.return_value = latency
    device = AudioDevice(1, "alsa_input.secret", "Личный микрофон", False)
    source = PulseSimpleSource(devices=lambda: [device])
    try:
        with caplog.at_level(logging.DEBUG, logger="astra_voice.worker.audio"):
            source.open(device.name)
        # Раньше тест требовал запрос latency из open; теперь это запрещено T-40.
        pulse_library.pa_simple_get_latency.assert_not_called()
        assert source.is_open
        assert caplog.messages == []
        assert source.latency_us() == (1200 if latency == 1200 else None)
        pulse_library.pa_simple_get_latency.assert_called_once()
        assert pulse_library.pa_simple_get_latency.call_args.args[0].value == 123
        assert device.name not in caplog.text
        assert device.description not in caplog.text
    finally:
        source.close()


def test_pulse_declares_all_function_signatures(monkeypatch: pytest.MonkeyPatch) -> None:
    """Регресс SIGSEGV: у всех функций указан ABI, указатель new не усекается."""
    libraries: dict[str, FakeLibrary] = {}

    def cdll(name: str, **kwargs: object) -> FakeLibrary:
        """Возвращает только фейковую библиотеку."""
        library = FakeLibrary()
        libraries[name] = library
        return library

    monkeypatch.setattr(ctypes, "CDLL", cdll)
    _Pulse()
    expected = {
        "libpulse-simple.so.0": {
            "pa_simple_new",
            "pa_simple_read",
            "pa_simple_flush",
            "pa_simple_get_latency",
            "pa_simple_free",
        },
        "libpulse.so.0": {"pa_strerror"},
    }
    assert libraries.keys() == expected.keys()
    for name, symbols in expected.items():
        assert libraries[name].functions.keys() == symbols
        for symbol in symbols:
            assigned = libraries[name].functions[symbol].assigned
            assert "argtypes" in assigned, symbol
            assert "restype" in assigned, symbol
            assert isinstance(assigned["argtypes"], list) and assigned["argtypes"], symbol
    new = libraries["libpulse-simple.so.0"].functions["pa_simple_new"]
    assert new.assigned["restype"] is ctypes.c_void_p


@pytest.mark.parametrize("failed_library", ["libpulse-simple.so.0", "libpulse.so.0"])
def test_pulse_load_failure(monkeypatch: pytest.MonkeyPatch, failed_library: str) -> None:
    """Отказ загрузки любой библиотеки превращается в AudioError."""

    def cdll(name: str, **kwargs: object) -> FakeLibrary:
        """Выдаёт OSError на выбранной библиотеке без нативной загрузки."""
        if name == failed_library:
            raise OSError("библиотека недоступна")
        return FakeLibrary()

    monkeypatch.setattr(ctypes, "CDLL", cdll)
    with pytest.raises(AudioError) as caught:
        _Pulse()
    assert caught.value.code == ERROR_FAILED


@pytest.mark.parametrize("failed", [False, True])
def test_pulse_flush_calls_server(monkeypatch: pytest.MonkeyPatch, failed: bool) -> None:
    """Сброс обращается к libpulse; при отказе продолжать запись нельзя."""
    pulse = Mock(spec=_Pulse)
    pulse.lib = Mock()
    handle = ctypes.c_void_p(123)
    pulse.open.return_value = (handle, 0)
    pulse.lib.pa_simple_flush.return_value = -1 if failed else 0
    pulse.lib.pa_simple_get_latency.return_value = 1000
    monkeypatch.setattr("astra_voice.worker.audio._Pulse", lambda: pulse)
    # У44: тест сброса открывает проверенный микрофон, сервер не выбирает умолчание.
    device = AudioDevice(1, "alsa_input.chosen", "Микрофон", False)
    source = PulseSimpleSource(devices=lambda: [device])
    source.flush()
    pulse.lib.pa_simple_flush.assert_not_called()
    source.open(device.name)
    try:
        if failed:
            with pytest.raises(AudioError) as caught:
                source.flush()
            assert caught.value.code == ERROR_FAILED
        else:
            source.flush()
        pulse.lib.pa_simple_flush.assert_called_once()
        assert pulse.lib.pa_simple_flush.call_args.args[0] is handle
    finally:
        source.close()
    source.flush()
    pulse.lib.pa_simple_flush.assert_called_once()


@pytest.fixture
def wav_factory(tmp_path: Path) -> WavFactory:
    """Генерирует временный WAV с целым числом порций PCM16."""

    def create(amplitude: int, chunks: int) -> Path:
        """Записывает постоянный сигнал либо цифровую тишину."""
        path = tmp_path / "source.wav"
        with wave.open(str(path), "wb") as wav:
            wav.setparams((CHANNELS, SAMPLE_BYTES, RATE, 0, "NONE", "not compressed"))
            wav.writeframes(struct.pack("<h", amplitude) * (CHUNK_BYTES // SAMPLE_BYTES) * chunks)
        return path

    return create


@pytest.fixture
def wait_capture(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[[], None]]:
    """Ждёт полного выхода потока, чтобы отсутствие поздних событий было проверяемо."""
    threads: list[ObservedThread] = []

    class ObservedThread(threading.Thread):
        """Добавляет сигнал завершения, не меняя цикл чтения AudioCapture."""

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            """Регистрирует создаваемый поток для ожидания и уборки."""
            super().__init__(*args, **kwargs)
            self.finished = threading.Event()
            threads.append(self)

        def run(self) -> None:
            """Сигнализирует только после возврата всей функции потока."""
            try:
                super().run()
            finally:
                self.finished.set()

    def wait() -> None:
        """Ожидает все уже запущенные потоки с ограничением времени."""
        assert threads
        for thread in threads:
            assert thread.finished.wait(TIMEOUT), "Поток захвата не завершился"
            thread.join(timeout=TIMEOUT)
            assert not thread.is_alive()

    monkeypatch.setattr(threading, "Thread", ObservedThread)
    yield wait
    for thread in threads:
        thread.join(timeout=TIMEOUT)
        assert not thread.is_alive(), "После теста остался фоновый поток"


class CaptureProbe:
    """Собирает события и PCM, продвигая инъектированные часы при приёме порции."""

    def __init__(
        self,
        source: AudioSource,
        *,
        step: float = 0.0,
        sink: Callable[[str, array[float]], bool] | None = None,
        on_error: Callable[[str, str, str], None] | None = None,
    ) -> None:
        """Подключает наблюдаемые callback-и к настоящему захвату."""
        self.now = 0.0
        self.sleeps: list[float] = []
        self.step = step
        self.sink = sink
        self.error_sink = on_error
        self.events: Queue[tuple[float, Message]] = Queue()
        self.errors: Queue[tuple[str, str, str]] = Queue()
        self.samples: list[array[float]] = []
        self.accepted: list[bool] = []
        self.capture = AudioCapture(
            source=source,
            on_samples=self.on_samples,
            on_event=self.on_event,
            on_error=self.on_error,
            clock=lambda: self.now,
            sleep=self.sleep,
        )

    def sleep(self, delay: float) -> None:
        """Продвигает виртуальное время повторов без реального ожидания."""
        self.sleeps.append(delay)
        self.now += delay

    def on_samples(self, uid: str, samples: array[float]) -> bool:
        """Учитывает порцию и возвращает решение настоящего автомата."""
        self.now += self.step
        self.samples.append(samples)
        accepted = self.sink(uid, samples) if self.sink is not None else True
        self.accepted.append(accepted)
        return accepted

    def on_event(self, event: Message) -> None:
        """Сохраняет событие вместе с показанием виртуальных часов."""
        self.events.put((self.now, event))

    def on_error(self, uid: str, code: str, message: str) -> None:
        """Сохраняет ошибку и при необходимости передаёт её автомату."""
        self.errors.put((uid, code, message))
        if self.error_sink is not None:
            self.error_sink(uid, code, message)

    def drain(self) -> list[tuple[float, Message]]:
        """Читает накопленные события после синхронизации с потоком."""
        result: list[tuple[float, Message]] = []
        while True:
            try:
                result.append(self.events.get_nowait())
            except Empty:
                return result


@pytest.fixture
def probes() -> Iterator[list[CaptureProbe]]:
    """Освобождает каждый источник даже при падении проверки."""
    created: list[CaptureProbe] = []
    yield created
    for probe in created:
        probe.capture.stop()


def test_signal_levels_are_rate_limited(
    wav_factory: WavFactory, wait_capture: Callable[[], None], probes: list[CaptureProbe]
) -> None:
    """Уровни сигнала содержат RMS/peak и прореживаются по виртуальным часам."""
    step = 1 / (LEVEL_RATE_HZ * 8)
    probe = CaptureProbe(WavFileSource(wav_factory(16384, 100)), step=step)
    probes.append(probe)
    probe.capture.start("signal", None)
    wait_capture()
    levels = [(stamp, event) for stamp, event in probe.drain() if event["type"] == "level"]
    assert 2 <= len(levels) < len(probe.samples) == 100
    for _stamp, event in levels:
        assert event["utterance_id"] == "signal"
        assert event["rms_dbfs"] == pytest.approx(-6.0, abs=0.1)
        assert event["peak_dbfs"] == pytest.approx(-6.0, abs=0.1)
    for (previous, _), (current, _) in zip(levels, levels[1:], strict=False):
        assert current - previous >= 1 / LEVEL_RATE_HZ - 1e-12
    assert probe.errors.empty()
    assert not probe.capture.active


def test_silence_is_reported_once_after_hold(
    wav_factory: WavFactory, wait_capture: Callable[[], None], probes: list[CaptureProbe]
) -> None:
    """Длительная тишина даёт одно событие после выдержки, без повторов."""
    step = SILENCE_HOLD_S / 8
    probe = CaptureProbe(WavFileSource(wav_factory(0, 32)), step=step)
    probes.append(probe)
    probe.capture.start("silence", None)
    wait_capture()
    silent = [(stamp, event) for stamp, event in probe.drain() if event["type"] == "silent"]
    assert len(probe.samples) == 32
    assert len(silent) == 1
    stamp, event = silent[0]
    assert SILENCE_HOLD_S <= stamp <= SILENCE_HOLD_S + step
    assert event == {"type": "silent", "utterance_id": "silence"}
    assert probe.now >= 4 * SILENCE_HOLD_S
    assert probe.errors.empty()


@pytest.fixture
def worker_factory(tmp_path: Path) -> Iterator[Callable[..., tuple[WorkerState, FakeEngine]]]:
    """Создаёт автомат с общими фейками движка и гарантирует его закрытие."""
    workers: list[WorkerState] = []

    def create(**kwargs: Any) -> tuple[WorkerState, FakeEngine]:
        """Загружает фейковую модель без numpy, файлов модели и сети."""
        engine = FakeEngine()
        worker = WorkerState(
            engine_factory=lambda layout: engine,
            cancel_factory=FakeCancelToken,
            audio_factory=list,
            data_dir_factory=lambda: tmp_path,
            runtime="fake",
            **kwargs,
        )
        workers.append(worker)
        assert (
            worker.handle(
                {
                    "type": "model.load",
                    "id": "gigaam",
                    "revision": "r1",
                    "dir": str(tmp_path / "model"),
                    "layout": "fake",
                    "variant": "int8",
                    "threads": 1,
                    "min_ram_mb": 512,
                }
            )[0]["type"]
            == "model.loaded"
        )
        return worker, engine

    yield create
    for worker in workers:
        worker.close()


class CountingWavSource(WavFileSource):
    """Считает обращения к настоящему WAV-источнику после достижения лимита."""

    def __init__(self, path: Path) -> None:
        """Сохраняет публичный счётчик чтений тестового источника."""
        super().__init__(path)
        self.read_calls = 0

    def read_chunk(self) -> bytes | None:
        """Делегирует чтение без изменения данных и признака ended."""
        self.read_calls += 1
        return super().read_chunk()


def test_record_limit_stops_reading_without_extra_samples(
    monkeypatch: pytest.MonkeyPatch,
    wav_factory: WavFactory,
    wait_capture: Callable[[], None],
    probes: list[CaptureProbe],
    worker_factory: Callable[..., tuple[WorkerState, FakeEngine]],
) -> None:
    """Лимит внутри второй порции обрезает PCM и останавливает цикл через False."""
    events: Queue[Message] = Queue()
    source = CountingWavSource(wav_factory(8192, 10))
    # Замыкание связывает callback с автоматом после передачи capture в конструктор.
    probe = CaptureProbe(source, sink=lambda uid, samples: worker.on_samples(uid, samples))
    probes.append(probe)
    worker, _ = worker_factory(capture=probe.capture, limit_s=0.025, on_event=events.put)
    stop = Mock(side_effect=AssertionError("Лимит не должен ждать поток или закрывать источник"))
    with monkeypatch.context() as patch:
        patch.setattr(probe.capture, "stop", stop)
        assert worker.handle({"type": "record.start", "utterance_id": "limit"}) == []
        assert events.get(timeout=TIMEOUT) == {"type": "record.limit", "utterance_id": "limit"}
        wait_capture()
    stop.assert_not_called()
    assert worker.state is State.idle
    assert worker.buffers == {"limit": array("f", [0.25] * 400)}
    assert probe.accepted == [True, False]
    assert len(probe.samples) == 2
    assert source.read_calls == 2
    assert not source.ended
    assert not source.is_open
    assert not probe.capture.active
    assert probe.errors.empty()
    assert events.empty()


def test_wav_eof_is_not_an_error(
    wav_factory: WavFactory, wait_capture: Callable[[], None], probes: list[CaptureProbe]
) -> None:
    """Конец WAV завершает поток штатно, не вызывая on_error."""
    source = WavFileSource(wav_factory(8192, 2))
    probe = CaptureProbe(source)
    probes.append(probe)
    probe.capture.start("eof", None)
    wait_capture()
    assert source.ended
    assert not source.is_open
    assert len(probe.samples) == 2
    assert not probe.capture.active
    assert probe.errors.empty()
    assert all(event["type"] != "error" for _, event in probe.drain())


class ControlledSource:
    """Источник с управляемыми порциями и счётчиками открытий и закрытий."""

    def __init__(self, label: str | None = None) -> None:
        """Подготавливает очередь, позволяющую остановить чтение без sleep."""
        self.device_label = label
        self.device_name: str | None = None
        self.is_open = False
        self.ended = False
        self.open_calls = 0
        self.close_calls = 0
        self.flush_calls = 0
        self.chunks: Queue[bytes | None] = Queue()
        self.reading = threading.Event()
        self.keep_reading: Callable[[], bool] | None = None

    def open(self, device: str | None) -> None:
        """Учитывает ленивое открытие выбранного источника."""
        self.open_calls += 1
        self.is_open = True
        self.device_name = device

    def read_chunk(self) -> bytes | None:
        """Ждёт разрешения теста на данные или обрыв источника."""
        self.reading.set()
        if self.keep_reading is None:
            return self.chunks.get(timeout=TIMEOUT)
        # Короткие ожидания очереди позволяют фейку реагировать на публичный
        # признак остановки. Общий предел ожидания — те же пять секунд.
        for _ in range(500):
            if not self.keep_reading():
                return None
            try:
                return self.chunks.get(timeout=TIMEOUT / 500)
            except Empty:
                continue
        raise AssertionError("Захват не остановил чтение источника")

    def flush(self) -> None:
        """Учитывает сброс перед повторным использованием открытого источника."""
        self.flush_calls += 1

    def close(self) -> None:
        """Учитывает освобождение открытого источника."""
        if self.is_open:
            self.close_calls += 1
        self.is_open = False
        self.device_name = None


class BufferedSource(ControlledSource):
    """Имитирует серверный буфер: открытый микрофон копит звук во время паузы."""

    def __init__(self, *, ignore_close: bool) -> None:
        super().__init__()
        self.ignore_close = ignore_close
        self.buffered: list[bytes] = []
        self.flushes_at_read: list[int] = []

    def pause_audio(self, chunk: bytes) -> None:
        """Добавляет чужую речь, только если микрофон остался открытым."""
        if self.is_open:
            self.buffered.append(chunk)

    def open(self, device: str | None) -> None:
        """Новое соединение не наследует данные предыдущей записи."""
        super().open(device)
        self.buffered.clear()

    def flush(self) -> None:
        """Сбрасывает данные паузы у переиспользуемого соединения."""
        super().flush()
        self.buffered.clear()

    def read_chunk(self) -> bytes | None:
        """Сначала отдаёт накопленное сервером, затем новые данные записи."""
        self.flushes_at_read.append(self.flush_calls)
        if self.buffered:
            return self.buffered.pop(0)
        return super().read_chunk()

    def close(self) -> None:
        """Позволяет отдельно проверить страховку для незакрытого источника."""
        if self.ignore_close:
            self.close_calls += 1
        else:
            super().close()


@pytest.mark.parametrize("ignore_close", [False, True], ids=["reopen", "flush-open-source"])
def test_two_recordings_never_include_pause_audio(
    ignore_close: bool,
    wait_capture: Callable[[], None],
    probes: list[CaptureProbe],
    worker_factory: Callable[..., tuple[WorkerState, FakeEngine]],
) -> None:
    """Один захват и источник не переносят чужую речь из паузы во вторую запись."""
    source = BufferedSource(ignore_close=ignore_close)
    worker, _ = worker_factory()
    probe = CaptureProbe(source, sink=worker.on_samples, on_error=worker.on_error)
    probes.append(probe)
    source.keep_reading = lambda: probe.capture.active
    worker.set_capture(probe.capture)
    for index, amplitude in enumerate((8192, 16384), start=1):
        source.chunks.put(struct.pack("<h", amplitude) * (CHUNK_BYTES // SAMPLE_BYTES))
        assert worker.handle({"type": "record.start", "utterance_id": str(index)}) == []
        expected_opens = 1 if ignore_close else index
        if index == 1 or not ignore_close:
            assert probe.events.get(timeout=TIMEOUT)[1] == {"type": "audio.ready"}
        assert probe.events.get(timeout=TIMEOUT)[1]["type"] == "level"
        assert worker.handle({"type": "record.stop", "utterance_id": str(index)}) == []
        wait_capture()
        assert source.open_calls == expected_opens
        assert source.close_calls >= index
        assert source.is_open == ignore_close
        assert source.flush_calls == (index - 1 if ignore_close else 0)
        assert worker.buffers[str(index)] == array(
            "f", [amplitude / 32768] * (CHUNK_BYTES // SAMPLE_BYTES)
        )
        assert probe.errors.empty()
        assert probe.drain() == []
        if index == 1:
            # Виртуальная пауза: чужая речь возникает после полного выхода потока.
            source.pause_audio(struct.pack("<h", -16384) * (CHUNK_BYTES // SAMPLE_BYTES))
            assert bool(source.buffered) == ignore_close
            source.flushes_at_read.clear()

    assert len(probe.samples) == 2
    assert source.flushes_at_read
    assert set(source.flushes_at_read) == {int(ignore_close)}
    assert source.buffered == []


def test_source_interruption_reports_error(
    wait_capture: Callable[[], None],
    probes: list[CaptureProbe],
    worker_factory: Callable[..., tuple[WorkerState, FakeEngine]],
) -> None:
    """None без ended вызывает on_error и удаляет неудавшуюся запись автомата."""
    events: Queue[Message] = Queue()
    worker, _ = worker_factory(on_event=events.put, limit_s=CHUNK_BYTES / (RATE * SAMPLE_BYTES))
    source = ControlledSource()
    source.chunks.put(None)
    closed_at_error: list[tuple[bool, int]] = []

    def on_error(uid: str, code: str, message: str) -> None:
        """Проверяет состояние источника именно в момент передачи ошибки."""
        closed_at_error.append((source.is_open, source.close_calls))
        worker.on_error(uid, code, message)

    probe = CaptureProbe(source, sink=worker.on_samples, on_error=on_error)
    probes.append(probe)
    worker.set_capture(probe.capture)
    assert worker.handle({"type": "record.start", "utterance_id": "broken"}) == []
    uid, code, message = probe.errors.get(timeout=TIMEOUT)
    assert (uid, code) == ("broken", ERROR_FAILED)
    assert message
    error = events.get(timeout=TIMEOUT)
    assert (error["type"], error["code"], error["utterance_id"]) == (
        "error",
        ERROR_FAILED,
        "broken",
    )
    wait_capture()
    assert not source.ended
    assert not probe.capture.active
    assert worker.state is State.idle
    assert worker.buffers == {}
    assert closed_at_error == [(False, 1)]
    assert not source.is_open
    assert source.close_calls == 1

    source.chunks.put(struct.pack("<h", 8192) * (CHUNK_BYTES // SAMPLE_BYTES))
    assert worker.handle({"type": "record.start", "utterance_id": "recovered"}) == []
    wait_capture()
    assert source.open_calls == source.close_calls == 2
    assert not source.is_open
    assert worker.buffers == {"recovered": array("f", [0.25] * (CHUNK_BYTES // SAMPLE_BYTES))}
    assert probe.errors.empty()
    assert [event for _, event in probe.drain()] == [{"type": "audio.ready"}] * 2
    assert events.get(timeout=TIMEOUT) == {"type": "record.limit", "utterance_id": "recovered"}
    assert events.empty()


def test_record_cancel_stops_capture_without_late_levels(
    monkeypatch: pytest.MonkeyPatch,
    wait_capture: Callable[[], None],
    probes: list[CaptureProbe],
    worker_factory: Callable[..., tuple[WorkerState, FakeEngine]],
) -> None:
    """Отмена работающей записи закрывает источник и отбрасывает последний прочитанный кадр."""
    worker, _ = worker_factory()
    source = ControlledSource()
    second_read = threading.Event()
    late_read_finished = threading.Event()
    original_read = source.read_chunk
    read_calls = 0

    def read_chunk() -> bytes | None:
        """Отмечает вход во второе чтение и возврат опоздавшего кадра."""
        nonlocal read_calls
        read_calls += 1
        if read_calls == 2:
            second_read.set()
        chunk = original_read()
        if read_calls == 2:
            late_read_finished.set()
        return chunk

    monkeypatch.setattr(source, "read_chunk", read_chunk)
    probe = CaptureProbe(source, sink=worker.on_samples, on_error=worker.on_error, step=0.1)
    probes.append(probe)
    worker.set_capture(probe.capture)
    chunk = struct.pack("<h", 8192) * (CHUNK_BYTES // SAMPLE_BYTES)
    source.chunks.put(chunk)
    assert worker.handle({"type": "record.start", "utterance_id": "cancelled"}) == []
    assert probe.events.get(timeout=TIMEOUT)[1] == {"type": "audio.ready"}
    assert probe.events.get(timeout=TIMEOUT)[1]["type"] == "level"
    assert second_read.wait(TIMEOUT)
    assert probe.capture.active
    capture_thread = probe.capture._thread
    assert capture_thread is not None
    original_join = capture_thread.join

    def join(timeout: float | None = None) -> None:
        """Возвращает из read_chunk ещё один кадр строго после запроса остановки."""
        assert not probe.capture.active
        source.chunks.put(chunk)
        original_join(timeout)

    monkeypatch.setattr(capture_thread, "join", join)
    assert worker.handle({"type": "record.cancel", "utterance_id": "cancelled"}) == [
        {"type": "cancelled", "utterance_id": "cancelled"}
    ]
    monkeypatch.setattr(capture_thread, "join", original_join)
    wait_capture()
    assert not capture_thread.is_alive()
    assert late_read_finished.is_set()
    assert not probe.capture.active
    assert not source.is_open
    assert source.close_calls == 1
    assert worker.state is State.idle
    assert worker.buffers == {}
    assert len(probe.samples) == 1
    assert probe.drain() == []
    assert probe.errors.empty()


def test_wav_can_be_reopened(wav_factory: WavFactory) -> None:
    """После close файл снова читается с начала, включая сброс ended."""
    source = WavFileSource(wav_factory(8192, 1))
    try:
        for _ in range(2):
            source.open(None)
            assert source.is_open
            assert not source.ended
            assert source.read_chunk() == struct.pack("<h", 8192) * (CHUNK_BYTES // SAMPLE_BYTES)
            assert source.read_chunk() is None
            assert source.ended
            source.close()
            assert not source.is_open
    finally:
        source.close()


@pytest.mark.parametrize("label", [None, "", "   ", "Встроенный микрофон"])
def test_audio_close_and_lazy_reopen_emit_ready_once(
    label: str | None,
    wait_capture: Callable[[], None],
    probes: list[CaptureProbe],
    worker_factory: Callable[..., tuple[WorkerState, FakeEngine]],
) -> None:
    """И4: audio.close останавливает захват, следующий старт открывает его заново."""
    source = ControlledSource(label)
    worker, _ = worker_factory()
    probe = CaptureProbe(source, sink=worker.on_samples)
    probes.append(probe)
    source.keep_reading = lambda: probe.capture.active
    worker.set_capture(probe.capture)
    assert source.open_calls == source.close_calls == 0
    expected: Message = {"type": "audio.ready"}
    if label is not None and label.strip():
        expected["device"] = label
    for index in range(1, 3):
        source.reading.clear()
        source.chunks.put(struct.pack("<h", 8192) * (CHUNK_BYTES // SAMPLE_BYTES))
        assert (
            worker.handle(
                {"type": "record.start", "utterance_id": str(index), "device": "technical.input"}
            )
            == []
        )
        assert probe.events.get(timeout=TIMEOUT)[1] == expected
        assert probe.events.get(timeout=TIMEOUT)[1]["type"] == "level"
        assert source.reading.wait(TIMEOUT)
        assert probe.capture.active
        assert source.open_calls == index
        assert source.device_name == "technical.input"
        assert worker.handle({"type": "audio.close"}) == [{"type": "audio.closed"}]
        wait_capture()
        assert not probe.capture.active
        assert not source.is_open
        assert source.close_calls == index
        assert worker.state is State.idle
        assert all(event["type"] != "audio.ready" for _, event in probe.drain())
        assert probe.errors.empty()


@pytest.mark.parametrize(
    "kind", ["audio.close", "record.stop", "record.cancel", "recognize", "model.unload", "close"]
)
def test_stop_joins_capture_outside_worker_lock(
    kind: str,
    monkeypatch: pytest.MonkeyPatch,
    wait_capture: Callable[[], None],
    probes: list[CaptureProbe],
    worker_factory: Callable[..., tuple[WorkerState, FakeEngine]],
) -> None:
    """Остановка не держит RLock, нужный завершающемуся callback-у захвата."""
    worker, _ = worker_factory()
    source = ControlledSource()
    callback_entered = threading.Event()
    allow_callback = threading.Event()
    replies: Queue[list[Message] | None] = Queue()
    lock_held_at_join: list[bool] = []

    def on_samples(uid: str, samples: array[float]) -> bool:
        """Задерживает callback перед захватом блокировки до вызова join."""
        callback_entered.set()
        assert allow_callback.wait(TIMEOUT)
        return worker.on_samples(uid, samples)

    probe = CaptureProbe(source, sink=on_samples, on_error=worker.on_error)
    probes.append(probe)
    source.keep_reading = lambda: probe.capture.active
    worker.set_capture(probe.capture)
    source.chunks.put(struct.pack("<h", 8192) * (CHUNK_BYTES // SAMPLE_BYTES))
    assert worker.handle({"type": "record.start", "utterance_id": "locked"}) == []
    command_thread: threading.Thread | None = None
    try:
        assert callback_entered.wait(TIMEOUT)
        capture_thread = probe.capture._thread
        assert capture_thread is not None
        original_join = capture_thread.join

        def join(timeout: float | None = None) -> None:
            """Наблюдает владение RLock именно потоком, вызывающим настоящий join."""
            # CPython предоставляет эту проверку, но в стабах threading её нет.
            lock_held_at_join.append(cast(Any, worker._lock)._is_owned())
            allow_callback.set()
            original_join(timeout)

        def stop() -> None:
            """Вызывает команду отдельно, чтобы ограничить ожидание handle тестом."""
            if kind == "close":
                worker.close()
                replies.put(None)
            else:
                replies.put(worker.handle({"type": kind, "utterance_id": "locked"}))

        monkeypatch.setattr(capture_thread, "join", join)
        command_thread = threading.Thread(target=stop)
        command_thread.start()
        expected: list[Message] | None = []
        if kind == "audio.close":
            expected = [{"type": "audio.closed"}]
        elif kind == "record.cancel":
            expected = [{"type": "cancelled", "utterance_id": "locked"}]
        elif kind == "close":
            expected = None
        # Старый join под RLock удерживал команду две секунды.
        assert replies.get(timeout=0.5) == expected
        assert lock_held_at_join == [False]
        assert not capture_thread.is_alive()
        assert not source.is_open
        assert source.close_calls == 1
        assert not probe.capture.active
        assert probe.errors.empty()
    finally:
        allow_callback.set()
        if command_thread is not None:
            command_thread.join(timeout=TIMEOUT)
        worker.close()
        wait_capture()


def test_audio_ready_only_on_actual_open(
    wait_capture: Callable[[], None],
    probes: list[CaptureProbe],
    worker_factory: Callable[..., tuple[WorkerState, FakeEngine]],
) -> None:
    """И4: каждая запись заново открывает микрофон и сообщает audio.ready."""
    source = ControlledSource()
    worker, _ = worker_factory(limit_s=CHUNK_BYTES / (RATE * SAMPLE_BYTES))
    probe = CaptureProbe(source, sink=worker.on_samples, on_error=worker.on_error)
    probes.append(probe)
    worker.set_capture(probe.capture)
    events: list[Message] = []

    # Между записями микрофон теперь закрыт: прежнее переиспользование нарушало приватность.
    for index in range(1, 4):
        if index == 3:
            assert worker.handle({"type": "audio.close"}) == [{"type": "audio.closed"}]
            assert not source.is_open
            assert source.close_calls == 2
            assert probe.drain() == []

        uid = str(index)
        source.chunks.put(struct.pack("<h", 8192) * (CHUNK_BYTES // SAMPLE_BYTES))
        assert worker.handle({"type": "record.start", "utterance_id": uid}) == []
        wait_capture()
        events.extend(event for _, event in probe.drain())
        assert events == [{"type": "audio.ready"}] * index
        assert source.open_calls == index
        assert not source.is_open
        assert source.close_calls == index
        assert source.flush_calls == 0
        assert len(probe.samples) == index
        assert worker.buffers[uid] == array("f", [0.25] * (CHUNK_BYTES // SAMPLE_BYTES))
        assert worker.state is State.idle
        assert not probe.capture.active
        assert probe.errors.empty()


def test_capture_and_recognition_create_no_temporary_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    wav_factory: WavFactory,
    wait_capture: Callable[[], None],
    probes: list[CaptureProbe],
    worker_factory: Callable[..., tuple[WorkerState, FakeEngine]],
) -> None:
    """S5-A4: полный цикл до result не оставляет WAV или временных файлов."""
    temporary = tmp_path / "tmp"
    temporary.mkdir()
    monkeypatch.setenv("TMPDIR", str(temporary))
    monkeypatch.setattr(tempfile, "tempdir", None)
    assert Path(tempfile.gettempdir()) == temporary
    path = wav_factory(8192, 3)

    def files(root: Path) -> list[Path]:
        """Снимок имён всех файлов, включая вложенные и скрытые."""
        return sorted(item.relative_to(root) for item in root.rglob("*") if item.is_file())

    before = files(tmp_path), files(temporary)
    assert before == ([path.relative_to(tmp_path)], [])
    events: Queue[Message] = Queue()
    worker, engine = worker_factory(on_event=events.put)
    probe = CaptureProbe(WavFileSource(path), sink=worker.on_samples, on_error=worker.on_error)
    probes.append(probe)
    worker.set_capture(probe.capture)
    assert worker.handle({"type": "record.start", "utterance_id": "disk"}) == []
    wait_capture()
    assert worker.handle({"type": "record.stop", "utterance_id": "disk"}) == []
    assert worker.handle({"type": "recognize", "utterance_id": "disk"}) == []
    result = events.get(timeout=TIMEOUT)
    assert (result["type"], result["utterance_id"], result["text"]) == (
        "result",
        "disk",
        engine.text,
    )
    assert engine.audios == [[0.25] * (3 * CHUNK_BYTES // SAMPLE_BYTES)]
    assert worker.buffers == {}
    assert probe.errors.empty()
    assert (files(tmp_path), files(temporary)) == before


def test_normalize_s16le_boundaries() -> None:
    """Знаковые little-endian отсчёты сохраняют длину и правильный масштаб."""
    values = normalize(struct.pack("<hhh", -32768, 0, 32767))
    assert len(values) == 3
    assert list(values) == [-1.0, 0.0, 32767 / 32768]
    assert len(normalize(b"")) == 0


def test_dbfs_zero_and_full_scale() -> None:
    """Нулевая амплитуда конечна, максимум PCM16 близок к нулю дБFS."""
    assert dbfs(0) == -120.0
    assert dbfs(-0.5) == -120.0
    assert dbfs(32767) == pytest.approx(0.0, abs=0.01)


def test_dbfs_preserves_fractional_amplitude() -> None:
    """Ненулевой дробный RMS не превращается в цифровую тишину."""
    assert dbfs(0.5) == pytest.approx(-96.3, abs=0.1)


def test_level_preserves_fractional_rms(
    wait_capture: Callable[[], None], probes: list[CaptureProbe]
) -> None:
    """Редкие отсчёты амплитуды 1 дают RMS меньше единицы и конечный уровень."""
    source = ControlledSource()
    source.ended = True
    source.chunks.put(struct.pack("<hhhh", 1, 0, 0, 0))
    source.chunks.put(None)
    probe = CaptureProbe(source)
    probes.append(probe)
    probe.capture.start("quiet", None)
    wait_capture()
    levels = [event for _, event in probe.drain() if event["type"] == "level"]
    assert len(levels) == 1
    assert levels[0]["rms_dbfs"] == pytest.approx(-96.3, abs=0.1)
    assert probe.errors.empty()


def test_capture_bounds_label_in_event_and_log(
    wait_capture: Callable[[], None],
    probes: list[CaptureProbe],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Даже сторонний источник не передаёт длинную подпись в IPC и журнал."""
    label = "М" * (MAX_DEVICE_LABEL + 100)
    source = ControlledSource(label)
    source.ended = True
    source.chunks.put(None)
    probe = CaptureProbe(source)
    probes.append(probe)
    with caplog.at_level(logging.INFO, logger="astra_voice.worker.audio"):
        probe.capture.start("label", "alsa_input.private")
        wait_capture()
    expected = "М" * (MAX_DEVICE_LABEL - 1) + "…"
    assert [event for _, event in probe.drain()] == [{"type": "audio.ready", "device": expected}]
    prefix = "Источник записи готов: "
    assert caplog.messages == [prefix + "М" * (MAX_DEVICE_LABEL - len(prefix) - 1) + "…"]
    assert label not in caplog.text
    assert "alsa_" not in caplog.text
    assert probe.errors.empty()


@pytest.mark.parametrize(
    ("output", "code", "monitor"),
    [
        ("alsa_output.x.monitor\n", 0, True),
        ("system.capture\n", 0, True),
        (OSError("pactl недоступен"), 0, False),
        (subprocess.CalledProcessError(1, "pactl"), 0, False),
        (subprocess.TimeoutExpired("pactl", 5), 0, False),
        (UnicodeDecodeError("utf-8", b"\xff", 0, 1, "неверный ответ"), 0, False),
        ("alsa_input.microphone\n", 1, False),
        ("", 0, False),
        ("  \n", 0, False),
        ("@DEFAULT_SOURCE@\n", 0, False),
        ("@NONE@\n", 0, False),
        ("alsa_input.missing\n", 0, False),
    ],
    ids=[
        "monitor",
        "monitor-flag",
        "os-error",
        "command-error",
        "timeout",
        "decode-error",
        "nonzero",
        "empty",
        "whitespace",
        "default-alias",
        "none-alias",
        "missing",
    ],
)
def test_record_start_rejects_unsafe_default_before_libpulse(
    output: str | Exception,
    code: int,
    monitor: bool,
    pulse_library: Mock,
    monkeypatch: pytest.MonkeyPatch,
    wait_capture: Callable[[], None],
    probes: list[CaptureProbe],
    worker_factory: Callable[..., tuple[WorkerState, FakeEngine]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """T-39/У44: отказ выходит через record.start до загрузки библиотек и pa_simple_new."""
    devices = [
        AudioDevice(1, "alsa_input.microphone", "Микрофон", False),
        AudioDevice(2, "alsa_output.x.monitor", "Колонки", True),
        AudioDevice(3, "system.capture", "Захват системы", True),
    ]
    run = Mock(wraps=default_source_run(output, code))
    source = PulseSimpleSource(devices=lambda: devices, default=partial(default_device, run=run))
    cdll = Mock(wraps=ctypes.CDLL)
    monkeypatch.setattr(ctypes, "CDLL", cdll)
    events: Queue[Message] = Queue()
    worker, _ = worker_factory(on_event=events.put)
    probe = CaptureProbe(source, sink=lambda uid, samples: False, on_error=worker.on_error)
    probes.append(probe)
    worker.set_capture(probe.capture)

    with caplog.at_level(logging.DEBUG, logger="astra_voice.worker.audio"):
        assert worker.handle({"type": "record.start", "utterance_id": "default"}) == []
        wait_capture()

    event = events.get_nowait()
    assert (event["type"], event["code"], event["utterance_id"]) == (
        "error",
        ERROR_NO_DEVICE,
        "default",
    )
    assert event["message"] == (
        "Микрофон не найден: звук по умолчанию — это звук системы. Выберите микрофон в настройках."
        if monitor
        else "Микрофон не найден. Выберите устройство записи в настройках."
    )
    assert encode(event)
    assert "alsa_" not in str(event)
    assert all(record.levelno == logging.DEBUG for record in caplog.records)
    assert events.empty()
    assert probe.errors.get_nowait()[1] == ERROR_NO_DEVICE
    assert probe.errors.empty()
    assert probe.drain() == []
    assert probe.samples == []
    assert worker.state is State.idle
    assert worker.buffers == {}
    assert not source.is_open
    assert source.selected_device is None
    assert run.call_count == OPEN_FIRST_RETRIES + 1
    cdll.assert_not_called()
    assert pulse_library.pa_simple_new.call_count == 0


def test_record_start_resolves_default_and_reports_changes(
    pulse_library: Mock,
    wait_capture: Callable[[], None],
    probes: list[CaptureProbe],
    worker_factory: Callable[..., tuple[WorkerState, FakeEngine]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """T-39/У44: две записи без device открывают найденные имена и сообщают о смене."""
    first = AudioDevice(2, "alsa_input.builtin", "Встроенный микрофон", False)
    second = AudioDevice(3, "alsa_input.usb", "USB-гарнитура", False)
    devices = [AudioDevice(1, "alsa_output.x.monitor", "Колонки", True), first, second]
    run = Mock()
    source = PulseSimpleSource(devices=lambda: devices, default=partial(default_device, run=run))
    events: Queue[Message] = Queue()
    worker, _ = worker_factory(on_event=events.put)
    selected: list[AudioDevice | None] = []
    names: list[str | None] = []

    def samples(uid: str, chunk: array[float]) -> bool:
        """Сохраняет выбор до закрытия источника и ограничивает запись одной порцией."""
        selected.append(source.selected_device)
        names.append(source.device_name)
        worker.on_samples(uid, chunk)
        return False

    probe = CaptureProbe(source, sink=samples, on_error=worker.on_error)
    probes.append(probe)
    worker.set_capture(probe.capture)
    for index, microphone in enumerate((first, second, second)):
        run.side_effect = default_source_run(microphone.name + "\n")
        with caplog.at_level(logging.INFO, logger="astra_voice.worker.audio"):
            assert worker.handle({"type": "record.start", "utterance_id": str(index)}) == []
            wait_capture()
        ready = [event for _, event in probe.drain() if event["type"] == "audio.ready"]
        expected: Message = {"type": "audio.ready", "device": microphone.label}
        if index < 2:
            expected["changed"] = f"Источник звука изменился: {microphone.label}"
        assert ready == [expected]
        assert encode(expected)
        assert "alsa_" not in str(ready)
        assert selected[-1] is microphone
        assert names[-1] == microphone.name
        assert pulse_library.pa_simple_new.call_args.args[3] == microphone.name.encode("utf-8")
        assert pulse_library.pa_simple_new.call_count == index + 1
        assert pulse_library.pa_simple_free.call_count == index + 1
        assert run.call_count == index + 1
        assert probe.errors.empty()
        assert events.empty()
        assert not source.is_open
        assert worker.handle({"type": "record.stop", "utterance_id": str(index)}) == []
    assert "alsa_" not in caplog.text


def test_capture_reports_device_changes_between_recordings(
    pulse_library: Mock,
    monkeypatch: pytest.MonkeyPatch,
    wait_capture: Callable[[], None],
    probes: list[CaptureProbe],
) -> None:
    """У39/T-35: имя, описание и тип сравниваются между открытиями, индекс не важен."""
    devices: list[AudioDevice] = []
    # У44: None теперь означает проверенное устройство, а не безымянное умолчание сервера.
    fallback = AudioDevice(4, "alsa_input.default", "Встроенный микрофон", False)
    source = PulseSimpleSource(
        devices=lambda: devices,
        default=partial(default_device, run=default_source_run(fallback.name)),
        sleep=lambda delay: None,
    )
    probe = CaptureProbe(source, sink=lambda uid, samples: False)
    probes.append(probe)
    ready = Mock(wraps=WorkerState.audio_ready)
    monkeypatch.setattr(WorkerState, "audio_ready", ready)
    choices: list[tuple[AudioDevice | None, str | None]] = [
        (None, fallback.label),
        (AudioDevice(1, "alsa_input.one", "Микрофон", False), "Микрофон"),
        (AudioDevice(2, "alsa_input.one", "Микрофон", False), None),
        (AudioDevice(2, "alsa_input.one", "Гарнитура", False), "Гарнитура"),
        (AudioDevice(3, "alsa_input.two", "Гарнитура", False), "Гарнитура"),
        (
            AudioDevice(3, "alsa_input.two", "Гарнитура", True),
            "Гарнитура (звук системы)",
        ),
        (None, fallback.label),
        (None, None),
    ]
    for index, (device, changed_label) in enumerate(choices):
        selected = device if device is not None else fallback
        devices[:] = [selected]
        probe.capture.start(str(index), device.name if device is not None else None)
        wait_capture()
        expected: Message = {"type": "audio.ready", "device": selected.label}
        if changed_label is not None:
            expected["changed"] = f"Источник звука изменился: {changed_label}"
        assert [event for _, event in probe.drain()] == [expected]
        assert encode(expected)
        assert "alsa_" not in str(expected)
        assert ready.call_count == index + 1
        assert probe.errors.empty()
        assert not source.is_open
        assert source.selected_device is None

        # Неудачное открытие не выдаёт ready и не стирает предыдущий выбор.
        probe.capture.start(f"missing-{index}", "alsa_input.missing")
        wait_capture()
        assert probe.errors.get_nowait()[1] == ERROR_NO_DEVICE
        assert probe.errors.empty()
        assert probe.drain() == []
        assert ready.call_count == index + 1
    assert pulse_library.pa_simple_new.call_count == len(choices)
    assert pulse_library.pa_simple_free.call_count == len(choices)


@pytest.mark.parametrize("monitor", [False, True])
def test_capture_bounds_change_notification(
    pulse_library: Mock,
    monitor: bool,
    wait_capture: Callable[[], None],
    probes: list[CaptureProbe],
) -> None:
    """Готовый текст пилюли ограничивается по символам тем же пределом, что и подпись."""
    device = AudioDevice(1, "alsa_input.private", "Ё" * (MAX_DEVICE_LABEL + 100), monitor)
    source = PulseSimpleSource(devices=lambda: [device])
    probe = CaptureProbe(source, sink=lambda uid, samples: False)
    probes.append(probe)
    probe.capture.start("long", device.name)
    wait_capture()
    events = [event for _, event in probe.drain()]
    assert len(events) == 1
    event = events[0]
    prefix = "Источник звука изменился: "
    assert event == {
        "type": "audio.ready",
        "device": device.label,
        "changed": prefix + "Ё" * (MAX_DEVICE_LABEL - len(prefix) - 1) + "…",
    }
    assert len(event["changed"]) == len(event["device"]) == MAX_DEVICE_LABEL
    assert encode(event)
    assert probe.errors.empty()


@pytest.mark.parametrize("monitor", [False, True])
def test_capture_sanitizes_pactl_description(
    monitor: bool,
    short_sources: str,
    pulse_library: Mock,
    wait_capture: Callable[[], None],
    probes: list[CaptureProbe],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """T-41: чужие 10 КиБ не подделывают строки событий и единственную запись журнала."""
    name = short_sources.splitlines()[int(monitor)].split("\t")[1]
    prefix = " \n\x00\u202eМикрофон\r\tUSB\u200b "
    description = prefix + "X" * (10 * 1024 - len(prefix.encode("utf-8")))
    assert len(description.encode("utf-8")) == 10 * 1024
    run = pactl_run(short_sources, json.dumps([{"name": name, "description": description}]))
    source = PulseSimpleSource(devices=partial(list_devices, run=run))
    probe = CaptureProbe(source, sink=lambda uid, samples: False)
    probes.append(probe)
    with caplog.at_level(logging.INFO, logger="astra_voice.worker.audio"):
        probe.capture.start("untrusted-description", name)
        wait_capture()
    events = [event for _, event in probe.drain()]
    assert len(events) == 1
    event = events[0]
    assert event["type"] == "audio.ready"
    readiness = [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("Источник записи готов: ")
    ]
    assert len(readiness) == 1
    for text in (event["device"], event["changed"], readiness[0]):
        assert isinstance(text, str)
        assert text.splitlines() == [text]
        assert len(text) <= MAX_DEVICE_LABEL
        assert all(unicodedata.category(char) not in {"Cc", "Cf"} for char in text)
        assert "Микрофон USB " in text
    if monitor:
        assert event["device"].endswith(" (звук системы)")
    assert encode(event)
    assert probe.errors.empty()


@pytest.mark.parametrize("changed", [1, None, [], {}])
def test_audio_ready_rejects_non_string_change(changed: object) -> None:
    """Новое необязательное поле IPC принимает только строку."""
    with pytest.raises(FrameError):
        encode({"type": "audio.ready", "changed": changed})


@pytest.mark.parametrize("previous_failure", [False, True])
def test_first_open_retries_missing_device_until_success(
    previous_failure: bool,
    monkeypatch: pytest.MonkeyPatch,
    wait_capture: Callable[[], None],
    probes: list[CaptureProbe],
    worker_factory: Callable[..., tuple[WorkerState, FakeEngine]],
) -> None:
    """Первая неудачная запись не гасит F6.3; успешное открытие гасит его."""
    device = AudioDevice(1, "alsa_input.chosen", "Микрофон", False)
    source = ControlledSource(device.label)
    source.ended = True
    source.chunks.put(None)
    worker, _ = worker_factory()
    probe = CaptureProbe(source, on_error=worker.on_error)
    probes.append(probe)
    worker.set_capture(probe.capture)
    failures_left = OPEN_FIRST_RETRIES + 1 if previous_failure else 1
    requested: list[str | None] = []
    original_open = source.open

    def open_device(name: str | None) -> None:
        """Имитирует исчезновение выбранного имени из списка WirePlumber."""
        nonlocal failures_left
        requested.append(name)
        devices = [] if failures_left else [device]
        failures_left = max(0, failures_left - 1)
        resolve_device(name, devices)
        original_open(name)

    monkeypatch.setattr(source, "open", open_device)
    if previous_failure:
        assert (
            worker.handle({"type": "record.start", "utterance_id": "failed", "device": device.name})
            == []
        )
        wait_capture()
        assert probe.errors.get_nowait()[1] == ERROR_NO_DEVICE
        assert len(requested) == OPEN_FIRST_RETRIES + 1
        assert probe.drain() == []
        failures_left = 1
        requested.clear()
        probe.sleeps.clear()

    assert (
        worker.handle({"type": "record.start", "utterance_id": "first", "device": device.name})
        == []
    )
    wait_capture()
    assert requested == [device.name, device.name]
    assert source.open_calls == 1
    assert probe.sleeps == [OPEN_FIRST_PAUSE_S]
    assert [event for _, event in probe.drain()] == [
        {"type": "audio.ready", "device": device.label}
    ]
    assert probe.errors.empty()
    assert worker.handle({"type": "record.stop", "utterance_id": "first"}) == []

    failures_left = 1
    requested.clear()
    probe.sleeps.clear()
    assert (
        worker.handle({"type": "record.start", "utterance_id": "next", "device": device.name}) == []
    )
    wait_capture()
    # ERROR_NO_DEVICE не повторяется только после первого успешного открытия.
    assert requested == [device.name]
    assert probe.sleeps == []
    assert probe.errors.get_nowait()[1] == ERROR_NO_DEVICE
    assert probe.drain() == []


def test_total_deadline_bounds_nested_pulse_retries(
    monkeypatch: pytest.MonkeyPatch,
    wait_capture: Callable[[], None],
    probes: list[CaptureProbe],
) -> None:
    """Один бюджет ограничивает и внутренние попытки Pulse, и повторы F6.3."""
    device = AudioDevice(1, "alsa_input.chosen", "Микрофон", False)
    source = PulseSimpleSource(devices=lambda: [device])
    probe = CaptureProbe(source)
    probes.append(probe)
    monkeypatch.setattr(source, "_sleep", probe.sleep)
    pulse = Mock(spec=_Pulse)
    timeouts: list[float] = []

    def refuse(name: str | None, *, timeout: float) -> tuple[None, int]:
        """Исчерпывает выделенное ожидание по инъектированным часам."""
        assert name == device.name
        assert probe.now < OPEN_TOTAL_DEADLINE_S
        timeouts.append(timeout)
        probe.now += timeout
        return None, 0

    pulse.open.side_effect = refuse
    monkeypatch.setattr("astra_voice.worker.audio._Pulse", lambda: pulse)
    probe.capture.start("deadline", device.name)
    wait_capture()
    assert probe.now == pytest.approx(OPEN_TOTAL_DEADLINE_S)
    assert timeouts == pytest.approx([2.0, 2.0, 2.0, 0.4])
    assert probe.sleeps == [0.3, 0.3, OPEN_FIRST_PAUSE_S]
    assert probe.errors.get_nowait() == (
        "deadline",
        ERROR_FAILED,
        "Не удалось вовремя подготовить запись звука.",
    )
    assert probe.errors.empty()
    assert probe.drain() == []
    assert not probe.capture.active
    assert not source.is_open


def test_total_deadline_includes_device_listing(
    monkeypatch: pytest.MonkeyPatch,
    short_sources: str,
    wait_capture: Callable[[], None],
    probes: list[CaptureProbe],
) -> None:
    """Две команды pactl делят общий бюджет; после него Pulse не вызывается."""
    source = PulseSimpleSource()
    probe = CaptureProbe(source)
    probes.append(probe)
    timeouts: list[float] = []

    def run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        """Краткий список занимает пять секунд, JSON исчерпывает остаток."""
        timeout = kwargs["timeout"]
        assert isinstance(timeout, (int, float))
        timeouts.append(timeout)
        probe.now += timeout
        if len(timeouts) == 1:
            return subprocess.CompletedProcess(args, 0, short_sources, "")
        raise subprocess.TimeoutExpired(args, timeout)

    def devices(*, deadline: _OpenDeadline | None = None) -> list[AudioDevice]:
        """Подставляет только запуск процесса, сохраняя настоящий разбор списка."""
        return list_devices(run=run, deadline=deadline)

    monkeypatch.setattr("astra_voice.worker.audio.list_devices", devices)
    probe.capture.start("listing", "alsa_input.pci.microphone")
    wait_capture()
    assert timeouts == [5.0, OPEN_TOTAL_DEADLINE_S - 5.0]
    assert probe.now == OPEN_TOTAL_DEADLINE_S
    assert probe.sleeps == []
    assert probe.errors.get_nowait()[1] == ERROR_FAILED
    assert probe.drain() == []
    assert not source.is_open


def test_total_deadline_includes_default_lookup(
    pulse_library: Mock,
    monkeypatch: pytest.MonkeyPatch,
    short_sources: str,
    wait_capture: Callable[[], None],
    probes: list[CaptureProbe],
) -> None:
    """У44: поиск умолчания получает остаток после обеих команд списка устройств."""
    timeouts: list[float] = []

    def run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        """Расходует шесть секунд на список и остаток бюджета на поиск умолчания."""
        timeout = kwargs["timeout"]
        assert isinstance(timeout, (int, float))
        timeouts.append(timeout)
        if args == ["pactl", "list", "short", "sources"]:
            probe.now += 4.0
            return subprocess.CompletedProcess(args, 0, short_sources, "")
        if args == ["pactl", "-f", "json", "list", "sources"]:
            probe.now += 2.0
            return subprocess.CompletedProcess(args, 0, "[]", "")
        assert args == ["pactl", "get-default-source"]
        probe.now += timeout
        raise subprocess.TimeoutExpired(args, timeout)

    monkeypatch.setattr("astra_voice.worker.audio.list_devices", partial(list_devices, run=run))
    cdll = Mock(wraps=ctypes.CDLL)
    monkeypatch.setattr(ctypes, "CDLL", cdll)
    source = PulseSimpleSource(default=partial(default_device, run=run))
    probe = CaptureProbe(source)
    probes.append(probe)
    probe.capture.start("default-deadline", None)
    wait_capture()
    assert timeouts == [5.0, 4.0, 2.0]
    assert probe.now == OPEN_TOTAL_DEADLINE_S
    assert probe.sleeps == []
    assert probe.errors.get_nowait()[1] == ERROR_FAILED
    assert probe.errors.empty()
    assert probe.drain() == []
    assert not source.is_open
    cdll.assert_not_called()
    assert pulse_library.pa_simple_new.call_count == 0


def test_total_deadline_shortens_retry_pause(
    monkeypatch: pytest.MonkeyPatch,
    wait_capture: Callable[[], None],
    probes: list[CaptureProbe],
) -> None:
    """Остаток бюджета ограничивает паузу F6.3 и запрещает следующую попытку."""
    source = ControlledSource()
    probe = CaptureProbe(source)
    probes.append(probe)
    calls: list[str | None] = []

    def refuse(name: str | None) -> None:
        calls.append(name)
        probe.now += 3.4
        raise AudioError(ERROR_FAILED, "Не удалось включить запись звука.")

    monkeypatch.setattr(source, "open", refuse)
    probe.capture.start("pause", "alsa_input.chosen")
    wait_capture()
    assert calls == ["alsa_input.chosen"] * 2
    assert probe.now == OPEN_TOTAL_DEADLINE_S
    assert probe.sleeps == pytest.approx([OPEN_FIRST_PAUSE_S, 0.2])
    assert probe.errors.get_nowait()[1] == ERROR_FAILED
    assert probe.errors.empty()
    assert probe.drain() == []


@pytest.mark.parametrize("monitor", [False, True])
def test_pulse_uses_human_label(
    monitor: bool,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Подпись Pulse ограничена и содержит предупреждение о записи системы."""
    name = "alsa_output.private.monitor" if monitor else "alsa_input.private"
    device = AudioDevice(1, name, "Я" * (MAX_DEVICE_LABEL + 100), monitor)
    pulse = Mock(spec=_Pulse)
    pulse.lib = Mock()
    pulse.open.return_value = (ctypes.c_void_p(123), 0)
    pulse.lib.pa_simple_get_latency.return_value = 1000
    monkeypatch.setattr("astra_voice.worker.audio._Pulse", lambda: pulse)
    source = PulseSimpleSource(devices=lambda: [device])
    try:
        with caplog.at_level(logging.INFO, logger="astra_voice.worker.audio"):
            source.open(name)
        assert source.device_label == device.label
        assert len(source.device_label) == MAX_DEVICE_LABEL
        assert name not in caplog.text
        assert device.description not in caplog.text
        if monitor:
            assert caplog.messages == [f"Выбран источник записи: {device.label}"]
    finally:
        source.close()


@pytest.mark.parametrize("blocked_call", ["read", "flush", "second_new", "get_latency"])
@pytest.mark.parametrize("cancel", ["request_stop", "audio.close", "engine-failed"])
def test_capture_watchdog_never_frees_during_libpulse_call(
    blocked_call: str,
    cancel: str,
    pulse_library: Mock,
    monkeypatch: pytest.MonkeyPatch,
    wait_capture: Callable[[], None],
    worker_factory: Callable[..., tuple[WorkerState, FakeEngine]],
) -> None:
    """T-40: зависший C-вызов остаётся у владельца до возврата, даже после отмены."""
    watchdog_s = 0.03
    monkeypatch.setattr("astra_voice.worker.audio.STOP_WATCHDOG_S", watchdog_s)
    monkeypatch.setattr("astra_voice.worker.state._available_vad", lambda: None)
    entered = threading.Event()
    release = threading.Event()
    returned = threading.Event()
    engine_gate = threading.Event()
    events: Queue[Message] = Queue()
    worker, engine = worker_factory(on_event=events.put)
    if cancel == "engine-failed":
        engine.gate = engine_gate
        engine.raises = True
        worker.handle({"type": "record.start", "utterance_id": "job"})
        worker.feed_audio("job", [0.25] * 320)
        worker.handle({"type": "record.stop", "utterance_id": "job"})
        worker.handle({"type": "recognize", "utterance_id": "job"})
        assert engine.started.wait(TIMEOUT)

    device = AudioDevice(1, "alsa_input.chosen", "Микрофон", False)
    source = PulseSimpleSource(devices=lambda: [device], sleep=lambda _: None)
    close_threads: list[threading.Thread] = []
    free_threads: list[threading.Thread] = []
    new_threads: list[threading.Thread] = []
    call_threads: list[threading.Thread] = []
    original_close = source.close

    def close() -> None:
        close_threads.append(threading.current_thread())
        original_close()

    def free(handle: ctypes.c_void_p) -> None:
        assert returned.is_set(), "pa_simple_free вызван внутри незавершённого C-вызова"
        assert handle.value == 123
        free_threads.append(threading.current_thread())

    def block(*args: object) -> int:
        call_threads.append(threading.current_thread())
        entered.set()
        assert release.wait(TIMEOUT)
        returned.set()
        return 123 if blocked_call == "second_new" else 0

    def new(*args: object) -> int | None:
        new_threads.append(threading.current_thread())
        if blocked_call == "second_new":
            return None if len(new_threads) == 1 else block(*args)
        return 123

    def samples(uid: str, pcm: array[float]) -> bool:
        if blocked_call == "get_latency":
            # Диагностический запрос проверяем отдельно: в open его быть не должно.
            source.latency_us()
        return worker.on_samples(uid, pcm)

    capture = AudioCapture(
        source=source,
        on_samples=samples,
        on_event=lambda _: None,
        on_error=worker.on_error,
    )
    monkeypatch.setattr(source, "close", close)
    pulse_library.pa_simple_new.side_effect = new
    pulse_library.pa_simple_free.side_effect = free
    if blocked_call != "second_new":
        getattr(pulse_library, f"pa_simple_{blocked_call}").side_effect = block
    if blocked_call == "flush":
        original_open = capture._open

        def reuse(device: str | None, running: threading.Event) -> bool | None:
            # Устраиваем ветку повторного использования, сохраняя открытие
            # настоящим PulseSimpleSource в том же потоке захвата.
            opened = original_open(device, running)
            return None if opened is None else False

        monkeypatch.setattr(capture, "_open", reuse)
    worker.set_capture(capture)
    try:
        worker.handle({"type": "record.start", "utterance_id": "blocked", "device": device.name})
        assert entered.wait(TIMEOUT)
        owner = capture._thread
        assert owner is not None
        closes_before_stop = list(close_threads)
        stopped_at = time.monotonic()
        if cancel == "audio.close":
            with pytest.raises(CaptureStopTimeout):
                worker.handle({"type": "audio.close"})
        else:
            if cancel == "engine-failed":
                engine_gate.set()
                assert events.get(timeout=TIMEOUT)["code"] == "engine-failed"
            else:
                capture.request_stop()
            worker.check_capture_watchdog()
            with pytest.raises(CaptureStopTimeout):
                capture.stop()
        assert time.monotonic() - stopped_at >= watchdog_s
        with pytest.raises(CaptureStopTimeout):
            worker.check_capture_watchdog()
        assert owner.is_alive()
        assert not capture.active
        assert not returned.is_set()
        assert close_threads == closes_before_stop
        pulse_library.pa_simple_free.assert_not_called()
        assert call_threads == [owner]
        assert new_threads == [owner] * (2 if blocked_call == "second_new" else 1)
        if blocked_call != "get_latency":
            pulse_library.pa_simple_get_latency.assert_not_called()
    finally:
        release.set()
        engine_gate.set()
        # Сначала даём владельцу закончить C-вызов, затем закрываем executor;
        # wait_capture также наблюдает его поток, который живёт до shutdown.
        if capture._thread is not None:
            capture._thread.join(TIMEOUT)
        worker.close()
        wait_capture()
    assert not source.is_open
    assert free_threads == [owner]
    assert close_threads == [owner, owner]  # open сбрасывает пустой источник, finally закрывает.
    worker.check_capture_watchdog()


@pytest.mark.parametrize("retry_loop", ["source", "capture"])
def test_audio_closed_prevents_next_open_attempt(
    retry_loop: str,
    pulse_library: Mock,
    monkeypatch: pytest.MonkeyPatch,
    wait_capture: Callable[[], None],
) -> None:
    """T-40: отмена в паузе запрещает следующий pa_simple_new в обоих циклах."""
    if retry_loop == "capture":
        # Один внутренний отказ передаёт управление внешнему циклу AudioCapture._open.
        monkeypatch.setattr("astra_voice.worker.audio.OPEN_RETRIES", 1)
    pulse_library.pa_simple_new.return_value = None
    paused = threading.Event()
    release = threading.Event()

    def pause(delay: float) -> None:
        paused.set()
        assert release.wait(TIMEOUT)

    device = AudioDevice(1, "alsa_input.chosen", "Микрофон", False)
    source = PulseSimpleSource(devices=lambda: [device], sleep=pause)
    worker = WorkerState()
    capture = AudioCapture(
        source=source,
        on_samples=worker.on_samples,
        on_event=lambda _: None,
        on_error=worker.on_error,
        sleep=pause,
    )
    worker.set_capture(capture)
    worker.handle({"type": "record.start", "utterance_id": "retry", "device": device.name})
    try:
        assert paused.wait(TIMEOUT)
        owner = capture._thread
        assert owner is not None
        original_join = owner.join

        def join(timeout: float | None = None) -> None:
            assert not capture.active
            release.set()
            original_join(timeout)

        monkeypatch.setattr(owner, "join", join)
        assert worker.handle({"type": "audio.close"}) == [{"type": "audio.closed"}]
        calls_at_closed = pulse_library.pa_simple_new.call_count
        assert calls_at_closed == 1
        wait_capture()
        assert pulse_library.pa_simple_new.call_count == calls_at_closed
        assert not owner.is_alive()
        assert not source.is_open
        pulse_library.pa_simple_free.assert_not_called()
        pulse_library.pa_simple_get_latency.assert_not_called()
    finally:
        release.set()
        wait_capture()
        worker.close()


def test_capture_stop_before_start_closes_unowned_source() -> None:
    """Без запуска потока источник допустимо закрыть непосредственно владельцу."""
    source = Mock(spec=AudioSource)
    capture = AudioCapture(source=source, on_samples=Mock(), on_event=Mock(), on_error=Mock())
    capture.stop()
    source.close.assert_called_once_with()


def test_watchdog_keeps_deadline_for_previous_capture(
    pulse_library: Mock,
    monkeypatch: pytest.MonkeyPatch,
    wait_capture: Callable[[], None],
) -> None:
    """Новая запись и повторная отмена не скрывают поток, зависший в finally/free."""
    from astra_voice.worker.audio import STOP_WATCHDOG_S

    now = 0.0
    monkeypatch.setattr("astra_voice.worker.audio.time", Mock(monotonic=lambda: now))
    entered = threading.Event()
    release = threading.Event()
    free_threads: list[threading.Thread] = []

    def free(handle: ctypes.c_void_p) -> None:
        free_threads.append(threading.current_thread())
        entered.set()
        assert release.wait(TIMEOUT)

    pulse_library.pa_simple_free.side_effect = free
    device = AudioDevice(1, "alsa_input.chosen", "Микрофон", False)
    source = PulseSimpleSource(devices=lambda: [device])
    capture = AudioCapture(
        source=source,
        on_samples=lambda uid, samples: False,
        on_event=lambda _: None,
        on_error=Mock(),
    )
    capture.start("first", device.name)
    try:
        assert entered.wait(TIMEOUT)
        first = capture._thread
        assert first is not None
        now = STOP_WATCHDOG_S - 1
        capture.start("next", device.name)
        assert capture.active
        capture.check_stop_watchdog()
        capture.request_stop()
        now = STOP_WATCHDOG_S + 0.01
        with pytest.raises(CaptureStopTimeout):
            capture.check_stop_watchdog()
        with pytest.raises(CaptureStopTimeout):
            capture.stop()
        assert first.is_alive()
        pulse_library.pa_simple_new.assert_called_once()
        assert free_threads == [first]
    finally:
        release.set()
        wait_capture()
        capture.stop()
    pulse_library.pa_simple_new.assert_called_once()
    pulse_library.pa_simple_free.assert_called_once()
    capture.check_stop_watchdog()
