"""Команды настоящего GUI совместимы с кодеком IPC без потери полей."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

from astra_voice.core.dictation import CANCEL_TIMEOUT_MS, RECOGNIZE_TIMEOUT_S, DictationPhase
from astra_voice.core.model_request import build_model_load
from astra_voice.core.settings import Settings
from astra_voice.platform.hotkey import RECORD_LIMIT_S, HotkeyState
from astra_voice.platform.session import SessionKind
from astra_voice.runtime import DictationRuntime
from astra_voice.worker import ipc

pytestmark = pytest.mark.unit


@pytest.fixture
def runtime(monkeypatch: pytest.MonkeyPatch) -> Iterator[DictationRuntime]:
    """Подменяем внешние ресурсы, сохраняя проводку runtime → оркестратор → send."""
    monkeypatch.setattr(DictationRuntime, "_create_timer", lambda self: Mock())
    supervisor = Mock(generation=1, state="running")

    def send(message: dict[str, Any], *, timeout: float | None = None) -> None:
        ipc.encode(message)

    supervisor.send.side_effect = send
    instance = DictationRuntime(
        settings=Settings(),
        session_kind=SessionKind.FLY,
        supervisor_factory=Mock(return_value=supervisor),
        pill_factory=Mock(),
        tray_factory=Mock(),
        hotkey_factory=Mock(),
        stats_factory=Mock(),
        paste_func=Mock(),
        restore_paste=Mock(),
        x11_factory=Mock(return_value=Mock(active_window=Mock(return_value=42))),
        guard_factory=Mock(),
        provider_factory=Mock(),
    )
    yield instance
    instance.shutdown()


@pytest.mark.parametrize(
    "extra",
    [{}, {"device": "av_test_src"}, {"device": None}, {"device": ""}, {"device": 123}],
    ids=["default", "named", "none", "empty", "wrong-type"],
)
def test_gui_dictation_commands_encode(runtime: DictationRuntime, extra: dict[str, Any]) -> None:
    runtime.settings.extra.update(extra)
    on_state = runtime.hotkey.on_state
    assert on_state is not None
    on_state(HotkeyState.RECORDING, "press")
    assert runtime.phase == DictationPhase.RECORDING
    send = runtime.supervisor.send
    assert isinstance(send, Mock)
    start = send.call_args.args[0]
    uid = start["utterance_id"]
    assert uid
    expected = {"type": "record.start", "utterance_id": uid}
    if extra.get("device") == "av_test_src":
        expected["device"] = "av_test_src"
    assert start == expected
    assert runtime.record_params() == {"device": extra.get("device"), "limit_s": RECORD_LIMIT_S}
    assert send.call_args.kwargs == {"timeout": RECORD_LIMIT_S + RECOGNIZE_TIMEOUT_S}

    on_state(HotkeyState.PROCESSING, "release")
    runtime.orchestrator.cancel("escape")
    # Нет подтверждения отмены: настоящий callback таймера отправляет audio.close.
    timer = next(t for t in runtime.timers if t.start.call_args.args == (CANCEL_TIMEOUT_MS,))
    timer.timeout.connect.call_args.args[0]()
    messages = [call.args[0] for call in send.call_args_list]
    assert [message["type"] for message in messages] == [
        "record.start",
        "record.stop",
        "recognize",
        "record.cancel",
        "audio.close",
    ]
    for message in messages:
        assert ipc.decode(ipc.encode(message)[4:]) == message
    for message in messages[1:4]:
        assert message == {"type": message["type"], "utterance_id": uid}
    assert messages[-1] == {"type": "audio.close"}


def test_record_start_with_explicit_none_is_rejected(runtime: DictationRuntime) -> None:
    assert runtime.hotkey.on_state is not None
    runtime.hotkey.on_state(HotkeyState.RECORDING, "press")
    send = runtime.supervisor.send
    assert isinstance(send, Mock)
    message = send.call_args.args[0].copy()
    message["device"] = None
    with pytest.raises(ipc.FrameError, match="Неверный тип поля device") as caught:
        ipc.encode(message)
    assert caught.value.code == ipc.BAD_FIELD


def test_model_load_encodes(tmp_path: Path) -> None:
    settings = Settings(model_id="test-model", extra={"model_revision": "test-revision"})
    message = build_model_load(settings.to_dict(), store_dir=tmp_path / "store")
    assert message["type"] == "model.load"
    assert ipc.decode(ipc.encode(message)[4:]) == message
