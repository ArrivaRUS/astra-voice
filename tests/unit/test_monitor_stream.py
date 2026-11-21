"""Exact target policy; never attach a replacement/default output implicitly."""

from __future__ import annotations

import ctypes
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

from astra_voice.meetings.identity import (
    IdentityLimit,
    ProcessGone,
    ProcessKey,
    ProcReader,
    ProcUnavailable,
)
from astra_voice.worker import monitor_stream
from astra_voice.worker.meeting_identity_check import PinnedStream, PinnedTarget
from astra_voice.worker.monitor_stream import MonitorStreamSource, Target

pytestmark = pytest.mark.unit

RESOLVED = Target(7, 11, 123, 99, "sink-a.monitor", 21, 431)
PINNED = PinnedTarget(99, (PinnedStream(7, 11, 21, 73, ProcessKey(123, 431)),), 10.0)
FAR = {"kind": "app_stream", "call_id": "c"}
MISMATCHES = [
    ("index", 8),
    ("serial", 12),
    ("pid", 555),
    ("cookie", 100),
    ("client", 22),
    ("starttime", 432),
]


def replace_identity_field(target: Target, field: str, value: int) -> Target:
    change: dict[str, Any] = {field: value}
    return replace(target, **change)


class Pulse:
    def __init__(self) -> None:
        self.target: Target | None = RESOLVED
        self.after_open: list[Target | None] = []
        self.open_error: Exception | None = None
        self.calls: list[tuple[Any, ...]] = []
        self.events: list[tuple[str, int]] = []
        self.default = "sink-a.monitor"
        self.monitor_supported = True
        self.server_kind = "pulseaudio"
        self.server_local = True

    def resolve(
        self, index: int, *, expected_pid: int | None = None, confirm_loss: bool = False
    ) -> Target | None:
        self.calls.append(("resolve", index))
        return self.target

    def default_monitor(self) -> str:
        return self.default

    def open_record(self, monitor: str, index: int | None, token: str) -> None:
        self.calls.append((monitor, index, token))
        if self.after_open:
            self.target = self.after_open.pop(0)
        if self.open_error is not None:
            raise self.open_error

    def close_record(self) -> None:
        self.calls.append(("closed",))

    def read_chunk(self) -> bytes:
        self.calls.append(("read",))
        return b"\0\0" * 1600

    def poll_events(self) -> list[tuple[str, int]]:
        events, self.events = self.events, []
        return events

    def sample_clock(self) -> tuple[str, int, int]:
        return "pulse", 100, 100100


def make(pulse: Pulse, kind: str = "app_stream") -> MonitorStreamSource:
    return MonitorStreamSource(pulse, "run1", target=PINNED if kind == "app_stream" else None)


def test_exact_target_before_connect_and_no_default_after_removed() -> None:
    pulse = Pulse()
    source = make(pulse)
    source.open()
    assert source.read_chunk() == b"\0\0" * 1600
    assert pulse.calls == [("resolve", 7), ("sink-a.monitor", 7, "run1"), ("resolve", 7), ("read",)]
    pulse.target = None
    pulse.events.append(("removed", 7))
    assert source.read_chunk() is None and source.ended
    pulse.target = replace(RESOLVED, serial=99, pid=555)
    for _ in range(100):
        assert source.read_chunk() is None
    assert len([c for c in pulse.calls if len(c) == 3]) == 1


@pytest.mark.parametrize("fresh", ["same", "missing", *(field for field, _ in MISMATCHES)])
def test_prepare_uses_pinned_identity_in_native_backend(
    monkeypatch: pytest.MonkeyPatch, fresh: str
) -> None:
    pulse = Pulse()
    backend = monitor_stream.CapturePulseBackend()
    with monkeypatch.context() as patch:
        patch.setattr(monitor_stream, "NativePulse", lambda: pulse)
        if fresh == "same":
            assert backend.prepare(FAR, target=PINNED) == {
                "server_local": True,
                "server_kind": "pulseaudio",
                "far_available": True,
            }
        else:
            pulse.target = (
                None
                if fresh == "missing"
                else replace_identity_field(RESOLVED, fresh, dict(MISMATCHES)[fresh])
            )
            reason = "target-gone" if fresh == "missing" else "target-mismatch"
            with pytest.raises(ValueError, match=reason):
                backend.prepare(FAR, target=PINNED)
    assert pulse.calls == [("resolve", 7)]


def test_backend_source_retains_start_target(monkeypatch: pytest.MonkeyPatch) -> None:
    created: list[Pulse] = []

    class NativeDouble(Pulse):
        def __init__(self) -> None:
            super().__init__()
            created.append(self)

        def context_ready(self) -> bool:
            return True

    class UnexpectedNative:
        def __init__(self) -> None:
            pytest.fail("the backend must capture the factory when creating the source")

    with monkeypatch.context() as patch:
        patch.setattr(monitor_stream, "NativePulse", NativeDouble)
        source = monitor_stream.CapturePulseBackend().source(FAR, target=PINNED)
        assert isinstance(source, monitor_stream.MixedFarSource)
        assert source.target is PINNED and created == []
        patch.setattr(monitor_stream, "NativePulse", UnexpectedNative)
        source.open()
        try:
            assert len(created) == 1
            assert created[0].calls == [
                ("resolve", 7),
                (RESOLVED.monitor, 7, source.token),
                ("resolve", 7),
            ]
        finally:
            source.shutdown()


def test_backend_prepare_rejects_mismatch_in_any_pinned_member(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = replace(
        PINNED,
        streams=tuple(replace(PINNED.streams[0], sink_input=i) for i in range(7, 10)),
    )
    checked: list[int] = []
    mismatch: int | None = None

    class NativeDouble(Pulse):
        def resolve(
            self, index: int, *, expected_pid: int | None = None, confirm_loss: bool = False
        ) -> Target:
            checked.append(index)
            return replace(RESOLVED, index=index, serial=RESOLVED.serial + (index == mismatch))

    with monkeypatch.context() as patch:
        patch.setattr(monitor_stream, "NativePulse", NativeDouble)
        backend = monitor_stream.CapturePulseBackend()
        assert backend.prepare(FAR, target=target)["far_available"]
        assert checked == [7, 8, 9]
        for mismatch in [8, 9]:
            checked.clear()
            with pytest.raises(ValueError, match="^target-mismatch$"):
                backend.prepare(FAR, target=target)
            assert checked == list(range(7, mismatch + 1))


@pytest.mark.parametrize("count,reason", [(0, "target-gone"), (9, "target-mismatch")])
def test_backend_rejects_targets_outside_the_member_limit(
    monkeypatch: pytest.MonkeyPatch, count: int, reason: str
) -> None:
    target = replace(PINNED, streams=PINNED.streams * count)
    backend = monitor_stream.CapturePulseBackend()
    with monkeypatch.context() as patch:
        patch.setattr(monitor_stream, "NativePulse", Pulse)
        with pytest.raises(ValueError, match=f"^{reason}$"):
            backend.source(FAR, target=target)
        assert backend.control is None
        with pytest.raises(ValueError, match=f"^{reason}$"):
            backend.prepare(FAR, target=target)
        assert isinstance(backend.control, Pulse) and backend.control.calls == []


@pytest.mark.parametrize("boundary", [None, "create", "connect"])
def test_mixer_propagates_admission_check_and_guard_to_native_boundaries(
    monkeypatch: pytest.MonkeyPatch, boundary: str | None
) -> None:
    from astra_voice.worker import pulse_stream

    libs: list[Mock] = []
    allowed = True
    guard_held = False
    target = replace(PINNED, streams=(PINNED.streams[0], replace(PINNED.streams[0], sink_input=8)))

    @contextmanager
    def guard() -> Iterator[None]:
        nonlocal guard_held
        assert not guard_held
        guard_held = True
        try:
            yield
        finally:
            guard_held = False

    def init(pulse: Any) -> None:
        pulse.lib = lib = Mock()
        pulse.mainloop = None
        second = bool(libs)
        libs.append(lib)
        disconnected = False

        def gated_create(*_args: object) -> int:
            assert guard_held and allowed
            return 22

        def gated_connect(*_args: object) -> int:
            assert guard_held and allowed
            return 0

        def proplist_set(*_args: object) -> int:
            nonlocal allowed
            if second and boundary == "create":
                allowed = False
            return 0

        def set_monitor(*_args: object) -> int:
            nonlocal allowed
            if second and boundary == "connect":
                allowed = False
            return 0

        def disconnect(*_args: object) -> int:
            nonlocal disconnected
            disconnected = True
            return 0

        lib.pa_proplist_new.return_value = 10
        lib.pa_proplist_sets.side_effect = proplist_set
        lib.pa_stream_new_with_proplist.side_effect = gated_create
        lib.pa_stream_set_monitor_stream.side_effect = set_monitor
        lib.pa_stream_connect_record.side_effect = gated_connect
        lib.pa_stream_disconnect.side_effect = disconnect
        lib.pa_context_get_state.return_value = 4
        lib.pa_stream_get_state.side_effect = lambda _: 4 if disconnected else 2

    class NativeDouble(monitor_stream.NativePulse):
        def _connect(self) -> None:
            self.context = 1

        def resolve(
            self, index: int, *, expected_pid: int | None = None, confirm_loss: bool = False
        ) -> Target:
            return replace(RESOLVED, index=index)

    with monkeypatch.context() as patch:
        patch.setattr(pulse_stream._PulseAsync, "__init__", init)
        patch.setattr(monitor_stream, "NativePulse", NativeDouble)
        source = monitor_stream.CapturePulseBackend().source(FAR, target=target)
        assert isinstance(source, monitor_stream.MixedFarSource)
        assert libs == []
        source.admission_check = lambda: allowed
        source.admission_guard = guard
        try:
            if boundary is None:
                source.open()
            else:
                with pytest.raises(ValueError, match="^admission-stale$"):
                    source.open()
            first, second = libs
            first.pa_stream_new_with_proplist.assert_called_once()
            first.pa_stream_set_monitor_stream.assert_called_once_with(22, 7)
            first.pa_stream_connect_record.assert_called_once()
            assert second.pa_stream_new_with_proplist.call_count == (boundary != "create")
            if boundary == "create":
                second.pa_stream_set_monitor_stream.assert_not_called()
            else:
                second.pa_stream_set_monitor_stream.assert_called_once_with(22, 8)
            assert second.pa_stream_connect_record.call_count == (boundary is None)
            for lib in libs:
                steps = [
                    call[0]
                    for call in lib.method_calls
                    if call[0] in {"pa_stream_set_monitor_stream", "pa_stream_connect_record"}
                ]
                if lib.pa_stream_connect_record.called:
                    assert steps == ["pa_stream_set_monitor_stream", "pa_stream_connect_record"]
        finally:
            source.shutdown()


def test_backend_prepare_reports_unsupported_monitor_without_pcm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pulse = Pulse()
    pulse.monitor_supported = False
    with monkeypatch.context() as patch:
        patch.setattr(monitor_stream, "NativePulse", lambda: pulse)
        info = monitor_stream.CapturePulseBackend().prepare(FAR, target=PINNED)
    assert info["far_available"] is False and info["far_reason"] == "monitor-stream-unsupported"
    assert not pulse.calls


def fake_native_control(
    patch: pytest.MonkeyPatch,
    resolved: Target,
    *,
    stream_props: dict[bytes, bytes],
    client_props: dict[bytes, bytes],
    server_local: bool = True,
) -> monitor_stream.NativePulse:
    row = monitor_stream._InputInfo(
        index=resolved.index, client=resolved.client, sink=5, proplist=1
    )
    client = monitor_stream._ClientInfo(index=resolved.client, proplist=2)
    sink = monitor_stream._SinkInfo(monitor_name=resolved.monitor.encode())
    properties = {
        slot: {key: ctypes.create_string_buffer(value) for key, value in props.items()}
        for slot, props in ((1, stream_props), (2, client_props))
    }
    native = monitor_stream.NativePulse.__new__(monitor_stream.NativePulse)
    native.cookie, native.context, native.pulse = resolved.cookie, 1, Mock()
    native.server_kind, native.server_local, native.monitor_supported = (
        "pulseaudio",
        server_local,
        True,
    )

    def get(proplist: int, key: bytes, pointer: Any, size: Any) -> int:
        buffer = properties[proplist].get(key)
        if buffer is None:
            return -1
        ctypes.cast(pointer, ctypes.POINTER(ctypes.c_void_p))[0] = ctypes.addressof(buffer)
        ctypes.cast(size, ctypes.POINTER(ctypes.c_size_t))[0] = ctypes.sizeof(buffer)
        return 0

    def serve(info: Any, expected_index: int) -> Any:
        def operation(context: int, index: int, receive: Any, userdata: Any) -> int:
            assert index == expected_index
            receive(context, ctypes.pointer(info), 0, userdata)
            receive(context, None, 1, userdata)
            return 1

        return operation

    native.pulse.lib.pa_proplist_get.side_effect = get
    native.pulse.lib.pa_context_get_sink_input_info.side_effect = serve(row, resolved.index)
    native.pulse.lib.pa_context_get_client_info.side_effect = serve(client, resolved.client)
    native.pulse.lib.pa_context_get_sink_info_by_index.side_effect = serve(sink, 5)
    native.pulse.lib.pa_context_get_index.return_value = 51
    patch.setattr(native, "_server", lambda: None)
    patch.setattr(native, "_wait", lambda ready: ready())
    return native


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "serial",
        "pid",
        "client",
        "parse",
        "uid",
        "client-pid",
        "conflict",
        OSError,
        ProcUnavailable,
        ProcessGone,
        IdentityLimit,
    ],
)
def test_native_resolve_reads_client_and_proc_starttime(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fault: Any
) -> None:
    proc = tmp_path / "proc"
    directory = proc / "123"
    directory.mkdir(parents=True)
    stat_fields = " ".join(["S", *(["0"] * 18), "431"])
    (directory / "stat").write_text(
        "bad stat" if fault == "parse" else f"123 (dion ) app) {stat_fields}\n"
    )
    owner = directory.stat().st_uid
    reader = ProcReader(proc, uid=owner + (fault == "uid"))
    stream_props = {b"object.serial": b"11"}
    if fault == "serial":
        stream_props.clear()
    if fault not in {"pid", "client-pid"}:
        stream_props[b"application.process.id"] = b"123"
    # The valid stat for the conflicting client makes PID rejection observable
    # even if a regression falls through to reading its stat.
    if fault == "client-pid":
        other = proc / "124"
        other.mkdir()
        (other / "stat").write_text(f"124 (dion) {stat_fields}\n")
    client_props = (
        {b"application.process.id": b"124"} if fault in {"client-pid", "conflict"} else {}
    )
    with monkeypatch.context() as patch:
        native = fake_native_control(
            patch,
            replace(RESOLVED, client=0xFFFFFFFF) if fault == "client" else RESOLVED,
            stream_props=stream_props,
            client_props=client_props,
        )
        patch.setattr(monitor_stream, "ProcReader", lambda **_: reader)
        if fault == "uid":
            with pytest.raises(ProcUnavailable):
                reader.starttime(123)
        elif isinstance(fault, type):
            patch.setattr(reader, "starttime", Mock(side_effect=fault))
        resolved = native.resolve(7, expected_pid=123)
    assert resolved == (RESOLVED if fault is None else None)


def test_first_resolve_rejects_wrong_client_before_connect() -> None:
    pulse = Pulse()
    pulse.target = replace(RESOLVED, client=RESOLVED.client + 1)
    pulse.after_open.append(RESOLVED)
    with pytest.raises(ValueError, match="^target-mismatch$"):
        make(pulse).open()
    assert pulse.calls == [("resolve", RESOLVED.index)]


@pytest.mark.parametrize("moving", [False, True])
def test_connect_failure_with_live_target_preserves_audio_failed(moving: bool) -> None:
    pulse = Pulse()
    source = make(pulse)
    monitor = RESOLVED.monitor
    if moving:
        source.open()
        monitor = "sink-b.monitor"
        pulse.target = replace(RESOLVED, monitor=monitor)
        pulse.events.append(("changed", RESOLVED.index))
    pulse.calls.clear()
    pulse.open_error = ValueError("audio-failed")
    with pytest.raises(ValueError, match="^audio-failed$"):
        source.read_chunk() if moving else source.open()
    assert pulse.calls == [
        ("resolve", RESOLVED.index),
        *([("closed",)] if moving else []),
        (monitor, RESOLVED.index, "run1"),
        ("closed",),
        ("resolve", RESOLVED.index),
    ]
    assert not source.opened and not source.ended


@pytest.mark.parametrize("field,value", [*MISMATCHES, ("missing", 0)])
def test_second_resolve_rejects_change_during_connect(field: str, value: int) -> None:
    pulse = Pulse()
    pulse.after_open.append(
        None if field == "missing" else replace_identity_field(RESOLVED, field, value)
    )
    source = make(pulse)
    reason = "target-gone" if field == "missing" else "target-mismatch"
    with pytest.raises(ValueError, match=reason):
        source.open()
    assert pulse.calls == [
        ("resolve", 7),
        ("sink-a.monitor", 7, "run1"),
        ("resolve", 7),
        ("closed",),
    ]
    assert not source.opened and source.read_chunk() is None


@pytest.mark.parametrize("field,value", MISMATCHES)
@pytest.mark.parametrize("event", [("changed", 7), ("server", 0)])
def test_identity_change_ends_target(field: str, value: int, event: tuple[str, int]) -> None:
    pulse = Pulse()
    source = make(pulse)
    source.open()
    assert pulse.target is not None
    pulse.target = replace_identity_field(pulse.target, field, value)
    pulse.events.append(event)
    assert source.read_chunk() is None and source.ended


@pytest.mark.parametrize("field,value", [("same", 0), *MISMATCHES, ("missing", 0)])
def test_sink_move_reopens_exact_target_and_marks_clock_boundary(field: str, value: int) -> None:
    pulse = Pulse()
    source = make(pulse)
    source.open()
    pulse.target = replace(RESOLVED, monitor="sink-b.monitor")
    if field != "same":
        pulse.after_open.append(
            None if field == "missing" else replace_identity_field(pulse.target, field, value)
        )
    pulse.calls.clear()
    pulse.events.append(("changed", 7))
    expected = [("resolve", 7), ("closed",), ("sink-b.monitor", 7, "run1"), ("resolve", 7)]
    if field == "same":
        assert source.read_chunk() == b"" and source.change == "sink-moved"
        assert pulse.calls == expected
    else:
        assert source.read_chunk() is None and source.ended and source.change is None
        assert pulse.calls == [*expected, ("closed",)]


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


def test_native_prepare_and_own_context_never_create_pcm(
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
    with monkeypatch.context() as patch:
        patch.setattr(module, "_meeting_pulse_type", lambda: lambda: pulse)
        patch.setattr(module.NativePulse, "_server", lambda self: None)
        backend = module.CapturePulseBackend()
        assert backend.own_context() is None
        assert not hasattr(module.CapturePulseBackend, "detector_start")
        try:
            backend.prepare({"kind": "none"})
            assert backend.own_context() == (42, 0)
            pulse.lib.pa_context_get_index.return_value = 0xFFFFFFFF
            assert backend.own_context() is None
            pulse.stream_new.assert_not_called()
            pulse.stream_connect_record.assert_not_called()
            pulse.lib.pa_stream_new_with_proplist.assert_not_called()
        finally:
            backend.close()


def test_capture_context_new_and_connect_take_the_observer_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import Mock

    from astra_voice.worker import monitor_stream, pulse_stream
    from astra_voice.worker.meeting_audio_local import CONNECT_LOCK

    lib = Mock()

    def locked_call(*args: Any) -> int:
        assert CONNECT_LOCK.locked()
        return 1

    lib.pa_context_new.side_effect = locked_call
    lib.pa_context_connect.side_effect = locked_call
    with monkeypatch.context() as patch:
        patch.setattr(pulse_stream._PulseAsync, "__init__", lambda self: setattr(self, "lib", lib))
        native = monitor_stream._meeting_pulse_type()()
        assert native.context_new(2, b"test") == 1
        assert native.context_connect(1, None, pulse_stream.PA_CONTEXT_NOAUTOSPAWN, None) == 1
    assert not CONNECT_LOCK.locked()


@pytest.mark.parametrize("kind", ["app_stream", "sink_monitor"])
def test_session_guard_refuses_far_reopen_after_resolution(kind: str) -> None:
    pulse = Pulse()
    source = make(pulse, kind)
    allowed = [True]
    source.admission_check = lambda: allowed[0]
    source.open()
    if kind == "app_stream":

        def resolve(
            index: int, *, expected_pid: int | None = None, confirm_loss: bool = False
        ) -> Target | None:
            allowed[0] = False
            return replace(RESOLVED, monitor="sink-b.monitor")

        pulse.resolve = resolve  # type: ignore[method-assign]
        pulse.events.append(("changed", 7))
    else:

        def default() -> str:
            allowed[0] = False
            return "sink-b.monitor"

        pulse.default_monitor = default  # type: ignore[method-assign]
        pulse.events.append(("server", 0))
    assert source.read_chunk() is None
    assert len([call for call in pulse.calls if len(call) == 3]) == 1
    assert not source.opened


@pytest.mark.parametrize("boundary", ["create", "connect"])
def test_native_meeting_admission_rechecked_at_pcm_boundary(
    monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    from unittest.mock import Mock

    from astra_voice.worker import pulse_stream
    from astra_voice.worker.monitor_stream import _meeting_pulse_type

    lib = Mock()
    lib.pa_proplist_new.return_value = 10
    lib.pa_proplist_sets.return_value = 0
    lib.pa_stream_new_with_proplist.return_value = 22
    monkeypatch.setattr(
        pulse_stream._PulseAsync, "__init__", lambda self: setattr(self, "lib", lib)
    )
    native = _meeting_pulse_type()()
    native.admission_check = lambda: False
    with pytest.raises(ValueError, match="admission-stale"):
        if boundary == "create":
            native.stream_new(1, b"meeting", None, None)
        else:
            native.stream_connect_record(22, b"source", None, 0)
    lib.pa_stream_new_with_proplist.assert_not_called()
    lib.pa_stream_connect_record.assert_not_called()
