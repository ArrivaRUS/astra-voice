"""US-2.6: выгрузка модели по простою и загрузка заново по нажатию.

Настоящие оркестратор, автомат хоткея и мост настроек; воркер, таймеры и время — фейки
из test_runtime. Выгрузка — новый процесс воркера без model.load (RSS как у свежего
воркера); следующее нажатие показывает «Загружаю модель…», а запись начинается, когда
модель готова, если клавиша ещё не отпущена.
"""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import Mock

import pytest
from test_command_mouse_runtime import MouseRuntimeRig
from test_runtime import MARKER, FakeTimer, Rig, assert_phase

from astra_voice.core.dictation import DictationPhase
from astra_voice.core.settings import MODEL_UNLOAD_MINUTES, Settings, from_dict
from astra_voice.platform.hotkey import HotkeyState
from astra_voice.platform.mouse_button import MouseHoldState
from astra_voice.ui.bridges import SettingsApply, SettingsBridge
from astra_voice.ui.pill import ERROR_MODEL_LOAD_FAILED, PillState

pytestmark = pytest.mark.unit
FIVE_MIN_S = 5 * 60.0


def make_rig(monkeypatch: pytest.MonkeyPatch, minutes: int = 5) -> Rig:
    rig = Rig(monkeypatch, from_dict({"model_dir": "/tmp/model", "model_unload_minutes": minutes}))
    rig.runtime.start()
    # Mock хоткея отвечает Mock на has_pending_press; настоящий менеджер — False в покое.
    rig.hotkey.has_pending_press = False
    load_model(rig)
    return rig


def load_model(rig: Rig) -> None:
    rig.event(type="hello")
    rig.event(type="model.loaded")
    rig.event(type="result", utterance_id="file", text="проверка")
    assert rig.runtime._selfcheck == "ok" and not rig.runtime._loading_model


def sent(rig: Rig) -> list[str]:
    return [str(item.args[0]["type"]) for item in rig.supervisor.send.call_args_list]


def idle_timer(rig: Rig) -> FakeTimer | None:
    return cast(FakeTimer | None, rig.runtime._idle_unload_timer)


def fire_idle(rig: Rig) -> None:
    timer = idle_timer(rig)
    assert timer is not None and timer.active
    timer.fire()


def settle(rig: Rig) -> None:
    """Доводит короткие таймеры фаз (пилюля «Готово», сторож отмены) до IDLE."""
    for timer in list(rig.timers):
        if timer.name == "scheduled" and timer is not idle_timer(rig) and timer.interval < 60_000:
            timer.fire()


def park(rig: Rig) -> None:
    rig.now += FIVE_MIN_S
    fire_idle(rig)
    assert rig.runtime._model_parked
    rig.event(type="hello")


def test_always_keeps_model_and_never_arms_unload(monkeypatch: pytest.MonkeyPatch) -> None:
    rig = make_rig(monkeypatch, minutes=0)
    assert Settings().model_unload_minutes == 0
    assert idle_timer(rig) is None
    rig.now += 24 * 3600
    rig.recognize()
    for timer in rig.timers:
        timer.fire()
    assert idle_timer(rig) is None
    assert not rig.runtime._model_parked
    rig.supervisor_factory.assert_called_once()
    rig.supervisor.stop.assert_not_called()


def test_unload_after_idle_restarts_worker_without_model(monkeypatch: pytest.MonkeyPatch) -> None:
    rig = make_rig(monkeypatch)
    timer = idle_timer(rig)
    assert timer is not None and timer.interval == FIVE_MIN_S * 1000
    generation = rig.supervisor.generation
    loads = sent(rig).count("model.load")
    rig.pill.show_state.reset_mock()
    rig.now += FIVE_MIN_S - 1
    fire_idle(rig)  # раньше срока проверка лишь переносится на остаток
    assert not rig.runtime._model_parked
    assert idle_timer(rig) is not None and idle_timer(rig).interval == 1000  # type: ignore[union-attr]
    rig.now += 1
    fire_idle(rig)
    assert rig.runtime._model_parked
    rig.supervisor.stop.assert_called_once()
    assert rig.supervisor.generation == generation + 1
    assert rig.supervisor_factory.call_count == 2
    rig.event(type="hello")
    # Новый процесс воркера модель не грузит: память — как у свежего воркера.
    assert sent(rig).count("model.load") == loads
    assert idle_timer(rig) is None
    rig.pill.show_state.assert_not_called()
    # Падение выгруженного воркера и его автоперезапуск тоже не грузят модель.
    rig.supervisor.generation += 1
    rig.event(type="hello")
    assert sent(rig).count("model.load") == loads


@pytest.mark.parametrize("activity", ["dictation", "worker-event", "hotkey"])
def test_any_activity_restarts_idle_countdown(
    monkeypatch: pytest.MonkeyPatch, activity: str
) -> None:
    rig = make_rig(monkeypatch)
    rig.now += 200
    if activity == "dictation":
        rig.recognize()
        rig.now += 1  # recognize отпускает клавишу на секунду позже
    elif activity == "worker-event":
        rig.event(type="measured", vm_hwm_kb=1000)
    else:
        rig.hotkey.fsm.press(rig.now)
        rig.hotkey.fsm.escape(rig.now)
        rig.event(type="cancelled", utterance_id=rig.runtime.orchestrator._utterance_id)
    settle(rig)
    assert_phase(rig.runtime, DictationPhase.IDLE)
    last = rig.now
    rig.now = 10.0 + FIVE_MIN_S + 50  # исходный срок истёк, с активности — нет
    fire_idle(rig)
    assert not rig.runtime._model_parked
    rig.now = last + FIVE_MIN_S
    fire_idle(rig)
    assert rig.runtime._model_parked


def test_work_in_progress_postpones_unload(monkeypatch: pytest.MonkeyPatch) -> None:
    rig = make_rig(monkeypatch)
    rig.hotkey.fsm.press(rig.now)
    assert_phase(rig.runtime, DictationPhase.RECORDING)
    rig.now += FIVE_MIN_S + 10  # долгая запись без событий воркера
    fire_idle(rig)
    assert not rig.runtime._model_parked
    rig.supervisor.stop.assert_not_called()
    rig.release(rig.now)
    rig.event(type="result", text=MARKER)
    rig.now = 10.0 + 2 * FIVE_MIN_S + 20  # срок, взведённый в записи: идёт пилюля «Готово»
    fire_idle(rig)
    assert not rig.runtime._model_parked
    settle(rig)
    finished = rig.now
    rig.now = finished + FIVE_MIN_S - 0.5
    fire_idle(rig)
    assert not rig.runtime._model_parked
    rig.now = finished + FIVE_MIN_S
    fire_idle(rig)
    assert rig.runtime._model_parked


def test_hold_during_reload_starts_recording_when_model_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = make_rig(monkeypatch)
    park(rig)
    rig.pill.show_state.reset_mock()
    rig.pill.hide.reset_mock()
    loads = sent(rig).count("model.load")
    rig.hotkey.fsm.press(rig.now)
    assert sent(rig).count("model.load") == loads + 1
    assert rig.supervisor.send.call_args.kwargs == {"timeout": 10.0}
    rig.pill.show_state.assert_called_once_with(PillState.LOADING_MODEL)
    assert_phase(rig.runtime, DictationPhase.IDLE)
    assert "record.start" not in sent(rig)[-1:]
    rig.now += 1.5
    rig.event(type="model.loaded")
    assert sent(rig)[-1] == "transcribe.file"  # самопроверка нового процесса
    rig.event(type="result", utterance_id="file", text="проверка")
    assert_phase(rig.runtime, DictationPhase.RECORDING)
    assert sent(rig)[-1] == "record.start"
    rig.pill.hide.assert_not_called()
    rig.release(rig.now + 2)
    rig.event(type="result", text=MARKER)
    assert rig.paste.call_args.args[0] == MARKER
    assert not rig.runtime._model_parked
    assert idle_timer(rig) is not None


@pytest.mark.parametrize("gesture", ["release", "escape"])
def test_released_before_ready_does_not_record(
    monkeypatch: pytest.MonkeyPatch, gesture: str
) -> None:
    rig = make_rig(monkeypatch)
    park(rig)
    rig.hotkey.fsm.press(rig.now)
    if gesture == "release":
        rig.release(rig.now + 0.5)
    else:
        rig.hotkey.fsm.escape(rig.now + 0.5)
    rig.event(type="model.loaded")
    rig.event(type="result", utterance_id="file", text="проверка")
    assert_phase(rig.runtime, DictationPhase.IDLE)
    assert "record.start" not in sent(rig)
    assert rig.hotkey.fsm.state is HotkeyState.IDLE
    rig.pill.hide.assert_called()
    # Следующее нажатие — обычная диктовка без загрузки.
    loads = sent(rig).count("model.load")
    rig.recognize()
    assert rig.paste.call_args.args[0] == MARKER
    assert sent(rig).count("model.load") == loads


def test_toggle_press_during_reload_starts_recording(monkeypatch: pytest.MonkeyPatch) -> None:
    rig = Rig(
        monkeypatch,
        from_dict({"model_dir": "/tmp/model", "model_unload_minutes": 15, "hotkey_mode": "toggle"}),
    )
    rig.runtime.start()
    rig.hotkey.has_pending_press = False
    load_model(rig)
    assert idle_timer(rig).interval == 15 * 60 * 1000  # type: ignore[union-attr]
    rig.now += 15 * 60
    fire_idle(rig)
    rig.event(type="hello")
    rig.hotkey.fsm.press(rig.now)
    rig.hotkey.fsm.release(rig.now + 0.1)  # переключение: запись не отпущена
    rig.event(type="model.loaded")
    rig.event(type="result", utterance_id="file", text="проверка")
    assert_phase(rig.runtime, DictationPhase.RECORDING)


def test_failed_reload_shows_error_and_does_not_record_later(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = make_rig(monkeypatch)
    park(rig)
    rig.hotkey.fsm.press(rig.now)
    rig.event(type="error", code="engine-failed", request_type="model.load")
    assert not rig.runtime._loading_model
    rig.pill.show_state.assert_called_with(PillState.ERROR, text=ERROR_MODEL_LOAD_FAILED)
    assert_phase(rig.runtime, DictationPhase.IDLE)
    assert not rig.runtime._wake_dictation


def test_setting_changes_apply_on_the_fly(monkeypatch: pytest.MonkeyPatch) -> None:
    rig = make_rig(monkeypatch, minutes=0)
    assert idle_timer(rig) is None
    rig.now += 1000
    rig.runtime.apply_model_unload_minutes(5)
    timer = idle_timer(rig)
    assert timer is not None and timer.interval == FIVE_MIN_S * 1000  # отсчёт с момента выбора
    rig.runtime.apply_model_unload_minutes(60)
    assert timer.deleted and idle_timer(rig).interval == 60 * 60 * 1000  # type: ignore[union-attr]
    rig.runtime.apply_model_unload_minutes(0)
    assert idle_timer(rig) is None
    rig.runtime.apply_model_unload_minutes(7)  # вне списка — без изменений
    assert rig.runtime.settings.model_unload_minutes == 0 and idle_timer(rig) is None
    # Выгруженная модель при выборе «Всегда» сразу возвращается в память.
    rig.runtime.apply_model_unload_minutes(5)
    park(rig)
    loads = sent(rig).count("model.load")
    rig.runtime.apply_model_unload_minutes(0)
    assert not rig.runtime._model_parked
    assert sent(rig).count("model.load") == loads + 1
    rig.event(type="model.loaded")
    rig.event(type="result", utterance_id="file", text="проверка")
    assert_phase(rig.runtime, DictationPhase.IDLE)
    assert idle_timer(rig) is None


def test_microphone_test_while_unloaded_loads_model(monkeypatch: pytest.MonkeyPatch) -> None:
    rig = make_rig(monkeypatch)
    park(rig)
    callback = Mock()
    assert rig.runtime.start_test("", callback)
    assert not rig.runtime._model_parked
    rig.event(type="hello")
    assert sent(rig)[-1] == "model.load"


def test_mouse_command_stays_armed_and_press_reloads_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mouse = MouseRuntimeRig(monkeypatch)
    runtime = mouse.runtime
    request: dict[str, Any] = {
        "type": "model.load",
        "id": "model",
        "revision": "revision",
        "dir": "/tmp/trusted",
        "layout": "onnx-asr-gigaam-v3",
        "variant": "gigaam-v3-e2e-rnnt",
        "threads": 2,
        "min_ram_mb": 768,
    }
    monkeypatch.setattr(runtime, "_resolve_model_request", lambda: dict(request))
    runtime.restart_worker(_park=True)
    mouse.rig.event(type="hello")
    retry = runtime._mouse_retry_timer
    assert retry is not None
    cast(FakeTimer, retry).fire()
    assert runtime.command_mouse_status == "ready"  # кнопка захвачена и при выгрузке
    mouse.press()
    assert not runtime._model_parked and runtime._loading_model
    assert mouse.sent("model.load")
    assert_phase(runtime, DictationPhase.IDLE)
    assert mouse.manager.state is MouseHoldState.IDLE
    mouse.rig.pill.show_state.assert_called_with(PillState.LOADING_MODEL)
    retry = runtime._mouse_retry_timer
    assert retry is not None
    cast(FakeTimer, retry).fire()
    assert runtime.command_mouse_status == "suspended"  # пока модель грузится
    mouse.release()
    mouse.rig.event(type="model.loaded")
    mouse.rig.event(type="result", utterance_id="file", text="проверка")
    runtime._tick_hotkey()
    retry = runtime._mouse_retry_timer
    assert retry is not None
    cast(FakeTimer, retry).fire()
    assert runtime.command_mouse_status == "ready"
    mouse.record_mouse()


def test_command_hotkey_press_reloads_model(monkeypatch: pytest.MonkeyPatch) -> None:
    mouse = MouseRuntimeRig(monkeypatch)
    runtime = mouse.runtime
    monkeypatch.setattr(
        runtime,
        "_resolve_model_request",
        lambda: {
            "type": "model.load",
            "id": "model",
            "revision": "revision",
            "dir": "/tmp/trusted",
            "layout": "onnx-asr-gigaam-v3",
            "variant": "gigaam-v3-e2e-rnnt",
            "threads": 2,
            "min_ram_mb": 768,
        },
    )
    runtime.restart_worker(_park=True)
    mouse.rig.event(type="hello")
    assert runtime.command_hotkey is not None
    runtime.command_hotkey.fsm.press(mouse.rig.now)
    assert mouse.sent("model.load") and runtime._loading_model
    assert runtime.command_hotkey.fsm.state is HotkeyState.IDLE
    assert_phase(runtime, DictationPhase.IDLE)


@pytest.mark.parametrize("value", [-1, 1, 30, "5", True, None, 5.0])
def test_invalid_saved_value_keeps_model_in_memory(value: object) -> None:
    settings = from_dict({"model_unload_minutes": value})
    assert settings.model_unload_minutes == 0


@pytest.mark.parametrize("minutes", MODEL_UNLOAD_MINUTES)
def test_saved_choice_is_loaded(minutes: int) -> None:
    assert from_dict({"model_unload_minutes": minutes}).model_unload_minutes == minutes


def test_bridge_saves_choice_applies_it_and_rejects_others() -> None:
    settings = Settings()
    save = Mock()
    apply = Mock(spec=SettingsApply)
    bridge = SettingsBridge(settings, save=save, apply=apply)
    prop = "modelUnloadMinutes"  # стабы Qt описывают свойство методом: зовём сеттер
    setattr(bridge, prop, 15)
    assert settings.model_unload_minutes == 15 and bridge.modelUnloadMinutes == 15
    save.assert_called_once_with(settings)
    apply.model_unload_minutes.assert_called_once_with(15)
    for invalid in (7, -5, True):
        setattr(bridge, prop, invalid)
    assert settings.model_unload_minutes == 15
    assert save.call_count == 1 and apply.model_unload_minutes.call_count == 1
