"""MN-2 option A: a wake gesture records only while PTT is physically held.

Exercise real keyboard event routing; an FSM left in RECORDING after a short
PTT tap is deliberately insufficient evidence that the key remains down.
Worker, time and UI ports stay isolated through the runtime Rig.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from unittest.mock import Mock

import pytest
from test_model_idle_unload import load_model, park, sent
from test_runtime import MARKER, Rig, assert_phase

from astra_voice.core.dictation import DictationPhase
from astra_voice.core.settings import from_dict
from astra_voice.models.catalog_effective import CATALOG_LOCK
from astra_voice.platform.hotkey import (
    GrabResult,
    HotkeyBackend,
    HotkeyEvent,
    HotkeyManager,
    HotkeyState,
    MappingEvent,
)
from astra_voice.platform.paste import snapshot_target
from astra_voice.ui.pill import PillState

pytestmark = pytest.mark.unit


@dataclass
class WakeRig:
    rig: Rig
    hotkey: HotkeyManager
    batches: list[list[HotkeyEvent | MappingEvent]]

    def key(
        self,
        kind: Literal["KeyPress", "KeyRelease"],
        *,
        after: float = 0.0,
        code: int = 65,
        confirmed_hold: bool = False,
    ) -> None:
        self.rig.now += after
        event = HotkeyEvent(
            kind,
            code,
            int(self.rig.now * 1000),
            escape=code == 9,
            mods=4 if code == 65 else 0,
            confirmed_hold=confirmed_hold,
        )
        self.hotkey.handle_event(event, self.rig.now)

    def mapped(self, result: GrabResult) -> None:
        self.batches.append([MappingEvent({self.rig.runtime.settings.hotkey: result}, None)])
        self.rig.runtime._process_hotkey()

    def loaded(self) -> None:
        self.rig.event(type="model.loaded")
        assert sent(self.rig)[-1] == "transcribe.file"
        assert "record.start" not in sent(self.rig)

    def ready(self) -> None:
        self.rig.event(type="result", utterance_id="file", text="проверка")


def unloaded(
    monkeypatch: pytest.MonkeyPatch,
    *,
    mode: str = "ptt",
) -> WakeRig:
    backend = Mock(spec=HotkeyBackend)
    backend.grab_combo.return_value = GrabResult("ok", keycode=65, mods=4)
    backend.grab_escape.return_value = GrabResult("ok", keycode=9)
    backend.ungrab_combo.return_value = GrabResult("ok")
    backend.fileno.return_value = 17
    batches: list[list[HotkeyEvent | MappingEvent]] = []
    backend.poll_events.side_effect = lambda timeout=0.0: (
        [] if timeout or not batches else batches.pop(0)
    )
    rig: Rig
    hotkey = HotkeyManager(backend, clock=lambda: rig.now)
    rig = Rig(
        monkeypatch,
        from_dict(
            {
                "model_dir": "/tmp/model",
                "model_unload_minutes": 5,
                "hotkey_mode": mode,
                "hotkey": "Ctrl+Space",
            }
        ),
        hotkey_factory=lambda: hotkey,
    )
    rig.runtime.start()
    load_model(rig)
    park(rig)
    rig.pill.show_state.reset_mock()
    rig.pill.hide.reset_mock()
    return WakeRig(rig, hotkey, batches)


def assert_load_only(wake: WakeRig) -> None:
    assert "record.start" not in sent(wake.rig)
    assert "record.cancel" not in sent(wake.rig)
    assert wake.hotkey.fsm.state is HotkeyState.IDLE
    assert_phase(wake.rig.runtime, DictationPhase.IDLE)
    assert wake.rig.runtime._selfcheck == "ok"
    assert not wake.rig.runtime._loading_model
    wake.rig.pill.hide.assert_called()


@pytest.mark.parametrize("duration", [0.1, 0.299, 0.3, 0.5])
@pytest.mark.parametrize("release_during", ["loading", "selfcheck"])
def test_ptt_release_before_ready_is_load_only(
    monkeypatch: pytest.MonkeyPatch, duration: float, release_during: str
) -> None:
    wake = unloaded(monkeypatch)
    loads = sent(wake.rig).count("model.load")
    wake.key("KeyPress")
    assert sent(wake.rig).count("model.load") == loads + 1
    wake.rig.pill.show_state.assert_called_once_with(PillState.LOADING_MODEL)
    if release_during == "selfcheck":
        wake.loaded()
    wake.key("KeyRelease", after=duration)
    assert wake.hotkey.fsm.state is HotkeyState.IDLE
    assert wake.rig.runtime._loading_model  # отпускание не отменяет загрузку
    if release_during == "loading":
        wake.loaded()
    wake.ready()
    assert_load_only(wake)
    # Новое физическое нажатие на готовой модели начинает обычную диктовку.
    wake.key("KeyPress", after=1)
    assert sent(wake.rig)[-1] == "record.start"
    assert sent(wake.rig).count("model.load") == loads + 1


@pytest.mark.parametrize("release_before_ready", [False, True])
def test_configured_toggle_wakes_only_even_if_held_through_ready(
    monkeypatch: pytest.MonkeyPatch, release_before_ready: bool
) -> None:
    wake = unloaded(monkeypatch, mode="toggle")
    wake.key("KeyPress")
    assert wake.hotkey.fsm.state is HotkeyState.IDLE
    if release_before_ready:
        wake.key("KeyRelease", after=0.1)
    wake.loaded()
    wake.ready()
    assert_load_only(wake)
    if not release_before_ready:
        # Автоповтор всё ещё удержанной первой клавиши — не следующий жест.
        wake.key("KeyPress", after=0.5)
        assert "record.start" not in sent(wake.rig)
        wake.key("KeyRelease", after=0.1)
    loads = sent(wake.rig).count("model.load")
    wake.key("KeyPress", after=1)
    assert sent(wake.rig)[-1] == "record.start"
    assert sent(wake.rig).count("model.load") == loads


@pytest.mark.parametrize("result", [GrabResult("ok", keycode=66), GrabResult("busy")])
def test_mapping_lost_release_cancels_wait_without_cancelling_load(
    monkeypatch: pytest.MonkeyPatch, result: GrabResult
) -> None:
    wake = unloaded(monkeypatch)
    wake.key("KeyPress")
    wake.mapped(result)
    assert wake.hotkey.mapping_release_lost
    assert wake.hotkey.fsm.state is HotkeyState.IDLE
    assert wake.rig.runtime._loading_model
    assert "record.start" not in sent(wake.rig)
    wake.loaded()
    wake.ready()
    assert_load_only(wake)


def test_successful_mapping_with_same_keycode_keeps_physical_hold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wake = unloaded(monkeypatch)
    wake.key("KeyPress")
    wake.mapped(GrabResult("ok", keycode=65, mods=4))
    assert not wake.hotkey.mapping_release_lost
    wake.loaded()
    wake.ready()
    assert sent(wake.rig).count("record.start") == 1
    assert_phase(wake.rig.runtime, DictationPhase.RECORDING)


@pytest.mark.parametrize("same_window", [False, True])
def test_physical_hold_uses_press_window_and_title_digest(
    monkeypatch: pytest.MonkeyPatch, same_window: bool
) -> None:
    wake = unloaded(monkeypatch)
    initial = snapshot_target(wake.rig.x11, 4321)
    assert initial.title_digest is not None
    wake.key("KeyPress")
    if not same_window:
        wake.rig.x11.target_window.return_value = 9876
    title = wake.rig.x11.d.create_resource_object.return_value.get_full_property.return_value
    title.value = b"A different title after model loading"
    wake.rig.now += 2
    wake.loaded()
    wake.ready()
    assert sent(wake.rig).count("record.start") == 1
    assert_phase(wake.rig.runtime, DictationPhase.RECORDING)
    wake.key("KeyRelease", after=1)
    wake.rig.fire_tail()
    wake.rig.event(type="result", text=MARKER)
    assert wake.rig.paste.call_count == 1
    assert wake.rig.paste.call_args.args[1] == initial


def test_escape_while_waking_leaves_model_loading_without_autostart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wake = unloaded(monkeypatch)
    wake.key("KeyPress")
    wake.key("KeyPress", code=9, after=0.1)
    assert wake.hotkey.fsm.state is HotkeyState.IDLE
    assert wake.rig.runtime._loading_model
    wake.loaded()
    wake.ready()
    assert_load_only(wake)


@pytest.mark.parametrize("state", [HotkeyState.IDLE, HotkeyState.PROCESSING])
def test_ready_does_not_restart_abandoned_fsm_while_physical_key_is_down(
    monkeypatch: pytest.MonkeyPatch, state: HotkeyState
) -> None:
    wake = unloaded(monkeypatch)
    wake.key("KeyPress")
    assert wake.hotkey.physical_key_down
    assert wake.rig.runtime._wake_dictation
    # Независимый сброс автомата не доказывает физическое отпускание клавиши.
    # Даже если ожидание пробуждения осталось, готовность не возобновляет запись.
    wake.hotkey.fsm.state = state
    wake.loaded()
    wake.ready()
    assert_load_only(wake)


@pytest.mark.parametrize("barrier", ["model.loaded", "selfcheck"])
def test_t236_revocation_during_hotkey_wake_blocks_autostart(
    monkeypatch: pytest.MonkeyPatch, barrier: str
) -> None:
    """T-236 (N-4, слияние M7 с US-2.6): отзыв на барьере ready во время пробуждения.

    Удержанный PTT разбудил выгруженную модель; отзыв принят до ready → запись не
    стартует ни в одном воркере, автомат хоткея в IDLE, ожидание пробуждения
    сброшено, прежний воркер остановлен один раз, а чистый воркер модель не грузит.
    """
    wake = unloaded(monkeypatch)
    rig, runtime = wake.rig, wake.rig.runtime
    revoked = [False]
    runtime.set_revoked_check(lambda _model_id, _revision: revoked[0])
    wake.key("KeyPress")
    assert runtime._wake_dictation and runtime._wake_generation == rig.supervisor.generation
    assert wake.hotkey.physical_key_down
    generation = rig.supervisor.generation
    stops = rig.supervisor.stop.call_count
    starts = rig.supervisor.start.call_count
    if barrier == "selfcheck":
        wake.loaded()
    revoked[0] = True
    if barrier == "model.loaded":
        rig.event(type="model.loaded")
    else:
        wake.ready()

    assert rig.supervisor.stop.call_count == stops + 1
    assert rig.supervisor.start.call_count == starts + 1
    assert rig.supervisor.generation == generation + 1
    assert runtime._selfcheck == "failed" and not runtime._loading_model
    assert not runtime._wake_dictation
    assert runtime._wake_window is None and runtime._wake_generation is None
    assert not runtime._model_parked and runtime._idle_unload_timer is None
    assert wake.hotkey.fsm.state is HotkeyState.IDLE
    assert_phase(runtime, DictationPhase.IDLE)
    loads = sent(rig).count("model.load")
    rig.event(type="hello")
    # failed: чистый воркер не грузит модель на hello — отозванной модели в памяти нет.
    assert sent(rig).count("model.load") == loads
    assert "record.start" not in sent(rig)
    # Отпускание и новое нажатие не шлют чистому воркеру ни записи, ни загрузки.
    before = len(sent(rig))
    wake.key("KeyRelease", after=0.1)
    wake.key("KeyPress", after=1)
    assert sent(rig)[before:] == []


def test_t236_wake_autostart_runs_after_catalog_lock_is_released(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T-236 / N-4(б): под CATALOG_LOCK — только решение о готовности, без записи."""
    wake = unloaded(monkeypatch)
    rig, runtime = wake.rig, wake.rig.runtime
    checks: list[bool] = []

    def not_revoked(_model_id: str, _revision: str) -> bool:
        checks.append(lock_owned())
        return False

    runtime.set_revoked_check(not_revoked)
    owned_at_start: list[bool] = []
    send = rig.send

    def observe(message: dict[str, object], **kwargs: object) -> object:
        if message["type"] == "record.start":
            owned_at_start.append(lock_owned())
        return send(message, **kwargs)

    rig.supervisor.send.side_effect = observe
    wake.key("KeyPress")
    wake.loaded()
    checks.clear()
    wake.ready()

    assert checks == [True]  # барьер ready проверяет отзыв под замком
    assert owned_at_start == [False]  # автостарт записи — уже после замка
    assert sent(rig).count("record.start") == 1
    assert_phase(runtime, DictationPhase.RECORDING)
    assert not runtime._wake_dictation and runtime._wake_generation is None


def lock_owned() -> bool:
    # CPython даёт этот probe, но в typeshed у RLock его нет.
    is_owned = getattr(CATALOG_LOCK, "_is_owned")  # noqa: B009
    return bool(is_owned())
