"""D4: unload ACK follows native release; allocator trimming is optional off glibc."""

from __future__ import annotations

import ctypes
import time
import weakref
from types import SimpleNamespace
from typing import Any

import pytest

from astra_voice.worker import engine, vad
from astra_voice.worker.engine import EngineError
from astra_voice.worker.meeting_transcribe import MeetingTranscribeState

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("real_heap_trim")]


class Resident:
    def __init__(self) -> None:
        self.cycle = self


class Engine:
    def __init__(self, *, stuck: bool = False) -> None:
        self._model: Resident | None = Resident()
        self.stuck = stuck

    def unload(self) -> None:
        if not self.stuck:
            self._model = None


class Trim:
    argtypes: Any = None
    restype: Any = None

    def __init__(self) -> None:
        self.calls: list[int] = []

    def __call__(self, pad: int) -> int:
        self.calls.append(pad)
        return 1


def test_unload_ack_drops_engine_vad_and_trims_before_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = MeetingTranscribeState()
    resident = Engine()
    engine_ref = weakref.ref(resident)
    model_ref = weakref.ref(resident._model)
    session = Resident()
    vad_ref = weakref.ref(session)
    state.engine = resident  # type: ignore[assignment]
    state.loaded = {"type": "model.loaded"}
    monkeypatch.setattr(vad, "_session", session)
    monkeypatch.setattr(vad, "_load_attempted", True)
    del resident, session
    trim = Trim()
    monkeypatch.setattr(
        ctypes, "CDLL", lambda _: SimpleNamespace(gnu_get_libc_version=object(), malloc_trim=trim)
    )
    try:
        state.handle({"type": "model.unload", "req": "u1", "request_type": "model.unload"})
        until = time.monotonic() + 2
        events: list[dict[str, Any]] = []
        while time.monotonic() < until:
            events.extend(state.poll(16))
            if any(e["type"] == "model.unloaded" for e in events):
                break
            time.sleep(0.001)
        assert any(e["type"] == "model.unloaded" for e in events), events
        assert state.engine is None and state.loaded is None
        assert vad._session is None and not vad._load_attempted
        assert engine_ref() is None and model_ref() is None and vad_ref() is None
        assert trim.calls == [0]
        assert trim.argtypes == [ctypes.c_size_t] and trim.restype is ctypes.c_int
    finally:
        state.cleanup()


def test_unconfirmed_unload_never_trims_or_acks(monkeypatch: pytest.MonkeyPatch) -> None:
    state = MeetingTranscribeState()
    state.engine = Engine(stuck=True)  # type: ignore[assignment]
    calls: list[bool] = []
    monkeypatch.setattr(engine, "_trim_heap", lambda: calls.append(True))
    try:
        with pytest.raises(EngineError, match="unload-unconfirmed"):
            state._unload_ack()
        assert state.engine is not None and calls == []
    finally:
        state.cleanup()


@pytest.mark.parametrize("missing", ["glibc", "trim", "libc"])
def test_trim_heap_without_glibc_extension_is_harmless(
    monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    trim = Trim()

    def library(_: Any) -> Any:
        if missing == "libc":
            raise OSError("no process library")
        if missing == "glibc":
            return SimpleNamespace(malloc_trim=trim)
        return SimpleNamespace(gnu_get_libc_version=object())

    monkeypatch.setattr(ctypes, "CDLL", library)
    engine._trim_heap()
    assert trim.calls == []


def test_trim_malloc_does_not_collect_python_objects(monkeypatch: pytest.MonkeyPatch) -> None:
    import gc

    def forbidden() -> None:
        raise AssertionError("block trimming must not run Python GC")

    trim = Trim()
    monkeypatch.setattr(gc, "collect", forbidden)
    monkeypatch.setattr(
        ctypes, "CDLL", lambda _: SimpleNamespace(gnu_get_libc_version=object(), malloc_trim=trim)
    )
    engine._trim_malloc()
    assert trim.calls == [0]
