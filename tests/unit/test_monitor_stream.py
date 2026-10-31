"""Exact target policy; never attach a replacement/default output implicitly."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from astra_voice.worker.monitor_stream import MonitorStreamSource, Target

pytestmark = pytest.mark.unit


class Pulse:
    def __init__(self) -> None:
        self.target: Target | None = Target(7, 11, 123, 99, "sink-a.monitor")
        self.calls: list[tuple[Any, ...]] = []
        self.events: list[tuple[str, int]] = []
        self.default = "sink-a.monitor"
        self.monitor_supported = True

    def resolve(self, index: int) -> Target | None:
        return self.target

    def default_monitor(self) -> str:
        return self.default

    def open_record(self, monitor: str, index: int | None, token: str) -> None:
        self.calls.append((monitor, index, token))

    def close_record(self) -> None:
        self.calls.append(("closed",))

    def read_chunk(self) -> bytes:
        return b"\0\0" * 1600

    def poll_events(self) -> list[tuple[str, int]]:
        events, self.events = self.events, []
        return events

    def sample_clock(self) -> tuple[str, int, int]:
        return "pulse", 100, 100100


def make(pulse: Pulse, kind: str = "app_stream") -> MonitorStreamSource:
    return MonitorStreamSource(pulse, {"kind": kind, "sink_input": 7, "object_serial": 11}, "run1")


def test_exact_target_before_connect_and_no_default_after_removed() -> None:
    pulse = Pulse()
    source = make(pulse)
    source.open()
    assert pulse.calls == [("sink-a.monitor", 7, "run1")]
    pulse.target = None
    pulse.events.append(("removed", 7))
    assert source.read_chunk() is None and source.ended
    pulse.target = Target(7, 99, 555, 99, "sink-a.monitor")
    for _ in range(100):
        assert source.read_chunk() is None
    assert len([c for c in pulse.calls if len(c) == 3]) == 1


@pytest.mark.parametrize("field,value", [("serial", 12), ("pid", 555), ("cookie", 100)])
def test_identity_change_ends_target(field: str, value: int) -> None:
    from dataclasses import replace

    pulse = Pulse()
    source = make(pulse)
    source.open()
    assert pulse.target is not None
    changes: dict[str, Any] = {field: value}
    pulse.target = replace(pulse.target, **changes)
    pulse.events.append(("changed", 7))
    assert source.read_chunk() is None and source.ended


def test_sink_move_reopens_exact_target_and_marks_clock_boundary() -> None:
    pulse = Pulse()
    source = make(pulse)
    source.open()
    pulse.target = Target(7, 11, 123, 99, "sink-b.monitor")
    pulse.events.append(("changed", 7))
    assert source.read_chunk() == b""
    assert source.change == "sink-moved"
    assert pulse.calls[-1] == ("sink-b.monitor", 7, "run1")


def test_server_event_verifies_target_and_explicit_a_follows_default() -> None:
    pulse = Pulse()
    source = make(pulse, "sink_monitor")
    source.open()
    pulse.default = "sink-b.monitor"
    pulse.events.append(("server", 0))
    assert source.read_chunk() == b""
    assert pulse.calls[-1] == ("sink-b.monitor", None, "run1")


def test_unsupported_b_never_silently_becomes_a() -> None:
    pulse = Pulse()
    pulse.monitor_supported = False
    with pytest.raises(ValueError, match="monitor-stream-unsupported"):
        make(pulse).open()
    assert not pulse.calls


def test_capture_has_no_routing_mutations() -> None:
    root = Path(__file__).resolve().parents[2] / "src/astra_voice/worker"
    forbidden = (
        "move-sink-input",
        "set-default-sink",
        "set-default-source",
        "load-module",
        "null-sink",
        "pa_context_move_sink_input",
        "pa_context_set_default",
        "pa_context_load_module",
    )
    for name in ("meeting_capture.py", "monitor_stream.py"):
        code = (root / name).read_text()
        for token in forbidden:
            assert token not in code


def test_monitor_serializes_native_read_and_close() -> None:
    import threading

    pulse = Pulse()
    source = make(pulse)
    source.open()
    entered, release, closed = threading.Event(), threading.Event(), threading.Event()

    def read() -> bytes:
        entered.set()
        release.wait(2)
        return b""

    pulse.read_chunk = read  # type: ignore[method-assign]
    reader = threading.Thread(target=source.read_chunk)
    reader.start()
    assert entered.wait(1)

    def close() -> None:
        source.close()
        closed.set()

    attempted = threading.Event()
    original_close = close

    def attempt() -> None:
        attempted.set()
        original_close()

    closer = threading.Thread(target=attempt)
    closer.start()
    assert attempted.wait(1)
    try:
        assert not closed.wait(0.05), "native-close-overlapped-read"
    finally:
        release.set()
        reader.join(2)
        closer.join(2)
    assert closed.is_set()


def test_native_monitor_binding_precedes_connect() -> None:
    from unittest.mock import Mock

    from astra_voice.worker.monitor_stream import NativePulse

    native = NativePulse.__new__(NativePulse)
    native.stream = None
    native.buffer = bytearray()
    native.context = 1
    native.monitor_supported = True
    native.pulse = Mock()
    native.pulse.stream_new.return_value = 22
    calls: list[str] = []

    def bind(*args: Any) -> int:
        calls.append("bind")
        return 0

    def connect(*args: Any) -> int:
        calls.append("connect")
        return 0

    native.pulse.lib.pa_stream_set_monitor_stream.side_effect = bind
    native.pulse.stream_connect_record.side_effect = connect
    native._wait = lambda ready: None  # type: ignore[method-assign]
    native.open_record("exact.monitor", 7, "run")
    assert calls == ["bind", "connect"]
    native.pulse.lib.pa_stream_set_monitor_stream.assert_called_once_with(22, 7)


def test_native_meeting_frag_proplist_and_hole_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    import ctypes
    from unittest.mock import Mock

    from astra_voice.worker import pulse_stream
    from astra_voice.worker.audio import _PaBufferAttr
    from astra_voice.worker.monitor_stream import _meeting_pulse_type

    lib = Mock()
    lib.pa_proplist_new.return_value = 10
    lib.pa_proplist_sets.return_value = 0
    lib.pa_stream_new_with_proplist.return_value = 22
    monkeypatch.setattr(
        pulse_stream._PulseAsync, "__init__", lambda self: setattr(self, "lib", lib)
    )
    native = _meeting_pulse_type()()
    assert native.stream_new(1, b"ignored", None, None) == 22
    assert [call.args[:3] for call in lib.pa_proplist_sets.call_args_list] == [
        (10, b"application.name", b"Astra Voice"),
        (10, b"media.name", "Запись встречи".encode()),
        (10, b"astra-voice.run", native.token.encode()),
    ]
    assert lib.pa_context_get_server_info.argtypes[1] is ctypes.c_void_p
    observed: list[int] = []

    def connect(stream: int, name: bytes, attr: Any, flags: int) -> int:
        observed.append(ctypes.cast(attr, ctypes.POINTER(_PaBufferAttr)).contents.fragsize)
        return 0

    lib.pa_stream_connect_record.side_effect = connect
    assert native.stream_connect_record(22, b"source", None, 0) == 0
    assert observed == [3200]
    monkeypatch.setattr(pulse_stream._PulseAsync, "peek", lambda self, stream: 3200)
    with pytest.raises(ValueError, match="audio-gap"):
        native.peek(22)


def test_native_prepare_and_detector_context_never_create_pcm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import Mock

    from astra_voice.worker import monitor_stream as module

    pulse = Mock()
    pulse.monitor_supported = True
    pulse.mainloop_new.return_value = 2
    pulse.mainloop_get_api.return_value = 3
    pulse.context_new.return_value = 1
    pulse.context_connect.return_value = 0
    pulse.context_get_state.return_value = 4
    pulse.context_subscribe.return_value = 4
    pulse.lib.pa_context_get_index.return_value = 42
    monkeypatch.setattr(module, "_meeting_pulse_type", lambda: lambda: pulse)
    monkeypatch.setattr(module.NativePulse, "_server", lambda self: None)
    backend = module.CapturePulseBackend()
    try:
        backend.prepare({"kind": "none"})
        assert backend.detector_start() == 42
        pulse.stream_new.assert_not_called()
        pulse.stream_connect_record.assert_not_called()
        pulse.lib.pa_stream_new_with_proplist.assert_not_called()
    finally:
        backend.close()
